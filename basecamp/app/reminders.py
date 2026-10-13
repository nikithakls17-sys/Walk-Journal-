"""Daily walk reminder and evening journal reminder, checked once a minute while Base Camp runs."""
import asyncio
import logging
from datetime import datetime

from . import config, context, db, push

log = logging.getLogger("walkjournal.reminders")
CHECK_EVERY_S = 60
SEND_WINDOW_MIN = 60  # only within an hour after the chosen time, so switching it on at 9 PM doesn't fire at once


def _hhmm(value: str | None, default: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in (value or default).split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except ValueError:
        pass
    h, m = default.split(":")
    return int(h), int(m)


def valid_hhmm(value: str) -> bool:
    try:
        h, m = (int(x) for x in value.split(":"))
        return 0 <= h < 24 and 0 <= m < 60 and len(value) == 5
    except (ValueError, AttributeError):
        return False


def journaled_today(today: str, tz) -> bool:
    """You've journaled today if a walk finished today has a note."""
    with db.tx() as c:
        rows = c.execute("SELECT finished_at, note FROM quests WHERE status='done' AND finished_at IS NOT NULL").fetchall()
    return any((r["note"] or "").strip() and datetime.fromtimestamp(r["finished_at"], tz).strftime("%Y-%m-%d") == today
               for r in rows)


def _in_window(now: datetime, hm: tuple[int, int]) -> bool:
    minutes = now.hour * 60 + now.minute - (hm[0] * 60 + hm[1])
    return 0 <= minutes < SEND_WINDOW_MIN


def due(settings: dict, now: datetime, sent: dict) -> list[str]:
    """Which reminders should go out right now. `now` is in the user's timezone; `sent` maps kind -> last date sent."""
    today = now.strftime("%Y-%m-%d")
    out = []
    if settings.get("notify_daily") and sent.get("daily") != today:
        if _in_window(now, _hhmm(settings.get("daily_time"), "17:00")):
            out.append("daily")
    if settings.get("notify_journal") and sent.get("journal") != today:
        if _in_window(now, _hhmm(settings.get("journal_time"), "20:00")) and not journaled_today(today, now.tzinfo):
            out.append("journal")
    return out


async def tick(now: datetime | None = None) -> list[str]:
    if config.CLOUD:  # reminders are a local-mode feature (one walker, one schedule)
        return []
    settings = db.get_setting("settings") or {}
    if not (settings.get("notify_daily") or settings.get("notify_journal")):
        return []
    now = now or datetime.now(context.tzinfo(settings.get("tz")))
    sent = db.get_setting("reminders_sent") or {}
    kinds = due(settings, now, sent)
    for kind in kinds:
        await push.send_all(push.reminder_payload(kind))
        sent[kind] = now.strftime("%Y-%m-%d")  # once a day, even if a device was unreachable
    if kinds:
        db.set_setting("reminders_sent", sent)
    return kinds


async def loop():
    while True:
        try:
            await tick()
        except Exception:  # a reminder must never take Base Camp down
            log.exception("reminder tick failed")
        await asyncio.sleep(CHECK_EVERY_S)
