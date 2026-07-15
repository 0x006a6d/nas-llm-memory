#!/bin/sh
# SessionStart hook — 設定同期 + sender起動(設計書§3.2, §3.3)
# 単一のバックグラウンドワーカーに直列化: pull完了後にsenderを起動する。
# flockがある環境では多重SessionStartのpull競合も排他(sender自体は自前ロック持ち)。
# 失敗してもセッション開始はブロックしない。
CONFIG_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd) || exit 0
(
    if command -v flock >/dev/null 2>&1; then
        exec 9>"${TMPDIR:-/tmp}/claude-config-sync.lock"
        flock -n 9 || exit 0  # 別のSessionStartが同期中
    fi
    if command -v timeout >/dev/null 2>&1; then
        timeout 60 git -C "$CONFIG_DIR" pull --ff-only >/dev/null 2>&1
    else
        git -C "$CONFIG_DIR" pull --ff-only >/dev/null 2>&1
    fi
    python3 "$CONFIG_DIR/hooks/sender.py" >/dev/null 2>&1
) >/dev/null 2>&1 &
exit 0
