"""Shared item catalog for the org inventory/goals and marketplace apps
(design: docs/org-inventory-goals.md + docs/marketplace.md).

One canonical reference of items both apps point at, each with a stable
`item_id`, a `name`, a `kind`, and a `unit`. Three sources merge into one list:

  * commodity feed — every uexcorp commodity (names from load_commodity_names),
    kind="commodity", unit="SCU".
  * vehicle feed   — uexcorp ships (the trimmed `load_ships` rows), kind="ship",
    unit="each".
  * custom items   — admin/member-added rows in the `catalog_items` table for
    anything not in a feed (components, FPS gear, …); their own kind/unit.

The id is synthesized from the name (`commodity:<slug>`, `ship:<slug>`,
`custom:<n>`) so it stays stable across a feed refresh that reorders the numeric
feed ids. Inventory rows and goal line items also store the resolved `name` +
`unit` denormalized (like `events.roles` stores role names), so the catalog is a
picker/validator, not a hard foreign key — a later feed change can't strand a
contribution.

This module is pure data shaping (no I/O); app.py owns the feeds + DB rows and
passes them in, the same "reference data lives in code" shape as event_taxonomy.
"""

import re

# Catalog `kind` allow-list. Feed items are commodity/ship; custom items pick one
# of these (gear/component cover everything not in a feed).
KINDS = ("commodity", "ship", "component", "gear")

# Default unit per kind when a custom item doesn't state one.
_DEFAULT_UNIT = {"commodity": "SCU", "ship": "each", "component": "each",
                 "gear": "each"}

# Unit allow-list a member may pick when logging a holding / goal line item. The
# catalog stamps every commodity "SCU", but fauna parts / harvestables / gear are
# counted individually ("each"/"unit"), so the member can override the default.
# Kept deliberately short — these are the only sane counting units in-game.
UNITS = ("SCU", "each", "unit", "L", "mg")


def valid_unit(unit):
    """A member-supplied unit if it's in the allow-list, else None (caller then
    falls back to the catalog item's default)."""
    u = (unit or "").strip()
    return u if u in UNITS else None


def slug(name: str) -> str:
    """A url/id-safe slug of an item name: lowercased, non-alphanumerics collapsed
    to single hyphens, trimmed. Stable across feed refreshes (keyed on the name,
    not the feed's reorderable numeric id)."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "item"


def default_unit(kind: str) -> str:
    return _DEFAULT_UNIT.get(kind, "each")


def feed_items(commodity_names, ships) -> list[dict]:
    """Canonical catalog items from the two uexcorp feeds.

    `commodity_names` is the flat name list (load_commodity_names); `ships` is the
    trimmed ship rows (load_ships, each `{name, company, scu}`). De-duped by
    synthesized id so two feed rows that slug the same collapse to one item."""
    out, seen = [], set()
    for name in commodity_names or []:
        if not name:
            continue
        iid = f"commodity:{slug(name)}"
        if iid in seen:
            continue
        seen.add(iid)
        out.append({"item_id": iid, "name": name, "kind": "commodity", "unit": "SCU"})
    for s in ships or []:
        name = s.get("name") if isinstance(s, dict) else s
        if not name:
            continue
        iid = f"ship:{slug(name)}"
        if iid in seen:
            continue
        seen.add(iid)
        out.append({"item_id": iid, "name": name, "kind": "ship", "unit": "each"})
    return out


def custom_item(row: dict) -> dict:
    """A `catalog_items` DB row → a canonical catalog item (id `custom:<n>`)."""
    kind = row.get("kind") if row.get("kind") in KINDS else "gear"
    return {
        "item_id": f"custom:{row['id']}",
        "name": row.get("name"),
        "kind": kind,
        "unit": row.get("unit") or default_unit(kind),
    }


def build(commodity_names, ships, custom_rows) -> list[dict]:
    """The full merged catalog (feeds + custom), sorted by name. Custom items win
    on an id clash so an org can override a feed name if they ever need to."""
    by_id = {it["item_id"]: it for it in feed_items(commodity_names, ships)}
    for r in custom_rows or []:
        it = custom_item(r)
        by_id[it["item_id"]] = it
    return sorted(by_id.values(), key=lambda it: (it["name"] or "").lower())


def search(catalog, q: str, limit: int = 50) -> list[dict]:
    """Substring (case-insensitive) name search over a built catalog. Prefix
    matches rank above mid-string matches; ties keep the catalog's name order.
    Empty query returns the first `limit` items (the picker's initial list)."""
    q = (q or "").strip().lower()
    if not q:
        return catalog[:limit]
    starts, contains = [], []
    for it in catalog:
        n = (it["name"] or "").lower()
        if n.startswith(q):
            starts.append(it)
        elif q in n:
            contains.append(it)
    return (starts + contains)[:limit]
