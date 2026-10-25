"""Static GTFS: fetching, loading into the DB, and looking things up.

Each distinct feed is a `static_version`, identified by a content id: the first
12 hex digits of the sha256 over its `.txt` members' names and hashes, so a
re-zipped copy of the same feed is the same version. Versions are never
deleted; trips are looked up by service date, since a new feed can be published
before it takes effect.
"""

import csv
import hashlib
import io
import json
import sqlite3
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

from aaata_gtfs import filestore
from aaata_gtfs.config import DATA_PATH, STATIC_URL
from aaata_gtfs.http import get


@dataclass(frozen=True)
class FetchResult:
    content: bytes
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class NotModified:
    etag: str | None
    last_modified: str | None


async def fetch_static(
    client: httpx.AsyncClient,
    etag: str | None,
    last_modified: str | None,
    url: str = STATIC_URL,
) -> FetchResult | NotModified:
    """Conditional GET of the static feed zip."""
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    response = await get(client, url, headers)
    new_etag = response.headers.get("etag", etag)
    new_last_modified = response.headers.get("last-modified", last_modified)
    if response.status_code == 304:
        return NotModified(new_etag, new_last_modified)
    return FetchResult(response.content, new_etag, new_last_modified)


def _txt_members(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """The feed's `.txt` files by base name, ignoring `__MACOSX/` and directories."""
    members = {}
    for info in zf.infolist():
        if info.is_dir() or info.filename.startswith("__MACOSX/") or "/__MACOSX/" in info.filename:
            continue
        name = info.filename.rsplit("/", 1)[-1]
        if name.endswith(".txt") and not name.startswith("._"):
            members[name] = info
    return members


def content_sha256(zip_bytes: bytes) -> str:
    """Hash of a feed's content, independent of zip member order and timestamps."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        entries = sorted(
            (name, hashlib.sha256(zf.read(info)).hexdigest()) for name, info in _txt_members(zf).items()
        )
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def content_id(content_sha: str) -> str:
    return content_sha[:12]


def parse_time(value: str | None) -> int | None:
    """GTFS `HH:MM:SS` (hours may exceed 24) to seconds after the service day's noon - 12 h."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _int(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(float(value)) if value else None


def _float(value: str | None) -> float | None:
    value = (value or "").strip()
    return float(value) if value else None


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _rows(zf: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo], name: str) -> Iterator[dict[str, str]]:
    info = members.get(name)
    if info is None:
        return
    with zf.open(info) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))


