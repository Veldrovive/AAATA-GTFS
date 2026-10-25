"""
Sanity-checks linear referencing: how far in-service positions are from their
trip's shape, per route, and how our stop distances along each static shape in
use (stop spurs cut) compare with the agency's shape_dist_traveled.
"""
import argparse
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import numpy as np

from aaata_gtfs import db
from aaata_gtfs.config import DATA_PATH, LOCAL_TZ, db_path
from aaata_gtfs.geometry import DEFAULT_MAX_OFFSET_FT, snap_sightings
from aaata_gtfs.queries import PositionRow, data_time_range, positions
from aaata_gtfs.shapes import GeometryResolver
from aaata_gtfs.static_gtfs import StaticGtfs, StaticTrip


def parse_time(value: str) -> float:
    t = datetime.fromisoformat(value)
    return (t if t.tzinfo else t.replace(tzinfo=LOCAL_TZ)).timestamp()


def route_key(route_id: str) -> tuple[bool, str]:
    return (not route_id.isdigit(), route_id.zfill(6))


def table(rows: Sequence[Sequence[object]], headers: tuple[str, ...]) -> str:
    cells = [headers, *[tuple(str(c) for c in row) for row in rows]]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    out = []
    for j, row in enumerate(cells):
        out.append("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
        if j == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check snapping offsets and stop distances along shapes.")
    parser.add_argument("--data-path", type=Path, default=DATA_PATH, help=f"Data directory (default: {DATA_PATH})")
    parser.add_argument("--start", type=parse_time, help="ISO-8601 (local if naive; default: 24 h before --end)")
    parser.add_argument("--end", type=parse_time, help="ISO-8601 (local if naive; default: the latest data)")
    parser.add_argument("--max-offset-ft", type=float, default=DEFAULT_MAX_OFFSET_FT,
                        help=f"Offsets over this count as off-route (default: {DEFAULT_MAX_OFFSET_FT:g})")
    args = parser.parse_args()

    path = db_path(args.data_path)
    if not path.exists():
        sys.exit(f"no database at {path}")
    conn = db.connect(path, read_only=True)
    data_range = data_time_range(conn)
    if data_range is None:
        sys.exit("no positions in the database")
    end = args.end if args.end is not None else data_range[1]
    start = args.start if args.start is not None else end - 86400

    static = StaticGtfs(conn)
    resolver = GeometryResolver(conn, static, args.data_path)
    by_vehicle: dict[str, list[PositionRow]] = defaultdict(list)
    for p in positions(conn, start, end):
        if p.trip_id is not None:
            by_vehicle[p.vehicle_id].append(p)

    offsets: dict[str, list[float]] = defaultdict(list)
    unresolved: dict[str, int] = defaultdict(int)
    trips_in_use: dict[tuple[str, str], StaticTrip] = {}
    for rows in by_vehicle.values():
        for ping in snap_sightings(rows, resolver, args.max_offset_ft):
            route_id = ping.position.route_id or "?"
            if ping.projection is None:
                unresolved[route_id] += 1
                continue
            offsets[route_id].append(ping.projection.offset_ft)
            trip = static.trip(ping.position.trip_id or "", ping.position.service_date)
            if trip is not None and trip.shape_id is not None:
                trips_in_use.setdefault((trip.version, trip.shape_id), trip)

    print(f"Window: {datetime.fromtimestamp(start, LOCAL_TZ):%Y-%m-%d %H:%M} – "
          f"{datetime.fromtimestamp(end, LOCAL_TZ):%Y-%m-%d %H:%M %Z}\n")
    print("Offsets of in-service positions from their trip's shape (ft)")
    rows_out = []
    for route_id in sorted(set(offsets) | set(unresolved), key=route_key):
        a = np.array(offsets.get(route_id, []), dtype=float)
        if len(a):
            p50, p90, p99 = np.percentile(a, [50, 90, 99])
            over = float((a > args.max_offset_ft).mean())
            rows_out.append((route_id, len(a), f"{p50:.1f}", f"{p90:.1f}", f"{p99:.1f}", f"{over:.1%}",
                             unresolved.get(route_id, 0)))
        else:
            rows_out.append((route_id, 0, "-", "-", "-", "-", unresolved.get(route_id, 0)))
    print(table(rows_out, ("route", "pings", "p50", "p90", "p99", f">{args.max_offset_ft:g} ft", "no shape")))

    print("\nStop distances along static shapes in use: ours (spurs cut) minus the agency's (ft)")
    rows_out: list[tuple[object, ...]] = []
    for (version, shape_id), trip in sorted(trips_in_use.items(), key=lambda kv: (route_key(kv[1].route_id), kv[0])):
        geometry = resolver.static_geometry(trip)
        if geometry is None:
            continue
        diffs = []
        for stop in geometry.stops:
            agency = geometry.agency_dist_ft(stop.stop_sequence) if stop.stop_sequence is not None else None
            if agency is not None:
                diffs.append(stop.dist_along_ft - agency)
        if not diffs:
            rows_out.append((trip.route_id, shape_id, len(geometry.stops), "-", "-", "-", "-"))
            continue
        a = np.array(diffs)
        last_agency = geometry.agency_dist_ft(geometry.stops[-1].stop_sequence or -1)
        ratio = (geometry.stops[-1].dist_along_ft / last_agency - 1) if last_agency else float("nan")
        cut = geometry.n_input_vertices - geometry.n_vertices
        rows_out.append((trip.route_id, shape_id, len(geometry.stops), cut, f"{np.median(a):.0f}",
                         f"{a[np.abs(a).argmax()]:.0f}", f"{ratio:+.2%}"))
    print(table(rows_out, ("route", "shape", "stops", "spur vertices cut", "median diff", "max diff",
                           "length vs agency")))


if __name__ == "__main__":
    main()
