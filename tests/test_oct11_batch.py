"""Duplicate checks, tropical seasons, no-location walks, 75% completion with partial walks, gentle weekly goals,
and gallery photos placed by the phone's live GPS."""
import asyncio
import time
from datetime import date, datetime, timezone

from basecamp.app import badges, context, db, gemma, main, push, quests, store
from tests.test_core import LAT, LNG, client, new_quest, upload
from tests.test_gallery_photos import gallery_jpeg, pick


def gemma_yes(monkeypatch, delay=0.0):
    calls = []

    async def chat(*a, **k):
        calls.append(k.get("operation"))
        if delay:
            await asyncio.sleep(delay)
        return {"match": True, "confidence": 0.9, "what_i_see": "a leaf", "reason": "clear"}
    monkeypatch.setattr(gemma, "chat", chat)
    return calls


# ---------- A: a photo is never checked twice in parallel ----------

def test_retry_skips_a_photo_that_is_already_being_checked(monkeypatch):
    q = new_quest()
    cap = upload(q, "t1")  # Gemma unreachable -> stays pending
    assert cap["status"] == "pending"
    calls = gemma_yes(monkeypatch)
    main.CHECKING.add(cap["id"])  # its first check is still running
    try:
        r = client.post("/api/captures/retry-pending").json()
        assert r["already_checking"] >= 1 and calls == []
    finally:
        main.CHECKING.discard(cap["id"])
    client.post("/api/captures/retry-pending")
    assert calls.count("verify_photo") >= 1
    assert client.get(f"/api/quests/{q['id']}").json()["tasks"][0]["status"] == "pass"


def test_parallel_retries_check_each_photo_once(monkeypatch):
    q = new_quest()
    cap = upload(q, "t1")
    calls = gemma_yes(monkeypatch, delay=0.2)

    async def burst():
        await asyncio.gather(*(main.check_once(cap["id"]) for _ in range(5)))
    asyncio.run(burst())
    assert calls.count("verify_photo") == 1


# ---------- 1: tropical seasons from each walker's own location ----------

def test_tropical_seasons_for_india_latitudes():
    hyd = 17.4
    assert context.season_info(date(2026, 4, 10), hyd)["season"] == "summer"
    assert context.season_info(date(2026, 7, 10), hyd)["season"] == "monsoon"
    assert context.season_info(date(2026, 10, 11), hyd)["season"] == "post-monsoon"
    assert context.season_info(date(2026, 1, 10), hyd)["season"] == "winter"
    assert context.season_info(date(2026, 1, 10), hyd)["key"] == "tropical-winter"
    assert context.season_info(date(2026, 10, 11), 23.5)["climate"] == "tropical"
    assert context.season_info(date(2026, 10, 11), 23.6)["season"] == "fall"
    assert context.season_info(date(2026, 1, 10), -12)["season"] == "wet"
    assert context.season_info(date(2026, 10, 11), LAT)["season"] == "fall"  # Missouri unchanged


def test_tropical_quest_uses_tropical_ideas_and_tasks():
    place = quests.home_place(17.4, 78.35)
    ctx = context.build(place, "nearby", "medium", "Asia/Kolkata", [], with_weather=False)
    assert ctx["climate"] == "tropical" and "hibiscus" in ctx["season_ideas"]
    ctx["mode"] = "from_here"
    q = quests.fallback_quest(quests.mark_night(ctx), place)
    tropical_labels = {t[3] for t in quests.SEASON_TASKS[ctx["season_key"]]}
    assert any(t["type_label"] in tropical_labels for t in q["tasks"])
    assert not any(w in " ".join(t["prompt"].lower() for t in q["tasks"]) for w in ("acorn", "pinecone", "red leaf"))


def test_each_walker_gets_their_own_season():
    india = client.post("/api/quests", json={"mode": "from_here", "lat": 17.4, "lng": 78.35, "difficulty": "easy",
                                             "tz": "Asia/Kolkata"}).json()
    missouri = client.post("/api/quests", json={"mode": "from_here", "lat": LAT, "lng": LNG, "difficulty": "easy",
                                                "tz": "America/Chicago"}).json()
    seasons = {india["season"], missouri["season"]}
    assert "post-monsoon" in seasons or "monsoon" in seasons or "winter" in seasons or "summer" in seasons
    assert india["season"] != missouri["season"]


# ---------- 2: no-location walks ----------

def no_location_quest(difficulty="easy"):
    q = client.post("/api/quests", json={"mode": "no_location", "lat": 17.4, "lng": 78.35, "difficulty": difficulty,
                                         "tz": "Asia/Kolkata"}).json()
    return client.post(f"/api/quests/{q['id']}/start", json={"started_at": time.time()}).json()


