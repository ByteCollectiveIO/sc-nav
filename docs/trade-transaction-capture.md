# Trade transaction capture — the log confirms the trade (#41) — build plan

**Status: 🔨 building 2026-08-02.** Born from the #40.2 Game.log spike
([watcher-modular-hud.md](watcher-modular-hud.md) §9.2.3): commodity kiosks
log both sides of every trade with total, unit price, SCU, commodity GUID and
shop name — verified against a real run where the log's sell total beat the
pilot's memory (161,211.00 vs a remembered 161,214).

Companion to [trade-route-planner.md](trade-route-planner.md) (#21, run mode)
and [uex-data-contribution.md](uex-data-contribution.md) (#39 — this build
creates that doc's §6.1 `price_reports` ledger and starts filling it; the
planner-facing overlay and freshness badge remain #39 slice 0's remit).

## 1. The idea

Run mode asks the pilot to type the price and SCU they just transacted. The
game already wrote both to `Game.log`, more accurately, at the moment it
happened. Stop asking; start reading:

- the watcher tails the log it already tails and reports commodity
  transactions to the server;
- the server matches them to the pilot's active trade run and surfaces a
  **nudge** ("⚡ terminal reported: BUY 29 SCU @ 3,408 — confirm applies
  these"), never a blind auto-confirm — the log records the *request*, not
  the fulfillment (verified: no ack line follows), so a human stays in the
  loop;
- confirming with the detected transaction teaches the two mappings the log
  doesn't hand us (`resourceGUID` → commodity name, `shopName` → our POI) —
  validated by the confirm itself, accumulating org-wide;
- every transaction that resolves lands in the `price_reports` ledger, so
  org-observed prices accrue from day one with zero manual entry.

## 2. The line shapes (build 12344265, 2026-08-02 capture)

- **Buy** — `<CEntityComponentCommodityUIProvider::SendCommodityBuyRequest>`:
  `shopName[…] price[TOTAL] shopPricePerCentiSCU[UNIT/100] resourceGUID[uuid]
  … quantity[N cSCU]`. Unit price = perCentiSCU × 100; SCU = cSCU ÷ 100.
- **Sell** — `…::SendCommoditySellRequest>`: `shopName[…] amount[TOTAL]
  resourceGUID[uuid] … quantity[N]` (already SCU). Unit = total ÷ quantity.
- **Cargo handling**, on both lines: `autoLoading[0|1]` (1 = the kiosk moved
  the cargo to/from the ship, 0 = freight elevator / hand-load) and
  `Cargo Box Data: boxSize[n] | unitAmount[n]` (SCU per box × box count; the
  sell line wraps its pair in extra brackets and drops the trailing zeros).
  Captured since the follow-up build — see §6.
- Timestamps are the line's own UTC stamp. Patch-fragility rule (#40.2 §9.2):
  parse defensively, and everything downstream degrades to absence — run mode
  without a watcher (or after a format change) behaves exactly as today.

## 3. Pieces

### 3.1 Watcher

- `parse_trade_txn(line)` — pure, in `sc_nav_watcher.py`, tested in
  `test_parse.py`. Returns `{side, shop, guid, total, unit_price, scu, t}`
  or None, plus `{auto_load, box_size, box_count}` when the line carries them
  (`_txn_cargo_handling`, separate optional searches — a reworded box-data
  string must cost that field, never the transaction).
- `GameLogShardReader` collects matches while scanning the lines it already
  scans; `pop_transactions()` drains. One tail, two consumers.
- `Sender.send_transactions()` → `POST /api/trade/transactions` (same token,
  UA, and error handling as positions). Failed batches re-queue (cap 40,
  oldest dropped) and retry next loop.
- Sticky opt-out `--no-trade-capture` (config `trade_capture`, default ON —
  a member already streaming live position is not surprised by trade capture;
  one startup log line says it's active).

### 3.2 Server

New tables (`db.py`): `trade_transactions` (the raw ledger; dedup on
member+t+side+guid+total, since a resent batch must not double-file),
`commodity_guids` + `shop_pois` (learned mappings with confirm counts),
`price_reports` (exactly #39 §6.1's schema, `uex_state` reserved).

`POST /api/trade/transactions` (`require_user` — new endpoint, so the watcher
token works without touching the #40.2 §5 auth question):

1. dedup + store each transaction;
2. resolve commodity/POI through the mapping tables when known;
3. if the caller's active run's active leg expects this side (pending→buy,
   bought→sell) and the commodity matches-or-is-unknown, park it as the
   session's `pending_txn` → `trade_run_view` gains `txn`, frame pushed so
   the nudge arrives live over the WS;
4. resolvable transactions write `price_reports` rows.

`PATCH /api/trade/run` gains `txn_id`: a buy/sell confirm that names the
pending transaction (the SPA attaches it automatically when leg+side match)
defaults `price`/`scu` from the log's numbers, records run/leg onto the
transaction row, **learns both mappings** (this confirm is the human
validation), and writes the price report if resolution just happened. The
pending txn is cleared on any cursor motion, replan, or abandon — a stale
nudge must not survive the state it described.

### 3.3 SPA

Run card: a `⚡ terminal reported …` banner on the active leg when `txn` is
present (apply-and-confirm button + dismiss), the leg's price/SCU inputs
prefilled from it, and `submitTradeLeg` attaching `txn_id` whenever the
pending transaction matches the leg being confirmed — so the learning path
does not depend on which button the pilot presses.

## 4. Deliberately not in v1

- **Auto-confirm without a tap** — revisit only after mappings mature and
  fulfillment ambiguity (§1) has an answer; likely an org setting.
- ~~**The planner-facing price overlay + freshness badges**~~ — **built as
  the immediate follow-up (v0.87.0)**: see the #39 doc's status header for
  what shipped (overlay, `⚡ org` badges, side-aware age filter, ≥2-confirm
  ingest gate, `is_missing` from stockouts).
- **Kiosk-board (`AddingCommodityBox`) ingestion** for terminal commodity
  lists / stock presence — real signal, separate decision.
- **Mission-hauling companion** (#40.2 §9.2.3 item 4) — its own feature.

## 5. Testing

- `watcher/test_parse.py` — both real captured lines parse exactly (numbers
  from the verified run), malformed/foreign lines return None, reader
  pick-up + drain, cSCU arithmetic.
- `server/test_app.py` — batch POST with a token stores + dedups; nudge
  appears on the matching run view (and not on side/commodity mismatch);
  PATCH with `txn_id` defaults actuals, learns both mappings, stamps the
  transaction row, files the price report; anonymous callers rejected.
- Live tail → POST on the Windows box: the one manual step, next real run.

## 6. Cargo handling — capture now, model later

The kiosk lines also state *how* the cargo moves (§2). That's the missing term
in aUEC/hour: the planner charges a flat `STOP_DWELL_S = 120` per stop
(`nav_core.py`), method-blind, and that constant feeds leg `profit_per_hour`,
route totals, and the solver's `per_hour` score. Auto-loading a hold costs real
minutes; the plan prices them at zero.

CIG has never published a rate and it moves every patch, so the answer isn't
research — it's measurement. **What shipped is only the capture**:
`auto_load` / `box_size` / `box_count` on `TradeTxnIn` and the
`trade_transactions` ledger (`_ensure_column` for pre-existing DBs). Nothing
reads them yet. They land now because price can be re-derived later and this
cannot: an unlabelled run is lost to the model forever.

Modelling notes for whoever picks this up:

- **Fit on box count, not SCU.** Cargo moves a box at a time, so 32 × 1 SCU and
  1 × 32 SCU are the same tonnage and almost certainly not the same wait. The
  2026-08-02 capture is 29 × 1 SCU on both sides.
- **Dwell is already observable** — transaction timestamp → the ship's first
  sustained motion away from the POI, off the position stream we already have.
  It also covers hand-loading, which the log never mentions.
- **Robust stats only.** A pilot who went AFK at the kiosk is in that sample;
  medians and trimming, not means.
- **Seed it in-game before the data accrues**: three autoload buys at equal SCU
  but deliberately different box counts pin the fixed overhead and confirm
  whether boxes or SCU drive the wait.
- Then a dwell model replaces the constant, behind an auto-load toggle on the
  plan form. Because dwell feeds the solver *score*, this re-ranks routes —
  fewer, fatter stops win once loading is priced honestly.

**Half of that toggle now exists (#46, 2026-08-09).** The plan form carries
`loading` = `auto | hand`, and in hand mode the solver sizes every fill to whole
cargo containers (`nav_core.box_plan`) — so each leg already knows its
`box_count`, which is the model's independent variable.

**But the last step above — box count feeding the solver score — is now ruled
out by decision, not just deferred.** The user's call, 2026-08-09: container size
is a *time-saving and personal preference* matter, and "it shouldn't really shape
the math for making money." Pricing a choice the player freely makes (and can
re-make mid-run, §6 of the #46 entry in the backlog) into `profit_per_hour` would
have the solver optimize personal comfort as if it were revenue — two members on
the same lane would get different routes because one of them prefers bigger
boxes.

What survives: a measured dwell is still worth having for **display** — telling a
player a fill is roughly forty minutes of lifting lets them choose knowingly,
which is where a preference belongs. Everything below still applies to fitting
that number. What must not happen is it leaking back into the ranking.
