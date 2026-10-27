"""Shared fixtures: real feed captures from 2026-10-25 ~22:12 UTC (see SPEC.md §10)."""

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Unpack

import pytest
from google.transit import gtfs_realtime_pb2 as pb
from sqlalchemy import CursorResult
from sqlmodel import Session

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


def sql(session: Session, query: str, params: Sequence[Any] = ()) -> CursorResult[Unpack[tuple[Any, ...]]]:
    """Run raw SQL on the session's connection, to check what's actually stored
    independently of the models."""
    return session.connection().exec_driver_sql(query, tuple(params))


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    """A fresh database (file-backed, in tmp_path)."""
    s = db.open_db(tmp_path / "test.sqlite3")
    yield s
    s.close()


@pytest.fixture
def static_session(session: Session, tmp_path: Path) -> Session:
    """A database with the static fixture loaded."""
    load_static_file(session, STATIC_ZIP, tmp_path)
    return session


@pytest.fixture
def static(static_session: Session) -> StaticGtfs:
    return StaticGtfs(static_session)


def make_store(session: Session, data_path: Path, now: float = HEADER_TS + 10) -> Store:
    run_id = start_run(session, "test", {}, now)
    return Store(session, StaticGtfs(session), run_id, data_path, archive_raw=True, now=now)


@pytest.fixture
def store(static_session: Session, tmp_path: Path) -> Store:
    """A Store over the static fixture, archiving to tmp_path."""
    return make_store(static_session, tmp_path)


@pytest.fixture
def loaded_store(store: Store) -> Store:
    """A Store that has stored the vehicles, trips and alerts fixtures."""
    for feed in ("vehicles", "trips", "alerts"):
        assert store.store_feed(feed, fixture_bytes(f"{feed}.pb"), HEADER_TS + 3).stored
    return store
