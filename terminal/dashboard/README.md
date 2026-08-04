# dashboard — 記憶統合システムのローカルビューア+エディタ

ブラウザから「Claude のコンテキストに何が注入されているか」を確認し、調整するためのツール。

実行時の配置は配布リポジトリ側の `~/claude-config/dashboard/`(公開リポジトリでは
`terminal/dashboard/` がその正本。配布リポジトリへコピーして使う)。以下のパス表記は
配布後の `~/claude-config/` 配置を前提とする。

## 起動

```
python3 ~/claude-config/dashboard/server.py            # 通常
python3 ~/claude-config/dashboard/server.py --demo     # NASに接続せず demo/ のダミーデータで動く(公開リポ向け)
python3 ~/claude-config/dashboard/server.py --port N   # ポート変更(既定 8810)
```

→ http://127.0.0.1:8810 (127.0.0.1 バインドのみ。外部依存なし、python3 標準ライブラリのみ)

NAS への問い合わせは `ssh nas`(~/.ssh/config)経由。NAS 系データは `.cache/nas.json` に
キャッシュされ、画面右上の「NAS更新」で再取得する。

## タブ構成(パイプライン順)

システムの本質は「収集 → 蒸留 → index 生成 → 配布・注入」のパイプラインで、
タブはおおむねこの順に並ぶ。文書事務概況タブ冒頭の全体図が各段の実数と入口になる。

- 文書事務概況 — パイプライン全体図(各段の実数とタブへのリンク)、毎セッション注入される
                 コンテキストの内訳(64KiB バジェットに対する使用率ドーナツ)、
                 turns/facts 件数、hook の重複登録などの自動検出
- 収受簿       — NAS が各端末から受け付けた記録(raw_payloads)の受付台帳(収受番号・
                 受付印・差出端末・種別・処理状況)と端末別の収受状況。収集除外
                 (sync-exclude.txt、全端末に配布)の編集もこのタブ
- 現用文書     — current_facts の閲覧と 追加/修正/撤去、turns の PGroonga 全文検索、
                 重要な会話のフラグ付け(NAS flags テーブル、session_id 単位、全端末共通)、
                 auto memory スナップショット(各端末の内蔵メモリ取り込み履歴)の閲覧
- 決裁・後閲   — 起案・決裁ワークフロー(ringi)のうち人間の要処理キュー。停留所マップ
                 (路線図。滞留件数クリックで一覧を絞り込む)、決裁待ち・未決(審査中も含む)/
                 差し戻し・再審理中/後閲待ち/決裁済・施行待ちの4フィルタで文書一覧、
                 原本モーダル(決裁・差戻・後閲印の操作)。skill 登載と生ログ(turns)廃棄は
                 人間決裁事項、それ以外は LLM が決裁・施行し人間は後閲(妥当なら後閲印、
                 問題があれば差し戻し)。LLM決裁直後のskill/haiki/ikan文書は後閲待ちと
                 決裁済・施行待ちの両方に出る(施行条件が後閲印のため。仕様どおりの重複)。
                 専決規程(NAS batch/config.json の roles / ringi 主要フラグ)の編集も
                 このタブ(保存時に .bak 退避、反映は翌晩のバッチから)
- 書庫         — 完結した文書(施行済で後閲も済んだもの、および承認前に差し戻された
                 終端廃案を含む)だけを載せる閲覧専用の書庫。種別フィルタと原本モーダル
                 (閲覧のみ。決裁欄・回議録を表示)。決裁待ち・審査中・差し戻し中(処理可能な
                 もの)・後閲待ち・決裁済(施行待ち)の文書はここには出ない(決裁・後閲タブへ)
- 管理簿       — 行政文書ファイル管理簿(record_files)の分類・名称・保存期間・満了する日・
                 措置・件数・保存場所・状態、標準文書保存期間基準(規程)の閲覧(閲覧のみ。
                 操作は夜間バッチと決裁・後閲タブ)。移管済みファイルは公文書館から取り寄せ可
