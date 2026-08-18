"""opencode 収集経路の検査(DB・NAS不要)。

sender.spool_opencode() は一時SQLite(実物と同じ列)に対して、
parsers.parse_opencode() は sender が作るスプール形式に対して検査する。
実行: python3 -m unittest discover tests
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "nas" / "ingest"))

import parsers  # noqa: E402


def load_sender(home: Path):
    """HOMEを一時ディレクトリに向けてsender.pyをロードする(SPOOL等がHOME依存)。"""
    with mock.patch.dict(os.environ, {"HOME": str(home)}):
        spec = importlib.util.spec_from_file_location(
            "sender_under_test", ROOT / "terminal" / "hooks" / "sender.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def make_db(path: Path, *, directory="/tmp/proj", session="ses_1",
            messages=(), parts=()):
    """opencode の実DBと同じ列を持つ最小のセッションDBを作る。"""
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, title TEXT,
                            agent TEXT, model TEXT, time_created INT, time_updated INT);
      CREATE TABLE message (id TEXT, session_id TEXT, time_created INT,
                            time_updated INT, data TEXT);
      CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,
                         time_created INT, time_updated INT, data TEXT);
    """)
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
                (session, "global", directory, "t", "build",
                 json.dumps({"id": "kimi-k3", "providerID": "opencode-go"}), 0, 0))
    for m in messages:
        con.execute("INSERT INTO message VALUES (?,?,?,?,?)", m)
    for p in parts:
        con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", p)
    con.commit()
    con.close()


def msg(mid, created, role, *, updated=None, agent="build", model=None):
    data = {"role": role, "agent": agent, "time": {"created": created}}
    if model:
        data["model"] = model
    return (mid, "ses_1", created, updated if updated is not None else created,
            json.dumps(data, ensure_ascii=False))


def part(pid, mid, created, data):
    return (pid, mid, "ses_1", created, created, json.dumps(data, ensure_ascii=False))


