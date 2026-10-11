"""Notifications: reminders, the single live quest notification, privacy guardrails, VAPID keys."""
import asyncio
import base64
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from basecamp.app import config, db, gemma, push, reminders
from tests.test_core import client, new_quest, upload

TZ = ZoneInfo("America/Chicago")
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fake_subscription() -> dict:
    """A real-looking browser subscription with valid encryption keys."""
    k = ec.generate_private_key(ec.SECP256R1())
    p256dh = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": f"https://push.example.com/{os.urandom(4).hex()}", "keys": {"p256dh": b64(p256dh), "auth": b64(os.urandom(16))}}


@pytest.fixture
def vapid(monkeypatch):
    public, private = push.generate_keys()
    monkeypatch.setattr(config, "VAPID_PUBLIC_KEY", public)
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", private)
    return public, private


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def send_all(payload):
        push.assert_private(payload)
        out.append(payload)
        return {"ok": 1}
    monkeypatch.setattr(push, "send_all", send_all)
    return out


def at(hhmm: str, day="2026-10-11") -> datetime:
    return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


# ---------- settings ----------

def test_notifications_are_off_by_default_with_5pm_daily_time():
    db.set_setting("settings", None)
    s = client.get("/api/settings").json()
    assert (s["notify_daily"], s["notify_journal"], s["notify_quest"]) == (False, False, False)
    assert s["daily_time"] == "17:00" and s["journal_time"] == "20:00"


def test_bad_reminder_time_is_rejected():
    r = client.post("/api/settings", json={"daily_time": "5pm"})
    assert r.status_code == 400


# ---------- reminders ----------

def test_daily_reminder_fires_once_in_the_hour_after_the_chosen_time():
    s = {"notify_daily": True, "daily_time": "17:00"}
    assert reminders.due(s, at("16:59"), {}) == []
    assert reminders.due(s, at("17:00"), {}) == ["daily"]
    assert reminders.due(s, at("17:45"), {}) == ["daily"]
    assert reminders.due(s, at("18:01"), {}) == []  # turned on at 9 PM? no instant reminder
    assert reminders.due(s, at("17:10"), {"daily": "2026-10-11"}) == []  # already sent today
    assert reminders.due(s, at("17:10"), {"daily": "2026-10-10"}) == ["daily"]
    assert reminders.due({"notify_daily": False}, at("17:10"), {}) == []


def test_journal_reminder_only_if_not_journaled_today(monkeypatch):
    s = {"notify_journal": True, "journal_time": "20:00"}
    monkeypatch.setattr(reminders, "journaled_today", lambda today, tz: False)
    assert reminders.due(s, at("20:15"), {}) == ["journal"]
    monkeypatch.setattr(reminders, "journaled_today", lambda today, tz: True)
    assert reminders.due(s, at("20:15"), {}) == []


def test_journaled_today_means_a_walk_finished_today_with_a_note():
    q = new_quest()
    client.post(f"/api/quests/{q['id']}/finish", json={"note": "Crunchy leaves", "screen_seconds": 10})
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    assert reminders.journaled_today(today, TZ)
    assert not reminders.journaled_today("2000-01-01", TZ)


def test_tick_sends_each_reminder_once_and_remembers(sent):
    db.set_setting("reminders_sent", {})
    db.set_setting("settings", {"notify_daily": True, "daily_time": "17:00", "tz": "America/Chicago"})
    assert asyncio.run(reminders.tick(at("17:02"))) == ["daily"]
    assert asyncio.run(reminders.tick(at("17:03"))) == []
    assert len(sent) == 1 and sent[0]["tag"] == "wj-daily" and sent[0]["url"] == "/#home"


# ---------- quest notification ----------

def quest_like(statuses):
    return {"title": "Golden Loop", "tasks": [
        {"id": f"t{i}", "kind": "photo", "prompt": f"Task {i}", "status": st} for i, st in enumerate(statuses, 1)]}


def test_quest_payload_counts_done_and_offers_take_photo_for_next_task():
    p = push.quest_payload(quest_like(["pass", "unconfirmed", "todo", "fail", "todo"]))
    assert p["title"] == "Golden Loop: 2 of 5 done"
    assert p["tag"] == push.QUEST_TAG and p["renotify"] is False
    assert p["actions"] == [{"action": "snap", "title": "Take photo"}]
    assert p["action_urls"]["snap"] == "/#snap/t3" and p["next_task"] == "t3"
    assert p["body"].splitlines()[0] == "✓ Task 1" and p["body"].splitlines()[2] == "○ Task 3"


