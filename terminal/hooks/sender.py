#!/usr/bin/env python3
"""sender — スプール内の未送信分をNAS ingest APIへPOSTする(設計書§3.2)

at-least-once。重複はDB側UNIQUE制約で吸収される。
NAS到達不能時は静かに諦める(スプールに残ることが記録)。
"""
import fcntl
import json
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SPOOL = Path.home() / ".claude-spool"
CONFIG = SPOOL / "config.json"
SENT_KEEP_DAYS = 14  # 障害復旧用にsentを保持(設計書§10 P0)


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
    if not pending.exists():
        return
    sent.mkdir(parents=True, exist_ok=True)

    # 多重起動防止
    lock = open(SPOOL / ".sender.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # 既に別のsenderが動いている

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
