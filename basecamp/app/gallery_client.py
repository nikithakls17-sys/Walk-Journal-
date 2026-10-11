"""Opt-in sharing to the public Sky Gallery. Gemma checks locally that it's a sky with no people first."""
import io
from datetime import datetime

import httpx
from PIL import Image

from . import config, gemma

MODERATION_SCHEMA = {
    "type": "object",
    "required": ["mostly_sky", "people_or_faces", "readable_text", "reason"],
    "properties": {
        "mostly_sky": {"type": "boolean"},
        "people_or_faces": {"type": "boolean"},
        "readable_text": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}
MODERATION_SYSTEM = """You check photos before they are shared publicly. Be strict about privacy.
mostly_sky: true if the sky fills at least half the photo.
people_or_faces: true if any person, face, body part or reflection of a person is visible.
readable_text: true if any readable text, house number, street sign or license plate is visible."""


class ShareError(RuntimeError):
    pass


async def moderate(jpeg: bytes) -> tuple[bool, str]:
    try:
        r = await gemma.chat("Can this photo be shared publicly in a gallery of skies?", MODERATION_SCHEMA,
                             system=MODERATION_SYSTEM, images=[jpeg], temperature=0.0, operation="moderate_sky",
                             outcome=lambda m: "share_allowed" if _verdict(m)[0] else "share_blocked")
    except gemma.GemmaError:
        return False, "Base Camp couldn't check this photo right now, so it wasn't shared. Try again later."
    return _verdict(r)


def _verdict(r: dict) -> tuple[bool, str]:
    if r.get("people_or_faces"):
        verdict = False, "This photo seems to have a person in it, so it stays private. Skies only!"
    elif r.get("readable_text"):
        verdict = False, "There's readable text or a sign in this one, so it stays private."
    elif not r.get("mostly_sky"):
        verdict = False, "This one is mostly not sky, so it stays in your journal."
    else:
        verdict = True, "Looks like a lovely sky."
    return verdict


def prepare(jpeg: bytes) -> bytes:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    img.thumbnail((1280, 1280))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)  # fresh encode, no EXIF
    return out.getvalue()


async def share(jpeg: bytes, city: str, taken: datetime, season: str) -> dict:
    if not config.GALLERY_URL:
        raise ShareError("GALLERY_URL is not set")
    data = {"city": city.strip()[:60] or "Somewhere", "date": taken.strftime("%Y-%m-%d"),
            "local_time": taken.strftime("%H:%M"), "season": season}
    headers = {"X-Gallery-Token": config.GALLERY_TOKEN} if config.GALLERY_TOKEN else {}
    async with httpx.AsyncClient(timeout=60) as client:  # Render free tier may need to wake up
        r = await client.post(f"{config.GALLERY_URL}/api/skies", data=data, headers=headers,
                              files={"image": ("sky.jpg", prepare(jpeg), "image/jpeg")})
    if r.status_code != 200:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:120]
        raise ShareError(f"Gallery said no ({r.status_code}): {detail}")
    return {**r.json(), "gallery": config.GALLERY_URL}
