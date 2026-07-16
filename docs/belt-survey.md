# Belt survey — crowd-sourced field mapping (backlog #36) — design plan

**Status: ✅ BUILT 2026-07-16 (same day as the design), browser-verified via
the headless preview harness (mark → live pocket → keeger plan with 635 m
miss → in-pocket verdict, end-to-end); NOT in-game verified.** Build notes /
deviations:
- Shipped as designed: Keeger region (`keeger_contains`, locate/chip/capture
  annotation, panel copy), survey marks (`custom_pois.survey` JSON column,
  `Poi.survey`, capture payload normalization, negatives first-class),
  two-tier fit (`survey_pockets` live from mark #1 w/ merge clustering +
  negative-capped extents; `survey_field_model` gated at 25),
  `HaloPlanIn.belt` glaciem|keeger (SVY-* keys also pin directly),
  `GET /api/halo/survey` + `/export` (versioned `_meta`),
  `POST /api/admin/survey/clear`, ⛏ survey block in the AFTER-THE-DROP
  panel (ws-resolved ARMED → refresh), GLACIEM|KEEGER belt seg + progress
  line + export link, org-survey plan badges, Keeger dashed ring + surveyed
  dots on the map, keeger/keeger_pocket verdicts.
- **§3.3 corrected during build: Keeger IS a system-disambiguation rung** —
  guarded by the fresh-sticky ladder like the other belts (which postdates
  the original "no rung" reasoning). Caught live in the preview: a hint-less
  session's first-ever fix at the rocks (watcher booted while parked there)
  stamped its mark "Stanton" via the nearest-container guess — physically
  absurd at 48 Gm (Stanton's outermost body orbits 28.9 Gm) — and the mark
  vanished from the Nyx map. Regression tests pin the fix.
- **Solver honesty guard the design missed:** `POCKET_MISS_CEILING_M`
  (100,000 km). With ~9 Nyx markers, chord coverage of the 48 Gm annulus is
  sparse — a deep-belt mark far from every marker chord has NO honest drop
  plan (best "plans" missed by 0.4–25 Gm in testing). Candidates beyond the
  ceiling are dropped; when nothing survives, the plan 400s with the
  contract-marker explanation instead of emitting a multi-Gm "drop" card.
  The plannable sweet spot is real and matches how miners fly: **rocks on
  station approach chords** (the demo mark 0.8 Gm out from People's Service
  Station Alpha planned at 635 m miss, staged from Levski).
- Deferred from §5: the navigator capture form got no survey chips — typing
  type "survey" there works (defaults to a positive medium-density mark);
  the chip UI lives in the Halo Finder where surveying actually happens. Companion to
`halo-finder-expansion.md` (#35). The idea is the user's: players drop survey
marks while flying unmapped belts (the Keeger Belt first), the app aggregates
them, fits the field's geometry, and feeds the fitted model straight back into
the #35 pocket-mode drop planner — plus an export so a well-surveyed field can
be promoted to committed constants for everyone (the Cornerstone precedent,
industrialized).

---

## 1. The problem (and what changed since #35 shipped)

#35 shipped the Keeger Belt as out-of-scope with "not physicalized — zero
containers in the data dump." That was **wrong in an important way**
(corrected 2026-07-16):

- The wiki API's live-game-data page for the Keeger Belt carries real content
  tables under a `HPP_Nyx_KeegerBelt` harvest-point provider: **ship mining
  ~10%** (Aluminum ore deposits confirmed) and **salvage 0.03–4%** (890/C2
  debris, Reclaimer wrecks). The belt is implemented and lootable.
- The four People's Service Stations form a perfect ring at exactly
  **48.000 Gm, z=0** — they sit *inside* the belt (user-confirmed in-game).
- Keeger contracts spawn **temporary quantum markers inside the belt**
  (user-reported), so players routinely get deep into it.
- What's still missing is **geometry**: zero Keeger containers even in the
  current build's starmap dump (re-verified against the live feed). We know
  the ring's radius; we don't know its width, thickness, or — the Glaciem
  lesson — *where along it the rocks actually are*. Blind drops into a ring
  that might be mostly empty are the exact trap #35's pocket model avoids.

Generalized: any belt CIG ships without container geometry (Keeger today,
future systems tomorrow) has content we can't plan into. But our players fly
these belts anyway — every `/showlocation` fix at a rock is a measurement.
Cornerstone mapped the Aaron Halo with 1,746 hand-taken photo samples; our
watcher pipeline takes the same measurement as a one-button side effect of
normal play.

## 2. The loop

```
mark (⛏ one tap at a rock or an empty spot)
  → target (that mark IS a plannable pocket, org-wide, immediately)
  → refine (more marks merge into clusters; centroids/extents sharpen)
  → model (~25+ marks: ring width/height/coverage — the exportable fit)
  → export (org opts in → reviewed → committed constants for everyone)
```

Contract QT markers are the bootstrap: jump to a contract point deep in the
belt, mark what's around you. No code needs to know about the contracts
themselves — any in-belt fix works, whatever brought the player there.

## 3. Design decisions

### 3.1 A survey mark is a custom POI, not a new entity

The capture flow already does everything hard: arm → next `/showlocation`
fix → POI with exact `global_m`, owner, shard, system (with the #35 belt
annotation). A survey mark is `type="survey"` plus one new nullable JSON
column on `custom_pois` (`survey`, via the house `_ensure_column` pattern):

```json
{"rocks": "none|sparse|medium|dense", "ores": ["Aluminum", "..."],
 "salvage": false, "source": "contract|freeroam"}
```

- **Negative marks (`rocks: "none"`) are first-class.** Coverage boundaries
  are what killed naive Glaciem drops; an empty-spot mark is as informative
  as a dense one. The UI makes "nothing here" one tap, not a failure case.
- Marks are org-visible by default (surveying is a team sport); `private`
  stays available. `ores` reuses the existing ore vocabulary (#32 badges
  render free).
- No new table. Fitted models are **derived, never stored** — recomputed
  from the marks at nav-rebuild time, so they can't go stale against their
  own inputs, and deleting bad marks heals the fit automatically.

### 3.2 One-tap capture lives in the Halo Finder (and the navigator form)

`#/halo` AFTER-THE-DROP panel gains **⛏ Mark survey point**: arms a capture
pre-typed `survey`, with quick chips for density (none/sparse/medium/dense),
ore names (typeahead over the ore vocab), and salvage — no free-text required.
Auto-name (`Keeger survey #N`), auto belt annotation. The navigator capture
form's type picker gains "survey" with the same chips, for marks taken outside
the Halo Finder.

### 3.3 Prerequisite: Keeger becomes a named region

The Nyx belt registry row gains a second region: `keeger` —
`{r_m: 48.0e9, kind: "region"}` (envelope width/height start as generous
constants, replaced by the fit once one exists). Powers: locate verdict
("in the Keeger Belt region — nearest station Alpha, N km"), navigator
`☄ Keeger Belt` chip, capture annotation (so marks self-classify), and the
Nyx panel copy naming both belts. **No system-disambiguation rung** — 48 Gm
z=0 collides with Pyro V↔VI traffic; the fresh-sticky ladder and detected
containers already handle it, and marks are always taken by a session with
live context.

### 3.4 The fit: two tiers — targets are live from mark #1, statistics wait

A rock-positive mark is **ground truth, not an estimate** — a player stood at
that rock and measured it, the same epistemic status as a datamined Glaciem
pocket center. So targeting must never sit behind a sample-size gate; only
aggregate *statistics* need one. `nav_core.fit_belt_survey(marks)` returns
both tiers:

- **Tier 1 — surveyed pockets, live immediately (org-internal).** Every
  rock-positive mark seeds a plannable pocket: centroid = the mark, envelope
  = the default pocket radius. Marks within the merge distance (start
  ~2× `GLACIEM_POCKET_RADIUS_M`, tune against real data) join one cluster
  whose centroid/extent refine with each addition; sample count rides along
  as the confidence badge ("org survey · 1 mark" → "· 9 marks"). Negative
  marks never create targets; inside a cluster's neighborhood they bound its
  extent. Output shape matches #35's pocket dicts, so the solver consumes
  them untouched.
- **Tier 2 — the field model, gated at ~25 positive marks.** Cylindrical
  radius median + p5–p95 radial band, |z| p95 half-height, angular coverage
  fraction. This is what calibrates the Keeger region envelope (locate
  verdicts, chip) beyond the starting constants — and it's the exportable,
  promotable artifact (§3.6). Below the gate the panel shows honest progress
  ("14 rock marks — ~25 needed for a field model"), while tier-1 targeting
  keeps working the whole time.

### 3.5 Feeding the planner

`build_belt_registry` gains a survey pass: tier-1 clusters attach to the
system's row as `surveyed_pockets` (kind `"surveyed"`). The Nyx target panel
offers **GLACIEM RING** (datamined) and **KEEGER BELT (org survey)** —
appearing the moment the org's first rock mark lands; pocket mode plans into
surveyed clusters exactly as it does datamined ones, with the per-cluster
mark count and "org-surveyed, not game data" honesty in the plan card.
Rebuilt on `_rebuild_nav` (mark saves already rebuild nav — captures do
today).

### 3.6 Export (the "give it back" half)

`GET /api/halo/survey/export?system=Nyx` → versioned JSON: every survey mark
(coords, payload, created, app version) + the current fitted model +
attribution line. Member-visible (it's the org's own data). Two consumers:

1. **This project**: a well-surveyed field's fit gets reviewed and promoted
   to committed constants (like `HALO_BANDS`) so every deployment benefits
   without needing its own marks — the maintainer decides when a fit is
   stable enough.
2. **Community**: the export is a citable dataset (our Cornerstone moment).

Import of another org's export is deliberately deferred (trust/dedup
questions); the committed-constants path covers sharing for now.

### 3.7 Staleness and patches

Marks carry `created` timestamps and the app version at capture. Rock spawns
are assumed static per patch (the Aaron survey has held from 3.16.1 through
4.x; server-side spawn *selection* varies, placement volumes don't). Admin
gets **clear survey marks** per system (existing `/api/admin/stats/*/clear`
pattern) for a patch that visibly moves a field; no auto-expiry.

## 4. API

- `POST /api/capture/start` — existing; `kind: "poi"` gains the optional
  `survey` payload (validated shape, caps per house guardrails) when
  `type == "survey"`.
- `GET /api/halo/survey?system=` — marks + fit + progress for the UI layer
  (map dots, progress copy).
- `GET /api/halo/survey/export?system=` — §3.6 document.
- `GET /api/halo/targets` — Nyx row gains `keeger` region + `surveyed_pockets`
  when fitted.
- Admin: `POST /api/admin/survey/clear` (per system).

## 5. Frontend

1. `#/halo` AFTER-THE-DROP: ⛏ Mark survey point + quick chips (§3.2).
2. Nyx target panel: belt picker (Glaciem | Keeger), survey progress line,
   surveyed-cluster plans badged "org survey · N marks".
3. Halo map: survey dots layer (positive = filled, negative = hollow) on the
   system view + inset; fitted cluster arcs on the Keeger ring.
4. Navigator capture form: "survey" type + chips.
5. Export button (Nyx panel, next to the progress line).

## 6. Build order

1. Keeger region row + locate/chip/capture annotation (+ the §1 doc
   correction in halo-finder-expansion.md) — ships alone, useful day one.
2. Survey capture: `custom_pois.survey` column, capture payload, halo/nav
   UI, tests.
3. `fit_belt_survey` + registry pass + synthetic-fixture tests (fit a known
   fake ring from generated marks, incl. negative-mark boundary cases).
4. Planner/panel integration (surveyed pockets) + map layer.
5. Export + admin clear.
6. Docs + in-game shakedown with real Keeger contracts.

Zero new tables; one new column; solver untouched (consumes fitted pockets
as-is). The fit function is the only genuinely new math, and it's ~60 lines
of percentiles and gap-splitting.

## 7. Open questions (answered by the first real survey run)

- How far from a People's Service Station do rocks extend (calibrates the
  region envelope before any fit exists)?
- Do Keeger contract QT markers persist after contract completion (re-usable
  bootstrap) or vanish (marks are the only persistent record — fine)?
- Angular gap threshold: is Keeger clustered like Glaciem (~4% coverage) or
  more continuous? The fit's gap parameter may need one tuning pass.
- Shard variance: assumed none (Cornerstone precedent); the export carries
  shard ids so it's checkable later.
