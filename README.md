# nas-llm-memory

家庭内 NAS を中継点にして、複数端末の LLM エージェントのセッション記録を集約し、夜間バッチで「事実」に蒸留して全端末に配り直す記憶同期システム。

端末 A で学んだことが、翌朝には端末 B のセッションに載っている、を受け入れ基準として構築した。

## 構成

```text
[各端末 (Mac / WSL2)]
  SessionEnd hook → ~/.claude-spool/pending/ に JSON 書き出し (ネットワーク不使用)
  sender (SessionStart hook + 1時間おき cron/launchd) → NAS へ POST
  SessionStart hook → ~/claude-config を git pull --ff-only

[NAS]
  ingest API   : FastAPI (Docker), 自己署名 TLS + Bearer 認証, 受信時に秘密情報を正規表現マスク
  PostgreSQL   : 17 + PGroonga (Docker)
  設定リポジトリ : claude-config.git (bare)。skills / hooks / memory index を全端末に配布
  夜間バッチ    : cron → nightly.py (VERIFY → ORGANIZE → ENRICH → git push)
  バックアップ  : cron → pg_dump (直近14日 + 月次保持)
```

## ディレクトリ

- `terminal/` — 端末側。hooks (spool_write.py / sender.py / session_start.sh)、setup.sh、settings.json のテンプレート
- `nas/` — NAS 側。ingest API (FastAPI + スキーマ SQL)、docker-compose、夜間バッチ、バックアップ、crontab

## データの流れ

1. Claude Code のセッションが終わると SessionEnd hook がトランスクリプトをローカルスプールに書く。ここではネットワークに触らないので、NAS が落ちていてもセッションは正常終了する
2. sender がスプールの未送信分を ingest API に POST する。at-least-once 送達で、重複は DB 側の `UNIQUE(session_id, message_uuid)` が吸収する
3. ingest は正規表現 (`ingest/redact_patterns.json`) で API キーや秘密鍵をマスクしてから保存する
4. 夜間バッチが `claude -p` で生ログから事実を抽出し、検証を通ったものを facts 層に入れ、プロジェクト別の memory index (Markdown) を生成して設定リポジトリに push する
5. 各端末は次のセッション開始時に git pull で index を受け取る。Claude Code の CLAUDE.md から `@~/claude-config/memory/<key>/index.md` で注入する

## DB スキーマ (claude_memory)

- `raw_payloads` — 受信生データ (マスク済み)。パース失敗時の保険
- `turns` — 生ログ層。append-only
- `auto_memory_snapshots` — Claude Code の auto memory ファイルのスナップショット
- `facts` — 事実層。UPDATE せず `replaces` で系譜管理し、`current_facts` ビューが現在有効な事実を返す
- `batch_runs` — バッチ実行記録 + watermark

## 運用して踏んだ罠と対策 (実装済み)

1. 自己増殖ループ。バッチ自身の `claude -p` セッションが SessionEnd hook で収集されてしまう。バッチは `CLAUDE_SPOOL_SKIP=1` を付けて claude を起動し、spool_write.py の冒頭でスキップする
2. 捏造の事実化。ツール無効の `claude -p` はツール実行結果をでっち上げることがある。VERIFY プロンプトで「assistant の主張は `[tool_result]` の裏付けが無い限り verified=false」を強制する
3. UGOS (UGREEN NAS) のファイル転送制限。rsync/scp/sftp がラッパーでパス制限されるため、転送は `tar | ssh` か `ssh 'cat > file'` で行う

## セットアップ概要

NAS 側: `nas/` を配置し、`ingest/secrets/` に `api_token` と `db_password` を置き (600)、`ingest/gen_tls_cert.sh <NASのIP>` で TLS 証明書を生成、`nas/.env` に `INGEST_BIND_IP=<NASのLAN IP>` を書いてから `docker compose up -d`。スキーマは番号順に適用する (`002_pgroonga.sql` は PGroonga で全文検索する場合のみ):

```bash
cd nas
for f in ingest/schema/001_init.sql ingest/schema/003_p2.sql ingest/schema/004_event_id.sql; do
  docker compose exec -T db psql -U claude -d claude_memory -v ON_ERROR_STOP=1 -f - < "$f"
done
```

cron は `nas/batch/crontab.txt` を参考に登録する (配置は `terminal/setup/deploy_nas_batch.sh`)。

端末側:

```bash
git clone ssh://NAS_USER@NAS_IP/volume2/claude-system/repos/claude-config.git ~/claude-config
NAS_IP=<NASのIP> ~/claude-config/setup/setup.sh
```

setup.sh は冪等で、skills の symlink、settings.json への hooks マージ、スプール設定、sender の定期実行登録 (macOS は launchd、Linux は cron)、ユーザーレベル CLAUDE.md への index @import を行う。

## 前提

- 端末: git、python3 (3.9+)、Claude Code。hooks は POSIX 前提 (Windows は WSL2 で使う)
- NAS: Docker が動く Linux NAS。実環境は UGREEN (UGOS) だが依存はない。NAS 上でも Claude Code CLI を認証済みにしておく (夜間バッチが使う)
- 秘密情報 (API トークン、DB パスワード) はこのリポジトリには含まれない。各自が secrets ファイルとして配置する
