"""Engine, session factory and the declarative base."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from dbrel.config import DB_PATH


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

    Base.metadata.create_all(get_engine())
