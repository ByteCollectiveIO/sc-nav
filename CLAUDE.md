# CLAUDE.md — repo map & navigation

Purpose of this file: let a fresh session find the right code **without reading
whole files**. Grep the banner conventions below; jump with `Read(offset/limit)`.
Keep this map current when you add a view, endpoint, or table.

## What this is
Star Citizen org tool — eleven apps in one SPA: navigator, cargo + trade route
planners, Prospector (drop planner + belt survey, #38), event planner (+ fleet
rosters, templates), Ops (run a mission + its record), group finder, danger board, resource manager, aUEC marketplace, org
intel. FastAPI backend, **single-file** SPA, plus a Windows watcher (Python
script) that reports the player's in-game position.

- Backend: `server/app.py` (HTTP/WS + routes), `server/nav_core.py` (pure nav/route
  logic, fully unit-tested), `server/db.py` (SQLite schema + queries).
- Frontend: `server/static/index.html` — ONE file: `<style>` + body + `<script>`.
  **Authored as one file, SERVED as three** (2026-09-19, slow-origin fix):
  `app._shell_parts` lifts the `<style>`/`<script>` bodies out to content-hashed
  `/assets/app.<hash16>.css|js` (immutable; `shell_asset` route; stale hash =
  current bytes + no-store) so the no-store, nonce-stamped shell is ~140 KB, not
  1.2 MB. Keep exactly ONE `<style>` and ONE `<script>` in the file. Versioned
  `/images/…?v=` are immutable (`security_headers`); launcher `.app-logo` art is
  `loading="lazy"` and sized 2× display (320 px); org logo URL = `?v=org_logo_version()`
  (file mtime ms) on `/api/branding|me|settings` → `orgLogoUrl`. `#boot-splash`
  covers the shell until `boot()` resolves.