class TestSpoolOpencode(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="oc-home-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        (self.home / ".claude-spool").mkdir()
        self.db = self.home / "opencode.db"
        self.mod = load_sender(self.home)
        self.mod.SPOOL = self.home / ".claude-spool"

    def run_scan(self, now=10_000.0):
        with mock.patch.dict(os.environ, {"OPENCODE_DB": str(self.db)}), \
             mock.patch.object(self.mod.time, "time", return_value=now), \
             mock.patch.object(self.mod, "_git_remote", return_value=None), \
             mock.patch.object(self.mod.exclude, "load_entries", return_value=[]):
            self.mod.spool_opencode()
        return sorted((self.home / ".claude-spool" / "pending").glob("*.json"))

    def test_message_and_parts_are_spooled(self):
        make_db(self.db,
                messages=[msg("msg_1", 1_000_000, "user"),
                          msg("msg_2", 1_000_100, "assistant")],
                parts=[part("prt_1", "msg_1", 1_000_000, {"type": "text", "text": "こんにちは"}),
                       part("prt_2", "msg_2", 1_000_100, {"type": "reasoning", "text": "考える"}),
                       part("prt_3", "msg_2", 1_000_101, {"type": "text", "text": "やります"})])
        files = self.run_scan()
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["agent"], "opencode")
        self.assertEqual(payload["session_id"], "ses_1")
        self.assertEqual(payload["originator"], "build")
        lines = [json.loads(x) for x in payload["transcript"].splitlines()]
        self.assertEqual([x["id"] for x in lines], ["msg_1", "msg_2"])
        self.assertEqual(len(lines[1]["parts"]), 2)  # reasoningも生では残す(落とすのはparser)

    def test_watermark_sends_only_new_messages(self):
        make_db(self.db, messages=[msg("msg_1", 1_000_000, "user")],
                parts=[part("prt_1", "msg_1", 1_000_000, {"type": "text", "text": "1通目"})])
        self.assertEqual(len(self.run_scan()), 1)
        self.assertEqual(len(self.run_scan()), 1)  # 2回目は増えない(既送分は再送しない)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO message VALUES (?,?,?,?,?)", msg("msg_2", 1_000_200, "user"))
        con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                    part("prt_2", "msg_2", 1_000_200, {"type": "text", "text": "2通目"}))
        con.commit(); con.close()
        files = self.run_scan()
        self.assertEqual(len(files), 2)
        new = [f for f in files if "msg_2" in f.read_text(encoding="utf-8")]
        self.assertEqual(len(new), 1)
        payload = json.loads(new[0].read_text(encoding="utf-8"))
        ids = [json.loads(x)["id"] for x in payload["transcript"].splitlines()]
        self.assertEqual(ids, ["msg_2"])  # 差分だけ

    def test_in_flight_message_is_deferred(self):
        """更新が新しすぎる(書きかけの可能性がある)messageは次回に回す。"""
        make_db(self.db,
                messages=[msg("msg_1", 1_000, "assistant", updated=9_999_000)],
                parts=[part("prt_1", "msg_1", 1_000, {"type": "text", "text": "途中"})])
        self.assertEqual(self.run_scan(now=10_000.0), [])

    def test_watermark_stops_before_deferred_message(self):
        """書きかけmessageより後の分を送っても、透かしは書きかけの手前で止める。"""
        make_db(self.db, messages=[
            msg("msg_1", 1_000, "assistant", updated=9_999_000),   # 書きかけ(保留)
            msg("msg_2", 2_000, "user"),                           # 確定済み(後発)
        ], parts=[part("prt_1", "msg_1", 1_000, {"type": "text", "text": "途中"}),
                  part("prt_2", "msg_2", 2_000, {"type": "text", "text": "次の指示"})])
        self.assertEqual(self.run_scan(now=10_000.0), [])  # 保留を飛び越して送らない
        state = self.home / ".claude-spool" / "opencode-sent.jsonl"
        self.assertFalse(state.exists())  # 透かしも進めない
        # 書きかけが確定すれば両方送られる
        con = sqlite3.connect(self.db)
        con.execute("UPDATE message SET time_updated = 1000 WHERE id = 'msg_1'")
        con.commit(); con.close()
        files = self.run_scan(now=10_000.0)
        ids = [json.loads(x)["id"]
               for x in json.loads(files[0].read_text(encoding="utf-8"))["transcript"].splitlines()]
        self.assertEqual(ids, ["msg_1", "msg_2"])

    def test_large_session_is_split(self):
        """初回走査の大きなセッションは1ペイロードに詰め込まない。"""
        big = "あ" * 2_000
        msgs, parts = [], []
        for i in range(5):
            msgs.append(msg(f"msg_{i}", 1_000 + i, "user"))
            parts.append(part(f"prt_{i}", f"msg_{i}", 1_000 + i, {"type": "text", "text": big}))
        make_db(self.db, messages=msgs, parts=parts)
        self.mod.OPENCODE_CHUNK_BYTES = 8_000   # 1ペイロードに2件程度
        files = self.run_scan()
        self.assertGreater(len(files), 1)
        got = []
        for f in files:
            p = json.loads(f.read_text(encoding="utf-8"))
            got += [json.loads(x)["id"] for x in p["transcript"].splitlines()]
        self.assertEqual(sorted(got), sorted(f"msg_{i}" for i in range(5)))  # 全件そろう
        self.assertEqual(len(got), len(set(got)))                            # 重複しない

    def test_streaming_parts_defer_the_message(self):
        """message行が据え置きでもpartが更新中なら書きかけ扱いにする。

        実測した取りこぼし: 1時間半かかった応答は message.time_updated が
        開始時刻のまま part だけが伸び続けたため、本文が空の状態で送られ
        (パーサは行を作らない)、透かしだけ進んで永久に欠落した。
        """
        make_db(self.db,
                messages=[msg("msg_1", 1_000, "assistant", updated=1_000)],  # 行は据え置き
                parts=[part("prt_1", "msg_1", 1_000, {"type": "reasoning", "text": "考え中"})])
        con = sqlite3.connect(self.db)
        con.execute("UPDATE part SET time_updated = 9999000 WHERE id='prt_1'")  # 執筆中
        con.commit(); con.close()
        self.assertEqual(self.run_scan(now=10_000.0), [])          # 送らない
        self.assertFalse((self.home / ".claude-spool" / "opencode-sent.jsonl").exists())
        # 応答が完成したら本文ごと送られる
        con = sqlite3.connect(self.db)
        con.execute("UPDATE part SET time_updated = 1000 WHERE id='prt_1'")
        con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                    part("prt_2", "msg_1", 1_100, {"type": "text", "text": "書き上がった本文"}))
        con.commit(); con.close()
        files = self.run_scan(now=10_000.0)
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertIn("書き上がった本文", payload["transcript"])

    def test_excluded_project_is_not_spooled_but_watermarked(self):
        make_db(self.db, directory="/tmp/secret",
                messages=[msg("msg_1", 1_000_000, "user")],
                parts=[part("prt_1", "msg_1", 1_000_000, {"type": "text", "text": "秘密"})])
        with mock.patch.dict(os.environ, {"OPENCODE_DB": str(self.db)}), \
             mock.patch.object(self.mod.time, "time", return_value=10_000.0), \
             mock.patch.object(self.mod, "_git_remote", return_value=None), \
             mock.patch.object(self.mod.exclude, "load_entries", return_value=[]), \
             mock.patch.object(self.mod.exclude, "is_excluded", return_value=True):
            self.mod.spool_opencode()
        self.assertEqual(list((self.home / ".claude-spool" / "pending").glob("*.json")), [])
        state = (self.home / ".claude-spool" / "opencode-sent.jsonl").read_text(encoding="utf-8")
        self.assertIn('"watermark": 1000000', state)  # 走査済みとして記録される

    def test_missing_db_is_noop(self):
        with mock.patch.dict(os.environ, {"OPENCODE_DB": str(self.home / "none.db")}):
            self.mod.spool_opencode()  # 例外にならない(opencode未使用端末)


