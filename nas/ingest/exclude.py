"""sync-exclude 判定(設計書§8.3 収集除外)

sync-exclude.txt(claude-config リポジトリ直下、全端末へgit pullで配布)の形式:
  - 1行1エントリ。空行と # 始まりのコメント行は無視
  - '/' または '~' で始まる行: パスglob。fnmatch準拠に加え、
    '<base>/**' は base ディレクトリ自身とその配下すべてに一致する
  - それ以外の行: project_key の完全一致(大文字小文字は無視)

同じファイルを terminal/hooks/ と nas/ingest/ の両方に置く(端末=第一防衛線、
ingest=第二防衛線)。変更時は両方を同一内容に保つこと。
"""
import fnmatch
import os
import re
from pathlib import Path


def normalize_project_key(git_remote_url, project_dir):
    """ingest側 app.py と同じ正規化規約(設計原則5)。変更時は揃えること。"""
    if git_remote_url:
        key = git_remote_url.strip()
        key = re.sub(r"^[a-z+]+://", "", key)   # scheme除去
        key = re.sub(r"^[^@/]+@", "", key)      # user@除去
        key = key.replace(":", "/")             # scp形式 host:path → host/path
        key = re.sub(r"\.git$", "", key)
        key = re.sub(r"/+", "/", key).strip("/")
        return key.lower()
    if project_dir:
        return Path(project_dir).name or "unknown"
    return "unknown"


def load_entries(path):
    """sync-exclude.txt を読む。無い/読めない場合は除外なし(空リスト)。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return []
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _munge(path):
    """Claude Codeの ~/.claude/projects/ ディレクトリ名規約(記号→'-')。"""
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


def _path_matches(path, pattern):
    pattern = os.path.expanduser(pattern)
    path = path.rstrip("/")
    if pattern.endswith("/**"):
        base = pattern[:-3].rstrip("/")
        return path == base or path.startswith(base + "/")
    return fnmatch.fnmatch(path, pattern)


def is_excluded(entries, project_key=None, project_dir=None, munged_dir=None):
    """除外対象なら True。

    - project_key: 正規化済みキー(完全一致エントリと比較)
    - project_dir: cwd等の実パス(パスglobエントリと比較)
    - munged_dir: auto memory のように実パスが失われ munged 名しか無い場合に渡す。
      パスglobは '<base>/**' 形式に限り munged 名へ変換してプレフィックス比較する
    """
    for e in entries:
        if e.startswith(("/", "~")):
            if project_dir and _path_matches(project_dir, e):
                return True
            if munged_dir and e.endswith("/**"):
                base = _munge(os.path.expanduser(e)[:-3].rstrip("/"))
                if munged_dir == base or munged_dir.startswith(base + "-"):
                    return True
        else:
            if project_key and project_key.lower() == e.lower():
                return True
            if munged_dir and munged_dir == e:
                return True
    return False
