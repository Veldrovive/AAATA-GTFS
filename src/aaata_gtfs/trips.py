"""Trip reconstruction: each trip instance's positions as distance along its shape.

A trajectory is one vehicle's run of one trip instance (`trip_id`,
`service_date`), as clean (fix time, distance along the static shape) points.
It's built in explicit filtering stages, each removing what it rejects (with a
reason) rather than flagging it:

1. **Trip labels.** Each position gets the trip id the vehicle reports, unless
   the trips feed then has the vehicle on a different *current* trip (the
   earliest scheduled of the trips assigned to it, since passed trips drop
   out) and the bus has already been seen at that trip's start and isn't at
   the end of the reported one. This fixes the stale trip id a bus reports
   for a while after starting its next trip, without moving the end of its
   last trip (the trips feed often moves a bus on while it's still
   approaching the terminus).
2. **Detours.** Trips that a trip modification selects at any time they're
   observed are dropped whole (the model only uses static shapes for now).
3. **Off-route.** Positions further than `max_offset_ft` from the shape are
   removed; a trajectory with a run of `max_off_route_run` or more of them
   between on-route ones (an unannounced detour) is dropped.
4. **Stale repeats.** Runs of a repeated position after which the bus moves
   implausibly fast are removed (it was moving while its position was frozen).
5. **Backward jumps.** A single position well behind both neighbours (an AVL
   quirk) is removed.
6. **Restarts.** Where the distance falls far below the furthest reached
   (a bus deadheading along its route to the start under the trip's id),
   the positions before the new run are removed.
7. **Monotone fit.** Distances are fitted by isotonic regression against fix
   time, absorbing the remaining few feet of snapping noise.

`stop_crossings` then interpolates each stop's arrival and departure.
"""

import sqlite3
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

import numpy as np

from aaata_gtfs.geometry import DEFAULT_MAX_OFFSET_FT, ShapeGeometry
from aaata_gtfs.service_dates import schedule_ts
from aaata_gtfs.shapes import GeometryResolver
from aaata_gtfs.static_gtfs import StaticGtfs

#: Speed (ft/s, ~55 mph) above which a bus must have moved during a run of
#: repeated positions.
STALE_SPEED_FTPS = 80.0

#: A position this far behind both its neighbours is an AVL backward jump.
BACKWARD_JUMP_FT = 100.0

#: A fall this far below the furthest distance reached means a restart.
RESTART_FT = 1000.0

#: This many consecutive off-route positions between on-route ones mean the
#: bus left its shape (~90 s at the feed's cadence).
MAX_OFF_ROUTE_RUN = 3

#: A bus this close to a trip's start (along its shape) has reached it.
START_ZONE_FT = 300.0

#: Trajectories with fewer positions than this are dropped.
MIN_PINGS = 5

#: A bus within this distance of a stop (along the shape) is at it.
STOP_ZONE_FT = 50.0


@dataclass(frozen=True)
class Sighting:
    """A vehicle position with the header time of the snapshot it first appeared in."""

    vehicle_id: str
    fix_ts: int
    lat: float
    lon: float
    trip_id: str | None
    route_id: str | None
    service_date: str | None
    header_ts: int


def load_sightings(conn: sqlite3.Connection, start_ts: float, end_ts: float) -> list[Sighting]:
    """Positions with `start_ts <= fix_ts <= end_ts`, ordered by vehicle then fix time."""
    return [
        Sighting(*row)
        for row in conn.execute(
            "SELECT vp.vehicle_id, vp.fix_ts, vp.lat, vp.lon, vp.trip_id, vp.route_id, vp.service_date, "
            "f.header_ts FROM vehicle_position vp JOIN feed_snapshot f ON f.id = vp.snapshot_id "
            "WHERE vp.fix_ts BETWEEN ? AND ? ORDER BY vp.vehicle_id, vp.fix_ts",
            (int(start_ts), int(end_ts)),
        )
    ]


@dataclass(frozen=True)
class Assignment:
    """The trips feed had a vehicle on a trip instance from `first_ts` to `last_ts` (header times)."""

    trip_id: str
    service_date: str | None
    first_ts: int
    last_ts: int
    scheduled_start: float  # epoch seconds; inf if unknown


