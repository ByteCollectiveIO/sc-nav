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

**Per-category webhooks (done in v1, 2026-06-30).** Originally scoped as one
shared webhook with v1.1 adding named webhooks per channel — but per-category
routing was pulled forward into v1: each category (events / marketplace / goals /
records) has its own webhook URL, so an org can route on-topic messages to
on-topic channels (`#events`, `#market`, …). Reusing the same URL across
categories collapses to one channel, so the single-channel case still works. A
category is "on" exactly when it has a valid webhook — there's no separate enable
toggle; clearing the URL turns it off.

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
- `discord_webhook_<category>` — **secret**, one per category (events /
  marketplace / goals / records). Never returned in any API response; settings
  GET returns `discord_webhooks: {<category>: {set: bool, tail: "…xxxx"}}` only.
  A category fires iff its URL is a valid Discord webhook (presence = enabled;
  no separate `notify_<category>` toggle). `notify.set_webhook`/`webhook_status`.
- `discord_webhook_url` (legacy, v0.13.0's single shared URL) — migrated to every
  unset per-category key at startup by `notify.migrate_legacy_webhook`, then
  consumed. Kept only as a migration source.
- Optional `discord_reminder_lead_min` (default 30).

### Dispatcher
A single module `server/notify.py`:
- `async def send(category, text, *, mentions=..., dedup_key=...)` — looks up
  `category`'s webhook and POSTs `{content, allowed_mentions}` via `urllib`
  **in a thread** (`asyncio.to_thread`), so a slow/broken webhook never blocks a
  request or the event loop.
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
1. ✅ **DONE (2026-06-30). Shipped v0.13.0, then reworked to per-category (below,
   uncommitted).** `notify.py` dispatcher (async `send(category, text, …)` that
   POSTs in a worker thread, never raises, in-memory dedup, single 429 retry,
   locked-down `allowed_mentions`; URL validate/mask/`is_configured(category)`/
   `webhook_status`/`set_webhook`/`migrate_legacy_webhook` helpers) + admin
   settings (per-category `discord_webhook_<cat>` stored in `meta`, validated
   against real Discord hosts on write, never echoed back — GET returns
   `discord_webhooks: {cat: {set, tail}}` + `discord_reminder_lead_min`) + a
   DISCORD NOTIFICATIONS section in ORG SETTINGS: one row per category (URL input,
   Save, per-category "Test" via `POST /api/settings/discord/test {category}`, and
   an explicit Clear — blank-on-Save leaves a category unchanged rather than
   wiping it), plus reminder-lead minutes. Tests in `test_app.py`
   (`NotifyValidationTests`, `DiscordSettingsTests`) pin anti-SSRF validation, the
   mask never leaking the URL, the per-category store/never-echo round-trip, and
   the legacy→per-category migration.
2. ✅ **DONE (2026-06-30, uncommitted).** Inline event notifications
   (create / cancel), gated on the `events` toggle. Shared helpers in `app.py`:
   `_deep_link('#/events')`, `_discord_ts()` (emits Discord `<t:unix:F/R>` tags so
   each member sees the start time in their own timezone), and `_notify_bg()`
   which fires the coroutine as a background task so the POST never delays the
   request. `_notify_event_created` / `_notify_event_cancelled` build the
   messages (dedup_key `event-created:<id>` / `event-cancelled:<id>`, no
   mentions — a broadcast). Wired into `POST /api/events` and
   `DELETE /api/events/{id}`. Content-injection safe: a hostile title can't ping
   because `notify.send` locks `allowed_mentions` (parse:[], users only).
   `EventNotifyTests` in `test_app.py` cover fire-when-on, silent-when-off, the
   cancel message, and the timestamp/deep-link helpers. Edit/start-time-change
   notification deferred (not in step 2 scope).
3. Scheduled reminders (the `events.reminded_at` loop) — the marquee feature.
4. Marketplace offer/confirm pings (with `<@id>` mentions).
5. Goals-100% + hauling-record pings.
6. ✅ **Per-category channel routing** — done early as part of step 1 (see above),
   not deferred to v1.1.

## Relevant code
- `server/auth.py` — OAuth scopes (why there's no bot).
- `server/app.py` — `/api/settings` (admin store), `presence_broadcaster` (the
  background-loop pattern to copy for reminders), event/market/goal routes (inline
  fire points), `members` table (Discord ids for `<@id>` mentions).
- `server/db.py` — `events` (add `reminded_at`), `meta` (settings store).
