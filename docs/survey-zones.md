# Named survey zones — deliberate field mapping (backlog #36.1) — design plan

**Status: ✅ BUILT 2026-07-18 (same day as the design), browser-verified via
the headless preview harness (zone selector + active-zone banner + zone-centered
radar render correctly); backend end-to-end tested (create → active → armed
capture auto-tags → geometry derives → planner pins by slug → rename/delete
lifecycle). NOT in-game verified.** Build notes / deviations:
- **Ownership keyed on `created_by` (discord_id), not a PlayerID.** The first
  draft used `owner_id` (a handle-derived PlayerID), but a session with no bound
  handle has none, so nobody but an admin could edit their own zone. Switched to
  the member's discord_id (always present), matching the group_templates /
  trade_favorites precedent.
- Shipped as designed: `survey_zones` table + `members.active_survey_zone` (via
  `_ensure_column`); `survey.zone_id` JSON tag; `survey_cluster_fit` extracted
  and shared by proximity clusters + zones; `survey_zones_state`; zone-tagged
  marks excluded from `survey_pockets` + `annotate_glaciem_survey`; zones carry a
  `survey` sub-dict so the barren down-rank / drop badge / frontend treat them
  uniformly; POST/GET/PATCH/DELETE `/api/halo/survey/zones` + PUT
  `/api/halo/survey/zones/active`; capture-arm auto-tag; zones in `/api/halo/survey`
  + export; zone slug pins the planner in ANY system (`_halo_goal_system`
  resolves the system from the slug); frontend zone selector + active banner +
  zone-centered radar + plan-a-drop-here.
- Delete untags marks live (in `nav.pois`) + persists, so they revert to
  proximity clustering — never destroyed.

