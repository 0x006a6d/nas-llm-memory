#!/bin/bash
# 夜間統合バッチ(設計書§6)— NASホストのcronから日次実行
# 実体は nightly.py(VERIFY→ORGANIZE→ENRICH→push)。cron向けにPATHを整えるだけのラッパー。
set -u

export HOME="${HOME:-/volume1/homes/NAS_USER}"
export PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/current/bin:/usr/bin:/bin"
# nvmのnodeを解決(claudeのshimが必要とする)
if [ -d "$HOME/.nvm/versions/node" ]; then
    NODE_BIN=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)
    [ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
fi

exec python3 "$HOME/claude-config/batch/nightly.py"
