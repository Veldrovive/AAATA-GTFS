"""The database schema as SQLModel tables (SQLAlchemy ORM).

Every table, column, index and constraint name matches the schema the
collector has always created (SPEC.md §4.3), so existing databases are used as
they are. Schema changes go through Alembic (`aaata_gtfs/migrations/`): change
a model here, then add a revision (`uv run alembic revision --autogenerate -m
"..."`) and check what it generated.

Strings are `TEXT` and floats `REAL`, as SQLite stores them; times are UTC
epoch seconds (INTEGER for feed times, REAL for our clock), service dates
`YYYYMMDD` text.
"""

from typing import Any, ClassVar

from sqlalchemy import REAL, CheckConstraint, Column, Index, MetaData, Table, Text, UniqueConstraint
from sqlalchemy.sql.base import ReadOnlyColumnCollection
from sqlmodel import Field, SQLModel


def _text(*, primary_key: bool = False, foreign_key: str | None = None, nullable: bool = True) -> Any:
    if foreign_key is not None:
        return Field(default=None if nullable else ..., sa_type=Text, foreign_key=foreign_key,
                     primary_key=primary_key, nullable=nullable)
    return Field(default=None if nullable else ..., sa_type=Text, primary_key=primary_key, nullable=nullable)


def _real(*, nullable: bool = True) -> Any:
    return Field(default=None if nullable else ..., sa_type=REAL, nullable=nullable)


def _snapshot_fk() -> Any:
    return Field(foreign_key="feed_snapshot.id")


def _static_version_fk() -> Any:
    return Field(sa_type=Text, foreign_key="static_version.id", primary_key=True)


class _Table(SQLModel):
    """Base of every table. Each sets `__tablename__` to the schema's name."""

    # SQLModel declares it as a str-or-callable; every table here gives a str.
    __tablename__: ClassVar[str]  # pyrefly: ignore[bad-override]


# ---------------------------------------------------------------- collection


class CollectorRun(_Table, table=True):
    __tablename__ = "collector_run"

    id: int | None = Field(default=None, primary_key=True)
    started_at: float = _real(nullable=False)
    stopped_at: float | None = _real()
    version: str = _text(nullable=False)
    args: str = _text(nullable=False)


class FeedSnapshot(_Table, table=True):
    """One row per new feed message (new header timestamp)."""

    __tablename__ = "feed_snapshot"
    __table_args__ = (
        CheckConstraint("feed IN ('vehicles','trips','alerts')"),
        UniqueConstraint("feed", "header_ts"),
    )

    id: int | None = Field(default=None, primary_key=True)
    feed: str = _text(nullable=False)
    header_ts: int
    fetched_at: float = _real(nullable=False)
    sha256: str = _text(nullable=False)
    n_entities: int
    n_bytes: int
    blob: str | None = _text()
    run_id: int = Field(foreign_key="collector_run.id")


# ---------------------------------------------------------------- vehicles


class VehiclePosition(_Table, table=True):
    """One row per `(vehicle_id, fix_ts)`: a stale fix repeated across refreshes is stored once."""

    __tablename__ = "vehicle_position"
    __table_args__ = (
        Index("vehicle_position_ts", "fix_ts"),
        Index("vehicle_position_trip", "trip_id", "service_date", "fix_ts"),
        {"sqlite_with_rowid": False},
    )

    vehicle_id: str = _text(primary_key=True, nullable=False)
    fix_ts: int = Field(primary_key=True, sa_column_kwargs={"autoincrement": False})
    lat: float = _real(nullable=False)
    lon: float = _real(nullable=False)
    bearing: float | None = _real()
    speed_mps: float | None = _real()
    trip_id: str | None = _text()
    route_id: str | None = _text()
    service_date: str | None = _text()
    trip_schedule_relationship: str | None = _text()
    direction_id: int | None = None
    start_date: str | None = _text()
    start_time: str | None = _text()
    current_stop_sequence: int | None = None
    stop_id: str | None = _text()
    current_status: str | None = _text()
    occupancy_status: str | None = _text()
    congestion_level: str | None = _text()
    odometer: float | None = _real()
    vehicle_label: str | None = _text()
    snapshot_id: int = _snapshot_fk()


class VehicleSeen(_Table, table=True):
    """A contiguous run of vehicles snapshots a vehicle appeared in."""

    __tablename__ = "vehicle_seen"
    __table_args__ = (Index("vehicle_seen_last", "last_snapshot_id"), {"sqlite_with_rowid": False})

    vehicle_id: str = _text(primary_key=True, nullable=False)
    first_snapshot_id: int = Field(foreign_key="feed_snapshot.id", primary_key=True,
                                   sa_column_kwargs={"autoincrement": False})
    last_snapshot_id: int = _snapshot_fk()


