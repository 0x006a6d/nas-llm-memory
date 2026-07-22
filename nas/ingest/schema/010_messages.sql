-- 端末間の申し送り(単発・即時)。恒常知識はfacts/index、こちらは「次のセッションへの一言」
-- 宛先はNULL=不問(to_device/to_project両方NULLなら全セッション宛て)。
-- 受信(POST /inbox)時にread_atが打たれ、以後は注入されない
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -f - < 010_messages.sql

BEGIN;

CREATE TABLE IF NOT EXISTS messages (
  id          bigserial PRIMARY KEY,
  from_device text NOT NULL,
  to_device   text,
  to_project  text,
  body        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  read_at     timestamptz
);
CREATE INDEX IF NOT EXISTS messages_unread ON messages (created_at) WHERE read_at IS NULL;

-- 008適用済み環境では検索ロールにも読ませる(履歴の閲覧用)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reader') THEN
    GRANT SELECT ON public.messages TO reader;
  END IF;
END $$;

COMMIT;
