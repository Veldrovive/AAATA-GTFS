"""Dedup-aware writers: nothing is stored twice when nothing changed.

A `Store` keeps in-memory copies of the last stored state (latest snapshot per
feed, latest position per vehicle, latest trip update per trip
instance, latest version per detour, shape, alert and BusTime route), and
rebuilds them from the DB on construction so a restart inserts nothing new for
data it already has. Each snapshot is processed in one transaction. The rules
per table are in SPEC.md §4.3.
"""

import hashlib
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, col

from aaata_gtfs import db, filestore
from aaata_gtfs.bustime import payload_hash, route_version_id
from aaata_gtfs.config import DATA_PATH
from aaata_gtfs.models import (
    AlertVersion,
    BusTimeRouteVersion,
    CollectorRun,
    FeedSnapshot,
    RtShapeVersion,
    TripModificationsDate,
    TripModificationsTrip,
    TripModificationsVersion,
    TripUpdate,
    VehiclePosition,
    VehicleSeen,
    columns,
    table,
)
from aaata_gtfs.realtime import (
    AlertRecord,
    DecodeError,
    FeedMessage,
    RtShapeRecord,
    TripModificationsRecord,
    TripUpdateRecord,
    VehicleRecord,
    alert_records,
    alerts_content_sha256,
    header_ts,
    parse_feed,
    rt_shape_records,
    trip_modifications_records,
    trip_update_records,
    vehicle_records,
)
from aaata_gtfs.service_dates import service_date_for
from aaata_gtfs.static_gtfs import StaticGtfs

log = logging.getLogger(__name__)

FEEDS = ("vehicles", "trips", "alerts")

#: Caches of trip instances are rebuilt from rows this recent...
CACHE_LOAD_HOURS = 36.0
#: ...and pruned of instances not seen for this long.
CACHE_PRUNE_HOURS = 6.0


class MalformedFeed(Exception):
    """A payload that didn't decode as a FeedMessage."""

    def __init__(self, feed: str, sha256: str, error: Exception) -> None:
        super().__init__(f"malformed {feed} feed (sha256 {sha256}): {error}")
        self.feed = feed
        self.sha256 = sha256


def start_run(session: Session, version: str, args: dict[str, Any], started_at: float | None = None) -> int:
    """Record a collector run and return its id."""
    run = CollectorRun(
        started_at=started_at if started_at is not None else time.time(), version=version,
        args=json.dumps(args, sort_keys=True, default=str),
    )
    with db.write(session):
        session.add(run)
        session.flush()
        run_id = run.id
    assert run_id is not None
    return run_id


def finish_run(session: Session, run_id: int, stopped_at: float | None = None) -> None:
    with db.write(session):
        session.exec(
            update(CollectorRun).where(col(CollectorRun.id) == run_id)
            .values(stopped_at=stopped_at if stopped_at is not None else time.time())
        )


@dataclass
class SnapshotResult:
    """What storing one feed message did. `snapshot_id` is None if it was skipped."""

    feed: str
    header_ts: int
    snapshot_id: int | None
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def stored(self) -> bool:
        return self.snapshot_id is not None


@dataclass
class _LastSnapshot:
    id: int
    header_ts: int
    sha256: str


@dataclass
class _TripState:
    row_id: int
    fields: tuple[object, ...]
    last_seen: float  # our clock, for pruning


#: The trip-level fields a new `trip_update` version is stored for. Not `update_ts`: the
#: feed stamps every trip update on every refresh, so a version keeps the first one's.
_TRIP_FIELDS = (
    "vehicle_id", "schedule_relationship", "modifications_id", "delay",
    "first_stop_sequence", "n_stop_time_updates", "route_id",
)

_VEHICLE_COLUMNS = (
    "vehicle_id", "fix_ts", "lat", "lon", "bearing", "speed_mps", "trip_id", "route_id",
    "service_date", "trip_schedule_relationship", "direction_id", "start_date", "start_time",
    "current_stop_sequence", "stop_id", "current_status", "occupancy_status", "congestion_level",
    "odometer", "vehicle_label", "snapshot_id",
)


_Versioned = type[TripModificationsVersion] | type[RtShapeVersion] | type[AlertVersion]
_VersionRow = TripModificationsVersion | RtShapeVersion | AlertVersion | BusTimeRouteVersion


