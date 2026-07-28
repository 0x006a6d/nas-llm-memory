#!/usr/bin/env python3
"""claude-config dashboard — メモリ/コンテキスト設定のローカルビューア+エディタ。

起動:  python3 ~/claude-config/dashboard/server.py   → http://127.0.0.1:8810
依存:  python3 標準ライブラリのみ。NAS への問い合わせは ssh nas 経由(~/.ssh/config の Host nas)。

編集の意味論(設計書§6 の規約に従う):
  - facts への操作が恒久調整。index.md は夜間バッチ(03:00)が current_facts から全再生成する。
  - 追加   = INSERT (replaces=NULL)
  - 修正   = INSERT (replaces=旧id)  … nightly の ORGANIZE と同じ表現
  - 撤去   = UPDATE retired_by=自id … view(retired_by IS NULL)から外れる。nightly に retire 経路が
             無いため、置換先の無い削除はこの自己参照 tombstone で表す。
  - index.md / sync-exclude.txt のファイル編集は保存前に同名 .bak へ退避。
"""
import concurrent.futures
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
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


_USAGE_CACHE = {"sig": None, "data": None}


def skill_usage():
    """全エージェントのスキル使用集計。{agent名: {呼び出し名: {count,last}}} を返す。

    エージェントはハードコードで増やさない前提の入れ物。新しいエージェントの
    集計器を足したらここに1行追加するだけで UI 列は動的に増える。
    """
    return {"claude": claude_skill_usage(), "codex": codex_skill_usage()}


def claude_skill_usage():
    """~/.claude/projects/*.jsonl を走査し Skill ツール呼び出しを集計する。

    返り値: {呼び出し名: {"count": int, "last": ISO文字列}}。
    呼び出し名は Skill tool の input.skill(user/claude-config は素の名、
    plugin は "<plugin>:<skill>")。

    限界(UI にも注記):
      - Skill ツール経由の発動のみ。手順を手作業でなぞった場合は残らない。
      - この端末のローカルログのみ。全端末横断は NAS turns 側。
      - projects/ に残っているセッション分だけ(古いログはローテートで消える)。
    """
    if DEMO:
        return {}
    proj_glob = CLAUDE_DIR / "projects"
    # glob と stat の間にローテートで消えたファイルは無視する(1本欠けても全体を止めない)
    files = []
    sig_parts = []
    for p in sorted(proj_glob.glob("*/*.jsonl")):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append(str(p))
        sig_parts.append((str(p), st.st_mtime_ns, st.st_size))
    sig = tuple(sig_parts)
    if _USAGE_CACHE["sig"] == sig and _USAGE_CACHE["data"] is not None:
        return _USAGE_CACHE["data"]
    agg = {}
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    if '"Skill"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    ts = o.get("timestamp")
                    for c in ((o.get("message") or {}).get("content") or []):
                        if not (isinstance(c, dict) and c.get("type") == "tool_use"
                                and c.get("name") == "Skill"):
                            continue
                        sk = (c.get("input") or {}).get("skill")
                        if not sk:
                            continue
                        e = agg.setdefault(sk, {"count": 0, "last": None})
                        e["count"] += 1
                        if ts and (e["last"] is None or ts > e["last"]):
                            e["last"] = ts
        except Exception:  # noqa: BLE001 — 壊れたログ1本で全体を止めない
            continue
    _USAGE_CACHE["sig"] = sig
    _USAGE_CACHE["data"] = agg
    return agg


# Codex の rollout ログ用: ファイルごとの (mtime,size) → 集計結果キャッシュ
_CODEX_USAGE_CACHE = {"files": {}}
_CODEX_SKILL_RE = re.compile(r'/skills/(?:[^/\s"\']+/)*([^/\s"\']+)/SKILL\.md')
_CODEX_CALL_TYPES = ("function_call", "custom_tool_call", "local_shell_call")


def _codex_scan_file(path):
    """rollout ログ1本から {skill名: {count,last}} を抽出する。

    tool call の payload に現れる /skills/<name>/SKILL.md だけを数える
    (session_meta 等の指示文に含まれる SKILL.md 言及は対象外)。
    同一ターン内の再読(分割 cat 等)は1回に数える。
    """
    per = {}
    seen = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "SKILL.md" not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                p = o.get("payload") or {}
                if p.get("type") not in _CODEX_CALL_TYPES:
                    continue
                blob = json.dumps(
                    {k: p.get(k) for k in ("name", "input", "arguments", "command")},
                    ensure_ascii=False)
                ts = o.get("timestamp")
                turn = str(p.get("internal_chat_message_metadata_passthrough") or ts)
                for m in _CODEX_SKILL_RE.finditer(blob):
                    name = m.group(1)
                    if (name, turn) in seen:
                        continue
                    seen.add((name, turn))
                    e = per.setdefault(name, {"count": 0, "last": None})
                    e["count"] += 1
                    if ts and (e["last"] is None or ts > e["last"]):
                        e["last"] = ts
    except Exception:  # noqa: BLE001 — 壊れたログ1本で全体を止めない
        pass
    return per


