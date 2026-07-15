-- P2: auto_memory_snapshots用のwatermarkを追加(設計書§6.3)
ALTER TABLE batch_runs ADD COLUMN IF NOT EXISTS watermark_snapshot_id bigint;
