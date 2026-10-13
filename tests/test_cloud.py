"""Cloud mode (public beta): private journals per phone, daily limits, feedback, export/delete, hosted Gemma.
Local mode must stay exactly as it was."""
import asyncio
import io
import json
import os
import time
import zipfile

import httpx
import pytest

from basecamp.app import config, db, gemma, limits, main
from tests.test_core import LAT, LNG, client, photo


def phone():
    """Headers for a fresh phone on its own network."""
    return {"X-WJ-User": os.urandom(12).hex(), "X-Forwarded-For": f"10.{os.urandom(1)[0]}.{os.urandom(1)[0]}.7"}


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr(config, "CLOUD", True)
    monkeypatch.setattr(config, "MODE", "cloud")
    monkeypatch.setattr(config, "GEMMA_BACKEND", "google")
    yield


def make_quest(h, mode="from_here"):
    q = client.post("/api/quests", headers=h, json={"mode": mode, "lat": LAT, "lng": LNG, "difficulty": "easy",
                                                    "tz": "America/Chicago"})
    assert q.status_code == 200, q.text
    q = q.json()
    return client.post(f"/api/quests/{q['id']}/start", headers=h, json={"started_at": time.time()}).json()


def snap(h, q, task="t1"):
    return client.post(f"/api/quests/{q['id']}/captures", headers=h, data={
        "task_id": task, "client_id": os.urandom(6).hex(), "taken_at": str(time.time() * 1000),
        "lat": str(LAT), "lng": str(LNG), "accuracy": "10", "source": "camera"},
        files={"photo": ("p.jpg", photo(), "image/jpeg")})


# ---------- local mode is unchanged ----------

def test_local_mode_config_keeps_the_laptop_privacy_note():
    c = client.get("/api/config").json()
    assert c["cloud"] is False and c["backend"] == "ollama" and c["can_delete"] is False
    assert "never leave the laptop" in c["privacy"] and "Google" not in c["privacy"]


def test_local_mode_never_deletes_and_needs_no_phone_id():
    before = client.get("/api/journal").json()["entries"]
    r = client.delete("/api/my-data")
    assert r.status_code == 400 and "data folder" in r.json()["detail"]
    assert client.get("/api/journal").json()["entries"] == before


# ---------- cloud: every phone has its own private journal ----------

def test_cloud_needs_a_phone_id(cloud):
    assert client.get("/api/journal").status_code == 401
    assert client.get("/api/journal", headers={"X-WJ-User": "short"}).status_code == 401


def test_cloud_phones_never_see_each_other_or_the_laptop_walks(cloud, monkeypatch):
    monkeypatch.setattr(config, "CLOUD", False)
    local_q = make_quest({})  # a walk made in local mode (no owner)
    client.post(f"/api/quests/{local_q['id']}/finish", json={"note": "laptop walk"})
    monkeypatch.setattr(config, "CLOUD", True)

    a, b = phone(), phone()
    qa = make_quest(a)
    client.post(f"/api/quests/{qa['id']}/finish", headers=a, json={"note": "mine"})
    assert client.get(f"/api/quests/{qa['id']}", headers=b).status_code == 404
    assert client.get(f"/api/quests/{local_q['id']}", headers=a).status_code == 404
    assert snap(b, qa).status_code == 404
    assert client.post(f"/api/quests/{qa['id']}/finish", headers=b, json={}).status_code == 404
    ja = client.get("/api/journal", headers=a).json()["entries"]
    assert [e["id"] for e in ja] == [qa["id"]]
    assert client.get("/api/journal", headers=b).json()["entries"] == []
    assert client.get("/api/map", headers=b).json()["visited"] == []
    # and the laptop's own journal still has its walk, and not the phone's
    monkeypatch.setattr(config, "CLOUD", False)
    ids = {e["id"] for e in client.get("/api/journal").json()["entries"]}
    assert local_q["id"] in ids and qa["id"] not in ids


def test_cloud_settings_are_per_phone(cloud):
    a, b = phone(), phone()
    client.post("/api/settings", headers=a, json={"home_lat": 17.4, "home_lng": 78.35, "weekly_goal": 5})
    assert client.get("/api/settings", headers=a).json()["weekly_goal"] == 5
    assert client.get("/api/settings", headers=b).json()["weekly_goal"] == 3
    assert client.get("/api/badges", headers=b).json()["week"]["goal"] == 3


