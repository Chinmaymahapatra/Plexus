"""
Rate Limit Middleware — sliding window counter in Redis.

Limits:
  - Unauthenticated: 10 req/min per IP (basic abuse prevention)
  - Authenticated:   60 req/min per developer (soft limit)
  - /v1/call:        30 req/min per developer (proxy calls cost money)

All limits configurable. Redis key format:
  rl:<ip>:<window_start_minute>
  rl:<developer_id>:<window_start_minute>
"""

import time
from typing import Optional

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.cache import get_redis

log = structlog.get_logger()

WINDOW_SECONDS = 60  # 1-minute sliding window

LIMITS = {
    "unauthenticated": 10,
    "authenticated":   60,
    "proxy_call":      30,   # /v1/call endpoint specifically
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        redis = await get_redis()

        # Determine which limit to apply
        developer = getattr(request.state, "developer", None)
        is_proxy_call = request.url.path.startswith("/v1/call")

        if developer:
            key_id = str(developer.id)
            limit = LIMITS["proxy_call"] if is_proxy_call else LIMITS["authenticated"]
        else:
            key_id = request.client.host if request.client else "unknown"
            limit = LIMITS["unauthenticated"]

        window = int(time.time()) // WINDOW_SECONDS
        redis_key = f"rl:{key_id}:{window}"

        try:
            count = await redis.incr(redis_key)
            if count == 1:
                await redis.expire(redis_key, WINDOW_SECONDS * 2)  # 2x window for safety

            if count > limit:
                log.warning(
                    "rate_limit.exceeded",
                    key_id=key_id,
                    count=count,
                    limit=limit,
                    path=request.url.path,
                )
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "Rate limit exceeded",
                        "limit": limit,
                        "window": "60 seconds",
                        "retry_after": WINDOW_SECONDS - (int(time.time()) % WINDOW_SECONDS),
                    },
                    headers={"Retry-After": str(WINDOW_SECONDS)},
                )
        except Exception as e:
            # Redis down → log and allow through (fail open for rate limiting)
            log.error("rate_limit.redis_error", error=str(e))

        response = await call_next(request)

        # Attach rate limit headers to response
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - count))
        return response