"""nightly.py のconfig解釈とask_claudeのモデル引数の検査(DB・claude CLI不要)。

nightly.py はimport時に SYSTEM_DIR/batch/config.json を読むため、
CLAUDE_SYSTEM_DIR を一時ディレクトリへ向けてから毎回別名でロードする。
実行: python3 -m unittest discover tests
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# nightly.py が import ringi するため、単体実行でもbatchディレクトリを見えるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nas" / "batch"))

NIGHTLY_PATH = Path(__file__).resolve().parent.parent / "nas" / "batch" / "nightly.py"

_seq = 0


def load_nightly(config=None):
    """config(dict|str|None)をbatch/config.jsonに置いた一時SYSTEM_DIRでnightlyをロード。"""
    global _seq
    tmp = Path(tempfile.mkdtemp(prefix="nightly-test-"))
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


if __name__ == "__main__":
    unittest.main()
