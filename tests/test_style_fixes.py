"""Fixes from tester data: Gemma stays loaded between requests, and 2 of 3 now completes a walk."""
import asyncio
import json

import httpx

from basecamp.app import gemma, store
from tests.test_core import client, new_quest, upload


def test_gemma_requests_ask_ollama_to_stay_loaded_for_2_hours(monkeypatch):
    sent = {}

    async def fake_post(self, url, json=None, **k):
        sent.update(json)
        body = {"message": {"content": '{"ok": true}'}, "prompt_eval_count": 1, "eval_count": 1}
        return httpx.Response(200, content=__import__("json").dumps(body), request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    asyncio.run(gemma.chat("hi", {"type": "object", "properties": {"ok": {"type": "boolean"}}}))
    assert sent["keep_alive"] == "2h" and sent["think"] is False


def test_two_of_three_now_completes_a_walk(monkeypatch):
    async def ok(*a, **k):
        return {"match": True, "confidence": 0.9, "what_i_see": "yes", "reason": "ok"}
    monkeypatch.setattr(gemma, "chat", ok)
    q = new_quest("easy")
    upload(q, "t1")
    upload(q, "t2")
    done = client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 20}).json()
    assert done["progress"]["needed"] == 2 and done["completed"] and not done["partial"] and done["points"] == 20


def test_labels_follow_the_rule_without_rewriting_rows(monkeypatch):
    async def ok(*a, **k):
        return {"match": True, "confidence": 0.9, "what_i_see": "yes", "reason": "ok"}
    monkeypatch.setattr(gemma, "chat", ok)
    q = new_quest("easy")
    upload(q, "t1")
    upload(q, "t2")
    client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 20})
    row_before = store.get_quest(q["id"])
    monkeypatch.setattr(store, "PASS_SHARE", 1.0)  # a stricter rule: 3 of 3 needed
    assert store.progress(row_before)["completed"] is False
    assert store.get_quest(q["id"]) == row_before  # judging by a rule never touches the stored row


def test_rescue_endpoint_replays_start_and_finish_from_a_form():
    import time as _t
    q = client.post("/api/quests", json={"place_id": client.post("/api/places/here", json={"lat": 38.76, "lng": -93.74}).json()["id"],
                                         "difficulty": "easy"}).json()
    started = _t.time()
    r = client.post("/api/rescue/job", data={"job": json.dumps({"kind": "start", "questId": q["id"], "body": {"started_at": started}})})
    assert r.status_code == 200 and r.json()["started_at"] == started
    r = client.post("/api/rescue/job", data={"job": json.dumps({"kind": "finish", "questId": q["id"],
                                                                "body": {"note": "rescued", "screen_seconds": 9}})})
    assert r.status_code == 200 and r.json()["note"] == "rescued"
    assert client.post("/api/rescue/job", data={"job": "{not json"}).status_code == 400
    assert client.post("/api/rescue/job", data={"job": json.dumps({"kind": "start", "questId": "nope"})}).status_code == 404
