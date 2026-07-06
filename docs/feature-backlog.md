# Feature backlog

The working list of what's **next, small, or parked** — not a history book.
Consolidated 2026-07-04 (as of **v0.36.0**): shipped features are one line in
the [Shipped log](#shipped-log) with a pointer to their spec doc; the full
historical design prose that used to live here is preserved verbatim in
[`archive/feature-backlog-full-2026-07-04.md`](archive/feature-backlog-full-2026-07-04.md).

**How this file works**
- Numbering continues from the historical backlog (#1–25); new items start at #26.
- An active entry captures *decisions*, so it can be picked up without re-deriving.
- When something ships it collapses to a Shipped-log row; its spec doc (if any)
  is the lasting reference. Doc statuses live in [`docs/README.md`](README.md).

---

## Now / next

### 26. SC Wiki API reference-data layer (foundation)

**Status:** vehicles/quantum slice **SHIPPED v0.37.0** — `tools/sync_quantum.py`
fetches `/api/vehicles` + `/api/vehicle-items?filter[type]=QuantumDrive`,
distills committed `poi/quantum_drives.json` + `poi/quantum_profiles.json`
(+ `quantum_match_report.txt` build artifact), version-stamped, no runtime
calls. 230 ship profiles / 57 drives / 0 identity mismatches / 81%
uexcorp-hauler coverage. Footer carries the CC BY-SA 4.0 attribution. Smoke test
in `test_nav_core.py`. **Blueprints slice SHIPPED v0.40.0**
(`tools/sync_blueprints.py` → committed `poi/blueprints.json`, 1,559 recipes —
see #25 in the Shipped log). **Remaining slice** (same fetch→distill
convention): `/api/locations/positions` (feeds #28).

`https://api.star-citizen.wiki` (OpenAPI at `/api/openapi`) is a public,
game-version-scoped JSON API — no auth for game data, pagination
`page[size]` ≤ 200, license **CC BY-SA 4.0 with attribution** (unlike erkul's
CC BY-NC-ND, which we rejected). Use **English fields only** (German strings are
BY-NC-SA). It resolves the project's two standing data blockers and opens three
enrichment paths (#27, #25, #28).

**Deliverable:** a sync/distill script (same convention as
[`quantum-data-pipeline.md`](quantum-data-pipeline.md): fetch → distill →
committed `poi/*.json`, **no live runtime calls**), each output stamped with the
game version from `GET /api/game-versions/default` (currently
`4.8.2-LIVE.12030094`). Add a one-line attribution ("Game data:
Star Citizen Wiki, CC BY-SA 4.0") to the site footer/about. Manual per-patch
re-run is the cadence; runtime auto-refresh is deliberately not v1.

Key endpoints (all probed): `/api/vehicles` (290 ships, incl. per-ship `quantum`
block + `fuel` + `cargo_grids` + `insurance` + `uex_prices`),
`/api/vehicle-items?filter[type]=QuantumDrive` (full drive stats),
`/api/blueprints` (1,559 recipes w/ ingredients, craft time, dismantle returns),
`/api/locations/positions?filter[system]=` (x/y/z + `qt_valid` + parent, 809
Stanton entities), `/api/locations/{id}` (per-POI `quantum_travel` radii +
`amenities`), `/api/commodities` (box sizes, mineable/harvestable/salvage flags).

### 27. Quantum fuel & max jump-range (cargo + trade planners)

**Status:** **SHIPPED v0.37.0.** Fuel burn + max-range are
live in both planners: nav_core annotation/summary/`in_range_only`, app.py
`/api/ships` `quantum` enrichment + `_resolve_drive` + solver wiring, and a SHIP-
panel drive picker + in-range checkbox + per-leg fuel + range callout (drive
remembered in localStorage, no DB migration). Unmatched ships degrade gracefully.
Spec + build notes: [`quantum-fuel-range.md`](quantum-fuel-range.md).

Decisions locked: default drive + override picker; max-range as **advisory
warning** with an opt-in "only in-range routes" hard constraint; unmatched ships
degrade gracefully (no fabricated numbers). The original blocker — an early
datamined pass covered only ~49% of hauler ships (that raw mine has since been
removed) — is solved: source the per-ship `quantum` block from `/api/vehicles`
(**95% coverage**, 230/242 spaceships; the 12 missing are drive-less snubs,
correctly absent) and the drive catalog from
`/api/vehicle-items?filter[type]=QuantumDrive`. The SCU/Gm fuel math and JSON
shapes from the design carry over unchanged; only the source is the wiki API.

### 25.1 Craft commissions v1.1 (follow-on to the shipped #25)

**Status: CLOSED — everything build-worthy shipped v0.40.0–v0.44.0** (see the
Shipped log). Spec: [`blueprint-craft-commissions.md`](blueprint-craft-commissions.md)
§10–§12.

- ~~**Member blueprint library** (§10)~~ — **SHIPPED v0.42.0**:
  `member_blueprints` table + "My Blueprints" settings picker; commission board
  shows "⚒ N can craft" + a "✨ Requests I can craft" filter (LFG match pattern).
- ~~**§11 sell-side ripples**~~ — **SHIPPED v0.43.0 + v0.44.0**: canonical
  stat-name autocomplete (§11.2, `/api/blueprints/stat-names` + datalist,
  v0.43.0); `blueprint:` identity for sale/auction listings (§11.3 — market
  picker offers ⚒ recipes, `blueprint_key` stamped on any mode, `kind=blueprint`
  filter finds crafted goods, v0.44.0); auto-estimated stat panel (§11.4 —
  per-slot asks for commissions, uniform-at-Qn for an advertised overall
  quality, assumption stated in-UI, v0.44.0). Still open from §11: plausibility
  nudges (§11.5), price↔quality intelligence (§11.6), numeric stat values
  (§11.7) — post-bedding-in ideas, grab opportunistically.
- ~~**Estimated material cost** (§12)~~ — **SHIPPED v0.43.0**:
  `nav_core.blueprint_material_cost` × market reference prices → "mats ≈" on
  both spec-builder forms, commission cards/detail, crafted-sale detail, and
  the craft-goal header; gem/item inputs degrade to a named *unpriced* list
  (still no per-gem price source). All 1,559 feed recipes price out.
- ~~**Choice-group picker**~~ — **DECIDED SKIP 2026-07-05** (per this item's
  own conditional): the feed's 9 `sel` aspects sit on exactly 3 fringe recipes
  (the Aztalan Legs armor variants, each "pick 2 of 3" over the same slots).
  The manifest lists all 3 options — a slight over-count on those 3 recipes
  only. Revisit if a game patch puts choice groups on recipes players care
  about.
- ~~**Announce name-check**~~ — **SHIPPED v0.43.0**: the WANTED announce
  @-mentions library-matched crafters (poster excluded, capped at 15).

### 28. Starmap & POI enrichment from the wiki API 🆕 (needs scoping)

**Status:** opportunity identified 2026-07-04; three independent slices, each
cheap once #26 exists. Scope before building.

- **a) POI validation/expansion** — `/api/locations/positions` gives real x/y/z
  + `qt_valid` + parent hierarchy per system (stanton/pyro/nyx). Cross-check our
  starmap.space-derived POI set; backfill missing QT destinations.
- **b) Arrival & detour radii** — per-POI `quantum_travel`
  (`arrival_radius`/`obstruction_radius`, e.g. Everus Harbor 24 km/8 km) can
  sharpen run-mode arrival detection and replace the flat org-wide
  `hazard_radius_km` detour margin with per-destination values.
- **c) Terminal amenities** — `amenities` (Commodity Trading via freight
  elevator vs. loading dock, hangar/pad sizes, clinics) → trade-planner stop
  annotations and pad-size-vs-ship warnings.

---

## Fast-follows by app

Small, unblocked items harvested (2026-07-04) from every spec doc's
Deferred/Open sections, so they stop hiding in eighteen files. Grab
opportunistically; none is urgent.

- **Trade planner (#21):** teammate-lane-awareness ("someone's already running
  this lane" — needs a presence-side design pass first) · exact B&B "thorough"
  solver option under a ≤4-stop cap.
- **Danger board / routing (#24):** two-waypoint detour fallback (v2.1 — a
  `# v2.1` marker sits at the spot in `nav_core`) · severity-scale + radius
  tuning once the board has real data (partly superseded by #28b).
- **Marketplace (#15):** inventory bridge (list from holdings; one-click list
  surplus from met goals) · price history → "fair price" hint from completed
  deals · WTB saved searches (largely realized by #25) · richer reputation
  (only if abuse appears).
- **Resource Manager (#14):** map→goal badging ("needed for N goals" in the
  finder) · contribution history/leaderboard · goal-met → marketplace bridge.
  (Recipe-BOM goal seeding + personal goals shipped v0.42.0, #14.2; ship-BOM
  templates still open.)
- **Events (#13/#20):** POI-linked event location (autocomplete exists; still
  stores freeform text) · recurring events via a "clone event" shortcut ·
  attendance / organizer leaderboard · per-user timezone setting ·
  edit/start-time-change notifications.
- **Cargo planner (#12):** start-from-chosen-POI (`start_id`) + free start ·
  contract-selection advisor (reward-per-hour is already captured) · per-leg
  drive-accurate ETA (lands with #27).
- **Identity / profiles (#17/#30):** member-facing directory surface (opt-out
  already honored; #30's playstyle tags now make it genuinely useful) ·
  directory avatars (hash captured; rendering is one CDN call) · LFG
  ✨-suggested-matches weighting persistent profile tags, so matching works even
  when a member hasn't set a transient activity · `PLAYSTYLE_TAGS` vocabulary
  governance (custom org tags as a setting) if orgs ask.
- **Notifications (#18):** auction "ending soon" ping (needs a scheduled loop) ·
  goal milestone pings at 50/75% (off by default).
- **Platform:** capture-side Discord-id attribution (`owner_id` still =
  `player_id` on capture; deletes are already discord-scoped — the last
  migration tail) · cosmetic handle editing via `PUT /api/me`.

---

## Parked (deliberate, with reasons)

- **#22 Refinery job tracker** — real SC pain point but per-player utility, not
  org-oriented.
- **#23 Recognition badges** — liked, but can get tacky fast; revisit with
  restraint (few, earned, tasteful).
- **OCR contract ingestion (#12)** — the only remaining cargo-entry automation;
  brittle across game UI patches, a project in its own right.
- **3D box bin-packing (#12/#21)** — scalar "usable SCU" is good enough.
- **Watcher packaging (.exe)** — stay a Python script until adoption feedback
  says otherwise; PyInstaller + code-signing (~$200/yr) is the plan if revisited.
- **Monetization / CIG permission inquiry** —
  [`monetization-and-deployment.md`](monetization-and-deployment.md); draft the
  CIG ask only if a paid hosting tier becomes real. Non-commercial rule stands.
- **Discord DMs / bot** — webhook-only stands; revisit only if members ask for
  private alerts.
- **Redis / multi-worker** — won't-do at org size; the in-process hub requires a
  single worker (documented loudly in the migration doc).

---

## Shipped log

Everything below is live (deploy = merge to `origin/main`; a git-based Portainer
stack auto-redeploys within ~5 min). Full design/build notes: the spec doc where
listed, else the [archived backlog](archive/feature-backlog-full-2026-07-04.md).

| # | Feature | Shipped | Reference |
|---|---------|---------|-----------|
| — | Multi-user / org migration (OAuth, SQLite, presence, admin) | 2026-06-18 | [multi-user-migration.md](multi-user-migration.md) |
| 1 | Fresh-only observation markers | 2026-06-19 | archive |
| 2 | Custom-POI notes + upstream comments | 2026-06-19 | archive |
| 3 | Dedicated settings page (first hash route) | 2026-06-19 | archive |
| 4 | Custom org logo | 2026-06-19 | archive |
| 5 | Drop ETA readout (keep calc) | 2026-06-19 | archive |
| 6 | Panel reorder (teammates above map) | 2026-06-19 | archive |
| 7 | Harvestables capture | 2026-06-19 | archive |
| 8 | Harvestables forecast/finder/heatmap | 2026-06-19 | archive |
| 9 | Nonce-based CSP (closed the security batch) | 2026-06-30 | archive |
| 10 | Per-shard nodes | 2026-06-20 | archive |
| 11 | Mobile-responsive CSS | 2026-06-20 | archive |
| 12 | Cargo-hauling planner v1 (+ multi-pickup, rewards, guild boards) | 2026-06-21 | [cargo-hauling-planner.md](cargo-hauling-planner.md) |
| 13 | Guild event planner v1 (+ 7-item UI pass) | 2026-06-23/24 · v0.2.x–0.3.0 | [event-planner.md](event-planner.md), [event-planner-todo.md](event-planner-todo.md) |
| 14 | Resource Manager (inventory + goals) | 2026-06-24 · v0.5.0 | [org-inventory-goals.md](org-inventory-goals.md) |
| 15 | Org marketplace (sale/auction/barter) + scale/search pass | 2026-06-25/26 · v0.6.0–v0.7.1 | [marketplace.md](marketplace.md) |
| 16 | Resource Manager v1.1 (units, POI locations, edit, allocations) | 2026-06-25 | [org-inventory-goals.md](org-inventory-goals.md) |
| 17 | Member identity, primary handle & directory | 2026-06-29 | [member-identity-and-directory.md](member-identity-and-directory.md) |
| 18 | Discord notifications (webhook, per-category) | v0.14.0–v0.17.0 | [discord-notifications.md](discord-notifications.md) |
| 19 | Who's online + Group Finder (LFG) | v0.18.0–v0.22.0 | [who-is-online-lfg.md](who-is-online-lfg.md) |
| 20 | Fleet roster / squad organizer (+ seat & group templates) | v0.23.0–v0.24.1 | [fleet-roster-squad-organizer.md](fleet-roster-squad-organizer.md) |
| — | Team-tracking multiplayer fixes · watcher heartbeat | v0.25.0 · v0.26.0 | memory/commits |
| — | Impeccable design sweeps (every surface >35/40) | v0.26.1 · v0.27.0 | `.impeccable/critique/` |
| 21 | Trade Route Planner (solver, run mode, history/stats, favorites, freshness UX) | v0.28.1–v0.33.0 | [trade-route-planner.md](trade-route-planner.md) |
| 24 | Pirate danger warnings v1 + v2 snare-detour routing | v0.34.0 · v0.35.0 | [pirate-warnings.md](pirate-warnings.md), [snare-detour-routing.md](snare-detour-routing.md) |
| — | Launcher reorganization (3 themed groups) | v0.36.0 | PR #13 |
| 26/27 | Quantum data slice (wiki API) + fuel/range in both planners | v0.37.0 | [quantum-fuel-range.md](quantum-fuel-range.md), [quantum-data-pipeline.md](quantum-data-pipeline.md) |
| 21 | Trade planner stock + demand-side reports (STOCK WATCH) | v0.38.0 · v0.39.0 | [trade-route-planner.md](trade-route-planner.md) |
| 25 | Blueprint craft commissions v1 (+ blueprint feed, spec builder, slider-driven quality minimums) | v0.40.0 · v0.41.0 | [blueprint-craft-commissions.md](blueprint-craft-commissions.md) |
| 14.2 / 25.1 | Personal + blueprint-seeded craft goals · member blueprint library · commission crafter-matching (§10) | v0.42.0 (⚒ glyph fix v0.42.1) | [blueprint-craft-commissions.md](blueprint-craft-commissions.md) §10 |
| 25.1 | Craft-goal spec builder (per-slot quality targets) · estimated materials cost (§12) · stat-name autocomplete (§11.2) · WANTED announce pings capable crafters | v0.43.0 | [blueprint-craft-commissions.md](blueprint-craft-commissions.md) §11–§12 |
| 25.1 | `blueprint:` identity for sale/auction listings (§11.3) · expected-stats panel on blueprint-linked listings (§11.4) — closes #25.1 | v0.44.0 | [blueprint-craft-commissions.md](blueprint-craft-commissions.md) §11 |
| 29/30 | Resource Manager restructure (Goals · Inventory · Blueprints peer tabs, library out of Settings, My-holdings default) · member playstyle profile (Settings PROFILE chips → Who's Online + directory) | v0.45.0 | [rm-restructure-and-profile.md](rm-restructure-and-profile.md) |
