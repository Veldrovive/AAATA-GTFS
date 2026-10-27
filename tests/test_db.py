import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from aaata_gtfs import db
from aaata_gtfs.migrations import legacy
from aaata_gtfs.models import CollectorRun


def check_matches_models(engine: Engine) -> None:
    """Alembic's autogenerate finds nothing to change between the schema and the models."""
    with engine.begin() as conn:
        cfg = db.alembic_config()
        cfg.attributes["connection"] = conn
        command.check(cfg)


def legacy_db(path: Path, version: int) -> None:
    """A database as the collector made it before Alembic, at a `user_version`, with some rows."""
    conn = sqlite3.connect(path)
    for number, script in enumerate(legacy.MIGRATIONS[:version], start=1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    conn.execute("INSERT INTO collector_run (started_at, version, args) VALUES (1, 'old', '{}')")
    conn.execute("INSERT INTO feed_snapshot VALUES (1, 'trips', 100, 101.0, 'x', 0, 0, NULL, 1)")
    conn.execute("INSERT INTO trip_update (trip_id, n_stop_time_updates, snapshot_id, last_seen_snapshot_id) "
                 "VALUES ('t', 0, 1, 1)")
    if version == 1:
        conn.execute("INSERT INTO stop_time_prediction (trip_update_id, trip_id, stop_sequence, stop_id, "
                     "observed_ts, snapshot_id) VALUES (1, 't', 1, 's', 100, 1)")
    conn.commit()
    conn.close()


def names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()


def test_new_database(tmp_path: Path) -> None:
    path = tmp_path / "x.sqlite3"
    session = db.open_db(path)
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    assert db.current_revision(engine) == db.head_revision()
    assert db.migrate(engine) == db.head_revision()  # idempotent
    assert session.connection().exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    check_matches_models(engine)
    session.close()

    ro = db.connect(path, read_only=True)
    ro.add(CollectorRun(started_at=1, version="x", args="{}"))
    with pytest.raises(OperationalError):
        ro.commit()


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_database_is_kept(tmp_path: Path, version: int) -> None:
    path = tmp_path / "x.sqlite3"
    legacy_db(path, version)
    assert ("stop_time_prediction" in names(path)) == (version == 1)
    engine = db.make_engine(path)
    assert db.migrate(engine) == db.head_revision()
    assert db.migrate(engine) == db.head_revision()
    check_matches_models(engine)
    assert not {n for n in names(path) if n.startswith("stop_time_prediction")}
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone() == (len(legacy.MIGRATIONS),)
    assert conn.execute("SELECT version FROM collector_run").fetchall() == [("old",)]
    assert conn.execute("SELECT trip_id, snapshot_id FROM trip_update").fetchall() == [("t", 1)]
    conn.close()


def test_schema_identical_either_way(tmp_path: Path) -> None:
    """A new database and a migrated old one have the same schema, statement for statement."""
    new, old = tmp_path / "new.sqlite3", tmp_path / "old.sqlite3"
    db.migrate(db.make_engine(new))
    legacy_db(old, 1)
    db.migrate(db.make_engine(old))

    def schema(path: Path) -> list[tuple[str, str]]:
        conn = sqlite3.connect(path)
        try:
            return sorted(conn.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
        finally:
            conn.close()

    assert schema(new) == schema(old)
