"""後閲キュー処理(process_remands / 再審理saishinri)の検査(DB・claude CLI不要)。

実行: python3 -m unittest discover tests
"""
import json
import unittest
from unittest import mock

from test_ringi_flow import Harness


class RemandHarness(Harness):
    def __init__(self, config=None):
        super().__init__(config=config)
        self.skills_queue = []
        self.remands_queue = []
        self.linked = []
        self.logs = []
        self.retired = []

    def fake_psql(self, sql):
        if "kind='skill' AND state='approved' AND seen_state='seen'" in sql:
            self.sqls.append(sql)
            return json.dumps(self.skills_queue) if self.skills_queue else ""
        if "FROM drafts WHERE state='reexamine'" in sql:
            self.sqls.append(sql)
            return json.dumps(self.remands_queue) if self.remands_queue else ""
        if "FROM draft_facts df JOIN facts" in sql:
            self.sqls.append(sql)
            return json.dumps(self.linked) if self.linked else ""
        if "FROM draft_log WHERE draft_id" in sql:
            self.sqls.append(sql)
            return json.dumps(self.logs) if self.logs else ""
        if sql.startswith("UPDATE facts SET retired_by"):
            self.sqls.append(sql)
            self.retired.append(sql)
            return "1"
        if sql.startswith("INSERT INTO facts"):
            self.sqls.append(sql)
            self.fact_seq += 1
            return str(self.fact_seq)
        if sql.startswith("SELECT project_key FROM facts"):
            self.sqls.append(sql)
            return "proj"
        return super().fake_psql(sql)

    def run_remands(self):
        with self.ctx():
            return self.mod.process_remands(run_id=9)

    def run_skill_queue(self):
        with self.ctx():
            return self.mod.process_skill_queue(run_id=9)


def remand_row(doc_id=50, kind="fact"):
    return {"id": doc_id, "doc_no": "2026-0050", "kind": kind, "project_key": "proj",
            "title": "t", "proposal": "伺い文…"}


HUMAN_LOGS = [
    {"actor": "kian:mk", "action": "kian", "memo": None},
    {"actor": "human", "action": "sashimodoshi", "memo": "この事実は誤り。撤回してほしい"},
]


class TestSkillExecuteQueue(unittest.TestCase):
    def test_seen_skill_executed(self):
        # 人間が決裁(approved+seen)したskill文書を翌晩施行する
        h = RemandHarness()
        h.skills_queue = [{"id": 7, "name": "raster-qa"}]
        with mock.patch.object(h.mod, "execute_skill_doc") as ex:
            h.run_skill_queue()
        ex.assert_called_once_with(7, "raster-qa", 9)

    def test_execute_failure_swallowed(self):
        h = RemandHarness()
        h.skills_queue = [{"id": 7, "name": "a"}, {"id": 8, "name": "b"}]
        with mock.patch.object(h.mod, "execute_skill_doc",
                               side_effect=[RuntimeError("x"), None]) as ex:
            h.run_skill_queue()  # 例外が伝播しない
        self.assertEqual(ex.call_count, 2)


class TestSaishinri(unittest.TestCase):
    def test_retire_action(self):
        h = RemandHarness()
        h.remands_queue = [remand_row()]
        h.linked = [{"id": 5, "content": "誤った事実", "status": "verified", "retired": False}]
        h.logs = HUMAN_LOGS
        h.scripts["saishinri"] = [{"actions": [{"op": "retire", "fact_id": 5}],
                                   "memo": "メモのとおり撤回する"}]
        touched = h.run_remands()
        self.assertEqual(touched, {"proj"})
        self.assertTrue(h.retired)
        self.assertIn("id = 5", h.retired[0])
        # saishinri文書が起票・施行され、原文書がexecuted+seenに戻る
        drafts = h.sqls_like("INSERT INTO drafts")
        self.assertEqual(len(drafts), 1)
        self.assertIn("'saishinri'", drafts[0])
        self.assertTrue(h.sqls_like("state='executed', seen_state='seen'"))
        # 決裁者に人間メモが渡っている
        self.assertIn("この事実は誤り", h.asks[0][2])
        self.assertEqual(h.asks[0][1], "mo")

    def test_replace_action_links_new_fact(self):
        h = RemandHarness()
        h.remands_queue = [remand_row()]
        h.linked = [{"id": 5, "content": "古い", "status": "verified", "retired": False}]
        h.logs = HUMAN_LOGS
        h.scripts["saishinri"] = [{"actions": [{"op": "replace", "fact_id": 5,
                                                "content": "直した事実"}],
                                   "memo": "置換"}]
        touched = h.run_remands()
        self.assertEqual(touched, {"proj"})
        ins = [s for s in h.sqls if s.startswith("INSERT INTO facts")]
        self.assertEqual(len(ins), 1)
        self.assertIn("'直した事実'", ins[0])
        self.assertTrue(h.sqls_like("INSERT INTO draft_facts"))

    def test_unlinked_fact_id_ignored(self):
        h = RemandHarness()
        h.remands_queue = [remand_row()]
        h.linked = [{"id": 5, "content": "x", "status": "verified", "retired": False}]
        h.logs = HUMAN_LOGS
        h.scripts["saishinri"] = [{"actions": [{"op": "retire", "fact_id": 999}],
                                   "memo": "無関係のfactを消そうとする"}]
        touched = h.run_remands()
        self.assertEqual(touched, set())
        self.assertEqual(h.retired, [])
        self.assertIn("是正なし", "\n".join(h.sqls_like("INSERT INTO drafts")))

    def test_regenerate_for_index_doc(self):
        h = RemandHarness()
        h.remands_queue = [remand_row(kind="index")]
        h.logs = HUMAN_LOGS
        h.scripts["saishinri"] = [{"actions": [{"op": "regenerate"}], "memo": "再生成"}]
        touched = h.run_remands()
        self.assertEqual(touched, {"proj"})

    def test_malformed_response_still_closes_remand(self):
        h = RemandHarness()
        h.remands_queue = [remand_row()]
        h.logs = HUMAN_LOGS
        h.scripts["saishinri"] = [["not a dict"]]
        touched = h.run_remands()
        self.assertEqual(touched, set())
        # 原文書は閉じる(永久に再審理キューへ残らない)。人間は再度差し戻せる
        self.assertTrue(h.sqls_like("state='executed', seen_state='seen'"))
        self.assertEqual(len(h.sqls_like("INSERT INTO drafts")), 1)


if __name__ == "__main__":
    unittest.main()
