# Pirate danger warnings & snare-aware routing (backlog #24)

**Status:** v1 FEATURE-COMPLETE 2026-07-04 (build-order steps 1–7 done; only
`/deploy` remains). Backend board + `#/pirates` frontend + impeccable polish
(32→~36/40) + the trade-planner `ignore/warn/avoid` integration are all built and
tested (**349-test suite green**). Grew out of the #21 trade-planner "hazard
markers" v2 fast-follow; supersedes that parking note. v2 (the snare-detour
one-hop reroute) stays parked below.

Star Citizen traders get ambushed two ways: (a) pirates set **quantum snares**
along popular buy→sell lanes to yank anyone flying the direct line out of QT, and
(b) players camp popular destinations and shoot whoever shows up. Both are
*predictable* — the same lanes, the same stations — which is exactly why a
community warning layer is worth building. It defends traders **and** doubles as a
pirate-finder for the org members who like to go hunt the campers (a second
gameplay loop).

This feature lets any member post a time-bound danger warning (around a POI, or
along a lane between two POIs), tag it PVP or PVE, and have the trade planner
optionally avoid or flag the danger. Warnings are community-refreshable and age
off on their own.

---

## Locked scope decisions (2026-07-04)

1. **v1 = board + avoid + warn. The lane detour (route-around) is parked to v2.**
   The straight-line-only travel model (see below) makes detour routing genuinely
   new capability and ~80% of the effort. Ship the board + POI-avoidance +
   warn-badges first; build detours once the board actually has data in it.
2. **Own app + trade widget.** A standalone `#/pirates` board (mirrors the
   `#/lfg` Group Finder) is the pirate-finder/hunter surface; a compact
   active-warnings widget lives inside `#/trade`.
3. **Lane warnings = two anchor POIs + an auto/severity-scaled radius.** Poster
   picks the two endpoints (e.g. Baijini Point + Orison); the system models the
   danger as a capsule around that line. No manual radius dragging — survivors
   posting mid-escape need speed.

---

## The architectural fact that shapes the whole feature

**There is no POI transit graph.** `nav_core.travel_cost(nav, src, dst)` is the
single cost primitive, and it computes:

- **Intra-system:** straight-line 3D Euclidean distance (`dist3`). The *only*
  intermediary hop anywhere is a hardcoded planet→moon two-hop special case.
- **Cross-system:** a 3-node gate BFS over `GATE_LINKS`
  (Stanton↔Pyro↔Nyx) — a graph over *systems*, not POIs.

`travel_cost` has exactly two consumers: `_cost_route` and `_greedy_route`
(nav_core.py). Every trade leg (`buy_poi → sell_poi`) is one straight-line call.

Consequence: **"avoid a POI" is trivial** (drop it from the candidate pool), but
**"route around a lane" is new pathfinding** — there are no waypoints to route
through, so a detour has to be *synthesized*. That is why #5 (the detour) is
isolated into v2.

---

## Data model

New table `pirate_warnings`, shaped like `lfg` (the closest existing analog: a
time-bound, player-posted, community-refreshable, self-ageing record). Keep
`created REAL` as the age-off driver.

```sql
CREATE TABLE IF NOT EXISTS pirate_warnings (
    id            INTEGER PRIMARY KEY,
    poster        TEXT NOT NULL,      -- Discord member id of the reporter
    kind          TEXT NOT NULL,      -- 'point' (around a POI) | 'lane' (between two)
    threat        TEXT NOT NULL,      -- 'pvp' (players) | 'pve' (NPC pirates)
    severity      TEXT NOT NULL,      -- 'sighted' | 'active' | 'deadly' (drives radius + colour)
    system        TEXT,               -- resolved from anchors when available
    anchor_a_poi  INTEGER,            -- endpoint / centre POI id (nav_core Poi.id)
    anchor_b_poi  INTEGER,            -- second endpoint for 'lane'; NULL for 'point'
    location      TEXT,               -- free text, always kept ("Between Baijini and Orison")
    note          TEXT,               -- optional detail ("2x Cutlass + a snare")
    confirmations TEXT,               -- json list of member ids who re-verified
    created       REAL NOT NULL       -- epoch seconds; refresh bumps this
);
```

**Anchors vs. free text (your point 7).** The location field is fed by the
existing free-text-tolerant `attachPoiPicker`, so a survivor escaping a snare can
just *type* "Between Baijini Point and Orison" and the warning lands on the board
immediately. But a warning only becomes **routing-actionable** when it carries
concrete anchor POI id(s):

