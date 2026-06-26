"""SQLite persistence for SC Nav's user-contributed data (Phase 2).

The upstream dataset (containers/poi) and the commodity/biome/fauna reference
lists stay as files — those are caches/seeds. What moves here is the mutable,
collaborative data that used to live in per-file JSON with whole-file rewrites
and in-memory id counters:

  * custom_pois      — user-created POIs (ids >= CUSTOM_ID_START)
  * observations     — resource + wildlife sightings (ids >= OBSERVATION_ID_START)
  * handles          — handle -> stable player_id registry
  * watcher_tokens   — hashed per-user watcher tokens

Row shapes mirror nav_core.custom_poi_to_dict / observation_to_dict so the
existing merge/parse code in nav_core is reused verbatim — nav_core never sees
the database. WAL mode + SQLite's single-writer locking give safe concurrent
reads with serialized writes; a process-level lock makes the shared connection
thread-safe for the occasional asyncio.to_thread caller.
"""

import json
import sqlite3
import threading

# Reserved id ranges so customs never collide with upstream item_ids (~1-2000)
# and observations never collide with custom POIs.
CUSTOM_ID_START = 1_000_000
OBSERVATION_ID_START = 2_000_000

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS custom_pois (
    id INTEGER PRIMARY KEY,
    name TEXT, system TEXT, container TEXT, type TEXT,
    local_km TEXT, global_m TEXT,
    latitude REAL, longitude REAL, height_m REAL,
    qt_marker INTEGER DEFAULT 0,
    owner_id INTEGER, owner_handle TEXT,
    note TEXT,
    private INTEGER DEFAULT 0     -- owner-only POI; hidden from the rest of the org
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    system TEXT, container TEXT,
    local_km TEXT, global_m TEXT,
    latitude REAL, longitude REAL, height_m REAL,
    biome TEXT, note TEXT,
    owner_id INTEGER, owner_handle TEXT,
    observed_at TEXT,
    shard_id TEXT,           -- SC shard the sighting was made on (Game.log)
    data TEXT
);
CREATE INDEX IF NOT EXISTS observations_category ON observations(category);

CREATE TABLE IF NOT EXISTS handles (
    player_id INTEGER PRIMARY KEY,
    handle TEXT UNIQUE,
    first_seen TEXT, last_seen TEXT,
    discord_id TEXT          -- owning member; bound when a watcher posts the handle
);

CREATE TABLE IF NOT EXISTS watcher_tokens (
    id TEXT PRIMARY KEY,
    hash TEXT UNIQUE,
    discord_id TEXT, display_name TEXT, label TEXT,
    created TEXT, last_used TEXT
);

-- Per-member cargo-planner ship prefs: the usable-SCU a member has learned for
-- each ship (stated catalog SCU minus what they can't physically stuff). Keyed
-- on the Discord member id; one row per (member, ship).
CREATE TABLE IF NOT EXISTS user_ships (
    discord_id TEXT NOT NULL,
    name TEXT NOT NULL,
    usable_scu REAL,
    last_used TEXT,
    PRIMARY KEY (discord_id, name)
);

-- Cargo-planner runs: one active route a member is executing, plus their
-- completed-run history. `data` is the JSON run blob (ordered stops, package
-- states, the active-stop cursor); ship/started_at/completed_at are denormalized
-- for history queries. At most one row per member has status='active'.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    discord_id TEXT NOT NULL,
    status TEXT NOT NULL,          -- active | completed | abandoned
    ship TEXT,
    started_at TEXT,
    completed_at TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS runs_owner_status ON runs(discord_id, status);

-- Event planner: guild-organized in-game events (raids, ops, survey/exploration
-- expeditions, meetups). `organizer_id` is the creating member; `roles` is the
-- JSON target roster [{role, needed}]; `start_at` is UTC ISO8601 (rendered local
-- in the client). `category` is a JSON list of flavors (an event can be both PvP
-- and PvE). `location` is the rally point (where the org forms up); the optional
-- `event_location` is where the activity actually happens. Cancelling sets status
-- rather than deleting, so a mistaken cancel is recoverable and the roster history
-- survives.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    organizer_id TEXT NOT NULL,         -- Discord member id of the creator
    title TEXT, description TEXT,
    type TEXT, category TEXT,           -- type & category: JSON lists (1+ each)
    start_at TEXT,                      -- event start, UTC ISO8601
    signup_deadline TEXT,               -- optional UTC ISO8601; after it, signups lock
    duration_min INTEGER,
    location TEXT,                       -- rally point
    event_location TEXT,                 -- where the activity happens (optional)
    min_players INTEGER, max_players INTEGER,   -- max NULL = unlimited
    roles TEXT,                         -- JSON target roster: [{role, needed}]
    status TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled | cancelled | completed
    created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS events_start ON events(start_at);

-- One signup per (event, member) — joining again upserts the role list. `roles`
-- is a JSON list of role names the member will fill (a medic who'll also escort
-- claims both). Withdrawn rows are kept (status flips) so a re-join is cheap.
CREATE TABLE IF NOT EXISTS event_signups (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL,
    discord_id TEXT NOT NULL,
    roles TEXT,                         -- JSON list of role names
    status TEXT NOT NULL DEFAULT 'going',   -- going | maybe | withdrawn
    note TEXT,
    created_at TEXT,
    UNIQUE(event_id, discord_id)
);
CREATE INDEX IF NOT EXISTS event_signups_event ON event_signups(event_id);

-- Org inventory & goals + marketplace: a shared item catalog of anything not in
-- the uexcorp commodity/vehicle feeds (components, FPS gear, …). Feed items are
-- synthesized in catalog.py from the cached feeds and aren't stored here; only
-- the hand-entered customs live in this table (catalog id `custom:<id>`).
CREATE TABLE IF NOT EXISTS catalog_items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- commodity | ship | component | gear
    unit TEXT,                          -- SCU | each | …
    creator_id TEXT,                    -- Discord member who added it
    created_at TEXT
);

-- Per-member inventory ledger. SC has no shared org storage, so a holding is an
-- *attributed pledge*, never a pooled vault — every row is owned by a member.
-- A row earmarked to a goal (`goal_id` set) IS that member's contribution toward
-- it; `goal_id NULL` is a general (allocatable) holding. `item_name`/`unit` are
-- denormalized off the catalog at write time (like events.roles stores names) so
-- a later feed change can't strand a row. One row per (owner, item, location,
-- goal) — re-logging sets the quantity rather than stacking duplicates.
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY,
    owner_id TEXT NOT NULL,             -- Discord member id
    item_id TEXT NOT NULL,             -- catalog id (commodity:/ship:/custom:)
    item_name TEXT, unit TEXT,
    qty REAL NOT NULL DEFAULT 0,
    location TEXT,                      -- free text (e.g. "Area18 hangar")
    note TEXT,
    goal_id INTEGER,                   -- earmarked goal, or NULL = general pool
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS inventory_owner ON inventory(owner_id);
CREATE INDEX IF NOT EXISTS inventory_goal ON inventory(goal_id);
CREATE INDEX IF NOT EXISTS inventory_item ON inventory(item_id);

-- Org procurement goals: a target with a deadline + priority and a JSON list of
-- line items [{item_id, item_name, unit, qty_needed}] (same JSON-blob pattern as
-- events.roles). Contributions are inventory rows pointed at the goal, so fill is
-- derived (derive_goal_progress), never stored. `priority` 1–10, 1 = highest.
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY,
    creator_id TEXT NOT NULL,           -- Discord member id of the creator
    title TEXT, description TEXT,
    priority INTEGER NOT NULL DEFAULT 5,
    deadline TEXT,                      -- optional UTC ISO8601; rendered local
    status TEXT NOT NULL DEFAULT 'active',   -- active | met | archived
    line_items TEXT,                    -- JSON: [{item_id, item_name, unit, qty_needed}]
    created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS goals_status ON goals(status);

