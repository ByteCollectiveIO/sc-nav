#!/usr/bin/env python3
"""sync_locations.py — offline distill of the SC Wiki locations catalog.

Backlog #28 (starmap & POI enrichment), the last #26 slice. Fetches the SC Wiki
API's locations feed and distills one committed JSON artifact the server loads
at runtime:

    poi/locations.json             — wiki location records (POIs + enrichment)
    poi/locations_sync_report.txt  — coverage/classification audit (NOT loaded)

Run this OFFLINE, manually, once per CIG patch; commit the regenerated JSON.
The server never calls the API — it reads only the committed file.

Every record carries either `local` (body-local rotating-frame km, identical to
the starmap.space convention) or `global` (static system-frame meters), plus
QT radii + amenities used for enrichment even when the POI itself is deduped
against the starmap catalog at load time.

Frame assignment (docs/wiki-poi-enrichment.md §2): wiki x/y/z are system-frame
meters snapshotted at rotation epoch, so for an entity inside a body's rotating
object container, `local = (global - body_center)` reproduces the starmap
body-local frame exactly. Containment is decided GEOMETRICALLY against the
nearest body (the wiki parent chain is unreliable — some surface entities are
parented to a sibling moon), except that orbital stations (Manmade family) are
static in the system frame even inside a planet's grid; comm arrays are the one
Manmade family that rotates with its body.

Source: SC Wiki API (https://api.star-citizen.wiki), public, no auth for game
data, license CC BY-SA 4.0 with attribution. English fields only. Output is
stamped with the game version from /api/game-versions/default.

Usage:
    python3 tools/sync_locations.py            # fetch live + write poi/*.json
    python3 tools/sync_locations.py --dry-run  # fetch + report, write nothing
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://api.star-citizen.wiki"
UA = "sc-nav-project/1.0 (locations data sync; +github.com/bytecollective)"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POI = os.path.join(REPO, "poi")

ATTRIBUTION = "Game data: Star Citizen Wiki (api.star-citizen.wiki), CC BY-SA 4.0"

# Systems to distill: wiki filter value -> starmap.space System casing (the
# server keys containers by that casing, so records must match it).
SYSTEMS = {"stanton": "Stanton", "pyro": "Pyro", "nyx": "Nyx"}

# The container catalog stays starmap-owned (rotation params live there); these
# wiki types are the containers, never distilled as POI records.
BODY_TYPES = {"Planet", "Moon"}
CONTAINER_TYPES = BODY_TYPES | {"Star"}

# Entity names that are engine plumbing, not places.
JUNK_NAME = re.compile(r"^(jumppoint|<=|\W*$)", re.IGNORECASE)

# Manmade-family entities are static in the system frame (orbital stations,
# platforms) even when they sit inside a planet's grid — with one exception:
# comm arrays hold a fixed spot over their body, i.e. they rotate with it.
STATIC_TYPES = {"Manmade", "Manmade_VisibleOnInteraction"}
COMM_ARRAY = re.compile(r"^comm\s+array\b", re.IGNORECASE)
COMM_ARRAY_GRID_FACTOR = 1.5   # 4.8.2 moved some arrays just past the cached grid radius

# Amenities worth shipping: operational facts a planner/run view acts on.
# Everything else (shops, bars, mission givers) stays out of the artifact.
AMENITY_KEEP = re.compile(
    r"^(commodity trading|hangar|landing pad|clinic|docking|refuel|repair"
    r"|vehicle services|cargo center|habitation)", re.IGNORECASE)


# ---------------------------------------------------------------- fetch

def _get(path: str, **params):
    q = urllib.parse.urlencode(params, safe="")
    url = f"{BASE}{path}" + (f"?{q}" if q else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:      # definitive, not transient
                raise
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1}/3 after error: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
        except Exception as e:  # noqa: BLE001 — offline tool, retry transient errors
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1}/3 after error: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def _paged(path: str, **filt):
    out, page = [], 1
    while True:
        params = dict(filt)
        params["page[size]"] = 200
        params["page[number]"] = page
        d = _get(path, **params)
        out.extend(d.get("data", []))
        meta = d.get("meta", {}) or {}
        last = meta.get("last_page") or (meta.get("page", {}) or {}).get("last_page")
        if not last or page >= last:
            break
        page += 1
    return out


# ---------------------------------------------------------------- helpers

def _norm(s: str) -> str:
    return " ".join((s or "").replace('"', "").lower().split())


_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
          "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def _body_name_candidates(wiki_name: str) -> list[str]:
    """Normalized name variants to match a wiki body against a starmap
    container: as-is, plus roman-numeral suffix folded into the stem
    ('Pyro V' -> 'pyro5', starmap's naming)."""
    n = _norm(wiki_name)
    out = [n]
    m = re.match(r"^(.*?)\s+(i{1,3}|iv|v|vi{2,3}|vi|ix|x)$", n)
    if m:
        out.append(m.group(1).replace(" ", "") + _ROMAN[m.group(2)])
    return out


def _dist(a, b) -> float:
    return math.dist((a["x"], a["y"], a["z"]), (b["x"], b["y"], b["z"]))


# ---------------------------------------------------------------- distill

def distill_system(sys_wiki: str, sys_starmap: str, positions: list[dict],
                   detail_by_uuid: dict, containers: list[dict], report: list[str]):
    """Distill one system's positions payload into location records.

    Returns (records, stats). Never dedupes against the starmap POI catalog —
    that happens at load time in the server, so radii/amenity enrichment stays
    available for name-matched starmap POIs and trade terminals.
    """
    by_uuid = {w["uuid"]: w for w in positions}
    conts = {c_norm: c for c in containers if c["System"] == sys_starmap
             for c_norm in [_norm(c["ObjectContainer"])]}

    # wiki bodies -> starmap containers (positions proven identical; we need
    # the starmap radii + canonical container name)
    bodies = []
    for w in positions:
        if w["type"] not in BODY_TYPES:
            continue
        c = next((conts[cand] for cand in _body_name_candidates(w["name"]) if cand in conts), None)
        if c is None:
            report.append(f"  !! {sys_wiki}: wiki body '{w['name']}' has no starmap container — its POIs fall back to static frame")
        bodies.append((w, c))

    stats = {"kept_local": 0, "kept_static": 0, "dropped_hidden": 0, "dropped_junk": 0,
             "dropped_subloc": 0, "dropped_dup": 0, "dropped_container": 0}
    eyeball = []
    records = []
    for w in positions:
        if w["type"] in CONTAINER_TYPES:
            stats["dropped_container"] += 1
            continue
        if w.get("hidden"):
            stats["dropped_hidden"] += 1
            continue
        if w["type"] == "unknown" or JUNK_NAME.search(w["name"] or ""):
            stats["dropped_junk"] += 1
            continue
        # Sub-locations (a station's clinic, an outpost's admin office) are
        # parented under another POI, not a body/star — not routable places of
        # their own, and their frame would be wrong if station-mounted.
        parent = by_uuid.get(w.get("parent_uuid"))
        if parent is not None and parent["type"] not in CONTAINER_TYPES:
            stats["dropped_subloc"] += 1
            continue

        # frame: nearest body, geometric containment
        body, cont, bd = None, None, None
        for b, c in bodies:
            d = _dist(w, b)
            if bd is None or d < bd:
                body, cont, bd = b, c, d
        local = None
        if cont is not None:
            grid = max(float(cont.get("GRIDRadius") or 0),
                       float(cont.get("OrbitalMarkerRadius") or 0),
                       float(cont.get("BodyRadius") or 0) * 1.5)
            is_comm = bool(COMM_ARRAY.match(w["name"] or ""))
            if is_comm:
                grid *= COMM_ARRAY_GRID_FACTOR
            inside = grid > 0 and bd <= grid
            if inside and (is_comm or w["type"] not in STATIC_TYPES):
                local = [round((w["x"] - body["x"]) / 1000, 6),
                         round((w["y"] - body["y"]) / 1000, 6),
                         round((w["z"] - body["z"]) / 1000, 6)]
            elif inside and w["type"] in STATIC_TYPES:
                eyeball.append(f"  {sys_wiki}: '{w['name']}' [{w['type']}] inside "
                               f"{body['name']} grid at {bd/1000:,.0f} km -> kept STATIC (orbital station)")

        detail = detail_by_uuid.get(w["uuid"]) or {}
        qt = detail.get("quantum_travel") or {}
        amen = sorted({a.get("name") for a in (detail.get("amenities") or [])
                       if a.get("name") and AMENITY_KEEP.match(a["name"])})
        rec = {
            "uuid": w["uuid"],
            "name": " ".join((w["name"] or "").split()),
            "system": sys_starmap,
            "container": cont["ObjectContainer"] if (local is not None and cont) else None,
            "local_km": local,
            "global_m": None if local is not None else [round(w["x"], 3), round(w["y"], 3), round(w["z"], 3)],
            "type": w["type"],
            "qt_valid": bool(w.get("qt_valid")),
            "arrival_m": qt.get("arrival_radius") or None,
            "obstruction_m": qt.get("obstruction_radius") or None,
            "adoption_m": qt.get("adoption_radius") or None,
            "amenities": amen,
            "block_travel": bool(detail.get("block_travel")),
            "has_resources": bool(detail.get("has_resources")),
        }
        records.append(rec)
        stats["kept_local" if local is not None else "kept_static"] += 1

    # in-feed duplicate names are ambiguous (generic 'Derelict Outpost' etc.) —
    # drop every copy; a name-keyed record must be unambiguous.
    seen, dupes = {}, set()
    for r in records:
        key = _norm(r["name"])
        if key in seen:
            dupes.add(key)
        seen[key] = r
    if dupes:
        for d in sorted(dupes):
            n = sum(1 for r in records if _norm(r["name"]) == d)
            report.append(f"  {sys_wiki}: dropped in-feed duplicate name '{d}' x{n}")
        stats["dropped_dup"] = sum(1 for r in records if _norm(r["name"]) in dupes)
        records = [r for r in records if _norm(r["name"]) not in dupes]
        stats["kept_local"] = sum(1 for r in records if r["local_km"] is not None)
        stats["kept_static"] = len(records) - stats["kept_local"]

    report.extend(eyeball)
    return records, stats


# ---------------------------------------------------------------- validation

def validate_against_starmap(records: list[dict], report: list[str]) -> None:
    """Frame check against the committed starmap POI cache: every record that
    name-matches a body-local starmap POI on the same container must land
    within a few km (moved-by-CIG outliers are listed, not fatal)."""
    path = os.path.join(POI, "poi.json")
    if not os.path.exists(path):
        report.append("validation skipped: poi/poi.json not present")
        return
    sm = json.load(open(path))
    sm_local = {}
    for p in sm:
        if p.get("Planet") and p["Planet"] != "Space":
            sm_local.setdefault((_norm(p["System"]), _norm(p["PoiName"])), p)
    tested = agree = 0
    outliers = []
    for r in records:
        if r["local_km"] is None:
            continue
        sp = sm_local.get((_norm(r["system"]), _norm(r["name"])))
        if not sp or _norm(sp["Planet"]) != _norm(r["container"]):
            continue
        d = math.dist(r["local_km"], (float(sp["XCoord"]), float(sp["YCoord"]), float(sp["ZCoord"])))
        tested += 1
        if d <= 5:
            agree += 1
        else:
            outliers.append(f"  {r['name']} ({r['container']}): {d:,.0f} km from the starmap record (moved by CIG?)")
    report.append(f"frame validation vs starmap body-local POIs: {agree}/{tested} within 5 km")
    report.extend(outliers)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="fetch + report, write nothing")
    args = ap.parse_args()

    print("game version ...", end=" ", flush=True)
    gv = _get("/api/game-versions/default")["data"]["code"]
    print(gv)

    print("fetching locations catalog ...", end=" ", flush=True)
    detail = _paged("/api/locations")
    detail_by_uuid = {d["uuid"]: d for d in detail if d.get("uuid")}
    print(len(detail))

    positions_by_sys = {}
    for sys_wiki in SYSTEMS:
        print(f"fetching positions {sys_wiki} ...", end=" ", flush=True)
        positions_by_sys[sys_wiki] = _get("/api/locations/positions",
                                          **{"filter[system]": sys_wiki}).get("data", [])
        print(len(positions_by_sys[sys_wiki]))

    # The paginated list always returns `amenities: []` — only the per-uuid
    # detail endpoint populates them. Fetch detail for our systems' QT-valid
    # entities only (amenities matter for routable destinations).
    amen_uuids = sorted({w["uuid"] for ps in positions_by_sys.values() for w in ps
                         if w.get("qt_valid") and not w.get("hidden")
                         and w["type"] not in CONTAINER_TYPES})
    print(f"fetching amenities for {len(amen_uuids)} locations ...", end=" ", flush=True)

    def _detail(uuid):
        try:
            data = _get(f"/api/locations/{uuid}").get("data") or {}
            # a handful of uuids resolve to a disambiguation list; take none
            return uuid, data if isinstance(data, dict) else {}
        except Exception as e:  # noqa: BLE001 — a missing detail record isn't fatal
            print(f"  detail fetch failed for {uuid}: {e}", file=sys.stderr)
            return uuid, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for uuid, full in ex.map(_detail, amen_uuids):
            if full.get("amenities"):
                detail_by_uuid[uuid] = {**detail_by_uuid.get(uuid, {}), "amenities": full["amenities"]}
    print("done")

    containers = json.load(open(os.path.join(POI, "containers.json")))

    report = [f"locations sync report — game {gv}", ""]
    all_records, totals = [], {}
    for sys_wiki, sys_starmap in SYSTEMS.items():
        positions = positions_by_sys[sys_wiki]
        recs, stats = distill_system(sys_wiki, sys_starmap, positions, detail_by_uuid, containers, report)
        all_records.extend(recs)
        for k, v in stats.items():
            totals[k] = totals.get(k, 0) + v
        report.append(f"{sys_wiki}: kept {stats['kept_local']} body-local + {stats['kept_static']} static "
                      f"(dropped: {stats['dropped_hidden']} hidden, {stats['dropped_subloc']} sub-locations, "
                      f"{stats['dropped_junk']} junk, {stats['dropped_dup']} dup-names, "
                      f"{stats['dropped_container']} containers)")

    report.append("")
    validate_against_starmap(all_records, report)

    qt_n = sum(1 for r in all_records if r["qt_valid"])
    radii_n = sum(1 for r in all_records if r["arrival_m"])
    amen_n = sum(1 for r in all_records if r["amenities"])
    report.append("")
    report.append(f"total {len(all_records)} records · {qt_n} qt_valid · "
                  f"{radii_n} with arrival radius · {amen_n} with amenities")

    print("\n" + "\n".join(report[-6:]))

    if args.dry_run:
        print("\n[dry-run] nothing written")
        return

    out = {
        "_meta": {
            "source": "SC Wiki API (api.star-citizen.wiki)",
            "license": "CC BY-SA 4.0",
            "attribution": ATTRIBUTION,
            "game_version": gv,
            "generated_by": "tools/sync_locations.py",
            "record_count": len(all_records),
        },
        "locations": all_records,
    }
    with open(os.path.join(POI, "locations.json"), "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    with open(os.path.join(POI, "locations_sync_report.txt"), "w") as f:
        f.write("\n".join(report) + "\n")
    print(f"\n  wrote poi/locations.json ({len(all_records)} records), poi/locations_sync_report.txt")


if __name__ == "__main__":
    main()
