-- 文書の整理と管理簿(公文書管理法5条〜8条の実装)。
-- 収集した記録に分類・保存期間・満了日・満了時の措置(移管/廃棄)を与え、
-- 「行政文書ファイル」= (大分類, 中分類=project_key, 年度) の集合物として管理簿に載せる。
-- 個々の行に保存期間列は持たせない(法5条の「一の集合物にまとめる」に合わせ、
-- 廃棄・移管も集合物単位で行う)。
-- 適用: docker compose exec -T db psql -U claude -d claude_memory -v ON_ERROR_STOP=1 -f - < 015_kanribo.sql

BEGIN;

-- 標準文書保存期間基準(別表相当)。大分類ごとに保存期間・満了時の措置・施行ゲートを定める。
-- retention_days > 0 は日起算(受信日から)、retention_years > 0 は会計年度起算
-- (満了日 = 作成年度の年度末 + 保存年数)。両方0は「常用」(満了しない)。
CREATE TABLE IF NOT EXISTS retention_rules (
  category      text PRIMARY KEY,        -- 大分類
  source_table  text NOT NULL,           -- 実体のあるテーブル
  ts_column     text NOT NULL,           -- 年度・満了日の起算に使う時刻列
  retention_days  int NOT NULL DEFAULT 0,
  retention_years int NOT NULL DEFAULT 0,
  measure       text NOT NULL CHECK (measure IN ('ikan','haiki','jouyou')),  -- 移管/廃棄/常用
  gate          text NOT NULL CHECK (gate IN ('sokujiko','kouetsu')),        -- 決裁即施行/後閲印必須
  enabled       boolean NOT NULL DEFAULT false,  -- 段階適用: falseの分類は整理も廃棄もしない
  note          text
);

-- 行政文書ファイル管理簿。1行 = 1つの集合物(小分類)。
-- 件数とid範囲は整理(seiri)のたびに更新し、廃棄・移管の対象範囲になる。
-- 集合物の単位(period)は保存期間の起算に合わせる:
--   年度起算(retention_years) → period='2026'    (年度。満了 = 翌年度初めからN年)
--   日起算(retention_days)    → period='2026-07' (月。満了 = 月末からN日)
-- 日起算を年度でまとめると、その年度に行が入り続ける限り満了しないため月で締める
CREATE TABLE IF NOT EXISTS record_files (
  id           bigserial PRIMARY KEY,
  category     text NOT NULL REFERENCES retention_rules(category),  -- 大分類
  project_key  text NOT NULL,            -- 中分類(横断は 'general')
  name         text NOT NULL,            -- 小分類 = ファイル名称
  period       text NOT NULL,            -- 集合物の単位('2026' or '2026-07')
  fiscal_year  int  NOT NULL,            -- 年度(4/1区切り)
  expires_on   date,                     -- 保存期間の満了する日(常用はNULL)
  measure      text NOT NULL CHECK (measure IN ('ikan','haiki','jouyou')),  -- レコードスケジュール
  location     text NOT NULL,            -- 保存場所(DB表名 / 配布リポジトリ / archive パス)
  state        text NOT NULL DEFAULT 'genyou'
               CHECK (state IN ('genyou','manryou','haiki_zumi','ikan_zumi')),
  n_rows       bigint NOT NULL DEFAULT 0,
  id_from      bigint,                   -- 対象行の id 範囲(廃棄・移管の実行範囲)
  id_to        bigint,
  first_ts     timestamptz,
  last_ts      timestamptz,
  disposed_draft bigint REFERENCES drafts(id),  -- 廃棄・移管を決裁した文書
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (category, project_key, period)
);
CREATE INDEX IF NOT EXISTS record_files_expiry ON record_files (expires_on)
  WHERE state = 'genyou';

-- 廃棄した受信生データの証跡。本文だけ廃棄して行は残す
-- (turns.payload_id の FK が NO ACTION のため行削除できない。監査証跡としても残す)
ALTER TABLE raw_payloads ADD COLUMN IF NOT EXISTS disposed_at timestamptz;
ALTER TABLE raw_payloads ADD COLUMN IF NOT EXISTS disposed_draft bigint REFERENCES drafts(id);

-- 廃棄・移管・点検も起案文書として通す
ALTER TABLE drafts DROP CONSTRAINT IF EXISTS drafts_kind_check;
ALTER TABLE drafts ADD CONSTRAINT drafts_kind_check
  CHECK (kind IN ('fact','skill','index','saishinri','haiki','ikan','tenken'));

-- 標準文書保存期間基準の初期値。段階適用のため既定は enabled=false
-- (運用で1分類ずつ true にする。手順は docs/bunsho-kanri.md)
INSERT INTO retention_rules
  (category, source_table, ts_column, retention_days, retention_years, measure, gate, note)
VALUES
  ('shuju-raw',   'raw_payloads',          'received_at', 90, 0, 'haiki', 'sokujiko',
   '収受した生データ。turnsへパース済みで、90日は再パースの保険'),
  ('shuju-turns', 'turns',                 'ts',           0, 3, 'haiki', 'kouetsu',
   '生ログ。現用factsの根拠(provenance)になっている行は廃棄対象から外す'),
  ('shuju-memo',  'auto_memory_snapshots', 'received_at',  0, 1, 'haiki', 'kouetsu',
   '各端末の内蔵メモリ取り込み履歴'),
  ('kiroku-fact', 'facts',                 'created_at',   0, 0, 'jouyou', 'kouetsu',
   '事実層。常用(満了しない)。退役分の扱いは別途'),
  ('kessai-doc',  'drafts',                'created_at',   0, 10, 'ikan', 'kouetsu',
   '起案・決裁文書。10年で移管(廃棄しない)'),
  ('renraku-msg', 'messages',              'created_at',   0, 1, 'haiki', 'kouetsu',
   '端末間の事務引継'),
  ('unyou-run',   'batch_runs',            'started_at',   0, 3, 'haiki', 'sokujiko',
   'バッチ実行記録')
ON CONFLICT (category) DO NOTHING;

-- 検索ロール(008)にも読ませる
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reader') THEN
    GRANT SELECT ON retention_rules, record_files TO reader;
  END IF;
END $$;

COMMIT;
