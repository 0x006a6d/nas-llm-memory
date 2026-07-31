-- 決裁と後閲の整理 — 文書管理規程 第4章・第6章の改定と対。
-- 人間の承認が施行の条件であるものは「後閲」ではなく「決裁」そのもの。
-- 後閲は、人間が関与せずに施行された文書の事後確認に限る。
--
-- - 施行ゲートの値を 'kouetsu'(後閲印が条件) から 'kessai'(人間の決裁が条件) に改める。
--   kessai の分類は審査(課長)の上申で止まり、人間が決裁して初めて施行される
-- - 人間決裁の対象を見直す: 生ログ(shuju-turns)の廃棄のみ
--   (一次記録の不可逆な消失)。取り込み済み履歴(shuju-memo)・使い捨て伝言
--   (renraku-msg)・可逆な移管(kessai-doc)はルーチンとして sokujiko
--   (LLM決裁で即施行、事後に後閲)
-- - drafts.decision_class に 'human'(人間決裁) を追加
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -v ON_ERROR_STOP=1 -f - < 019_kessai_gate.sql

BEGIN;

ALTER TABLE retention_rules DROP CONSTRAINT IF EXISTS retention_rules_gate_check;
UPDATE retention_rules SET gate = 'kessai'
 WHERE category IN ('shuju-turns', 'kiroku-fact');
UPDATE retention_rules SET gate = 'sokujiko'
 WHERE category IN ('shuju-memo', 'renraku-msg', 'kessai-doc');
ALTER TABLE retention_rules ADD CONSTRAINT retention_rules_gate_check
  CHECK (gate IN ('sokujiko', 'kessai'));

ALTER TABLE drafts DROP CONSTRAINT IF EXISTS drafts_decision_class_check;
ALTER TABLE drafts ADD CONSTRAINT drafts_decision_class_check
  CHECK (decision_class IN ('senketsu', 'bucho', 'human'));

COMMIT;
