# SC Nav Server (Ubuntu 26.04)

Receives positions from the Windows clipboard watcher, computes navigation
state (container, lat/lon, bearing, distance, ETA) against the
`poi/containers.json` + `poi/poi.json` dataset, serves the browser UI, and
pushes live updates over WebSocket.

```
watcher (Windows PC) ──POST /api/position──▶ this server ──WS──▶ browser (laptop)
```

## Layout

![SC Nav Server](sc-navigator-routes.png)

```
server/
  nav_core.py        coordinate math (pure stdlib, unit-tested)
  app.py             FastAPI app (REST + WebSocket + static UI)
  static/index.html  browser UI
  test_nav_core.py   tests — run with: python3 test_nav_core.py
  requirements.txt
  deploy/sc-nav.service
```

## Dataset

On startup (and on `POST /api/refresh`) the server fetches the live dataset
from starmap.space:

- containers: https://starmap.space/api/v3/oc/index.php
- POIs: https://starmap.space/api/v3/pois/index.php

A successful fetch is written to the cache folder (`../poi` by default); if
starmap.space is unreachable, the server starts from that cache instead.
`GET /api/health` reports which one you're running on (`"source": "live"` or
`"cache"`). After a game patch moves things, `curl -X POST
http://192.168.1.68:8765/api/refresh` picks up the new data — no restart.

Env overrides: `SC_NAV_DATA` (cache dir), `SC_NAV_OC_URL`, `SC_NAV_POI_URL`,
`SC_NAV_OFFLINE=1` (skip fetching entirely).

## Custom POIs

Your own POIs live in `custom_pois.json` next to the cached dataset —
deliberately a *separate* file, because the upstream files are overwritten on
every live fetch. Custom IDs start at 1,000,000 so they can never collide
with upstream `item_id`s. If starmap.space later adds a POI you created,
just delete your custom copy. In the Docker deployment this file — along with
`resource_nodes.json` and `handles.json` — sits in the `sc-nav-data` volume,
so all user-contributed data survives image rebuilds and dataset refreshes.

Flow (from the web UI): enter a name and type under **ADD CUSTOM POI**, click
**capture next /showlocation**, walk to the spot in game, and run
`/showlocation`. The server converts that position into the parent body's
rotating frame (same storage convention as upstream POIs) and saves it.
Custom POIs are marked with ★ in lists and can be deleted via the ✕ in
search results. See also **Resource nodes** and **Contributor handles** below.

## Deploy option A: Docker (recommended — fits alongside existing containers)

