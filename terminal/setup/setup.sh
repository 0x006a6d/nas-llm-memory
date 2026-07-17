#!/bin/bash
# 端末セットアップスクリプト(設計書§3.4)— 冪等。何度実行してもよい。
#
# 使い方:
#   git clone ssh://NAS_USER@NAS_IP/volume2/claude-system/repos/claude-config.git ~/claude-config
#   ~/claude-config/setup/setup.sh
#
# APIトークンは初回実行時に対話入力(NASの /volume2/claude-system/ingest/secrets/api_token の値)
set -u

: "${NAS_IP:?環境変数 NAS_IP を設定してください (例: NAS_IP=192.168.x.x $0)}"
INGEST_URL="https://${NAS_IP}:8800"
# 配置パスはスクリプト自身の位置から導出する(クローン先の名前に依存しない)。
# hooks/ templates/ はこのスクリプトの親ディレクトリ直下にある前提
CONFIG_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)" || exit 1
CLAUDE_DIR="$HOME/.claude"
SPOOL_DIR="$HOME/.claude-spool"

echo "== claude-config 端末セットアップ ($CONFIG_DIR) =="

# 0. 前提確認
command -v python3 >/dev/null || { echo "ERROR: python3 が必要です"; exit 1; }
command -v git >/dev/null || { echo "ERROR: git が必要です"; exit 1; }
[ -d "$CONFIG_DIR/hooks" ] || { echo "ERROR: $CONFIG_DIR/hooks がありません"; exit 1; }

mkdir -p "$CLAUDE_DIR" "$SPOOL_DIR/pending" "$SPOOL_DIR/sent"
chmod +x "$CONFIG_DIR"/hooks/*.sh "$CONFIG_DIR"/hooks/*.py 2>/dev/null

# 1. skills symlink(配布リポジトリにskillsがある場合のみ)
if [ -d "$CONFIG_DIR/skills" ]; then
    if [ -e "$CLAUDE_DIR/skills" ] && [ ! -L "$CLAUDE_DIR/skills" ]; then
        echo "  既存の $CLAUDE_DIR/skills を skills.bak に退避"
        mv "$CLAUDE_DIR/skills" "$CLAUDE_DIR/skills.bak"
    fi
    ln -sfn "$CONFIG_DIR/skills" "$CLAUDE_DIR/skills"
    echo "  skills → $CONFIG_DIR/skills"
fi

# 2. settings.json にhooksをマージ(既存設定は保持)
python3 - "$CLAUDE_DIR/settings.json" "$CONFIG_DIR/templates/settings.json.tmpl" "$CONFIG_DIR" <<'PYEOF'
import json, os, sys
settings_path, tmpl_path, config_dir = sys.argv[1], sys.argv[2], sys.argv[3]

# 置換はJSON解析後に行う: 生テキスト置換だとパスに " や \ が入ったときJSONが壊れる
def subst(o):
    if isinstance(o, str):
        return o.replace("{{CONFIG_DIR}}", config_dir)
    if isinstance(o, list):
        return [subst(v) for v in o]
    if isinstance(o, dict):
        return {k: subst(v) for k, v in o.items()}
    return o

tmpl = subst(json.load(open(tmpl_path)))
settings = {}
if os.path.exists(settings_path):
    settings = json.load(open(settings_path))
hooks = settings.setdefault("hooks", {})
for event, entries in tmpl["hooks"].items():
    existing = json.dumps(hooks.get(event, []))
    for entry in entries:
        for h in entry["hooks"]:
            if h["command"] not in existing:
                hooks.setdefault(event, []).append(entry)
                break
json.dump(settings, open(settings_path, "w"), indent=2, ensure_ascii=False)
print("  settings.json: hooks設定を確認/追記")
PYEOF

# 3. ingest TLS証明書のピン止め(fingerprintの目視照合を通ったものだけ保存する。
#    照合前に保存するとMITM証明書まで永続的に信頼してしまう)
CERT_FILE="$SPOOL_DIR/ingest_cert.pem"
if [ ! -f "$CERT_FILE" ]; then
    CERT_TMP=$(mktemp)
    if command -v openssl >/dev/null \
        && openssl s_client -connect "${NAS_IP}:8800" </dev/null 2>/dev/null \
           | openssl x509 > "$CERT_TMP" 2>/dev/null; then
        echo "  取得したingest証明書のfingerprint:"
        openssl x509 -in "$CERT_TMP" -noout -fingerprint -sha256 | sed 's/^/    /'
        printf "  NAS側 gen_tls_cert.sh が表示した値と一致しますか? [y/N] "
        read -r ANS
        case "$ANS" in
        y|Y)
            mv "$CERT_TMP" "$CERT_FILE"
            chmod 600 "$CERT_FILE"
            echo "  証明書をピン止めしました"
            ;;
        *)
            rm -f "$CERT_TMP"
            echo "  証明書を保存しませんでした。senderはピン止め証明書が無い間は送信しません"
            ;;
        esac
    else
        rm -f "$CERT_TMP"
        echo "  WARN: ingest証明書を取得できませんでした(NASのingest未起動?)。起動後に再実行してください"
    fi
fi

# 4. スプール設定(APIトークン: 画面にもargvにも出さない)
if [ ! -f "$SPOOL_DIR/config.json" ]; then
    printf "  NAS ingest APIトークンを入力(非表示): "
    read -rs TOKEN
    echo
    umask 077
    TOKEN="$TOKEN" python3 - "$SPOOL_DIR/config.json" "$INGEST_URL" "$CERT_FILE" <<'PYEOF'
import json, os, sys
json.dump({"ingest_url": sys.argv[2], "api_token": os.environ["TOKEN"],
           "tls_cert": sys.argv[3]}, open(sys.argv[1], "w"))
PYEOF
    chmod 600 "$SPOOL_DIR/config.json"
    echo "  config.json 作成"
else
    echo "  config.json は既存(スキップ)"
fi

# 5. senderの定期実行(1時間おき)
case "$(uname -s)" in
Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.claude.spool-sender.plist"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.claude.spool-sender</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v python3)</string>
    <string>$CONFIG_DIR/hooks/sender.py</string>
  </array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null
    launchctl load "$PLIST"
    echo "  launchd: 1時間おきのsender登録"
    ;;
Linux)
    CRON_LINE="17 * * * * python3 $CONFIG_DIR/hooks/sender.py"
    ( crontab -l 2>/dev/null | grep -v "hooks/sender.py"; echo "$CRON_LINE" ) | crontab -
    echo "  cron: 1時間おきのsender登録"
    ;;
esac

# 6. ユーザーレベルCLAUDE.mdへの@import(設計書§7)
USER_MD="$CLAUDE_DIR/CLAUDE.md"
IMPORT_LINE="@$CONFIG_DIR/memory/general/index.md"
# 判定はパス表記に依存させない: 旧セットアップは `@~/claude-config/...` 形式で
# 書いており、絶対パスの完全一致だと二重追記になる。
# 行頭の @import のみ有効とみなす(コメント等での言及に反応しない)
if ! grep -qE '^@.*memory/general/index\.md' "$USER_MD" 2>/dev/null; then
    printf "\n%s\n" "$IMPORT_LINE" >> "$USER_MD"
    echo "  CLAUDE.md: general index を@import"
fi

echo "== 完了 =="
echo "プロジェクトごとのindex注入は、各プロジェクトのCLAUDE.mdに"
echo "  @$CONFIG_DIR/memory/<project-key>/index.md"
echo "を追記してください(indexは夜間バッチが生成した時点から有効)"
