"""Trip reconstruction stages and stop crossings, on synthetic positions along fixture shapes."""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from sqlmodel import Session

from aaata_gtfs.geometry import ShapeGeometry
from aaata_gtfs.models import FeedSnapshot, TripUpdate
from aaata_gtfs.queries import ModificationVersion
from aaata_gtfs.service_dates import schedule_ts
from aaata_gtfs.shapes import GeometryResolver
from aaata_gtfs.static_gtfs import StaticGtfs
from aaata_gtfs.store import start_run
from aaata_gtfs.trips import (
    Assignment,
    Sighting,
    TripAssignments,
    agency_stop_passed,
    isotonic_fit,
    reconstruct,
    stop_crossings,
)

SERVICE_DATE = "20261025"
# Consecutive trips of block 220222: A ends at Blake Transit Center, where B starts.
TRIP_A, TRIP_B = "2719010", "4233010"
VEHICLE = "555"


@pytest.fixture
def resolver(static_session: Session, static: StaticGtfs, tmp_path: Path) -> GeometryResolver:
    return GeometryResolver(static_session, static, tmp_path)


def geometry_of(resolver: GeometryResolver, trip_id: str) -> ShapeGeometry:
    trip = resolver.static.trip(trip_id, SERVICE_DATE)
    assert trip is not None
    geometry = resolver.static_geometry(trip)
    assert geometry is not None
    return geometry


def along(
    geometry: ShapeGeometry, dists: Sequence[float], t0: float, trip_id: str | None, dt: int = 30,
    offset_deg: float = 0.0,
) -> list[Sighting]:
    out = []
    for k, d in enumerate(dists):
        lat, lon = geometry.point_at(d)
        t = int(t0) + k * dt
        out.append(Sighting(VEHICLE, t, lat + offset_deg, lon, trip_id, "22", SERVICE_DATE, t + 15))
    return out


def start_of(trip_id: str) -> float:
    return schedule_ts(SERVICE_DATE, {TRIP_A: 28800, TRIP_B: 30600}[trip_id])


def test_isotonic_fit() -> None:
    assert isotonic_fit([1, 3, 2, 4, 0, 5]) == [1, 2.25, 2.25, 2.25, 2.25, 5]


def test_clean_trip_stages(resolver: GeometryResolver) -> None:
    g = geometry_of(resolver, TRIP_A)
    dists = list(np.linspace(0, 4000, 9)) + [5000.0] * 4 + [8000.0, 9000.0, 10000.0, 9500.0, 10600.0]
    dists += list(np.linspace(11000, g.length_ft, 20))
    result = reconstruct(along(g, dists, start_of(TRIP_A), TRIP_A), resolver)
    assert len(result.trajectories) == 1 and not result.dropped
    t = result.trajectories[0]
    assert (t.trip_id, t.service_date, t.vehicle_id, t.route_id) == (TRIP_A, SERVICE_DATE, VEHICLE, "22")
    reasons = [r.reason for r in t.removed]
    # The 3 repeats at 5,000 ft precede a 100 ft/s move; 9,500 is a one-off backward jump.
    assert reasons.count("stale_repeat") == 3 and reasons.count("backward_jump") == 1
    assert len(t.pings) == len(dists) - 4
    assert (np.diff(t.distances) >= 0).all()
    assert abs(t.distances[-1] - g.length_ft) < 5


def test_relabels_trip_boundary(resolver: GeometryResolver) -> None:
    ga, gb = geometry_of(resolver, TRIP_A), geometry_of(resolver, TRIP_B)
    t_end = start_of(TRIP_B) - 120
    a = along(ga, list(np.linspace(0, ga.length_ft, 40)), t_end - 39 * 30, TRIP_A)
    # At B's start (A's end) for 3 fixes, then 4 fixes on B, all still reporting
    # trip A: the 4 on B are relabelled, the 3 at the terminus stay A's arrival.
    b = along(gb, [0.0, 0.0, 0.0] + list(np.linspace(500, 3000, 4)) + list(np.linspace(3500, 9000, 10)),
              t_end + 30, None)
    b = [Sighting(s.vehicle_id, s.fix_ts, s.lat, s.lon, TRIP_A if k < 7 else TRIP_B, s.route_id,
                  s.service_date, s.header_ts) for k, s in enumerate(b)]
    # The trips feed drops A (moving the bus on to B) 5 fixes before A's end.
    switch = a[-5].header_ts
    assignments = TripAssignments([
        (VEHICLE, Assignment(TRIP_A, SERVICE_DATE, a[0].header_ts, switch - 1, start_of(TRIP_A))),
        (VEHICLE, Assignment(TRIP_B, SERVICE_DATE, a[0].header_ts, b[-1].header_ts, start_of(TRIP_B))),
    ])
    result = reconstruct(a + b, resolver, assignments)
    by_trip = {t.trip_id: t for t in result.trajectories}
    assert len(by_trip[TRIP_A].pings) == 43 and by_trip[TRIP_A].relabelled == 0
    assert len(by_trip[TRIP_B].pings) == len(b) - 3 and by_trip[TRIP_B].relabelled == 4


