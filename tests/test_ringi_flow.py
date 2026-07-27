"""起案・決裁ワークフロー(facts経路)の検査(DB・claude CLI不要)。

psql/ask_claude/shortlist_factsをモックし、審査・補正ループ・上申・決裁・施行の
分岐と、drafts/draft_log/draft_factsへ流れるSQLを検証する。
実行: python3 -m unittest discover tests
"""
import json
import unittest
from unittest import mock

from test_nightly_config import load_nightly


def cand(content, scope="project", status="verified"):
    return {"content": content, "status": status, "provenance": [1],
            "confidence": 0.9, "scope": scope}


class Harness:
    """nightlyモジュールにフェイクDB/LLMを差し込む。"""

    def __init__(self, config=None, shortlist=None):
        cfg = {"model": "m0",
               "roles": {"kian": "mk", "shinsa": "ms", "kessai": "mo"},
               "ringi": {"enabled": True}}
        if config:
            for k, v in config.items():
                if isinstance(v, dict):
                    cfg.setdefault(k, {}).update(v)
                else:
                    cfg[k] = v
        self.mod = load_nightly(cfg)
        self.mod._EDGES_OK = False      # fact_edges系SQLを省いて検査を単純に
        self.mod._PGROONGA_OK = True
        self.mod._DRAFTS_OK = True
        self.sqls = []
        self.draft_seq = 0
        self.fact_seq = 100
        self.shortlist = shortlist or []
        self.scripts = {}   # label種別 -> 応答(list)の列
        self.asks = []      # (label, model, prompt)

    def fake_psql(self, sql):
        self.sqls.append(sql)
        if sql.startswith("INSERT INTO drafts"):
            self.draft_seq += 1
            return f"{self.draft_seq}|2026-{self.draft_seq:04d}"
        if sql.startswith("UPDATE drafts"):
            return "1"
        if sql.startswith("WITH m AS"):
            self.fact_seq += 1
            return str(self.fact_seq)
        return ""

    def fake_ask(self, prompt, label, model=None):
        self.asks.append((label, model, prompt))
        kind = label.split(":")[0]
        return json.dumps(self.scripts[kind].pop(0), ensure_ascii=False)

    def ctx(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(mock.patch.object(self.mod, "psql", side_effect=self.fake_psql))
        stack.enter_context(mock.patch.object(self.mod, "ask_claude", side_effect=self.fake_ask))
        stack.enter_context(mock.patch.object(
            self.mod, "shortlist_facts", side_effect=lambda key, content, k=10: self.shortlist))
        return stack

    def run(self, candidates, project="proj"):
        with self.ctx():
            return self.mod.ringi_facts_project(project, candidates, run_id=9)

    # --- 検査ヘルパ
    def sqls_like(self, fragment):
        return [s for s in self.sqls if fragment in s]


class TestKeiiSenketsu(unittest.TestCase):
    def test_insert_and_skip(self):
        h = Harness()
        h.scripts["shinsa"] = [[{"action": "insert", "replaces": None, "extends": []},
                                {"action": "skip", "memo": "重複"}]]
        ins, drp = h.run([cand("事実A"), cand("事実B")])
        self.assertEqual((ins, drp), (1, 1))
        # 起票は1文書(課長専決)。上申・決裁は無い
        self.assertEqual(len(h.sqls_like("INSERT INTO drafts")), 1)
        self.assertEqual(h.sqls_like("'joshin'"), [])
        # 遷移: shinsa_ok(専決)→shiko
        self.assertTrue(h.sqls_like("decision_class='senketsu'"))
        self.assertTrue(h.sqls_like("executed_at=now()"))
        # 回議録: kian起案・shinsa_ok・shiko(system)
        log_sqls = "\n".join(h.sqls_like("INSERT INTO draft_log"))
        for frag in ("'kian:mk'", "'shinsa:ms'", "'shinsa_ok'", "'system'", "'shiko'"):
            self.assertIn(frag, log_sqls)
        # facts紐付け
        self.assertTrue(h.sqls_like("INSERT INTO draft_facts"))
        # 審査は審査モデルで呼ばれる
        self.assertEqual(h.asks[0][0], "shinsa:proj")
        self.assertEqual(h.asks[0][1], "ms")


class TestJoshinKessai(unittest.TestCase):
    def test_replace_approved_by_kessai(self):
        h = Harness(shortlist=[{"id": 41, "content": "旧事実"}])
        h.scripts["shinsa"] = [[{"action": "insert", "replaces": 41, "extends": []}]]
        h.scripts["kessai"] = [[{"action": "approve"}]]
        ins, drp = h.run([cand("新事実")])
        self.assertEqual((ins, drp), (1, 0))
        # 部長決裁文書: joshin→kessai_ok(bucho)→shiko
        log_sqls = "\n".join(h.sqls_like("INSERT INTO draft_log"))
        self.assertIn("'joshin'", log_sqls)
        self.assertIn("'kessai:mo'", log_sqls)
        self.assertTrue(h.sqls_like("decision_class='bucho'"))
        # factsに置換つき挿入
        self.assertTrue([s for s in h.sqls_like("WITH m AS") if " 41," in s or ", 41," in s])
        # 決裁は決裁モデル
        kessai_calls = [a for a in h.asks if a[0].startswith("kessai")]
        self.assertEqual(kessai_calls[0][1], "mo")

    def test_escalate_without_replace(self):
        h = Harness()
        h.scripts["shinsa"] = [[{"action": "insert", "replaces": None, "escalate": True,
                                 "memo": "矛盾疑い"}]]
        h.scripts["kessai"] = [[{"action": "approve"}]]
        ins, drp = h.run([cand("疑義あり事実")])
        self.assertEqual((ins, drp), (1, 0))
        self.assertTrue(h.sqls_like("decision_class='bucho'"))

    def test_all_hiketsu_rejects_document(self):
        h = Harness(shortlist=[{"id": 41, "content": "旧事実"}])
        h.scripts["shinsa"] = [[{"action": "insert", "replaces": 41}]]
        h.scripts["kessai"] = [[{"action": "hiketsu", "memo": "既存が正しい"}]]
        ins, drp = h.run([cand("誤った置換")])
        self.assertEqual((ins, drp), (0, 1))
        # 否決: rejected遷移があり、shiko(施行)・facts挿入・紐付けが無い
        self.assertTrue(h.sqls_like("state='rejected'"))
        self.assertEqual(h.sqls_like("executed_at=now()"), [])
        self.assertEqual(h.sqls_like("WITH m AS"), [])
        self.assertEqual(h.sqls_like("INSERT INTO draft_facts"), [])

    def test_kessai_sashimodoshi_then_keii(self):
        """決裁差し戻し→審査再判定で軽易化→課長専決で施行"""
        h = Harness(shortlist=[{"id": 41, "content": "旧事実"}])
        h.scripts["shinsa"] = [
            [{"action": "insert", "replaces": 41}],                 # 初審: 置換で上申
            [{"action": "insert", "replaces": None, "extends": []}],  # 再審: 併存で軽易化
        ]
        h.scripts["kessai"] = [[{"action": "sashimodoshi", "memo": "置換でなく併存では"}]]
        ins, drp = h.run([cand("新事実")])
        self.assertEqual((ins, drp), (1, 0))
        # 再審の申し送りに決裁メモが入る
        second_shinsa = [a for a in h.asks if a[0].startswith("shinsa")][1]
        self.assertIn("申し送り: 決裁差し戻し: 置換でなく併存では", second_shinsa[2])
        # 最終的に課長専決(bucho文書は起票されない)
        self.assertTrue(h.sqls_like("decision_class='senketsu'"))
        self.assertFalse(h.sqls_like("decision_class='bucho'"))


class TestHoseiLoop(unittest.TestCase):
    def test_hosei_then_insert(self):
        h = Harness()
        h.scripts["shinsa"] = [
            [{"action": "hosei", "memo": "端末名を明記"}],
            [{"action": "insert", "replaces": None}],
        ]
        h.scripts["hosei"] = [[{"content": "WSL(NucBoxEVO-X2)では動く"}]]
        ins, drp = h.run([cand("この端末では動く")])
        self.assertEqual((ins, drp), (1, 0))
        # 補正はkianモデルが行い、補正後contentで登載。payloadに補正前(original)が残る
        hosei_calls = [a for a in h.asks if a[0].startswith("hosei")]
        self.assertEqual(hosei_calls[0][1], "mk")
        draft_sql = h.sqls_like("INSERT INTO drafts")[0]
        self.assertIn("WSL(NucBoxEVO-X2)では動く", draft_sql)
        self.assertIn("original", draft_sql)
        self.assertIn("この端末では動く", draft_sql)
        # 回議録に審査の差し戻しと起案の補正が残る
        log_sqls = "\n".join(h.sqls_like("INSERT INTO draft_log"))
        self.assertIn("'sashimodoshi'", log_sqls)
        self.assertIn("'hosei'", log_sqls)

    def test_hosei_rounds_exceeded(self):
        h = Harness(config={"ringi": {"max_hosei_rounds": 1}})
        h.scripts["shinsa"] = [
            [{"action": "hosei", "memo": "不備1"}],
            [{"action": "hosei", "memo": "まだ不備"}],
        ]
        h.scripts["hosei"] = [[{"content": "直したつもり"}]]
        ins, drp = h.run([cand("不備のある候補")])
        self.assertEqual((ins, drp), (0, 1))
        self.assertEqual(h.sqls_like("WITH m AS"), [])  # facts無し
        # 文書は起票され(廃案の記録)、専決で施行(登載外0件の決裁記録)
        self.assertEqual(len(h.sqls_like("INSERT INTO drafts")), 1)
        self.assertIn("補正往復の上限", "\n".join(h.sqls_like("INSERT INTO draft_log")))

    def test_withdraw(self):
        h = Harness()
        h.scripts["shinsa"] = [[{"action": "hosei", "memo": "秘密情報を含む"}]]
        h.scripts["hosei"] = [[{"withdraw": True}]]
        ins, drp = h.run([cand("token=abc123 で動いた")])
        self.assertEqual((ins, drp), (0, 1))
        self.assertIn("取り下げ", "\n".join(h.sqls_like("INSERT INTO draft_log")))


class TestScopeSplit(unittest.TestCase):
    def test_general_and_project_docs(self):
        h = Harness()
        h.scripts["shinsa"] = [
            [{"action": "insert", "replaces": None}],
            [{"action": "insert", "replaces": None}],
        ]
        ins, drp = h.run([cand("プロジェクト事実"), cand("全般事実", scope="general")])
        self.assertEqual((ins, drp), (2, 0))
        drafts = h.sqls_like("INSERT INTO drafts")
        self.assertEqual(len(drafts), 2)  # key別に1文書ずつ
        self.assertTrue(any("'general'" in s for s in drafts))
        self.assertTrue(any("'proj'" in s for s in drafts))


if __name__ == "__main__":
    unittest.main()
