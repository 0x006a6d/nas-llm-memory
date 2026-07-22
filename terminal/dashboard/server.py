#!/usr/bin/env python3
"""claude-config dashboard — メモリ/コンテキスト設定のローカルビューア+エディタ。

起動:  python3 ~/claude-config/dashboard/server.py   → http://127.0.0.1:8810
依存:  python3 標準ライブラリのみ。NAS への問い合わせは ssh nas 経由(~/.ssh/config の Host nas)。

編集の意味論(設計書§6 の規約に従う):
  - facts への操作が恒久調整。index.md は夜間バッチ(03:30)が current_facts から全再生成する。
  - 追加   = INSERT (replaces=NULL)
  - 修正   = INSERT (replaces=旧id)  … nightly の ORGANIZE と同じ表現
  - 撤去   = UPDATE retired_by=自id … view(retired_by IS NULL)から外れる。nightly に retire 経路が
             無いため、置換先の無い削除はこの自己参照 tombstone で表す。
  - index.md / sync-exclude.txt のファイル編集は保存前に同名 .bak へ退避。
"""
import json
import os
import re
import subprocess
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
DASH_DIR = Path(__file__).resolve().parent
CONFIG_DIR = DASH_DIR.parent
CLAUDE_DIR = HOME / ".claude"
CACHE_DIR = DASH_DIR / ".cache"
NAS_CACHE = CACHE_DIR / "nas.json"
DEMO_DIR = DASH_DIR / "demo"
PORT = 8810
DEMO = False  # --demo: NAS へ接続せず demo/ のダミーデータで動く(公開リポ向け)

SSH_TARGET = "nas"
PSQL = ("cd /volume2/claude-system && "
        "docker compose exec -T db psql -U claude -d claude_memory -t -A -f -")


# ---------------------------------------------------------------- NAS access

def run_sql(sql, timeout=40):
    """ssh nas 経由で SQL を実行し stdout を返す。失敗時は RuntimeError。"""
    if DEMO:
        raise RuntimeError("demo モードでは NAS への操作はできません")
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", SSH_TARGET, PSQL],
        input=sql.encode(), capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-800:])
    return proc.stdout.decode(errors="replace").strip()


def sql_json(inner_select):
    out = run_sql(f"select coalesce(json_agg(t),'[]') from ({inner_select}) t;")
    # psql -t -A は行のみ返す。json_agg の改行を含みうるので全体を join
    return json.loads(out or "[]")


def dollar_quote(text):
    """SQL 文字列リテラルを dollar quoting で安全に構築する。"""
    tag = "dq"
    i = 0
    # 本文末尾が "$tag" で終わる場合も、連結時に閉じ区切りが跨って成立するため避ける
    while f"${tag}$" in text or text.endswith(f"${tag}"):
        i += 1
        tag = f"dq{i}"
    return f"${tag}${text}${tag}$"


# ---------------------------------------------------------------- local state

def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def frontmatter(text):
    """SKILL.md frontmatter から name/description を抜く。"""
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    meta = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
    return meta


def collect_skills():
    skills = []
    seen = set()
    for base, source in [(CLAUDE_DIR / "skills", "user"),
                         (CONFIG_DIR / "skills", "claude-config")]:
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            real = str(f.resolve())
            if real in seen:
                continue
            seen.add(real)
            text = read_text(f) or ""
            meta = frontmatter(text)
            skills.append({
                "name": meta.get("name", d.name), "dir": d.name,
                "description": meta.get("description", ""),
                "source": source, "path": str(f), "bytes": len(text.encode()),
            })
    for f in sorted(CLAUDE_DIR.glob("plugins/cache/*/*/*/skills/*/SKILL.md")):
        real = str(f.resolve())
        if real in seen:
            continue
        seen.add(real)
        text = read_text(f) or ""
        meta = frontmatter(text)
        rel = f.relative_to(CLAUDE_DIR / "plugins" / "cache")
        plugin = f"{rel.parts[1]}@{rel.parts[0]}"
        skills.append({
            "name": meta.get("name", f.parent.name), "dir": f.parent.name,
            "description": meta.get("description", ""),
            "source": f"plugin:{plugin}", "path": str(f),
            "bytes": len(text.encode()),
        })
    return skills


