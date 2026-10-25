# aaata-gtfs — project spec

This document is the full brief for building this project. **Phase 1 (the data collector,
its storage, and visualization) is to be implemented in one go from this file.** Phase 2 is
outlined at the end for context only: do not build it yet.

Everything here was checked against the live feeds on 2026-10-25. Where a number is given
(sizes, cadence, counts), it was measured then.

---

## 1. Background and goals

TheRide (AAATA, the Ann Arbor Area Transportation Authority) runs Clever Devices BusTime.
A sibling project, `../AAATA-Bus-API` (package `aaata_bus`), polls the website's
`ServiceData` proxy (`busesforroute`, `predictionsforbus`, `getroutebyid`), stores JSON
Lines, reconstructs trips and draws maps. Its main weakness is that **a bus position from
`busesforroute` carries no time at all**: we only know when we asked, and a position may be
anything from 0 to 60+ s old. It also needs one request per route plus one per bus for
predictions, and prediction times only come to the minute.

TheRide also publishes a public, keyless **GTFS-realtime** feed (not mentioned on their
website) with:
- a **per-vehicle GPS fix timestamp to the second**, plus speed and bearing;
- every vehicle in the system in one ~2.3 KB request;
- trip updates (per-stop predicted arrival/departure **to the second**) for every active or
  upcoming trip in one ~25 KB request;
- detours as GTFS-RT `trip_modifications`, with their own shapes;
- service alerts.

This project is a new system built on GTFS (realtime + static). The old project keeps
running alongside it for a few days so the two datasets can be compared (see
`compare_with_old.py`, §9.4).

**Phase 1 goals**
1. A polite, always-on collector that stores everything useful from the realtime feeds,
   static GTFS and (as a cross-reference) BusTime's `getroutebyid`, in **SQLite plus a file
   store**, and **never stores a duplicate when nothing changed between calls**.
2. A linear-referencing (snapping) module for GTFS shapes, ported from the old project.
3. A route map equivalent to the old project's `plot_route_paths.py`.
4. Operational scripts: collector status, and a comparison against the old project's data.
5. Docker deployment, tests, and docs (`README.md`, `AGENTS.md`, `CLAUDE.md`).

---

## 2. Data sources (API spec)

Every HTTP request must send this header (TheRide can then identify and contact us):

```
User-Agent: aaata-gtfs (research data collector; contact: adempst.dev@gmail.com)
```

Use one shared `httpx.AsyncClient(headers=..., timeout=20, follow_redirects=True)`.

### 2.1 GTFS-realtime feeds (rt.theride.org)

| Feed | URL | Size (raw / gzip) | Poll |
|---|---|---|---|
| Vehicle positions | `https://rt.theride.org/gtfsrt/vehicles` | ~2.3 KB / 1.1 KB | each feed refresh (~30 s), see §5.2 |
| Trip updates (+ trip modifications + shapes) | `https://rt.theride.org/gtfsrt/trips` | ~25 KB / 9.4 KB | same cycle as vehicles |
| Alerts | `https://rt.theride.org/gtfsrt/alerts` | ~30 KB / 3.9 KB | every 5 min |

- Format: protobuf `transit_realtime.FeedMessage` (`Content-Type:
  application/x-google-protobuf`). Parse with `google.transit.gtfs_realtime_pb2` from the
  `gtfs-realtime-bindings` package (version 3.0.0 at the time of writing, which includes
  `trip_modifications` and `shape` entities).
- `header.gtfs_realtime_version = "2.0"`, `incrementality = FULL_DATASET`.
  `header.timestamp` (UTC epoch seconds) is when the server generated the feed.
- **No caching headers:** there's no `ETag`/`Last-Modified`, and conditional requests
  return 200 with the full body. Detect change with `header.timestamp` (and a sha256 of the body).
- **Refresh cadence:** vehicles and trips regenerate together, every 27–33 s (both
  carry the same `header.timestamp`). A new feed was available 1–5 s after its
  `header.timestamp`. Alerts share the same header timestamp, but their content rarely
  changes.
- `FeedEntity.id` values in vehicles/trips are just `"1"`, `"2"`, … per message, **not
  stable keys**. Key on the fields described below.

#### Vehicle positions (`entity.vehicle`, a `VehiclePosition`)
Observed fields (41 vehicles in the fixture, 29 of them with a trip):
- `vehicle.id`: fleet number, e.g. `"553"`. Same as the old project's `Bus.vehicle_id`.
- `timestamp`: **the time the position was measured**, UTC epoch seconds. At feed
  generation, ages ranged 3–34 s for live buses. A few vehicles had ages of 257 s and 359 s
  (no longer reporting; the same stale fix repeats until they drop out).
- `position.latitude`, `position.longitude`. These are snapped to the street, matching
  the old `busesforroute` positions to 0–1 ft when they're the same report.
- `position.bearing` (degrees) and `position.speed` (**metres per second**).
- `trip.trip_id`, `trip.route_id`, `trip.schedule_relationship` (`SCHEDULED`). Vehicles
  out of service have **no `trip`**.
- Not currently sent, but defined by GTFS-RT. Store them if they ever appear:
  `trip.direction_id`, `trip.start_date`, `trip.start_time`,
  `current_stop_sequence`, `stop_id`, `current_status`, `occupancy_status`,
  `congestion_level`, `position.odometer`, `vehicle.label`.
- Reporting cadence: most buses send a new fix about every 30 s, but intervals of 4–60 s
  occur. Each refresh, about 95% of vehicles had a new `timestamp`. About 11 of the ~41
  had a new timestamp at an *identical* position (stopped buses). These are real
  observations and must be kept.
- **Trip id at trip boundaries can be stale:** a bus that has just started trip B may
  still report trip A (the trip it just finished) in its vehicle position, while the trip
  updates feed already shows it on B.

#### Trip updates feed (`/gtfsrt/trips`)
Contains three entity kinds (fixture: 45 `trip_update`, 6 `trip_modifications`, 6
`shape`).

`entity.trip_update` (`TripUpdate`):
- `trip.trip_id` and `trip.route_id`. The `trip_id` is the same id as the old project's
  `BusPrediction.trip_id` (BusTime `tripid`) and the static GTFS `trips.txt` `trip_id`.
  `trip.start_date` is **not** sent.
- `trip.modified_trip.modifications_id` (e.g. `trip_modifications_501393`) and
  `affected_trip_id` are present when a detour applies to the trip.
- `vehicle.id`: the assigned bus. It's absent for trips that have no bus yet; the feed
  lists upcoming trips too.
- `timestamp`: when the prediction was computed. It matched the assigned vehicle's
  latest fix time to within a second (e.g. 1792965574 for both; 1792965579 vs
  1792965578).
- `stop_time_update[]`: `stop_sequence`, `stop_id` (a **GTFS stop_id**, see §2.2),
  `arrival.time` and/or `departure.time` (UTC epoch seconds), and `schedule_relationship`.
  `delay`/`uncertainty` are not sent but should be stored if present. The first stop of a
  trip has only `departure`. **Stops the bus has passed are dropped from the list**, so
  the lowest `stop_sequence` still listed shows progress.
- Per refresh, about 40% of all stop-time updates change. 55–65% of trip updates change
  in content, and 75–90% change `timestamp`.

`entity.trip_modifications` (`TripModifications`, the GTFS-RT detour mechanism):
- `FeedEntity.id` is the modification id, e.g. `trip_modifications_501393`. **The trailing
  number is the BusTime pattern id** (`pid`) of the detoured pattern, which also appears
  in `getroutebyid`.
- `selected_trips[]`: `trip_ids[]` (up to ~100 trips) and `shape_id`, e.g. `shape_501393`.
- `service_dates[]` (`YYYYMMDD`), and `modifications[]` with `start_stop_selector.stop_id`,
  `end_stop_selector.stop_id`, `propagated_modification_delay`, and `last_modified_time`.

