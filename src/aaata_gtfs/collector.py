"""The always-on collector: four independent asyncio loops sharing one client and one `Store`.

| loop            | what                                   | when                                    |
|-----------------|----------------------------------------|-----------------------------------------|
| realtime_loop   | vehicles, then trips                   | aligned to the feed refresh (~30 s)     |
| alerts_loop     | alerts                                 | every 5 min                             |
| static_loop     | static GTFS zip (conditional GET)      | at startup, then every 6 h              |
| bustime_loop    | getroutebyid for every static route    | after the first static load, then hourly|

All DB writes happen on the event loop thread under one `asyncio.Lock`, except
static loading, which runs in a worker thread in its own session (holding
the lock) because it takes seconds.
"""

import asyncio
import logging
import random
import signal
import time
from collections import Counter
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from aaata_gtfs import __version__, db
from aaata_gtfs.bustime import fetch_route_payload
from aaata_gtfs.config import (
    ALERTS_INTERVAL_S,
    ALERTS_URL,
    AVAILABILITY_S,
    BUSTIME_COURTESY_WAIT_RANGE_S,
    BUSTIME_INTERVAL_MIN,
    DATA_PATH,
    MAX_POLL_WAIT_S,
    MIN_POLL_WAIT_S,
    REFRESH_S,
    RETRY_S,
    STALL_AFTER_POLLS,
    STALL_RETRY_S,
    STATIC_INTERVAL_H,
    STATIC_URL,
    TRIPS_URL,
    VEHICLES_URL,
    db_path,
    ensure_data_dir,
)
from aaata_gtfs.http import backoff_delay, get_bytes, make_client
from aaata_gtfs.static_gtfs import FetchResult, StaticGtfs, fetch_static, load_static, touch_static
from aaata_gtfs.store import MalformedFeed, SnapshotResult, Store, finish_run, start_run

log = logging.getLogger(__name__)

#: How long the realtime loop waits for the first static load (so the first
#: snapshots get service dates) when no static version is stored yet.
STATIC_WAIT_S = 120.0
SUMMARY_INTERVAL_S = 3600.0


def next_poll_delay(now: float, latest_header_ts: int | None, unchanged_polls: int, consecutive_errors: int) -> float:
    """Seconds until the next realtime poll.

    - After an error: exponential backoff (5, 10, 20, ... s, capped at 120 s).
    - After a new feed (`unchanged_polls == 0`): when the next one should be
      available, `latest_header_ts + REFRESH_S + AVAILABILITY_S`, clamped to
      `[now + 2 s, now + 45 s]`.
    - While waiting for a refresh: every `RETRY_S` (4 s), or every
      `STALL_RETRY_S` (15 s) after `STALL_AFTER_POLLS` (8) unchanged polls.
    """
    if consecutive_errors > 0:
        return backoff_delay(consecutive_errors)
    if unchanged_polls == 0 and latest_header_ts is not None:
        due = latest_header_ts + REFRESH_S + AVAILABILITY_S - now
        return min(max(due, MIN_POLL_WAIT_S), MAX_POLL_WAIT_S)
    if unchanged_polls >= STALL_AFTER_POLLS:
        return STALL_RETRY_S
    return RETRY_S


