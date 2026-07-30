-- 借覧簿(供覧・閲覧・貸出の台帳) — 文書管理規程 docs/bunsho-kanri.md 第11章
-- 「読む」の証跡: dashboardでの閲覧、端末からの検索、公文書館からの取り寄せ(貸出)、
-- 端末inboxが申し送りを表示した事実(供覧)を1行ずつ記帳する。
-- 借覧簿自体も文書として保存期間基準に載せる(1年・廃棄・決裁即施行)。
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -v ON_ERROR_STOP=1 -f - < 018_shakuran.sql

BEGIN;

CREATE TABLE IF NOT EXISTS lending_log (
  id          bigserial PRIMARY KEY,
  at          timestamptz NOT NULL DEFAULT now(),
  actor       text NOT NULL,     -- 'human' / 端末名(macbook-pro等) / 'system'
  channel     text NOT NULL CHECK (channel IN ('dashboard','search','archive','inbox')),
  action      text NOT NULL CHECK (action IN ('etsuran','kensaku','kashidashi',
                                              'henkyaku','kouran')),
  object_kind text NOT NULL,     -- 'draft' / 'turns' / 'facts' / 'record_file' / 'message'
  object_ref  jsonb NOT NULL,    -- 閲覧: {"draft_id":N} / 検索: {"query":"...","hit_ids":[...],"n":N}
                                 -- 貸出: {"file_id":N,"path":"..."} / 供覧: {"message_ids":[...]}
  memo        text
);
CREATE INDEX IF NOT EXISTS lending_log_at ON lending_log (at);

-- 端末の検索経路(readerロール)から記帳できるように。読み戻しは不要(最小権限)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reader') THEN
    GRANT INSERT ON lending_log TO reader;
    GRANT USAGE ON SEQUENCE lending_log_id_seq TO reader;
  END IF;
END $$;

-- 保存期間基準に借覧簿を追加(段階適用: 有効化は運用で行う)
INSERT INTO retention_rules
  (category, source_table, ts_column, retention_days, retention_years, measure, gate, note)
VALUES
  ('kanri-shakuran', 'lending_log', 'at', 0, 1, 'haiki', 'sokujiko',
   '借覧簿。閲覧・検索・貸出・供覧の台帳')
ON CONFLICT (category) DO NOTHING;

COMMIT;
