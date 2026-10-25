# aaata-gtfs

A polite, always-on collector for TheRide's (AAATA, Ann Arbor) public **GTFS-realtime**
feeds, plus static GTFS and BusTime's `getroutebyid` as a cross-reference, stored in
**SQLite plus a raw file store**, with tools to map and check the data.

Compared with polling the website's BusTime proxy (the sibling `../AAATA-Bus-API`
project), GTFS-realtime gives every bus's **GPS fix time to the second** (plus speed and
bearing), the whole fleet in one ~2.3 KB request, per-stop predictions to the second, and
detours as `trip_modifications` with their own shapes. The collector stores nothing twice
when nothing changed between calls.

The full design is in [SPEC.md](SPEC.md); guidance for coding agents is in
[AGENTS.md](AGENTS.md).

> Transit scheduling, geographic, and real-time data provided by permission of AAATA/TheRide

## Running the collector

### Docker (recommended for always-on)

```bash
mkdir -p data                                       # so Docker doesn't create it owned by root
printf 'UID=%s\nGID=%s\n' "$(id -u)" "$(id -g)" > .env   # only needed if you aren't 1000:1000
docker compose up -d --build
docker compose logs -f collector
```

The container runs as your user (`user: "${UID:-1000}:${GID:-1000}"`, read from the
gitignored `.env`; bash's `UID` is read-only and not exported, so it can't be set inline),
so everything in `./data` stays yours.
`docker compose stop` sends SIGTERM; the collector records its stop time and closes the DB.
Other scripts run in the same image by name, e.g.
`docker compose run --rm collector collector_status.py`.

### Locally

```bash
uv sync
uv run scripts/collect.py                         # data goes to ./data
uv run scripts/collect.py --data-path /tmp/x      # or anywhere else
```

Options: `--no-raw-archive` (don't keep raw realtime payloads), `--alerts-interval-s 300`,
`--static-interval-h 6`, `--bustime-interval-min 60`, `--no-bustime`, `--log-level INFO`.
The data directory can also be set with `AAATA_GTFS_DATA_PATH`.

What it polls:

| Feed | When |
|---|---|
| vehicles + trips (`rt.theride.org/gtfsrt/…`) | once per feed refresh (~30 s): the next poll is timed from the feed's header timestamp, about 2 requests per 30 s |
| alerts | every 5 min, stored only when the content changed |
| static GTFS zip | at startup and every 6 h, conditional GET |
| BusTime `getroutebyid` | after the first static load and hourly, every route, 2–5 s apart |

Every request carries `User-Agent: aaata-gtfs (research data collector; contact: …)`.

### Is it working?

```bash
uv run scripts/collector_status.py [--data-path PATH]
```

prints the latest snapshot per feed and its age, the current run, rows per table (total
and per day for a week), gaps between vehicles snapshots over 90 s in the last day, version
counts, active detours, and disk use.

## Making the map

```bash
uv run scripts/plot_route_paths.py                        # last 2 h of data
uv run scripts/plot_route_paths.py --routes 4 22 --start 2026-10-25T16:00 --end 2026-10-25T18:00
uv run scripts/plot_route_paths.py --serve                # then open the printed URL
```

It writes `data/route_paths.html` (or `--output`): one layer per route with its shapes
(detour shapes dashed) and stops, each bus's pings joined in fix-time order, a toggle to
show the pings snapped onto their trip's shape, and the latest position of each active bus
as a fleet-number pill. Times are local (America/Detroit) when no offset is given.
Other options: `--max-gap-minutes 15`, `--no-points`, `--no-latest`, `--no-route-shapes`,
`--no-stops`, `--pings raw|snapped`, `--max-offset-ft 150`.

`--serve [PORT]` serves the map on `127.0.0.1:8000` after writing it. On a remote machine,
forward the port (VS Code does this automatically, or `ssh -L 8000:localhost:8000 host`)
and open the printed URL.

## Other scripts

- `scripts/check_geometry.py`: how far in-service positions are from their trip's shape
  (p50/p90/p99 per route), and our stop distances along each shape in use (stop spurs cut)
  against the agency's `shape_dist_traveled`.
- `scripts/compare_with_old.py`: the parallel-run comparison with the old project's
  `route_prediction_data.jsonl` (fix-time matching, coverage, BusTime pattern ↔ GTFS shape
  mapping, trip ids, predictions). Reads the old file without changing it.

## Where the data lives

Everything is under `data/` (gitignored; override with `--data-path` or
`AAATA_GTFS_DATA_PATH`):

```
data/
  aaata_gtfs.sqlite3                               # all parsed data (WAL mode)
  blobs/
    rt/<feed>/<YYYY>/<MM>/<DD>/<HH>.pbs.gz         # raw realtime payloads, one gzip member each
    static/<content_id>.zip                        # every static GTFS version
    bustime/getroutebyid/<route_id>/<sha12>.json.gz
  route_paths.html                                 # the map, when made
```

The database is the source of truth; the blobs let everything be re-parsed if a parser
bug turns up. Expect roughly 30 MB/day of blobs and the same again in the DB.

Storage in brief (details in SPEC.md §4):

- **Times** are UTC epoch seconds (feed times INTEGER, our clock REAL); service dates are
  `YYYYMMDD`; static schedule times are seconds after the service day's local noon − 12 h.
- `feed_snapshot`: one row per *new* feed message (new header timestamp); repeats aren't stored.
- `vehicle_position`: one row per `(vehicle_id, fix_ts)` — the GPS fix time, so a stale fix
  repeated across refreshes is stored once. `vehicle_seen` records contiguous presence in the feed.
- `trip_update` + `stop_time_prediction`: a trip instance `(trip_id, service_date)` gets a
  row only when something changed, and only the stops whose prediction changed.
  `queries.predictions_at` reconstructs what was predicted at any snapshot.
- `trip_modifications_version`, `rt_shape_version`, `alert_version`,
  `bustime_route_version`, `static_version`: content-versioned, with the first and last
  snapshot (or check) each version was seen in.
- `static_*` tables: every static GTFS version, so trips are looked up by service date.

Scripts open the DB read-only, so they can run while the collector writes.

## Development

```bash
uv sync
uv run pyrefly check
uv run pytest              # offline, against real captures in tests/fixtures/
uv run pytest -m live      # also hits the real feeds
```
