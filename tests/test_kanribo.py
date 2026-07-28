"""整理と管理簿(kanribo.py / 015_kanribo.sql)の検査(DB不要)。

実行: python3 -m unittest discover tests
"""
import datetime
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "nas" / "batch"))

import kanribo  # noqa: E402

from test_ringi_flow import Harness  # noqa: E402

SCHEMA = (ROOT / "nas" / "ingest" / "schema" / "015_kanribo.sql").read_text(encoding="utf-8")


def rule(days=0, years=0, measure="haiki", category="shuju-turns", gate="kouetsu"):
    return {"category": category, "retention_days": days, "retention_years": years,
            "measure": measure, "gate": gate}


class TestSchemaMirror(unittest.TestCase):
    """SOURCES(コード) と retention_rules の初期値(規程) が食い違わないこと。"""

    def _seeded_rules(self):
        """015 の INSERT から (分類, テーブル, 時刻列) を読む。"""
        body = SCHEMA.split("INSERT INTO retention_rules", 1)[1]
        return {m[0]: (m[1], m[2]) for m in
                re.findall(r"\('([a-z-]+)',\s*'(\w+)',\s*'(\w+)'", body)}

    def test_categories_match(self):
        seeded = self._seeded_rules()
        self.assertEqual(set(seeded), set(kanribo.SOURCES),
                         "015の分類とkanribo.SOURCESがずれている")

    def test_table_and_ts_column_match(self):
        for cat, (table, ts) in self._seeded_rules().items():
            src = kanribo.SOURCES[cat]
            self.assertEqual(src["table"], table, f"{cat}: テーブル不一致")
            self.assertIn(ts, src["ts"], f"{cat}: 時刻列不一致")

    def test_states_and_measures_in_check(self):
        for v in list(kanribo.MEASURE_LABEL) + list(kanribo.STATE_LABEL):
            self.assertIn(f"'{v}'", SCHEMA, f"{v} が015のCHECKに無い")


class TestPeriodAndExpiry(unittest.TestCase):
    def test_period_unit(self):
        d = datetime.date(2026, 7, 28)
        self.assertEqual(kanribo.period_of(rule(days=90), d), "2026-07")   # 日起算は月
        self.assertEqual(kanribo.period_of(rule(years=3), d), "2026")      # 年度起算は年度
        # 1〜3月は前年度に属する
        self.assertEqual(kanribo.period_of(rule(years=3), datetime.date(2027, 3, 31)), "2026")

    def test_fiscal_year_expiry(self):
        """保存期間3年 = 作成年度の翌年度初め(4/1)から3年 → 3年後の3/31。"""
        self.assertEqual(kanribo.expires_on(rule(years=3), "2026"),
                         datetime.date(2030, 3, 31))
        self.assertEqual(kanribo.expires_on(rule(years=1), "2026"),
                         datetime.date(2028, 3, 31))
        self.assertEqual(kanribo.expires_on(rule(years=10), "2026"),
                         datetime.date(2037, 3, 31))

    def test_day_expiry_from_month_end(self):
        """保存期間90日 = その月の末日から90日。月が閉じるので満了日はずれない。"""
        self.assertEqual(kanribo.expires_on(rule(days=90), "2026-07"),
                         datetime.date(2026, 10, 29))     # 7/31 + 90日
        self.assertEqual(kanribo.expires_on(rule(days=90), "2026-12"),
                         datetime.date(2027, 3, 31))      # 12/31 + 90日(年跨ぎ)
        self.assertEqual(kanribo.expires_on(rule(days=30), "2028-02"),
                         datetime.date(2028, 3, 30))      # 閏年の2/29 + 30日

    def test_jouyou_never_expires(self):
        self.assertIsNone(kanribo.expires_on(rule(measure="jouyou"), "2026"))
        self.assertIsNone(kanribo.expires_on(rule(days=0, years=0), "2026"))

    def test_file_name(self):
        self.assertEqual(kanribo.file_name("shuju-turns", "proj", "2026"),
                         "収受(生ログ) proj 令和8年度")
        self.assertEqual(kanribo.file_name("shuju-raw", "general", "2026-07"),
                         "収受(生データ) general 令和8年度7月")
        # 令和元年度は「元」(ringi.display_doc_no と同じ表記規則)
        self.assertEqual(kanribo.file_name("shuju-turns", "p", "2019"),
                         "収受(生ログ) p 令和元年度")

    def test_fiscal_year_of_month_period(self):
        self.assertEqual(kanribo.fiscal_year_of("2026-07"), 2026)
        self.assertEqual(kanribo.fiscal_year_of("2027-02"), 2026)  # 1〜3月は前年度


