from fastapi import APIRouter
from datetime import datetime

router = APIRouter()


@router.get("/")
def health_check():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@router.get("/system")
def system_health():
    from src.engines.health_engine import get_system_health
    health = get_system_health()
    return {
        "overall": health.overall,
        "timestamp": health.timestamp.isoformat(),
        "services": [
            {
                "name": s.name,
                "status": s.status,
                "latency_ms": s.latency_ms,
                "last_check": s.last_check.isoformat(),
                "error": s.error,
            }
            for s in health.services
        ],
    }
