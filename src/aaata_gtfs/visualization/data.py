"""Turning stored positions into plottable paths."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlmodel import Session

from aaata_gtfs.queries import PositionRow, active_positions

#: Consecutive positions of a bus further apart than this start a new path, so
#: a bus that leaves service and comes back later isn't joined by a straight line.
DEFAULT_MAX_GAP_S = 15 * 60


@dataclass
class BusPath:
    """A single bus's track on one route (or out of service), ordered by fix time."""

    route_id: str | None  # None: not in service (no trip)
    vehicle_id: str
    sightings: list[PositionRow] = field(default_factory=list)

    @property
    def coordinates(self) -> list[tuple[float, float]]:
        """(lat, lon) pairs in time order."""
        return [(p.lat, p.lon) for p in self.sightings]

    @property
    def start(self) -> int:
        return self.sightings[0].fix_ts

    @property
    def end(self) -> int:
        return self.sightings[-1].fix_ts


def trace_bus_paths(positions: Iterable[PositionRow], max_gap_s: float = DEFAULT_MAX_GAP_S) -> list[BusPath]:
    """Group positions into time-ordered paths, one per bus per stretch of service.

    A path ends where two consecutive positions are more than `max_gap_s`
    apart, or where the bus changes route (or goes in or out of service).

    Returns:
        Paths sorted by route (out of service last), vehicle, then start time.
    """
    by_vehicle: dict[str, list[PositionRow]] = defaultdict(list)
    for p in positions:
        by_vehicle[p.vehicle_id].append(p)

    paths: list[BusPath] = []
    for vehicle_id, rows in by_vehicle.items():
        rows.sort(key=lambda p: p.fix_ts)
        current: BusPath | None = None
        for p in rows:
            route_id = p.route_id if p.trip_id is not None else None
            if current is None or route_id != current.route_id or p.fix_ts - current.end > max_gap_s:
                current = BusPath(route_id, vehicle_id)
                paths.append(current)
            current.sightings.append(p)
    paths.sort(key=lambda path: (path.route_id is None, path.route_id or "", path.vehicle_id, path.start))
    return paths


@dataclass(frozen=True)
class LatestPosition:
    """A vehicle's latest fix as of a vehicles snapshot."""

    position: PositionRow
    snapshot_ts: int  # the snapshot's header time

    @property
    def age_s(self) -> int:
        return self.snapshot_ts - self.position.fix_ts

    @property
    def route_id(self) -> str | None:
        return self.position.route_id if self.position.trip_id is not None else None


def latest_positions(
    session: Session, at: float, route_ids: Iterable[str] | None = None
) -> dict[str | None, list[LatestPosition]]:
    """Each route's active vehicles (see `queries.active_positions`) at the
    latest vehicles snapshot at or before `at`, keyed by route (None: not in service)."""
    active = active_positions(session, at)
    if active is None:
        return {}
    header_ts, rows = active
    wanted = set(route_ids) if route_ids is not None else None
    out: dict[str | None, list[LatestPosition]] = defaultdict(list)
    for p in rows:
        latest = LatestPosition(p, header_ts)
        if wanted is not None and latest.route_id not in wanted:
            continue
        out[latest.route_id].append(latest)
    return dict(out)
