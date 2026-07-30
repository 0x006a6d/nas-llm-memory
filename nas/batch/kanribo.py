#!/usr/bin/env python3
"""整理と管理簿(公文書管理法5条〜7条) — 015_kanribo.sql と対

- 整理: 収集した記録に分類・名称・保存期間・満了日・満了時の措置を与える
- 集合物: 「行政文書ファイル」= (大分類, 中分類=project_key, 期間) の単位で管理する。
  個々の行に保存期間を持たせない(廃棄・移管も集合物単位で行う)
- 保存期間の起算: 年単位は会計年度起算(満了 = 作成年度の翌年度初めからN年)、
  日単位は月で締めてから起算(満了 = 月末からN日)

このモジュールはDBへ接続しない(SQL文字列を返すだけ)。ringi.py と同じ約束。
"""
import datetime

import ringi

# 大分類ごとの実体。retention_rules(規程)は保存期間と措置の正、
# こちらは「どのテーブルのどの列を、どう束ねるか」の正。
# 両者が食い違わないことは tests/test_kanribo.py が 015_kanribo.sql と照合して検査する。
SOURCES = {
    "shuju-raw": {
        "label": "収受(生データ)", "table": "raw_payloads", "ts": "received_at",
        "key": "'general'", "id": "id", "where": "disposed_at IS NULL",
        "location": "DB raw_payloads",
    },
    "shuju-turns": {
        "label": "収受(生ログ)", "table": "turns", "ts": "coalesce(ts, now())",
        "key": "project_key", "id": "id", "where": None,
        "location": "DB turns",
    },
    "shuju-memo": {
        "label": "収受(内蔵メモリ)", "table": "auto_memory_snapshots", "ts": "received_at",
        "key": "project_key", "id": "id", "where": None,
        "location": "DB auto_memory_snapshots",
    },
    "kiroku-fact": {
        "label": "記録(事実)", "table": "facts", "ts": "created_at",
        "key": "project_key", "id": "id", "where": None,
        "location": "DB facts",
    },
    "kessai-doc": {
        "label": "決裁文書", "table": "drafts", "ts": "created_at",
        "key": "project_key", "id": "id", "where": None,
        "location": "DB drafts",
    },
    "renraku-msg": {
        "label": "事務引継", "table": "messages", "ts": "created_at",
        "key": "coalesce(to_project, 'general')", "id": "id", "where": None,
        "location": "DB messages",
    },
    "unyou-run": {
        "label": "運用記録", "table": "batch_runs", "ts": "started_at",
        "key": "coalesce(project_key, 'general')", "id": "id", "where": None,
        "location": "DB batch_runs",
    },
    "kanri-shakuran": {
        "label": "借覧簿", "table": "lending_log", "ts": "at",
        "key": "'general'", "id": "id", "where": None,
        "location": "DB lending_log",
    },
}

MEASURE_LABEL = {"ikan": "移管", "haiki": "廃棄", "jouyou": "常用"}
STATE_LABEL = {"genyou": "現用", "manryou": "満了", "haiki_zumi": "廃棄済", "ikan_zumi": "移管済"}


# ---------------------------------------------------------------- 期間と満了日

def period_of(rule: dict, d: datetime.date) -> str:
    """その日付が属する集合物の単位。年度起算は年度、日起算は月。"""
    if rule["retention_days"]:
        return f"{d.year}-{d.month:02d}"
    return str(ringi.fiscal_year(d))


def expires_on(rule: dict, period: str):
    """保存期間の満了する日。常用(jouyou / 期間0)はNone。

    年度起算: 作成年度の翌年度初め(4/1)から起算してN年 → (年度+1+N)/3/31
    日起算  : その月の末日から起算してN日
    """
    if rule["measure"] == "jouyou" or (not rule["retention_days"]
                                       and not rule["retention_years"]):
        return None
    if rule["retention_days"]:
        y, m = (int(x) for x in period.split("-"))
        last = (datetime.date(y + (m == 12), (m % 12) + 1, 1)
                - datetime.timedelta(days=1))     # その月の末日
        return last + datetime.timedelta(days=rule["retention_days"])
    fy = int(period)
    return datetime.date(fy + 1 + rule["retention_years"], 3, 31)


