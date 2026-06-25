# Org inventory & goals — design

**Status:** designed 2026-06-24; **v1 BUILT 2026-06-24** (uncommitted) as the
"Resource Manager" app — catalog + inventory ledger + goals, all three layers,
backend (12 tests) + `#/goals` / `#/inventory` UI + navigator deep-link. Build
notes in [`docs/feature-backlog.md`](feature-backlog.md) §14. This doc captures the
decisions agreed before any code, matching how the cargo planner
([`docs/cargo-hauling-planner.md`](cargo-hauling-planner.md)) and event planner
([`docs/event-planner.md`](event-planner.md)) were specced first. It is the
**fourth app** in the SPA (after Resource Navigator, Cargo Planner, Event
Planner) and the **backend half** of a two-app pair — the
[Marketplace](marketplace.md) is the sibling app and shares this app's **item
catalog**.

An app for the org to **track what it holds and set resource goals against it**.
An admin (or any member, configurable) defines a goal — "gather 500 SCU Titanium,
300 SCU Laranite, and 50 Quantanium to fund the org Hull-C" — with a **deadline**
and a **priority 1–10 (1 = highest)**. Members log what they've contributed toward
it. Each goal tracks fill against its line items (`Titanium 320/500 SCU`,
`overall 64%`), and the contributions roll up into an org-wide **inventory** view.

**Why this app is org-specific (not generic):** the goal line items feed *back
into* the Resource Navigator. A goal of "500 SCU Titanium" is a one-click jump to
the navigator's resource finder ("where's Titanium near me right now"). No
third-party tool plans an org procurement campaign that hands its shopping list
straight to *your* map.

**Out of scope (v1):** real escrow/shared storage (SC has none — see *The
inventory reality* below), automatic in-game inventory sync (no API exists),
multi-org tenancy (the app is locked to one guild), and selling/trading — that's
the [Marketplace](marketplace.md).

---

## The inventory reality (why this is a ledger, not a vault)

Star Citizen has **no shared org storage**. Inventory physically lives in
individual players' local hangars, ship holds, and personal inventories at
specific stations. There is no "org bank" to read from and no API to read it with.

So "org inventory" **cannot** be a literal pooled stash. It is a **per-member
ledger of pledges/contributions**: a member declares "I'm holding 80 SCU Titanium
for the org" or "I delivered 120 SCU toward the Hull-C goal." This is the model
the org actually wants anyway — it answers *who contributed what*, which a
fungible total can't. (Decision locked with the user 2026-06-24.)

Consequences that shape everything below:

- Every inventory quantity is **attributed to a Discord member** (the `owner_id =
  discord_id` key used everywhere in `db.py`).
- Trust is **social, not enforced** — the app records claims; the org verifies
  in-game. Same trust model as the marketplace handshake.
- "Org inventory on hand" is a **derived rollup** (`SUM(qty) GROUP BY item`), never
  a stored authoritative number.

---

## Why this fits the existing system

Same shape as the cargo and event planners — it reuses, not invents:

- **Identity & org gate** — every endpoint keys on `user["id"]` (the Discord
  member id used throughout `server/db.py`); `auth_gate` already restricts the app
  to org members, so "the org" *is* the contributor pool. Admin actions reuse
  `require_admin`.
- **Item catalog already half-exists** — `load_commodities` / `load_ships`
  (`app.py:244-300`) cache the uexcorp commodities + vehicles feeds to disk
  (`commodities.json`, `ships.json`). The catalog is **those feed items + custom
  hand-entered items** (components, gear, and anything not in a feed). Served like
  `/api/ships`.
- **Pure derivation + unit tests** — fill/progress math lives in `nav_core.py`
  (`derive_goal_progress`, `derive_inventory_rollup`), patterned off
  `derive_event_fill` / `derive_run_stats`, pinned by `test_nav_core.py` (the CI
  gate).
- **Schema conventions** — `CREATE TABLE IF NOT EXISTS` + `_ensure_column`
  migrations in `db.py`; JSON-blob columns for line-item lists, exactly like
  `events.roles` and the `/api/route/*` package blobs.
- **App shell exists** — launcher card at `#/` + hash-routed `#/goals` /
  `#/inventory` views as branches in `applyView()`; no shell work.

---

## Three layers

```
Catalog     the universe of items (commodities + ships + custom)   served like /api/ships
Inventory   per-member ledger of holdings/contributions            attributed rows, derived rollup
Goals       targets with deadline + priority + line items          fill = contributions vs. need
```

