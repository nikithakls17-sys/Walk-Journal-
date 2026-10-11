"""Quest generation. Code supplies the facts and enforces the rules; Gemma only writes."""
import json
import random
import re

from . import gemma

TASK_COUNTS = {"easy": 3, "medium": 5, "hard": 7}
DEFAULT_DEADLINE = {"easy": 20, "medium": 45, "hard": 90}
TRACKER_GOAL_MI = {"nearby": 1.0, "walk_run": 1.5, "adventure": 3.0}
CATEGORIES = ["leaf", "sky", "water", "tree", "plant", "flower", "animal", "stone", "ground", "structure", "other"]

# (prompt, verify, category, type_label)
COMMON_TASKS = [
    ("Photograph the sky above you", "the sky, taking up most of the photo", "sky", "sky"),
    ("Find a stone you like and photograph it", "a stone or rock, close up", "stone", "stone"),
    ("Touch the grass: take a photo of your hand on it", "a hand touching grass", "ground", "touch grass"),
    ("Find a leaf that catches your eye", "a single leaf, close up", "leaf", "leaf"),
    ("Photograph the tallest tree you can see", "a tree with its trunk or canopy visible", "tree", "tall tree"),
    ("Find something small and green that is growing", "a green plant", "plant", "green plant"),
]
SEASON_TASKS = {
    "fall": [
        ("Find a red leaf", "a leaf that is mostly red", "leaf", "red leaf"),
        ("Find a yellow leaf", "a leaf that is mostly yellow", "leaf", "yellow leaf"),
        ("Find an acorn", "an acorn", "plant", "acorn"),
        ("Find a pinecone", "a pinecone", "plant", "pinecone"),
        ("Spot a tree that has changed color", "a tree with orange, red, or yellow leaves", "tree", "changed tree"),
        ("Photograph a pile of fallen leaves", "fallen leaves on the ground", "leaf", "leaf pile"),
    ],
    "winter": [
        ("Find frost on the grass", "frost or ice crystals on grass or plants", "ground", "frost"),
        ("Photograph bare branches against the sky", "bare tree branches against the sky", "tree", "bare branches"),
        ("Look for animal tracks", "animal footprints in snow or mud", "animal", "animal tracks"),
        ("Find an evergreen", "an evergreen tree or branch with needles", "tree", "evergreen"),
        ("Find a seed pod or dried plant", "a dried plant, seed head, or seed pod", "plant", "dried plant"),
    ],
    "spring": [
        ("Find the first flowers", "a flower in bloom", "flower", "first flowers"),
        ("Find buds on a branch", "buds or new shoots on a branch", "tree", "buds"),
        ("Find a puddle reflection", "a reflection in a puddle", "water", "puddle reflection"),
        ("Photograph new green leaves", "fresh, light green leaves", "leaf", "new leaves"),
        ("Find a clover", "a clover plant", "plant", "clover"),
    ],
    "summer": [
        ("Find a dandelion", "a dandelion flower or seed head", "flower", "dandelion"),
        ("Find the shade under the biggest tree", "the shade beneath a large tree", "tree", "big tree shade"),
        ("Find a wildflower", "a flower growing outdoors", "flower", "wildflower"),
        ("Photograph a patch of clover", "clover plants", "plant", "clover"),
        ("Find a feather", "a bird feather", "animal", "feather"),
    ],
}
RARE_TASKS = {
    "fall": [("Find a ladybug", "a ladybug", "animal", "ladybug"),
             ("Spot a woolly bear caterpillar", "a fuzzy brown and black caterpillar", "animal", "woolly bear")],
    "winter": [("Spot a cardinal", "a red cardinal bird", "animal", "cardinal"),
               ("Find an icicle", "an icicle", "other", "icicle")],
    "spring": [("Find a bird's nest", "a bird's nest", "animal", "bird nest"),
               ("Find a robin's egg shell", "a small blue eggshell", "animal", "robin egg")],
    "summer": [("Find a ladybug", "a ladybug", "animal", "ladybug"),
               ("Spot a bee on a flower", "a bee on a flower", "animal", "bee on flower")],
}

