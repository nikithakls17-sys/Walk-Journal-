"""Swap this task: one Gemma replacement, same rules, limited per quest, no point penalty."""
import time

import pytest

from basecamp.app import db, gemma, push, quests, store
from tests.test_core import LAT, LNG, client, upload


def make_quest(difficulty="easy", mode="here", place_desc=""):
    if mode == "from_here":
        q = client.post("/api/quests", json={"mode": "from_here", "lat": LAT, "lng": LNG, "difficulty": difficulty,
                                             "tz": "America/Chicago"}).json()
    else:
        pid = f"g-test-{difficulty}-{time.time_ns()}"
        store.save_place({"id": pid, "name": "Maple Park", "type": "park", "description": place_desc, "features": [],
                          "lat": LAT, "lng": LNG})
        q = client.post("/api/quests", json={"place_id": pid, "difficulty": difficulty, "tz": "America/Chicago"}).json()
    # Pin the context so results don't depend on the real clock (no night-time sky rule surprises).
    raw = store.get_quest(q["id"])
    store.update_quest(q["id"], context=raw["context"] | {"night_quest": False, "time_of_day": "afternoon"})
    return client.post(f"/api/quests/{q['id']}/start", json={"started_at": time.time()}).json()


def gemma_writes(monkeypatch, *tasks):
    """Gemma returns these tasks in order, one per call."""
    queue = list(tasks)

    async def chat(prompt, schema, **k):
        if k.get("operation") == "swap_task":
            if not queue:
                raise gemma.GemmaError("no more fake replies")  # behaves like Gemma being unreachable
            return dict(queue.pop(0))
        return {"match": True, "confidence": 0.9, "what_i_see": "yes", "reason": "ok"}
    monkeypatch.setattr(gemma, "chat", chat)
    return queue


def task(prompt, label, category="leaf", **kw):
    return {"kind": "photo", "prompt": prompt, "verify": prompt.lower(), "category": category, "type_label": label,
            "place_specific": False, "rare": False} | kw


def plain_ids(q):
    """Photo tasks that are neither place-specific nor the rare find."""
    return [t["id"] for t in q["tasks"] if t["kind"] == "photo" and not t["place_specific"] and not t["rare"]]


def swap(q, task_id):
    return client.post(f"/api/quests/{q['id']}/swap", json={"task_id": task_id})


def test_swap_replaces_task_in_place_with_new_id_and_counts_down(monkeypatch):
    gemma_writes(monkeypatch, task("Find a heart-shaped leaf", "heart leaf"))
    q = make_quest("easy")
    assert q["swaps_left"] == 1
    r = swap(q, "t2").json()
    assert r["swap"]["source"] == "gemma" and r["swaps_left"] == 0
    assert r["tasks"][1]["prompt"] == "Find a heart-shaped leaf"
    assert r["tasks"][1]["id"] not in {"t1", "t2", "t3"} and r["tasks"][1]["status"] == "todo"
    assert [t["id"] for t in r["tasks"]][0] == "t1" and len(r["tasks"]) == 3
    assert swap(q, "t1").status_code == 400  # Easy: 1 swap


@pytest.mark.parametrize("difficulty,limit", [("easy", 1), ("medium", 2), ("hard", 2)])
def test_swap_limits(monkeypatch, difficulty, limit):
    gemma_writes(monkeypatch, *[task(f"Find thing {i}", f"thing {i}") for i in range(3)])
    q = make_quest(difficulty)
    assert q["swaps_left"] == limit
    ids = plain_ids(q)
    for i in range(limit):
        r = swap(q, ids[i]).json()
        assert r["swap"]["source"] == "gemma"
    assert swap(q, ids[limit]).status_code == 400


def test_hard_swaps_are_separate_from_the_rare_skip(monkeypatch):
    gemma_writes(monkeypatch, task("Find a seed pod", "seed pod"))
    q = make_quest("hard")
    r = swap(q, plain_ids(q)[0]).json()
    assert r["swaps_left"] == 1 and r["skips_left"] == 1


def test_swap_rejects_invented_feature_and_falls_back(monkeypatch):
    gemma_writes(monkeypatch, task("Photograph the lake", "lake view", "water"),
                 task("Stand on the bridge", "bridge", "structure"))
    q = make_quest("easy")
    r = swap(q, "t1").json()
    new = r["tasks"][0]["prompt"].lower()
    assert r["swap"]["source"] == "fallback" and "lake" not in new and "bridge" not in new
    assert any("invented feature" in n for n in r["swap"]["notes"])


