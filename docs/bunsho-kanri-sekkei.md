# 文書管理 改定設計書 (2026-07 IPKNOWLEDGEフロー準拠化)

自治体文書管理システム(富士通IPKNOWLEDGE)の標準フロー — 収受/供覧 → 起案 →
決裁(電子/押印) → 発送/施行、横に履歴管理・原本保管・検索、下流に引継 → 借覧管理 →
保存/移管/廃棄 → 公文書館、右に効果・分析 → 公開目録 → 情報公開 — を手本に、
記憶統合システムの文書管理を拡充した際の設計と実施の記録。

運用規則の正本は docs/bunsho-kanri.md(文書管理規程)。本書は「なぜこの形にしたか」と
「何をいつ実施したか」を残す。

## 1. フロー対応表

| # | ブロック | 実装 | 出自 |
|---|---------|------|------|
| 1 | 収受/供覧 | ingest → raw_payloads(received_at=受付印, id=収受番号)。供覧=inbox配信時にingestが借覧簿へ記帳 | 収受は既存、供覧記帳は本改定 |
| 2 | 起案 | drafts(012)。採番=年度×連番、過去起案の複写=related_doc | 既存 |
| 3 | 電子決裁 | LLM審査(課長=shinsa)→専決/上申→LLM決裁(部長=kessai) | 既存 |
| 4 | 押印決裁 | dashboardの`approve_exec`(施行許可の後閲印)。skill/haiki/ikanのapproved文書に押せる | 本改定(旧approve_skillの一般化) |
| 5 | 発送/施行 | 施行(facts登載・skill配置)+index/skillのgit push=全端末配布 | 既存 |
| 6 | 履歴管理 | 回議録draft_log(追記のみ)+facts.replaces系譜 | 既存 |
| 7 | 原本保管(改ざん防止) | 決裁時に封緘(sealed_sha)+改変禁止トリガ | 本改定(017_genpon) |
| 8 | 検索/全文検索 | PGroonga全文+kNNハイブリッド。全端末横断 | 既存 |
| 9 | 引継 | messages(事務引継)+record_files.location(書庫情報) | 既存 |
| 10 | 借覧管理 | 借覧簿lending_log: 閲覧・検索・貸出・供覧の台帳 | 本改定(018_shakuran) |
| 11 | 保存/移管/廃棄 | retention_rules→seiri→record_files→満了→伺い→決裁(+押印)→施行 | 既存(015)。施行経路の不通を本改定で是正 |
| 12 | 公文書館 | archive/(年度別jsonl.gz+sha256)+利用請求(dashboardの取り寄せ=貸出記帳) | 保管は既存、利用請求は本改定 |
| 13 | 効果・分析 | tenken(毎晩)+nendo_report(年度)。収受・借覧・後閲統計を追加 | 既存+本改定 |
| 14 | 公開目録/情報公開 | index.mdのclaude-config配布=公開。除外=redact/exclude/purge | 既存 |

例外経路: purge.py(本人求めによる事後除外)は本フローの外の非常口として維持。

## 2. 改定前に見つかっていた不備と是正

1. **押印決裁の欠落(デッドロック)** — dashboardの後閲印はexecuted/rejected限定、
   施行許可はskill限定で、gate=kouetsuの廃棄・移管文書(approved)に印を押す手段が
   無かった。nightlyの施行条件(seen_state='seen')に永遠に到達しない。
   → `approve_exec`(kind in skill/haiki/ikan)へ一般化。approve_skillは別名として維持。
2. **廃棄系がringi.enabledに巻き込まれて停止** — 起票(ringi_haiki/ringi_ikan)と
   施行が`ringi.enabled`(facts登載の移行スイッチ、当時false)のガード内にあり、
   保存期間基準を有効化しても何も起きない状態だった。
   → `bunsho.enabled`を新設して分離。施行はprocess_bunsho_queue()として
   process_remands()から独立させた。
