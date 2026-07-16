# 追補設計書: 初回データ移行(バックフィル)

memory-system-design.md / codex-integration.md への追補。
定常経路は「今後発生するログ」のみを扱うため、稼働開始時に既存データを一度だけ流し込む。

実装対応:

| 設計 | 実装 |
|---|---|
| §1 バックフィルスクリプト | `terminal/setup/backfill-claude.sh` |
| §2 watermark初期化 | `nightly.py --init-watermark` |
| §2 backfill-distill | `nightly.py --backfill-distill N` + `nas/ingest/schema/005_backfill.sql` + `nas/batch/crontab.txt` のコメント行 |

## 0. 対象の整理

| データ | 場所(各端末) | 移行 | 方法 |
|---|---|---|---|
| Claude Code 過去トランスクリプト | `~/.claude/projects/**/*.jsonl` | 必要 | バックフィルスクリプト(§1) |
| Claude Code auto memory | `~/.claude/projects/*/memory/*.md` | 必要 | 同上(kind='auto_memory') |
| Codex 過去rollout | `~/.codex/sessions/**/rollout-*.jsonl` | **不要** | senderの走査方式が初回実行時に全履歴を自然に拾う(codex-integration.md §2.1。Codex統合は別追補) |
| 旧記憶システムのDB(SQLite) | 旧環境 | 原則不要 | §3参照 |
| 手書きのCLAUDE.md群 | 各プロジェクト | 不要 | そのまま有効。移行対象ではない |

## 1. Claude Code バックフィルスクリプト

定常経路はSessionEndフック起点のため、過去セッションは流れない。
各端末で一度だけ実行するスクリプト `backfill-claude.sh` を設定リポジトリに同梱する。

- `~/.claude/projects/` 配下の全JSONLを列挙し、定常スプールと同じペイロード形式
  (kind='transcript', agent='claude-code')で `~/.claude-spool/pending/` に書き出す
- project_key はディレクトリ名(パス由来のハイフン区切り)からではなく、
  **JSONL内のcwdフィールドから解決する**(定常経路と同じ規約。
  ディレクトリ名は復元が曖昧なため使わない)
- auto memoryファイルも同時に列挙して送る
- 送信は通常のsenderに任せる(スプールに置くだけ)。冪等性は
  UNIQUE(session_id, message_uuid) が保証するので、定常経路と重複しても壊れない
- 実行は端末ごとに1回。再実行しても無害(冪等。event_idを内容位置から決定的に生成し、
  raw_payloads の UNIQUE(event_id) でも吸収する)

**時期の注意**: Claude Codeにはローカルトランスクリプトの保持期間設定があり、
既定では一定期間で古いセッションが削除されうる。**バックフィルはシステム稼働の
最初期に実施する**(遅らせるほど過去ログが消える)。

## 2. 初回夜間バッチの負荷制御

バックフィル直後の夜間バッチは数ヶ月分のturnsを一度に処理することになり、
LLM使用量・実行時間が跳ね上がる。以下で制御する。

- `batch_runs` のwatermark初期値を「バックフィル完了時点」ではなく
  「システム稼働開始時点」に設定し、**過去分は定常バッチの対象外**とする
  (`nightly.py --init-watermark`。バックフィル投入後・定常バッチ稼働前に一度だけ実行)
- 過去分の蒸留は専用の `backfill-distill` モードで行う:
  - プロジェクト単位 × 期間チャンク(1ヶ月分)で分割実行
  - 優先順位はアクティブなプロジェクトから。休眠プロジェクトは後回しまたは省略
  - 1晩に1〜2チャンクずつ、通常バッチの後に実行(crontab.txt のコメント行を有効化)
  - 使用量の実測(本体設計§13)を兼ねる: 最初のチャンクで消費量を確認してから続行
- 過去分の事実は鮮度で劣るため、ORGANIZE時に現行事実と矛盾したら**常に負ける**
  (バックフィル専用のORGANIZE規則 + replaces強制NULL。
  replacesの向きが逆にならないよう、チャンクは古い期間から順に処理する)
- 進捗は `backfill_progress` テーブル(project_key ごとの done_through / completed)。
  失敗runの補償(facts削除)と整合するよう、進捗のDB反映はrun成功の直前まで遅延する

## 3. 旧記憶システムのデータ

- 旧DBの生ログ層が「Claude Codeのトランスクリプト由来」であれば、
  同じ内容が§1のバックフィルで入るため移行不要。二重投入してもUNIQUE制約で弾かれる
- ただし旧システム稼働中にローカル保持期間切れで**JSONL側からは既に消えた**セッションが
  旧DBにだけ残っている可能性がある。その場合のみ、旧DB→ペイロード形式への
  変換スクリプトで補完する(session_id / message_uuid を保持していることが条件)
- 旧システムの蒸留済み知識(事実・要約層)は移行しない。
  検証基準が現行と異なるため、生ログから再蒸留する方が一貫する。
  どうしても引き継ぎたい知見があれば、`status='unverified'` のfactsとして
  手動投入し、夜間バッチの検証に委ねる

## 4. 移行完了の確認

- [ ] 各端末で backfill-claude.sh を実行済み(端末名をチェックリスト化)
- [ ] turns の最古レコード日時が、各端末のJSONL最古と一致する
- [ ] agent='codex' の過去分が入っている(sender初回走査の完了確認。Codex統合導入後)
- [ ] backfill-distill が全アクティブプロジェクトを消化し、index.md に
      過去由来の恒常事実(環境・ビルド手順等)が反映されている
      (`SELECT project_key, done_through, completed FROM backfill_progress;`)
- [ ] バックフィル分を含めてもSSDボリュームの使用量が想定内
      (JSONLはテキストなので通常は問題にならないが、初回に一度確認する)

## 5. 順序(全体)

1. NAS構築完了(nas-setup.md)+ P0(ingest経路)稼働
2. **直ちに各端末でバックフィル実行**(§1。保持期間による消失を防ぐ)
3. スキーマ 005 を適用し、`nightly.py --init-watermark` を一度実行
4. P1〜P2を実装・稼働(定常の夜間バッチはwatermark以降のみ処理)
5. backfill-distill を数晩かけて実行(§2。crontabのコメント行を有効化し、
   全プロジェクト completed になったら行を削除)
6. §4のチェックで移行完了