def collect_hooks():
    """settings.json とプラグインの hooks を出所付きでフラット化する。"""
    entries = []

    def flatten(hooks_dict, source, plugin_root=None):
        for event, groups in (hooks_dict or {}).items():
            for g in groups:
                for h in g.get("hooks", []):
                    cmd = h.get("command", "")
                    injected = None
                    m = re.search(r'additionalContext\\?":\\?"(.+?)\\?"\}\}', cmd)
                    if m:
                        injected = m.group(1).replace('\\"', '"')
                    entries.append({
                        "event": event, "source": source,
                        "matcher": g.get("matcher"), "condition": h.get("if"),
                        "command": cmd, "timeout": h.get("timeout"),
                        "injected": injected, "plugin_root": plugin_root,
                    })

    settings = {}
    raw = read_text(CLAUDE_DIR / "settings.json")
    if raw:
        try:
            settings = json.loads(raw)
        except ValueError:
            pass
    flatten(settings.get("hooks"), "settings.json")

    for f in sorted(CLAUDE_DIR.glob("plugins/cache/*/*/*/hooks/hooks.json")):
        try:
            data = json.loads(read_text(f) or "{}")
        except ValueError:
            continue
        rel = f.relative_to(CLAUDE_DIR / "plugins" / "cache")
        flatten(data.get("hooks"), f"plugin:{rel.parts[1]}@{rel.parts[0]}",
                plugin_root=str(f.parent.parent))

    # 同一イベント×同一コマンドの重複登録を検出(spool_write 二重登録の類)
    counts = {}
    for e in entries:
        key = (e["event"], re.sub(r'\s+|"', "", e["command"]))
        counts[key] = counts.get(key, 0) + 1
        e["duplicate"] = counts[key] > 1
    return entries, settings


# ------------------------------------------------- hooks (Claude ⇄ Codex / manifest)

# 正本 hooks_apply.py(../hooks/)をモジュールとして読み込む。
# manifest(hooks-manifest.json)の読み書き・実設定への展開はすべてそちらに委譲する。
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "hooks_apply", CONFIG_DIR / "hooks" / "hooks_apply.py")
hooks_apply = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(hooks_apply)

CODEX_HOOKS = HOME / ".codex" / "hooks.json"


def collect_codex_hooks():
    """~/.codex/hooks.json を collect_hooks と同じ形にフラット化する。"""
    entries = []
    try:
        data = json.loads(read_text(CODEX_HOOKS) or "{}")
    except ValueError:
        return entries
    for event, groups in (data.get("hooks") or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                entries.append({
                    "event": event, "source": "codex:hooks.json",
                    "matcher": g.get("matcher"), "condition": h.get("if"),
                    "command": h.get("command", ""), "timeout": h.get("timeout"),
                    "injected": None, "duplicate": False,
                })
    return entries


def memory_indexes():
    files = []
    base = CONFIG_DIR / "memory"
    if not base.is_dir():
        return files
    for d in sorted(base.iterdir()):
        f = d / "index.md"
        if not f.is_file():
            continue
        text = read_text(f) or ""
        files.append({
            "key": d.name, "path": str(f), "bytes": len(text.encode()),
            "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M"),
            "auto_generated": "夜間バッチ生成" in text.splitlines()[1] if len(text.splitlines()) > 1 else False,
            "content": text,
        })
    return files


def git_info():
    def g(*args):
        p = subprocess.run(["git", "-C", str(CONFIG_DIR), *args],
                           capture_output=True, text=True, timeout=10)
        return p.stdout.strip()
    return {"status": g("status", "--short"),
            "last": g("log", "-1", "--format=%h %ad %s", "--date=format:%m-%d %H:%M")}


