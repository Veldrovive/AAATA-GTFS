"""Which service day a trip instance belongs to.

GTFS trip ids repeat on every day a trip runs, so a trip *instance* is
`(trip_id, service_date)`. TheRide's realtime feed doesn't send `start_date`,
so it's derived from the observation time and the static schedule.
"""

from datetime import date, datetime, time, timedelta

from aaata_gtfs.config import LOCAL_TZ
from aaata_gtfs.static_gtfs import StaticGtfs

#: A trip instance is plausible from this long before its first departure to
#: this long after its last arrival.
WINDOW_SLACK_S = 2 * 3600


def fmt_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def parse_date(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


def local_date(ts: float) -> date:
    return datetime.fromtimestamp(ts, LOCAL_TZ).date()


def schedule_origin(service_date: str) -> float:
    """Epoch seconds of schedule time 0 on a service day: local noon minus 12 h
    (the GTFS definition, which is right across DST changes)."""
    noon = datetime.combine(parse_date(service_date), time(12), LOCAL_TZ)
    return noon.timestamp() - 12 * 3600


def schedule_ts(service_date: str, seconds: int) -> float:
    """Epoch seconds of a schedule time (seconds after the origin) on a service day."""
    return schedule_origin(service_date) + seconds


def _candidates(static: StaticGtfs, trip_id: str, d0: date) -> list[tuple[str, float, float, float]]:
    """(service_date, scheduled start, window start, window end) for each of
    d0 - 1 and d0 on which the trip runs."""
    key = (trip_id, fmt_date(d0))
    cached = static.service_date_cache.get(key)
    if cached is not None:
        return cached
    out = []
    for d in (d0 - timedelta(days=1), d0):
        service_date = fmt_date(d)
        trip = static.trip(trip_id, service_date)
        if trip is None or not static.runs_on(trip.version, trip.service_id, service_date):
            continue
        span = static.trip_span(trip.version, trip_id)
        if span is None:
            continue
        start = schedule_ts(service_date, span[0])
        end = schedule_ts(service_date, span[1])
        out.append((service_date, start, start - WINDOW_SLACK_S, end + WINDOW_SLACK_S))
    static.service_date_cache[key] = out
    return out


def service_date_for(static: StaticGtfs, trip_id: str, ts: float) -> str | None:
    """The service date of the instance of `trip_id` running around `ts`.

    Of the previous and current local day, the one on which the trip runs and
    whose scheduled window (first departure - 2 h to last arrival + 2 h)
    contains `ts`; if both do, the one with the nearer scheduled start. If
    neither does, today if the trip runs today, else None.
    """
    d0 = local_date(ts)
    candidates = _candidates(static, trip_id, d0)
    inside = [c for c in candidates if c[2] <= ts <= c[3]]
    if inside:
        return min(inside, key=lambda c: abs(ts - c[1]))[0]
    today = fmt_date(d0)
    return today if any(c[0] == today for c in candidates) else None
