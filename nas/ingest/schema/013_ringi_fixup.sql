-- 012 の制約補強(012 は既に本番適用済みのため別マイグレーションで追随)。
-- - draft_log.action の CHECK に 'skill_mv' を追加(skill施行の中断再開判定の記帳)
-- - drafts.related_doc の FK を ON DELETE SET NULL に(失敗run補償で原文書が消える
--   経路が将来生じても、是正文書側の参照が残って制約違反にならないように)
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -v ON_ERROR_STOP=1 -f - < 013_ringi_fixup.sql

BEGIN;

ALTER TABLE draft_log DROP CONSTRAINT IF EXISTS draft_log_action_check;
ALTER TABLE draft_log ADD CONSTRAINT draft_log_action_check
  CHECK (action IN ('kian','hosei','shinsa_ok','joshin','sashimodoshi',
                    'kessai_ok','hiketsu','shiko','kouetsu','saishinri','skill_mv'));

ALTER TABLE drafts DROP CONSTRAINT IF EXISTS drafts_related_doc_fkey;
ALTER TABLE drafts ADD CONSTRAINT drafts_related_doc_fkey
  FOREIGN KEY (related_doc) REFERENCES drafts(id) ON DELETE SET NULL;

COMMIT;
