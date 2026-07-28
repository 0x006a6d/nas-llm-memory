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
タブはこの順に並ぶ。概要タブ冒頭の全体図が各段の実数と入口になる。

- 概要         — パイプライン全体図(各段の実数とタブへのリンク)、毎セッション注入される
                 コンテキストの内訳(64KiB バジェットに対する使用率ドーナツ)、
                 turns/facts 件数、hook の重複登録などの自動検出
- 収集         — sync-exclude.txt の編集、crontab、バッチ実行履歴、リポジトリ状態
                 (収集段の管理と健全性確認)
- 記録 (facts) — current_facts の閲覧と 追加/修正/撤去、turns の PGroonga 全文検索、
                 重要な会話のフラグ付け(NAS flags テーブル、session_id 単位、全端末共通)、
                 auto memory スナップショット(各端末の内蔵メモリ取り込み履歴)の閲覧
- 書架 (決裁)  — 起案・決裁ワークフロー(ringi)の完結文書の閲覧と後閲。文書番号・決裁欄・
                 伺い文・回議録・登載 facts を表示し、後閲印またはメモ付き差し戻し
                 (差し戻しは翌晩の便で決裁者が再審理)。skill 文書は後閲印が施行の条件。
                 専決規程(NAS batch/config.json の roles / ringi 主要フラグ)の編集も
                 このタブ(保存時に .bak 退避、反映は翌晩のバッチから)
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
- コンテキスト — CLAUDE.md と memory/*/index.md の閲覧・編集(バイトゲージ付き)。
                 一覧は「毎セッション注入 / この端末のプロジェクト / 他端末」に
                 グループ分け。Codex 側配布(~/.codex/AGENTS.md 管理セクションと
                 各登録プロジェクトの AGENTS.override.md)の生成状態表も出す
- 配布         — routing.json の端末×プロジェクト マトリクス編集(どの端末にどの
                 プロジェクトの index を注入するか)
- 申し送り     — 端末/プロジェクト宛の一度きりのメッセージ送信と履歴
- 使用量       — Claude Code のテレメトリ集計(コスト/トークン/セッション/日別・端末別・
                 モデル別)。各端末が OTLP で NAS の otel-collector に送り、NAS の
                 Prometheus(9090)に保存されたものを HTTP API で集計する。ホストは
                 ~/.claude-spool/config.json の ingest_url から導出(IP はコードに置かない)

## 編集の意味論(重要)

- index.md は夜間バッチ(03:00)が current_facts から**全再生成**する。直接編集は即効だが
  翌バッチで上書きされる。恒久的な調整は「記憶 (facts)」タブで行うこと。
- facts の操作は nightly の規約に合わせている:
  - 追加 = INSERT(replaces=NULL, created_by=dashboard-日付)
  - 修正 = 新 fact を INSERT し replaces=旧id(置換連鎖)
  - 撤去 = retired_by=自id の自己参照 tombstone(view から外れる。nightly に retire
    経路が無いための表現)
- ファイル保存(index.md / sync-exclude.txt)は書き込み前に同名 `.bak` へ退避する。
- 編集できるファイルは server.py の `resolve_save_target()` のホワイトリストのみ。
- 会話フラグは NAS の `flags` テーブル(session_id 主キー)に保存し、turns 本体は変更しない。
  テーブルは `create table if not exists` で作成済み。夜間バッチへの重み付け連携は未実装
  (次段階。バッチは NAS 側リポジトリのため)。
