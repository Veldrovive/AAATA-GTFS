"""Traffic signals from OpenStreetMap, located along shapes.

Signals come from one Overpass API query over the area the static shapes
cover, made rarely (by hand, with `scripts/fetch_osm_signals.py`) and saved as
a dated blob, `blobs/osm/signals/<YYYYMMDDTHHMMSSZ>.json.gz`. OSM data is ©
OpenStreetMap contributors, under the ODbL.

Both `highway=traffic_signals` (intersection signals) and signalised
pedestrian crossings (`crossing=traffic_signals`) are kept: either can stop a
bus. A signal often has several nodes (one per approach, or per carriageway of
a divided road), so nodes close together along a shape are merged into one
light.
"""

import gzip
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from aaata_gtfs.config import BLOBS_DIR, DATA_PATH, OVERPASS_URL
from aaata_gtfs.filestore import write_once
from aaata_gtfs.geometry import ShapeGeometry
from aaata_gtfs.http import get

SIGNALS_DIR = f"{BLOBS_DIR}/osm/signals"

#: Margin (degrees, ~0.5 mi) added around the shapes' bounding box.
BBOX_MARGIN_DEG = 0.008

#: A signal node this close to a shape is on it. Signal nodes sit on the
#: intersection or a stop line, within a lane or two of the centerline.
SIGNAL_MAX_OFFSET_FT = 60.0

#: Signal nodes this close together along a shape are one light.
SIGNAL_MERGE_FT = 150.0

#: Overpass can take a minute on a city-sized query.
OVERPASS_TIMEOUT_S = 120.0

#: Attribution required wherever OSM-derived data is displayed.
OSM_CREDIT = "© OpenStreetMap contributors"


@dataclass(frozen=True)
class Signal:
    osm_id: int
    lat: float
    lon: float
    kind: str  # "intersection" or "crossing"
    tags: dict[str, str]


@dataclass(frozen=True)
class LightPosition:
    """A traffic light on a shape: one or more signal nodes merged."""

    dist_along_ft: float
    osm_ids: tuple[int, ...]
    kind: str  # "intersection" if any node is one, else "crossing"


def signals_query(bbox: tuple[float, float, float, float]) -> str:
    """Overpass QL for signal nodes in (south, west, north, east)."""
    s, w, n, e = bbox
    area = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
    return (
        "[out:json][timeout:90];("
        f'node["highway"="traffic_signals"]{area};'
        f'node["crossing"="traffic_signals"]{area};'
        ");out body;"
    )


def shapes_bbox(points: Iterable[tuple[float, float]], margin_deg: float = BBOX_MARGIN_DEG) -> tuple[float, float, float, float]:
    """(south, west, north, east) around (lat, lon) points, with a margin."""
    lats, lons = zip(*points)
    return (min(lats) - margin_deg, min(lons) - margin_deg, max(lats) + margin_deg, max(lons) + margin_deg)


async def fetch_signals(
    client: httpx.AsyncClient, bbox: tuple[float, float, float, float], url: str = OVERPASS_URL
) -> bytes:
    """The Overpass JSON response for signal nodes in a bounding box."""
    response = await get(client, f"{url}?data={quote(signals_query(bbox))}", timeout_s=OVERPASS_TIMEOUT_S)
    return response.content


def signals_relpath(fetched_at: float) -> str:
    t = datetime.fromtimestamp(fetched_at, timezone.utc)
    return f"{SIGNALS_DIR}/{t:%Y%m%dT%H%M%SZ}.json.gz"


def put_signals(payload: bytes, fetched_at: float, data_path: Path = DATA_PATH) -> str:
    """Save an Overpass response (gzipped); returns its relpath."""
    relpath = signals_relpath(fetched_at)
    write_once(data_path / relpath, gzip.compress(payload, mtime=0))
    return relpath


def latest_signals_blob(data_path: Path = DATA_PATH) -> Path | None:
    """The most recently fetched signals blob, if any."""
    found = sorted((data_path / SIGNALS_DIR).glob("*.json.gz"))
    return found[-1] if found else None


def parse_signals(payload: bytes) -> list[Signal]:
    """Signals from an Overpass JSON response."""
    out = []
    for element in json.loads(payload)["elements"]:
        if element.get("type") != "node":
            continue
        tags = element.get("tags", {})
        kind = "intersection" if tags.get("highway") == "traffic_signals" and "crossing" not in tags else "crossing"
        out.append(Signal(int(element["id"]), float(element["lat"]), float(element["lon"]), kind, tags))
    return out


def load_signals(path: Path) -> list[Signal]:
    return parse_signals(gzip.decompress(path.read_bytes()))


def lights_on_shape(
    geometry: ShapeGeometry,
    signals: Sequence[Signal],
    max_offset_ft: float = SIGNAL_MAX_OFFSET_FT,
    merge_ft: float = SIGNAL_MERGE_FT,
) -> list[LightPosition]:
    """The lights a bus passes along a shape, in order.

    Each signal node is located on every pass of the shape within
    `max_offset_ft` (a shape through an intersection twice meets its light
    twice); nodes within `merge_ft` of the previous one along the shape join
    its light, which sits at their mean distance.
    """
    hits: list[tuple[float, Signal]] = []
    lat0, lat1 = geometry.latitudes.min(), geometry.latitudes.max()
    lon0, lon1 = geometry.longitudes.min(), geometry.longitudes.max()
    pad = 0.001  # ~350 ft, more than max_offset_ft
    for signal in signals:
        if not (lat0 - pad <= signal.lat <= lat1 + pad and lon0 - pad <= signal.lon <= lon1 + pad):
            continue
        for p in geometry.project_passes(signal.lat, signal.lon, max_offset_ft):
            hits.append((p.dist_along_ft, signal))
    hits.sort(key=lambda h: h[0])

    groups: list[list[tuple[float, Signal]]] = []
    for hit in hits:
        if groups and hit[0] - groups[-1][-1][0] <= merge_ft:
            groups[-1].append(hit)
        else:
            groups.append([hit])
    return [
        LightPosition(
            sum(d for d, _ in group) / len(group),
            tuple(dict.fromkeys(s.osm_id for _, s in group)),
            "intersection" if any(s.kind == "intersection" for _, s in group) else "crossing",
        )
        for group in groups
    ]
