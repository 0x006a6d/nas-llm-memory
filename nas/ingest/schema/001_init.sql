-- 分散Claude Code環境 記憶統合システム — 基本スキーマ(設計書§5)
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -f - < 001_init.sql

BEGIN;

-- §5.1 raw_payloads(生受信データ)
CREATE TABLE IF NOT EXISTS raw_payloads (
  id          bigserial PRIMARY KEY,
  device      text NOT NULL,
  kind        text NOT NULL,            -- 'transcript' | 'auto_memory'
  payload     jsonb NOT NULL,           -- 受信したまま(マスク適用後)
  received_at timestamptz NOT NULL DEFAULT now(),
  parsed_at   timestamptz,              -- パース成功時刻(NULL=未処理/失敗)
  parse_error text
);

-- §5.2 turns(生ログ層 — append-only)
CREATE TABLE IF NOT EXISTS turns (
  id            bigserial PRIMARY KEY,
  device        text NOT NULL,
  project_key   text NOT NULL,          -- gitリモートURL正規化 or ディレクトリ名
  session_id    text NOT NULL,
  message_uuid  text NOT NULL,
  role          text NOT NULL,          -- 'user' | 'assistant' | 'tool' 等
  content       text NOT NULL,          -- マスク適用後の本文
  ts            timestamptz,
  cwd           text,
  git_branch    text,
  model         text,
  payload_id    bigint REFERENCES raw_payloads(id),
  redacted      boolean NOT NULL DEFAULT false,
  UNIQUE (session_id, message_uuid)     -- 冪等ingestの要
);
CREATE INDEX IF NOT EXISTS turns_project_ts ON turns (project_key, ts);

-- §5.3 auto_memory_snapshots
CREATE TABLE IF NOT EXISTS auto_memory_snapshots (
  id          bigserial PRIMARY KEY,
  device      text NOT NULL,
  project_key text NOT NULL,
  file_path   text NOT NULL,
  content     text NOT NULL,
  file_mtime  timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (device, file_path, file_mtime)
);

-- §5.4 facts(事実層 — 検証済み知識)
CREATE TABLE IF NOT EXISTS facts (
  id           bigserial PRIMARY KEY,
  project_key  text NOT NULL,           -- 'general' はプロジェクト横断
  content      text NOT NULL,
  status       text NOT NULL,           -- 'verified' | 'unverified'
  provenance   bigint[] NOT NULL,       -- 根拠となる turns.id 群
  confidence   real,
  replaces     bigint REFERENCES facts(id),  -- 系譜。UPDATEはしない
  created_at   timestamptz NOT NULL DEFAULT now(),
  created_by   text NOT NULL            -- バッチrun ID
);
CREATE INDEX IF NOT EXISTS facts_project ON facts (project_key) WHERE replaces IS NULL;

-- 「現在有効な事実」= 他のfactのreplacesに参照されていない行
CREATE OR REPLACE VIEW current_facts AS
SELECT f.* FROM facts f
WHERE NOT EXISTS (SELECT 1 FROM facts g WHERE g.replaces = f.id);

-- §5.5 embeddings(任意・後回し可。独立テーブルに隔離)
CREATE TABLE IF NOT EXISTS embeddings (
  source_table text NOT NULL,           -- 'turns' | 'facts'
  source_id    bigint NOT NULL,
  model        text NOT NULL,
  vec          bytea NOT NULL,          -- pgvector導入時は vector 型に変更
  PRIMARY KEY (source_table, source_id, model)
);

-- §6.1-6 / §10 batch_runs(夜間バッチの実行記録・watermark)
CREATE TABLE IF NOT EXISTS batch_runs (
  id                 bigserial PRIMARY KEY,
  project_key        text,
  started_at         timestamptz NOT NULL DEFAULT now(),
  finished_at        timestamptz,
  status             text NOT NULL DEFAULT 'running',  -- 'running' | 'success' | 'failed'
  turns_processed    int,
  candidates_dropped int,
  index_lines        int,
  watermark_turn_id  bigint,             -- このrunが処理した最後のturns.id
  notes              text
);

COMMIT;
