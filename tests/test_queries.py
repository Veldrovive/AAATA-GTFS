from pathlib import Path

from aaata_gtfs import filestore
from aaata_gtfs.queries import (
    active_vehicles,
    latest_vehicles_snapshot,
    positions,
    rt_shape_at,
    trip_modifications_at,
)
from aaata_gtfs.store import Store
from tests.conftest import HEADER_TS, fixture_message, with_header


def test_rt_archive_appends_members(tmp_path: Path) -> None:
    a = filestore.append_rt("vehicles", HEADER_TS, b"first", tmp_path)
    b = filestore.append_rt("vehicles", HEADER_TS + 30, b"second", tmp_path)
    assert a == b
    assert list(filestore.iter_rt(tmp_path / a)) == [(HEADER_TS, b"first"), (HEADER_TS + 30, b"second")]


def test_positions_and_active_vehicles(loaded_store: Store) -> None:
    session = loaded_store.session
    rows = positions(session, 0, HEADER_TS)
    assert len(rows) == 41
    assert rows == sorted(rows, key=lambda r: (r.vehicle_id, r.fix_ts))
    assert {r.vehicle_id for r in positions(session, 0, HEADER_TS, ["4"])} == {"1933", "2002", "553"}
    snapshot = latest_vehicles_snapshot(session, HEADER_TS + 5)
    assert snapshot is not None and snapshot[1] == HEADER_TS
    assert latest_vehicles_snapshot(session, HEADER_TS - 1) is None
    active = active_vehicles(session, HEADER_TS + 5)
    # Vehicles whose fix is over 120 s old at the snapshot have stopped reporting.
    stale = {r.vehicle_id for r in rows if HEADER_TS - r.fix_ts > 120}
    assert stale and active == {r.vehicle_id for r in rows} - stale


def test_detours_in_effect(loaded_store: Store) -> None:
    session = loaded_store.session
    mods = trip_modifications_at(session, HEADER_TS)
    assert len(mods) == 6
    mod = next(m for m in mods if m.modifications_id == "trip_modifications_501393")
    assert mod.pattern_id == "501393" and len(mod.trips) == 108 and "20261025" in mod.service_dates
    assert trip_modifications_at(session, HEADER_TS + 3600) == []
    shape = rt_shape_at(session, "shape_501393", HEADER_TS + 30)
    assert shape is not None and shape.encoded_polyline
    assert rt_shape_at(session, "shape_501393", HEADER_TS + 3600) is None
