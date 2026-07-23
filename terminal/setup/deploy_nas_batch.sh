#!/bin/bash
# NASバッチスクリプトの配置(NAS上で実行)
# リポジトリの nas/batch/ を /volume2/claude-system/batch/ にコピーして実行権限を付ける
set -eu

REPO_DIR="${1:-$HOME/nas-llm-memory}"
SRC="$REPO_DIR/nas/batch"
DEST="/volume2/claude-system/batch"

mkdir -p "$DEST"
cp "$SRC"/nightly.sh "$SRC"/nightly.py "$SRC"/backup.sh "$SRC"/purge.py "$SRC"/compact.py "$SRC"/edges_backfill.py "$SRC"/skill_scout.py "$DEST/"
# crontab.txt の NAS_USER プレースホルダーは実行ユーザーで展開して配置する
sed "s|NAS_USER|$(id -un)|g" "$SRC/crontab.txt" > "$DEST/crontab.txt"
chmod +x "$DEST"/nightly.sh "$DEST"/backup.sh
echo "deployed to $DEST"