def codex_skill_usage():
    """~/.codex/sessions/**/rollout-*.jsonl から SKILL.md 読み取り(参照)を集計する。

    Codex には Claude の Skill ツールに相当する構造化呼び出しが無く、skill の
    使用はシェル等で SKILL.md を読む形でログに残る。読んだ=使ったとは限らない
    (検討のみの場合を含む)ため、UI では「参照」として表示し注記する。
    """
    if DEMO:
        return {}
    root = Path.home() / ".codex" / "sessions"
    cache = _CODEX_USAGE_CACHE["files"]
    agg = {}
    seen_paths = set()
    for p in sorted(root.glob("**/rollout-*.jsonl")):
        sp = str(p)
        try:
            st = p.stat()
        except OSError:
            continue
        seen_paths.add(sp)
        key = (st.st_mtime_ns, st.st_size)
        ent = cache.get(sp)
        if ent is None or ent[0] != key:
            ent = (key, _codex_scan_file(p))
            cache[sp] = ent
        for name, e in ent[1].items():
            a = agg.setdefault(name, {"count": 0, "last": None})
            a["count"] += e["count"]
            if e["last"] and (a["last"] is None or e["last"] > a["last"]):
                a["last"] = e["last"]
    for sp in [k for k in cache if k not in seen_paths]:
        del cache[sp]
    return agg


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
    add_dir(Path.home() / ".codex" / "skills", "codex")  # Codex 専用スキルも同列に扱う
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

    # Codex 側プラグイン(~/.codex/plugins/cache)の3層。openai-bundled = CLI 同梱の内蔵、
    # openai-primary-runtime = 実行時に取得・組み立てられるランタイム、
    # openai-curated-remote = リモート配布のプラグイン。
    # 同一プラグインの旧バージョンが cache に残るため、更新時刻が最新のものを採る
    # (バージョン名の辞書順では 0.10.0 < 0.9.0 になり旧版を掴む)
    codex_cache = Path.home() / ".codex" / "plugins" / "cache"
    for tier_dir, src in (("openai-bundled", "codex-builtin"),
                          ("openai-primary-runtime", "codex-runtime"),
                          ("openai-curated-remote", "codex-remote")):
        picked = {}
        for f in sorted(codex_cache.glob(f"{tier_dir}/*/*/skills/*/SKILL.md")):
            key = (f.parents[3].name, f.parent.name)  # (plugin, skill名)
            cur = picked.get(key)
            if cur is None or f.stat().st_mtime >= cur.stat().st_mtime:
                picked[key] = f
        for (plugin, _name), f in sorted(picked.items()):
            real = str(f.resolve())
            if real in seen:
                continue
            seen.add(real)
            e = _md_entry(f, f.parent.name, src, editable=False)
            e["name"] = f"{plugin}:{e['name']}"
            skills.append(e)

    usage = skill_usage()  # {agent: {呼び出し名: {count,last}}}
    # 同名/同dirのスキルが複数ソースにあると1つの使用実績を全部へ誤帰属する。
    # キーを主張するレコードが一意のときだけ帰属し、曖昧なら未計上(0)のまま残す
    claims = {}
    for s in skills:
        for k in {s["name"], s["dir"]}:
            claims.setdefault(k, []).append(s)
    for s in skills:
        s["usage"] = {}
        for agent, table in usage.items():
            u = {}
            for k in (s["name"], s["dir"]):
                if k in table and len(claims[k]) == 1:
                    u = table[k]
                    break
            s["usage"][agent] = {"count": u.get("count", 0),
                                 "last": (u.get("last") or "")[:10]}
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


