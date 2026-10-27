import json
import sqlite3
from pathlib import Path

import pytest
from google.transit import gtfs_realtime_pb2 as pb

from aaata_gtfs import db, filestore
from aaata_gtfs.store import MalformedFeed, Store
from tests.conftest import HEADER_TS, fixture_bytes, fixture_message, make_store, with_header


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in db.TABLES if t != "collector_run"}


def test_first_snapshots(loaded_store: Store) -> None:
    c = counts(loaded_store.conn)
    assert c["feed_snapshot"] == 3
    assert c["vehicle_position"] == 41
    assert c["vehicle_seen"] == 41
    assert c["trip_update"] == 45
    assert c["trip_modifications_version"] == 6
    assert c["trip_modifications_trip"] == 108 + 10 + 82 + 9 + 80 + 61
    assert c["rt_shape_version"] == 6
    assert c["alert_version"] == 19
    # Positions on the static fixture's routes get the fixture day's service date.
    rows = loaded_store.conn.execute(
        "SELECT vehicle_id, service_date FROM vehicle_position WHERE route_id IN ('4', '22')"
    ).fetchall()
    assert len(rows) == 5 and {sd for _, sd in rows} == {"20261025"}
    # Trips not in the static fixture have no service date.
    assert loaded_store.conn.execute(
        "SELECT COUNT(*) FROM vehicle_position WHERE route_id = '3' AND service_date IS NULL"
    ).fetchone()[0] > 0


def test_raw_archive(loaded_store: Store, tmp_path: Path) -> None:
    blob = loaded_store.conn.execute("SELECT blob FROM feed_snapshot WHERE feed = 'vehicles'").fetchone()[0]
    assert blob == "blobs/rt/vehicles/2026/10/25/22.pbs.gz"
    records = list(filestore.iter_rt(tmp_path / blob))
    assert records == [(HEADER_TS, fixture_bytes("vehicles.pb"))]


def test_same_vehicles_twice_adds_nothing(loaded_store: Store) -> None:
    before = counts(loaded_store.conn)
    result = loaded_store.store_feed("vehicles", fixture_bytes("vehicles.pb"), HEADER_TS + 10)
    assert not result.stored
    # Same positions in a new snapshot: no new positions, presence intervals extended.
    newer = with_header(fixture_message("vehicles.pb"), HEADER_TS + 30).SerializeToString()
    result = loaded_store.store_feed("vehicles", newer, HEADER_TS + 33)
    assert result.stored and result.counts["new_fixes"] == 0
    after = counts(loaded_store.conn)
    assert after["vehicle_position"] == before["vehicle_position"]
    assert after["vehicle_seen"] == before["vehicle_seen"]
    assert after["feed_snapshot"] == before["feed_snapshot"] + 1
    assert loaded_store.conn.execute(
        "SELECT COUNT(*) FROM vehicle_seen WHERE last_snapshot_id = ?", (result.snapshot_id,)
    ).fetchone()[0] == 41


def test_one_new_fix_adds_one_position(loaded_store: Store) -> None:
    before = counts(loaded_store.conn)
    message = with_header(fixture_message("vehicles.pb"), HEADER_TS + 30)
    message.entity[0].vehicle.timestamp += 30
    result = loaded_store.store_feed("vehicles", message.SerializeToString(), HEADER_TS + 33)
    assert result.counts["new_fixes"] == 1
    assert counts(loaded_store.conn)["vehicle_position"] == before["vehicle_position"] + 1


def test_vehicle_presence_intervals(loaded_store: Store) -> None:
    message = with_header(fixture_message("vehicles.pb"), HEADER_TS + 30)
    gone = message.entity[0].vehicle.vehicle.id
    del message.entity[0]
    loaded_store.store_feed("vehicles", message.SerializeToString(), HEADER_TS + 33)
    back = with_header(fixture_message("vehicles.pb"), HEADER_TS + 60)
    loaded_store.store_feed("vehicles", back.SerializeToString(), HEADER_TS + 63)
    intervals = loaded_store.conn.execute(
        "SELECT first_snapshot_id, last_snapshot_id FROM vehicle_seen WHERE vehicle_id = ? ORDER BY 1", (gone,)
    ).fetchall()
    assert len(intervals) == 2  # left the feed and came back
    other = message.entity[0].vehicle.vehicle.id
    assert loaded_store.conn.execute(
        "SELECT COUNT(*) FROM vehicle_seen WHERE vehicle_id = ?", (other,)
    ).fetchone()[0] == 1


