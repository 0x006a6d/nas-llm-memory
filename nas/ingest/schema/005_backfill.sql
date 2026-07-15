-- 初回データ移行(追補設計書§2): backfill-distill の進捗管理
-- 定常バッチのwatermarkとは独立に、過去分をプロジェクト×月チャンクで蒸留した位置を持つ
CREATE TABLE IF NOT EXISTS backfill_progress (
  project_key  text PRIMARY KEY,
  done_through timestamptz,               -- この時刻(月境界)より前のtsは蒸留済み。NULL=未着手
  completed    boolean NOT NULL DEFAULT false
);
