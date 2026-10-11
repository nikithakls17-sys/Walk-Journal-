import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
PHOTO_DIR = DATA_DIR / "photos"
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "walkjournal.db"
for d in (DATA_DIR, PHOTO_DIR, AUDIO_DIR):
    d.mkdir(parents=True, exist_ok=True)

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
