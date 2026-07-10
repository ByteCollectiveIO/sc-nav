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
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    private: bool = False            # owner-only; hidden from the rest of the org
    note: str | None = None          # free-text context; upstream POIs map from Comment
    nearest_qt: str | None = None    # name of nearest QT-marker POI (computed)
    nearest_qt_dist_m: float | None = None  # distance to that marker, meters
    source: str = "starmap"          # catalog origin: "starmap" | "wiki" (#28)
    arrival_radius_m: float | None = None   # per-POI QT arrival radius (#28b)


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
    synth_container_pois(nav)
    return nav


# Container types that are dockable cargo locations (Lagrange stations, jump
# points, asteroid/refinery/naval bases) but live only in the container catalog,
# not the POI catalog — so they aren't otherwise searchable or pickable as
# pickup/dropoff stops. Bodies, bare Lagrange points and asteroid belts are not
# cargo destinations and stay out.
_STATION_CONTAINER_TYPES = {
    "RestStop", "Refinery Station", "Naval Station", "AsteroidBase", "Jumppoint",
}
# Reserved POI id range for these synthesized container-stations (well clear of
# starmap ids ~<50k, custom 1M+, observations 2M+). Ids are derived from the
# container's internal name so they're stable across restarts (run persistence
# can reference them).
CONTAINER_POI_START = 3_000_000


def _station_lagrange_code(internal_name: str | None) -> str | None:
    """The Lagrange designation players actually search by, pulled from a
    station's internal name ('ARC-L1-A Station' -> 'ARC-L1'); None when the name
    isn't Lagrange-style (e.g. 'PYR6 Station', 'Hurston-Wikelo')."""
    base = re.sub(r"[-\s]*[A-Z]?\s*Station\s*$", "", internal_name or "").strip()
    return base if re.search(r"-L[1-5]\b", base) else None


def synth_container_pois(nav: NavData) -> None:
    """Add cargo-relevant station containers to nav.pois as directly-QT-able
    space POIs, so the navigator + cargo planner can search and route to them.
    Skips any whose name already exists as a POI. Lagrange stations get their
    L-code folded into the name ('Wide Forest Station (ARC-L1)') for search."""
    existing = {p.name.strip().lower() for p in nav.pois.values()}
    used_ids = set(nav.pois)
    for cont in nav.containers.values():
        if cont.type not in _STATION_CONTAINER_TYPES:
            continue
        if cont.name.strip().lower() in existing:
            continue
        base = re.sub(r"\s+", " ", cont.name).strip()   # raw names carry stray tabs
        code = _station_lagrange_code(cont.internal_name)
        name = f"{base} ({code})" if code and code.lower() not in base.lower() else base
        pid = CONTAINER_POI_START + (zlib.crc32((cont.internal_name or cont.name).encode()) % 900_000)
        while pid in used_ids:        # crc collisions are vanishingly rare; probe anyway
            pid += 1
        used_ids.add(pid)
        nav.pois[pid] = Poi(
            id=pid, name=name, system=cont.system, container_name=None,
            type=cont.type, local_km=None, global_m=cont.pos,
            latitude=None, longitude=None, height_m=None, qt_marker=True,
        )


# ---------------------------------------------------------------------------
# Wiki locations catalog (#28): poi/locations.json, distilled from the SC Wiki
# API by tools/sync_locations.py. Two consumers: add_wiki_pois imports the
# records as routable POIs (org-toggled, like the starmap catalog), and
# annotate_arrival_radii attaches per-POI QT arrival radii to *whatever* POIs
# are loaded (always on — physics metadata, not content).
# ---------------------------------------------------------------------------

# Reserved id range for wiki-catalog POIs, derived from the record uuid so ids
# are stable across syncs and restarts. Clear of starmap (<50k), custom (1M+),
# observations (2M+) and synthesized container-stations (3M+).
WIKI_POI_START = 4_000_000

# Wiki entity types -> the display vocabulary the POI catalog already uses.
_WIKI_TYPE_LABELS = {
    "Manmade": "Station",
    "Manmade_VisibleOnInteraction": "Station",
    "LandingZone": "Landing Zone",
    "Asteroid_ValidQT": "Asteroid Cluster",
    "Asteroid": "Asteroid Cluster",
    "NavPoint": "Nav Point",
    "JumpPoint": "Jump Point",
    "PointOfInterest": "Point of Interest",
}


def wiki_name_key(name: str) -> tuple[str, ...]:
    """Order-insensitive token key for matching one place name across catalogs.
    Handles the naming drift between the sources: 'ARC-L1 Wide Forest Station'
    (wiki) vs 'Wide Forest Station (ARC-L1)' (synth_container_pois' fold) vs
    'ARC L1' vs 'ARC-L1' — lowercase, strip quotes/parens, fold hyphens to
    spaces, compare as a sorted token set."""
    cleaned = re.sub(r"[()\"'’]", " ", (name or "").lower()).replace("-", " ")
    return tuple(sorted(cleaned.split()))


def add_wiki_pois(nav: NavData, locations: list[dict]) -> int:
    """Import wiki-catalog locations into nav.pois as routable POIs (#28a).

    Dedup is by (system, token-set name) against everything already loaded —
    starmap POIs, containers, synthesized stations — with the incumbent
    winning, so enabling both catalogs never doubles a place. Never matches on
    position: a POI CIG moved between patches would otherwise double up.
    Call after parse_data/synth_container_pois and before merge_custom_pois
    (custom POIs are user content and may legitimately shadow names).
    Returns the number of POIs added."""
    existing: set[tuple[str, tuple[str, ...]]] = set()
    for p in nav.pois.values():
        existing.add((p.system.lower(), wiki_name_key(p.name)))
    for (system, cname) in nav.containers:
        existing.add((system.lower(), wiki_name_key(cname)))

    added = 0
    for rec in locations:
        key = (rec["system"].lower(), wiki_name_key(rec["name"]))
        if key in existing:
            continue
        existing.add(key)
        pid = WIKI_POI_START + (zlib.crc32(rec["uuid"].encode()) % 900_000)
        while pid in nav.pois:          # crc collisions are vanishingly rare; probe anyway
            pid += 1
        local = rec.get("local_km")
        glob = rec.get("global_m")
        nav.pois[pid] = Poi(
            id=pid,
            name=rec["name"],
            system=rec["system"],
            container_name=rec.get("container") if local else None,
            type=_WIKI_TYPE_LABELS.get(rec.get("type") or "", rec.get("type") or "Unknown"),
            local_km=tuple(local) if local else None,
            global_m=tuple(glob) if glob else None,
            latitude=None, longitude=None, height_m=None,
            qt_marker=bool(rec.get("qt_valid")),
            source="wiki",
            arrival_radius_m=rec.get("arrival_m"),
        )
        added += 1
    return added


def upgrade_qt_markers(nav: NavData, locations: list[dict]) -> int:
    """Promote loaded POIs the wiki catalog knows to be QT destinations (#28a).

    The starmap feed's QTMarker coverage trails the game (150 markers in
    Stanton vs ~550 qt_valid in 4.8 game data — CIG made most outposts QT-able
    in recent patches), so places both catalogs know would stay unroutable
    after dedup. Same derive-don't-edit precedent as parse_data's Landing Zone
    rule. Conservative on purpose: only when the record's name maps to exactly
    ONE loaded POI in that system AND the container agrees (generic repeated
    names like 'Derelict Outpost' never mass-upgrade). Runs under the same org
    toggle as add_wiki_pois. Returns the number of POIs promoted."""
    by_key: dict[tuple[str, tuple[str, ...]], list[Poi]] = {}
    for p in nav.pois.values():
        by_key.setdefault((p.system.lower(), wiki_name_key(p.name)), []).append(p)
    n = 0
    for rec in locations:
        if not rec.get("qt_valid"):
            continue
        cands = by_key.get((rec["system"].lower(), wiki_name_key(rec["name"]))) or []
        if len(cands) != 1:
            continue
        p = cands[0]
        if p.qt_marker or p.custom or p.source == "wiki":
            continue
        rec_container = rec.get("container") if rec.get("local_km") else None
        if (p.container_name or None) != rec_container:
            continue
        p.qt_marker = True
        n += 1
    return n


def annotate_arrival_radii(nav: NavData, locations: list[dict]) -> int:
    """Attach wiki per-POI QT arrival radii to loaded POIs by name (#28b).

    Enrichment, not content: runs regardless of the wiki-POI org toggle, and
    touches starmap/synthesized POIs the wiki also knows (custom POIs won't
    name-match, by design). Radii sharpen run-mode arrival detection; a POI
    with no match keeps None and the caller's flat threshold applies.
    Returns the number of POIs annotated."""
    radii: dict[tuple[str, tuple[str, ...]], float] = {}
    for rec in locations:
        if rec.get("arrival_m"):
            radii[(rec["system"].lower(), wiki_name_key(rec["name"]))] = float(rec["arrival_m"])
    hit = 0
    for p in nav.pois.values():
        if p.arrival_radius_m is None:
            r = radii.get((p.system.lower(), wiki_name_key(p.name)))
            if r:
                p.arrival_radius_m = r
                hit += 1
    return hit


# ---------------------------------------------------------------------------
# Trade-terminal crosswalk: UEX commodity terminals -> routable nav POIs.
#
# UEX terminal rows say WHICH station / outpost / city a terminal sits in (by
# UEX's own ids and place names) but carry no game-file x/y/z. We place each
# terminal on the map by name-matching its location against the POI catalog we
# already have — the same technique synth_container_pois uses to route to
# stations that live only in the container catalog. Several terminals (shops)
# can resolve to the same physical POI. A terminal that doesn't resolve is left
# out of routing (and reported to the caller), never silently mis-placed.
# ---------------------------------------------------------------------------


def _norm_terminal_name(s: str | None) -> str:
    """Case/space-folded key for matching a place name across the two datasets."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _terminal_place_candidates(row: dict) -> list[str]:
    """Location names to try matching a terminal against, best anchor first.
    A terminal's `name` (e.g. 'Admin - ARC-L1', 'Platinum Bay - CRU-L4') carries
    a shop-type prefix, so the *physical* location comes from displayname and the
    specific place-name fields; `name`/`nickname` are last-resort fallbacks."""
    out: list[str] = []
    for key in ("displayname", "space_station_name", "outpost_name",
                "city_name", "nickname", "name"):
        v = row.get(key)
        if v and v not in out:
            out.append(v)
    return out


def match_terminals(nav: NavData, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Resolve UEX terminal rows to routable nav POIs by name.

    Returns ``(resolved, unmatched)`` where each resolved entry is
    ``{id, name, system, poi_id, poi_name}`` (``id`` = UEX terminal id) and each
    unmatched entry is the original row (for logging / coverage stats). Matching
    tries, per place-name candidate:
      1. exact normalized name (station/outpost/city as-is),
      2. Lagrange word-order flip — 'CRU-L4 Shallow Fields Station' (UEX) vs
         'Shallow Fields Station (CRU-L4)' (synth_container_pois' fold),
      3. gateway word-order flip — 'Pyro Gateway' (UEX) vs 'Gateway Station
         Pyro' (dataset).
    Matches against POIs only (travel_cost needs a Poi; synth_container_pois has
    already promoted the station containers we care about into nav.pois)."""
    by_name: dict[str, Poi] = {}
    lag_index: dict[tuple[str, str], Poi] = {}   # (l-code, base) -> poi
    for p in nav.pois.values():
        by_name.setdefault(_norm_terminal_name(p.name), p)
        m = re.search(r"([A-Z]{2,4}-L[1-5])", p.name)
        if m:
            base = re.sub(r"[()]|" + re.escape(m.group(1)), "", p.name)
            lag_index.setdefault((m.group(1).lower(), _norm_terminal_name(base)), p)

    def resolve(row: dict) -> Poi | None:
        for cand in _terminal_place_candidates(row):
            # drop a trailing system qualifier: 'Pyro Gateway (Stanton)'.
            cand = re.sub(r"\s*\((Stanton|Pyro|Nyx|Terra|Magnus)\)\s*$", "",
                          cand, flags=re.I).strip()
            key = _norm_terminal_name(cand)
            if key in by_name:
                return by_name[key]
            lag = re.match(r"([A-Z]{2,4}-L[1-5])\s+(.*)", cand)
            if lag:
                base = _norm_terminal_name(re.sub(r"\bstation\b", "", lag.group(2)))
                hit = lag_index.get((lag.group(1).lower(), base))
                if hit:
                    return hit
            gate = re.match(r"(.*)\s+Gateway$", cand, flags=re.I)
            if gate:
                hit = by_name.get(_norm_terminal_name(f"Gateway Station {gate.group(1)}"))
                if hit:
                    return hit
        return None

    resolved: list[dict] = []
    unmatched: list[dict] = []
    for row in rows:
        poi = resolve(row)
        if poi is None:
            unmatched.append(row)
            continue
        resolved.append({
            "id": row.get("id"),
            "name": row.get("displayname") or row.get("name") or poi.name,
            "system": row.get("star_system_name") or poi.system,
            "poi_id": poi.id,
            "poi_name": poi.name,
        })
    return resolved, unmatched


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
        "private": getattr(poi, "private", False),
        "note": poi.note,
        "nearest_qt": poi.nearest_qt,
        "nearest_qt_dist_m": poi.nearest_qt_dist_m,
        "source": getattr(poi, "source", "starmap"),
        "arrival_radius_m": getattr(poi, "arrival_radius_m", None),
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


def poi_visible_to(poi, viewer_owner_ids) -> bool:
    """A POI is visible to a viewer unless it's marked private and owned by
    someone else. Privacy is keyed on the in-game PlayerID(s) the viewer owns
    (`viewer_owner_ids`); shared POIs are visible to everyone."""
    if not getattr(poi, "private", False):
        return True
    return poi.owner_id is not None and poi.owner_id in viewer_owner_ids


def compute_state(
    nav: NavData,
    pos_m,
    t_unix: float,
    destination_id: int | None = None,
    prev_pos=None,
    prev_t: float | None = None,
    nearest_count: int = 10,
    viewer_owner_ids=frozenset(),
) -> dict:
    """Full navigation state for one position sample. `viewer_owner_ids` are the
    PlayerIDs the viewer owns, used to hide other members' private POIs."""
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

    visible_pois = [p for p in nav.pois.values() if poi_visible_to(p, viewer_owner_ids)]
    nearest_pois = _nearest(visible_pois, _poi_summary)
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
    # Never route to another member's private POI, even if its id is known.
    if dest_entity is not None and not poi_visible_to(dest_entity, viewer_owner_ids):
        dest_entity = None
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
        # Deep space: no container detects this far out, but the position is
        # still *in* a system (nearest-container heuristic) — stamping
        # "Unknown" would make travel_cost treat the POI as cross-system and
        # unroutable (breaks deep-space captures, e.g. Aaron Halo rocks).
        return system_at(nav, pos_m) or "Unknown", None, None, tuple(pos_m), None, None, None
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
    private: bool = False,
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
        private=bool(private),
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
        "private": poi.private,
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
        private=bool(d.get("private")),
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
    """Build the lookup of QT-marker POIs used by nearest_qt_marker. Private POIs
    are excluded: a QT marker is shared navigation infrastructure, and a private
    one would otherwise leak its name/location via every entity's nearest_qt."""
    nav.qt_markers = [p for p in nav.pois.values()
                      if p.qt_marker and not getattr(p, "private", False)]
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
    viewer_owner_ids=frozenset(),
) -> list[dict]:
    q = query.strip().lower()
    results = []
    for p in nav.pois.values():
        if not poi_visible_to(p, viewer_owner_ids):
            continue
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


def system_at(nav: NavData, pos) -> str | None:
    """The system a raw position is in: the container detected at it, else the
    nearest container's system (deep space between bodies)."""
    c = detect_container(nav, pos)
    if c is not None:
        return c.system
    best, best_d = None, math.inf
    for cont in nav.containers.values():
        d = dist3(pos, cont.pos)
        if d < best_d:
            best, best_d = cont, d
    return best.system if best is not None else (nav.systems[0] if nav.systems else None)


def position_start(nav: NavData, pos) -> Poi:
    """A synthetic space-POI standing in for the player's live position, so the
    route planner can seed the first leg from where they actually are (the
    show_location feed) — travel_cost only needs a `src` with a system + a
    resolvable global position."""
    return Poi(
        id=-1, name="your location", system=system_at(nav, pos), container_name=None,
        type="", local_km=None, global_m=(pos[0], pos[1], pos[2]),
        latitude=None, longitude=None, height_m=None, qt_marker=False,
    )


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


# ---------------------------------------------------------------------------
# Snare-detour routing (#24 v2) — hazard geometry.
#
# Upgrades danger handling from endpoint matching to flight-path geometry: a
# danger warning becomes a *volume* (a sphere at a point warning's anchor, a
# capsule along a lane warning's two anchors), and a QT leg's real jump
# segments are tested against those volumes. A conflicting segment is either
# routed around with an inserted waypoint (`_detour_via`) or, when an endpoint
# we must visit sits inside a volume, flagged `blocked`. All pure geometry,
# zero dependencies — see docs/snare-detour-routing.md.
# ---------------------------------------------------------------------------

# Severity-scaled hazard radius. Base is an org setting (hazard_radius_km);
# these multipliers are fixed in code.
HAZARD_SEVERITY_SCALE = {"sighted": 0.5, "active": 1.0, "deadly": 1.5}
DEFAULT_HAZARD_RADIUS_M = 5_000_000.0    # 5,000 km — a berth wide enough to
                                         # cover anchor imprecision + roaming
                                         # pirates; trivial at Gm leg scales.
_DETOUR_BUDGET = 1.5                      # give up past +50% distance -> blocked


def _seg_point_dist(p0, p1, c) -> float:
    """Minimum distance from segment p0->p1 to point c (clamped projection)."""
    dx, dy, dz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
    L2 = dx * dx + dy * dy + dz * dz
    if L2 == 0.0:
        return dist3(p0, c)
    t = ((c[0] - p0[0]) * dx + (c[1] - p0[1]) * dy + (c[2] - p0[2]) * dz) / L2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    proj = (p0[0] + t * dx, p0[1] + t * dy, p0[2] + t * dz)
    return dist3(proj, c)


def _seg_seg_dist(p0, p1, q0, q1) -> float:
    """Minimum distance between segments p0->p1 and q0->q1 (Ericson's clamped
    closest-point-of-two-segments; handles parallel + degenerate cases)."""
    d1 = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
    d2 = (q1[0] - q0[0], q1[1] - q0[1], q1[2] - q0[2])
    r = (p0[0] - q0[0], p0[1] - q0[1], p0[2] - q0[2])
    a = d1[0] * d1[0] + d1[1] * d1[1] + d1[2] * d1[2]     # |d1|^2
    e = d2[0] * d2[0] + d2[1] * d2[1] + d2[2] * d2[2]     # |d2|^2
    f = d2[0] * r[0] + d2[1] * r[1] + d2[2] * r[2]
    EPS = 1e-9
    if a <= EPS and e <= EPS:                             # both are points
        return dist3(p0, q0)
    if a <= EPS:                                          # seg1 is a point
        s = 0.0
        t = min(1.0, max(0.0, f / e))
    else:
        c = d1[0] * r[0] + d1[1] * r[1] + d1[2] * r[2]
        if e <= EPS:                                      # seg2 is a point
            t = 0.0
            s = min(1.0, max(0.0, -c / a))
        else:
            b = d1[0] * d2[0] + d1[1] * d2[1] + d1[2] * d2[2]
            denom = a * e - b * b
            s = min(1.0, max(0.0, (b * f - c * e) / denom)) if denom > EPS else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t = 0.0
                s = min(1.0, max(0.0, -c / a))
            elif t > 1.0:
                t = 1.0
                s = min(1.0, max(0.0, (b - c) / a))
    c1 = (p0[0] + s * d1[0], p0[1] + s * d1[1], p0[2] + s * d1[2])
    c2 = (q0[0] + t * d2[0], q0[1] + t * d2[1], q0[2] + t * d2[2])
    return dist3(c1, c2)