@dataclass
class CollectorOptions:
    data_path: Path = DATA_PATH
    archive_raw: bool = True
    alerts_interval_s: float = ALERTS_INTERVAL_S
    static_interval_h: float = STATIC_INTERVAL_H
    bustime_interval_min: float = BUSTIME_INTERVAL_MIN
    bustime: bool = True
    bustime_courtesy_wait_range_s: tuple[float, float] = BUSTIME_COURTESY_WAIT_RANGE_S
    args: dict[str, Any] = field(default_factory=dict)  # recorded in collector_run


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Collector:
    """Owns the DB session, the `Store` and the HTTP client for one run."""

    def __init__(self, options: CollectorOptions, client: httpx.AsyncClient,
                 clock: Callable[[], float] = time.time) -> None:
        self.options = options
        self.client = client
        self.clock = clock
        ensure_data_dir(options.data_path)
        self.db_path = db_path(options.data_path)
        self.session = db.open_db(self.db_path)
        self.run_id = start_run(self.session, __version__, options.args, clock())
        self.static = StaticGtfs(self.session)
        self.store = Store(self.session, self.static, self.run_id, options.data_path, options.archive_raw, clock())
        self.lock = asyncio.Lock()
        self.static_ready = asyncio.Event()
        if self.static.latest_version() is not None:
            self.static_ready.set()
        self.requests: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        # Realtime polling state.
        self.unchanged_polls = 0
        self.consecutive_errors = 0
        self.latest_header_ts: int | None = None
        self._stall_logged = False

    def close(self) -> None:
        finish_run(self.session, self.run_id, self.clock())
        engine = self.session.get_bind()
        self.session.close()
        if isinstance(engine, Engine):
            engine.dispose()

    async def _fetch(self, name: str, url: str) -> bytes:
        self.requests[name] += 1
        return await get_bytes(self.client, url)

    async def _store(self, feed: str, payload: bytes, fetched_at: float) -> SnapshotResult:
        async with self.lock:
            return self.store.store_feed(feed, payload, fetched_at)

    # ---------------------------------------------------------------- realtime

    async def realtime_cycle(self) -> bool:
        """Fetch and store vehicles then trips once. Returns whether either was new.

        Raises on HTTP errors and malformed feeds.
        """
        any_new = False
        for feed, url in (("vehicles", VEHICLES_URL), ("trips", TRIPS_URL)):
            payload = await self._fetch(feed, url)
            fetched_at = self.clock()
            result = await self._store(feed, payload, fetched_at)
            if not result.stored:
                continue
            any_new = True
            if self.latest_header_ts is None or result.header_ts > self.latest_header_ts:
                self.latest_header_ts = result.header_ts
            age = fetched_at - result.header_ts
            if age < 0 or age > 60:
                log.warning("%s: fetched %.0f s after its header time %s; is the clock NTP-synced?",
                            feed, age, _fmt_ts(result.header_ts))
            c = result.counts
            if feed == "vehicles":
                log.info("vehicles H=%d new_fixes=%d/%d%s", result.header_ts, c["new_fixes"], c["vehicles"],
                         f" conflicts={c['conflicts']}" if c["conflicts"] else "")
            else:
                log.info("trips H=%d tu_rows=%d unchanged=%d%s", result.header_ts, c["tu_rows"], c["unchanged"],
                         "".join(f" {k}={c[k]}" for k in ("shape_versions", "modification_versions") if c[k]))
        return any_new

    async def realtime_loop(self) -> None:
        if not self.static_ready.is_set():
            try:
                await asyncio.wait_for(self.static_ready.wait(), STATIC_WAIT_S)
            except TimeoutError:
                log.warning("no static GTFS yet; storing realtime data without service dates")
        while True:
            try:
                new = await self.realtime_cycle()
                self.consecutive_errors = 0
                if new:
                    self.unchanged_polls = 0
                    self._stall_logged = False
                else:
                    self.unchanged_polls += 1
                    if self.unchanged_polls >= STALL_AFTER_POLLS and not self._stall_logged:
                        since = _fmt_ts(self.latest_header_ts) if self.latest_header_ts else "startup"
                        log.warning("feed stalled since H=%s; polling every %.0f s", since, STALL_RETRY_S)
                        self._stall_logged = True
            except (httpx.HTTPError, MalformedFeed, SQLAlchemyError) as e:
                self.consecutive_errors += 1
                self.errors["realtime"] += 1
                log.error("realtime poll failed (%d in a row): %s: %s",
                          self.consecutive_errors, type(e).__name__, e)
            except Exception:
                # A bug shouldn't stop an always-on collector; log it and back off.
                self.consecutive_errors += 1
                self.errors["realtime"] += 1
                log.exception("realtime poll failed unexpectedly (%d in a row)", self.consecutive_errors)
            await asyncio.sleep(
                next_poll_delay(self.clock(), self.latest_header_ts, self.unchanged_polls, self.consecutive_errors)
            )

    # ------------------------------------------------------------------ alerts

    async def alerts_once(self) -> SnapshotResult:
        payload = await self._fetch("alerts", ALERTS_URL)
        result = await self._store("alerts", payload, self.clock())
        if result.stored:
            log.info("alerts H=%d alerts=%d new_versions=%d", result.header_ts, result.counts["alerts"],
                     result.counts["alert_versions"])
        return result

    async def alerts_loop(self) -> None:
        errors = 0
        while True:
            try:
                await self.alerts_once()
                errors = 0
                delay = self.options.alerts_interval_s
            except (httpx.HTTPError, MalformedFeed, SQLAlchemyError) as e:
                errors += 1
                self.errors["alerts"] += 1
                log.error("alerts poll failed (%d in a row): %s: %s", errors, type(e).__name__, e)
                delay = min(backoff_delay(errors), self.options.alerts_interval_s)
            except Exception:
                errors += 1
                self.errors["alerts"] += 1
                log.exception("alerts poll failed unexpectedly (%d in a row)", errors)
                delay = min(backoff_delay(errors), self.options.alerts_interval_s)
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------ static

    def _load_static_in_thread(self, result: FetchResult, fetched_at: float) -> str:
        with Session(self.session.get_bind()) as session:
            return load_static(session, result.content, fetched_at, result.etag, result.last_modified,
                               self.options.data_path)

    async def static_once(self) -> str | None:
        """Check for a new static feed and load it. Returns the version served."""
        latest = self.static.versions[0] if self.static.versions else None
        self.requests["static"] += 1
        result = await fetch_static(
            self.client, latest.etag if latest else None, latest.last_modified if latest else None, STATIC_URL
        )
        now = self.clock()
        async with self.lock:
            if isinstance(result, FetchResult):
                started = time.monotonic()
                known = {v.id for v in self.static.versions}
                version = await asyncio.to_thread(self._load_static_in_thread, result, now)
                if version not in known:
                    log.info("static: loaded new version %s in %.1f s", version, time.monotonic() - started)
                else:
                    log.info("static: unchanged (%s)", version)
            else:
                if latest is None:
                    return None
                version = latest.id
                touch_static(self.session, version, now, result.etag, result.last_modified)
                log.info("static: not modified (%s)", version)
            self.static.reload()
        self.static_ready.set()
        return version

    async def static_loop(self) -> None:
        errors = 0
        while True:
            try:
                await self.static_once()
                errors = 0
                delay = self.options.static_interval_h * 3600
            except (httpx.HTTPError, SQLAlchemyError, OSError, ValueError) as e:
                errors += 1
                self.errors["static"] += 1
                log.error("static fetch failed (%d in a row): %s: %s", errors, type(e).__name__, e)
                delay = backoff_delay(errors)
            except Exception:
                errors += 1
                self.errors["static"] += 1
                log.exception("static fetch failed unexpectedly (%d in a row)", errors)
                delay = backoff_delay(errors)
            await asyncio.sleep(delay)

    # ----------------------------------------------------------------- BusTime

    async def bustime_once(self) -> tuple[int, int]:
        """Fetch getroutebyid for every route in the newest static version.
        Returns (routes fetched, new versions)."""
        version = self.static.latest_version()
        if version is None:
            return 0, 0
        route_ids = sorted(self.static.routes(version), key=lambda r: (not r.isdigit(), r.zfill(6)))
        fetched = new = 0
        low, high = self.options.bustime_courtesy_wait_range_s
        for i, route_id in enumerate(route_ids):
            if i:
                await asyncio.sleep(random.uniform(low, high))
            try:
                self.requests["bustime"] += 1
                body, payload = await fetch_route_payload(self.client, route_id)
                async with self.lock:
                    new += self.store.store_bustime(route_id, body, payload, self.clock())
                fetched += 1
            except (httpx.HTTPError, ValueError, SQLAlchemyError) as e:
                self.errors["bustime"] += 1
                log.error("getroutebyid %s failed: %s: %s", route_id, type(e).__name__, e)
        log.info("bustime: fetched %d/%d routes, %d new versions", fetched, len(route_ids), new)
        return fetched, new

    async def bustime_loop(self) -> None:
        await self.static_ready.wait()
        while True:
            try:
                await self.bustime_once()
            except Exception:
                self.errors["bustime"] += 1
                log.exception("bustime pass failed unexpectedly")
            await asyncio.sleep(self.options.bustime_interval_min * 60)

    # ----------------------------------------------------------------- summary

    def summary(self) -> str:
        stats = self.store.stats
        rows = " ".join(f"{k[5:]}={v}" for k, v in sorted(stats.items()) if k.startswith("rows_"))
        snaps = " ".join(f"{f}={stats[f'snapshots_{f}']}" for f in ("vehicles", "trips", "alerts"))
        return (
            f"summary: requests {dict(self.requests)} snapshots {snaps} rows {rows or '-'} "
            f"conflicts={stats['conflicts']} errors {dict(self.errors) or 0}"
        )

    async def summary_loop(self) -> None:
        while True:
            await asyncio.sleep(SUMMARY_INTERVAL_S)
            log.info(self.summary())
            pruned = self.store.prune(self.clock())
            if pruned:
                log.info("pruned %d trip instances from the caches", pruned)

    async def run(self) -> None:
        loops: list[Callable[[], Coroutine[Any, Any, None]]] = [
            self.static_loop, self.realtime_loop, self.alerts_loop, self.summary_loop,
        ]
        if self.options.bustime:
            loops.append(self.bustime_loop)
        async with asyncio.TaskGroup() as group:
            for loop in loops:
                group.create_task(loop(), name=loop.__name__)


async def run_collector(options: CollectorOptions, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Run until SIGTERM/SIGINT, then record the run's stop time and close the DB."""
    async with make_client(transport) as client:
        collector = Collector(options, client)
        log.info("collector run %d started (data in %s, raw archive %s)", collector.run_id,
                 options.data_path, "on" if options.archive_raw else "off")
        main = asyncio.current_task()
        loop = asyncio.get_running_loop()
        stopping = False

        def stop(signame: str) -> None:
            nonlocal stopping
            if not stopping and main is not None:
                stopping = True
                log.info("%s received; stopping", signame)
                main.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop, sig.name)
        try:
            await collector.run()
        except asyncio.CancelledError:
            if not stopping:
                raise
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
            log.info(collector.summary())
            collector.close()
            log.info("collector run %d stopped", collector.run_id)
