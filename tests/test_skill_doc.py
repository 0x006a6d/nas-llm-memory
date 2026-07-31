"""skill登載伺い(ringi_skill_scan / execute_skill_doc)の検査(DB・claude CLI・git不要)。

実行: python3 -m unittest discover tests
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_ringi_flow import Harness


def setup_repo(h, name="raster-qa", count=3, kind="new", skill_md="# 手順\n1. やる\n",
               case=None):
    repo = Path(tempfile.mkdtemp(prefix="repo-test-"))
    if case is not None:  # /tmp に残骸を残さない
        case.addCleanup(shutil.rmtree, repo, ignore_errors=True)
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
        self.skill_mv_count = "1"  # 移動完了の記帳(再開テスト用。"0"=記帳なし)

    def fake_psql(self, sql):
        if "payload->>'name'" in sql:
            self.sqls.append(sql)
            return json.dumps(self.skill_prev) if self.skill_prev else ""
        if sql.startswith("SELECT state FROM drafts"):
            return "pending_review"
        if sql.startswith("SELECT count(*) FROM draft_log"):
            self.sqls.append(sql)
            return self.skill_mv_count
        return super().fake_psql(sql)

    def scan(self):
        with self.ctx():
            self.mod.ringi_skill_scan(run_id=9)


class TestSkillScan(unittest.TestCase):
    def test_below_min_count_not_filed(self):
        h = SkillHarness()
        setup_repo(h, count=1, case=self)
        h.scan()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_improve_kind_skipped(self):
        h = SkillHarness()
        setup_repo(h, kind="improve", case=self)
        h.scan()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_collision_with_existing_skill_skipped(self):
        h = SkillHarness()
        repo = setup_repo(h, case=self)
        (repo / "skills" / "raster-qa").mkdir()
        h.scan()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_happy_path_stops_at_joshin(self):
        """審査の上申で停止する。決裁は人間の専権(LLM決裁を呼ばない)。"""
        h = SkillHarness()
        setup_repo(h, case=self)
        h.scripts["shinsa-skill"] = [{"action": "joshin", "memo": "重複なし・手順具体的"}]
        h.scan()
        drafts = h.sqls_like("INSERT INTO drafts")
        self.assertEqual(len(drafts), 1)
        self.assertIn("'skill'", drafts[0])
        self.assertIn("人間の決裁を条件", drafts[0])
        # joshin(pending_decision)で停止。決裁・施行はされない
        self.assertTrue(h.sqls_like("state='pending_decision'"))
        self.assertEqual(h.sqls_like("decision_class="), [])
        self.assertEqual(h.sqls_like("executed_at=now()"), [])
        log_sqls = "\n".join(h.sqls_like("INSERT INTO draft_log"))
        self.assertIn("'skill-scout'", log_sqls)
        self.assertIn("重複なし・手順具体的", log_sqls)
        # 審査=msのみ(決裁のLLM呼び出しは無い)
        self.assertEqual([a[1] for a in h.asks], ["ms"])

    def test_shinsa_hiketsu(self):
        h = SkillHarness()
        setup_repo(h, case=self)
        h.scripts["shinsa-skill"] = [{"action": "hiketsu", "memo": "既存と重複"}]
        h.scan()
        self.assertTrue(h.sqls_like("state='rejected'"))
        self.assertEqual([a for a in h.asks if a[0].startswith("kessai")], [])

    def test_already_filed_skipped(self):
        h = SkillHarness()
        setup_repo(h, count=3, case=self)
        h.skill_prev = [{"state": "approved", "count": "3"}]
        h.scan()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_rejected_refiled_only_with_more_evidence(self):
        h = SkillHarness()
        setup_repo(h, count=3, case=self)
        h.skill_prev = [{"state": "rejected", "count": "3"}]
        h.scan()
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])  # count同じ→再起票しない
        h2 = SkillHarness()
        setup_repo(h2, count=5, case=self)
        h2.skill_prev = [{"state": "rejected", "count": "3"}]
        h2.scripts["shinsa-skill"] = [{"action": "joshin", "memo": "ok"}]
        h2.scan()
        self.assertEqual(len(h2.sqls_like("INSERT INTO drafts")), 1)

class TestExecuteSkillDoc(unittest.TestCase):
    def test_frontmatter_injected_and_git_called(self):
        h = SkillHarness()
        repo = setup_repo(h, skill_md="# 手順\n1. やる\n", case=self)  # frontmatterなし
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
        logs = "\n".join(h.sqls_like("INSERT INTO draft_log"))
        self.assertIn("skills/raster-qa へ登載", logs)
        self.assertIn("'skill_mv'", logs)  # 移動完了の記帳(中断再開の判定根拠)

    def test_existing_dst_rejected(self):
        h = SkillHarness()
        repo = setup_repo(h, case=self)
        (repo / "skills" / "raster-qa").mkdir()
        with h.ctx(), self.assertRaises(RuntimeError):
            h.mod.execute_skill_doc(7, "raster-qa", run_id=9)

    def _moved_repo(self, h):
        """移動だけ済んだ形(候補は消え、skills/に入っている)のリポジトリを作る。"""
        repo = setup_repo(h, case=self)
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
        order = []  # git呼び出しとSQLを同一のイベント列に記録し、順序を直接比較する
        base_psql = h.fake_psql

        def tracking_psql(sql):
            order.append("sql: " + sql)
            return base_psql(sql)

        def tracking_git(cmd, **kw):
            order.append("git: " + " ".join(cmd))
            return mock.Mock(returncode=0,
                             stdout="R  skills-candidates/x -> skills/x" if "status" in cmd else "")

        with mock.patch.object(h.mod, "psql", side_effect=tracking_psql), \
                mock.patch.object(h.mod.subprocess, "run", side_effect=tracking_git):
            h.mod.execute_skill_doc(7, "raster-qa", run_id=9)
        git = [e for e in order if e.startswith("git: ")]
        self.assertFalse(any(" mv " in e for e in git))       # 移動はやり直さない
        self.assertTrue(any("commit" in e for e in git))      # 未コミットなのでcommitする
        self.assertTrue(any("push" in e for e in git))
        # push が通ってから状態(executed_at)を進める: gitイベントとSQLイベントの位置を直接比較
        i_push = max(i for i, e in enumerate(order) if e.startswith("git: ") and "push" in e)
        i_exec = min(i for i, e in enumerate(order) if "executed_at=now()" in e)
        self.assertLess(i_push, i_exec)
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
            with self.assertRaises(h.mod.subprocess.CalledProcessError):
                h.mod.execute_skill_doc(7, "raster-qa", run_id=9)
        self.assertEqual(h.sqls_like("executed_at=now()"), [])

    def test_missing_src_without_dst_still_errors(self):
        h = SkillHarness()
        repo = setup_repo(h, case=self)
        for p in sorted((repo / "skills-candidates" / "raster-qa").iterdir()):
            p.unlink()
        (repo / "skills-candidates" / "raster-qa").rmdir()
        with h.ctx(), self.assertRaises(RuntimeError):
            h.mod.execute_skill_doc(7, "raster-qa", run_id=9)

    def test_resume_without_mv_record_rejected(self):
        """移動の記帳が無いのに skills/<name> がある=手動作成の可能性。誤って登載しない。"""
        h = SkillHarness()
        h.skill_mv_count = "0"  # 記帳なし
        self._moved_repo(h)
        calls = []
        with h.ctx(), mock.patch.object(h.mod.subprocess, "run",
                                        side_effect=self._fake_git(calls)):
            with self.assertRaises(RuntimeError):
                h.mod.execute_skill_doc(7, "raster-qa", run_id=9)
        self.assertEqual(h.sqls_like("executed_at=now()"), [])
        self.assertEqual(calls, [])  # gitも触らない


if __name__ == "__main__":
    unittest.main()
