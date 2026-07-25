#!/bin/bash
# 夜間バッチ共通ラッパー — cronの素のPATH(/usr/bin:/bin)にはclaude(nvmのnode配下)が無く、
# python3を直接起動したバッチはask_claudeでFileNotFoundErrorになる(edges.logで実証)。
# nightly.shと同じPATH組み立てでbatch/配下のスクリプトを起動する。
# usage: run_batch.sh <スクリプト名(拡張子なし)> [引数...]
set -u

export HOME="${HOME:-/volume1/homes/NAS_USER}"
export PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/current/bin:/usr/bin:/bin"
# nvmのnodeを解決(claudeのshimが必要とする)
if [ -d "$HOME/.nvm/versions/node" ]; then
    NODE_BIN=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)
    [ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd) || exit 1
NAME="$1"
shift
exec python3 "$SCRIPT_DIR/$NAME.py" "$@"
