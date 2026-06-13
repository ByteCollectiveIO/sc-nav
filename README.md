# SC Nav

A Star Citizen navigation tool. You run `/showlocation` in game; a watcher on
the gaming PC forwards your coordinates to a small server on your LAN, which
computes your container, lat/lon, altitude, and bearing/distance/ETA to any POI
or recorded resource node, and pushes it live to a browser on a second device.

## Layout (one repo, cloned to both machines)

```
watcher/   Windows gaming PC: reads /showlocation from the clipboard, POSTs it
server/    Linux box (or Docker): math, REST + WebSocket API, browser UI
poi/       dataset cache/seed (containers.json, poi.json) + runtime user data
```

The watcher and server share one contract (the `/api/position` payload and the
coordinate conventions), so they live together and version as a unit. Clone the
whole repo to each machine and run only the part that machine needs — a repo
isn't a deployment unit, and keeping them together means a change that spans
both (they're common) is one commit, never two repos to keep in sync.

## Quick start

- **Gaming PC** — see [watcher/README.md](watcher/README.md). Set the server
  address (and your handle) in `watcher/run_watcher.bat` and run it.
- **Linux server** — see [server/README.md](server/README.md). Docker
  (`docker compose up -d --build`) or a systemd service; then open
  `http://<server>:8765` on your laptop.

## Notes

- The server fetches the live dataset from starmap.space on startup and caches
  it into `poi/`. The committed `poi/containers.json` + `poi/poi.json` are an
  offline seed so a fresh clone runs (and the tests pass) without network. When
  you run the server directly from the repo *online*, the live fetch overwrites
  those two files — `git checkout poi/` to discard the churn. In Docker the
  cache lives in a named volume, so the repo copy is never touched.
- Per-deployment data the server writes (`poi/custom_pois.json`,
  `poi/resource_nodes.json`, `poi/wildlife.json`, `poi/handles.json`,
  `watcher/watcher_config.json`) is gitignored — it's user data, not source.
- Tests: `python3 server/test_nav_core.py` and `python3 watcher/test_parse.py`.
