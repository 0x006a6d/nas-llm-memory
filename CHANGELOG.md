# Changelog

このシステムの変更履歴。バージョンは 0.x 系で、1.0.0 は運用が納得できる形に
なったときのために取っておく。挙動・スキーマ・規程が変わる変更は MINOR、
修正のみは PATCH。dashboard など claude-config 側の変更もここに書く(一冊主義)。
v0.0.1 以前の経緯は git log と docs/ を参照。

## [v0.3.2] - 2026-08-20

### claude-config: SOPスキル5本を新母艦(RTX 5090機)の実態に更新

母艦移行(EVO-X2 ROCm→RTX 5090 CUDA)に伴い、comfyui-launch・ds4-launch・
vid2vid-pose-transfer・model-backup-nas・wezterm-tmuxのROCm前提の記述
(HSA環境変数、SSD streaming必須、DWPoseのtorchscript回避、設定パス等)を
新環境で検証した値に書き換えた。未実測の箇所(ds4のCUDA初回ビルド・起動)と
移行で消えたローカルパッチ・補助スクリプトはその旨を明記した。

## [v0.3.1] - 2026-08-18

### claude-config: dotfiles-maintenanceスキルのGitHubアカウント名を修正

スキルのdescriptionと本文が旧アカウント名Flowers-of-Romanceのままだったため、
リネーム後の0x006a6dに直した。GCMの資格情報キーとremote URLのusernameは
旧名のままで動くため、そちらは変更していない。

### テストのSQLリテラルをSQLite 3.45系でも通る書き方に直す

test_opencode.pyがSQL文字列に数値リテラル9_999_000を直書きしており、
アンダースコア区切りに対応しないSQLite 3.46未満ではunrecognized tokenで
落ちていた。区切りを外した。Python側のリテラルとバインドパラメータは変更なし。

## [v0.3.0] - 2026-08-05

### skill改善提案(improve)を起案・決裁ワークフローへ

夜間バッチが kind=improve のスキル候補を改定伺いとして起票するようにした。
審査(課長)が現行SKILL.mdへ改善断片を溶かし込んだ改定後全文を作って上申し、
人間の決裁後、翌晩の施行が封緘済みの全文で skills/ 側を上書きして候補を削除する。
現行本文が15,000字を超える候補は全文を審査に渡せないため起票しない。

### dashboard: 後閲表示の整合と閲覧手段の追加

回議録に後閲の記帳が無い文書(人間決裁等)を「後閲済」でなく「後閲不要」と表示し、
原本の決裁欄と矛盾しないようにした。スキルタブは候補一覧に起票状態
(文書番号・状態)列を、スキル・コマンド・エージェント一覧に実体ファイルの
インライン表示(開く)を追加。収受簿は数値列の整列を直し、処理状況フィルタと
エラー内容の展開表示を追加。逓送タブは夜間便リプレイを先頭に移した。

## [v0.2.0] - 2026-08-05

### ingest自己修復watchdogの追加

NAS再起動時にeth0のアドレス取得よりdocker起動が先行すると、ingestの
LAN IP(INGEST_BIND_IP)へのbindが失敗したままexitedで取り残される(restartポリシーは
起動失敗をリトライしない)。この取り残しを回復するwatchdogを追加した。
cron 5分毎に/healthをピン止め証明書で確認し、不通ならforce-recreateする。
bind先アドレスが未割当の間は待機し、compose操作には実行時間の上限を設けた。
配置はdeploy_nas_batch.sh(claude-config側)の配布リストに追加した。

## [v0.1.0] - 2026-08-05

### dashboard: タブ再編 — 決裁・後閲と逓送の分離

書庫タブに同居していた決裁操作・路線図・夜間便リプレイを分離した。決裁・後閲タブは
人間の要処理(決裁待ち・未決/差し戻し・再審理中/後閲待ち/決裁済・施行待ちの4キュー)と
専決規程、逓送タブは夜間便の記録(crontab・実行履歴・便リプレイ)、書庫は完結文書の
閲覧専用とした。監理は接続設定・hook・配布リポジトリの確認に純化し、収受除外
(sync-exclude)は収受簿へ移した。あわせて shelf のフィルタを整理し、未決に審査中を
含め、終端廃案は完結文書として書庫に収めた。全状態×後閲状態の組がいずれかの一覧
から到達できることを実測で確認した。経緯は docs/bunsho-kanri-sekkei.md §7。

## [v0.0.3] - 2026-08-05

### バッチ失敗時の原因表示

claude CLI が非ゼロ終了したとき、stderr が空でも stdout の envelope から
result 本文(使用量上限の通知等)と subtype を例外メッセージに含めるようにした。
batch_runs の notes と dashboard の注意欄に失敗原因がそのまま残る。

## [v0.0.2] - 2026-08-02

### ライセンス

WTFPL を設定 (LICENSE 新設、README に明記)。

## [v0.0.1] - 2026-08-02

変更履歴の起点。この時点で稼働しているもの:

- 収受 — 各端末の hook/sender → ingest (HTTPS + Bearer、正規表現マスク、収集除外、
  event_id 冪等) → raw_payloads / turns / auto_memory_snapshots。
  Claude Code / Codex / opencode の transcript に対応
- 蒸留 — 夜間バッチが VERIFY (事実候補の抽出) → ORGANIZE (二段照合) → ENRICH →
  index.md 生成・全端末配布。claude 不調への耐性 (応答内 JSON の問い直し、
  プロジェクト単位の隔離と partial 記録)
- 検索 — PGroonga 全文 + 関連展開 (fact_edges / provenance / 類似)。
  読み取り専用ロール reader
- 稟議 — 起案・審査・決裁・施行・後閲の状態機械と回議録 (drafts / draft_log)。
  skill 登載と生ログ廃棄は人間の決裁事項 (schema 019)。人間の決裁者が差し戻した
  上申文書は審査が再検討する (補正して再上申または廃案)
- 文書管理 — 保存期間基準・管理簿・満了検出・廃棄/移管 (文書管理規程 docs/bunsho-kanri.md)。
  原本封緘 (sealed_sha) と改変禁止トリガ (017)、借覧簿 (閲覧・検索・貸出・供覧, 018)、
  公文書館の利用請求
- 端末間 — 申し送り (messages / inbox)、skills・hooks・記憶 index の git 配布
- dashboard (claude-config 側) — 書庫 (決裁・後閲・夜間便リプレイ)・管理簿・
  収受簿・スキル・監理ほか
