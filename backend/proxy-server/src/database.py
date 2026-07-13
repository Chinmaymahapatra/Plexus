"""
Async PostgreSQL connection pool via SQLAlchemy + asyncpg.
All models are defined here so Alembic can find them for migrations.
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
import structlog

from src.config import settings

log = structlog.get_logger()

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,   # detect stale connections
    echo=settings.is_dev, # log SQL in dev
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    log.info("db.connecting", url=settings.DATABASE_URL.split("@")[-1])
    # Tables are created via Alembic migrations, not here.
    # This just verifies the connection pool is healthy.
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: c.execute(c.dialect.statement_compiler(c, None)))
    log.info("db.connected")


async def close_db():
    await engine.dispose()
    log.info("db.disconnected")