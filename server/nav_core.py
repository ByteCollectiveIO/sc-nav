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
import re
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
    note: str | None = None          # free-text context; upstream POIs map from Comment
    nearest_qt: str | None = None    # name of nearest QT-marker POI (computed)
    nearest_qt_dist_m: float | None = None  # distance to that marker, meters


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
    # SC shard the sighting was made on (e.g. "pub_use1b_12030094_130"), read
    # from Game.log by the watcher. Ephemeral nodes only exist on their shard,
    # so this is what lets a client hide nodes that aren't on its server. None
    # for legacy records and captures with no shard known.
    shard_id: str | None = None
    nearest_qt: str | None = None    # name of nearest QT-marker POI (computed)
    nearest_qt_dist_m: float | None = None  # distance to that marker, meters


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
            # Jumpable marker if QTMarker==1, OR Type=="Landing Zone" (major
            # cities like Area18 are QT destinations but are sometimes flagged
            # 0/null upstream). QTMarker is 1/-1/0/null; only 1 counts (bool()
            # would wrongly treat -1 as truthy). Derived here, not by editing
            # the data, so it survives starmap.space refreshes.
            qt_marker=(
                p.get("QTMarker") in (1, "1")
                or (p.get("Type") or "").strip() == "Landing Zone"
            ),
            # Upstream catalog carries a capital-C Comment for some POIs; surface
            # it (read-only) through the same note field custom POIs now use.
            note=p.get("Comment") or None,
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


def local_km_from_latlon(lat, lon, radius_m) -> tuple[float, float, float]:
    """Inverse of latlon_from_local on the body surface: (lat, lon ±180) ->
    body-local km at sea level. Used to place a grid-cell center back into the
    rotating frame so we can find the QT marker nearest to it."""
    r = radius_m / 1000.0
    lon360 = RAD(lon + 180.0)
    return (r * math.cos(RAD(lat)) * math.cos(lon360),
            r * math.cos(RAD(lat)) * math.sin(lon360),
            r * math.sin(RAD(lat)))


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
        "latitude": poi.latitude,
        "longitude": poi.longitude,
        "owner_id": poi.owner_id,
        "owner_handle": poi.owner_handle,
        "note": poi.note,
        "nearest_qt": poi.nearest_qt,
        "nearest_qt_dist_m": poi.nearest_qt_dist_m,
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
        "latitude": obs.latitude,
        "longitude": obs.longitude,
        "biome": obs.biome,
        "note": obs.note,
        "owner_id": obs.owner_id,
        "owner_handle": obs.owner_handle,
        "observed_at": obs.observed_at,
        "shard_id": obs.shard_id,
        "nearest_qt": obs.nearest_qt,
        "nearest_qt_dist_m": obs.nearest_qt_dist_m,
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

    # "What's around me" forecast — only meaningful on a body surface. Ores and
    # harvestables are forecast separately (their compositions are never pooled).
    forecast = None
    harvestable_forecast = None
    if container is not None and container.is_body and lat is not None:
        forecast = resource_forecast(
            nav, container.system, container.name, lat, lon, container.body_radius
        )
        harvestable_forecast = resource_forecast(
            nav, container.system, container.name, lat, lon, container.body_radius,
            category="harvestable",
        )

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
        "resource_forecast": forecast,
        "harvestable_forecast": harvestable_forecast,
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
    qt_marker: bool = False,
    note: str | None = None,
) -> Poi:
    """Create a POI at a global position, stored the same way the upstream
    dataset stores it: body-local rotating-frame km when at a container,
    global meters when in open space.

    Set qt_marker=True to record the POI as a jumpable quantum-travel marker
    (e.g. an Orbital Marker the user is mapping) so it becomes a candidate
    nearest-jump target for every other entity."""
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
        qt_marker=bool(qt_marker),
        custom=True,
        owner_id=owner_id,
        owner_handle=owner_handle,
        note=note,
    )
    poi.nearest_qt, poi.nearest_qt_dist_m = nearest_qt_marker(nav, poi, t_unix)
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
        "qt_marker": poi.qt_marker,
        "owner_id": poi.owner_id,
        "owner_handle": poi.owner_handle,
        "note": poi.note,
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
        qt_marker=bool(d.get("qt_marker")),
        custom=True,
        owner_id=d.get("owner_id"),
        owner_handle=d.get("owner_handle"),
        note=d.get("note"),
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


def _normalize_harvestable(data: dict) -> dict:
    data = dict(data)
    data["name"] = (data.get("name") or "Unknown")
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
    "harvestable": {
        "file": "harvestables.json",   # no legacy file; kept for symmetry
        "type_field": "name",
        "search_fields": ("name",),
        "normalize": _normalize_harvestable,
        "display_name": lambda d: d.get("name") or "Harvestable",
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
    shard_id: str | None = None,
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
        shard_id=shard_id,
    )
    obs.nearest_qt, obs.nearest_qt_dist_m = nearest_qt_marker(nav, obs, t_unix)
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
        "shard_id": obs.shard_id,
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
        shard_id=d.get("shard_id"),
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


