"""Feed URLs, request headers, data locations and polling intervals."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

#: The repository root (this file is src/aaata_gtfs/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the database and file store live. Set `AAATA_GTFS_DATA_PATH` to override.
DATA_PATH = Path(os.getenv("AAATA_GTFS_DATA_PATH", str(REPO_ROOT / "data")))
DB_NAME = "aaata_gtfs.sqlite3"
BLOBS_DIR = "blobs"
DB_PATH = DATA_PATH / DB_NAME
BLOB_PATH = DATA_PATH / BLOBS_DIR

#: TheRide's local time zone (agency.txt says America/New_York, which is the same).
LOCAL_TZ = ZoneInfo("America/Detroit")

#: Sent with every request so TheRide can identify and contact us.
USER_AGENT = "aaata-gtfs (research data collector; contact: adempst.dev@gmail.com)"
REQUEST_HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT_S = 20.0

VEHICLES_URL = "https://rt.theride.org/gtfsrt/vehicles"
TRIPS_URL = "https://rt.theride.org/gtfsrt/trips"
ALERTS_URL = "https://rt.theride.org/gtfsrt/alerts"
RT_FEED_URLS = {"vehicles": VEHICLES_URL, "trips": TRIPS_URL, "alerts": ALERTS_URL}
STATIC_URL = "https://www.theride.org/sites/default/files/google/google_transit.zip"
GETROUTEBYID_URL = "https://www.theride.org/api/ServiceData?method=getroutebyid&routeid={route_id}"
#: OpenStreetMap's Overpass API, queried rarely and by hand for traffic signals (`osm.py`).
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

#: The vehicles and trips feeds regenerate about this often, together.
REFRESH_S = 30.0
#: A new feed is available this long after its header timestamp.
AVAILABILITY_S = 3.0
#: Bounds on the wait until the next realtime poll after a new feed.
MIN_POLL_WAIT_S = 2.0
MAX_POLL_WAIT_S = 45.0
#: Retry interval while the feed hasn't refreshed yet.
RETRY_S = 4.0
#: After this many consecutive unchanged polls, the feed counts as stalled...
STALL_AFTER_POLLS = 8
#: ...and is polled this often until it refreshes.
STALL_RETRY_S = 15.0
#: Error backoff: ERROR_BACKOFF_S, doubling, capped at MAX_ERROR_BACKOFF_S.
ERROR_BACKOFF_S = 5.0
MAX_ERROR_BACKOFF_S = 120.0

ALERTS_INTERVAL_S = 300.0
STATIC_INTERVAL_H = 6.0
BUSTIME_INTERVAL_MIN = 60.0
#: Random wait between per-route getroutebyid requests.
BUSTIME_COURTESY_WAIT_RANGE_S = (2.0, 5.0)

#: Required by TheRide's developer terms wherever the data is displayed.
THERIDE_CREDIT = "Transit scheduling, geographic, and real-time data provided by permission of AAATA/TheRide"


def db_path(data_path: Path = DATA_PATH) -> Path:
    return data_path / DB_NAME


def blob_path(data_path: Path = DATA_PATH) -> Path:
    return data_path / BLOBS_DIR


def ensure_data_dir(data_path: Path = DATA_PATH) -> Path:
    """Create the data directory if it's missing, and return it."""
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path
