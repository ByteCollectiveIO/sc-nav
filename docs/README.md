# docs/ index

The authoritative status of every design document, so a doc's own header never
has to be trusted alone. Consolidated 2026-07-04 (v0.36.0).

**Statuses:** ✅ shipped (spec is the reference for what's live) ·
📐 design, not built · 🅿 parked strategy · 📦 historical record.

**Lifecycle:** design doc written before build → Status header updated when it
ships → leftover ideas move to the [backlog](feature-backlog.md) fast-follows
(not into new sections of old docs). Line numbers and test counts inside docs
drift — trust grep and CI, not the citation.

## Orientation (start here)

| Doc | What it is |
|---|---|
| [apps/README.md](apps/README.md) | **User-facing showcase & how-to guides** — one page per app (screenshots + walkthroughs). The player-facing counterpart to the design specs below |
| [product-overview.md](product-overview.md) | **The consolidated map**: apps, platform services, data sources, conventions |
| [feature-backlog.md](feature-backlog.md) | Fast-follows, parked items, shipped log (#36 shipped v0.59.0; #37 survey platform slices 0–5 shipped v0.64–0.72, rest designed) |
| [org-deployment-guide.md](org-deployment-guide.md) | **Runbook for an org self-hosting an instance** — VPS sizing, registrar→Cloudflare DNS migration (§5, step-by-step incl. DNSSEC + email records), Cloudflare Tunnel + Portainer install, day-one settings, backups, maintenance, runbook. Written for volunteers, not sysadmins. Printable copy: [SC-Nav-Org-Deployment-Guide.pdf](SC-Nav-Org-Deployment-Guide.pdf) — **rendered, not a source of truth; regenerate it when the markdown changes** |
| [security-review-2026-08.md](security-review-2026-08.md) | **Threat model + security posture** across four surfaces (watcher→app, app→member, insider, external): what was fixed, what is verified sound and shouldn't be re-reviewed, what's still open and in what order |
| [../PRODUCT.md](../PRODUCT.md) · [../DESIGN.md](../DESIGN.md) | Product scope/brand · visual design system |

## Active designs (not built)

| Doc | Status | Covers |
|---|---|---|
| [survey-platform.md](survey-platform.md) | 🔨 slices 0–5 shipped v0.64–0.72 | #37: survey tool → prospecting suite — **radar layers, $$$ value tiers, ore-first routing, scan detail, arrival routing, coverage gaps + the always-on overview map, survey stats + Org Intel Surveying + Discord milestones + radar drift nudge ALL live**; remaining: patch staleness, cross-org import, mark kinds |
| [uex-data-contribution.md](uex-data-contribution.md) | 🔨 slice 0 built 2026-08-02 (v0.87.0) · outbound 🅿 parked | #39: **slice 0 (local price overlay) SHIPPED, fed by #41's log-derived observations** — org prices beat the UEX scrape per (terminal, commodity, side) newest-wins, per-side freshness + `⚡ org` badges, ≥2-confirm ingest gate, `is_missing` rows from stockouts. The outbound half (posting back to UEX: credential models, the screenshot blocker, quality guardrails) stays parked |
| [trade-transaction-capture.md](trade-transaction-capture.md) | 🔨 built 2026-08-02, awaiting live test | #41: the watcher reads commodity buys/sells out of Game.log (both sides verified: total · unit price · SCU · commodity GUID · shop) and POSTs them to `/api/trade/transactions`; run mode gets a "⚡ terminal reported" confirm nudge with the real numbers pre-filled; confirms teach the guid→commodity + shop→POI mappings and fill the #39 §6.1 `price_reports` ledger from day one. Human-in-the-loop by design — the log records the *request*, not fulfillment |
| [watcher-hud-interaction.md](watcher-hud-interaction.md) | 🔨 I1 built 2026-08-02, awaiting a flight (I2/I3 design) | #40.1: makes the shipped light HUD **inert then interactive** — foreground watcher, hover-polled click-through, hold-to-interact (replaces #40's blocked W2 rather than unblocking it), then an **in-game target chooser** so the HUD can retarget without alt-tabbing. Findings from surveying `sc-overlay` in §2; the two that reshaped it: SC's raw-input mouse read means a chooser that **takes focus parks the ship** (§4.1), and `POST /api/destination` is `require_session`, so the watcher token 401s today (§5.1). **§11 = the 2026-08-02 flight report that pulled I1 forward**: the heavy overlay ate clicks meant for the game, then wedged — the freeze being the one §2.5 predicted (`CalculateNativeWinOcclusion`). I1 shipped shared `watcher/sc_nav_win32.py`, game-foreground-gated click-through in both overlays, auto-hide, and for heavy a private browser profile (the only way its stability flags are guaranteed to apply), pid-based adoption and an `IsHungAppWindow` watchdog |
| [watcher-modular-hud.md](watcher-modular-hud.md) | 📐 exploratory 2026-08-01 | #40.2: what a **modular widget HUD** could be — nav / Prospector DROP / survey ⛏ / ore-RS reference / danger / trade-leg widgets as N small opaque tk windows sharing #40.1's click-through + dialog machinery, styled after `sc-overlay`'s one-question-per-widget grammar (concepts only; FSL license bars code). Superset of #40.1 (its I1–I3 = M0–M2 unchanged). Key findings: **every wanted endpoint is `require_session` today** → one auth-policy pass (§5); one composite `GET /api/hud?widgets=` door (§6); heavy-mode `#/hud` dock as the high-fidelity tier (§8.2); Game.log event-parsing spike (§9.0–9.2, part 1 done: missions/deaths/zones in the log, scan signatures NOT — their scanner is OCR-fed); **piggyback option (§9.3): sc-overlay's local sidecar API (port 8778, deliberately serves external consumers) exposes its OCR scan reads via `GET /api/mining` + SSE — could auto-attach RS evidence to survey marks without us building OCR; §9.4 scan-sweep method: rotate-in-place census stations → zone ore stats from RS divisibility, staleness-proof by construction (RS only — their OCR reads no distance). Capture 1 analyzed (§9.2.1, cargo session): **`<Calculate Route>` names the player's current location on every QT plot** — real-time named position without `/showlocation`; "Deliver N SCU of X to Y" objectives + jurisdiction transitions parseable; RS-in-log all-but-closed (zero signatures across the 70-session corpus). **Commodity-kiosk gate PASSED (§9.2.3, verified trade-run capture): buy+sell log total/unit-price/SCU/GUID/shop both sides → trade-run auto-confirm, actual-price capture (#39 slice 0), mission-hauling companion all unblocked** |
| [survey-app-restructure.md](survey-app-restructure.md) | v0.73.0 | #38: `#/halo` IA refactor — one app, three tabs (DROP · FIELD · ATLAS) + rename to **Prospector**; separates plan / cockpit / org-data surfaces without splitting the field loop; frontend-only, builds the shelf #37 import lands on |

## Shipped feature specs (reference for what's live)

| Doc | Shipped | Covers |
|---|---|---|
| [multi-user-migration.md](multi-user-migration.md) | 2026-06-18 | OAuth, org gating, SQLite, presence, admin — all phases |
| [cargo-hauling-planner.md](cargo-hauling-planner.md) | 2026-06-21 | Cargo Planner v1 + app shell/launcher origin; quantum overlay lands via #27 |
| [event-planner.md](event-planner.md) | 2026-06-23 | Event Planner v1 (taxonomy since amended by the todo doc) |
| [event-planner-todo.md](event-planner-todo.md) | 2026-06-24 | 📦 7-item UI/taxonomy pass; amends event-planner.md (multi-select types, Event/Race) |
| [org-inventory-goals.md](org-inventory-goals.md) | 2026-06-24/25 | Resource Manager v1 + v1.1 allocations model |
| [marketplace.md](marketplace.md) | 2026-06-25/26 | Marketplace v1 + scaling/search pass; some "Deferred" items since built — see its build-log notes |
| [member-identity-and-directory.md](member-identity-and-directory.md) | 2026-06-29 | members table, primary handle, seller_handle, admin directory |
| [discord-notifications.md](discord-notifications.md) | v0.14.0–v0.17.0 | Per-category webhook pushes (#18); coverage + platform much expanded by the 2026-08 org-launch batch below |
| [org-launch-review-2026-08.md](org-launch-review-2026-08.md) | 2026-08-04 | **Org-launch hardening batch**: 4-agent review of Marketplace/Events/webhooks + competitor research → P0 roster fix, auction/outbid/reschedule/waitlist notifications, mention paging past 50, webhook health, my-activity tab, staleness+renew, admin delete, capacity+waitlist, clone/past/attendee mgmt, market sweep loop, ping opt-out. Open boxes = next-pass batch |
| [who-is-online-lfg.md](who-is-online-lfg.md) | v0.18.0–v0.22.0 | Online roster + Group Finder (#19) |
| [fleet-roster-squad-organizer.md](fleet-roster-squad-organizer.md) | v0.23.0–v0.24.1 | Event groups/assignments, seat + group templates (#20) |
| [trade-route-planner.md](trade-route-planner.md) | v0.28.1–v0.33.0 | Trade Route Planner, all 6 steps (#21) |
| [pirate-warnings.md](pirate-warnings.md) | v0.34.0 | Danger Board + planner avoid/warn (#24 v1) |
| [snare-detour-routing.md](snare-detour-routing.md) | v0.35.0 | Hazard-volume detour routing (#24 v2) |
| [quantum-data-pipeline.md](quantum-data-pipeline.md) | v0.37.0 | `tools/sync_quantum.py` → committed `poi/quantum_{drives,profiles}.json` (#26 slice) |
| [quantum-fuel-range.md](quantum-fuel-range.md) | v0.37.0 | Fuel burn + max-range in both planners (#27) |
| [blueprint-craft-commissions.md](blueprint-craft-commissions.md) | v0.40.0–v0.44.0 | Commission mode + blueprint feed + spec builder (#25); library, craft-goal spec, mats cost, stat autocomplete, sale identity + expected stats (#25.1 — closed; leftovers are backlog fast-follows) |
| [rm-restructure-and-profile.md](rm-restructure-and-profile.md) | v0.45.0 | RM Goals · Inventory · Blueprints restructure (#29) + member playstyle profile tags (#30) |
| [wiki-poi-enrichment.md](wiki-poi-enrichment.md) | v0.46.0 | Wiki locations catalog: `wiki_pois_enabled` import (241 POIs + 206 QT promotions), per-POI arrival radii, trade-stop amenity chips (#28; closes #26) |
| [halo-finder.md](halo-finder.md) | 2026-07-10 | Halo Finder, the tenth app (`#/halo`, #31): Aaron Halo band/POI drop planner, staging hops, verify-and-refine, navigator belt chip |
| [halo-finder-expansion.md](halo-finder-expansion.md) | v0.55.0 | Halo Finder → Nyx Glaciem Ring pocket mode (381 datamined segments, ~4% coverage insight) + Pyro unmarked-field fly-bys (#35); Pyro VI/V rings researched-and-rejected (don't exist); in-game pass pending |
| [belt-survey.md](belt-survey.md) | v0.59.0 | Crowd-sourced belt mapping (#36): ⛏ survey marks → pockets live from mark #1 → Keeger drop plans + field-model export; Keeger region awareness + guarded system rung; miss-ceiling honesty guard; in-game pass pending |
| [watcher-overlay.md](watcher-overlay.md) | v0.83.0–v0.84.2 | #40: in-game overlay in the watcher bundle, both modes in-game verified — **light HUD** (target · distance · bearing · ETA · fix age; costs zero extra requests, `post_position` already built the frame and discarded it) and **heavy beta** (the whole SPA pinned over the game in an app-mode browser window, live over WS). No inbound traffic, no firewall changes (§4). Win32 gotchas that cost three cuts in §10.1/§13.7/§13.8 |
| [survey-zones.md](survey-zones.md) | v0.59.0 | Named survey zones (#36.1): active-zone auto-tag so `⛏` marks localize into a deliberately-named, plannable field anywhere (incl. the Glaciem ring dead-zone); zone owns identity, geometry always derived; in-game pass pending |

## Strategy / records

| Doc | Status | Notes |
|---|---|---|
| [monetization-and-deployment.md](monetization-and-deployment.md) | 🅿 parked 2026-06-28 | CIG fan-rules research; non-commercial rule; CIG inquiry not drafted |
| [archive/feature-backlog-full-2026-07-04.md](archive/feature-backlog-full-2026-07-04.md) | 📦 archive | Full pre-consolidation backlog with every design's original prose (#1–25) |
