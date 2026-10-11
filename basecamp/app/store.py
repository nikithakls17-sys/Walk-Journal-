"""Data access helpers shared by the API routes."""
from . import db

# "unconfirmed": Gemma matched a gallery photo, but it had no EXIF time/GPS to confirm when and where.
STATUS_RANK = {"pass": 7, "self": 6, "unconfirmed": 5, "unsure": 4, "pending": 3, "retake": 2, "fail": 1}


def save_place(p: dict) -> dict:
    with db.tx() as c:
        existing = c.execute("SELECT id FROM places WHERE id=?", (p["id"],)).fetchone()
        if existing:
            c.execute("""UPDATE places SET name=?, type=?, description=?, features=?, address=?, rating=?, lat=?, lng=?,
                         radius_m=COALESCE(?, radius_m), raw=? WHERE id=?""",
                      (p["name"], p.get("type"), p.get("description"), db.dumps(p.get("features") or []),
                       p.get("address"), p.get("rating"), p["lat"], p["lng"], p.get("radius_m"),
                       db.dumps(p.get("raw") or {}), p["id"]))
        else:
            c.execute("""INSERT INTO places (id, name, type, description, features, address, rating, lat, lng, radius_m, raw, created_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (p["id"], p["name"], p.get("type"), p.get("description"), db.dumps(p.get("features") or []),
                       p.get("address"), p.get("rating"), p["lat"], p["lng"], p.get("radius_m"),
                       db.dumps(p.get("raw") or {}), db.now()))
    return get_place(p["id"])


def get_place(pid: str) -> dict | None:
    with db.tx() as c:
        return db.row(c.execute("SELECT * FROM places WHERE id=?", (pid,)).fetchone())


def get_quest(qid: str) -> dict | None:
    with db.tx() as c:
        return db.row(c.execute("SELECT * FROM quests WHERE id=?", (qid,)).fetchone())


def update_quest(qid: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = [db.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
    with db.tx() as c:
        c.execute(f"UPDATE quests SET {cols} WHERE id=?", (*vals, qid))


def captures_for(qid: str) -> list[dict]:
    with db.tx() as c:
        return db.rows(c.execute("SELECT * FROM captures WHERE quest_id=? ORDER BY created_at", (qid,)).fetchall())


def get_capture(cid: str) -> dict | None:
    with db.tx() as c:
        return db.row(c.execute("SELECT * FROM captures WHERE id=? OR client_id=?", (cid, cid)).fetchone())


def update_capture(cid: str, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = [db.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
    with db.tx() as c:
        c.execute(f"UPDATE captures SET {cols} WHERE id=?", (*vals, cid))


def best_capture(caps: list[dict]) -> dict | None:
    return max(caps, key=lambda c: (STATUS_RANK.get(c["status"], 0), c["created_at"]), default=None)


def task_states(quest: dict) -> list[dict]:
    caps = captures_for(quest["id"])
    out = []
    for t in quest["tasks"]:
        mine = [c for c in caps if c["task_id"] == t["id"]]
        best = best_capture(mine)
        status = "skipped" if t.get("skipped") else (best["status"] if best else "todo")
        out.append({**t, "status": status, "capture": public_capture(best) if best else None, "attempts": len(mine)})
    return out


def public_capture(c: dict) -> dict:
    keep = ("id", "client_id", "task_id", "kind", "taken_at", "status", "place_ok", "time_ok", "quality", "gemma",
            "reason", "verified_at", "proof")
    d = {k: c.get(k) for k in keep}
    d["photo_url"] = f"/photos/{c['photo']}" if c.get("photo") else None
    if isinstance(d.get("gemma"), dict):
        d["gemma"] = {k: v for k, v in d["gemma"].items() if not k.startswith("_")}
    return d


def recent_task_types(limit_quests: int = 3) -> list[str]:
    with db.tx() as c:
        qs = db.rows(c.execute("SELECT tasks FROM quests WHERE started_at IS NOT NULL ORDER BY created_at DESC LIMIT ?",
                               (limit_quests,)).fetchall())
    labels = []
    for q in qs:
        for t in q["tasks"] or []:
            if t.get("kind") == "photo" and t.get("type_label") and t["type_label"] not in labels:
                labels.append(t["type_label"])
    return labels
