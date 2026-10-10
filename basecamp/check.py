"""Phase 0 sanity check: is Ollama up and is the Gemma model pulled?"""
import sys

import httpx

from basecamp.app import config


def main() -> int:
    try:
        r = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
    except Exception as e:
        print(f"[x] Ollama not reachable at {config.OLLAMA_URL}: {e}")
        print("    Install from https://ollama.com and run `ollama serve`.")
        return 1
    names = [m["name"] for m in r.json().get("models", [])]
    if config.GEMMA_MODEL not in names:
        print(f"[x] Ollama is up but {config.GEMMA_MODEL} is missing. Run: ollama pull {config.GEMMA_MODEL}")
        return 1
    print(f"[ok] Ollama up, {config.GEMMA_MODEL} available")
    for key in ("SERPAPI_KEY", "ELEVENLABS_API_KEY", "SENTRY_DSN", "GALLERY_URL"):
        print(f"[{'ok' if getattr(config, key) else '--'}] {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
