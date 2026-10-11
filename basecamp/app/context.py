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


TROPIC_LAT = 23.5

# Tropical season ideas (keys are task-bank keys; "summer"/"winter" in the tropics are not temperate ones).
SEASON_GUIDE |= {
    "tropical-summer": ["hibiscus", "bougainvillea", "neem leaves", "a coconut tree", "crows or mynas", "a bright warm sky"],
    "monsoon": ["puddles after rain", "raindrops on a leaf", "dark rain clouds", "hibiscus", "crows or mynas", "a coconut tree"],
    "post-monsoon": ["bougainvillea", "neem leaves", "a coconut tree", "crows or mynas", "a clear washed sky", "hibiscus"],
    "tropical-winter": ["bougainvillea", "neem leaves", "a coconut tree", "crows or mynas", "a soft hazy morning sky", "hibiscus"],
}
SEASON_GUIDE["wet"] = SEASON_GUIDE["monsoon"]
SEASON_GUIDE["dry"] = SEASON_GUIDE["tropical-summer"]


def season_info(d: date, lat: float) -> dict:
    """Season for the walker's own location. Between the tropics (-23.5..23.5) local seasons replace
    fall/winter/spring/summer: India-style summer/monsoon/post-monsoon/winter north of the equator,
    wet/dry south of it. `key` picks the task bank; `season` is the name Gemma and the user see."""
    hemisphere = "north" if lat >= 0 else "south"
    m = d.month
    if -TROPIC_LAT <= lat <= TROPIC_LAT:
        if hemisphere == "north":
            season = ("winter" if m in (12, 1, 2) else "summer" if m in (3, 4, 5)
                      else "monsoon" if m in (6, 7, 8, 9) else "post-monsoon")
            key = {"summer": "tropical-summer", "winter": "tropical-winter"}.get(season, season)
        else:
            season = key = "wet" if m in (11, 12, 1, 2, 3, 4) else "dry"
        return {"season": season, "hemisphere": hemisphere, "climate": "tropical", "key": key}
    north = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
             6: "summer", 7: "summer", 8: "summer", 9: "fall", 10: "fall", 11: "fall"}[m]
    if hemisphere == "south":
        north = {"winter": "summer", "summer": "winter", "spring": "fall", "fall": "spring"}[north]
    return {"season": north, "hemisphere": hemisphere, "climate": "temperate", "key": north}


def season_for(d: date, lat: float) -> tuple[str, str]:
    info = season_info(d, lat)
    return info["season"], info["hemisphere"]


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
    info = season_info(now.date(), place["lat"])
    season, hemisphere = info["season"], info["hemisphere"]
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
        "season_key": info["key"],
        "climate": info["climate"],
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
        "season_ideas": SEASON_GUIDE[info["key"]],
    }