def fiscal_year_of(period: str) -> int:
    """集合物の単位から年度を出す('2026-07' → 2026、'2025' → 2025)。"""
    if "-" in period:
        y, m = (int(x) for x in period.split("-"))
        return ringi.fiscal_year(datetime.date(y, m, 1))
    return int(period)


def file_name(category: str, project_key: str, period: str) -> str:
    """小分類 = 行政文書ファイルの名称(管理簿に載る名前)。"""
    label = SOURCES[category]["label"]
    fy = fiscal_year_of(period)
    n = ringi.reiwa(fy)
    wareki = f"令和{'元' if n == 1 else n}年度"
    if "-" in period:
        return f"{label} {project_key} {wareki}{int(period.split('-')[1])}月"
    return f"{label} {project_key} {wareki}"


# ---------------------------------------------------------------- SQLビルダ

def q(s) -> str:
    return ringi.q(s)


def scan_sql(category: str, rule: dict) -> str:
    """整理の材料を集めるSELECT。集合物ごとの件数・id範囲・時刻範囲を返す。

    期間の切り方は保存期間の起算に合わせる(日起算=月、年度起算=年度)。
    年度は「4/1区切り」なので、3か月引いた年をとる。
    """
    src = SOURCES[category]
    where = f"WHERE {src['where']} " if src["where"] else ""
    if rule["retention_days"]:
        period = (f"to_char({src['ts']}, 'YYYY-MM')")
    else:
        period = (f"extract(year from ({src['ts']}) - interval '3 months')::int::text")
    return (
        f"SELECT json_agg(json_build_object('period', period, 'key', key, 'n', n, "
        f"'id_from', id_from, 'id_to', id_to, 'first_ts', first_ts, 'last_ts', last_ts)) "
        f"FROM (SELECT {period} AS period, {src['key']} AS key, count(*) AS n, "
        f"min({src['id']}) AS id_from, max({src['id']}) AS id_to, "
        f"min({src['ts']}) AS first_ts, max({src['ts']}) AS last_ts "
        f"FROM {src['table']} {where}GROUP BY 1, 2) s;"
    )


def upsert_sql(category: str, rule: dict, row: dict) -> str:
    """管理簿への記載(法7条)。既に廃棄・移管済みの行は更新しない。

    件数とid範囲は整理のたびに現況へ合わせる(その集合物に後から行が増えるため)。
    満了日と措置は記載時に確定し、以後は動かさない(レコードスケジュール)。
    """
    period = str(row["period"])
    exp = expires_on(rule, period)
    return (
        f"INSERT INTO record_files (category, project_key, name, period, fiscal_year, "
        f"expires_on, measure, location, n_rows, id_from, id_to, first_ts, last_ts) "
        f"VALUES ({q(category)}, {q(row['key'])}, "
        f"{q(file_name(category, row['key'], period))}, {q(period)}, "
        f"{fiscal_year_of(period)}, {q(exp) if exp else 'NULL'}, "
        f"{q(rule['measure'])}, {q(SOURCES[category]['location'])}, "
        f"{int(row['n'])}, {int(row['id_from'])}, {int(row['id_to'])}, "
        f"{q(row['first_ts'])}, {q(row['last_ts'])}) "
        f"ON CONFLICT (category, project_key, period) DO UPDATE SET "
        f"n_rows = EXCLUDED.n_rows, "
        f"id_from = least(record_files.id_from, EXCLUDED.id_from), "
        f"id_to = greatest(record_files.id_to, EXCLUDED.id_to), "
        f"first_ts = least(record_files.first_ts, EXCLUDED.first_ts), "
        f"last_ts = greatest(record_files.last_ts, EXCLUDED.last_ts), "
        f"updated_at = now() "
        f"WHERE record_files.state = 'genyou' "
        f"RETURNING id;"
    )


