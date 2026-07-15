"""
Auth Middleware — validates Plexus SDK tokens on every proxied request.

Token format: plx_live_<32-char-hex>  or  plx_test_<32-char-hex>
We store a SHA-256 hash of the token in the DB, never the raw value.

Public routes (no token needed):
  - POST /v1/auth/register
  - POST /v1/auth/login
  - GET  /health
  - POST /v1/webhooks/stripe   (validated by Stripe signature instead)
"""

import hashlib
from typing import Optional, Tuple

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from src.database import AsyncSessionLocal
from src.models import SDKToken, Developer

log = structlog.get_logger()

PUBLIC_PATHS = {
    "/health",
    "/v1/auth/register",
    "/v1/auth/login",
    "/v1/webhooks/stripe",
    "/docs",
    "/openapi.json",
}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip auth for public paths
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        token = self._extract_token(request)
        if not token:
            return JSONResponse(
                status_code=401,
                content={"error": "Missing Authorization header. Include: Bearer plx_live_<token>"},
            )

        # CHANGED: _validate_token now returns a (developer, sdk_token) tuple
        # instead of just the developer, so we have the real SDKToken row
        # available to attach to request.state below.
        developer, sdk_token = await self._validate_token(token)
        if not developer:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid or expired SDK token"},
            )

        # Attach developer to request state for downstream use
        request.state.developer = developer
        request.state.sdk_token = sdk_token  # CHANGED: added — this is the real SDKToken DB row
        request.state.token_raw = token

        return await call_next(request)

    def _extract_token(self, request: Request) -> Optional[str]:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]
        return None

    # CHANGED: return type is now Tuple[Optional[Developer], Optional[SDKToken]]
    # instead of Optional[Developer]. Both failure paths now return (None, None)
    # instead of just None, so the caller can always safely unpack two values.
    async def _validate_token(self, token: str) -> Tuple[Optional[Developer], Optional[SDKToken]]:
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SDKToken)
                .where(SDKToken.token_hash == token_hash)
                .where(SDKToken.is_active == True)
            )
            sdk_token = result.scalar_one_or_none()

            if not sdk_token:
                return None, None  # CHANGED: was `return None`

            # Load the developer
            developer = await db.get(Developer, sdk_token.developer_id)
            if not developer or not developer.is_active:
                return None, None  # CHANGED: was `return None`

            return developer, sdk_token  # CHANGED: was `return developer`