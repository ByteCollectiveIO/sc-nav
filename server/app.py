"""SC Nav server.

Receives positions from the Windows clipboard watcher, computes navigation
state against the containers/poi dataset, and pushes live updates to browser
clients over WebSocket.

Run:  uvicorn app:app --host 0.0.0.0 --port 8765
Data: ../poi by default, override with SC_NAV_DATA=/path/to/poi
"""

import asyncio
import hashlib
import json
import os
import secrets
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import auth
import nav_core

DATA_DIR = Path(os.environ.get("SC_NAV_DATA", Path(__file__).parent.parent / "poi"))
STATIC_DIR = Path(__file__).parent / "static"

# Live dataset endpoints (the files in DATA_DIR act as the offline cache).
OC_URL = os.environ.get("SC_NAV_OC_URL", "https://starmap.space/api/v3/oc/index.php")
POI_URL = os.environ.get("SC_NAV_POI_URL", "https://starmap.space/api/v3/pois/index.php")
COMMODITIES_URL = os.environ.get("SC_NAV_COMMODITIES_URL", "https://api.uexcorp.uk/2.0/commodities")
OFFLINE = os.environ.get("SC_NAV_OFFLINE") == "1"

data_info = {"source": None, "fetched_at": None, "error": None}


def _fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "sc-nav/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_nav_data() -> nav_core.NavData:
    """Fetch live data from starmap.space; fall back to the on-disk cache.

    A successful fetch refreshes the cache files, so the newest good dataset
    survives restarts and network outages.
    """
    if not OFFLINE:
        try:
            oc_raw = _fetch_json(OC_URL)
            poi_raw = _fetch_json(POI_URL)
            if len(oc_raw) < 50 or len(poi_raw) < 100:
                raise ValueError(
                    f"suspiciously small dataset ({len(oc_raw)} containers, "
                    f"{len(poi_raw)} pois) — keeping cache"
                )
            fresh = nav_core.parse_data(oc_raw, poi_raw)
            try:
                (DATA_DIR / "containers.json").write_text(json.dumps(oc_raw))
                (DATA_DIR / "poi.json").write_text(json.dumps(poi_raw))
            except OSError as exc:
                print(f"[sc-nav] cache write failed (continuing): {exc}")
            data_info.update(
                source="live",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )
            return fresh
        except Exception as exc:
            data_info["error"] = str(exc)
            print(f"[sc-nav] live fetch failed, using cached data: {exc}")
    data_info["source"] = "offline" if OFFLINE else "cache"
    return nav_core.load_data(DATA_DIR)


CUSTOM_POI_FILE = DATA_DIR / "custom_pois.json"
HANDLES_FILE = DATA_DIR / "handles.json"
COMMODITIES_FILE = DATA_DIR / "commodities.json"  # cached uexcorp commodities
# One file per observation category (resource_nodes.json, wildlife.json, …),
# defined centrally in nav_core so adding a category needs no new wiring here.
OBSERVATION_FILES = {
    cat: DATA_DIR / spec["file"] for cat, spec in nav_core.OBSERVATION_CATEGORIES.items()
}


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


def load_custom_pois() -> list[dict]:
    return _load_json_list(CUSTOM_POI_FILE)


def save_custom_pois(custom: list[dict]) -> None:
    _save_json_list(CUSTOM_POI_FILE, custom)


class HandleRegistry:
    """Maps in-game handles to stable assigned PlayerIDs.

    The PlayerID (not the raw handle) is the key attached to contributions, so
    a character rename keeps a player's history intact."""

    def __init__(self, path: Path):
        self.path = path
        self.by_handle = {h["handle"]: h for h in _load_json_list(path)}

    def register(self, handle: str) -> dict:
        handle = handle.strip()
        now = datetime.now(timezone.utc).isoformat()
        entry = self.by_handle.get(handle)
        if entry is None:
            next_id = max((e["player_id"] for e in self.by_handle.values()), default=0) + 1
            entry = {"player_id": next_id, "handle": handle, "first_seen": now, "last_seen": now}
            self.by_handle[handle] = entry
            # Persist only when a genuinely new handle appears — this runs on
            # the position hot path (every /showlocation), so we must not
            # rewrite the file on every sample just to bump last_seen.
            try:
                _save_json_list(self.path, list(self.by_handle.values()))
            except OSError as exc:
                print(f"[sc-nav] handle registry save failed: {exc}")
        else:
            entry["last_seen"] = now  # in-memory only; not worth a disk write per position
        return entry

    def list(self) -> list[dict]:
        return sorted(self.by_handle.values(), key=lambda e: e["handle"].lower())