# ---------------------------------------------------------------- trips


class TripUpdate(_Table, table=True):
    """A trip instance's trip-level fields, versioned when they change.

    Per-stop predictions aren't stored (the raw trips archive keeps them).
    """

    __tablename__ = "trip_update"
    __table_args__ = (
        Index("trip_update_trip", "trip_id", "service_date", "snapshot_id"),
        Index("trip_update_vehicle", "vehicle_id", "snapshot_id"),
        Index("trip_update_snapshot", "snapshot_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    trip_id: str = _text(nullable=False)
    service_date: str | None = _text()
    route_id: str | None = _text()
    vehicle_id: str | None = _text()
    update_ts: int | None = None
    schedule_relationship: str | None = _text()
    modifications_id: str | None = _text()
    delay: int | None = None
    first_stop_sequence: int | None = None
    n_stop_time_updates: int
    snapshot_id: int = _snapshot_fk()  # observed in
    last_seen_snapshot_id: int = _snapshot_fk()  # still identical up to


class TripModificationsVersion(_Table, table=True):
    __tablename__ = "trip_modifications_version"
    __table_args__ = (Index("trip_modifications_version_id", "modifications_id", "first_snapshot_id"),)

    id: int | None = Field(default=None, primary_key=True)
    modifications_id: str = _text(nullable=False)
    pattern_id: str | None = _text()
    content_sha256: str = _text(nullable=False)
    content_json: str = _text(nullable=False)
    first_snapshot_id: int = _snapshot_fk()
    last_snapshot_id: int = _snapshot_fk()


class TripModificationsTrip(_Table, table=True):
    __tablename__ = "trip_modifications_trip"
    __table_args__ = (Index("trip_modifications_trip_trip", "trip_id"), {"sqlite_with_rowid": False})

    version_id: int = Field(foreign_key="trip_modifications_version.id", primary_key=True,
                            sa_column_kwargs={"autoincrement": False})
    trip_id: str = _text(primary_key=True, nullable=False)
    shape_id: str | None = _text()


class TripModificationsDate(_Table, table=True):
    __tablename__ = "trip_modifications_date"
    __table_args__ = ({"sqlite_with_rowid": False},)

    version_id: int = Field(foreign_key="trip_modifications_version.id", primary_key=True,
                            sa_column_kwargs={"autoincrement": False})
    service_date: str = _text(primary_key=True, nullable=False)


class RtShapeVersion(_Table, table=True):
    __tablename__ = "rt_shape_version"
    __table_args__ = (Index("rt_shape_version_id", "shape_id", "first_snapshot_id"),)

    id: int | None = Field(default=None, primary_key=True)
    shape_id: str = _text(nullable=False)
    content_sha256: str = _text(nullable=False)
    encoded_polyline: str = _text(nullable=False)
    first_snapshot_id: int = _snapshot_fk()
    last_snapshot_id: int = _snapshot_fk()


# ---------------------------------------------------------------- alerts


class AlertVersion(_Table, table=True):
    __tablename__ = "alert_version"
    __table_args__ = (Index("alert_version_id", "alert_id", "first_snapshot_id"),)

    id: int | None = Field(default=None, primary_key=True)
    alert_id: str = _text(nullable=False)
    content_sha256: str = _text(nullable=False)
    content_json: str = _text(nullable=False)
    header_text: str | None = _text()
    route_ids: str | None = _text()
    first_snapshot_id: int = _snapshot_fk()
    last_snapshot_id: int = _snapshot_fk()


# ---------------------------------------------------------------- static GTFS


class StaticVersion(_Table, table=True):
    __tablename__ = "static_version"

    id: str = _text(primary_key=True, nullable=False)
    content_sha256: str = Field(sa_type=Text, unique=True)
    zip_sha256: str = _text(nullable=False)
    fetched_at: float = _real(nullable=False)
    last_checked_at: float = _real(nullable=False)
    etag: str | None = _text()
    last_modified: str | None = _text()
    feed_version: str | None = _text()
    feed_start_date: str | None = _text()
    feed_end_date: str | None = _text()
    blob: str = _text(nullable=False)


class StaticRoute(_Table, table=True):
    __tablename__ = "static_route"
    __table_args__ = ({"sqlite_with_rowid": False},)

    static_version: str = _static_version_fk()
    route_id: str = _text(primary_key=True, nullable=False)
    short_name: str | None = _text()
    long_name: str | None = _text()
    route_type: int | None = None
    color: str | None = _text()
    text_color: str | None = _text()


class StaticStop(_Table, table=True):
    __tablename__ = "static_stop"
    __table_args__ = ({"sqlite_with_rowid": False},)

    static_version: str = _static_version_fk()
    stop_id: str = _text(primary_key=True, nullable=False)
    stop_code: str | None = _text()
    name: str | None = _text()
    lat: float | None = _real()
    lon: float | None = _real()
    wheelchair_boarding: int | None = None


class StaticTrip(_Table, table=True):
    __tablename__ = "static_trip"
    __table_args__ = (Index("static_trip_trip", "trip_id"), {"sqlite_with_rowid": False})

    static_version: str = _static_version_fk()
    trip_id: str = _text(primary_key=True, nullable=False)
    route_id: str = _text(nullable=False)
    service_id: str = _text(nullable=False)
    headsign: str | None = _text()
    direction_id: int | None = None
    block_id: str | None = _text()
    shape_id: str | None = _text()


class StaticStopTime(_Table, table=True):
    """Times are seconds after the service day's local noon − 12 h (can exceed 24 h)."""

    __tablename__ = "static_stop_time"
    __table_args__ = ({"sqlite_with_rowid": False},)

    static_version: str = _static_version_fk()
    trip_id: str = _text(primary_key=True, nullable=False)
    stop_sequence: int = Field(primary_key=True, sa_column_kwargs={"autoincrement": False})
    stop_id: str = _text(nullable=False)
    arrival_s: int | None = None
    departure_s: int | None = None
    shape_dist_m: float | None = _real()
    timepoint: int | None = None
    pickup_type: int | None = None
    drop_off_type: int | None = None
    stop_headsign: str | None = _text()


class StaticShapePoint(_Table, table=True):
    __tablename__ = "static_shape_point"
    __table_args__ = ({"sqlite_with_rowid": False},)

    static_version: str = _static_version_fk()
    shape_id: str = _text(primary_key=True, nullable=False)
    seq: int = Field(primary_key=True, sa_column_kwargs={"autoincrement": False})
    lat: float = _real(nullable=False)
    lon: float = _real(nullable=False)
    dist_m: float | None = _real()


class StaticCalendar(_Table, table=True):
    __tablename__ = "static_calendar"
    __table_args__ = ({"sqlite_with_rowid": False},)

    static_version: str = _static_version_fk()
    service_id: str = _text(primary_key=True, nullable=False)
    days: str = _text(nullable=False)  # seven 0/1 characters, Monday first
    start_date: str = _text(nullable=False)
    end_date: str = _text(nullable=False)


class StaticCalendarDate(_Table, table=True):
    __tablename__ = "static_calendar_date"
    __table_args__ = ({"sqlite_with_rowid": False},)

    static_version: str = _static_version_fk()
    service_id: str = _text(primary_key=True, nullable=False)
    date: str = _text(primary_key=True, nullable=False)
    exception_type: int


# ---------------------------------------------------------------- BusTime


class BusTimeRouteVersion(_Table, table=True):
    __tablename__ = "bustime_route_version"
    __table_args__ = (Index("bustime_route_version_route", "route_id", "fetched_at"),)

    id: int | None = Field(default=None, primary_key=True)
    route_id: str = _text(nullable=False)
    route_version: str = _text(nullable=False)
    content_sha256: str = _text(nullable=False)
    fetched_at: float = _real(nullable=False)
    last_checked_at: float = _real(nullable=False)
    blob: str = _text(nullable=False)


metadata: MetaData = SQLModel.metadata


def table(model: type[SQLModel]) -> Table:
    """A model's table (typed; SQLModel classes don't declare `__table__`)."""
    t = getattr(model, "__table__")
    assert isinstance(t, Table)
    return t


def columns(model: type[SQLModel]) -> ReadOnlyColumnCollection[str, Column[Any]]:
    """A model's columns, for SQLAlchemy `select`s of several of them: SQLModel's
    class attributes are typed as their Python values, not columns."""
    return table(model).c

#: Every table, in the order they're reported (e.g. by collector_status.py).
TABLES: list[type[SQLModel]] = [
    CollectorRun, FeedSnapshot, VehiclePosition, VehicleSeen, TripUpdate,
    TripModificationsVersion, TripModificationsTrip, TripModificationsDate, RtShapeVersion,
    AlertVersion, StaticVersion, StaticRoute, StaticStop, StaticTrip, StaticStopTime,
    StaticShapePoint, StaticCalendar, StaticCalendarDate, BusTimeRouteVersion,
]
