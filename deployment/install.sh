#!/usr/bin/env bash
# VPS deployment script for sports-betting-bot
set -euo pipefail

APP_DIR="/opt/sports-betting-bot"
SERVICE_USER="betting"

echo "==> Creating service user"
id "$SERVICE_USER" &>/dev/null || useradd --system --no-create-home "$SERVICE_USER"

echo "==> Installing system deps"
apt-get update -qq
apt-get install -y --no-install-recommends python3.11 python3.11-venv python3-pip sqlite3

echo "==> Setting up app directory"
mkdir -p "$APP_DIR"/{data,logs}
cp -r . "$APP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

echo "==> Creating virtualenv and installing Python deps"
sudo -u "$SERVICE_USER" python3.11 -m venv "$APP_DIR/venv"
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

echo "==> Copying .env template"
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "  ⚠️  Edit $APP_DIR/.env and add your API keys before starting the service"
fi

echo "==> Installing systemd service"
cp "$APP_DIR/deployment/sports-betting-bot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable sports-betting-bot

echo ""
echo "✅ Installation complete!"
echo ""
echo "  1. Edit your API keys:  nano $APP_DIR/.env"
echo "  2. Start the bot:       systemctl start sports-betting-bot"
echo "  3. View logs:           journalctl -u sports-betting-bot -f"
