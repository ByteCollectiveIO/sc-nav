# Inventory pickup runs — design (#48)

🔨 **Deliver half (§1–§7) built 2026-10-17, unreleased** — as-built detail in
implementation-notes.md › Resource manager; deferred from §4: the Discord
announce doesn't mention the drop-off yet. **Sell mode (§8) built 2026-10-17, unreleased** — see §8.8 for where the build departs from this design. A player request: pick holdings from
your Resource Manager inventory that sit at known locations and get an
efficient route that collects them all and drops them off somewhere. Two
destinations, one feature:

- **Deliver** (§1–§7) — to a fixed place, most often an org goal's drop-off
  once the goal has filled. Pure hand-off to the cargo planner.
- **Sell** (§8) — consolidate and sell where it pays best (e.g. Iron and
  Silicon scattered over four stations → collect → sell). The destination is
  *chosen by the planner* from the trade price feed, so this half needs a
  small server-side solver step on top of `plan_route`.

---

## Decisions (user, 2026-10-17)

| Question | Call |
|---|---|
| Does a goal carry a drop-off location? | **Yes** — optional `goals.dropoff_poi_id`, set by the goal creator. Blank = the player picks one. |
| Items not measured in SCU (`each`, `unit`, `L`, `mg`)? | **0-SCU stops** — visited, never count against the hold. |
| "Delivered" state / move holdings after the run? | **v2.** v1 is plan + run only. |

Implied, not separately asked (flag if wrong):
- **Own holdings only.** Every holding is one member's pledge in their own
  hangar/local inventory (org-inventory-goals.md "The inventory reality"); you
  can't collect another member's stash. A multi-contributor goal = each
  contributor plans their own run. An organizer-side "who's bringing what"
  logistics view is a separate idea.
- **Goal runs carry the committed amount, never the promised part.** See §3.

---

## 1. Why this is a handoff, not a new solver

The cargo planner (`nav_core.plan_route`, docs/cargo-hauling-planner.md)
already solves the problem: its unit is a **package** (`scu` of `commodity`
from POI A → POI B), it is capacity-aware, orders stops with branch-and-bound,
and run mode checks pickups/drops off. A collection run is N packages
`holding.location_poi_id → dropoff` sharing one destination.

So the feature = build that package list from inventory and hand it to
`#/route` **pre-filled**, via the same path "Clone" on a past run uses
(`cloneRun(run)` → `addPkgRow(pre)`, `pre = {commodity, scu, from_id,
from_name, to_id, to_name, contract}`). The player reviews, edits, picks a
ship, Plans and Runs exactly as today. **No new endpoint, no second solver,
no new run table** — history/leaderboard/stats keep working (see §5 for the
one leaderboard question).

---

## 2. Data gap: holdings have no POI

`inventory.location` is **free text** (`db.py` inventory table, ≤120 chars
`_LOCATION_MAX`). The Inventory form's location box already uses
`attachPoiPicker`, which sets `input.dataset.poiId` on a pick — but the POST
sends only `location` text (`index.html` inventory add handler), so the id is
thrown away.

Changes:
1. **Schema** — `_ensure_column("inventory", "location_poi_id", "INTEGER")`.
   The lot unique key (`_INV_LOT_KEY_SQL`) stays on the **text** location:
   don't change lot identity.
2. **Write paths** — `InventoryIn`, the inventory `PATCH` model and
   `ContributeIn` get `location_poi_id: int | None`. Server validates it's in
   `nav.pois` (else store NULL, keep the text) and that the POI's name matches
   the text (a picked id with retyped text = stale pick → NULL). Client sends
   `dataset.poiId` when the picker is in its `picked` state.
3. **Existing rows** — resolve on read, best-effort: exact case-folded match
   of `location` against POI names; a unique match is used (not persisted —
   the row's text is still the source of truth, and the POI set changes with
   `starmap_pois_enabled`/`wiki_pois_enabled`). Expect low hit rates
   ("Area18 hangar" ≠ "Area18").
4. **Unresolved holdings** in a pickup selection pre-fill the row's "from"
   box with the text but **unpicked**, so the existing planner validation asks
   for a location. Picking it there optionally writes back to the holding
   ("remember this location" — same call as the inline editor).

