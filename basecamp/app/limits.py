"""Daily limits for the public beta (cloud mode only), so one visitor or a script can't spend the Gemma,
SerpApi or ElevenLabs quota. Three layers per action: per phone, per IP address, and for the whole app.
Counters reset at midnight UTC. Local mode has no limits."""
import time

from fastapi import HTTPException

from . import config, db, users

# action: (per phone, per IP, whole app) per day. Each can be changed with LIMIT_<ACTION>_PER_<USER|IP|DAY>.
DEFAULTS = {
    "quests": (5, 15, 300),
    "photos": (20, 60, 2000),
    "searches": (4, 10, 30),  # SerpApi's free plan is 100 searches a month
    "swaps": (15, 40, 600),
    "audio": (5, 15, 150),  # ElevenLabs characters
    "feedback": (5, 15, 500),
}
WHAT = {"quests": "new quests", "photos": "photo checks", "searches": "place searches", "swaps": "task swaps",
        "audio": "read-alouds", "feedback": "feedback messages"}
EXTRA = {
    "photos": " Your journal is all here, and you can keep walking.",
    "quests": " Your journal is all here. Try “Walk from here” tomorrow, or finish today's quest.",
    "searches": " “Walk from here” doesn't need a search, so you can still use that.",
    "audio": " Your phone can still read it aloud with its own voice.",
}


def limits_for(action: str) -> tuple[int, int, int]:
    u, i, d = DEFAULTS[action]
    return (config.limit(f"{action}_per_user", u), config.limit(f"{action}_per_ip", i),
            config.limit(f"{action}_per_day", d))


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def used(action: str, key: str, day: str | None = None) -> int:
    with db.tx() as c:
        r = c.execute("SELECT n FROM usage WHERE day=? AND key=? AND action=?", (day or today(), key, action)).fetchone()
    return r["n"] if r else 0


def take(action: str, uid: str | None, ip: str | None):
    """Count one use, or raise 429 with a friendly message if a daily limit is reached."""
    if not config.CLOUD:
        return
    per_user, per_ip, per_day = limits_for(action)
    day = today()
    keys = [(f"user:{users.hashed(uid)}", per_user, "you've used today's"),
            (f"ip:{users.hashed(ip)}", per_ip, "this network has used today's"),
            ("all", per_day, "Walk Journal has used all of today's")]
    for key, cap, who in keys:
        if used(action, key, day) >= cap:
            if key == "all":
                msg = (f"Walk Journal is a small free beta and has used all of today's {WHAT[action]}. "
                       "Please come back tomorrow. 🌿")
            else:
                msg = (f"You've reached today's limit of {cap} {WHAT[action]}"
                       f"{' on this network' if key.startswith('ip:') else ''}. "
                       "Walk Journal is a small free beta, so this resets tomorrow. 🌿")
            raise HTTPException(429, msg + EXTRA.get(action, ""))
    with db.tx() as c:
        for key, _, _ in keys:
            c.execute("""INSERT INTO usage (day, key, action, n) VALUES (?,?,?,1)
                         ON CONFLICT(day, key, action) DO UPDATE SET n = n + 1""", (day, key, action))


def summary() -> dict:
    return {a: {"per_user": limits_for(a)[0]} for a in DEFAULTS} if config.CLOUD else {}
