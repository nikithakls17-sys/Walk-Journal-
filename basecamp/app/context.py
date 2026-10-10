"""Facts about right now that code computes and hands to Gemma: season, sun, weather."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
from astral import LocationInfo
from astral.sun import sun

SEASON_GUIDE = {
    "fall": ["red leaf", "yellow leaf", "acorn", "pinecone", "squirrel gathering food", "a tree that has changed color"],
    "winter": ["frost on grass", "bare branches against the sky", "animal tracks", "an evergreen", "your breath in cold air"],
    "spring": ["first flowers", "buds on a branch", "a bird's nest", "a puddle reflection", "new green leaves"],
    "summer": ["ladybug", "butterfly", "dandelion", "shade under the biggest tree", "a bee on a flower"],
}

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers", 81: "rain showers", 82: "heavy showers",
       95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm"}


def tzinfo(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "America/Chicago")
    except Exception:
        return ZoneInfo("America/Chicago")


def season_for(d: date, lat: float) -> tuple[str, str]:
    hemisphere = "north" if lat >= 0 else "south"
    m = d.month
    north = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
             6: "summer", 7: "summer", 8: "summer", 9: "fall", 10: "fall", 11: "fall"}[m]
    if hemisphere == "south":
        north = {"winter": "summer", "summer": "winter", "spring": "fall", "fall": "spring"}[north]
    return north, hemisphere


def sun_times(lat: float, lng: float, when: datetime) -> dict:
    loc = LocationInfo(latitude=lat, longitude=lng, timezone=str(when.tzinfo))
    try:
        s = sun(loc.observer, date=when.date(), tzinfo=when.tzinfo)
    except ValueError:  # polar day/night
        return {"sunrise": None, "sunset": None}
    return {"sunrise": s["sunrise"], "sunset": s["sunset"], "dawn": s["dawn"], "dusk": s["dusk"]}


def weather(lat: float, lng: float) -> str | None:
    try:
        r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=4, params={
            "latitude": lat, "longitude": lng, "current": "temperature_2m,weather_code",
            "temperature_unit": "fahrenheit"})
        cur = r.json()["current"]
        return f"{WMO.get(cur['weather_code'], 'mixed')}, {round(cur['temperature_2m'])}F"
    except Exception:
        return None


def build(place: dict, distance_tier: str, difficulty: str, tz: str | None, recent_task_types: list[str],
          now: datetime | None = None, with_weather: bool = True) -> dict:
    zone = tzinfo(tz)
    now = now or datetime.now(zone)
    season, hemisphere = season_for(now.date(), place["lat"])
    st = sun_times(place["lat"], place["lng"], now)
    sunset, sunrise = st.get("sunset"), st.get("sunrise")
    minutes_until_sunset = int((sunset - now).total_seconds() // 60) if sunset else None
    is_night = bool(sunset and sunrise and (now >= sunset or now < sunrise))
    part = ("night" if is_night else "morning" if now.hour < 11 else "midday" if now.hour < 15
            else "evening" if minutes_until_sunset is not None and minutes_until_sunset <= 120 else "afternoon")
    return {
        "place": {
            "name": place["name"], "type": place.get("type") or "place",
            "description": place.get("description") or "",
            "features_from_search": place.get("features") or [],
            "lat": round(place["lat"], 4), "lng": round(place["lng"], 4),
        },
        "distance_tier": distance_tier,
        "difficulty": difficulty,
        "season": season,
        "hemisphere": hemisphere,
        "local_time": now.strftime("%H:%M"),
        "time_of_day": part,
        "is_night": is_night,
        "sunrise": sunrise.strftime("%H:%M") if sunrise else None,
        "sunset": sunset.strftime("%H:%M") if sunset else None,
        "sunset_ts": sunset.timestamp() if sunset else None,
        "minutes_until_sunset": minutes_until_sunset,
        "weather": weather(place["lat"], place["lng"]) if with_weather else None,
        "recent_task_types": recent_task_types,
        "season_ideas": SEASON_GUIDE[season],
    }
