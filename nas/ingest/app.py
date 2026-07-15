"""ingest API — 分散Claude Code環境 記憶統合システム (設計書§4)

受信 → マスク(§8.1) → raw_payloads保存 → パース → turns/auto_memory_snapshots へ冪等INSERT。
「生保存 → パース」の二段構成。パース失敗しても raw_payloads には残る。
"""
import json
import os
import re
from pathlib import Path
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

# ---------------------------------------------------------------- 設定

def _read_secret(env_name: str) -> str:
    path = os.environ[env_name]
    return Path(path).read_text().strip()


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
    if not auth.startswith("Bearer ") or auth[7:].strip() != API_TOKEN:
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


def mask_value(value: Any) -> Any:
    """JSON構造を再帰的に辿り、全文字列にマスクを適用する。"""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, list):
        return [mask_value(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_value(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------- project_key正規化 (設計原則5)

def normalize_project_key(git_remote_url: str | None, project_dir: str | None) -> str:
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


# ---------------------------------------------------------------- transcriptパース

def render_content(message: dict) -> str:
    """message.content(文字列 or ブロック配列)をテキストに落とす。"""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False) if content is not None else ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "thinking":
            continue  # thinkingは保存しない
        elif btype == "tool_use":
            name = block.get("name", "?")
            inp = json.dumps(block.get("input", {}), ensure_ascii=False)
            parts.append(f"[tool_use:{name}] {inp}")
        elif btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                text = "\n".join(
                    b.get("text", "") for b in inner
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                text = inner if isinstance(inner, str) else json.dumps(inner, ensure_ascii=False)
            parts.append(f"[tool_result] {text}")
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def parse_transcript(payload: dict, payload_id: int) -> list[dict]:
    """スプールペイロードのtranscript(JSONL文字列)をturns行のリストへ。"""
    device = payload.get("device", "unknown")
    session_id = payload.get("session_id") or "unknown"
    project_key = normalize_project_key(
        payload.get("git_remote_url"), payload.get("project_dir")
    )
    rows: list[dict] = []
    for line in (payload.get("transcript") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # 壊れた行はスキップ(rawには残っている)
        if obj.get("type") not in ("user", "assistant"):
            continue  # summary / system / file-history等は対象外
        message = obj.get("message") or {}
        uuid = obj.get("uuid")
        if not uuid:
            continue
        content = render_content(message)
        if not content:
            continue
        rows.append({
            "device": device,
            "project_key": project_key,
            "session_id": obj.get("sessionId") or session_id,
            "message_uuid": uuid,
            "role": message.get("role") or obj.get("type"),
            "content": content,
            "ts": obj.get("timestamp"),
            "cwd": obj.get("cwd") or payload.get("project_dir"),
            "git_branch": obj.get("gitBranch") or payload.get("git_branch"),
            "model": message.get("model"),
            "payload_id": payload_id,
        })
    return rows


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

    # 1. マスクを適用してから生保存(生JSONにも秘密は残さない — §8.1)
    payload = mask_value(payload)
    device = payload.get("device", "unknown")

    with pool.connection() as conn:
        payload_id = conn.execute(
            "INSERT INTO raw_payloads (device, kind, payload) VALUES (%s, %s, %s) RETURNING id",
            (device, kind, Json(payload)),
        ).fetchone()[0]

        # 2. パース → 冪等INSERT
        try:
            if kind == "transcript":
                rows = parse_transcript(payload, payload_id)
                inserted = 0
                for r in rows:
                    cur = conn.execute(
                        """
                        INSERT INTO turns (device, project_key, session_id, message_uuid,
                                           role, content, ts, cwd, git_branch, model, payload_id)
                        VALUES (%(device)s, %(project_key)s, %(session_id)s, %(message_uuid)s,
                                %(role)s, %(content)s, %(ts)s, %(cwd)s, %(git_branch)s,
                                %(model)s, %(payload_id)s)
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
        except Exception as exc:  # パース失敗はrawに記録して200(データは失われていない)
            conn.execute(
                "UPDATE raw_payloads SET parse_error = %s WHERE id = %s",
                (str(exc)[:2000], payload_id),
            )
            result = {"payload_id": payload_id, "parse_error": str(exc)[:200]}

    return result
