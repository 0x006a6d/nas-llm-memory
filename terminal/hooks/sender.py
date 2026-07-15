#!/usr/bin/env python3
"""sender — スプール内の未送信分をNAS ingest APIへPOSTする(設計書§3.2)

at-least-once。重複はDB側UNIQUE制約で吸収される。
NAS到達不能時は静かに諦める(スプールに残ることが記録)。
"""
import fcntl
import json
import sys
import time
import urllib.request
from pathlib import Path

SPOOL = Path.home() / ".claude-spool"
CONFIG = SPOOL / "config.json"
SENT_KEEP_DAYS = 14  # 障害復旧用にsentを保持(設計書§10 P0)


def main():
    if not CONFIG.exists():
        return
    cfg = json.loads(CONFIG.read_text())
    url = cfg["ingest_url"].rstrip("/") + "/ingest"
    token = cfg["api_token"]

    pending = SPOOL / "pending"
    sent = SPOOL / "sent"
    if not pending.exists():
        return
    sent.mkdir(parents=True, exist_ok=True)

    # 多重起動防止
    lock = open(SPOOL / ".sender.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # 既に別のsenderが動いている

    for f in sorted(pending.glob("*.json")):
        try:
            req = urllib.request.Request(
                url,
                data=f.read_bytes(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    f.rename(sent / f.name)
                else:
                    return
        except Exception:
            return  # 到達不能: 次回再送

    # sentの世代整理
    cutoff = time.time() - SENT_KEEP_DAYS * 86400
    for f in sent.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
