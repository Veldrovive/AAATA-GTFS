import asyncio
from pathlib import Path

import httpx
import pytest

from aaata_gtfs import db
from aaata_gtfs.collector import Collector, CollectorOptions, next_poll_delay
from aaata_gtfs.config import (
    ALERTS_URL,
    GETROUTEBYID_URL,
    STATIC_URL,
    TRIPS_URL,
    USER_AGENT,
    VEHICLES_URL,
    db_path,
)
from aaata_gtfs.http import backoff_delay, make_client
from tests.conftest import FIXTURES, HEADER_TS, STATIC_ZIP, fixture_bytes, sql

H = 1_000_000


def test_next_poll_after_new_feed() -> None:
    # Due at H + 30 + 3; clamped to [now + 2, now + 45].
    assert next_poll_delay(H + 5, H, 0, 0) == pytest.approx(28)
    assert next_poll_delay(H + 32, H, 0, 0) == 2
    assert next_poll_delay(H - 100, H, 0, 0) == 45


def test_next_poll_while_unchanged_and_stalled() -> None:
    assert next_poll_delay(H + 35, H, 1, 0) == 4
    assert next_poll_delay(H + 60, H, 7, 0) == 4
    assert next_poll_delay(H + 70, H, 8, 0) == 15
    assert next_poll_delay(H + 500, H, 50, 0) == 15
    assert next_poll_delay(H, None, 1, 0) == 4


def test_next_poll_after_errors() -> None:
    assert [next_poll_delay(H, H, 0, n) for n in range(1, 8)] == [5, 10, 20, 40, 80, 120, 120]
    assert backoff_delay(0) == 0


class FakeTheRide:
    """Serves the fixtures and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        route_22 = (FIXTURES / "getroutebyid_22.json").read_bytes()
        self.routes = {
            GETROUTEBYID_URL.format(route_id="22"): route_22,
            GETROUTEBYID_URL.format(route_id="4"): b'{"route":{"id":"4","nm":[],"pas":[]}}',
        }
        self.feeds = {
            VEHICLES_URL: fixture_bytes("vehicles.pb"),
            TRIPS_URL: fixture_bytes("trips.pb"),
            ALERTS_URL: fixture_bytes("alerts.pb"),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.feeds:
            return httpx.Response(200, content=self.feeds[url],
                                  headers={"content-type": "application/x-google-protobuf"})
        if url == STATIC_URL:
            if request.headers.get("if-none-match") == '"v1"':
                return httpx.Response(304, headers={"etag": '"v1"'})
            return httpx.Response(200, content=STATIC_ZIP.read_bytes(), headers={"etag": '"v1"'})
        if url in self.routes:
            return httpx.Response(200, content=self.routes[url])
        return httpx.Response(404)


def test_collector_cycles(tmp_path: Path) -> None:
    fake = FakeTheRide()
    clock = iter(range(HEADER_TS + 3, HEADER_TS + 1000))

    async def run() -> None:
        async with make_client(httpx.MockTransport(fake)) as client:
            options = CollectorOptions(data_path=tmp_path, bustime_courtesy_wait_range_s=(0.0, 0.0))
            collector = Collector(options, client, clock=lambda: float(next(clock)))
            try:
                assert await collector.static_once() is not None
                assert collector.static_ready.is_set()
                assert await collector.realtime_cycle()
                assert collector.latest_header_ts == HEADER_TS
                assert not await collector.realtime_cycle()  # same feeds again: nothing new
                assert (await collector.alerts_once()).stored
                assert await collector.bustime_once() == (2, 2)
                assert await collector.bustime_once() == (2, 0)
                # The second static check is conditional and gets a 304.
                assert await collector.static_once() == collector.static.latest_version()
            finally:
                collector.close()

    asyncio.run(run())

    # Every request identifies us.
    assert fake.requests and all(r.headers["user-agent"] == USER_AGENT for r in fake.requests)
    session = db.connect(db_path(tmp_path), read_only=True)
    counts = {feed: n for feed, n in sql(session, "SELECT feed, COUNT(*) FROM feed_snapshot GROUP BY feed")}
    assert counts == {"vehicles": 1, "trips": 1, "alerts": 1}
    assert sql(session, "SELECT COUNT(*) FROM vehicle_position").scalar_one() == 41
    assert sql(session, "SELECT COUNT(*) FROM vehicle_position WHERE service_date = '20261025'").scalar_one() == 5
    assert sql(session, "SELECT COUNT(*) FROM bustime_route_version").scalar_one() == 2
    assert sql(session, "SELECT COUNT(*) FROM static_version").scalar_one() == 1
    run = sql(session, "SELECT stopped_at FROM collector_run").one()
    assert run[0] is not None
    assert (tmp_path / "blobs" / "rt" / "trips" / "2026" / "10" / "25" / "22.pbs.gz").exists()
