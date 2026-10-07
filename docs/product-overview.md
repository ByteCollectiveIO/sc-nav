# SC Nav — product overview

The consolidated "what this product is today" document: the apps, the platform
services underneath them, the data sources, and where the authoritative detail
lives. **Current as of v1.20.1 (2026-10-06).** Update the app map and the
data-source table when those change, and keep history out of it — that's the
[backlog's shipped log](feature-backlog.md#shipped-log).

Companion documents (each owns its concern; don't duplicate them here):
- [`docs/apps/`](apps/README.md) — the player-facing showcase & how-to, one page per app.
- [`PRODUCT.md`](../PRODUCT.md) — users, scope, brand voice, design principles.
- [`DESIGN.md`](../DESIGN.md) — the visual design system (tokens, components, theming).
- [`CLAUDE.md`](../CLAUDE.md) — repo map, code-navigation conventions, guardrails.
- [`implementation-notes.md`](implementation-notes.md) — as-built per-feature detail.
- [`docs/README.md`](README.md) — index of every design doc with its status.
- [`feature-backlog.md`](feature-backlog.md) — what's next / parked / shipped.

## What SC Nav is

A self-hosted **Star Citizen org companion suite**: one FastAPI backend, one
single-file SPA (`server/static/index.html`, hash-routed, no build step — served
as a small shell plus content-hashed CSS/JS), a SQLite database, and a Windows
**watcher** script that reads the game's `Game.log` and forwards the player's
`/showlocation` position, shard, game build, signed-in handle and commodity-kiosk
transactions to the server. Members sign in with Discord OAuth (gated to one
guild); the server pushes live state over WebSocket to a browser on a second
device. Unofficial fan project, strictly non-commercial under CIG's fan-content
rules.

Two release channels: `main` is trunk and runs on our staging server (a few
volunteer testers); `stable` is the release channel the self-hosting orgs track,
fast-forwarded only by the tag-release workflow.

## The apps (launcher map)

Eleven apps in three themed launcher groups, plus account and admin surfaces.

**Out in the 'Verse** — solo tools for a live session:
| App | Route | What it does | Spec |
|---|---|---|---|
| Resource Navigator | `#/nav` | Live position → bearing/distance/ETA to POIs, resource nodes and wildlife; capture resource / harvestable / fauna sightings (resource nodes take a 0–1000 quality and optional quality lines); forecast scoped to the named area you're standing in, element finder, heatmaps; ore value badges; NEARBY pins + mark-mined; shard-aware fresh-only markers; live teammate presence | archived backlog #1–11, [survey-zones-surface.md](survey-zones-surface.md) |
| Cargo Planner | `#/route` | Pickup-and-delivery route solver for hauling contracts (incl. A↔B round trips); jump gates costed as time; quantum fuel + range; run mode with arrival detection; rewards, history, guild hauling boards | [cargo-hauling-planner.md](cargo-hauling-planner.md), [quantum-fuel-range.md](quantum-fuel-range.md) |
| Trade Route Planner | `#/trade` | Multi-leg buy→sell planner on live prices with an org price overlay; stop kinds (dock-only ships, stations & cities), legality, sort by return, return floor, container sizing from kiosk menus, mixed cargo loads; run mode with live replan, mid-run top-up, watcher-reported transactions; stock/demand reports; saved routes; realized-profit history/stats; hazard detours | [trade-route-planner.md](trade-route-planner.md), [trade-topup.md](trade-topup.md), [trade-transaction-capture.md](trade-transaction-capture.md) |
| Prospector | `#/halo` (ATLAS) · `#/halo/field` · `#/halo/drop` | The org's survey atlas: named areas on planets/moons (ore, harvestables and fauna as separate lanes) and in the belts, ranked by value and by survey health; FIELD = RS-signature lookup + survey marks + pocket radar; DROP = quantum-drop planner into unmarked rock space (Aaron Halo bands, Nyx Glaciem pockets, Pyro fields, Keeger) | [survey-app-restructure.md](survey-app-restructure.md), [survey-platform.md](survey-platform.md), [survey-zones-surface.md](survey-zones-surface.md), [belt-survey.md](belt-survey.md), [halo-finder.md](halo-finder.md) |

**Rally the Org** — coordination:
| App | Route | What it does | Spec |
|---|---|---|---|
| Event Planner | `#/events` | Events with roles/targets, capacity + auto-promoting waitlist, mission details, contracts + payout rules; templates (built-in + org, Official flag, history); fleet roster with ship seat templates + manifest; Discord announcement that updates live as signups change, optional banner image | [event-planner.md](event-planner.md), [fleet-roster-squad-organizer.md](fleet-roster-squad-organizer.md), [event-operations.md](event-operations.md) |
| Ops | `#/ops` · `#/ops/<id>` | Run a mission and keep its record: Setup → Live → Settle → Closed; attendance, guests, deputies; ledger → equal split → minimal payment list confirmed by recipients; contracts checklist; verifiable loot rolls (Need/Want/Pass); standalone loot-roll tool; Discord record; participation stats | [event-operations.md](event-operations.md) |
| Group Finder | `#/lfg` | LFG board (looking-for-members / looking-to-join), playstyle tags, suggested matches, promote-to-event, Discord announce; live Who's Online roster | [who-is-online-lfg.md](who-is-online-lfg.md) |
| Danger Board | `#/pirates` | Community pirate warnings (point/lane, players/NPCs, severity, still-active confirms, age-off); feeds hazard volumes into both planners' detour routing (avoid is the default); "organize hunt" → event | [pirate-warnings.md](pirate-warnings.md), [snare-detour-routing.md](snare-detour-routing.md) |

**Run the Org** — logistics & management:
| App | Route | What it does | Spec |
|---|---|---|---|
| Resource Manager | `#/goals` · `#/inventory` · `#/blueprints` | Goals of three kinds (materials, blueprint unlock, survey an area) with pledges and quality floors; holdings ledger with per-lot quality; blueprint library table (craftable from free stock, expected stats, List → market) + org blueprint readiness | [org-inventory-goals.md](org-inventory-goals.md), [inventory-quality.md](inventory-quality.md), [blueprint-library.md](blueprint-library.md), [blueprint-readiness.md](blueprint-readiness.md) |
| Org Marketplace | `#/market` | aUEC-only sale / auction / barter / commission / buy-order board with dual-confirm handshake; crafter storefronts + directed requests; lot quality; list from inventory; org price memory, trends, availability + pickup | [marketplace.md](marketplace.md), [blueprint-craft-commissions.md](blueprint-craft-commissions.md) |
| Org Intel | `#/intel` | Guild analytics: mapping, hauling, trading, surveying, market; leaderboards incl. op attendance; member directory (admin) | [survey-platform.md](survey-platform.md) (Surveying), in-code otherwise |

Also: Who's Online (`#/online`), Settings (`#/settings` — a category rail:
Profile, Watcher, My Ops, Account for everyone; General, Members, Branding,
Discord, Announcements, Apps, Data for admins), Setup guide (`#/setup`), legal
(`#/terms`, `#/privacy`).

## Platform services (cross-cutting, reused by every app)

- **Auth & org gating** — Discord OAuth (`/auth/*`), deny-by-default
  `auth_gate`, persistent `members` table, admin grants, member off-boarding
  that also revokes watcher tokens. Watcher tokens reach only the three
  watcher endpoints. In-game handles bind trust-on-first-use, case-insensitively;
  admins can unbind and merge.
- **Live layer** — one WebSocket (`/ws`) fanning out nav state, teammate
  presence (surface- and shard-aware), the online roster, LFG, warnings, op
  pings and dataset refreshes. In-process `Hub` ⇒ **single worker is mandatory**.
- **Notifications** — `server/notify.py`: per-category Discord incoming
  webhooks (events, ops, marketplace, goals, records, LFG, pirates, survey) as
  embed cards with deep links; paced under Discord's rate limit; delivery
  health shown in settings; per-member "don't @-ping me". Announcement wording
  is admin-editable through slot templates with a server-rendered preview; an
  opt-in org thumbnail and per-event banner links; the new-event post is edited
  in place as signups change. No bot, by decision.
- **Shared item catalog** — commodities, ships, equipment and custom items,
  consumed by Resource Manager and Marketplace; the `blueprint:` namespace
  resolves against the committed blueprint feed.
- **Travel model** — `nav_core.travel_cost`: straight-line quantum legs over
  QT markers, jump gates resolved by name and costed as time, hazard volumes
  with detour waypoints; shared by both planners' solvers and run modes.
- **Survey model** — named zones (belt and surface) whose membership is
  geometric and retroactive; value and survey health computed per read and
  kept separate on purpose; patch staleness from game-build stamps.
- **Analytics pattern** — pure, unit-tested `derive_*` functions in `nav_core`
  feeding `/api/leaderboard`, `/api/stats`, `/api/intel/*` and per-app history.
- **Watcher** — `watcher/sc_nav_watcher.py` on the gaming PC: `/showlocation`
  clipboard parsing, shard + game build + signed-in handle from `Game.log`,
  commodity-kiosk transactions and container menus, position heartbeat,
  optional in-game overlay (light HUD or the full app pinned over the game).
  Stays a Python script (packaging parked).
- **Self-hosting** — Docker image, Portainer git stack, Cloudflare Tunnel; org
  branding (name, logo, copy, MOTD, per-app tile art); admin-only update check
  against GitHub releases (read-only, never self-updates). Runbook:
  [org-deployment-guide.md](org-deployment-guide.md).

## Data sources

| Source | What we take | How it enters | License/terms |
|---|---|---|---|
| starmap.space | POI + container catalog | **vendored snapshot** committed in `poi/`, updated only through `tools/sync_containers.py` (reviewed diff, tombstones for deleted anchors); no runtime fetch | community dataset |
| uexcorp API | commodities, terminal prices + stock, vehicles, equipment | live fetch + cache; scheduled refresh (default 6 h, 2 h floor), `/api/refresh` | permitted w/ attribution |
| SC Wiki API (`api.star-citizen.wiki`) | quantum drives + per-ship fuel/range, blueprints, locations (POIs, QT radii, amenities) | `tools/sync_*.py` per game patch → committed `poi/*.json`; Pyro frame rotation fitted each sync | **CC BY-SA 4.0, attribution required** |
| Strata (CELD) API | ore RS signatures | optional, key-gated; synced per deployment into the data volume, **not committed** (no redistribution license) | attribution rendered where used |
| Game.log (via watcher) | position, shard, game build, handle, kiosk transactions + container menus | `/api/position`, `/api/handle`, `/api/trade/transactions` | player's own client |
| Org members | sightings, survey marks, prices, stock/container reports, warnings | the app itself | org-owned |
| erkul.games | — (rejected) | — | CC BY-NC-**ND** — no derivatives, unusable |

Rule of thumb: third-party reference data is **snapshot-synced and
committed** (or synced per deployment where its license forbids committing);
only UEX prices refresh live, and never on the request path.

## Conventions that keep this maintainable

- **Releases** — SemVer in `server/version.py`; `/deploy` opens a gated release
  PR; merging it auto-tags and fast-forwards `stable` via the `tag-release`
  Action. Merging to `main` deploys staging (Portainer git stack).
- **Docs lifecycle** — a feature gets a design doc in `docs/` before build;
  its Status header and the [docs index](README.md) are updated when it ships;
  as-built detail goes to [implementation-notes.md](implementation-notes.md);
  visible changes update the app's page in [`docs/apps/`](apps/README.md);
  leftovers move to the [backlog](feature-backlog.md).
- **Guardrails** (never regress): deny-by-default auth gate, CSP nonce +
  security headers, WS origin check, image magic-byte sniff, Pydantic input
  caps, rate limits; single-file SPA, no bundler; DESIGN.md tokens; aUEC-only,
  non-commercial. Full list in [CLAUDE.md](../CLAUDE.md).
- **Testing** — pure logic in `nav_core.py` with its own suite; endpoint
  behavior in `test_app.py` (TestClient); run with `SC_NAV_OFFLINE=1`. Don't
  cite test counts in docs — they go stale.
