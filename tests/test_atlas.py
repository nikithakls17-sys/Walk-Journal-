"""Cloud mode on Render's free plan: everything lives in MongoDB Atlas (a stand-in here, never the real cluster),
photos are small and EXIF-free, and local mode never touches Atlas."""
import io
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from PIL import Image
from pymongo import DeleteOne, ReplaceOne
from pymongo.errors import PyMongoError

from basecamp.app import atlas, config, db, main, photos, verify
from tests.test_cloud import make_quest, phone, snap
from tests.test_core import client

ROOT = Path(__file__).resolve().parents[1]


class FakeCollection:
    def __init__(self, owner):
        self.docs, self.owner = {}, owner

    def _check(self):
        if self.owner.down:
            raise PyMongoError("Atlas is down")

    def find(self, flt=None, projection=None):
        self._check()
        hide_id = projection and projection.get("_id") == 0
        return [{k: v for k, v in d.items() if not (hide_id and k == "_id")} for d in self.docs.values()]

    def find_one(self, flt, projection=None):
        self._check()
        return self.docs.get(flt["_id"])

    def replace_one(self, flt, doc, upsert=False):
        self._check()
        self.docs[flt["_id"]] = dict(doc)

    def delete_many(self, flt):
        self._check()
        for _id in flt["_id"]["$in"]:
            self.docs.pop(_id, None)

    def bulk_write(self, ops, ordered=True):
        self._check()
        for op in ops:
            if isinstance(op, ReplaceOne):
                self.docs[op._filter["_id"]] = dict(op._doc)
            elif isinstance(op, DeleteOne):
                self.docs.pop(op._filter["_id"], None)


class FakeAtlas:
    def __init__(self):
        self.collections, self.down = {}, False

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection(self))


@pytest.fixture
def cloud_atlas(monkeypatch):
    fake = FakeAtlas()
    monkeypatch.setattr(config, "CLOUD", True)
    monkeypatch.setattr(config, "MODE", "cloud")
    monkeypatch.setattr(config, "GEMMA_BACKEND", "google")
    monkeypatch.setattr(config, "ATLAS", True)
    monkeypatch.setattr(config, "DB_PATH", Path(tempfile.mkdtemp(prefix="wj-atlas-")) / "atlas-working-copy.db")
    atlas.use(fake)
    db.init()
    yield fake
    atlas.use(None)


def restart():
    """What a Render restart does: the working copy is gone, and the app rebuilds it from Atlas."""
    config.DB_PATH.unlink()
    db.init()


def test_walks_settings_and_limits_are_written_to_atlas(cloud_atlas):
    h = phone()
    q = make_quest(h)
    client.post("/api/settings", headers=h, json={"weekly_goal": 5})
    snap(h, q)
    a = cloud_atlas
    assert q["id"] in a["quests"].docs and a["quests"].docs[q["id"]]["user_id"]
    assert len(a["captures"].docs) == 1 and len(a["photos"].docs) == 1
    assert any(d["action"] == "photos" for d in a["usage"].docs.values())  # composite key rows mirror too
    assert any("weekly_goal" in d["value"] for d in a["settings"].docs.values())


def test_a_restart_rebuilds_everything_from_atlas(cloud_atlas):
    h = phone()
    q = make_quest(h)
    cap = snap(h, q).json()
    client.post("/api/settings", headers=h, json={"weekly_goal": 6})
    restart()
    again = client.get(f"/api/quests/{q['id']}", headers=h)
    assert again.status_code == 200 and again.json()["title"] == q["title"]
    assert client.get("/api/settings", headers=h).json()["weekly_goal"] == 6
    assert client.get(cap["photo_url"]).content == next(iter(cloud_atlas["photos"].docs.values()))["data"]
    assert client.get(f"/api/quests/{q['id']}", headers=phone()).status_code == 404  # still private after restart


