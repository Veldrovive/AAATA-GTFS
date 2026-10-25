"""Read helpers for analysis and visualization."""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

#: RT entities are "in effect" this long either side of the snapshots they were seen in.
EFFECT_GRACE_S = 60
#: A vehicle whose latest fix is older than this at a snapshot has stopped reporting.
ACTIVE_MAX_AGE_S = 120


@dataclass(frozen=True)
class PositionRow:
    vehicle_id: str
    fix_ts: int
    lat: float
    lon: float
    bearing: float | None
    speed_mps: float | None
    trip_id: str | None
    route_id: str | None
    service_date: str | None
    snapshot_id: int


_POSITION_COLUMNS = (
    "vehicle_id, fix_ts, lat, lon, bearing, speed_mps, trip_id, route_id, service_date, snapshot_id"
)


def positions(
    conn: sqlite3.Connection,
    start_ts: float,
    end_ts: float,
    route_ids: Iterable[str] | None = None,
) -> list[PositionRow]:
    """Positions with `start_ts <= fix_ts <= end_ts`, ordered by vehicle then fix time.

    With `route_ids`, only positions on those routes (so none out of service).
    """
    sql = f"SELECT {_POSITION_COLUMNS} FROM vehicle_position WHERE fix_ts BETWEEN ? AND ?"
    params: list[object] = [int(start_ts), int(end_ts)]
    if route_ids is not None:
        ids = list(route_ids)
        sql += f" AND route_id IN ({', '.join('?' * len(ids))})"
        params += ids
    sql += " ORDER BY vehicle_id, fix_ts"
    return [PositionRow(*row) for row in conn.execute(sql, params)]