def _point_in_volume(c, vol) -> bool:
    if vol["kind"] == "capsule" and vol.get("b") is not None:
        return _seg_point_dist(vol["a"], vol["b"], c) < vol["r"]
    return dist3(vol["a"], c) < vol["r"]


def segment_hits(p0, p1, volumes) -> list[dict]:
    """The volumes that segment p0->p1 enters (sphere: seg->point < r; capsule:
    seg->seg < r). Caller should pre-filter volumes to the segment's system."""
    hits = []
    for v in volumes:
        if v["kind"] == "capsule" and v.get("b") is not None:
            d = _seg_seg_dist(p0, p1, v["a"], v["b"])
        else:
            d = _seg_point_dist(p0, p1, v["a"])
        if d < v["r"]:
            hits.append(v)
    return hits


def hazard_volumes(nav: NavData, warnings, t_ref, *,
                   radius_m=DEFAULT_HAZARD_RADIUS_M, extra_point_ids=()) -> list[dict]:
    """Turn active danger warnings (#24) + a personal blacklist into hazard
    volumes for the detour engine. Each volume is {kind: 'sphere'|'capsule',
    a, b, r, warning_id, system}. A `point` warning with a resolvable anchor ->
    sphere; a `lane` warning with both anchors -> capsule; un-anchored warnings
    contribute nothing (same rule as trade_avoid_sets). `extra_point_ids`
    (blacklisted POI ids) become spheres with warning_id=None at base radius.
    Radius scales with severity for warnings, ×1.0 for the blacklist."""
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    vols = []
    for w in warnings or ():
        scale = HAZARD_SEVERITY_SCALE.get(w.get("severity"), 1.0)
        r = radius_m * scale
        a_id, b_id = w.get("anchor_a_poi"), w.get("anchor_b_poi")
        if w.get("kind") == "lane":
            if a_id is None or b_id is None or a_id == b_id:
                continue
            pa, pb = nav.pois.get(a_id), nav.pois.get(b_id)
            if pa is None or pb is None:
                continue
            ga, gb = poi_global_m(nav, pa, t_ref), poi_global_m(nav, pb, t_ref)
            if ga is None or gb is None:
                continue
            vols.append({"kind": "capsule", "a": ga, "b": gb, "r": r,
                         "warning_id": w.get("id"), "system": pa.system})
        else:
            if a_id is None:
                continue
            pa = nav.pois.get(a_id)
            if pa is None:
                continue
            ga = poi_global_m(nav, pa, t_ref)
            if ga is None:
                continue
            vols.append({"kind": "sphere", "a": ga, "b": None, "r": r,
                         "warning_id": w.get("id"), "system": pa.system})
    for pid in extra_point_ids or ():
        p = nav.pois.get(pid)
        if p is None:
            continue
        g = poi_global_m(nav, p, t_ref)
        if g is None:
            continue
        vols.append({"kind": "sphere", "a": g, "b": None, "r": radius_m,
                     "warning_id": None, "system": p.system})
    return vols


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


def travel_cost(nav: NavData, src, dst, t_ref: float | None = None, *,
                avoid=None, memo=None) -> dict:
    """QT travel cost from `src` to `dst`, with optional snare-detour handling.

    With `avoid=None` (the default) this is the pure straight-line QT cost — the
    behavior and returned dict are byte-for-byte identical to the legacy model,
    so every existing caller is unaffected. With `avoid` = a list of hazard
    volumes (see hazard_volumes), the leg's real jump segments are tested and,
    on conflict, a detour waypoint is synthesized (#24 v2); the returned dict
    then also carries `waypoints`/`detour_m`/`dodged`/`blocked` and `distance_m`
    includes the added detour distance. `memo` (an optional per-solve dict keyed
    by (src, dst)) caches results so the greedy inner loop re-costs each POI pair
    at most once."""
    # Consult the memo before any costing — the no-hazard path (avoid=None, the
    # common case) also benefits: the greedy solver re-costs the same POI pairs
    # across restarts/legs, so without this every pair was recomputed from
    # scratch (~50x redundancy at production POI scale). t_ref is in the key so a
    # per-solve memo (constant t_ref) stays correct; hazard volumes are constant
    # per memo, so keying on (src, dst, t_ref) is sufficient.
    key = None
    if memo is not None:
        key = (getattr(src, "id", id(src)), getattr(dst, "id", id(dst)), t_ref)
        if key in memo:
            return memo[key]
    result = _base_travel_cost(nav, src, dst, t_ref)
    if avoid:
        _apply_detours(nav, src, dst, t_ref, avoid, result)
    if memo is not None:
        memo[key] = result
    return result


def _base_travel_cost(nav: NavData, src, dst, t_ref: float | None = None) -> dict:
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


def _intra_segments(nav: NavData, from_pos, dst, system: str, t_ref: float) -> list[dict]:
    """The real QT jump segments of an in-system hop from a global position to
    `dst`, mirroring _intra_leg's geometry (planet->moon two-hop included) but
    returning the segment endpoints for hazard testing. Each = {a, b, system}."""
    cont = nav.container_of(dst)
    if getattr(dst, "qt_marker", False) or cont is None:
        ref = entity_global_m(nav, dst, t_ref)
    else:
        marker = _nearest_qt_poi(nav, dst, t_ref)
        ref = entity_global_m(nav, marker, t_ref) if marker is not None else cont.pos
    if ref is None:
        return []
    parent = parent_planet(nav, cont) if cont is not None else None
    in_local = parent is not None and nearest_planet(nav, dst.system, from_pos) is parent
    if parent is not None and not in_local:
        return [{"a": from_pos, "b": parent.pos, "system": system},
                {"a": parent.pos, "b": ref, "system": system}]
    return [{"a": from_pos, "b": ref, "system": system}]


def _leg_segments(nav: NavData, src, dst, t_ref: float) -> list[dict]:
    """Decompose a travel_cost leg into its testable QT jump segments (global
    endpoints tagged by system) for snare-detour hazard testing. Mirrors
    travel_cost's own decomposition; the gate tunnel itself is not testable
    space (a camped gate is a point warning caught by the approach segment's
    endpoint test)."""
    from_pos = entity_global_m(nav, src, t_ref)
    if from_pos is None:
        return []
    if src.system == dst.system:
        return _intra_segments(nav, from_pos, dst, src.system, t_ref)
    path = system_path(src.system, dst.system)
    if path is None:
        return []
    segs = []
    out_gate = _gate_poi(nav, src.system, path[1])
    if out_gate is not None:
        segs += _intra_segments(nav, from_pos, out_gate, src.system, t_ref)
    in_gate = _gate_poi(nav, dst.system, path[-2])
    if in_gate is not None:
        gate_pos = entity_global_m(nav, in_gate, t_ref)
        if gate_pos is not None:
            segs += _intra_segments(nav, gate_pos, dst, dst.system, t_ref)
    return segs


def _detour_via(nav: NavData, p0, p1, volumes, system: str, t_ref: float):
    """Cheapest single QT marker W in `system` whose two hops p0->W and W->p1
    both clear every volume. Returns (Poi|None, added_distance_m). Pruned by the
    ellipse bound d(p0,W)+d(W,p1) < _DETOUR_BUDGET·d(p0,p1) before the (more
    expensive) segment tests."""
    direct = dist3(p0, p1)
    budget = _DETOUR_BUDGET * direct
    best, best_total = None, math.inf
    for w in nav.qt_markers:
        if w.system != system:
            continue
        wp = entity_global_m(nav, w, t_ref)
        if wp is None:
            continue
        total = dist3(p0, wp) + dist3(wp, p1)
        if total >= budget or total >= best_total:
            continue
        # A segment ending inside a volume registers as a hit, so this also
        # rejects a candidate marker that itself sits in the hazard.
        if segment_hits(p0, wp, volumes) or segment_hits(wp, p1, volumes):
            continue
        best, best_total = w, total
    # v2.1: two-waypoint fallback for heavily overlapping capsules — deferred.
    if best is None:
        return None, 0.0
    return best, best_total - direct


def _apply_detours(nav: NavData, src, dst, t_ref, volumes, result) -> None:
    """Augment a base travel_cost result with snare-detour analysis (#24 v2):
    test each real jump segment against the hazard volumes and, per conflict,
    insert a detour waypoint or flag the danger `blocked` (an endpoint we must
    visit sits inside a volume, or no clearing marker exists within budget).
    Mutates `result` in place — detour distance folds into `distance_m`."""
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    waypoints, dodged, blocked = [], [], []
    detour_m = 0.0
    for seg in _leg_segments(nav, src, dst, t_ref):
        vols = [v for v in volumes if v["system"] == seg["system"]]
        hits = segment_hits(seg["a"], seg["b"], vols) if vols else []
        if not hits:
            continue
        pending = []
        for v in hits:
            if _point_in_volume(seg["a"], v) or _point_in_volume(seg["b"], v):
                if v.get("warning_id") is not None:
                    blocked.append(v["warning_id"])   # camped endpoint — no reroute
            else:
                pending.append(v)
        if not pending:
            continue
        W, added = _detour_via(nav, seg["a"], seg["b"], pending, seg["system"], t_ref)
        if W is None:
            for v in pending:
                if v.get("warning_id") is not None:
                    blocked.append(v["warning_id"])
        else:
            waypoints.append({"id": W.id, "name": W.name})
            detour_m += added
            for v in pending:
                if v.get("warning_id") is not None:
                    dodged.append(v["warning_id"])
    if detour_m and result.get("distance_m") is not None:
        result["distance_m"] += detour_m
    if waypoints:
        result["waypoints"] = waypoints
    if detour_m:
        result["detour_m"] = detour_m
    blocked = list(dict.fromkeys(blocked))
    bset = set(blocked)
    dodged = [w for w in dict.fromkeys(dodged) if w not in bset]
    if dodged:
        result["dodged"] = dodged
    if blocked:
        result["blocked"] = blocked


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
            "from_id": p["from_id"], "to_id": p["to_id"], "contract": p.get("contract"),
            "group": p.get("group"), "group_scu": p.get("group_scu")}


def leg_fuel_scu(distance_m, fuel_req):
    """Quantum fuel (SCU) burned flying `distance_m` on a drive whose `fuel_req`
    is SCU per gigameter (1 Gm = 1e9 m). `None` fuel_req (unknown ship) or `None`
    distance (unroutable leg) -> `None`; the planner then shows no fuel figure
    rather than a fabricated one (#27)."""
    if fuel_req is None or distance_m is None:
        return None
    return distance_m / 1e9 * fuel_req


def _leg_view(leg, fuel_req=None, max_range_m=None):
    if leg is None:
        return None
    v = {"distance_m": leg["distance_m"], "eta_s": _leg_time_s(leg["distance_m"]),
         "qt_marker": leg["qt_marker"], "via": leg["via"],
         "cross_system": leg["cross_system"], "via_gate": leg["via_gate"],
         "partial": leg["partial"]}
    # Snare-detour extras (#24 v2), present only when the leg was costed with
    # hazard volumes and something actually conflicted.
    for k in ("waypoints", "detour_m", "dodged", "blocked"):
        if leg.get(k):
            v[k] = leg[k]
    # Quantum fuel/range annotation (#27) — only when a drive is known. Distance
    # here is already post-detour, so fuel over a re-routed path is automatic.
    if fuel_req is not None:
        d = leg["distance_m"]
        v["fuel_scu"] = leg_fuel_scu(d, fuel_req)
        v["over_range"] = bool(max_range_m is not None and d is not None and d > max_range_m)
    return v


def _fuel_summary(leg_views):
    """Aggregate the per-leg fuel/range annotations of a route's leg views into
    (total_fuel_scu, over_range_count, worst_leg_m). Legs without a `fuel_scu`
    (unknown ship / unroutable) are ignored. Returns all-None when no leg carried
    a drive annotation."""
    fuels = [lv["fuel_scu"] for lv in leg_views
             if lv and lv.get("fuel_scu") is not None]
    over = sum(1 for lv in leg_views if lv and lv.get("over_range"))
    dists = [lv["distance_m"] for lv in leg_views
             if lv and lv.get("distance_m") is not None]
    if not fuels and not any("fuel_scu" in lv for lv in leg_views if lv):
        return None, None, None
    return (round(sum(fuels), 3) if fuels else 0.0,
            over, max(dists) if dists else None)


def _stop_delta(stops, gpick, gdrop, gtot, j, seen):
    """Onboard SCU change from visiting stop j, given the groups already started
    (`seen`). Static (normal-package) load/drop plus the conservative group rule:
    a multi-pickup group's full total joins the hold at its *first* pickup and
    leaves at its drop. Returns (delta, newly_seen_group_ids)."""
    d = stops[j]["load_scu"] - stops[j]["drop_scu"]
    newly = []
    for g in gpick[j]:
        if g not in seen:
            d += gtot[g]
            newly.append(g)
    for g in gdrop[j]:
        if g in seen:            # always true: precedence puts all pickups first
            d -= gtot[g]
    return d, newly


def plan_route(nav: NavData, packages, usable_scu, start_id=None, start_pos=None,
               t_ref=None, *, avoid_volumes=None,
               fuel_req=None, max_range_m=None, in_range_only=False) -> dict:
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
                     "contract": p.get("contract"), "group": p.get("group"),
                     "group_scu": float(p["group_scu"]) if p.get("group_scu") is not None else None})

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
    # Multi-pickup groups carry their total once (group_scu), accounted dynamically
    # during ordering — full total held from the group's first pickup to its drop —
    # so they're kept out of the static per-stop load/drop sums here. Their
    # precedence (every pickup before the drop) still rides on `preds`.
    groups = {}   # gid -> {"total": float, "picks": set(stop idx), "drop": stop idx}
    for pi, p in enumerate(pkgs):
        a, b = idx[p["from_id"]], idx[p["to_id"]]
        stops[a]["loads"].append(pi)
        stops[b]["drops"].append(pi)
        g = p.get("group")
        if g is None:
            stops[a]["load_scu"] += p["scu"]
            stops[b]["drop_scu"] += p["scu"]
        else:
            info = groups.setdefault(g, {"total": 0.0, "picks": set(), "drop": b})
            if p.get("group_scu") is not None:
                info["total"] = float(p["group_scu"])
            info["picks"].add(a)
            info["drop"] = b
        if a != b:
            preds[b].add(a)

    # Per-stop group views for the dynamic onboard accounting (see _stop_delta).
    gpick = [[] for _ in range(n)]   # groups with a pickup at this stop
    gdrop = [[] for _ in range(n)]   # groups whose dropoff is this stop
    gtot = {}                        # gid -> delivery total
    for g, info in groups.items():
        gtot[g] = info["total"]
        for sidx in info["picks"]:
            gpick[sidx].append(g)
        gdrop[info["drop"]].append(g)

    # --- cost matrix ---
    # A shared memo makes the snare-detour search run at most once per POI pair
    # per solve (no-op when avoid_volumes is None — the fast path).
    memo = {}
    legs = [[None] * n for _ in range(n)]
    dmat = [[math.inf] * n for _ in range(n)]
    for a in range(n):
        for b in range(n):
            if a == b:
                continue
            leg = travel_cost(nav, stops[a]["poi"], stops[b]["poi"], t_ref,
                              avoid=avoid_volumes, memo=memo)
            legs[a][b] = leg
            if leg["distance_m"] is not None:
                dmat[a][b] = leg["distance_m"]
    # Start seed: a live position (show_location) wins over a chosen POI; absent
    # both, the run begins free at the optimizer's first stop.
    if start_pos is not None:
        start_poi = position_start(nav, start_pos)
    elif start_id is not None:
        start_poi = nav.pois.get(int(start_id))
    else:
        start_poi = None
    start_legs = [None] * n
    start_d = [0.0] * n
    if start_poi is not None:
        for b in range(n):
            leg = travel_cost(nav, start_poi, stops[b]["poi"], t_ref,
                              avoid=avoid_volumes, memo=memo)
            start_legs[b] = leg
            start_d[b] = leg["distance_m"] if leg["distance_m"] is not None else math.inf

    def step_cost(prev, j):
        return start_d[j] if prev is None else dmat[prev][j]

    gctx = (gpick, gdrop, gtot)

    # --- minimum capacity the bundle requires (precedence only, min peak) ---
    min_cap = _min_capacity(stops, preds, n, gctx)

    # A per-leg range cap forbids any hop the ship can't cover on a full tank —
    # active only when the player opts into "in-range only" and a drive is known
    # (#27). All stops must be visited, so an over-range hop makes an ordering
    # infeasible (unlike the selective trade solver, which just drops the trade).
    max_leg_m = max_range_m if (in_range_only and max_range_m) else None

    # --- order the stops (minimize distance under precedence + capacity) ---
    if n <= _BNB_MAX_STOPS:
        order, peak = _bnb_order(stops, preds, dmat, start_d, usable_scu, n, gctx,
                                 max_leg_m=max_leg_m)
    else:
        order, peak = _greedy_order(stops, preds, step_cost, usable_scu, n, gctx,
                                    max_leg_m=max_leg_m)

    if order is None:
        # Distinguish a range failure from a capacity/connectivity one so the UI
        # can tell the player to uncheck "in-range only" or switch drives: re-solve
        # without the cap; if that succeeds, range was the blocker.
        range_infeasible = False
        if max_leg_m is not None:
            if n <= _BNB_MAX_STOPS:
                alt, _ = _bnb_order(stops, preds, dmat, start_d, usable_scu, n, gctx)
            else:
                alt, _ = _greedy_order(stops, preds, step_cost, usable_scu, n, gctx)
            range_infeasible = alt is not None
        return {"summary": {"feasible": False, "num_stops": n,
                            "num_packages": len(pkgs), "usable_scu": usable_scu,
                            "peak_load_scu": None, "min_capacity_scu": round(min_cap, 2),
                            "total_distance_m": None, "total_time_s": None,
                            "range_infeasible": range_infeasible,
                            "start_id": start_poi.id if (start_poi and start_poi.id >= 0) else None,
                            "start": start_poi.name if start_poi else None}, "stops": []}

    # --- build output ---
    out_stops = []
    onboard = total_dist = 0.0
    seen = set()
    for k, si in enumerate(order):
        s = stops[si]; poi = s["poi"]
        delta, newly = _stop_delta(stops, gpick, gdrop, gtot, si, seen)
        onboard += delta
        seen.update(newly)
        leg = start_legs[si] if k == 0 else legs[order[k - 1]][si]
        if leg and leg["distance_m"] is not None:
            total_dist += leg["distance_m"]
        out_stops.append({
            "stop_id": s["id"], "name": poi.name, "system": poi.system,
            "type": poi.type,
            "pickups": [_pkg_view(pkgs[pi]) for pi in s["loads"]],
            "dropoffs": [_pkg_view(pkgs[pi]) for pi in s["drops"]],
            "leg": _leg_view(leg, fuel_req=fuel_req, max_range_m=max_range_m),
            "onboard_scu": round(onboard, 2),
        })
    total_time = sum((_leg_time_s(s["leg"]["distance_m"]) or 0.0)
                     for s in out_stops if s["leg"]) + STOP_DWELL_S * n

    summary = {"feasible": True, "num_stops": n, "num_packages": len(pkgs),
               "usable_scu": usable_scu, "peak_load_scu": round(peak, 2),
               "min_capacity_scu": round(min_cap, 2),
               "total_distance_m": total_dist, "total_time_s": total_time,
               "start_id": start_poi.id if (start_poi and start_poi.id >= 0) else None,
               "start": start_poi.name if start_poi else None}
    if fuel_req is not None:
        tf, oc, wl = _fuel_summary([s["leg"] for s in out_stops])
        summary["total_fuel_scu"] = tf
        summary["over_range_count"] = oc
        summary["worst_leg_m"] = wl
    return {"summary": summary, "stops": out_stops}


