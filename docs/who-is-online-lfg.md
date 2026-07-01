# Who's online + group finder (LFG / "rally now")

**Status:** scoped, not built (2026-06-30). Backlog #19.
**Priority:** #2 — the social glue for *spontaneous* play. Turns "I wonder if
anyone's on" into "3 people are on, let's group up."

## Goal
Show who from the org is online **right now**, what they're doing, and let them
flag intent ("free for ops", "need 2 for a bunker run") so impromptu groups form
without a scheduled event. Spontaneous activity is the lifeblood of an org; the
app currently has no surface for it.

---

## What already exists (and the gap)

Two separate online signals exist today (`server/app.py`):
1. **`online_count`** — number of members with a browser tab open. Anonymous
   (just a count), broadcast over WS as `{type:"online"}`.
2. **`hub.presence`** — live position fixes per member, **but surface-only**
   (`_presence_record` returns `None` unless the member is standing on a body
   surface) and only when a watcher is running and the share toggle is on. Powers
   the TEAMMATES roster on the navigator map.

**The gap:** there is no roster of *who is online and what they intend to do*
that works in space, at a station, or before they even launch the game. Presence
is map-bound; the online count is faceless. LFG needs an identity-bearing,
location-optional "I'm here and available" signal.

---

## Model: a lightweight presence + status layer

Add an **online roster** decoupled from the surface-fix presence:

- A member is "online" if they have a live WS connection (tab open) **or** an
  active watcher heartbeat. Key by `discord_id` (collapse multiple tabs).
- Each member can set an **availability status** + **activity intent**:
  - status: `available` · `busy` · `afk` (default derived: online = available
    unless set otherwise; idle timeout → afk).
  - activity: a short free-text or chip — `hauling`, `mining`, `salvage`,
    `combat/PvE`, `combat/PvP`, `exploring`, `just chilling`, `open to anything`.
  - optional current location (system / nearest POI) **when** surface presence or
    a coarse watcher fix is available; otherwise omit — never block on position.