`entity.shape` (`Shape`): `shape_id` (e.g. `shape_501393`) and `encoded_polyline`
(Google polyline algorithm, precision 1e-5). These are the detour paths.
They have no stop spurs (see §6).

#### Alerts feed
19 alerts in the fixture. `FeedEntity.id` is the alert's title, which is stable.
`alert.header_text` and `alert.description_text` come as translations (`en`, `es`, `pt`;
the text is identical in all three). `informed_entity[]` holds `agency_id` and optional
`route_id`; there's no `active_period`. `cause`/`effect` are mostly `OTHER_*`.

### 2.2 Static GTFS

- URL: `https://www.theride.org/sites/default/files/google/google_transit.zip` (~2.5 MB).
  (`/media/401/download`, linked from the developer page, is a PDF, not the feed.)
- The server sends `ETag` and `Last-Modified`. Use `If-None-Match`/`If-Modified-Since`, and
  also compare content hashes in case it ignores them.
- Files: `agency.txt` (tz `America/New_York`, the same as America/Detroit), `calendar.txt`,
  `calendar_dates.txt`, `feed_info.txt` (`feed_version` e.g. `S1000302`, `feed_start_date`,
  `feed_end_date`), `routes.txt` (30 routes; `route_id` == `route_short_name`, e.g. `"4"`,
  `"22"`, `"104"`), `stops.txt` (1060), `trips.txt` (4235), `stop_times.txt` (106,869),
  `shapes.txt` (73,495 points, 87 shapes), `transfers.txt`. `frequencies.txt`,
  `fare_attributes.txt` and `fare_rules.txt` are header-only. The zip also has a
  `__MACOSX/` directory; ignore it.
- `trips.txt`: `trip_id, route_id, service_id, trip_headsign, trip_short_name,
  direction_id, block_id, shape_id, wheelchair_accessible, bikes_allowed`. Shape ids look like
  `shp-4-55`.
- `stop_times.txt`: `trip_id, arrival_time, departure_time, stop_id, stop_sequence,
  stop_headsign, pickup_type, drop_off_type, shape_dist_traveled, timepoint`. Times are
  `HH:MM:SS` and may exceed 24:00:00.
- `shapes.txt`: `shape_id, shape_pt_lat, shape_pt_lon, shape_pt_sequence,
  shape_dist_traveled`.
- **`shape_dist_traveled` is in metres** in both files (shp-4-55: 13,817 vs 13,913 m by
  haversine; the agency's figure is slightly shorter).
- **Stop ids:** GTFS `stop_id` ≠ BusTime stop id. **BusTime's `stpid` is GTFS
  `stop_code`.** Example: GTFS stop_id `551` has stop_code `348`, which is BusTime stop
  `348`, "WB Cross + Ballard". GTFS-RT uses GTFS `stop_id`s.
- **Every stop lies exactly on a vertex of its trip's shape** (34/34 within 1 ft on
  shp-22-04).
