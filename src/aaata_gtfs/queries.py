"""Read helpers for analysis and visualization."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Unpack

from sqlalchemy import ColumnCollection, Select, and_, func, select
from sqlmodel import Session

from aaata_gtfs import db
from aaata_gtfs import models as m

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


_vp = m.columns(m.VehiclePosition)
_fs = m.columns(m.FeedSnapshot)
_POSITION_COLUMNS = (
    _vp.vehicle_id, _vp.fix_ts, _vp.lat, _vp.lon, _vp.bearing, _vp.speed_mps, _vp.trip_id, _vp.route_id,
    _vp.service_date, _vp.snapshot_id,
)


def positions(
    session: Session,
    start_ts: float,
    end_ts: float,
    route_ids: Iterable[str] | None = None,
) -> list[PositionRow]:
    """Positions with `start_ts <= fix_ts <= end_ts`, ordered by vehicle then fix time.

    With `route_ids`, only positions on those routes (so none out of service).
    """
    query = select(*_POSITION_COLUMNS).where(_vp.fix_ts.between(int(start_ts), int(end_ts)))
    if route_ids is not None:
        query = query.where(_vp.route_id.in_(list(route_ids)))
    return [PositionRow(*row) for row in db.rows(session, query.order_by(_vp.vehicle_id, _vp.fix_ts))]


def data_time_range(session: Session) -> tuple[int, int] | None:
    """(earliest, latest) fix time in the DB."""
    row = db.rows(session, select(func.min(_vp.fix_ts), func.max(_vp.fix_ts))).first()
    return (int(row[0]), int(row[1])) if row and row[0] is not None else None


def latest_vehicles_snapshot(session: Session, at_or_before: float) -> tuple[int, int] | None:
    """(snapshot_id, header_ts) of the latest vehicles snapshot at or before a time."""
    row = db.rows(
        session,
        select(_fs.id, _fs.header_ts).where(_fs.feed == "vehicles", _fs.header_ts <= int(at_or_before))
        .order_by(_fs.header_ts.desc()).limit(1),
    ).first()
    return (int(row[0]), int(row[1])) if row else None


def _latest_fixes(session: Session, snapshot_id: int, header_ts: int) -> list[PositionRow]:
    """Each vehicle present in a vehicles snapshot, at its latest fix as of then."""
    seen = m.columns(m.VehicleSeen)
    positions_p = m.table(m.VehiclePosition).alias("p")
    p = positions_p.c
    latest = (
        select(seen.vehicle_id, func.max(p.fix_ts).label("fix_ts"))
        .join(positions_p, and_(p.vehicle_id == seen.vehicle_id, p.fix_ts <= header_ts))
        .where(seen.first_snapshot_id <= snapshot_id, seen.last_snapshot_id >= snapshot_id)
        .group_by(seen.vehicle_id).subquery()
    )
    return [
        PositionRow(*row)
        for row in db.rows(
            session,
            select(*_POSITION_COLUMNS)
            .join(latest, and_(_vp.vehicle_id == latest.c.vehicle_id, _vp.fix_ts == latest.c.fix_ts))
            .order_by(_vp.vehicle_id)
        )
    ]


def active_positions(session: Session, at: float) -> tuple[int, list[PositionRow]] | None:
    """(header_ts, positions) for the vehicles active at the latest vehicles
    snapshot at or before `at`: present in it, with a fix within
    `ACTIVE_MAX_AGE_S` of its header time (so stale, non-reporting buses are dropped)."""
    snapshot = latest_vehicles_snapshot(session, at)
    if snapshot is None:
        return None
    snapshot_id, header_ts = snapshot
    fixes = [p for p in _latest_fixes(session, snapshot_id, header_ts) if header_ts - p.fix_ts <= ACTIVE_MAX_AGE_S]
    return header_ts, fixes


def active_vehicles(session: Session, at: float) -> set[str]:
    """Vehicles active at the latest vehicles snapshot at or before `at` (see `active_positions`)."""
    active = active_positions(session, at)
    return {p.vehicle_id for p in active[1]} if active else set()


def snapshot_near(session: Session, feed: str, t: float) -> tuple[int, int] | None:
    """(snapshot_id, header_ts) of the feed's snapshot with header time nearest `t`."""
    snapshots = select(_fs.id, _fs.header_ts).where(_fs.feed == feed)
    before = db.rows(session, snapshots.where(_fs.header_ts <= int(t)).order_by(_fs.header_ts.desc()).limit(1)).first()
    after = db.rows(session, snapshots.where(_fs.header_ts > int(t)).order_by(_fs.header_ts).limit(1)).first()
    found = [r for r in (before, after) if r is not None]
    if not found:
        return None
    best = min(found, key=lambda r: abs(r[1] - t))
    return int(best[0]), int(best[1])


