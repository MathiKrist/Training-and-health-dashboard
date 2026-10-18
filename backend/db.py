import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id            INTEGER PRIMARY KEY,
    start_local   TEXT,
    date          TEXT,
    type          TEXT,
    name          TEXT,
    distance_m    REAL,
    duration_s    REAL,
    avg_hr        REAL,
    max_hr        REAL,
    avg_speed     REAL,
    elev_gain     REAL,
    training_load REAL,
    aerobic_te    REAL,
    anaerobic_te  REAL,
    te_label      TEXT,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);

CREATE TABLE IF NOT EXISTS daily (
    date           TEXT PRIMARY KEY,
    resting_hr     REAL,
    hrv_last_night REAL,
    hrv_weekly_avg REAL,
    hrv_low        REAL,
    hrv_high       REAL,
    hrv_status     TEXT,
    sleep_s        REAL,
    sleep_score    REAL,
    bb_high        REAL,
    bb_low         REAL,
    stress_avg     REAL,
    steps          REAL,
    cal_total      REAL,  -- calories burnt in the day: total, active and resting (BMR)
    cal_active     REAL,
    cal_resting    REAL,
    readiness      REAL,
    recovery_h     REAL,  -- Garmin's recovery time left on waking, in hours
    training_status       TEXT,
    training_status_since TEXT,
    acute_load     REAL,
    acute_load_min REAL,
    acute_load_max REAL,
    acwr_status    TEXT,
    vo2max         REAL,
    load_low           REAL,
    load_low_min       REAL,
    load_low_max       REAL,
    load_high          REAL,
    load_high_min      REAL,
    load_high_max      REAL,
    load_anaerobic     REAL,
    load_anaerobic_min REAL,
    load_anaerobic_max REAL,
    load_focus         TEXT,
    pred_5k        REAL,  -- Garmin's race predictor: predicted seconds for each distance
    pred_10k       REAL,
    pred_half      REAL,
    pred_marathon  REAL
);

-- GPS track per activity as JSON [[lat, lon], ...]; "[]" when the activity has none
CREATE TABLE IF NOT EXISTS tracks (activity_id INTEGER PRIMARY KEY, points TEXT);

-- Laps per activity as JSON [{km, duration_s, speed, ...}, ...]; fetched when the activity is first opened
CREATE TABLE IF NOT EXISTS laps (activity_id INTEGER PRIMARY KEY, laps TEXT);

-- A run's fastest time over set distances as JSON {"400": seconds, "1000": seconds, ...}, worked out from its
-- second-by-second data; "{}" when that couldn't be read
CREATE TABLE IF NOT EXISTS efforts (activity_id INTEGER PRIMARY KEY, bests TEXT);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

ACTIVITY_COLS = [
    "id", "start_local", "date", "type", "name", "distance_m", "duration_s",
    "avg_hr", "max_hr", "avg_speed", "elev_gain", "training_load",
    "aerobic_te", "anaerobic_te", "te_label", "raw",
]
DAILY_COLS = [
    "date", "resting_hr", "hrv_last_night", "hrv_weekly_avg", "hrv_low",
    "hrv_high", "hrv_status", "sleep_s", "sleep_score", "bb_high", "bb_low",
    "stress_avg", "steps", "cal_total", "cal_active", "cal_resting", "readiness", "recovery_h", "training_status", "training_status_since",
    "acute_load", "acute_load_min", "acute_load_max", "acwr_status", "vo2max",
    "load_low", "load_low_min", "load_low_max", "load_high", "load_high_min", "load_high_max", "load_anaerobic", "load_anaerobic_min", "load_anaerobic_max", "load_focus",
    "pred_5k", "pred_10k", "pred_half", "pred_marathon",
]


def connect(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # The sync and the page both write (the popup stores routes and laps); wait for each other rather than fail,
    # and let the page read while the sync writes
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Columns added since the table was first made
    have = {r["name"] for r in conn.execute("PRAGMA table_info(daily)")}
    for col in DAILY_COLS:
        if col not in have:
            conn.execute(f"ALTER TABLE daily ADD COLUMN {col} REAL")
    return conn


def upsert_activity(conn, row):
    row = dict(row)
    if isinstance(row.get("raw"), (dict, list)):
        row["raw"] = json.dumps(row["raw"])
    values = [row.get(c) for c in ACTIVITY_COLS]
    conn.execute(
        f"INSERT OR REPLACE INTO activities ({','.join(ACTIVITY_COLS)}) "
        f"VALUES ({','.join('?' * len(ACTIVITY_COLS))})",
        values,
    )


def delete_activities(conn, ids):
    """Remove activities, with their tracks, laps and best efforts."""
    for table, col in (("activities", "id"), ("tracks", "activity_id"), ("laps", "activity_id"), ("efforts", "activity_id")):
        conn.executemany(f"DELETE FROM {table} WHERE {col} = ?", [(i,) for i in ids])


def upsert_daily(conn, row):
    """Merge non-empty fields into the day's row, keeping what's already there."""
    existing = conn.execute("SELECT * FROM daily WHERE date = ?", (row["date"],)).fetchone()
    merged = dict(existing) if existing else {}
    merged.update({k: v for k, v in row.items() if v is not None})
    values = [merged.get(c) for c in DAILY_COLS]
    conn.execute(
        f"INSERT OR REPLACE INTO daily ({','.join(DAILY_COLS)}) "
        f"VALUES ({','.join('?' * len(DAILY_COLS))})",
        values,
    )


def set_track(conn, activity_id, points):
    conn.execute("INSERT OR REPLACE INTO tracks (activity_id, points) VALUES (?, ?)", (activity_id, json.dumps(points)))


def get_track(conn, activity_id):
    row = conn.execute("SELECT points FROM tracks WHERE activity_id = ?", (activity_id,)).fetchone()
    return json.loads(row["points"]) if row else None


def set_laps(conn, activity_id, laps):
    conn.execute("INSERT OR REPLACE INTO laps (activity_id, laps) VALUES (?, ?)", (activity_id, json.dumps(laps)))


def get_laps(conn, activity_id):
    row = conn.execute("SELECT laps FROM laps WHERE activity_id = ?", (activity_id,)).fetchone()
    return json.loads(row["laps"]) if row else None


def set_efforts(conn, activity_id, bests):
    conn.execute("INSERT OR REPLACE INTO efforts (activity_id, bests) VALUES (?, ?)", (activity_id, json.dumps(bests)))


def get_efforts(conn, activity_id):
    """One run's best efforts: {metres: seconds}, or None if not worked out yet."""
    row = conn.execute("SELECT bests FROM efforts WHERE activity_id = ?", (activity_id,)).fetchone()
    return {int(m): s for m, s in json.loads(row["bests"]).items()} if row else None


def all_efforts(conn):
    """Every run's best efforts: {activity_id: {metres: seconds}}."""
    return {r["activity_id"]: {int(m): s for m, s in json.loads(r["bests"]).items()}
            for r in conn.execute("SELECT activity_id, bests FROM efforts")}


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