def manryou_sql() -> str:
    """満了したファイルの状態を現用→満了に進める(法5条のレコードスケジュールの実行段)。

    満了日を過ぎた現用ファイルだけを対象にする。措置(廃棄/移管)は記載時に
    確定済みなので、ここでは判断しない。返り値は状態を進めた行。
    """
    return (
        "WITH upd AS ("
        "UPDATE record_files SET state = 'manryou', updated_at = now() "
        "WHERE state = 'genyou' AND expires_on IS NOT NULL "
        "AND expires_on <= current_date RETURNING id, category, name, measure, n_rows) "
        "SELECT json_agg(json_build_object('id', id, 'category', category, 'name', name, "
        "'measure', measure, 'n_rows', n_rows) ORDER BY id) FROM upd;"
    )


def manryou_soon_sql(days: int = 30) -> str:
    """満了が近いファイル(既定30日以内)。点検・注意喚起用。"""
    return (
        f"SELECT count(*) FROM record_files WHERE state = 'genyou' "
        f"AND expires_on IS NOT NULL "
        f"AND expires_on <= current_date + {int(days)};"
    )


def pending_measure_sql() -> str:
    """満了済みで、まだ廃棄・移管の文書が起票されていないファイル。"""
    return (
        "SELECT json_agg(json_build_object('id', id, 'category', category, "
        "'project_key', project_key, 'name', name, 'period', period, "
        "'expires_on', expires_on, 'measure', measure, 'location', location, "
        "'n_rows', n_rows, 'id_from', id_from, 'id_to', id_to) ORDER BY expires_on, id) "
        "FROM record_files WHERE state = 'manryou' AND disposed_draft IS NULL "
        "AND measure <> 'jouyou';"
    )


# ---------------------------------------------------------------- 廃棄(法8条2項)

def dispose_sql(f: dict, draft_id: int) -> str:
    """1ファイルの廃棄を実行するSQL。返り値は RETURNING で消えた件数を返す1文。

    分類ごとに実体が違うので消し方も変える:
    - shuju-raw : 本文(payload)だけ廃棄して行は残す。turns.payload_id のFKが
      NO ACTION で行削除できないうえ、いつ何を廃棄したかの証跡も残したい
    - shuju-turns: 行削除。ただし**現用factsの根拠(provenance)になっている行は残す**
      (根拠の消えた事実を作らない = 現用文書は廃棄しない)
    - その他: 行削除。batch_runs は watermark を持つ最新の成功runを必ず残す

    範囲は管理簿の id_from〜id_to(整理で確定済み)に限定し、中分類(project_key)でも絞る。
    措置が廃棄でないファイルは受け付けない(呼び出し側の絞り込みに依存しない)。
    """
    if f.get("measure") != "haiki":
        raise ValueError(f"廃棄SQL: 措置が廃棄でない({f.get('measure')}) {f.get('name')}")
    cat = f["category"]
    src = SOURCES[cat]
    lo, hi = int(f["id_from"]), int(f["id_to"])
    key = f["project_key"]
    if cat == "shuju-raw":
        return (
            f"WITH d AS (UPDATE raw_payloads SET payload = '{{}}'::jsonb, "
            f"disposed_at = now(), disposed_draft = {int(draft_id)} "
            f"WHERE id BETWEEN {lo} AND {hi} AND disposed_at IS NULL RETURNING id) "
            f"SELECT count(*) FROM d;"
        )
    if cat == "shuju-turns":
        return (
            f"WITH d AS (DELETE FROM turns t WHERE t.id BETWEEN {lo} AND {hi} "
            f"AND t.project_key = {q(key)} "
            f"AND NOT EXISTS (SELECT 1 FROM current_facts cf "
            f"WHERE t.id = ANY(cf.provenance)) RETURNING t.id) "
            f"SELECT count(*) FROM d;"
        )
    if cat == "unyou-run":
        return (
            f"WITH d AS (DELETE FROM batch_runs b WHERE b.id BETWEEN {lo} AND {hi} "
            f"AND coalesce(b.project_key, 'general') = {q(key)} "
            f"AND b.finished_at IS NOT NULL "
            f"AND (b.watermark_turn_id IS NULL OR b.watermark_turn_id < "
            f"(SELECT max(watermark_turn_id) FROM batch_runs WHERE status = 'success')) "
            f"RETURNING b.id) SELECT count(*) FROM d;"
        )
    return (
        f"WITH d AS (DELETE FROM {src['table']} x WHERE x.id BETWEEN {lo} AND {hi} "
        f"AND {src['key'].replace('to_project', 'x.to_project')} = {q(key)} "
        f"RETURNING x.id) SELECT count(*) FROM d;"
    )


