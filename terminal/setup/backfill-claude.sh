#!/bin/bash
# 初回データ移行(追補設計書§1): 過去のClaude Codeトランスクリプトとauto memoryを
# 定常経路と同じペイロード形式でローカルスプールに書き出す。各端末で一度だけ実行する。
#
# - ネットワークには触らない。送信は通常のsenderに任せる
# - 再実行しても無害: event_idは内容位置から決定的に生成され、ingest側の
#   UNIQUE(event_id) / UNIQUE(session_id, message_uuid) が重複を吸収する
# - Claude Codeのローカル保持期間で古いセッションは消えるため、稼働開始の最初期に実行する
set -euo pipefail

exec python3 - <<'PYEOF'
import hashlib
import json
import socket
import subprocess
import time
from pathlib import Path

SPOOL = Path.home() / ".claude-spool" / "pending"
PROJECTS = Path.home() / ".claude" / "projects"
DEVICE = socket.gethostname()


def iso(ts):
    t = time.localtime(ts)
    s = time.strftime("%Y-%m-%dT%H:%M:%S%z", t)
    return s[:-2] + ":" + s[-2:]


def git_remote(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
        out = r.stdout.strip()
        return out if r.returncode == 0 and out else None
    except Exception:
        return None


def event_id(kind, path, mtime):
    # 決定的なID: 再実行時に同じスプール名/同じevent_idになり、二重投入されない
    h = hashlib.sha1(f"{DEVICE}:{kind}:{path}:{int(mtime)}".encode()).hexdigest()
    return f"backfill-{h}"


def spool(payload, name):
    SPOOL.mkdir(parents=True, exist_ok=True)
    tmp = SPOOL / (name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.rename(SPOOL / name)


def main():
    if not PROJECTS.exists():
        print(f"{PROJECTS} がありません。この端末に移行対象はありません")
        return
    n_t = n_m = n_skip = 0
    for proj_dir in sorted(PROJECTS.iterdir()):
        if not proj_dir.is_dir():
            continue

        # --- 過去トランスクリプト
        for jl in sorted(proj_dir.glob("*.jsonl")):
            try:
                mtime = jl.stat().st_mtime
                text = jl.read_text(errors="replace")
            except Exception:
                n_skip += 1
                continue
            # project_keyはディレクトリ名(復元が曖昧)ではなくJSONL内のcwdから解決する(§1)
            cwd = session_id = None
            for line in text.splitlines():
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                cwd = cwd or obj.get("cwd")
                session_id = session_id or obj.get("sessionId")
                if cwd and session_id:
                    break
            if not cwd and not session_id:
                n_skip += 1  # パース可能な行が無い
                continue
            eid = event_id("transcript", jl, mtime)
            spool({
                "device": DEVICE,
                "kind": "transcript",
                "agent": "claude-code",
                "event_id": eid,
                "session_id": session_id or jl.stem,
                "project_dir": cwd,
                "git_remote_url": git_remote(cwd) if cwd and Path(cwd).is_dir() else None,
                "git_branch": None,
                "transcript": text,
                "client_version": None,
                "captured_at": iso(time.time()),
                "backfill": True,
            }, f"{eid}.json")
            n_t += 1

        # --- auto memory
        for md in proj_dir.glob("memory/**/*.md"):
            try:
                mtime = md.stat().st_mtime
                content = md.read_text(errors="replace")
            except Exception:
                n_skip += 1
                continue
            eid = event_id("auto_memory", md, mtime)
            spool({
                "device": DEVICE,
                "kind": "auto_memory",
                "agent": "claude-code",
                "event_id": eid,
                "project_dir": proj_dir.name,
                "git_remote_url": None,
                "file_path": str(md),
                "content": content,
                "file_mtime": iso(mtime),
                "captured_at": iso(time.time()),
                "backfill": True,
            }, f"{eid}.json")
            n_m += 1

    print(f"backfill spooled: transcripts={n_t} memories={n_m} skipped={n_skip}")
    print("送信はsenderが行います(次のSessionStart、または hooks/sender.py を手動実行)")


main()
PYEOF
