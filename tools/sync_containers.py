#!/usr/bin/env python3
"""sync_containers.py — reviewed re-sync of the starmap.space snapshots.

The container catalog (rotation params, grid radii — the physics the
rotating-frame position math runs on) and the optional starmap POI catalog
are VENDORED: the server never fetches them at runtime, it reads only the
committed files (2026-08-07 decision — a mid-flight upstream revision deleted
station containers, relocated belt segments 33 Gm, and silently overwrote
every deployment's cache; see CLAUDE.md Nav/live and the ghost-anchor
machinery). This tool is the only path new upstream data takes into the app,
and its diff report is what a human reviews before committing:

    poi/containers.json            — the container catalog the server loads
    poi/poi.json                   — the starmap POI catalog (org opt-in)
    poi/container_tombstones.json  — containers upstream deleted/renamed that
                                     stored org data may still be anchored to;
                                     loaded ONLY for ghost-anchor resolution
                                     (nav_core.register_ghost_containers),
                                     never for navigation/detection
    poi/containers_sync_report.txt — the diff/audit report (NOT loaded)

Run OFFLINE, manually, once per CIG patch (or when upstream announces a
revision); review the report + git diff; commit the regenerated files.

Tombstone rules: an old container whose (system, folded-name) key is absent
from the new snapshot moves to the tombstones file with its full geometry —
`nav_core.container_name_key` folding means a pure respelling ('ARC-L1' ->
'ARC L1') needs no tombstone, while a true rename ('Jumppoint_Nyx_Castra' ->
'Nyx - Castra Jump Point') and a deletion ('Wide Forest Station') both get
one. A tombstone whose key reappears upstream is pruned. Station-class
deletions are flagged loudly: those are core navigation anchors, and losing
one upstream is more often their regression than the game's truth.

Usage:
    python3 tools/sync_containers.py            # fetch + write + report
    python3 tools/sync_containers.py --dry-run  # fetch + report, write nothing
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POI = os.path.join(REPO, "poi")
sys.path.insert(0, os.path.join(REPO, "server"))
from nav_core import container_name_key  # noqa: E402  (the SAME fold the server resolves with)

OC_URL = os.environ.get("SC_NAV_OC_URL", "https://starmap.space/api/v3/oc/index.php")
POI_URL = os.environ.get("SC_NAV_POI_URL", "https://starmap.space/api/v3/pois/index.php")
UA = "sc-nav-project/1.0 (container data sync; +github.com/bytecollective)"

# Mirror of nav_core._STATION_CONTAINER_TYPES (kept literal so the tool runs
# even against an older checkout): dockable anchors whose loss is loud.
STATION_TYPES = {"RestStop", "Refinery Station", "Naval Station",
                 "AsteroidBase", "Jumppoint"}

# Refuse to adopt an implausibly small dataset outright — the old runtime
# guard, now enforced at review time instead of boot time.
MIN_CONTAINERS = 50
MIN_POIS = 100

# A container whose position shifted by more than this is reported as MOVED
# (patches nudge things; a 33 Gm teleport like the 2026-08 Keeger relocation
# deserves eyes).
MOVED_REPORT_M = 50e6


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def key_of(row):
    return (row.get("System"), container_name_key(row.get("ObjectContainer") or ""))


def pos_of(row):
    return (float(row.get("XCoord") or 0), float(row.get("YCoord") or 0),
            float(row.get("ZCoord") or 0))


def dist(a, b):
    return math.dist(a, b)


def name_of(row):
    return f"{(row.get('ObjectContainer') or '').strip()} [{row.get('Type')}] ({row.get('System')})"


def diff_containers(old, new, tombstones):
    """Classify new-vs-old and produce (report_lines, new_tombstones)."""
    old_by_key, new_by_key = {}, {}
    for r in old:
        old_by_key.setdefault(key_of(r), r)
    for r in new:
        new_by_key.setdefault(key_of(r), r)

    lines = []
    added = [r for k, r in new_by_key.items() if k not in old_by_key]
    deleted = [r for k, r in old_by_key.items() if k not in new_by_key]
    renamed, moved = [], []
    for k, nr in new_by_key.items():
        orow = old_by_key.get(k)
        if orow is None:
            continue
        if (orow.get("ObjectContainer") or "") != (nr.get("ObjectContainer") or ""):
            renamed.append((orow, nr))
        d = dist(pos_of(orow), pos_of(nr))
        if d > MOVED_REPORT_M:
            moved.append((orow, nr, d))

    lines.append(f"containers: {len(old)} committed -> {len(new)} fetched")
    for label, rows in (("ADDED", added), ("DELETED", deleted)):
        lines.append(f"{label}: {len(rows)}")
        for r in sorted(rows, key=name_of):
            flag = "  ⚠ STATION-CLASS" if r.get("Type") in STATION_TYPES else ""
            lines.append(f"  {label.lower()}: {name_of(r)}{flag}")
    lines.append(f"RENAMED (same folded key — resolves without a tombstone): {len(renamed)}")
    for orow, nr in sorted(renamed, key=lambda p: name_of(p[0])):
        lines.append(f"  '{(orow.get('ObjectContainer') or '').strip()}' -> "
                     f"'{(nr.get('ObjectContainer') or '').strip()}' ({nr.get('System')})")
    lines.append(f"MOVED > {MOVED_REPORT_M/1e6:.0f},000 km: {len(moved)}")
    for orow, nr, d in sorted(moved, key=lambda p: -p[2]):
        lines.append(f"  {name_of(nr)}: {d/1e9:.2f} Gm")

    # Tombstones: carry forward the still-dead, add the newly deleted, prune
    # anything upstream restored.
    stamp = time.strftime("%Y-%m-%d")
    kept = [t for t in tombstones if key_of(t) not in new_by_key]
    pruned = [t for t in tombstones if key_of(t) in new_by_key]
    fresh = [{**r, "_tombstoned": stamp} for r in deleted]
    for t in pruned:
        lines.append(f"tombstone pruned (upstream restored): {name_of(t)}")
    for t in fresh:
        lines.append(f"tombstoned: {name_of(t)}")
    lines.append(f"tombstones: {len(tombstones)} -> {len(kept) + len(fresh)}")
    return lines, kept + fresh


def diff_pois(old, new):
    old_ids = {r.get("item_id"): r for r in old}
    new_ids = {r.get("item_id"): r for r in new}
    lines = [f"starmap POIs: {len(old)} committed -> {len(new)} fetched",
             f"  ids added: {len(new_ids.keys() - old_ids.keys())}, "
             f"ids removed: {len(old_ids.keys() - new_ids.keys())}"]
    # Renumbering canary: jump-lane + crosswalk logic references stable ids.
    for r in old:
        if r.get("item_id") in new_ids:
            nr = new_ids[r["item_id"]]
            if (nr.get("PoiName") or "") != (r.get("PoiName") or ""):
                lines.append(f"  id {r['item_id']} renamed: '{r.get('PoiName')}' "
                             f"-> '{nr.get('PoiName')}'")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    oc_new = fetch(OC_URL)
    poi_new = fetch(POI_URL)
    if len(oc_new) < MIN_CONTAINERS or len(poi_new) < MIN_POIS:
        print(f"REFUSED: implausibly small dataset ({len(oc_new)} containers, "
              f"{len(poi_new)} POIs) — nothing written")
        return 1

    oc_old = read_json(os.path.join(POI, "containers.json"), [])
    poi_old = read_json(os.path.join(POI, "poi.json"), [])
    tombs = read_json(os.path.join(POI, "container_tombstones.json"), [])

    lines, tombs_out = diff_containers(oc_old, oc_new, tombs)
    lines += diff_pois(poi_old, poi_new)
    report = "\n".join([f"containers sync {time.strftime('%Y-%m-%d %H:%M')} — {OC_URL}",
                        ""] + lines) + "\n"
    print(report)

    if args.dry_run:
        print("(dry run — nothing written)")
        return 0
    with open(os.path.join(POI, "containers.json"), "w") as f:
        json.dump(oc_new, f)
    with open(os.path.join(POI, "poi.json"), "w") as f:
        json.dump(poi_new, f)
    with open(os.path.join(POI, "container_tombstones.json"), "w") as f:
        json.dump(tombs_out, f)
    with open(os.path.join(POI, "containers_sync_report.txt"), "w") as f:
        f.write(report)
    print("wrote poi/containers.json, poi/poi.json, poi/container_tombstones.json")
    print("review the report + git diff, run the suites, then commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
