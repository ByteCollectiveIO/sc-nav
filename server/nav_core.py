"""Core navigation math for the SC nav server.

Pure standard-library functions over the containers.json / poi.json dataset.
All coordinate conventions here were verified empirically against the dataset:

- Container positions are meters in the star system's global frame
  (origin = system center, NOT the star — Stanton's star is ~3.2 Gm off origin).
- Surface POI positions are kilometers in the parent body's *rotating* local
  frame (origin = body center).
- POIs with Planet == "Space" store global meters directly.
- latitude  = asin(z / r)                      (matches stored values < 0.01 deg)
- longitude360 = atan2(y, x) mod 360          (matches stored values < 0.01 deg)
- longitude (+/-180 form) = longitude360 - 180

The one thing that cannot be verified offline is the rotation epoch below —
it must be calibrated in game (see README "Calibrating rotation").
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --- Rotation model calibration knobs ---------------------------------------
# Community-established simulation epoch. If standing at a known POI shows a
# consistent east/west offset on rotating bodies, tune these.
ROTATION_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
# +1: bodies rotate counterclockwise looking down +Z (local->global = +theta).
# Flip to -1 if calibration shows mirrored rotation drift.
ROTATION_SIGN = 1.0

DEG = math.degrees
RAD = math.radians


@dataclass
class Container:
    name: str
    system: str
    type: str
    internal_name: str
    pos: tuple[float, float, float]  # meters, system frame
    body_radius: float               # meters
    om_radius: float                 # meters
    grid_radius: float               # meters
    rotation_speed: float            # hours per revolution (0 = no spin)
    rotation_adjustment: float       # degrees

    @property
    def is_body(self) -> bool:
        return self.body_radius > 0 and self.type in ("Star", "Planet", "Moon")

    def detection_radius(self) -> float:
        """How close (m) counts as 'at' this container."""
        return max(self.grid_radius, self.om_radius, self.body_radius * 1.5)


@dataclass
class Poi:
    id: int
    name: str
    system: str
    container_name: str | None       # None for Planet == "Space"
    type: str
    local_km: tuple[float, float, float] | None  # body-local km (surface POIs)
    global_m: tuple[float, float, float] | None  # system meters (space POIs)
    latitude: float | None
    longitude: float | None          # +/-180 form
    height_m: float | None
    qt_marker: bool
    custom: bool = False             # user-created, lives in custom_pois.json
    owner_id: int | None = None      # PlayerID who recorded it (custom only)
    owner_handle: str | None = None


@dataclass
class ResourceNode:
    """One *observation* of an ephemeral mineable deposit.

    Stored as an append-only log (not an editable entity) because nodes
    respawn pseudo-randomly — the value is in the history of where/what was
    seen, which feeds clustering and heatmaps later. Position is stored in the
    parent body's rotating frame, identical to POIs."""

    id: int
    ore: str
    band: int                        # 1-8
    quality: str                     # label derived from band
    system: str
    container_name: str | None
    local_km: tuple[float, float, float] | None
    global_m: tuple[float, float, float] | None
    latitude: float | None
    longitude: float | None
    height_m: float | None           # auto-captured altitude (spawn-rule input)
    biome: str | None                # optional manual field
    note: str | None
    owner_id: int | None
    owner_handle: str | None
    observed_at: str                 # ISO timestamp of the sighting


@dataclass
class NavData:
    containers: dict[tuple[str, str], Container] = field(default_factory=dict)
    pois: dict[int, Poi] = field(default_factory=dict)
    nodes: dict[int, ResourceNode] = field(default_factory=dict)
    systems: list[str] = field(default_factory=list)

    def container_of(self, entity) -> Container | None:
        """Parent container of a Poi or ResourceNode (both carry
        container_name + system)."""
        if entity.container_name is None:
            return None
        return self.containers.get((entity.system, entity.container_name))


