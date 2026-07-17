"""ingest API — 分散Claude Code環境 記憶統合システム (設計書§4)

受信 → マスク(§8.1) → raw_payloads保存 → パース → turns/auto_memory_snapshots へ冪等INSERT。
「生保存 → パース」の二段構成。パース失敗しても raw_payloads には残る。
"""
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from exclude import is_excluded, load_entries, normalize_project_key
from parsers import parse_codex_rollout, parse_transcript

# ---------------------------------------------------------------- 設定

def _read_secret(env_name: str) -> str:
    path = os.environ[env_name]
    return Path(path).read_text().strip()


# 非rootの保証: compose の user: ${INGEST_UID} に 0 を渡されても起動させない
if hasattr(os, "getuid") and os.getuid() == 0:
    raise SystemExit("ingest をrootで実行しない: nas/.env の INGEST_UID に非rootのuidを設定する")

DB_PASSWORD = _read_secret("DB_PASSWORD_FILE")
API_TOKEN = _read_secret("API_TOKEN_FILE")

CONNINFO = (
    f"host={os.environ.get('DB_HOST', 'db')} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ.get('DB_NAME', 'claude_memory')} "
    f"user={os.environ.get('DB_USER', 'claude')} "
    f"password={DB_PASSWORD}"
)

pool = ConnectionPool(CONNINFO, min_size=1, max_size=4, open=False)

app = FastAPI(title="claude-memory ingest")


@app.on_event("startup")
def _startup() -> None:
    pool.open()


@app.on_event("shutdown")
def _shutdown() -> None:
    pool.close()


# ---------------------------------------------------------------- 認証

def require_token(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    # compare_digest: 比較時間からトークンを推測されないように(タイミング攻撃対策)
    if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:].strip(), API_TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


# ---------------------------------------------------------------- マスク (§8.1)

_PATTERNS: list[tuple[str, re.Pattern]] = [
    (p["name"], re.compile(p["pattern"]))
    for p in json.loads(Path(__file__).with_name("redact_patterns.json").read_text())
]


def mask_text(text: str) -> str:
    for name, rx in _PATTERNS:
        def _repl(m: re.Match, _name: str = name) -> str:
            # キャプチャグループがあるパターンは「プレフィックスを残して値だけ潰す」
            prefix = "".join(g for g in m.groups() if g) if m.groups() else ""
            return f"{prefix}[REDACTED:{_name}]"

        text = rx.sub(_repl, text)
    return text


_SENSITIVE_KEYS = {"password", "passwd", "pwd"}


def mask_value(value: Any) -> Any:
    """JSON構造を再帰的に辿り、全文字列にマスクを適用する。

    正規表現(値の形式ベース)に加え、JSONキーが認証情報を示す場合は値を形式に関係なく潰す。
    """
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, list):
        return [mask_value(v) for v in value]
    if isinstance(value, dict):
        return {
            k: "[REDACTED:password-key]"
            if isinstance(v, str) and k.lower() in _SENSITIVE_KEYS
            else mask_value(v)
            for k, v in value.items()
        }
    return value


# ---------------------------------------------------------------- 収集除外 (§8.3 第二防衛線)

_EXCLUDE_FILE = os.environ.get("SYNC_EXCLUDE_FILE")
_exclude_cache: tuple[float, list] = (0.0, [])


def exclude_entries() -> list:
    """sync-exclude.txt のエントリ(mtimeが変わったら再読込)。未設定なら除外なし。"""
    global _exclude_cache
    if not _EXCLUDE_FILE:
        return []
    try:
        mtime = os.stat(_EXCLUDE_FILE).st_mtime
    except OSError:
        return []  # 配布リポジトリ未マウント等: 第一防衛線(端末側)に任せる
    if mtime != _exclude_cache[0]:
        _exclude_cache = (mtime, load_entries(_EXCLUDE_FILE))
    return _exclude_cache[1]


# ---------------------------------------------------------------- エンドポイント

