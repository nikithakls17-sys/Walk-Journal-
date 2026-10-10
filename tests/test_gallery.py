import importlib.util
import io
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from basecamp.app import gallery_client, gemma
from tests.test_core import client, new_quest, photo, upload

os.environ["DATA_DIR"] = os.environ.get("DATA_DIR") or tempfile.mkdtemp()
spec = importlib.util.spec_from_file_location("sky_gallery", Path(__file__).parents[1] / "gallery" / "app.py")
sky = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sky)
gal = TestClient(sky.app)


def jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (2400, 1600), (150, 200, 230))
    exif = Image.Exif()
    exif[0x8825] = {2: (38.0, 45.0, 0.0)}
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def post(**kw):
    data = {"city": "Warrensburg, MO", "date": "2026-10-11", "local_time": "18:40", "season": "fall"} | kw
    return gal.post("/api/skies", data=data, files={"image": ("s.jpg", jpeg_with_gps(), "image/jpeg")},
                    headers={"x-forwarded-for": data.pop("ip", "1.1.1.1")})


def test_upload_strips_exif_resizes_and_lists():
    r = post(ip="2.2.2.2")
    assert r.status_code == 200
    img = Image.open(io.BytesIO(gal.get(r.json()["url"]).content))
    assert max(img.size) <= 1280 and not img.getexif()
    assert any(s["id"] == r.json()["id"] for s in gal.get("/api/skies").json())
    assert "Warrensburg" in gal.get("/").text


def test_rate_limit_per_ip():
    codes = [post(ip="3.3.3.3").status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_rejects_non_images_and_bad_season():
    assert gal.post("/api/skies", data={"city": "x", "date": "d", "local_time": "t", "season": "fall"},
                    files={"image": ("a.jpg", b"nope", "image/jpeg")}, headers={"x-forwarded-for": "4.4.4.4"}).status_code == 400
    assert post(season="monsoon", ip="5.5.5.5").status_code == 400


def _sky_capture(monkeypatch, moderation):
    async def chat(prompt, schema, **k):
        if k.get("operation") == "moderate_sky":
            return moderation
        return {"match": True, "confidence": 0.9, "what_i_see": "sky", "reason": "ok"}
    monkeypatch.setattr(gemma, "chat", chat)
    q = new_quest()
    for t in q["tasks"]:
        t["category"] = "sky"
    from basecamp.app import store
    store.update_quest(q["id"], tasks=q["tasks"])
    return upload(q, "t1", data=photo())


def test_share_blocked_when_people_in_photo(monkeypatch):
    cap = _sky_capture(monkeypatch, {"mostly_sky": True, "people_or_faces": True, "readable_text": False, "reason": "a person"})
    r = client.post(f"/api/captures/{cap['id']}/share", json={"city": "Warrensburg"})
    assert r.status_code == 422 and "person" in r.json()["detail"]


def test_share_sends_only_city_date_time(monkeypatch):
    cap = _sky_capture(monkeypatch, {"mostly_sky": True, "people_or_faces": False, "readable_text": False, "reason": "sky"})
    sent = {}

    async def fake_share(jpeg, city, taken, season):
        sent.update(city=city, season=season, taken=taken)
        return {"id": "x", "url": "/skies/x.jpg", "gallery": "https://example"}
    monkeypatch.setattr(gallery_client, "share", fake_share)
    r = client.post(f"/api/captures/{cap['id']}/share", json={"city": "Warrensburg"})
    assert r.status_code == 200 and sent["city"] == "Warrensburg" and sent["season"]
