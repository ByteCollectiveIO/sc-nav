# Halo Finder (backlog #31) — design plan

**Status: ✅ SHIPPED 2026-07-10 (built as designed, same day).** Build notes:
`nav_core` halo section (constants + geometry + `plan_halo_drop` solver, end of
file) · app.py `# --- halo finder (#31)` endpoint group · `#/halo` view + JS
module + launcher card · 19 nav_core tests (4 Cornerstone golden fixtures, all
within tolerance) + 8 app tests. Alternates ship as full drop+leg pairs so the
client promotes one without a re-plan. The §3.8 extras landed too: navigator
"☄ Halo band N" chip (client-side against the band feed) and automatic band
annotation on deep-space POI captures (`_halo_capture_note`).
**v0.51.1:** `chord_obstructed` endpoint rule — low-orbit stations (Baijini
Point) and surface outposts no longer veto every chord; drops plan from all
177 Stanton markers. **v0.52.0:** HALO MAP canvas in the plan card (true-scale
top-down system view + magnified drop-zone inset; browser-verified via the
headless-Chrome preview harness) + the sticky-session-system fix for
deep-space coordinate ambiguity (raw positions can't name their system —
every system centers on its own origin; the session's last container-confirmed
system now disambiguates captures, halo locate, and live-start plans).
**v0.52.1:** completes the deep-space-system fix for the belt itself. v0.52.0's
session-hint only helped when the client had a recent container-confirmed
system; a live fix taken *inside* the belt (jump in, `/showlocation`) had no
such context, so `system_at`'s raw nearest-container fallback still mixed the
per-system frames and named **Pyro/Nyx** — the plan then rejected it with
"travel to Stanton first," and locate reported the wrong system. New
`nav_core.halo_contains(pos)` (cylindrical radius within the band table + near
the ecliptic, `HALO_PLANE_TOLERANCE_M`) makes `system_at` resolve any in-belt
position to Stanton directly — the ring sits ~20 Gm out where no Pyro/Nyx body
lives, so it's an unambiguous landmark. Fixes the plan, locate, and (hint-less)
capture paths; the session hint still takes precedence, the envelope is the
smarter geometric fallback. +2 regression tests.
A tenth app:
plot quantum-travel drops into the **Aaron Halo**, Stanton's ring asteroid belt.
The player picks a density band (or one of their own custom POIs inside the
belt), and the app answers: *set destination X, jump, exit QT when the
distance-to-destination readout hits D* — plus staging legs when a direct chord
doesn't exist, and post-drop verification from the next `/showlocation` fix.

Everything below is implementable in `nav_core` + `app.py` + `index.html` with
**zero new dependencies and no new DB tables**. Read
`docs/snare-detour-routing.md` first for the hazard-volume/detour machinery this
reuses.

---

## 1. The problem (game facts)

The Aaron Halo is a circular asteroid belt around the whole Stanton system,
between the orbits of Crusader (~19.1 Gm) and ArcCorp (~28.9 Gm). It is the
richest and safest mining/salvage region in Stanton — and it has **no quantum
markers inside it**. You cannot QT *to* the belt; you QT *through* it on a
route between two ordinary markers and manually exit (hold B) partway.

The community technique (pioneered by CaptSheppard / Cornerstone,
cstone.space) is chart-based: plot a route whose straight line crosses the
belt, watch the HUD "distance to destination" readout during QT, and exit at a
published number to land in a chosen band. Cornerstone's 2022 survey (1,746
photo samples at 1,000 km intervals, AI-counted) mapped the belt precisely;
the charts have been circulated unchanged from game version 3.16.1 through 4.x
(CIG moved spawn data server-side in 4.0, so nobody datamines this — the
survey *is* the source of truth, and no post-4.0 re-measurement contradicts
it).

What the charts can't do — and we can, because we already have every marker's
true 3D position and the player's live position — is generalize: **any start
point** (including a live deep-space fix), **any band**, **any custom POI
inside the belt**, obstruction-aware, with per-drive precision hints.

## 2. Survey data (the band model)

Source: "Aaron Halo — Detailed Shape and Density Survey" + "Aaron Halo Travel
Routes", CaptSheppard / Cornerstone (game version 3.16.1-LIVE, 2022; republished
unchanged through 3.23+). **Datum: all distances are to the Stanton starmap
marker — the system-grid centerpoint, NOT the star.** This matters: in our
coordinate frame that marker is the **origin (0,0,0)**, and all Stanton planets
verifiably orbit the origin in the z=0 plane. The Stanton *star container* sits
offset at ≈ (0.136, 1.294, 2.923) Gm and must not be used as ring center.

