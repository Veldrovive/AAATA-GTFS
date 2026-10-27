"""
Compares this project's GTFS data with the old aaata-bus collector's JSON Lines
over the period both ran (parallel-run validation; see SPEC.md §9.4).

The old file is streamed line by line and never modified.
"""
import argparse
import bisect
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import cos, hypot, radians
from pathlib import Path
from typing import Any

import numpy as np

from aaata_gtfs import db
from aaata_gtfs.config import DATA_PATH, LOCAL_TZ, REPO_ROOT, db_path
from aaata_gtfs.geometry import EARTH_RADIUS_FT
from aaata_gtfs.queries import PositionRow, data_time_range, positions
from aaata_gtfs.shapes import GeometryResolver
from aaata_gtfs.static_gtfs import StaticGtfs

DEFAULT_OLD_DATA = REPO_ROOT.parent / "AAATA-Bus-API" / "dat" / "route_prediction_data.jsonl"
MATCH_FT = 3.0
#: An old ping can match a fix up to this long after it was requested (clock skew).
MATCH_AHEAD_S = 5
#: ...and no older than this.
MATCH_LOOKBACK_S = 600


def parse_time(value: str) -> float:
    """ISO-8601 to epoch seconds; naive times are local (America/Detroit)."""
    t = datetime.fromisoformat(value)
    return (t if t.tzinfo else t.replace(tzinfo=LOCAL_TZ)).timestamp()


