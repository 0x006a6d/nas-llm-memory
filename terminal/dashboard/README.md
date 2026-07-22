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

## タブ構成

- 概要         — 毎セッション注入されるコンテキストの内訳(64KiB バジェットに対する
                 使用率ドーナツ)、turns/facts 件数、hook の重複登録などの自動検出
- コンテキスト — CLAUDE.md と memory/*/index.md の閲覧・編集(バイトゲージ付き)
- 記憶 (facts) — current_facts の閲覧と 追加/修正/撤去、turns の PGroonga 全文検索、
                 auto memory スナップショット(各端末の内蔵メモリ取り込み履歴)の閲覧
- スキル       — ~/.claude/skills + プラグイン由来スキルの一覧
- Hooks        — hooks-manifest(claude-config/hooks/hooks-manifest.json)の宣言的管理:
                 対象 CLI(Claude/Codex)のチェックと「保存して適用」で両設定へ展開
                 (実処理は hooks/hooks_apply.py。SessionStart でも自動適用)。
                 加えて settings.json・各プラグイン・~/.codex/hooks.json の実登録を
                 イベント別に集約表示。コンテキスト注入 hook は本文を琥珀枠で表示
- 収集設定     — sync-exclude.txt の編集、crontab、バッチ実行履歴、リポジトリ状態

## 編集の意味論(重要)

- index.md は夜間バッチ(03:30)が current_facts から**全再生成**する。直接編集は即効だが
  翌バッチで上書きされる。恒久的な調整は「記憶 (facts)」タブで行うこと。
- facts の操作は nightly の規約に合わせている:
  - 追加 = INSERT(replaces=NULL, created_by=dashboard-日付)
  - 修正 = 新 fact を INSERT し replaces=旧id(置換連鎖)
  - 撤去 = retired_by=自id の自己参照 tombstone(view から外れる。nightly に retire
    経路が無いための表現)
- ファイル保存(index.md / sync-exclude.txt)は書き込み前に同名 `.bak` へ退避する。
- 編集できるファイルは server.py の `resolve_save_target()` のホワイトリストのみ。