def load_data(data_dir: str | Path) -> NavData:
    data_dir = Path(data_dir)
    return parse_data(
        json.loads((data_dir / "containers.json").read_text()),
        json.loads((data_dir / "poi.json").read_text()),
    )


def parse_data(containers_raw: list[dict], pois_raw: list[dict]) -> NavData:
    nav = NavData()

    for c in containers_raw:
        cont = Container(
            name=c["ObjectContainer"],
            system=c["System"],
            type=c["Type"],
            internal_name=c.get("InternalName") or "",
            pos=(float(c["XCoord"]), float(c["YCoord"]), float(c["ZCoord"])),
            body_radius=float(c.get("BodyRadius") or 0),
            om_radius=float(c.get("OrbitalMarkerRadius") or 0),
            grid_radius=float(c.get("GRIDRadius") or 0),
            rotation_speed=float(c.get("RotationSpeedX") or 0),
            rotation_adjustment=float(c.get("RotationAdjustmentX") or 0),
        )
        nav.containers[(cont.system, cont.name)] = cont

    for p in pois_raw:
        is_space = p["Planet"] == "Space"
        xyz = (float(p["XCoord"]), float(p["YCoord"]), float(p["ZCoord"]))
        poi = Poi(
            id=int(p["item_id"]),
            name=p["PoiName"],
            system=p["System"],
            container_name=None if is_space else p["Planet"],
            type=p.get("Type") or "Unknown",
            local_km=None if is_space else xyz,
            global_m=xyz if is_space else None,
            latitude=p.get("Latitude"),
            longitude=p.get("Longitude"),
            height_m=p.get("Height"),
            qt_marker=bool(p.get("QTMarker")),
        )
        nav.pois[poi.id] = poi

    nav.systems = sorted({c.system for c in nav.containers.values()})
    return nav


# ---------------------------------------------------------------------------
# Rotation + frame transforms
# ---------------------------------------------------------------------------


def rotation_degrees(container: Container, t_unix: float) -> float:
    """Body's current rotation angle around +Z, degrees."""
    if container.rotation_speed == 0:
        return 0.0
    hours = (t_unix - ROTATION_EPOCH) / 3600.0
    revolutions = hours / container.rotation_speed
    return ROTATION_SIGN * ((revolutions * 360.0 + container.rotation_adjustment) % 360.0)


def _rot_z(v: tuple[float, float, float], deg: float) -> tuple[float, float, float]:
    a = RAD(deg)
    c, s = math.cos(a), math.sin(a)
    return (v[0] * c - v[1] * s, v[0] * s + v[1] * c, v[2])


def global_to_local_km(container: Container, pos_m, t_unix: float):
    """System-frame meters -> body-local km (rotating frame)."""
    rel = (
        pos_m[0] - container.pos[0],
        pos_m[1] - container.pos[1],
        pos_m[2] - container.pos[2],
    )
    unrot = _rot_z(rel, -rotation_degrees(container, t_unix))
    return (unrot[0] / 1000.0, unrot[1] / 1000.0, unrot[2] / 1000.0)


def local_km_to_global(container: Container, local_km, t_unix: float):
    """Body-local km (rotating frame) -> system-frame meters."""
    rot = _rot_z(local_km, rotation_degrees(container, t_unix))
    return (
        rot[0] * 1000.0 + container.pos[0],
        rot[1] * 1000.0 + container.pos[1],
        rot[2] * 1000.0 + container.pos[2],
    )


def poi_global_m(nav: NavData, entity, t_unix: float):
    """Global system-frame position of a Poi or ResourceNode at time t."""
    if entity.global_m is not None:
        return entity.global_m
    container = nav.container_of(entity)
    if container is None or entity.local_km is None:
        return None
    return local_km_to_global(container, entity.local_km, t_unix)


# Both POIs and resource nodes resolve their position the same way.
entity_global_m = poi_global_m


# ---------------------------------------------------------------------------
# Spherical helpers
# ---------------------------------------------------------------------------


