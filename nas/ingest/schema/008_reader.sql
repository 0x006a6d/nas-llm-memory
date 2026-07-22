-- 検索用の読み取り専用ロール。付与はSELECTのみで、書き込みは権限エラーで失敗する
-- 接続はコンテナ内local socket(trust)経由のみ。パスワード未設定のためTCP(scram)では接続できない
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -f - < 008_reader.sql

BEGIN;

-- readerが既に存在する場合は想定形(LOGIN可・特権なし・メンバーシップなし)であることを検証し、
-- 別用途のロールが紛れていたら適用を止める
DO $$
DECLARE
  r pg_roles%ROWTYPE;
BEGIN
  SELECT * INTO r FROM pg_roles WHERE rolname = 'reader';
  IF NOT FOUND THEN
    CREATE ROLE reader LOGIN;
  ELSIF NOT r.rolcanlogin
     OR r.rolsuper OR r.rolcreaterole OR r.rolcreatedb
     OR r.rolreplication OR r.rolbypassrls
     OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member = r.oid) THEN
    RAISE EXCEPTION 'role "reader" は既存で想定形と異なる(特権またはメンバーシップあり)。手動確認が必要';
  END IF;
END $$;

GRANT CONNECT ON DATABASE claude_memory TO reader;
GRANT USAGE ON SCHEMA public TO reader;
-- raw_payloadsは対象外(検索はturns/factsで足りる。最小権限)
-- 以後テーブルを追加しても自動では見えない。必要になったものだけ明示GRANTする
GRANT SELECT ON
  public.turns,
  public.facts,
  public.current_facts,
  public.auto_memory_snapshots,
  public.batch_runs
TO reader;

COMMIT;
