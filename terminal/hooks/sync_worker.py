#!/usr/bin/env python3
"""SessionStart worker — 設定pull → sender起動の直列化(session_start.shから起動)

- 排他はfcntl.flock(macOS/Linux両対応)。ロックはユーザー所有の~/.claude-spool内に置く
  (共有/tmpの予測可能パスを使わない)
- pullはtimeout付き。失敗してもsenderは起動する(次のセッションで再試行)
"""
import fcntl
import subprocess
import sys
from pathlib import Path

SPOOL = Path.home() / ".claude-spool"


def main():
    config_dir = sys.argv[1]
    SPOOL.mkdir(parents=True, exist_ok=True)
    lock = open(SPOOL / ".sync_worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # 別のSessionStartが同期中
    try:
        subprocess.run(["git", "-C", config_dir, "pull", "--ff-only", "-q"],
                       capture_output=True, timeout=60)
    except Exception:
        pass  # pull不能(オフライン等)でも続行
    subprocess.run([sys.executable, str(Path(config_dir) / "hooks" / "sender.py")],
                   timeout=1800)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