### 1. Item catalog (the shared backend — also powers the Marketplace)

One canonical reference of items, each with a stable `item_id`, `name`, `kind`
(`commodity` | `ship` | `component` | `gear`), and optional `unit` (`SCU` for
commodities, `each` for discrete items). Source:

- **Feed-backed items** — names from `load_all_commodities` (every uexcorp
  commodity, not just raw ores) and `load_ships`. Synthesize a stable id like
  `commodity:titanium`, `ship:hull-c`.
- **Custom items** — admin/member-added rows in a `catalog_items` table for
  anything not in a feed (weapons, armor, components, FPS gear). Id `custom:<n>`.

A single `GET /api/catalog?q=` autocomplete (debounced, like the POI/commodity
pickers already in the SPA) feeds **both** this app's goal/inventory forms and the
marketplace listing form. **Build this first** — it's the dependency both apps
sit on.

### 2. Inventory (per-member ledger)

`inventory` rows: `(id, owner_id, item_id, qty, location, note, updated_at)`.

- A row is one member's **holding** of one item at a location — a general pledge.
- `location` is free text (e.g. "Area18 hangar", "Hull-A onboard") — useful for
  logistics but not validated; it autocompletes from the POI dataset (v1.1).
- `unit` is denormalized off the catalog at write time but **member-overridable**
  from a short allow-list (`catalog.UNITS`) — fauna parts / gear count "each", not
  "SCU" (v1.1).
- The member owns their rows (create/edit/delete); admins can see all and adjust.
- **Org inventory view** (`#/inventory`) = `derive_inventory_rollup`: total per
  item across all members (each holding counted **once**), expandable to
  per-member/per-location breakdown.

> **v1.1 model change (2026-06-25):** a goal contribution is **not** a duplicate
> inventory row. It's an **allocation drawn from a holding** — see Goals below.
> The old `inventory.goal_id` column is retired (a one-time migration converts any
> legacy goal-tagged rows into holding + allocation pairs).

### 3. Goals (targets with deadline + priority)

`goals` rows mirror the `events` table shape:
`(id, creator_id, title, description, priority, deadline, status, created_at)`
plus a JSON `line_items` blob `[{item_id, qty_needed}]` (same pattern as
`events.roles`).

- **priority** INTEGER 1–10, 1 = highest; default sort is priority asc then
  deadline asc.
- **deadline** UTC ISO8601, rendered local (same util the event planner uses);
  nullable for open-ended goals.
- **status** `active` | `met` | `archived`; auto-flips to `met` when every line
  item's contributions ≥ need (display-only; admin can reopen).
- **Contributions** are **allocations drawn from a holding** (v1.1), tracked in an
  `inventory_allocations` table `(id, inventory_id FK, goal_id, qty, …)`. Committing
  30 of a 50-unit holding records one allocation (qty 30) and leaves the holding's
  `available = qty − Σ allocations` at 20 — never a second ledger row, so the org
  rollup never double-counts. `POST /api/goals/{id}/contribute` finds-or-creates the
  member's (item, location) holding, tops it up only if they commit more than they'd
  declared on hand, then adds/tops-up the allocation. "My holdings" renders each
  holding with its commitments nested (parent→child) and a "free" remainder.

`derive_goal_progress(goal, inventory_rows)` (pure, tested) returns per-line
`{item_id, needed, have, pct}` + an overall `pct` (the rule the tests pin:
overall = total have / total needed across lines, capped at 100%), plus a
`per_contributor` breakdown for the accountability view.

---

## Endpoints (mirroring existing conventions)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/catalog?q=` | item search; feed items + custom. Shared with marketplace. |
| `POST` | `/api/catalog` | add a custom item (admin or any member — TBD, default member). |
| `GET` | `/api/inventory` | org rollup; `?owner=me` for mine (holdings + nested allocations), `?goal=<id>` for a goal's contributions. |
| `POST` | `/api/inventory` | log/adjust my **holding** `{item_id, qty, unit?, location?, note?}` (no goal — see contribute). |
| `PATCH` | `/api/inventory/{id}` | edit qty/location/note/unit (owner-or-admin); qty can't drop below committed (v1.1). |
| `DELETE` | `/api/inventory/{id}` | owner-or-admin; withdraws any allocations drawn from it. |
| `POST` | `/api/goals/{id}/contribute` | commit `{item_id, qty, location?}` as an allocation drawn from my holding (v1.1). |
| `GET` | `/api/goals` | list, sorted priority↑ then deadline↑; `?status=`. |
| `POST` | `/api/goals` | create (admin, or any member — configurable like events). |
| `PATCH` | `/api/goals/{id}` | creator-or-admin; edit fields/line items/status. |
| `DELETE` | `/api/goals/{id}` | creator-or-admin. |
| `GET` | `/api/goals/{id}` | detail incl. `derive_goal_progress` + per-contributor. |