Ten concentric bands, circular all 360°, identical across servers. Radii in km
from the origin:

| Band | Inner | Outer | Center | Densest pt | Width | Half-height |
|---|---|---|---|---|---|---|
| 1 | 19,673,000 | 19,715,000 | 19,694,000 | 19,702,000 | 43,000 | ~625 |
| 2 | 19,815,000 | 19,914,000 | 19,864,000 | 19,857,000 | 100,000 | ~2,070 |
| 3 | 19,914,000 | 20,071,000 | 19,993,000 | 19,995,000 | 157,000 | ~4,046 |
| 4 | 20,129,000 | 20,230,000 | 20,180,000 | 20,168,000 | 102,000 | ~2,912 |
| 5 | 20,230,000 | 20,407,000 | 20,319,000 | 20,320,000 | 177,000 | ~4,998 |
| 6 | 20,407,000 | 20,540,000 | 20,474,000 | 20,471,000 | 132,000 | ~5,000 |
| 7 | 20,540,000 | 20,750,000 | 20,645,000 | 20,662,000 | 211,000 | ~4,998 |
| 8 | 20,793,000 | 20,968,000 | 20,881,000 | 20,881,000 | 176,000 | ~3,487 |
| 9 | 21,046,000 | 21,132,000 | 21,089,000 | 21,082,000 | 87,000 | ~2,400 |
| 10 | 21,159,000 | 21,299,000 | 21,229,000 | 21,207,000 | 141,000 | ~2,008 |

Facts that shape the design:
- **Band 5 is the jackpot**: ~3× the peak density of any other band, with
  notably larger asteroids. Bands 2/3, 4/5, 5/6, 6/7 share borders; the rest
  are separated by voids (27,000–100,000 km of near-empty space).
- **The belt is razor-thin vertically**: ≤ ±5,000 km about the ecliptic (z=0)
  vs ~1.6M km of radial span. A route that crosses the right *radius* at high
  |z| passes over/under the rocks — which is exactly why routes from the three
  jump-point Gateway stations never touch the belt. The solver must check z at
  the crossing, not just radius.
- Density within a band is roughly bell-shaped around the densest point;
  bands 5 and 7 end abruptly at their vertical edges, the others fade.
- Visible-asteroid density ≠ mineable-spawn density (mineables are
  server-placed, loosely tracking density; groups of up to 16 rocks). We guide
  to geometry; we don't promise rocks.
- Validation identity: on a published chart route the two directions' drop
  numbers sum to the route length (e.g. ARC-L1↔CRU-L4: 12,744,803 + 11,256,961
  = 14,292,609 + 9,709,155 = 24,001,764 km) — a free correctness check, and
  the published numbers become **golden test fixtures** for our solver.

## 3. Design decisions (and the reasoning, so they don't get relitigated)

### 3.1 The band model ships as `nav_core` constants, not data files or settings

Ten rows of four numbers. Not an org setting (no admin should edit survey
data), not a `poi/*.json` feed (nothing to sync — there is no API; the survey
is a one-time community measurement), not a DB table. A `HALO_BANDS` constant
in `nav_core.py` with a source comment, plus `HALO_SYSTEM = "Stanton"`,
`HALO_HALF_HEIGHT_M` per band. If a future patch moves the belt, editing ten
lines is the right maintenance cost. Beware the starmap dataset's own POI
id=8000 "Aaron Halo - Band": it is a placeholder sitting at (0,0,0) — exclude
it from Halo Finder suggestions and never use it as geometry.

### 3.2 Guidance is pre-computed numbers, not live tracking

QT cruise is 53.6–283 Mm/s; the watcher heartbeat is 60 s and positions arrive
only when the player runs `/showlocation`. Live mid-jump tracking is
physically impossible with our pipeline — and unnecessary: the in-game HUD
already shows a live distance-to-destination readout. So the app does what the
community does, better: it emits the numbers to watch for, **before** the
jump. The live position is used at the *endpoints* — seeding the plan from
where you are, and verifying/refining after the drop (§3.8).

### 3.3 Primary guidance is a drop *window*, with the densest point as the bullseye

For a chosen chord, the band crossing is an interval, not a point: the readout
value when you **enter** the band and when you **exit** it. Exiting QT anywhere
inside the window puts you in the band — robust to reaction time and server
lag. We display `ENTER 14,391,000 → PEAK 14,292,609 → EXIT 14,214,000` style
guidance plus the window's duration in seconds at the player's quantum drive
speed (we have per-drive speeds from the #26/#27 quantum data — reuse the
drive picker). Reaction-time context from the survey: manual exits are
repeatable to ~±200 km at low speed; at full cruise a 200 ms reaction costs
10,000–57,000 km, vs band widths of 43,000–211,000 km. Bands are easy;
bullseyes want a slow drive or a shallow crossing.

