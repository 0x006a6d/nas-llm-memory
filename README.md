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

- `terminal/` — 端末側。hooks (spool_write.py / sender.py / session_start.sh)、setup.sh、settings.json のテンプレート、sync-exclude.txt (収集除外リスト)
- `nas/` — NAS 側。ingest API (FastAPI + スキーマ SQL)、docker-compose、夜間バッチ、バックアップ、purge、crontab

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
- `facts` — 事実層。UPDATE せず `replaces` で系譜管理し、`current_facts` ビューが現在有効な事実を返す。類似 fact は週次の `batch/compact.py` が統合する (統合 fact が新しい側を `replaces`、古い側に `retired_by` を刻む。削除なしで系譜は双方向に追える)
- `batch_runs` — バッチ実行記録 + watermark
- `drafts` / `draft_log` / `draft_facts` — 起案・決裁ワークフローの書庫 (起案文書・回議録・登載 facts の紐付け。下記参照)

## 起案・決裁ワークフロー (公文書方式)

夜間バッチの判断を自治体の公文書の起案・回議・決裁になぞらえて構成できる。`batch/config.json` の `ringi.enabled` で切り替える (falsy なら従来パイプラインのまま)。前提スキーマは `012_ringi.sql` と `002_pgroonga.sql` の両方で、いずれかが未適用なら `enabled` を立てても従来経路で動く (バッチが WARN を出す)。

- 専決規程 — 役割ごとに担当モデルを分掌する。`roles.kian` = 起案 (turns から事実候補を調べ上げる係員)、`roles.shinsa` = 審査 (既存 facts と照合する課長。軽易案件はここで専決)、`roles.kessai` = 決裁 (部長。既存 fact の置換・撤回・矛盾疑いの上申案件のみ)、`roles.enrich` = index 生成。上申するかどうかはコード側の機械判定で、モデルの裁量にしない
- 起案文書 — facts 登載・index 改定・skill 登載の判断を 1 件ずつ伺い文 (「下記のとおり登載してよろしいか」+ 別記) 付きの文書として起票し、年度別連番の文書番号を採番して `drafts` に保存する。処理履歴は回議録 (`draft_log`) に残る
- 差し戻し — 全階層共通で「メモを付与して前の担当者に戻す」。審査→起案者 (内容不備の補正指示、上限往復数超過で廃案)、決裁→審査 (再判定)、人間の後閲→決裁者 (翌晩の便で再審理し、是正の saishinri 文書を起票する)
- 未決繰越 — 決裁者の応答が形式不一致で決裁が付かない案件は、承認 (未レビューの置換が通る) にも否決 (watermark が進むので候補が二度と起票されない) にも倒さず、**未決**の文書として `pending_decision` のまま残す。まず 1 件ずつ問い直し、それでも決まらなければ翌晩の便が payload (候補本文と審査判定) から再審理する。決まった案件から順に施行し、残りは繰越を重ねて `ringi.max_miketsu_nights` (既定 3 晩) を超えたら廃案にする
- 後閲 — バッチは無人で施行まで進み (代決)、人間は dashboard の書庫タブで事後確認する。後閲印または差し戻し。skill の施行 (skills/ 本体への登載 = 全端末配布) だけは人間の後閲印を施行条件とする (`ringi.skill_auto_execute` で解除可)
- 試行 (第 1 期) — `ringi.trial` または `nightly.py --trial` で、起案候補モデル (`ringi.trial_models`) を本番 run の内側で並行実行し、審査モデルの突合で拾い漏れ率・誤拾い率を `batch/trial/summary.md` に実測する。facts には入れない。数晩の実測で `roles.kian` を決めてから `enabled` を入れる想定。所要時間は `ringi.trial_budget_min` (既定 25 分) で打ち止めし、04:00 チェーンの欠測を防ぐ (見送り・切り落とし分は突合表に記録される)

適用は 2 段階: コードを deploy しても `enabled` が立つまで挙動は変わらないので、先に配置して従来動作を確認してから config を切り替える。専決規程 (roles と主要フラグ) は dashboard の書庫タブからも変更できる (翌晩から反映)。

## 整理と管理簿 (公文書管理法5〜7条)

収集した記録は、分類・名称・保存期間・保存期間の満了する日・満了時の措置を与えて「行政文書ファイル」(集合物) にまとめ、管理簿に載せる (整理)。措置は満了より前に決めておく (レコードスケジュール)。

