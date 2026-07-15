#!/usr/bin/env python3
"""SessionEnd/Stop hook — セッションをローカルスプールに書き出す(設計書§3.1)

ネットワークアクセスは一切しない。ローカル追記のみ。絶対に失敗させない。
stdin: Claude Code のhook JSON (session_id, transcript_path, cwd, ...)
"""
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

SPOOL = Path.home() / ".claude-spool"
PENDING = SPOOL / "pending"


def git_info(args, cwd):
    try:
        r = subprocess.run(
            ["git", "-C", cwd] + args,
            capture_output=True, text=True, timeout=5,
        )
        out = r.stdout.strip()
        return out if r.returncode == 0 and out else None
    except Exception:
        return None


def iso(ts=None):
    t = time.localtime(ts) if ts is not None else time.localtime()
    s = time.strftime("%Y-%m-%dT%H:%M:%S%z", t)
    return s[:-2] + ":" + s[-2:]  # +0900 → +09:00


def spool(payload, name):
    PENDING.mkdir(parents=True, exist_ok=True)
    tmp = PENDING / (name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.rename(PENDING / name)  # senderが途中のファイルを送らないようatomicに置く


def main():
    # 夜間バッチ等の内部claude呼び出しは収集しない(自己増殖ループ防止)
    if os.environ.get("CLAUDE_SPOOL_SKIP") == "1":
        return
    data = json.load(sys.stdin)
    cwd = data.get("cwd") or os.getcwd()
    session_id = data.get("session_id") or "unknown"
    device = socket.gethostname()
    captured_at = iso()

    # --- transcript本体
    transcript = ""
    tp = data.get("transcript_path")
    if tp and Path(tp).exists():
        transcript = Path(tp).read_text(errors="replace")
    if transcript:
        # ファイル名は端末生成のevent_idのみで構成する:
        # 外部入力(session_id)や同秒実行で宛先が衝突してatomic renameが上書きするのを防ぎ、
        # ingest側の再送重複排除キーも兼ねる
        event_id = uuid.uuid4().hex
        spool({
            "device": device,
            "kind": "transcript",
            "event_id": event_id,
            "session_id": session_id,
            "project_dir": cwd,
            "git_remote_url": git_info(["remote", "get-url", "origin"], cwd),
            "git_branch": git_info(["rev-parse", "--abbrev-ref", "HEAD"], cwd),
            "transcript": transcript,
            "client_version": data.get("version"),
            "captured_at": captured_at,
        }, f"transcript-{event_id}.json")

    # --- auto memory: 前回スキャン以降に更新されたmarkdown(設計書§3.1)
    state = SPOOL / "last_memory_scan"
    last = state.stat().st_mtime if state.exists() else 0
    scan_start = time.time()  # スキャン中に更新されたファイルを次回対象に残す基準
    failed_mtimes = []
    projects_root = Path.home() / ".claude" / "projects"
    if projects_root.exists():
        for md in projects_root.glob("*/memory/**/*.md"):
            try:
                mtime = md.stat().st_mtime
            except Exception:
                failed_mtimes.append(last)  # mtime不明: watermarkを進めず次回リトライ
                continue
            if mtime <= last:
                continue
            try:
                # project_keyはmungedディレクトリ名から(端末間で安定)
                munged = md.relative_to(projects_root).parts[0]
                event_id = uuid.uuid4().hex
                spool({
                    "device": device,
                    "kind": "auto_memory",
                    "event_id": event_id,
                    "project_dir": munged,
                    "git_remote_url": None,
                    "file_path": str(md),
                    "content": md.read_text(errors="replace"),
                    "file_mtime": iso(mtime),
                    "captured_at": captured_at,
                }, f"mem-{event_id}.json")
            except Exception:
                failed_mtimes.append(mtime)
    # watermarkは「スキャン開始時刻」と「失敗した最古のファイルの手前」の小さい方:
    # スキャン中に更新されたファイルも失敗分も、次回必ず再スキャンされる
    # (再送で生じる重複はDB側のUNIQUE(device, file_path, file_mtime)が吸収する)
    t = scan_start
    if failed_mtimes:
        t = min(t, max(min(failed_mtimes) - 1, 0))
    state.touch()
    os.utime(state, (t, t))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # hookは絶対に失敗させない
        try:
            SPOOL.mkdir(parents=True, exist_ok=True)
            with open(SPOOL / "hook_errors.log", "a") as f:
                f.write(f"{time.strftime('%F %T')} spool_write: {exc}\n")
        except Exception:
            pass
    sys.exit(0)