class TripAssignments:
    """Which trip the trips feed had each vehicle on, by header time."""

    def __init__(self, assignments: Iterable[tuple[str, Assignment]]) -> None:
        by_vehicle: dict[str, list[Assignment]] = defaultdict(list)
        for vehicle_id, a in assignments:
            by_vehicle[vehicle_id].append(a)
        self._by_vehicle = {v: sorted(rows, key=lambda a: a.first_ts) for v, rows in by_vehicle.items()}
        self._starts = {v: [a.first_ts for a in rows] for v, rows in self._by_vehicle.items()}

    def current(self, vehicle_id: str, header_ts: int) -> Assignment | None:
        """The vehicle's current trip at a snapshot: of the trips assigned to it
        then, the earliest scheduled (passed trips are no longer listed)."""
        rows = self._by_vehicle.get(vehicle_id)
        if not rows:
            return None
        live = [a for a in rows[: bisect_right(self._starts[vehicle_id], header_ts)] if a.last_ts >= header_ts]
        return min(live, key=lambda a: a.scheduled_start) if live else None


def load_assignments(
    conn: sqlite3.Connection, static: StaticGtfs, start_ts: float, end_ts: float
) -> TripAssignments:
    """Trip assignments from `trip_update` rows overlapping `[start_ts, end_ts]`."""
    spans: dict[tuple[str, str | None], float] = {}

    def scheduled_start(trip_id: str, service_date: str | None) -> float:
        key = (trip_id, service_date)
        if key not in spans:
            trip = static.trip(trip_id, service_date)
            span = static.trip_span(trip.version, trip_id) if trip is not None else None
            spans[key] = (
                schedule_ts(service_date, span[0]) if span is not None and service_date is not None else float("inf")
            )
        return spans[key]

    rows = conn.execute(
        "SELECT t.vehicle_id, t.trip_id, t.service_date, f.header_ts, l.header_ts FROM trip_update t "
        "JOIN feed_snapshot f ON f.id = t.snapshot_id JOIN feed_snapshot l ON l.id = t.last_seen_snapshot_id "
        "WHERE t.vehicle_id IS NOT NULL AND l.header_ts >= ? AND f.header_ts <= ?",
        (int(start_ts), int(end_ts)),
    ).fetchall()
    return TripAssignments(
        (vehicle_id, Assignment(trip_id, service_date, first, last, scheduled_start(trip_id, service_date)))
        for vehicle_id, trip_id, service_date, first, last in rows
    )


@dataclass(frozen=True)
class TripPing:
    """One position of a trajectory."""

    fix_ts: int
    lat: float
    lon: float
    raw_dist_ft: float  # projected distance along the shape
    offset_ft: float  # distance from the shape
    is_repeat: bool  # same position as the vehicle's previous fix
    dist_ft: float  # raw_dist_ft made non-decreasing in time (isotonic fit)


@dataclass(frozen=True)
class RemovedPing:
    fix_ts: int
    lat: float
    lon: float
    reason: str  # "off_route", "stale_repeat" or "backward_jump"


@dataclass(frozen=True)
class Trajectory:
    """One vehicle's run of one trip instance along its static shape."""

    trip_id: str
    service_date: str
    vehicle_id: str
    route_id: str
    geometry: ShapeGeometry = field(repr=False)
    pings: tuple[TripPing, ...]
    removed: tuple[RemovedPing, ...] = ()
    relabelled: int = 0  # positions moved here from the vehicle's reported trip id

    @property
    def start_ts(self) -> int:
        return self.pings[0].fix_ts

    @property
    def end_ts(self) -> int:
        return self.pings[-1].fix_ts

    @property
    def times(self) -> np.ndarray:
        return np.array([p.fix_ts for p in self.pings], dtype=float)

    @property
    def distances(self) -> np.ndarray:
        """The fitted (non-decreasing) distances."""
        return np.array([p.dist_ft for p in self.pings], dtype=float)


@dataclass
class Reconstruction:
    trajectories: list[Trajectory]
    #: Trajectories dropped whole, by reason.
    dropped: Counter[str] = field(default_factory=Counter)


def isotonic_fit(values: Sequence[float]) -> list[float]:
    """Least-squares non-decreasing fit to `values` (pool adjacent violators)."""
    blocks: list[list[float]] = []  # [mean, count]
    for value in values:
        blocks.append([value, 1.0])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            mean, count = blocks.pop()
            prev = blocks[-1]
            prev[0] = (prev[0] * prev[1] + mean * count) / (prev[1] + count)
            prev[1] += count
    fitted: list[float] = []
    for mean, count in blocks:
        fitted.extend([mean] * int(count))
    return fitted


@dataclass(frozen=True)
class _Labelled:
    sighting: Sighting
    trip_id: str
    service_date: str | None
    relabelled: bool