def test_cuts_approach_before_start(resolver: GeometryResolver) -> None:
    g = geometry_of(resolver, TRIP_A)
    # Deadheading backwards along the route to the start, then the trip.
    dists = [3000.0, 2000.0, 1000.0, 0.0] + list(np.linspace(300, 12000, 15))
    result = reconstruct(along(g, dists, start_of(TRIP_A) - 120, TRIP_A), resolver)
    t = result.trajectories[0]
    assert [r.reason for r in t.removed] == ["before_restart"] * 3
    assert t.pings[0].raw_dist_ft < 5


def test_drops_ambiguous_double_run(resolver: GeometryResolver) -> None:
    g = geometry_of(resolver, TRIP_A)
    dists = list(np.linspace(0, 8000, 8)) + list(np.linspace(6000, 0, 4)) + list(np.linspace(500, 12000, 10))
    result = reconstruct(along(g, dists, start_of(TRIP_A), TRIP_A), resolver)
    assert not result.trajectories and result.dropped == {"restart": 1}


def test_drops_off_route_run(resolver: GeometryResolver) -> None:
    g = geometry_of(resolver, TRIP_A)
    on = along(g, list(np.linspace(0, 12000, 20)), start_of(TRIP_A), TRIP_A)
    off = along(g, [6000.0, 6500.0, 7000.0], on[7].fix_ts + 5, TRIP_A, dt=5, offset_deg=0.05)
    result = reconstruct(on + off, resolver)
    assert not result.trajectories and result.dropped == {"off_route_run": 1}
    # One stray position is only removed.
    result = reconstruct(on + off[:1], resolver)
    assert [r.reason for r in result.trajectories[0].removed] == ["off_route"]


def test_drops_detoured(resolver: GeometryResolver) -> None:
    g = geometry_of(resolver, TRIP_A)
    sightings = along(g, list(np.linspace(0, 12000, 20)), start_of(TRIP_A), TRIP_A)
    t0 = sightings[5].fix_ts
    resolver.modifications = [
        ModificationVersion(1, "trip_modifications_1", "1", t0, t0 + 60, {TRIP_A: "rt-shape"}, frozenset())
    ]
    result = reconstruct(sightings, resolver)
    assert not result.trajectories and result.dropped == {"detoured": 1}


def test_stop_crossings(resolver: GeometryResolver) -> None:
    g = geometry_of(resolver, TRIP_A)
    stop = g.stops[5]
    d = stop.dist_along_ft
    # Approach at 30 ft/s, dwell 60 s at the stop, leave at 30 ft/s; a fix every 10 s.
    dists = [d - 600 + 300 * k for k in range(2)] + [d] * 7 + [d + 300 * (k + 1) for k in range(3)]
    t0 = int(start_of(TRIP_A))
    result = reconstruct(along(g, dists, t0, TRIP_A, dt=10), resolver)
    crossing = next(c for c in stop_crossings(result.trajectories[0]) if c.stop_id == stop.stop_id)
    assert crossing.arrival_ts == pytest.approx(t0 + 10 + 250 / 30, abs=0.01)
    assert crossing.passed_ts == pytest.approx(t0 + 20, abs=0.01)
    assert crossing.departure_ts == pytest.approx(t0 + 80 + 50 / 30, abs=0.01)
    assert crossing.dwell_s == pytest.approx(70 + 50 / 30 - 250 / 30, abs=0.01)
    first = stop_crossings(result.trajectories[0])[0]
    assert first.arrival_ts is None and first.departure_ts is None  # not observed


def test_agency_stop_passed(static_session: Session) -> None:
    run_id = start_run(static_session, "test", {}, 0.0)
    for k, (header_ts, first_seq) in enumerate([(100, 1), (130, 1), (160, 3), (190, 4)], start=1):
        static_session.add(FeedSnapshot(
            id=k, feed="trips", header_ts=header_ts, fetched_at=header_ts, sha256="x", n_entities=0, n_bytes=0,
            run_id=run_id,
        ))
        static_session.add(TripUpdate(
            trip_id=TRIP_A, service_date=SERVICE_DATE, first_stop_sequence=first_seq, n_stop_time_updates=0,
            snapshot_id=k, last_seen_snapshot_id=k,
        ))
    static_session.commit()
    assert agency_stop_passed(static_session, TRIP_A, SERVICE_DATE) == {1: 160, 2: 160, 3: 190}
