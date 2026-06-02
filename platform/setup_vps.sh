#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Sports Intelligence Platform — VPS Setup Script
# Run once on a fresh Ubuntu 22.04 / Debian 12 VPS.
# Usage: bash setup_vps.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 1. Preflight checks ───────────────────────────────────────────────────────
info "Checking prerequisites..."
[[ -f .env ]] || error ".env file not found. Copy .env.example to .env and fill in your values."
grep -q "^ANTHROPIC_API_KEY=sk-ant" .env || error "ANTHROPIC_API_KEY not set in .env"
grep -q "^ODDS_API_KEY=" .env          || error "ODDS_API_KEY not set in .env"
grep -q "^POSTGRES_PASSWORD=" .env     || error "POSTGRES_PASSWORD not set in .env"
PGPASS=$(grep "^POSTGRES_PASSWORD=" .env | cut -d= -f2)
[[ "$PGPASS" == "...choose-a-strong-password..." ]] && error "Change POSTGRES_PASSWORD from the placeholder value"
[[ ${#PGPASS} -lt 16 ]] && warn "POSTGRES_PASSWORD is short — use at least 16 characters for production"

# ── 2. Install Docker if missing ──────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    usermod -aG docker "$USER"
    info "Docker installed. You may need to log out and back in for group changes."
fi

if ! docker compose version &>/dev/null; then
    info "Installing Docker Compose plugin..."
    apt-get install -y docker-compose-plugin 2>/dev/null || \
    pip install docker-compose 2>/dev/null || \
    error "Could not install Docker Compose. Install manually."
fi

# ── 3. Pull and build ─────────────────────────────────────────────────────────
info "Building containers..."
docker compose build --no-cache

# ── 4. Start infrastructure ───────────────────────────────────────────────────
info "Starting Postgres and Redis..."
docker compose up -d postgres redis
info "Waiting for Postgres to be healthy..."
timeout 60 bash -c 'until docker compose exec postgres pg_isready -U sip -d sip_db; do sleep 2; done'
info "Postgres is ready."

# ── 5. Run DB migrations ──────────────────────────────────────────────────────
info "Running database migrations..."
docker compose run --rm api python -m alembic upgrade head || {
    warn "Alembic migration failed — attempting to create tables directly..."
    docker compose run --rm api python -c "from src.db.session import init_db; init_db(); print('Tables created.')"
}

# ── 6. Start all services ─────────────────────────────────────────────────────
info "Starting all services..."
docker compose up -d

# ── 7. Verify health ──────────────────────────────────────────────────────────
info "Waiting 20s for services to stabilise..."
sleep 20

info "Checking service status..."
docker compose ps

API_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null || echo "000")
if [[ "$API_STATUS" == "200" ]]; then
    info "✓ API is healthy (HTTP 200)"
else
    warn "API health check returned HTTP $API_STATUS — check logs with: docker compose logs api"
fi

WORKER_OK=$(docker compose exec celery-worker celery -A src.workers.celery_app inspect ping 2>/dev/null | grep -c "OK" || echo "0")
if [[ "$WORKER_OK" -gt 0 ]]; then
    info "✓ Celery worker is responding"
else
    warn "Celery worker not responding — check: docker compose logs celery-worker"
fi

# ── 8. Set up log rotation ────────────────────────────────────────────────────
cat > /etc/logrotate.d/sip-docker 2>/dev/null <<'LOGROTATE'
/var/lib/docker/containers/*/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
LOGROTATE
info "Log rotation configured."

# ── 9. Set up daily backup cron ───────────────────────────────────────────────
CRON_JOB="0 2 * * * cd $(pwd) && docker compose --profile backup run --rm db-backup >> /var/log/sip-backup.log 2>&1"
(crontab -l 2>/dev/null | grep -v "db-backup"; echo "$CRON_JOB") | crontab -
info "Daily backup cron job installed (runs at 2 AM UTC)."

# ── 10. Summary ───────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Sports Intelligence Platform — Deployment Complete ${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo ""
echo "  API:          http://localhost:8000"
echo "  API docs:     http://localhost:8000/docs"
echo "  Health:       http://localhost:8000/health"
echo ""
echo "  Useful commands:"
echo "    docker compose logs -f              # stream all logs"
echo "    docker compose logs -f celery-beat  # watch scheduled tasks"
echo "    docker compose logs -f celery-worker # watch Discord alerts (sent via alert_worker)"
echo "    docker compose restart              # restart all services"
echo "    docker compose down && docker compose up -d  # full restart"
echo "    docker compose --profile backup run --rm db-backup  # manual backup"
echo ""
echo "  To check picks being generated:"
echo "    docker compose exec celery-worker celery -A src.workers.celery_app inspect active"
echo ""
warn "If this is your first run, wait ~10 minutes for the first odds scan and pick generation."
warn "Picks will appear in your Discord channels automatically."
echo ""
