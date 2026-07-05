# SC Nav

A self-hosted **Star Citizen org companion suite**. A watcher on the gaming PC
forwards your in-game `/showlocation` position (and shard) to a small server,
which computes container, lat/lon, altitude, and bearing/distance to any POI or
recorded resource node and pushes it live (WebSocket) to a browser on a second
device. Around that navigator core has grown a nine-app suite — cargo and trade
route planners, event planner with fleet rosters, group finder, pirate danger
board, org inventory/goals, an aUEC marketplace, and guild analytics — behind
Discord-OAuth org gating. Unofficial fan project, not affiliated with Cloud
Imperium Games; strictly non-commercial.

![SC Nav Server](server/sc-navigator-routes.png)

## Layout (one repo, cloned to both machines)

```
watcher/   Windows gaming PC: reads /showlocation from the clipboard, POSTs it
server/    Linux box (or Docker): FastAPI + SQLite, REST + WebSocket, the SPA
poi/       dataset cache/seed (containers.json, poi.json) + the SQLite volume
docs/      design docs — see docs/README.md for the index
```

The watcher and server share one contract (the `/api/position` payload and the
coordinate conventions), so they live together and version as a unit. Clone the
whole repo to each machine and run only the part that machine needs.

## Quick start

- **Gaming PC** — see [watcher/README.md](watcher/README.md). Set the server
  address in `watcher/run_watcher.bat`, generate a watcher token in the web
  app's Settings, and run it.
- **Server** — see [server/README.md](server/README.md). Docker
  (`docker compose up -d --build`) or systemd; configure Discord OAuth + guild
  id; then open the server URL and sign in with Discord.

## Orientation

- **What the product is** (apps, services, data sources):
  [docs/product-overview.md](docs/product-overview.md)
- **What's next / parked / shipped**:
  [docs/feature-backlog.md](docs/feature-backlog.md)
- **Design system**: [DESIGN.md](DESIGN.md) · **Product scope/brand**:
  [PRODUCT.md](PRODUCT.md) · **Repo/code navigation**: [CLAUDE.md](CLAUDE.md)

## Notes

- The server fetches the live POI dataset from starmap.space on startup and
  caches it into `poi/`; the committed `poi/containers.json` + `poi/poi.json`
  are an offline seed so a fresh clone runs (and tests pass) without network.
  In Docker the cache lives in a named volume, so the repo copy is untouched.
- User/org data (POIs, observations, members, runs, events, listings, …) lives
  in SQLite at `poi/sc_nav.db` (the `/data` volume in Docker) — see
  `server/db.py` for the schema.
- Versioning: SemVer in `server/version.py`, surfaced at `/api/health` and the
  footer. Releases go out via a `/deploy` PR; merging auto-tags.
- Tests: `python3 server/test_nav_core.py`, `python3 server/test_app.py`,
  `python3 watcher/test_parse.py`.
