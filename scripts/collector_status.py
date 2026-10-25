"""
Prints a health and coverage report of the collector's database (read-only).
"""
import argparse
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from aaata_gtfs import db
from aaata_gtfs.config import DATA_PATH, LOCAL_TZ, blob_path, db_path

GAP_THRESHOLD_S = 90
DAYS = 7


def fmt_local(ts: float) -> str:
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds / 60:.1f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def table(rows: Sequence[Sequence[object]], headers: tuple[str, ...]) -> str:
    cells = [headers, *[tuple("" if c is None else str(c) for c in row) for row in rows]]
    widths = [max(len(str(r[i])) for r in cells) for i in range(len(headers))]
    lines = []
    for j, row in enumerate(cells):
        lines.append("  ".join(
            str(c).rjust(w) if j and str(c).replace(",", "").replace(".", "").isdigit() else str(c).ljust(w)
            for c, w in zip(row, widths)
        ).rstrip())
        if j == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report on the collector's database.")
    parser.add_argument("--data-path", type=Path, default=DATA_PATH,
                        help=f"Data directory (default: {DATA_PATH})")
    args = parser.parse_args()
    path = db_path(args.data_path)
    if not path.exists():
        sys.exit(f"no database at {path}")
    conn = db.connect(path, read_only=True)
    now = time.time()

    print(f"Database: {path}\n")
    print("Latest snapshot per feed")
    rows = []
    for feed in ("vehicles", "trips", "alerts"):
        row = conn.execute(
            "SELECT header_ts, fetched_at, n_entities FROM feed_snapshot WHERE feed = ? "
            "ORDER BY header_ts DESC LIMIT 1", (feed,)
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM feed_snapshot WHERE feed = ?", (feed,)).fetchone()[0]
        if row:
            rows.append((feed, f"{count:,}", fmt_local(row[0]), fmt_duration(now - row[0]) + " ago", row[2]))
        else:
            rows.append((feed, "0", "-", "-", "-"))
    print(table(rows, ("feed", "snapshots", "latest header time", "age", "entities")))

    run = conn.execute(
        "SELECT id, started_at, stopped_at, version FROM collector_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    n_runs = conn.execute("SELECT COUNT(*) FROM collector_run").fetchone()[0]
    print()
    if run:
        state = f"stopped {fmt_local(run[2])}" if run[2] else f"running for {fmt_duration(now - run[1])}"
        print(f"Collector run {run[0]} (of {n_runs}): started {fmt_local(run[1])}, {state}, version {run[3]}")
    else:
        print("No collector runs yet")

    # Rows per table, total and per local day.
    today = datetime.fromtimestamp(now, LOCAL_TZ).date()
    days = [today - timedelta(days=i) for i in range(DAYS - 1, -1, -1)]
    bounds = [datetime.combine(d, datetime.min.time(), LOCAL_TZ).timestamp() for d in days]
    bounds.append(bounds[-1] + 86400 * 2)

    def snapshot_range(start: float, end: float) -> tuple[int, int]:
        lo, hi = conn.execute(
            "SELECT MIN(id), MAX(id) FROM feed_snapshot WHERE header_ts >= ? AND header_ts < ?",
            (int(start), int(end)),
        ).fetchone()
        return (lo, hi) if lo is not None else (0, -1)

    per_day: dict[str, list[int]] = {}
    for name, sql, by in (
        ("feed_snapshot", "SELECT COUNT(*) FROM feed_snapshot WHERE header_ts >= ? AND header_ts < ?", "ts"),
        ("vehicle_position", "SELECT COUNT(*) FROM vehicle_position WHERE fix_ts >= ? AND fix_ts < ?", "ts"),
        ("trip_update", "SELECT COUNT(*) FROM trip_update WHERE snapshot_id BETWEEN ? AND ?", "snap"),
        ("stop_time_prediction",
         "SELECT COUNT(*) FROM stop_time_prediction WHERE snapshot_id BETWEEN ? AND ?", "snap"),
    ):
        counts = []
        for i in range(DAYS):
            params = (int(bounds[i]), int(bounds[i + 1])) if by == "ts" else snapshot_range(bounds[i], bounds[i + 1])
            counts.append(conn.execute(sql, params).fetchone()[0])
        per_day[name] = counts
    print("\nRows per table")
    rows = []
    for name in db.TABLES:
        total = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        rows.append((name, f"{total:,}", *(f"{c:,}" for c in per_day.get(name, [])),
                     *([""] * (DAYS if name not in per_day else 0))))
    print(table(rows, ("table", "total", *(f"{d:%a %m-%d}" for d in days))))

    # Gaps between vehicles snapshots.
    stamps = [r[0] for r in conn.execute(
        "SELECT header_ts FROM feed_snapshot WHERE feed = 'vehicles' AND header_ts >= ? ORDER BY header_ts",
        (int(now - 86400),),
    )]
    gaps = sorted(((b - a, a) for a, b in zip(stamps, stamps[1:]) if b - a > GAP_THRESHOLD_S), reverse=True)
    print(f"\nGaps over {GAP_THRESHOLD_S} s between vehicles snapshots in the last 24 h: {len(gaps)}, "
          f"totalling {fmt_duration(sum(g for g, _ in gaps))}")
    if gaps:
        print(table([(fmt_local(a), fmt_duration(g)) for g, a in gaps[:10]], ("from", "duration")))

    print("\nVersions")
    rows = []
    for label, sql in (
        ("static GTFS", "SELECT COUNT(*), COUNT(DISTINCT id) FROM static_version"),
        ("RT shapes", "SELECT COUNT(*), COUNT(DISTINCT shape_id) FROM rt_shape_version"),
        ("trip modifications", "SELECT COUNT(*), COUNT(DISTINCT modifications_id) FROM trip_modifications_version"),
        ("alerts", "SELECT COUNT(*), COUNT(DISTINCT alert_id) FROM alert_version"),
        ("BusTime routes", "SELECT COUNT(*), COUNT(DISTINCT route_id) FROM bustime_route_version"),
    ):
        versions, ids = conn.execute(sql).fetchone()
        rows.append((label, f"{versions:,}", f"{ids:,}"))
    print(table(rows, ("kind", "versions", "distinct ids")))
    static = conn.execute(
        "SELECT id, feed_version, feed_start_date, feed_end_date, fetched_at, last_checked_at "
        "FROM static_version ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()
    if static:
        print(f"Newest static version {static[0]} ({static[1]}, {static[2]}–{static[3]}), fetched "
              f"{fmt_local(static[4])}, last checked {fmt_local(static[5])}")

    latest_trips = conn.execute(
        "SELECT id FROM feed_snapshot WHERE feed = 'trips' ORDER BY header_ts DESC LIMIT 1"
    ).fetchone()
    print("\nActive detours (in the latest trips snapshot)")
    if latest_trips:
        rows = [
            (r[0], r[1], r[2], fmt_local(r[3]))
            for r in conn.execute(
                "SELECT m.modifications_id, m.pattern_id, COUNT(t.trip_id), f.header_ts "
                "FROM trip_modifications_version m JOIN feed_snapshot f ON f.id = m.first_snapshot_id "
                "LEFT JOIN trip_modifications_trip t ON t.version_id = m.id "
                "WHERE m.last_snapshot_id = ? GROUP BY m.id ORDER BY m.modifications_id",
                (latest_trips[0],),
            )
        ]
        print(table(rows, ("modification", "pattern", "trips", "version since")) if rows else "none")
    else:
        print("no trips snapshots yet")

    db_size = sum(p.stat().st_size for p in path.parent.glob(path.name + "*") if p.is_file())
    print(f"\nDatabase size {fmt_bytes(db_size)} (with WAL); blobs {fmt_bytes(dir_size(blob_path(args.data_path)))}")


if __name__ == "__main__":
    main()
