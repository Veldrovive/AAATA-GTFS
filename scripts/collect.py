"""
Runs the GTFS-realtime collector until SIGTERM/SIGINT.
"""
import argparse
import asyncio
import logging
from pathlib import Path

from aaata_gtfs.collector import CollectorOptions, run_collector
from aaata_gtfs.config import (
    ALERTS_INTERVAL_S,
    BUSTIME_INTERVAL_MIN,
    DATA_PATH,
    STATIC_INTERVAL_H,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect TheRide's GTFS-realtime, static GTFS and BusTime routes.")
    parser.add_argument("--data-path", type=Path, default=DATA_PATH,
                        help=f"Where the database and blobs go (default: {DATA_PATH})")
    parser.add_argument("--no-raw-archive", action="store_true",
                        help="Don't archive raw realtime feed payloads (static and BusTime are always kept)")
    parser.add_argument("--alerts-interval-s", type=float, default=ALERTS_INTERVAL_S,
                        help=f"Alerts poll interval (default: {ALERTS_INTERVAL_S:g})")
    parser.add_argument("--static-interval-h", type=float, default=STATIC_INTERVAL_H,
                        help=f"Static GTFS check interval (default: {STATIC_INTERVAL_H:g})")
    parser.add_argument("--bustime-interval-min", type=float, default=BUSTIME_INTERVAL_MIN,
                        help=f"getroutebyid interval (default: {BUSTIME_INTERVAL_MIN:g})")
    parser.add_argument("--no-bustime", action="store_true", help="Don't collect BusTime getroutebyid")
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO)")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO; the collector logs what it stores instead.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    options = CollectorOptions(
        data_path=args.data_path,
        archive_raw=not args.no_raw_archive,
        alerts_interval_s=args.alerts_interval_s,
        static_interval_h=args.static_interval_h,
        bustime_interval_min=args.bustime_interval_min,
        bustime=not args.no_bustime,
        args={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
    )
    asyncio.run(run_collector(options))


if __name__ == "__main__":
    main()
