# Org marketplace — design

**Status:** designed 2026-06-24; **v1 BUILT 2026-06-25** (uncommitted) — the
**fifth app** in the SPA. All four build steps landed in one pass: `listings` +
`listing_offers` tables (`db.py`) with sale/auction/barter on one `mode`
discriminator; `nav_core.derive_auction_state` (tie-break-by-arrival + buyout
short-circuit, 9 new unit tests, suite 138 green); the full `/api/market`
endpoint family (browse/detail/create/edit-cancel, `offer` for buy/bid/counter,
`offer/{id}` accept/withdraw, `confirm` dual-handshake, lazy auction expiry on
read); and the `#/market` SPA (launcher card, board with mode-filter tabs +
"My listings", detail with per-mode action blocks + bid/offer list + dual-confirm
handshake + completed-deals reputation, new/edit form with mode toggle), plus the
persistent aUEC-only banner. `delete_member` hard-deletes a member's listings +
bids and de-identifies buyer rows. **Needs /deploy.** Deferred items below are
still deferred (inventory bridge, Discord announce, WTB, price history). This doc
captures the decisions
agreed before any code, matching the cargo planner
([`docs/cargo-hauling-planner.md`](cargo-hauling-planner.md)) and event planner
([`docs/event-planner.md`](event-planner.md)). It is the **fifth app** in the SPA
and the **sibling** of [Org Inventory & Goals](org-inventory-goals.md) — it shares
that app's **item catalog** and reuses its trust model.

Star Citizen has **no built-in auction house or marketplace.** This app gives org
members a place to **sell, auction, and trade in-game items among themselves**,
priced in **aUEC only — never real money.** A member posts a listing (item, qty,
fixed price *or* timed auction *or* barter ask); other members buy / bid / make an
offer; the two parties settle the actual handoff **in-game**, then mark the
listing complete.

**Why this app is org-specific (not generic):** it's a closed, trusted, single-
guild market gated by the same Discord membership as the rest of the app — no
strangers, no scams-from-outside, no real-money line to police. It piggybacks on
the [Inventory & Goals](org-inventory-goals.md) **item catalog**, so listings
reference the same items the org already tracks.

**Out of scope (v1, hard):** **real-money transactions of any kind** (aUEC only —
this is a fan project under CIG's IP; see the existing fansite/trademark
disclaimer). Also: in-game escrow (SC has none — see *The coordination-board
reality*), shipping/delivery automation, cross-org trading, and reputation
scores beyond a simple completed-deals count.

---

## The coordination-board reality (why this is a handshake, not an exchange)

There is **no in-game escrow** and **no API to move items or aUEC.** The app
**cannot** hold funds, hold goods, or guarantee a trade. So the marketplace is a
**coordination board**: it helps two members *agree* on terms; the actual
exchange happens in-game, in person, on trust — the same social-trust model as the
inventory ledger.

That fixes the shape of the whole app:

- The unit of work is a **listing** moving through states `open → pending →
  completed` (or `cancelled` / `expired`), each transition a recorded **handshake**
  between buyer and seller — not a money movement.
- "Completed" means **both parties confirm** the in-game handoff happened. The app
  records the agreement; it never touches goods or aUEC.
- Trust is social. The only enforcement is transparency (who agreed to what, when)
  plus a lightweight completed-deals count per member.

---

## Why this fits the existing system

- **Shared item catalog** — listings reference the **same `item_id`** the
  inventory/goals app defines (`GET /api/catalog`, feed items + custom). Build the
  catalog once for both apps. A listing is "item + qty + ask."
- **Identity & org gate** — `auth_gate` already restricts the app to one guild's
  members, so the market is **closed and trusted** for free. Seller/buyer are
  `discord_id`s; the cosmetic handle is the display label (the identity model the
  whole app already uses).
- **Pure derivation + tests** — auction/offer resolution (current high bid, is-it-
  closed, winner) is pure math in `nav_core.py` (`derive_auction_state`), pinned by
  `test_nav_core.py`. Time math (auction `ends_at`) reuses the UTC-store/local-
  render util the event planner uses.
- **Schema + UI conventions** — `CREATE TABLE IF NOT EXISTS` + `_ensure_column`;
  launcher card + hash-routed `#/market` view family in `applyView()`; cards/detail
  CSS in the spirit of `#/events`.
