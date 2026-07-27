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
            clusters = mod.align_trial("proj", results)
        # 審査モデルで呼ばれている
        cmd = m.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "ms")
        self.assertEqual(len(clusters), 2)  # dict以外は落ちる
        self.assertEqual(clusters[0]["members"], {"m0": [0, 1], "m1": [0]})  # 範囲外9と重複が消える
        self.assertEqual(clusters[1]["members"], {"m0": [], "m1": []})       # 負indexが消える


class TestTrialProject(unittest.TestCase):
    def test_writes_json_and_summary(self):
        cfg = {"model": "m0", "roles": {"shinsa": "ms"},
               "ringi": {"trial": True, "trial_models": ["mh", "m0"]}}  # m0重複はスキップ
        mod = load_nightly(cfg)
        baseline = [{"content": "b0", "status": "verified", "scope": "project"}]
        trial_cand = [{"content": "t0", "status": "unverified", "scope": "project",
                       "provenance": [], "confidence": None}]
        clusters = [{"members": {"A": [0], "B": [0]}, "value": True, "reason": "同一"}]

        with mock.patch.object(mod, "verify_project", return_value=trial_cand) as vp, \
             mock.patch.object(mod, "align_trial", return_value=[
                 {"members": {"m0": [0], "mh": [0]}, "value": True, "reason": "同一"}]):
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
             mock.patch.object(mod, "align_trial", return_value=[]):
            mod.trial_project("proj/x", [([], [])], baseline, run_id=43)
        summary = (tdir / "summary.md").read_text(encoding="utf-8")
        self.assertEqual(summary.count("# 起案モデル試行の突合表"), 1)
        self.assertIn("| 43 | proj/x |", summary)


class TestTrialFlagPlumbing(unittest.TestCase):
    def test_trial_off_by_default(self):
        mod = load_nightly({"model": "m0"})
        self.assertFalse(mod.ringi.ringi_settings(mod.BATCH_CONFIG)["trial"])

    def test_trial_on_by_config(self):
        mod = load_nightly({"model": "m0", "ringi": {"trial": True}})
        self.assertTrue(mod.ringi.ringi_settings(mod.BATCH_CONFIG)["trial"])


if __name__ == "__main__":
    unittest.main()