- スキル       — /context に出る構成要素の一覧を出所別に表示:
                 スキル(user / claude-config / codex ~/.codex/skills / 各プロジェクト
                 .claude/skills / プラグイン / Codex プラグイン3層 ~/.codex/plugins/cache
                 の openai-bundled=内蔵・openai-primary-runtime=実行時ランタイム・
                 openai-curated-remote=リモート)、コマンド(commands/*.md)、
                 エージェント(agents/*.md)、Claude Code 内蔵(builtin-context.json の
                 手動スナップショット。バイナリ埋め込みで列挙不可のため、claude の
                 バージョンが変わると要更新の注意を出す)。各項目に編集可否を明示。
                 使用実績はエージェント同列: Claude 発動(~/.claude/projects の Skill
                 ツール呼び出し)と Codex 参照(~/.codex/sessions の SKILL.md 読み取り)
- Hooks        — hooks-manifest(claude-config/hooks/hooks-manifest.json)の宣言的管理:
                 対象 CLI(Claude/Codex)のチェックと「保存して適用」で両設定へ展開
                 (実処理は hooks/hooks_apply.py。SessionStart でも自動適用)。
                 加えて settings.json・各プラグイン・~/.codex/hooks.json の実登録を
                 イベント別に集約表示。コンテキスト注入 hook は本文を琥珀枠で表示
- 例規(常用文書)— CLAUDE.md と memory/*/index.md の閲覧・編集(バイトゲージ付き)。
                 一覧は「毎セッション注入 / この端末のプロジェクト / 他端末」に
                 グループ分け。Codex 側配布(~/.codex/AGENTS.md 管理セクションと
                 各登録プロジェクトの AGENTS.override.md)の生成状態表も出す
- 配付先       — routing.json の端末×プロジェクト マトリクス編集(どの端末にどの
                 プロジェクトの index を注入するか)
- 事務引継     — 端末/プロジェクト宛の一度きりのメッセージ送信と履歴
- 予算執行     — Claude Code のテレメトリ集計(コスト/トークン/セッション/日別・端末別・
                 モデル別)。各端末が OTLP で NAS の otel-collector に送り、NAS の
                 Prometheus(9090)に保存されたものを HTTP API で集計する。ホストは
                 ~/.claude-spool/config.json の ingest_url から導出(IP はコードに置かない)
- 逓送         — 夜間便(NAS 夜間バッチ)の発着記録。crontab の登録内容、バッチ実行履歴
                 (逓送簿。時刻は UTC)、夜間便リプレイ(1便の回議を文書が席から席へ動く
                 アニメーションとして再生)
- 監理         — 端末側の設定管理。この端末の実接続設定(~/.claude-spool。ingest_url・
                 トークン/証明書の fp・送信キュー・設定同期の状態)、端末側 hook スクリプト
                 一覧、claude-config リポジトリの最新コミットと作業ツリー状態

## 編集の意味論(重要)

- index.md は夜間バッチ(03:00)が current_facts から**全再生成**する。直接編集は即効だが
  翌バッチで上書きされる。恒久的な調整は「現用文書」タブで行うこと。
- facts の操作は nightly の規約に合わせている:
  - 追加 = INSERT(replaces=NULL, created_by=dashboard-日付)
  - 修正 = 新 fact を INSERT し replaces=旧id(置換連鎖)
  - 撤去 = retired_by=自id の自己参照 tombstone(view から外れる。nightly に retire
    経路が無いための表現)
- ファイル保存(index.md / sync-exclude.txt)は書き込み前に同名 `.bak` へ退避する。
- 編集できるファイルは server.py の `resolve_save_target()` のホワイトリストのみ。
- 会話フラグは NAS の `flags` テーブル(session_id 主キー)に保存し、turns 本体は変更しない。
  DDL は `nas/ingest/schema/016_flags.sql`。会話中からは ingest の `POST /flag`
  (nas-memory-flag スキル)でも付けられ、出所は created_by の接頭辞
  (`dashboard-` / `session-<device>-`)で区別する。夜間バッチへの重み付け連携は未実装
  (次段階。バッチは NAS 側リポジトリのため)。