class TestSql(unittest.TestCase):
    def test_scan_groups_by_month_for_day_rules(self):
        sql = kanribo.scan_sql("shuju-raw", rule(days=90, category="shuju-raw"))
        self.assertIn("to_char(received_at, 'YYYY-MM')", sql)
        self.assertIn("FROM raw_payloads WHERE disposed_at IS NULL", sql)  # 廃棄済みは数えない
        self.assertIn("min(id)", sql)
        self.assertIn("max(id)", sql)

    def test_scan_groups_by_fiscal_year_for_year_rules(self):
        sql = kanribo.scan_sql("shuju-turns", rule(years=3))
        self.assertIn("interval '3 months'", sql)   # 4/1区切り
        self.assertIn("FROM turns GROUP BY", sql)

    def test_upsert_records_schedule_and_range(self):
        row = {"period": "2026", "key": "proj", "n": 120, "id_from": 5, "id_to": 400,
               "first_ts": "2026-04-02T00:00:00+09:00", "last_ts": "2027-03-30T00:00:00+09:00"}
        sql = kanribo.upsert_sql("shuju-turns", rule(years=3), row)
        self.assertIn("INSERT INTO record_files", sql)
        self.assertIn("'2030-03-31'", sql)          # 満了日(レコードスケジュール)
        self.assertIn("'haiki'", sql)               # 満了時の措置
        self.assertIn("'DB turns'", sql)            # 保存場所
        self.assertIn("ON CONFLICT (category, project_key, period) DO UPDATE", sql)
        self.assertIn("least(record_files.id_from", sql)
        self.assertIn("WHERE record_files.state = 'genyou'", sql)  # 廃棄済みは触らない

    def test_upsert_jouyou_has_no_expiry(self):
        row = {"period": "2026", "key": "p", "n": 1, "id_from": 1, "id_to": 1,
               "first_ts": "2026-04-02T00:00:00+09:00", "last_ts": "2026-04-02T00:00:00+09:00"}
        sql = kanribo.upsert_sql("kiroku-fact", rule(measure="jouyou"), row)
        self.assertIn("NULL, 'jouyou'", sql.replace("  ", " "))

    def test_escaping(self):
        row = {"period": "2026", "key": "it's", "n": 1, "id_from": 1, "id_to": 1,
               "first_ts": "2026-04-02T00:00:00+09:00", "last_ts": "2026-04-02T00:00:00+09:00"}
        sql = kanribo.upsert_sql("shuju-turns", rule(years=3), row)
        self.assertIn("'it''s'", sql)


class SeiriHarness(Harness):
    """nightly.seiri() 用。管理簿の有無・規程・走査結果に応えるフェイクDB。"""

    def __init__(self, rules=None, scan=None, kanribo_ok=True):
        super().__init__()
        self.rules = rules if rules is not None else []
        self.scan = scan or []
        self.mod._KANRIBO_OK = kanribo_ok

    def fake_psql(self, sql):
        if "to_regclass('public.record_files')" in sql:
            return "1"
        if "FROM retention_rules WHERE enabled" in sql:
            self.sqls.append(sql)
            return json.dumps(self.rules, ensure_ascii=False)
        if sql.startswith("SELECT json_agg(json_build_object('period'"):
            self.sqls.append(sql)
            return json.dumps(self.scan, ensure_ascii=False)
        return super().fake_psql(sql)

    def run_seiri(self):
        with self.ctx():
            return self.mod.seiri(run_id=9)


