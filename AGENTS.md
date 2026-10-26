# AGENTS.md

Guidance for AI coding agents working in this repository. **Keep this file updated** as
the project changes: new modules, conventions, and anything learned about the feeds.

## Project overview

`aaata-gtfs` collects TheRide's (AAATA, Ann Arbor) public, keyless **GTFS-realtime**
feeds (vehicle positions, trip updates with trip modifications and shapes, alerts), the
static GTFS feed, and BusTime's `getroutebyid` (as a cross-reference) into **one SQLite
database plus a raw blob store**, never storing a duplicate when nothing changed. It also
has linear referencing on GTFS shapes, a folium route map, and operational scripts. It
replaces the sibling project `../AAATA-Bus-API` (`aaata_bus`), which polls BusTime's
website proxy; the two run in parallel for a while (`scripts/compare_with_old.py`).

[SPEC.md](SPEC.md) is the full design. Phase 1 (this code) is the collector, storage,
geometry and map. Phase 2 (trip reconstruction, observed arrivals, prediction accuracy,
models) is outlined at the end of SPEC.md: **discuss modelling decisions with the user
before implementing any model**, and clean data in explicit filtering stages rather than
flags every consumer must check.

## Layout

- `src/aaata_gtfs/` — the package (uv build backend, `src` layout, `py.typed`).
  - `config.py` — feed URLs, `USER_AGENT` / `REQUEST_HEADERS`, `DATA_PATH` (from
    `AAATA_GTFS_DATA_PATH`, default `<repo>/data`), `db_path()` / `blob_path()` for a
    given data path, `ensure_data_dir()` (creates it), polling constants, `LOCAL_TZ`
    (America/Detroit), and `THERIDE_CREDIT` (required attribution).
  - `http.py` — `make_client()` (the one shared `httpx.AsyncClient`; takes a
    `transport` for tests), `get()` (sends `REQUEST_HEADERS` per request, raises on 4xx/5xx
    but not 304), `get_bytes()`, `backoff_delay(n)` (5, 10, 20 … 120 s).
  - `db.py` — `connect(path, read_only=False)` with the pragmas (WAL, `synchronous=NORMAL`,
    foreign keys, `busy_timeout`), `migrate()` over `MIGRATIONS` using `PRAGMA
    user_version`, `open_db()`, and `TABLES`. Migration 1 is the schema in SPEC.md §4.3
    plus a few indexes (`*_snapshot`, `vehicle_seen_last`) for status queries. **Append new
    migrations; never edit a shipped one.**
  - `filestore.py` — raw blobs under `<data>/blobs/`: `append_rt` / `iter_rt` for hourly
    RT files of appended gzip members (8-byte BE header_ts, 4-byte BE length, payload),
    `put_static`, `put_bustime` (gzipped, named by canonical hash), `read_blob`. Paths in
    the DB are relative to the data path.
  - `realtime.py` — pure protobuf → frozen dataclasses: `VehicleRecord`,
    `TripUpdateRecord` (+ `StopTimeRecord`s), `TripModificationsRecord`, `RtShapeRecord`,
    `AlertRecord`; `parse_feed`, `header_ts`, `content_sha256` (entities only, header
    timestamp zeroed; used to dedup alerts), `canonical_json`. Optional fields go through
    `_int`/`_float`/`_str`/`_enum` (`HasField`), so absent is `None`; enums are stored by
    name.
  - `static_gtfs.py` — `fetch_static` (conditional GET → `FetchResult | NotModified`),
    `load_static` (idempotent; one transaction; content id = sha256 over sorted
    `(member name, member sha256)` of the `.txt` files, ignoring `__MACOSX/`),
    `touch_static`, and `StaticGtfs`, a cached read side: `trip(trip_id, service_date)`
    (prefers the most recently fetched version whose feed dates cover the date),
    `stop_times`, `trip_span`, `shape_points`, `stops`, `routes`, `route_trips`,
    `route_stop_uses`, `stop_code_to_id`, `runs_on`, `version_for`, `latest_version`.
    Call `reload()` after loading a new version.
  - `service_dates.py` — `service_date_for(static, trip_id, ts)`: the feed sends no
    `start_date`, so a trip instance's service date is the day (yesterday or today, local)
    on which the trip runs and whose scheduled window ±2 h contains `ts`. Also
    `schedule_ts(service_date, seconds)` (local noon − 12 h + s, right across DST).
  - `bustime.py` — `fetch_route_payload`, `canonical_payload` / `payload_hash` (ported
    from the old `route_history.py`), `route_version_id`, and minimal `parse_patterns` →
    `BusTimePattern` (`path_stop_ids()`: the driven path; `regular_stop_ids()`: the
    bypassed regular path on a detoured pattern).
  - `store.py` — `Store`: dedup-aware writers with in-memory last-state caches rebuilt
    from the DB at construction (restart-safe). `store_feed(feed, payload, fetched_at) ->
    SnapshotResult` (raises `MalformedFeed` after archiving), `store_bustime`, `prune`,
    `stats`. Also `start_run` / `finish_run` for `collector_run`. The dedup rule per table
    is in SPEC.md §4.3; each snapshot is one transaction.
  - `collector.py` — `Collector` (owns the connection, `Store`, client) with
    `realtime_cycle`, `alerts_once`, `static_once`, `bustime_once` and their loops, run in
    an `asyncio.TaskGroup` by `run_collector` (SIGTERM/SIGINT → set `stopped_at`, close).
    `next_poll_delay(now, latest_header_ts, unchanged_polls, consecutive_errors)` is the
    pure scheduling function. Static loading runs in a thread on its own connection,
    under the same `asyncio.Lock` that serializes all writes.
  - `polyline.py` — `decode_polyline` (Google algorithm).
  - `geometry.py` — `ShapeGeometry(key, route_id, points, stops, stop_tolerance_ft,
    shape_id=None)`, ported from the old `PatternGeometry`: stops attach to the first
    vertex at or after the previous stop's within tolerance (1 ft static, 6 ft RT), stop
    spurs are cut, then `project` / `project_many` (chunked by 2,000 points) /
    `point_at` / `path_between` / `stop_distances` / `agency_dist_ft`. `snap_sightings`
    projects one vehicle's time-ordered positions near the previous on-route one on the
    same geometry `key`. `Sighting` and `GeometrySource` are the protocols it needs.
  - `shapes.py` — `GeometryResolver(conn, static, data_path)`: `for_trip(trip_id,
    service_date, t)` (the detour's RT shape while a modification selecting the trip is in
    effect, else the static shape; cached per key + stop list), `static_geometry`,
    `rt_geometry`, `modification_for`, `modifications_between`, `rt_shapes_between`, and
    `pattern_for_trip` (BusTime pid: the detour's `pattern_id`, else the undetoured
    pattern whose stop ids equal the static trip's stop codes). Loads all modification and
    RT shape versions at construction; `refresh()` to reload.
  - `queries.py` — read helpers: `positions`, `data_time_range`, `latest_vehicles_snapshot`,
    `active_positions` / `active_vehicles` (in the snapshot and fix ≤ 120 s old),
    `predictions_at`, `snapshot_near`, `all_trip_modifications` /
    `trip_modifications_at`, `all_rt_shapes` / `rt_shape_at`, `bustime_route_versions`.
    RT entities are in effect from their first snapshot − 60 s to their last + 60 s.
  - `visualization/` — `data.py`: `BusPath` (route None = not in service),
    `trace_bus_paths` (split on route change or gap), `latest_positions`
    (`LatestPosition` with `age_s`). `route_paths.py`: `route_drawings` (static shapes of
    the version in effect at the window end, RT detour shapes in effect during it,
    `detour-regular` when every trip of a static shape running in the window is
    detoured; stops once each) and `plot_route_paths` (port of the old map: same colors,
    CSS classes `route-shape`, `bus-ping`, `ping-raw`, `ping-snapped`, `bus-latest`,
    `detour-path`, `detour-regular`, `_OverlayToggle` template, TheRide credit in the
    attribution). `serve.py`: localhost server for `--serve`.
  - `__init__.py` — re-exports the public API; **keep `__all__` current**.
- `scripts/` — `collect.py`, `plot_route_paths.py`, `collector_status.py`,
  `compare_with_old.py`, `check_geometry.py`; run with `uv run scripts/<name>.py`. All
  take `--data-path`. Everything except `collect.py` opens the DB read-only.
- `tests/` — offline pytest suite over real captures in `tests/fixtures/` (see SPEC.md
  §10); `conftest.py` has the shared fixtures (`static_conn`, `store`, `loaded_store`,
  `with_header` to make a "next refresh" of a fixture). `test_live.py` is marked `live`
  and skipped by default (`uv run pytest -m live`).
- `tasks/` — design notes for planned work, referenced from SPEC.md.
  `bus-position-model.md` is the agreed Phase 2 model design; keep it current as
  decisions change.
- `data/` — collected data (gitignored).
- `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, `.dockerignore` — the
  containerized collector, running as the host user.

## Workflow

Dependencies are managed with **uv** (Python ≥ 3.13). `uv sync` installs everything,
including the dev group (pytest, pyrefly).

After making changes, always run both and fix anything you introduced:

```bash
uv run pyrefly check   # type checking (config in pyrefly.toml; checks unannotated defs too)
uv run pytest          # offline tests
```

Add or update tests when changing behavior. Tests build modified feed messages by editing
parsed `FeedMessage`s and re-serialising (`tests/conftest.py`), and fake the network with
`httpx.MockTransport` (see `tests/test_collector.py`); use `asyncio.run` inside a test
rather than pytest-asyncio.

## Conventions

- Type annotations everywhere; frozen dataclasses for records. No ORM, pandas or
  pydantic: stdlib `sqlite3`, `csv`, `zipfile`, `json`, `hashlib`, `zoneinfo`, `argparse`.
- protobuf has stubs (`gtfs_realtime_pb2.pyi`); read optional fields through the small
  typed helpers in `realtime.py` instead of sprinkling ignores. pyrefly warns that
  `google.protobuf.*` has no stubs (`types-protobuf` isn't a dependency); that's expected.
- `xyzservices` providers are dynamic: use `providers.query_name("Esri.WorldGrayCanvas")`.
- **Times:** store UTC epoch seconds (INTEGER for feed times, REAL for our clock), never
  naive datetimes. Service dates are `YYYYMMDD` text. Display in `LOCAL_TZ`.
- **No duplicates:** every writer goes through `Store`, and anything cached in memory must
  be rebuildable from the DB in `Store.load_caches` (a restart must insert nothing).
- All HTTP goes through `http.get` / `get_bytes` on the shared client, so the `User-Agent`
  is always sent.
- Scripts that write call `ensure_data_dir()`; readers use `db.connect(..., read_only=True)`.

## Gotchas

- **Be polite:** identify with the User-Agent on every request, no tight loops; keep the
  2–5 s courtesy wait between per-route `getroutebyid` calls.
- **Feed cadence:** vehicles and trips regenerate together every 27–33 s with the same
  `header.timestamp`, available 1–5 s after it. No `ETag`/`Last-Modified` (conditional
  requests return 200), so change is detected by header timestamp. The realtime loop polls
  at `H + 33 s`, then every 4 s until new, then every 15 s after 8 misses.
- **Alerts** carry the same regenerating header but rarely change, so they're deduped on
  content: `alerts_content_sha256` hashes the sorted per-alert canonical hashes.
  **TheRide shuffles each alert's translation order (en/es/pt) between fetches**, so
  `alert_records` sorts `translation`, `informed_entity` and `active_period` before
  hashing; hashing the serialized message would store a "new" version of every alert on
  every poll. `feed_snapshot.sha256` holds that content hash for alerts and the raw body
  hash for vehicles/trips.
- `FeedEntity.id` in vehicles/trips is `"1"`, `"2"`, … per message: **not a key**. Key on
  vehicle id + fix time, trip id + service date, modification/shape ids. For alerts the
  entity id is the title, which is stable.
- `VehiclePosition.timestamp` is the GPS fix time (3–34 s old at generation for live buses);
  a bus that stops reporting repeats its stale fix until it drops out. A stopped bus sends
  new fix times at the same position — real data, keep it.
- **Trip ids at trip boundaries can be stale:** a bus that just started trip B may still
  report trip A in its vehicle position while the trips feed shows it on B.
- Trip updates list upcoming trips too (no `vehicle`), drop passed stops (so the lowest
  `stop_sequence` shows progress), and send no `start_date` or `delay`. The first stop has
  only `departure`.
- **Detours** are `trip_modifications`: the id's trailing digits are the BusTime pattern id
  (`trip_modifications_501393` → `501393`); `selected_trips` give the trips and the RT
  `shape_id`; shapes are encoded polylines (1e-5°, ~3.6 ft) without stop spurs.
- **Unannounced detours exist.** In the fixtures, bus 555 (route 22, trip 1511010, shape
  `shp-22-01`) is ~1,630 ft off its static shape but 14 ft from BusTime's Nixon detour
  path (pattern `501321`, in `getroutebyid_22.json`), which GTFS-RT carries no
  modification for. Expect a few in-service positions far off their shape.
- **Stop ids:** GTFS `stop_id` ≠ BusTime stop id. BusTime's `stpid` is GTFS `stop_code`
  (GTFS `551` = code `348` = BusTime `348`). GTFS-RT uses GTFS `stop_id`s.
- **Shapes:** every stop lies on a shape vertex; many are drawn as out-and-back spurs to
  the curb (15–121 ft). The agency's `shape_dist_traveled` (metres) doesn't count spurs;
  with them cut our lengths are within ~0.5% (`shp-22-04`: 1,247 → 1,191 vertices,
  43,219 → 41,635 ft; agency 41,450 ft).
- **Pattern ↔ shape matching** (`pattern_for_trip`) uses exact stop-code sequences against
  undetoured patterns only; while BusTime shows a detour, the regular shape has no exact
  match (its stops equal the detoured pattern's *bypassed* path). Matching is only for the
  parallel-run comparison.
- **Static GTFS** is replaced at each service change and can be published early, so look
  trips up by service date (`StaticGtfs.trip(trip_id, service_date)`), never "latest".
  The zip has a `__MACOSX/` directory; ignore it. Schedule times can exceed 24:00:00.
- `getroutebyid`: collections are wrapped (`{"pas": {"pa": [...]}}`), a single element is a
  bare object, empty collections/missing scalars are `[]`, and **pattern order varies
  between fetches** (hence `canonical_payload`). A detoured pattern lists the full detour
  path (`dtr: false`) then the full bypassed path (`dtr: true`); never join them. Unknown
  route ids return a nameless stub, not an error. It lists only the patterns currently
  in use, so a route can gain or lose one between hourly checks (route 46 added short-turn
  pattern 7659 one evening): those are real new versions, not hashing noise.
- Endpoints that don't work (don't retry): `ServiceData` methods off its whitelist return
  an empty 200; `rt.theride.org/bustime/api/v3/*` needs a key;
  `aaatarealtimebus.azurewebsites.us` is 403.
- Positions are already snapped to the street. Occasionally one jumps a few hundred feet
  backwards for one report and recovers (an AVL quirk).
- Where a shape runs both ways along a street, pings after the turnaround can snap to the
  outbound pass; the `near_ft` hint in `snap_sightings` mitigates this.
- Predictions run on into a bus's next trip: filter by trip id.
- Route-level data only changes between checks (hourly BusTime, 6-hourly static), so
  changes are timed to within that interval.
- **Docker:** the compose service runs as `${UID:-1000}:${GID:-1000}` (bash's `UID` is
  read-only and unexported; set both in `.env`), so `./data` stays owned by the host user.
  Create `./data` before the first `up`, or Docker creates it as root. The entrypoint runs
  the venv's python directly (not `uv run`, which needs a writable cache).
- Displays of the data must credit: "Transit scheduling, geographic, and real-time data
  provided by permission of AAATA/TheRide" (`THERIDE_CREDIT`).
