import io
import sqlite3
import zipfile
from pathlib import Path

from aaata_gtfs import db
from aaata_gtfs.service_dates import schedule_ts, service_date_for
from aaata_gtfs.static_gtfs import StaticGtfs, content_id, content_sha256, load_static, parse_time
from tests.conftest import HEADER_TS, STATIC_ZIP


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in db.TABLES if t.startswith("static")}


def test_load_counts(static_conn: sqlite3.Connection, tmp_path: Path) -> None:
    c = table_counts(static_conn)
    assert c["static_version"] == 1
    assert c["static_route"] == 2
    assert c["static_trip"] == 452
    assert c["static_stop_time"] == 15257
    assert c["static_stop"] == 132
    assert c["static_calendar"] == 3
    assert c["static_calendar_date"] == 4
    assert static_conn.execute("SELECT COUNT(DISTINCT shape_id) FROM static_shape_point").fetchone()[0] == 4
    version = static_conn.execute(
        "SELECT feed_version, feed_start_date, feed_end_date, blob FROM static_version"
    ).fetchone()
    assert version[:3] == ("S1000302", "20260823", "20270130")
    assert (tmp_path / version[3]).read_bytes() == STATIC_ZIP.read_bytes()


def test_values(static: StaticGtfs) -> None:
    version = static.latest_version()
    assert version is not None
    stop = static.stops(version)["62"]
    assert (stop.stop_code, stop.name) == ("1164", "S - Huron east of 5th")
    assert static.routes(version)["22"].long_name == "Pontiac - Dhu Varren"
    trip = static.trip("18020")
    assert trip is not None and trip.route_id == "4"
    times = static.stop_times(version, "18020")
    assert times[0].departure_s == 17 * 3600 + 47 * 60 and times[0].shape_dist_m == 0.0
    assert times[1].arrival_s == 17 * 3600 + 47 * 60 + 55 and times[1].shape_dist_m == 380.53
    assert static.stop_code_to_id(version)["1164"] == "62"
    assert parse_time("25:01:02") == 25 * 3600 + 62


def test_load_again_is_a_noop(static_conn: sqlite3.Connection, tmp_path: Path) -> None:
    before = table_counts(static_conn)
    version = load_static(static_conn, STATIC_ZIP.read_bytes(), 2000.0, '"etag"', "Sun, 25 Oct 2026", tmp_path)
    assert table_counts(static_conn) == before
    row = static_conn.execute("SELECT id, last_checked_at, etag FROM static_version").fetchone()
    assert row == (version, 2000.0, '"etag"')


def test_rezipped_copy_has_the_same_content_id() -> None:
    original = STATIC_ZIP.read_bytes()
    with zipfile.ZipFile(io.BytesIO(original)) as src:
        members = [(info.filename, src.read(info)) for info in src.infolist()]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as dst:
        for name, data in reversed(members):
            dst.writestr(zipfile.ZipInfo(name, date_time=(2030, 1, 2, 3, 4, 6)), data)
        dst.writestr("__MACOSX/._stops.txt", b"junk")
    rezipped = buffer.getvalue()
    assert rezipped != original
    assert content_id(content_sha256(rezipped)) == content_id(content_sha256(original))


def test_runs_on(static: StaticGtfs) -> None:
    v = static.latest_version()
    assert v is not None
    assert static.runs_on(v, "2", "20261025")  # Sunday service on a Sunday
    assert not static.runs_on(v, "3", "20261025")
    assert static.runs_on(v, "3", "20261120")  # weekday
    assert not static.runs_on(v, "3", "20261119")  # Thanksgiving, removed by calendar_dates
    assert static.runs_on(v, "1", "20261031")  # Saturday
    assert not static.runs_on(v, "1", "20270206")  # after the calendar ends


def test_service_date_on_the_fixture_day(static: StaticGtfs) -> None:
    # Bus 553 on trip 2705010 at the fixture time (Sunday 18:11 local).
    assert service_date_for(static, "2705010", HEADER_TS) == "20261025"


def test_service_date_after_midnight(static: StaticGtfs) -> None:
    # Weekday trip 3902020 runs 23:45 to 24:26 (00:26 the next calendar day).
    v = static.latest_version()
    assert v is not None
    assert static.trip_span(v, "3902020") == (85500, 87960)
    monday_late = schedule_ts("20261026", 87960) - 60  # Tuesday 00:25 local
    assert service_date_for(static, "3902020", monday_late) == "20261026"
    # Tuesday's 06:02 trip, early on Tuesday, is Tuesday's.
    assert service_date_for(static, "3572020", schedule_ts("20261027", 21720)) == "20261027"


def test_service_date_on_a_day_the_trip_does_not_run(static: StaticGtfs) -> None:
    # A weekday trip seen on Sunday afternoon: Saturday and Sunday are both out.
    assert service_date_for(static, "3572020", schedule_ts("20261025", 12 * 3600)) is None
    # Unknown trips have no service date.
    assert service_date_for(static, "no-such-trip", HEADER_TS) is None
