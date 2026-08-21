"""hooks_apply.py が同じhookを多重登録しないことの検査。

新母艦(RTX 5090機)のsettings.jsonでSessionEndが3つ、SessionStartが2つに
増えていた。手書きの `$HOME` 表記と、manifestを展開した絶対パス表記を
別物と判定していたため、apply のたびに展開形が積み増されていた。
同一視の範囲は command だけでなく matcher / timeout / if まで含める
(同じ command を matcher 違いで登録する構成があるため)。
実行: python3 -m unittest discover tests
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "terminal" / "hooks"

SPOOL_CMD = "python3 $HOME/claude-config/hooks/spool_write.py"


def load_hooks_apply(home: Path, manifest: Path = None):
    env = {"HOME": str(home)}
    if manifest is not None:
        env["HOOKS_MANIFEST"] = str(manifest)
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("hooks_apply_under_test",
                                                      HOOKS / "hooks_apply.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class SameCmdCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="ha-home-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.mod = load_hooks_apply(self.home)
        self.h = str(self.home)

    def test_expanded_form_matches_home_form(self):
        self.assertTrue(self.mod._same_cmd(
            "python3 $HOME/claude-config/hooks/spool_write.py",
            f"python3 {self.h}/claude-config/hooks/spool_write.py"))

    def test_quoting_difference_matches(self):
        self.assertTrue(self.mod._same_cmd(
            'python3 "$HOME/claude-config/hooks/spool_write.py"',
            f"python3 {self.h}/claude-config/hooks/spool_write.py"))
        self.assertTrue(self.mod._same_cmd(
            '"$HOME/claude-config/hooks/session_start.sh"',
            f"{self.h}/claude-config/hooks/session_start.sh"))

    def test_braced_home_matches(self):
        self.assertTrue(self.mod._same_cmd(
            "${HOME}/claude-config/hooks/session_start.sh",
            f"{self.h}/claude-config/hooks/session_start.sh"))

    def test_different_commands_do_not_match(self):
        self.assertFalse(self.mod._same_cmd(
            "python3 $HOME/claude-config/hooks/spool_write.py",
            f"python3 {self.h}/claude-config/hooks/sender.py"))

    def test_unbalanced_quotes_do_not_raise(self):
        self.assertFalse(self.mod._same_cmd('echo "unclosed', "echo unclosed"))


class ApplyManifestCase(unittest.TestCase):
    """手書きエントリがある状態で apply したときの増減。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="ha-apply-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        (self.home / ".claude").mkdir()
        self.settings = self.home / ".claude" / "settings.json"
        self.manifest = self.home / "hooks-manifest.json"

    def _write_settings(self, matcher, timeout=None):
        hook = {"type": "command", "command": SPOOL_CMD}
        if timeout is not None:
            hook["timeout"] = timeout
        entry = {"hooks": [hook]}
        if matcher is not None:
            entry["matcher"] = matcher
        self.settings.write_text(json.dumps(
            {"hooks": {"PreToolUse": [entry]}}, ensure_ascii=False), encoding="utf-8")

    def _write_manifest(self, matcher, timeout=None):
        hook = {"name": "テスト用", "event": "PreToolUse", "command": SPOOL_CMD,
                "targets": ["claude"], "matcher": matcher, "if": None,
                "timeout": timeout}
        self.manifest.write_text(json.dumps({"hooks": [hook]}, ensure_ascii=False),
                                 encoding="utf-8")

    def _apply(self):
        mod = load_hooks_apply(self.home, self.manifest)
        report = mod.apply_manifest()
        hooks = json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]
        return mod, report, hooks

    def test_home_form_entry_is_adopted_not_duplicated(self):
        self._write_settings(matcher="Bash")
        self._write_manifest(matcher="Bash")
        mod, report, hooks = self._apply()
        self.assertEqual(len(hooks["PreToolUse"]), 1)
        self.assertEqual(report["added"], [])
        self.assertEqual(len(report["adopted"]), 1)
        # 手書きの $HOME 表記が展開形に書き換えられていないこと
        self.assertEqual(hooks["PreToolUse"][0]["hooks"][0]["command"], SPOOL_CMD)

    def test_apply_twice_does_not_grow(self):
        self._write_settings(matcher="Bash")
        self._write_manifest(matcher="Bash")
        self._apply()
        _, _, hooks = self._apply()
        self.assertEqual(len(hooks["PreToolUse"]), 1)

    def test_different_matcher_is_added(self):
        self._write_settings(matcher="Write")
        self._write_manifest(matcher="Bash")
        _, report, hooks = self._apply()
        self.assertEqual(len(hooks["PreToolUse"]), 2)
        self.assertEqual(len(report["added"]), 1)
        matchers = sorted(e.get("matcher") for e in hooks["PreToolUse"])
        self.assertEqual(matchers, ["Bash", "Write"])

    def test_different_timeout_is_added(self):
        self._write_settings(matcher="Bash", timeout=5)
        self._write_manifest(matcher="Bash", timeout=10)
        _, report, hooks = self._apply()
        self.assertEqual(len(hooks["PreToolUse"]), 2)
        self.assertEqual(len(report["added"]), 1)

    def test_status_reports_home_form_as_applied(self):
        self._write_settings(matcher="Bash")
        self._write_manifest(matcher="Bash")
        mod = load_hooks_apply(self.home, self.manifest)
        rows = mod.manifest_status()["rows"]
        self.assertEqual(rows[0]["state"]["claude"], "applied")


if __name__ == "__main__":
    unittest.main()