def test_no_location_quest_has_only_everyday_tasks():
    for diff, n in quests.TASK_COUNTS.items():
        q = no_location_quest(diff)
        assert q["mode"] == "no_location" and len(q["tasks"]) == n
        photos = [t for t in q["tasks"] if t["kind"] == "photo"]
        assert all(t["type_label"] in quests.NO_LOCATION_LABELS for t in photos)
        assert not any(t["rare"] or t["place_specific"] for t in photos)


def test_no_location_rules_drop_anything_else():
    ctx = {"mode": "no_location", "difficulty": "easy", "season": "fall", "distance_tier": "nearby", "recent_task_types": []}
    raw = {"tasks": [{"prompt": "Find a red leaf", "verify": "a red leaf", "category": "leaf", "type_label": "red leaf"},
                     {"prompt": "Photograph your shoes on the path", "verify": "shoes", "category": "ground", "type_label": "your shoes"}]}
    q, notes = quests.enforce_rules(raw, ctx, quests.home_place(LAT, LNG))
    assert all(t["type_label"] in quests.NO_LOCATION_LABELS for t in q["tasks"])
    assert any("everyday" in n for n in notes)


def test_no_location_photo_skips_place_and_says_place_not_confirmed(monkeypatch):
    gemma_yes(monkeypatch)
    q = no_location_quest()
    cap = upload(q, "t1", lat=LAT + 1)  # any GPS sent is ignored: there's no place to check against
    assert cap["status"] == "unconfirmed" and cap["proof"] == "no_location"
    assert cap["label"] == "place not confirmed" and cap["time_ok"] == 1 and cap["place_ok"] is None
    late = upload(q, "t2", taken_at=(time.time() - 3 * 3600) * 1000)
    assert late["status"] == "fail" and "time window" in late["reason"]


# ---------- 3: 75% completes a walk; below that it's a partial walk ----------

def test_needed_is_75_percent_rounded_to_nearest():
    assert [store.needed_for(n) for n in (3, 4, 5, 6, 7)] == [2, 3, 4, 5, 5]


def test_partial_walk_saves_to_journal_and_map_but_earns_nothing(monkeypatch):
    gemma_yes(monkeypatch)
    q = new_quest("medium")  # 5 tasks: 4 needed
    for t in ("t1", "t2", "t3"):
        upload(q, t)
    before = set(badges.earned())
    done = client.post(f"/api/quests/{q['id']}/finish", json={"note": "Short one today", "screen_seconds": 30}).json()
    assert done["partial"] and not done["completed"] and done["points"] == 0 and done["new_badges"] == []
    assert done["progress"] == {"done": 3, "total": 5, "needed": 4, "away_met": True, "completed": False}
    assert set(badges.earned()) == before
    entry = next(e for e in client.get("/api/journal").json()["entries"] if e["id"] == q["id"])
    assert entry["partial"] and entry["note"] == "Short one today"
    visits = [v for p in client.get("/api/map").json()["visited"] for v in p["visits"]]
    assert any(v["quest_id"] == q["id"] and v["partial"] for v in visits)
    assert q["id"] not in {d["id"] for d in badges._done_quests()}


def test_four_of_five_completes(monkeypatch):
    gemma_yes(monkeypatch)
    q = new_quest("medium")
    for t in ("t1", "t2", "t3", "t4"):
        upload(q, t)
    done = client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 30}).json()
    assert done["completed"] and not done["partial"] and done["points"] == 40


def test_skipped_rare_task_leaves_the_count(monkeypatch):
    gemma_yes(monkeypatch)
    q = new_quest("hard")  # 7 tasks; skip the rare one -> 6 counted -> 5 needed
    rare = next(t["id"] for t in q["tasks"] if t["rare"])
    client.post(f"/api/quests/{q['id']}/skip", json={"task_id": rare})
    assert client.get(f"/api/quests/{q['id']}").json()["progress"]["needed"] == 5


# ---------- 4: gentle weekly goal instead of daily streaks ----------

def test_streak_badges_are_gone_and_weekly_badges_exist():
    codes = set(badges.BADGES)
    assert not {"streak_3", "streak_7"} & codes
    assert {"week_well_walked", "four_good_weeks"} <= codes


def test_retired_streak_badge_rows_are_kept_but_not_shown():
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO badges VALUES ('streak_3', ?, 'legacy-quest')", (time.time(),))
    assert "streak_3" in badges.earned()  # the row is untouched
    assert client.get("/api/journal").status_code == 200
    assert all(b["code"] != "streak_3" for b in client.get("/api/badges").json()["badges"])


def test_weekly_goal_setting_validates_and_defaults_to_3():
    db.set_setting("settings", None)
    assert client.get("/api/settings").json()["weekly_goal"] == 3
    assert client.post("/api/settings", json={"weekly_goal": 0}).status_code == 400
    assert client.post("/api/settings", json={"weekly_goal": 8}).status_code == 400
    assert client.post("/api/settings", json={"weekly_goal": 2}).json()["weekly_goal"] == 2
    db.set_setting("settings", None)