def spool_state():
    """~/.claude-spool の実接続設定と送信キューの状態(収受簿タブ表示用)。

    api_token は生値を返さない。端末間の設定照合ができるよう sha256 指紋の先頭だけ返す
    (TLS 証明書も同様。全端末で同じ指紋になっていれば同じ設定を向いている)。
    """
    spool = HOME / ".claude-spool"
    cfg_path = spool / "config.json"
    if DEMO:
        return {"config_path": "~/.claude-spool/config.json", "present": False}
    out = {"config_path": str(cfg_path), "present": cfg_path.is_file()}
    if not out["present"]:
        return out
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"config.json を読めません: {exc}"
        return out
    token = str(cfg.get("api_token") or "")
    cert_str = str(cfg.get("tls_cert") or "")
    cert = Path(cert_str) if cert_str else None

    # 表示するURLはscheme://host[:port]/pathに再構成する(userinfo・query・fragmentは
    # 表示に不要なので、仮に書かれていても落とす)
    u = urllib.parse.urlsplit(str(cfg.get("ingest_url") or ""))
    host = u.hostname or ""
    if u.port:
        host += f":{u.port}"
    ingest_url = f"{u.scheme}://{host}{u.path}" if u.scheme and host else ""

    def _count(d):
        # senderが毎時走ってpending→sentへ動かすため、列挙中にエントリが消えうる
        try:
            return sum(1 for _ in d.iterdir()) if d.is_dir() else None
        except OSError:
            return None

    def _mtime_of(p):
        try:
            return datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
        except OSError:
            return None

    def _newest_mtime(d):
        newest = None
        try:
            for p in d.glob("*"):
                try:
                    t = p.stat().st_mtime
                except OSError:
                    continue
                newest = t if newest is None else max(newest, t)
        except OSError:
            return None
        return datetime.fromtimestamp(newest).strftime("%m-%d %H:%M") if newest else None

    cert_fp = None
    if cert is not None and cert.is_file():
        try:
            cert_fp = hashlib.sha256(cert.read_bytes()).hexdigest()[:12]
        except OSError:
            pass
    out.update({
        "ingest_url": ingest_url,
        "token_set": bool(token),
        # 照合用の指紋。トークンはsetup生成の高エントロピー値で、先頭48bitから生値は
        # 復元できない(127.0.0.1限定ページ)。生値そのものは絶対に返さない
        "token_fp": hashlib.sha256(token.encode()).hexdigest()[:12] if token else None,
        "tls_cert": cert_str or None,
        "tls_cert_present": bool(cert is not None and cert.is_file()),
        "tls_cert_fp": cert_fp,
        "pending": _count(spool / "pending"),
        "sent": _count(spool / "sent"),
        "last_sent_at": _newest_mtime(spool / "sent"),
        "last_memory_scan_at": _mtime_of(spool / "last_memory_scan"),
    })
    return out


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


def codex_agents_state():
    """Codex への index 配布物の生成状態(例規タブ表示用)。

    agents_sync.py が sender 実行時に生成する ~/.codex/AGENTS.md の管理セクションと、
    登録プロジェクト(~/.claude-spool/codex-projects.json)の AGENTS.override.md を確認する。
    """
    marker = "<!-- nas-memory:begin"

    def stat(p):
        if not p.is_file():
            return None
        text = read_text(p) or ""
        return {"path": str(p), "bytes": len(text.encode()),
                "managed": marker in text,
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")}

    reg = load_json(HOME / ".claude-spool" / "codex-projects.json", [])
    if not isinstance(reg, list):  # 手編集等でオブジェクト化していた場合に壊れないよう
        reg = []
    return {
        "global": stat(HOME / ".codex" / "AGENTS.md"),
        "registry_path": str(HOME / ".claude-spool" / "codex-projects.json"),
        "projects": [{"dir": p,
                      "agents": stat(Path(p) / "AGENTS.md"),
                      "override": stat(Path(p) / "AGENTS.override.md")}
                     for p in reg if isinstance(p, str)],
    }


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
        "spool": spool_state(),
        "hook_scripts": sorted(p.name for p in (CONFIG_DIR / "hooks").glob("*.py")),
        "git": git_info(),
        "routing": routing_state(),
        "codex_agents": codex_agents_state(),
        "skill_candidates": skill_candidates(),
        "vibe_island_present": (HOME / ".vibe-island/bin/vibe-island-bridge").exists(),
        "generated_at": datetime.now().strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------- NAS queries

def _nas_batch_config_text():
    """config.json の生バイト(書き戻し時のハッシュ照合に使う)。無し/読めなければ None。

    照合はNAS側のsha256sum(実ファイル)と突き合わせるため、errors="replace"の
    デコードを挟むと非UTF-8バイトでハッシュがずれ、無変更でも衝突扱いになる。
    """
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", SSH_TARGET,
             "cat /volume2/claude-system/batch/config.json"],
            capture_output=True, timeout=15)
        if proc.returncode != 0:
            return None
        return proc.stdout
    except Exception:  # noqa: BLE001
        return None