def test_quest_payload_when_all_done_has_no_camera_action():
    p = push.quest_payload(quest_like(["pass", "self", "skipped"]))
    assert "all 3 done" in p["title"] and p["actions"] == []


def test_notifications_never_carry_photos_or_locations():
    with pytest.raises(push.PushError):
        push.assert_private({"title": "x", "data": {"lat": 38.7}})
    with pytest.raises(push.PushError):
        push.assert_private({"title": "x", "body": "/photos/abc.jpg"})
    q = new_quest()
    p = push.quest_payload(client.get(f"/api/quests/{q['id']}").json())
    text = str(p)
    assert "/photos/" not in text and "38.76" not in text and "-93.7" not in text


def test_verified_photo_pushes_a_quest_update_when_enabled(monkeypatch, vapid, sent):
    async def ok(*a, **k):
        return {"match": True, "confidence": 0.9, "what_i_see": "a leaf", "reason": "clear"}
    monkeypatch.setattr(gemma, "chat", ok)
    db.set_setting("settings", {"notify_quest": True})
    q = new_quest()
    upload(q, "t1")
    assert sent and sent[-1]["tag"] == push.QUEST_TAG and sent[-1]["title"].endswith("1 of 3 done")
    sent.clear()
    db.set_setting("settings", {"notify_quest": False})
    upload(q, "t2")
    assert sent == []


# ---------- subscriptions + sending ----------

def test_push_key_needs_vapid(vapid):
    assert client.get("/api/push/key").json()["key"] == vapid[0]


def test_push_key_without_vapid_is_503():
    assert client.get("/api/push/key").status_code == 503


def test_subscribe_and_unsubscribe():
    sub = fake_subscription()
    assert client.post("/api/push/subscribe", json=sub).status_code == 200
    assert sub["endpoint"] in [s["endpoint"] for s in push.subscriptions()]
    assert client.post("/api/push/subscribe", json={"endpoint": "http://evil", "keys": {}}).status_code == 400
    client.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]})
    assert sub["endpoint"] not in [s["endpoint"] for s in push.subscriptions()]


def test_expired_subscriptions_are_removed(monkeypatch, vapid):
    gone, live = fake_subscription(), fake_subscription()
    push.save_subscription(gone)
    push.save_subscription(live)
    monkeypatch.setattr(push, "_send_one", lambda sub, payload: "gone" if sub["endpoint"] == gone["endpoint"] else "ok")
    asyncio.run(push.send_all(push.reminder_payload("daily")))
    endpoints = [s["endpoint"] for s in push.subscriptions()]
    assert gone["endpoint"] not in endpoints and live["endpoint"] in endpoints
    push.remove_subscription(live["endpoint"])


def test_real_encryption_and_vapid_signing(vapid):
    """pywebpush encrypts for a real browser key and signs with our VAPID key (no network)."""
    from py_vapid import Vapid02  # what pywebpush uses for aes128gcm
    from pywebpush import WebPusher
    encoded = WebPusher(fake_subscription()).encode(b'{"title": "hi"}', "aes128gcm")
    assert encoded["body"] and len(encoded["body"]) > 16
    headers = Vapid02.from_string(private_key=vapid[1]).sign({"aud": "https://push.example.com", "sub": config.VAPID_SUBJECT})
    auth = headers["Authorization"]
    assert auth.startswith("vapid t=") and auth.endswith(f"k={vapid[0]}")  # signed by the key the phone was given


def test_write_keys_to_env_adds_once_and_keeps_other_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SERPAPI_KEY=abc\nVAPID_PUBLIC_KEY=\n", encoding="utf-8")
    assert push.write_keys_to_env(env) is True
    text = env.read_text(encoding="utf-8")
    assert "SERPAPI_KEY=abc" in text and text.count("VAPID_PUBLIC_KEY=") == 1
    assert [l for l in text.splitlines() if l.startswith("VAPID_PRIVATE_KEY=")][0] != "VAPID_PRIVATE_KEY="
    assert push.write_keys_to_env(env) is False and env.read_text(encoding="utf-8") == text