JSON-blob columns for `line_items`; the stateless math (`derive_goal_progress`,
`derive_inventory_rollup`) lives in `nav_core.py` and is unit-tested before the
endpoints land — same build order as the cargo/event planners.

---

## UI (`#/goals` + `#/inventory`, launcher card)

- **Launcher** — one new card at `#/` ("Org Inventory & Goals").
- **`#/goals`** — board of goal cards sorted by priority/deadline. Each card: title,
  a **priority chip** (color-ramped 1→10), a **deadline countdown** ("4 days left"
  / overdue in red), and an **overall fill bar**. Reuses the event-planner chip +
  fill-bar CSS.
- **Goal detail** — per-line-item fill bars (`Titanium 320/500 SCU`), a
  **"contribute" form** (item is prefilled per line; qty + optional location), and
  a **per-contributor breakdown** (the accountability payoff). Each line item has a
  **"find in navigator"** link → `#/nav` resource finder for that commodity.
- **`#/inventory`** — the org rollup table (item, total, # holders), each row
  expandable to per-member/per-location. Filter `mine` vs `all`.
- CSS in the spirit of `#/stats` / `#/events` (hand-rolled, no chart lib).

---

## Navigator integration (the org-specific hook)

A goal line item is a procurement *intent*; the navigator is the org's resource
*map*. The link goes both ways:

- **Goal → map**: each line item deep-links into the resource finder for that
  commodity (`#/nav` with the commodity preselected), so a member reading the goal
  can immediately go gather it.
- **Map → goal (deferred)**: later, the finder could badge a commodity with "needed
  for 2 active org goals," turning routine mining into goal progress. Noted under
  *Deferred*.

This is why the app is org-specific and lives *inside* this webapp rather than
being a generic spreadsheet.

---

## Build order

0. **Item catalog** — `catalog_items` table + `/api/catalog` search over feeds +
   custom. (Shared dependency; the marketplace blocks on this too.)
1. **Inventory ledger** — `inventory` table, `/api/inventory`,
   `derive_inventory_rollup` + tests, `#/inventory` view.
2. **Goals** — `goals` table, `/api/goals`, `derive_goal_progress` + tests,
   `#/goals` board + detail, contribute form (reuses the inventory insert).
3. **Navigator deep-link** — goal line item → resource finder.

---

## Deferred (cheap paths noted)

- **Map → goal badging** — finder shows "needed for N goals" per commodity.
  Needs only a `GET /api/goals?item=<id>` reverse lookup + a badge in the finder.
- **Goal completion → Marketplace** — when a goal is `met`, surplus general
  inventory can be one-click listed on the marketplace (the two apps share the
  catalog and the `inventory` rows already, so this is a UI bridge, not new data).
- **Contribution history / leaderboard** — "top org contributors this month,"
  patterned off `derive_guild_leaderboard`. The attributed ledger already holds
  the data.
- **Discord announce** — same deferred hook the event planner has; ping a channel
  when a high-priority goal is created or its deadline nears.
- **Goal templates** — "fund a `<ship>`" prefills line items from a known BOM.

---

## Relevant code

- `server/nav_core.py` — add `derive_goal_progress` + `derive_inventory_rollup`
  (pure, unit-tested); pattern off `derive_event_fill` / `derive_run_stats`.
- `server/db.py` — `CREATE TABLE IF NOT EXISTS` + `_ensure_column`; new
  `catalog_items`, `inventory`, `goals` tables keyed on `discord_id` / `owner_id`.
- `server/app.py` — `require_session` / `require_admin`; catalog served like
  `/api/ships` over `load_all_commodities` + `load_ships` + custom rows; JSON-blob
  `line_items` as in `events.roles` / `/api/route/*`.
- `server/static/index.html` — launcher card + `#/goals` and `#/inventory` views
  as branches in `applyView()`; priority chips + fill bars reuse the event CSS.
