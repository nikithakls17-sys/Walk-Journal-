import asyncio
import json

import httpx
import sentry_sdk
from sentry_sdk.transport import Transport

from basecamp.app import config, gemma, tracing, verify


class Capture(Transport):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.events, self.spans = [], []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.type == "transaction":
                self.events.append(item.payload.json)
            elif item.type == "span":  # SDK 2.7x streams gen_ai spans as standalone v2 span items
                for sp in item.payload.json.get("items", []):
                    self.spans.append({"op": sp["attributes"]["sentry.op"]["value"],
                                       "data": {k: v["value"] for k, v in sp["attributes"].items()}})


def test_gemma_calls_become_gen_ai_spans(monkeypatch):
    transport = Capture()
    sentry_sdk.init(dsn="https://key@o0.ingest.sentry.io/0", transport=transport, traces_sample_rate=1.0)
    monkeypatch.setattr(tracing, "_enabled", True)

    async def fake_post(self, url, json=None, **k):
        body = {"message": {"content": '{"match": true, "confidence": 0.92, "what_i_see": "a red leaf", "reason": "clear"}'},
                "prompt_eval_count": 812, "eval_count": 41}
        return httpx.Response(200, content=__import__("json").dumps(body), request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    task = {"prompt": "Find a red leaf", "verify": "a leaf that is mostly red"}
    result = asyncio.run(verify.check_content(b"\xff\xd8fakejpeg", task))
    assert verify.decide_content(result) == "pass"
    sentry_sdk.flush()

    spans = [s for e in transport.events for s in e.get("spans", [])] + transport.spans
    roots = [e["contexts"]["trace"] for e in transport.events]
    chat = next(s for s in spans if s["op"] == "gen_ai.chat")
    agent = next(r for r in roots if r["op"] == "gen_ai.invoke_agent")
    assert chat["data"]["gen_ai.request.model"] == config.GEMMA_MODEL
    assert chat["data"]["gen_ai.usage.input_tokens"] == 812
    assert chat["data"]["gen_ai.usage.output_tokens"] == 41
    assert chat["data"]["walkjournal.outcome"] == "photo_match"
    assert agent["data"]["gen_ai.agent.name"] == "walk-journal.verify_photo"
    # privacy: no prompt text or image goes to Sentry
    blob = json.dumps([transport.events, transport.spans])
    assert "red leaf" not in blob and "fakejpeg" not in blob
    sentry_sdk.init(dsn=None)
