"""ElevenLabs text-to-speech with an on-disk mp3 cache. Only the quest text is sent."""
import hashlib
from pathlib import Path

import httpx

from . import config

MODEL_ID = "eleven_flash_v2_5"


class TTSError(RuntimeError):
    pass


def speech_text(quest: dict) -> str:
    tasks = [t for t in quest["tasks"]]
    lines = " ".join(f"{i}. {t['prompt']}." for i, t in enumerate(tasks, 1))
    return f"{quest['title']}. {quest['spoken_intro']} Here's what to find. {lines} Enjoy the walk."


async def mp3_for(text: str) -> Path:
    if not config.ELEVENLABS_API_KEY:
        raise TTSError("ELEVENLABS_API_KEY is not set")
    key = hashlib.sha1(f"{config.ELEVENLABS_VOICE_ID}|{MODEL_ID}|{text}".encode()).hexdigest()[:20]
    path = config.AUDIO_DIR / f"{key}.mp3"
    if path.exists():
        return path
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{config.ELEVENLABS_VOICE_ID}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": config.ELEVENLABS_API_KEY, "Accept": "audio/mpeg"},
            json={"text": text, "model_id": MODEL_ID,
                  "voice_settings": {"stability": 0.6, "similarity_boost": 0.75, "speed": 0.95}},
        )
    if r.status_code != 200:
        raise TTSError(f"ElevenLabs error {r.status_code}: {r.text[:200]}")
    path.write_bytes(r.content)
    return path
