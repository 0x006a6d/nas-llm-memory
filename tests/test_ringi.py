"""ringi.py の純ロジック検査(DB不要)。実行: python3 -m unittest discover tests"""
import datetime
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nas" / "batch"))

import ringi  # noqa: E402


class TestFiscalYear(unittest.TestCase):
    def test_boundary(self):
        self.assertEqual(ringi.fiscal_year(datetime.date(2026, 3, 31)), 2025)
        self.assertEqual(ringi.fiscal_year(datetime.date(2026, 4, 1)), 2026)
        self.assertEqual(ringi.fiscal_year(datetime.datetime(2027, 1, 15, 3, 0)), 2026)

    def test_display_doc_no(self):
        # 令和元年=2019年度
        self.assertEqual(ringi.display_doc_no(2026, 12), "記憶第12号(令和8年度)")
        self.assertEqual(ringi.display_doc_no(2019, 1), "記憶第1号(令和元年度)")


class TestTransitions(unittest.TestCase):
    def test_senketsu_path(self):
        """軽易案件: 起案→審査OK(専決)→施行→後閲差し戻し→再審理完了"""
        s = "pending_review"
        s = ringi.next_state(s, "shinsa_ok")
        self.assertEqual(s, "approved")
        s = ringi.next_state(s, "shiko")
        self.assertEqual(s, "executed")
        s = ringi.next_state(s, "sashimodoshi")
        self.assertEqual(s, "reexamine")
        s = ringi.next_state(s, "saishinri")
        self.assertEqual(s, "executed")

    def test_joshin_path(self):
        """重要案件: 上申→決裁→施行"""
        s = ringi.next_state("pending_review", "joshin")
        self.assertEqual(s, "pending_decision")
        self.assertEqual(ringi.next_state(s, "kessai_ok"), "approved")
        self.assertEqual(ringi.next_state(s, "sashimodoshi"), "remanded_to_reviewer")
        self.assertEqual(ringi.next_state(s, "hiketsu"), "rejected")

    def test_hosei_loop(self):
        """補正往復: 審査差し戻し→補正→再回議"""
        s = ringi.next_state("pending_review", "sashimodoshi")
        self.assertEqual(s, "remanded_to_drafter")
        self.assertEqual(ringi.next_state(s, "hosei"), "pending_review")
        self.assertEqual(ringi.next_state(s, "hiketsu"), "rejected")

    def test_skill_kouetsu_gate(self):
        """skill後閲待ち(approved停止)への差し戻しは廃案"""
        self.assertEqual(ringi.next_state("approved", "sashimodoshi"), "rejected")

    def test_invalid(self):
        for state, action in [("executed", "shiko"), ("rejected", "hosei"),
                              ("pending_review", "kessai_ok"), ("approved", "kian")]:
            with self.assertRaises(ValueError):
                ringi.next_state(state, action)

    def test_states_match_schema(self):
        """TRANSITIONSに現れる状態・actionが012のCHECK句の集合と一致する"""
        sql = (Path(__file__).resolve().parent.parent
               / "nas" / "ingest" / "schema" / "012_ringi.sql").read_text(encoding="utf-8")
        states = set()
        for frm, act in ringi.TRANSITIONS:
            states.add(frm)
            states.add(ringi.TRANSITIONS[(frm, act)])
        for s in states:
            self.assertIn(f"'{s}'", sql, f"state {s} が012_ringi.sqlのCHECKに無い")
        # TRANSITIONSのaction + 状態遷移を伴わない記帳(起案・後閲・skill移動の中断判定用)
        actions = {a for _, a in ringi.TRANSITIONS} | {"kian", "kouetsu", "skill_mv"}
        for a in actions:
            self.assertIn(f"'{a}'", sql, f"action {a} が012_ringi.sqlのCHECKに無い")


