"""SC Nav server.

Receives positions from the Windows clipboard watcher, computes navigation
state against the containers/poi dataset, and pushes live updates to browser
clients over WebSocket.

Run:  uvicorn app:app --host 0.0.0.0 --port 8765
Data: ../poi by default, override with SC_NAV_DATA=/path/to/poi
"""

import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

import auth
import catalog
import db
import event_taxonomy
import nav_core
import notify
from version import __version__ as APP_VERSION

DATA_DIR = Path(os.environ.get("SC_NAV_DATA", Path(__file__).parent.parent / "poi"))
STATIC_DIR = Path(__file__).parent / "static"
# Admin-uploaded guild logo lives on the writable /data volume (not the static
# dir, which is baked into the image and lost on rebuild). Served by a route,
# not the StaticFiles mount. PNG/JPG/WebP only — no SVG (script-injection risk).
BRANDING_DIR = DATA_DIR / "branding"
_LOGO_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_LOGO_MAX_BYTES = 2 * 1024 * 1024


def _sniff_image(data: bytes, ext: str) -> bool:
    """True when the leading bytes match the magic number for `ext`. Guards the
    upload against a mislabeled (or polyglot) file slipping onto the volume."""
    if ext == "png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == "jpg":
        return data[:3] == b"\xff\xd8\xff"
    if ext == "webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False
# Watcher source for the Setup-page download. In the Docker image the files are
# copied to server/watcher_src (see Dockerfile); in a dev checkout they live in
# the repo's ../watcher. First existing wins.
WATCHER_DIR = next(
    (p for p in (Path(__file__).parent / "watcher_src",
                 Path(__file__).parent.parent / "watcher") if p.is_dir()),
    None,
)
# Files bundled into the download (everything else — tests, __pycache__, any
# stale watcher_config.json — is left out).
WATCHER_BUNDLE_FILES = ("sc_nav_watcher.py", "run_watcher.bat", "README.md")

# Live dataset endpoints (the files in DATA_DIR act as the offline cache).
OC_URL = os.environ.get("SC_NAV_OC_URL", "https://starmap.space/api/v3/oc/index.php")
POI_URL = os.environ.get("SC_NAV_POI_URL", "https://starmap.space/api/v3/pois/index.php")
COMMODITIES_URL = os.environ.get("SC_NAV_COMMODITIES_URL", "https://api.uexcorp.uk/2.0/commodities")
SHIPS_URL = os.environ.get("SC_NAV_SHIPS_URL", "https://api.uexcorp.uk/2.0/vehicles")
# Equipment / ship parts (weapons, components, armor, attachments, …). The feed
# lists prices per terminal; we only consume the distinct item names for the
# shared catalog. One row per (item, terminal), so it's large — cached to disk.
ITEMS_URL = os.environ.get("SC_NAV_ITEMS_URL", "https://api.uexcorp.space/2.0/items_prices_all/")
# Commodity trading (trade-route planner, #21): per-terminal buy/sell prices +
# the terminal catalog that places them. Both are large (one row per commodity
# per terminal / one row per terminal) so they're cached to disk like the feeds
# above. Terminals carry no game-file x/y/z — nav_core.match_terminals resolves
# each to a routable POI by name.
TERMINALS_URL = os.environ.get("SC_NAV_TERMINALS_URL", "https://api.uexcorp.space/2.0/terminals")
TRADE_PRICES_URL = os.environ.get("SC_NAV_TRADE_PRICES_URL", "https://api.uexcorp.space/2.0/commodities_prices_all")
OFFLINE = os.environ.get("SC_NAV_OFFLINE") == "1"

# Canonical public URL (e.g. https://nav.bytecollective.io). When set it is the
# only address baked into the watcher download bundle, so a spoofed Host /
# X-Forwarded-Host header can't redirect a member's watcher (and its token).
PUBLIC_BASE_URL = os.environ.get("SC_NAV_PUBLIC_URL", "").rstrip("/")

data_info = {"source": None, "fetched_at": None, "error": None}


def _fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "sc-nav/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def starmap_pois_enabled() -> bool:
    """Whether to load starmap.space's POI catalog. Defaults OFF: a new org
    starts from a blank POI database (their own custom POIs only) and an admin
    opts in. Once opted in, the flag persists, so every restart and /api/refresh
    re-fetches the latest catalog automatically. Celestial bodies (the container
    catalog) are always loaded — the nav math needs them."""
    return db.get_setting("starmap_pois_enabled", "0") == "1"


def wiki_pois_enabled() -> bool:
    """Whether to import the SC Wiki locations catalog (#28a) as routable POIs
    — datamined outposts, stations and asteroid clusters the starmap feed
    lacks. Same blank-slate opt-in as starmap_pois_enabled, toggled
    independently; the source is the committed poi/locations.json snapshot
    (tools/sync_locations.py), not a runtime fetch. Its QT arrival radii are
    enrichment and load regardless of this flag."""
    return db.get_setting("wiki_pois_enabled", "0") == "1"


def member_role_id() -> str:
    """Discord role a user must hold (besides guild membership) to sign in.
    Empty = any guild member is allowed. DB-backed + admin-editable, seeded by
    the ORG_MEMBER_ROLE_ID env default. Admins (ADMIN_IDS) bypass this check."""
    return db.get_setting("member_role_id", auth.MEMBER_ROLE_ID) or ""


def extra_admin_ids() -> list[str]:
    """Discord ids granted admin from the UI (DB-backed, admin-editable).
    Additive to the env `ADMIN_IDS` root admins, which the UI can't touch."""
    raw = db.get_setting("extra_admin_ids", "") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


def admin_ids() -> set[str]:
    """Effective admin set: the immutable env root admins (auth.ADMIN_IDS)
    unioned with the DB-backed list. Keeping the env admins as a floor means a
    wrecked DB or a bad UI edit can never lock everyone out, and resolving this
    live (rather than trusting the login-time flag) makes a grant/revoke take
    effect on the member's very next request."""
    return auth.ADMIN_IDS | set(extra_admin_ids())


def obs_fresh_window_h() -> int:
    """How many hours an observation stays "fresh" for the map markers + NEARBY
    list. Resource nodes and fauna are ephemeral (SC respawns them), so stale
    sightings are hidden from those actionable views by default. DB-backed +
    admin-editable so it can be tuned to SC's respawn cadence without a redeploy;
    the heatmap still aggregates all sightings regardless of this."""
    try:
        return max(1, int(db.get_setting("obs_fresh_window_h", "48")))
    except (TypeError, ValueError):
        return 48


def org_name() -> str:
    """The guild's display name, shown beside the built-in branding on the login
    splash and the app chooser. DB-backed + admin-editable; empty = show the
    generic product copy only."""
    return (db.get_setting("org_name", "") or "").strip()


def motd_state() -> dict:
    """The message-of-the-day an admin can broadcast to members: the text plus the
    epoch it was last set. `updated` gives the client a stable key to remember a
    per-member dismissal and to re-show the banner whenever the text changes."""
    text = (db.get_setting("motd", "") or "").strip()
    try:
        updated = int(db.get_setting("motd_updated", "0") or "0")
    except (TypeError, ValueError):
        updated = 0
    return {"text": text, "updated": updated if text else 0}


def lfg_ageoff_min() -> int:
    """Minutes a Group Finder post lives before it ages off the board (removed).
    DB-backed + admin-editable so an org can tune how "right now" the board feels
    without a redeploy; applies live to existing posts (the lifecycle is computed
    from `created`, not frozen at post time). Default 180 (3h)."""
    try:
        return max(1, int(db.get_setting("lfg_ageoff_min", "180")))
    except (TypeError, ValueError):
        return 180


def lfg_stale_min() -> int:
    """Minutes a Group Finder post stays "fresh" (green) before it's flagged stale
    (yellow). Always kept strictly below the age-off so every post has a green
    phase. Default 120 (2h)."""
    ageoff = lfg_ageoff_min()
    try:
        v = int(db.get_setting("lfg_stale_min", "120"))
    except (TypeError, ValueError):
        v = 120
    return max(1, min(v, ageoff - 1)) if ageoff > 1 else 1


def warning_ageoff_min() -> int:
    """Minutes a pirate danger warning lives before it ages off the board (#24).
    Admin-editable; applies live to existing warnings (the lifecycle is computed
    from `created`, not frozen at post time). Warnings are far more ephemeral than
    LFG posts — a snare gets set up and abandoned within the hour. Default 60."""
    try:
        return max(1, int(db.get_setting("warning_ageoff_min", "60")))
    except (TypeError, ValueError):
        return 60


def warning_stale_min() -> int:
    """Minutes a warning stays "fresh" before it's flagged stale (nearing age-off,
    "still there?"). Always kept strictly below the age-off so every warning has a
    fresh phase. Default 40."""
    ageoff = warning_ageoff_min()
    try:
        v = int(db.get_setting("warning_stale_min", "40"))
    except (TypeError, ValueError):
        v = 40
    return max(1, min(v, ageoff - 1)) if ageoff > 1 else 1


def hazard_radius_km() -> int:
    """Base hazard radius (km) a danger warning projects for snare-detour routing
    (#24 v2) — the corridor the trade/cargo planners route around. Severity scales
    it in code (sighted ×0.5, active ×1.0, deadly ×1.5). Admin-editable. Default
    5000 km: far wider than a Mantis bubble, covering anchor imprecision + roaming
    pirates, yet trivial at Gm leg scales."""
    try:
        return max(1, int(db.get_setting("hazard_radius_km", "5000")))
    except (TypeError, ValueError):
        return 5000


def stock_ageoff_min() -> int:
    """Minutes a trade stock report (out-of-stock / low-stock, #21) lives before
    it ages off — and stops steering the trade solver away from that buy. Terminal
    inventory in-game restocks on the order of an hour or two, so the default is
    180; admins tune it like the LFG/warning windows. Applies live (computed from
    `created`, not frozen at post time)."""
    try:
        return max(1, int(db.get_setting("stock_ageoff_min", "180")))
    except (TypeError, ValueError):
        return 180


def active_stock_reports() -> list[dict]:
    """The live stock board: fresh reports only (side effect: expired rows are
    pruned). Each row gains `age_s` for the client's badges."""
    now = time.time()
    reports = db.stock_reports_since(now - stock_ageoff_min() * 60)
    for r in reports:
        r["age_s"] = max(0, int(now - (r.get("created") or now)))
    return reports


def load_wiki_locations() -> list[dict]:
    """The committed SC Wiki locations snapshot (poi/locations.json, #28) —
    regenerated per game patch by tools/sync_locations.py, never fetched at
    runtime. Static, code-versioned reference data like the quantum/blueprint
    feeds, so the image-bundled copy next to the server code is authoritative
    and tried FIRST; `DATA_DIR` is the dev/CI fallback. In production DATA_DIR
    is a named volume seeded only on first creation, so a file added in a later
    release never reaches an existing volume (the v0.37.1 lesson — see
    load_quantum + the Dockerfile COPY). Empty list when absent everywhere."""
    for base in (Path(__file__).parent, DATA_DIR):     # code-bundled wins over the volume
        try:
            locs = json.loads((base / "locations.json").read_text()).get("locations")
            if locs:
                return locs
        except (OSError, json.JSONDecodeError):
            continue
    print("[sc-nav] wiki locations catalog not found — #28 features degrade to off")
    return []


wiki_locations = load_wiki_locations()


def _apply_wiki_catalog(fresh: nav_core.NavData) -> nav_core.NavData:
    """Fold the wiki locations catalog into a freshly parsed NavData: new POIs
    + QT-marker promotion of starmap POIs the game now allows jumping to, when
    the org opted in (#28a); per-POI QT arrival radii always (#28b — physics
    metadata, enriches the starmap/synth POIs regardless of the toggle)."""
    if wiki_pois_enabled():
        nav_core.add_wiki_pois(fresh, wiki_locations)
        nav_core.upgrade_qt_markers(fresh, wiki_locations)
    nav_core.annotate_arrival_radii(fresh, wiki_locations)
    return fresh


def load_nav_data() -> nav_core.NavData:
    """Fetch live data from starmap.space; fall back to the on-disk cache.

    A successful fetch refreshes the cache files, so the newest good dataset
    survives restarts and network outages. The POI catalog is skipped when the
    org has opted out (containers are always loaded). The wiki locations
    catalog (committed snapshot) is folded in last, so its dedup sees the
    complete starmap set.
    """
    want_pois = starmap_pois_enabled()
    if not OFFLINE:
        try:
            oc_raw = _fetch_json(OC_URL)
            poi_raw = _fetch_json(POI_URL) if want_pois else []
            if len(oc_raw) < 50 or (want_pois and len(poi_raw) < 100):
                raise ValueError(
                    f"suspiciously small dataset ({len(oc_raw)} containers, "
                    f"{len(poi_raw)} pois) — keeping cache"
                )
            fresh = nav_core.parse_data(oc_raw, poi_raw)
            try:
                (DATA_DIR / "containers.json").write_text(json.dumps(oc_raw))
                if want_pois:
                    (DATA_DIR / "poi.json").write_text(json.dumps(poi_raw))
            except OSError as exc:
                print(f"[sc-nav] cache write failed (continuing): {exc}")
            data_info.update(
                source="live",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )
            return _apply_wiki_catalog(fresh)
        except Exception as exc:
            data_info["error"] = str(exc)
            print(f"[sc-nav] live fetch failed, using cached data: {exc}")
    data_info["source"] = "offline" if OFFLINE else "cache"
    oc_raw = json.loads((DATA_DIR / "containers.json").read_text())
    poi_raw = json.loads((DATA_DIR / "poi.json").read_text()) if want_pois else []
    return _apply_wiki_catalog(nav_core.parse_data(oc_raw, poi_raw))


COMMODITIES_FILE = DATA_DIR / "commodities.json"  # cached uexcorp commodities
SHIPS_FILE = DATA_DIR / "ships.json"               # cached uexcorp vehicles
ITEMS_FILE = DATA_DIR / "items.json"               # cached uexcorp items_prices_all
TERMINALS_FILE = DATA_DIR / "trade_terminals.json" # cached uexcorp terminals
TRADE_PRICES_FILE = DATA_DIR / "trade_prices.json" # cached uexcorp commodities_prices_all
DB_FILE = DATA_DIR / "sc_nav.db"                   # user-contributed data (Phase 2)


def _load_json_list(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save_json_list(path: Path, items: list[dict]) -> None:
    path.write_text(json.dumps(items, indent=1))


def load_biomes() -> dict:
    """Normalize server/biomes.json into lookups for the biome datalist:
      by_body[system][body] -> [biome names]   (system/body lowercased)
      by_system[system]      -> [union of the system's biome names]
      all                    -> [every biome name]
    Source shape: star_systems -> system -> planets -> planet ->
      { "biomes": [{biome_name,...}], "moons": { moon: [{biome_name,...}] } }.
    Both planets and moons land in by_body; the UI narrows to the player's body
    and falls back body -> system -> all."""
    out = {"by_body": {}, "by_system": {}, "all": []}
    try:
        raw = json.loads((Path(__file__).parent / "biomes.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[sc-nav] biomes load failed: {exc}")
        return out

    def names_of(entries):
        return sorted({e["biome_name"] for e in (entries or []) if e.get("biome_name")})

    all_names = set()
    for system, sysv in (raw.get("star_systems") or {}).items():
        s = system.lower()
        sys_names = set()
        bodies = out["by_body"].setdefault(s, {})
        for planet, pv in ((sysv or {}).get("planets") or {}).items():
            pv = pv or {}
            pnames = names_of(pv.get("biomes"))
            if pnames:
                bodies[planet.lower()] = pnames
                sys_names.update(pnames)
            for moon, entries in (pv.get("moons") or {}).items():
                mnames = names_of(entries)
                if mnames:
                    bodies[moon.lower()] = mnames
                    sys_names.update(mnames)
        out["by_system"][s] = sorted(sys_names)
        all_names.update(sys_names)
    out["all"] = sorted(all_names)
    return out


def load_fauna_names() -> list[str]:
    """Curated fauna/species names for the Add Fauna datalist. A committed
    reference list shipped with the server (server/fauna.json)."""
    try:
        names = json.loads((Path(__file__).parent / "fauna.json").read_text())
        return sorted({n for n in names if n}, key=str.lower)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[sc-nav] fauna list load failed: {exc}")
        return []


def load_raw_commodity_names() -> list[str]:
    """Sorted names of raw (is_raw==1) commodities from uexcorp, used to
    populate the ore datalist. Fetched live with an on-disk cache fallback,
    mirroring the dataset loader."""
    rows = None
    if not OFFLINE:
        try:
            resp = _fetch_json(COMMODITIES_URL, timeout=15)
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows:
                _save_json_list(COMMODITIES_FILE, rows)
        except Exception as exc:
            print(f"[sc-nav] commodities fetch failed, using cache: {exc}")
    if not rows:
        rows = _load_json_list(COMMODITIES_FILE)
    names = {r["name"] for r in rows if r.get("is_raw") in (1, "1", True) and r.get("name")}
    return sorted(names)


def load_ships() -> list[dict]:
    """Cargo-capable spaceships from the uexcorp vehicles feed (name + stated
    SCU + company), for the cargo-planner ship picker. Fetched live with an
    on-disk cache fallback, mirroring the commodities loader. The full rows are
    cached (not just the trimmed view) so the deferred quantum-drive/range work
    can reuse fuel + capability fields without a second feed."""
    rows = None
    if not OFFLINE:
        try:
            resp = _fetch_json(SHIPS_URL, timeout=15)
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows:
                _save_json_list(SHIPS_FILE, rows)
        except Exception as exc:
            print(f"[sc-nav] ships fetch failed, using cache: {exc}")
    if not rows:
        rows = _load_json_list(SHIPS_FILE)

    def to_scu(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    ships = [
        # name_full includes the manufacturer ("Argo MOLE"), which is how players
        # search; fall back to the bare model name.
        {"name": r.get("name_full") or r["name"], "company": r.get("company_name"),
         "scu": to_scu(r.get("scu"))}
        for r in rows
        if (r.get("name_full") or r.get("name")) and r.get("is_spaceship") in (1, "1", True)
        and to_scu(r.get("scu")) > 0
    ]
    ships.sort(key=lambda s: s["name"].lower())
    return ships


def load_quantum() -> tuple[dict, dict, dict]:
    """Load the committed quantum artifacts distilled from the SC Wiki API by
    tools/sync_quantum.py (#26/#27). Returns (drives, profiles, uex_index):
    drives = {qd_class_name: {...}}; profiles = {slug: {fuel_scu, drives, ...}};
    uex_index = {uexcorp name_full: slug}. All empty (feature simply off) if the
    files are absent — the planners degrade to no fuel/range UI.

    These are **static, code-versioned reference data**, not a runtime cache, so
    the image-bundled copy next to the server code is authoritative and is tried
    FIRST. `DATA_DIR` is a fallback (that's where dev/CI and the sync script keep
    them). In production `DATA_DIR` is a named volume that shadows the image's
    baked `/data`, and Docker only seeds a volume on first creation — so a file
    added in a later release would never reach an existing volume. Loading from
    the code dir sidesteps that entirely and guarantees the data matches the
    deployed version. See the Dockerfile `COPY poi/quantum_*.json`."""
    def _read(base: Path, name: str) -> dict:
        try:
            return json.loads((base / name).read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    for base in (Path(__file__).parent, DATA_DIR):     # code-bundled wins over the volume
        prof_doc = _read(base, "quantum_profiles.json")
        if prof_doc.get("profiles"):
            drives = _read(base, "quantum_drives.json").get("drives", {})
            return drives, prof_doc["profiles"], prof_doc.get("uexcorp", {})
    return {}, {}, {}


def _ship_quantum_obj(profile: dict) -> dict:
    """The `quantum` sub-object attached to a /api/ships row: enough for the
    frontend drive picker + range readout, without leaking distill internals."""
    default = next((d for d in profile["drives"] if d["is_default"]),
                   profile["drives"][0] if profile["drives"] else None)
    return {
        "fuel_scu": profile["fuel_scu"],
        "qd_size": profile.get("qd_size"),
        "default_qd": profile.get("default_qd"),
        "default_range_m": default["range_m"] if default else None,
        "max_range_m": profile.get("max_range_m"),
        "default_from_synth": profile.get("default_from_synth", False),
        "drives": [{"qd": d["qd"], "name": d["name"], "fuel_req": d["fuel_req"],
                    "range_m": d["range_m"], "is_default": d["is_default"]}
                   for d in profile["drives"]],
    }


def enrich_ships_quantum(ship_rows: list[dict]) -> None:
    """Attach a `quantum` sub-object to each planner ship that matched a wiki
    profile (mutates in place). Unmatched ships get no `quantum` key — the UI hides
    the drive picker and range readout for them (no fabricated numbers, #27)."""
    for s in ship_rows:
        slug = QUANTUM_UEX.get(s["name"])
        prof = QUANTUM_PROFILES.get(slug) if slug else None
        if prof and prof.get("drives"):
            s["quantum"] = _ship_quantum_obj(prof)


def _resolve_drive(ship_name: str | None, qd_key: str | None):
    """(fuel_req, max_range_m, resolved_qd) for a ship + optional drive override.
    Falls back to the ship's stock drive when qd_key is unknown/None; returns
    (None, None, None) for an unmatched ship so the solvers skip fuel/range."""
    slug = QUANTUM_UEX.get(ship_name or "")
    prof = QUANTUM_PROFILES.get(slug) if slug else None
    if not prof or not prof.get("drives"):
        return (None, None, None)
    by_key = {d["qd"]: d for d in prof["drives"]}
    d = by_key.get(qd_key) or by_key.get(prof.get("default_qd")) or prof["drives"][0]
    return (d["fuel_req"], d["range_m"], d["qd"])


def load_blueprints() -> dict:
    """Load the committed crafting-blueprint feed distilled from the SC Wiki API
    by tools/sync_blueprints.py (#26, feeds the marketplace craft-commission
    mode #25). Returns {bp_key: record}; empty (commissions still post, no
    manifest/spec help) if the file is absent. Same code-bundled-first loading
    rationale as load_quantum — see that docstring + the Dockerfile COPY."""
    for base in (Path(__file__).parent, DATA_DIR):     # code-bundled wins over the volume
        try:
            doc = json.loads((base / "blueprints.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("blueprints"):
            return doc["blueprints"]
    return {}


def _blueprint_index_row(key: str, bp: dict) -> dict:
    """One search-index row for GET /api/blueprints — enough for the picker
    (name + disambiguating category + a materials summary), never the full
    modifier payload (that's the detail endpoint's job)."""
    ins = bp.get("aspects") or []
    return {
        "key": key,
        "name": bp.get("name"),
        "cat": bp.get("cat"),
        "time_s": bp.get("time_s"),
        "default": bp.get("default", False),
        "inputs": ", ".join(dict.fromkeys(a["input"] for a in ins if a.get("input"))),
        "has_mods": any(a.get("mods") for a in ins),
    }


def load_fleet_ships() -> list[dict]:
    """All spaceships (name + crew size + a default seat template) for the fleet
    organizer's ship picker — a superset of the cargo `ships` list (combat
    multicrew ships have no SCU but very much have crews). Reads the full
    vehicle rows that `load_ships` persists to `SHIPS_FILE`, so it needs no extra
    fetch; the seat template is derived by the pure `nav_core.ship_seat_template`."""
    rows = _load_json_list(SHIPS_FILE)
    flags = [f for f, _ in nav_core.SHIP_ROLE_FLAGS]
    out = []
    for r in rows:
        name = r.get("name_full") or r.get("name")
        if not name or r.get("is_spaceship") not in (1, "1", True):
            continue
        try:
            crew = int(float(r.get("crew") or 0))
        except (TypeError, ValueError):
            crew = 0
        crew = max(1, crew)
        traits = {f for f in flags if r.get(f) in (1, "1", True)}
        out.append({"name": name, "crew": crew,
                    "seats": nav_core.ship_seat_template(crew, traits)})
    out.sort(key=lambda s: s["name"].lower())
    return out


def load_commodity_names() -> list[str]:
    """All commodity names from uexcorp (every kind, not just is_raw ores) for
    the cargo-planner commodity picker — hauling contracts carry Medical
    Supplies, Processed Food, etc., not only raw ores. Same fetch + on-disk
    cache as the ore loader."""
    rows = None
    if not OFFLINE:
        try:
            resp = _fetch_json(COMMODITIES_URL, timeout=15)
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows:
                _save_json_list(COMMODITIES_FILE, rows)
        except Exception as exc:
            print(f"[sc-nav] commodities fetch failed, using cache: {exc}")
    if not rows:
        rows = _load_json_list(COMMODITIES_FILE)
    return sorted({r["name"] for r in rows if r.get("name")})


def load_item_names() -> list[str]:
    """Distinct equipment / ship-part names from the uexcorp items_prices_all feed
    (weapons, components, armor, attachments, …) for the shared item catalog, so
    the inventory/goals + marketplace apps can reference gear that isn't a bulk
    commodity or a vehicle. The feed lists one row per item *per terminal*, so the
    same item recurs many times — we keep the distinct `item_name` set. Fetched
    live with an on-disk cache fallback, mirroring the commodities loader."""
    rows = None
    if not OFFLINE:
        try:
            resp = _fetch_json(ITEMS_URL, timeout=30)
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows:
                _save_json_list(ITEMS_FILE, rows)
        except Exception as exc:
            print(f"[sc-nav] items fetch failed, using cache: {exc}")
    if not rows:
        rows = _load_json_list(ITEMS_FILE)
    return sorted({r["item_name"] for r in rows if r.get("item_name")})


def _price_map_from_rows(rows, name_key: str) -> dict:
    """Median buy/sell aUEC price per item name across a feed's rows — the items
    feed lists one row per terminal, so the same item recurs at many prices; the
    median is a robust central reference. Zero/blank prices are ignored. Returns
    `{name: {"buy": int|None, "sell": int|None}}`."""
    from statistics import median
    buys, sells = {}, {}
    for r in rows or []:
        nm = r.get(name_key)
        if not nm:
            continue
        b, s = r.get("price_buy"), r.get("price_sell")
        if b:
            buys.setdefault(nm, []).append(float(b))
        if s:
            sells.setdefault(nm, []).append(float(s))
    out = {}
    for nm in set(buys) | set(sells):
        out[nm] = {"buy": round(median(buys[nm])) if buys.get(nm) else None,
                   "sell": round(median(sells[nm])) if sells.get(nm) else None}
    return out


def load_item_prices() -> dict:
    """Reference buy/sell price per equipment item from the cached items feed (the
    name loader refreshed the cache, so this reads it without a second fetch). For
    the marketplace 'market value' anchor — aUEC only."""
    return _price_map_from_rows(_load_json_list(ITEMS_FILE), "item_name")


def load_commodity_prices() -> dict:
    """Reference buy/sell price per commodity from the cached commodities feed (one
    row per commodity, so no median needed — but routed through the same shaper)."""
    return _price_map_from_rows(_load_json_list(COMMODITIES_FILE), "name")


def build_item_prices() -> dict:
    """Catalog `item_id` → {buy, sell} aUEC reference, merged from the commodity and
    equipment feeds, for the marketplace's suggested 'market value'. Keyed by the
    same synthesized ids catalog.build uses, so a listing's item_id looks up here.
    (Ships are priced inconsistently in aUEC and are left without a hint.)"""
    out = {}
    for nm, p in load_commodity_prices().items():
        out[f"commodity:{catalog.slug(nm)}"] = p
    for nm, p in load_item_prices().items():
        out[f"item:{catalog.slug(nm)}"] = p
    return out


def load_harvestable_names() -> list[str]:
    """Sorted names of harvestable flora/natural commodities (uexcorp
    kind=="Natural" and is_harvestable==1) for the Add Fauna & Harvestables
    datalist. Reuses the same commodities cache as the ore loader."""
    rows = None
    if not OFFLINE:
        try:
            resp = _fetch_json(COMMODITIES_URL, timeout=15)
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows:
                _save_json_list(COMMODITIES_FILE, rows)
        except Exception as exc:
            print(f"[sc-nav] commodities fetch failed, using cache: {exc}")
    if not rows:
        rows = _load_json_list(COMMODITIES_FILE)
    names = {
        r["name"] for r in rows
        if r.get("kind") == "Natural"
        and r.get("is_harvestable") in (1, "1", True)
        and r.get("name")
    }
    return sorted(names)


def load_trade_terminals() -> list[dict]:
    """UEX commodity terminals (one row per terminal), for the trade-route planner
    (#21). Filtered to live, commodity-type terminals — the only ones that buy/sell
    bulk cargo. Fetched live with an on-disk cache fallback, same as the other feeds.
    Placement onto the map happens later, in nav_core.match_terminals."""
    rows = None
    if not OFFLINE:
        try:
            resp = _fetch_json(TERMINALS_URL, timeout=30)
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows:
                _save_json_list(TERMINALS_FILE, rows)
        except Exception as exc:
            print(f"[sc-nav] terminals fetch failed, using cache: {exc}")
    if not rows:
        rows = _load_json_list(TERMINALS_FILE)
    return [r for r in rows
            if r.get("type") == "commodity" and r.get("is_available_live")]


def load_trade_prices() -> list[dict]:
    """UEX per-terminal commodity prices (commodities_prices_all — one row per
    commodity per terminal), the live buy/sell feed the trade planner ranks over.
    Each row joins to a terminal via `id_terminal` and to a commodity by
    `commodity_name`. Cached to disk like the rest; large (~2.5k rows)."""
    rows = None
    if not OFFLINE:
        try:
            resp = _fetch_json(TRADE_PRICES_URL, timeout=40)
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows:
                _save_json_list(TRADE_PRICES_FILE, rows)
        except Exception as exc:
            print(f"[sc-nav] trade prices fetch failed, using cache: {exc}")
    if not rows:
        rows = _load_json_list(TRADE_PRICES_FILE)
    return rows or []


class HandleRegistry:
    """Maps in-game handles to stable assigned PlayerIDs (DB-backed, cached
    in memory).

    The PlayerID (not the raw handle) is the key attached to contributions, so
    a character rename keeps a player's history intact."""

    def __init__(self):
        self.by_handle = {h["handle"]: h for h in db.all_handles()}

    def register(self, handle: str, discord_id: str | None = None) -> dict:
        handle = handle.strip()
        now = datetime.now(timezone.utc).isoformat()
        entry = self.by_handle.get(handle)
        if entry is None:
            next_id = max((e["player_id"] for e in self.by_handle.values()), default=0) + 1
            entry = {"player_id": next_id, "handle": handle, "first_seen": now,
                     "last_seen": now, "discord_id": discord_id}
            self.by_handle[handle] = entry
            # Persist only when a genuinely new handle appears — this runs on the
            # position hot path (every /showlocation), so we don't write per
            # sample just to bump last_seen (kept in memory).
            try:
                db.upsert_handle(entry)
            except Exception as exc:
                print(f"[sc-nav] handle registry save failed: {exc}")
        else:
            entry["last_seen"] = now  # in-memory only; not worth a write per position
            # Trust-on-first-use ownership: bind the handle the first time we learn
            # who is posting it (the watcher's token resolves to a Discord id), but
            # NEVER transfer a handle already bound to a *different* member. Without
            # this guard any authenticated caller could POST /api/position with
            # someone else's handle and steal ownership of their contributions and
            # verified marketplace identity (a handle is client-supplied free text).
            if discord_id and entry.get("discord_id") is None:
                entry["discord_id"] = discord_id
                try:
                    db.upsert_handle(entry)
                except Exception as exc:
                    print(f"[sc-nav] handle owner bind failed: {exc}")
        return entry

    def player_ids_for(self, discord_id: str) -> set[int]:
        """Every PlayerID owned by a Discord member (alts/renames included).
        Used to scope deletes to a member's own contributions."""
        return {e["player_id"] for e in self.by_handle.values()
                if e.get("discord_id") == discord_id}

    def handles_for(self, discord_id: str) -> list[str]:
        """The in-game handles a Discord member has had a watcher bind to them,
        most-recently-seen first. These are the *verified* handles — the picker's
        options and the set a marketplace handle is checked against."""
        owned = [e for e in self.by_handle.values()
                 if e.get("discord_id") == discord_id]
        owned.sort(key=lambda e: e.get("last_seen") or "", reverse=True)
        return [e["handle"] for e in owned]

    def owns_handle(self, discord_id: str, handle: str) -> bool:
        """Whether `handle` is currently bound to this Discord member (the live
        verification check behind a listing's `handle_verified`)."""
        if not handle:
            return False
        return any(e["handle"] == handle and e.get("discord_id") == discord_id
                   for e in self.by_handle.values())

    def handle_for(self, player_id: int) -> str | None:
        """Current handle for a PlayerID (latest known after any rename)."""
        for e in self.by_handle.values():
            if e["player_id"] == player_id:
                return e["handle"]
        return None

    def list(self) -> list[dict]:
        return sorted(self.by_handle.values(), key=lambda e: e["handle"].lower())


class MemberDirectory:
    """Persisted Discord identities (DB-backed, cached in memory), keyed by
    discord_id. Backs display-name resolution everywhere a member is shown and the
    admin directory. Loaded at boot; kept current on login (upsert) and on the
    member's own primary-handle / opt-out edits."""

    def __init__(self):
        self.by_id = {m["discord_id"]: m for m in db.all_members()}

    def upsert(self, profile: dict) -> None:
        rec = {"now": datetime.now(timezone.utc).isoformat(), **profile}
        db.upsert_member(rec)
        did = str(profile["id"])
        cur = self.by_id.get(did, {"discord_id": did})
        cur.update({"username": profile.get("username"),
                    "display_name": profile.get("display_name"),
                    "guild_nick": profile.get("guild_nick"),
                    "last_login": rec["now"]})
        cur.setdefault("first_login", rec["now"])
        cur.setdefault("primary_handle", None)
        cur.setdefault("directory_opt_out", 0)
        self.by_id[did] = cur

    def get(self, discord_id: str) -> dict | None:
        return self.by_id.get(str(discord_id))

    def set_primary_handle(self, discord_id: str, handle: str | None) -> None:
        did = str(discord_id)
        db.set_primary_handle(did, handle)
        self.by_id.setdefault(did, {"discord_id": did})["primary_handle"] = handle

    def set_opt_out(self, discord_id: str, opt_out: bool) -> None:
        did = str(discord_id)
        db.set_directory_opt_out(did, opt_out)
        self.by_id.setdefault(did, {"discord_id": did})["directory_opt_out"] = 1 if opt_out else 0

    def set_playstyles(self, discord_id: str, tags: list[str]) -> None:
        # Cache the raw column value (JSON or None) so reads parse uniformly
        # whether the row came from the DB load or this setter.
        did = str(discord_id)
        db.set_member_playstyles(did, tags)
        self.by_id.setdefault(did, {"discord_id": did})["playstyle_tags"] = \
            json.dumps(tags) if tags else None

    def forget(self, discord_id: str) -> None:
        self.by_id.pop(str(discord_id), None)

    def display_name(self, discord_id: str) -> str | None:
        """The member's preferred display label: org nickname, then Discord
        display name. None if we've never persisted this member (caller falls
        back to a handle or id stub)."""
        m = self.by_id.get(str(discord_id))
        if not m:
            return None
        return m.get("guild_nick") or m.get("display_name")


class TokenStore:
    """Per-user watcher tokens (DB-backed, cached in memory). The headless
    watcher can't do OAuth, so an org member mints a token in the web UI and the
    watcher sends it as a bearer token. Only the hash is persisted; admin status
    is resolved live."""

    def __init__(self):
        self.items = db.all_tokens()

    @staticmethod
    def _hash(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _public(t: dict) -> dict:
        return {"id": t["id"], "label": t["label"],
                "created": t["created"], "last_used": t["last_used"]}

    def resolve(self, raw: str) -> dict | None:
        """A raw bearer token -> the owning member (id/display_name/is_admin),
        or None. last_used is bumped in memory only (avoids a write per position
        on the watcher heartbeat)."""
        h = self._hash(raw)
        for t in self.items:
            if secrets.compare_digest(t["hash"], h):
                t["last_used"] = datetime.now(timezone.utc).isoformat()
                return {
                    "id": t["discord_id"],
                    "display_name": t.get("display_name"),
                    "is_admin": t["discord_id"] in admin_ids(),
                }
        return None

    def mint(self, discord_id: str, display_name: str, label: str) -> tuple[str, dict]:
        raw = secrets.token_urlsafe(32)
        entry = {
            "id": secrets.token_hex(8),
            "hash": self._hash(raw),
            "discord_id": discord_id,
            "display_name": display_name,
            "label": (label or "watcher").strip()[:60],
            "created": datetime.now(timezone.utc).isoformat(),
            "last_used": None,
        }
        db.add_token(entry)
        self.items.append(entry)
        return raw, self._public(entry)

    def list_for(self, discord_id: str) -> list[dict]:
        return [self._public(t) for t in self.items if t["discord_id"] == discord_id]

    def revoke(self, token_id: str, discord_id: str, is_admin: bool) -> bool:
        for i, t in enumerate(self.items):
            if t["id"] == token_id and (is_admin or t["discord_id"] == discord_id):
                db.delete_token(token_id)
                self.items.pop(i)
                return True
        return False


def merge_all_observations(target_nav) -> None:
    nav_core.merge_observations(target_nav, db.list_observations())


app = FastAPI(title="SC Nav")


# Auth gate: every /api/* call needs a logged-in org member (session) or a valid
# watcher token; the SPA shell, /auth/* and /api/health stay open. Registered
# BEFORE SessionMiddleware below so that (being the inner layer) it runs after
# the session has been loaded from the cookie.
@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    # The org logo + name are shown on the pre-auth login splash, so reading them
    # is public (GET only — POST/DELETE still fall through to the admin-gated
    # route). /api/branding carries the public name; the logo is its own image
    # route.
    if (not path.startswith("/api/") or path == "/api/health"
            or (path == "/api/branding" and request.method == "GET")
            or (path == "/api/org-logo" and request.method == "GET")):
        return await call_next(request)
    if request.session.get("user") or token_user(request):
        return await call_next(request)
    return JSONResponse({"detail": "not authenticated"}, status_code=401)


# Signed session cookie (Discord login state). The secret must be stable across
# restarts so sessions survive a redeploy; a random fallback keeps dev working.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
_SESSION_SECRET = os.environ.get("SESSION_SECRET")
if not _SESSION_SECRET:
    # A per-process random key invalidates every session on each restart and
    # can't verify cookies signed by a sibling worker — a misconfiguration, not
    # a safe default. Warn loudly (prod runs with COOKIE_SECURE) so it's caught.
    _SESSION_SECRET = secrets.token_hex(32)
    print("[sc-nav] WARNING: SESSION_SECRET unset — using a per-process random "
          "key; sessions will not survive a restart or span multiple workers. "
          "Set SESSION_SECRET in the deployment environment.")
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    https_only=COOKIE_SECURE,
    same_site="lax",
    max_age=8 * 3600,
)

# Compress text responses (the ~700 KB SPA shell, POI catalog, boards). The shell
# is served no-store for the CSP nonce, so gzip is the only transfer-size lever;
# behind the Cloudflare tunnel this also shrinks origin→edge traffic. Only bodies
# ≥1 KB are touched, so tiny JSON/error responses are left alone.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Defense-in-depth response headers on every response (static + API). The SPA
# ships one inline <script>, authorized by a per-request nonce so script-src can
# drop 'unsafe-inline' entirely — an injected <script> (or img onerror, etc.)
# carries no valid nonce and won't execute, so a future escaping slip can't
# become script execution. Inline STYLE attributes are pervasive (style="width:
# ..%") and nonces don't cover them, so style-src keeps 'unsafe-inline' (style
# injection is far lower risk). No 'unsafe-eval', no external script/object
# sources, framing denied (clickjacking). Output-escaping is still the primary
# XSS defense; the nonce makes the CSP a real backstop rather than a formality.
def _csp(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Fresh, unguessable nonce per request; the index route reads it back off
    # request.state to stamp the inline <script> with a matching nonce.
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _csp(nonce))
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


db.init(DB_FILE)
db.import_legacy_json(DATA_DIR, nav_core.OBSERVATION_CATEGORIES)  # one-time JSON -> SQLite

nav = load_nav_data()
handles = HandleRegistry()
tokens = TokenStore()
members_dir = MemberDirectory()
raw_commodity_names = load_raw_commodity_names()
commodity_names = load_commodity_names()
harvestable_names = load_harvestable_names()
ships = load_ships()
QUANTUM_DRIVES, QUANTUM_PROFILES, QUANTUM_UEX = load_quantum()
enrich_ships_quantum(ships)     # attach per-ship quantum fuel/range (#27), when matched
fleet_ships = load_fleet_ships()
blueprints_feed = load_blueprints()     # crafting recipes for commissions (#25)
item_names = load_item_names()
item_prices = build_item_prices()
fauna_names = load_fauna_names()
biomes = load_biomes()


def rebuild_catalog() -> list[dict]:
    """The merged item catalog (commodity + ship + equipment feeds + custom rows),
    shared by the inventory/goals and marketplace apps. Rebuilt at startup and
    whenever a custom item is added or the feeds are refreshed. Each item carries an
    optional `price` reference (aUEC buy/sell from the feeds) for the marketplace's
    suggested market value."""
    return catalog.build(commodity_names, ships, db.list_catalog_items(), item_names,
                         prices=item_prices)


item_catalog = rebuild_catalog()
item_catalog_by_id = {it["item_id"]: it for it in item_catalog}


def resolve_catalog_item(item_id: str) -> dict | None:
    """A catalog id → its canonical item ({item_id, name, kind, unit}), or None.
    Endpoints resolve the client's chosen id here to stamp the authoritative
    name/unit onto a stored row, rather than trusting a client-sent name.

    `blueprint:<bp_key>` ids resolve against the blueprint feed instead of the
    merged catalog — craftable items stay out of the inventory/goals pickers
    (#25) but gain first-class identity on marketplace listings."""
    if item_id.startswith("blueprint:"):
        bp = blueprints_feed.get(item_id[len("blueprint:"):])
        if bp is None:
            return None
        return {"item_id": item_id, "name": bp.get("name"),
                "kind": "blueprint", "unit": "each"}
    return item_catalog_by_id.get(item_id)


def refresh_catalog() -> None:
    """Rebuild the in-memory catalog + its id index after the custom rows or feeds
    change. Cheap (a few hundred items); called from the custom-item + refresh
    endpoints."""
    global item_catalog, item_catalog_by_id
    item_catalog = rebuild_catalog()
    item_catalog_by_id = {it["item_id"]: it for it in item_catalog}
nav_core.merge_custom_pois(nav, db.list_custom_pois())
merge_all_observations(nav)
nav_core.assign_qt_markers(nav)


# --- trade-route planner feeds (#21) ---------------------------------------
trade_terminals_raw = load_trade_terminals()   # cached UEX commodity terminals
trade_prices = load_trade_prices()             # cached per-terminal buy/sell rows
trade_terminals: list[dict] = []               # resolved onto routable POIs
trade_terminals_by_id: dict[int, dict] = {}
trade_price_points: list[dict] = []            # live prices joined to resolved terminals


def _serialize_trade_price(row: dict, term: dict) -> dict:
    """One live price row joined to its routable terminal. `buy` = what the
    terminal sells to you (you buy there); `sell` = what it pays you (you sell
    there) — UEX's price_buy/price_sell are from the player's side. Supply/demand
    stock + status flags pass through from UEX; `updated_at` is the scrape time
    (unix s) for the 'as of Xh ago' freshness label (advisory, no age-off)."""
    return {
        "commodity": row.get("commodity_name"),
        "terminal_id": term["id"],
        "terminal": term["name"],
        "system": term["system"],
        "poi_id": term["poi_id"],
        "buy": row.get("price_buy") or None,
        "sell": row.get("price_sell") or None,
        "scu_buy": row.get("scu_buy") or 0,
        "scu_sell_stock": row.get("scu_sell_stock") or 0,
        "status_buy": row.get("status_buy") or 0,
        "status_sell": row.get("status_sell") or 0,
        "updated_at": row.get("date_modified") or row.get("date_added"),
    }


def rebuild_trade_terminals() -> None:
    """Resolve the cached UEX commodity terminals onto routable nav POIs via the
    name-match crosswalk, then join the price feed onto those terminals. Run at
    startup and after /api/refresh — the match depends on the live nav POI catalog.
    Terminals that don't resolve are dropped from routing (never mis-placed) and
    their count logged; price rows at an unresolved terminal are likewise dropped."""
    global trade_terminals, trade_terminals_by_id, trade_price_points
    resolved, unmatched = nav_core.match_terminals(nav, trade_terminals_raw)
    trade_terminals = resolved
    trade_terminals_by_id = {t["id"]: t for t in resolved}
    trade_price_points = [
        _serialize_trade_price(row, trade_terminals_by_id[tid])
        for row in trade_prices
        if (tid := row.get("id_terminal")) in trade_terminals_by_id
    ]
    if trade_terminals_raw:
        print(f"[sc-nav] trade terminals resolved {len(resolved)}/"
              f"{len(trade_terminals_raw)} ({len(unmatched)} unplaced); "
              f"{len(trade_price_points)} routable price rows")


rebuild_trade_terminals()


# --- auth dependencies (defined before the endpoints that use them) ---------
def current_user(request: Request) -> dict | None:
    """The signed-in member. `is_admin` is recomputed against the live admin set
    (not read from the login-time session value) so a UI grant/revoke applies on
    the member's next request rather than only at their next sign-in."""
    user = request.session.get("user")
    if user is None:
        return None
    return {**user, "is_admin": user["id"] in admin_ids()}


def token_user(request: Request) -> dict | None:
    """The org member behind a `Authorization: Bearer <watcher token>`, or None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return tokens.resolve(header[7:].strip())


def require_session(request: Request) -> dict:
    """Dependency: a logged-in org member, else 401."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def require_admin(user: dict = Depends(require_session)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    return user


def ensure_owns(user: dict, owner_id: int | None) -> None:
    """A member may delete only their own contributions — any PlayerID bound to
    their Discord id (alts/renames included). Admins delete anything.
    Ownerless legacy records (owner_id is None) are admin-only."""
    if user.get("is_admin"):
        return
    if owner_id is not None and owner_id in handles.player_ids_for(user["id"]):
        return
    raise HTTPException(status_code=403, detail="you can only delete your own contributions")


def require_user(request: Request) -> dict:
    """A logged-in member (browser session) OR a watcher token — used where
    either client is valid (e.g. posting a position)."""
    user = current_user(request) or token_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def viewer_owner_ids(user: dict | None) -> frozenset[int]:
    """The PlayerIDs a viewer owns (alts/renames included) — the key that lets
    them see their own private POIs. Empty for an anonymous viewer, so they see
    only shared POIs."""
    if not user:
        return frozenset()
    return frozenset(handles.player_ids_for(user["id"]))


# Field length caps. User-supplied free text is bounded at the schema edge so a
# member (or a watcher token) can't persist multi-MB strings that then get
# fanned out to every connected tab over the WebSocket. Generous vs. real use,
# tight vs. abuse.
_NAME_MAX = 120
_TYPE_MAX = 60
_NOTE_MAX = 500
_TERM_MAX = 80     # ore / species / harvestable name
_BIOME_MAX = 60
_HANDLE_MAX = 64
_BAND_MAX = 16
_RAW_MAX = 512
_META_MAX = 64     # client_time / source / discord-id-ish small fields
_SHARD_MAX = 64    # SC shard id, e.g. "pub_use1b_12030094_130"
_LABEL_MAX = 60
_COMMODITY_MAX = 80
_PKG_ID_MAX = 64
_CONTRACT_MAX = 60   # player's free-text contract/group label
_MAX_PACKAGES = 60   # one hauling run rarely exceeds a handful of contracts
_DESC_MAX = 2000     # event description free-text
_MAX_ROSTER_ROLES = 20   # target roles on one event
_MAX_SIGNUP_ROLES = 10   # roles one member claims on a signup
_MAX_PLAYERS = 10_000    # sanity cap on min/max player counts
_ITEM_ID_MAX = 120       # catalog id, e.g. "commodity:medical-supplies"
_UNIT_MAX = 16           # SCU / each / …
_LOCATION_MAX = 120      # free-text holding location
_MAX_QTY = 1e12          # sanity cap on a single inventory/goal quantity
_MAX_LINE_ITEMS = 50     # line items on one goal
# How far back the upcoming board reaches to keep live/ongoing events visible
# (their start has passed but they aren't finished); finished ones are then filtered
# out by derived phase. Generous enough to cover most op lengths + the live grace.
_EVENT_BOARD_LOOKBACK_MIN = 24 * 60


class PositionIn(BaseModel):
    x: float
    y: float
    z: float
    raw: str | None = Field(default=None, max_length=_RAW_MAX)
    client_time: str | None = Field(default=None, max_length=_META_MAX)
    source: str | None = Field(default=None, max_length=_META_MAX)
    handle: str | None = Field(default=None, max_length=_HANDLE_MAX)
    # SC shard id from Game.log (watcher). Stamped onto captures and broadcast so
    # clients can tell which ephemeral nodes / teammates are on their own server.
    shard: str | None = Field(default=None, max_length=_SHARD_MAX)


class DestinationIn(BaseModel):
    poi_id: int


class CaptureIn(BaseModel):
    name: str = Field(max_length=_NAME_MAX)
    type: str = Field(default="Custom", max_length=_TYPE_MAX)
    qt_marker: bool = False   # record as a jumpable QT marker (e.g. an OM)
    private: bool = False     # owner-only POI; hidden from the rest of the org
    note: str = Field(default="", max_length=_NOTE_MAX)   # optional free-text context


class NodeCaptureIn(BaseModel):
    ore: str = Field(max_length=_TERM_MAX)
    band: int | str | None = None   # 1-8, or "Unk"/None; str length checked in the handler

    biome: str | None = Field(default=None, max_length=_BIOME_MAX)
    note: str | None = Field(default=None, max_length=_NOTE_MAX)


class WildlifeCaptureIn(BaseModel):
    species: str = Field(max_length=_TERM_MAX)
    biome: str | None = Field(default=None, max_length=_BIOME_MAX)
    note: str | None = Field(default=None, max_length=_NOTE_MAX)


class HarvestableCaptureIn(BaseModel):
    name: str = Field(max_length=_TERM_MAX)
    biome: str | None = Field(default=None, max_length=_BIOME_MAX)
    note: str | None = Field(default=None, max_length=_NOTE_MAX)


class PackageIn(BaseModel):
    """One cargo line: pick up `scu` of `commodity` at `from_id`, deliver to
    `to_id`. from->to encodes pickup-before-dropoff precedence.

    Multi-pickup delivery (rare): when a contract gives one commodity total but
    spreads the cargo over several pickup locations without saying how much is at
    each, the rows share a `group` id and carry the delivery total in `group_scu`
    (per-row `scu` is then unused/0). The solver counts that total once, holds it
    conservatively from the group's first pickup to its drop, and requires every
    listed pickup to precede the dropoff. Normal rows leave `group` None."""
    id: str | None = Field(default=None, max_length=_PKG_ID_MAX)
    commodity: str | None = Field(default=None, max_length=_COMMODITY_MAX)
    scu: float = Field(ge=0, le=100_000)
    from_id: int
    to_id: int
    contract: str | None = Field(default=None, max_length=_CONTRACT_MAX)  # display-only group label
    group: str | None = Field(default=None, max_length=_PKG_ID_MAX)       # multi-pickup binding id
    group_scu: float | None = Field(default=None, ge=0, le=100_000)       # delivery total for the group


_MAX_REWARD = 1e12   # generous aUEC ceiling — a sanity cap, not a real limit


class RoutePlanIn(BaseModel):
    packages: list[PackageIn] = Field(max_length=_MAX_PACKAGES)
    usable_scu: float = Field(gt=0, le=100_000)
    start_id: int | None = None    # POI to start from
    start_here: bool = False       # start from the caller's live show_location fix
    # Precedence: start_here (live position) > start_id (chosen POI) > free start.
    # Per-contract payout keyed by the package `contract` label ("" = the
    # ungrouped bucket). Display-only/advisory — never affects routing; the run's
    # total payout is the sum, the denominator for aUEC/hour.
    rewards: dict[str, float] = Field(default_factory=dict, max_length=_MAX_PACKAGES)

    @field_validator("rewards")
    @classmethod
    def _cap_reward_keys(cls, v: dict) -> dict:
        # Keys are contract labels; without a length cap a client could ship a few
        # multi-MB key strings and balloon memory before the count check runs.
        if any(len(k) > _CONTRACT_MAX for k in v):
            raise ValueError(f"reward label too long (max {_CONTRACT_MAX} chars)")
        return v
    # Pirate danger board (#24 v2): snare-detour routing. `avoid` (the default) —
    # route around dangers, stops never change; `warn` — flag legs that touch a
    # danger; `ignore` — plan as-is. `avoid_poi_ids` is the caller's personal
    # blacklist (localStorage), places to always route around; unknown ids are
    # silently skipped (POIs can be refreshed away).
    avoid_mode: str = "avoid"
    avoid_poi_ids: list[int] = Field(default_factory=list, max_length=50)
    # Quantum fuel & range (#27). `ship` (uexcorp name_full) + optional `qd` (drive
    # class_name override) resolve the drive; `in_range_only` turns the advisory
    # over-range warning into a hard solver constraint. All optional — an unmatched
    # ship or absent `ship` simply yields no fuel/range figures.
    ship: str | None = Field(default=None, max_length=_NAME_MAX)
    qd: str | None = Field(default=None, max_length=_NAME_MAX)
    in_range_only: bool = False


# Breadcrumb trail tuning. In-memory and session-scoped (lost on restart).
PATH_MIN_MOVE_M = 250.0   # don't record a crumb until you've moved this far
PATH_MAX = 5000           # cap so a long session can't grow unbounded
SHARED_PATH_MAX = 500     # crumbs shared with teammates per presence upsert (payload cap)

# Cargo-run arrival thresholds. Generous on purpose: arrival only surfaces the
# stop's package checklist for the player to confirm — it never auto-completes —
# so erring toward "you're here" is safe and helpful.
ARRIVAL_SURFACE_M = 5_000.0    # on the destination's own body (surface guidance)
ARRIVAL_SPACE_M = 50_000.0     # everything else (station / space approach)
# When the destination carries a wiki QT arrival radius (#28b), the space
# threshold becomes that radius ×1.5 — QT drops the ship *at* the radius, so
# the margin keeps drop-out counting as arrived — floored so the tiny radii in
# the catalog (asteroid clusters: 100 m) stay forgiving.
ARRIVAL_RADIUS_FACTOR = 1.5
ARRIVAL_RADIUS_FLOOR_M = 10_000.0

# Live presence tuning.
# A single stuck/backpressured WS client would otherwise block a serial broadcast
# indefinitely — and since some broadcasts run under hub.lock, that would freeze
# every lock-taking endpoint (a single-client global stall). Bound every send.
WS_SEND_TIMEOUT_S = 5.0
WS_CLOSE_TIMEOUT_S = 2.0

# Cap tabs per member so a reconnect storm can't grow ws_clients (and the O(tabs)
# broadcast fan-out) without bound.
WS_MAX_CLIENTS_PER_MEMBER = 8


async def _ws_send(ws, text: str) -> bool:
    """Send `text` on `ws` with a hard timeout. Returns False if the socket errored
    or timed out (a slow reader hitting TCP backpressure) and should be dropped.

    On failure we also close the socket (best-effort, bounded) so the client's
    onclose fires and it reconnects — which re-registers it in ws_clients. Without
    the close, a slow-but-alive client that recovers after one timed-out send would
    be silently dropped from broadcasts and go deaf until a manual reload (its ~20s
    ping keeps the socket open but never re-adds it here)."""
    try:
        await asyncio.wait_for(ws.send_text(text), timeout=WS_SEND_TIMEOUT_S)
        return True
    except Exception:
        try:
            await asyncio.wait_for(ws.close(), timeout=WS_CLOSE_TIMEOUT_S)
        except Exception:
            pass
        return False


PRESENCE_TICK_S = 1.0     # broadcaster cadence (coalesced upserts, ~1 Hz)
# Positions arrive only when a player manually runs /showlocation (the watcher has
# no position heartbeat), so a fix can be many minutes old yet still the best known
# spot. Keep a teammate on the map for a long window and let the client fade the
# marker as it ages (see MATE_STALE_S), rather than dropping them after a couple of
# minutes of not re-copying their coords.
PRESENCE_STALE_S = 1800.0  # drop a teammate only after this long with no new position
PRESENCE_MOVE_M = 5.0     # only recompute heading once actually moving

# Online roster (who's-online layer, backlog #19). Identity-bearing and NOT
# surface-gated — a member is "online" the moment a tab connects, wherever they
# are. Records are refreshed by the client's ~20s WS ping; this stale window is a
# backstop for a half-open socket that never fired a clean disconnect.
ONLINE_STALE_S = 90.0
# Manual availability states a member can set (#19 step 2); anything else falls
# back to "available". Ordering (available→busy→afk) lives in Hub._ONLINE_ORDER.
ONLINE_STATUSES = ("available", "busy", "afk")
_ACTIVITY_MAX = 60   # free-text "what I'm up to" on the online roster
# Shared playstyle / activity vocabulary — the one list reused as online-status
# activity quick-picks (step 2) and LFG entry tags (step 3). Served at
# /api/playstyles so both surfaces draw from the same source of truth.
PLAYSTYLE_TAGS = [
    "hauling", "mining", "salvage", "trading", "bunkers", "bounty",
    "PvE", "PvP", "FPS", "flight", "exploration", "medical/rescue",
    "RP", "casual", "serious", "new-player-friendly",
]
# Member profile (#30): persistent declared playstyles, capped — a profile is a
# signature, not a checklist (LFG posts cap similarly).
_PROFILE_MAX_TAGS = 6


def member_playstyles(member: dict | None) -> list[str]:
    """A member's declared profile tags (#30), parsed from the stored JSON and
    re-filtered against the live vocabulary so trimming PLAYSTYLE_TAGS can never
    resurface a retired tag."""
    raw = (member or {}).get("playstyle_tags")
    if not raw:
        return []
    try:
        vals = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [t for t in vals if isinstance(t, str) and t in PLAYSTYLE_TAGS][:_PROFILE_MAX_TAGS]

# Looking-for-group entries (#19 step 3): transient, in-memory, matching-oriented
# — NOT the scheduled `events` table. Two directions: "lfm" (I'm hosting / starting
# a group, need players — carries slots) and "lfj" (I'm solo, want in — a raised
# hand). Persisted (survives restarts) with a purely time-based lifecycle:
# fresh (green) → stale (yellow) → aged off, driven by the org's lfg_ageoff_min /
# lfg_stale_min settings off each post's `created`. One active LFM + one active
# LFJ per member (re-posting supersedes). Long-lived plans belong in Events.
LFG_DIRECTIONS = ("lfm", "lfj")
_LFG_NOTE_MAX = 280
_LFG_MAX_SLOTS = 40           # LFM slots-needed upper bound
_LFG_MAX_TAGS = 6             # playstyle chips per entry
# "Announce to Discord" (#19 step 4) is opt-in per post and rate-limited per member
# so one person can't blast the channel. Arming the cooldown is what gates it.
LFG_ANNOUNCE_COOLDOWN_S = 600.0
_lfg_announce_at: dict[str, float] = {}   # poster id -> last announce (monotonic)

# Pirate danger warnings (#24). A community-refreshable, time-bound danger board;
# a warning is a `point` (around one POI) or a `lane` (a snare between two anchor
# POIs), tagged pvp/pve at a severity. Same green→stale→age-off lifecycle as LFG
# (off `created`), same opt-in per-member rate-limited Discord announce.
WARNING_KINDS = ("point", "lane")
WARNING_THREATS = ("pvp", "pve")
WARNING_SEVERITIES = ("sighted", "active", "deadly")
_WARNING_NOTE_MAX = 280
_WARNING_LOCATION_MAX = 120
WARNING_ANNOUNCE_COOLDOWN_S = 600.0
WARNINGS_MAX_PER_MEMBER = 12   # flood guard: distinct active dangers one member may hold
_warning_announce_at: dict[str, float] = {}   # poster id -> last announce (monotonic)

# Craft-request announce shouts (#25) share the same per-member cooldown shape.
COMMISSION_ANNOUNCE_COOLDOWN_S = 600.0
_commission_announce_at: dict[str, float] = {}   # poster id -> last announce (monotonic)

# How often the scheduled event-reminder loop scans for due events.
REMINDER_TICK_S = 60.0


class Session:
    """One org member's live state: position cursor, destination, capture
    arming, breadcrumb trail, and their open browser tabs. Keyed by Discord id
    so each member gets an independent course while sharing the dataset."""

    def __init__(self, user: dict):
        self.user = user           # {"id","display_name","is_admin"}
        self.pos = None
        self.t = None
        # Last container-confirmed system. Deep space is ambiguous from raw
        # coordinates (every system centers on its own (0,0,0) in one shared
        # numeric space), but you can't change systems without transiting a
        # jump gate — always near detectable containers — so this sticks.
        self.system = None
        self.prev_pos = None
        self.prev_t = None
        self.destination_id = None
        self.run = None            # active cargo-planner run blob (or None)
        self.trade_run = None      # active trade-route run blob (or None)
        self.nav_state = None
        # capture_pending: {"kind": "poi"} or
        # {"kind": "observation", "category", "data", "biome", "note"} while armed
        self.capture_pending = None
        self.last_capture = None      # summary of this member's most recent capture
        self.owner = None             # {"player_id","handle"} from latest position
        self.shard = None             # current SC shard id (from the watcher's Game.log)
        self.tracking = False
        self.path = []                # crumbs: {lat, lon, container}
        # Live teammate presence: on by default, one-way opt-out (hide yourself
        # but keep seeing others). In-memory + per-session (resets to share-on
        # on restart, matching the "share by default" decision).
        self.share_presence = True
        self.ws_clients: set[WebSocket] = set()

    def capture_status(self):
        return {
            "armed": self.capture_pending is not None,
            "pending": self.capture_pending,
            "last": self.last_capture,
            "owner": self.owner,
        }

    def recompute(self):
        if self.pos is None:
            self.nav_state = None
            return
        self._recompute_state()
        # Sticky system: learn it when a container confirms it; backfill the
        # deep-space view (compute_state reports None there) so clients — the
        # navigator's halo chip, the halo locate loop — know where we are.
        if self.nav_state.get("system"):
            self.system = self.nav_state["system"]
        elif self.system:
            self.nav_state["system"] = self.system

    def _recompute_state(self):
        self.nav_state = nav_core.compute_state(
            nav, self.pos, self.t,
            destination_id=self.destination_id,
            prev_pos=self.prev_pos, prev_t=self.prev_t,
            viewer_owner_ids=viewer_owner_ids(self.user),
        )
        # The client's own shard rides on the state so it can flag which
        # observations / teammates share its server.
        self.nav_state["shard"] = self.shard
        self._attach_breadcrumbs()
        self.nav_state["run"] = self.run_view()
        self.nav_state["trade_run"] = self.trade_run_view()

    def _arrived_at_active(self) -> bool:
        """Whether the guidance distance to the active stop is within the
        arrival threshold (surface vs. space picked from the live readout).
        A destination with a wiki QT arrival radius (#28b) gets a threshold
        tailored to it instead of the flat space constant."""
        dest = self.nav_state.get("destination") if self.nav_state else None
        if not dest:
            return False
        surf = dest.get("surface_distance_m")
        if surf is not None:
            return surf < ARRIVAL_SURFACE_M
        d = dest.get("distance_m")
        thr = ARRIVAL_SPACE_M
        if dest.get("arrival_radius_m"):
            thr = max(dest["arrival_radius_m"] * ARRIVAL_RADIUS_FACTOR,
                      ARRIVAL_RADIUS_FLOOR_M)
        return d is not None and d < thr

    def onboard_scu(self) -> float:
        """Live cargo aboard = sum of SCU for packages currently 'onboard'. A
        multi-pickup group's full total counts once while any of its pickups is
        aboard (conservative, matching the planner's capacity model)."""
        if not self.run:
            return 0.0
        total = 0.0
        group_aboard = {}     # gid -> group_scu, counted if any pickup is onboard
        for p in self.run["packages"].values():
            g = p.get("group")
            if g is None:
                if p["state"] == "onboard":
                    total += p["scu"]
            elif p["state"] == "onboard":
                group_aboard[g] = float(p.get("group_scu") or 0)
        return total + sum(group_aboard.values())

    def run_view(self) -> dict | None:
        """The active run as the client renders it: ordered stops with per-package
        live state, the active-stop cursor, live onboard SCU, and the arrival
        flag for the active stop."""
        if not self.run:
            return None
        run, pkgs = self.run, self.run["packages"]
        active = run["active"]
        stops = []
        for i, s in enumerate(run["stops"]):
            stops.append({
                **s,
                "pickups": [{**pkgs[str(p["id"])]} for p in s["pickups"]],
                "dropoffs": [{**pkgs[str(p["id"])]} for p in s["dropoffs"]],
            })
        return {
            "id": run["id"], "ship": run.get("ship"), "usable_scu": run["usable_scu"],
            "active": active, "done": active >= len(run["stops"]),
            "arrived": active < len(run["stops"]) and self._arrived_at_active(),
            "onboard_scu": round(self.onboard_scu(), 2),
            "stops": stops,
        }

    def trade_run_view(self) -> dict | None:
        """The active trade run as the client renders it: ordered legs with per-leg
        state (pending → bought → sold), the active-leg cursor, the current phase
        (buy vs sell — which POI guidance is pointing at), running realized profit,
        the SCU currently aboard, and the arrival flag for the active waypoint."""
        run = self.trade_run
        if not run:
            return None
        legs, states = run["legs"], run["leg_states"]
        active = run["active"]
        n = len(legs)
        done = active >= n
        phase = None if done else ("sell" if states[active] == "bought" else "buy")
        # Aboard = the actual SCU bought on the active leg if the player entered it,
        # else the planned load.
        onboard = 0.0
        if not done and states[active] == "bought":
            a = legs[active]
            onboard = float(a.get("actual_buy_scu") or a.get("scu") or 0)
        # A skipped leg (bailed / stock-out) parks in 'sold' to move the cursor
        # but was never transacted — it contributes nothing realized.
        realized = sum(nav_core.trade_leg_realized(l) or 0
                       for l, st in zip(legs, states)
                       if st == "sold" and not l.get("skipped"))
        return {
            "id": run["id"], "ship": run.get("ship"), "usable_scu": run.get("usable_scu"),
            "active": active, "phase": phase, "done": done,
            "arrived": (not done) and self._arrived_at_active(),
            "onboard_scu": round(onboard, 2),
            "realized_profit": realized,
            "legs": [{**l, "state": st,
                      "realized": (nav_core.trade_leg_realized(l)
                                   if st == "sold" and not l.get("skipped") else None)}
                     for l, st in zip(legs, states)],
            "summary": run.get("summary"),
        }

    def record_crumb(self):
        """Append a breadcrumb if tracking is on, we're on a body surface, and
        we've moved far enough since the last crumb. Call after recompute()."""
        s = self.nav_state
        cont = s.get("container") if s else None
        if not (self.tracking and cont and cont.get("is_body") and s.get("latitude") is not None):
            return
        name, lat, lon = cont["name"], s["latitude"], s["longitude"]
        if self.path and self.path[-1]["container"] == name:
            radius = cont.get("body_radius_m") or 1.0
            last = self.path[-1]
            if nav_core.surface_distance_m(last["lat"], last["lon"], lat, lon, radius) < PATH_MIN_MOVE_M:
                return
        self.path.append({"lat": lat, "lon": lon, "container": name})
        if len(self.path) > PATH_MAX:
            del self.path[: len(self.path) - PATH_MAX]
        self._attach_breadcrumbs()

    def _attach_breadcrumbs(self):
        """Expose the tracking flag + the trail for the *current* container
        (crumbs on other bodies aren't drawable on the local map)."""
        if self.nav_state is None:
            return
        cont = self.nav_state.get("container")
        cur = cont["name"] if cont else None
        self.nav_state["tracking"] = self.tracking
        self.nav_state["path"] = (
            [{"lat": c["lat"], "lon": c["lon"]} for c in self.path if c["container"] == cur]
            if cur else []
        )

    async def broadcast(self):
        message = json.dumps(
            {"type": "state", "data": self.nav_state, "capture": self.capture_status()}
        )
        targets = list(self.ws_clients)   # copy: a tab may connect/drop mid-send
        if not targets:
            return
        oks = await asyncio.gather(*(_ws_send(ws, message) for ws in targets))
        for ws, ok in zip(targets, oks):
            if not ok:
                self.ws_clients.discard(ws)


class SessionHub:
    """All live member sessions + the single lock that serializes state +
    shared-dataset mutations (org scale is low; one lock is simplest + safe).

    Also owns live teammate presence: `presence[uid]` holds each sharing
    member's latest on-a-body fix; changes are queued (`_dirty`/`_removed`) and
    flushed by a ~1 Hz background broadcaster so a fast watcher can't spam tabs.
    All mutations happen while holding `lock`."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.sessions: dict[str, Session] = {}
        self.presence: dict[str, dict] = {}   # uid -> internal record (w/ last_update)
        self._dirty: set[str] = set()          # uids with a pending upsert
        self._removed: set[str] = set()        # uids with a pending remove
        # Who's-online roster (backlog #19): uid -> {status, activity, visible,
        # since, last_seen}. Decoupled from `presence` — it's identity-bearing and
        # works in space / at a station / before the game is even launched.
        self.online: dict[str, dict] = {}
        # Looking-for-group board (#19 step 3): id -> entry. In-memory + ephemeral
        # like `online`; one active LFM + one active LFJ per member (re-posting in a
        # direction supersedes), auto-expiring and dropped when the poster goes offline.
        self.lfg: dict[int, dict] = {}
        self._lfg_seq = 0
        # Pirate danger warnings (#24): id -> warning. Persisted (survives restart)
        # and community-refreshable; ages off purely by the clock like `lfg`.
        self.warnings: dict[int, dict] = {}
        self._warning_seq = 0

    def get(self, user: dict) -> Session:
        sess = self.sessions.get(user["id"])
        if sess is None:
            sess = Session(user)
            self.sessions[user["id"]] = sess
            # Resume an in-progress cargo run across restart / reconnect: reload
            # it and re-point guidance at its active stop.
            run = db.get_active_run(user["id"])
            if run:
                sess.run = run
                _point_at_active_stop(sess)
            # Resume an in-progress trade run too; when both exist the trade run
            # (re-pointed last) owns the single guidance destination.
            trade_run = db.get_active_trade_run(user["id"])
            if trade_run:
                sess.trade_run = trade_run
                _point_at_active_trade_leg(sess)
        else:
            sess.user = user           # refresh display name / admin flag
        return sess

    # --- presence -----------------------------------------------------------
    def _presence_record(self, sess: "Session") -> dict | None:
        """Build a presence fix from a session's current nav_state, or None when
        the member isn't on a body surface (presence is surface-only — there's no
        teammate map in deep space). Heading is derived from the last fix."""
        s = sess.nav_state
        cont = s.get("container") if s else None
        if not (cont and cont.get("is_body") and s.get("latitude") is not None):
            return None
        uid = sess.user["id"]
        lat, lon = s["latitude"], s["longitude"]
        system, body = s.get("system"), cont["name"]
        heading = None
        prev = self.presence.get(uid)
        if prev and prev["system"] == system and prev["body"] == body:
            radius = cont.get("body_radius_m") or 1.0
            dist, bearing = nav_core.great_circle(prev["lat"], prev["lon"], lat, lon, radius)
            heading = bearing if dist > PRESENCE_MOVE_M else prev.get("heading")
        # Share the current body's breadcrumb trail so teammates can see where this
        # member has already mapped (cuts duplicate scouting). Only crumbs on the
        # body they're standing on are drawable on the shared local map; cap the
        # payload so a long trail can't bloat the ~1 Hz upsert.
        trail = [{"lat": c["lat"], "lon": c["lon"]}
                 for c in sess.path if c["container"] == body]
        if len(trail) > SHARED_PATH_MAX:
            trail = trail[len(trail) - SHARED_PATH_MAX:]
        return {
            "discord_id": uid,
            "display_name": sess.user.get("display_name"),
            "handle": sess.owner["handle"] if sess.owner else None,
            "shard": sess.shard,
            "system": system, "body": body, "lat": lat, "lon": lon,
            "heading": heading, "path": trail, "last_update": time.time(),
        }

    @staticmethod
    def _public_presence(rec: dict) -> dict:
        """Wire form: drop last_update, expose age_s at send time."""
        return {
            "discord_id": rec["discord_id"], "display_name": rec["display_name"],
            "handle": rec["handle"], "shard": rec["shard"],
            "system": rec["system"], "body": rec["body"],
            "lat": rec["lat"], "lon": rec["lon"], "heading": rec["heading"],
            "path": rec.get("path", []),
            "age_s": max(0.0, time.time() - rec["last_update"]),
        }

    def touch_presence(self, sess: "Session") -> None:
        """Recompute + queue this member's presence (or a remove if they left a
        body / stopped sharing). Call under the lock after recompute()."""
        uid = sess.user["id"]
        rec = self._presence_record(sess) if sess.share_presence else None
        if rec is None:
            self.drop_presence(uid)
            return
        self.presence[uid] = rec
        self._dirty.add(uid)
        self._removed.discard(uid)

    def drop_presence(self, uid: str) -> None:
        if uid in self.presence:
            del self.presence[uid]
            self._removed.add(uid)
            self._dirty.discard(uid)

    def roster(self) -> list[dict]:
        return [self._public_presence(r) for r in self.presence.values()]

    # --- online roster (who's online, #19) ----------------------------------
    def mark_online(self, sess: "Session") -> bool:
        """Register (or refresh the heartbeat of) a member in the online roster.
        Returns True when this call actually *adds* the member — a fresh arrival
        or a re-add after a stale prune — so the caller knows to broadcast. A plain
        heartbeat on an already-present member returns False. Call under the lock."""
        uid = sess.user["id"]
        rec = self.online.get(uid)
        if rec is None:
            now = time.time()
            # Seed from the member's persisted prefs so a refresh/reconnect keeps
            # their chosen status/activity and "appear offline" choice (read once
            # per arrival, not on the per-ping heartbeat below).
            prefs = db.get_member(uid) or {}
            status = prefs.get("online_status")
            self.online[uid] = {
                "status": status if status in ONLINE_STATUSES else "available",
                "activity": prefs.get("online_activity"),
                # Profile tags ride the roster record (loaded once per arrival,
                # like status/activity) so the broadcast path stays DB-free.
                "tags": member_playstyles(prefs),
                "visible": not prefs.get("appear_offline"),
                "since": now,
                "last_seen": now,
            }
            return True
        rec["last_seen"] = time.time()
        return False

    def drop_online(self, uid: str) -> bool:
        """Remove a member from the online roster (last tab closed / stale).
        Returns True if a record was actually removed. Call under the lock."""
        return self.online.pop(uid, None) is not None

    def _public_online(self, uid: str, rec: dict) -> dict:
        """Wire form for one online member: resolved name, status/activity, how
        long they've been online, and a coarse location — but only when they're
        already sharing presence (never blocks on position)."""
        out = {
            "discord_id": uid,
            "name": _resolve_member_name(uid, None),
            "status": rec["status"],
            "activity": rec["activity"],
            "tags": rec.get("tags") or [],
            "since_s": max(0.0, time.time() - rec["since"]),
        }
        pres = self.presence.get(uid)
        if pres:
            out["location"] = {
                "system": pres["system"], "body": pres["body"], "shard": pres["shard"],
            }
        return out

    _ONLINE_ORDER = {"available": 0, "busy": 1, "afk": 2}

    def online_roster(self) -> list[dict]:
        """Visible online members, available-first then alphabetical."""
        recs = [self._public_online(uid, r) for uid, r in self.online.items()
                if r["visible"]]
        recs.sort(key=lambda r: (self._ONLINE_ORDER.get(r["status"], 9),
                                 r["name"].lower()))
        return recs

    async def broadcast_online_roster(self) -> None:
        """Push the full identity-bearing online roster to every tab. Cheap at org
        scale (dozens of members); sent on arrivals/departures/stale prunes."""
        await self.send_to_all_clients(
            {"type": "online_roster", "users": self.online_roster()})

    async def send_to_all_clients(self, message: dict) -> None:
        text = json.dumps(message)
        # Snapshot (session, ws) pairs, then fan out concurrently with a per-send
        # timeout: the whole broadcast is bounded by WS_SEND_TIMEOUT_S wall-clock
        # instead of the sum over clients, and a stuck client can't stall it.
        targets = [(s, ws) for s in list(self.sessions.values())
                   for ws in list(s.ws_clients)]
        if not targets:
            return
        oks = await asyncio.gather(*(_ws_send(ws, text) for _, ws in targets))
        for (s, ws), ok in zip(targets, oks):
            if not ok:
                s.ws_clients.discard(ws)

    def online_count(self) -> int:
        """Members shown as online right now — the size of the *visible* online
        roster. Counts people, not tabs (one member with three tabs is one online
        player), and a member who set themselves to "appear offline" (#19 step 2,
        visible=False) drops out. Approximate: a half-open tab lingers until the
        stale-prune sweeps it (see ONLINE_STALE_S)."""
        return sum(1 for r in self.online.values() if r["visible"])

    async def broadcast_online(self) -> None:
        """Push the current online-player count to every tab. Cheap; called on
        connect/disconnect so the top-bar count tracks comings and goings."""
        await self.send_to_all_clients({"type": "online", "count": self.online_count()})

    # --- looking-for-group board (who wants to group, #19 step 3) ------------
    def post_lfg(self, poster: str, direction: str, tags: list[str],
                 slots: int | None, note: str, rally: str | None,
                 comms: bool) -> dict:
        """Create this member's LFG entry in the given direction. One active entry
        per direction per member — a re-post supersedes the previous one. Returns the
        new internal record. Call under the lock."""
        superseded = [eid for eid, e in self.lfg.items()
                      if e["poster"] == poster and e["direction"] == direction]
        for eid in superseded:
            del self.lfg[eid]
        db.lfg_delete(superseded)
        self._lfg_seq += 1
        entry = {
            "id": self._lfg_seq, "poster": poster, "direction": direction,
            "tags": tags, "slots": slots if direction == "lfm" else None,
            "note": note, "rally": rally, "comms": comms,
            "responders": [],   # joiners (lfm) / interested pings (lfj)
            "created": time.time(),
        }
        self.lfg[entry["id"]] = entry
        db.lfg_upsert(entry)
        return entry

    def join_lfg(self, entry_id: int, uid: str) -> dict | None:
        """Toggle a member's response: Join/Leave (LFM) or Ping/Un-ping (LFJ). A member
        can't respond to their own post; LFM joins are capped at the slot count (a full
        entry ignores new joins). Returns the updated entry, or None if it's gone.
        Call under the lock."""
        e = self.lfg.get(entry_id)
        if e is None or uid == e["poster"]:
            return e
        if uid in e["responders"]:
            e["responders"].remove(uid)                     # leave / un-ping
        elif not (e["direction"] == "lfm" and e["slots"]
                  and len(e["responders"]) >= e["slots"]):  # join unless full
            e["responders"].append(uid)
        db.lfg_upsert(e)
        return e

    def close_lfg(self, entry_id: int, uid: str, is_admin: bool) -> bool:
        """Remove an entry — only its poster (or an admin) may close it. Returns True
        if one was actually removed. Call under the lock."""
        e = self.lfg.get(entry_id)
        if e is None or (e["poster"] != uid and not is_admin):
            return False
        del self.lfg[entry_id]
        db.lfg_delete(entry_id)
        return True

    def drop_lfg_for(self, uid: str) -> bool:
        """Drop every entry a member posted. Returns True if anything was removed.
        Call under the lock. (Not tied to presence anymore — posts age off by the
        clock — but kept for an explicit purge, e.g. an admin removing a member.)"""
        gone = [eid for eid, e in self.lfg.items() if e["poster"] == uid]
        for eid in gone:
            del self.lfg[eid]
        db.lfg_delete_for(uid)
        return bool(gone)

    def prune_lfg(self, now: float) -> bool:
        """Sweep aged-off entries (older than the org's age-off window). Returns True
        if any were removed. Call under the lock."""
        ageoff_s = lfg_ageoff_min() * 60
        gone = [eid for eid, e in self.lfg.items() if now - e["created"] >= ageoff_s]
        for eid in gone:
            del self.lfg[eid]
        db.lfg_delete(gone)
        return bool(gone)

    def _public_lfg(self, e: dict) -> dict:
        """Wire form for one entry: resolved poster name + their live online status,
        responder names, filled count, and age / time-left."""
        now = time.time()
        prec = self.online.get(e["poster"])
        age = max(0.0, now - e["created"])
        ageoff_s = lfg_ageoff_min() * 60
        stale_s = lfg_stale_min() * 60
        return {
            "id": e["id"], "poster_id": e["poster"],
            "poster": _resolve_member_name(e["poster"], None),
            "poster_status": prec["status"] if prec else "offline",
            "direction": e["direction"], "tags": e["tags"], "slots": e["slots"],
            "note": e["note"], "rally": e["rally"], "comms": e["comms"],
            "responders": [{"id": r, "name": _resolve_member_name(r, None)}
                           for r in e["responders"]],
            "filled": len(e["responders"]),
            "age_s": age,
            "expires_s": max(0.0, ageoff_s - age),
            "stale": age >= stale_s,   # → yellow card; nearing age-off
        }

    def lfg_board(self) -> list[dict]:
        """All active LFG entries, newest first. The frontend splits them by direction."""
        return [self._public_lfg(e) for e in
                sorted(self.lfg.values(), key=lambda e: e["created"], reverse=True)]

    async def broadcast_lfg(self) -> None:
        """Push the full LFG board to every tab — org-scale cheap, mirrors the online
        roster. Sent on post / join / close / expire."""
        await self.send_to_all_clients({"type": "lfg", "entries": self.lfg_board()})

    # --- pirate danger warnings (#24) ---------------------------------------
    def post_warning(self, poster: str, kind: str, threat: str, severity: str,
                     anchor_a: int | None, anchor_b: int | None,
                     location: str, note: str) -> dict:
        """Create a danger warning. A member re-posting the *same* danger (same kind
        + same anchors + same free-text location) supersedes their previous one rather
        than stacking duplicates; distinct dangers coexist. `system` is resolved from
        the anchors when they map to known POIs. Returns the new record. Call under
        the lock."""
        system = None
        for pid in (anchor_a, anchor_b):
            p = nav.pois.get(pid) if pid else None
            if p is not None:
                system = p.system
                break
        key = (kind, anchor_a, anchor_b, location.strip().lower())
        superseded = [wid for wid, w in self.warnings.items()
                      if w["poster"] == poster and (
                          w["kind"], w["anchor_a_poi"], w["anchor_b_poi"],
                          (w["location"] or "").strip().lower()) == key]
        # Flood guard: one member can only hold so many *distinct* active dangers.
        # Supersede (re-post of an identical danger) is exempt — it's net-zero. This
        # bounds the board, the DB, and the hazard-volume set the solvers process.
        if not superseded:
            mine = sum(1 for w in self.warnings.values() if w["poster"] == poster)
            if mine >= WARNINGS_MAX_PER_MEMBER:
                raise ValueError(
                    f"You already have {WARNINGS_MAX_PER_MEMBER} active warnings — "
                    "clear one before posting another.")
        for wid in superseded:
            del self.warnings[wid]
        db.warning_delete(superseded)
        self._warning_seq += 1
        entry = {
            "id": self._warning_seq, "poster": poster, "kind": kind,
            "threat": threat, "severity": severity, "system": system,
            "anchor_a_poi": anchor_a, "anchor_b_poi": anchor_b,
            "location": location, "note": note,
            "confirmations": [], "created": time.time(),
        }
        self.warnings[entry["id"]] = entry
        db.warning_upsert(entry)
        return entry

    def confirm_warning(self, warning_id: int, uid: str) -> dict | None:
        """Community "still active" refresh: bump `created` (resetting the age-off
        clock) and, for anyone other than the poster, record the confirmer. Returns the
        updated warning, or None if it's gone. Call under the lock."""
        w = self.warnings.get(warning_id)
        if w is None:
            return None
        if uid != w["poster"] and uid not in w["confirmations"]:
            w["confirmations"].append(uid)
        w["created"] = time.time()
        db.warning_upsert(w)
        return w

    def close_warning(self, warning_id: int, uid: str, is_admin: bool) -> bool:
        """Clear a warning ("all clear") — poster or admin only. Returns True if one
        was removed. Call under the lock."""
        w = self.warnings.get(warning_id)
        if w is None or (w["poster"] != uid and not is_admin):
            return False
        del self.warnings[warning_id]
        db.warning_delete(warning_id)
        return True

    def drop_warnings_for(self, uid: str) -> bool:
        """Drop every warning a member posted (e.g. an admin purge). Call under the
        lock."""
        gone = [wid for wid, w in self.warnings.items() if w["poster"] == uid]
        for wid in gone:
            del self.warnings[wid]
        db.warning_delete_for(uid)
        return bool(gone)

    def prune_warnings(self, now: float) -> bool:
        """Sweep aged-off warnings (older than the org's age-off window). Returns True
        if any were removed. Call under the lock."""
        ageoff_s = warning_ageoff_min() * 60
        gone = [wid for wid, w in self.warnings.items()
                if now - w["created"] >= ageoff_s]
        for wid in gone:
            del self.warnings[wid]
        db.warning_delete(gone)
        return bool(gone)

    def _public_warning(self, w: dict) -> dict:
        """Wire form for one warning: resolved poster + anchor POIs (name/system) +
        confirmations + age / time-left. `anchor_*` resolve to None-name when the POI
        id is unknown to the current nav dataset (still routable-intent, just unlabeled)."""
        now = time.time()
        age = max(0.0, now - w["created"])
        ageoff_s = warning_ageoff_min() * 60
        stale_s = warning_stale_min() * 60

        def _anchor(pid):
            if not pid:
                return None
            p = nav.pois.get(pid)
            if p is None:
                return {"id": pid, "name": None, "system": None, "container": None}
            return {"id": pid, "name": p.name, "system": p.system,
                    "container": p.container_name}

        return {
            "id": w["id"], "poster_id": w["poster"],
            "poster": _resolve_member_name(w["poster"], None),
            "kind": w["kind"], "threat": w["threat"], "severity": w["severity"],
            "system": w["system"],
            "anchor_a": _anchor(w["anchor_a_poi"]),
            "anchor_b": _anchor(w["anchor_b_poi"]),
            "location": w["location"], "note": w["note"],
            "confirmations": [{"id": c, "name": _resolve_member_name(c, None)}
                              for c in w["confirmations"]],
            "confirmed_ids": list(w["confirmations"]),
            "confirm_count": len(w["confirmations"]),
            "age_s": age,
            "expires_s": max(0.0, ageoff_s - age),
            "stale": age >= stale_s,   # → yellow card; nearing age-off
        }

    def warnings_board(self) -> list[dict]:
        """All active danger warnings. Deadliest first, then freshest — the ordering a
        trader (avoid the worst) and a hunter (find the freshest) both want."""
        rank = {"deadly": 0, "active": 1, "sighted": 2}
        return [self._public_warning(w) for w in sorted(
            self.warnings.values(),
            key=lambda w: (rank.get(w["severity"], 9), -w["created"]))]

    async def broadcast_warnings(self) -> None:
        """Push the full danger board to every tab. Sent on post / confirm / clear /
        expire."""
        await self.send_to_all_clients(
            {"type": "warnings", "entries": self.warnings_board()})

    def active_trade_warnings(self) -> list[dict]:
        """Snapshot of the live danger board as internal records, for the trade
        planner's avoid/warn modes (#24). Call under the lock — the returned list is a
        fresh copy, so the solve can read it without holding the lock. Un-anchored
        (board-only) warnings are harmless: the nav_core avoid/annotate helpers skip
        anything missing the anchor(s) it needs."""
        return list(self.warnings.values())

    def forget_entity(self, entity_id: int) -> None:
        """A deleted/refreshed-away POI/observation must stop being any member's
        destination or last-capture reference."""
        for s in self.sessions.values():
            if s.destination_id == entity_id:
                s.destination_id = None
            if s.last_capture and s.last_capture.get("id") == entity_id:
                s.last_capture = None

    async def broadcast_all(self) -> None:
        """The shared dataset changed (capture/delete/refresh) — recompute and
        push every session so all members' nearby/destination reflect it."""
        for s in self.sessions.values():
            s.recompute()
            await s.broadcast()
        # The per-session `state` push refreshes each viewer's live nearest lists,
        # but the whole-dataset browse/filter view (and the contributor dropdown)
        # is fed by a separate /api/pois + /api/observations fetch. Nudge every tab
        # to refetch it so another member's new POI/observation shows up in the
        # filtered NEARBY list, not just on the map.
        await self.send_to_all_clients({"type": "dataset"})


hub = SessionHub()


async def presence_broadcaster():
    """~1 Hz loop: drop teammates whose last fix is stale (emit `remove`), then
    flush coalesced upserts/removes to every open tab. Coalescing means a fast
    watcher posting many positions still costs at most one upsert per tick."""
    while True:
        await asyncio.sleep(PRESENCE_TICK_S)
        try:
            # Compute every frame under the lock (snapshots of hub state), but do the
            # actual WS fan-out AFTER releasing it — a slow client must never stall
            # the ~1 Hz loop while holding hub.lock (that would freeze every
            # lock-taking endpoint). The frames are point-in-time; sending them a
            # moment later is fine (all board frames are full-state and idempotent).
            frames = []
            async with hub.lock:
                now = time.time()
                for uid, rec in list(hub.presence.items()):
                    if now - rec["last_update"] > PRESENCE_STALE_S:
                        hub.drop_presence(uid)
                upserts = [hub._public_presence(hub.presence[u])
                           for u in hub._dirty if u in hub.presence]
                removes = list(hub._removed)
                hub._dirty.clear()
                hub._removed.clear()
                # Backstop for the online roster: sweep members whose WS ping went
                # quiet (a half-open socket that never fired a clean disconnect).
                stale = [uid for uid, r in list(hub.online.items())
                         if now - r["last_seen"] > ONLINE_STALE_S]
                online_dropped = False
                for uid in stale:
                    online_dropped |= hub.drop_online(uid)
                # LFG board self-cleans purely by the clock: sweep aged-off posts.
                # (No longer tied to presence — a post lives its full age-off window
                # even if the poster steps away, and survives restarts.)
                lfg_changed = hub.prune_lfg(now)
                warnings_changed = hub.prune_warnings(now)
                if upserts:
                    frames.append({"type": "presence", "op": "upsert", "users": upserts})
                for uid in removes:
                    frames.append({"type": "presence", "op": "remove", "discord_id": uid})
                if online_dropped:
                    frames.append({"type": "online", "count": hub.online_count()})
                    frames.append({"type": "online_roster", "users": hub.online_roster()})
                if lfg_changed:
                    frames.append({"type": "lfg", "entries": hub.lfg_board()})
                if warnings_changed:
                    frames.append({"type": "warnings", "entries": hub.warnings_board()})
            for frame in frames:
                await hub.send_to_all_clients(frame)
        except Exception as exc:   # never let the loop die on a transient error
            print(f"[sc-nav] presence broadcaster error: {exc}")


async def event_reminder_loop():
    """Marquee scheduled-reminder loop. Every REMINDER_TICK_S, ping the events
    channel for scheduled events whose start is within the org's reminder lead.
    Each event is claimed (its `reminded_at` stamped) BEFORE we send, so a
    restart, a slow tick, or two overlapping ticks can never double-ping. Idle
    (no query) whenever the `events` webhook isn't configured."""
    while True:
        await asyncio.sleep(REMINDER_TICK_S)
        try:
            if not notify.is_configured("events"):
                continue
            now = datetime.now(timezone.utc)
            until = now + timedelta(minutes=notify.reminder_lead_min())
            for ev in db.events_due_for_reminder(now.isoformat(), until.isoformat()):
                # Claim first: only the caller that stamps reminded_at sends.
                if db.mark_event_reminded(ev["id"], now.isoformat()):
                    await _notify_event_reminder(ev)
        except Exception as exc:   # never let the loop die on a transient error
            print(f"[sc-nav] event reminder loop error: {exc}")


@app.on_event("startup")
async def _start_presence_broadcaster():
    # Re-hydrate the Group Finder board so a redeploy/restart no longer wipes it.
    # Aged-off posts self-clean on the first broadcaster tick; the id sequence
    # resumes past the highest persisted id so a new post never collides.
    for e in db.lfg_all():
        hub.lfg[e["id"]] = e
    hub._lfg_seq = max([0, *hub.lfg])
    # Same re-hydration for the pirate danger board (#24).
    for w in db.warnings_all():
        hub.warnings[w["id"]] = w
    hub._warning_seq = max([0, *hub.warnings])
    asyncio.create_task(presence_broadcaster())
    asyncio.create_task(event_reminder_loop())
    # v0.13.0 stored one shared Discord webhook; move it to the new per-category
    # settings so notifications keep flowing after this upgrade (one-time, no-op
    # thereafter).
    notify.migrate_legacy_webhook()


@app.post("/api/position")
async def post_position(body: PositionIn, user: dict = Depends(require_user)):
    async with hub.lock:
        sess = hub.get(user)
        now = time.time()
        new_pos = (body.x, body.y, body.z)
        if sess.pos is not None and new_pos != sess.pos:
            sess.prev_pos, sess.prev_t = sess.pos, sess.t
        sess.pos, sess.t = new_pos, now
        if body.shard:
            sess.shard = body.shard.strip() or None

        if body.handle:
            entry = handles.register(body.handle, sess.user["id"])
            # Only claim the handle as this session's owner when it is actually
            # bound to this member — otherwise a caller reporting someone else's
            # handle would get their captures attributed to the victim's PlayerID.
            if entry.get("discord_id") == sess.user["id"]:
                sess.owner = {"player_id": entry["player_id"], "handle": entry["handle"]}

        captured = False
        if sess.capture_pending is not None:
            pending = sess.capture_pending
            sess.capture_pending = None
            owner = sess.owner or {}
            if pending["kind"] == "observation":
                _capture_observation(sess, new_pos, now, pending, owner)
            else:
                _capture_poi(sess, new_pos, now, pending, owner)
            captured = True

        sess.recompute()
        sess.record_crumb()
        hub.touch_presence(sess)        # queue a teammate-map upsert (or remove)
        if captured:
            await hub.broadcast_all()   # a new POI is visible to everyone
        else:
            await sess.broadcast()
    return {"ok": True}


def _halo_capture_note(poi) -> str | None:
    """Band annotation for a deep-space capture inside the Aaron Halo (#31):
    tagging a rock immediately records which band it's in, so it's findable
    (and plannable-to) later without re-deriving anything."""
    if (poi.container_name is not None or poi.system != nav_core.HALO_SYSTEM
            or poi.global_m is None):
        return None
    loc = nav_core.halo_locate(poi.global_m)
    if loc["status"] == "band":
        return f"Aaron Halo band {loc['band']}"
    if loc["status"] == "band_offplane":
        return f"Aaron Halo band {loc['band']} radius, off-plane"
    if loc["status"] == "void":
        return f"Aaron Halo void (bands {loc['between'][0]}–{loc['between'][1]})"
    return None


def _capture_poi(sess, pos_m, now, pending, owner):
    next_id = db.next_custom_poi_id()
    poi = nav_core.custom_poi_from_position(
        nav, pos_m, now, pending["name"], pending["type"], next_id,
        owner_id=owner.get("player_id"), owner_handle=owner.get("handle"),
        qt_marker=pending.get("qt_marker", False),
        private=pending.get("private", False), note=pending.get("note"),
        system_hint=sess.system,
    )
    halo_tag = _halo_capture_note(poi)
    if halo_tag and halo_tag not in (poi.note or ""):
        poi.note = f"{poi.note} · {halo_tag}" if poi.note else halo_tag
    try:
        db.add_custom_poi(nav_core.custom_poi_to_dict(poi))
    except Exception as exc:
        print(f"[sc-nav] custom poi save failed: {exc}")
    nav.pois[poi.id] = poi
    # A new QT marker changes the nearest-jump answer for every other entity,
    # so rebuild the index + reassign nearest_qt across the dataset.
    if poi.qt_marker:
        nav_core.assign_qt_markers(nav)
    sess.last_capture = {
        "kind": "poi", "id": poi.id, "name": poi.name, "type": poi.type,
        "container": poi.container_name or "Space", "system": poi.system,
        "latitude": poi.latitude, "longitude": poi.longitude,
        "qt_marker": poi.qt_marker, "private": poi.private,
        "owner_handle": poi.owner_handle,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _capture_observation(sess, pos_m, now, pending, owner):
    category = pending["category"]
    # Shared id space across categories (>= OBSERVATION_ID_START); MAX(id)+1 from
    # the DB, so a deleted top id is never reused even across restarts.
    next_id = db.next_observation_id()
    obs = nav_core.observation_from_position(
        nav, pos_m, now, category, pending["data"], next_id,
        biome=pending.get("biome"), note=pending.get("note"),
        owner_id=owner.get("player_id"), owner_handle=owner.get("handle"),
        shard_id=sess.shard, system_hint=sess.system,
    )
    try:
        db.add_observation(nav_core.observation_to_dict(obs))
    except Exception as exc:
        print(f"[sc-nav] observation save failed: {exc}")
    nav.observations[obs.id] = obs
    sess.last_capture = {
        **nav_core._observation_base(obs),
        "latitude": obs.latitude, "longitude": obs.longitude,
        "captured_at": obs.observed_at,
    }


@app.get("/api/state")
async def get_state(user: dict = Depends(require_session)):
    sess = hub.get(user)
    return {
        "state": sess.nav_state,
        "destination_id": sess.destination_id,
        "capture": sess.capture_status(),
        "systems": nav.systems,
    }


@app.get("/api/pois")
async def get_pois(
    q: str = "", system: str | None = None, container: str | None = None,
    type: str | None = None, owner_id: int | None = None, limit: int = 25,
    user: dict | None = Depends(current_user),
):
    return nav_core.search_pois(
        nav, query=q, system=system, container=container, poi_type=type,
        owner_id=owner_id, limit=min(limit, 5000),
        viewer_owner_ids=viewer_owner_ids(user),
    )


@app.post("/api/destination")
async def set_destination(body: DestinationIn, user: dict = Depends(require_session)):
    target = nav.pois.get(body.poi_id) or nav.observations.get(body.poi_id)
    # A private POI you don't own is invisible — including as a routing target.
    if isinstance(target, nav_core.Poi) and not nav_core.poi_visible_to(
        target, viewer_owner_ids(user)
    ):
        target = None
    if target is None:
        raise HTTPException(status_code=404, detail="unknown poi_id")
    async with hub.lock:
        sess = hub.get(user)
        sess.destination_id = body.poi_id
        sess.recompute()
        await sess.broadcast()
    if isinstance(target, nav_core.Observation):
        name = nav_core.OBSERVATION_CATEGORIES[target.category]["display_name"](target.data)
    else:
        name = target.name
    return {"ok": True, "destination": {"id": body.poi_id, "name": name}}


@app.delete("/api/destination")
async def clear_destination(user: dict = Depends(require_session)):
    async with hub.lock:
        sess = hub.get(user)
        sess.destination_id = None
        sess.recompute()
        await sess.broadcast()
    return {"ok": True}


@app.post("/api/capture/start")
async def capture_start(body: CaptureIn, user: dict = Depends(require_session)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    async with hub.lock:
        sess = hub.get(user)
        if sess.capture_pending is not None:
            raise HTTPException(status_code=409, detail="another capture is already armed; cancel it first")
        sess.capture_pending = {
            "kind": "poi", "name": name, "type": body.type.strip() or "Custom",
            # A QT marker is shared navigation infrastructure, so private wins:
            # a private POI is never also a QT marker.
            "qt_marker": body.qt_marker and not body.private,
            "private": body.private,
            "note": body.note.strip() or None,
        }
        await sess.broadcast()
        return {"ok": True, "capture": sess.capture_status()}


async def _arm_observation(user, category, data, biome, note):
    async with hub.lock:
        sess = hub.get(user)
        if sess.capture_pending is not None:
            raise HTTPException(status_code=409, detail="another capture is already armed; cancel it first")
        sess.capture_pending = {
            "kind": "observation",
            "category": category,
            "data": data,
            "biome": (biome or "").strip() or None,
            "note": (note or "").strip() or None,
        }
        await sess.broadcast()
        return {"ok": True, "capture": sess.capture_status()}


@app.post("/api/capture/node")
async def capture_node_start(body: NodeCaptureIn, user: dict = Depends(require_session)):
    ore = body.ore.strip()
    if not ore:
        raise HTTPException(status_code=400, detail="ore is required")
    if isinstance(body.band, str) and len(body.band) > _BAND_MAX:
        raise HTTPException(status_code=400, detail="band value too long")
    # band passed through raw; _normalize_resource handles "Unk"/None.
    return await _arm_observation(
        user, "resource", {"ore": ore, "band": body.band}, body.biome, body.note
    )


@app.post("/api/capture/wildlife")
async def capture_wildlife_start(body: WildlifeCaptureIn, user: dict = Depends(require_session)):
    species = body.species.strip()
    if not species:
        raise HTTPException(status_code=400, detail="species is required")
    return await _arm_observation(user, "wildlife", {"species": species}, body.biome, body.note)


@app.post("/api/capture/harvestable")
async def capture_harvestable_start(body: HarvestableCaptureIn, user: dict = Depends(require_session)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    return await _arm_observation(user, "harvestable", {"name": name}, body.biome, body.note)


@app.post("/api/capture/cancel")
async def capture_cancel(user: dict = Depends(require_session)):
    async with hub.lock:
        sess = hub.get(user)
        sess.capture_pending = None
        await sess.broadcast()
    return {"ok": True}


@app.get("/api/handles")
async def list_handles():
    return handles.list()


@app.get("/api/raw_commodities")
async def list_raw_commodities():
    """Raw-ore names (uexcorp is_raw==1) for the ore datalist."""
    return raw_commodity_names


@app.get("/api/commodities")
async def list_commodities():
    """All commodity names (every kind) for the cargo-planner commodity picker."""
    return commodity_names


@app.get("/api/ships")
async def list_ships():
    """Cargo-capable ships (name + stated SCU) for the cargo-planner ship
    picker, from the uexcorp vehicles feed."""
    return ships


@app.get("/api/fleet/ships")
async def list_fleet_ships():
    """All spaceships with crew size + a default seat template, for the fleet
    organizer's ship picker (#20 v1.1). Superset of /api/ships."""
    return fleet_ships


_BLUEPRINT_INDEX_CAP = 50


def _blueprint_price_of(name: str) -> float | None:
    """aUEC-per-SCU reference for a blueprint input material, from the same
    merged price map the marketplace's 'market value' hint uses (buy side first —
    the estimate answers "what would acquiring the mats cost")."""
    p = item_prices.get(f"commodity:{catalog.slug(name or '')}") or {}
    return p.get("buy") or p.get("sell")


def _blueprint_est_cost(bp: dict) -> dict:
    """Per-craft materials-cost estimate for a recipe (#25.1 §12)."""
    return nav_core.blueprint_material_cost(bp, _blueprint_price_of)


@app.get("/api/blueprints/stat-names")
async def list_blueprint_stat_names():
    """The canonical finished-stat vocabulary across the whole blueprint feed
    (~22 names like "Damage Mitigation") — autocomplete for the crafted-stats
    form so free-typed names stop fragmenting the `?stat=` market filter
    (#25.1 §11.2). Registered before the /{bp_key} route so the literal path
    wins. Derived per call (the feed is in-memory; trivial)."""
    names = {m.get("prop")
             for bp in blueprints_feed.values()
             for a in bp.get("aspects") or []
             for m in a.get("mods") or [] if m.get("prop")}
    return {"stats": sorted(names)}


@app.get("/api/blueprints")
async def list_blueprints(q: str | None = None, category: str | None = None):
    """Crafting-blueprint search index for the commission picker (#25): key,
    item name, category, craft time, input summary. Filter with `?q=` (substring
    over name/inputs/key) and/or `?category=`; capped — the client autocompletes,
    it never pages the whole feed. Categories ride along for the filter UI."""
    needle = (q or "").strip().lower()
    rows = []
    for key, bp in blueprints_feed.items():
        row = _blueprint_index_row(key, bp)
        if category and row["cat"] != category:
            continue
        if needle and needle not in (
                f"{row['name']} {row['inputs']} {key}".lower()):
            continue
        rows.append(row)
    rows.sort(key=lambda r: ((r["name"] or "").lower(), r["key"]))
    cats = sorted({bp.get("cat") or "Misc" for bp in blueprints_feed.values()})
    return {"blueprints": rows[:_BLUEPRINT_INDEX_CAP], "total": len(rows),
            "categories": cats}


@app.get("/api/blueprints/{bp_key}")
async def get_blueprint(bp_key: str):
    """One blueprint's full record — aspects/inputs with min qualities and stat
    modifiers — plus the derived views the spec builder renders: the materials
    manifest (qty 1; the client scales) and the stat→driver inversion."""
    bp = blueprints_feed.get(bp_key)
    if bp is None:
        raise HTTPException(status_code=404, detail="unknown blueprint")
    return {"key": bp_key, **bp,
            "manifest": nav_core.blueprint_manifest(bp, 1),
            "stat_drivers": nav_core.blueprint_stat_drivers(bp),
            "est_cost": _blueprint_est_cost(bp)}


@app.get("/api/harvestables")
async def list_harvestables():
    """Harvestable flora/natural names (uexcorp kind=Natural, is_harvestable=1)
    for the Add Fauna & Harvestables datalist."""
    return harvestable_names


@app.get("/api/fauna")
async def list_fauna():
    """Curated fauna/species names for the Add Fauna datalist."""
    return fauna_names


# --- Trade-route planner (#21): terminals, prices, best-single-trade ranking -
@app.get("/api/trade/terminals")
async def list_trade_terminals():
    """Commodity terminals resolved onto routable POIs (id, name, system, poi_id),
    for the trade-planner terminal pickers. Unresolved terminals are omitted."""
    return trade_terminals


@app.get("/api/trade/prices")
async def list_trade_prices(commodity: str | None = None, terminal: int | None = None):
    """Live per-terminal buy/sell prices for resolved terminals, optionally sliced
    to one `commodity` (name) or `terminal` (id). Rows at terminals that didn't
    resolve to a POI are dropped — the planner can't route to them."""
    cnorm = (commodity or "").strip().lower()
    return [
        p for p in trade_price_points
        if (terminal is None or p["terminal_id"] == terminal)
        and (not cnorm or (p["commodity"] or "").strip().lower() == cnorm)
    ]


@app.get("/api/trade/trades")
async def list_trade_trades(
    commodity: str | None = None, system: str | None = None,
    capacity_scu: float | None = None, min_margin: int = 0,
    sort: str = "auto", limit: int = 50,
    budget: int | None = None, max_price_age_days: int | None = None,
):
    """Best single buy→sell trades over the live price feed, richest first — the
    manual-mode suggestion list and the seed set for the multi-leg solver (#21).
    `capacity_scu` (usually the member's ship's usable SCU) unlocks the throughput
    fields (max_scu, trade_profit, profit_per_hour) and the per-hour ranking.
    `budget` caps each load to affordable aUEC; `max_price_age_days` drops stale
    price points so the board matches the planner's freshness filter."""
    max_age_s = max_price_age_days * 86400 if max_price_age_days else None
    return nav_core.rank_trades(
        nav, trade_price_points, commodity=commodity, system=system,
        capacity_scu=capacity_scu, min_margin=max(0, min_margin),
        sort=sort, limit=max(1, min(limit, 200)),
        budget=(budget if budget and budget > 0 else None), max_age_s=max_age_s,
    )


class TradeLegIn(BaseModel):
    commodity: str = Field(max_length=_NAME_MAX)
    buy_terminal_id: int
    sell_terminal_id: int
    scu: float | None = Field(default=None, gt=0, le=100_000)


class TradePlanIn(BaseModel):
    mode: str = "auto"                                  # auto | filtered | manual
    usable_scu: float = Field(gt=0, le=100_000)
    start_id: int | None = None                         # POI to start from
    start_here: bool = False                            # start from live position
    max_stops: int = Field(default=6, ge=2, le=12)
    commodities: list[str] = Field(default_factory=list, max_length=50)  # filtered mode
    system: str | None = Field(default=None, max_length=_NAME_MAX)       # intra-system lock
    sort: str = "per_hour"                              # per_hour | profit
    budget: int | None = Field(default=None, gt=0, le=10_000_000_000)    # aUEC on hand cap
    minimize_deadhead: bool = False                     # trade profit for fuller holds
    max_price_age_days: int | None = Field(default=None, ge=1, le=365)   # drop stale prices
    legs: list[TradeLegIn] = Field(default_factory=list, max_length=24)  # manual mode
    ship: str | None = Field(default=None, max_length=_NAME_MAX)
    # Quantum fuel & range (#27): optional drive override + hard in-range constraint.
    qd: str | None = Field(default=None, max_length=_NAME_MAX)
    in_range_only: bool = False
    # Pirate danger board (#24): ignore = plan as-is; warn = plan normally but flag
    # legs that touch (or fly past) an active warning; avoid = route around dangers —
    # snared lanes are detoured, camped terminals dropped (auto/filtered only; manual
    # legs are the player's explicit choice, only ever badged). Default avoid: danger
    # handling is on unless overridden (#24 v2, decision 6).
    avoid_mode: str = "avoid"                           # ignore | warn | avoid
    # Personal blacklist (localStorage): POI ids to always route around, layered on
    # top of the board as extra hazard volumes. Unknown ids silently skipped.
    avoid_poi_ids: list[int] = Field(default_factory=list, max_length=50)


class TradeRunPatchIn(BaseModel):
    action: str                                # buy | sell | advance | stockout | demandout
    # target leg (defaults to the active one); guards against a stale click
    # confirming the wrong leg after the cursor moved.
    leg: int | None = Field(default=None, ge=0, le=100)
    # Actuals entered at the terminal (optional): the real aUEC/SCU price and SCU
    # moved on this buy or sell. Recorded so earnings stats reflect what actually
    # happened rather than UEX's scrape; unset falls back to the plan's figures.
    price: float | None = Field(default=None, ge=0, le=100_000_000)
    scu: float | None = Field(default=None, gt=0, le=100_000)


class TradeReplanIn(BaseModel):
    """Re-solve from the caller's live position. Sunk cargo (the active leg if
    it's mid-trade, i.e. bought-not-sold) is carried forward automatically from the
    run's own state. Optional knobs override the run's stored plan params."""
    max_stops: int | None = Field(default=None, ge=2, le=12)
    system: str | None = Field(default=None, max_length=_NAME_MAX)
    sort: str | None = None                             # per_hour | profit
    budget: int | None = Field(default=None, gt=0, le=10_000_000_000)
    minimize_deadhead: bool | None = None
    max_price_age_days: int | None = Field(default=None, ge=1, le=365)
    avoid_mode: str | None = None                       # ignore | warn | avoid (#24)
    # Quantum (#27): override the run's stored drive / in-range constraint on re-plan.
    qd: str | None = Field(default=None, max_length=_NAME_MAX)
    in_range_only: bool | None = None


class TradeFavoriteIn(BaseModel):
    """A saved trade-route favorite (#21): a member-named plan `config` (validated
    as a full TradePlanIn so a loaded favorite is always re-plannable) plus an
    optional `start_label` — the start POI's display name, which the client can't
    resolve from an id alone, kept only to repaint the picker on load."""
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    config: TradePlanIn
    start_label: str | None = Field(default=None, max_length=_NAME_MAX)


_DEADHEAD_WEIGHT = 3.0   # empty-hold time multiplier when minimize_deadhead is on


def _norm_avoid_mode(mode, default="ignore") -> str:
    return mode if mode in ("ignore", "warn", "avoid") else default


def _build_hazard_volumes(warnings, blacklist, t_ref):
    """Hazard volumes for the snare-detour planners (#24 v2): the active danger
    board plus the caller's personal blacklist, at the org's configured base
    radius. Returns None when there's nothing to build (keeps the solver on its
    zero-cost avoid=None fast path)."""
    if not warnings and not blacklist:
        return None
    return nav_core.hazard_volumes(
        nav, warnings, t_ref, radius_m=hazard_radius_km() * 1000.0,
        extra_point_ids=blacklist or ()) or None


def _leg_warning_view(w: dict) -> dict:
    """Compact wire form of one danger warning (#24) as it attaches to a trade leg:
    id + threat/severity + a resolved "where" label the leg badge can render."""
    def nm(pid):
        p = nav.pois.get(pid) if pid else None
        return p.name if p else None
    a, b = nm(w.get("anchor_a_poi")), nm(w.get("anchor_b_poi"))
    if w.get("kind") == "lane":
        where = f"{a} ↔ {b}" if a and b else (w.get("location") or "a trade lane")
    else:
        where = a or (w.get("location") or "a location")
    return {"id": w.get("id"), "kind": w.get("kind"), "threat": w.get("threat"),
            "severity": w.get("severity"), "where": where}


def _leg_stock_view(r: dict) -> dict:
    """Compact wire form of one stock report as it attaches to a trade leg:
    side (supply = buy end, demand = sell end) + kind + the observed SCU (low
    reports) + freshness + who saw it."""
    return {"side": r.get("side") or "supply", "kind": r.get("kind"),
            "scu": r.get("scu"), "age_s": r.get("age_s"),
            "by": r.get("poster_name") or ""}


_PAD_SIZE_ORDER = {"XS": 0, "S": 1, "M": 2, "L": 3, "XL": 4}


def _amenity_view(amens: list[str]) -> dict | None:
    """Distill a wiki amenity list to the operational facts a trade stop view
    renders (#28c): how cargo moves (freight elevator vs loading dock), the
    largest hangar/pad, and clinic presence. None when nothing relevant."""
    v: dict = {}
    cargo = []
    for a in amens:
        if a == "Commodity Trading - Freight Elevator":
            cargo.append("elevator")
        elif a == "Commodity Trading - Loading Dock":
            cargo.append("dock")
        elif a == "Clinic":
            v["clinic"] = True
        else:
            m = re.match(r"(Hangar|Landing Pad) (XS|S|M|L|XL)$", a)
            if m:
                key = "hangar" if m.group(1) == "Hangar" else "pad"
                if _PAD_SIZE_ORDER[m.group(2)] > _PAD_SIZE_ORDER.get(v.get(key), -1):
                    v[key] = m.group(2)
    if cargo:
        v["cargo"] = cargo
    return v or None


# (system, token-name) -> distilled amenity view, for every wiki location that
# has relevant amenities. Static per deploy (the catalog is a committed
# snapshot), so built once; keyed like the POI dedup so any loaded POI —
# starmap, synthesized station or wiki — resolves regardless of the org toggle.
WIKI_AMENITIES = {
    (rec["system"].lower(), nav_core.wiki_name_key(rec["name"])): view
    for rec in wiki_locations
    if (view := _amenity_view(rec.get("amenities") or []))
}


def _poi_amenity_view(poi_id) -> dict | None:
    p = nav.pois.get(poi_id)
    if p is None:
        return None
    return WIKI_AMENITIES.get((p.system.lower(), nav_core.wiki_name_key(p.name)))


def _annotate_leg_amenities(plan: dict) -> dict:
    """Tag each costed trade leg's endpoints with the stop's amenity facts
    (#28c) so the client can chip them. Mutates + returns the plan."""
    for lg in plan.get("legs") or ():
        for id_key, out_key in (("buy_poi_id", "buy_amen"), ("sell_poi_id", "sell_amen")):
            v = _poi_amenity_view(lg.get(id_key))
            if v:
                lg[out_key] = v
    return plan


def _annotate_leg_stock(plan: dict, reports: list[dict]) -> dict:
    """Tag each costed trade leg touched by a fresh stock report (#21) — supply
    reports on its buy end, demand reports on its sell end — so the client can
    badge it ('reported out of stock 45m ago', 'won't buy here'). No-op with no
    reports. Mutates + returns the plan."""
    if reports:
        for lg in plan.get("legs") or ():
            hits = nav_core.trade_leg_stock(lg, reports)
            if hits:
                lg["stock"] = [_leg_stock_view(r) for r in hits]
    return plan


_SEVERITY_RANK = {"deadly": 0, "active": 1, "sighted": 2}


def _annotate_trade_legs(plan: dict, warnings: list[dict], mode: str,
                         volumes=None, t_ref=None) -> dict:
    """Tag each costed leg with the active warnings around it (#24) so the client
    can badge the danger. No-op in ignore mode or with no warnings. In warn mode,
    with hazard `volumes`, also badges dangers the leg merely *flies past* (via
    nav_core.leg_hazards) — not just those at its buy/sell endpoints. In avoid
    mode, translates each leg's detour outcomes (`dodged`/`blocked` warning ids on
    its haul/approach) into resolved view dicts. Mutates + returns the plan."""
    if mode == "ignore" or not warnings:
        return plan
    wmap = {w.get("id"): w for w in warnings}
    for lg in plan.get("legs") or ():
        hits = nav_core.trade_leg_warnings(lg, warnings)
        if mode == "warn" and volumes:
            bp, sp = nav.pois.get(lg.get("buy_poi_id")), nav.pois.get(lg.get("sell_poi_id"))
            if bp is not None and sp is not None and not lg.get("held"):
                seen = {h.get("id") for h in hits}
                for wid in nav_core.leg_hazards(nav, bp, sp, volumes, t_ref):
                    w = wmap.get(wid)
                    if w is not None and wid not in seen:
                        hits.append(w)
                        seen.add(wid)
                hits.sort(key=lambda w: _SEVERITY_RANK.get(w.get("severity"), 9))
        if hits:
            lg["warnings"] = [_leg_warning_view(w) for w in hits]
        # Detour outcomes (avoid mode): dodged/blocked ids ride on the leg's
        # approach + haul sub-views; resolve them to named view dicts for badges.
        dodged, blocked = [], []
        for sub in (lg.get("to_buy"), lg.get("haul")):
            if sub:
                dodged += sub.get("dodged") or []
                blocked += sub.get("blocked") or []
        if dodged:
            lg["dodged"] = [_leg_warning_view(wmap[i]) for i in dict.fromkeys(dodged) if i in wmap]
        if blocked:
            lg["blocked"] = [_leg_warning_view(wmap[i]) for i in dict.fromkeys(blocked) if i in wmap]
    return plan


def _annotate_cargo_stops(plan: dict, warnings: list[dict], mode: str) -> dict:
    """Layer the danger board over a cargo plan (#24 v2). Cargo stops are
    contractual (never dropped), so: `warn` badges stops sitting on a warned POI
    (no reroute); `avoid` translates each arrival leg's detour outcomes
    (`dodged`/`blocked`) into named view dicts on the stop. Mutates + returns."""
    if mode == "ignore" or not warnings:
        return plan
    wmap = {w.get("id"): w for w in warnings}
    if mode == "warn":
        by_poi = {}
        for w in warnings:
            if w.get("kind") == "point" and w.get("anchor_a_poi") is not None:
                by_poi.setdefault(w["anchor_a_poi"], []).append(w)
        for s in plan.get("stops") or ():
            hits = by_poi.get(s.get("stop_id"))
            if hits:
                hits = sorted(hits, key=lambda w: _SEVERITY_RANK.get(w.get("severity"), 9))
                s["warnings"] = [_leg_warning_view(w) for w in hits]
        return plan
    for s in plan.get("stops") or ():           # avoid: resolve detour outcomes
        leg = s.get("leg") or {}
        if leg.get("dodged"):
            s["dodged"] = [_leg_warning_view(wmap[i]) for i in leg["dodged"] if i in wmap]
        if leg.get("blocked"):
            s["blocked"] = [_leg_warning_view(wmap[i]) for i in leg["blocked"] if i in wmap]
    return plan


@app.post("/api/trade/plan")
async def post_trade_plan(body: TradePlanIn, user: dict = Depends(require_session)):
    """Stateless trade-route planner (#21). `mode`:
      auto      — the solver picks commodities + route for max profit/hour,
      filtered  — same, restricted to `commodities`,
      manual    — cost the player's chosen `legs` in order (no solver).
    Start from a POI (`start_id`) or the caller's live position (`start_here`).
    `avoid_mode` (#24) layers the pirate danger board over the result. Returns
    {summary, legs, start} — feasibility, per-leg buy/sell + travel, route totals."""
    async with hub.lock:
        sess = hub.sessions.get(user["id"])
        warnings = hub.active_trade_warnings()
    # The solver is pure Python and can run 100s of ms at production POI scale;
    # off-load it so a plan request never freezes the event loop (WS pushes, the
    # presence broadcaster, watcher /api/position). It reads a snapshot of stable
    # globals + a couple of session attributes — no lock needed.
    return await asyncio.to_thread(_solve_trade_plan, body, sess, warnings)


def _solve_trade_plan(body: TradePlanIn, sess: "Session | None",
                      warnings: list[dict] | None = None) -> dict:
    """Resolve start (POI or live position) and run the right solver for the
    requested mode. Shared by /api/trade/plan (stateless) and /api/trade/run
    (persisted). `warnings` (active danger board records, #24) drive avoid_mode:
    'avoid' drops warned POIs/snared lanes from the solver; 'warn' plans normally and
    flags touched legs. Fresh stock reports (#21) always steer: 'out' buys are
    dropped from the solver pool (any avoid_mode — an empty shelf isn't a danger
    preference), and touched legs get `stock` badges. Raises HTTPException on bad
    input / missing live position."""
    if body.start_id is not None and body.start_id not in nav.pois:
        raise HTTPException(status_code=404, detail="unknown start_id")
    start_pos = None
    if body.start_here:
        if sess is None or sess.pos is None:
            raise HTTPException(status_code=400,
                                detail="no live position yet — run /showlocation, or pick a start POI")
        start_pos = sess.pos
    t_ref = sess.t if sess else None
    max_age_s = body.max_price_age_days * 86400 if body.max_price_age_days else None
    dh_weight = _DEADHEAD_WEIGHT if body.minimize_deadhead else 1.0
    mode = _norm_avoid_mode(body.avoid_mode, default="avoid")
    warnings = warnings or []
    blacklist = list(body.avoid_poi_ids or ())
    # Snare-detour volumes (#24 v2): built once per request when danger handling is
    # on. Passed to the solver only in avoid mode (it routes around them); in warn
    # mode they still feed the annotate pass so fly-past dangers get badged.
    volumes = _build_hazard_volumes(warnings, blacklist, t_ref) if mode != "ignore" else None
    avoid_poi_ids, avoid_pairs = (nav_core.trade_avoid_sets(warnings)
                                  if mode == "avoid" else (None, None))
    if mode == "avoid" and blacklist:
        # A blacklisted terminal is a camped terminal — drop it as an endpoint too,
        # not just as a fly-past volume (decision 4).
        avoid_poi_ids = frozenset(avoid_poi_ids or ()) | set(blacklist)
    solver_volumes = volumes if mode == "avoid" else None
    stock_reports = active_stock_reports()
    fuel_req, max_range_m, _qd = _resolve_drive(body.ship, body.qd)   # #27
    try:
        if body.mode == "manual":
            # Manual legs are the player's explicit choice — never silently dropped;
            # they still get warn/avoid badges + costed detours so a snared pick is flagged.
            # (in_range_only only ever annotates manual legs, never drops them.)
            plan = nav_core.cost_trade_legs(
                nav, trade_price_points, [lg.model_dump() for lg in body.legs],
                body.usable_scu, start_id=body.start_id, start_pos=start_pos,
                budget=body.budget, t_ref=t_ref, avoid_volumes=solver_volumes,
                fuel_req=fuel_req, max_range_m=max_range_m)
        else:
            plan = nav_core.plan_trade_route(
                nav, trade_price_points, body.usable_scu,
                start_id=body.start_id, start_pos=start_pos, max_stops=body.max_stops,
                commodities=(body.commodities if body.mode == "filtered" else None),
                system=body.system, sort=body.sort, budget=body.budget,
                deadhead_weight=dh_weight, max_age_s=max_age_s, t_ref=t_ref,
                avoid_poi_ids=avoid_poi_ids, avoid_pairs=avoid_pairs,
                avoid_volumes=solver_volumes,
                avoid_buys=nav_core.stock_avoid_buys(stock_reports),
                avoid_sells=nav_core.stock_avoid_sells(stock_reports),
                fuel_req=fuel_req, max_range_m=max_range_m, in_range_only=body.in_range_only)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _annotate_leg_stock(plan, stock_reports)
    _annotate_leg_amenities(plan)
    return _annotate_trade_legs(plan, warnings, mode, volumes, t_ref)


_LOW_STOCK_FRACTION = 0.5   # transacted under half the planned load → auto 'low' report


def _file_stock_report(user: dict, leg: dict, kind: str, scu=None,
                       side: str = "supply") -> None:
    """File a stock report (#21) off a run leg: `side` picks the anchor — the
    leg's buy terminal for supply reports (no-stock skip / short buy) or its sell
    terminal for demand reports (won't-buy / short sell). Silently a no-op when
    the leg has no POI on that side to anchor to (e.g. a held leg's buy) — an
    unanchored report can't steer the solver or badge anything."""
    if side == "demand":
        poi_id, terminal = leg.get("sell_poi_id"), leg.get("sell_terminal")
    else:
        poi_id, terminal = leg.get("buy_poi_id"), leg.get("buy_terminal")
    if poi_id is None or not leg.get("commodity"):
        return
    db.stock_report_save({
        "poi_id": poi_id, "terminal": terminal or "",
        "commodity": leg["commodity"], "side": side, "kind": kind, "scu": scu,
        "poster": user["id"], "poster_name": user.get("display_name") or "",
        "created": time.time(),
    })


def _initial_trade_states(legs: list[dict]) -> list[str]:
    """Per-leg starting state: a `held` leg (sunk cargo already aboard from a
    re-plan) begins 'bought' — heading to its sell; every other leg begins
    'pending' — heading to its buy."""
    return ["bought" if lg.get("held") else "pending" for lg in legs]


def _point_at_active_trade_leg(sess: "Session") -> None:
    """Point guidance at the active leg's current-phase waypoint: its buy terminal
    while 'pending', its sell terminal once 'bought'. Clears when the run is done."""
    run = sess.trade_run
    if run and run["active"] < len(run["legs"]):
        leg = run["legs"][run["active"]]
        st = run["leg_states"][run["active"]]
        sess.destination_id = leg.get("sell_poi_id") if st == "bought" else leg.get("buy_poi_id")
    else:
        sess.destination_id = None


def _advance_trade_run(sess: "Session") -> bool:
    """Skip the cursor past any fully-sold legs and re-point guidance. Returns True
    when the run is now complete."""
    run = sess.trade_run
    while run["active"] < len(run["legs"]) and run["leg_states"][run["active"]] == "sold":
        run["active"] += 1
    _point_at_active_trade_leg(sess)
    return run["active"] >= len(run["legs"])


def _new_trade_run(user: dict, body: TradePlanIn, plan: dict) -> dict:
    """Build the persisted trade-run blob from a fresh plan: ordered legs, parallel
    per-leg states, the active cursor, and the plan params (so a later re-plan can
    reuse the same knobs). Frozen summary totals ride along for history."""
    legs = plan.get("legs") or []
    sm = plan.get("summary") or {}
    return {
        "ship": body.ship, "usable_scu": body.usable_scu,
        "display_name": user.get("display_name"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "legs": legs, "leg_states": _initial_trade_states(legs), "active": 0,
        "summary": sm,
        "params": {
            "max_stops": body.max_stops, "commodities": body.commodities,
            "system": body.system, "sort": body.sort, "budget": body.budget,
            "minimize_deadhead": body.minimize_deadhead,
            "max_price_age_days": body.max_price_age_days,
            "avoid_mode": _norm_avoid_mode(body.avoid_mode, default="avoid"),   # #24
            "avoid_poi_ids": list(body.avoid_poi_ids or ()),                    # #24 v2 blacklist
            "qd": body.qd, "in_range_only": body.in_range_only,                 # #27
        },
    }


@app.post("/api/trade/run")
async def start_trade_run(body: TradePlanIn, user: dict = Depends(require_session)):
    """Start (and persist) an active trade run from the same input as /plan.
    Re-solves server-side, points guidance at the first leg's buy terminal, and
    replaces any prior active trade run. 409 if the plan yields no feasible legs."""
    async with hub.lock:
        sess = hub.get(user)
        plan = _solve_trade_plan(body, sess, hub.active_trade_warnings())
        if not plan["summary"].get("feasible") or not plan.get("legs"):
            reason = plan["summary"].get("reason") or "no profitable trades for these filters"
            raise HTTPException(status_code=409, detail=f"trade route infeasible: {reason}")
        run = _new_trade_run(user, body, plan)
        run["id"] = db.start_trade_run(user["id"], body.ship, run["started_at"], run)
        sess.trade_run = run
        _point_at_active_trade_leg(sess)
        sess.recompute()
        await sess.broadcast()
        return {"ok": True, "trade_run": sess.trade_run_view()}


@app.get("/api/trade/run")
async def get_trade_run(user: dict = Depends(require_session)):
    """The caller's active trade run (or null) — for the UI to load / resume."""
    async with hub.lock:
        return {"trade_run": hub.get(user).trade_run_view()}


@app.patch("/api/trade/run")
async def patch_trade_run(body: TradeRunPatchIn, user: dict = Depends(require_session)):
    """Advance the active trade run. `action`:
      buy       — confirm the buy at the active leg (pending → bought; guidance
                  flips to its sell terminal). Entering an SCU well under plan
                  auto-files a shared supply-'low' stock report for the terminal,
      sell      — confirm the sell (bought → sold; cursor advances to the next
                  leg). Entering an SCU well under plan auto-files a shared
                  demand-'low' report (the terminal barely bought),
      advance   — skip the active leg (bailed on it; excluded from realized stats),
      stockout  — skip the active buy because the terminal had *nothing to buy*:
                  same cursor motion as advance, plus a shared supply-'out' report
                  that steers everyone's solver away while it's fresh (#21),
      demandout — the sell terminal *won't buy* the held cargo: files a shared
                  demand-'out' report but does NOT move the cursor — the player
                  still holds the load; the natural next step is re-plan-from-here,
                  which now avoids that buyer (held-cargo sell included).
    `leg` optionally pins the target index to guard a stale click. Completing the
    last leg finishes the run."""
    async with hub.lock:
        sess = hub.get(user)
        run = sess.trade_run
        if not run:
            raise HTTPException(status_code=404, detail="no active trade run")
        active = run["active"]
        if active >= len(run["legs"]):
            raise HTTPException(status_code=409, detail="trade run already complete")
        if body.leg is not None and body.leg != active:
            raise HTTPException(status_code=409, detail="stale leg — the run has moved on")
        st = run["leg_states"][active]
        leg = run["legs"][active]
        if body.action == "buy":
            if st != "pending":
                raise HTTPException(status_code=409, detail="active leg is not awaiting a buy")
            if body.price is not None:
                leg["actual_buy_price"] = body.price
            if body.scu is not None:
                leg["actual_buy_scu"] = body.scu
                planned = float(leg.get("scu") or 0)
                if planned and body.scu < planned * _LOW_STOCK_FRACTION:
                    _file_stock_report(user, leg, "low", scu=body.scu)
            run["leg_states"][active] = "bought"
        elif body.action == "sell":
            if st != "bought":
                raise HTTPException(status_code=409, detail="active leg is not awaiting a sell")
            if body.price is not None:
                leg["actual_sell_price"] = body.price
            if body.scu is not None:
                leg["actual_sell_scu"] = body.scu
                planned = float(leg.get("scu") or 0)
                if planned and body.scu < planned * _LOW_STOCK_FRACTION:
                    _file_stock_report(user, leg, "low", scu=body.scu, side="demand")
            run["leg_states"][active] = "sold"
        elif body.action == "advance":
            leg["skipped"] = True                  # abandon the leg, move on —
            run["leg_states"][active] = "sold"     # never counted as realized
        elif body.action == "stockout":
            if st != "pending":
                raise HTTPException(status_code=409,
                                    detail="stock-out only applies before the buy")
            leg["skipped"] = True
            leg["stockout"] = True
            run["leg_states"][active] = "sold"
            _file_stock_report(user, leg, "out")   # steer the org away while fresh
        elif body.action == "demandout":
            if st != "bought":
                raise HTTPException(status_code=409,
                                    detail="no-demand only applies at the sell")
            # No cursor motion: the cargo is still aboard. The report immediately
            # steers this player's own re-plan (and everyone else's solver).
            leg["demand_reported"] = True
            _file_stock_report(user, leg, "out", side="demand")
        else:
            raise HTTPException(status_code=400, detail="bad action")
        _point_at_active_trade_leg(sess)
        completed = _advance_trade_run(sess)
        if completed:
            run["completed_at"] = datetime.now(timezone.utc).isoformat()
            db.complete_trade_run(user["id"], run["id"], run["completed_at"], run)
            sess.trade_run = None
            sess.destination_id = None
        else:
            db.update_trade_run(user["id"], run["id"], run)
        sess.recompute()
        await sess.broadcast()
        return {"ok": True, "completed": completed, "trade_run": sess.trade_run_view()}


@app.post("/api/trade/run/replan")
async def replan_trade_run(body: TradeReplanIn, user: dict = Depends(require_session)):
    """Re-solve the active run from the caller's live position (pirates knocked
    them off course). Legs already sold stay as history; the active leg's sunk
    cargo — if it's been bought but not yet sold — is carried forward and offloaded
    first, then fresh trades chain onto the freed hold. Optional knobs override the
    run's stored plan params."""
    # Phase 1 — snapshot everything the solver needs under the lock, then release
    # it so the pure-Python solve (100s of ms at production scale) doesn't freeze
    # the event loop OR serialize every other request behind hub.lock.
    async with hub.lock:
        sess = hub.get(user)
        run = sess.trade_run
        if not run:
            raise HTTPException(status_code=404, detail="no active trade run")
        if sess.pos is None:
            raise HTTPException(status_code=400,
                                detail="no live position yet — run /showlocation")
        run_id = run["id"]
        active = run["active"]
        legs, states = run["legs"], run["leg_states"]
        leg_states_snap = list(states)             # detect an active-leg change mid-solve
        done_legs = legs[:active]                  # everything before the cursor is sold
        held = None
        if active < len(legs) and states[active] == "bought":
            lg = legs[active]
            held = {"commodity": lg["commodity"], "scu": lg["scu"],
                    "buy_price": lg["buy_price"]}
        p = run.get("params") or {}
        max_age_days = (body.max_price_age_days if body.max_price_age_days is not None
                        else p.get("max_price_age_days"))
        max_age_s = max_age_days * 86400 if max_age_days else None
        minimize = (body.minimize_deadhead if body.minimize_deadhead is not None
                    else p.get("minimize_deadhead"))
        # Danger board (#24): re-plan honors the run's avoid_mode (or a body override).
        # A live re-plan is exactly when avoidance matters most — pirates just hit you.
        mode = _norm_avoid_mode(body.avoid_mode if body.avoid_mode is not None
                                else p.get("avoid_mode"), default="avoid")
        warnings = hub.active_trade_warnings()
        blacklist = list(p.get("avoid_poi_ids") or ())
        start_pos, t_ref, ship = sess.pos, sess.t, run.get("ship")
        volumes = _build_hazard_volumes(warnings, blacklist, t_ref) if mode != "ignore" else None
        avoid_poi_ids, avoid_pairs = (nav_core.trade_avoid_sets(warnings)
                                      if mode == "avoid" else (None, None))
        if mode == "avoid" and blacklist:
            avoid_poi_ids = frozenset(avoid_poi_ids or ()) | set(blacklist)
        # #27: re-resolve the drive from the run's ship (body may override qd /
        # in-range). A mid-run re-plan is exactly when a tight tank matters.
        qd = body.qd if body.qd is not None else p.get("qd")
        in_range = (body.in_range_only if body.in_range_only is not None
                    else bool(p.get("in_range_only")))
        fuel_req, max_range_m, _qd = _resolve_drive(ship, qd)
        # Fresh stock reports (#21) steer the re-plan too — a mid-run stock-out
        # skip followed by "re-plan from here" must not route back to the empty shelf.
        stock_reports = active_stock_reports()
        usable_scu = run["usable_scu"]
        max_stops = body.max_stops or p.get("max_stops") or 6
        commodities = p.get("commodities") or None
        system = body.system if body.system is not None else p.get("system")
        sort = body.sort or p.get("sort") or "per_hour"
        budget = p.get("budget")

    # Phase 2 — solve off the loop and lock.
    new_plan = await asyncio.to_thread(
        nav_core.replan_trade_route,
        nav, trade_price_points, usable_scu, start_pos=start_pos, held=held,
        max_stops=max_stops, commodities=commodities, system=system, sort=sort,
        budget=budget,
        deadhead_weight=(_DEADHEAD_WEIGHT if minimize else 1.0),
        max_age_s=max_age_s, t_ref=t_ref,
        avoid_poi_ids=avoid_poi_ids, avoid_pairs=avoid_pairs,
        avoid_volumes=(volumes if mode == "avoid" else None),
        avoid_buys=nav_core.stock_avoid_buys(stock_reports),
        avoid_sells=nav_core.stock_avoid_sells(stock_reports),
        fuel_req=fuel_req, max_range_m=max_range_m, in_range_only=in_range)
    _annotate_leg_stock(new_plan, stock_reports)
    _annotate_leg_amenities(new_plan)
    _annotate_trade_legs(new_plan, warnings, mode, volumes, t_ref)
    new_legs = new_plan.get("legs") or []
    if not new_legs:
        reason = new_plan["summary"].get("reason") or "no profitable continuation from here"
        raise HTTPException(status_code=409, detail=f"re-plan infeasible: {reason}")

    # Phase 3 — re-acquire the lock and commit, but only if the run hasn't moved
    # on underneath us (another tab advanced/abandoned/bought on the active leg
    # while we solved off-lock). We also compare leg_states, not just active: a
    # concurrent `buy` sets leg_states[active]="bought" WITHOUT moving the cursor,
    # and committing over it would discard that purchase + leave its cargo (which
    # our snapshot solved as held=None) untracked. Reject → the client re-plans
    # from the now-correct state.
    async with hub.lock:
        sess = hub.get(user)
        run = sess.trade_run
        if (not run or run["id"] != run_id or run["active"] != active
                or run["leg_states"] != leg_states_snap):
            raise HTTPException(status_code=409,
                                detail="the run moved on while re-planning — try again")
        p = run.get("params") or {}
        p["qd"], p["in_range_only"] = qd, in_range   # persist the params used
        p["avoid_mode"] = mode                       # persist the mode used
        run["params"] = p
        run["legs"] = done_legs + new_legs
        run["leg_states"] = (["sold"] * len(done_legs)) + _initial_trade_states(new_legs)
        run["active"] = len(done_legs)
        run["summary"] = new_plan.get("summary") or {}
        _point_at_active_trade_leg(sess)
        db.update_trade_run(user["id"], run["id"], run)
        sess.recompute()
        await sess.broadcast()
        return {"ok": True, "trade_run": sess.trade_run_view()}


@app.get("/api/trade/stock")
async def get_trade_stock(user: dict = Depends(require_session)):
    """The live stock board (#21): org members' fresh out-of-stock / low-stock
    reports, freshest first, for the planner's STOCK WATCH strip. `ageoff_min`
    rides along so the client can phrase the window."""
    return {"reports": active_stock_reports(), "ageoff_min": stock_ageoff_min()}


@app.delete("/api/trade/run")
async def abandon_trade_run(user: dict = Depends(require_session)):
    """Abandon the caller's active trade run and release the guidance destination."""
    async with hub.lock:
        sess = hub.get(user)
        had = db.abandon_trade_run(user["id"])
        sess.trade_run = None
        sess.destination_id = None
        sess.recompute()
        await sess.broadcast()
        return {"ok": True, "abandoned": had}


# The forecast/finder/heatmap endpoints work for any mappable observation
# category (resources by ore, harvestables by name); `category` selects which.
_MAPPABLE_CATEGORIES = ("resource", "harvestable")


def _require_mappable_category(category: str) -> str:
    if category not in _MAPPABLE_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"unknown category: {category}")
    return category


@app.get("/api/resource_cells")
async def get_resource_cells(system: str, body: str, category: str = "resource"):
    """Per-cell type composition for the map heatmap (cells with ≥1 sighting)."""
    _require_mappable_category(category)
    cont = nav.containers.get((system, body))
    if cont is None or not cont.is_body:
        raise HTTPException(status_code=404, detail="unknown body")
    cells = nav_core.resource_cells(nav, system, body, cont.body_radius, category=category)
    return {"cell_m": nav_core.RESOURCE_CELL_M, "cells": cells}


@app.get("/api/resource_ores")
async def get_resource_ores(category: str = "resource"):
    """Type names present in sightings of `category` (element-finder picker)."""
    _require_mappable_category(category)
    return nav_core.resource_ore_names(nav, category=category)


@app.get("/api/resource_hotspots")
async def get_resource_hotspots(
    request: Request, ore: str, system: str | None = None, body: str | None = None,
    limit: int = 20, sort: str = "likely", category: str = "resource",
):
    """Known areas richest in `ore` (or harvestable name), ranked. sort: likely |
    near | value. The 'near'/'value' modes use the caller's own live position."""
    _require_mappable_category(category)
    sess = hub.sessions.get(require_user(request)["id"])
    pos = sess.pos if sess else None
    t = sess.t if sess else None
    return {
        "ore": ore,
        "sort": sort,
        "category": category,
        "has_position": pos is not None,
        "cell_m": nav_core.RESOURCE_CELL_M,
        "hotspots": nav_core.resource_hotspots(
            nav, ore, system=system, body=body, limit=min(limit, 100),
            from_pos=pos, t_ref=t, sort=sort, category=category,
        ),
    }


def _clean_rewards(rewards: dict) -> dict:
    """Validate + drop empty per-contract payouts. Keys are contract labels
    (capped like the entry field); values are non-negative aUEC under a sanity
    ceiling. 400 on anything malformed."""
    out = {}
    if len(rewards) > _MAX_PACKAGES:
        raise HTTPException(status_code=400, detail="too many contract rewards")
    for label, amount in rewards.items():
        if len(label) > _CONTRACT_MAX:
            raise HTTPException(status_code=400, detail="contract label too long")
        if not (0 <= amount <= _MAX_REWARD):
            raise HTTPException(status_code=400, detail="reward out of range")
        if amount:                       # 0 / blank means "no payout entered"
            out[label] = float(amount)
    return out


def _apply_reward_summary(summary: dict, rewards: dict) -> dict:
    """Layer the run's payout onto a plan summary: total reward and the derived
    aUEC/hour (needs a finite run time). Always present so the client can render
    uniformly; aUEC/hour is null when there's no reward or no time estimate."""
    total = round(float(sum(rewards.values())), 2)
    t = summary.get("total_time_s")
    summary["total_reward"] = total
    summary["auec_per_hour"] = round(total / (t / 3600.0), 2) if (total and t) else None
    return summary


@app.post("/api/route/plan")
async def post_route_plan(body: RoutePlanIn, user: dict = Depends(require_session)):
    """Stateless cargo-route optimizer: order the accepted packages into an
    efficient run under the ship's usable SCU. Returns ordered stops (each with
    pickups/dropoffs, arrival leg detail, running onboard SCU) plus a feasibility
    + totals summary (payout + aUEC/hour when rewards are supplied). Leg distances
    reflect the caller's live rotation time."""
    sess = hub.sessions.get(user["id"])
    if body.start_id is not None and body.start_id not in nav.pois:
        raise HTTPException(status_code=404, detail="unknown start_id")
    rewards = _clean_rewards(body.rewards)
    start_pos = None
    if body.start_here:
        if sess is None or sess.pos is None:
            raise HTTPException(status_code=400,
                                detail="no live position yet — run /showlocation, or pick a start POI")
        start_pos = sess.pos
    # Snare-detour routing (#24 v2): cargo stops are contractual so avoid can only
    # add detours, never drop a stop — safe on by default.
    mode = _norm_avoid_mode(body.avoid_mode, default="avoid")
    warnings = []
    if mode != "ignore":
        async with hub.lock:
            warnings = hub.active_trade_warnings()
    volumes = (_build_hazard_volumes(warnings, list(body.avoid_poi_ids or ()),
                                     sess.t if sess else None) if mode == "avoid" else None)
    fuel_req, max_range_m, _qd = _resolve_drive(body.ship, body.qd)   # #27
    try:
        # Off-load the pure-Python solve so it never freezes the event loop.
        plan = await asyncio.to_thread(
            nav_core.plan_route,
            nav, [p.model_dump() for p in body.packages],
            usable_scu=body.usable_scu, start_id=body.start_id, start_pos=start_pos,
            t_ref=sess.t if sess else None, avoid_volumes=volumes,
            fuel_req=fuel_req, max_range_m=max_range_m, in_range_only=body.in_range_only,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _apply_reward_summary(plan["summary"], rewards)
    return _annotate_cargo_stops(plan, warnings, mode)


# --- cargo run execution (stateful, persisted per member) -------------------


class RunStartIn(RoutePlanIn):
    # ship/qd/in_range_only inherited from RoutePlanIn (#27).
    pass


class RunPatchIn(BaseModel):
    package_id: str | None = Field(default=None, max_length=_PKG_ID_MAX)
    group: str | None = Field(default=None, max_length=_PKG_ID_MAX)  # set every package of a group
    state: str | None = None       # pending | onboard | delivered
    advance: bool = False          # force past the current stop (partial load)


def _point_at_active_stop(sess: "Session") -> None:
    """Point the session's destination at the run's active stop (or clear it
    when the run is finished), so the existing guidance loop drives the player."""
    run = sess.run
    if run and run["active"] < len(run["stops"]):
        sess.destination_id = run["stops"][run["active"]]["stop_id"]
    else:
        sess.destination_id = None


def _stop_resolved(run: dict, i: int) -> bool:
    """A stop is done when every package it loads is onboard/delivered and every
    package it drops is delivered."""
    pkgs = run["packages"]
    if any(pkgs[str(p["id"])]["state"] == "pending" for p in run["stops"][i]["pickups"]):
        return False
    return all(pkgs[str(p["id"])]["state"] == "delivered" for p in run["stops"][i]["dropoffs"])


def _advance_run(sess: "Session") -> bool:
    """Skip the cursor past any fully-resolved stops and re-point guidance.
    Returns True when the run is now complete."""
    run = sess.run
    while run["active"] < len(run["stops"]) and _stop_resolved(run, run["active"]):
        run["active"] += 1
    _point_at_active_stop(sess)
    return run["active"] >= len(run["stops"])


@app.post("/api/route/run")
async def start_run(body: RunStartIn, user: dict = Depends(require_session)):
    """Start (and persist) an active run from the same input as /plan. Re-solves
    server-side, sets the first stop as the guidance destination, and replaces
    any prior active run. 409 if the bundle is infeasible."""
    if body.start_id is not None and body.start_id not in nav.pois:
        raise HTTPException(status_code=404, detail="unknown start_id")
    rewards = _clean_rewards(body.rewards)
    async with hub.lock:
        sess = hub.get(user)
        start_pos = None
        if body.start_here:
            if sess.pos is None:
                raise HTTPException(status_code=400,
                                    detail="no live position yet — run /showlocation, or pick a start POI")
            start_pos = sess.pos
        # Snare-detour routing (#24 v2): cargo stops are contractual, so avoid only
        # ever adds detours. Snapshot the board (already under the lock) + build volumes.
        mode = _norm_avoid_mode(body.avoid_mode, default="avoid")
        warnings = hub.active_trade_warnings() if mode != "ignore" else []
        volumes = (_build_hazard_volumes(warnings, list(body.avoid_poi_ids or ()), sess.t)
                   if mode == "avoid" else None)
        fuel_req, max_range_m, _qd = _resolve_drive(body.ship, body.qd)   # #27
        try:
            plan = nav_core.plan_route(
                nav, [p.model_dump() for p in body.packages],
                usable_scu=body.usable_scu, start_id=body.start_id, start_pos=start_pos,
                t_ref=sess.t, avoid_volumes=volumes,
                fuel_req=fuel_req, max_range_m=max_range_m, in_range_only=body.in_range_only,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not plan["summary"]["feasible"]:
            raise HTTPException(
                status_code=409,
                detail=f"route infeasible: needs {plan['summary']['min_capacity_scu']} usable SCU",
            )
        _apply_reward_summary(plan["summary"], rewards)
        _annotate_cargo_stops(plan, warnings, mode)
        packages = {}
        for s in plan["stops"]:
            for p in s["pickups"]:
                packages[str(p["id"])] = {**p, "state": "pending"}
        sm = plan["summary"]
        run = {
            "ship": body.ship, "usable_scu": body.usable_scu,
            # denormalized onto the blob so the guild leaderboard can label the
            # member without a join (the runs row keys only on discord_id).
            "display_name": user.get("display_name"),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stops": plan["stops"], "packages": packages, "active": 0,
            # danger-handling knobs, kept so run-mode rerenders keep the mode (#24 v2)
            "avoid_mode": mode, "avoid_poi_ids": list(body.avoid_poi_ids or ()),
            # frozen totals for history/stats (no need to re-solve completed runs)
            "rewards": rewards, "total_reward": sm["total_reward"],
            "total_time_s": sm["total_time_s"], "total_distance_m": sm["total_distance_m"],
        }
        run["id"] = db.start_run(user["id"], body.ship, run["started_at"], run)
        sess.run = run
        _point_at_active_stop(sess)
        sess.recompute()
        await sess.broadcast()
        return {"ok": True, "run": sess.run_view()}


@app.get("/api/route/run")
async def get_run(user: dict = Depends(require_session)):
    """The caller's active run (or null) — for the UI to load / resume run mode."""
    async with hub.lock:
        return {"run": hub.get(user).run_view()}


@app.patch("/api/route/run")
async def patch_run(body: RunPatchIn, user: dict = Depends(require_session)):
    """Check a package off at the active stop (state = onboard / delivered), or
    force-advance. Auto-advances past fully-resolved stops; completing the last
    stop finishes the run."""
    async with hub.lock:
        sess = hub.get(user)
        run = sess.run
        if not run:
            raise HTTPException(status_code=404, detail="no active run")
        if body.state is not None and (body.package_id is not None or body.group is not None):
            if body.state not in ("pending", "onboard", "delivered"):
                raise HTTPException(status_code=400, detail="bad package state")
            if body.group is not None:
                # set every package of a multi-pickup group at once (its single drop)
                ids = [pid for pid, p in run["packages"].items() if p.get("group") == body.group]
                if not ids:
                    raise HTTPException(status_code=404, detail="unknown group")
                for pid in ids:
                    run["packages"][pid]["state"] = body.state
            else:
                if body.package_id not in run["packages"]:
                    raise HTTPException(status_code=404, detail="unknown package")
                run["packages"][body.package_id]["state"] = body.state
        if body.advance and run["active"] < len(run["stops"]):
            run["active"] += 1
            _point_at_active_stop(sess)
        completed = _advance_run(sess)
        if completed:
            db.complete_run(user["id"], run["id"],
                            datetime.now(timezone.utc).isoformat(), run)
            # Ping the channel if this haul just set a new org record. Compare
            # against every other completed run org-wide (exclude this one, now in db).
            prior = [r for r in db.list_all_completed_runs() if r.get("id") != run["id"]]
            records = nav_core.derive_run_record(run, prior)
            if records:
                _notify_bg(_notify_hauling_record(user["id"], run, records))
            sess.run = None
            sess.destination_id = None
        else:
            db.update_run(user["id"], run["id"], run)
        sess.recompute()
        await sess.broadcast()
        return {"ok": True, "completed": completed, "run": sess.run_view()}


@app.delete("/api/route/run")
async def abandon_run(user: dict = Depends(require_session)):
    """Abandon the caller's active run and release the guidance destination."""
    async with hub.lock:
        sess = hub.get(user)
        had = db.abandon_run(user["id"])
        sess.run = None
        sess.destination_id = None
        sess.recompute()
        await sess.broadcast()
        return {"ok": True, "abandoned": had}


def _run_summary(run: dict) -> dict:
    """Compact completed-run record for the history list + the 'clone' shortcut:
    headline totals plus the full package list (POI ids resolved to names) so the
    UI can repopulate the entry form without another round-trip."""
    pkgs = []
    records = nav_core.run_packages(run)
    total_scu = nav_core.packages_scu(records)
    for p in records:
        fid, tid = p.get("from_id"), p.get("to_id")
        pkgs.append({
            "commodity": p.get("commodity"), "scu": float(p.get("scu") or 0),
            "from_id": fid, "from_name": nav_core._poi_name(nav, fid),
            "to_id": tid, "to_name": nav_core._poi_name(nav, tid),
            "contract": p.get("contract"),
            "group": p.get("group"), "group_scu": p.get("group_scu"),
        })
    reward = nav_core.run_total_reward(run)
    t = run.get("total_time_s")
    return {
        "id": run.get("id"), "ship": run.get("ship"),
        "started_at": run.get("started_at"), "completed_at": run.get("completed_at"),
        "usable_scu": run.get("usable_scu"),
        "num_stops": len(run.get("stops", [])), "num_packages": len(pkgs),
        "total_scu": round(total_scu, 2), "packages": pkgs,
        "reward": round(reward, 2), "rewards": run.get("rewards") or {},
        "auec_per_hour": round(reward / (t / 3600.0), 2) if (reward and t) else None,
    }


@app.get("/api/route/history")
async def get_route_history(user: dict = Depends(require_session)):
    """The caller's completed hauling runs (freshest first, for the recent-runs
    list + clone), headline hauling stats (totals + aUEC/hour) in two scopes —
    `stats` over the recent window and `session_stats` since the player's session
    marker — and frequency-ranked quick-picks (lanes / commodities / ships) that
    float a player's repeat hauls to the top of the entry pickers."""
    runs = db.list_run_history(user["id"])
    session_start = db.get_cargo_session_start(user["id"])
    # ISO-8601 UTC timestamps compare lexicographically, so a string >= works.
    session_runs = ([r for r in runs if (r.get("completed_at") or "") >= session_start]
                    if session_start else runs)
    return {"runs": [_run_summary(r) for r in runs],
            "stats": nav_core.derive_run_stats(runs),
            "session_stats": nav_core.derive_run_stats(session_runs),
            "session_start": session_start,
            "picks": nav_core.derive_quick_picks(nav, runs)}


@app.post("/api/route/session/reset")
async def reset_route_session(user: dict = Depends(require_session)):
    """Start a fresh hauling session: stamp 'now' as the session marker so the
    session-scoped stats reset to zero and count only runs completed from here on.
    Non-destructive — run history and quick-picks are untouched."""
    ts = datetime.now(timezone.utc).isoformat()
    db.set_cargo_session_start(user["id"], ts)
    return {"ok": True, "session_start": ts}


def _resolve_member_name(discord_id: str, stored: str | None) -> str:
    """A display name for a member anywhere one is shown (leaderboards, listings,
    stats). Prefers a name a row was stamped with; then the persisted Discord
    identity (org nick → display name) from the member directory; then a watcher
    token's name; then the member's primary or any bound handle; finally a short
    id stub. Keeps every surface labelled even for legacy rows or members who
    never minted a token."""
    if stored:
        return stored
    name = members_dir.display_name(discord_id)
    if name:
        return name
    for t in tokens.items:
        if t.get("discord_id") == discord_id and t.get("display_name"):
            return t["display_name"]
    member = members_dir.get(discord_id)
    if member and member.get("primary_handle"):
        return member["primary_handle"]
    for h in handles.handles_for(discord_id):
        return h
    return f"Member {str(discord_id)[-4:]}"


def _cargo_window_start(rng: str) -> str | None:
    """ISO start for a cargo leaderboard/stats time window: 'week' = the trailing
    7 days; anything else = all-time (None)."""
    if rng == "week":
        return (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return None


@app.get("/api/cargo/leaderboard")
async def cargo_leaderboard(range: str = "all", user: dict = Depends(require_session)):
    """Guild hauling leaderboard. Every member's completed runs are tallied per
    member (no opt-in — contribution is the point), then surfaced as two boards:
    top earners (total aUEC) and most efficient (aUEC/hour, members with timed
    runs only). `range=week` limits to the trailing 7 days; default is all-time."""
    runs = db.list_all_completed_runs(_cargo_window_start(range))
    rows = nav_core.derive_guild_leaderboard(runs)
    for r in rows:
        r["display_name"] = _resolve_member_name(r["discord_id"], r.get("display_name"))
        r["mine"] = r["discord_id"] == user["id"]
    earners = sorted(rows, key=lambda r: (-r["total_reward"], r["display_name"].lower()))
    efficient = sorted((r for r in rows if r.get("auec_per_hour")),
                       key=lambda r: (-r["auec_per_hour"], r["display_name"].lower()))
    return {"range": range, "num_haulers": len(rows),
            "earners": earners, "efficient": efficient}


@app.get("/api/cargo/stats")
async def cargo_stats(range: str = "all", user: dict = Depends(require_session)):
    """Guild-wide hauling statistics for the cargo Statistics page: headline
    totals, top commodities / lanes / ships, and a weekly aUEC sparkline.
    `range=week` scopes the totals/breakdowns to the trailing 7 days; the
    sparkline always spans the trailing weeks so the trend stays readable."""
    runs = db.list_all_completed_runs(_cargo_window_start(range))
    stats = nav_core.derive_guild_cargo_stats(nav, runs)
    # Weekly aUEC earned (mirrors /api/stats' activity series). Always all-time so
    # the trend doesn't collapse to a single bar under the 'week' range.
    spark_runs = runs if range != "week" else db.list_all_completed_runs(None)
    weeks: Counter = Counter()
    for run in spark_runs:
        ts = run.get("completed_at")
        rw = nav_core.run_total_reward(run)
        if not ts or not rw:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        weeks[_iso_week_start(dt)] += rw
    activity = []
    if weeks:
        end = _iso_week_start(datetime.now(timezone.utc))
        start = end - timedelta(weeks=_STATS_WEEKS - 1)
        wk = start
        while wk <= end:
            activity.append({"label": wk.strftime("%b %d"),
                             "count": round(weeks.get(wk, 0), 2)})
            wk += timedelta(weeks=1)
    return {"range": range, **stats, "activity": activity}


# --- trade-route history + statistics (#21 step 6) -------------------------


def _trade_run_summary(run: dict) -> dict:
    """Compact completed trade-run record for the history list + the 'run again'
    shortcut: headline realized totals plus the leg list (terminals + realized
    profit per leg) so the UI can render it and re-enter the lanes in manual mode
    without another round-trip."""
    legs = []
    for l in nav_core._trade_sold_legs(run):
        legs.append({
            "commodity": l.get("commodity"), "scu": l.get("scu"), "held": bool(l.get("held")),
            "buy_terminal_id": l.get("buy_terminal_id"), "buy_terminal": l.get("buy_terminal"),
            "buy_poi_id": l.get("buy_poi_id"), "buy_system": l.get("buy_system"),
            "sell_terminal_id": l.get("sell_terminal_id"), "sell_terminal": l.get("sell_terminal"),
            "sell_poi_id": l.get("sell_poi_id"), "sell_system": l.get("sell_system"),
            "realized": nav_core.trade_leg_realized(l),
        })
    profit = nav_core.trade_run_realized(run)
    sm = run.get("summary") or {}
    t = sm.get("total_time_s")
    return {
        "id": run.get("id"), "ship": run.get("ship"),
        "started_at": run.get("started_at"), "completed_at": run.get("completed_at"),
        "usable_scu": run.get("usable_scu"), "num_legs": len(legs),
        "total_scu": round(nav_core.trade_run_scu(run), 2),
        "total_distance_m": sm.get("total_distance_m"),
        "profit": profit, "legs": legs,
        "auec_per_hour": round(profit / (t / 3600.0), 2) if (profit and t) else None,
    }


@app.get("/api/trade/history")
async def get_trade_history(user: dict = Depends(require_session)):
    """The caller's completed trade runs (freshest first, for the recent-runs list
    + re-run), headline trading stats (realized profit, SCU, aUEC/hour) in two
    scopes — all-time and since the player's session marker — and frequency-ranked
    quick-picks (lanes / commodities / ships) that float repeat trades to the top of
    the pickers."""
    runs = db.list_trade_run_history(user["id"])
    session_start = db.get_trade_session_start(user["id"])
    session_runs = ([r for r in runs if (r.get("completed_at") or "") >= session_start]
                    if session_start else runs)
    return {"runs": [_trade_run_summary(r) for r in runs],
            "stats": nav_core.derive_trade_run_stats(runs),
            "session_stats": nav_core.derive_trade_run_stats(session_runs),
            "session_start": session_start,
            "picks": nav_core.derive_trade_quick_picks(nav, runs)}


@app.post("/api/trade/session/reset")
async def reset_trade_session(user: dict = Depends(require_session)):
    """Start a fresh trading session: stamp 'now' as the session marker so the
    session-scoped stats reset to zero and count only runs completed from here on.
    Non-destructive — trade history and quick-picks are untouched."""
    ts = datetime.now(timezone.utc).isoformat()
    db.set_trade_session_start(user["id"], ts)
    return {"ok": True, "session_start": ts}


@app.get("/api/trade/favorites")
async def list_trade_favorites(user: dict = Depends(require_session)):
    """The caller's saved trade-route favorites (freshest first). Each carries its
    stored plan `config` so the client can restore the planner form and re-solve
    against live prices."""
    return {"favorites": db.list_trade_favorites(user["id"])}


@app.post("/api/trade/favorites")
async def save_trade_favorite(body: TradeFavoriteIn, user: dict = Depends(require_session)):
    """Save the current planner setup as a named favorite. Only the *config* is
    stored (not resolved legs/prices) — loading it re-plans against live UEX data.
    Re-saving under an existing name overwrites it in place. Returns the row id."""
    cfg = body.config.model_dump()
    if body.start_label:
        cfg["start_label"] = body.start_label
    ts = datetime.now(timezone.utc).isoformat()
    fid = db.save_trade_favorite(user["id"], body.name.strip(), cfg, ts)
    return {"ok": True, "id": fid}


@app.delete("/api/trade/favorites/{fav_id}")
async def delete_trade_favorite(fav_id: int, user: dict = Depends(require_session)):
    """Remove one of the caller's saved favorites."""
    if not db.delete_trade_favorite(user["id"], fav_id):
        raise HTTPException(status_code=404, detail="unknown favorite")
    return {"ok": True}


@app.get("/api/trade/stats")
async def trade_stats(range: str = "all", user: dict = Depends(require_session)):
    """Guild-wide trading statistics for the #/trade-stats page: realized-profit
    headline totals, top commodities / lanes / ships, a top-traders board, and a
    weekly aUEC sparkline. `range=week` scopes totals/breakdowns/board to the
    trailing 7 days; the sparkline always spans the trailing weeks."""
    runs = db.list_all_completed_trade_runs(_cargo_window_start(range))
    stats = nav_core.derive_guild_trade_stats(nav, runs)
    traders = nav_core.derive_trade_leaderboard(runs)
    for r in traders:
        r["display_name"] = _resolve_member_name(r["discord_id"], r.get("display_name"))
        r["mine"] = r["discord_id"] == user["id"]
    traders.sort(key=lambda r: (-r["total_profit"], r["display_name"].lower()))
    # Weekly realized aUEC (mirrors /api/cargo/stats' series). Always all-time so the
    # trend doesn't collapse to a single bar under the 'week' range.
    spark_runs = runs if range != "week" else db.list_all_completed_trade_runs(None)
    weeks: Counter = Counter()
    for run in spark_runs:
        ts = run.get("completed_at")
        profit = nav_core.trade_run_realized(run)
        if not ts or not profit:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        weeks[_iso_week_start(dt)] += profit
    activity = []
    if weeks:
        end = _iso_week_start(datetime.now(timezone.utc))
        start = end - timedelta(weeks=_STATS_WEEKS - 1)
        wk = start
        while wk <= end:
            activity.append({"label": wk.strftime("%b %d"),
                             "count": round(weeks.get(wk, 0), 2)})
            wk += timedelta(weeks=1)
    return {"range": range, **stats, "traders": traders, "activity": activity}


# --- halo finder (#31): Aaron Halo QT-drop planner --------------------------


class HaloPlanIn(BaseModel):
    """Plan a QT drop into the Aaron Halo. Exactly one of `band` (1-10) /
    `target_poi_id` (a POI inside the belt) selects the goal; start is
    `start_poi_id` or, by default, the caller's live position."""
    band: int | None = Field(default=None, ge=1, le=10)
    target_poi_id: int | None = None
    start_poi_id: int | None = None
    aim: str = "band"                                   # band | peak
    allow_staging: bool = True
    ship: str | None = Field(default=None, max_length=_NAME_MAX)
    qd: str | None = Field(default=None, max_length=_NAME_MAX)
    avoid_poi_ids: list[int] = Field(default_factory=list, max_length=50)


@app.get("/api/halo/bands")
def get_halo_bands(user: dict = Depends(require_session)):
    """The Aaron Halo band model for the picker strip + the system-map bodies
    (star + true planets; moons are sub-pixel at map scale). Static — bodies
    don't move in SC — and identical for every caller."""
    star = [c for c in nav.containers.values()
            if c.system == nav_core.HALO_SYSTEM and c.type == "Star"]
    planets = nav_core._planets_in_system(nav, nav_core.HALO_SYSTEM)
    return {"system": nav_core.HALO_SYSTEM,
            "bands": [{**b, "width_m": b["outer_m"] - b["inner_m"]}
                      for b in nav_core.HALO_BANDS],
            "bodies": [{"name": c.name, "type": c.type,
                        "x": c.pos[0], "y": c.pos[1], "r": c.body_radius}
                       for c in star + planets],
            "attribution": nav_core.HALO_ATTRIBUTION}


def _halo_fix_system(pos, sess: "Session | None") -> str:
    """Best-effort star system for a live Halo position fix, most-confident
    signal first. Deep space is system-ambiguous — every system's data centers
    on its own (0,0,0), so the raw nearest-container guess mixes frames and can
    name the wrong system 20 Gm out. Order:
      1. a container actually detected at the fix — ground truth;
      2. inside the Aaron Halo ring (`halo_contains`) — an unambiguous Stanton
         landmark that must OUTRANK a stale sticky value carried over from an
         earlier system (the in-belt fix, v0.52.2);
      3. the session's sticky, container-confirmed system;
      4. the nearest-container heuristic — last resort.
    """
    c = nav_core.detect_container(nav, pos)
    if c is not None:
        return c.system
    if nav_core.halo_contains(pos):
        return nav_core.HALO_SYSTEM
    if sess is not None and sess.system:
        return sess.system
    return nav_core.system_at(nav, pos)


def _solve_halo_plan(body: HaloPlanIn, sess: "Session | None", user: dict) -> dict:
    """Resolve start/target and run the halo drop planner. Runs off the event
    loop (pure geometry over ~200 markers; the staged POI pair scan is the
    worst case). Raises HTTPException on bad input / missing live position."""
    if (body.band is None) == (body.target_poi_id is None):
        raise HTTPException(status_code=400,
                            detail="pick exactly one of band / target_poi_id")
    viewer = viewer_owner_ids(user)
    if body.start_poi_id is not None:
        start = nav.pois.get(body.start_poi_id)
        if start is None or not nav_core.poi_visible_to(start, viewer):
            raise HTTPException(status_code=404, detail="unknown start_poi_id")
    elif sess is None or sess.pos is None:
        raise HTTPException(status_code=400,
                            detail="no live position yet — run /showlocation, or pick a start POI")
    else:
        start = nav_core.position_start(nav, sess.pos)
        start.system = _halo_fix_system(sess.pos, sess)
    if start.system != nav_core.HALO_SYSTEM:
        # Stanton-only v1. Only a CONFIDENT foreign start is rejected: a start
        # POI, or a live fix sitting at a container detected in another system.
        # A container-less deep-space live fix is system-ambiguous (per-system
        # frames overlap near the origin) and the Halo is Stanton-only, so
        # assume Stanton rather than false-reject — this was the "travel to
        # Stanton first" bug for in-belt / deep-space fixes.
        if body.start_poi_id is not None or nav_core.detect_container(nav, sess.pos) is not None:
            raise HTTPException(status_code=400,
                                detail=f"the Aaron Halo is in {nav_core.HALO_SYSTEM} — travel there first")
        start.system = nav_core.HALO_SYSTEM
    target = None
    if body.target_poi_id is not None:
        target = nav.pois.get(body.target_poi_id)
        if target is None or not nav_core.poi_visible_to(target, viewer):
            raise HTTPException(status_code=404, detail="unknown target_poi_id")
        if target.system != nav_core.HALO_SYSTEM:
            raise HTTPException(status_code=400,
                                detail=f"target is outside {nav_core.HALO_SYSTEM}")
    # Never suggest (or stage through) a marker the caller can't see.
    markers = [p for p in nav.qt_markers
               if p.system == nav_core.HALO_SYSTEM
               and nav_core.poi_visible_to(p, viewer)]
    fuel_req, max_range_m, qd = _resolve_drive(body.ship, body.qd)   # #27
    speed = (QUANTUM_DRIVES.get(qd) or {}).get("drive_speed") if qd else None
    try:
        return nav_core.plan_halo_drop(
            nav, start=start, band=body.band, target=target,
            aim=(body.aim if body.aim in ("band", "peak") else "band"),
            markers=markers,
            volumes=nav_core.body_volumes(nav, nav_core.HALO_SYSTEM),
            avoid_poi_ids=body.avoid_poi_ids, allow_staging=body.allow_staging,
            t_ref=sess.t if sess else None, drive_speed_ms=speed,
            fuel_req=fuel_req, max_range_m=max_range_m)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/halo/plan")
async def post_halo_plan(body: HaloPlanIn, user: dict = Depends(require_session)):
    """Aaron Halo drop planner (#31): "set destination X, jump, exit QT when
    the HUD readout hits D" — into a chosen density band or at one of the
    caller's deep-space POIs, with a staging leg when no clean direct chord
    exists. Geometry is time-invariant, so a plan can be computed now and
    flown later. Returns {start, band|target, legs, drop, alternates,
    attribution}."""
    async with hub.lock:
        sess = hub.sessions.get(user["id"])
    return await asyncio.to_thread(_solve_halo_plan, body, sess, user)


@app.get("/api/halo/locate")
async def get_halo_locate(target_poi_id: int | None = None,
                          user: dict = Depends(require_session)):
    """Classify the caller's latest position fix against the band model — the
    post-drop verify step ("you're in band 5, 12,400 km past the inner edge")
    and the navigator's in-belt chip. `target_poi_id` additionally reports the
    actual miss distance to that POI (POI-mode Refine loop)."""
    async with hub.lock:
        sess = hub.sessions.get(user["id"])
        pos = sess.pos if sess else None
        t = sess.t if sess else None
    if pos is None:
        raise HTTPException(status_code=400,
                            detail="no live position yet — run /showlocation")
    # Deep-space coordinates are system-ambiguous; resolve most-confident
    # signal first (detected container → in-belt geometry → sticky → guess).
    fix_system = _halo_fix_system(pos, sess)
    if fix_system != nav_core.HALO_SYSTEM:
        view = {"status": "other_system", "system": fix_system}
    else:
        view = nav_core.halo_locate(pos)
    view["fix_age_s"] = max(0.0, time.time() - t) if t else None
    if target_poi_id is not None:
        tp = nav.pois.get(target_poi_id)
        if tp is not None and nav_core.poi_visible_to(tp, viewer_owner_ids(user)):
            g = nav_core.poi_global_m(nav, tp, t or time.time())
            if g is not None:
                view["target"] = {"id": tp.id, "name": tp.name,
                                  "miss_m": nav_core.dist3(pos, g)}
    return view


# --- event planner (guild events) ------------------------------------------


class RoleTargetIn(BaseModel):
    role: str = Field(max_length=_TYPE_MAX)
    needed: int = Field(default=1, ge=0, le=500)


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=_NAME_MAX)
    description: str = Field(default="", max_length=_DESC_MAX)
    # An event can span several activities (e.g. a Cargo Haul + Combat Patrol)
    # and several flavors (e.g. both PvP and PvE) at once.
    types: list[str] = Field(default_factory=list, max_length=12)
    categories: list[str] = Field(default_factory=list, max_length=12)
    start_at: str = Field(max_length=_META_MAX)   # ISO8601 UTC; validated below
    # Optional: after this, signups lock. Blank ⇒ signups close at start_at.
    signup_deadline: str | None = Field(default=None, max_length=_META_MAX)
    duration_min: int | None = Field(default=None, ge=0, le=100_000)
    location: str = Field(default="", max_length=_NAME_MAX)         # rally point
    event_location: str = Field(default="", max_length=_NAME_MAX)   # where it happens
    min_players: int = Field(default=0, ge=0, le=_MAX_PLAYERS)
    max_players: int | None = Field(default=None, ge=1, le=_MAX_PLAYERS)
    roles: list[RoleTargetIn] = Field(default_factory=list, max_length=_MAX_ROSTER_ROLES)


class SignupIn(BaseModel):
    roles: list[str] = Field(default_factory=list, max_length=_MAX_SIGNUP_ROLES)
    status: str = Field(default="going", max_length=16)   # going | maybe
    note: str | None = Field(default=None, max_length=_NOTE_MAX)


_EVENT_PUBLIC = ("id", "organizer_id", "title", "description",
                 "start_at", "signup_deadline", "duration_min", "location", "event_location",
                 "min_players", "max_players", "roles", "status",
                 "created_at", "updated_at")


def _normalize_event_start(s: str) -> str:
    """Parse the client's start time and canonicalize to a UTC ISO8601 string.
    Naive (tz-less) inputs are assumed UTC. Rejects unparseable values."""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="start_at must be ISO8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _validate_event(body: EventIn) -> dict:
    """Validate an event against the curated taxonomy and normalize its fields
    into the column dict db.create_event / db.update_event expect."""
    types, type_seen = [], set()
    for t in body.types:
        if t not in event_taxonomy.TYPES:
            raise HTTPException(status_code=400, detail=f"unknown event type: {t}")
        if t not in type_seen:
            type_seen.add(t)
            types.append(t)
    if not types:
        raise HTTPException(status_code=400, detail="pick at least one type")
    categories, cat_seen = [], set()
    for c in body.categories:
        if c not in event_taxonomy.CATEGORIES:
            raise HTTPException(status_code=400, detail=f"unknown event category: {c}")
        if c not in cat_seen:
            cat_seen.add(c)
            categories.append(c)
    if not categories:
        raise HTTPException(status_code=400, detail="pick at least one category")
    roster, seen = [], set()
    for r in body.roles:
        if r.role not in event_taxonomy.ROLES:
            raise HTTPException(status_code=400, detail=f"unknown role: {r.role}")
        if r.role in seen:
            raise HTTPException(status_code=400, detail=f"duplicate role: {r.role}")
        seen.add(r.role)
        roster.append({"role": r.role, "needed": r.needed})
    if body.max_players is not None and body.max_players < max(1, body.min_players):
        raise HTTPException(status_code=400, detail="max_players must be >= min_players")
    start_at = _normalize_event_start(body.start_at)
    signup_deadline = None
    if body.signup_deadline:
        signup_deadline = _normalize_event_start(body.signup_deadline)
        if signup_deadline > start_at:
            raise HTTPException(status_code=400,
                                detail="signup deadline must be at or before the event start")
    return {
        "title": body.title.strip(),
        "description": (body.description or "").strip(),
        "type": types, "category": categories,
        "start_at": start_at,
        "signup_deadline": signup_deadline,
        "duration_min": body.duration_min,
        "location": (body.location or "").strip(),
        "event_location": (body.event_location or "").strip(),
        "min_players": body.min_players, "max_players": body.max_players,
        "roles": roster,
    }


def _validate_signup_roles(roles: list[str]) -> list[str]:
    """De-dupe (order-preserving) and reject any role outside the taxonomy."""
    out, seen = [], set()
    for r in roles:
        if r not in event_taxonomy.ROLES:
            raise HTTPException(status_code=400, detail=f"unknown role: {r}")
        if r in seen:
            continue
        seen.add(r)
        out.append(r)
    return out


def _require_event_owner(ev: dict, user: dict) -> None:
    if ev["organizer_id"] != user["id"] and not user.get("is_admin"):
        raise HTTPException(status_code=403,
                            detail="only the organizer or an admin can change this event")


def _event_view(ev: dict, user: dict, detail: bool = False) -> dict:
    """Serialize an event for the client: its fields plus the derived fill
    summary, the organizer's name, the caller's own signup, and the caller's
    permissions. `detail=True` adds the attendee roster (used by the detail view;
    list cards skip it to stay light)."""
    signups = db.list_signups(ev["id"])
    mine = next((s for s in signups
                 if s["discord_id"] == user["id"] and s["status"] != "withdrawn"), None)
    view = {k: ev.get(k) for k in _EVENT_PUBLIC}
    view["types"] = ev.get("type") or []
    view["categories"] = ev.get("category") or []
    view["organizer_name"] = _resolve_member_name(ev["organizer_id"], None)
    view["is_organizer"] = ev["organizer_id"] == user["id"]
    view["can_edit"] = view["is_organizer"] or bool(user.get("is_admin"))
    view["fill"] = nav_core.derive_event_fill(ev, signups)
    view.update(nav_core.derive_event_phase(ev, datetime.now(timezone.utc)))
    view["my_signup"] = ({"roles": mine["roles"], "status": mine["status"]}
                         if mine else None)
    if detail:
        view["attendees"] = [
            {"discord_id": s["discord_id"],
             "display_name": _resolve_member_name(s["discord_id"], None),
             "roles": s["roles"], "status": s["status"]}
            for s in signups if s["status"] in ("going", "maybe")
        ]
    return view


# --- Discord notifications: shared helpers + message builders ----------------
# The dispatcher lives in notify.py; here we build the human-facing messages and
# fire them WITHOUT blocking the request that triggered them. notify.send already
# offloads the HTTP to a thread and never raises, but awaiting it would still make
# the caller wait on Discord — so app-event notifications are fired as background
# tasks. (The scheduled reminders in a later step call notify.send directly from
# their own loop.)

def _deep_link(hash_path: str) -> str:
    """A one-click link back into the SPA for a notification, or '' if we don't
    know our public URL (SC_NAV_PUBLIC_URL). `hash_path` is like '#/events'."""
    return f"\n{PUBLIC_BASE_URL}/{hash_path}" if PUBLIC_BASE_URL else ""


def _discord_ts(iso: str, style: str = "F") -> str:
    """Render an ISO8601 UTC time as a Discord `<t:unix:style>` tag so every
    member sees it in their own timezone. Falls back to the raw string."""
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return f"<t:{int(dt.timestamp())}:{style}>"
    except (ValueError, AttributeError, TypeError):
        return iso or ""


def _notify_bg(coro) -> None:
    """Fire a notification coroutine as a background task so it can't delay the
    request. Any error is logged and swallowed — a notification must never break
    the action that triggered it."""
    async def _run():
        try:
            await coro
        except Exception as exc:   # defensive: notify.send already swallows, but the builders read settings
            print(f"[sc-nav] notify task failed: {exc}")
    asyncio.create_task(_run())


async def _notify_event_created(ev: dict) -> None:
    if not notify.is_configured("events"):
        return
    where = (ev.get("event_location") or ev.get("location") or "").strip()
    loc = f"\n📍 {where}" if where else ""
    await notify.send(
        "events",
        f"📅 **New event: {ev['title']}**\n"
        f"Starts {_discord_ts(ev['start_at'])} ({_discord_ts(ev['start_at'], 'R')})"
        f"{loc}{_deep_link('#/events')}",
        dedup_key=f"event-created:{ev['id']}")


async def _notify_event_cancelled(ev: dict) -> None:
    if not notify.is_configured("events"):
        return
    await notify.send(
        "events",
        f"🚫 **Event cancelled: {ev['title']}**\n"
        f"Was set for {_discord_ts(ev['start_at'])}.{_deep_link('#/events')}",
        dedup_key=f"event-cancelled:{ev['id']}")


async def _notify_event_reminder(ev: dict) -> None:
    """The marquee "starting soon" ping. Pings each signed-up member by id so the
    reminder actually pulls them back in-game (the whole point of the feature)."""
    if not notify.is_configured("events"):
        return
    where = (ev.get("event_location") or ev.get("location") or "").strip()
    loc = f"\n📍 {where}" if where else ""
    attendees = [str(s["discord_id"]) for s in db.list_signups(ev["id"])
                 if s.get("status") != "withdrawn"
                 and str(s.get("discord_id") or "").isdigit()]
    pings = ("\n" + " ".join(f"<@{d}>" for d in attendees)) if attendees else ""
    await notify.send(
        "events",
        f"⏰ **Starting soon: {ev['title']}**\n"
        f"Begins {_discord_ts(ev['start_at'])} ({_discord_ts(ev['start_at'], 'R')})"
        f"{loc}{_deep_link('#/events')}{pings}",
        mentions=attendees,
        dedup_key=f"event-reminder:{ev['id']}")


async def _notify_lfg_posted(pub: dict) -> None:
    """Announce a new looking-for-group post to the org's Discord (#19 step 4, opt-in
    per post). A channel shout with NO @mentions — it's an open call, not a directed
    ping; members funnel back through the deep link to actually join."""
    if not notify.is_configured("lfg"):
        return
    lfm = pub["direction"] == "lfm"
    lines = [f"🔎 **Looking for members** — {pub['poster']}" if lfm
             else f"🙋 **Looking to join** — {pub['poster']}"]
    if lfm and pub.get("slots"):
        lines.append(f"Needs {pub['slots']} (filled {pub['filled']}/{pub['slots']})")
    if pub.get("tags"):
        lines.append(" ".join(f"`{t}`" for t in pub["tags"]))
    if pub.get("note"):
        lines.append(pub["note"])
    meta = []
    if pub.get("rally"):
        meta.append(f"📍 {pub['rally']}")
    if pub.get("comms"):
        meta.append("🎙 voice comms")
    if meta:
        lines.append(" · ".join(meta))
    await notify.send("lfg", "\n".join(lines) + _deep_link("#/lfg"),
                      dedup_key=f"lfg-posted:{pub['id']}")


def _lfg_announce_ok(poster_id: str) -> bool:
    """Anti-spam gate for 'announce to Discord': one announced LFG post per member per
    cooldown. Returns True (and arms the cooldown) only when allowed."""
    now = time.monotonic()
    # A never-announced member must never read as rate-limited. Don't use 0.0 as the
    # "never" sentinel: monotonic() can be < the cooldown shortly after boot, so
    # `now - 0.0 < cooldown` would wrongly gate a member's first announce for the
    # server's first LFG_ANNOUNCE_COOLDOWN_S seconds of uptime. Check membership.
    last = _lfg_announce_at.get(poster_id)
    if last is not None and now - last < LFG_ANNOUNCE_COOLDOWN_S:
        return False
    _lfg_announce_at[poster_id] = now
    return True


async def _notify_warning_posted(pub: dict) -> None:
    """Announce a fresh pirate danger warning to the org's Discord (#24, opt-in per
    post). A channel shout with NO @mentions — it warns traders off the lane and
    rallies hunters onto the camper; both funnel back through the deep link."""
    if not notify.is_configured("pirates"):
        return
    icon = "☠️" if pub["threat"] == "pvp" else "🤖"
    threat = "players (PvP)" if pub["threat"] == "pvp" else "NPC pirates (PvE)"
    a = (pub.get("anchor_a") or {}).get("name")
    b = (pub.get("anchor_b") or {}).get("name")
    if pub["kind"] == "lane":
        where = f"{a} ↔ {b}" if a and b else (pub.get("location") or "a trade lane")
        head = f"{icon} **Pirate snare — {where}**"
    else:
        where = a or (pub.get("location") or "a location")
        head = f"{icon} **Danger near {where}**"
    lines = [head,
             f"{pub['severity'].upper()} · {threat} · reported by {pub['poster']}"]
    loc = (pub.get("location") or "").strip()
    if loc and loc != where:
        lines.append(f"📍 {loc}")
    if pub.get("note"):
        lines.append(pub["note"])
    await notify.send("pirates", "\n".join(lines) + _deep_link("#/pirates"),
                      dedup_key=f"warning-posted:{pub['id']}")


def _warning_announce_ok(poster_id: str) -> bool:
    """Anti-spam gate for announcing a danger warning: one announced warning per
    member per cooldown. Returns True (and arms the cooldown) only when allowed."""
    now = time.monotonic()
    last = _warning_announce_at.get(poster_id)
    if last is not None and now - last < WARNING_ANNOUNCE_COOLDOWN_S:
        return False
    _warning_announce_at[poster_id] = now
    return True


def _mentions(*discord_ids) -> tuple[list[str], str]:
    """(allowed-mentions list, trailing ping text) for one or more members, deduped
    and order-preserving. Only real Discord snowflakes can be pinged — legacy or
    synthetic ids are dropped so a message never carries a dead `<@id>`."""
    valid = list(dict.fromkeys(str(i) for i in discord_ids if str(i or "").isdigit()))
    ping = ("\n" + " ".join(f"<@{d}>" for d in valid)) if valid else ""
    return valid, ping


def _auec(n) -> str:
    """Format an aUEC amount for a notification (thousands-separated, no decimals)."""
    return f"{float(n):,.0f} aUEC"


def _offer_amount_text(offer: dict) -> str:
    """How an offer reads in a ping: an aUEC bid/price for sale+auction, or the
    countered item / note for a barter."""
    if offer.get("amount_auec") is not None:
        return f"offered {_auec(offer['amount_auec'])}"
    parts = [p for p in (offer.get("offer_item_name"),
                         f"“{offer['offer_note']}”" if offer.get("offer_note") else None) if p]
    return "offered " + (" — ".join(parts) if parts else "a trade")


def _commission_announce_ok(poster_id: str) -> bool:
    """Anti-spam gate for announcing a craft request: one shout per member per
    cooldown. Returns True (and arms the cooldown) only when allowed."""
    now = time.monotonic()
    last = _commission_announce_at.get(poster_id)
    if last is not None and now - last < COMMISSION_ANNOUNCE_COOLDOWN_S:
        return False
    _commission_announce_at[poster_id] = now
    return True


_ANNOUNCE_MENTION_CAP = 15   # sanity cap on capable-crafter pings per announce


async def _notify_commission_posted(listing: dict) -> None:
    """Opt-in shout for a new craft request (#25) — the requester asked for
    reach, so it broadcasts to the marketplace channel with the job's headline
    terms (spec quality, budget, materials sourcing, needed-by). Members whose
    blueprint library holds the recipe get an @-mention (#25.1) so the request
    reaches the people who can actually take it; everyone else just sees the
    channel post."""
    if not notify.is_configured("marketplace"):
        return
    bits = []
    spec = (listing.get("attributes") or {}).get("spec") or {}
    if spec.get("quality"):
        bits.append(f"Q{spec['quality']}+")
    qty = listing.get("qty") or 1
    if qty and float(qty) > 1:
        bits.append(f"×{qty:g}")
    if listing.get("price_auec"):
        bits.append(f"budget {_auec(listing['price_auec'])}")
    mats = {"requester": "materials supplied", "crafter": "crafter sources mats",
            "split": "materials split"}.get(listing.get("materials") or "")
    if mats:
        bits.append(mats)
    if listing.get("ends_at"):
        bits.append(f"needed by {str(listing['ends_at'])[:10]}")
    who = _resolve_member_name(listing["seller_id"], None)
    detail = (" — " + ", ".join(bits)) if bits else ""
    # Ping the members who can craft this (library match), minus the requester.
    key = listing.get("blueprint_key")
    crafters = [m for m in (db.blueprint_crafters(key) if key else [])
                if str(m) != str(listing["seller_id"])][:_ANNOUNCE_MENTION_CAP]
    craft_line = ("\nCan craft: " + " ".join(f"<@{m}>" for m in crafters)) if crafters else ""
    await notify.send(
        "marketplace",
        f"🛠️ **WANTED: {listing.get('item_name')}**{detail}\n"
        f"Posted by {who}. Quote the job on the board.{craft_line}{_deep_link('#/market')}",
        dedup_key=f"commission-posted:{listing['id']}",
        mentions=crafters)


async def _notify_market_offer(listing: dict, offer: dict, *, deal: bool) -> None:
    """Tell the seller activity landed on their listing — a standing offer/bid, or
    an instant buy/buyout that already struck the deal (`deal=True`). Commission
    copy reads crafter-side: a quote on the job, never a purchase."""
    if not notify.is_configured("marketplace"):
        return
    mentions, ping = _mentions(listing["seller_id"])
    who = _resolve_member_name(offer["bidder_id"], None)
    item = listing.get("item_name") or "your listing"
    if listing.get("mode") == "commission":
        body = (f"🛠️ **New quote on {item}**\n{who} quoted "
                f"{_auec(offer['amount_auec'])}.")
    elif deal:
        body = (f"🎉 **{item} sold!**\n"
                f"{who} bought it for {_auec(offer['amount_auec'])} — confirm the "
                f"handoff once you meet up in-game.")
    else:
        body = f"💰 **New offer on {item}**\n{who} {_offer_amount_text(offer)}."
    await notify.send("marketplace", f"{body}{_deep_link('#/market')}{ping}",
                      mentions=mentions, dedup_key=f"market-offer:{offer['id']}")


async def _notify_market_accepted(listing: dict, offer: dict) -> None:
    """Tell the bidder the seller accepted their offer — the deal is now pending."""
    if not notify.is_configured("marketplace"):
        return
    mentions, ping = _mentions(offer["bidder_id"])
    seller = _resolve_member_name(listing["seller_id"], None)
    item = listing.get("item_name") or "a listing"
    amount = f" — {_auec(offer['amount_auec'])}" if offer.get("amount_auec") is not None else ""
    if listing.get("mode") == "commission":
        body = (f"🛠️ **You got the job**\n{seller} accepted your quote on {item}"
                f"{amount}. Get crafting.")
    else:
        body = (f"🤝 **Your offer was accepted**\n{seller} accepted your offer on "
                f"{item}{amount}. Coordinate the handoff.")
    await notify.send(
        "marketplace", f"{body}{_deep_link('#/market')}{ping}",
        mentions=mentions, dedup_key=f"market-accept:{offer['id']}")


async def _notify_market_confirm_needed(listing: dict, confirmed_by: str) -> None:
    """One side confirmed the handoff — nudge the other to confirm so it closes."""
    if not notify.is_configured("marketplace"):
        return
    other_id = listing["buyer_id"] if confirmed_by == "seller" else listing["seller_id"]
    mentions, ping = _mentions(other_id)
    who = _resolve_member_name(listing["seller_id"] if confirmed_by == "seller"
                               else listing["buyer_id"], None)
    item = listing.get("item_name") or "your deal"
    await notify.send(
        "marketplace",
        f"📦 **{who} confirmed the handoff for {item}**\n"
        f"Confirm on your side to close the deal.{_deep_link('#/market')}{ping}",
        mentions=mentions,
        dedup_key=f"market-confirm:{listing['id']}:{confirmed_by}")


async def _notify_market_completed(listing: dict) -> None:
    """Both sides confirmed — the trade is done. Ping buyer and seller."""
    if not notify.is_configured("marketplace"):
        return
    mentions, ping = _mentions(listing["seller_id"], listing["buyer_id"])
    seller = _resolve_member_name(listing["seller_id"], None)
    buyer = _resolve_member_name(listing["buyer_id"], None)
    item = listing.get("item_name") or "a listing"
    amount = f", {_auec(listing['final_auec'])}" if listing.get("final_auec") is not None else ""
    label = "Commission complete" if listing.get("mode") == "commission" else "Deal complete"
    await notify.send(
        "marketplace",
        f"✅ **{label}: {item}**\n{seller} ↔ {buyer}{amount}. Nice doing "
        f"business.{_deep_link('#/market')}{ping}",
        mentions=mentions, dedup_key=f"market-complete:{listing['id']}")


async def _notify_goal_met(goal: dict, contributions) -> None:
    """Celebrate a procurement goal crossing 100% — a communal win, so it broadcasts
    to the channel and pings the goal's creator so they know it's fulfilled."""
    if not notify.is_configured("goals"):
        return
    mentions, ping = _mentions(goal["creator_id"])
    contributors = len({r.get("owner_id") for r in (contributions or []) if r.get("owner_id")})
    who = f"\n{contributors} contributor{'s' if contributors != 1 else ''} chipped in." if contributors else ""
    await notify.send(
        "goals",
        f"🎯 **Goal reached: {goal['title']}**\nFully stocked — 100%.{who}"
        f"{_deep_link('#/goals')}{ping}",
        mentions=mentions, dedup_key=f"goal-met:{goal['id']}")


async def _notify_hauling_record(hauler_id: str, run: dict, records: dict) -> None:
    """Brag a just-completed run that set a new org hauling record (single-run total
    and/or aUEC/hour). Broadcasts to the channel and pings the hauler."""
    if not notify.is_configured("records"):
        return
    mentions, ping = _mentions(hauler_id)
    who = _resolve_member_name(hauler_id, None)
    lines = []
    if records.get("total") is not None:
        lines.append(f"💰 single-run haul: **{_auec(records['total'])}**")
    if records.get("rate") is not None:
        lines.append(f"⚡ efficiency: **{_auec(records['rate'])}/hr**")
    if not lines:
        return
    await notify.send(
        "records",
        f"🏆 **New org hauling record — {who}!**\n" + "\n".join(lines) +
        f"{_deep_link('#/route')}{ping}",
        mentions=mentions, dedup_key=f"hauling-record:{run.get('id')}")


@app.get("/api/events/taxonomy")
async def events_taxonomy(user: dict = Depends(require_session)):
    """Curated types / categories / grouped roles for the create form."""
    return event_taxonomy.taxonomy()


@app.get("/api/events")
async def list_events(range: str = "upcoming", user: dict = Depends(require_session)):
    """The event board. `range=past` lists finished/cancelled events (freshest
    first); default lists everything not yet finished — open, signups-closed, and
    live/ongoing — soonest first. Each carries its derived fill + phase so cards
    render (and badge) without a per-event round-trip."""
    now_dt = datetime.now(timezone.utc)
    if range == "past":
        rows = db.list_events("past", now_dt.isoformat())
    else:
        # Reach back so live/ongoing events (start passed, not yet ended) stay on the
        # board; the phase filter below drops the ones that have actually finished.
        lookback = (now_dt - timedelta(minutes=_EVENT_BOARD_LOOKBACK_MIN)).isoformat()
        rows = db.list_events("upcoming", lookback)
    views = [_event_view(e, user) for e in rows]
    if range == "past":
        views = [v for v in views if v["phase"] in ("ended", "cancelled")]
    else:
        views = [v for v in views if v["phase"] != "ended"]
    return {"range": range, "events": views}


@app.post("/api/events")
async def create_event(body: EventIn, user: dict = Depends(require_session)):
    """Create an event. Any org member may organize."""
    fields = _validate_event(body)
    now = datetime.now(timezone.utc).isoformat()
    eid = db.create_event({**fields, "organizer_id": user["id"],
                           "status": "scheduled", "created_at": now, "updated_at": now})
    ev = db.get_event(eid)
    _notify_bg(_notify_event_created(ev))
    return _event_view(ev, user, detail=True)


@app.get("/api/events/{event_id}")
async def get_event(event_id: int, user: dict = Depends(require_session)):
    """One event with its attendee roster + fill."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    return _event_view(ev, user, detail=True)


@app.patch("/api/events/{event_id}")
async def edit_event(event_id: int, body: EventIn, user: dict = Depends(require_session)):
    """Edit an event (organizer or admin) — full replace of the editable fields."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    _require_event_owner(ev, user)
    fields = _validate_event(body)
    db.update_event(event_id, fields, datetime.now(timezone.utc).isoformat())
    return _event_view(db.get_event(event_id), user, detail=True)


@app.delete("/api/events/{event_id}")
async def cancel_event(event_id: int, user: dict = Depends(require_session)):
    """Cancel an event (organizer or admin). Soft — the row + roster survive."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    _require_event_owner(ev, user)
    db.cancel_event(event_id, datetime.now(timezone.utc).isoformat())
    _notify_bg(_notify_event_cancelled(ev))
    return {"ok": True, "status": "cancelled"}


@app.post("/api/events/{event_id}/signup")
async def signup_event(event_id: int, body: SignupIn,
                       user: dict = Depends(require_session)):
    """Join (or update) the caller's signup with the role(s) they'll fill."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    phase = nav_core.derive_event_phase(ev, datetime.now(timezone.utc))
    if not phase["signups_open"]:
        detail = ("event is cancelled" if phase["phase"] == "cancelled"
                  else "signups have closed for this event")
        raise HTTPException(status_code=400, detail=detail)
    roles = _validate_signup_roles(body.roles)
    status = body.status if body.status in ("going", "maybe") else "going"
    db.upsert_signup(event_id, user["id"], roles, status, body.note,
                     datetime.now(timezone.utc).isoformat())
    return _event_view(db.get_event(event_id), user, detail=True)


@app.delete("/api/events/{event_id}/signup")
async def withdraw_signup(event_id: int, user: dict = Depends(require_session)):
    """Withdraw the caller from an event (kept as 'withdrawn' so re-joining is easy)."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    db.withdraw_signup(event_id, user["id"])
    return _event_view(db.get_event(event_id), user, detail=True)


# --- fleet roster / squad organizer (#20) ----------------------------------
# A plan layer over signups: the organizer (or an admin) builds named groups and
# slots signed-up members into them. All routes are scoped to the event and the
# write routes reuse _require_event_owner, so the plan is organizer-owned while
# signups stay member-owned.

_MAX_EVENT_GROUPS = 60       # sanity cap on units in one event's plan
_SLOT_MAX = 40               # seat/role label within a group


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    kind: str = Field(default="squad", max_length=_TYPE_MAX)
    ship: str | None = Field(default=None, max_length=_NAME_MAX)
    capacity: int | None = Field(default=None, ge=0, le=_MAX_PLAYERS)
    parent_id: int | None = Field(default=None)
    leader_id: str | None = Field(default=None, max_length=_META_MAX)
    notes: str | None = Field(default=None, max_length=_NOTE_MAX)
    sort: int = Field(default=0, ge=0, le=100_000)


class AssignmentIn(BaseModel):
    discord_id: str = Field(min_length=1, max_length=_META_MAX)
    # group_id None ⇒ unassign the member back to the pool.
    group_id: int | None = Field(default=None)
    slot: str | None = Field(default=None, max_length=_SLOT_MAX)


def _going_signup_ids(event_id: int) -> set[str]:
    """discord_ids of members currently `going` — the only members who may lead or
    hold a seat in the plan."""
    return {str(s["discord_id"]) for s in db.list_signups(event_id)
            if s.get("status") == "going"}


def _validate_group(body: GroupIn, event_id: int, going: set[str]) -> dict:
    """Normalize a group payload into the column dict db expects, validating kind,
    an optional same-event parent, and an optional leader who must be `going`."""
    kind = body.kind if body.kind in nav_core.GROUP_KINDS else "squad"
    parent_id = body.parent_id
    if parent_id is not None:
        parent = db.get_event_group(parent_id)
        if parent is None or parent["event_id"] != event_id:
            raise HTTPException(status_code=400, detail="parent group not in this event")
    leader = (body.leader_id or "").strip() or None
    if leader is not None and leader not in going:
        raise HTTPException(status_code=400, detail="leader must be a signed-up member")
    return {
        "name": body.name.strip(), "kind": kind,
        "ship": (body.ship or "").strip() or None,
        "capacity": body.capacity, "parent_id": parent_id,
        "leader_id": leader, "notes": (body.notes or "").strip() or None,
        "sort": body.sort,
    }


def _roster_board_view(ev: dict, user: dict) -> dict:
    """Serialize the plan for an event: the derived board (groups + members +
    unassigned pool) plus the caller's own assignment and edit permission."""
    groups = db.list_event_groups(ev["id"])
    assignments = db.list_event_assignments(ev["id"])
    signups = db.list_signups(ev["id"])
    ids = {str(s["discord_id"]) for s in signups}
    ids.update(str(g["leader_id"]) for g in groups if g.get("leader_id"))
    names = {did: _resolve_member_name(did, None) for did in ids}
    board = nav_core.derive_roster_board(groups, assignments, signups, names)
    board["can_edit"] = ev["organizer_id"] == user["id"] or bool(user.get("is_admin"))
    mine = next((a for a in assignments if str(a["discord_id"]) == user["id"]), None)
    if mine:
        grp = next((g for g in board["groups"] if g["id"] == mine["group_id"]), None)
        board["my_assignment"] = ({"group_id": mine["group_id"],
                                   "group_name": grp["name"] if grp else None,
                                   "slot": mine.get("slot") or ""} if grp else None)
    else:
        board["my_assignment"] = None
    return board


@app.get("/api/events/{event_id}/groups")
async def event_roster(event_id: int, user: dict = Depends(require_session)):
    """The roster board: groups with their assigned members + the unassigned pool.
    Any member may view; only the organizer/admin sees editable controls."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    return _roster_board_view(ev, user)


@app.post("/api/events/{event_id}/groups")
async def create_group(event_id: int, body: GroupIn,
                       user: dict = Depends(require_session)):
    """Add a group (unit) to an event's plan (organizer or admin)."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    _require_event_owner(ev, user)
    if len(db.list_event_groups(event_id)) >= _MAX_EVENT_GROUPS:
        raise HTTPException(status_code=400, detail="too many groups for one event")
    fields = _validate_group(body, event_id, _going_signup_ids(event_id))
    db.create_event_group(event_id, fields)
    return _roster_board_view(ev, user)


@app.patch("/api/events/{event_id}/groups/{gid}")
async def edit_group(event_id: int, gid: int, body: GroupIn,
                     user: dict = Depends(require_session)):
    """Edit a group (organizer or admin) — full replace of its editable fields."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    _require_event_owner(ev, user)
    fields = _validate_group(body, event_id, _going_signup_ids(event_id))
    if not db.update_event_group(event_id, gid, fields):
        raise HTTPException(status_code=404, detail="unknown group")
    return _roster_board_view(ev, user)


@app.delete("/api/events/{event_id}/groups/{gid}")
async def delete_group(event_id: int, gid: int,
                       user: dict = Depends(require_session)):
    """Delete a group (organizer or admin). Its members fall back to the pool."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    _require_event_owner(ev, user)
    if not db.delete_event_group(event_id, gid):
        raise HTTPException(status_code=404, detail="unknown group")
    return _roster_board_view(ev, user)


@app.put("/api/events/{event_id}/assignments")
async def set_assignment(event_id: int, body: AssignmentIn,
                         user: dict = Depends(require_session)):
    """Assign/move a member into a group + seat, or unassign (group_id null).
    Organizer or admin only; the member must be a `going` signup."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    _require_event_owner(ev, user)
    did = body.discord_id.strip()
    if body.group_id is None:
        db.clear_event_assignment(event_id, did)
    else:
        if did not in _going_signup_ids(event_id):
            raise HTTPException(status_code=400, detail="member isn't signed up")
        grp = db.get_event_group(body.group_id)
        if grp is None or grp["event_id"] != event_id:
            raise HTTPException(status_code=400, detail="unknown group")
        slot = (body.slot or "").strip() or None
        db.set_event_assignment(event_id, did, body.group_id, slot,
                                datetime.now(timezone.utc).isoformat())
    return _roster_board_view(ev, user)


@app.get("/api/events/{event_id}/manifest")
async def event_manifest(event_id: int, user: dict = Depends(require_session)):
    """The plan rendered as Discord-flavored markdown (the op order to paste)."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    board = _roster_board_view(ev, user)
    return {"text": nav_core.build_event_manifest(ev, board),
            "can_post": notify.is_configured("events")}


@app.post("/api/events/{event_id}/manifest/post")
async def post_manifest(event_id: int, user: dict = Depends(require_session)):
    """Post the manifest to the org's Discord channel (organizer or admin)."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    _require_event_owner(ev, user)
    if not notify.is_configured("events"):
        raise HTTPException(status_code=400, detail="Discord notifications aren't configured")
    board = _roster_board_view(ev, user)
    text = nav_core.build_event_manifest(ev, board)
    _notify_bg(notify.send("events", f"{text}{_deep_link('#/events')}",
                           dedup_key=None))
    return {"ok": True}


# --- saved group templates (#20 v1.1) --------------------------------------
# Org-shared unit layouts: an organizer snapshots an event's groups (structure
# only, no members) into a named template, then stamps it onto a future event.

_MAX_TEMPLATES = 60          # sanity cap on org-shared templates


class TemplateSaveIn(BaseModel):
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    event_id: int            # the event whose current groups to snapshot


class ApplyTemplateIn(BaseModel):
    template_id: int


def _snapshot_event_groups(event_id: int) -> list[dict]:
    """The reusable structure of an event's plan: each group's name/kind/ship/
    capacity, in board order. Members, leaders, notes and hierarchy are dropped
    — a template is a blank unit layout, not a roster."""
    out = []
    for g in db.list_event_groups(event_id):
        out.append({"name": g.get("name"), "kind": g.get("kind") or "squad",
                    "ship": g.get("ship"), "capacity": g.get("capacity")})
    return out


def _template_to_dict(t: dict, user: dict) -> dict:
    """Serialize a stored template; `groups` is decoded, `can_delete` reflects
    the caller (author or admin)."""
    try:
        groups = json.loads(t.get("groups") or "[]")
    except (ValueError, TypeError):
        groups = []
    return {"id": t["id"], "name": t.get("name"), "groups": groups,
            "group_count": len(groups),
            "can_delete": t.get("created_by") == user["id"] or bool(user.get("is_admin"))}


@app.get("/api/group-templates")
async def list_templates(user: dict = Depends(require_session)):
    """All saved group templates (org-shared)."""
    return [_template_to_dict(t, user) for t in db.list_group_templates()]


@app.post("/api/group-templates")
async def save_template(body: TemplateSaveIn, user: dict = Depends(require_session)):
    """Snapshot an event's current groups into a named, reusable template.
    Organizer or admin of that event (the plan is theirs to reuse)."""
    ev = db.get_event(body.event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    _require_event_owner(ev, user)
    if len(db.list_group_templates()) >= _MAX_TEMPLATES:
        raise HTTPException(status_code=400, detail="too many saved templates")
    groups = _snapshot_event_groups(body.event_id)
    if not groups:
        raise HTTPException(status_code=400, detail="this event has no units to save")
    db.create_group_template(body.name.strip(), json.dumps(groups), user["id"],
                             datetime.now(timezone.utc).isoformat())
    return [_template_to_dict(t, user) for t in db.list_group_templates()]


@app.delete("/api/group-templates/{tid}")
async def delete_template(tid: int, user: dict = Depends(require_session)):
    """Delete a saved template (author or admin)."""
    t = db.get_group_template(tid)
    if t is None:
        raise HTTPException(status_code=404, detail="unknown template")
    if t.get("created_by") != user["id"] and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="not your template")
    db.delete_group_template(tid)
    return [_template_to_dict(x, user) for x in db.list_group_templates()]


@app.post("/api/events/{event_id}/groups/apply-template")
async def apply_template(event_id: int, body: ApplyTemplateIn,
                         user: dict = Depends(require_session)):
    """Stamp a saved template's units onto an event's plan (organizer or admin).
    Groups are appended (existing units are untouched); the event's group cap
    still applies."""
    ev = db.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="unknown event")
    _require_event_owner(ev, user)
    t = db.get_group_template(body.template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="unknown template")
    try:
        tgroups = json.loads(t.get("groups") or "[]")
    except (ValueError, TypeError):
        tgroups = []
    existing = len(db.list_event_groups(event_id))
    if existing + len(tgroups) > _MAX_EVENT_GROUPS:
        raise HTTPException(status_code=400, detail="too many groups for one event")
    for i, g in enumerate(tgroups):
        kind = g.get("kind") if g.get("kind") in nav_core.GROUP_KINDS else "squad"
        cap = g.get("capacity")
        db.create_event_group(event_id, {
            "name": (g.get("name") or "Unit").strip()[:_NAME_MAX], "kind": kind,
            "ship": (g.get("ship") or None), "capacity": cap,
            "sort": existing + i,
        })
    return _roster_board_view(ev, user)


# --- org inventory & goals (shared item catalog) ---------------------------


class CatalogItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    kind: str = Field(default="gear", max_length=_TYPE_MAX)
    unit: str | None = Field(default=None, max_length=_UNIT_MAX)


class InventoryIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=_ITEM_ID_MAX)
    qty: float = Field(ge=0, le=_MAX_QTY)
    location: str = Field(default="", max_length=_LOCATION_MAX)
    note: str | None = Field(default=None, max_length=_NOTE_MAX)
    unit: str | None = Field(default=None, max_length=_UNIT_MAX)


class GoalLineIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=_ITEM_ID_MAX)
    qty_needed: float = Field(ge=0, le=_MAX_QTY)
    unit: str | None = Field(default=None, max_length=_UNIT_MAX)


class SpecInputIn(BaseModel):
    """One per-slot material-quality minimum: the spec-builder slider position for
    that crafting slot, e.g. Emitter (Hadanite) ≥ Q800. Shared by craft requests
    (#25, under attributes.spec.inputs) and blueprint-seeded craft goals (#14.2,
    where it raises the seeded line items' target quality)."""
    slot: str = Field(min_length=1, max_length=40)              # e.g. "Emitter"
    input: str = Field(min_length=1, max_length=40)             # e.g. "Hadanite"
    min_q: int = Field(ge=1, le=1000)


class GoalIn(BaseModel):
    title: str = Field(min_length=1, max_length=_NAME_MAX)
    description: str = Field(default="", max_length=_DESC_MAX)
    priority: int = Field(default=5, ge=1, le=10)
    deadline: str | None = Field(default=None, max_length=_META_MAX)
    status: str | None = Field(default=None, max_length=16)
    line_items: list[GoalLineIn] = Field(default_factory=list, max_length=_MAX_LINE_ITEMS)
    # Personal vs org scope (#14.2) + optional blueprint seed: when `blueprint_key`
    # is given without explicit line_items, the materials manifest for
    # `blueprint_qty` crafts is expanded into the goal's line items server-side.
    # `blueprint_inputs` carries the spec-builder sliders' per-slot quality asks —
    # each seeded line's target quality is the recipe minimum max'd with these.
    visibility: str | None = Field(default=None, max_length=16)
    blueprint_key: str | None = Field(default=None, max_length=_META_MAX)
    blueprint_qty: float = Field(default=1, ge=1, le=_MAX_QTY)
    blueprint_inputs: list[SpecInputIn] = Field(default_factory=list, max_length=12)


_GOAL_VISIBILITY = ("org", "personal")


def _seed_goal_lines(blueprint_key: str, qty: float,
                     input_qs: dict | None = None) -> tuple[list[dict], list[str]]:
    """Expand a blueprint's materials manifest into goal line items, mapping each
    input material to its commodity catalog item. `input_qs` ({slot: q}, from the
    spec-builder sliders) raises each line's target quality above the recipe's own
    minimum. Returns (lines, unmapped) — unmapped names (e.g. a material not yet
    in the commodity feed) are surfaced as a non-fatal warning rather than
    silently dropped. 404 if the recipe is unknown."""
    bp = blueprints_feed.get(blueprint_key)
    if bp is None:
        raise HTTPException(status_code=404, detail="unknown blueprint")

    def resolve(name: str):
        return resolve_catalog_item(f"commodity:{catalog.slug(name)}")

    seeded = nav_core.blueprint_goal_lines(bp, qty, resolve, input_qs)
    lines = [{"item_id": l["item_id"], "item_name": l["item_name"],
              "unit": l["unit"], "qty_needed": l["qty_needed"],
              **({"min_q": l["min_q"]} if l.get("min_q") else {})}
             for l in seeded["lines"][:_MAX_LINE_ITEMS]]
    return lines, seeded["unmapped"]


_CATALOG_BP_CAP = 10


@app.get("/api/catalog")
async def get_catalog(q: str = "", bp: int = 0, user: dict = Depends(require_session)):
    """Item search over the merged catalog (commodity + ship feeds + custom rows),
    debounced from the picker. Empty `q` returns the head of the list. Shared by
    the inventory/goals forms and the marketplace listing form. `bp=1` appends
    matching craftable recipes as `blueprint:` items (#25.1 §11.3) — only the
    marketplace picker asks for them, so crafted goods get shared identity on
    sale/auction listings while staying out of the inventory/goals pickers."""
    items = catalog.search(item_catalog, q, limit=50)
    if bp:
        needle = q.strip().lower()
        hits = []
        for key, rec in blueprints_feed.items():
            name = rec.get("name") or key
            if needle and needle not in f"{name} {key}".lower():
                continue
            hits.append({"item_id": f"blueprint:{key}", "name": name,
                         "kind": "blueprint", "unit": "each",
                         "cat": rec.get("cat")})
        hits.sort(key=lambda r: (r["name"].lower(), r["item_id"]))
        items = items + hits[:_CATALOG_BP_CAP]
    return {"items": items}


@app.post("/api/catalog")
async def add_catalog_item(body: CatalogItemIn, user: dict = Depends(require_session)):
    """Add a custom catalog item (any member) — for anything not in a feed
    (components, FPS gear, …). Feed items already exist and need no entry."""
    kind = body.kind if body.kind in catalog.KINDS else "gear"
    name = body.name.strip()
    unit = (body.unit or "").strip() or catalog.default_unit(kind)
    now = datetime.now(timezone.utc).isoformat()
    cid = db.add_catalog_item(name, kind, unit, user["id"], now)
    refresh_catalog()
    return resolve_catalog_item(f"custom:{cid}")


def _resolve_or_400(item_id: str) -> dict:
    it = resolve_catalog_item(item_id)
    if it is None:
        raise HTTPException(status_code=400, detail=f"unknown item: {item_id}")
    return it


def _enrich_owner_names(entries: list[dict], key: str = "owner_id") -> list[dict]:
    """Stamp a display_name onto rollup/contributor entries keyed by Discord id."""
    for e in entries:
        e["display_name"] = _resolve_member_name(e.get(key), None)
    return entries


def _holding_view(row: dict) -> dict:
    """Enrich a holding with how much of it is committed to goals and what's left
    free. `available = qty - Σ allocations`; a goal contribution can't exceed it."""
    committed = db.committed_for_holding(row["id"])
    return {**row, "committed": committed,
            "available": round(float(row.get("qty") or 0) - committed, 6)}


@app.get("/api/inventory")
async def get_inventory(owner: str | None = None, goal: int | None = None,
                        user: dict = Depends(require_session)):
    """The inventory ledger. `owner=me` lists the caller's own holdings with their
    goal commitments nested (each holding shows committed vs. available); `goal=<id>`
    lists a goal's contributions; with neither, the org-wide rollup (derived
    `SUM(qty) GROUP BY item` over holdings, each counted once — never a stored
    total)."""
    if owner == "me":
        rows = db.list_inventory(owner_id=user["id"])
        by_holding: dict[int, list] = {}
        for a in db.allocations_for_owner(user["id"]):
            by_holding.setdefault(a["inventory_id"], []).append(a)
        out = []
        for r in rows:
            allocs = by_holding.get(r["id"], [])
            committed = sum(float(a["qty"] or 0) for a in allocs)
            out.append({**r, "committed": committed,
                        "available": round(float(r.get("qty") or 0) - committed, 6),
                        "allocations": [{"goal_id": a["goal_id"],
                                         "goal_title": a.get("goal_title"),
                                         "qty": a["qty"]} for a in allocs]})
        return {"scope": "mine", "rows": out}
    if goal is not None:
        rows = db.list_goal_contributions(goal_id=goal)
        return {"scope": "goal", "goal_id": goal,
                "contributors": _enrich_owner_names(
                    nav_core.derive_inventory_rollup(rows))}
    rollup = nav_core.derive_inventory_rollup(db.list_inventory())
    for it in rollup:
        _enrich_owner_names(it["by_owner"])
    return {"scope": "org", "items": rollup}


@app.post("/api/inventory")
async def log_inventory(body: InventoryIn, user: dict = Depends(require_session)):
    """Log/adjust the caller's holding of an item. One row per (owner, item,
    location) — re-logging SETS the quantity. A holding is a general pledge;
    earmarking part of it to a goal is a separate allocation (see the contribute
    endpoint), so what's logged here is never double-counted as a contribution."""
    item = _resolve_or_400(body.item_id)
    unit = catalog.valid_unit(body.unit) or item["unit"]
    now = datetime.now(timezone.utc).isoformat()
    row = db.upsert_inventory(
        user["id"], item["item_id"], item["name"], unit, body.qty,
        body.location.strip() or None, (body.note or "").strip() or None,
        None, now)
    return _holding_view(row)


class InventoryEditIn(BaseModel):
    qty: float = Field(ge=0, le=_MAX_QTY)
    location: str = Field(default="", max_length=_LOCATION_MAX)
    note: str | None = Field(default=None, max_length=_NOTE_MAX)
    unit: str | None = Field(default=None, max_length=_UNIT_MAX)


@app.patch("/api/inventory/{inv_id}")
async def edit_inventory(inv_id: int, body: InventoryEditIn,
                         user: dict = Depends(require_session)):
    """Edit a holding's qty / location / note / unit (owner-or-admin). The item
    can't be changed here (delete + re-add for that). Quantity can't drop below
    what's already committed to goals from this holding."""
    row = db.get_inventory(inv_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown inventory row")
    if row["owner_id"] != user["id"] and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="you can only edit your own holdings")
    committed = db.committed_for_holding(inv_id)
    if body.qty < committed:
        raise HTTPException(
            status_code=400,
            detail=f"{committed:g} is committed to goals; quantity can't go below that")
    fields = {"qty": body.qty,
              "location": body.location.strip() or None,
              "note": (body.note or "").strip() or None,
              "unit": catalog.valid_unit(body.unit) or row.get("unit")}
    db.update_inventory(inv_id, fields, datetime.now(timezone.utc).isoformat())
    return _holding_view(db.get_inventory(inv_id))


@app.delete("/api/inventory/{inv_id}")
async def delete_inventory(inv_id: int, user: dict = Depends(require_session)):
    """Remove a holding (owner-or-admin). Any goal allocations drawn from it are
    withdrawn with it."""
    row = db.get_inventory(inv_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown inventory row")
    if row["owner_id"] != user["id"] and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="you can only remove your own holdings")
    db.delete_inventory(inv_id)
    return {"ok": True}


def _validate_goal(body: GoalIn) -> dict:
    """Validate a goal against the catalog and normalize into db column fields.
    Each line item's id must resolve; its name/unit are stamped from the catalog
    (not trusted from the client) and duplicate items are rejected. When a
    `blueprint_key` is supplied without explicit line items, the recipe's
    materials manifest is expanded into the line items server-side (a "craft
    goal")."""
    title = body.title.strip()
    # None = "unspecified" → create defaults to org; edit preserves the current value.
    visibility = body.visibility if body.visibility in _GOAL_VISIBILITY else None
    unmapped: list[str] = []
    fields: dict = {}
    if body.blueprint_key and not body.line_items:
        # A craft goal: the recipe (at the requested qualities) IS the line items.
        # The spec rides along so edits can restore the sliders; slots the recipe
        # doesn't have are dropped rather than trusted from the client.
        bp = blueprints_feed.get(body.blueprint_key) or {}
        slot_input = {a.get("slot"): a.get("input")
                      for a in bp.get("aspects") or [] if a.get("input")}
        inputs = [{"slot": i.slot.strip(), "input": slot_input[i.slot.strip()],
                   "min_q": int(i.min_q)}
                  for i in body.blueprint_inputs if i.slot.strip() in slot_input]
        input_qs = {i["slot"]: i["min_q"] for i in inputs}
        lines, unmapped = _seed_goal_lines(body.blueprint_key, body.blueprint_qty,
                                           input_qs)
        fields = {"blueprint_key": body.blueprint_key,
                  "blueprint_qty": float(body.blueprint_qty),
                  "blueprint_inputs": inputs}
    else:
        # Hand-entered lines: any blueprint fields on the body are ignored and the
        # stored ones left untouched (an existing craft goal keeps its tag).
        lines, seen = [], set()
        for li in body.line_items:
            item = _resolve_or_400(li.item_id)
            if item["item_id"] in seen:
                raise HTTPException(status_code=400, detail=f"duplicate line item: {item['name']}")
            seen.add(item["item_id"])
            unit = catalog.valid_unit(li.unit) or item["unit"]
            lines.append({"item_id": item["item_id"], "item_name": item["name"],
                          "unit": unit, "qty_needed": li.qty_needed})
    if not lines:
        raise HTTPException(status_code=400, detail="add at least one line item")
    deadline = None
    if body.deadline:
        deadline = _normalize_event_start(body.deadline)   # reuse UTC canonicalizer
    return {**fields, "title": title, "description": (body.description or "").strip(),
            "priority": body.priority, "deadline": deadline, "line_items": lines,
            "visibility": visibility, "seed_unmapped": unmapped}


def _require_goal_owner(goal: dict, user: dict) -> None:
    if goal["creator_id"] != user["id"] and not user.get("is_admin"):
        raise HTTPException(status_code=403,
                            detail="only the creator or an admin can change this goal")


def _can_view_goal(goal: dict, user: dict) -> bool:
    """Org goals are public; a personal goal is visible only to its creator (or an
    admin)."""
    if goal.get("visibility") != "personal":
        return True
    return goal["creator_id"] == user["id"] or bool(user.get("is_admin"))


def _goal_craft_block(goal: dict, detail: bool = False) -> dict | None:
    """The craft-goal header for a blueprint-seeded goal: recipe name + craft time
    for the card, plus (in detail) each line's demanded min-quality so the UI can
    badge it, the saved spec (craft count + per-slot quality asks, restoring the
    edit form's sliders) and the expected finished stats at those qualities.
    Degrades to `available: False` if the recipe left the feed on a game-patch
    re-sync (the goal keeps working on its denormalized line items)."""
    key = goal.get("blueprint_key")
    if not key:
        return None
    inputs = goal.get("blueprint_inputs") or []
    bp = blueprints_feed.get(key)
    if bp is None:
        return {"blueprint_key": key, "available": False,
                "qty": goal.get("blueprint_qty") or 1, "inputs": inputs}
    block = {"blueprint_key": key, "available": True, "name": bp.get("name"),
             "cat": bp.get("cat"), "time_s": bp.get("time_s"),
             "default": bp.get("default"),
             "qty": goal.get("blueprint_qty") or 1, "inputs": inputs}
    if detail:
        # Per-line target quality: stored on the seeded lines (recipe minimum
        # max'd with the member's asks); re-derived from the recipe for goals
        # saved before min_q was denormalized.
        minq = {l["item_id"]: l["min_q"]
                for l in (goal.get("line_items") or []) if l.get("min_q")}
        if not minq:
            def resolve(name):
                return resolve_catalog_item(f"commodity:{catalog.slug(name)}")
            seeded = nav_core.blueprint_goal_lines(bp, 1, resolve)
            minq = {l["item_id"]: l["min_q"]
                    for l in seeded["lines"] if l.get("min_q")}
        block["min_q"] = minq
        block["unlocks"] = bp.get("unlocks") or []
        block["est_cost"] = _blueprint_est_cost(bp)   # per craft; UI scales by qty
        if inputs:
            block["stat_preview"] = nav_core.blueprint_stat_preview(
                bp, {i["slot"]: i["min_q"] for i in inputs})
    return block


def _goal_view(goal: dict, contributions, user: dict, detail: bool = False) -> dict:
    """Serialize a goal with its derived progress. `contributions` is the list of
    inventory rows earmarked to it. Auto-flips a fully-covered active goal to
    'met' (and a no-longer-met one back to active) lazily on read — display state
    follows the ledger without a background job; an admin can still archive."""
    progress = nav_core.derive_goal_progress(goal, contributions)
    status = goal.get("status")
    if status in ("active", "met"):
        target = "met" if progress["is_met"] else "active"
        if target != status:
            db.set_goal_status(goal["id"], target, datetime.now(timezone.utc).isoformat())
            status = target
    view = {k: goal.get(k) for k in
            ("id", "creator_id", "title", "description", "priority", "deadline",
             "line_items", "created_at", "updated_at")}
    view["status"] = status
    view["visibility"] = goal.get("visibility") or "org"
    view["blueprint_key"] = goal.get("blueprint_key")
    view["creator_name"] = _resolve_member_name(goal["creator_id"], None)
    view["is_mine"] = goal["creator_id"] == user["id"]
    view["can_edit"] = goal["creator_id"] == user["id"] or bool(user.get("is_admin"))
    view["progress"] = progress
    craft = _goal_craft_block(goal, detail=detail)
    if craft:
        view["craft"] = craft
    if detail:
        view["progress"]["per_contributor"] = _enrich_owner_names(
            progress["per_contributor"])
        # "Where my materials are": the caller's own committed holdings grouped by
        # location, so a craft-goal shows what's staged where.
        locs: dict[str, list] = {}
        for c in contributions:
            if c.get("owner_id") != user["id"]:
                continue
            locs.setdefault(c.get("location") or "Unspecified", []).append(
                {"name": c.get("item_name"), "qty": c.get("qty"), "unit": c.get("unit")})
        view["my_locations"] = [{"location": k, "items": v} for k, v in locs.items()]
    return view


@app.get("/api/goals")
async def list_goals(status: str | None = None, user: dict = Depends(require_session)):
    """The goals board, sorted priority↑ then deadline↑. Each carries its derived
    overall fill so cards render without an N+1 (contributions are fetched once
    and grouped by goal)."""
    goals = db.list_goals(status, viewer_id=user["id"])
    contributions: dict[int, list] = {}
    for row in db.list_goal_contributions():
        contributions.setdefault(row["goal_id"], []).append(row)
    return {"goals": [_goal_view(g, contributions.get(g["id"], []), user) for g in goals]}


@app.post("/api/goals")
async def create_goal(body: GoalIn, user: dict = Depends(require_session)):
    """Create a procurement goal (any org member). Seed a "craft goal" by passing
    `blueprint_key` (+ optional `blueprint_qty`) with no line items: the recipe's
    materials manifest becomes the line items. `visibility` scopes it org|personal."""
    fields = _validate_goal(body)
    now = datetime.now(timezone.utc).isoformat()
    gid = db.create_goal({**fields, "creator_id": user["id"], "status": "active",
                          "created_at": now, "updated_at": now})
    goal = db.get_goal(gid)
    view = _goal_view(goal, db.list_goal_contributions(goal_id=gid), user, detail=True)
    if fields.get("seed_unmapped"):
        view["seed_unmapped"] = fields["seed_unmapped"]   # materials with no catalog match
    return view


@app.get("/api/goals/{goal_id}")
async def get_goal(goal_id: int, user: dict = Depends(require_session)):
    """One goal with per-line fill + the per-contributor breakdown."""
    goal = db.get_goal(goal_id)
    if goal is None or not _can_view_goal(goal, user):
        raise HTTPException(status_code=404, detail="unknown goal")
    return _goal_view(goal, db.list_goal_contributions(goal_id=goal_id), user, detail=True)


@app.patch("/api/goals/{goal_id}")
async def edit_goal(goal_id: int, body: GoalIn, user: dict = Depends(require_session)):
    """Edit a goal (creator or admin) — full replace of the editable fields. A
    `status` of 'archived' parks it; 'active' reopens it (the met/active flip is
    otherwise automatic)."""
    goal = db.get_goal(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="unknown goal")
    _require_goal_owner(goal, user)
    fields = _validate_goal(body)
    if fields.get("visibility") is None:
        fields.pop("visibility", None)     # unspecified → keep the goal's current scope
    if body.status in ("active", "met", "archived"):
        fields["status"] = body.status
    db.update_goal(goal_id, fields, datetime.now(timezone.utc).isoformat())
    goal = db.get_goal(goal_id)
    return _goal_view(goal, db.list_goal_contributions(goal_id=goal_id), user, detail=True)


@app.delete("/api/goals/{goal_id}")
async def remove_goal(goal_id: int, user: dict = Depends(require_session)):
    """Delete a goal (creator or admin). The parent holdings survive as general
    inventory; only this goal's allocations against them are dropped."""
    goal = db.get_goal(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="unknown goal")
    _require_goal_owner(goal, user)
    db.delete_goal(goal_id)
    return {"ok": True}


class ContributeIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=_ITEM_ID_MAX)
    qty: float = Field(gt=0, le=_MAX_QTY)
    location: str = Field(default="", max_length=_LOCATION_MAX)


@app.post("/api/goals/{goal_id}/contribute")
async def contribute_to_goal(goal_id: int, body: ContributeIn,
                             user: dict = Depends(require_session)):
    """Commit some of the caller's holding toward a goal. The commitment is an
    *allocation* drawn from the member's (item, location) holding — not a duplicate
    ledger row — so it never double-counts in the org rollup. If the holding
    doesn't hold enough free quantity, it's topped up to cover the commitment (the
    member is declaring they have at least that much on hand)."""
    goal = db.get_goal(goal_id)
    if goal is None or not _can_view_goal(goal, user):
        raise HTTPException(status_code=404, detail="unknown goal")
    item = _resolve_or_400(body.item_id)
    loc = body.location.strip() or None
    now = datetime.now(timezone.utc).isoformat()
    was_met = nav_core.derive_goal_progress(
        goal, db.list_goal_contributions(goal_id=goal_id))["is_met"]
    holding = db.get_holding(user["id"], item["item_id"], loc)
    if holding is None:
        # No matching holding yet: the contribution itself declares the holding.
        holding = db.upsert_inventory(
            user["id"], item["item_id"], item["name"], item["unit"], body.qty,
            loc, None, None, now)
    existing = db.find_allocation(holding["id"], goal_id)
    # Free quantity must cover the (new portion of the) commitment; bump the
    # holding up if the member is committing more than they'd declared on hand.
    committed = db.committed_for_holding(holding["id"])
    new_committed = committed - float((existing or {}).get("qty") or 0) + body.qty
    if new_committed > float(holding["qty"] or 0):
        db.update_inventory(holding["id"], {"qty": new_committed}, now)
    if existing:
        db.update_allocation(existing["id"], float(existing["qty"] or 0) + body.qty, now)
    else:
        db.add_allocation(holding["id"], goal_id, body.qty, now)
    contributions = db.list_goal_contributions(goal_id=goal_id)
    # Ping when this contribution is the one that tips the goal to 100% (and it
    # wasn't already met, and it's not an archived/parked goal). Personal goals
    # stay private — no org-wide broadcast.
    if (not was_met and goal.get("status") != "archived"
            and goal.get("visibility") != "personal"
            and nav_core.derive_goal_progress(goal, contributions)["is_met"]):
        _notify_bg(_notify_goal_met(goal, contributions))
    return _goal_view(db.get_goal(goal_id), contributions, user, detail=True)


# --- org marketplace (shares the item catalog) -----------------------------
# A coordination board, not an exchange: SC has no auction house or item API, so
# the app only records that two members agreed on terms — the goods + aUEC move
# in-game on trust. aUEC ONLY, never real money (fan project under CIG's IP).


_LISTING_MODES = ("sale", "auction", "barter", "commission")

# Who sources a commission's input materials — changes the price of the job more
# than anything else, so it's first-class (column + board chip), not note text.
_COMMISSION_MATERIALS = ("requester", "crafter", "split")


class CraftStatIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)              # e.g. "Power output"
    value: str = Field(min_length=1, max_length=40)            # free-form, e.g. "12.5 MW"


class CraftedIn(BaseModel):
    """Optional crafted-item quality annotation (SC 4.8 crafting). A crafted
    component's quality rides from its materials (1–1000, in 8 bands) into its
    per-stat values, so a seller can advertise an overall quality and/or band plus
    a few finished-stat rows. Stored as a free-form JSON blob — no fixed schema, so
    it survives whatever the in-game model turns out to expose. `inputs` carries a
    craft request's per-slot material minimums (commission spec builder)."""
    quality: int | None = Field(default=None, ge=1, le=1000)
    band: int | None = Field(default=None, ge=1, le=8)
    stats: list[CraftStatIn] = Field(default_factory=list, max_length=12)
    inputs: list[SpecInputIn] = Field(default_factory=list, max_length=12)


class ListingIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=_ITEM_ID_MAX)
    qty: float = Field(default=1, gt=0, le=_MAX_QTY)
    mode: str = Field(default="sale", max_length=16)            # sale | auction | barter
    price_auec: float | None = Field(default=None, ge=0, le=_MAX_QTY)    # sale
    start_price: float | None = Field(default=None, ge=0, le=_MAX_QTY)   # auction
    buyout_auec: float | None = Field(default=None, ge=0, le=_MAX_QTY)   # auction
    ends_at: str | None = Field(default=None, max_length=_META_MAX)      # auction
    want: str | None = Field(default=None, max_length=_NOTE_MAX)         # barter
    note: str | None = Field(default=None, max_length=_NOTE_MAX)
    unit: str | None = Field(default=None, max_length=_UNIT_MAX)
    crafted: CraftedIn | None = None                            # crafted-quality blob
    seller_handle: str | None = Field(default=None, max_length=_HANDLE_MAX)  # in-game meetup name
    materials: str | None = Field(default=None, max_length=16)  # commission: requester|crafter|split
    announce: bool = False                                      # commission: Discord shout


class ListingPatchIn(BaseModel):
    status: str | None = Field(default=None, max_length=16)     # 'cancelled' only
    qty: float | None = Field(default=None, gt=0, le=_MAX_QTY)
    price_auec: float | None = Field(default=None, ge=0, le=_MAX_QTY)
    buyout_auec: float | None = Field(default=None, ge=0, le=_MAX_QTY)
    ends_at: str | None = Field(default=None, max_length=_META_MAX)
    want: str | None = Field(default=None, max_length=_NOTE_MAX)
    note: str | None = Field(default=None, max_length=_NOTE_MAX)
    crafted: CraftedIn | None = None       # present (even if empty) ⇒ replace/clear
    materials: str | None = Field(default=None, max_length=16)  # commission only


class OfferIn(BaseModel):
    amount_auec: float | None = Field(default=None, ge=0, le=_MAX_QTY)   # sale/auction
    offer_item_id: str | None = Field(default=None, max_length=_ITEM_ID_MAX)  # barter
    offer_note: str | None = Field(default=None, max_length=_NOTE_MAX)


class OfferActionIn(BaseModel):
    action: str = Field(max_length=16)                         # accept | withdraw


_LISTING_PUBLIC = ("id", "seller_id", "seller_handle", "item_id", "item_name", "unit",
                   "qty", "mode", "price_auec", "start_price", "buyout_auec", "ends_at",
                   "want", "status", "note", "buyer_id", "seller_confirmed",
                   "buyer_confirmed", "attributes", "blueprint_key", "materials",
                   "created_at", "updated_at", "completed_at")

# The columns a board card needs — a subset of _LISTING_PUBLIC, served without the
# per-listing offer query (auction high-bid/count ride the denormalized columns).
# `attributes` rides along so a card can show the "Crafted · Qn" badge.
_LISTING_CARD = ("id", "seller_id", "seller_handle", "item_id", "item_name", "unit",
                 "qty", "mode", "price_auec", "start_price", "ends_at", "want",
                 "status", "sort_price", "offer_count", "attributes",
                 "blueprint_key", "materials", "created_at")


def _clean_crafted(crafted: "CraftedIn | None") -> dict | None:
    """Normalize a crafted-quality annotation into the stored JSON blob, or None if
    it carries nothing. Drops blank stat rows; keeps quality/band only when set.
    `inputs` (per-slot material minimums from the commission spec builder) ride
    along so the crafter can see which material qualities to source."""
    if crafted is None:
        return None
    out: dict = {}
    if crafted.quality is not None:
        out["quality"] = int(crafted.quality)
    if crafted.band is not None:
        out["band"] = int(crafted.band)
    stats = [{"name": s.name.strip(), "value": s.value.strip()}
             for s in (crafted.stats or []) if s.name.strip() and s.value.strip()]
    if stats:
        out["stats"] = stats
    inputs = [{"slot": i.slot.strip(), "input": i.input.strip(), "min_q": int(i.min_q)}
              for i in (crafted.inputs or []) if i.slot.strip() and i.input.strip()]
    if inputs:
        out["inputs"] = inputs
    return out or None

_MARKET_PAGE = 25          # default board page size
_MARKET_PAGE_MAX = 100     # cap a client-supplied ?limit=


def _validate_listing(body: ListingIn) -> dict:
    """Validate a new listing against the catalog + its mode, normalizing into the
    db column fields. The item's name/unit are stamped from the catalog (not
    trusted from the client). Each mode requires its own fields; aUEC only."""
    item = _resolve_or_400(body.item_id)
    if body.mode not in _LISTING_MODES:
        raise HTTPException(status_code=400, detail=f"unknown listing mode: {body.mode}")
    fields = {"item_id": item["item_id"], "item_name": item["name"],
              "unit": catalog.valid_unit(body.unit) or item["unit"],
              "qty": body.qty, "mode": body.mode,
              "note": (body.note or "").strip() or None,
              "attributes": _clean_crafted(body.crafted),
              "price_auec": None, "start_price": None, "buyout_auec": None,
              "ends_at": None, "want": None, "blueprint_key": None, "materials": None}
    # Any listing whose item is a craftable recipe carries its blueprint_key
    # (#25.1 §11.3) — on sale/auction it gives the crafted good shared identity
    # (exact-item search, kind=blueprint filter, expected-stats panel), not just
    # on commissions.
    if item["item_id"].startswith("blueprint:"):
        fields["blueprint_key"] = item["item_id"][len("blueprint:"):]
    if body.mode == "sale":
        if body.price_auec is None:
            raise HTTPException(status_code=400, detail="a sale listing needs a price")
        fields["price_auec"] = float(body.price_auec)
    elif body.mode == "commission":
        # A craft request (#25): the item IS a blueprint; budget is optional
        # ("open to quotes"); ends_at is an optional needed-by date; the quality
        # spec rides under attributes.spec so a future as-delivered annotation
        # can sit beside it.
        if not item["item_id"].startswith("blueprint:"):
            raise HTTPException(status_code=400,
                                detail="a craft request needs a blueprint item")
        materials = (body.materials or "crafter").strip().lower()
        if materials not in _COMMISSION_MATERIALS:
            raise HTTPException(status_code=400,
                                detail="materials must be requester, crafter or split")
        fields["materials"] = materials
        if body.price_auec is not None:
            fields["price_auec"] = float(body.price_auec)
        if body.ends_at:
            ends_at = _normalize_event_start(body.ends_at)
            if datetime.fromisoformat(ends_at) <= datetime.now(timezone.utc):
                raise HTTPException(status_code=400,
                                    detail="the needed-by date must be in the future")
            fields["ends_at"] = ends_at
        spec = _clean_crafted(body.crafted)
        fields["attributes"] = {"spec": spec} if spec else None
    elif body.mode == "auction":
        if body.start_price is None:
            raise HTTPException(status_code=400, detail="an auction needs a start price")
        if not body.ends_at:
            raise HTTPException(status_code=400, detail="an auction needs an end time")
        ends_at = _normalize_event_start(body.ends_at)       # reuse UTC canonicalizer
        if datetime.fromisoformat(ends_at) <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="auction end time must be in the future")
        fields["start_price"] = float(body.start_price)
        fields["ends_at"] = ends_at
        if body.buyout_auec is not None:
            if float(body.buyout_auec) < float(body.start_price):
                raise HTTPException(status_code=400,
                                    detail="buyout must be at or above the start price")
            fields["buyout_auec"] = float(body.buyout_auec)
    else:   # barter
        want = (body.want or "").strip()
        if not want:
            raise HTTPException(status_code=400, detail="a barter listing needs what you want in return")
        fields["want"] = want
    return fields


def _resolve_listing_expiry(listing: dict, offers, now: datetime) -> dict:
    """Lazily settle a closed auction on read — no background job. An open auction
    past its end (or one a buyout has cleared) becomes `pending` with the winning
    bidder (their bid accepted, the rest marked lost), or `expired` if nobody bid.
    A commission past its needed-by date just goes `expired` — no winner
    derivation, the requester picks quotes manually while it's open. Other modes
    are returned unchanged. Returns the (reloaded) listing."""
    if listing["status"] != "open":
        return listing
    if listing["mode"] == "commission":
        if _auction_time_up(listing, now):
            db.set_listing_status(listing["id"], "expired", now.isoformat())
            return db.get_listing(listing["id"])
        return listing
    if listing["mode"] != "auction":
        return listing
    state = nav_core.derive_auction_state(listing, offers, now)
    if not state["is_closed"]:
        return listing
    ts = now.isoformat()
    if state["winner_id"]:
        win = next((o for o in offers if o["bidder_id"] == state["winner_id"]
                    and float(o.get("amount_auec") or 0) == state["winning_amount"]), None)
        db.settle_listing(listing["id"], state["winner_id"], "pending", ts)
        if win:
            db.set_offer_status(win["id"], "accepted")
            db.reject_other_offers(listing["id"], win["id"])
    else:
        db.set_listing_status(listing["id"], "expired", ts)
    return db.get_listing(listing["id"])


def _commission_mats_est(listing: dict) -> float | None:
    """A craft request's estimated total materials cost (#25.1 §12): the recipe's
    per-craft estimate × the requested quantity. None when the recipe left the
    feed or no input has a price reference — the UI simply omits the hint.
    Pure in-memory math (feed + price map), so cards can carry it without a
    query."""
    key = listing.get("blueprint_key")
    bp = blueprints_feed.get(key) if key else None
    if bp is None:
        return None
    cost = _blueprint_est_cost(bp)
    if cost["total"] is None:
        return None
    return round(cost["total"] * max(float(listing.get("qty") or 1), 1))


def _listing_view(listing: dict, user: dict, detail: bool = False) -> dict:
    """Serialize a listing: its fields, the seller's name + completed-deals count,
    the derived auction state (auctions), the caller's own standing offer, and the
    caller's permissions. `detail=True` adds the full offer/bid list. Lazily
    settles a lapsed auction first so the board never shows a stale 'open'."""
    now = datetime.now(timezone.utc)
    listing = _resolve_listing_expiry(listing, db.list_offers(listing["id"]), now)
    offers = db.list_offers(listing["id"])
    state = nav_core.derive_auction_state(listing, offers, now)
    view = {k: listing.get(k) for k in _LISTING_PUBLIC}
    view["seller_name"] = _resolve_member_name(listing["seller_id"], None)
    # Verified iff the meetup handle is currently bound to the seller (live, so a
    # listing typed before the seller ran the watcher self-upgrades once it binds).
    view["handle_verified"] = handles.owns_handle(
        listing["seller_id"], listing.get("seller_handle"))
    view["seller_deals"] = db.completed_deals_count(listing["seller_id"])
    view["is_seller"] = listing["seller_id"] == user["id"]
    view["can_edit"] = view["is_seller"] or bool(user.get("is_admin"))
    view["auction"] = state if listing["mode"] == "auction" else None
    if listing["mode"] == "commission":
        comm = nav_core.commission_board_state(listing, offers)
        key = listing.get("blueprint_key")
        comm["can_craft_count"] = db.blueprint_crafter_counts([key]).get(key, 0) if key else 0
        comm["i_can_craft"] = bool(key and db.member_has_blueprint(user["id"], key))
        comm["mats_est"] = _commission_mats_est(listing)
        view["commission"] = comm
    else:
        view["commission"] = None
    view["offer_count"] = state["bid_count"]
    mine = next((o for o in offers
                 if o["bidder_id"] == user["id"] and o["status"] == "active"), None)
    view["my_offer"] = ({"id": mine["id"], "amount_auec": mine["amount_auec"],
                         "offer_item_name": mine.get("offer_item_name"),
                         "offer_note": mine.get("offer_note")} if mine else None)
    if listing.get("buyer_id"):
        view["buyer_name"] = _resolve_member_name(listing["buyer_id"], None)
        # The buyer's meetup handle so the seller knows who to look for in-game
        # (and vice-versa) once a deal is pending.
        view["buyer_handle"] = _meetup_handle(listing["buyer_id"])
        view["is_buyer"] = listing["buyer_id"] == user["id"]
        view["can_confirm"] = (listing["status"] == "pending"
                               and (view["is_seller"] or view["is_buyer"]))
    else:
        view["buyer_name"], view["is_buyer"], view["can_confirm"] = None, False, False
        view["buyer_handle"] = None
    if detail:
        view["offers"] = [
            {"id": o["id"], "bidder_id": o["bidder_id"],
             "bidder_name": _resolve_member_name(o["bidder_id"], None),
             "amount_auec": o["amount_auec"], "offer_item_id": o.get("offer_item_id"),
             "offer_item_name": o.get("offer_item_name"), "offer_note": o.get("offer_note"),
             "status": o["status"], "created_at": o["created_at"],
             "is_mine": o["bidder_id"] == user["id"]}
            for o in offers]
        view["expected_stats"] = _listing_expected_stats(listing)
        if listing["mode"] != "commission" and listing.get("blueprint_key"):
            # Fair-price anchor for buyers of a crafted good (commissions carry
            # theirs under the commission block).
            view["mats_est"] = _commission_mats_est(listing)
    return view


def _listing_expected_stats(listing: dict) -> dict | None:
    """The expected finished stats of a blueprint-linked listing (#25.1 §11.4).
    A commission with per-slot quality asks previews at exactly those (`basis:
    "inputs"`); otherwise an advertised overall quality previews every slot at
    that number (`basis: "uniform"` — a stated assumption, since the game derives
    stats per input slot). None when there's no recipe, no quality signal, or the
    recipe left the feed."""
    key = listing.get("blueprint_key")
    bp = blueprints_feed.get(key) if key else None
    if bp is None:
        return None
    attrs = listing.get("attributes") or {}
    spec = attrs.get("spec") or {}
    inputs = spec.get("inputs") or []
    if inputs:
        stats = nav_core.blueprint_stat_preview(
            bp, {i["slot"]: i["min_q"] for i in inputs})
        return {"basis": "inputs", "stats": stats} if stats else None
    quality = spec.get("quality") if listing.get("mode") == "commission" \
        else attrs.get("quality")
    if quality is None:
        return None
    q = float(quality)
    stats = nav_core.blueprint_stat_preview(
        bp, {a.get("slot"): q for a in bp.get("aspects") or []})
    return {"basis": "uniform", "quality": quality, "stats": stats} if stats else None


def _auction_time_up(listing: dict, now: datetime) -> bool:
    """Whether an auction's clock has run out (a buyout is settled synchronously at
    bid time, so time expiry is the only state the board has to resolve lazily)."""
    ends = listing.get("ends_at")
    if not ends:
        return False
    try:
        return now >= datetime.fromisoformat(ends)
    except ValueError:
        return False


def _listing_card(listing: dict, user: dict, deals: dict,
                  craft_counts: dict | None = None, my_bps: set | None = None) -> dict:
    """Lightweight board serializer: only the columns a card draws, with no
    per-listing offer query. An auction's high bid / bid count come from the
    denormalized `sort_price` / `offer_count` (kept current by
    db.refresh_listing_denorm), so the board read is pure SQL. Commission cards
    also carry crafter matching (#25.1): how many members can craft the recipe and
    whether the caller can — from the member blueprint library."""
    card = {k: listing.get(k) for k in _LISTING_CARD}
    card["seller_name"] = _resolve_member_name(listing["seller_id"], None)
    card["handle_verified"] = handles.owns_handle(
        listing["seller_id"], listing.get("seller_handle"))
    card["seller_deals"] = deals.get(str(listing["seller_id"]), 0)
    card["is_seller"] = listing["seller_id"] == user["id"]
    if listing["mode"] == "auction":
        cnt = int(listing.get("offer_count") or 0)
        # sort_price is the high bid once anyone has bid, else the start price.
        card["auction"] = {"high_bid": listing.get("sort_price") if cnt else None,
                           "bid_count": cnt}
    else:
        card["auction"] = None
    if listing["mode"] == "commission":
        cnt = int(listing.get("offer_count") or 0)
        key = listing.get("blueprint_key")
        # sort_price is the best (lowest) quote once anyone has quoted, else the
        # requester's budget (may be NULL = open to quotes).
        card["commission"] = {
            "best_quote": listing.get("sort_price") if cnt else None,
            "quote_count": cnt, "budget": listing.get("price_auec"),
            "can_craft_count": (craft_counts or {}).get(key, 0),
            "i_can_craft": bool(key and my_bps and key in my_bps),
            "mats_est": _commission_mats_est(listing)}
    else:
        card["commission"] = None
    return card


@app.get("/api/market")
async def list_market(mode: str | None = None, item: str | None = None,
                      seller: str | None = None, q: str | None = None,
                      kind: str | None = None, min_price: float | None = None,
                      max_price: float | None = None, sort: str = "recent",
                      limit: int = _MARKET_PAGE, offset: int = 0,
                      min_quality: float | None = None, max_quality: float | None = None,
                      band: int | None = None, stat: str | None = None,
                      user: dict = Depends(require_session)):
    """The marketplace board — a paged, filterable, sortable slice of listings.
    Defaults to open listings; `seller=me` lists all of the caller's own (any
    status), `seller=<id>` another member's open listings. Filters: `mode`, exact
    `item`, free-text `q` over the item name, `kind` (commodity/ship/item/custom),
    a `min_price`/`max_price` band, and the crafted-quality filters
    `min_quality`/`max_quality`/`band`/`stat`. `sort` ∈ recent|oldest|price_asc|
    price_desc|ending. Returns a lightweight card per listing (no N+1) plus `total`
    for paging. A lapsed auction the page surfaces is settled lazily here (per-page,
    so only lapsed ones touch their offers) and then drops off the open board."""
    limit = max(1, min(int(limit), _MARKET_PAGE_MAX))
    offset = max(0, int(offset))
    mine = seller == "me"
    rows, total = db.list_listings(
        mode=mode, item_id=item, seller_id=user["id"] if mine else seller,
        open_only=not mine, q=q, kind=kind, min_price=min_price,
        max_price=max_price, sort=sort, limit=limit, offset=offset,
        min_quality=min_quality, max_quality=max_quality, band=band, stat=stat)
    now = datetime.now(timezone.utc)
    cards = []
    for r in rows:
        if (r["mode"] in ("auction", "commission") and r["status"] == "open"
                and _auction_time_up(r, now)):
            r = _resolve_listing_expiry(r, db.list_offers(r["id"]), now)
            if not mine and r["status"] != "open":   # left the open board
                total -= 1
                continue
        cards.append(r)
    deals = db.completed_deals_counts([r["seller_id"] for r in cards])
    # Crafter matching for commission cards (#25.1): count members who can craft
    # each visible recipe (one grouped query) + the caller's own library.
    bp_keys = [r["blueprint_key"] for r in cards
               if r["mode"] == "commission" and r.get("blueprint_key")]
    craft_counts = db.blueprint_crafter_counts(bp_keys) if bp_keys else {}
    my_bps = ({b["blueprint_key"] for b in db.list_member_blueprints(user["id"])}
              if bp_keys else set())
    return {"listings": [_listing_card(r, user, deals, craft_counts, my_bps)
                         for r in cards],
            "total": total, "limit": limit, "offset": offset,
            # Whether the commission form should offer the Discord announce
            # checkbox (mirrors the LFG board's announce_available flag).
            "announce_available": notify.is_configured("marketplace")}


def _meetup_handle(discord_id: str) -> str | None:
    """The in-game handle to show as a member's meetup name: their chosen primary
    handle, else the most-recent watcher-bound one, else None."""
    return _member_identity(discord_id)["primary_handle"]


@app.post("/api/market")
async def create_listing(body: ListingIn, user: dict = Depends(require_session)):
    """Post a listing (any org member). aUEC only — never real money. The listing
    carries a `seller_handle` (the in-game meetup name): the seller's typed value,
    else their primary handle. It may be unverified — `handle_verified` is computed
    live at read time from the handle registry, so it self-upgrades once a watcher
    binds the handle."""
    fields = _validate_listing(body)
    handle = (body.seller_handle or "").strip() or _meetup_handle(user["id"])
    now = datetime.now(timezone.utc).isoformat()
    lid = db.create_listing({**fields, "seller_id": user["id"], "seller_handle": handle,
                             "status": "open", "created_at": now, "updated_at": now})
    # Opt-in Discord shout for craft requests (#25) — rate-limited per member so
    # the channel never floods; silently skipped when webhooks aren't configured.
    if (body.announce and fields["mode"] == "commission"
            and notify.is_configured("marketplace")
            and _commission_announce_ok(user["id"])):
        _notify_bg(_notify_commission_posted(db.get_listing(lid)))
    return _listing_view(db.get_listing(lid), user, detail=True)


# NB: must be declared BEFORE /api/market/{listing_id} — FastAPI matches in order,
# and "stats" would otherwise bind to {listing_id:int} and 422.
@app.get("/api/market/stats")
async def market_stats(range: str = "all", user: dict = Depends(require_session)):
    """Guild marketplace statistics for the Org Intel Market section: confirmed-
    deal totals (aUEC volume, items moved, deal + trader counts), the top sellers
    by aUEC, the most-traded items, and a weekly aUEC-volume sparkline. Only
    completed deals count — expired/cancelled ads never appear, so this measures
    confirmed trades. `range=week` scopes the totals/breakdowns to the trailing 7
    days; the sparkline always spans the trailing weeks so the trend stays read."""
    listings = db.list_completed_listings(_cargo_window_start(range))
    stats = nav_core.derive_market_stats(listings)
    for s in stats["top_sellers"]:
        s["display_name"] = _resolve_member_name(s["discord_id"], None)
        s["mine"] = s["discord_id"] == user["id"]
    # Weekly aUEC volume (mirrors cargo/stats' sparkline). Always all-time so the
    # trend doesn't collapse to a single bar under the 'week' range.
    spark = listings if range != "week" else db.list_completed_listings(None)
    weeks: Counter = Counter()
    for lst in spark:
        ts = lst.get("completed_at")
        amt = lst.get("final_auec")
        if not ts or not amt:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        weeks[_iso_week_start(dt)] += float(amt)
    activity = []
    if weeks:
        end = _iso_week_start(datetime.now(timezone.utc))
        start = end - timedelta(weeks=_STATS_WEEKS - 1)
        wk = start
        while wk <= end:
            activity.append({"label": wk.strftime("%b %d"),
                             "count": round(weeks.get(wk, 0), 2)})
            wk += timedelta(weeks=1)
    return {"range": range, **stats, "activity": activity}


@app.get("/api/market/{listing_id}")
async def get_market_listing(listing_id: int, user: dict = Depends(require_session)):
    """One listing with its offer/bid list + derived auction state."""
    listing = db.get_listing(listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="unknown listing")
    return _listing_view(listing, user, detail=True)


@app.patch("/api/market/{listing_id}")
async def edit_listing(listing_id: int, body: ListingPatchIn,
                       user: dict = Depends(require_session)):
    """Edit or cancel a listing (seller or admin). `status='cancelled'` calls it
    off (allowed before it completes); otherwise the mode's own fields can be
    tweaked while it's still open."""
    listing = db.get_listing(listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="unknown listing")
    if listing["seller_id"] != user["id"] and not user.get("is_admin"):
        raise HTTPException(status_code=403,
                            detail="only the seller or an admin can change this listing")
    ts = datetime.now(timezone.utc).isoformat()
    if body.status is not None:
        if body.status != "cancelled":
            raise HTTPException(status_code=400, detail="the only status you can set is 'cancelled'")
        if listing["status"] in ("completed", "cancelled"):
            raise HTTPException(status_code=400, detail="this listing is already closed")
        db.set_listing_status(listing_id, "cancelled", ts)
        return _listing_view(db.get_listing(listing_id), user, detail=True)
    if listing["status"] != "open":
        raise HTTPException(status_code=400, detail="only an open listing can be edited")
    fields: dict = {}
    if body.qty is not None:
        fields["qty"] = body.qty
    if body.note is not None:
        fields["note"] = body.note.strip() or None
    if body.crafted is not None:           # present (even if empty) ⇒ replace/clear
        spec = _clean_crafted(body.crafted)
        # Commission specs live under attributes.spec (a future as-delivered
        # annotation sits beside it); other modes keep the flat crafted blob.
        if listing["mode"] == "commission":
            fields["attributes"] = {"spec": spec} if spec else None
        else:
            fields["attributes"] = spec
    if listing["mode"] == "sale" and body.price_auec is not None:
        fields["price_auec"] = float(body.price_auec)
    if listing["mode"] == "commission":
        if body.price_auec is not None:
            fields["price_auec"] = float(body.price_auec)   # revised budget
        if body.ends_at:
            ends_at = _normalize_event_start(body.ends_at)
            if datetime.fromisoformat(ends_at) <= datetime.now(timezone.utc):
                raise HTTPException(status_code=400,
                                    detail="the needed-by date must be in the future")
            fields["ends_at"] = ends_at
        if body.materials is not None:
            materials = body.materials.strip().lower()
            if materials not in _COMMISSION_MATERIALS:
                raise HTTPException(status_code=400,
                                    detail="materials must be requester, crafter or split")
            fields["materials"] = materials
    if listing["mode"] == "barter" and body.want is not None:
        want = body.want.strip()
        if not want:
            raise HTTPException(status_code=400, detail="a barter listing needs what you want in return")
        fields["want"] = want
    if listing["mode"] == "auction":
        if body.ends_at:
            ends_at = _normalize_event_start(body.ends_at)
            if datetime.fromisoformat(ends_at) <= datetime.now(timezone.utc):
                raise HTTPException(status_code=400, detail="auction end time must be in the future")
            fields["ends_at"] = ends_at
        if body.buyout_auec is not None:
            if float(body.buyout_auec) < float(listing["start_price"] or 0):
                raise HTTPException(status_code=400, detail="buyout must be at or above the start price")
            fields["buyout_auec"] = float(body.buyout_auec)
    if fields:
        db.update_listing(listing_id, fields, ts)
    return _listing_view(db.get_listing(listing_id), user, detail=True)


@app.post("/api/market/{listing_id}/offer")
async def place_offer(listing_id: int, body: OfferIn,
                      user: dict = Depends(require_session)):
    """Buy / bid / make an offer / counter a barter (any member but the seller).
    For a sale, an offer at or above the ask is an instant buy (→ pending); a lower
    one waits for the seller to accept. An auction bid must clear the next minimum
    and instantly wins on a buyout. A barter offer is a counter-item and/or note."""
    listing = db.get_listing(listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="unknown listing")
    now = datetime.now(timezone.utc)
    listing = _resolve_listing_expiry(listing, db.list_offers(listing_id), now)
    if listing["seller_id"] == user["id"]:
        raise HTTPException(status_code=400, detail="you can't bid on your own listing")
    if listing["status"] != "open":
        raise HTTPException(status_code=400, detail="this listing is no longer open")
    ts = now.isoformat()
    mode = listing["mode"]
    if mode == "auction":
        if body.amount_auec is None:
            raise HTTPException(status_code=400, detail="a bid amount is required")
        state = nav_core.derive_auction_state(listing, db.list_offers(listing_id), now)
        if float(body.amount_auec) < state["next_min_bid"]:
            raise HTTPException(status_code=400,
                                detail=f"bid must be at least {state['next_min_bid']:g} aUEC")
        oid = db.add_offer(listing_id, user["id"], float(body.amount_auec),
                           None, None, None, ts)
        state = nav_core.derive_auction_state(listing, db.list_offers(listing_id), now)
        if state["bought_out"] and state["winner_id"] == user["id"]:
            db.settle_listing(listing_id, user["id"], "pending", ts)
            db.set_offer_status(oid, "accepted")
            db.reject_other_offers(listing_id, oid)
    elif mode == "commission":
        # A crafter's quote: their price (may differ from the posted budget) +
        # a note (ETA, proposed quality, material notes). Never an instant deal
        # — the requester picks a quote manually.
        if body.amount_auec is None:
            raise HTTPException(status_code=400, detail="a quote needs an aUEC amount")
        oid = db.add_offer(listing_id, user["id"], float(body.amount_auec), None, None,
                           (body.offer_note or "").strip() or None, ts)
    elif mode == "sale":
        price = float(listing["price_auec"] or 0)
        amount = float(body.amount_auec) if body.amount_auec is not None else price
        oid = db.add_offer(listing_id, user["id"], amount, None, None,
                           (body.offer_note or "").strip() or None, ts)
        if amount >= price:                  # buy at (or above) the ask → instant deal
            db.settle_listing(listing_id, user["id"], "pending", ts)
            db.set_offer_status(oid, "accepted")
            db.reject_other_offers(listing_id, oid)
    else:   # barter
        item_id = item_name = None
        if body.offer_item_id:
            it = _resolve_or_400(body.offer_item_id)
            item_id, item_name = it["item_id"], it["name"]
        note = (body.offer_note or "").strip() or None
        if not item_id and not note:
            raise HTTPException(status_code=400, detail="offer an item or a note")
        oid = db.add_offer(listing_id, user["id"], None, item_id, item_name, note, ts)
    final = db.get_listing(listing_id)
    _notify_bg(_notify_market_offer(
        final, db.get_offer(oid),
        deal=final["status"] == "pending" and final.get("buyer_id") == user["id"]))
    return _listing_view(final, user, detail=True)


@app.patch("/api/market/{listing_id}/offer/{offer_id}")
async def act_on_offer(listing_id: int, offer_id: int, body: OfferActionIn,
                       user: dict = Depends(require_session)):
    """Accept an offer (seller or admin → the listing goes pending with that
    bidder, the rest are dropped) or withdraw your own active offer."""
    listing = db.get_listing(listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="unknown listing")
    offer = db.get_offer(offer_id)
    if offer is None or offer["listing_id"] != listing_id:
        raise HTTPException(status_code=404, detail="unknown offer")
    ts = datetime.now(timezone.utc).isoformat()
    if body.action == "withdraw":
        if offer["bidder_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="you can only withdraw your own offer")
        if (listing["mode"] == "commission" and offer["status"] == "accepted"
                and listing["status"] == "pending"):
            # The accepted crafter can bail on the job ("can't source the
            # Riccite") — the request goes back to open for other quotes; offers
            # that were already lost stay lost.
            db.set_offer_status(offer_id, "withdrawn")
            db.settle_listing(listing_id, None, "open", ts)
        elif offer["status"] != "active":
            raise HTTPException(status_code=400, detail="that offer is no longer active")
        else:
            db.set_offer_status(offer_id, "withdrawn")
    elif body.action == "accept":
        if listing["seller_id"] != user["id"] and not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="only the seller can accept an offer")
        if listing["status"] != "open":
            raise HTTPException(status_code=400, detail="this listing is no longer open")
        if offer["status"] != "active":
            raise HTTPException(status_code=400, detail="that offer is no longer active")
        db.settle_listing(listing_id, offer["bidder_id"], "pending", ts)
        db.set_offer_status(offer_id, "accepted")
        db.reject_other_offers(listing_id, offer_id)
        _notify_bg(_notify_market_accepted(db.get_listing(listing_id), offer))
    else:
        raise HTTPException(status_code=400, detail="unknown action")
    return _listing_view(db.get_listing(listing_id), user, detail=True)


@app.post("/api/market/{listing_id}/confirm")
async def confirm_handoff(listing_id: int, user: dict = Depends(require_session)):
    """Confirm the in-game handoff happened (buyer or seller). When both sides have
    confirmed, the deal is `completed` — the app never touches goods or aUEC, it
    only records that both parties agree it's done."""
    listing = db.get_listing(listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="unknown listing")
    if listing["status"] != "pending":
        raise HTTPException(status_code=400, detail="no pending deal to confirm")
    is_seller = listing["seller_id"] == user["id"]
    is_buyer = listing["buyer_id"] == user["id"]
    if not (is_seller or is_buyer):
        raise HTTPException(status_code=403, detail="only the buyer or seller can confirm this deal")
    ts = datetime.now(timezone.utc).isoformat()
    side = "seller" if is_seller else "buyer"
    # Completion is decided atomically inside db.confirm_listing (keyed off the
    # other side's live flag), not from the read above — see its docstring.
    db.confirm_listing(listing_id, side, ts, completed_at=ts)
    final = db.get_listing(listing_id)
    if final["status"] == "completed":
        _notify_bg(_notify_market_completed(final))
    else:
        _notify_bg(_notify_market_confirm_needed(final, confirmed_by=side))
    return _listing_view(final, user, detail=True)


@app.get("/api/biomes")
async def list_biomes():
    """Biome lookups (by_body / by_system / all) for the biome datalist; the
    UI narrows to the player's current body, falling back to system then all."""
    return biomes


@app.get("/api/custom_pois")
async def list_custom_pois(user: dict | None = Depends(current_user)):
    """Custom POIs visible to the caller — everyone's shared POIs plus the
    caller's own private ones."""
    allowed = viewer_owner_ids(user)
    return [
        d for d in db.list_custom_pois()
        if not d.get("private")
        or (d.get("owner_id") is not None and d["owner_id"] in allowed)
    ]


class PoiEditIn(BaseModel):
    note: str | None = Field(default=None, max_length=_NOTE_MAX)
    private: bool | None = None   # toggle owner-only visibility


@app.patch("/api/custom_pois/{poi_id}")
async def update_custom_poi(poi_id: int, body: PoiEditIn, user: dict = Depends(require_session)):
    """Edit a custom POI's note and/or private flag. Ownership-scoped like
    delete; only custom POIs are editable (upstream POIs carry a read-only
    Comment). Only the supplied fields change."""
    async with hub.lock:
        poi = nav.pois.get(poi_id)
        if poi is None or not getattr(poi, "custom", False):
            raise HTTPException(status_code=404, detail="unknown custom poi")
        ensure_owns(user, poi.owner_id)
        if body.note is not None:
            note = body.note.strip() or None
            db.update_custom_poi_note(poi_id, note)
            poi.note = note
        if body.private is not None and body.private != poi.private:
            db.update_custom_poi_private(poi_id, body.private)
            poi.private = body.private
            # A QT marker going private (or back) changes the shared jump index,
            # so rebuild it + reassign nearest_qt across the dataset.
            if poi.qt_marker:
                nav_core.assign_qt_markers(nav)
        await hub.broadcast_all()
    return {"ok": True, "note": poi.note, "private": poi.private}


@app.delete("/api/custom_pois/{poi_id}")
async def delete_custom_poi(poi_id: int, user: dict = Depends(require_session)):
    async with hub.lock:
        removed = nav.pois.get(poi_id)
        if removed is None or not getattr(removed, "custom", False):
            raise HTTPException(status_code=404, detail="unknown custom poi")
        ensure_owns(user, removed.owner_id)
        was_qt = removed.qt_marker
        db.delete_custom_poi(poi_id)
        nav.pois.pop(poi_id, None)
        # Removing a QT marker leaves other entities pointing at a marker that's
        # gone, so rebuild the index + reassign nearest_qt across the dataset.
        if was_qt:
            nav_core.assign_qt_markers(nav)
        hub.forget_entity(poi_id)
        await hub.broadcast_all()
    return {"ok": True}


@app.get("/api/observations")
async def get_observations(
    q: str = "", category: str | None = None, system: str | None = None,
    container: str | None = None, type: str | None = None,
    owner_id: int | None = None, limit: int = 100,
):
    return nav_core.search_observations(
        nav, query=q, category=category, system=system, container=container,
        type_value=type, owner_id=owner_id, limit=min(limit, 5000),
    )


@app.delete("/api/observations/{obs_id}")
async def delete_observation(obs_id: int, user: dict = Depends(require_session)):
    async with hub.lock:
        obs = nav.observations.get(obs_id)
        if obs is None:
            raise HTTPException(status_code=404, detail="unknown observation")
        ensure_owns(user, obs.owner_id)
        db.delete_observation(obs_id)
        nav.observations.pop(obs_id, None)
        hub.forget_entity(obs_id)
        await hub.broadcast_all()
        return {"ok": True}


@app.get("/api/leaderboard")
async def leaderboard(user: dict = Depends(require_session)):
    """Per-contributor tallies for the Leaderboard page: custom POIs and each
    observation category, counted per player. Players are keyed by their stable
    PlayerID (so a character rename stays one row) and labelled with their
    current handle; ownerless legacy records fold into a single 'Unknown' row."""
    cats = [
        {"key": "poi", "label": "POIs"},
        {"key": "resource", "label": "Resource Nodes"},
        {"key": "wildlife", "label": "Fauna"},
        {"key": "harvestable", "label": "Harvestables"},
    ]
    by_player: dict[str, dict] = {}
    mine_ids = handles.player_ids_for(user["id"])

    def bucket(owner_id, owner_handle):
        if owner_id is not None:
            key = f"id:{owner_id}"
            label = handles.handle_for(owner_id) or owner_handle or f"Player {owner_id}"
        elif owner_handle:
            key = f"h:{owner_handle}"
            label = owner_handle
        else:
            key, label = "unknown", "Unknown"
        row = by_player.get(key)
        if row is None:
            row = by_player[key] = {
                "handle": label,
                "counts": {c["key"]: 0 for c in cats},
                "total": 0,
                "mine": owner_id is not None and owner_id in mine_ids,
            }
        return row

    for poi in db.list_custom_pois():
        row = bucket(poi.get("owner_id"), poi.get("owner_handle"))
        row["counts"]["poi"] += 1
        row["total"] += 1

    for obs in db.list_observations():
        cat = obs.get("category")
        if cat not in ("resource", "wildlife", "harvestable"):
            continue
        row = bucket(obs.get("owner_id"), obs.get("owner_handle"))
        row["counts"][cat] += 1
        row["total"] += 1

    contributors = sorted(
        by_player.values(), key=lambda r: (-r["total"], r["handle"].lower())
    )
    totals = {c["key"]: sum(r["counts"][c["key"]] for r in contributors) for c in cats}
    return {
        "categories": cats,
        "contributors": contributors,
        "totals": totals,
        "grand_total": sum(totals.values()),
    }


# How many entries each "top N" breakdown returns to the Statistics page; the
# response also carries the distinct-count so the UI can say "+N more".
_STATS_TOP_N = 15
# How many trailing weeks the activity sparkline covers.
_STATS_WEEKS = 16


def _top_counter(counter: Counter, limit: int = _STATS_TOP_N) -> dict:
    """A Counter -> {"items": [{"name","count"}, ...top], "distinct": int} shape.
    Distinct is the full key count so the UI can note how many were truncated."""
    items = [{"name": name, "count": n} for name, n in counter.most_common(limit)]
    return {"items": items, "distinct": len(counter)}


def _iso_week_start(dt: datetime) -> datetime:
    """Monday 00:00 (UTC) of the ISO week containing `dt`."""
    d = dt.astimezone(timezone.utc)
    monday = (d - timedelta(days=d.weekday())).date()
    return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)


@app.get("/api/stats")
async def stats(user: dict = Depends(require_session)):
    """Aggregate dataset statistics for the Statistics page: overall totals, the
    spread across bodies / systems / biomes / shards, per-type breakdowns
    (ore, fauna species, harvestables, POI types), resource quality bands, and a
    weekly activity sparkline. Computed live — the dataset is small (org scale),
    so a full scan per page load is cheap and always current.

    POIs are read from the in-memory NavData (the only place the imported
    starmap catalog exists), split by `.custom` into guild-created vs imported.
    Imported POIs are folded into the dataset-wide breakdowns (bodies, systems,
    POI types) as their own dimension, and surfaced separately in the totals so
    the UI can annotate them; the guild-activity metrics (contributors, weekly
    activity, resource/fauna/harvestable observations) stay contribution-only."""
    # Normalize the Poi dataclasses to the same dict shape the observation rows
    # use (note: "container" not "container_name") so one aggregation path serves
    # both. `custom` distinguishes guild-created POIs from the imported catalog.
    # Private POIs are owner-only — keep them out of org-wide stats entirely so
    # neither their count nor their location leaks to the rest of the org.
    all_pois = [
        {"system": p.system, "container": p.container_name,
         "type": p.type or "Custom", "owner_id": p.owner_id,
         "owner_handle": p.owner_handle, "custom": p.custom}
        for p in nav.pois.values()
        if not getattr(p, "private", False)
    ]
    custom_pois = [p for p in all_pois if p["custom"]]
    imported_pois = [p for p in all_pois if not p["custom"]]
    obs = db.list_observations()

    cat_obs = {"resource": [], "wildlife": [], "harvestable": []}
    for o in obs:
        bucket = cat_obs.get(o.get("category"))
        if bucket is not None:
            bucket.append(o)

    def _body_label(r):
        return r.get("container") or "Deep Space"

    # --- coverage: distinct systems / bodies / shards / contributors ---------
    # Coverage spans the whole dataset (imported catalog included); contributors
    # is guild-only (imported POIs carry no owner).
    coverage = all_pois + obs
    systems = {r.get("system") for r in coverage if r.get("system")}
    bodies = {(r.get("system"), r.get("container")) for r in coverage if r.get("container")}
    shards = {o.get("shard_id") for o in obs if o.get("shard_id")}
    contributors = set()
    for r in custom_pois + obs:
        if r.get("owner_id") is not None:
            contributors.add(f"id:{r['owner_id']}")
        elif r.get("owner_handle"):
            contributors.add(f"h:{r['owner_handle']}")

    # --- where: records per body (stacked by category) -----------------------
    by_body: dict[tuple, dict] = {}
    by_system: Counter = Counter()
    by_biome: Counter = Counter()

    def _body_row(r):
        key = (r.get("system"), _body_label(r))
        row = by_body.get(key)
        if row is None:
            row = by_body[key] = {
                "body": key[1], "system": key[0] or "?", "imported": 0,
                "poi": 0, "resource": 0, "wildlife": 0, "harvestable": 0, "total": 0,
            }
        return row

    for p in all_pois:
        row = _body_row(p)
        if p["custom"]:
            row["poi"] += 1
            row["total"] += 1   # `total` ranks by guild activity, not catalog size
        else:
            row["imported"] += 1   # carried as scope context, not part of the rank
        if p.get("system"):
            by_system[p["system"]] += 1
    for o in obs:
        cat = o.get("category")
        if cat not in cat_obs:
            continue
        row = _body_row(o)
        row[cat] += 1
        row["total"] += 1
        if o.get("system"):
            by_system[o["system"]] += 1
        if o.get("biome"):
            by_biome[o["biome"]] += 1

    # Rank by guild contributions ("most-mapped by us"); bodies that only carry
    # imported catalog POIs aren't "mapped" by the guild, so they're left out
    # here (their scope still shows in the systems / coverage / type breakdowns).
    top_bodies = sorted(
        (r for r in by_body.values() if r["total"] > 0),
        key=lambda r: (-r["total"], r["body"].lower()),
    )[:_STATS_TOP_N]

    # --- per-type breakdowns -------------------------------------------------
    ores = Counter(o["data"].get("ore") or "Unknown" for o in cat_obs["resource"])
    species = Counter(o["data"].get("species") or "Unknown" for o in cat_obs["wildlife"])
    harvestables = Counter(o["data"].get("name") or "Unknown" for o in cat_obs["harvestable"])
    # POI types span both guild-created and imported POIs (the imported catalog is
    # where the rich type variety lives). Each item carries the guild subtotal
    # alongside the total so the UI can show how many of each type the org
    # contributed vs imported.
    poi_types_all = Counter(p["type"] for p in all_pois)
    poi_types_guild = Counter(p["type"] for p in custom_pois)
    poi_types = {
        "items": [
            {"name": name, "count": n, "guild": poi_types_guild.get(name, 0)}
            for name, n in poi_types_all.most_common(_STATS_TOP_N)
        ],
        "distinct": len(poi_types_all),
    }

    # --- resource quality bands (B1..B8 + Unknown) ---------------------------
    bands = Counter()
    for o in cat_obs["resource"]:
        b = o["data"].get("band")
        try:
            bands[str(max(1, min(8, int(b))))] += 1
        except (TypeError, ValueError):
            bands["Unk"] += 1
    band_series = [{"band": f"B{n}", "count": bands.get(str(n), 0)} for n in range(1, 9)]
    band_series.append({"band": "Unk", "count": bands.get("Unk", 0)})

    # --- weekly activity (observations carry a timestamp; POIs don't) --------
    weeks: Counter = Counter()
    for o in obs:
        ts = o.get("observed_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        weeks[_iso_week_start(dt)] += 1
    activity = []
    if weeks:
        end = _iso_week_start(datetime.now(timezone.utc))
        start = end - timedelta(weeks=_STATS_WEEKS - 1)
        wk = start
        while wk <= end:
            activity.append({
                "label": wk.strftime("%b %d"),
                "count": weeks.get(wk, 0),
            })
            wk += timedelta(weeks=1)

    return {
        "totals": {
            "poi": len(custom_pois),
            "poi_imported": len(imported_pois),
            "resource": len(cat_obs["resource"]),
            "wildlife": len(cat_obs["wildlife"]),
            "harvestable": len(cat_obs["harvestable"]),
            "observations": len(obs),
            # Guild contributions only (imported catalog excluded).
            "records": len(custom_pois) + len(obs),
            "contributors": len(contributors),
            "systems": len(systems),
            "bodies": len(bodies),
            "shards": len(shards),
        },
        # Whether an imported POI catalog (starmap and/or wiki) is loaded, so the
        # UI knows to show the imported annotations even when the count is 0.
        "catalog_enabled": starmap_pois_enabled() or wiki_pois_enabled(),
        "top_bodies": top_bodies,
        "systems": _top_counter(by_system),
        "ores": _top_counter(ores),
        "species": _top_counter(species),
        "harvestables": _top_counter(harvestables),
        "biomes": _top_counter(by_biome),
        "poi_types": poi_types,
        "bands": band_series,
        "activity": activity,
    }


@app.post("/api/path/{action}")
async def path_control(action: str, user: dict = Depends(require_session)):
    if action not in ("start", "stop", "clear"):
        raise HTTPException(status_code=404, detail="unknown path action")
    async with hub.lock:
        sess = hub.get(user)
        if action == "start":
            sess.tracking = True
        elif action == "stop":
            sess.tracking = False
        else:  # clear
            sess.path.clear()
        if sess.nav_state is not None:
            sess._attach_breadcrumbs()
        await sess.broadcast()
    return {"ok": True, "tracking": sess.tracking, "crumbs": len(sess.path)}


async def _rebuild_nav() -> None:
    """Rebuild NavData (upstream catalog + DB customs/observations) and swap it
    in. Used by /api/refresh and when org settings change."""
    global nav
    fresh = await asyncio.to_thread(load_nav_data)
    nav_core.merge_custom_pois(fresh, db.list_custom_pois())
    merge_all_observations(fresh)
    nav_core.assign_qt_markers(fresh)
    async with hub.lock:
        nav = fresh
        for s in hub.sessions.values():
            if (s.destination_id is not None
                    and s.destination_id not in nav.pois
                    and s.destination_id not in nav.observations):
                s.destination_id = None
        await hub.broadcast_all()
    rebuild_trade_terminals()   # re-resolve the crosswalk against the new POI set


@app.post("/api/refresh")
async def refresh_data(admin: dict = Depends(require_admin)):
    """Re-fetch the dataset (starmap) and the commodities list (uexcorp)
    without restarting. Admin only."""
    global raw_commodity_names, commodity_names, ships, fleet_ships, item_names, item_prices
    global trade_terminals_raw, trade_prices
    raw_commodity_names = await asyncio.to_thread(load_raw_commodity_names)
    commodity_names = await asyncio.to_thread(load_commodity_names)
    ships = await asyncio.to_thread(load_ships)
    fleet_ships = await asyncio.to_thread(load_fleet_ships)
    item_names = await asyncio.to_thread(load_item_names)
    item_prices = await asyncio.to_thread(build_item_prices)   # fresh price refs
    trade_terminals_raw = await asyncio.to_thread(load_trade_terminals)
    trade_prices = await asyncio.to_thread(load_trade_prices)
    refresh_catalog()   # feeds changed → rebuild the shared item catalog
    await _rebuild_nav()   # also re-runs the trade-terminal crosswalk
    return {
        "ok": True,
        "data": data_info,
        "containers": len(nav.containers),
        "pois": len(nav.pois),
        "observations": len(nav.observations),
        "raw_commodities": len(raw_commodity_names),
        "harvestables": len(harvestable_names),
        "ships": len(ships),
        "items": len(item_names),
        "trade_terminals": len(trade_terminals),
        "trade_prices": len(trade_prices),
    }


@app.post("/api/admin/stats/resources/clear")
async def clear_resource_stats(admin: dict = Depends(require_admin)):
    """Wipe every resource/wildlife/harvestable sighting org-wide (admin only):
    zeroes the Statistics page's contribution metrics and the resource
    leaderboard. Custom POIs and QT markers are kept. The in-memory dataset is
    rebuilt afterwards so the change is live without a restart (and any session
    aimed at a now-deleted sighting is cleared)."""
    deleted = await asyncio.to_thread(db.clear_observations)
    await _rebuild_nav()
    return {"ok": True, "deleted": deleted, "observations": len(nav.observations)}


@app.post("/api/admin/stats/cargo/clear")
async def clear_cargo_stats(admin: dict = Depends(require_admin)):
    """Wipe finished hauling runs org-wide (admin only): zeroes the cargo
    leaderboard, hauling stats, and every member's run history. In-progress
    (active) runs are left running."""
    deleted = await asyncio.to_thread(db.clear_run_history)
    return {"ok": True, "deleted": deleted}


@app.post("/api/admin/stats/trade/clear")
async def clear_trade_stats(admin: dict = Depends(require_admin)):
    """Wipe finished trade runs org-wide (admin only): zeroes the guild trade stats
    board and every member's trade history. In-progress (active) runs keep going."""
    deleted = await asyncio.to_thread(db.clear_trade_run_history)
    return {"ok": True, "deleted": deleted}


@app.get("/api/settings")
async def get_settings(user: dict = Depends(require_session)):
    """Org-wide settings (any member can read; admins change them)."""
    return {
        "starmap_pois_enabled": starmap_pois_enabled(),
        "wiki_pois_enabled": wiki_pois_enabled(),
        "member_role_id": member_role_id(),
        "obs_fresh_window_h": obs_fresh_window_h(),
        "lfg_ageoff_min": lfg_ageoff_min(),
        "lfg_stale_min": lfg_stale_min(),
        "warning_ageoff_min": warning_ageoff_min(),
        "warning_stale_min": warning_stale_min(),
        "hazard_radius_km": hazard_radius_km(),      # snare-detour base radius (#24 v2)
        "stock_ageoff_min": stock_ageoff_min(),      # stock-report lifetime (#21)
        "extra_admin_ids": extra_admin_ids(),       # DB-backed, editable here
        "root_admin_ids": sorted(auth.ADMIN_IDS),   # env, read-only floor
        "org_logo": bool(db.get_setting("org_logo_ext")),
        "org_name": org_name(),
        "motd": motd_state()["text"],
        # Per-category Discord webhooks: never echo a URL (it's a credential) —
        # surface only whether each category has one set + a masked tail.
        "discord_webhooks": notify.webhook_status(),
        "discord_reminder_lead_min": notify.reminder_lead_min(),
    }


class SettingsIn(BaseModel):
    starmap_pois_enabled: bool | None = None
    wiki_pois_enabled: bool | None = None    # SC Wiki locations catalog (#28a)
    member_role_id: str | None = Field(default=None, max_length=_META_MAX)
    obs_fresh_window_h: int | None = Field(default=None, ge=1, le=8760)  # 1h .. 1yr
    # Group Finder lifecycle (minutes): posts turn stale (yellow) at lfg_stale_min
    # and age off the board at lfg_ageoff_min. Capped at a week; stale < age-off is
    # enforced in the handler.
    lfg_ageoff_min: int | None = Field(default=None, ge=1, le=10080)
    lfg_stale_min: int | None = Field(default=None, ge=1, le=10080)
    # Pirate danger-board lifecycle (minutes): warnings turn stale at warning_stale_min
    # and age off at warning_ageoff_min. Same stale < age-off rule, enforced below.
    warning_ageoff_min: int | None = Field(default=None, ge=1, le=10080)
    warning_stale_min: int | None = Field(default=None, ge=1, le=10080)
    # Base hazard radius (km) a warning projects for snare-detour routing (#24 v2);
    # severity scales it in code. 100 km .. 200,000 km.
    hazard_radius_km: int | None = Field(default=None, ge=100, le=200_000)
    # Stock-report lifetime (minutes, #21): how long an out-of-stock report keeps
    # steering the trade solver away from that buy. Capped at a week.
    stock_ageoff_min: int | None = Field(default=None, ge=1, le=10080)
    # Discord snowflakes are ~17-20 digits; cap the list so the admin form can't
    # be used to stuff the meta table. Each id is validated (isdigit) below.
    extra_admin_ids: list[str] | None = Field(default=None, max_length=200)
    # Per-category Discord webhook URLs, e.g. {"events": "https://…", "goals": ""}
    # ("" clears one). Each is validated against real Discord hosts in the handler
    # (anti-SSRF). A category is "on" iff it has a valid webhook.
    discord_webhooks: dict[str, str] | None = None
    discord_reminder_lead_min: int | None = Field(default=None, ge=1, le=1440)
    # Custom guild branding: the org's display name (shown on the login splash +
    # app chooser) and a broadcast message-of-the-day. Both "" clears. Plain text
    # only — rendered via textContent on the client, never as HTML.
    org_name: str | None = Field(default=None, max_length=80)
    motd: str | None = Field(default=None, max_length=2000)


@app.post("/api/settings")
async def update_settings(body: SettingsIn, admin: dict = Depends(require_admin)):
    """Update org settings (admin only). Only the fields present are changed.
    Toggling the POI catalog rebuilds the dataset; the member-role gate takes
    effect at the next login (existing sessions stand until they expire); the
    freshness window is display-only and applies on the clients' next refresh."""
    if body.member_role_id is not None:
        db.set_setting("member_role_id", body.member_role_id.strip())
    if body.extra_admin_ids is not None:
        cleaned, seen = [], set()
        for raw in body.extra_admin_ids:
            s = (raw or "").strip()
            if not s or s in seen:
                continue
            if not s.isdigit() or len(s) > 20:   # Discord ids are numeric snowflakes (<=20 digits)
                raise HTTPException(status_code=400,
                                    detail=f"invalid Discord id: {s!r}")
            seen.add(s)
            if s not in auth.ADMIN_IDS:   # root admins are implicit; don't store dupes
                cleaned.append(s)
        # The env root admins are the floor; only block a change that would
        # leave the whole org with no admin at all (possible only when ADMIN_IDS
        # is unset), which would be an unrecoverable lockout.
        if not (auth.ADMIN_IDS or cleaned):
            raise HTTPException(status_code=400, detail="can't remove the last admin")
        db.set_setting("extra_admin_ids", ",".join(cleaned))
    if body.obs_fresh_window_h is not None:
        db.set_setting("obs_fresh_window_h", str(max(1, body.obs_fresh_window_h)))
    if body.lfg_ageoff_min is not None or body.lfg_stale_min is not None:
        # Resolve the pair against current values, then enforce stale < age-off so a
        # post always has a green phase before it goes yellow.
        ageoff = body.lfg_ageoff_min if body.lfg_ageoff_min is not None else lfg_ageoff_min()
        stale = body.lfg_stale_min if body.lfg_stale_min is not None else lfg_stale_min()
        if stale >= ageoff:
            raise HTTPException(status_code=400,
                                detail="Stale time must be less than the age-off time.")
        db.set_setting("lfg_ageoff_min", str(ageoff))
        db.set_setting("lfg_stale_min", str(stale))
    if body.warning_ageoff_min is not None or body.warning_stale_min is not None:
        ageoff = body.warning_ageoff_min if body.warning_ageoff_min is not None else warning_ageoff_min()
        stale = body.warning_stale_min if body.warning_stale_min is not None else warning_stale_min()
        if stale >= ageoff:
            raise HTTPException(status_code=400,
                                detail="Stale time must be less than the age-off time.")
        db.set_setting("warning_ageoff_min", str(ageoff))
        db.set_setting("warning_stale_min", str(stale))
    if body.hazard_radius_km is not None:
        db.set_setting("hazard_radius_km", str(body.hazard_radius_km))
    if body.stock_ageoff_min is not None:
        db.set_setting("stock_ageoff_min", str(body.stock_ageoff_min))
    if body.starmap_pois_enabled is not None:
        db.set_setting("starmap_pois_enabled", "1" if body.starmap_pois_enabled else "0")
        await _rebuild_nav()
    if body.wiki_pois_enabled is not None:
        db.set_setting("wiki_pois_enabled", "1" if body.wiki_pois_enabled else "0")
        await _rebuild_nav()
    if body.discord_webhooks is not None:
        for cat, url in body.discord_webhooks.items():
            if cat not in notify.CATEGORIES:
                raise HTTPException(status_code=400,
                                    detail=f"unknown notification category: {cat}")
            url = (url or "").strip()
            if url and not notify.is_valid_webhook_url(url):
                raise HTTPException(status_code=400,
                                    detail=f"not a valid Discord webhook URL for {cat}")
            notify.set_webhook(cat, url)   # "" clears it
    if body.discord_reminder_lead_min is not None:
        db.set_setting(notify.REMINDER_LEAD_KEY, str(body.discord_reminder_lead_min))
    if body.org_name is not None:
        db.set_setting("org_name", body.org_name.strip())
    if body.motd is not None:
        new_motd = body.motd.strip()
        # Only stamp a fresh update time when the text actually changes, so a
        # no-op save doesn't resurface a banner every member already dismissed.
        if new_motd != motd_state()["text"]:
            db.set_setting("motd", new_motd)
            db.set_setting("motd_updated", str(int(time.time())) if new_motd else "0")
    return {"ok": True, "starmap_pois_enabled": starmap_pois_enabled(),
            "wiki_pois_enabled": wiki_pois_enabled(),
            "member_role_id": member_role_id(),
            "obs_fresh_window_h": obs_fresh_window_h(),
            "lfg_ageoff_min": lfg_ageoff_min(), "lfg_stale_min": lfg_stale_min(),
            "warning_ageoff_min": warning_ageoff_min(), "warning_stale_min": warning_stale_min(),
            "hazard_radius_km": hazard_radius_km(),
            "stock_ageoff_min": stock_ageoff_min(),
            "extra_admin_ids": extra_admin_ids(),
            "root_admin_ids": sorted(auth.ADMIN_IDS), "pois": len(nav.pois),
            "discord_webhooks": notify.webhook_status(),
            "discord_reminder_lead_min": notify.reminder_lead_min(),
            "org_name": org_name(), "motd": motd_state()["text"]}


_test_send_at = 0.0   # last admin test-send, for a light cooldown


class DiscordTestIn(BaseModel):
    category: str = Field(max_length=_TYPE_MAX)


@app.post("/api/settings/discord/test")
async def test_discord_webhook(body: DiscordTestIn, admin: dict = Depends(require_admin)):
    """Fire a test message at a category's webhook so an admin can confirm that
    channel's routing before relying on it. Rate-limited to once every few
    seconds across all categories."""
    global _test_send_at
    if body.category not in notify.CATEGORIES:
        raise HTTPException(status_code=400, detail="unknown notification category")
    if not notify.is_configured(body.category):
        raise HTTPException(status_code=400,
                            detail=f"no Discord webhook set for {body.category}")
    now = time.monotonic()
    if now - _test_send_at < 5:
        raise HTTPException(status_code=429, detail="slow down — try again in a moment")
    _test_send_at = now
    who = admin.get("username") or "an admin"
    ok = await notify.send(
        body.category,
        f"✅ **Org Navigator** — the **{body.category}** channel is connected. "
        f"Test sent by {who}.{_deep_link('')}")
    if not ok:
        raise HTTPException(status_code=502,
                            detail="Discord rejected the message — check the webhook URL")
    return {"ok": True}


@app.get("/api/branding")
async def get_branding():
    """Public org branding for the pre-auth login splash: the guild name and
    whether a custom logo exists. Deliberately minimal — no member data — since
    this is reachable without a session (see the auth_gate exemption)."""
    return {"org_name": org_name(), "org_logo": bool(db.get_setting("org_logo_ext"))}


@app.get("/api/org-logo")
async def get_org_logo():
    """Serve the org's uploaded logo (shown alongside the built-in one in the
    header and on the login splash). Public so it can render pre-auth; the
    auth_gate middleware exempts this GET."""
    ext = db.get_setting("org_logo_ext")
    if ext:
        path = BRANDING_DIR / f"org_logo.{ext}"
        if path.is_file():
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="no org logo")


@app.post("/api/org-logo")
async def upload_org_logo(file: UploadFile = File(...),
                          admin: dict = Depends(require_admin)):
    """Replace the org's custom logo (admin). Validates by Content-Type and caps
    size; writes to the /data volume and records the extension in `meta`."""
    ext = _LOGO_TYPES.get((file.content_type or "").lower())
    if not ext:
        raise HTTPException(status_code=400, detail="logo must be a PNG, JPG, or WebP image")
    data = await file.read(_LOGO_MAX_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="the file is empty")
    if len(data) > _LOGO_MAX_BYTES:
        raise HTTPException(status_code=400, detail="logo too large (max 2 MB)")
    # Verify the bytes actually match the claimed type — the Content-Type header
    # is client-supplied, so don't trust it to keep e.g. an HTML/script polyglot
    # off the /data volume.
    if not _sniff_image(data, ext):
        raise HTTPException(status_code=400,
                            detail="file contents don't match a PNG, JPG, or WebP image")
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    # Drop any prior logo (possibly a different extension) so none is orphaned.
    for old in BRANDING_DIR.glob("org_logo.*"):
        old.unlink(missing_ok=True)
    (BRANDING_DIR / f"org_logo.{ext}").write_bytes(data)
    db.set_setting("org_logo_ext", ext)
    return {"ok": True, "org_logo": True}


@app.delete("/api/org-logo")
async def delete_org_logo(admin: dict = Depends(require_admin)):
    """Remove the org's custom logo (admin). The built-in logo always remains."""
    for old in BRANDING_DIR.glob("org_logo.*"):
        old.unlink(missing_ok=True)
    db.set_setting("org_logo_ext", "")
    return {"ok": True, "org_logo": False}


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "containers": len(nav.containers),
        "pois": len(nav.pois),
        "observations": len(nav.observations),
        "handles": len(handles.by_handle),
        "raw_commodities": len(raw_commodity_names),
        "harvestables": len(harvestable_names),
        "ships": len(ships),
        "trade_terminals": len(trade_terminals),
        "trade_prices": len(trade_prices),
        "active_sessions": sum(1 for s in hub.sessions.values() if s.pos is not None),
        "data": data_info,
    }


def _ws_origin_ok(ws: WebSocket) -> bool:
    """Reject a cross-origin WebSocket handshake. SameSite=Lax already keeps a
    script-initiated cross-site handshake from carrying the session cookie, but
    an explicit Origin check is cheap defense-in-depth against socket hijacking.
    A same-origin browser sends Origin == its own scheme://host; non-browser
    clients (no Origin) are allowed since the cookie gate still applies."""
    origin = ws.headers.get("origin")
    if not origin:
        return True
    if PUBLIC_BASE_URL and origin.rstrip("/") == PUBLIC_BASE_URL:
        return True
    host = ws.headers.get("x-forwarded-host") or ws.headers.get("host")
    if host:
        try:
            return urllib.parse.urlparse(origin).netloc == host
        except ValueError:
            return False
    return False


def _my_online_prefs(uid: str) -> dict:
    """A member's own persisted who's-online prefs (for the #/online control bar).
    Defaults for a member who never set them: available, no activity, visible."""
    prefs = db.get_member(uid) or {}
    status = prefs.get("online_status")
    return {
        "status": status if status in ONLINE_STATUSES else "available",
        "activity": prefs.get("online_activity"),
        "appear_offline": bool(prefs.get("appear_offline")),
    }


@app.get("/api/online")
async def online_snapshot(user: dict = Depends(require_session)):
    """Who's-online roster snapshot (#19): visible members with an open tab right
    now, available-first, plus the caller's own prefs (`me`) to seed the status
    control. The live view is WS-driven (`online_roster` deltas); this is the
    initial paint for the #/online view and any non-WS consumer."""
    async with hub.lock:
        users, count = hub.online_roster(), hub.online_count()
    return {"users": users, "count": count, "me": _my_online_prefs(user["id"])}


@app.get("/api/playstyles")
async def playstyles(user: dict = Depends(require_session)):
    """The shared activity/playstyle vocabulary (#19): online-status activity
    quick-picks (step 2) and LFG entry tags (step 3) draw from this one list."""
    return {"tags": PLAYSTYLE_TAGS}


class OnlineStatusIn(BaseModel):
    status: str = Field(default="available", max_length=_UNIT_MAX)
    activity: str | None = Field(default=None, max_length=_ACTIVITY_MAX)
    appear_offline: bool = False


@app.post("/api/online/status")
async def set_online_status(body: OnlineStatusIn, user: dict = Depends(require_session)):
    """Set the caller's who's-online status / activity / visibility (#19 step 2).
    Persisted (survives reconnect) AND applied to their live roster record, then
    the roster + count are rebroadcast so every tab reflects it. "Appear offline"
    drops them from the visible roster — a lighter consent, independent of the
    navigator's position-sharing toggle."""
    uid = user["id"]
    status = body.status if body.status in ONLINE_STATUSES else "available"
    activity = (body.activity or "").strip()[:_ACTIVITY_MAX] or None
    db.set_online_prefs(uid, status, activity, body.appear_offline)
    async with hub.lock:
        rec = hub.online.get(uid)
        if rec is not None:
            rec["status"] = status
            rec["activity"] = activity
            rec["visible"] = not body.appear_offline
    await hub.broadcast_online()
    await hub.broadcast_online_roster()
    return {"status": status, "activity": activity,
            "appear_offline": body.appear_offline}


class LFGPostIn(BaseModel):
    direction: str = Field(default="lfm", max_length=_UNIT_MAX)   # lfm | lfj
    tags: list[str] = Field(default_factory=list, max_length=_LFG_MAX_TAGS)
    slots: int | None = Field(default=None, ge=1, le=_LFG_MAX_SLOTS)   # lfm only
    note: str = Field(default="", max_length=_LFG_NOTE_MAX)
    rally: str | None = Field(default=None, max_length=_NAME_MAX)   # optional rally point
    comms: bool = False   # voice/comms expected
    announce: bool = False   # opt-in: also broadcast this post to the org's Discord


@app.get("/api/lfg")
async def lfg_snapshot(user: dict = Depends(require_session)):
    """Looking-for-group board snapshot (#19 step 3): all active LFM/LFJ entries,
    newest first. Live updates arrive over WS as `lfg`; this is the initial paint."""
    async with hub.lock:
        entries = hub.lfg_board()
    # `announce_available` lets the composer show its "announce to Discord" opt-in
    # only when the org has actually configured an LFG webhook (a bool, never the URL).
    return {"entries": entries, "count": len(entries),
            "announce_available": notify.is_configured("lfg")}


@app.post("/api/lfg")
async def create_lfg(body: LFGPostIn, user: dict = Depends(require_session)):
    """Post an LFG entry (#19 step 3). `lfm` = hosting / starting a group, needs
    players (carries slots); `lfj` = solo, wants in (a raised hand). Tags are filtered
    to the shared playstyle vocabulary. One active entry per direction per member —
    re-posting supersedes the previous one."""
    direction = body.direction if body.direction in LFG_DIRECTIONS else "lfm"
    tags = [t for t in dict.fromkeys(body.tags) if t in PLAYSTYLE_TAGS][:_LFG_MAX_TAGS]
    note = (body.note or "").strip()[:_LFG_NOTE_MAX]
    rally = (body.rally or "").strip()[:_NAME_MAX] or None
    slots = body.slots if direction == "lfm" else None
    async with hub.lock:
        entry = hub.post_lfg(user["id"], direction, tags, slots, note, rally, bool(body.comms))
        pub = hub._public_lfg(entry)
    await hub.broadcast_lfg()
    # Opt-in Discord shout (rate-limited per member so nobody can blast the channel).
    if body.announce and notify.is_configured("lfg") and _lfg_announce_ok(user["id"]):
        _notify_bg(_notify_lfg_posted(pub))
    return pub


@app.post("/api/lfg/{entry_id}/join")
async def respond_lfg(entry_id: int, user: dict = Depends(require_session)):
    """Respond to an entry (#19 step 3): Join/Leave an LFM (fills a slot) or Ping/Un-ping
    an LFJ. Idempotent toggle; responding to your own post is a no-op."""
    async with hub.lock:
        entry = hub.join_lfg(entry_id, user["id"])
        if entry is None:
            raise HTTPException(status_code=404, detail="That LFG post is gone.")
        pub = hub._public_lfg(entry)
    await hub.broadcast_lfg()
    return pub


@app.delete("/api/lfg/{entry_id}")
async def delete_lfg(entry_id: int, user: dict = Depends(require_session)):
    """Close an LFG entry (#19 step 3) — poster or admin only."""
    async with hub.lock:
        ok = hub.close_lfg(entry_id, user["id"], bool(user.get("is_admin")))
    if not ok:
        raise HTTPException(status_code=404, detail="No such LFG post (or not yours).")
    await hub.broadcast_lfg()
    return {"ok": True}


class WarningIn(BaseModel):
    kind: str = Field(default="point", max_length=_UNIT_MAX)       # point | lane
    threat: str = Field(default="pvp", max_length=_UNIT_MAX)       # pvp | pve
    severity: str = Field(default="active", max_length=_UNIT_MAX)  # sighted|active|deadly
    anchor_a: int | None = None      # POI id: the centre (point) / first endpoint (lane)
    anchor_b: int | None = None      # POI id: lane second endpoint (ignored for point)
    location: str = Field(default="", max_length=_WARNING_LOCATION_MAX)  # free text
    note: str = Field(default="", max_length=_WARNING_NOTE_MAX)
    announce: bool = False           # opt-in: also shout this warning to the org's Discord


@app.get("/api/warnings")
async def warnings_snapshot(user: dict = Depends(require_session)):
    """Pirate danger board snapshot (#24): all active warnings, deadliest + freshest
    first. Live updates arrive over WS as `warnings`; this is the initial paint.
    `announce_available` gates the composer's Discord opt-in to when a pirates webhook
    is configured (a bool, never the URL)."""
    async with hub.lock:
        entries = hub.warnings_board()
    return {"entries": entries, "count": len(entries),
            "announce_available": notify.is_configured("pirates")}


@app.post("/api/warnings")
async def create_warning(body: WarningIn, user: dict = Depends(require_session)):
    """Post a pirate danger warning (#24). `point` = danger around one POI; `lane` = a
    snare between two anchor POIs. Anchors must resolve to known POIs to steer the
    planner later; a warning with only free-text `location` is still valid board-only
    intel (a survivor posting mid-escape). Re-posting the same danger supersedes your
    previous one rather than stacking duplicates."""
    kind = body.kind if body.kind in WARNING_KINDS else "point"
    threat = body.threat if body.threat in WARNING_THREATS else "pvp"
    severity = body.severity if body.severity in WARNING_SEVERITIES else "active"
    location = (body.location or "").strip()[:_WARNING_LOCATION_MAX]
    note = (body.note or "").strip()[:_WARNING_NOTE_MAX]
    anchor_a = body.anchor_a if body.anchor_a in nav.pois else None
    anchor_b = body.anchor_b if (kind == "lane" and body.anchor_b in nav.pois) else None
    if anchor_b is not None and anchor_b == anchor_a:
        anchor_b = None
    if anchor_a is None and not location:
        raise HTTPException(
            status_code=400,
            detail="A warning needs a location — pick a POI or describe where.")
    async with hub.lock:
        try:
            entry = hub.post_warning(user["id"], kind, threat, severity,
                                     anchor_a, anchor_b, location, note)
        except ValueError as exc:
            raise HTTPException(status_code=429, detail=str(exc))
        pub = hub._public_warning(entry)
    await hub.broadcast_warnings()
    # Opt-in Discord shout (rate-limited per member so nobody can blast the channel).
    if body.announce and notify.is_configured("pirates") and _warning_announce_ok(user["id"]):
        _notify_bg(_notify_warning_posted(pub))
    return pub


@app.post("/api/warnings/{warning_id}/confirm")
async def warning_confirm(warning_id: int, user: dict = Depends(require_session)):
    """Community "still active" refresh (#24): resets the age-off clock and records you
    as a confirmer (a credibility signal, "3 people confirmed"). Anyone may confirm;
    idempotent per member."""
    async with hub.lock:
        entry = hub.confirm_warning(warning_id, user["id"])
        if entry is None:
            raise HTTPException(status_code=404, detail="That warning is gone.")
        pub = hub._public_warning(entry)
    await hub.broadcast_warnings()
    return pub


@app.delete("/api/warnings/{warning_id}")
async def delete_warning(warning_id: int, user: dict = Depends(require_session)):
    """Clear a danger warning ("all clear") — poster or admin only (#24)."""
    async with hub.lock:
        ok = hub.close_warning(warning_id, user["id"], bool(user.get("is_admin")))
    if not ok:
        raise HTTPException(status_code=404, detail="No such warning (or not yours).")
    await hub.broadcast_warnings()
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Browsers only; require a logged-in org member (session loaded from cookie).
    if not _ws_origin_ok(ws):
        await ws.close(code=1008)   # policy violation (cross-origin)
        return
    user = ws.session.get("user")
    if not user:
        await ws.close(code=1008)   # policy violation
        return
    await ws.accept()
    sess = hub.get(user)
    # Cap tabs per member: a reconnect storm would otherwise grow ws_clients (and
    # the O(tabs) broadcast fan-out) without bound. Real usage is a handful of tabs.
    if len(sess.ws_clients) >= WS_MAX_CLIENTS_PER_MEMBER:
        await ws.close(code=1013)   # try again later
        return
    was_offline = not sess.ws_clients   # first tab for this member?
    sess.ws_clients.add(ws)
    async with hub.lock:
        hub.mark_online(sess)           # join the who's-online roster (#19)
    try:
        # Send this member's current state immediately so the UI isn't blank
        # until their next /showlocation.
        await ws.send_text(
            json.dumps(
                {
                    "type": "state",
                    "data": sess.nav_state,
                    "capture": sess.capture_status(),
                }
            )
        )
        # Initial teammate snapshot so the new tab's map/roster start populated
        # (later changes arrive as throttled presence deltas). The who's-online
        # roster ships alongside so the new tab can render #/online immediately.
        async with hub.lock:
            roster = hub.roster()
            online = hub.online_roster()
            board = hub.lfg_board()
            warnings = hub.warnings_board()
        await ws.send_text(json.dumps({"type": "roster", "users": roster}))
        await ws.send_text(json.dumps({"type": "online_roster", "users": online}))
        await ws.send_text(json.dumps({"type": "lfg", "entries": board}))
        await ws.send_text(json.dumps({"type": "warnings", "entries": warnings}))
        # Tell the new tab the current count immediately; tell everyone else only
        # when this member actually came online (a 2nd/3rd tab doesn't change it,
        # but their arrival does add them to everyone's online roster).
        if was_offline:
            await hub.broadcast_online()
            await hub.broadcast_online_roster()
        else:
            await ws.send_text(json.dumps({"type": "online", "count": hub.online_count()}))
        while True:
            await ws.receive_text()  # client pings: bump the online heartbeat
            async with hub.lock:
                readded = hub.mark_online(sess)   # re-add if a stale prune dropped us
            if readded:
                await hub.broadcast_online()
                await hub.broadcast_online_roster()
    except WebSocketDisconnect:
        pass
    finally:
        sess.ws_clients.discard(ws)
        if not sess.ws_clients:   # member's last tab closed — they went offline
            async with hub.lock:
                hub.drop_online(user["id"])
            await hub.broadcast_online()
            await hub.broadcast_online_roster()
            # LFG posts intentionally persist past a disconnect — they age off by the
            # clock (green→stale→gone), so closing a tab no longer drops them.


# ---------------------------------------------------------------------------
# Discord OAuth gate (Phase 0)
# ---------------------------------------------------------------------------
# Login + org-membership check + signed session for browsers; bearer watcher
# tokens for the headless watcher. The auth_gate middleware enforces "any /api/*
# needs one of these" centrally; the dependencies below add the finer checks
# (session-only, admin-only).


# The OAuth CSRF token rides in its own short-lived cookie rather than the Lax
# session, because Discord's "Authorize" button is a cross-site POST that
# redirects to /auth/callback. Chrome's "Lax+POST" grace still sends a Lax
# cookie there, but Safari/WebKit (iPhone) does not — so a Lax session lost the
# state and every mobile login 400'd with "invalid OAuth state". SameSite=None
# (only valid alongside Secure, i.e. over HTTPS) is sent on that redirect. Kept
# separate from the session cookie so the rest of the app stays Lax.
OAUTH_STATE_COOKIE = "oauth_state"


def _set_oauth_state_cookie(resp: Response, state: str) -> None:
    resp.set_cookie(
        OAUTH_STATE_COOKIE, state,
        max_age=600, httponly=True, secure=COOKIE_SECURE,
        samesite="none" if COOKIE_SECURE else "lax", path="/auth",
    )


def _clear_oauth_state_cookie(resp: Response) -> None:
    resp.delete_cookie(OAUTH_STATE_COOKIE, path="/auth")


@app.get("/auth/login")
async def auth_login(request: Request):
    if not auth.configured():
        raise HTTPException(status_code=503, detail="Discord login is not configured")
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(auth.authorize_url(state))
    _set_oauth_state_cookie(resp, state)
    return resp


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    expected = request.cookies.get(OAUTH_STATE_COOKIE)
    if not state or state != expected:
        raise HTTPException(status_code=400, detail="invalid OAuth state")
    if not code:
        raise HTTPException(status_code=400, detail="missing authorization code")
    try:
        token = await asyncio.to_thread(auth.exchange_code, code)
        profile, denied = await asyncio.to_thread(
            auth.fetch_member_profile, token, member_role_id(), admin_ids())
    except Exception as exc:
        # A urllib HTTPError carries Discord's JSON error body (e.g.
        # {"error":"invalid_client"}); read it and log to stdout so the real
        # reason shows up in `docker logs`, not just an opaque 502.
        body = ""
        if hasattr(exc, "read"):
            try:
                body = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
        print(f"[sc-nav] auth callback failed: {exc!r} {body}", flush=True)
        traceback.print_exc()
        # Detail (which may carry Discord's raw error body) is logged above only;
        # the client gets a generic message so we don't disclose internals.
        raise HTTPException(status_code=502, detail="Discord authentication failed; please try again.")
    if profile is None:
        request.session.clear()
        html = auth.MISSING_ROLE_HTML if denied == "missing_role" else auth.NOT_IN_ORG_HTML
        resp = HTMLResponse(html, status_code=403)
        _clear_oauth_state_cookie(resp)
        return resp
    request.session["user"] = profile
    # Persist the Discord identity so display names survive past the session cookie
    # and back the member directory (step: docs/member-identity-and-directory.md).
    try:
        members_dir.upsert(profile)
    except Exception as exc:                       # never block sign-in on this
        print(f"[sc-nav] member upsert failed: {exc!r}", flush=True)
    resp = RedirectResponse("/")
    _clear_oauth_state_cookie(resp)
    return resp


@app.post("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return {"ok": True}


def _member_identity(discord_id: str) -> dict:
    """The member's identity block for the UI: their verified (watcher-bound)
    handles, the chosen primary handle (defaulting to the most-recent bound one
    when unset), and the directory opt-out. Read-only — picking a primary is an
    explicit PUT, so the default here never writes."""
    owned = handles.handles_for(discord_id)
    member = members_dir.get(discord_id) or {}
    primary = member.get("primary_handle")
    if primary not in owned:                    # unset, or stale after a rename
        primary = owned[0] if owned else None
    return {"handles": owned, "primary_handle": primary,
            "directory_opt_out": bool(member.get("directory_opt_out"))}


@app.get("/api/me")
async def api_me(user: dict = Depends(require_session)):
    """The signed-in org member (or 401). Drives the UI's account state. Carries
    the live presence-share flag so the UI's toggle reflects the current state,
    plus the member's handles + primary-handle + directory preference."""
    motd = motd_state()
    return {**user, "share_presence": hub.get(user).share_presence,
            "org_logo": bool(db.get_setting("org_logo_ext")),
            "org_name": org_name(),
            "motd": motd["text"], "motd_updated": motd["updated"],
            "ships": db.list_user_ships(user["id"]),
            "playstyle_tags": member_playstyles(members_dir.get(user["id"])),
            **_member_identity(user["id"])}


class ProfileIn(BaseModel):
    share_presence: bool | None = None
    # Declared playstyle tags (#30). None = untouched; [] = clear. Oversized
    # payloads are rejected outright (mirrors LFGPostIn.tags).
    playstyle_tags: list[str] | None = Field(default=None, max_length=_PROFILE_MAX_TAGS)


class ShipPrefIn(BaseModel):
    """A member's learned usable-SCU for a ship (the cargo-planner override)."""
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    usable_scu: float = Field(ge=0, le=100_000)


@app.put("/api/me")
async def update_me(body: ProfileIn, user: dict = Depends(require_session)):
    """Update the caller's profile: the presence-share toggle (turning it off
    emits a `remove` and stops broadcasting the member — one-way, they keep
    receiving teammates; turning it on re-publishes their last fix) and the
    declared playstyle tags (#30), normalized like LFG tags — dedup, allowlist,
    cap — then mirrored onto their live online-roster record so org-mates see
    the change without waiting for a reconnect."""
    uid = user["id"]
    tags = None
    if body.playstyle_tags is not None:
        tags = [t for t in dict.fromkeys(body.playstyle_tags)
                if t in PLAYSTYLE_TAGS][:_PROFILE_MAX_TAGS]
        members_dir.set_playstyles(uid, tags)
    async with hub.lock:
        sess = hub.get(user)
        if body.share_presence is not None:
            sess.share_presence = body.share_presence
            hub.touch_presence(sess)   # re-publish, or drop if now off / not on a body
        if tags is not None:
            rec = hub.online.get(uid)
            if rec is not None:
                rec["tags"] = tags
    if tags is not None:
        await hub.broadcast_online_roster()
    out = {"ok": True, "share_presence": sess.share_presence}
    if tags is not None:
        out["playstyle_tags"] = tags
    return out


class PrimaryHandleIn(BaseModel):
    handle: str | None = Field(default=None, max_length=_HANDLE_MAX)


@app.put("/api/me/primary-handle")
async def set_primary_handle(body: PrimaryHandleIn, user: dict = Depends(require_session)):
    """Choose which of the caller's in-game handles is shown across the apps. Must
    be one a watcher has bound to them (a verified handle), or null to clear back
    to the default. Rejecting unowned handles keeps the picker honest — you can't
    claim a handle that isn't yours."""
    handle = (body.handle or "").strip() or None
    if handle is not None and not handles.owns_handle(user["id"], handle):
        raise HTTPException(status_code=400,
                            detail="that handle isn't bound to your account")
    members_dir.set_primary_handle(user["id"], handle)
    return {"ok": True, **_member_identity(user["id"])}


class DirectoryOptIn(BaseModel):
    opt_out: bool


@app.put("/api/me/directory-opt-out")
async def set_directory_opt_out(body: DirectoryOptIn, user: dict = Depends(require_session)):
    """Hide the caller from member-facing directory surfaces. Cosmetic with
    respect to admins, who always see everyone (the Discord<->handle link already
    exists in the handle registry); the UI says so plainly."""
    members_dir.set_opt_out(user["id"], body.opt_out)
    return {"ok": True, "directory_opt_out": body.opt_out}


@app.get("/api/intel/directory")
async def member_directory(admin: dict = Depends(require_admin)):
    """Admin-only member directory: the cross-walk between each member's Discord
    identity (org nick / display name) and their watcher-bound in-game handle(s).
    Admin-only by design; a member's `opt_out` is surfaced here (admins always see
    everyone — the link already exists in the handle registry) so the UI can flag
    opted-out rows, and it filters them out of any future member-facing view.
    docs/member-identity-and-directory.md."""
    rows = []
    for did, m in members_dir.by_id.items():
        owned = handles.handles_for(did)
        primary = m.get("primary_handle")
        if primary not in owned:
            primary = owned[0] if owned else None
        rows.append({
            "discord_id": did,
            "display_name": m.get("guild_nick") or m.get("display_name"),
            "username": m.get("username"),
            "handles": owned,
            "primary_handle": primary,
            "playstyle_tags": member_playstyles(m),
            "opt_out": bool(m.get("directory_opt_out")),
            "last_login": m.get("last_login"),
            "is_admin": did in admin_ids(),
        })
    rows.sort(key=lambda r: (r["display_name"] or r["username"] or r["discord_id"]).lower())
    return {"members": rows, "total": len(rows)}


@app.delete("/api/me")
async def delete_me(request: Request, user: dict = Depends(require_session)):
    """Self-service account deletion (Privacy Policy). Erases the caller's
    personal data — watcher tokens, saved ships, cargo runs, handle->Discord
    bindings, hauling-session marker — and de-identifies their contributed POIs/
    sightings (kept for the org, stripped of owner). The browser session is
    cleared so they're signed out; signing in again just creates a fresh, empty
    account (deletion erases data, it doesn't ban the Discord member)."""
    uid = user["id"]
    async with hub.lock:
        player_ids = handles.player_ids_for(uid)
        counts = db.delete_member(uid, player_ids)

        # Mirror the DB changes in the in-memory caches so nothing stale survives
        # until the next restart. 1) Drop this member's private POIs outright, then
        # de-identify the rest of their live contributions.
        for poi in [p for p in nav.pois.values()
                    if p.owner_id in player_ids and getattr(p, "private", False)]:
            nav.pois.pop(poi.id, None)
            hub.forget_entity(poi.id)
        for poi in nav.pois.values():
            if poi.owner_id in player_ids:
                poi.owner_id = poi.owner_handle = None
        for obs in nav.observations.values():
            if obs.owner_id in player_ids:
                obs.owner_id = obs.owner_handle = None
        # 2) Forget their handle bindings + watcher tokens.
        handles.by_handle = {h: e for h, e in handles.by_handle.items()
                             if e.get("discord_id") != uid}
        tokens.items = [t for t in tokens.items if t["discord_id"] != uid]
        members_dir.forget(uid)
        # 3) Drop their live presence + session so teammates see them leave.
        hub.drop_presence(uid)
        hub.sessions.pop(uid, None)
        await hub.broadcast_all()
    await hub.broadcast_online()
    request.session.clear()
    return {"ok": True, "deleted": counts}


@app.put("/api/me/ship")
async def remember_ship(body: ShipPrefIn, user: dict = Depends(require_session)):
    """Save (or update) the caller's usable-SCU for a ship and mark it most
    recently used. Returns the caller's saved fleet, freshest first."""
    db.upsert_user_ship(user["id"], body.name.strip(), body.usable_scu,
                        datetime.now(timezone.utc).isoformat())
    return {"ok": True, "ships": db.list_user_ships(user["id"])}


@app.delete("/api/me/ship")
async def forget_ship(name: str, user: dict = Depends(require_session)):
    """Drop a saved ship from the caller's fleet."""
    if not db.delete_user_ship(user["id"], name.strip()):
        raise HTTPException(status_code=404, detail="no such saved ship")
    return {"ok": True, "ships": db.list_user_ships(user["id"])}


class MemberBlueprintIn(BaseModel):
    blueprint_key: str = Field(min_length=1, max_length=_META_MAX)


def _member_blueprint_view(rows) -> list[dict]:
    """Enrich saved blueprint keys with feed name/category for display. A key whose
    recipe left the feed on a re-sync still lists (name falls back to the key)."""
    out = []
    for r in rows:
        key = r["blueprint_key"]
        bp = blueprints_feed.get(key)
        out.append({"blueprint_key": key, "added_at": r.get("added_at"),
                    "name": (bp or {}).get("name") or key,
                    "cat": (bp or {}).get("cat"), "available": bp is not None})
    return out


@app.get("/api/me/blueprints")
async def my_blueprints(user: dict = Depends(require_session)):
    """The caller's blueprint library — recipes they own / can craft (#25.1). Powers
    the 'requests I can craft' board filter and the 'seed a craft goal' quick-pick."""
    return {"blueprints": _member_blueprint_view(db.list_member_blueprints(user["id"]))}


@app.post("/api/me/blueprints")
async def add_my_blueprint(body: MemberBlueprintIn, user: dict = Depends(require_session)):
    """Add a recipe to the caller's library (must resolve in the blueprint feed)."""
    key = body.blueprint_key.strip()
    if key not in blueprints_feed:
        raise HTTPException(status_code=404, detail="unknown blueprint")
    db.add_member_blueprint(user["id"], key, datetime.now(timezone.utc).isoformat())
    return {"ok": True,
            "blueprints": _member_blueprint_view(db.list_member_blueprints(user["id"]))}


@app.delete("/api/me/blueprints")
async def remove_my_blueprint(key: str, user: dict = Depends(require_session)):
    """Remove a recipe from the caller's library."""
    if not db.delete_member_blueprint(user["id"], key.strip()):
        raise HTTPException(status_code=404, detail="not in your library")
    return {"ok": True,
            "blueprints": _member_blueprint_view(db.list_member_blueprints(user["id"]))}


class TokenCreateIn(BaseModel):
    label: str = Field(default="watcher", max_length=_LABEL_MAX)


@app.post("/api/tokens")
async def create_token(request: Request, body: TokenCreateIn):
    """Mint a watcher token for the signed-in member. The raw token is returned
    once and never stored in the clear."""
    user = require_session(request)
    raw, public = tokens.mint(user["id"], user.get("display_name"), body.label)
    return {"token": raw, **public}


@app.get("/api/tokens")
async def list_tokens(request: Request):
    user = require_session(request)
    return tokens.list_for(user["id"])


@app.delete("/api/tokens/{token_id}")
async def delete_token(request: Request, token_id: str):
    user = require_session(request)
    if not tokens.revoke(token_id, user["id"], user.get("is_admin", False)):
        raise HTTPException(status_code=404, detail="unknown token")
    return {"ok": True}


def _server_base_url(request: Request) -> str:
    """Public base URL the watcher should POST to.

    Prefer the explicitly configured SC_NAV_PUBLIC_URL — the watcher zip bakes a
    freshly minted token into the bundled bat's SERVER=, so an attacker who could
    spoof X-Forwarded-Host (reachable only if the app is exposed off-tunnel)
    could otherwise redirect a victim's watcher — and its token — to their own
    server. Falling back to the forwarded headers keeps dev/no-config working."""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc)
    return f"{scheme}://{host}"


def _build_watcher_zip(base_url: str, token: str) -> bytes:
    """Zip the watcher up with this member's setup baked in: the bat is pointed
    at `base_url`, and `watcher_config.json` carries the token so the script
    authenticates with zero typing (the watcher reads token stickily from there).
    Returned under a top-level `watcher/` folder for a clean unzip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in WATCHER_BUNDLE_FILES:
            src = WATCHER_DIR / name
            if not src.is_file():
                continue
            text = src.read_text(encoding="utf-8")
            if name == "run_watcher.bat":
                # Replace the SERVER= line (whatever address it was committed with)
                # with this deployment's URL so the user never edits the bat.
                text = re.sub(r"(?m)^set SERVER=.*$", f"set SERVER={base_url}", text)
            zf.writestr(f"watcher/{name}", text)
        # The token lives in watcher_config.json, the script's sticky-config file.
        zf.writestr("watcher/watcher_config.json", json.dumps({"token": token}))
    return buf.getvalue()


@app.post("/download/watcher")
async def download_watcher(request: Request):
    """Mint a token for the signed-in member and stream back a personalized,
    ready-to-run watcher bundle (Setup page, step 2). The token is baked into the
    zip rather than put in a URL, so it never lands in a log or browser history."""
    user = require_session(request)
    if WATCHER_DIR is None:
        raise HTTPException(status_code=503, detail="watcher bundle unavailable")
    raw, _ = tokens.mint(user["id"], user.get("display_name"), "watcher download")
    data = _build_watcher_zip(_server_base_url(request), raw)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="watcher.zip"'},
    )


# The SPA shell is served through a route (not the StaticFiles mount) so the
# per-request CSP nonce can be stamped onto its single inline <script>. Routing
# is hash-based, so "/" is the only path that serves the shell; "/index.html" is
# handled too in case it's hit directly. Read per request so a dev edit shows up
# without a restart (matching StaticFiles); the file is small and only read on a
# full page load, not per API call. All other assets fall through to the mount.
INDEX_FILE = STATIC_DIR / "index.html"


def _index_response(request: Request) -> HTMLResponse:
    nonce = getattr(request.state, "csp_nonce", "")
    html = (
        INDEX_FILE.read_text(encoding="utf-8")
        .replace("<script>", f'<script nonce="{nonce}">', 1)
        # Stamp the running version into the footer (placeholder degrades to the
        # current version; the string is our own SemVer, so no escaping needed).
        .replace("{{APP_VERSION}}", APP_VERSION)
    )
    # Never cache the shell: a cached document would pin a stale nonce that no
    # longer matches the per-request CSP header, dead-scripting the app.
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _index_response(request)


@app.get("/index.html", response_class=HTMLResponse)
async def index_html(request: Request):
    return _index_response(request)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