def test_conflicting_repeat_is_counted(loaded_store: Store) -> None:
    message = with_header(fixture_message("vehicles.pb"), HEADER_TS + 30)
    message.entity[0].vehicle.position.latitude += 0.001  # same fix_ts, different content
    result = loaded_store.store_feed("vehicles", message.SerializeToString(), HEADER_TS + 33)
    assert result.counts["conflicts"] == 1 and result.counts["new_fixes"] == 0
    assert loaded_store.stats["conflicts"] == 1


def test_same_trips_twice_only_advances_last_seen(loaded_store: Store) -> None:
    before = counts(loaded_store.conn)
    assert not loaded_store.store_feed("trips", fixture_bytes("trips.pb"), HEADER_TS + 10).stored
    newer = with_header(fixture_message("trips.pb"), HEADER_TS + 30).SerializeToString()
    result = loaded_store.store_feed("trips", newer, HEADER_TS + 33)
    assert result.counts["tu_rows"] == 0 and result.counts["unchanged"] == 45
    after = counts(loaded_store.conn)
    for table in ("trip_update", "trip_modifications_version", "rt_shape_version"):
        assert after[table] == before[table], table
    conn = loaded_store.conn
    assert conn.execute(
        "SELECT COUNT(*) FROM trip_update WHERE last_seen_snapshot_id = ?", (result.snapshot_id,)
    ).fetchone()[0] == 45
    for table in ("trip_modifications_version", "rt_shape_version"):
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE last_snapshot_id = ?", (result.snapshot_id,)
        ).fetchone()[0] == 6


def first_arrival(message: pb.FeedMessage) -> pb.TripUpdate.StopTimeUpdate:
    for entity in message.entity:
        if entity.HasField("trip_update"):
            for stu in entity.trip_update.stop_time_update:
                if stu.HasField("arrival"):
                    return stu
    raise AssertionError("no arrival in fixture")


def test_changed_arrival_alone_adds_no_rows(loaded_store: Store) -> None:
    """Predictions aren't stored (the raw archive keeps them), so a moved arrival
    doesn't version the trip update; passing a stop does."""
    before = counts(loaded_store.conn)
    message = with_header(fixture_message("trips.pb"), HEADER_TS + 30)
    first_arrival(message).arrival.time += 60
    result = loaded_store.store_feed("trips", message.SerializeToString(), HEADER_TS + 33)
    assert result.counts["tu_rows"] == 0
    assert counts(loaded_store.conn)["trip_update"] == before["trip_update"]

    message = with_header(fixture_message("trips.pb"), HEADER_TS + 60)
    tu = next(e.trip_update for e in message.entity if e.trip_update.trip.trip_id == "1532010")
    del tu.stop_time_update[0]
    result = loaded_store.store_feed("trips", message.SerializeToString(), HEADER_TS + 63)
    assert result.counts["tu_rows"] == 1
    assert counts(loaded_store.conn)["trip_update"] == before["trip_update"] + 1


def test_changed_versions(loaded_store: Store) -> None:
    conn = loaded_store.conn
    message = with_header(fixture_message("trips.pb"), HEADER_TS + 30)
    for entity in message.entity:
        if entity.HasField("shape"):
            entity.shape.encoded_polyline += "??"
            break
    for entity in message.entity:
        if entity.HasField("trip_modifications"):
            entity.trip_modifications.service_dates.append("20261027")
            break
    result = loaded_store.store_feed("trips", message.SerializeToString(), HEADER_TS + 33)
    assert result.counts["shape_versions"] == 1 and result.counts["modification_versions"] == 1
    assert conn.execute("SELECT COUNT(*) FROM rt_shape_version").fetchone()[0] == 7
    assert conn.execute("SELECT COUNT(*) FROM trip_modifications_version").fetchone()[0] == 7
    # The other five of each were only extended.
    for table in ("rt_shape_version", "trip_modifications_version"):
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE last_snapshot_id = ? AND first_snapshot_id < ?",
            (result.snapshot_id, result.snapshot_id),
        ).fetchone()[0] == 5


