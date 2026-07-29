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


class TestKanriboCounts(unittest.TestCase):
    def test_manryou_count_matches_list_condition(self):
        """概況の満了件数と管理簿一覧のフィルタが同じ条件であること。"""
        with mock.patch.object(server, "DEMO", False), \
             mock.patch.object(server, "run_sql", return_value="3|1|0") as rs:
            out = server.kanribo_counts()
        sql = rs.call_args[0][0]
        self.assertIn("state='manryou'", sql)                 # 満了に進んだ分も数える
        self.assertIn("expires_on <= current_date", sql)      # まだ現用の満了分も数える
        self.assertEqual(out, {"genyou": 3, "manryou": 1, "sumi": 0})

    def test_counts_none_when_schema_absent(self):
        with mock.patch.object(server, "DEMO", False), \
             mock.patch.object(server, "run_sql", side_effect=RuntimeError("no table")):
            self.assertIsNone(server.kanribo_counts())


def _norm(sql):
    """SQLの比較用に空白を潰す(条件の書き方が改行で割れても比較できるように)。"""
    return " ".join(sql.split())


class TestShelfCounts(unittest.TestCase):
    """停留所マップの件数。後閲待ちの条件は shelf_pending_count() が正。"""

    ROWS = [
        {"id": 1, "state": "executed", "seen_state": "seen"},
        {"id": 2, "state": "executed", "seen_state": "pending"},
        {"id": 3, "state": "approved", "seen_state": "pending"},
        {"id": 4, "state": "pending_decision", "seen_state": "pending"},
        {"id": 5, "state": "reexamine", "seen_state": "remanded"},
    ]

    def _sql(self, ret):
        with mock.patch.object(server, "DEMO", False), \
             mock.patch.object(server, "run_sql", return_value=ret) as rs:
            out = server.shelf_counts()
        return _norm(rs.call_args[0][0]), out

    def test_all_states_counted(self):
        sql, out = self._sql("|".join(["0"] * 10))
        for s in server.SHELF_STATES:
            self.assertIn(f"count(*) filter (where state='{s}')", sql)
        self.assertEqual(set(out), set(server.SHELF_STATES) | {"kouetsu_pending", "total"})

    def test_kouetsu_condition_matches_pending_count(self):
        with mock.patch.object(server, "DEMO", False), \
             mock.patch.object(server, "run_sql", return_value="0") as rs:
            server.shelf_pending_count()
        cond = _norm(rs.call_args[0][0]).split(" where ", 1)[1].rstrip(";")
        self.assertIn(cond, self._sql("|".join(["0"] * 10))[0])

    def test_values_map_in_order(self):
        _, out = self._sql("1|2|3|4|5|6|7|8|9|10")
        self.assertEqual(out["pending_review"], 1)
        self.assertEqual(out["reexamine"], 8)
        self.assertEqual(out["kouetsu_pending"], 9)
        self.assertEqual(out["total"], 10)

    def test_demo_counts_match_rows(self):
        with mock.patch.object(server, "DEMO", True), \
             mock.patch.object(server, "load_json", return_value={"list": self.ROWS}):
            out = server.shelf_counts()
        self.assertEqual(out["executed"], 2)
        self.assertEqual(out["approved"], 1)
        self.assertEqual(out["pending_decision"], 1)
        self.assertEqual(out["reexamine"], 1)
        self.assertEqual(out["pending_review"], 0)
        # 後閲待ちは shelf_list("pending") と同じ集合(id 2,3)
        self.assertEqual(out["kouetsu_pending"], 2)
        self.assertEqual(out["total"], 5)

    def test_counts_none_when_schema_absent(self):
        with mock.patch.object(server, "DEMO", False), \
             mock.patch.object(server, "run_sql", side_effect=RuntimeError("no table")):
            self.assertIsNone(server.shelf_counts())


