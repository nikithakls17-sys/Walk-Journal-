"""Who is asking. Local mode has one walker (the laptop's owner): every request is user None, and all the
existing walks (stored with no user) stay theirs. Cloud mode is a public beta with no accounts: each phone
makes a random id on first open and sends it with every request, and only sees its own walks. The server
only ever stores a hash of that id, so a copy of the database can't be used to open someone's journal."""
import hashlib
import re

from fastapi import HTTPException, Request

from . import config, db

HEADER = "X-WJ-User"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def current(request: Request) -> str | None:
    if not config.CLOUD:
        return None
    uid = request.headers.get(HEADER) or request.query_params.get("u") or ""
    if not ID_RE.match(uid):
        raise HTTPException(401, "Please reopen Walk Journal so it can set up your private journal.")
    return hashed(uid)


def hashed(value: str | None) -> str:
    """Stored instead of the raw id or IP (usage counters, feedback): it can match, but not be used."""
    return hashlib.sha256(f"walkjournal:{value or 'local'}".encode()).hexdigest()[:24]


def client_ip(request: Request) -> str:
    # Render (and most hosts) put the caller's address first in X-Forwarded-For.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def settings_key(uid: str | None) -> str:
    return "settings" if uid is None else f"settings:{uid}"


def settings(uid: str | None) -> dict:
    return db.get_setting(settings_key(uid)) or {}
