"""設定同期(sync_worker.py / session_start.sh)の検査。

Mac miniで /usr/bin/git と /usr/bin/python3 が壊れ(CommandLineTools不在)、
pull失敗を握りつぶしていたため設定同期が2週間止まっていた。
「動くものを探す」「失敗を必ず残す」の2点を固定する。
実行: python3 -m unittest discover tests
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "terminal" / "hooks"


def load_sync_worker(home: Path):
    with mock.patch.dict(os.environ, {"HOME": str(home)}):
        spec = importlib.util.spec_from_file_location("sync_worker_under_test",
                                                     HOOKS / "sync_worker.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class SyncWorkerCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="sw-home-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.spool = self.home / ".claude-spool"
        self.spool.mkdir()
        self.mod = load_sync_worker(self.home)
        self.mod.SPOOL = self.spool
        self.mod.SYNC_STATE = self.spool / "sync_state.json"

    def state(self):
        return json.loads((self.spool / "sync_state.json").read_text(encoding="utf-8"))

    def log(self):
        p = self.spool / "sync_worker.log"
        return p.read_text(encoding="utf-8") if p.is_file() else ""


class TestFindGit(SyncWorkerCase):
    def test_skips_broken_git_and_takes_working_one(self):
        """存在するだけでは採らない。--version が通るものを選ぶ。

        macOSの /usr/bin/git は CommandLineTools が壊れていると存在しても落ちる。
        """
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd[0])
            rc = 0 if cmd[0] == "/usr/local/bin/git" else 1   # 他は壊れている
            return subprocess.CompletedProcess(cmd, rc)

        with mock.patch.object(self.mod.os.path, "exists", return_value=True), \
             mock.patch.object(self.mod.shutil, "which", side_effect=lambda c: c), \
             mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            self.assertEqual(self.mod.find_git(), "/usr/local/bin/git")
        self.assertEqual(calls[0], "/opt/homebrew/bin/git")   # 優先順に試す
        self.assertIn("/usr/local/bin/git", calls)

    def test_none_when_all_broken(self):
        with mock.patch.object(self.mod.os.path, "exists", return_value=True), \
             mock.patch.object(self.mod.shutil, "which", side_effect=lambda c: c), \
             mock.patch.object(self.mod.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1)):
            self.assertIsNone(self.mod.find_git())

    def test_candidate_order_covers_each_platform(self):
        # Apple Silicon / Intel mac / PATH / Apple製 の順
        self.assertEqual(self.mod.GIT_CANDIDATES,
                         ("/opt/homebrew/bin/git", "/usr/local/bin/git", "git", "/usr/bin/git"))


class TestRecordSync(SyncWorkerCase):
    def test_success_resets_failures(self):
        self.mod.record_sync(False, "boom")
        self.mod.record_sync(False, "boom")
        self.assertEqual(self.state()["consecutive_failures"], 2)
        self.mod.record_sync(True)
        st = self.state()
        self.assertEqual(st["consecutive_failures"], 0)
        self.assertTrue(st["last_success_at"])
        self.assertEqual(st["last_error"], "")

    def test_failure_keeps_last_success(self):
        self.mod.record_sync(True)
        ok = self.state()["last_success_at"]
        self.mod.record_sync(False, "network unreachable")
        st = self.state()
        self.assertEqual(st["last_success_at"], ok)   # 成功時刻は残す
        self.assertIn("network", st["last_error"])

    def test_corrupt_state_is_replaced(self):
        (self.spool / "sync_state.json").write_text("{壊れたJSON", encoding="utf-8")
        self.mod.record_sync(True)
        self.assertEqual(self.state()["consecutive_failures"], 0)


class TestMainRecordsPullResult(SyncWorkerCase):
    def _run_main(self, git=None, pull_rc=0, stderr=""):
        def fake_run(cmd, **kw):
            if git and cmd[0] == git and "pull" in cmd:
                return subprocess.CompletedProcess(cmd, pull_rc, stdout="", stderr=stderr)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(self.mod, "find_git", return_value=git), \
             mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(sys, "argv", ["sync_worker.py", str(self.home / "cfg")]):
            self.mod.main()

    def test_pull_success_recorded(self):
        self._run_main(git="/usr/local/bin/git", pull_rc=0)
        self.assertEqual(self.state()["consecutive_failures"], 0)
        self.assertTrue(self.state()["last_success_at"])

    def test_pull_failure_is_logged_not_swallowed(self):
        """握りつぶさない: これが無いと2週間気づけなかった。"""
        self._run_main(git="/usr/local/bin/git", pull_rc=1, stderr="fatal: unable to access")
        self.assertIn("pull rc=1", self.log())
        st = self.state()
        self.assertEqual(st["consecutive_failures"], 1)
        self.assertIn("unable to access", st["last_error"])

    def test_missing_git_is_logged(self):
        self._run_main(git=None)
        self.assertIn("no working git", self.log())
        self.assertEqual(self.state()["consecutive_failures"], 1)


class TestSessionStartScript(unittest.TestCase):
    """session_start.sh も動くpython3を探す(hookは最小PATHで呼ばれる)。"""

    SCRIPT = (HOOKS / "session_start.sh").read_text(encoding="utf-8")

    def test_syntax(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(HOOKS / "session_start.sh")],
                                        capture_output=True).returncode, 0)

    def test_probes_candidates_in_order(self):
        resolver = (HOOKS / "find_python.sh").read_text(encoding="utf-8")
        self.assertIn("/opt/homebrew/bin/python3 /usr/local/bin/python3 python3 /usr/bin/python3",
                      resolver)
        self.assertIn("--version >/dev/null 2>&1", resolver)   # 存在確認だけにしない
        self.assertIn("find_python.sh", self.SCRIPT)           # hookはそれを使う

    def test_records_when_no_python(self):
        self.assertIn("no working python3", self.SCRIPT)
        self.assertIn("sync_worker.log", self.SCRIPT)

    def test_uses_resolved_python_everywhere(self):
        # 生の python3 呼び出しが残っていないこと(候補リストの行は除く)
        for line in self.SCRIPT.splitlines():
            if line.strip().startswith("for c in"):
                continue
            self.assertNotIn(' python3 "$CONFIG_DIR', line)

    def test_runs_with_minimal_path(self):
        """PATH=/usr/bin:/bin だけでも python3 を見つけて sync_worker を起動できる。"""
        home = Path(tempfile.mkdtemp(prefix="ss-home-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        fake_hooks = home / "cfg" / "hooks"
        fake_hooks.mkdir(parents=True)
        marker = home / "ran.txt"
        for name in ("sync_worker.py", "inbox_check.py"):
            (fake_hooks / name).write_text(
                f"open({str(marker)!r}, 'a').write({name!r} + chr(10))\n", encoding="utf-8")
        shutil.copy(HOOKS / "session_start.sh", fake_hooks / "session_start.sh")
        shutil.copy(HOOKS / "find_python.sh", fake_hooks / "find_python.sh")
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
        # 実機の /usr/bin/python3 が壊れていても候補探索で拾えることを見る
        subprocess.run(["/bin/sh", str(fake_hooks / "session_start.sh")], env=env,
                       capture_output=True, timeout=60)
        self.assertIn("inbox_check.py", marker.read_text(encoding="utf-8"))


class TestSharedPythonResolver(unittest.TestCase):
    """python3の解決は1か所(find_python.sh)に集約し、各スクリプトはそれを使う。"""

    def test_resolver_picks_working_interpreter(self):
        out = subprocess.run(
            ["/bin/sh", "-c", f'. "{HOOKS}/find_python.sh"; find_python'],
            capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")})
        self.assertEqual(out.returncode, 0, out.stderr)
        py = out.stdout.strip()
        self.assertTrue(py)
        # 返ってきたものが実際に動くこと(存在確認だけで済ませていない)
        self.assertEqual(subprocess.run([py, "--version"], capture_output=True,
                                        timeout=30).returncode, 0)

    def test_broken_interpreter_on_path_is_skipped(self):
        """PATH上に「存在するが動かない python3」があっても掴まない。

        Mac miniの /usr/bin/python3 がこれ(CommandLineTools不在でxcrunエラー)。
        候補が1つも動かない場合の分岐は find_git 側のテストでモック検査している。
        """
        d = Path(tempfile.mkdtemp(prefix="brokenpy-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        broken = d / "python3"
        broken.write_text("#!/bin/sh\necho 'xcrun: error' >&2\nexit 1\n", encoding="utf-8")
        broken.chmod(0o755)
        out = subprocess.run(
            ["/bin/sh", "-c", f'. "{HOOKS}/find_python.sh"; find_python'],
            capture_output=True, text=True, timeout=60,
            env={"PATH": f"{d}:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")})
        self.assertEqual(out.returncode, 0, out.stderr)
        py = out.stdout.strip()
        self.assertNotEqual(py, str(broken))
        self.assertEqual(subprocess.run([py, "--version"], capture_output=True,
                                        timeout=30).returncode, 0)

    def test_scripts_use_the_resolver(self):
        for rel in ("hooks/session_start.sh", "setup/backfill-claude.sh", "setup/setup.sh"):
            text = (ROOT / "terminal" / rel).read_text(encoding="utf-8")
            self.assertIn("find_python.sh", text, f"{rel} が共通処理を使っていない")
            self.assertIn("find_python)", text, f"{rel} が解決結果を使っていない")

    def test_setup_records_absolute_interpreter(self):
        """launchd/cron に登録するのは「動く」python3の絶対パス。"""
        text = (ROOT / "terminal" / "setup" / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("<string>$PY</string>", text)
        self.assertIn('CRON_LINE="17 * * * * $PY', text)
        self.assertNotIn("$(command -v python3)", text)


if __name__ == "__main__":
    unittest.main()