-- Goal contributions are *allocations drawn from a parent holding*, not duplicate
-- inventory rows. Committing 30 of a 50-unit holding records one allocation here
-- (qty 30) and leaves the holding's `available = qty - SUM(allocations)` at 20, so
-- a contribution is never double-counted in the org rollup. One allocation per
-- (holding, goal); deleting either the holding or the goal removes it.
CREATE TABLE IF NOT EXISTS inventory_allocations (
    id INTEGER PRIMARY KEY,
    inventory_id INTEGER NOT NULL,      -- parent holding (inventory.id)
    goal_id INTEGER NOT NULL,
    qty REAL NOT NULL DEFAULT 0,
    created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS inv_alloc_holding ON inventory_allocations(inventory_id);
CREATE INDEX IF NOT EXISTS inv_alloc_goal ON inventory_allocations(goal_id);

-- Org marketplace (design: docs/marketplace.md). SC has no auction house or item
-- API, so a listing is a *coordination board entry*, not an exchange: the app
-- records that two members agreed on terms; the goods + aUEC move in-game on
-- trust. aUEC only — never real money (fan project under CIG's IP). One row per
-- listing across all three modes (`mode` discriminator); offers/bids live in the
-- child table. `item_name`/`unit` are denormalized off the catalog at write time
-- (like events.roles / inventory) so a feed change can't strand a listing.
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    seller_id TEXT NOT NULL,            -- Discord member id of the seller
    item_id TEXT NOT NULL,             -- catalog id (commodity:/ship:/custom:)
    item_name TEXT, unit TEXT,
    qty REAL NOT NULL DEFAULT 1,
    mode TEXT NOT NULL DEFAULT 'sale', -- sale | auction | barter
    price_auec REAL,                   -- sale: fixed ask (aUEC)
    start_price REAL,                  -- auction: opening bid
    buyout_auec REAL,                  -- auction: optional instant-win price
    ends_at TEXT,                      -- auction: close time, UTC ISO8601
    want TEXT,                         -- barter: what the seller wants (free text)
    status TEXT NOT NULL DEFAULT 'open',   -- open | pending | completed | cancelled | expired
    note TEXT,
    buyer_id TEXT,                     -- set when a deal is struck (pending+)
    seller_confirmed INTEGER NOT NULL DEFAULT 0,   -- handoff confirmed by seller
    buyer_confirmed INTEGER NOT NULL DEFAULT 0,     -- handoff confirmed by buyer
    -- Denormalized board columns (refresh_listing_denorm keeps them current) so the
    -- browse board is pure SQL — filter/sort/page with no per-listing derivation.
    sort_price REAL,                   -- display/sort price: sale=price_auec,
                                       -- auction=high bid or start_price, barter=NULL
    offer_count INTEGER NOT NULL DEFAULT 0,         -- active offers/bids on the listing
    -- Optional crafted-item quality annotation (SC 4.8): JSON
    -- {quality:1-1000, band:1-8, stats:[{name,value}]} so a seller can advertise a
    -- crafted component's quality + per-stat values. Free-form like goals.line_items.
    attributes TEXT,
    created_at TEXT, updated_at TEXT, completed_at TEXT
);
CREATE INDEX IF NOT EXISTS listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS listings_seller ON listings(seller_id);
CREATE INDEX IF NOT EXISTS listings_item ON listings(item_id);
-- NB: the listings_sort_price index is created in init() *after* _ensure_column
-- adds sort_price — it can't live here, since on an upgrade this script runs before
-- the column exists (the listings table is already present, so CREATE TABLE is a
-- no-op and the column would be missing when the index references it).

-- One row per bid / offer / barter counter against a listing. For sale+auction
-- `amount_auec` is the aUEC bid/offer; for barter `offer_item_id` + `offer_note`
-- is the counter-item. The seller accepts one (→ the listing goes pending); the
-- rest are marked 'lost'. Bidders can withdraw an active offer.
CREATE TABLE IF NOT EXISTS listing_offers (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL,
    bidder_id TEXT NOT NULL,           -- Discord member id of the bidder
    amount_auec REAL,                  -- sale/auction: aUEC bid/offer
    offer_item_id TEXT,                -- barter: catalog id of the counter-item
    offer_item_name TEXT,              -- denormalized name of the counter-item
    offer_note TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active | accepted | withdrawn | lost
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS listing_offers_listing ON listing_offers(listing_id);
"""


def init(db_path) -> None:
    global _conn
    _conn = sqlite3.connect(str(db_path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    with _lock, _conn:
        _conn.executescript(SCHEMA)
        # Migrate DBs created before a column existed (CREATE TABLE IF NOT EXISTS
        # won't add columns to an already-present table).
        _ensure_column("handles", "discord_id", "TEXT")
        _ensure_column("custom_pois", "note", "TEXT")
        _ensure_column("custom_pois", "private", "INTEGER DEFAULT 0")
        _ensure_column("observations", "shard_id", "TEXT")
        _ensure_column("events", "event_location", "TEXT")
        _ensure_column("events", "signup_deadline", "TEXT")
        denorm_added = _ensure_column("listings", "sort_price", "REAL")
        denorm_added |= _ensure_column("listings", "offer_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column("listings", "attributes", "TEXT")
        # Index the board's sort column now that it's guaranteed to exist (see the
        # note in SCHEMA — it can't be created inside the schema script on upgrade).
        _conn.execute("CREATE INDEX IF NOT EXISTS listings_sort_price ON listings(sort_price)")
        _migrate_inventory_allocations()
        if denorm_added:                        # only on the boot that adds the columns
            _backfill_listing_denorm()


def _migrate_inventory_allocations() -> None:
    """Convert legacy goal-tagged inventory rows into the allocation model.

    The pre-v1.1 ledger stored a goal contribution as a *separate* inventory row
    with `goal_id` set, which double-counted against the org rollup and had no
    link to the holding it came from. Convert each such row into a plain holding
    (clear `goal_id`) plus one allocation of its full quantity against that goal.
    Idempotent: after it runs no goal-tagged inventory rows remain to convert."""
    rows = _conn.execute(
        "SELECT id, goal_id, qty, updated_at FROM inventory "
        "WHERE goal_id IS NOT NULL").fetchall()
    for r in rows:
        _conn.execute(
            "INSERT INTO inventory_allocations "
            "(inventory_id, goal_id, qty, created_at, updated_at) VALUES (?,?,?,?,?)",
            (r["id"], r["goal_id"], r["qty"], r["updated_at"], r["updated_at"]))
        _conn.execute("UPDATE inventory SET goal_id=NULL WHERE id=?", (r["id"],))


def _ensure_column(table: str, column: str, decl: str) -> bool:
    """Add a column to an existing table if it's missing. Returns True when it
    actually added it (i.e. this is the migration boot), so a caller can run a
    one-time backfill only when the column was just introduced."""
    cols = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return True
    return False


def _j(v):
    return json.dumps(v) if v is not None else None


def _u(s):
    return json.loads(s) if s else None


# --- custom POIs -----------------------------------------------------------

_CUSTOM_COLS = ("id", "name", "system", "container", "type", "local_km",
                "global_m", "latitude", "longitude", "height_m", "qt_marker",
                "owner_id", "owner_handle", "note", "private")


def _custom_row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "name": r["name"], "system": r["system"],
        "container": r["container"], "type": r["type"],
        "local_km": _u(r["local_km"]), "global_m": _u(r["global_m"]),
        "latitude": r["latitude"], "longitude": r["longitude"],
        "height_m": r["height_m"], "qt_marker": bool(r["qt_marker"]),
        "owner_id": r["owner_id"], "owner_handle": r["owner_handle"],
        "note": r["note"], "private": bool(r["private"]),
    }


def add_custom_poi(d: dict) -> None:
    with _lock, _conn:
        _conn.execute(
            "INSERT OR REPLACE INTO custom_pois "
            "(id,name,system,container,type,local_km,global_m,latitude,longitude,"
            "height_m,qt_marker,owner_id,owner_handle,note,private) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["id"], d.get("name"), d.get("system"), d.get("container"),
             d.get("type"), _j(d.get("local_km")), _j(d.get("global_m")),
             d.get("latitude"), d.get("longitude"), d.get("height_m"),
             1 if d.get("qt_marker") else 0, d.get("owner_id"), d.get("owner_handle"),
             d.get("note"), 1 if d.get("private") else 0),
        )


def list_custom_pois() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM custom_pois").fetchall()
    return [_custom_row_to_dict(r) for r in rows]


def next_custom_poi_id() -> int:
    with _lock:
        row = _conn.execute(
            "SELECT COALESCE(MAX(id), ?) FROM custom_pois", (CUSTOM_ID_START - 1,)
        ).fetchone()
    return row[0] + 1


def delete_custom_poi(poi_id: int) -> bool:
    with _lock, _conn:
        cur = _conn.execute("DELETE FROM custom_pois WHERE id=?", (poi_id,))
    return cur.rowcount > 0


def update_custom_poi_note(poi_id: int, note: str | None) -> bool:
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE custom_pois SET note=? WHERE id=?", (note, poi_id)
        )
    return cur.rowcount > 0


def update_custom_poi_private(poi_id: int, private: bool) -> bool:
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE custom_pois SET private=? WHERE id=?",
            (1 if private else 0, poi_id),
        )
    return cur.rowcount > 0


# --- observations ----------------------------------------------------------


def _obs_row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "category": r["category"], "system": r["system"],
        "container": r["container"], "local_km": _u(r["local_km"]),
        "global_m": _u(r["global_m"]), "latitude": r["latitude"],
        "longitude": r["longitude"], "height_m": r["height_m"],
        "biome": r["biome"], "note": r["note"], "owner_id": r["owner_id"],
        "owner_handle": r["owner_handle"], "observed_at": r["observed_at"],
        "shard_id": r["shard_id"],
        "data": _u(r["data"]) or {},
    }


def add_observation(d: dict) -> None:
    with _lock, _conn:
        _conn.execute(
            "INSERT OR REPLACE INTO observations "
            "(id,category,system,container,local_km,global_m,latitude,longitude,"
            "height_m,biome,note,owner_id,owner_handle,observed_at,shard_id,data) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["id"], d.get("category"), d.get("system"), d.get("container"),
             _j(d.get("local_km")), _j(d.get("global_m")), d.get("latitude"),
             d.get("longitude"), d.get("height_m"), d.get("biome"), d.get("note"),
             d.get("owner_id"), d.get("owner_handle"), d.get("observed_at"),
             d.get("shard_id"), _j(d.get("data") or {})),
        )


def list_observations() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM observations").fetchall()
    return [_obs_row_to_dict(r) for r in rows]


def next_observation_id() -> int:
    with _lock:
        row = _conn.execute(
            "SELECT COALESCE(MAX(id), ?) FROM observations", (OBSERVATION_ID_START - 1,)
        ).fetchone()
    return row[0] + 1


def delete_observation(obs_id: int) -> bool:
    with _lock, _conn:
        cur = _conn.execute("DELETE FROM observations WHERE id=?", (obs_id,))
    return cur.rowcount > 0


def clear_observations() -> int:
    """Wipe every resource/wildlife/harvestable sighting (admin 'clear resource
    statistics'). Custom POIs and QT markers live in their own tables and are
    untouched. Returns the number of rows removed."""
    with _lock, _conn:
        cur = _conn.execute("DELETE FROM observations")
    return cur.rowcount


# --- handles ---------------------------------------------------------------


def all_handles() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM handles").fetchall()
    return [dict(r) for r in rows]


def upsert_handle(entry: dict) -> None:
    with _lock, _conn:
        _conn.execute(
            "INSERT OR REPLACE INTO handles (player_id,handle,first_seen,last_seen,discord_id) "
            "VALUES (?,?,?,?,?)",
            (entry["player_id"], entry["handle"], entry.get("first_seen"),
             entry.get("last_seen"), entry.get("discord_id")),
        )


# --- watcher tokens --------------------------------------------------------


def all_tokens() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM watcher_tokens").fetchall()
    return [dict(r) for r in rows]


def add_token(t: dict) -> None:
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO watcher_tokens (id,hash,discord_id,display_name,label,created,last_used) "
            "VALUES (?,?,?,?,?,?,?)",
            (t["id"], t["hash"], t["discord_id"], t.get("display_name"),
             t.get("label"), t.get("created"), t.get("last_used")),
        )


def delete_token(token_id: str) -> bool:
    with _lock, _conn:
        cur = _conn.execute("DELETE FROM watcher_tokens WHERE id=?", (token_id,))
    return cur.rowcount > 0


# --- cargo-planner ship prefs (per member) ---------------------------------


def list_user_ships(discord_id: str) -> list[dict]:
    """A member's saved ships, most-recently-used first."""
    with _lock:
        rows = _conn.execute(
            "SELECT name, usable_scu, last_used FROM user_ships WHERE discord_id=? "
            "ORDER BY last_used DESC, name",
            (str(discord_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_user_ship(discord_id: str, name: str, usable_scu: float, last_used: str) -> None:
    """Remember (or update) a member's usable-SCU for a ship and stamp last_used."""
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO user_ships (discord_id, name, usable_scu, last_used) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(discord_id, name) DO UPDATE SET "
            "usable_scu=excluded.usable_scu, last_used=excluded.last_used",
            (str(discord_id), name, usable_scu, last_used),
        )


def delete_user_ship(discord_id: str, name: str) -> bool:
    with _lock, _conn:
        cur = _conn.execute(
            "DELETE FROM user_ships WHERE discord_id=? AND name=?",
            (str(discord_id), name),
        )
    return cur.rowcount > 0


# --- cargo-planner runs (per member) ---------------------------------------


def get_active_run(discord_id: str) -> dict | None:
    """The member's in-progress run as the parsed blob (with the row id), or
    None. At most one active run exists per member."""
    with _lock:
        row = _conn.execute(
            "SELECT id, ship, started_at, data FROM runs "
            "WHERE discord_id=? AND status='active' ORDER BY id DESC LIMIT 1",
            (str(discord_id),),
        ).fetchone()
    if row is None:
        return None
    run = _u(row["data"]) or {}
    run["id"] = row["id"]
    return run


def start_run(discord_id: str, ship: str | None, started_at: str, run: dict) -> int:
    """Persist a fresh active run, abandoning any prior active one (a member runs
    one route at a time). Returns the new run id."""
    with _lock, _conn:
        _conn.execute(
            "UPDATE runs SET status='abandoned' WHERE discord_id=? AND status='active'",
            (str(discord_id),),
        )
        cur = _conn.execute(
            "INSERT INTO runs (discord_id, status, ship, started_at, data) "
            "VALUES (?, 'active', ?, ?, ?)",
            (str(discord_id), ship, started_at, _j(run)),
        )
    return cur.lastrowid


def update_run(discord_id: str, run_id: int, run: dict) -> None:
    """Persist progress on the active run (package states / active cursor)."""
    with _lock, _conn:
        _conn.execute(
            "UPDATE runs SET data=? WHERE id=? AND discord_id=? AND status='active'",
            (_j(run), run_id, str(discord_id)),
        )


def complete_run(discord_id: str, run_id: int, completed_at: str, run: dict) -> None:
    """Mark the active run completed, freezing its final blob for history."""
    with _lock, _conn:
        _conn.execute(
            "UPDATE runs SET status='completed', completed_at=?, data=? "
            "WHERE id=? AND discord_id=? AND status='active'",
            (completed_at, _j(run), run_id, str(discord_id)),
        )


def abandon_run(discord_id: str) -> bool:
    """Drop the member's active run (they bailed). Returns whether one existed."""
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE runs SET status='abandoned' WHERE discord_id=? AND status='active'",
            (str(discord_id),),
        )
    return cur.rowcount > 0


def get_cargo_session_start(discord_id: str) -> str | None:
    """The member's hauling-session marker (ISO ts): stats since this point are
    'this session'. None until they first start a session."""
    return _meta_get(f"cargo_session_start:{discord_id}")


def set_cargo_session_start(discord_id: str, ts: str) -> None:
    """Stamp the start of a fresh hauling session (the 'reset' action)."""
    _meta_set(f"cargo_session_start:{discord_id}", ts)


# --- event planner (events + signups) --------------------------------------

# Columns a create/edit writes; `roles` and `category` are JSON. organizer_id,
# status and created_at are set on insert and never edited here (status flips via
# cancel_event); updated_at is stamped on every write.
_EVENT_EDITABLE = ("title", "description", "type", "category", "start_at",
                   "signup_deadline", "duration_min", "location", "event_location",
                   "min_players", "max_players", "roles")

# Columns the create/edit layer hands us as Python lists; stored as JSON text.
_EVENT_JSON = ("roles", "category", "type")


def _event_json_list(raw) -> list:
    """Parse a stored multi-value column (`type` / `category`) into a list.
    Tolerates both the new JSON-list form and legacy single-string rows written
    before these axes went multi-value."""
    if not raw:
        return []
    try:
        parsed = _u(raw)
    except (ValueError, TypeError):
        parsed = None        # legacy plain-string row (not JSON)
    if isinstance(parsed, list):
        return parsed
    return [raw] if isinstance(raw, str) else []


def _event_row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["roles"] = _u(d.get("roles")) or []
    d["category"] = _event_json_list(d.get("category"))
    d["type"] = _event_json_list(d.get("type"))
    return d


def _signup_row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["roles"] = _u(d.get("roles")) or []
    return d


def create_event(d: dict) -> int:
    """Insert a new event (caller supplies validated fields + organizer_id +
    created_at/updated_at). Returns the new event id."""
    with _lock, _conn:
        cur = _conn.execute(
            "INSERT INTO events (organizer_id, title, description, type, category, "
            "start_at, signup_deadline, duration_min, location, event_location, "
            "min_players, max_players, roles, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(d["organizer_id"]), d.get("title"), d.get("description"),
             _j(d.get("type") or []), _j(d.get("category") or []), d.get("start_at"),
             d.get("signup_deadline"), d.get("duration_min"), d.get("location"),
             d.get("event_location"), d.get("min_players"), d.get("max_players"),
             _j(d.get("roles") or []), d.get("status", "scheduled"),
             d.get("created_at"), d.get("updated_at")),
        )
    return cur.lastrowid


def get_event(event_id: int) -> dict | None:
    """One event by id (roles parsed), or None."""
    with _lock:
        row = _conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return _event_row_to_dict(row) if row else None


def list_events(scope: str, now_iso: str) -> list[dict]:
    """Events for the board. `scope='past'` → started before now, freshest first;
    anything else → scheduled events from now on, soonest first. Cancelled future
    events drop out of both (soft-hidden) but stay reachable by id."""
    if scope == "past":
        sql = "SELECT * FROM events WHERE start_at < ? ORDER BY start_at DESC"
    else:
        sql = ("SELECT * FROM events WHERE status='scheduled' AND start_at >= ? "
               "ORDER BY start_at ASC")
    with _lock:
        rows = _conn.execute(sql, (now_iso,)).fetchall()
    return [_event_row_to_dict(r) for r in rows]


def update_event(event_id: int, fields: dict, updated_at: str) -> bool:
    """Replace the editable columns of an event (organizer/admin check is the
    caller's job). Returns whether a row matched."""
    sets = ", ".join(f"{c}=?" for c in _EVENT_EDITABLE)
    vals = [_j(fields.get(c) or []) if c in _EVENT_JSON else fields.get(c)
            for c in _EVENT_EDITABLE]
    with _lock, _conn:
        cur = _conn.execute(
            f"UPDATE events SET {sets}, updated_at=? WHERE id=?",
            (*vals, updated_at, event_id),
        )
    return cur.rowcount > 0


def cancel_event(event_id: int, updated_at: str) -> bool:
    """Soft-cancel: flip status to 'cancelled' (the row + roster survive)."""
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE events SET status='cancelled', updated_at=? WHERE id=?",
            (updated_at, event_id),
        )
    return cur.rowcount > 0


def list_signups(event_id: int) -> list[dict]:
    """All signups for an event (roles parsed), oldest first."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM event_signups WHERE event_id=? ORDER BY created_at, id",
            (event_id,),
        ).fetchall()
    return [_signup_row_to_dict(r) for r in rows]


def upsert_signup(event_id: int, discord_id: str, roles: list, status: str,
                  note: str | None, created_at: str) -> None:
    """Join (or update) a member's signup. UNIQUE(event_id, discord_id) makes a
    re-join overwrite roles/status/note; created_at is preserved from first join."""
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO event_signups (event_id, discord_id, roles, status, note, "
            "created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(event_id, discord_id) DO UPDATE SET "
            "roles=excluded.roles, status=excluded.status, note=excluded.note",
            (event_id, str(discord_id), _j(roles or []), status, note, created_at),
        )


def withdraw_signup(event_id: int, discord_id: str) -> bool:
    """Member bows out: flip their signup to 'withdrawn' (kept, so re-joining is
    cheap). Returns whether they had a signup."""
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE event_signups SET status='withdrawn' WHERE event_id=? AND discord_id=?",
            (event_id, str(discord_id)),
        )
    return cur.rowcount > 0


def list_run_history(discord_id: str, limit: int = 50) -> list[dict]:
    """Completed runs, freshest first (feeds the deferred history/quick-picks)."""
    with _lock:
        rows = _conn.execute(
            "SELECT id, ship, started_at, completed_at, data FROM runs "
            "WHERE discord_id=? AND status='completed' ORDER BY completed_at DESC LIMIT ?",
            (str(discord_id), limit),
        ).fetchall()
    out = []
    for r in rows:
        run = _u(r["data"]) or {}
        run["id"] = r["id"]
        run["completed_at"] = r["completed_at"]
        out.append(run)
    return out


def list_all_completed_runs(since: str | None = None) -> list[dict]:
    """Every member's completed runs (for the guild leaderboard/stats), freshest
    first. Each parsed blob carries its row id, the owning `discord_id`, and
    `completed_at`. `since` (ISO ts) limits to runs completed at/after that point
    for the trailing-week window; None is all-time."""
    q = ("SELECT id, discord_id, completed_at, data FROM runs "
         "WHERE status='completed'")
    params: list = []
    if since:
        q += " AND completed_at >= ?"
        params.append(since)
    q += " ORDER BY completed_at DESC"
    with _lock:
        rows = _conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        run = _u(r["data"]) or {}
        run["id"] = r["id"]
        run["discord_id"] = r["discord_id"]
        run["completed_at"] = r["completed_at"]
        out.append(run)
    return out


def clear_run_history() -> int:
    """Wipe finished hauling runs (completed + abandoned) across all members for
    the admin 'clear cargo statistics' action — this zeroes the leaderboard,
    hauling stats, and every member's run history. In-progress (active) runs are
    left alone so a member mid-haul isn't disrupted. Returns rows removed."""
    with _lock, _conn:
        cur = _conn.execute(
            "DELETE FROM runs WHERE status IN ('completed','abandoned')")
    return cur.rowcount


# --- catalog (custom items) ------------------------------------------------


def add_catalog_item(name: str, kind: str, unit: str | None,
                     creator_id: str, created_at: str) -> int:
    """Insert a custom catalog item (anything not in a feed). Returns its row id;
    the catalog id surfaced to clients is `custom:<id>`."""
    with _lock, _conn:
        cur = _conn.execute(
            "INSERT INTO catalog_items (name, kind, unit, creator_id, created_at) "
            "VALUES (?,?,?,?,?)",
            (name, kind, unit, str(creator_id), created_at),
        )
    return cur.lastrowid


def list_catalog_items() -> list[dict]:
    """All custom catalog rows (catalog.py merges these with the feed items)."""
    with _lock:
        rows = _conn.execute("SELECT * FROM catalog_items ORDER BY name").fetchall()
    return [dict(r) for r in rows]


# --- inventory ledger ------------------------------------------------------


def _inventory_row_to_dict(r: sqlite3.Row) -> dict:
    return dict(r)


def upsert_inventory(owner_id: str, item_id: str, item_name: str, unit: str | None,
                     qty: float, location: str | None, note: str | None,
                     goal_id: int | None, updated_at: str) -> dict:
    """Log a member's holding. One row per (owner, item, location, goal): an
    existing match has its quantity/note SET (not summed) to the new value, so
    re-logging "I hold 80 SCU here" is idempotent rather than stacking. Returns
    the resulting row. (SQLite treats NULLs as distinct in a UNIQUE, so the match
    is done explicitly here with COALESCE rather than via an upsert conflict.)"""
    with _lock, _conn:
        existing = _conn.execute(
            "SELECT id FROM inventory WHERE owner_id=? AND item_id=? "
            "AND COALESCE(location,'')=COALESCE(?,'') "
            "AND COALESCE(goal_id,0)=COALESCE(?,0)",
            (str(owner_id), item_id, location, goal_id),
        ).fetchone()
        if existing:
            _conn.execute(
                "UPDATE inventory SET qty=?, item_name=?, unit=?, note=?, updated_at=? "
                "WHERE id=?",
                (qty, item_name, unit, note, updated_at, existing["id"]),
            )
            rid = existing["id"]
        else:
            cur = _conn.execute(
                "INSERT INTO inventory (owner_id, item_id, item_name, unit, qty, "
                "location, note, goal_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(owner_id), item_id, item_name, unit, qty, location, note,
                 goal_id, updated_at),
            )
            rid = cur.lastrowid
        row = _conn.execute("SELECT * FROM inventory WHERE id=?", (rid,)).fetchone()
    return _inventory_row_to_dict(row)


def list_inventory(owner_id: str | None = None, goal_id: int | None = None) -> list[dict]:
    """Inventory rows, optionally scoped to one member (`owner_id`) and/or one
    goal's contributions (`goal_id`). Freshest first."""
    q, params = "SELECT * FROM inventory", []
    where = []
    if owner_id is not None:
        where.append("owner_id=?")
        params.append(str(owner_id))
    if goal_id is not None:
        where.append("goal_id=?")
        params.append(goal_id)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY updated_at DESC, id DESC"
    with _lock:
        rows = _conn.execute(q, params).fetchall()
    return [_inventory_row_to_dict(r) for r in rows]


def get_inventory(inv_id: int) -> dict | None:
    with _lock:
        row = _conn.execute("SELECT * FROM inventory WHERE id=?", (inv_id,)).fetchone()
    return _inventory_row_to_dict(row) if row else None


def get_holding(owner_id: str, item_id: str, location: str | None) -> dict | None:
    """A member's general (goal-less) holding of an item at a location, if any —
    the parent a goal contribution draws from. Mirrors the upsert match key."""
    with _lock:
        row = _conn.execute(
            "SELECT * FROM inventory WHERE owner_id=? AND item_id=? "
            "AND COALESCE(location,'')=COALESCE(?,'') AND goal_id IS NULL",
            (str(owner_id), item_id, location)).fetchone()
    return _inventory_row_to_dict(row) if row else None


_INV_EDITABLE = ("qty", "location", "note", "unit")


def update_inventory(inv_id: int, fields: dict, updated_at: str) -> bool:
    """Edit an existing holding's qty/location/note/unit (owner/admin check is the
    caller's job). Only keys present in `fields` are written."""
    cols = [c for c in _INV_EDITABLE if c in fields]
    if not cols:
        return False
    sets = ", ".join(f"{c}=?" for c in cols)
    vals = [fields[c] for c in cols]
    with _lock, _conn:
        cur = _conn.execute(
            f"UPDATE inventory SET {sets}, updated_at=? WHERE id=?",
            (*vals, updated_at, inv_id))
    return cur.rowcount > 0


def delete_inventory(inv_id: int) -> bool:
    """Delete a holding and any goal allocations drawn from it (so removing a
    holding withdraws its contributions rather than orphaning them)."""
    with _lock, _conn:
        _conn.execute("DELETE FROM inventory_allocations WHERE inventory_id=?", (inv_id,))
        cur = _conn.execute("DELETE FROM inventory WHERE id=?", (inv_id,))
    return cur.rowcount > 0


# --- goal allocations (contributions drawn from a holding) ------------------


def committed_for_holding(inv_id: int) -> float:
    """Total quantity of a holding already committed to goals (Σ allocations)."""
    with _lock:
        row = _conn.execute(
            "SELECT COALESCE(SUM(qty),0) AS c FROM inventory_allocations "
            "WHERE inventory_id=?", (inv_id,)).fetchone()
    return float(row["c"] or 0)


def get_allocation(alloc_id: int) -> dict | None:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM inventory_allocations WHERE id=?", (alloc_id,)).fetchone()
    return dict(row) if row else None


def find_allocation(inv_id: int, goal_id: int) -> dict | None:
    """The existing allocation from a holding to a goal, if any (contributing again
    to the same goal from the same holding tops it up rather than duplicating)."""
    with _lock:
        row = _conn.execute(
            "SELECT * FROM inventory_allocations WHERE inventory_id=? AND goal_id=?",
            (inv_id, goal_id)).fetchone()
    return dict(row) if row else None


def add_allocation(inv_id: int, goal_id: int, qty: float, now: str) -> int:
    with _lock, _conn:
        cur = _conn.execute(
            "INSERT INTO inventory_allocations "
            "(inventory_id, goal_id, qty, created_at, updated_at) VALUES (?,?,?,?,?)",
            (inv_id, goal_id, qty, now, now))
    return cur.lastrowid


def update_allocation(alloc_id: int, qty: float, now: str) -> bool:
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE inventory_allocations SET qty=?, updated_at=? WHERE id=?",
            (qty, now, alloc_id))
    return cur.rowcount > 0