class TestSqlBuilders(unittest.TestCase):
    def test_insert_draft_numbering(self):
        sql = ringi.insert_draft_sql(
            kind="fact", project_key="general", title="t", proposal="p",
            payload={"candidates": []}, created_by="run-1", fy=2026)
        # 採番: 同一文内の集約 + 桁あふれで切り詰めないlpad
        self.assertIn("coalesce(max(seq), 0) + 1", sql)
        self.assertIn("WHERE fiscal_year = 2026", sql)
        self.assertIn("greatest(4, length(s.n::text))", sql)
        self.assertIn("RETURNING id, doc_no", sql)
        self.assertIn("'pending_review'", sql)
        self.assertIn("NULL", sql)  # related_doc

    def test_escaping(self):
        sql = ringi.insert_draft_sql(
            kind="skill", project_key="k", title="it's", proposal="a'b",
            payload={"name": "x'y"}, created_by="run-2", fy=2026)
        self.assertIn("'it''s'", sql)
        self.assertIn("'a''b'", sql)
        self.assertIn('x\'\'y', sql)  # jsonb内もエスケープ
        with self.assertRaises(ValueError):
            ringi.insert_draft_sql(kind="bogus", project_key="k", title="t",
                                   proposal="p", payload={}, created_by="r", fy=2026)

    def test_payload_keeps_japanese(self):
        sql = ringi.insert_draft_sql(
            kind="fact", project_key="k", title="t", proposal="p",
            payload={"c": "日本語"}, created_by="r", fy=2026)
        self.assertIn("日本語", sql)  # ensure_ascii=False(\\uXXXXにしない)

    def test_transition_sql_decision_class(self):
        sql = ringi.transition_sql(5, "pending_review", "shinsa_ok")
        self.assertIn("state='approved'", sql)
        self.assertIn("decision_class='senketsu'", sql)
        self.assertIn("decided_at=now()", sql)
        self.assertIn("AND state = 'pending_review'", sql)
        self.assertIn("RETURNING id", sql)
        sql = ringi.transition_sql(5, "pending_decision", "kessai_ok")
        self.assertIn("decision_class='bucho'", sql)

    def test_transition_sql_kouetsu_remand(self):
        sql = ringi.transition_sql(7, "executed", "sashimodoshi")
        self.assertIn("state='reexamine'", sql)
        self.assertIn("seen_state='remanded'", sql)
        sql = ringi.transition_sql(7, "reexamine", "saishinri")
        self.assertIn("state='executed'", sql)
        self.assertIn("seen_state='seen'", sql)

    def test_transition_sql_shiko(self):
        sql = ringi.transition_sql(9, "approved", "shiko")
        self.assertIn("executed_at=now()", sql)

    def test_log_sql(self):
        sql = ringi.log_sql(3, "shinsa:claude-sonnet-5", "sashimodoshi", "run-4",
                            memo="端末名が無い", payload=[{"action": "hosei"}])
        self.assertIn("'shinsa:claude-sonnet-5'", sql)
        self.assertIn("'端末名が無い'", sql)
        self.assertIn("::jsonb", sql)
        sql = ringi.log_sql(3, "human", "kouetsu", "dashboard")
        self.assertIn("NULL, NULL", sql)

    def test_link_facts_sql(self):
        sql = ringi.link_facts_sql(11, [101, 102])
        self.assertIn("ARRAY[101,102]::bigint[]", sql)
        self.assertIn("ON CONFLICT DO NOTHING", sql)


class TestConfig(unittest.TestCase):
    def test_model_for_roles(self):
        cfg = {"model": "m0", "roles": {"kian": "m1", "shinsa": ""}}
        self.assertEqual(ringi.model_for(cfg, "kian"), "m1")
        self.assertEqual(ringi.model_for(cfg, "shinsa"), "m0")  # 空はフォールバック
        self.assertEqual(ringi.model_for(cfg, "kessai"), "m0")  # 未定義もフォールバック

    def test_model_for_backward_compat(self):
        # 旧形式({"model": ...} のみ)・空configでも壊れない
        self.assertEqual(ringi.model_for({"model": "m0"}, "kian"), "m0")
        self.assertEqual(ringi.model_for({}, "kian"), "")

    def test_ringi_settings_defaults(self):
        s = ringi.ringi_settings({})
        self.assertFalse(s["enabled"])
        self.assertEqual(s["max_hosei_rounds"], 2)
        self.assertEqual(s["max_kessai_rounds"], 1)
        self.assertFalse(s["skill_auto_execute"])

    def test_ringi_settings_override_and_unknown(self):
        s = ringi.ringi_settings({"ringi": {"enabled": True, "max_hosei_rounds": 3,
                                            "unknown_key": 1}})
        self.assertTrue(s["enabled"])
        self.assertEqual(s["max_hosei_rounds"], 3)
        self.assertNotIn("unknown_key", s)

    def test_example_config_parses(self):
        """config.example.json が実装の解釈と整合している"""
        cfg = json.loads((Path(__file__).resolve().parent.parent
                          / "nas" / "batch" / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(ringi.model_for(cfg, "kessai"), cfg["roles"]["kessai"])
        s = ringi.ringi_settings(cfg)
        self.assertFalse(s["enabled"])  # 既定は従来動作


class TestProposal(unittest.TestCase):
    def test_build_fact_proposal(self):
        text = ringi.build_proposal(
            "fact",
            ["新規登載 2件(別記第1)", "既存事実の置換 1件(別記第2)"],
            [("新規登載", ["WSLでは…", "MacBookでは…"]),
             ("置換", ["[41] 旧内容 → 新内容"])])
        self.assertIn("事実層(facts)に登載してよろしいか。", text)
        self.assertIn("記", text.splitlines())
        self.assertIn("1. 新規登載 2件(別記第1)", text)
        self.assertIn("別記第1(新規登載)", text)
        self.assertIn("別記第2(置換)", text)
        self.assertIn(" 1. [41] 旧内容 → 新内容", text)

    def test_titles(self):
        self.assertEqual(ringi.build_title("fact", project="vitals"),
                         "プロジェクト vitals に係る事実の登載について(伺い)")
        self.assertIn("スキル「raster-qa」", ringi.build_title("skill", name="raster-qa"))
        self.assertIn("2026-0012", ringi.build_title("saishinri", doc_no="2026-0012"))

    def test_appendix_as_preformatted_text(self):
        # 別記に整形済みブロック(diff等)を文字列でそのまま載せられる
        text = ringi.build_proposal("index", ["差分は別記第1のとおり"],
                                    [("差分", "--- 現行\n+++ 改定案\n-旧\n+新")])
        self.assertIn("別記第1(差分)", text)
        self.assertIn("\n-旧\n+新", text)
        self.assertNotIn(" 1. ---", text)  # 番号付けしない

    def test_ask_lines_cover_all_kinds(self):
        for kind in ("fact", "index", "skill", "saishinri"):
            self.assertIn("よろしいか", ringi.build_proposal(kind, ["x"]))


if __name__ == "__main__":
    unittest.main()
