from basecamp.app import gemma, places
from tests.test_core import LAT, LNG, client

SAMPLE = [
    {"title": "Lions Lake Park", "place_id": "A", "type": "Park", "types": ["Park"], "gps_coordinates": {"latitude": LAT + 0.004, "longitude": LNG},
     "rating": 4.6, "description": "Fishing lake with a walking trail", "extensions": [{"amenities": ["Restroom"]}]},
    {"title": "Far Away Conservation Area", "place_id": "B", "type": "Conservation area", "gps_coordinates": {"latitude": LAT + 0.2, "longitude": LNG},
     "rating": 4.8},
    {"title": "Pine Street Park", "place_id": "C", "type": "Park", "gps_coordinates": {"latitude": LAT - 0.006, "longitude": LNG + 0.002}, "rating": 4.1},
    {"title": "No GPS Park", "place_id": "D"},
]


def fake_search(monkeypatch):
    async def search(q, lat, lng, zoom):
        return SAMPLE
    monkeypatch.setattr(places, "serpapi_search", search)


def test_nearby_filters_by_tier_and_dedupes(monkeypatch):
    fake_search(monkeypatch)
    import asyncio
    near = asyncio.run(places.nearby(LAT, LNG, "nearby"))
    assert [p["name"] for p in near] == ["Lions Lake Park", "Pine Street Park"]
    adv = asyncio.run(places.nearby(LAT, LNG, "adventure"))
    assert len(adv) == 3
    far = next(p for p in adv if p["id"] == "g-B")
    assert far["radius_m"] == 1500  # conservation areas are big


def test_suggest_falls_back_to_code_rank(monkeypatch):
    fake_search(monkeypatch)
    r = client.get("/api/places/suggest", params={"lat": LAT, "lng": LNG, "tier": "nearby"}).json()
    assert r["source"] == "code" and len(r["picks"]) == 2 and all(p["reason"] for p in r["picks"])


def test_gemma_rank_ignores_bad_indexes(monkeypatch):
    fake_search(monkeypatch)
    many = [dict(SAMPLE[0], place_id=f"P{i}", title=f"Park {i}") for i in range(6)]

    async def search(q, lat, lng, zoom):
        return many
    monkeypatch.setattr(places, "serpapi_search", search)

    async def chat(*a, **k):
        return {"picks": [{"index": 99, "reason": "bad"}, {"index": 2, "reason": "Golden light by the lake"}, {"index": 2, "reason": "dup"}]}
    monkeypatch.setattr(gemma, "chat", chat)
    r = client.get("/api/places/suggest", params={"lat": LAT, "lng": LNG, "tier": "nearby"}).json()
    assert r["source"] == "gemma" and len(r["picks"]) == 3
    assert r["picks"][0]["reason"] == "Golden light by the lake"


def test_map_counts_visits(monkeypatch):
    fake_search(monkeypatch)
    client.get("/api/places/suggest", params={"lat": LAT, "lng": LNG, "tier": "nearby"})
    q = client.post("/api/quests", json={"place_id": "g-A", "difficulty": "easy"}).json()
    client.post(f"/api/quests/{q['id']}/finish", json={"screen_seconds": 30})
    m = client.get("/api/map").json()
    lions = next(p for p in m["visited"] if p["id"] == "g-A")
    assert len(lions["visits"]) >= 1
    assert all(p["id"] != "g-A" for p in m["suggested"])
