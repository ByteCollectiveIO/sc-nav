# Feature backlog

Planned features that are designed but not yet implemented. Each entry captures
the decision so we can pick it up later without re-deriving it.

---

## 1. Fresh-only observation markers (stale nodes as a reference overlay)

**Status:** done (2026-06-19). Built as designed: `obs_fresh_window_h` meta
setting (default 48h) exposed via `/api/settings`; a "fresh only" map toggle
(default on) filters both `drawEntities` and the NEARBY render via `isFreshObs`
in `server/static/index.html`; observations placed this browser session
(`sessionObsIds`) always show; the ore heatmap is untouched. Admins tune the
window from the ORG SETTINGS panel. No schema change.

### Problem

Resource nodes and fauna in Star Citizen are **ephemeral** — the game respawns
them into new locations over time, so they are not static. We currently draw
*every* observation as a point marker on the map. Old markers are misleading:
they imply "go here to find it," but SC has almost certainly moved/respawned the
node since. (SC's exact respawn cadence is unknown / uncalibrated.)

The observations are still valuable in aggregate for "what tends to be in this
area," so we don't want to delete them — just stop presenting stale ones as if
they're actionable.

### Decision

Keep the existing two-tier split and lean into it:

- **Heatmap / resource forecast** — unchanged. Aggregates *all* observations
  (all-time) into grid cells. This is the statistical "what's likely here" view
  and is where stale data belongs. (`/api/resource_cells`, heatmap draw in
  `server/static/index.html` around the `resourceCells` render.)
- **Point markers + NEARBY list** — default to **fresh-only**.
- **"show all" toggle** — reveals the full observation history as a reference
  overlay for the player (today's behavior, on demand). No dimming — it's a
  binary fresh-only vs. show-all toggle, reusing the existing layer-checkbox
  pattern (`index.html` layer checkboxes near the `resources` / `wildlife`
  toggles).

### Freshness rule

An observation is "fresh" if **either**:

- `observed_at` >= now − window, **or**
- it belongs to the **current live session** (your own just-placed nodes always
  show, even if they cross the window).

### Defaults / knobs

1. **Freshness window** — store as a `meta` key/value setting (see
   `db.get_setting` / `set_setting` in `server/db.py`). Default **~48h**, tunable
   as we learn SC's respawn cadence without a code change. The toggle is the
   escape hatch in the meantime.
2. **Current session always fresh** — yes, include it.
3. **Window scope** — start with **one shared window** for both resources and
   wildlife. Split into separate per-category windows later only if it feels
   wrong (wildlife likely turns over faster).

### Implementation sketch

- **No schema change, no DB migration, no data loss.** Every observation already
  ships `observed_at` to the client — both in live state via
  `nav_core._observation_base` and from `/api/observations`. This is a
  display-only change.
- Add a `fresh_only` toggle (default **on**).
- Filter `observations()` in `server/static/index.html` by
  `observed_at >= now − window OR current-session`, applied to **both**
  `drawEntities()` and the NEARBY-list render.
- Surface the freshness window from the `meta` setting (and optionally a small UI
  control) so it can be tuned without a redeploy.

### Relevant code

- `server/static/index.html` — point-marker draw (`drawEntities(observations())`),
  NEARBY render, layer checkboxes, heatmap (`resourceCells`).
- `server/nav_core.py` — `_observation_base` (already includes `observed_at`),
  `search_observations`.
- `server/db.py` — `observations` table (has `observed_at`), `get_setting` /
  `set_setting` for the window value.

---

## 2. Notes on custom POIs (+ surface upstream POI comments)

**Status:** designed, not started.

### Problem

Resources and fauna observations both have an optional `note` field, but
**custom POIs do not** — there is nowhere to record context for a user-created
POI. We want to add notes to custom POIs and display them in the POI table.

### Background: poi.json's `Comment` is never used

`poi/poi.json` is the **upstream catalog**, not user data. It was never moved
into SQLite (only `custom_pois`, `observations`, `handles`, `watcher_tokens`
were — see the `server/db.py` docstring); it's read fresh from file each
startup. Its capital-`Comment` field isn't just un-migrated — it's **never
parsed at all**: `nav_core.parse_data` builds the `Poi` and drops `Comment`. So
no POI (custom or upstream) currently carries a note anywhere in the running
system.

This splits the work into two independent pieces:

- **(a) Custom POI notes** — the real ask. A new editable field, full
  create → DB → display chain.
- **(b) Upstream `Comment` display** — near-free bonus: parse `Comment` in
  `parse_data` into the same note field so existing upstream comments show in the
  table too. **Read-only** (can't persist edits back to the upstream file).

### Decisions

1. **Editable after creation — yes.** Add a `PATCH /api/custom_pois/{id}`
   endpoint for the note plus an inline edit in the table. (Custom POIs are
   currently create + delete only, no edit endpoint — this adds the first one.)
2. **Display in the table — yes.**
3. **Surface upstream `Comment` (b) — yes.**

### Implementation chain

A `note` field threads through five spots; **no data loss, one additive
migration** (existing custom POIs get `note = NULL`).

1. **`server/db.py`** — add `note TEXT` to the `custom_pois` table; add
   `_ensure_column("custom_pois", "note", "TEXT")` migration (mirrors how
   `discord_id` was back-added); update `_CUSTOM_COLS`, `_custom_row_to_dict`,
   `add_custom_poi`. Add an update helper for the PATCH (e.g.
   `update_custom_poi_note(id, note)`).
2. **`server/nav_core.py`** — add `note: str | None = None` to the `Poi`
   dataclass; thread through `custom_poi_from_position`, `custom_poi_to_dict`,
   `poi_from_custom_dict`; add `"note": poi.note` to `_poi_base` (this single
   line surfaces it to both live state and the browse table); in `parse_data`
   set `note=p.get("Comment")` for upstream POIs (the (b) bonus).
3. **`server/app.py`** — add `note: str = ""` to `CaptureIn`, stash in
   `capture_pending`, pass to `custom_poi_from_position` in `_capture_poi`; add
   the `PATCH /api/custom_pois/{id}` endpoint (ownership-scoped, consistent with
   the existing delete guard), updating both the DB and the in-memory
   `nav.pois[id].note`.
4. **`server/static/index.html`** — add a note input to the ADD CUSTOM POI form
   (near `poi-name` / `poi-type`) and include it in the capture POST; show the
   note in the table detail column (`entDetail` currently returns `esc(e.type)`
   for POIs — append the note) and wire the inline edit → PATCH.

### Relevant code

- `server/db.py` — `custom_pois` table + `add_custom_poi` / `_custom_row_to_dict`
  / `_CUSTOM_COLS`; `delete_custom_poi` as the ownership-guard pattern.
- `server/nav_core.py` — `Poi` dataclass, `parse_data` (drops `Comment` today),
  `_poi_base`, `custom_poi_from_position`, `custom_poi_to_dict`,
  `poi_from_custom_dict`.
- `server/app.py` — `CaptureIn`, `_capture_poi`, `/api/capture/start`,
  `/api/custom_pois`, `delete_custom_poi` (ownership scoping to mirror).
- `server/static/index.html` — ADD CUSTOM POI form, `entDetail`, table row
  render, `delSpan` / `wireDelete` (pattern for the inline edit control).