- Watcher: `watcher/` (runs on the player's Windows box; reads Game.log).
- Version: `server/version.py` (SemVer; surfaced at `/api/health` + footer).
- Tests: `server/test_nav_core.py` + `server/test_app.py`. **Two channels:**
  `main` = trunk — all PRs land here, and OUR server tracks it (it's a staging
  box: a handful of org members volunteered as testers, not a live org).
  `stable` = the release channel, tracked by the self-hosting orgs, which are
  the only real production. `tag-release` fast-forwards `stable` on each
  release; **nothing else may push it** (see `/deploy`).
- Docs: `docs/README.md` = index w/ per-doc status · `docs/product-overview.md`
  = consolidated app/service/data map · `docs/feature-backlog.md` = active work ·
  `docs/implementation-notes.md` = as-built per-feature detail (grep before changing a feature).

## DO NOT READ (token sinks / generated / binary)
- `server/.venv/**` — dependencies. Never read; never grep here.
- `poi/*.db`, `poi/*.db-wal/-shm` — SQLite binaries. Use `db.py` for schema.
- `poi/*.json` runtime caches (gitignored) AND the committed feeds
  (`poi.json`/`containers.json`/`container_tombstones.json`/`quantum_*`/`blueprints.json`/`locations.json`)
  — all token sinks. The schema is in code, not here.
- `server/testdata/uex/*.json` — minified UEX feed fixtures CI seeds into `poi/`
  so the app suite runs with `SC_NAV_OFFLINE=1` (no live fetch in the gate).
  Regenerate with `tools/make_test_fixtures.py`; never hand-edit, never read.
- `.impeccable/`, `.github/skills/`, `.claude/skills/` — tooling, not app code.

## Navigation conventions (how to find things fast)
Every logical section is marked by a greppable banner. To locate code, grep the
banner — don't scroll.
- **index.html JS**:  `grep -n "// ----" server/static/index.html`
- **index.html CSS**: `grep -n "/* ----" server/static/index.html`
- **index.html views**: `grep -n 'id="[a-z-]*-view"' server/static/index.html`
- **app.py routes**:  `grep -nE "^@app\.(get|post|put|patch|delete)" server/app.py`
- **db tables**:      `grep -n "CREATE TABLE" server/db.py`

## index.html section index (~29.7k lines; ranges drift — confirm by grep)
`<style>` lines 7–3617 · body 3619–~6546 · `<script>` 6548–29655.

Body views (each a `#…-view` container, hash-routed):
launcher, main (navigator), settings (category rail, one section at a time: `#/settings` = Profile, `#/settings/<watcher|my-ops|account>` for everyone, `#/settings/<general|members|branding|discord|announcements|apps|data>` admin-only — `discord` = webhook CHANNELS only; `announcements` = message templates + org image + linked-image previews (split 2026-10-06 critique: templates were buried ~1,300 px down) — `SETTINGS_SECTIONS`/`applySettingsSection`; data loads on ENTRY to Settings, not per section; a non-admin or unknown slug falls back to Profile), setup, intel, leaderboard, stats,
cargo-leaderboard, cargo-stats, route (cargo planner), events, goals, inventory,
blueprints (RM's third tab, #29), market, online (who's online, #19),
ops (Ops — run a mission + its record, `#/ops`, `#/ops/<id>`), lfg (group finder / LFG board, #19), pirates (danger board / pirate warnings, #24),
halo (Prospector, #31/#38: masthead tabs `#/halo` ATLAS (landing; `#/halo/atlas` still resolves) · `#/halo/field` FIELD · `#/halo/drop` DROP — containers `halo-tab-drop/field/atlas`, `applyHaloTab`/`haloGoTab`, FIELD pin `renderHaloFly`, ATLAS `renderHaloAtlas*`), terms, privacy.

JS modules (by `// ----` banner): formatting · ore colors · ore value badges (#32) · resource forecast · sighting pickers (`sightingPicker` factory — ONE combobox for both capture forms, instanced as `orePicker`/`wildPicker`; a `<datalist>` cannot annotate an option without replacing its text, so fauna+harvestables share this rather than a second idiom. Shortlists are per-category groups, never pooled) · state ·
freshness · shard · nearby · captures · destination · path/map · search ·
element finder · teammate presence · websocket · auth gate (+ PROFILE playstyle
chips, #30) · cargo planner + run mode · event planner · resource manager
(shared masthead `rmMast`; catalog picker / goals / inventory / my blueprint
library at `#/blueprints`, #29)
· operations (#/ops) · marketplace · pirate danger board (#24) · halo finder / Prospector (#31, tabs #38) · view router · settings sections · leaderboard · statistics · Org Intel · org settings
· org name + MOTD (`applyBranding`/`renderMotd`/`setLoginOrgName`, v0.50.0) · org logo · admins · watcher tokens · setup guide · init.


## app.py endpoint groups (grep the route to get the exact line)
**Per-feature detail lives in [`docs/implementation-notes.md`](docs/implementation-notes.md)**
(one `## <group>` per bullet below, moved verbatim 2026-10-06). **Before changing a
feature, grep that file for the function you're touching** — it holds the settled
user calls, in-game measurements and past-bug history that tests and code comments
don't. Design reasoning: the linked `docs/*.md`.
- **Nav/live:** `/api/position`, `/api/state`, `/api/pois`, `/api/destination`, `/api/capture/*`, `/api/path/{action}`, `/api/refresh`. Node quality (`q`/`lines` → `nav_core._normalize_resource`, `quality_lines_summary`; precedence lines > q > band), area-scoped forecast (`resource_forecast(zone=)`, `forecast_zone_at`), zone biomes, deep-space system ambiguity (`Session.system`), game-build stamping + patch staleness (`patch_of`, `PatchTracker`), container-name drift + ghost anchors (`resolve_container`, `register_ghost_containers`), vendored starmap snapshots (`tools/sync_containers.py`).
- **Who's online / LFG (#19):** `/api/online`, `/api/online/status`, `/api/playstyles`, `/api/lfg*` — docs/who-is-online-lfg.md.
- **Reference data:** `/api/handles` (projected — never return the raw registry entry), `/api/commodities`, `/api/raw_commodities`, `/api/ships`, `/api/harvestables`, `/api/fauna`, `/api/resource_*` (incl. `/api/resource_values`, #32), `/api/biomes`, `/api/custom_pois`, `/api/observations`, `/api/ore_signatures` (Strata feed via `tools/sync_strata.py`; NOT committed, optional everywhere, attribution required).
- **Cargo planner:** `/api/route/plan|run|history|session/reset`; `/api/cargo/leaderboard|stats`. Round-trip revisits (`_preds_cyclic`), jump gates as TIME (`GATE_NAMES`, `build_gate_registry`, `leg_eta_s`, `GATE_TRAVERSAL_S` unmeasured) — docs/cargo-hauling-planner.md.
- **Trade (#21):** `/api/trade/terminals|prices|trades|plan`, run `/api/trade/run` (+`/replan`, `/topup`), `/api/trade/history`, `/api/trade/stats`, `/api/trade/favorites`, `/api/trade/stock`, `/api/trade/containers`, `/api/trade/transactions` (watcher). Stock/demand reports, org price overlay, stop kinds (#34), legality (#42), sort by return (#44), margin floor (#43), container sizing + box policy + kiosk menus (#46), txn capture + shop identity (#41), top-up + mixed loads — docs/trade-route-planner.md, trade-topup.md, trade-transaction-capture.md.
- **Danger board (#24):** `/api/warnings` (+`/{id}/confirm`, DELETE); hazard volumes + detours (`hazard_volumes`, `travel_cost(avoid=)`) — docs/pirate-warnings.md, snare-detour-routing.md.
- **Prospector (#31/#35–#38):** `/api/halo/targets|bands|plan|locate`, `/api/halo/survey` (+`/export`, `/zones`, `/zones/active`, `/zones/{id}` + `/restore` + `/sightings`), `/api/custom_pois/{id}/refile`, `/api/admin/survey/clear`. Belt registry, system ladder, Keeger arcs, named zones + undo, survey lanes, health, belt ore parity, FIELD tab, pocket radar — docs/halo-finder*.md, belt-survey.md, survey-*.md.
- **Events + Ops:** `/api/events*` (signup, groups, assignments, manifest, attendees, complete, start, save-template, notify-test), `/api/event-templates*`, `/api/ops*` (phase, roster, join, attendance, deputies, rules, contracts, ledger, transfers, loot, rolls, members, leaderboard), `/api/me/ops`, `/api/admin/ops/guests*` — docs/event-planner.md, fleet-roster-squad-organizer.md, event-operations.md.
- **Resource manager:** `/api/catalog`, `/api/inventory*` (+`/{id}/split`), `/api/goals*` (contribute, announce; kinds materials|unlock|survey); blueprints `/api/blueprints*` (`/readiness`, `/stat-names` registered before `/{bp_key}`), `/api/me/blueprints` — docs/org-inventory-goals.md, inventory-quality.md, blueprint-*.md.
- **Marketplace:** `/api/market*` (modes sale|auction|barter|commission|wtb; offers, confirm, renew, `item_history`, trends, crafters) — docs/marketplace.md, blueprint-craft-commissions.md.
- **Org analytics:** `/api/leaderboard`, `/api/stats`, `/api/intel/directory`, `/api/intel/surveying`.
- **Admin + branding:** `/api/settings`, `/api/branding` (public), `/api/org-logo`, `/api/app-image/{key}`, `/api/admin/stats/*/clear`, `/api/admin/poi-overrides*`, `/api/admin/prices/clear`, `/api/admin/containers/clear`; scheduled feed refresh (`feed_refresh_loop`, **2 h floor is a user mandate**), wiki catalog (`_apply_wiki_catalog`, Pyro frame alignment) — docs/wiki-poi-enrichment.md.
- **Auth/account:** `/auth/*`, `/api/me*`, `/api/tokens`, `/api/handle`, `/api/admin/handles*` (unbind, merge), `/api/admin/members/{id}/access`; handle case-fold + watcher handle detection — docs/member-identity-and-directory.md.
- **Discord:** `server/notify.py` + `server/notify_templates.py`; `/api/settings/discord/org-image`, `/api/admin/notify-templates*`. Golden file `server/testdata/notify_golden.json` — regenerate ONLY for an intended wording change: `SC_NAV_OFFLINE=1 SC_NAV_REGEN_GOLDEN=1 .venv/bin/python -m pytest -q test_app.py -k NotifyGolden` — docs/discord-notifications.md, discord-notification-customization.md.
- **Update check:** `/api/updates` (admin, read-only). **Misc:** `/api/health`, `/download/watcher`, `/` + `/index.html`.
- Run the suite with `SC_NAV_OFFLINE=1` (CI does): without it `import app` fetches live UEX feeds and can hang.

## Don't regress — feature rules with thin or no other protection
Each line below is a rule that NO test fails on and no comment at the code states
(or whose test/comment only covers part of it) — so this line is its only guard.
Audit of all 450 rules moved out on 2026-10-06: `docs/archive/claude-md-audit-2026-10-06.md`.
**Adding a test or an at-the-code comment for a rule is how it earns its way off this list.**
- **Nav, data feeds, cargo:**
  - Never trust bare system_at() for deep-space stamps; pass Session.system as system_hint through custom_poi_from_position/observation_from_position/_frame_at.
  - Call _register_ghost_anchors after the DB merges in BOTH nav-build paths (startup and _rebuild_nav).
  - Ghost containers must never be used by detect_container for new captures.
  - Keep ghost_containers out of nav.containers: ghosts never synth POIs and never enter the belt registry.
  - Keep nav_core._survey_scan_stats rs_bases pure org evidence; merge with Strata only presentationally in the frontend.
  - The same stop_id may appear twice in plan_route stops; cargo run mode must handle repeats, don't dedupe by stop_id.
- **Trade planner:**
  - Trade favorites store plan CONFIG, never resolved legs — re-solve on load (TradeFavoriteIn/applyTradeConfig).
  - A detected transaction NEVER auto-confirms a leg — it only parks Session.pending_txn as a nudge; the log records the request, not fulfillment.
  - Don't wire captured auto_load/box_size/box_count into STOP_DWELL_S or route ranking yet — a guessed per-box constant would silently re-rank routes.
  - Don't assume dwell fits BOX COUNT: measured loading time fits SCU (85 s + 0.327 s/SCU); the equal-SCU/different-box experiment is unrun.
  - Don't add the auto-load fee (~30 aUEC/SCU each end) to profit/profit_per_hour until real data exists — 3 samples of one commodity.
  - Do NOT merge min_return_pct with the board's min_margin — one is a percentage, the other an aUEC/SCU spread; both apply.
  - min_return_pct is a CONSTRAINT; the solver still optimizes profit/hour — don't make the floor change the objective.
  - box_policy ALWAYS returns a policy; loading (auto|hand) only picks the default box mode — never gate sizing on hand-load (auto-load + hand-unload trap).
  - minimize_deadhead is unrelated to short fills (it penalizes empty repositioning) — don't reach for it to fix an under-filled hold.
  - Define fmtAuec once — a duplicate function declaration wins by hoisting (marketplace's 'N aUEC' version double-printed the unit).
  - tradeRunSig must include primary_sold + per-lot sold/added flags or WS de-dupe skips the re-render.
  - The trade board (/api/trade/trades, rank_trades) stays single-commodity — cargo:mixed is solver modes only.
- **Prospector + survey UI:**
  - Restore with a taken id or (system,slug) must 409 're-create the zone instead' — never silently re-home marks onto another row.
  - Keep rename/archive/delete reachable from the active-zone banner too: a 0-mark zone never opens the details card.
  - annotate_surface_lane_values tiers the harvestable lane in its OWN pool; never tier gems against ores.
  - Belt scan lines keep EVERY ore's Q (composition record) — unlike the node form's own-ore-only quality lines; don't restrict to one ore.
  - Every capture button (node, wild, POI, survey) reads exactly "Arm /showlocation capture" (CAPTURE_ARM_LABEL).
- **Events, Ops, Resource manager, Marketplace:**
  - PUT /api/events/{id}/attendees (manage_attendee) deliberately skips signups-open and capacity gates — organizer/admin only
  - db.complete_event only completes a status='scheduled' event; completed events go to the past tab regardless of clock
  - attendeesByRole must index byRole[t.role], not the {role,needed} target object
  - POST /api/ops/{id}/join adds the caller only and never sets attendance
  - Round-robin preview and roll must both use _rr_order so 'next up' never lies
  - Op page content is phase-gated (no attendance seg in Setup; split/payments hidden while Live)
  - Ops tile art ships at 320 px like every launcher tile
  - Pledged craft preview (_pledged_craft_preview) is detail-only via _goal_craft_block(contributions=)
  - _resolve_listing_expiry matches the winner by max bid, never float equality against winning_amount
  - _notify_survey_goal_met posts for org goals only, never personal
  - Never auto-create a survey goal when a zone is created
  - JS bpEffectAt must stay in step with nav_core.blueprint_quality_effect
- **Admin, auth, Discord:**
  - Don't extend ORG_COPY_MAX to app-tile NAMES/DESCRIPTIONS — artwork is overridable, identity isn't (PRODUCT.md scope boundary).
  - App-image GET is served `immutable`; every upload must bump the `app_image_<key>` epoch (the URL cache-buster) or browsers keep the old art forever.
  - `starmap_pois_enabled` and `wiki_pois_enabled` default OFF (blank-slate org); flipping either triggers `_rebuild_nav`.
  - `_refresh_feeds` is uexcorp feeds-only; never add a runtime starmap fetch — containers/poi are vendored snapshots updated via tools/sync_containers.py.
  - tools/sync_locations.py must FIT the wiki→starmap rotation per system each sync (`fit_frame_rotation`), never hard-code −85.23°.
  - Never round the fitted rotation angle before applying it (0.0004° ≈ 380 km at 54 Gm); round only the `_meta` record.
  - Committed Pyro static records must sit on their starmap containers and MNK-833 within 100 km of the real fix.
  - Call `apply_poi_overrides` AFTER merge_custom_pois/observations and BEFORE `assign_qt_markers`, in BOTH `_rebuild_nav` and startup.
  - POI overrides deliberately don't cover the trade-terminal feed or the NEARBY list (out of scope).
  - Honor `notify_opt_out` in `_mentions()` AND `_event_attendee_ids`: id leaves allowed_mentions/ping; name stays in message text.
  - `_merge_handle_rows` must re-attribute EVERY table keyed on PlayerID `owner_id` (today exactly custom_pois + observations); a new such table must join the merge.
  - A linked (url) org image is NOT auto-previewed in the app; load only on an explicit 'Load preview from <host>' click for an allowed host.
  - In-app previews of linked images must set `referrerpolicy=no-referrer`; form/org-image panel load only on click.
  - Template test-send shares the `_test_send_at` cooldown with the webhook Test (429 within 5 s).
  - `.dc-*` Discord-dark preview colours stay scoped under `.dc-msg`, not global tokens.
  - `_event_crew(ev)` is the ONE crew/roles formatter (new-event post + reminder); don't fork a second.
  - Pings ride `content` only (mentions inside embeds never ping); user names in embed titles; `<t:>` never in titles.
  - Event manifest post stays plain text on purpose (chunked document via `_manifest_chunks`), not an embed.
  - /api/updates is admin-only and read-only — never self-update.


## db.py tables
meta · custom_pois · poi_overrides · observations · handles · members · watcher_tokens ·
user_ships · runs · trade_runs · trade_favorites · stock_reports ·
trade_transactions · commodity_guids · shop_pois + shop_ids (#41; shopName is a PREFAB name, shopId identifies the terminal) · price_reports (#41/#39 §6.1) ·
container_reports (#46) ·
pirate_warnings · events ·
event_signups · event_groups · event_assignments · group_templates ·
event_templates + event_template_history · operations · op_roster · op_log · op_contracts + op_contract_ticks · op_ledger ·
op_transfers · op_loot + op_loot_intents + op_rolls + op_rotations · lfg ·
catalog_items · inventory · goals · inventory_allocations · listings ·
listing_offers.

## Guardrails (don't regress these)
- **Security**: CSP/nonce + defense-in-depth headers (app.py `_csp`, http middleware);
  WS origin check; image magic-byte sniff (`_sniff_image`, no SVG); input caps on
  Pydantic models. Don't widen these casually. Full posture + threat model:
  `docs/security-review-2026-08.md`. The load-bearing pieces:
  - **`auth_gate` is deny-by-default.** `_PUBLIC_EXACT` (method+path) +
    `_PUBLIC_PREFIXES` (`/auth/`, `/images/`) are the ENTIRE anonymous surface;
    everything else needs a session. A new route is private unless you list it.
    It used to be "deny `/api/*`, allow the rest", which silently published
    `/openapi.json`, `/docs`, `/redoc` and anything dropped in `server/static/`.
    The one pattern-matched exemption is `_SHELL_ASSET_RE` (the lifted-out shell
    CSS/JS, same bytes the public shell used to carry inline) — an exact shape,
    never a bare `/assets/` prefix.
    Interactive API docs are off (`FastAPI(docs_url=None, ...)`).
  - **Watcher tokens are scoped** to `_WATCHER_PATHS` (`/api/position`,
    `/api/handle`, `/api/trade/transactions`) — keep in sync with the watcher.
    They previously reached every dependency-less GET, i.e. the whole org
    dataset, from a credential stored in plaintext on a member's gaming PC.
  - **Off-boarding**: `POST /api/admin/members/{id}/access` → `access_revoked()`
    checked in `session_user`/`current_user`/`token_user`. Guild membership is
    only verified at OAuth login, so this is the ONLY thing that ejects a
    removed member before their 8h cookie expires; it also deletes their
    (never-expiring) watcher tokens.
  - **Coordinates are bounded + finite** (`PositionIn`, `_COORD_MAX`). A NaN
    persisted a custom POI that could never be serialized again, permanently
    500ing `/api/custom_pois` org-wide. `validation_error` also strips the
    rejected value from 422 bodies — echoing a NaN back re-raised it.
  - **Org price observations are banded** (`_ORG_PRICE_MAX_RATIO`, judged
    against the pre-overlay UEX snapshot `_uex_price_base`, never the running
    value) and purgeable (`POST /api/admin/prices/clear`). The confirms gate
    validates the learned guid/shop mappings, never the price itself.
  - **Rate limiting**: `rate_limit(bucket, uid)` + `_RATE_LIMITS` — per MEMBER
    (everything limited is authenticated), buckets `solve` (the three planners),
    `watcher` (position + txn ingest), `token` (`/api/tokens`,
    `/download/watcher`). Limits sit far above real use; tests clear
    `app._rate_hits`.
  - **Confirm dialogs**: `confirmDialog` resets `dlg.returnValue` before `showModal()`. A modal closed with Esc KEEPS the previous returnValue, so without the reset Esc on any confirm after an earlier OK resolved as OK, which made Esc on a destructive confirm DELETE (found 2026-10-06; `promptDialog` already had the reset). `safe: true` defaults focus to Cancel without red styling (privacy opt-ins).
- **Form controls**: `textarea.ti` and `select.ti` share the `input.ti` dark style + focus ring. Give every text/number input, select and textarea `class="ti"` (or put it inside `.ev-field`), INCLUDING ones built in JS template strings; a bare control renders as the browser's grey/white box with no focus state (2026-10-06: 6 textareas, 14 Settings inputs, the 8 JS-built webhook inputs, `.ev-rr-needed` and `#trade-boxsize` were bare; Market's `.mk-search`/`.mk-craftrow` rules lacked `font-family: inherit` → Arial). Verify with a computed-style sweep across routes, not a source grep. `button:disabled` is dimmed + not-allowed app-wide.
- **Frontend escaping**: `esc()` covers text AND quoted-attribute context.
    The trap is `.join()` inside a template literal — use `.map(esc).join(...)`.
- **Design**: follow `DESIGN.md` (tokens, components) and `PRODUCT.md` (scope).
- **No build step**: the SPA is served as-is. Don't introduce a bundler.
