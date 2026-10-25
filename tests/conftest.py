"""Shared fixtures: real feed captures from 2026-10-25 ~22:12 UTC (see SPEC.md §10)."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from google.transit import gtfs_realtime_pb2 as pb

from aaata_gtfs import db
from aaata_gtfs.static_gtfs import StaticGtfs, load_static_file
from aaata_gtfs.store import Store, start_run

FIXTURES = Path(__file__).parent / "fixtures"
HEADER_TS = 1792966315
STATIC_ZIP = FIXTURES / "gtfs_static_4_22.zip"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_message(name: str) -> pb.FeedMessage:
    message = pb.FeedMessage()
    message.ParseFromString(fixture_bytes(name))
    return message


def with_header(message: pb.FeedMessage, header_ts: int) -> pb.FeedMessage:
    copy = pb.FeedMessage()
    copy.CopyFrom(message)
    copy.header.timestamp = header_ts
    return copy


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A fresh database (file-backed, in tmp_path)."""
    connection = db.open_db(tmp_path / "test.sqlite3")
    yield connection
    connection.close()


@pytest.fixture
def static_conn(conn: sqlite3.Connection, tmp_path: Path) -> sqlite3.Connection:
    """A database with the static fixture loaded."""
    load_static_file(conn, STATIC_ZIP, tmp_path)
    return conn


@pytest.fixture
def static(static_conn: sqlite3.Connection) -> StaticGtfs:
    return StaticGtfs(static_conn)


def make_store(conn: sqlite3.Connection, data_path: Path, now: float = HEADER_TS + 10) -> Store:
    run_id = start_run(conn, "test", {}, now)
    return Store(conn, StaticGtfs(conn), run_id, data_path, archive_raw=True, now=now)


@pytest.fixture
def store(static_conn: sqlite3.Connection, tmp_path: Path) -> Store:
    """A Store over the static fixture, archiving to tmp_path."""
    return make_store(static_conn, tmp_path)


@pytest.fixture
def loaded_store(store: Store) -> Store:
    """A Store that has stored the vehicles, trips and alerts fixtures."""
    for feed in ("vehicles", "trips", "alerts"):
        assert store.store_feed(feed, fixture_bytes(f"{feed}.pb"), HEADER_TS + 3).stored
    return store
