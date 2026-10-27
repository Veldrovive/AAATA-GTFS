"""
Writes an HTML map of the paths traced by each route's buses over a time window.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

from aaata_gtfs import db
from aaata_gtfs.config import DATA_PATH, LOCAL_TZ, db_path
from aaata_gtfs.geometry import DEFAULT_MAX_OFFSET_FT
from aaata_gtfs.queries import data_time_range, positions
from aaata_gtfs.shapes import GeometryResolver
from aaata_gtfs.static_gtfs import StaticGtfs
from aaata_gtfs.visualization import latest_positions, plot_route_paths, route_drawings, trace_bus_paths
from aaata_gtfs.visualization.data import DEFAULT_MAX_GAP_S
from aaata_gtfs.visualization.serve import DEFAULT_SERVE_PORT, serve

DEFAULT_WINDOW_S = 2 * 3600


def parse_time(value: str) -> float:
    """ISO-8601 to epoch seconds; naive times are local (America/Detroit)."""
    t = datetime.fromisoformat(value)
    if t.tzinfo is None:
        t = t.replace(tzinfo=LOCAL_TZ)
    return t.timestamp()


def main() -> None:
    parser = argparse.ArgumentParser(description="Map the paths traced by each route's buses.")
    parser.add_argument("--data-path", type=Path, default=DATA_PATH,
                        help=f"Data directory with the database (default: {DATA_PATH})")
    parser.add_argument("--output", type=Path, default=None,
                        help="Where to write the HTML map (default: route_paths.html in the data directory)")
    parser.add_argument("--routes", nargs="+", help="Only plot these route ids (default: all)")
    parser.add_argument("--start", type=parse_time,
                        help="Window start, ISO-8601 (local time if naive; default: 2 h before --end)")
    parser.add_argument("--end", type=parse_time,
                        help="Window end, ISO-8601 (local time if naive; default: the latest data)")
    parser.add_argument("--max-gap-minutes", type=float, default=DEFAULT_MAX_GAP_S / 60,
                        help="Break a bus's line when positions are further apart than this")
    parser.add_argument("--no-points", action="store_true", help="Draw only the lines, not each ping")
    parser.add_argument("--no-latest", action="store_true",
                        help="Don't mark each active bus's position at the window end")
    parser.add_argument("--no-route-shapes", action="store_true",
                        help="Don't draw each route's shapes or snap pings to them")
    parser.add_argument("--no-stops", action="store_true", help="Draw route shapes without their stops")
    parser.add_argument("--pings", choices=["raw", "snapped"], default="raw",
                        help="Which bus pings the map opens showing: raw positions joined by lines, or "
                        "snapped onto their trip's shape, without lines (default: raw). The map has a "
                        "toggle to switch.")
    parser.add_argument("--max-offset-ft", type=float, default=DEFAULT_MAX_OFFSET_FT,
                        help=f"Pings further than this from their shape aren't snapped "
                        f"(default: {DEFAULT_MAX_OFFSET_FT:g})")
    parser.add_argument("--serve", nargs="?", type=int, const=DEFAULT_SERVE_PORT, metavar="PORT",
                        help=f"After writing, serve the map over HTTP (default port: {DEFAULT_SERVE_PORT}). "
                        "Handy on a remote machine: forward the port (VS Code does this automatically, "
                        "or use `ssh -L PORT:localhost:PORT host`) and open the printed URL.")
    args = parser.parse_args()

    path = db_path(args.data_path)
    if not path.exists():
        sys.exit(f"no database at {path}; run scripts/collect.py first")
    session = db.connect(path, read_only=True)
    output: Path = args.output or args.data_path / "route_paths.html"

    data_range = data_time_range(session)
    if data_range is None and (args.start is None or args.end is None):
        sys.exit("no vehicle positions in the database yet")
    end = args.end if args.end is not None else data_range[1] if data_range else 0.0
    start = args.start if args.start is not None else end - DEFAULT_WINDOW_S

    rows = positions(session, start, end, args.routes)
    paths = trace_bus_paths(rows, max_gap_s=args.max_gap_minutes * 60)
    latest = {} if args.no_latest else latest_positions(session, end, args.routes)

    static = StaticGtfs(session)
    resolver = None
    drawings = {}
    if not args.no_route_shapes:
        resolver = GeometryResolver(session, static, args.data_path)
        route_ids = set(args.routes or ()) | {p.route_id for p in paths if p.route_id is not None}
        route_ids |= {r for r in latest if r is not None}
        drawings = route_drawings(static, resolver, sorted(route_ids), start, end)

    output.parent.mkdir(parents=True, exist_ok=True)
    plot_route_paths(
        paths,
        show_points=not args.no_points,
        routes=drawings,
        show_stops=not args.no_stops,
        ping_view=args.pings,
        max_offset_ft=args.max_offset_ft,
        resolver=resolver,
        latest=latest,
        static=static,
    ).save(str(output))
    n_latest = sum(len(v) for v in latest.values())
    window = (f"{datetime.fromtimestamp(start, LOCAL_TZ):%Y-%m-%d %H:%M:%S} – "
              f"{datetime.fromtimestamp(end, LOCAL_TZ):%Y-%m-%d %H:%M:%S %Z}")
    print(f"Wrote {len(paths)} paths, {len(rows)} pings and {n_latest} latest positions ({window}) to {output}")

    if args.serve is not None:
        serve(output, args.serve)


if __name__ == "__main__":
    main()
