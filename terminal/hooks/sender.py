#!/usr/bin/env python3
"""sender — スプール内の未送信分をNAS ingest APIへPOSTする(設計書§3.2)

at-least-once。重複はDB側UNIQUE制約で吸収される。
NAS到達不能時は静かに諦める(スプールに残ることが記録)。
"""
import fcntl
import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# import失敗(部分配布・古いcheckout等)でも本来の送信は止めない。
# 除外判定が使えない間はCodex走査を行わない(fail-closed: 新しい収集経路は
# 除外リストを適用できることが前提)
try:
    import agents_sync
except Exception:
    agents_sync = None
try:
    import exclude
except Exception:
    exclude = None

SPOOL = Path.home() / ".claude-spool"
CONFIG = SPOOL / "config.json"
CONFIG_DIR = Path(__file__).resolve().parent.parent  # ~/claude-config
SENT_KEEP_DAYS = 14   # 障害復旧用にsentを保持(設計書§10 P0)
CODEX_MIN_AGE = 300   # 書きかけrollout回避: mtimeが5分以上前のみ送る(Codex追補§2.1)
CODEX_CHUNK_BYTES = 8_000_000  # 1ペイロードに含める行データの上限(巨大rolloutを分割)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 追従しない → HTTPErrorになり送信失敗として扱われる


def _post_curl(url: str, token: str, cert: str, path: Path) -> bool:
    """urllibで送れない環境向けのcurl代替送信。

    macOS 15+のローカルネットワーク権限は非AppleバイナリのLAN接続を
    EHOSTUNREACHで無言拒否する(brew Pythonが該当。GUIで許可しても
    Python更新で失効しうる)。Apple製 /usr/bin/curl は権限免除のため通る。
    セキュリティ特性はurllib経路と同等を維持する:
    - --cacert でピン止め証明書を強制(検証失敗はcurlが非0で失敗)
    - リダイレクトは-L無しのcurlは追従せず、http_code!=200として失敗扱い
    - トークンはargvに出さず(psで見える)、`-H @-` でstdinから渡す
    """
    curl = "/usr/bin/curl" if Path("/usr/bin/curl").exists() else "curl"
    try:
        r = subprocess.run(
            [curl, "--silent", "--output", "/dev/null",
             "--write-out", "%{http_code}", "--max-time", "60",
             "--cacert", cert,
             "--header", "Content-Type: application/json",
             "--header", "@-",
             "--data-binary", f"@{path}", url],
            input=f"Authorization: Bearer {token}",
            capture_output=True, text=True, timeout=90)
    except Exception:
        return False
    return r.returncode == 0 and r.stdout.strip() == "200"


def _iso(ts):
    t = time.localtime(ts)
    s = time.strftime("%Y-%m-%dT%H:%M:%S%z", t)
    return s[:-2] + ":" + s[-2:]


