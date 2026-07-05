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
| [product-overview.md](product-overview.md) | **The consolidated map**: apps, platform services, data sources, conventions |
| [feature-backlog.md](feature-backlog.md) | Active work (#25–28), fast-follows, parked items, shipped log |
| [../PRODUCT.md](../PRODUCT.md) · [../DESIGN.md](../DESIGN.md) | Product scope/brand · visual design system |

## Active designs (not built)

| Doc | Status | Notes |
|---|---|---|
| [blueprint-craft-commissions.md](blueprint-craft-commissions.md) | ✅ v1 BUILT 2026-07-05, pending release | Backlog #25; commission mode + blueprint feed + spec builder; v1.1 (member blueprint library) open |

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
| [discord-notifications.md](discord-notifications.md) | v0.14.0–v0.17.0 | Per-category webhook pushes (#18) |
| [who-is-online-lfg.md](who-is-online-lfg.md) | v0.18.0–v0.22.0 | Online roster + Group Finder (#19) |
| [fleet-roster-squad-organizer.md](fleet-roster-squad-organizer.md) | v0.23.0–v0.24.1 | Event groups/assignments, seat + group templates (#20) |
| [trade-route-planner.md](trade-route-planner.md) | v0.28.1–v0.33.0 | Trade Route Planner, all 6 steps (#21) |
| [pirate-warnings.md](pirate-warnings.md) | v0.34.0 | Danger Board + planner avoid/warn (#24 v1) |
| [snare-detour-routing.md](snare-detour-routing.md) | v0.35.0 | Hazard-volume detour routing (#24 v2) |
| [quantum-data-pipeline.md](quantum-data-pipeline.md) | v0.37.0 | `tools/sync_quantum.py` → committed `poi/quantum_{drives,profiles}.json` (#26 slice) |
| [quantum-fuel-range.md](quantum-fuel-range.md) | v0.37.0 | Fuel burn + max-range in both planners (#27) |

## Strategy / records

| Doc | Status | Notes |
|---|---|---|
| [monetization-and-deployment.md](monetization-and-deployment.md) | 🅿 parked 2026-06-28 | CIG fan-rules research; non-commercial rule; CIG inquiry not drafted |
| [archive/feature-backlog-full-2026-07-04.md](archive/feature-backlog-full-2026-07-04.md) | 📦 archive | Full pre-consolidation backlog with every design's original prose (#1–25) |
