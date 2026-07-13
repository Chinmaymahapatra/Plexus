"""
Plexus Proxy Server — FastAPI entrypoint

Startup order:
  1. Config validation
  2. DB connection pool
  3. Redis connection
  4. Route registration
  5. Middleware stack
"""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings
from src.database import init_db, close_db
from src.cache import init_redis, close_redis
from src.routes import auth, wallet, proxy, registry, webhooks
from src.middleware.auth import AuthMiddleware
from src.middleware.rate_limit import RateLimitMiddleware

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of shared resources."""
    log.info("plexus.startup", env=settings.ENVIRONMENT)
    await init_db()
    await init_redis()
    yield
    await close_db()
    await close_redis()
    log.info("plexus.shutdown")


app = FastAPI(
    title="Plexus API",
    description="Agentic payments and API proxy infrastructure",
    version="0.1.0",
    lifespan=lifespan,
    # Hide docs in production
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url=None,
)

# ─── Middleware (applied in reverse order — bottom runs first) ─────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.ENVIRONMENT == "development" else ["https://app.plexus.dev"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware)

# ─── Routes ───────────────────────────────────────────────────────────────────
app.include_router(auth.router,     prefix="/v1/auth",     tags=["auth"])
app.include_router(wallet.router,   prefix="/v1/wallet",   tags=["wallet"])
app.include_router(proxy.router,    prefix="/v1/call",     tags=["proxy"])
app.include_router(registry.router, prefix="/v1/registry", tags=["registry"])
app.include_router(webhooks.router, prefix="/v1/webhooks", tags=["webhooks"])


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}