def load_static(
    conn: sqlite3.Connection,
    zip_bytes: bytes,
    fetched_at: float,
    etag: str | None,
    last_modified: str | None,
    data_path: Path = DATA_PATH,
) -> str:
    """Store a static feed and return its version id.

    A feed already stored (same content) only has `last_checked_at`, `etag` and
    `last_modified` updated. A new one is archived and loaded in one transaction.
    """
    content_sha = content_sha256(zip_bytes)
    version = content_id(content_sha)
    with conn:
        if conn.execute("SELECT 1 FROM static_version WHERE id = ?", (version,)).fetchone():
            conn.execute(
                "UPDATE static_version SET last_checked_at = ?, etag = COALESCE(?, etag), "
                "last_modified = COALESCE(?, last_modified) WHERE id = ?",
                (fetched_at, etag, last_modified, version),
            )
            return version

        blob = filestore.put_static(version, zip_bytes, data_path)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            members = _txt_members(zf)
            info = next(_rows(zf, members, "feed_info.txt"), {})
            conn.execute(
                "INSERT INTO static_version (id, content_sha256, zip_sha256, fetched_at, last_checked_at, "
                "etag, last_modified, feed_version, feed_start_date, feed_end_date, blob) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version, content_sha, hashlib.sha256(zip_bytes).hexdigest(), fetched_at, fetched_at,
                    etag, last_modified, _text(info.get("feed_version")),
                    _text(info.get("feed_start_date")), _text(info.get("feed_end_date")), blob,
                ),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO static_route VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (version, r["route_id"], _text(r.get("route_short_name")), _text(r.get("route_long_name")),
                     _int(r.get("route_type")), _text(r.get("route_color")), _text(r.get("route_text_color")))
                    for r in _rows(zf, members, "routes.txt")
                ),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO static_stop VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (version, r["stop_id"], _text(r.get("stop_code")), _text(r.get("stop_name")),
                     _float(r.get("stop_lat")), _float(r.get("stop_lon")), _int(r.get("wheelchair_boarding")))
                    for r in _rows(zf, members, "stops.txt")
                ),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO static_trip VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (version, r["trip_id"], r["route_id"], r["service_id"], _text(r.get("trip_headsign")),
                     _int(r.get("direction_id")), _text(r.get("block_id")), _text(r.get("shape_id")))
                    for r in _rows(zf, members, "trips.txt")
                ),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO static_stop_time VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (version, r["trip_id"], int(r["stop_sequence"]), r["stop_id"],
                     parse_time(r.get("arrival_time")), parse_time(r.get("departure_time")),
                     _float(r.get("shape_dist_traveled")), _int(r.get("timepoint")),
                     _int(r.get("pickup_type")), _int(r.get("drop_off_type")), _text(r.get("stop_headsign")))
                    for r in _rows(zf, members, "stop_times.txt")
                ),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO static_shape_point VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (version, r["shape_id"], int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]),
                     float(r["shape_pt_lon"]), _float(r.get("shape_dist_traveled")))
                    for r in _rows(zf, members, "shapes.txt")
                ),
            )
            days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            conn.executemany(
                "INSERT OR REPLACE INTO static_calendar VALUES (?, ?, ?, ?, ?)",
                (
                    (version, r["service_id"], "".join((r.get(d) or "0").strip() for d in days),
                     r["start_date"].strip(), r["end_date"].strip())
                    for r in _rows(zf, members, "calendar.txt")
                ),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO static_calendar_date VALUES (?, ?, ?, ?)",
                (
                    (version, r["service_id"], r["date"].strip(), int(r["exception_type"]))
                    for r in _rows(zf, members, "calendar_dates.txt")
                ),
            )
    return version


def touch_static(conn: sqlite3.Connection, version: str, checked_at: float,
                 etag: str | None, last_modified: str | None) -> None:
    """Record that the server still serves `version` (e.g. after a 304)."""
    with conn:
        conn.execute(
            "UPDATE static_version SET last_checked_at = ?, etag = COALESCE(?, etag), "
            "last_modified = COALESCE(?, last_modified) WHERE id = ?",
            (checked_at, etag, last_modified, version),
        )


@dataclass(frozen=True)
class StaticVersion:
    id: str
    fetched_at: float
    last_checked_at: float
    etag: str | None
    last_modified: str | None
    feed_version: str | None
    feed_start_date: str | None
    feed_end_date: str | None

    def covers(self, service_date: str) -> bool:
        return (
            self.feed_start_date is not None
            and self.feed_end_date is not None
            and self.feed_start_date <= service_date <= self.feed_end_date
        )


@dataclass(frozen=True)
class StaticTrip:
    version: str
    trip_id: str
    route_id: str
    service_id: str
    headsign: str | None
    direction_id: int | None
    block_id: str | None
    shape_id: str | None


@dataclass(frozen=True)
class StaticStopTime:
    trip_id: str
    stop_sequence: int
    stop_id: str
    arrival_s: int | None
    departure_s: int | None
    shape_dist_m: float | None
    timepoint: int | None
    stop_headsign: str | None


@dataclass(frozen=True)
class StaticStop:
    stop_id: str
    stop_code: str | None  # the number riders see; BusTime's stop id
    name: str | None
    lat: float
    lon: float


@dataclass(frozen=True)
class StaticRoute:
    route_id: str
    short_name: str | None
    long_name: str | None
    route_type: int | None
    color: str | None
    text_color: str | None


@dataclass(frozen=True)
class ShapePoint:
    lat: float
    lon: float
    dist_m: float | None


@dataclass(frozen=True)
class _Calendar:
    days: str
    start_date: str
    end_date: str


