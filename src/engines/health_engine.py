"""System health dashboard — check all services and report status."""
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class ServiceStatus:
    name: str
    status: Literal["ok", "degraded", "down"]
    latency_ms: float
    last_check: datetime
    error: str = ""


@dataclass
class SystemHealth:
    overall: Literal["ok", "degraded", "down"]
    services: list[ServiceStatus]
    timestamp: datetime = field(default_factory=datetime.utcnow)


def check_odds_api() -> ServiceStatus:
    import os, urllib.request
    name = "odds_api"
    t0 = time.monotonic()
    try:
        api_key = os.getenv("ODDS_API_KEY", "")
        url = f"https://api.the-odds-api.com/v4/sports/?apiKey={api_key}&all=false"
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "sports-intel/1.0")
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read(64)
        latency = (time.monotonic() - t0) * 1000
        return ServiceStatus(name=name, status="ok", latency_ms=latency, last_check=datetime.utcnow())
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        # Redact API key from error message to prevent secret leakage in logs
        api_key = os.getenv("ODDS_API_KEY", "")
        safe_err = str(e).replace(api_key, "***") if api_key else str(e)
        return ServiceStatus(name=name, status="down", latency_ms=latency, last_check=datetime.utcnow(), error=safe_err)


def check_redis() -> ServiceStatus:
    name = "redis"
    t0 = time.monotonic()
    try:
        import redis as redis_lib
        from src.core.config import REDIS_URL
        r = redis_lib.from_url(REDIS_URL, socket_connect_timeout=3)
        r.ping()
        latency = (time.monotonic() - t0) * 1000
        return ServiceStatus(name=name, status="ok", latency_ms=latency, last_check=datetime.utcnow())
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        return ServiceStatus(name=name, status="down", latency_ms=latency, last_check=datetime.utcnow(), error=str(e))


def check_database() -> ServiceStatus:
    name = "database"
    t0 = time.monotonic()
    try:
        from src.db.session import get_db
        from src.db.models import Pick
        with get_db() as db:
            db.query(Pick).limit(1).all()
        latency = (time.monotonic() - t0) * 1000
        return ServiceStatus(name=name, status="ok", latency_ms=latency, last_check=datetime.utcnow())
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        return ServiceStatus(name=name, status="down", latency_ms=latency, last_check=datetime.utcnow(), error=str(e))


def check_discord() -> ServiceStatus:
    import os
    import urllib.request, urllib.error
    name = "discord"
    t0 = time.monotonic()
    try:
        webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
        if not webhook:
            return ServiceStatus(
                name=name, status="ok", latency_ms=0,
                last_check=datetime.utcnow(), error="Not configured (optional)",
            )
        # Discord webhooks only accept POST — GET returns 405. Send an empty POST
        # with ?wait=false so Discord doesn't queue a message, just validates the URL.
        data = b"{}"
        req = urllib.request.Request(webhook + "?wait=false", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read(64)
        except urllib.error.HTTPError as http_err:
            # 400 = bad JSON body but webhook exists and is reachable → treat as ok
            if http_err.code in (400, 204):
                pass
            else:
                raise
        latency = (time.monotonic() - t0) * 1000
        return ServiceStatus(name=name, status="ok", latency_ms=latency, last_check=datetime.utcnow())
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        status = "degraded" if "not set" in str(e) else "down"
        return ServiceStatus(name=name, status=status, latency_ms=latency, last_check=datetime.utcnow(), error=str(e))


def check_celery_workers() -> ServiceStatus:
    name = "celery"
    t0 = time.monotonic()
    try:
        from src.workers.celery_app import app
        inspector = app.control.inspect(timeout=2)
        active = inspector.ping()
        latency = (time.monotonic() - t0) * 1000
        if active:
            return ServiceStatus(name=name, status="ok", latency_ms=latency, last_check=datetime.utcnow())
        return ServiceStatus(name=name, status="degraded", latency_ms=latency, last_check=datetime.utcnow(), error="No workers responded")
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        return ServiceStatus(name=name, status="down", latency_ms=latency, last_check=datetime.utcnow(), error=str(e))


def get_system_health() -> SystemHealth:
    services = [
        check_database(),
        check_redis(),
        check_odds_api(),
        check_discord(),
        check_celery_workers(),
    ]
    statuses = {s.status for s in services}
    if "down" in statuses:
        overall = "down"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "ok"
    return SystemHealth(overall=overall, services=services)


def format_health_report(health: SystemHealth) -> str:
    icons = {"ok": "✅", "degraded": "⚠️", "down": "❌"}
    lines = [f"**System Health: {icons[health.overall]} {health.overall.upper()}**"]
    lines.append(f"Checked at {health.timestamp.strftime('%H:%M:%S UTC')}")
    lines.append("")
    for svc in health.services:
        icon = icons[svc.status]
        line = f"{icon} **{svc.name}** — {svc.latency_ms:.0f}ms"
        if svc.error:
            line += f" — `{svc.error[:80]}`"
        lines.append(line)
    return "\n".join(lines)