- `point` needs `anchor_a_poi` → planner can avoid/flag that POI.
- `lane` needs both anchors → v2 can model the capsule and detour.

Unanchored warnings are board-only (still fully useful for the pirate-finder
loop). This is deliberate graceful degradation — never block a fast post on
picking exact POIs.

**DB helpers** (clone the `lfg` set in `db.py`): `_warning_row_to_dict`,
`warnings_all` (seed the in-memory board at boot), `warning_upsert`
(`INSERT OR REPLACE`), `warning_delete(ids)`, `warning_delete_for(poster)`.
Age-off is done in the app layer, not the DB (same as LFG).

---

## Lifecycle (your point 2)

Mirror the LFG time-based lifecycle exactly:

- **Fresh → stale → age-off**, all computed off `created`.
- Two admin org-settings (in `meta`, edited in ORG SETTINGS next to the LFG
  knobs): `warning_ageoff_min` (default **60** — an hour, per your spec) and
  `warning_stale_min` (default **40**, kept strictly below age-off).
- **Refresh = anyone, not just the poster** ("still active" button) → bumps
  `created` to now and appends the confirmer to `confirmations`. This is the
  community-refresh you asked for; it also surfaces "3 people confirmed" as a
  credibility signal.
- `Hub.prune_warnings(now)` sweep deletes entries older than age-off and
  `db.warning_delete`s them (copy `Hub.prune_lfg`).
- `_public_warning(e)` wire shape computes `age_s`, `expires_s`, `stale`,
  `confirm_count` (copy `_public_lfg`).

---

## Backend

**In-memory board on `Hub`** (`self.warnings`), seeded from `warnings_all()` at
boot, mutated on post/refresh/clear, pruned on read — identical shape to the LFG
hub board. Broadcast a `warnings` WS frame on every change (copy
`broadcast_lfg`).

**Endpoints** (mirror the LFG routes):

- `GET  /api/warnings` — board snapshot (+ `announce_available`).
- `POST /api/warnings` — post a warning (`WarningIn` Pydantic model with input
  caps; optional `announce` flag → Discord).
- `POST /api/warnings/{id}/confirm` — community refresh (bump `created`).
- `DELETE /api/warnings/{id}` — clear (poster or admin).

**Discord announce (opt-in).** Add a `"pirates"` category to `notify.CATEGORIES`,
a matching row in the frontend `DISCORD_CATS` array (so the settings UI renders a
webhook field), and a `_notify_warning_posted(pub)` builder mirroring
`_notify_lfg_posted`. Gate with a per-member cooldown (copy `_lfg_announce_ok` /
`LFG_ANNOUNCE_COOLDOWN_S`). Only fires when `announce and
notify.is_configured("pirates") and cooldown-ok`.

---

## Planner integration (your points 3 & 4)

**Toggle in `#/trade`.** Add a three-way control read by `buildTradePlanBody`:
`ignore` (default) / `warn` / `avoid`, threaded into `/api/trade/plan`,
`/api/trade/run`, and the favorites config blob.

- **`avoid` + point warning** → drop that POI from the trade candidate pool.
  One filter in `_trade_candidates`: skip any row whose `buy_poi_id` or
  `sell_poi_id` is in the active-avoid set. This fully delivers **point #4** — an
  avoided station is simply never chosen as a buy or sell stop.
- **`warn`** → plan normally, then annotate any leg whose buy/sell POI sits near
  an active warning with a ⚠ badge on the leg card (reuse the
  `.tl-fresh.stale` / `--warn` visual idiom already on trade legs). No route
  change; just informs.
- **`avoid` + lane warning (v1)** → for now, treat a lane whose *both* endpoints
  match the leg as "avoid that pairing" (drop the row) and warn on the rest. True
  detour insertion is v2.

The active-warning set passed to the solver is just the current board filtered to
anchored, non-stale entries — computed once per plan request.

**Trade-view widget.** A compact panel above `#trade-result` listing active
warnings that touch the planned system(s), each with its stale/age badge and a
"still active?" confirm button — so a trader sees the danger without leaving the
planner.

---

## Frontend — the `#/pirates` board (your point 6)

Clone the `#/lfg` Group Finder stack:

