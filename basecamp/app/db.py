import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS places (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT,
  description TEXT,
  features TEXT DEFAULT '[]',
  address TEXT,
  rating REAL,
  lat REAL NOT NULL,
  lng REAL NOT NULL,
  radius_m REAL,
  raw TEXT,
  created_at REAL
);

CREATE TABLE IF NOT EXISTS search_cache (
  key TEXT PRIMARY KEY,
  created_at REAL,
  results TEXT
);

CREATE TABLE IF NOT EXISTS quests (
  id TEXT PRIMARY KEY,
  place_id TEXT REFERENCES places(id),
  created_at REAL,
  started_at REAL,
  deadline_at REAL,
  finished_at REAL,
  tz TEXT,
  distance_tier TEXT,
  difficulty TEXT,
  title TEXT,
  spoken_intro TEXT,
  deadline_minutes INTEGER,
  tasks TEXT,
  context TEXT,
  source TEXT,
  status TEXT DEFAULT 'new',
  skips_left INTEGER DEFAULT 0,
  screen_seconds REAL,
  note TEXT,
  mood TEXT,
  points INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS captures (
  id TEXT PRIMARY KEY,
  client_id TEXT UNIQUE,
  quest_id TEXT REFERENCES quests(id),
  task_id TEXT,
  kind TEXT,
  photo TEXT,
  taken_at REAL,
  lat REAL,
  lng REAL,
  accuracy REAL,
  source TEXT,
  status TEXT DEFAULT 'pending',
  place_ok INTEGER,
  time_ok INTEGER,
  quality TEXT,
  gemma TEXT,
  reason TEXT,
  created_at REAL,
  verified_at REAL
);

CREATE TABLE IF NOT EXISTS badges (
  code TEXT PRIMARY KEY,
  earned_at REAL,
  quest_id TEXT
);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release; init() adds any that an older database is missing.
MIGRATIONS = {
    "quests": [("mode", "TEXT DEFAULT 'place'"), ("start_lat", "REAL"), ("start_lng", "REAL"),
               ("away_goal_m", "REAL DEFAULT 0"), ("requirement_met", "INTEGER")],
    "captures": [("distance_m", "REAL")],
}


def init():
    with tx() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


JSON_COLS = {"features", "raw", "tasks", "context", "quality", "gemma", "results"}


def row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        if isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def rows(rs) -> list[dict]:
    return [row(r) for r in rs]


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def get_setting(key: str, default=None):
    with tx() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value):
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, dumps(value)))


def now() -> float:
    return time.time()
