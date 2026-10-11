"""Loaded by pytest before any test module, so the app's config never sees the real .env values.

Without this, running a single test file that imports basecamp first used the real data/ folder,
the real Gemma, and real API keys.
"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="walkjournal-test-")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"  # nothing listens here
for key in ("SERPAPI_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "SENTRY_DSN", "GALLERY_URL",
            "GALLERY_TOKEN", "MONGODB_URI", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"):
    os.environ[key] = ""  # load_dotenv() never overrides variables that are already set


def pytest_configure(config):
    from basecamp.app import config as app_config
    real = (app_config.ROOT / "data").resolve()
    assert app_config.DATA_DIR.resolve() != real, "tests must never touch the real data/ folder"
    assert not app_config.SERPAPI_KEY and not app_config.ELEVENLABS_API_KEY, "tests must never use real API keys"
