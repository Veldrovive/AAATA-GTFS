"""Collecting TheRide (AAATA) GTFS-realtime and static GTFS data into SQLite."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("aaata-gtfs")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"

from aaata_gtfs.config import (  # noqa: E402
    BLOB_PATH,
    DATA_PATH,
    DB_PATH,
    LOCAL_TZ,
    REQUEST_HEADERS,
    THERIDE_CREDIT,
    USER_AGENT,
    ensure_data_dir,
)
from aaata_gtfs.db import connect, migrate, open_db  # noqa: E402
from aaata_gtfs.geometry import (  # noqa: E402
    DEFAULT_MAX_OFFSET_FT,
    Projection,
    ShapeGeometry,
    ShapeStop,
    SnappedPing,
    StopPosition,
    snap_sightings,
)
from aaata_gtfs.osm import LightPosition, Signal, lights_on_shape  # noqa: E402
from aaata_gtfs.polyline import decode_polyline  # noqa: E402
from aaata_gtfs.queries import (  # noqa: E402
    ModificationVersion,
    PositionRow,
    RtShapeVersion,
    StopTimeRow,
    active_positions,
    active_vehicles,
    latest_vehicles_snapshot,
    positions,
    predictions_at,
    rt_shape_at,
    trip_modifications_at,
)
from aaata_gtfs.realtime import (  # noqa: E402
    AlertRecord,
    RtShapeRecord,
    StopTimeRecord,
    TripModificationsRecord,
    TripUpdateRecord,
    VehicleRecord,
    parse_feed,
)
from aaata_gtfs.service_dates import service_date_for  # noqa: E402
from aaata_gtfs.shapes import GeometryResolver  # noqa: E402
from aaata_gtfs.static_gtfs import StaticGtfs, load_static  # noqa: E402
from aaata_gtfs.store import Store  # noqa: E402
from aaata_gtfs.trips import (  # noqa: E402
    Reconstruction,
    StopCrossing,
    Trajectory,
    TripPing,
    reconstruct,
    reconstruct_from_db,
    stop_crossings,
)

__all__ = [
    "AlertRecord",
    "BLOB_PATH",
    "DATA_PATH",
    "DB_PATH",
    "DEFAULT_MAX_OFFSET_FT",
    "GeometryResolver",
    "LOCAL_TZ",
    "LightPosition",
    "ModificationVersion",
    "PositionRow",
    "Projection",
    "REQUEST_HEADERS",
    "Reconstruction",
    "RtShapeRecord",
    "RtShapeVersion",
    "ShapeGeometry",
    "ShapeStop",
    "Signal",
    "SnappedPing",
    "StaticGtfs",
    "StopCrossing",
    "StopPosition",
    "StopTimeRecord",
    "StopTimeRow",
    "Store",
    "THERIDE_CREDIT",
    "Trajectory",
    "TripModificationsRecord",
    "TripPing",
    "TripUpdateRecord",
    "USER_AGENT",
    "VehicleRecord",
    "__version__",
    "active_positions",
    "active_vehicles",
    "connect",
    "decode_polyline",
    "ensure_data_dir",
    "latest_vehicles_snapshot",
    "lights_on_shape",
    "load_static",
    "migrate",
    "open_db",
    "parse_feed",
    "positions",
    "predictions_at",
    "reconstruct",
    "reconstruct_from_db",
    "rt_shape_at",
    "service_date_for",
    "snap_sightings",
    "stop_crossings",
    "trip_modifications_at",
]