- **Optional inventory bridge** — a member can list *from* their general (goal-less)
  inventory rows; completing a sale decrements that row. Pure UI/data reuse since
  both apps share `inventory` — but **deferred** to keep v1 simple (see below).

---

## Listing types (one table, a `mode` discriminator)

The user picked **sell + auction + trade** (2026-06-24). Three modes on one
`listings` row, distinguished by `mode`:

| mode | fields used | settle rule |
|---|---|---|
| `sale` | `price_auec` (fixed) | buyer clicks **Buy** → `pending`; quantities can allow "make offer" below ask. |
| `auction` | `start_price`, `ends_at`, optional `buyout_auec` | timed; highest bid at `ends_at` wins (or instant on `buyout`). |
| `barter` | `want` (free-text or catalog item the seller wants) | offers are counter-items; seller accepts one → `pending`. |

All three share `item_id`, `qty`, `seller_id`, `status`, `note`, timestamps.
Offers/bids live in a child `listing_offers` table so all three modes use one
"someone responded" flow.

---

## Data model

`listings`: `(id, seller_id, item_id, qty, mode, price_auec, start_price,
buyout_auec, ends_at, want, status, note, created_at, completed_at,
buyer_id NULL)`.

- `status`: `open` | `pending` | `completed` | `cancelled` | `expired`.
- `ends_at`: UTC ISO8601 for auctions (NULL otherwise); expiry is **derived**
  (`derive_auction_state`) and lazily flipped on read, like the cargo planner's
  arrival detection — no background job.

`listing_offers`: `(id, listing_id, bidder_id, amount_auec NULL, offer_item_id
NULL, offer_note, created_at, status)`.

- For `auction`/`sale`: `amount_auec` is the bid/offer. For `barter`:
  `offer_item_id` + `offer_note` is the counter-item.
- `status`: `active` | `accepted` | `withdrawn` | `lost`.

`derive_auction_state(listing, offers, now)` (pure, tested) → current high bid,
bidder, whether closed, computed winner, and the next minimum bid. The tests pin
tie-breaking (earliest bid wins on equal amount) and the buyout short-circuit.

---

## The deal handshake (settlement without escrow)

1. **Sale**: buyer clicks Buy (or seller accepts an offer) → `pending`, `buyer_id`
   set. Both see "arrange handoff in-game."
2. **Auction**: at `ends_at`, top bid wins → `pending` with that bidder.
3. **Barter**: seller accepts a counter-offer → `pending`.
4. **Both confirm** the in-game handoff → `completed` (`completed_at` set). Either
   can **dispute/cancel** before both confirm → back to `open` or `cancelled`.

A member's **completed-deals count** (seller + buyer side) is the only reputation
signal in v1 — derived, displayed on listings, no schema beyond counting
`completed` rows.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/catalog?q=` | **shared** with inventory/goals. |
| `GET` | `/api/market` | browse open listings; filters `?mode=`, `?item=`, `?seller=me`. |
| `POST` | `/api/market` | create a listing (any member). |
| `GET` | `/api/market/{id}` | detail incl. `derive_auction_state` + offers. |
| `PATCH` | `/api/market/{id}` | seller-or-admin: edit/cancel/confirm. |
| `POST` | `/api/market/{id}/offer` | place a bid / offer / barter counter (any member ≠ seller). |
| `PATCH` | `/api/market/{id}/offer/{oid}` | accept (seller) / withdraw (bidder). |
| `POST` | `/api/market/{id}/confirm` | buyer-or-seller confirms handoff; both → `completed`. |

Stateless math (`derive_auction_state`, completed-deals count) in `nav_core.py`,
unit-tested before endpoints land. Same build order as the other planners.

---

## UI (`#/market`, launcher card)

- **Launcher** — one new card at `#/` ("Org Marketplace").
- **`#/market`** — listing cards: item + qty, a **mode chip** (Sale / Auction /
  Barter, color-coded like event categories), price-or-current-bid-or-want, and
  for auctions a **countdown** ("ends in 2h"). Filter by mode/item; search via the
  shared catalog autocomplete.
- **New listing form** — mode toggle reveals the right fields (price / start+ends /
  want); item via the shared catalog picker; optional **"list from my inventory"**
  prefill (deferred bridge).
- **Listing detail** — current state, offer/bid list, the right action (Buy / Bid /
  Make offer / Accept / Confirm handoff), and the seller's completed-deals count.
- **aUEC-only banner** — a persistent "in-game aUEC only — no real-money trades"
  note on the form and listings, reinforcing the fan-project disclaimer already on
  the login splash and launcher.
