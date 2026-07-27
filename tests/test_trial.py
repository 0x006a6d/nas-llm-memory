"""試行モード(第1期: 起案モデルの並行比較)の検査(DB・claude CLI不要)。

実行: python3 -m unittest discover tests
"""
import json
import unittest
from unittest import mock

from test_nightly_config import load_nightly, fake_claude_result


class TestTrialMetrics(unittest.TestCase):
    def setUp(self):
        self.mod = load_nightly({"model": "m0"})

    def test_metrics(self):
        clusters = [
            {"members": {"a": [0], "b": [0]}, "value": True},
            {"members": {"a": [1]}, "value": True},
            {"members": {"b": [1, 2]}, "value": False},
        ]
        m = self.mod.trial_metrics(clusters, {"a": 2, "b": 3})
        self.assertEqual(m["a"]["hit"], 2)
        self.assertEqual(m["a"]["miss_rate"], 0.0)
        self.assertEqual(m["a"]["noise_rate"], 0.0)
        self.assertEqual(m["b"]["hit"], 1)
        self.assertEqual(m["b"]["miss_rate"], 0.5)   # 価値あり2つのうち1つ漏れ
        self.assertEqual(m["b"]["noise_rate"], round(2 / 3, 3))

    def test_metrics_degenerate(self):
        # 価値ありクラスタ0 → 拾い漏れ率は判定不能(None)。候補0 → 誤拾い率None
        m = self.mod.trial_metrics([], {"a": 0})
        self.assertIsNone(m["a"]["miss_rate"])
        self.assertIsNone(m["a"]["noise_rate"])

    def test_summary_rows(self):
        rows = self.mod.trial_summary_rows(7, "p", {
            "a": {"cands": 2, "hit": 1, "valuable": 2, "miss_rate": 0.5, "noise_rate": None}})
        self.assertEqual(rows, ["| 7 | p | a | 2 | 1/2 | 50% | - |"])

    def test_summary_rows_marks_omitted(self):
        # 突合に載せられなかった切り落とし候補数を候補数に併記する
        rows = self.mod.trial_summary_rows(7, "p", {
            "a": {"cands": 2, "hit": 1, "valuable": 2, "miss_rate": 0.5, "noise_rate": None,
                  "omitted": 3}})
        self.assertEqual(rows, ["| 7 | p | a | 2(-3) | 1/2 | 50% | - |"])


class TestAlignTrial(unittest.TestCase):
    def test_normalizes_letters_and_ranges(self):
        mod = load_nightly({"model": "m0", "roles": {"shinsa": "ms"}})
        results = {"m0": [{"content": "x"}, {"content": "y"}],
                   "m1": [{"content": "x2"}]}
        raw = [{"members": {"A": [0, 1, 9], "B": [0, 0]}, "value": True, "reason": "同一"},
               "garbage",
               {"members": {"B": [-1]}, "value": False}]
        r = fake_claude_result()
        r.stdout = json.dumps({"subtype": "success", "result": json.dumps(raw),
                               "usage": {}, "modelUsage": {}})
        with mock.patch.object(mod.subprocess, "run", return_value=r) as m:
            clusters, omitted = mod.align_trial("proj", results)
        # 審査モデルで呼ばれている
        cmd = m.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "ms")
        self.assertEqual(len(clusters), 2)  # dict以外は落ちる
        self.assertEqual(clusters[0]["members"], {"m0": [0, 1], "m1": [0]})  # 範囲外9と重複が消える
        self.assertEqual(clusters[1]["members"], {"m0": [], "m1": []})       # 負indexが消える
        self.assertEqual(omitted, {"m0": 0, "m1": 0})

    def test_budget_truncates_and_reports_omitted(self):
        """枠を超える候補は切り落とし、件数がomittedに残る(沈黙の切り落とし禁止)。"""
        mod = load_nightly({"model": "m0"})
        results = {"m0": [{"content": "x" * 900} for _ in range(5)]}
        r = fake_claude_result()
        r.stdout = json.dumps({"subtype": "success", "result": "[]",
                               "usage": {}, "modelUsage": {}})
        with mock.patch.object(mod, "TRIAL_ALIGN_BUDGET_CHARS", 2000), \
                mock.patch.object(mod.subprocess, "run", return_value=r) as m:
            clusters, omitted = mod.align_trial("proj", results)
        self.assertEqual(omitted["m0"], 3)  # 2000枠に収まるのは先頭2件
        prompt = m.call_args.kwargs["input"]
        self.assertIn("[1]", prompt)
        self.assertNotIn("[2]", prompt)  # 切り落とした候補はプロンプトに載らない
        self.assertEqual(clusters, [])


