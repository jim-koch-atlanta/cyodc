"""Engine, session factory, and the declarative Base.

One engine/sessionmaker per process, built from `CYODC_DATABASE_URL`. SQLite
needs two things the deploy target (Postgres) gives for free: cross-thread
sharing (`check_same_thread=False`) and FK enforcement (a per-connection
`PRAGMA foreign_keys=ON`, wired below).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    url = get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


engine = _make_engine()
# autoflush=True (the default) matters here: within one turn the DM may call
# several tools, and a later tool must see an earlier tool's pending writes
# (e.g. take then use the same item). Flush != commit, so replay-safety holds.
SessionLocal = sessionmaker(
    bind=engine, autoflush=True, expire_on_commit=False, future=True
)


@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
    # SQLite ignores FK constraints (and ON DELETE CASCADE) unless asked, per
    # connection. No-op on Postgres, which enforces them natively.
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def init_db() -> None:
    """Create all tables if missing. M2 uses create_all; M7 switches to Alembic."""
    from app.db import models  # noqa: F401  (register mappers on Base.metadata)

    Base.metadata.create_all(engine)


@contextmanager
def get_db_session() -> Iterator[Session]:
    """Transactional session scope: commit on success, roll back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