def test_week_progress_counts_only_completed_walks(monkeypatch):
    gemma_yes(monkeypatch)
    db.set_setting("settings", {"weekly_goal": 7})
    start = badges.week_progress()["this_week"]
    q = new_quest()
    upload(q, "t1")
    client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 10})  # partial
    assert badges.week_progress()["this_week"] == start
    q = new_quest()
    for t in ("t1", "t2", "t3"):
        upload(q, t)
    w = client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 10}).json()["week"]
    assert w["this_week"] == start + 1 and w["goal"] == 7
    db.set_setting("settings", None)


def utc_noon(d: date) -> float:
    return datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).timestamp()


def test_week_well_walked_and_four_good_weeks(monkeypatch):
    weeks = [date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28), date(2026, 10, 5)]
    fake = [{"finished_at": utc_noon(d), "tz": "UTC"} for d in weeks]
    monkeypatch.setattr(badges, "_done_quests", lambda: fake)
    monkeypatch.setattr(badges, "weekly_goal", lambda: 1)
    w = badges.week_progress(today=date(2026, 10, 11))
    assert w["weeks_met"] == 4 and w["best_run"] == 4 and w["weeks_in_a_row"] == 4


def test_unfinished_current_week_never_breaks_anything(monkeypatch):
    fake = [{"finished_at": utc_noon(date(2026, 10, 5)), "tz": "UTC"}]
    monkeypatch.setattr(badges, "_done_quests", lambda: fake)
    monkeypatch.setattr(badges, "weekly_goal", lambda: 1)
    w = badges.week_progress(today=date(2026, 10, 13))  # new week, no walk yet
    assert w["this_week"] == 0 and w["weeks_in_a_row"] == 1


def test_reminders_are_gentle():
    daily, journal = push.reminder_payload("daily"), push.reminder_payload("journal")
    assert daily["title"] == "Nice day for a short walk?"
    text = (daily["title"] + daily["body"] + journal["title"] + journal["body"]).lower()
    assert not any(w in text for w in ("streak", "don't break", "missed", "lose", "!"))


# ---------- gallery photos placed by the phone's live GPS ----------

def test_gallery_exif_time_plus_live_gps_is_verified_gallery(monkeypatch):
    gemma_yes(monkeypatch)
    q = new_quest()
    cap = pick(q, "t1", gallery_jpeg(time.time() - 60), phone_lat=LAT)  # EXIF time, no EXIF GPS, phone at the place
    assert cap["status"] == "pass" and cap["proof"] == "gallery_live" and cap["label"] == "verified (gallery)"
    row = store.get_capture(cap["id"])
    assert row["exif_time"] == 1 and row["exif_gps"] == 0 and row["user_agent"] is not None


def test_gallery_photo_from_the_last_15_minutes_counts(monkeypatch):
    gemma_yes(monkeypatch)
    q = new_quest()
    store.update_quest(q["id"], started_at=time.time() - 60, deadline_at=time.time() + 600)
    cap = pick(q, "t1", gallery_jpeg(time.time() - 10 * 60), phone_lat=LAT)  # before the quest, within 15 min
    assert cap["status"] == "pass"
    old = pick(q, "t2", gallery_jpeg(time.time() - 40 * 60), phone_lat=LAT)
    assert old["status"] == "fail" and "time window" in old["reason"]


def test_gallery_live_gps_far_away_fails_place(monkeypatch):
    gemma_yes(monkeypatch)
    q = new_quest()
    cap = pick(q, "t1", gallery_jpeg(time.time() - 60), phone_lat=LAT + 0.05)
    assert cap["status"] == "fail" and cap["place_ok"] == 0 and "upload" in cap["reason"]


def test_gallery_without_exif_time_stays_not_confirmed_even_with_live_gps(monkeypatch):
    gemma_yes(monkeypatch)
    q = new_quest()
    cap = pick(q, "t1", gallery_jpeg(), phone_lat=LAT)
    assert cap["status"] == "unconfirmed" and cap["label"] == "time/place not confirmed"
    row = store.get_capture(cap["id"])
    assert row["exif_time"] == 0 and row["exif_gps"] == 0


def test_database_changes_are_additive_only():
    """Every migration is an ADD COLUMN; nothing drops, renames or rewrites existing rows."""
    import inspect
    assert "DROP" not in inspect.getsource(db).upper()
    for table, cols in db.MIGRATIONS.items():
        for name, decl in cols:
            assert "DROP" not in decl.upper() and "UPDATE" not in decl.upper()
    assert "UPDATE " not in inspect.getsource(db.init)
