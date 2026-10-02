#!/usr/bin/env bash
# ------------------------------------------------------------------
# Pragna AI voice demo - one-command install / update for Ubuntu
#
#   First time :  sudo bash deploy.sh
#   Own domain :  sudo bash deploy.sh demo.yourdomain.com
#   Update     :  sudo bash /opt/voice-demo/deploy.sh      (after a git push)
#
# What it does: installs Python + Git + Caddy, downloads the code from
# GitHub into /opt/voice-demo, installs the Python packages and headless
# Chrome, asks for your GHL details (first time only), starts the app as
# a service that restarts by itself, and puts HTTPS in front of it.
# ------------------------------------------------------------------
set -euo pipefail

# Everything is inside main() so the script keeps working when "git reset" replaces this file mid-run.
main() {

  REPO="https://github.com/srinivasareddy-syamala/voice-demo.git"
  APP_DIR="/opt/voice-demo"
  BASE_DOMAIN="vps-7263.onecom-cloud.one"       # always served, so the site never goes offline
  DOMAIN="${1:-$BASE_DOMAIN}"                   # optional extra address, e.g. try.pragna.ai
  if [ "$DOMAIN" = "$BASE_DOMAIN" ]; then SITES="$BASE_DOMAIN"; else SITES="$BASE_DOMAIN, $DOMAIN"; fi
  APP_USER="voicedemo"

  if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:   sudo bash deploy.sh"
    exit 1
  fi
  export DEBIAN_FRONTEND=noninteractive

  echo
  echo "==> [1/7] Installing system packages (Python, Git, Caddy)..."
  apt-get update -y -q
  apt-get install -y -q python3 python3-venv python3-pip git curl gnupg ca-certificates \
                        debian-keyring debian-archive-keyring apt-transport-https
  if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y -q
    apt-get install -y -q caddy
  fi

  echo
  echo "==> [2/7] Getting the code from GitHub..."
  git config --global --add safe.directory "$APP_DIR" || true
  if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --quiet origin
    git -C "$APP_DIR" reset --hard origin/main      # .env and .venv are not in git, they are kept
  else
    git clone --quiet "$REPO" "$APP_DIR"
  fi
  cd "$APP_DIR"

  echo
  echo "==> [3/7] Installing Python packages (first time takes a few minutes)..."
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt

  echo
  echo "==> [4/7] Installing headless Chrome (for screenshots and hard-to-read websites)..."
  export PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.browsers"
  .venv/bin/python -m playwright install --with-deps chromium \
    || echo "    (Chrome could not be installed - the app still works, with fewer fallbacks)"

  echo
  echo "==> [5/7] Settings (.env)..."
  if [ ! -f .env ]; then
    echo "    Enter your GoHighLevel details. Press Enter to accept a value shown in [brackets]."
    read -r -s -p "    GHL token (starts with pit-, typing is hidden): " GHL_KEY; echo
    read -r -p "    GHL Location ID [s9jsy9dp0zOh0nDsRvcD]: " GHL_LOC
    read -r -p "    GHL Voice AI Agent ID [6abdf3955deba8d1c1f3ac30]: " GHL_AGENT
    read -r -p "    WhatsApp number [+44 7446 952720]: " WA
    cat > .env <<EOF
GHL_API_KEY=${GHL_KEY}
GHL_LOCATION_ID=${GHL_LOC:-s9jsy9dp0zOh0nDsRvcD}
GHL_AGENT_ID=${GHL_AGENT:-6abdf3955deba8d1c1f3ac30}
GHL_VOICE_API_VERSION=2021-07-28
GHL_AGENT_PHONE=
CORS_ORIGINS=*
BRAND_NAME=Pragna AI
WHATSAPP_NUMBER=${WA:-+44 7446 952720}
THUM_IO_AUTH=
WIDGET_AUTO_OPEN=1
EOF
    echo "    Saved. To change later:  sudo nano $APP_DIR/.env   then   sudo systemctl restart voice-demo"
  else
    echo "    Keeping the existing .env"
  fi
  chmod 600 .env

  echo
  echo "==> [6/7] Starting the app as a service..."
  id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
  cat > /etc/systemd/system/voice-demo.service <<EOF
[Unit]
Description=Pragna AI voice demo
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PLAYWRIGHT_BROWSERS_PATH=$APP_DIR/.browsers
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --quiet voice-demo
  systemctl restart voice-demo

  echo
  echo "==> [7/7] HTTPS web address (Caddy gets the certificate automatically)..."
  cat > /etc/caddy/Caddyfile <<EOF
$SITES {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
EOF
  systemctl enable --quiet caddy
  systemctl restart caddy
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  fi

  sleep 4
  echo
  if curl -fsS http://127.0.0.1:8000/api/config >/dev/null 2>&1; then
    echo "App is running."
  else
    echo "The app did not answer yet. See the reason with:  sudo journalctl -u voice-demo -n 40 --no-pager"
  fi
  echo
  echo "=================================================================="
  echo "  Your website:   https://$BASE_DOMAIN"
  if [ "$DOMAIN" != "$BASE_DOMAIN" ]; then
    echo "  Also (once its DNS points to this server):   https://$DOMAIN"
  fi
  echo "=================================================================="
  echo "  (the first visit can take up to a minute while HTTPS is set up)"
  echo
  echo "  App log      :  sudo journalctl -u voice-demo -f"
  echo "  Restart app  :  sudo systemctl restart voice-demo"
  echo "  Update code  :  sudo bash $APP_DIR/deploy.sh $DOMAIN"
  echo "  HTTPS log    :  sudo journalctl -u caddy -n 40 --no-pager"
}

main "$@"
