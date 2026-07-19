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

### 38. Survey app restructure — Halo Finder → Prospector ✅ BUILT

**Status: BUILT 2026-07-19 (designed + built same day; all three slices
R1–R3 in one pass), suites 736 green, browser-verified via the preview
harness; PENDING RELEASE.** Build deviations (no last-tab memory; FIELD pin
reuses the full drop block; `.halo-tab-dot[hidden]` CSS gotcha) recorded in
the doc's status header. Full plan:
[`survey-app-restructure.md`](survey-app-restructure.md). The `#/halo` app
accreted #31→#35→#36→#37 into one ~230-line scroll that interleaves three
jobs; the ⛏ survey block — now the app's most differentiated capability —
reads as an addon buried under AFTER THE DROP. Decision: **separate the
surfaces, not the app** (the field loop couples planning and surveying at the
same live moment; survey outputs feed the planner). One app, RM-masthead
tabs (#29 precedent): **DROP** (plan, `#/halo` default) · **FIELD** (armed
plan + verdict + radar + one-tap ⛏ mark, `#/halo/field`) · **ATLAS** (zones,
coverage/NEXT GAP, export — and the future #37 import home,
`#/halo/atlas`); system seg promoted to the masthead; app renamed
**Prospector** (DECIDED — user 2026-07-19: ~90% of what's mapped is ore
nodes). Frontend-only, zero API changes; 3 slices R1–R3, each ships alone.
All §9 pre-build questions DECIDED 2026-07-19 (ATLAS system-scoped; explicit
→ FLY IT, no auto-switch) — **ready to build**; only §9.4 (DROP result-card
trim) rides along in R1 review.

### 37. Survey platform — value, direction, lifecycle, scope 🔨 SLICES 0–5 SHIPPED

