"""Linear referencing: positions as distance along a shape instead of lat/lon.

A `ShapeGeometry` is one shape (static GTFS or a GTFS-RT detour shape) as a
polyline in a local flat coordinate system measured in feet (an
equirectangular projection around its first point, accurate to well under 0.1%
at city scale). Positions are projected onto it to get
`(dist_along_ft, offset_ft)`; a large offset means the bus is off the path
(detour, deadheading, GPS garbage).

Stops lie on shape vertices. Like BusTime's paths, GTFS shapes draw many stops
as a short out-and-back jog to the curb (`A, STOP, A`); those spurs are cut,
since the agency's `shape_dist_traveled` doesn't count them.

Ported from the old project's `PatternGeometry`.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from math import cos, radians
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

#: Mean Earth radius (6,371,008.8 m) in feet.
EARTH_RADIUS_FT = 20_902_259.0
FT_PER_M = 3.28084

#: Pings further than this from the path are rejected. GPS error and the gap
#: between a street's centerline and its curb lane are well under this; buses on
#: an unmapped detour or deadheading are usually far more.
DEFAULT_MAX_OFFSET_FT = 150.0

#: Limits on what counts as a stop spur (see `ShapeGeometry._remove_stop_spurs`).
#: Observed curb jogs are 1–2 vertices deep and 15–130 ft round trip; real
#: out-and-back trips into side streets or lots are longer.
MAX_STOP_SPUR_VERTICES = 6
MAX_STOP_SPUR_FT = 200.0
#: A spur's return vertex is often a foot or so off its outbound one.
SPUR_RETURN_TOLERANCE_FT = 3.0

#: How close a stop must be to a vertex to sit on it. Static shapes have stops
#: exactly on vertices; RT shapes are encoded at 1e-5° (about 3.6 ft).
STATIC_STOP_TOLERANCE_FT = 1.0
RT_STOP_TOLERANCE_FT = 6.0

#: `project_many` works in chunks of this many points to bound memory.
PROJECT_CHUNK = 2000


@dataclass(frozen=True)
class ShapeStop:
    """A stop of the trip a shape is built for, in trip order."""

    stop_id: str  # GTFS stop_id
    stop_code: str | None  # the number riders see (BusTime's stop id)
    name: str | None
    lat: float
    lon: float
    stop_sequence: int | None = None
    shape_dist_m: float | None = None  # stop_times.shape_dist_traveled (metres)


@dataclass(frozen=True)
class StopPosition:
    """A stop on a shape, located by distance along it."""

    stop_id: str
    stop_code: str | None
    name: str | None
    dist_along_ft: float
    lat: float
    lon: float
    stop_sequence: int | None
    on_vertex: bool  # False when no vertex was near enough and it was located by projection


@dataclass(frozen=True)
class Projection:
    """Where a point lands on a shape."""

    dist_along_ft: float  # distance from the shape's start to the snapped point
    offset_ft: float  # perpendicular distance from the point to the path
    lat: float  # the snapped point
    lon: float


class ShapeGeometry:
    """A shape's path, set up for projecting points onto it.

    Args:
        key: Identifies the source, e.g. `"static:<version>:<shape_id>"` or
            `"rt:<rt_shape_version.id>"`.
        route_id: The route it belongs to.
        points: The shape's (lat, lon) vertices in order.
        stops: The trip's stops in order. Each is attached to the first vertex
            at or after the previous stop's that lies within `stop_tolerance_ft`.
        stop_tolerance_ft: See `STATIC_STOP_TOLERANCE_FT`, `RT_STOP_TOLERANCE_FT`.
        shape_id: The GTFS or RT shape id, for display (defaults to `key`).
    """

    def __init__(
        self,
        key: str,
        route_id: str,
        points: Sequence[tuple[float, float]],
        stops: Sequence[ShapeStop] = (),
        stop_tolerance_ft: float = STATIC_STOP_TOLERANCE_FT,
        shape_id: str | None = None,
    ) -> None:
        if len(points) < 2:
            raise ValueError(f"shape {key} has fewer than two points")
        self.key = key
        self.route_id = route_id
        self.shape_id = shape_id if shape_id is not None else key
        self._lat0, self._lon0 = points[0]
        self._x_scale = EARTH_RADIUS_FT * cos(radians(self._lat0))
        #: How many vertices the input had, before spurs were cut.
        self.n_input_vertices = len(points)

        lats = np.array([p[0] for p in points], dtype=float)
        lons = np.array([p[1] for p in points], dtype=float)
        raw_xy = self._to_xy(lats, lons)
        stop_vertex = self._attach_stops(raw_xy, stops, stop_tolerance_ft)
        path, stop_bases = self._remove_stop_spurs(raw_xy, stop_vertex)

        self.latitudes = lats[path]
        self.longitudes = lons[path]
        self.xy = raw_xy[path]
        # Vertices can repeat; zero-length segments are harmless but skipped.
        segments = np.diff(self.xy, axis=0)
        lengths = np.hypot(segments[:, 0], segments[:, 1])
        self.cum_dist_ft = np.concatenate([[0.0], np.cumsum(lengths)])
        keep = lengths > 0
        if not keep.any():
            raise ValueError(f"shape {key} has zero length")
        self._seg_start = self.xy[:-1][keep]
        self._seg_vec = segments[keep]
        self._seg_len2 = lengths[keep] ** 2
        self._seg_dist0 = self.cum_dist_ft[:-1][keep]

        self.stops: list[StopPosition] = []
        self._agency_dist_ft: dict[int, float] = {}
        previous: float | None = None
        for i, stop in enumerate(stops):
            base = stop_bases.get(i)
            if base is not None:
                # A spur's stop sits off to the side, so locate it on the street
                # beside where its spur left, not at the spur's base vertex.
                near = float(self.cum_dist_ft[base])
            else:
                log.debug("%s: stop %s (seq %s) is on no vertex; located by projection",
                          key, stop.stop_id, stop.stop_sequence)
                near = previous
            dist = self.project(stop.lat, stop.lon, near).dist_along_ft
            previous = dist
            self.stops.append(StopPosition(
                stop.stop_id, stop.stop_code, stop.name, dist, stop.lat, stop.lon,
                stop.stop_sequence, base is not None,
            ))
            if stop.stop_sequence is not None and stop.shape_dist_m is not None:
                self._agency_dist_ft[stop.stop_sequence] = stop.shape_dist_m * FT_PER_M

    def _attach_stops(
        self, xy: np.ndarray, stops: Sequence[ShapeStop], tolerance_ft: float
    ) -> list[int | None]:
        """The vertex each stop sits on (None if none is near enough), walking in trip order."""
        attached: list[int | None] = []
        start = 0
        for stop in stops:
            s_xy = self._to_xy(np.array(stop.lat), np.array(stop.lon))
            dists = np.hypot(*(xy[start:] - s_xy).T)
            hits = np.flatnonzero(dists <= tolerance_ft)
            if len(hits):
                start += int(hits[0])
                attached.append(start)
            else:
                attached.append(None)
        return attached

    def _remove_stop_spurs(
        self, xy_all: np.ndarray, stop_vertex: list[int | None]
    ) -> tuple[list[int], dict[int, int]]:
        """Drop the short out-and-back spurs drawn to many stops.

        Such stops are drawn as a jog off the street to the curb and back, e.g.
        `A, STOP, A` or `A, B, STOP, B, A` (the return vertex is often a foot or
        so off the outbound one). Left in, each adds up to ~120 ft to distances
        along the path, which the agency's `shape_dist_traveled` doesn't count,
        and pulls pings at the curb into the spur. A backtrack is only removed
        if it returns within `SPUR_RETURN_TOLERANCE_FT` of a recent vertex,
        contains a stop, and is at most `MAX_STOP_SPUR_FT` long in total
        (nested spurs included), so real out-and-back trips to a stop stay.

        Returns:
            Indices of the kept vertices, and for each attached stop (by its
            index in the stop list), the index in the kept path it hangs off.
        """
        stops_at: dict[int, list[int]] = {}
        for stop_index, vertex in enumerate(stop_vertex):
            if vertex is not None:
                stops_at.setdefault(vertex, []).append(stop_index)

        path: list[int] = []
        xy: list[np.ndarray] = []
        removed_ft: list[float] = []  # length of spurs already cut from each vertex
        hung: list[tuple[int, int]] = []  # (stop index, index in path)
        for i in range(len(xy_all)):
            p_xy = xy_all[i]
            base = self._spur_base(xy, removed_ft, hung, p_xy)
            if base is not None:
                spur = np.array(xy[base:] + [p_xy])
                removed_ft[base] = float(np.hypot(*np.diff(spur, axis=0).T).sum() + sum(removed_ft[base:]))
                hung = [(s, min(j, base)) for s, j in hung]
                hung.extend((s, base) for s in stops_at.get(i, []))
                del path[base + 1:], xy[base + 1:], removed_ft[base + 1:]
                continue
            path.append(i)
            xy.append(p_xy)
            removed_ft.append(0.0)
            hung.extend((s, len(path) - 1) for s in stops_at.get(i, []))
        return path, dict(hung)

    @staticmethod
    def _spur_base(
        xy: list[np.ndarray],
        removed_ft: list[float],
        hung: list[tuple[int, int]],
        point: np.ndarray,
    ) -> int | None:
        """Index of the vertex `point` returns to if that closes a stop spur."""
        for back in range(1, min(MAX_STOP_SPUR_VERTICES, len(xy) - 1) + 1):
            base = len(xy) - 1 - back
            if np.hypot(*(xy[base] - point)) > SPUR_RETURN_TOLERANCE_FT:
                continue
            # Stops already hung on a vertex by an inner spur count too.
            if not any(j > base for _, j in hung):
                return None
            spur = np.array(xy[base:] + [point])
            length = np.hypot(*np.diff(spur, axis=0).T).sum() + sum(removed_ft[base:])
            return base if length <= MAX_STOP_SPUR_FT else None
        return None

    def __repr__(self) -> str:
        return f"ShapeGeometry(key={self.key!r}, length_ft={self.length_ft:.0f}, stops={len(self.stops)})"

    @property
    def length_ft(self) -> float:
        return float(self.cum_dist_ft[-1])

    @property
    def n_vertices(self) -> int:
        return len(self.xy)

    def _to_xy(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        x = np.radians(lon - self._lon0) * self._x_scale
        y = np.radians(lat - self._lat0) * EARTH_RADIUS_FT
        return np.stack([x, y], axis=-1)

    def _to_latlon(self, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        lat = self._lat0 + np.degrees(xy[..., 1] / EARTH_RADIUS_FT)
        lon = self._lon0 + np.degrees(xy[..., 0] / self._x_scale)
        return lat, lon

    def project_many(
        self,
        latitudes: Sequence[float] | np.ndarray,
        longitudes: Sequence[float] | np.ndarray,
        near_ft: Sequence[float | None] | None = None,
        max_offset_ft: float = DEFAULT_MAX_OFFSET_FT,
    ) -> list[Projection]:
        """Project many points at once. See `project`."""
        lats = np.asarray(latitudes, dtype=float)
        lons = np.asarray(longitudes, dtype=float)
        out: list[Projection] = []
        for start in range(0, len(lats), PROJECT_CHUNK):
            end = start + PROJECT_CHUNK
            near = None if near_ft is None else near_ft[start:end]
            out.extend(self._project_chunk(lats[start:end], lons[start:end], near, max_offset_ft))
        return out

    def _project_chunk(
        self,
        latitudes: np.ndarray,
        longitudes: np.ndarray,
        near_ft: Sequence[float | None] | None,
        max_offset_ft: float,
    ) -> list[Projection]:
        xy = self._to_xy(latitudes, longitudes)
        # Point-to-segment projection for every (point, segment) pair.
        rel = xy[:, None, :] - self._seg_start[None, :, :]
        t = np.clip((rel * self._seg_vec).sum(-1) / self._seg_len2, 0.0, 1.0)
        foot = self._seg_start + t[..., None] * self._seg_vec
        offset = np.hypot(*(xy[:, None, :] - foot).transpose(2, 0, 1))
        along = self._seg_dist0 + t * np.sqrt(self._seg_len2)

        best = offset.argmin(axis=1)
        if near_ft is not None:
            for i, near in enumerate(near_ft):
                if near is not None:
                    best[i] = self._nearest_pass(offset[i], along[i], near, max_offset_ft, int(best[i]))

        rows = np.arange(len(xy))
        lat, lon = self._to_latlon(foot[rows, best])
        return [
            Projection(float(a), float(o), float(la), float(lo))
            for a, o, la, lo in zip(along[rows, best], offset[rows, best], lat, lon)
        ]

    @staticmethod
    def _nearest_pass(
        offset: np.ndarray, along: np.ndarray, near: float, max_offset_ft: float, best: int
    ) -> int:
        """Index of the best segment on the pass of the path nearest `near`.

        A pass is a contiguous run of segments within `max_offset_ft` of the
        point; each is represented by its closest segment. Picking among passes
        rather than segments keeps the hint from dragging the snap along the
        pass the point is actually on.
        """
        close = offset <= max(max_offset_ft, offset[best])
        # Boundaries of each contiguous run of close segments.
        edges = np.flatnonzero(np.diff(np.concatenate([[0], close.astype(np.int8), [0]])))
        passes = [start + int(offset[start:end].argmin()) for start, end in zip(edges[::2], edges[1::2])]
        return min(passes, key=lambda j: abs(along[j] - near))

    def project(
        self,
        lat: float,
        lon: float,
        near_ft: float | None = None,
        max_offset_ft: float = DEFAULT_MAX_OFFSET_FT,
    ) -> Projection:
        """Project a point onto the path.

        Args:
            lat, lon: The point.
            near_ft: Where along the path the point is expected to be, e.g. the
                previous ping's distance. Where the path passes the point more
                than once (loops, out-and-back streets, a terminus at both ends),
                the pass within `max_offset_ft` closest to this is used. Without
                it, the nearest pass wins.
            max_offset_ft: How far from the path counts as on it, for `near_ft`.

        Returns:
            The projection. Check `offset_ft` against a threshold such as
            `DEFAULT_MAX_OFFSET_FT` to reject off-path points.
        """
        return self.project_many([lat], [lon], [near_ft], max_offset_ft)[0]

    def point_at(self, dist_along_ft: float) -> tuple[float, float]:
        """(lat, lon) of the point this far along the path (clamped to it)."""
        d = min(max(dist_along_ft, 0.0), self.length_ft)
        x = np.interp(d, self.cum_dist_ft, self.xy[:, 0])
        y = np.interp(d, self.cum_dist_ft, self.xy[:, 1])
        lat, lon = self._to_latlon(np.array([x, y]))
        return float(lat), float(lon)

    def path_between(self, start_ft: float, end_ft: float) -> list[tuple[float, float]]:
        """(lat, lon) points of the path from `start_ft` to `end_ft` along it
        (clamped to it): both ends plus every vertex in between."""
        start = min(max(start_ft, 0.0), self.length_ft)
        end = min(max(end_ft, start), self.length_ft)
        inner = (self.cum_dist_ft > start) & (self.cum_dist_ft < end)
        middle = list(zip(self.latitudes[inner].tolist(), self.longitudes[inner].tolist()))
        return [self.point_at(start), *middle, self.point_at(end)]

    @property
    def coordinates(self) -> list[tuple[float, float]]:
        """The (spur-free) path's (lat, lon) vertices."""
        return list(zip(self.latitudes.tolist(), self.longitudes.tolist()))

    def stop_distances(self, stop_id: str) -> list[float]:
        """Distances along the path of every visit to a stop (a loop may visit one twice)."""
        return [stop.dist_along_ft for stop in self.stops if stop.stop_id == stop_id]

    def agency_dist_ft(self, stop_sequence: int) -> float | None:
        """The agency's distance to a stop (`shape_dist_traveled`), in feet, if given."""
        return self._agency_dist_ft.get(stop_sequence)


