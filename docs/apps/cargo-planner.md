# Cargo Planner

> Pickup-and-delivery route solver for hauling contracts — plan the most efficient multi-stop run under your ship's cargo capacity, then run it turn-by-turn. **Route:** `#/route` · **Launcher group:** Out in the 'Verse

<div align="center">
  <img src="../../images/readme_images/cargo_planner_screenshot.webp" alt="The Cargo Planner: ship and quantum drive, a start point, three package rows with contract labels and their payouts, the danger control, then the PLAN headline (payout, aUEC/hr, stops, QT distance, time, fuel, peak load) and the STOPS list visiting Baijini Point at both ends of an A↔B round trip, with ▲ load / ▼ drop lines, contract chips and ✓ completes" width="820">
  <br>
  <sub>Plan output: feasibility summary, ordered stops with per-leg detail, and the run/clear actions.</sub>
</div>

## What it is

You've picked up a handful of hauling contracts — each one a commodity, an
amount, a pickup, and a dropoff. Star Citizen never tells you the *order* to
run them in, or whether they even fit in your hold together. Work it out by
hand and you end up backtracking across a system, or loading a stack that
overflows your grid halfway through the run.

Cargo Planner takes the pickups and dropoffs straight off your contract
screen and solves for the best visiting order under your ship's real cargo
capacity — respecting that a package can't be delivered before it's picked
up, and that your hold can never carry more than it holds. It tells you up
front whether everything fits and what the run costs in quantum distance,
time and fuel, routes around reported pirate activity, then walks you stop by stop with live arrival detection off your own
in-game position, so you always know where to go next and what to load or
drop when you get there.

