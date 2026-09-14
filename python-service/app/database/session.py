"""Engine and session management.

Synchronous SQLAlchemy on purpose: FastAPI runs ``def`` endpoints in a
threadpool, ccxt is synchronous, and financial bookkeeping is far easier to
reason about without interleaved awaits. Throughput is not the constraint here —
one workflow tick per symbol per timeframe is a trivial load.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _create_engine(settings: Settings) -> Engine:
    url = settings.database_url
    kwargs: dict[str, object] = {"echo": settings.db_echo, "future": True}
    if url.startswith("sqlite"):
        # In-memory SQLite needs one shared connection or each session sees an
        # empty database.
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url.endswith("sqlite://"):
            kwargs["poolclass"] = StaticPool
    else:
        kwargs.update(
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_recycle=1800,
        )
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - setup
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is None:
        _engine = _create_engine(settings or get_settings())
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(settings),
            # autoflush ON: a query must see writes made earlier in the same
            # request. With it off, cancelling an order and then listing open
            # orders returns the cancelled one, and the monitor can act on stale
            # position state. Read-after-write consistency matters more here than
            # avoiding a mid-transaction flush.
            autoflush=True,
            autocommit=False,
            expire_on_commit=False,
        )
    return _session_factory


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    factory = get_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def reset_engine() -> None:
    """Drop cached engine/session factory (tests, or after a config change)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def configure_engine(engine: Engine) -> None:
    """Install a pre-built engine (used by tests to inject SQLite)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = sessionmaker(
        bind=engine, autoflush=True, autocommit=False, expire_on_commit=False
    )


def check_database(settings: Settings | None = None) -> tuple[bool, str]:
    try:
        with get_engine(settings).connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:
        return False, str(exc)
