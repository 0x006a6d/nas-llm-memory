"""廃棄伺い(法8条2項)の検査(DB・claude CLI不要)。

廃棄は取り消せないので、消し方の条件(範囲・除外・バックアップ)を重点的に固定する。
実行: python3 -m unittest discover tests
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "nas" / "batch"))

import kanribo  # noqa: E402

from test_ringi_flow import Harness  # noqa: E402


def rec(category="shuju-turns", **kw):
    f = {"id": 7, "category": category, "project_key": "proj",
         "name": "収受(生ログ) proj 令和8年度", "period": "2026",
         "expires_on": "2030-03-31", "measure": "haiki",
         "location": "DB turns", "n_rows": 120, "id_from": 10, "id_to": 400}
    f.update(kw)
    return f


class TestDisposeSql(unittest.TestCase):
    def test_turns_keeps_rows_backing_current_facts(self):
        """現用factsの根拠(provenance)になっているturnsは廃棄しない。"""
        sql = kanribo.dispose_sql(rec(), draft_id=5)
        self.assertIn("DELETE FROM turns", sql)
        self.assertIn("id BETWEEN 10 AND 400", sql)
        self.assertIn("t.project_key = 'proj'", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("current_facts", sql)
        self.assertIn("t.id = ANY(cf.provenance)", sql)

    def test_raw_disposes_body_only(self):
        """受信生データは本文だけ廃棄し、行と受信日時は証跡として残す。"""
        sql = kanribo.dispose_sql(rec(category="shuju-raw", location="DB raw_payloads"), 5)
        self.assertIn("UPDATE raw_payloads SET payload = '{}'::jsonb", sql)
        self.assertIn("disposed_at = now()", sql)
        self.assertIn("disposed_draft = 5", sql)
        self.assertIn("disposed_at IS NULL", sql)     # 二重廃棄しない
        self.assertNotIn("DELETE", sql)

    def test_batch_runs_keeps_watermark_holder(self):
        """watermarkを持つ最新の成功runは消さない(消すと収集が巻き戻る)。"""
        sql = kanribo.dispose_sql(rec(category="unyou-run", location="DB batch_runs"), 5)
        self.assertIn("DELETE FROM batch_runs", sql)
        self.assertIn("max(watermark_turn_id)", sql)
        self.assertIn("b.finished_at IS NOT NULL", sql)  # 実行中のrunは消さない

    def test_generic_delete_is_range_and_key_bound(self):
        sql = kanribo.dispose_sql(rec(category="renraku-msg", location="DB messages"), 5)
        self.assertIn("DELETE FROM messages", sql)
        self.assertIn("BETWEEN 10 AND 400", sql)
        self.assertIn("'proj'", sql)

    def test_survivors_only_for_turns(self):
        self.assertIn("current_facts", kanribo.survivors_sql(rec()))
        self.assertEqual(kanribo.survivors_sql(rec(category="shuju-raw")), "")

    def test_haiki_items_state_scope(self):
        items = kanribo.haiki_items(rec(), survivors=3)
        joined = " / ".join(items)
        self.assertIn("120件", joined)
        self.assertIn("id 10〜400", joined)
        self.assertIn("2030-03-31", joined)
        self.assertIn("3件は現用の事実の根拠", joined)


class HaikiHarness(Harness):
    """廃棄伺い(ringi_haiki / execute_haiki_doc)用のフェイクDB。"""

    def __init__(self, files=None, rules=None, state="manryou", fresh_backup=True,
                 dispose_count=118):
        super().__init__()
        self.mod._KANRIBO_OK = True
        self.files = files if files is not None else [rec()]
        self.rules = rules if rules is not None else [
            {"category": "shuju-turns", "measure": "haiki", "gate": "kouetsu",
             "retention_days": 0, "retention_years": 3}]
        self.state = state
        self.dispose_count = dispose_count
        self.fresh = fresh_backup

    def fake_psql(self, sql):
        if "to_regclass('public.record_files')" in sql:
            return "1"
        if "FROM retention_rules WHERE enabled" in sql:
            return json.dumps(self.rules, ensure_ascii=False)
        if "FROM record_files WHERE state = 'manryou'" in sql:
            self.sqls.append(sql)
            return json.dumps(self.files, ensure_ascii=False)
        if "FROM record_files WHERE id =" in sql:
            self.sqls.append(sql)
            return json.dumps([dict(self.files[0], state=self.state)], ensure_ascii=False)
        if "EXISTS (SELECT 1 FROM current_facts" in sql:   # survivors
            self.sqls.append(sql)
            return "2"
        if sql.startswith("WITH d AS"):                     # 廃棄の実行
            self.sqls.append(sql)
            return str(self.dispose_count)
        return super().fake_psql(sql)

    def run_haiki(self):
        # Harness.ctx() は contextmanager なので、追加のpatchは入れ子にする
        with self.ctx(), mock.patch.object(self.mod, "backup_is_fresh",
                                           return_value=self.fresh):
            return self.mod.ringi_haiki(run_id=9)


class TestRingiHaiki(unittest.TestCase):
    def _scripts(self, h, shinsa="joshin", kessai="approve"):
        h.scripts["shinsa-haiki"] = [{"action": shinsa, "memo": "満了確認"}]
        h.scripts["kessai-haiki"] = [{"action": kessai, "memo": "廃棄可"}]

    def test_kouetsu_gate_stops_at_approved(self):
        """後閲印が条件の分類は、決裁で止めて実際には消さない。"""
        h = HaikiHarness()
        self._scripts(h)
        self.assertEqual(h.run_haiki(), 1)
        self.assertTrue(h.sqls_like("decision_class='bucho'"))
        self.assertEqual(h.sqls_like("WITH d AS"), [])          # 消していない
        self.assertEqual(h.sqls_like("executed_at=now()"), [])  # 施行していない
        drafts = h.sqls_like("INSERT INTO drafts")
        self.assertIn("'haiki'", drafts[0])
        self.assertIn("廃棄一覧", drafts[0])

    def test_sokujiko_gate_executes(self):
        """決裁で施行する分類は、その場で廃棄まで進む。"""
        h = HaikiHarness(rules=[{"category": "shuju-turns", "measure": "haiki",
                                 "gate": "sokujiko", "retention_days": 0,
                                 "retention_years": 3}])
        self._scripts(h)
        h.run_haiki()
        self.assertTrue(h.sqls_like("DELETE FROM turns"))
        self.assertTrue(h.sqls_like("executed_at=now()"))
        self.assertIn("state = 'haiki_zumi'", "\n".join(h.sqls_like("UPDATE record_files")))

    def test_shinsa_hiketsu_keeps_data(self):
        h = HaikiHarness()
        self._scripts(h, shinsa="hiketsu")
        h.run_haiki()
        self.assertTrue(h.sqls_like("state='rejected'"))
        self.assertEqual(h.sqls_like("WITH d AS"), [])
        self.assertEqual([a for a in h.asks if a[0].startswith("kessai")], [])

    def test_kessai_hiketsu_keeps_data(self):
        h = HaikiHarness(rules=[{"category": "shuju-turns", "measure": "haiki",
                                 "gate": "sokujiko", "retention_days": 0,
                                 "retention_years": 3}])
        self._scripts(h, kessai="hiketsu")
        h.run_haiki()
        self.assertTrue(h.sqls_like("state='rejected'"))
        self.assertEqual(h.sqls_like("WITH d AS"), [])

    def test_no_backup_blocks_execution(self):
        """当日のバックアップが無ければ施行しない(廃棄は取り消せない)。"""
        h = HaikiHarness(fresh_backup=False,
                         rules=[{"category": "shuju-turns", "measure": "haiki",
                                 "gate": "sokujiko", "retention_days": 0,
                                 "retention_years": 3}])
        self._scripts(h)
        h.run_haiki()   # 件単位で握りつぶす: runは落とさない
        self.assertEqual(h.sqls_like("DELETE FROM turns"), [])

    def test_ikan_files_are_not_disposed(self):
        h = HaikiHarness(files=[rec(measure="ikan")])
        self.assertEqual(h.run_haiki(), 0)
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_disabled_category_is_skipped(self):
        h = HaikiHarness(rules=[])   # 規程が無効
        self.assertEqual(h.run_haiki(), 0)
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_already_disposed_file_only_updates_state(self):
        h = HaikiHarness(state="haiki_zumi",
                         rules=[{"category": "shuju-turns", "measure": "haiki",
                                 "gate": "sokujiko", "retention_days": 0,
                                 "retention_years": 3}])
        self._scripts(h)
        h.run_haiki()
        self.assertEqual(h.sqls_like("WITH d AS"), [])          # 二重に消さない
        self.assertTrue(h.sqls_like("executed_at=now()"))       # 状態だけ追いつかせる


if __name__ == "__main__":
    unittest.main()