def latlon_from_local(local_km) -> tuple[float, float, float]:
    """Returns (latitude, longitude(+/-180), radius_km). Convention verified
    against the dataset: lon360 = atan2(y, x); in-game longitude = lon360 - 180."""
    x, y, z = local_km
    r = math.sqrt(x * x + y * y + z * z)
    if r == 0:
        return 0.0, 0.0, 0.0
    lat = DEG(math.asin(max(-1.0, min(1.0, z / r))))
    lon360 = DEG(math.atan2(y, x)) % 360.0
    lon = lon360 - 180.0
    return lat, lon, r


def great_circle(lat1, lon1, lat2, lon2, radius_m) -> tuple[float, float]:
    """Returns (surface distance m, initial bearing deg 0..360, 0=N, 90=E)."""
    p1, p2 = RAD(lat1), RAD(lat2)
    dl = RAD(lon2 - lon1)
    sin_p1, cos_p1 = math.sin(p1), math.cos(p1)
    sin_p2, cos_p2 = math.sin(p2), math.cos(p2)

    central = math.acos(
        max(-1.0, min(1.0, sin_p1 * sin_p2 + cos_p1 * cos_p2 * math.cos(dl)))
    )
    bearing = DEG(
        math.atan2(
            math.sin(dl) * cos_p2,
            cos_p1 * sin_p2 - sin_p1 * cos_p2 * math.cos(dl),
        )
    ) % 360.0
    return central * radius_m, bearing


def dist3(a, b) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


# ---------------------------------------------------------------------------
# Container detection + nav state
# ---------------------------------------------------------------------------


def detect_container(nav: NavData, pos_m) -> Container | None:
    """Nearest container whose detection radius covers the position.
    Searched across all systems — /showlocation doesn't say which system the
    player is in, so proximity to a known body is what identifies it."""
    best, best_d = None, math.inf
    for c in nav.containers.values():
        radius = c.detection_radius()
        if radius <= 0:
            continue
        d = dist3(pos_m, c.pos)
        if d <= radius and d < best_d:
            best, best_d = c, d
    return best


def _geo_fields(nav, entity, t, pos_m, player_lat, player_lon, surface_radius_m):
    """Distance / surface-distance / bearing common to POIs and nodes."""
    gp = entity_global_m(nav, entity, t)
    out = {
        "distance_m": dist3(pos_m, gp) if gp else None,
        "bearing_deg": None,
        "surface_distance_m": None,
        "latitude": entity.latitude,
        "longitude": entity.longitude,
    }
    if (
        player_lat is not None
        and surface_radius_m
        and entity.latitude is not None
        and entity.longitude is not None
    ):
        sd, bearing = great_circle(
            player_lat, player_lon, entity.latitude, entity.longitude, surface_radius_m
        )
        out["surface_distance_m"] = sd
        out["bearing_deg"] = bearing
    return out


def _poi_summary(nav, poi, t, pos_m, player_lat=None, player_lon=None, surface_radius_m=None):
    out = {
        "kind": "poi",
        "id": poi.id,
        "name": poi.name,
        "type": poi.type,
        "system": poi.system,
        "container": poi.container_name or "Space",
        "qt_marker": poi.qt_marker,
        "custom": poi.custom,
        "owner_id": poi.owner_id,
        "owner_handle": poi.owner_handle,
    }
    out.update(_geo_fields(nav, poi, t, pos_m, player_lat, player_lon, surface_radius_m))
    return out


def _node_summary(nav, node, t, pos_m, player_lat=None, player_lon=None, surface_radius_m=None):
    out = {
        "kind": "resource",
        "id": node.id,
        "name": f"{node.ore} (B{node.band})",
        "type": node.ore,
        "ore": node.ore,
        "band": node.band,
        "quality": node.quality,
        "system": node.system,
        "container": node.container_name or "Space",
        "biome": node.biome,
        "note": node.note,
        "owner_id": node.owner_id,
        "owner_handle": node.owner_handle,
        "observed_at": node.observed_at,
        "custom": True,
    }
    out.update(_geo_fields(nav, node, t, pos_m, player_lat, player_lon, surface_radius_m))
    return out