# Feature words a task may only use if the place data backs them up.
# (trigger words in the task, evidence words in the place data)
FEATURE_GROUPS = [
    (["lake", "pond", "reservoir", "lagoon", "shoreline", "shore", "lakeside"],
     ["lake", "pond", "reservoir", "lagoon", "lakeside"]),
    (["river", "creek", "stream", "brook", "riverbank"], ["river", "creek", "stream", "brook", "riverwalk"]),
    (["spring"], ["spring"]),
    (["waterfall", "cascade"], ["waterfall", "falls", "cascade"]),
    (["beach"], ["beach"]),
    (["dock", "pier", "jetty"], ["dock", "pier", "jetty", "marina", "boat"]),
    (["boardwalk"], ["boardwalk"]),
    (["bridge", "footbridge"], ["bridge", "footbridge"]),
    (["dam", "spillway"], ["dam", "spillway"]),
    (["playground", "swing", "slide"], ["playground"]),
    (["garden", "arboretum", "greenhouse"], ["garden", "arboretum", "botanical"]),
    (["fountain"], ["fountain"]),
    (["pavilion", "gazebo", "shelter", "bandstand"], ["pavilion", "gazebo", "shelter", "bandstand"]),
    (["statue", "monument", "memorial", "sculpture", "plaque"], ["statue", "monument", "memorial", "sculpture", "plaque"]),
    (["trailhead"], ["trailhead", "trail"]),
    (["overlook", "summit", "bluff", "viewpoint", "lookout", "peak"],
     ["overlook", "summit", "bluff", "viewpoint", "lookout", "peak", "hill"]),
    (["hill"], ["hill", "bluff", "summit"]),
    (["prairie"], ["prairie"]),
    (["marsh", "wetland", "swamp"], ["marsh", "wetland", "swamp"]),
    (["forest", "woods", "woodland"], ["forest", "woods", "woodland", "conservation", "nature"]),
    (["ballfield", "diamond", "soccer", "baseball", "softball", "court", "tennis", "basketball", "pickleball"],
     ["ballfield", "field", "soccer", "baseball", "softball", "court", "tennis", "basketball", "pickleball", "sport", "athletic"]),
    (["golf"], ["golf"]),
    (["campground", "campsite"], ["campground", "campsite", "camping"]),
    (["lighthouse"], ["lighthouse"]),
    (["barn", "farm"], ["barn", "farm"]),
    (["fishing"], ["fishing", "lake", "pond", "river"]),
]

UNSAFE = re.compile(
    r"\b(climb\w*|swim\w*|wad(e|ing)|dive|diving|jump\w*|trespass\w*|off[- ]trail|leave the (trail|path)|"
    r"(enter|into|in) the (water|lake|pond|river|creek|stream)|cross the (road|street|river|creek|stream)|"
    r"(touch|pet|feed|chase|catch|hold|pick up) (a |the |an )?(squirrel|bird|duck|goose|geese|deer|snake|animal|"
    r"wildlife|frog|turtle|rabbit|insect|bee|ladybug|caterpillar)\w*|pick (a |the )?flower\w*|on the ice)\b", re.I)
# Walk from here has no destination, so tasks may not send people to a park, trail or landmark.
NO_DESTINATION = re.compile(r"\b(parks?|trails?|trailheads?|landmarks?|downtown|campus|visitor center)\b", re.I)
NIGHT_SKY = re.compile(r"\b(moon|stars?|night|constellation|planet)\b", re.I)

QUEST_SCHEMA = {
    "type": "object",
    "required": ["title", "spoken_intro", "deadline_minutes", "tasks"],
    "properties": {
        "title": {"type": "string"},
        "spoken_intro": {"type": "string"},
        "deadline_minutes": {"type": "integer"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "prompt", "verify", "category", "type_label", "place_specific", "rare"],
                "properties": {
                    "kind": {"type": "string", "enum": ["photo"]},
                    "prompt": {"type": "string"},
                    "verify": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "type_label": {"type": "string"},
                    "place_specific": {"type": "boolean"},
                    "rare": {"type": "boolean"},
                },
            },
        },
    },
}

