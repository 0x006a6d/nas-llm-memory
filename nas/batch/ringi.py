#!/usr/bin/env python3
"""起案・回議・決裁(公文書ワークフロー)の共通部品 — 012_ringi.sql と対

- 採番: 年度(4/1区切り)×年度内連番。INSERT 1文で採番する(書き手はflock下のバッチのみ)
- 状態機械: TRANSITIONS が遷移定義の正。SQLビルダは from_state ガード付きUPDATEを組む
- 伺い文: 公文書風テンプレート(LLMは使わない。回議録・決裁欄はUIが draft_log から描く)
- 専決規程: config.json の roles/ringi の解釈(model_for / ringi_settings)

このモジュールはDBへ接続しない(SQL文字列を返すだけ)。実行は呼び出し側の
psql(nightly.py)や dashboard が行う。nightly からimportされるため、循環を避けて
nightly には依存しない。
"""
import json

# ---------------------------------------------------------------- 専決規程(config.json)

RINGI_DEFAULTS = {
    "enabled": False,            # falsyなら従来パイプラインのまま(移行スイッチ)
    "trial": False,              # 第1期試行(起案モデルの並行比較)
    "trial_models": ["claude-haiku-4-5-20251001", "claude-sonnet-5"],  # 試行する起案候補
    "trial_budget_min": 25,      # 試行の合計所要時間の上限(分)。超過分のモデルは翌晩以降に回す
    "max_hosei_rounds": 2,       # 審査→起案者の補正往復の上限。超過は廃案
    "max_kessai_rounds": 1,      # 決裁→審査の差し戻し往復の上限。超過は廃案
    "skill_min_count": 2,        # skills-candidates を起票する検出回数の下限
    "skill_auto_execute": False, # true: skillも決裁即施行(後閲印を施行条件にしない)
    "index_delete_ratio": 0.3,   # index改定で削除行がこの割合を超えたら上申
    "max_miketsu_nights": 3,     # 決裁不能案件を未決のまま繰り越す上限(超過で廃案)
}


def model_for(config: dict, role: str) -> str:
    """役割(kian/shinsa/kessai/enrich)の担当モデル。未定義は共通modelへフォールバック。"""
    roles = config.get("roles") or {}
    v = roles.get(role)
    return str(v) if v else str(config.get("model") or "")


def ringi_settings(config: dict) -> dict:
    """ringi設定を既定値とマージして返す(未知キーは無視)。"""
    given = config.get("ringi") or {}
    return {k: given.get(k, d) for k, d in RINGI_DEFAULTS.items()}


# ---------------------------------------------------------------- 採番・文書番号

def fiscal_year(d) -> int:
    """date/datetime → 年度(4/1区切り)。"""
    return d.year - (1 if d.month < 4 else 0)


def reiwa(fy: int) -> int:
    """年度 → 令和年(令和元年=2019年度)。"""
    return fy - 2018


def display_doc_no(fy: int, seq: int) -> str:
    """表示用文書番号。DBのdoc_noは機械形式('2026-0012')でこちらはUI用。

    令和元年度は「令和1年度」ではなく「令和元年度」と書く(公用文の表記)。
    """
    n = reiwa(fy)
    return f"記憶第{seq}号(令和{'元' if n == 1 else n}年度)"


# ---------------------------------------------------------------- 状態機械

