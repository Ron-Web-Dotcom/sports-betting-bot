#!/bin/bash
# Sports Intelligence Platform — VPS Deploy Script
# Ubuntu 22.04/24.04 | Run as root from /root
# Usage: bash deploy.sh

set -e
REPO="https://github.com/ron-web-dotcom/sports-betting-bot"
APP_DIR="/root/sports-bot"
VENV="$APP_DIR/venv"
PLATFORM="$APP_DIR/platform"

echo "==> [1/8] System packages"
apt-get update -qq
apt-get install -y -qq \
    python3.12 python3.12-venv python3.12-dev \
    redis-server git curl build-essential

echo "==> [2/8] Configure Redis (32MB cap, db 0-2 for sports bot)"
cat > /etc/redis/redis.conf.d/sports-bot.conf 2>/dev/null || true
# Patch main redis.conf
grep -q "maxmemory 64mb" /etc/redis/redis.conf || \
    echo -e "\nmaxmemory 64mb\nmaxmemory-policy allkeys-lru" >> /etc/redis/redis.conf
systemctl enable redis-server
systemctl restart redis-server
echo "    Redis running: $(redis-cli ping)"

echo "==> [3/8] Clone / update repo"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull origin claude/dreamy-mayer-qu9LQ
else
    git clone -b claude/dreamy-mayer-qu9LQ "$REPO" "$APP_DIR"
fi

echo "==> [4/8] Python virtual environment"
python3.12 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$PLATFORM/requirements.txt" -q

echo "==> [5/8] Environment file"
if [ ! -f "$PLATFORM/.env" ]; then
    cp "$PLATFORM/.env.template" "$PLATFORM/.env" 2>/dev/null || cat > "$PLATFORM/.env" << 'ENV'
# === Sports Intelligence Platform — Environment ===
# Fill in ALL values before starting services

OPENAI_API_KEY=
ODDS_API_KEY=
DISCORD_WEBHOOK_URL=

# Leave these as-is (SQLite + local Redis)
USE_SQLITE=true
ENVIRONMENT=development
REDIS_URL=redis://localhost:6379/3
CELERY_BROKER=redis://localhost:6379/4
CELERY_BACKEND=redis://localhost:6379/5

# Optional
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY=
ENV
    echo ""
    echo "  *** EDIT YOUR SECRETS: nano $PLATFORM/.env ***"
    echo ""
fi

echo "==> [6/8] Initialize database"
cd "$PLATFORM"
"$VENV/bin/python" -c "
from src.db.session import engine
from src.db.models import Base
Base.metadata.create_all(engine)
print('    DB tables created.')
"

echo "==> [7/8] systemd service files"

# Celery Worker (solo pool — single process, low RAM)
cat > /etc/systemd/system/sports-bot-worker.service << EOF
[Unit]
Description=Sports Bot — Celery Worker
After=network.target redis-server.service
Requires=redis-server.service

[Service]
Type=simple
User=root
WorkingDirectory=$PLATFORM
EnvironmentFile=$PLATFORM/.env
ExecStart=$VENV/bin/celery -A src.workers.celery_app worker \
    --loglevel=info \
    --pool=solo \
    --concurrency=1 \
    --max-tasks-per-child=50 \
    --logfile=/var/log/sports-bot/worker.log
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Celery Beat (scheduler)
cat > /etc/systemd/system/sports-bot-beat.service << EOF
[Unit]
Description=Sports Bot — Celery Beat Scheduler
After=network.target redis-server.service
Requires=redis-server.service

[Service]
Type=simple
User=root
WorkingDirectory=$PLATFORM
EnvironmentFile=$PLATFORM/.env
ExecStart=$VENV/bin/celery -A src.workers.celery_app beat \
    --loglevel=info \
    --logfile=/var/log/sports-bot/beat.log
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "==> [8/8] Enable and start services"
mkdir -p /var/log/sports-bot
systemctl daemon-reload
systemctl enable sports-bot-worker sports-bot-beat
# Don't auto-start yet — user must fill .env first
echo ""
echo "================================================================"
echo "  Deploy complete! Before starting:"
echo ""
echo "  1. Fill in your secrets:"
echo "     nano $PLATFORM/.env"
echo ""
echo "  2. Start the bot:"
echo "     systemctl start sports-bot-worker sports-bot-beat"
echo ""
echo "  3. Check logs:"
echo "     journalctl -u sports-bot-worker -f"
echo "     journalctl -u sports-bot-beat -f"
echo ""
echo "  4. Check status:"
echo "     systemctl status sports-bot-worker sports-bot-beat"
echo "================================================================"
