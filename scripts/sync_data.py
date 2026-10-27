"""
Copies the database and blobs between machines through an rclone remote (a
Google Drive or OneDrive folder): `push` on the collector's machine, `pull`
wherever you analyse. The remote is set in the gitignored .env; see the
"Moving data between machines" section of README.md.
"""
import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from aaata_gtfs.config import DATA_PATH, LOCAL_TZ
from aaata_gtfs.datasync import REMOTE_ENV, Manifest, Rclone, SyncError, environment, pull, push


def describe(manifest: Manifest) -> str:
    latest = ("no data" if manifest.latest_fetched_at is None else
              datetime.fromtimestamp(manifest.latest_fetched_at, LOCAL_TZ).strftime("data to %Y-%m-%d %H:%M %Z"))
    pushed = datetime.fromtimestamp(manifest.pushed_at, LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
    return f"{latest}, {manifest.db_bytes / 1e6:.0f} MB, pushed from {manifest.host} at {pushed}"


def main() -> None:
    env = environment()
    default_data = Path(env.get("AAATA_GTFS_DATA_PATH", str(DATA_PATH)))
    parser = argparse.ArgumentParser(description="Push or pull the data directory through an rclone remote.")
    parser.add_argument("direction", choices=["push", "pull"])
    parser.add_argument("--data-path", type=Path, default=default_data, help=f"Data directory (default: {default_data})")
    parser.add_argument("--remote", default=env.get(REMOTE_ENV),
                        help=f"rclone path of the shared folder (default: ${REMOTE_ENV} from the environment or .env)")
    parser.add_argument("--db-only", action="store_true", help="Skip the blobs")
    parser.add_argument("--skip-rt", action="store_true",
                        help="Skip the raw realtime archives (blobs/rt: most of the blobs; not needed to fit models)")
    parser.add_argument("--force", action="store_true", help="Replace the other side even if it has newer data")
    parser.add_argument("--verbose", "-v", action="store_true", help="Log each file copied, and the rclone commands")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.verbose:
        logging.getLogger("aaata_gtfs").setLevel(logging.DEBUG)
    if not args.remote:
        sys.exit(f"no remote: set {REMOTE_ENV} in .env (e.g. {REMOTE_ENV}=aaata:aaata-gtfs) or pass --remote")

    try:
        remote = Rclone(args.remote, env, verbose=args.verbose)
        if args.direction == "push":
            manifest = push(args.data_path, remote, blobs=not args.db_only, rt_blobs=not args.skip_rt,
                            force=args.force)
            print(f"pushed to {args.remote}: {describe(manifest)}")
        else:
            result = pull(args.data_path, remote, blobs=not args.db_only, rt_blobs=not args.skip_rt,
                          force=args.force)
            if result is None:
                print("database already up to date" + ("" if args.db_only else "; blobs copied"))
            else:
                print(f"pulled into {args.data_path}: {describe(result)}")
    except SyncError as e:
        sys.exit(str(e))
    except subprocess.CalledProcessError as e:  # rclone has printed why
        sys.exit(f"rclone {e.cmd[1]} failed (exit status {e.returncode})")


if __name__ == "__main__":
    main()
