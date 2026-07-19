# Survey platform — from drop helper to org prospecting suite (backlog #37) — design plan

**Status: 🔨 IN BUILD (2026-07-19).** Slices 0–4 are BUILT (radar layers
v0.64.0 · value layer v0.65.0 · ore-first routing v0.66.0 · scan detail +
zone detail v0.67.0 · arrival routing v0.68.0 · coverage gaps + overview
map §5.1); §5.2–§5.3 and phases 4–5 (§6–§7) remain design. Successor to
the shipped #36/#36.1 stack
([belt-survey.md](belt-survey.md), [survey-zones.md](survey-zones.md),
[halo-finder-expansion.md](halo-finder-expansion.md)); assumes the v0.60–v0.63
scaling work (version-keyed `survey_state` cache, incremental QT maintenance,
off-loop dataset refresh) as its performance foundation.

---

## 1. Where we are, and the gap

What shipped through v0.59.0 answers **"get me into the rocks and let me mark
what I find."** Marks are ground truth with first-class negatives; zones give
deliberate membership and a name; geometry is always derived from the marks;
the radar navigates a pocket; the export carries provenance. That is a
*mapping* tool.

The goal is a *prospecting* tool: **"where should the org spend its mining
hours tonight, and how do we know?"** Four gaps stand between here and there:

