# Event calendar export

Status: built 2026-10-18 (branch `events-calendar-feed`), unreleased.

Get org events into Google Calendar, Apple Calendar or Outlook. Two pieces:

1. **Add to calendar** on every event page: a one-time copy.
2. **Calendar subscription** in Settings → Calendar: a live feed the member's
   calendar app re-fetches. **Off unless an org admin turns it on.**

## Options considered (2026-10-15)

| Option | Verdict |
|---|---|
| Per-event "Add to calendar" (Google/Outlook prefill URL + `.ics` download) | **Built.** Tiny, no new exposure; a snapshot, so a reschedule doesn't follow it. |
| Subscribed ICS feed (`/cal/<token>.ics`) | **Built, admin opt-in.** Works with every calendar app and with self-hosting orgs at zero setup. Google refreshes it slowly (observed 12–24 h, not configurable), so Discord pings stay authoritative. |
| Google Calendar API push (OAuth) | Rejected. Calendar scopes need Google app verification. Every self-hosting org would need its own Google Cloud project and verification. We'd store Google refresh tokens. Google-only. |
| Apple (iCloud) write | Not possible. The only way is CalDAV with app-specific passwords. |
| Email invites (iTIP) | Rejected. We have no member email addresses (Discord-only identity) and no mail sending. |
| Discord Scheduled Events | Rejected. Needs a bot; the app is webhook-only by design. |

## Settled calls (user, 2026-10-18)

- The feed is a **conscious admin choice**: org setting `calendar_feed_enabled`,
  default off (Settings → Apps → CALENDAR SUBSCRIPTIONS).
- **Turning it off deletes every member's link.** Turning it back on does not
  revive old links, which may have been the reason it was switched off.
- Each member picks the scope: **events I'm in** (organizer, or a going/maybe
  signup) or **all org events**. Changing scope keeps the same link.
- Detail is **the basics**: title, time, rally point, event location, and a link
  back to the event. No description, roster or roles.
- **Revoking a member's access deletes their link** (and account deletion does too).
- **Ad-hoc ops are never in the feed.** The feed reads the `events` table only;
  quick ops live in `operations`.
- **Refresh hint included**: `REFRESH-INTERVAL`/`X-PUBLISHED-TTL` = 1 h. Apple
  and Outlook honor it; Google ignores it, and the UI says so.

## As built

- `server/calendar_ics.py`: the one RFC 5545 serializer (escaping, 75-octet
  UTF-8-safe folding, UTC stamps, no VTIMEZONE). Used by the feed and the
  single-event download. Stable `UID:event-<id>@<host>`, so a re-fetch updates
  the entry instead of duplicating it. A cancelled event stays in the feed as
  `STATUS:CANCELLED` with a "Cancelled:" title prefix. A missing duration gets 60 min.
- **Feed** `GET /cal/<token>.ics`. The token is `secrets.token_urlsafe(32)`
  (43 chars); only its sha256 is stored, in `calendar_feeds` (one row per member).
  So the URL is shown once, at creation; "Make a new link" replaces it.
  - `auth_gate` lets the path through by **exact shape** (`_CALENDAR_FEED_RE`,
    GET only). The route then answers a bare 404 unless the setting is on, the
    token resolves and the member isn't revoked. 404 rather than 401, so an
    anonymous caller learns nothing.
  - Window: events from 30 days ago onward (`_CAL_HISTORY_DAYS`), any status,
    capped at 500. Rate bucket `calendar` (120/h per feed owner). `last_used`
    is stamped per fetch and shown to the member as "last checked by a calendar".
- **Member API** `GET|POST|PUT|DELETE /api/me/calendar`. POST mints (403 when the
  org setting is off; `token` rate bucket). PUT changes scope.
- **Single event** `GET /api/events/{id}/ics` (session-authenticated, needs no
  setting). The Google/Outlook.com links are built client-side in `eventCalLinks`.
  Its description must stay in step with `calendar_ics.description`.
- **Subscribe buttons**: Apple uses `webcal://…`. Google uses
  `https://calendar.google.com/calendar/r?cid=<encoded webcal URL>`. Outlook gets
  "Add calendar → Subscribe from web → paste".
- Feed URLs use `SC_NAV_PUBLIC_URL` when set (via `_server_base_url`), else the
  request host. Google fetches from its own servers, so the server must be
  reachable from the internet; a Cloudflare Access gate in front would need an
  exception for `/cal/`.

## Open questions / not built

- Guild membership is still only checked at login. A member who leaves Discord
  but isn't revoked in the app keeps a working link, same as their watcher token.
- No per-event VALARM reminders: subscribed-feed alarms are dropped by Google
  and handled inconsistently by Apple.
- Not verified in real Google/Apple clients yet (needs a deployed, public URL).
