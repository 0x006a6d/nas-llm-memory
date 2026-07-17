-- Codex CLI 対応(追補設計書§1): 発生元エージェント列
-- 値: 'claude-code' | 'codex'。facts はエージェント非依存のため変更しない
-- (出所は provenance → turns.agent で辿れる)
ALTER TABLE raw_payloads ADD COLUMN IF NOT EXISTS agent text NOT NULL DEFAULT 'claude-code';
ALTER TABLE turns        ADD COLUMN IF NOT EXISTS agent text NOT NULL DEFAULT 'claude-code';
