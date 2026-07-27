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