1. **No economics.** A zone knows its ores but not what they're worth. The
   `$$$` value-badge machinery (#32) already prices every ore — it just never
   meets the survey data.
2. **No ore-first retrieval — the payoff loop.** On planets the org already
   lives the right workflow: element finder → pick an ore → ranked
   high-probability areas → destination set → fly the bearing. Deep-space
   survey data feeds none of that. A miner who wants Quantainium tonight
   can't ask the map for it — they'd have to remember which zone had it.
   Until surveys route miners, the map is scenery.
3. **No direction.** Surveying is wandering. Nothing says "this arc is
   unmapped," "this zone needs 6 more marks for a model," or "you've drifted
   past your own coverage."
4. **No lifecycle.** Marks live forever with equal weight. A game patch can
   move a field; the org can't tell "confirmed last week on this build" from
   "mapped three patches ago." Sharing between orgs is export-only.
5. **Rocks only.** Salvage is a boolean; wrecks, ice, gas clouds, and
   surface prospecting (ROC/FPS) have no home. Exploration players log more
   than asteroids.

Each phase below is an independently shippable slice, ordered by
leverage-per-risk. Phase 1 is the one that changes what the tool *is*.

## 2. Design invariants (carried from #36, non-negotiable)

- **Marks are ground truth; derived products are never stored.** Every new
  aggregate (value score, coverage, staleness) is recomputed from marks at
  read time behind the `nav.version`-keyed `survey_state` cache. Deleting a
  mark heals everything.
- **One-tap stays one-tap.** No new required fields on the ⛏ flow, ever.
  Richness is always an optional add-on, and the "nothing here" negative
  remains a single tap.
- **Honesty over precision.** Value and coverage are tiers and fractions with
  a stated basis, never fake aUEC decimals — the `$$$*` refined-basis
  asterisk (#32/v0.53.1) is the house pattern.
- **Instrument, not game.** Milestones and leaderboards inform coordination;
  no badges, streaks, or confetti (PRODUCT.md anti-reference).
- Additive storage only: new keys inside the existing `custom_pois.survey`
  JSON blob; `_ensure_column` for anything else; existing marks never
  rewritten.

## 3. Phase 1 — the value layer ("is this field worth mining?")

### 3.1 Optional scan detail on a mark — ✅ BUILT (slice 3, 2026-07-18)

The in-game scanner shows a rock's **mass** and **composition percentages**.
Those two numbers turn "Iron, Quartz" into an actual value estimate. The
payload gains one optional block:

```json
{"rocks": "dense", "ores": ["Quantainium (Raw)", "Iron (Ore)"],
 "scan": {"mass_kg": 3520, "comp": {"Quantainium (Raw)": 21, "Iron (Ore)": 44}}}
```

- Caps per house guardrails: `mass_kg` 1–10,000,000; `comp` ≤ 8 entries,
  percentages 0–100 (sum NOT validated to 100 — players type what they see,
  inerts make up the rest); ore names validated against the shared vocabulary
  the datalist already serves.
- **Flow decision — scan detail attaches to the LAST mark, after the fact.**
  Typing percentages mid-flight competes with flying; the ⛏ tap must stay
  instant. AFTER-THE-DROP gains a collapsed "＋ scan detail" row that appears
  once a mark resolves and PATCHes the just-created mark (new
  `PATCH /api/custom_pois/{id}/survey`, owner-or-admin, survey-type POIs
  only). Same row works for any mark selected from the zone detail view
  (§3.4), so a second crewman can enrich while the pilot flies.

### 3.2 The value model (nav_core, pure) — ✅ ALL BASES BUILT (ores/density slice 1; "scanned" slice 3)

`survey_value(cluster, prices) -> {score, tier, basis}` where `cluster` is
any pocket/zone dict with a `survey` block and `prices` is the existing
`build_resource_values` output (refined-price fallback included):

- **basis "scanned"** — any member mark carries `scan.comp`: score =
  density weight × Σ(mean comp% × price per ore). The strongest signal.
- **basis "ores"** — ores listed, no scans: score = density weight × mean
  price of the listed ores. Today's data gets this immediately.
- **basis "density"** — positive marks, no ores: density weight × the
  category's median ore price. Weakest, still rankable.
- Density weights: none=0, sparse=1, medium=2.5, dense=5 (tunable constants;
  the ratios matter, not the units — scores only ever compare to each other).
- **Tier = terciles across the org's surveyed clusters**, exactly the #32
  `resource_value_tiers` approach → the existing `$$$ / $$ / $` chips render
  unchanged, with the basis in the tooltip ("value basis: 3 scans" / "ore
  list only" / "density only"). Salvage-flagged clusters get a fixed `⚙`
  annotation instead of joining the ore terciles (different economy, no
  honest common denominator).
- **Build decisions (slice 1, user-confirmed):** the tercile pool is
  **per-system** (`app._survey_valued` pools surveyed pockets + org-overlaid
  Glaciem pockets + named zones in one cut — "$$$" means "best in this
  belt"); a **mixed salvage+ore cluster keeps its ore tier AND the ⚙
  marker** — only salvage-with-no-positive-rocks clusters are ⚙-only and
  untiered; barren/no-signal clusters carry no `value` at all (no chip —
  the status line already says barren). Implemented as
  `nav_core.survey_value` / `annotate_survey_values` (non-mutating; price
  lookup is suffix/case-tolerant against the `raw_commodity_names` keys).

Computed on read inside `survey_state` consumers; the math is trivial next to
the clustering that's already cached, so no separate cache entry — but price
data changes on feed refresh, which already bumps nothing in `nav.version`.
**Decision:** `_refresh_feeds` calls `nav.touch()` after rebuilding
`resource_values` so value tiers refresh with prices (the feeds path already
rebuilds catalogs; one touch is consistent with the mutation contract).

### 3.3 Where value shows up — ✅ BUILT (slice 1) except the §5.2/§4.2 surfaces

Zone banner and zone `<select>` options ✅ · Nyx/Keeger target panels ✅
(value-ranked surveyed-pocket chips + picker-datalist labels) · plan cards ✅
("⛏ org survey · 12 marks · $$$", alternates rows too) · halo map dots
tinted by tier ✅ · the export ✅ (score + basis per cluster, so a shared
dataset carries its economics) · Org Intel (§5.2's Surveying section,
pending) · every row of the ore-first finder (§4.2, pending). Also landed:
`_refresh_feeds` now calls `nav.touch()` (the §3.2 decision) so a price
refresh re-cuts cached tiers.

### 3.4 Zone detail view — ✅ BUILT (slice 3: inline "▸ details" expansion; staleness column waits for §6.1)

The zones panel gains a per-zone expansion (not a new app): mark timeline
(who, when, density, ores, scan), contributor list, ore breakdown with
per-ore mean comp% when scanned, value tier + basis, staleness (§6.1), and
the "plan a drop here" / rename / close / delete actions that already exist.
This is also where "＋ scan detail" attaches to an arbitrary mark.

## 4. Phase 2 — ore-first routing ("take me to the Quantainium") — ✅ BUILT (slice 2, 2026-07-18)

The planetary reflex, extended to the belts: **select an ore, get directed to
the most efficient high-probability place the org has mapped, one tap from a
drop plan.** This is the feature that converts survey hours into mining
hours, and it works on TODAY'S marks (they already carry ores). Every later
phase sharpens it: scans add composition confidence, staleness discounts old
intel, coverage gaps answer "and if we haven't mapped a source yet, go survey
HERE."

### 4.1 Scoring: likelihood × efficiency (nav_core, pure)

`find_ore_in_space(nav, ore, from_pos=None, qd=None) -> ranked candidates`
over every survey cluster that could hold the ore — named zones, SVY
proximity pockets, and datamined Glaciem pockets carrying a survey overlay:

- **Likelihood** = (fraction of the cluster's positive marks listing the
  ore, shrunk toward the belt-wide prior by mark count — the exact
  `body_base_rate` shrinkage thinking, so a lucky 2-mark zone can't outrank a
  proven 20-mark one) × density weight × mean scan comp% for the ore when
  any member mark carries scan detail (§3.1).
- **Efficiency** = quantum travel cost from the live fix (`travel_cost`,
  standard hazard avoidance) **gated by drop plannability**: a cluster the
  miss ceiling rejects ranks below a plannable one no matter its likelihood,
  and is labeled "expedition — creep N km from <drop point>" rather than
  hidden.
- **Freshness** (once §6.1 ships): unverified-this-patch clusters are
  discounted, never hidden.
- Three sort modes, mirroring the element finder verbatim: **likely**
  (ignore distance) · **near** (closest plannable first) · **value**
  (likelihood × §3 value tier, travel-discounted).

### 4.2 Where the miner touches it

- **The element finder grows a DEEP SPACE section.** Same ore picker the
  miner already uses; results split "on planets" (today's observation
  groups) and "in the belts" (survey clusters) under one visual vocabulary.
  Each belt row: cluster name, mark count, likelihood, `$$$` tier + basis,
  distance, and **⤓ Plan drop** — which pins the cluster into the halo
  planner and comes back with guidance armed. One flow from "I want
  Quantainium" to an EXIT AT number on the HUD.
- **Halo Finder gains an `⛏ Ore` goal.** Pick an ore; AUTO targets the
  top-ranked cluster reachable from the start, overridable through the same
  alternates row the planner already renders for pockets.

### 4.3 Honesty rules

- Below ~3 positive marks a cluster shows "**1 mark**" / "**2 marks**", not
  a likelihood percentage — early data guides, it doesn't promise.
- "**No mapped source for <ore> in reach**" is a first-class answer, and it
  links straight to the NEXT GAP suggestion (§5.1) — a retrieval miss
  becomes survey direction instead of a dead end.
- Cross-system candidates are listed but labeled "travel there first" (the
  same-system start rule); ranking never silently mixes systems.
- Routing concentrates the org on the top result, and rocks deplete
  server-side — ~~see open question §11.6~~ **decided + shipped with the
  slice**: a one-tap "⛏ mined out" report (element-finder rows) files into
  the `survey_depletion` table (stock-report recipe: one live row per key,
  prune-on-read), down-ranks the cluster in ore routing ONLY (never hidden,
  survey record untouched), ages off via org setting
  `survey_depletion_ageoff_min` (default 240 = a 4 h play session, a user
  call). The `⛏ Ore` plan goal skips fresh-depleted clusters outright.

### 4.4 Build notes (slice 2, 2026-07-18)

- Endpoint decision (§8): a **sibling `GET /api/survey/find?ore=&sort=`** —
  the cluster rows share almost nothing with `resource_hotspots`' cell rows.
  Fix-system rows rank with travel + plannability; every other system rides
  in `elsewhere` ("travel there first") so rankings never mix systems.
- Likelihood = (listing marks / positive marks) shrunk toward the pool rate
  by `RESOURCE_PRIOR_STRENGTH`, × `SURVEY_DENSITY_W` — `survey_cluster_fit`
  grew an `ore_counts` tally to feed it. Scan-comp% term waits for §3.1
  (slice 3); the freshness discount waits for §6.1 (slice 6).
- Plannability probe is geometric, not a solver call: min `_seg_point_dist`
  over start→marker chords vs the miss ceiling → hit | plannable |
  expedition (+ creep distance). The real solver runs only on "⤓ Plan drop".
- The `⛏ Ore` goal hands the top-8 ranked clusters to `plan_halo_drop` as
  the pocket pool — cross-cluster alternates come free from the
  marker×pocket scan.
- The element-finder picker now unions survey-mark ores into
  `/api/resource_ores`, so a belt-only ore is findable.
- **Arrival plans + staging cost sanity (routing fix, found in-game
  2026-07-18):** a live Levski→SVY-29 plan staged 70+ Gm through People's
  Service Station Alpha to reach rocks hugging QV Breaker BRK-320, because
  the solver's only maneuver was "early exit before the marker" — a marker
  INSIDE the pocket can never satisfy the run-up floor or the
  between-endpoints clamp. `_halo_arrival_candidate` adds the missing move:
  "jump to the marker, let the jump complete, you arrive in the rocks"
  (bypasses `HALO_DROP_MIN_M`; still obstruction-checked; drop card renders
  **ARRIVE AT <marker>**). Plus `HALO_STAGE_COST_X`/`HALO_DIRECT_MISS_OK_X`:
  a staged hit that flies >10× the direct chord no longer beats a direct
  near-miss within 3 pocket radii — sublight closes that gap far faster
  than crossing the system twice.

## 5. Phase 3 — direction ("where should I survey next?")

### 5.1 Coverage gaps, honestly — ✅ BUILT (slice 4, 2026-07-19)

The trap: most of a 48 Gm ring is **not drop-plannable** (the
`POCKET_MISS_CEILING_M` lesson — with sparse markers, only station-approach
chords produce honest drops). A gap suggester that points at unreachable arc
is worse than none. Two-tier design:

- `nav_core.survey_gaps(nav, system, from_pos=None)`: sample the belt ring at
  a fixed angular step (candidate points every 0.25° ≈ 210,000 km on Keeger —
  coarse enough to stay ~1,440 points, well under a millisecond against a few
  hundred marks; cached in `survey_state`). A candidate is **covered** when a
  mark or zone centroid sits within `SURVEY_MERGE_M`; else it's a gap.
- Each gap is classified **plannable** (a drop plan from the org's markers
  reaches it inside the miss ceiling — reuse the existing solver in
  check-mode, bounded to the N nearest gaps, not all of them) or
  **expedition** (reachable only by sublight creep; reported with the nearest
  plannable drop point and the creep distance from it).
- Surfaces: a "NEXT GAP" line in the Keeger/zone panel ("nearest unmapped
  arc: 0.8 Gm past SVY-1000241 — drop there, creep 0.4 Gm outward"), hollow
  arc segments on the halo map, and a `gap` goal on `HaloPlanIn` that plans
  the drop leg of the nearest plannable gap.
- Coverage fraction (the tier-2 model already computes one) gets a plain
  progress line per belt: "Keeger arc surveyed: 3.1%" — an honest number that
  doubles as the org's long-campaign scoreboard.
- **Build decisions (slice 4, 2026-07-19):** the sampler is an **exact
  angle-interval union**, not the 0.25° ring sampling sketched above — a
  mark covers only ±SURVEY_MERGE_M (±0.0124°) of the 48 Gm ring, far finer
  than any sane step, so sampling would miss real coverage between samples;
  the union is O(marks log marks) and exact (`nav_core.survey_gaps`, cached
  in `survey_state`). Plannability is the slice-2 chord-miss probe over
  public **marker-pair** chords (start-independent), bounded to the largest
  `GAP_PROBE_MAX` arcs. Gaps ride `/api/halo/targets` (the §8
  "decide at build" call). **Always-on overview map (user ask, same
  intent):** the Halo Finder now renders a per-system overview map on load —
  belts, surveyed pockets/zones tinted by value tier, gap arcs in amber,
  stations + gateways (`doc.markers`) — with **click-to-pin** (tap a
  pocket/zone/field to set it as the plan destination) and the coverage
  line as its caption. The NEXT GAP line + "⛏ Survey the next gap" button
  live in the Keeger panel; the `gap` goal resolves the most reachable arc
  into a synthetic pocket, so arrival/staging/cost-sanity all apply.

### 5.2 Survey activity — derived, not stored

No `survey_runs` table. A **survey session** is derived from the marks
themselves (owner + zone + `created` gaps under 30 min bridge a session),
exactly the derive-don't-store philosophy — deleting marks heals the stats.
`nav_core.derive_survey_stats(marks)` produces per-member and per-zone
tallies (marks, positives, scans, sessions, first/latest) feeding:

- a **Surveying section in Org Intel** (`#/intel/surveying`, sibling of the
  Trading section pattern): org totals, coverage per belt, top contributors,
  freshest zones. Ranked lists are fine; they're logistics, not achievements.
- **Discord milestones** (opt-in, new `notify` category `survey`, the
  standard cooldowns): zone created with `announce` (LFG-style flag), zone
  reaches the field-model gate, a belt's first model fit. Threshold
  crossings only — never per-mark.

### 5.3 Radar nudge (client-only)

The radar already tracks drift between fixes. When the current fix sits more
than ~½ zone radius from the last mark in the active zone, the radar tip line
gains "you've drifted past your last mark — ⛏ here keeps the map dense."
Pure client logic on data it already has; no server change.

### 5.4 Radar reference POIs ✅ (slice 0, built 2026-07-18)

The radar drew only center/path/player — but existing POIs near a pocket are
reference points *independent of the pocket definitions*, and the QT-marked
ones are visible in-game from the cockpit: the only true orientation cue deep
space offers. `nav_core.radar_ref_pois` (active + org-visible + same-system +
non-survey POIs within reach, nearest-first, capped) feeds
`GET /api/halo/radar/refs`; the client fetches once per radar pocket key
(landmarks don't move) and glyph-codes by provenance — bright diamond =
QT-marked (in-game visible), dim diamond = catalog POI without a marker,
square = org/custom pin. POIs beyond the current zoom render as rim ticks
with name + distance: the direction cue matters more than the position.
Same-system filtering is load-bearing (every system centers on its own
origin); private POIs show only to their owner.

### 5.5 In-pocket heatmap ✅ (slice 0, built 2026-07-18)

The planetary heatmap reflex applied inside a pocket: do mineables spawn in a
pattern, and does that pattern move over time? `nav_core.survey_heat_cells`
mirrors `resource_cells` on the pocket plane — marks bucketed into square
(dx, dy) cells (top-down like the radar; adaptive nice-rounded cell ≈ ⅛
pocket radius), per-cell mark counts, mean density weight
(`SURVEY_DENSITY_W`, the same constants §3.2's value model reuses), plurality
`top` ore, composition shrunk toward the pocket-wide rate, and `barren`
(surveyed-empty ≠ unsurveyed — negatives stay first-class). Served by
`GET /api/halo/radar/heat` (by pocket `key` or `zone_id`), derived per read.

- **Time is a window, not a mode:** an ALL / 7D / 24H seg re-aggregates over
  only the marks in the window — comparing windows by eye is the honest
  drift detector. This forced the `custom_pois.created` column (epoch,
  `_ensure_column`); pre-#37 marks have NULL = unknown age and appear only
  in ALL, never guessed fresh. (§5.2's session derivation was already
  assuming `created` — slice 0 pre-lands it.)
- **Heat ON switches the radar to analysis view** (span held at the full
  pocket radius so cells read as a map); heat OFF keeps the nav view's
  path auto-zoom. Modes: ROCKS (density, confidence-weighted alpha; barren
  cells in dim slate) and ORES (dominant ore in the shared `oreColor`
  scheme, so colors match the planet heatmap). A specific-ore mode can
  follow once dominant-ore proves out.

## 6. Phase 4 — lifecycle & sharing ("can I trust this data?")

### 6.1 Patch stamping and staleness

- The watcher already parses Game.log headers (shard-id precedent); it
  additionally extracts the **game build/version string** and sends it on
  `POST /api/position` (`game_build`, optional — old watchers keep working).
  The session carries it; `_capture_poi` stamps `survey.build` on marks.
- A zone/pocket's **freshness** is derived: the newest positive mark's build
  vs the org's current-majority build (majority across the last N position
  posts — also derived, held on the hub). Mismatch → an "unverified on
  <build>" badge on zone banner/detail/targets; any fresh positive mark on
  the new build clears it. No flags stored, no admin ceremony — the
  pirate-warning "confirm still active" idea, but automatic.
- The admin nuke (`/api/admin/survey/clear`) stays for fields a patch
  visibly deleted; staleness handles the common "probably still there" case.

### 6.2 Import (closing the #36 §3.6 deferral)

- `POST /api/admin/survey/import` — admin-only upload of another org's
  export document. Validation: `_meta` version check, mark-count cap
  (~5,000), coordinate sanity (inside a known system's envelope), payload
  caps as usual.
- Imported marks land as survey POIs with `survey.source_org` (from the
  export's attribution) and `pending: true` — **excluded from every
  derivation until approved**. A review panel in ORG SETTINGS (POI-QC-panel
  pattern) shows the batch (count, systems, zones, value summary) with
  approve/reject per batch, not per mark.
- Dedupe on approve: an imported mark within ¼ `SURVEY_MERGE_M` of an
  existing org mark with the same polarity is dropped (the org's own
  measurement wins); conflicting polarity keeps both (disagreement is
  signal — it surfaces as a mixed cluster).
- Imported zones arrive as closed zones named "<name> (via <org>)" — visible,
  plannable by pin, off the default target list until the org adopts them.

### 6.3 Promotion tooling (maintainer, offline)

`tools/promote_survey.py <export.json>`: prints the fitted model as a
committed-constants block next to a diff against the current constants, so
"promote a stable community survey the way `HALO_BANDS` shipped" becomes a
reviewed one-liner instead of hand-transcription. Pure tooling; no runtime
surface.

## 7. Phase 5 — beyond asteroids (scope expansion, each its own slice)

### 7.1 Mark kinds

`survey.kind`: `rocks` (default, absent = rocks — all existing data keeps
meaning) · `salvage` · `ice` · `gas` · `derelict` · `hazard`. The existing
`salvage` boolean stays readable but the kind supersedes it. Kind drives:
the mark glyph on maps, which value model applies (salvage/derelict join the
`⚙` annotation lane, not ore terciles), and one special case — a `hazard`
mark offers "⚠ also post to the Danger Board" (pre-filled `POST
/api/warnings`, the board→events cross-link precedent, never automatic).

### 7.2 Surface zones (design sketch — its own doc before build, #37.1)

Zones gain an optional surface anchor (`body`, `lat`, `lon`, `radius_km` via
`_ensure_column`); on-body marks localize by great-circle distance instead
of xyz. The radar becomes a lat/lon top-down plot (the navigator map
projection already exists), guidance reuses bearing/distance. This unlocks
ROC/FPS prospecting — likely the largest audience expansion in the whole
plan — but it touches the navigator's map stack, so it gets its own design
pass once deep-space phases 1–2 (value + ore routing) have proven the model.

### 7.3 Value-aware mining circuit (backlog once value + routing are live)

"Best expected aUEC/hour reachable with my ship and fuel": zone value scores
× `travel_cost` × a refinery-terminal endpoint (UEX already feeds refinery
locations to the trade stack). The trade-solver pattern applied to mining —
`plan zone → mine → refinery` as legs. Deliberately last: it extends §4's
single-destination routing into a multi-leg circuit, and it needs Phase 1's
value model plus Phase 3's coverage to keep suggestions fresh.

## 8. API summary (all additive)

- ✅ `GET /api/halo/radar/refs?system=&x=&y=&z=&r=` — reference POIs near a
  pocket center for the radar overlay (§5.4); r clamped server-side.
- ✅ `GET /api/halo/radar/heat?system=&key=|zone_id=&window_h=` — pocket-plane
  survey heatmap cells with the age window (§5.5).
- ✅ `custom_pois.created` column (epoch, `_ensure_column`) — stamped on every
  new capture; threads through `Poi.created` → `survey_marks` (§5.5, and the
  prerequisite §5.2 was silently assuming).
- ✅ `PATCH /api/custom_pois/{id}/survey` — attach/replace scan detail on an
  owned survey mark (§3.1; empty body clears; `kind` joins in slice 8).
  Marks views/export gained `zone_id`/`created`/`scan` (zone timeline feed).
  Routing note (slice 3): the §4.1 comp% term is **pool-relative** —
  unscanned clusters use the pool's mean scanned comp% as their multiplier,
  so contributing a scan never down-ranks you against ignorance.
- `GET /api/halo/survey` + `/export` + `/zones` — rows gain `value`
  (`{score, tier, basis}`), `freshness`, `kind` rollups; export `_meta`
  version bumps.
- ✅ `GET /api/survey/find?ore=&sort=` — §4.1's ranked clusters (sibling
  endpoint; decided at build — the responses share almost nothing).
- ✅ `HaloPlanIn.ore: str` — the `⛏ Ore` goal: AUTO-target the top-ranked
  clusters for the ore (§4.2); depleted skipped.
- ✅ `POST /api/survey/depleted` + `POST /api/admin/survey/depletion/clear` +
  org setting `survey_depletion_ageoff_min` (§4.3 mined-out, §11.6).
- ✅ `GET /api/resource_ores` unions survey-mark ores (finder picker).
- ✅ `doc.gaps` on `/api/halo/targets` (folded in — decided at build) +
  `doc.markers` (public station/gateway landmarks for the overview map).
- ✅ `HaloPlanIn.gap: bool` — plan the most reachable plannable gap.
- `GET /api/intel/surveying` — derived org survey stats (§5.2).
- `POST /api/admin/survey/import` + `GET/POST /api/admin/survey/imports`
  (batch review) (§6.2).
- `POST /api/position` gains optional `game_build` (§6.1).
- New `notify` category `survey` in org settings (§5.2).

## 9. Build order (each row ships alone)

0. ✅ **Radar reference layers** (§5.4–5.5, user-prioritized; built
   2026-07-18): POI overlay + in-pocket heatmap with the age window.
   Pre-lands `custom_pois.created` (needed by §5.2/§6.1 later) and
   `SURVEY_DENSITY_W` (reused by §3.2's value model).
1. ✅ **Value layer core** (§3.2–3.3, built 2026-07-18): `survey_value` +
   tiers on every existing surface, ores/density bases only. No new inputs
   needed — the org's current marks light up immediately. *Smallest slice,
   biggest reframe.*
2. ✅ **Ore-first routing** (§4, built 2026-07-18): `find_ore_in_space` +
   the element finder's IN-THE-BELTS section + the `⛏ Ore` plan goal, plus
   the §11.6 mined-out report (decided into this slice). Also needs no new
   inputs — with slice 1 it completes the survey→mine loop on today's data.
   *The payoff feature; shipped before asking surveyors for anything more.*
3. ✅ **Scan detail** (§3.1) + zone detail view (§3.4), built 2026-07-18:
   the "scanned" basis, sharpening both value tiers and routing likelihoods.
4. ✅ **Coverage gaps + NEXT GAP + map arcs** (§5.1, built 2026-07-19) +
   the always-on per-system overview map with click-to-pin (user ask).
5. **Survey stats + Intel section + Discord milestones** (§5.2) + radar
   nudge (§5.3).
6. **Patch stamping + staleness** (§6.1) — watcher release rides along;
   routing picks up the freshness discount for free.
7. **Import + review queue** (§6.2); promotion tool (§6.3) anytime.
8. **Mark kinds** (§7.1).
9. Surface zones (#37.1 doc first) and the mining circuit — separate designs.

## 10. Test plan (per slice, house standard)

- ✅ slice 0: `radar_ref_pois` radius/cap/system-isolation/visibility/
  disabled/survey-exclusion; `survey_heat_cells` bucketing, adaptive cell
  size, comp shrinkage, multi-ore plurality + deterministic tie-break,
  barren flag, window filter incl. NULL-`created` (ALL-only), density
  means; endpoint auth/params/clamps/404s; `created` stamped on capture
  and threaded to marks.
- nav_core: `survey_value` bases/tiers/salvage-lane fixtures incl. empty and
  single-cluster tercile edges; `find_ore_in_space` fixtures — shrinkage
  (2-mark fluke vs 20-mark proven), plannability gate + expedition labeling,
  cross-system flagging, all three sort modes, the sub-3-mark honesty
  rendering; gap sampler coverage classification +
  plannable/expedition split against synthetic marker layouts;
  `derive_survey_stats` session bridging; freshness derivation across build
  changes; import dedupe polarity rules.
- app: scan PATCH ownership/caps/type guard; `game_build` threading
  position→mark; import validation/pending-exclusion/approve flow; gap and
  stats endpoints; notify cooldowns.
- Browser (preview harness): value chips on zone banner/targets/plan card in
  both themes; zone detail; NEXT GAP card; import review panel.
- Every new derivation keyed on `nav.version` gets a cache-invalidation test
  (the count-aliasing lesson from v0.60.0).

## 11. Open questions (answer before or during build)

1. ~~**Scan ergonomics**~~ — DECIDED at slice-3 scoping (user): full form
   with EVERYTHING optional — one "best ore %" is a valid scan, and a parked
   surveyor can transcribe the whole readout. One form serves both.
2. **Density weights:** tune the 0/1/2.5/5 weights against a real evening of
   marks. (~~Tercile pool~~ — decided at slice-1 build: per-system.)
3. ~~**Gap step size**~~ — MOOTED at slice-4 build: the exact interval
   union has no step (see §5.1 build decisions).
4. **Majority-build derivation** (§5.1): is hub-side majority robust with a
   handful of watchers, or should the admin pin the org's current build in
   settings instead?
5. **Import trust ceiling:** is batch-level approve enough, or do orgs want
   per-zone cherry-picking on import?
6. ~~**Mined-out reports (§4.3)**~~ — DECIDED at slice-2 scoping (user):
   yes, in the routing slice itself. Minimal shape shipped: routing-only
   down-rank, survey record untouched, 4 h default age-off
   (`survey_depletion_ageoff_min`), no Discord. See §4.3/§4.4.

## 12. Not in scope

- Real-time scanner integration (no game API exists; transcription only).
- Cross-org live federation (import/export documents only).
- Auto-pricing salvage/derelict finds (no honest common denominator with ore
  prices; they get the `⚙` lane, not fake tiers).
- Any change to the one-tap ⛏ flow's required inputs.
