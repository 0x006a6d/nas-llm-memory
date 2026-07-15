-- raw_payloads の再送重複排除: 端末生成の event_id に一意制約
-- (senderはat-least-once。成功応答を取りこぼした再送で同じ全文が蓄積されるのを防ぐ)
-- NULL は event_id を持たない旧クライアントとの互換用(一意インデックスはNULL同士を衝突させない)
-- 列追加と一意性は分離する: ADD COLUMN IF NOT EXISTS は列が既存だと制約ごとスキップされる
ALTER TABLE raw_payloads ADD COLUMN IF NOT EXISTS event_id text;
CREATE UNIQUE INDEX IF NOT EXISTS raw_payloads_event_id_key ON raw_payloads (event_id);
