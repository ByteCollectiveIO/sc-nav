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
WEBHOOK_KEY = "discord_webhook_url"
# Per-feature toggles. Off by default for goals milestones (noise); the rest
# default on once a webhook is configured. The route layer owns the defaults;
# here we just read the stored "1"/"0".
CATEGORIES = ("events", "marketplace", "goals", "records")
REMINDER_LEAD_KEY = "discord_reminder_lead_min"


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


def webhook_url() -> str:
    return (db.get_setting(WEBHOOK_KEY, "") or "").strip()


def is_configured() -> bool:
    return is_valid_webhook_url(webhook_url())


def category_enabled(category: str) -> bool:
    """Whether a category fires. Defaults: everything on except 'goals'
    milestones, which are noisy. Only meaningful once a webhook is set."""
    default = "0" if category == "goals" else "1"
    return db.get_setting(f"notify_{category}", default) == "1"


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


async def send(text: str, *, mentions: list[str] | None = None,
               dedup_key: str | None = None) -> bool:
    """Post ``text`` to the configured Discord webhook. Returns True on a
    best-effort success, False if not configured / deduped / failed. NEVER
    raises — a notification problem must not break the caller's action.

    ``mentions`` is a list of Discord user-id strings; prefix the relevant id in
    the text with ``<@id>`` to actually ping them. We scope allowed_mentions to
    exactly those ids and never allow @everyone/@here/role pings.
    """
    url = webhook_url()
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