def _at_end_of(resolver: GeometryResolver, s: Sighting, max_offset_ft: float, zone_ft: float) -> bool:
    """Whether a position is within `zone_ft` of the end of the trip it reports."""
    if s.trip_id is None:
        return False
    geometry = resolver.for_trip(s.trip_id, s.service_date, s.fix_ts)
    if geometry is None:
        return False
    p = geometry.project(s.lat, s.lon, geometry.length_ft, max_offset_ft)
    return p.offset_ft <= max_offset_ft and p.dist_along_ft >= geometry.length_ft - zone_ft


def _label(
    sightings: Sequence[Sighting], assignments: TripAssignments | None, resolver: GeometryResolver,
    max_offset_ft: float, start_zone_ft: float,
) -> list[_Labelled]:
    """Stage 1: the trip each of one vehicle's positions belongs to.

    Where the trips feed's current trip differs from the reported one, the
    position goes to the current trip once the bus has been seen within
    `start_zone_ft` of that trip's start (the trips feed often moves a bus on
    while it's still approaching the terminus of its last trip), unless it's
    within `start_zone_ft` of the end of the reported trip (its arrival there).
    """
    out = []
    started: set[tuple[str, str | None]] = set()
    for s in sightings:
        current = assignments.current(s.vehicle_id, s.header_ts) if assignments is not None else None
        if current is not None and current.trip_id != s.trip_id:
            key = (current.trip_id, current.service_date)
            if key not in started:
                geometry = resolver.for_trip(current.trip_id, current.service_date, s.fix_ts)
                if geometry is not None:
                    p = geometry.project(s.lat, s.lon, 0.0, max_offset_ft)
                    if p.offset_ft <= max_offset_ft and p.dist_along_ft <= start_zone_ft:
                        started.add(key)
            if key in started and not _at_end_of(resolver, s, max_offset_ft, start_zone_ft):
                out.append(_Labelled(s, current.trip_id, current.service_date, s.trip_id is not None))
                continue
        if s.trip_id is not None:
            out.append(_Labelled(s, s.trip_id, s.service_date, False))
    return out


def _drop_stale_repeats(pings: list[TripPing], stale_speed_ftps: float) -> tuple[list[TripPing], list[TripPing]]:
    """Stage 4: (kept, removed)."""
    kept: list[TripPing] = []
    removed: list[TripPing] = []
    run: list[TripPing] = []
    for ping in pings:
        if ping.is_repeat:
            run.append(ping)
            continue
        if run:
            last = run[-1]
            elapsed = ping.fix_ts - last.fix_ts
            if elapsed > 0 and (ping.raw_dist_ft - last.raw_dist_ft) / elapsed > stale_speed_ftps:
                removed.extend(run)
            else:
                kept.extend(run)
            run = []
        kept.append(ping)
    kept.extend(run)  # a trailing run has nothing after it to judge by
    return kept, removed


def _drop_backward_jumps(pings: list[TripPing], jump_ft: float) -> tuple[list[TripPing], list[TripPing]]:
    """Stage 5: (kept, removed). A ping more than `jump_ft` behind the last
    kept one and the next one is removed."""
    kept: list[TripPing] = []
    removed: list[TripPing] = []
    for i, ping in enumerate(pings):
        behind_prev = kept and ping.raw_dist_ft < kept[-1].raw_dist_ft - jump_ft
        # ...and the next one recovers to where the bus was.
        behind_next = (
            kept and i + 1 < len(pings) and ping.raw_dist_ft < pings[i + 1].raw_dist_ft - jump_ft
            and pings[i + 1].raw_dist_ft >= kept[-1].raw_dist_ft - jump_ft
        )
        if behind_prev and behind_next:
            removed.append(ping)
        else:
            kept.append(ping)
    return kept, removed


def _restart_at(pings: Sequence[TripPing], restart_ft: float) -> int | None:
    """Index of the first ping more than `restart_ft` behind the furthest before it."""
    furthest = -np.inf
    for i, ping in enumerate(pings):
        if ping.raw_dist_ft < furthest - restart_ft:
            return i
        furthest = max(furthest, ping.raw_dist_ft)
    return None