- Respect existing privacy: the `share_presence`/directory opt-out already lets a
  member hide their location. Online status is a **separate, lighter** consent —
  default to showing "online + status" but let a member go invisible ("appear
  offline") independent of position sharing.

### Looking-for-group (LFG) — first-class feature

A transient, no-schedule way to broadcast intent and connect people *right now*.
This sits alongside the online roster: the roster says *who's on*, LFG says *who
wants to do what with whom*.

**Two directions** — every LFG entry declares which way it points:
- **Looking for members** ("LFM") — *I have a group / am starting one, need
  people.* Carries slots-needed. e.g. *"Bunker run — need 2"*.
- **Looking to join** ("LFG") — *I'm solo and want in on something.* No slots;
  it's a hand raised. e.g. *"Solo, down for any PvE — ping me"*.

This two-way model matters: a board of only "need 2" posts misses the lone player
who'd happily fill a slot but isn't hosting. Showing both lets the dashboard
**match** them.

**Amplifying info on each entry:**
- **Direction** (LFM / LFJ) — required.
- **Playstyle tags** (multi-select chips): `PvE`, `PvP`, `FPS`, `flight`,
  `hauling`, `mining`, `salvage`, `bunkers`, `bounty`, `exploration`,
  `medical/rescue`, `RP`, `casual`, `serious`, `new-player-friendly`. Reuse/extend
  the activity chips from the online-status model — one shared vocabulary.
- **Slots needed** (LFM only) + filled count.
- Short free-text note (what/where, comms expectations).
- Optional rally point (system / POI).
- Optional **headset/comms** flag (e.g. "Discord voice required").
- Auto-set: poster, created-at, expiry.

**Lifecycle:**
- Other members **Join** (LFM) or **Invite/ping** (LFJ) → connects the two sides.
  Joining an LFM fills a slot; reaching out to an LFJ is a nudge.
- Auto-expires (default ~2–3h) or when the poster closes it / goes offline. These
  are ephemeral — **not** the scheduled `events` table. (For something planned,
  the member makes a real event instead — offer a "promote to event" shortcut.)

### LFG dashboard (the connect surface)

The marquee view. A live board of all open LFG entries that helps people *find
each other*, not just read a list:
- **Filter by playstyle** (chips) + direction (LFM / LFJ / both) + "has open
  slots" + rally location.
- **Two-column or grouped layout**: "Groups needing players" (LFM) vs. "Players
  looking to join" (LFJ), so a host scans available players and a soloist scans
  open groups at a glance.
- Each card: poster handle (+ online status dot from the roster), direction,
  playstyle chips, slots filled/needed, note, rally point, age/expiry, and the
  action button (Join / Ping / Close-if-mine).
- **Suggested matches** (nice-to-have): for the signed-in member, surface LFM
  posts whose playstyle tags overlap their declared activity, and vice-versa.
- Live-updates over WS as entries are posted/filled/expired.

### Discord push per entry (opt-in, ties to #18)

When posting an LFG entry, the author can tick **"announce to Discord"** → posts
a formatted message to the org's LFG/channel webhook (the same dispatcher and
admin-configured webhook from **#18**): direction, playstyle chips, slots, note,
and a deep link back to the dashboard (`/#/lfg`). This is the reach multiplier —
most members are in Discord, not staring at the app. Channel-only delivery means
the post `@mention`s nobody by default (it's a broadcast); a member replying in
Discord still funnels back to the app link to actually join.

Guardrails: per-member rate-limit on announced LFG posts (anti-spam — don't let
one person blast the channel); only announce on create, not on every edit.

**Reuse vs. new:** don't overload `events` (it carries scheduling, roles, status
lifecycle). LFG entries are short-lived and matching-oriented. Keep them
**in-memory** like presence for v1 (they don't need history and expire fast);
persist to a small `lfg_posts` table only if losing them on restart proves
annoying. Either way they're keyed by `discord_id` (one active LFM + one active
LFJ per member is a sane cap).

---

## Surfaces
- A new **"Who's Online" view** (`#/online` or fold into the launcher) listing
  online members: name/handle (from `members` + primary handle), status chip,
  activity, location-if-shared, last-seen. Sort: available first.
- The **LFG dashboard** (`#/lfg`, or a tab beside the roster): the two-direction
  connect board described above — filter by playstyle/direction, Join/Ping, and a
  "post LFG entry" composer (direction + playstyle chips + slots + note + rally +
  "announce to Discord" checkbox).
- The **launcher** gets a live "🟢 N online" badge (already have the count; add a
  click-through) and, when LFG entries are open, a "🔎 N looking for group" badge.
- WS-driven so both update live (reuse the existing WS hub + broadcast).

---

## Architecture
- Extend the hub with an `online: dict[discord_id, record]` (status, activity,
  last_seen) populated on WS connect + a periodic client heartbeat, pruned on
  disconnect / stale timeout — mirror `presence` lifecycle, but identity-bearing
  and **not** surface-gated.
- New WS message types: `online_roster` (full/delta), `lfg` (post/join/close/expire).
- REST for the non-live bits: `GET /api/online` (snapshot), `POST /api/online/status`
  (set my status/activity/visibility); `GET /api/lfg` (open entries, filterable by
  direction/playstyle), `POST /api/lfg` (create — direction, playstyle tags, slots,
  note, rally, `announce` flag), `POST /api/lfg/{id}/join`, `DELETE /api/lfg/{id}`
  (close). On create with `announce=true`, call the **#18** dispatcher (rate-limited
  per member).
- Heartbeat: piggyback on existing WS traffic where possible; add a tiny
  client→server "still here / status" ping.

## Privacy & guardrails
- "Appear offline" must fully suppress a member from the roster.
- Location only ever shown if the member is already sharing position.
- Don't leak presence to non-members (same auth gate as everything else).