Companion to
[belt-survey.md](belt-survey.md) (#36) and
[halo-finder-expansion.md](halo-finder-expansion.md) (#35). The user's ask:
"if I come across an asteroid field that is not in a known pocket and I want to
add it and survey it, what is the workflow to add it as a named survey zone and
then localize the survey points into that new pocket?"

Decisions locked with the user before this doc:
- **Grouping = active zone (auto-tag).** Name a zone once; it becomes your
  active zone and every `⛏` mark auto-tags to it. Deterministic membership, no
  proximity guessing. "Stop" = clear the active zone.
- **Scope = anywhere.** Any system, any deep-space field — including the
  Glaciem ring dead-zone between datamined pockets and open Nyx space.

---

## 1. Why the current implementation isn't enough

#36 already lets you survey unmapped space, but membership is emergent, not
deliberate. `survey_pockets(nav, system)` (nav_core) clusters rock-positive
marks by **proximity**: a mark joins the nearest cluster within `SURVEY_MERGE_M`
(≈ 10,392 km) of its centroid, else starts a new one; the pocket key is
`SVY-<lowest member id>` and its radius comes from the members' spread, capped
inward by the nearest "nothing here" negative. That gives three gaps:

1. **No name.** Pockets are `SVY-1000234`, never "Nyx Iron Field A."
2. **No deliberate grouping.** Geography decides membership, so two adjacent
   fields can silently merge, or one big field can split. You can't declare
   "these 40 marks are *one* field."
3. **A location dead-zone.** `survey_pockets` excludes any mark inside the
   Glaciem envelope (`glaciem_contains`), and `annotate_glaciem_survey` only
   attaches a mark to a *datamined* pocket within its grid radius. So a rock
   field found **inside the Glaciem ring but between** datamined pockets is
   stored yet completely inert — no pocket, no name, no plan.

Named zones fix all three, and reuse everything already built: the geometry
extrapolation (centroid + spread, negative-capped), the pocket-mode drop
solver, the live in-pocket radar, and the export.

## 2. Model: a zone is an id + name; geometry is always derived

Consistent with #36's "derive live from the marks, never cache the fit"
philosophy, a **zone owns only its identity**. Its center, radius, density, and
ore list are always recomputed from its member marks, so a new mark refines the
zone on the next read and deleting a mark heals it.

- **Membership** = a mark tagged with the zone's id. Explicit, not geographic.
- **Geometry** = the existing `survey_pockets` math applied to the zone's
  members: centroid of positives (barren zones use all marks), radius =
  `max(GLACIEM_POCKET_RADIUS_M-ish floor, 1.2 × spread)` capped inward by the
  nearest negative. Reused verbatim — factor the per-cluster reducer out of
  `survey_pockets` into a shared `survey_cluster_fit(members, negatives)` so both
  proximity clusters and zones call it.
- **Anywhere** = zone grouping does **not** apply the `glaciem_contains` filter
  (unlike `survey_pockets`), so a zone in the ring dead-zone or any system is
  first-class.

### 2.1 Storage

- **New table `survey_zones`** (`db.py`): `id` (int PK), `slug` (stable, from
  the name — the planner pin key), `name` (display), `system`, `owner_id`,
  `created` (epoch), `closed` (0/1). Small; org-visible (shared — two members
  can survey the same zone together).
- **Marks carry `zone_id`**: extend the existing `custom_pois.survey` JSON blob
  with `"zone_id": <int|null>`. No new column — the payload is already JSON
  (`SurveyPayloadIn` / `Poi.survey`). Untagged marks (`zone_id` absent/null)
  keep proximity clustering, so **all existing #36 data is untouched**.
- **Active zone per member**: `members.active_survey_zone` (int|null) via
  `_ensure_column`, exactly like `members.playstyle_tags` (#30). Per-member
  (your active zone is yours), while the zone itself is shared.

## 3. The workflow (what the pilot does)

1. Fly to the new field. In the Halo view's `⛏ SURVEY` block, the **Zone**
   control shows `Active: none`. Tap **＋ New zone**, name it ("Nyx Iron Field
   A"). It's created in the current fix's system and set as your active zone.
2. A banner shows **`Surveying: Nyx Iron Field A — 0 marks`** with the live
   in-pocket radar directly beneath (the radar already exists; it now centers
   on the zone's running centroid).
3. Drop `⛏` marks as you fly the field (density / ores / salvage as today).
   Each auto-tags to the active zone — no proximity guessing, no re-typing.
   Negatives ("nothing here") tighten the boundary; positives grow/confirm it.
   Banner updates: `12 marks · ⌀ ~180 km · Iron, Quartz`.
4. The zone is **immediately a named, plannable pocket**: a drop-planner target
   (pin by name), a radar, an export row. It works whether the field is in
   Keeger, open Nyx, or the Glaciem ring dead-zone.
5. **Finish / switch**: pick another active zone, or **Clear active** (the
   "stop"). Marks dropped with no active zone fall back to proximity clustering
   as before. A finished zone can be **Closed** (kept, but off the default
   target list) or **Renamed** any time.

Implementing "stop" as *clear the active zone* (not a separate mode) means
there's no way to drop an untagged mark by forgetting to press stop — the
control always shows exactly which zone you're feeding.

## 4. API

- `POST /api/halo/survey/zones` — `{name, system?}` (system defaults to the
  caller's current fix system). Creates the zone, returns it, sets it active for
  the caller. `409` on a duplicate slug in the same system.
- `GET /api/halo/survey/zones?system=` — list zones with **derived** geometry:
  `{id, slug, name, system, closed, marks, positive, status
  (barren|sparse|medium|dense|empty), center_xyz, grid_radius_m, ores, salvage,
  closest_center_m, owner_handle}`.
- `PATCH /api/halo/survey/zones/{id}` — `{name?, closed?}` (rename / close /
  reopen). Owner or admin.
- `DELETE /api/halo/survey/zones/{id}` — owner/admin. Member marks become
  untagged (`zone_id` cleared) → they revert to proximity clustering, never
  deleted.
- **Active zone**: `PUT /api/me` gains `active_survey_zone` (int|null,
  validated against a visible zone), carried on `GET /api/me`; mirrors the
  `playstyle_tags` precedent. Setting it is also the return path of zone create.
- **Capture tagging**: `POST /api/capture/start` (type=survey) stamps
  `survey.zone_id` from the caller's `active_survey_zone` at arm time (still
  overridable by an explicit `zone_id` in the payload, e.g. a per-mark pick).
  Resolution (`_capture_poi`) already persists the survey blob unchanged.
- **Existing endpoints gain zones**: `/api/halo/survey` + `/export` add a
  `zones` array (the derived state above) alongside `pockets`/`glaciem`;
  `/api/halo/targets` (ring) already lists surveyed pockets — zones join that
  pool. `HaloPlanIn.pocket_key` resolves a zone slug (the pin key), so a zone is
  planned exactly like a datamined or SVY- pocket.

## 5. nav_core

- `survey_cluster_fit(members, negatives) -> dict` — extract the per-cluster
  reducer currently inline in `survey_pockets` (centroid, radius, density, ores,
  salvage, closest-center). Single source of truth for zones + proximity
  clusters.
- `survey_zones_state(nav, system, zones) -> list[dict]` — group marks by
  `zone_id`, fit each with `survey_cluster_fit`, emit #35-pocket-shaped dicts
  (`kind:"zone"`, `key:slug`, `name`, `status`, `center_xyz`, `grid_radius_m`,
  …) so `plan_halo_drop` consumes them untouched. **No `glaciem_contains`
  filter** (zones are allowed anywhere).
- `survey_pockets` change: exclude marks that carry a `zone_id` (they belong to
  a deliberate zone, not a proximity cluster) so a mark is never double-counted.
- `annotate_glaciem_survey` change: skip zone-tagged marks too — a tagged mark
  is its zone's, not the datamined overlay's. (A mark inside Wtn-227 with no
  zone still annotates Wtn-227 as today.)
- Barren zones are first-class: an all-negative zone gets `status:"barren"` and
  the same flag + `GLACIEM_BARREN_PENALTY` down-rank the datamined overlay uses.

## 6. Frontend

- `⛏ SURVEY` block gains a **Zone** row: active-zone `<select>` (populated from
  `/api/halo/survey/zones`) + **＋ New zone** (reuses the shared `promptDialog`)
  + **Clear active**. Persist nothing client-side — the active zone lives on the
  member record so it follows you across devices/reconnects.
- **Active-zone banner** above the radar: name, mark count, extent, ore list,
  status pill (barren pill reuses the existing warn styling). The live radar
  (`updatePocketRadar`/`drawPocketRadar`) centers on the zone centroid from
  `/api/halo/survey/zones` when a zone is active and the fix has no datamined
  pocket — so you get the scale-and-progress view even in open space.
- **Zones panel** (list): rename / close / delete / "plan a drop here" (pins the
  slug and runs `planHalo`). Sits with the Keeger progress UI.
- Belt selector: zones appear in the pocket pool the same way SVY- pockets do;
  a zone slug pins directly (no belt toggle needed, per the existing
  `pocket_key` path).

## 7. Edge cases & decisions

- **System mismatch**: a zone is system-scoped. Dropping a mark while your
  active zone's system ≠ the fix's system → the mark's own system wins and the
  client warns "active zone is in {sys}; this mark is in {sys2} — not tagged."
  Keeps a zone from spanning systems by accident.
- **Collaboration**: zones are org-visible; the active-zone pref is per-member,
  so two pilots can feed the same zone at once. `survey_marks` already excludes
  private marks, so a private mark never leaks into a shared zone.
- **Barren open-space zone**: valid and useful ("we checked here, empty") —
  `status:"barren"`, radius from the negatives' extent, down-ranked if planned.
- **Delete vs close**: close keeps the zone off default targets but preserves
  data + plannability by pin; delete untags marks (revert to proximity), never
  destroys marks.
- **Admin reset**: `POST /api/admin/survey/clear` also clears `survey_zones`
  for the system (marks + zones heal together, matching the patch-reset intent).

## 8. Migration & backward compatibility

- Additive only: new `survey_zones` table + `members.active_survey_zone` via
  `_ensure_column`; `zone_id` is a new optional key in an existing JSON blob.
- Existing marks have no `zone_id` → unchanged proximity clustering. SVY-
  pockets and named zones coexist. No feed reload, no data rewrite.

## 9. Test plan

- nav_core: `survey_cluster_fit` parity with the old inline reducer;
  `survey_zones_state` groups by tag (not proximity) and works inside the
  Glaciem envelope; zone-tagged marks drop out of `survey_pockets` +
  `annotate_glaciem_survey`; barren-zone status + down-rank; centroid/radius/
  negative-cap correctness.
- app: create → active-zone set → armed survey capture stamps `zone_id` →
  `/api/halo/survey/zones` shows the derived geometry → `plan` pins the slug and
  targets it; anywhere (a zone at a ring-void fix plans); rename/close/delete
  lifecycle; system-mismatch guard; existing untagged proximity path unchanged.
- browser: new-zone flow, active banner, zone-centered radar, plan-from-zone —
  via the headless preview harness (as with the barren overlay + radar).

## 10. Not in scope (fast-follows)

- Promoting a well-surveyed zone to committed constants (the #36 export already
  carries the data; promotion stays manual).
- Multi-zone drop planning / "nearest unmapped gap" suggestions.
- Auto-suggesting a zone when you drop a mark near an existing one.