SYSTEM = """You write short, warm outdoor photo quests for Walk Journal, an app that gets people off their screens.
You are given FACTS as JSON. Use only those facts. Rules:
- Place-specific tasks may ONLY mention features that literally appear in place.name, place.type, place.description or place.features_from_search. Never invent a lake, bridge, trail sign, or anything else.
- Use season_ideas as inspiration, not a script. Match the season, time of day and weather.
- If time_of_day is evening and it is before sunset, include one sky/sunset task. Never include a sky task at night unless it is about the moon or stars. Morning quests may include dew.
- Avoid repeating anything in recent_task_types.
- Never ask for anything unsafe: no climbing, no entering water, no touching or feeding wildlife, no picking flowers, stay on paths.
- Each task needs a "verify" field: a short, literal description of what must be visible in the photo, e.g. "a single leaf that is mostly red".
- type_label is a 1-3 word name for the task type, e.g. "red leaf", "sky", "lake view".
- spoken_intro: 2-3 warm, calm sentences that will be read aloud. Mention the place by name. End by telling them to put the phone away.
- Keep prompts short (under 12 words), friendly and concrete."""


def _words(text: str) -> set[str]:
    raw = re.findall(r"[a-z]+", text.lower())
    return set(raw) | {w[:-1] for w in raw if len(w) > 3 and w.endswith("s")}


def place_text(place: dict) -> str:
    return " ".join([place.get("name") or "", place.get("type") or "", place.get("description") or "",
                     " ".join(place.get("features") or [])])


def unsupported_features(task_text: str, place: dict) -> list[str]:
    """Feature words in the task that the place data does not back up."""
    tw, pw = _words(task_text), _words(place_text(place))
    bad = []
    for triggers, evidence in FEATURE_GROUPS:
        hit = [t for t in triggers if t in tw]
        if hit and not any(e in pw for e in evidence):
            bad.extend(hit)
    return bad


def place_features(place: dict) -> list[str]:
    pw = _words(place_text(place))
    return [triggers[0] for triggers, evidence in FEATURE_GROUPS if any(e in pw for e in evidence)]


def is_unsafe(text: str) -> bool:
    return bool(UNSAFE.search(text))


def _task(t: tuple, **extra) -> dict:
    prompt, verify, category, label = t
    return {"kind": "photo", "prompt": prompt, "verify": verify, "category": category, "type_label": label,
            "place_specific": False, "rare": False, **extra}


def _bank(season: str, used: set[str], rare=False) -> list[dict]:
    src = RARE_TASKS[season] if rare else SEASON_TASKS[season] + COMMON_TASKS
    out = [_task(t, rare=rare) for t in src if t[3] not in used]
    random.shuffle(out)
    return out


def _place_task(place: dict, n: int) -> dict:
    name = place["name"]
    options = [
        (f"Take a wide photo that shows where you are at {name}", "a wide outdoor scene", "other", "wide view"),
        (f"Photograph your favorite spot at {name}", "an outdoor scene", "other", "favorite spot"),
        (f"Find the most peaceful view at {name}", "a calm outdoor view", "other", "peaceful view"),
    ]
    return _task(options[n % len(options)], place_specific=True)


def home_place(lat: float, lng: float) -> dict:
    """The "place" for a Walk from here quest: no destination, so no features Gemma could mention."""
    return {"name": "your neighborhood", "type": "walk from home", "description": "", "features": [],
            "lat": lat, "lng": lng}


