"""
FastAPI application — Sports Intelligence Platform.

REST API for the web dashboard and external integrations.
"""
import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from src.api.routers import picks, analytics, portfolio, discussion, health, personalization, feedback

app = FastAPI(
    title="Sports Intelligence Platform",
    description="Commercial-grade sports betting intelligence API",
    version="1.0.0",
)

# allow_origins=["*"] and allow_credentials=True is forbidden by the CORS spec
# (browsers will reject such responses). Restrict origins to explicitly configured
# hosts; fall back to localhost-only in development.
_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

_API_KEY = os.getenv("PLATFORM_API_KEY", "")
_PUBLIC_PATHS = {"/health", "/health/"}


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Reject all non-health requests that don't carry the correct API key.

    Key is read from X-API-Key header or ?api_key= query param.
    When PLATFORM_API_KEY is not set the check is skipped (dev/test mode only).
    """
    if not _API_KEY or request.url.path in _PUBLIC_PATHS or request.method == "OPTIONS":
        return await call_next(request)

    key = request.headers.get("X-API-Key") or request.query_params.get("api_key", "")
    if key != _API_KEY:
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})

    return await call_next(request)


app.include_router(health.router,      prefix="/health",     tags=["health"])
app.include_router(picks.router,       prefix="/picks",      tags=["picks"])
app.include_router(analytics.router,   prefix="/analytics",  tags=["analytics"])
app.include_router(portfolio.router,   prefix="/portfolio",  tags=["portfolio"])
app.include_router(discussion.router,  prefix="/discuss",    tags=["discussion"])
app.include_router(personalization.router, prefix="/profile", tags=["personalization"])
app.include_router(feedback.router,    prefix="/feedback",   tags=["feedback"])


@app.on_event("startup")
async def startup():
    from src.db.session import init_db
    init_db()
