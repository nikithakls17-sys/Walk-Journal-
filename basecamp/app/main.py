"""Base Camp: the FastAPI server. Local mode runs on the laptop next to Gemma in Ollama; cloud mode is the public
beta on Render, with Google's hosted Gemma, one private journal per phone, and daily limits."""
import asyncio
import html
import io
import json
import secrets
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from . import (badges, config, context, db, gallery_client, gemma, limits, places, push, quests, reminders, store,
               tracing, tts, users, verify)
from .geo import M_PER_MI

tracing.init()
db.init()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(reminders.loop())  # daily + journal reminders, checked once a minute (local mode)
    yield
    task.cancel()


app = FastAPI(title="Walk Journal Base Camp", lifespan=lifespan)
STATIC = Path(__file__).resolve().parents[1] / "static"
User = Depends(users.current)  # None in local mode; the phone's hashed id in cloud mode


def sharing_on() -> bool:
    """The Sky Gallery (public sharing) is off in the public beta, whatever the environment says."""
    return not config.CLOUD


PRIVACY = {
    "ollama": "Your photos, notes, mood and exact location stay on the laptop running Base Camp. Gemma checks your "
              "photos right there, so they never leave the laptop.",
    "google": "Photos are checked by Google’s hosted Gemma. Your journal, notes and mood are stored on the Walk Journal "
              "server, and only you can see them. Nothing is ever shared publicly.",
}


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
    weekly_goal: int = 3  # walks a week; gentle, never a streak


@app.get("/api/health")
async def health():
    return {"ok": True, "gemma": await gemma.available(), "model": gemma.model_name(), "mode": config.MODE,
            "backend": config.GEMMA_BACKEND, "serpapi": bool(config.SERPAPI_KEY),
            "elevenlabs": bool(config.ELEVENLABS_API_KEY), "gallery": bool(config.GALLERY_URL) and sharing_on(), "push": push.enabled()}


@app.get("/api/config")
def app_config():
    """What the phone needs to know about this Base Camp: which mode, which privacy note, what's switched on."""
    backend = "google" if config.GEMMA_BACKEND == "google" else "ollama"
    return {"mode": config.MODE, "cloud": config.CLOUD, "backend": backend, "privacy": PRIVACY[backend],
            "photos_checked_by": "Google’s hosted Gemma" if backend == "google" else "Gemma on the laptop",
            "beta": config.CLOUD, "gallery": sharing_on(), "push": push.enabled(), "can_delete": config.CLOUD,
            "limits": limits.summary()}


@app.get("/api/settings")
def get_settings(uid: str | None = User):
    return Settings(**users.settings(uid)).model_dump()


@app.post("/api/settings")
def save_settings(s: Settings, uid: str | None = User):
    for t in (s.daily_time, s.journal_time):
        if not reminders.valid_hhmm(t):
            raise HTTPException(400, "times must look like 17:00")
    if not 1 <= s.weekly_goal <= 7:
        raise HTTPException(400, "weekly goal must be 1 to 7 walks")
    db.set_setting(users.settings_key(uid), s.model_dump())
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
    s = db.get_setting("settings") or {}  # push is local mode only (one walker)
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
async def suggest(request: Request, lat: float, lng: float, tier: str = "nearby", difficulty: str = "easy",
                  tz: str | None = None, uid: str | None = User):
    limits.take("searches", uid, users.client_ip(request))
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
def map_data(uid: str | None = User):
    with db.tx() as c:
        done = db.rows(c.execute("""SELECT q.id, q.place_id, q.title, q.started_at, q.finished_at FROM quests q
                                    WHERE q.status='done' AND q.user_id IS ? ORDER BY q.finished_at DESC""",
                                 (uid,)).fetchall())
        if uid is None:
            all_places = {p["id"]: p for p in db.rows(c.execute("SELECT id, name, type, lat, lng FROM places").fetchall())}
        else:  # cloud: only places from this phone's own quests, never other people's searches
            all_places = {p["id"]: p for p in db.rows(c.execute(
                """SELECT DISTINCT p.id, p.name, p.type, p.lat, p.lng FROM places p JOIN quests q ON q.place_id = p.id
                   WHERE q.user_id = ?""", (uid,)).fetchall())}
        thumbs = db.rows(c.execute("""SELECT c.quest_id, c.photo FROM captures c JOIN quests q ON q.id = c.quest_id
                                      WHERE c.status IN ('pass','self','unconfirmed') AND c.kind='photo'
                                      AND q.user_id IS ? ORDER BY c.created_at""", (uid,)).fetchall())
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
        full = store.get_quest(q["id"])
        v["visits"].append({"quest_id": q["id"], "title": q["title"], "date": q["finished_at"],
                            "photos": photos_by_quest.get(q["id"], [])[:4],
                            "partial": not store.progress(full)["completed"]})
        if q["started_at"] and q["finished_at"]:
            outside += max(0, q["finished_at"] - q["started_at"])
    suggested = [p for pid, p in all_places.items()
                 if pid not in visited and not pid.startswith("here-") and not pid.startswith(HOME_WALKS_ID)]
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
    lat: float | None = None  # from_here: start point. no_location: the walker's saved home (season/sunset only)
    lng: float | None = None
    distance_tier: str = "nearby"
    difficulty: str = "easy"
    tz: str | None = None