3. **NASのschema/コピーの欠落** — schema/の正本はこのリポジトリ(nas/ingest/schema)
   だが、NAS側コピーに012/013/016が未配置のままDBだけ適用が進んでいた。
   当初これを「原本消失」と誤認してDBから012を逆生成した(スクラッチDBで
   001→適用チェーンと稼働DBとのpg_dump diff一致まで検証済み)が、
   リポジトリに原本があると判明したため原本で置換した。
   → 是正はリポジトリからの同期。再発検知として点検(tenken)に欠番検査を追加。
   新規migrationの番号もこれで衝突しかけた(016_flags.sqlが既存)ため、
   原本保管=017、借覧簿=018で採番。

## 3. 新設要素の設計判断

### 3.1 原本保管(017_genpon.sql)

- 封緘値は `sha256(doc_no || '\n' || title || '\n' || proposal || '\n' || payload::text)`。
  jsonbの正規形を使うので再計算が安定する。決裁の遷移と同じUPDATEで確定する
  (ringi.transition_sql)。
- 改変禁止はBEFOREトリガ。許すのは状態進行列(state/seen_state/seen_at/executed_at)のみ。
  related_docは参照先の移管(FKのSET NULL)でだけ変わるため、廃棄セッション限定で許す。
- 例外経路はGUC `bunsho.disposal = 'on'` のみ。GUC未設定時にcurrent_settingが
  NULLを返し三値論理でガードが素通りするバグを初回検証で踏んだため、
  coalesceで必ずbooleanに落とす実装にしている(トリガのテストは実DBの
  ロールバックされるトランザクションで7ケース実証)。
- 点検の封緘照合は全件(現状の文書量では安価。増えたらサンプリングに切り替える)。

### 3.2 借覧簿(018_shakuran.sql)

- 1テーブルに閲覧(etsuran)・検索(kensaku)・貸出/返却(kashidashi/henkyaku)・
  供覧(kouran)を積む。対象はobject_kind+object_ref(jsonb)で表す。
- readerロールはINSERT+シーケンスUSAGEのみ(RETURNINGはSELECT権限を要求して
  失敗するため付けない — 検証で確認)。
- 記帳失敗は閲覧・検索・配信を妨げない(可用性優先)。例外は公文書館の取り寄せで、
  貸出だけは記帳が通ることを表示の条件にする。
- 供覧の記帳は端末側でなくingestの/inbox(配信の実施側)で行う。端末改修不要で、
  旧クライアントでも漏れない。配信のトランザクションとは分離する
  (記帳失敗が既読化を巻き戻して二重配信にならないように)。
- 借覧簿自体も文書: kanri-shakuran(1年・年度起算・廃棄・sokujiko)として
  保存期間基準と管理簿の同じ経路に乗せる。

### 3.3 公文書館の利用請求

- 移管の施行時に管理簿のlocationを`archive/<年度>/<ファイル>.jsonl.gz`へ更新し、
  dashboardの取り寄せはそれだけを参照する(パスはホワイトリスト正規表現で検証)。
- 取り寄せは閲覧のみでDBへ書き戻さない。恒久復元は必要になったら起案文書で通す。

## 4. 実施記録(2026-07-31)

- 017_genpon.sql / 018_shakuran.sql を稼働DBへ適用。既存決裁済み文書は0件のため
  遡及封緘の対象なし
- NAS schema/ をリポジトリと同期(012/013/016を配置)。スクラッチDBで001→018の
  適用チェーン全通過、drafts系スキーマがpg_dumpで稼働DBと完全一致することを確認
- nightly.py: bunsho.enabled分離・process_bunsho_queue新設・廃棄/移管施行への
  GUC付与・移管時のlocation更新・点検拡張(封緘照合・トリガ存在・schema欠番)・
  年度報告拡張(収受・借覧・後閲統計)
- dashboard: approve_exec・閲覧記帳・公文書館取り寄せ。実サーバ+実DBで
  E2E検証(閲覧→借覧簿、押印→seen/回議録、取り寄せ→貸出記帳、
  nightlyの施行クエリが押印済み文書を拾うこと、封緘無し決裁済み文書を
  点検がgenpon_ihan=1として検出すること)
- ingest /inbox: 供覧記帳(イメージ再ビルドで反映)。実メッセージ配信で検証
- config.json: bunsho.enabled=true。retention_rules: kanri-shakuranを有効化
  (適用順序は規程 第9章)
- tests: TestSchemaMirrorを015+018対応に修正、封緘・bunsho設定のテストを追加
  (235件全通過)
