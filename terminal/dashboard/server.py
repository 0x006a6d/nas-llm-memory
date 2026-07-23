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
import socket
import subprocess
import tempfile
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


class ConflictError(RuntimeError):
    """楽観ロック衝突。HTTP 409 で返す。"""


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


def load_json(path, default):
    try:
        v = json.loads(read_text(path) or "")
        return v if isinstance(v, type(default)) else default
    except ValueError:
        return default


def claude_projects():
    """~/.claude.json の projects キー = この端末で開いた全プロジェクト。"""
    data = load_json(HOME / ".claude.json", {})
    return [p for p in sorted(data.get("projects") or {})
            if Path(p).expanduser().is_dir()]


def plugin_roots():
    """インストール済みプラグインの実体ディレクトリ一覧。

    installed_plugins.json(installPath)を正とし、enabled は
    settings.json の enabledPlugins("<plugin>@<marketplace>" キー)から引く。
    """
    enabled_map = load_json(CLAUDE_DIR / "settings.json", {}).get("enabledPlugins") or {}
    roots = []
    data = load_json(CLAUDE_DIR / "plugins" / "installed_plugins.json", {})
    for key, installs in (data.get("plugins") or {}).items():
        plugin, _, market = key.partition("@")
        for ins in installs if isinstance(installs, list) else []:
            if not isinstance(ins, dict):
                continue
            install_path = ins.get("installPath")
            # 空文字は Path("") == "." (cwd) に解決されて誤検出するため除外
            if not isinstance(install_path, str) or not install_path:
                continue
            root = Path(install_path)
            if root.is_dir():
                roots.append({"plugin": plugin, "marketplace": market,
                              "root": root, "enabled": bool(enabled_map.get(key))})
    if not roots:  # フォールバック: cache/<marketplace>/<plugin>/<version> を直接走査
        for d in sorted(CLAUDE_DIR.glob("plugins/cache/*/*/*")):
            if d.is_dir():
                roots.append({"plugin": d.parent.name,
                              "marketplace": d.parent.parent.name,
                              "root": d, "enabled": None})
    return roots


def _md_entry(f, fallback_name, source, editable, enabled=None, kind="skill"):
    text = read_text(f) or ""
    meta = frontmatter(text)
    return {"name": meta.get("name", fallback_name), "dir": f.parent.name,
            "description": meta.get("description", ""),
            "source": source, "path": str(f), "bytes": len(text.encode()),
            "kind": kind, "editable": editable, "enabled": enabled}


def collect_skills():
    skills = []
    seen = set()

    def add_dir(base, source):
        if not base.is_dir():
            return
        for d in sorted(base.iterdir()):
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            real = str(f.resolve())
            if real in seen:
                continue
            seen.add(real)
            skills.append(_md_entry(f, d.name, source, editable=True))

    add_dir(CLAUDE_DIR / "skills", "user")
    add_dir(CONFIG_DIR / "skills", "claude-config")
    # プロジェクト側 .claude/skills(/context の Project 欄)。~/ 直下プロジェクトは
    # user(~/.claude/skills)と同一ディレクトリになるが、resolve 済みパスの seen で重複排除される
    for proj in claude_projects():
        add_dir(Path(proj) / ".claude" / "skills", f"project:{proj}")
    for pr in plugin_roots():
        base = pr["root"] / "skills"
        if not base.is_dir():
            continue
        for f in sorted(base.glob("*/SKILL.md")):
            real = str(f.resolve())
            if real in seen:
                continue
            seen.add(real)
            e = _md_entry(f, f.parent.name,
                          f"plugin:{pr['plugin']}@{pr['marketplace']}",
                          editable=False, enabled=pr["enabled"])
            e["name"] = f"{pr['plugin']}:{e['name']}"  # /context の表示規則に合わせる
            skills.append(e)
    return skills


