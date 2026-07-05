# Blueprint craft commissions — design (backlog #25)

**Status:** designed 2026-07-04; **not built.** Data source is the SC Wiki API
`/api/blueprints` (backlog #26); an early datamined extract used during scoping
has since been removed from the repo.

A member posts a **craft request**: "build me this item, to this quality spec,
for this price" — and an org crafter takes the job. It is the marketplace's
**fourth listing mode** (`commission`), not a new app: same board, same offers
table, same dual-confirm handshake, same aUEC-only rule. What's genuinely new is
the **blueprint reference feed** — the recipe reference data that lets the app
show exactly what a job takes (materials, SCU, craft time) and which input
material's *quality* drives which finished stat.

This is also the deferred marketplace **"WTB" item** (docs/marketplace.md,
deferred list) finally landing — specialized to the one case where a want-to-buy
post carries real structure: a craftable item with a recipe and a quality spec.

**Out of scope (hard, same as the marketplace):** real-money anything, escrow
(SC has none — this stays a coordination board), automated delivery, and
anything that pretends the app can verify a crafted item's actual in-game
stats. The spec is an *agreement*, the confirm is a *handshake*; trust is social.

---

## 1. The data (raw mine + the SC Wiki blueprint API)

> **Update (2026-07-04): the Star Citizen Wiki's open API is the primary (and
> only) source.** See §1b — the raw mine turned out to be missing every
> *item-kind* ingredient (crafting gems), and the wiki adds slider semantics,
> dismantle data, and blueprint-unlock missions. The raw mine has since been
> **removed from the repo**; §1a below is retained as design history — it records
> the shape facts (categories, modifier vocabulary, dup-key handling) that were
> verified against the mine and still inform the feed.

### 1a. The datamined mine (removed — design history)

The original extract lived in `docs/datamined_data/blueprints_data/` (now removed
from the repo; UTF-8 **with BOM**, `utf-8-sig`). What it contained, and the shape
facts derived from it that still inform the feed:

- **`blueprints.json`** (~10 MB, the master) — 1,561 blueprint records. One is a
  no-tier placeholder (`GlobalGenericDismantle`) → dropped; **1,560 usable**.
  Each: `blueprint` (unique id, e.g. `BP_CRAFT_AMRS_LaserCannon_S1`),
  `item_name`, `item_key`, `category`, `process` (always
  `CraftingProcess_Creation`), `entityClass` (p4k path — drop at distill), and
  `tiers[]` (today always exactly one tier, `tier: 0`) carrying `craftTimeSec`
  and `inputs[]`: `{slot, resource, scu, minQuality, quantityMultiplier,
  modifiers[]}`. `optionalInputs` is always empty in this mine.
- **`blueprints.csv`** (3,886 rows) — the same inputs flattened one row per
  (blueprint, slot, resource). Convenience view; the JSON is canonical.
- **`modifiers.csv`** (6,485 rows) — flattened modifier ranges: for a given
  blueprint input, how that input resource's **quality (0–1000)** maps to a
  finished-item property. `mode` is `multiplier` (5,887 rows, e.g. Damage
  Mitigation ×0.85→×1.15 across Q0→Q1000) or `additive` (598 rows, all
  Power Pips). Some properties have up to 7 piecewise ranges (`rangeIndex`).
- **`resources.csv`** (26 rows) — per-resource rollup: recipe count, SCU
  min/avg/max, categories it feeds. The 26 input resources are a mix of
  classic ores (Agricium, Titanium, Gold, …) and new 4.x crafting materials
  (Aslarite, Ouratite, Lindinium, Stileron, Savrilium, …).

**Shape facts that drive the design** (verified against the mine):

- **`blueprint` is the only unique key.** `item_key` collides 23× and
  `item_name` 37× — the mine contains aliased recipes (e.g. four blueprints all
  named "Cryo-Star SL" across component sizes; recolor variants like
  `ksar_rifle_energy_01_blue_gold` sharing an `item_key`). The feed keys on
  `blueprint`; the UI disambiguates dup names with the category (size) chip.