It's a planner, not a market tool — it doesn't find or price contracts for
you (that's the in-game contract manager and UEX). It picks up once you
already have contracts in hand and turns them into a route.

## How to use it

### Set up the run

1. Open **Cargo Planner** from the launcher (`#/route`).
2. Under **SHIP**, type your ship's name to prefill usable SCU from the
   catalog. Override it if what actually fits differs (box-tiling rarely
   matches the stated grid exactly), then `remember` it for next time.
3. If your ship has a known quantum-drive profile, a **quantum drive**
   dropdown appears — pick your equipped drive for live fuel/range figures.
   Check `in-range only` to make the solver reject any leg your tank can't
   cover in one jump (off by default; otherwise it's just a warning).
4. Under **START FROM**, type a station/POI you're at, click `📍 my current
   location`, or leave it blank to let the solver pick the best first stop.
5. Under **PACKAGES**, click `+ add package` per contract line — commodity,
   SCU, `from` → `to` (type-to-search POI pickers), and an optional
   `contract` label. For a contract that gives only a commodity *total*
   across several pickup points, use `+ add multi-pickup delivery` instead:
   enter the total once and list the pickups. Contracts that run both ways
   between the same two places (A→B and B→A) are fine — enter them as they
   are.
6. Once packages carry contract labels, **CONTRACT PAYOUTS (optional)**
   appears — one aUEC reward per contract, which drives aUEC/hour.
7. Leave **Pirate danger** on `Avoid` (default) to detour around active
   Danger Board warnings, or switch to `Warn` / `Ignore`. Under it,
   **AVOIDED LOCATIONS** is your own list of places to always route around
   (shared with the Trade Route Planner).
8. Click `Plan route`. `Clear route` wipes the package rows and starts over.

### Read the plan

**PLAN** leads with the **PAYOUT** and aUEC/hr when you entered rewards
(otherwise **EST. TIME**), then STOPS, PACKAGES, QT DISTANCE, and QT FUEL
when a drive is known. A **PEAK LOAD** gauge shows the fullest your hold gets
against your usable SCU. If the bundle doesn't fit, the plan says so and
names the minimum usable SCU that *would* work, so you know whether to drop
a contract or split it across two trips.

Under **STOPS**, each stop shows its QT marker, distance, and ETA, any "via"
parent-planet hop, any cross-system jump gate (`⇆ …`), and a `dodge via …`
note where the route detours around danger. Each stop lists the `▲ load` and
`▼ drop` lines, colour-coded by contract, a `✓ completes` chip where a
contract is fully delivered, and what's aboard after the stop. On a
two-way contract set, the same place can appear twice in the list — once to
drop, once to pick up — which is the honest order for that run. A stop
reported camped is flagged, and with a drive selected a `⚠ over range` badge
appears wherever a jump would drain more than a full tank.

### Run it

1. From a feasible plan, click `Start this run ▸`. The run persists
   server-side (survives a reload or a phone switch) and sets your live
   destination to the first stop.
2. The rest of the SPA guides you there like the Resource Navigator —
   bearing, distance, ETA, nearest QT marker — off your watcher's
   `/showlocation` feed.
3. On arrival, the stop's package checklist appears. Loading/dropping is a
   manual in-game action (the freight elevator), so the app never
   auto-completes a stop — check off each package as you move it. A checked
   pickup adds its SCU to the live "cargo aboard" readout; a checked dropoff
   frees it.
4. The run card also shows what's **in the hold** (with contract chips to
   match against your boxes) and per-contract delivery progress. If a
   detour was planned, guidance names the waypoint to QT to first.
5. Once every package at a stop is resolved, the run auto-advances. Use
   `skip to next stop ▸` to force an advance, or `abandon` to drop the run.
6. Finishing the last stop shows a **ROUTE COMPLETE** card. `Plan another
   route` clears the form (keeping ship + usable SCU) for the next batch.

### Recent hauls & history

The **RECENT HAULS** panel speeds up entry the more you use the app:
**FREQUENT LANES** — your most-run `from → to` pairs as one-click chips that
add a prefilled row; **LAST RUNS** — recent completed runs with a `clone`
button that refills the whole form; your most-hauled commodities also float
to the top of the commodity picker. A **Session ⇄ Recent** toggle switches
the stat cards between *this play session* (reset with `↻ start new
session`) and your rolling recent-runs window — runs, aUEC/hr, SCU moved, QT
distance, time.

## Features

- **Precedence + capacity solver** — every pickup lands before its dropoff;
  onboard SCU never exceeds usable capacity at any point, computed with
  exact travel distances including the parent-planet two-hop rule.
- **Round-trip contracts** — A→B and B→A deliveries in one bundle plan
  cleanly; the planner visits a place twice when that's what the cargo
  needs.
- **Jump gates** — cross-system stops route through the right gate, and the
  crossing counts as travel time rather than distance. A gate whose
  position isn't known is shown as `(approx)`.
- **Multi-pickup deliveries** — a commodity total across several unlabeled
  pickup points, without guessing a split; plans conservatively so it never
  under-budgets capacity.
- **Quantum fuel & range overlay** — for ships with a known drive profile:
  per-leg fuel burn, a fuel total, an advisory over-range warning, and an
  opt-in hard `in-range only` constraint. Unmatched ships plan as before —
  no fabricated numbers.
- **Danger detours** — `Avoid` detours around Danger Board warnings without
  ever dropping a contracted stop; `Warn` flags stops reported camped; your
  AVOIDED LOCATIONS list applies on every plan.
- **Live start position** — a chosen POI, your live position, or the
  solver's own best-first-stop pick.
- **Turn-by-turn run mode** — the navigator's live guidance loop, arrival
  detection, a per-package checklist, live cargo-aboard readout. Runs
  persist per-member, so a restart or device switch resumes where you left
  off.
- **Reward tracking** — per-contract aUEC payouts summed into a run total
  and aUEC/hour, on the plan summary and your run history.
- **History & quick-picks** — every run logged; frequency-ranked lanes,
  commodities, and ships surface as shortcuts, and any run can be cloned.
- **Guild hauling analytics** — `GUILD HAULING` (`#/cargo-leaderboard`) and
  `GUILD HAULING OVERVIEW` (`#/cargo-stats`), off Org Intel: top earners and
  aUEC/hr leaderboards, guild totals, top commodities / lanes / ships.

## Works with the rest of the suite

Route planning honors the **Danger Board** (`#/pirates`): an `Avoid`-mode
plan detours around a live threat automatically, and `Warn` flags a stop
reported camped. The AVOIDED LOCATIONS list is shared with the Trade Route
Planner. Ship/commodity/quantum-drive data ride on the same reference feeds used
elsewhere in the suite, and completed runs feed both your `#/route` history
and the guild-wide hauling boards under **Org Intel**.

## Tips

- `remember` your usable SCU per ship once — it prefills every future plan
  for that ship, no re-entry needed.
- If a bundle comes back infeasible, check the **min capacity** figure: it's
  exactly how much SCU you'd need, so you know whether to drop a contract or
  split the run.
- Turn on `in-range only` before a Pyro run on a small-tank ship — the
  difference between a warning you might miss and a route guaranteed to
  never strand you mid-jump.
- A multi-pickup group holds its entire declared total onboard from first
  pickup to drop — plan your usable SCU with that margin in mind.

---
<sub>Part of the <a href="./README.md">SC Org Navigator app suite</a>. Design/reference spec: <a href="../cargo-hauling-planner.md">docs/cargo-hauling-planner.md</a>.</sub>
