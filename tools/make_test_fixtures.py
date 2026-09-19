#!/usr/bin/env python3
"""Regenerate the committed UEX feed fixtures used by CI (server/testdata/uex/).

Why this exists
---------------
`server/test_app.py` drives the real FastAPI app, and the app's uexcorp loaders
fetch live with an on-disk cache fallback. Those caches (`poi/commodities.json`,
`poi/ships.json`, `poi/item_catalog.json`, …) are gitignored per-deployment
runtime data, so a fresh CI checkout has none of them — which meant CI was
fetching the live UEX API on every run. On 2026-09-19 a runner lost the network
mid-run (`Connection reset by peer`), the catalogs came back empty, 8 tests
failed, and because `tag-release` is gated on `tests` succeeding on `main`, the
v1.14.3 release silently didn't happen.

So CI now runs the app suite with `SC_NAV_OFFLINE=1` over these fixtures
instead: no network, deterministic, and it exercises the offline path that
self-hosting orgs with `SC_NAV_OFFLINE` set actually run.

What gets kept
--------------
Only what the suite needs, trimmed hard — these are committed, so they should
stay small and stay a *test fixture*, not a vendored copy of someone's dataset:
  * commodities  — all rows (205; needed for ore value tiers across categories),
                   bulky presentation/bookkeeping fields dropped.
  * ships        — spaceships only, same field trim. The quantum tests match
                   these names against the committed `poi/quantum_profiles.json`,
                   so the names have to be the real ones.
  * item_catalog — only the handful of rows the tests actually resolve, plus a
                   small cross-section for shape realism.

Nothing here is authoritative. If a new test needs a feed row that isn't in the
fixture, the app suite fails in CI with an empty-catalog symptom (a 400
"unknown item: …", or a value tier that comes back None) — re-run this script
with a populated cache and commit the result.

Usage
-----
Run it on a box whose `poi/` cache is populated (i.e. the server has started at
least once with network):

    python3 tools/make_test_fixtures.py

Override the source with SC_NAV_DATA, as the server does.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get("SC_NAV_DATA", ROOT / "poi"))
OUT = ROOT / "server" / "testdata" / "uex"

# Dropped from every row: store/photo URLs, wiki links, import bookkeeping and
# the UUIDs. None of it is read by the app; together it is most of the bytes.
DROP_PREFIXES = ("url_", "date_")
DROP_KEYS = {"uuid", "wiki", "screenshot", "notification"}

# Catalog ids the suite resolves by slug. Keep in step with test_app.py; the
# cost of a stale entry here is a confusing 400 in CI, so it is worth a grep
# (`grep -o '"item:[a-z0-9-]*"' server/test_app.py`) when tests are added.
NEEDED_ITEM_SLUGS = {"turbodrive"}
ITEM_SAMPLE_PER_SECTION = 3


def _trim(row: dict) -> dict:
    return {k: v for k, v in row.items()
            if not k.startswith(DROP_PREFIXES) and k not in DROP_KEYS}


def _load(name: str) -> list[dict]:
    path = SRC / name
    if not path.exists():
        sys.exit(f"missing {path} — start the server once with network, or point "
                 f"SC_NAV_DATA at a populated cache")
    rows = json.loads(path.read_text())
    if not rows:
        sys.exit(f"{path} is empty — the cache was written by a failed fetch")
    return rows


def _write(name: str, rows: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    # Compact: these are read by machines only, and they live in git forever.
    path.write_text(json.dumps(rows, separators=(",", ":"), sort_keys=True))
    print(f"{path.relative_to(ROOT)}: {len(rows)} rows, {path.stat().st_size // 1024} KB")


def main() -> None:
    _write("commodities.json", [_trim(r) for r in _load("commodities.json")])

    ships = [_trim(r) for r in _load("ships.json")
             if r.get("is_spaceship") in (1, "1", True)]
    _write("ships.json", ships)

    catalog = _load("item_catalog.json")
    keep, seen = [], {}
    for row in catalog:
        slug = row.get("slug")
        section = row.get("section") or ""
        if slug in NEEDED_ITEM_SLUGS:
            keep.append(row)
        elif seen.get(section, 0) < ITEM_SAMPLE_PER_SECTION:
            seen[section] = seen.get(section, 0) + 1
            keep.append(row)
    missing = NEEDED_ITEM_SLUGS - {r.get("slug") for r in keep}
    if missing:
        sys.exit(f"item_catalog.json has no row for {sorted(missing)} — the feed "
                 f"changed; update NEEDED_ITEM_SLUGS or the test that wants it")
    _write("item_catalog.json", [_trim(r) for r in keep])


if __name__ == "__main__":
    main()
