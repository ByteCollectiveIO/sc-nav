# Starmap & POI enrichment from the SC Wiki API (backlog #28)

**Status: ✅ SHIPPED v0.46.0 (PR #33), confirmed live 2026-07-05** — scoped,
built and released the same day. All three slices + the data layer landed in
one pass; suites 312 nav_core / 202 app green. License: **CC BY-SA 4.0 with
attribution** — the footer attribution line shipped with #26 already covers
it. §9 lists where the build deviated from this spec (all deviations are
strict improvements found in the data). Not yet verified in-browser: the ORG
SETTINGS wiki toggle and the trade-leg amenity chips deserve an eyeball.

**As built:** `tools/sync_locations.py` → committed `poi/locations.json`
(634 records, 4.8.2) · nav_core `add_wiki_pois` (241 wiki-only POIs, ids 4M+,
`source="wiki"`) + `upgrade_qt_markers` (206 matched starmap POIs promoted to
QT destinations — see §9) + `annotate_arrival_radii` (392 POIs, always-on) ·
app.py `wiki_pois_enabled` org toggle + `_apply_wiki_catalog` in
`load_nav_data` + `_arrived_at_active` per-POI threshold (×1.5, 10 km floor) +
`_amenity_view`/`WIKI_AMENITIES`/`_annotate_leg_amenities` on trade legs ·
frontend `wiki-toggle` in ORG SETTINGS + `amenChips` on plan/run leg views.

---

## 1. Supersede or complement? → **Complement. Keep both sources.**

The central scoping question was whether the wiki's location data could replace
the starmap.space POI feed. Measured against Stanton (our cache vs.
`/api/locations/positions?filter[system]=stanton`):

| | starmap.space POI feed | wiki `/api/locations` |
|---|---|---|
| Stanton entities | 1,289 (1,885 all systems) | 809 (1,955 all systems incl. Pyro 291 / Nyx 150) |
| Content character | **community-recorded surface content**: 451 caves, 126 underground facilities, 117 wrecks, 44 rivers, racetracks, mission areas | **datamined game entities**: 460 outposts, comm arrays, stations/clinics, asteroid clusters, Lagrange points |
| QT destinations | 150 `QTMarker==1` (Stanton) | 550 `qt_valid` (Stanton), 171 Pyro, 97 Nyx |
| Name overlap (Stanton) | — | only 423/809 match a starmap POI (+19 match containers) |
| Unique value | caves/wrecks/rivers **absent from the wiki entirely** | ~**299 real QT destinations we don't have** (§3), per-POI QT radii, amenities |
| Containers (bodies) | rotation speed/adjustment — **our calibrated nav math depends on this** | positions only, no rotation params |

Verdict per layer:

- **Containers: starmap only, unchanged.** The wiki carries no rotation
  parameters and our rotation epoch is calibrated against starmap's values.
  Don't touch.
- **POIs: two independent, coexisting import layers.** Neither is a superset:
  starmap's community surface layer (caves/wrecks) doesn't exist in the wiki;
  the wiki's datamined outposts/radii/amenities don't exist in starmap. Orgs
  can enable either, both, or neither.

## 2. Coordinate-frame proof (the make-or-break technical question)

Wiki positions are **system-frame meters snapshotted at rotation epoch** — the
same epoch starmap's body-local frame is defined against. For a surface POI,
`local = (wiki_global − parent_body_center)` reproduces starmap's body-local
rotating-frame coordinates **exactly**: on 412 name-matched Stanton surface
POIs, 407 agree within 5 km in full 3-D (most bodies: longitude offset 0.000°,
radius/latitude identical to the printed digit). The 5 outliers (comm arrays,
Security Post Kareah, Shanks — 235–1,220 km off) are POIs CIG *moved between
game patches*; our starmap cache predates 4.8.2. So:

- Surface POI conversion: `local_km = (global − parent_center) / 1000`, parent
  resolved via `parent_uuid` → container matched **by name**.
- Space POIs (parent = star or none): `global_m` as-is.
- **Do not dedupe by position** — a moved POI would double up. Name is the key.

## 3. What the wiki adds (measured)

- **~299 QT destinations missing from our routable set** (after excluding
  containers, hidden entries, and junk): 190 Stanton (161 outposts —
  Adair's Retreat, Ghost Hollow, distribution centres, comm arrays…),
  97 Pyro (incl. 15 mineable `Cluster XXX-nnn` asteroid clusters, Farro Data
  Centers, Lazarus hubs), 12 Nyx (People's service stations, gateways).
- **Per-POI `quantum_travel` block**: `arrival_radius` / `obstruction_radius` /
  `adoption_radius` (e.g. Everus Harbor 24 km / 8 km / 25 km) — slice b.
- **`amenities`**: Commodity Trading **Freight Elevator vs Loading Dock**,
  Hangar/Pad sizes (`Hangar L`, `Landing Pad M`), Clinic, Docking, Vehicle
  Services, shops — slice c.
- `connections`: jump-point pairs with per-SCU fuel cost (Stanton↔Pyro↔Nyx) —
  matches the gate chain hardcoded in `nav_core`; useful as a validation
  cross-check, not a driver.
- Junk requiring a distill filter: `hidden: true` (363 in Stanton — mission
  interiors), `type: unknown` rows (`jumppointlocationsecurity-000`,
  `jumppointturrets`), `<= uninitialized =>`, and intra-wiki duplicate names.

## 4. Data layer — `tools/sync_locations.py` (the remaining #26 slice)

Same convention as `sync_quantum.py` / `sync_blueprints.py`: fetch → distill →
committed `poi/locations.json`, version-stamped from
`/api/game-versions/default`, **no runtime API calls**, manual re-run per game
patch.

- Fetch: paginated `/api/locations?page[size]=200` (~10 calls for 1,955 rows —
  the **list response already carries `quantum_travel`, `amenities`, `parent`
  inline**, so no per-POI detail calls) + `/api/locations/positions?filter[system]=`
  ×3 for x/y/z + `qt_valid` + `parent_uuid` (join on uuid).
- Distill each kept location to: `uuid`, `name`, `system`, parent container
  name (from the resolved parent chain), `local_km` **or** `global_m` (per §2),
  `qt_valid`, `type`, `quantum_travel` radii (m), curated `amenities` list,
  `block_travel`, `has_resources`.
- Drop: `hidden`, `type` in (`unknown`, `Star`, `Planet`, `Moon`) (containers
  stay starmap-owned), junk-name patterns, duplicate (system, normalized name)
  within the feed itself.
- Emit a `locations_sync_report.txt` build artifact (counts kept/dropped,
  parent-resolution failures) like the quantum match report.

## 5. Slice a — POI import behind an org toggle

**Admin setting `wiki_pois_enabled`, default OFF** — the exact
`starmap_pois_enabled` pattern (`app.py:105`): a new org starts from a blank
POI database and opts into each catalog separately; the ORG SETTINGS panel gets
a second toggle beside the starmap one ("Wiki locations catalog — datamined
outposts, stations & asteroid clusters with QT radii and amenities,
CC BY-SA 4.0"). Flipping it calls `_rebuild_nav()`, same as starmap.
Asymmetry to accept: starmap re-fetches live on restart/refresh; the wiki layer
is a committed snapshot updated per patch via the sync script (deploy = data
refresh). That's the #26 convention working as intended.

**Id namespace**: wiki POIs get ids at **4,000,000+** (`crc32(uuid)`-derived,
stable across syncs) — clear of starmap (<50k), custom (1M+), observations
(2M+), synthesized container-stations (3M+).

**Dedup at load time** (in `parse_data`/a sibling loader, mirroring
`synth_container_pois`' existing name-guard):

1. Normalize: lowercase, collapse whitespace, strip decorative quotes.
2. Skip a wiki POI when its (system, name) already exists as a starmap POI, a
   container, or a synthesized station — **starmap wins** when both have it
   (its type taxonomy drives existing UI filters). Loose containment matching
   for the Lagrange variants (wiki "ARC-L1 Wide Forest Station" vs. synth
   "Wide Forest Station (ARC-L1)").
3. Load order: starmap POIs → synth container stations → wiki POIs, so the
   guard set is complete before wiki rows are considered.
4. When starmap is OFF and wiki ON, wiki records stand alone (dedup then only
   guards containers/synth stations).

Wiki POIs surface everywhere imported POIs already do (search, destination
picker, planners' stop pickers, map), flagged with a `source: "wiki"` so the
POI drawer can attribute them.

## 6. Slice b — arrival & detour radii

Today run-mode arrival is two flat constants (`ARRIVAL_SURFACE_M` /
`ARRIVAL_SPACE_M`, `app.py _arrived_at_active`). With `poi/locations.json`
loaded (independent of the POI toggle — radii are physics metadata, not
content):

- Build a name-keyed crosswalk `(system, normalized name) → quantum_travel`
  and attach `arrival_radius_m` to matching POIs at parse time (matches
  starmap-imported POIs *and* wiki POIs; custom POIs won't match, by design).
- Arrival check prefers the per-POI radius when the destination has one, else
  falls back to the flat constants. Cargo + trade run modes both.
- Detour margins (#24 v2 snare routing) keep the org-wide `hazard_radius_km`
  for *warnings* (that's a threat radius, not a POI property), but
  `obstruction_radius` can pad `_detour_via` clearance around the destination
  itself later — **defer** until the v2.1 two-waypoint fallback work touches
  that code.

## 7. Slice c — terminal amenities in the trade planner

- Crosswalk amenities onto resolved trade terminals (`match_terminals` already
  resolves terminals→POIs by name; extend the resolved record).
- Stop annotations in plan/run views: **⬆ Freight Elevator / 🚚 Loading Dock**
  chip per leg end (the loading-dock/freight-elevator distinction changes how
  players load), clinic/refuel presence in the stop detail.
- **Pad-size-vs-ship warning**: amenities carry the largest hangar/pad size
  (`Hangar L`, `Landing Pad M`); wiki vehicles carry a ship `size` class
  (already fetched by `sync_quantum.py`). Persist ship size into the quantum
  profiles (or a sibling field) and warn on a leg whose stop's max pad <
  ship size. Needs a size-class ordering table (XS<S<M<L<XL) and a probe of
  how station pads are actually annotated — **build last, verify data quality
  first**.

## 8. Phasing & estimates

1. **Data layer** (§4): sync script + committed JSON + report. Independent, no
   app changes. ~a session.
2. **Slice a** (§5): loader + dedup + toggle + settings UI + tests. The main
   slice. ~a session.
3. **Slice b arrival radii** (§6): crosswalk + arrival-check preference + tests.
   Small.
4. **Slice c amenities** (§7): terminal crosswalk + leg chips; pad-size warning
   as a follow-up after data-quality check. Medium.

Open questions parked (not blockers): whether to badge `has_resources` POIs in
the resource-manager finder; whether `connections` fuel costs should feed the
planners' gate-leg fuel estimates (currently drive-based).

## 9. As-built deviations from this spec (2026-07-05)

- **QT-marker promotion (new, the big one).** Most of the "~299 missing QT
  destinations" turned out to exist in starmap as *non-QT* POIs — dedup
  correctly skips them, but they'd stay unroutable. So the import also
  promotes `qt_marker` on matched starmap POIs the game data marks `qt_valid`
  (`nav_core.upgrade_qt_markers`, same derive-don't-edit precedent as the
  Landing Zone rule in `parse_data`). Conservative: only a name that maps to
  exactly ONE loaded POI in its system, container must agree — generic
  repeated names ('Derelict Outpost') never mass-upgrade. Gated by the same
  org toggle. Net with everything on: **241 added + 206 promoted → 508 QT
  markers** (starmap alone: ~150 in Stanton).
- **Frame classification is geometric, not parent-chain.** The wiki's
  `parent_uuid` chain is unreliable (entities on microTech's surface parented
  to Calliope), so containment tests against the *nearest* body. Manmade-family
  types inside a grid are static orbital stations (Everus, Baijini, GrimHEX…,
  eyeballed in the sync report) — except comm arrays, which rotate with their
  body (grid ×1.5 tolerance; CIG nudged some past the cached grid radius).
- **Amenities need per-uuid detail calls**: the paginated list always returns
  `amenities: []`, so the sync fetches `/api/locations/{uuid}` for the ~490
  QT-valid entities (threaded, still a couple of minutes).
- **Sub-locations dropped in the distiller** (parent is another POI, not a
  body/star): station clinics/admin offices aren't routable places and their
  frame would be wrong if station-mounted.
- **Pad-size-vs-ship warning deferred** as anticipated: the uexcorp vehicles
  feed carries no size class (`size: None` on all 279 rows). The chips show
  the stop's max hangar/pad size instead, so players self-check; the warning
  needs ship-size plumbing through `sync_quantum.py` first (fast-follow).
- Arrival threshold: `arrival_radius × 1.5` with a **10 km floor** — QT drops
  the ship *at* the radius (margin keeps drop-out counting as arrived), and
  asteroid clusters carry 100 m radii that would otherwise be far stricter
  than the old flat 50 km.
