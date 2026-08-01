#!/usr/bin/env python3
"""sync_strata.py — offline distill of the Strata (CELD) public mining API.

Feeds Prospector's RS-signature reference (#38 playtest note 5): every material
has a base radar signature and every contact reads an integer multiple of it,
readable from ~25 km. We already derive `rs_bases` by GCD over the org's own
single-ore scans (`nav_core._survey_scan_stats`) — that table starts EMPTY and
fills only as members happen to scan single-ore rocks. Strata publishes the
datamined per-ore signature outright, so the cheat sheet ships complete and our
GCD-derived values become a cross-check instead of the only source.

Writes two artifacts:

    poi/ore_signatures.json        — per-ore RS + hardness, keyed by OUR ore
                                     name, plus per-belt ore concentration
    poi/strata_sync_report.txt     — crosswalk/coverage audit (NOT loaded)

Run this OFFLINE, manually, once per CIG patch; commit the regenerated JSON.
The server never calls Strata — it reads only the committed file. That is also
what Strata asks for: the data only changes when CIG ships a patch, so cache
aggressively rather than polling.

Source: Strata Mining Tools by Celestial Dynamics [CELD]
(https://strata.celd.space), key-gated public API. A key is self-service and
free at https://strata.celd.space/api-keys (Discord login + verified email +
real name on file). Rate limit 120 requests / 60 s per key; a 429 carries
Retry-After. Strata asks that tools surfacing this data credit "Strata (CELD)"
and link back — that credit ships in the FIELD-tab RS card, don't drop it.

Usage:
    export STRATA_API_KEY=celd_...
    python3 tools/sync_strata.py                 # fetch live + write poi/
    python3 tools/sync_strata.py --dry-run       # fetch + report, write nothing
    python3 tools/sync_strata.py --all-locations # every mineable location, not
                                                 # just the three surveyed belts
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://strata.celd.space"
UA = "sc-nav-project/1.0 (ore signature sync; +github.com/bytecollective)"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POI = os.path.join(REPO, "poi")

ATTRIBUTION = "Ore signatures: Strata (CELD) — strata.celd.space"

# 120 req/60 s per key. We make 5 calls in the default run and ~1 per location
# under --all-locations, so this throttle is courtesy, not necessity.
THROTTLE_S = 0.6

# The belts Prospector actually surveys → the Strata location the ore table
# comes from. Matched on the API's `slug` first, then a normalized display
# name, so a location-id reshuffle across patches doesn't silently drop a belt.
# Keys are ours: "halo" = the Stanton band model (nav_core.HALO_SYSTEM),
# "glaciem"/"keeger" = the HaloPlanIn.belt vocabulary.
BELTS = {
    "halo": ("aaron-halo", "Aaron Halo"),
    "glaciem": ("glaciem-ring", "Glaciem Ring"),
    "keeger": ("keeger-belt", "Keeger Belt"),
}


# ---------------------------------------------------------------- fetch

def _get(path: str, key: str, **params):
    """One authenticated GET, with retry. 429 is honored via Retry-After (the
    documented back-off contract) rather than blind-retried; 401 is fatal and
    says so — a wrong key is a config error, not a transient one."""
    q = urllib.parse.urlencode(params, safe="")
    url = f"{BASE}{path}" + (f"?{q}" if q else "")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json",
        "Authorization": f"Bearer {key}"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                body = e.read().decode("utf-8", "replace")[:200]
                sys.exit(f"[strata] {e.code} from {path}: {body}\n"
                         f"         Check STRATA_API_KEY (get one at {BASE}/api-keys).")
            if e.code == 429 and attempt < 3:
                wait = int(e.headers.get("Retry-After") or 30)
                print(f"  rate limited, sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1}/3 after HTTP {e.code}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
        except Exception as e:  # noqa: BLE001 — offline tool, retry transient errors
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1}/3 after error: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


# ---------------------------------------------------------------- crosswalk

def _slugify(name: str) -> str:
    """Strata's documented slug rule: lowercase, drop parenthetical suffixes,
    non-alphanumerics collapse to '-'. Used as the fallback join key when the
    display names don't match byte-for-byte."""
    s = re.sub(r"\(.*?\)", "", str(name)).lower().strip()
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", s))


