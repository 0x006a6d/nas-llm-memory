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

## 5. 改定記録(2026-07-31 決裁と後閲の整理)

「人間の承認が施行の条件なら、それは後閲ではなく決裁そのもの」という指摘を受けて
是正した(019_kessai_gate.sql)。§1の表4「押印決裁=approve_exec」はこの改定で
人間決裁(kessai op)に置き換わった。

- 人間決裁の対象を性質で定めた: skill登載(実行される指示の全端末配布)と
  生ログturnsの廃棄(一次記録の不可逆な消失)。どちらも審査(課長)の上申で
  pending_decisionに止まり、dashboardの決裁/否決を待つ。決裁と同時に封緘し、
  seen済みにする(自ら決裁した文書に後閲は不要)
- 取り込み済み履歴(shuju-memo)・使い捨て伝言(renraku-msg)・可逆な移管(kessai-doc)
  はルーチンとしてsokujiko(LLM決裁で即施行)に変更。後閲は「人間が関与せずに
  施行された文書の事後確認」に一本化された
- スキル登載経路は独立スイッチ(skill.enabled)で有効化し、LLM決裁(KESSAI_SKILL_PROMPT)と
  skill_auto_execute設定を廃止。セッションへの「採用して」による手動採用の案内も
  書庫決裁への一本化で廃止
- 未決繰越の自動廃案(3晩)はkind='fact'限定であることを確認済み。人間決裁待ちは
  期限なく待つ
- E2E検証中に既存バグを検出・修正: dashboardの書庫操作の楽観ロックが一度も効いて
  いなかった(psqlが-tでもINSERTのコマンドタグを出すため空出力判定が偽陽性)。
  RETURNINGのdraft_id行の有無で判定するよう修正し、409/200を実測で確認

## 6. 改定記録(2026-07-31 dashboardのタブ整理・原本表示・決裁差戻)

- 収受簿タブを本物の受付台帳(raw_payloads: 収受番号=id・受付印=received_at・
  差出端末・種別・処理状況)に置き換えた。従来の中身(sync-exclude・crontab・
  バッチ実行履歴・spool接続設定・hook・配布リポジトリ)は新設の「監理」タブへ移し、
  書庫タブにあった専決規程(モデルの役割分担)も監理タブへ移設。
  タブ名「書庫 (決裁済)」は決裁待ち・未決も入るため「書庫」に改めた
- 書庫の文書は一覧クリックで公文書風モーダル(原本)を開く形にし、決裁欄に
  印鑑を押す表示にした(俳句=haiku・曽根=sonnet・尾羽=opus・人間=human。
  担当モデル名から印影を引き、該当しない担当は文字のまま)
- 決裁待ち文書の操作を「決裁/否決」から「決裁/差戻」に改めた(否決opはAPI互換で残置)。
  差戻はringi.TRANSITIONSどおり pending_decision→remanded_to_reviewer で、
  翌晩process_remandsの_saikento_oneが審査(課長)として人間メモを踏まえ
  補正・再上申(skillはSKILL.md補正可)または廃案にする。人間決裁事項のため
  審査の専決(shinsa_ok)で人間を飛ばす遷移は使わない

## 7. 改定記録(2026-08-05 タブ再編: 決裁・後閲と逓送の分離)

書庫タブに決裁操作・夜間便リプレイ・路線図が同居し、監理タブに専決規程・夜間バッチの
実行状況・設備が混在していたのを、「人間が処理する場」「完結文書の書庫」「夜間便の記録」
「端末設備」の4つに分けた。実装後の2回のレビューで到達性(一覧から漏れる状態がないか)
の不備が見つかり、以下は当該レビューを経た最終形。

- 新設した「決裁・後閲」タブに、書庫タブにあった停留所マップ(路線図)・専決規程
  (batch/config.json の roles、旧監理タブ)・人間の要処理キュー(文書一覧+原本モーダル)
  を集約した。「書庫」タブは施行済・廃案で人間の操作が残っていない完結文書だけを載せる
  閲覧専用書庫に純化した。「逓送」タブに、監理タブにあったNAS夜間バッチ(crontab)・
  バッチ実行履歴と、書庫タブにあった夜間便リプレイを移した。「監理」タブは端末側の
  実接続設定(~/.claude-spool)・hookスクリプト・claude-configリポジトリ状態だけに絞った。
  「収受簿」タブに収集除外(sync-exclude.txt)の編集を監理タブから移設した(収受から
  除外する規程なので受付台帳の隣が自然)
