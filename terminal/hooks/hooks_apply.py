#!/usr/bin/env python3
"""hooks-manifest.json を Claude Code / Codex の実設定へ展開する。

設計:
  - 正本は claude-config/hooks/hooks-manifest.json(非公開・git pull で全端末配布)。
    このスクリプト自体はコード(公開リポ nas-llm-memory が正本)で、個人設定を含まない。
  - 「manifest に書く → 各端末が SessionStart(sync_worker)で自動適用」が基本経路。
    手動実行も可: python3 hooks_apply.py [--dry-run] [--quiet]
  - 管理台帳 ~/.claude/hooks-manifest-state.json に「自分が書き込んだエントリ」を記録し、
    次回適用時にそれだけを消してから再展開する(手書き・プラグインのフックには触れない)。
    manifest と同一の既存エントリは追加せず台帳に取り込む(adoption)。
  - 書き込み前に <file>.bak-日時 へ退避。変更が無ければ書き込まない。

Codex 向け変換(Claude Code と仕様が違う箇所):
  - SessionEnd → Stop で代替(ターン毎に発火するため冪等なスクリプト向け)
  - "if" 条件 → 判定ラッパースクリプトを ~/.codex/hooks/ に生成
  - matcher は正規表現なので単語は ^word$ に包む

manifest スキーマ(hooks-manifest.example.json 参照):
  {"hooks": [{"name": str, "event": str, "command": str($HOME 展開可),
              "targets": ["claude"|"codex", ...],
              "matcher": str|null, "if": str|null, "timeout": int|null}]}
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
HOOKS_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = Path(os.environ.get("HOOKS_MANIFEST") or HOOKS_DIR / "hooks-manifest.json")
STATE_PATH = HOME / ".claude" / "hooks-manifest-state.json"
CODEX_HOOKS_PATH = HOME / ".codex" / "hooks.json"
CODEX_WRAP_DIR = HOME / ".codex" / "hooks"

# 両 CLI に存在するイベント(Codex 0.144.x 実機+公式ドキュメントで確認)
BOTH_EVENTS = {
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PermissionRequest", "Stop", "SubagentStart", "SubagentStop", "PreCompact",
}
CLAUDE_EVENTS = BOTH_EVENTS | {"SessionEnd", "Notification", "StopFailure"}

WRAPPER_TEMPLATE = '''#!/usr/bin/env python3
"""hooks_apply が生成した "if" 条件ラッパー(元: {condition})。

