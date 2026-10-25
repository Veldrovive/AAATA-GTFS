"""Map of the paths each route's buses have traced (port of the old project's map)."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import folium
from branca.element import MacroElement, Template
from xyzservices import providers

from aaata_gtfs.config import LOCAL_TZ, THERIDE_CREDIT
from aaata_gtfs.geometry import DEFAULT_MAX_OFFSET_FT, GeometrySource, snap_sightings
from aaata_gtfs.polyline import decode_polyline
from aaata_gtfs.queries import PositionRow
from aaata_gtfs.service_dates import fmt_date, schedule_ts
from aaata_gtfs.shapes import GeometryResolver
from aaata_gtfs.static_gtfs import StaticGtfs
from aaata_gtfs.visualization.data import BusPath, LatestPosition

PingView = Literal["raw", "snapped"]

#: Categorical hues, assigned to routes in this fixed order. Only the first three
#: stay distinguishable for colorblind viewers when lines cross, so past that
#: rely on the layer toggles and tooltips to tell routes apart.
ROUTE_COLORS = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#6250d6",  # violet
    "#e34948",  # red
]
#: Buses out of service (no trip).
NOT_IN_SERVICE_COLOR = "#8a8a8a"
NOT_IN_SERVICE_LAYER = "Not in service"

ANN_ARBOR = (42.2808, -83.7430)
MPH_PER_MPS = 2.236936

#: CSS classes on the drawn shapes, so the overlay toggle can hide each kind at once.
ROUTE_SHAPE_CLASS = "route-shape"
BUS_PING_CLASS = "bus-ping"
#: A detour's RT shape, and a static shape whose trips are all detoured. Only
#: one is shown at a time, picked by the overlay toggle's "Detours" checkbox.
DETOUR_PATH_CLASS = "detour-path"
DETOUR_REGULAR_CLASS = "detour-regular"
#: Bus pings are drawn twice: raw positions joined by lines, and snapped onto
#: their trip's shape (no lines, since they'd only retrace the path). The
#: overlay toggle's "Bus pings" radio shows one set at a time.
PING_RAW_CLASS = "ping-raw"
PING_SNAPPED_CLASS = "ping-snapped"
#: Each active bus's latest position, drawn as a labelled marker.
LATEST_POSITION_CLASS = "bus-latest"

ShapeKind = Literal["regular", "detour", "detour-regular"]


class _OverlayToggle(MacroElement):
    """Map control with checkboxes that show or hide route shapes and bus pings.

    Works across all route layers (which stay individually toggleable) by
    toggling an `off-<name>` class on the map container while a box is unchecked;
    the CSS hides the matching shapes. With `has_detours`, a "Detours" box swaps
    detoured shapes between their detour path and their regular path. With
    `has_snapped`, a raw/snapped radio picks which version of the bus pings is
    shown, starting at `ping_view`, via a `snapped-pings` class on the container.
    With `has_latest`, a box shows or hides the latest-position markers.
    """

    def __init__(
        self,
        has_detours: bool,
        has_snapped: bool = False,
        ping_view: PingView = "raw",
        has_latest: bool = False,
    ) -> None:
        super().__init__()
        self.has_detours = has_detours
        self.has_latest = has_latest
        self.has_snapped = has_snapped
        self.ping_view = ping_view

    _template = Template(
        """
        {% macro header(this, kwargs) %}
        <style>
            .off-route-shape .route-shape,
            .off-bus-ping .bus-ping,
            .off-bus-latest .bus-latest,
            .off-detour .detour-path,
            .leaflet-container:not(.off-detour) .detour-regular,
            .snapped-pings .ping-raw,
            .leaflet-container:not(.snapped-pings) .ping-snapped { display: none; }
            .overlay-toggle label { display: block; }
            .overlay-toggle .ping-view { margin-left: 1.2em; }
            .bus-latest div {
                display: inline-block;
                transform: translate(-50%, -50%);
                padding: 1px 5px;
                border: 2px solid white;
                border-radius: 9px;
                box-shadow: 0 0 3px rgba(0, 0, 0, 0.6);
                color: white;
                font: bold 11px/14px sans-serif;
                white-space: nowrap;
            }
        </style>
        {% endmacro %}
        {% macro script(this, kwargs) %}
        (function () {
            var map = {{ this._parent.get_name() }};
            var control = L.control({position: "topright"});
            control.onAdd = function () {
                var div = L.DomUtil.create(
                    "div", "leaflet-control-layers leaflet-control-layers-expanded overlay-toggle");
                L.DomEvent.disableClickPropagation(div);
                var items = [["route-shape", "Route paths & stops"], ["bus-ping", "Bus pings"]];
                {% if this.has_latest %}
                items.push(["bus-latest", "Latest bus positions"]);
                {% endif %}
                {% if this.has_detours %}
                items.push(["detour", "Detours (uncheck for regular paths)"]);
                {% endif %}
                items.forEach(function (item) {
                    var label = L.DomUtil.create("label", "", div);
                    var box = L.DomUtil.create("input", "", label);
                    box.type = "checkbox";
                    box.checked = true;
                    label.appendChild(document.createTextNode(" " + item[1]));
                    L.DomEvent.on(box, "change", function () {
                        map.getContainer().classList.toggle("off-" + item[0], !box.checked);
                    });
                });
                {% if this.has_snapped %}
                var container = map.getContainer();
                container.classList.toggle("snapped-pings", {{ (this.ping_view == "snapped") | tojson }});
                [["raw", "raw, joined in time order"], ["snapped", "snapped to route path"]].forEach(
                    function (view) {
                        var label = L.DomUtil.create("label", "ping-view", div);
                        var radio = L.DomUtil.create("input", "", label);
                        radio.type = "radio";
                        radio.name = "{{ this.get_name() }}-ping-view";
                        radio.checked = view[0] === {{ this.ping_view | tojson }};
                        label.appendChild(document.createTextNode(" " + view[1]));
                        L.DomEvent.on(radio, "change", function () {
                            container.classList.toggle("snapped-pings", view[0] === "snapped");
                        });
                    });
                {% endif %}
                return div;
            };
            control.addTo(map);
        })();
        {% endmacro %}
        """
    )


# --------------------------------------------------------------------- shapes


@dataclass(frozen=True)
class DrawnShape:
    shape_id: str
    coords: list[tuple[float, float]]
    headsigns: tuple[str, ...]
    kind: ShapeKind


@dataclass(frozen=True)
class DrawnStop:
    stop_id: str
    stop_code: str | None
    name: str | None
    lat: float
    lon: float
    headsigns: tuple[str, ...]
    detour_class: str  # "" or DETOUR_REGULAR_CLASS


@dataclass
class RouteDrawing:
    """A route's shapes and stops, as drawn beneath its buses."""

    route_id: str
    short_name: str | None
    long_name: str | None
    shapes: list[DrawnShape] = field(default_factory=list)
    stops: list[DrawnStop] = field(default_factory=list)

    @property
    def title(self) -> str:
        name = f"Route {self.short_name or self.route_id}"
        return name + (f" &middot; {self.long_name}" if self.long_name else "")