## Build order
1. Online roster: hub `online` map + WS connect/heartbeat/prune + `GET /api/online`
   + the Who's Online view. (Status defaults; no manual status yet.)
   **BUILT 2026-06-30 (uncommitted, needs /deploy).** `hub.online` dict keyed by
   discord_id (record: status/activity/visible/since/last_seen — the fields steps
   2/3 reuse), lifecycle wired into the `/ws` connect→ping-heartbeat→disconnect path
   (identity-bearing, NOT surface-gated like `presence`); `ONLINE_STALE_S=90` backstop
   prune folded into `presence_broadcaster`; `mark_online`/`drop_online`/`online_roster`
   (available-first sort)/`_public_online` (location rides only when the member already
   shares presence); `online_count` now = size of the *visible* roster (so step 2's
   "appear offline" lowers it). WS msgs: `online_roster` (full snapshot, org-scale
   cheap) + existing `online` count; both sent to a new tab on connect, broadcast to
   all on arrival/departure/stale. `GET /api/online` snapshot. Frontend: `#/online`
   "Who's Online" view (name, status chip, activity, location-if-shared, since),
   launcher `🟢 N online` badge → `#/online`, WS handling + `applyView` wiring +
   APP_TITLE/APP_LABEL. Tests: `OnlineRosterTests` in test_app.py (6) — suite 186 green.
   **Deferred to later steps:** manual status/activity (step 2), "appear offline"
   toggle (the `visible` flag + count seam already exist), LFG (step 3).
2. Manual status + activity + "appear offline" (`POST /api/online/status`). Define
   the shared playstyle-tag vocabulary here (reused by LFG).
   **BUILT 2026-06-30 (uncommitted, needs /deploy).** Prefs are PERSISTED (not just
   ephemeral on the WS record) so a refresh/reconnect keeps a member's status and —
   importantly — their "appear offline" privacy choice: new `members` columns
   `online_status`/`online_activity`/`appear_offline` (`_ensure_column` migration) +
   `db.set_online_prefs`; `mark_online` seeds the in-memory record from them on the
   arrival path (once per connect, not per ping). `POST /api/online/status`
   (`OnlineStatusIn`: status validated to available/busy/afk, activity capped 60,
   appear_offline bool) persists + updates the live record + rebroadcasts roster/count.
   `GET /api/online` now also returns `me` (caller's own prefs) to seed the control.
   Shared vocabulary = `PLAYSTYLE_TAGS` constant served at `GET /api/playstyles`
   (hauling/mining/salvage/…/PvE/PvP/FPS/… — reused as step-2 activity quick-picks
   AND step-3 LFG tags). "Appear offline" is a lighter consent, independent of the
   navigator's position-sharing toggle. Frontend: a YOUR STATUS panel on #/online
   (status select, activity input, quick-pick chips from /api/playstyles, appear-offline
   checkbox, Save). Tests: OnlineRosterTests +5 (persist/apply, bad-status fallback,
   blank-activity→null, appear-offline hides+sticks, playstyles served) — suite 191 green.
3. LFG dashboard: in-memory entries (LFM + LFJ, playstyle tags, slots) + Join/Close
   + the two-direction connect board, WS-driven.
4. "Announce to Discord" per entry → #18 dispatcher (rate-limited).
5. (nice-to-have) suggested matches; "promote LFG → scheduled event" shortcut.

## Relevant code
- `server/app.py` — `hub`, `presence`/`online_count`/`broadcast_online`,
  `presence_broadcaster` (lifecycle pattern), WS handler, `PRESENCE_STALE_S`.
- `server/db.py` — `members` (names), `handles` (primary handle).
- `server/static/index.html` — WS module, TEAMMATES roster (render pattern to
  echo), launcher view, `#online` badge, view router.
- Ties into **#18 Discord notifications** (rally → channel ping).