- 集合物の単位 — `(大分類, 中分類 = project_key, 期間)`。個々の行に保存期間を持たせない (法5条の「一の集合物にまとめる」に合わせ、廃棄・移管も集合物単位で行う)
- 期間の切り方 — 保存期間が年単位なら**年度** (4/1 区切り)、日単位なら**月**。日単位を年度でまとめると、その年度に行が入り続ける限り満了しないため月で締める
- 満了する日 — 年度起算は「作成年度の翌年度初めから N 年」= (年度+1+N)/3/31、日起算は「その月の末日から N 日」
- 標準文書保存期間基準 — `retention_rules` テーブルが規程の正 (大分類 → 保存期間・満了時の措置・施行ゲート)。`enabled` で 1 分類ずつ適用する。初期値は raw_payloads 90日 / turns 3年 / auto_memory 1年 / facts 常用 / drafts 10年で移管 / messages 1年 / batch_runs 3年
- 管理簿 — `record_files`。dashboard の「管理簿」タブが分類・名称・保存期間・満了する日・措置・件数・保存場所・状態を表示する (閲覧のみ。操作は夜間バッチ)
- どのテーブルをどう束ねるかは `batch/kanribo.py` の `SOURCES` が正で、規程 (015 の初期値) との一致は `tests/test_kanribo.py` が検査する

整理は夜間バッチの run 冒頭で行う (`seiri()`)。既存のパイプラインには触れず、失敗しても本体へ波及させない。

満了したファイルは措置に従って処理する。廃棄は必ず起案文書 (廃棄伺い) を通し、審査・決裁を経てから施行する。現用 facts の根拠になっている turns、watermark を持つ最新の成功 run、raw_payloads の受信証跡は保存期間が満了しても残す。施行の条件 (決裁で施行 / 人間の後閲印が条件) は分類ごとに規程で定める。運用の全体は `docs/bunsho-kanri.md` (文書管理規程) にまとめてある。

## 運用して踏んだ罠と対策 (実装済み)

1. 自己増殖ループ。バッチ自身の `claude -p` セッションが SessionEnd hook で収集されてしまう。バッチは `CLAUDE_SPOOL_SKIP=1` を付けて claude を起動し、spool_write.py の冒頭でスキップする
2. 捏造の事実化。ツール無効の `claude -p` はツール実行結果をでっち上げることがある。VERIFY プロンプトで「assistant の主張は `[tool_result]` の裏付けが無い限り verified=false」を強制する
3. UGOS (UGREEN NAS) のファイル転送制限。rsync/scp/sftp がラッパーでパス制限されるため、転送は `tar | ssh` か `ssh 'cat > file'` で行う

## セットアップ概要

NAS 側: `nas/` を配置し、`ingest/secrets/` に `api_token` と `db_password` を置き (600)、`ingest/gen_tls_cert.sh <NASのIP>` で TLS 証明書を生成、`nas/.env` に `INGEST_BIND_IP=<NASのLAN IP>` と `INGEST_UID=<secrets の所有 uid (通常 id -u)>` を書いてから `docker compose up -d`。スキーマは番号順に適用する (`002_pgroonga.sql` は PGroonga で全文検索する場合のみ):

