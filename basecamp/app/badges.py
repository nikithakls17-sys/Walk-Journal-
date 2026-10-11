"""Badges and streaks. Code-awarded, and only verified (Gemma-passed) tasks count."""
from datetime import date, datetime, timedelta

from . import context, db, store

BADGES = {
    "first_grass": ("First Grass", "🌱", "Finish your first quest"),
    "leaf_peeper": ("Leaf Peeper", "🍁", "3 verified fall leaf finds"),
    "cloud_spotter": ("Cloud Spotter", "☁️", "5 verified sky photos"),
    "rare_find": ("Rare Find", "🐞", "Verify a rare find"),
    "explorer": ("Explorer", "🧭", "Walk 5 different places"),
    "regular": ("Regular", "🏡", "Walk the same place 3 times"),
    "golden_hour": ("Golden Hour", "🌇", "A sky photo within 30 min of sunset"),
    "screen_light": ("Screen Light", "🍃", "Finish a quest with under 60 s on screen"),
    "streak_3": ("3-Day Streak", "🔥", "Quests 3 days in a row"),
    "streak_7": ("7-Day Streak", "🌟", "Quests 7 days in a row"),
}


def _done_quests() -> list[dict]:
    with db.tx() as c:
        # A walk from here that never got far enough from the start doesn't count toward badges or streaks.
        return db.rows(c.execute("""SELECT * FROM quests WHERE status='done' AND COALESCE(requirement_met, 1) = 1
                                    ORDER BY finished_at""").fetchall())


def _verified() -> list[tuple[dict, dict, dict]]:
    """(quest, task, capture) for every verified photo/tracker task."""
    out = []
    for q in _done_quests():
        caps = store.captures_for(q["id"])
        for t in q["tasks"]:
            passed = [c for c in caps if c["task_id"] == t["id"] and c["status"] == "pass"]
            if passed and not t.get("skipped"):
                out.append((q, t, passed[0]))
    return out


def quest_day(q: dict) -> date:
    return datetime.fromtimestamp(q["finished_at"], context.tzinfo(q["tz"])).date()


def current_streak(today: date | None = None) -> int:
    days = {quest_day(q) for q in _done_quests()}
    if not days:
        return 0
    d = today or max(days)
    if d not in days:
        d -= timedelta(days=1)  # streak is still alive if you walked yesterday
    n = 0
    while d in days:
        n += 1
        d -= timedelta(days=1)
    return n


def earned() -> dict[str, dict]:
    with db.tx() as c:
        return {r["code"]: dict(r) for r in c.execute("SELECT * FROM badges").fetchall()}


def evaluate(quest_id: str) -> list[dict]:
    """Award any newly earned badges after a quest finishes. Returns the new ones."""
    have = earned()
    done = _done_quests()
    verified = _verified()
    q = store.get_quest(quest_id)
    checks = {
        "first_grass": len(done) >= 1,
        "leaf_peeper": sum(1 for qq, t, _ in verified if t.get("category") == "leaf"
                           and (qq["context"] or {}).get("season") == "fall") >= 3,
        "cloud_spotter": sum(1 for _, t, _ in verified if t.get("category") == "sky") >= 5,
        "rare_find": any(t.get("rare") for _, t, _ in verified),
        "explorer": len({qq["place_id"] for qq in done}) >= 5,
        "regular": any(n >= 3 for n in _place_counts(done).values()),
        "golden_hour": any(t.get("category") == "sky" and _near_sunset(qq, c) for qq, t, c in verified),
        "screen_light": bool(q and q["screen_seconds"] is not None and q["screen_seconds"] < 60
                             and any(qq["id"] == quest_id for qq, _, _ in verified)),
        "streak_3": current_streak() >= 3,
        "streak_7": current_streak() >= 7,
    }
    new = []
    with db.tx() as c:
        for code, ok in checks.items():
            if ok and code not in have:
                c.execute("INSERT INTO badges VALUES (?,?,?)", (code, db.now(), quest_id))
                new.append(describe(code))
    return new


def _place_counts(done: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for q in done:
        counts[q["place_id"]] = counts.get(q["place_id"], 0) + 1
    return counts


def _near_sunset(q: dict, cap: dict) -> bool:
    sunset_ts = (q["context"] or {}).get("sunset_ts")
    return bool(sunset_ts and cap.get("taken_at") and abs(cap["taken_at"] - sunset_ts) <= 30 * 60)


def describe(code: str) -> dict:
    name, icon, how = BADGES[code]
    return {"code": code, "name": name, "icon": icon, "how": how}


def all_badges() -> list[dict]:
    have = earned()
    return [describe(code) | {"earned_at": have.get(code, {}).get("earned_at")} for code in BADGES]
