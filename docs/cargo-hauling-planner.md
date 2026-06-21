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
   - **Cross-system (v1, decided 2026-06-20):** the dataset already spans five
     systems (Stanton, Pyro, Nyx, Ellis, Sol) — `nav.systems`, and every entity
     carries `system`. But the current cost primitives are **intra-system only**:
     `nearest_qt_marker` is scoped to the target's system (returns nothing if no
     QT marker there). Two pieces are therefore required for v1:
     - **Jump-gate POIs** — add the inter-system gates (Stanton↔Pyro, etc.) as
       POIs flagged like QT markers, one endpoint per system.
     - **Inter-system routing in `travel_cost`** — when `a.system != b.system`,
       cost = `a → gate(A)` + gate traversal + `gate(B) → b`, recursing as a small
       graph over gates if the lane chains systems. Same via-hop primitives,
       just one level up: gates are the inter-system QT markers.
3. **Optimize** under two constraints:
   - **precedence** — a package's pickup stop precedes its dropoff stop;
   - **capacity** — onboard SCU never exceeds `usable_scu` at any point.
   - **≤ ~12 stops:** branch-and-bound with precedence + capacity pruning (Held-
     Karp DP is the alternative). Sub-millisecond at this scale.
   - **more:** nearest-neighbor seed + precedence-safe 2-opt / or-opt local
     search (reject any move that breaks precedence or capacity).
   - Pure stdlib — no solver dependency, matching the codebase ethos.
