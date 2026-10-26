"""
Fetches traffic signals from OpenStreetMap (one Overpass API query over the
area of the newest static feed's shapes) and saves the response as a dated
blob under <data>/blobs/osm/signals/. Run it rarely, by hand; the model reads
the latest blob.
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

from aaata_gtfs import db
from aaata_gtfs.config import DATA_PATH, db_path, ensure_data_dir
from aaata_gtfs.http import make_client
from aaata_gtfs.osm import fetch_signals, parse_signals, put_signals, shapes_bbox, signals_query
from aaata_gtfs.static_gtfs import StaticGtfs


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch OSM traffic signals for the area TheRide's shapes cover.")
    parser.add_argument("--data-path", type=Path, default=DATA_PATH, help=f"Data directory (default: {DATA_PATH})")
    parser.add_argument("--dry-run", action="store_true", help="Print the query and exit")
    args = parser.parse_args()

    path = db_path(args.data_path)
    if not path.exists():
        sys.exit(f"no database at {path}")
    conn = db.connect(path, read_only=True)
    version = StaticGtfs(conn).latest_version()
    if version is None:
        sys.exit("no static feed loaded")
    points = conn.execute(
        "SELECT lat, lon FROM static_shape_point WHERE static_version = ?", (version,)
    ).fetchall()
    bbox = shapes_bbox(points)
    if args.dry_run:
        print(signals_query(bbox))
        return

    async def fetch() -> bytes:
        async with make_client() as client:
            return await fetch_signals(client, bbox)

    fetched_at = time.time()
    payload = asyncio.run(fetch())
    relpath = put_signals(payload, fetched_at, ensure_data_dir(args.data_path))
    signals = parse_signals(payload)
    kinds = {k: sum(s.kind == k for s in signals) for k in ("intersection", "crossing")}
    print(f"{len(signals)} signal nodes ({kinds['intersection']} intersection, {kinds['crossing']} crossing) "
          f"-> {relpath}")


if __name__ == "__main__":
    main()
