-- 重要な会話のフラグ(session_id 単位。turns 本体は変更しない)。
-- 実体は dashboard の運用開始時にNAS上で手動作成済みのため、本ファイルは
-- そのDDLの正規化(冪等)。列は現物と同一で、追加・変更はしない。
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -v ON_ERROR_STOP=1 -f - < 016_flags.sql

BEGIN;

CREATE TABLE IF NOT EXISTS flags (
  session_id text PRIMARY KEY,
  note       text NOT NULL DEFAULT '',
  created_by text NOT NULL,   -- 'dashboard-YYYYMMDD'(UI) | 'session-<device>-YYYYMMDD'(ingest /flag)
  created_at timestamptz NOT NULL DEFAULT now()
);

-- 008適用済み環境では検索ロールにも読ませる(フラグ付き会話の絞り込み用)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reader') THEN
    GRANT SELECT ON public.flags TO reader;
  END IF;
END $$;

COMMIT;