def _git_remote(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
        out = r.stdout.strip()
        return out if r.returncode == 0 and out else None
    except Exception:
        return None


def spool_codex():
    """Codex rolloutの走査(Codex追補§2.1)。

    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl のうち未送信/更新分を
    スプール形式に包んでpendingへ置く(送信は通常の送信ループに任せる)。

    増分送信: rolloutはappend-onlyなので、送信済み行数を codex-sent.jsonl に
    記録し、新しい完全行(改行で終わった行)だけを line_offset 付きで送る。
    常駐セッションの巨大rolloutを成長のたびに全量再送しないための仕組み。
    行番号は常にファイル先頭からの絶対番号で、ingest側のID規約
    (<ファイル名>:L<行番号>)と一致する。1ペイロードはCODEX_CHUNK_BYTESで分割。
    ファイルが縮んだ場合(ローテーション等)は行0から送り直し、
    重複はDB側UNIQUEが吸収する。
    """
    if exclude is None:
        return  # 除外判定なしで新規収集経路を動かさない(fail-closed)
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return  # Codex未使用端末
    state_path = SPOOL / "codex-sent.jsonl"
    state = {}
    if state_path.exists():
        for line in state_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                # 旧形式(lines無し)は0扱い: 次に変化したとき一度だけ全量再送になる
                state[r["path"]] = (r["size"], r["mtime"], r.get("lines", 0))
            except Exception:
                pass

    excludes = exclude.load_entries(CONFIG_DIR / "sync-exclude.txt")
    device = socket.gethostname()
    pending = SPOOL / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    now = time.time()
    changed = False
    for f in sorted(sessions.glob("*/*/*/rollout-*.jsonl")):
        try:
            st = f.stat()
        except OSError:
            continue
        if now - st.st_mtime < CODEX_MIN_AGE:
            continue  # 書きかけの可能性: 次回以降に回す
        sig = (st.st_size, int(st.st_mtime))
        prev = state.get(str(f))
        if prev and (prev[0], prev[1]) == sig:
            continue
        # 縮んだ(ローテーション/書き直し)場合は先頭から送り直す
        sent_lines = prev[2] if prev and st.st_size >= prev[0] else 0
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        all_lines = text.splitlines()
        # 末尾の改行未達の行は書きかけの可能性があるため次回に回す
        full_lines = all_lines if text.endswith("\n") else all_lines[:-1]

        # rollout冒頭のsession_meta(通常1行目)からcwd・セッションID・originatorを読む。
        # cwdは除外判定(§8.3)、IDとoriginatorは増分チャンク(meta行を含まない)の帰属に使う
        cwd = meta_id = originator = None
        for line in full_lines[:200]:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "session_meta" and not meta_id:
                p = obj.get("payload") or {}
                meta_id = p.get("id") or p.get("session_id")
                originator = p.get("originator")
            if obj.get("type") in ("session_meta", "turn_context") and not cwd:
                cwd = (obj.get("payload") or {}).get("cwd")
            if cwd and meta_id:
                break
        remote = _git_remote(cwd) if cwd and Path(cwd).is_dir() else None
        if not exclude.is_excluded(
                excludes,
                project_key=exclude.normalize_project_key(remote, cwd),
                project_dir=cwd):
            # チャンクはturn_context/session_meta行を含まないことがあるため、
            # 「チャンク開始時点の最新model/cwd」を同梱してparserに引き継ぐ。
            # json.loadsは該当typeの行だけに限定する(巨大ファイルの全行parse回避)
            ctx_model = None
            ctx_cwd = cwd

            def _track_context(lines_range):
                nonlocal ctx_model, ctx_cwd
                for ln in lines_range:
                    if '"turn_context"' not in ln and '"session_meta"' not in ln:
                        continue
                    try:
                        o = json.loads(ln)
                    except Exception:
                        continue
                    p = o.get("payload") or {}
                    if o.get("type") == "turn_context":
                        ctx_model = p.get("model") or ctx_model
                        ctx_cwd = p.get("cwd") or ctx_cwd
                    elif o.get("type") == "session_meta":
                        ctx_cwd = p.get("cwd") or ctx_cwd

            _track_context(full_lines[:sent_lines])  # 送信済み範囲の文脈を復元
            start = sent_lines
            while start < len(full_lines):
                i = start
                acc = 0
                while i < len(full_lines):
                    # UTF-8バイト数で追加前に判定(超過は1行単独送信の場合のみ)
                    b = len(full_lines[i].encode("utf-8")) + 1
                    if i > start and acc + b > CODEX_CHUNK_BYTES:
                        break
                    acc += b
                    i += 1
                # 決定的event_id: 同じ(ファイル, 開始行, サイズ, mtime)は
                # 再実行しても二重投入されない
                event_id = "codex-" + hashlib.sha1(
                    f"{device}:{f}:{start}:{sig[0]}:{sig[1]}".encode()).hexdigest()
                payload = json.dumps({
                    "device": device,
                    "kind": "transcript",
                    "agent": "codex",
                    "event_id": event_id,
                    "session_id": f.stem,   # ingest側パーサがsession_metaのidを優先する
                    "codex_session_id": meta_id,  # チャンクにmeta行が無いときの帰属先
                    "originator": originator,  # どの入口か(codex-tui / codex_exec / Codex Desktop / Claude Code)
                    "line_offset": start,   # このチャンクの先頭行番号(0始まり)
                    "context_model": ctx_model,  # チャンク開始時点の最新turn_context
                    "context_cwd": ctx_cwd,
                    "project_dir": cwd,
                    "git_remote_url": remote,
                    "git_branch": None,
                    "transcript": "\n".join(full_lines[start:i]),
                    "client_version": None,
                    "captured_at": _iso(now),
                }, ensure_ascii=False)
                tmp = pending / (event_id + ".json.tmp")
                tmp.write_text(payload, encoding="utf-8")
                tmp.rename(pending / (event_id + ".json"))
                _track_context(full_lines[start:i])  # 次チャンク用に文脈を進める
                start = i
        # 除外分も走査済みとして記録(毎回読み直さない)
        state[str(f)] = (sig[0], sig[1], len(full_lines))
        changed = True

    if changed:
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text("\n".join(
            json.dumps({"path": p, "size": s, "mtime": m, "lines": n}, ensure_ascii=False)
            for p, (s, m, n) in sorted(state.items())) + "\n", encoding="utf-8")
        tmp.rename(state_path)


def main():
    if not CONFIG.exists():
        return
    cfg = json.loads(CONFIG.read_text())
    url = cfg["ingest_url"].rstrip("/") + "/ingest"
    token = cfg["api_token"]

    # ingestは自己署名TLS: https + setup.shがピン止めした証明書(tls_cert)を必須にする。
    # httpや証明書未取得のまま送るとBearerトークンと全ペイロードが平文/検証なしで流れるため、
    # 条件が揃わない間は送信しない(スプールに残り、証明書取得後に再送される)
    if not url.startswith("https"):
        return
    cert = cfg.get("tls_cert")
    if not cert or not Path(cert).exists():
        return
    ctx = ssl.create_default_context(cafile=cert)
    # リダイレクト追従を拒否: urllibは3xxでAuthorizationヘッダごと転送先へ再送するため、
    # http/別ホストへ誘導されるとBearerトークンが漏れる。3xxはHTTPError=失敗として扱う
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ctx))

    pending = SPOOL / "pending"
    sent = SPOOL / "sent"
    pending.mkdir(parents=True, exist_ok=True)
    sent.mkdir(parents=True, exist_ok=True)

    # 多重起動防止
    lock = open(SPOOL / ".sender.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # 既に別のsenderが動いている

    # Codex rolloutの走査(失敗しても通常送信は続ける)
    try:
        spool_codex()
    except Exception:
        pass
    # indexの配布(Codex追補§3): general→~/.codex/AGENTS.md、
    # 登録済みプロジェクト→<project>/AGENTS.override.md
    try:
        if agents_sync is not None:
            agents_sync.update_global_agents(CONFIG_DIR)
            agents_sync.update_project_agents(CONFIG_DIR)
    except Exception:
        pass

    use_curl = False
    for f in sorted(pending.glob("*.json")):
        ok = False
        if use_curl:
            ok = _post_curl(url, token, cert, f)
        else:
            try:
                req = urllib.request.Request(
                    url,
                    data=f.read_bytes(),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                )
                with opener.open(req, timeout=60) as resp:
                    ok = resp.status == 200
            except Exception:
                # urllibが失敗してもcurlなら通る環境がある(_post_curl docstring)。
                # curlでも失敗なら本当に到達不能: 次回再送
                if _post_curl(url, token, cert, f):
                    ok = use_curl = True
        if not ok:
            return
        f.rename(sent / f.name)

    # sentの世代整理
    cutoff = time.time() - SENT_KEEP_DAYS * 86400
    for f in sent.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
