# Surface survey zones — named mining areas on planets and moons (#37.1) — design plan

**Status: SLICE 1 BUILT (backend), rest DESIGN.** The doc `survey-platform.md`
§7.2 promised this before build.

> **Built so far — backend foundation, nothing user-visible.** Storage
> (`body`/`center_lat`/`center_lon`/`radius_m` + the reserved
> `observations.game_build`), the `list_survey_zones` kind filter and the
> scoped admin clear (both §7/§9 regressions, fixed before a surface row can
> exist), the membership predicate with its 10 km altitude ceiling and
> `resource`-only filter, `surface_zone_fit`, `surface_zones_state`,
> `surface_cap_area_m2`, rasterized `body_coverage`, create/PATCH/GET with the
> body-prefixed slug that survives a rename, and the drop-planner reject that
> names the category instead of blaming the evidence. 38 tests.
>
> **Slice 2 — the two features, also built.** The **value basis** is a fifth
> basis `"surface"`: band weight (linear, pivot 4) x expected ore price,
> where each ore's weight is its **Wilson lower bound**, not its raw share —
> so sample size is discounted in the same step and by the same statistic
> `resource_hotspots` ranks the anonymous grid with (3/3 Quantanium scores
> 438 where 20/20 scores 839). Unpriced ores fall back to the category
> median and say so via `priced: false`. Terciles are cut across a
> **surface-only per-system pool**, so `$$$` means "best surface area we know
> in Stanton" and the belt pool is provably untouched. The **stats adapter**
> emits two streams — per-(zone, observation) pairs for the per-zone rollups,
> de-duplicated by observation id for member ranks, org totals and sessions —
> and `derive_surface_survey_stats` keeps surface activity in its own block
> beside the belt totals rather than pooled into them (user's call). Unique
> rows carry `zone_id: None` on purpose: crossing an invisible circle is not
> the start of a new mining session. `/api/intel/surveying` gains a
> `surface` block with per-body **absolute mapped area**. 17 more tests.
>
> **Not built:** every UI surface (§5, §6, §11.1) and the `kind="survey"`
> goal (§11.2, release two). `GET /api/halo/survey/zones` still defaults to
> `kind="deep"` so a deployed SPA sees exactly what it saw before; the UI
> slice flips that default. **Reviewed in four passes.** §12 and §13 are review output (gaps,
cross-tool hooks). §14 is the **audit trail** of an adversarial pass that
checked every claim against the code: each correction it found has since been
folded into the section it affects, and a fourth pass re-verified §14 itself and
fixed three of its own imprecisions. **So §1–§13 now read correctly
top-to-bottom; §14 is kept to stop the mistakes being re-introduced, not as a
required pre-read.** Companions:
[survey-zones.md](survey-zones.md) (#36.1 — the deep-space zone this extends),
[belt-survey.md](belt-survey.md) (#36),
[survey-platform.md](survey-platform.md) (#37 — this is slice 5's first half).

The user's ask: "Resource Navigator builds stats on areas as you log resource
nodes *as a whole*, but there is no way to declare an actual survey zone like in
Prospector. It would be nice to define survey zones for planetary and
moon-based ore nodes so that detail cards could be built for those areas in the
ATLAS."

Decisions locked with the user before this doc:
- **Membership is geometric and retroactive**, not tagged at capture time
  (unlike #36.1). Every observation already carries `latitude`/`longitude`, so a
  circle drawn today inherits every node the org ever logged inside it.
- **One zone concept, not two.** Same `survey_zones` table + a nullable surface
  anchor, so ATLAS stays one list and one detail card.
- **Scale: 50 km.** "It's not uncommon to range that far flying around looking
  for good ore. Outside of that people go back up to space and pick another spot
  — which would be a new survey zone." 50 km is the cap, and it is also the
  natural *semantic* boundary: one zone = the ground you can sweep in one
  landing. **That is a statement about EXTENT, never about visits** — a zone is
  a durable place that accumulates evidence forever, from anyone, across any
  number of trips (§2.1).
- **Navigable, not plannable.** A surface zone is never a quantum drop target,
  but selecting one must offer **plot a route to its nearest QT marker**.
- **Mining *is* surveying (§11).** Nodes logged by a member who doesn't know
  the zone exists contribute automatically, and an org leader can declare a
  **priority zone** that fills itself as people mine normally. This falls out
  of geometric membership for free; what it needs built is feedback, a
  `kind="survey"` goal, and a `since` baseline so a priority zone isn't born
  complete.

---

## 1. Why the existing zone doesn't already do this

#36.1 made a zone "a named tag on marks, geometry always derived." That part
generalizes perfectly. Three things underneath it do not:

1. **Wrong evidence type.** Belt zones group **survey marks** — durable
   `custom_pois` with `type="survey"`, absolute `global_m` xyz, and first-class
   *negative* evidence (`rocks="none"` bounds a field's edge in
   `survey_cluster_fit`, `nav_core.py:7634`). Planetary ore nodes are
   **observations**: an append-only log of ephemeral things that respawn,
   anchored in the body's rotating frame (`local_km` + lat/lon), with no way to
   record an absence. Different table, different id space, different meaning.
2. **Wrong scale.** `survey_cluster_fit` floors every zone at
   `GLACIEM_POCKET_RADIUS_M` ≈ 5,196 km (`nav_core.py:7637`). A 50 km mining
   patch would report as ~10,400 km across.
3. **Wrong geometry.** The belt fitter is a 3-D centroid in a non-rotating
   frame. Two points on Daymar 50 km apart are a great-circle arc on a rotating
   sphere; their xyz distance is correct only at one instant, and their xyz
   *positions* change every second.

Everything else — identity-only rows, derived-never-stored, org-shared
ownership, `closed`, slugs, the ATLAS table shell, the detail card shell —
carries over. **Two things that look like they carry over do NOT, and they are
the two biggest items of real work in this build: the value tier (§4) and the
activity stats (§8). Neither is a parameter change; budget both as features.**

## 2. Model: a zone is an identity plus a circle on a body

A surface zone owns **its identity and its boundary**, and nothing else. Unlike
a belt zone (whose boundary is derived from its marks) a surface zone's circle
is **declared**, because there is no negative evidence to bound it with — a body
with no sighting at a spot proves nothing. Everything *inside* the circle stays
derived: ore mix, band distribution, sample count, value, contributors,
freshness.

- **Anchor** = `(system, body, center_lat, center_lon, radius_m)`.
- **Membership** = `great_circle(o.lat, o.lon, z.lat, z.lon, body_radius) <=
  z.radius_m`, over the body's **`resource`** observations only, plus an
  altitude ceiling (§9 — orbital fixes carry a valid ground track). Pure
  function of the observation row: no column on `observations`, no backfill, no
  capture change. `observations` also holds `wildlife` and `harvestable`, so the
  category filter is load-bearing, not tidiness — without it a fauna sighting
  joins a mining rollup. `nav_core._obs_on_body(nav, system, body, category)`
  (`nav_core.py:1798`) already does exactly this bucket lookup, defaults to
  `category="resource"`, and guards both `latitude` and `longitude`; use it
  rather than writing a new one.
- **Shape is a circle, not a box.** A box needs lat/lon bounds that wrap badly
  at the antimeridian and distort into a non-box on a sphere; `great_circle`
  (`nav_core.py:773`) is one primitive the codebase already has and the only
  one a 50 km radius needs.
- **Zones may overlap**, and a node inside two zones counts in both — see
  §2.2 for the full rule and the double-counting traps it creates.

### 2.1 A zone is a place, not a visit

Worth stating because "one landing" reads as a time limit and isn't one. A zone
has **no visit lifecycle**. Come back tomorrow, next week or next patch: every
node logged inside the circle joins it, from any member, forever, with no
re-arming and nothing to close. That is §11's passive contribution seen from
the zone's side — accumulation is the default, not an opt-in.

**But what accumulates is SIGHTINGS, not distinct rocks.** Nodes respawn, so
mining the same outcrop on five trips files five observation rows. For *"how
much evidence do we have"* that is exactly right. For *"how rich is this
place"* it is a mild bias: re-logging a favourite spot is not five independent
samples, and while §8.1's Wilson bound absorbs some of it (`n` grows too), the
ore **composition** is per-sighting and drifts toward whatever gets re-logged
most.

Do **not** try to de-duplicate — nothing in the data distinguishes a respawn
from a re-sighting, and guessing would throw away real evidence. Say it in the
card's honesty footer instead, alongside the §5 line.

### 2.2 Overlap: both zones own the node

Membership is a pure per-zone predicate with no tie-breaking. A node inside two
circles is a member of **both**: both cards count it, both ore mixes include
it, both surveyor lists credit the member.

The alternative — assign each node to exactly one zone — needs an arbitrary
rule (nearest center? smallest zone? oldest?), and that rule would silently
**rewrite history** the moment anyone PATCHes a radius, moving nodes between
zones and changing both cards retroactively. Independence keeps each zone's
answer a function of its own circle and nothing else.

**This is where the two zone kinds diverge, and they share a table.** A belt
zone is *exclusive* — `zone_id` is one integer, and `survey_pockets` skips
zone-tagged marks (`nav_core.py:7683`) precisely so nothing is double-counted.
Surface zones deliberately break that, so **anything that sums across zones
will over-count**:

- **The §14.2 stats adapter** is the sharpest edge. It synthesizes mark-shaped
  dicts per zone, so a node in two zones is emitted twice and inflates the
  org's totals and that member's rank. **It must de-duplicate by observation id
  before tallying** — per-zone rollups double-count by design, org totals must
  not.
- **Coverage** must union, not sum — and §14.8's answer is to report absolute
  km² via grid-cell rasterization rather than attempt an analytic cap union.
- **Two overlapping priority goals**: one node advances both. Deliberate and
  useful (two leaders can care about overlapping ground), but state it rather
  than discover it.

Guardrails: §12.3 warns at create time on substantial overlap — warn, never
block, since a tight zone inside a broad one is legitimate — and §14.12 flags
the degenerate identical-center case, which gets no dedup at all today.

### 2.3 Scale constants (`nav_core`)

```
SURFACE_ZONE_RADIUS_MAX_M     = 50_000.0   # user's call: one landing's RANGE
SURFACE_ZONE_RADIUS_DEFAULT_M = 25_000.0   # "claim here" without thinking
SURFACE_ZONE_RADIUS_MIN_M     =    500.0   # below this it's a node, not an area
```

Sanity check against real bodies (`poi/containers.json` `BodyRadius`): Daymar
r=295 km → 1,854 km circumference, so a 50 km-radius zone spans 5.4% of the
plate — legible at a glance with no zoom. Yela 313 km, Cellin 260 km, Wala 283
km, Lyria 223 km all behave the same. Hurston (1,000 km) and ArcCorp (800 km)
need a zoom step; Crusader (7,450 km) is a gas giant nobody surface-mines.
**The moons that matter are exactly the ones this scale reads well on.**

### 2.4 Storage

Additive to the existing table, all via `_ensure_column` — a NULL `body` is
today's deep-space zone, untouched:

```
survey_zones += body TEXT            -- container name; NULL = deep-space zone
                center_lat REAL
                center_lon REAL
                radius_m REAL
```

`kind` is **derived**, not stored: `"surface" if body else "zone"`. One less
column that can disagree with itself.

**Slug collision:** the existing constraint is `UNIQUE (system, slug)`
(`db.py:532`), so "Iron Ridge" on Daymar would block "Iron Ridge" on Yela.
SQLite cannot alter a constraint without rebuilding the table, so instead
**surface slugs carry the body**: `_zone_slug` gains a prefix →
`daymar-iron-ridge`. No migration, no rebuild, and the slug still reads as a
human key in the export.

**Two traps the prefix walks into, both must be fixed with it:**

1. **PATCH drops the prefix.** `patch_survey_zone` re-slugs from the request
   payload alone — `slug = _zone_slug(body.name)` (`app.py:7194`) — and never
   reads the stored row's body (`ZonePatchIn`, `app.py:7126`, carries only
   `name` and `closed`). So renaming "Iron Ridge" → "Iron Ridge North" rewrites
   the slug to `iron-ridge-north`, losing the prefix, re-opening the collision
   and silently changing the zone's pin key. **Re-slug from the stored row, not
   the payload.**
2. **The prefix eats the truncation budget.** `_zone_slug` truncates at 40
   chars (`app.py:7088`) while `ZoneIn.name` allows 48 (`app.py:7121`). A
   `microtech-` prefix spends 10 of those 40, so two long names on one body
   truncate into each other and 409 with a message blaming the *name*. Either
   lengthen the truncation or validate the composed slug and say so plainly.

**Naming collision to avoid in the code itself:** these handlers already bind
the FastAPI request payload to a parameter named `body` (`app.py:7194` is
`body.name`). The celestial `body` this doc adds is a different thing with the
same spelling, in the same functions. Name the new one `body_name` or
`container` in the handler signatures; a mix-up here is the kind that type
checks and still ships.

## 3. The workflow (what the pilot does)

1. You're on Daymar, logging nodes as you always have. Nothing about the
   capture flow changes.
2. The navigator's capture column gains **⛏ Name this area** — it claims a
   circle at your live fix, default 25 km, radius adjustable (1–50 km), and
   nothing else. **Creation lives in the navigator, not Prospector**, because
   that is where you are standing when you find the ore.
3. The zone is instantly populated: every node the org has *ever* logged inside
   that circle is already a member. The confirmation reads
   `Iron Ridge — 47 sightings, 6 surveyors, Quantainium · Taranite · Iron`.
4. It appears in the Prospector ATLAS immediately, in the same table as the
   deep-space zones, with a **BODY** column and a full detail card (§5).
5. Selecting it offers **▸ Details**, **Set destination** (nearest QT marker,
   §4) and the usual rename / archive / delete lifecycle.
6. Alternative entry: ATLAS can **promote a hotspot** — `resource_hotspots`
   (`nav_core.py:2519`) already ranks equal-area grid cells by Wilson lower
   bound, so "name this cell" turns knowledge the org already earned into a
   named zone in one tap. Same endpoint, center = cell center, radius = default.

## 4. Navigable, never plannable

A surface zone must stay out of two pools it would otherwise fall into, because
`survey_zones_state` deliberately emits `#35`-shaped pocket dicts so
`plan_halo_drop` consumes them untouched (`nav_core.py:7877`):

- **The drop planner — the reject already exists and says the wrong thing.**
  There is no quantum drop onto a moon's surface, but this is not a new guard
  to add: `_halo_goal_system` matches `pocket_key` against a bare
  `db.list_survey_zones()` — **every system, no body filter**
  (`app.py:6757-6758`) — and happily returns the surface zone's system. The
  request then dies further along at `app.py:6851-6852` with *"that zone has no
  survey marks yet — drop one with ⛏ first"*, which is both confusing and
  wrong: the zone isn't unfinished, it's the wrong kind. Two existing sites need
  a surface branch, and the cross-system ambiguity 400 (`app.py:6759-6763`) now
  also fires on a surface/deep-space slug twin. Aim for the KGR arc pin's
  tone (#36 phase 1) — explain the category, don't report a shortfall.
- **The value tercile pool** (`_survey_valued`, `app.py:7098`). A `$$$` chip
  means "best in this belt", so tiering a moon patch against a belt pocket
  would silently redefine it — surface zones need their **own per-system
  surface pool**. **But splitting the pool is the easy half, and on its own it
  ships blank chips.** There is no score to tier: `_survey_value_from_index`
  (`nav_core.py:7950`) computes `SURVEY_DENSITY_W[density] × price` across three
  bases in priority order — `scanned` (mean composition × ore sell price,
  `nav_core.py:7963`), `ores` (mean sell of the listed ores, `:7979`) and
  `density` (weight × median sell, `:7989`) — and bails at `:7953` with
  `if positives <= 0:`, returning `None` unless the mark carried *salvage*, the
  one escape hatch (`{"basis": "salvage"}`). An observation has ore, band and
  quality: no `positives`, no `rocks`, no `density`, no salvage. Every row
  falls through unscored, so **every value chip in §5, §6.1 and §13.1 renders
  blank.** What surface zones need is a **fifth basis** — band-weighted ×
  composition × price — with its own score definition, its own terciles and its
  own honesty label. Budget it as a feature, not a parameter.

What they gain instead: **`nearest_qt` / `nearest_qt_id` / `nearest_qt_dist_m`**
on the zone row, computed from the zone center by the **two-step recipe**
`resource_hotspots` uses — there is no single helper that takes a lat/lon.
`nearest_qt_marker` (`nav_core.py:1542`) wants an *entity* with
`.system`/`.container_name`/`.local_km`, and it yields only a name and a
distance; the **id** comes from a separate name+system linear scan over
`nav.qt_markers`. So: synthesize a throwaway `Poi` at the zone center via
`local_km_from_latlon`, call `nearest_qt_marker`, then scan (§8.1). ATLAS
renders a **Set destination** button
→ `setDestination(z.nearest_qt_id)` (`index.html:6548`) → the navigator takes
over with live bearing/distance/ETA. This is the existing element-finder
`JUMP TO (QT)` affordance, applied to an area instead of a node.

### 4.1 Two-stage navigation — the QT marker is only half the trip

`POST /api/destination` resolves `poi_id` against **`nav.pois` *or*
`nav.observations`** (`app.py:3821`) — an observation is already a first-class
destination. That matters, because a zone's nearest QT marker can be tens of km
from the zone itself, and the last leg is exactly the part a newcomer to the
area can't eyeball.

So the zone card offers two targets, not one:

1. **Jump to** `nearest_qt` — the QT marker (a POI). Gets you to the
   neighbourhood.
2. **Fly to** the zone's best recent evidence — *set destination to a member
   observation*: freshest, unworked (`data.mined_at` unset), highest band. Zero
   new machinery; it's an observation id the endpoint already accepts.

This is strictly better than minting a custom POI at the zone center, which
would pollute the POI set, the search index and NEARBY with one synthetic
marker per zone. The doc deliberately does **not** add a coordinate-destination
mode to `/api/destination` for the same reason — the evidence is a better
target than the centroid anyway, because it's somewhere ore was actually seen.

## 5. What the detail card says (and what it must not claim)

The belt card's verdict is "the field is this big, this dense." A surface zone
**cannot** say that: no negatives, ephemeral targets, freshness windows. It
answers a different and more useful question — *"is this area worth landing
in again?"*

Reuse `renderHaloSurveyCard` (`index.html:17001`) with a surface branch:

- **Verdict line** — `<top ore> country · N sightings from M surveyors` with the
  value chip, or `THIN — 3 sightings, nothing above band 2`.
- **Stat tiles** — Sightings · Surveyors · Distinct ores · Value · Area ⌀ ·
  Freshest (age). (Belt tiles Rock hits / Scans / Field ⌀ are swapped out.)
- **Ore composition chart** — reuse `surveyOreChart`, but mind its contract, or
  it renders nothing or nonsense:
  - It **gates on an empty `z.ore_counts`** (`index.html:16940-16945`) and falls
    back to "No ore reported yet". A composition dict alone is not enough — the
    surface row must carry a parallel `ore_counts`.
  - It prints composition values **literally**, with no ×100
    (`index.html:16952`). That is correct and matches the server, which already
    treats `scan_comp` as being in **percent** units (`nav_core.py:7971` divides
    by 100 on the way in). So the conversion belongs on the producing side:
    emit percent, not the 0..1 probabilities `_shrunk_composition` returns.
    Feeding it 0..1 ships bars labeled "0.42%".
  - **Which statistic**: use the Wilson lower bound for the zone's *likelihood*
    numbers, per §8.1 — one statistical model shared with the hotspot grid, and
    it already solves "three lucky Quantainium hits read as 80%".
    `_shrunk_composition` (`nav_core.py:1824`) stays where it was written for,
    the forecast's own-cell-plus-ring smoothing. One composition, one model.
- **Band distribution** — a small histogram of scan bands 1–8. This is the
  planetary analogue of the belt's density read, and it is the number that
  actually decides whether to land: band 7–8 hits mean the area is worth the trip.
- **Sighting timeline** — reuse the mark timeline: age · handle · ore · band ·
  biome, newest first, honouring the `mined_at` depletion flag.
- **Honesty footer** — the sibling of the belt card's "strong evidence, not
  proof": *"Nodes respawn. This is where the org has found ore before, not
  where ore is now."* Carry §2.1's sightings-not-rocks caveat here too.

**What the shared card must suppress.** `renderHaloSurveyCard` is built for belt
zones and will otherwise ship dead or wrong controls on a surface card:

- `surveyRsCard(z)` is rendered **unconditionally** (`index.html:17063`). RS
  signatures are a mark-scan concept with no observation analogue — suppress it
  or the surface card carries a permanently empty panel.
- **Set active** renders for any non-closed zone (`index.html:17054-17055`).
  There is no capture-time tagging to feed on the surface side (§7), so the
  button is meaningless here and must be suppressed explicitly.
- **Plan a drop here** is already gated on `z.marks` (`index.html:17053`), so it
  self-suppresses *provided the surface row never populates `marks`*. Keep
  sightings in their own field and this one is free — but it is free by
  accident, so pin it with a test rather than trusting it.

## 6. Display: three levels, each honest at its own scale

The user's question: *the belt and space-based surveys display on the system
map, which is nice because you get a visual sense of where people have
surveyed. How do we represent a surface zone?*

The problem is pure scale. The ATLAS coverage map is system-scale
(`drawHaloSystemMap`, `index.html:17743`); every surface zone on Daymar sits at
Daymar's position, so a dozen of them collapse into one pixel. Zooming doesn't
help — at 50 km the moon itself is still sub-pixel until you're past any
meaningful system view. **So don't put surface zones on the system map. Put a
badge on the body, and give the body its own plate.**

### 6.1 Level 1 — system map: the body carries the badge

Unchanged map, one new layer. A body with surface zones renders a tinted ring
plus a count (`⛏ 3`), tinted by its best zone's value tier — exactly the
at-a-glance property the user likes about the current map, just aggregated to
the only scale where it can be read. Clicking the ring drills to level 2. Deep
space zones keep their own squares as today.

### 6.2 Level 2 — body plate: a new, equal-area top-down of one moon

A full-body map, entered by clicking a body's ring or any surface zone's row,
exited by a back chip or zooming out. Precedent for making it a mode of the
same canvas rather than a second widget: the Glaciem closeup became a zoom
preset so the wheel can always return (`dab23bf`).

**Projection: Lambert cylindrical equal-area** — `x ∝ lon`, `y ∝ sin(lat)`.
Three reasons, and they're the whole argument:

1. **The org's existing heat cells tile it exactly.** `nav_core.grid_cell`
   (`nav_core.py:1766`) *already* bins observations in Lambert equal-area. On
   this plate a cell is a rectangle with no resampling, so the #37 ore heatmap
   renders underneath the zones **for free** — the body plate ships with a data
   layer on day one, before anyone draws a single zone.
2. **Area measurement stays honest.** Any coverage figure must not be inflated
   by polar stretch, and plate carrée would lie here. But report **absolute
   area, not a percentage of the body** — see §8's `body_coverage`: a maximum
   50 km zone is ~7,840 km² against Daymar's ~1,093,600 km², i.e. **0.72%**, so
   a percentage reads as a rounding error forever and invites the innumerate
   "12% of Daymar" the first draft of this doc contained. (§2.3's "5.4% of the
   plate" is a *circumference* fraction about on-screen legibility — a different
   quantity that must never be read as an area share.)
3. It is one formula, no dependency, no build step.

The honest cost: a 50 km circle is a slightly flattened ellipse near the poles.
So draw a zone by **projecting its sampled great-circle boundary** (a ~48-point
polygon), never by stroking a circle primitive. That is also what makes a zone
crossing the antimeridian render correctly.

Layers, bottom to top: ore heat cells (existing `/api/resource_cells`) · the
body's POIs and QT markers as anchors · zone polygons, value-tinted, labeled ·
the selected zone highlighted · your live position if you're on that body.
Click hit-testing reuses `haloOverviewHits`.

### 6.3 Level 3 — zone plate inside the detail card: local, north-up, familiar

Inside the card, a small north-up plate centered on the zone, drawn with the
**existing** `projectMeters` used by the navigator map (`index.html:6260`) —
local ENU in meters. At 50 km on a 295 km body that's ~9.7° of arc, so
tangent-plane distortion is ~1.5%: negligible, and worth it because this is the
exact visual language pilots already read every session. Shows member sightings
as dots (ore-colored, faded by age), the zone boundary, the nearest QT marker
with a line to it, and your live position.

**Net:** level 1 answers *where has the org surveyed*, level 2 answers *where on
this moon*, level 3 answers *where in this area*. Only level 2 is new canvas
work; levels 1 and 3 are layers on maps that already exist.

## 7. API (all additive)

- `POST /api/halo/survey/zones` — `ZoneIn` gains `body`, `center_lat`,
  `center_lon`, `radius_m` (validated to §2.3, `body` resolved against
  `nav.containers` with an `is_body` guard). Supplying `body` makes it a surface
  zone; omitting it leaves the **write** path byte-for-byte unchanged — but not
  the read paths, see the bullet below. Creating a surface zone does **not** set
  it active (there is no capture-time tagging to feed).
  **Do not widen the `nav.belts` gate.** An earlier draft called for widening it
  to "belt system OR resolvable body"; that is a no-op. `build_belt_registry`
  returns exactly `{Stanton, Nyx, Pyro}` (`nav_core.py:7372-7385`) and every
  body in `poi/containers.json` lives in one of the three, so the gate never
  rejects a body this feature can reach. Keep the `is_body` guard (§9); drop
  the widen and the test that was written for it.
- **`db.list_survey_zones` needs a `body IS NULL` filter — before anything
  else lands.** It is `SELECT * FROM survey_zones WHERE system=?` with no body
  filter (`db.py:995`), and `_survey_zones_view` (`app.py:7091`) hands every row
  to `survey_zones_state`, which emits `{kind:"zone", marks:0, status:"empty"}`
  for anything with no tagged marks. So the moment one `body`-bearing row
  exists, the ATLAS table, `_survey_valued`'s pool, `/api/halo/survey`,
  `/export` and `/api/intel/surveying` all start rendering a phantom
  *"Iron Ridge — empty, 0 marks"* next to the real surface row. This is the
  first commit of the build, not a follow-up.
- `GET /api/halo/survey/zones?system=&body=` — rows gain `kind`, `body`,
  `center_lat/lon`, `radius_m`, `nearest_qt*`, and for surface zones the §5
  rollup (`sightings`, `surveyors`, `ores`, `ore_comp`, `bands`, `freshest`,
  `value`). `?body=` filters; omitted returns both kinds.
- `PATCH /api/halo/survey/zones/{id}` — gains `radius_m` (re-fencing an area
  you mis-sized is the common edit; moving the center is a new zone). Must also
  re-slug from the **stored row** rather than the payload, or the body prefix is
  lost on the first rename (§2.4). Note that re-fencing **rewrites membership
  retroactively**, which is fine for the card and not fine for a survey goal's
  denominator — §9 reconciles the two.
- `DELETE` — unchanged, and for a surface zone it's genuinely free: membership
  is geometric, so nothing is tagged and nothing needs untagging.
- `GET /api/halo/survey` + `/export` — surface zones join the `zones` array.
  `_meta.document` is a **fixed string**, `"sc-nav belt survey export"`
  (`app.py:7042`), with no schema version to bump — and it becomes a lie once
  surface zones join. Rename it; the only real version field there is
  `app_version` (`app.py:7043`).
- `GET /api/intel/surveying` — **must stop iterating `nav.belts` only**
  (`app.py:12956`); a surface section keyed by body joins the belt sections.
- `GET /api/halo/survey/zones/{id}/sightings` — the card's timeline. A separate
  endpoint, not a filter on `/api/halo/survey`: that endpoint returns *marks*
  (deep space), and the card would otherwise fetch a system's entire mark list
  to render a moon.

## 8. nav_core

- `surface_zone_members(nav, zone) -> list[Observation]` — a thin filter over
  **`_obs_on_body(nav, system, body, "resource")`** (`nav_core.py:1798`), which
  already does the O(1) `scope_index` bucket lookup and the lat/lon guard. Add
  only the great-circle test and the §9 altitude ceiling. Don't reimplement the
  lookup, and don't drop the category filter (§2).
- `surface_zone_fit(members, body_radius) -> dict` — the surface sibling of
  `survey_cluster_fit`: counts, `_shrunk_composition` ore mix, band histogram,
  distinct surveyors, freshest/oldest stamps, `mined_at` awareness. **Shares no
  code with the belt fitter** — different evidence, different scale, and forcing
  one function to serve both is how the 5,196 km floor would sneak back in.
  **But it must share its statistics with `resource_hotspots`** (§8.1).
- `surface_zones_state(nav, system, zones)` — the `survey_zones_state` sibling;
  emits rows with `kind:"surface"` that the ATLAS table and card consume
  uniformly, and that `plan_halo_drop` is taught to reject (§4).
- `body_coverage(zones, body_radius)` — **absolute mapped area, not a share of
  the body** (§6.2): `3 zones · 24 km²`. Union, never sum (§2.2) — but don't
  derive the union analytically, because N overlapping spherical caps need
  inclusion–exclusion. Rasterize onto the existing `grid_cell` lattice and count
  distinct cells; that reuses the projection §6.2 already commits to and is
  exact enough for a caption.
- `derive_survey_stats` — **does NOT work unchanged, and this is the second of
  the build's two big items** (§1). It buckets on `m["zone_id"]` — a tag
  surface membership never sets — and sessionizes on a **numeric**
  `m["created"]`, while an observation carries an ISO-string `observed_at`; it
  also reads `m["positive"]` and `m["scan"]` (`nav_core.py:8337-8360`). The
  consumer then does `activity.get(z["zone_id"])` (`app.py:12979`), so without
  an adapter every surface zone renders `surveyors: 0, latest: None` and §11's
  credit claim is simply false.
  Cheapest honest fix: an adapter yielding mark-shaped dicts from a zone's
  members (`created` = `observed_at` parsed to epoch, `zone_id` = the zone,
  `positive` = True). **It must de-duplicate by observation id before tallying
  org totals**, because overlapping zones emit the same node more than once
  (§2.2) — per-zone rollups double-count by design, org totals must not.
  **Open decision this doc must make before building:** do observation sessions
  pool with mark sessions in `totals`? They measure different activities
  (surveying a belt vs mining a moon), and pooling makes the org's "survey
  sessions" number mean two things at once.

### 8.1 A named zone is a hotspot with a name — reuse the statistics

`resource_hotspots` (`nav_core.py:2519`) already answers "which areas are
richest in this ore" over the anonymous grid: per area it computes the
empirical hit rate `n_ore / n`, ranks by **Wilson lower bound** so a
well-sampled 8/10 beats a lucky 3/3, tracks `avg_band`, and then — this is the
part §4 needs — synthesizes a `Poi` at the area's center via
`local_km_from_latlon` and calls `nearest_qt_marker` to attach
`nearest_qt`/`nearest_qt_id`/`nearest_qt_dist_m` plus a travel-distance sort
(`nav_core.py:2586-2600`).

A named zone is functionally **that same object with a name and a declared
boundary instead of a derived cell**. So `surface_zone_fit` substitutes a
zone's member set for a cell's sample set and reuses the scoring block
verbatim. Two reasons this is not just laziness:

1. **One statistical model.** If a zone computed likelihood differently from a
   cell, the element finder could rank "Iron Ridge" and the unnamed cell inside
   it inconsistently — the same ground, two numbers. The Wilson bound also
   already solves the §5 problem of three lucky hits reading as "80% Quantainium"
   (it's a better answer there than `_shrunk_composition`, which was written for
   the forecast's own-cell-plus-ring smoothing; use Wilson for zone likelihood
   and keep shrinkage for the forecast).
2. **`nearest_qt` for free**, including the rotating-frame correctness that
   block already handles (markers move; it evaluates at `ROTATION_EPOCH`).

### 8.2 Caching

**Memoization is required here, not a nicety.** `survey_state` is
`nav.version`-cached, but #36.1 zones deliberately are **not** (they need a
SQLite read per request, `app.py:7090`). A surface zone is heavier: membership
scans its body's observation bucket per zone per read, and the body plate
fetches zones + heat cells together.

`scope_index` makes each scan O(bucket) rather than O(dataset), but its cache
key is `(nav.version, len(nav.pois), len(nav.observations))` (`nav_core.py:947`)
— and **every capture bumps `nav.version`**, so during exactly the activity this
feature is designed for, the index rebuilds between reads. Meanwhile
`_survey_valued` has **nine call sites** (`app.py:6653`, `:6831`, `:6846`,
`:6902`, `:7004`, `:7142`, `:7423`, `:7457`, `:12977`), and the
`/api/intel/surveying` one executes once per belt system — three times per
request today. Add §11.1's per-fix chip and this lands squarely on the hot path.

So: version-keyed memoization in the `survey_state` style, extended to include
the zones' own table state, plus §2's category filter to keep each member set
small. No new rate-limit bucket: creation is a cheap write and the reads are
GETs already covered by the existing gate.

## 9. Edge cases & decisions

- **Deep-space capture.** `latitude` is NULL for any fix with no container
  (`nav_core.py:1185`); membership must guard on it, as every existing consumer
  does.
- **Orbital captures DO get a surface lat/lon — membership needs an altitude
  cap.** An earlier draft of this doc claimed "a node logged in orbit is in no
  zone." That is wrong. `Container.detection_radius()` is
  `max(grid_radius, om_radius, body_radius * 1.5)` (`nav_core.py:59`) and
  `_frame_at` stamps lat/lon whenever `container.body_radius > 0`
  (`nav_core.py:1188`) — it checks neither `is_body` nor altitude. So anything
  logged up to **half a body-radius above the surface** (≈442 km over Daymar,
  i.e. most of the QT approach) carries a valid ground track and would fall
  into a 50 km zone. The membership predicate needs an explicit `height_m`
  ceiling; pick it from the vehicle case (a ROC/ship prospecting run is
  sub-10 km), not from the detection radius.
- **`is_body` admits Stars, and there are no Moons.** `is_body` is
  `body_radius > 0 and type in ("Star","Planet","Moon")` (`nav_core.py:56`), so
  a bare `is_body` guard would let someone claim a 50 km zone on **Stanton
  Star** (`BodyRadius` 696,000 km) — cap the body radius too. And every moon in
  `poi/containers.json` is typed `"Planet"`; there is no `"Moon"` type in the
  data, so nothing can filter moons from planets. Say "bodies", not
  "planets and moons", anywhere it matters.
- **Three `AsteroidBase` containers carry `BodyRadius` 200 km** (the Wikelo
  stations), so they mint lat/lon observations while failing `is_body` —
  coordinates on something you can never zone. Harmless, but don't be surprised
  by it in the data.
- **Body radius zero / stations.** `is_body` guard at create time; you cannot
  claim a 50 km circle around Port Olisar.
- **Antimeridian and poles.** Handled by projecting the boundary polygon rather
  than stroking a circle (§6.2), and by `great_circle` for membership — neither
  path uses lon arithmetic that can wrap.
- **A zone someone drew too big.** `radius_m` is PATCHable; the cap is 50 km at
  the API, not just the UI.
- **Retroactive membership is a feature, and also a surprise.** A new zone is
  born with history. The create confirmation states the count it inherited
  (§3, step 3) so nobody thinks the number is a bug.
- **No private observations.** Unlike custom POIs (which carry `private`, and
  which `survey_marks` filters on), observations have no private flag — they are
  already the org's shared dataset. So passive membership (§11) exposes nothing
  that `/api/observations` and `/api/stats`'s `by_body` counts didn't already.
  It does make one member's mining pattern more *legible* once it's named and
  aggregated; if that ever matters, the lever is an opt-out on the member
  record, not a flag on the zone.
- **Depletion.** Two mechanisms, both already shipped, and they answer
  different questions. Per-**node**: `data.mined_at` marks a worked node; the
  card shows it but the rollup still counts it — "ore was here" is the durable
  fact, "ore is here right now" never was. Per-**area**: the `survey_depletion`
  board is already keyed on zone slug and self-expires — see §13.2, which the
  first draft of this doc missed.
- **Admin reset is a REGRESSION to prevent, not a behaviour to inherit.**
  `POST /api/admin/survey/clear` is gated on `nav.belts` (`app.py:7061`) and
  calls `db.clear_survey_zones(system)`, which is
  `DELETE FROM survey_zones WHERE system=?` (`db.py:1052`) — **every zone in the
  system, no kind filter**. So an admin resetting the *belt* after a patch
  silently destroys every named moon zone in Stanton, which is unrelated work by
  a different set of people. The endpoint must scope to deep-space zones, or
  take an explicit kind, before surface zones exist. (Clearing *observations*
  separately remains harmless: membership is geometric, so the cards just empty
  and nothing is orphaned.)
- **Re-fencing vs a survey goal's denominator.** PATCHing `radius_m` rewrites
  membership retroactively — correct for the card, and quietly corrosive for a
  §11.2 survey goal, whose progress denominator then moves mid-flight. Pick one
  and state it: either the goal snapshots the zone's geometry at creation, or
  goal progress is explicitly defined as "against the zone as it is now". Do not
  leave it to whichever reader gets there first.
- **Archived (`closed`) zones still contain observations geometrically.** State
  the answers rather than discovering them: does the §11.1 chip fire inside one?
  Does capture say "counted toward"? Does it count in coverage? Recommendation:
  archived means *historical* — it keeps its card and its history, stops firing
  the chip and the capture line, and drops out of coverage.
- **Shard.** Rollups pool across shards while the navigator has a `shardOnly`
  toggle. That is deliberate — a zone is about place, not instance — and it is
  exactly the kind of deliberate asymmetry that reads as a bug unless the card
  says so.
- **`DELETE /api/me` de-identifies observations**, so a zone's surveyor count
  drops without anything having been deleted. Consistent with the belt side;
  worth stating so it isn't chased as data loss.
- **Two zones with the same center** get no dedup and no warning — §12.3 warns
  on *overlap*, and identical centers are the degenerate case it doesn't
  special-case.

## 10. Test plan

- **nav_core**: membership at the boundary (inside/outside/exactly r), NULL-lat
  guard, the altitude ceiling (an orbital fix with a valid ground track is NOT a
  member, §9), the `resource`-only category filter (a `wildlife` row on the same
  body is not a member, §2), antimeridian and near-pole zones, overlap
  double-counting, coverage as absolute area with union ≠ sum, band histogram,
  empty zone.
- **app**: create on a body → inherits existing observations with no backfill;
  create without `body` → unchanged deep-space path; **a surface zone never
  appears as a phantom "empty, 0 marks" row** in the ATLAS list, `/api/halo/survey`,
  `/export` or Intel Surveying (the `list_survey_zones` filter, §7); the
  `is_body` guard rejects a station *and* a star; slug prefixing prevents the
  Daymar/Yela collision **and survives a rename** (§2.4); a surface slug is
  rejected by the drop planner with a category-appropriate 400, not "no survey
  marks yet"; surface zones score on their own basis and tier in their own pool
  (a belt `$$$` is unaffected by adding a rich moon zone, and a surface zone's
  chip is **not blank**); `nearest_qt_id` resolves and `Set destination`
  round-trips; the stats adapter credits a passive contributor **once**, not
  once per overlapping zone; `POST /api/admin/survey/clear` on a belt system
  leaves surface zones intact; Intel Surveying includes a body with no belt.
- **browser** (headless preview harness): the three map levels, the drill-down
  and the way back, a zone polygon near a pole, the detail card's band
  histogram, promote-a-hotspot.

## 11. Passive contribution: mining *is* surveying

The user's scenario: *"a user may be logging ore as they mine, not even aware
they're in an existing survey zone. Can their marked nodes contribute behind
the scenes? An org leader could declare a priority zone and users just
naturally fill it in as they go about their normal mining."*

**That is already what §2's geometric membership buys, and it is the strongest
argument for that call.** A node logged inside the circle is a member at the
next read — no tag, no active zone, no awareness, no opt-in. The same decision
that removes the backfill also removes the whole "did you remember to press
start" failure mode the belt side lives with (#36.1 §3 had to design the active
zone *specifically* so marks couldn't be dropped untagged; surface zones can't
have that bug because there is nothing to forget).

**Credit on the zone card is free.** `contributors` derives from the members'
`owner_handle` on the observations, exactly as the belt card derives it from
mark owners, so a passive contributor is named on the card without ever opening
Prospector.

**Credit in Org Intel Surveying is NOT free** — this was the first draft's
worst claim, because it reads as a payoff and is actually a build item.
`derive_survey_stats` buckets on a `zone_id` tag that geometric membership never
sets and sessionizes on a numeric `created` that an observation doesn't have, so
surface zones render `surveyors: 0, latest: None` until the §8 adapter exists.
The adapter is cheap; assuming it isn't needed is what costs.

So four things are *not* free, and they are what the scenario actually asks
for: the adapter above, plus the three below.

### 11.1 Feedback — otherwise the loop is invisible

Passive contribution works silently, which also means nobody knows it happened.
Two small surfaces fix that, both siblings of things that exist:

- **Navigator chip**: `📍 Iron Ridge · 48 sightings` when your fix is inside a
  zone — the surface twin of the belt `haloWhereChip`. **Not a client-side
  reuse, though**: `haloWhereChip` returns `""` as soon as `state.container` is
  set (`index.html:15935`), i.e. it is deep-space-only by construction, and it
  can live in JS at all only because belt geometry is a small static doc
  preloaded client-side. The surface predicate is server-side Python, and there
  is no JS great-circle (`projectMeters`, `index.html:6205`, is a
  player-relative tangent projection, not a distance primitive). Two options,
  pick one and budget it: ship a zones-for-this-body payload plus a JS
  haversine, or add a `surface_zone` field to the nav-state push — the latter
  runs the predicate **server-side on every position sample for every online
  member**, which is the §8.2 performance question in its sharpest form.
- **Capture result line**: `counted toward Iron Ridge` on the node-capture
  confirmation, next to the existing ore value chip.

That is the whole awareness mechanism. Deliberately not a prompt, not a modal,
not an opt-in — the point is that it costs the miner nothing.

### 11.2 Priority zones belong in Goals, not on the zone row

> **Sequencing: this is release two, not slice one.** A new goal kind plus a
> spec column, a progress deriver, announce and board placement is a release of
> its own, and it is worth building only once real zones exist to point it at.
> The capability stays a locked requirement — it is the user's scenario — it
> just isn't the first commit.

"An org leader declares a priority" is, in this codebase, **a goal**. The Goals
app already owns priority, deadline, Discord announce + re-post, progress bars,
board placement, and goal-met notification. Reinventing those five things as
flags on `survey_zones` is strictly more code in a worse place.

So: a third goal kind, `kind="survey"`, following the `kind="unlock"` branch
(`app.py:9345`) exactly — its own spec JSON, `priority`/`deadline`, empty
`line_items`, and **derived-only progress with no contributions**, which unlock
goals already established (`_unlock_block`, `derive_unlock_progress`). Lines
keep the materials shape with `unit:"sightings"`.

```
goals.survey_spec = {zone_id, target: {mode: "sightings"|"surveyors", value}, since}
```

Progress derives from the zone rollup, so it updates as people mine. A leader
posts "Survey Iron Ridge — 200 sightings by Friday" to the same board and the
same Discord webhook as every other org goal, and the miners fill it without
being told.

### 11.3 The baseline catch — a priority zone must not be born complete

**This is the one thing that will break if it isn't designed in.** Membership
is retroactive, so a leader who declares a priority zone over an area the org
already mines gets a goal that is instantly at 100% — the evidence predates the
ask. Worse, it would be *silently* wrong: the number is real, it just doesn't
measure the thing the leader wanted.

Fix: `survey_spec.since` is stamped at goal creation, and survey-goal progress
counts only observations with `observed_at >= since`. The zone card keeps
showing its full history (that's the useful number for "should I land here?");
the goal counts new evidence only. Two different questions, two different
counts, both honest — and `observations.observed_at` already carries what's
needed.

### 11.4 What this is and isn't

Be straight about it in the copy: the ore heat grid **already** aggregates
passively, body-wide. What a zone adds is a **name**, a **target**, and
**credit** — which is exactly what turns a heatmap into something an org can
organize around, and nothing more.

And the evidence stays biased in a way the card must not paper over. People log
what they mine, and they mine what's valuable and convenient, so a zone's ore
mix reflects mining behaviour as much as geology — on top of the §5 problem
that there is no negative evidence at all. The §5 footer already carries the
right framing (*"where the org has found ore before, not where ore is now"*);
passive contribution makes it more important, not less.

### 11.5 Milestones

> **Defer this one.** It runs the membership predicate on the capture hot path,
> its dedup is in-process only so it double-fires across workers, and its
> natural gate is a §11.2 survey goal's target — which is release two anyway.

`_survey_capture_milestones` (`app.py:3735`) fires from `_capture_poi` (`app.py:3681`) — the
POI path. Surface nodes arrive via `_capture_observation` (`app.py:3771`), so
it needs a sibling hook there to post "Iron Ridge hit 50 sightings" to the
existing `survey` notify category. Same exactly-on-the-gate discipline (fires
when the count *equals* the gate, never repeatedly), and a survey goal's target
is the natural gate.

## 12. Gaps found in review (fold into the build)

Five things the first draft of this doc missed.

### 12.1 Patch staleness is load-bearing here, and it has a data gap

A game patch reshuffles planetary ore. A zone whose whole promise is *"is this
area worth landing in again?"* built from three-patches-ago sightings is
actively misleading — **more** so than a belt pocket, because rock fields are
stable terrain and surface node spawns are not.

#37 slice 4 §6.1 already designs the answer (watcher sends `game_build` on
`POST /api/position`; staleness is derived by comparing the newest evidence's
build against the org's current-majority build, no stored flags, no admin
ceremony). But it stamps `survey.build` on **marks** — the custom-POI path.
**`observations` has no build column**, only `shard_id` and `observed_at`
(`db.py:64`), so surface zones get nothing from slice 4 as designed.

Two consequences for this build:
- Reserve `observations.game_build` (`_ensure_column`, nullable) now and stamp
  it in `_capture_observation` the moment the session carries one. Cheap, and
  retrofitting it later means a permanent blind era in the data.
- Until then the card's freshness tile is a **date, not a trust signal**, and
  the copy must not imply otherwise.

`shard_id` is not a substitute: it identifies an instance, not a build, and a
zone is about place, not instance. Zone rollups are deliberately **shard-blind**
(unlike NEARBY, which is shard-scoped because it points at a node you could fly
to *right now*).

### 12.2 The element finder should name zones, not just cells

The reverse-lookup flow — "pick an ore, tell me where" — is where a miner
actually starts, and it currently ranks anonymous grid cells planet-side and
org survey clusters in its `IN THE BELTS` section (`index.html:6555`). A named
zone appearing there as **"Iron Ridge — 62% of 47 sightings"** instead of a
nameless cell at some lat/lon is most of this feature's day-to-day value, and
§8.1's shared statistics make the rows directly comparable.

Rule: a zone row **replaces** the cells it contains rather than sitting beside
them, or the same ground gets counted twice in one ranked list.

### 12.3 Nothing stops a body from being littered with overlapping zones

Any member can claim a circle, and §2 allows overlap on purpose. But a
retroactive zone is born looking authoritative (inherited history, populated
card), so ten members each naming their own overlapping 50 km circle on Daymar
produces ten confident, mutually-redundant cards. The belt side never had this
problem because marks are deliberate and sparse.

Minimum guard, at create time only:
- Compute overlap against existing zones on that body and **warn** in the
  create dialog: *"overlaps Iron Ridge by 80% — name this one anyway?"*
  (`promptDialog` already supports a checkbox/confirm shape).
- Never block. Two orgs' pilots genuinely may want a tight zone inside a broad
  one, and a hard rule here would be guessing.
- Surface `can_edit` honestly: zones are creator-or-admin editable
  (`_require_zone_owner`, `app.py:7250`), so admins can already merge by
  deleting the redundant ones — and for surface zones delete is free
  (geometric membership means nothing is tagged, nothing is orphaned).

### 12.4 "Best node right now" is the question the card doesn't answer

§5's rollup answers *is this area good*. On arrival the miner asks *where do I
point the ship*. The card should carry one line: **freshest unworked node at or
above band N, with its age** — and that line is also the §4.1 "Fly to" target.
All three inputs (`observed_at`, `data.band`, `data.mined_at`) are already on
the row.

### 12.5 Hotspot cells inside a named zone should say so

`resource_hotspots` feeds both the element finder and the promote-a-hotspot
path (§3, step 6). A cell already inside a named zone should render as
*"in Iron Ridge"* rather than offering to name it again — otherwise the promote
affordance manufactures the §12.3 clutter it's meant to avoid.

## 13. Cross-tool hooks (review pass 2)

The first draft named two cross-app hooks (the `kind="survey"` goal, the
`survey` notify category) and missed the rest of the suite. Ranked by
value-to-effort. The rejections in §13.8 matter as much as the accepts.

### 13.1 "Who's in this zone right now" — presence already carries lat/lon

`Hub._public_presence` (`app.py:2681`) already publishes `system`, `body`,
`lat`, `lon`, `heading` per sharing member. **Zone membership is the identical
predicate** (`great_circle`) applied to a presence record instead of an
observation row — the same "geometric, no new capture" argument §2 makes,
applied to live fixes.

ATLAS row gets `👥 2 here now`; the §11.1 navigator chip becomes
`📍 Iron Ridge · 48 sightings · two org-mates here`. That turns §11's
invisibility problem from an information problem into a social one — you don't
just learn your nodes counted, you learn someone is 8 km away.

Privacy is structural, not added: `touch_presence` (`app.py:2696`) only builds
a record when `sess.share_presence`, so a non-sharer is absent by construction.

### 13.2 The depletion board is already keyed on zone slug — §9 missed a whole subsystem

`survey_depletion.key` is documented as **`SVY-* | Wtn-* | zone slug`**
(`db.py:475`), with `POST /api/survey/depleted`, `active_survey_depletion()`
(`app.py:435`), a time age-off org setting `survey_depletion_ageoff_min`
(default 240 min ≈ one play session) and a wired frontend button. §9's
`mined_at` bullet describes per-node depletion and misses the per-**area**
mechanism that already exists.

This is the direct answer to §5's honesty problem. The footer says *"where the
org found ore before, not where ore is now"* — the depletion board is the
shipped, self-expiring way to say *"…and it isn't now."* The zone card gets
`⛏ worked out` and a `MINED OUT · 40m ago` chip that evaporates on its own.

Only the 404 guard widens (it resolves the key against the belt pool,
`app.py:7455`), and §4 already establishes a separate surface pool.

**Invariant to respect:** `active_survey_depletion()` currently feeds *routing*
— the ore-goal pocket pool and the element finder's `depleted` set. Surface
reports must be **display-only** and must not leak into either, for exactly the
reason §4 keeps surface zones out of `plan_halo_drop`.

### 13.3 Put the chip on the glass, not just in the SPA

§11.1's feedback reaches the miner only when they alt-tab — and the person it's
designed for is in the game. `POST /api/position` already returns a `nav`
object built from the frame snapshotted in that same request (watcher-overlay
§5.1: "no extra compute, no extra lock time, no extra request"), and the
overlay renders target · distance · ETA · bearing.

Adding `nav.zone = {name, slug, sightings}` when the fix is inside a zone is
**the same predicate on the same fix already in hand** — one dict key. Then
§11.1's "it costs the miner nothing" becomes literally true.

### 13.4 Zone → LFG, and zone → Event

Both are existing seed patterns with existing producers; a zone is just a third
caller.

- **LFG** (`postLfg`, `index.html:6919`): prefill `tags:["mining"]` (already in
  the shared vocab), `rally` = the zone's `nearest_qt` name (§4 computes it),
  note = the zone's name + top ores. In-memory + WS, so a zone DELETE orphans
  nothing.
- **Event** via the shared `eventSeed` (`index.html:7053`, already fed by
  `promoteLfgToEvent` and `promoteWarningToEvent`): `event_location` = the
  zone, `location` = the rally QT marker, `types:["Mining Op"]`, seat roles.
  Both `events.location` and `events.event_location` are free TEXT
  (`db.py:183`), so **no schema change**.

Why both, when §11.2 routes priority to Goals: Goals owns priority, deadline
and progress; Events owns rally points, rosters, reminders and the Discord
manifest. A 4-person mining op is an event; a 200-sighting target is a goal.
Together they close the loop — **goal declares → LFG recruits → presence
(§13.1) confirms they showed → geometric membership credits them
automatically.**

### 13.5 Coverage % belongs in `/api/stats` too

`/api/stats` already builds `by_body`/`top_bodies` = "most-mapped by us"
(`app.py:11293`). §8's `body_coverage` shipped into that table turns *"where
have we been"* into *"where should we survey next"*:
`Daymar · 412 records · 3 zones · 24 km² mapped` — absolute area, for the
reason §6.2 gives (a percentage of a body is a rounding error at this scale).
One dict merge on an aggregation that already scans every observation. (Org Intel Surveying answers
*who* surveyed; this answers *what is* surveyed.)

### 13.6 Danger board ↔ zone — the valuable direction is inbound

Filing a warning at a zone needs nothing new: `anchor_a_poi` = the zone's
`nearest_qt_id`, free-text `location` = the zone name, and `_leg_warning_view`
(`app.py:4474`) already falls back to `location`. The half worth building is
the **reverse** match — a warning resolving into a zone lights a `⚠` chip on
its ATLAS row and, with §13.1, on the navigator chip. A solo Prospector pilot
50 km from anywhere is the highest-value recipient of a PvP warning in the
whole org, and today the board cannot reach them.

### 13.7 Correction to §11.5 — the milestone hook needs a dedup key

`_capture_observation` (`app.py:3771`) is the **hot** path: every ore node every
member logs. `notify._pace` reserves a 2.0 s slot per category, so
exactly-on-the-gate is necessary but not sufficient — pass a `dedup_key` too
(`notify.send`'s 120 s dedup), which the belt milestone path already does.

### 13.8 Rejected — considered and deliberately not done

- **Surface zones as `hazard_volumes` / snare-detour input.** Those volumes are
  built in the **non-rotating** global frame at a `t_ref` — the identical
  objection §1 (item 3) raises against the belt fitter, and fatal for the same reason.
  Independently, a 50 km cap on a crust intersects no quantum leg;
  `body_volumes`/`chord_obstructed` already model the body as the obstacle.
- **Zone as a marketplace `pickup_poi` or inventory `location`.** Both are free
  text, which is the trap — a pickup point is somewhere two members can meet
  and hand over cargo; a zone is 50 km of dirt with no station, terminal or
  storage. It would mint listings nobody can fulfil.
- **"Where do I sell this zone's ore" → the trade planner.** Wrong app twice:
  mined output is a **raw** commodity needing refining, and there is no
  refinery entity anywhere in the codebase; and the trade solver's world is
  terminal→terminal legs off the UEX feed. Ore value is already answered by the
  `$$$` chips §5 puts on the card.
- **Auto-creating the survey goal when a zone is created.** Breaks §11.3, the
  load-bearing decision: `survey_spec.since` must be stamped deliberately by a
  leader, and zone-creation time is the wrong moment by construction. It would
  also shout in two Discord channels (`survey` + `goals`) from one action.
- **A new notify category for surface zones.** `CATEGORIES` is a closed tuple
  and each entry is an admin channel-routing decision; no org wants
  `#surface-survey` separate from `#survey`.
- **A "zones named" leaderboard column.** `/api/leaderboard` counts *evidence*.
  Naming a circle is one tap with zero evidentiary content and is trivially
  farmed. Credit already lands honestly through `derive_survey_stats` → Org
  Intel Surveying, keyed on the members' observations.
- **A zone-scoped `resource_forecast`.** Its signature fits, but it is
  deliberately a *neighborhood* model (own cell + ring-1, distance-weighted).
  A 50 km zone spans many cells, so "the zone's forecast" would be a second
  number that looks like §5's composition and answers a different question.
  One composition chart, not two.

## 14. Audit trail — what the adversarial passes found

**Everything below is already folded into §1–§13.** This section is kept so the
mistakes can't be re-introduced by someone who reasons their way back to the
original, more convenient claim — each entry says what was wrong, where the
correction now lives, and what the code actually does. It is not a pre-read.

**Pass 3** checked every claim in the first draft against the code. **Pass 4**
re-checked pass 3 and found three of *its* findings imprecise — marked
⚠ **pass-3 error** below, because a wrong correction is worse than the original
mistake: it carries the authority of a review.

| # | The first draft claimed | Verdict | Now corrected in |
|---|---|---|---|
| 14.1 | surface zones just need their own tercile pool | there is no score to tier at all | §4 |
| 14.2 | "credit is free" in Org Intel Surveying | needs an adapter | §8, §11 |
| 14.3 | omitting `body` is byte-for-byte unchanged | true of writes, false of every read | §7 |
| 14.4 | a body prefix fixes slug collisions | PATCH drops it; truncation eats it | §2.4 |
| 14.5 | add a tailored 400 to the drop planner | the reject exists and misdiagnoses | §4 |
| 14.6 | one helper stamps `nearest_qt_id` | it's a two-step recipe | §4, §8.1 |
| 14.7 | reuse the belt card | it needs explicit suppression | §5 |
| 14.8 | "12% of Daymar" | 0.72% is the ceiling; report km² | §6.2, §8, §13.5 |
| 14.9 | reuse `haloWhereChip`'s predicate | it's deep-space-only, and server-side here | §11.1 |
| 14.10 | membership is just a great-circle test | needs a category filter; helper exists | §2, §8 |
| 14.11 | caching "should" be added | required; `nav.version` churns under mining | §8.2 |
| 14.12 | §9 covered the edge cases | six missing, one a live regression | §9 |
| 14.13 | (sequencing not considered) | four cuts and two deferrals | §7, §11.2, §11.5 |

### 14.1 The value tier — the detail that makes it a feature, not a flag

`_survey_value_from_index` (`nav_core.py:7950`) computes
`SURVEY_DENSITY_W[density] × price` over three bases in priority order —
`scanned` (`:7963`), `ores` (`:7979`), `density` (`:7989`) — and bails at
`:7953` on `if positives <= 0:`.

⚠ **pass-3 error:** pass 3 wrote that it "returns `None` when `positive <= 0`".
The local is `positives`, and the bail is **not** unconditional: a *salvage*
signal still returns a dict (`{"basis": "salvage", "salvage": True}`). The
conclusion survives intact — an observation has no `positives`, no `rocks`, no
`density` **and no salvage**, so it can't even take the escape hatch — but the
precise shape matters to whoever writes the fifth basis.

### 14.2 The stats adapter

Detail in §8. The de-dup requirement is the non-obvious half: overlapping zones
emit the same observation more than once (§2.2), so per-zone rollups
double-count by design and org totals must not.

### 14.3 Phantom empty zones

`db.list_survey_zones` is `SELECT * FROM survey_zones WHERE system=?` with no
body filter (`db.py:995`). Note the table has **no `body` column at all** today
(`db.py:522-532`) — §2.4 adds it — so this filter and that column are one
commit, and it must be the first.

### 14.4 The slug prefix

Both traps in §2.4. Pass 4 adds a third, cosmetic but real: these handlers
already bind the request payload to a parameter named `body` (`app.py:7194` is
`body.name`), which is about to collide with the celestial `body`. Named in
§2.4.

### 14.5 The drop-planner reject

⚠ **pass-3 error:** pass 3 put the "no survey marks yet" message in
`_halo_goal_system` at `app.py:6845`. It is in `_halo_goal`, at
`app.py:6851-6852`, guarded by `if not zone.get("xyz"):`. The all-systems
no-body match is real and is at `app.py:6757-6758`; the ambiguity 400 is at
`app.py:6759-6763`. Corrected in §4.

### 14.6 `nearest_qt_id`

Recipe in §4 and §8.1. One caveat §8.1 should not oversell: `resource_hotspots`
passes `ROTATION_EPOCH` at `nav_core.py:2592` even though a resolved `t_ref` is
in scope at `:2582`. Copy the recipe, but don't cite that line as evidence the
rotating frame is handled carefully — and consider whether the surface path
wants the resolved `t_ref` instead.

### 14.7 Card suppression

⚠ **pass-3 error, two of them.** Pass 3 said the chart "needs ×100". It does
not: it prints composition literally (`index.html:16952`) and the **server
already stores `scan_comp` in percent units** (`nav_core.py:7971` divides by
100 on the way in), so the two agree. The conversion belongs on the producing
side, because `_shrunk_composition` returns 0..1 — feed percent. Pass 3 also
said **Plan a drop here** and **Set active** are unconditional; they aren't.
The drop button is gated on `z.marks` (`index.html:17053`) and self-suppresses
if the surface row never populates `marks`; **Set active** is gated only on
`!z.closed` (`:17054`) and genuinely does need suppressing. `surveyRsCard` is
unconditional as stated (`:17063`). All corrected in §5.

### 14.8 Coverage

§6.2, §8 and §13.5. A maximum 50 km zone is a spherical cap of ~7,840 km²
against Daymar's ~1,093,600 km² — **0.72%**; "12%" would need ~17
non-overlapping maximum zones.

### 14.9 The navigator chip

§11.1. `haloWhereChip` returns `""` on `state.container` **or** a missing
position (`index.html:15935`), with four further early exits below it — it is
comprehensively deep-space-only, not incidentally so.

### 14.10 Scoping the member set

§2 and §8. `_obs_on_body` (`nav_core.py:1798`) already defaults to
`category="resource"` and guards both coordinates, so the correction mostly
consists of *not* writing new code.

### 14.11 Caching

§8.2. Pass 4 sharpens two numbers: `scope_index`'s key is the composite
`(nav.version, len(nav.pois), len(nav.observations))` (`nav_core.py:947`), not
`nav.version` alone — the churn argument is unaffected, since every capture
moves all three. And `_survey_valued` has **nine call sites**, of which
`/api/intel/surveying` is one, executed once per belt system (three times per
request today); pass 3 read those three executions as three call sites.

### 14.12 Edge cases

All six are now bullets in §9. The first is the one that matters: `POST
/api/admin/survey/clear` → `db.clear_survey_zones` is
`DELETE FROM survey_zones WHERE system=?` (`db.py:1052`), gated on `nav.belts`
(`app.py:7061`) — a belt reset would take every named moon zone in the system
with it. That is a regression to prevent, not a note.

### 14.13 Cuts and sequencing

Folded into the sections they cut: the belt-gate widen (§7 — `build_belt_registry`
returns exactly `{Stanton, Nyx, Pyro}`, `nav_core.py:7372-7385`, and every body
lives in one of them, so the widen is a no-op), `_meta.document` (§7), the
milestone hook (§11.5, deferred), and the `kind="survey"` goal (§11.2, release
two). **Kept against the reviewer's advice:** §6.1's body badge. Its job isn't
"a way to reach level 2" — the ATLAS row does that — it is the user's explicit
ask that the system map keep showing, at a glance, where the org has surveyed.
It's a ring and a count.

## 15. Not in scope (fast-follows)

- **Polygons and multi-part zones.** A circle covers one landing's range; if
  the area you want isn't a circle, make a second zone rather than adding
  shapes. (A *shape* limit, not a visit limit — see §2.1.) Revisit only if real
  use proves otherwise.
- **Auto-suggesting a zone** from a dense cell cluster (the promote path in §3, step 6
  is the manual version; automatic naming can wait for evidence it's wanted).
- **Value-aware mining circuits** on the surface (that's #37 §7.3, and it needs
  this data to exist first).
- **Cross-body comparison** ("the best Iron country in Stanton") — the element
  finder already answers the node-level question; the zone-level version should
  wait until there are enough zones for a ranking to mean anything.