def _versions_query(t: ColumnCollection[str, Any], *columns: str) -> Select[Unpack[tuple[Any, ...]]]:
    """id, the given columns, and the header times of the first and last
    snapshots, of every row of a versioned table, oldest first."""
    first, last = m.table(m.FeedSnapshot).alias("first"), m.table(m.FeedSnapshot).alias("last")
    return (
        select(t.id, *(t[c] for c in columns), first.c.header_ts, last.c.header_ts)
        .join(first, first.c.id == t.first_snapshot_id)
        .join(last, last.c.id == t.last_snapshot_id)
        .order_by(first.c.header_ts, t.id)
    )


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


def all_trip_modifications(session: Session) -> list[ModificationVersion]:
    """Every stored trip modifications version, oldest first."""
    trips: dict[int, dict[str, str | None]] = {}
    mt = m.columns(m.TripModificationsTrip)
    for version_id, trip_id, shape_id in db.rows(session, select(mt.version_id, mt.trip_id, mt.shape_id)):
        trips.setdefault(version_id, {})[trip_id] = shape_id
    dates: dict[int, set[str]] = {}
    md = m.columns(m.TripModificationsDate)
    for version_id, service_date in db.rows(session, select(md.version_id, md.service_date)):
        dates.setdefault(version_id, set()).add(service_date)
    return [
        ModificationVersion(row[0], row[1], row[2], row[3], row[4], trips.get(row[0], {}),
                            frozenset(dates.get(row[0], ())))
        for row in db.rows(
            session, _versions_query(m.columns(m.TripModificationsVersion), "modifications_id", "pattern_id")
        )
    ]


def trip_modifications_at(session: Session, t: float) -> list[ModificationVersion]:
    """Trip modifications (detours) in effect at `t`."""
    return [m for m in all_trip_modifications(session) if m.in_effect(t)]


@dataclass(frozen=True)
class RtShapeVersion:
    id: int
    shape_id: str
    encoded_polyline: str
    first_ts: int
    last_ts: int

    def in_effect(self, t: float, grace_s: float = EFFECT_GRACE_S) -> bool:
        return self.first_ts - grace_s <= t <= self.last_ts + grace_s


def all_rt_shapes(session: Session) -> list[RtShapeVersion]:
    """Every stored RT shape version, oldest first."""
    return [
        RtShapeVersion(*row)
        for row in db.rows(session, _versions_query(m.columns(m.RtShapeVersion), "shape_id", "encoded_polyline"))
    ]


def rt_shape_at(session: Session, shape_id: str, t: float) -> RtShapeVersion | None:
    """The version of an RT shape in effect at `t` (the newest, if several are)."""
    found = [s for s in all_rt_shapes(session) if s.shape_id == shape_id and s.in_effect(t)]
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


def bustime_route_versions(session: Session, route_id: str | None = None) -> list[BusTimeRouteVersion]:
    """Stored BusTime route versions, oldest first."""
    b = m.columns(m.BusTimeRouteVersion)
    query = select(b.id, b.route_id, b.route_version, b.content_sha256, b.fetched_at, b.last_checked_at, b.blob)
    if route_id is not None:
        query = query.where(b.route_id == route_id)
    return [BusTimeRouteVersion(*row) for row in db.rows(session, query.order_by(b.fetched_at, b.id))]
