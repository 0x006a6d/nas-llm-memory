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

import agents_sync
import exclude

SPOOL = Path.home() / ".claude-spool"
CONFIG = SPOOL / "config.json"
CONFIG_DIR = Path(__file__).resolve().parent.parent  # ~/claude-config
SENT_KEEP_DAYS = 14   # 障害復旧用にsentを保持(設計書§10 P0)
CODEX_MIN_AGE = 300   # 書きかけrollout回避: mtimeが5分以上前のみ送る(Codex追補§2.1)


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
    送信済みは (パス, サイズ, mtime) を codex-sent.jsonl に記録して差分だけ包む。
    resume等でファイルが伸びたら全体を再送し、重複はDB側UNIQUEが吸収する。
    """
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
                state[r["path"]] = (r["size"], r["mtime"])
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
        if state.get(str(f)) == sig:
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        # rollout冒頭のsession_metaからcwdを読み、除外判定する(§8.3)
        cwd = None
        for line in text.splitlines()[:20]:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "session_meta":
                cwd = (obj.get("payload") or {}).get("cwd")
                break
        remote = _git_remote(cwd) if cwd and Path(cwd).is_dir() else None
        if not exclude.is_excluded(
                excludes,
                project_key=exclude.normalize_project_key(remote, cwd),
                project_dir=cwd):
            # 決定的event_id: 同じ(ファイル, サイズ, mtime)は再実行しても二重投入されない
            event_id = "codex-" + hashlib.sha1(
                f"{device}:{f}:{sig[0]}:{sig[1]}".encode()).hexdigest()
            payload = json.dumps({
                "device": device,
                "kind": "transcript",
                "agent": "codex",
                "event_id": event_id,
                "session_id": f.stem,   # ingest側パーサがsession_metaのidを優先する
                "project_dir": cwd,
                "git_remote_url": remote,
                "git_branch": None,
                "transcript": text,
                "client_version": None,
                "captured_at": _iso(now),
            }, ensure_ascii=False)
            tmp = pending / (event_id + ".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.rename(pending / (event_id + ".json"))
        state[str(f)] = sig  # 除外分も走査済みとして記録(毎回読み直さない)
        changed = True

    if changed:
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text("\n".join(
            json.dumps({"path": p, "size": s, "mtime": m}, ensure_ascii=False)
            for p, (s, m) in sorted(state.items())) + "\n", encoding="utf-8")
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
    # general indexを ~/.codex/AGENTS.md の管理セクションへ配布(Codex追補§3.1)
    try:
        agents_sync.update_global_agents(CONFIG_DIR)
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
