#!/bin/sh
# 動く python3 を探す共通処理(hook・setup・backfill から source する)
#
# 存在確認(command -v)だけでは不十分: macOSの /usr/bin/python3 は
# CommandLineTools が壊れていると存在しても xcrun エラーで落ちる。
# 実際にMac miniでこれが起き、収集と設定同期が2週間止まった。
# 置き場所も端末ごとに違う(Apple Silicon=/opt/homebrew, Intel=/usr/local,
# WSL/NAS=/usr/bin)ので、候補を順に --version で試して通ったものを使う。
#
# 使い方:
#   . "$CONFIG_DIR/hooks/find_python.sh"
#   PY=$(find_python) || { 動かない場合の処理; }

find_python() {
    for _c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 /usr/bin/python3; do
        if command -v "$_c" >/dev/null 2>&1 && "$_c" --version >/dev/null 2>&1; then
            command -v "$_c"
            return 0
        fi
    done
    return 1
}