class TestTrialProject(unittest.TestCase):
    def test_writes_json_and_summary(self):
        cfg = {"model": "m0", "roles": {"shinsa": "ms"},
               "ringi": {"trial": True, "trial_models": ["mh", "m0"]}}  # m0重複はスキップ
        mod = load_nightly(cfg)
        baseline = [{"content": "b0", "status": "verified", "scope": "project"}]
        trial_cand = [{"content": "t0", "status": "unverified", "scope": "project",
                       "provenance": [], "confidence": None}]

        with mock.patch.object(mod, "verify_project", return_value=trial_cand) as vp, \
             mock.patch.object(mod, "align_trial", return_value=(
                 [{"members": {"m0": [0], "mh": [0]}, "value": True, "reason": "同一"}],
                 {"m0": 0, "mh": 0})):
            mod.trial_project("proj/x", [([], [])], baseline, run_id=42)
        # 試行モデルはmhのみ(重複m0は再verifyしない)、chunk1つ分
        self.assertEqual(vp.call_count, 1)
        self.assertEqual(vp.call_args.kwargs.get("model"), "mh")
        self.assertEqual(vp.call_args.kwargs.get("label_prefix"), "trial-verify")

        tdir = mod.SYSTEM_DIR / "batch" / "trial"
        data = json.loads(
            (tdir / f"run42-{mod.project_dir_name('proj/x')}.json").read_text(encoding="utf-8"))
        self.assertEqual(data["project"], "proj/x")
        self.assertIn("m0", data["candidates"])
        self.assertIn("mh", data["candidates"])
        summary = (tdir / "summary.md").read_text(encoding="utf-8")
        self.assertIn("| 42 | proj/x | m0 |", summary)
        self.assertIn("| 42 | proj/x | mh |", summary)
        # 2回目の追記でヘッダが重複しない
        with mock.patch.object(mod, "verify_project", return_value=trial_cand), \
             mock.patch.object(mod, "align_trial", return_value=([], {})):
            mod.trial_project("proj/x", [([], [])], baseline, run_id=43)
        summary = (tdir / "summary.md").read_text(encoding="utf-8")
        self.assertEqual(summary.count("# 起案モデル試行の突合表"), 1)
        self.assertIn("| 43 | proj/x |", summary)

    def test_align_skipped_when_all_empty(self):
        """どのモデルも候補を出さない晩は突合(align_trial)を呼ばない。"""
        cfg = {"model": "m0", "ringi": {"trial": True, "trial_models": ["mh"]}}
        mod = load_nightly(cfg)
        with mock.patch.object(mod, "verify_project", return_value=[]), \
                mock.patch.object(mod, "align_trial") as al:
            mod.trial_project("proj/x", [([], [])], [], run_id=44)
        self.assertEqual(al.call_count, 0)
        data = json.loads((mod.SYSTEM_DIR / "batch" / "trial"
                           / f"run44-{mod.project_dir_name('proj/x')}.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(data["clusters"], [])

    def test_deadline_skips_remaining_models(self):
        """時間枠を超えたら残りの試行モデルはverifyせず見送り、記録に残す。"""
        cfg = {"model": "m0", "ringi": {"trial": True, "trial_models": ["mh", "ms"]}}
        mod = load_nightly(cfg)
        baseline = [{"content": "b0", "status": "verified", "scope": "project"}]
        with mock.patch.object(mod, "verify_project", return_value=[]) as vp, \
                mock.patch.object(mod, "align_trial", return_value=([], {})):
            mod.trial_project("proj/x", [([], [])], baseline, run_id=45,
                              deadline=mod.time.monotonic() - 1)  # 既に超過
        self.assertEqual(vp.call_count, 0)
        data = json.loads((mod.SYSTEM_DIR / "batch" / "trial"
                           / f"run45-{mod.project_dir_name('proj/x')}.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(data["skipped_models"], ["mh", "ms"])


class TestTrialFlagPlumbing(unittest.TestCase):
    def test_trial_off_by_default(self):
        mod = load_nightly({"model": "m0"})
        self.assertFalse(mod.ringi.ringi_settings(mod.BATCH_CONFIG)["trial"])

    def test_trial_on_by_config(self):
        mod = load_nightly({"model": "m0", "ringi": {"trial": True}})
        self.assertTrue(mod.ringi.ringi_settings(mod.BATCH_CONFIG)["trial"])


if __name__ == "__main__":
    unittest.main()
