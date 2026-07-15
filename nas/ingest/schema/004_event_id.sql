-- raw_payloads の再送重複排除: 端末生成の event_id に一意制約
-- (senderはat-least-once。成功応答を取りこぼした再送で同じ全文が蓄積されるのを防ぐ)
-- NULL は event_id を持たない旧クライアントとの互換用(UNIQUEはNULL同士を衝突させない)
ALTER TABLE raw_payloads ADD COLUMN IF NOT EXISTS event_id text UNIQUE;
