"""Base Camp: the FastAPI server that runs on the laptop next to Gemma."""
import asyncio
import io
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from . import (badges, config, context, db, gallery_client, gemma, places, push, quests, reminders, store, tracing,
               tts, verify)
from .geo import M_PER_MI

tracing.init()
db.init()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(reminders.loop())  # daily + journal reminders, checked once a minute
    yield
    task.cancel()


app = FastAPI(title="Walk Journal Base Camp", lifespan=lifespan)
STATIC = Path(__file__).resolve().parents[1] / "static"


# ---------- settings ----------

class Settings(BaseModel):
    home_lat: float | None = None
    home_lng: float | None = None
    home_city: str | None = None
    units: str = "mi"
    auto_read: bool = False  # read the quest aloud as soon as it starts (off by default)
    tz: str | None = None  # the phone's timezone, so reminders fire at the user's local time
    # Notifications: all off until the user turns them on.
    notify_daily: bool = False
    daily_time: str = "17:00"
    notify_journal: bool = False
    journal_time: str = "20:00"
    notify_quest: bool = False


@app.get("/api/health")
async def health():
    return {"ok": True, "gemma": await gemma.available(), "model": config.GEMMA_MODEL,
            "serpapi": bool(config.SERPAPI_KEY), "elevenlabs": bool(config.ELEVENLABS_API_KEY),
            "gallery": bool(config.GALLERY_URL), "push": push.enabled()}


@app.get("/api/settings")
def get_settings():
    return Settings(**(db.get_setting("settings") or {})).model_dump()


@app.post("/api/settings")
def save_settings(s: Settings):
    for t in (s.daily_time, s.journal_time):
        if not reminders.valid_hhmm(t):
            raise HTTPException(400, "times must look like 17:00")
    db.set_setting("settings", s.model_dump())
    return s


# ---------- push notifications ----------

