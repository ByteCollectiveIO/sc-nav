"""server/notify.py — Discord webhook dispatcher.

Everything the app has shipped so far is pull-only; this is the one piece that
lets the app *push* back to the org. We have no bot (OAuth scopes are
identify/guilds only — see server/auth.py), so delivery is a single
admin-configured incoming-webhook URL stored in `meta`. Full design in
docs/discord-notifications.md.

Design rules (do not regress):
- ``send()`` NEVER raises into the caller. A broken/slow webhook must not fail
  the user action that triggered the notification — we log and swallow.
- The blocking HTTP POST runs in a worker thread so a dead webhook can't stall
  the event loop or the request that fired it.
- Light in-memory dedup so a double-submit / retry doesn't double-post.
- ``allowed_mentions`` is locked down: never @everyone/@here/role pings; only the
  explicit user ids we pass. This keeps app-generated content from mass-pinging.
- The webhook URL is a credential: validate it on write (anti-SSRF — only real
  Discord webhook hosts), store it, and mask it on read.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request

import db

# --- settings keys (meta table) ----------------------------------------------
# Each category has its OWN webhook URL, so an org can route on-topic messages to
# on-topic channels (e.g. events -> #events, marketplace -> #market). Putting the
# same URL in several categories is fine — they'll all post to that one channel.
# A category is "on" exactly when it has a valid webhook, so there's no separate
# enable toggle: clearing the URL turns that category off.
CATEGORIES = ("events", "marketplace", "goals", "records", "lfg", "pirates")
_WEBHOOK_PREFIX = "discord_webhook_"          # + category, e.g. discord_webhook_events
_LEGACY_WEBHOOK_KEY = "discord_webhook_url"   # v0.13.0's single shared webhook
REMINDER_LEAD_KEY = "discord_reminder_lead_min"


def _webhook_key(category: str) -> str:
    return f"{_WEBHOOK_PREFIX}{category}"


# --- webhook URL validation / masking ----------------------------------------
# Only genuine Discord webhook endpoints. This is the anti-SSRF / open-relay
# guard: without it an admin (or anyone who compromises the settings form) could
# point the server at an arbitrary internal host. https only, known hosts only,
# path must be /api/webhooks/<id>/<token>.
_WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$"
)


def is_valid_webhook_url(url: str) -> bool:
    return bool(_WEBHOOK_RE.match((url or "").strip()))


def mask(url: str) -> str:
    """A safe-to-display tail of the webhook token, e.g. '…AbCd'. Never returns
    enough to reconstruct the URL."""
    url = (url or "").strip()
    if not url:
        return ""
    tail = url.rsplit("/", 1)[-1]
    return "…" + tail[-4:] if len(tail) >= 4 else "…"


def webhook_url(category: str) -> str:
    """The configured webhook URL for a category, or '' if none/unknown."""
    if category not in CATEGORIES:
        return ""
    return (db.get_setting(_webhook_key(category), "") or "").strip()


def is_configured(category: str) -> bool:
    """Whether a category will fire — i.e. it has a valid webhook set."""
    return is_valid_webhook_url(webhook_url(category))


def any_configured() -> bool:
    return any(is_configured(c) for c in CATEGORIES)


def webhook_status() -> dict[str, dict]:
    """Per-category webhook state for the settings API: whether one is set and a
    masked tail to confirm which. NEVER exposes the raw URL (it's a credential)."""
    return {c: {"set": is_configured(c), "tail": mask(webhook_url(c))}
            for c in CATEGORIES}


def migrate_legacy_webhook() -> None:
    """v0.13.0 stored ONE `discord_webhook_url` shared by every category. Now each
    category has its own. On upgrade, seed every still-unset category from the
    legacy value (so notifications don't silently stop after the update) and then
    consume the legacy key. Idempotent: a no-op once the legacy key is gone."""
    legacy = (db.get_setting(_LEGACY_WEBHOOK_KEY, "") or "").strip()
    if not legacy:
        return
    if is_valid_webhook_url(legacy):
        for c in CATEGORIES:
            if not webhook_url(c):
                db.set_setting(_webhook_key(c), legacy)
    db.set_setting(_LEGACY_WEBHOOK_KEY, "")   # consumed — don't migrate twice


def set_webhook(category: str, url: str) -> None:
    """Store (or clear, with '') a category's webhook URL. Caller validates."""
    if category in CATEGORIES:
        db.set_setting(_webhook_key(category), (url or "").strip())


def reminder_lead_min() -> int:
    try:
        return max(1, int(db.get_setting(REMINDER_LEAD_KEY, "30") or "30"))
    except (TypeError, ValueError):
        return 30


# --- dedup --------------------------------------------------------------------
# A double-submit, a client retry, or two broadcaster ticks racing can all try to
# post the same thing. Remember recent dedup keys briefly and drop repeats.
_DEDUP_TTL_S = 120.0
_recent: dict[str, float] = {}


def _dedup_seen(key: str) -> bool:
    now = time.monotonic()
    # opportunistic prune so the dict can't grow without bound
    if len(_recent) > 256:
        for k, exp in list(_recent.items()):
            if exp <= now:
                _recent.pop(k, None)
    exp = _recent.get(key)
    if exp and exp > now:
        return True
    _recent[key] = now + _DEDUP_TTL_S
    return False


# --- dispatch -----------------------------------------------------------------
def _post(url: str, payload: dict) -> None:
    """Blocking POST of one Discord webhook message. Runs in a worker thread.
    Honors a single 429 retry_after (capped) so a brief rate-limit doesn't drop
    the message; any other failure is logged by the caller."""
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(2):
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "sc-nav/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                return
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                try:
                    info = json.loads(exc.read().decode("utf-8") or "{}")
                    delay = min(float(info.get("retry_after", 1.0)), 5.0)
                except Exception:
                    delay = 1.0
                time.sleep(delay)
                continue
            raise


async def send(category: str, text: str, *, mentions: list[str] | None = None,
               dedup_key: str | None = None) -> bool:
    """Post ``text`` to ``category``'s Discord webhook. Returns True on a
    best-effort success, False if that category has no webhook / deduped /
    failed. NEVER raises — a notification problem must not break the caller's
    action.

    ``mentions`` is a list of Discord user-id strings; prefix the relevant id in
    the text with ``<@id>`` to actually ping them. We scope allowed_mentions to
    exactly those ids and never allow @everyone/@here/role pings.
    """
    url = webhook_url(category)
    if not is_valid_webhook_url(url):
        return False
    if dedup_key and _dedup_seen(dedup_key):
        return False

    users = [str(m) for m in (mentions or []) if str(m).isdigit()][:50]
    payload = {
        "content": text[:1900],   # Discord hard-caps content at 2000 chars
        "allowed_mentions": {"parse": [], "users": users},
    }
    try:
        await asyncio.to_thread(_post, url, payload)
        return True
    except Exception as exc:   # log and swallow — see module docstring
        print(f"[sc-nav] discord notify failed: {exc}")
        return False
