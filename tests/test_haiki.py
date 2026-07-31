"""廃棄伺い(法8条2項)の検査(DB・claude CLI不要)。

廃棄は取り消せないので、消し方の条件(範囲・除外・バックアップ)を重点的に固定する。
実行: python3 -m unittest discover tests
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "nas" / "batch"))

import kanribo  # noqa: E402

from test_ringi_flow import Harness  # noqa: E402


def rec(category="shuju-turns", **kw):
    f = {"id": 7, "category": category, "project_key": "proj",
         "name": "収受(生ログ) proj 令和8年度", "period": "2026",
         "expires_on": "2030-03-31", "measure": "haiki",
         "location": "DB turns", "n_rows": 120, "id_from": 10, "id_to": 400}
    f.update(kw)
    return f


class TestDisposeSql(unittest.TestCase):
    def test_turns_keeps_rows_backing_current_facts(self):
        """現用factsの根拠(provenance)になっているturnsは廃棄しない。"""
        sql = kanribo.dispose_sql(rec(), draft_id=5)
        self.assertIn("DELETE FROM turns", sql)
        self.assertIn("id BETWEEN 10 AND 400", sql)
        self.assertIn("t.project_key = 'proj'", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("current_facts", sql)
        self.assertIn("t.id = ANY(cf.provenance)", sql)

    def test_raw_disposes_body_only(self):
        """受信生データは本文だけ廃棄し、行と受信日時は証跡として残す。"""
        sql = kanribo.dispose_sql(rec(category="shuju-raw", location="DB raw_payloads"), 5)
        self.assertIn("UPDATE raw_payloads SET payload = '{}'::jsonb", sql)
        self.assertIn("disposed_at = now()", sql)
        self.assertIn("disposed_draft = 5", sql)
        self.assertIn("disposed_at IS NULL", sql)     # 二重廃棄しない
        self.assertNotIn("DELETE", sql)

    def test_batch_runs_keeps_watermark_holder(self):
        """watermarkを持つ最新の成功runは消さない(消すと収集が巻き戻る)。"""
        sql = kanribo.dispose_sql(rec(category="unyou-run", location="DB batch_runs"), 5)
        self.assertIn("DELETE FROM batch_runs", sql)
        self.assertIn("max(watermark_turn_id)", sql)
        self.assertIn("b.finished_at IS NOT NULL", sql)  # 実行中のrunは消さない

    def test_generic_delete_is_range_and_key_bound(self):
        sql = kanribo.dispose_sql(rec(category="renraku-msg", location="DB messages"), 5)
        self.assertIn("DELETE FROM messages", sql)
        self.assertIn("BETWEEN 10 AND 400", sql)
        self.assertIn("'proj'", sql)

    def test_measure_guard(self):
        """措置の取り違えを本体で止める(呼び出し側の絞り込みに依存しない)。"""
        with self.assertRaises(ValueError):
            kanribo.dispose_sql(rec(measure="ikan"), 5)          # 移管を削除しない
        with self.assertRaises(ValueError):
            kanribo.ikan_delete_sql(rec(measure="haiki"))
        with self.assertRaises(ValueError):
            kanribo.haiki_items(rec(measure="jouyou"))           # 常用を廃棄しない

    def test_survivors_only_for_turns(self):
        self.assertIn("current_facts", kanribo.survivors_sql(rec()))
        self.assertEqual(kanribo.survivors_sql(rec(category="shuju-raw")), "")

    def test_haiki_items_state_scope(self):
        items = kanribo.haiki_items(rec(), survivors=3)
        joined = " / ".join(items)
        self.assertIn("120件", joined)
        self.assertIn("id 10〜400", joined)
        self.assertIn("2030-03-31", joined)
        self.assertIn("3件は現用の事実の根拠", joined)


class HaikiHarness(Harness):
    """廃棄伺い(ringi_haiki / execute_haiki_doc)用のフェイクDB。"""

    def __init__(self, files=None, rules=None, state="manryou", fresh_backup=True,
                 dispose_count=118):
        super().__init__()
        self.mod._KANRIBO_OK = True
        self.files = files if files is not None else [rec()]
        self.rules = rules if rules is not None else [
            {"category": "shuju-turns", "measure": "haiki", "gate": "kessai",
             "retention_days": 0, "retention_years": 3}]
        self.state = state
        self.dispose_count = dispose_count
        self.fresh = fresh_backup

    def fake_psql(self, sql):
        if "to_regclass('public.record_files')" in sql:
            return "1"
        if "FROM retention_rules WHERE enabled" in sql:
            return json.dumps(self.rules, ensure_ascii=False)
        if "FROM record_files WHERE state = 'manryou'" in sql:
            self.sqls.append(sql)
            return json.dumps(self.files, ensure_ascii=False)
        if "FROM record_files WHERE id =" in sql:
            self.sqls.append(sql)
            return json.dumps([dict(self.files[0], state=self.state)], ensure_ascii=False)
        if "EXISTS (SELECT 1 FROM current_facts" in sql:   # survivors
            self.sqls.append(sql)
            return "2"
        if sql.startswith("WITH d AS"):                     # 廃棄の実行
            self.sqls.append(sql)
            return str(self.dispose_count)
        return super().fake_psql(sql)

    def run_haiki(self):
        # Harness.ctx() は contextmanager なので、追加のpatchは入れ子にする
        with self.ctx(), mock.patch.object(self.mod, "backup_is_fresh",
                                           return_value=self.fresh):
            return self.mod.ringi_haiki(run_id=9)


class TestRingiHaiki(unittest.TestCase):
    def _scripts(self, h, shinsa="joshin", kessai="approve"):
        h.scripts["shinsa-haiki"] = [{"action": shinsa, "memo": "満了確認"}]
        h.scripts["kessai-haiki"] = [{"action": kessai, "memo": "廃棄可"}]

    def test_kessai_gate_stops_at_joshin(self):
        """人間の決裁が条件の分類は、審査の上申で止める(LLM決裁を呼ばない)。"""
        h = HaikiHarness()
        self._scripts(h)
        self.assertEqual(h.run_haiki(), 1)
        self.assertTrue(h.sqls_like("state='pending_decision'"))  # 上申で停止
        self.assertEqual(h.sqls_like("decision_class="), [])      # 決裁していない
        self.assertEqual(h.sqls_like("WITH d AS"), [])            # 消していない
        self.assertEqual(h.sqls_like("executed_at=now()"), [])    # 施行していない
        self.assertEqual([a for a in h.asks if a[0].startswith("kessai")], [])
        drafts = h.sqls_like("INSERT INTO drafts")
        self.assertIn("'haiki'", drafts[0])
        self.assertIn("廃棄一覧", drafts[0])

    def test_sokujiko_gate_executes(self):
        """決裁で施行する分類は、その場で廃棄まで進む。"""
        h = HaikiHarness(rules=[{"category": "shuju-turns", "measure": "haiki",
                                 "gate": "sokujiko", "retention_days": 0,
                                 "retention_years": 3}])
        self._scripts(h)
        h.run_haiki()
        self.assertTrue(h.sqls_like("DELETE FROM turns"))
        self.assertTrue(h.sqls_like("executed_at=now()"))
        self.assertIn("state = 'haiki_zumi'", "\n".join(h.sqls_like("UPDATE record_files")))

    def test_shinsa_hiketsu_keeps_data(self):
        h = HaikiHarness()
        self._scripts(h, shinsa="hiketsu")
        h.run_haiki()
        self.assertTrue(h.sqls_like("state='rejected'"))
        self.assertEqual(h.sqls_like("WITH d AS"), [])
        self.assertEqual([a for a in h.asks if a[0].startswith("kessai")], [])

    def test_kessai_hiketsu_keeps_data(self):
        h = HaikiHarness(rules=[{"category": "shuju-turns", "measure": "haiki",
                                 "gate": "sokujiko", "retention_days": 0,
                                 "retention_years": 3}])
        self._scripts(h, kessai="hiketsu")
        h.run_haiki()
        self.assertTrue(h.sqls_like("state='rejected'"))
        self.assertEqual(h.sqls_like("WITH d AS"), [])

    def test_no_backup_blocks_execution(self):
        """当日のバックアップが無ければ施行しない(廃棄は取り消せない)。"""
        h = HaikiHarness(fresh_backup=False,
                         rules=[{"category": "shuju-turns", "measure": "haiki",
                                 "gate": "sokujiko", "retention_days": 0,
                                 "retention_years": 3}])
        self._scripts(h)
        h.run_haiki()   # 件単位で握りつぶす: runは落とさない
        self.assertEqual(h.sqls_like("DELETE FROM turns"), [])

    def test_ikan_files_are_not_disposed(self):
        h = HaikiHarness(files=[rec(measure="ikan")])
        self.assertEqual(h.run_haiki(), 0)
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_disabled_category_is_skipped(self):
        h = HaikiHarness(rules=[])   # 規程が無効
        self.assertEqual(h.run_haiki(), 0)
        self.assertEqual(h.sqls_like("INSERT INTO drafts"), [])

    def test_already_disposed_file_only_updates_state(self):
        h = HaikiHarness(state="haiki_zumi",
                         rules=[{"category": "shuju-turns", "measure": "haiki",
                                 "gate": "sokujiko", "retention_days": 0,
                                 "retention_years": 3}])
        self._scripts(h)
        h.run_haiki()
        self.assertEqual(h.sqls_like("WITH d AS"), [])          # 二重に消さない
        self.assertTrue(h.sqls_like("executed_at=now()"))       # 状態だけ追いつかせる


class TestIkanSql(unittest.TestCase):
    def test_export_is_range_and_key_bound(self):
        sql = kanribo.export_sql(rec(measure="ikan"))
        self.assertIn("to_jsonb(x)", sql)
        self.assertIn("BETWEEN 10 AND 400", sql)
        self.assertIn("'proj'", sql)

    def test_export_general_key_has_no_key_condition(self):
        sql = kanribo.export_sql(rec(category="shuju-raw", project_key="general"))
        self.assertIn("FROM raw_payloads", sql)
        self.assertNotIn("= 'general'", sql)   # キーが定数の分類は範囲だけで絞る

    def test_delete_after_export(self):
        sql = kanribo.ikan_delete_sql(rec(category="kessai-doc", measure="ikan",
                                         location="DB drafts"))
        self.assertIn("DELETE FROM drafts", sql)
        self.assertIn("BETWEEN 10 AND 400", sql)

    def test_archive_path_is_year_scoped_and_safe(self):
        p = kanribo.archive_path(rec(category="kessai-doc", project_key="github.com/x/y"))
        self.assertEqual(p, "2026/kessai-doc_github.com-x-y_2026.jsonl.gz")


class IkanHarness(HaikiHarness):
    def __init__(self, archive_dir, **kw):
        kw.setdefault("files", [rec(category="kessai-doc", measure="ikan",
                                    location="DB drafts", name="決裁文書 proj 令和8年度")])
        kw.setdefault("rules", [{"category": "kessai-doc", "measure": "ikan",
                                 "gate": "sokujiko", "retention_days": 0,
                                 "retention_years": 10}])
        super().__init__(**kw)
        self.mod.ARCHIVE_DIR = archive_dir
        self.rows = [{"id": 11, "content": "決裁の中身", "created_at": "2026-04-02"},
                     {"id": 12, "content": "その2", "created_at": "2026-04-03"}]

    def fake_psql(self, sql):
        if "to_jsonb(x)" in sql:
            self.sqls.append(sql)
            return json.dumps(self.rows, ensure_ascii=False)
        return super().fake_psql(sql)

    def run_ikan(self):
        with self.ctx(), mock.patch.object(self.mod, "backup_is_fresh",
                                           return_value=self.fresh):
            return self.mod.ringi_ikan(run_id=9)


class TestRingiIkan(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil
        self.dir = Path(tempfile.mkdtemp(prefix="archive-test-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_export_then_delete_and_roundtrip(self):
        """移管は中身を書き出してからDBを外す。書き出しは読み戻せる。"""
        h = IkanHarness(self.dir)
        h.scripts["kessai-ikan"] = [{"action": "approve", "memo": "移管可"}]
        self.assertEqual(h.run_ikan(), 1)
        out = self.dir / "2026" / "kessai-doc_proj_2026.jsonl.gz"
        self.assertTrue(out.is_file())
        import gzip
        got = [json.loads(x) for x in
               gzip.open(out, "rt", encoding="utf-8").read().splitlines()]
        self.assertEqual(got, h.rows)                 # 往復して一致
        self.assertTrue(h.sqls_like("DELETE FROM drafts"))
        self.assertIn("state = 'ikan_zumi'", "\n".join(h.sqls_like("UPDATE record_files")))
        self.assertNotIn(".tmp", "".join(str(p) for p in self.dir.rglob("*")))

    def test_hiketsu_keeps_rows_in_db(self):
        h = IkanHarness(self.dir)
        h.scripts["kessai-ikan"] = [{"action": "hiketsu", "memo": "まだ置く"}]
        h.run_ikan()
        self.assertEqual(h.sqls_like("DELETE FROM drafts"), [])
        self.assertTrue(h.sqls_like("state='rejected'"))

    def test_kessai_gate_stops_before_delete(self):
        h = IkanHarness(self.dir, rules=[{"category": "kessai-doc", "measure": "ikan",
                                          "gate": "kessai", "retention_days": 0,
                                          "retention_years": 10}])
        h.run_ikan()
        self.assertEqual(h.sqls_like("DELETE FROM drafts"), [])   # 人間の決裁待ち
        self.assertTrue(h.sqls_like("state='pending_decision'"))  # 上申で停止
        self.assertEqual(h.sqls_like("decision_class="), [])      # 決裁していない
        self.assertEqual([a for a in h.asks if a[0].startswith("kessai")], [])

    def test_haiki_files_are_not_migrated(self):
        h = IkanHarness(self.dir, files=[rec(measure="haiki")])
        self.assertEqual(h.run_ikan(), 0)


class TestAtomicityAndRollback(unittest.TestCase):
    def test_dispose_and_record_is_one_statement(self):
        """実行と管理簿の記載を1文にする(消えたのに管理簿が現用、を作らない)。"""
        sql = kanribo.dispose_and_record_sql(rec(), draft_id=5)
        self.assertEqual(sql.count(";"), 1)
        self.assertIn("DELETE FROM turns", sql)
        self.assertIn("UPDATE record_files SET state = 'haiki_zumi'", sql)
        self.assertIn("n_rows = (SELECT count(*) FROM d)", sql)   # 実際に消えた件数
        self.assertIn("WHERE id = 7", sql)

    def test_dispose_and_record_for_ikan(self):
        sql = kanribo.dispose_and_record_sql(rec(category="kessai-doc", measure="ikan",
                                                 location="DB drafts"), 5,
                                             state="ikan_zumi")
        self.assertEqual(sql.count(";"), 1)
        self.assertIn("DELETE FROM drafts", sql)
        self.assertIn("'ikan_zumi'", sql)

    def test_unfile_only_touches_manryou(self):
        sql = kanribo.unfile_sql(7)
        self.assertIn("disposed_draft = NULL", sql)
        self.assertIn("state = 'manryou'", sql)   # 施行済みの記載は戻さない

    def test_abort_restores_unfiled_state(self):
        """処理中断は決着ではないので、起票を取り消して翌晩また拾えるようにする。"""
        h = HaikiHarness()
        h.scripts["shinsa-haiki"] = []     # 応答が尽きて例外になる
        h.run_haiki()                      # 件単位で握りつぶす
        self.assertTrue(h.sqls_like("disposed_draft = NULL"))

    def test_hiketsu_keeps_filed_marker(self):
        """否決は決着なので起票の印を残す(毎晩むやみに再起票しない)。"""
        h = HaikiHarness()
        h.scripts["shinsa-haiki"] = [{"action": "hiketsu", "memo": "保存継続"}]
        h.scripts["kessai-haiki"] = []
        h.run_haiki()
        self.assertEqual(h.sqls_like("disposed_draft = NULL"), [])


if __name__ == "__main__":
    unittest.main()
