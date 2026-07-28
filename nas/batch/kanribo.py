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


def rules_sql() -> str:
    """有効な保存期間基準(規程)を読む。"""
    return (
        "SELECT json_agg(json_build_object('category', category, 'source_table', source_table, "
        "'ts_column', ts_column, 'retention_days', retention_days, "
        "'retention_years', retention_years, 'measure', measure, 'gate', gate) ORDER BY category) "
        "FROM retention_rules WHERE enabled;"
    )