Fallback always shown: the **system-wide method** — "or watch your distance to
the Stanton marker in the starmap and exit at `20,320,000 km`" — which works on
any belt-crossing route and is patch-proof. Our value-add over that method is
picking the best marker, single-readout guidance (no mobiGlas juggling),
obstruction handling, POI targeting, and verification.

### 3.4 Chord geometry: cylinder crossings, one new math helper

Bands are modeled as origin-centered cylindrical annuli (radius bounds from
the table, |z| ≤ half-height). For a leg from P0 to P1, crossings of radius r
solve a quadratic in t on the **xy components only**:
`|xy(P0) + t·xy(d)|² = r²`. Each root t ∈ (0,1) with |z(t)| ≤ h is a crossing;
drop distance = `|P1 − P(t)|`. (At ≤5,000 km of z over ~20M km of radius, the
3D readout and the cylindrical radius agree within ~1 km — we compute
cylindrically for correctness and emit 3D distances for the HUD.) This is the
only genuinely new geometry: `_ring_crossings(p0, p1, r) -> [t]`, ~20 lines,
plus band assembly around it. Everything else — segment-vs-sphere
(`_seg_point_dist`), hazard volumes, detour search (`_detour_via`), leg
decomposition — already exists from #24 v2.

A chord through the belt interior crosses each band **twice** (in and out
legs of the chord); a route from outside to a marker inside the belt's inner
edge (e.g. anything at Crusader's orbit) crosses once. We emit the first
crossing along the travel direction by default and list the second as an
alternate.

### 3.5 Choosing the destination marker: score, don't just pick nearest

From a start S and target band N, scan `nav.qt_markers` (Stanton: ~177–196,
each resolvable via `poi_global_m`; markers are targetable system-wide from
the starmap, so every one is a candidate). For each marker M whose chord S→M
crosses band N within z-height and t ∈ (0,1):

- **Reject** if the chord is obstructed (§3.6) or the drop point is too close
  to M (must exit well before auto-arrival; floor ~200,000 km) or the crossing
  is outside the segment.
- **Score** = weighted: window length in seconds at the player's drive speed
  (longer = more forgiving), total jump distance (shorter = less fuel/time),
  crossing steepness. Steepness is the tradeoff knob: a radial chord gives a
  short window but precise radius control (best for "densest point"); a
  grazing chord stretches the window (best for "just get me in the band").
  The band picker's aim toggle — `anywhere in band` vs `densest point` —
  flips the steepness weight.

Return the best plan plus 2–3 alternates (different markers), like the trade
planner's route list. All time-invariant: planets/stations don't move in SC,
so drop numbers can be computed once and flown later.

### 3.6 The sun (and planets) become hazard volumes — reusing #24 v2 wholesale

The game refuses QT routes obstructed by celestial bodies. We model the
Stanton star as a synthetic sphere volume `{kind:'sphere', a: star.pos,
r: body_radius × margin}` at its **real offset position** (it is NOT at
origin), and planets likewise (body_radius × margin; cheap to include even
though they rarely matter at belt scale), and feed them through the existing
`segment_hits` test. If every direct chord from S to a band-N crossing is
obstructed or geometrically impossible (start far off-plane, start occluded by
the sun), the planner emits a **staging leg**: pick the cheapest QT marker T
(existing `travel_cost` + `_detour_via` machinery, same ellipse-budget local
search) such that T→M has a clean band-N crossing — then the plan is
`S → T (normal QT leg)` + `T → M (drop leg)`, rendered exactly like trade/cargo
legs (`_leg_view` dict shape, so `detourVia`/`waypointSteps`/fuel chips render
for free). Margin factor is a constant (start 1.2×); wire an org setting only
if reality disagrees.

### 3.7 Custom-POI targeting: closest approach over (start, marker) pairs

Case 3 — "get me back to the wreck I tagged": the target is a fixed point
P★ (a deep-space custom POI; the capture flow already records `global_m`).
No chord will hit it exactly; the plan minimizes **miss distance** =
`_seg_point_dist(S, M, P★)` over candidate markers M; drop distance =
`|M − Pc|` at the closest-approach point Pc. When the user allows a staging
hop, optimize over (T, M) pairs — ~200×200 chords × one segment-point test
each is trivially cheap and materially shrinks the miss, because you can
choose a start that lines up with P★ and a marker beyond it. The plan card
reports the expected miss honestly ("drops you ~9,400 km from POI 'Big Q
Rock'") — the in-game compass is useless in deep space, so getting within
visual/radar range *is* the product. No band constraint applies in POI mode
(the POI's own radius/z is whatever it is).

**Prerequisite bug fix**: `_frame_at` (nav_core ~851) stamps deep-space
captures `system="Unknown"` because `detect_container` finds no container 20 Gm
out; that makes `travel_cost` treat them as cross-system and unroutable —
breaking exactly the POIs this app is about. Fall back to `system_at()` (which
resolves "Stanton" by nearest container) — a one-line fix plus test, shipped
first.

### 3.8 Verify-and-refine closes the loop (this is the killer feature)

After the drop, the player runs `/showlocation`; the watcher posts the fix and
the app immediately classifies it: radial distance → band lookup + z-height →
**"You're in band 5, 12,400 km inside, 800 km below plane"** (or "in the
3→4 void, 22,000 km short of band 4"). In POI mode it also shows the actual
miss and offers **Refine**: re-run the same planner from the new fix, which
converges over 1–2 hops as geometry improves. The classifier (`halo_locate`)
is pure nav_core math on an existing position — also surfaced as a passive
chip in the Navigator when any live fix lands inside the belt annulus, and on
the capture flow so tagging a rock immediately records its band.

### 3.9 Stanton-only v1

The Aaron Halo is a Stanton feature. If the caller's start resolves to
Pyro/Nyx, return a friendly "travel to Stanton first" error rather than
auto-planning gate legs — cross-system staging triples the solver surface for
a case nobody starts from. The existing gate chain makes it a natural v2 if
asked for. **Note (v0.52.1):** this guard is only meant to catch a *genuine*
cross-system start. A live fix taken inside the belt must resolve to Stanton,
not trip this error — see the `halo_contains` note at the top; the guard now
only fires for real Pyro/Nyx starts.

### 3.10 Attribution: credit Cornerstone visibly

The band table is CaptSheppard/Cornerstone's measured data. No license is
stated on cstone.space (unlike the wiki's CC BY-SA), and raw measurements are
facts — but the courteous and safe course is a visible in-app credit line on
the Halo Finder view ("Band survey: CaptSheppard / Cornerstone — cstone.space")
with a link, mirroring the wiki attribution precedent in the footer, plus a
source comment on the constant. If we ever embed their chart imagery (we
don't plan to — we draw our own density strip), ask permission first.

## 4. API design

New endpoint group in `app.py` (`# --- halo finder (#31) ---`):

- `GET /api/halo/bands` — the band table + attribution string, for the picker
  strip. Static, cacheable.
- `POST /api/halo/plan` — `HaloPlanIn`:
  `band: int|None (1–10)` XOR `target_poi_id: int|None` (custom POI in belt) ·
  `start_poi_id: int|None` (default: caller's live session position via
  `position_start`, 404 if neither) · `aim: "band"|"peak"` (band mode) ·
  `allow_staging: bool = True` · `ship`/`qd` (optional, for window-seconds +
  fuel, resolved like the other planners) · `avoid_poi_ids` (shared personal
  blacklist). Returns `{legs: [...], drop: {marker_id, marker_name,
  enter_m, peak_m, exit_m, window_s, star_dist_peak_m, crossing_xyz,
  steep_deg, expected_miss_m (POI mode), second_crossing: {...}|None},
  alternates: [...], band: {...}, attribution}`.
- `GET /api/halo/locate` — classify the caller's latest fix (band / void /
  outside; radial + z offsets; distance+bearing to `target_poi_id` if given).

Input caps per house guardrails; solver runs through the same
`asyncio.to_thread` pattern as trade replan if profiling says it needs it
(expected not: ~200 chords × a quadratic is microseconds).

## 5. Frontend (`#/halo`, "Halo Finder")

Tenth launcher card + `#halo-view` + router wiring per house pattern
(`applyView`, `APP_LABEL`/`APP_TITLE`, back-toggle). One `// ---------- halo
finder (#31) ----------` JS section. Layout mirrors the trade planner:

1. **Target panel**: band picker drawn as a horizontal density strip — 10
   bars, width ∝ band width, height/tint ∝ peak density (band 5 visibly
   dominant), radii labeled; tap to select. Toggle `☄ band` / `📍 my POI`
   (POI mode = `attachPoiPicker` filtered to deep-space customs). Aim toggle
   `anywhere in band | densest point`. Start = live position (default, shows
   freshness) or station via POI picker. Ship/drive reuses the planners' SHIP
   panel pattern.
2. **Plan card**: leg list (staging legs use existing leg renderer) ending in
   the **DROP leg** — a large monospace readout block:
   `Set destination CRU-L4 → jump → EXIT at 14,292,609 km`
   with the enter/exit window, window-seconds for the selected drive, the
   star-marker fallback number, steepness, fuel chips, and alternates as
   compact rows (tap to promote). Copy button for the drop number.
3. **After-the-drop panel**: live `halo/locate` result on every position fix
   (WS-driven like the navigator), band verdict + miss distance + **Refine**
   button (POI mode) + **Tag this spot** (existing capture flow, band
   annotated into the note).
4. Attribution line, small, bottom of view.

No new WS frames; position fixes already broadcast state.

## 6. Build order

1. **`_frame_at` Unknown-system fix** (nav_core) + regression test — ships
   independently; also fixes deep-space captures everywhere else.
2. **nav_core geometry**: `HALO_BANDS`, `_ring_crossings`, `halo_chord(p0, p1,
   band)` (crossings + windows + drop distances + steepness), `halo_locate
   (pos)`. Golden tests against published Cornerstone chart values (ARC-L1↔
   CRU-L4 per-band numbers, sum-identity, CRU-L1 inside-out routes) using real
   dataset marker positions. **Premise already validated ad-hoc (2026-07-10)**
   against the live dataset: our ARC-L1→CRU-L4 chord = 24,003,285 km vs
   Cornerstone's 24,001,764 (+0.006%); our computed band-5-peak drop =
   14,290,792 km vs published 14,292,609 (−1,817 km = 0.013% of route, ~1% of
   band-5 width); z at the crossing = 453 km, well inside the ±4,998 km
   half-height. Set golden-test tolerance ≈ 5,000 km. Z-height test:
   Gateway-station chords produce no crossings.
3. **nav_core solver**: `plan_halo_drop(nav, start, band=|target=, aim=,
   markers=, avoid_volumes=, staging=)` — candidate scan, scoring, staging
   fallback, POI closest-approach mode, `_leg_view`-shaped output. Star/planet
   volume builder app-side (`_build_body_volumes`).
4. **app.py**: the three endpoints + `HaloPlanIn` + tests (TestClient, auth
   monkeypatch incl. `app.token_user`).
5. **index.html**: view + router + launcher card + density strip + plan card.
6. **Verify/refine panel** + navigator in-belt chip + capture band annotation.
7. Docs: CLAUDE.md map (view, endpoint group, JS banner), README.md index row
   flip to shipped, backlog #31 collapse, this doc's status header.

## 7. Deferred (v2+, deliberately)

- **Ore intelligence**: Regolith Survey Corps (regolith.rocks) treats
  AARON_HALO as a first-class gravity well with crowd-sourced, current-patch
  ore/rock-class data — a candidate enrichment feed for "which band for
  quantainium", if they expose an API and license.
- **Cross-system starts** (gate-chain staging), **automated multi-hop homing**
  (auto-chain refine jumps), **wiki `obstruction_m`** per-body radii (already
  synced by `tools/sync_locations.py`, currently unused) replacing the flat
  margin factor, **band re-survey tooling** if a patch ever moves the belt
  (the survey method is fully documented and reproducible with our watcher).
- **Danger overlay**: halo drops are famously pirate-free (off all traffic
  lanes), but the Danger Board hazard volumes already thread through the
  solver — exposing `avoid_mode` here is nearly free if it ever matters.

## 8. Research sources

- Cornerstone knowledge base: "Aaron Halo Travel Routes" (article 36) +
  "Detailed Shape and Density Survey" (article 65), CaptSheppard —
  cstone.space; density chart `Aaron_Halo_Density_Chart3.png` (3.16.1); route
  charts 3.19.1-LIVE Rel.1 (PDF mirrored on cdn.star-citizen.wiki).
- Prior art: SnarePlan (snareplan.dolus.eu — straight-line QT-route
  interception math, validates the chord model), pitan.xyz Stanton navigator
  (trilateration + `/showlocation` paste + band table), StarMap
  (starmap.space — clipboard `/showlocation` tracking, our watcher's cousin),
  Regolith (post-4.0 ore surveys), NOVA Intergalactic (outdated 6-band model).
- Game facts: QT rebalance 3.24.2 patch notes (drive speeds/fuel; doesn't
  change distance geometry); RedMonsterGaming on 4.0 server-side spawn data;
  Galactapedia "Aaron Halo (Stanton Belt Alpha)".
