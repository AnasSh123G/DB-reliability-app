"""Engine, session factory and the declarative base."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from dbrel.config import DB_PATH
from dbrel import config

_BULK_LOAD = False


class Base(DeclarativeBase):
    pass


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """WAL + sane durability. Applied to every new connection.

    WAL is what lets a poll write while a query reads. synchronous=NORMAL is the
    right trade with WAL: a crash can lose the last commit, not corrupt the file.
    """
    cursor = dbapi_connection.cursor()
    for pragma in (
        "journal_mode=WAL",
        "synchronous=NORMAL",
        "busy_timeout=10000",
        "foreign_keys=ON",
        "temp_store=MEMORY",
        "cache_size=-131072",  # 128 MB
        "mmap_size=268435456",  # 256 MB
    ):
        cursor.execute(f"PRAGMA {pragma}")
    if _BULK_LOAD:
        cursor.execute(f"PRAGMA cache_size={-config.BACKFILL_CACHE_MIB * 1024}")
        cursor.execute(f"PRAGMA mmap_size={config.BACKFILL_MMAP_MIB * 1024 * 1024}")
        cursor.execute("PRAGMA wal_autocheckpoint=20000")
    cursor.close()


def make_engine(path: Path | None = None, echo: bool = False) -> Engine:
    db_path = Path(path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite+pysqlite:///{db_path}", echo=echo, future=True)


_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def dispose_engine() -> None:
    """Close the pool and forget the module-level singletons.

    A later get_engine()/get_sessionmaker() call rebuilds them lazily. Needed
    before deleting the SQLite file out from under a live process (reset-db):
    without this, a pooled connection can keep the file open and the delete
    either fails or leaves the reader confused about what it is reading.
    """
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None


def enable_bulk_load(enabled: bool = True) -> bool:
    """Select bounded backfill tuning for new connections; return the prior mode."""
    global _BULK_LOAD
    if enabled and (config.BACKFILL_CACHE_MIB <= 0 or config.BACKFILL_MMAP_MIB < 0):
        raise ValueError("backfill cache must be positive and mmap must be non-negative")
    previous = _BULK_LOAD
    _BULK_LOAD = enabled
    dispose_engine()
    return previous


def get_sessionmaker() -> sessionmaker[Session]:
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=get_engine(), future=True)
    return _Session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commit on success, roll back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def read_scope() -> Iterator[Session]:
    """Read-only session: never commits, always rolls back. The web path.

    session_scope commits on exit, which for a GET is both a pointless round-trip
    and a standing invitation to write from a request — tt_feed_version() inserts
    a row when none exists, and would happily do so while rendering a page. Making
    the web use a scope that cannot commit turns "the server never writes" from a
    convention into a property. Closing per request is also what stops a long read
    transaction from holding back WAL checkpointing.
    """
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def create_all() -> None:
    import dbrel.models  # noqa: F401  (registers every mapper)
    from dbrel.views import create_day_views

    engine = get_engine()
    Base.metadata.create_all(engine)
    # After the tables, because the views select from them.
    create_day_views(engine)
