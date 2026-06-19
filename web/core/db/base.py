# core/db/base.py
# Praesidium Series 2.0
# PostgreSQL 16 via asyncpg — replaces MariaDB 11 / aiomysql
# Application always connects through PgBouncer on port 6432, not PostgreSQL directly.
# DATABASE_URL must use dialect: postgresql+asyncpg://

import os
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

# ── Database URL ─────────────────────────────────────────────────────────────
# Must be postgresql+asyncpg:// — never mysql+aiomysql://
# In production this points at PgBouncer (port 6432), not PostgreSQL directly.
# Example: postgresql+asyncpg://praesidium_db:password@10.10.60.11:6432/praesidium_hjmm-prod
DATABASE_URL: str = os.environ["DATABASE_URL"]

if "mysql" in DATABASE_URL or "aiomysql" in DATABASE_URL or "pymysql" in DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL contains a MySQL/MariaDB dialect. "
        "Series 2.0 requires postgresql+asyncpg://. "
        "Update your .env file."
    )

if not DATABASE_URL.startswith("postgresql+asyncpg://"):
    # Allow plain postgresql:// by upgrading it — but warn loudly.
    if DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
        logger.warning(
            "DATABASE_URL upgraded from postgresql:// to postgresql+asyncpg://. "
            "Set the correct dialect in .env to suppress this warning."
        )
    else:
        raise RuntimeError(
            f"DATABASE_URL must start with postgresql+asyncpg://. Got: {DATABASE_URL[:40]}..."
        )

# ── Engine ───────────────────────────────────────────────────────────────────
# NullPool is required when connecting via PgBouncer in transaction-pooling mode.
# PgBouncer manages the server-side pool; SQLAlchemy must not pool on top of it.
engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    poolclass=NullPool,          # Required for PgBouncer transaction-mode
    echo=os.environ.get("DB_ECHO", "false").lower() == "true",
    future=True,
)

# ── Session factory ──────────────────────────────────────────────────────────
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

# ── Co-counsel projection: per-transaction schema overlay ─────────────
# A co-counsel request resolves to the firm's shared matter through the
# `cocounsel` schema of re-stamping views. We flip the schema with
# `SET LOCAL search_path` at transaction start — SET LOCAL is scoped to the
# transaction, so it is SAFE under PgBouncer transaction pooling (resets at
# COMMIT/ROLLBACK, never leaks to the next pooled client). The contextvar is set
# per-request by AuthMiddleware for co_counsel users only; otherwise it is None
# and this hook is a no-op (firm paths byte-identical).
import contextvars
from sqlalchemy import event as _sa_event

_CC_SEARCH_PATHS = {"cocounsel, public", "client_portal, public"}
cc_search_path = contextvars.ContextVar("cc_search_path", default=None)
# The co-counsel user id — the cocounsel views project ONLY this user's
# subscribed matters (external_user_scopes), so isolation is per-user and does
# not depend on any query applying the Chinese wall.
cc_user_id = contextvars.ContextVar("cc_user_id", default=None)


@_sa_event.listens_for(engine.sync_engine, "begin")
def _apply_cc_search_path(conn):
    sp = cc_search_path.get()
    if sp and sp in _CC_SEARCH_PATHS:
        conn.exec_driver_sql(f"SET LOCAL search_path = {sp}")
        u = cc_user_id.get()
        if u is not None:
            conn.exec_driver_sql(f"SET LOCAL app.cc_user = {int(u)}")


# ── Base declarative class ───────────────────────────────────────────────────
class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


# ── Dependency ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager for a database session.
    Use TenantSession (core/db/tenant.py) in application code — not this directly.
    This is the low-level primitive used by TenantSession.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an AsyncSession.
    Use require_tenant_session() in application endpoints instead.
    """
    async with get_db_session() as session:
        yield session


# ── Compatibility exports expected by all application modules ─────────────────

class TenantSession:
    """Async context manager that yields an AsyncSession scoped to a tenant.
    Supports both new-style TenantSession(tenant_id) and
    old-style TenantSession(session, tenant_id) for backward compatibility.
    Usage:
        async with TenantSession(tenant_id) as session:
            result = await session.execute(...)
    """
    def __init__(self, tenant_id_or_session, tenant_id: str = None):
        if tenant_id is not None:
            # Old-style: TenantSession(session, tenant_id)
            self.tenant_id = tenant_id
            self._sync_session = tenant_id_or_session
        else:
            # New-style: TenantSession(tenant_id)
            self.tenant_id = tenant_id_or_session
            self._sync_session = None
        self._session: AsyncSession | None = None
        self._ctx = None

    def query(self, *args, **kwargs):
        """Backward-compatible sync query method."""
        if self._sync_session:
            return self._sync_session.query(*args, **kwargs)
        raise RuntimeError("query() requires a sync session — use async with TenantSession(tenant_id) instead")

    def add(self, obj):
        if self._sync_session:
            return self._sync_session.add(obj)

    def flush(self):
        if self._sync_session:
            return self._sync_session.flush()

    def commit(self):
        if self._sync_session:
            return self._sync_session.commit()

    def rollback(self):
        if self._sync_session:
            return self._sync_session.rollback()

    def close(self):
        if self._sync_session:
            return self._sync_session.close()

    async def __aenter__(self) -> AsyncSession:
        self._ctx = get_db_session()
        self._session = await self._ctx.__aenter__()
        self._session.info["tenant_id"] = self.tenant_id
        return self._session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self._ctx.__aexit__(exc_type, exc_val, exc_tb)


def get_session_factory():
    """Return the async session factory. Used by modules that manage
    their own session lifecycle."""
    return AsyncSessionLocal


async def get_tenant_session(tenant_id: str) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a TenantSession for the given tenant_id."""
    async with TenantSession(tenant_id) as session:
        yield session


def get_engine() -> AsyncEngine:
    """Return the async engine instance."""
    return engine


async def init_db(*args, **kwargs) -> None:
    """Initialize the database — creates tables if they do not exist.
    In production, schema is managed by Alembic migrations.
    This is a no-op safety check only."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── Sync engine for backward-compatible modules ───────────────────────────────
# The old codebase uses get_session_factory()() to get a sync session.
# We provide a sync session factory here so those modules keep working
# without requiring a full async rewrite of 42+ files.
from sqlalchemy import create_engine as _create_sync_engine
from sqlalchemy.orm import sessionmaker as _sessionmaker, Session as _SyncSession

_SYNC_DATABASE_URL = DATABASE_URL.replace(
    "postgresql+asyncpg://", "postgresql+psycopg2://"
)

_sync_engine = _create_sync_engine(
    _SYNC_DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

_SyncSessionLocal = _sessionmaker(
    bind=_sync_engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_session_factory():
    """Return sync session factory for backward-compatible modules."""
    return _SyncSessionLocal


def get_sync_session() -> _SyncSession:
    """Get a sync session directly."""
    return _SyncSessionLocal()
