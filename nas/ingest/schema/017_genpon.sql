-- 原本保管(改ざん防止) — 文書管理規程 docs/bunsho-kanri.md 第10章
-- 決裁完了文書の本文をsha256で固定し(sealed_sha)、以後の変更・削除をDBが拒否する。
-- 回議録(draft_log)は追記のみ(訂正は追記で行う)。
-- 例外は保存期間満了の廃棄・移管のみで、施行経路がGUC `bunsho.disposal = on` を
-- 立てて通す(nightlyのexecute_haiki_doc/execute_ikan_docだけが立てる)。
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -v ON_ERROR_STOP=1 -f - < 017_genpon.sql

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE drafts ADD COLUMN IF NOT EXISTS sealed_sha text;

-- 封緘値の定義はこの1式(ringi.transition_sqlの決裁時UPDATEと、点検の再計算照合が同じ式を使う):
--   sha256(doc_no || '\n' || title || '\n' || proposal || '\n' || payload::text)
-- payload::text はjsonbの正規形(キー順・空白が正規化される)なので再計算が安定する。

-- 遡及封緘: 既に決裁済みの文書はこの時点の本文を原本と定める
UPDATE drafts
   SET sealed_sha = encode(digest(doc_no || E'\n' || title || E'\n' || proposal
                                  || E'\n' || payload::text, 'sha256'), 'hex')
 WHERE decided_at IS NOT NULL AND sealed_sha IS NULL;

-- 決裁済み文書の本文・決裁情報の変更と削除を拒否する。
-- 許すのは状態進行列(state, seen_state, seen_at, executed_at)のみ。
-- related_doc は参照先文書の移管(FKのON DELETE SET NULL)でだけ変わりうるため、
-- 廃棄・移管セッション(bunsho.disposal=on)に限って許す。
CREATE OR REPLACE FUNCTION drafts_genpon_guard() RETURNS trigger AS $$
DECLARE
  -- GUC未設定時にcurrent_settingはNULLを返す。三値論理でガードが素通りしないよう
  -- coalesceで必ずbooleanに落とす
  disposal boolean := coalesce(current_setting('bunsho.disposal', true), '') = 'on';
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.decided_at IS NOT NULL AND NOT disposal THEN
      RAISE EXCEPTION '原本保管: 決裁済み文書(%)は削除できない(移管はbunsho.disposal経由のみ)',
        OLD.doc_no;
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.decided_at IS NOT NULL THEN
    IF NEW.doc_no        IS DISTINCT FROM OLD.doc_no
       OR NEW.fiscal_year IS DISTINCT FROM OLD.fiscal_year
       OR NEW.seq         IS DISTINCT FROM OLD.seq
       OR NEW.kind        IS DISTINCT FROM OLD.kind
       OR NEW.project_key IS DISTINCT FROM OLD.project_key
       OR NEW.title       IS DISTINCT FROM OLD.title
       OR NEW.proposal    IS DISTINCT FROM OLD.proposal
       OR NEW.payload     IS DISTINCT FROM OLD.payload
       OR NEW.created_at  IS DISTINCT FROM OLD.created_at
       OR NEW.created_by  IS DISTINCT FROM OLD.created_by
       OR NEW.decided_at  IS DISTINCT FROM OLD.decided_at
       OR NEW.decision_class IS DISTINCT FROM OLD.decision_class
       OR (OLD.sealed_sha IS NOT NULL AND NEW.sealed_sha IS DISTINCT FROM OLD.sealed_sha)
       OR (NEW.related_doc IS DISTINCT FROM OLD.related_doc AND NOT disposal)
    THEN
      RAISE EXCEPTION '原本保管: 決裁済み文書(%)の本文・決裁情報は変更できない', OLD.doc_no;
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS drafts_genpon_guard_tg ON drafts;
CREATE TRIGGER drafts_genpon_guard_tg
  BEFORE UPDATE OR DELETE ON drafts
  FOR EACH ROW EXECUTE FUNCTION drafts_genpon_guard();

-- 回議録は追記のみ。削除は文書本体の廃棄・移管への追従(FK CASCADE)だけを許す
CREATE OR REPLACE FUNCTION draft_log_append_only() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND coalesce(current_setting('bunsho.disposal', true), '') = 'on' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION '回議録: 追記のみ(訂正は追記で行う。廃棄はbunsho.disposal経由のみ)';
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS draft_log_append_only_tg ON draft_log;
CREATE TRIGGER draft_log_append_only_tg
  BEFORE UPDATE OR DELETE ON draft_log
  FOR EACH ROW EXECUTE FUNCTION draft_log_append_only();

COMMIT;
