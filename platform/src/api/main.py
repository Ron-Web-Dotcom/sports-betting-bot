"""
FastAPI application — Sports Intelligence Platform.

REST API for the web dashboard and external integrations.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.api.routers import picks, analytics, portfolio, discussion, health

app = FastAPI(
    title="Sports Intelligence Platform",
    description="Commercial-grade sports betting intelligence API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router,      prefix="/health",     tags=["health"])
app.include_router(picks.router,       prefix="/picks",      tags=["picks"])
app.include_router(analytics.router,   prefix="/analytics",  tags=["analytics"])
app.include_router(portfolio.router,   prefix="/portfolio",  tags=["portfolio"])
app.include_router(discussion.router,  prefix="/discuss",    tags=["discussion"])


@app.on_event("startup")
async def startup():
    from src.db.session import init_db
    init_db()
