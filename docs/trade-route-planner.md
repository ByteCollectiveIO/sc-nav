# Trade Route Planner — design

**Status:** designing (2026-07-03). Backlog #21, previously parked; revisited
with a concrete answer to the parking reason (see *Why revisit now* below).
Nothing built yet — this doc is the decision record we build from.

---

## Background

Commodity trading — buy low at one terminal, sell high at another — is
distinct from the existing [Cargo-Hauling Planner](cargo-hauling-planner.md),
which routes **contract** cargo (fixed pickup→dropoff pairs a mission already
assigned, precedence-constrained, reward is a flat payout per contract). Here
there's no contract: the player picks *what* to buy and *where* to sell it,
and the "right" answer changes as UEX prices update. A player wants:

- What commodities exist, and what terminals buy/sell them.
- Live buy/sell prices per terminal (where's cheap, where's rich).
- The most profitable route for their ship's cargo capacity — buy/sell pairs
  chained into a loop that maximizes aUEC (or aUEC/hour).
- To do this either hands-off (give me a route), semi-directed (I want to
  trade *these* commodities), or fully manual (let me plan leg-by-leg with
  live prices in front of me).
- A visual route like the cargo planner's, live re-routing if a run gets
  interrupted (e.g. pirates), and the ability to re-plan from wherever they
  end up instead of walking back to the original start.

## Why revisit now (the parking reason, answered)

Backlog #21 was parked as "good idea but several non-org-specific tools
already do this — duplicates external sites, isn't org-differentiated."
That's true of a bare price-lookup tool. Three things make it worth building
*here* instead of pointing members at a UEX-alike site:

1. **It already knows where you are.** The watcher feeds live position into
   this same server (`compute_state` / `/ws`). A generic trading site needs
   you to type in your current terminal; we can seed the plan from
   `position_start` for free, and — the actual differentiator — **re-plan
   from your live position mid-run** if you get pulled off course, using the
   exact same guidance loop the cargo planner already drives players with.
