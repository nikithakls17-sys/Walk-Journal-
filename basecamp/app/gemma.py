"""Thin client for Gemma running locally in Ollama, with JSON-schema structured output."""
import base64
import json
import time
from typing import Callable

import httpx

from . import config, tracing


class GemmaError(RuntimeError):
    pass


async def chat(prompt: str, schema: dict, *, system: str | None = None, images: list[bytes] | None = None,
               temperature: float = 0.7, operation: str = "chat", timeout: float = 180,
               outcome: Callable[[dict], str] | None = None) -> dict:
    """Call Gemma with a JSON schema. `outcome` labels the result for tracing (runs inside the span)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    msg = {"role": "user", "content": prompt}
    if images:
        msg["images"] = [base64.b64encode(b).decode() for b in images]
    messages.append(msg)
    body = {
        "model": config.GEMMA_MODEL,
        "messages": messages,
        "format": schema,
        "stream": False,
        "options": {"temperature": temperature},
    }
    with tracing.gen_ai_span(operation, config.GEMMA_MODEL, prompt) as span:
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"{config.OLLAMA_URL}/api/chat", json=body)
                r.raise_for_status()
        except httpx.HTTPError as e:
            span.fail(str(e))
            raise GemmaError(f"Ollama request failed: {e}") from e
        data = r.json()
        content = data.get("message", {}).get("content", "")
        try:
            result = json.loads(content)
        except ValueError as e:
            span.fail("invalid json")
            raise GemmaError(f"Gemma returned invalid JSON: {content[:200]}") from e
        span.finish(
            latency_s=time.perf_counter() - t0,
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            response=content,
        )
        if outcome:
            span.outcome(outcome(result))
        return result


async def available() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{config.OLLAMA_URL}/api/tags")
            return any(m["name"] == config.GEMMA_MODEL for m in r.json().get("models", []))
    except Exception:
        return False