def old_utc(value: str) -> float:
    """The old collector's naive timestamps are UTC (container clock)."""
    t = datetime.fromisoformat(value)
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def distance_ft(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = radians(lat2 - lat1) * EARTH_RADIUS_FT
    dx = radians(lon2 - lon1) * EARTH_RADIUS_FT * cos(radians((lat1 + lat2) / 2))
    return hypot(dx, dy)


def iter_old(path: Path, start: float, end: float) -> Iterator[tuple[float, dict[str, Any]]]:
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                snapshot = json.loads(line)
                t = old_utc(snapshot["requested_at"])
            except (ValueError, KeyError, TypeError):
                continue
            if start <= t <= end:
                yield t, snapshot


def old_time_range(path: Path) -> tuple[float, float] | None:
    lo = hi = None
    for t, _ in iter_old(path, float("-inf"), float("inf")):
        lo = t if lo is None else min(lo, t)
        hi = t if hi is None else max(hi, t)
    return (lo, hi) if lo is not None and hi is not None else None


def dist_summary(values: list[float], unit: str = "s") -> str:
    if not values:
        return "n=0"
    a = np.array(values, dtype=float)
    p10, p50, p90 = np.percentile(a, [10, 50, 90])
    return (f"n={len(a):,}  p10={p10:.1f} {unit}  median={p50:.1f} {unit}  p90={p90:.1f} {unit}  "
            f"min={a.min():.1f} {unit}  max={a.max():.1f} {unit}")


def table(rows: Sequence[Sequence[object]], headers: tuple[str, ...]) -> str:
    cells = [headers, *[tuple("" if c is None else str(c) for c in row) for row in rows]]
    widths = [max(len(str(r[i])) for r in cells) for i in range(len(headers))]
    out = []
    for j, row in enumerate(cells):
        out.append("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())
        if j == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


@dataclass
class Match:
    old_t: float
    route_id: str
    vehicle_id: str
    pattern_id: str
    new: PositionRow


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare GTFS data with the old aaata-bus collector's data.")
    parser.add_argument("--old-data", type=Path, default=DEFAULT_OLD_DATA,
                        help=f"The old route_prediction_data.jsonl (default: {DEFAULT_OLD_DATA})")
    parser.add_argument("--data-path", type=Path, default=DATA_PATH, help=f"This project's data (default: {DATA_PATH})")
    parser.add_argument("--start", type=parse_time, help="ISO-8601 (local if naive; default: start of the overlap)")
    parser.add_argument("--end", type=parse_time, help="ISO-8601 (local if naive; default: end of the overlap)")
    args = parser.parse_args()

    path = db_path(args.data_path)
    if not path.exists():
        sys.exit(f"no database at {path}")
    if not args.old_data.exists():
        sys.exit(f"no old data at {args.old_data}")
    conn = db.connect(path, read_only=True)
    new_range = data_time_range(conn)
    if new_range is None:
        sys.exit("no positions in the new database")
    start, end = args.start, args.end
    if start is None or end is None:
        old_range = old_time_range(args.old_data)
        if old_range is None:
            sys.exit("no snapshots in the old data")
        start = start if start is not None else max(old_range[0], new_range[0])
        end = end if end is not None else min(old_range[1], new_range[1])
    if start >= end:
        sys.exit("the datasets don't overlap")
    print(f"Window: {datetime.fromtimestamp(start, LOCAL_TZ):%Y-%m-%d %H:%M:%S} – "
          f"{datetime.fromtimestamp(end, LOCAL_TZ):%Y-%m-%d %H:%M:%S %Z}\n")

    static = StaticGtfs(conn)
    resolver = GeometryResolver(conn, static, args.data_path)
    # New positions per vehicle, with a margin so pings at the window start can match.
    by_vehicle: dict[str, list[PositionRow]] = defaultdict(list)
    for p in positions(conn, start - MATCH_LOOKBACK_S, end + MATCH_AHEAD_S):
        by_vehicle[p.vehicle_id].append(p)
    fix_times = {v: [p.fix_ts for p in rows] for v, rows in by_vehicle.items()}

    old_pings = matched = absent = never = 0
    staleness: list[float] = []
    matches: list[Match] = []
    old_routes: set[str] = set()
    old_positions: dict[tuple[str, str, int], set[tuple[float, float]]] = defaultdict(set)  # (route, vehicle, hour)
    matched_new: set[tuple[str, int]] = set()
    old_unmatched_positions: Counter[str] = Counter()
    trip_agree = trip_total = 0
    trip_disagreements: list[tuple[float, str, str, str]] = []  # (ft from start/end, vehicle, old, new)

    for t, snapshot in iter_old(args.old_data, start, end):
        route_id = str(snapshot.get("route_id"))
        old_routes.add(route_id)
        preds: dict[str, list[dict[str, Any]]] = snapshot.get("predictions") or {}
        for bus in snapshot.get("buses") or []:
            old_pings += 1
            vehicle_id = str(bus["vehicle_id"])
            lat, lon = float(bus["latitude"]), float(bus["longitude"])
            old_positions[(route_id, vehicle_id, int(t // 3600))].add((lat, lon))
            rows = by_vehicle.get(vehicle_id, [])
            i = bisect.bisect_right(fix_times.get(vehicle_id, []), t + MATCH_AHEAD_S) - 1
            found = None
            while i >= 0 and rows[i].fix_ts >= t - MATCH_LOOKBACK_S:
                if distance_ft(lat, lon, rows[i].lat, rows[i].lon) <= MATCH_FT:
                    found = rows[i]
                    break
                i -= 1
            if found is None:
                times = fix_times.get(vehicle_id, [])
                lo = bisect.bisect_left(times, t - MATCH_LOOKBACK_S)
                if bisect.bisect_right(times, t + MATCH_LOOKBACK_S) > lo:
                    never += 1
                else:
                    absent += 1
                old_unmatched_positions[route_id] += 1
                continue
            matched += 1
            staleness.append(t - found.fix_ts)
            matched_new.add((vehicle_id, found.fix_ts))
            matches.append(Match(t, route_id, vehicle_id, str(bus.get("pattern_id")), found))

            # 4. Trip ids: the old nearest-stop prediction's trip vs the GTFS vehicle's trip.
            bus_preds = preds.get(vehicle_id) or []
            if bus_preds and found.trip_id is not None:
                first = min(bus_preds, key=lambda p: p.get("distance_to_stop_ft", 1 << 30))
                trip_total += 1
                if str(first.get("trip_id")) == found.trip_id:
                    trip_agree += 1
                else:
                    geometry = resolver.for_trip(found.trip_id, found.service_date, found.fix_ts)
                    edge = float("nan")
                    if geometry is not None:
                        d = geometry.project(found.lat, found.lon).dist_along_ft
                        edge = min(d, geometry.length_ft - d)
                    trip_disagreements.append((edge, vehicle_id, str(first.get("trip_id")), found.trip_id))

    print("1. Fix-time matching (old ping = same vehicle within 3 ft, latest fix <= requested_at + 5 s)")
    rate = matched / old_pings if old_pings else 0.0
    print(f"   old pings {old_pings:,}, matched {matched:,} ({rate:.1%}); unmatched: vehicle absent from GTFS "
          f"{absent:,}, position never in GTFS {never:,}")
    print(f"   requested_at - fix_ts: {dist_summary(staleness)}\n")

    print("2. Coverage on the routes the old collector tracks")
    new_on_routes = [p for rows in by_vehicle.values() for p in rows
                     if p.route_id in old_routes and start <= p.fix_ts <= end]
    new_by_route: dict[str, list[PositionRow]] = defaultdict(list)
    for p in new_on_routes:
        new_by_route[p.route_id or ""].append(p)
    rows_out = []
    for route_id in sorted(old_routes, key=lambda r: (not r.isdigit(), r.zfill(6))):
        old_vh = {k: v for k, v in old_positions.items() if k[0] == route_id}
        old_distinct = sum(len(v) for v in old_vh.values())
        new_rows = new_by_route.get(route_id, [])
        new_vh = {(p.vehicle_id, p.fix_ts // 3600) for p in new_rows}
        new_only = sum(1 for p in new_rows if (p.vehicle_id, p.fix_ts) not in matched_new)
        rows_out.append((
            route_id, len(old_vh), f"{old_distinct / len(old_vh):.1f}" if old_vh else "-",
            len(new_vh), f"{len(new_rows) / len(new_vh):.1f}" if new_vh else "-",
            old_unmatched_positions[route_id], new_only,
        ))
    print(table(rows_out, ("route", "old veh-h", "old pos/veh-h", "new veh-h", "new pos/veh-h",
                           "old-only pings", "new-only fixes")))
    print()

    print("3. Pattern mapping for matched pings (old pattern_id vs GTFS)")
    combos: Counter[tuple[str, str, str, str]] = Counter()
    for m in matches:
        p = m.new
        if p.trip_id is None:
            combos[(m.route_id, m.pattern_id, "-", "(not in service)")] += 1
            continue
        pid = resolver.pattern_for_trip(p.trip_id, p.service_date, p.fix_ts) or "?"
        geometry = resolver.for_trip(p.trip_id, p.service_date, p.fix_ts)
        combos[(m.route_id, m.pattern_id, pid, geometry.shape_id if geometry else "?")] += 1
    print(table([(*k, f"{n:,}") for k, n in sorted(combos.items())],
                ("route", "old pattern", "GTFS pattern", "GTFS shape", "pings")))
    print()

    print("4. Trip ids (old nearest-stop prediction vs GTFS vehicle position)")
    if trip_total:
        print(f"   agreement {trip_agree:,}/{trip_total:,} ({trip_agree / trip_total:.1%})")
        buckets: Counter[str] = Counter()
        for edge, *_ in trip_disagreements:
            if edge != edge:
                buckets["no geometry"] += 1
            elif edge < 500:
                buckets["< 500 ft"] += 1
            elif edge < 2000:
                buckets["500–2,000 ft"] += 1
            elif edge < 5000:
                buckets["2,000–5,000 ft"] += 1
            else:
                buckets[">= 5,000 ft"] += 1
        if buckets:
            print("   disagreements by distance from the GTFS trip's start or end:")
            print(table(sorted(buckets.items()), ("distance", "count")))
    else:
        print("   no matched pings with predictions")
    print()


if __name__ == "__main__":
    main()
