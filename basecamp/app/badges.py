"""Badges and the weekly goal. Gentle by design: walking is encouraged, never required.

Only completed walks (75%+ of tasks) count toward badges and the weekly goal; partial walks still live in
the journal and on the map. Task badges (Leaf Peeper, Cloud Spotter...) need Gemma-verified tasks.
"""
from collections import Counter
from datetime import date, datetime, timedelta

from . import context, db, store

BADGES = {
    "first_grass": ("First Grass", "🌱", "Complete your first walk"),
    "leaf_peeper": ("Leaf Peeper", "🍁", "3 verified fall leaf finds"),
    "cloud_spotter": ("Cloud Spotter", "☁️", "5 verified sky photos"),
    "rare_find": ("Rare Find", "🐞", "Verify a rare find"),
    "explorer": ("Explorer", "🧭", "Walk 5 different places"),
    "regular": ("Regular", "🏡", "Walk the same place 3 times"),
    "golden_hour": ("Golden Hour", "🌇", "A sky photo within 30 min of sunset"),
    "screen_light": ("Screen Light", "🍃", "Finish a walk with under 60 s on screen"),
    "week_well_walked": ("Week Well Walked", "🌿", "Reach your weekly walking goal"),
    "four_good_weeks": ("Four Good Weeks", "🍀", "Reach your weekly goal 4 weeks in a row"),
}
DEFAULT_WEEKLY_GOAL = 3


def _finished_quests() -> list[dict]:
    with db.tx() as c:
        return db.rows(c.execute("SELECT * FROM quests WHERE status='done' ORDER BY finished_at").fetchall())


def _done_quests() -> list[dict]:
    """Completed walks only: 75%+ of tasks done (and, for Walk from here, far enough from the start)."""
    return [q for q in _finished_quests() if store.progress(q)["completed"]]


def _verified() -> list[tuple[dict, dict, dict]]:
    """(quest, task, capture) for every Gemma-verified task in a completed walk."""
    out = []
    for q in _done_quests():
        caps = store.captures_for(q["id"])
        for t in q["tasks"]:
            passed = [c for c in caps if c["task_id"] == t["id"] and c["status"] == "pass"]
            if passed and not t.get("skipped"):
                out.append((q, t, passed[0]))
    return out


# ---------- weekly goal (Monday-Sunday weeks in the walker's timezone) ----------

def weekly_goal() -> int:
    try:
        return max(1, min(7, int((db.get_setting("settings") or {}).get("weekly_goal") or DEFAULT_WEEKLY_GOAL)))
    except (TypeError, ValueError):
        return DEFAULT_WEEKLY_GOAL


def _week(d: date) -> tuple[int, int]:
    y, w, _ = d.isocalendar()
    return y, w


def _prev(wk: tuple[int, int]) -> tuple[int, int]:
    return _week(date.fromisocalendar(wk[0], wk[1], 1) - timedelta(days=7))


def quest_day(q: dict) -> date:
    return datetime.fromtimestamp(q["finished_at"], context.tzinfo(q["tz"])).date()


def week_progress(today: date | None = None) -> dict:
    """This week's completed walks against the goal. A week still in progress never counts against you."""
    goal = weekly_goal()
    tz = context.tzinfo((db.get_setting("settings") or {}).get("tz"))
    today = today or datetime.now(tz).date()
    counts = Counter(_week(quest_day(q)) for q in _done_quests())
    this = _week(today)
    wk = this if counts[this] >= goal else _prev(this)
    in_a_row = 0
    while counts[wk] >= goal:
        in_a_row += 1
        wk = _prev(wk)
    best, run, cur = 0, 0, None
    for w in sorted(counts):
        run = run + 1 if (cur is not None and _prev(w) == cur and counts[w] >= goal) else (1 if counts[w] >= goal else 0)
        cur = w if counts[w] >= goal else None
        best = max(best, run)
    return {"goal": goal, "this_week": counts[this], "met": counts[this] >= goal,
            "weeks_in_a_row": in_a_row, "best_run": best, "weeks_met": sum(1 for w in counts if counts[w] >= goal)}


def earned() -> dict[str, dict]:
    with db.tx() as c:
        return {r["code"]: dict(r) for r in c.execute("SELECT * FROM badges").fetchall()}


def evaluate(quest_id: str) -> list[dict]:
    """Award any newly earned badges after a completed walk. Returns the new ones."""
    have = earned()
    done = _done_quests()
    verified = _verified()
    q = store.get_quest(quest_id)
    week = week_progress()
    checks = {
        "first_grass": len(done) >= 1,
        "leaf_peeper": sum(1 for qq, t, _ in verified if t.get("category") == "leaf"
                           and (qq["context"] or {}).get("season") == "fall") >= 3,
        "cloud_spotter": sum(1 for _, t, _ in verified if t.get("category") == "sky") >= 5,
        "rare_find": any(t.get("rare") for _, t, _ in verified),
        "explorer": len({qq["place_id"] for qq in done}) >= 5,
        "regular": any(n >= 3 for n in Counter(qq["place_id"] for qq in done).values()),
        "golden_hour": any(t.get("category") == "sky" and _near_sunset(qq, c) for qq, t, c in verified),
        "screen_light": bool(q and q["screen_seconds"] is not None and q["screen_seconds"] < 60
                             and any(qq["id"] == quest_id for qq in done)),
        "week_well_walked": week["weeks_met"] >= 1,
        "four_good_weeks": week["best_run"] >= 4,
    }
    new = []
    with db.tx() as c:
        for code, ok in checks.items():
            if ok and code not in have:
                c.execute("INSERT INTO badges VALUES (?,?,?)", (code, db.now(), quest_id))
                new.append(describe(code))
    return new


def _near_sunset(q: dict, cap: dict) -> bool:
    sunset_ts = (q["context"] or {}).get("sunset_ts")
    return bool(sunset_ts and cap.get("taken_at") and abs(cap["taken_at"] - sunset_ts) <= 30 * 60)


def describe(code: str) -> dict | None:
    """None for retired badges (the old daily streaks): their rows stay in the database, untouched."""
    if code not in BADGES:
        return None
    name, icon, how = BADGES[code]
    return {"code": code, "name": name, "icon": icon, "how": how}


def all_badges() -> list[dict]:
    have = earned()
    return [describe(code) | {"earned_at": have.get(code, {}).get("earned_at")} for code in BADGES]
