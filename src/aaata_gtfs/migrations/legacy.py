"""The schema scripts shipped before Alembic, kept to bring old databases up to date.

Until Alembic, the schema version was kept in `PRAGMA user_version` and these
scripts were applied in order. A database with an older `user_version` is
brought up to the last one here, then stamped at Alembic's baseline revision
(`versions/0001_baseline.py`), which creates this same schema on a new database.
Never edit these.
"""

SCHEMA_V1 = """
CREATE TABLE collector_run (
  id          INTEGER PRIMARY KEY,
  started_at  REAL NOT NULL,
  stopped_at  REAL,
  version     TEXT NOT NULL,
  args        TEXT NOT NULL
);

CREATE TABLE feed_snapshot (
  id          INTEGER PRIMARY KEY,
  feed        TEXT NOT NULL CHECK (feed IN ('vehicles','trips','alerts')),
  header_ts   INTEGER NOT NULL,
  fetched_at  REAL NOT NULL,
  sha256      TEXT NOT NULL,
  n_entities  INTEGER NOT NULL,
  n_bytes     INTEGER NOT NULL,
  blob        TEXT,
  run_id      INTEGER NOT NULL REFERENCES collector_run(id),
  UNIQUE (feed, header_ts)
);

CREATE TABLE vehicle_position (
  vehicle_id      TEXT NOT NULL,
  fix_ts          INTEGER NOT NULL,
  lat             REAL NOT NULL,
  lon             REAL NOT NULL,
  bearing         REAL,
  speed_mps       REAL,
  trip_id         TEXT,
  route_id        TEXT,
  service_date    TEXT,
  trip_schedule_relationship TEXT,
  direction_id    INTEGER,
  start_date      TEXT,
  start_time      TEXT,
  current_stop_sequence INTEGER,
  stop_id         TEXT,
  current_status  TEXT,
  occupancy_status TEXT,
  congestion_level TEXT,
  odometer        REAL,
  vehicle_label   TEXT,
  snapshot_id     INTEGER NOT NULL REFERENCES feed_snapshot(id),
  PRIMARY KEY (vehicle_id, fix_ts)
) WITHOUT ROWID;
CREATE INDEX vehicle_position_ts   ON vehicle_position(fix_ts);
CREATE INDEX vehicle_position_trip ON vehicle_position(trip_id, service_date, fix_ts);

CREATE TABLE vehicle_seen (
  vehicle_id        TEXT NOT NULL,
  first_snapshot_id INTEGER NOT NULL REFERENCES feed_snapshot(id),
  last_snapshot_id  INTEGER NOT NULL REFERENCES feed_snapshot(id),
  PRIMARY KEY (vehicle_id, first_snapshot_id)
) WITHOUT ROWID;
CREATE INDEX vehicle_seen_last ON vehicle_seen(last_snapshot_id);

CREATE TABLE trip_update (
  id                   INTEGER PRIMARY KEY,
  trip_id              TEXT NOT NULL,
  service_date         TEXT,
  route_id             TEXT,
  vehicle_id           TEXT,
  update_ts            INTEGER,
  schedule_relationship TEXT,
  modifications_id     TEXT,
  delay                INTEGER,
  first_stop_sequence  INTEGER,
  n_stop_time_updates  INTEGER NOT NULL,
  snapshot_id          INTEGER NOT NULL REFERENCES feed_snapshot(id),
  last_seen_snapshot_id INTEGER NOT NULL REFERENCES feed_snapshot(id)
);
CREATE INDEX trip_update_trip ON trip_update(trip_id, service_date, snapshot_id);
CREATE INDEX trip_update_vehicle ON trip_update(vehicle_id, snapshot_id);
CREATE INDEX trip_update_snapshot ON trip_update(snapshot_id);

CREATE TABLE stop_time_prediction (
  trip_update_id   INTEGER NOT NULL REFERENCES trip_update(id),
  trip_id          TEXT NOT NULL,
  service_date     TEXT,
  stop_sequence    INTEGER NOT NULL,
  stop_id          TEXT NOT NULL,
  arrival_time     INTEGER,
  departure_time   INTEGER,
  arrival_delay    INTEGER,
  departure_delay  INTEGER,
  arrival_uncertainty   INTEGER,
  departure_uncertainty INTEGER,
  schedule_relationship TEXT,
  observed_ts      INTEGER NOT NULL,
  snapshot_id      INTEGER NOT NULL REFERENCES feed_snapshot(id)
);
CREATE INDEX stop_time_prediction_key ON stop_time_prediction(trip_id, service_date, stop_sequence, snapshot_id);
CREATE INDEX stop_time_prediction_stop ON stop_time_prediction(stop_id, observed_ts);
CREATE INDEX stop_time_prediction_snapshot ON stop_time_prediction(snapshot_id);

CREATE TABLE trip_modifications_version (
  id                 INTEGER PRIMARY KEY,
  modifications_id   TEXT NOT NULL,
  pattern_id         TEXT,
  content_sha256     TEXT NOT NULL,
  content_json       TEXT NOT NULL,
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
  shape_id          TEXT NOT NULL,
  content_sha256    TEXT NOT NULL,
  encoded_polyline  TEXT NOT NULL,
  first_snapshot_id INTEGER NOT NULL REFERENCES feed_snapshot(id),
  last_snapshot_id  INTEGER NOT NULL REFERENCES feed_snapshot(id)
);
CREATE INDEX rt_shape_version_id ON rt_shape_version(shape_id, first_snapshot_id);

CREATE TABLE alert_version (
  id                INTEGER PRIMARY KEY,
  alert_id          TEXT NOT NULL,
  content_sha256    TEXT NOT NULL,
  content_json      TEXT NOT NULL,
  header_text       TEXT,
  route_ids         TEXT,
  first_snapshot_id INTEGER NOT NULL REFERENCES feed_snapshot(id),
  last_snapshot_id  INTEGER NOT NULL REFERENCES feed_snapshot(id)
);
CREATE INDEX alert_version_id ON alert_version(alert_id, first_snapshot_id);

CREATE TABLE static_version (
  id              TEXT PRIMARY KEY,
  content_sha256  TEXT NOT NULL UNIQUE,
  zip_sha256      TEXT NOT NULL,
  fetched_at      REAL NOT NULL,
  last_checked_at REAL NOT NULL,
  etag            TEXT,
  last_modified   TEXT,
  feed_version    TEXT,
  feed_start_date TEXT,
  feed_end_date   TEXT,
  blob            TEXT NOT NULL
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
  service_id TEXT NOT NULL, days TEXT NOT NULL,
  start_date TEXT NOT NULL, end_date TEXT NOT NULL,
  PRIMARY KEY (static_version, service_id)) WITHOUT ROWID;
CREATE TABLE static_calendar_date (static_version TEXT NOT NULL REFERENCES static_version(id),
  service_id TEXT NOT NULL, date TEXT NOT NULL, exception_type INTEGER NOT NULL,
  PRIMARY KEY (static_version, service_id, date)) WITHOUT ROWID;

CREATE TABLE bustime_route_version (
  id              INTEGER PRIMARY KEY,
  route_id        TEXT NOT NULL,
  route_version   TEXT NOT NULL,
  content_sha256  TEXT NOT NULL,
  fetched_at      REAL NOT NULL,
  last_checked_at REAL NOT NULL,
  blob            TEXT NOT NULL
);
CREATE INDEX bustime_route_version_route ON bustime_route_version(route_id, fetched_at);
"""

#: Stop storing the agency's per-stop predictions: they changed on almost every
#: refresh and were ~60% of the database. The raw trips archive keeps them.
DROP_STOP_TIME_PREDICTION = """
DROP TABLE stop_time_prediction;
"""

#: Migration n (1-based) is MIGRATIONS[n - 1].
MIGRATIONS = [SCHEMA_V1, DROP_STOP_TIME_PREDICTION]
#: Migrations that free enough space to be worth a VACUUM afterwards.
VACUUM_AFTER = {2}


def statements(script: str) -> list[str]:
    """A script's statements (none of them contain a `;`)."""
    return [s.strip() for s in script.split(";") if s.strip()]