def compute_state(
    nav: NavData,
    pos_m,
    t_unix: float,
    destination_id: int | None = None,
    prev_pos=None,
    prev_t: float | None = None,
    nearest_count: int = 10,
) -> dict:
    """Full navigation state for one position sample."""
    container = detect_container(nav, pos_m)

    lat = lon = None
    altitude_m = None
    surface_radius_m = None
    if container is not None and container.is_body:
        local = global_to_local_km(container, pos_m, t_unix)
        lat, lon, r_km = latlon_from_local(local)
        altitude_m = r_km * 1000.0 - container.body_radius
        surface_radius_m = container.body_radius

    speed_ms = None
    if prev_pos is not None and prev_t is not None and t_unix > prev_t:
        dt = t_unix - prev_t
        if dt <= 300:
            speed_ms = dist3(pos_m, prev_pos) / dt

    def _in_scope(entity):
        # When at a container, restrict to that container (+ space entities);
        # otherwise consider everything.
        if container is None:
            return True
        return entity.system == container.system and (
            entity.container_name == container.name or entity.container_name is None
        )

    def _nearest(entities, summarize):
        rows = [
            summarize(nav, e, t_unix, pos_m, lat, lon, surface_radius_m)
            for e in entities
            if _in_scope(e)
        ]
        return sorted(
            (r for r in rows if r["distance_m"] is not None),
            key=lambda r: r["distance_m"],
        )[:nearest_count]

    nearest_pois = _nearest(nav.pois.values(), _poi_summary)
    nearest_nodes = _nearest(nav.nodes.values(), _node_summary)

    destination = None
    dest_entity = nav.pois.get(destination_id)
    if dest_entity is None:
        dest_entity = nav.nodes.get(destination_id)
    if dest_entity is not None:
        # Same body only if both name AND system match — names can repeat
        # across the multi-system dataset (matches _in_scope's guard).
        same_container = (
            container is not None
            and dest_entity.container_name == container.name
            and dest_entity.system == container.system
        )
        # Pick the summarizer from the entity we actually resolved, not from a
        # second id lookup that could disagree if an id ever exists in both.
        summarize = _node_summary if isinstance(dest_entity, ResourceNode) else _poi_summary
        destination = summarize(
            nav,
            dest_entity,
            t_unix,
            pos_m,
            lat if same_container else None,
            lon if same_container else None,
            surface_radius_m if same_container else None,
        )
        destination["same_container"] = same_container
        destination["eta_s"] = None
        # Explicit None check: a true surface distance of 0.0 (standing on it)
        # must not fall back to the 3D distance.
        guide = destination["surface_distance_m"]
        if guide is None:
            guide = destination["distance_m"]
        if speed_ms and speed_ms > 1 and guide is not None:
            destination["eta_s"] = guide / speed_ms

    return {
        "t": t_unix,
        "position": {"x": pos_m[0], "y": pos_m[1], "z": pos_m[2]},
        "system": container.system if container else None,
        "container": (
            {
                "name": container.name,
                "type": container.type,
                "distance_from_center_m": dist3(pos_m, container.pos),
            }
            if container
            else None
        ),
        "latitude": lat,
        "longitude": lon,
        "altitude_m": altitude_m,
        "speed_ms": speed_ms,
        "destination": destination,
        "nearest_pois": nearest_pois,
        "nearest_nodes": nearest_nodes,
    }


# ---------------------------------------------------------------------------
# Custom (user-created) POIs
# ---------------------------------------------------------------------------

# Reserved ID range so customs never collide with upstream item_ids (~1-2000).
CUSTOM_ID_START = 1_000_000


