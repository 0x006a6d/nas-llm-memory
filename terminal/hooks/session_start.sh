#!/bin/sh
# SessionStart hook — 設定同期 + sender起動(設計書§3.2, §3.3)
# すべてバックグラウンド・失敗無視。セッション開始をブロックしない。
# 配置パスは自身の位置から導出(setup.shと同じ規約: hooksの親が配布リポジトリ)
CONFIG_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd) || exit 0
( git -C "$CONFIG_DIR" pull --ff-only >/dev/null 2>&1 & ) >/dev/null 2>&1
( nohup python3 "$CONFIG_DIR/hooks/sender.py" >/dev/null 2>&1 & ) >/dev/null 2>&1
exit 0
