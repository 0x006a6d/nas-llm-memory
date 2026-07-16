#!/bin/sh
# SessionStart hook — 設定同期 + sender起動(設計書§3.2, §3.3)
# 実体はsync_worker.py(pull→senderの直列化・flock排他・timeoutを持つ)。
# ここではバックグラウンド起動だけしてセッション開始をブロックしない。
CONFIG_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd) || exit 0
( python3 "$CONFIG_DIR/hooks/sync_worker.py" "$CONFIG_DIR" >/dev/null 2>&1 & ) >/dev/null 2>&1
exit 0
