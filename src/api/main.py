"""
FastAPI application — Sports Intelligence Platform.

REST API for the web dashboard and external integrations.
"""
import logging
import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from src.api.routers import picks, analytics, portfolio, discussion, health, personalization, feedback

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Sports Intelligence Platform",
    description="Commercial-grade sports betting intelligence API",
    version="1.0.0",
)

# ── CORS ───────────────────────────────────────────────────────────────────────
# allow_origins=["*"] + allow_credentials=True is forbidden by the CORS spec.
# Restrict to explicitly configured origins; default to localhost only.
_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-API-Key"],
)

# ── API key auth ───────────────────────────────────────────────────────────────
_API_KEY = os.getenv("PLATFORM_API_KEY", "")
_PUBLIC_PATHS = {"/health", "/health/"}

# Rate-limit counters (in-memory; Redis-backed in production would be ideal)
_rate_cache: dict = {}


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """All non-health endpoints require X-API-Key header or ?api_key= query param.
    PLATFORM_API_KEY not set → check skipped (dev/test only).
    """
    if request.url.path in _PUBLIC_PATHS or request.method == "OPTIONS":
        return await call_next(request)
    # If PLATFORM_API_KEY is not configured, block all non-public routes in production
    if not _API_KEY:
        from src.core.config import ENVIRONMENT
        if ENVIRONMENT == "production":
            return JSONResponse(status_code=503, content={"detail": "API key not configured"})
        return await call_next(request)  # dev/test: allow through with warning

    key = request.headers.get("X-API-Key") or request.query_params.get("api_key", "")
    if not key or key != _API_KEY:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    return await call_next(request)


@app.middleware("http")
async def rate_limit_discuss(request: Request, call_next):
    """Simple per-IP rate limit on /discuss: 10 req/min to protect Claude API spend."""
    import time
    if request.url.path.startswith("/discuss") and request.method == "POST":
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window_start, count = _rate_cache.get(ip, (now, 0))
        if now - window_start > 60:
            window_start, count = now, 0
        count += 1
        _rate_cache[ip] = (window_start, count)
        if count > 10:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded — max 10 /discuss requests per minute"},
            )
        # Prune stale entries every 1000 requests to prevent unbounded memory growth
        if len(_rate_cache) > 1000:
            stale = [k for k, (ws, _) in _rate_cache.items() if now - ws > 120]
            for k in stale:
                _rate_cache.pop(k, None)
    return await call_next(request)


# ── Global error handler — never expose internals ──────────────────────────────
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(health.router,           prefix="/health",   tags=["health"])
app.include_router(picks.router,            prefix="/picks",    tags=["picks"])
app.include_router(analytics.router,        prefix="/analytics",tags=["analytics"])
app.include_router(portfolio.router,        prefix="/portfolio",tags=["portfolio"])
app.include_router(discussion.router,       prefix="/discuss",  tags=["discussion"])
app.include_router(personalization.router,  prefix="/profile",  tags=["personalization"])
app.include_router(feedback.router,         prefix="/feedback", tags=["feedback"])


@app.on_event("startup")
async def startup():
    from src.db.session import init_db
    init_db()
    if not _API_KEY:
        logger.warning(
            "PLATFORM_API_KEY is not set — all endpoints are publicly accessible! "
            "Set PLATFORM_API_KEY in your environment for production."
        )