A custom POI (`custom_pois`) is a valid location — it's in `nav.pois`.

---

## 3. What quantity each package carries

| Source | `scu` on the package |
|---|---|
| "Plan pickup run" from My holdings | the holding's `qty` (player can lower it in the planner) |
| "Plan my delivery" from a goal | the allocation's **covered** amount = `qty − short` (`_ALLOC_SHORT_SQL`, already returned by `list_goal_contributions`) |

- The **short** part of a pledge (v1.2: commit past what you hold) is not in
  any hangar, so it's not a pickup. It's listed under the planner as
  "still to gather: N of X" (deep-link to the resource finder, the existing
  goal→map link) — never planned as a stop.
- **Unit ≠ SCU** (`catalog.UNITS` = SCU, each, unit, L, mg) → package
  `scu: 0`, commodity label carries the real amount ("12 each Medical
  pen"). The planner's client currently **skips** rows with `!(scu > 0)`
  (`index.html` plan submit loop); relax that for rows marked as
  inventory-sourced 0-SCU (a `data-zero-ok` flag set by the prefill), not
  globally — a contract row with no SCU is still an entry mistake.
  Server `PackageIn.scu` is already `ge=0`.
- **Already at the drop-off** (`location_poi_id == dropoff`) → not a package;
  shown as "already there".
- **Quality lots** (#151): two lots of the same ore at one location are two
  packages (same from/to), labeled with their Q. Fine for the solver; stops
  merge per location anyway.

---

## 4. Goal drop-off

- `_ensure_column("goals", "dropoff_poi_id", "INTEGER")` +
  `dropoff_name TEXT` (denormalized like `inventory.item_name`, so a POI
  that disappears with a toggle still reads sensibly).
- `GoalIn` gains both; set in the goal create/edit form with
  `attachPoiPicker`. Validated against `nav.pois` like §2.
- Goal detail shows "📦 Deliver to <name>". A Discord announce for a goal with
  a drop-off mentions it (template variable; golden file regen = intended
  wording change).
- Personal goals can have one too (delivering to yourself, e.g. a crafting
  station).

---

## 5. UI

**Inventory → My holdings**
- Row checkboxes (only on own rows) + a sticky action "Plan pickup run (N)".
- Opens a small dialog: destination (POI picker, or "a goal's drop-off" select
  of active goals that have one) → navigate to `#/route` with the prefill.

**Goal detail**
- "Plan my delivery" button, shown when the viewer has ≥1 covered
  contribution to this goal. Destination = goal drop-off; if none, the same
  destination dialog.
- Disabled-with-reason when everything is still short ("nothing gathered
  yet").

**Cargo planner (`#/route`)**
- Accepts the prefill through a one-shot seed (`routeSeed`, consumed once —
  same pattern as `eventSeed`), applied with the `cloneRun` code path.
- Banner above the package list: "From your inventory · goal <title>" and the
  "still to gather" list.
- `contract` field on each row = holding/goal label so run mode and history
  show what it was for.

**Open question (decide at build):** should inventory pickup runs count on the
cargo leaderboard/stats? They're real hauling, but zero-reward. Simplest:
record them like any run (reward is already optional) and let the reward
column show "—".

---

## 5a. Too much cargo for one trip (user, 2026-10-17)

A one-drop-off run has to hold everything at once before the last stop, so a
haul bigger than the ship used to come back "won't fit". The user's call: let
the player choose. The cargo planner has a **"split into trips if it doesn't
fit"** checkbox (off by default, remembered per viewer) that plans several
trips instead — each trip fills the hold, delivers, and the next one starts
from the drop-off. A holding bigger than the hold is collected in parts. It
works for any cargo-planner bundle, not just inventory runs (multi-pickup
deliveries excepted). As built: implementation-notes.md › Cargo planner.

## 6. v2 — built 2026-10-18 (unreleased), except the organizer view

- **Delivered** (`inventory_allocations.delivered_qty`): how much of a
  contribution has been handed over — never more than its stocked part
  (qty − short). `derive_goal_progress` reports `delivered` per line and per
  contributor. The contributor marks their own (**Mark delivered** / undo);
  the goal's creator or an admin can mark anyone's (**Mark received**), which
  is the org confirming receipt in-game. `POST /api/goals/{id}/delivered`.
  A goal pickup run carries only what's stocked and not yet delivered.
- **After a run planned from Inventory**, the completion card offers one
  click to make the ledger match (`POST /api/route/runs/{id}/inventory`, once
  per run): a delivery moves the hauled stock to the drop-off (pledges follow
  it — `db.move_holding`) and, for a goal run, marks it delivered; a sell run
  takes what sold off the holdings, and anything a buyer wouldn't take stays
  yours at that station. Runs carry `inventory: {kind, goal_id, sold}` and
  package ids `h<holding id>` so the server knows which holdings rode.
- **Drop-off in Discord**: the goal post's progress line says
  "📦 deliver to <station>", and the goal-met ping says where to deliver.
  Goals without a drop-off post exactly as before (golden file unchanged).
- **Split sales**: see §8.4. **Sell my leftover**: a met goal offers to sell
  the viewer's free (uncommitted) SCU stock of its items, through sell mode
  (`POST /api/inventory/pickup` with `free_only`).
- Still later: an organizer view of per-contributor pickup locations.

---

## 7. Build order + tests

1. Schema columns + write-path validation (`inventory.location_poi_id`,
   `goals.dropoff_poi_id/dropoff_name`). Tests: invalid id → NULL, stale
   pick (text mismatch) → NULL, lot key unchanged, goal drop-off round-trip.
2. Read-side resolution helper (pure, in `nav_core` or `catalog`): text → POI
   id, unique case-folded match only. Unit-tested incl. ambiguous names.
3. Package builder (pure): holdings/allocations + dropoff → packages,
   "still to gather", "already there". Tests: covered = qty − short,
   oldest-first short attribution respected, non-SCU → 0, same-location skip.
   Pure in Python so it's testable; exposed as a small JSON helper only if the
   client can't build it from `/api/inventory` + `/api/goals/{id}` (prefer
   client-side; both payloads already carry `short`, `unit`, `location`).
4. Planner: seed hand-off + `data-zero-ok` relaxation; verify 0-SCU stops
   order correctly in `plan_route` (add a test: a 0-SCU package still produces
   its pickup + drop stops).
5. Inventory checkboxes + dialog; goal detail button; goal form drop-off.

6. Sell mode (§8) — after 1–5, since it reuses the location data, prefill
   and run path.

Guards to keep: own rows only (server already scopes `/api/inventory` edits
to owner-or-admin; the pickup selection is client-side over your own rows),
`esc()` on every prefilled label, `class="ti"` on new inputs.

---

## 8. Sell mode — consolidate and sell (user, 2026-10-17)

> "In their inventory they have Iron and Silicon in several different
> locations. They want to gather that all up and then sell it at the most
> profitable location."

### 8.1 Why it's not just "pick a destination"

In Deliver mode the drop-off is given and `plan_route` only orders stops. In
Sell mode the drop-off is **part of the answer**, and the best one isn't
simply the highest price: a terminal paying 3% more three jumps away can lose
to a closer one, and a terminal that only takes 200 SCU of Iron can't absorb
600. So the planner has to choose **where each commodity gets sold** and
**in what order to visit everything**, together.

The two planners already split this way (trade-route-planner.md "Solver
approach"): the cargo planner orders a fixed set of stops; the trade planner
chooses which stops to use. Sell mode is in between: **the pickups are fixed,
only the sell terminals are chosen.** That's a much smaller search than full
trade planning, so it wraps the existing exact solver rather than building a
new one.

### 8.2 What's already there to reuse

| Need | Existing piece |
|---|---|
| Sell price per terminal | trade price feed rows `{commodity, poi_id, sell, scu_sell_stock, …}` |
| Org-reported prices beat the UEX scrape | price overlay (#39 slice 0), already merged into the feed |
| Stale prices | `_price_fresh(p, max_age_s, now, side="sell")` |
| How much a terminal will take | `scu_sell_stock` + org stock reports (`demand_caps`, `_cap_price_point`) |
| Terminal stopped buying | `avoid_sells` from stock reports |
| Contraband | `legality_allows` (#42) |
| Danger, ship can't land | `avoid_poi_ids`, `avoid_volumes`, `exclude_poi_ids` (#24, #34) |
| Order the stops, respect the hold | `plan_route` (exact branch-and-bound, ≤12 stops) |
| Inventory item → priced commodity | catalog builds `commodity:<slug>` from the same feed names, so case-folded `item_name` = price `commodity` |

The sell-side filtering in `_trade_candidates` (freshness, caps, legality,
avoid lists) is the part to factor out into a shared `_sell_points(prices,
commodity, …)` helper, so both planners filter sell points the same way.

### 8.3 Solver: `nav_core.plan_sell_route`

```
plan_sell_route(nav, prices, holdings, usable_scu, *, start_id/start_pos,
                sort="per_hour" | "total", single_terminal=False,
                k=3, max_age_s, legality, illicit, demand_caps,
                avoid_*, exclude_poi_ids, avoid_volumes, fuel_req, max_range_m)
  holdings = [{commodity, scu, from_id, label}]
```

1. **Candidates per commodity.** Sell points from `_sell_points` that pay for
   it, ranked by price; keep the top `k` (default 3). Also keep the best
   terminal that buys *every* commodity in the selection, if one exists:
   one stop that takes everything is often the best answer, even when it
   isn't top-price for any single item.
2. **Enumerate assignments** (commodity → terminal). With C commodities this
   is at most k^C: 3 commodities = 27 combinations, each one `plan_route`
   call on fixed packages (holding → assigned terminal). `single_terminal=True`
   limits it to the terminals that buy everything.
3. **Score** each solved route:
   - revenue = Σ min(scu, terminal cap) × sell price
   - `sort="total"`: highest revenue, time breaks ties
   - `sort="per_hour"` (default, same as the trade planner): revenue ÷ total
     route time. The goods are already owned, so there's no purchase cost: what
     the player is trading off is aUEC against time spent flying.
4. **Return the best route** in `plan_route`'s exact shape (summary + stops),
   plus a `sales` list per drop stop `{commodity, scu, price, revenue,
   price_age_s, source: "org"|"uex", capped}`, plus the **2 runners-up** as
   short summaries so the player can compare, e.g. "Sell all at Lorville CBD:
   412k · 38 min" vs "Iron → ARC-L1, Silicon → Area18: 430k · 52 min".

**Bounds:** cap at C ≤ 4 commodities with k = 3 (81 solves). More than
that → k drops to 2, then greedy (best price per commodity, one solve) with
a "simplified: too many commodities" note. Each solve is small (the stops
are only the pickups plus at most C terminals), so 81 is fine for the
`solve` rate-limit bucket. Pin the run time with a test.

**Hold smaller than the haul:** nothing extra. Holding → terminal packages
let `plan_route` sell along the way to free space ("collect Iron, sell it,
then collect Silicon"), so a ship that can't carry everything still gets a
route instead of a "too big" error.

### 8.4 Edge cases

- **Terminal takes less than you have:** *(v2, built 2026-10-18: every assignment is
  also tried with the rest sent on to the next-best buyers — up to 3 per commodity,
  holdings carried in parts `h<id>~<n>` — and the better route wins.)* v1 kept one terminal per commodity
  and sells up to the cap, flagging the rest: "ARC-L1 takes ~200 of your
  600 SCU Iron — 400 left over". Splitting one commodity across two
  terminals is **v2**, because it means splitting a package and makes the
  search much bigger.
- **No known buyer** (gear, components, non-SCU units, or commodities not in
  the price feed): not eligible to sell. Listed as "no market price"; the
  player can leave it out or send it to a destination they pick. The 0-SCU
  stop rule from §3 applies only in Deliver mode.
- **Quality (#151):** the price feed has no quality, so every lot of a
  commodity gets the same price. Shown as a note when lots of different
  quality are selected. Don't invent a quality multiplier.
- **Stale prices:** use the trade planner's freshness setting. Each sale
  shows the price's age and source (`⚡ org` badge like the trade board).
- **Mixed run:** each selected holding is either *sell* or *deliver to X*
  (e.g. sell the Iron, take the Silicon to a goal). Fixed packages go
  straight through; only the sell ones are enumerated. Same solver.
- **Already at a good terminal:** a holding sitting at a terminal that's in
  its candidate list becomes a zero-distance sale at that stop, not a skip
  as in §3.

### 8.5 Endpoint + UI

- `POST /api/route/plan_sell` (session, rate-limited `solve`, capped
  Pydantic input like `PackageIn`): `{holdings: [{inventory_id, scu}],
  usable_scu, start, sort, single_terminal, ship}`. The server reads the
  holdings (owner-checked, never trusts the client for commodity or
  location), resolves POIs (§2), loads prices + caps + warnings the way
  `/api/trade/plan` does, and calls `plan_sell_route`.
- **Inventory → My holdings:** the destination dialog from §5 gets a third
  choice, **"Sell at the best price"**, alongside "a place" and "a goal's
  drop-off", with options for *best aUEC/hour* vs *most aUEC* and *one stop
  only*.
- **Result:** shown in `#/route` as a normal plan (pre-filled through the
  `routeSeed` hand-off with the chosen terminals as each row's "to"), with
  a sales panel: per-stop revenue, total, aUEC/hour, and the runners-up as
  "use this instead" buttons. Run mode, history and stats then work
  unchanged. History stores the expected revenue as the run's reward, which
  also answers the §5 leaderboard question for sell runs.
- **Why not in the trade planner's screen?** The trade planner is
  buy → sell trading with budgets, top-ups and favorites. Sell mode never
  buys, and its stops come from your inventory. Putting it there would add
  a fourth mode with half the controls hidden. It reuses the trade planner's
  data and filters, but runs in the cargo planner's screen.

### 8.6 Tests

- `_sell_points` gives the same results as today's `_trade_candidates`
  sell-side filtering (refactor guard; the existing trade tests must stay
  green).
- `plan_sell_route`: picks the one terminal that buys everything when it
  beats a split; picks a split when the gain outweighs the extra flying
  time; `total` vs `per_hour` choose differently on a constructed case; a cap
  caps revenue and flags the leftover; no-buyer commodity → listed, not
  planned; a hold smaller than the haul still gets a route that sells along
  the way; the fallback past 4 commodities; illicit commodities follow
  `legality`.
- Endpoint: another member's `inventory_id` → 404; rate-limited.

### 8.7 Open questions for build

1. Should runners-up be routes that are actually different (different
   terminals), not the same terminals in a slightly different order? Likely
   yes: dedupe by the commodity → terminal mapping.
2. Should Sell mode also be offered when a goal is **met with surplus**
   (org-inventory-goals.md "Deferred: Goal completion → Marketplace")? It's
   the same selection with a different button, so it's cheap. Decide when
   the delivered state lands (v2).

Answered at build: (1) yes — alternatives are always different assignments,
and the next two by score; (2) still open, with the v2 delivered state.

### 8.8 As built (2026-10-17) — where it departs from §8.2–§8.5

- **No legality filter.** §8.2 listed `legality_allows`; the trade planner
  deliberately never applies legality to cargo already held (TRADE_LEGALITY:
  switching to "legal" must not strand contraband in your hold), and sell
  mode is all held cargo — so it keeps every buyer.
- **Camped buyers** (danger board / personal blacklist) are skipped while
  another buyer exists; if they're a commodity's only buyers they come back
  and the sale is flagged "only buyer is camped". Ship-unusable stops and
  stops a member reported not buying are always out.
- **The planner owns the ship.** The Inventory page can't know your hold, so
  "💰 Sell at best price" (next to "Plan pickup run") opens the cargo planner
  in sell mode: rows come pre-filled with "to" locked as *best buyer*, and
  **Plan route** calls `POST /api/route/plan_sell` with the planner's ship,
  start, danger, drive and split-into-trips settings. The banner carries
  best aUEC/hour vs most aUEC and "one stop only".
- **Endpoint input** is `items: [{holding_id, from_id?, scu?}]`: the row's
  corrected pickup and lowered amount are honoured; the commodity always
  comes from the holding. Per-piece items are listed "no SCU market price".
- **Result** is a normal cargo plan: rows get their buyers, the contract
  label is `Sell` with the expected revenue as its payout (so history,
  stats and aUEC/hour carry it), and *Start this run* runs it. The SELL
  panel lists each sale (price, amount, revenue, price age, `⚡ org`,
  left over past demand) and the two alternatives; **Use this instead**
  fills that assignment in and re-plans it as a normal run.
