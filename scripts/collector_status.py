"""
Prints a health and coverage report of the collector's database (read-only).
"""
import argparse
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import ColumnElement, func, select
from sqlmodel import Session, SQLModel

from aaata_gtfs import db
from aaata_gtfs import models as m
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


def count(session: Session, model: type[SQLModel], *where: ColumnElement[bool]) -> int:
    return int(db.rows(session, select(func.count()).select_from(m.table(model)).where(*where)).scalar_one())


def main() -> None:
    parser = argparse.ArgumentParser(description="Report on the collector's database.")
    parser.add_argument("--data-path", type=Path, default=DATA_PATH,
                        help=f"Data directory (default: {DATA_PATH})")
    args = parser.parse_args()
    path = db_path(args.data_path)
    if not path.exists():
        sys.exit(f"no database at {path}")
    session = db.connect(path, read_only=True)
    now = time.time()
    fs, run_c = m.columns(m.FeedSnapshot), m.columns(m.CollectorRun)

    print(f"Database: {path}\n")
    print("Latest snapshot per feed")
    rows = []
    for feed in ("vehicles", "trips", "alerts"):
        row = db.rows(
            session,
            select(fs.header_ts, fs.fetched_at, fs.n_entities).where(fs.feed == feed)
            .order_by(fs.header_ts.desc()).limit(1),
        ).first()
        n = count(session, m.FeedSnapshot, fs.feed == feed)
        if row:
            rows.append((feed, f"{n:,}", fmt_local(row[0]), fmt_duration(now - row[0]) + " ago", row[2]))
        else:
            rows.append((feed, "0", "-", "-", "-"))
    print(table(rows, ("feed", "snapshots", "latest header time", "age", "entities")))

    run = db.rows(
        session,
        select(run_c.id, run_c.started_at, run_c.stopped_at, run_c.version).order_by(run_c.id.desc()).limit(1),
    ).first()
    n_runs = count(session, m.CollectorRun)
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
        lo, hi = db.rows(
            session, select(func.min(fs.id), func.max(fs.id)).where(fs.header_ts >= int(start), fs.header_ts < int(end))
        ).one()
        return (lo, hi) if lo is not None else (0, -1)

    vp, tu = m.columns(m.VehiclePosition), m.columns(m.TripUpdate)
    per_day: dict[str, list[int]] = {
        "feed_snapshot": [
            count(session, m.FeedSnapshot, fs.header_ts >= int(bounds[i]), fs.header_ts < int(bounds[i + 1]))
            for i in range(DAYS)
        ],
        "vehicle_position": [
            count(session, m.VehiclePosition, vp.fix_ts >= int(bounds[i]), vp.fix_ts < int(bounds[i + 1]))
            for i in range(DAYS)
        ],
        "trip_update": [
            count(session, m.TripUpdate, tu.snapshot_id.between(*snapshot_range(bounds[i], bounds[i + 1])))
            for i in range(DAYS)
        ],
    }
    print("\nRows per table")
    rows = []
    for model in m.TABLES:
        name = m.table(model).name
        total = count(session, model)
        rows.append((name, f"{total:,}", *(f"{c:,}" for c in per_day.get(name, [])),
                     *([""] * (DAYS if name not in per_day else 0))))
    print(table(rows, ("table", "total", *(f"{d:%a %m-%d}" for d in days))))

    # Gaps between vehicles snapshots.
    stamps = list(db.rows(
        session,
        select(fs.header_ts).where(fs.feed == "vehicles", fs.header_ts >= int(now - 86400)).order_by(fs.header_ts),
    ).scalars())
    gaps = sorted(((b - a, a) for a, b in zip(stamps, stamps[1:]) if b - a > GAP_THRESHOLD_S), reverse=True)
    print(f"\nGaps over {GAP_THRESHOLD_S} s between vehicles snapshots in the last 24 h: {len(gaps)}, "
          f"totalling {fmt_duration(sum(g for g, _ in gaps))}")
    if gaps:
        print(table([(fmt_local(a), fmt_duration(g)) for g, a in gaps[:10]], ("from", "duration")))

    print("\nVersions")
    rows = []
    for label, model, id_col in (
        ("static GTFS", m.StaticVersion, "id"),
        ("RT shapes", m.RtShapeVersion, "shape_id"),
        ("trip modifications", m.TripModificationsVersion, "modifications_id"),
        ("alerts", m.AlertVersion, "alert_id"),
        ("BusTime routes", m.BusTimeRouteVersion, "route_id"),
    ):
        c = m.columns(model)
        versions, ids = db.rows(session, select(func.count(), func.count(c[id_col].distinct()))).one()
        rows.append((label, f"{versions:,}", f"{ids:,}"))
    print(table(rows, ("kind", "versions", "distinct ids")))
    sv = m.columns(m.StaticVersion)
    static = db.rows(
        session,
        select(sv.id, sv.feed_version, sv.feed_start_date, sv.feed_end_date, sv.fetched_at, sv.last_checked_at)
        .order_by(sv.fetched_at.desc()).limit(1),
    ).first()
    if static:
        print(f"Newest static version {static[0]} ({static[1]}, {static[2]}–{static[3]}), fetched "
              f"{fmt_local(static[4])}, last checked {fmt_local(static[5])}")

    latest_trips = db.rows(
        session, select(fs.id).where(fs.feed == "trips").order_by(fs.header_ts.desc()).limit(1)
    ).scalar()
    print("\nActive detours (in the latest trips snapshot)")
    if latest_trips is not None:
        mv, mt = m.columns(m.TripModificationsVersion), m.columns(m.TripModificationsTrip)
        rows = [
            (r[0], r[1], r[2], fmt_local(r[3]))
            for r in db.rows(
                session,
                select(mv.modifications_id, mv.pattern_id, func.count(mt.trip_id), fs.header_ts)
                .join(m.table(m.FeedSnapshot), fs.id == mv.first_snapshot_id)
                .outerjoin(m.table(m.TripModificationsTrip), mt.version_id == mv.id)
                .where(mv.last_snapshot_id == latest_trips).group_by(mv.id).order_by(mv.modifications_id),
            )
        ]
        print(table(rows, ("modification", "pattern", "trips", "version since")) if rows else "none")
    else:
        print("no trips snapshots yet")

    db_size = sum(p.stat().st_size for p in path.parent.glob(path.name + "*") if p.is_file())
    print(f"\nDatabase size {fmt_bytes(db_size)} (with WAL); blobs {fmt_bytes(dir_size(blob_path(args.data_path)))}")


if __name__ == "__main__":
    main()