def data_time_range(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """(earliest, latest) fix time in the DB."""
    row = conn.execute("SELECT MIN(fix_ts), MAX(fix_ts) FROM vehicle_position").fetchone()
    return (int(row[0]), int(row[1])) if row and row[0] is not None else None


def latest_vehicles_snapshot(conn: sqlite3.Connection, at_or_before: float) -> tuple[int, int] | None:
    """(snapshot_id, header_ts) of the latest vehicles snapshot at or before a time."""
    row = conn.execute(
        "SELECT id, header_ts FROM feed_snapshot WHERE feed = 'vehicles' AND header_ts <= ? "
        "ORDER BY header_ts DESC LIMIT 1",
        (int(at_or_before),),
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else None


def _latest_fixes(conn: sqlite3.Connection, snapshot_id: int, header_ts: int) -> list[PositionRow]:
    """Each vehicle present in a vehicles snapshot, at its latest fix as of then."""
    return [
        PositionRow(*row)
        for row in conn.execute(
            f"SELECT {_POSITION_COLUMNS} FROM vehicle_position vp "
            "JOIN (SELECT s.vehicle_id, MAX(p.fix_ts) AS fix_ts FROM vehicle_seen s "
            "      JOIN vehicle_position p ON p.vehicle_id = s.vehicle_id AND p.fix_ts <= ? "
            "      WHERE s.first_snapshot_id <= ? AND s.last_snapshot_id >= ? GROUP BY s.vehicle_id) m "
            "USING (vehicle_id, fix_ts) ORDER BY vehicle_id",
            (header_ts, snapshot_id, snapshot_id),
        )
    ]


def active_positions(conn: sqlite3.Connection, at: float) -> tuple[int, list[PositionRow]] | None:
    """(header_ts, positions) for the vehicles active at the latest vehicles
    snapshot at or before `at`: present in it, with a fix within
    `ACTIVE_MAX_AGE_S` of its header time (so stale, non-reporting buses are dropped)."""
    snapshot = latest_vehicles_snapshot(conn, at)
    if snapshot is None:
        return None
    snapshot_id, header_ts = snapshot
    fixes = [p for p in _latest_fixes(conn, snapshot_id, header_ts) if header_ts - p.fix_ts <= ACTIVE_MAX_AGE_S]
    return header_ts, fixes


def active_vehicles(conn: sqlite3.Connection, at: float) -> set[str]:
    """Vehicles active at the latest vehicles snapshot at or before `at` (see `active_positions`)."""
    active = active_positions(conn, at)
    return {p.vehicle_id for p in active[1]} if active else set()


@dataclass(frozen=True)
class StopTimeRow:
    stop_sequence: int
    stop_id: str
    arrival_time: int | None
    departure_time: int | None
    arrival_delay: int | None
    departure_delay: int | None
    schedule_relationship: str | None
    observed_ts: int
    snapshot_id: int


def predictions_at(
    conn: sqlite3.Connection, trip_id: str, service_date: str | None, snapshot_id: int
) -> list[StopTimeRow]:
    """What the agency predicted for each remaining stop of a trip instance as
    of a snapshot: the latest prediction per stop at or before it, for stops at
    or after the `first_stop_sequence` of the trip update in effect then."""
    tu = conn.execute(
        "SELECT first_stop_sequence FROM trip_update WHERE trip_id = ? AND service_date IS ? "
        "AND snapshot_id <= ? ORDER BY snapshot_id DESC, id DESC LIMIT 1",
        (trip_id, service_date, snapshot_id),
    ).fetchone()
    if tu is None:
        return []
    first = tu[0]
    latest: dict[int, StopTimeRow] = {}
    for row in conn.execute(
        "SELECT stop_sequence, stop_id, arrival_time, departure_time, arrival_delay, departure_delay, "
        "schedule_relationship, observed_ts, snapshot_id FROM stop_time_prediction "
        "WHERE trip_id = ? AND service_date IS ? AND snapshot_id <= ? ORDER BY stop_sequence, snapshot_id",
        (trip_id, service_date, snapshot_id),
    ):
        latest[row[0]] = StopTimeRow(*row)
    return [r for seq, r in sorted(latest.items()) if first is None or seq >= first]


def snapshot_near(conn: sqlite3.Connection, feed: str, t: float) -> tuple[int, int] | None:
    """(snapshot_id, header_ts) of the feed's snapshot with header time nearest `t`."""
    before = conn.execute(
        "SELECT id, header_ts FROM feed_snapshot WHERE feed = ? AND header_ts <= ? ORDER BY header_ts DESC LIMIT 1",
        (feed, int(t)),
    ).fetchone()
    after = conn.execute(
        "SELECT id, header_ts FROM feed_snapshot WHERE feed = ? AND header_ts > ? ORDER BY header_ts LIMIT 1",
        (feed, int(t)),
    ).fetchone()
    found = [r for r in (before, after) if r is not None]
    if not found:
        return None
    best = min(found, key=lambda r: abs(r[1] - t))
    return int(best[0]), int(best[1])


@dataclass(frozen=True)
class ModificationVersion:
    id: int
    modifications_id: str
    pattern_id: str | None
    first_ts: int  # header time of the first snapshot it was seen in
    last_ts: int  # ...and of the last
    trips: dict[str, str | None]  # trip_id -> shape_id
    service_dates: frozenset[str]

    def in_effect(self, t: float, grace_s: float = EFFECT_GRACE_S) -> bool:
        return self.first_ts - grace_s <= t <= self.last_ts + grace_s

    def overlaps(self, start: float, end: float, grace_s: float = EFFECT_GRACE_S) -> bool:
        return self.first_ts - grace_s <= end and start <= self.last_ts + grace_s

    def applies_on(self, service_date: str | None) -> bool:
        return service_date is None or not self.service_dates or service_date in self.service_dates


def all_trip_modifications(conn: sqlite3.Connection) -> list[ModificationVersion]:
    """Every stored trip modifications version, oldest first."""
    trips: dict[int, dict[str, str | None]] = {}
    for version_id, trip_id, shape_id in conn.execute(
        "SELECT version_id, trip_id, shape_id FROM trip_modifications_trip"
    ):
        trips.setdefault(version_id, {})[trip_id] = shape_id
    dates: dict[int, set[str]] = {}
    for version_id, service_date in conn.execute("SELECT version_id, service_date FROM trip_modifications_date"):
        dates.setdefault(version_id, set()).add(service_date)
    return [
        ModificationVersion(row[0], row[1], row[2], row[3], row[4], trips.get(row[0], {}),
                            frozenset(dates.get(row[0], ())))
        for row in conn.execute(
            "SELECT m.id, m.modifications_id, m.pattern_id, f.header_ts, l.header_ts "
            "FROM trip_modifications_version m JOIN feed_snapshot f ON f.id = m.first_snapshot_id "
            "JOIN feed_snapshot l ON l.id = m.last_snapshot_id ORDER BY f.header_ts, m.id"
        )
    ]


def trip_modifications_at(conn: sqlite3.Connection, t: float) -> list[ModificationVersion]:
    """Trip modifications (detours) in effect at `t`."""
    return [m for m in all_trip_modifications(conn) if m.in_effect(t)]


@dataclass(frozen=True)
class RtShapeVersion:
    id: int
    shape_id: str
    encoded_polyline: str
    first_ts: int
    last_ts: int

    def in_effect(self, t: float, grace_s: float = EFFECT_GRACE_S) -> bool:
        return self.first_ts - grace_s <= t <= self.last_ts + grace_s


def all_rt_shapes(conn: sqlite3.Connection) -> list[RtShapeVersion]:
    """Every stored RT shape version, oldest first."""
    return [
        RtShapeVersion(*row)
        for row in conn.execute(
            "SELECT r.id, r.shape_id, r.encoded_polyline, f.header_ts, l.header_ts FROM rt_shape_version r "
            "JOIN feed_snapshot f ON f.id = r.first_snapshot_id JOIN feed_snapshot l ON l.id = r.last_snapshot_id "
            "ORDER BY f.header_ts, r.id"
        )
    ]


def rt_shape_at(conn: sqlite3.Connection, shape_id: str, t: float) -> RtShapeVersion | None:
    """The version of an RT shape in effect at `t` (the newest, if several are)."""
    found = [s for s in all_rt_shapes(conn) if s.shape_id == shape_id and s.in_effect(t)]
    return found[-1] if found else None


@dataclass(frozen=True)
class BusTimeRouteVersion:
    id: int
    route_id: str
    route_version: str
    content_sha256: str
    fetched_at: float
    last_checked_at: float
    blob: str


def bustime_route_versions(conn: sqlite3.Connection, route_id: str | None = None) -> list[BusTimeRouteVersion]:
    """Stored BusTime route versions, oldest first."""
    sql = (
        "SELECT id, route_id, route_version, content_sha256, fetched_at, last_checked_at, blob "
        "FROM bustime_route_version"
    )
    params: tuple[str, ...] = ()
    if route_id is not None:
        sql += " WHERE route_id = ?"
        params = (route_id,)
    return [BusTimeRouteVersion(*row) for row in conn.execute(sql + " ORDER BY fetched_at, id", params)]