def load_local_ores() -> list[str]:
    """Our ore vocabulary — the uexcorp raw-commodity names the survey marks,
    forecast and value badges are all keyed by (app.load_raw_commodity_names).
    Read from the local feed cache so this tool needs no server import.

    That cache is gitignored, so a fresh clone won't have it: start the server
    once (or run with OFFLINE unset) to populate it. Without it every ore lands
    in NOT CARRIED — loud and obvious in the report, never a silent half-sync."""
    path = os.path.join(POI, "commodities.json")
    try:
        with open(path) as f:
            rows = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[strata] no local ore vocabulary at {path} ({exc.__class__.__name__}).\n"
              f"         Start the server once to populate the uexcorp commodities "
              f"cache, then re-run — otherwise nothing maps.", file=sys.stderr)
        return []
    rows = rows.get("data") if isinstance(rows, dict) else rows
    return sorted({r["name"] for r in rows
                   if r.get("is_raw") in (1, "1", True) and r.get("name")})


def build_crosswalk(local: list[str]) -> dict[str, str]:
    """{join key → our ore name}. Both the exact display name and the slug are
    registered: Strata's convention ("Beryl (Raw)", "Iron (Ore)") matches
    uexcorp's almost everywhere, and the slug catches the rest. Slug collisions
    keep the FIRST binding — the API's own docs warn slugs are best-effort."""
    keys: dict[str, str] = {}
    for name in local:
        keys.setdefault(name.lower(), name)
        keys.setdefault(_slugify(name), name)
    return keys


def resolve_ore(row: dict, keys: dict[str, str]) -> str | None:
    """Strata ore row → our ore name, or None when we don't carry it (their
    catalog includes FPS/ground-vehicle gems we may not stock)."""
    for cand in (str(row.get("name") or "").lower(), row.get("slug") or "",
                 _slugify(row.get("name") or "")):
        if cand and cand in keys:
            return keys[cand]
    return None


# ---------------------------------------------------------------- distill

def distill_ore(row: dict) -> dict:
    """One /api/public/ores row → our compact record. `rs` is the field the
    FIELD-tab card reads; instability/resistance ride along because they answer
    'is this rock worth cracking' at the same glance.

    instability/resistance of 0 is REAL data, not missing — the API docs are
    explicit about it, so this null-checks rather than truth-checks."""
    def _num(v):
        return v if isinstance(v, (int, float)) else None
    return {
        "id": row.get("id"),
        "strata_name": row.get("name"),
        "slug": row.get("slug"),
        "tier": _num(row.get("tier")),
        "category": row.get("category"),        # ship | fps | ground_vehicle
        "rs": _num(row.get("scanSignature")),
        "instability": _num(row.get("instability")),
        "resistance": _num(row.get("resistance")),
    }


def distill_location(payload: dict, keys: dict[str, str]) -> dict:
    """One /api/public/location-ores/{id} response → {location…, ores:[…]}.

    `concentration` is an ABSOLUTE share (0–1) of the mining presence at that
    location and `tier` an absolute band on it — the API documents both as
    directly comparable across space/surface/cave/exploration, which is what
    makes this usable as a per-belt prior next to our own survey counts.
    `rankPct` is deliberately NOT carried: it is only meaningful within one
    group, and a bare number in our JSON would invite exactly the cross-group
    comparison the docs warn against."""
    loc = payload.get("location") or {}
    ores = []
    for r in payload.get("ores") or []:
        conc = r.get("concentration")
        ores.append({
            "ore": keys.get(str(r.get("name") or "").lower())
                   or keys.get(_slugify(r.get("name") or ""))
                   or r.get("name"),
            "id": r.get("oreId"),
            "conc": round(conc, 4) if isinstance(conc, (int, float)) else None,
            "tier": r.get("tier"),
            "tier_name": r.get("tierName"),
            "rs": r.get("scanSignature"),
        })
    return {"id": loc.get("id"), "name": loc.get("name"),
            "system": loc.get("system"), "type": loc.get("type"),
            "ores": ores}


