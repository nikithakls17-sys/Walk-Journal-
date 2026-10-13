"""Thin Gemma client with JSON-schema structured output.

Two backends, same call: Gemma in Ollama on the laptop (local mode), or Google AI Studio's hosted Gemma
(cloud mode, GEMMA_BACKEND=google). Every caller and every rule check is identical either way.
"""
import base64
import json
import re
import time
from typing import Callable

import httpx

from . import config, tracing


KEEP_ALIVE = "2h"


class GemmaError(RuntimeError):
    pass


def model_name() -> str:
    return config.GOOGLE_GEMMA_MODEL if config.GEMMA_BACKEND == "google" else config.GEMMA_MODEL


async def chat(prompt: str, schema: dict, *, system: str | None = None, images: list[bytes] | None = None,
               temperature: float = 0.7, operation: str = "chat", timeout: float = 180,
               outcome: Callable[[dict], str] | None = None) -> dict:
    """Call Gemma with a JSON schema. `outcome` labels the result for tracing (runs inside the span)."""
    with tracing.gen_ai_span(operation, model_name(), prompt) as span:
        t0 = time.perf_counter()
        try:
            if config.GEMMA_BACKEND == "google":
                content, tokens_in, tokens_out = await _google(prompt, schema, system, images, temperature, timeout)
            else:
                content, tokens_in, tokens_out = await _ollama(prompt, schema, system, images, temperature, timeout)
        except httpx.HTTPError as e:
            span.fail(str(e))
            raise GemmaError(f"Gemma request failed: {e}") from e
        except GemmaError as e:
            span.fail(str(e))
            raise
        try:
            result = parse_json(content)
        except ValueError as e:
            span.fail("invalid json")
            raise GemmaError(f"Gemma returned invalid JSON: {content[:200]}") from e
        span.finish(latency_s=time.perf_counter() - t0, input_tokens=tokens_in, output_tokens=tokens_out,
                    response=content)
        if outcome:
            span.outcome(outcome(result))
        return result


def parse_json(content: str) -> dict:
    """The reply as JSON. Hosted models sometimes wrap it in ```json fences or add a line around it."""
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        result = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        result = json.loads(text[start:end + 1])
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object")
    return result


# ---------- local: Ollama ----------

async def _ollama(prompt, schema, system, images, temperature, timeout) -> tuple[str, int | None, int | None]:
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
        "think": False,  # gemma4 thinks by default: slow on a laptop, and the reply sometimes ignored the schema
        "keep_alive": KEEP_ALIVE,  # stay loaded between requests; reloading took ~80 s in testing
        "stream": False,
        "options": {"temperature": temperature},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{config.OLLAMA_URL}/api/chat", json=body)
        r.raise_for_status()
    data = r.json()
    return data.get("message", {}).get("content", ""), data.get("prompt_eval_count"), data.get("eval_count")


# ---------- cloud: Google AI Studio's hosted Gemma ----------

# Optional request fields the hosted Gemma may not accept. If Google answers 400 to one, it is dropped for
# the rest of this run and the request is sent again without it. The schema is always in the prompt too,
# so the reply has the same shape either way.
_OPTIONAL = ["responseJsonSchema", "responseMimeType", "thinkingConfig", "systemInstruction"]
_HINTS = {"JSON mode": ["responseMimeType", "responseJsonSchema"], "hinking": ["thinkingConfig"],
          "eveloper instruction": ["systemInstruction"], "ystem instruction": ["systemInstruction"]}
_unsupported: set[str] = set()


def google_body(prompt: str, schema: dict, system: str | None, images: list[bytes] | None,
                temperature: float) -> dict:
    text = (f"{prompt}\n\nReply with only a JSON object (no other text) that matches this JSON schema:\n"
            f"{json.dumps(schema)}")
    parts = [{"text": text}]
    for b in images or []:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}})
    gen = {"temperature": temperature, "responseMimeType": "application/json", "responseJsonSchema": schema,
           "thinkingConfig": {"thinkingLevel": "minimal"}}
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {k: v for k, v in gen.items() if k not in _unsupported}}
    if system and "systemInstruction" not in _unsupported:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    return body


def drop_for(error: str, left: list[str]) -> list[str]:
    """Which optional fields to stop sending after a 400: the ones the error names, else all of them."""
    named = [f for f in left if f in error]
    for hint, fields in _HINTS.items():
        if hint in error:
            named += [f for f in fields if f in left]
    return named or left


async def _google(prompt, schema, system, images, temperature, timeout) -> tuple[str, int | None, int | None]:
    if not config.GOOGLE_API_KEY:
        raise GemmaError("GOOGLE_API_KEY is not set")
    url = f"{config.GOOGLE_API_URL}/models/{config.GOOGLE_GEMMA_MODEL}:generateContent"
    headers = {"x-goog-api-key": config.GOOGLE_API_KEY}  # a header, so the key never lands in a URL or log
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            r = await client.post(url, json=google_body(prompt, schema, system, images, temperature), headers=headers)
            if r.status_code == 400:
                left = [f for f in _OPTIONAL if f not in _unsupported]
                if left:
                    _unsupported.update(drop_for(r.text, left))
                    continue
            if r.status_code == 429:
                raise GemmaError("Google's Gemma quota is used up for now")
            r.raise_for_status()
            break
    data = r.json()
    cands = data.get("candidates") or []
    parts = (cands[0].get("content") or {}).get("parts", []) if cands else []
    content = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    usage = data.get("usageMetadata") or {}
    return content, usage.get("promptTokenCount"), usage.get("candidatesTokenCount")


async def available() -> bool:
    if config.GEMMA_BACKEND == "google":
        return bool(config.GOOGLE_API_KEY)  # no test call: it would spend quota on every health check
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{config.OLLAMA_URL}/api/tags")
            return any(m["name"] == config.GEMMA_MODEL for m in r.json().get("models", []))
    except Exception:
        return False