class StaticGtfs:
    """Read-side lookups over every stored static version, with in-memory caches.

    Call `reload()` after a new version is loaded.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.reload()

    def reload(self) -> None:
        rows = self.conn.execute(
            "SELECT id, fetched_at, last_checked_at, etag, last_modified, feed_version, "
            "feed_start_date, feed_end_date FROM static_version ORDER BY fetched_at DESC"
        ).fetchall()
        #: Newest (most recently fetched) first.
        self.versions = [StaticVersion(*row) for row in rows]
        self._by_id = {v.id: v for v in self.versions}
        self._trip_rows: dict[str, list[StaticTrip]] = {}
        self._trip_cache: dict[tuple[str, str | None], StaticTrip | None] = {}
        self._stop_times: dict[tuple[str, str], list[StaticStopTime]] = {}
        self._spans: dict[tuple[str, str], tuple[int, int] | None] = {}
        self._shapes: dict[tuple[str, str], list[ShapePoint]] = {}
        self._stops: dict[str, dict[str, StaticStop]] = {}
        self._routes: dict[str, dict[str, StaticRoute]] = {}
        self._calendars: dict[str, tuple[dict[str, _Calendar], dict[tuple[str, str], int]]] = {}
        self._route_trips: dict[tuple[str, str], list[StaticTrip]] = {}
        #: Used by `service_dates.service_date_for`.
        self.service_date_cache: dict[tuple[str, str], list[tuple[str, float, float, float]]] = {}

    def latest_version(self) -> str | None:
        """The most recently fetched version."""
        return self.versions[0].id if self.versions else None

    def version(self, version_id: str) -> StaticVersion | None:
        return self._by_id.get(version_id)

    def version_for(self, service_date: str | None) -> str | None:
        """The version in effect on a date: the most recently fetched one whose
        feed dates cover it, else the most recently fetched."""
        if service_date is not None:
            for v in self.versions:
                if v.covers(service_date):
                    return v.id
        return self.latest_version()

    def _trips_with_id(self, trip_id: str) -> list[StaticTrip]:
        rows = self._trip_rows.get(trip_id)
        if rows is None:
            rows = [
                StaticTrip(*row)
                for row in self.conn.execute(
                    "SELECT static_version, trip_id, route_id, service_id, headsign, direction_id, "
                    "block_id, shape_id FROM static_trip WHERE trip_id = ?",
                    (trip_id,),
                )
            ]
            order = {v.id: i for i, v in enumerate(self.versions)}
            rows.sort(key=lambda t: order.get(t.version, len(order)))
            self._trip_rows[trip_id] = rows
        return rows

    def trip(self, trip_id: str, service_date: str | None = None) -> StaticTrip | None:
        """A trip, from the version that covers `service_date` if one has it,
        else from the most recently fetched version that has it."""
        key = (trip_id, service_date)
        if key in self._trip_cache:
            return self._trip_cache[key]
        rows = self._trips_with_id(trip_id)
        found = None
        if service_date is not None:
            found = next(
                (t for t in rows if (v := self._by_id.get(t.version)) is not None and v.covers(service_date)),
                None,
            )
        if found is None and rows:
            found = rows[0]
        self._trip_cache[key] = found
        return found

    def stop_times(self, version: str, trip_id: str) -> list[StaticStopTime]:
        key = (version, trip_id)
        rows = self._stop_times.get(key)
        if rows is None:
            rows = [
                StaticStopTime(*row)
                for row in self.conn.execute(
                    "SELECT trip_id, stop_sequence, stop_id, arrival_s, departure_s, shape_dist_m, "
                    "timepoint, stop_headsign FROM static_stop_time "
                    "WHERE static_version = ? AND trip_id = ? ORDER BY stop_sequence",
                    key,
                )
            ]
            self._stop_times[key] = rows
        return rows

    def trip_span(self, version: str, trip_id: str) -> tuple[int, int] | None:
        """(first departure, last arrival) in schedule seconds, or None if the trip has no times."""
        key = (version, trip_id)
        if key not in self._spans:
            row = self.conn.execute(
                "SELECT MIN(COALESCE(departure_s, arrival_s)), MAX(COALESCE(arrival_s, departure_s)) "
                "FROM static_stop_time WHERE static_version = ? AND trip_id = ?",
                key,
            ).fetchone()
            self._spans[key] = (int(row[0]), int(row[1])) if row and row[0] is not None else None
        return self._spans[key]

    def shape_points(self, version: str, shape_id: str) -> list[ShapePoint]:
        key = (version, shape_id)
        points = self._shapes.get(key)
        if points is None:
            points = [
                ShapePoint(*row)
                for row in self.conn.execute(
                    "SELECT lat, lon, dist_m FROM static_shape_point "
                    "WHERE static_version = ? AND shape_id = ? ORDER BY seq",
                    key,
                )
            ]
            self._shapes[key] = points
        return points

    def stops(self, version: str) -> dict[str, StaticStop]:
        stops = self._stops.get(version)
        if stops is None:
            stops = {
                row[0]: StaticStop(*row)
                for row in self.conn.execute(
                    "SELECT stop_id, stop_code, name, lat, lon FROM static_stop WHERE static_version = ?",
                    (version,),
                )
            }
            self._stops[version] = stops
        return stops

    def routes(self, version: str) -> dict[str, StaticRoute]:
        routes = self._routes.get(version)
        if routes is None:
            routes = {
                row[0]: StaticRoute(*row)
                for row in self.conn.execute(
                    "SELECT route_id, short_name, long_name, route_type, color, text_color "
                    "FROM static_route WHERE static_version = ?",
                    (version,),
                )
            }
            self._routes[version] = routes
        return routes

    def route_trips(self, version: str, route_id: str) -> list[StaticTrip]:
        key = (version, route_id)
        trips = self._route_trips.get(key)
        if trips is None:
            trips = [
                StaticTrip(*row)
                for row in self.conn.execute(
                    "SELECT static_version, trip_id, route_id, service_id, headsign, direction_id, "
                    "block_id, shape_id FROM static_trip WHERE static_version = ? AND route_id = ? "
                    "ORDER BY trip_id",
                    key,
                )
            ]
            self._route_trips[key] = trips
        return trips

    def route_stop_uses(self, version: str, route_id: str) -> list[tuple[str, str | None, str | None]]:
        """Distinct (stop_id, shape_id, headsign) over a route's trips' stop times."""
        return [
            (row[0], row[1], row[2])
            for row in self.conn.execute(
                "SELECT DISTINCT st.stop_id, t.shape_id, t.headsign FROM static_trip t "
                "JOIN static_stop_time st ON st.static_version = t.static_version AND st.trip_id = t.trip_id "
                "WHERE t.static_version = ? AND t.route_id = ?",
                (version, route_id),
            )
        ]

    def stop_code_to_id(self, version: str) -> dict[str, str]:
        """GTFS stop_code (= BusTime stop id) to GTFS stop_id."""
        return {s.stop_code: s.stop_id for s in self.stops(version).values() if s.stop_code}

    def _calendar(self, version: str) -> tuple[dict[str, _Calendar], dict[tuple[str, str], int]]:
        cal = self._calendars.get(version)
        if cal is None:
            calendars = {
                row[0]: _Calendar(row[1], row[2], row[3])
                for row in self.conn.execute(
                    "SELECT service_id, days, start_date, end_date FROM static_calendar WHERE static_version = ?",
                    (version,),
                )
            }
            exceptions = {
                (row[0], row[1]): int(row[2])
                for row in self.conn.execute(
                    "SELECT service_id, date, exception_type FROM static_calendar_date WHERE static_version = ?",
                    (version,),
                )
            }
            cal = (calendars, exceptions)
            self._calendars[version] = cal
        return cal

    def runs_on(self, version: str, service_id: str, service_date: str) -> bool:
        """Whether a service runs on a date (`YYYYMMDD`): calendar.txt, then calendar_dates.txt exceptions."""
        calendars, exceptions = self._calendar(version)
        exception = exceptions.get((service_id, service_date))
        if exception == 1:
            return True
        if exception == 2:
            return False
        cal = calendars.get(service_id)
        if cal is None or not cal.start_date <= service_date <= cal.end_date:
            return False
        weekday = date(int(service_date[:4]), int(service_date[4:6]), int(service_date[6:])).weekday()
        return cal.days[weekday] == "1"


def load_static_file(conn: sqlite3.Connection, path: Path, data_path: Path = DATA_PATH) -> str:
    """Load a static zip from disk (e.g. a test fixture or a manual download)."""
    return load_static(conn, path.read_bytes(), time.time(), None, None, data_path)
