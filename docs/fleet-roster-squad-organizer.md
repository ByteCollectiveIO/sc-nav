# Fleet roster / squad organizer (event group planning)

**Status:** **v1 SHIPPED 2026-07-01 — v0.23.0 (commit 259456a)** (steps 1–4).
**v1.1 (step 5) BUILT 2026-07-01 — uncommitted, needs /deploy.** Backlog #20.
Tables `event_groups` + `event_assignments`; `nav_core.derive_roster_board` /
`build_event_manifest` (pure, tested — `RosterBoardTests` + `EventManifestTests`);
endpoints `GET/POST/PATCH/DELETE /api/events/{id}/groups[/{gid}]`,
`PUT /api/events/{id}/assignments`, `GET /api/events/{id}/manifest` +
`POST .../manifest/post`; UI = a **Fleet roster** section on the event detail
(`renderFleetSection`/`drawFleet`): unit cards + unassigned pool + assign row +
inline group form + per-member "your assignment" callout + copy/post-to-Discord
manifest.

### v1.1 (built 2026-07-01, uncommitted)
Two additions, both from step 5 of the build order:

**Ship-aware seat templates.** `nav_core.ship_seat_template(crew, traits)` (pure,
7 tests in `ShipSeatTemplateTests`) derives a default seat layout — Pilot,
Co-Pilot, one role-flavored specialist seat (`SHIP_ROLE_FLAGS`: medical→Medic,
mining→Mining Op, …), then Turret N. New `load_fleet_ships()` reads the full
uexcorp vehicle rows `load_ships` already caches (no extra fetch), yielding all
spaceships as `{name, crew, seats}` at `GET /api/fleet/ships` (superset of the
cargo `/api/ships`). Frontend: the group form's ship field is now a datalist
picker; choosing a known ship auto-fills the unit size (crew), and the assign
row's seat input offers that unit's ship seats as a datalist.

**Saved group templates.** New org-shared `group_templates` table (JSON blob of
`[{name, kind, ship, capacity}]` — structure, not members) + `db` CRUD.
Endpoints: `GET /api/group-templates`, `POST /api/group-templates`
(snapshot an event's units; organizer/admin of that event),
`DELETE /api/group-templates/{tid}` (author or admin),
`POST /api/events/{id}/groups/apply-template` (stamp a template's units onto an
event; organizer/admin). Frontend: a "Templates" toggle in the fleet controls
opens a panel to save the current plan and apply/delete saved templates.
Integration coverage in `test_app.FleetTemplateTests` (feed shape, snapshot→
apply→delete lifecycle, empty-plan rejection). Suite 165 nav_core + 73 app green.

Original scope follows.
**Priority:** #3 — turns "12 people signed up" into an actual operational plan.
A genuine org-management tool: organize signed-up members into squads (FPS),
squadrons (flight), and ship crews **before** an event.

## Goal
On top of the existing event planner, let an organizer slot signed-up members
into named **units** with a hierarchy — and assign people to specific ships and
seats — so everyone knows their squad, their bird, and their role at op start.

---

## What already exists (and the gap)

The event planner (`docs/event-planner.md`, `events` + `event_signups`):
- `events.roles` — JSON **target roster**: `[{role, needed}]` (e.g. 2 medics).
- `event_signups.roles` — JSON list of role names **each member will fill**.
- So we know *who's coming* and *what roles they want* — but everyone sits in one
  flat pool. There is **no concept of grouping** them into units, no ship/seat
  assignment, no per-squad leader. That's the gap.

---

## Model: groups + assignments (a layer over signups)

Two new tables (keep `events`/`event_signups` as-is — this is additive):

```
event_groups
  id            INTEGER PK
  event_id      INTEGER  -> events.id
  parent_id     INTEGER  -> event_groups.id  (NULL = top-level; enables hierarchy)
  name          TEXT     (e.g. "Alpha Squad", "Gold Squadron", "Drake Caterpillar #1")
  kind          TEXT     squad | squadron | crew | section | wing
  ship          TEXT     (optional; for crew groups — from the ships feed / user_ships)
  capacity      INTEGER  (optional target size; drives "needs N more")
  leader_id     TEXT     (optional; a signup's discord_id)
  notes         TEXT
  sort          INTEGER

event_assignments
  event_id      INTEGER
  discord_id    TEXT     (a signed-up member)
  group_id      INTEGER  -> event_groups.id
  slot          TEXT     (role/seat within the group: pilot, gunner, medic, breacher…)
  UNIQUE(event_id, discord_id)   -- a member is in exactly one leaf group
```

**Why a separate assignments table** (not a `group_id` column on `event_signups`):
keeps the signup (intent to attend) cleanly separate from the plan (where the
organizer puts them), and the plan is organizer-owned while signups are
member-owned. A member's *requested* roles still live on the signup; their
*assigned* seat lives here.

### Hierarchy
`parent_id` gives nesting so a **squadron** (top group) contains **ship crews**
(child groups), each crew having seats; or a **platoon** contains **squads** of
**fireteams**. Don't over-engineer the UI depth — support 2 levels well
(top-level unit → members, or unit → ship crews → members).

### Ship-aware crews
A crew group can name a `ship` (from the existing ships feed / a member's
`user_ships`). Seats become the slots (pilot, co-pilot, turret×N, engineer,
medic). Optionally seed seat templates per ship from known crew sizes.

---

## Surfaces (on the event detail view)
- **Roster board**: an "Unassigned" pool of signups on one side; group columns/
  cards on the other. Assign by picking a member into a group + slot (drag-and-
  drop is nice-to-have; a select/picker is fine for v1 and works on mobile).
- **Capacity feedback**: each group shows filled/capacity; the target roster
  (`events.roles`) is cross-checked — "2/2 medics ✓", "squad needs 2 more".
- **Per-member view**: a signed-up member sees *their* assignment — "You're in
  Alpha Squad, breacher" — front and center on the event.
- **Manifest export**: render the whole plan as Discord-flavored markdown to paste
  (or auto-post via **#18**) — the op order at a glance.
- **Templates**: save a group structure (Alpha/Bravo/Charlie, or a standard
  squadron) and apply it to a future event. Store as a JSON blob (org-level, in
  `meta`, or a small `group_templates` table). v1.1.

## Permissions
- Organizer (`events.organizer_id`) + admins edit the plan. Members see it
  read-only (and their own assignment highlighted).
- Can't assign someone who isn't a `going` signup (or allow, and mark them as a
  pending invite — decide at build; default to signups-only).

## Endpoints
- `GET /api/events/{id}/groups` — groups + assignments + the unassigned pool.
- `POST/PATCH/DELETE /api/events/{id}/groups[/{gid}]` — manage groups.
- `PUT /api/events/{id}/assignments` — assign/move/unassign a member (organizer).
- `GET /api/events/{id}/manifest` — the formatted op-order export.

## Build order
1. Tables + groups CRUD + assignment endpoint (server + nav-side logic).
2. Roster board UI: unassigned pool + group cards + assign/move/remove.
3. Capacity + target-roster cross-check; per-member "your assignment".
4. Manifest export (→ Discord via #18).
5. (v1.1) ship-aware crews with seat templates; saved group templates.

## Relevant code
- `server/db.py` — `events`, `event_signups` (the base), `user_ships`, add the two
  new tables.
- `server/app.py` — `/api/events*` group (routes to extend), `/api/ships`.
- `server/static/index.html` — events view + `.ev-roster*` styles/markup (the
  existing target-roster UI is the visual starting point), view router.
- `docs/event-planner.md` — the feature this builds on.
- Ties into **#18** (manifest → channel post).