def delete_allocation(alloc_id: int) -> bool:
    with _lock, _conn:
        cur = _conn.execute(
            "DELETE FROM inventory_allocations WHERE id=?", (alloc_id,))
    return cur.rowcount > 0


def list_goal_contributions(goal_id: int | None = None) -> list[dict]:
    """Goal contributions as flat rows the fill math consumes — each allocation
    joined to its parent holding so it carries item/owner/location. Shaped like the
    old goal-tagged inventory rows ({item_id, item_name, unit, qty, owner_id,
    location, goal_id}) plus allocation/holding ids, so derive_goal_progress is
    unchanged. `goal_id` None returns every goal's contributions (board grouping)."""
    q = ("SELECT a.id AS allocation_id, a.goal_id, a.qty AS qty, "
         "a.inventory_id AS holding_id, i.owner_id, i.item_id, i.item_name, "
         "i.unit, i.location FROM inventory_allocations a "
         "JOIN inventory i ON i.id = a.inventory_id")
    params: list = []
    if goal_id is not None:
        q += " WHERE a.goal_id=?"
        params.append(goal_id)
    with _lock:
        rows = _conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def allocations_for_owner(owner_id: str) -> list[dict]:
    """A member's goal commitments, keyed to the holding they're drawn from, with
    the goal title — powers the nested parent→child render in 'my holdings'."""
    with _lock:
        rows = _conn.execute(
            "SELECT a.id, a.inventory_id, a.goal_id, a.qty, g.title AS goal_title "
            "FROM inventory_allocations a JOIN inventory i ON i.id = a.inventory_id "
            "LEFT JOIN goals g ON g.id = a.goal_id WHERE i.owner_id=? "
            "ORDER BY a.id", (str(owner_id),)).fetchall()
    return [dict(r) for r in rows]