**Status: slice 0 (radar reference layers §5.4–5.5, v0.64.0 — Pocket Radar
POI overlay + in-pocket survey heatmap w/ ALL/7D/24H age window), slice 1
(value layer §3.2–3.3, v0.65.0 — $$$ tiers on every survey surface,
per-system tercile pool, salvage ⚙ lane, price-refresh re-tiering), slice 2
(ore-first routing §4, v0.66.0 — `/api/survey/find` + the element finder's
IN THE BELTS section + the halo `⛏ Ore` goal + ⛏ mined-out reports w/ 4 h
age-off) slice 3 (scan detail + zone detail view §3.1/§3.4, v0.67.0 —
after-the-drop scanner transcription via `PATCH .../survey`, the "scanned"
value basis, pool-relative routing comp% term, zone ▸ details timeline),
the v0.68.0 routing fix (arrival plans + staging cost sanity from a live
in-game report, pocket picker parity, radar ☀ sun compass), slice 4
(coverage gaps §5.1 + the always-on 3-system overview map w/ click-to-pin
+ NEXT GAP, v0.69.0) and slice 5 (survey stats + Org Intel Surveying
section §5.2 + Discord `survey` milestones + radar drift nudge §5.3,
v0.72.0) are SHIPPED. Remaining slices (staleness, import, kinds) stay
design.**
Full plan:
[`survey-platform.md`](survey-platform.md). Evolves the shipped #36/#36.1
survey stack from a mapping tool into an org **prospecting** suite, in
independently shippable slices: **(1) value layer** — per-zone `$$$` tiers
from the existing #32 price machinery (ores/density bases work on today's
marks; optional after-the-fact scan detail adds a "scanned" basis); **(2)
ore-first routing — the payoff loop (user's framing: mirror the planetary
element finder)** — pick an ore → ranked high-probability survey clusters
(likelihood shrinkage × travel cost, plannability-gated per the miss-ceiling
lesson) → one tap to a drop plan; element finder grows a DEEP SPACE section
+ halo `⛏ Ore` goal; works on today's marks; **(3) direction** — honest
coverage-gap targeting (plannable vs expedition gaps; a routing miss links
to NEXT GAP), derived survey stats + Org Intel section + Discord `survey`
milestones, radar drift nudge; **(4) lifecycle** — watcher game-build
stamping → automatic staleness badges, cross-org import with a
pending/review queue + dedupe, maintainer promotion tooling; **(5) scope** —
mark kinds (salvage/ice/gas/derelict/hazard w/ Danger Board cross-file),
surface zones (own doc #37.1 before build), value-aware mining circuit.
Invariants: derived-never-stored, one-tap stays one-tap, tiers-with-basis
honesty, no gamification. Build order in doc §9; slices 1+2 need zero new
inputs and complete the survey→mine loop on existing data.

### 36. Belt survey — crowd-sourced field mapping (Keeger first) ✅ BUILT

**Status: built 2026-07-16 (same day as the design), browser-verified
end-to-end (mark → live pocket → plan at 635 m miss → in-pocket verdict);
NOT in-game verified. Suites 376/252 green.** Two build discoveries worth
knowing: Keeger had to join the guarded system-disambiguation ladder (a
hint-less watcher fix at 48 Gm was getting stamped "Stanton" and losing the
mark), and a pocket-miss ceiling (100,000 km) now rejects un-plannable
deep-belt marks with a contract-marker explanation instead of emitting
multi-Gm "drop" cards — with sparse Nyx markers, the drop-plannable sweet
spot is rocks on station approach chords. Full design + build notes:
[`belt-survey.md`](belt-survey.md). The user's idea: players drop one-tap
**survey marks** (custom POIs, `type="survey"` + one JSON payload column —
density incl. first-class "nothing here" negatives, ores, salvage) while
flying unmapped belts; **surveyed pockets go live org-wide from the FIRST
rock mark** (a mark is ground truth — centroid target w/ mark-count
confidence badge; nearby marks merge and refine), feeding the #35
pocket-mode planner as `surveyed` pockets; the statistical field model
(ring width/height/coverage) gates at ~25 marks and is the exportable
artifact — export → review → committed constants for every deployment
(Cornerstone precedent, industrialized).
Bootstrap: Keeger contracts spawn QT markers deep in-belt — any fix taken
there is a measurement. **Prerequisite slice ships alone: Keeger becomes a
named region** (stations ring the belt at exactly 48.000 Gm; wiki live data
confirms `HPP_Nyx_KeegerBelt` mining ~10% + salvage — the #35 doc's "not
physicalized" call was wrong, corrected in its §4). Fits are derived at
nav-rebuild, never stored; no new tables; solver untouched.

### 35. Halo Finder multi-system expansion — Glaciem Ring + Pyro fields ✅ BUILT

**Status: built 2026-07-15 (same day as the design), browser-verified via the
preview harness (Nyx pocket hit 1,463 km staged from Levski; Akiro fly-by
6,258 km via RAB-JAK; Stanton band flow regression-clean). NOT in-game
verified — the design doc's §7 unknowns (do Wtn pockets spawn rocks
contract-free, ring QT obstruction, RMB rock density) still need a flight.**
Suites 367/244 green. Full design + build notes:
[`halo-finder-expansion.md`](halo-finder-expansion.md). Extends #31 to the
**Nyx Glaciem Ring** (circumstellar ring at 15.000 Gm — but only ~4% of the
circumference holds rocks, in 381 datamined pocket containers we already ship
in `containers.json`, so the planner aims chords at **pocket centers**, not a
radius crossing) and **Pyro's 102 unmarked resource fields** (PYR L-points +
RMB sites, coords already in `locations.json`; Akiro Cluster = the PYR1-L3
field). Key decisions: per-system belt registry on `NavData` (`bands` /
`ring+pockets` / `fields`), new pocket mode = POI closest-approach over a
target *set*, `glaciem_contains` joins the system-disambiguation ladder with a
fresh-sticky-beats-geometry rule (Stanton traffic crosses 15 Gm constantly —
regression case pinned in the doc), band mode deliberately NOT offered for
Nyx. **Pyro VI / Pyro V planetary rings don't exist** (researched — lore only)
and the Keeger Belt isn't physicalized yet; both explicitly out of scope. No
new tables/deps/sync tools. In-game unknowns to verify listed in the doc §7.

