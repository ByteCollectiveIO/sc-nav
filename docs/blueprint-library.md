# Blueprint library page (#/blueprints)

Status: **🔨 built 2026-09-12 — awaiting dev-server test, unreleased** (rides the #151 release hold).

## The problem

The third Resource Manager tab listed a member's saved recipes as an
unordered list: name, category, Gather, Remove. The feed record behind each
row carries far more, and the holdings ledger now carries lot quality (#151),
so the page can answer the two questions a crafter actually has: *what can I
build right now, and how good will it be?*

## Data (all read-time, nothing new stored)

Per library row, `app._member_blueprint_view` now joins:

| Field | Source |
|---|---|
| `manufacturer`, `size`, `cls`, `grade` | UEX item catalog spec by **name** (`item_specs`; 1,493 / 1,559 recipes hit — the same four columns the holdings table shows) |
| `time_s`, `default`, `unlocks`, `has_mods` | feed record |
| `inputs` `[{input, kind, need, unit}]`, `max_min_q` | `nav_core.blueprint_manifest` |
| `est_cost` `{total, unpriced}` | `app._blueprint_est_cost` (per craft) |
| `craft` | `nav_core.craftable_from_holdings` — see below |

## Craftable now

`craftable_from_holdings(bp, holdings, resolve)` reads the member's **free**
stock (`available`, never pledged qty) per input material. For each slot it
fills the recipe's need from the member's lots **best quality first** — the
way a crafter loads the fabricator — and reports:

- `have` / `need` / `ok` per slot, `crafts` = how many whole crafts the
  scarcest input allows;
- `q` = qty-weighted quality of the rated portion of that fill; `unrated`
  qty drawn once rated lots run out (can't inform a stat); a slot with no
  rated lot at all is `assumed` (base Q500, same convention as the goal
  preview);
- headline `quality` = the **lowest** slot quality — the marketplace's own
  "overall = weakest input" rule — and `stat_preview` at the fill qualities.

`ok` is the row's "craftable now" state; `crafts` is the count chip.

## Create listing

The library's **List** action seeds the marketplace create form
(`mktFormSeed`) with item `blueprint:<key>`, qty = `crafts` (min 1), crafted
quality = the headline quality, and the finished stats from `stat_preview`
as stat rows — so the listing advertises what the member can *actually*
make, not a typed guess. An unrated or assumed slot is called out in the
seed note; the member can still edit every field.

## Page

Grouped by category (collapsible), sortable columns Name · Maker · Size/Grade
· Materials · Est. cost · Time · Craftable; text + category + material
filters (same facet bar pattern as inventory). Materials in the row are
**green** when the member's free stock covers them, red when short. Clicking
a row opens the **recipe card** (a modal, `bpDetailModal`): identity line,
time / est. cost / quality floor, the craftable verdict, materials vs free
stock per slot (one row per MATERIAL: slot · needed/craft · held (free,
aggregate) · quality), expected stats, and **Unlocked by** — an "Easiest
path" line (lowest reputation gate, then best chance, then highest payout)
above one quiet table grouped per faction, lowest gate first; a location
every mission shares is hoisted into the faction header and empty cells stay
empty (impeccable critique 2026-09-12, 27/40 → fixes in
`.impeccable/critique/`). Actions: Gather (existing) · List (new) · Remove.

## Unlock paths (feed change, 2026-09-12)

`tools/sync_blueprints.py` now fetches each unlocking mission once
(`/api/missions/<uuid>`, cached) and stores `unlocks` as structured entries:
`{title, chance, mission, giver, faction, faction_type, mission_type,
rep_min {name, pts}, rep_gain, systems[], places[]}` (cap 20). The old
`"Title (100%)"` strings are still rendered by `bpUnlockObj`. Re-synced to
4.10.0 the same day — that brought in the Recco Battaglia mining modules
(Clearcut / Deluge / Overrun) and the ore pods (MISC Enhanced, GOLEM MC-4),
which are the recipes an org's reputation push is about; see the "org
blueprint readiness" proposal in the backlog.
