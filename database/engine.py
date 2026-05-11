"""
database/engine.py — async engine + session factory.
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
import sqlalchemy

from app_config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
)

AsyncSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def init_db() -> None:
    """Verify DB connectivity on startup (migrations handled by Alembic)."""
    async with engine.connect() as conn:
        await conn.execute(sqlalchemy.text("SELECT 1"))


async def get_session() -> AsyncSession:
    """Dependency / context-manager helper."""
    async with AsyncSessionFactory() as session:
        yield session