def test_cloud_photos_live_in_atlas_not_on_the_disk(cloud_atlas):
    h = phone()
    cap = snap(h, make_quest(h)).json()
    name = cap["photo_url"].rsplit("/", 1)[-1]
    assert not (config.PHOTO_DIR / name).exists()
    doc = cloud_atlas["photos"].docs[name]
    img = Image.open(io.BytesIO(doc["data"]))
    assert max(img.size) <= photos.CLOUD_MAX_PX and doc["bytes"] <= photos.CLOUD_MAX_BYTES
    assert not img.getexif()
    r = client.get(cap["photo_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_delete_my_data_removes_it_from_atlas_too(cloud_atlas):
    a, b = phone(), phone()
    qa, qb = make_quest(a), make_quest(b)
    snap(a, qa)
    snap(b, qb)
    assert client.delete("/api/my-data", headers=a).json()["deleted_photos"] == 1
    assert qa["id"] not in cloud_atlas["quests"].docs and qb["id"] in cloud_atlas["quests"].docs
    assert len(cloud_atlas["photos"].docs) == 1 and len(cloud_atlas["captures"].docs) == 1
    restart()
    assert client.get(f"/api/quests/{qb['id']}", headers=b).status_code == 200


def test_export_reads_photos_from_atlas(cloud_atlas):
    import zipfile
    h = phone()
    q = make_quest(h)
    snap(h, q)
    client.post(f"/api/quests/{q['id']}/finish", headers=h, json={"note": "fresh air", "mood": "calm"})
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/export", headers=h).content))
    assert any(n.startswith("photos/") and n.endswith(".jpg") for n in z.namelist())


def test_an_atlas_outage_keeps_changes_and_sends_them_later(cloud_atlas):
    h = phone()
    cloud_atlas.down = True
    q = make_quest(h)  # the walker isn't blocked by the outage
    assert q["id"] not in cloud_atlas["quests"].docs
    cloud_atlas.down = False
    client.post("/api/settings", headers=h, json={"weekly_goal": 4})  # the next change carries the backlog
    assert q["id"] in cloud_atlas["quests"].docs


def test_no_rebuild_while_unsent_changes_wait_for_a_down_atlas(cloud_atlas):
    cloud_atlas.down = True
    make_quest(phone())
    with pytest.raises(RuntimeError):
        db.init()
    assert config.DB_PATH.exists()  # the working copy that still owes Atlas is kept
    cloud_atlas.down = False
    db.init()
    assert len(cloud_atlas["quests"].docs) == 1


def test_clean_image_shrinks_busy_photos_and_strips_exif():
    rnd = random.Random(1)
    img = Image.frombytes("RGB", (3000, 2000), bytes(rnd.getrandbits(8) for _ in range(3000 * 2000 * 3)))
    exif = Image.Exif()
    exif[0x0132] = "2026:10:19 08:00:00"
    exif[0x8825] = {1: "N", 2: (38.0, 45.0, 0.0)}
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    assert Image.open(io.BytesIO(buf.getvalue())).getexif()
    jpeg, small = verify.clean_image(buf.getvalue(), photos.CLOUD_MAX_PX, photos.CLOUD_MAX_BYTES)
    out = Image.open(io.BytesIO(jpeg))
    assert len(jpeg) <= photos.CLOUD_MAX_BYTES and max(out.size) <= 800 and not out.getexif()


def test_local_photos_keep_their_old_size():
    jpeg, img = photos.clean(_jpeg(2400, 1800))
    assert max(img.size) == 1600


def _jpeg(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 140, 70)).save(buf, "JPEG")
    return buf.getvalue()


def _config_in(env: dict) -> str:
    env = {**os.environ, "DATA_DIR": tempfile.mkdtemp(prefix="wj-cfg-"), "MONGODB_URI": "", "RENDER": "", **env}
    code = "from basecamp.app import config; print(config.ATLAS, config.DB_PATH.name)"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    return r.stdout.strip() or r.stderr.strip().splitlines()[-1]


def test_local_mode_never_uses_atlas_even_with_a_connection_string():
    assert _config_in({"WALKJOURNAL_MODE": "local", "MONGODB_URI": "mongodb://example"}) == "False walkjournal.db"


def test_cloud_mode_uses_a_separate_working_copy_so_a_laptop_journal_is_never_replaced():
    assert _config_in({"WALKJOURNAL_MODE": "cloud", "MONGODB_URI": "mongodb://example"}) == "True atlas-working-copy.db"


def test_cloud_mode_on_render_refuses_to_start_without_atlas():
    assert "needs MONGODB_URI" in _config_in({"WALKJOURNAL_MODE": "cloud", "RENDER": "true"})


def test_cloud_privacy_note_has_the_free_tier_warning():
    assert main.PRIVACY["google"] == ("Photos are checked by Google’s hosted Gemma. On the free tier, Google may use "
                                      "them to improve its products. Don’t photograph people or anything private.")


def test_blueprint_is_the_free_plan_with_no_disk():
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "plan: free" in text and "\n    disk:" not in text and "key: MONGODB_URI" in text


def test_the_app_says_the_free_server_is_waking_up():
    html = (ROOT / "basecamp/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "basecamp/static/app.js").read_text(encoding="utf-8")
    assert 'id="waking"' in html and "Waking up the trail" in html
    assert '$("#waking").hidden = false' in js and '$("#waking").hidden = true' in js