def nearest_qt_marker(nav: NavData, target, t_ref: float):
    """(name, distance_m) of the nearest jumpable QT-marker POI to `target`
    (a Poi or Observation). A target that is itself a QT marker returns its own
    name at distance 0. Prefers a marker on the same body (rotation-invariant
    local distance); falls back to the nearest QT marker elsewhere in the same
    system. Returns (None, None) if there's no QT marker in the system."""
    if getattr(target, "qt_marker", False):
        return target.name, 0.0
    system = target.system
    # Same-body candidates: compare in the body-local frame (km, time-invariant).
    if target.container_name is not None and target.local_km is not None:
        best, best_d = None, math.inf
        for p in nav.qt_by_container.get((system, target.container_name), []):
            if p is target or p.local_km is None:
                continue
            d = dist3(target.local_km, p.local_km)
            if d < best_d:
                best, best_d = p, d
        if best is not None:
            return best.name, best_d * 1000.0       # km -> meters
    # Fallback: nearest QT marker anywhere in the same system (global meters).
    # Body centers are static in the dataset, so this is rotation-insensitive.
    tg = entity_global_m(nav, target, t_ref)
    if tg is None:
        return None, None
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
    return (best.name, best_d) if best else (None, None)


def assign_qt_markers(nav: NavData, t_ref: float | None = None) -> None:
    """(Re)build the QT index and assign every POI/observation its nearest
    QT-marker name + distance. Run after load and after a dataset refresh."""
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    index_qt_markers(nav)
    for p in nav.pois.values():
        p.nearest_qt, p.nearest_qt_dist_m = nearest_qt_marker(nav, p, t_ref)
    for o in nav.observations.values():
        o.nearest_qt, o.nearest_qt_dist_m = nearest_qt_marker(nav, o, t_ref)


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


# ---------------------------------------------------------------------------
# Resource-node statistics (spatial ore composition over a body)
# ---------------------------------------------------------------------------
#
# Ore nodes respawn in different exact spots, but type composition is believed
# to cluster by area. We bin sightings into an equal-area grid on the body and
# estimate, per area, the probability of each ore — many ores coexist, so the
# output is always a *composition*, never a single label.
#
# Sparsity is handled two ways so a solo logger gets useful answers early:
#   * Shrinkage: each area's composition is pulled toward the body-wide base
#     rate by a Dirichlet prior, so one node never reads as "100% X".
#   * Neighborhood blend: the "what's around me" forecast pools the player's
#     own cell with its ring-1 neighbors (distance-weighted).
# Both are presence-only estimates: an empty area means "not yet logged", not
# "nothing spawns" — coverage normalization is a later phase.

# Equal-area grid cell size (meters, target edge near the equator). Cells are
# uniform in longitude and in sin(latitude) — the Lambert cylindrical
# equal-area trick — so every cell covers the same area regardless of latitude.
RESOURCE_CELL_M = 2000.0
# Dirichlet/Laplace prior strength (pseudo-counts) pulling a sparse area toward
# the body base rate. Roughly the sample size at which local data and the prior
# carry equal weight.
RESOURCE_PRIOR_STRENGTH = 6.0
# Forecast neighborhood weights by Chebyshev ring distance (own cell vs ring-1).
_FORECAST_RING_WEIGHT = {0: 1.0, 1: 0.5}


def grid_dims(radius_m: float, cell_m: float = RESOURCE_CELL_M) -> tuple[int, int]:
    """(n_lon, n_lat) cell counts for a body of the given radius. lon spans the
    full circumference (2 pi R); lat spans pole-to-pole as sin(lat) over 2 R."""
    n_lon = max(1, round(2 * math.pi * radius_m / cell_m))
    n_lat = max(1, round(2 * radius_m / cell_m))
    return n_lon, n_lat


def grid_cell(lat: float, lon: float, radius_m: float, cell_m: float = RESOURCE_CELL_M):
    """(i_lon, i_lat) equal-area cell index for a lat/lon on a body."""
    n_lon, n_lat = grid_dims(radius_m, cell_m)
    u = ((lon + 180.0) % 360.0) / 360.0
    v = (math.sin(RAD(lat)) + 1.0) / 2.0
    i_lon = min(n_lon - 1, int(u * n_lon))
    i_lat = min(n_lat - 1, max(0, int(v * n_lat)))
    return i_lon, i_lat


def grid_cell_center(i_lon: int, i_lat: int, radius_m: float, cell_m: float = RESOURCE_CELL_M):
    """(lat, lon) of the center of an equal-area cell — used to draw the map
    heatmap and to measure neighbor distance."""
    n_lon, n_lat = grid_dims(radius_m, cell_m)
    lon = (i_lon + 0.5) / n_lon * 360.0 - 180.0
    sin_lat = max(-1.0, min(1.0, 2.0 * (i_lat + 0.5) / n_lat - 1.0))
    return DEG(math.asin(sin_lat)), lon


# The forecast / element-finder / heatmap stats are category-agnostic: they bin
# sightings into the equal-area grid and rank by a type field. Resources rank by
# "ore", harvestables by "name". Each function takes `category` and reads that
# category's type field, so harvestables reuse the exact same math (kept on its
# own data — ore and harvestable compositions are never pooled).
def _type_of(o: Observation, field: str) -> str:
    return o.data.get(field) or "Unknown"


