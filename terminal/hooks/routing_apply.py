#!/usr/bin/env python3
"""routing.json → プロジェクトindex注入レジストリへの適用(SessionStartで実行)

claude-config直下の routing.json は「どの端末にどのプロジェクトのindexを注入するか」の
中央宣言(dashboardで編集、git配布)。形式:

    { "<device>": { "projects": ["<そのdevice上の絶対パス>", ...] }, ... }

自端末(socket.gethostname())のエントリがあるときだけ、ローカルレジストリ
(~/.claude-spool/codex-projects.json)を宣言の内容に一致させる(冪等)。
エントリが無い端末は何もしない(従来のregister運用のまま=現状維持)。

登録解除されたプロジェクトは、agents_syncが生成したAGENTS.override.mdを撤去する
(署名の無いファイルには触れない)。生成はここでは行わず、直後に動くsenderの
agents_sync同期に任せる。

usage: routing_apply.py <config_dir> [--quiet]
"""
import json
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agents_sync

ROUTING = "routing.json"


def load_routing(config_dir) -> dict:
    path = Path(config_dir) / ROUTING
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("routing.json はオブジェクトではありません")
    return data


def desired_projects(routing: dict, device: str):
    """自端末の宣言リスト。エントリが無ければNone(=現状維持)。"""
    entry = routing.get(device)
    if entry is None:
        return None
    if not isinstance(entry, dict) or not isinstance(entry.get("projects"), list):
        raise ValueError(f"routing.json の {device} エントリが不正です")
    out = []
    for p in entry["projects"]:
        if not isinstance(p, str) or not p.strip():
            # 黙って読み飛ばすとレジストリが縮む: 宣言全体を不正として何もしない
            raise ValueError(f"projects の要素が不正です: {p!r}")
        path = Path(p).expanduser()
        if not path.is_absolute():
            raise ValueError(f"絶対パスではありません: {p}")
        out.append(os.path.normpath(str(path)))
    return sorted(set(out))


def remove_override(project_dir: str, quiet: bool) -> bool:
    """登録解除されたプロジェクトから生成物を撤去する(署名確認付き)。

    署名判定は agents_sync._sync_project の撤去分岐と同一基準に揃える。
    撤去に失敗したらFalse: 呼び出し側はレジストリを更新せず次回に再試行する
    (更新してしまうと古いindexを注入する生成物が残ったまま二度と回収されない)。
    """
    target = Path(project_dir) / "AGENTS.override.md"
    try:
        if not target.exists():
            return True
        cur = target.read_text(encoding="utf-8")
        if cur.startswith(agents_sync.GEN_NOTE) or agents_sync.BEGIN in cur:
            target.unlink()
            if not quiet:
                print(f"routing_apply: 撤去 {target}")
        elif not quiet:
            print(f"routing_apply: WARN {target} は生成物の署名が無いため残置", file=sys.stderr)
        return True
    except OSError as e:
        print(f"routing_apply: WARN {target}: {e}(レジストリ更新を保留、次回再試行)",
              file=sys.stderr)
        return False


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--quiet"]
    quiet = "--quiet" in sys.argv[1:]
    if len(args) != 1:
        sys.exit("usage: routing_apply.py <config_dir> [--quiet]")
    config_dir = args[0]

    try:
        routing = load_routing(config_dir)
    except Exception as e:
        # 壊れた宣言で既存レジストリを消さない: 何もせず警告のみ
        print(f"routing_apply: WARN routing.json を読めません: {e}", file=sys.stderr)
        return
    desired = desired_projects(routing, socket.gethostname())
    if desired is None:
        return

    current = sorted(set(agents_sync._load_registry()))
    if current == desired:
        return

    removed = [remove_override(gone, quiet) for gone in sorted(set(current) - set(desired))]
    if not all(removed):
        return  # 撤去に失敗した項目がある: レジストリを触らず次回のセッションで再試行

    reg = agents_sync.REGISTRY
    reg.parent.mkdir(parents=True, exist_ok=True)
    tmp = reg.with_name(reg.name + ".tmp")
    tmp.write_text(json.dumps(desired, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    tmp.replace(reg)
    if not quiet:
        print(f"routing_apply: レジストリ更新 {len(current)} -> {len(desired)} 件"
              f"(追加{len(set(desired) - set(current))}/削除{len(set(current) - set(desired))})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"routing_apply: WARN {e}", file=sys.stderr)
        sys.exit(0)