# (現状態, action) -> 次状態。ここが遷移定義の正(012のCHECKは値の妥当性のみ)
TRANSITIONS = {
    ("pending_review", "shinsa_ok"): "approved",            # 軽易案件は課長専決
    ("pending_review", "joshin"): "pending_decision",       # 重要案件(置換/撤回/矛盾)は上申
    ("pending_review", "sashimodoshi"): "remanded_to_drafter",
    ("pending_review", "hiketsu"): "rejected",
    ("remanded_to_drafter", "hosei"): "pending_review",     # 補正して再回議
    ("remanded_to_drafter", "hiketsu"): "rejected",         # 補正往復の上限超過で廃案
    ("pending_decision", "kessai_ok"): "approved",
    ("pending_decision", "sashimodoshi"): "remanded_to_reviewer",
    ("pending_decision", "hiketsu"): "rejected",
    ("remanded_to_reviewer", "shinsa_ok"): "approved",      # 再審査で軽易化すれば専決
    ("remanded_to_reviewer", "joshin"): "pending_decision",
    ("remanded_to_reviewer", "hiketsu"): "rejected",
    ("approved", "shiko"): "executed",
    ("approved", "sashimodoshi"): "rejected",               # 後閲待ちskillへの差し戻し=廃案
    ("executed", "sashimodoshi"): "reexamine",              # 人間の後閲差し戻し(翌晩再審理)
    ("reexamine", "saishinri"): "executed",                 # 再審理完了(是正文書は別途起票)
}

# 承認actionが決める決裁区分(専決規程)
DECISION_CLASS_BY_ACTION = {"shinsa_ok": "senketsu", "kessai_ok": "bucho"}


def next_state(state: str, action: str) -> str:
    to = TRANSITIONS.get((state, action))
    if to is None:
        raise ValueError(f"invalid transition: {state} --{action}-->")
    return to


# ---------------------------------------------------------------- SQLビルダ

def q(s) -> str:
    """SQL文字列リテラル用エスケープ(nightly.qと同一。循環import回避の重複)。"""
    return "'" + str(s).replace("'", "''") + "'"


def jsonb(obj) -> str:
    return q(json.dumps(obj, ensure_ascii=False)) + "::jsonb"


def insert_draft_sql(*, kind: str, project_key: str, title: str, proposal: str,
                     payload, created_by: str, fy: int, related_doc=None) -> str:
    """起案文書の起票(採番込みの1文)。RETURNING id, doc_no。

    採番は同一文内の集約で行う: 書き手はflock下のバッチのみで競合しない
    (UNIQUE(fiscal_year, seq) が最終防衛)。
    doc_noのlpadは4桁を下限に桁あふれでも切り詰めない(lpadは幅超過を切るため)。
    起票の初期状態は pending_review 固定(呼び出し側から任意の状態で作らせない。
    以後の状態は必ず transition_sql のガード付きUPDATEを通す)。
    """
    if kind not in ("fact", "skill", "index", "saishinri", "haiki", "ikan", "tenken"):
        raise ValueError(f"invalid kind: {kind}")
    fy = int(fy)
    rel = str(int(related_doc)) if related_doc is not None else "NULL"
    return (
        f"INSERT INTO drafts (fiscal_year, seq, doc_no, kind, project_key, "
        f"title, proposal, payload, state, related_doc, created_by) "
        f"SELECT {fy}, s.n, {fy} || '-' || lpad(s.n::text, greatest(4, length(s.n::text)), '0'), "
        f"{q(kind)}, {q(project_key)}, {q(title)}, {q(proposal)}, {jsonb(payload)}, "
        f"'pending_review', {rel}, {q(created_by)} "
        f"FROM (SELECT coalesce(max(seq), 0) + 1 AS n FROM drafts WHERE fiscal_year = {fy}) s "
        f"RETURNING id, doc_no;"
    )


def log_sql(draft_id: int, actor: str, action: str, created_by: str,
            memo=None, payload=None) -> str:
    """回議録への記帳。"""
    memo_sql = q(memo) if memo else "NULL"
    payload_sql = jsonb(payload) if payload is not None else "NULL"
    return (
        f"INSERT INTO draft_log (draft_id, actor, action, memo, payload, created_by) "
        f"VALUES ({int(draft_id)}, {q(actor)}, {q(action)}, {memo_sql}, {payload_sql}, "
        f"{q(created_by)});"
    )