def check_task(t: dict, ctx: dict, place: dict, used: set[str], recent: set[str] = frozenset()) -> tuple[dict | None, str | None]:
    """Validate one Gemma task against every rule. Returns (clean task, None) or (None, why it was dropped).
    Used for whole quests and for single-task swaps, so both follow exactly the same rules."""
    if not t.get("prompt"):
        return None, None
    text = f"{t.get('prompt', '')} {t.get('verify', '')}"
    label = (t.get("type_label") or "").lower().strip()
    from_here = ctx.get("mode") == "from_here"
    if is_unsafe(text):
        return None, f"dropped unsafe task: {t['prompt']}"
    if from_here and NO_DESTINATION.search(text):
        return None, f"dropped destination task on a walk from here: {t['prompt']}"
    bad = unsupported_features(text, place)
    if bad:
        return None, f"dropped task with invented feature {bad}: {t['prompt']}"
    if t.get("category") == "sky" and ctx.get("night_quest") and not NIGHT_SKY.search(text):
        return None, f"dropped daytime sky task at night: {t['prompt']}"
    if label and (label in recent or label in used):
        return None, f"dropped repeated task type '{label}'"
    specific = bool(t.get("place_specific")) and not from_here
    if specific and not place_features(place) and place["name"].lower() not in text.lower():
        specific = False
    return {"kind": "photo", "prompt": t["prompt"].strip(), "verify": (t.get("verify") or t["prompt"]).strip(),
            "category": t.get("category") if t.get("category") in CATEGORIES else "other",
            "type_label": label or "photo", "place_specific": specific,
            "rare": bool(t.get("rare")) and ctx["difficulty"] == "hard"}, None


def enforce_rules(raw: dict, ctx: dict, place: dict) -> tuple[dict, list[str]]:
    """Validate and repair a Gemma quest. Returns the quest and the repairs made."""
    difficulty, season = ctx["difficulty"], ctx["season"]
    n = TASK_COUNTS[difficulty]
    notes: list[str] = []
    recent = {r.lower() for r in ctx.get("recent_task_types", [])}
    used: set[str] = set()
    night = ctx.get("night_quest")
    from_here = ctx.get("mode") == "from_here"

    tasks = []
    for t in raw.get("tasks", []):
        task, note = check_task(t, ctx, place, used, recent)
        if note:
            notes.append(note)
        if task:
            used.add((t.get("type_label") or "").lower().strip())
            tasks.append(task)

    want_place = 0 if from_here else {"easy": 0, "medium": 1, "hard": 2}[difficulty]
    want_rare = 1 if difficulty == "hard" else 0
    photo_slots = n - (1 if difficulty == "hard" else 0)

    for t in [t for t in tasks if t["rare"]][want_rare:]:
        t["rare"] = False
    have_place = sum(t["place_specific"] for t in tasks)
    k = 0
    while have_place < want_place:
        tasks.insert(0, _place_task(place, k))
        k += 1
        have_place += 1
        notes.append("added place-specific task")
    if want_rare and not any(t["rare"] for t in tasks):
        bank = _bank(season, used | recent, rare=True)
        if bank:
            tasks.append(bank[0])
            used.add(bank[0]["type_label"])
            notes.append("added rare find")
    while len(tasks) > photo_slots:
        idx = next((i for i in range(len(tasks) - 1, -1, -1)
                    if not tasks[i]["place_specific"] and not tasks[i]["rare"]), len(tasks) - 1)
        tasks.pop(idx)
    bank = [b for b in _bank(season, used | recent) if not (night and b["category"] == "sky")]
    while len(tasks) < photo_slots and bank:
        tasks.append(bank.pop(0))
        used.add(tasks[-1]["type_label"])
        notes.append("padded with seasonal task")
    # After several quests the "no repeats from the last 3 quests" rule can empty the bank.
    # A full quest matters more, so allow recent types again (still never twice in one quest).
    bank = [b for b in _bank(season, used) if not (night and b["category"] == "sky")]
    while len(tasks) < photo_slots and bank:
        tasks.append(bank.pop(0))
        used.add(tasks[-1]["type_label"])
        notes.append("padded with a recently used task type")
    if difficulty == "hard":
        goal = TRACKER_GOAL_MI.get(ctx["distance_tier"], 1.5)
        tasks.append({"kind": "tracker", "prompt": f"Walk at least {goal:g} miles and add your tracker screenshot",
                      "verify": "", "category": "other", "type_label": "tracker", "place_specific": False,
                      "rare": False, "goal": {"distance_mi": goal}})
    for i, t in enumerate(tasks, 1):
        t["id"] = f"t{i}"

    quest = {
        "title": (raw.get("title") or "A Walk Outside").strip()[:80],
        "spoken_intro": (raw.get("spoken_intro") or "").strip(),
        "deadline_minutes": clamp_deadline(raw.get("deadline_minutes"), ctx),
        "tasks": tasks,
    }
    if not quest["spoken_intro"]:
        quest["spoken_intro"] = default_intro(place, quest)
    return quest, notes


