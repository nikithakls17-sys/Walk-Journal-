"""Real places near the user (SerpApi Google Maps), cached in SQLite, filtered by code, ranked by Gemma."""
import json
import time

import httpx

from . import config, db, gemma, store
from .geo import haversine_m, miles

QUERIES = ["parks", "hiking trails", "nature trail", "lake"]
CACHE_DAYS = 7
TIERS = {  # (min_mi, max_mi, map zoom)
    "nearby": (0, 1, "15z"),
    "walk_run": (0, 5, "13z"),
    "adventure": (0, 20, "11z"),
}
BIG_PLACE_WORDS = ("state park", "conservation", "national", "wildlife", "reservoir", "forest", "preserve", "trail")


class PlacesError(RuntimeError):
    pass


def _cache_key(q: str, lat: float, lng: float, zoom: str) -> str:
    return f"{q}|{lat:.2f}|{lng:.2f}|{zoom}"


async def serpapi_search(q: str, lat: float, lng: float, zoom: str) -> list[dict]:
    key = _cache_key(q, lat, lng, zoom)
    with db.tx() as c:
        hit = c.execute("SELECT * FROM search_cache WHERE key=?", (key,)).fetchone()
    if hit and time.time() - hit["created_at"] < CACHE_DAYS * 86400:
        return json.loads(hit["results"])
    if not config.SERPAPI_KEY:
        raise PlacesError("SERPAPI_KEY is not set")
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get("https://serpapi.com/search.json", params={
            "engine": "google_maps", "type": "search", "q": q, "ll": f"@{lat},{lng},{zoom}",
            "api_key": config.SERPAPI_KEY})
    if r.status_code != 200:
        raise PlacesError(f"SerpApi error {r.status_code}: {r.text[:200]}")
    results = r.json().get("local_results", [])
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO search_cache VALUES (?,?,?)", (key, time.time(), json.dumps(results)))
    return results


def _flatten_extensions(ext) -> list[str]:
    out = []
    for item in ext or []:
        if isinstance(item, dict):
            for v in item.values():
                out.extend(v if isinstance(v, list) else [v])
        elif isinstance(item, str):
            out.append(item)
    return [str(x) for x in out]


def to_place(r: dict) -> dict | None:
    gps = r.get("gps_coordinates") or {}
    if "latitude" not in gps or not r.get("title"):
        return None
    types = r.get("types") or ([r["type"]] if r.get("type") else [])
    features = list(dict.fromkeys([t for t in types if t] + _flatten_extensions(r.get("extensions"))))[:25]
    typ = (r.get("type") or (types[0] if types else "park")).lower()
    text = f"{r['title']} {typ}".lower()
    radius = 1500 if any(w in text for w in BIG_PLACE_WORDS) else config.PLACE_RADIUS_M
    return {
        "id": "g-" + str(r.get("place_id") or r.get("data_id") or f"{gps['latitude']:.5f},{gps['longitude']:.5f}"),
        "name": r["title"], "type": typ, "description": r.get("description") or "", "features": features,
        "address": r.get("address"), "rating": r.get("rating"), "lat": gps["latitude"], "lng": gps["longitude"],
        "radius_m": radius, "raw": {"thumbnail": r.get("thumbnail"), "reviews": r.get("reviews")},
    }


async def nearby(lat: float, lng: float, tier: str) -> list[dict]:
    lo, hi, zoom = TIERS.get(tier, TIERS["nearby"])
    seen, out, errors = set(), [], []
    for q in QUERIES:
        try:
            results = await serpapi_search(q, lat, lng, zoom)
        except PlacesError as e:
            errors.append(str(e))
            continue
        for r in results:
            p = to_place(r)
            if not p or p["id"] in seen:
                continue
            seen.add(p["id"])
            p["distance_mi"] = round(miles(haversine_m(lat, lng, p["lat"], p["lng"])), 2)
            if lo <= p["distance_mi"] <= hi:
                out.append(p)
    if not out and errors:
        raise PlacesError(errors[0])
    for p in out:
        store.save_place(p)
    return sorted(out, key=lambda p: p["distance_mi"])


def visit_counts() -> dict[str, int]:
    with db.tx() as c:
        return {r["place_id"]: r["n"] for r in c.execute(
            "SELECT place_id, COUNT(*) n FROM quests WHERE status='done' GROUP BY place_id").fetchall()}


RANK_SCHEMA = {
    "type": "object",
    "required": ["picks"],
    "properties": {"picks": {"type": "array", "items": {
        "type": "object", "required": ["index", "reason"],
        "properties": {"index": {"type": "integer"}, "reason": {"type": "string"}}}}},
}

RANK_SYSTEM = """You pick the best 3 walks for RIGHT NOW from a list of real places. Consider the season,
minutes until sunset (a far place is a bad pick if the sun sets soon), difficulty, weather, and variety
(an unvisited place is a nice change, a favorite is fine too). Only pick from the list, by index.
Each reason is one short, friendly line (under 15 words) that mentions why it suits right now."""


def code_rank(cands: list[dict], visits: dict) -> list[dict]:
    return sorted(cands, key=lambda p: (visits.get(p["id"], 0) > 2, -(p.get("rating") or 3.5), p["distance_mi"]))


async def rank(cands: list[dict], ctx: dict) -> tuple[list[dict], str]:
    visits = visit_counts()
    pool = code_rank(cands, visits)[:12]
    if len(pool) <= 3:
        return [dict(p, reason=default_reason(p, visits)) for p in pool], "code"
    listing = [{"index": i, "name": p["name"], "type": p["type"], "description": p["description"][:160],
                "features": p["features"][:8], "distance_mi": p["distance_mi"], "rating": p.get("rating"),
                "times_visited": visits.get(p["id"], 0)} for i, p in enumerate(pool)]
    prompt = f"RIGHT NOW: {json.dumps(ctx)}\n\nPLACES:\n{json.dumps(listing, indent=1)}\n\nPick the best 3."
    try:
        r = await gemma.chat(prompt, RANK_SCHEMA, system=RANK_SYSTEM, temperature=0.4, operation="rank_walks")
    except gemma.GemmaError:
        return [dict(p, reason=default_reason(p, visits)) for p in pool[:3]], "code"
    span = r.pop("_span", None)
    picks, used = [], set()
    for pick in r.get("picks", []):
        i = pick.get("index")
        if isinstance(i, int) and 0 <= i < len(pool) and i not in used:
            used.add(i)
            picks.append(dict(pool[i], reason=(pick.get("reason") or "").strip() or default_reason(pool[i], visits)))
    for i, p in enumerate(pool):  # top up if Gemma returned fewer than 3 valid picks
        if len(picks) >= 3:
            break
        if i not in used:
            picks.append(dict(p, reason=default_reason(p, visits)))
    if span:
        span.outcome(f"ranked:{len(used)}_valid")
    return picks[:3], "gemma"


def default_reason(p: dict, visits: dict) -> str:
    n = visits.get(p["id"], 0)
    if n == 0:
        return f"Somewhere new, {p['distance_mi']:g} mi away."
    return f"An old friend: you've walked here {n} time{'s' if n > 1 else ''}."
