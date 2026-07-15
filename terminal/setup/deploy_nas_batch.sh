#!/bin/bash
# NASバッチスクリプトの配置(NAS上で実行)
# リポジトリの batch/ を /volume2/claude-system/batch/ にコピーして実行権限を付ける
set -eu

REPO_DIR="${1:-$HOME/claude-config}"
DEST="/volume2/claude-system/batch"

cp "$REPO_DIR"/batch/nightly.sh "$REPO_DIR"/batch/backup.sh "$REPO_DIR"/batch/crontab.txt "$DEST/"
chmod +x "$DEST"/nightly.sh "$DEST"/backup.sh
echo "deployed to $DEST"
