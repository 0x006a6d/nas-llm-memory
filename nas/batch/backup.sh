#!/bin/bash
# DBバックアップ(手順書§7)— NASホストのcronから日次実行
# 失敗時のみログに残す(成功時は無通知)
# pipefail必須: これが無いとpg_dump失敗の部分ダンプをgzip成功で上書きしてしまう
set -euo pipefail

SYSTEM_DIR="/volume2/claude-system"
BACKUP_DIR="/volume1/claude-backup"
LOG="$SYSTEM_DIR/batch/backup_error.log"

fail() {
    echo "$(date '+%F %T') backup FAILED: $*" >> "$LOG"
    exit 1
}

# 1. pg_dump → HDD
DUMP="$BACKUP_DIR/pgdump/claude_memory_$(date +%F).sql.gz"
cd "$SYSTEM_DIR" || fail "cd $SYSTEM_DIR"
docker compose exec -T db pg_dump -U claude claude_memory | gzip > "$DUMP" \
    || fail "pg_dump"
# 空ダンプ検知
[ "$(stat -c %s "$DUMP")" -gt 1000 ] || fail "dump too small: $DUMP"
gunzip -t "$DUMP" || fail "gunzip -t $DUMP"

# 2. bare repoのコピー
# 前回失敗の.tmp残骸があると cp -a がその配下へ入れ子コピーするため、必ず消してから
rm -rf "$BACKUP_DIR/repos/.claude-config.git.tmp" || fail "tmp cleanup"
cp -a "$SYSTEM_DIR/repos/claude-config.git" "$BACKUP_DIR/repos/.claude-config.git.tmp" \
    || fail "repo copy"
rm -rf "$BACKUP_DIR/repos/claude-config.git" || fail "old repo remove"
mv "$BACKUP_DIR/repos/.claude-config.git.tmp" "$BACKUP_DIR/repos/claude-config.git" \
    || fail "repo swap"

# 3. 世代整理: 直近14日 + 各月1日分を保持
find "$BACKUP_DIR/pgdump" -name 'claude_memory_*.sql.gz' -mtime +14 \
    ! -name 'claude_memory_????-??-01.sql.gz' -delete || fail "prune"

exit 0