class Sighting(Protocol):
    """A vehicle position, e.g. `queries.PositionRow`."""

    @property
    def trip_id(self) -> str | None: ...
    @property
    def service_date(self) -> str | None: ...
    @property
    def fix_ts(self) -> int: ...
    @property
    def lat(self) -> float: ...
    @property
    def lon(self) -> float: ...


class GeometrySource(Protocol):
    """Picks the geometry a trip followed at a time, e.g. `shapes.GeometryResolver`."""

    def for_trip(self, trip_id: str, service_date: str | None, t: float) -> ShapeGeometry | None: ...


@dataclass(frozen=True)
class SnappedPing[S: Sighting]:
    """A position located on its trip's shape."""

    position: S
    projection: Projection | None  # None when there's no trip or its geometry is unknown
    max_offset_ft: float
    geometry: ShapeGeometry | None = None  # what it was projected onto

    @property
    def on_route(self) -> bool:
        return self.projection is not None and self.projection.offset_ft <= self.max_offset_ft


def snap_sightings[S: Sighting](
    positions: Sequence[S],
    resolver: GeometrySource,
    max_offset_ft: float = DEFAULT_MAX_OFFSET_FT,
) -> list[SnappedPing[S]]:
    """Project one vehicle's time-ordered positions onto their trips' shapes.

    Each position is projected near the previous on-route one on the same
    geometry (same `key`), so a bus is tracked through loops rather than
    jumping to whichever pass of the path is nearest.
    """
    snapped: list[SnappedPing[S]] = []
    last: tuple[str, float] | None = None  # (geometry key, dist_along_ft)
    for position in positions:
        geometry = (
            resolver.for_trip(position.trip_id, position.service_date, position.fix_ts)
            if position.trip_id is not None
            else None
        )
        if geometry is None:
            snapped.append(SnappedPing(position, None, max_offset_ft))
            continue
        near = last[1] if last is not None and last[0] == geometry.key else None
        ping = SnappedPing(
            position, geometry.project(position.lat, position.lon, near, max_offset_ft), max_offset_ft, geometry
        )
        if ping.on_route and ping.projection is not None:
            last = (geometry.key, ping.projection.dist_along_ft)
        snapped.append(ping)
    return snapped