def home_walks_id(uid: str | None) -> str:
    return HOME_WALKS_ID if uid is None else f"{HOME_WALKS_ID}-{uid}"


def home_walks_place(lat: float, lng: float, uid: str | None = None) -> dict:
    """All Walk from here quests share one map pin, at home if it's set, otherwise at the first start."""
    existing = store.get_place(home_walks_id(uid))
    s = users.settings(uid)
    if s.get("home_lat") is not None:
        lat, lng = s["home_lat"], s["home_lng"]
    elif existing:
        lat, lng = existing["lat"], existing["lng"]
    return store.save_place({"id": home_walks_id(uid), "name": "Home walks", "type": "walk from home",
                             "description": "", "features": [], "lat": lat, "lng": lng})


@app.post("/api/quests")
async def create_quest(req: NewQuest, request: Request, uid: str | None = User):
    if req.difficulty not in quests.TASK_COUNTS:
        raise HTTPException(400, "difficulty must be easy, medium or hard")
    if req.distance_tier not in places.TIERS:
        raise HTTPException(400, "distance_tier must be nearby, walk_run or adventure")
    limits.take("quests", uid, users.client_ip(request))
    if req.mode == "from_here":
        if req.lat is None or req.lng is None:
            raise HTTPException(400, "Walk from here needs your current location")
        place = home_walks_place(req.lat, req.lng, uid)
        gen_place = quests.home_place(req.lat, req.lng)  # what Gemma sees: no destination, no features
        start, away_goal = (req.lat, req.lng), AWAY_GOAL_M[req.difficulty]
    elif req.mode == "no_location":
        # Location permission denied: everyday tasks only, no place check. The saved home (if any) only
        # sets the season and sunset; it is never used to check photos.
        s = users.settings(uid)
        lat = req.lat if req.lat is not None else s.get("home_lat", 39.0)
        lng = req.lng if req.lng is not None else s.get("home_lng", -95.0)
        place = home_walks_place(lat, lng, uid)
        gen_place, start, away_goal = quests.home_place(lat, lng), (None, None), 0
    elif req.mode == "place":
        place = store.get_place(req.place_id or "")
        if not place:
            raise HTTPException(404, "unknown place")
        gen_place, start, away_goal = place, (None, None), 0
    else:
        raise HTTPException(400, "mode must be place, from_here or no_location")
    ctx = context.build(gen_place, req.distance_tier, req.difficulty, req.tz, store.recent_task_types(uid=uid))
    ctx["mode"] = req.mode
    quest, source, notes = await quests.generate(ctx, gen_place)
    qid = db.new_id()
    with db.tx() as c:
        c.execute("""INSERT INTO quests (id, place_id, created_at, tz, distance_tier, difficulty, title, spoken_intro,
                     deadline_minutes, tasks, context, source, skips_left, mode, start_lat, start_lng, away_goal_m, user_id)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (qid, place["id"], db.now(), req.tz, req.distance_tier, req.difficulty, quest["title"],
                   quest["spoken_intro"], quest["deadline_minutes"], db.dumps(quest["tasks"]),
                   db.dumps(ctx | {"repairs": notes}), source, 1 if req.difficulty == "hard" else 0,
                   req.mode, start[0], start[1], away_goal, uid))
    return quest_view(qid)


away_progress = store.away_progress


def quest_view(qid: str) -> dict:
    q = store.get_quest(qid)
    if not q:
        raise HTTPException(404, "unknown quest")
    place = store.get_place(q["place_id"])
    tasks = store.task_states(q)
    ctx = q["context"] or {}
    farthest, away_met = away_progress(q)
    prog = store.progress(q, tasks)
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
        "progress": prog, "completed": prog["completed"], "partial": q["status"] == "done" and not prog["completed"],
    }


def own_quest(qid: str, uid: str | None) -> dict:
    """The quest, if it belongs to this walker. Someone else's quest is a plain 404, as if it didn't exist."""
    q = store.get_quest(qid)
    if not q or q.get("user_id") != uid:
        raise HTTPException(404, "unknown quest")
    return q


def own_capture(cid: str, uid: str | None) -> dict:
    c = store.get_capture(cid)
    if not c:
        raise HTTPException(404, "not found")
    own_quest(c["quest_id"], uid)
    return c


@app.get("/api/quests/{qid}")
def get_quest(qid: str, uid: str | None = User):
    own_quest(qid, uid)
    return quest_view(qid)


class Start(BaseModel):
    started_at: float | None = None  # epoch seconds from the phone (may be queued offline)


@app.post("/api/quests/{qid}/start")
def start_quest(qid: str, s: Start, uid: str | None = User):
    q = own_quest(qid, uid)
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
async def swap_task(qid: str, s: Skip, background: BackgroundTasks, request: Request, uid: str | None = User):
    """Replace one unfinished photo task with a new one from Gemma. No point penalty."""
    q = own_quest(qid, uid)
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
    limits.take("swaps", uid, users.client_ip(request))
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
def skip_task(qid: str, s: Skip, uid: str | None = User):
    q = own_quest(qid, uid)
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

# Photos whose check is running right now. A phone sync asks to retry every pending photo; without this,
# a photo still being checked was checked again in parallel (one photo got ~9 Gemma calls in testing).
CHECKING: set[str] = set()
GALLERY_RECENT_S = 15 * 60  # a gallery photo taken in the last 15 minutes counts as "now"
NO_TIME_MESSAGE = ("Your phone removed this photo's time and location for privacy. It still counts, "
                   "but to get a full ✓, use Take photo.")


async def check_once(cid: str, extra: dict | None = None) -> bool:
    """Run a photo's checks unless they're already running. Returns False if skipped."""
    if cid in CHECKING:
        return False
    CHECKING.add(cid)
    try:
        await run_checks(cid, extra)
        return True
    finally:
        CHECKING.discard(cid)


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
                      background: BackgroundTasks = None, request: Request = None, uid: str | None = User):
    q = own_quest(qid, uid)
    existing = store.get_capture(client_id)
    if existing:
        own_quest(existing["quest_id"], uid)
        return store.public_capture(existing)
    task = next((t for t in q["tasks"] if t["id"] == task_id), None)
    if not task:
        raise HTTPException(404, "unknown task")
    limits.take("photos", uid, users.client_ip(request) if request is not None else None)
    raw = await photo.read()
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(413, "photo too large")
    taken = taken_at / 1000 if taken_at > 1e11 else taken_at  # accept ms or s
    proof = "no_location" if q.get("mode") == "no_location" else "live"
    exif_time = exif_gps = None
    if source == "gallery" and task["kind"] == "photo":
        # Time comes from the photo's EXIF. Place comes from the photo's EXIF GPS if it has one, otherwise from
        # the phone's live GPS at upload (sent with the upload). No EXIF time means nothing can be confirmed.
        meta = verify.read_exif(raw, context.tzinfo(q["tz"]))
        exif_time, exif_gps = int(meta["taken_at"] is not None), int(meta["lat"] is not None)
        if q.get("mode") == "no_location":
            proof = "time_only" if exif_time else "none"
            taken = meta["taken_at"] or taken
            lat = lng = accuracy = None
        elif exif_time and exif_gps:
            proof, taken, lat, lng, accuracy = "exif", meta["taken_at"], meta["lat"], meta["lng"], meta["accuracy"]
        elif exif_time and lat is not None and lng is not None:
            proof, taken = "gallery_live", meta["taken_at"]
        elif exif_time:
            proof, taken, lat, lng, accuracy = "time_only", meta["taken_at"], None, None, None
        else:
            proof, lat, lng, accuracy = "none", None, None, None
    elif q.get("mode") == "no_location":
        lat = lng = accuracy = None
    try:
        jpeg, img = verify.clean_image(raw)  # EXIF (incl. GPS) is gone from here on
    except Exception:
        raise HTTPException(400, "not an image")
    cid = db.new_id()
    fname = f"{cid}.jpg"
    (config.PHOTO_DIR / fname).write_bytes(jpeg)
    ua =(request.headers.get("user-agent", "") if request is not None else "")[:300]
    CHECKING.add(cid)  # claimed before the row exists, so a sync can't start a second check
    try:
        with db.tx() as c:
            c.execute("""INSERT INTO captures (id, client_id, quest_id, task_id, kind, photo, taken_at, lat, lng, accuracy,
                         source, status, created_at, proof, exif_time, exif_gps, user_agent)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (cid, client_id, qid, task_id, task["kind"], fname, taken, lat, lng, accuracy, source,
                       "pending", db.now(), proof, exif_time, exif_gps, ua))
        extra = {"file_modified": file_modified / 1000 if file_modified and file_modified > 1e11 else file_modified}
        await run_checks(cid, extra)
    finally:
        CHECKING.discard(cid)
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
        reason = {
            "unconfirmed": NO_TIME_MESSAGE,
            "fail": f"I see {g.get('what_i_see', 'something else')}. {g.get('reason', '')}".strip(),
            "unsure": f"Couldn't tell. {g.get('what_i_see', '')}".strip(),
            "pending": "Saved. Gemma will check it when Base Camp is reachable.",
        }[status]
        store.update_capture(cid, status=status, place_ok=None, time_ok=None, quality=qual, gemma=g, reason=reason,
                             verified_at=None if status == "pending" else time.time())
        return

    gallery = c["source"] == "gallery"

    def time_check() -> bool:
        ok = verify.check_time(c["taken_at"], q, c["created_at"])
        if not ok and gallery:  # gallery: inside the quest window, or taken in the last 15 minutes
            ok = -verify.TIME_GRACE_S <= c["created_at"] - c["taken_at"] <= GALLERY_RECENT_S
        return ok

    if c.get("proof") in ("no_location", "time_only") or q.get("mode") == "no_location":
        # Place can't be confirmed (location off, or a gallery photo with a time but no GPS): check time and content.
        time_ok = time_check()
        if not time_ok:
            store.update_capture(cid, status="fail", place_ok=None, time_ok=0, quality=qual, verified_at=time.time(),
                                 reason="This photo was taken outside the quest time window.")
            return
        g = await verify.check_content(jpeg, task)
        status = verify.decide_content(g)
        why = ("location is off, so only the time and what's in the photo were checked." if c.get("proof") == "no_location"
               else "this photo has a time but no location, so only the time and what's in it were checked.")
        reason = {
            "pass": f"{g.get('what_i_see', '')} Place not confirmed: {why}".strip(),
            "fail": f"I see {g.get('what_i_see', 'something else')}. {g.get('reason', '')}".strip(),
            "unsure": f"Couldn't tell. {g.get('what_i_see', '')}".strip(),
            "pending": "Saved. Gemma will check it when Base Camp is reachable.",
        }[status]
        store.update_capture(cid, status="unconfirmed" if status == "pass" else status, place_ok=None, time_ok=1,
                             quality=qual, gemma=g, reason=reason, verified_at=None if status == "pending" else time.time())
        return

    if q.get("mode") == "from_here":
        tier_mi = places.TIERS.get(q["distance_tier"], places.TIERS["nearby"])[1]
        place = {"name": "your start point", "lat": q["start_lat"], "lng": q["start_lng"],
                 "radius_m": from_here_radius_m(q["distance_tier"])}
    else:
        place = store.get_place(q["place_id"])
    place_ok, dist = verify.check_place(c["lat"], c["lng"], c["accuracy"], place)
    time_ok = time_check()
    fm = (extra or {}).get("file_modified")
    if c["source"] == "input" and fm and fm < c["taken_at"] - verify.TIME_GRACE_S:
        time_ok = False
    reason = None
    if not place_ok:
        if dist is None:
            reason = "No GPS fix with this photo, so I can't confirm the place."
        elif c.get("proof") == "gallery_live":
            reason = (f"You uploaded this {dist / M_PER_MI:.1f} mi from the walk's area. Gallery photos are placed by "
                      "your phone's location when you upload them, so upload while you're still out.")
        elif q.get("mode") == "from_here":
            reason = (f"This was taken {dist / M_PER_MI:.1f} mi from where you started. "
                      f"This walk stays within {tier_mi:g} mi, so head back a little and try again.")
        else:
            reason = f"This was taken {dist} m from {place['name']}. Head a little closer and try again."
    elif not time_ok:
        reason = ("This gallery photo was taken outside the quest time window." if gallery
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
async def retry_capture(cid: str, uid: str | None = User):
    c = own_capture(cid, uid)
    if c["status"] == "pending":
        await check_once(c["id"])
    return store.public_capture(store.get_capture(c["id"]))


@app.post("/api/captures/retry-pending")
async def retry_pending(background: BackgroundTasks, uid: str | None = User):
    with db.tx() as conn:
        rows = conn.execute("""SELECT c.id, c.quest_id FROM captures c JOIN quests q ON q.id = c.quest_id
                               WHERE c.status='pending' AND q.user_id IS ?""", (uid,)).fetchall()
    retried = [r for r in rows if r["id"] not in CHECKING]  # never a second check of a photo in progress
    for r in retried:
        await check_once(r["id"])
    for qid in {r["quest_id"] for r in retried}:
        background.add_task(push_quest_progress, qid)
    return {"retried": len(retried), "already_checking": len(rows) - len(retried)}


@app.post("/api/captures/{cid}/self-confirm")
def self_confirm(cid: str, background: BackgroundTasks, uid: str | None = User):
    c = own_capture(cid, uid)
    if c["status"] not in ("unsure", "pending"):
        raise HTTPException(400, "only photos Gemma couldn't judge can be self-confirmed")
    place_by_design = c.get("proof") in ("no_location", "time_only")
    if c["kind"] == "photo" and not (c["time_ok"] and (c["place_ok"] or place_by_design)):
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
def finish_quest(qid: str, f: Finish, uid: str | None = User):
    q = own_quest(qid, uid)
    now = time.time()
    finished = f.finished_at if f.finished_at and (q["started_at"] or 0) <= f.finished_at <= now + 60 else now
    if not q["started_at"]:
        store.update_quest(qid, started_at=q["created_at"], deadline_at=q["created_at"] + q["deadline_minutes"] * 60)
        q = store.get_quest(qid)
    tasks = store.task_states(q)
    _, met = away_progress(q)  # Medium/Hard walks from here only count if you actually walked away from the start
    prog = store.progress(q, tasks)  # 75% of tasks (rounded up) completes the walk; below that it's a partial walk
    store.update_quest(qid, finished_at=q["finished_at"] or finished, status="done", note=f.note, mood=f.mood,
                       screen_seconds=f.screen_seconds if f.screen_seconds is not None else q["screen_seconds"],
                       points=points_for(tasks) if prog["completed"] else 0, requirement_met=int(met),
                       completed=int(prog["completed"]))
    view = quest_view(qid)
    view["outside_seconds"] = (view["finished_at"] or now) - view["started_at"]
    view["new_badges"] = badges.evaluate(qid, uid) if not q["finished_at"] and prog["completed"] else []
    view["week"] = badges.week_progress(uid=uid)
    return view


# ---------- rescue: offline items stranded on an expired link ----------
# A phone keeps its offline queue under the link it was opened from. When a free tunnel link expires,
# that queue can't reach Base Camp any more. /rescue.js (loaded by a bookmarklet on the old page) sends
# photos to the normal capture endpoint, and quest start/finish items here: as a form field, because a
# page on another site can send a form but not JSON. Same handlers as normal; nothing is ever deleted.

@app.post("/api/rescue/job")
def rescue_job(job: str = Form(...)):
    if config.CLOUD:  # a laptop-tunnel problem; the cloud link never changes
        raise HTTPException(404, "not found")
    try:
        j = json.loads(job)
    except ValueError:
        raise HTTPException(400, "job must be JSON")
    qid, body = j.get("questId"), j.get("body") or {}
    if not qid or not store.get_quest(qid):
        raise HTTPException(404, "unknown quest")
    if j.get("kind") == "start":
        return start_quest(qid, Start(**body), None)
    if j.get("kind") == "finish":
        return finish_quest(qid, Finish(**body), None)
    raise HTTPException(400, "only start and finish items are rescued here; photos use the capture endpoint")


# ---------- sky gallery ----------

class Share(BaseModel):
    city: str


@app.post("/api/captures/{cid}/share")
async def share_sky(cid: str, s: Share, uid: str | None = User):
    if not sharing_on():
        raise HTTPException(404, "The Sky Gallery is turned off.")
    c = own_capture(cid, uid)
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
async def quest_audio(qid: str, request: Request, uid: str | None = User):
    q = own_quest(qid, uid)
    limits.take("audio", uid, users.client_ip(request))
    try:
        path = await tts.mp3_for(tts.speech_text(q))
    except tts.TTSError as e:
        raise HTTPException(503, str(e))
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=31536000"})


# ---------- journal & badges ----------

@app.get("/api/journal")
def journal(uid: str | None = User):
    with db.tx() as c:
        qs = db.rows(c.execute("SELECT id FROM quests WHERE status='done' AND user_id IS ? ORDER BY finished_at DESC",
                               (uid,)).fetchall())
    earned_by_quest: dict[str, list] = {}
    for r in badges.earned(uid).values():
        b = badges.describe(r["code"])  # retired badges (old daily streaks) stay stored but aren't shown
        if b:
            earned_by_quest.setdefault(r["quest_id"], []).append(b)
    entries = []
    for row in qs:
        v = quest_view(row["id"])
        v["outside_seconds"] = (v["finished_at"] or 0) - (v["started_at"] or 0)
        v["badges"] = earned_by_quest.get(row["id"], [])
        entries.append(v)
    return {"entries": entries, "week": badges.week_progress(uid=uid)}


@app.get("/api/badges")
def all_badges(uid: str | None = User):
    return {"badges": badges.all_badges(uid), "week": badges.week_progress(uid=uid)}


# ---------- feedback ----------

class Feedback(BaseModel):
    message: str = ""
    rating: int | None = None  # 1-10, optional


FEEDBACK_MAX = 2000


@app.post("/api/feedback")
def send_feedback(f: Feedback, request: Request, uid: str | None = User):
    message = f.message.strip()
    if not message and f.rating is None:
        raise HTTPException(400, "Write a few words or pick a rating first.")
    if len(message) > FEEDBACK_MAX:
        raise HTTPException(400, f"Please keep it under {FEEDBACK_MAX} characters.")
    if f.rating is not None and not 1 <= f.rating <= 10:
        raise HTTPException(400, "The rating is 1 to 10.")
    limits.take("feedback", uid, users.client_ip(request))
    with db.tx() as c:
        c.execute("""INSERT INTO feedback (id, user_hash, created_at, message, rating, mode, user_agent)
                     VALUES (?,?,?,?,?,?,?)""",
                  (db.new_id(), uid or "local", db.now(), message, f.rating, config.MODE,
                   request.headers.get("user-agent", "")[:300]))
    return {"ok": True, "message": "Thank you! Your feedback was sent. 🌿"}


basic = HTTPBasic(auto_error=False)


def admin(creds: HTTPBasicCredentials | None = Depends(basic)):
    """Any username; ADMIN_PASSWORD as the password. With no ADMIN_PASSWORD set, the page doesn't exist."""
    if not config.ADMIN_PASSWORD:
        raise HTTPException(404, "not found")
    if not creds or not secrets.compare_digest(creds.password.encode(), config.ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "admin only", headers={"WWW-Authenticate": 'Basic realm="Walk Journal admin"'})


@app.get("/admin/feedback", response_class=HTMLResponse)
def feedback_page(_=Depends(admin)):
    with db.tx() as c:
        rows = c.execute("SELECT * FROM feedback ORDER BY created_at DESC").fetchall()
    rated = [r["rating"] for r in rows if r["rating"] is not None]
    avg = f"{sum(rated) / len(rated):.1f} / 10 from {len(rated)} ratings" if rated else "no ratings yet"
    trs = "".join(
        f"<tr><td>{time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['created_at']))} UTC</td>"
        f"<td class=n>{r['rating'] if r['rating'] is not None else ''}</td><td>{html.escape(r['message'] or '')}</td>"
        f"<td class=s>{html.escape((r['user_hash'] or '')[:8])}</td></tr>" for r in rows)
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content=noindex><title>Walk Journal feedback</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;margin:0;padding:16px;background:#FBF5E6;color:#4A3B2C}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #e3d6b8;padding:8px;text-align:left;vertical-align:top}}
.n{{font-weight:700;text-align:center}}.s{{color:#998;font-family:monospace}}
td:nth-child(3){{white-space:pre-wrap;word-break:break-word}}</style>
<h1>Feedback</h1><p>{len(rows)} messages · average {avg}</p>
<table><tr><th>When</th><th>Rating</th><th>Message</th><th>Walker</th></tr>{trs}</table>"""


# ---------- your data: export and delete ----------

@app.get("/api/export")
def export_journal(uid: str | None = User):
    """A zip of this walker's walks (journal.json) and photos. Read-only: nothing is changed."""
    with db.tx() as c:
        qids = [r["id"] for r in c.execute("""SELECT id FROM quests WHERE user_id IS ? AND started_at IS NOT NULL
                                              ORDER BY started_at""", (uid,)).fetchall()]
    walks, photos = [], []
    for qid in qids:
        v = quest_view(qid)
        for t in v["tasks"]:
            cap = t.get("capture")
            if cap and cap.get("photo_url"):
                name = cap["photo_url"].rsplit("/", 1)[-1]
                photos.append(name)
                cap["photo_file"] = f"photos/{name}"
        walks.append(v)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("journal.json", json.dumps({"exported_at": datetime.now().isoformat(timespec="seconds"),
                                               "app": "Walk Journal", "walks": walks,
                                               "settings": users.settings(uid)}, ensure_ascii=False, indent=2))
        z.writestr("README.txt", "Your Walk Journal: journal.json has every walk (tasks, notes, moods, points), "
                                 "and photos/ has the photo that counted for each task.\n")
        for name in dict.fromkeys(photos):
            path = config.PHOTO_DIR / name
            if path.exists():
                z.write(path, f"photos/{name}")
    stamp = datetime.now().strftime("%Y-%m-%d")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="walk-journal-{stamp}.zip"'})


@app.delete("/api/my-data")
def delete_my_data(uid: str | None = User):
    """Cloud mode: erase this phone's walks, photos, settings, badges and feedback. Local mode never deletes:
    the journal lives in the laptop's data folder, and that's where it's managed."""
    if uid is None:
        raise HTTPException(400, "On a laptop Base Camp, your journal lives in its data folder. "
                                 "Nothing is deleted from the app.")
    with db.tx() as c:
        qs = c.execute("SELECT id, place_id FROM quests WHERE user_id=?", (uid,)).fetchall()
        qids = [r["id"] for r in qs]
        photos = [r["photo"] for qid in qids
                  for r in c.execute("SELECT photo FROM captures WHERE quest_id=?", (qid,)).fetchall() if r["photo"]]
        for qid in qids:
            c.execute("DELETE FROM captures WHERE quest_id=?", (qid,))
        c.execute("DELETE FROM quests WHERE user_id=?", (uid,))
        for pid in {r["place_id"] for r in qs}:  # spots made from this walker's location, if no one else uses them
            personal = pid.startswith("here-") or pid == home_walks_id(uid)
            if personal and not c.execute("SELECT 1 FROM quests WHERE place_id=?", (pid,)).fetchone():
                c.execute("DELETE FROM places WHERE id=?", (pid,))
        c.execute("DELETE FROM places WHERE id=?", (home_walks_id(uid),))
        c.execute("DELETE FROM user_badges WHERE user_id=?", (uid,))
        c.execute("DELETE FROM feedback WHERE user_hash=?", (uid,))
        c.execute("DELETE FROM settings WHERE key=?", (users.settings_key(uid),))
    for name in photos:
        (config.PHOTO_DIR / name).unlink(missing_ok=True)
    return {"ok": True, "deleted_walks": len(qids), "deleted_photos": len(photos)}


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
