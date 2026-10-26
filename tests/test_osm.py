"""OSM signals: parsing, storage, and locating lights along shapes."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from aaata_gtfs.config import USER_AGENT
from aaata_gtfs.geometry import ShapeGeometry
from aaata_gtfs.http import make_client
from aaata_gtfs.osm import (
    fetch_signals,
    latest_signals_blob,
    lights_on_shape,
    load_signals,
    parse_signals,
    put_signals,
    shapes_bbox,
)

FT_PER_DEG_LAT = 364_000.0
LAT0, LON0 = 42.28, -83.74
FT_PER_DEG_LON = FT_PER_DEG_LAT * 0.74  # cos(42.28°)


def point(north_ft: float, east_ft: float = 0.0) -> tuple[float, float]:
    return LAT0 + north_ft / FT_PER_DEG_LAT, LON0 + east_ft / FT_PER_DEG_LON


def payload(nodes: list[tuple[int, tuple[float, float], dict[str, str]]]) -> bytes:
    return json.dumps({"elements": [
        {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": tags} for i, (lat, lon), tags in nodes
    ]}).encode()


SIGNAL = {"highway": "traffic_signals"}
CROSSING = {"highway": "crossing", "crossing": "traffic_signals"}


def test_parse_signals() -> None:
    signals = parse_signals(payload([(1, point(0), SIGNAL), (2, point(10), CROSSING)]))
    assert [(s.osm_id, s.kind) for s in signals] == [(1, "intersection"), (2, "crossing")]


def test_lights_on_out_and_back_shape() -> None:
    # North 2,000 ft, then back south 30 ft to the east (the other side of the street).
    points = [point(d) for d in range(0, 2001, 100)] + [point(d, 30) for d in range(2000, -1, -100)]
    geometry = ShapeGeometry("test", "T", points)
    signals = parse_signals(payload([
        (1, point(1000, 5), SIGNAL),  # an intersection's two nodes, 40 ft apart
        (2, point(1040, 25), SIGNAL),
        (3, point(1500, 400), SIGNAL),  # a block away: not on the route
        (4, point(300, 15), CROSSING),
    ]))
    lights = lights_on_shape(geometry, signals)
    # Each is met once on the way out and once on the way back.
    assert [(light.osm_ids, light.kind) for light in lights] == [
        ((4,), "crossing"),
        ((1, 2), "intersection"),
        ((2, 1), "intersection"),  # node 2 first on the way back
        ((4,), "crossing"),
    ]
    assert [light.dist_along_ft for light in lights] == pytest.approx([300, 1020, 3020, 3735], abs=15)


def test_store_and_fetch_signals(tmp_path: Path) -> None:
    body = payload([(1, point(0), SIGNAL)])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body)

    async def fetch() -> bytes:
        async with make_client(httpx.MockTransport(handler)) as client:
            return await fetch_signals(client, shapes_bbox([point(0), point(100)]))

    assert asyncio.run(fetch()) == body
    assert seen[0].headers["User-Agent"] == USER_AGENT
    assert 'node["highway"="traffic_signals"]' in seen[0].url.params["data"]

    put_signals(body, 1_792_966_315.0, tmp_path)
    put_signals(body, 1_792_000_000.0, tmp_path)
    latest = latest_signals_blob(tmp_path)
    assert latest is not None and latest.name == "20261025T221155Z.json.gz"
    assert [s.osm_id for s in load_signals(latest)] == [1]
