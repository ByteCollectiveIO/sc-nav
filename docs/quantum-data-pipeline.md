# Quantum data pipeline — fetch → distill → committed artifacts

Companion to [`quantum-fuel-range.md`](quantum-fuel-range.md). Describes how the
**SC Wiki API** (backlog #26) becomes the two small files the server loads, so a
per-patch refresh is reproducible.

> **History (2026-07-04):** this pipeline was originally scoped against a
> datamined `data.p4k` extract (~49% ship coverage). That raw mine has since been
> **removed from the repo**; the SC Wiki API replaced it as the source (same
> fetch → distill → committed-JSON convention, 95% coverage) and the mine's
> re-mine/cross-check role is retired. The units fact and output shapes below are
> unchanged — only the inputs swapped from local CSVs to API calls.

## Inputs (SC Wiki API, fetched offline, NOT called at runtime)

Base `https://api.star-citizen.wiki`, public, no auth for game data, license
**CC BY-SA 4.0 with attribution** — use **English fields only**. Stamp the output
with `GET /api/game-versions/default` (currently `4.8.2-LIVE.12030094`).

- **`/api/vehicle-items?filter[type]=QuantumDrive`** — the quantum-drive catalog.
  Per drive: `quantum_drive.fuel_rate` (SCU/m), `quantum_fuel_requirement`,
  drive speed, spool/calibration/cooldown, size/grade/class.
- **`/api/vehicles`** (290 ships, 2 pages @ 200) — per-ship, precomputed:
  `quantum_speed`, `quantum_fuel_capacity`, `quantum_range` (meters), spool time,
  reference-trip fuel/time. **Coverage 230/242 spaceships (95%)**; the 12 missing
  are drive-less snubs (MPUV, Merlin, Fury…) — correctly absent, not a gap.

Because `/api/vehicles` already returns a resolved `quantum` block per hull, there
is no variant-flattening or internal-id matching to do — the earlier mine's
558-records-→-140-hulls step is gone.

## Key facts (carried over from the design, still verified)

- `fuel_req` is **SCU / gigameter**. Identity: `fuel_capacity_scu / fuel_req ==
  max_range_Gm`; 1 Gm = 1e9 m; nav_core distances are meters. This drives the
  per-leg `leg_fuel_scu` math in [`quantum-fuel-range.md`](quantum-fuel-range.md).
- The wiki's `quantum_range` (meters) equals `quantum_fuel_capacity / fuel_rate`,
  so the precomputed per-ship range and the per-drive `fuel_rate` are consistent —
  the distill can carry both without recomputation drift.

## Distill (`sync_quantum.py`, committed, run offline)

1. **Fetch + cache** the two endpoints (paginate `/api/vehicles`); keep a raw
   snapshot for auditing.
2. **Drive catalog** → `quantum_drives.json`
   `{ qd: {name, size, fuel_req, drive_speed, cooldown_s, spool_s} }`, keyed by
   the drive's item ref.
3. **Ship profiles** → `quantum_profiles.json`, keyed by a stable ship id:
   `{ ship_name, qd_size, fuel_scu, default_qd, max_range_m,
      drives:[{qd, name, fuel_req, range_m, is_default}] }`.
   - `range_m` per drive = `fuel_scu / fuel_req` (= the wiki `quantum_range` for
     the stock drive); `drives` sorted default-first, then range descending.
4. **Match to uexcorp** (`poi/ships.json` cache, filter `is_spaceship & scu>0`)
   so `/api/ships` can attach the profile by `name_full`. Strategy, first hit
   wins: normalized `name_full` == profile `ship_name`; edition-stripped name;
   slug; curated `ALIASES` dict. Emit `quantum_match_report.txt` (matched /
   unmatched planner ships) as a build artifact.

## Outputs (committed, loaded at runtime; un-gitignore these)

- `poi/quantum_drives.json` — drive catalog.
- `poi/quantum_profiles.json` — per-ship profiles.
- `poi/quantum_match_report.txt` — coverage audit (build artifact; not loaded).

Runtime uses only the two JSONs. `app.py` builds the normalized-name index +
applies `ALIASES` at load (light lookup; the heavy matching already happened
here). Keep the runtime index consistent with the distill matcher (share the
helpers or mirror them exactly).

## Refreshing (per CIG patch)

Quantum data shifts when CIG patches drives/ships. To refresh: re-run
`sync_quantum.py` against the current game version, commit the regenerated
`poi/quantum_*.json`. **No server code change** — the loaders and solvers are
data-driven.