- CSS reuses event chips + countdown + card grid; no chart lib.

---

## Relationship to Inventory & Goals

The two apps are **siblings over one catalog**, not stacked:

- **Shared**: `catalog_items` + `GET /api/catalog`. Build once. The marketplace
  blocks on the catalog, so **ship the inventory/goals catalog first.**
- **Optional bridge (deferred)**: list *from* general inventory rows; a completed
  sale decrements the seller's `inventory` qty, and surplus from a `met` goal can be
  one-click listed. Pure reuse of shared tables — UI work, no new data model.
- **Kept separate**: inventory is *org-pooled pledges toward goals*; the market is
  *member-to-member personal sales*. Different intent, same items.

---

## Build order

0. **Item catalog** — shared with inventory/goals; **must land first.** If that app
   builds first, this step is already done.
1. **Listings + sale mode** — `listings` table, `/api/market` CRUD, `#/market`
   browse + detail, the `sale` handshake. Ship the simplest mode end-to-end first.
2. **Offers + auction mode** — `listing_offers`, bid flow, `derive_auction_state` +
   tests, countdown UI, lazy expiry.
3. **Barter mode** — counter-item offers reuse the offers table + accept flow.
4. **Reputation + confirm** — completed-deals count, dual-confirm settlement.

---

## Deferred (cheap paths noted)

- **Inventory bridge** — list from / decrement `inventory` rows; surplus from `met`
  goals → one-click listing. Both apps already share the tables.
- **Discord announce** — post new/ending listings to a channel (same deferred hook
  the event planner has).
- **Saved searches / wanted ads** — a "WTB" listing is just the inverse `mode`;
  notify when a matching sale appears.
- **Price history** — completed `sale`/`auction` rows already hold cleared prices;
  `derive_*` a rolling median per item for a "fair price" hint. No new data.
- **Richer reputation** — ratings beyond a completed-deals count, if abuse appears.
- **Suggested / market-value price** *(planned 2026-06-25)* — the uexcorp
  `items_prices_all` feed (now backing the equipment side of the shared catalog)
  carries `price_buy` / `price_sell` per item, and the commodities feed carries
  commodity prices. Surface a reference **"market value"** on the listing form (and
  a one-click "use market price") so a seller can anchor their aUEC ask against the
  current in-game economy. Cheap path: have the catalog item carry an optional
  `price` (today it's name-only — `{item_id, name, kind, unit}`); compute a per-item
  median buy/sell when the feed is loaded and stamp it on the item, then prefill the
  form. No new table — the value rides the in-memory catalog like `unit` does.
- **Crafted-item quality annotation (SC 4.8 crafting)** *(planned 2026-06-25)* —
  as of Star Citizen 4.8, crafting attaches a **quality** to items: source materials
  (ores) carry a quality value **1–1000** in **8 static bands** (Band 8 = 1000 =
  premium), and that quality propagates through refining → crafting into the finished
  component's stats (power plants, coolers, shield generators, weapons). A member
  selling a *crafted* item will want to advertise its quality, so let the seller
  **annotate quality properties on a listing**: an optional quality value (1–1000)
  and/or band (1–8), plus free-form per-stat attributes. Cheap path: a JSON
  `attributes` blob on `listings` (same pattern as `events.roles` / goal
  `line_items`) rendered as a "Crafted · Qn" badge on the card + a stats table on the
  detail. **Open research before fixing the schema:** confirm exactly what a crafted
  item *instance* exposes in-game (a single quality scalar vs. per-component-stat
  values, and whether band or raw 1–1000 is the player-facing number).

---

## Relevant code

- `server/nav_core.py` — add `derive_auction_state` (+ completed-deals helper),
  pure and unit-tested; pattern off `derive_event_fill` / `derive_run_stats`.
- `server/db.py` — `CREATE TABLE IF NOT EXISTS` + `_ensure_column`; new `listings`
  + `listing_offers` tables keyed on `discord_id`; **`catalog_items` is shared**
  with [inventory/goals](org-inventory-goals.md).
- `server/app.py` — `require_session` / seller-guard / `require_admin`; catalog
  served like `/api/ships`; UTC `ends_at` stored, local-rendered; lazy expiry on
  read like the run arrival check.
- `server/static/index.html` — launcher card + `#/market` views in `applyView()`;
  mode chips + countdown reuse the event CSS; persistent aUEC-only disclaimer.
