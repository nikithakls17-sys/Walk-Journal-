"""Sky Gallery: a calm public wall of skies people chose to share. Deployed on Render.

Stores images in MongoDB Atlas when MONGODB_URI is set, otherwise in a local SQLite file.
Only receives: an already-resized sky photo, a city name, a date, a local time and a season.
"""
import base64
import io
import os
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from html import escape

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from PIL import Image, ImageOps

MONGODB_URI = os.getenv("MONGODB_URI", "")
GALLERY_TOKEN = os.getenv("GALLERY_TOKEN", "")
DATA_DIR = os.getenv("DATA_DIR", ".")
MAX_BYTES = 4 * 1024 * 1024
MAX_PX = 1280
RATE = (5, 3600)  # 5 uploads per IP per hour
SEASONS = {"spring", "summer", "fall", "winter", "monsoon", "post-monsoon", "wet", "dry"}  # incl. tropical seasons

app = FastAPI(title="Walk Journal Sky Gallery")


# ---------- storage ----------

class MongoStore:
    def __init__(self, uri: str):
        from pymongo import DESCENDING, MongoClient
        self.col = MongoClient(uri, serverSelectionTimeoutMS=8000)["walkjournal"]["skies"]
        self.col.create_index([("created_at", DESCENDING)])
        self.desc = DESCENDING

    def add(self, doc: dict):
        self.col.insert_one(dict(doc, _id=doc["id"]))

    def latest(self, limit: int) -> list[dict]:
        return list(self.col.find({}, {"image": 0}).sort("created_at", self.desc).limit(limit))

    def image(self, sid: str) -> bytes | None:
        d = self.col.find_one({"_id": sid}, {"image": 1})
        return bytes(d["image"]) if d else None


class SqliteStore:
    def __init__(self, path: str):
        self.path = path
        with self._c() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS skies (id TEXT PRIMARY KEY, city TEXT, date TEXT, local_time TEXT,
                         season TEXT, created_at REAL, image BLOB)""")

    def _c(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def add(self, doc: dict):
        with self._c() as c:
            c.execute("INSERT INTO skies VALUES (?,?,?,?,?,?,?)", (doc["id"], doc["city"], doc["date"], doc["local_time"],
                                                                   doc["season"], doc["created_at"], doc["image"]))

    def latest(self, limit: int) -> list[dict]:
        with self._c() as c:
            return [dict(r) for r in c.execute("""SELECT id, city, date, local_time, season, created_at FROM skies
                                                   ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()]

    def image(self, sid: str) -> bytes | None:
        with self._c() as c:
            r = c.execute("SELECT image FROM skies WHERE id=?", (sid,)).fetchone()
        return r["image"] if r else None


store = MongoStore(MONGODB_URI) if MONGODB_URI else SqliteStore(os.path.join(DATA_DIR, "skies.db"))


# ---------- rate limit ----------

hits: dict[str, deque] = defaultdict(deque)


def rate_limited(ip: str) -> bool:
    n, window = RATE
    q = hits[ip]
    now = time.time()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= n:
        return True
    q.append(now)
    return False


def client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "?")


# ---------- API ----------

def clean(s: str, n: int) -> str:
    return " ".join((s or "").split())[:n]


@app.post("/api/skies")
async def add_sky(request: Request, image: UploadFile = File(...), city: str = Form(...), date: str = Form(...),
                  local_time: str = Form(...), season: str = Form(...), x_gallery_token: str = Header("")):
    if GALLERY_TOKEN and x_gallery_token != GALLERY_TOKEN:
        raise HTTPException(401, "bad token")
    if rate_limited(client_ip(request)):
        raise HTTPException(429, "Too many skies for now. Try again later.")
    raw = await image.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "image too large")
    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Exception:
        raise HTTPException(400, "not an image")
    img.thumbnail((MAX_PX, MAX_PX))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82)  # re-encoded: no EXIF, no GPS
    season = season.lower()
    if season not in SEASONS:
        raise HTTPException(400, "bad season")
    doc = {"id": uuid.uuid4().hex[:16], "city": clean(city, 60) or "Somewhere", "date": clean(date, 10),
           "local_time": clean(local_time, 5), "season": season, "created_at": time.time(), "image": out.getvalue()}
    store.add(doc)
    return {"id": doc["id"], "url": f"/skies/{doc['id']}.jpg"}


@app.get("/api/skies")
def list_skies(limit: int = 60):
    return [{k: d[k] for k in ("id", "city", "date", "local_time", "season")} | {"url": f"/skies/{d['id']}.jpg"}
            for d in store.latest(min(limit, 200))]


@app.get("/skies/{sid}.jpg")
def sky_image(sid: str):
    data = store.image(sid)
    if not data:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "storage": "mongodb" if MONGODB_URI else "sqlite"}


# ---------- page ----------

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sky Gallery</title>
<meta name="description" content="Skies shared by people out on a walk with Walk Journal.">
<style>
:root{--bg:#FBF5E6;--text:#4A3B2C;--muted:rgba(74,59,44,.65);--card:#FFFDF7;--edge:#EADFC4;--sky:#CFE6F2}
@media (prefers-color-scheme:dark){:root{--bg:#1F2A24;--text:#F3ECD9;--muted:rgba(243,236,217,.65);--card:#26332C;--edge:#17201B;--sky:#1A2433}}
*{box-sizing:border-box}body{margin:0;font:17px/1.5 ui-rounded,"Nunito","Segoe UI",system-ui,sans-serif;color:var(--text);
background:linear-gradient(var(--sky),var(--bg) 280px) no-repeat,var(--bg)}
header{max-width:1100px;margin:0 auto;padding:40px 16px 8px}h1{margin:0;font-size:2rem}header p{margin:4px 0 0;color:var(--muted)}
main{max-width:1100px;margin:0 auto;padding:16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}
figure{margin:0;background:var(--card);border:1px solid var(--edge);border-radius:18px;overflow:hidden;box-shadow:0 2px 0 var(--edge)}
img{display:block;width:100%;aspect-ratio:4/3;object-fit:cover;background:var(--sky)}
figcaption{padding:10px 14px;font-weight:700}figcaption span{display:block;font-weight:400;color:var(--muted);font-size:.85rem}
.empty{grid-column:1/-1;text-align:center;color:var(--muted);padding:60px 0}
footer{text-align:center;color:var(--muted);font-size:.85rem;padding:32px 16px}
</style></head><body>
<header><h1>Sky Gallery</h1><p>Skies people saw on their walks. No likes, no comments. Just look up.</p></header>
<main>__ITEMS__</main>
<footer>Shared from <a href="https://github.com/nikithakls17-sys/Walk-Journal-" style="color:inherit">Walk Journal</a>.
City-level location only. Photos are checked for people before sharing.</footer>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def page():
    items = store.latest(120)
    if not items:
        cards = '<p class="empty">No skies yet. Go outside and look up.</p>'
    else:
        cards = "".join(
            f'<figure><img src="/skies/{escape(d["id"])}.jpg" alt="Sky over {escape(d["city"])}" loading="lazy">'
            f'<figcaption>{escape(d["city"])}<span>{escape(d["date"])} · {escape(d["local_time"])} · {escape(d["season"])}</span>'
            f'</figcaption></figure>' for d in items)
    return PAGE.replace("__ITEMS__", cards)
