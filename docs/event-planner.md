# Guild event planner — design

> **Amendments since ship:** `type` became a multi-select `types` list and the
> taxonomy gained "Event"/"Race" categories
> ([event-planner-todo.md](event-planner-todo.md), v0.3.0). The deferred
> "Discord announcements" item shipped via #18
> ([discord-notifications.md](discord-notifications.md)). Fleet rosters layered
> on top as #20 ([fleet-roster-squad-organizer.md](fleet-roster-squad-organizer.md)).

**Status:** designed 2026-06-23; **v1 BUILT 2026-06-23** (all four build-order
steps live, uncommitted pending the user's commit/deploy). This doc captures the
decisions agreed before any code, matching how the cargo planner was specced
first ([`docs/cargo-hauling-planner.md`](cargo-hauling-planner.md)). What shipped
matches the design below; the only deliberate UI deviation is that the detail
roster is grouped **by target role** (each role's fill bar + the members filling
it) rather than a flat per-member list — it satisfies the "grouped by role" intent
and reads cleaner with multi-role signups. Deferred items (recurring events,
Discord announce, attendance leaderboard, POI-linked location) remain deferred.

An app for organizing **in-game events** — raids, mining/salvage ops, meetups,
and especially **survey & exploration expeditions**. An organizer creates an
event (type, category, time, start location, roster targets); members sign up for
the role(s) they'll fill; each event tracks its fill against the targets
(`3/5 players`, `Medical 2/2`, `Surveyor 1/3`).

This is the **third app** in the SPA (after the Resource Navigator and Cargo
Planner). The app shell — launcher at `#/`, hash-routed peer views — already
exists, so this adds one launcher card and one view family; no shell work.

**Why this app is org-specific (not generic):** the Survey & Exploration event
types feed *back into* the rest of the webapp. A survey op is an organized push to
grow the navigator's own dataset — resource cells, ores, hotspots, fauna, biomes,
custom POIs. No third-party tool plans an event whose deliverable is *your* map.

**Out of scope (v1):** recurring events, Discord announcements/RSVP, per-user
timezone settings, and post-event attendance credit. Each is noted under
*Deferred* with the cheap path to add it later.

---

## Why this fits the existing system

Same shape as the cargo planner — it reuses, not invents:

- **Identity & org gate** — every endpoint keys on `user["id"]` (the Discord
  member id used throughout `server/db.py`), and `auth_gate` middleware already
  restricts the whole app to org members. So "the guild" *is* the attendee pool
  for free; no separate invite/membership concept is needed.
- **Persistence conventions** — new tables follow the `CREATE TABLE IF NOT EXISTS`
  + `_ensure_column` migration pattern, storing structured fields as JSON blobs in
  a column (mirrors `runs.data`, `observations`, `custom_pois`).
- **Reference data served from code** — the taxonomy (types/categories/roles) is a
  curated constants module served via one endpoint, exactly like `/api/ships` and
  `/api/commodities`. Edited in a commit, not a CRUD UI.
- **Pure, tested core** — the only real logic (fill computation) lives in
  `server/nav_core.py` as a pure function with unit tests, like
  `derive_run_stats` / `derive_guild_leaderboard`.
- **SPA conventions** — a hash-routed view (`#/events`) added as a peer branch in
  `applyView()`, hand-rolled CSS to match (`server/static/index.html`).

---

## Two layers

The feature splits into **Author/Browse** (the event board + create form) and
**Signup/Track** (joining and the live fill computation).

```
Author/Browse   create events, list + calendar     CRUD on /api/events
Signup/Track    join with role(s), compute fill     /api/events/{id}/signup
                                                     + derive_event_fill (pure)
```

---

## Data model

```
event   = { id, organizer_id, title, description,
            type, category(JSON list), start_at(UTC), duration_min?,
            location(rally point), event_location?, min_players, max_players?,
            roles: [{role, needed}],   status, created_at, updated_at }

signup  = { id, event_id, discord_id, roles: [role, ...],
            status: going | maybe | withdrawn, note?, created_at }
```

- The **contract atom is the signup**, and a signup carries a *list* of roles — a
  medic who'll also escort claims both. This is the one fact the fill math hinges
  on (see *Fill computation*).
- `roles` on the event is the **target roster**: `[{role, needed}]`. `needed` is a
  soft target, not a cap — surplus signups are allowed and shown as a surplus, not
  rejected.
- `max_players` nullable = unlimited; `min_players` drives the "minimum met?"
  gate. `start_at` is **UTC** (store UTC, render each viewer's local time).
- `category` is a **list** — an event can carry several flavors at once (a PvP +
  PvE op). Legacy single-string rows parse back as a one-element list.
- `location` is the **rally point** (where the org forms up); `event_location` is
  the optional spot where the activity actually happens. Both are freeform text
  with POI-search autocomplete in the form.
- `status`: `scheduled → cancelled | completed`. Cancelling keeps the row (and its
  roster) rather than deleting, so a mistaken cancel is recoverable and history is
  intact.

---

## Taxonomy (curated, in code)

Served from `GET /api/events/taxonomy`. Three independent axes so combinations
stay open (a *PvE Survey Op* and a *PvP Raid* both express cleanly):

**Type** — the activity / game loop:
```
Raid · Mining Op · Salvage Op · Cargo Haul · Bounty Hunt ·
Survey Op · Exploration · Racing · Combat Patrol · Medical Op ·
Industrial · Meetup / Social · Training
```

**Category** — the flavor(s), for filtering. An event may carry **several** (a
raid that's both PvP and PvE):
```
PvP · PvE · Social · Logistics · Mixed
```
(Survey/Exploration are *Types*, not a category — they're just PvE. Deliberately
not duplicated onto the category axis.)

**Roles** — what a signup fills, grouped for the UI (the stored value is the flat
role name):
```
Combat & Security   Combat (Ship) · Combat (FPS) · Escort · Medical
Industrial          Mining · Salvage · Cargo / Hauling
Survey & Explore    Surveyor · Naturalist · Cartographer · Pathfinder / Scout
Support             Support / Logistics · Command
```

### Survey & Exploration — the org-specific cluster

These two types exist to grow the navigator's dataset, and the four survey roles
map **1:1 onto the app's four capture domains**, so a signup's role tells you
which dataset they'll grow:

| Role | In-event job | App capture domain it feeds |
|---|---|---|
| **Surveyor** | Run resource scans, log deposits | resource cells · ores · hotspots |
| **Naturalist** | Catalog wildlife, flora, terrain | fauna · harvestables · biomes |
| **Cartographer** | Log coordinates, drop & annotate POIs | custom POIs · position/watcher data |
| **Pathfinder / Scout** | Find new sites, scan ahead, recon | (exploration lead) |

`Pathfinder / Scout` absorbs the generic recon role — one role, no overlap.
Prospecting is folded into `Surveyor` + the `Mining` role rather than a separate
role that fills the same scan bars. **Survey = catalog the known; Exploration =
find the unknown** — both end in data entering the POI/observation system.

---

## Fill computation — the testable core

A pure `derive_event_fill(event, signups)` in `server/nav_core.py`, unit-tested
like the rest of the suite. Given the event's `min/max_players` + target `roles`
and the going signups (each with a role list):

```
total_going  = count of distinct signups with status="going"
spots_left   = max_players - total_going          (None ⇒ unlimited)
min_met      = total_going >= min_players
roster       = [{role, needed, filled, short}]
               filled = # going signups listing that role
               short  = max(0, needed - filled)
```

**The double-count rule (the decision the tests pin down):** a signup counts
toward **every role it lists** for the per-role bars, but the headline total
counts **distinct players**. So a 5-person op where two people each cover Medical
*and* Escort shows `5 players` up top and full Medical *and* Escort bars below —
the per-role view reflects coverage, the headline reflects headcount, and neither
lies.

---

## Permissions

- **Create:** any signed-in org member (already gated by `auth_gate`). The board
  is meant to be social; no role gate in v1.
- **Edit / cancel:** the organizer **or** an app admin (`require_session` then
  check `event.organizer_id == user["id"] or user["is_admin"]`).
- **Sign up / withdraw:** any member, for any scheduled event.

---

## Persistence (DB)

New tables in `server/db.py`, keyed on the Discord member id, following the
existing `CREATE TABLE IF NOT EXISTS` + `_ensure_column` pattern:

- **`events`** — `(id, organizer_id, title, description, type, category,
  start_at, duration_min, location, event_location, min_players, max_players,
  roles, status, created_at, updated_at)`; `roles` and `category` are JSON blobs
  (`category` is the list of flavors). `event_location` is added via `_ensure_column`
  for DBs predating multi-location.
- **`event_signups`** — `(id, event_id, discord_id, roles, status, note,
  created_at)` with `UNIQUE(event_id, discord_id)` (one signup per member per
  event — joining again upserts the role list) and an index on `event_id`.

No change to `/api/me`. Organizer/attendee display names resolve through the
existing handle/session machinery.

---

## Endpoints (new)

```
GET    /api/events/taxonomy            types, categories, grouped roles (form data)
GET    /api/events?range=upcoming|past list for cards + calendar
POST   /api/events                     create (any member)
GET    /api/events/{id}                detail + roster + derived fill
PATCH  /api/events/{id}                edit (organizer or admin)
DELETE /api/events/{id}                cancel (organizer or admin; soft)
POST   /api/events/{id}/signup         join / update my role list (upsert)
DELETE /api/events/{id}/signup         withdraw
```

List responses carry the derived fill summary so cards render without N+1 detail
fetches.

---

## Client (`#/events` SPA view)

Launcher gets an **Events** card → `#/events`. Hand-rolled CSS to match the
existing SPA; the calendar is a CSS month grid (same spirit as the hand-rolled
charts on `#/stats`).

- **Board (`#/events`)** — an intro blurb, then a month **calendar** with
  pronounced dots on event days, above a scrollable list of upcoming **event
  cards**. Card = gold title, type + category chips (one per category),
  *local* start time (UTC stored, rendered local with a UTC tooltip), location,
  and the fill bars: headline `3/5 players` + per-role mini-bars
  `Medical 2/2 ✓ · Surveyor 1/3`. Filter by category/type.
- **Create** (`#/events/new`) — title, a multi-select **category** chip row, type
  picker (from taxonomy), **separate Date + Time** inputs (entered local with a
  timezone hint, stored UTC), **rally point** + optional **event location** (both
  freeform with POI-search autocomplete), min/max players, and a repeatable
  `role + needed` target-roster builder. Native pickers render dark
  (`color-scheme`) to match the site.
- **Detail (`#/event/{id}`)** — full description, roster grouped by role, and a
  **role multi-select** to join (the grouped taxonomy). Organizer/admin sees edit
  + cancel.

---

## Build order (bottom-up)

Mirrors the cargo planner's sequencing — backend + tests first, view last.

1. **Schema + taxonomy + fill logic** — `events` / `event_signups` tables, the
   taxonomy constants, and `derive_event_fill` in `nav_core.py` with unit tests
   (the double-count rule, min-met gate, unlimited-max, surplus). No UI; fully
   testable. *Only part with real logic.*
2. **Event CRUD** — `/api/events` create/list/detail/edit/cancel with
   organizer-or-admin guards on mutation; list carries derived fill.
3. **Signup** — `/api/events/{id}/signup` upsert/withdraw against
   `UNIQUE(event_id, discord_id)`.
4. **`#/events` view** — launcher card, calendar + cards, create form, detail +
   role multi-select signup. CSS-only, matches the SPA.

Suite grows ~79 → ~90 with the `derive_event_fill` tests.

---

## Deferred (with the cheap path to add later)

- **Recurring events** — one-off rows only in v1. A "clone event" shortcut (like
  the cargo planner's clone-haul quick-pick) covers most of the need cheaply.
- **Discord announcements** — web-only first. An admin-configured channel
  **webhook URL** (org setting) posting an embed on create/update is the
  lightweight path — no bot token. Full bot RSVP/reminders is a much larger build.
- **Location → POI link** — rally point + event location now have POI-search
  autocomplete (against the starmap POIs the navigator serves) but still store
  freeform text; linking the event to a stable POI id is the remaining step.
- **Attendance credit / organizer leaderboard** — completed events with their
  rosters could feed a "most active organizer / most attended" board like the
  guild hauling leaderboard. Not v1.
- **Per-user timezone** — v1 renders the browser's local time; no stored setting.

---

## Relevant code

- `server/nav_core.py` — add `derive_event_fill`; pattern off `derive_run_stats` /
  `derive_guild_leaderboard` (pure, unit-tested).
- `server/db.py` — `CREATE TABLE IF NOT EXISTS` + `_ensure_column` pattern; new
  `events` + `event_signups` tables keyed on `discord_id`.
- `server/app.py` — `require_session` / `require_admin` deps and organizer-guard
  pattern; taxonomy served like `/api/ships` (`GET /api/commodities`); JSON-blob
  column handling as in the `/api/route/*` handlers.
- `server/static/index.html` — launcher card + new `#/events` view added as
  branches in `applyView()`; calendar/cards CSS in the spirit of `#/stats`.
- `server/test_nav_core.py` — `derive_event_fill` tests alongside the existing
  suite.
