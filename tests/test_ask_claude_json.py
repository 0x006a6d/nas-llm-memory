"""ask_claude_json(応答内JSON破損の問い直し)の検査(DB・claude CLI不要)。

外側のSDK envelope破損の問い直しはask_claude自身が持つ。ここで検査するのは
モデルが書いた中身のJSONが壊れていたとき(途中で切れた配列等)に1度だけ
問い直し、2度目も壊れていたら諦める挙動。
実行: python3 -m unittest discover tests
"""
import unittest
from unittest import mock

from test_nightly_config import load_nightly


class TestAskClaudeJson(unittest.TestCase):
    def setUp(self):
        self.mod = load_nightly({"model": "m0"})

    def test_valid_first_try(self):
        with mock.patch.object(self.mod, "ask_claude", return_value='[{"a": 1}]') as ac:
            self.assertEqual(self.mod.ask_claude_json("p", "l"), [{"a": 1}])
        self.assertEqual(ac.call_count, 1)

    def test_retry_on_broken_json(self):
        # run 37/41の実際の壊れ方: JSONの頭はあるが直後で破綻している
        with mock.patch.object(self.mod, "ask_claude",
                               side_effect=["[…", '{"ok": true}']) as ac:
            self.assertEqual(self.mod.ask_claude_json("p", "l"), {"ok": True})
        self.assertEqual(ac.call_count, 2)

    def test_retry_on_no_json(self):
        with mock.patch.object(self.mod, "ask_claude",
                               side_effect=["すみません、出せません", "[]"]) as ac:
            self.assertEqual(self.mod.ask_claude_json("p", "l"), [])
        self.assertEqual(ac.call_count, 2)

    def test_gives_up_after_second_failure(self):
        with mock.patch.object(self.mod, "ask_claude",
                               side_effect=["[…", "[…"]) as ac:
            with self.assertRaises(Exception):
                self.mod.ask_claude_json("p", "l")
        self.assertEqual(ac.call_count, 2)

    def test_model_passthrough(self):
        with mock.patch.object(self.mod, "ask_claude", return_value="[]") as ac:
            self.mod.ask_claude_json("p", "l", model="mx")
        for c in ac.call_args_list:
            self.assertEqual(c.kwargs.get("model"), "mx")


if __name__ == "__main__":
    unittest.main()
