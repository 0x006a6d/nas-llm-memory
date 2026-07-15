#!/bin/sh
# SessionStart hook — 設定同期 + sender起動(設計書§3.2, §3.3)
# すべてバックグラウンド・失敗無視。セッション開始をブロックしない。
( git -C "$HOME/claude-config" pull --ff-only >/dev/null 2>&1 & ) >/dev/null 2>&1
( nohup python3 "$HOME/claude-config/hooks/sender.py" >/dev/null 2>&1 & ) >/dev/null 2>&1
exit 0