def _local_dates(start_ts: float, end_ts: float) -> list[str]:
    """Service dates that can have trips running in `[start_ts, end_ts]`."""
    day = datetime.fromtimestamp(start_ts, LOCAL_TZ).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end_ts, LOCAL_TZ).date()
    dates = []
    while day <= last:
        dates.append(fmt_date(day))
        day += timedelta(days=1)
    return dates


def route_drawings(
    static: StaticGtfs,
    resolver: GeometryResolver,
    route_ids: Iterable[str],
    start_ts: float,
    end_ts: float,
) -> dict[str, RouteDrawing]:
    """Shapes and stops for each route over a time window.

    Each route gets every static shape of its trips in the static version in
    effect at the window end, plus the RT detour shapes in effect during the
    window for its trips. A static shape all of whose trips running in the
    window are selected by an active detour is marked `detour-regular`.
    """
    end_date = fmt_date(datetime.fromtimestamp(end_ts, LOCAL_TZ).date())
    version = static.version_for(end_date)
    if version is None:
        return {}
    dates = _local_dates(start_ts, end_ts)
    mods = resolver.modifications_between(start_ts, end_ts)
    routes = static.routes(version)
    stops = static.stops(version)
    drawings: dict[str, RouteDrawing] = {}
    for route_id in route_ids:
        route = routes.get(route_id)
        trips = static.route_trips(version, route_id)
        if route is None and not trips:
            continue
        drawing = RouteDrawing(route_id, route.short_name if route else None, route.long_name if route else None)
        by_shape: dict[str, list[str]] = defaultdict(list)  # shape_id -> trip ids
        headsigns: dict[str, dict[str, None]] = defaultdict(dict)
        for trip in trips:
            if trip.shape_id is not None:
                by_shape[trip.shape_id].append(trip.trip_id)
                if trip.headsign:
                    headsigns[trip.shape_id][trip.headsign] = None
        trip_by_id = {t.trip_id: t for t in trips}

        def detoured(trip_id: str, service_date: str) -> bool:
            return any(trip_id in m.trips and m.applies_on(service_date) for m in mods)

        def in_window(trip_id: str, service_date: str) -> bool:
            """Whether the trip instance's scheduled span overlaps the window."""
            if not static.runs_on(version, trip_by_id[trip_id].service_id, service_date):
                return False
            span = static.trip_span(version, trip_id)
            return span is not None and (
                schedule_ts(service_date, span[0]) <= end_ts and start_ts <= schedule_ts(service_date, span[1])
            )

        regular_kind: dict[str, ShapeKind] = {}
        for shape_id, trip_ids in sorted(by_shape.items()):
            running = [(trip_id, d) for trip_id in trip_ids for d in dates if in_window(trip_id, d)]
            covered = bool(running) and all(detoured(trip_id, d) for trip_id, d in running)
            regular_kind[shape_id] = "detour-regular" if covered else "regular"
            coords = [(p.lat, p.lon) for p in static.shape_points(version, shape_id)]
            if len(coords) >= 2:
                drawing.shapes.append(
                    DrawnShape(shape_id, coords, tuple(headsigns[shape_id]), regular_kind[shape_id])
                )

        # Detour shapes for this route's trips.
        detour_headsigns: dict[str, dict[str, None]] = defaultdict(dict)
        for mod in mods:
            for trip_id, shape_id in mod.trips.items():
                trip = trip_by_id.get(trip_id)
                if trip is not None and shape_id is not None:
                    detour_headsigns[shape_id].update({trip.headsign: None} if trip.headsign else {})
        for shape_id, signs in sorted(detour_headsigns.items()):
            versions = resolver.rt_shapes_between(shape_id, start_ts, end_ts)
            if not versions:
                continue
            coords = decode_polyline(versions[-1].encoded_polyline)
            if len(coords) >= 2:
                drawing.shapes.append(DrawnShape(shape_id, coords, tuple(signs), "detour"))

        # Stops, once each, with every headsign that serves them.
        stop_signs: dict[str, dict[str, None]] = defaultdict(dict)
        stop_kinds: dict[str, set[ShapeKind]] = defaultdict(set)
        for stop_id, shape_id, headsign in static.route_stop_uses(version, route_id):
            if headsign:
                stop_signs[stop_id][headsign] = None
            stop_kinds[stop_id].add(regular_kind.get(shape_id or "", "regular"))
        for stop_id in sorted(stop_kinds):
            stop = stops.get(stop_id)
            if stop is None:
                continue
            only_regular = stop_kinds[stop_id] == {"detour-regular"}
            drawing.stops.append(DrawnStop(
                stop_id, stop.stop_code, stop.name, stop.lat, stop.lon, tuple(stop_signs[stop_id]),
                DETOUR_REGULAR_CLASS if only_regular else "",
            ))
        drawings[route_id] = drawing
    return drawings


