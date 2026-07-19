-- compaction(ORGANIZE二段照合化 追補§4): 統合で退役したfactの記録
-- 統合fact M はペアの新しい側を replaces で置き換え、古い側には retired_by=M を刻む。
-- 削除せず全系譜を保持する(replaces と retired_by の双方向で統合履歴を追える)
ALTER TABLE facts ADD COLUMN IF NOT EXISTS retired_by bigint REFERENCES facts(id);

-- 「現在有効な事実」= replaces で置き換えられておらず、compaction でも退役していない行
CREATE OR REPLACE VIEW current_facts AS
SELECT f.* FROM facts f
WHERE f.retired_by IS NULL
  AND NOT EXISTS (SELECT 1 FROM facts g WHERE g.replaces = f.id);
