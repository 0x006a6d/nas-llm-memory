-- 起案・回議・決裁(公文書ワークフロー)の書架。
-- 夜間バッチの判断(facts登載・index改定・skill登載)を起案文書として起票し、
-- 審査(課長専決)・決裁(部長)・施行・人間の後閲を回議録付きで記録する。
-- 状態遷移の定義は batch/ringi.py の TRANSITIONS が正(SQLは値の妥当性のみ検査)。
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -f - < 012_ringi.sql

BEGIN;

-- 起案文書。facts同様append-only志向だが、ワークフロー進行のため
-- state/seen_state系列のUPDATEは許す(retired_by・redactedに続く例外)
CREATE TABLE IF NOT EXISTS drafts (
  id             bigserial PRIMARY KEY,
  fiscal_year    int  NOT NULL,              -- 年度(4/1区切り)
  seq            int  NOT NULL,              -- 年度内連番(失敗runの補償削除で欠番が出るのは許容)
  doc_no         text NOT NULL UNIQUE,       -- 機械形式 '2026-0012'。表示形式(記憶第N号)はUI側で組む
  kind           text NOT NULL CHECK (kind IN ('fact','skill','index','saishinri')),
  project_key    text NOT NULL,
  title          text NOT NULL,              -- 件名
  proposal       text NOT NULL,              -- 伺い文+別記(テンプレート生成。回議録はdraft_logが正)
  payload        jsonb NOT NULL,             -- 機械可読の案件本体(候補配列・判定・diff・skill名等)
  state          text NOT NULL DEFAULT 'pending_review' CHECK (state IN
                   ('pending_review','remanded_to_drafter','pending_decision',
                    'remanded_to_reviewer','approved','executed','rejected','reexamine')),
  decision_class text CHECK (decision_class IN ('senketsu','bucho')),  -- 課長専決/部長決裁
  decided_at     timestamptz,
  executed_at    timestamptz,
  seen_state     text NOT NULL DEFAULT 'pending' CHECK (seen_state IN ('pending','seen','remanded')),
  seen_at        timestamptz,                -- 後閲(pending=後閲待ち/seen=後閲済み/remanded=差し戻し)
  -- 再審理(saishinri)文書→原文書。原文書が消える経路(失敗run補償)では参照をNULLへ
  related_doc    bigint REFERENCES drafts(id) ON DELETE SET NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  created_by     text NOT NULL,              -- 'run-<id>'(失敗補償の削除キー)
  UNIQUE (fiscal_year, seq)
);
-- 書架の後閲待ち一覧(dashboard)と差し戻しキュー(nightly冒頭)の走査用
CREATE INDEX IF NOT EXISTS drafts_seen_pending ON drafts (created_at) WHERE seen_state = 'pending';
CREATE INDEX IF NOT EXISTS drafts_queue ON drafts (id)
  WHERE state IN ('reexamine', 'approved');

-- 回議録(処理履歴。差し戻しメモを含む。INSERTのみ、UPDATEしない)
CREATE TABLE IF NOT EXISTS draft_log (
  id         bigserial PRIMARY KEY,
  draft_id   bigint NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
  actor      text NOT NULL,   -- 'kian:<model>' | 'shinsa:<model>' | 'kessai:<model>' | 'human'
  action     text NOT NULL CHECK (action IN
               ('kian','hosei','shinsa_ok','joshin','sashimodoshi',
                'kessai_ok','hiketsu','shiko','kouetsu','saishinri','skill_mv')),
  memo       text,            -- 差し戻しメモ・審査意見
  payload    jsonb,           -- LLM応答の生JSON(監査用)
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL
);
CREATE INDEX IF NOT EXISTS draft_log_draft ON draft_log (draft_id);

-- 施行された文書と登載factsの相互紐付け(facts本体はALTERしない)。
-- 失敗runの補償は drafts を先に消せば本表はCASCADEで消える(facts削除より先に行うこと)
CREATE TABLE IF NOT EXISTS draft_facts (
  draft_id bigint NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
  fact_id  bigint NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  PRIMARY KEY (draft_id, fact_id)
);
CREATE INDEX IF NOT EXISTS draft_facts_fact ON draft_facts (fact_id);

-- 008適用済み環境では検索ロールにも読ませる(書架の閲覧用)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reader') THEN
    GRANT SELECT ON public.drafts, public.draft_log, public.draft_facts TO reader;
  END IF;
END $$;

COMMIT;
