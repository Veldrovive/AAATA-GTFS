"""BusTime `getroutebyid`, collected hourly as a cross-reference.

It describes a route's current patterns (BusTime `pid`s) with their drawn
paths and stops, plus detours and bulletins. Collections are wrapped
(`{"pas": {"pa": [...]}}`), a single element comes as a bare object, and empty
collections or missing scalars come as `[]`. Only what's needed to map
patterns to GTFS shapes is parsed here; the raw payload is archived.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from aaata_gtfs.config import GETROUTEBYID_URL
from aaata_gtfs.http import get


async def fetch_route_payload(client: httpx.AsyncClient, route_id: str) -> tuple[bytes, dict[str, Any]]:
    """The raw body and parsed JSON of `getroutebyid` for a route."""
    body = (await get(client, GETROUTEBYID_URL.format(route_id=route_id))).content
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError(f"getroutebyid {route_id}: expected a JSON object")
    return body, payload


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sorted_collection(value: Any, key: str, sort_key: Callable[[Any], str]) -> Any:
    """A getroutebyid collection (`{key: [...]}`, `{key: {...}}` or `[]`) with its
    items in a fixed order."""
    if isinstance(value, dict) and isinstance(value.get(key), list):
        return {**value, key: sorted(value[key], key=sort_key)}
    return value


def canonical_payload(payload: Any) -> Any:
    """`payload` with the orderings the API doesn't keep stable put in a fixed order.

    getroutebyid lists a route's patterns in an order that varies between
    fetches of the same data, so patterns, detours and bulletins are sorted.
    Points within a pattern stay in order, since their order is the path.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("route"), dict):
        return payload
    route = dict(payload["route"])
    by_id: Callable[[Any], str] = lambda item: str(item.get("id")) if isinstance(item, dict) else _canonical_json(item)
    for field, key, sort_key in (("pas", "pa", by_id), ("dtrs", "dtr", by_id), ("sbs", "sb", _canonical_json)):
        if field in route:
            route[field] = _sorted_collection(route[field], key, sort_key)
    return {**payload, "route": route}


def payload_hash(payload: Any) -> str:
    """SHA-256 of a payload's canonical JSON: key order and the order of
    patterns, detours and bulletins don't matter."""
    return hashlib.sha256(_canonical_json(canonical_payload(payload)).encode()).hexdigest()


def route_version_id(route_id: str, content_sha256: str) -> str:
    """`<route_id>-<sha12>`, as in the old project."""
    return f"{route_id}-{content_sha256[:12]}"


def _ensure_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _unwrap(value: Any, key: str) -> list[Any]:
    """Items of a wrapped collection: `{key: [...]}`, `{key: {...}}` or `[]`."""
    if isinstance(value, dict):
        return _ensure_list(value.get(key))
    return []


def _scalar(value: Any) -> str | None:
    """A scalar field; missing ones come as `[]`."""
    if value is None or isinstance(value, (list, dict)):
        return None
    return str(value)


@dataclass(frozen=True)
class BusTimePoint:
    lat: float
    lon: float
    is_bypassed: bool  # `dtr`: on a detoured pattern, part of the regular path it bypasses
    stop_id: str | None  # BusTime stop id (= GTFS stop_code), when a stop is here
    stop_name: str | None


@dataclass(frozen=True)
class BusTimePattern:
    pattern_id: str
    direction: str | None
    points: tuple[BusTimePoint, ...]

    @property
    def is_detoured(self) -> bool:
        return any(p.is_bypassed for p in self.points)

    def regular_stop_ids(self) -> list[str]:
        """Stops in order on the path buses normally take: all of them on an
        undetoured pattern, else the bypassed regular path's (`dtr: true`)."""
        detoured = self.is_detoured
        return [p.stop_id for p in self.points if p.stop_id is not None and p.is_bypassed == detoured]

    def path_stop_ids(self) -> list[str]:
        """Stops in order on the path actually driven (`dtr: false`)."""
        return [p.stop_id for p in self.points if p.stop_id is not None and not p.is_bypassed]


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def parse_patterns(payload: dict[str, Any]) -> list[BusTimePattern]:
    """The route's patterns. Points that don't parse are skipped."""
    route = payload.get("route")
    if not isinstance(route, dict):
        return []
    patterns = []
    for pa in _unwrap(route.get("pas"), "pa"):
        if not isinstance(pa, dict) or _scalar(pa.get("id")) is None:
            continue
        points = []
        for pt in _ensure_list(pa.get("pt")):
            if not isinstance(pt, dict):
                continue
            try:
                lat, lon = float(pt["lat"]), float(pt["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            bs = pt.get("bs")
            stop_id = _scalar(bs.get("id")) if isinstance(bs, dict) else None
            stop_name = _scalar(bs.get("st")) if isinstance(bs, dict) else None
            points.append(BusTimePoint(lat, lon, _truthy(pt.get("dtr")), stop_id, stop_name))
        patterns.append(BusTimePattern(str(pa["id"]), _scalar(pa.get("d")), tuple(points)))
    return patterns