- **Stop spurs:** like `getroutebyid`, shapes draw many stops as a short out-and-back
  jog to the curb (`A, STOP, A`, 15–121 ft round trip), and every spur tip is a stop. The
  old project's spur removal, run unchanged on the GTFS shapes, removes 56 vertices from
  shp-22-04 (43,219 → 41,635 ft; the agency's `shape_dist_traveled` gives 41,450 ft),
  8 from shp-4-55 (45,635 → 45,391; agency 45,330), 2 from shp-4-06 (44,877 → 44,833;
  agency 44,687) and none from shp-22-01 (38,011; agency 37,745). So **the agency doesn't
  count spurs**, and with them cut our lengths are within about 0.5% of theirs.
- **Positions vs shapes:** of 25 in-service buses whose trip had no detour, 20 were within
  1 ft of their trip's static shape and 2 within 16 ft. Three were 600–1600 ft off: two
  near the end of their trip (layover, or a stale trip id) and one mid-route at speed (cause
  unknown; possibly an unannounced detour). Detoured trips were 1–3 ft from their RT shape.
- Static GTFS is replaced at each service change (the current feed covers
  2026-08-23..2027-01-30). It can be published before it takes effect, so look trips up
  by service date, not by "latest" (see §6.2).

### 2.3 BusTime `getroutebyid` (cross-reference only)

`https://www.theride.org/api/ServiceData?method=getroutebyid&routeid={route_id}` returns
JSON describing the route's current patterns (BusTime `pid`s), their drawn paths with stops,
active detours and bulletins. It's collected hourly so the parallel run can map BusTime
pattern ids to GTFS shapes, and so detours are on record as BusTime sees them. See the old
project's `src/aaata_bus/schema.py` (`Route`, `RoutePattern`, `RoutePoint`, `RouteStop`)
and AGENTS.md "API quirks" for its format. Key points:
- Collections are wrapped (`{"pas": {"pa": [...]}}`), a single element comes as a bare
  object instead of a list, and empty collections or missing scalars come as `[]`.
- A detoured pattern's `pt` list is the full detour path (`dtr: false`) followed by the
  full bypassed regular path (`dtr: true`).
- **Pattern order varies between fetches** of identical data, so hash a canonical form:
  sort `pas.pa` and `dtrs.dtr` by `id`, sort `sbs.sb` by canonical JSON, keep points
  in order, and dump JSON with sorted keys and compact separators. Port
  `canonical_payload` / `payload_hash` from `../AAATA-Bus-API/src/aaata_bus/route_history.py`.
- Unknown route ids return a nameless, pattern-less stub, not an error.

On 2026-10-25, matching shapes to patterns by their exact stop sequence (GTFS
`stop_code` list vs pattern stop ids, regular path only) gave `shp-4-55 → 7605`,
`shp-22-01 → 5948`, `shp-22-04 → 7599`. `shp-4-06` matched nothing, because
`getroutebyid` listed its detour pattern `501393` in its place.

### 2.4 Endpoints that do NOT work (don't retry these)
- `ServiceData` returns an empty 200 for any method not on its whitelist (e.g.
  `getvehicles`, `gettime`).
- `https://rt.theride.org/bustime/api/v3/*` needs an API key (`Developers@TheRide.org`).
- `https://aaatarealtimebus.azurewebsites.us` (in the website JS) is disabled (403).

### 2.5 Terms
TheRide's developer terms ask apps that display the data to show: "Transit scheduling,
geographic, and real-time data provided by permission of AAATA/TheRide". Put this in the map's
attribution and the README.

---

## 3. Tooling, dependencies, layout

- Python **≥3.13** (set by `uv init`), managed with **uv**. Build backend `uv_build`
  (already set). Package `aaata_gtfs` in `src/` layout with `py.typed`.
- Runtime dependencies (`uv add`; take the latest versions):
  `httpx`, `gtfs-realtime-bindings` (brings `protobuf`), `numpy`, `folium` (brings
  `branca`, `jinja2`), `xyzservices`.
  Use only the standard library for SQLite (`sqlite3`), CSV, zip, gzip, json, hashlib,
  zoneinfo and argparse. No ORM, no pandas, no pydantic.
- Dev group: `uv add --dev pytest pyrefly`. Copy `pyrefly.toml` from `../AAATA-Bus-API`
  (set `python-version = "3.13"`). `uv run pyrefly check` and `uv run pytest` must both pass.
- Type annotations everywhere. pyrefly checks unannotated defs too. Prefer `dataclass(frozen=True)`
  for records. protobuf message classes have stub-less attributes; if pyrefly complains,
  wrap access in small typed helper functions rather than sprinkling ignores.
- `xyzservices` providers are attached dynamically, so use
  `providers.query_name("Esri.WorldGrayCanvas")` (not attribute access) to keep pyrefly happy.

```
AAATA-GTFS/
  SPEC.md                  # this file
  README.md  AGENTS.md  CLAUDE.md
  pyproject.toml  uv.lock  pyrefly.toml  .gitignore  .dockerignore
  Dockerfile  docker-compose.yml  docker-entrypoint.sh
  src/aaata_gtfs/
    __init__.py            # re-exports the public API; keep __all__ current
    py.typed
    config.py              # URLs, USER_AGENT/REQUEST_HEADERS, DATA_PATH, intervals, LOCAL_TZ
    http.py                # shared client factory, get_bytes(), retry/backoff helper
    db.py                  # connect(), schema DDL, migrations (PRAGMA user_version)
    filestore.py           # raw blob archive: RT hourly record files, static zips, JSON payloads
    realtime.py            # protobuf -> typed records (VehicleRecord, TripUpdateRecord, ...)
    static_gtfs.py         # download/parse/load static GTFS; StaticGtfs lookup class
    service_dates.py       # service_date_for(trip_id, ts): which service day a trip instance is
    bustime.py             # getroutebyid fetch, canonical hash, minimal pattern parsing
    store.py               # dedup-aware writers (Store class with in-memory last-state caches)
    collector.py           # async scheduler (realtime, alerts, static, bustime loops)
    polyline.py            # Google encoded polyline decoder
    geometry.py            # ShapeGeometry (linear referencing), spur removal, snapping
    shapes.py              # GeometryResolver: (trip_id, service_date, time) -> ShapeGeometry
    queries.py             # read helpers for analysis/viz (positions in window, predictions_at, ...)
    visualization/
      __init__.py
      data.py              # BusPath, trace_bus_paths, latest_positions (from the DB)
      route_paths.py       # folium map (port of the old one)
      serve.py             # localhost HTTP server for --serve (copy from old project)
  scripts/
    collect.py             # run the collector
    plot_route_paths.py    # route map
    collector_status.py    # health/coverage report
    compare_with_old.py    # parallel-run comparison with ../AAATA-Bus-API data
    check_geometry.py      # snapping offsets + stop-distance sanity check
  tests/
    fixtures/              # real captures, see §10
    test_*.py
  data/                    # gitignored; default data dir
```

`config.py`:
- `DATA_PATH = Path(os.getenv("AAATA_GTFS_DATA_PATH", <repo root>/data))`;
  `DB_PATH = DATA_PATH / "aaata_gtfs.sqlite3"`; `BLOB_PATH = DATA_PATH / "blobs"`.
- `ensure_data_dir()` creates `DATA_PATH` if missing (unlike the old project, which
  asserted). Scripts that write call it first.
- `LOCAL_TZ = ZoneInfo("America/Detroit")`.

Add `data/` and `.env` to `.gitignore`.

---

## 4. Storage

### 4.1 Principles
- **One SQLite file** (`data/aaata_gtfs.sqlite3`) holds all parsed data. It is the source
  of truth for analysis.
- **A file store** (`data/blobs/`) keeps the raw bytes of everything fetched (deduplicated),
  so data can be re-parsed if a parser bug is found or a field we ignored turns out to matter.
  The collector's `--no-raw-archive` turns RT archiving off. Static zips and BusTime
  payloads are always kept (they're rare).
- **No duplicate rows when nothing changed.** The specific rule per table is in §4.3.
  A restart must not re-insert anything: caches are rebuilt from the DB at startup (§5.4).
- **Times:** every instant is stored as **UTC epoch seconds**: INTEGER for feed-provided
  times, REAL for our own clock (`time.time()`). Never store naive datetimes (a lesson
  from the old project). Service dates are TEXT `YYYYMMDD`. Static schedule times are
  INTEGER seconds after local midnight of the service day (may exceed 86400).
- Enum values from protobuf are stored as their names (`"SCHEDULED"`), via
  `Enum.Name(value)`.
- SQLite pragmas on every connection: `journal_mode=WAL`, `synchronous=NORMAL`,
  `foreign_keys=ON`, `busy_timeout=10000`. The collector is the only writer; scripts open
  read-only (`file:...?mode=ro` URI) so they can run while it collects.
- Schema version in `PRAGMA user_version`. `db.migrate(conn)` applies an ordered list of
  migration scripts. Phase 1 ships migration 1 = the DDL below.

### 4.2 File store layout
```
data/blobs/
  rt/<feed>/<YYYY>/<MM>/<DD>/<HH>.pbs.gz       # feed in {vehicles, trips, alerts}; hour of header_ts (UTC)
  static/<content_id>.zip                      # static GTFS zips, named by content id (§4.3.6)
  bustime/getroutebyid/<route_id>/<sha12>.json.gz
```
RT hourly files are **appended gzip members**, one per stored snapshot (open with
`gzip.open(path, "ab")`; readers handle concatenated members transparently). Each record
is an 8-byte big-endian `header_ts`, then a 4-byte big-endian payload length, then the raw
protobuf payload. Provide `filestore.append_rt(feed, header_ts, payload) -> relpath` and
`filestore.iter_rt(path) -> Iterator[tuple[int, bytes]]`. Paths stored in the DB are
relative to `DATA_PATH`. Expected volume with archiving on is ~30 MB/day; the DB grows at
roughly the same rate.

### 4.3 Schema (migration 1)

Write this DDL in `db.py`. Columns and constraints as listed; add more indexes if a query
in §6–§9 needs them.

#### 4.3.1 Collector runs and snapshots
```sql
CREATE TABLE collector_run (
  id          INTEGER PRIMARY KEY,
  started_at  REAL NOT NULL,
  stopped_at  REAL,                 -- set on clean shutdown
  version     TEXT NOT NULL,        -- package version
  args        TEXT NOT NULL         -- JSON of CLI args
);

-- One row per *new* realtime feed message (new header_ts); repeats are not stored.
CREATE TABLE feed_snapshot (
  id          INTEGER PRIMARY KEY,
  feed        TEXT NOT NULL CHECK (feed IN ('vehicles','trips','alerts')),
  header_ts   INTEGER NOT NULL,     -- FeedHeader.timestamp
  fetched_at  REAL NOT NULL,        -- our clock when first fetched
  sha256      TEXT NOT NULL,
  n_entities  INTEGER NOT NULL,
  n_bytes     INTEGER NOT NULL,
  blob        TEXT,                 -- relpath of the hourly archive file, NULL if archiving off
  run_id      INTEGER NOT NULL REFERENCES collector_run(id),
  UNIQUE (feed, header_ts)
);
```
Dedup: skip the message entirely if `header_ts` equals the last stored `header_ts` for
that feed (or the `(feed, header_ts)` row exists). If `header_ts` is the same but the sha256
differs, log a warning and skip. For **alerts**, also skip (no row) when the sha256
equals the last stored alerts sha256: alerts regenerate every 30 s, but polling every 5 min
with content dedup is what we want.

#### 4.3.2 Vehicle positions
```sql
CREATE TABLE vehicle_position (
  vehicle_id      TEXT NOT NULL,
  fix_ts          INTEGER NOT NULL,   -- VehiclePosition.timestamp (GPS fix time)
  lat             REAL NOT NULL,
  lon             REAL NOT NULL,
  bearing         REAL,
  speed_mps       REAL,
  trip_id         TEXT,               -- NULL when out of service
  route_id        TEXT,
  service_date    TEXT,               -- derived, §6.3; NULL if no trip or trip unknown
  trip_schedule_relationship TEXT,
  direction_id    INTEGER,            -- the rest: NULL unless the feed starts sending them
  start_date      TEXT,
  start_time      TEXT,
  current_stop_sequence INTEGER,
  stop_id         TEXT,
  current_status  TEXT,
  occupancy_status TEXT,
  congestion_level TEXT,
  odometer        REAL,
  vehicle_label   TEXT,
  snapshot_id     INTEGER NOT NULL REFERENCES feed_snapshot(id),  -- first seen in
  PRIMARY KEY (vehicle_id, fix_ts)
) WITHOUT ROWID;
CREATE INDEX vehicle_position_ts   ON vehicle_position(fix_ts);
CREATE INDEX vehicle_position_trip ON vehicle_position(trip_id, service_date, fix_ts);
```
Dedup: `INSERT OR IGNORE` on `(vehicle_id, fix_ts)`. A stale fix repeated across
snapshots is stored once. A new `fix_ts` at an identical position **is** stored. If a
repeat has different content (e.g. trip_id changed but fix_ts didn't), keep the first row
and count it in a per-run `conflicts` counter that's logged hourly.

Vehicle presence (which vehicles were in a snapshot) is not stored per snapshot, because that
would duplicate rows every 30 s. Instead, `vehicle_seen`:
```sql
CREATE TABLE vehicle_seen (            -- contiguous presence intervals of a vehicle in the feed
  vehicle_id        TEXT NOT NULL,
  first_snapshot_id INTEGER NOT NULL REFERENCES feed_snapshot(id),
  last_snapshot_id  INTEGER NOT NULL REFERENCES feed_snapshot(id),
  PRIMARY KEY (vehicle_id, first_snapshot_id)
) WITHOUT ROWID;
```
For each vehicles snapshot: if the vehicle was in the previous stored vehicles snapshot,
update `last_snapshot_id` on its open interval. Otherwise insert a new interval. This answers
"was bus X in the feed at time t, and was it still reporting?" without duplication.

#### 4.3.3 Trip updates and predictions
```sql
-- One row per observation of a trip instance in which anything changed.
CREATE TABLE trip_update (
  id                   INTEGER PRIMARY KEY,
  trip_id              TEXT NOT NULL,
  service_date         TEXT,             -- derived, §6.3
  route_id             TEXT,
  vehicle_id           TEXT,             -- NULL when no bus assigned
  update_ts            INTEGER,          -- TripUpdate.timestamp
  schedule_relationship TEXT,
  modifications_id     TEXT,             -- trip.modified_trip.modifications_id
  delay                INTEGER,          -- TripUpdate.delay, if sent
  first_stop_sequence  INTEGER,          -- lowest stop_sequence still listed
  n_stop_time_updates  INTEGER NOT NULL,
  snapshot_id          INTEGER NOT NULL REFERENCES feed_snapshot(id),  -- observed in
  last_seen_snapshot_id INTEGER NOT NULL REFERENCES feed_snapshot(id)  -- still identical up to
);
CREATE INDEX trip_update_trip ON trip_update(trip_id, service_date, snapshot_id);
CREATE INDEX trip_update_vehicle ON trip_update(vehicle_id, snapshot_id);

-- A stop's prediction, stored only when it changed for that trip instance + stop.
CREATE TABLE stop_time_prediction (
  trip_update_id   INTEGER NOT NULL REFERENCES trip_update(id),
  trip_id          TEXT NOT NULL,
  service_date     TEXT,
  stop_sequence    INTEGER NOT NULL,
  stop_id          TEXT NOT NULL,        -- GTFS stop_id
  arrival_time     INTEGER,
  departure_time   INTEGER,
  arrival_delay    INTEGER,
  departure_delay  INTEGER,
  arrival_uncertainty   INTEGER,
  departure_uncertainty INTEGER,
  schedule_relationship TEXT,
  observed_ts      INTEGER NOT NULL,     -- TripUpdate.timestamp, else the snapshot header_ts
  snapshot_id      INTEGER NOT NULL REFERENCES feed_snapshot(id)
);
CREATE INDEX stop_time_prediction_key ON stop_time_prediction(trip_id, service_date, stop_sequence, snapshot_id);
CREATE INDEX stop_time_prediction_stop ON stop_time_prediction(stop_id, observed_ts);
```
Dedup, per trips snapshot and per trip update (key `(trip_id, service_date)`; use `""` in
the cache key when `service_date` is NULL):
1. Build the stop-time tuples `(stop_id, arrival_time, departure_time, delays,
   uncertainties, schedule_relationship)` keyed by `stop_sequence`.
2. Compare each one with the last stored tuple for `(trip_id, service_date,
   stop_sequence)`. Collect the changed ones.
3. If any stop changed, or any trip-level field (`vehicle_id`, `update_ts`,
   `schedule_relationship`, `modifications_id`, `delay`, `first_stop_sequence`,
   `n_stop_time_updates`, `route_id`) differs from the last stored `trip_update` row for the
   key: insert a new `trip_update` row, then insert the changed stops referencing it.
4. Otherwise just set `last_seen_snapshot_id` on the last stored row.

This makes "what did the agency predict for stop S of trip T at time t" answerable
(`queries.predictions_at`, §6.4) while storing only about 40% of stop-time updates. It
also records when stops drop out of the list (`first_stop_sequence` changes), which
approximates when each stop was passed, to within a feed refresh.

#### 4.3.4 Detours (trip modifications) and RT shapes
```sql
CREATE TABLE trip_modifications_version (
  id                 INTEGER PRIMARY KEY,
  modifications_id   TEXT NOT NULL,      -- e.g. 'trip_modifications_501393'
  pattern_id         TEXT,               -- trailing digits: BusTime pid, e.g. '501393'
  content_sha256     TEXT NOT NULL,
  content_json       TEXT NOT NULL,      -- canonical JSON of the TripModifications message
  first_snapshot_id  INTEGER NOT NULL REFERENCES feed_snapshot(id),
  last_snapshot_id   INTEGER NOT NULL REFERENCES feed_snapshot(id)
);
CREATE INDEX trip_modifications_version_id ON trip_modifications_version(modifications_id, first_snapshot_id);
CREATE TABLE trip_modifications_trip (
  version_id INTEGER NOT NULL REFERENCES trip_modifications_version(id),
  trip_id    TEXT NOT NULL,
  shape_id   TEXT,
  PRIMARY KEY (version_id, trip_id)
) WITHOUT ROWID;
CREATE INDEX trip_modifications_trip_trip ON trip_modifications_trip(trip_id);
CREATE TABLE trip_modifications_date (
  version_id   INTEGER NOT NULL REFERENCES trip_modifications_version(id),
  service_date TEXT NOT NULL,
  PRIMARY KEY (version_id, service_date)
) WITHOUT ROWID;

CREATE TABLE rt_shape_version (
  id                INTEGER PRIMARY KEY,
  shape_id          TEXT NOT NULL,       -- e.g. 'shape_501393'
  content_sha256    TEXT NOT NULL,
  encoded_polyline  TEXT NOT NULL,
  first_snapshot_id INTEGER NOT NULL REFERENCES feed_snapshot(id),
  last_snapshot_id  INTEGER NOT NULL REFERENCES feed_snapshot(id)
);
CREATE INDEX rt_shape_version_id ON rt_shape_version(shape_id, first_snapshot_id);
```
Versioning rule (shared with alerts below): for each entity, compare its content sha256
with the **latest** version row for that id. If equal, update `last_snapshot_id`;
otherwise insert a new version. Canonical JSON =
`json.dumps(MessageToDict(msg, preserving_proto_field_name=True), sort_keys=True,
separators=(",", ":"))`. Sort list fields whose order isn't meaningful (`trip_ids`,
`service_dates`) before hashing. An entity that's absent from a snapshot simply stops
having `last_snapshot_id` advanced, so "in effect at t" means
`first.header_ts <= t <= last.header_ts` (with a small grace, §6.2).

#### 4.3.5 Alerts
```sql
CREATE TABLE alert_version (
  id                INTEGER PRIMARY KEY,
  alert_id          TEXT NOT NULL,       -- FeedEntity.id (the alert title)
  content_sha256    TEXT NOT NULL,
  content_json      TEXT NOT NULL,
  header_text       TEXT,                -- English translation, for convenience
  route_ids         TEXT,                -- comma-joined informed route_ids, '' = system-wide
  first_snapshot_id INTEGER NOT NULL REFERENCES feed_snapshot(id),
  last_snapshot_id  INTEGER NOT NULL REFERENCES feed_snapshot(id)
);
CREATE INDEX alert_version_id ON alert_version(alert_id, first_snapshot_id);
```
Same versioning rule. Note that alert snapshots are only stored when the alerts feed's
content changed, so `last_snapshot_id` for alerts is only advanced when the feed changed.
Also record each alerts poll's time in memory, and treat an alert as in effect until the
first stored alerts snapshot that lacks it.

#### 4.3.6 Static GTFS
```sql
CREATE TABLE static_version (
  id              TEXT PRIMARY KEY,   -- content id: first 12 hex of content sha256 (below)
  content_sha256  TEXT NOT NULL UNIQUE,
  zip_sha256      TEXT NOT NULL,
  fetched_at      REAL NOT NULL,      -- first fetched
  last_checked_at REAL NOT NULL,      -- last time the server still served it
  etag            TEXT,
  last_modified   TEXT,
  feed_version    TEXT,
  feed_start_date TEXT,
  feed_end_date   TEXT,
  blob            TEXT NOT NULL       -- relpath of the zip
);
CREATE TABLE static_route (static_version TEXT NOT NULL REFERENCES static_version(id),
  route_id TEXT NOT NULL, short_name TEXT, long_name TEXT, route_type INTEGER,
  color TEXT, text_color TEXT, PRIMARY KEY (static_version, route_id)) WITHOUT ROWID;
CREATE TABLE static_stop (static_version TEXT NOT NULL REFERENCES static_version(id),
  stop_id TEXT NOT NULL, stop_code TEXT, name TEXT, lat REAL, lon REAL,
  wheelchair_boarding INTEGER, PRIMARY KEY (static_version, stop_id)) WITHOUT ROWID;
CREATE TABLE static_trip (static_version TEXT NOT NULL REFERENCES static_version(id),
  trip_id TEXT NOT NULL, route_id TEXT NOT NULL, service_id TEXT NOT NULL, headsign TEXT,
  direction_id INTEGER, block_id TEXT, shape_id TEXT,
  PRIMARY KEY (static_version, trip_id)) WITHOUT ROWID;
CREATE INDEX static_trip_trip ON static_trip(trip_id);
CREATE TABLE static_stop_time (static_version TEXT NOT NULL REFERENCES static_version(id),
  trip_id TEXT NOT NULL, stop_sequence INTEGER NOT NULL, stop_id TEXT NOT NULL,
  arrival_s INTEGER, departure_s INTEGER, shape_dist_m REAL, timepoint INTEGER,
  pickup_type INTEGER, drop_off_type INTEGER, stop_headsign TEXT,
  PRIMARY KEY (static_version, trip_id, stop_sequence)) WITHOUT ROWID;
CREATE TABLE static_shape_point (static_version TEXT NOT NULL REFERENCES static_version(id),
  shape_id TEXT NOT NULL, seq INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  dist_m REAL, PRIMARY KEY (static_version, shape_id, seq)) WITHOUT ROWID;
CREATE TABLE static_calendar (static_version TEXT NOT NULL REFERENCES static_version(id),
  service_id TEXT NOT NULL, days TEXT NOT NULL,   -- 7 chars Mon..Sun of 0/1
  start_date TEXT NOT NULL, end_date TEXT NOT NULL,
  PRIMARY KEY (static_version, service_id)) WITHOUT ROWID;
CREATE TABLE static_calendar_date (static_version TEXT NOT NULL REFERENCES static_version(id),
  service_id TEXT NOT NULL, date TEXT NOT NULL, exception_type INTEGER NOT NULL,
  PRIMARY KEY (static_version, service_id, date)) WITHOUT ROWID;
```
Content sha256 = sha256 over the sorted list of `(member name, sha256 of member bytes)`
for the `*.txt` members (ignoring `__MACOSX/`), so a re-zipped but identical feed isn't
a new version. A new version is loaded into the tables in a single transaction. An
unchanged one only bumps `last_checked_at` (and stores the new ETag/Last-Modified).
Loading the full feed (~107k stop times, ~73k shape points) should take a few seconds
with `executemany`.

#### 4.3.7 BusTime route versions
```sql
CREATE TABLE bustime_route_version (
  id              INTEGER PRIMARY KEY,
  route_id        TEXT NOT NULL,
  route_version   TEXT NOT NULL,      -- '<route_id>-<sha12>' like the old project
  content_sha256  TEXT NOT NULL,      -- payload_hash (canonical)
  fetched_at      REAL NOT NULL,
  last_checked_at REAL NOT NULL,
  blob            TEXT NOT NULL
);
CREATE INDEX bustime_route_version_route ON bustime_route_version(route_id, fetched_at);
```
Same rule as the old project: insert only if the canonical hash differs from that route's
latest version. Otherwise bump `last_checked_at`.

---

## 5. Collector (`collector.py`, `scripts/collect.py`)

### 5.1 Structure
An asyncio program with one shared `httpx.AsyncClient` and four independent loops started
with `asyncio.TaskGroup`:

| Loop | What | When |
|---|---|---|
| `realtime_loop` | vehicles, then trips, back to back | adaptive, aligned to feed refresh (§5.2) |
| `alerts_loop` | alerts | every 300 s (`--alerts-interval-s`) |
| `static_loop` | static GTFS zip, conditional GET | at startup, then every 6 h (`--static-interval-h`) |
| `bustime_loop` | `getroutebyid` for every route in the newest static version | at startup (after the first static load), then every 60 min (`--bustime-interval-min`); random 2–5 s courtesy wait between routes; `--no-bustime` disables it |

All DB writes go through one `Store` object on the event loop thread (sqlite3 is
synchronous; writes are small and fast). Static loading may take seconds, so run it with
`asyncio.to_thread` on its **own** connection, and keep a single writer at a time with an
`asyncio.Lock` around writes. Each snapshot's processing is one transaction.

### 5.2 Realtime polling: ask only when new data is due
The feeds refresh about every 30 s, and because each position carries its own fix time,
fetching sooner after a refresh adds nothing but load. The schedule is:
1. Fetch vehicles and trips. For each, parse `header.timestamp` (H).
2. If H is newer than the last stored H for that feed: process and store it.
3. Next poll at `H_latest + REFRESH_S + AVAILABILITY_S`, with `REFRESH_S = 30` and
   `AVAILABILITY_S = 3`, clamped to `[now + 2 s, now + 45 s]`. This assumes the host
   clock is NTP-synced; log a warning if `fetched_at - H` is negative or above 60 s for
   a fresh feed.
4. If neither feed was new: retry after `RETRY_S = 4`. After 8 consecutive unchanged polls,
   back off to every 15 s and log once ("feed stalled since H"). Return to normal on the
   next new H.
5. HTTP or parse errors: exponential backoff 5, 10, 20, … capped at 120 s, logging each
   failure on one line. A malformed message is archived (if archiving is on) under the
   feed's file but not parsed; log its sha256.

Expected load: about 2–2.5 requests per 30 s in total (vehicles + trips), versus the old
project's dozens.

### 5.3 Processing a snapshot
- **vehicles:** insert `feed_snapshot` (+ archive), insert/ignore `vehicle_position` rows
  (compute `service_date` via `service_date_for`), update `vehicle_seen` intervals.
- **trips:** insert `feed_snapshot` (+ archive), then in this order:
  `rt_shape_version`, `trip_modifications_version` (+ its trips and dates),
  `trip_update` / `stop_time_prediction` with the dedup in §4.3.3.
- **alerts:** content dedup, then `alert_version`.

### 5.4 Restart safety
On startup, before polling:
- `INSERT` a `collector_run`. Set its `stopped_at` on SIGTERM/SIGINT (Docker sends
  SIGTERM; handle it with `loop.add_signal_handler`) and close the DB cleanly.
- Load caches from the DB: the last `header_ts` and sha256 per feed; open `vehicle_seen`
  intervals (those whose `last_snapshot_id` is the latest vehicles snapshot); the latest
  `trip_update` row per `(trip_id, service_date)` and the latest `stop_time_prediction` per
  `(trip_id, service_date, stop_sequence)`, both limited to rows from the last 36 hours; and
  the latest version row per modifications id, RT shape id, alert id and BusTime route.
- Prune in-memory caches of trip instances not seen for 6 hours.

### 5.5 CLI and logging
`uv run scripts/collect.py [--data-path PATH] [--no-raw-archive] [--alerts-interval-s 300]
[--static-interval-h 6] [--bustime-interval-min 60] [--no-bustime] [--log-level INFO]`.
Use `logging` with timestamps, written to stdout. At INFO, log one line per stored
snapshot (`vehicles H=… new_fixes=38/41`, `trips H=… tu_rows=24 stp_rows=277 unchanged=21`)
and one hourly summary (requests, snapshots, rows per table, conflicts, errors).

---

## 6. Library: static lookups, service dates, geometry

### 6.1 `realtime.py`
Pure functions that turn a `FeedMessage` (or its bytes) into frozen dataclasses:
`VehicleRecord`, `TripUpdateRecord` (with `stops: tuple[StopTimeRecord, ...]`),
`TripModificationsRecord`, `RtShapeRecord`, `AlertRecord`, plus
`parse_feed(payload) -> FeedMessage`. Optional protobuf fields must use `HasField` so an
absent field becomes `None`, not `0`/`""`. The store layer consumes these records, which
keeps parsing testable without a DB.

### 6.2 `static_gtfs.py`: `StaticGtfs`
- `fetch_static(client, etag, last_modified) -> FetchResult | NotModified` and
  `load_static(conn, zip_bytes, fetched_at, etag, last_modified) -> static_version id`
  (idempotent).
- `StaticGtfs(conn)`, a read-side lookup with in-memory caches:
  - `trip(trip_id, service_date=None) -> StaticTrip | None`: among versions containing
    the trip, prefer one whose `[feed_start_date, feed_end_date]` contains `service_date`
    (most recently fetched first). Otherwise use the most recently fetched version that has
    the trip.
  - `stop_times(version, trip_id)`, `shape_points(version, shape_id)`,
    `stops(version) -> dict[stop_id, StaticStop]`, `routes(version)`,
    `stop_code_to_id(version)`.
  - `runs_on(version, service_id, date) -> bool`: calendar plus calendar_dates exceptions.
  - `latest_version()`.

Geometry for RT entities is "in effect at t" when
`first_snapshot.header_ts - 60 <= t <= last_snapshot.header_ts + 60`.

### 6.3 `service_dates.py`
`service_date_for(static, trip_id, ts) -> str | None`. GTFS trip ids repeat every day the
trip runs, so a trip *instance* is `(trip_id, service_date)`. The feed doesn't send
`start_date`, so derive it:
- Let `d0` be the local (America/Detroit) date of `ts`. Candidates: `d0 - 1 day` and `d0`.
- For each candidate `d` on which the trip runs (`runs_on`), compute its scheduled window
  `[first departure - 2 h, last arrival + 2 h]`, where a schedule time `s` on day `d` is
  local noon of `d` minus 12 h plus `s` seconds (the GTFS definition, which is right across
  DST changes).
- Return the candidate whose window contains `ts`; if several do, the one whose scheduled
  start is closest. If none does, return `d0` when the trip runs on `d0`, else `None`.

Cache by `(trip_id, d0)`.

### 6.4 `queries.py` (read side)
- `positions(conn, start_ts, end_ts, route_ids=None) -> list[PositionRow]`, ordered by
  vehicle and then `fix_ts`.
- `latest_vehicles_snapshot(conn, at_or_before) -> (snapshot_id, header_ts) | None`.
- `active_vehicles(conn, at) -> set[str]`: vehicles in the `vehicle_seen` interval covering the
  latest vehicles snapshot at or before `at` **and** whose latest fix is within 120 s of
  that snapshot's `header_ts` (this drops stale non-reporting buses).
- `predictions_at(conn, trip_id, service_date, snapshot_id) -> list[StopTimeRow]`: the
  latest `stop_time_prediction` per stop with `snapshot_id <=` the given one, keeping only
  stops at or after the `first_stop_sequence` of the trip update in effect then.
- `trip_modifications_at(conn, t) -> list[ModificationVersion]`, `rt_shape_at(conn,
  shape_id, t)`.

### 6.5 `polyline.py`
`decode_polyline(s: str, precision: int = 5) -> list[tuple[float, float]]`, the standard
Google algorithm. Test: ``"_p~iF~ps|U_ulLnnqC_mqNvxq`@"`` decodes to
`[(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]`.

### 6.6 `geometry.py`: port of the old linear referencing
Port `../AAATA-Bus-API/src/aaata_bus/geometry.py` (`PatternGeometry`, spur removal,
`project`/`project_many`/`_nearest_pass`, `point_at`, `path_between`, `stop_distances`,
`SnappedPing`, `snap_sightings`) as `ShapeGeometry`, with these changes:
- Constructor takes the input directly:
  `ShapeGeometry(key: str, route_id: str, points: Sequence[tuple[float, float]],
  stops: Sequence[ShapeStop], stop_tolerance_ft: float)`, where `ShapeStop` is
  `(stop_id, stop_code, name, lat, lon)` in trip order, and `key` identifies the source
  (`"static:<version>:<shape_id>"` or `"rt:<rt_shape_version.id>"`).
- **Stop vertices:** walk the stops in trip order and attach each to the first vertex at
  or after the previous stop's vertex that lies within `stop_tolerance_ft`
  (1 ft for static shapes; 6 ft for RT shapes, whose 1e-5° precision is about 3.6 ft). The
  spur algorithm then runs exactly as in the old code (`MAX_STOP_SPUR_VERTICES = 6`,
  `MAX_STOP_SPUR_FT = 200`, `SPUR_RETURN_TOLERANCE_FT = 3`). A stop with no matching
  vertex is located by projection, without spur handling, and logged at DEBUG.
- Keep the equirectangular local projection in feet around the first point
  (`EARTH_RADIUS_FT = 20_902_259.0`) and `DEFAULT_MAX_OFFSET_FT = 150`.
- Expose `agency_dist_ft(stop_sequence)` from `stop_times.shape_dist_traveled × 3.28084`
  when available, for the sanity check in §9.5.
- `project_many` builds a (points × segments) matrix. Chunk it into 2,000 points to bound
  memory.
- `snap_sightings(positions, resolver, max_offset_ft)` works as in the old code: each
  position is projected near the previous on-route one on the same geometry (same `key`).

### 6.7 `shapes.py`: `GeometryResolver`
`resolver.for_trip(trip_id, service_date, t) -> ShapeGeometry | None`:
1. If a `trip_modifications_version` in effect at `t` selects `trip_id` (and its
   `service_dates` include `service_date`, when known), use its `shape_id`'s
   `rt_shape_version` in effect at `t`, with the trip's static stops.
2. Otherwise use the static trip's `shape_id` from `StaticGtfs.trip(trip_id, service_date)`.
3. Cache geometries by `key`.

It also provides `pattern_for_trip(...) -> str | None`, which returns the modification's
`pattern_id` if the trip is detoured, else the BusTime pid matched to the static shape by
stop sequence (§2.3), using the BusTime route version in effect at `t`. Use
`stop_code` for the comparison, and only the regular-path (`dtr: false`) stops of
undetoured patterns. Cache the shape → pid map per (static version, BusTime route
version). This is for the comparison with the old data; nothing else depends on pids.

---

## 7. Visualization (Phase 1): the route map

Port `../AAATA-Bus-API/src/aaata_bus/visualization/route_paths.py`,
`visualization/data.py`, `visualization/serve.py` and `scripts/plot_route_paths.py`, keeping
the look and behaviour, with these changes for GTFS.

**What stays the same** (read the old code; this is a summary):
- Esri.WorldGrayCanvas basemap via `providers.query_name`; map centred on Ann Arbor
  `(42.2808, -83.7430)`, then fit to the data.
- One `folium.FeatureGroup` per route, named "Route N", in numeric order, coloured from
  the same 8-colour `ROUTE_COLORS` list in that order. The layer control is not collapsed.
- Route shapes are wide (weight 7) and faint (opacity 0.3) beneath the pings. Stops are
  small white-filled circles in the route colour, drawn once per stop even when shared by
  several shapes, with a tooltip.
- Bus pings are drawn twice. Raw pings are joined by weight-3 lines with a white-ringed
  dot per ping (`--no-points` drops the dots). Snapped pings are markers on the path without
  lines; unsnappable pings are hollow at their raw position, with the reason in the tooltip.
  A radio button in the overlay control switches between them through a `snapped-pings`
  class on the map container.
- The `_OverlayToggle` control has checkboxes for "Route paths & stops", "Bus pings",
  "Latest bus positions" and "Detours (uncheck for regular paths)", using CSS classes
  `route-shape`, `bus-ping`, `ping-raw`, `ping-snapped`, `bus-latest`, `detour-path` and
  `detour-regular`, and `off-<name>` classes on the container. Copy the template.
- The latest positions are fleet-number pills (`DivIcon`, class `bus-latest`) on top,
  coloured by route.
- A bus's line is split into separate paths at time gaps over `--max-gap-minutes` (15).
- `--serve [PORT]` serves the written file on `127.0.0.1` (default 8000) and prints the URL.

**What changes:**
- Data comes from the DB through `queries.positions` over a time window:
  `--start`/`--end` (ISO-8601, local time if naive; default **the last 2 hours of
  data**, because all ~30 routes are collected now and a full day is ~100k pings) and
  `--routes`.
- Paths are grouped by `(route_id, vehicle_id)` and ordered by **`fix_ts`**. Positions
  without a trip go into a gray (`#8a8a8a`) "Not in service" layer, raw only. A bus that
  changes route starts a new path in the other layer.
- Route shapes for route R: every distinct `shape_id` among R's trips in the static version
  in effect at the window end, plus any RT detour shapes in effect during the window for
  R's trips. A detour shape is dashed (`8 8`) with class `detour-path`. A static shape all
  of whose trips in the window are covered by active modifications gets
  `detour-regular`. The "Detours" box only appears when there's a detour. Shape tooltip:
  route short and long name, headsign(s), `shape_id`.
- Stops come from the static stop times of R's trips. Tooltip: name, "Stop <stop_code>"
  (the number riders see), GTFS stop_id, and headsigns.
- Ping tooltips now show seconds and the extra fields: `Route 4 · bus 553`,
  `Oct 25 18:01:07` (local time of `fix_ts`), speed in mph, bearing, headsign and trip_id. The
  snapped view adds "N ft along <shape_id> · M ft off it".
- Latest positions: for each vehicle in `queries.active_vehicles(window end)`, its latest
  position. The tooltip adds the position's age at that snapshot.
- Attribution: append the TheRide credit line (§2.5) to the tile attribution.
- Use `GeometryResolver` + `snap_sightings` for snapping.

CLI: `uv run scripts/plot_route_paths.py [--data-path] [--output data/route_paths.html]
[--routes ...] [--start ...] [--end ...] [--max-gap-minutes 15] [--no-points]
[--no-latest] [--no-route-shapes] [--no-stops] [--pings raw|snapped] [--max-offset-ft 150]
[--serve [PORT]]`. It prints the number of paths, pings and latest positions written.

---

## 8. Deployment

- `Dockerfile`: like the old one, but on `python:3.13-slim-trixie`, with
  `AAATA_GTFS_DATA_PATH=/app/data`, `CMD ["collect.py"]`, and the same
  `docker-entrypoint.sh` (runs `scripts/<name>.py` by name, or passes through
  `pytest`/`python`/other commands). Add a `.dockerignore` for `data/`, `.venv/`, `.git/`.
- `docker-compose.yml`: a `collector` service with `restart: unless-stopped`,
  `volumes: ["./data:/app/data"]`, `stop_grace_period: 30s`, and
  `user: "${UID:-1000}:${GID:-1000}"`. The old project's data ended up owned by root, which
  forced `--output` workarounds for local runs; this avoids that. Make sure the image's
  `/app/data` and the venv don't need root to run.
- The old project's compose keeps running unchanged during the parallel period.

---

## 9. Scripts

### 9.1 `collect.py`
Runs the collector (§5).

### 9.2 `plot_route_paths.py`
See §7.

### 9.3 `collector_status.py`
Prints a health report from the DB, read-only:
- the latest snapshot per feed (header time in local time, and how long ago);
- the current collector run and its start time;
- rows per table, in total and per local day for the last 7 days;
- gaps between consecutive vehicles snapshots over 90 s in the last 24 h (top 10, with
  durations), and their total;
- version counts (static, RT shapes, trip modifications, alerts, BusTime routes) and the
  active detours (modification id, pattern id, number of trips);
- the size of the DB file and of `blobs/`.

### 9.4 `compare_with_old.py`: parallel-run validation
Inputs: `--old-data PATH` (default
`../AAATA-Bus-API/dat/route_prediction_data.jsonl`; read-only, about 180 MB, so stream it line by
line), `--data-path`, `--start/--end` (default: the overlap of both datasets).

The old format is one JSON object per line: `{"route_id", "requested_at", "buses": [...],
"predictions": {vehicle_id: [...]}, "predictions_collected"}`, using **field names, not API
aliases**. `requested_at` and `buses[].timestamp` are naive ISO strings in **UTC**
(container clock). Example bus: `{"vehicle_id": "555", "route_number": "22", "latitude":
42.31119, "longitude": -83.71241, "pattern_id": "5948", "dispatch_id": "13374", "timestamp":
"2026-10-25T19:08:29.344344", ...}` (`dispatch_id` is distance along the pattern in feet).
Example prediction: `{"vehicle_id": "555", "stop_id": "1575", "trip_id": "287010",
"predicted_arrival_at": "2026-10-25T15:09:00-04:00", "prediction_generated_at":
"2026-10-25T15:08:00-04:00", "distance_to_stop_ft": 1424, "stop_sequence": 13, ...}`. The
old collector tracks only some routes (22, 63, 65, 66 at the time of writing).

Report (plain-text tables):
1. **Fix-time matching.** For each old ping, find the new DB's position for the same vehicle
   at the same spot (within 3 ft), taking the latest `fix_ts <= requested_at + 5 s`. Print
   the match rate, and the distribution (p10/median/p90/max) of `requested_at - fix_ts` (how
   stale the old pings really were). Break unmatched pings into "vehicle absent from GTFS",
   and "position never in GTFS" (superseded before a feed refresh).
2. **Coverage.** For the routes the old collector tracks: distinct positions per
   vehicle-hour in old vs new, and positions only one side has.
3. **Pattern mapping.** For matched pings, tabulate old `pattern_id` against
   `GeometryResolver.pattern_for_trip` and the GTFS `shape_id`. Show every
   combination with counts, so a one-to-one mapping (and any exceptions) is visible.
4. **Trip ids.** For old snapshots with predictions, compare the old nearest-stop
   prediction's `trip_id` with the GTFS vehicle's `trip_id` at the matched fix. Print the
   agreement rate, and list disagreements by distance from the trip's start or end (expect
   them at trip boundaries).
5. **Predictions.** For old predictions, map `stop_id` (BusTime stpid = GTFS stop_code)
   to a GTFS stop_id, and take the new DB's `predictions_at` for that trip and stop at the
   snapshot nearest the old `requested_at`. Print the distribution of
   `old predicted_arrival_at - new arrival_time` (expect about −60..+60 s, since the old
   times are truncated to the minute).

### 9.5 `check_geometry.py`
For a window (default: the last 24 h), snap every in-service position and report the
offset distribution (p50/p90/p99, and the share over 150 ft) per route. Also, for each
static shape in use, compare our stop distances along the shape (spurs cut) with the
agency's `shape_dist_traveled` (in feet), reporting per-shape median and max differences.
A spot check on four shapes found that the agency doesn't count spurs (§2.2), with our
lengths 0.2–0.7% longer than theirs. This script checks that on every shape in use, stop
by stop.

---

## 10. Tests (offline)

Fixtures already in `tests/fixtures/` (real captures, 2026-10-25 ~22:12 UTC):
- `vehicles.pb`: header 1792966315, 41 vehicles, 29 with a trip. Route 4 has buses
  `1933, 2002, 553`; route 22 has `505, 555`.
- `trips.pb`: same header. 45 trip updates (3 with `modified_trip`), 6
  `trip_modifications` (`shape_501393` with 108 trips, `shape_501401` with 10,
  `shape_501400` with 82, `shape_501402` with 9, `shape_501314` with 80,
  `shape_501391` with 61), 6 shapes, 696 stop-time updates. No `start_date` or `delay`.
- `alerts.pb`: same header, 19 alerts, no active periods.
- `gtfs_static_4_22.zip`: the real static feed cut down to routes 4 and 22 (452 trips,
  15,257 stop times, 132 stops, shapes `shp-4-06`, `shp-4-55`, `shp-22-01`, `shp-22-04`),
  with the full calendar, agency and feed_info.
- `getroutebyid_22.json`: a route 22 `getroutebyid` payload with an active detour (from
  the old project).

Required coverage (use a temporary data dir via `tmp_path` and an in-memory or temp
SQLite):
- **Parsing:** record counts and a few field values from each `.pb`; optional fields absent
  → `None`.
- **Dedup:**
  - Storing the same vehicles message twice adds no rows.
  - Changing one vehicle's `fix_ts` adds exactly one position.
  - The same trips message twice adds no `trip_update`/`stop_time_prediction` rows, but
    advances `last_seen_snapshot_id`.
  - Changing one stop's `arrival.time` adds one `trip_update` row and one
    `stop_time_prediction` row.
  - Re-sending identical trip modifications, RT shapes or alerts only bumps
    `last_snapshot_id`; changed content adds a version.
  - Build the modified messages in tests by editing parsed `FeedMessage`s and re-serialising.
- **Restart:** a new `Store` on the same DB, fed the same messages, inserts nothing.
- **Static:** load the zip and check counts. Loading it again is a no-op. A re-zipped copy
  (different member order or timestamps) gets the same content id. `runs_on`, and
  `service_date_for` around midnight and on a day the trip doesn't run.
- **Geometry:**
  - The polyline test vector (§6.5).
  - `shp-22-04`: all 34 stops attach to vertices, spur removal drops exactly 56
    vertices (1,247 → 1,191) and the length goes from 43,219 to 41,635 ft (±1 ft).
    `shp-22-01` loses none (707 vertices, 38,011 ft).
  - Projection round-trip (`point_at(project(p).dist) ≈ p`).
  - `near_ft` picks the right pass on an out-and-back test path.
  - Fixture vehicles on route 22 snap within 20 ft.
- **Resolver:** a fixture trip listed in `trip_modifications` gets the RT shape; an
  unmodified trip gets its static shape. Matching `shp-22-01` and `shp-22-04` against
  `getroutebyid_22.json` by stop sequence: if the detour in that payload breaks the exact
  match, assert on the regular-path pattern instead.
- **Collector scheduling:** test the next-poll-time function as a pure function (new H,
  unchanged, stalled backoff, error backoff). Run one realtime cycle against an
  `httpx.MockTransport` serving the fixtures (use `asyncio.run` inside the test; no
  pytest-asyncio needed).
- **Map:** build a small map from fixture-loaded data, render it (`fmap.get_root().render()`),
  and assert that the CSS classes, the latest pills and the attribution text are present.
- Every request carries the `User-Agent` (assert on it in the mock transport).

Also add `tests/test_live.py`, marked `@pytest.mark.live` and **skipped by default**
(register the marker in pyproject; run with `-m live`). It fetches each feed once and
checks that they parse and that the header timestamp is less than 5 minutes old. It warns
instead of failing when there are no vehicles (overnight).

---

## 11. Docs to write

- `README.md`: what this is, how to run the collector (Docker and local), how to make the
  map (including `--serve` with port forwarding), where data lives, a summary of the storage
  layout, and the TheRide credit line.
- `AGENTS.md`: guidance for coding agents in the style of `../AAATA-Bus-API/AGENTS.md`:
  overview, a module-by-module layout, the workflow (`uv run pyrefly check`, `uv run
  pytest`), conventions, and **gotchas** (copy the relevant facts from §2 and §12). Include
  "keep this file updated".
- `CLAUDE.md`: `@AGENTS.md` plus a two-line summary, like the old project's.

---

## 12. Gotchas carried over from the old project
- Be polite: identify with the User-Agent on every request, no tight loops, and keep the
  courtesy waits for per-route BusTime calls.
- Positions are already snapped to the street. Occasionally a position jumps a few hundred
  feet backwards for one report and then recovers (an AVL quirk, not a snapping error).
- A stopped bus keeps reporting the same position with new fix times. That is real data.
- Where a pattern runs both ways along one street (e.g. route 66 on Green Rd, out to the
  park and ride and back), pings after the turnaround can snap to the outbound pass. The
  `near_ft` hint in `snap_sightings` mitigates this; Phase 2's trip reconstruction must
  handle the rest.
- Predictions run on into a bus's next trip. Filter by trip id.
- Detours change the path a trip follows. Never join a regular path and its detour path
  when drawing.
- Route-level data changes only between collection checks (hourly for BusTime routes,
  6-hourly for static), so a change can only be timed to within that interval.

---

## 13. Phase 1 definition of done
1. `uv run pyrefly check` and `uv run pytest` pass.
2. `uv run scripts/collect.py --data-path /tmp/x` runs against the live feeds for at least
   3 minutes, and then:
   - `collector_status.py` shows about 6 vehicles and 6 trips snapshots, ~1 alerts
     snapshot, 1 static version and ~30 BusTime route versions;
   - restarting the collector and running it 1 more minute adds no duplicate rows (check
     the counts by key);
   - `plot_route_paths.py --data-path /tmp/x` writes a map with shapes, stops, raw and
     snapped pings and latest pills.
3. `docker compose build` succeeds and `docker compose up -d` collects into `./data`, with
   the files owned by the host user.
4. README.md, AGENTS.md and CLAUDE.md are written. Commit on a branch (no remote is set up
   yet).

---

## 14. Phase 2 (outline only; do not build yet)

Discuss modelling decisions (labels, train/test splits, baselines, evaluation) with the
user before implementing any model. Clean data in explicit **filtering stages** that
output clean records, rather than adding flags that every consumer has to remember to
check.

1. **Trip reconstruction:** port `trips.py`, but trips come from `trip_id` + `service_date`
   directly, so no majority vote is needed. Handle stale trip ids at trip boundaries (use
   the trip-updates feed's vehicle assignment and progress along the shape). Fit distance
   along the shape with isotonic regression, against **fix time** instead of poll time.
   Filter stale repeats and backward jumps in a stage.
2. **Observed stop arrivals:** interpolate each trip's distance-vs-fix-time to every stop's
   distance. Cross-check against the time `first_stop_sequence` passed the stop (§4.3.3).
3. **Agency prediction accuracy:** compare `stop_time_prediction` (as of each observation
   time and lead time) with the observed arrivals. This is the baseline any model has to
   beat.
4. **GPS lag:** port `rides.py` and `gps_lag.py`. Phone rides now measure the lag between
   `fix_ts` and the true time directly, which settles whether `fix_ts` is the GPS fix time
   or the server's receive time.
5. **Segment speeds and travel-time distributions** (port `segments.py`), as features for a
   probabilistic arrival-time model.
6. Visualizations: trip time–distance plots, ride lag plots and segment speed maps, ported
   from the old `visualization/` package.