def survivors_sql(f: dict) -> str:
    """廃棄しても残る行(= 現用factsの根拠になっているturns)の件数。伺い文に載せる。"""
    if f["category"] != "shuju-turns":
        return ""
    return (
        f"SELECT count(*) FROM turns t WHERE t.id BETWEEN {int(f['id_from'])} "
        f"AND {int(f['id_to'])} AND t.project_key = {q(f['project_key'])} "
        f"AND EXISTS (SELECT 1 FROM current_facts cf WHERE t.id = ANY(cf.provenance));"
    )


def disposed_sql(file_id: int, draft_id: int, n: int, state: str = "haiki_zumi") -> str:
    """管理簿に施行の結果を記載する(施行と別文で呼ぶ経路用)。"""
    return (
        f"UPDATE record_files SET state = {q(state)}, disposed_draft = {int(draft_id)}, "
        f"n_rows = {int(n)}, updated_at = now() WHERE id = {int(file_id)} RETURNING id;"
    )


def dispose_and_record_sql(f: dict, draft_id: int, state: str = "haiki_zumi") -> str:
    """廃棄・移管の実行と管理簿への記載を1文にする。

    別々に流すと、消えたのに管理簿が現用のまま(またはその逆)という中間状態が
    残りうる。実行した件数をそのまま管理簿の件数に書く。
    """
    inner = dispose_sql(f, draft_id) if state == "haiki_zumi" else ikan_delete_sql(f)
    body = inner[len("WITH d AS ("):].rsplit(") SELECT count(*) FROM d;", 1)[0]
    return (
        f"WITH d AS ({body}), "
        f"u AS (UPDATE record_files SET state = {q(state)}, "
        f"disposed_draft = {int(draft_id)}, n_rows = (SELECT count(*) FROM d), "
        f"updated_at = now() WHERE id = {int(f['id'])}) "
        f"SELECT count(*) FROM d;"
    )


def unfile_sql(file_id: int) -> str:
    """起票を取り消して未起票へ戻す(処理中断時)。決着した否決には使わない。"""
    return (f"UPDATE record_files SET disposed_draft = NULL, updated_at = now() "
            f"WHERE id = {int(file_id)} AND state = 'manryou' RETURNING id;")


def haiki_items(f: dict, survivors: int = 0) -> list:
    """廃棄伺いの「記」の箇条(件名・数量・保存期間・満了日を明示する)。"""
    if f.get("measure") != "haiki":
        raise ValueError(f"廃棄伺い: 措置が廃棄でない({f.get('measure')}) {f.get('name')}")
    items = [
        f"分類 {f['category']}・ファイル「{f['name']}」を廃棄する",
        f"保存期間は{('満了日 ' + str(f['expires_on'])[:10]) if f.get('expires_on') else '常用'}"
        f"をもって満了している",
        f"対象は{f['n_rows']}件(id {f['id_from']}〜{f['id_to']}、保存場所 {f['location']})",
    ]
    if f["category"] == "shuju-raw":
        items.append("廃棄は本文のみとし、受信記録の行と受信日時は証跡として残す")
    if survivors:
        items.append(f"うち{survivors}件は現用の事実の根拠(provenance)のため廃棄しない")
    return items


# ---------------------------------------------------------------- 移管(法8条1項)

def export_sql(f: dict) -> str:
    """移管するファイルの中身を1行1JSONで取り出すSQL。

    廃棄と違い中身を残すので、行をそのまま(列名つきで)書き出す。
    範囲は管理簿の id_from〜id_to と中分類に限定する。
    """
    cat = f["category"]
    src = SOURCES[cat]
    key_expr = src["key"].replace("to_project", "x.to_project") \
        if "to_project" in src["key"] else src["key"]
    cond = f"x.id BETWEEN {int(f['id_from'])} AND {int(f['id_to'])}"
    if key_expr != "'general'":
        cond += f" AND {key_expr.replace('project_key', 'x.project_key')} = {q(f['project_key'])}"
    return (f"SELECT json_agg(to_jsonb(x) ORDER BY x.id) FROM {src['table']} x "
            f"WHERE {cond};")


