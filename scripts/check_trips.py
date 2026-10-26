"""
Sanity-checks trip reconstruction: how many trajectories each stage kept or
dropped, what it removed, and how our interpolated stop departures compare
with when the agency's trip updates stopped listing each stop.
"""
import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

from aaata_gtfs import db
from aaata_gtfs.config import DATA_PATH, LOCAL_TZ, db_path
from aaata_gtfs.queries import data_time_range
from aaata_gtfs.shapes import GeometryResolver
from aaata_gtfs.static_gtfs import StaticGtfs
from aaata_gtfs.trips import agency_stop_passed, reconstruct_from_db, stop_crossings


def parse_time(value: str) -> float:
    t = datetime.fromisoformat(value)
    return (t if t.tzinfo else t.replace(tzinfo=LOCAL_TZ)).timestamp()


def main() -> None:
    parser = argparse.ArgumentParser(description="Check trip reconstruction against the agency's trip updates.")
    parser.add_argument("--data-path", type=Path, default=DATA_PATH, help=f"Data directory (default: {DATA_PATH})")
    parser.add_argument("--start", type=parse_time, help="ISO-8601 (local if naive; default: 24 h before --end)")
    parser.add_argument("--end", type=parse_time, help="ISO-8601 (local if naive; default: the latest data)")
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
    result = reconstruct_from_db(conn, resolver, start, end)

    trajectories = result.trajectories
    print(f"{datetime.fromtimestamp(start, LOCAL_TZ):%Y-%m-%d %H:%M} – "
          f"{datetime.fromtimestamp(end, LOCAL_TZ):%Y-%m-%d %H:%M}")
    print(f"trajectories: {len(trajectories)} ({sum(len(t.pings) for t in trajectories)} positions, "
          f"{sum(t.relabelled for t in trajectories)} relabelled from a stale trip id)")
    print("dropped:", dict(result.dropped.most_common()) or "none")
    removed = Counter(r.reason for t in trajectories for r in t.removed)
    print("positions removed:", dict(removed.most_common()) or "none")
    covered = [(t.distances[-1] - t.distances[0]) / t.geometry.length_ft for t in trajectories]
    if covered:
        print(f"shape covered per trajectory: median {np.median(covered):.0%}")

    diffs = []
    for t in trajectories:
        passed = agency_stop_passed(conn, t.trip_id, t.service_date)
        for c in stop_crossings(t)[1:]:  # the agency drops a trip's first stop early
            if c.departure_ts is not None and c.stop_sequence in passed:
                diffs.append(passed[c.stop_sequence] - c.departure_ts)
    if diffs:
        q = np.percentile(diffs, [5, 25, 50, 75, 95])
        print(f"agency 'passed' minus our departure, {len(diffs)} stops (s): "
              + ", ".join(f"p{p} {v:+.0f}" for p, v in zip((5, 25, 50, 75, 95), q)))
        print("  (positive: the agency noticed after we saw it leave; the trips feed refreshes every ~30 s)")


if __name__ == "__main__":
    main()