def nas_batch_config():
    """NAS のバッチ共通設定(/volume2/claude-system/batch/config.json)。無し/読めなければ None。"""
    raw = _nas_batch_config_text()
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


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
        # 60件 ≈ 12日分(1晩 nightly+チェーン3本+α)。週次compactの健全性判定が
        # 窓から落ちない件数にしておく(10件だと2日で失敗が見えなくなる)
        "batch_runs": sql_json(
            "select id, started_at, finished_at, status, turns_processed, "
            "index_lines, notes from batch_runs order by id desc limit 60"),
        "auto_memory": sql_json(
            "select id, device, project_key, file_path, file_mtime, "
            "length(content) bytes from auto_memory_snapshots "
            "order by file_mtime desc"),
        # 表示用: 各project_keyの主要端末(ホームディレクトリ系キーの端末名注釈に使う)
        "devices_by_project": sql_json(
            "select distinct on (project_key) project_key, device, count(*) n "
            "from turns group by project_key, device "
            "order by project_key, n desc"),
        # 配付先タブ用: device×projectごとの最頻cwd(ルーティング宣言のパス候補)
        "device_projects": sql_json(
            "select device, project_key, cwd, n from ("
            "select device, project_key, cwd, count(*) n, "
            "row_number() over (partition by device, project_key "
            "order by count(*) desc) rn "
            "from turns where cwd is not null and cwd <> '' "
            "group by device, project_key, cwd) t "
            "where rn = 1 order by device, project_key"),
        "batch_config": nas_batch_config(),
        "shelf_pending": shelf_pending_count(),
        "shelf_miketsu": shelf_miketsu_count(),
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


def search_turns(q, project, limit=50, flagged_only=False):
    if DEMO:
        return []
    cond = f"content &@~ {dollar_quote(q)}"
    if project:
        cond += f" and project_key = {dollar_quote(project)}"
    if flagged_only:
        cond += " and exists (select 1 from flags f where f.session_id = turns.session_id)"
    return sql_json(
        "select id, device, agent, project_key, session_id, role, ts, "
        "left(content, 600) snippet, "
        "exists (select 1 from flags f where f.session_id = turns.session_id) flagged "
        "from turns "
        f"where {cond} order by ts desc limit {int(limit)}")


def list_flags():
    """フラグ付き会話の一覧。会話の先頭ユーザー発話を見出しとして添える。"""
    if DEMO:
        return []
    return sql_json(
        "select fl.session_id, fl.note, fl.created_by, fl.created_at, "
        "h.device, h.agent, h.project_key, h.ts first_ts, h.head, a.n from flags fl "
        "left join lateral ("
        "  select device, agent, project_key, ts, left(content, 200) head "
        "  from turns where session_id = fl.session_id and role = 'user' "
        "  and content not like '<%' "  # 注入タグ(<recommended_plugins>等)で始まる行は見出しに使わない
        "  order by ts limit 1) h on true "
        "left join lateral ("
        "  select count(*) n from turns where session_id = fl.session_id) a on true "
        "order by fl.created_at desc")


def flag_op(op, session_id, note):
    """重要な会話のフラグ付与/解除。session_id 単位(turns 本体は変更しない)。"""
    if not session_id:
        raise ValueError("session_id がありません")
    today = datetime.now().strftime("%Y%m%d")
    by = dollar_quote(f"dashboard-{today}")
    sid = dollar_quote(session_id)
    if op == "add":
        sql = (f"insert into flags (session_id, note, created_by) values "
               f"({sid}, {dollar_quote(note or '')}, {by}) "
               "on conflict (session_id) do update set note = excluded.note "
               "returning session_id;")
    elif op == "remove":
        sql = f"delete from flags where session_id = {sid} returning session_id;"
    else:
        raise ValueError(f"unknown op: {op}")
    return run_sql(sql)


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


# ---------------------------------------------------------------- 書庫(起案・決裁文書)

def _doc_no_disp(fy, seq):
    """表示用文書番号(記憶第N号(令和X年度)。令和元年度は「元」)。

    表記規則の正は nas/batch/ringi.py の display_doc_no。dashboard は
    claude-config 側へ単体配布されるため import できず、ここにミラーする
    (一致は tests/test_dashboard_ringi.py が ringi.py との照合で検査)。
    UI(app.js)はこの値を表示するだけにし、年度計算を持たない。
    """
    n = int(fy) - 2018
    return f"記憶第{int(seq)}号(令和{'元' if n == 1 else n}年度)"


def _stamp_doc_no(row):
    if row.get("fiscal_year") is not None and row.get("seq") is not None:
        row["doc_no_disp"] = _doc_no_disp(row["fiscal_year"], row["seq"])
    return row


