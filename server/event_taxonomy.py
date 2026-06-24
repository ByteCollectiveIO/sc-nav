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
# deliberately not duplicated onto this axis (they're just PvE).
CATEGORIES = ["PvP", "PvE", "Social", "Logistics", "Mixed"]

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


def taxonomy() -> dict:
    """The full taxonomy payload for `GET /api/events/taxonomy`."""
    return {
        "types": TYPES,
        "categories": CATEGORIES,
        "role_groups": ROLE_GROUPS,
        "roles": ROLES,
    }