### 34. Trade planner: stop kinds for big haulers ✅ BUILT

**Status: built 2026-07-12, browser-verified (headless harness, real Hull-C
plan).** A Hull-C has no landing gear — it can *only* moor at a station cargo
dock — and even ships that can land planetside find surface outposts a chore in
a big hull. The planner now takes `stops` = `any | stations | dock`:
`stations` drops planet/moon surface stops, `dock` keeps only stations with a
cargo dock.

The win was that **the data already knew**: uexcorp flags `is_loading_dock` on
exactly five ships (Hull C/D/E, Kraken ×2) and `has_loading_dock` on terminals —
so we didn't need a hand-curated station list. The catch is that the terminal
flag is per *desk*, not per station (Levski declares its dock on "Cargo
Services", never on its commodity desk), so it has to be OR-ed across the
unfiltered feed. Plus a gateway rule (UEX omits the flag on the Nyx-side
gateways; every gateway has a cargo deck). Yields exactly the in-game Hull-C
set of 14 stops. Magnus Gateway is absent from the feed entirely — nothing to
do until UEX carries it.

Design detail worth remembering: `exclude_poi_ids` is a **separate** solver set
from `avoid_poi_ids`, because the held-cargo re-plan deliberately *ignores*
danger (you can run a blockade to offload sunk cargo) but must never ignore
physics (no daring lands a Hull-C on a moon). Full write-up in
[`trade-route-planner.md`](trade-route-planner.md#stop-kinds--the-big-hauler-filter-34--as-built).

**Possible follow-on:** the cargo-hauling planner (#12) has the same problem —
its contract stops are player-entered, so it can't *drop* them, but it could
badge a stop the chosen ship can't use. Not built.

### 33. Scheduled UEX feed refresh (admin-configurable) ✅ BUILT

**Status: built 2026-07-11 with #32, browser-verified.** Before this, uexcorp
feeds (commodities/items/terminals/prices) loaded **once at process startup**;
the only later refresh was the curl-only admin `POST /api/refresh` — prices
were as old as the last deploy. Now `feed_refresh_loop()` (started alongside
the presence broadcaster) re-pulls the feeds on a schedule: org setting
`feed_refresh_h`, **default 6h, hard 2h floor** (be kind to the community-run
UEX API — admins can go longer, never shorter; API rejects <2 with 400, the
reader clamps hand-edited meta rows up), `0` = off, cap 720h. Setting changes
apply without restart (re-read every 5-min tick). Shared `_refresh_feeds()`
body now also backs the manual endpoint — which finally has UI: ORG SETTINGS
"UEX PRICE DATA" panel with interval input, "prices as of" readout
(`feeds_refreshed_at`), and a **Refresh now** button. The scheduled pass
refreshes feeds only (starmap stays manual — it only changes with game
patches). Bonus fix: `/api/refresh` previously dropped per-ship quantum
enrichment (#27) until restart; `_refresh_feeds` re-applies it.

### 32. Ore value badges — "pause and mine, or keep surveying?" ✅ BUILT

**Status: built 2026-07-11, browser-verified via the preview harness.** While
surveying, every place an ore/harvestable name appears in the navigator now
carries a relative-value badge (`$$$`/`$$`/`$`, tooltip = ≈aUEC/SCU sell ref)
so the player knows instantly whether a scanned node is worth stopping for.
Data: the already-cached uexcorp commodities feed — raw ores without their own
sell price fall back to their refined commodity ("Quantainium (Raw)" →
"Quantainium"); those badges carry a trailing **asterisk** (`$$$*`, tooltip
states the refined basis) so raw-vs-refined pricing is never conflated
silently. Genuinely unpriced names (Ice, rubble) get **no** badge rather
than a misleading "low". Tiers are rank terciles *within* each category
(ores vs ores, harvestables vs harvestables; `nav_core.resource_value_tiers`),
so buckets survive patch-day price rebalances and the 23M-aUEC Jaclium outlier
can't squash the scale. New `GET /api/resource_values` (rebuilt on
`/api/refresh`); badges on: resource forecast, NEARBY detail, element-finder
picker options + status line, destination panel, ADD RESOURCE NODE live hint +
capture confirmations. 37/44 ores + 7/10 harvestables badged with today's cache.

### 31. Halo Finder — Aaron Halo drop planner (tenth app) ✅ SHIPPED

**Status: built 2026-07-10 (same day as the design).** Full spec + build notes:
[`halo-finder.md`](halo-finder.md). The tenth app (`#/halo`): pick a density
band (band 5 = the ~3×-dense jackpot) or a deep-space custom POI, get "set
destination X, exit QT at D km" with an enter/peak/exit drop *window*
(+ seconds at your drive's speed), a staging hop when the sun/geometry blocks
every direct chord, the patch-proof star-marker fallback number, and post-drop
`/showlocation` verdicts ("you're in band 5, 12,400 km past the inner edge")
with a Refine loop for POI targeting. Passive extras: the navigator's
"☄ Halo band N" deep-space chip and automatic band annotation on deep-space
captures. Geometry golden-tested against Cornerstone's published chart numbers
(4 fixtures, all ≤5,000 km off); the prereq `_frame_at` Unknown-system fix
(deep-space captures were unroutable) shipped with it.

### 26. SC Wiki API reference-data layer (foundation) ✅ COMPLETE

**Status:** all three slices shipped — vehicles/quantum **v0.37.0**
(`tools/sync_quantum.py` → `poi/quantum_{drives,profiles}.json`, 230 profiles /
57 drives / 81% hauler coverage), blueprints **v0.40.0**
(`tools/sync_blueprints.py` → `poi/blueprints.json`, 1,559 recipes), locations
**v0.46.0 with #28** (`tools/sync_locations.py` → `poi/locations.json`, 634
records). Footer carries the CC BY-SA 4.0 attribution; every artifact is
version-stamped, no runtime API calls, manual per-patch re-run. Kept below as
the wiki-API reference:

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

---

## Fast-follows by app

Small, unblocked items harvested (2026-07-04) from every spec doc's
Deferred/Open sections, so they stop hiding in eighteen files. Grab
opportunistically; none is urgent.

- **Trade planner (#21):** teammate-lane-awareness ("someone's already running
  this lane" — needs a presence-side design pass first) · exact B&B "thorough"
  solver option under a ≤4-stop cap · pad-size-vs-ship warning on stops (#28c
  chips already show the stop's max hangar/pad; needs ship size class plumbed
  through `sync_quantum.py` — the uexcorp feed has none).
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
| 26/28 | Wiki locations catalog: `wiki_pois_enabled` import (241 wiki-only POIs + 206 QT-marker promotions → 508 QT destinations) · per-POI QT arrival radii in run-mode arrival · trade-stop amenity chips (freight elevator / loading dock / hangar-pad / clinic) | v0.46.0 | [wiki-poi-enrichment.md](wiki-poi-enrichment.md) |
| 31 | Halo Finder (tenth app): Aaron Halo band/POI drop planner, staging hops, star-marker fallback, post-drop verify + Refine, navigator belt chip, capture band annotation (+ `_frame_at` deep-space fix) | 2026-07-10 | [halo-finder.md](halo-finder.md) |
| 31 | Halo Finder fixes + map: endpoint-aware obstruction (low-orbit/surface starts, v0.51.1) · HALO MAP system view + drop-zone inset · sticky session system for deep-space ambiguity (v0.52.0) | v0.51.1 · v0.52.0 | [halo-finder.md](halo-finder.md) |
| 31 | Halo Finder deep-space system fixes: `halo_contains` ring-envelope makes `system_at` resolve an in-belt fix to Stanton (v0.52.1) · plan/locate resolve start system confidence-first (container > in-belt > sticky > guess) so a stale sticky no longer false-rejects, "my current location" start now arms→awaits next /showlocation (v0.52.2) — user-verified in-game | v0.52.1 · v0.52.2 | [halo-finder.md](halo-finder.md) |