class TestParseOpencode(unittest.TestCase):
    def payload(self, lines, **kw):
        p = {"device": "macbook", "agent": "opencode", "session_id": "ses_1",
             "project_dir": "/Users/jm/go", "git_remote_url": None,
             "originator": "build",
             "context_model": json.dumps({"id": "kimi-k3", "providerID": "opencode-go"}),
             "transcript": "\n".join(json.dumps(x, ensure_ascii=False) for x in lines)}
        p.update(kw)
        return p

    def test_roles_and_ids(self):
        rows = parsers.parse_opencode(self.payload([
            {"id": "msg_1", "time_created": 1785186895403,
             "message": {"role": "user", "agent": "build"},
             "parts": [{"type": "text", "text": "直して"}]},
            {"id": "msg_2", "time_created": 1785186895417,
             "message": {"role": "assistant", "agent": "build",
                         "modelID": "kimi-k3", "providerID": "opencode-go",
                         "path": {"cwd": "/Users/jm/go"}},
             "parts": [{"type": "reasoning", "text": "考える"},
                       {"type": "text", "text": "直した"},
                       {"type": "tool", "tool": "bash",
                        "state": {"status": "completed", "input": {"command": "ls"},
                                  "output": "a.txt"}}]},
        ]), payload_id=7)
        self.assertEqual([r["role"] for r in rows], ["user", "assistant"])
        self.assertEqual([r["message_uuid"] for r in rows], ["msg_1", "msg_2"])
        self.assertEqual({r["session_id"] for r in rows}, {"opencode:ses_1"})
        self.assertEqual({r["agent"] for r in rows}, {"opencode"})
        self.assertEqual({r["originator"] for r in rows}, {"build"})
        self.assertEqual(rows[1]["model"], "opencode-go/kimi-k3")
        self.assertTrue(rows[0]["ts"].startswith("2026-"))
        body = rows[1]["content"]
        self.assertIn("直した", body)
        self.assertIn("[tool_use:bash]", body)
        self.assertIn("[tool_result] a.txt", body)
        self.assertNotIn("考える", body)  # reasoningは保存しない

    def test_model_falls_back_to_session(self):
        rows = parsers.parse_opencode(self.payload([
            {"id": "msg_1", "time_created": 1, "message": {"role": "user"},
             "parts": [{"type": "text", "text": "やあ"}]}]), payload_id=1)
        self.assertEqual(rows[0]["model"], "opencode-go/kimi-k3")

    def test_empty_and_broken_lines_skipped(self):
        p = self.payload([
            {"id": "msg_1", "time_created": 1, "message": {"role": "user"}, "parts": []},
            {"id": "msg_2", "time_created": 2, "message": {"role": "system"},
             "parts": [{"type": "text", "text": "system"}]},
        ])
        p["transcript"] += "\n{壊れたJSON\n"
        self.assertEqual(parsers.parse_opencode(p, payload_id=1), [])

    def test_subtask_recorded(self):
        rows = parsers.parse_opencode(self.payload([
            {"id": "msg_1", "time_created": 1, "message": {"role": "assistant"},
             "parts": [{"type": "subtask", "agent": "review",
                        "description": "差分を見る"}]}]), payload_id=1)
        self.assertIn("[subtask:review] 差分を見る", rows[0]["content"])


if __name__ == "__main__":
    unittest.main()
