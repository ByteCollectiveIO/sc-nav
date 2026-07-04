# Snare-detour routing (backlog #24 v2) — design plan

**Status: BUILT 2026-07-04 (all 8 build-order steps), suite 389 green (254
nav_core + 135 app), pending /deploy.** This is the "lane
snare-detour" work parked when #24 v1 shipped (v0.34.0). It upgrades danger
handling from *endpoint matching* (drop/badge trades whose buy/sell POI is
warned) to *flight-path geometry* (detect a leg that merely flies **past** a
danger, and synthesize a detour waypoint around it). Both the Trade Route
Planner (`#/trade`, #21) and the Cargo Hauling Planner (`#/route`, #12) get it.

Read `docs/pirate-warnings.md` first for the v1 board/avoid/warn machinery this
builds on. Everything below is implementable in `nav_core` + `app.py` +
`index.html` with **zero new dependencies**.

---

## Design decisions (and the reasoning, so they don't get relitigated)

### 1. No graph database, no graph library

The question "should routing become a graph?" mostly dissolves on the game's
physics. In Star Citizen you cannot QT to empty space — you jump
marker-to-marker, and from anywhere you can jump to *any* in-system QT marker.
So intra-system space is already a **complete graph** over QT markers with
Euclidean edge weights, and in such a graph the shortest path is always the
direct edge. That is why today's straight-line `travel_cost` is *correct*, not
a shortcut. A detour only exists when we **forbid** a corridor (a snare
capsule); then the optimum is the direct edge replaced by one intermediate
marker — occasionally two if capsules overlap. That is a bounded local search,
not general pathfinding.

The one place routing genuinely is a graph — cross-system gates — is already
implemented as one: `system_path()` BFS over `GATE_LINKS` (nav_core ~line
1508). It stays as-is.

Search-space sizes (measured from the live dataset, 2026-07-04):
**Stanton 196 QT markers, Pyro 30, Nyx 5** (total 231; `nav.qt_markers` after
`synth_container_pois` + `index_qt_markers`). A one-hop detour scan is ~200
candidates × a few segment tests. Neo4j is a database server (ops + licensing
cost) for million-edge graphs; networkx would add a dependency for algorithms
we need ~40 lines of geometry for. **Neither. Pure functions in `nav_core`.**

### 2. Extend `travel_cost`, don't replace it (answers "unify or duplicate?")

Every distance consumer already funnels through `travel_cost`: the trade
solver (`_cost_route`, `_greedy_route`), the cargo planner (`plan_route` cost
matrix), resource hotspots. So the unification is: add an **optional
`avoid=` parameter** to `travel_cost`. With `avoid=None` (default) the code
path is byte-for-byte today's — zero cost, zero regression risk for every
existing caller. With volumes present it tests the leg's segments and, on
conflict, runs the detour search. Both planners inherit the feature by
threading one kwarg; no redundant second routing system is created.

### 3. The 12-stop cap is orthogonal — leave it alone in this work

`TradePlanIn.max_stops` `le=12` (app.py ~2198, halved to 6 legs) is not a
travel-model limitation and a graph would not lift it. It bounds greedy solver
work (already cheap — each `travel_cost` is a couple of `dist3`s) and, more
importantly, plan *usefulness*: prices rot over a multi-hour run (the reason
v0.33.0 shipped the freshness filter) and greedy error compounds per leg.
Raising the cap is a one-line Pydantic change plus maybe a bigger
`_TRADE_RESTARTS`; do it as its own decision, not smuggled into this feature.
Detour tests add per-leg cost only when warnings are active (see Performance).

### 4. Three use cases, one mechanism

| Case | Geometry helps? | Behavior |
|---|---|---|
| **Snare on the flight line** (lane warning, or point warning near the path) | Yes | Insert waypoint: "+1 jump to dodge, +N% time" |
| **Camped destination** (point warning AT the buy/sell/delivery POI) | No | Trade solver: drop candidate (avoid) / loud badge (warn). Cargo + manual legs: can't drop a contractual stop — flag `blocked`, escalate UI |
| **Personal blacklist** (player never wants to see POI X) | Same as above | Blacklisted ids feed the same two inputs: endpoint filter + point volumes |

### 5. Avoid mode gets *smarter*, not just stricter

v1 avoid mode drops the exact snared lane (`avoid_pairs`) from
`_trade_candidates`. With detours, a snared lane is usually still viable via a
waypoint — dropping it throws away profit. **New semantics in avoid mode:**
- Camped-POI endpoint drops (`avoid_poi_ids`) stay — no geometry fixes a
  camped terminal.
- `avoid_pairs` lane drops are **replaced** by detour costing: the candidate
  stays in the pool, its haul is costed *with* volumes, so the solver either
  routes around (paying the honest detour distance in its own score) or
  naturally deprioritizes it. If no detour clears (`blocked`), `_greedy_route`
  skips the candidate.

### 6. Default `avoid_mode` flips from `ignore` to `avoid`

Per product decision: danger handling should be on unless overridden. Trade:
default `"avoid"` (solver picks alternates; manual legs are still never
dropped, only badged). Cargo: also `"avoid"` — safe because cargo stops are
contractual so avoid can only ever add detours, never drop stops. The
ignore/warn/avoid seg stays for override and is persisted (favorites already
store `avoid_mode`; saved configs keep whatever they saved — only the
*unset* default changes). Update `TRADE_AVOID_HINT` copy accordingly.

---

## nav_core additions (all pure, all unit-testable)

### A. Hazard volumes

```python
# Severity-scaled hazard radius. Base is an org setting (see app.py section);
# these multipliers are fixed in code.
HAZARD_SEVERITY_SCALE = {"sighted": 0.5, "active": 1.0, "deadly": 1.5}
DEFAULT_HAZARD_RADIUS_M = 5_000_000.0    # 5,000 km — see Tuning

def hazard_volumes(nav, warnings, t_ref, *, radius_m=DEFAULT_HAZARD_RADIUS_M,
                   extra_point_ids=()) -> list[dict]
```

Returns a list of volumes: `{"kind": "sphere"|"capsule", "a": (x,y,z),
"b": (x,y,z)|None, "r": float, "warning_id": str|None, "system": str}`.
- `point` warning with a resolvable anchor → sphere at
  `poi_global_m(anchor, t_ref)`.
- `lane` warning with both anchors → capsule (segment a→b, radius r).
- Un-anchored warnings contribute nothing (board-only intel, same rule as
  `trade_avoid_sets`).
- `extra_point_ids` (personal blacklist POI ids) → spheres with
  `warning_id=None`.
- `r = radius_m * HAZARD_SEVERITY_SCALE[severity]` (blacklist uses ×1.0).

### B. Segment geometry

```python
def _seg_point_dist(p0, p1, c) -> float          # min distance segment→point
def _seg_seg_dist(p0, p1, q0, q1) -> float       # min distance segment→segment
def segment_hits(p0, p1, volumes) -> list[dict]  # volumes the segment enters
```

Standard closed-form clamped-parameter formulas; ~40 lines total. A sphere is
hit when `_seg_point_dist < r`; a capsule when `_seg_seg_dist < r`. Filter
volumes by `system` before testing (positions are global but systems are far
apart; the filter is for clarity + speed, keyed off the leg's system).

### C. `travel_cost(nav, src, dst, t_ref=None, *, avoid=None, memo=None)`

- `avoid=None` → **identical behavior and identical returned dict as today.**
  Write a regression test asserting this on a handful of legs.
- `memo` — optional dict keyed `(id(src) or src.id, dst.id)`; solvers pass one
  per solve because the greedy inner loop re-costs the same POI pairs heavily.
- With volumes: decompose the leg into its real jump segments — the code
  already does this decomposition, reuse it:
  - intra-system direct: 1 segment (from_pos → dst ref)
  - intra-system moon rule: 2 segments (from_pos → parent, parent → ref)
  - cross-system: the src-side intra leg(s) + dst-side intra leg(s). The gate
    tunnel itself is not testable space; a camped gate is a point warning at
    the gate POI, which the approach segment's endpoint test catches.
- Per conflicting segment, run the detour search (D). New keys on the returned
  dict (absent/empty when `avoid` is None or nothing conflicts):
  - `waypoints`: ordered `[{"id": poi_id, "name": str}]` extra jumps inserted
  - `detour_m`: added distance vs the direct leg
  - `dodged`: warning ids successfully routed around
  - `blocked`: warning ids that could NOT be avoided (an endpoint sits inside
    a volume, or no clearing waypoint within budget)
- **Endpoint-inside-volume** (the camped destination): detect before
  searching — if the dst ref point (or src position) is inside a volume, it's
  `blocked` immediately; no waypoint can fix it.

### D. Detour search

```python
_DETOUR_BUDGET = 1.5   # give up if the best detour exceeds 150% of direct

def _detour_via(nav, p0, p1, volumes, system, t_ref) -> tuple[poi|None, float]
```

Candidates: `nav.qt_markers` in `system` with a resolvable
`entity_global_m` position. Keep every `W` where `seg(p0, W)` **and**
`seg(W, p1)` clear *all* volumes; return the min-added-distance one. Prune
with the ellipse bound `d(p0,W)+d(W,p1) < _DETOUR_BUDGET * d(p0,p1)` before
running segment tests (cheap first). ~200 candidates worst case — trivial.

**Two-waypoint search is deliberately deferred.** It's only needed when
capsules overlap so heavily that no single marker clears both sides — rare
with real board data. If 1-hop fails, return `blocked` and let the solver
skip / the UI badge. Leave a `# v2.1: two-waypoint fallback` comment at the
spot. (If later needed: pairs pruned by the same ellipse bound, O(M²)≈38k
tests worst case, still fine.)

### E. Warn-mode geometric annotation

`trade_leg_warnings` (endpoint matching) stays. Add:

```python
def leg_hazards(nav, src, dst, volumes, t_ref) -> list  # warning ids the direct leg crosses
```

Used by warn mode to badge fly-past dangers *without* changing the route —
this is a new capability (v1 could only match endpoint anchors).

### F. Solver threading

- `plan_trade_route(..., avoid_volumes=None)` → `_solve_route` →
  `_greedy_route` / `_cost_route`: every `travel_cost` call gains
  `avoid=avoid_volumes, memo=memo` (one shared `memo` per solve). In
  `_greedy_route`, a candidate whose haul comes back `blocked` is skipped (in
  avoid mode only). Detour distance flows into `distance_m`, so scores/ETAs
  are automatically honest.
- `_trade_candidates`: **keep** `avoid_poi_ids`; **stop passing**
  `avoid_pairs` when volumes are supplied (decision 5). Keep the parameter for
  backward compat / tests.
- `cost_trade_legs` (manual mode) gains `avoid_volumes=None`: manual legs are
  never dropped, but they get costed detours and `blocked` badges.
- `replan_trade_route`: threads through automatically via `plan_trade_route`;
  the `held` sell leg is costed with volumes too (you're already loaded — a
  detour on the sell approach is exactly what a mid-run reroute is for).
- **Cargo** `plan_route(..., avoid_volumes=None)`: the n×n cost matrix and
  `start_legs` calls gain `avoid=`. Stops are contractual — never dropped —
  so cargo "avoid" ≡ "detour where possible, flag `blocked` where not". Leg
  views come from `_leg_view`: extend it to carry
  `waypoints`/`detour_m`/`dodged`/`blocked` when present (it already picks
  explicit keys, so absent keys need a `.get`).

---

## app.py changes

### Models
- `TradePlanIn.avoid_mode` default `"ignore"` → `"avoid"` (decision 6).
- `TradePlanIn` + `RoutePlanIn` gain
  `avoid_poi_ids: list[int] = Field(default_factory=list, max_length=50)` —
  the personal blacklist, client-supplied (stored in localStorage, see
  frontend). Validate ids exist in `nav.pois` (silently skip unknown — POIs
  can be refreshed away).
- `RoutePlanIn.avoid_mode: str = "avoid"` — new; cargo had no danger wiring
  at all.

### Wiring
- `_solve_trade_plan`: build volumes once per request —
  `nav_core.hazard_volumes(nav, warnings, t_ref, radius_m=<setting>,
  extra_point_ids=body.avoid_poi_ids)` — when mode ≠ ignore and there's
  anything to build; pass as `avoid_volumes` in **avoid** mode; in **warn**
  mode solve without them but annotate with them.
- `_annotate_trade_legs`: also attach geometric hits (`leg_hazards`) and
  translate `dodged`/`blocked` warning ids into `_leg_warning_view` dicts so
  the client renders names. Add `dodged`/`blocked` view lists per leg.
- Cargo `/api/route/plan` + `/api/route/run` (POST): same volume build +
  pass-through; persist `avoid_mode` + `avoid_poi_ids` in the run's stored
  plan params (runs table already stores the plan JSON) so run-mode rerenders
  and future replans keep the mode.
- Trade run replan (`/api/trade/run/replan`): already re-solves with the
  run's `avoid_mode`; it inherits volumes via `_solve_trade_plan`'s shared
  path — verify `TradeReplanIn.avoid_mode` override still works.

### Settings
- New org setting `hazard_radius_km` (int, default **5000**, admin-editable
  in ORG SETTINGS next to `warning_ageoff_min`/`warning_stale_min`), served
  via `/api/settings` like its siblings. This is the base radius;
  severity multipliers stay in code.

---

## Frontend changes (`server/static/index.html`)

### Trade Route Planner `#/trade`
1. **Avoid seg default** → `avoid` (`let tradeAvoid = "avoid"` ~line 6524);
   update `TRADE_AVOID_HINT` copy; `applyTradeConfig` fallback
   `cfg.avoid_mode || "avoid"`.
2. **`renderTradeLeg`** (~line 7219): the `tl-move` line gains the detour —
   `haul 12.3 Gm · <span class="via">dodge via OM-3 (+0.8 Gm)</span>`; reuse
   the existing `.via` accent class. A leg with `blocked` warnings renders an
   escalated `tl-danger` line: `☠ destination camped — no reroute exists`
   (severity class from the worst blocked warning).
3. **Route-level callout** (~line 7202): split the current single message into
   `↪ N legs rerouted around danger (+X Gm total)` (informational, avoid
   mode) vs `⚠ N legs touch danger that cannot be routed around` (warning,
   links the flagged legs).
4. **Run mode**: the active-leg guidance renders waypoints as explicit
   pre-steps: `QT to OM-3 first — dodging ☠ deadly at Yela ↔ CRU-L1 · then
   QT to <sell terminal>`. Same for the trade run checklist rows.
5. **Blacklist UI**: a compact "AVOIDED LOCATIONS" chip row inside HOW TO
   PLAN (below the danger seg): `attachPoiPicker` input to add, × per chip to
   remove, persisted in `localStorage` (`tradeAvoidPois`), sent as
   `avoid_poi_ids` from `buildTradePlanBody`. Shared with cargo (store once,
   read by both planners — it's "places I avoid", not per-planner).

### Cargo Hauling Planner `#/route` (currently has NO danger UI)
1. **Danger seg**: add the same ignore/warn/avoid 3-way seg near the plan
   controls, defaulting to `avoid`. Factor the seg + hint into a small shared
   helper (or duplicate the 6-line pattern — either is fine, but keep the
   same wire values and visual style as trade's; cargo's "avoid" hint should
   say "detour around dangers — your stops never change").
2. **`renderStop`** (~line 6485): the leg line gains
   `· <span class="via">dodge via OM-3 (+0.8 Gm)</span>`; a stop whose
   arrival leg has `blocked` warnings gets a `.route-danger` line:
   `☠ this stop is reported camped (deadly, PvP) — fly in expecting company`.
3. **`renderRun`** (~line 6418, run-mode checklist): same waypoint prefix on
   the leg line, e.g. `QT to OM-3 (dodge) · then QT to <b>marker</b> · 12.3
   Gm`.
4. **Plan summary**: mirror trade's route-level callout (rerouted-vs-blocked
   counts).
5. Send `avoid_mode` + shared `avoid_poi_ids` in the `/api/route/plan` and
   run-start bodies.

### Live-run reroute nudge (both planners)
The WS `warnings` frame already reaches every tab. On receiving one while a
trade/cargo run is active, client-side check whether any *new* warning
touches the remaining legs (endpoint match is enough client-side — ids are in
the leg views) and show a dismissible banner: `☠ New danger reported on your
route — [Re-plan around it]`. Trade wires the button to the existing
`/api/trade/run/replan`; cargo v1 of this can simply link to re-planning the
remaining stops (cargo has no replan endpoint — do NOT build one for this;
just deep-link back to the planner with the run's packages, or defer the
cargo banner entirely if it gets fiddly).

---

## Tests

`server/test_nav_core.py` (extend the existing #24 test classes' style):
- Geometry: `_seg_point_dist`/`_seg_seg_dist` known-answer cases (parallel,
  crossing, degenerate point-segments, clamped endpoints).
- `hazard_volumes`: anchored/un-anchored/blacklist/severity-scaling.
- `travel_cost` **regression**: `avoid=None` returns exactly today's dict
  (build a plan pre/post on synthetic data and diff).
- Detour: synthetic system (3 stations in a line + a capsule across the
  middle) → waypoint inserted, `detour_m > 0`, `dodged` populated; endpoint
  inside sphere → `blocked`, no waypoints; no clearing marker → `blocked`.
- `plan_trade_route` avoid mode: snared lane now **detoured not dropped**
  (assert the lane's trade appears with waypoints); camped endpoint still
  dropped; blocked-haul candidate skipped.
- `cost_trade_legs` manual: blocked leg badged, never dropped.
- `plan_route` cargo: detour in the cost matrix changes stop order when it
  should; blocked stop flagged; `avoid_volumes=None` byte-identical.

`server/test_app.py` (mirror `TradeDangerWiringTests`):
- `avoid_mode` defaults, `avoid_poi_ids` accepted + capped + unknown-id-safe,
  cargo plan/run persistence of the mode, `hazard_radius_km` setting
  round-trip, annotate views carry `dodged`/`blocked`.

---

## Build order

1. **Geometry + `hazard_volumes`** (pure nav_core + tests). No callers yet.
2. **`travel_cost(avoid=, memo=)` + `_detour_via`** + the `avoid=None`
   regression test. Still no behavior change anywhere.
3. **Trade threading**: solver kwargs, decision-5 candidate semantics,
   `leg_hazards`, `_annotate_trade_legs` views, `_solve_trade_plan` volume
   build, replan path, default flip, `avoid_poi_ids`, setting. Tests.
4. **Cargo threading**: `plan_route(avoid_volumes=)`, `RoutePlanIn`, run
   persistence. Tests.
5. **Frontend trade** (seg default, leg/route rendering, run mode, blacklist
   chips).
6. **Frontend cargo** (new seg, stop/run rendering, summary callout) + admin
   `hazard_radius_km` input.
7. **Reroute nudge banner** (trade first; cargo only if cheap).
8. `/deploy`.

Steps 1–2 are shippable with zero UI change; steps 3–4 are independently
testable behind the API. Keep commits per step; suite must stay green
throughout (349 at v0.34.0).

## Performance notes

- Volumes are built **once per request**, only when mode ≠ ignore and the
  board has anchored warnings (or a blacklist is supplied). The
  `avoid=None` fast path costs nothing.
- The greedy inner loop re-costs repeating POI pairs — the shared `memo`
  makes detour search run at most once per (src, dst) pair per solve.
- Worst realistic case: ~600 candidate pairs × (≤3 segments × ≤20 volumes)
  distance tests + a few 200-candidate detour scans — well under 50ms in
  Python. No solver restructuring needed.

## Tuning / open items (decide during implementation, defaults given)

- `DEFAULT_HAZARD_RADIUS_M` **5,000 km**: an in-game Mantis bubble is ~20 km,
  but the hazard corridor players actually respect (anchor imprecision +
  repositioning pirates) is much wider; at Gm leg scales a 5,000 km berth
  costs almost nothing extra. Admin-tunable via `hazard_radius_km`, so the
  default just needs to be sane.
- `_DETOUR_BUDGET` 1.5 (give up past +50% distance → `blocked`).
- Severity scale 0.5/1.0/1.5 — fixed in code, revisit with board data.
- Positions use the plan's fixed `t_ref` (moons move; markers rotate) — the
  same approximation every existing consumer makes. Fine.
- Two-waypoint detours: deferred (see D).
- Raising `max_stops` past 12: explicitly out of scope (decision 3).