The project root has a `Dockerfile` and `docker-compose.yml`. The image bakes
the repo's poi snapshot into a named volume as seed data, fetches live data
on boot, and runs as a non-root user. Nothing touches other containers; the
only shared resource is host port 8765 (remap the left side of `ports:` in
the compose file if it's taken).

From your Mac:

```bash
rsync -av --exclude server/.venv --exclude __pycache__ \
    ~/Documents/dev/star_citizen/nav_project/ <user>@192.168.1.68:~/sc-nav/
```

On the server (any account in the `docker` group):

```bash
cd ~/sc-nav
docker compose up -d --build
curl http://localhost:8765/api/health   # expect "source": "live"
```

`restart: unless-stopped` keeps it running across reboots. Update after a
code change with `docker compose up -d --build`; update the dataset without
a restart via `curl -X POST http://192.168.1.68:8765/api/refresh`.

### Data persistence & backups

All user data (custom POIs, resource nodes, wildlife, handle registry) lives
in the named Docker volume `sc-nav-data` (mounted at `/data`). It **survives**
a normal redeploy:

```bash
docker compose down            # safe — named volumes are kept
docker compose up -d --build   # new code, data intact
```

The image's seed (`COPY poi/ /data/`) only populates the volume the first time
it's created, so a rebuild never overwrites your data. **The one command that
destroys it is `docker compose down -v`** (the `-v`/`--volumes` flag removes
named volumes) — avoid it unless you intend to wipe everything. (Breadcrumb
trails are the exception — in-memory only, reset on every restart by design.)

Back up the volume any time (do this periodically once you've collected real
finds):

```bash
# backup -> sc-nav-backup.tar.gz in the current directory
docker run --rm -v sc-nav-data:/data -v "$PWD":/backup alpine \
    tar czf /backup/sc-nav-backup.tar.gz -C /data .

# restore into the volume (stop the app first so it's not writing)
docker compose down
docker run --rm -v sc-nav-data:/data -v "$PWD":/backup alpine \
    sh -c "rm -rf /data/* && tar xzf /backup/sc-nav-backup.tar.gz -C /data"
docker compose up -d
```

## Deploy option B: bare systemd service

From your Mac, copy the project over (the `.venv` here is local — exclude it):

```bash
rsync -av --exclude .venv --exclude __pycache__ \
    ~/Documents/dev/star_citizen/nav_project/ jeremiah@192.168.1.68:/tmp/sc-nav/
```

On the server:

```bash
sudo apt update && sudo apt install -y python3-venv
sudo mkdir -p /opt/sc-nav && sudo cp -r /tmp/sc-nav/{server,poi} /opt/sc-nav/
sudo useradd --system --home /opt/sc-nav --shell /usr/sbin/nologin scnav || true

cd /opt/sc-nav/server
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo chown -R scnav:scnav /opt/sc-nav

# verify before installing the service
sudo -u scnav .venv/bin/python test_nav_core.py
sudo -u scnav .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8765   # Ctrl-C after checking
```

Sanity check from another machine: `curl http://192.168.1.68:8765/api/health`
should return `{"ok":true,"containers":496,"pois":1885,...}`.

Install as a service:

```bash
sudo cp deploy/sc-nav.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sc-nav
systemctl status sc-nav
```

If ufw is active, allow LAN access only:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp
```

(Only needed for option B — Docker-published ports bypass ufw via its own
iptables rules, so option A is reachable on the LAN without a ufw rule. To
restrict the Docker port to one interface/IP instead, bind it explicitly in
the compose file, e.g. `"192.168.1.68:8765:8765"`.)

NTP matters: planet rotation is computed from wall-clock time, so keep
`timedatectl` showing "System clock synchronized: yes" (default on Ubuntu).

## Connect the pieces

- **Gaming PC**: in `watcher/run_watcher.bat`, set `SERVER=http://192.168.1.68:8765`
  and (optionally) `HANDLE=YourInGameName` so your captures are attributed.
- **Laptop**: open `http://192.168.1.68:8765` — live readouts appear after the
  first in-game `/showlocation`.

## Contributor handles & attribution

The watcher stamps each position with the player's handle (`--handle`, or the
`HANDLE` var in `run_watcher.bat`; it's remembered in `watcher_config.json`
after the first run). The server keeps `handles.json`, a registry that assigns
each handle a stable **PlayerID** — and it's that PlayerID, not the raw handle,
that's recorded on captured POIs and nodes, so a character rename keeps a
contributor's history intact. When friends each run their own watcher with
their own handle, attribution is automatic. The UI shows who each entry is
"by" and lets you filter the Nearby table by contributor.

## Observations (resource nodes + wildlife)

User-recorded *observations* are an **append-only log** — because the things
they record are ephemeral and respawn, each capture is a sighting, not an
editable entity, which is what makes later clustering / heatmap analysis
possible. Observations share one capture/store/search/summary path keyed by a
`category` (adding a category is one entry in `nav_core.OBSERVATION_CATEGORIES`
plus a capture endpoint — no new store/search/summary code):

- **resource** (`resource_nodes.json`): ore, band 1–8, quality auto-derived
  from band (1 Lowest, 2–4 Low-Mid, 5–6 Good/High, 7 Very High, 8 Perfect).
  Band may also be **Unk** (the default) — you can't know quality until a node
  is mined — which stores band `null` and quality `"Unk"`. The ore datalist is
  populated from the uexcorp commodities API (`is_raw == 1`), fetched on
  startup and cached to `commodities.json` (falls back to the cache offline).
