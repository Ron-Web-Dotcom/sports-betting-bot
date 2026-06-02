"""Tests for health_engine."""
import pytest
from unittest.mock import patch, MagicMock
from src.engines.health_engine import (
    ServiceStatus, SystemHealth, check_database, check_redis,
    check_discord, get_system_health, format_health_report,
)
from datetime import datetime


def test_check_database_ok():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.limit.return_value.all.return_value = []
    with patch("src.db.session.get_db", return_value=ms):
        result = check_database()
    assert result.status == "ok"
    assert result.name == "database"
    assert result.latency_ms >= 0


def test_check_database_down_on_error():
    with patch("src.db.session.get_db", side_effect=Exception("DB failed")):
        result = check_database()
    assert result.status == "down"
    assert "DB failed" in result.error


def test_check_redis_ok():
    mock_r = MagicMock()
    mock_r.ping.return_value = True
    with patch("redis.from_url", return_value=mock_r):
        result = check_redis()
    assert result.status == "ok"
    assert result.name == "redis"


def test_check_redis_down_on_error():
    with patch("redis.from_url", side_effect=Exception("Redis refused")):
        result = check_redis()
    assert result.status == "down"


def test_check_discord_degraded_when_not_set():
    with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": ""}):
        result = check_discord()
    assert result.status == "degraded"


def test_get_system_health_returns_systemhealth():
    with patch("src.engines.health_engine.check_database",
               return_value=ServiceStatus("database", "ok", 5.0, datetime.utcnow())), \
         patch("src.engines.health_engine.check_redis",
               return_value=ServiceStatus("redis", "ok", 2.0, datetime.utcnow())), \
         patch("src.engines.health_engine.check_odds_api",
               return_value=ServiceStatus("odds_api", "ok", 100.0, datetime.utcnow())), \
         patch("src.engines.health_engine.check_discord",
               return_value=ServiceStatus("discord", "ok", 50.0, datetime.utcnow())), \
         patch("src.engines.health_engine.check_celery_workers",
               return_value=ServiceStatus("celery", "ok", 10.0, datetime.utcnow())):
        health = get_system_health()
    assert isinstance(health, SystemHealth)
    assert health.overall == "ok"
    assert len(health.services) == 5


def test_get_system_health_down_if_any_down():
    with patch("src.engines.health_engine.check_database",
               return_value=ServiceStatus("database", "down", 5.0, datetime.utcnow(), "fail")), \
         patch("src.engines.health_engine.check_redis",
               return_value=ServiceStatus("redis", "ok", 2.0, datetime.utcnow())), \
         patch("src.engines.health_engine.check_odds_api",
               return_value=ServiceStatus("odds_api", "ok", 100.0, datetime.utcnow())), \
         patch("src.engines.health_engine.check_discord",
               return_value=ServiceStatus("discord", "ok", 50.0, datetime.utcnow())), \
         patch("src.engines.health_engine.check_celery_workers",
               return_value=ServiceStatus("celery", "ok", 10.0, datetime.utcnow())):
        health = get_system_health()
    assert health.overall == "down"


def test_get_system_health_degraded():
    with patch("src.engines.health_engine.check_database",
               return_value=ServiceStatus("database", "ok", 5.0, datetime.utcnow())), \
         patch("src.engines.health_engine.check_redis",
               return_value=ServiceStatus("redis", "degraded", 2.0, datetime.utcnow(), "warn")), \
         patch("src.engines.health_engine.check_odds_api",
               return_value=ServiceStatus("odds_api", "ok", 100.0, datetime.utcnow())), \
         patch("src.engines.health_engine.check_discord",
               return_value=ServiceStatus("discord", "ok", 50.0, datetime.utcnow())), \
         patch("src.engines.health_engine.check_celery_workers",
               return_value=ServiceStatus("celery", "ok", 10.0, datetime.utcnow())):
        health = get_system_health()
    assert health.overall == "degraded"


def test_format_health_report_contains_services():
    health = SystemHealth(
        overall="ok",
        services=[
            ServiceStatus("database", "ok", 5.0, datetime.utcnow()),
            ServiceStatus("redis", "down", 2.0, datetime.utcnow(), "refused"),
        ],
    )
    report = format_health_report(health)
    assert "database" in report
    assert "redis" in report
    assert "refused" in report
    assert "OK" in report.upper() or "ok" in report


def test_format_health_report_shows_latency():
    health = SystemHealth(
        overall="ok",
        services=[ServiceStatus("database", "ok", 12.34, datetime.utcnow())],
    )
    report = format_health_report(health)
    assert "12" in report
