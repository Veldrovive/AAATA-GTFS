"""Hits TheRide's real feeds. Skipped by default; run with `uv run pytest -m live`."""

import asyncio
import time
import warnings

import pytest

from aaata_gtfs.config import RT_FEED_URLS
from aaata_gtfs.http import get_bytes, make_client
from aaata_gtfs.realtime import header_ts, parse_feed, vehicle_records

pytestmark = pytest.mark.live


@pytest.mark.parametrize("feed", sorted(RT_FEED_URLS))
def test_feed_is_fresh(feed: str) -> None:
    async def fetch() -> bytes:
        async with make_client() as client:
            return await get_bytes(client, RT_FEED_URLS[feed])

    message = parse_feed(asyncio.run(fetch()))
    age = time.time() - header_ts(message)
    assert age < 300, f"{feed} feed is {age:.0f} s old"
    if feed == "vehicles" and not vehicle_records(message):
        warnings.warn("no vehicles in the feed (overnight?)", stacklevel=1)