def test_cloud_config_shows_the_hosted_gemma_note_and_sharing_is_off(cloud):
    c = client.get("/api/config").json()
    assert c["cloud"] and c["beta"] and c["can_delete"] and c["gallery"] is False and c["push"] is False
    assert c["privacy"].startswith("Photos are checked by Google’s hosted Gemma.")
    assert c["limits"]["photos"]["per_user"] == limits.limits_for("photos")[0]
    h = phone()
    q = make_quest(h)
    cap = snap(h, q).json()
    assert client.post(f"/api/captures/{cap['id']}/share", headers=h, json={"city": "X"}).status_code == 404
    assert client.post("/api/rescue/job", data={"job": json.dumps({"kind": "start", "questId": q["id"]})}).status_code == 404


# ---------- cloud: daily limits ----------

def test_photo_limit_per_phone_has_a_friendly_message(cloud, monkeypatch):
    monkeypatch.setitem(limits.DEFAULTS, "photos", (2, 100, 10000))
    h = phone()
    q = make_quest(h)
    assert snap(h, q, "t1").status_code == 200
    assert snap(h, q, "t2").status_code == 200
    r = snap(h, q, "t3")
    assert r.status_code == 429
    msg = r.json()["detail"]
    assert "today's limit of 2 photo checks" in msg and "resets tomorrow" in msg
    other = phone()
    assert snap(other, make_quest(other), "t1").status_code == 200  # another phone is fine


def test_quest_limit_per_ip_catches_new_phone_ids(cloud, monkeypatch):
    monkeypatch.setitem(limits.DEFAULTS, "quests", (5, 2, 10000))
    ip = {"X-Forwarded-For": "203.0.113.9"}
    make_quest({**phone(), **ip})
    make_quest({**phone(), **ip})
    r = client.post("/api/quests", headers={**phone(), **ip}, json={"mode": "from_here", "lat": LAT, "lng": LNG})
    assert r.status_code == 429 and "on this network" in r.json()["detail"]


def test_whole_app_daily_cap(cloud, monkeypatch):
    with db.tx() as c:
        c.execute("DELETE FROM usage WHERE action='feedback'")  # test database only
    monkeypatch.setitem(limits.DEFAULTS, "feedback", (5, 5, 1))
    assert client.post("/api/feedback", headers=phone(), json={"message": "hi"}).status_code == 200
    r = client.post("/api/feedback", headers=phone(), json={"message": "hi again"})
    assert r.status_code == 429 and "come back tomorrow" in r.json()["detail"]


def test_local_mode_has_no_limits(monkeypatch):
    monkeypatch.setitem(limits.DEFAULTS, "feedback", (0, 0, 0))
    assert client.post("/api/feedback", json={"message": "from the laptop", "rating": 9}).status_code == 200


# ---------- feedback + admin page ----------

def test_feedback_validation_and_admin_page(cloud, monkeypatch):
    h = phone()
    assert client.post("/api/feedback", headers=h, json={"message": "  "}).status_code == 400
    assert client.post("/api/feedback", headers=h, json={"message": "x", "rating": 11}).status_code == 400
    assert client.post("/api/feedback", headers=h, json={"message": "x" * 2001}).status_code == 400
    assert client.post("/api/feedback", headers=h, json={"message": "<b>Loved</b> it", "rating": 8}).status_code == 200
    assert client.post("/api/feedback", headers=h, json={"rating": 6}).status_code == 200  # a rating alone is fine

    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    assert client.get("/admin/feedback").status_code == 404  # no password set: the page doesn't exist
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "s3cret-pass")
    assert client.get("/admin/feedback").status_code == 401
    assert client.get("/admin/feedback", auth=("me", "wrong")).status_code == 401
    page = client.get("/admin/feedback", auth=("anyone", "s3cret-pass"))
    assert page.status_code == 200 and "&lt;b&gt;Loved&lt;/b&gt; it" in page.text and "<b>Loved" not in page.text


# ---------- export + delete ----------

