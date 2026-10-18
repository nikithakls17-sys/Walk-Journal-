"""Where task photos live. Local mode: JPEG files in data/photos, as always. Cloud mode with Atlas: small JPEGs
(at most 800 px, about 100 KB, no EXIF) in the "photos" collection, because Render's free disk is wiped on every
restart, and small photos keep the free cluster well under its 512 MB."""
import re
import time

from . import atlas, config, verify

CLOUD_MAX_PX = 800
CLOUD_MAX_BYTES = 100_000
NAME_RE = re.compile(r"^[0-9a-f]{12}\.jpg$")


def clean(raw: bytes):
    """Strip EXIF (incl. GPS) and size the photo for where it will be stored. Returns (jpeg bytes, image)."""
    if config.CLOUD:
        return verify.clean_image(raw, CLOUD_MAX_PX, CLOUD_MAX_BYTES)
    return verify.clean_image(raw)


def _coll():
    return atlas.database()["photos"]


def save(name: str, jpeg: bytes):
    if config.ATLAS:
        _coll().replace_one({"_id": name}, {"_id": name, "data": jpeg, "bytes": len(jpeg), "created_at": time.time()},
                            upsert=True)
    else:
        (config.PHOTO_DIR / name).write_bytes(jpeg)


def load(name: str) -> bytes | None:
    if not NAME_RE.match(name or ""):
        return None
    if config.ATLAS:
        doc = _coll().find_one({"_id": name}, {"data": 1})
        return bytes(doc["data"]) if doc else None
    path = config.PHOTO_DIR / name
    return path.read_bytes() if path.exists() else None


def delete(names: list[str]):
    if config.ATLAS:
        if names:
            _coll().delete_many({"_id": {"$in": list(names)}})
        return
    for name in names:
        (config.PHOTO_DIR / name).unlink(missing_ok=True)
