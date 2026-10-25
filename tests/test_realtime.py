import pytest

from aaata_gtfs.realtime import (
    DecodeError,
    alert_records,
    alerts_content_sha256,
    header_ts,
    parse_feed,
    pattern_id_of,
    rt_shape_records,
    trip_modifications_records,
    trip_update_records,
    vehicle_records,
)
from tests.conftest import HEADER_TS, fixture_bytes, fixture_message, with_header


def test_vehicles() -> None:
    message = parse_feed(fixture_bytes("vehicles.pb"))
    assert header_ts(message) == HEADER_TS
    records = vehicle_records(message)
    assert len(records) == 41
    assert sum(r.trip_id is not None for r in records) == 29
    by_id = {r.vehicle_id: r for r in records}
    route_4 = sorted(r.vehicle_id for r in records if r.route_id == "4")
    assert route_4 == ["1933", "2002", "553"]
    assert sorted(r.vehicle_id for r in records if r.route_id == "22") == ["505", "555"]

    bus = by_id["470"]
    assert (bus.trip_id, bus.route_id, bus.trip_schedule_relationship) == ("288010", "3", "SCHEDULED")
    assert bus.fix_ts == 1792966283
    assert bus.bearing == 333
    assert bus.speed_mps == 0.0
    # Not sent by the feed: absent, not 0 or "".
    assert bus.direction_id is None and bus.start_date is None and bus.current_stop_sequence is None
    assert bus.current_status is None and bus.occupancy_status is None and bus.odometer is None
    assert bus.vehicle_label is None

    out_of_service = [r for r in records if r.trip_id is None]
    assert all(r.route_id is None and r.trip_schedule_relationship is None for r in out_of_service)


def test_trip_updates() -> None:
    message = fixture_message("trips.pb")
    updates = trip_update_records(message)
    assert len(updates) == 45
    assert sum(u.modifications_id is not None for u in updates) == 3
    assert sum(u.n_stop_time_updates for u in updates) == 696
    assert all(u.start_date is None and u.delay is None for u in updates)

    detoured = next(u for u in updates if u.trip_id == "1532010")
    assert detoured.modifications_id == "trip_modifications_501314"
    assert detoured.vehicle_id == "549"
    assert detoured.update_ts == 1792966311
    assert detoured.first_stop_sequence == 1
    first = detoured.stops[0]
    # The first stop has only a departure.
    assert first.arrival_time is None and first.departure_time == 1792966520
    assert first.stop_id == "1604" and first.schedule_relationship == "SCHEDULED"
    assert first.arrival_delay is None and first.arrival_uncertainty is None
    assert all(a.stop_sequence < b.stop_sequence for a, b in zip(updates[0].stops, updates[0].stops[1:]))


def test_trip_modifications_and_shapes() -> None:
    message = fixture_message("trips.pb")
    mods = {m.modifications_id: m for m in trip_modifications_records(message)}
    assert len(mods) == 6
    counts = {m.selected_trips[0][1]: len(m.selected_trips) for m in mods.values()}
    assert counts == {
        "shape_501393": 108, "shape_501401": 10, "shape_501400": 82,
        "shape_501402": 9, "shape_501314": 80, "shape_501391": 61,
    }
    mod = mods["trip_modifications_501393"]
    assert mod.pattern_id == "501393"
    assert "20261025" in mod.service_dates
    assert '"start_stop_selector":{"stop_id":"577"}' in mod.content_json

    shapes = rt_shape_records(message)
    assert sorted(s.shape_id for s in shapes) == sorted(str(k) for k in counts)
    assert all(s.encoded_polyline for s in shapes)


def test_modification_hash_ignores_trip_order() -> None:
    message = fixture_message("trips.pb")
    entity = next(e for e in message.entity if e.HasField("trip_modifications"))
    before = trip_modifications_records(message)[0].content_sha256
    trip_ids = list(entity.trip_modifications.selected_trips[0].trip_ids)
    del entity.trip_modifications.selected_trips[0].trip_ids[:]
    entity.trip_modifications.selected_trips[0].trip_ids.extend(reversed(trip_ids))
    assert trip_modifications_records(message)[0].content_sha256 == before


def test_alerts() -> None:
    message = fixture_message("alerts.pb")
    alerts = alert_records(message)
    assert len(alerts) == 19
    first = next(a for a in alerts if a.alert_id == "4th Ave/BTC Temporary Closure Starts May 3")
    assert first.header_text == "4th Ave/BTC Closure Starts May 3"
    assert first.route_ids == ""
    assert all(a.content_sha256 for a in alerts)


def test_alerts_content_hash_ignores_header() -> None:
    message = fixture_message("alerts.pb")
    assert alerts_content_sha256(message) == alerts_content_sha256(with_header(message, HEADER_TS + 30))


def test_malformed() -> None:
    with pytest.raises(DecodeError):
        parse_feed(b"\xff\xff not a protobuf")


def test_pattern_id_of() -> None:
    assert pattern_id_of("trip_modifications_501393") == "501393"
    assert pattern_id_of("no_digits") is None
