"""Cloud mode storage in MongoDB Atlas (the free M0 cluster, database "walk_journal").

Render's free plan has no persistent disk: the filesystem is wiped on every restart, deploy and spin-down. So in
cloud mode Atlas holds everything, and SQLite is only a working copy that the existing queries run against:

- at start, the working copy is rebuilt from Atlas (one collection per table, one document per row);
- triggers note the primary key of every row a transaction inserts, updates or deletes;
- after each commit, those rows are copied to Atlas (or removed from it).

If Atlas can't be reached after a commit, the changes stay noted and go with the next commit (or at shutdown).
Photos are stored separately, in the "photos" collection (see photos.py). Local mode never imports pymongo or
touches Atlas: the laptop keeps data/walkjournal.db exactly as before.
"""
import logging
import sqlite3
import threading

from . import config

log = logging.getLogger("walkjournal.atlas")
_db = None
_lock = threading.Lock()
CHANGES = "_atlas_changes"
MAX_KEY_COLS = 3  # usage has the widest primary key: (day, key, action)


def database():
    global _db
    if _db is None:
        from pymongo import MongoClient
        _db = MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=15000, appname="walk-journal")[config.MONGODB_DB]
    return _db


def use(db):
    """Tests: swap in a stand-in database."""
    global _db
    _db = db


def tables(c: sqlite3.Connection) -> list[str]:
    return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                    "AND name != ?", (CHANGES,)).fetchall()]


def key_cols(c: sqlite3.Connection, table: str) -> list[str]:
    info = c.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]


def doc_id(keys) -> str:
    return str(keys[0]) if len(keys) == 1 else "|".join(map(str, keys))


def restore(c: sqlite3.Connection) -> int:
    """Load every row from Atlas into an empty working copy. Returns the number of rows."""
    n = 0
    for table in tables(c):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        for doc in database()[table].find({}, {"_id": 0}):
            row = {k: v for k, v in doc.items() if k in cols}  # columns added later keep their DEFAULT
            if row:
                c.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                          list(row.values()))
                n += 1
    return n


def install_triggers(c: sqlite3.Connection):
    c.execute(f"CREATE TABLE IF NOT EXISTS {CHANGES} (seq INTEGER PRIMARY KEY AUTOINCREMENT, tbl TEXT, "
              f"k1, k2, k3)")
    for table in tables(c):
        keys = key_cols(c, table)
        assert 0 < len(keys) <= MAX_KEY_COLS, f"{table} needs a primary key for Atlas"
        cols = ", ".join(f"k{i + 1}" for i in range(len(keys)))

        def note(ref):
            vals = ", ".join(f"{ref}.{k}" for k in keys)
            return f"INSERT INTO {CHANGES} (tbl, {cols}) VALUES ('{table}', {vals});"

        c.execute(f"CREATE TRIGGER IF NOT EXISTS atlas_{table}_i AFTER INSERT ON {table} BEGIN {note('NEW')} END")
        c.execute(f"CREATE TRIGGER IF NOT EXISTS atlas_{table}_u AFTER UPDATE ON {table} "
                  f"BEGIN {note('OLD')} {note('NEW')} END")
        c.execute(f"CREATE TRIGGER IF NOT EXISTS atlas_{table}_d AFTER DELETE ON {table} BEGIN {note('OLD')} END")


def flush(c: sqlite3.Connection) -> bool:
    """Copy the rows changed since the last flush to Atlas. True if nothing is left waiting."""
    from pymongo import DeleteOne, ReplaceOne
    from pymongo.errors import PyMongoError

    with _lock:
        pending = c.execute(f"SELECT * FROM {CHANGES} ORDER BY seq").fetchall()
        if not pending:
            return True
        changed = dict.fromkeys((r["tbl"], tuple(r[f"k{i}"] for i in range(1, MAX_KEY_COLS + 1))) for r in pending)
        ops: dict[str, list] = {}
        for table, raw in changed:
            keys = key_cols(c, table)
            vals = raw[:len(keys)]
            where = " AND ".join(f"{k} IS ?" for k in keys)
            row = c.execute(f"SELECT * FROM {table} WHERE {where}", vals).fetchone()
            _id = doc_id(vals)
            ops.setdefault(table, []).append(
                ReplaceOne({"_id": _id}, {"_id": _id, **dict(row)}, upsert=True) if row else DeleteOne({"_id": _id}))
        try:
            for table, batch in ops.items():
                database()[table].bulk_write(batch, ordered=True)
        except PyMongoError as e:
            log.warning("Atlas write failed, will retry with the next change: %s", e)
            return False
        c.execute(f"DELETE FROM {CHANGES} WHERE seq <= ?", (pending[-1]["seq"],))
        c.commit()
        return True