def clamp_deadline(value, ctx: dict) -> int:
    base = DEFAULT_DEADLINE[ctx["difficulty"]]
    try:
        d = int(value)
    except (TypeError, ValueError):
        d = base
    d = max(int(base * 0.5), min(int(base * 1.5), d))
    mins = ctx.get("minutes_until_sunset")
    if not ctx.get("night_quest") and mins is not None and mins > 0:
        d = min(d, max(10, mins - 5))
    return d


def default_intro(place: dict, quest: dict) -> str:
    n = len(quest["tasks"])
    if place.get("type") == "walk from home":
        return (f"Welcome to {quest['title']}, a walk from right where you are. You have {n} things to find in about "
                f"{quest['deadline_minutes']} minutes. Wander wherever feels good, and put your phone away.")
    return (f"Welcome to {quest['title']} at {place['name']}. You have {n} things to find in about "
            f"{quest['deadline_minutes']} minutes. Take your time, look around, and put your phone away.")


def mark_night(ctx: dict) -> dict:
    """A quest that would end after dark becomes a night quest: no daytime sky tasks, no sunset clamp."""
    mins = ctx.get("minutes_until_sunset")
    ctx["night_quest"] = bool(ctx.get("is_night") or (mins is not None and mins < 15))
    return ctx


def fallback_quest(ctx: dict, place: dict) -> dict:
    """Used when Gemma is unreachable: a code-written quest from the same banks and rules."""
    title = {"fall": "An Autumn Wander", "winter": "A Winter Lookabout", "spring": "A Spring Ramble",
             "summer": "A Summer Stroll"}[ctx["season"]]
    raw = {"title": title, "spoken_intro": "", "deadline_minutes": DEFAULT_DEADLINE[ctx["difficulty"]], "tasks": []}
    if ctx.get("time_of_day") == "evening" and not ctx.get("night_quest"):
        raw["tasks"].append({"prompt": "Catch the evening sky before sunset", "verify": "the sky at sunset or dusk",
                             "category": "sky", "type_label": "sunset sky"})
    return enforce_rules(raw, ctx, place)[0]


SWAP_LIMITS = {"easy": 1, "medium": 2, "hard": 2}  # separate from the Hard rare-find skip
TASK_SCHEMA = QUEST_SCHEMA["properties"]["tasks"]["items"]


async def swap_task(ctx: dict, place: dict, old: dict, others: list[dict], attempts: int = 2) -> tuple[dict, str, list[str]]:
    """One replacement task with the quest's own context and every rule. Returns (task, source, notes).
    Never repeats a task type already in this quest (including the one being swapped out)."""
    used = {(t.get("type_label") or "").lower() for t in others + [old]}
    need_specific = bool(old.get("place_specific")) and ctx.get("mode") != "from_here"
    need_rare = bool(old.get("rare"))
    asks = []
    if need_specific:
        asks.append("It must be place-specific (place_specific=true) and only use features listed for the place.")
    if need_rare:
        asks.append("It must be a rare find (rare=true) that might not be possible this time of year.")
    if ctx.get("mode") == "from_here":
        asks.append("This is a walk from home with no destination: no parks, trails, lakes or landmarks.")
    facts = {k: v for k, v in ctx.items() if k not in ("sunset_ts", "repairs")}
    facts["place"] = {k: v for k, v in ctx.get("place", {}).items() if k not in ("lat", "lng")}
    prompt = (f"FACTS:\n{json.dumps(facts, indent=1)}\n\n"
              f"The walker wants to swap out this task: {old['prompt']!r}.\n"
              f"Write ONE different photo task for the same walk. {' '.join(asks)}\n"
              f"Do not repeat any of these task types: {sorted(l for l in used if l)}.\n"
              f"Tasks already in this quest: {[t['prompt'] for t in others]}.")
    notes: list[str] = []
    for _ in range(attempts):
        try:
            raw = await gemma.chat(prompt, TASK_SCHEMA, system=SYSTEM, temperature=0.9, operation="swap_task",
                                   outcome=lambda r: "swap_ok" if check_task(r, ctx, place, used)[0] else "swap_rejected")
        except gemma.GemmaError as e:
            notes.append(str(e))
            break
        task, note = check_task(raw, ctx, place, used)
        if note:
            notes.append(note)
        if task and need_specific and not task["place_specific"]:
            notes.append(f"dropped swap that wasn't place-specific: {task['prompt']}")
            task = None
        if task:
            task["rare"] = need_rare
            return task, "gemma", notes
    return _swap_fallback(ctx, place, used, need_specific, need_rare), "fallback", notes