def collect_commands():
    """スラッシュコマンド(commands/*.md)。呼び出し名は /<name>、プラグインは /<plugin>:<name>。"""
    out = []
    seen = set()

    def add_dir(base, source, editable, prefix="", enabled=None):
        if not base.is_dir():
            return
        for f in sorted(base.glob("*.md")):
            real = str(f.resolve())
            if real in seen:
                continue
            seen.add(real)
            e = _md_entry(f, f.stem, source, editable, enabled, kind="command")
            e["name"] = f"/{prefix}{f.stem}"
            out.append(e)

    add_dir(CLAUDE_DIR / "commands", "user", True)
    for proj in claude_projects():
        add_dir(Path(proj) / ".claude" / "commands", f"project:{proj}", True)
    for pr in plugin_roots():
        add_dir(pr["root"] / "commands",
                f"plugin:{pr['plugin']}@{pr['marketplace']}", False,
                prefix=f"{pr['plugin']}:", enabled=pr["enabled"])
    return out


def collect_agents():
    """カスタムエージェント(agents/*.md)。呼び出し名はプラグインのみ <plugin>:<name>。"""
    out = []
    seen = set()

    def add_dir(base, source, editable, prefix="", enabled=None):
        if not base.is_dir():
            return
        for f in sorted(base.glob("*.md")):
            real = str(f.resolve())
            if real in seen:
                continue
            seen.add(real)
            e = _md_entry(f, f.stem, source, editable, enabled, kind="agent")
            e["name"] = f"{prefix}{f.stem}"
            out.append(e)

    add_dir(CLAUDE_DIR / "agents", "user", True)
    for proj in claude_projects():
        add_dir(Path(proj) / ".claude" / "agents", f"project:{proj}", True)
    for pr in plugin_roots():
        add_dir(pr["root"] / "agents",
                f"plugin:{pr['plugin']}@{pr['marketplace']}", False,
                prefix=f"{pr['plugin']}:", enabled=pr["enabled"])
    return out


def builtin_snapshot():
    """Claude Code 本体内蔵のコンテキスト構成要素(バイナリ埋め込みで列挙不可)。

    builtin-context.json は /context 出力からの手動スナップショット。
    current_version と captured_with の不一致で古さを検知する。
    """
    data = load_json(DASH_DIR / "builtin-context.json", {})
    ver = None
    try:
        target = HOME / ".local" / "bin" / "claude"
        if target.is_symlink() or target.exists():
            ver = target.resolve().name
    except OSError:
        pass
    data["current_version"] = ver
    return data


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


def skill_candidates():
    """skills-candidates/(スキル候補の置き場。発動しない提案データ)を読む。"""
    out = []
    base = CONFIG_DIR / "skills-candidates"
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        f = d / "meta.json"
        if not f.is_file():
            continue
        try:
            meta = json.loads(read_text(f) or "{}")
        except ValueError:
            continue
        if not isinstance(meta, dict):
            continue
        evidence = meta.get("evidence", [])
        draft = read_text(d / "SKILL.md") or ""
        out.append({"name": meta.get("name", d.name),
                    "kind": meta.get("kind", "new"),
                    "target_skill": meta.get("target_skill"),
                    "summary": meta.get("summary", ""),
                    "count": meta.get("count", 0),
                    "evidence_n": len(evidence) if isinstance(evidence, list) else 0,
                    "updated": meta.get("updated", ""),
                    "draft": draft})
    return out


def routing_state():
    """routing.json(端末別のindex注入宣言)と自端末のローカルレジストリ。"""
    path = CONFIG_DIR / "routing.json"
    raw = read_text(path) or ""
    parsed, err = {}, None
    if raw.strip():
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                parsed, err = {}, "オブジェクトではありません"
        except ValueError as e:
            err = str(e)
    reg = []
    try:
        reg = json.loads(read_text(HOME / ".claude-spool" / "codex-projects.json") or "[]")
        if not isinstance(reg, list):
            reg = []
    except ValueError:
        pass
    return {"path": str(path), "parsed": parsed, "error": err, "raw": raw,
            "local_device": socket.gethostname(), "local_registry": reg}


