"""skill登載伺い(ringi_skill_scan / execute_skill_doc)の検査(DB・claude CLI・git不要)。

実行: python3 -m unittest discover tests
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_ringi_flow import Harness


def setup_repo(h, name="raster-qa", count=3, kind="new", skill_md="# 手順\n1. やる\n"):
    repo = Path(tempfile.mkdtemp(prefix="repo-test-"))
    h.mod.REPO_DIR = repo
    d = repo / "skills-candidates" / name
    d.mkdir(parents=True)
    meta = {"name": name, "kind": kind, "summary": "テスト手順", "count": count,
            "evidence": [1, 2], "created": "2026-07-01"}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (repo / "skills").mkdir()
    return repo


class SkillHarness(Harness):
    def __init__(self, config=None):
        super().__init__(config=config)
        self.skill_prev = None  # 起票済み判定クエリへの応答(list or None)

    def fake_psql(self, sql):
        if "payload->>'name'" in sql:
            self.sqls.append(sql)
            return json.dumps(self.skill_prev) if self.skill_prev else ""
        if sql.startswith("SELECT state FROM drafts"):
            return "pending_review"
        return super().fake_psql(sql)

    def scan(self):
        with self.ctx():
            self.mod.ringi_skill_scan(run_id=9)


class TestSkillScan(unittest.TestCase):
    def test_below_min_count_not_filed(self):
        h = SkillHarness()
        setup_repo(h, count=1)
        h.scan()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_improve_kind_skipped(self):
        h = SkillHarness()
        setup_repo(h, kind="improve")
        h.scan()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_collision_with_existing_skill_skipped(self):
        h = SkillHarness()
        repo = setup_repo(h)
        (repo / "skills" / "raster-qa").mkdir()
        h.scan()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_happy_path_stops_at_approved(self):
        h = SkillHarness()
        setup_repo(h)
        h.scripts["shinsa-skill"] = [{"action": "joshin", "memo": "重複なし・手順具体的"}]
        h.scripts["kessai-skill"] = [{"action": "approve", "memo": "登載可"}]
        h.scan()
        drafts = h.sqls_like("INSERT INTO drafts")
        self.assertEqual(len(drafts), 1)
        self.assertIn("'skill'", drafts[0])
        self.assertIn("後閲印を条件", drafts[0])
        # joshin→kessai_ok(bucho)で停止。施行(shiko)されない
        self.assertTrue(h.sqls_like("decision_class='bucho'"))
        self.assertEqual(h.sqls_like("executed_at=now()"), [])
        log_sqls = "\n".join(h.sqls_like("INSERT INTO draft_log"))
        self.assertIn("'skill-scout'", log_sqls)
        self.assertIn("重複なし・手順具体的", log_sqls)
        # 審査=ms、決裁=mo
        self.assertEqual([a[1] for a in h.asks], ["ms", "mo"])

    def test_shinsa_hiketsu(self):
        h = SkillHarness()
        setup_repo(h)
        h.scripts["shinsa-skill"] = [{"action": "hiketsu", "memo": "既存と重複"}]
        h.scan()
        self.assertTrue(h.sqls_like("state='rejected'"))
        self.assertEqual([a for a in h.asks if a[0].startswith("kessai")], [])

    def test_already_filed_skipped(self):
        h = SkillHarness()
        setup_repo(h, count=3)
        h.skill_prev = [{"state": "approved", "count": "3"}]
        h.scan()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_rejected_refiled_only_with_more_evidence(self):
        h = SkillHarness()
        setup_repo(h, count=3)
        h.skill_prev = [{"state": "rejected", "count": "3"}]
        h.scan()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])  # count同じ→再起票しない
        h2 = SkillHarness()
        setup_repo(h2, count=5)
        h2.skill_prev = [{"state": "rejected", "count": "3"}]
        h2.scripts["shinsa-skill"] = [{"action": "joshin", "memo": "ok"}]
        h2.scripts["kessai-skill"] = [{"action": "approve", "memo": "ok"}]
        h2.scan()
        self.assertEqual(len(h2.sqls_like("INSERT INTO drafts")), 1)

    def test_auto_execute_calls_shiko(self):
        h = SkillHarness(config={"ringi": {"skill_auto_execute": True}})
        setup_repo(h)
        h.scripts["shinsa-skill"] = [{"action": "joshin", "memo": "ok"}]
        h.scripts["kessai-skill"] = [{"action": "approve", "memo": "ok"}]
        with mock.patch.object(h.mod, "execute_skill_doc") as ex:
            h.scan()
        self.assertEqual(ex.call_count, 1)


class TestExecuteSkillDoc(unittest.TestCase):
    def test_frontmatter_injected_and_git_called(self):
        h = SkillHarness()
        repo = setup_repo(h, skill_md="# 手順\n1. やる\n")  # frontmatterなし
        git_calls = []

        def fake_run(cmd, **kw):
            git_calls.append(cmd)
            return mock.Mock(returncode=0)

        with h.ctx(), mock.patch.object(h.mod.subprocess, "run", side_effect=fake_run):
            h.mod.execute_skill_doc(7, "raster-qa", run_id=9)
        text = (repo / "skills-candidates" / "raster-qa" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: raster-qa\ndescription: テスト手順\n---"))
        self.assertIn("# 手順", text)
        joined = [" ".join(c) for c in git_calls]
        self.assertTrue(any("mv skills-candidates/raster-qa skills/raster-qa" in c for c in joined))
        self.assertTrue(any("push" in c for c in joined))
        # 遷移approved→shikoと回議録
        self.assertTrue(h.sqls_like("executed_at=now()"))
        self.assertIn("skills/raster-qa へ登載", "\n".join(h.sqls_like("INSERT INTO draft_log")))

    def test_existing_dst_rejected(self):
        h = SkillHarness()
        repo = setup_repo(h)
        (repo / "skills" / "raster-qa").mkdir()
        with h.ctx(), self.assertRaises(RuntimeError):
            h.mod.execute_skill_doc(7, "raster-qa", run_id=9)

    def _moved_repo(self, h):
        """移動だけ済んだ形(候補は消え、skills/に入っている)のリポジトリを作る。"""
        repo = setup_repo(h)
        for p in sorted((repo / "skills-candidates" / "raster-qa").iterdir()):
            p.unlink()
        (repo / "skills-candidates" / "raster-qa").rmdir()
        (repo / "skills" / "raster-qa").mkdir()
        return repo

    def _fake_git(self, calls, porcelain=""):
        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout=porcelain if "status" in cmd else "")
        return run

    def test_resume_pushes_before_state_update(self):
        """移動後に落ちた文書の再開: mvはやり直さず、未コミット分をcommit+pushしてから遷移。"""
        h = SkillHarness()
        self._moved_repo(h)
        calls = []
        with h.ctx(), mock.patch.object(
                h.mod.subprocess, "run",
                side_effect=self._fake_git(calls, porcelain="R  skills-candidates/x -> skills/x")):
            h.mod.execute_skill_doc(7, "raster-qa", run_id=9)
        joined = [" ".join(c) for c in calls]
        self.assertFalse(any(" mv " in c for c in joined))       # 移動はやり直さない
        self.assertTrue(any("commit" in c for c in joined))      # 未コミットなのでcommitする
        self.assertTrue(any("push" in c for c in joined))
        # push が通ってから状態を進める
        self.assertLess(max(i for i, c in enumerate(joined) if "push" in c), len(joined))
        self.assertTrue(h.sqls_like("executed_at=now()"))
        self.assertIn("前回の中断分", "\n".join(h.sqls_like("INSERT INTO draft_log")))

    def test_resume_skips_commit_when_clean(self):
        """commit済みでpushだけ落ちていた場合: commitはせずpushのみ。"""
        h = SkillHarness()
        self._moved_repo(h)
        calls = []
        with h.ctx(), mock.patch.object(h.mod.subprocess, "run",
                                        side_effect=self._fake_git(calls, porcelain="")):
            h.mod.execute_skill_doc(7, "raster-qa", run_id=9)
        joined = [" ".join(c) for c in calls]
        self.assertFalse(any("commit" in c for c in joined))
        self.assertTrue(any("push" in c for c in joined))

    def test_resume_push_failure_keeps_state(self):
        """pushが失敗したら状態は進めない(翌晩また再開する)。"""
        h = SkillHarness()
        self._moved_repo(h)

        def run(cmd, **kw):
            if "push" in cmd:
                raise h.mod.subprocess.CalledProcessError(1, cmd)
            return mock.Mock(returncode=0, stdout="")

        with h.ctx(), mock.patch.object(h.mod.subprocess, "run", side_effect=run):
            with self.assertRaises(Exception):
                h.mod.execute_skill_doc(7, "raster-qa", run_id=9)
        self.assertEqual(h.sqls_like("executed_at=now()"), [])

    def test_missing_src_without_dst_still_errors(self):
        h = SkillHarness()
        repo = setup_repo(h)
        for p in sorted((repo / "skills-candidates" / "raster-qa").iterdir()):
            p.unlink()
        (repo / "skills-candidates" / "raster-qa").rmdir()
        with h.ctx(), self.assertRaises(RuntimeError):
            h.mod.execute_skill_doc(7, "raster-qa", run_id=9)


if __name__ == "__main__":
    unittest.main()