def _frame_at(nav: NavData, pos_m, t_unix: float):
    """Resolve a global position into storage form: returns
    (system, container_name, local_km, global_m, lat, lon, height_m)."""
    container = detect_container(nav, pos_m)
    if container is None:
        return "Unknown", None, None, tuple(pos_m), None, None, None
    local = global_to_local_km(container, pos_m, t_unix)
    lat = lon = height = None
    if container.body_radius > 0:
        lat, lon, r_km = latlon_from_local(local)
        height = r_km * 1000.0 - container.body_radius
    return container.system, container.name, local, None, lat, lon, height


def custom_poi_from_position(
    nav: NavData,
    pos_m,
    t_unix: float,
    name: str,
    poi_type: str,
    poi_id: int,
    owner_id: int | None = None,
    owner_handle: str | None = None,
) -> Poi:
    """Create a POI at a global position, stored the same way the upstream
    dataset stores it: body-local rotating-frame km when at a container,
    global meters when in open space."""
    system, cname, local, gm, lat, lon, height = _frame_at(nav, pos_m, t_unix)
    return Poi(
        id=poi_id,
        name=name,
        system=system,
        container_name=cname,
        type=poi_type,
        local_km=local,
        global_m=gm,
        latitude=lat,
        longitude=lon,
        height_m=height,
        qt_marker=False,
        custom=True,
        owner_id=owner_id,
        owner_handle=owner_handle,
    )


def custom_poi_to_dict(poi: Poi) -> dict:
    return {
        "id": poi.id,
        "name": poi.name,
        "system": poi.system,
        "container": poi.container_name,
        "type": poi.type,
        "local_km": list(poi.local_km) if poi.local_km else None,
        "global_m": list(poi.global_m) if poi.global_m else None,
        "latitude": poi.latitude,
        "longitude": poi.longitude,
        "height_m": poi.height_m,
        "owner_id": poi.owner_id,
        "owner_handle": poi.owner_handle,
    }


def poi_from_custom_dict(d: dict) -> Poi:
    return Poi(
        id=int(d["id"]),
        name=d["name"],
        system=d.get("system") or "Unknown",
        container_name=d.get("container"),
        type=d.get("type") or "Custom",
        local_km=tuple(d["local_km"]) if d.get("local_km") else None,
        global_m=tuple(d["global_m"]) if d.get("global_m") else None,
        latitude=d.get("latitude"),
        longitude=d.get("longitude"),
        height_m=d.get("height_m"),
        qt_marker=False,
        custom=True,
        owner_id=d.get("owner_id"),
        owner_handle=d.get("owner_handle"),
    )


def merge_custom_pois(nav: NavData, custom_dicts: list[dict]) -> None:
    for d in custom_dicts:
        poi = poi_from_custom_dict(d)
        nav.pois[poi.id] = poi


# ---------------------------------------------------------------------------
# Resource nodes (ephemeral mineable deposits — observation log)
# ---------------------------------------------------------------------------

RESOURCE_ID_START = 2_000_000


def quality_for_band(band: int) -> str:
    band = max(1, min(8, int(band)))
    if band == 1:
        return "Lowest"
    if band <= 4:
        return "Low to Mid"
    if band <= 6:
        return "Good / High"
    if band == 7:
        return "Very High"
    return "Perfect"


def resource_node_from_position(
    nav: NavData,
    pos_m,
    t_unix: float,
    ore: str,
    band: int,
    node_id: int,
    biome: str | None = None,
    note: str | None = None,
    owner_id: int | None = None,
    owner_handle: str | None = None,
    observed_at: str | None = None,
) -> ResourceNode:
    band = max(1, min(8, int(band)))
    system, cname, local, gm, lat, lon, height = _frame_at(nav, pos_m, t_unix)
    return ResourceNode(
        id=node_id,
        ore=ore,
        band=band,
        quality=quality_for_band(band),
        system=system,
        container_name=cname,
        local_km=local,
        global_m=gm,
        latitude=lat,
        longitude=lon,
        height_m=height,
        biome=biome,
        note=note,
        owner_id=owner_id,
        owner_handle=owner_handle,
        observed_at=observed_at or datetime.now(timezone.utc).isoformat(),
    )