def shelf_list(filt, kind):
    """drafts一覧。filt: pending(後閲待ち)|remanded(差し戻し中)|miketsu(未決)|all。"""
    if DEMO:
        data = load_json(DEMO_DIR / "shelf.json", {})
        rows = data.get("list", [])
        # 判定は下のSQLと同じ条件にする(demoと本番で見え方を変えない)
        if filt == "pending":
            rows = [r for r in rows if r.get("seen_state") == "pending"
                    and r.get("state") in ("executed", "rejected", "approved")]
        elif filt == "remanded":
            rows = [r for r in rows if r.get("seen_state") == "remanded"
                    or r.get("state") == "reexamine"]
        elif filt == "miketsu":
            rows = [r for r in rows if r.get("state") == "pending_decision"]
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        return [_stamp_doc_no(dict(r)) for r in rows]
    cond = "true"
    if filt == "pending":
        # 後閲対象 = 完結して人間がまだ見ていない文書(施行済み・廃案・後閲待ちskill)
        cond = "seen_state = 'pending' and state in ('executed','rejected','approved')"
    elif filt == "remanded":
        cond = "seen_state = 'remanded' or state = 'reexamine'"
    elif filt == "miketsu":
        # 未決 = 決裁が付かず翌晩へ繰り越し中の文書(承認でも廃案でもない)
        cond = "state = 'pending_decision'"
    if kind:
        cond = f"({cond}) and kind = {dollar_quote(kind)}"
    return [_stamp_doc_no(r) for r in sql_json(
        "select id, doc_no, fiscal_year, seq, kind, project_key, title, state, "
        "decision_class, seen_state, created_at, decided_at, executed_at, related_doc "
        f"from drafts where {cond} order by id desc limit 100")]


def shelf_doc(did):
    """文書詳細: drafts行 + 回議録(draft_log) + 登載facts + 関連文書。"""
    did = int(did)
    if DEMO:
        data = load_json(DEMO_DIR / "shelf.json", {})
        doc = (data.get("docs") or {}).get(str(did))
        if not doc:
            raise ValueError(f"demo文書がありません: {did}")
        return _stamp_doc_no(doc)
    rows = sql_json(f"select * from drafts where id = {did}")
    if not rows:
        raise ValueError(f"文書がありません: id={did}")
    doc = _stamp_doc_no(rows[0])
    doc["log"] = sql_json(
        "select id, actor, action, memo, created_at, created_by "
        f"from draft_log where draft_id = {did} order by id")
    doc["facts"] = sql_json(
        "select f.id, f.content, f.status, (f.retired_by is not null) retired, "
        "exists(select 1 from facts g where g.replaces = f.id) superseded "
        f"from draft_facts df join facts f on f.id = df.fact_id "
        f"where df.draft_id = {did} order by f.id")
    rel = doc.get("related_doc")
    doc["related"] = sql_json(
        "select id, doc_no, kind, title, state from drafts "
        f"where related_doc = {did}"
        + (f" or id = {int(rel)}" if rel else "") + " "
        "order by id")
    return doc


def shelf_op(op, draft_id, memo):
    """後閲操作。kouetsu=後閲印 / remand=メモ付き差し戻し / approve_skill=skill施行許可。

    差し戻しの意味論: executed文書→翌晩、決裁者が再審理(reexamine)。
    approved(後閲待ちskill)→廃案。remandはメモ必須(前の担当者への指示)。
    """
    did = int(draft_id)
    if DEMO:
        raise RuntimeError("demo モードでは後閲操作できません")
    today = datetime.now().strftime("%Y%m%d")
    by = dollar_quote(f"dashboard-{today}")
    if op == "kouetsu":
        sql = ("update drafts set seen_state='seen', seen_at=now() "
               f"where id={did} and seen_state='pending' "
               "and state in ('executed','rejected') returning id;")
        action, log_memo = "kouetsu", memo or None
    elif op == "remand":
        if not (memo or "").strip():
            raise ValueError("差し戻しにはメモ(前の担当者への指示)が必要です")
        sql = ("update drafts set seen_state='remanded', seen_at=now(), "
               "state = case when state='executed' then 'reexamine' "
               "when state='approved' then 'rejected' else state end "
               f"where id={did} and seen_state='pending' "
               "and state in ('executed','approved') returning id;")
        action, log_memo = "sashimodoshi", memo
    elif op == "approve_skill":
        # 後閲印=施行許可。翌晩のnightlyがskills/へ移して施行する
        sql = ("update drafts set seen_state='seen', seen_at=now() "
               f"where id={did} and seen_state='pending' and state='approved' "
               "and kind='skill' returning id;")
        action, log_memo = "kouetsu", memo or "後閲印(施行許可)"
    else:
        raise ValueError(f"unknown op: {op}")
    # 状態更新と回議録の記帳を1文(CTE)で行う: 更新だけ通って記帳が落ちると
    # 後閲印や差し戻しの痕跡が残らないため
    if not run_sql(f"with upd as ({sql.rstrip('; ')}) "
                   "insert into draft_log (draft_id, actor, action, memo, created_by) "
                   f"select id, 'human', {dollar_quote(action)}, "
                   f"{dollar_quote(log_memo) if log_memo else 'null'}, {by} from upd "
                   "returning draft_id;"):
        raise ConflictError("文書の状態が変わっています。再読込してください")
    return {"ok": True}


