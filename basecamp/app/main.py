"""Base Camp: the FastAPI server that runs on the laptop next to Gemma."""
import io
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from . import badges, config, context, db, gallery_client, gemma, places, quests, store, tracing, tts, verify

tracing.init()
db.init()
app = FastAPI(title="Walk Journal Base Camp")
STATIC = Path(__file__).resolve().parents[1] / "static"


# ---------- settings ----------

class Settings(BaseModel):
    home_lat: float | None = None
    home_lng: float | None = None
    home_city: str | None = None
    units: str = "mi"


@app.get("/api/health")
async def health():
    return {"ok": True, "gemma": await gemma.available(), "model": config.GEMMA_MODEL,
            "serpapi": bool(config.SERPAPI_KEY), "elevenlabs": bool(config.ELEVENLABS_API_KEY),
            "gallery": bool(config.GALLERY_URL)}


@app.get("/api/settings")
def get_settings():
    return db.get_setting("settings", Settings().model_dump())


@app.post("/api/settings")
def save_settings(s: Settings):
    db.set_setting("settings", s.model_dump())
    return s


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
        thumbs = db.rows(c.execute("""SELECT quest_id, photo FROM captures WHERE status IN ('pass','self')
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
    suggested = [p for pid, p in all_places.items() if pid not in visited and not pid.startswith("here-")]
    return {"visited": list(visited.values()), "suggested": suggested,
            "stats": {"walks": len(done), "places": len(visited), "outside_seconds": round(outside)}}


# ---------- quests ----------

class NewQuest(BaseModel):
    place_id: str
    distance_tier: str = "nearby"
    difficulty: str = "easy"
    tz: str | None = None


@app.post("/api/quests")
async def create_quest(req: NewQuest):
    if req.difficulty not in quests.TASK_COUNTS:
        raise HTTPException(400, "difficulty must be easy, medium or hard")
    place = store.get_place(req.place_id)
    if not place:
        raise HTTPException(404, "unknown place")
    ctx = context.build(place, req.distance_tier, req.difficulty, req.tz, store.recent_task_types())
    quest, source, notes = await quests.generate(ctx, place)
    qid = db.new_id()
    with db.tx() as c:
        c.execute("""INSERT INTO quests (id, place_id, created_at, tz, distance_tier, difficulty, title, spoken_intro,
                     deadline_minutes, tasks, context, source, skips_left) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (qid, place["id"], db.now(), req.tz, req.distance_tier, req.difficulty, quest["title"],
                   quest["spoken_intro"], quest["deadline_minutes"], db.dumps(quest["tasks"]),
                   db.dumps(ctx | {"repairs": notes}), source, 1 if req.difficulty == "hard" else 0))
    return quest_view(qid)


def quest_view(qid: str) -> dict:
    q = store.get_quest(qid)
    if not q:
        raise HTTPException(404, "unknown quest")
    place = store.get_place(q["place_id"])
    tasks = store.task_states(q)
    ctx = q["context"] or {}
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
                      file_modified: float | None = Form(None), photo: UploadFile = File(...)):
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
    try:
        jpeg, img = verify.clean_image(raw)
    except Exception:
        raise HTTPException(400, "not an image")
    cid = db.new_id()
    fname = f"{cid}.jpg"
    (config.PHOTO_DIR / fname).write_bytes(jpeg)
    taken = taken_at / 1000 if taken_at > 1e11 else taken_at  # accept ms or s
    with db.tx() as c:
        c.execute("""INSERT INTO captures (id, client_id, quest_id, task_id, kind, photo, taken_at, lat, lng, accuracy,
                     source, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (cid, client_id, qid, task_id, task["kind"], fname, taken, lat, lng, accuracy, source,
                   "pending", db.now()))
    extra = {"file_modified": file_modified / 1000 if file_modified and file_modified > 1e11 else file_modified}
    await run_checks(cid, extra)
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

    place = store.get_place(q["place_id"])
    place_ok, dist = verify.check_place(c["lat"], c["lng"], c["accuracy"], place)
    time_ok = verify.check_time(c["taken_at"], q, c["created_at"])
    fm = (extra or {}).get("file_modified")
    if c["source"] != "camera" and fm and fm < c["taken_at"] - verify.TIME_GRACE_S:
        time_ok = False
    reason = None
    if not place_ok:
        reason = ("No GPS fix with this photo, so I can't confirm the place." if dist is None
                  else f"This was taken {dist} m from {place['name']}. Head a little closer and try again.")
    elif not time_ok:
        reason = "This photo was taken outside the quest time window."
    if reason:
        store.update_capture(cid, status="fail", place_ok=int(place_ok), time_ok=int(time_ok), quality=qual,
                             reason=reason, verified_at=time.time())
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
                         verified_at=None if status == "pending" else time.time())


@app.post("/api/captures/{cid}/retry")
async def retry_capture(cid: str):
    c = store.get_capture(cid) or _404()
    if c["status"] == "pending":
        await run_checks(c["id"])
    return store.public_capture(store.get_capture(c["id"]))


@app.post("/api/captures/retry-pending")
async def retry_pending():
    with db.tx() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM captures WHERE status='pending'").fetchall()]
    for cid in ids:
        await run_checks(cid)
    return {"retried": len(ids)}


@app.post("/api/captures/{cid}/self-confirm")
def self_confirm(cid: str):
    c = store.get_capture(cid) or _404()
    if c["status"] not in ("unsure", "pending"):
        raise HTTPException(400, "only photos Gemma couldn't judge can be self-confirmed")
    if c["kind"] == "photo" and not (c["place_ok"] and c["time_ok"]):
        raise HTTPException(400, "place and time checks must pass first")
    store.update_capture(c["id"], status="self", reason="You confirmed this one yourself.", verified_at=time.time())
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
        elif t["status"] == "self":
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
    store.update_quest(qid, finished_at=q["finished_at"] or finished, status="done", note=f.note, mood=f.mood,
                       screen_seconds=f.screen_seconds if f.screen_seconds is not None else q["screen_seconds"],
                       points=points_for(tasks))
    view = quest_view(qid)
    view["outside_seconds"] = (view["finished_at"] or now) - view["started_at"]
    view["new_badges"] = badges.evaluate(qid) if not q["finished_at"] else []
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
    if task.get("category") != "sky" or c["status"] not in ("pass", "self"):
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