def test_export_has_walks_and_photos(cloud, monkeypatch):
    async def ok(*a, **k):
        return {"match": True, "confidence": 0.9, "what_i_see": "a leaf", "reason": "ok"}
    monkeypatch.setattr(gemma, "chat", ok)
    h = phone()
    q = make_quest(h)
    snap(h, q)
    client.post(f"/api/quests/{q['id']}/finish", headers=h, json={"note": "lovely", "mood": "calm"})
    r = client.get(f"/api/export?u={h['X-WJ-User']}")  # the phone downloads it as a plain link
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    data = json.loads(z.read("journal.json"))
    assert [w["note"] for w in data["walks"]] == ["lovely"]
    cap = data["walks"][0]["tasks"][0]["capture"]
    assert cap["photo_file"] in z.namelist()
    assert client.get("/api/export", headers=phone()).status_code == 200  # someone else's export is empty, not mine
    other = zipfile.ZipFile(io.BytesIO(client.get("/api/export", headers=phone()).content))
    assert json.loads(other.read("journal.json"))["walks"] == []


def test_delete_my_data_erases_only_this_phone(cloud):
    a, b = phone(), phone()
    qa, qb = make_quest(a), make_quest(b)
    cap = snap(a, qa).json()
    client.post("/api/feedback", headers=a, json={"message": "bye"})
    client.post("/api/settings", headers=a, json={"weekly_goal": 6})
    photo_path = config.PHOTO_DIR / cap["photo_url"].rsplit("/", 1)[-1]
    assert photo_path.exists()
    r = client.delete("/api/my-data", headers=a).json()
    assert r["deleted_walks"] == 1 and r["deleted_photos"] == 1
    assert not photo_path.exists()
    assert client.get(f"/api/quests/{qa['id']}", headers=a).status_code == 404
    assert client.get("/api/settings", headers=a).json()["weekly_goal"] == 3
    assert client.get(f"/api/quests/{qb['id']}", headers=b).status_code == 200  # the other phone is untouched


# ---------- hosted Gemma (Google AI Studio) ----------

def test_google_backend_sends_image_and_key_header_and_parses_fenced_json(cloud, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-key")
    gemma._unsupported.clear()
    seen = {}

    async def fake_post(self, url, json=None, headers=None, **k):
        seen.update(url=url, body=json, headers=headers)
        reply = {"candidates": [{"content": {"parts": [{"text": '```json\n{"match": true}\n```'}]}}],
                 "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}}
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    out = asyncio.run(gemma.chat("Is this a leaf?", {"type": "object"}, system="be kind", images=[b"\xff\xd8jpeg"]))
    assert out == {"match": True}
    assert seen["url"].endswith(f"/models/{config.GOOGLE_GEMMA_MODEL}:generateContent")
    assert seen["headers"]["x-goog-api-key"] == "test-key" and "key=" not in seen["url"]
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[1]["inline_data"]["mime_type"] == "image/jpeg"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "be kind"


def test_google_backend_drops_fields_the_model_rejects(cloud, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-key")
    gemma._unsupported.clear()
    bodies = []

    async def fake_post(self, url, json=None, headers=None, **k):
        bodies.append(json)
        if "responseMimeType" in json["generationConfig"]:
            return httpx.Response(400, text="JSON mode is not enabled for models/gemma",
                                  request=httpx.Request("POST", url))
        reply = {"candidates": [{"content": {"parts": [{"text": 'Sure! {"ok": true}'}]}}]}
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    assert asyncio.run(gemma.chat("hi", {"type": "object"})) == {"ok": True}
    assert len(bodies) == 2 and "responseMimeType" not in bodies[1]["generationConfig"]
    assert "JSON schema" in bodies[1]["contents"][0]["parts"][0]["text"]  # the schema is still in the prompt
    gemma._unsupported.clear()


def test_google_quota_error_leaves_photos_pending_not_lost(cloud, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-key")

    async def fake_post(self, url, json=None, headers=None, **k):
        return httpx.Response(429, text="quota", request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    h = phone()
    q = make_quest(h)
    assert snap(h, q).json()["status"] == "pending"


def test_health_reports_the_hosted_model_without_spending_quota(cloud, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "k")
    h = client.get("/api/health").json()
    assert h["gemma"] is True and h["model"] == config.GOOGLE_GEMMA_MODEL and h["mode"] == "cloud"