def shelf_pending_count():
    """後閲待ち文書数(文書事務概況タブの注意欄用)。012未適用ならNone。"""
    try:
        out = run_sql("select count(*) from drafts where seen_state='pending' "
                      "and state in ('executed','rejected','approved');")
        return int(out or 0)
    except Exception:  # noqa: BLE001 — drafts未適用環境
        return None


def shelf_miketsu_count():
    """未決(決裁が付かず繰越中)の文書数。012未適用ならNone。"""
    try:
        out = run_sql("select count(*) from drafts where state='pending_decision';")
        return int(out or 0)
    except Exception:  # noqa: BLE001 — drafts未適用環境
        return None


# --------------------------------------------- 専決規程(batch/config.json)の編集

BATCH_CONFIG_REMOTE = "/volume2/claude-system/batch/config.json"
CONFIG_ROLES = ("kian", "shinsa", "kessai", "enrich")
CONFIG_RINGI_TYPES = {
    "enabled": bool, "trial": bool, "max_hosei_rounds": int, "max_kessai_rounds": int,
    "skill_min_count": int, "skill_auto_execute": bool, "index_delete_ratio": (int, float),
    "trial_models": list, "trial_budget_min": int,
}
# 数値設定の許容範囲(規程外の値を書いて翌晩のバッチを壊さない)
CONFIG_RINGI_RANGE = {
    "max_hosei_rounds": (0, 10), "max_kessai_rounds": (0, 10),
    "skill_min_count": (1, 100), "index_delete_ratio": (0, 1),
    "trial_budget_min": (1, 180),
}
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,100}$")


