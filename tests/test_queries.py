import sqlite3
from pathlib import Path

from aaata_gtfs import db, filestore
from aaata_gtfs.queries import (
    active_vehicles,
    latest_vehicles_snapshot,
    positions,
    predictions_at,
    rt_shape_at,
    trip_modifications_at,
)
from aaata_gtfs.store import Store
from tests.conftest import HEADER_TS, fixture_message, with_header


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "x.sqlite3")
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    assert db.migrate(conn) == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    ro = db.connect(tmp_path / "x.sqlite3", read_only=True)
    try:
        ro.execute("INSERT INTO collector_run (started_at, version, args) VALUES (1, 'x', '{}')")
        raise AssertionError("read-only connection accepted a write")
    except sqlite3.OperationalError:
        pass


def test_rt_archive_appends_members(tmp_path: Path) -> None:
    a = filestore.append_rt("vehicles", HEADER_TS, b"first", tmp_path)
    b = filestore.append_rt("vehicles", HEADER_TS + 30, b"second", tmp_path)
    assert a == b
    assert list(filestore.iter_rt(tmp_path / a)) == [(HEADER_TS, b"first"), (HEADER_TS + 30, b"second")]


def test_positions_and_active_vehicles(loaded_store: Store) -> None:
    conn = loaded_store.conn
    rows = positions(conn, 0, HEADER_TS)
    assert len(rows) == 41
    assert rows == sorted(rows, key=lambda r: (r.vehicle_id, r.fix_ts))
    assert {r.vehicle_id for r in positions(conn, 0, HEADER_TS, ["4"])} == {"1933", "2002", "553"}
    snapshot = latest_vehicles_snapshot(conn, HEADER_TS + 5)
    assert snapshot is not None and snapshot[1] == HEADER_TS
    assert latest_vehicles_snapshot(conn, HEADER_TS - 1) is None
    active = active_vehicles(conn, HEADER_TS + 5)
    # Vehicles whose fix is over 120 s old at the snapshot have stopped reporting.
    stale = {r.vehicle_id for r in rows if HEADER_TS - r.fix_ts > 120}
    assert stale and active == {r.vehicle_id for r in rows} - stale


def test_predictions_at(loaded_store: Store) -> None:
    conn = loaded_store.conn
    first = conn.execute("SELECT id FROM feed_snapshot WHERE feed = 'trips'").fetchone()[0]
    before = predictions_at(conn, "1532010", None, first)
    assert len(before) == 16 and before[0].departure_time == 1792966520

    # Next refresh: the first stop has been passed and one arrival moved.
    message = with_header(fixture_message("trips.pb"), HEADER_TS + 30)
    tu = next(e.trip_update for e in message.entity if e.trip_update.trip.trip_id == "1532010")
    del tu.stop_time_update[0]
    tu.stop_time_update[0].arrival.time += 45
    result = loaded_store.store_feed("trips", message.SerializeToString(), HEADER_TS + 33)
    assert result.snapshot_id is not None
    after = predictions_at(conn, "1532010", None, result.snapshot_id)
    assert [r.stop_sequence for r in after] == list(range(2, 17))
    moved = before[1].arrival_time
    assert moved is not None and after[0].arrival_time == moved + 45
    assert after[1] == before[2]  # unchanged stops come from the earlier snapshot
    # As of the first snapshot, nothing changed.
    assert predictions_at(conn, "1532010", None, first) == before


def test_detours_in_effect(loaded_store: Store) -> None:
    conn = loaded_store.conn
    mods = trip_modifications_at(conn, HEADER_TS)
    assert len(mods) == 6
    mod = next(m for m in mods if m.modifications_id == "trip_modifications_501393")
    assert mod.pattern_id == "501393" and len(mod.trips) == 108 and "20261025" in mod.service_dates
    assert trip_modifications_at(conn, HEADER_TS + 3600) == []
    shape = rt_shape_at(conn, "shape_501393", HEADER_TS + 30)
    assert shape is not None and shape.encoded_polyline
    assert rt_shape_at(conn, "shape_501393", HEADER_TS + 3600) is None
