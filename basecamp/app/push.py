"""Web Push from Base Camp (VAPID). Notifications carry text only: never photos, never locations.

    python -m basecamp.app.push --generate   # create VAPID keys and add them to .env
"""
import asyncio
import base64
import json
import sys

from . import config, db

QUEST_TAG = "wj-quest"  # one notification per quest that replaces itself instead of stacking
ICON = "/icons/icon-192.png"


class PushError(RuntimeError):
    pass


def enabled() -> bool:
    if config.CLOUD:  # the public beta has no reminders yet: they'd need one schedule per phone
        return False
    return bool(config.VAPID_PUBLIC_KEY and config.VAPID_PRIVATE_KEY)


# ---------- subscriptions ----------

def save_subscription(sub: dict, user_agent: str = ""):
    endpoint = sub.get("endpoint", "")
    keys = sub.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise PushError("invalid subscription")
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO push_subs (endpoint, keys, user_agent, created_at) VALUES (?,?,?,?)",
                  (endpoint, db.dumps(keys), user_agent[:200], db.now()))


def remove_subscription(endpoint: str):
    with db.tx() as c:
        c.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))


def subscriptions() -> list[dict]:
    with db.tx() as c:
        return [{"endpoint": r["endpoint"], "keys": json.loads(r["keys"])}
                for r in c.execute("SELECT endpoint, keys FROM push_subs").fetchall()]


# ---------- sending ----------

def _send_one(sub: dict, payload: dict) -> str:
    """Returns "ok", "gone" (subscription expired: delete it) or "error"."""
    from pywebpush import WebPushException, webpush
    try:
        webpush(subscription_info=sub, data=json.dumps(payload), vapid_private_key=config.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": config.VAPID_SUBJECT}, ttl=3600, timeout=10)
        return "ok"
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        return "gone" if status in (404, 410) else "error"
    except Exception:
        return "error"


async def send_all(payload: dict) -> dict:
    """Send one notification to every subscribed device. Expired subscriptions are removed."""
    assert_private(payload)
    if not enabled():
        return {"sent": 0, "skipped": "VAPID keys not set"}
    results = {"ok": 0, "gone": 0, "error": 0}
    for sub in subscriptions():
        r = await asyncio.to_thread(_send_one, sub, payload)
        results[r] += 1
        if r == "gone":
            remove_subscription(sub["endpoint"])
    return results


FORBIDDEN_KEYS = {"lat", "lng", "latitude", "longitude", "photo", "photo_url", "image", "start", "address", "place"}


def assert_private(payload: dict):
    """Guardrail: a notification payload may never carry coordinates, places or photos."""
    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k in FORBIDDEN_KEYS:
                    raise PushError(f"notification payload must not include '{k}'")
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str) and ("/photos/" in v or v.startswith("data:image")):
            raise PushError("notification payload must not include photos")
    walk(payload)


# ---------- payloads ----------

DONE = {"pass", "self", "unconfirmed", "skipped"}


def quest_payload(quest: dict) -> dict:
    """The live quest notification: task list, "2 of 5 done", and a Take photo action for the next task."""
    tasks = quest["tasks"]
    done = [t for t in tasks if t.get("status") in DONE]
    nxt = next((t for t in tasks if t.get("status") not in DONE), None)
    lines = [f"{'✓' if t.get('status') in DONE else '○'} {t['prompt']}" for t in tasks]
    if nxt is None:
        title, actions, url = f"{quest['title']}: all {len(tasks)} done!", [], "/#quest"
        lines.insert(0, "Tap to finish and write in your journal.")
    else:
        title = f"{quest['title']}: {len(done)} of {len(tasks)} done"
        is_photo = nxt.get("kind") == "photo"
        actions = [{"action": "snap", "title": "Take photo"}] if is_photo else []
        url = f"/#snap/{nxt['id']}" if is_photo else "/#quest"
    payload = {"title": title, "body": "\n".join(lines), "tag": QUEST_TAG, "icon": ICON, "renotify": False,
               "silent": True, "url": "/#quest", "actions": actions, "next_task": nxt["id"] if nxt else None,
               "action_urls": {"snap": url} if actions else {}}
    assert_private(payload)
    return payload


def reminder_payload(kind: str) -> dict:
    if kind == "daily":
        # Kind and easy to ignore: walking is encouraged, never required.
        p = {"title": "Nice day for a short walk?", "body": "Only if you feel like it. A gentle quest is ready whenever you are.",
             "tag": "wj-daily", "url": "/#home"}
    else:
        p = {"title": "Want to jot down today's walk?", "body": "A few words is plenty, or skip it. Totally fine.",
             "tag": "wj-journal", "url": "/#journal"}
    return p | {"icon": ICON, "actions": [], "action_urls": {}}


# ---------- key generation ----------

def generate_keys() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    k = ec.generate_private_key(ec.SECP256R1())
    private = b64(k.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    public = b64(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    return public, private


def write_keys_to_env(path=None) -> bool:
    """Append VAPID keys to .env if they aren't there yet. Never overwrites existing values."""
    path = path or (config.ROOT / ".env")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    have = {line.split("=", 1)[0].strip() for line in text.splitlines() if "=" in line and line.split("=", 1)[1].strip()}
    if {"VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"} <= have:
        return False
    public, private = generate_keys()
    lines = [l for l in text.splitlines() if l.split("=", 1)[0].strip() not in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY")]
    lines += ["# Web Push (generated by: python -m basecamp.app.push --generate)",
              f"VAPID_PUBLIC_KEY={public}", f"VAPID_PRIVATE_KEY={private}"]
    if "VAPID_SUBJECT" not in have:
        lines.append(f"VAPID_SUBJECT={config.VAPID_SUBJECT}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


if __name__ == "__main__":
    if "--generate" in sys.argv:
        print("Added VAPID keys to .env" if write_keys_to_env() else ".env already has VAPID keys; left them alone")
