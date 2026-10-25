"""GTFS-realtime protobuf messages turned into typed records.

Pure functions, no DB: `parse_feed` decodes a payload, and the `*_records`
functions pull each entity kind out of a `FeedMessage` as frozen dataclasses,
which the store layer consumes. Optional protobuf fields are read with
`HasField`, so an absent field is `None`, not `0` or `""`. Enum values become
their names (`"SCHEDULED"`).
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError, Message
from google.transit import gtfs_realtime_pb2 as pb

FeedMessage = pb.FeedMessage

__all__ = [
    "AlertRecord",
    "DecodeError",
    "FeedMessage",
    "RtShapeRecord",
    "StopTimeRecord",
    "TripModificationsRecord",
    "TripUpdateRecord",
    "VehicleRecord",
    "alert_records",
    "alerts_content_sha256",
    "canonical_json",
    "parse_feed",
    "rt_shape_records",
    "trip_modifications_records",
    "trip_update_records",
    "vehicle_records",
]


def parse_feed(payload: bytes) -> pb.FeedMessage:
    """Decode a feed. Raises `DecodeError` on a malformed payload."""
    message = pb.FeedMessage()
    message.ParseFromString(payload)
    if not message.HasField("header"):
        raise DecodeError("feed has no header")
    return message


def header_ts(message: pb.FeedMessage) -> int:
    return message.header.timestamp


def alerts_content_sha256(message: pb.FeedMessage) -> str:
    """SHA-256 of an alerts feed's content: its alerts' canonical hashes, sorted.

    Ignores the header (whose timestamp changes every refresh even when nothing
    else does) and the order of entities and of each alert's translations, which
    TheRide shuffles between fetches.
    """
    hashes = sorted(f"{a.alert_id}\n{a.content_sha256}" for a in alert_records(message))
    return _sha256("\n".join(hashes))


# Small typed accessors for optional fields.


def _int(msg: Message, field: str) -> int | None:
    return int(getattr(msg, field)) if msg.HasField(field) else None


def _float(msg: Message, field: str) -> float | None:
    return float(getattr(msg, field)) if msg.HasField(field) else None


def _str(msg: Message, field: str) -> str | None:
    return str(getattr(msg, field)) if msg.HasField(field) else None


def _enum(msg: Message, field: str) -> str | None:
    """An enum field's value name, or None if it's not set."""
    if not msg.HasField(field):
        return None
    enum_type = msg.DESCRIPTOR.fields_by_name[field].enum_type
    value = int(getattr(msg, field))
    named = enum_type.values_by_number.get(value) if enum_type is not None else None
    return named.name if named is not None else str(value)


def canonical_json(message: Message, sort_lists: Iterable[str] = ()) -> str:
    """Canonical JSON of a message: proto field names, sorted keys, compact.

    Lists named in `sort_lists` (at any depth), whose order isn't meaningful,
    are sorted first.
    """
    value = MessageToDict(message, preserving_proto_field_name=True)
    return json.dumps(_sort_lists(value, frozenset(sort_lists)), sort_keys=True, separators=(",", ":"))


def _sort_lists(value: Any, names: frozenset[str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            item = _sort_lists(item, names)
            if key in names and isinstance(item, list):
                item = sorted(item, key=lambda x: json.dumps(x, sort_keys=True))
            out[key] = item
        return out
    if isinstance(value, list):
        return [_sort_lists(item, names) for item in value]
    return value


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class VehicleRecord:
    vehicle_id: str
    fix_ts: int  # VehiclePosition.timestamp: when the position was measured
    lat: float
    lon: float
    bearing: float | None
    speed_mps: float | None
    trip_id: str | None  # None when out of service
    route_id: str | None
    trip_schedule_relationship: str | None
    # Not sent by TheRide at the time of writing; stored if they ever appear.
    direction_id: int | None = None
    start_date: str | None = None
    start_time: str | None = None
    current_stop_sequence: int | None = None
    stop_id: str | None = None
    current_status: str | None = None
    occupancy_status: str | None = None
    congestion_level: str | None = None
    odometer: float | None = None
    vehicle_label: str | None = None


def vehicle_records(message: pb.FeedMessage) -> list[VehicleRecord]:
    """Every vehicle position with a vehicle id, a fix time and a position."""
    records = []
    for entity in message.entity:
        if not entity.HasField("vehicle"):
            continue
        vp = entity.vehicle
        if not (vp.HasField("vehicle") and vp.vehicle.HasField("id")):
            continue
        if not (vp.HasField("timestamp") and vp.HasField("position")):
            continue
        trip = vp.trip if vp.HasField("trip") else None
        position = vp.position
        records.append(VehicleRecord(
            vehicle_id=vp.vehicle.id,
            fix_ts=vp.timestamp,
            lat=float(position.latitude),
            lon=float(position.longitude),
            bearing=_float(position, "bearing"),
            speed_mps=_float(position, "speed"),
            trip_id=_str(trip, "trip_id") if trip is not None else None,
            route_id=_str(trip, "route_id") if trip is not None else None,
            trip_schedule_relationship=_enum(trip, "schedule_relationship") if trip is not None else None,
            direction_id=_int(trip, "direction_id") if trip is not None else None,
            start_date=_str(trip, "start_date") if trip is not None else None,
            start_time=_str(trip, "start_time") if trip is not None else None,
            current_stop_sequence=_int(vp, "current_stop_sequence"),
            stop_id=_str(vp, "stop_id"),
            current_status=_enum(vp, "current_status"),
            occupancy_status=_enum(vp, "occupancy_status"),
            congestion_level=_enum(vp, "congestion_level"),
            odometer=_float(position, "odometer"),
            vehicle_label=_str(vp.vehicle, "label"),
        ))
    return records


@dataclass(frozen=True)
class StopTimeRecord:
    stop_sequence: int
    stop_id: str  # GTFS stop_id
    arrival_time: int | None
    departure_time: int | None
    arrival_delay: int | None
    departure_delay: int | None
    arrival_uncertainty: int | None
    departure_uncertainty: int | None
    schedule_relationship: str | None

    def content(self) -> tuple[object, ...]:
        """Everything but the stop sequence, for change detection."""
        return (
            self.stop_id, self.arrival_time, self.departure_time, self.arrival_delay,
            self.departure_delay, self.arrival_uncertainty, self.departure_uncertainty,
            self.schedule_relationship,
        )


@dataclass(frozen=True)
class TripUpdateRecord:
    trip_id: str
    route_id: str | None
    vehicle_id: str | None  # None when no bus is assigned yet
    update_ts: int | None  # TripUpdate.timestamp: when the prediction was computed
    schedule_relationship: str | None
    modifications_id: str | None  # trip.modified_trip.modifications_id (a detour)
    delay: int | None
    start_date: str | None
    stops: tuple[StopTimeRecord, ...]

    @property
    def first_stop_sequence(self) -> int | None:
        """The lowest stop sequence still listed: stops already passed are dropped."""
        return min((s.stop_sequence for s in self.stops), default=None)

    @property
    def n_stop_time_updates(self) -> int:
        return len(self.stops)


def _stop_time_record(stu: pb.TripUpdate.StopTimeUpdate) -> StopTimeRecord | None:
    if not (stu.HasField("stop_sequence") and stu.HasField("stop_id")):
        return None
    arrival = stu.arrival if stu.HasField("arrival") else None
    departure = stu.departure if stu.HasField("departure") else None
    return StopTimeRecord(
        stop_sequence=stu.stop_sequence,
        stop_id=stu.stop_id,
        arrival_time=_int(arrival, "time") if arrival is not None else None,
        departure_time=_int(departure, "time") if departure is not None else None,
        arrival_delay=_int(arrival, "delay") if arrival is not None else None,
        departure_delay=_int(departure, "delay") if departure is not None else None,
        arrival_uncertainty=_int(arrival, "uncertainty") if arrival is not None else None,
        departure_uncertainty=_int(departure, "uncertainty") if departure is not None else None,
        schedule_relationship=_enum(stu, "schedule_relationship"),
    )


def trip_update_records(message: pb.FeedMessage) -> list[TripUpdateRecord]:
    """Every trip update with a trip id. Stop-time updates without a stop
    sequence or stop id are left out."""
    records = []
    for entity in message.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip = tu.trip
        if not trip.HasField("trip_id"):
            continue
        stops = tuple(r for r in map(_stop_time_record, tu.stop_time_update) if r is not None)
        modified = trip.modified_trip if trip.HasField("modified_trip") else None
        records.append(TripUpdateRecord(
            trip_id=trip.trip_id,
            route_id=_str(trip, "route_id"),
            vehicle_id=_str(tu.vehicle, "id") if tu.HasField("vehicle") else None,
            update_ts=_int(tu, "timestamp"),
            schedule_relationship=_enum(trip, "schedule_relationship"),
            modifications_id=_str(modified, "modifications_id") if modified is not None else None,
            delay=_int(tu, "delay"),
            start_date=_str(trip, "start_date"),
            stops=tuple(sorted(stops, key=lambda s: s.stop_sequence)),
        ))
    return records


def pattern_id_of(modifications_id: str) -> str | None:
    """The BusTime pattern id in a modification id: its trailing digits."""
    digits = ""
    for ch in reversed(modifications_id):
        if not ch.isdigit():
            break
        digits = ch + digits
    return digits or None


@dataclass(frozen=True)
class TripModificationsRecord:
    modifications_id: str  # FeedEntity.id, e.g. "trip_modifications_501393"
    pattern_id: str | None  # its trailing digits, the BusTime pid
    content_json: str
    content_sha256: str
    selected_trips: tuple[tuple[str, str | None], ...]  # (trip_id, shape_id)
    service_dates: tuple[str, ...]


def trip_modifications_records(message: pb.FeedMessage) -> list[TripModificationsRecord]:
    records = []
    for entity in message.entity:
        if not entity.HasField("trip_modifications"):
            continue
        tm = entity.trip_modifications
        content = canonical_json(tm, sort_lists=("trip_ids", "service_dates", "selected_trips"))
        selected = []
        for sel in tm.selected_trips:
            shape_id = _str(sel, "shape_id")
            selected.extend((trip_id, shape_id) for trip_id in sel.trip_ids)
        records.append(TripModificationsRecord(
            modifications_id=entity.id,
            pattern_id=pattern_id_of(entity.id),
            content_json=content,
            content_sha256=_sha256(content),
            selected_trips=tuple(selected),
            service_dates=tuple(sorted(set(tm.service_dates))),
        ))
    return records


@dataclass(frozen=True)
class RtShapeRecord:
    shape_id: str  # e.g. "shape_501393"
    encoded_polyline: str  # Google polyline, precision 1e-5
    content_sha256: str


def rt_shape_records(message: pb.FeedMessage) -> list[RtShapeRecord]:
    records = []
    for entity in message.entity:
        if not entity.HasField("shape"):
            continue
        shape = entity.shape
        if not shape.HasField("shape_id"):
            continue
        records.append(RtShapeRecord(
            shape_id=shape.shape_id,
            encoded_polyline=shape.encoded_polyline,
            content_sha256=_sha256(canonical_json(shape)),
        ))
    return records


@dataclass(frozen=True)
class AlertRecord:
    alert_id: str  # FeedEntity.id, which is the alert's title
    content_json: str
    content_sha256: str
    header_text: str | None  # English translation
    route_ids: str  # comma-joined informed route ids; "" means system-wide


def _english(text: pb.TranslatedString) -> str | None:
    translations = list(text.translation)
    for t in translations:
        if t.language == "en":
            return t.text
    return translations[0].text if translations else None


def alert_records(message: pb.FeedMessage) -> list[AlertRecord]:
    records = []
    for entity in message.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        # Translation order varies between fetches of the same alert.
        content = canonical_json(alert, sort_lists=("translation", "informed_entity", "active_period"))
        routes = sorted({e.route_id for e in alert.informed_entity if e.HasField("route_id")})
        records.append(AlertRecord(
            alert_id=entity.id,
            content_json=content,
            content_sha256=_sha256(content),
            header_text=_english(alert.header_text) if alert.HasField("header_text") else None,
            route_ids=",".join(routes),
        ))
    return records
