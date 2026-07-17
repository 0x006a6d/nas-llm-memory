#!/usr/bin/env python3
"""AGENTS.md / AGENTS.override.md への index 配布(Codex追補設計書§3)

Codexには@import構文が無いため、マーカーで区切った管理セクションを直接書き換える。
管理セクション外の手書き本文には触れない。

- グローバル(§3.1): ~/.codex/AGENTS.md の管理セクションに memory/general/index.md を展開
- プロジェクト(§3.2): 登録済みプロジェクトに AGENTS.override.md を生成する。
  内容は「手書きAGENTS.md全文 + 管理セクション(memory/<key>/index.md)」。
  codex 0.144.1 は <project>/.codex/AGENTS.md を読まず、AGENTS.override.md が
  同ディレクトリの AGENTS.md を隠して単独で読まれる(実機検証済み)ため、
  手書き全文の連結が必須。生成物は .git/info/exclude で追跡から外す。
  Claude Code側はプロジェクトCLAUDE.mdの `@AGENTS.override.md` で同じものを読む。

プロジェクトの登録(セットアップ手順):
    python3 agents_sync.py register <project-dir>
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import exclude

BEGIN = "<!-- nas-memory:begin (auto-generated, do not edit) -->"
END = "<!-- nas-memory:end -->"
GEN_NOTE = "<!-- agents_sync生成(AGENTS.md + memory index)。編集はAGENTS.mdへ。このファイルはcommitしない -->"
REGISTRY = Path.home() / ".claude-spool" / "codex-projects.json"
# Codexのproject_doc_max_bytes既定(32KiB)。結合後の合計で打ち切られる(追補§3.3)
OVERRIDE_MAX_BYTES = 32 * 1024


def update_global_agents(config_dir) -> bool:
    """memory/general/index.md を ~/.codex/AGENTS.md の管理セクションへ展開する。

    書き換えたらTrue。Codex未使用端末(~/.codexが無い)では何もしない。
    呼び出しタイミング: senderの各実行時(SessionStart + 定期実行)。
    indexはgit pullで更新されるため、pullが走る端末でのみ追随する。
    """
    codex_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if not codex_dir.is_dir():
        return False
    index = Path(config_dir) / "memory" / "general" / "index.md"
    if not index.exists():
        return False
    section = f"{BEGIN}\n{index.read_text(encoding='utf-8').strip()}\n{END}"

    target = codex_dir / "AGENTS.md"
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    # 最後のBEGINと対応するENDのペアだけを置き換える:
    # 手書き本文に孤立したBEGINが紛れていても、その後方の本文を巻き込まない
    b = current.rfind(BEGIN)
    e = current.find(END, b) if b != -1 else -1
    if b != -1 and e != -1:
        new = current[:b] + section + current[e + len(END):]
    else:
        # 管理セクションが無ければ手書き本文の末尾に追加(本文は保持)
        new = (current.rstrip() + "\n\n" if current.strip() else "") + section + "\n"
    if new == current:
        return False
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.rename(target)
    return True


# ---------------------------------------------------------------- プロジェクト(§3.2)

def _git_remote(cwd):
    try:
        r = subprocess.run(["git", "-C", str(cwd), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
        out = r.stdout.strip()
        return out if r.returncode == 0 and out else None
    except Exception:
        return None


def _project_dir_name(project_key: str) -> str:
    """project_key → memory/配下のディレクトリ名。nightly.pyと同じ規約(変更時は揃えること)"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", project_key).strip("-")
    if safe in ("", ".", ".."):
        return f"unknown-{hashlib.sha1(project_key.encode()).hexdigest()[:8]}"
    if safe == project_key:
        return safe
    return f"{safe}-{hashlib.sha1(project_key.encode()).hexdigest()[:8]}"


def _load_registry(strict: bool = False) -> list:
    """登録済みプロジェクト一覧。ファイルが無ければ空。

    読めない/形式不正の場合、strict=True(register経路)は例外にする:
    黙って空扱いにすると、直後の保存で既存の登録が空で上書きされるため。
    strict=False(同期経路)は警告して空を返す(同期は読み取りのみで安全)。
    """
    if not REGISTRY.exists():
        return []
    try:
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
        if not isinstance(reg, list):
            raise ValueError(f"list以外の形式: {type(reg).__name__}")
        return [p for p in reg if isinstance(p, str)]
    except Exception as e:
        if strict:
            raise RuntimeError(f"レジストリ({REGISTRY})を読めません: {e}") from e
        print(f"agents_sync: WARN レジストリ({REGISTRY})を読めません: {e}", file=sys.stderr)
        return []


