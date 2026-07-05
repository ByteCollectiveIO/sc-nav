# CLAUDE.md — repo map & navigation

Purpose of this file: let a fresh session find the right code **without reading
whole files**. Grep the banner conventions below; jump with `Read(offset/limit)`.
Keep this map current when you add a view, endpoint, or table.

## What this is
Star Citizen org tool — nine apps in one SPA: navigator, cargo + trade route
planners, event planner (+ fleet rosters), group finder, danger board, resource
manager, aUEC marketplace, org intel. FastAPI backend, **single-file** SPA, plus
a Windows watcher (Python script) that reports the player's in-game position.

- Backend: `server/app.py` (HTTP/WS + routes), `server/nav_core.py` (pure nav/route
  logic, fully unit-tested), `server/db.py` (SQLite schema + queries).
- Frontend: `server/static/index.html` — ONE file: `<style>` + body + `<script>`.
- Watcher: `watcher/` (runs on the player's Windows box; reads Game.log).
- Version: `server/version.py` (SemVer; surfaced at `/api/health` + footer).
- Tests: `server/test_nav_core.py` + `server/test_app.py`. Deploy = push to
  `origin/main` (see `/deploy`).
- Docs: `docs/README.md` = index w/ per-doc status · `docs/product-overview.md`
  = consolidated app/service/data map · `docs/feature-backlog.md` = active work.

## DO NOT READ (token sinks / generated / binary)
- `server/.venv/**` — dependencies. Never read; never grep here.
- `poi/*.db`, `poi/*.db-wal/-shm` — SQLite binaries. Use `db.py` for schema.
- `poi/*.json` runtime caches (gitignored). The schema is in code, not here.
- `.impeccable/`, `.github/skills/`, `.claude/skills/` — tooling, not app code.

## Navigation conventions (how to find things fast)
Every logical section is marked by a greppable banner. To locate code, grep the
banner — don't scroll.
- **index.html JS**:  `grep -n "// ----" server/static/index.html`
- **index.html CSS**: `grep -n "/* ----" server/static/index.html`
- **index.html views**: `grep -n 'id="[a-z-]*-view"' server/static/index.html`
- **app.py routes**:  `grep -nE "^@app\.(get|post|put|patch|delete)" server/app.py`
- **db tables**:      `grep -n "CREATE TABLE" server/db.py`

## index.html section index (~7594 lines; ranges drift — confirm by grep)
`<style>` lines 7–1341 · body 1344–2410 · `<script>` 2411–7592.

Body views (each a `#…-view` container, hash-routed):
launcher, main (navigator), settings, setup, intel, leaderboard, stats,
cargo-leaderboard, cargo-stats, route (cargo planner), events, goals, inventory,
market, online (who's online, #19), lfg (group finder / LFG board, #19),
pirates (danger board / pirate warnings, #24), terms, privacy.

JS modules (by `// ----` banner): formatting · resource forecast · state ·
freshness · shard · nearby · captures · destination · path/map · search ·
element finder · teammate presence · websocket · auth gate · cargo planner +
run mode · event planner · resource manager (catalog picker / goals / inventory)
· marketplace · pirate danger board (#24) · view router · leaderboard · statistics · Org Intel · org settings
· org logo · admins · watcher tokens · setup guide · init.

## app.py endpoint groups (grep the route to get the exact line)
- Nav/live: `/api/position`, `/api/state`, `/api/pois`, `/api/destination`, `/api/capture/*`, `/api/path/{action}`, `/api/refresh`
- Who's online (#19): `/api/online` (roster snapshot + `me` prefs), `/api/online/status` (set status/activity/appear-offline), `/api/playstyles` (shared activity/LFG vocab); LFG board `/api/lfg` (snapshot + post), `/api/lfg/{id}/join` (toggle), `/api/lfg/{id}` (close) — in-memory, WS `lfg` frame, surfaced in its own **Group Finder** app (`#/lfg`); `/api/lfg` snapshot carries `announce_available`; `POST /api/lfg` takes an `announce` flag → rate-limited Discord shout (`notify` category `lfg`, #19 step 4)
- Reference data: `/api/handles`, `/api/commodities`, `/api/raw_commodities`, `/api/ships`, `/api/harvestables`, `/api/fauna`, `/api/resource_*`, `/api/biomes`, `/api/custom_pois`, `/api/observations`
- Cargo planner: `/api/route/plan|run|history|session/reset`
- Cargo analytics: `/api/cargo/leaderboard`, `/api/cargo/stats`
- Trade Route Planner (#21): `/api/trade/terminals|prices|trades`, `/api/trade/plan` (auto/filtered/manual); **run mode (step 5)** `/api/trade/run` (POST start / GET resume / PATCH `action` buy|sell|advance / DELETE abandon) + `/api/trade/run/replan` (re-solve from live position, sunk-cargo-aware). Legs not stops: per-leg buy→sell phase; solver in `nav_core.plan_trade_route`/`cost_trade_legs`/`replan_trade_route`; session helpers `_point_at_active_trade_leg`/`_advance_trade_run`/`Session.trade_run_view`. **History + stats (step 6):** `/api/trade/history` (personal runs + realized-profit stats + quick-picks), `/api/trade/session/reset`, `/api/trade/stats` (guild Trading section `#/intel/trading`, `#/trade-stats` aliased) + admin `/api/admin/stats/trade/clear`; realized profit `nav_core.trade_leg_realized`→`trade_run_realized`; derivations `derive_trade_run_stats`/`derive_trade_quick_picks`/`derive_guild_trade_stats`/`derive_trade_leaderboard`; frontend `renderTradeHistory` (#/trade RECENT TRADES) + `renderTradeStats`. **Favorites:** `GET/POST /api/trade/favorites` + `DELETE /api/trade/favorites/{id}` (`TradeFavoriteIn`; save plan *config* not resolved legs → re-solve on load; `trade_favorites` table; `db.list/save/delete_trade_favorite`); frontend SAVED ROUTES panel `renderTradeFavorites`/`saveTradeFavorite`/`applyTradeConfig`, shared `buildTradePlanBody` + new generic `promptDialog`
- Pirate danger warnings (#24): `/api/warnings` (board snapshot + `announce_available`), `POST /api/warnings` (post point|lane, pvp|pve, severity, anchor POI id(s) + free-text location; opt-in `announce`), `POST /api/warnings/{id}/confirm` (community "still active" refresh), `DELETE /api/warnings/{id}` (poster/admin). In-memory `Hub.warnings` board persisted to `pirate_warnings`, time-based age-off (`warning_ageoff_min`/`warning_stale_min`, default 60/40) pruned in the presence broadcaster, WS `warnings` frame; `notify` category `pirates`. Frontend = its own **Danger Board** app (`#/pirates`): composer + board + launcher card + `☠️ N` badge; admin lifecycle knobs `warning_ageoff_min`/`warning_stale_min` in ORG SETTINGS. **Trade-planner integration:** `avoid_mode` (ignore|warn|avoid) on `TradePlanIn`/`TradeReplanIn`; `hub.active_trade_warnings()` → `_solve_trade_plan`/replan build avoid sets (nav_core `trade_avoid_sets`) for the `_trade_candidates` `avoid_poi_ids`/`avoid_pairs` filter, and `_annotate_trade_legs` (nav_core `trade_leg_warnings`) tags touched legs; frontend `setTradeAvoid` seg + per-leg ⚠ badge + route-level callout in `renderTradePlan`. **Board→events:** each card's "⚔ Organize hunt" (`promoteWarningToEvent`) prefills CREATE EVENT via the shared `eventSeed` (was `lfgEventSeed`) + `#/events/new` + `POST /api/events`. **v2 snare-detour routing (docs/snare-detour-routing.md):** warnings become hazard *volumes* (`nav_core.hazard_volumes` → sphere/capsule; base `hazard_radius_km` org setting ×severity), `travel_cost(avoid=, memo=)` decomposes a leg into jump segments (`_leg_segments`), tests them (`segment_hits`/`_seg_point_dist`/`_seg_seg_dist`) and inserts a detour waypoint (`_detour_via`) or flags `blocked`; result carries `waypoints`/`detour_m`/`dodged`/`blocked`. Threaded through the trade solver + cargo `plan_route(avoid_volumes=)`. `avoid_mode` **default flipped to `avoid`**; both `TradePlanIn`+`RoutePlanIn` gain `avoid_poi_ids` (personal blacklist, localStorage `avoidPois`, shared by both planners via `mountAvoidBlacklist`); `RoutePlanIn.avoid_mode` new (cargo had none). `_build_hazard_volumes` in app.py; warn-mode fly-past via `nav_core.leg_hazards`; cargo stops via `_annotate_cargo_stops`. Frontend: `detourVia`/`waypointSteps`/`worstSev` helpers, "dodge via" spans + blocked lines + `.route-reroute` callout, live `maybeTradeRerouteNudge` banner on a new-danger WS frame
- Events: `/api/events*`, `/api/events/{id}/signup`; **fleet roster (#20)** `/api/events/{id}/groups[/{gid}]` (board + group CRUD), `/api/events/{id}/assignments` (PUT assign/move/unassign, group_id null = unassign), `/api/events/{id}/manifest` (+ `/post` → Discord). Plan is organizer/admin-owned; nav-side logic `nav_core.derive_roster_board`/`build_event_manifest`
- Resource manager: `/api/catalog`, `/api/inventory*`, `/api/goals*`
- Marketplace: `/api/market*` (offers, confirm)
- Org analytics: `/api/leaderboard`, `/api/stats`, `/api/intel/directory`
- Admin: `/api/admin/stats/*/clear`, `/api/settings`, `/api/org-logo`
- Auth/account: `/auth/login|callback|logout`, `/api/me*`, `/api/tokens`
- Misc: `/api/health`, `/download/watcher`, `/` + `/index.html`

## db.py tables
meta · custom_pois · observations · handles · members · watcher_tokens ·
user_ships · runs · trade_runs · trade_favorites · pirate_warnings · events ·
event_signups · event_groups · event_assignments · catalog_items · inventory ·
goals · inventory_allocations · listings · listing_offers.

## Guardrails (don't regress these)
- **Security**: CSP/nonce + defense-in-depth headers (app.py `_csp`, http middleware);
  host-header pin; WS origin check; image magic-byte sniff (`_sniff_image`, no SVG);
  input caps on Pydantic models. Don't widen these casually.
- **Design**: follow `DESIGN.md` (tokens, components) and `PRODUCT.md` (scope).
- **No build step**: the SPA is served as-is. Don't introduce a bundler.
