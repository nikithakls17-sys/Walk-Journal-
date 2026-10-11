"""Choosing a photo from the gallery: EXIF time + GPS make it fully verified; without them it's
"time/place not confirmed", which completes the task but never counts toward badges."""
import io
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from basecamp.app import badges, gemma, verify
from tests.test_core import LAT, LNG, client, new_quest

TZ = ZoneInfo("America/Chicago")


def dms(x: float) -> tuple:
    x = abs(x)
    d = int(x)
    m = int((x - d) * 60)
    return (float(d), float(m), round((x - d - m / 60) * 3600, 4))


def gallery_jpeg(when: float | None = None, lat: float | None = None, lng: float | None = None) -> bytes:
    img = Image.new("RGB", (800, 600), (90, 140, 70))
    d = ImageDraw.Draw(img)
    for i in range(0, 800, 20):
        d.line([(i, 0), (800 - i, 600)], fill=(200, 40, 30), width=3)
    exif = Image.Exif()
    if when is not None:
        exif.get_ifd(verify.EXIF_IFD)[verify.DATETIME_ORIGINAL] = datetime.fromtimestamp(when, TZ).strftime("%Y:%m:%d %H:%M:%S")
    if lat is not None:
        exif[verify.GPS_IFD] = {1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lng >= 0 else "W", 4: dms(lng)}
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def pick(q, task_id, data, phone_lat=None):
    form = {"task_id": task_id, "client_id": os.urandom(6).hex(), "taken_at": str(time.time() * 1000), "source": "gallery"}
    if phone_lat is not None:  # where the phone is now must NOT be used for a gallery photo
        form |= {"lat": str(phone_lat), "lng": str(LNG), "accuracy": "5"}
    return client.post(f"/api/quests/{q['id']}/captures", data=form, files={"photo": ("g.jpg", data, "image/jpeg")}).json()


def gemma_answers(monkeypatch, match=True, confidence=0.9):
    async def chat(*a, **k):
        return {"match": match, "confidence": confidence, "what_i_see": "a red leaf", "reason": "clear"}
    monkeypatch.setattr(gemma, "chat", chat)


def test_read_exif_time_gps_and_offset():
    now = time.time()
    meta = verify.read_exif(gallery_jpeg(now, LAT, LNG), TZ)
    assert abs(meta["taken_at"] - now) < 1.5
    assert abs(meta["lat"] - LAT) < 1e-4 and abs(meta["lng"] - LNG) < 1e-4
    assert meta["accuracy"] == verify.DEFAULT_EXIF_GPS_ACCURACY_M
    assert verify.read_exif(gallery_jpeg(), TZ) == {"taken_at": None, "lat": None, "lng": None, "accuracy": None}


def test_gallery_photo_with_exif_in_window_and_place_is_fully_verified(monkeypatch):
    gemma_answers(monkeypatch)
    q = new_quest()
    cap = pick(q, "t1", gallery_jpeg(time.time() - 30, LAT, LNG))
    assert cap["status"] == "pass" and cap["proof"] == "exif" and cap["place_ok"] == 1 and cap["time_ok"] == 1
    assert cap["label"] == "verified (gallery)"
    from tests.test_core import upload
    upload(q, "t2")
    upload(q, "t3")
    done = client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 30}).json()
    assert done["completed"] and done["points"] == 30
    assert any(qq["id"] == q["id"] and t["id"] == "t1" for qq, t, _ in badges._verified())


def test_gallery_photo_taken_before_quest_fails_time(monkeypatch):
    gemma_answers(monkeypatch)
    q = new_quest()
    cap = pick(q, "t1", gallery_jpeg(time.time() - 3 * 3600, LAT, LNG))
    assert cap["status"] == "fail" and cap["time_ok"] == 0 and "gallery photo" in cap["reason"]


def test_gallery_photo_exif_gps_is_used_not_the_phones_location(monkeypatch):
    gemma_answers(monkeypatch)
    q = new_quest()
    # The phone is standing at the place, but the photo's own GPS says 5 km away.
    cap = pick(q, "t1", gallery_jpeg(time.time(), LAT + 0.045, LNG), phone_lat=LAT)
    assert cap["status"] == "fail" and cap["place_ok"] == 0


def test_gallery_photo_without_exif_is_unconfirmed_and_never_counts_for_badges(monkeypatch):
    gemma_answers(monkeypatch)
    q = new_quest()
    cap = pick(q, "t1", gallery_jpeg(), phone_lat=LAT)
    assert cap["status"] == "unconfirmed" and cap["proof"] == "none"
    assert cap["place_ok"] is None and cap["time_ok"] is None
    assert "removed this photo's time and location" in cap["reason"] and "Take photo" in cap["reason"]
    assert cap["label"] == "time/place not confirmed"
    view = client.get(f"/api/quests/{q['id']}").json()
    assert view["tasks"][0]["status"] == "unconfirmed"
    from tests.test_core import upload
    upload(q, "t2")
    upload(q, "t3")
    done = client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 30}).json()
    assert done["completed"] and done["points"] == 25  # the ◐ task completes it like a self-confirm (5)
    verified_tasks = {t["id"] for qq, t, _ in badges._verified() if qq["id"] == q["id"]}
    assert verified_tasks == {"t2", "t3"}  # the ◐ task never counts toward badges


def test_gallery_photo_with_time_but_no_gps_is_unconfirmed(monkeypatch):
    gemma_answers(monkeypatch)
    q = new_quest()
    assert pick(q, "t1", gallery_jpeg(time.time()))["status"] == "unconfirmed"


def test_unconfirmed_photo_still_gets_gemma_content_check(monkeypatch):
    gemma_answers(monkeypatch, match=False)
    q = new_quest()
    cap = pick(q, "t1", gallery_jpeg())
    assert cap["status"] == "fail" and "I see" in cap["reason"]


def test_unconfirmed_pending_then_retry_stays_unconfirmed(monkeypatch):
    q = new_quest()
    cap = pick(q, "t1", gallery_jpeg())  # Gemma unreachable in tests
    assert cap["status"] == "pending"
    gemma_answers(monkeypatch)
    client.post("/api/captures/retry-pending")
    assert client.get(f"/api/quests/{q['id']}").json()["tasks"][0]["status"] == "unconfirmed"


def test_camera_photo_beats_earlier_unconfirmed_one(monkeypatch):
    from tests.test_core import upload
    gemma_answers(monkeypatch)
    q = new_quest()
    pick(q, "t1", gallery_jpeg())
    upload(q, "t1")
    assert client.get(f"/api/quests/{q['id']}").json()["tasks"][0]["status"] == "pass"


def test_stored_gallery_photo_has_no_exif(monkeypatch):
    gemma_answers(monkeypatch)
    q = new_quest()
    cap = pick(q, "t1", gallery_jpeg(time.time(), LAT, LNG))
    stored = client.get(cap["photo_url"]).content
    assert not Image.open(io.BytesIO(stored)).getexif()


def test_auto_read_is_off_by_default_and_remembered():
    assert client.get("/api/settings").json()["auto_read"] is False
    s = {"home_lat": LAT, "home_lng": LNG, "home_city": "Warrensburg, MO", "units": "mi", "auto_read": True}
    client.post("/api/settings", json=s)
    assert client.get("/api/settings").json()["auto_read"] is True
    client.post("/api/settings", json=s | {"auto_read": False})
    assert client.get("/api/settings").json()["auto_read"] is False