def _bnb_order(stops, preds, dmat, start_d, cap, n, gctx, *, max_leg_m=None):
    """Branch-and-bound: least-distance precedence+capacity-feasible order.
    `max_leg_m` (when set) forbids any single hop longer than the ship's tank —
    the "in-range only" constraint. Returns (order, peak_load) or (None, None)
    if no feasible order exists."""
    gpick, gdrop, gtot = gctx
    best = {"cost": math.inf, "order": None, "peak": None}

    def dfs(order, visited, onboard, cost, peak, seen):
        if cost >= best["cost"]:
            return
        if len(order) == n:
            best.update(cost=cost, order=list(order), peak=peak)
            return
        prev = order[-1] if order else None
        for j in range(n):
            if j in visited or not preds[j].issubset(visited):
                continue
            delta, newly = _stop_delta(stops, gpick, gdrop, gtot, j, seen)
            no = onboard + delta
            if no > cap + 1e-9:
                continue
            step = start_d[j] if prev is None else dmat[prev][j]
            if step == math.inf:
                continue
            if max_leg_m is not None and step > max_leg_m:
                continue
            dfs(order + [j], visited | {j}, no, cost + step, max(peak, no),
                seen | set(newly))

    dfs([], set(), 0.0, 0.0, 0.0, frozenset())
    return (best["order"], best["peak"]) if best["order"] is not None else (None, None)


def _greedy_order(stops, preds, step_cost, cap, n, gctx, *, max_leg_m=None):
    """Nearest-neighbor fallback for large stop sets: at each step take the
    nearest precedence- and capacity-feasible stop. `max_leg_m` (when set) rejects
    any hop longer than the ship's tank (the "in-range only" constraint)."""
    gpick, gdrop, gtot = gctx
    order, visited = [], set()
    onboard = peak = 0.0
    seen = set()
    prev = None
    while len(order) < n:
        cands = [
            j for j in range(n)
            if j not in visited and preds[j].issubset(visited)
            and onboard + _stop_delta(stops, gpick, gdrop, gtot, j, seen)[0] <= cap + 1e-9
            and step_cost(prev, j) != math.inf
            and (max_leg_m is None or step_cost(prev, j) <= max_leg_m)
        ]
        if not cands:
            return None, None
        j = min(cands, key=lambda j: step_cost(prev, j))
        delta, newly = _stop_delta(stops, gpick, gdrop, gtot, j, seen)
        onboard += delta
        peak = max(peak, onboard)
        seen.update(newly)
        order.append(j); visited.add(j); prev = j
    return order, peak


def _min_capacity(stops, preds, n, gctx):
    """Minimum peak onboard SCU achievable over any precedence-valid order —
    i.e. the smallest ship the bundle can possibly fit on. Exhaustive for small
    sets, greedy (least-load-first) above the bound."""
    gpick, gdrop, gtot = gctx
    if n > _BNB_MAX_STOPS:
        visited = set()
        onboard = peak = 0.0
        seen = set()
        while len(visited) < n:
            cands = [j for j in range(n) if j not in visited and preds[j].issubset(visited)]
            j = min(cands, key=lambda j: _stop_delta(stops, gpick, gdrop, gtot, j, seen)[0])
            delta, newly = _stop_delta(stops, gpick, gdrop, gtot, j, seen)
            onboard += delta
            peak = max(peak, onboard)
            seen.update(newly)
            visited.add(j)
        return peak
    best = {"peak": math.inf}

    def dfs(visited, onboard, peak, seen):
        if peak >= best["peak"]:
            return
        if len(visited) == n:
            best["peak"] = peak
            return
        for j in range(n):
            if j in visited or not preds[j].issubset(visited):
                continue
            delta, newly = _stop_delta(stops, gpick, gdrop, gtot, j, seen)
            no = onboard + delta
            dfs(visited | {j}, no, max(peak, no), seen | set(newly))

    dfs(set(), 0.0, 0.0, frozenset())
    return best["peak"]


def _poi_name(nav: NavData, pid):
    poi = nav.pois.get(int(pid)) if pid is not None else None
    return poi.name if poi else None


def run_packages(run: dict) -> list[dict]:
    """The package records of a stored run, regardless of which shape it was
    persisted in. Active/completed runs keep packages as an id->record dict;
    fall back to scraping the stops' pickups for any older blob."""
    pkgs = run.get("packages")
    if isinstance(pkgs, dict):
        return list(pkgs.values())
    if isinstance(pkgs, list):
        return pkgs
    out = []
    for s in run.get("stops", []):
        out.extend(s.get("pickups", []))
    return out


def packages_scu(packages) -> float:
    """Total SCU a package list represents. A multi-pickup group's pickup rows
    share one delivery total (`group_scu`) instead of per-row `scu`, so the total
    is counted once per group; normal rows sum their own `scu`."""
    total = 0.0
    seen = set()
    for p in packages:
        g = p.get("group")
        if g is None:
            total += float(p.get("scu") or 0)
        elif g not in seen:
            seen.add(g)
            total += float(p.get("group_scu") or 0)
    return total


def run_total_reward(run: dict) -> float:
    """A run's total payout: the stored total if present, else the sum of its
    per-contract rewards (older blobs may carry only the rewards map)."""
    total = run.get("total_reward")
    if total is not None:
        return float(total)
    rw = run.get("rewards")
    if isinstance(rw, dict):
        return float(sum(v for v in rw.values() if v))
    return 0.0


def derive_run_stats(runs) -> dict:
    """Headline hauling analytics over a member's completed runs: totals plus the
    overall aUEC/hour (reward ÷ run time). Pure derivation over the stored blobs;
    runs predating reward/time capture contribute 0 and are simply diluted."""
    total_reward = total_scu = total_dist = total_time = 0.0
    for run in runs:
        total_reward += run_total_reward(run)
        total_scu += packages_scu(run_packages(run))
        total_dist += float(run.get("total_distance_m") or 0)
        total_time += float(run.get("total_time_s") or 0)
    per_hr = (total_reward / (total_time / 3600.0)) if total_time > 0 else None
    return {
        "num_runs": len(runs),
        "total_reward": round(total_reward, 2),
        "total_scu": round(total_scu, 2),
        "total_distance_m": round(total_dist, 2),
        "total_time_s": round(total_time, 2),
        "auec_per_hour": round(per_hr, 2) if per_hr is not None else None,
    }


def _run_rate(run: dict):
    """A run's aUEC/hour, or None when it lacks a positive reward+time to derive one."""
    reward = run_total_reward(run)
    t = float(run.get("total_time_s") or 0)
    return (reward / (t / 3600.0)) if (reward > 0 and t > 0) else None


def derive_run_record(run: dict, prior_runs) -> dict:
    """Which org-wide hauling records a just-completed `run` set against every
    `prior_run` (all other completed runs, org-wide). Returns
    `{"total": auec, "rate": auec_per_hour}` with a key present only when this run
    *strictly* beats an established prior best on that metric — the very first
    qualifying run doesn't count as a record (there's nothing to beat), so the
    channel isn't pinged for a lone haul. Pure derivation over the stored blobs."""
    out: dict = {}
    reward = run_total_reward(run)
    prior_rewards = [r for r in (run_total_reward(p) for p in prior_runs) if r > 0]
    if reward > 0 and prior_rewards and reward > max(prior_rewards):
        out["total"] = reward
    rate = _run_rate(run)
    prior_rates = [r for r in (_run_rate(p) for p in prior_runs) if r]
    if rate is not None and prior_rates and rate > max(prior_rates):
        out["rate"] = rate
    return out


