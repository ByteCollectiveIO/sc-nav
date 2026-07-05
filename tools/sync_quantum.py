#!/usr/bin/env python3
"""sync_quantum.py — offline distill of quantum-drive fuel/range data.

Backlog #26 (SC Wiki API reference-data layer), vehicles slice. Fetches the SC
Wiki API's vehicle + quantum-drive feeds and distills two small, committed JSON
artifacts the server loads at runtime:

    poi/quantum_drives.json    — the global quantum-drive catalog
    poi/quantum_profiles.json  — per-ship quantum profiles + resolved uexcorp map
    poi/quantum_match_report.txt — coverage audit (build artifact, NOT loaded)

Run this OFFLINE, manually, once per CIG patch; commit the regenerated JSONs.
The server never calls the API — it reads only the committed files.

Source: SC Wiki API (https://api.star-citizen.wiki), public, no auth for game
data, license CC BY-SA 4.0 with attribution. English fields only (other locales
carry a stricter BY-NC-SA license). Output is stamped with the game version from
/api/game-versions/default.

Usage:
    python3 tools/sync_quantum.py            # fetch live + write poi/*.json
    python3 tools/sync_quantum.py --dry-run  # fetch + report, write nothing
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://api.star-citizen.wiki"
UA = "sc-nav-project/1.0 (quantum data sync; +github.com/bytecollective)"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POI = os.path.join(REPO, "poi")

ATTRIBUTION = "Game data: Star Citizen Wiki (api.star-citizen.wiki), CC BY-SA 4.0"

# Curated uexcorp-name -> wiki-slug fixes for hulls the automatic matcher can't
# resolve (variant editions whose quantum hardware is identical to a base hull
# the wiki does carry). Kept small and hand-verified; every entry is logged in
# the match report. Left-hand side is the uexcorp `name_full`.
ALIASES = {
    # e.g. "Some UEX Name": "wiki-slug",
}


# ---------------------------------------------------------------- fetch

def _get(path: str, **params):
    q = urllib.parse.urlencode(params, safe="")
    url = f"{BASE}{path}" + (f"?{q}" if q else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 — offline tool, retry transient errors
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1}/3 after error: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def _paged(path: str, **filt):
    """Fetch every page of a paginated collection (page[size]=200)."""
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


# ---------------------------------------------------------------- distill

def build_drive_catalog(drive_items):
    """{class_name: {name, size, grade, class, fuel_req, drive_speed, cooldown_s, spool_s}}.

    fuel_req is SCU per gigameter (Gm): the wiki's `quantum_fuel_requirement`.
    Identity (verified): ship fuel_capacity / fuel_req == max_range_Gm.
    """
    out = {}
    for d in drive_items:
        cn = d.get("class_name")
        qd = d.get("quantum_drive") or {}
        fr = qd.get("quantum_fuel_requirement")
        if not cn or fr is None:
            continue
        sj = qd.get("standard_jump") or {}
        out[cn] = {
            "name": d.get("name"),
            "size": d.get("size"),
            "grade": d.get("grade"),
            "class": d.get("class"),
            "fuel_req": fr,
            "drive_speed": sj.get("drive_speed"),
            "cooldown_s": sj.get("cooldown_time"),
            "spool_s": sj.get("spool_up_time"),
        }
    return out


def _qd_port(vehicle):
    for p in vehicle.get("ports") or []:
        if p.get("type") == "QuantumDrive" or p.get("name") == "hardpoint_quantum_drive":
            return p
    return None


def build_profiles(vehicles, drives):
    """{slug: profile}. Gated on the ship's `quantum` block (not the QD port), so
    hulls the wiki gives a range for but no equippable port (e.g. Hull C, capital
    ships with TEMP drives) still get a usable stock profile — no fabricated data,
    the numbers come straight from the wiki's own quantum block.
    """
    profiles = {}
    skipped_no_quantum = 0
    for v in vehicles:
        q = v.get("quantum") or {}
        cap = q.get("quantum_fuel_capacity")
        rng = q.get("quantum_range")
        if not cap or not rng:
            skipped_no_quantum += 1
            continue
        slug = v.get("slug")
        port = _qd_port(v)
        default_qd = None
        qd_size = None
        if port:
            default_qd = port.get("class_name") or (port.get("equipped_item") or {}).get("class_name")
            szs = port.get("sizes") or {}
            qd_size = szs.get("max") or (port.get("equipped_item") or {}).get("size")

        # Compatible drives = every catalog drive of the ship's QD size. When the
        # QD size is unknown (no equippable port — e.g. Hull C, capital hulls) we
        # can't verify drive compatibility, so we offer only the stock drive
        # synthesized below rather than fabricating a picker of every drive.
        comp = []
        for cn, dr in drives.items():
            if qd_size is None or dr["size"] != qd_size:
                continue
            range_m = round(cap / dr["fuel_req"] * 1e9)
            comp.append({
                "qd": cn, "name": dr["name"], "fuel_req": dr["fuel_req"],
                "range_m": range_m, "is_default": cn == default_qd,
            })

        # Guarantee a stock default whose range == the wiki's quantum_range. When
        # the equipped drive isn't in the catalog (TEMP/capital) or there's no QD
        # port at all, synthesize it from the quantum block itself.
        default_from_synth = not any(d["is_default"] for d in comp)
        if default_from_synth:
            stock_fuel_req = cap / (rng / 1e9)
            name = "Stock"
            if default_qd and default_qd in drives:
                name = drives[default_qd]["name"]
            comp.append({
                "qd": default_qd or f"STOCK::{slug}",
                "name": name, "fuel_req": round(stock_fuel_req, 8),
                "range_m": round(rng), "is_default": True, "synthetic": True,
            })

        comp.sort(key=lambda d: (not d["is_default"], -d["range_m"]))
        profiles[slug] = {
            "ship_name": v.get("game_name") or v.get("name"),
            "qd_size": qd_size,
            "fuel_scu": cap,
            "default_qd": next((d["qd"] for d in comp if d["is_default"]), None),
            "default_from_synth": default_from_synth,
            "wiki_range_m": round(rng),
            "max_range_m": max((d["range_m"] for d in comp), default=None),
            "drives": comp,
        }
    return profiles, skipped_no_quantum


# ---------------------------------------------------------------- uexcorp match

_EDITIONS = [
    "best in show edition", "pirate edition", "carbon edition", "talus edition",
    "solstice edition", "harmony edition", "fortuna edition", "auspicious edition",
    "liberator edition", "executive edition", "collector edition",
    "wikelo work special", " edition",
]


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _strip_edition(name):
    n = (name or "").lower()
    for e in _EDITIONS:
        n = n.replace(e, "")
    return n.strip()


def build_uexcorp_map(profiles, ships):
    """Resolve each uexcorp cargo-capable hauler's name_full -> profile slug, offline.

    Strategy, first hit wins: exact normalized name; edition-stripped; curated
    alias; progressive right-word strip down to the base hull (variants share the
    same quantum hardware). Returns (uex_map, matched, unmatched, fuzzy).
    """
    by_norm = {}
    for slug, p in profiles.items():
        by_norm.setdefault(_norm(p["ship_name"]), slug)
        by_norm.setdefault(_norm(_strip_edition(p["ship_name"])), slug)
    alias_slugs = set(profiles)

    uex_map = {}
    matched, unmatched, fuzzy = [], [], []
    haulers = [s for s in ships if s.get("is_spaceship") and (s.get("scu") or 0) > 0]
    for s in haulers:
        nf = s["name_full"]
        # 1. exact / edition-strip
        slug = by_norm.get(_norm(nf)) or by_norm.get(_norm(_strip_edition(nf)))
        # 2. curated alias
        if not slug and nf in ALIASES and ALIASES[nf] in alias_slugs:
            slug = ALIASES[nf]
            fuzzy.append(f"alias    {nf}  ->  {slug}")
        # 3. progressive right-word strip (Retaliator Bomber -> Retaliator)
        if not slug:
            words = _strip_edition(nf).split()
            while len(words) > 1 and not slug:
                words = words[:-1]
                cand = by_norm.get(_norm(" ".join(words)))
                if cand:
                    slug = cand
                    fuzzy.append(f"suffix   {nf}  ->  {profiles[slug]['ship_name']} ({slug})")
        if slug:
            uex_map[nf] = slug
            matched.append(nf)
        else:
            unmatched.append(nf)
    return uex_map, matched, unmatched, fuzzy


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="fetch + report, write nothing")
    args = ap.parse_args()

    print("game version ...", end=" ", flush=True)
    gv = _get("/api/game-versions/default")["data"]["code"]
    print(gv)

    print("fetching vehicles ...", end=" ", flush=True)
    vehicles = _paged("/api/vehicles")
    print(len(vehicles))
    print("fetching QuantumDrive catalog ...", end=" ", flush=True)
    drive_items = _paged("/api/vehicle-items", **{"filter[type]": "QuantumDrive"})
    print(len(drive_items))

    drives = build_drive_catalog(drive_items)
    profiles, skipped = build_profiles(vehicles, drives)

    # coverage identity check (default drive range must equal the wiki range)
    bad = [slug for slug, p in profiles.items()
           if p["max_range_m"] and not any(
               d["is_default"] and abs(d["range_m"] - p["wiki_range_m"]) <= p["wiki_range_m"] * 0.01
               for d in p["drives"])]

    ships_path = os.path.join(POI, "ships.json")
    ships = json.load(open(ships_path)) if os.path.exists(ships_path) else []
    uex_map, matched, unmatched, fuzzy = build_uexcorp_map(profiles, ships)

    meta = {
        "source": "SC Wiki API (api.star-citizen.wiki)",
        "license": "CC BY-SA 4.0",
        "attribution": ATTRIBUTION,
        "game_version": gv,
        "generated_by": "tools/sync_quantum.py",
    }
    drives_out = {"_meta": {**meta, "drive_count": len(drives)}, "drives": drives}
    profiles_out = {
        "_meta": {**meta, "profile_count": len(profiles),
                  "uexcorp_matched": len(matched), "uexcorp_total": len(matched) + len(unmatched)},
        "profiles": profiles,
        "uexcorp": uex_map,
    }

    print(f"\n  drives: {len(drives)}  profiles: {len(profiles)}  "
          f"(skipped {skipped} drive-less snubs)")
    print(f"  identity mismatches: {len(bad)}  {bad[:6] if bad else ''}")
    pct = len(matched) * 100 // max(len(matched) + len(unmatched), 1)
    print(f"  uexcorp haulers matched: {len(matched)}/{len(matched) + len(unmatched)} ({pct}%)"
          f"  [{len(fuzzy)} fuzzy]")

    if args.dry_run:
        print("\n[dry-run] nothing written")
        return

    os.makedirs(POI, exist_ok=True)
    with open(os.path.join(POI, "quantum_drives.json"), "w") as f:
        json.dump(drives_out, f, indent=1, ensure_ascii=False)
    with open(os.path.join(POI, "quantum_profiles.json"), "w") as f:
        json.dump(profiles_out, f, indent=1, ensure_ascii=False)

    report = [
        f"quantum match report — game {gv}",
        f"drives {len(drives)} · profiles {len(profiles)} · "
        f"uexcorp {len(matched)}/{len(matched) + len(unmatched)} ({pct}%)",
        "",
        f"FUZZY MATCHES ({len(fuzzy)}) — eyeball these:",
        *(f"  {x}" for x in fuzzy),
        "",
        f"UNMATCHED uexcorp haulers ({len(unmatched)}) — no quantum data in the "
        "wiki for these (mostly concept ships); planner degrades gracefully:",
        *(f"  {x}" for x in sorted(unmatched)),
    ]
    with open(os.path.join(POI, "quantum_match_report.txt"), "w") as f:
        f.write("\n".join(report) + "\n")

    print("\n  wrote poi/quantum_drives.json, poi/quantum_profiles.json, "
          "poi/quantum_match_report.txt")


if __name__ == "__main__":
    main()
