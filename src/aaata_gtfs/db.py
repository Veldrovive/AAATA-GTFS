"""The SQLite database: engines, sessions and migrations.

The schema is the SQLModel tables in `models.py`, migrated with Alembic
(`aaata_gtfs/migrations/`). `migrate` brings any database to the latest
revision, including ones from before Alembic (schema version in `PRAGMA
user_version`): those get the remaining legacy scripts, then are stamped at the
baseline revision, so nothing in them is rebuilt or lost.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Unpack

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, Executable, Result, create_engine, event, inspect
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool
from sqlmodel import Session

from aaata_gtfs.migrations import legacy
from aaata_gtfs.models import TABLES as _MODELS

MIGRATIONS_PATH = Path(__file__).parent / "migrations"
#: The Alembic revision whose schema is the last legacy (user_version) one.
BASELINE_REVISION = "0001"

#: Table names in the order they're reported (e.g. by collector_status.py).
TABLES = [str(m.__tablename__) for m in _MODELS]


def make_engine(path: Path | str, read_only: bool = False) -> Engine:
    """An engine with the standard pragmas (WAL, `synchronous=NORMAL`, foreign
    keys, a busy timeout).

    Read-only engines (`file:...?mode=ro`) are for scripts that run while the
    collector writes. `":memory:"` gives a private in-memory database.
    """
    memory = str(path) == ":memory:"
    if read_only:
        uri = f"file:{Path(path).resolve()}?mode=ro"
        engine = create_engine(
            "sqlite://", creator=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False)
        )
    elif memory:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    else:
        # The static loader writes from a worker thread; writes are serialized by the collector.
        engine = create_engine(f"sqlite:///{Path(path)}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        if not read_only and not memory:
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    return engine


def connect(path: Path | str, read_only: bool = False) -> Session:
    """A session on the database at `path` (see `make_engine`), not migrated."""
    return Session(make_engine(path, read_only))


def open_db(path: Path | str) -> Session:
    """A read-write session with the schema migrated to the latest revision."""
    engine = make_engine(path)
    migrate(engine)
    return Session(engine)


@contextmanager
def write(session: Session) -> Iterator[Session]:
    """One transaction: commit on success, roll back on an exception."""
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise


def rows(session: Session, statement: Executable) -> Result[Unpack[tuple[Any, ...]]]:
    """Run a SQLAlchemy `select` in the session and return its rows.

    Queries are built on `models.columns(...)` with SQLAlchemy's `select`, which
    is typed for any number of columns; SQLModel's `select` and `Session.exec`
    only for up to four. This is the ORM session's own `execute` (autoflush, the
    session's transaction), which SQLModel's subclass marks as discouraged.
    """
    return OrmSession.execute(session, statement)


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_PATH))
    cfg.set_main_option("path_separator", "os")
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _legacy_version(engine: Engine) -> int | None:
    """The `user_version` of a database from before Alembic, else None."""
    with engine.connect() as conn:
        if "alembic_version" in inspect(conn).get_table_names():
            return None
        version = int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)
    return version or None


def _finish_legacy(engine: Engine, version: int) -> None:
    """Apply the legacy scripts past `version` (each whole or not at all), VACUUM
    if one of them freed a lot of space, and stamp the baseline revision."""
    raw = engine.raw_connection()
    try:
        dbapi = raw.driver_connection
        assert isinstance(dbapi, sqlite3.Connection)
        for number, script in enumerate(legacy.MIGRATIONS[version:], start=version + 1):
            # executescript commits first; wrap each one so it applies whole or not at all.
            dbapi.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
        if any(n > version for n in legacy.VACUUM_AFTER):
            dbapi.execute("VACUUM")
    finally:
        raw.close()
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.stamp(cfg, BASELINE_REVISION)


def migrate(engine: Engine) -> str:
    """Bring the database to the latest revision. Returns it."""
    version = _legacy_version(engine)
    if version is not None:
        _finish_legacy(engine, version)
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    revision = current_revision(engine)
    assert revision is not None
    return revision