# --------------------------------------------------------------------- drawing


def _route_sort_key(route_id: str) -> tuple[int, str]:
    return (int(route_id), route_id) if route_id.isdigit() else (10**9, route_id)


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, LOCAL_TZ)


def _route_label(route_id: str | None) -> str:
    return f"Route {route_id}" if route_id is not None else "Not in service"


def _headsign(static: StaticGtfs | None, p: PositionRow) -> str | None:
    if static is None or p.trip_id is None:
        return None
    trip = static.trip(p.trip_id, p.service_date)
    return trip.headsign if trip is not None else None


def _describe(path: BusPath, static: StaticGtfs | None) -> str:
    signs = " / ".join(dict.fromkeys(s for p in path.sightings if (s := _headsign(static, p))))
    return (
        f"<b>{_route_label(path.route_id)}</b> &middot; bus {path.vehicle_id}<br>"
        f"{_local(path.start):%b %d %H:%M:%S} – {_local(path.end):%H:%M:%S} ({len(path.sightings)} pings)"
        + (f"<br>{signs}" if signs else "")
    )


def _ping_tooltip(p: PositionRow, static: StaticGtfs | None) -> str:
    lines = [f"{_route_label(p.route_id if p.trip_id else None)} &middot; bus {p.vehicle_id}",
             f"{_local(p.fix_ts):%b %d %H:%M:%S}"]
    motion = []
    if p.speed_mps is not None:
        motion.append(f"{p.speed_mps * MPH_PER_MPS:.0f} mph")
    if p.bearing is not None:
        motion.append(f"bearing {p.bearing:.0f}°")
    if motion:
        lines.append(" &middot; ".join(motion))
    headsign = _headsign(static, p)
    if headsign:
        lines.append(headsign)
    if p.trip_id is not None:
        lines.append(f"trip {p.trip_id}")
    return "<br>".join(lines)


