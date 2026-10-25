from pathlib import Path

from aaata_gtfs.config import THERIDE_CREDIT
from aaata_gtfs.queries import positions
from aaata_gtfs.shapes import GeometryResolver
from aaata_gtfs.store import Store
from aaata_gtfs.visualization import latest_positions, plot_route_paths, route_drawings, trace_bus_paths
from tests.conftest import HEADER_TS, fixture_message, with_header


def test_trace_bus_paths_splits_on_gaps_and_route_changes(loaded_store: Store) -> None:
    message = with_header(fixture_message("vehicles.pb"), HEADER_TS + 30)
    for entity in message.entity:
        vehicle = entity.vehicle
        vehicle.timestamp += 30
        if vehicle.vehicle.id == "553":
            vehicle.trip.route_id = "22"  # bus 553 switches route
    loaded_store.store_feed("vehicles", message.SerializeToString(), HEADER_TS + 33)
    rows = positions(loaded_store.conn, 0, HEADER_TS + 30)
    paths = trace_bus_paths(rows, max_gap_s=900)
    bus_553 = [p for p in paths if p.vehicle_id == "553"]
    assert [p.route_id for p in bus_553] == ["22", "4"]
    assert all(len(p.sightings) == 1 for p in bus_553)
    bus_505 = next(p for p in paths if p.vehicle_id == "505")
    assert len(bus_505.sightings) == 2
    assert trace_bus_paths(rows, max_gap_s=10) and len(trace_bus_paths(rows, max_gap_s=10)) > len(paths)
    # Out-of-service paths sort last.
    assert paths[-1].route_id is None


def test_map_renders(loaded_store: Store, tmp_path: Path) -> None:
    conn = loaded_store.conn
    static = loaded_store.static
    start, end = HEADER_TS - 3600, HEADER_TS
    rows = positions(conn, start, end)
    paths = trace_bus_paths(rows)
    latest = latest_positions(conn, end)
    resolver = GeometryResolver(conn, static, tmp_path)
    drawings = route_drawings(static, resolver, ["4", "22"], start, end)
    kinds = {(s.shape_id, s.kind) for d in drawings.values() for s in d.shapes}
    # shp-4-06's Sunday trips are all on detour 501393, drawn as shape_501393.
    assert ("shp-4-06", "detour-regular") in kinds
    assert ("shape_501393", "detour") in kinds
    assert ("shp-4-55", "regular") in kinds and ("shp-22-04", "regular") in kinds
    assert len({s.stop_id for s in drawings["22"].stops}) == len(drawings["22"].stops) > 30

    fmap = plot_route_paths(paths, routes=drawings, resolver=resolver, latest=latest, static=static,
                            ping_view="snapped")
    html = fmap.get_root().render()
    for css_class in ("route-shape", "bus-ping", "ping-raw", "ping-snapped", "bus-latest",
                      "detour-path", "detour-regular", "snapped-pings"):
        assert css_class in html, css_class
    assert "Detours (uncheck for regular paths)" in html
    assert THERIDE_CREDIT in html
    assert "Esri" in html
    # Latest pills (folium JSON-escapes the DivIcon HTML), colored per route.
    pills = html.replace("\\u003c", "<").replace("\\u003e", ">").replace('\\"', '"')
    assert '"className": "bus-latest"' in pills
    for vehicle_id in ("553", "505"):
        assert f">{vehicle_id}</div>" in pills
    assert "Not in service" in html
    assert "Route 4 &middot; bus 553" in html
    assert "ft along shape_501393" in html  # bus 553's detoured trip snapped to the RT shape
    assert "mph" in html


def test_map_without_shapes(loaded_store: Store) -> None:
    rows = positions(loaded_store.conn, HEADER_TS - 3600, HEADER_TS, ["22"])
    html = plot_route_paths(trace_bus_paths(rows), show_points=False).get_root().render()
    assert "bus-ping" in html
    assert "Detours (uncheck" not in html and "raw, joined in time order" not in html
