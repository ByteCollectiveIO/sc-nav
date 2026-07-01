# CLAUDE.md — repo map & navigation

Purpose of this file: let a fresh session find the right code **without reading
whole files**. Grep the banner conventions below; jump with `Read(offset/limit)`.
Keep this map current when you add a view, endpoint, or table.

## What this is
Star Citizen org tool: live navigator + cargo-hauling planner + event planner +
resource manager + aUEC marketplace. FastAPI backend, **single-file** SPA, plus a
Windows watcher (Python script) that reports the player's in-game position.

- Backend: `server/app.py` (HTTP/WS + routes), `server/nav_core.py` (pure nav/route
  logic, fully unit-tested), `server/db.py` (SQLite schema + queries).
- Frontend: `server/static/index.html` — ONE file: `<style>` + body + `<script>`.
- Watcher: `watcher/` (runs on the player's Windows box; reads Game.log).
- Version: `server/version.py` (SemVer; surfaced at `/api/health` + footer).
- Tests: `server/test_nav_core.py`. Deploy = push to `origin/main` (see `/deploy`).

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
market, online (who's online, #19), terms, privacy.

JS modules (by `// ----` banner): formatting · resource forecast · state ·
freshness · shard · nearby · captures · destination · path/map · search ·
element finder · teammate presence · websocket · auth gate · cargo planner +
run mode · event planner · resource manager (catalog picker / goals / inventory)
· marketplace · view router · leaderboard · statistics · Org Intel · org settings
· org logo · admins · watcher tokens · setup guide · init.

## app.py endpoint groups (grep the route to get the exact line)
- Nav/live: `/api/position`, `/api/state`, `/api/pois`, `/api/destination`, `/api/capture/*`, `/api/path/{action}`, `/api/refresh`
- Who's online (#19): `/api/online` (roster snapshot + `me` prefs), `/api/online/status` (set status/activity/appear-offline), `/api/playstyles` (shared activity/LFG vocab); LFG board `/api/lfg` (snapshot + post), `/api/lfg/{id}/join` (toggle), `/api/lfg/{id}` (close) — in-memory, WS `lfg` frame, surfaced in `#/online`
- Reference data: `/api/handles`, `/api/commodities`, `/api/raw_commodities`, `/api/ships`, `/api/harvestables`, `/api/fauna`, `/api/resource_*`, `/api/biomes`, `/api/custom_pois`, `/api/observations`
- Cargo planner: `/api/route/plan|run|history|session/reset`
- Cargo analytics: `/api/cargo/leaderboard`, `/api/cargo/stats`
- Events: `/api/events*`, `/api/events/{id}/signup`
- Resource manager: `/api/catalog`, `/api/inventory*`, `/api/goals*`
- Marketplace: `/api/market*` (offers, confirm)
- Org analytics: `/api/leaderboard`, `/api/stats`, `/api/intel/directory`
- Admin: `/api/admin/stats/*/clear`, `/api/settings`, `/api/org-logo`
- Auth/account: `/auth/login|callback|logout`, `/api/me*`, `/api/tokens`
- Misc: `/api/health`, `/download/watcher`, `/` + `/index.html`

## db.py tables
meta · custom_pois · observations · handles · members · watcher_tokens ·
user_ships · runs · events · event_signups · catalog_items · inventory · goals ·
inventory_allocations · listings · listing_offers.

## Guardrails (don't regress these)
- **Security**: CSP/nonce + defense-in-depth headers (app.py `_csp`, http middleware);
  host-header pin; WS origin check; image magic-byte sniff (`_sniff_image`, no SVG);
  input caps on Pydantic models. Don't widen these casually.
- **Design**: follow `DESIGN.md` (tokens, components) and `PRODUCT.md` (scope).
- **No build step**: the SPA is served as-is. Don't introduce a bundler.