def transition_sql(draft_id: int, from_state: str, action: str) -> str:
    """状態遷移(from_stateガード付き)。RETURNING id — 空なら状態が想定と違う(競合)。

    付随列も遷移に応じて更新する:
    - 承認(shinsa_ok/kessai_ok): decided_at と決裁区分(専決規程で自動決定)
    - 施行(shiko): executed_at
    - 後閲差し戻し(executed→reexamine): seen_state='remanded'
    - 再審理完了(saishinri): seen_state='seen'(後閲対応済み)
    """
    to = next_state(from_state, action)
    sets = [f"state={q(to)}"]
    if action in DECISION_CLASS_BY_ACTION:
        sets.append("decided_at=now()")
        sets.append(f"decision_class={q(DECISION_CLASS_BY_ACTION[action])}")
    if action == "shiko":
        sets.append("executed_at=now()")
    if from_state == "executed" and action == "sashimodoshi":
        sets.append("seen_state='remanded'")
        sets.append("seen_at=now()")
    if action == "saishinri":
        sets.append("seen_state='seen'")
    return (
        f"UPDATE drafts SET {', '.join(sets)} "
        f"WHERE id = {int(draft_id)} AND state = {q(from_state)} RETURNING id;"
    )


def link_facts_sql(draft_id: int, fact_ids: list) -> str:
    """施行した文書と登載factsの紐付け。"""
    ids = ",".join(str(int(f)) for f in fact_ids)
    return (
        f"INSERT INTO draft_facts (draft_id, fact_id) "
        f"SELECT {int(draft_id)}, f FROM unnest(ARRAY[{ids}]::bigint[]) AS f "
        f"ON CONFLICT DO NOTHING;"
    )


# ---------------------------------------------------------------- 伺い文テンプレート

_TITLES = {
    "fact": "プロジェクト {project} に係る事実の登載について(伺い)",
    "index": "プロジェクト {project} の index 改定について(伺い)",
    "skill": "スキル「{name}」の登載について(伺い)",
    "saishinri": "文書 {doc_no} の後閲差し戻しに伴う是正について(伺い)",
    "haiki": "行政文書ファイル「{name}」の廃棄について(伺い)",
    "ikan": "行政文書ファイル「{name}」の移管について(伺い)",
    "tenken": "{period} の文書管理状況について(報告)",
}

_ASKS = {
    "fact": "標記について、下記のとおり事実層(facts)に登載してよろしいか。",
    "index": "標記について、下記のとおり index を改定してよろしいか。",
    "skill": "標記について、下記のスキルを skills 本体に登載してよろしいか。",
    "saishinri": "標記について、後閲による差し戻しを受け、下記のとおり是正してよろしいか。",
    "haiki": "標記について、保存期間が満了したので、下記のとおり廃棄してよろしいか。",
    "ikan": "標記について、保存期間が満了したので、下記のとおり移管してよろしいか。",
    "tenken": "標記について、下記のとおり報告する。",
}


def build_title(kind: str, **kw) -> str:
    return _TITLES[kind].format(**kw)


def build_proposal(kind: str, items: list, appendices: list = ()) -> str:
    """伺い文本文。items=「記」の箇条、appendices=[(見出し, 本文), ...](別記)。

    別記の本文はリストなら番号付き箇条書き、文字列なら整形済みブロック
    (diff等)としてそのまま載せる。
    文書番号・起案日・回議録は本文に含めない(採番は同一INSERT内、履歴の正はdraft_log。
    表示はUIが drafts/draft_log の列から組む)。
    """
    lines = [_ASKS[kind], "", "記", ""]
    lines += [f"{i}. {item}" for i, item in enumerate(items, 1)]
    for n, (heading, body) in enumerate(appendices, 1):
        lines += ["", f"別記第{n}({heading})"]
        if isinstance(body, str):
            lines.append(body)
        else:
            lines += [f" {i}. {b}" for i, b in enumerate(body, 1)]
    return "\n".join(lines) + "\n"