def state():
    hooks, settings = collect_hooks()
    sync_path = CONFIG_DIR / "sync-exclude.txt"
    sync = read_text(sync_path) or ""
    claude_md = read_text(CLAUDE_DIR / "CLAUDE.md") or ""
    return {
        "claude_md": {"path": str(CLAUDE_DIR / "CLAUDE.md"), "content": claude_md,
                      "bytes": len(claude_md.encode())},
        "memory_indexes": memory_indexes(),
        "sync_exclude": {"path": str(sync_path), "content": sync,
                         "bytes": len(sync.encode())},
        "hooks": hooks,
        "codex_hooks": collect_codex_hooks(),
        "manifest": hooks_apply.manifest_status(),
        "settings": {k: settings.get(k) for k in
                     ("model", "autoMemoryEnabled", "enabledPlugins", "statusLine")},
        "skills": collect_skills(),
        "crontab": read_text(CONFIG_DIR / "batch" / "crontab.txt") or "",
        "hook_scripts": sorted(p.name for p in (CONFIG_DIR / "hooks").glob("*.py")),
        "git": git_info(),
        "vibe_island_present": (HOME / ".vibe-island/bin/vibe-island-bridge").exists(),
        "generated_at": datetime.now().strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------- NAS queries

def nas_snapshot():
    if DEMO:
        data = json.loads((DEMO_DIR / "nas.json").read_text(encoding="utf-8"))
        data["fetched_at"] = datetime.now().strftime("%m-%d %H:%M:%S") + " (demo)"
        return data
    data = {
        "turns_by_project": sql_json(
            "select project_key, count(*) n, max(ts) last_ts from turns "
            "group by project_key order by n desc"),
        "facts_by_project": sql_json(
            "select project_key, count(*) n from current_facts "
            "group by project_key order by n desc"),
        "batch_runs": sql_json(
            "select id, started_at, finished_at, status, turns_processed, "
            "index_lines, notes from batch_runs order by id desc limit 10"),
        "auto_memory": sql_json(
            "select id, device, project_key, file_path, file_mtime, "
            "length(content) bytes from auto_memory_snapshots "
            "order by file_mtime desc"),
        # 表示用: 各project_keyの主要端末(ホームディレクトリ系キーの端末名注釈に使う)
        "devices_by_project": sql_json(
            "select distinct on (project_key) project_key, device, count(*) n "
            "from turns group by project_key, device "
            "order by project_key, n desc"),
        "fetched_at": datetime.now().strftime("%m-%d %H:%M:%S"),
    }
    CACHE_DIR.mkdir(exist_ok=True)
    # 並行リクエストが書きかけJSONを読まないよう、tmpに書いて原子的に置換
    tmp = NAS_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, NAS_CACHE)
    return data


def get_facts(project):
    if DEMO:
        rows = json.loads((DEMO_DIR / "facts.json").read_text(encoding="utf-8"))
        return [r for r in rows if r["project_key"] == project]
    return sql_json(
        "select id, content, status, confidence, created_at, created_by, replaces "
        f"from current_facts where project_key = {dollar_quote(project)} "
        "order by id desc")


def search_turns(q, project, limit=50):
    if DEMO:
        return []
    cond = f"content &@~ {dollar_quote(q)}"
    if project:
        cond += f" and project_key = {dollar_quote(project)}"
    return sql_json(
        "select id, device, agent, project_key, role, ts, "
        "left(content, 600) snippet from turns "
        f"where {cond} order by ts desc limit {int(limit)}")


