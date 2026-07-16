-- §5.6 全文検索(PGroonga)。導入できない場合はこのファイルの適用を保留する(設計は不変)
CREATE EXTENSION IF NOT EXISTS pgroonga;
CREATE INDEX IF NOT EXISTS turns_content_pgroonga ON turns USING pgroonga (content);
CREATE INDEX IF NOT EXISTS facts_content_pgroonga ON facts USING pgroonga (content);