def save_batch_config(body):
    """rolesとringi(専決規程)を検証してNASのconfig.jsonへ書き戻す。

    - 既存configを土台に上書きし、未知キー(将来の設定)は保持する
    - 楽観ロック: bodyのexpected(画面が読んだ時点のconfig)と現物が違えば409
    - 書き込み前に.bakへ退避(index.md編集と同型)。反映は翌晩のバッチから
    """
    if DEMO:
        raise RuntimeError("demo モードでは保存できません")
    current_raw = _nas_batch_config_text()
    try:
        current = json.loads(current_raw) if current_raw is not None else {}
    except ValueError:
        current = {}
    if body.get("expected") is not None and body["expected"] != current:
        raise ConflictError("NASのconfig.jsonが別の場所で更新されています。"
                            "再読込してから保存し直してください")
    new = dict(current)
    if "model" in body:
        m = str(body["model"] or "")
        if m and not MODEL_RE.match(m):
            raise ValueError(f"model名が不正です: {m}")
        # roles と同じく、空文字は「明示的な解除」(キーを残さない)
        if m:
            new["model"] = m
        else:
            new.pop("model", None)
    if "roles" in body:
        roles = body["roles"]
        if not isinstance(roles, dict) or set(roles) - set(CONFIG_ROLES):
            raise ValueError(f"rolesのキーは {'/'.join(CONFIG_ROLES)} のみ")
        for k, v in roles.items():
            if not isinstance(v, str) or (v and not MODEL_RE.match(v)):
                raise ValueError(f"roles.{k} のモデル名が不正です: {v!r}")
        # 送られたキーだけを既存へ重ねる(未送信の役割は現状維持、空文字は明示的な解除)
        merged_roles = dict(current.get("roles") or {})
        for k, v in roles.items():
            if v:
                merged_roles[k] = v
            else:
                merged_roles.pop(k, None)
        new["roles"] = merged_roles
    if "ringi" in body:
        ringi = body["ringi"]
        if not isinstance(ringi, dict) or set(ringi) - set(CONFIG_RINGI_TYPES):
            raise ValueError("ringiに未知のキーがあります")
        merged = dict(current.get("ringi") or {})
        for k, v in ringi.items():
            t = CONFIG_RINGI_TYPES[k]
            if (isinstance(v, bool) and t is not bool) or not isinstance(v, t):
                raise ValueError(f"ringi.{k} の型が不正です: {v!r}")
            if k == "trial_models" and not all(
                    isinstance(x, str) and MODEL_RE.match(x) for x in v):
                raise ValueError("ringi.trial_models のモデル名が不正です")
            if k in CONFIG_RINGI_RANGE:
                lo, hi = CONFIG_RINGI_RANGE[k]
                if not lo <= v <= hi:
                    raise ValueError(f"ringi.{k} は {lo}〜{hi} の範囲で指定してください: {v!r}")
            merged[k] = v
        new["ringi"] = merged
    text = json.dumps(new, ensure_ascii=False, indent=1) + "\n"
    # 楽観ロックの本チェックはNAS側で行う: 読取りと書込みが別sshセッションだと、
    # 並行する2つの保存がともにローカル比較を通過し、後勝ちで片方の変更が無警告に
    # 消える。flock下で現物のsha256を照合してから差し替える(衝突は exit 109)。
    # expectedが無い(初回等)ときは照合を省略する(従来どおり)
    esha = ""
    if body.get("expected") is not None:
        esha = (hashlib.sha256(current_raw).hexdigest()
                if current_raw is not None else "MISSING")
    # 同一ディレクトリの一時ファイル(名前は重ならないようmktemp)へ受け、JSONとして
    # 読めることを確かめてからmvで差し替える。転送が途中で切れても稼働中の
    # config.jsonが半端なJSONにならない(バッチはこれを毎晩読む)。
    # mktempの0600で既存の権限を潰さないよう、既存ファイルから権限を引き継ぐ
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", SSH_TARGET,
         f"set -e; "
         f"exec 9> {BATCH_CONFIG_REMOTE}.lock; flock 9; "
         f"if [ -n '{esha}' ]; then "
         f"  if [ '{esha}' = 'MISSING' ]; then "
         f"    if [ -f {BATCH_CONFIG_REMOTE} ]; then echo 'config conflict' >&2; exit 109; fi; "
         f"  else "
         f"    cur=$(sha256sum {BATCH_CONFIG_REMOTE} 2>/dev/null | cut -d' ' -f1 || true); "
         f"    if [ \"$cur\" != '{esha}' ]; then echo 'config conflict' >&2; exit 109; fi; "
         f"  fi; "
         f"fi; "
         f"cp {BATCH_CONFIG_REMOTE} {BATCH_CONFIG_REMOTE}.bak 2>/dev/null || true; "
         f"t=$(mktemp {BATCH_CONFIG_REMOTE}.XXXXXX); trap 'rm -f \"$t\"' EXIT; "
         'cat > "$t"; python3 -c \'import json,sys; json.load(open(sys.argv[1]))\' "$t"; '
         f"if [ -f {BATCH_CONFIG_REMOTE} ]; then chmod --reference={BATCH_CONFIG_REMOTE} \"$t\"; "
         f"else chmod 644 \"$t\"; fi; "
         f"mv \"$t\" {BATCH_CONFIG_REMOTE}; trap - EXIT"],
        input=text.encode(), capture_output=True, timeout=15)
    if proc.returncode == 109:
        raise ConflictError("NASのconfig.jsonが別の場所で更新されています。"
                            "再読込してから保存し直してください")
    if proc.returncode != 0:
        raise RuntimeError(f"書き込み失敗: {proc.stderr.decode(errors='replace')[-300:]}")
    return {"saved": True, "config": new}


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


# ---------------------------------------------------------------- 使用量(Claude Code telemetry)
# 各端末の Claude Code が OTLP で NAS の otel-collector に送るメトリクスを
# Prometheus(NAS:9090)の HTTP API で集計する。ホストは ingest_url から導出し、
# コードに IP を書かない。

def _nas_host():
    cfg = load_json(HOME / ".claude-spool" / "config.json", {})
    host = urllib.parse.urlsplit(str(cfg.get("ingest_url") or "")).hostname
    if not host:
        raise RuntimeError("~/.claude-spool/config.json の ingest_url から NAS ホストを特定できません")
    return host


def _prom_query(base, expr, at=None, timeout=10):
    params = {"query": expr}
    if at is not None:
        params["time"] = str(at)
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = json.load(r)
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus 応答異常: {str(data)[:300]}")
    return data["data"]["result"]