def resource_node_to_dict(node: ResourceNode) -> dict:
    return {
        "id": node.id,
        "ore": node.ore,
        "band": node.band,
        "quality": node.quality,
        "system": node.system,
        "container": node.container_name,
        "local_km": list(node.local_km) if node.local_km else None,
        "global_m": list(node.global_m) if node.global_m else None,
        "latitude": node.latitude,
        "longitude": node.longitude,
        "height_m": node.height_m,
        "biome": node.biome,
        "note": node.note,
        "owner_id": node.owner_id,
        "owner_handle": node.owner_handle,
        "observed_at": node.observed_at,
    }


def node_from_dict(d: dict) -> ResourceNode:
    band = int(d.get("band") or 1)
    return ResourceNode(
        id=int(d["id"]),
        ore=d.get("ore") or "Unknown",
        band=band,
        quality=d.get("quality") or quality_for_band(band),
        system=d.get("system") or "Unknown",
        container_name=d.get("container"),
        local_km=tuple(d["local_km"]) if d.get("local_km") else None,
        global_m=tuple(d["global_m"]) if d.get("global_m") else None,
        latitude=d.get("latitude"),
        longitude=d.get("longitude"),
        height_m=d.get("height_m"),
        biome=d.get("biome"),
        note=d.get("note"),
        owner_id=d.get("owner_id"),
        owner_handle=d.get("owner_handle"),
        observed_at=d.get("observed_at") or "",
    )


def merge_resource_nodes(nav: NavData, node_dicts: list[dict]) -> None:
    for d in node_dicts:
        node = node_from_dict(d)
        nav.nodes[node.id] = node


def search_pois(
    nav: NavData,
    query: str = "",
    system: str | None = None,
    container: str | None = None,
    poi_type: str | None = None,
    owner_id: int | None = None,
    limit: int = 25,
) -> list[dict]:
    q = query.strip().lower()
    results = []
    for p in nav.pois.values():
        if system and p.system != system:
            continue
        if container and (p.container_name or "Space") != container:
            continue
        if poi_type and p.type != poi_type:
            continue
        if owner_id is not None and p.owner_id != owner_id:
            continue
        name = p.name.lower()
        if q and q not in name:
            continue
        rank = 0 if name.startswith(q) else 1
        results.append(
            (
                rank,
                len(name),
                {
                    "kind": "poi",
                    "id": p.id,
                    "name": p.name,
                    "type": p.type,
                    "system": p.system,
                    "container": p.container_name or "Space",
                    "qt_marker": p.qt_marker,
                    "custom": p.custom,
                    "owner_id": p.owner_id,
                    "owner_handle": p.owner_handle,
                },
            )
        )
    results.sort(key=lambda r: (r[0], r[1], r[2]["name"]))
    return [r[2] for r in results[:limit]]


def search_nodes(
    nav: NavData,
    query: str = "",
    system: str | None = None,
    container: str | None = None,
    ore: str | None = None,
    owner_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    q = query.strip().lower()
    results = []
    for n in nav.nodes.values():
        if system and n.system != system:
            continue
        if container and (n.container_name or "Space") != container:
            continue
        if ore and n.ore != ore:
            continue
        if owner_id is not None and n.owner_id != owner_id:
            continue
        if q and q not in n.ore.lower():
            continue
        results.append(
            {
                "kind": "resource",
                "id": n.id,
                "name": f"{n.ore} (B{n.band})",
                "type": n.ore,
                "ore": n.ore,
                "band": n.band,
                "quality": n.quality,
                "system": n.system,
                "container": n.container_name or "Space",
                "biome": n.biome,
                "note": n.note,
                "owner_id": n.owner_id,
                "owner_handle": n.owner_handle,
                "observed_at": n.observed_at,
                "custom": True,
            }
        )
    # newest observations first
    results.sort(key=lambda r: r["observed_at"], reverse=True)
    return results[:limit]