- **Categories** (14): `FPSArmours` (dominant), `FPSWeapons`,
  `VehicleComponentS0–S4`, `VehicleWeaponsS1–S6`, `MissionItem`.
- **`minQuality`** on an input is almost always 0 or 1 (i.e. "any"), but a
  handful of recipes demand Q500–Q900 inputs — the manifest must surface it.
- **Craft time** ranges from 10 s (a magazine) to hours (large components) —
  worth showing on the request card.
- **Modifiers are the killer feature**: the data says *which slot's material
  quality drives which finished stat* ("Damage Mitigation rides the Armored
  Carapace / Ouratite quality; Min/Max Temp ride the Insulative Liner /
  Aslarite"). That's exactly the vocabulary a requester needs to write a spec
  ("I care about Damage Mitigation → I need high-Q Ouratite") and a crafter
  needs to quote it.
- A compact distilled feed (drop `entityClass`, `blueprintName`, flatten the
  single tier) is **~1.2 MB**; a name/category search index alone is ~140 KB.

### 1b. The Star Citizen Wiki blueprint API (primary source — DECISION LOCKED)

**Locked 2026-07-04:** the wiki API is the source; the raw mine has been removed
(§1a kept only as design history). Rationale:
it matches the project's existing reliance on maintained third-party data
(uexcorp commodities/ships/terminals) instead of self-maintained extraction;
and since the project is non-commercial by rule anyway (donations at most,
per docs/monetization-and-deployment.md), owning the data pipeline buys
nothing — minimizing maintenance is the priority.

`https://api.star-citizen.wiki/api/blueprints` — open JSON API, no auth.
Paginated list (`page[size]` up to 200 → 8 pages for all **1,559** blueprints —
effectively the same set as the mine's 1,560) + a per-blueprint detail endpoint
(`/api/blueprints/{uuid}`, ~70 ms). Verified 2026-07-04 against the Omnisky III
Cannon and the Novikov Helmet; records are stamped `game_version:
"4.8.2-LIVE.12030094"`, so the source tracks game patches **without us
re-mining**.

**What it fixed — the mine is missing item-kind ingredients.** A recipe's
inputs come in two kinds: `resource` (ore/refined material, measured in SCU)
and **`item` (a countable component — crafting *gems* like Hadanite, Dolivine,
measured in units)**. The mine only captured resource-kind inputs: it shows the
Omnisky III as Frame/Agricium 0.36 SCU only, while the real recipe is Frame
(0.36 SCU Agricium) + **Emitter (7× Hadanite)** + **Aperture Iris (7×
Dolivine)**. No gem appears anywhere in the mine's 26-resource list, so every
gem-using recipe's materials bill is undercounted there. This alone decides the
source question.

**What it adds beyond the mine:**

- **Slider semantics made explicit** (`aspects`): each requirement group
  (Frame / Emitter / Aperture Iris on weapons; Shell / Insulative Liner on
  armor) is an *aspect* with its own quality slider (0/1–1000, initial 500) —
  the slider **is the quality of the input material you feed that group**.
  This is exactly the wiki site's slider UI, and it's the model our spec
  builder should mirror: **quality is chosen per aspect, not per item.**
- **Modifier interpolation**: each group modifier declares
  `value_range_type: linear` with `modifier_range` (`at_min_quality` →
  `at_max_quality` across `quality_range`), or piecewise `value_segments`
  (the mine's multi-`rangeIndex` properties — Power Pips, Integrity, Coolant
  Rating…). At the initial Q500 a symmetric linear modifier sits at ×1.0 =
  the item's base stat.
- **Same-property stacking**: on the Omnisky III *both* Emitter and Aperture
  Iris modify Impact Force (×0.95–×1.05 each) — two sliders, one stat, so the
  effects compose (combined ×0.9025–×1.1025; multiplicative composition is the
  natural reading of independent multipliers — verify against the wiki UI).
- **Direction + display metadata**: `better_when` (higher/lower) per property,
  `unit_format` (e.g. `%+.2f %%` — rendered as a percent delta off base).
- **Choice groups**: `selection_group` / `is_choice_group` / `option_count`
  in the aspect model — the schema supports "pick one of N alternative
  inputs" (neither sample uses it; handle `option_count > 1` gracefully).
- **Blueprint acquisition**: `is_available_by_default` +
  `unlocking_missions[]` (mission title, chance, grouped "Guaranteed"/chance
  buckets) — i.e. *how a crafter gets this blueprint*. Gold for the
  commission detail view and the v1.1 member-blueprint library.
- **Dismantle data**: `dismantle` (time, efficiency 0.5) + per-resource
  `dismantle_returns`.
- **Stable identity + cross-links**: blueprint `uuid`, output item `uuid`,
  resource/commodity UUIDs with API links (`/api/commodities/{uuid}`) — a
  cleaner join key toward price data than name matching.

The mine's `blueprint` id survives as the wiki's `key` field
(`BP_CRAFT_AMRS_LaserCannon_S1`), so **`blueprint_key` and the
`blueprint:<key>` catalog namespace are unchanged** — the wiki UUID is stored
alongside, but the key stays the stable, human-greppable identifier.

**Usage terms (SETTLED 2026-07-04, from
`api.star-citizen.wiki/developers` + the OpenAPI spec):**

- **Credit `api.star-citizen.wiki` in public projects** — required. Ship the
  credit next to the existing uexcorp/fansite attributions (footer + the
  blueprint picker/manifest panel).
- **Commercial use is not permitted** (per the RSI Fandom FAQ) — a non-issue:
  this project is already hard-committed to non-commercial under CIG's fan
  rules (docs/monetization-and-deployment.md).
- **Rate limits:** only *search* endpoints are limited (60 req/min/IP; image
  search 10/min). List + detail endpoints have no stated limit — the sync
  script throttles politely anyway (it's offline and infrequent).
- **Version pinning:** game-data endpoints accept `?version=` (see
  `GET /api/game-versions`); omitting it uses the current default. The sync
  script pins the version it fetched into the artifact for reproducibility.
- Full OpenAPI spec: `GET /api/openapi` (YAML); interactive docs at
  docs.star-citizen.wiki; source github.com/StarCitizenWiki/API; health `/up`.

---

## 2. Why this fits the existing system

- **Marketplace mode #4, not a new table.** The marketplace was explicitly built
  as "one `listings` table, a `mode` discriminator, one child `listing_offers`
  table for *anyone responded*" (docs/marketplace.md). A commission is a listing
  where the poster wants an item *made* instead of *sold* — offers, accept,
  pending, dual-confirm, completed-deals reputation, board denorm columns,
  paging/filters all reuse as-is.
- **The quality vocabulary already exists.** Listings already carry an
  `attributes` crafted-quality blob (`CraftedIn`: quality 1–1000, band 1–8,
  stats[]) with board filters `min_quality`/`band`/`stat`, and the app already
  renders resource quality bands B1–B8 (resource-navigator). A commission spec
  is the *same shape pointed the other way*: requested-quality instead of
  as-built-quality.
- **The distill pipeline convention exists.** The quantum work
  (docs/quantum-data-pipeline.md) established: a committed offline sync/distill
  script fetches the wiki API and emits small artifacts into `poi/`
  (un-gitignored per-file); the server lazy-loads those. Blueprints follow the
  identical path.
- **Identity, gate, notify** — same Discord auth gate; the `marketplace` notify
  category already exists with `_notify_market_*` helpers for offer / accepted /
  confirm-needed / completed.
- **Material sourcing hooks in later.** The blueprint manifest names resources +
  SCU; `/api/raw_commodities` already has raw-material prices for an estimated
  material cost, and the Resource Navigator knows where ores live. Both are
  cheap follow-on integrations, not v1 blockers.

---

## 3. The commission model

### Roles — poster stays `seller_id` (read: "owner")

In sale/auction/barter the listing poster delivers goods and receives aUEC. A
commission inverts the money/goods flow: the **poster pays** and **receives**
the item; the **crafter delivers** and **gets paid**. Two possible column
mappings; we keep the *ownership* invariant, not the money-direction one:

- `seller_id` = the **requester** (poster). Every existing permission check —
  edit, cancel, accept-an-offer, "My listings" — keys on `seller_id`, so the
  whole lifecycle machinery works unchanged.
- `buyer_id` = the **crafter** whose offer was accepted (set at accept, exactly
  like today).

Only display copy is role-aware (the detail view says "Requester" /
"Crafter" instead of "Seller" / "Buyer" when `mode == 'commission'`).
Completed-deals reputation counts both sides, unchanged.

### Lifecycle (identical state machine)

```
open ──(crafter offers; requester accepts one)──► pending
pending ──(both confirm the in-game handoff)──► completed
open|pending ──► cancelled          (requester or admin; accepted crafter can
                                     withdraw → listing returns to open)
open ──(needed-by date passes)──► expired      (lazy, on read — like auctions)
```

- **Offer** = a crafter's quote: `amount_auec` (their price — may differ from
  the posted budget) + `offer_note` (ETA, proposed quality, material notes).
  Multiple crafters can quote; requester picks one. `OfferIn` needs zero changes
  for v1 (the quote's promised quality rides the note; a structured
  `promised_quality` field is a v1.1 nicety).
- **Accept** → `pending`, others → `lost` — existing code path.
- **Dual-confirm** — both parties confirm the in-game handoff (item + aUEC),
  existing `/confirm` endpoint. `final_auec` freezes the accepted quote, so
  market stats include commissions for free.
- **Withdraw-after-accept**: today a bidder can only withdraw an *active*
  offer. For commissions the accepted crafter must be able to bail ("can't
  source the Riccite") — new mode-aware rule: withdrawing an **accepted** offer
  on a commission flips the listing back to `open` (offers that were `lost`
  stay lost; the board renotifies). Small, contained change in the offer PATCH.

### The request spec (what "to their specifications" means)

Stored in the existing `attributes` JSON blob (free-form by design), under a
`spec` key so a future "as-delivered" annotation can sit beside it:

```json
{ "spec": {
    "quality":  700,            // target overall quality 1–1000 (optional)
    "band":     6,              // or/and a band 1–8 (optional)
    "stats":   [{"name": "Damage Mitigation", "value": "≥ 1.10x"}],  // ≤12 rows
    "materials": "crafter"      // who sources inputs: requester|crafter|split
}}
```

- `quality`/`band`/`stats` reuse `CraftedIn` validation verbatim (`_clean_crafted`
  wrapped under the `spec` key for this mode).
- `materials` is the one genuinely new field — it changes the price of the job
  more than anything else, so it's first-class in the UI and a board chip:
  **"materials incl."** vs **"you supply mats"** vs **"split"**.
- The stat rows are free-text values on purpose (same as today's crafted
  stats): the app *suggests* stat names from the blueprint's modifiers, but
  never pretends it can enforce them.

### Quantity, budget, deadline

- `qty` — existing column ("craft me 4 of these"). The materials manifest
  multiplies by qty.
- `price_auec` — the requester's **budget** (optional; empty = "open to
  quotes"). Reuses the sale column; `sort_price` denorm = budget or best quote,
  mirroring the auction rule.
- `ends_at` — optional **needed-by** date, reusing the auction column. The lazy
  expiry sweep becomes mode-aware: a lapsed commission just goes `expired`
  (no winner derivation).

---

## 4. Blueprint reference feed

### Sync (offline, committed — same *pattern* as the quantum sync script)

`sync_blueprints.py` (committed, run offline) fetches from the **SC Wiki API**
(§1b):

1. Page the list (`page[size]=200`, 8 pages), then fetch each blueprint's
   detail (~1,559 calls at ~70 ms — a few polite minutes; cache responses
   locally so a re-run only refetches changes).
2. Distill each detail into a compact record keyed by the blueprint **`key`**
   (the mine's id, unchanged), keeping: name, category, craft time, game
   version, uuid, `is_available_by_default`, unlock-mission summaries,
   dismantle efficiency/returns, and per-aspect inputs — **both kinds**:

```json
{ "BP_CRAFT_AMRS_LaserCannon_S1": {
    "uuid": "280f47b7-…", "name": "Omnisky III Cannon",
    "cat": "VehicleWeaponsS1", "time_s": 540, "ver": "4.8.2",
    "default": false, "unlocks": ["Tactical Strike Group Needed (100%)"],
    "aspects": [
      { "slot": "Frame", "kind": "resource", "input": "Agricium",
        "scu": 0.36, "min_q": 1,
        "mods": [ { "prop": "Integrity", "dir": "higher",
                    "ranges": [{"q0":0,"q1":1000,"v0":0.9,"v1":1.1,"mode":"multiplier"}] } ] },
      { "slot": "Emitter", "kind": "item", "input": "Hadanite", "qty": 7,
        "mods": [ { "prop": "Impact Force", "dir": "higher",
                    "ranges": [{"q0":0,"q1":1000,"v0":0.95,"v1":1.05,"mode":"multiplier"}] } ] },
      { "slot": "Aperture Iris", "kind": "item", "input": "Dolivine", "qty": 7,
        "mods": [ "…same as Emitter…" ] } ] } }
```

3. Emit **`poi/blueprints.json`** (committed / un-gitignored) + a coverage
   report (counts per category, records diffed vs the raw mine as a sanity
   cross-check, choice-group occurrences) like `quantum_match_report.txt`.

On a game patch: re-run the sync, review the diff, commit. **No re-mining and
no server code changes.** The raw mine stays in the repo as the independent
cross-check that caught nothing the API has — and vice versa (it's how we
found the missing-gems gap).

### Serving (lazy-load + in-memory index, like every other reference feed)

- `GET /api/blueprints?q=&category=` — the **search index only** (id, name,
  category, craft time, input-resource summary, has-stat-mods flag). Paged or
  capped (~50 rows) — 1,560 names filter fine server-side; never ship 1.2 MB to
  the client.
- `GET /api/blueprints/{bp_id}` — one full record: inputs, SCU, min quality,
  modifiers. This is what the spec-builder UI renders.
- Loaded lazily on first request, cached in module memory (the file is static
  per deploy). Auth-gated like the other reference endpoints.

### Catalog identity

A commission's `item_id` uses a new catalog namespace: **`blueprint:<bp_id>`**,
resolved against the feed by `resolve_catalog_item` (name/unit stamped
server-side, unit `each`) — exactly how `commodity:`/`ship:`/`custom:` work
today. Blueprint items (guns, armor pieces, coolers…) mostly don't exist in the
current catalog, and this keeps them out of the inventory/goals pickers until
we deliberately want them there. `listings.item_name` stays denormalized at
write time, so a future mine that renames an item can't strand old rows.

---

## 5. Data model changes (small)

`listings` gains two columns via `_ensure_column` (no migration pain):

- `blueprint_key TEXT` — the `BP_CRAFT_…` id (also recoverable from `item_id`,
  but a real column keeps board SQL/filters clean).
- `materials TEXT` — `requester | crafter | split` (commission mode only).

Everything else rides existing columns as described above (`attributes.spec`,
`price_auec` = budget, `ends_at` = needed-by, `final_auec` = agreed quote).
`_LISTING_MODES` gains `commission`; `_validate_listing` gets a mode branch
(budget optional, blueprint required + must resolve, spec cleaned via the
`CraftedIn` path, materials enum). `listing_offers` is unchanged in v1.

---

## 6. Pure logic in `nav_core.py` (unit-tested first, as always)

- `blueprint_manifest(bp, qty)` — aggregate the materials bill across **both
  ingredient kinds**: per resource, total SCU (× qty); per item (gems), total
  count (× qty); plus the max `min_q` demanded and which slots consume each.
  Powers the manifest panel and the Discord post.
- `blueprint_stat_drivers(bp)` — invert aspects→modifiers into
  `stat → [(slot, input, effect-range, direction)]`, the spec-builder's
  vocabulary ("Damage Mitigation ← Shell (Stileron): ×0.85–×1.15"). Where
  several aspects drive one stat (Omnisky: Emitter + Aperture Iris → Impact
  Force), the combined range multiplies across aspects.
- `blueprint_quality_effect(mod_ranges, q)` — piecewise interpolation of a
  modifier at input quality `q`, handling multi-segment properties and
  `additive` vs `multiplier` modes; a companion combines same-stat modifiers
  across aspects (multiplicative). Powers the "at Q700 you'd get ≈ ×1.09"
  hint in the spec builder — clearly labeled an estimate.
- `commission_board_state(listing, offers)` — best-quote derivation for the
  board card (mirrors `derive_auction_state`'s role; much simpler — no
  tie-breaks, requester picks manually).

Tests pin: manifest aggregation across duplicate resources + qty, min-quality
max-wins, stat-driver inversion incl. a multi-range property, interpolation at
range boundaries, additive mode (Power Pips), expiry-not-auction behavior.

---

## 7. API surface (delta only)

| Verb | Route | Change |
|---|---|---|
| `GET` | `/api/blueprints` | **new** — search index (`?q`, `?category`). |
| `GET` | `/api/blueprints/{bp_id}` | **new** — full recipe + modifiers. |
| `POST` | `/api/market` | accepts `mode: "commission"` (+ `blueprint_key`, `materials`, `spec` via `crafted`-style body field). |
| `GET` | `/api/market` | `?mode=commission` already works once the mode exists; card serializer adds spec/materials chips. |
| `POST` | `/api/market/{id}/offer` | unchanged (quote = amount + note). |
| `PATCH` | `/api/market/{id}/offer/{oid}` | withdraw-after-accept → listing back to `open` (commission only). |
| `PATCH` | `/api/market/{id}` | edit/cancel — mode-aware field set (budget, needed-by, spec, materials). |
| `POST` | `/api/market/{id}/confirm` | unchanged. |

---

## 8. UI (`#/market`, no new app)

- **Board**: a **Requests** filter tab beside Sale/Auction/Barter. Commission
  cards read differently on purpose: "**WANTED** · Omnisky III Cannon ·
  Q700+ · budget 45,000 · materials incl. · ends in 3d" with a distinct chip
  color (the mode-chip system already exists).
- **New-listing form**: the mode toggle grows a **Craft request** option →
  blueprint autocomplete (over `/api/blueprints`, disambiguating dup names with
  the category chip) → the **spec builder**:
  - **Materials manifest** (from `blueprint_manifest`): resources (SCU × qty)
    *and* gem/item ingredients (count × qty), min-quality flags, craft time,
    and the materials-sourcing 3-way toggle. With "requester supplies", this
    doubles as the shopping list.
  - **Stat spec** (from `blueprint_stat_drivers`): the blueprint's actual
    tunable stats as suggested rows — pick "Damage Mitigation", the UI shows
    *which* aspect/input drives it and the Q0→Q1000 effect range, with a
    **per-aspect quality slider** (mirroring the in-game/wiki model — initial
    500 = base stats) previewing the interpolated, cross-aspect-combined
    effect (`blueprint_quality_effect`).
  - **Blueprint availability** (from the feed): "unlocked by default" or the
    unlock-mission list — so a requester knows how rare the ask is before
    posting, and a crafter can see what it takes to learn the recipe.
  - Target overall quality/band + budget + needed-by + note.
- **Detail view**: role-aware labels (Requester/Crafter), the spec + manifest
  rendered read-only, quotes list (existing offers UI), accept → pending →
  dual-confirm (existing). Accepted crafter gets a **Withdraw from job** button.
- **aUEC-only banner** unchanged and prominent — a commission is still an
  in-game, in-person, trust-based handoff.

---

## 9. Discord notifications (reuse `marketplace` category)

- **New request posted** — opt-in `announce` flag like LFG/pirates: "🛠️ WANTED:
  Omnisky III Cannon (Q700+), budget 45k, materials included — 3 days" +
  deep link. Rate-limited via the existing announce plumbing.
- Offer received / accepted / confirm-needed / completed — the four existing
  `_notify_market_*` helpers, with mode-aware copy ("quote" not "bid",
  "crafter" not "buyer").

---

## 10. v1.1 — member blueprint library ("who can craft this?")

In-game, a player must *own* a blueprint to craft it. A small follow-on makes
matching real instead of broadcast:

- `member_blueprints` table (`member_id`, `blueprint_key`, `added_at`) + a
  "My blueprints" picker in the profile/settings area (autocomplete over the
  feed; bulk-add by category).
- Board: "**3 members** can craft this" on request cards; a **"requests I can
  craft"** filter for crafters (the LFG suggested-matches pattern, `✨`).
- Notify: the announce ping can name-check capable crafters (or DM-style
  per-category webhooks stay org-wide — decide with users).

Deliberately not v1: it's a second table + UI surface, and the feature works
socially without it.

---

## 11. Ripple: what the data does for the *existing* sell/buy quality model

*(added 2026-07-04, after the question "does the modifier data change the
quality data points we already collect on sale/auction listings?")*

Today's crafted-quality annotation (`CraftedIn` → `attributes`) is deliberately
free-form: hand-typed overall quality 1–1000, band 1–8, and up to 12 free-text
`{name, value}` stat rows, filtered by `min_quality`/`max_quality` (JSON
extract), exact `band`, and a substring `LIKE` over stat names *and* values.
That shape was chosen when the in-game model was unknown. The mine doesn't
invalidate any of it — the 0–1000 scale matches, free-form survives — but it
upgrades several parts from "blind" to "informed":

1. **The big conceptual shift: overall quality is a lossy summary.** Different
   input slots drive different finished stats (Ouratite→Damage Mitigation,
   Aslarite→Temp range on the same armor). Two Q700 items can differ materially
   depending on *which* input was high quality. So per-stat rows — not the
   headline quality number — are the real comparable, and the sell-side UI
   should treat them that way (stat chips on cards, stats above quality in the
   detail view).
2. **Canonical stat vocabulary (cheapest, highest value).** The mine yields
   ~22 canonical stat display names ("Damage Mitigation", "Coolant Rating",
   "Quantum Fuel Burn"…). Free-text names fragment the `?stat=` filter today
   ("dmg mit" never matches "Damage Mitigation"). Fix: the crafted-stats form
   suggests names from the blueprint feed — per-item once the listing links a
   blueprint, global list otherwise. Form-side autocomplete only; no schema
   change, and the existing substring filter immediately gets sharper data.
3. **`blueprint:` identity for sale/auction listings too.** Crafted components
   listed for sale today land on `custom:` catalog items — every seller mints
   their own, so nothing is comparable across listings. Letting the sale form
   pick from the blueprint feed (same `blueprint:<bp_id>` namespace as
   commissions) unifies crafted-goods identity: exact-item search, a "crafted"
   `kind` filter, cross-listing price comparison, and the commission→resale
   loop all fall out. This is the enabling move for everything analytics-shaped.
4. **Auto-estimated stat panel.** For a listing with a blueprint link + an
   overall quality, the detail view can render the *expected* stats by
   interpolating the modifiers at that quality (`blueprint_quality_effect`) —
   seller types one number, buyers see a full labeled-estimate stat sheet.
   Caveat stated in-UI: assumes uniform input quality (see §12 open question).
5. **Plausibility nudges.** The mine bounds each stat's possible range
   (e.g. Damage Mitigation caps at ×1.15). A claimed ×1.4 can get a soft
   "outside the possible range for this recipe" hint at listing time — a
   nudge, never a block (the app can't verify in-game stats and doesn't
   pretend to).
6. **Price↔quality market intelligence (needs #3 first).** With shared item
   identity and `final_auec` already frozen at completion, "Q700+ Omnisky III
   recently sold for ≈45k" becomes a pure listings query — a pricing hint for
   sale forms *and* commission budgets.
7. **Numeric stat values — deferred.** Values stay free text for now.
   The feed's mode/range data would support structured numeric capture and
   true numeric stat filters/sorts ("Coolant Rating ≥ 1.05"), but that's a
   bigger form + filter change; revisit once #2/#3 have bedded in.
8. **Band stays as-is.** The mine is pure 0–1000 with no band table, so the
   band⇄quality mapping remains unverified; keep the two independent fields.

Sequencing: #2 and #5 are form-side sugar that could ship *with* commission
step 3 (they reuse the same feed + helpers); #3 is a small deliberate decision
(one more kind prefix + picker source) best made when step 2 lands; #4 rides
`blueprint_quality_effect` for free; #6/#7 are post-v1.

---

## 12. Deferred / open questions

- **Estimated material cost** — `blueprint_manifest` × raw-commodity prices
  (`/api/raw_commodities`) → a "mats ≈ 12,400 aUEC" hint on the form and card.
  Cheap and high-value; first candidate after v1. The wiki API's resource →
  commodity UUID cross-links (§1b) may make the mapping exact instead of
  name-matched; gem/item ingredient prices still need a source (uexcorp? the
  wiki's own item endpoints?). Degrade to "n/a" per unmapped input.
- ~~**Wiki API terms/attribution**~~ — **RESOLVED** (§1b): credit
  `api.star-citizen.wiki`, non-commercial only (already our posture), search
  is the only rate-limited surface, `?version=` pinning available. Remaining
  decision is just sync cadence (manual per-patch re-run is the default,
  matching quantum).
- **Runtime background refresh (v2 maintenance option)** — the committed
  `poi/blueprints.json` still needs a manual sync re-run per game patch. The
  uexcorp feeds already solve this shape at runtime (fetch on a timer, cache
  to `poi/`, serve stale on failure); the same pattern pointed at the wiki
  API (`/api/game-versions` default-version check → re-pull details only
  when the version changes, a page at a time) would remove even that manual
  step. More code + failure modes, so not v1 — but it's the logical endpoint
  of the minimize-maintenance rationale that picked the wiki source.
- **Overall-quality math** — the wiki confirms quality is *chosen per aspect*
  (one slider per input group), and stat effects interpolate per aspect — but
  how the finished item's single headline "quality/band" derives from the
  slider positions is still unverified. Until then the UI treats overall
  target quality as an agreement between parties, per-stat hints as
  per-aspect estimates, and cross-aspect stacking as multiplicative (verify
  against the wiki's slider UI).
- **Choice groups** — the aspect schema supports pick-one-of-N alternative
  inputs (`is_choice_group`); no sampled blueprint uses it yet. The sync
  report counts occurrences; the spec-builder needs a picker only if/when
  they appear.
- **Recipe drift on game patches** — an open request whose blueprint vanishes
  from a re-synced feed keeps working (denormalized name + spec); the detail
  view shows "recipe data unavailable" instead of the manifest.
- **Structured quotes** — `promised_quality` on offers, so accept can compare
  quotes on more than price (v1.1+).
- **Inventory bridge** — "requester supplies materials" could earmark actual
  inventory holdings against the job (allocation pattern from goals). Real
  design work; park it.
- **Crafted-item resale loop** — a completed commission could one-click list
  the item back on the board with its `attributes` as-built quality. Cute,
  later.
- **MissionItem category** (4 recipes) — include or filter from the picker?
  Trivial either way; default include.

---

## 13. Build order

0. **Sync + feed** — `sync_blueprints.py` (SC Wiki API, §1b) →
   `poi/blueprints.json`; `/api/blueprints` index + detail endpoints;
   `blueprint:` catalog namespace. *(No UI yet; testable with curl.)*
1. **nav_core helpers + tests** — manifest, stat drivers, quality
   interpolation, board state. Suite green before any endpoint work.
2. **Commission mode end-to-end** — `_ensure_column` ×2, `_LISTING_MODES` +
   `_validate_listing` branch, mode-aware expiry + withdraw-after-accept,
   board tab + card + detail with role-aware copy, minimal form (blueprint
   picker + budget + materials toggle + note). *Ship the thin slice.*
3. **Spec builder** — manifest panel, stat-driver rows, quality-effect
   preview, needed-by.
4. **Discord** — announce flag + mode-aware copy in the four market helpers.
5. **v1.1** — member blueprint library + can-craft matching (own release).

Steps 0–2 are the MVP; 3 is where the blueprint reference feed visibly pays off;
each step deployable alone, per house style.