class TestSeiri(unittest.TestCase):
    def test_files_are_registered(self):
        h = SeiriHarness(
            rules=[rule(years=3, category="shuju-turns")],
            scan=[{"period": "2026", "key": "proj", "n": 12, "id_from": 1, "id_to": 12,
                   "first_ts": "2026-04-02T00:00:00+09:00",
                   "last_ts": "2026-05-02T00:00:00+09:00"}])
        self.assertEqual(h.run_seiri(), 1)
        ins = h.sqls_like("INSERT INTO record_files")
        self.assertEqual(len(ins), 1)
        self.assertIn("'収受(生ログ) proj 令和8年度'", ins[0])
        self.assertIn("'2030-03-31'", ins[0])

    def test_disabled_rules_do_nothing(self):
        h = SeiriHarness(rules=[])          # enabled が1件も無い
        self.assertEqual(h.run_seiri(), 0)
        self.assertEqual(h.sqls_like("INSERT INTO record_files"), [])

    def test_unknown_category_is_skipped(self):
        h = SeiriHarness(rules=[rule(years=1, category="bogus-cat")])
        self.assertEqual(h.run_seiri(), 0)
        self.assertEqual(h.sqls_like("INSERT INTO record_files"), [])

    def test_no_rows_no_record(self):
        h = SeiriHarness(rules=[rule(years=3)],
                         scan=[{"period": "2026", "key": "p", "n": 0, "id_from": None,
                                "id_to": None, "first_ts": None, "last_ts": None}])
        self.assertEqual(h.run_seiri(), 0)

    def test_schema_absent_is_noop(self):
        h = SeiriHarness(rules=[rule(years=3)], kanribo_ok=False)
        with mock.patch.object(h.mod, "psql", side_effect=h.fake_psql):
            self.assertEqual(h.mod.seiri(run_id=9), 0)


class TestManryou(unittest.TestCase):
    def test_manryou_sql_only_touches_expired_genyou(self):
        sql = kanribo.manryou_sql()
        self.assertIn("SET state = 'manryou'", sql)
        self.assertIn("WHERE state = 'genyou'", sql)
        self.assertIn("expires_on <= current_date", sql)
        self.assertIn("expires_on IS NOT NULL", sql)   # 常用は満了しない

    def test_pending_measure_excludes_jouyou_and_already_filed(self):
        sql = kanribo.pending_measure_sql()
        self.assertIn("state = 'manryou'", sql)
        self.assertIn("disposed_draft IS NULL", sql)   # 起票済みは二重に出さない
        self.assertIn("measure <> 'jouyou'", sql)

    def test_soon_window(self):
        self.assertIn("current_date + 30", kanribo.manryou_soon_sql())
        self.assertIn("current_date + 7", kanribo.manryou_soon_sql(7))


class ManryouHarness(SeiriHarness):
    def __init__(self, done=None, soon="0", **kw):
        super().__init__(**kw)
        self.done = done or []
        self.soon = soon

    def fake_psql(self, sql):
        if "SET state = 'manryou'" in sql:
            self.sqls.append(sql)
            return json.dumps(self.done, ensure_ascii=False)
        if "current_date +" in sql:
            self.sqls.append(sql)
            return self.soon
        return super().fake_psql(sql)

    def run_check(self):
        with self.ctx():
            return self.mod.check_manryou(run_id=9)


class TestCheckManryou(unittest.TestCase):
    def test_expired_files_are_marked(self):
        h = ManryouHarness(done=[{"id": 3, "category": "shuju-raw", "name": "収受(生データ) general 令和8年度4月",
                                  "measure": "haiki", "n_rows": 412}], soon="2")
        out = h.run_check()
        self.assertEqual([f["id"] for f in out], [3])
        self.assertTrue(h.sqls_like("SET state = 'manryou'"))

    def test_nothing_expired(self):
        h = ManryouHarness(done=[], soon="0")
        self.assertEqual(h.run_check(), [])

    def test_schema_absent_is_noop(self):
        h = ManryouHarness(kanribo_ok=False)
        with mock.patch.object(h.mod, "psql", side_effect=h.fake_psql):
            self.assertEqual(h.mod.check_manryou(run_id=9), [])


