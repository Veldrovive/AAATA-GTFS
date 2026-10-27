import json
from pathlib import Path

import pytest

from aaata_gtfs.bustime import parse_patterns
from aaata_gtfs.geometry import ShapeGeometry, snap_sightings
from aaata_gtfs.polyline import decode_polyline
from aaata_gtfs.queries import positions
from aaata_gtfs.shapes import GeometryResolver
from aaata_gtfs.static_gtfs import StaticGtfs
from aaata_gtfs.store import Store
from tests.conftest import FIXTURES, HEADER_TS


def test_decode_polyline() -> None:
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [
        (38.5, -120.2), (40.7, -120.95), (43.252, -126.453),
    ]


def static_geometry(static: StaticGtfs, shape_id: str) -> ShapeGeometry:
    version = static.latest_version()
    assert version is not None
    trip = next(t for r in ("4", "22") for t in static.route_trips(version, r) if t.shape_id == shape_id)
    resolver = GeometryResolver(static.session, static)
    geometry = resolver.static_geometry(trip)
    assert geometry is not None
    return geometry


def test_spur_removal_shp_22_04(static: StaticGtfs) -> None:
    g = static_geometry(static, "shp-22-04")
    assert len(g.stops) == 34 and all(s.on_vertex for s in g.stops)
    assert (g.n_input_vertices, g.n_vertices) == (1247, 1191)
    assert g.length_ft == pytest.approx(41_635, abs=1)
    raw = ShapeGeometry("raw", "22", [(p.lat, p.lon) for p in static.shape_points(g.key.split(":")[1], "shp-22-04")])
    assert raw.length_ft == pytest.approx(43_219, abs=1)
    # Stop distances increase along the trip and roughly agree with the agency's.
    dists = [s.dist_along_ft for s in g.stops]
    assert dists == sorted(dists)
    last = g.stops[-1]
    agency = g.agency_dist_ft(last.stop_sequence or 0)
    assert agency is not None and abs(last.dist_along_ft / agency - 1) < 0.01


def test_no_spurs_on_shp_22_01(static: StaticGtfs) -> None:
    g = static_geometry(static, "shp-22-01")
    assert (g.n_input_vertices, g.n_vertices) == (707, 707)
    assert g.length_ft == pytest.approx(38_011, abs=1)


def test_projection_round_trip(static: StaticGtfs) -> None:
    g = static_geometry(static, "shp-4-55")
    for d in (0.0, 1234.5, g.length_ft / 2, g.length_ft - 10):
        lat, lon = g.point_at(d)
        p = g.project(lat, lon, near_ft=d)
        assert p.dist_along_ft == pytest.approx(d, abs=0.5)
        assert p.offset_ft < 0.5
    path = g.path_between(1000, 2000)
    assert path[0] == pytest.approx(g.point_at(1000)) and path[-1] == pytest.approx(g.point_at(2000))


def test_near_ft_picks_the_right_pass() -> None:
    # Out 1 km east along a street and back on the same street.
    lat0, lon0 = 42.28, -83.74
    east = [(lat0, lon0 + i * 0.001) for i in range(13)]
    g = ShapeGeometry("test", "x", east + east[::-1][1:])
    half = g.length_ft / 2
    lat, lon = east[4]
    outbound = g.project(lat, lon, near_ft=0.25 * half)
    inbound = g.project(lat, lon, near_ft=1.75 * half)
    assert outbound.dist_along_ft < half < inbound.dist_along_ft
    assert outbound.offset_ft < 1 and inbound.offset_ft < 1
    assert inbound.dist_along_ft == pytest.approx(2 * half - outbound.dist_along_ft, abs=1)


def test_project_many_chunks(static: StaticGtfs) -> None:
    g = static_geometry(static, "shp-4-55")
    lats, lons = zip(*[g.point_at(d) for d in range(0, int(g.length_ft), 9)])
    projections = g.project_many(lats, lons)
    assert len(projections) > 2000
    assert max(p.offset_ft for p in projections) < 0.5


def test_fixture_route_22_buses_snap(loaded_store: Store, tmp_path: Path) -> None:
    session = loaded_store.session
    resolver = GeometryResolver(session, loaded_store.static, tmp_path)
    rows = positions(session, HEADER_TS - 600, HEADER_TS, ["22"])
    assert sorted(r.vehicle_id for r in rows) == ["505", "555"]
    snapped = {ping.position.vehicle_id: ping for ping in snap_sightings(rows, resolver)}
    on_route = snapped["505"]
    assert on_route.projection is not None and on_route.projection.offset_ft < 20 and on_route.on_route
    # 555 was on BusTime's Nixon detour (pattern 501321 in getroutebyid_22.json, 14 ft
    # from its path), which GTFS-RT didn't announce: ~1,630 ft off the static shape.
    off_route = snapped["555"]
    assert off_route.projection is not None and off_route.geometry is not None
    assert off_route.geometry.shape_id == "shp-22-01"
    assert off_route.projection.offset_ft == pytest.approx(1630, abs=5)
    assert not off_route.on_route


def test_resolver_picks_detour_and_static_shapes(loaded_store: Store, tmp_path: Path) -> None:
    resolver = GeometryResolver(loaded_store.session, loaded_store.static, tmp_path)
    detoured = resolver.for_trip("2705010", "20261025", HEADER_TS)  # in trip_modifications_501393
    assert detoured is not None and detoured.key.startswith("rt:") and detoured.shape_id == "shape_501393"
    # Its stops are the static trip's, located on the RT shape.
    assert len(detoured.stops) == 34
    regular = resolver.for_trip("378010", "20261025", HEADER_TS)
    assert regular is not None and regular.key.endswith(":shp-4-55") and regular.key.startswith("static:")
    # Outside the detour's effective time, the static shape again.
    later = resolver.for_trip("2705010", "20261025", HEADER_TS + 3600)
    assert later is not None and later.shape_id == "shp-4-06"
    assert resolver.pattern_for_trip("2705010", "20261025", HEADER_TS) == "501393"


def test_pattern_matching_route_22(loaded_store: Store, tmp_path: Path) -> None:
    body = (FIXTURES / "getroutebyid_22.json").read_bytes()
    payload = json.loads(body)
    loaded_store.store_bustime("22", body, payload, HEADER_TS - 100)
    resolver = GeometryResolver(loaded_store.session, loaded_store.static, tmp_path)
    bustime = resolver.bustime_version_at("22", HEADER_TS)
    version = loaded_store.static.latest_version()
    assert bustime is not None and version is not None
    # shp-22-04 matches its undetoured pattern exactly.
    assert resolver.shape_pattern_ids(version, bustime) == {"shp-22-04": "7599"}
    assert resolver.pattern_for_trip("259010", "20261025", HEADER_TS) == "7599"
    # The payload's Nixon detour replaced shp-22-01's pattern: it matches only the
    # detoured pattern's bypassed regular path.
    detour = next(p for p in parse_patterns(payload) if p.pattern_id == "501321")
    assert detour.is_detoured
    stops = loaded_store.static.stops(version)
    trip = next(t for t in loaded_store.static.route_trips(version, "22") if t.shape_id == "shp-22-01")
    codes = [stops[st.stop_id].stop_code for st in loaded_store.static.stop_times(version, trip.trip_id)]
    assert detour.regular_stop_ids() == codes
    assert resolver.pattern_for_trip(trip.trip_id, "20261025", HEADER_TS) is None
