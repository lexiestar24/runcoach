"""SQLite schema + connection helpers for runcoach."""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runcoach.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    date            TEXT PRIMARY KEY,   -- YYYY-MM-DD
    resting_hr      INTEGER,
    min_hr          INTEGER,
    max_hr          INTEGER,
    avg_stress      INTEGER,
    rhr_7day        INTEGER,
    body_battery_low  INTEGER,
    body_battery_high INTEGER,
    steps           INTEGER,
    total_kcal      INTEGER,
    active_kcal     INTEGER,
    resp_avg        REAL,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sleep (
    date            TEXT PRIMARY KEY,   -- calendar date the sleep is credited to
    total_seconds   INTEGER,
    deep_seconds    INTEGER,
    light_seconds   INTEGER,
    rem_seconds     INTEGER,
    awake_seconds   INTEGER,
    score           INTEGER,            -- overall sleep score 0-100
    resp_avg        REAL,
    sleep_stress    REAL,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id     INTEGER PRIMARY KEY,
    start_local     TEXT,               -- 'YYYY-MM-DD HH:MM:SS'
    date            TEXT,               -- YYYY-MM-DD (derived)
    type            TEXT,
    name            TEXT,
    distance_m      REAL,
    duration_s      REAL,
    moving_s        REAL,
    avg_hr          REAL,
    max_hr          REAL,
    avg_pace_s_per_km REAL,
    calories        REAL,
    avg_cadence     REAL,
    elevation_gain  REAL,
    vo2max          REAL,
    hr_z1_s         REAL,
    hr_z2_s         REAL,
    hr_z3_s         REAL,
    hr_z4_s         REAL,
    hr_z5_s         REAL,
    notes           TEXT,               -- user's Garmin activity note/description
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);

CREATE TABLE IF NOT EXISTS reschedules (
    orig_date       TEXT PRIMARY KEY,   -- the planned workout date
    new_date        TEXT,               -- the date it was actually moved to
    note            TEXT
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    # lightweight migrations for columns added after first release
    cols = {r[1] for r in conn.execute("PRAGMA table_info(activities)")}
    if "notes" not in cols:
        conn.execute("ALTER TABLE activities ADD COLUMN notes TEXT")
    # start GPS, used to look up per-run weather conditions
    if "start_lat" not in cols:
        conn.execute("ALTER TABLE activities ADD COLUMN start_lat REAL")
    if "start_lon" not in cols:
        conn.execute("ALTER TABLE activities ADD COLUMN start_lon REAL")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
