#!/bin/bash
# ingest用の自己署名TLS証明書を生成(NAS上で一度実行してから docker compose up -d)
# 使い方: ./gen_tls_cert.sh <NASのLAN IP> [出力dir=./secrets]
set -euo pipefail

IP="${1:?使い方: $0 <NASのLAN IP> [出力dir]}"
DIR="${2:-$(dirname "$0")/secrets}"

mkdir -p "$DIR"
umask 077
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$DIR/tls_key.pem" -out "$DIR/tls_cert.pem" \
    -subj "/CN=claude-memory-ingest" \
    -addext "subjectAltName=IP:$IP"
chmod 644 "$DIR/tls_cert.pem"

echo "generated: $DIR/tls_cert.pem"
echo "端末側 setup.sh が表示するfingerprintと以下が一致することを確認する:"
openssl x509 -in "$DIR/tls_cert.pem" -noout -fingerprint -sha256
