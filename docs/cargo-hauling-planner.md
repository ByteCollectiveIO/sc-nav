# Cargo-hauling route planner — design

**Status:** designed, not built (2026-06-20). This doc captures the decisions so
the feature can be picked up later without re-deriving them.

A planner for **cargo-hauling contracts**: take the pickups/dropoffs a player has
accepted, compute an efficient visiting order under cargo capacity, then guide
the player through the run and learn from completed runs.

**Out of scope:** commodities *trading* (buy-low/sell-high). UEX already serves
that; we deliberately don't duplicate it.

---

## Why this fits the existing system

Most of the hard parts already exist in `server/nav_core.py`:

- **Realistic travel-cost model** — `resource_hotspots` (~`nav_core.py:1294-1309`)
  already computes SC-accurate travel: jump to the nearest QT marker, plus the
  "you must QT to the parent planet first, then the moon" two-hop rule via
  `parent_planet` + `nearest_planet`. This is exactly the planner's cost
  function; it just needs generalizing from "player → hotspot" into a reusable
  `travel_cost(nav, from, to)` between any two stops.
- **Geo primitives** — `entity_global_m`, `dist3`, `great_circle`,
  `nearest_qt_marker`; every POI already carries `nearest_qt` /
  `nearest_qt_dist_m`.
- **Guidance loop** — `compute_state` already resolves a session `destination_id`
  into distance / surface-distance / bearing / ETA / nearest QT marker, streamed
  over `/ws` from the watcher's `/showlocation` → `/api/position` feed. The
  planner reuses this verbatim for turn-by-turn.
- **Conventions** — stateless compute endpoints (`/api/resource_hotspots`) and a
  hash-routed SPA (`#/stats`) to mirror.

---

## Three layers

The feature splits cleanly into **Plan** (stateless), **Execute** (stateful,
per-user), and **Learn** (persisted history).

```
Plan      compute optimal stop order            stateless POST /api/route/plan
Execute   walk the route, confirm cargo,        per-user run persisted in DB,
          live onboard-SCU                        driven by the position pipeline
Learn     completed-run history → quick-picks    per-user append-only log
```

---

## Data model

```
package  = { id, commodity, scu, from_id, to_id, state }
           state: pending → onboard → delivered
           from_id / to_id are POI ids; from→to encodes pickup-before-dropoff
           precedence for free.

contract = { id, reward?, packages[] }
           one accepted mission = N packages. reward is optional now but captured
           so the future "which contracts to accept" selector needs no rework.

ship     = { name, usable_scu }
           usable_scu is a single scalar capacity (see below).

run      = { id, owner_id, ship, contracts[], stops[], status, started_at }
           the active/executing route; persisted per-user.
```

### Why "package" (from→to) rather than separate pickup/dropoff lists

- The `from → to` link **encodes precedence for free** — you can't enter a pickup
  without its matching dropoff, which is exactly what the solver needs.
- It reads straight off the in-game mission screen (each cargo line has an origin
  and a destination), so there's no mental reshaping during entry.
- Multi-pickup / multi-dropoff is just several packages; the planner **merges
  visits** to any shared location into one stop.

---

## Ship capacity

- **Source:** the **uexcorp vehicles feed** (`https://api.uexcorp.uk/2.0/vehicles`,
  has `scu` per ship), fetched + cached as `ships.json` exactly like
  `commodities.json` today (`_fetch_json` → cache → load on startup → endpoint →
  re-fetch in `/api/refresh` + health counts; see
  `load_raw_commodity_names`/`COMMODITIES_URL` in `server/app.py` as the
  template). New `GET /api/ships`.
- **Stated vs. actual:** the stated cargo grid often differs from what a player
  can physically stuff (box tiling can leave you short; corridor-stuffing can
  exceed it). v1 models capacity as a **single "usable SCU" scalar**:
  - prefill the catalog's stated SCU,
  - let the user **override** it,
  - **remember it per ship** (per-user; see persistence).
- **Not in v1:** true 3D bin-packing of fixed-size cargo boxes (1/2/4/8/16/24/32
  SCU). The scalar `total SCU ≤ usable SCU` is the right abstraction; box-tiling
  awareness is a possible later refinement, not a v1 requirement.

### Commodity picker

Hauling commodities are **not** just raw ores, so the picker uses the **full**
`commodities.json` (all names), not the `is_raw==1` filter that the ore datalist
uses (`load_raw_commodity_names`).

---

## Plan layer — the solver

`POST /api/route/plan` → `{ packages[], usable_scu, start_id? }` →
ordered stops + per-leg detail.

1. **Build the stop set** — merge packages sharing a location into one stop; each
   stop carries its load list (pickups) and drop list (dropoffs).