# --- goals -----------------------------------------------------------------


def _goal_row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["line_items"] = _u(d.get("line_items")) or []
    return d


def create_goal(d: dict) -> int:
    """Insert a goal (caller supplies validated fields + creator_id + timestamps)."""
    with _lock, _conn:
        cur = _conn.execute(
            "INSERT INTO goals (creator_id, title, description, priority, deadline, "
            "status, line_items, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(d["creator_id"]), d.get("title"), d.get("description"),
             d.get("priority", 5), d.get("deadline"), d.get("status", "active"),
             _j(d.get("line_items") or []), d.get("created_at"), d.get("updated_at")),
        )
    return cur.lastrowid


def get_goal(goal_id: int) -> dict | None:
    with _lock:
        row = _conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
    return _goal_row_to_dict(row) if row else None


def list_goals(status: str | None = None) -> list[dict]:
    """Goals sorted priority ascending (1 = highest) then soonest deadline, open-
    ended (NULL deadline) last. `status` filters when given."""
    q = "SELECT * FROM goals"
    params: list = []
    if status:
        q += " WHERE status=?"
        params.append(status)
    # NULL deadlines sort last within a priority; SQLite orders NULL first, so key
    # on (deadline IS NULL) before deadline.
    q += " ORDER BY priority ASC, deadline IS NULL, deadline ASC, id DESC"
    with _lock:
        rows = _conn.execute(q, params).fetchall()
    return [_goal_row_to_dict(r) for r in rows]