- **Composer panel:** kind toggle (Point / Lane), threat toggle (PVP / PVE),
  severity picker, one POI-autocomplete for `point` or two for `lane` (the
  free-text-tolerant `attachPoiPicker`, exactly like the LFG rally field), a note
  field with a live counter, and a post row with the hidden Discord-announce
  opt-in.
- **The board:** filter bar (threat, severity, system, "touches my planned
  route"), warning cards via a `warningCard(e)` renderer carrying `mine` /
  `stale` modifier classes, `fmtLeft` countdown, `confirm_count` badge, and
  Confirm / Clear actions. Sort deadliest-and-freshest first.
- **Live updates:** WS `warnings` frame replaces the board wholesale and
  re-renders (copy the LFG handler).

This board *is* the pirate-finder: hunters filter to PVP + fresh + high severity
and go engage.

---

## v2 — snare detour routing (your point 5)

**No longer parked: full implementation-ready design in
`docs/snare-detour-routing.md` (2026-07-04).** Headlines: no graph dependency
(intra-system space is a complete graph over QT markers, so a detour is a
bounded waypoint search, ~200 candidates); `travel_cost` gains an optional
`avoid=` volumes param (default None = today's exact path) so both the trade
planner AND the cargo planner inherit it; avoid mode stops dropping snared
lanes and detours them instead; camped destinations flag `blocked`; personal
blacklist + `hazard_radius_km` org setting ride along. The sketch below is the
original parking note, kept for history — the new doc supersedes it.

Documented now so the v1 data model doesn't paint us into a corner (it doesn't —
anchors + severity are already captured).

The snare sits on the **loaded `haul` segment** (buy→sell). "Changing your
approach vector" in-game means jumping to a *different* QT marker and re-QTing in
from another angle — you can't QT to empty space, you jump marker-to-marker. So
the detour is a **bounded one-hop search**, not general graph pathfinding:

1. **Model the lane warning as a capsule:** the segment between `anchor_a` and
   `anchor_b`, plus radius `R` scaled by `severity`. Pure geometry, unit-testable
   in `nav_core` (fits its "pure, fully-tested" ethos).
2. **Conflict test:** a trader's leg conflicts if its straight segment passes
   within `R` of the capsule (segment-to-segment min-distance).
3. **Detour search:** for a conflicting leg, enumerate in-system QT-marker POIs;
   keep those `W` where **both** `buy→W` and `W→sell` clear all active capsules;
   pick the min added-cost `W`; insert it as an extra travel segment. Present as
   "+1 hop to dodge the snare, +N% time."
4. **Thread through the two chokepoints** (`_cost_route`, `_greedy_route`) via an
   avoid/waypoint parameter, so it propagates cleanly through the whole solver.
5. **Live-run reroute:** reuse the `POST /api/trade/run/replan` pattern (already
   re-solves from live position carrying sunk cargo). When a fresh warning lands
   on your *active* leg's lane, offer "reroute around."

Effectiveness is capped by anchor-data quality, which is why it waits until the
board is populated.

---

## Build order (v1)

1. ✅ `pirate_warnings` table + `db` helpers + `WarningIn` model. *(schema)* —
   `db.py` table + `_warning_row_to_dict`/`warnings_all`/`warning_upsert`/
   `warning_delete`/`warning_delete_for`; `WarningIn` in `app.py`.
2. ✅ `Hub` board + prune + `_public_warning` + `broadcast_warnings` + the four
   `/api/warnings*` endpoints (GET / POST / `{id}/confirm` / DELETE). *(backend)* —
   `warning_ageoff_min`/`warning_stale_min` settings (default 60/40), boot re-hydration,
   prune hooked into the presence broadcaster, WS `warnings` frame.
3. ✅ `notify` `"pirates"` category + `_notify_warning_posted` + `_warning_announce_ok`
   opt-in gate. *(discord)* — 26 app tests added (`WarningBoardTests` +
   `WarningAnnounceTests`); suite 332 green.
4. ✅ `#/pirates` app: composer (kind/threat/severity toggles + two free-text-tolerant
   POI pickers) + board (`warningCard`, threat/severity/hide-stale filters) + WS
   `warnings` handler + router entry + launcher card + launcher `☠️ N dangers` badge.
   *(frontend board)*
   - **"⚔ Organize hunt" → event planner (the other side of the loop):** every card
     also promotes the danger into a prefilled CREATE EVENT (`promoteWarningToEvent`
     → shared `eventSeed` → `#/events/new`, reusing `POST /api/events` — same path as
     the LFG promote, which is why `lfgEventSeed` was generalized to `eventSeed`). The
     seed maps threat→category (pvp→PvP, pve→PvE), threat→type (pvp→Combat Patrol,
     pve→Bounty Hunt), the warning's "where"→event_location, and seeds a Combat (Ship)
     roster. So the board both helps traders *avoid* danger and lets hunters *organize
     to go kill it*. No backend change — pure frontend over the existing event API. — then an **impeccable critique+polish pass** (dual-agent,
   snapshot in `.impeccable/critique/`): **32 → ~36/40**. Fixes: added a primary
   `.readout` glance-strip (ACTIVE/DEADLY/PLAYERS counts — the system's signature
   readout), made the severity badge the sole focal read (threat stated once in the
   sub-line), aligned bold weights + the pill radius (12px) to the sibling boards,
   added a `--bad-wash` token (was a hardcoded rgba), a global `::placeholder`
   contrast rule (DESIGN.md), `aria-live` on the post-status, grouped/labeled the
   filter axes, and bumped touch targets to ≥34px. Deterministic detector: 0 in-scope
   findings.
5. ✅ Trade toggle in `buildTradePlanBody`; `_trade_candidates` avoid-filter; ⚠
   warn-badges on legs; the route-level danger callout. *(planner integration)* —
   `ignore/warn/avoid` seg in HOW TO PLAN (`setTradeAvoid`, sent as `avoid_mode`,
   restored by `applyTradeConfig`). nav_core: `_trade_candidates` gained
   `avoid_poi_ids`/`avoid_pairs`, threaded through `plan_trade_route`/
   `replan_trade_route`; new pure helpers `trade_avoid_sets` + `trade_leg_warnings`.
   app: `hub.active_trade_warnings()` snapshot → `_solve_trade_plan`/replan compute
   the avoid sets (avoid mode) and `_annotate_trade_legs` tags touched legs
   (warn+avoid). Manual legs are never dropped (player's choice) but still badged.
   Verified end-to-end: avoid re-routes around a warned POI (2.67M→2.39M, the cost
   of avoidance); warn flags the legs; ignore unchanged.
6. ✅ Frontend `DISCORD_CATS` `"pirates"` row (admins set the webhook in ORG SETTINGS);
   `warning_ageoff_min`/`warning_stale_min` admin inputs (DANGER BOARD settings panel) +
   `/api/settings` support. Launcher-tile logo resized to 700px to match siblings.
7. ✅ `nav_core` unit tests for the avoid-filter + active-warning derivation
   (`TradeDangerAvoidTests`, 11 tests) + app wiring tests (`TradeDangerWiringTests`,
   6 tests). **Full suite 349 green.**
8. ⏳ `/deploy`. **← the only remaining step; v1 is otherwise FEATURE-COMPLETE.**

---

## Relevant code (grep targets — confirm lines, they drift)

- **Clone target (LFG):** `db.py` `lfg` table + `lfg_all/upsert/delete*`;
  `app.py` `Hub.post_lfg/join_lfg/close_lfg/prune_lfg/_public_lfg/broadcast_lfg`,
  `/api/lfg*` routes, `_notify_lfg_posted`, `_lfg_announce_ok`,
  `lfg_ageoff_min`/`lfg_stale_min`; `index.html` `#/lfg` view + `lfgCard`,
  `renderLfgBoard`, `loadLfg`, `postLfg`, `fmtLeft`.
- **Planner hooks:** `nav_core.plan_trade_route`, `_trade_candidates`,
  `_cost_route`, `_greedy_route`, `travel_cost`; `app.py` `_solve_trade_plan`,
  `/api/trade/plan|run|run/replan`; `index.html` `buildTradePlanBody`,
  `renderTradeLeg`, `#/trade` view.
- **POI picker:** `index.html` `attachPoiPicker`; `app.py` `GET /api/pois` →
  `nav_core.search_pois`.
- **Discord:** `notify.py` `CATEGORIES`, `send`; `index.html` `DISCORD_CATS`.
- **Travel model / v2 geometry:** `nav_core.travel_cost`, `dist3`, `GATE_LINKS`,
  `system_path`, `nearest_qt_marker`, `Poi`, `position_start`.
