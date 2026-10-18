import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
PHOTO_DIR = DATA_DIR / "photos"
AUDIO_DIR = DATA_DIR / "audio"
for d in (DATA_DIR, PHOTO_DIR, AUDIO_DIR):
    d.mkdir(parents=True, exist_ok=True)

# "local": one walker, Gemma in Ollama on this laptop (the original Base Camp).
# "cloud": a public beta (Render). Every phone gets its own private journal, daily limits protect the API
# quota, and Gemma calls go to Google AI Studio's hosted Gemma.
MODE = os.getenv("WALKJOURNAL_MODE", "local").strip().lower()
CLOUD = MODE == "cloud"
# Cloud mode storage: MongoDB Atlas (database "walk_journal" on the free cluster). Render's free plan has no disk,
# so with a MONGODB_URI every table and photo lives in Atlas and the SQLite file is only a working copy, rebuilt
# from Atlas at each start (atlas.py). Local mode always uses data/walkjournal.db and never touches Atlas.
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB = os.getenv("MONGODB_DB", "walk_journal")
ATLAS = CLOUD and bool(MONGODB_URI)
if CLOUD and not ATLAS and os.getenv("RENDER"):  # Render sets RENDER; its free disk would lose every walk
    raise RuntimeError("Cloud mode on Render needs MONGODB_URI: the free filesystem is wiped on every restart.")
# A different file name, so a laptop trying cloud mode can never replace its own journal with the working copy.
DB_PATH = DATA_DIR / ("atlas-working-copy.db" if ATLAS else "walkjournal.db")
# Where Gemma runs: "ollama" (local) or "google" (AI Studio's hosted Gemma). Cloud mode defaults to google.
GEMMA_BACKEND = os.getenv("GEMMA_BACKEND", "google" if CLOUD else "ollama").strip().lower()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_GEMMA_MODEL = os.getenv("GOOGLE_GEMMA_MODEL", "gemma-4-26b-a4b-it")  # accepts images, for photo checks
GOOGLE_API_URL = os.getenv("GOOGLE_API_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
# Reads the feedback page at /admin/feedback (any username, this as the password). Blank = page off.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "gemma4:e4b")
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID") or "21m00Tcm4TlvDq8ikWAM"  # blank in .env -> default voice
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
GALLERY_URL = os.getenv("GALLERY_URL", "").rstrip("/")
GALLERY_TOKEN = os.getenv("GALLERY_TOKEN", "")
PLACE_RADIUS_M = float(os.getenv("PLACE_RADIUS_M", "400"))
# Web Push. Generate with: python -m basecamp.app.push --generate
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT") or "mailto:nikithakls17-sys@users.noreply.github.com"


def limit(name: str, default: int) -> int:
    """Daily limits for cloud mode, overridable in the environment (e.g. LIMIT_PHOTOS_PER_USER=20)."""
    try:
        return int(os.getenv(f"LIMIT_{name.upper()}", default))
    except ValueError:
        return default
