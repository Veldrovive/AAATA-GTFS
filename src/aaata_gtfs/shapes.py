"""Which shape a trip followed at a given time.

A trip normally follows its static GTFS shape. While a trip modification
(detour) selecting it is in effect, it follows the modification's RT shape
instead. `GeometryResolver` implements `geometry.GeometrySource`.
"""

import json
import logging
from bisect import bisect_right
from pathlib import Path

from sqlmodel import Session

from aaata_gtfs import filestore
from aaata_gtfs.bustime import parse_patterns
from aaata_gtfs.config import DATA_PATH
from aaata_gtfs.geometry import RT_STOP_TOLERANCE_FT, STATIC_STOP_TOLERANCE_FT, ShapeGeometry, ShapeStop
from aaata_gtfs.polyline import decode_polyline
from aaata_gtfs.queries import (
    EFFECT_GRACE_S,
    BusTimeRouteVersion,
    ModificationVersion,
    RtShapeVersion,
    all_rt_shapes,
    all_trip_modifications,
    bustime_route_versions,
)
from aaata_gtfs.static_gtfs import StaticGtfs, StaticTrip

log = logging.getLogger(__name__)


class GeometryResolver:
    """`(trip_id, service_date, t) -> ShapeGeometry`, with geometries cached by key.

    Reads every trip modification and RT shape version at construction; call
    `refresh()` to pick up newer ones.
    """

    def __init__(self, session: Session, static: StaticGtfs, data_path: Path = DATA_PATH) -> None:
        self.session = session
        self.static = static
        self.data_path = data_path
        self._geometries: dict[tuple[str, tuple[str, ...]], ShapeGeometry | None] = {}
        self._shape_pids: dict[tuple[str, int], dict[str, str]] = {}
        self.refresh()

    def refresh(self) -> None:
        self.modifications = all_trip_modifications(self.session)
        self._mods_by_trip: dict[str, list[ModificationVersion]] = {}
        for mod in self.modifications:
            for trip_id in mod.trips:
                self._mods_by_trip.setdefault(trip_id, []).append(mod)
        self._rt_shapes: dict[str, list[RtShapeVersion]] = {}
        for shape in all_rt_shapes(self.session):
            self._rt_shapes.setdefault(shape.shape_id, []).append(shape)
        self._bustime: dict[str, list[BusTimeRouteVersion]] = {}
        for version in bustime_route_versions(self.session):
            self._bustime.setdefault(version.route_id, []).append(version)

    def modification_for(
        self, trip_id: str, service_date: str | None, t: float
    ) -> tuple[ModificationVersion, str | None] | None:
        """The detour in effect at `t` that selects the trip (the newest, if
        several), and the shape id it gives the trip."""
        found = [
            m for m in self._mods_by_trip.get(trip_id, ()) if m.in_effect(t) and m.applies_on(service_date)
        ]
        if not found:
            return None
        mod = found[-1]
        return mod, mod.trips[trip_id]

    def modifications_between(self, start: float, end: float) -> list[ModificationVersion]:
        """Detours in effect at any time in `[start, end]`."""
        return [m for m in self.modifications if m.overlaps(start, end)]

    def rt_shape_at(self, shape_id: str, t: float) -> RtShapeVersion | None:
        found = [s for s in self._rt_shapes.get(shape_id, ()) if s.in_effect(t)]
        return found[-1] if found else None

    def rt_shapes_between(self, shape_id: str, start: float, end: float) -> list[RtShapeVersion]:
        """Versions of an RT shape in effect at any time in `[start, end]`, oldest first."""
        return [
            s for s in self._rt_shapes.get(shape_id, ())
            if s.first_ts - EFFECT_GRACE_S <= end and start <= s.last_ts + EFFECT_GRACE_S
        ]

    def trip_stops(self, trip: StaticTrip) -> tuple[ShapeStop, ...]:
        """A static trip's stops in order, as `ShapeStop`s."""
        stops = self.static.stops(trip.version)
        out = []
        for st in self.static.stop_times(trip.version, trip.trip_id):
            stop = stops.get(st.stop_id)
            if stop is None:
                continue
            out.append(ShapeStop(st.stop_id, stop.stop_code, stop.name, stop.lat, stop.lon,
                                 st.stop_sequence, st.shape_dist_m))
        return tuple(out)

    def _build(
        self, key: str, shape_id: str, route_id: str, points: list[tuple[float, float]],
        stops: tuple[ShapeStop, ...], tolerance_ft: float,
    ) -> ShapeGeometry | None:
        # Trips sharing a shape usually share stops too; cache per (shape, stop list).
        cache_key = (key, tuple(s.stop_id for s in stops))
        if cache_key not in self._geometries:
            try:
                self._geometries[cache_key] = ShapeGeometry(key, route_id, points, stops, tolerance_ft, shape_id)
            except ValueError as e:
                log.debug("no geometry for %s: %s", key, e)
                self._geometries[cache_key] = None
        return self._geometries[cache_key]

    def static_geometry(self, trip: StaticTrip) -> ShapeGeometry | None:
        """A static trip's own shape (ignoring detours)."""
        if trip.shape_id is None:
            return None
        key = f"static:{trip.version}:{trip.shape_id}"
        points = [(p.lat, p.lon) for p in self.static.shape_points(trip.version, trip.shape_id)]
        return self._build(
            key, trip.shape_id, trip.route_id, points, self.trip_stops(trip), STATIC_STOP_TOLERANCE_FT
        )

    def rt_geometry(self, shape: RtShapeVersion, trip: StaticTrip | None, route_id: str = "") -> ShapeGeometry | None:
        """An RT (detour) shape, with the static trip's stops if given."""
        stops = self.trip_stops(trip) if trip is not None else ()
        return self._build(
            f"rt:{shape.id}", shape.shape_id, trip.route_id if trip is not None else route_id,
            decode_polyline(shape.encoded_polyline), stops, RT_STOP_TOLERANCE_FT,
        )

    def for_trip(self, trip_id: str, service_date: str | None, t: float) -> ShapeGeometry | None:
        """The geometry a trip instance followed at `t`: its detour's RT shape
        while a detour selecting it is in effect, else its static shape."""
        trip = self.static.trip(trip_id, service_date)
        mod = self.modification_for(trip_id, service_date, t)
        if mod is not None and mod[1] is not None:
            shape = self.rt_shape_at(mod[1], t)
            if shape is not None:
                return self.rt_geometry(shape, trip)
        if trip is None:
            return None
        return self.static_geometry(trip)

    # ----------------------------------------------------- BusTime pattern ids

    def bustime_version_at(self, route_id: str, t: float) -> BusTimeRouteVersion | None:
        """The BusTime route version in effect at `t`: the latest fetched at or
        before it, else the earliest."""
        versions = self._bustime.get(route_id)
        if not versions:
            return None
        i = bisect_right([v.fetched_at for v in versions], t)
        return versions[i - 1] if i else versions[0]

    def shape_pattern_ids(self, static_version: str, bustime: BusTimeRouteVersion) -> dict[str, str]:
        """Static shape id -> BusTime pid for one route, matched by exact stop
        sequence (GTFS stop_code vs pattern stop ids), using only undetoured patterns."""
        key = (static_version, bustime.id)
        cached = self._shape_pids.get(key)
        if cached is not None:
            return cached
        payload = json.loads(filestore.read_blob(bustime.blob, self.data_path))
        by_stops = {
            tuple(p.path_stop_ids()): p.pattern_id for p in parse_patterns(payload) if not p.is_detoured
        }
        stops = self.static.stops(static_version)
        mapping: dict[str, str] = {}
        for trip in self.static.route_trips(static_version, bustime.route_id):
            if trip.shape_id is None or trip.shape_id in mapping:
                continue
            codes = tuple(
                stops[st.stop_id].stop_code or "" if st.stop_id in stops else ""
                for st in self.static.stop_times(static_version, trip.trip_id)
            )
            pid = by_stops.get(codes)
            if pid is not None:
                mapping[trip.shape_id] = pid
        self._shape_pids[key] = mapping
        return mapping

    def pattern_for_trip(self, trip_id: str, service_date: str | None, t: float) -> str | None:
        """The BusTime pattern id a trip instance ran as at `t`: its detour's
        pattern id if detoured, else the pid whose stops match its static shape's."""
        mod = self.modification_for(trip_id, service_date, t)
        if mod is not None and mod[0].pattern_id is not None:
            return mod[0].pattern_id
        trip = self.static.trip(trip_id, service_date)
        if trip is None or trip.shape_id is None:
            return None
        bustime = self.bustime_version_at(trip.route_id, t)
        if bustime is None:
            return None
        return self.shape_pattern_ids(trip.version, bustime).get(trip.shape_id)