def pick_belts(locations: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """Our belt keys → the Strata location row, matched on slug then normalized
    name. Returns (found, missing) so a renamed/removed belt shows up in the
    report as a MISS instead of vanishing from the artifact."""
    by_slug = {str(r.get("slug") or ""): r for r in locations}
    by_name = {_slugify(r.get("name") or ""): r for r in locations}
    found, missing = {}, []
    for key, (slug, name) in BELTS.items():
        row = by_slug.get(slug) or by_name.get(_slugify(name))
        if row:
            found[key] = row
        else:
            missing.append(f"{key} ({name})")
    return found, missing


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + report, write nothing")
    ap.add_argument("--all-locations", action="store_true",
                    help="pull the ore table for EVERY mineable location "
                         "(~1 request each) instead of just the surveyed belts")
    ap.add_argument("--key", default=os.environ.get("STRATA_API_KEY"),
                    help="Strata API key (default: $STRATA_API_KEY)")
    args = ap.parse_args()

    if not args.key:
        sys.exit("[strata] no API key — set STRATA_API_KEY or pass --key.\n"
                 f"         Self-service, free: {BASE}/api-keys")

    local = load_local_ores()
    keys = build_crosswalk(local)
    print(f"local ore vocabulary: {len(local)} names")

    print("fetching ore catalog ...", end=" ", flush=True)
    ores_resp = _get("/api/public/ores", args.key)
    rows = ores_resp.get("ores") or []
    print(f"{len(rows)}")
    time.sleep(THROTTLE_S)

    ores: dict[str, dict] = {}
    unmapped, no_rs, collisions = [], [], []
    for row in rows:
        rec = distill_ore(row)
        name = resolve_ore(row, keys)
        if rec["rs"] is None:
            no_rs.append(rec["strata_name"] or rec["id"])
        if not name:
            unmapped.append(f"{rec['strata_name']} [{rec['category'] or '?'}]")
            continue
        if name in ores:
            collisions.append(f"{name} ← {rec['strata_name']} (kept "
                              f"{ores[name]['strata_name']})")
            continue
        ores[name] = rec

    print(f"  mapped {len(ores)}/{len(rows)} onto our ore names "
          f"({len(unmapped)} not carried, {len(no_rs)} without an RS value)")

    print("fetching location catalog ...", end=" ", flush=True)
    loc_resp = _get("/api/public/locations", args.key)
    locations = loc_resp.get("locations") or []
    print(f"{len(locations)}")
    time.sleep(THROTTLE_S)

    if args.all_locations:
        targets = {str(r.get("id")): r for r in locations}
        missing_belts: list[str] = []
    else:
        picked, missing_belts = pick_belts(locations)
        targets = {k: v for k, v in picked.items()}
    if missing_belts:
        print(f"  !! belt not found in the catalog: {', '.join(missing_belts)}")

    belts: dict[str, dict] = {}
    for i, (key, row) in enumerate(sorted(targets.items())):
        loc_id = row.get("id")
        print(f"  location {i + 1}/{len(targets)}: {row.get('name')}", flush=True)
        payload = _get(f"/api/public/location-ores/{urllib.parse.quote(str(loc_id))}",
                       args.key)
        belts[key] = distill_location(payload, keys)
        time.sleep(THROTTLE_S)

    ore_rows = sum(len(b["ores"]) for b in belts.values())
    print(f"\n  ores {len(ores)} · locations {len(belts)} · ore rows {ore_rows}")

    if args.dry_run:
        print("\n[dry-run] nothing written")
        return

    out = {
        "_meta": {
            "source": "Strata Mining Tools (strata.celd.space) by Celestial Dynamics [CELD]",
            "attribution": ATTRIBUTION,
            "api_attribution": ores_resp.get("attribution"),
            "generated_by": "tools/sync_strata.py",
            "endpoints": ["/api/public/ores", "/api/public/locations",
                          "/api/public/location-ores/{location}"],
            "ore_count": len(ores),
            "location_count": len(belts),
            "scope": "all-locations" if args.all_locations else "surveyed-belts",
        },
        "ores": ores,
        "locations": belts,
    }
    os.makedirs(POI, exist_ok=True)
    with open(os.path.join(POI, "ore_signatures.json"), "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, sort_keys=True)

    report = [
        "strata sync report",
        f"ores {len(ores)} mapped of {len(rows)} · locations {len(belts)} · "
        f"ore rows {ore_rows}",
        "",
        "RS SIGNATURES (our ore name → base RS):",
        *(f"  {n}: {r['rs']}" for n, r in sorted(ores.items())),
        "",
        f"NOT CARRIED — no local ore by that name ({len(unmapped)}):",
        *(f"  {n}" for n in sorted(unmapped)),
        "",
        f"NO RS VALUE from the API ({len(no_rs)}):",
        *(f"  {n}" for n in sorted(no_rs)),
        "",
        f"CROSSWALK COLLISIONS ({len(collisions)}):",
        *(f"  {c}" for c in sorted(collisions)),
        "",
        f"BELTS NOT FOUND ({len(missing_belts)}):",
        *(f"  {b}" for b in sorted(missing_belts)),
    ]
    with open(os.path.join(POI, "strata_sync_report.txt"), "w") as f:
        f.write("\n".join(report) + "\n")

    print("\n  wrote poi/ore_signatures.json, poi/strata_sync_report.txt")


if __name__ == "__main__":
    main()
