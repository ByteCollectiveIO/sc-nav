"""SC Nav server.

Receives positions from the Windows clipboard watcher, computes navigation
state against the containers/poi dataset, and pushes live updates to browser
clients over WebSocket.

Run:  uvicorn app:app --host 0.0.0.0 --port 8765
Data: ../poi by default, override with SC_NAV_DATA=/path/to/poi
"""

import asyncio
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import nav_core

DATA_DIR = Path(os.environ.get("SC_NAV_DATA", Path(__file__).parent.parent / "poi"))
STATIC_DIR = Path(__file__).parent / "static"

# Live dataset endpoints (the files in DATA_DIR act as the offline cache).
OC_URL = os.environ.get("SC_NAV_OC_URL", "https://starmap.space/api/v3/oc/index.php")
POI_URL = os.environ.get("SC_NAV_POI_URL", "https://starmap.space/api/v3/pois/index.php")
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
RESOURCE_NODE_FILE = DATA_DIR / "resource_nodes.json"
HANDLES_FILE = DATA_DIR / "handles.json"


def _load_json_list(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save_json_list(path: Path, items: list[dict]) -> None:
    path.write_text(json.dumps(items, indent=1))


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


app = FastAPI(title="SC Nav")
nav = load_nav_data()
custom_pois = load_custom_pois()
resource_nodes = _load_json_list(RESOURCE_NODE_FILE)
handles = HandleRegistry(HANDLES_FILE)
nav_core.merge_custom_pois(nav, custom_pois)
nav_core.merge_resource_nodes(nav, resource_nodes)


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


class NodeCaptureIn(BaseModel):
    ore: str
    band: int = 1
    biome: str | None = None
    note: str | None = None


class AppState:
    """Latest sample + destination + connected websocket clients."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.pos = None
        self.t = None
        self.prev_pos = None
        self.prev_t = None
        self.destination_id = None
        self.nav_state = None
        # capture_pending: {"kind": "poi"|"node", ...fields} while armed
        self.capture_pending = None
        self.last_capture = None      # summary of most recent capture
        self.owner = None             # {"player_id","handle"} from latest position
        self.clients: set[WebSocket] = set()

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
            if pending["kind"] == "node":
                _capture_node(new_pos, now, pending, owner)
            else:
                _capture_poi(new_pos, now, pending, owner)

        state.recompute()
        await state.broadcast()
    return {"ok": True}


def _capture_poi(pos_m, now, pending, owner):
    next_id = max(
        (c["id"] for c in custom_pois), default=nav_core.CUSTOM_ID_START - 1
    ) + 1
    poi = nav_core.custom_poi_from_position(
        nav, pos_m, now, pending["name"], pending["type"], next_id,
        owner_id=owner.get("player_id"), owner_handle=owner.get("handle"),
    )
    custom_pois.append(nav_core.custom_poi_to_dict(poi))
    try:
        save_custom_pois(custom_pois)
    except OSError as exc:
        print(f"[sc-nav] custom poi save failed: {exc}")
    nav.pois[poi.id] = poi
    state.last_capture = {
        "kind": "poi", "id": poi.id, "name": poi.name, "type": poi.type,
        "container": poi.container_name or "Space", "system": poi.system,
        "latitude": poi.latitude, "longitude": poi.longitude,
        "owner_handle": poi.owner_handle,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _capture_node(pos_m, now, pending, owner):
    next_id = max(
        (n["id"] for n in resource_nodes), default=nav_core.RESOURCE_ID_START - 1
    ) + 1
    node = nav_core.resource_node_from_position(
        nav, pos_m, now, pending["ore"], pending["band"], next_id,
        biome=pending.get("biome"), note=pending.get("note"),
        owner_id=owner.get("player_id"), owner_handle=owner.get("handle"),
    )
    resource_nodes.append(nav_core.resource_node_to_dict(node))
    try:
        _save_json_list(RESOURCE_NODE_FILE, resource_nodes)
    except OSError as exc:
        print(f"[sc-nav] resource node save failed: {exc}")
    nav.nodes[node.id] = node
    state.last_capture = {
        "kind": "resource", "id": node.id, "name": f"{node.ore} (B{node.band})",
        "ore": node.ore, "band": node.band, "quality": node.quality,
        "container": node.container_name or "Space", "system": node.system,
        "latitude": node.latitude, "longitude": node.longitude,
        "owner_handle": node.owner_handle,
        "captured_at": node.observed_at,
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
        owner_id=owner_id, limit=min(limit, 200),
    )


@app.post("/api/destination")
async def set_destination(body: DestinationIn):
    target = nav.pois.get(body.poi_id) or nav.nodes.get(body.poi_id)
    if target is None:
        raise HTTPException(status_code=404, detail="unknown poi_id")
    async with state.lock:
        state.destination_id = body.poi_id
        state.recompute()
        await state.broadcast()
    name = getattr(target, "name", None) or f"{target.ore} (B{target.band})"
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
        state.capture_pending = {
            "kind": "poi", "name": name, "type": body.type.strip() or "Custom"
        }
        await state.broadcast()
    return {"ok": True, "capture": state.capture_status()}


@app.post("/api/capture/node")
async def capture_node_start(body: NodeCaptureIn):
    ore = body.ore.strip()
    if not ore:
        raise HTTPException(status_code=400, detail="ore is required")
    async with state.lock:
        state.capture_pending = {
            "kind": "node",
            "ore": ore,
            "band": max(1, min(8, body.band)),
            "biome": (body.biome or "").strip() or None,
            "note": (body.note or "").strip() or None,
        }
        await state.broadcast()
    return {"ok": True, "capture": state.capture_status()}


@app.post("/api/capture/cancel")
async def capture_cancel():
    async with state.lock:
        state.capture_pending = None
        await state.broadcast()
    return {"ok": True}


@app.get("/api/handles")
async def list_handles():
    return handles.list()


@app.get("/api/custom_pois")
async def list_custom_pois():
    return custom_pois


@app.delete("/api/custom_pois/{poi_id}")
async def delete_custom_poi(poi_id: int):
    async with state.lock:
        idx = next((i for i, c in enumerate(custom_pois) if c["id"] == poi_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="unknown custom poi")
        custom_pois.pop(idx)
        save_custom_pois(custom_pois)
        nav.pois.pop(poi_id, None)
        if state.destination_id == poi_id:
            state.destination_id = None
        if state.last_capture and state.last_capture["id"] == poi_id:
            state.last_capture = None
        state.recompute()
        await state.broadcast()
    return {"ok": True}


@app.get("/api/nodes")
async def get_nodes(
    q: str = "", system: str | None = None, container: str | None = None,
    ore: str | None = None, owner_id: int | None = None, limit: int = 100,
):
    return nav_core.search_nodes(
        nav, query=q, system=system, container=container, ore=ore,
        owner_id=owner_id, limit=min(limit, 500),
    )


@app.delete("/api/nodes/{node_id}")
async def delete_node(node_id: int):
    async with state.lock:
        idx = next((i for i, n in enumerate(resource_nodes) if n["id"] == node_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="unknown resource node")
        resource_nodes.pop(idx)
        _save_json_list(RESOURCE_NODE_FILE, resource_nodes)
        nav.nodes.pop(node_id, None)
        if state.destination_id == node_id:
            state.destination_id = None
        if state.last_capture and state.last_capture["id"] == node_id:
            state.last_capture = None
        state.recompute()
        await state.broadcast()
    return {"ok": True}


@app.post("/api/refresh")
async def refresh_data():
    """Re-fetch the dataset from starmap.space without restarting."""
    global nav
    fresh = await asyncio.to_thread(load_nav_data)
    nav_core.merge_custom_pois(fresh, custom_pois)
    nav_core.merge_resource_nodes(fresh, resource_nodes)
    async with state.lock:
        nav = fresh
        if (
            state.destination_id is not None
            and state.destination_id not in nav.pois
            and state.destination_id not in nav.nodes
        ):
            state.destination_id = None
        state.recompute()
        await state.broadcast()
    return {
        "ok": True,
        "data": data_info,
        "containers": len(nav.containers),
        "pois": len(nav.pois),
        "nodes": len(nav.nodes),
    }


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "containers": len(nav.containers),
        "pois": len(nav.pois),
        "nodes": len(nav.nodes),
        "handles": len(handles.by_handle),
        "has_position": state.pos is not None,
        "data": data_info,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