def _cut_before_restarts(
    pings: list[TripPing], restart_ft: float
) -> tuple[list[TripPing], list[TripPing]] | None:
    """Stage 6: (kept, removed), or None if the trajectory is ambiguous.

    Where the distance falls back by more than `restart_ft`, everything before
    the lowest point after the fall is removed, provided that earlier part
    made less than `restart_ft` of forward progress (a bus approaching its
    start along the route under the trip's id). If it made more, the bus ran
    part of the route twice and which run was the trip is ambiguous.
    """
    removed: list[TripPing] = []
    while (i := _restart_at(pings, restart_ft)) is not None:
        before = np.array([p.raw_dist_ft for p in pings[:i]])
        if (before - np.minimum.accumulate(before)).max() >= restart_ft:  # forward progress
            return None
        tail = [p.raw_dist_ft for p in pings[i:]]
        cut = i + int(np.argmin(tail))
        removed += pings[:cut]
        pings = pings[cut:]
    return pings, removed


def _longest_inner_run(on_route: Sequence[bool]) -> int:
    """Longest run of False strictly between two Trues."""
    idx = [i for i, ok in enumerate(on_route) if ok]
    return max((b - a - 1 for a, b in zip(idx, idx[1:])), default=0)


def _removed(pings: Iterable[TripPing], reason: str) -> list[RemovedPing]:
    return [RemovedPing(p.fix_ts, p.lat, p.lon, reason) for p in pings]


def _build_trajectory(
    vehicle_id: str,
    trip_id: str,
    service_date: str,
    labelled: Sequence[_Labelled],
    resolver: GeometryResolver,
    dropped: Counter[str],
    max_offset_ft: float,
    max_off_route_run: int,
    stale_speed_ftps: float,
    jump_ft: float,
    restart_ft: float,
    min_pings: int,
) -> Trajectory | None:
    trip = resolver.static.trip(trip_id, service_date)
    if trip is None:
        dropped["unknown_trip"] += 1
        return None
    # Stage 2: detours.
    first, last = labelled[0].sighting.fix_ts, labelled[-1].sighting.fix_ts
    if any(m.overlaps(first, last) and m.applies_on(service_date) and trip_id in m.trips
           for m in resolver.modifications):
        dropped["detoured"] += 1
        return None
    geometry = resolver.static_geometry(trip)
    if geometry is None:
        dropped["no_geometry"] += 1
        return None

    # Stage 3: project in order, each near the previous on-route position.
    near = 0.0  # a trip starts at its shape's start
    pings: list[TripPing] = []
    removed: list[RemovedPing] = []
    on_route: list[bool] = []
    previous: tuple[float, float] | None = None
    for item in labelled:
        s = item.sighting
        projection = geometry.project(s.lat, s.lon, near, max_offset_ft)
        is_repeat = previous == (s.lat, s.lon)
        previous = (s.lat, s.lon)
        ok = projection.offset_ft <= max_offset_ft
        on_route.append(ok)
        if not ok:
            removed.append(RemovedPing(s.fix_ts, s.lat, s.lon, "off_route"))
            continue
        near = projection.dist_along_ft
        pings.append(TripPing(s.fix_ts, s.lat, s.lon, near, projection.offset_ft, is_repeat, near))
    if _longest_inner_run(on_route) >= max_off_route_run:
        dropped["off_route_run"] += 1
        return None

    pings, stale = _drop_stale_repeats(pings, stale_speed_ftps)
    removed += _removed(stale, "stale_repeat")
    pings, jumps = _drop_backward_jumps(pings, jump_ft)
    removed += _removed(jumps, "backward_jump")
    cut = _cut_before_restarts(pings, restart_ft)
    if cut is None:
        dropped["restart"] += 1
        return None
    pings, before = cut
    removed += _removed(before, "before_restart")
    if len(pings) < min_pings:
        dropped["too_few_pings"] += 1
        return None

    fitted = isotonic_fit([p.raw_dist_ft for p in pings])
    return Trajectory(
        trip_id, service_date, vehicle_id, trip.route_id, geometry,
        tuple(replace(p, dist_ft=d) for p, d in zip(pings, fitted)),
        tuple(sorted(removed, key=lambda r: r.fix_ts)),
        sum(item.relabelled for item in labelled),
    )