@app.get("/health")
def health() -> dict:
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/ingest", dependencies=[Depends(require_token)])
async def ingest(request: Request) -> dict:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    kind = payload.get("kind", "transcript")
    if kind not in ("transcript", "auto_memory"):
        raise HTTPException(status_code=400, detail=f"unknown kind: {kind}")
    agent = payload.get("agent", "claude-code")
    if agent not in ("claude-code", "codex"):
        raise HTTPException(status_code=400, detail=f"unknown agent: {agent}")

    # 収集除外(§8.3 第二防衛線)。200で応えて何も保存しない:
    # エラーにすると古い除外リストの端末のスプールが詰まり、後続の送信まで止まるため
    entries = exclude_entries()
    project_dir = payload.get("project_dir") or ""
    # 旧仕様のauto_memoryはproject_dirがmunged名(絶対パスでない)。その場合のみ
    # mungedフォールバック判定を使う(新仕様は実cwdが入り、通常判定が効く)
    munged = project_dir if kind == "auto_memory" and not project_dir.startswith("/") else None
    if entries and is_excluded(
        entries,
        project_key=normalize_project_key(payload.get("git_remote_url"), project_dir),
        project_dir=project_dir if project_dir.startswith("/") else None,
        munged_dir=munged,
    ):
        return {"excluded": True}

    # 1. マスクを適用してから生保存(生JSONにも秘密は残さない — §8.1)
    payload = mask_value(payload)
    device = payload.get("device", "unknown")
    event_id = payload.get("event_id")
    event_id = str(event_id)[:100] if event_id else None

    with pool.connection() as conn:
        # 端末生成のevent_idで再送(at-least-once)を重複排除。event_id無しの旧形式は素通し
        row = conn.execute(
            "INSERT INTO raw_payloads (device, kind, payload, event_id, agent) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (event_id) DO NOTHING RETURNING id",
            (device, kind, Json(payload), event_id, agent),
        ).fetchone()
        if row is None:
            return {"duplicate": True, "event_id": event_id}
        payload_id = row[0]

        # 2. パース → 冪等INSERT。セーブポイントで分離し、
        #    内側のDBエラーが raw_payloads の保存と parse_error 記録を巻き込まないようにする
        try:
            with conn.transaction():
                result = _parse_and_insert(conn, kind, payload, payload_id)
        except Exception as exc:  # パース失敗はrawに記録して200(データは失われていない)
            conn.execute(
                "UPDATE raw_payloads SET parse_error = %s WHERE id = %s",
                (str(exc)[:2000], payload_id),
            )
            result = {"payload_id": payload_id, "parse_error": str(exc)[:200]}

    return result


def _parse_and_insert(conn, kind: str, payload: dict, payload_id: int) -> dict:
    device = payload.get("device", "unknown")
    if kind == "transcript":
        parse = parse_codex_rollout if payload.get("agent") == "codex" else parse_transcript
        rows = parse(payload, payload_id)
        inserted = 0
        for r in rows:
            cur = conn.execute(
                """
                INSERT INTO turns (device, project_key, session_id, message_uuid,
                                   role, content, ts, cwd, git_branch, model, payload_id, agent)
                VALUES (%(device)s, %(project_key)s, %(session_id)s, %(message_uuid)s,
                        %(role)s, %(content)s, %(ts)s, %(cwd)s, %(git_branch)s,
                        %(model)s, %(payload_id)s, %(agent)s)
                ON CONFLICT (session_id, message_uuid) DO NOTHING
                """,
                r,
            )
            inserted += cur.rowcount
        result = {"payload_id": payload_id, "parsed": len(rows), "inserted": inserted}
    else:  # auto_memory
        cur = conn.execute(
            """
            INSERT INTO auto_memory_snapshots (device, project_key, file_path, content, file_mtime)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (device, file_path, file_mtime) DO NOTHING
            """,
            (
                device,
                normalize_project_key(payload.get("git_remote_url"), payload.get("project_dir")),
                payload.get("file_path", "unknown"),
                payload.get("content", ""),
                payload.get("file_mtime"),
            ),
        )
        result = {"payload_id": payload_id, "inserted": cur.rowcount}
    conn.execute("UPDATE raw_payloads SET parsed_at = now() WHERE id = %s", (payload_id,))
    return result
