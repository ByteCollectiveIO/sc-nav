# Discord notifications (push integration)

**Status:** scoped, not built (2026-06-30). Backlog #18.
**Priority:** #1 — highest engagement-per-effort. Everything shipped so far is
pull-only; this makes the app *reach out* and pull members back in-game.

## Goal
When something the member cares about happens in the app, push a message to
Discord — the place the org already lives. Drives the value of every existing
feature (events, marketplace, goals, hauling records) by closing the loop.

---

## The central constraint: we have no bot

Today's OAuth scopes (`server/auth.py`): `identify guilds guilds.members.read`.
We authenticate users via OAuth only — **there is no bot user in the guild**, and
no `webhook.incoming` scope. So before designing notifications we must choose a
delivery channel. Three options:

| Option | What it is | DMs? | Channel posts? | Cost |
|---|---|---|---|---|
| **A. Admin incoming-webhook URL** | Admin creates a webhook on a Discord channel, pastes the URL into app settings; server POSTs to it. | ❌ | ✅ | ~zero — no bot, no new scope |
| **B. Bot token** | Run a real bot in the guild. | ✅ | ✅ | host a bot process, message intents, bot in guild |
| **C. `webhook.incoming` OAuth scope** | Each user authorizes the app to create a webhook in a channel they pick, at login. | ❌ | ✅ (per-user channel) | re-consent flow, store per-user webhook |

### Decision: ship Option A first
Channel broadcasts cover ~80% of the value (event reminders, "roster filled",
"new hauling record", "marketplace offer") and cost almost nothing. **Defer DMs
(Option B) indefinitely** — a bot is a whole new deployable surface and the org
already gets pinged in-channel. Revisit only if members ask for private alerts.

One webhook is enough for v1. v1.1 can add a small set of named webhooks so the
org can route categories to different channels (e.g. `#events`, `#market`).

---

## Notification taxonomy (what fires)

Group by the feature that owns the event. Each is a toggle in settings.

**Events** (highest value — time-sensitive, communal)
- Event created / updated (start time changed)
- Reminder **T-minus 30 min** (and optionally T-24h) before `start_at`
- Signup deadline approaching (T-2h before `signup_deadline`)
- Target roster filled (all roles met)
- Event cancelled
- "Starting now"

**Marketplace** (transactional — the seller wants to know *now*)
- New offer on your listing
- Offer accepted / counter-offered
- Trade confirmed (both sides) — the dual-confirm handshake completes
- Auction ending soon / auction won

**Goals (Resource Manager)**
- Goal reached 100%
- Big milestone crossed (50%, 75%) — optional, off by default (noise)

**Hauling / org records** (social proof, light gamification)
- New org hauling record (route, aUEC/hr, single-run total)

> Marketplace/goal/offer events are *per-member* by nature ("offer on **your**
> listing"). With channel-only delivery (Option A) we post to the channel and
> **@mention** the relevant member by Discord id (`<@id>`) so it reads as
> directed without needing DMs. Member ids are already in the `members` table.

---

## Architecture

### Settings (admin-only)
Store in the existing settings/`meta` mechanism (`/api/settings`, `db.meta`):
- `discord_webhook_url` — **secret**. Never returned in any API response; the
  settings GET returns only a boolean `discord_webhook_set` + a masked tail.
- `notify_<category>` booleans (events / marketplace / goals / records).
- Optional `discord_reminder_lead_min` (default 30).

### Dispatcher
A single module `server/notify.py`:
- `async def send(text, *, allowed_mentions=...)` — POSTs the Discord webhook
  JSON (`{content, allowed_mentions}`) via `httpx`/`urllib` **in a thread**, so a
  slow/broken webhook never blocks a request or the event loop.
- **Never raises into the caller.** Log and swallow; a notification failing must
  not fail the user action that triggered it.
- Light **rate-limit / dedup**: an in-memory recent-key set so a double-submit or
  a retry doesn't double-post. Respect Discord's 429 `retry_after`.
- Format messages as Discord-flavored markdown; include a deep link back into the
  app (`/#/events`, `/#/market`) so the ping is one click from action.

### Fired inline vs. scheduled
- **Inline** (most): call `notify.send(...)` right after the state change in the
  relevant route — e.g. in `POST /api/market/{id}/offer`, after a goal hits 100%
  in the contribute path, on event create/cancel.
- **Scheduled** (reminders): a background loop modeled on the existing
  `presence_broadcaster` (`server/app.py`). ~every 60s, scan `events` for ones
  whose `start_at` is within the lead window and not yet reminded. **Idempotency
  is mandatory** — add a `reminded_at` (or a `notified` JSON set for multiple
  reminder stages) column to `events`, set it when fired, so a restart or a slow
  tick never double-pings.

---

## Security
- The webhook URL is a credential. Store it; **mask it on read**; validate on
  write that it matches `https://discord.com/api/webhooks/...` (or
  `discordapp.com`) to prevent the app being turned into an **SSRF / open relay**.
- Mentions: send `allowed_mentions` scoped to the specific user id(s) only —
  never `@everyone`/`@here` from app-generated content.
- Rate-limit admin test-sends.

---

## Build order
1. ✅ **DONE (2026-06-30, uncommitted).** `notify.py` dispatcher (async `send()`
   that POSTs in a worker thread, never raises, in-memory dedup, single 429
   retry, locked-down `allowed_mentions`; URL validate/mask/`is_configured`/
   `category_enabled` helpers) + admin settings (`discord_webhook_url` stored in
   `meta` and validated against real Discord hosts on write, never echoed back —
   GET returns only `discord_webhook_set` + a masked tail + the `notify_*`
   toggles + `discord_reminder_lead_min`) + a DISCORD NOTIFICATIONS section in
   ORG SETTINGS (URL field, per-category toggles, reminder-lead minutes, Save,
   and a rate-limited "Send test" via `POST /api/settings/discord/test`).
   Nothing fires from app events yet — this proves delivery. Tests in
   `server/test_app.py` (`NotifyValidationTests`, `DiscordSettingsTests`) pin the
   anti-SSRF validation, the mask never leaking the URL, and the store/never-echo
   round-trip.
2. Inline event notifications (create / cancel) — smallest surface, validates the
   inline pattern.
3. Scheduled reminders (the `events.reminded_at` loop) — the marquee feature.
4. Marketplace offer/confirm pings (with `<@id>` mentions).
5. Goals-100% + hauling-record pings.
6. (v1.1) multiple named webhooks → per-category channel routing.

## Relevant code
- `server/auth.py` — OAuth scopes (why there's no bot).
- `server/app.py` — `/api/settings` (admin store), `presence_broadcaster` (the
  background-loop pattern to copy for reminders), event/market/goal routes (inline
  fire points), `members` table (Discord ids for `<@id>` mentions).
- `server/db.py` — `events` (add `reminded_at`), `meta` (settings store).