class TestTenken(unittest.TestCase):
    def test_tenken_sql_counts_inconsistencies(self):
        sql = kanribo.tenken_sql()
        for k in ("no_schedule", "overdue", "unfiled", "genyou", "manryou"):
            self.assertIn(f"'{k}'", sql)
        self.assertIn("measure <> 'jouyou' AND expires_on IS NULL", sql)  # 措置未設定

    def test_problems_empty_when_clean(self):
        self.assertEqual(kanribo.tenken_problems(
            {"files": 5, "no_schedule": 0, "overdue": 0, "unfiled": 0}), [])

    def test_problems_listed(self):
        p = kanribo.tenken_problems({"no_schedule": 1, "overdue": 2, "unfiled": 3})
        self.assertEqual(len(p), 3)
        self.assertIn("満了日超過のまま現用 2件", p)

    def test_nendo_report_sql_is_year_scoped(self):
        sql = kanribo.nendo_report_sql(2025)
        self.assertIn("fiscal_year = 2025", sql)
        self.assertIn("'haiki'", sql)
        self.assertIn("'kouetsu_machi'", sql)


class TenkenHarness(SeiriHarness):
    def __init__(self, stats=None, report=None, already_filed="0", today=None, **kw):
        super().__init__(**kw)
        self.stats = stats or {"files": 3, "genyou": 3, "manryou": 0, "haiki_zumi": 0,
                               "ikan_zumi": 0, "no_schedule": 0, "overdue": 0, "unfiled": 0}
        self.report = report or {"files": 3, "rows": 100, "haiki": 0, "ikan": 0,
                                 "drafts": 2, "miketsu": 0, "kouetsu_machi": 1}
        self.filed = already_filed
        self.today = today or datetime.date(2027, 4, 2)   # 年度替わり直後

    def fake_psql(self, sql):
        if "'no_schedule'" in sql:
            self.sqls.append(sql)
            return json.dumps(self.stats, ensure_ascii=False)
        if "'kouetsu_machi'" in sql and "fiscal_year =" in sql:
            self.sqls.append(sql)
            return json.dumps(self.report, ensure_ascii=False)
        if "FROM drafts WHERE kind='tenken'" in sql:
            self.sqls.append(sql)
            return self.filed
        return super().fake_psql(sql)

    def run_tenken(self):
        with self.ctx(), mock.patch.object(self.mod.datetime, "date",
                                           mock.Mock(today=lambda: self.today,
                                                     wraps=datetime.date)):
            return self.mod.tenken(run_id=9)


class TestTenkenRun(unittest.TestCase):
    def test_report_filed_once_at_year_turn(self):
        h = TenkenHarness()
        h.mod._DRAFTS_OK = True
        h.run_tenken()
        drafts = h.sqls_like("INSERT INTO drafts")
        self.assertEqual(len(drafts), 1)
        self.assertIn("'tenken'", drafts[0])
        self.assertIn("令和8年度", drafts[0])          # 前年度(2026年度)の報告
        self.assertIn("'general'", drafts[0])
        # 報告は決裁事項ではない: 供覧(専決)で完結し施行まで進む
        self.assertTrue(h.sqls_like("decision_class='senketsu'"))
        self.assertTrue(h.sqls_like("executed_at=now()"))

    def test_report_not_refiled(self):
        h = TenkenHarness(already_filed="1")
        h.mod._DRAFTS_OK = True
        h.run_tenken()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_report_not_filed_outside_year_turn(self):
        h = TenkenHarness(today=datetime.date(2027, 9, 1))   # 年度替わりを過ぎている
        h.mod._DRAFTS_OK = True
        h.run_tenken()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_inconsistencies_do_not_raise(self):
        h = TenkenHarness(stats={"files": 4, "genyou": 2, "manryou": 2, "haiki_zumi": 0,
                                 "ikan_zumi": 0, "no_schedule": 1, "overdue": 2,
                                 "unfiled": 2}, already_filed="1")
        h.mod._DRAFTS_OK = True
        out = h.run_tenken()
        self.assertEqual(out["overdue"], 2)


if __name__ == "__main__":
    unittest.main()
