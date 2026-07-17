#!/usr/bin/env python3
"""purge — 収集済みプロジェクトの事後除外(設計書§8.3)

usage: purge.py --project <project_key> [--yes]

該当project_keyの行を turns / raw_payloads / auto_memory_snapshots / facts /
backfill_progress から削除し、配布リポジトリの memory/<dir>/ を撤去してpushする。
append-only原則の例外その2(redactと並ぶ)として、操作ログを purge.log に残す。

注意:
- generalスコープへ昇格した事実は消さない(他プロジェクト由来の知識と混在するため)。
  provenanceが該当プロジェクトのturnsを指すgeneral事実は件数と共に警告表示するので、
  必要なら手動で整理する
- 実行前に sync-exclude.txt へ該当キーを追加しておくこと(再収集の防止)
"""
import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nightly import (GIT_ENV, REPO_DIR, SYSTEM_DIR, project_dir_name, psql,
                     psql_json, pull_repo, q)

sys.path.insert(0, str(SYSTEM_DIR / "ingest"))
try:
    from exclude import normalize_project_key
except ImportError:  # ingest未配置でも動く(raw_payloadsの突き合わせだけ精度が落ちる)
    normalize_project_key = None


def collect_raw_ids(key: str) -> list:
    """該当project_keyに帰属する raw_payloads の id を集める。

    turns.payload_id 経由に加え、turnsに1行も残らなかったペイロード
    (パース失敗・全行スキップ)もpayload内のcwd/remoteから正規化して拾う。
    """
    ids = {int(r["id"]) for r in psql_json(
        f"SELECT json_agg(json_build_object('id', payload_id)) FROM "
        f"(SELECT DISTINCT payload_id FROM turns "
        f" WHERE project_key={q(key)} AND payload_id IS NOT NULL) t;")}
    if normalize_project_key is not None:
        rows = psql_json(
            "SELECT json_agg(json_build_object('id', id, 'u', payload->>'git_remote_url', "
            "'d', payload->>'project_dir')) FROM raw_payloads;")
        for r in rows:
            if normalize_project_key(r.get("u"), r.get("d")) == key:
                ids.add(int(r["id"]))
    return sorted(ids)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="purge対象のproject_key(完全一致)")
    ap.add_argument("--yes", action="store_true", help="確認プロンプトを省略")
    args = ap.parse_args()
    key = args.project

    n_turns = int(psql(f"SELECT count(*) FROM turns WHERE project_key={q(key)};"))
    n_mem = int(psql(f"SELECT count(*) FROM auto_memory_snapshots WHERE project_key={q(key)};"))
    n_facts = int(psql(f"SELECT count(*) FROM facts WHERE project_key={q(key)};"))
    raw_ids = collect_raw_ids(key)
    n_general = int(psql(
        f"SELECT count(*) FROM facts WHERE project_key='general' AND provenance && "
        f"(SELECT coalesce(array_agg(id), ARRAY[]::bigint[]) FROM turns WHERE project_key={q(key)});"))

    print(f"purge対象 project_key={key}")
    print(f"  turns={n_turns} raw_payloads={len(raw_ids)} "
          f"auto_memory={n_mem} facts={n_facts}")
    if n_turns + n_mem + n_facts + len(raw_ids) == 0:
        print("対象がありません")
        return
    if n_general:
        print(f"  警告: このプロジェクトのturnsを根拠に持つgeneral事実が{n_general}件あります"
              f"(削除しません。必要なら手動で整理)")
    if not args.yes:
        ans = input("削除しますか? [yes/N] ")
        if ans.strip().lower() != "yes":
            print("中止しました")
            return

    raw_list = ",".join(map(str, raw_ids)) or "0"
    # 1トランザクションで削除。他プロジェクトの事実が該当factsをreplacesで
    # 参照している場合はFK違反にならないよう系譜を切ってから消す
    psql(f"""
BEGIN;
UPDATE facts SET replaces = NULL
 WHERE replaces IN (SELECT id FROM facts WHERE project_key={q(key)})
   AND project_key <> {q(key)};
DELETE FROM facts WHERE project_key={q(key)};
DELETE FROM turns WHERE project_key={q(key)};
DELETE FROM auto_memory_snapshots WHERE project_key={q(key)};
DELETE FROM raw_payloads WHERE id IN ({raw_list});
DELETE FROM backfill_progress WHERE project_key={q(key)};
COMMIT;
""")

    # 配布済みindexの撤去
    pull_repo()
    index_dir = REPO_DIR / "memory" / project_dir_name(key)
    removed = index_dir.is_dir()
    if removed:
        shutil.rmtree(index_dir)
        subprocess.run(["git", "-C", str(REPO_DIR), "add", "-A", "memory"],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", str(REPO_DIR)] + GIT_ENV +
                       ["commit", "-q", "-m", f"purge {key}: index撤去"],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", str(REPO_DIR), "push", "-q"], check=True, timeout=120)

    with open(SYSTEM_DIR / "batch" / "purge.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%F %T')} purge project={key} "
                f"turns={n_turns} raw={len(raw_ids)} memories={n_mem} "
                f"facts={n_facts} index_removed={removed}\n")
    print(f"完了: turns={n_turns} raw={len(raw_ids)} memories={n_mem} facts={n_facts} "
          f"index{'撤去' if removed else '無し'}")
    print("再収集を防ぐため sync-exclude.txt への追加を忘れずに")


if __name__ == "__main__":
    main()
