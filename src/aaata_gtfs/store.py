"""Dedup-aware writers: nothing is stored twice when nothing changed.

A `Store` keeps in-memory copies of the last stored state (latest snapshot per
feed, latest position per vehicle, latest trip update and stop prediction per
trip instance, latest version per detour, shape, alert and BusTime route), and
rebuilds them from the DB on construction so a restart inserts nothing new for
data it already has. Each snapshot is processed in one transaction. The rules
per table are in SPEC.md §4.3.
"""

import hashlib
import json
import logging
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aaata_gtfs import filestore
from aaata_gtfs.bustime import payload_hash, route_version_id
from aaata_gtfs.config import DATA_PATH
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


def start_run(conn: sqlite3.Connection, version: str, args: dict[str, Any], started_at: float | None = None) -> int:
    """Record a collector run and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO collector_run (started_at, version, args) VALUES (?, ?, ?)",
            (started_at if started_at is not None else time.time(), version,
             json.dumps(args, sort_keys=True, default=str)),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, stopped_at: float | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE collector_run SET stopped_at = ? WHERE id = ?",
            (stopped_at if stopped_at is not None else time.time(), run_id),
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


_TRIP_FIELDS = (
    "vehicle_id", "update_ts", "schedule_relationship", "modifications_id", "delay",
    "first_stop_sequence", "n_stop_time_updates", "route_id",
)

_VEHICLE_COLUMNS = (
    "vehicle_id", "fix_ts", "lat", "lon", "bearing", "speed_mps", "trip_id", "route_id",
    "service_date", "trip_schedule_relationship", "direction_id", "start_date", "start_time",
    "current_stop_sequence", "stop_id", "current_status", "occupancy_status", "congestion_level",
    "odometer", "vehicle_label", "snapshot_id",
)


def _trip_fields(record: TripUpdateRecord) -> tuple[object, ...]:
    return (
        record.vehicle_id, record.update_ts, record.schedule_relationship, record.modifications_id,
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
        conn: sqlite3.Connection,
        static: StaticGtfs,
        run_id: int,
        data_path: Path = DATA_PATH,
        archive_raw: bool = True,
        now: float | None = None,
    ) -> None:
        self.conn = conn
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
        conn = self.conn
        self.last_snapshot: dict[str, _LastSnapshot] = {}
        for feed in FEEDS:
            row = conn.execute(
                "SELECT id, header_ts, sha256 FROM feed_snapshot WHERE feed = ? ORDER BY header_ts DESC LIMIT 1",
                (feed,),
            ).fetchone()
            if row:
                self.last_snapshot[feed] = _LastSnapshot(*row)

        # Vehicles present in the latest vehicles snapshot, with their open interval.
        self.open_seen: dict[str, int] = {}
        last_vehicles = self.last_snapshot.get("vehicles")
        if last_vehicles is not None:
            self.open_seen = dict(conn.execute(
                "SELECT vehicle_id, first_snapshot_id FROM vehicle_seen WHERE last_snapshot_id = ?",
                (last_vehicles.id,),
            ).fetchall())

        cutoff_ts = int(now - CACHE_LOAD_HOURS * 3600)
        min_snapshot = conn.execute(
            "SELECT MIN(id) FROM feed_snapshot WHERE header_ts >= ?", (cutoff_ts,)
        ).fetchone()[0]
        min_snapshot = min_snapshot if min_snapshot is not None else 1 << 62

        # Latest position per vehicle.
        self.last_fix: dict[str, tuple[int, tuple[object, ...]]] = {}
        cols = ", ".join(_VEHICLE_COLUMNS[2:-1])
        for row in conn.execute(
            f"SELECT vp.vehicle_id, vp.fix_ts, {cols} FROM vehicle_position vp "
            "JOIN (SELECT vehicle_id, MAX(fix_ts) AS fix_ts FROM vehicle_position WHERE fix_ts >= ? "
            "GROUP BY vehicle_id) m USING (vehicle_id, fix_ts)",
            (cutoff_ts,),
        ):
            self.last_fix[row[0]] = (row[1], tuple(row[2:]))

        # Latest trip update per trip instance.
        self.trips: dict[tuple[str, str], _TripState] = {}
        for row in conn.execute(
            f"SELECT tu.id, tu.trip_id, tu.service_date, {', '.join('tu.' + f for f in _TRIP_FIELDS)} "
            "FROM trip_update tu JOIN (SELECT MAX(id) AS id FROM trip_update WHERE snapshot_id >= ? "
            "GROUP BY trip_id, service_date) m ON tu.id = m.id",
            (min_snapshot,),
        ):
            self.trips[(row[1], row[2] or "")] = _TripState(row[0], tuple(row[3:]), now)

        # Latest version of each versioned entity: (row id, content sha256).
        self.versions: dict[str, dict[str, tuple[int, str]]] = {}
        for kind, table, id_col in (
            ("trip_modifications", "trip_modifications_version", "modifications_id"),
            ("shape", "rt_shape_version", "shape_id"),
            ("alert", "alert_version", "alert_id"),
        ):
            self.versions[kind] = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    f"SELECT t.{id_col}, t.id, t.content_sha256 FROM {table} t "
                    f"JOIN (SELECT MAX(id) AS id FROM {table} GROUP BY {id_col}) m ON t.id = m.id"
                )
            }
        self.bustime: dict[str, tuple[int, str]] = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT b.route_id, b.id, b.content_sha256 FROM bustime_route_version b "
                "JOIN (SELECT MAX(id) AS id FROM bustime_route_version GROUP BY route_id) m ON b.id = m.id"
            )
        }

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

        with self.conn:
            blob = filestore.append_rt(feed, h, payload, self.data_path) if self.archive_raw else None
            cur = self.conn.execute(
                "INSERT INTO feed_snapshot (feed, header_ts, fetched_at, sha256, n_entities, n_bytes, blob, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (feed, h, fetched_at, sha, len(message.entity), len(payload), blob, self.run_id),
            )
            snapshot_id = cur.lastrowid
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
        exists = self.conn.execute(
            "SELECT 1 FROM feed_snapshot WHERE feed = ? AND header_ts = ?", (feed, h)
        ).fetchone()
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
            rows.append((record.vehicle_id, record.fix_ts, *content, snapshot_id))
        for row in rows:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO vehicle_position ({', '.join(_VEHICLE_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_VEHICLE_COLUMNS))})",
                row,
            )
            new_fixes += cur.rowcount
        self.stats["conflicts"] += conflicts

        # Presence intervals: extend those open in the previous snapshot, open new ones.
        open_seen = {}
        for vehicle_id in records:
            first = self.open_seen.get(vehicle_id)
            if first is not None:
                self.conn.execute(
                    "UPDATE vehicle_seen SET last_snapshot_id = ? WHERE vehicle_id = ? AND first_snapshot_id = ?",
                    (snapshot_id, vehicle_id, first),
                )
                open_seen[vehicle_id] = first
            else:
                self.conn.execute(
                    "INSERT INTO vehicle_seen (vehicle_id, first_snapshot_id, last_snapshot_id) VALUES (?, ?, ?)",
                    (vehicle_id, snapshot_id, snapshot_id),
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
                self.conn.execute(
                    "UPDATE trip_update SET last_seen_snapshot_id = ? WHERE id = ?", (snapshot_id, state.row_id)
                )
                state.last_seen = now
                unchanged += 1
                continue
            cur = self.conn.execute(
                "INSERT INTO trip_update (trip_id, service_date, route_id, vehicle_id, update_ts, "
                "schedule_relationship, modifications_id, delay, first_stop_sequence, n_stop_time_updates, "
                "snapshot_id, last_seen_snapshot_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.trip_id, service_date, record.route_id, record.vehicle_id, record.update_ts,
                 record.schedule_relationship, record.modifications_id, record.delay,
                 record.first_stop_sequence, record.n_stop_time_updates, snapshot_id, snapshot_id),
            )
            row_id = cur.lastrowid
            assert row_id is not None
            self.trips[key] = _TripState(row_id, fields, now)
            tu_rows += 1
        result.counts.update(
            tu_rows=tu_rows, unchanged=unchanged, shape_versions=shapes,
            modification_versions=mods, rows_trip_update=tu_rows,
        )

    def _version(self, kind: str, entity_id: str, sha: str, snapshot_id: int, table: str) -> bool:
        """Advance the latest version's `last_snapshot_id` if its content is
        unchanged and return False; return True if a new version is needed."""
        latest = self.versions[kind].get(entity_id)
        if latest is not None and latest[1] == sha:
            self.conn.execute(f"UPDATE {table} SET last_snapshot_id = ? WHERE id = ?", (snapshot_id, latest[0]))
            return False
        return True

    def _store_shape(self, record: RtShapeRecord, snapshot_id: int) -> bool:
        if not self._version("shape", record.shape_id, record.content_sha256, snapshot_id, "rt_shape_version"):
            return False
        cur = self.conn.execute(
            "INSERT INTO rt_shape_version (shape_id, content_sha256, encoded_polyline, first_snapshot_id, "
            "last_snapshot_id) VALUES (?, ?, ?, ?, ?)",
            (record.shape_id, record.content_sha256, record.encoded_polyline, snapshot_id, snapshot_id),
        )
        assert cur.lastrowid is not None
        self.versions["shape"][record.shape_id] = (cur.lastrowid, record.content_sha256)
        self.stats["rows_rt_shape_version"] += 1
        return True

    def _store_modifications(self, record: TripModificationsRecord, snapshot_id: int) -> bool:
        if not self._version(
            "trip_modifications", record.modifications_id, record.content_sha256, snapshot_id,
            "trip_modifications_version",
        ):
            return False
        cur = self.conn.execute(
            "INSERT INTO trip_modifications_version (modifications_id, pattern_id, content_sha256, content_json, "
            "first_snapshot_id, last_snapshot_id) VALUES (?, ?, ?, ?, ?, ?)",
            (record.modifications_id, record.pattern_id, record.content_sha256, record.content_json,
             snapshot_id, snapshot_id),
        )
        version_id = cur.lastrowid
        assert version_id is not None
        self.conn.executemany(
            "INSERT OR IGNORE INTO trip_modifications_trip (version_id, trip_id, shape_id) VALUES (?, ?, ?)",
            [(version_id, trip_id, shape_id) for trip_id, shape_id in record.selected_trips],
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO trip_modifications_date (version_id, service_date) VALUES (?, ?)",
            [(version_id, d) for d in record.service_dates],
        )
        self.versions["trip_modifications"][record.modifications_id] = (version_id, record.content_sha256)
        self.stats["rows_trip_modifications_version"] += 1
        return True

    def _store_alert(self, record: AlertRecord, snapshot_id: int) -> bool:
        if not self._version("alert", record.alert_id, record.content_sha256, snapshot_id, "alert_version"):
            return False
        cur = self.conn.execute(
            "INSERT INTO alert_version (alert_id, content_sha256, content_json, header_text, route_ids, "
            "first_snapshot_id, last_snapshot_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record.alert_id, record.content_sha256, record.content_json, record.header_text,
             record.route_ids, snapshot_id, snapshot_id),
        )
        assert cur.lastrowid is not None
        self.versions["alert"][record.alert_id] = (cur.lastrowid, record.content_sha256)
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
        with self.conn:
            if latest is not None and latest[1] == sha:
                self.conn.execute(
                    "UPDATE bustime_route_version SET last_checked_at = ? WHERE id = ?", (fetched_at, latest[0])
                )
                return False
            blob = filestore.put_bustime(route_id, sha, body, self.data_path)
            cur = self.conn.execute(
                "INSERT INTO bustime_route_version (route_id, route_version, content_sha256, fetched_at, "
                "last_checked_at, blob) VALUES (?, ?, ?, ?, ?, ?)",
                (route_id, route_version_id(route_id, sha), sha, fetched_at, fetched_at, blob),
            )
        assert cur.lastrowid is not None
        self.bustime[route_id] = (cur.lastrowid, sha)
        self.stats["rows_bustime_route_version"] += 1
        return True