def test_swap_rejects_unsafe_and_repeated_types(monkeypatch):
    q = make_quest("easy")
    existing = q["tasks"][1]["type_label"]
    gemma_writes(monkeypatch, task("Climb the oak tree", "oak climb", "tree"),
                 task("Another one like task 2", existing))
    r = swap(q, "t1").json()
    labels = [t["type_label"] for t in r["tasks"]]
    assert len(labels) == len(set(labels))
    assert "climb" not in r["tasks"][0]["prompt"].lower()
    notes = " ".join(r["swap"]["notes"])
    assert "unsafe" in notes and "repeated" in notes


def test_swap_never_brings_back_the_task_it_replaced(monkeypatch):
    q = make_quest("easy")
    old = q["tasks"][0]
    gemma_writes(monkeypatch, task(old["prompt"], old["type_label"], old["category"]),
                 task(old["prompt"], old["type_label"], old["category"]))
    r = swap(q, "t1").json()
    assert r["tasks"][0]["type_label"] != old["type_label"]


def test_walk_from_here_swap_has_no_destinations(monkeypatch):
    gemma_writes(monkeypatch, task("Walk to the park entrance", "park gate", "structure", place_specific=True),
                 task("Find a feather", "feather", "animal", place_specific=True))
    q = make_quest("medium", mode="from_here")
    r = swap(q, "t1").json()
    assert r["tasks"][0]["prompt"] == "Find a feather" and r["tasks"][0]["place_specific"] is False


def test_place_specific_task_is_replaced_by_a_place_specific_one(monkeypatch):
    gemma_writes(monkeypatch, task("Find a red leaf", "red leaf 2"),  # not place-specific: rejected
                 task("Photograph the lake from the shore", "lake view", "water", place_specific=True))
    q = make_quest("medium", place_desc="City park with a fishing lake")
    i = next(i for i, t in enumerate(q["tasks"]) if t["place_specific"])
    r = swap(q, q["tasks"][i]["id"]).json()
    assert r["tasks"][i]["place_specific"] and "lake" in r["tasks"][i]["prompt"]


def test_rare_task_swap_stays_rare(monkeypatch):
    gemma_writes(monkeypatch, task("Spot a monarch butterfly", "monarch", "animal", rare=True))
    q = make_quest("hard")
    i = next(i for i, t in enumerate(q["tasks"]) if t["rare"])
    assert swap(q, q["tasks"][i]["id"]).json()["tasks"][i]["rare"] is True


def test_gemma_down_still_swaps_with_a_valid_fallback():
    q = make_quest("medium")
    r = swap(q, "t2").json()  # Ollama is unreachable in tests
    assert r["swap"]["source"] == "fallback"
    labels = [t["type_label"] for t in r["tasks"]]
    assert len(labels) == len(set(labels)) and r["tasks"][1]["prompt"]


def test_finished_tracker_and_done_tasks_cannot_be_swapped(monkeypatch):
    gemma_writes(monkeypatch)
    q = make_quest("hard")
    tracker = next(t["id"] for t in q["tasks"] if t["kind"] == "tracker")
    assert swap(q, tracker).status_code == 400
    upload(q, "t1")  # verified by the mocked Gemma
    assert swap(q, "t1").status_code == 400
    client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 10})
    assert swap(q, "t2").status_code == 400


def test_old_photos_do_not_count_for_the_new_task(monkeypatch):
    async def no(prompt, schema, **k):
        if k.get("operation") == "swap_task":
            return task("Find a feather", "feather", "animal")
        return {"match": False, "confidence": 0.9, "what_i_see": "a car", "reason": "wrong"}
    monkeypatch.setattr(gemma, "chat", no)
    q = make_quest("easy")
    upload(q, "t1")
    assert client.get(f"/api/quests/{q['id']}").json()["tasks"][0]["status"] == "fail"
    r = swap(q, "t1").json()
    assert r["tasks"][0]["status"] == "todo" and r["tasks"][0]["attempts"] == 0


def test_no_point_penalty_for_swapping(monkeypatch):
    gemma_writes(monkeypatch, task("Find a feather", "feather", "animal"))
    q = make_quest("easy")
    r = swap(q, "t1").json()
    for t in r["tasks"]:
        upload(q, t["id"])
    done = client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 10}).json()
    assert done["points"] == 30  # 3 verified tasks x 10, same as a quest with no swaps


def test_swap_updates_the_quest_notification(monkeypatch):
    gemma_writes(monkeypatch, task("Find a feather", "feather", "animal"))
    sent = []

    async def send_all(payload):
        push.assert_private(payload)
        sent.append(payload)
    monkeypatch.setattr(push, "send_all", send_all)
    monkeypatch.setattr(push, "enabled", lambda: True)
    db.set_setting("settings", {"notify_quest": True})
    q = make_quest("easy")
    swap(q, "t1")
    db.set_setting("settings", {})
    assert sent and sent[-1]["tag"] == push.QUEST_TAG and "○ Find a feather" in sent[-1]["body"]