def ikan_delete_sql(f: dict) -> str:
    """移管後にDBから外すSQL(中身はアーカイブ領域に残っている)。

    措置が移管でないファイルは受け付けない(廃棄経路と取り違えない)。
    """
    if f.get("measure") != "ikan":
        raise ValueError(f"移管SQL: 措置が移管でない({f.get('measure')}) {f.get('name')}")
    src = SOURCES[f["category"]]
    key_expr = src["key"].replace("to_project", "x.to_project") \
        if "to_project" in src["key"] else src["key"]
    cond = f"x.id BETWEEN {int(f['id_from'])} AND {int(f['id_to'])}"
    if key_expr != "'general'":
        cond += f" AND {key_expr.replace('project_key', 'x.project_key')} = {q(f['project_key'])}"
    return (f"WITH d AS (DELETE FROM {src['table']} x WHERE {cond} RETURNING x.id) "
            f"SELECT count(*) FROM d;")


def archive_path(f: dict) -> str:
    """移管先(アーカイブ領域)の相対パス。年度で切ってファイル名は管理簿と対応させる。"""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in str(f["project_key"]))
    return f"{fiscal_year_of(f['period'])}/{f['category']}_{safe}_{f['period']}.jsonl.gz"


def ikan_items(f: dict, path: str, n: int, sha: str) -> list:
    """移管伺いの「記」の箇条。"""
    return [
        f"分類 {f['category']}・ファイル「{f['name']}」を移管する",
        f"保存期間は満了日 {str(f.get('expires_on') or '')[:10]} をもって満了している",
        f"対象は{n}件(id {f['id_from']}〜{f['id_to']}、保存場所 {f['location']})",
        f"移管先は archive/{path}(sha256 {sha[:16]}…)",
        "移管後はDBから外すが、中身はアーカイブ領域に保存し復元できる",
    ]


# ---------------------------------------------------------------- 点検・報告(法9条)

def tenken_sql() -> str:
    """管理簿と実データのずれを点検する材料。

    - 措置未設定(常用でないのに満了日が無い)
    - 満了しているのに現用のまま(check_manryouが走っていない)
    - 施行済みなのに管理簿が現用のまま(または逆)
    """
    return (
        "SELECT json_build_object("
        "'files', (SELECT count(*) FROM record_files), "
        "'genyou', (SELECT count(*) FROM record_files WHERE state='genyou'), "
        "'manryou', (SELECT count(*) FROM record_files WHERE state='manryou'), "
        "'haiki_zumi', (SELECT count(*) FROM record_files WHERE state='haiki_zumi'), "
        "'ikan_zumi', (SELECT count(*) FROM record_files WHERE state='ikan_zumi'), "
        "'no_schedule', (SELECT count(*) FROM record_files "
        "                WHERE measure <> 'jouyou' AND expires_on IS NULL), "
        "'overdue', (SELECT count(*) FROM record_files "
        "            WHERE state='genyou' AND expires_on <= current_date), "
        "'unfiled', (SELECT count(*) FROM record_files "
        "            WHERE state='manryou' AND disposed_draft IS NULL "
        "            AND measure <> 'jouyou'), "
        # 原本保管(017_genpon)の検証: 封緘ハッシュの再計算照合と改変禁止トリガの存在
        "'genpon_ihan', (SELECT count(*) FROM drafts WHERE decided_at IS NOT NULL "
        "                AND (sealed_sha IS NULL OR sealed_sha <> "
        "                     encode(digest(doc_no || E'\\n' || title || E'\\n' || "
        "                     proposal || E'\\n' || payload::text, 'sha256'), 'hex'))), "
        "'genpon_trigger', (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
        "                   AND tgname IN ('drafts_genpon_guard_tg', "
        "                                  'draft_log_append_only_tg')));"
    )


