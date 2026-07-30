#!/usr/bin/env python3
"""SessionStart worker — 設定pull → sender起動の直列化(session_start.shから起動)

- 排他はfcntl.flock(macOS/Linux両対応)。ロックはユーザー所有の~/.claude-spool内に置く
  (共有/tmpの予測可能パスを使わない)
- pullはtimeout付き。失敗してもsenderは起動する(次のセッションで再試行)。
  ただし失敗は必ず記録する: 握りつぶしていたため、Mac miniで
  /usr/bin/git が壊れて設定同期が2週間止まっていたのに誰も気づけなかった
- gitは端末ごとに置き場所が違う(Apple Silicon=/opt/homebrew, Intel=/usr/local,
  WSL/NAS=/usr/bin)。--version が通るものを探して使う
"""
import fcntl
import json
import os
import shutil
import subprocess
import time
import sys
from pathlib import Path

SPOOL = Path.home() / ".claude-spool"
SYNC_STATE = SPOOL / "sync_state.json"
GIT_CANDIDATES = ("/opt/homebrew/bin/git", "/usr/local/bin/git", "git", "/usr/bin/git")


def note(msg: str):
    """sync_worker.log へ1行追記(失敗しても本処理は続ける)。"""
    try:
        with open(SPOOL / "sync_worker.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def find_git():
    """動くgitを探す。macOSの /usr/bin/git は CommandLineTools が壊れていると
    xcrun エラーで落ちるため、存在確認だけでは不十分で --version を試す。"""
    for c in GIT_CANDIDATES:
        path = shutil.which(c) if "/" not in c else (c if os.path.exists(c) else None)
        if not path:
            continue
        try:
            if subprocess.run([path, "--version"], capture_output=True,
                              timeout=10).returncode == 0:
                return path
        except Exception:
            continue
    return None


def record_sync(ok: bool, detail: str = ""):
    """設定同期の成否を残す(dashboardの収受簿タブが「何日失敗しているか」を出す)。"""
    try:
        st = json.loads(SYNC_STATE.read_text(encoding="utf-8")) if SYNC_STATE.is_file() else {}
    except Exception:
        st = {}
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    st["last_attempt_at"] = now
    if ok:
        st["last_success_at"] = now
        st["consecutive_failures"] = 0
        st["last_error"] = ""
    else:
        st["consecutive_failures"] = int(st.get("consecutive_failures") or 0) + 1
        st["last_error"] = detail[:300]
    try:
        tmp = SYNC_STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.rename(SYNC_STATE)
    except OSError:
        pass


def main():
    config_dir = sys.argv[1]
    SPOOL.mkdir(parents=True, exist_ok=True)
    lock = open(SPOOL / ".sync_worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # 別のSessionStartが同期中
    git = find_git()
    if git is None:
        note("no working git (設定同期できない)")
        record_sync(False, "no working git")
    else:
        try:
            proc = subprocess.run([git, "-C", config_dir, "pull", "--ff-only", "-q"],
                                  capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                record_sync(True)
            else:
                # オフラインでもここに来る。記録は残すが収集は続行する
                err = (proc.stderr or proc.stdout or "").strip()
                note(f"pull rc={proc.returncode}: {err[-300:]}")
                record_sync(False, f"rc={proc.returncode} {err[-200:]}")
        except Exception as exc:
            note(f"pull error: {exc!r}")
            record_sync(False, repr(exc)[:200])
    try:
        # pull した hooks-manifest.json を実設定(Claude/Codex)へ展開する
        proc = subprocess.run([sys.executable,
                               str(Path(config_dir) / "hooks" / "hooks_apply.py"),
                               "--quiet"],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            with open(SPOOL / "sync_worker.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} "
                        f"hooks_apply rc={proc.returncode}: {proc.stderr[-300:]}\n")
    except subprocess.TimeoutExpired:
        with open(SPOOL / "sync_worker.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} hooks_apply timeout\n")
    except Exception as exc:  # 適用失敗でも収集は続行(次のセッションで再試行)
        try:
            with open(SPOOL / "sync_worker.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} hooks_apply error: {exc!r}\n")
        except OSError:
            pass
    try:
        # pull した routing.json をプロジェクトindex注入レジストリへ適用する
        # (senderより先に: 直後のagents_sync同期が新しいレジストリで動くように)
        proc = subprocess.run([sys.executable,
                               str(Path(config_dir) / "hooks" / "routing_apply.py"),
                               config_dir, "--quiet"],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            with open(SPOOL / "sync_worker.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} "
                        f"routing_apply rc={proc.returncode}: {proc.stderr[-300:]}\n")
    except Exception as exc:
        try:
            with open(SPOOL / "sync_worker.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} routing_apply error: {exc!r}\n")
        except OSError:
            pass
    subprocess.run([sys.executable, str(Path(config_dir) / "hooks" / "sender.py")],
                   timeout=1800)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
