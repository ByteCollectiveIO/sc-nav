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
    nearest_qt: str | None = None    # name of nearest QT-marker POI (computed)


@dataclass
class Observation:
    """One *sighting* of an ephemeral, user-recorded thing at a location.

    Stored as an append-only log (not an editable entity) because the things
    it records respawn pseudo-randomly — the value is the history of where/what
    was seen, which feeds clustering and heatmaps later. Position is stored in
    the parent body's rotating frame, identical to POIs.

    `category` selects the kind ("resource", "wildlife", …); `data` holds the
    category-specific fields (resource: ore/band/quality; wildlife: species).
    Common geo/owner/biome/note/timestamp fields are shared across categories
    so one capture/serialize/search/summary path serves them all."""

    id: int
    category: str
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
    data: dict                       # category-specific fields
    nearest_qt: str | None = None    # name of nearest QT-marker POI (computed)


@dataclass
class NavData:
    containers: dict[tuple[str, str], Container] = field(default_factory=dict)
    pois: dict[int, Poi] = field(default_factory=dict)
    observations: dict[int, Observation] = field(default_factory=dict)
    systems: list[str] = field(default_factory=list)
    # QT-marker indexes (filled by assign_qt_markers)
    qt_markers: list = field(default_factory=list)               # all qt_marker POIs
    qt_by_container: dict = field(default_factory=dict)           # (system,container) -> [Poi]

    def container_of(self, entity) -> Container | None:
        """Parent container of any positioned entity (Poi or Observation —
        both carry container_name + system)."""
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


def _poi_base(poi) -> dict:
    return {
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
        "nearest_qt": poi.nearest_qt,
    }


def _observation_base(obs) -> dict:
    cat = OBSERVATION_CATEGORIES[obs.category]
    # Flatten category data first (ore/band/quality or species for the UI),
    # then set the canonical fields so a data key can never clobber them.
    out = dict(obs.data)
    out.update({
        "kind": obs.category,
        "id": obs.id,
        "name": cat["display_name"](obs.data),
        "type": obs.data.get(cat["type_field"]),
        "system": obs.system,
        "container": obs.container_name or "Space",
        "biome": obs.biome,
        "note": obs.note,
        "owner_id": obs.owner_id,
        "owner_handle": obs.owner_handle,
        "observed_at": obs.observed_at,
        "nearest_qt": obs.nearest_qt,
        "custom": True,
    })
    return out


def _poi_summary(nav, poi, t, pos_m, player_lat=None, player_lon=None, surface_radius_m=None):
    out = _poi_base(poi)
    out.update(_geo_fields(nav, poi, t, pos_m, player_lat, player_lon, surface_radius_m))
    return out