_GOAL_EDITABLE = ("title", "description", "priority", "deadline", "status", "line_items")
_GOAL_JSON = ("line_items",)


def update_goal(goal_id: int, fields: dict, updated_at: str) -> bool:
    """Replace the editable columns of a goal (creator/admin check is the caller's
    job). Only the keys present in `fields` are written."""
    cols = [c for c in _GOAL_EDITABLE if c in fields]
    if not cols:
        return False
    sets = ", ".join(f"{c}=?" for c in cols)
    vals = [_j(fields.get(c) or []) if c in _GOAL_JSON else fields.get(c) for c in cols]
    with _lock, _conn:
        cur = _conn.execute(
            f"UPDATE goals SET {sets}, updated_at=? WHERE id=?",
            (*vals, updated_at, goal_id),
        )
    return cur.rowcount > 0


def set_goal_status(goal_id: int, status: str, updated_at: str) -> bool:
    """Flip just the status (used by the auto met/active recompute on read)."""
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE goals SET status=?, updated_at=? WHERE id=?",
            (status, updated_at, goal_id),
        )
    return cur.rowcount > 0


def delete_goal(goal_id: int) -> bool:
    """Hard-delete a goal and drop its allocations (the parent holdings survive as
    general inventory; only the earmarks against this goal go away)."""
    with _lock, _conn:
        _conn.execute("DELETE FROM inventory_allocations WHERE goal_id=?", (goal_id,))
        cur = _conn.execute("DELETE FROM goals WHERE id=?", (goal_id,))
    return cur.rowcount > 0


