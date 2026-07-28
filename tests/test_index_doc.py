"""index改定伺い(enrich_ringi)の検査(DB・claude CLI不要)。

実行: python3 -m unittest discover tests
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_ringi_flow import Harness


def make(h, body_lines, key="proj", case=None):
    """enrich_ringiをbuild_index_bodyモックで走らせる。"""
    body = "\n".join(body_lines) + "\n"
    repo = Path(tempfile.mkdtemp(prefix="repo-test-"))
    if case is not None:  # /tmp に残骸を残さない
        case.addCleanup(shutil.rmtree, repo, ignore_errors=True)
    h.mod.REPO_DIR = repo
    with h.ctx(), mock.patch.object(h.mod, "build_index_body",
                                    return_value=(body, len(body_lines))):
        return h.mod.enrich_ringi(key, run_id=9), body


class TestIndexDoc(unittest.TestCase):
    def test_new_index_is_senketsu(self):
        h = Harness()
        n, body = make(h, ["# t", "内容A"], case=self)
        self.assertEqual(n, 2)
        self.assertEqual(h.mod.index_path("proj").read_text(encoding="utf-8"), body)
        drafts = h.sqls_like("INSERT INTO drafts")
        self.assertEqual(len(drafts), 1)
        self.assertIn("'index'", drafts[0])
        self.assertTrue(h.sqls_like("decision_class='senketsu'"))
        self.assertIn("機械的帰結につき専決", "\n".join(h.sqls_like("INSERT INTO draft_log")))

    def test_unchanged_files_no_draft(self):
        h = Harness()
        repo = Path(tempfile.mkdtemp(prefix="repo-test-"))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        h.mod.REPO_DIR = repo
        body = "# t\n内容A\n"
        p = h.mod.index_path("proj")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        with h.ctx(), mock.patch.object(h.mod, "build_index_body",
                                        return_value=(body, 2)):
            n = h.mod.enrich_ringi("proj", run_id=9)
        self.assertEqual(n, 2)
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def _with_old(self, h, old_lines, new_lines, kessai_script=None):
        repo = Path(tempfile.mkdtemp(prefix="repo-test-"))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        h.mod.REPO_DIR = repo
        p = h.mod.index_path("proj")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
        if kessai_script is not None:
            h.scripts["kessai-index"] = kessai_script
        body = "\n".join(new_lines) + "\n"
        with h.ctx(), mock.patch.object(h.mod, "build_index_body",
                                        return_value=(body, len(new_lines))):
            return h.mod.enrich_ringi("proj", run_id=9), body, p

    def test_small_change_senketsu_with_diff(self):
        h = Harness()
        old = [f"行{i}" for i in range(10)]
        new = old[:9] + ["行9改"]
        n, body, p = self._with_old(h, old, new)
        self.assertEqual(n, 10)
        self.assertEqual(p.read_text(encoding="utf-8"), body)
        draft = h.sqls_like("INSERT INTO drafts")[0]
        self.assertIn("-行9", draft)     # unified diffが別記に載る
        self.assertIn("+行9改", draft)
        self.assertTrue(h.sqls_like("decision_class='senketsu'"))
        self.assertEqual([a for a in h.asks if a[0].startswith("kessai-index")], [])

    def test_mass_deletion_escalates_and_approved(self):
        h = Harness()
        old = [f"行{i}" for i in range(10)]
        new = old[:5]  # 削除5/10 = 50% > 30%
        n, body, p = self._with_old(h, old, new,
                                    kessai_script=[{"action": "approve", "memo": "統合済みで妥当"}])
        self.assertEqual(n, 5)
        self.assertEqual(p.read_text(encoding="utf-8"), body)
        self.assertTrue(h.sqls_like("decision_class='bucho'"))
        kessai = [a for a in h.asks if a[0].startswith("kessai-index")]
        self.assertEqual(kessai[0][1], "mo")  # 決裁モデル
        self.assertIn("削除5行", "\n".join(h.sqls_like("INSERT INTO draft_log")))

    def test_mass_deletion_hiketsu_keeps_old(self):
        h = Harness()
        old = [f"行{i}" for i in range(10)]
        new = old[:5]
        n, body, p = self._with_old(h, old, new,
                                    kessai_script=[{"action": "hiketsu", "memo": "重要事実が消える"}])
        self.assertEqual(n, 0)
        self.assertEqual(p.read_text(encoding="utf-8"), "\n".join(old) + "\n")  # 現行維持
        self.assertTrue(h.sqls_like("state='rejected'"))
        self.assertEqual(h.sqls_like("executed_at=now()"), [])

    def test_malformed_kessai_keeps_old(self):
        h = Harness()
        old = [f"行{i}" for i in range(10)]
        # scripts機構はJSONを返すので、形式不一致はdict以外で表現
        n, body, p = self._with_old(h, old, old[:5], kessai_script=[["not", "a", "dict"]])
        self.assertEqual(n, 0)
        self.assertEqual(p.read_text(encoding="utf-8"), "\n".join(old) + "\n")


if __name__ == "__main__":
    unittest.main()
