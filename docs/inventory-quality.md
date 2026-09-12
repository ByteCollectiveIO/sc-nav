# Inventory item quality (#151)

Status: **🔨 step 1 (holding identity) built 2026-09-12 · steps 2–5 designed** — researched against SC Alpha 4.10.

## The problem

A holding in the Resource Manager is `(owner, item, location)` and nothing
else. Two lots of the same material at different qualities are a single row,
and logging the second one **overwrites** the first (`db.upsert_inventory`
SETs qty on a key match, it doesn't sum). As of 4.10 the game keeps quality
on the commodity in inventory and stacks each quality value separately, so
members now hold exactly the thing the app can't represent. The same gap hides
a crafted item's as-built quality, which is the property that decides whether
it's worth trading.

Quality already exists at both ends of our pipeline and is missing only in the
middle:

| Layer | Has quality? | Where |
|---|---|---|
| Blueprint recipes | ✅ per-slot `min_q` gate + per-slot stat modifiers over Q0–1000 | `poi/blueprints.json` → `nav_core.blueprint_quality_effect` |
| Craft-goal spec | ✅ per-slot asks (`goals.blueprint_inputs`) → per-line `min_q` | `nav_core.blueprint_goal_lines`, `app._goal_craft_block` |
| Marketplace listing | ✅ crafted blob `{quality, band, stats[], inputs[]}` + board filters | `listings.attributes`, `CraftedIn`, `db._listing_filter_sql` |
| **Inventory holding** | ❌ | `inventory` table, `InventoryIn`, `renderInventory` |
| **Allocation / contribution** | ❌ | `inventory_allocations`, `ContributeIn`, `list_goal_contributions` |
| **Goal progress** | ❌ (`min_q` is display-only — the builder's own docstring says "advisory, since inventory doesn't track quality") | `nav_core.derive_goal_progress` |

## What the game does (4.10, researched 2026-09-12)

- **One integer, 1–1000, on every mineable.** Refining preserves it: the
  primary ingredient sets the refined output's quality, secondaries follow a
  quantity curve (lower quality = more volume for the same output).
- **Every distinct value is its own crate.** No blending on pickup. A planned
  merge tool averages by SCU weight; a planned folder view groups variants of
  one material under one expandable entry. Neither is in 4.10 yet.
- **Station-bought material is Q0; crafting needs ≥ Q1.** So "quality 0" is
  a real, meaningful value, distinct from "unrated".
- **Contracts now enforce minimums** — the Wikelo Clipper hand-in wants
  Ouratite at Q800 min and Stileron at Q700. That's the first non-crafting
  reason a goal line needs a floor.
- **Crafted items:** per-slot input quality drives specific finished stats;
  4.10 fixed the stats showing in the item description. The game publishes
  **no formula for a single headline quality** of a crafted item — our
  "overall = lowest slider" is a client heuristic
  (`index.html` marketplace form), and the 8-band mapping we render
  (`⌈q ÷ 125⌉`, `qualityBand`) is still unverified against the game's bands.
- **Tiers** (items 1–3, ships 1–5) are announced but not live; everything
  crafts at tier 0 today. Don't model them yet.

Sources: Elyxir 4.10 breakdown · MrKraken 4.10 shadow notes · Scopique
"Mining and Crafting and Decision-Making" (2026-04) · The Impound crafting
guide + Star Citizen Live inventory/crafting Q&A · ggwtb 4.7 mining guide.

## Properties, by item class

The catalog kind decides which properties a holding may carry. Kinds today:
`commodity | ship | item | component | gear` (`catalog.KINDS`) plus the
virtual `blueprint` (marketplace only, `app.resolve_catalog_item`).

| Class | New holding properties | Notes |
|---|---|---|
| **Commodity** (raw ore, refined, gem) | `quality` int 0–1000, nullable | The game's own scalar. **Null = unrated** (older rows, member didn't look); **0 = station-bought**. Band is *derived*, never stored. |
| **Crafted item** (`blueprint:<key>`, or an `item:` with a blueprint link) | `crafted` JSON = the marketplace `CraftedIn` shape minus `band`: `{quality, stats[], inputs[]}` + `blueprint_key` | Same blob the listing already stores, pointed at a holding instead. "List this from my inventory" becomes a prefill. Band is derived (decision 3). |
| **Ship / gear / component** | none | Equipment grade/class/size is a *type-level* property already joined at read time (`_item_spec`) and rendered in the holdings table. Not quality. |

**Derived, not stored — `subkind` on catalog rows:** `raw | refined | gem |
crafted`. Raw vs refined are already separate UEX commodities
(`_RAW_ORE_SUFFIX` convention), gems are exactly the `kind: "item"` inputs of
the blueprint feed (Hadanite, Dolivine, …), crafted = the `blueprint:` prefix.
This is what lets the inventory UI finally show *what kind of thing* a row is
and facet on it (`INV_FACETS`).

**Naming:** use `quality` / `min_q`, matching the blueprint side. `spec` on an
inventory row already means the catalog's type-level characteristics blob, and
`grade` is already the equipment-grade column — both would collide.

## Interconnections, in build order

### 1. Holding identity gains quality (the fix for the actual bug) — BUILT

As-built notes (2026-09-12): `db._INV_LOT_KEY_SQL` + `inventory_lot` UNIQUE
index (expression index over the COALESCEd key); `_check_quality_kind` also
admits `custom:` items; `/api/inventory?owner=me` rows carry `kind` so the row
editor knows whether the field applies; the location-move duplicate (an edit
landing on an existing lot's key) is refused too — it was a latent bug before
quality joined the key.

Key becomes `(owner, item, location, quality)` with null treated as its own
value. Touch points:

- `db.upsert_inventory` match SQL + `db.get_holding` (mirrors it; the
  contribute path uses it to find the parent holding — without the new key a
  contribution binds to an arbitrary lot).
- Make it a real `UNIQUE` index at the same time (today it's code-enforced,
  and `COALESCE` is already used because SQLite treats NULLs as distinct).
- `InventoryIn` / `InventoryEditIn` / `ContributeIn` (`location` + `on_hand`
  branch) gain `quality`; `_INV_EDITABLE` gains it under decision 2's
  unallocated-only rule.
- Thread the column through the three positional consumers:
  `db.list_goal_contributions` projection, `nav_core.derive_inventory_rollup`
  grouping, `app._my_holdings_for_goal`.
- **No auto-merge.** The game keeps crates separate; averaging two lots
  destroys the information the member is recording. If a merge affordance is
  ever wanted it's an explicit action with the SCU-weighted average shown
  first.
- Frontend: `#inv-quality` number input on the add form (commodities only,
  hidden for ships/gear), Q column + band chip in `invMineTableHtml`, inline
  edit in `openInvEdit`, `quality` facet.

### 2. Goals gate on quality

`derive_goal_progress` compares each contribution's lot quality to the line's
`min_q`:

- `have` = qualifying qty; new `have_low` = under-floor qty, rendered as its
  own hatched segment (the pledge bar already has the pattern).
- **Unrated lots (null) count as qualifying but are flagged** — the org can't
  verify, and blocking would punish every pre-migration row. Q0 does *not*
  qualify against a `min_q ≥ 1` line.
- Contribute: server 409s an under-floor lot with the reason as `detail`,
  overridable with `allow_low` — the exact shape of today's over-fill
  `allow_over` guard. The `#goal-c-src` source picker lists lots with
  `Q<n> · ≈B<n>` and sorts qualifying first.
- **Split action** (decision 2): `POST /api/inventory/{id}/split`
  `{qty, quality, location?}` — qty ≤ unallocated free qty; target key
  existing → sum into it. Dialog, not the inline row editor.
- **Manual `min_q` on any goal line**, not just craft-seeded ones
  (`GoalLineIn.min_q`). Contract hand-ins are the motivating case.

### 3. Craft goals preview real stats

`_goal_craft_block.stat_preview` runs `blueprint_stat_preview` on the
*requested* slot qualities. With lot qualities on pledges it can also run on
the *pledged* ones: "expected stats with what we have," and name the slot
dragging the result. Pure derivation, no new storage.

### 4. Marketplace: commodity quality + inventory bridge

- `attributes.quality` is legal on commodity listings too (today `crafted` is
  the only door in). `min_quality`/`max_quality` board filters already exist.
  A Q800 Ouratite sale and a WTB "Q700+ Stileron" both become expressible.
- "List from inventory": a sell form seeded from a holding carries its
  `quality` / `crafted` blob. Completing the deal can decrement the lot.
- The parked "requester supplies materials" commission bridge
  (`blueprint-craft-commissions.md` §12) becomes buildable: earmark lots
  against the job with the goal-allocation pattern, quality-checked.

### 5. Rollups group by band

Org-wide `derive_inventory_rollup` keeps `item_id` as the group and adds a
`by_band` breakdown (B1–B8 via the derived band, plus "unrated"), mirroring
the game's planned folder view. One row per exact Q value is unreadable at
org scale; the member's own view keeps exact lots.

## Out of scope / don't conflate

- **Scanner bands on POI observations** (`nav_core.quality_for_band`,
  `data.band` 1–8 on resource nodes) are a *node* property from the mining
  scanner, on a different scale. Keep them independent; a future capture flow
  could record the mined Q1–1000 on the node, but that's Prospector work.
- **Tiers** — not live.
- **Price ↔ quality intelligence** (`blueprint-craft-commissions.md` §11.6)
  gets its input from this work but stays a marketplace fast-follow.

## Decisions (settled 2026-09-12)

All five went with the recommendation. Choice 1 depends on choice 2: strict
gating only holds because a pledged lot's quality can't change underneath a
goal. If 2 is ever relaxed, relax 1 to advisory with it.

1. **Goal gating is strict, with a visible under-floor bucket and an
   override.** Progress counts only lots meeting the line's `min_q`;
   under-floor qty is a separate `have_low` figure with its own hatched bar
   segment. Unrated (null) lots count as qualifying but flagged. Contributing
   an under-floor lot 409s with the confirm text as `detail`; `allow_low`
   overrides — the same shape as the over-fill `allow_over` guard.
   *Why:* advisory is what exists now and is exactly the silent over-count
   the issue is about.

2. **Quality is editable in place only while the lot has no allocations;
   otherwise split.** `quality` joins `_INV_EDITABLE`, but `update_inventory`
   409s when the lot has any allocation (pointing at split or withdraw) and
   409s on a key collision. A **split** action (step 2) moves *unallocated*
   qty into a new lot with a new quality and optional location; a split-off
   portion landing on an existing key sums into it (safe: it carries no
   allocations by construction). Pledges always stay on the original row.
   *Why:* in-place edit mutates identity — under a pledged lot that changes a
   goal's progress with no contribution event; a split never touches a row a
   goal depends on, and typos stay a cheap inline fix.

3. **Listing band is derived, not stored.** Remove the free-typed band input
   from the marketplace form and the `band` key from new `attributes` blobs;
   derive `⌈q ÷ 125⌉` wherever rendered; keep reading `band` from old blobs
   when `quality` is absent. The board's exact-band filter becomes a quality
   range. *Why:* two independent fields with an unverified mapping contradict
   each other, and the mapping is the same guess either way.

4. **Migration: null backfill, then a UNIQUE index, collisions logged.**
   `_ensure_column("inventory", "quality")` with no default → every existing
   row is unrated. Then, in `init()` after the migration (the order
   `_migrate_handle_case` uses), scan for rows already sharing
   `(owner_id, item_id, COALESCE(location,''), quality)`, **log them with ids
   and leave them for an admin**, and create the index only when the scan is
   clean. *Why:* the key has never had a constraint, so a silent duplicate may
   exist; summing quantities without a person looking is the one irreversible
   move here.

5. **Add form defaults to blank, never 0.** The quality input starts empty
   and submits null; placeholder says station-bought = 0; hidden entirely for
   ships/gear/components. *Why:* 0 is a real value that marks a lot
   non-craftable, so defaulting to it poisons every lazy entry.