def nendo_report_sql(fy: int) -> str:
    """年度の管理状況(受入・廃棄・移管・滞留)。管理状況報告の別記に使う。"""
    return (
        f"SELECT json_build_object("
        f"'files', (SELECT count(*) FROM record_files WHERE fiscal_year = {int(fy)}), "
        f"'rows', (SELECT coalesce(sum(n_rows), 0) FROM record_files "
        f"         WHERE fiscal_year = {int(fy)}), "
        f"'haiki', (SELECT count(*) FROM record_files "
        f"          WHERE fiscal_year = {int(fy)} AND state = 'haiki_zumi'), "
        f"'ikan', (SELECT count(*) FROM record_files "
        f"         WHERE fiscal_year = {int(fy)} AND state = 'ikan_zumi'), "
        f"'drafts', (SELECT count(*) FROM drafts WHERE fiscal_year = {int(fy)}), "
        f"'miketsu', (SELECT count(*) FROM drafts WHERE state = 'pending_decision'), "
        f"'kouetsu_machi', (SELECT count(*) FROM drafts WHERE seen_state = 'pending' "
        f"                  AND state IN ('executed','rejected','approved')), "
        # 収受・借覧・後閲の年度統計(docs/bunsho-kanri.md §6)
        f"'shunyu', (SELECT count(*) FROM raw_payloads "
        f"           WHERE received_at >= '{int(fy)}-04-01'::date "
        f"           AND received_at < '{int(fy) + 1}-04-01'::date), "
        f"'shakuran', (SELECT coalesce(json_object_agg(action, n), '{{}}'::json) FROM "
        f"             (SELECT action, count(*) AS n FROM lending_log "
        f"              WHERE at >= '{int(fy)}-04-01'::date "
        f"              AND at < '{int(fy) + 1}-04-01'::date GROUP BY action) s), "
        f"'kouetsu_days', (SELECT round((extract(epoch FROM percentile_cont(0.5) "
        f"                 WITHIN GROUP (ORDER BY seen_at - executed_at)) / 86400.0)"
        f"                 ::numeric, 1) FROM drafts WHERE seen_at IS NOT NULL "
        f"                 AND executed_at IS NOT NULL AND fiscal_year = {int(fy)}));"
    )


def tenken_items(st: dict) -> list:
    """点検結果の「記」の箇条。"""
    return [
        f"管理簿に{st.get('files', 0)}ファイル"
        f"(現用{st.get('genyou', 0)} / 満了{st.get('manryou', 0)} / "
        f"廃棄済{st.get('haiki_zumi', 0)} / 移管済{st.get('ikan_zumi', 0)})",
        f"措置未設定 {st.get('no_schedule', 0)}件",
        f"満了日を過ぎているのに現用のまま {st.get('overdue', 0)}件",
        f"満了したが廃棄・移管の起票が無い {st.get('unfiled', 0)}件",
        f"原本封緘の照合不一致 {st.get('genpon_ihan', 0)}件"
        f"(改変禁止トリガ {st.get('genpon_trigger', 0)}/2)",
    ]


def tenken_problems(st: dict) -> list:
    """点検で見つかった不整合(空なら異状なし)。"""
    out = []
    if st.get("no_schedule"):
        out.append(f"措置未設定 {st['no_schedule']}件")
    if st.get("overdue"):
        out.append(f"満了日超過のまま現用 {st['overdue']}件")
    if st.get("unfiled"):
        out.append(f"満了したが未起票 {st['unfiled']}件")
    if st.get("genpon_ihan"):
        out.append(f"原本封緘の照合不一致 {st['genpon_ihan']}件")
    if st.get("genpon_trigger", 2) < 2:
        out.append("原本保管の改変禁止トリガが欠落")
    return out


def rules_sql() -> str:
    """有効な保存期間基準(規程)を読む。"""
    return (
        "SELECT json_agg(json_build_object('category', category, 'source_table', source_table, "
        "'ts_column', ts_column, 'retention_days', retention_days, "
        "'retention_years', retention_years, 'measure', measure, 'gate', gate) ORDER BY category) "
        "FROM retention_rules WHERE enabled;"
    )
