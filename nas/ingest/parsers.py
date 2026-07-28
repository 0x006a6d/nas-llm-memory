"""transcriptパーサ(設計書§4、Codex追補§2.3)

app.py から分離した純stdlib部分。DB・FastAPIに依存しないため、
NAS外でもそのままテストできる。

- parse_transcript      : Claude Code のセッションJSONL
- parse_codex_rollout   : Codex CLI の rollout JSONL(role正規化・決定的ID)
- parse_opencode        : opencode の SQLite を sender が JSONL 化したもの
"""
import datetime
import json

from exclude import normalize_project_key


# ---------------------------------------------------------------- Claude Code

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
        payload.get("git_remote_url"), payload.get("project_dir"), device
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
            "agent": "claude-code",
            "originator": None,  # originatorはCodex rollout固有(追補§2.3)
        })
    return rows


# ---------------------------------------------------------------- Codex CLI (追補§2.3)

def _render_codex_blocks(content) -> str:
    """Codexのcontent(文字列 or {type, text}ブロック配列)をテキストに落とす。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False) if content is not None else ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
        elif block.get("type") in ("input_text", "output_text", "text"):
            parts.append(block.get("text", ""))
        elif block.get("type") == "input_image":
            parts.append("[image]")
        # encrypted_content等は落とす(復号できない)
    return "\n".join(p for p in parts if p)


def parse_codex_rollout(payload: dict, payload_id: int) -> list[dict]:
    """Codex rollout JSONL をturns行のリストへ(追補設計書§2.3)。

    - role正規化: message(user/assistant) / tool呼び出し=assistant / tool出力=tool。
      developerメッセージ(注入指示のボイラープレート)とreasoning(暗号化)は対象外
    - message_uuid: 行番号ベースで決定的に生成(再送しても同じID = 冪等)。
      同一セッションの複数rolloutファイル(resume)でも衝突しないよう
      ファイル名(session_idではなく)を含める
    - session_id: 'codex:' プレフィックス + session_metaのid(無ければファイル名)
    - 増分チャンク対応: senderは新しい行だけを line_offset(0始まりの先頭行番号)付きで
      送ってくる。行番号は常にファイル絶対番号にしてID規約を維持する。
      チャンクにsession_meta/turn_context行が含まれない場合に備え、
      senderが同梱する codex_session_id / context_model / context_cwd
      (チャンク開始時点の最新値)を初期状態として使う
    """
    device = payload.get("device", "unknown")
    file_stem = payload.get("session_id") or "unknown"  # senderはrolloutファイル名(拡張子なし)を入れる
    project_key = normalize_project_key(
        payload.get("git_remote_url"), payload.get("project_dir"), device
    )
    session_id = payload.get("codex_session_id")
    cwd = payload.get("context_cwd") or payload.get("project_dir")
    model = payload.get("context_model")
    # どの入口で使ったか(codex-tui / codex_exec / Codex Desktop / Claude Code)。
    # チャンクにmeta行が無い場合に備えsender同梱値を初期値にする(追補§2.3)
    originator = payload.get("originator")
    try:
        line_offset = int(payload.get("line_offset") or 0)
    except (TypeError, ValueError):
        line_offset = 0
    rows: list[dict] = []
    for lineno, line in enumerate((payload.get("transcript") or "").splitlines(),
                                  1 + line_offset):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # 壊れた行はスキップ(rawには残っている)
        ltype = obj.get("type")
        p = obj.get("payload") or {}
        if ltype == "session_meta":
            session_id = session_id or p.get("id") or p.get("session_id")
            cwd = p.get("cwd") or cwd
            originator = p.get("originator") or originator
            continue
        if ltype == "turn_context":
            model = p.get("model") or model
            cwd = p.get("cwd") or cwd
            continue
        if ltype != "response_item":
            continue  # event_msg / world_state / compacted 等は対象外

        ptype = p.get("type")
        if ptype == "message":
            role = p.get("role")
            if role not in ("user", "assistant"):
                continue  # developer=注入指示のボイラープレート
            content = _render_codex_blocks(p.get("content"))
        elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            role = "assistant"
            args = p.get("arguments") or p.get("input") or ""
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            content = f"[tool_use:{p.get('name', '?')}] {args}"
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            role = "tool"
            out = p.get("output")
            if isinstance(out, str):
                # custom_tool_call_output はブロック配列のJSON文字列のことがある
                try:
                    out = _render_codex_blocks(json.loads(out))
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                out = _render_codex_blocks(out)
            content = f"[tool_result] {out}"
        else:
            continue  # reasoning(暗号化) / agent_message 等は対象外

        if not content or not content.strip():
            continue
        rows.append({
            "device": device,
            "project_key": project_key,
            "session_id": f"codex:{session_id or file_stem}",
            "message_uuid": f"{file_stem}:L{lineno}",
            "role": role,
            "content": content,
            "ts": obj.get("timestamp"),
            "cwd": cwd,
            "git_branch": payload.get("git_branch"),
            "model": model,
            "payload_id": payload_id,
            "agent": "codex",
            "originator": originator,
        })
    return rows


# ---------------------------------------------------------------- opencode

def _epoch_ms_to_iso(ms) -> str | None:
    """opencode のエポックミリ秒をISO8601(UTC)へ。数値でなければNone。"""
    try:
        sec = int(ms) / 1000
    except (TypeError, ValueError):
        return None
    return datetime.datetime.fromtimestamp(sec, datetime.timezone.utc).isoformat()


def _render_opencode_parts(parts) -> str:
    """opencode の part 配列をテキストに落とす。

    text=本文、tool=呼び出しと結果、subtask=サブエージェント起動。
    reasoning(思考)は Claude Code の thinking と同じく保存しない。
    step-start / step-finish は区切りなので落とす。
    """
    out: list[str] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            out.append(part.get("text", ""))
        elif ptype == "tool":
            state = part.get("state") or {}
            name = part.get("tool", "?")
            args = state.get("input")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False) if args is not None else ""
            out.append(f"[tool_use:{name}] {args}")
            result = state.get("output")
            if result:
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
                out.append(f"[tool_result] {result}")
        elif ptype == "subtask":
            out.append(f"[subtask:{part.get('agent') or part.get('command') or '?'}] "
                       f"{part.get('description') or ''}")
        # reasoning / step-start / step-finish は対象外
    return "\n".join(p for p in out if p)


def _opencode_model(message: dict, payload: dict) -> str | None:
    """message から表示用のモデル名を組む(無ければセッションのモデル)。"""
    model = message.get("model")
    if isinstance(model, dict):
        name = model.get("modelID") or model.get("id")
        provider = model.get("providerID")
    else:
        name = message.get("modelID")
        provider = message.get("providerID")
    if not name:
        ctx = payload.get("context_model")
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except json.JSONDecodeError:
                ctx = None
        if isinstance(ctx, dict):
            name = ctx.get("modelID") or ctx.get("id")
            provider = provider or ctx.get("providerID")
    if not name:
        return None
    return f"{provider}/{name}" if provider else name


def parse_opencode(payload: dict, payload_id: int) -> list[dict]:
    """opencode セッションをturns行のリストへ。

    sender が SQLite の message とその part を束ねて 1行1メッセージの JSONL
    ({id, time_created, message, parts})にしたものを受ける。

    - role: message.role をそのまま使う(user/assistant のみ)
    - message_uuid: opencode の message.id(DB全体で一意。再送しても同じ = 冪等)
    - session_id: 'opencode:' プレフィックス + セッションID
    - originator: エージェント種別(build/plan 等)。Codex の originator と同じ枠に入れる
    """
    device = payload.get("device", "unknown")
    session_id = payload.get("session_id") or "unknown"
    project_key = normalize_project_key(
        payload.get("git_remote_url"), payload.get("project_dir"), device
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
        message = obj.get("message") or {}
        uuid = obj.get("id")
        role = message.get("role")
        if not uuid or role not in ("user", "assistant"):
            continue
        content = _render_opencode_parts(obj.get("parts"))
        if not content.strip():
            continue
        ts = obj.get("time_created") or (message.get("time") or {}).get("created")
        rows.append({
            "device": device,
            "project_key": project_key,
            "session_id": f"opencode:{session_id}",
            "message_uuid": uuid,
            "role": role,
            "content": content,
            "ts": _epoch_ms_to_iso(ts),
            "cwd": (message.get("path") or {}).get("cwd") or payload.get("project_dir"),
            "git_branch": payload.get("git_branch"),
            "model": _opencode_model(message, payload),
            "payload_id": payload_id,
            "agent": "opencode",
            "originator": message.get("agent") or payload.get("originator"),
        })
    return rows
