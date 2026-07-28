"""dashboard(server.py)と nas/batch/ringi.py の二箇所保持の照合(DB・ssh不要)。

server.py は claude-config 側へ単体配布されるため ringi.py を import できず、
文書番号の表示規則(_doc_no_disp)と remand の状態遷移(SQLのCASE式)をミラーしている。
将来 ringi.py 側が変わったとき dashboard だけ取り残されないよう、ここで一致を検査する。
実行: python3 -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "nas" / "batch"))
sys.path.insert(0, str(ROOT / "terminal" / "dashboard"))

import ringi  # noqa: E402
import server  # noqa: E402


class TestDocNoDispMirror(unittest.TestCase):
    def test_matches_ringi_display_doc_no(self):
        # 令和元年度(2019年度)は「元」と書く公用文表記を含めて一致する
        for fy, seq in [(2026, 1), (2026, 12), (2019, 1), (2019, 34), (2020, 7)]:
            self.assertEqual(server._doc_no_disp(fy, seq),
                             ringi.display_doc_no(fy, seq), f"fy={fy} seq={seq}")

    def test_shelf_list_stamps_display(self):
        rows = [{"id": 1, "doc_no": "2026-0001", "fiscal_year": 2026, "seq": 1,
                 "kind": "fact", "project_key": "p", "title": "t",
                 "state": "executed", "seen_state": "pending"}]
        with mock.patch.object(server, "sql_json",
                               return_value=[dict(r) for r in rows]) as sq:
            out = server.shelf_list("all", None)
        self.assertEqual(sq.call_count, 1)
        self.assertEqual(out[0]["doc_no_disp"], "記憶第1号(令和8年度)")


class TestShelfOpTransitions(unittest.TestCase):
    """shelf_op の remand が SQL CASE に直書きする状態遷移の正は ringi.TRANSITIONS。"""

    def _remand_sql(self):
        captured = []

        def fake_run_sql(sql, **kw):
            captured.append(sql)
            return "1"

        with mock.patch.object(server, "run_sql", side_effect=fake_run_sql):
            server.shelf_op("remand", 5, "やり直してください")
        return captured[0]

    def test_executed_goes_to_reexamine(self):
        to = ringi.next_state("executed", "sashimodoshi")
        self.assertIn(f"when state='executed' then '{to}'", self._remand_sql())

    def test_approved_goes_to_rejected(self):
        to = ringi.next_state("approved", "sashimodoshi")
        self.assertIn(f"when state='approved' then '{to}'", self._remand_sql())

    def test_demo_mode_rejected(self):
        with mock.patch.object(server, "DEMO", True):
            with self.assertRaises(RuntimeError):
                server.shelf_op("kouetsu", 5, "")


if __name__ == "__main__":
    unittest.main()


class TestShelfFilters(unittest.TestCase):
    """demo判定と本番SQLの条件を揃える(見え方が環境で変わらないように)。"""

    ROWS = [
        {"id": 1, "kind": "fact", "state": "executed", "seen_state": "seen"},
        {"id": 2, "kind": "fact", "state": "executed", "seen_state": "pending"},
        {"id": 3, "kind": "skill", "state": "approved", "seen_state": "pending"},
        {"id": 4, "kind": "fact", "state": "pending_decision", "seen_state": "pending"},
        {"id": 5, "kind": "fact", "state": "reexamine", "seen_state": "remanded"},
    ]

    def _demo_ids(self, filt):
        rows = [dict(r, doc_no="2026-0001", fiscal_year=2026, seq=1) for r in self.ROWS]
        with mock.patch.object(server, "DEMO", True), \
             mock.patch.object(server, "load_json", return_value={"list": rows}):
            return [r["id"] for r in server.shelf_list(filt, None)]

    def _sql_cond(self, filt):
        with mock.patch.object(server, "DEMO", False), \
             mock.patch.object(server, "sql_json", return_value=[]) as sq:
            server.shelf_list(filt, None)
        return sq.call_args[0][0]

    def test_miketsu_is_pending_decision(self):
        self.assertEqual(self._demo_ids("miketsu"), [4])
        self.assertIn("state = 'pending_decision'", self._sql_cond("miketsu"))

    def test_pending_excludes_undecided_docs(self):
        # 未決(pending_decision)は完結していないので後閲待ちに出さない
        self.assertEqual(self._demo_ids("pending"), [2, 3])
        self.assertIn("state in ('executed','rejected','approved')",
                      self._sql_cond("pending"))

    def test_remanded_includes_reexamine(self):
        self.assertEqual(self._demo_ids("remanded"), [5])
        self.assertIn("state = 'reexamine'", self._sql_cond("remanded"))


class TestKanriboFilters(unittest.TestCase):
    """管理簿も demo判定と本番SQLの条件を揃える。"""

    ROWS = [
        {"id": 1, "category": "shuju-raw", "state": "manryou", "expires_on": "2026-07-29"},
        {"id": 2, "category": "shuju-raw", "state": "genyou", "expires_on": "2999-01-01"},
        {"id": 3, "category": "shuju-turns", "state": "genyou", "expires_on": "2000-01-01"},
        {"id": 4, "category": "kessai-doc", "state": "ikan_zumi", "expires_on": "2026-01-01"},
        {"id": 5, "category": "kiroku-fact", "state": "genyou", "expires_on": None},
    ]

    def _demo_ids(self, filt, category=""):
        with mock.patch.object(server, "DEMO", True), \
             mock.patch.object(server, "load_json", return_value={"list": self.ROWS}):
            return [r["id"] for r in server.kanribo_list(filt, category)]

    def _sql(self, filt, category=""):
        with mock.patch.object(server, "DEMO", False), \
             mock.patch.object(server, "sql_json", return_value=[]) as sq:
            server.kanribo_list(filt, category)
        return sq.call_args[0][0]

    def test_genyou(self):
        self.assertEqual(self._demo_ids("genyou"), [2, 3, 5])
        self.assertIn("state = 'genyou'", self._sql("genyou"))

    def test_manryou_includes_expired_genyou(self):
        # 満了状態の行 + 現用だが満了日を過ぎた行
        self.assertEqual(self._demo_ids("manryou"), [1, 3])
        sql = self._sql("manryou")
        self.assertIn("state = 'manryou'", sql)
        self.assertIn("expires_on <= current_date", sql)

    def test_sumi(self):
        self.assertEqual(self._demo_ids("sumi"), [4])
        self.assertIn("state in ('haiki_zumi','ikan_zumi')", self._sql("sumi"))

    def test_category_filter(self):
        self.assertEqual(self._demo_ids("all", "shuju-raw"), [1, 2])
        self.assertIn("category = ", self._sql("all", "shuju-raw"))

    def test_jouyou_never_counted_as_expired(self):
        # 満了日NULL(常用)は満了に出ない
        self.assertNotIn(5, self._demo_ids("manryou"))
