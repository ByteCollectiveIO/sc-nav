"""Curated taxonomy for the guild event planner (design: docs/event-planner.md).

Types/categories/roles are a hand-maintained list edited in a commit, served to
the create form via one endpoint (`GET /api/events/taxonomy`) — the same
"reference data lives in code" pattern as the ship/commodity catalogs. Three
independent axes so combinations stay open (a PvE *Survey Op*, a PvP *Raid*).

Roles are grouped only for the signup UI; the stored value is the flat role
name. `ROLES` is the flat allow-list endpoints validate signups/rosters against.
"""

# The activity / game loop. Survey Op and Exploration are first-class because
# they feed the navigator's own dataset (see docs/event-planner.md).
TYPES = [
    "Raid", "Mining Op", "Salvage Op", "Cargo Haul", "Bounty Hunt",
    "Survey Op", "Exploration", "Racing", "Combat Patrol", "Medical Op",
    "Industrial", "Meetup / Social", "Training",
]

# The flavor, for filtering. Survey/Exploration are Types, not a category —
# deliberately not duplicated onto this axis (they're just PvE). "Event" tags the
# special/seasonal happenings; "Race" the competitive runs.
CATEGORIES = ["PvP", "PvE", "Social", "Logistics", "Mixed", "Event", "Race"]

# What a signup fills. The four Survey & Exploration roles map 1:1 onto the
# navigator's capture domains (Surveyor→cells/ores/hotspots,
# Naturalist→fauna/harvestables/biomes, Cartographer→POIs/position,
# Pathfinder/Scout→recon).
ROLE_GROUPS = [
    {"group": "Combat & Security",
     "roles": ["Combat (Ship)", "Combat (FPS)", "Escort", "Medical"]},
    {"group": "Industrial",
     "roles": ["Mining", "Salvage", "Cargo / Hauling"]},
    {"group": "Survey & Exploration",
     "roles": ["Surveyor", "Naturalist", "Cartographer", "Pathfinder / Scout"]},
    {"group": "Support",
     "roles": ["Support / Logistics", "Command"]},
]

# Flat allow-list (order preserved) for validation.
ROLES = [role for g in ROLE_GROUPS for role in g["roles"]]


# Mission details (docs/event-operations.md §13): the FPS/PvP specifics a Type
# is too coarse for. `mission` is free text — these are only datalist
# suggestions, because the game renames and adds mission kinds every patch.
MISSION_SUGGESTIONS = [
    "Bunker (PvE)", "Bounty — ERT", "Bounty — VHRT", "Bounty — MRT",
    "Xenothreat", "Contested zone", "Executive hangar", "Siege of Orison",
    "Jumptown", "Cargo contract", "Mining contract", "Salvage contract",
]

# Rules of engagement. "" = not stated (the default — most ops never say).
ROE = [
    {"key": "pve_only", "label": "PvE only"},
    {"key": "pvp_if_engaged", "label": "PvP if engaged"},
    {"key": "pvp_hunt", "label": "PvP hunt"},
]
ROE_KEYS = [r["key"] for r in ROE]

# The mission-detail keys an event (or template) may carry, in display order.
DETAIL_KEYS = ("mission", "loadout", "medical", "comms", "roe", "prereqs")

# Built-in event templates (docs/event-operations.md §14.5). Read-only presets
# that live in CODE, not the DB, so a release can improve one without touching
# an org's own copies ("Copy to customize" makes an ordinary org template that
# records `builtin_key`, and hides the built-in it came from). Bump `version`
# whenever a preset's contents change: events snapshot the template they were
# created from, and the version is what tells a reader which preset they got.
# Every type/category/role here MUST be in the lists above (a test pins it).
# Role counts are PLACEHOLDERS pending review by someone who runs these ops.
BUILTIN_TEMPLATES = [
    {"key": "mining", "name": "Mining", "version": 1, "event": {
        "types": ["Mining Op"], "categories": ["PvE"], "duration_min": 120,
        "roles": [{"role": "Mining", "needed": 3},
                  {"role": "Cargo / Hauling", "needed": 1},
                  {"role": "Escort", "needed": 1}],
        "details": {"mission": "Mining contract"}},
     "groups": []},
    {"key": "salvage", "name": "Salvage", "version": 1, "event": {
        "types": ["Salvage Op"], "categories": ["PvE"], "duration_min": 120,
        "roles": [{"role": "Salvage", "needed": 2},
                  {"role": "Cargo / Hauling", "needed": 1},
                  {"role": "Escort", "needed": 1}],
        "details": {"mission": "Salvage contract"}},
     "groups": []},
    {"key": "cargo", "name": "Cargo haul", "version": 1, "event": {
        "types": ["Cargo Haul"], "categories": ["Logistics"], "duration_min": 90,
        "roles": [{"role": "Cargo / Hauling", "needed": 2},
                  {"role": "Escort", "needed": 2}],
        "details": {"mission": "Cargo contract"}},
     "groups": []},
    {"key": "bounty", "name": "Bounty / combat patrol", "version": 1, "event": {
        "types": ["Bounty Hunt", "Combat Patrol"], "categories": ["PvE"],
        "duration_min": 90,
        "roles": [{"role": "Combat (Ship)", "needed": 4},
                  {"role": "Medical", "needed": 1}],
        "details": {"mission": "Bounty — VHRT", "roe": "pve_only"}},
     "groups": []},
    {"key": "bunker", "name": "Bunker (FPS)", "version": 1, "event": {
        "types": ["Combat Patrol"], "categories": ["PvE"], "duration_min": 90,
        "roles": [{"role": "Combat (FPS)", "needed": 4},
                  {"role": "Medical", "needed": 1},
                  {"role": "Support / Logistics", "needed": 1}],
        "details": {"mission": "Bunker (PvE)", "loadout": "Medium armour",
                    "medical": "Bring 2 medpens", "roe": "pve_only"}},
     "groups": [{"name": "Assault", "kind": "squad", "ship": None, "capacity": 4},
                {"name": "Dropship", "kind": "crew", "ship": None, "capacity": 2}]},
    {"key": "raid", "name": "Raid", "version": 1, "event": {
        "types": ["Raid"], "categories": ["PvP"], "duration_min": 150,
        "roles": [{"role": "Combat (FPS)", "needed": 6},
                  {"role": "Medical", "needed": 2},
                  {"role": "Combat (Ship)", "needed": 2},
                  {"role": "Command", "needed": 1}],
        "details": {"loadout": "Heavy armour", "medical": "Bring 3 medpens",
                    "roe": "pvp_if_engaged"}},
     "groups": [{"name": "Alpha", "kind": "squad", "ship": None, "capacity": 4},
                {"name": "Bravo", "kind": "squad", "ship": None, "capacity": 4},
                {"name": "Air cover", "kind": "wing", "ship": None, "capacity": 2}]},
]
BUILTIN_BY_KEY = {t["key"]: t for t in BUILTIN_TEMPLATES}


def taxonomy() -> dict:
    """The full taxonomy payload for `GET /api/events/taxonomy`."""
    return {
        "types": TYPES,
        "categories": CATEGORIES,
        "role_groups": ROLE_GROUPS,
        "roles": ROLES,
        "mission_suggestions": MISSION_SUGGESTIONS,
        "roe": ROE,
    }