# --- marketplace listings + offers -----------------------------------------


_LISTING_EDITABLE = ("qty", "price_auec", "start_price", "buyout_auec", "ends_at",
                      "want", "note", "status", "attributes")
_LISTING_JSON = ("attributes",)


def _listing_row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["seller_confirmed"] = bool(d.get("seller_confirmed"))
    d["buyer_confirmed"] = bool(d.get("buyer_confirmed"))
    d["attributes"] = _u(d.get("attributes"))     # JSON crafted-quality blob → dict
    return d


def _recompute_denorm(listing_id: int) -> None:
    """Recompute a listing's denormalized board columns from its current offers
    (caller must hold _lock). `sort_price` is the board's display/sort price —
    sale: the fixed ask; auction: the highest standing bid, else the start price
    (matches derive_auction_state.high_bid, which is the plain max); barter: NULL
    (no aUEC price). `offer_count` is the count of still-active offers."""
    row = _conn.execute(
        "SELECT mode, price_auec, start_price FROM listings WHERE id=?",
        (listing_id,)).fetchone()
    if row is None:
        return
    cnt = _conn.execute(
        "SELECT COUNT(*) AS c FROM listing_offers WHERE listing_id=? AND status='active'",
        (listing_id,)).fetchone()["c"]
    if row["mode"] == "sale":
        sort_price = row["price_auec"]
    elif row["mode"] == "auction":
        high = _conn.execute(
            "SELECT MAX(amount_auec) AS m FROM listing_offers WHERE listing_id=? "
            "AND status IN ('active','accepted')", (listing_id,)).fetchone()["m"]
        sort_price = high if high is not None else row["start_price"]
    else:                                       # barter — no aUEC price
        sort_price = None
    _conn.execute("UPDATE listings SET sort_price=?, offer_count=? WHERE id=?",
                  (sort_price, int(cnt or 0), listing_id))


def refresh_listing_denorm(listing_id: int) -> None:
    """Public entry the endpoints call after any mutation (offer placed/withdrawn,
    deal settled, auction lapsed, listing created/price-edited) to keep the board's
    denormalized sort_price/offer_count current. Cheap; lets the board read stay
    pure SQL with no per-listing derivation."""
    with _lock, _conn:
        _recompute_denorm(listing_id)


def _backfill_listing_denorm() -> None:
    """One-time reconcile of the denorm columns for listings created before the
    columns existed — run only on the migration boot that adds them (init gates on
    `_ensure_column`'s return). Recomputes from the offers, the source of truth.
    Idempotent, but per-mutation maintenance keeps the columns current afterward, so
    this never runs again."""
    ids = [r["id"] for r in _conn.execute("SELECT id FROM listings").fetchall()]
    for lid in ids:
        _recompute_denorm(lid)


def create_listing(d: dict) -> int:
    """Insert a listing (caller supplies validated fields + seller_id + timestamps)."""
    with _lock, _conn:
        cur = _conn.execute(
            "INSERT INTO listings (seller_id, item_id, item_name, unit, qty, mode, "
            "price_auec, start_price, buyout_auec, ends_at, want, status, note, "
            "attributes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(d["seller_id"]), d["item_id"], d.get("item_name"), d.get("unit"),
             d.get("qty", 1), d.get("mode", "sale"), d.get("price_auec"),
             d.get("start_price"), d.get("buyout_auec"), d.get("ends_at"),
             d.get("want"), d.get("status", "open"), d.get("note"),
             _j(d.get("attributes")), d.get("created_at"), d.get("updated_at")),
        )
        _recompute_denorm(cur.lastrowid)        # seed the board columns
    return cur.lastrowid


def get_listing(listing_id: int) -> dict | None:
    with _lock:
        row = _conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    return _listing_row_to_dict(row) if row else None