def derive_quick_picks(nav: NavData, runs, limit: int = 12) -> dict:
    """Frequency-ranked data-entry priors from a member's completed runs: the
    lanes (from->to) they haul most, the commodities they carry (with the SCU
    amount they most often book), and the ships they run. Feeds the planner's
    quick-picks so repeat hauls re-enter in a couple of clicks. Pure derivation
    over the persisted run blobs — names are resolved against the live catalog."""
    lane_ct, ship_ct = {}, {}
    commodity_ct, commodity_scu = {}, {}
    for run in runs:
        ship = (run.get("ship") or "").strip()
        if ship:
            ship_ct[ship] = ship_ct.get(ship, 0) + 1
        for p in run_packages(run):
            fid, tid = p.get("from_id"), p.get("to_id")
            if fid is not None and tid is not None:
                lane_ct[(int(fid), int(tid))] = lane_ct.get((int(fid), int(tid)), 0) + 1
            name = (p.get("commodity") or "").strip()
            if name:
                commodity_ct[name] = commodity_ct.get(name, 0) + 1
                scu = p.get("scu") or p.get("group_scu")
                if scu:
                    by = commodity_scu.setdefault(name, {})
                    by[float(scu)] = by.get(float(scu), 0) + 1

    lanes = []
    for (fid, tid), ct in sorted(lane_ct.items(), key=lambda kv: (-kv[1], kv[0])):
        fn, tn = _poi_name(nav, fid), _poi_name(nav, tid)
        if fn is None or tn is None:
            continue                       # a POI that no longer resolves — skip
        lanes.append({"from_id": fid, "from_name": fn, "to_id": tid,
                      "to_name": tn, "count": ct})
        if len(lanes) >= limit:
            break

    commodities = []
    for name, ct in sorted(commodity_ct.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]:
        scu = None
        by = commodity_scu.get(name)
        if by:                             # the SCU amount most often booked for it
            scu = max(by.items(), key=lambda kv: (kv[1], kv[0]))[0]
        commodities.append({"commodity": name, "count": ct, "scu": scu})

    ships = [{"ship": s, "count": c}
             for s, c in sorted(ship_ct.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
    return {"lanes": lanes, "commodities": commodities, "ships": ships}


# ---------------------------------------------------------------------------
# Trade-route planner (#21): best-single-trade ranking.
#
# The building block under every planning mode: given live per-terminal prices,
# find the most profitable buy->sell pairs for a commodity across the map. A
# "trade" is buy commodity C at terminal A, sell at terminal B (B pays more than
# A charges). Ranked by margin (profit/SCU) or, once ship capacity + the QT leg
# are folded in, by throughput (profit/hour) — the number a trader actually
# optimizes. Reuses travel_cost + _leg_time_s verbatim; surfaced directly as
# manual-mode suggestions and as the seed set for the multi-leg solver (step 3).
# ---------------------------------------------------------------------------

# Cap on buy->sell pairs we cost a QT leg for. Pairs are pre-ranked by margin and
# only the richest are priced for travel, keeping the ranking cheap without
# dropping a realistic best-per-hour trade (high throughput needs margin too).
_TRADE_TRAVEL_BUDGET = 400


def _clamp_scu(capacity, supply, demand):
    """SCU actually movable on a trade: your hold, capped by what's for sale and
    what the buyer will take. Supply/demand of 0 means unknown (UEX often lacks
    live stock), so it doesn't clamp — only positive figures do. Returns None when
    capacity is unset."""
    scu = float(capacity) if capacity else None
    if scu is None:
        return None
    for lim in (supply, demand):
        if lim and lim > 0:
            scu = min(scu, float(lim))
    return scu


def _trade_row(name, src, dst, margin, capacity_scu, budget=None) -> dict:
    """A single buy->sell trade record (no travel yet) from a buy price point
    `src` and sell price point `dst`. Travel fields (distance/eta/via_gate) and
    profit_per_hour are filled by the caller when a route/leg is costed. When
    `capacity_scu` is given, max_scu clamps to available supply/demand and the
    profit/cost totals follow. `budget` (aUEC on hand) further clamps the load so
    buy_cost never exceeds what the player can afford — trades are sequential, so
    the binding capital constraint is one hold-fill at a time. `*_updated_at` carry
    each side's UEX scrape time (unix s) for freshness badges."""
    row = {
        "commodity": name,
        "buy_terminal_id": src.get("terminal_id"), "buy_terminal": src.get("terminal"),
        "buy_poi_id": src.get("poi_id"), "buy_system": src.get("system"),
        "sell_terminal_id": dst.get("terminal_id"), "sell_terminal": dst.get("terminal"),
        "sell_poi_id": dst.get("poi_id"), "sell_system": dst.get("system"),
        "buy_price": int(src["buy"]), "sell_price": int(dst["sell"]),
        "profit_per_scu": margin,
        "supply_scu": src.get("scu_buy") or 0,
        "demand_scu": dst.get("scu_sell_stock") or 0,
        "buy_updated_at": src.get("updated_at"), "sell_updated_at": dst.get("updated_at"),
        "distance_m": None, "eta_s": None, "cross_system": None, "via_gate": None,
        "max_scu": None, "trade_profit": None, "buy_cost": None, "profit_per_hour": None,
    }
    max_scu = _clamp_scu(capacity_scu, row["supply_scu"], row["demand_scu"])
    if max_scu and budget and row["buy_price"] > 0:
        max_scu = min(max_scu, math.floor(budget / row["buy_price"]))
    if max_scu:
        row["max_scu"] = int(max_scu)
        row["trade_profit"] = int(round(margin * max_scu))
        row["buy_cost"] = int(round(row["buy_price"] * max_scu))
    return row


def rank_trades(nav: NavData, prices, *, commodity=None, system=None,
                capacity_scu=None, min_margin=0, limit=50, sort="auto",
                budget=None, max_age_s=None, now_ts=None, t_ref=None) -> list[dict]:
    """Best buy->sell trades over the live price feed, richest first.

    `prices` is the resolved per-terminal price list (the shape /api/trade/prices
    emits: commodity, terminal_id, terminal, system, poi_id, buy, sell, scu_buy,
    scu_sell_stock). For each commodity we pair every terminal that sells it *to*
    you (a `buy` price) with every terminal that buys it *from* you (a `sell`
    price) at a positive margin, then cost the QT leg between the two POIs.

    Filters: `commodity` (one name), `system` (both ends in-system — no gate hop),
    `min_margin` (aUEC/SCU floor). `capacity_scu` (usable SCU) enables the
    throughput fields (max_scu, trade_profit, buy_cost, profit_per_hour). `sort`:
      "margin"    — profit per SCU (needs no capacity or travel),
      "per_hour"  — trade_profit / leg time (needs capacity; travel-priced),
      "auto"      — per_hour when capacity is given, else margin.
    Returns up to `limit` trade dicts."""
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    now = now_ts if now_ts is not None else time.time()
    cnorm = (commodity or "").strip().lower()

    # bucket price points per commodity: buy-side (sells to you) / sell-side.
    buyers: dict[str, list] = {}
    sellers: dict[str, list] = {}
    for p in prices:
        name = p.get("commodity")
        if not name or (cnorm and name.strip().lower() != cnorm):
            continue
        if system and p.get("system") != system:
            continue
        if not _price_fresh(p, max_age_s, now):
            continue
        if p.get("buy"):
            buyers.setdefault(name, []).append(p)
        if p.get("sell"):
            sellers.setdefault(name, []).append(p)

    # positive-margin candidate pairs (cheap — no travel cost yet).
    cand = []
    for name, srcs in buyers.items():
        dsts = sellers.get(name)
        if not dsts:
            continue
        for src in srcs:
            for dst in dsts:
                if src.get("poi_id") == dst.get("poi_id"):
                    continue                      # same dock — not a trade
                margin = int(dst["sell"]) - int(src["buy"])
                if margin <= 0 or margin < min_margin:
                    continue
                cand.append((margin, name, src, dst))
    cand.sort(key=lambda c: -c[0])

    want_hour = bool(capacity_scu) and sort in ("per_hour", "auto")
    window = cand[:_TRADE_TRAVEL_BUDGET] if want_hour else cand[:max(limit, 1)]

    out = []
    for margin, name, src, dst in window:
        row = _trade_row(name, src, dst, margin, capacity_scu, budget=budget)
        if want_hour:
            sp, dp = nav.pois.get(src.get("poi_id")), nav.pois.get(dst.get("poi_id"))
            if sp is not None and dp is not None:
                leg = travel_cost(nav, sp, dp, t_ref)
                row["distance_m"] = leg["distance_m"]
                row["cross_system"] = leg["cross_system"]
                row["via_gate"] = leg["via_gate"]
                row["eta_s"] = _leg_time_s(leg["distance_m"])
                if row["eta_s"] and row["trade_profit"] is not None:
                    hours = (row["eta_s"] + STOP_DWELL_S) / 3600.0
                    if hours > 0:
                        row["profit_per_hour"] = int(round(row["trade_profit"] / hours))
        out.append(row)

    keyfn = {
        "margin": lambda r: r["profit_per_scu"],
        "per_hour": lambda r: (r["profit_per_hour"] if r["profit_per_hour"] is not None else -1),
    }.get(sort)
    if keyfn is None:                             # "auto"
        keyfn = ((lambda r: (r["profit_per_hour"] if r["profit_per_hour"] is not None else -1))
                 if want_hour else (lambda r: r["profit_per_scu"]))
    out.sort(key=lambda r: -keyfn(r))
    return out[:limit]


# ---------------------------------------------------------------------------
# Trade-route planner (#21) step 3: multi-leg solver.
#
# Choosing WHICH buy/sell pairs to include (not just ordering a fixed set) makes
# this an orienteering/prize-collecting problem — strictly harder than the cargo
# planner's fixed-package ordering. v1 uses the recommended heuristic: rank single
# trades (rank_trades' pairs), greedily chain the best per-hour continuation from
# the current position, and multi-start from each of the top seeds to escape the
# myopic local optimum. Each leg fills the hold with one commodity and empties it
# at the sell, so capacity stays a scalar and travel_cost is reused verbatim.
# ---------------------------------------------------------------------------

_TRADE_RESTARTS = 6           # greedy re-seeds (unforced + top-N forced first legs)


def _start_ref(start: Poi | None) -> dict | None:
    if start is None:
        return None
    return {"id": start.id, "name": start.name, "system": start.system}


def _dist_of(leg) -> float:
    return (leg["distance_m"] or 0.0) if leg else 0.0


def _price_fresh(p, max_age_s, now) -> bool:
    """Whether a price point is fresh enough to plan on. When `max_age_s` is set,
    a point older than that — or one with no scrape timestamp at all (we can't
    vouch for it) — is dropped."""
    if not max_age_s:
        return True
    ts = p.get("updated_at")
    return ts is not None and (now - ts) <= max_age_s


def _trade_candidates(prices, capacity_scu, *, system=None, commodities=None,
                      budget=None, max_age_s=None, now_ts=None,
                      avoid_poi_ids=frozenset(), avoid_pairs=frozenset(),
                      avoid_buys=frozenset(), avoid_sells=frozenset()) -> list[dict]:
    """Every movable positive-margin buy->sell trade (no travel yet), richest
    total-profit first — the pool the greedy chain draws from. `commodities` (a
    name set) restricts to filtered mode; `system` keeps both ends in-system.
    `budget` caps each load to what the player can afford; `max_age_s` drops price
    points older than that (stale-data opt-out). `avoid_poi_ids` drops any trade
    that buys or sells at a warned POI (a camped station); `avoid_pairs` (a set of
    frozenset({buy_poi, sell_poi})) drops the exact snared lane — both from the
    pirate danger board (#24). `avoid_buys` / `avoid_sells` (sets of
    (poi_id, commodity_lower), from stock reports) each drop only their own side:
    a terminal a member found out of stock still *sells* fine, and one that
    stopped buying (no demand) still *buys* fine."""
    now = now_ts if now_ts is not None else time.time()
    cset = {c.strip().lower() for c in commodities} if commodities else None
    buyers: dict[str, list] = {}
    sellers: dict[str, list] = {}
    for p in prices:
        name = p.get("commodity")
        if not name or (cset and name.strip().lower() not in cset):
            continue
        if system and p.get("system") != system:
            continue
        if not _price_fresh(p, max_age_s, now):
            continue
        if p.get("buy"):
            buyers.setdefault(name, []).append(p)
        if p.get("sell"):
            sellers.setdefault(name, []).append(p)
    rows = []
    for name, srcs in buyers.items():
        key = name.strip().lower()
        for dst in sellers.get(name, ()):
            for src in srcs:
                sid, did = src.get("poi_id"), dst.get("poi_id")
                if sid == did:
                    continue
                if sid in avoid_poi_ids or did in avoid_poi_ids:
                    continue            # a camped buy/sell POI (avoid mode, #24)
                if avoid_buys and (sid, key) in avoid_buys:
                    continue            # reported out of stock — buy side only
                if avoid_sells and (did, key) in avoid_sells:
                    continue            # reported no demand — sell side only
                if avoid_pairs and frozenset((sid, did)) in avoid_pairs:
                    continue            # a snared buy->sell lane (avoid mode, #24)
                margin = int(dst["sell"]) - int(src["buy"])
                if margin <= 0:
                    continue
                row = _trade_row(name, src, dst, margin, capacity_scu, budget=budget)
                if row["max_scu"]:            # need a movable quantity to be real
                    rows.append(row)
    rows.sort(key=lambda r: -(r["trade_profit"] or 0))
    return rows


def trade_avoid_sets(warnings) -> tuple[frozenset, frozenset]:
    """Turn active anchored danger warnings (#24) into the two exclusion sets the
    trade solver understands: `avoid_poi_ids` (POIs never to buy/sell at — a camped
    `point` warning's anchor) and `avoid_pairs` (exact snared buy<->sell lanes — a
    `lane` warning's two anchors as a frozenset). A warning missing the anchor(s) it
    needs is board-only intel and contributes nothing. Order-independent; safe on an
    empty/None list."""
    poi_ids, pairs = set(), set()
    for w in warnings or ():
        a, b = w.get("anchor_a_poi"), w.get("anchor_b_poi")
        if w.get("kind") == "lane":
            if a is not None and b is not None and a != b:
                pairs.add(frozenset((a, b)))
        elif a is not None:                     # point (a lane missing an end is inert)
            poi_ids.add(a)
    return frozenset(poi_ids), frozenset(pairs)


def trade_leg_warnings(leg: dict, warnings) -> list[dict]:
    """The active danger warnings (#24) that touch one costed trade leg — for the
    'warn' planner mode's per-leg badges. A `point` warning touches a leg when its
    anchor is the leg's buy or sell POI (loading/unloading at a camped station); a
    `lane` warning touches when its two anchors ARE the leg's buy and sell POIs
    (flying the exact snared lane). Deadliest first. Detecting a snare the leg merely
    flies *past* (between other POIs) is v2 — the straight-line nav model has no
    waypoints to test against."""
    ends = {leg.get("buy_poi_id"), leg.get("sell_poi_id")}
    ends.discard(None)
    hits = []
    for w in warnings or ():
        a, b = w.get("anchor_a_poi"), w.get("anchor_b_poi")
        if w.get("kind") == "lane":
            if a is not None and b is not None and len(ends) == 2 and {a, b} == ends:
                hits.append(w)
        elif a is not None and a in ends:
            hits.append(w)
    rank = {"deadly": 0, "active": 1, "sighted": 2}
    hits.sort(key=lambda w: rank.get(w.get("severity"), 9))
    return hits


def _report_side(r: dict) -> str:
    """A report's trade side; rows written before the demand feature (no `side`)
    are supply-side by construction."""
    return r.get("side") or "supply"


def _stock_avoid_pairs(reports, side) -> frozenset:
    """The (poi_id, commodity_lower) pairs with a fresh `out` report on `side`.
    Only `kind == 'out'` excludes — a 'low' report is a badge, not a routing veto
    (a short shelf / soft market can still be worth the stop). Safe on an
    empty/None list; reports missing a POI anchor or commodity contribute
    nothing."""
    pairs = set()
    for r in reports or ():
        if r.get("kind") != "out" or _report_side(r) != side:
            continue
        pid, name = r.get("poi_id"), r.get("commodity")
        if pid is not None and name:
            pairs.add((pid, name.strip().lower()))
    return frozenset(pairs)


def stock_avoid_buys(reports) -> frozenset:
    """Buy-side exclusion set: terminals members recently found *out of stock*
    (supply-side `out` reports)."""
    return _stock_avoid_pairs(reports, "supply")


def stock_avoid_sells(reports) -> frozenset:
    """Sell-side exclusion set: terminals members recently found *not buying*
    the commodity (demand-side `out` reports)."""
    return _stock_avoid_pairs(reports, "demand")


def trade_leg_stock(leg: dict, reports) -> list[dict]:
    """The active stock reports that touch one costed trade leg — a supply-side
    report matches the leg's *buy* end, a demand-side report its *sell* end,
    always on the leg's own commodity — for per-leg badges ("reported out of
    stock 45m ago", "won't buy here"). Out-of-stock first, then low, freshest
    first within a kind."""
    name = (leg.get("commodity") or "").strip().lower()
    if not name:
        return []
    end = {"supply": leg.get("buy_poi_id"), "demand": leg.get("sell_poi_id")}
    hits = [r for r in reports or ()
            if r.get("poi_id") is not None
            and r.get("poi_id") == end.get(_report_side(r))
            and (r.get("commodity") or "").strip().lower() == name]
    rank = {"out": 0, "low": 1}
    hits.sort(key=lambda r: (rank.get(r.get("kind"), 9), -(r.get("created") or 0)))
    return hits


def leg_hazards(nav: NavData, src, dst, volumes, t_ref) -> list:
    """Warning ids whose hazard volumes the *direct* (un-detoured) leg src->dst
    crosses (#24 v2). Warn mode uses this to badge fly-past dangers the endpoint
    match (trade_leg_warnings) can't see — the leg merely passing near a snare,
    not buying/selling in it — without changing the route. Deadliest-first order
    isn't guaranteed here (ids only); the caller resolves + ranks."""
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    ids = []
    for seg in _leg_segments(nav, src, dst, t_ref):
        vols = [v for v in volumes if v["system"] == seg["system"]]
        for v in segment_hits(seg["a"], seg["b"], vols) if vols else ():
            if v.get("warning_id") is not None:
                ids.append(v["warning_id"])
    return list(dict.fromkeys(ids))


def _cost_route(nav: NavData, chosen: list[dict], start: Poi | None, t_ref,
                *, avoid=None, memo=None, fuel_req=None, max_range_m=None) -> dict:
    """Walk an ordered list of trade rows from `start`, costing the QT legs
    (reposition to each buy terminal, then the loaded haul to its sell terminal)
    and accumulating profit/time/distance. Returns {summary, legs}. `avoid`
    (hazard volumes) + `memo` thread snare-detour costing into every leg;
    `fuel_req`/`max_range_m` (when a drive is known, #27) annotate each leg with
    fuel_scu/over_range and add fuel totals to the summary."""
    legs, pos, prev_sell = [], start, None
    total_profit = total_dist = total_time = 0.0
    deadhead_time = loaded_time = 0.0
    peak_capital = 0
    oldest_ts = None
    feasible = bool(chosen)
    stops = 0
    for row in chosen:
        held = bool(row.get("held"))              # cargo already aboard (mid-run re-plan)
        # A held leg has no buy terminal to fly to — you start already loaded, so
        # the "buy" position is the current position (`start`) and there's no empty
        # approach or extra buy-stop.
        bp = start if held else nav.pois.get(row["buy_poi_id"])
        sp = nav.pois.get(row["sell_poi_id"])
        approach = (travel_cost(nav, pos, bp, t_ref, avoid=avoid, memo=memo)
                    if (pos is not None and bp is not None) else None)
        haul = (travel_cost(nav, bp, sp, t_ref, avoid=avoid, memo=memo)
                if (bp is not None and sp is not None) else None)
        if haul is None or haul["distance_m"] is None:
            feasible = False
        if not held and row["buy_poi_id"] != prev_sell:   # buying where we just sold = one stop
            stops += 1
        stops += 1
        total_profit += row["trade_profit"] or 0
        total_dist += _dist_of(approach) + _dist_of(haul)
        approach_t = ((_leg_time_s(approach["distance_m"]) or 0.0) if approach else 0.0)
        haul_t = (_leg_time_s(haul["distance_m"]) or 0.0) if haul else 0.0
        deadhead_time += approach_t          # flying to the buy = empty hold
        loaded_time += haul_t                # flying the haul = loaded
        total_time += approach_t + haul_t + 2 * STOP_DWELL_S
        peak_capital = max(peak_capital, row["buy_cost"] or 0)
        for ts in (row.get("buy_updated_at"), row.get("sell_updated_at")):
            if ts is not None and (oldest_ts is None or ts < oldest_ts):
                oldest_ts = ts
        legs.append({
            k: row[k] for k in (
                "commodity", "buy_terminal_id", "buy_terminal", "buy_poi_id", "buy_system",
                "sell_terminal_id", "sell_terminal", "sell_poi_id", "sell_system",
                "buy_price", "sell_price", "profit_per_scu", "supply_scu", "demand_scu",
                "buy_updated_at", "sell_updated_at")
        } | {
            "scu": row["max_scu"], "profit": row["trade_profit"], "buy_cost": row["buy_cost"],
            "held": held,
            "to_buy": None if held else (_leg_view(approach, fuel_req, max_range_m) if approach else None),
            "haul": _leg_view(haul, fuel_req, max_range_m) if haul else None,
        })
        pos = sp if sp is not None else pos
        prev_sell = row["sell_poi_id"]
    hours = total_time / 3600.0
    move = deadhead_time + loaded_time
    summary = {
        "feasible": feasible and bool(legs),
        "legs": len(legs), "stops": stops,
        "total_profit": int(round(total_profit)),
        "peak_capital": int(peak_capital),
        "total_distance_m": total_dist,
        "total_time_s": total_time,
        "deadhead_time_s": deadhead_time,
        "loaded_time_s": loaded_time,
        "loaded_pct": int(round(100 * loaded_time / move)) if move > 0 else None,
        "oldest_updated_at": oldest_ts,
        "profit_per_hour": int(round(total_profit / hours)) if hours > 0 else None,
    }
    if fuel_req is not None:
        views = [lv for leg in legs for lv in (leg.get("to_buy"), leg.get("haul"))]
        tf, oc, wl = _fuel_summary(views)
        summary["total_fuel_scu"] = tf
        summary["over_range_count"] = oc
        summary["worst_leg_m"] = wl
    return {"summary": summary, "legs": legs}


def _greedy_route(nav, cands, start, max_legs, optimize, t_ref, first=None,
                  deadhead_weight=1.0, *, avoid=None, memo=None, max_leg_m=None) -> list[dict]:
    """Build a trade chain by repeatedly taking the best-scoring reachable trade
    from the current position. `optimize` = 'profit' (raw) or 'per_hour'
    (throughput). `deadhead_weight` > 1 penalizes empty-hold repositioning: it
    scales the approach (empty) leg's time in the per-hour score, and in profit
    mode shrinks a trade's score by its approach time — so a fuller-hold chain wins
    even at some profit cost. `first` forces the opening leg (multi-start seed).
    With `avoid` (hazard volumes) a candidate whose haul can't be routed around a
    danger (`blocked`) is skipped — the snare-aware analog of avoid mode.
    `max_leg_m` (in-range only, #27) drops a candidate whose haul OR approach hop
    exceeds the ship's tank — an over-range trade is simply unusable from here."""
    chosen, pos, used = [], start, set()
    key = lambda r: (r["commodity"], r["buy_poi_id"], r["sell_poi_id"])
    while len(chosen) < max_legs:
        pool = [first] if (first is not None and not chosen) else cands
        pick, pick_score = None, 0.0
        for r in pool:
            if key(r) in used:
                continue
            bp = nav.pois.get(r["buy_poi_id"])
            sp = nav.pois.get(r["sell_poi_id"])
            if bp is None or sp is None:
                continue
            haul = travel_cost(nav, bp, sp, t_ref, avoid=avoid, memo=memo)
            if haul["distance_m"] is None:
                continue
            if avoid and haul.get("blocked"):
                continue                      # camped haul with no reroute — skip
            if max_leg_m is not None and haul["distance_m"] > max_leg_m:
                continue                      # loaded haul out of range — skip
            profit = r["trade_profit"] or 0
            # With a range cap the approach must be measured even in profit mode,
            # so an out-of-range reposition drops the candidate.
            approach = (travel_cost(nav, pos, bp, t_ref, avoid=avoid, memo=memo)
                        if (pos is not None and (optimize != "profit" or deadhead_weight > 1.0
                                                 or max_leg_m is not None))
                        else None)
            if (max_leg_m is not None and approach is not None
                    and approach["distance_m"] is not None
                    and approach["distance_m"] > max_leg_m):
                continue                      # empty reposition out of range — skip
            approach_t = ((_leg_time_s(approach["distance_m"]) or 0.0) if approach else 0.0)
            if optimize == "profit":
                # empty-hold hours shrink the score; weight 1.0 => raw profit.
                score = profit / (1.0 + (deadhead_weight - 1.0) * (approach_t / 3600.0))
            else:
                eta = (approach_t * deadhead_weight
                       + (_leg_time_s(haul["distance_m"]) or 0.0) + 2 * STOP_DWELL_S)
                score = profit / (eta / 3600.0) if eta > 0 else 0.0
            if score > pick_score:
                pick, pick_score = r, score
        if pick is None:
            break
        chosen.append(pick)
        used.add(key(pick))
        sp = nav.pois.get(pick["sell_poi_id"])
        pos = sp if sp is not None else pos
    return chosen


def _route_score(summary, optimize, deadhead_weight) -> float:
    """Rank a costed route. profit mode -> total profit shrunk by empty-hold hours;
    per_hour mode -> profit over time with deadhead time up-weighted. weight 1.0
    reduces to raw total_profit / profit_per_hour (unchanged behavior)."""
    dh = summary.get("deadhead_time_s") or 0.0
    if optimize == "profit":
        return summary["total_profit"] / (1.0 + (deadhead_weight - 1.0) * (dh / 3600.0))
    eff_hours = (summary["total_time_s"] + (deadhead_weight - 1.0) * dh) / 3600.0
    return summary["total_profit"] / eff_hours if eff_hours > 0 else 0.0


def _solve_route(nav, cands, start, max_legs, optimize, t_ref, deadhead_weight,
                 *, avoid=None, memo=None, max_leg_m=None) -> list[dict]:
    """Multi-start greedy over the candidate pool: an unforced chain plus one
    forced from each of the top seeds, keeping the best-scoring complete route.
    Returns the chosen trade rows (empty if nothing chains). `avoid`/`memo`
    thread snare-detour costing into every leg cost; `max_leg_m` enforces the
    in-range-only cap (#27)."""
    if not cands or max_legs < 1:
        return []
    routes = [_greedy_route(nav, cands, start, max_legs, optimize, t_ref,
                            deadhead_weight=deadhead_weight, avoid=avoid, memo=memo,
                            max_leg_m=max_leg_m)]
    for seed in cands[:_TRADE_RESTARTS]:
        routes.append(_greedy_route(nav, cands, start, max_legs, optimize, t_ref,
                                    first=seed, deadhead_weight=deadhead_weight,
                                    avoid=avoid, memo=memo, max_leg_m=max_leg_m))
    best, best_score = [], -1.0
    for chosen in routes:
        if not chosen:
            continue
        score = _route_score(_cost_route(nav, chosen, start, t_ref,
                                         avoid=avoid, memo=memo)["summary"],
                             optimize, deadhead_weight)
        if score > best_score:
            best, best_score = chosen, score
    return best


def plan_trade_route(nav: NavData, prices, usable_scu, *, start_id=None,
                     start_pos=None, max_stops=6, commodities=None, system=None,
                     sort="per_hour", budget=None, deadhead_weight=1.0,
                     max_age_s=None, now_ts=None, t_ref=None,
                     avoid_poi_ids=None, avoid_pairs=None, avoid_volumes=None,
                     avoid_buys=None, avoid_sells=None,
                     fuel_req=None, max_range_m=None, in_range_only=False) -> dict:
    """Auto / filtered trade-route solver: pick and order the buy->sell trades that
    maximize profit (sort='profit') or profit/hour (sort='per_hour', default) for a
    `usable_scu` hold, starting from a POI (`start_id`) or live position
    (`start_pos`), within a `max_stops` budget. `commodities` (name list) restricts
    to filtered mode; `system` locks to intra-system trades. `budget` caps each
    hold-fill to affordable aUEC; `deadhead_weight` > 1 trades profit for less
    empty-hold flight; `max_age_s` drops stale price points. Returns
    {summary, legs, start} — the same shape cost_trade_legs produces for manual mode."""
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    optimize = "profit" if sort == "profit" else "per_hour"
    avoid_poi_ids = frozenset(avoid_poi_ids or ())
    avoid_pairs = frozenset(frozenset(p) for p in (avoid_pairs or ()))
    memo = {}
    start = None
    if start_id is not None:
        start = nav.pois.get(start_id)
    elif start_pos is not None:
        start = position_start(nav, start_pos)

    # Decision 5 (#24 v2): with hazard volumes, a snared lane is usually still
    # viable via a detour, so we stop dropping it (`avoid_pairs`) and let the
    # solver pay the honest detour distance in each leg's score instead. Camped
    # endpoints (`avoid_poi_ids`) still drop — no geometry fixes a camped terminal.
    cand_pairs = frozenset() if avoid_volumes else avoid_pairs
    cands = _trade_candidates(prices, usable_scu, system=system, commodities=commodities,
                              budget=budget, max_age_s=max_age_s, now_ts=now_ts,
                              avoid_poi_ids=avoid_poi_ids, avoid_pairs=cand_pairs,
                              avoid_buys=frozenset(avoid_buys or ()),
                              avoid_sells=frozenset(avoid_sells or ()))
    max_legs = max(1, max_stops // 2)
    if not cands:
        empty = _cost_route(nav, [], start, t_ref)
        empty["summary"]["reason"] = "no profitable trades for these filters"
        empty["summary"]["usable_scu"] = float(usable_scu)
        empty["start"] = _start_ref(start)
        return empty

    max_leg_m = max_range_m if (in_range_only and max_range_m) else None
    chosen = _solve_route(nav, cands, start, max_legs, optimize, t_ref, deadhead_weight,
                          avoid=avoid_volumes, memo=memo, max_leg_m=max_leg_m)
    best = _cost_route(nav, chosen, start, t_ref, avoid=avoid_volumes, memo=memo,
                       fuel_req=fuel_req, max_range_m=max_range_m)
    best["summary"]["usable_scu"] = float(usable_scu)
    best["start"] = _start_ref(start)
    return best


def cost_trade_legs(nav: NavData, prices, legs_in, usable_scu, *, start_id=None,
                    start_pos=None, budget=None, t_ref=None, avoid_volumes=None,
                    fuel_req=None, max_range_m=None) -> dict:
    """Manual mode: cost a player-chosen ordered list of legs (each
    {commodity, buy_terminal_id, sell_terminal_id, scu?}) into the same
    {summary, legs, start} shape the solver returns — no route selection, just live
    prices + running profit/time. A leg's `scu` optionally caps the load below the
    supply/demand/hold maximum; `budget` caps each fill to affordable aUEC. Raises
    ValueError on an unknown/unpriced leg. Manual legs are never dropped, but with
    `avoid_volumes` they get costed detours + `blocked` badges (#24 v2)."""
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    idx = {(p["commodity"], p["terminal_id"]): p for p in prices}
    chosen = []
    for lg in legs_in:
        name = lg["commodity"]
        src = idx.get((name, lg["buy_terminal_id"]))
        dst = idx.get((name, lg["sell_terminal_id"]))
        if src is None or dst is None or not src.get("buy") or not dst.get("sell"):
            raise ValueError(f"leg references an unknown or unpriced terminal for {name}")
        margin = int(dst["sell"]) - int(src["buy"])
        row = _trade_row(name, src, dst, margin, usable_scu, budget=budget)
        scu = lg.get("scu")
        if scu:                                   # honor an explicit per-leg cap
            capped = min(int(scu), row["max_scu"] or int(scu))
            row["max_scu"] = capped
            row["trade_profit"] = int(round(margin * capped))
            row["buy_cost"] = int(round(row["buy_price"] * capped))
        chosen.append(row)
    start = None
    if start_id is not None:
        start = nav.pois.get(start_id)
    elif start_pos is not None:
        start = position_start(nav, start_pos)
    costed = _cost_route(nav, chosen, start, t_ref, avoid=avoid_volumes, memo={},
                         fuel_req=fuel_req, max_range_m=max_range_m)
    costed["summary"]["usable_scu"] = float(usable_scu)
    costed["start"] = _start_ref(start)
    return costed


def _held_sell_leg(nav, prices, held, start, *, system, max_age_s, now, t_ref,
                   optimize, avoid=None, memo=None,
                   avoid_sells=frozenset()) -> tuple[dict | None, "Poi | None"]:
    """The best buyer for cargo the player is already carrying (sunk cost), as a
    sell-only leg. The buy is already paid for, so buy_cost going forward is 0 and
    profit is the realized spread over the recorded buy price. Prefers a buyer in
    the locked `system`, but a held load must be offloadable somewhere — falls back
    to any system if the lock strands it. `avoid_sells` drops buyers with a fresh
    no-demand report — unlike danger volumes (which only make a buyer costlier), a
    terminal that won't take the cargo is a hard no. Returns (leg_row, sell_poi) or
    (None, None) when nothing buys the commodity."""
    name = held.get("commodity")
    scu = int(round(float(held.get("scu") or 0)))
    buy_price = int(held.get("buy_price") or 0)
    cnorm = (name or "").strip().lower()

    def buyers(locked):
        out = []
        for p in prices:
            if (p.get("commodity") or "").strip().lower() != cnorm:
                continue
            if locked and system and p.get("system") != system:
                continue
            if not p.get("sell") or not _price_fresh(p, max_age_s, now):
                continue
            if avoid_sells and (p.get("poi_id"), cnorm) in avoid_sells:
                continue
            if nav.pois.get(p.get("poi_id")) is None:
                continue
            out.append(p)
        return out

    pts = buyers(True) or buyers(False)
    if not pts or scu <= 0:
        return None, None
    best, best_score, best_poi = None, -1e18, None
    for p in pts:
        sp = nav.pois.get(p["poi_id"])
        leg = travel_cost(nav, start, sp, t_ref, avoid=avoid, memo=memo) if start is not None else None
        eta = _leg_time_s(leg["distance_m"]) if (leg and leg["distance_m"] is not None) else None
        revenue = int(p["sell"]) * scu
        if optimize == "profit":
            score = revenue
        else:
            hours = ((eta or 0.0) + STOP_DWELL_S) / 3600.0
            score = revenue / hours if hours > 0 else float(revenue)
        if score > best_score:
            best, best_score, best_poi = p, score, sp
    margin = int(best["sell"]) - buy_price
    row = {
        "commodity": name,
        "buy_terminal_id": None, "buy_terminal": "carried cargo",
        "buy_poi_id": (start.id if start is not None else None),
        "buy_system": (start.system if start is not None else None),
        "sell_terminal_id": best.get("terminal_id"), "sell_terminal": best.get("terminal"),
        "sell_poi_id": best.get("poi_id"), "sell_system": best.get("system"),
        "buy_price": buy_price, "sell_price": int(best["sell"]),
        "profit_per_scu": margin,
        "supply_scu": 0, "demand_scu": best.get("scu_sell_stock") or 0,
        "buy_updated_at": None, "sell_updated_at": best.get("updated_at"),
        "distance_m": None, "eta_s": None, "cross_system": None, "via_gate": None,
        "max_scu": scu, "trade_profit": int(round(margin * scu)), "buy_cost": 0,
        "profit_per_hour": None, "held": True,
    }
    return row, best_poi


def replan_trade_route(nav: NavData, prices, usable_scu, *, start_id=None,
                       start_pos=None, held=None, max_stops=6, commodities=None,
                       system=None, sort="per_hour", budget=None,
                       deadhead_weight=1.0, max_age_s=None, now_ts=None,
                       t_ref=None, avoid_poi_ids=None, avoid_pairs=None,
                       avoid_volumes=None, avoid_buys=None, avoid_sells=None,
                       fuel_req=None, max_range_m=None, in_range_only=False) -> dict:
    """Re-solve a trade route from the player's *current* position mid-run,
    carrying forward any sunk cargo. `held` = {commodity, scu, buy_price}: a hold
    already loaded with cargo that's been paid for but not yet sold. The new plan
    must offload it first (best reachable buyer for that commodity — the sell leg
    is flagged `held`, has no buy approach, and needs no forward capital), then
    chain further trades with the freed hold. Without `held` this is just
    plan_trade_route from the live position. Returns the {summary, legs, start}
    shape; the summary carries `carried_commodity`/`carried_scu` when a sunk load
    was folded in."""
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    optimize = "profit" if sort == "profit" else "per_hour"
    now = now_ts if now_ts is not None else time.time()
    avoid_poi_ids = frozenset(avoid_poi_ids or ())
    avoid_pairs = frozenset(frozenset(p) for p in (avoid_pairs or ()))
    memo = {}

    held_scu = float(held.get("scu")) if (held and held.get("scu")) else 0.0
    if held_scu <= 0:
        return plan_trade_route(
            nav, prices, usable_scu, start_id=start_id, start_pos=start_pos,
            max_stops=max_stops, commodities=commodities, system=system, sort=sort,
            budget=budget, deadhead_weight=deadhead_weight, max_age_s=max_age_s,
            now_ts=now_ts, t_ref=t_ref, avoid_poi_ids=avoid_poi_ids,
            avoid_pairs=avoid_pairs, avoid_volumes=avoid_volumes,
            avoid_buys=avoid_buys, avoid_sells=avoid_sells,
            fuel_req=fuel_req, max_range_m=max_range_m, in_range_only=in_range_only)

    start = None
    if start_id is not None:
        start = nav.pois.get(start_id)
    elif start_pos is not None:
        start = position_start(nav, start_pos)

    # The held-cargo sell leg is costed with volumes too — you're already loaded,
    # so a detour on the sell approach is exactly what a mid-run reroute is for.
    # It also honors demand-side stock reports (`avoid_sells`): "this terminal
    # won't buy my cargo → re-plan" is exactly the moment a fresh no-demand
    # report must steer the replacement buyer.
    held_row, sell_poi = _held_sell_leg(
        nav, prices, held, start, system=system, max_age_s=max_age_s, now=now,
        t_ref=t_ref, optimize=optimize, avoid=avoid_volumes, memo=memo,
        avoid_sells=frozenset(avoid_sells or ()))
    if held_row is None:
        empty = _cost_route(nav, [], start, t_ref)
        empty["summary"]["reason"] = f"no known buyer for held {held.get('commodity')}"
        empty["summary"]["usable_scu"] = float(usable_scu)
        empty["summary"]["carried_commodity"] = held.get("commodity")
        empty["summary"]["carried_scu"] = held_scu
        empty["start"] = _start_ref(start)
        return empty

    # Chain further trades from the sell terminal with the freed (full) hold. The
    # held-cargo sell leg above is left unfiltered — a sunk load must be offloadable
    # even if its only buyer sits in a warned zone — but the continuation avoids danger.
    cand_pairs = frozenset() if avoid_volumes else avoid_pairs
    cands = _trade_candidates(prices, usable_scu, system=system,
                              commodities=commodities, budget=budget,
                              max_age_s=max_age_s, now_ts=now,
                              avoid_poi_ids=avoid_poi_ids, avoid_pairs=cand_pairs,
                              avoid_buys=frozenset(avoid_buys or ()),
                              avoid_sells=frozenset(avoid_sells or ()))
    cont_legs = max(0, (max_stops - 1) // 2)      # held sell used one stop
    max_leg_m = max_range_m if (in_range_only and max_range_m) else None
    cont = _solve_route(nav, cands, sell_poi, cont_legs, optimize, t_ref,
                        deadhead_weight, avoid=avoid_volumes, memo=memo,
                        max_leg_m=max_leg_m) if cont_legs else []
    best = _cost_route(nav, [held_row] + cont, start, t_ref, avoid=avoid_volumes, memo=memo,
                       fuel_req=fuel_req, max_range_m=max_range_m)
    best["summary"]["usable_scu"] = float(usable_scu)
    best["summary"]["carried_commodity"] = held.get("commodity")
    best["summary"]["carried_scu"] = held_scu
    best["start"] = _start_ref(start)
    return best


def trade_leg_realized(leg: dict) -> int | None:
    """Realized profit for a completed leg. Prefers the actual buy/sell figures the
    player entered at the terminal (`actual_buy_price`/`actual_buy_scu`,
    `actual_sell_price`/`actual_sell_scu`) over the plan's — so earnings stats
    reflect what really happened, not UEX's possibly-stale scrape. Each side falls
    back to its planned value when unentered; a held leg's buy is the sunk
    `buy_price`/`scu`. Returns the plan's `profit` when nothing was entered."""
    bp, bs = leg.get("actual_buy_price"), leg.get("actual_buy_scu")
    sp, ss = leg.get("actual_sell_price"), leg.get("actual_sell_scu")
    if bp is None and bs is None and sp is None and ss is None:
        return leg.get("profit")                       # nothing entered — planned
    buy_price = bp if bp is not None else leg.get("buy_price")
    buy_scu = bs if bs is not None else leg.get("scu")
    sell_price = sp if sp is not None else leg.get("sell_price")
    sell_scu = ss if ss is not None else leg.get("scu")
    return int(round((sell_price or 0) * (sell_scu or 0)
                     - (buy_price or 0) * (buy_scu or 0)))


# ---------------------------------------------------------------------------
# Trade-route planner (#21): history + statistics (step 6).
#
# Same Learn-layer shape as the cargo planner (derive_run_stats / quick_picks /
# guild_cargo_stats), over the trade_runs blobs instead. The headline aUEC number
# is *realized* profit — the actual buy/sell figures a player entered at each
# terminal (trade_leg_realized), not UEX's scraped estimate — so a trader's stats
# reflect what they actually earned. Distance/time ride from the frozen plan
# summary (updated on every re-plan). Pure derivations over the stored blobs.
# ---------------------------------------------------------------------------


def _trade_sold_legs(run: dict) -> list[dict]:
    """A run's transacted legs. Prefers the parallel `leg_states` (a completed run
    is all 'sold'; a mid-run blob mixes states) and returns only the sold ones;
    falls back to every leg when states are missing or mis-sized (older blobs).
    A `skipped` leg (bailed / stock-out) is never transacted — it parks in the
    'sold' state to move the cursor, but counting its *planned* profit as realized
    would inflate every stat, so it's dropped here."""
    legs = run.get("legs") or []
    states = run.get("leg_states")
    if not states or len(states) != len(legs):
        return [l for l in legs if not l.get("skipped")]
    return [l for l, st in zip(legs, states) if st == "sold" and not l.get("skipped")]


def trade_run_realized(run: dict) -> int:
    """A trade run's realized aUEC profit: the sum of each sold leg's realized
    profit (entered actuals, else the plan — see `trade_leg_realized`). Mirrors the
    live tally `Session.trade_run_view` shows in run mode so history agrees."""
    return int(sum(trade_leg_realized(l) or 0 for l in _trade_sold_legs(run)))


def trade_run_scu(run: dict) -> float:
    """Total SCU actually moved across a run's sold legs — the entered sell SCU
    when the player recorded it, else the planned load."""
    total = 0.0
    for l in _trade_sold_legs(run):
        total += float(l.get("actual_sell_scu") or l.get("scu") or 0)
    return total


def _trade_run_summary_num(run: dict, key: str) -> float:
    """A numeric field off a run's frozen plan `summary` (distance/time totals),
    0 when absent."""
    return float((run.get("summary") or {}).get(key) or 0)


def derive_trade_run_stats(runs) -> dict:
    """Headline trading analytics over a set of completed trade runs: realized
    profit, SCU moved, distance/time, and the overall aUEC/hour (realized profit ÷
    run time). Runs predating a metric contribute 0 and are simply diluted, same as
    the cargo planner's `derive_run_stats`."""
    total_profit = total_scu = total_dist = total_time = 0.0
    for run in runs:
        total_profit += trade_run_realized(run)
        total_scu += trade_run_scu(run)
        total_dist += _trade_run_summary_num(run, "total_distance_m")
        total_time += _trade_run_summary_num(run, "total_time_s")
    per_hr = (total_profit / (total_time / 3600.0)) if total_time > 0 else None
    return {
        "num_runs": len(runs),
        "total_profit": int(round(total_profit)),
        "total_scu": round(total_scu, 2),
        "total_distance_m": round(total_dist, 2),
        "total_time_s": round(total_time, 2),
        "auec_per_hour": round(per_hr, 2) if per_hr is not None else None,
    }


def _trade_lane_pick(leg: dict, count: int) -> dict:
    """A quick-pick / top-lane row from a sample leg: the commodity plus both
    terminals (id + name + system + resolved POI id), so the UI can re-enter the
    whole buy→sell leg in manual mode with one click."""
    return {
        "commodity": leg.get("commodity"),
        "buy_terminal_id": leg.get("buy_terminal_id"), "buy_terminal": leg.get("buy_terminal"),
        "buy_poi_id": leg.get("buy_poi_id"), "buy_system": leg.get("buy_system"),
        "sell_terminal_id": leg.get("sell_terminal_id"), "sell_terminal": leg.get("sell_terminal"),
        "sell_poi_id": leg.get("sell_poi_id"), "sell_system": leg.get("sell_system"),
        "count": count,
    }


def _tally_trade_legs(runs):
    """Shared fold over a run set's sold legs for the quick-picks / stats builders.
    Returns per-commodity SCU + count, per-lane count + a sample leg, and per-ship
    count. A `held` leg (carried cargo from a re-plan) has no real buy terminal, so
    it counts toward its commodity but never toward a lane."""
    lane_ct, lane_meta = {}, {}
    commodity_ct, commodity_scu = {}, {}
    ship_ct = {}
    for run in runs:
        ship = (run.get("ship") or "").strip()
        if ship:
            ship_ct[ship] = ship_ct.get(ship, 0) + 1
        for l in _trade_sold_legs(run):
            name = (l.get("commodity") or "").strip()
            if not name:
                continue
            commodity_ct[name] = commodity_ct.get(name, 0) + 1
            commodity_scu[name] = commodity_scu.get(name, 0.0) + \
                float(l.get("actual_sell_scu") or l.get("scu") or 0)
            if l.get("held"):
                continue
            btid, stid = l.get("buy_terminal_id"), l.get("sell_terminal_id")
            if btid is not None and stid is not None:
                k = (name, btid, stid)
                lane_ct[k] = lane_ct.get(k, 0) + 1
                lane_meta[k] = l
    return lane_ct, lane_meta, commodity_ct, commodity_scu, ship_ct


def _trade_top_lanes(nav: NavData, lane_ct, lane_meta, limit) -> list[dict]:
    """Lanes ranked by frequency, dropping any whose terminals no longer resolve on
    the live map (so a stale lane never re-enters an unroutable leg)."""
    out = []
    for k, ct in sorted(lane_ct.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        l = lane_meta[k]
        if nav.pois.get(l.get("buy_poi_id")) is None or nav.pois.get(l.get("sell_poi_id")) is None:
            continue
        out.append(_trade_lane_pick(l, ct))
        if len(out) >= limit:
            break
    return out


def derive_trade_quick_picks(nav: NavData, runs, limit: int = 12) -> dict:
    """Frequency-ranked data-entry priors from a member's completed trade runs: the
    buy→sell lanes they run most (for one-click manual re-entry), the commodities
    they trade (with the SCU amount they most often move — a filter prior), and the
    ships they fly. Feeds `#/trade`'s quick-picks; pure derivation over the blobs."""
    lane_ct, lane_meta, commodity_ct, commodity_scu, ship_ct = _tally_trade_legs(runs)
    # commodity_scu here is a total; quick-picks want the *most-often-moved* amount,
    # so re-fold sold legs into a per-commodity SCU histogram.
    scu_hist: dict = {}
    for run in runs:
        for l in _trade_sold_legs(run):
            name = (l.get("commodity") or "").strip()
            scu = l.get("actual_sell_scu") or l.get("scu")
            if name and scu:
                by = scu_hist.setdefault(name, {})
                by[float(scu)] = by.get(float(scu), 0) + 1

    lanes = _trade_top_lanes(nav, lane_ct, lane_meta, limit)
    commodities = []
    for name, ct in sorted(commodity_ct.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]:
        scu = None
        by = scu_hist.get(name)
        if by:                             # the SCU amount most often moved for it
            scu = max(by.items(), key=lambda kv: (kv[1], kv[0]))[0]
        commodities.append({"commodity": name, "count": ct, "scu": scu})
    ships = [{"ship": s, "count": c}
             for s, c in sorted(ship_ct.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
    return {"lanes": lanes, "commodities": commodities, "ships": ships}


def derive_guild_trade_stats(nav: NavData, runs, limit: int = 15) -> dict:
    """Guild-wide trading aggregates for the `#/trade-stats` page: the headline
    totals (realized profit / SCU / distance / time + aUEC/hour, via
    `derive_trade_run_stats`), a count of distinct traders, and the top commodities
    (by total SCU moved), busiest lanes, and most-flown ships across every member's
    completed trade runs. The top-traders board + weekly series are added by the
    endpoint (it owns name resolution + the iso-week helper)."""
    base = derive_trade_run_stats(runs)
    traders = {str(r.get("discord_id")) for r in runs if r.get("discord_id")}
    lane_ct, lane_meta, commodity_ct, commodity_scu, ship_ct = _tally_trade_legs(runs)
    commodities = [
        {"commodity": n, "scu": round(commodity_scu[n], 2), "count": commodity_ct[n]}
        for n in sorted(commodity_scu, key=lambda k: (-commodity_scu[k], k))[:limit]
    ]
    lanes = _trade_top_lanes(nav, lane_ct, lane_meta, limit)
    ships = [{"ship": s, "count": c}
             for s, c in sorted(ship_ct.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
    return {**base, "num_traders": len(traders), "top_commodities": commodities,
            "top_lanes": lanes, "top_ships": ships}


def derive_trade_leaderboard(runs) -> list[dict]:
    """Per-member trading tallies for the top-traders board. Groups completed trade
    runs by owning `discord_id` and runs `derive_trade_run_stats` over each group,
    so a member's whole trading record collapses to one row (the stat block plus a
    `display_name` lifted from their most recent run that carried one). The endpoint
    resolves missing names and ranks. Runs lacking a discord_id are skipped."""
    by_member: dict[str, list] = {}
    for run in runs:
        did = str(run.get("discord_id") or "")
        if not did:
            continue
        by_member.setdefault(did, []).append(run)
    rows = []
    for did, member_runs in by_member.items():
        name = next((r.get("display_name") for r in member_runs if r.get("display_name")), None)
        rows.append({"discord_id": did, "display_name": name,
                     **derive_trade_run_stats(member_runs)})
    return rows


def derive_guild_leaderboard(runs) -> list[dict]:
    """Per-member hauling tallies for the guild leaderboard. Groups completed
    runs by their owning `discord_id` and runs `derive_run_stats` over each
    group, so one member's whole hauling record collapses to a single row (the
    stat block plus a `display_name` lifted from their most recent run that
    carried one). The endpoint resolves missing names, ranks, and splits the
    rows into the top-earner and aUEC/hour boards. Runs lacking a discord_id
    (shouldn't happen for persisted runs) are skipped."""
    by_member: dict[str, list] = {}
    for run in runs:
        did = str(run.get("discord_id") or "")
        if not did:
            continue
        by_member.setdefault(did, []).append(run)
    rows = []
    for did, member_runs in by_member.items():
        # member_runs preserve the freshest-first order they arrived in, so the
        # first that carries a name is the most recent one.
        name = next((r.get("display_name") for r in member_runs if r.get("display_name")), None)
        rows.append({"discord_id": did, "display_name": name,
                     **derive_run_stats(member_runs)})
    return rows


def derive_guild_cargo_stats(nav: NavData, runs, limit: int = 15) -> dict:
    """Guild-wide hauling aggregates for the cargo Statistics page: the headline
    totals (reward / SCU / distance / time + aUEC/hour, via `derive_run_stats`)
    plus a count of distinct haulers, and the top commodities (by total SCU
    moved), busiest lanes, and most-run ships across every member's completed
    runs. Pure derivation; POI ids resolve against the live catalog and lanes
    that no longer resolve are dropped. The weekly activity series is added by
    the endpoint, which owns the iso-week helper."""
    base = derive_run_stats(runs)
    haulers = {str(r.get("discord_id")) for r in runs if r.get("discord_id")}
    lane_ct, ship_ct = {}, {}
    commodity_scu, commodity_ct = {}, {}
    for run in runs:
        ship = (run.get("ship") or "").strip()
        if ship:
            ship_ct[ship] = ship_ct.get(ship, 0) + 1
        seen_groups = set()   # a multi-pickup group's total counts once per run
        for p in run_packages(run):
            fid, tid = p.get("from_id"), p.get("to_id")
            if fid is not None and tid is not None:
                lane_ct[(int(fid), int(tid))] = lane_ct.get((int(fid), int(tid)), 0) + 1
            name = (p.get("commodity") or "").strip()
            if name:
                g = p.get("group")
                if g is None:
                    commodity_scu[name] = commodity_scu.get(name, 0.0) + float(p.get("scu") or 0)
                    commodity_ct[name] = commodity_ct.get(name, 0) + 1
                elif g not in seen_groups:
                    seen_groups.add(g)
                    commodity_scu[name] = commodity_scu.get(name, 0.0) + float(p.get("group_scu") or 0)
                    commodity_ct[name] = commodity_ct.get(name, 0) + 1

    lanes = []
    for (fid, tid), ct in sorted(lane_ct.items(), key=lambda kv: (-kv[1], kv[0])):
        fn, tn = _poi_name(nav, fid), _poi_name(nav, tid)
        if fn is None or tn is None:
            continue                       # a POI that no longer resolves — skip
        lanes.append({"from_id": fid, "from_name": fn, "to_id": tid,
                      "to_name": tn, "count": ct})
        if len(lanes) >= limit:
            break
    commodities = [
        {"commodity": n, "scu": round(commodity_scu[n], 2), "count": commodity_ct[n]}
        for n in sorted(commodity_scu, key=lambda k: (-commodity_scu[k], k))[:limit]
    ]
    ships = [{"ship": s, "count": c}
             for s, c in sorted(ship_ct.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
    return {**base, "num_haulers": len(haulers), "top_commodities": commodities,
            "top_lanes": lanes, "top_ships": ships}


def derive_market_stats(listings, limit: int = 15) -> dict:
    """Guild marketplace aggregates for the Org Intel Market section, over
    *completed* deals only (the dual-confirm handshake; expired/cancelled
    listings never reach here, so this measures confirmed trades, not ads).
    aUEC volume and the seller board cover sale + auction deals (`final_auec`
    frozen at completion); barter deals are counted on their own — goods-for-
    goods, no aUEC. Pure derivation: returns seller member ids for the endpoint
    to name + rank, and the weekly volume series is added there."""
    auec_volume = items_moved = 0.0
    barter_deals = 0
    traders: set[str] = set()
    seller_auec: dict[str, float] = {}
    seller_deals: dict[str, int] = {}
    item_qty: dict[str, float] = {}
    item_ct: dict[str, int] = {}
    for lst in listings:
        sid = str(lst.get("seller_id") or "")
        bid = str(lst.get("buyer_id") or "")
        if sid:
            traders.add(sid)
        if bid:
            traders.add(bid)
        qty = float(lst.get("qty") or 0)
        items_moved += qty
        name = (lst.get("item_name") or "").strip()
        if name:
            item_qty[name] = item_qty.get(name, 0.0) + qty
            item_ct[name] = item_ct.get(name, 0) + 1
        if lst.get("mode") == "barter":
            barter_deals += 1
            continue
        amt = lst.get("final_auec")
        amt = float(amt) if amt is not None else 0.0
        auec_volume += amt
        if sid:
            seller_auec[sid] = seller_auec.get(sid, 0.0) + amt
            seller_deals[sid] = seller_deals.get(sid, 0) + 1
    top_sellers = [
        {"discord_id": sid, "auec": round(seller_auec[sid], 2),
         "deals": seller_deals.get(sid, 0)}
        for sid in sorted(seller_auec, key=lambda k: (-seller_auec[k], k))[:limit]
    ]
    top_items = [
        {"item": n, "qty": round(item_qty[n], 2), "count": item_ct[n]}
        for n in sorted(item_qty, key=lambda k: (-item_qty[k], k))[:limit]
    ]
    return {
        "num_deals": len(listings),
        "auec_volume": round(auec_volume, 2),
        "items_moved": round(items_moved, 2),
        "num_traders": len(traders),
        "barter_deals": barter_deals,
        "auec_deals": len(listings) - barter_deals,
        "top_sellers": top_sellers,
        "top_items": top_items,
    }


def derive_event_fill(event: dict, signups) -> dict:
    """Fill of an event against its target roster (design: docs/event-planner.md).

    `event` carries `min_players`, `max_players` (None ⇒ unlimited) and a target
    roster `roles=[{role, needed}]`; `signups` is the list of signups, each with
    a `roles` list and a `status` (going | maybe | withdrawn; missing ⇒ going).

    The one rule the headline and the bars disagree on, on purpose: a member
    counts toward **every role they list** for the per-role bars (a medic who'll
    also escort fills both), but `total_going` counts **distinct members** — so a
    5-person op where two people each cover Medical and Escort shows `5 players`
    up top and full Medical *and* Escort bars below, and neither figure lies.
    Pure derivation; the list endpoint embeds this so cards render without N+1."""
    going = [s for s in signups if (s.get("status") or "going") == "going"]

    # Distinct members. UNIQUE(event_id, discord_id) should already guarantee one
    # signup per member, but de-dupe defensively so a stray double never inflates
    # the headcount (or any role bar).
    seen, members = set(), []
    for s in going:
        did = s.get("discord_id")
        if did in seen:
            continue
        seen.add(did)
        members.append(s)
    total = len(members)

    min_players = int(event.get("min_players") or 0)
    max_players = event.get("max_players")
    max_players = int(max_players) if max_players is not None else None

    role_filled: dict[str, int] = {}
    for s in members:
        for role in {r for r in (s.get("roles") or []) if r}:   # per-member set
            role_filled[role] = role_filled.get(role, 0) + 1

    roster = []
    for r in (event.get("roles") or []):
        needed = int(r.get("needed") or 0)
        filled = role_filled.get(r.get("role"), 0)
        roster.append({"role": r.get("role"), "needed": needed, "filled": filled,
                       "short": max(0, needed - filled)})

    return {
        "total_going": total,
        "min_players": min_players,
        "max_players": max_players,
        "spots_left": None if max_players is None else max(0, max_players - total),
        "min_met": total >= min_players,
        "is_full": max_players is not None and total >= max_players,
        "roster": roster,
    }


# How long a no-duration event stays "live" after its start before it's treated as
# finished — events without a set duration have no real end time, so we give them a
# grace window rather than flipping to finished the instant they start.
EVENT_LIVE_GRACE_MIN = 180


def _parse_event_dt(s):
    """Parse a stored event timestamp (UTC ISO8601) to an aware datetime, or None.
    Tolerates the trailing-Z form the same way `_normalize_event_start` does."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def derive_event_phase(event: dict, now_dt: datetime) -> dict:
    """Lifecycle phase of an event, derived purely from its timestamps + status.

    No stored phase column: an event moves Open → Signups closed → Live → Finished
    on its own as the clock passes its signup deadline, start, and end. `cancelled`
    and an explicit `completed` status are the only stored overrides.

      open    — accepting signups (now < signup close)
      closed  — signups locked, not started yet (signup close ≤ now < start)
      live    — under way (start ≤ now < end)
      ended   — finished (now ≥ end, or status 'completed')
      cancelled — explicitly cancelled

    `signup close` is the signup deadline if set, else the start. `end` is
    start + duration, or start + EVENT_LIVE_GRACE_MIN when no duration is set.
    Returns the phase plus the derived `signups_open` flag and the resolved
    `signup_close` / `end_at` ISO strings (None when start is unparseable)."""
    status = event.get("status")
    start = _parse_event_dt(event.get("start_at"))
    deadline = _parse_event_dt(event.get("signup_deadline"))
    signup_close = deadline or start
    if start is not None:
        dur = event.get("duration_min")
        grace = int(dur) if dur else EVENT_LIVE_GRACE_MIN
        end_at = start + timedelta(minutes=grace)
    else:
        end_at = None

    if status == "cancelled":
        phase = "cancelled"
    elif start is None:
        phase = "open"                          # malformed start: leave it joinable
    elif status == "completed" or now_dt >= end_at:
        phase = "ended"
    elif now_dt >= start:
        phase = "live"
    elif signup_close is not None and now_dt >= signup_close:
        phase = "closed"
    else:
        phase = "open"

    return {
        "phase": phase,
        "signups_open": phase == "open",
        "signup_close": signup_close.isoformat() if signup_close else None,
        "end_at": end_at.isoformat() if end_at else None,
    }


# --- fleet roster / squad organizer (#20) ----------------------------------
# The plan (groups + assignments) is a layer over the signups. These pure helpers
# turn the three stored lists (groups, assignments, going-signups) into the board
# the client renders and the Discord manifest — no DB, no I/O.

GROUP_KINDS = ("squad", "squadron", "crew", "section", "wing")

# uexcorp is_* role flags → the specialist seat they imply, in priority order.
# A role-specialized ship gets one flavored seat in its default template.
SHIP_ROLE_FLAGS = (
    ("is_medical", "Medic"), ("is_mining", "Mining Op"),
    ("is_salvage", "Salvage Op"), ("is_refuel", "Fuel Op"),
    ("is_repair", "Repair Op"), ("is_science", "Science Op"),
    ("is_datarunner", "Data Op"),
)


def ship_seat_template(crew, traits=()) -> list[str]:
    """A default seat layout for a `crew`-size ship (design:
    docs/fleet-roster-squad-organizer.md, #20 v1.1). `traits` is a set of the
    ship's uexcorp is_* role flags, which flavor one specialist seat (a medical
    ship gets a Medic, a mining ship a Mining Op). Pure + deterministic so the
    ships feed, the endpoint, and the tests share one source of truth.

    Returns exactly `crew` seat labels: Pilot, Co-Pilot (crew>=2), an optional
    specialist seat, then Turret 1..N to fill the rest. Suggestions only — the
    organizer freely renames a seat when assigning."""
    try:
        n = int(crew)
    except (TypeError, ValueError):
        n = 1
    n = max(1, min(n, 50))
    traits = set(traits)
    seats = ["Pilot"]
    if n >= 2:
        seats.append("Co-Pilot")
    specialist = next((label for flag, label in SHIP_ROLE_FLAGS if flag in traits), None)
    if specialist and len(seats) < n:
        seats.append(specialist)
    turret = 1
    while len(seats) < n:
        seats.append(f"Turret {turret}")
        turret += 1
    return seats[:n]


def derive_roster_board(groups, assignments, signups, names=None) -> dict:
    """Assemble the organizer's roster board (design: docs/fleet-roster-squad-organizer.md).

    Inputs are the three stored lists for one event:
      `groups`      — [{id, parent_id, name, kind, ship, capacity, leader_id, notes, sort}]
      `assignments` — [{discord_id, group_id, slot}]
      `signups`     — event signups ([{discord_id, roles, status}])
    `names` optionally maps discord_id → display name (else a short stub is used).

    Only members who are a *going* signup can hold a seat, so an assignment whose
    member has withdrawn (or was never a signup) is dropped — the plan self-heals
    as the roster changes. Returns:

      {groups: [{...group, members: [{discord_id, name, slot, is_leader}],
                 filled, capacity, short}],
       unassigned: [{discord_id, name, roles}],   # going members with no seat
       assigned_count, total_going}

    Groups keep their stored order; each carries its members (leader first) and a
    fill/capacity readout. Pure derivation so the endpoint fans it out N+1-free."""
    names = names or {}

    def nm(did):
        return names.get(did) or f"Member {str(did)[-4:]}"

    going = {}
    for s in signups:
        if (s.get("status") or "going") == "going":
            going[s.get("discord_id")] = s        # UNIQUE keeps this one-per-member

    by_group: dict[int, list] = {}
    seated = set()
    for a in assignments:
        did = a.get("discord_id")
        if did not in going or did in seated:      # withdrawn / stray double
            continue
        seated.add(did)
        by_group.setdefault(a.get("group_id"), []).append(a)

    out_groups = []
    for g in groups:
        gid = g.get("id")
        leader = g.get("leader_id")
        members = []
        for a in by_group.get(gid, []):
            did = a.get("discord_id")
            members.append({"discord_id": did, "name": nm(did),
                            "slot": a.get("slot") or "", "is_leader": did == leader})
        # Leader first, then by name for a stable, readable order.
        members.sort(key=lambda m: (not m["is_leader"], m["name"].lower()))
        cap = g.get("capacity")
        cap = int(cap) if cap not in (None, "") else None
        filled = len(members)
        out_groups.append({
            "id": gid, "parent_id": g.get("parent_id"), "name": g.get("name"),
            "kind": g.get("kind"), "ship": g.get("ship"), "notes": g.get("notes"),
            "leader_id": leader, "sort": g.get("sort") or 0,
            "members": members, "filled": filled, "capacity": cap,
            "short": None if cap is None else max(0, cap - filled),
        })

    unassigned = [
        {"discord_id": did, "name": nm(did), "roles": s.get("roles") or []}
        for did, s in going.items() if did not in seated
    ]
    unassigned.sort(key=lambda m: m["name"].lower())

    return {
        "groups": out_groups,
        "unassigned": unassigned,
        "assigned_count": len(seated),
        "total_going": len(going),
    }


def build_event_manifest(event, board) -> str:
    """Render a roster board as Discord-flavored markdown — the op order at a
    glance, ready to paste or auto-post (#18). `event` supplies the header
    (title/start/rally); `board` is a derive_roster_board() result. Pure text."""
    lines = [f"**{event.get('title') or 'Event'} — Fleet Manifest**"]
    where = event.get("location")
    if where:
        lines.append(f"Rally: {where}")
    lines.append("")

    for g in board.get("groups", []):
        head = g["name"]
        bits = []
        if g.get("ship"):
            bits.append(g["ship"])
        cap = g.get("capacity")
        bits.append(f"{g['filled']}/{cap}" if cap is not None else f"{g['filled']}")
        head += f" ({', '.join(bits)})"
        lines.append(f"__{head}__")
        if g["members"]:
            for m in g["members"]:
                tag = " ⭐" if m["is_leader"] else ""
                seat = f" — {m['slot']}" if m["slot"] else ""
                lines.append(f"• {m['name']}{seat}{tag}")
        else:
            lines.append("• _(empty)_")
        lines.append("")

    pool = board.get("unassigned", [])
    if pool:
        lines.append(f"__Unassigned ({len(pool)})__")
        for m in pool:
            lines.append(f"• {m['name']}")
    return "\n".join(lines).rstrip()


def _qty(v) -> float:
    """Coerce a stored quantity to a float, treating junk as 0."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def derive_inventory_rollup(rows) -> list[dict]:
    """Org inventory rollup (design: docs/org-inventory-goals.md).

    SC has no shared org storage, so the "org inventory" is never a stored total —
    it's this derived `SUM(qty) GROUP BY item` over every member's attributed
    pledges. `rows` is the inventory ledger ([{item_id, item_name, unit, qty,
    owner_id, location, ...}]). Returns one entry per item, biggest total first:

      {item_id, name, unit, total, holders (distinct owners),
       by_owner: [{owner_id, qty}], by_location: [{location, qty}]}

    Pure derivation so the endpoint can fan it out without an N+1; the per-owner /
    per-location breakdowns power the expandable rows in the #/inventory view."""
    items: dict[str, dict] = {}
    for r in rows or []:
        iid = r.get("item_id")
        if not iid:
            continue
        it = items.get(iid)
        if it is None:
            it = items[iid] = {
                "item_id": iid,
                "name": r.get("item_name") or iid,
                "unit": r.get("unit"),
                "total": 0.0,
                "by_owner": {},
                "by_location": {},
            }
        qty = _qty(r.get("qty"))
        it["total"] += qty
        owner = r.get("owner_id")
        it["by_owner"][owner] = it["by_owner"].get(owner, 0.0) + qty
        loc = (r.get("location") or "").strip() or "—"
        it["by_location"][loc] = it["by_location"].get(loc, 0.0) + qty

    out = []
    for it in items.values():
        by_owner = sorted(({"owner_id": o, "qty": q} for o, q in it["by_owner"].items()),
                          key=lambda x: x["qty"], reverse=True)
        by_location = sorted(({"location": l, "qty": q} for l, q in it["by_location"].items()),
                             key=lambda x: x["qty"], reverse=True)
        out.append({
            "item_id": it["item_id"], "name": it["name"], "unit": it["unit"],
            "total": it["total"], "holders": len(it["by_owner"]),
            "by_owner": by_owner, "by_location": by_location,
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return out


def derive_goal_progress(goal: dict, inventory_rows) -> dict:
    """Fill of a procurement goal against its line items (design:
    docs/org-inventory-goals.md).

    `goal` carries `line_items=[{item_id, item_name, unit, qty_needed}]`;
    `inventory_rows` are the contributions earmarked to this goal (the ledger rows
    with `goal_id == goal id`). Returns per-line `{item_id, name, unit, needed,
    have, pct, short}` plus an overall `pct` and a `per_contributor` breakdown.

    The pinned rule: a line's `pct` is `have/needed` capped at 100, but the
    headline `overall_pct` is `total have / total needed` summed *across lines*
    (also capped) — so a goal one line over and one line short doesn't read as
    "done" off the average. `have` per line sums only contributions of that item,
    so a stray contribution of an off-list item doesn't inflate any line.
    `is_met` is true when every line is fully covered."""
    # Sum contributed quantity per item, and total contributed per member.
    have_by_item: dict[str, float] = {}
    by_contributor: dict[str, float] = {}
    for r in inventory_rows or []:
        iid = r.get("item_id")
        qty = _qty(r.get("qty"))
        if iid:
            have_by_item[iid] = have_by_item.get(iid, 0.0) + qty
        owner = r.get("owner_id")
        by_contributor[owner] = by_contributor.get(owner, 0.0) + qty

    lines, total_need, total_have, all_met = [], 0.0, 0.0, True
    for li in (goal.get("line_items") or []):
        iid = li.get("item_id")
        needed = _qty(li.get("qty_needed"))
        have = have_by_item.get(iid, 0.0)
        # A line counts at most its need toward the overall fill, so over-supplying
        # one item can't paper over a shortfall on another.
        total_need += needed
        total_have += min(have, needed) if needed > 0 else have
        pct = 100.0 if needed <= 0 else min(100.0, have / needed * 100.0)
        if needed > 0 and have < needed:
            all_met = False
        line = {
            "item_id": iid,
            "name": li.get("item_name") or iid,
            "unit": li.get("unit"),
            "needed": needed,
            "have": have,
            "pct": round(pct, 1),
            "short": max(0.0, needed - have),
        }
        # Craft goals stamp a target quality per material (recipe minimum max'd
        # with the member's slider asks) — advisory, rides through to the UI badge.
        if li.get("min_q"):
            line["min_q"] = int(li["min_q"])
        lines.append(line)

    overall = 100.0 if total_need <= 0 else min(100.0, total_have / total_need * 100.0)
    per_contributor = sorted(
        ({"owner_id": o, "qty": q} for o, q in by_contributor.items()),
        key=lambda x: x["qty"], reverse=True)
    return {
        "lines": lines,
        "overall_pct": round(overall, 1),
        "is_met": bool(lines) and all_met,
        "per_contributor": per_contributor,
    }


# Smallest raise (aUEC) a new bid must clear over the standing high bid, so the
# next-minimum the UI shows and the endpoint enforces agree.
MIN_BID_INCREMENT = 1


def derive_auction_state(listing: dict, offers, now: datetime) -> dict:
    """Live state of a marketplace listing's bidding (design: docs/marketplace.md).

    `listing` carries `mode`, `start_price`, `buyout_auec`, `ends_at`, `status`;
    `offers` is the child rows ([{id, bidder_id, amount_auec, status, created_at}]).
    Returns the standing high bid + bidder, whether the listing is closed, the
    computed winner (auctions only), and the next minimum acceptable bid.

    Two rules the tests pin:
      * **Tie-break by arrival** — equal aUEC amounts never displace an earlier
        bid, so the first to reach a price holds the lead (bids are scanned oldest
        first and a later equal amount doesn't beat the standing high).
      * **Buyout short-circuit** — if any bid meets `buyout_auec`, the auction is
        closed instantly and won by the *earliest* bid that hit the buyout, even
        before `ends_at`.

    Pure derivation; the endpoints embed it (and lazily flip a closed auction to
    pending/expired on read, like the cargo run's arrival check) so there's no
    background job. Non-auction modes get a usable `bid_count` / `high_bid` too."""
    mode = listing.get("mode")
    status = listing.get("status")
    # A struck or dead listing is closed regardless of the clock.
    terminal = status in ("pending", "completed", "cancelled", "expired")

    # Standing bids: active (or already-accepted) aUEC offers, oldest first so the
    # tie-break favors the earliest bid at any given amount.
    bids = [o for o in (offers or [])
            if o.get("amount_auec") is not None
            and (o.get("status") or "active") in ("active", "accepted")]
    bids.sort(key=lambda o: (o.get("created_at") or "", o.get("id") or 0))

    high = None
    for o in bids:
        # Strictly greater, so a later equal amount can't unseat the earlier bid.
        if high is None or _qty(o.get("amount_auec")) > _qty(high.get("amount_auec")):
            high = o
    high_bid = _qty(high.get("amount_auec")) if high else None
    high_bidder = high.get("bidder_id") if high else None

    buyout = listing.get("buyout_auec")
    buyout = _qty(buyout) if buyout is not None else None
    bought_out, buyout_winner = False, None
    if buyout is not None and buyout > 0:
        for o in bids:                      # oldest first → earliest to hit it wins
            if _qty(o.get("amount_auec")) >= buyout:
                bought_out, buyout_winner = True, o
                break

    ends = _parse_event_dt(listing.get("ends_at"))
    time_up = ends is not None and now >= ends
    is_closed = terminal or bought_out or time_up

    winner_id, winning_amount = None, None
    if is_closed and mode == "auction":
        win = buyout_winner or high
        if win is not None:
            winner_id = win.get("bidder_id")
            winning_amount = _qty(win.get("amount_auec"))

    start = listing.get("start_price")
    start = _qty(start) if start is not None else 0.0
    next_min_bid = high_bid + MIN_BID_INCREMENT if high_bid is not None else start

    return {
        "bid_count": len(bids),
        "high_bid": high_bid,
        "high_bidder": high_bidder,
        "is_closed": is_closed,
        "bought_out": bought_out,
        "time_up": time_up,
        "winner_id": winner_id,
        "winning_amount": winning_amount,
        "next_min_bid": next_min_bid,
        "ends_at": listing.get("ends_at"),
    }


# ---------------------------------------------------------------------------
#
#   BLUEPRINT CRAFT COMMISSIONS (#25)
#
#   Pure helpers over the committed blueprint feed (poi/blueprints.json,
#   distilled by tools/sync_blueprints.py from the SC Wiki API — design:
#   docs/blueprint-craft-commissions.md). A blueprint record:
#     {name, cat, time_s, default, aspects: [
#        {slot, kind: 'resource'|'item', input, scu|qty, min_q?, sel?,
#         mods: [{prop, dir, mode: 'multiplier'|'additive',
#                 ranges: [{q0, q1, v0, v1}]}]}]}
#
# ---------------------------------------------------------------------------


def blueprint_manifest(bp: dict, qty: float = 1) -> dict:
    """The materials bill for crafting `qty` of a blueprint, aggregated across
    both ingredient kinds: resources (ore/refined, SCU) and items (countable
    gems, units). Duplicate inputs across slots sum; a demanded min quality
    max-wins (the crafter has to satisfy the strictest slot). Powers the
    commission manifest panel and the Discord post."""
    qty = max(float(qty or 1), 1.0)
    res: dict[str, dict] = {}
    items: dict[str, dict] = {}
    for a in bp.get("aspects") or []:
        name = a.get("input")
        if not name:
            continue
        min_q = int(a.get("min_q") or 0)
        if a.get("kind") == "item":
            row = items.setdefault(name, {"input": name, "qty": 0, "slots": [], "min_q": 0})
            row["qty"] += (a.get("qty") or 0) * qty
        else:
            row = res.setdefault(name, {"input": name, "scu": 0.0, "slots": [], "min_q": 0})
            row["scu"] = round(row["scu"] + (a.get("scu") or 0.0) * qty, 6)
        row["slots"].append(a.get("slot"))
        row["min_q"] = max(row["min_q"], min_q)
    time_s = bp.get("time_s") or 0
    return {
        "resources": sorted(res.values(), key=lambda r: -r["scu"]),
        "items": sorted(items.values(), key=lambda r: -r["qty"]),
        "time_s": time_s,
        "total_time_s": round(time_s * qty),
        "max_min_q": max([r["min_q"] for r in (*res.values(), *items.values())] or [0]),
    }


def blueprint_goal_lines(bp: dict, qty: float, resolve, input_qs: dict | None = None) -> dict:
    """Turn a blueprint's materials manifest into procurement-goal line items —
    the bridge that lets a member seed a "collect everything to craft X" goal
    straight from a recipe (Resource Manager × the blueprint feed). `resolve`
    is a callback mapping a material name to a catalog item ({item_id, name} or
    None); the app passes commodity-slug resolution. Resource inputs become SCU
    line items, gem/item inputs become "each" counts (the manifest quantity is a
    count, so the unit is fixed by kind, not inherited from the catalog), and
    each line carries the strictest min-quality demanded — the recipe's own
    minimum max'd with the member's per-slot quality asks (`input_qs`, {slot: q}
    from the spec-builder sliders) across every slot consuming that input.
    Advisory, since inventory doesn't track quality. Inputs that don't resolve
    to a catalog item are returned in `unmapped` so the caller can warn instead
    of silently dropping them."""
    man = blueprint_manifest(bp, qty)
    req_by_input: dict[str, int] = {}
    for a in bp.get("aspects") or []:
        q = (input_qs or {}).get(a.get("slot"))
        if a.get("input") and q:
            name = a["input"]
            req_by_input[name] = max(req_by_input.get(name, 0), int(q))
    lines, unmapped = [], []
    for rows, qkey, unit in ((man["resources"], "scu", "SCU"),
                             (man["items"], "qty", "each")):
        for r in rows:
            name = r.get("input")
            item = resolve(name) if name else None
            if not item:
                if name:
                    unmapped.append(name)
                continue
            lines.append({
                "item_id": item["item_id"],
                "item_name": item.get("name") or name,
                "unit": unit,
                "qty_needed": round(float(r.get(qkey) or 0), 6),
                "min_q": max(int(r.get("min_q") or 0), req_by_input.get(name, 0)),
            })
    return {"lines": lines, "unmapped": unmapped}


def blueprint_material_cost(bp: dict, price_of) -> dict:
    """Estimated aUEC cost of ONE craft's materials (#25.1 §12). `price_of` maps
    an input material name to an aUEC-per-SCU reference (or None). Only resource
    (SCU) inputs are priced — gem/item ingredients are counts with no per-unit
    price source yet, so they degrade into `unpriced` alongside resources the
    price feed doesn't know. `total` is None when nothing priced at all. Scales
    linearly with craft count, so callers multiply client-side."""
    man = blueprint_manifest(bp, 1)
    total, priced_any, unpriced = 0.0, False, []
    for r in man["resources"]:
        p = price_of(r.get("input"))
        if p:
            total += float(p) * float(r.get("scu") or 0)
            priced_any = True
        else:
            unpriced.append(r["input"])
    unpriced += [r["input"] for r in man["items"]]
    return {"total": round(total) if priced_any else None, "unpriced": unpriced}


def _mod_extremes(mod: dict) -> tuple[float, float]:
    """The lowest/highest value a modifier can reach across its full quality
    span (piecewise segments included — endpoints are the extremes of a linear
    segment, so scanning endpoints is exact)."""
    vals = [v for r in mod.get("ranges") or [] for v in (r.get("v0"), r.get("v1"))
            if v is not None]
    if not vals:
        return (1.0, 1.0)
    return (min(vals), max(vals))


def blueprint_stat_drivers(bp: dict) -> list[dict]:
    """Invert a blueprint's aspects→modifiers into the spec-builder vocabulary:
    per finished stat, which slot/input drives it and over what effect range
    ("Damage Mitigation ← Shell (Stileron): ×0.85–×1.15"). Where several aspects
    drive one stat, the combined range composes across them — multiplicatively
    for multiplier mods, additively for additive ones (independent sliders)."""
    stats: dict[str, dict] = {}
    for a in bp.get("aspects") or []:
        for m in a.get("mods") or []:
            prop = m.get("prop")
            if not prop:
                continue
            lo, hi = _mod_extremes(m)
            entry = stats.setdefault(prop, {
                "prop": prop, "dir": m.get("dir") or "higher",
                "mode": m.get("mode") or "multiplier", "drivers": []})
            entry["drivers"].append({
                "slot": a.get("slot"), "input": a.get("input"), "kind": a.get("kind"),
                "v_min": lo, "v_max": hi, "mode": m.get("mode") or "multiplier",
                "ranges": m.get("ranges") or []})
    out = []
    for entry in stats.values():
        c_lo, c_hi = None, None
        for d in entry["drivers"]:
            if c_lo is None:
                c_lo, c_hi = d["v_min"], d["v_max"]
            elif d["mode"] == "additive":
                c_lo, c_hi = c_lo + d["v_min"], c_hi + d["v_max"]
            else:
                c_lo, c_hi = c_lo * d["v_min"], c_hi * d["v_max"]
        entry["combined_min"] = round(c_lo, 6)
        entry["combined_max"] = round(c_hi, 6)
        out.append(entry)
    return sorted(out, key=lambda e: e["prop"])


def blueprint_quality_effect(mod: dict, q: float) -> float:
    """A modifier's value at input quality `q` (0–1000): piecewise-linear
    interpolation over its ranges. `q` clamps to the covered span; a gap
    between segments (e.g. 0–500 / 501–1000) resolves to the nearer edge.
    Additive-mode values are integer steps in the data, so they round."""
    ranges = sorted(mod.get("ranges") or [], key=lambda r: (r.get("q0") or 0))
    if not ranges:
        return 1.0
    q = max(min(float(q), ranges[-1].get("q1") or 1000), ranges[0].get("q0") or 0)
    seg = ranges[0]
    for r in ranges:
        q0 = r.get("q0") or 0
        if q0 <= q:
            seg = r
        if q0 <= q <= (r.get("q1") if r.get("q1") is not None else 1000):
            seg = r
            break
    q0, q1 = seg.get("q0") or 0, seg.get("q1") if seg.get("q1") is not None else 1000
    v0, v1 = seg.get("v0"), seg.get("v1")
    if v0 is None or v1 is None:
        return 1.0
    if q1 <= q0:
        val = v1
    else:
        t = (max(min(q, q1), q0) - q0) / (q1 - q0)
        val = v0 + (v1 - v0) * t
    if (mod.get("mode") or "multiplier") == "additive":
        return float(round(val))
    return round(val, 6)


def blueprint_stat_preview(bp: dict, qualities: dict | None = None) -> list[dict]:
    """The expected finished stats at given per-aspect input qualities
    (`{slot: q}`, default 500 = base). Same-stat modifiers across aspects
    compose multiplicatively (multiplier mode) / additively (additive mode) —
    an *estimate*, clearly labeled so in the UI; the app never pretends it can
    verify in-game stats."""
    qualities = qualities or {}
    out = []
    for entry in blueprint_stat_drivers(bp):
        val = None
        for d in entry["drivers"]:
            q = qualities.get(d["slot"], 500)
            eff = blueprint_quality_effect({"mode": d["mode"], "ranges": d["ranges"]}, q)
            if val is None:
                val = eff
            elif d["mode"] == "additive":
                val += eff
            else:
                val *= eff
        out.append({"prop": entry["prop"], "dir": entry["dir"], "mode": entry["mode"],
                    "value": round(val, 6) if val is not None else None})
    return out


def commission_board_state(listing: dict, offers) -> dict:
    """Live quote state of a craft-request listing for its board card / detail
    view (mirrors derive_auction_state's role, much simpler: no tie-breaks or
    winner derivation — the requester picks a quote manually). Best quote =
    the lowest active aUEC amount; a lapsed needed-by date just expires the
    request (no winner), which the endpoint settles lazily on read."""
    quotes = [o for o in (offers or [])
              if o.get("amount_auec") is not None
              and (o.get("status") or "active") in ("active", "accepted")]
    amounts = [_qty(o.get("amount_auec")) for o in quotes]
    budget = listing.get("price_auec")
    return {
        "quote_count": len(quotes),
        "best_quote": min(amounts) if amounts else None,
        "budget": _qty(budget) if budget is not None else None,
        "ends_at": listing.get("ends_at"),
    }


# ---------------------------------------------------------------------------
# Halo Finder (#31) — Aaron Halo QT-drop geometry + planner.
#
# The Aaron Halo is Stanton's ring asteroid belt (between Crusader's and
# ArcCorp's orbits). It has no quantum markers: you QT *through* it between two
# ordinary markers and manually exit partway. This module answers "set
# destination X, jump, exit when the HUD distance-to-destination readout hits
# D" for a chosen density band or a custom POI inside the belt — plus a staging
# leg when no clean direct chord exists, and post-drop classification of the
# next /showlocation fix. Full design: docs/halo-finder.md.
# ---------------------------------------------------------------------------

HALO_SYSTEM = "Stanton"
HALO_ATTRIBUTION = "Band survey: CaptSheppard / Cornerstone — cstone.space"

# Band model from "Aaron Halo — Detailed Shape and Density Survey",
# CaptSheppard / Cornerstone (cstone.space; surveyed 3.16.1, unchanged through
# 4.x). Datum: the Stanton starmap marker == our system origin (0,0,0) — NOT
# the star container, which sits offset ~(0.136, 1.294, 2.923) Gm. Bands are
# origin-centered cylindrical annuli about the z=0 plane. All values meters.
# `peak_m` is the surveyed densest radius; `density` is the relative peak
# density (band 5 ≈ 3× any other) for the picker strip — geometry only, spawn
# data is server-side.
HALO_BANDS = [
    {"band": 1,  "inner_m": 19_673_000e3, "outer_m": 19_715_000e3, "peak_m": 19_702_000e3, "half_height_m": 625e3,   "density": 0.30},
    {"band": 2,  "inner_m": 19_815_000e3, "outer_m": 19_914_000e3, "peak_m": 19_857_000e3, "half_height_m": 2_070e3, "density": 0.30},
    {"band": 3,  "inner_m": 19_914_000e3, "outer_m": 20_071_000e3, "peak_m": 19_995_000e3, "half_height_m": 4_046e3, "density": 0.25},
    {"band": 4,  "inner_m": 20_129_000e3, "outer_m": 20_230_000e3, "peak_m": 20_168_000e3, "half_height_m": 2_912e3, "density": 0.30},
    {"band": 5,  "inner_m": 20_230_000e3, "outer_m": 20_407_000e3, "peak_m": 20_320_000e3, "half_height_m": 4_998e3, "density": 1.00},
    {"band": 6,  "inner_m": 20_407_000e3, "outer_m": 20_540_000e3, "peak_m": 20_471_000e3, "half_height_m": 5_000e3, "density": 0.35},
    {"band": 7,  "inner_m": 20_540_000e3, "outer_m": 20_750_000e3, "peak_m": 20_662_000e3, "half_height_m": 4_998e3, "density": 0.30},
    {"band": 8,  "inner_m": 20_793_000e3, "outer_m": 20_968_000e3, "peak_m": 20_881_000e3, "half_height_m": 3_487e3, "density": 0.25},
    {"band": 9,  "inner_m": 21_046_000e3, "outer_m": 21_132_000e3, "peak_m": 21_082_000e3, "half_height_m": 2_400e3, "density": 0.20},
    {"band": 10, "inner_m": 21_159_000e3, "outer_m": 21_299_000e3, "peak_m": 21_207_000e3, "half_height_m": 2_008e3, "density": 0.25},
]

# The starmap dataset ships a placeholder POI "Aaron Halo - Band" (id 8000)
# sitting at (0,0,0) — never geometry, never a candidate.
HALO_PLACEHOLDER_POI_IDS = frozenset({8000})

# The drop point must sit well before the destination marker's auto-arrival
# deceleration; closer than this and the ship starts braking inside the window.
HALO_DROP_MIN_M = 200_000e3

# Celestial-body obstruction margin: the game refuses QT routes that graze a
# body, so candidate chords are tested against body_radius × this factor.
HALO_BODY_MARGIN = 1.2

# POI mode: a direct chord that already passes within this of the target is
# "good enough" — don't bother scanning staged (T, M) pairs.
HALO_POI_MISS_GOOD_M = 20_000e3


def body_volumes(nav: NavData, system: str, margin: float = HALO_BODY_MARGIN) -> list[dict]:
    """Obstruction volumes for every celestial body in `system` (star, planets,
    moons), shaped like hazard_volumes entries so segment_hits works unchanged.
    Body positions are static in SC, so callers may build these once. Note the
    Stanton star is NOT at the origin — its container position is used."""
    return [{"kind": "sphere", "a": c.pos, "b": None,
             "r": c.body_radius * margin, "warning_id": None,
             "system": c.system, "body": c.name}
            for c in nav.containers.values()
            if c.system == system and c.is_body]


def halo_band(n: int) -> dict:
    """Band row by 1-based band number; ValueError outside 1–10."""
    if not isinstance(n, int) or not 1 <= n <= len(HALO_BANDS):
        raise ValueError(f"unknown halo band {n!r}")
    return HALO_BANDS[n - 1]


def _ring_crossings(p0, p1, r_m: float) -> list[float]:
    """The t values in (0,1) where segment p0->p1 crosses cylindrical radius
    r_m about the origin — a quadratic in the xy components only (the belt is
    an annulus about the z=0 plane; at ≤5,000 km of z over ~20 Gm of radius the
    3D and cylindrical radii agree within ~1 km). Sorted ascending; tangent
    grazes (double roots) return nothing."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    a = dx * dx + dy * dy
    if a == 0.0:
        return []
    b = 2.0 * (p0[0] * dx + p0[1] * dy)
    c = p0[0] * p0[0] + p0[1] * p0[1] - r_m * r_m
    disc = b * b - 4.0 * a * c
    if disc <= 0.0:
        return []
    s = math.sqrt(disc)
    return sorted(t for t in ((-b - s) / (2.0 * a), (-b + s) / (2.0 * a))
                  if 0.0 < t < 1.0)


def _crossing_steepness_deg(p0, p1, pc) -> float:
    """How radially the chord p0->p1 crosses the ring at point pc: 90° = dead
    radial (short window, precise radius control), 0° = tangential graze
    (stretched window, sloppy radius)."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    chord = math.hypot(dx, dy)
    radial = math.hypot(pc[0], pc[1])
    if chord == 0.0 or radial == 0.0:
        return 90.0
    cos_r = abs((dx * pc[0] + dy * pc[1]) / (chord * radial))
    return math.degrees(math.asin(min(1.0, cos_r)))


def halo_band_crossings(p0, p1, band: dict) -> list[dict]:
    """Crossing intervals of `band` along the chord p0->p1, in travel order.

    Roots of the inner/outer radii partition the segment; each sub-interval
    whose midpoint radius lies inside the annulus AND whose ends are within the
    band's half-height is a crossing. Distances are 3D (what the in-game HUD
    distance-to-destination readout shows). Each interval:
      {t_enter, t_peak, t_exit, enter_m, peak_m, exit_m, crossing_xyz,
       star_dist_peak_m, steep_deg}
    where *_m = distance from that point to the destination p1, t_peak is the
    densest-radius crossing (or the radial extremum clipped to the interval
    when the chord never reaches the peak radius), and star_dist_peak_m is the
    drop point's distance to the Stanton origin marker (the patch-proof
    fallback readout)."""
    h = band["half_height_m"]
    d = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])

    def P(t):
        return (p0[0] + t * d[0], p0[1] + t * d[1], p0[2] + t * d[2])

    events = [0.0] + sorted(set(_ring_crossings(p0, p1, band["inner_m"])
                                + _ring_crossings(p0, p1, band["outer_m"]))) + [1.0]
    out = []
    for t0, t1 in zip(events, events[1:]):
        pm = P((t0 + t1) / 2.0)
        if not band["inner_m"] <= math.hypot(pm[0], pm[1]) <= band["outer_m"]:
            continue
        pe, px = P(t0), P(t1)
        if abs(pe[2]) > h or abs(px[2]) > h:
            continue                     # crosses the radius but over/under the rocks
        peak_ts = [t for t in _ring_crossings(p0, p1, band["peak_m"]) if t0 <= t <= t1]
        if peak_ts:
            tp = peak_ts[0]
        else:
            # Chord stays on one side of the densest radius: aim for the radial
            # extremum (xy perigee/apogee), clipped into the interval.
            a2 = d[0] * d[0] + d[1] * d[1]
            t_star = -(p0[0] * d[0] + p0[1] * d[1]) / a2 if a2 else (t0 + t1) / 2.0
            tp = min(max(t_star, t0), t1)
        pp = P(tp)
        out.append({
            "t_enter": t0, "t_peak": tp, "t_exit": t1,
            "enter_m": dist3(pe, p1), "peak_m": dist3(pp, p1), "exit_m": dist3(px, p1),
            "crossing_xyz": pp,
            "star_dist_peak_m": math.dist(pp, (0.0, 0.0, 0.0)),
            "steep_deg": _crossing_steepness_deg(p0, p1, pp),
        })
    return out


def halo_locate(pos) -> dict:
    """Classify a position fix against the band model: inside a band (or at a
    band radius but off-plane), in a void between bands, or outside the belt
    entirely. Pure cylindrical-radius + z lookup — the post-drop verdict and
    the navigator's in-belt chip."""
    r, z = math.hypot(pos[0], pos[1]), pos[2]
    view = {"r_m": r, "z_m": z}
    for b in HALO_BANDS:
        if b["inner_m"] <= r <= b["outer_m"]:
            view.update({
                "status": "band" if abs(z) <= b["half_height_m"] else "band_offplane",
                "band": b["band"],
                "inside_m": r - b["inner_m"],       # radial depth past the inner edge
                "to_outer_m": b["outer_m"] - r,
                "off_peak_m": r - b["peak_m"],      # signed: + = outside the densest radius
                "half_height_m": b["half_height_m"],
            })
            return view
    if r < HALO_BANDS[0]["inner_m"]:
        view.update({"status": "outside", "side": "inward",
                     "near_band": 1, "to_belt_m": HALO_BANDS[0]["inner_m"] - r})
        return view
    if r > HALO_BANDS[-1]["outer_m"]:
        view.update({"status": "outside", "side": "outward",
                     "near_band": HALO_BANDS[-1]["band"],
                     "to_belt_m": r - HALO_BANDS[-1]["outer_m"]})
        return view
    for a, b in zip(HALO_BANDS, HALO_BANDS[1:]):
        if a["outer_m"] < r < b["inner_m"]:
            view.update({"status": "void",
                         "between": [a["band"], b["band"]],
                         "to_inner_band_m": r - a["outer_m"],
                         "to_outer_band_m": b["inner_m"] - r})
            return view
    return view      # unreachable with a well-formed band table


def _seg_sphere_ts(p0, p1, c, r_m: float) -> list[float]:
    """The t values in [0,1] where segment p0->p1 crosses the sphere |P-c|=r_m
    (full 3D — used for the POI-mode drop window). Clipped to the segment."""
    d = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
    f = (p0[0] - c[0], p0[1] - c[1], p0[2] - c[2])
    a = d[0] * d[0] + d[1] * d[1] + d[2] * d[2]
    if a == 0.0:
        return []
    b = 2.0 * (f[0] * d[0] + f[1] * d[1] + f[2] * d[2])
    k = f[0] * f[0] + f[1] * f[1] + f[2] * f[2] - r_m * r_m
    disc = b * b - 4.0 * a * k
    if disc <= 0.0:
        return []
    s = math.sqrt(disc)
    return sorted(min(1.0, max(0.0, t))
                  for t in ((-b - s) / (2.0 * a), (-b + s) / (2.0 * a)))


def _minmax_norm(vals: list[float]) -> list[float]:
    lo, hi = min(vals), max(vals)
    span = hi - lo
    return [(v - lo) / span if span else 0.5 for v in vals]


def _halo_band_candidate(s_pos, marker, m_pos, band: dict, volumes) -> dict | None:
    """One scored-scan candidate: the chord s_pos->marker crossed against a
    band. None when the chord misses the band (radius or z-height), the drop
    would land inside the marker's auto-arrival approach, or a celestial body
    obstructs the plotted route (the game refuses to plot those)."""
    crossings = halo_band_crossings(s_pos, m_pos, band)
    if not crossings:
        return None
    cross = crossings[0]                  # first along the travel direction
    if cross["exit_m"] < HALO_DROP_MIN_M:
        return None
    if volumes and segment_hits(s_pos, m_pos, volumes):
        return None
    plotted_m = dist3(s_pos, m_pos)
    return {
        "marker": marker, "m_pos": m_pos, "cross": cross,
        "second": crossings[1] if len(crossings) > 1 else None,
        "window_m": cross["enter_m"] - cross["exit_m"],
        "flown_m": plotted_m - cross["peak_m"],
        "plotted_m": plotted_m,
    }


def _halo_poi_candidate(s_pos, marker, m_pos, p_star, volumes) -> dict | None:
    """POI-mode candidate: closest approach of the chord s_pos->marker to the
    target point. None when the approach clamps to an endpoint (the chord
    doesn't actually pass the target), the drop point sits too close to the
    marker, or the chord is obstructed."""
    d = (m_pos[0] - s_pos[0], m_pos[1] - s_pos[1], m_pos[2] - s_pos[2])
    L2 = d[0] * d[0] + d[1] * d[1] + d[2] * d[2]
    if L2 == 0.0:
        return None
    t = ((p_star[0] - s_pos[0]) * d[0] + (p_star[1] - s_pos[1]) * d[1]
         + (p_star[2] - s_pos[2]) * d[2]) / L2
    if not 0.0 < t < 1.0:
        return None
    pc = (s_pos[0] + t * d[0], s_pos[1] + t * d[1], s_pos[2] + t * d[2])
    drop_m = dist3(pc, m_pos)
    if drop_m < HALO_DROP_MIN_M:
        return None
    if volumes and segment_hits(s_pos, m_pos, volumes):
        return None
    miss_m = dist3(pc, p_star)
    # Reaction window: the stretch of the chord within (miss + slack) of the
    # target — exit anywhere inside it and you're never much worse than the
    # best-case miss.
    slack = max(miss_m, 25_000e3)
    ts = _seg_sphere_ts(s_pos, m_pos, p_star, miss_m + slack)
    t0, t1 = (ts[0], ts[-1]) if len(ts) >= 2 else (t, t)
    p_in = (s_pos[0] + t0 * d[0], s_pos[1] + t0 * d[1], s_pos[2] + t0 * d[2])
    p_out = (s_pos[0] + t1 * d[0], s_pos[1] + t1 * d[1], s_pos[2] + t1 * d[2])
    return {
        "marker": marker, "m_pos": m_pos, "miss_m": miss_m,
        "cross": {
            "t_enter": t0, "t_peak": t, "t_exit": t1,
            "enter_m": dist3(p_in, m_pos), "peak_m": drop_m,
            "exit_m": dist3(p_out, m_pos), "crossing_xyz": pc,
            "star_dist_peak_m": math.dist(pc, (0.0, 0.0, 0.0)),
            "steep_deg": None,
        },
        "second": None,
        "window_m": dist3(p_in, m_pos) - dist3(p_out, m_pos),
        "flown_m": dist3(s_pos, pc),
        "plotted_m": dist3(s_pos, m_pos),
    }


def _score_halo_band_cands(cands: list[dict], aim: str) -> None:
    """Attach a comparable `score` to each band-mode candidate (min-max
    normalized within this scan). `aim` flips the steepness weight: "peak"
    wants a radial crossing (precise radius control at the densest point),
    "band" wants a long forgiving window."""
    wins = _minmax_norm([c["window_m"] for c in cands])
    flown = _minmax_norm([c["flown_m"] for c in cands])
    steep = _minmax_norm([c["cross"]["steep_deg"] for c in cands])
    for c, w, f, s in zip(cands, wins, flown, steep):
        if aim == "peak":
            c["score"] = 0.5 * s + 0.3 * w + 0.2 * (1.0 - f)
        else:
            c["score"] = 0.6 * w + 0.25 * (1.0 - f) + 0.15 * (1.0 - s)


def _score_halo_poi_cands(cands: list[dict]) -> None:
    """POI-mode score: miss distance dominates (getting within visual/radar
    range IS the product), flown distance breaks ties."""
    miss = _minmax_norm([c["miss_m"] for c in cands])
    flown = _minmax_norm([c["flown_m"] for c in cands])
    for c, m, f in zip(cands, miss, flown):
        c["score"] = 0.8 * (1.0 - m) + 0.2 * (1.0 - f)


def _halo_drop_view(cand: dict, drive_speed_ms=None) -> dict:
    """The wire `drop` block: everything the pilot needs on one card."""
    speed = drive_speed_ms or QT_CRUISE_SPEED_MS
    cross = cand["cross"]
    view = {
        "marker_id": cand["marker"].id, "marker_name": cand["marker"].name,
        "enter_m": cross["enter_m"], "peak_m": cross["peak_m"],
        "exit_m": cross["exit_m"], "window_m": cand["window_m"],
        "window_s": cand["window_m"] / speed if speed else None,
        "star_dist_peak_m": cross["star_dist_peak_m"],
        "crossing_xyz": list(cross["crossing_xyz"]),
        "steep_deg": cross["steep_deg"],
        "plotted_m": cand["plotted_m"], "flown_m": cand["flown_m"],
    }
    if cand.get("miss_m") is not None:
        view["expected_miss_m"] = cand["miss_m"]
    if cand.get("second"):
        s = cand["second"]
        view["second_crossing"] = {"enter_m": s["enter_m"], "peak_m": s["peak_m"],
                                   "exit_m": s["exit_m"]}
    return view


def _halo_drop_leg(from_name: str, cand: dict, fuel_req=None, max_range_m=None) -> dict:
    """The final leg view: jump toward the marker, exit early at the drop
    point. `distance_m` is the distance actually flown (start -> drop);
    `plotted_m` is the full plotted route the drive must accept."""
    leg = {"kind": "drop", "from": from_name, "to": cand["marker"].name,
           "distance_m": cand["flown_m"], "eta_s": _leg_time_s(cand["flown_m"]),
           "plotted_m": cand["plotted_m"]}
    if fuel_req is not None:
        leg["fuel_scu"] = leg_fuel_scu(cand["flown_m"], fuel_req)
        leg["over_range"] = bool(max_range_m is not None
                                 and cand["plotted_m"] > max_range_m)
    return leg


def plan_halo_drop(nav: NavData, *, start, band: int | None = None,
                   target=None, aim: str = "band", markers=None, volumes=None,
                   avoid_poi_ids=None, allow_staging: bool = True,
                   t_ref: float | None = None, drive_speed_ms: float | None = None,
                   fuel_req=None, max_range_m=None, alternates: int = 3) -> dict:
    """Plan a QT drop into the Aaron Halo (#31).

    `start` is any positioned Poi (position_start for a live fix); exactly one
    of `band` (1-10) / `target` (a Poi inside or near the belt) selects the
    goal. Scans candidate destination markers (`markers`, default: the
    system's QT markers), rejecting obstructed chords (`volumes`, see
    body_volumes) and drops inside the auto-arrival approach, then scores by
    drop-window length / flown distance / crossing steepness (`aim` flips the
    steepness preference; POI mode scores by miss distance instead). When no
    direct chord works (`allow_staging`), a staging hop through the cheapest
    viable marker T is planned via the existing travel_cost machinery.

    Returns {start, band|target, aim, legs, drop, alternates, attribution};
    raises ValueError when no viable plan exists or inputs are inconsistent.
    All geometry is time-invariant (nothing in Stanton moves), so plans can be
    computed now and flown later."""
    if (band is None) == (target is None):
        raise ValueError("pick exactly one of band / target")
    t_ref = ROTATION_EPOCH if t_ref is None else t_ref
    s_pos = entity_global_m(nav, start, t_ref)
    if s_pos is None:
        raise ValueError("start position unresolvable")
    band_row = halo_band(band) if band is not None else None
    p_star = None
    if target is not None:
        p_star = entity_global_m(nav, target, t_ref)
        if p_star is None:
            raise ValueError("target position unresolvable")

    avoid = set(avoid_poi_ids or ()) | set(HALO_PLACEHOLDER_POI_IDS)
    if markers is None:
        markers = [p for p in nav.qt_markers if p.system == HALO_SYSTEM]
    cands_from = {}          # marker id -> global pos, resolved once

    def usable(m) -> bool:
        return (m.id not in avoid and m.id != getattr(start, "id", None)
                and (target is None or m.id != target.id))

    def marker_pos(m):
        if m.id not in cands_from:
            cands_from[m.id] = entity_global_m(nav, m, t_ref)
        return cands_from[m.id]

    def scan(from_pos):
        out = []
        for m in markers:
            if not usable(m):
                continue
            m_pos = marker_pos(m)
            if m_pos is None or dist3(from_pos, m_pos) < HALO_DROP_MIN_M:
                continue
            if band_row is not None:
                c = _halo_band_candidate(from_pos, m, m_pos, band_row, volumes)
            else:
                c = _halo_poi_candidate(from_pos, m, m_pos, p_star, volumes)
            if c is not None:
                out.append(c)
        if out:
            if band_row is not None:
                _score_halo_band_cands(out, aim)
            else:
                _score_halo_poi_cands(out)
            out.sort(key=lambda c: -c["score"])
        return out

    direct = scan(s_pos)
    staged, stage_poi, stage_leg = [], None, None
    need_staged = (not direct if band_row is not None else
                   (not direct or direct[0]["miss_m"] > HALO_POI_MISS_GOOD_M))
    if allow_staging and need_staged:
        # Staging markers we can cleanly reach from the start.
        stages = [m for m in markers
                  if usable(m) and marker_pos(m) is not None
                  and dist3(s_pos, marker_pos(m)) > 1.0
                  and not (volumes and segment_hits(s_pos, marker_pos(m), volumes))]
        if band_row is not None:
            # Band mode: nearest-first — the first T whose onward chords cross
            # the band is (near-)cheapest, and band quality barely depends on T.
            for T in sorted(stages, key=lambda m: dist3(s_pos, marker_pos(m))):
                found = [c for c in scan(marker_pos(T)) if c["marker"].id != T.id]
                if found:
                    staged, stage_poi = found, T
                    break
        else:
            # POI mode: the whole point of staging is lining a chord up with
            # the target, so optimize miss over all (T, M) pairs. The pair scan
            # is cheap projections; the (pricier) obstruction test is deferred
            # to the miss-sorted walk below and stops once the best clean T has
            # its alternates.
            pairs = []
            for T in stages:
                t_pos = marker_pos(T)
                for m in markers:
                    if not usable(m) or m.id == T.id:
                        continue
                    m_pos = marker_pos(m)
                    if m_pos is None or dist3(t_pos, m_pos) < HALO_DROP_MIN_M:
                        continue
                    c = _halo_poi_candidate(t_pos, m, m_pos, p_star, None)
                    if c is not None:
                        pairs.append((c, T))
            pairs.sort(key=lambda ct: (ct[0]["miss_m"], ct[0]["flown_m"]))
            for c, T in pairs:
                if stage_poi is not None and T.id != stage_poi.id:
                    continue                 # alternates share the staging hop
                if volumes and segment_hits(marker_pos(T), c["m_pos"], volumes):
                    continue
                stage_poi = T
                staged.append(c)
                if len(staged) > alternates:
                    break
        if staged and stage_poi is not None:
            leg = travel_cost(nav, start, stage_poi, t_ref, avoid=volumes)
            stage_leg = _leg_view(leg, fuel_req, max_range_m)
            stage_leg.update({"kind": "travel", "from": start.name,
                              "to": stage_poi.name})

    # POI mode with both options on the table: staging must *materially* beat
    # the direct miss to justify the extra jump.
    use_staged = bool(staged) and (
        not direct or (band_row is None
                       and staged[0]["miss_m"] < 0.6 * direct[0]["miss_m"]))
    pool = staged if use_staged else direct
    if not pool:
        raise ValueError("no viable drop route from here"
                         + ("" if allow_staging else " (staging disabled)"))

    best, alts = pool[0], pool[1:1 + max(0, alternates)]
    from_name = stage_poi.name if use_staged else start.name
    legs = ([stage_leg] if use_staged else []) + \
        [_halo_drop_leg(from_name, best, fuel_req, max_range_m)]
    plan = {
        "start": {"id": getattr(start, "id", None), "name": start.name},
        "aim": aim if band_row is not None else None,
        "staged": use_staged,
        "legs": legs,
        "drop": _halo_drop_view(best, drive_speed_ms),
        # Full drop + leg views per alternate, so the client can promote one
        # to the main card without a re-plan round trip.
        "alternates": [{"drop": _halo_drop_view(c, drive_speed_ms),
                        "leg": _halo_drop_leg(from_name, c, fuel_req, max_range_m)}
                       for c in alts],
        "attribution": HALO_ATTRIBUTION,
    }
    if band_row is not None:
        plan["band"] = dict(band_row, width_m=band_row["outer_m"] - band_row["inner_m"])
    if target is not None:
        plan["target"] = {"id": target.id, "name": target.name}
    return plan
