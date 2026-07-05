# SC Nav — product overview

The consolidated "what this product is today" document. One page per concern:
the apps, the platform services underneath them, the data sources, and where
the authoritative detail lives. Written 2026-07-04 at **v0.36.0**; update the
app map and data-source table when those change, and keep history out of it —
that's the [backlog's shipped log](feature-backlog.md#shipped-log).

Companion documents (each owns its concern; don't duplicate them here):
- [`PRODUCT.md`](../PRODUCT.md) — users, scope, brand voice, design principles.
- [`DESIGN.md`](../DESIGN.md) — the visual design system (tokens, components, theming).
- [`CLAUDE.md`](../CLAUDE.md) — repo map & code-navigation conventions.
- [`docs/README.md`](README.md) — index of every design doc with its status.
- [`feature-backlog.md`](feature-backlog.md) — what's next / parked / shipped.

## What SC Nav is

A self-hosted **Star Citizen org companion suite**: one FastAPI backend, one
single-file SPA (`server/static/index.html`, hash-routed, no build step), a
SQLite database, and a Windows **watcher** script that forwards the player's
in-game `/showlocation` position (and shard) from `Game.log` to the server.
Members sign in with Discord OAuth (gated to one guild); the server pushes live
state over WebSocket to a browser on a second device. Unofficial fan project,
strictly non-commercial under CIG's fan-content rules.

## The apps (launcher map, as of v0.36.0)

Nine apps in three themed launcher groups, plus account/admin surfaces.

**Out in the 'Verse** — solo tools for a live session:
| App | Route | What it does | Spec |
|---|---|---|---|
| Resource Navigator | `#/nav` | Live position → bearing/distance to POIs; observation capture (resources/fauna/harvestables); forecast, element finder, heatmaps; shard-aware, fresh-only markers; teammate map presence | archived backlog #1–11 |
| Cargo Planner | `#/route` | Pickup-and-delivery route solver for hauling contracts; run mode with arrival detection; rewards, history, quick-picks, guild hauling boards | [cargo-hauling-planner.md](cargo-hauling-planner.md) |
| Trade Route Planner | `#/trade` | Buy-low/sell-high multi-leg planner on live UEX terminal prices; run mode with live-position replan; realized-profit history/stats; saved routes; hazard-aware (ignore/warn/avoid + snare detours) | [trade-route-planner.md](trade-route-planner.md) |

**Rally the Org** — coordination:
| App | Route | What it does | Spec |
|---|---|---|---|
| Event Planner | `#/events` | Post events (multi-type, roles/targets), signups, fill tracking; fleet roster: groups/squads/crews with ship seat templates, manifest → Discord | [event-planner.md](event-planner.md), [fleet-roster-squad-organizer.md](fleet-roster-squad-organizer.md) |
| Group Finder | `#/lfg` | LFG board (looking-for-members / looking-to-join), playstyle tags, suggested matches, promote-to-event, Discord announce; SQLite-persisted with green→stale→age-off lifecycle | [who-is-online-lfg.md](who-is-online-lfg.md) |
| Danger Board | `#/pirates` | Community pirate warnings (point/lane, PvP/PvE, severity, still-active confirms, age-off); feeds hazard volumes into both planners' snare-detour routing; "organize hunt" → event | [pirate-warnings.md](pirate-warnings.md), [snare-detour-routing.md](snare-detour-routing.md) |

**Run the Org** — logistics & management:
| App | Route | What it does | Spec |
|---|---|---|---|
| Resource Manager | `#/goals` + `#/inventory` | Shared item catalog; per-member holdings ledger; procurement goals with allocations drawn from holdings; deep-links into the navigator's finder | [org-inventory-goals.md](org-inventory-goals.md) |
| Org Marketplace | `#/market` | aUEC-only sale/auction/barter board with dual-confirm handshake, search/filter/sort, crafted-quality annotations, market-value hints; commission mode planned (#25) | [marketplace.md](marketplace.md) |
| Org Intel | `#/intel` | Guild analytics: leaderboards, capture/hauling/trading stats, member directory (admin) | in-code; backlog #17 for identity |

Also: Who's Online roster (`#/online`, reached via the 🟢 badge), Settings
(`#/settings`: watcher tokens, org settings, branding, notifications), Setup
guide (`#/setup`), legal (`#/terms`, `#/privacy`).

## Platform services (cross-cutting, reused by every app)

- **Auth & org gating** — Discord OAuth (`/auth/*`), one-guild `auth_gate`,
  persistent `members` table upserted at login, primary in-game handle,
  `ADMIN_IDS` + admin grants. Identity rule: new data is keyed
  `owner_id = discord_id` (one legacy tail: observation capture still keys by
  player handle — backlog "Platform" fast-follow).
- **Live layer** — one WebSocket (`/ws`) fanning out frames: nav state,
  teammate presence (surface + shard aware), online roster, `lfg`, `warnings`,
  dataset-refresh. In-process `Hub` ⇒ **single worker is mandatory**.
- **Notifications** — `server/notify.py`: per-category Discord incoming
  webhooks (events, marketplace, goals, records, lfg, pirates), threaded,
  rate-limited, never raises. No bot, by decision.
- **Shared item catalog** — commodities + ships + equipment feeds + custom
  items (`catalog.py`), consumed by Resource Manager, Marketplace, and (soon)
  blueprint commissions.
- **Travel model** — `nav_core.travel_cost`: straight-line QT legs over a
  complete graph of QT markers, 3-system gate chain (Stanton—Pyro—Nyx), hazard
  volumes (sphere/capsule) with detour-waypoint insertion; shared by both
  planners' solvers and run modes.
- **Analytics pattern** — pure, unit-tested `derive_*` functions in `nav_core`
  feeding `/api/leaderboard`, `/api/stats`, `/api/intel/*`, per-app history
  panels.
- **Watcher** — `watcher/sc_nav_watcher.py` on the gaming PC: clipboard
  `/showlocation` parsing, shard from `Game.log`, position heartbeat
  (default 60 s), token auth. Stays a Python script (packaging parked).

## Data sources

| Source | What we take | How it enters | License/terms |
|---|---|---|---|
| starmap.space | POI/container catalog | fetched at startup, cached in `poi/` (committed seed for offline) | community dataset |
| uexcorp API | commodities, terminal prices, vehicles, equipment | fetch + cache, `/api/refresh` | permitted w/ attribution |
| **SC Wiki API** (`api.star-citizen.wiki`) 🆕 | per-ship quantum fuel/range (95% coverage), quantum-drive catalog, blueprints (1,559), starmap x/y/z + QT radii + amenities, commodity metadata | **planned** (#26): sync script → committed `poi/*.json`, game-version-stamped; no live calls | **CC BY-SA 4.0, attribution required; English fields only** |
| Game.log (via watcher) | position, shard id | `/api/position` | player's own client |
| erkul.games | — (rejected) | — | CC BY-NC-**ND** — no derivatives, unusable |

Rule of thumb: reference data is **snapshot-synced and committed**, never
fetched live at runtime from third parties on the request path.

## Conventions that keep this maintainable

- **Releases** — SemVer in `server/version.py`; `/deploy` opens a release PR,
  merge auto-tags via the `tag-release` GitHub Action; deploy = push to
  `origin/main` + manual server rebuild.
- **Docs lifecycle** — a feature gets a design doc in `docs/` *before* build;
  its **Status header** is updated when it ships; open leftovers move to the
  [backlog](feature-backlog.md)'s fast-follows, not new sections in old docs.
  [`docs/README.md`](README.md) is the status index.
- **Guardrails** (never regress): CSP nonce + security headers, host pin, WS
  origin check, image magic-byte sniff, Pydantic input caps; single-file SPA,
  no bundler; DESIGN.md tokens; aUEC-only, non-commercial.
- **Testing** — pure logic lives in `nav_core.py` with its own suite;
  endpoint behavior in `test_app.py` (TestClient). Suite size grows ~weekly;
  don't cite counts in docs (they staled everywhere — grep the CI instead).