@app.get("/api/push/key")
def push_key():
    if not push.enabled():
        raise HTTPException(503, "Notifications aren't set up on Base Camp (no VAPID keys in .env).")
    return {"key": config.VAPID_PUBLIC_KEY}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    try:
        push.save_subscription(await request.json(), request.headers.get("user-agent", ""))
    except (push.PushError, ValueError, AttributeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    push.remove_subscription((await request.json()).get("endpoint", ""))
    return {"ok": True}


async def push_quest_progress(qid: str):
    """Update the single live quest notification (same tag, replaces itself) after a task changes."""
    s = db.get_setting("settings") or {}
    q = store.get_quest(qid)
    if not (s.get("notify_quest") and push.enabled() and q and q["status"] == "active"):
        return
    await push.send_all(push.quest_payload(quest_view(qid)))


# ---------- places ----------

class Here(BaseModel):
    lat: float
    lng: float
    name: str | None = None


@app.post("/api/places/here")
def place_here(h: Here):
    """A place at the user's current spot (for testing without SerpApi)."""
    pid = f"here-{round(h.lat, 4)}-{round(h.lng, 4)}"
    return store.save_place({"id": pid, "name": h.name or "Right Here", "type": "neighborhood walk",
                             "description": "", "features": [], "lat": h.lat, "lng": h.lng})


@app.get("/api/places/suggest")
async def suggest(lat: float, lng: float, tier: str = "nearby", difficulty: str = "easy", tz: str | None = None):
    try:
        cands = await places.nearby(lat, lng, tier)
    except places.PlacesError as e:
        raise HTTPException(503, f"Couldn't search for places: {e}")
    if not cands:
        return {"picks": [], "others": [], "source": "none"}
    anchor = {"name": "you", "lat": lat, "lng": lng}
    ctx = context.build(anchor, tier, difficulty, tz, [])
    facts = {k: ctx[k] for k in ("season", "local_time", "time_of_day", "sunset", "minutes_until_sunset", "weather",
                                 "difficulty", "distance_tier")}
    picks, source = await places.rank(cands, facts)
    pick_ids = {p["id"] for p in picks}
    slim = lambda p: {k: p.get(k) for k in ("id", "name", "type", "lat", "lng", "distance_mi", "rating", "address", "reason")}
    return {"picks": [slim(p) for p in picks], "others": [slim(p) for p in cands if p["id"] not in pick_ids],
            "source": source}


# ---------- map ----------

@app.get("/api/map")
def map_data():
    with db.tx() as c:
        done = db.rows(c.execute("""SELECT q.id, q.place_id, q.title, q.started_at, q.finished_at FROM quests q
                                    WHERE q.status='done' ORDER BY q.finished_at DESC""").fetchall())
        all_places = {p["id"]: p for p in db.rows(c.execute("SELECT id, name, type, lat, lng FROM places").fetchall())}
        thumbs = db.rows(c.execute("""SELECT quest_id, photo FROM captures WHERE status IN ('pass','self','unconfirmed')
                                      AND kind='photo' ORDER BY created_at""").fetchall())
    photos_by_quest: dict[str, list[str]] = {}
    for t in thumbs:
        photos_by_quest.setdefault(t["quest_id"], []).append(f"/photos/{t['photo']}")
    visited: dict[str, dict] = {}
    outside = 0.0
    for q in done:
        p = all_places.get(q["place_id"])
        if not p:
            continue
        v = visited.setdefault(p["id"], {**p, "visits": []})
        v["visits"].append({"quest_id": q["id"], "title": q["title"], "date": q["finished_at"],
                            "photos": photos_by_quest.get(q["id"], [])[:4]})
        if q["started_at"] and q["finished_at"]:
            outside += max(0, q["finished_at"] - q["started_at"])
    suggested = [p for pid, p in all_places.items()
                 if pid not in visited and not pid.startswith("here-") and pid != HOME_WALKS_ID]
    return {"visited": list(visited.values()), "suggested": suggested,
            "stats": {"walks": len(done), "places": len(visited), "outside_seconds": round(outside)}}


# ---------- quests ----------

HOME_WALKS_ID = "home-walks"
AWAY_GOAL_M = {"easy": 0, "medium": 300, "hard": 300}  # Walk from here: one photo this far from the start


def from_here_radius_m(tier: str) -> float:
    """Walk from here: every photo must be within the chosen distance of the start (Nearby = 1 mile)."""
    return places.TIERS.get(tier, places.TIERS["nearby"])[1] * M_PER_MI


class NewQuest(BaseModel):
    mode: str = "place"  # "place" (go to a destination) or "from_here" (a walk around home)
    place_id: str | None = None
    lat: float | None = None  # start point, for mode=from_here
    lng: float | None = None
    distance_tier: str = "nearby"
    difficulty: str = "easy"
    tz: str | None = None


def home_walks_place(lat: float, lng: float) -> dict:
    """All Walk from here quests share one map pin, at home if it's set, otherwise at the first start."""
    existing = store.get_place(HOME_WALKS_ID)
    s = db.get_setting("settings", {}) or {}
    if s.get("home_lat") is not None:
        lat, lng = s["home_lat"], s["home_lng"]
    elif existing:
        lat, lng = existing["lat"], existing["lng"]
    return store.save_place({"id": HOME_WALKS_ID, "name": "Home walks", "type": "walk from home",
                             "description": "", "features": [], "lat": lat, "lng": lng})


@app.post("/api/quests")
async def create_quest(req: NewQuest):
    if req.difficulty not in quests.TASK_COUNTS:
        raise HTTPException(400, "difficulty must be easy, medium or hard")
    if req.distance_tier not in places.TIERS:
        raise HTTPException(400, "distance_tier must be nearby, walk_run or adventure")
    if req.mode == "from_here":
        if req.lat is None or req.lng is None:
            raise HTTPException(400, "Walk from here needs your current location")
        place = home_walks_place(req.lat, req.lng)
        gen_place = quests.home_place(req.lat, req.lng)  # what Gemma sees: no destination, no features
        start, away_goal = (req.lat, req.lng), AWAY_GOAL_M[req.difficulty]
    elif req.mode == "place":
        place = store.get_place(req.place_id or "")
        if not place:
            raise HTTPException(404, "unknown place")
        gen_place, start, away_goal = place, (None, None), 0
    else:
        raise HTTPException(400, "mode must be place or from_here")
    ctx = context.build(gen_place, req.distance_tier, req.difficulty, req.tz, store.recent_task_types())
    ctx["mode"] = req.mode
    quest, source, notes = await quests.generate(ctx, gen_place)
    qid = db.new_id()
    with db.tx() as c:
        c.execute("""INSERT INTO quests (id, place_id, created_at, tz, distance_tier, difficulty, title, spoken_intro,
                     deadline_minutes, tasks, context, source, skips_left, mode, start_lat, start_lng, away_goal_m)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (qid, place["id"], db.now(), req.tz, req.distance_tier, req.difficulty, quest["title"],
                   quest["spoken_intro"], quest["deadline_minutes"], db.dumps(quest["tasks"]),
                   db.dumps(ctx | {"repairs": notes}), source, 1 if req.difficulty == "hard" else 0,
                   req.mode, start[0], start[1], away_goal))
    return quest_view(qid)


def away_progress(q: dict) -> tuple[float, bool]:
    """(farthest verified photo from the start in meters, whether the away goal is met)."""
    caps = [c for c in store.captures_for(q["id"])
            if c["kind"] == "photo" and c["status"] in ("pass", "self") and c.get("distance_m") is not None]
    farthest = max((c["distance_m"] for c in caps), default=0.0)
    goal = q.get("away_goal_m") or 0
    return farthest, farthest >= goal


def quest_view(qid: str) -> dict:
    q = store.get_quest(qid)
    if not q:
        raise HTTPException(404, "unknown quest")
    place = store.get_place(q["place_id"])
    tasks = store.task_states(q)
    ctx = q["context"] or {}
    farthest, away_met = away_progress(q)
    return {
        "id": q["id"], "title": q["title"], "spoken_intro": q["spoken_intro"], "status": q["status"],
        "difficulty": q["difficulty"], "distance_tier": q["distance_tier"], "source": q["source"],
        "deadline_minutes": q["deadline_minutes"], "created_at": q["created_at"], "started_at": q["started_at"],
        "deadline_at": q["deadline_at"], "finished_at": q["finished_at"], "skips_left": q["skips_left"],
        "season": ctx.get("season"), "sunset": ctx.get("sunset"), "weather": ctx.get("weather"),
        "night_quest": ctx.get("night_quest"), "repairs": ctx.get("repairs", []),
        "place": {k: place[k] for k in ("id", "name", "type", "lat", "lng", "radius_m", "address")} if place else None,
        "tasks": tasks, "screen_seconds": q["screen_seconds"], "note": q["note"], "mood": q["mood"],
        "points": q["points"],
        "mode": q.get("mode") or "place", "start": {"lat": q["start_lat"], "lng": q["start_lng"]} if q.get("start_lat") is not None else None,
        "radius_m": from_here_radius_m(q["distance_tier"]) if q.get("mode") == "from_here" else None,
        "away_goal_m": q.get("away_goal_m") or 0, "farthest_m": round(farthest), "away_met": away_met,
        "requirement_met": q.get("requirement_met"), "swaps_left": swaps_left(q),
    }


@app.get("/api/quests/{qid}")
def get_quest(qid: str):
    return quest_view(qid)


class Start(BaseModel):
    started_at: float | None = None  # epoch seconds from the phone (may be queued offline)


@app.post("/api/quests/{qid}/start")
def start_quest(qid: str, s: Start):
    q = store.get_quest(qid) or _404()
    if q["started_at"]:
        return quest_view(qid)
    now = time.time()
    started = s.started_at if s.started_at and q["created_at"] - 60 <= s.started_at <= now + 60 else now
    store.update_quest(qid, started_at=started, deadline_at=started + q["deadline_minutes"] * 60, status="active")
    return quest_view(qid)


class Skip(BaseModel):
    task_id: str


SWAPPABLE = {"todo", "fail", "retake", "unsure"}  # unfinished, and not waiting on a check


def swaps_left(q: dict) -> int:
    return max(0, quests.SWAP_LIMITS[q["difficulty"]] - (q.get("swaps_used") or 0))


@app.post("/api/quests/{qid}/swap")
async def swap_task(qid: str, s: Skip, background: BackgroundTasks):
    """Replace one unfinished photo task with a new one from Gemma. No point penalty."""
    q = store.get_quest(qid) or _404()
    if q["status"] == "done":
        raise HTTPException(400, "This quest is finished.")
    if swaps_left(q) < 1:
        raise HTTPException(400, "No swaps left on this quest.")
    states = {t["id"]: t for t in store.task_states(q)}
    old = states.get(s.task_id)
    if not old:
        raise HTTPException(404, "unknown task")
    if old["kind"] != "photo":
        raise HTTPException(400, "Only photo tasks can be swapped.")
    if old["status"] not in SWAPPABLE:
        raise HTTPException(400, "Only unfinished tasks can be swapped.")
    ctx = dict(q["context"] or {})
    ctx.setdefault("mode", q.get("mode") or "place")
    if ctx["mode"] == "from_here":
        place = quests.home_place(q["start_lat"], q["start_lng"])
    else:
        place = store.get_place(q["place_id"])
    others = [t for t in q["tasks"] if t["id"] != s.task_id]
    new, source, notes = await quests.swap_task(ctx, place, old, others)
    # A fresh id, so photos taken for the old task never count for the new one.
    new["id"] = f"t{len(q['tasks']) + (q.get('swaps_used') or 0) + 1}"
    new["swapped_from"] = old["prompt"]
    tasks = [new if t["id"] == s.task_id else t for t in q["tasks"]]
    store.update_quest(qid, tasks=tasks, swaps_used=(q.get("swaps_used") or 0) + 1)
    background.add_task(push_quest_progress, qid)
    return quest_view(qid) | {"swap": {"replaced": old["prompt"], "with": new["prompt"], "source": source,
                                       "notes": notes}}


@app.post("/api/quests/{qid}/skip")
def skip_task(qid: str, s: Skip):
    q = store.get_quest(qid) or _404()
    if q["skips_left"] < 1:
        raise HTTPException(400, "no skips left")
    tasks = q["tasks"]
    for t in tasks:
        if t["id"] == s.task_id:
            t["skipped"] = True
            break
    else:
        raise HTTPException(404, "unknown task")
    store.update_quest(qid, tasks=tasks, skips_left=q["skips_left"] - 1)
    return quest_view(qid)


# ---------- captures ----------

USER_MESSAGES = {
    "too_dark": "That one's too dark for me to see. Try a retake in better light.",
    "too_bright": "That one's washed out. Try a retake facing away from the sun.",
    "blurry": "That one's a little blurry. Hold still and try again.",
}


@app.post("/api/quests/{qid}/captures")
async def add_capture(qid: str, task_id: str = Form(...), client_id: str = Form(...), taken_at: float = Form(...),
                      lat: float | None = Form(None), lng: float | None = Form(None),
                      accuracy: float | None = Form(None), source: str = Form("camera"),
                      file_modified: float | None = Form(None), photo: UploadFile = File(...),
                      background: BackgroundTasks = None):
    existing = store.get_capture(client_id)
    if existing:
        return store.public_capture(existing)
    q = store.get_quest(qid) or _404()
    task = next((t for t in q["tasks"] if t["id"] == task_id), None)
    if not task:
        raise HTTPException(404, "unknown task")
    raw = await photo.read()
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(413, "photo too large")
    taken = taken_at / 1000 if taken_at > 1e11 else taken_at  # accept ms or s
    proof = "live"
    if source == "gallery" and task["kind"] == "photo":
        # Time and place come from the photo itself, never from where the phone is now.
        meta = verify.read_exif(raw, context.tzinfo(q["tz"]))
        if meta["taken_at"] is not None and meta["lat"] is not None:
            proof, taken, lat, lng, accuracy = "exif", meta["taken_at"], meta["lat"], meta["lng"], meta["accuracy"]
        else:
            proof, lat, lng, accuracy = "none", None, None, None
    try:
        jpeg, img = verify.clean_image(raw)  # EXIF (incl. GPS) is gone from here on
    except Exception:
        raise HTTPException(400, "not an image")
    cid = db.new_id()
    fname = f"{cid}.jpg"
    (config.PHOTO_DIR / fname).write_bytes(jpeg)
    with db.tx() as c:
        c.execute("""INSERT INTO captures (id, client_id, quest_id, task_id, kind, photo, taken_at, lat, lng, accuracy,
                     source, status, created_at, proof) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (cid, client_id, qid, task_id, task["kind"], fname, taken, lat, lng, accuracy, source,
                   "pending", db.now(), proof))
    extra = {"file_modified": file_modified / 1000 if file_modified and file_modified > 1e11 else file_modified}
    await run_checks(cid, extra)
    if background is not None:
        background.add_task(push_quest_progress, qid)
    return store.public_capture(store.get_capture(cid))


async def run_checks(cid: str, extra: dict | None = None):
    c = store.get_capture(cid)
    q = store.get_quest(c["quest_id"])
    task = next(t for t in q["tasks"] if t["id"] == c["task_id"])
    jpeg = (config.PHOTO_DIR / c["photo"]).read_bytes()
    tz = context.tzinfo(q["tz"])

    if task["kind"] == "tracker":
        today = verify.local_date(c["taken_at"], tz)
        reading = await verify.read_tracker(jpeg, today)
        status, reason = verify.decide_tracker(reading, task.get("goal", {}), today)
        store.update_capture(cid, status=status, reason=reason, gemma=reading, place_ok=None, time_ok=None,
                             verified_at=None if status == "pending" else time.time())
        return

    qual = verify.quality(Image.open(io.BytesIO(jpeg)), task.get("category"))
    if not qual["ok"]:
        store.update_capture(cid, status="retake", quality=qual, reason=USER_MESSAGES[qual["problem"]],
                             verified_at=time.time())
        return

    if c.get("proof") == "none":
        g = await verify.check_content(jpeg, task)
        status = verify.decide_content(g)
        if status == "pass":
            status = "unconfirmed"
        note = "This gallery photo has no time or location info, so only what's in it was checked."
        reason = {
            "unconfirmed": f"{g.get('what_i_see', '')} {note}".strip(),
            "fail": f"I see {g.get('what_i_see', 'something else')}. {g.get('reason', '')}".strip(),
            "unsure": f"Couldn't tell. {g.get('what_i_see', '')}".strip(),
            "pending": "Saved. Gemma will check it when Base Camp is reachable.",
        }[status]
        store.update_capture(cid, status=status, place_ok=None, time_ok=None, quality=qual, gemma=g, reason=reason,
                             verified_at=None if status == "pending" else time.time())
        return

    if q.get("mode") == "from_here":
        tier_mi = places.TIERS.get(q["distance_tier"], places.TIERS["nearby"])[1]
        place = {"name": "your start point", "lat": q["start_lat"], "lng": q["start_lng"],
                 "radius_m": from_here_radius_m(q["distance_tier"])}
    else:
        place = store.get_place(q["place_id"])
    place_ok, dist = verify.check_place(c["lat"], c["lng"], c["accuracy"], place)
    time_ok = verify.check_time(c["taken_at"], q, c["created_at"])
    fm = (extra or {}).get("file_modified")
    if c["source"] == "input" and fm and fm < c["taken_at"] - verify.TIME_GRACE_S:
        time_ok = False
    reason = None
    if not place_ok:
        if dist is None:
            reason = "No GPS fix with this photo, so I can't confirm the place."
        elif q.get("mode") == "from_here":
            reason = (f"This was taken {dist / M_PER_MI:.1f} mi from where you started. "
                      f"This walk stays within {tier_mi:g} mi, so head back a little and try again.")
        else:
            reason = f"This was taken {dist} m from {place['name']}. Head a little closer and try again."
    elif not time_ok:
        reason = ("This gallery photo was taken outside the quest time window." if c.get("proof") == "exif"
                  else "This photo was taken outside the quest time window.")
    if reason:
        store.update_capture(cid, status="fail", place_ok=int(place_ok), time_ok=int(time_ok), quality=qual,
                             reason=reason, distance_m=dist, verified_at=time.time())
        return

    g = await verify.check_content(jpeg, task)
    status = verify.decide_content(g)
    reason = {
        "pass": g.get("what_i_see"),
        "fail": f"I see {g.get('what_i_see', 'something else')}. {g.get('reason', '')}".strip(),
        "unsure": f"Couldn't tell. {g.get('what_i_see', '')}".strip(),
        "pending": "Saved. Gemma will check it when Base Camp is reachable.",
    }[status]
    store.update_capture(cid, status=status, place_ok=1, time_ok=1, quality=qual, gemma=g, reason=reason,
                         distance_m=dist, verified_at=None if status == "pending" else time.time())


@app.post("/api/captures/{cid}/retry")
async def retry_capture(cid: str):
    c = store.get_capture(cid) or _404()
    if c["status"] == "pending":
        await run_checks(c["id"])
    return store.public_capture(store.get_capture(c["id"]))


@app.post("/api/captures/retry-pending")
async def retry_pending(background: BackgroundTasks):
    with db.tx() as conn:
        rows = conn.execute("SELECT id, quest_id FROM captures WHERE status='pending'").fetchall()
    for r in rows:
        await run_checks(r["id"])
    for qid in {r["quest_id"] for r in rows}:
        background.add_task(push_quest_progress, qid)
    return {"retried": len(rows)}


@app.post("/api/captures/{cid}/self-confirm")
def self_confirm(cid: str, background: BackgroundTasks):
    c = store.get_capture(cid) or _404()
    if c["status"] not in ("unsure", "pending"):
        raise HTTPException(400, "only photos Gemma couldn't judge can be self-confirmed")
    if c["kind"] == "photo" and not (c["place_ok"] and c["time_ok"]):
        raise HTTPException(400, "place and time checks must pass first")
    store.update_capture(c["id"], status="self", reason="You confirmed this one yourself.", verified_at=time.time())
    background.add_task(push_quest_progress, c["quest_id"])
    return store.public_capture(store.get_capture(c["id"]))


# ---------- finish ----------

class Finish(BaseModel):
    note: str | None = None
    mood: str | None = None
    screen_seconds: float | None = None
    finished_at: float | None = None


def points_for(tasks: list[dict]) -> int:
    pts = 0
    for t in tasks:
        if t["status"] == "pass":
            pts += 10 + (20 if t.get("rare") else 0)
        elif t["status"] in ("self", "unconfirmed"):
            pts += 5
    return pts


@app.post("/api/quests/{qid}/finish")
def finish_quest(qid: str, f: Finish):
    q = store.get_quest(qid) or _404()
    now = time.time()
    finished = f.finished_at if f.finished_at and (q["started_at"] or 0) <= f.finished_at <= now + 60 else now
    if not q["started_at"]:
        store.update_quest(qid, started_at=q["created_at"], deadline_at=q["created_at"] + q["deadline_minutes"] * 60)
        q = store.get_quest(qid)
    tasks = store.task_states(q)
    _, met = away_progress(q)  # Medium/Hard walks from here only count if you actually walked away from the start
    store.update_quest(qid, finished_at=q["finished_at"] or finished, status="done", note=f.note, mood=f.mood,
                       screen_seconds=f.screen_seconds if f.screen_seconds is not None else q["screen_seconds"],
                       points=points_for(tasks) if met else 0, requirement_met=int(met))
    view = quest_view(qid)
    view["outside_seconds"] = (view["finished_at"] or now) - view["started_at"]
    view["new_badges"] = badges.evaluate(qid) if not q["finished_at"] and met else []
    view["streak"] = badges.current_streak()
    return view


# ---------- sky gallery ----------

class Share(BaseModel):
    city: str


@app.post("/api/captures/{cid}/share")
async def share_sky(cid: str, s: Share):
    c = store.get_capture(cid) or _404()
    q = store.get_quest(c["quest_id"])
    task = next((t for t in q["tasks"] if t["id"] == c["task_id"]), {})
    if task.get("category") != "sky" or c["status"] not in ("pass", "self", "unconfirmed"):
        raise HTTPException(400, "Only verified sky photos can be shared.")
    jpeg = (config.PHOTO_DIR / c["photo"]).read_bytes()
    ok, message = await gallery_client.moderate(jpeg)
    if not ok:
        raise HTTPException(422, message)
    taken = datetime.fromtimestamp(c["taken_at"], context.tzinfo(q["tz"]))
    try:
        return await gallery_client.share(jpeg, s.city, taken, (q["context"] or {}).get("season", "fall"))
    except (gallery_client.ShareError, httpx.HTTPError) as e:
        raise HTTPException(424, f"Couldn't reach the Sky Gallery: {e}")  # not 502: the phone treats 502 as offline


# ---------- voice ----------

@app.get("/api/quests/{qid}/audio")
async def quest_audio(qid: str):
    q = store.get_quest(qid) or _404()
    try:
        path = await tts.mp3_for(tts.speech_text(q))
    except tts.TTSError as e:
        raise HTTPException(503, str(e))
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=31536000"})


# ---------- journal & badges ----------

@app.get("/api/journal")
def journal():
    with db.tx() as c:
        qs = db.rows(c.execute("SELECT id FROM quests WHERE status='done' ORDER BY finished_at DESC").fetchall())
        earned_by_quest: dict[str, list] = {}
        for r in c.execute("SELECT code, quest_id FROM badges").fetchall():
            earned_by_quest.setdefault(r["quest_id"], []).append(badges.describe(r["code"]))
    entries = []
    for row in qs:
        v = quest_view(row["id"])
        v["outside_seconds"] = (v["finished_at"] or 0) - (v["started_at"] or 0)
        v["badges"] = earned_by_quest.get(row["id"], [])
        entries.append(v)
    return {"entries": entries, "streak": badges.current_streak()}


@app.get("/api/badges")
def all_badges():
    return {"badges": badges.all_badges(), "streak": badges.current_streak()}


def _404():
    raise HTTPException(404, "not found")


# ---------- static ----------

@app.get("/photos/{name}")
def photo(name: str):
    path = (config.PHOTO_DIR / name).resolve()
    if path.parent != config.PHOTO_DIR.resolve() or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000"})


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
