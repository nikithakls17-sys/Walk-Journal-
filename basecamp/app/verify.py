"""Proof checks: right place (code), right time (code), right thing (Gemma vision)."""
import io
import time
from datetime import datetime

from PIL import Image, ImageFilter, ImageOps, ImageStat

from . import config, gemma
from .geo import haversine_m

MIN_CONFIDENCE = 0.6
MAX_GPS_TOLERANCE_M = 150
TIME_GRACE_S = 120
DARK_THRESHOLD = 35
BRIGHT_THRESHOLD = 245
BLUR_THRESHOLD = 25  # variance of the Laplacian on a 512px grayscale; tuned with eval/
SMOOTH_CATEGORIES = {"sky", "water"}  # a clear sky has almost no edges, so skip the blur test

VISION_SCHEMA = {
    "type": "object",
    "required": ["match", "confidence", "what_i_see", "reason"],
    "properties": {
        "match": {"type": "boolean"},
        "confidence": {"type": "number"},
        "what_i_see": {"type": "string"},
        "reason": {"type": "string"},
    },
}

VISION_SYSTEM = """You check photo proof for an outdoor scavenger hunt. Be fair but honest.
Say match=true only if the required thing is clearly visible in the photo. Close enough counts
(a mostly-red leaf with some orange is still a red leaf). Screens, printed pictures and indoor scenes
do not count for outdoor tasks. confidence is 0 to 1. what_i_see is one short sentence."""

TRACKER_SCHEMA = {
    "type": "object",
    "required": ["readable", "date", "distance_mi", "steps", "duration_min"],
    "properties": {
        "readable": {"type": "boolean"},
        "date": {"type": "string", "description": "YYYY-MM-DD, or empty if not shown"},
        "distance_mi": {"type": "number"},
        "steps": {"type": "integer"},
        "duration_min": {"type": "number"},
    },
}

TRACKER_SYSTEM = """You read screenshots from fitness trackers and watch apps. Extract only what is shown.
Convert km to miles (1 km = 0.621 mi). Use 0 for values that are not visible. If you cannot read it, readable=false.
If the screenshot says "Today", use the date you are given as today."""


def clean_image(data: bytes, max_px: int = 1600) -> tuple[bytes, Image.Image]:
    """Rotate per EXIF, then re-encode without any EXIF/GPS metadata."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_px, max_px))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)  # no exif= argument -> metadata dropped
    return out.getvalue(), img


def quality(img: Image.Image, category: str | None = None) -> dict:
    gray = img.convert("L")
    gray.thumbnail((512, 512))
    brightness = ImageStat.Stat(gray).mean[0]
    lap = gray.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128))
    sharpness = ImageStat.Stat(lap).var[0]
    problem = None
    if brightness < DARK_THRESHOLD:
        problem = "too_dark"
    elif brightness > BRIGHT_THRESHOLD:
        problem = "too_bright"
    elif sharpness < BLUR_THRESHOLD and category not in SMOOTH_CATEGORIES:
        problem = "blurry"
    return {"brightness": round(brightness, 1), "sharpness": round(sharpness, 1), "ok": problem is None, "problem": problem}


def check_place(lat, lng, accuracy, place: dict) -> tuple[bool, float | None]:
    if lat is None or lng is None:
        return False, None
    dist = haversine_m(lat, lng, place["lat"], place["lng"])
    radius = place.get("radius_m") or config.PLACE_RADIUS_M
    tolerance = min(accuracy or 0, MAX_GPS_TOLERANCE_M)
    return dist <= radius + tolerance, round(dist)


def check_time(taken_at: float, quest: dict, received_at: float | None = None) -> bool:
    received_at = received_at or time.time()
    if not quest.get("started_at") or not taken_at:
        return False
    if taken_at > received_at + TIME_GRACE_S:  # from the future
        return False
    return quest["started_at"] - TIME_GRACE_S <= taken_at <= quest["deadline_at"] + TIME_GRACE_S


async def check_content(image_jpeg: bytes, task: dict) -> dict:
    prompt = (f"Task: {task['prompt']}\nThe photo must show: {task['verify']}\n"
              "Does this photo show that? Answer in JSON.")
    try:
        r = await gemma.chat(prompt, VISION_SCHEMA, system=VISION_SYSTEM, images=[image_jpeg],
                             temperature=0.1, operation="verify_photo", timeout=120,
                             outcome=lambda g: OUTCOME_LABEL[decide_content(_clamp(g))])
    except gemma.GemmaError as e:
        return {"error": str(e)}
    return _clamp(r)


OUTCOME_LABEL = {"pass": "photo_match", "fail": "photo_no_match", "unsure": "couldnt_tell", "pending": "error"}


def _clamp(g: dict) -> dict:
    try:
        g["confidence"] = max(0.0, min(1.0, float(g.get("confidence") or 0)))
    except (TypeError, ValueError):
        g["confidence"] = 0.0
    return g


def decide_content(g: dict) -> str:
    if g.get("error"):
        return "pending"
    if g.get("match") and g["confidence"] >= MIN_CONFIDENCE:
        return "pass"
    if not g.get("match") and g["confidence"] >= MIN_CONFIDENCE:
        return "fail"
    return "unsure"


async def read_tracker(image_jpeg: bytes, today: str) -> dict:
    try:
        r = await gemma.chat(f"Today is {today}. Read this fitness tracker screenshot.", TRACKER_SCHEMA,
                             system=TRACKER_SYSTEM, images=[image_jpeg], temperature=0.0,
                             operation="read_tracker", timeout=120,
                             outcome=lambda t: "tracker_readable" if t.get("readable") else "tracker_unreadable")
    except gemma.GemmaError as e:
        return {"error": str(e)}
    return r


def decide_tracker(reading: dict, goal: dict, today: str) -> tuple[str, str]:
    if reading.get("error"):
        return "pending", "Base Camp couldn't reach Gemma, will retry."
    if not reading.get("readable"):
        return "unsure", "Couldn't read that screenshot. Try a clearer one."
    date = (reading.get("date") or "").strip()
    if date and date != today:
        return "fail", f"That screenshot is from {date}, not today."
    want = goal.get("distance_mi", 0)
    got = float(reading.get("distance_mi") or 0)
    if got >= want:
        return "pass", f"{got:.2f} mi walked. Goal was {want:g} mi."
    return "fail", f"{got:.2f} mi so far. Goal is {want:g} mi."


def local_date(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