def _swap_fallback(ctx: dict, place: dict, used: set[str], need_specific: bool, need_rare: bool) -> dict:
    """Code-written replacement from the same banks, used when Gemma is unreachable or keeps breaking rules."""
    night = ctx.get("night_quest")
    if need_specific:
        for k in range(3):
            t = _place_task(place, k)
            if t["type_label"] not in used:
                return t
    pools = ([_bank(ctx["season"], used, rare=True)] if need_rare else []) + [_bank(ctx["season"], used)]
    for pool in pools:
        for t in pool:
            if not (night and t["category"] == "sky"):
                return t | {"rare": need_rare}
    return _task(("Find something that makes you smile", "an outdoor scene", "other", "smile find"), rare=need_rare)


def _quest_outcome(raw: dict, ctx: dict, place: dict) -> str:
    notes = enforce_rules(raw, dict(ctx), place)[1]
    dropped = sum(1 for x in notes if x.startswith("dropped"))
    return "quest_generated" if not dropped else f"quest_repaired:{dropped}"


async def generate(ctx: dict, place: dict, attempts: int = 2) -> tuple[dict, str, list[str]]:
    """Returns (quest, source, repair_notes). source is 'gemma' or 'fallback'."""
    mark_night(ctx)
    n = TASK_COUNTS[ctx["difficulty"]]
    rules = {
        "easy": f"Write {n} photo tasks: common, easy finds (a leaf, the sky, a stone you like, touching the grass).",
        "medium": f"Write {n} photo tasks: seasonal finds plus at least 1 place-specific task.",
        "hard": f"Write {n - 1} photo tasks: seasonal finds, 2 place-specific tasks, and 1 rare find (rare=true) "
                "that might not be possible this time of year.",
    }[ctx["difficulty"]]
    if ctx.get("mode") == "from_here":
        rules = {
            "easy": f"Write {n} photo tasks",
            "medium": f"Write {n} photo tasks",
            "hard": f"Write {n - 1} photo tasks, including 1 rare find (rare=true) that might not be possible this time of year",
        }[ctx["difficulty"]] + (" for a walk from home with NO destination. Only seasonal and time-of-day finds anyone "
                                "could spot on a neighborhood walk (sky, trees, leaves, plants, stones, birds). "
                                "No place-specific tasks: place_specific must be false, and never mention parks, lakes, "
                                "trails or any landmark.")
    facts = {k: v for k, v in ctx.items() if k != "sunset_ts"} | {"place": {k: v for k, v in ctx["place"].items() if k not in ("lat", "lng")}}
    prompt = (f"FACTS:\n{json.dumps(facts, indent=1)}\n\n{rules}\n"
              f"Suggest deadline_minutes around {DEFAULT_DEADLINE[ctx['difficulty']]}.")
    best, best_dropped, best_notes = None, 99, []
    for _ in range(attempts):
        try:
            raw = await gemma.chat(prompt, QUEST_SCHEMA, system=SYSTEM, temperature=0.8, operation="generate_quest",
                                   outcome=lambda r: _quest_outcome(r, ctx, place))
        except gemma.GemmaError as e:
            return fallback_quest(ctx, place), "fallback", [str(e)]
        quest, notes = enforce_rules(raw, ctx, place)
        dropped = sum(1 for x in notes if x.startswith("dropped"))
        if dropped < best_dropped:
            best, best_dropped, best_notes = quest, dropped, notes
        if dropped <= n // 3:
            break
    return best, "gemma", best_notes