def test_alerts_dedup_and_versions(loaded_store: Store) -> None:
    conn = loaded_store.conn
    # Same content with a newer header: alerts are deduped on content, so nothing is stored.
    same = with_header(fixture_message("alerts.pb"), HEADER_TS + 300).SerializeToString()
    assert not loaded_store.store_feed("alerts", same, HEADER_TS + 303).stored
    assert conn.execute("SELECT COUNT(*) FROM feed_snapshot WHERE feed = 'alerts'").fetchone()[0] == 1
    # One alert changed: one new version, the rest extended.
    changed = with_header(fixture_message("alerts.pb"), HEADER_TS + 600)
    changed.entity[0].alert.header_text.translation[0].text += " (updated)"
    result = loaded_store.store_feed("alerts", changed.SerializeToString(), HEADER_TS + 603)
    assert result.stored and result.counts["alert_versions"] == 1
    assert conn.execute("SELECT COUNT(*) FROM alert_version").fetchone()[0] == 20
    assert conn.execute(
        "SELECT COUNT(*) FROM alert_version WHERE last_snapshot_id = ?", (result.snapshot_id,)
    ).fetchone()[0] == 19


def test_restart_inserts_nothing(loaded_store: Store, tmp_path: Path) -> None:
    conn = loaded_store.conn
    newer = {f: with_header(fixture_message(f"{f}.pb"), HEADER_TS + 30).SerializeToString()
             for f in ("vehicles", "trips")}
    for feed, payload in newer.items():
        loaded_store.store_feed(feed, payload, HEADER_TS + 33)
    before = counts(conn)

    restarted = make_store(conn, tmp_path, now=HEADER_TS + 40)
    for feed in ("vehicles", "trips", "alerts"):
        assert not restarted.store_feed(feed, fixture_bytes(f"{feed}.pb"), HEADER_TS + 41).stored
    for feed, payload in newer.items():
        assert not restarted.store_feed(feed, payload, HEADER_TS + 41).stored
    assert counts(conn) == before

    # The next refresh (same content) after the restart adds only a snapshot row each.
    for feed in ("vehicles", "trips"):
        payload = with_header(fixture_message(f"{feed}.pb"), HEADER_TS + 60).SerializeToString()
        result = restarted.store_feed(feed, payload, HEADER_TS + 63)
        assert result.stored
    after = counts(conn)
    assert after["feed_snapshot"] == before["feed_snapshot"] + 2
    for table in ("vehicle_position", "vehicle_seen", "trip_update",
                  "trip_modifications_version", "rt_shape_version", "alert_version"):
        assert after[table] == before[table], table


def test_bustime_versions(store: Store, tmp_path: Path) -> None:
    body = fixture_bytes("getroutebyid_22.json")
    payload = json.loads(body)
    assert store.store_bustime("22", body, payload, 1000.0)
    # Same content with patterns in another order: not a new version.
    route = payload["route"]
    route["pas"]["pa"] = list(reversed(route["pas"]["pa"]))
    assert not store.store_bustime("22", json.dumps(payload).encode(), payload, 2000.0)
    row = store.conn.execute(
        "SELECT route_version, fetched_at, last_checked_at, blob FROM bustime_route_version"
    ).fetchall()
    assert len(row) == 1
    route_version, fetched_at, last_checked_at, blob = row[0]
    assert route_version.startswith("22-") and (fetched_at, last_checked_at) == (1000.0, 2000.0)
    assert filestore.read_blob(blob, tmp_path) == body


def test_malformed_payload_is_archived(store: Store, tmp_path: Path) -> None:
    with pytest.raises(MalformedFeed):
        store.store_feed("vehicles", b"\xff\xff garbage", HEADER_TS)
    assert list((tmp_path / "blobs" / "rt" / "vehicles").rglob("*.pbs.gz"))
    assert store.conn.execute("SELECT COUNT(*) FROM feed_snapshot").fetchone()[0] == 0


def test_alerts_with_shuffled_translations_are_unchanged(loaded_store: Store) -> None:
    # TheRide reorders each alert's translations (and its entities) between fetches.
    message = with_header(fixture_message("alerts.pb"), HEADER_TS + 300)
    for entity in message.entity:
        for text in (entity.alert.header_text, entity.alert.description_text):
            translations = list(text.translation)
            del text.translation[:]
            text.translation.extend(reversed(translations))
    entities = list(message.entity)
    del message.entity[:]
    message.entity.extend(reversed(entities))
    assert not loaded_store.store_feed("alerts", message.SerializeToString(), HEADER_TS + 303).stored
    assert loaded_store.conn.execute("SELECT COUNT(*) FROM alert_version").fetchone()[0] == 19