def fact_op(op, project, content, fact_id):
    today = datetime.now().strftime("%Y%m%d")
    by = dollar_quote(f"dashboard-{today}")
    if op == "add":
        sql = ("insert into facts (project_key, content, status, provenance, "
               "confidence, created_by) values "
               f"({dollar_quote(project)}, {dollar_quote(content)}, 'verified', "
               f"'{{}}', 1.0, {by}) returning id;")
    elif op == "replace":
        sql = ("insert into facts (project_key, content, status, provenance, "
               "confidence, replaces, created_by) "
               "select project_key, "
               f"{dollar_quote(content)}, 'verified', provenance, 1.0, id, {by} "
               f"from facts where id = {int(fact_id)} returning id;")
    elif op == "retire":
        sql = (f"update facts set retired_by = id where id = {int(fact_id)} "
               "and retired_by is null returning id;")
    else:
        raise ValueError(f"unknown op: {op}")
    return run_sql(sql)


# ---------------------------------------------------------------- save files

def resolve_save_target(target):
    """編集を許可するファイルのホワイトリスト。"""
    if target == "sync_exclude":
        return CONFIG_DIR / "sync-exclude.txt"
    m = re.match(r"^index:([A-Za-z0-9._-]+)$", target or "")
    if m and m.group(1) not in (".", ".."):
        p = CONFIG_DIR / "memory" / m.group(1) / "index.md"
        if p.is_file():
            return p
    return None


def save_file(target, content):
    path = resolve_save_target(target)
    if path is None:
        raise ValueError(f"編集対象外: {target}")
    bak = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "bytes": len(content.encode()), "backup": str(bak)}


# ---------------------------------------------------------------- http server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                self.send_file(DASH_DIR / "static" / "index.html",
                               "text/html; charset=utf-8")
            elif url.path.startswith("/static/"):
                name = os.path.basename(url.path)
                ctype = {"css": "text/css", "js": "text/javascript"}.get(
                    name.rsplit(".", 1)[-1], "text/plain")
                self.send_file(DASH_DIR / "static" / name, f"{ctype}; charset=utf-8")
            elif url.path == "/api/state":
                self.send_json(state())
            elif url.path == "/api/nas":
                if DEMO or q.get("refresh", ["0"])[0] == "1" or not NAS_CACHE.is_file():
                    self.send_json(nas_snapshot())
                else:
                    self.send_json(json.loads(NAS_CACHE.read_text()))
            elif url.path == "/api/facts":
                self.send_json(get_facts(q["project"][0]))
            elif url.path == "/api/auto_memory":
                rows = sql_json("select content from auto_memory_snapshots "
                                f"where id = {int(q['id'][0])}")
                self.send_json({"content": rows[0]["content"] if rows else ""})
            elif url.path == "/api/turns":
                self.send_json(search_turns(q["q"][0],
                                            q.get("project", [None])[0]))
            else:
                self.send_error(404)
        except Exception as e:  # noqa: BLE001 — API 応答としてエラーを返す
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        # CSRF対策: ブラウザ発のクロスオリジンPOST(Originが付く)は拒否する。
        # Origin無し(curl等のローカルツール)は従来どおり許可。
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}",
                                     f"http://localhost:{PORT}"):
            self.send_json({"error": "forbidden origin"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/save":
                self.send_json(save_file(body.get("target"), body.get("content", "")))
            elif self.path == "/api/manifest":
                op = body.get("op")
                if op == "set_targets":
                    m = hooks_apply.load_manifest()
                    m["hooks"][int(body["index"])]["targets"] = body.get("targets", [])
                    hooks_apply.save_manifest(m)
                    self.send_json({"ok": True})
                elif op == "apply":
                    self.send_json(hooks_apply.apply_manifest())
                else:
                    raise ValueError(f"unknown op: {op}")
            elif self.path == "/api/fact":
                out = fact_op(body.get("op"), body.get("project", ""),
                              body.get("content", ""), body.get("id"))
                self.send_json({"result": out})
            else:
                self.send_error(404)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 500)


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        DEMO = True
    if "--port" in sys.argv:
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    mode = " [demo]" if DEMO else ""
    print(f"claude-config dashboard{mode}: http://127.0.0.1:{PORT}")
    server.serve_forever()