Codex の PreToolUse は "if" 条件をサポートしないため、stdin の
tool_input.command を glob 照合してから元コマンドを実行する。
"""
import fnmatch
import json
import re
import subprocess
import sys

PATTERN = {pattern!r}
ORIG = {orig!r}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    cmd = (payload.get("tool_input") or {{}}).get("command") or ""
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    rx = fnmatch.translate(PATTERN).replace(r"\\Z", "")  # 部分一致(Claude の if と同挙動)
    if not re.search(rx, cmd):
        return 0
    p = subprocess.run(ORIG, shell=True, capture_output=True, text=True)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
'''


class UnsupportedHook(Exception):
    pass


def _claude_settings_path():
    return Path(os.path.realpath(HOME / ".claude" / "settings.json"))


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _read_json_strict(path, default):
    """(data, ok)。ファイルが無い→(default, True)。有るが壊れている→(None, False)。

    壊れた設定を default で上書きすると既存設定・適用済みフックを失うため、
    apply はこの ok=False を見てそのターゲットをスキップする。
    """
    if not Path(path).is_file():
        return default, True
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), True
    except (OSError, ValueError):
        return None, False


def load_manifest():
    return _read_json(MANIFEST_PATH, {"hooks": []})


def save_manifest(data):
    if MANIFEST_PATH.is_file():
        _backup(MANIFEST_PATH)
    MANIFEST_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _backup(path):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = Path(f"{path}.bak-{ts}")
    bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return str(bak)


def _expand(cmd):
    return cmd.replace("${HOME}", str(HOME)).replace("$HOME", str(HOME))


def render(hook, target, write_wrapper=False):
    """manifest エントリを target 用に変換する。(event, entry, notes) を返す。

    write_wrapper=False では "if" ラッパーの内容は生成せずパスだけ決める(dry-run/状態表示用)。
    """
    event = hook.get("event")
    raw_cmd = hook.get("command")
    if not event or not raw_cmd:
        raise UnsupportedHook("event / command が未定義")
    cmd = _expand(raw_cmd)
    matcher = hook.get("matcher")
    cond = hook.get("if")
    notes = []

    if target == "claude":
        if event not in CLAUDE_EVENTS:
            raise UnsupportedHook(f"{event} は Claude Code に無いイベント")
        h = {"type": "command", "command": cmd}
        if cond:
            h["if"] = cond
        if hook.get("timeout"):
            h["timeout"] = hook["timeout"]
        entry = {"hooks": [h]}
        if matcher:
            entry["matcher"] = matcher
        return event, entry, notes

    # target == "codex"
    if cond:
        m = re.match(r"^(\w+)\((.+)\)$", cond)
        if not m:
            raise UnsupportedHook(f'if 条件を変換できない: {cond}')
        tool, pattern = m.group(1), m.group(2)
        # cond も含める: 同一 event+cmd で if 条件だけ違うフック同士の衝突を防ぐ
        slug = hashlib.sha1(f"{event}|{cmd}|{cond}".encode()).hexdigest()[:8]
        wrapper = CODEX_WRAP_DIR / f"manifest_wrap_{slug}.py"
        if write_wrapper:
            CODEX_WRAP_DIR.mkdir(parents=True, exist_ok=True)
            wrapper.write_text(WRAPPER_TEMPLATE.format(
                condition=cond, pattern=pattern, orig=cmd), encoding="utf-8")
        cmd, matcher = f"python3 {wrapper}", tool
        notes.append('"if" 条件をラッパースクリプト化')
    if event == "SessionEnd":
        event = "Stop"
        notes.append("SessionEnd が無いため Stop で代替(ターン毎に発火)")
    elif event not in BOTH_EVENTS:
        raise UnsupportedHook(f"{event} は Codex に代替イベントが無い")

    h = {"type": "command", "command": cmd}
    if hook.get("timeout"):
        h["timeout"] = hook["timeout"]
    entry = {"hooks": [h]}
    if matcher and matcher not in ("*", ""):
        entry["matcher"] = f"^{matcher}$" if re.fullmatch(r"\w+", matcher) else matcher
    return event, entry, notes


def _iter_commands(hooks_dict):
    for ev, groups in (hooks_dict or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                yield ev, h.get("command", "")


def manifest_status():
    """dashboard 用: manifest 各行の適用状態。設定は書き換えない。"""
    manifest = load_manifest()
    actual = {
        "claude": dict.fromkeys(
            (ev, c) for ev, c in _iter_commands(
                _read_json(_claude_settings_path(), {}).get("hooks"))),
        "codex": dict.fromkeys(
            (ev, c) for ev, c in _iter_commands(
                _read_json(CODEX_HOOKS_PATH, {}).get("hooks"))),
    }
    rows = []
    for i, hook in enumerate(manifest.get("hooks", [])):
        row = {"index": i, "name": hook.get("name", "?"), "event": hook.get("event", "?"),
               "command": hook.get("command", ""), "matcher": hook.get("matcher"),
               "if": hook.get("if"), "timeout": hook.get("timeout"),
               "targets": hook.get("targets", []), "state": {}, "notes": {}}
        for target in ("claude", "codex"):
            if target not in row["targets"]:
                row["state"][target] = "off"
                continue
            try:
                ev, entry, notes = render(hook, target)
                cmd = entry["hooks"][0]["command"]
                applied = (ev, cmd) in actual[target]
                row["state"][target] = "applied" if applied else "pending"
                row["notes"][target] = notes
            except UnsupportedHook as e:
                row["state"][target] = "unsupported"
                row["notes"][target] = [str(e)]
        rows.append(row)
    return {"path": str(MANIFEST_PATH), "exists": MANIFEST_PATH.is_file(), "rows": rows}


def apply_manifest(dry_run=False):
    """manifest を両設定へ展開する。report(dict)を返す。

    sync_worker(SessionStart)と dashboard の同時適用で read-modify-write が
    交錯しないよう、~/.claude-spool のロックで直列化する(sync_worker と同じ流儀)。
    """
    import fcntl
    lock_dir = HOME / ".claude-spool"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / ".hooks_apply.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _apply_manifest_locked(dry_run)


def _apply_manifest_locked(dry_run=False):
    manifest = load_manifest()
    state = _read_json(STATE_PATH, {})
    report = {"changed": [], "adopted": [], "added": [], "removed": [],
              "skipped": [], "backups": []}

    targets = {"claude": _claude_settings_path(), "codex": CODEX_HOOKS_PATH}
    for target, path in targets.items():
        default = {} if target == "claude" else {"hooks": {}}
        data, ok = _read_json_strict(path, default)
        if not ok:
            report["skipped"].append(
                f"{target}: 設定ファイルが不正なJSONのため触らずスキップ({path})")
            continue
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        tr = {"added": [], "removed": [], "adopted": [], "skipped": []}
        hooks_dict = data.setdefault("hooks", {})

        # 1) 前回このツールが書いたエントリを消す(それ以外には触れない)
        prev = {tuple(x) for x in state.get(target, [])}
        for ev in list(hooks_dict):
            groups = []
            for g in hooks_dict[ev]:
                kept = [h for h in g.get("hooks", [])
                        if (ev, h.get("command", "")) not in prev]
                for h in g.get("hooks", []):
                    if (ev, h.get("command", "")) in prev and h not in kept:
                        tr["removed"].append(f"{target}: {ev} {h.get('command','')[:60]}")
                if kept:
                    g["hooks"] = kept
                    groups.append(g)
            if groups:
                hooks_dict[ev] = groups
            else:
                del hooks_dict[ev]

        # 2) manifest を展開して追加(同一エントリが既にあれば台帳への取り込みのみ)
        new_state = []
        for hook in manifest.get("hooks", []):
            if target not in hook.get("targets", []):
                continue
            try:
                ev, entry, _ = render(hook, target, write_wrapper=not dry_run)
            except UnsupportedHook as e:
                tr["skipped"].append(f"{target}: {hook.get('name','?')} — {e}")
                continue
            cmd = entry["hooks"][0]["command"]
            exists = any(c == cmd and e == ev
                         for e, c in _iter_commands(hooks_dict))
            if exists:
                tr["adopted"].append(f"{target}: {hook.get('name','?')}")
            else:
                hooks_dict.setdefault(ev, []).append(entry)
                tr["added"].append(f"{target}: {hook.get('name','?')} → {ev}")
            new_state.append([ev, cmd])

        after = json.dumps(data, ensure_ascii=False, sort_keys=True)
        report["adopted"] += tr["adopted"]
        report["skipped"] += tr["skipped"]
        if after != before:
            # 変更が無いターゲットの added/removed は「消して同じものを戻した」だけなので報告しない
            report["added"] += tr["added"]
            report["removed"] += tr["removed"]
            report["changed"].append(str(path))
            report.setdefault("changed_targets", []).append(target)
            if not dry_run:
                if path.is_file():
                    report["backups"].append(_backup(path))
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
        state[target] = new_state

    if not dry_run:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if "codex" in report.get("changed_targets", []):
        report["notice"] = "Codex 側を変更しました。次回 Codex 起動時に /hooks で信頼してください。"
    return report


def main():
    quiet = "--quiet" in sys.argv
    dry = "--dry-run" in sys.argv
    if not MANIFEST_PATH.is_file():
        if not quiet:
            print(f"manifest が無いためスキップ: {MANIFEST_PATH}")
        return 0
    report = apply_manifest(dry_run=dry)
    if not quiet:
        label = "(dry-run)" if dry else ""
        print(f"hooks_apply{label}: 変更 {len(report['changed'])} ファイル")
        for k in ("added", "adopted", "removed", "skipped"):
            for line in report[k]:
                print(f"  {k}: {line}")
        for b in report["backups"]:
            print(f"  backup: {b}")
        if report.get("notice"):
            print(f"  ⚠ {report['notice']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
