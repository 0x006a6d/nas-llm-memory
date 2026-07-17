-- Codex追補§2.3: session_metaのoriginator(どの入口で使ったか)。
-- 実測値: codex-tui / codex_exec / Codex Desktop / Claude Code。claude-code行はNULL。
-- 用途: 入口別の集計(追補§4)と、Claude Code経由Codexセッションの二重計上の識別(追補§7。
-- 収集はスキップせず、集計側でoriginatorを見て区別する方針)
ALTER TABLE turns ADD COLUMN IF NOT EXISTS originator text;
