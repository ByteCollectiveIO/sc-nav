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


def _ensure_column(table: str, column: str, decl: str) -> None:
    cols = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
