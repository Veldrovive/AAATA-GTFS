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
import time
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from itertools import batched
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, insert, select, update
from sqlmodel import Session, SQLModel

from aaata_gtfs import db, filestore
from aaata_gtfs import models as m
from aaata_gtfs.config import DATA_PATH, STATIC_URL
from aaata_gtfs.http import get

#: Rows per INSERT batch when loading a feed (stop_times.txt has ~100,000).
LOAD_CHUNK_ROWS = 5000


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


def _insert_all(session: Session, model: type[SQLModel], rows: Iterable[dict[str, Any]]) -> None:
    """INSERT OR REPLACE `rows` in chunks (so a big file isn't all in memory at once)."""
    statement = insert(model).prefix_with("OR REPLACE")
    for chunk in batched(rows, LOAD_CHUNK_ROWS):
        session.exec(statement, params=list(chunk))


def _touch(session: Session, version: str, checked_at: float, etag: str | None, last_modified: str | None) -> None:
    v = m.columns(m.StaticVersion)
    session.exec(
        update(m.table(m.StaticVersion)).where(v.id == version).values(
            last_checked_at=checked_at,
            etag=func.coalesce(etag, v.etag),
            last_modified=func.coalesce(last_modified, v.last_modified),
        )
    )


def load_static(
    session: Session,
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
    with db.write(session):
        if session.get(m.StaticVersion, version) is not None:
            _touch(session, version, fetched_at, etag, last_modified)
            return version

        blob = filestore.put_static(version, zip_bytes, data_path)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            members = _txt_members(zf)
            info = next(_rows(zf, members, "feed_info.txt"), {})
            session.add(m.StaticVersion(
                id=version, content_sha256=content_sha, zip_sha256=hashlib.sha256(zip_bytes).hexdigest(),
                fetched_at=fetched_at, last_checked_at=fetched_at, etag=etag, last_modified=last_modified,
                feed_version=_text(info.get("feed_version")), feed_start_date=_text(info.get("feed_start_date")),
                feed_end_date=_text(info.get("feed_end_date")), blob=blob,
            ))
            session.flush()
            _insert_all(session, m.StaticRoute, (
                {"static_version": version, "route_id": r["route_id"],
                 "short_name": _text(r.get("route_short_name")), "long_name": _text(r.get("route_long_name")),
                 "route_type": _int(r.get("route_type")), "color": _text(r.get("route_color")),
                 "text_color": _text(r.get("route_text_color"))}
                for r in _rows(zf, members, "routes.txt")
            ))
            _insert_all(session, m.StaticStop, (
                {"static_version": version, "stop_id": r["stop_id"], "stop_code": _text(r.get("stop_code")),
                 "name": _text(r.get("stop_name")), "lat": _float(r.get("stop_lat")),
                 "lon": _float(r.get("stop_lon")), "wheelchair_boarding": _int(r.get("wheelchair_boarding"))}
                for r in _rows(zf, members, "stops.txt")
            ))
            _insert_all(session, m.StaticTrip, (
                {"static_version": version, "trip_id": r["trip_id"], "route_id": r["route_id"],
                 "service_id": r["service_id"], "headsign": _text(r.get("trip_headsign")),
                 "direction_id": _int(r.get("direction_id")), "block_id": _text(r.get("block_id")),
                 "shape_id": _text(r.get("shape_id"))}
                for r in _rows(zf, members, "trips.txt")
            ))
            _insert_all(session, m.StaticStopTime, (
                {"static_version": version, "trip_id": r["trip_id"], "stop_sequence": int(r["stop_sequence"]),
                 "stop_id": r["stop_id"], "arrival_s": parse_time(r.get("arrival_time")),
                 "departure_s": parse_time(r.get("departure_time")),
                 "shape_dist_m": _float(r.get("shape_dist_traveled")), "timepoint": _int(r.get("timepoint")),
                 "pickup_type": _int(r.get("pickup_type")), "drop_off_type": _int(r.get("drop_off_type")),
                 "stop_headsign": _text(r.get("stop_headsign"))}
                for r in _rows(zf, members, "stop_times.txt")
            ))
            _insert_all(session, m.StaticShapePoint, (
                {"static_version": version, "shape_id": r["shape_id"], "seq": int(r["shape_pt_sequence"]),
                 "lat": float(r["shape_pt_lat"]), "lon": float(r["shape_pt_lon"]),
                 "dist_m": _float(r.get("shape_dist_traveled"))}
                for r in _rows(zf, members, "shapes.txt")
            ))
            days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            _insert_all(session, m.StaticCalendar, (
                {"static_version": version, "service_id": r["service_id"],
                 "days": "".join((r.get(d) or "0").strip() for d in days),
                 "start_date": r["start_date"].strip(), "end_date": r["end_date"].strip()}
                for r in _rows(zf, members, "calendar.txt")
            ))
            _insert_all(session, m.StaticCalendarDate, (
                {"static_version": version, "service_id": r["service_id"], "date": r["date"].strip(),
                 "exception_type": int(r["exception_type"])}
                for r in _rows(zf, members, "calendar_dates.txt")
            ))
    return version


def touch_static(session: Session, version: str, checked_at: float,
                 etag: str | None, last_modified: str | None) -> None:
    """Record that the server still serves `version` (e.g. after a 304)."""
    with db.write(session):
        _touch(session, version, checked_at, etag, last_modified)


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


_trip = m.columns(m.StaticTrip)
_TRIP_SELECT = select(
    _trip.static_version, _trip.trip_id, _trip.route_id, _trip.service_id, _trip.headsign, _trip.direction_id,
    _trip.block_id, _trip.shape_id,
)


class StaticGtfs:
    """Read-side lookups over every stored static version, with in-memory caches.

    Call `reload()` after a new version is loaded.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.reload()

    def reload(self) -> None:
        v = m.columns(m.StaticVersion)
        rows = db.rows(
            self.session,
            select(v.id, v.fetched_at, v.last_checked_at, v.etag, v.last_modified, v.feed_version,
                   v.feed_start_date, v.feed_end_date).order_by(v.fetched_at.desc())
        ).all()
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
                for row in db.rows(self.session, _TRIP_SELECT.where(_trip.trip_id == trip_id))
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
        st = m.columns(m.StaticStopTime)
        key = (version, trip_id)
        rows = self._stop_times.get(key)
        if rows is None:
            rows = [
                StaticStopTime(*row)
                for row in db.rows(
                    self.session,
                    select(st.trip_id, st.stop_sequence, st.stop_id, st.arrival_s, st.departure_s,
                           st.shape_dist_m, st.timepoint, st.stop_headsign)
                    .where(st.static_version == version, st.trip_id == trip_id).order_by(st.stop_sequence)
                )
            ]
            self._stop_times[key] = rows
        return rows

    def trip_span(self, version: str, trip_id: str) -> tuple[int, int] | None:
        """(first departure, last arrival) in schedule seconds, or None if the trip has no times."""
        key = (version, trip_id)
        if key not in self._spans:
            st = m.columns(m.StaticStopTime)
            row = db.rows(
                self.session,
                select(func.min(func.coalesce(st.departure_s, st.arrival_s)),
                       func.max(func.coalesce(st.arrival_s, st.departure_s)))
                .where(st.static_version == version, st.trip_id == trip_id)
            ).first()
            self._spans[key] = (int(row[0]), int(row[1])) if row and row[0] is not None else None
        return self._spans[key]

    def shape_points(self, version: str, shape_id: str) -> list[ShapePoint]:
        sp = m.columns(m.StaticShapePoint)
        key = (version, shape_id)
        points = self._shapes.get(key)
        if points is None:
            points = [
                ShapePoint(*row)
                for row in db.rows(
                    self.session,
                    select(sp.lat, sp.lon, sp.dist_m)
                    .where(sp.static_version == version, sp.shape_id == shape_id).order_by(sp.seq)
                )
            ]
            self._shapes[key] = points
        return points

    def stops(self, version: str) -> dict[str, StaticStop]:
        stops = self._stops.get(version)
        if stops is None:
            c = m.columns(m.StaticStop)
            stops = {
                row[0]: StaticStop(*row)
                for row in db.rows(
                    self.session,
                    select(c.stop_id, c.stop_code, c.name, c.lat, c.lon).where(c.static_version == version)
                )
            }
            self._stops[version] = stops
        return stops

    def routes(self, version: str) -> dict[str, StaticRoute]:
        routes = self._routes.get(version)
        if routes is None:
            r = m.columns(m.StaticRoute)
            routes = {
                row[0]: StaticRoute(*row)
                for row in db.rows(
                    self.session,
                    select(r.route_id, r.short_name, r.long_name, r.route_type, r.color, r.text_color)
                    .where(r.static_version == version)
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
                for row in db.rows(
                    self.session,
                    _TRIP_SELECT.where(_trip.static_version == version, _trip.route_id == route_id)
                    .order_by(_trip.trip_id)
                )
            ]
            self._route_trips[key] = trips
        return trips

    def route_stop_uses(self, version: str, route_id: str) -> list[tuple[str, str | None, str | None]]:
        """Distinct (stop_id, shape_id, headsign) over a route's trips' stop times."""
        t, st = _trip, m.columns(m.StaticStopTime)
        return [
            (row[0], row[1], row[2])
            for row in db.rows(
                self.session,
                select(st.stop_id, t.shape_id, t.headsign).distinct()
                .join(m.table(m.StaticStopTime), (st.static_version == t.static_version) & (st.trip_id == t.trip_id))
                .where(t.static_version == version, t.route_id == route_id)
            )
        ]

    def stop_code_to_id(self, version: str) -> dict[str, str]:
        """GTFS stop_code (= BusTime stop id) to GTFS stop_id."""
        return {s.stop_code: s.stop_id for s in self.stops(version).values() if s.stop_code}

    def _calendar(self, version: str) -> tuple[dict[str, _Calendar], dict[tuple[str, str], int]]:
        cal = self._calendars.get(version)
        if cal is None:
            c, d = m.columns(m.StaticCalendar), m.columns(m.StaticCalendarDate)
            calendars = {
                row[0]: _Calendar(row[1], row[2], row[3])
                for row in db.rows(
                    self.session,
                    select(c.service_id, c.days, c.start_date, c.end_date).where(c.static_version == version)
                )
            }
            exceptions = {
                (row[0], row[1]): int(row[2])
                for row in db.rows(
                    self.session,
                    select(d.service_id, d.date, d.exception_type).where(d.static_version == version)
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


def load_static_file(session: Session, path: Path, data_path: Path = DATA_PATH) -> str:
    """Load a static zip from disk (e.g. a test fixture or a manual download)."""
    return load_static(session, path.read_bytes(), time.time(), None, None, data_path)
