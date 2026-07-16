# Halo Finder multi-system expansion (backlog #35) — design plan

**Status: ✅ BUILT 2026-07-15 (same day as the design), browser-verified via
the headless preview harness; NOT yet in-game-verified (§7 unknowns stand).**
Build notes / deviations from the plan below:
- Shipped as designed: `nav.belts` registry (`build_belt_registry` /
  `glaciem_pockets` / `pyro_fields`, end of nav_core.py), pocket mode in
  `plan_halo_drop` (`pockets=` kwarg, one candidate per (marker, pocket)
  pair, alternates deduped to distinct markers), `glaciem_contains` +
  `glaciem_locate` + `field_locate`, `system_at` Glaciem rung,
  `Session.system_t` freshness (`SYSTEM_STICKY_FRESH_S` 30 min) +
  `_halo_fix_system` ladder, `GET /api/halo/targets`,
  `HaloPlanIn.system/pocket_key/field_uuid/include_mission`, per-system
  locate + capture notes (in-pocket captures annotate via the detected ring
  segment container itself), `#/halo` system seg + Nyx/Pyro target panels +
  per-system map/inset/chip/verdicts.
- **§3.4 refined during build:** with two belts, geometry outranks a fresh
  sticky ONLY when they agree — a fresh foreign sticky wins over BOTH
  envelopes (symmetric; Pyro traffic crosses ~20 Gm too). Stale sticky still
  loses to geometry (the v0.52.2 rule preserved; regression tests pin both).
- **Solver addition the design missed:** when NO stage yields an in-pocket
  hit (sparse-marker datasets), pocket staging keeps the smallest near-miss
  stage and applies POI mode's 0.6× materiality rule — without it a Levski
  start planned a 182,000 km direct miss instead of a 17,000 km staged one.
  Pocket scoring is hit-first, then POI-style miss-dominated among misses.
- **§3.7 deviation:** pocket pinning is a datalist input (+ the API
  `pocket_key`), not the tap-to-pin arc map — AUTO + distinct-marker
  alternates cover the real use; the arc map stays a possible polish pass.
- Marker-density reality check (matters for expectations): with the org's
  production toggles (starmap + wiki POIs ON) Nyx has ~11 markers and pocket
  drops HIT (Levski start → staged via a People's Service Station →
  1,463 km inside pocket Wtn-252; browser-verified end-to-end). With the
  toggles off (bare dataset) only gateway chords exist and plans are honest
  near-misses (~17,000 km staged). Akiro via RAB-JAK: 6,258 km miss.
- Bonus fix: `.seg[hidden]` CSS rule — the house seg component ignored the
  `hidden` attribute (author `display` beats the UA rule), which also
  affected the pre-#35 aim-seg-in-POI-mode path.