def reconstruct(
    sightings: Iterable[Sighting],
    resolver: GeometryResolver,
    assignments: TripAssignments | None = None,
    max_offset_ft: float = DEFAULT_MAX_OFFSET_FT,
    max_off_route_run: int = MAX_OFF_ROUTE_RUN,
    stale_speed_ftps: float = STALE_SPEED_FTPS,
    jump_ft: float = BACKWARD_JUMP_FT,
    restart_ft: float = RESTART_FT,
    min_pings: int = MIN_PINGS,
    start_zone_ft: float = START_ZONE_FT,
) -> Reconstruction:
    """Clean trajectories from positions (any order), through the stages above.

    Positions without a trip, or whose trip instance has no service date, are
    ignored. Returns trajectories sorted by vehicle then start time.
    """
    by_vehicle: dict[str, list[Sighting]] = defaultdict(list)
    for s in sightings:
        by_vehicle[s.vehicle_id].append(s)
    result = Reconstruction([])
    for vehicle_id, rows in sorted(by_vehicle.items()):
        rows.sort(key=lambda s: s.fix_ts)
        groups: dict[tuple[str, str], list[_Labelled]] = defaultdict(list)
        for item in _label(rows, assignments, resolver, max_offset_ft, start_zone_ft):
            if item.service_date is not None:
                groups[(item.trip_id, item.service_date)].append(item)
        for (trip_id, service_date), labelled in groups.items():
            trajectory = _build_trajectory(
                vehicle_id, trip_id, service_date, labelled, resolver, result.dropped, max_offset_ft,
                max_off_route_run, stale_speed_ftps, jump_ft, restart_ft, min_pings,
            )
            if trajectory is not None:
                result.trajectories.append(trajectory)
    result.trajectories.sort(key=lambda t: (t.vehicle_id, t.start_ts))
    return result


def reconstruct_from_db(
    conn: sqlite3.Connection, resolver: GeometryResolver, start_ts: float, end_ts: float
) -> Reconstruction:
    """`reconstruct` (default thresholds) over the positions and trip
    assignments stored for a time range."""
    return reconstruct(
        load_sightings(conn, start_ts, end_ts),
        resolver,
        load_assignments(conn, resolver.static, start_ts, end_ts),
    )


# ------------------------------------------------------------------ stop crossings


@dataclass(frozen=True)
class StopCrossing:
    """When a trajectory reached and left a stop, interpolated from its fitted curve.

    `arrival_ts` is when it came within `STOP_ZONE_FT` of the stop, `departure_ts`
    when it went beyond that past it, and `passed_ts` when it reached the stop
    itself. Each is None when not bracketed by observations (the first stop
    has no arrival, the last no departure, and stops before the first or after
    the last position have neither).
    """

    stop_sequence: int | None
    stop_id: str
    dist_ft: float
    arrival_ts: float | None
    passed_ts: float | None
    departure_ts: float | None

    @property
    def dwell_s(self) -> float | None:
        if self.arrival_ts is None or self.departure_ts is None:
            return None
        return self.departure_ts - self.arrival_ts


def _first_time_reaching(times: np.ndarray, dists: np.ndarray, d: float, strict: bool) -> float | None:
    """First time the piecewise-linear non-decreasing curve reaches `d` (exceeds it if strict).
    None unless an earlier observation is below it."""
    hits = np.flatnonzero(dists > d if strict else dists >= d)
    if not len(hits) or hits[0] == 0:
        return None
    i = int(hits[0])
    t0, t1, d0, d1 = times[i - 1], times[i], dists[i - 1], dists[i]
    return float(t0 + (t1 - t0) * (d - d0) / (d1 - d0))


def stop_crossings(trajectory: Trajectory, zone_ft: float = STOP_ZONE_FT) -> list[StopCrossing]:
    """Arrival, passing and departure times at each of the trip's stops."""
    times, dists = trajectory.times, trajectory.distances
    out = []
    stops = trajectory.geometry.stops
    for k, stop in enumerate(stops):
        d = stop.dist_along_ft
        arrival = None if k == 0 else _first_time_reaching(times, dists, d - zone_ft, strict=False)
        departure = None if k == len(stops) - 1 else _first_time_reaching(times, dists, d + zone_ft, strict=True)
        out.append(StopCrossing(
            stop.stop_sequence, stop.stop_id, d, arrival,
            _first_time_reaching(times, dists, d, strict=False), departure,
        ))
    return out


def agency_stop_passed(conn: sqlite3.Connection, trip_id: str, service_date: str | None) -> dict[int, int]:
    """stop_sequence -> header time of the first trips snapshot whose update for
    the trip instance no longer listed it (so the agency considered it passed)."""
    passed: dict[int, int] = {}
    previous: int | None = None
    for first, header_ts in conn.execute(
        "SELECT t.first_stop_sequence, f.header_ts FROM trip_update t JOIN feed_snapshot f "
        "ON f.id = t.snapshot_id WHERE t.trip_id = ? AND t.service_date IS ? AND t.first_stop_sequence "
        "IS NOT NULL ORDER BY f.header_ts",
        (trip_id, service_date),
    ):
        if previous is not None:
            for seq in range(previous, first):
                passed.setdefault(seq, header_ts)
        previous = first if previous is None else max(previous, first)
    return passed
