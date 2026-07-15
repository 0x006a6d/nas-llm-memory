#!/bin/bash
# NASバッチスクリプトの配置(NAS上で実行)
# リポジトリの nas/batch/ を /volume2/claude-system/batch/ にコピーして実行権限を付ける
set -eu

REPO_DIR="${1:-$HOME/nas-llm-memory}"
SRC="$REPO_DIR/nas/batch"
DEST="/volume2/claude-system/batch"

cp "$SRC"/nightly.sh "$SRC"/nightly.py "$SRC"/backup.sh "$SRC"/crontab.txt "$DEST/"
chmod +x "$DEST"/nightly.sh "$DEST"/backup.sh
echo "deployed to $DEST"