def save_routing(routing, expected=None):
    """routing.json を検証・保存し、そのファイルだけを commit & push で配布する。"""
    if not isinstance(routing, dict):
        raise ValueError("routing はオブジェクトである必要があります")
    for dev, entry in routing.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("projects"), list):
            raise ValueError(f"{dev} のエントリが不正です(projects リストが必要)")
        for p in entry["projects"]:
            if not isinstance(p, str) or not p.startswith(("/", "~")):
                raise ValueError(f"{dev}: 絶対パスではありません: {p}")
    def g(*args, check=False):
        p = subprocess.run(["git", "-C", str(CONFIG_DIR), *args],
                           capture_output=True, text=True, timeout=60)
        if check and p.returncode != 0:
            raise RuntimeError(f"git {args[0]}: {p.stderr.strip()[-300:]}")
        return p
    # 書き込みより先にpull(check付き): 別端末が更新したrouting.jsonへの上書き(lost update)を防ぐ
    g("pull", "--ff-only", "-q", check=True)

    path = CONFIG_DIR / "routing.json"
    # 楽観ロック: クライアントが画面を読んだ時点の内容(expected)と pull 後の実体が
    # 違えば、別端末の更新を古い画面で上書きするのを拒否する
    if expected is not None:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != expected:
            raise ConflictError("routing.json が別端末で更新されています。"
                                "ページを再読込してから保存し直してください")
    bak = path.with_suffix(".json.bak")
    if path.exists():
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(routing, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")
    g("add", "routing.json", check=True)
    if g("diff", "--cached", "--quiet", "--", "routing.json").returncode == 0:
        return {"pushed": False, "note": "変更なし"}
    g("commit", "-q", "-m", "routing.json更新(dashboard)", check=True)
    g("push", "-q", check=True)
    return {"pushed": True}


# ---------------------------------------------------------------- 申し送り(messages)

def _spool_config():
    cfg = json.loads(read_text(HOME / ".claude-spool" / "config.json") or "{}")
    url = str(cfg.get("ingest_url") or "").rstrip("/")
    if not url.startswith("https://") or not cfg.get("api_token"):
        raise RuntimeError("~/.claude-spool/config.json が未整備です")
    return cfg, url


def send_message(to_device, to_project, body):
    """ingest APIのPOST /message経由で送信する(マスク・検証はサーバ側)。"""
    if DEMO:
        raise RuntimeError("demo モードでは送信できません")
    cfg, url = _spool_config()
    req = {"from_device": socket.gethostname(),
           "to_device": to_device or None, "to_project": to_project or None,
           "body": str(body or "")}
    # トークンは--config、本文は一時ファイル: どちらもps(argv)に出さない
    conf = (f'header = "Authorization: Bearer {cfg["api_token"]}"\n'
            f'header = "Content-Type: application/json"\n')
    with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
        json.dump(req, f)
        f.flush()
        cmd = ["curl", "-sS", "--fail", "--max-time", "5", "--config", "-",
               "-X", "POST", "-d", "@" + f.name, url + "/message"]
        if cfg.get("tls_cert"):
            cmd += ["--cacert", str(cfg["tls_cert"])]
        r = subprocess.run(cmd, capture_output=True, text=True, input=conf, timeout=10)
    try:
        return {"id": json.loads(r.stdout)["id"]}
    except Exception:
        raise RuntimeError(f"送信失敗: rc={r.returncode} {r.stderr.strip()[-200:]} {r.stdout[:200]}")


def list_messages():
    return sql_json(
        "select id, from_device, to_device, to_project, left(body, 300) body, "
        "created_at, read_at from messages order by id desc limit 30")


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
        "commands": collect_commands(),
        "agents": collect_agents(),
        "builtin": builtin_snapshot(),
        "crontab": read_text(CONFIG_DIR / "batch" / "crontab.txt") or "",
        "hook_scripts": sorted(p.name for p in (CONFIG_DIR / "hooks").glob("*.py")),
        "git": git_info(),
        "routing": routing_state(),
        "skill_candidates": skill_candidates(),
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
        # 配布タブ用: device×projectごとの最頻cwd(ルーティング宣言のパス候補)
        "device_projects": sql_json(
            "select device, project_key, cwd, n from ("
            "select device, project_key, cwd, count(*) n, "
            "row_number() over (partition by device, project_key "
            "order by count(*) desc) rn "
            "from turns where cwd is not null and cwd <> '' "
            "group by device, project_key, cwd) t "
            "where rn = 1 order by device, project_key"),
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
            elif url.path == "/api/messages":
                self.send_json(list_messages())
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
            elif self.path == "/api/routing":
                self.send_json(save_routing(body.get("routing"),
                                            body.get("expected")))
            elif self.path == "/api/message_send":
                self.send_json(send_message(body.get("to_device"),
                                            body.get("to_project"),
                                            body.get("body")))
            else:
                self.send_error(404)
        except ConflictError as e:
            self.send_json({"error": str(e)}, 409)
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
