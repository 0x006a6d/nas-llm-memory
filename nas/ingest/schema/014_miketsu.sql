-- 未決繰越(決裁が付かなかった上申案件を、承認も否決もせず翌晩へ回す)。
-- - draft_log.action の CHECK に 'kurikoshi'(繰越)を追加
-- - 未決文書の走査用に部分インデックス(pending_decision の fact 文書)
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -v ON_ERROR_STOP=1 -f - < 014_miketsu.sql

BEGIN;

ALTER TABLE draft_log DROP CONSTRAINT IF EXISTS draft_log_action_check;
ALTER TABLE draft_log ADD CONSTRAINT draft_log_action_check
  CHECK (action IN ('kian','hosei','shinsa_ok','joshin','sashimodoshi',
                    'kessai_ok','hiketsu','shiko','kouetsu','saishinri',
                    'skill_mv','kurikoshi'));

CREATE INDEX IF NOT EXISTS drafts_miketsu ON drafts (id)
  WHERE state = 'pending_decision';

COMMIT;
