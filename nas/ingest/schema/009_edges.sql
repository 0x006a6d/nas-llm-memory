-- 事実間の補足関係。factsの系譜列(replaces/retired_by)とは独立で、
-- 「両方有効なまま関連し合う」組を持つ。検索時は方向を区別せず両側からたどる
-- type='extends': 補足関係あり
-- type='none'   : 判定済みで補足関係なし(edges_backfill.pyの再判定防止。検索では使わない)
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -f - < 009_edges.sql

BEGIN;

CREATE TABLE IF NOT EXISTS fact_edges (
  from_id    bigint NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  to_id      bigint NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  type       text   NOT NULL CHECK (type IN ('extends', 'none')),
  created_by text   NOT NULL,
  PRIMARY KEY (from_id, to_id, type),
  CHECK (from_id <> to_id)
);
CREATE INDEX IF NOT EXISTS fact_edges_to ON fact_edges (to_id);
-- 1ペア1判定(向き・typeによらず)。extendsとnoneの併存や逆向きの重複を排除する
CREATE UNIQUE INDEX IF NOT EXISTS fact_edges_pair
  ON fact_edges (LEAST(from_id, to_id), GREATEST(from_id, to_id));

-- 008適用済み環境では検索ロールにも読ませる
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reader') THEN
    GRANT SELECT ON public.fact_edges TO reader;
  END IF;
END $$;

COMMIT;
