#!/bin/bash
# ingest自己修復watchdog(cron */5分)
# 経緯: 2026-08-05 NAS再起動時、eth0のDHCP取得よりdocker起動が先行し、ingestの
# LAN IPへのbindが失敗したままexitedで取り残された(restartポリシーは
# 「起動失敗」をリトライしない)。/healthが落ちていたら作り直して自動回復する。
set -u

SYSTEM_DIR="/volume2/claude-system"
# bind先IPはdocker-compose.ymlと同じ出所(nas/.envのINGEST_BIND_IP)から取る
BIND_ADDR=$(sed -n 's/^INGEST_BIND_IP=//p' "$SYSTEM_DIR/.env")
HEALTH_URL="https://$BIND_ADDR:8800/health"
# ingestが提示する自己署名証明書そのもの(端末側setup.shがピン止めするのと同一ファイル)。
# 検証なし(-k)だと想定外のリスナーの200で健全と誤認しうるため、ピン止めで判定する
CERT="$SYSTEM_DIR/ingest/secrets/tls_cert.pem"
# docker daemonごと固まっているとcompose操作が返らずロックを握り続けるため上限を切る
COMPOSE_TIMEOUT=120

# 多重起動防止(compose操作の重複を避ける。健全時パスはロック不要だが一律で取る)
exec 9> "$SYSTEM_DIR/batch/.watchdog.lock"
flock -n 9 || exit 0

ts() { date '+%Y-%m-%d %H:%M:%S'; }

if [ -z "$BIND_ADDR" ]; then
    echo "$(ts) INGEST_BIND_IPが$SYSTEM_DIR/.envに無い。判定できないため何もしない"
    exit 1
fi
if [ ! -r "$CERT" ]; then
    echo "$(ts) 証明書が読めない($CERT)。健全性を判定できないため何もしない"
    exit 1
fi

check_health() {
    curl -s --cacert "$CERT" --output /dev/null --write-out '%{http_code}' \
         --max-time 10 "$HEALTH_URL" || true
}

code=$(check_health)
[ "$code" = "200" ] && exit 0

# bind先アドレスが無い間はrecreateしても同じ失敗を繰り返すだけなので待つ
# (DHCP停止でアドレス喪失中のログ肥大と無駄なchurnを避ける)
if ! ip -4 addr show | grep -q "inet $BIND_ADDR/"; then
    echo "$(ts) health=$code, $BIND_ADDR 未割当のため待機"
    exit 0
fi

echo "$(ts) health=$code, ingestをforce-recreateする"
cd "$SYSTEM_DIR" || exit 1
if ! timeout "$COMPOSE_TIMEOUT" docker compose up -d --force-recreate ingest; then
    echo "$(ts) compose upが失敗またはタイムアウト"
    exit 1
fi

sleep 10
code=$(check_health)
if [ "$code" = "200" ]; then
    echo "$(ts) 回復した(health=200)"
else
    echo "$(ts) 回復せず(health=$code)。次回again"
    timeout 30 docker compose ps ingest
    exit 1
fi