def _add_ping(
    layer: folium.FeatureGroup,
    location: tuple[float, float],
    color: str,
    class_name: str,
    tooltip: str,
    hollow: bool = False,
) -> None:
    folium.CircleMarker(
        location,
        radius=4,
        color=color if hollow else "white",
        weight=2,
        fill=True,
        fill_color="white" if hollow else color,
        fill_opacity=1.0,
        className=class_name,
        tooltip=tooltip,
    ).add_to(layer)


def _add_snapped_pings(
    path: BusPath,
    resolver: GeometrySource,
    color: str,
    layer: folium.FeatureGroup,
    max_offset_ft: float,
    static: StaticGtfs | None,
) -> None:
    """Draw a path's pings snapped onto their trips' shapes; pings that don't
    snap are drawn hollow where they were seen."""
    class_name = f"{BUS_PING_CLASS} {PING_SNAPPED_CLASS}"
    for ping in snap_sightings(path.sightings, resolver, max_offset_ft):
        p = ping.position
        tooltip = _ping_tooltip(p, static)
        projection = ping.projection
        if projection is None or ping.geometry is None:
            tooltip += f"<br><i>not snapped: no shape for trip {p.trip_id}</i>"
        else:
            tooltip += (
                f"<br>{projection.dist_along_ft:,.0f} ft along {ping.geometry.shape_id}"
                f" &middot; {projection.offset_ft:,.0f} ft off it"
            )
        if projection is not None and ping.on_route:
            location = (projection.lat, projection.lon)
        else:
            location = (p.lat, p.lon)
            if projection is not None:
                tooltip += " <i>(too far to snap)</i>"
        _add_ping(layer, location, color, class_name, tooltip, hollow=not ping.on_route)


def _add_latest_position(latest: LatestPosition, color: str, layer: folium.FeatureGroup,
                         static: StaticGtfs | None) -> None:
    """Mark where a bus was at the latest snapshot with a pill showing its fleet number."""
    p = latest.position
    folium.Marker(
        (p.lat, p.lon),
        icon=folium.DivIcon(
            html=f'<div style="background: {color}">{p.vehicle_id}</div>',
            class_name=LATEST_POSITION_CLASS,
            icon_size=(0, 0),
        ),
        tooltip=f"<b>Latest position</b><br>{_ping_tooltip(p, static)}<br>{latest.age_s} s old at "
                f"{_local(latest.snapshot_ts):%H:%M:%S}",
        z_index_offset=1000,
    ).add_to(layer)


def _detour_class(kind: ShapeKind) -> str:
    return {"detour": DETOUR_PATH_CLASS, "detour-regular": DETOUR_REGULAR_CLASS}.get(kind, "")


def _add_route_shape(drawing: RouteDrawing, color: str, layer: folium.FeatureGroup, show_stops: bool) -> None:
    """Draw a route's shapes (detour paths dashed) and its stops."""
    for shape in drawing.shapes:
        is_detour = shape.kind == "detour"
        tooltip = (
            f"<b>{drawing.title}</b><br>{' / '.join(shape.headsigns)}<br>shape {shape.shape_id}"
            + (" &middot; <i>detour</i>" if is_detour else "")
        )
        # Wide and faint so the bus traces drawn on top stay readable.
        folium.PolyLine(
            shape.coords,
            color=color,
            weight=7,
            opacity=0.3,
            dash_array="8 8" if is_detour else None,
            className=f"{ROUTE_SHAPE_CLASS} {_detour_class(shape.kind)}".strip(),
            tooltip=tooltip,
        ).add_to(layer)

    if not show_stops:
        return
    for stop in drawing.stops:
        folium.CircleMarker(
            (stop.lat, stop.lon),
            radius=3,
            color=color,
            weight=2,
            fill=True,
            fill_color="white",
            fill_opacity=1.0,
            className=f"{ROUTE_SHAPE_CLASS} {stop.detour_class}".strip(),
            tooltip=(
                f"<b>{stop.name or stop.stop_id}</b><br>Stop {stop.stop_code or '?'} "
                f"&middot; stop_id {stop.stop_id} &middot; {drawing.title}<br>{' / '.join(stop.headsigns)}"
            ),
        ).add_to(layer)


