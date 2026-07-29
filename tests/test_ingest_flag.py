"""ingest API の POST /flag の検査(DB不要)。

app.py は import 時に DB_PASSWORD_FILE/API_TOKEN_FILE と psycopg/fastapi を要求する。
env は一時ファイルに向け、依存が無い端末ではモジュールごと skip する
(他のtestsがapp.pyをimportしていないのはこの依存のため)。
実行: python3 -m unittest discover tests
"""
import contextlib
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "nas" / "ingest"))

TOKEN = "test-token-0123456789"
_SECRETS = tempfile.TemporaryDirectory(prefix="ingest-test-")  # プロセス終了時に片付く
(Path(_SECRETS.name) / "db_password").write_text("pw\n")
(Path(_SECRETS.name) / "api_token").write_text(TOKEN + "\n")

app_mod = None
TestClient = None
skip_reason = ""
with mock.patch.dict(os.environ, {
        "DB_PASSWORD_FILE": str(Path(_SECRETS.name) / "db_password"),
        "API_TOKEN_FILE": str(Path(_SECRETS.name) / "api_token")}):
    try:
        import app as app_mod  # noqa: E402
        from fastapi.testclient import TestClient  # noqa: E402
    except ModuleNotFoundError as e:  # fastapi/psycopg 未導入の端末だけ skip
        if e.name and e.name.split(".")[0] in (
                "fastapi", "starlette", "httpx", "psycopg", "psycopg_pool"):
            skip_reason = f"ingest依存が無い環境: {e}"
        else:  # app.py 自体の import 破損は隠さず失敗させる
            raise


class FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    """conn.execute() のSQLと引数を記録し、用意した行を順に返す。"""

    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return FakeCursor(self.rows.pop(0) if self.rows else None)


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @contextlib.contextmanager
    def connection(self):
        yield self.conn

    def open(self):
        pass

    def close(self):
        pass


@unittest.skipUnless(app_mod, skip_reason or "app未ロード")
class TestIngestFlag(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConn([("s-1",)])
        self.pool_patch = mock.patch.object(app_mod, "pool", FakePool(self.conn))
        self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)
        self.client = TestClient(app_mod.app)

    def post(self, body, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.post("/flag", json=body, headers=headers)

    def test_no_token_is_401(self):
        r = self.post({"session_id": "s-1"}, token=None)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.conn.calls, [])

    def test_bad_token_is_401(self):
        r = self.post({"session_id": "s-1"}, token="wrong")
        self.assertEqual(r.status_code, 401)

    def test_invalid_session_id_is_400(self):
        for sid in ("", "a b", "a/b", "セッション", "x" * 121, 12, None):
            with self.subTest(sid=sid):
                r = self.post({"session_id": sid})
                self.assertEqual(r.status_code, 400)
        self.assertEqual(self.conn.calls, [])

    def test_note_too_long_is_400(self):
        r = self.post({"session_id": "s-1", "note": "x" * (app_mod.FLAG_NOTE_MAX + 1)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.conn.calls, [])
        # 上限ちょうどは通す(切り詰めない)
        r = self.post({"session_id": "s-1", "note": "x" * app_mod.FLAG_NOTE_MAX})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.conn.calls[0][1][1]), app_mod.FLAG_NOTE_MAX)

    def test_note_must_be_string(self):
        self.assertEqual(self.post({"session_id": "s-1", "note": {"a": 1}}).status_code, 400)

    def test_unknown_op_is_400(self):
        self.assertEqual(self.post({"session_id": "s-1", "op": "toggle"}).status_code, 400)
        self.assertEqual(self.conn.calls, [])

    def test_add_upserts_and_returns_session_id(self):
        sid = "1e5f2c3a-1111-2222-3333-444455556666"
        self.conn.rows = [(sid,)]
        day0 = datetime.now().strftime("%Y%m%d")
        r = self.post({"session_id": sid, "note": "重要", "device": "MacBook Pro/夜"})
        day1 = datetime.now().strftime("%Y%m%d")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"session_id": sid, "op": "add"})
        sql, params = self.conn.calls[0]
        self.assertIn("INSERT INTO flags", sql)
        self.assertIn("ON CONFLICT (session_id) DO UPDATE SET note = excluded.note", sql)
        self.assertIn("RETURNING session_id", sql)
        self.assertNotIn("created_by = excluded", sql)  # 出所は初回値を保持
        self.assertEqual(params[0], sid)
        self.assertEqual(params[1], "重要")
        # device は [^A-Za-z0-9._-] を除去。日付は日跨ぎ実行でもどちらかの日
        self.assertIn(params[2], {f"session-MacBookPro-{day0}", f"session-MacBookPro-{day1}"})

    def test_op_omitted_defaults_to_add(self):
        r = self.post({"session_id": "s-1"})
        self.assertEqual(r.json()["op"], "add")
        self.assertIn("INSERT INTO flags", self.conn.calls[0][0])
        self.assertEqual(self.conn.calls[0][1][1], "")  # note省略は空文字

    def test_device_omitted_is_unknown(self):
        self.post({"session_id": "s-1"})
        self.assertTrue(self.conn.calls[0][1][2].startswith("session-unknown-"))

    def test_long_device_is_truncated(self):
        self.post({"session_id": "s-1", "device": "d" * 200})
        self.assertEqual(self.conn.calls[0][1][2].split("-")[1], "d" * 60)

    def test_note_is_masked(self):
        self.post({"session_id": "s-1", "note": "key AKIAABCDEFGHIJKLMNOP の件"})
        self.assertEqual(self.conn.calls[0][1][1], "key [REDACTED:aws-access-key] の件")

    def test_remove_reports_deleted(self):
        self.conn.rows = [("s-1",)]
        r = self.post({"session_id": "s-1", "op": "remove"})
        self.assertEqual(r.json(), {"op": "remove", "deleted": True})
        sql, params = self.conn.calls[0]
        self.assertIn("DELETE FROM flags WHERE session_id = %s RETURNING session_id", sql)
        self.assertEqual(params, ("s-1",))

    def test_remove_missing_row_is_not_deleted(self):
        self.conn.rows = [None]
        r = self.post({"session_id": "s-1", "op": "remove"})
        self.assertEqual(r.json(), {"op": "remove", "deleted": False})

    def test_session_id_boundary_lengths(self):
        self.assertEqual(self.post({"session_id": "x" * 120}).status_code, 200)
        self.assertEqual(self.post({"session_id": "x" * 121}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