def usage_snapshot(hours):
    """期間内の Claude Code 使用量。

    メトリクスはセッションごとに新系列になり、短命セッションでは全量が系列の
    初期値に入るため increase() では数え漏れる。カウンタは単調増加なので
    「セッション単位の max_over_time を合算」で期間合計を出す(期間境界を跨ぐ
    セッションは全量が期間側に入る近似)。
    """
    if DEMO:
        return json.loads((DEMO_DIR / "usage.json").read_text(encoding="utf-8"))
    hours = max(1, min(int(hours), 24 * 92))
    base = f"http://{_nas_host()}:9090"
    rng = f"{hours}h"

    def per_session(metric, by):
        return _prom_query(base, f"max by ({by}) (max_over_time({metric}[{rng}]))")

    def val(r):
        return float(r["value"][1])

    def agg(rows, key):
        out = {}
        for r in rows:
            k = r["metric"].get(key) or "(不明)"
            out[k] = out.get(k, 0.0) + val(r)
        return out

    cost_rows = per_session("claude_code_cost_usage_USD_total",
                            "session_id, model, host_name")
    tok_rows = per_session("claude_code_token_usage_tokens_total",
                           "session_id, model, host_name, type")
    act_rows = per_session("claude_code_active_time_seconds_total", "session_id")
    sessions = ({r["metric"].get("session_id") for r in cost_rows} |
                {r["metric"].get("session_id") for r in tok_rows})

    cost_host, cost_model = agg(cost_rows, "host_name"), agg(cost_rows, "model")
    tok_host, tok_model = agg(tok_rows, "host_name"), agg(tok_rows, "model")
    tok_type = agg(tok_rows, "type")

    def merged(cost_by, tok_by, label):
        keys = sorted(set(cost_by) | set(tok_by),
                      key=lambda k: -cost_by.get(k, 0.0))
        return [{label: k, "cost_usd": round(cost_by.get(k, 0.0), 4),
                 "tokens": int(tok_by.get(k, 0.0))} for k in keys]

    # 日別コスト: 各日の終端時刻で max_over_time[その日の経過秒] を評価する。
    # 日付を跨いで生きるセッションは累計値が跨いだ先の日にも入る近似(まれ)。
    daily = []
    if hours >= 48:
        now = time.time()
        midnight = datetime.now().replace(hour=0, minute=0, second=0,
                                          microsecond=0).timestamp()

        def day_cost(day_start, day_end):
            # 当日窓は0時からの経過秒。深夜0時直後は0秒になり、Prometheusが
            # duration 0 をパースエラーにするため1秒へ丸める
            window = max(1, int(day_end - day_start))
            date = datetime.fromtimestamp(day_start).strftime("%m-%d")
            try:
                rows = _prom_query(
                    base,
                    "sum(max by (session_id, model) (max_over_time("
                    f"claude_code_cost_usage_USD_total[{window}s])))",
                    at=day_end, timeout=4)  # 日別は短めの専用timeout(重い日の累積待機を抑える)
            except Exception:  # noqa: BLE001 — 日別は補助情報。1日の失敗でタブ全体を落とさない
                return {"date": date, "cost_usd": None}  # 欠測(UIは「欠測」と表示)
            return {"date": date,
                    "cost_usd": round(val(rows[0]), 4) if rows else 0.0}

        days = [(midnight - i * 86400, min(now, midnight - i * 86400 + 86400))
                for i in range(min(hours // 24, 31) - 1, -1, -1)]
        # 最大31回の逐次クエリでリクエストスレッドを長く埋めないよう並列化
        # (mapは投入順を保つので日付順は崩れない)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            daily = list(pool.map(lambda d: day_cost(*d), days))

    return {
        "hours": hours,
        "fetched_at": datetime.now().strftime("%m-%d %H:%M"),
        "grafana": f"http://{_nas_host()}:3000/d/claude-code",
        "totals": {"cost_usd": round(sum(cost_host.values()), 4),
                   "tokens": int(sum(tok_type.values())),
                   "sessions": len(sessions),
                   "active_seconds": int(sum(val(r) for r in act_rows))},
        "by_host": merged(cost_host, tok_host, "host"),
        "by_model": merged(cost_model, tok_model, "model"),
        "by_type": sorted(({"type": k, "tokens": int(v)} for k, v in tok_type.items()),
                          key=lambda r: -r["tokens"]),
        "daily": daily,
    }


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
        # UI更新のたびに古いJS/CSSが残らないよう常に再検証させる
        self.send_header("Cache-Control", "no-cache")
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
                                            q.get("project", [None])[0],
                                            flagged_only=q.get("flagged", ["0"])[0] == "1"))
            elif url.path == "/api/flags":
                self.send_json(list_flags())
            elif url.path == "/api/messages":
                self.send_json(list_messages())
            elif url.path == "/api/shelf":
                self.send_json(shelf_list(q.get("filter", ["pending"])[0],
                                          q.get("kind", [""])[0]))
            elif url.path == "/api/shelf_doc":
                self.send_json(shelf_doc(q["id"][0]))
            elif url.path == "/api/usage":
                self.send_json(usage_snapshot(q.get("hours", ["168"])[0]))
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
            elif self.path == "/api/flag":
                out = flag_op(body.get("op"), body.get("session_id"),
                              body.get("note", ""))
                self.send_json({"result": out})
            elif self.path == "/api/shelf_op":
                self.send_json(shelf_op(body.get("op"), body.get("id"),
                                        body.get("memo", "")))
            elif self.path == "/api/batch_config":
                self.send_json(save_batch_config(body))
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
