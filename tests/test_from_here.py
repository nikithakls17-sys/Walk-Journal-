"""Walk from here: a walk around wherever you are, with no destination."""
import time

from basecamp.app import badges, gemma, quests
from tests.test_core import LAT, LNG, client, upload

M_PER_DEG_LAT = 111_195  # meters per degree of latitude


def north(meters: float) -> float:
    return LAT + meters / M_PER_DEG_LAT


def walk(difficulty="medium", tier="nearby"):
    q = client.post("/api/quests", json={"mode": "from_here", "lat": LAT, "lng": LNG, "difficulty": difficulty,
                                         "distance_tier": tier, "tz": "America/Chicago"}).json()
    return client.post(f"/api/quests/{q['id']}/start", json={"started_at": time.time()}).json()


def gemma_says_yes(monkeypatch):
    async def ok(*a, **k):
        return {"match": True, "confidence": 0.9, "what_i_see": "a leaf", "reason": "clear"}
    monkeypatch.setattr(gemma, "chat", ok)


def test_walk_from_here_has_no_place_specific_tasks():
    for diff, n in quests.TASK_COUNTS.items():
        q = walk(diff)
        assert q["mode"] == "from_here" and q["place"]["name"] == "Home walks"
        assert q["start"] == {"lat": LAT, "lng": LNG}
        assert len(q["tasks"]) == n
        assert not any(t["place_specific"] for t in q["tasks"])
        assert q["away_goal_m"] == (0 if diff == "easy" else 300)


def test_rules_drop_destinations_and_never_add_place_tasks():
    place = quests.home_place(LAT, LNG)
    ctx = {"mode": "from_here", "difficulty": "hard", "season": "fall", "distance_tier": "nearby",
           "recent_task_types": [], "minutes_until_sunset": 300}
    raw = {"title": "x", "spoken_intro": "y", "deadline_minutes": 90, "tasks": [
        {"prompt": "Walk to the park entrance", "verify": "a park sign", "category": "structure", "type_label": "park"},
        {"prompt": "Photograph the lake", "verify": "a lake", "category": "water", "type_label": "lake"},
        {"prompt": "Find a red leaf", "verify": "a red leaf", "category": "leaf", "type_label": "red leaf", "place_specific": True},
    ]}
    q, notes = quests.enforce_rules(raw, ctx, place)
    prompts = " ".join(t["prompt"].lower() for t in q["tasks"])
    assert "park" not in prompts and "lake" not in prompts
    assert not any(t["place_specific"] for t in q["tasks"])
    assert len(q["tasks"]) == 7 and q["tasks"][-1]["kind"] == "tracker"
    assert any(t["rare"] for t in q["tasks"])
    assert "added place-specific task" not in notes


def test_photo_within_one_mile_passes_beyond_fails(monkeypatch):
    gemma_says_yes(monkeypatch)
    q = walk("easy", "nearby")
    near = upload(q, "t1", lat=north(1200))  # 1.2 km: inside 1 mile
    assert near["status"] == "pass", near
    far = upload(q, "t2", lat=north(2200))   # 2.2 km: outside 1 mile
    assert far["status"] == "fail" and "1 mi" in far["reason"] and "started" in far["reason"]


def test_walk_run_allows_farther(monkeypatch):
    gemma_says_yes(monkeypatch)
    q = walk("easy", "walk_run")
    assert upload(q, "t1", lat=north(4000))["status"] == "pass"  # 4 km is inside 5 miles


def test_medium_needs_one_photo_300m_away(monkeypatch):
    gemma_says_yes(monkeypatch)
    q = walk("medium")
    upload(q, "t1", lat=north(50))
    mid = client.get(f"/api/quests/{q['id']}").json()
    assert not mid["away_met"] and 40 <= mid["farthest_m"] <= 60
    done = client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 30}).json()
    assert done["requirement_met"] == 0 and done["points"] == 0 and done["new_badges"] == []
    assert q["id"] not in {d["id"] for d in badges._done_quests()}

    q2 = walk("medium")
    upload(q2, "t1", lat=north(50))
    upload(q2, "t2", lat=north(400))
    upload(q2, "t3", lat=north(100))
    upload(q2, "t4", lat=north(120))  # 4 of 5 = 75%+ and one photo 300 m+ away
    done2 = client.post(f"/api/quests/{q2['id']}/finish", json={"screen_seconds": 30}).json()
    assert done2["away_met"] and done2["requirement_met"] == 1 and done2["completed"] and done2["points"] == 40


def test_far_photo_must_be_verified_to_count(monkeypatch):
    async def no(*a, **k):
        return {"match": False, "confidence": 0.9, "what_i_see": "a car", "reason": "wrong"}
    monkeypatch.setattr(gemma, "chat", no)
    q = walk("hard")
    upload(q, "t1", lat=north(500))
    assert not client.get(f"/api/quests/{q['id']}").json()["away_met"]


def test_easy_has_no_away_goal(monkeypatch):
    gemma_says_yes(monkeypatch)
    q = walk("easy")
    for t in ("t1", "t2", "t3"):
        upload(q, t, lat=north(20))
    done = client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 20}).json()
    assert done["requirement_met"] == 1 and done["completed"] and done["points"] == 30


def test_map_shows_home_walks_pin_with_visit_count():
    before = next((p for p in client.get("/api/map").json()["visited"] if p["id"] == "home-walks"), {"visits": []})
    for _ in range(2):
        q = walk("easy")
        client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 10})
    m = client.get("/api/map").json()
    home = next(p for p in m["visited"] if p["id"] == "home-walks")
    assert home["name"] == "Home walks"
    assert len(home["visits"]) == len(before["visits"]) + 2
    assert all(p["id"] != "home-walks" for p in m["suggested"])


def test_from_here_needs_location():
    r = client.post("/api/quests", json={"mode": "from_here", "difficulty": "easy"})
    assert r.status_code == 400


def test_quest_stays_full_when_recent_quests_used_every_task_type():
    every = [t[3] for t in quests.SEASON_TASKS["fall"] + quests.COMMON_TASKS]
    ctx = {"mode": "from_here", "difficulty": "medium", "season": "fall", "distance_tier": "nearby",
           "recent_task_types": every, "minutes_until_sunset": 300}
    q, notes = quests.enforce_rules({"tasks": []}, ctx, quests.home_place(LAT, LNG))
    labels = [t["type_label"] for t in q["tasks"]]
    assert len(labels) == 5 and len(set(labels)) == 5
    assert "padded with a recently used task type" in notes