```bash
cd nas
for f in ingest/schema/001_init.sql ingest/schema/003_p2.sql \
         ingest/schema/004_event_id.sql ingest/schema/005_backfill.sql \
         ingest/schema/006_agent.sql ingest/schema/007_originator.sql \
         ingest/schema/008_reader.sql ingest/schema/009_edges.sql \
         ingest/schema/010_messages.sql ingest/schema/011_retired.sql \
         ingest/schema/012_ringi.sql ingest/schema/013_ringi_fixup.sql \
         ingest/schema/014_miketsu.sql ingest/schema/015_kanribo.sql; do
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

## 収集除外 (オプトアウト)

同期・収集したくないプロジェクトは claude-config リポジトリ直下の `sync-exclude.txt` に書く (1 回の編集で全端末に配布される)。書式は project_key の完全一致か、`~/private/**` のようなパス glob。三重に効く:

1. 端末側 — hook / sender (Codex rollout 走査含む) / backfill がスプールに書かない (データが端末の外に出ない)
2. NAS 側 — ingest API も同リストを読み、該当 POST を保存せず捨てる (古い端末や設定ミスからの漏れ止め)。docker-compose が claude-config clone を読み取り専用マウントする (`nas/.env` の `CLAUDE_CONFIG_DIR`)
3. 配布側 — turns が無いため index も生成されない

一時的にセッション単位で止めるには `NAS_MEMORY_DISABLE=1` を立てて起動する (Claude Code の hook 経路のみ。Codex は hook を使わず sender の走査で収集されるため対象外 — Codex を止めるには sync-exclude.txt に書く)。収集済みデータの事後除外は NAS 上で `python3 /volume2/claude-system/batch/purge.py --project <key>` (件数を表示して確認後、turns / raw_payloads / auto_memory_snapshots / facts / 配布済み index を削除し、purge.log に記録する)。

## Codex CLI 対応

OpenAI Codex CLI のセッションも同じ経路に載る (`docs` の追補設計):

- 収集 — sender が `~/.codex/sessions/**/rollout-*.jsonl` を走査し、未送信分をスプールに包んで送る (hook 不要。mtime 5 分未満の書きかけは次回に回す)。rollout は append-only なので**増分送信**: 送信済み行数を `~/.claude-spool/codex-sent.jsonl` に記録し、新しい完全行だけを `line_offset` 付き・8MB チャンクで送る。常駐セッションの巨大 rollout (実測 100MB 級) を成長のたびに全量再送しない
- 蒸留 — ingest が rollout を `agent='codex'` の turns に正規化 (role は user/assistant/tool へ写像、reasoning は暗号化のため対象外、ID は行番号から決定的に生成 = 再送で重複しない)。夜間バッチはエージェントを問わず同じ facts 層に蒸留する
- 配布 — sender が general index を `~/.codex/AGENTS.md` のマーカー区切り管理セクション (`nas-memory:begin/end`) に展開する。手書き本文には触れない
- 制約 — プロジェクト単位の注入は未実装。codex 0.144.1 は `<project>/.codex/AGENTS.md` を読まず (実機検証)、commit 対象の AGENTS.md 本体へ index を展開すると記憶がリポジトリに漏れるため

## opencode 対応

opencode (kimi 等を動かす CLI) のセッションも同じ経路に載る。opencode は JSONL ではなく SQLite に持つので、収集はファイル走査ではなく DB の読み取りになる。

- 収集 — sender が `~/.local/share/opencode/opencode.db` (`XDG_DATA_HOME` 尊重、`OPENCODE_DB` で上書き可) を読み取り専用で開き、`session` / `message` / `part` の3表から message 1 件 = JSONL 1 行に組み直して送る。**増分送信**: セッションごとに送信済みの最大 `message.time_created` を `~/.claude-spool/opencode-sent.jsonl` に記録し、それより新しい message だけを送る。書きかけの応答を取り込まないよう、最終更新が 5 分以内の message は次回に回す
- 蒸留 — ingest が `agent='opencode'` の turns に正規化する。`message_uuid` は opencode の message.id (DB 全体で一意) をそのまま使うので再送しても重複しない。part のうち text / tool (呼び出しと結果) / subtask を本文に落とし、reasoning は Claude Code の thinking と同じく保存しない。`originator` にはエージェント種別 (build / plan 等) が入る
- 除外 — セッションの作業ディレクトリで sync-exclude.txt を適用する (Codex と同じく hook 経路ではないので `NAS_MEMORY_DISABLE` は効かない)
- 配置順 — ingest を先に更新すること。未対応の ingest は `agent='opencode'` を 400 で拒否し、送信ループは最初の失敗で打ち切るためスプールが滞留する

新しいツールを収集対象に加えるときは、この 2 か所 (sender の走査 + ingest のパーサ) を足す。保存形式がツールごとに違うため共通化はしていない。

## 初回データ移行 (バックフィル)

定常経路は「今後発生するログ」のみを扱う。既存の過去ログは稼働開始の最初期に一度だけ流し込む (Claude Code のローカル保持期間で古いセッションから消えるため、遅らせない)。詳細は `docs/backfill.md`。

1. 各端末で `terminal/setup/backfill-claude.sh` を実行 (過去トランスクリプトと auto memory をスプールへ。送信は sender 任せ、再実行無害)
2. 送信が済んだら NAS で `nightly.py --init-watermark` を一度実行 (過去分を定常バッチの対象外にする)
3. 過去分の蒸留は `nightly.py --backfill-distill 2` を夜間に回す (プロジェクト×月チャンク、アクティブ優先、既存事実と矛盾する過去の事実は常に負ける)。`nas/batch/crontab.txt` のコメント行を有効化し、全プロジェクト完了で外す
4. **後から端末を追加した場合**: 新端末で backfill-claude.sh → sender 送信後、NAS で `nightly.py --extend-watermark` を実行する (watermark-init を現時点まで進め、投入分を distill 経路へ回す。device 別内訳を表示して確認、backfill 完了済みプロジェクトの turns が混ざる場合は安全側に中止する)

## 前提

- 端末: git、python3 (3.9+)、Claude Code。hooks は POSIX 前提 (Windows は WSL2 で使う)
- NAS: Docker が動く Linux NAS。実環境は UGREEN (UGOS) だが依存はない。NAS 上でも Claude Code CLI を認証済みにしておく (夜間バッチが使う)
- 秘密情報 (API トークン、DB パスワード) はこのリポジトリには含まれない。各自が secrets ファイルとして配置する