def _ensure_excluded(project_dir: Path) -> None:
    """AGENTS.override.md を .git/info/exclude に追記(gitリポでなければ何もしない)"""
    r = subprocess.run(["git", "-C", str(project_dir), "rev-parse", "--git-path", "info/exclude"],
                       capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        return
    p = Path(r.stdout.strip())
    if not p.is_absolute():
        p = project_dir / p
    current = p.read_text(encoding="utf-8") if p.exists() else ""
    if any(ln.strip() in ("/AGENTS.override.md", "AGENTS.override.md")
           for ln in current.splitlines()):
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        # 既存最終行に改行が無い場合に行が融合しないよう補完する
        if current and not current.endswith("\n"):
            f.write("\n")
        f.write("/AGENTS.override.md\n")


def _sync_project(config_dir, project_dir: str) -> bool:
    """1プロジェクトの AGENTS.override.md を再生成する。書き換えたらTrue。

    indexが未生成でも手書きAGENTS.mdがあれば手書きのみで生成する:
    CLAUDE.mdは `@AGENTS.override.md` だけを参照する構成(追補§3.2)のため、
    登録済みプロジェクトでは生成物が常に存在しないと手書き指示ごと消える。
    """
    d = Path(project_dir)
    if not d.is_dir():
        return False
    target = d / "AGENTS.override.md"
    key = exclude.normalize_project_key(_git_remote(d), str(d))
    index = Path(config_dir) / "memory" / _project_dir_name(key) / "index.md"
    hw_path = d / "AGENTS.md"
    handwritten = hw_path.read_text(encoding="utf-8") if hw_path.exists() else ""

    if not handwritten.strip() and not index.exists():
        # 中身になるものが何も無い: 自分の生成物(署名あり)だけ撤去する
        if target.exists():
            cur = target.read_text(encoding="utf-8")
            if cur.startswith(GEN_NOTE) or BEGIN in cur:
                target.unlink()
                return True
        return False

    section = (f"{BEGIN}\n{index.read_text(encoding='utf-8').strip()}\n{END}"
               if index.exists() else "")
    new = GEN_NOTE + "\n\n" \
        + (handwritten.rstrip() + ("\n\n" if section else "\n") if handwritten.strip() else "") \
        + (section + "\n" if section else "")
    # 連結漏れ検出(追補§6): overrideは手書きAGENTS.mdを完全に隠すため、
    # 手書き本文が結果に含まれないなら書き込まない(=手書き指示の消失を防ぐ)
    if handwritten.strip() and handwritten.rstrip() not in new:
        print(f"agents_sync: WARN {d}: 手書きAGENTS.mdの連結に失敗、書き込み中止", file=sys.stderr)
        return False
    if len(new.encode("utf-8")) > OVERRIDE_MAX_BYTES:
        print(f"agents_sync: WARN {d}: AGENTS.override.md が{OVERRIDE_MAX_BYTES}Bを超過"
              f"({len(new.encode('utf-8'))}B)。Codex側で切り詰められる可能性", file=sys.stderr)

    current = target.read_text(encoding="utf-8") if target.exists() else ""
    if new == current:
        return False
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.rename(target)
    _ensure_excluded(d)
    return True


def update_project_agents(config_dir) -> int:
    """登録済み全プロジェクトを同期する。書き換えた件数を返す。

    1プロジェクトの失敗(消えたディレクトリ等)は他へ波及させない。
    """
    changed = 0
    for p in _load_registry():
        try:
            if _sync_project(config_dir, p):
                changed += 1
        except Exception as e:
            print(f"agents_sync: WARN {p}: {e}", file=sys.stderr)
    return changed


def register(config_dir, project_dir: str) -> None:
    d = Path(project_dir).expanduser().resolve()
    if not d.is_dir():
        sys.exit(f"agents_sync: {d} はディレクトリではありません")
    try:
        reg = _load_registry(strict=True)
    except RuntimeError as e:
        sys.exit(f"agents_sync: {e}(修復するまでregisterを中止します)")
    if str(d) not in reg:
        reg.append(str(d))
        REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        tmp = REGISTRY.with_name(REGISTRY.name + ".tmp")
        tmp.write_text(json.dumps(sorted(reg), ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
        tmp.rename(REGISTRY)
        print(f"登録: {d}")
    else:
        print(f"登録済み: {d}")
    _ensure_excluded(d)  # 生成前でも先回りで追跡除外しておく
    changed = _sync_project(config_dir, str(d))
    target = d / "AGENTS.override.md"
    if target.exists():
        state = "生成しました" if changed else "最新です"
        note = "" if BEGIN in target.read_text(encoding="utf-8") else \
            "(memory index未生成: 夜間バッチが生成した後の同期で自動追記)"
        print(f"AGENTS.override.md を{state}{note}")
    else:
        print("手書きAGENTS.mdもmemory indexも無いため未生成(どちらかができた後の同期で自動作成)")
    print("Claude Code側: プロジェクトのCLAUDE.mdに `@AGENTS.override.md` を追記し、"
          "手書き指示はAGENTS.mdへ一本化してください(追補§3.2)")


def _usage():
    sys.exit("usage: agents_sync.py [sync | register <project-dir> | list]")


if __name__ == "__main__":
    config_dir = Path(__file__).resolve().parent.parent
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "sync" and len(sys.argv) <= 2:
        update_global_agents(config_dir)
        n = update_project_agents(config_dir)
        print(f"同期完了(プロジェクト更新: {n}件)")
    elif cmd == "register" and len(sys.argv) == 3:
        register(config_dir, sys.argv[2])
    elif cmd == "list" and len(sys.argv) <= 2:
        for p in _load_registry():
            print(p)
    else:
        _usage()
