# Quantum fuel & jump-range in the route planners — design

Status: **DESIGN ONLY — not built. UNBLOCKED 2026-07-04.** Adds real
quantum-drive fuel-usage and max-jump-range to both the **Cargo Hauling
Planner** (`#/route`) and the **Trade Route Planner** (`#/trade`).

> **Data-source update (2026-07-04):** the primary source is now the **SC Wiki
> API** (backlog #26): `GET /api/vehicles` carries a precomputed per-ship
> `quantum` block (speed / fuel capacity / range) at **95% coverage** — 230/242
> spaceships, the 12 missing being drive-less snubs — and
> `/api/vehicle-items?filter[type]=QuantumDrive` supplies the drive catalog for
> the override picker. CC BY-SA 4.0 with attribution. The early datamined
> `data.p4k` extract (~49% coverage — the original blocker) has since been
> **removed from the repo**; the mine-derived sections below (558→140 flatten,
> ~49% coverage) are retained as design history — the wiki's precomputed per-ship
> block supersedes that machinery (see [`quantum-data-pipeline.md`](quantum-data-pipeline.md)).
> Every locked decision, the SCU/Gm fuel math, and the committed-JSON output
> shapes are unchanged; only the source swaps.

Source data comes from the **SC Wiki API** (backlog #26), fetched and distilled
into committed `poi/*.json` — see [`quantum-data-pipeline.md`](quantum-data-pipeline.md):
- `/api/vehicle-items?filter[type]=QuantumDrive` — the quantum-drive catalog
  (size, fuel requirement, drive speed, cooldown, spool).
- `/api/vehicles` — per-ship `quantum` block (fuel capacity, range, default
  drive) at **95% coverage** (230/242 spaceships).

Companion pipeline notes: [`quantum-data-pipeline.md`](quantum-data-pipeline.md).

---

## What & why

Today the planners know only **SCU capacity**. Distance is exact (meters, incl.
hazard detours) but there is **no fuel model and no range constraint** — the only
physical limit reaching either solver is `usable_scu`. This feature adds:

1. **Fuel usage** — per-leg quantum-fuel burn (SCU) and a route-level total, from
   the selected ship's active quantum drive.
2. **Max jump range** — per-leg flag when a leg exceeds what the ship can cover on
   a full tank, shown as an advisory warning, with an **opt-in checkbox** that
   turns it into a hard solver constraint ("only in-range routes").
3. **Drive-override picker** — pick any size-compatible drive to see the
   range/fuel tradeoff live; defaults to the ship's stock drive.

### Decisions locked (don't relitigate)

- **Drive selection = default + override picker.** The mined data carries the full
  size-compatible drive list *with per-ship range* for every ship, so a picker is
  free data-wise. Ship stock drive is the default; the user can override.
- **Max-range = advisory by default + opt-in "in-range only" checkbox.** Over-range
  legs are rare inside Stanton and refueling mid-route is realistic, so the default
  is a non-blocking warning + fuel figures. A checkbox makes it a hard constraint
  for players who want guaranteed single-tank legs (small tanks, Pyro runs).
- **Unmatched ships degrade gracefully.** Only ~49% of cargo-capable planner ships
  have a mined quantum profile (see Coverage). Ships without one show *no* fuel/range
  UI and the checkbox is a no-op for them — **no fabricated numbers**. Planning works
  exactly as it does today.

---

## The units (the load-bearing fact)

- nav_core distances are **exact meters** end-to-end (`nav_core.py:4-10`,
  `dist3` at `447-448`); `travel_cost.distance_m` already folds in hazard detours
  (`nav_core.py:1856-1857`).
- Mined `fuelReq` is **SCU per gigameter** (Gm = 1e9 m). Proof: `fuelCapacity /
  fuelReq == maxRange_Gm` for every row (e.g. Avenger Stalker 1.1 / 0.0049 = 224.49).
- So the entire model is two one-liners:

  ```
  leg_fuel_scu = distance_m / 1e9 * fuel_req        # SCU burned on the leg
  max_range_m  = (fuel_capacity_scu / fuel_req) * 1e9   # == maxRange_Gm * 1e9
  over_range   = distance_m > max_range_m           # can't make it on a full tank
  ```

  Because `distance_m` is post-detour, fuel over a re-routed path is automatic.

**Refuel model:** planner stops are stations/terminals where quantum fuel refills,
so the range constraint is **per-leg** (stop→stop continuous jump), not cumulative.
The route-total fuel is an informational sum, not a hard budget.

---

## Data: flattening 558 variants → 140 ship profiles

> **Superseded (design history).** This section and the Coverage section below
> describe the removed datamined mine. The wiki API's `/api/vehicles` already
> returns one precomputed `quantum` block per ship (95% coverage), so the
> variant-flattening and name-matching machinery here no longer applies — kept
> only to document how the numbers were originally derived and cross-checked.

The 558 records are mostly NPC/AI/mission/skin **variants of the same hull**. Verified:
- 558 records → **140 distinct `ship_name`s**.
- Across all variants of a name the quantum fingerprint (size, fuel capacity,
  max range, full drive list) is **identical** — the *only* exception is **Aegis
  Gladius**, where some NPC variants ship a different *stock default drive* (Beacon
  vs Expedition); capacity/range/drive-list are still identical.
- NPC/non-player id markers: `_pu_ai_`, `_ea_ai_`, `_ai_`, `_unmanned_`, `_pir`,
  `_van` (Vanduul), `_hijacked`, `_boarded`, `_swarm`, `_showdown`, `_tutorial`.
  "Clean" ids (`manufacturer_hull`) are the player base ships. Cosmetic/event ids
  (`_bis2950`, `_fleetweek*`, `_piano`, `_nointerior`) are quantum-identical skins.

**Flatten rule:** group by `ship_name`; the canonical profile = the record with the
**fewest id segments** (tie-break shortest, then lexical). This lands the true player
base wherever one exists (`aegs_gladius` → Beacon default, `aegs_eclipse` over
`aegs_eclipse_bis2950`, `aegs_vanguard` over `aegs_vanguard_pu_ai_civ`).

**Default-drive caveat (5 player ships):** Origin 300i / 315p / 325a / 350r and RSI
Mantis exist in the mined data *only* as NPC variants — no clean base was captured.
Their capacity/range/drive-list are valid; only the *stock default* may reflect an
NPC loadout. Moot once the drive-override picker is used; the distill step tags these
with `default_from_npc: true` so the UI can (optionally) note it. Pure-NPC display
names ("Aegis Gladius Pirate", "Vanduul Blade/Glaive", "RSI Bengal Carrier") never
match a purchasable uexcorp ship, so they're harmless.

---

## Coverage (be honest about the gap)

The planners select ships from the **uexcorp** catalog (`/api/ships`, keyed by
`name_full`); the mined data is keyed by internal ship id. A best-effort matcher
(normalized name + edition-strip + manufacturer-slug) resolves **~60 / 126** planner
ships (**~49% of cargo-capable haulers**). The gap is **not** a matcher bug — popular
haulers (Cutlass Black, Hull B/D/E, Corsair, MSR, C1 Spirit, Zeus, Aurora, Carrack,
Apollo) simply have **no entry in this mining pass**. Matching is done **offline in
the distill step** so aliases can be hand-curated and runtime stays deterministic.
Re-mining those hulls later is the way to raise coverage; the code path needs no
change (drop in an updated `quantum_profiles.json`).

---

## Data pipeline & committed artifacts

A sync/distill script (committed, run offline) fetches the SC Wiki API feeds
(`/api/vehicles` + `/api/vehicle-items?filter[type]=QuantumDrive`) and reads the
local `poi/ships.json` uexcorp cache, emitting two **committed** artifacts
(un-gitignored, like `poi/poi.json`):

### `poi/quantum_drives.json` — global drive catalog (~63 rows, minus templates)
```json
{ "QDRV_JUST_S01_Colossus_SCItem":
  { "name": "Colossus", "size": 1, "fuel_req": 0.005488,
    "drive_speed": 257000000, "cooldown_s": 21, "spool_s": 4 } }
```

### `poi/quantum_profiles.json` — 140 flattened per-ship profiles
```json
{ "aegs_avenger_stalker":
  { "ship_name": "Aegis Avenger Stalker", "qd_size": 1,
    "fuel_scu": 1.1,
    "default_qd": "QDRV_TARS_S01_Expedition_SCItem",
    "default_from_npc": false,
    "max_range_m": 224490000000,
    "drives": [
      { "qd": "QDRV_TARS_S01_Expedition_SCItem", "name": "Expedition",
        "fuel_req": 0.0098, "range_m": 112240000000, "is_default": true },
      { "qd": "QDRV_ACAS_S01_LightFire_SCItem", "name": "LightFire",
        "fuel_req": 0.0049, "range_m": 224490000000, "is_default": false }
    ] } }
```
- `range_m` per drive = `maxRange_Gm * 1e9` (ship-specific: fuel_scu / fuel_req).
- `drives` sorted default-first, then by range descending.
- A committed `alias` map (offline) records curated uexcorp-name → profile-id fixes.

The distill script also writes a small `poi/quantum_match_report.txt` (matched /
unmatched planner ships) as a build artifact for eyeballing coverage — not loaded at
runtime.

**Why offline + committed (not runtime mining):** quantum data only changes when CIG
patches drives/ships, which requires re-mining anyway. Baking the match offline keeps
runtime deterministic, avoids shipping a 6.4 MB JSON, and lets us hand-patch aliases.

---

## nav_core additions (pure, unit-testable)

Keep nav_core provider-agnostic: it receives plain numbers, never the profile object.

### A. Fuel helper
```python
def leg_fuel_scu(distance_m, fuel_req):
    # fuel_req is SCU per Gm; distance in m. None fuel_req -> None (unknown ship).
    if fuel_req is None or distance_m is None: return None
    return distance_m / 1e9 * fuel_req
```

### B. Leg annotation
Every leg dict that already carries `distance_m` gains, when a drive is known:
- `fuel_scu` — `leg_fuel_scu(distance_m, fuel_req)`
- `over_range` — `max_range_m is not None and distance_m > max_range_m`

Applied in the cargo `renderStop`/leg build path and in `_cost_route` /
`cost_trade_legs` / `replan_trade_route` per-leg construction.

### C. Route summary
Both solvers' result summaries gain (all `None`/absent when drive unknown):
- `total_fuel_scu` — sum of per-leg `fuel_scu`
- `over_range_count` — number of legs with `over_range`
- `worst_leg_m` — longest single leg (to compare against `max_range_m` in UI)

### D. `in_range_only` constraint threading
New keyword on the solvers: `fuel_req=None, max_range_m=None, in_range_only=False`.

- **Cargo `plan_route`** (`nav_core.py:2059`): the tour must visit *all* stops, so
  a leg over range makes an *ordering* infeasible. Add the range test alongside the
  existing capacity guard in `_bnb_order`/`_greedy_order` (`nav_core.py:2241`): when
  `in_range_only and max_range_m and dmat[i][j] > max_range_m`, that transition is
  forbidden. If no ordering satisfies it, `plan_route` returns its normal shape with
  `range_infeasible: true` and an empty/failed order (frontend shows the message).
- **Trade `_cost_route`** (`nav_core.py:2748`): trades are *selective*, so an
  over-range approach or haul leg simply makes that trade unusable — when
  `in_range_only`, treat such a leg like the existing "haul unroutable"
  infeasibility (`distance_m is None`, `nav_core.py:2772`) and skip the trade.
- `cost_trade_legs` (manual) and `replan_trade_route` route through `_cost_route`,
  so they inherit the behavior; manual mode still *annotates* over-range legs even
  when `in_range_only` is off.

No change to `travel_cost` itself — it stays a pure distance/geometry function.

---

## app.py changes

### Load (startup, near `ships = load_ships()` at `app.py:814`)
```python
QUANTUM_DRIVES   = _load_json(DATA_DIR / "quantum_drives.json")     # {qd: {...}}
QUANTUM_PROFILES = _load_json(DATA_DIR / "quantum_profiles.json")   # {ship_id: {...}}
QUANTUM_BY_NAME  = _index_quantum_by_name(QUANTUM_PROFILES)         # norm(name)->profile
```
`_index_quantum_by_name` builds the normalized-name/edition-strip/alias index used to
map a uexcorp `name_full` → profile. (The heavy matching already happened offline;
this is the light runtime lookup + curated aliases.)

### `/api/ships` enrichment (`app.py:2135-2139`, via `load_ships` trim `360-368`)
Attach a `quantum` sub-object to each ship when matched, else omit:
```json
"quantum": { "fuel_scu": 1.1, "default_range_m": 112240000000,
             "max_range_m": 224490000000, "default_from_npc": false,
             "drives": [ { "qd": "...", "name": "Expedition",
                           "range_m": 112240000000, "fuel_req": 0.0098,
                           "is_default": true }, ... ] }
```

### Models
- `RoutePlanIn` (`app.py:1073`): add `ship: str | None = None`, `qd: str | None =
  None`, `in_range_only: bool = False`. (Cargo currently sends no ship in the plan
  body — it's added only on `startRun`; now the plan body carries it for fuel/range.)
- `TradePlanIn` (`app.py:2212`) already has `ship`; add `qd: str | None = None`,
  `in_range_only: bool = False`. Same two fields on `TradeReplanIn`.

### Resolve helper (shared)
```python
def _resolve_drive(ship_name, qd_key):
    """-> (fuel_req, max_range_m, resolved_qd) or (None, None, None)."""
    prof = QUANTUM_BY_NAME.get(_norm(ship_name or ""))
    if not prof: return (None, None, None)
    drives = {d["qd"]: d for d in prof["drives"]}
    d = drives.get(qd_key) or drives.get(prof["default_qd"]) or prof["drives"][0]
    return (d["fuel_req"], d["range_m"], d["qd"])
```
Call it in the plan/replan handlers and pass `fuel_req`, `max_range_m`,
`in_range_only` into `plan_route` / `plan_trade_route` / `cost_trade_legs` /
`replan_trade_route` (`app.py:2768-2772`, `2427-2439`). `max_range_m` here is the
**per-drive** range (`d["range_m"]`), not the profile's best-drive `max_range_m`.

Guardrails: `qd` is validated against the resolved ship's drive list (unknown →
falls back to default, never errors); no new external calls; no cap changes.

---

## Frontend changes (`server/static/index.html`)

Two parallel planners with duplicated ship pickers (`attachShipPicker` `5928` /
`attachTradeShipPicker` `7106`). Add the same three pieces to each; a shared helper
is optional but the pickers stay separate to limit blast radius.

### 1. Ship panel — drive picker + in-range checkbox
- Cargo SHIP panel (`index.html:2699-2714`), Trade SHIP panel (`2790-2808`):
  add a `<select class="ti" id="route-qd">` / `#trade-qd` populated from the picked
  ship's `quantum.drives` (default-first, label `"Expedition · 112 Gm"`), and a
  `<label><input type="checkbox" id="route-inrange"> Only in-range routes</label>`.
- On ship pick, if `ship.quantum`: fill the drive select, show the ship's default
  range next to SCU; else hide/disable both controls (graceful degrade).
- Persist the chosen drive with the ship: extend `savedScuFor`/`rememberShip`
  (`5956`/`5969`) and `ShipPrefIn` + `user_ships` with a nullable `qd` column
  (small migration), so "remember" stores drive too. (localStorage is the fallback
  if we want to avoid the migration — decide at build time; server-side is tidier.)

### 2. Plan request body
- Cargo (`index.html:6235`): add `ship: $("route-ship").value`, `qd:
  $("route-qd").value || null`, `in_range_only: $("route-inrange").checked`.
- Trade `buildTradePlanBody` (`7275`): add `qd`, `in_range_only` (ship already sent).

### 3. Render — per-leg fuel, route totals, range warning
- **Route summary strips** — cargo `sub` metrics (`6352-6355`), trade `sub`
  (`7365-7370`): add a `FUEL` metric (`fmtScu(total_fuel_scu)`), only when present.
- **Per-leg fuel** — cargo `.route-leg` line (`6649-6659`), trade `.tl-move`
  (`7449`): append `· 0.42 SCU fuel`; when `over_range`, add a `⚠ over range`
  span (reuse the danger accent).
- **Range warning callout** — model on the existing trade `route-reroute`/
  `route-danger` callouts (`7386-7393`) and cargo capacity gauge (`6357-6361`):
  a route-level `.route-range` line when `over_range_count > 0` ("N leg(s) exceed
  the <drive> range — refuel or pick a longer-range drive"), or when
  `range_infeasible` ("No in-range route with this drive — uncheck 'only in-range'
  or switch drives").
- New formatter `fmtScu(x)` beside the existing `fmtGm` distance formatter.

No new views; no bundler; CSP/nonce untouched.

---

## Tests (`server/test_nav_core.py`)

- `leg_fuel_scu`: unit conversion (1 Gm @ fuel_req → fuel_req SCU); `None` passthrough.
- `max_range_m` identity: `fuel_scu / fuel_req * 1e9 == range_m` from a fixture profile.
- Leg annotation: a leg gets `fuel_scu` and correct `over_range` boundary (just under
  / just over `max_range_m`).
- Summary: `total_fuel_scu` = sum of legs; `over_range_count` correct.
- `in_range_only` cargo: a two-stop plan whose only ordering has an over-range leg
  returns `range_infeasible`; a feasible drive does not.
- `in_range_only` trade: an over-range trade is dropped from the selected route;
  with the flag off it's kept but annotated `over_range`.
- Unknown ship (`fuel_req=None`): no fuel fields, no range filtering, plan unchanged.
- A distill-output smoke test (optional): 140 profiles, every `drives` non-empty,
  `range_m == round(fuel_scu/fuel_req)*1e9` per drive.

---

## Build order (bottom-up)

1. **Distill script + committed artifacts** (`quantum_drives.json`,
   `quantum_profiles.json`, match report). Un-gitignore the two JSONs.
2. **nav_core**: `leg_fuel_scu`, leg annotation, summary fields, `in_range_only`
   threading + tests. (Pure — fully testable before any wiring.)
3. **app.py**: load + name index + `/api/ships` enrichment + model fields +
   `_resolve_drive` + solver wiring. (Optional `user_ships.qd` migration.)
4. **Frontend cargo `#/route`**: drive select, checkbox, body fields, render.
5. **Frontend trade `#/trade`**: same, reusing the cargo pieces.
6. `/deploy`.

---

## Open items / tuning (defaults given)

- **`user_ships.qd` persistence vs localStorage** — recommend the DB column (matches
  the existing `remember` pattern); localStorage is the no-migration fallback.
- **`default_from_npc` surfacing** — v1 can ignore the flag entirely (drive picker
  makes it moot); optionally show a subtle "NPC-config default" note for the 5 ships.
- **Refuel time/cost in aUEC/hr** — out of scope for v1. The fuel-SCU figure is the
  hook; folding refuel dwell + cost into the trade `per_hour` score is a later pass.
- **Cross-system legs** — `travel_cost` models the gate tunnel as a fixed
  `GATE_TRAVERSAL_M` cost; fuel is computed over that same `distance_m`, which is a
  reasonable approximation (real gate transit fuel is separate in-game). Flag as a
  known simplification, revisit if it matters.
- **Raising coverage** — re-mine the ~50% missing hulls; drop in an updated
  `quantum_profiles.json`, no code change.
