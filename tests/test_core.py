"""End-to-end test of the core loop with Gemma mocked out."""
import io
import os
import time

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from basecamp.app import gemma, main, quests, verify

client = TestClient(main.app)
LAT, LNG = 38.7628, -93.7360


def photo(color=(200, 40, 30), sharp=True) -> bytes:
    img = Image.new("RGB", (800, 600), (90, 140, 70))
    d = ImageDraw.Draw(img)
    if sharp:
        for i in range(0, 800, 20):
            d.line([(i, 0), (800 - i, 600)], fill=color, width=3)
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def new_quest(difficulty="easy"):
    place = client.post("/api/places/here", json={"lat": LAT, "lng": LNG}).json()
    q = client.post("/api/quests", json={"place_id": place["id"], "difficulty": difficulty, "tz": "America/Chicago"}).json()
    return client.post(f"/api/quests/{q['id']}/start", json={"started_at": time.time()}).json()


def upload(q, task_id, lat=LAT, lng=LNG, data=None, taken_at=None):
    return client.post(f"/api/quests/{q['id']}/captures", data={
        "task_id": task_id, "client_id": os.urandom(6).hex(), "taken_at": str(taken_at or time.time() * 1000),
        "lat": str(lat), "lng": str(lng), "accuracy": "10", "source": "camera"},
        files={"photo": ("p.jpg", data or photo(), "image/jpeg")}).json()


def test_fallback_quest_has_right_shape():
    for diff, n in quests.TASK_COUNTS.items():
        q = new_quest(diff)
        assert q["source"] == "fallback"
        assert len(q["tasks"]) == n
        assert q["started_at"] and q["deadline_at"] > q["started_at"]
        if diff == "hard":
            assert q["tasks"][-1]["kind"] == "tracker" and q["skips_left"] == 1


def test_capture_pending_when_gemma_down_then_passes(monkeypatch):
    q = new_quest()
    cap = upload(q, "t1")
    assert cap["status"] == "pending", cap

    async def fake_chat(*a, **k):
        return {"match": True, "confidence": 0.9, "what_i_see": "a red leaf", "reason": "clear"}
    monkeypatch.setattr(gemma, "chat", fake_chat)
    assert client.post("/api/captures/retry-pending").json()["retried"] >= 1
    q2 = client.get(f"/api/quests/{q['id']}").json()
    assert q2["tasks"][0]["status"] == "pass"


def test_wrong_place_fails():
    q = new_quest()
    cap = upload(q, "t1", lat=LAT + 0.05)
    assert cap["status"] == "fail" and "m from" in cap["reason"]


def test_outside_time_window_fails():
    q = new_quest()
    cap = upload(q, "t1", taken_at=(time.time() - 3600) * 1000)
    assert cap["status"] == "fail" and "time window" in cap["reason"]


def test_blurry_photo_asks_for_retake():
    q = new_quest()
    cap = upload(q, "t1", data=photo(sharp=False))
    assert cap["status"] == "retake"


def test_unsure_then_self_confirm_and_finish(monkeypatch):
    async def unsure(*a, **k):
        return {"match": True, "confidence": 0.3, "what_i_see": "maybe a leaf", "reason": "dim"}
    monkeypatch.setattr(gemma, "chat", unsure)
    q = new_quest()
    cap = upload(q, "t1")
    assert cap["status"] == "unsure"
    assert client.post(f"/api/captures/{cap['id']}/self-confirm").json()["status"] == "self"
    done = client.post(f"/api/quests/{q['id']}/finish", json={"note": "lovely", "screen_seconds": 42}).json()
    assert done["status"] == "done" and done["points"] == 5 and done["screen_seconds"] == 42


def test_exif_is_stripped():
    img = Image.new("RGB", (100, 100))
    exif = Image.Exif()
    exif[0x8825] = {2: (38.0, 45.0, 0.0)}  # GPSInfo
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    cleaned, _ = verify.clean_image(buf.getvalue())
    assert not Image.open(io.BytesIO(cleaned)).getexif()


def test_tracker_decision():
    assert verify.decide_tracker({"readable": True, "date": "2026-10-11", "distance_mi": 2.0}, {"distance_mi": 1.5}, "2026-10-11")[0] == "pass"
    assert verify.decide_tracker({"readable": True, "date": "2026-10-10", "distance_mi": 2.0}, {"distance_mi": 1.5}, "2026-10-11")[0] == "fail"
    assert verify.decide_tracker({"readable": False}, {"distance_mi": 1.5}, "2026-10-11")[0] == "unsure"


def test_invented_feature_is_dropped():
    place = {"name": "Maple Park", "type": "park", "description": "", "features": [], "lat": LAT, "lng": LNG}
    ctx = {"difficulty": "easy", "season": "fall", "distance_tier": "nearby", "recent_task_types": [], "minutes_until_sunset": 300}
    raw = {"title": "x", "spoken_intro": "y", "deadline_minutes": 20, "tasks": [
        {"prompt": "Photograph the lake", "verify": "a lake", "category": "water", "type_label": "lake"}]}
    q, notes = quests.enforce_rules(raw, ctx, place)
    assert all("lake" not in t["prompt"].lower() for t in q["tasks"])
    assert any("invented" in n for n in notes)