# Board sort modes → SQL ORDER BY. Price/expiry push their NULLs (barter has no
# price; non-auctions have no end time) to the bottom regardless of direction so a
# sort never buries real rows under blanks. Every clause ends with `id` for a
# stable, deterministic page boundary.
_LISTING_SORTS = {
    "recent":     "created_at DESC, id DESC",
    "oldest":     "created_at ASC, id ASC",
    "price_asc":  "sort_price IS NULL, sort_price ASC, id DESC",
    "price_desc": "sort_price IS NULL, sort_price DESC, id DESC",
    "ending":     "ends_at IS NULL, ends_at ASC, id DESC",
}

# Item-kind filter values → the catalog id prefix they map to (the board filters by
# the listing's `item_id` prefix — no catalog join needed, no schema change).
_LISTING_KIND_PREFIX = {"commodity": "commodity:", "ship": "ship:",
                        "item": "item:", "custom": "custom:"}


def _listing_filter_sql(mode, item_id, seller_id, status, open_only,
                        q, kind, min_price, max_price):
    """Build the shared WHERE clause (+ params) for list_listings / count_listings
    so the browse and count queries can never drift apart."""
    where, params = [], []
    if mode:
        where.append("mode=?"); params.append(mode)
    if item_id:
        where.append("item_id=?"); params.append(item_id)
    if seller_id:
        where.append("seller_id=?"); params.append(str(seller_id))
    if status:
        where.append("status=?"); params.append(status)
    elif open_only:
        where.append("status='open'")
    if q:
        # Escape LIKE metacharacters so a literal % or _ in the query doesn't act as
        # a wildcard (escape the backslash first, then % and _).
        needle = (q.strip().lower().replace("\\", "\\\\")
                  .replace("%", "\\%").replace("_", "\\_"))
        where.append("LOWER(item_name) LIKE ? ESCAPE '\\'"); params.append(f"%{needle}%")
    prefix = _LISTING_KIND_PREFIX.get(kind)
    if prefix:
        where.append("item_id LIKE ?"); params.append(f"{prefix}%")
    # Price range rides the denormalized sort_price (pure SQL — no post-derivation).
    # A min/max necessarily excludes barter, which has no price.
    if min_price is not None:
        where.append("sort_price IS NOT NULL AND sort_price >= ?"); params.append(min_price)
    if max_price is not None:
        where.append("sort_price IS NOT NULL AND sort_price <= ?"); params.append(max_price)
    return (" WHERE " + " AND ".join(where) if where else ""), params


def list_listings(mode: str | None = None, item_id: str | None = None,
                  seller_id: str | None = None, status: str | None = None,
                  open_only: bool = False, q: str | None = None,
                  kind: str | None = None, min_price: float | None = None,
                  max_price: float | None = None, sort: str = "recent",
                  limit: int | None = None, offset: int = 0) -> tuple[list[dict], int]:
    """Browse listings with optional filters, a sort, and paging. Filters: mode /
    exact item / seller / status (or `open_only`), free-text `q` over the item name,
    item `kind` (catalog-id prefix), and a `min_price`/`max_price` band over the
    denormalized sort_price. `sort` is one of `_LISTING_SORTS` (default freshest).
    `limit`/`offset` page the result. Returns `(rows, total)` where `total` is the
    full unpaged match count, so the board can show "1–25 of N"."""
    clause, params = _listing_filter_sql(mode, item_id, seller_id, status,
                                         open_only, q, kind, min_price, max_price)
    order = _LISTING_SORTS.get(sort, _LISTING_SORTS["recent"])
    sql = f"SELECT * FROM listings{clause} ORDER BY {order}"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"; params = [*params, int(limit), int(max(0, offset))]
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
        if limit is None:
            total = len(rows)
        else:
            cparams = params[:-2]               # drop LIMIT/OFFSET for the count
            total = _conn.execute(
                f"SELECT COUNT(*) AS c FROM listings{clause}", cparams).fetchone()["c"]
    return [_listing_row_to_dict(r) for r in rows], int(total)


def update_listing(listing_id: int, fields: dict, updated_at: str) -> bool:
    """Replace editable columns of a listing (seller/admin check is the caller's
    job). Only keys present in `fields` (and in the editable set) are written."""
    cols = [c for c in _LISTING_EDITABLE if c in fields]
    if not cols:
        return False
    sets = ", ".join(f"{c}=?" for c in cols)
    vals = [_j(fields[c]) if c in _LISTING_JSON else fields[c] for c in cols]
    with _lock, _conn:
        cur = _conn.execute(
            f"UPDATE listings SET {sets}, updated_at=? WHERE id=?",
            (*vals, updated_at, listing_id))
        _recompute_denorm(listing_id)           # price/start edits move sort_price
    return cur.rowcount > 0


def settle_listing(listing_id: int, buyer_id: str | None, status: str,
                   updated_at: str) -> bool:
    """Move a listing to `pending` with its buyer (a deal struck), or flip status
    for an expiry/cancel. Resets the dual-confirm flags when a new deal starts."""
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE listings SET status=?, buyer_id=?, seller_confirmed=0, "
            "buyer_confirmed=0, updated_at=? WHERE id=?",
            (status, str(buyer_id) if buyer_id is not None else None,
             updated_at, listing_id))
        _recompute_denorm(listing_id)           # keep the board count/price in step
    return cur.rowcount > 0


def confirm_listing(listing_id: int, side: str, updated_at: str,
                    completed_at: str | None = None) -> bool:
    """Record one side's handoff confirmation (`side` is 'seller' or 'buyer'); when
    `completed_at` is given the listing is also flipped to completed (the caller
    decides that once both flags are set)."""
    col = "seller_confirmed" if side == "seller" else "buyer_confirmed"
    sets = f"{col}=1, updated_at=?"
    params: list = [updated_at]
    if completed_at is not None:
        sets += ", status='completed', completed_at=?"
        params.append(completed_at)
    params.append(listing_id)
    with _lock, _conn:
        cur = _conn.execute(f"UPDATE listings SET {sets} WHERE id=?", params)
        _recompute_denorm(listing_id)
    return cur.rowcount > 0


def set_listing_status(listing_id: int, status: str, updated_at: str) -> bool:
    """Flip just the status (used by the lazy auction-expiry recompute on read)."""
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE listings SET status=?, updated_at=? WHERE id=?",
            (status, updated_at, listing_id))
        _recompute_denorm(listing_id)
    return cur.rowcount > 0


def delete_listing(listing_id: int) -> bool:
    """Hard-delete a listing and its offers."""
    with _lock, _conn:
        _conn.execute("DELETE FROM listing_offers WHERE listing_id=?", (listing_id,))
        cur = _conn.execute("DELETE FROM listings WHERE id=?", (listing_id,))
    return cur.rowcount > 0


def completed_deals_count(member_id: str) -> int:
    """How many listings a member has completed as seller or buyer — the only
    reputation signal in v1 (derived, never stored)."""
    did = str(member_id)
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) AS c FROM listings WHERE status='completed' "
            "AND (seller_id=? OR buyer_id=?)", (did, did)).fetchone()
    return int(row["c"] or 0)


def completed_deals_counts(member_ids) -> dict:
    """Completed-deals count for several members in one query — the board's
    reputation signal without an N+1 (one query for the whole page's sellers).
    Returns {member_id: count}; a member with no completed deals maps to 0."""
    ids = [str(m) for m in dict.fromkeys(member_ids) if m]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    with _lock:
        rows = _conn.execute(
            f"SELECT seller_id, buyer_id FROM listings WHERE status='completed' "
            f"AND (seller_id IN ({ph}) OR buyer_id IN ({ph}))", (*ids, *ids)).fetchall()
    counts = {i: 0 for i in ids}
    idset = set(ids)
    for r in rows:
        if r["seller_id"] in idset:
            counts[r["seller_id"]] += 1
        if r["buyer_id"] in idset:
            counts[r["buyer_id"]] += 1
    return counts