Extends the Halo Finder
(#31, `docs/halo-finder.md`) beyond Stanton's Aaron Halo to the **Nyx Glaciem
Ring** and **Pyro's deep-space asteroid fields**, with the same product intent:
drop a player out of quantum travel inside minable/salvageable space with
enough fidelity to make the trip pay. Read `docs/halo-finder.md` first — this
doc only covers what changes; the solver, obstruction handling, staging,
verify-and-refine loop, and map all carry over.

---

## 1. Research verdict (2026-07-15, web survey + local feed analysis)

| Area | Verdict | Why |
|---|---|---|
| **Glaciem Ring (Nyx)** | ✅ build — highest value | Live since Alpha 4.4 (2025-11); circumstellar ring at 15.000 Gm; full geometry already datamined in our committed `containers.json`; confirmed mining/salvage/missions; **no community survey or tool exists** — first-mover |
| **Pyro deep-space fields** | ✅ build — nearly free | 102 unmarked resource fields (16 PYR L-point fields + 86 RMB derelict mining sites) with exact coords already in `poi/locations.json`; existing POI-mode solver is the right shape |
| **Akiro Cluster (Pyro)** | ✅ as a named field target | No live QT marker or position record; anchor = PYR1-L3 at (−1.534, 8.431, 0) Gm; nearest live markers RAB-ALPHA 1.45 Gm, Checkmate 3.92 Gm — textbook mid-route drop |
| **Pyro VI / Pyro V planetary rings** | ❌ out of scope | **They don't exist.** Pyro VI ("Terminus") is a ringless protoplanet; Pyro V is a ringless gas giant. Galactapedia's "Pyro IV may one day become rings" is future-lore. The real content near Terminus = PYR6 L1/L2 fields + the 62–76 Gm cluster shell — covered by the Pyro field planner |
| **Keeger Belt (Nyx Belt Beta)** | ❌ not yet | Zero containers in the 4.4+ data dump — visual/lore only. Re-check per game patch |
| **Named Pyro clusters / RAB bases (47, `qt_valid`)** | already covered | They have their own quantum markers; the #28 wiki pipeline already imports them as targetable POIs. No drop planning needed — surface them, don't plan them |

## 2. Game facts (what's different from the Aaron Halo)

### 2.1 Glaciem Ring — a ring that is 96% empty

- Circumstellar ring around the **Nyx origin** at cylindrical radius
  **15.000 Gm** (datamined centers span 14.9968–15.0025 Gm), **z = 0 plane**
  (max |z| = 114 km, at the Levski segment). Delamar — with Levski — sits *on*
  the ring at exactly 15.000 Gm.
- **The minable content is 381 discrete pocket containers, not a continuous
  band**: 300 `Glaciemring_Segment_Wtn-*` (general), 80
  `…_Mission_Genrl_001_0xx` (QV Breaker mining-rights mission arcs), 1
  `…_Levski`. Each pocket's GRIDRadius is 5,196 km (~6,000 km cube). Along-ring
  coverage ≈ **4.2%** of the 94.25M km circumference; median Wtn spacing
  ~0.83° (~218,000 km of arc), worst Wtn gap 7.5°.
- **Consequence — the core design change:** the Aaron technique ("exit where
  your route crosses radius R") fails here; a random 15 Gm crossing is ~96%
  likely to be empty ring. The planner must aim chords at **pocket centers**.
  We have all 381 centers with exact XYZ in `poi/containers.json` (they
  already load into `nav.containers` as type `AsteroidBelt`; deliberately not
  synthesized into POIs). This is *better* data than Cornerstone's Stanton
  survey — exact placements vs. statistical bands — and needs no external
  dependency.
- Nyx QT-marker inventory is **sparse** (~a dozen): Levski, QVPS Station, four
  People's Service Stations (r ≈ 43–48 Gm), Pyro Gateway (24 Gm) + Castra
  Gateway (27 Gm) jump points (synthesized container-stations), planets/OMs.
  Nyx planets orbit far **off-plane** (z ±2.9–3.3 Gm) — unlike Stanton, the
  z-check does heavy lifting on Nyx chords; gateway/station→Levski chords are
  the workhorses (both gateways sit at z=0).
- Confirmed content (SC Wiki API + scminer.rocks): ship mining — Torite 28.5%,
  Bexalite/Borase/Gold ~18%, Ice/Aluminum/Corundum/Iron ~14%, Lindinium (epic)
  10%, Savrilium (legendary) 2%; salvage debris fields; QV Breaker
  mining-rights contracts inside the Mission segments.

### 2.2 Pyro — a point-field system, not a belt system

- **No circumstellar belt exists** (zero belt/ring containers; confirmed
  across wiki/starmap/navdata). Asteroid content is ~150 discrete sites at the
  planets' orbital shells.
- Already in `poi/locations.json` (game 4.8.2): **102 unmarked resource
  fields** — type `Asteroid`, `qt_valid=false`, `has_resources=true`: the 16
  `PYR1–PYR6 L1–L5` Lagrange fields (in-plane, at each planet's orbital
  radius) and 86 `RMB-*` derelict PyAm mining sites (uniformly ~15 Mm *below*
  the plane — the z-offset matters for drop math). Plus 47 `Asteroid_ValidQT`
  sites (19 named `Cluster XXX-###`, off-plane up to ±0.9 Gm; 26 `RAB-*`
  outlaw bases) that need no planner — they're QT markers.
- **Akiro Cluster** ("Pyro Cluster Alpha"): wiki record exists but has **no
  position row / no live marker** — treat it as a display name on the PYR1-L3
  field target.
- Content gap to be honest about in the UI: **no public per-field ore mapping
  exists** for deep-space Pyro (SCMINER covers planets/moons only). We guide
  to geometry; ore intel is a future enrichment (Regolith, or our own capture
  flow — every drop ends in `/showlocation` + a taggable POI).

## 3. Design decisions

### 3.1 One app, a per-system belt registry — not three apps

Halo Finder keeps its name and view; `#/halo` gains a system selector
(STANTON | NYX | PYRO). The Stanton-singleton constants (`HALO_SYSTEM`,
`HALO_BANDS`-as-the-only-model) become a **registry** built once at
`load_nav_data` time and attached to `NavData` (new field `nav.belts`):

- **Stanton** → `{kind: "bands", bands: HALO_BANDS}` — unchanged, survey-based
  (`HALO_BANDS` keeps its name; golden fixtures keep passing).
- **Nyx** → `{kind: "ring", r_m: 15.000e9, half_height_m: ~5.2e6, pockets:
  [...]}` — pockets derived from `nav.containers` (`system=="Nyx" and
  type=="AsteroidBelt"`), each `{key: "Wtn-022", kind: "general"|"mission"|
  "levski", xyz, grid_radius_m}`. **Derived, not duplicated** — a starmap
  refresh that moves the ring moves the planner automatically.
- **Pyro** → `{kind: "fields", fields: [...]}` from the wiki locations feed:
  records with `type.startswith("Asteroid") and has_resources and not
  qt_valid`, each `{uuid, name, xyz, shell}` (+ the Akiro display alias on
  PYR1-L3). Built from `locations.json` directly at load — **independent of
  the `wiki_pois_enabled` org toggle**: these are planner targets, not
  searchable catalog POIs, and the toggle exists to control catalog noise,
  not planner capability. (When the toggle *is* on, the same records also
  exist as wiki POIs — the plan view links them so tag/navigate flows work.)

### 3.2 Three target modes; the new one is "pocket mode" (multi-target)

The solver already has two modes: **band** (annulus crossings) and **POI**
(closest approach to one fixed point, staged (T, M) pair scan). The expansion
adds a third that is just POI mode over a target *set*:

- **Stanton**: band mode + POI mode — unchanged.
- **Nyx pocket mode**: the user picks "the ring" (optionally pins a specific
  pocket); the solver scans (marker M, pocket P★) pairs with
  `_halo_poi_candidate` and scores by miss + flown distance — i.e. it picks
  the pocket the geometry makes cheapest, which is the correct default when
  pockets are interchangeable. ~381 pockets × ~12 markers ≈ 4,600 cheap
  projections — microseconds; same `asyncio.to_thread` posture as today.
  A candidate only counts as "in the pocket" when `miss_m` ≲ the pocket's
  grid radius (5,196 km) + slack; otherwise it's reported honestly as a
  near-miss the way POI mode already does.
  Default pool = the 300 **Wtn general segments + Levski-adjacent**; Mission
  segments are a filter chip, OFF by default (their rocks are believed
  contract-gated via QV Breaker — unverified, see §7).
- **Pyro field mode**: the user picks a named field (searchable list grouped
  by shell: "Pyro I shell — Akiro Cluster (PYR1-L3)", "Terminus shell —
  PYR6-L2", "RMB-ZARF", …); single fixed target = **exactly the existing POI
  mode**, target resolved from the registry instead of a user capture. The
  RABs/named clusters (marker-holding) appear in the same list badged "direct
  jump" and short-circuit to a normal travel leg — no drop math.

Band mode is *not* offered for Nyx (a 96%-empty ring makes "anywhere in band"
an anti-feature); the one-row ring envelope still powers locate/map/system
disambiguation.

### 3.3 Solver changes are parameterization, not new geometry

`plan_halo_drop` gains `system` + registry-driven target resolution and one
new scan shape (pocket mode's target set — an outer loop around
`_halo_poi_candidate`, reusing the staged-pair machinery with the pocket set
swapped in for the single P★). `_ring_crossings`/`halo_band_crossings` are
already origin-generic — every system's frame centers on its own (0,0,0),
which is exactly the datum each belt needs. `body_volumes(nav, system)` is
already system-parameterized. `star_dist_peak_m` (the patch-proof fallback
readout) is "distance to the system's starmap marker" — origin in every
frame, so it generalizes untouched; only the label copy changes ("distance to
Nyx marker"). Foreign-start guard becomes per-system: "travel to Nyx first"
etc., same confidence-first rules as v0.52.2. Cross-system staging (gate
chains) stays deferred, as in #31.

### 3.4 System disambiguation: Glaciem joins `halo_contains` — carefully

`halo_contains` (Aaron envelope → Stanton) gets a sibling
`glaciem_contains` (r within ~14.95–15.05 Gm AND tight |z|) → Nyx, wired into
`system_at` and `app._halo_fix_system`. Two cautions, both load-bearing:

- **Tolerance must be much tighter than Aaron's.** Aaron uses
  `HALO_PLANE_TOLERANCE_M = 1 Gm` of z-slack; the Glaciem disk is ±5.2 Mm.
  Use ~50–100 Mm — generous for a real drop, still razor-specific.
- **The collision case is real this time.** No Pyro/Nyx body lives at
  Stanton's 19.7–21.3 Gm, but *Stanton traffic crosses 15 Gm constantly*
  (between Hurston 12.85 Gm and Crusader 19.1 Gm orbits). A Stanton player
  interdicted/dropped at r≈15 Gm, z≈0 must not get stamped "Nyx". Mitigation:
  in the `_halo_fix_system` ladder, belt geometry outranks a **stale** sticky
  system but a **fresh** sticky (container-confirmed within a TTL, e.g.
  30 min) outranks belt geometry. That requires stamping a timestamp on
  `Session.system` when a container confirms it — a small, general
  improvement. The Aaron branch keeps today's behavior (its radius band has
  no cross-system traffic in practice); regression tests pin both.

### 3.5 Locate/verify, captures, and the map generalize per system

- `halo_locate(pos, system=)`: Stanton → band verdicts (unchanged) · Nyx →
  "in pocket Wtn-022, 3,400 km from center" / "in ring void — nearest pocket
  Wtn-023, 190,000 km clockwise" (along-ring bearing so the player can creep
  along the ring sublight or hop) / "off ring" with radial+z offsets · Pyro →
  nearest registry field + miss distance.
- Capture annotation (`_halo_capture_note`) stamps pocket/field names, so
  tagged rocks and wrecks become re-targetable POIs with provenance.
- The HALO MAP canvas parameterizes: Stanton draws the 10-band annulus
  (unchanged); Nyx draws the 15 Gm ring + pocket dots (pinned pocket
  highlighted) — 381 dots at true scale is visually ideal, the ~4% coverage
  *is* the story; Pyro draws bodies + the chosen field. The inset works
  unchanged (chord + drop window + target).
- Navigator chip: `haloWhereChip` gains the Nyx/Pyro verdicts — "☄ Glaciem
  pocket Wtn-022" beats a bare deep-space readout.

### 3.6 API: one targets feed, system-aware plan/locate

- `GET /api/halo/targets?system=` — superset of today's `/api/halo/bands`:
  Stanton `{bands, bodies, attribution}` · Nyx `{ring, pockets[], bodies,
  attribution}` · Pyro `{fields[], bodies, attribution}`. Keep
  `GET /api/halo/bands` as a Stanton-shaped alias (deprecated, not removed).
- `POST /api/halo/plan` — `HaloPlanIn` gains `system` (default: resolved from
  the start fix), `pocket_key: str|None` (Nyx pin), `field_uuid: str|None`
  (Pyro); exactly one goal among `band` / `target_poi_id` / pocket-or-ring /
  `field_uuid`, validated per system. Input caps per house guardrails.
- `GET /api/halo/locate` — carries `system` resolution and the per-system
  verdict shapes above.

### 3.7 Frontend: system seg + two new target panels

`#/halo` keeps its layout; the target panel swaps by system seg:

1. **STANTON** — density strip, unchanged.
2. **NYX** — ring arc map (pocket dots; tap to pin, default AUTO "best
   pocket"), Wtn/Mission filter chip, "☄ Glaciem Ring — 4% of the ring holds
   the rocks; we aim at the pockets" one-liner (the insight is the product;
   say it).
3. **PYRO** — searchable field list grouped by shell, with the marker-holding
   clusters/RABs badged "direct jump" and ore-intel marked unknown.

Plan card, drop readout, alternates, after-the-drop panel: unchanged shapes.
Attribution line becomes per-system (§3.8).

### 3.8 Attribution

- Stanton: Cornerstone credit unchanged.
- Nyx: geometry is game data via the committed starmap dataset
  (`containers.json`) — credit the dataset source line already used for the
  navigator ("starmap data: starmap.space", per its existing attribution),
  not Cornerstone.
- Pyro: SC Wiki API (CC BY-SA 4.0) — already attributed app-wide for #28;
  the targets feed carries the same string.

## 4. What we deliberately do NOT build

- **Pyro VI / Pyro V ring planners** — the rings don't exist in-game.
- **Keeger Belt** — ~~not physicalized~~ **CORRECTION 2026-07-16: it IS
  implemented and lootable** (wiki live data: `HPP_Nyx_KeegerBelt` provider,
  ship mining ~10% incl. Aluminum, salvage 0.03–4%; the People's Service
  Stations ring the belt at exactly 48.000 Gm z=0, user-confirmed in-game;
  Keeger contracts spawn in-belt QT markers). What it lacks is **container
  geometry** — zero Keeger containers even in the current build's starmap
  feed (re-verified live), so there are no pocket centers to aim drops at.
  Successor plan: crowd-sourced survey marks + fitted clusters feed the
  pocket planner — backlog #36, [`belt-survey.md`](belt-survey.md).
- **Nyx band mode / density model** — one ring, no bands, no survey to encode.
- **Per-pocket/per-field ore intel** — no public data; deferred to a future
  enrichment (Regolith integration or our own org-sourced capture stats).
- **Cross-system starts** — same deferral as #31.

## 5. Build order

1. **nav_core registry + geometry**: `nav.belts` builder (`build_belt_registry`),
   `glaciem_pockets(nav)`, `glaciem_contains`, per-system `halo_locate`.
   Tests: registry counts/radii pinned against the committed feeds (381
   pockets, r∈[14.99, 15.01] Gm; 102 Pyro fields); locate verdicts; the
   §3.4 Stanton-at-15 Gm regression.
2. **Solver pocket mode** + per-system `plan_halo_drop` parameterization.
   Fixtures: self-derived from datamined coords (e.g. Pyro Gateway→Levski
   chord must yield a pocket hit with miss ≤ grid radius; internal-consistency
   sum identity as in #31) — flagged as *self-derived, pending in-game
   verification* (unlike Stanton there are no community-published numbers to
   check against; the in-game pass in step 6 is the real oracle).
3. **Session.system freshness stamp** + `_halo_fix_system` ladder update
   (fresh-sticky > belt-geometry > stale-sticky) + regression tests.
4. **app.py**: `/api/halo/targets`, `HaloPlanIn.system/pocket_key/field_uuid`,
   locate; TestClient tests (auth monkeypatch incl. `app.token_user`).
5. **index.html**: system seg, Nyx/Pyro target panels, per-system map, chip +
   capture-note copy.
6. **In-game verification pass** (the §7 unknowns): fly a planned Glaciem
   pocket drop, a Pyro L-point drop, and an Akiro/PYR1-L3 drop; record
   findings in this doc.
7. Docs: CLAUDE.md map, README index row, backlog #35 collapse, this header.

## 6. Effort shape

Step 2 is the only genuinely new solver code (~an outer loop + scoring);
steps 1/3/4 are parameterization and plumbing; step 5 is the bulk of the
line-count (two target panels + map variants). No new DB tables, no new
dependencies, no new sync tools — every byte of source data is already
committed in `poi/containers.json` and `poi/locations.json`.

## 7. Unknowns to verify in-game (before/at step 6)

- Do the 300 Wtn pockets spawn minable rocks without a contract? (Mission
  segments believed QV-Breaker-gated; Wtn believed free-roam. Unverified.)
- Does QT plotting *through* the ring plane get obstructed near pockets?
- Is the visual ring radially wider than the pocket containers (player
  perception of "I'm in the ring but no rocks")?
- Akiro/PYR1-L3: is there actually minable content at the L-point, or is the
  lore cluster elsewhere/unpopulated? (RAB-ALPHA 1.45 Gm away suggests the
  region is dressed.)
- Pyro RMB fields: minable rock density vs. FPS-loot-only dressing.

## 8. Research sources (2026-07-15 sweep)

- Local: `poi/containers.json` (381 `Glaciemring_Segment_*`, build 10679008,
  2025-11-12) · `poi/locations.json` (149 Pyro asteroid records, game
  4.8.2-LIVE.12030094).
- Galactapedia: Glaciem Ring (Nyx Belt Alpha) · Keeger Belt (Nyx Belt Beta) ·
  Akiro Cluster (Pyro Cluster Alpha) · Terminus (Pyro VI).
- SC Wiki API location records: glaciem-ring (ores/salvage %), akiro-cluster
  (no coords); positions feed (1,250 rows: 809 Stanton / 291 Pyro / 150 Nyx).
- scminer.rocks/data/ore-by-location/Glaciem Ring (per-ore abundance) ·
  scmdb.net (Pyro VI mineables).
- Alpha 4.4 Nyx launch coverage (massivelyop.com, neowin.net, 2025-11) ·
  QV Breaker mining-rights guides (theimpound.com, expcarry.com).
- cstone.space knowledge base — confirmed **no** Glaciem/Pyro survey exists
  (Aaron Halo survey only) · pitan.xyz — Stanton only. First-mover confirmed.
- starzen.space RAB loot guide · scuplift.com (cluster internal names =
  "encounter region" containers).