- 決裁・後閲タブのフィルタは miketsu(決裁待ち・未決)/remanded(差し戻し・再審理中)/
  pending(後閲待ち)/kiketsu(決裁済・施行待ち)の4つ。当初は3つだったが、人間が決裁
  (kessai op)すると同時に封緘・seen済みになる仕様のため、決裁直後〜翌晩の施行までの間、
  その文書は seen_state='pending' の条件を持つ pending フィルタにも state='pending_decision'
  を条件にする miketsu フィルタにも該当せず、どの一覧からも到達不能になっていた。
  `state='approved'` を条件にする kiketsu を追加してこれを解消した。
  LLM決裁直後(skill/haiki/ikan、seen_state='pending')は施行条件が人間の後閲印であるため
  pending と kiketsu の両方に出るが、これは仕様どおりの重複として維持する(排他にしない)
- 停留所マップのクリックは、当初「任意の state への直接絞り込み」(`filt=all&state=X`)
  だったが、これだと審査中(pending_review)や決裁済(approved)を経由して完結文書
  (kanketsu 相当)が決裁・後閲タブの一覧にそのまま出てしまう。停留所クリックの絞り込み先を
  決裁・後閲タブが持つ4つのキューフィルタだけに揃え(起案/審査/決裁→miketsu、決裁済→kiketsu、
  施行/後閲→pending)、`state` パラメータは一切送らないようにした
- 「審査」停留所を非クリックにする案も検討したが、それでは審査中(pending_review)の文書が
  UI全体から到達不能のまま残ってしまう。裁定として miketsu の条件を
  `state in ('pending_review','pending_decision')` に拡張し(文書事務上「未決」は
  「決裁が終わっていない文書」全般を指すので、この方が名前に対して正確)、審査停留所は
  クリック可能なまま miketsu に接続した。各停留所のバッジ件数(審査=pending_review件数、
  決裁=pending_decision件数)は一覧全体(miketsu)の部分集合になるので、以前のように
  「バッジの合計と一覧の中身が一致しない」問題は起きない
- 「施行」停留所の件数は当初 executed(施行済み全件)を出していたが、これは後閲済みの
  古い文書も積み上がって減らない数字になる。「まだ後閲されていない施行済み」だけを表す
  `executed_pending`(state='executed' かつ seen_state='pending')を`shelf_counts()`に
  追加し、バッジをこちらに差し替えた(クリック先の pending 一覧には rejected/approved 分も
  混ざるため、バッジ件数と一覧件数は厳密には一致しない)
- 「決裁」停留所の「未決 N」二重バッジ(別ソース `N.shelf_miketsu` 由来)は本体バッジ
  (`c.pending_decision`)とほぼ同じ数字で、後閲・決裁操作を経ても片方だけが更新され
  食い違うことがあったため削除し、単一ソースにした
- 完結の判定(`filter=kanketsu`)は当初 `state in ('executed','rejected') and
  seen_state='seen'` だったが、承認前に差し戻された廃案(`approved`→`rejected`、
  `seen_state='remanded'`)は施行された効果が無く是正(saishinri)の対象にもならない
  終端であるにもかかわらず、この条件から漏れていた。かといって差し戻し・再審理中
  (remanded)キューに残しても、翌晩のバッチのどの再処理経路(`process_remands` の
  `state='reexamine'`・`remanded_to_reviewer` 向け再審査、`process_miketsu` の
  `state='pending_decision'` 向け未決繰越)も `rejected` を拾わない設計になっている
  (rejected は廃案という終端そのものであり、蒸し返す経路を持たせていない) ため
  誰も処理できない — 要処理キューに操作不能な項目を残す形になっていた。
  kanketsu の条件を `state in ('executed','rejected') and seen_state <> 'pending'` に
  広げてこの終端廃案を書庫へ送り(「差し戻し」チップ付きで並ぶ)、remanded 側の条件から
  `state <> 'rejected'` を明示して終端廃案を除外した。これにより差し戻し・再審理中には
  処理可能な文書だけが残る
- 原本モーダル(`openShelfDoc()`)は当初「完結文書を開くと決裁・後閲の各条件が自然に
  falseになるので閲覧専用表示になる」という設計だったが、これは書庫タブから開いた文書の
  「関連文書」リンク経由でまだ未完結の文書を再帰的に開いた場合に成立しない(rel-doc の
  再帰呼び出しは readOnly を継承していなかった)。明示の `readOnly` 引数を追加し、
  決裁・後閲の4条件すべてに `&& !readOnly` を掛け、rel-doc の再帰呼び出しにも同じ
  `readOnly` を伝播するようにした。書庫タブは常に `readOnly=true` で呼ぶ
- 収受簿タブの端末/種別フィルタ変更で `renderCollect()` 全体を再描画すると、同じタブ内の
  収集除外(sync-exclude.txt)エディタの未保存の編集が消えてしまう。受付台帳部分だけを
  差し替える `loadShuju()` に分離し、フィルタ変更はこちらだけを呼ぶようにした
- 決裁・後閲タブでの決裁・差戻・後閲印の操作後は、文書一覧だけでなく停留所マップの件数も
  古びる。`openShelfDoc` の `onChanged` コールバックを `refreshAfterOp`(一覧の再取得+
  `drawStationMap` の再実行)にして両方を引き直すようにした