def add_offer(listing_id: int, bidder_id: str, amount_auec: float | None,
              offer_item_id: str | None, offer_item_name: str | None,
              offer_note: str | None, created_at: str) -> int:
    with _lock, _conn:
        cur = _conn.execute(
            "INSERT INTO listing_offers (listing_id, bidder_id, amount_auec, "
            "offer_item_id, offer_item_name, offer_note, status, created_at) "
            "VALUES (?,?,?,?,?,?,'active',?)",
            (listing_id, str(bidder_id), amount_auec, offer_item_id,
             offer_item_name, offer_note, created_at))
        _recompute_denorm(listing_id)           # offer_count++ / auction high bid
    return cur.lastrowid


def get_offer(offer_id: int) -> dict | None:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM listing_offers WHERE id=?", (offer_id,)).fetchone()
    return dict(row) if row else None


def list_offers(listing_id: int) -> list[dict]:
    """All offers on a listing, oldest first (so equal-amount bids tie-break by
    arrival — the rule derive_auction_state pins)."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM listing_offers WHERE listing_id=? "
            "ORDER BY created_at ASC, id ASC", (listing_id,)).fetchall()
    return [dict(r) for r in rows]


def set_offer_status(offer_id: int, status: str) -> bool:
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE listing_offers SET status=? WHERE id=?", (status, offer_id))
        row = _conn.execute(
            "SELECT listing_id FROM listing_offers WHERE id=?", (offer_id,)).fetchone()
        if row is not None:
            _recompute_denorm(row["listing_id"])    # withdraw/accept moves count/bid
    return cur.rowcount > 0


def reject_other_offers(listing_id: int, keep_offer_id: int) -> None:
    """Mark every active offer on a listing 'lost' except the accepted one (called
    when a deal is struck so the losing bids stop showing as open)."""
    with _lock, _conn:
        _conn.execute(
            "UPDATE listing_offers SET status='lost' WHERE listing_id=? "
            "AND id!=? AND status='active'", (listing_id, keep_offer_id))
        _recompute_denorm(listing_id)


# --- account deletion (privacy: erase a member) ----------------------------


def delete_member(discord_id: str, player_ids: set[int]) -> dict:
    """Erase a member's personal data for an account-deletion request, in one
    transaction. Their *contributions* (custom POIs / observations) are kept for
    the org but de-identified: owner_id/owner_handle are nulled for every
    PlayerID the member owned. Everything personal — watcher tokens, saved ships,
    cargo runs, the handle->Discord bindings, and the hauling-session marker — is
    hard-deleted. Returns per-table counts; the caller mirrors these changes in
    the in-memory caches under the hub lock."""
    did = str(discord_id)
    counts = {"pois_anonymized": 0, "pois_deleted": 0, "observations_anonymized": 0}
    with _lock, _conn:
        if player_ids:
            marks = ",".join("?" * len(player_ids))
            ids = list(player_ids)
            # Private POIs were never shared with the org, so de-identifying them
            # would just leave invisible orphans — hard-delete them instead.
            counts["pois_deleted"] = _conn.execute(
                f"DELETE FROM custom_pois "
                f"WHERE private=1 AND owner_id IN ({marks})", ids).rowcount
            counts["pois_anonymized"] = _conn.execute(
                f"UPDATE custom_pois SET owner_id=NULL, owner_handle=NULL "
                f"WHERE owner_id IN ({marks})", ids).rowcount
            counts["observations_anonymized"] = _conn.execute(
                f"UPDATE observations SET owner_id=NULL, owner_handle=NULL "
                f"WHERE owner_id IN ({marks})", ids).rowcount
        counts["tokens"] = _conn.execute(
            "DELETE FROM watcher_tokens WHERE discord_id=?", (did,)).rowcount
        counts["ships"] = _conn.execute(
            "DELETE FROM user_ships WHERE discord_id=?", (did,)).rowcount
        counts["runs"] = _conn.execute(
            "DELETE FROM runs WHERE discord_id=?", (did,)).rowcount
        counts["handles"] = _conn.execute(
            "DELETE FROM handles WHERE discord_id=?", (did,)).rowcount
        # Inventory pledges are personal holdings — hard-delete them, withdrawing
        # any goal allocations drawn from them first so none dangle.
        _conn.execute(
            "DELETE FROM inventory_allocations WHERE inventory_id IN "
            "(SELECT id FROM inventory WHERE owner_id=?)", (did,))
        counts["inventory"] = _conn.execute(
            "DELETE FROM inventory WHERE owner_id=?", (did,)).rowcount
        # Goals and custom catalog items are org reference data — keep them but
        # de-identify the creator (mirrors how shared POIs are anonymized).
        counts["goals_anonymized"] = _conn.execute(
            "UPDATE goals SET creator_id='' WHERE creator_id=?", (did,)).rowcount
        _conn.execute(
            "UPDATE catalog_items SET creator_id='' WHERE creator_id=?", (did,))
        # Marketplace listings are personal member-to-member sales — hard-delete the
        # member's own listings (and their offers), and their bids on others'.
        # Where they were merely a buyer, de-identify rather than delete the seller's
        # record (mirrors how shared POIs are anonymized, not removed).
        _conn.execute(
            "DELETE FROM listing_offers WHERE listing_id IN "
            "(SELECT id FROM listings WHERE seller_id=?)", (did,))
        _conn.execute("DELETE FROM listing_offers WHERE bidder_id=?", (did,))
        counts["listings"] = _conn.execute(
            "DELETE FROM listings WHERE seller_id=?", (did,)).rowcount
        _conn.execute(
            "UPDATE listings SET buyer_id=NULL WHERE buyer_id=?", (did,))
        _conn.execute("DELETE FROM meta WHERE key=?",
                      (f"cargo_session_start:{did}",))
    return counts


# --- one-time migration from the legacy JSON files -------------------------


def _meta_get(key: str):
    with _lock:
        row = _conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(key: str, value: str) -> None:
    with _lock, _conn:
        _conn.execute("INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (key, value))


# --- settings (key/value in the meta table) --------------------------------


def get_setting(key: str, default: str | None = None) -> str | None:
    v = _meta_get(key)
    return default if v is None else v


def set_setting(key: str, value: str) -> None:
    _meta_set(key, value)


def import_legacy_json(data_dir, observation_categories) -> None:
    """One-time import of the old per-file JSON into the DB. Guarded by a meta
    flag so it runs exactly once (even if the user later deletes every row),
    and the JSON files are left in place as a backup."""
    if _meta_get("legacy_imported"):
        return

    def _read(name):
        try:
            return json.loads((data_dir / name).read_text())
        except (OSError, ValueError):
            return []

    for d in _read("custom_pois.json"):
        try:
            add_custom_poi(d)
        except (sqlite3.Error, KeyError, TypeError) as exc:
            print(f"[sc-nav] legacy custom POI import skipped: {exc}")

    for category, spec in observation_categories.items():
        for d in _read(spec["file"]):
            d = dict(d)
            d.setdefault("category", category)
            try:
                add_observation(d)
            except (sqlite3.Error, KeyError, TypeError) as exc:
                print(f"[sc-nav] legacy observation import skipped: {exc}")

    for h in _read("handles.json"):
        try:
            upsert_handle(h)
        except (sqlite3.Error, KeyError) as exc:
            print(f"[sc-nav] legacy handle import skipped: {exc}")

    for t in _read("watcher_tokens.json"):
        try:
            add_token(t)
        except (sqlite3.Error, KeyError) as exc:
            print(f"[sc-nav] legacy token import skipped: {exc}")

    _meta_set("legacy_imported", "1")
    print("[sc-nav] legacy JSON imported into SQLite")