def _observation_summary(nav, obs, t, pos_m, player_lat=None, player_lon=None, surface_radius_m=None):
    out = _observation_base(obs)
    out.update(_geo_fields(nav, obs, t, pos_m, player_lat, player_lon, surface_radius_m))
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
    # Per-category so a dense category (e.g. resources) can't starve a sparse
    # one (e.g. wildlife) out of the merged list.
    by_cat = {}
    for o in nav.observations.values():
        by_cat.setdefault(o.category, []).append(o)
    nearest_observations = []
    for cat in OBSERVATION_CATEGORIES:
        nearest_observations += _nearest(by_cat.get(cat, []), _observation_summary)

    destination = None
    dest_entity = nav.pois.get(destination_id)
    if dest_entity is None:
        dest_entity = nav.observations.get(destination_id)
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
        summarize = _observation_summary if isinstance(dest_entity, Observation) else _poi_summary
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
                "body_radius_m": container.body_radius,
                "is_body": container.is_body,
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
        "nearest_observations": nearest_observations,
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
    poi = Poi(
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
    poi.nearest_qt = nearest_qt_marker(nav, poi, t_unix)
    return poi


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
        try:
            poi = poi_from_custom_dict(d)
        except (KeyError, ValueError, TypeError) as exc:
            print(f"[sc-nav] skipping bad custom POI record: {exc}")
            continue
        nav.pois[poi.id] = poi


# ---------------------------------------------------------------------------
# Observations (ephemeral user-recorded things — append-only log per category)
# ---------------------------------------------------------------------------

# Shared id space across all observation categories, above custom POIs (1M).
OBSERVATION_ID_START = 2_000_000


UNKNOWN_QUALITY = "Unk"


def quality_for_band(band) -> str:
    # Band is unknown until a node is mined; band None/non-numeric -> "Unk".
    try:
        band = max(1, min(8, int(band)))
    except (TypeError, ValueError):
        return UNKNOWN_QUALITY
    if band == 1:
        return "Lowest"
    if band <= 4:
        return "Low to Mid"
    if band <= 6:
        return "Good / High"
    if band == 7:
        return "Very High"
    return "Perfect"


def _normalize_resource(data: dict) -> dict:
    data = dict(data)
    try:
        band = max(1, min(8, int(data.get("band"))))
    except (TypeError, ValueError):
        band = None                                 # unknown until mined
    data["band"] = band
    data["quality"] = quality_for_band(band)        # always derived from band
    data["ore"] = (data.get("ore") or "Unknown")
    return data


def _normalize_wildlife(data: dict) -> dict:
    data = dict(data)
    data["species"] = (data.get("species") or "Unknown")
    return data


# Per-category behavior. Adding a category is one entry here plus a capture
# endpoint — no new dataclass/store/search/summary code.
OBSERVATION_CATEGORIES = {
    "resource": {
        "file": "resource_nodes.json",
        "type_field": "ore",
        "search_fields": ("ore",),
        "normalize": _normalize_resource,
        "display_name": lambda d: f"{d.get('ore')} (B{d.get('band') if d.get('band') is not None else '?'})",
    },
    "wildlife": {
        "file": "wildlife.json",
        "type_field": "species",
        "search_fields": ("species",),
        "normalize": _normalize_wildlife,
        "display_name": lambda d: d.get("species") or "Wildlife",
    },
}


def observation_from_position(
    nav: NavData,
    pos_m,
    t_unix: float,
    category: str,
    data: dict,
    obs_id: int,
    biome: str | None = None,
    note: str | None = None,
    owner_id: int | None = None,
    owner_handle: str | None = None,
    observed_at: str | None = None,
) -> Observation:
    if category not in OBSERVATION_CATEGORIES:
        raise ValueError(f"unknown observation category: {category}")
    data = OBSERVATION_CATEGORIES[category]["normalize"](data)
    system, cname, local, gm, lat, lon, height = _frame_at(nav, pos_m, t_unix)
    obs = Observation(
        id=obs_id,
        category=category,
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
        data=data,
    )
    obs.nearest_qt = nearest_qt_marker(nav, obs, t_unix)
    return obs


def observation_to_dict(obs: Observation) -> dict:
    return {
        "id": obs.id,
        "category": obs.category,
        "system": obs.system,
        "container": obs.container_name,
        "local_km": list(obs.local_km) if obs.local_km else None,
        "global_m": list(obs.global_m) if obs.global_m else None,
        "latitude": obs.latitude,
        "longitude": obs.longitude,
        "height_m": obs.height_m,
        "biome": obs.biome,
        "note": obs.note,
        "owner_id": obs.owner_id,
        "owner_handle": obs.owner_handle,
        "observed_at": obs.observed_at,
        "data": obs.data,
    }


# Category fields that pre-generalization records stored at the top level
# instead of under "data" (resource nodes had ore/band/quality flat).
_LEGACY_FLAT_FIELDS = ("ore", "band", "quality", "species")


def observation_from_dict(d: dict, category: str | None = None) -> Observation:
    category = d.get("category") or category
    if category not in OBSERVATION_CATEGORIES:
        raise ValueError(f"unknown observation category: {category!r}")
    # Back-compat: if there's no "data" sub-dict, recover the category fields
    # from the legacy flat layout so old resource_nodes.json isn't reset to
    # Unknown/B1/Lowest on load.
    raw = d.get("data")
    if raw is None:
        raw = {k: d[k] for k in _LEGACY_FLAT_FIELDS if k in d}
    return Observation(
        id=int(d["id"]),
        category=category,
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
        data=OBSERVATION_CATEGORIES[category]["normalize"](raw),
    )


def merge_observations(nav: NavData, dicts: list[dict], category: str | None = None) -> None:
    for d in dicts:
        try:
            obs = observation_from_dict(d, category)
        except (KeyError, ValueError, TypeError) as exc:
            # One malformed/unknown-category record must not abort startup.
            print(f"[sc-nav] skipping bad observation record: {exc}")
            continue
        nav.observations[obs.id] = obs


def surface_distance_m(lat1, lon1, lat2, lon2, radius_m) -> float:
    """Great-circle distance only (drops the bearing) — used for breadcrumb
    move-distance gating."""
    return great_circle(lat1, lon1, lat2, lon2, radius_m)[0]


# ---------------------------------------------------------------------------
# Nearest QT (quantum-travel) marker
# ---------------------------------------------------------------------------


def index_qt_markers(nav: NavData) -> None:
    """Build the lookup of QT-marker POIs used by nearest_qt_marker."""
    nav.qt_markers = [p for p in nav.pois.values() if p.qt_marker]
    nav.qt_by_container = {}
    for p in nav.qt_markers:
        nav.qt_by_container.setdefault((p.system, p.container_name), []).append(p)


def nearest_qt_marker(nav: NavData, target, t_ref: float) -> str | None:
    """Name of the nearest jumpable QT-marker POI to `target` (Poi or
    Observation). A target that is itself a QT marker returns its own name.
    Prefers a marker on the same body (rotation-invariant local distance);
    falls back to the nearest QT marker elsewhere in the same system."""
    if getattr(target, "qt_marker", False):
        return target.name
    system = target.system
    # Same-body candidates: compare in the body-local frame (time-invariant).
    if target.container_name is not None and target.local_km is not None:
        best, best_d = None, math.inf
        for p in nav.qt_by_container.get((system, target.container_name), []):
            if p is target or p.local_km is None:
                continue
            d = dist3(target.local_km, p.local_km)
            if d < best_d:
                best, best_d = p, d
        if best is not None:
            return best.name
    # Fallback: nearest QT marker anywhere in the same system (global frame).
    # Body centers are static in the dataset, so this is rotation-insensitive.
    tg = entity_global_m(nav, target, t_ref)
    if tg is None:
        return None
    best, best_d = None, math.inf
    for p in nav.qt_markers:
        if p.system != system or p is target:
            continue
        pg = entity_global_m(nav, p, t_ref)
        if pg is None:
            continue
        d = dist3(tg, pg)
        if d < best_d:
            best, best_d = p, d
    return best.name if best else None


def assign_qt_markers(nav: NavData, t_ref: float | None = None) -> None:
    """(Re)build the QT index and assign every POI/observation its nearest
    QT-marker name. Run after load and after a dataset refresh."""
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    index_qt_markers(nav)
    for p in nav.pois.values():
        p.nearest_qt = nearest_qt_marker(nav, p, t_ref)
    for o in nav.observations.values():
        o.nearest_qt = nearest_qt_marker(nav, o, t_ref)


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
        results.append((rank, len(name), _poi_base(p)))
    results.sort(key=lambda r: (r[0], r[1], r[2]["name"]))
    return [r[2] for r in results[:limit]]


def search_observations(
    nav: NavData,
    query: str = "",
    category: str | None = None,
    system: str | None = None,
    container: str | None = None,
    type_value: str | None = None,
    owner_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    q = query.strip().lower()
    results = []
    for obs in nav.observations.values():
        if category and obs.category != category:
            continue
        if system and obs.system != system:
            continue
        if container and (obs.container_name or "Space") != container:
            continue
        cat = OBSERVATION_CATEGORIES[obs.category]
        if type_value and obs.data.get(cat["type_field"]) != type_value:
            continue
        if owner_id is not None and obs.owner_id != owner_id:
            continue
        if q and not any(q in str(obs.data.get(f, "")).lower() for f in cat["search_fields"]):
            continue
        results.append(_observation_base(obs))
    # newest sightings first
    results.sort(key=lambda r: r["observed_at"], reverse=True)
    return results[:limit]
