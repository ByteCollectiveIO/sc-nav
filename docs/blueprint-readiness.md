# Org blueprint readiness + unlock goals

Status: **🔨 built 2026-09-12 — awaiting dev-server test, unreleased** (rides the #151 hold).

## The org goal this serves

A real org directive: *"Priority One — Recco Battaglia blueprint initiative:
all Industry personnel push Battaglia reputation until the majority hold the
Clearcut / Deluge / Overrun modules and the MISC Enhanced / GOLEM MC-4 ore
pods; then Phase Two, mass production."* Three things make that trackable:

1. **Org blueprint readiness** — every recipe in the feed, who holds it, and
   how it's unlocked (faction + reputation standing, from the structured
   `unlocks` the feed now carries).
2. **An unlock goal** — a goal whose lines are *recipes* and whose progress
   counts *members*, scoped to a playstyle ("Industry" ≈ the `mining`
   tag), with a target of N members or a % of the scope. The board reads
   "Phase Two when the majority is ready" as a real bar; the goals Discord
   channel gets the ping when it crosses.
3. **Gather goals** (existing) for the production phase.

## Data

- `goals.kind` = `materials` (default, unchanged) | `unlock`.
- `goals.unlock_spec` JSON: `{blueprints: [key…], target: {mode: "count"|"pct",
  value: n}, playstyle: tag|null}` (`UnlockSpecIn`; 1–20 recipes that resolve
  in the feed; tag from `PLAYSTYLE_TAGS` or null = every member).
- Progress = `nav_core.derive_unlock_progress(spec, holders_by_key, scope)`:
  scope = members carrying the tag (or all members); per recipe `have` =
  scope members whose library holds it, `needed` = the count, or
  ⌈pct × scope⌉ (min 1); `is_met` when every recipe meets its target;
  `per_contributor` = recipes held per scope member (same shape the board
  already renders); `missing` per recipe = scope members still without it.
- Holders come from `member_blueprints` (`db.blueprint_holders`), scope from
  `members.playstyle_tags`. Nothing new is stored beyond the spec.
- Met detection: the library add (`POST /api/me/blueprints`) re-derives every
  active unlock goal naming that recipe and, on a first crossing, flips it to
  `met` + fires the goals-channel ping (`_notify_unlock_goal_met`). Reads
  still flip lazily like material goals.
- `GET /api/blueprints/readiness`: every feed recipe as a compact row
  `{key, name, cat, manufacturer, size, grade, default, holders, holder_names
  (cap 8), factions[], min_standing}` + `members_total` + per-tag scope sizes.

## UI

- **Blueprints screen** gains a scope seg: **My library** (the table from
  docs/blueprint-library.md) · **Org readiness** — filterable (text, category,
  faction, "nobody holds it") table: Recipe · Maker · Unlocked by (faction ·
  lowest standing) · Holders (names on hover) · select. Rows open the recipe
  card; a sticky bar **Create unlock goal (N selected)** seeds the goal form.
- **Goal form** gains a kind seg: Materials (unchanged) · **Blueprint unlock**
  — recipe list (picker to add), target (N members / % of scope), scope
  (Any member / playstyle tag).
- **Goal board / detail** for unlock goals: 🔓 chip, "% ready", per-recipe
  bars in members, the unlock path per recipe (faction-grouped), who holds
  it / who still needs it, and for the viewer "you hold it ✓" or **+ add to
  my library**. No contribute form (nothing to pledge).