2. **Cost matrix** — `travel_cost(nav, a, b)` for every stop pair, extracted from
   the `resource_hotspots` via-hop logic (nearest-QT-marker jump + planet→moon
   two-hop rule). `start_id` (current player position or first pickup) seeds it.
3. **Optimize** under two constraints:
   - **precedence** — a package's pickup stop precedes its dropoff stop;
   - **capacity** — onboard SCU never exceeds `usable_scu` at any point.
   - **≤ ~12 stops:** branch-and-bound with precedence + capacity pruning (Held-
     Karp DP is the alternative). Sub-millisecond at this scale.
   - **more:** nearest-neighbor seed + precedence-safe 2-opt / or-opt local
     search (reject any move that breaks precedence or capacity).
   - Pure stdlib — no solver dependency, matching the codebase ethos.
4. **Output per leg:** QT marker to jump to, distance, bearing, "via parent
   planet" flag, and **running onboard SCU**. Plus a feasibility summary:
   **peak load** and **minimum capacity required** — so an over-capacity bundle
   is reported ("these three contracts can't co-load on your Freelancer — drop
   one or take two trips") instead of silently producing a bad route.

---

## Execute layer — running the route

Reuses the existing destination/guidance loop; adds a checklist and auto-advance.

- **Start run:** the active stop's POI becomes the session `destination_id`, so
  existing nav (distance / bearing / ETA / QT marker via `compute_state`) guides
  the player there with zero new nav code.
- **Arrival detection:** active-stop distance drops below a threshold — reuse the
  container `detection_radius()` for stations, a small surface-distance for
  surface POIs. Broadcast an `arrived` flag over `/ws`.
- **Confirm on arrival (decided — do NOT auto-complete):** arriving ≠ cargo
  transferred (loading is a manual freight-elevator action). On arrival, surface
  the stop's **package checklist**; the player confirms what actually
  loaded/dropped. Checking a pickup flips `pending→onboard` and **adds** its SCU
  to live onboard load; checking a dropoff flips `onboard→delivered` and **frees**
  it. Completion is **per-package within a stop** (freight elevator is per-box;
  you may not load everything in one pass).
- **Advance:** when a stop's packages are resolved, advance `destination_id` to
  the next stop. Live readout: "cargo aboard: 412 / 696 SCU".
- **Durability (decided):** the active run **persists per-user in the DB**, so a
  mid-haul server restart / reconnect resumes where you left off (this revises the
  earlier "session-only like `destination_id`" lean).

---

## Learn layer — history & faster entry

- Append each completed run to a **per-user append-only log** (same spirit as
  `observations`): packages, reward, ship used, route taken, timestamps.
- **Primary payoff (data-entry accelerator):** frequency-ranked **quick-picks +
  typeahead priors** — the user's most-hauled `from→to` lanes, commodities, and
  SCU amounts float to the top of the pickers. Plus a "clone previous contract"
  shortcut for back-to-back similar lanes.
- **Analytics:** feeds the existing `#/stats` page — aUEC/hour, best lanes.

### Why manual entry, and the one automation path left

`Game.log` **does not contain contract details** (verified 2026-06-20 — only
contract-broker *connection* info to the SC backend; manifest / locations /
commodities never hit the log). So the watcher **cannot** auto-ingest contracts.

Therefore data entry is **manual** for v1, made painless by the history priors
above. The **only** remaining automation candidate is **OCR of the in-game
contract-manager screen** (screenshot → parse manifest) — brittle across UI
patches and a project in its own right, so it's a clearly-deferred future spike,
not v1.

---

## Persistence (DB)

Per-user data keys on the Discord member id (`user["id"]`, i.e. `discord_id` — the
identity used throughout `server/db.py`, e.g. the `handles.discord_id` and
`watcher_tokens.discord_id` columns). New tables (follow the existing
`CREATE TABLE IF NOT EXISTS` + `_ensure_column` migration pattern in
`server/db.py`):

- **`user_ships`** — `(discord_id, name, usable_scu, last_used)`; remembers each
  ship's learned usable SCU per user. (Alternatively fold a single "current ship"
  onto the user profile, but a small fleet table matches "remember per ship".)
- **`runs`** — `(id, discord_id, status, ship, started_at, completed_at, data)`
  where `data` is the JSON contracts/packages/stops/state blob (mirrors how
  `observations`/`custom_pois` store a JSON payload). Active run = the row with
  `status='active'`; completed runs become the history log.

`/api/me` currently returns session-derived fields only; extend it (or add a
sibling endpoint) to carry the user's saved ship + usable SCU.

---

## Endpoints (new)