class TestShelfListState(unittest.TestCase):
    """state指定も demo判定と本番SQLの条件を揃える。"""

    ROWS = [
        {"id": 1, "kind": "fact", "state": "executed", "seen_state": "seen"},
        {"id": 2, "kind": "fact", "state": "executed", "seen_state": "pending"},
        {"id": 3, "kind": "skill", "state": "approved", "seen_state": "pending"},
        {"id": 4, "kind": "fact", "state": "remanded_to_drafter", "seen_state": "seen"},
    ]

    def _demo_ids(self, filt, state, kind=None):
        rows = [dict(r, doc_no="2026-0001", fiscal_year=2026, seq=1) for r in self.ROWS]
        with mock.patch.object(server, "DEMO", True), \
             mock.patch.object(server, "load_json", return_value={"list": rows}):
            return [r["id"] for r in server.shelf_list(filt, kind, state)]

    def _sql(self, filt, state, kind=None):
        with mock.patch.object(server, "DEMO", False), \
             mock.patch.object(server, "sql_json", return_value=[]) as sq:
            server.shelf_list(filt, kind, state)
        return sq.call_args[0][0]

    def test_state_filters_demo_and_sql(self):
        self.assertEqual(self._demo_ids("all", "executed"), [1, 2])
        self.assertIn("and state = $dq$executed$dq$", self._sql("all", "executed"))

    def test_state_stacks_on_filt(self):
        self.assertEqual(self._demo_ids("pending", "executed"), [2])
        sql = self._sql("pending", "executed")
        self.assertIn("seen_state = 'pending'", sql)
        self.assertIn("and state = $dq$executed$dq$", sql)

    def test_state_stacks_on_kind(self):
        self.assertEqual(self._demo_ids("all", "approved", "skill"), [3])
        sql = self._sql("all", "approved", "skill")
        self.assertIn("kind = $dq$skill$dq$", sql)
        self.assertIn("and state = $dq$approved$dq$", sql)

    def test_no_state_keeps_existing_behaviour(self):
        self.assertEqual(self._demo_ids("all", None), [1, 2, 3, 4])
        self.assertEqual(self._sql("all", ""), self._sql("all", None))
        self.assertNotIn("and state = $", self._sql("all", None))

    def test_unknown_state_rejected(self):
        for bad in ("bogus", "executed'; drop", "seen"):
            with self.assertRaises(ValueError):
                self._demo_ids("all", bad)
            with self.assertRaises(ValueError):
                self._sql("all", bad)

    def test_every_state_accepted(self):
        for s in server.SHELF_STATES:
            self.assertIn(f"and state = $dq${s}$dq$", self._sql("all", s))


class TestShelfReplay(unittest.TestCase):
    """夜間便リプレイ。便の識別は draft_log.created_by='run-N'(便番号列は無い)。"""

    def _demo(self, run=None):
        with mock.patch.object(server, "DEMO", True):
            return server.shelf_replay(run)

    def test_demo_runs_newest_first(self):
        out = self._demo()
        self.assertEqual([r["run_id"] for r in out["runs"]], [11, 10])
        self.assertEqual(out["run_id"], 11)
        self.assertEqual([r["n"] for r in out["runs"]], [3, 10])

    def test_demo_events_are_log_ordered_and_stamped(self):
        out = self._demo(10)
        self.assertEqual(out["run_id"], 10)
        self.assertEqual([e["id"] for e in out["events"]], [1, 2, 3, 4, 6, 7, 8, 9, 10, 11])
        self.assertTrue(all(e.get("doc_no_disp") for e in out["events"]))
        self.assertEqual(out["events"][0]["doc_no_disp"], "記憶第1号(令和8年度)")
        self.assertEqual(out["events"][0]["draft_id"], 1)
        self.assertEqual(out["events"][0]["action"], "kian")

    def test_demo_run_selection(self):
        self.assertEqual([e["draft_id"] for e in self._demo(11)["events"]], [4, 4, 4])

    def test_prod_sql(self):
        runs = [{"run_id": 11, "n": 3, "t0": "t", "t1": "t"},
                {"run_id": 10, "n": 10, "t0": "t", "t1": "t"}]
        events = [{"id": 40, "draft_id": 4, "actor": "kian:m", "action": "kian",
                   "memo": None, "created_at": "t", "doc_no": "2026-0004",
                   "fiscal_year": 2026, "seq": 4, "kind": "fact", "title": "t",
                   "project_key": "p"}]
        with mock.patch.object(server, "DEMO", False), \
             mock.patch.object(server, "sql_json", side_effect=[runs, events]) as sq:
            out = server.shelf_replay()
        runs_sql, ev_sql = _norm(sq.call_args_list[0][0][0]), _norm(sq.call_args_list[1][0][0])
        self.assertIn("created_by ~ '^run-[0-9]+$'", runs_sql)
        self.assertIn("group by 1 order by 1 desc limit 10", runs_sql)
        self.assertIn("join drafts d on d.id = l.draft_id", ev_sql)
        self.assertIn("where l.created_by = $dq$run-11$dq$", ev_sql)
        self.assertIn("order by l.id", ev_sql)
        self.assertEqual(out["run_id"], 11)
        self.assertEqual(out["events"][0]["doc_no_disp"], "記憶第4号(令和8年度)")

    def test_prod_no_runs(self):
        with mock.patch.object(server, "DEMO", False), \
             mock.patch.object(server, "sql_json", return_value=[]) as sq:
            self.assertEqual(server.shelf_replay(),
                             {"runs": [], "run_id": None, "events": []})
        self.assertEqual(sq.call_count, 1)

    def test_invalid_run_rejected(self):
        for bad in ("abc", "10; drop", 0, -1):
            with self.assertRaises(ValueError):
                self._demo(bad)
        # 本番分岐でも SQL 実行前に拒否される
        with mock.patch.object(server, "DEMO", False), \
             mock.patch.object(server, "sql_json") as sq:
            for bad in ("abc", "10; drop", 0, -1):
                with self.assertRaises(ValueError):
                    server.shelf_replay(bad)
        self.assertEqual(sq.call_count, 0)