def _category_field(category: str) -> str:
    return OBSERVATION_CATEGORIES[category]["type_field"]


def _obs_on_body(nav: NavData, system: str, body: str,
                 category: str = "resource") -> list[Observation]:
    return [
        o for o in nav.observations.values()
        if o.category == category and o.system == system
        and o.container_name == body
        and o.latitude is not None and o.longitude is not None
    ]


def _wilson_lower_bound(successes: float, n: float, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for a proportion — high only
    when the observed ratio is high AND backed by enough samples. Used to rank
    hotspots so a lucky 3/3 doesn't outrank a solid 8/10."""
    if n <= 0:
        return 0.0
    phat = successes / n
    z2 = z * z
    centre = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return max(0.0, (centre - margin) / (1 + z2 / n))


def _shrunk_composition(counts: dict, total: float, prior: dict, alpha: float) -> dict:
    """P(ore) for a (possibly fractional) count dict, pulled toward `prior` by
    `alpha` pseudo-counts. With no local data this returns `prior`."""
    denom = total + alpha
    if denom <= 0:
        return dict(prior)
    return {
        ore: (counts.get(ore, 0.0) + alpha * prior.get(ore, 0.0)) / denom
        for ore in (set(counts) | set(prior))
    }


def body_base_rate(nav: NavData, system: str, body: str,
                   category: str = "resource") -> tuple[dict, int]:
    """(composition, n) of every sighting of `category` on the body — the prior
    that each area shrinks toward."""
    field = _category_field(category)
    counts: dict[str, int] = {}
    for o in _obs_on_body(nav, system, body, category):
        counts[_type_of(o, field)] = counts.get(_type_of(o, field), 0) + 1
    n = sum(counts.values())
    if n == 0:
        return {}, 0
    return {ore: c / n for ore, c in counts.items()}, n


def _ranked(comp: dict, counts: dict) -> list[dict]:
    rows = [
        {"ore": ore, "p": p, "n": int(counts.get(ore, 0))}
        for ore, p in comp.items()
    ]
    rows.sort(key=lambda r: (-r["p"], r["ore"]))
    return rows


def resource_forecast(
    nav: NavData, system: str, body: str, lat: float, lon: float,
    radius_m: float, cell_m: float = RESOURCE_CELL_M, category: str = "resource",
) -> dict | None:
    """Ranked type likelihoods (ore for resources, name for harvestables) for the
    player's neighborhood (own cell + ring-1, distance-weighted), shrunk toward
    the body base rate. None until the body has at least one sighting of the
    category."""
    field = _category_field(category)
    base, base_n = body_base_rate(nav, system, body, category)
    if base_n == 0:
        return None
    n_lon, _ = grid_dims(radius_m, cell_m)
    pi, pj = grid_cell(lat, lon, radius_m, cell_m)
    weighted: dict[str, float] = {}
    counts: dict[str, int] = {}
    total_w = 0.0
    n_local = 0
    for o in _obs_on_body(nav, system, body, category):
        oi, oj = grid_cell(o.latitude, o.longitude, radius_m, cell_m)
        ring = max(min((oi - pi) % n_lon, (pi - oi) % n_lon), abs(oj - pj))
        w = _FORECAST_RING_WEIGHT.get(ring)
        if w is None:
            continue
        ore = _type_of(o, field)
        weighted[ore] = weighted.get(ore, 0.0) + w
        counts[ore] = counts.get(ore, 0) + 1
        total_w += w
        n_local += 1
    comp = _shrunk_composition(weighted, total_w, base, RESOURCE_PRIOR_STRENGTH)
    return {
        "ranked": _ranked(comp, counts),
        "n_local": n_local,
        "n_body": base_n,
        "cell_m": cell_m,
    }


def resource_cells(
    nav: NavData, system: str, body: str, radius_m: float,
    cell_m: float = RESOURCE_CELL_M, category: str = "resource",
) -> list[dict]:
    """Per-cell shrunk type composition for every cell with at least one sighting
    of `category` (the only cells we can speak to) — drives the map heatmap."""
    field = _category_field(category)
    base, base_n = body_base_rate(nav, system, body, category)
    if base_n == 0:
        return []
    cells: dict[tuple[int, int], dict] = {}
    for o in _obs_on_body(nav, system, body, category):
        key = grid_cell(o.latitude, o.longitude, radius_m, cell_m)
        c = cells.setdefault(key, {})
        c[_type_of(o, field)] = c.get(_type_of(o, field), 0) + 1
    out = []
    for (i, j), counts in cells.items():
        n = sum(counts.values())
        comp = _shrunk_composition(counts, n, base, RESOURCE_PRIOR_STRENGTH)
        clat, clon = grid_cell_center(i, j, radius_m, cell_m)
        out.append({
            "lat": clat, "lon": clon, "n": n,
            # "top" = the ore actually logged most here (raw plurality), so a
            # sparse cell shows what *you found* rather than collapsing to the
            # body's dominant ore under the prior. "comp" stays the shrunk
            # probability used for the specific-ore heatmap and the forecast.
            "top": max(counts, key=counts.get),
            "comp": comp,
        })
    return out


def resource_ore_names(nav: NavData, category: str = "resource") -> list[str]:
    """Sorted type names that appear in sightings of `category` (ore for
    resources, name for harvestables) — populates the element-finder picker."""
    field = _category_field(category)
    return sorted({_type_of(o, field) for o in nav.observations.values()
                   if o.category == category})


# Body hierarchy is encoded in InternalName, not Type (the dataset types moons
# as "Planet"): a planet is "<System><number>" (e.g. Stanton2 = Crusader) and a
# moon appends a letter ("Stanton2b" = Daymar, moon of Crusader).
_MOON_RE = re.compile(r"^(.+?\d+)[a-z]+$")


def parent_planet(nav: NavData, moon: Container) -> Container | None:
    """The planet a moon orbits, from its InternalName (Stanton2b -> Stanton2),
    or None if the body isn't a moon."""
    m = _MOON_RE.match(moon.internal_name or "")
    if not m:
        return None
    parent_internal = m.group(1)
    return next(
        (c for c in nav.containers.values()
         if c.system == moon.system and c.internal_name == parent_internal and c is not moon),
        None,
    )


def _planets_in_system(nav: NavData, system: str) -> list[Container]:
    """Top-level planets (QT-reachable directly): bodies whose InternalName ends
    in a digit — moons end in a letter — excluding stars."""
    return [
        c for c in nav.containers.values()
        if c.system == system and c.body_radius > 0 and c.type != "Star"
        and not _MOON_RE.match(c.internal_name or "")
        and re.search(r"\d$", c.internal_name or "")
    ]


def nearest_planet(nav: NavData, system: str, pos) -> Container | None:
    planets = _planets_in_system(nav, system)
    return min(planets, key=lambda p: dist3(pos, p.pos)) if planets else None


# ---------------------------------------------------------------------------
# Travel cost — the reusable QT-distance primitive for the route planner.
#
# This generalizes the per-hotspot travel proxy in resource_hotspots into a
# cost between any two stops. The intra-system rule is identical to the one
# proven there: jump to the destination's nearest QT marker, and if the
# destination is a moon you aren't already neighboring, hop to its parent
# planet first (player -> planet -> moon).
# ---------------------------------------------------------------------------

# Currently-functioning jump-gate network (in-game): Stanton — Pyro — Nyx.
# Stanton<->Nyx is not a direct lane; it routes through Pyro. The Terra/Magnus
# jump points present in the dataset are NOT functioning gates, so they are
# deliberately excluded here.
GATE_LINKS: dict[str, list[str]] = {
    "Stanton": ["Pyro"],
    "Pyro": ["Stanton", "Nyx"],
    "Nyx": ["Pyro"],
}

# Gate POI on a system's side, toward a neighbor: (from_system, to_system) ->
# poi id. The dataset only carries clean endpoints for some sides; missing
# sides degrade to an approach-only cost (the leg is flagged `partial`).
GATE_ENDPOINTS: dict[tuple[str, str], int] = {
    ("Stanton", "Pyro"): 480,   # "Jump Point to Pyro" (Stanton-side, space POI)
    ("Nyx", "Pyro"): 642,       # "Gateway Station Pyro" (Nyx-side)
}

# Nominal fixed cost of traversing a jump-gate tunnel, in meters-equivalent, so
# the solver consistently prefers grouping same-system stops. Tunable; the gate
# approach legs already dominate, this just guarantees a cross-system penalty.
GATE_TRAVERSAL_M = 1.0e9


def _nearest_qt_poi(nav: NavData, target, t_ref: float):
    """The nearest jumpable QT-marker Poi to `target` (or `target` itself if it
    is one). Same selection rule as nearest_qt_marker — prefer a marker on the
    same body (rotation-invariant local distance), else nearest in-system — but
    returns the Poi so callers can resolve its position."""
    if getattr(target, "qt_marker", False):
        return target
    system = target.system
    if target.container_name is not None and target.local_km is not None:
        best, best_d = None, math.inf
        for p in nav.qt_by_container.get((system, target.container_name), []):
            if p is target or p.local_km is None:
                continue
            d = dist3(target.local_km, p.local_km)
            if d < best_d:
                best, best_d = p, d
        if best is not None:
            return best
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
    return best


def _intra_leg(nav: NavData, from_pos, dst, t_ref: float):
    """(distance_m, via, qt_marker_name) for an in-system hop from a global
    position to a destination Poi, applying the planet->moon two-hop rule."""
    cont = nav.container_of(dst)
    # A directly QT-able destination — a flagged QT marker, or any space POI
    # (stations and jump points float in the system frame) — is its own aim
    # point. Only a non-marker surface POI needs the nearest-marker substitute
    # (you can't quantum to a bare surface point; you jump to its marker first).
    if getattr(dst, "qt_marker", False) or cont is None:
        ref = entity_global_m(nav, dst, t_ref)
        marker_name = dst.name
    else:
        marker = _nearest_qt_poi(nav, dst, t_ref)
        ref = entity_global_m(nav, marker, t_ref) if marker is not None else cont.pos
        marker_name = marker.name if marker is not None else None
    if ref is None:
        return None, None, marker_name
    parent = parent_planet(nav, cont) if cont is not None else None
    in_local = parent is not None and nearest_planet(nav, dst.system, from_pos) is parent
    if parent is not None and not in_local:
        dist = dist3(from_pos, parent.pos) + dist3(parent.pos, ref)
        via = parent.name
    else:
        dist = dist3(from_pos, ref)
        via = None
    return dist, via, marker_name


def _gate_poi(nav: NavData, from_system: str, to_system: str):
    """The gate Poi on `from_system`'s side toward `to_system`, or None if the
    dataset doesn't carry that endpoint."""
    pid = GATE_ENDPOINTS.get((from_system, to_system))
    return nav.pois.get(pid) if pid is not None else None


def system_path(from_system: str, to_system: str) -> list[str] | None:
    """Shortest chain of systems through the functioning gate network, inclusive
    of both ends (e.g. Stanton -> Nyx == [Stanton, Pyro, Nyx]). None if
    unconnected."""
    if from_system == to_system:
        return [from_system]
    seen = {from_system}
    queue: list[list[str]] = [[from_system]]
    while queue:
        path = queue.pop(0)
        for nxt in GATE_LINKS.get(path[-1], []):
            if nxt in seen:
                continue
            if nxt == to_system:
                return path + [nxt]
            seen.add(nxt)
            queue.append(path + [nxt])
    return None


def travel_cost(nav: NavData, src, dst, t_ref: float | None = None) -> dict:
    """QT travel cost from stop `src` to stop `dst` (both Poi). Returns a dict:

        distance_m    total QT distance for the leg
        qt_marker     name of the marker to jump to on arrival (or None)
        via           parent planet for the moon two-hop rule (or None)
        cross_system  True if the leg crosses a jump gate
        via_gate      list of system names traversed, gate-first (or None)
        partial       True if a gate endpoint was missing and the cost is a
                      lower bound (approach-only on that side)

    Cross-system legs route through the functioning Stanton-Pyro-Nyx network:
    cost = src -> exit gate(src side) + gate traversal(s) + entry gate -> dst,
    using the same intra-system primitives one level up."""
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    from_pos = entity_global_m(nav, src, t_ref)
    if from_pos is None:
        return {"distance_m": None, "qt_marker": None, "via": None,
                "cross_system": False, "via_gate": None, "partial": True}

    if src.system == dst.system:
        dist, via, marker = _intra_leg(nav, from_pos, dst, t_ref)
        return {"distance_m": dist, "qt_marker": marker, "via": via,
                "cross_system": False, "via_gate": None, "partial": dist is None}

    path = system_path(src.system, dst.system)
    if path is None:                          # unconnected systems
        return {"distance_m": None, "qt_marker": None, "via": None,
                "cross_system": True, "via_gate": None, "partial": True}

    total = 0.0
    partial = False
    # src side: hop to the gate leaving src.system toward the next system.
    out_gate = _gate_poi(nav, src.system, path[1])
    if out_gate is not None:
        d, _, _ = _intra_leg(nav, from_pos, out_gate, t_ref)
        total += d or 0.0
    else:
        partial = True                        # unknown source-side gate
    # one tunnel traversal per gate crossed.
    total += GATE_TRAVERSAL_M * (len(path) - 1)
    # dst side: hop from the entry gate (last system's side toward prev) to dst.
    in_gate = _gate_poi(nav, dst.system, path[-2])
    if in_gate is not None:
        gate_pos = entity_global_m(nav, in_gate, t_ref)
        d, via, marker = _intra_leg(nav, gate_pos, dst, t_ref)
        total += d or 0.0
    else:
        # Unknown entry gate: floor the dst side with its local QT approach.
        marker_poi = _nearest_qt_poi(nav, dst, t_ref)
        total += dst.nearest_qt_dist_m or 0.0
        via, marker = None, (marker_poi.name if marker_poi else None)
        partial = True
    return {"distance_m": total, "qt_marker": marker, "via": via,
            "cross_system": True, "via_gate": path, "partial": partial}


def resource_hotspots(
    nav: NavData, ore: str, system: str | None = None, body: str | None = None,
    limit: int = 20, cell_m: float = RESOURCE_CELL_M,
    from_pos=None, t_ref: float | None = None, sort: str = "likely",
    category: str = "resource",
) -> list[dict]:
    """Known areas richest in `ore`, ranked, across every body (optionally
    filtered to one system/body) — a "where do I fly to mine X" planner.

    Bins resource sightings per body into the equal-area grid. Each cell reports
    the empirical hit rate of `ore` (n_ore / n samples there), but the base rank
    uses the Wilson lower bound of that rate, so a well-sampled 8/10 outranks a
    lucky 3/3 — the planner trusts a percentage more once it's backed by more
    visits. Each hotspot carries the QT marker to jump to.

    With `from_pos` (the player's global position), each hotspot also gets the
    straight-line travel distance to its jump marker, and `sort` chooses how to
    order:
      "likely" — by confidence-adjusted likelihood (ignores distance).
      "near"   — by travel distance (closest first).
      "value"  — likelihood discounted by travel, to trade off rich vs close.
    Without `from_pos` it always falls back to "likely"."""
    ore = ore.strip()
    if not ore:
        return []
    field = _category_field(category)
    groups: dict[tuple[str, str], list[Observation]] = {}
    for o in nav.observations.values():
        if o.category != category or o.latitude is None or o.longitude is None:
            continue
        if (system and o.system != system) or (body and o.container_name != body):
            continue
        groups.setdefault((o.system, o.container_name), []).append(o)

    out = []
    for (sys, bod), obs in groups.items():
        cont = nav.containers.get((sys, bod))
        if cont is None or cont.body_radius <= 0:
            continue
        cells: dict[tuple[int, int], dict] = {}
        for o in obs:
            key = grid_cell(o.latitude, o.longitude, cont.body_radius, cell_m)
            c = cells.setdefault(key, {"counts": {}, "bands": []})
            c["counts"][_type_of(o, field)] = c["counts"].get(_type_of(o, field), 0) + 1
            if _type_of(o, field) == ore and isinstance(o.data.get("band"), (int, float)):
                c["bands"].append(o.data["band"])
        for key, c in cells.items():
            n_ore = c["counts"].get(ore, 0)
            if n_ore == 0:
                continue
            n = sum(c["counts"].values())
            clat, clon = grid_cell_center(*key, cont.body_radius, cell_m)
            out.append({
                "system": sys, "body": bod, "lat": clat, "lon": clon,
                "p": n_ore / n, "score": _wilson_lower_bound(n_ore, n),
                "n": n, "n_ore": n_ore,
                "avg_band": (sum(c["bands"]) / len(c["bands"])) if c["bands"] else None,
            })

    # Keep a generous likelihood-ranked pool, then attach jump marker + travel
    # for just that pool (so distance can reorder a sane set without running the
    # marker search over every cell).
    out.sort(key=lambda r: (-r["score"], -r["n_ore"], -r["n"]))
    out = out[: max(limit * 4, 50)]
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    for h in out:
        cont = nav.containers.get((h["system"], h["body"]))
        target = Poi(
            id=-1, name="", system=h["system"], container_name=h["body"], type="",
            local_km=local_km_from_latlon(h["lat"], h["lon"], cont.body_radius),
            global_m=None, latitude=h["lat"], longitude=h["lon"], height_m=None,
            qt_marker=False,
        )
        h["nearest_qt"], h["nearest_qt_dist_m"] = nearest_qt_marker(nav, target, ROTATION_EPOCH)
        h["nearest_qt_id"] = next(
            (p.id for p in nav.qt_markers
             if p.name == h["nearest_qt"] and p.system == h["system"]),
            None,
        )
        # Travel proxy: distance from the player to the jump marker (its position
        # at t_ref, since rotating bodies move their markers), falling back to
        # the body center when the hotspot has no QT marker.
        #
        # Business rule: a moon is only reachable directly when you're already in
        # its planet's neighborhood (your nearest planet IS its parent). From
        # anywhere else you must QT to the parent planet first, then the moon —
        # so the cost is player->planet + planet->moon, and we flag the via-hop.
        h["travel_m"] = None
        h["via"] = None
        if from_pos is not None:
            marker = nav.pois.get(h["nearest_qt_id"]) if h["nearest_qt_id"] is not None else None
            ref = entity_global_m(nav, marker, t_ref) if marker is not None else cont.pos
            if ref is not None:
                parent = parent_planet(nav, cont)
                in_local_system = (
                    parent is not None
                    and nearest_planet(nav, cont.system, from_pos) is parent
                )
                if parent is not None and not in_local_system:
                    h["via"] = parent.name
                    h["travel_m"] = dist3(from_pos, parent.pos) + dist3(parent.pos, ref)
                else:
                    h["travel_m"] = dist3(from_pos, ref)

    if from_pos is not None and sort in ("near", "value"):
        if sort == "near":
            out.sort(key=lambda r: (r["travel_m"] if r["travel_m"] is not None else math.inf,
                                    -r["score"]))
        else:  # "value": likelihood discounted by travel, scaled to the data
            travels = sorted(r["travel_m"] for r in out if r["travel_m"] is not None)
            scale = max(travels[len(travels) // 2], 1.0) if travels else 1.0
            def value(r):
                tm = r["travel_m"] if r["travel_m"] is not None else scale * 10
                return r["score"] / (1 + tm / scale)
            out.sort(key=lambda r: (-value(r), -r["score"]))
    # else: keep the likelihood order already in place
    return out[:limit]


# ---------------------------------------------------------------------------
# Route planner — the pickup-and-delivery solver.
#
# A `package` = {id?, commodity, scu, from_id, to_id}; from->to encodes
# pickup-before-dropoff precedence for free. plan_route merges packages sharing
# a location into one stop, then orders the stops to minimize total QT distance
# under two constraints: precedence (a package's pickup precedes its dropoff)
# and capacity (onboard SCU never exceeds usable_scu). Pure stdlib.
# ---------------------------------------------------------------------------

# Planner timing knobs. Nominal until the drive catalog lands (build-order step
# 1), at which point per-leg time becomes drive-accurate; distance is exact now.
QT_CRUISE_SPEED_MS = 179_000_000.0   # ~179,000 km/s, nominal QT cruise speed
QT_LEG_OVERHEAD_S = 30.0             # spool-up + accel/decel + cooldown per jump
STOP_DWELL_S = 120.0                 # loading/unloading dwell per stop

# Above this many stops, exhaustive branch-and-bound is replaced by a
# precedence/capacity-safe nearest-neighbor pass. Real cargo runs sit well under
# this; the bound keeps the worst case bounded.
_BNB_MAX_STOPS = 11


def _leg_time_s(distance_m):
    if distance_m is None:
        return None
    return QT_LEG_OVERHEAD_S + distance_m / QT_CRUISE_SPEED_MS


def _pkg_view(p):
    # `contract` is a player-supplied grouping label, carried through for display
    # only (it never affects routing) so the UI can colour-group packages and
    # flag which contract a dropoff completes.
    return {"id": p["id"], "commodity": p["commodity"], "scu": p["scu"],
            "from_id": p["from_id"], "to_id": p["to_id"], "contract": p.get("contract")}


def _leg_view(leg):
    if leg is None:
        return None
    return {"distance_m": leg["distance_m"], "eta_s": _leg_time_s(leg["distance_m"]),
            "qt_marker": leg["qt_marker"], "via": leg["via"],
            "cross_system": leg["cross_system"], "via_gate": leg["via_gate"],
            "partial": leg["partial"]}


def plan_route(nav: NavData, packages, usable_scu, start_id=None, t_ref=None) -> dict:
    """Order accepted cargo packages into an efficient run.

    Returns {summary, stops}. `summary` carries feasibility (peak load vs.
    usable_scu, and the minimum capacity the bundle actually requires), totals
    (distance, time), and counts. `stops` is the ordered visit list, each with
    its pickups/dropoffs, the arrival leg detail, and running onboard SCU.
    An over-capacity or unconnected bundle returns feasible=False with an empty
    stop list and the min_capacity_scu needed to make it work."""
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    usable_scu = float(usable_scu)

    # --- normalize packages ---
    pkgs = []
    for i, p in enumerate(packages):
        fid, tid = int(p["from_id"]), int(p["to_id"])
        if fid not in nav.pois or tid not in nav.pois:
            raise ValueError(f"unknown POI id in package {p.get('id') if p.get('id') is not None else i}")
        # Fall back to the row index when no id was supplied (a present-but-None
        # `id`, as Pydantic emits, must still get a unique index — not stay None).
        pid = p.get("id")
        if pid is None:
            pid = i
        pkgs.append({"id": pid, "commodity": p.get("commodity"),
                     "scu": float(p.get("scu") or 0), "from_id": fid, "to_id": tid,
                     "contract": p.get("contract")})

    if not pkgs:
        return {"summary": {"feasible": True, "num_stops": 0, "num_packages": 0,
                            "usable_scu": usable_scu, "peak_load_scu": 0.0,
                            "min_capacity_scu": 0.0, "total_distance_m": 0.0,
                            "total_time_s": 0.0, "start_id": None}, "stops": []}

    # --- merge packages sharing a location into stops ---
    loc_ids = []
    for p in pkgs:
        for lid in (p["from_id"], p["to_id"]):
            if lid not in loc_ids:
                loc_ids.append(lid)
    idx = {lid: k for k, lid in enumerate(loc_ids)}
    n = len(loc_ids)
    stops = [{"id": lid, "poi": nav.pois[lid], "loads": [], "drops": [],
              "load_scu": 0.0, "drop_scu": 0.0} for lid in loc_ids]
    preds = [set() for _ in range(n)]
    for pi, p in enumerate(pkgs):
        a, b = idx[p["from_id"]], idx[p["to_id"]]
        stops[a]["loads"].append(pi); stops[a]["load_scu"] += p["scu"]
        stops[b]["drops"].append(pi); stops[b]["drop_scu"] += p["scu"]
        if a != b:
            preds[b].add(a)

    # --- cost matrix ---
    legs = [[None] * n for _ in range(n)]
    dmat = [[math.inf] * n for _ in range(n)]
    for a in range(n):
        for b in range(n):
            if a == b:
                continue
            leg = travel_cost(nav, stops[a]["poi"], stops[b]["poi"], t_ref)
            legs[a][b] = leg
            if leg["distance_m"] is not None:
                dmat[a][b] = leg["distance_m"]
    start_poi = nav.pois.get(int(start_id)) if start_id is not None else None
    start_legs = [None] * n
    start_d = [0.0] * n
    if start_poi is not None:
        for b in range(n):
            leg = travel_cost(nav, start_poi, stops[b]["poi"], t_ref)
            start_legs[b] = leg
            start_d[b] = leg["distance_m"] if leg["distance_m"] is not None else math.inf

    def step_cost(prev, j):
        return start_d[j] if prev is None else dmat[prev][j]

    # --- minimum capacity the bundle requires (precedence only, min peak) ---
    min_cap = _min_capacity(stops, preds, n)

    # --- order the stops (minimize distance under precedence + capacity) ---
    if n <= _BNB_MAX_STOPS:
        order, peak = _bnb_order(stops, preds, dmat, start_d, usable_scu, n)
    else:
        order, peak = _greedy_order(stops, preds, step_cost, usable_scu, n)

    if order is None:
        return {"summary": {"feasible": False, "num_stops": n,
                            "num_packages": len(pkgs), "usable_scu": usable_scu,
                            "peak_load_scu": None, "min_capacity_scu": round(min_cap, 2),
                            "total_distance_m": None, "total_time_s": None,
                            "start_id": start_poi.id if start_poi else None}, "stops": []}

    # --- build output ---
    out_stops = []
    onboard = total_dist = 0.0
    for k, si in enumerate(order):
        s = stops[si]; poi = s["poi"]
        onboard += s["load_scu"] - s["drop_scu"]
        leg = start_legs[si] if k == 0 else legs[order[k - 1]][si]
        if leg and leg["distance_m"] is not None:
            total_dist += leg["distance_m"]
        out_stops.append({
            "stop_id": s["id"], "name": poi.name, "system": poi.system,
            "type": poi.type,
            "pickups": [_pkg_view(pkgs[pi]) for pi in s["loads"]],
            "dropoffs": [_pkg_view(pkgs[pi]) for pi in s["drops"]],
            "leg": _leg_view(leg),
            "onboard_scu": round(onboard, 2),
        })
    total_time = sum((_leg_time_s(s["leg"]["distance_m"]) or 0.0)
                     for s in out_stops if s["leg"]) + STOP_DWELL_S * n

    return {"summary": {"feasible": True, "num_stops": n, "num_packages": len(pkgs),
                        "usable_scu": usable_scu, "peak_load_scu": round(peak, 2),
                        "min_capacity_scu": round(min_cap, 2),
                        "total_distance_m": total_dist, "total_time_s": total_time,
                        "start_id": start_poi.id if start_poi else None},
            "stops": out_stops}


def _bnb_order(stops, preds, dmat, start_d, cap, n):
    """Branch-and-bound: least-distance precedence+capacity-feasible order.
    Returns (order, peak_load) or (None, None) if no feasible order exists."""
    best = {"cost": math.inf, "order": None, "peak": None}

    def dfs(order, visited, onboard, cost, peak):
        if cost >= best["cost"]:
            return
        if len(order) == n:
            best.update(cost=cost, order=list(order), peak=peak)
            return
        prev = order[-1] if order else None
        for j in range(n):
            if j in visited or not preds[j].issubset(visited):
                continue
            no = onboard + stops[j]["load_scu"] - stops[j]["drop_scu"]
            if no > cap + 1e-9:
                continue
            step = start_d[j] if prev is None else dmat[prev][j]
            if step == math.inf:
                continue
            dfs(order + [j], visited | {j}, no, cost + step, max(peak, no))

    dfs([], set(), 0.0, 0.0, 0.0)
    return (best["order"], best["peak"]) if best["order"] is not None else (None, None)


def _greedy_order(stops, preds, step_cost, cap, n):
    """Nearest-neighbor fallback for large stop sets: at each step take the
    nearest precedence- and capacity-feasible stop."""
    order, visited = [], set()
    onboard = peak = 0.0
    prev = None
    while len(order) < n:
        cands = [
            j for j in range(n)
            if j not in visited and preds[j].issubset(visited)
            and onboard + stops[j]["load_scu"] - stops[j]["drop_scu"] <= cap + 1e-9
            and step_cost(prev, j) != math.inf
        ]
        if not cands:
            return None, None
        j = min(cands, key=lambda j: step_cost(prev, j))
        onboard += stops[j]["load_scu"] - stops[j]["drop_scu"]
        peak = max(peak, onboard)
        order.append(j); visited.add(j); prev = j
    return order, peak


def _min_capacity(stops, preds, n):
    """Minimum peak onboard SCU achievable over any precedence-valid order —
    i.e. the smallest ship the bundle can possibly fit on. Exhaustive for small
    sets, greedy (least-load-first) above the bound."""
    if n > _BNB_MAX_STOPS:
        visited = set()
        onboard = peak = 0.0
        while len(visited) < n:
            cands = [j for j in range(n) if j not in visited and preds[j].issubset(visited)]
            j = min(cands, key=lambda j: stops[j]["load_scu"] - stops[j]["drop_scu"])
            onboard += stops[j]["load_scu"] - stops[j]["drop_scu"]
            peak = max(peak, onboard)
            visited.add(j)
        return peak
    best = {"peak": math.inf}

    def dfs(visited, onboard, peak):
        if peak >= best["peak"]:
            return
        if len(visited) == n:
            best["peak"] = peak
            return
        for j in range(n):
            if j in visited or not preds[j].issubset(visited):
                continue
            no = onboard + stops[j]["load_scu"] - stops[j]["drop_scu"]
            dfs(visited | {j}, no, max(peak, no))

    dfs(set(), 0.0, 0.0)
    return best["peak"]
