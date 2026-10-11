from basecamp.app import badges, gemma, tts
from tests.test_core import client, new_quest, upload


def test_audio_without_key_is_503_so_phone_falls_back():
    q = new_quest()
    r = client.get(f"/api/quests/{q['id']}/audio")
    assert r.status_code == 503


def test_speech_text_reads_tasks():
    q = {"title": "Golden Loop", "spoken_intro": "Hello.", "tasks": [{"prompt": "Find a red leaf"}]}
    assert "1. Find a red leaf." in tts.speech_text(q)


def test_finish_awards_badges_and_journal_lists_entry(monkeypatch):
    async def ok(*a, **k):
        return {"match": True, "confidence": 0.95, "what_i_see": "yes", "reason": "clear"}
    monkeypatch.setattr(gemma, "chat", ok)
    q = new_quest()
    for t in ("t1", "t2", "t3"):
        upload(q, t)
    done = client.post(f"/api/quests/{q['id']}/finish", json={"note": "Saw a heron", "mood": "calm", "screen_seconds": 40}).json()
    codes = {b["code"] for b in done["new_badges"]} | set(badges.earned())
    assert "first_grass" in codes and "screen_light" in codes
    j = client.get("/api/journal").json()
    entry = next(e for e in j["entries"] if e["id"] == q["id"])
    assert entry["note"] == "Saw a heron" and entry["tasks"][0]["status"] == "pass"
    assert j["week"]["this_week"] >= 1 and j["week"]["goal"] == 3
    assert len(client.get("/api/badges").json()["badges"]) == len(badges.BADGES)


def test_self_confirmed_does_not_count_for_screen_light(monkeypatch):
    async def unsure(*a, **k):
        return {"match": True, "confidence": 0.2, "what_i_see": "?", "reason": "?"}
    monkeypatch.setattr(gemma, "chat", unsure)
    q = new_quest()
    cap = upload(q, "t1")
    client.post(f"/api/captures/{cap['id']}/self-confirm")
    assert not any(qq["id"] == q["id"] for qq, _, _ in badges._verified())
