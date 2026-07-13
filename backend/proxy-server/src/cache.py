"""Redis connection — used for rate limiting and caching."""

import redis.asyncio as aioredis
import structlog

from src.config import settings

log = structlog.get_logger()
_redis = None


async def init_redis():
    global _redis
    _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    await _redis.ping()
    log.info("redis.connected", url=settings.REDIS_URL)


async def close_redis():
    if _redis:
        await _redis.close()
    log.info("redis.disconnected")


async def get_redis():
    return _redis