def _trip_fields(record: TripUpdateRecord) -> tuple[object, ...]:
    return (
        record.vehicle_id, record.schedule_relationship, record.modifications_id,
        record.delay, record.first_stop_sequence, record.n_stop_time_updates, record.route_id,
    )


def _vehicle_content(record: VehicleRecord, service_date: str | None) -> tuple[object, ...]:
    """A position's stored values other than the key and snapshot, for conflict detection."""
    return (
        record.lat, record.lon, record.bearing, record.speed_mps, record.trip_id, record.route_id,
        service_date, record.trip_schedule_relationship, record.direction_id, record.start_date,
        record.start_time, record.current_stop_sequence, record.stop_id, record.current_status,
        record.occupancy_status, record.congestion_level, record.odometer, record.vehicle_label,
    )


class Store:
    """Writes snapshots to the DB (and the raw archive), skipping anything unchanged.

    The collector is the only writer and calls this from one thread.
    """

    def __init__(
        self,
        session: Session,
        static: StaticGtfs,
        run_id: int,
        data_path: Path = DATA_PATH,
        archive_raw: bool = True,
        now: float | None = None,
    ) -> None:
        self.session = session
        self.static = static
        self.run_id = run_id
        self.data_path = data_path
        self.archive_raw = archive_raw
        #: Running totals since startup: rows inserted per table, snapshots, conflicts, ...
        self.stats: Counter[str] = Counter()
        #: Our clock at each alerts poll (whether or not it was stored).
        self.alerts_polled_at: float | None = None
        self.load_caches(now if now is not None else time.time())

    # ------------------------------------------------------------------ caches

    def load_caches(self, now: float) -> None:
        """Rebuild every last-state cache from the DB."""
        session = self.session
        fs = columns(FeedSnapshot)
        self.last_snapshot: dict[str, _LastSnapshot] = {}
        for feed in FEEDS:
            row = db.rows(
                session,
                select(fs.id, fs.header_ts, fs.sha256).where(fs.feed == feed).order_by(fs.header_ts.desc()).limit(1)
            ).first()
            if row:
                self.last_snapshot[feed] = _LastSnapshot(*row)

        # Vehicles present in the latest vehicles snapshot, with their open interval.
        self.open_seen: dict[str, int] = {}
        last_vehicles = self.last_snapshot.get("vehicles")
        if last_vehicles is not None:
            seen = columns(VehicleSeen)
            self.open_seen = {
                vehicle_id: first
                for vehicle_id, first in db.rows(
                    session,
                    select(seen.vehicle_id, seen.first_snapshot_id).where(seen.last_snapshot_id == last_vehicles.id)
                )
            }

        cutoff_ts = int(now - CACHE_LOAD_HOURS * 3600)
        min_snapshot = db.rows(session, select(func.min(fs.id)).where(fs.header_ts >= cutoff_ts)).scalar()
        min_snapshot = min_snapshot if min_snapshot is not None else 1 << 62

        # Latest position per vehicle.
        self.last_fix: dict[str, tuple[int, tuple[object, ...]]] = {}
        vp = columns(VehiclePosition)
        latest = (
            select(vp.vehicle_id, func.max(vp.fix_ts).label("fix_ts"))
            .where(vp.fix_ts >= cutoff_ts).group_by(vp.vehicle_id).subquery()
        )
        for row in db.rows(
            session,
            select(*(vp[c] for c in _VEHICLE_COLUMNS[:-1]))
            .join(latest, and_(vp.vehicle_id == latest.c.vehicle_id, vp.fix_ts == latest.c.fix_ts))
        ):
            self.last_fix[row[0]] = (row[1], tuple(row[2:]))

        # Latest trip update per trip instance.
        self.trips: dict[tuple[str, str], _TripState] = {}
        tu = columns(TripUpdate)
        latest_ids = (
            select(func.max(tu.id)).where(tu.snapshot_id >= min_snapshot).group_by(tu.trip_id, tu.service_date)
        )
        for row in db.rows(
            session,
            select(tu.id, tu.trip_id, tu.service_date, *(tu[f] for f in _TRIP_FIELDS)).where(tu.id.in_(latest_ids))
        ):
            self.trips[(row[1], row[2] or "")] = _TripState(row[0], tuple(row[3:]), now)

        # Latest version of each versioned entity: (row id, content sha256).
        self.versions: dict[str, dict[str, tuple[int, str]]] = {}
        for kind, model, id_col in (
            ("trip_modifications", TripModificationsVersion, "modifications_id"),
            ("shape", RtShapeVersion, "shape_id"),
            ("alert", AlertVersion, "alert_id"),
        ):
            t = columns(model)
            self.versions[kind] = {
                entity_id: (row_id, sha)
                for entity_id, row_id, sha in db.rows(
                    session,
                    select(t[id_col], t.id, t.content_sha256)
                    .where(t.id.in_(select(func.max(t.id)).group_by(t[id_col])))
                )
            }
        b = columns(BusTimeRouteVersion)
        self.bustime: dict[str, tuple[int, str]] = {
            route_id: (row_id, sha)
            for route_id, row_id, sha in db.rows(
                session,
                select(b.route_id, b.id, b.content_sha256)
                .where(b.id.in_(select(func.max(b.id)).group_by(b.route_id)))
            )
        }
        session.commit()  # end the read transaction

    def prune(self, now: float) -> int:
        """Drop cached trip instances not seen for `CACHE_PRUNE_HOURS`. Returns how many."""
        cutoff = now - CACHE_PRUNE_HOURS * 3600
        stale = [key for key, state in self.trips.items() if state.last_seen < cutoff]
        for key in stale:
            del self.trips[key]
        return len(stale)

    # --------------------------------------------------------------- snapshots

    def store_feed(self, feed: str, payload: bytes, fetched_at: float) -> SnapshotResult:
        """Parse and store one fetched feed message, unless it's a repeat.

        Raises `MalformedFeed` (after archiving the payload, if archiving is on)
        when it doesn't parse.
        """
        if feed not in FEEDS:
            raise ValueError(f"unknown feed {feed!r}")
        try:
            message = parse_feed(payload)
        except DecodeError as e:
            sha = hashlib.sha256(payload).hexdigest()
            if self.archive_raw:
                filestore.append_rt(feed, int(fetched_at), payload, self.data_path)
            self.stats["malformed"] += 1
            raise MalformedFeed(feed, sha, e) from e

        h = header_ts(message)
        # Alerts are deduped on content, which excludes the header's timestamp.
        sha = alerts_content_sha256(message) if feed == "alerts" else hashlib.sha256(payload).hexdigest()
        if feed == "alerts":
            self.alerts_polled_at = fetched_at
        if self._is_repeat(feed, h, sha):
            return SnapshotResult(feed, h, None)

        with db.write(self.session):
            blob = filestore.append_rt(feed, h, payload, self.data_path) if self.archive_raw else None
            snapshot = FeedSnapshot(
                feed=feed, header_ts=h, fetched_at=fetched_at, sha256=sha, n_entities=len(message.entity),
                n_bytes=len(payload), blob=blob, run_id=self.run_id,
            )
            self.session.add(snapshot)
            self.session.flush()
            snapshot_id = snapshot.id
            assert snapshot_id is not None
            result = SnapshotResult(feed, h, snapshot_id)
            if feed == "vehicles":
                self._store_vehicles(message, snapshot_id, result)
            elif feed == "trips":
                self._store_trips(message, h, snapshot_id, fetched_at, result)
            else:
                self._store_alerts(message, snapshot_id, result)
        self.last_snapshot[feed] = _LastSnapshot(snapshot_id, h, sha)
        self.stats[f"snapshots_{feed}"] += 1
        for key, n in result.counts.items():
            if key.startswith("rows_"):
                self.stats[key] += n
        return result

    def _is_repeat(self, feed: str, h: int, sha: str) -> bool:
        last = self.last_snapshot.get(feed)
        if last is not None:
            if h == last.header_ts:
                if sha != last.sha256:
                    log.warning("%s: same header_ts %d but different content (sha256 %s); skipped", feed, h, sha)
                    self.stats["same_ts_different_content"] += 1
                return True
            if h < last.header_ts:
                log.warning("%s: header_ts %d is older than the last stored %d; skipped", feed, h, last.header_ts)
                return True
            if feed == "alerts" and sha == last.sha256:
                return True
        fs = columns(FeedSnapshot)
        exists = db.rows(self.session, select(fs.id).where(fs.feed == feed, fs.header_ts == h)).first()
        self.session.commit()  # end the read transaction
        return exists is not None

    def _store_vehicles(self, message: FeedMessage, snapshot_id: int, result: SnapshotResult) -> None:
        records: dict[str, VehicleRecord] = {}
        for record in vehicle_records(message):
            records.setdefault(record.vehicle_id, record)
        new_fixes = conflicts = 0
        rows = []
        for record in records.values():
            service_date = (
                service_date_for(self.static, record.trip_id, record.fix_ts) if record.trip_id else None
            )
            content = _vehicle_content(record, service_date)
            last = self.last_fix.get(record.vehicle_id)
            if last is not None and last[0] == record.fix_ts:
                if last[1] != content:
                    conflicts += 1
                continue
            # A fix older than the cached one is only inserted if it's somehow missing.
            if last is None or record.fix_ts > last[0]:
                self.last_fix[record.vehicle_id] = (record.fix_ts, content)
            rows.append(dict(zip(_VEHICLE_COLUMNS, (record.vehicle_id, record.fix_ts, *content, snapshot_id))))
        # A Core INSERT OR IGNORE on the session's connection (same transaction), for its rowcount.
        insert_position = insert(table(VehiclePosition)).on_conflict_do_nothing()
        connection = self.session.connection()
        for row in rows:
            new_fixes += connection.execute(insert_position, row).rowcount
        self.stats["conflicts"] += conflicts

        # Presence intervals: extend those open in the previous snapshot, open new ones.
        open_seen = {}
        for vehicle_id in records:
            first = self.open_seen.get(vehicle_id)
            if first is not None:
                self.session.exec(
                    update(VehicleSeen)
                    .where(col(VehicleSeen.vehicle_id) == vehicle_id, col(VehicleSeen.first_snapshot_id) == first)
                    .values(last_snapshot_id=snapshot_id)
                )
                open_seen[vehicle_id] = first
            else:
                self.session.add(
                    VehicleSeen(vehicle_id=vehicle_id, first_snapshot_id=snapshot_id, last_snapshot_id=snapshot_id)
                )
                open_seen[vehicle_id] = snapshot_id
        self.open_seen = open_seen
        result.counts.update(
            vehicles=len(records), new_fixes=new_fixes, conflicts=conflicts, rows_vehicle_position=new_fixes
        )

    def _store_trips(
        self, message: FeedMessage, h: int, snapshot_id: int, now: float, result: SnapshotResult
    ) -> None:
        shapes = sum(self._store_shape(r, snapshot_id) for r in rt_shape_records(message))
        mods = sum(self._store_modifications(r, snapshot_id) for r in trip_modifications_records(message))
        tu_rows = unchanged = 0
        for record in trip_update_records(message):
            ts = record.update_ts if record.update_ts is not None else h
            service_date = service_date_for(self.static, record.trip_id, ts)
            key = (record.trip_id, service_date or "")
            fields = _trip_fields(record)
            state = self.trips.get(key)
            if state is not None and state.fields == fields:
                self.session.exec(
                    update(TripUpdate).where(col(TripUpdate.id) == state.row_id)
                    .values(last_seen_snapshot_id=snapshot_id)
                )
                state.last_seen = now
                unchanged += 1
                continue
            row = TripUpdate(
                trip_id=record.trip_id, service_date=service_date, route_id=record.route_id,
                vehicle_id=record.vehicle_id, update_ts=record.update_ts,
                schedule_relationship=record.schedule_relationship, modifications_id=record.modifications_id,
                delay=record.delay, first_stop_sequence=record.first_stop_sequence,
                n_stop_time_updates=record.n_stop_time_updates, snapshot_id=snapshot_id,
                last_seen_snapshot_id=snapshot_id,
            )
            self.session.add(row)
            self.session.flush()
            row_id = row.id
            assert row_id is not None
            self.trips[key] = _TripState(row_id, fields, now)
            tu_rows += 1
        result.counts.update(
            tu_rows=tu_rows, unchanged=unchanged, shape_versions=shapes,
            modification_versions=mods, rows_trip_update=tu_rows,
        )

    def _version(self, kind: str, entity_id: str, sha: str, snapshot_id: int, model: _Versioned) -> bool:
        """Advance the latest version's `last_snapshot_id` if its content is
        unchanged and return False; return True if a new version is needed."""
        latest = self.versions[kind].get(entity_id)
        if latest is not None and latest[1] == sha:
            t = table(model)
            self.session.exec(update(t).where(t.c.id == latest[0]).values(last_snapshot_id=snapshot_id))
            return False
        return True

    def _add(self, row: _VersionRow) -> int:
        """Insert a row and return its id."""
        self.session.add(row)
        self.session.flush()
        assert row.id is not None
        return row.id

    def _store_shape(self, record: RtShapeRecord, snapshot_id: int) -> bool:
        if not self._version("shape", record.shape_id, record.content_sha256, snapshot_id, RtShapeVersion):
            return False
        row_id = self._add(RtShapeVersion(
            shape_id=record.shape_id, content_sha256=record.content_sha256,
            encoded_polyline=record.encoded_polyline, first_snapshot_id=snapshot_id, last_snapshot_id=snapshot_id,
        ))
        self.versions["shape"][record.shape_id] = (row_id, record.content_sha256)
        self.stats["rows_rt_shape_version"] += 1
        return True

    def _store_modifications(self, record: TripModificationsRecord, snapshot_id: int) -> bool:
        if not self._version(
            "trip_modifications", record.modifications_id, record.content_sha256, snapshot_id,
            TripModificationsVersion,
        ):
            return False
        version_id = self._add(TripModificationsVersion(
            modifications_id=record.modifications_id, pattern_id=record.pattern_id,
            content_sha256=record.content_sha256, content_json=record.content_json,
            first_snapshot_id=snapshot_id, last_snapshot_id=snapshot_id,
        ))
        trips = [
            {"version_id": version_id, "trip_id": trip_id, "shape_id": shape_id}
            for trip_id, shape_id in record.selected_trips
        ]
        if trips:
            self.session.connection().execute(insert(table(TripModificationsTrip)).on_conflict_do_nothing(), trips)
        dates = [{"version_id": version_id, "service_date": d} for d in record.service_dates]
        if dates:
            self.session.connection().execute(insert(table(TripModificationsDate)).on_conflict_do_nothing(), dates)
        self.versions["trip_modifications"][record.modifications_id] = (version_id, record.content_sha256)
        self.stats["rows_trip_modifications_version"] += 1
        return True

    def _store_alert(self, record: AlertRecord, snapshot_id: int) -> bool:
        if not self._version("alert", record.alert_id, record.content_sha256, snapshot_id, AlertVersion):
            return False
        row_id = self._add(AlertVersion(
            alert_id=record.alert_id, content_sha256=record.content_sha256, content_json=record.content_json,
            header_text=record.header_text, route_ids=record.route_ids, first_snapshot_id=snapshot_id,
            last_snapshot_id=snapshot_id,
        ))
        self.versions["alert"][record.alert_id] = (row_id, record.content_sha256)
        self.stats["rows_alert_version"] += 1
        return True

    def _store_alerts(self, message: FeedMessage, snapshot_id: int, result: SnapshotResult) -> None:
        records = alert_records(message)
        new = sum(self._store_alert(r, snapshot_id) for r in records)
        result.counts.update(alerts=len(records), alert_versions=new)

    # ----------------------------------------------------------------- BusTime

    def store_bustime(self, route_id: str, body: bytes, payload: Any, fetched_at: float) -> bool:
        """Store a getroutebyid payload if its canonical hash differs from the
        route's latest version, else bump `last_checked_at`. Returns whether it was new."""
        sha = payload_hash(payload)
        latest = self.bustime.get(route_id)
        with db.write(self.session):
            if latest is not None and latest[1] == sha:
                self.session.exec(
                    update(BusTimeRouteVersion).where(col(BusTimeRouteVersion.id) == latest[0])
                    .values(last_checked_at=fetched_at)
                )
                return False
            blob = filestore.put_bustime(route_id, sha, body, self.data_path)
            row_id = self._add(BusTimeRouteVersion(
                route_id=route_id, route_version=route_version_id(route_id, sha), content_sha256=sha,
                fetched_at=fetched_at, last_checked_at=fetched_at, blob=blob,
            ))
        self.bustime[route_id] = (row_id, sha)
        self.stats["rows_bustime_route_version"] += 1
        return True