- **wildlife** (`wildlife.json`): species; no quality.

Both also record position, auto-captured altitude, optional biome/note, and
the contributor. Capture mirrors POIs (Add … panel → arm → `/showlocation`).
The Nearby table and map combine POIs + observations with an All / POIs /
Resources / Wildlife filter. Observation IDs share one range (≥ 2,000,000).

## Nearest QT marker ("Jump to")

Every POI / resource / wildlife row shows the nearest jumpable **QT marker**
(POIs with `QTMarker == 1`) **and the distance to it** — the place to
quantum-jump to in order to reach it from space, and how far the target is from
that marker. It prefers a marker on the same body (compared in the rotation-
invariant local frame) and falls back to the nearest QT marker elsewhere in the
system. A POI that is itself a QT marker shows its own name (highlighted, 0 m).
Name + distance are precomputed at load and on `POST /api/refresh` (and on each
capture), so the per-frame nav path stays cheap.

## Breadcrumb trail + map

The UI has a north-up local map (no terrain — a metric grid centered on you)
showing your position + heading, logged POIs/resources/wildlife as toggleable
layers, and a breadcrumb trail. **Start / Stop / Clear Path** control tracking;
while on, each `/showlocation` drops a crumb if you're inside a planet/moon
container and have moved ≥ 250 m since the last (gated off in space). The trail
is **in-memory and session-scoped** — it's not persisted and is lost on a
server restart, by design. Crumbs are capped at 5000 points.

## API

| Route | Purpose |
|---|---|
| `POST /api/position` | watcher ingest: `{"x","y","z","handle"}` meters, system frame |
| `GET /api/state` | latest nav state (`nearest_pois`, `nearest_observations`, `path`, `tracking`, destination, capture) |
| `GET /api/pois?q=&system=&container=&type=&owner_id=&limit=` | POI search |
| `GET /api/observations?q=&category=&system=&container=&type=&owner_id=&limit=` | observation search (resource/wildlife) |
| `GET /api/handles` | contributor registry (handle → PlayerID) |
| `GET /api/raw_commodities` | raw-ore names (uexcorp `is_raw==1`) for the ore datalist |
| `POST /api/destination {"poi_id": N}` / `DELETE /api/destination` | set/clear destination (POI or observation id) |
| `POST /api/capture/start {"name","type"}` | arm custom-POI capture |
| `POST /api/capture/node {"ore","band","biome","note"}` | arm resource-node capture |
| `POST /api/capture/wildlife {"species","biome","note"}` | arm wildlife capture |
| `POST /api/capture/cancel` | cancel armed capture |
| `POST /api/path/{start,stop,clear}` | breadcrumb tracking control |
| `GET /api/custom_pois` / `DELETE /api/custom_pois/{id}` | list/delete custom POIs |
| `DELETE /api/observations/{id}` | delete an observation (resource or wildlife) |
| `WS /ws` | state pushed on connect and on every update |
| `POST /api/refresh` | re-fetch dataset from starmap.space |
| `GET /api/health` | liveness + dataset counts + data source |

## Calibrating rotation (first real-world test)

Everything was verified against the dataset itself except the **rotation
epoch**, which can only be checked in game. The procedure:

1. Land/stand at a well-known POI on a *rotating* body (e.g. Shubin SCD-1 on
   Daymar — Daymar rotates once per 2.48 h, so errors show up fast).
2. Run `/showlocation` and look at the UI's "nearest POIs".
3. **Correct**: the POI you're standing at shows ~0 km away. **Wrong epoch**:
   it shows km/hundreds-of-km away with latitude correct but longitude off —
   the offset is the rotation phase error.
4. Tune `ROTATION_EPOCH` / `ROTATION_SIGN` at the top of `nav_core.py` until
   the error vanishes. The relationship: 1° of longitude error on Daymar =
   rotation_speed × 3600 / 360 ≈ 24.8 s of epoch error.

Tidally-locked bodies (`RotationSpeedX = 0`) are immune — if nearest-POI looks
right there but wrong on Daymar, it's definitely the epoch.

