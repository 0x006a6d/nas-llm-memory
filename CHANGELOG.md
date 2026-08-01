# Changelog

このシステムの変更履歴。バージョンは 0.x 系で、1.0.0 は運用が納得できる形に
なったときのために取っておく。挙動・スキーマ・規程が変わる変更は MINOR、
修正のみは PATCH。dashboard など claude-config 側の変更もここに書く(一冊主義)。
v0.0.1 以前の経緯は git log と docs/ を参照。

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