2. **It knows your org-mates.** Presence (#19, `who is online`) already
   broadcasts who's doing what. A trade planner that's aware of "two
   teammates are already running Aluminum out of Reclamation & Disposal right
   now" can nudge a third player toward a different lane instead of everyone
   racing the same low-supply/low-demand terminal against each other and
   crashing their own margins — something a generic external tool has no way
   to know. (v2 idea below; not v1, see scope.)
3. **It shares data with the rest of the suite.** Same ship/usable-SCU
   profile as the cargo planner (`user_ships`), same aUEC/hr analytics
   pattern as `#/cargo-stats`, same POI catalog for start/end picking, same
   sign-in — no new account, no tab-switching to a third-party site mid-run.

None of that requires duplicating UEX's own site feature-for-feature; it
means the planner is a thin, org-aware shell over UEX's live price feed,
wired into the same live-position/teammate/analytics fabric as the rest of
the app. If that angle doesn't hold up once built, the honest fallback is
still "a nice, focused pointer to UEX's own trade tool" — but the position
+ presence integration is worth trying first.

## Scope: what's v1, what's deferred

**v1:**
- Live per-terminal commodity prices (new UEX feeds, below).
- All three entry modes from the original notes: auto (lazy), auto with a
  commodity filter, and manual leg-by-leg.
- Route optimization reusing the cargo planner's travel-cost model.
- Ship + usable-SCU reuse from the cargo planner (`user_ships` — no new
  ship picker).
- Start from a picked POI or live position (`position_start`, already built).
- Visual route output, matching the cargo planner's plan/run panels.
- Execute/run mode with recalculate-from-current-position (answers point 8
  in the original notes).

**Deferred (v2+ or parked-within-parked):**
- **Pirate/hazard lane marking + auto-reroute for org-mates** (original
  notes point 4). This is a real feature but a *different* one — a shared
  hazard-marker system with expiry, independent of trading — and deserves its
  own design rather than riding in as a sub-feature here. Flagged, not built.
- **Teammate-lane-awareness** ("someone's already running this lane") — needs
  a way to tell the presence system "I'm actively trading lane X", which the
  playstyle vocab already half-supports (`"trading"` is an existing tag in
  `PLAYSTYLE_TAGS`, `server/app.py:982`) but showing *which lane* is new
  surface. Worth a fast-follow once v1 ships and we see whether double-booking
  is actually a problem in practice.
- True 3D cargo bin-packing (same call as the cargo planner: the scalar
  "usable SCU" abstraction is good enough).
- Quantum-fuel range overlay — **reuse** the cargo planner's decision
  verbatim (advisory only, computed from a CIG drive catalog once that's
  unblocked; not duplicated here).

---

## Data sourcing — the new part

The cargo planner already fetches UEX data (`commodities`, `vehicles`) via
`_fetch_json` + on-disk cache + `/api/refresh` (see `load_raw_commodity_names`
/ `COMMODITIES_URL` in `server/app.py` as the template). That cached
commodities feed is a **global reference price** (one row per commodity —
see `load_commodity_prices`/`_price_map_from_rows`), not a live per-terminal
price. Trading needs the **per-terminal** feed, which is new:

| Feed | UEX endpoint | Gives us |
|---|---|---|
| Terminal prices | `commodities_prices_all` (or per-terminal `commodities_prices`) | buy/sell aUEC per commodity **per terminal**, one row per (commodity, terminal) — mirrors the shape `items_prices_all` already has (`load_item_prices`, one row per item per terminal) |
| Terminals | `terminals` | terminal id, name, and the moon/space-station/city/planet id it belongs to (**no raw x/y/z** — UEX doesn't carry game-file coordinates) |
| Star systems | `star_systems` | id → system name crosswalk for the terminal rows |

Cache + refresh exactly like the existing feeds: `_fetch_json` → on-disk
cache (`poi/trade_terminals.json`, `poi/trade_prices.json`) → loaded at
startup → refreshed by `/api/refresh` → counts surfaced at `/api/health`.

### The hard part: terminal → map location

UEX terminal rows tell you *which station/city/moon* a terminal is in by
UEX's own ids — they don't carry the game-file coordinates our map already
has. But the cargo planner solved this exact problem already:
`nav_core.synth_container_pois` (`server/nav_core.py:224`) synthesizes
directly-QT-able POIs for cargo-relevant station containers (Lagrange
stations, refineries, naval/asteroid bases) by **name-matching** them against
the real game-file container catalog, because they're the same in-game
entities under two different datasets.

Terminals get the same treatment: match each terminal's
`space_station_name` / `city_name` / outpost name against
`nav.containers` / `nav.pois` (case-normalized, same fold-in-the-L-code
trick for Lagrange stations). A terminal whose location doesn't resolve is
excluded from routing (not silently mis-placed) and logged, mirroring how
unmatched containers are simply skipped today. Expect the match rate to be
high — trading terminals are almost entirely at named
stations/outposts/cities, which is exactly the set `synth_container_pois`
already targets — but this needs verifying against a real feed pull before
committing to it as *the* mechanism, since it's the one piece with no
existing precedent to lean on 100%.

### Price freshness

UEX price rows carry their own scrape/update timestamp. Surface it
per-price ("as of Xh ago") rather than pretending it's real-time — prices
are UEX's own polling cadence, not something we control. No staleness logic
needed beyond display; this is advisory data, same spirit as the observation
freshness window (#1) but simpler (no age-off, just a label).

---

## Data model

```
terminal   = { id, name, system, poi_id }          # poi_id via the crosswalk above
price      = { terminal_id, commodity, buy, sell, updated_at }
leg        = { commodity, from_terminal, to_terminal, scu, buy_total, sell_total, profit }
ship       = { name, usable_scu }                  # reused from user_ships, unchanged
```

A **leg** is the trading unit — one commodity, bought at one terminal, sold
at another, for some SCU amount. A **route** is an ordered chain of legs
whose stops merge like the cargo planner's stops do (buy A here, sell A +
buy B at the same terminal, etc.).

---

## Planning modes (all three from the original notes, v1)

1. **Auto / lazy** — ship (+ usable SCU) + start (POI or live position) +
   a stop budget (default ~5, same spirit as the cargo planner's ≤12-stop
   cap) + optional knobs (stay in current system, minimum profit/hour). The
   planner picks commodities *and* the route.
2. **Auto with commodity filter** — same as above, restricted to a
   player-chosen commodity or small set (e.g. "just gold and agricium").
3. **Manual leg-by-leg** — the player picks each buy/sell terminal
   themselves; the tool's job shrinks to showing live prices, running
   profit/SCU-used, and the same visual route rendering as the other two
   modes, with no solver involved. This mode is nearly free once the price
   feed + terminal picker exist — it's the auto modes built as a UI, not a
   UI built on top of the auto modes.

All three reuse one POI-typeahead-with-quick-picks pattern (same as the
cargo planner's from/to pickers) and one commodity typeahead (same
`load_commodity_names` feed already serving the cargo planner — no new
commodity list).

---

## Solver approach — different shape than the cargo planner's

This is the one place the two planners genuinely diverge, worth calling
out explicitly so it doesn't get treated as "the same solver, new data":

- **Cargo planner:** every accepted package *must* be delivered — it's a
  fixed pickup/delivery set under precedence + capacity. Exact
  branch-and-bound is cheap at ≤12 stops because the stop *set* is given;
  the solver only orders it.
- **Trade planner:** nothing is fixed. The solver must *choose* which
  buy/sell pairs to include at all — this is an **orienteering /
  prize-collecting problem** (maximize profit subject to a stop budget and
  capacity), which is a strictly harder shape: the stop set itself is part
  of the search, not just its order.

**Recommended v1 approach — reuse the cheap primitive, use a cheaper
algorithm on top:**
1. Reuse `travel_cost(nav, src, dst)` verbatim for the cost of any leg
   between two resolved terminal-POIs (it's already a pure pairwise
   function, generalizes with zero changes).
2. Rank single-hop buy→sell pairs by profit-per-SCU (or profit-per-hour once
   travel time is folded in) — this is the "best trade" building block and
   is useful standalone (surfaced directly in manual mode as suggestions).
3. Chain a route greedily (nearest-profitable-next, capacity-aware) from the
   top-ranked pairs, then run a small local-search pass (or-opt / swap a
   stop for a better-ranked alternative) bounded by the stop budget — same
   spirit as the cargo planner's "nearest-neighbor seed + 2-opt" fallback for
   its larger-than-B&B cases, applied here as the *primary* method rather
   than the overflow case.
4. Exact search (small B&B over commodity/terminal choices) is only
   tractable for very small stop budgets (~4) — offer it as a "thorough"
   option under a low cap, not the default.

Output per leg mirrors the cargo planner's: distance, ETA, via-planet /
cross-system-gate flags (same gate model, unchanged), plus the
trading-specific numbers: buy/sell price used, profit, running aUEC total,
running SCU used. Route-level summary: total profit, total time, aUEC/hour
— the same metric the cargo planner already tracks per run
(`derive_run_stats`), so this plugs into the same analytics shape rather
than inventing a new one.

---

## Execute layer — reusing the guidance loop, with re-plan from live position

Reuses the cargo planner's Execute pattern almost exactly: the active
stop's POI becomes the session's `destination_id`, so distance/bearing/ETA/
QT-marker guidance is free via the existing `compute_state` / `/ws` loop.
What's different is the **recalculate** path the original notes call out
(point 8 — pirates knock you off-route):

- **"Re-plan from here"** — at any point in an active trade run, the player
  can trigger a re-solve seeded from their **live position** instead of the
  original start. Cheap to do: the solver is stateless and fast (same
  reasoning as the cargo planner's `start_pos` support via
  `nav_core.position_start`).
- **Sunk cargo carries forward.** If the player is currently holding bought
  commodity when they re-plan, that holding (commodity + SCU + what it cost)
  is **not** re-optimized away — it's a constraint on the new plan ("you're
  holding 40 SCU of Agricium bought at X; find the best continuation that
  sells it, plus whatever else fits in the remaining capacity"). This is the
  one real piece of new solver logic beyond the cargo planner's Execute
  layer, and needs its own test coverage once built.
- **Confirm-on-arrival**, same spirit as the cargo planner (arriving ≠
  transacted) — the player confirms the buy or sell actually happened at
  each stop rather than auto-completing.

---

## Persistence (DB)

Mirrors the cargo planner's `runs` table rather than extending it (keeps
blast radius contained — a trade run's blob shape is different: legs, not
packages/precedence):

- **`trade_runs`** — `(id, discord_id, status, ship, started_at,
  completed_at, data)`, `data` = JSON blob of ordered legs + per-leg state
  (`planned → bought → sold`) + the active-stop cursor + running profit.
  Same `active` / `completed` / `abandoned` status convention as `runs`.
- **Ship/usable-SCU** — no new table; reuses `user_ships` verbatim.
- **No new terminal/price tables** — those are refreshed feed caches
  (`poi/trade_terminals.json`, `poi/trade_prices.json`), not per-user data,
  same category as `commodities.json`.

---

## Endpoints (new)

- `GET  /api/trade/terminals` — resolved terminal→POI list (name, system,
  poi_id), for pickers.
- `GET  /api/trade/prices` — cached per-terminal commodity prices (+ as-of
  timestamps), or a `?commodity=`/`?terminal=` filtered slice.
- `POST /api/trade/plan` — stateless: mode (auto / filtered / manual legs),
  ship + usable SCU, start (POI id or live position), stop budget, optional
  commodity filter / system-lock / min-profit-per-hour → ordered legs +
  summary (same shape as `/api/route/plan`'s summary: totals, feasibility).
- `POST /api/trade/run` — start/persist an active trade run.
- `PATCH /api/trade/run` — confirm buy/sell at the active stop; advance.
- `POST /api/trade/run/replan` — re-solve from live position, carrying
  forward any held (sunk) cargo.
- `DELETE /api/trade/run` — abandon.
- `GET  /api/trade/history` — completed runs + frequency-ranked quick-picks
  (best lanes/commodities), same spirit as the cargo planner's
  `derive_quick_picks`.

---

## UI integration

Fifth app in the launcher (after Navigator / Cargo Planner / Event Planner /
Resource Manager / Marketplace — this slots in as a peer, not a fork of
`#/route`). New `#/trade` view family, following the existing
`applyView()` branch pattern — no shell changes needed, the app-launcher
architecture from the cargo planner already generalizes to "N apps."

- **Terminal/commodity pickers** — typeahead, same component pattern as the
  cargo planner's POI/commodity pickers.
- **Mode toggle** — Auto / Filtered / Manual, as three tabs or a single
  form that reveals/hides the commodity-filter and leg-by-leg controls.
- **Plan output** — ordered leg list + summary, visually matching the cargo
  planner's plan panel (reuse the CSS, not just the shape).
- **Run mode** — active leg highlighted, buy/sell confirm controls, running
  profit + SCU-used readout, a "re-plan from here" button front and center
  (this is the point-8 feature from the original notes and should not be
  buried in a menu).
- **History** — best lanes / commodities / aUEC-per-hour, `#/trade-stats`
  mirroring `#/cargo-stats`.

---

## Build order (bottom-up, mirrors how the cargo planner was staged)

1. **Terminal + price feeds** — `GET /api/trade/terminals` (+ the
   name-match crosswalk against `nav.containers`/`nav.pois`, logging
   unmatched terminals) and `GET /api/trade/prices`, cached like
   `commodities.json`. Verify match rate against a real feed pull before
   going further — this is the one piece with no existing precedent to lean
   on completely.
2. **Best-single-trade ranking** — profit-per-SCU / profit-per-hour ranking
   over resolved terminals, standalone and testable before any multi-leg
   chaining exists. Also the first payoff for **manual mode**.
3. **Multi-leg solver** — greedy chain + local search over the ranked pairs,
   reusing `travel_cost`; `POST /api/trade/plan` for auto + filtered modes.
4. **`#/trade` entry → plan UI** — pickers, mode toggle, plan rendering.
5. **Execute + re-plan** — `trade_runs` table, run/confirm/advance, and the
   sunk-cargo-aware re-plan endpoint.
6. **History + `#/trade-stats`** — quick-picks + aUEC/hr analytics, same
   shape as the cargo planner's Learn layer.

---

## Open questions for build time

1. **Stop-budget default** — the cargo planner's B&B cap is ~12 stops
   because the set is fixed; trading's orienteering shape is more expensive
   per stop considered. Recommend a *lower* default (~5) and confirm it
   feels right once real terminal density is visible.
2. **System-lock default** — should "stay in current system" default **on**?
   Cross-system trading legs pay the same gate-traversal cost the cargo
   planner already models, but the profit-per-hour math gets a lot less
   forgiving over a jump-gate hop. Lean default-on, override available.
3. **Teammate-lane-awareness (deferred item above)** — worth a fast-follow
   design once v1 ships and we can see whether double-booked lanes are an
   actual pain point, or theoretical.
4. **Manual mode's relationship to Execute** — does a manual-mode plan get
   to use Run mode too (buy/sell confirm, re-plan), or is manual mode purely
   a "look up prices and eyeball it" tool with no persisted run? Leaning
   toward "yes, manual plans can still be run" — it's the same leg list
   either way, just chosen by hand instead of the solver — but worth
   confirming before building two code paths by accident.