def plot_route_paths(
    paths: list[BusPath],
    show_points: bool = True,
    routes: Mapping[str, RouteDrawing] | None = None,
    show_stops: bool = True,
    ping_view: PingView = "raw",
    max_offset_ft: float = DEFAULT_MAX_OFFSET_FT,
    resolver: GeometrySource | None = None,
    latest: Mapping[str | None, list[LatestPosition]] | None = None,
    static: StaticGtfs | None = None,
) -> folium.Map:
    """Draw each bus path as a polyline, colored and layered by route.

    Args:
        paths: Paths from `trace_bus_paths`. Out-of-service paths (route None)
            go in a gray "Not in service" layer, raw only.
        show_points: Also mark each individual position along the line.
        routes: Shapes and stops from `route_drawings`, drawn beneath each
            route's buses in the same layer.
        show_stops: Mark each route's stops.
        ping_view: Which bus pings the map opens showing: "raw" positions
            joined by lines, or "snapped" onto their trip's shape (only with a
            `resolver`; the map's toggle switches between them). Pings further
            than `max_offset_ft` from their shape, or with no shape, are drawn
            hollow at their raw position when snapped.
        max_offset_ft: How far from its shape a ping can be and still snap.
        resolver: What to snap pings onto, e.g. a `GeometryResolver`.
        latest: Active buses at the window end, from `latest_positions`, marked
            with their fleet number on top of everything else.
        static: For headsigns in tooltips.
    """
    # Esri needs no API key (unlike CARTO) and no Referer header (unlike OSM), so
    # tiles load when the HTML is opened straight from disk.
    # Providers are attached dynamically, so look this one up by name to keep it typed.
    basemap = providers.query_name("Esri.WorldGrayCanvas")
    fmap = folium.Map(
        location=ANN_ARBOR,
        zoom_start=13,
        tiles=basemap.build_url(),
        attr=f"{basemap.html_attribution} | {THERIDE_CREDIT}",
        max_zoom=basemap.max_zoom,
    )

    by_route: dict[str | None, list[BusPath]] = defaultdict(list)
    for path in paths:
        by_route[path.route_id].append(path)
    shapes = dict(routes or {})
    latest = {route_id: buses for route_id, buses in (latest or {}).items() if buses}

    route_ids = sorted(
        {r for r in (*by_route.keys(), *shapes.keys(), *latest.keys()) if r is not None}, key=_route_sort_key
    )
    layers: list[tuple[str | None, str]] = [
        (route_id, ROUTE_COLORS[i % len(ROUTE_COLORS)]) for i, route_id in enumerate(route_ids)
    ]
    if None in by_route or None in latest:
        layers.append((None, NOT_IN_SERVICE_COLOR))

    snapping = resolver is not None
    for route_id, color in layers:
        layer = folium.FeatureGroup(name=_route_label(route_id) if route_id is not None else NOT_IN_SERVICE_LAYER)
        if route_id is not None and route_id in shapes:
            _add_route_shape(shapes[route_id], color, layer, show_stops)
        for path in by_route.get(route_id, []):
            snap_this = snapping and route_id is not None
            raw_class = f"{BUS_PING_CLASS} {PING_RAW_CLASS}" if snap_this else BUS_PING_CLASS
            if len(path.sightings) > 1:
                folium.PolyLine(
                    path.coordinates,
                    color=color,
                    weight=3,
                    opacity=0.8,
                    className=raw_class,
                    tooltip=_describe(path, static),
                ).add_to(layer)
            if show_points or len(path.sightings) == 1:
                for p in path.sightings:
                    _add_ping(layer, (p.lat, p.lon), color, raw_class, _ping_tooltip(p, static))
            if snap_this and resolver is not None:
                _add_snapped_pings(path, resolver, color, layer, max_offset_ft, static)
        for bus in latest.get(route_id, []):
            _add_latest_position(bus, color, layer, static)
        layer.add_to(fmap)

    coords = [c for path in paths for c in path.coordinates]
    coords += [(b.position.lat, b.position.lon) for buses in latest.values() for b in buses]
    coords += [c for drawing in shapes.values() for shape in drawing.shapes for c in shape.coords]
    if coords:
        lats, lons = zip(*coords)
        fmap.fit_bounds([(min(lats), min(lons)), (max(lats), max(lons))], padding=(20, 20))
    folium.LayerControl(collapsed=False).add_to(fmap)
    _OverlayToggle(
        has_detours=any(s.kind == "detour" for d in shapes.values() for s in d.shapes),
        has_snapped=snapping and any(p.route_id is not None for p in paths),
        ping_view=ping_view,
        has_latest=bool(latest),
    ).add_to(fmap)
    return fmap