4. **Output per leg:** QT marker / gate to jump to, distance, bearing, "via parent
   planet" and "via jump gate" flags, leg ETA, and **running onboard SCU**. Plus a
   run-level summary:
   - **Feasibility** — **peak load** and **minimum capacity required**, so an
     over-capacity bundle is reported ("these three contracts can't co-load on
     your Freelancer — drop one or take two trips") instead of silently producing
     a bad route.
   - **Total run time** — Σ leg ETAs + a per-stop loading-dwell constant. This is
     the answer to the player's *"how much time do I have to play"* decision in the
     accept step, and the denominator for the deferred reward-per-hour selector
     (reward is already captured per contract). Cheap to surface now.
   - **Fuel/refuel advisory** — see *Quantum fuel & refueling* below.

---

## Quantum fuel & refueling — advisory, not a constraint

**Decided 2026-06-20: v1 treats fuel as an advisory overlay, not a solver
constraint.** The route is still optimized purely on travel cost; fuel is layered
on top so the player decides, matching how SC hauling actually works.

**Hard data reality (verified against the uexcorp feed 2026-06-20):** the uexcorp
catalog gives us **no usable range data**. `fuel_quantum` is `0` for every vehicle
(field exists but unpopulated), and the only real signal, `is_quantum_capable`
(0/1), is true for every haul ship — so it's near-useless. Effective QT range is
**loadout-dependent** (the equipped *quantum drive* + the hull's quantum-fuel
tank), which the uexcorp ship catalog can't know.

**But range is computable from CIG game facts (decided 2026-06-21).** It decomposes
into two facts plus one player choice:

```
max single-jump range ≈ hull_quantum_fuel_capacity / drive_fuel_per_distance
per-leg QT time        ≈ overhead + leg_distance / drive_driveSpeed
```

- `drive_driveSpeed`, `drive_fuel_per_distance` (`quantumFuelRequirement`), and the
  two-stage accel rates are **per quantum-drive** game facts.
- `hull_quantum_fuel_capacity` is a **per-hull** game fact.
- The only thing the catalog can't know — *which drive is equipped* — is a small
  **player choice** (a drive dropdown), not an unknowable.

Both fact tables come from **CIG game data** via an openly-licensed extract (e.g.
scunpacked-style game-file dumps) or whatever UEX exposes — the same kind of
offline-cached feed as `commodities.json`. We compute `maxDistance` and per-leg
time ourselves; no third-party calculator is shipped or called at runtime. The
overhead constant (spool-up + accel/decel + cooldown, ~28 s for the sampled drive)
and the cruise term were validated against a known calculator's output and confirm
the linear-in-distance model holds at Gm scale.

> **Licensing note (2026-06-21) — why we compute this ourselves.**
> [erkul.games](https://erkul.games) exposes a private API whose loadout blob
> already contains a computed `results.travelTime.maxDistance`, which would have
> auto-filled the range field from a pasted share link. **We deliberately do not
> use it.** erkul content is licensed **CC BY-NC-ND 4.0**
> (Attribution-NonCommercial-NoDerivatives): bundling/redistributing their compiled
> `qdrives` feed or building on their calculator *output* implicates **NoDerivatives**,
> and the org/multi-user direction makes the **NonCommercial** clause a future
> liability; their `server.erkul.games/*` endpoints are also an undocumented private
> API (ToS/load risk). The *underlying numbers* (drive speed, fuel-per-distance, hull
> fuel tank) are **CIG game facts, not erkul's to license**, so we source those facts
> from a reuse-permitted dataset and do the math in-house. Local reference copies of
> the erkul responses are git-ignored (`docs/qdrives.json`, `docs/loadout.json`) —
> kept for development reference, never committed or served. This supersedes any
> earlier "paste your erkul link" idea.

- **Cumulative QT distance — always shown (free).** The cost matrix already
  computes per-leg QT distance, so the plan always displays per-leg and total QT
  distance. This is truthful with zero new data and is useful on its own.
- **Drive picker → computed per-ship range (decided 2026-06-21).** The player picks
  their ship (→ hull fuel tank) and their equipped quantum drive from a dropdown
  (→ drive speed + fuel-per-distance); the planner **computes** effective range and
  accurate per-leg time. This replaces the earlier "manual range number, no prefill"
  plan — it's still player-supplied (the drive choice) but now yields a real
  computed range and a real ETA instead of a hand-typed guess. The chosen drive is
  remembered per ship (per-user) alongside usable SCU; a manual range override stays
  available as a fallback. While no drive is chosen and no override is set, the
  planner shows distances but raises **no** warnings.
- **Flagging — only once a range is set.** With a range entered, flag any leg (or
  the run as a whole) whose cumulative QT distance exceeds it. No silent rerouting.
- **Opt-in refuel stops.** A *"consider refuel stops"* toggle (only meaningful once
  a range exists). When on, the planner inserts the nearest refuel-capable POI
  (stations / rest stops) as an advisory waypoint before a flagged leg; when off,
  it only warns. Refuel POIs are surfaced, never forced.
- **Interaction with jump gates** — cross-system lanes are the most likely to trip
  the range warning, so the fuel overlay and the gate routing are designed
  together: a gate leg reports its own QT distance into the same cumulative budget.

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
   Add a **quantum-drive catalog** (drive speed + fuel-per-distance, from CIG game
   data, offline-cached like `commodities.json`) and a hull→quantum-fuel-capacity
   fact, so the planner **computes** effective range from a per-ship drive choice
   (remembered per user). Manual range override kept as a fallback. Do **not** use
   erkul (CC BY-NC-ND / private API — see *Quantum fuel & refueling*).
   - ✅ **Ships feed SHIPPED 2026-06-21.** `load_ships()` + `GET /api/ships`
     (cargo-capable spaceships, name + stated SCU, from the uexcorp vehicles feed,
     cached `poi/ships.json` — full rows kept for the deferred drive work), wired
     into `/api/refresh` + `/api/health`.
   - ✅ **Ship-pref persistence SHIPPED 2026-06-21.** `user_ships` table
     `(discord_id, name, usable_scu, last_used)` in `server/db.py`
     (`list/upsert/delete_user_ship`); `GET /api/me` carries the saved fleet;
     `PUT /api/me/ship` (upsert + stamp last_used) and `DELETE /api/me/ship`.
   - ⛔ **Quantum-drive catalog + computed range: NOT built** — blocked on the
     CIG drive-data sourcing decision (erkul rejected; need a reuse-permitted
     extract). The fuel/refuel advisory waits on this.
2. ✅ **SHIPPED 2026-06-21.** `travel_cost(nav, src, dst)` extracted into
   `server/nav_core.py` + cross-system jump-gate routing + `plan_route` solver +
   `POST /api/route/plan` (`server/app.py`). 12 tests in `test_nav_core.py`
   (precedence / capacity / merging / via-hop / cross-system lane). Notes:
   - **Gate model:** functioning network is the **Stanton — Pyro — Nyx chain**
     (`GATE_LINKS`; Stanton↔Nyx routes via Pyro). The Terra/Magnus jump points in
     the dataset are **not** functioning gates and are excluded. `GATE_ENDPOINTS`
     only has clean Stanton-side (id 480) + Nyx-side (id 642) POIs; the **Pyro
     side has no gate POI in the data**, so Pyro-side legs degrade to an
     approach-only cost and the leg is flagged `partial: True`. `GATE_TRAVERSAL_M`
     is a tunable per-tunnel constant.
   - **`_intra_leg` refinement:** a directly-QT-able destination (a flagged QT
     marker, OR any space POI such as a station / jump point) aims at its own
     position; only a *non-marker surface POI* gets the nearest-marker
     substitution. This fixed the gate (a space POI) collapsing its source-side
     hop onto an unrelated nearby planet marker.
   - **Per-leg ETA + total run time** use **nominal** constants
     (`QT_CRUISE_SPEED_MS`, `QT_LEG_OVERHEAD_S`, `STOP_DWELL_S`) — distance is
     exact; time becomes drive-accurate once the step-1 drive catalog lands.
   - **Fuel/refuel advisory: NOT yet built** (needs the drive catalog). Plan
     output already carries per-leg + cumulative QT distance, so the overlay is a
     later add.
   - **Not yet built:** the live-position → synthetic start seed (start_id is an
     optional POI; absent it, the run begins free at the first stop).
3. ✅ **Run persistence + arrival detection SHIPPED 2026-06-21.** `runs` table
   `(id, discord_id, status, ship, started_at, completed_at, data)` in `db.py`
   (`get_active_run` / `start_run` / `update_run` / `complete_run` / `abandon_run`
   / `list_run_history`); the JSON `data` blob holds ordered stops + per-package
   state (`pending → onboard → delivered`) + the active-stop cursor. The active
   run lives on `Session.run` and is **reloaded in `hub.get` on session create**,
   so it resumes across restart / reconnect. Arrival = the live guidance distance
   to the active stop under `ARRIVAL_SURFACE_M` (5 km) / `ARRIVAL_SPACE_M` (50 km);
   `Session.run_view()` rides on `nav_state["run"]` (broadcast over `/ws`) and
   carries the `arrived` flag + live onboard SCU. Generous thresholds are safe —
   arrival only surfaces the checklist, never auto-completes.
4. ✅ **`#/route` entry → plan UI SHIPPED 2026-06-21** (`server/static/index.html`).
   Launcher card enabled (`#/route`), view wired into `applyView()`. Ship picker
   (datalist from `/api/ships`, prefills stated SCU or the member's saved
   override, "remember" → `PUT /api/me/ship`); package rows (free-text commodity
   + SCU + two type-to-search POI pickers resolving to ids via `/api/pois`);
   `POST /api/route/plan` → rendered summary (stops / packages / peak vs usable
   SCU / QT distance / est. time, infeasible → min-capacity message) + ordered
   stop list (per-leg QT marker / distance / ETA / via-planet / cross-system gate
   badge, running onboard SCU). CSS-only, matches the SPA.
   - ✅ **Run mode (execute) SHIPPED 2026-06-21.** "Start this run" on the plan →
     `POST /api/route/run` (re-solves server-side, 409 if infeasible). Run panel
     (`#route-run`): ship + onboard/usable SCU bar, ordered stops with the active
     one highlighted, the active stop's per-package **checklist** (load toggles
     pending↔onboard, drop toggles onboard↔delivered → `PATCH /api/route/run`),
     "skip to next stop" (force-advance), "abandon" (`DELETE`). Auto-advances past
     fully-resolved stops; finishing the last stop completes the run. Driven by
     both the HTTP responses *and* live `/ws` state (a `runSig` guard skips no-op
     redraws so the checklist doesn't thrash). Resumes via `GET /api/route/run` on
     entering `#/route`. Endpoints + lifecycle + arrival + resume verified
     end-to-end over HTTP (forged session + simulated positions).
   - **Dependency:** the from/to pickers search `/api/pois`, which is empty until
     the org enables the starmap catalog (default OFF) or adds custom POIs — same
     as the navigator's search.
5. History log + frequency-ranked quick-picks/priors + `#/stats` hooks.

**In v1 (decided 2026-06-20):** cross-system jump-gate routing; total-run-time
estimate; quantum-fuel range + opt-in refuel advisory (range **computed** from a
CIG-sourced drive catalog + per-ship drive choice, decided 2026-06-21 — not erkul).

**Deferred:** contract *selection* ("which to accept" — reward + run-time now
captured, so reward-per-hour is a thin later add), box-size/bin-packing, OCR
contract ingestion, anything trading-related.
