#!/bin/sh
# SessionStart hook — 設定同期 + sender起動(設計書§3.2, §3.3)
# 実体はsync_worker.py(pull→senderの直列化・flock排他・timeoutを持つ)。
# ここではバックグラウンド起動だけしてセッション開始をブロックしない。
CONFIG_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd) || exit 0

# 動くpython3を探す(hookはGUI/launchd由来の最小PATHで呼ばれる。詳細は find_python.sh)
. "$CONFIG_DIR/hooks/find_python.sh" 2>/dev/null || exit 0
PY=$(find_python) || {
    # python3が1つも動かない: 静かに諦めず痕跡を残す(次のセッションで気づけるように)
    mkdir -p "$HOME/.claude-spool" 2>/dev/null
    printf '%s no working python3 (PATH=%s)\n' "$(date -u +%Y-%m-%dT%H:%M:%S)" "$PATH" \
        >> "$HOME/.claude-spool/sync_worker.log" 2>/dev/null
    exit 0
}

( "$PY" "$CONFIG_DIR/hooks/sync_worker.py" "$CONFIG_DIR" >/dev/null 2>&1 & ) >/dev/null 2>&1
# 申し送り(messages)の未読をコンテキストへ注入する。ここだけインライン実行:
# 注入はセッション開始時にしかできない。NAS不達時はcurl --max-timeで数秒内に素通し
"$PY" "$CONFIG_DIR/hooks/inbox_check.py" 2>/dev/null
exit 0