WATCHER_TOKENS_FILE = DATA_DIR / "watcher_tokens.json"


class TokenStore:
    """Per-user watcher tokens. The headless watcher can't do OAuth, so an org
    member mints a token in the web UI and the watcher sends it as a bearer
    token. Tokens are stored hashed (only the hash is persisted) and bound to
    the minting member's Discord id; admin status is resolved live."""

    def __init__(self, path: Path):
        self.path = path
        self.items = _load_json_list(path)

    @staticmethod
    def _hash(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _public(t: dict) -> dict:
        return {"id": t["id"], "label": t["label"],
                "created": t["created"], "last_used": t["last_used"]}

    def resolve(self, raw: str) -> dict | None:
        """A raw bearer token -> the owning member (id/display_name/is_admin),
        or None. last_used is bumped in memory only (avoids a disk write per
        position, like HandleRegistry)."""
        h = self._hash(raw)
        for t in self.items:
            if secrets.compare_digest(t["hash"], h):
                t["last_used"] = datetime.now(timezone.utc).isoformat()
                return {
                    "id": t["discord_id"],
                    "display_name": t.get("display_name"),
                    "is_admin": t["discord_id"] in auth.ADMIN_IDS,
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
        self.items.append(entry)
        self._save()
        return raw, self._public(entry)

    def list_for(self, discord_id: str) -> list[dict]:
        return [self._public(t) for t in self.items if t["discord_id"] == discord_id]

    def revoke(self, token_id: str, discord_id: str, is_admin: bool) -> bool:
        for i, t in enumerate(self.items):
            if t["id"] == token_id and (is_admin or t["discord_id"] == discord_id):
                self.items.pop(i)
                self._save()
                return True
        return False

    def _save(self) -> None:
        try:
            _save_json_list(self.path, self.items)
        except OSError as exc:
            print(f"[sc-nav] watcher token save failed: {exc}")


def merge_all_observations(target_nav) -> None:
    for category, items in observations.items():
        nav_core.merge_observations(target_nav, items, category)


app = FastAPI(title="SC Nav")


# Auth gate: every /api/* call needs a logged-in org member (session) or a valid
# watcher token; the SPA shell, /auth/* and /api/health stay open. Registered
# BEFORE SessionMiddleware below so that (being the inner layer) it runs after
# the session has been loaded from the cookie.
@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path == "/api/health":
        return await call_next(request)
    if request.session.get("user") or token_user(request):
        return await call_next(request)
    return JSONResponse({"detail": "not authenticated"}, status_code=401)


# Signed session cookie (Discord login state). The secret must be stable across
# restarts so sessions survive a redeploy; a random fallback keeps dev working.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
    https_only=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
    same_site="lax",
    max_age=8 * 3600,
)

nav = load_nav_data()
custom_pois = load_custom_pois()
# observations[category] -> list of stored dicts (one JSON file each)
observations = {cat: _load_json_list(path) for cat, path in OBSERVATION_FILES.items()}
handles = HandleRegistry(HANDLES_FILE)
tokens = TokenStore(WATCHER_TOKENS_FILE)
raw_commodity_names = load_raw_commodity_names()
fauna_names = load_fauna_names()
biomes = load_biomes()
nav_core.merge_custom_pois(nav, custom_pois)
merge_all_observations(nav)
nav_core.assign_qt_markers(nav)


# --- auth dependencies (defined before the endpoints that use them) ---------
def current_user(request: Request) -> dict | None:
    return request.session.get("user")


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


class PositionIn(BaseModel):
    x: float
    y: float
    z: float
    raw: str | None = None
    client_time: str | None = None
    source: str | None = None
    handle: str | None = None


class DestinationIn(BaseModel):
    poi_id: int


class CaptureIn(BaseModel):
    name: str
    type: str = "Custom"
    qt_marker: bool = False   # record as a jumpable QT marker (e.g. an OM)


class NodeCaptureIn(BaseModel):
    ore: str
    band: int | str | None = None   # 1-8, or "Unk"/None when not yet mined
    biome: str | None = None
    note: str | None = None


class WildlifeCaptureIn(BaseModel):
    species: str
    biome: str | None = None
    note: str | None = None


# Breadcrumb trail tuning. In-memory and session-scoped (lost on restart).
PATH_MIN_MOVE_M = 250.0   # don't record a crumb until you've moved this far
PATH_MAX = 5000           # cap so a long session can't grow unbounded


class AppState:
    """Latest sample + destination + breadcrumb trail + websocket clients."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.pos = None
        self.t = None
        self.prev_pos = None
        self.prev_t = None
        self.destination_id = None
        self.nav_state = None
        # capture_pending: {"kind": "poi"} or
        # {"kind": "observation", "category", "data", "biome", "note"} while armed
        self.capture_pending = None
        self.last_capture = None      # summary of most recent capture
        self.owner = None             # {"player_id","handle"} from latest position
        # Breadcrumb trail (in-memory, session-scoped). One trail, matching the
        # single live position cursor (state.pos): the app tracks one active
        # stream at a time, so a single global path is the coherent model.
        # Crumb: {lat, lon, container}.
        self.tracking = False
        self.path = []
        # Monotonic observation id, so a deleted top id is never reused.
        self.obs_id_seq = max([*nav.observations.keys(), nav_core.OBSERVATION_ID_START - 1])
        self.clients: set[WebSocket] = set()

    def next_observation_id(self):
        self.obs_id_seq += 1
        return self.obs_id_seq

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
        self.nav_state = nav_core.compute_state(
            nav,
            self.pos,
            self.t,
            destination_id=self.destination_id,
            prev_pos=self.prev_pos,
            prev_t=self.prev_t,
        )
        self._attach_breadcrumbs()

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
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


state = AppState()


@app.post("/api/position")
async def post_position(body: PositionIn):
    async with state.lock:
        now = time.time()
        # Ignore exact duplicates within a second (watcher heartbeat); still
        # rebroadcast so freshly-opened UIs get state.
        new_pos = (body.x, body.y, body.z)
        if state.pos is not None and new_pos != state.pos:
            state.prev_pos, state.prev_t = state.pos, state.t
        state.pos, state.t = new_pos, now

        if body.handle:
            entry = handles.register(body.handle)
            state.owner = {"player_id": entry["player_id"], "handle": entry["handle"]}

        if state.capture_pending is not None:
            pending = state.capture_pending
            state.capture_pending = None
            owner = state.owner or {}
            if pending["kind"] == "observation":
                _capture_observation(new_pos, now, pending, owner)
            else:
                _capture_poi(new_pos, now, pending, owner)

        state.recompute()
        state.record_crumb()
        await state.broadcast()
    return {"ok": True}


def _capture_poi(pos_m, now, pending, owner):
    next_id = max(
        (c["id"] for c in custom_pois), default=nav_core.CUSTOM_ID_START - 1
    ) + 1
    poi = nav_core.custom_poi_from_position(
        nav, pos_m, now, pending["name"], pending["type"], next_id,
        owner_id=owner.get("player_id"), owner_handle=owner.get("handle"),
        qt_marker=pending.get("qt_marker", False),
    )
    custom_pois.append(nav_core.custom_poi_to_dict(poi))
    try:
        save_custom_pois(custom_pois)
    except OSError as exc:
        print(f"[sc-nav] custom poi save failed: {exc}")
    nav.pois[poi.id] = poi
    # A new QT marker changes the nearest-jump answer for every other entity,
    # so rebuild the index + reassign nearest_qt across the dataset.
    if poi.qt_marker:
        nav_core.assign_qt_markers(nav)
    state.last_capture = {
        "kind": "poi", "id": poi.id, "name": poi.name, "type": poi.type,
        "container": poi.container_name or "Space", "system": poi.system,
        "latitude": poi.latitude, "longitude": poi.longitude,
        "qt_marker": poi.qt_marker,
        "owner_handle": poi.owner_handle,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _capture_observation(pos_m, now, pending, owner):
    category = pending["category"]
    items = observations[category]
    # Monotonic across all categories -> unique, and never reuses a deleted id.
    next_id = state.next_observation_id()
    obs = nav_core.observation_from_position(
        nav, pos_m, now, category, pending["data"], next_id,
        biome=pending.get("biome"), note=pending.get("note"),
        owner_id=owner.get("player_id"), owner_handle=owner.get("handle"),
    )
    items.append(nav_core.observation_to_dict(obs))
    try:
        _save_json_list(OBSERVATION_FILES[category], items)
    except OSError as exc:
        print(f"[sc-nav] observation save failed: {exc}")
    nav.observations[obs.id] = obs
    state.last_capture = {
        **nav_core._observation_base(obs),
        "latitude": obs.latitude, "longitude": obs.longitude,
        "captured_at": obs.observed_at,
    }


@app.get("/api/state")
async def get_state():
    return {
        "state": state.nav_state,
        "destination_id": state.destination_id,
        "capture": state.capture_status(),
        "systems": nav.systems,
    }


@app.get("/api/pois")
async def get_pois(
    q: str = "", system: str | None = None, container: str | None = None,
    type: str | None = None, owner_id: int | None = None, limit: int = 25,
):
    return nav_core.search_pois(
        nav, query=q, system=system, container=container, poi_type=type,
        owner_id=owner_id, limit=min(limit, 5000),
    )


@app.post("/api/destination")
async def set_destination(body: DestinationIn):
    target = nav.pois.get(body.poi_id) or nav.observations.get(body.poi_id)
    if target is None:
        raise HTTPException(status_code=404, detail="unknown poi_id")
    async with state.lock:
        state.destination_id = body.poi_id
        state.recompute()
        await state.broadcast()
    if isinstance(target, nav_core.Observation):
        name = nav_core.OBSERVATION_CATEGORIES[target.category]["display_name"](target.data)
    else:
        name = target.name
    return {"ok": True, "destination": {"id": body.poi_id, "name": name}}


@app.delete("/api/destination")
async def clear_destination():
    async with state.lock:
        state.destination_id = None
        state.recompute()
        await state.broadcast()
    return {"ok": True}


@app.post("/api/capture/start")
async def capture_start(body: CaptureIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    async with state.lock:
        if state.capture_pending is not None:
            raise HTTPException(status_code=409, detail="another capture is already armed; cancel it first")
        state.capture_pending = {
            "kind": "poi", "name": name, "type": body.type.strip() or "Custom",
            "qt_marker": body.qt_marker,
        }
        await state.broadcast()
    return {"ok": True, "capture": state.capture_status()}


async def _arm_observation(category, data, biome, note):
    async with state.lock:
        if state.capture_pending is not None:
            raise HTTPException(status_code=409, detail="another capture is already armed; cancel it first")
        state.capture_pending = {
            "kind": "observation",
            "category": category,
            "data": data,
            "biome": (biome or "").strip() or None,
            "note": (note or "").strip() or None,
        }
        await state.broadcast()
    return {"ok": True, "capture": state.capture_status()}


@app.post("/api/capture/node")
async def capture_node_start(body: NodeCaptureIn):
    ore = body.ore.strip()
    if not ore:
        raise HTTPException(status_code=400, detail="ore is required")
    # band passed through raw; _normalize_resource handles "Unk"/None.
    return await _arm_observation(
        "resource", {"ore": ore, "band": body.band}, body.biome, body.note
    )


@app.post("/api/capture/wildlife")
async def capture_wildlife_start(body: WildlifeCaptureIn):
    species = body.species.strip()
    if not species:
        raise HTTPException(status_code=400, detail="species is required")
    return await _arm_observation("wildlife", {"species": species}, body.biome, body.note)


@app.post("/api/capture/cancel")
async def capture_cancel():
    async with state.lock:
        state.capture_pending = None
        await state.broadcast()
    return {"ok": True}


@app.get("/api/handles")
async def list_handles():
    return handles.list()


@app.get("/api/raw_commodities")
async def list_raw_commodities():
    """Raw-ore names (uexcorp is_raw==1) for the ore datalist."""
    return raw_commodity_names


@app.get("/api/fauna")
async def list_fauna():
    """Curated fauna/species names for the Add Fauna datalist."""
    return fauna_names


@app.get("/api/resource_cells")
async def get_resource_cells(system: str, body: str):
    """Per-cell ore composition for the map heatmap (cells with ≥1 sighting)."""
    cont = nav.containers.get((system, body))
    if cont is None or not cont.is_body:
        raise HTTPException(status_code=404, detail="unknown body")
    cells = nav_core.resource_cells(nav, system, body, cont.body_radius)
    return {"cell_m": nav_core.RESOURCE_CELL_M, "cells": cells}


@app.get("/api/resource_ores")
async def get_resource_ores():
    """Ore names present in resource sightings (element-finder picker)."""
    return nav_core.resource_ore_names(nav)


@app.get("/api/resource_hotspots")
async def get_resource_hotspots(
    ore: str, system: str | None = None, body: str | None = None,
    limit: int = 20, sort: str = "likely",
):
    """Known areas richest in `ore`, ranked. sort: likely | near | value.
    The 'near'/'value' modes use the live position to factor travel distance."""
    return {
        "ore": ore,
        "sort": sort,
        "has_position": state.pos is not None,
        "cell_m": nav_core.RESOURCE_CELL_M,
        "hotspots": nav_core.resource_hotspots(
            nav, ore, system=system, body=body, limit=min(limit, 100),
            from_pos=state.pos, t_ref=state.t, sort=sort,
        ),
    }


@app.get("/api/biomes")
async def list_biomes():
    """Biome lookups (by_body / by_system / all) for the biome datalist; the
    UI narrows to the player's current body, falling back to system then all."""
    return biomes


@app.get("/api/custom_pois")
async def list_custom_pois():
    return custom_pois


@app.delete("/api/custom_pois/{poi_id}")
async def delete_custom_poi(poi_id: int):
    async with state.lock:
        idx = next((i for i, c in enumerate(custom_pois) if c["id"] == poi_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="unknown custom poi")
        removed = custom_pois.pop(idx)
        save_custom_pois(custom_pois)
        nav.pois.pop(poi_id, None)
        # Removing a QT marker leaves other entities pointing at a marker that's
        # gone, so rebuild the index + reassign nearest_qt across the dataset.
        if removed.get("qt_marker"):
            nav_core.assign_qt_markers(nav)
        if state.destination_id == poi_id:
            state.destination_id = None
        if state.last_capture and state.last_capture["id"] == poi_id:
            state.last_capture = None
        state.recompute()
        await state.broadcast()
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
async def delete_observation(obs_id: int):
    async with state.lock:
        for category, items in observations.items():
            idx = next((i for i, o in enumerate(items) if o["id"] == obs_id), None)
            if idx is None:
                continue
            items.pop(idx)
            _save_json_list(OBSERVATION_FILES[category], items)
            nav.observations.pop(obs_id, None)
            if state.destination_id == obs_id:
                state.destination_id = None
            if state.last_capture and state.last_capture.get("id") == obs_id:
                state.last_capture = None
            state.recompute()
            await state.broadcast()
            return {"ok": True}
        raise HTTPException(status_code=404, detail="unknown observation")


@app.post("/api/path/{action}")
async def path_control(action: str):
    if action not in ("start", "stop", "clear"):
        raise HTTPException(status_code=404, detail="unknown path action")
    async with state.lock:
        if action == "start":
            state.tracking = True
        elif action == "stop":
            state.tracking = False
        else:  # clear
            state.path.clear()
        if state.nav_state is not None:
            state._attach_breadcrumbs()
        await state.broadcast()
    return {"ok": True, "tracking": state.tracking, "crumbs": len(state.path)}


@app.post("/api/refresh")
async def refresh_data(admin: dict = Depends(require_admin)):
    """Re-fetch the dataset (starmap) and the commodities list (uexcorp)
    without restarting. Admin only."""
    global nav, raw_commodity_names
    fresh = await asyncio.to_thread(load_nav_data)
    fresh_commodities = await asyncio.to_thread(load_raw_commodity_names)
    nav_core.merge_custom_pois(fresh, custom_pois)
    merge_all_observations(fresh)
    nav_core.assign_qt_markers(fresh)
    async with state.lock:
        nav = fresh
        raw_commodity_names = fresh_commodities
        if (
            state.destination_id is not None
            and state.destination_id not in nav.pois
            and state.destination_id not in nav.observations
        ):
            state.destination_id = None
        state.recompute()
        await state.broadcast()
    return {
        "ok": True,
        "data": data_info,
        "containers": len(nav.containers),
        "pois": len(nav.pois),
        "observations": len(nav.observations),
        "raw_commodities": len(raw_commodity_names),
    }


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "containers": len(nav.containers),
        "pois": len(nav.pois),
        "observations": len(nav.observations),
        "handles": len(handles.by_handle),
        "raw_commodities": len(raw_commodity_names),
        "has_position": state.pos is not None,
        "data": data_info,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Browsers only; require a logged-in org member (session loaded from cookie).
    if not ws.session.get("user"):
        await ws.close(code=1008)   # policy violation
        return
    await ws.accept()
    state.clients.add(ws)
    try:
        # Send current state immediately so the UI isn't blank until the
        # next /showlocation.
        await ws.send_text(
            json.dumps(
                {
                    "type": "state",
                    "data": state.nav_state,
                    "capture": state.capture_status(),
                }
            )
        )
        while True:
            await ws.receive_text()  # client pings; content ignored
    except WebSocketDisconnect:
        pass
    finally:
        state.clients.discard(ws)


# ---------------------------------------------------------------------------
# Discord OAuth gate (Phase 0)
# ---------------------------------------------------------------------------
# Login + org-membership check + signed session for browsers; bearer watcher
# tokens for the headless watcher. The auth_gate middleware enforces "any /api/*
# needs one of these" centrally; the dependencies below add the finer checks
# (session-only, admin-only).


@app.get("/auth/login")
async def auth_login(request: Request):
    if not auth.configured():
        raise HTTPException(status_code=503, detail="Discord login is not configured")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(auth.authorize_url(state))


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    expected = request.session.pop("oauth_state", None)
    if not state or state != expected:
        raise HTTPException(status_code=400, detail="invalid OAuth state")
    if not code:
        raise HTTPException(status_code=400, detail="missing authorization code")
    try:
        token = await asyncio.to_thread(auth.exchange_code, code)
        profile = await asyncio.to_thread(auth.fetch_member_profile, token)
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
        raise HTTPException(status_code=502, detail=f"Discord auth failed: {exc} {body}")
    if profile is None:
        request.session.clear()
        return HTMLResponse(auth.NOT_IN_ORG_HTML, status_code=403)
    request.session["user"] = profile
    return RedirectResponse("/")


@app.post("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
async def api_me(request: Request):
    """The signed-in org member (or 401). Drives the UI's account state."""
    return require_session(request)


class TokenCreateIn(BaseModel):
    label: str = "watcher"


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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
