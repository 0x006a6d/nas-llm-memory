"""~/.codex/AGENTS.md への general index 配布(Codex追補設計書§3.1)

Codexには@import構文が無いため、マーカーで区切った管理セクションを直接書き換える。
管理セクション外の手書き本文には触れない。

プロジェクト単位の注入(追補§3.2)は未実装: codex 0.144.1 は
<project>/.codex/AGENTS.md を読まず(実機検証済み)、代替の
<project>/AGENTS.md 本体への展開はcommit対象ファイルに記憶が漏れるため採用しない。
"""
import os
from pathlib import Path

BEGIN = "<!-- nas-memory:begin (auto-generated, do not edit) -->"
END = "<!-- nas-memory:end -->"


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