- `GET  /api/ships` — uexcorp vehicles catalog (name + stated SCU).
- `POST /api/route/plan` — stateless optimize (above).
- `POST /api/route/run` — start/persist an active run (sets `destination_id`).
- `PATCH /api/route/run` — check off package(s) at a stop; advance.
- `DELETE /api/route/run` — abandon the active run.
- `GET  /api/route/history` — completed runs + derived quick-picks/priors.
- Ship prefs ride on `/api/me` (GET) + `/api/me`-style PUT.

---

## UI integration — the app shell

The planner is the **second app** in what has been a single-app SPA, so it forces
a question the navigator never had to answer: *what is "home"?* Today the SPA
lands authed users straight on the navigator — `main-view` is the implicit home
("everything that isn't a sub-view"), and `#/settings` / `#/setup` /
`#/leaderboard` / `#/stats` are sibling views toggled by `applyView()`
(`server/static/index.html` ~l.1951). Anonymous deep-links already bounce behind
the login gate.

To host more than one app, insert an **app launcher** between the Discord gate and
the apps:

```
Discord login-gate (anonymous overlay — unchanged)
        ↓  authed, empty hash
#/            → App Launcher  (new home; also the future-expansion landing page)
#/nav         → Resource Navigator  (was the implicit main-view)
#/route       → Cargo Planner
#/stats #/leaderboard #/setup #/settings → as today
```

This is a **re-parenting, not a router rewrite**: promote `main-view` to a named
`#/nav` view, make empty-hash render the launcher, and add the launcher + `#/route`
as two more branches in `applyView()` — same pattern as the existing sub-views.
The map being hidden-by-default is already safe (`drawMap` bails on a 0-dim canvas
and redraws on return), so demoting it from always-present costs nothing.

Note this launcher is a **post-auth** surface, distinct from the existing
**pre-auth** login splash (which shows the org logo beside the built-in one);
don't conflate them.

### Decisions baked in

- **Launcher is home, but skippable.** Empty hash renders the launcher; deep
  links stay live so a user can bookmark `#/nav` or `#/route` and never see it.
  **No** silent auto-redirect to a "last app" — that turns the launcher into a
  flicker and kills its discovery value. A "remember last app" jump can come
  later as an *explicit* affordance, not a redirect.
- **Stats/Leaderboard become app-scoped, not global chrome.** The planner has its
  own analytics (aUEC/hour, best lanes — see Learn layer), so `#/stats` forks per
  app. Settings/Setup stay global (account + org). Don't bake "Stats is global"
  any deeper; let each app own its stats content.
- **"Back" = home.** Today ⚙ toggles Navigation↔Settings and doubles as back. With
  a launcher, make the org-logo / title in the top-left always link to `#/` (home
  = launcher) and let ⚙ stay purely Settings. This is the one real chrome
  refactor the shell needs.

### Sequencing

Build the shell **before** the planner UI (it's step 0 below), so `#/route` drops
into a home that already exists rather than retrofitting a launcher around a
shipped planner and touching both at once.

---

## Client (`#/route` SPA view)

- **Ship picker** — typeahead from `/api/ships` → prefill stated SCU → override →
  remembered.
- **Contract entry** — package rows (commodity typeahead from full
  `commodities.json`, SCU, from/to via existing POI search biased to cargo-capable
  types: Distribution Center, Orbital Station, Landing Zone, RestStop, major
  Outposts — plus ~15 hauling-hub quick-picks and history-ranked recents).
  Optional reward field. "Clone previous contract" shortcut.
- **Plan output** — ordered stop list with per-leg nav detail + feasibility
  summary (peak load / min capacity / infeasible flag).
- **Run mode** — current/next stop, the per-package arrival checklist, live
  onboard-SCU readout. Reuses the existing destination/guidance UI.

Built CSS-only / hand-rolled to match the existing SPA (`server/static/index.html`).

---

## Build order (bottom-up)

0. **App shell** — app launcher at `#/`, navigator re-parented to `#/nav`, launcher
   + `#/route` branches in `applyView()`, top-left logo/title → home (see *UI
   integration* above). Land this first so the planner slots into a real shell.
1. Ship feed — `GET /api/ships` + `user_ships` persistence + `/api/me` carry.
2. `travel_cost` extraction from `resource_hotspots` + `POST /api/route/plan`
   (with `server/test_nav_core.py` coverage for precedence/capacity/merging).
3. Run persistence (`runs` table) + arrival detection + per-package checklist on
   the position pipeline.
4. `#/route` UI (entry → plan → run).
5. History log + frequency-ranked quick-picks/priors + `#/stats` hooks.

**Deferred:** contract *selection* ("which to accept" — reward already captured),
box-size/bin-packing, OCR contract ingestion, anything trading-related.
