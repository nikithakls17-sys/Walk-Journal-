"""Data access helpers shared by the API routes."""
import math

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


def capture_label(c: dict) -> str | None:
    """The small honest label shown under a task."""
    status, proof = c.get("status"), c.get("proof")
    if status == "pass":
        return "verified (gallery)" if proof in ("exif", "gallery_live") else "verified"
    if status == "unconfirmed":
        return "place not confirmed" if proof in ("no_location", "time_only") else "time/place not confirmed"
    return None


def public_capture(c: dict) -> dict:
    keep = ("id", "client_id", "task_id", "kind", "taken_at", "status", "place_ok", "time_ok", "quality", "gemma",
            "reason", "verified_at", "proof")
    d = {k: c.get(k) for k in keep}
    d["label"] = capture_label(c)
    d["photo_url"] = f"/photos/{c['photo']}" if c.get("photo") else None
    if isinstance(d.get("gemma"), dict):
        d["gemma"] = {k: v for k, v in d["gemma"].items() if not k.startswith("_")}
    return d


# ---------- completion: a walk passes at 75% of its tasks (rounded up) ----------

DONE_STATUSES = {"pass", "self", "unconfirmed"}
PASS_SHARE = 0.75


def away_progress(q: dict) -> tuple[float, bool]:
    """(farthest verified photo from the start in meters, whether the Walk-from-here away goal is met)."""
    caps = [c for c in captures_for(q["id"])
            if c["kind"] == "photo" and c["status"] in ("pass", "self") and c.get("distance_m") is not None]
    farthest = max((c["distance_m"] for c in caps), default=0.0)
    return farthest, farthest >= (q.get("away_goal_m") or 0)


def needed_for(total: int) -> int:
    """Tasks needed to complete a walk: 75%, rounded up (3 of 4, 4 of 5, 6 of 7)."""
    return math.ceil(PASS_SHARE * total) if total else 0


def progress(q: dict, tasks: list[dict] | None = None) -> dict:
    """How far a walk got. Skipped tasks (Hard's rare-find skip) leave the count. Derived from the photos,
    so older walks are judged by the same rule without rewriting their rows."""
    tasks = tasks if tasks is not None else task_states(q)
    counted = [t for t in tasks if t.get("status") != "skipped"]
    done = sum(1 for t in counted if t.get("status") in DONE_STATUSES)
    needed = needed_for(len(counted))
    farthest, away_met = away_progress(q)
    return {"done": done, "total": len(counted), "needed": needed, "away_met": away_met,
            "completed": bool(counted) and done >= needed and away_met}


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
