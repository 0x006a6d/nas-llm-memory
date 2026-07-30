"""nightly.py のconfig解釈とask_claudeのモデル引数の検査(DB・claude CLI不要)。

nightly.py はimport時に SYSTEM_DIR/batch/config.json を読むため、
CLAUDE_SYSTEM_DIR を一時ディレクトリへ向けてから毎回別名でロードする。
実行: python3 -m unittest discover tests
"""
import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# nightly.py が import ringi するため、単体実行でもbatchディレクトリを見えるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nas" / "batch"))

NIGHTLY_PATH = Path(__file__).resolve().parent.parent / "nas" / "batch" / "nightly.py"

_seq = 0
_tmpdirs = []


def _sweep_tmpdirs():
    for d in _tmpdirs:
        shutil.rmtree(d, ignore_errors=True)


# load_nightlyは全テストが共用するため、呼び出し側ごとのaddCleanupではなく
# インタプリタ終了時にまとめて掃除する(/tmpに残骸を残さない)
atexit.register(_sweep_tmpdirs)


def load_nightly(config=None):
    """config(dict|str|None)をbatch/config.jsonに置いた一時SYSTEM_DIRでnightlyをロード。"""
    global _seq
    tmp = Path(tempfile.mkdtemp(prefix="nightly-test-"))
    _tmpdirs.append(tmp)
    (tmp / "batch").mkdir()
    if config is not None:
        text = config if isinstance(config, str) else json.dumps(config, ensure_ascii=False)
        (tmp / "batch" / "config.json").write_text(text, encoding="utf-8")
    _seq += 1
    with mock.patch.dict(os.environ, {"CLAUDE_SYSTEM_DIR": str(tmp)}):
        spec = importlib.util.spec_from_file_location(f"nightly_under_test_{_seq}", NIGHTLY_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def fake_claude_result(returncode=0):
    r = mock.Mock()
    r.returncode = returncode
    r.stdout = json.dumps({"subtype": "success", "result": "ok",
                           "usage": {}, "modelUsage": {"m": {}}})
    r.stderr = ""
    return r


class TestConfigLoading(unittest.TestCase):
    def test_old_format(self):
        mod = load_nightly({"model": "claude-opus-4-6"})
        self.assertEqual(mod.BATCH_MODEL, "claude-opus-4-6")
        self.assertEqual(mod.BATCH_CONFIG.get("model"), "claude-opus-4-6")

    def test_new_format(self):
        mod = load_nightly({"model": "m0", "roles": {"kian": "m1"},
                            "ringi": {"enabled": True}})
        self.assertEqual(mod.BATCH_MODEL, "m0")
        self.assertEqual(mod.BATCH_CONFIG["roles"]["kian"], "m1")

    def test_missing_or_broken(self):
        self.assertEqual(load_nightly(None).BATCH_MODEL, "")
        self.assertEqual(load_nightly("not json {").BATCH_MODEL, "")
        self.assertEqual(load_nightly("[1,2]").BATCH_CONFIG, {})  # dict以外は空扱い

    def test_system_dir_env(self):
        mod = load_nightly({"model": "x"})
        self.assertNotEqual(str(mod.SYSTEM_DIR), "/volume2/claude-system")


class TestAskClaudeModel(unittest.TestCase):
    def _run(self, mod, **kw):
        with mock.patch.object(mod.subprocess, "run",
                               return_value=fake_claude_result()) as m:
            out = mod.ask_claude("prompt", "label", **kw)
        self.assertEqual(out, "ok")
        return m.call_args[0][0]  # cmd

    def test_default_uses_batch_model(self):
        mod = load_nightly({"model": "m0"})
        cmd = self._run(mod)
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "m0")

    def test_explicit_model_overrides(self):
        mod = load_nightly({"model": "m0"})
        cmd = self._run(mod, model="m1")
        self.assertEqual(cmd[cmd.index("--model") + 1], "m1")

    def test_empty_model_means_cli_default(self):
        mod = load_nightly({"model": "m0"})
        cmd = self._run(mod, model="")
        self.assertNotIn("--model", cmd)

    def test_no_config_no_model_flag(self):
        mod = load_nightly(None)
        cmd = self._run(mod)
        self.assertNotIn("--model", cmd)


class TestAskClaudeBadEnvelope(unittest.TestCase):
    """終了コード0でも出力が壊れている場合(実際に2晩runが落ちた)。"""

    def _mod(self):
        return load_nightly({"model": "m0"})

    def test_retries_once_then_succeeds(self):
        mod = self._mod()
        good = fake_claude_result()
        bad = mock.Mock(returncode=0, stdout="[", stderr="")
        with mock.patch.object(mod.subprocess, "run", side_effect=[bad, good]) as run:
            out = mod.ask_claude("p", "verify:x")
        self.assertEqual(out, "ok")
        self.assertEqual(run.call_count, 2)      # 1度だけ問い直す

    def test_second_failure_reports_what_came_back(self):
        mod = self._mod()
        bad = mock.Mock(returncode=0, stdout="[", stderr="boom")
        with mock.patch.object(mod.subprocess, "run", side_effect=[bad, bad]):
            with self.assertRaises(RuntimeError) as cm:
                mod.ask_claude("p", "organize:y")
        msg = str(cm.exception)
        self.assertIn("claude output not json (organize:y)", msg)
        self.assertIn("stdout[:200]='['", msg)   # 何が返ってきたかを添える
        self.assertIn("boom", msg)

    def test_nonzero_exit_is_not_retried(self):
        """returncode!=0 は意味のある失敗なので問い直さない(コストを二重に払わない)。"""
        mod = self._mod()
        ng = mock.Mock(returncode=1, stdout="", stderr="usage limit")
        with mock.patch.object(mod.subprocess, "run", side_effect=[ng]) as run:
            with self.assertRaises(RuntimeError):
                mod.ask_claude("p", "verify:z")
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
