#!/usr/bin/env python3
"""sync_blueprints.py — offline distill of crafting-blueprint data.

Backlog #26 (SC Wiki API reference-data layer), blueprints slice — feeds the
marketplace craft-commission mode (#25). Fetches the SC Wiki API's blueprint
list + per-blueprint detail and distills one small, committed JSON artifact the
server loads at runtime:

    poi/blueprints.json           — compact per-blueprint records, keyed by the
                                    stable BP_CRAFT_… key
    poi/blueprint_sync_report.txt — coverage audit (build artifact, NOT loaded)

Run this OFFLINE, manually, once per CIG patch; commit the regenerated JSON.
The server never calls the API — it reads only the committed file.

Source: SC Wiki API (https://api.star-citizen.wiki), public, no auth for game
data. Credit api.star-citizen.wiki in public projects (shipped in the site
footer + blueprint UI); English fields only. Output is stamped with the game
version from /api/game-versions/default.

The ~1,559 detail fetches take a few polite minutes; responses are cached under
--cache DIR (keyed by uuid + game version) so a re-run only refetches changes.

Usage:
    python3 tools/sync_blueprints.py                    # fetch live + write poi/
    python3 tools/sync_blueprints.py --dry-run          # fetch + report, write nothing
    python3 tools/sync_blueprints.py --cache /tmp/bpc   # cache detail responses
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://api.star-citizen.wiki"
UA = "sc-nav-project/1.0 (blueprint data sync; +github.com/bytecollective)"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POI = os.path.join(REPO, "poi")

ATTRIBUTION = "Game data: Star Citizen Wiki (api.star-citizen.wiki)"

# Seconds between detail fetches — polite throttle for ~1,559 sequential calls
# against an endpoint with no stated rate limit.
THROTTLE_S = 0.15


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


def _detail(uuid: str, gv: str, cache_dir: str | None):
    """One blueprint's full record, via the cache when available."""
    if cache_dir:
        path = os.path.join(cache_dir, f"{uuid}.{gv}.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    d = _get(f"/api/blueprints/{uuid}")["data"]
    if cache_dir:
        with open(path, "w") as f:
            json.dump(d, f)
        time.sleep(THROTTLE_S)
    else:
        time.sleep(THROTTLE_S)
    return d


# ---------------------------------------------------------------- distill

def _distill_mod(m: dict) -> dict | None:
    """One aspect modifier → {prop, dir, mode, ranges:[{q0,q1,v0,v1}]}.

    `value_segments` (piecewise, e.g. Power Pips / segmented Integrity) wins over
    the flat modifier_range when present. `linear_integer_additive` → mode
    'additive' (step values, better_when still applies); plain 'linear' →
    'multiplier'.
    """
    prop = m.get("label") or m.get("property_key")
    if not prop:
        return None
    vrt = m.get("value_range_type") or "linear"
    mode = "additive" if "additive" in vrt else "multiplier"
    ranges = []
    for seg in m.get("value_segments") or []:
        ranges.append({"q0": seg.get("quality_min"), "q1": seg.get("quality_max"),
                       "v0": seg.get("modifier_at_start"), "v1": seg.get("modifier_at_end")})
    if not ranges:
        qr = m.get("quality_range") or {}
        mr = m.get("modifier_range") or {}
        if mr.get("at_min_quality") is None or mr.get("at_max_quality") is None:
            return None
        ranges = [{"q0": qr.get("min", 0), "q1": qr.get("max", 1000),
                   "v0": mr["at_min_quality"], "v1": mr["at_max_quality"]}]
    return {"prop": prop, "dir": m.get("better_when") or "higher",
            "mode": mode, "ranges": ranges}


def distill(detail: dict) -> dict | None:
    """One API detail record → the compact committed shape (see the design doc,
    docs/blueprint-craft-commissions.md §4). Returns None for records with no
    usable aspects (nothing to craft)."""
    aspects = []
    for a in (detail.get("aspects") or {}).get("aspects") or []:
        inp = a.get("input") or {}
        if not inp.get("name"):
            continue
        rec = {"slot": a.get("name") or a.get("key"),
               "kind": inp.get("kind") or "resource",
               "input": inp["name"]}
        if inp.get("quantity_scu") is not None:
            rec["scu"] = inp["quantity_scu"]
        if inp.get("quantity") is not None:
            rec["qty"] = inp["quantity"]
        if inp.get("min_quality"):
            rec["min_q"] = inp["min_quality"]
        if a.get("selection_group"):
            rec["sel"] = a["selection_group"]
        mods = [dm for m in a.get("modifiers") or [] if (dm := _distill_mod(m))]
        if mods:
            rec["mods"] = mods
        aspects.append(rec)
    if not aspects:
        return None
    out = detail.get("output") or {}
    rec = {
        "uuid": detail.get("uuid"),
        "name": detail.get("output_name"),
        "cat": out.get("type_label") or out.get("type") or "Misc",
        "type": out.get("type"),
        "cls": out.get("class"),
        "time_s": detail.get("craft_time_seconds"),
        "default": bool(detail.get("is_available_by_default")),
        "aspects": aspects,
    }
    unlocks = []
    for g in detail.get("unlocking_missions_grouped") or []:
        for m in g.get("missions") or []:
            pct = round((g.get("chance") or 0) * 100)
            unlocks.append(f"{m.get('title')} ({pct}%)")
    if unlocks:
        rec["unlocks"] = unlocks[:8]
    dis = detail.get("dismantle") or {}
    if dis.get("efficiency"):
        rec["dismantle"] = {"time_s": dis.get("time_seconds"), "eff": dis.get("efficiency")}
    returns = [{"input": r.get("name"), "scu": r.get("quantity_scu")}
               for r in detail.get("dismantle_returns") or [] if r.get("name")]
    if returns:
        rec["returns"] = returns
    return rec


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="fetch + report, write nothing")
    ap.add_argument("--cache", metavar="DIR", help="cache detail responses in DIR")
    args = ap.parse_args()
    if args.cache:
        os.makedirs(args.cache, exist_ok=True)

    print("game version ...", end=" ", flush=True)
    gv = _get("/api/game-versions/default")["data"]["code"]
    print(gv)

    print("fetching blueprint list ...", end=" ", flush=True)
    listing = _paged("/api/blueprints")
    print(len(listing))

    records: dict[str, dict] = {}
    dup_keys, no_aspects, choice_groups = [], [], 0
    t0 = time.time()
    for i, row in enumerate(listing):
        if i and i % 200 == 0:
            rate = i / (time.time() - t0)
            print(f"  detail {i}/{len(listing)}  ({rate:.0f}/s)", flush=True)
        d = _detail(row["uuid"], gv, args.cache)
        key = d.get("key")
        if not key:
            continue
        if key in records:
            dup_keys.append(key)
            continue
        rec = distill(d)
        if rec is None:
            no_aspects.append(key)
            continue
        choice_groups += sum(1 for a in rec["aspects"] if a.get("sel"))
        records[key] = rec

    cats: dict[str, int] = {}
    item_inputs = resource_inputs = 0
    for r in records.values():
        cats[r["cat"]] = cats.get(r["cat"], 0) + 1
        for a in r["aspects"]:
            if a["kind"] == "item":
                item_inputs += 1
            else:
                resource_inputs += 1

    print(f"\n  blueprints: {len(records)}  (dropped {len(no_aspects)} without aspects, "
          f"{len(dup_keys)} duplicate keys)")
    print(f"  aspect inputs: {resource_inputs} resource / {item_inputs} item (gems)")
    print(f"  choice-group aspects: {choice_groups}")
    print("  categories: " + ", ".join(f"{c} {n}" for c, n in sorted(cats.items())))

    if args.dry_run:
        print("\n[dry-run] nothing written")
        return

    out = {
        "_meta": {
            "source": "SC Wiki API (api.star-citizen.wiki)",
            "attribution": ATTRIBUTION,
            "game_version": gv,
            "generated_by": "tools/sync_blueprints.py",
            "blueprint_count": len(records),
        },
        "blueprints": records,
    }
    os.makedirs(POI, exist_ok=True)
    with open(os.path.join(POI, "blueprints.json"), "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, sort_keys=True)

    report = [
        f"blueprint sync report — game {gv}",
        f"blueprints {len(records)} · aspect inputs {resource_inputs} resource / "
        f"{item_inputs} item · choice-group aspects {choice_groups}",
        "",
        "CATEGORIES:",
        *(f"  {c}: {n}" for c, n in sorted(cats.items())),
        "",
        f"DROPPED — no usable aspects ({len(no_aspects)}):",
        *(f"  {k}" for k in sorted(no_aspects)),
        "",
        f"DUPLICATE keys skipped ({len(dup_keys)}):",
        *(f"  {k}" for k in sorted(dup_keys)),
    ]
    with open(os.path.join(POI, "blueprint_sync_report.txt"), "w") as f:
        f.write("\n".join(report) + "\n")

    print("\n  wrote poi/blueprints.json, poi/blueprint_sync_report.txt")


if __name__ == "__main__":
    main()
