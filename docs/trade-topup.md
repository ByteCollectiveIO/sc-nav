# Trade planner: mid-run top-up + mixed loads

Status: **PR 0 (#140) + PR 1 built 2026-08-22 · PR 2 built 2026-08-23 — awaiting live test**

## The problem (a real run)

A Drake Ironclad (2,200 SCU usable) planned Silicon out of Baijini Point. UEX
supply capped the plan at 21 × 24 SCU boxes (~504 SCU); the kiosk actually had
14 boxes (336 SCU). Result: a 2,200 SCU ship flew the leg 85% empty.

Two distinct gaps, one per phase:

1. **Plan-time** — the solver plans exactly one commodity per leg
   (`_trade_candidates` emits (commodity, src, dst) rows). When supply caps the
   best commodity, nothing packs the #2 commodity on the same lane into the
   rest of the hold. The plan itself never intended to fill the ship.
2. **Run-time** — the kiosk has less than the feed said. The auto low-stock
   report (`_LOW_STOCK_FRACTION`) tells the *org*, but nothing helps the pilot
   standing at the terminal with 1,864 SCU of empty hold.

**Non-fix:** the "minimize empty-hold flight" checkbox (`minimize_deadhead`)
penalizes empty *repositioning* between a sell and the next buy in the solver
score. It has no concept of a short fill; it was never going to help here.

## Shipping plan

- **PR 0** — bugfix, ships alone: mid-run re-plan built `held` from the leg's
  *planned* `scu`/`buy_price`, not the entered actuals — after a short fill, a
  re-plan carried phantom SCU into the held-sell leg. Actuals-first now.
- **PR 1** — the **leg-lots model** + the run-time **"⊕ top up from here"**
  button (this doc, below).
- **PR 2** — plan-time **mixed loads**: an opt-in `cargo: mixed` knob that
  makes the solver rank lanes on multi-commodity bundles (§PR 2 below —
  built 2026-08-23).

## The model: extras on the leg, never new legs

The run state machine is strictly sequential (buy leg *i* → sell leg *i* → buy
leg *i+1*), so "buy A, buy B, fly, sell A, sell B" cannot be two legs without
rebuilding the cursor into hop-groups. Instead a top-up is a **co-cargo lot on
the active leg**:

```
leg.extras = [{commodity, scu, buy_price, sell_price,
               sell_terminal, sell_terminal_id, box_size,
               actual_sell_price?, actual_sell_scu?, sold?}, …]
```

Hard constraint (v1, and the thing that keeps everything simple): **extras buy
at the leg's buy POI and sell at the leg's sell POI.** Same hop ⇒

- zero routing impact — distance/ETA/waypoints/hazard detours unchanged;
- profit ranking of suggestions is *exactly* per-hour ranking (identical
  travel for all candidates);
- consistent with the settled #46 call: box count / dwell never feeds route
  ranking. `STOP_DWELL_S` untouched.

`buy_price`/`scu` on a lot are **actuals** — a lot is created by a human at
the kiosk typing what they bought (add-as-bought, below). `sell_price` is the
plan figure from the feed; the sell confirm records its own actuals.

## PR 1 — run-time top-up

### `GET /api/trade/run/topup` (session auth, `solve` rate bucket)

Valid only while the active leg is state `bought` and not `held` (a held leg's
"buy POI" is wherever the re-plan happened — there is no kiosk there).

- `free_scu` = `usable_scu` − aboard, where aboard = primary
  (`actual_buy_scu` else plan, 0 once `primary_sold`) + unsold extras.
- Candidates = `nav_core.topup_candidates(...)`: price points buying at the
  leg's buy POI and selling at its sell POI, both sides fresh per the run's
  `max_price_age_days`, positive margin, primary commodity (and already-added
  extras) excluded, honoring stock-report avoid sets, run legality (#42),
  `min_return_pct` (#43), and the run's box policy (#46) sized to `free_scu`.
  Built on `_trade_row`, so rows carry the box plan / return % / freshness
  stamps the frontend already renders. Best terminal pair per commodity, top 8
  by `trade_profit`.
- The run's `budget` is deliberately **not** reapplied: remaining bankroll
  mid-run is unknowable server-side; each row shows `buy_cost` prominently and
  the pilot knows their wallet.

### `PATCH /api/trade/run action=addcargo` — one step, add-as-bought

`{action: "addcargo", leg, commodity, scu, price, box_size?}` — the suggestion
was the quote; the add IS the confirm, recording what the pilot actually
bought. The app never auto-confirms (same stance as #41). Server-side:

- re-validates against live prices (fresh sell point for that commodity at the
  leg's sell POI, fresh buy point at the buy POI) — 409 when the suggestion
  has gone stale, 400 on a commodity that was never suggestible;
- caps `extras` at 8 lots per leg;
- files a supply-`low` stock report when the bought SCU lands well under what
  the shelf could have supplied (same `_LOW_STOCK_FRACTION` rule as a short
  primary buy — ambiguous evidence accepted: a deliberate small buy files a
  report a later member disproves);
- folds the lot's expected profit into the frozen `summary.total_profit` (live
  finding, 2026-08-22): the run header reads "realized X of Y planned", and a
  plan that grew must say so — otherwise realized overtakes "planned" by the
  sell step. Time/distance are genuinely unchanged (same hop), so only the
  profit figure moves; per-hour/return stay the plan's until the next re-plan.

### Sell phase: per-lot confirms

- `action=sell` unchanged for the primary, `+ extra: <idx>` confirms one lot
  (its own `price`/`scu` actuals, demand-`low` auto-report on a short sell).
- The leg's state stays `bought` until the primary **and** every extra are
  sold (`primary_sold` flag on the leg, `sold` per lot; order free). Then the
  existing advance machinery runs untouched.
- `advance` (skip) is refused while ANY bought cargo is unsold — top-up lots
  *and*, since the 2026-08-23 profit-calc review, the bought primary (the same
  falsification: a skipped leg is excluded from realized stats, so a paid buy
  would silently vanish from them). Skip is offered in the buy phase only; the
  escape hatches are sell-short or re-plan (cargo re-homes as held legs).
- `demandout` stays primary-anchored; a stuck extra is handled by re-plan.
- The #41 txn nudge stays primary-only in v1 (`txn_id` ignored on `extra`
  confirms).

### Re-plan with lots aboard

`replan_trade_route(held=…)` accepts a **list** of held lots (a bare dict
still works — old signature). Sell legs are chained greedily: best
(score-wise) lot first from the live position, next lot from that sell POI,
and so on; continuation trades follow from the last sell stop. A lot with no
reachable buyer is reported in `summary.stranded_held` (list of commodity
names) while the sellable rest still plans — all-stranded keeps the existing
empty-plan + reason behavior.

App-side, `held` is built from the active leg: primary (unless
`primary_sold`) + unsold extras, actuals-first (PR 0), each with its
`box_size`.

### Stats

`trade_leg_realized(leg)` = primary realized + Σ lot realized
(`(actual_sell_price ?? sell_price) × (actual_sell_scu ?? scu) − buy_price ×
scu`). Everything downstream (run view tally, history, quick-picks, guild
stats) reads through it and needs no change.

### Frontend (run card)

- Bought-state active leg, not arrived, free hold ≥ 1 SCU → `⊕ top up from
  <buy terminal>` button; free > 25% of hold → amber "N SCU still empty"
  nudge line beside it.
- Suggestion panel: commodity · @buy → @sell · boxes × size chip · est.
  profit · return %. ADD expands an inline actuals row — the shared
  **total-first buy form** (size × count + total aUEC, see Actuals entry v2
  below) — confirm posts `addcargo`.
- Extras render as lines under the leg's buy step; the sell step grows one
  actuals row + confirm per unsold lot (sell side stays per-SCU with the box
  echo — the two kiosk ends genuinely differ, see CLAUDE.md #46d).
- `tradeRunSig` includes per-lot sold flags so WS re-renders fire.

## PR 2 — plan-time mixed loads — AS BUILT (2026-08-23)

Opt-in knob `cargo: single|mixed` on `TradePlanIn`/`TradeReplanIn` (default
`single` = the old behavior byte-for-byte; `_norm_cargo` widens unknowns to
single), persisted in run params + favorites; frontend `#trade-cargo` seg in
the ROUTE section (solver modes only — manual legs are the player's picks) +
`setTradeCargo`/`TRADE_CARGO_HINT` + a `mixed loads` rule chip.

Key structural fact, held as designed: `_greedy_route` consumes candidates
through `trade_profit`, `buy_cost`, `buy_poi_id`, `sell_poi_id` — so mixed
loads changed the **candidate generator**, not the solver core:

- `nav_core._trade_bundle_candidates`: runs `_trade_candidates` (so freshness,
  legality, stock avoids, `min_return_pct`, avoid/exclude sets and the #46 box
  policy all apply per lot with zero duplicated filtering), groups the
  survivors by (buy POI, sell POI), and greedy-fills the hold per pair by
  margin/SCU with per-commodity supply/demand caps + per-lot box snapping
  (`_bundle_fill`/`_commodity_cap`). When a *budget* is set, a second fill
  ordered by return-per-aUEC runs and the more profitable fill stands.
- The aggregate row's solver-facing figures (`trade_profit`/`buy_cost`/
  `max_scu`) are BUNDLE totals; the richest lot's fields are the row's
  top-level fields and a `primary` split + `lots[]` ride along. `_cost_route`
  emits the leg with **primary** scu/profit/buy_cost plus `lots` (each lot's
  own economics, `_lot_view`) and `bundle` (stop totals) — so realized stats,
  the low-stock fraction and the buy confirm keep speaking about one
  commodity, while summary totals and peak capital are the whole stop's.

**Two deviations from the original sketch, both deliberate:**

1. **The pool is a SUPERSET, not one-row-per-pair.** Single mode can ping-pong
   one lane with several single-commodity hold-fills (more SCU over more
   hops); a one-row-per-pair pool took that option away and made mixed WORSE
   on capped lanes (caught by the equivalence test). So the pool is every
   single row + one bundle row per multi-commodity pair, and the objective
   decides. `_greedy_route`'s used-set consumes a key per lot a bundle buys
   (`keys()`), so one shelf never sells twice in a route; `_solve_route`'s
   dedup sig names lot commodities so bundle-vs-single chains both survive to
   scoring. A fill that degenerates to one lot emits nothing — with a small
   hold the mixed pool IS the single pool, which makes the equivalence
   obligation structural (test-pinned at candidate and route level).
2. **Planned lots ride the leg as `leg.lots`, NOT as `extras`.** PR 1's hard
   invariant is that `extras` buy figures are ACTUALS created by `addcargo` at
   the kiosk (`_lot_realized` has no plan fallback on the buy side, and the
   run-start summary + addcargo's summary-grow would double-count a
   pre-seeded lot). So a planned lot is a *plan* until the pilot confirms it:
   the run card renders each unconfirmed lot as a prefilled add-as-bought row
   (sell phase, still at the buy kiosk) that files the **existing `addcargo`**
   — which detects the matching planned lot, marks it `added`, and skips the
   summary grow (the plan already counts it). A lot the kiosk doesn't have is
   simply never confirmed: nothing tracks it as aboard, `advance` isn't
   blocked, and a re-plan carries only what was really bought. PR 1's run
   machinery is genuinely unchanged.

Wiring: `plan_trade_route`/`replan_trade_route` take `cargo=`; run params
persist it; replan resolves body-override-else-params like every other knob.
The topup suggestions endpoint excludes planned lots (they have their own
rows); `_annotate_leg_legality` badges contraband lots individually. Plan leg
card: `⊕ mixed ×N` chip, `⊕ also …` lot lines, a `stop total` econ line.
The board (`/api/trade/trades`) stays single-commodity by nature.

Why it matters for big ships: at 2,200 SCU nearly every commodity is
supply-capped, so ranking lanes on their single best commodity is
systematically wrong — a lane with four mediocre commodities can beat a lane
with one great one (test-pinned: 4 × capped-100 mediocre lots at 40,000 beat
the single best commodity's 18,000). (A cheaper "post-pass" that fills
residual hold after the route is chosen was considered and rejected: it can't
fix lane *choice*.)

## Actuals entry v2 — total-first (2026-08-23, after a live run went −14M)

A pilot's first mixed run surfaced two entry gaps that had let a per-container
price land in a per-SCU field (a 24×/32× phantom loss in realized stats):

1. **The box size was only correctable on the primary's re-fit panel** — lots
   (planned co-loads, top-ups) were locked to the planned size, so a kiosk
   without it forced head-math across units.
2. **Per-unit × count entry fights the kiosk's own rounding** — the TOTAL is
   what the wallet really moved.

So every actuals form is now total-first: buy = *size select × count + total
aUEC* (shared `buyActualForm`; SCU and per-SCU derived, per-SCU unrounded so
price × scu reconstructs the true total), sell grid = *SCU + total* with a
derived /SCU column. `buy`/`addcargo` accept `box_size`: the size really used
is stored (`actual_box_size` / lot `box_size`, preferred by the replan's held
list) and files a container SEEN-report — a purchase can confirm a size
exists, never rule one out. Guard rails from the same incident: `advance`
refuses while any bought cargo is unsold (primary now, not just lots),
`confirmOddActual` gates any derived per-SCU ≥8× off the plan's own quote, a
re-plan's `stranded_held` renders as a run-card callout, and
`DELETE /api/trade/history/{run_id}` (owner or admin) removes a poisoned run
outright — stats read every stored blob forever, so the correction for a
wrong-unit run IS the removal.

## Deliberately out of scope

- **Extras selling at a different stop** than the leg's destination — a real
  new leg; needs a hop-group cursor rework. Possible v2 middle ground: allow
  sell POIs matching *later stops already on the route* (still zero detour).
- **Mixed-destination bundles** (buy at A, drop some at B, rest at C) — a
  pickup-and-delivery problem; order-of-magnitude solver rewrite. No.
- **#41 txn auto-match for extras** — extend `pending_txn` matching to lots
  once the shape settles.
- **Dwell modelling** — more lots = more kiosk transactions per stop, but per
  the settled #46 call that stays out of route ranking until the captured
  dwell data (#41 §6) produces a real model.

## Caveat worth remembering

Mixed loads lean *harder* on UEX supply figures — the exact number that lied
at Baijini. Plan-time mixed and run-time top-up compose rather than compete:
the plan is the best guess, the button is the truth reconciliation at the
kiosk.
