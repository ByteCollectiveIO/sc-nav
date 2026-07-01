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

**Status:** done (2026-06-19). Built as designed. `note TEXT` added to the
`custom_pois` table with an `_ensure_column("custom_pois", "note", "TEXT")`
migration (legacy rows get `note = NULL`); threaded through `Poi`,
`custom_poi_from_position` / `custom_poi_to_dict` / `poi_from_custom_dict`, and
`_poi_base`. `parse_data` now maps upstream `Comment` → `note` (read-only; 251
upstream POIs carry one). `CaptureIn.note` flows through the capture path; a new
ownership-scoped `PATCH /api/custom_pois/{id}` (with `db.update_custom_poi_note`)
edits it and re-broadcasts. UI: note input on the ADD CUSTOM POI form; the table
DETAIL column shows the note and, for custom POIs, an inline ✎ edit (prompt →
PATCH) — chosen over a live in-place input because the table re-renders on every
WS broadcast.

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

---

## 3. Dedicated settings page (move admin/watcher panels off the splash)

**Status:** done (2026-06-19). Built as designed — a client-side hash router
(`#/settings`) in the one SPA, no server change. `<main>` now wraps the splash
panels in `#main-view` and the account-only panels (`#token-panel`,
`#admin-panel`) in `#settings-view`; both wrappers are `display: contents` so
their children stay direct grid items of `<main>` (a `#id[hidden]` rule, more
specific than the bare id, still hides a view). A header `#nav-toggle` link
flips between "⚙ Settings" and "← Navigation", shown only when signed in.
`applyView()` (run from `renderAccount` and on `hashchange`) toggles the views,
bounces anonymous `#/settings` deep-links, lazy-loads `loadTokens()`/
`loadSettings()` on entering settings, and calls `drawMap()` when returning so
the canvas redraws at the right size. Admin-only gate on ORG SETTINGS preserved
(`#admin-panel` hidden for non-admins). Verified with a jsdom harness (23
assertions) against the real `index.html`.

### Problem

The main view is a single-page app: `server/static/index.html` stacks every
panel inside one `<main>`, and the account-only panels are tacked onto the
**bottom** — `#token-panel` (WATCHER TOKEN) and `#admin-panel` (ORG SETTINGS).
This pushes the navigation/finder/map content down and clutters the splash with
controls most members touch rarely. We want those settings on their own page,
reached via a link across the top, leaving the main UI cleaner.

### What moves

- **`#token-panel`** — watcher token generation + the token table
  (`loadTokens`, `/api/tokens` GET/POST/DELETE). Visible to any signed-in member.
- **`#admin-panel`** — ORG SETTINGS (`loadSettings`, `/api/settings`, Discord
  role gate, `obs_fresh_window_h`, etc.). Admin-only.

Both are currently shown by `renderAccount()` in `index.html` (around the
`$("token-panel").hidden = false` / `if (me.is_admin) $("admin-panel")...`
block). The same gating logic moves with the panels.

### Decision: client-side view, not a second HTML file

The app is served as one static SPA (`app.mount("/", StaticFiles(..., html=True))`
in `server/app.py`) and all the auth/WS/account bootstrap lives in `index.html`.
A standalone `settings.html` would duplicate that bootstrap (account header,
session check, `/api/me`). Instead:

- Keep one `index.html`; add a **hash-routed view toggle** (`#/settings` vs the
  default map view). A tiny router shows/hides the main panels as a group vs. the
  settings panels as a group.
- Add a **nav link in the `<header>`** (`index.html` ~line 152, next to the
  `#account` element) — e.g. a "Settings" link and a "← Back" / "Nav" link, or
  toggle the link label by current view. Only render the Settings link when
  signed in (the panels require a session anyway).
- Move `#token-panel` and `#admin-panel` markup into a `#settings-view`
  container; wrap the existing panels in a `#main-view` container so the router
  can flip them. No server route change — StaticFiles still serves the one file;
  the `#` fragment never hits the server.

(If a real separate URL is preferred over a hash route later, add a
`/settings` FastAPI route returning the same `index.html` and switch on
`location.pathname` — but the hash route avoids touching `app.py` entirely.)

### Notes / gotchas

- **Canvas sizing:** the map canvas guards against a zero-size canvas when its
  panel is hidden (see the resize guard ~`index.html` comment "Guard against a
  zero-size canvas"). When returning from the settings view, trigger a
  recenter/resize so the map redraws at the correct size.
- **Lazy load:** call `loadTokens()` / `loadSettings()` on entering the settings
  view (or keep the current load-on-sign-in) — don't fetch settings for members
  who never open the page.
- Preserve the admin-only gate: non-admins see the watcher-token section but not
  ORG SETTINGS, exactly as today.

### Relevant code

- `server/static/index.html` — `<header>` (~line 152, add nav link); `#account`
  (~156); `#token-panel` (~360) and `#admin-panel` (~374) markup to relocate;
  `renderAccount()` (~1322) and the `token-panel`/`admin-panel` `.hidden`
  toggles; `loadTokens()` / `loadSettings()` (~1331); the map canvas zero-size
  resize guard.
- `server/app.py` — `app.mount("/", StaticFiles(..., html=True))` (only touched
  if we opt for a real `/settings` route instead of the hash route).

---

## 4. Custom org logo (displayed alongside the Org Navigator logo)

**Status:** done (2026-06-19). Built as designed. Stored on the `/data` volume
at `BRANDING_DIR/org_logo.<ext>` with the extension recorded in the `meta` table
(`org_logo_ext`); no schema change. Three routes in `server/app.py`:
`GET /api/org-logo` (require_session, `FileResponse`, 404 if none),
`POST /api/org-logo` (admin, `UploadFile` validated by Content-Type against
PNG/JPG/WebP, 2 MB cap, replaces any prior file so no orphans) and
`DELETE /api/org-logo` (admin). `org_logo` bool added to both `/api/settings`
and `/api/me`. Frontend (`server/static/index.html`): header logo wrapped in a
`.logos` flex row with a `.logo-sep` divider and `#org-logo` shown next to the
built-in logo for every signed-in member (`applyOrgLogo` in `renderAccount`);
admin BRANDING section in ORG SETTINGS with file input / preview / remove,
driven by `applyOrgLogoAdmin` from `loadSettings`. Added `python-multipart` to
`requirements.txt` (FastAPI needs it for `UploadFile`) and `poi/branding/` to
`.gitignore`. Verified with a TestClient lifecycle check (404 → reject bad type
→ upload → byte-exact serve → no-orphan on re-upload → delete → 404).

### Problem

The server is built to be self-hosted by individual guilds, many of which have
their own logos. Admins should be able to upload their guild's logo to display
**next to** the built-in Org Navigator logo — never replacing it.

### Key constraint: where the file can live

The built-in logo lives in `server/static/images/sc_org_navigator_logo.png`,
served by the `StaticFiles` mount (`app.py:1418`). That directory is baked into
the Docker image, so an upload written there is **lost on the next image
rebuild**. The only writable, persisted location is the `/data` named volume
(`SC_NAV_DATA`, `app.py:35`) — the same volume that holds `sc_nav.db`. So the
upload is stored on the volume and served by a real route, not the static mount.

### Decisions

1. **Placement: header only.** Shown next to `.header-logo` after sign-in. The
   login card is *not* used, so `GET /api/org-logo` can stay behind
   `require_session` — no pre-auth exposure of whether an org logo exists.
2. **File types: raster only (PNG / JPG / WebP).** No SVG — avoids the XSS
   surface of serving an admin-uploaded SVG.
3. **No DB schema change.** Reuse the `meta` key/value table via
   `db.get_setting` / `set_setting` (e.g. `org_logo_ext = "png"` records both
   presence and extension).

### Implementation sketch

- **Storage:** save to `DATA_DIR / "branding" / "org_logo.<ext>"` (mkdir on
  first upload). One `meta` key `org_logo_ext` tracks presence + extension.
- **`server/app.py`** — three routes following the `require_admin` /
  `require_session` patterns:
  - `POST /api/org-logo` (admin) — `UploadFile`; validate content-type against
    PNG/JPG/WebP, cap size (~1–2 MB), write to the volume, set `org_logo_ext`.
  - `DELETE /api/org-logo` (admin) — delete the file + clear the meta key.
  - `GET /api/org-logo` (require_session) — `FileResponse` of the current file,
    404 if none.
  - Add `"org_logo": bool(get_setting("org_logo_ext"))` to the `/api/settings`
    payload (`get_settings`, app.py:1157) so the frontend knows to render it.
- **`server/static/index.html`** —
  - Header: wrap `.header-logo` (index.html:228) in a flex row; when
    `settings.org_logo` is true, append an `<img src="/api/org-logo">` sized to
    match (~40px). Cache-bust with a `?t=` on upload/delete.
  - ORG SETTINGS panel (`#admin-panel`, index.html:458): add a file input +
    current-logo preview + "Remove logo" button, wired to POST/DELETE and
    refreshing `loadSettings()`.

### Relevant code

- `server/app.py` — `DATA_DIR` (l.35), `require_admin` (l.418), `require_session`,
  `get_settings` / `update_settings` (l.1154–1208), `StaticFiles` mount (l.1418).
  FastAPI already provides `UploadFile` and `FileResponse` — no new deps.
- `server/db.py` — `get_setting` / `set_setting` over the `meta` table (l.275).
- `server/static/index.html` — `.header-logo` (l.228), `#admin-panel` (l.458),
  `loadSettings()` (l.1644+), the `settings`/`/api/settings` fetch.
- Docker: `/data` volume in `docker-compose.yml`; `Dockerfile` seeds `/data`.

---

## UI refresh batch (2026-06-19) — tackle in this order

Four UI changes designed together. **5 and 6 are quick, independent layout wins**
(do them first, in either order). **7 is the data plumbing that 8 depends on**, so
7 must land before 8. Net order: **5 → 6 → 7 → 8**.

---

## 5. Drop the ETA readout (keep the calculation)

**Status:** done (2026-06-19). Built as designed. Removed the ETA `.readout` box
and the two `#r-eta` writes in `index.html`; the `auto-fit` grid now balances at
6 boxes. `eta_s` is still computed in `nav_core.py` (and still set on the
destination payload) for future use; `fmtEta` is now unused but left in place,
paired with the retained calc.

### Problem

The top readout strip has 7 boxes — CURRENT CONTAINER, BEARING, DISTANCE, ETA,
ALTITUDE, SPEED, LAT/LON. The grid is `auto-fit, minmax(150px, 1fr)`
(`index.html:33`), so 7 boxes wrap to leave a single lonely box (LAT/LON) on its
own row at most widths. Dropping one box → 6, which fills rows evenly and looks
cleaner.

ETA is also the least meaningful readout: position updates are user-driven
(manual `/showlocation` runs at irregular intervals), not a constant-speed feed,
so the ETA value is misleading. Remove the **display** but **keep the
calculation** so we can resurrect it later.

### Decision

Display-only removal. The server keeps computing `eta_s`.

### Implementation

- **Keep** `eta_s` in `server/nav_core.py` (l.491 / l.498) untouched — it stays
  in the destination payload for future use.
- `server/static/index.html`:
  - Remove the ETA `.readout` box markup (l.266).
  - Remove the two lines that write it: `$("r-eta").innerHTML = fmtEta(d.eta_s)`
    (l.787) and the `r-eta` clear in the else branch (l.789, which sets
    `r-bearing`/`r-dist`/`r-eta` to `—` together — drop just the `r-eta` part).
  - `fmtEta` (l.667) becomes unused; leave it (cheap, paired with the retained
    calc) or delete it — either is fine.

### Relevant code

- `server/nav_core.py` — `eta_s` compute (l.491, l.498), **keep**.
- `server/static/index.html` — `.readouts` grid CSS (l.33), ETA box (l.266),
  `r-eta` writes (l.787, l.789), `fmtEta` (l.667).

---

## 6. Swap TEAMMATES above the map / ELEMENT FINDER below it

**Status:** done (2026-06-19). Pure markup reorder in `index.html` — panel order
is now DESTINATION → TEAMMATES → MAP → RESOURCE FORECAST → ELEMENT FINDER →
capture grid → NEARBY. No JS change (panels wired by id; layout follows DOM
order).

### Problem

Current panel order in `<main>` (`index.html`): DESTINATION → **ELEMENT FINDER**
(l.281) → MAP (l.303) → **TEAMMATES** (l.338) → RESOURCE FORECAST (l.347) →
capture grid (POI/Resource/Fauna adds, l.352) → NEARBY. We want teammates up top
near the map, and the finder down with the other resource tooling.

### Target order

DESTINATION → **TEAMMATES** → MAP → RESOURCE FORECAST → **ELEMENT FINDER** →
capture grid → NEARBY.

i.e. teammates display **above** the map; element finder displays **below** the
map and below the resource forecast, but **above** the ADD POI/Resource/Fauna
capture boxes.

### Implementation

Pure markup reordering in `server/static/index.html` — move the
`#finder-panel` block (l.281–301) and `#roster-panel` block (l.338–345) to their
new positions. No JS change: all panels are wired by `id`, and the layout is
document-flow grid items, so order follows DOM order.

- **Watch:** the map canvas has a zero-size resize guard (it recenters when its
  panel becomes visible). Moving the map panel in the DOM doesn't change *when*
  it's shown, so this should be inert — but eyeball the map after the move to
  confirm it still sizes on first render.

### Relevant code

- `server/static/index.html` — `#finder-panel` (l.281), `#map-panel` (l.303),
  `#roster-panel` (l.338), `#forecast-panel` (l.347), `.capture-grid` (l.352).

---

## 7. Add flora/harvestables to the "Add Fauna" box (rename → "Add Fauna & Harvestables")

**Status:** done (2026-06-19). Built as designed. New `harvestable` observation
category in `OBSERVATION_CATEGORIES` (type field `name`); `load_harvestable_names`
(uexcorp `kind=="Natural" && is_harvestable==1` → 10 items) served at
`/api/harvestables`; `POST /api/capture/harvestable`. The Add Fauna &
Harvestables box merges both lists into one datalist and routes each capture by
name-set membership (unknown free-text → wildlife). Harvestables got a full kind
identity (lime `--harvest`, ■ marker/square, badge, map layer, NEARBY filter,
fresh-only) and a Harvestables column on the leaderboard. Verified: loader filter,
app boot, 54 nav_core tests green.

### Problem

We can log fauna (animals) but not harvestable flora/plants. The UEX commodities
dataset (`poi/commodities.json`, loaded in `app.py`) marks these with
`kind == "Natural"` **and** `is_harvestable == 1`. We want them addable from the
same box, which becomes **ADD FAUNA & HARVESTABLES**. There are many, so the box
keeps its type-ahead (already a `<datalist>`, which does prefix search for free).

### Background (correct the current wiring)

- The **ADD FAUNA** box (`#wild-form`, l.408) populates its datalist
  (`#fauna-types`, l.411) from **`/api/fauna`** → `fauna_names` (curated
  `server/fauna.json`), **not** from commodities. (`index.html:1944`.)
- The **ADD RESOURCE NODE** box is the one fed by commodities, via
  `/api/raw_commodities` → `load_raw_commodity_names()` (`is_raw==1`,
  `app.py:218`). So harvestables are a *new* slice of the commodities data, not
  the one already wired into the fauna box.

### Key design point: the box must route to the right category

This box adds one free-text "species / name", but #8 needs to know whether a
given entry is **fauna** (a point marker only) or a **harvestable** (gets
finder/forecast/heatmap treatment). The combined datalist hides that distinction
from the user, so the **client resolves it at capture time**: if the typed name
is in the harvestable set → capture as category `harvestable`; otherwise →
`wildlife`. (Decide tie-break/unknown handling: unknown free-text defaults to
`wildlife`, the current behavior.)

This means #7 should land the harvestable **name list + category routing** even
though the finder/forecast/map payoff only arrives in #8. Until #8, a
`harvestable` observation just renders as a point marker like fauna.

### Implementation

- **`server/app.py`** — add `load_harvestable_names()` mirroring
  `load_raw_commodity_names()` (l.218) but filtering
  `kind == "Natural" and is_harvestable in (1,"1",True)`; expose
  `GET /api/harvestables` (mirror `/api/raw_commodities`, l.926); add the count to
  the `/api/settings` + status payloads alongside `raw_commodities`
  (l.1160, l.1274) if we surface counts.
- **`server/nav_core.py`** — add a `"harvestable"` entry to
  `OBSERVATION_CATEGORIES` (l.686): `type_field`/search on `"name"` (or reuse
  `"species"`), a `_normalize_harvestable`, and a `display_name`. The registry is
  designed so "adding a category is one entry here plus a capture endpoint."
- **`server/app.py`** — add the capture endpoint for `harvestable` (mirror the
  wildlife arm path, l.905–909 / `_arm_observation`).
- **`server/static/index.html`** —
  - Rename the `<h2>` to **ADD FAUNA & HARVESTABLES** (l.408).
  - On load, fetch `/api/harvestables` and merge into the `#fauna-types` datalist
    alongside `/api/fauna` (extend l.1944); keep a client-side `Set` of
    harvestable names for category routing.
  - In the capture POST, choose category `harvestable` vs `wildlife` by membership
    in that set, hitting the matching endpoint.

### Open question for build time

Reuse the single `#wild-form` box (one input, category inferred) **vs.** a small
fauna/harvestable toggle in the box. Recommend **inferred from the name set**
(zero extra UI, matches the "lots of options, just search" ask); add a toggle only
if inference proves ambiguous.

### Relevant code

- `server/app.py` — `load_raw_commodity_names` (l.218), `/api/raw_commodities`
  (l.926), `/api/fauna` (l.932), `_arm_observation` / wildlife arm (l.877, l.905).
- `server/nav_core.py` — `OBSERVATION_CATEGORIES` (l.686),
  `observation_from_position` (l.704).
- `server/static/index.html` — ADD FAUNA box (l.408), `#fauna-types` (l.411),
  datalist load (l.1944).

---

## 8. Track, map, forecast & find harvestables (like the element finder)

**Status:** done (2026-06-19) — forecast + finder + **heatmap** all shipped. The
resource stats (`body_base_rate`, `resource_forecast`,
`resource_cells`, `resource_hotspots`, `resource_ore_names`) were parameterized by
`category` + type field via `_type_of`/`_obs_on_body`, so harvestables reuse the
exact same math on their own data (compositions never pooled). `compute_state`
now emits `harvestable_forecast`; the three finder/heatmap endpoints accept
`?category=` (validated against `resource`/`harvestable`). UI: Element Finder
picker uses Ores/Harvestables optgroups carrying each option's category;
RESOURCE FORECAST shows separate "Ores"/"Harvestables" sections. Harvestable
point-markers already came from #7. **Heatmap (shipped 2026-06-19):** the
`#heatmap-mode` select is now category-aware — "Ores" and "Harvestables"
optgroups, each with "most likely" + per-type options, option values encoded
`"<category>:<name>"` so a selection is unambiguous; `ensureCells` keeps a
per-category cell cache (dual fetch of `/api/resource_cells`), and the legend +
canvas draw the active category's cells. The Harvestables optgroup hides on bodies
with no harvestable sightings. Verified end-to-end against real Daymar data
(forecast/hotspots/type-names/cells), bad-category rejection, JS syntax, and the
full test suite.

**Original plan below (for reference).**

**Depends on #7** (needs the `harvestable` category +
captures flowing in).

### Problem

Once harvestables are being logged (#7), treat them like resource nodes: list
them in the **RESOURCE FORECAST**, make them selectable in the **ELEMENT FINDER**,
and draw them on the **map heatmap** — all the "where do I find this" tooling
that today is hardwired to `category == "resource"`.

### Core decision: parameterize the resource stats by category, don't fork

The forecast/finder/heatmap math (Wilson lower bound, base-rate shrinkage, ring
weighting) lives in `nav_core.py` and is keyed on `category == "resource"` +
`_ore_of` (the `"ore"` data field): `_resource_obs_on_body` (l.996),
`body_base_rate` (l.1030), `resource_forecast` (l.1051), `resource_cells`
(l.1087), `resource_hotspots`, `resource_ore_names` (l.1118).

**Plan:** generalize these to take a `category` + a type-field accessor (ore for
resources, name for harvestables) so harvestables reuse the *exact* same stats on
their own data — no duplicated math. Band/quality is resource-only and only
*displayed*, never used in the likelihood math, so dropping it for harvestables is
clean.

### Keep the probability math within-category

Do **not** pool harvestables and ores into one composition — a cell with
"3 Aphorite + 2 SomePlant" must not read as a 60/40 *ore* mix. Compute ore
composition and harvestable composition **separately**, then present together:

- **Resource Forecast** — show harvestables as a clearly-labeled second
  section/group under the existing ore ranking (or a tab), each with its own
  likelihoods. (`#forecast-panel` l.347, `resource_forecast` render in
  `index.html` ~l.720.)
- **Element Finder** — include harvestables in the picker (`#finder-ore`, l.284),
  ideally `<optgroup>`-separated ("Ores" / "Harvestables"). The results table and
  `resource_hotspots` sort modes (likely/near/value) work unchanged once
  parameterized — though "best value" needs a price source for harvestables (the
  commodity `price_sell`); if that's not readily wired, disable the value sort for
  harvestables initially.
- **Map heatmap** — either a separate harvestable heatmap mode in the
  `#heatmap-mode` select (l.319) or fold into the existing one; recommend a
  distinct mode so ore and flora layers stay legible.

### Endpoints

Mirror the resource endpoints for harvestables (or add a `category=` param to the
existing ones): `/api/resource_ores` (l.948), `/api/resource_hotspots` (l.954),
`/api/resource_cells` (l.938), `resource_forecast` consumer. Parameterizing the
existing routes is less surface area than four new ones.

### Open questions for build time

1. **Forecast presentation** — combined list with category badges vs. separate
   sections vs. a tab. (Recommend separate labeled sections.)
2. **Finder picker** — one dropdown with optgroups vs. a category toggle.
3. **"Best value" sort for harvestables** — wire commodity `price_sell` or disable
   the sort for them at first.

### Relevant code

- `server/nav_core.py` — `_ore_of` (l.992), `_resource_obs_on_body` (l.996),
  `body_base_rate` (l.1030), `_shrunk_composition` (l.1018), `resource_forecast`
  (l.1051), `resource_cells` (l.1087), `resource_hotspots`, `resource_ore_names`
  (l.1118), `OBSERVATION_CATEGORIES` (l.686).
- `server/app.py` — `/api/resource_cells` (l.938), `/api/resource_ores` (l.948),
  `/api/resource_hotspots` (l.954).
- `server/static/index.html` — `#finder-panel` (l.281), `#forecast-panel`
  (l.347), `#heatmap-mode` (l.319), element-finder JS (~l.1366), resource-forecast
  JS (~l.720).

---

## 9. Nonce-based CSP for `script-src` (XSS containment, not just escaping)

**Status:** **DONE 2026-06-30.** Built as designed, closing out the 2026-06-19
security batch (input length caps, host-header pinning, WS origin check,
security-headers middleware, logo magic-byte check). `_csp(nonce)` emits
`script-src 'self' 'nonce-{nonce}'` (no `'unsafe-inline'`); the `security_headers`
middleware mints a fresh `secrets.token_urlsafe(16)` per request onto
`request.state.csp_nonce`; the SPA shell is served by explicit `/` + `/index.html`
routes (registered before the `StaticFiles` mount) that stamp the nonce onto the
one inline `<script>` and set `Cache-Control: no-store` so a cached document can't
pin a stale nonce. `style-src 'unsafe-inline'` left as-is per the scope decision.
New `server/test_app.py` (TestClient) pins the property — body nonce == CSP nonce
(middleware-before-route ordering), no `'unsafe-inline'`, fresh per request,
no-store — wired into CI as its own deps-installing step. Verified: index.html has
exactly one bare `<script>` and zero inline `on*=` handlers.

### Problem / why

The app's XSS defense today is **output escaping only**: every untrusted value
goes through `esc()` in `index.html` before hitting the DOM. Coverage is
currently complete, but it depends on a human remembering to call `esc()` at
*every* sink in *all future code*. One missed interpolation in a new feature is
one stored-XSS hole — and because notes/handles are broadcast to every connected
member over the WebSocket, a single stored payload is effectively wormable across
the whole org.

The security batch already added a CSP (`_CSP` constant + `security_headers`
middleware in `server/app.py`), but it keeps `script-src 'self' 'unsafe-inline'`
because the SPA ships one inline `<script>`. `'unsafe-inline'` means the CSP does
**not** block an injected `<script>` — exactly the gap a nonce closes. Goal: drop
`'unsafe-inline'` from `script-src` and authorize the one legit inline script via
a per-request nonce, so an injected inline script won't execute even if escaping
is ever missed. This is containment underneath the escaping, not a replacement.

### Scope decision: script-src only, leave style-src alone

Do **`script-src`** only. Leave `style-src 'unsafe-inline'` as-is:
- `index.html` has a `<style>` block (~l.7) and static `style="..."` attributes
  in markup (e.g. the capture-form inputs ~l.379/411/431), both of which are
  governed by `style-src` and would need refactoring to CSS classes to noncify.
- NOTE: programmatic CSSOM (`element.style.x = ...`, used heavily by the map /
  heatmap / forecast bars) is **not** governed by CSP — that keeps working
  regardless. Only the `<style>` block + static `style=` attrs need
  `'unsafe-inline'`. Refactoring those out is a separate, larger chore with low
  security payoff (style injection is far weaker than script injection).

So: `script-src` enforced via nonce; `style-src 'unsafe-inline'` stays.

### Why this needs a structural change

A nonce must be unique + unguessable **per response**, so the HTML shell can no
longer be served as a flat static file — it has to be templated per request to
stamp the same nonce into (a) the `<script nonce="...">` tag and (b) the CSP
header. Today the shell is served by the catch-all static mount
(`app.mount("/", StaticFiles(..., html=True))`, last line of `app.py`).

### Implementation sketch

1. **Generate a nonce per request** in `security_headers` middleware
   (`server/app.py`): `nonce = secrets.token_urlsafe(16)`, stash on
   `request.state.csp_nonce`. Build the CSP header from it:
   `script-src 'self' 'nonce-{nonce}'` (drop `'unsafe-inline'`); keep the rest of
   `_CSP` unchanged. Setting the nonce on every response's CSP is harmless even
   for JSON/asset responses (no inline script there to match).
2. **Serve the shell via a route, not the mount.** Add an explicit
   `@app.get("/")` (and any other top-level document path) returning an
   `HTMLResponse`, registered **before** the `app.mount("/", StaticFiles(...))`
   line so the exact-path route wins; the mount keeps serving `/images/*`, etc.
   The SPA uses hash routing (`#/settings`, `#/setup`, `#/leaderboard`), so only
   `/` serves the shell — hash routes never hit the server. Keep `/` public
   (auth_gate already exempts non-`/api` paths — login splash must render
   pre-auth).
3. **Template the nonce into the HTML.** Put `nonce="__CSP_NONCE__"` on the one
   inline `<script>` tag in `index.html` (~l.664), and in the route do
   `html = STATIC_DIR.joinpath("index.html").read_text().replace("__CSP_NONCE__",
   request.state.csp_nonce)`. (Single placeholder, single inline script — verify
   there's still exactly one `<script>` and zero inline `on*=` HTML attributes at
   build time; both were true on 2026-06-19.)
4. **Don't cache the shell.** Set `Cache-Control: no-store` on the `/` route so a
   cached document can't pin a stale nonce (mismatch would dead-script the app).
   The static mount's ETag/Last-Modified no longer applies to `/`.

### Gotchas

- Middleware vs. route ordering: the middleware must run and set
  `request.state.csp_nonce` *before* the route reads it. With Starlette HTTP
  middleware wrapping the routes that holds, but assert it in a test.
- Any future inline `<script>` or inline `on*=` handler will silently break
  unless it carries the nonce — that's the intended discipline, but document it
  so it doesn't surprise the next dev.
- Verify the WebSocket and `/api/*` still work (they carry no inline script; CSP
  nonce is irrelevant to them).

### Test

- The legit inline script executes (page boots) and the response CSP contains a
  matching `nonce-...`.
- An injected `<script>nonce-less</script>` does not execute (manual: paste a
  payload into a note, confirm no alert; the browser console logs a CSP refusal).
- `script-src` no longer contains `'unsafe-inline'`.
- Existing tests still pass; add a TestClient check that `GET /` returns a nonce
  in both the body and the CSP header and they match.

### Relevant code

- `server/app.py` — `_CSP` constant + `security_headers` middleware (added in the
  2026-06-19 batch, near `db.init`); `app.mount("/", StaticFiles(...))` (last
  line); `auth_gate` middleware (non-`/api` paths are public).
- `server/static/index.html` — inline `<script>` (~l.664), `<style>` block (~l.7),
  static `style=` attributes (~l.379/411/431).

---

## 10. Per-shard nodes (hide nodes that aren't on your SC server)

**Status:** done (2026-06-20). Built as designed below.

### Problem

The server aggregates observations from every contributor, but SC players are
spread across different **shards** (server instances). Ephemeral nodes
(resources/fauna) only exist on the shard they were seen on, so a node another
player reported from *their* shard doesn't exist on mine — yet we drew it on my
map as if actionable. There's no in-game API for your shard, so we couldn't tell
nodes apart by server.

### Decision

The watcher PC has SC's `Game.log`, which **does** expose the shard. Tag each
sighting with the shard it was made on and let clients filter to their own.

- **Shard source:** `Game.log` lines `<Join PU> … shard[<id>]` (initial) and
  `<Update Shard Id> New Shard Id: <id>` (re-fires on every shard change, so it
  tracks mid-session rotation). Verified against in-game `r_displayinfo`. The id
  embeds the build number (`pub_use1b_12030094_130`), so shard ids naturally
  self-invalidate across patches. (See the `shard-id-from-game-log` memory.)
- **Scope:** observations only. Custom POIs are geographic (same on every shard),
  so they're never shard-filtered.
- **UI:** a "this shard" map toggle (default on, beside "fresh only") hides nodes
  *known* to be on another shard; ours and untagged/legacy nodes stay visible.
  The session-time staleness heuristic now defers to the definitive shard match
  when both shards are known. The TEAMMATES roster sorts same-shard players first
  and tags them with a green "same shard" chip.

### Relevant code

- `watcher/sc_nav_watcher.py` — `GameLogShardReader` (tails `Game.log`, handles
  truncation), `--game-log` flag (sticky), `shard` added to the position payload.
- `server/nav_core.py` — `Observation.shard_id`, threaded through
  `observation_from_position` / `to_dict` / `from_dict` / `_observation_base`.
- `server/db.py` — `observations.shard_id` column + `_ensure_column` migration.
- `server/app.py` — `PositionIn.shard`, `Session.shard`, stamped in
  `_capture_observation`, exposed on `nav_state["shard"]` and in presence records.
- `server/static/index.html` — `currentShard`, `obsShardState`/`onShard`,
  `shardOnly` + `#shard-toggle`, roster same-shard sort/chip.

---

## 11. Mobile-friendly responsive UI (phones / iPads)

**Status:** done (2026-06-20). Built CSS-only as designed, no server/JS change.
`header` got `flex-wrap: wrap`; the four wide tables (`#finder-results`,
`#search-results`, `#token-table`, `#lb-table`) are wrapped in `.table-x`
(`overflow-x: auto`) and `.table-scroll` (NEARBY) gained `overflow-x: auto`, so
wide rows scroll inside their panel instead of past the viewport. One
`@media (max-width: 640px)` block tightens `main`/`.panel` padding, packs/shrinks
the `.readouts` tiles (44/30px digits → 30/22px), drops `#where` to its own
full-width line, neutralizes the inline `min-width` on the wide inputs
(`input.ti, select.sel { min-width: 0 !important }`), relaxes `.fc-row` from 4 to
3 columns (hides the `n=` count), and shortens the map to 320px. Map touch
already worked — `#map` carries `touch-action: none`. Verified HTML tag balance.

### Original plan below (for reference).

### Problem

The UI trails off the right edge on phones and iPads — much of it isn't visible
without horizontal scrolling. The app is one static SPA
(`server/static/index.html`, ~2180 lines, inline CSS + JS, no build step). It
*already* ships `<meta name="viewport" content="width=device-width,
initial-scale=1">` (l.5) and leans on fluid primitives almost everywhere
(`flex-wrap: wrap` on form/filter rows, `grid-template-columns: repeat(auto-fit,
minmax(...))` on the readouts/capture/leaderboard grids, a centered
`max-width: 1100px` main, the map canvas is `width: 100%`). The foundation is
mostly there — what's missing is small-screen tuning.

### Decision: responsive CSS on the one UI, NOT user-agent detection + a second UI

Do **not** sniff the user-agent and serve a separate mobile UI. That's the
high-maintenance path (two UIs to keep in sync, UA detection that misfires —
iPadOS reports as desktop Safari by default, etc.). Because the layout is already
largely fluid, the right move is a handful of `@media` rules and overflow
wrappers on the **single existing `index.html`**. No server work, no JS
architecture change — almost entirely CSS.

### The actual offenders (why it overflows today)

- **Zero `@media` queries** in the whole file — nothing is tuned for narrow
  screens.
- **Header doesn't wrap** — `header { display: flex }` (l.18) has no
  `flex-wrap`, so logo + title + connection dot + `#where` location + the
  Leaderboard/Setup/Settings nav links + `#account` all sit on one row and push
  off-screen.
- **Tables overflow** — the leaderboard (`#lb-table`), search results
  (`#search-results`), finder results (`#finder-results`), and token table
  (`#token-table`) have no horizontal-scroll wrapper, so wide rows spill past
  the viewport edge.
- **Fixed `min-width` inputs / grids force overflow** — e.g. the shard/rotation
  inputs at `min-width: 220px` (l.515/536) and the `.fc-row` fixed grid
  `130px 1fr 46px 56px` (l.160) are wider than a narrow screen.

### Implementation sketch (≈ half-day to a day, CSS-only)

1. **Header wrap** — add `flex-wrap: wrap` to `header` (l.18); shrink or collapse
   the nav links / `#where` readout on small screens.
2. **Table overflow wrappers** — wrap each `<table>` in an `overflow-x: auto`
   container (or a single `@media` rule giving tables a scroll parent) so wide
   tables scroll within their panel instead of the whole page.
3. **One `@media (max-width: 640px)` block** — collapse the multi-column grids to
   a single column, drop the fixed input `min-width`s to `100%`/`auto`, relax
   `.fc-row` to fewer columns, and tighten panel padding / font sizes.
4. **Map touch** — the map is canvas-based with pan/tap interaction; verify
   pan + node-tap work via touch (it may already work through pointer events —
   check `#map` handlers, the `mapHits` tap targets ~l.1293/1433, and the
   `.dragging` logic) and add `touch-action` if needed to stop the page
   panning while dragging the map.

### Relevant code

- `server/static/index.html` — `<style>` block (l.7+, add `@media` rules here);
  `header` (l.18) + header markup (l.260); the `.readouts` / `.capture-grid` /
  `.lb-cards` auto-fit grids (l.34/43/115); `.fc-row` fixed grid (l.160); tables
  `#lb-table`/`#search-results`/`#finder-results`/`#token-table`; fixed
  `min-width` inputs (l.515/536); map canvas `#map` (l.99) + tap/drag handlers
  (`mapHits` ~l.1293, draw ~l.1433).

---

## 12. Cargo-hauling route planner

**Status:** designed, not built (2026-06-20). Full spec in
[`docs/cargo-hauling-planner.md`](cargo-hauling-planner.md) — this is a pointer
so it isn't lost in the backlog.

### Problem

We've built a guild POI/resource tracker and navigator, but nothing for cargo
hauling. A hauling contract gives a set of pickups and dropoffs (commodity + SCU
at named locations, often multi-pickup/multi-dropoff); players take one or more
and want the most efficient visiting order under their ship's cargo capacity,
then guidance through the run. The locations, distances, and QT logic we already
have make this tractable. (Commodities *trading* is out of scope — UEX owns it.)

### Decision (summary — see the design doc for detail)

Three layers: **Plan** (stateless `POST /api/route/plan` — a Pickup-and-Delivery
solver reusing the via-hop `travel_cost` extracted from `resource_hotspots`),
**Execute** (per-user run persisted in DB, driven by the existing
`destination_id`/`compute_state`/`/ws` guidance loop; confirm-on-arrival package
checklists; live onboard-SCU), and **Learn** (per-user completed-run history →
frequency-ranked quick-picks/priors to ease manual entry, + `#/stats` analytics).

Key decisions: contract atom is a `package = {commodity, scu, from→to}` (encodes
precedence); ship list + stated SCU from the uexcorp vehicles feed (mirrors the
commodities fetch), capacity is a single per-user "usable SCU" override
remembered per ship; arrival prompts confirmation rather than auto-completing;
active runs persist per-user. `Game.log` has **no** contract data (verified), so
entry is manual — OCR of the contract screen is the only remaining automation
path and is deferred.

Because this is the **second app** in a single-app SPA, build step 0 is an **app
shell**: an app launcher at `#/` (Discord gate → launcher → app; also a
future-expansion landing page), the navigator re-parented from the implicit home
to `#/nav`, and `#/route` added as a peer view in `applyView()`. Launcher is home
but skippable via deep links (no silent last-app redirect); Stats/Leaderboard
become app-scoped since the planner has its own analytics.

### Relevant code

- `server/nav_core.py` — via-hop travel model to generalize (`resource_hotspots`
  ~l.1294-1309), geo primitives, `nearest_qt_marker`, `parent_planet`.
- `server/app.py` — uexcorp fetch pattern (`load_raw_commodity_names`,
  `COMMODITIES_URL`, `/api/refresh`), `compute_state` destination loop,
  `/api/position` + `/ws`, `/api/me`.
- `server/db.py` — `CREATE TABLE IF NOT EXISTS` + `_ensure_column` pattern; new
  `user_ships` + `runs` tables keyed on `discord_id`.
- `server/static/index.html` — app launcher + navigator re-parented to `#/nav` +
  new `#/route` view, all branches in `applyView()` (~l.1951).

## 13. Guild event planner

**Status:** designed + **v1 BUILT 2026-06-23** (uncommitted). Full spec in
[`docs/event-planner.md`](event-planner.md) — this is a pointer so it isn't lost
in the backlog.

### Problem

Star Citizen is a social game and this is a guild webapp, but nothing helps
members organize in-game events — raids, mining/salvage ops, meetups, and
especially **survey & exploration expeditions**. Organizers need to post an event
(type, time, start location, roster targets); members need to sign up for the
role(s) they'll fill; each event should track its fill against the targets
(`3/5 players`, `Surveyor 1/3`).

### Decision (summary — see the design doc for detail)

Two layers: **Author/Browse** (CRUD on `/api/events` — create + a calendar/cards
board) and **Signup/Track** (`/api/events/{id}/signup` upsert + a pure
`derive_event_fill` in `nav_core.py`). Third app in the SPA, but the app shell
already exists, so it's one launcher card + an `#/events` view family — no shell
work.

Key decisions: the signup is the atom and carries a **list** of roles; fill math
counts a signup toward *every* role it lists but the headline counts *distinct
players* (the rule the tests pin down). Taxonomy (types/categories/roles) is
**curated in code**, served like `/api/ships`. **Any org member** can create;
organizer-or-admin edits/cancels. Times stored **UTC**, rendered local. v1 is
**web-only** — recurring events, Discord announcements, and attendance leaderboards
are deferred (cheap paths noted in the doc).

The org-specific hook: **Survey Op** and **Exploration** event types feed the
navigator's own dataset, and the four survey roles map 1:1 onto the app's capture
domains — Surveyor→cells/ores/hotspots, Naturalist→fauna/harvestables/biomes,
Cartographer→POIs/position, Pathfinder/Scout→recon.

### Relevant code

- `server/nav_core.py` — add `derive_event_fill` (pure, unit-tested); pattern off
  `derive_run_stats` / `derive_guild_leaderboard`.
- `server/db.py` — `CREATE TABLE IF NOT EXISTS` + `_ensure_column` pattern; new
  `events` + `event_signups` tables keyed on `discord_id`.
- `server/app.py` — `require_session`/`require_admin` + organizer-guard; taxonomy
  served like `/api/ships`; JSON-blob columns as in `/api/route/*`.
- `server/static/index.html` — launcher card + new `#/events` view as branches in
  `applyView()`; calendar/cards CSS in the spirit of `#/stats`.

---

## 14. Org inventory & goals (resource procurement campaigns)

**Status:** **BUILT 2026-06-24** (uncommitted) — full doc
[`docs/org-inventory-goals.md`](org-inventory-goals.md). Fourth app in the SPA
("Resource Manager"); the shared **item catalog** half of a two-app pair with the
[Marketplace](#15-org-marketplace-auec-only-sellauctiontrade).

Built as designed: `catalog.py` (slug-based id synthesis `commodity:`/`ship:`/
`custom:<n>`, feed+custom merge, prefix-ranked search) + `catalog_items`/
`inventory`/`goals` tables (`db.py`) + pure `derive_inventory_rollup` /
`derive_goal_progress` in `nav_core.py` (12 new tests, suite 129 green; overall
fill caps each line at its need so over-supply can't mask a shortfall). Endpoints:
`GET/POST /api/catalog`, `/api/inventory` GET(rollup/mine/goal)+POST(idempotent
upsert per owner+item+location+goal)+DELETE, `/api/goals` CRUD+detail. Inventory
rows + goal line items denormalize item name/unit off the catalog at write time
(picker validates, not a hard FK). Frontend: launcher card, `#/goals` board+
detail+create/edit form (catalog autocomplete line items, priority 1–10 hue chip,
deadline countdown, per-line + overall fill bars, contribute form, per-contributor
breakdown) and `#/inventory` (org rollup table expandable to per-owner/per-
location + "my holdings" with delete). Goal line items deep-link into the navigator
finder (`rmFinderPrefill` → `consumeFinderPrefill`). Logos copied to
`static/images/resource_manager_logo.png`. Member-deletion erases inventory +
anonymizes goals/catalog. Verified over real HTTP (shell, logo, catalog). Auto
met/active flip is lazy on read. **Unreleased — needs /deploy.**

### Original plan below (for reference).

### Problem

The org gathers commodities/resources toward shared goals (e.g. "500 SCU Titanium
+ 300 Laranite to fund the Hull-C"), but nothing tracks **what we hold** or **what
we still need by when**. Members need to log contributions; leaders need fill,
deadlines, and priority at a glance.

### Decision (summary — see the design doc for detail)

Three layers over a shared **item catalog**: **Catalog** (uexcorp commodities +
ships feeds + custom items, served like `/api/ships`), **Inventory** (a *per-member
ledger* — SC has no shared storage, so holdings are attributed pledges, not a
vault), and **Goals** (title + **priority 1–10, 1=high** + **deadline** + JSON
`line_items`). A contribution *is* an `inventory` row earmarked to a goal
(`goal_id`), so there's **one ledger**. Fill math (`derive_goal_progress`,
`derive_inventory_rollup`) is pure + unit-tested, patterned off
`derive_event_fill`. Catalog is the **shared dependency** the marketplace also
needs — **build it first.**

The org-specific hook: each goal line item deep-links into the Resource Navigator's
finder ("where's Titanium near me"), so procurement campaigns hand their shopping
list straight to the org's own map.

### Relevant code

- `server/nav_core.py` — `derive_goal_progress` + `derive_inventory_rollup` (pure,
  tested); pattern off `derive_event_fill` / `derive_run_stats`.
- `server/db.py` — new `catalog_items`, `inventory`, `goals` tables;
  `_ensure_column` migrations; `owner_id`/`discord_id` keys.
- `server/app.py` — catalog served like `/api/ships` over `load_all_commodities` +
  `load_ships` + custom rows; JSON-blob `line_items` as in `events.roles`.
- `server/static/index.html` — launcher card + `#/goals` / `#/inventory` views;
  priority chips + fill bars reuse the event CSS.

---

## 15. Org marketplace (aUEC-only sell/auction/trade)

**Status:** **BUILT 2026-06-25 (v0.6.0)** — full doc
[`docs/marketplace.md`](marketplace.md). Fifth app in the SPA; **sibling** of
[Inventory & Goals](#14-org-inventory--goals-resource-procurement-campaigns),
sharing its **item catalog**. All four build steps landed in one pass: tables,
`derive_auction_state` (+ tests), the `/api/market` endpoint family, and the
`#/market` SPA (board / detail / form). The shared catalog also gained an
**equipment / ship-parts feed** (uexcorp `items_prices_all`, kind `item`,
~2.8k names) so listings aren't limited to commodities + ships — shipped
alongside in the same release window.

### Problem

SC has **no built-in auction house.** Org members want to sell/auction/trade
in-game items among themselves — **aUEC only, never real money** — without leaving
the org's tooling.

### Decision (summary — see the design doc for detail)

A **coordination board**, not an exchange: SC has no escrow/API, so the app records
**agreements** (listing `open → pending → completed` via a dual-confirm handshake);
the actual handoff is in-game on social trust. One `listings` table with a `mode`
discriminator — **sale** (fixed/offer), **auction** (timed bids + buyout), **barter**
(counter-items) — plus a `listing_offers` child for all three response flows.
Auction resolution (`derive_auction_state`) is pure + unit-tested; expiry is lazy
on read (no background job). Listings reference the **shared catalog** from app #14
(build it first). Reputation v1 = a derived completed-deals count. Closed/trusted
for free via the existing one-guild `auth_gate`. **aUEC-only** is a hard constraint
(fan project under CIG IP) surfaced as a persistent banner.

### Relevant code

- `server/nav_core.py` — `derive_auction_state` (+ completed-deals helper), pure +
  tested.
- `server/db.py` — new `listings` + `listing_offers` tables; **`catalog_items`
  shared** with #14; `_ensure_column` migrations.
- `server/app.py` — seller-guard + `require_admin`; UTC `ends_at` stored,
  local-rendered; lazy expiry like the run arrival check.
- `server/static/index.html` — launcher card + `#/market` views; mode chips +
  countdown reuse the event CSS; persistent aUEC-only disclaimer.

### 15.x Future enhancements (planned 2026-06-25)

**Update 2026-06-26 — the next pass shipped four of these (uncommitted, needs
/deploy):** *Suggested market-value price*, *Crafted-item quality annotation*,
*Listing search / filter / sort*, and *Board scalability — UI at volume* are all
**BUILT**. See `docs/marketplace.md` (Deferred + "Scaling the board") for the
shipped details. The rest below stay deferred.

- **Suggested / market-value price** — **BUILT 2026-06-26.** The uexcorp `items_prices_all` feed (now
  backing the equipment catalog) carries `price_buy` / `price_sell` per item, and
  the commodities feed carries commodity prices. Surface a reference **market value**
  on the listing form (+ a one-click "use market price") so a seller can anchor their
  aUEC ask. Cheap path: stamp an optional `price` onto each in-memory catalog item
  (median buy/sell, computed when the feed loads) — no new table; it rides the
  catalog like `unit`. Then prefill the form's price field.
- **Crafted-item quality annotation (SC 4.8 crafting)** — **BUILT 2026-06-26.** As of SC 4.8, crafting
  attaches a **quality** to items — source ores carry a quality **1–1000** in **8
  bands** (Band 8 = premium) that propagates through refining → crafting into the
  finished component's stats (power plants, coolers, shields, weapons). Let a seller
  **annotate quality on a listing** (quality value / band + free-form per-stat
  attributes) so a custom-crafted item can advertise what makes it better. Cheap
  path: a JSON `attributes` blob on `listings` (like `events.roles`), a "Crafted ·
  Qn" badge on the card, a stats table on detail. **Research first:** confirm what a
  crafted item *instance* actually exposes in-game (single quality scalar vs.
  per-stat, band vs. raw 1–1000) before fixing the schema. See `docs/marketplace.md`
  → Deferred for the same notes.
- **Listing search / filter / sort** — **BUILT 2026-06-26.** Today the board only filters by `mode`
  (Sale/Auction/Barter tabs) + a "My listings" toggle; `GET /api/market` already
  takes `?mode=` / `?item=` (exact id) / `?seller=me`. Extend to real discovery:
  - **Text search by item name** *(minimum)* — `?q=` substring (case-insensitive)
    over the denormalized `item_name` on `listings`. SQL `LIKE`, no new data.
  - **Filter by poster** — generalize `?seller=` to any member id (not just `me`),
    and make a listing's seller name a "show all from this seller" link.
  - **Filter by equipment type** — by catalog `kind` (commodity / ship / item /
    custom). The listing stores `item_id`; derive the kind from its prefix
    (`commodity:` / `ship:` / `item:` / `custom:`) — no schema change.
  - **Price range** — `?min_price=` / `?max_price=`. Note the per-mode price is
    *resolved* (sale = `price_auec`; auction = current high bid or `start_price`;
    barter = none), so this filters **after** `_listing_view` derives it, in Python —
    the same post-derivation pattern the events board uses to filter phases.
  - **Sort** — `?sort=` over post time (`created_at`, default desc), expiration
    (`ends_at`, auctions), or resolved price; asc/desc. Pure-SQL for `created_at` /
    `ends_at`; price sort rides the same post-derivation pass as the price filter.

  UI: a search box + a sort `<select>` in the board header, plus optional
  type/price-range controls; mostly query-param plumbing on the existing
  `db.list_listings` (which already has the filter scaffolding) + the `#/market`
  board. See `docs/marketplace.md` → Deferred for the same note.
- **Board scalability — UI at volume** — **BUILT 2026-06-26.** The v1 board renders *every* open listing as
  a card and derives each via a per-listing query (N+1); fine for dozens, not for a
  market with thousands of equipment listings. Plan: server-side paging
  (`?limit`/`?offset` + `total`, "Load more"); search-first navigation (filters
  narrow before paging); **kill the N+1 by denormalizing `sort_price` + `offer_count`
  onto `listings`** so the board is pure SQL read by a lightweight serializer (the
  rich `_listing_view` stays on detail); a dense list/table view alongside the card
  grid; and an "ending soon" strip for perishable auctions. This is the load-bearing
  prerequisite for the search/filter/sort feature above to scale. **Full design
  worked through in `docs/marketplace.md` → "Scaling the board".**

---

## 16. Resource Manager (inventory) — v1.1 fixes & refinements

**Status:** **ALL FIVE BUILT 2026-06-25** (uncommitted, needs /deploy). Five
follow-ups on the shipped Resource Manager (org inventory & goals, v0.5.0).
Built in order 2 → 3 → 4 → 5 as planned. Verified end-to-end over real HTTP
(catalog → log holding w/ unit → contribute as allocation → nested my-holdings →
edit w/ committed-floor guard → cascade deletes → legacy migration); nav_core
suite still 129 green, frontend JS syntax-checked. **Original plan below.**

### 16.1 Logo transparency — DONE 2026-06-24

The `resource_manager_logo.png` / `marketplace_logo.png` had a non-transparent
background. User re-exported both with a clear background; resized to 700px (to
match the other app-chooser logos) and copied into `server/static/images/`. **Note:**
the `?v={{APP_VERSION}}` cache-bust means the new file only reaches users on the
**next version bump** (the URL is unchanged until then — same caching gotcha as the
0.2.x logo saga). So this rides the next release with 16.2–16.5; no standalone
deploy needed.

### 16.2 Pick the unit when logging — DONE 2026-06-25

Built as designed. `catalog.UNITS = (SCU, each, unit, L, mg)` + `catalog.valid_unit`;
`unit` field on `InventoryIn` + `GoalLineIn` (validated, falls back to the catalog
default); a `unitSelect()` `<select>` next to qty in the inventory log form and each
goal line-item row, prefilled from the catalog item's unit on pick, overridable. An
unknown stored unit stays selectable as a head option.

**Problem.** The catalog assigns every commodity-feed item `unit = "SCU"`
(`catalog.feed_items`), but not all items are bulk cargo — fauna parts / harvestables
/ gear are counted **individually**. So "8 SCU of Iron" is right but "8 SCU of
Kopion Horns" is wrong; it should be "8 Kopion Horns (each)". The stored row already
has a `unit` column and the log endpoint already stamps the catalog item's unit — the
gap is that the unit is *fixed by kind* with no way for the member to choose.

**Decision.** Expose a **unit picker** in the log-holding form (and the goal
line-item form), defaulting to the catalog item's unit but **overridable**. Small
allow-list (`SCU`, `each`, `unit`, plus maybe `L`/`mg` if useful) — keep it short.

**Implementation.**
- `server/app.py` — add `unit: str | None` to `InventoryIn` and `GoalLineIn`;
  when present + in the allow-list, use it instead of the catalog default
  (`_resolve_or_400` still resolves name; unit becomes member-chosen). Validate
  against a `catalog.UNITS` allow-list.
- `server/static/index.html` — add a small `<select>` unit control next to the qty
  field in the log form (`renderInventory`) and each goal line-item row
  (`goalLineEditRow`); the catalog picker's `onPick` prefills it from `it.unit`,
  member can change it. Show the chosen unit in the rollup/“my holdings”/goal-line
  displays (already rendered from the row's `unit`).
- Consider per-item unit memory later (remember the unit a member last logged for an
  item) — deferred.

### 16.3 Location field should autocomplete from POIs — DONE 2026-06-25

Built as designed (frontend-only). The inventory log-form location, the goal-detail
contribute-form location, and the inline holding-edit location are each wrapped in a
`.poi-pick` span with `attachPoiPicker(...)`; free text still allowed. No schema
change — `location` stays free text.

**Problem.** The log-holding **location** is a plain free-text input, unlike the
rally-point / event-location / cargo from→to fields, which all autocomplete against
the POI/location dataset via `attachPoiPicker`.

**Decision/Implementation.** Wrap the inventory location input in a `.poi-pick`
span and call `attachPoiPicker(wrap)` on it (free text still allowed, like the event
rally point). Applies to both the `#/inventory` log form and the goal-detail
contribute form's location field. Frontend-only — no schema change (`location` stays
free text). Reuse is one-liner-ish: the picker already exists and is used in
`bindEventForm` (`document.querySelectorAll(".ev-form .poi-pick").forEach(attachPoiPicker)`).

### 16.4 Edit an inventory entry — DONE 2026-06-25

Built as designed. `db.update_inventory(id, fields, updated_at)` (qty/location/note/
unit); `PATCH /api/inventory/{id}` with `InventoryEditIn`, owner-or-admin guard, and
a floor check — qty can't drop below what's committed to goals from that holding.
"My holdings" gained an inline Edit form per row (item fixed; delete + re-add to
change it) that PATCHes and re-renders.

**Problem.** "My holdings" only offers **Remove**. Fixing a typo, adjusting a
quantity, or moving a holding to a different location forces a delete + re-add.
(The POST upsert is keyed on (owner, item, location, goal), so changing the location
or item via POST silently creates a *new* row instead of editing.)

**Decision.** Add a real **edit** path: a `PATCH /api/inventory/{id}` (owner-or-admin)
that updates qty / location / note / unit on an existing row, plus an inline edit
affordance in "my holdings" (mirror the custom-POI inline-edit pattern, backlog #2).
Item identity (`item_id`) edits are out of scope — to change the item, delete + re-add.

**Implementation.**
- `server/db.py` — `update_inventory(id, fields)` helper (UPDATE of the editable
  columns + `updated_at`).
- `server/app.py` — `PATCH /api/inventory/{id}` with an `InventoryEditIn`
  (qty/location/note/unit), owner-or-admin guard like `delete_inventory`.
- `server/static/index.html` — an "Edit" button per row in `renderInvMine` opening a
  small inline form (or prompt-based, matching the custom-POI note edit), PATCH, then
  re-render.

### 16.5 Goal contributions as child allocations — DONE 2026-06-25

Built with **option (b)** — a separate `inventory_allocations` table
`(id, inventory_id FK, goal_id, qty, created_at, updated_at)`; holdings stay clean.
A contribution is now an allocation drawn from a parent holding: `available = qty −
Σ allocations`. New `POST /api/goals/{id}/contribute` finds-or-creates the member's
(item, location) holding and tops it up only if committing more than declared on
hand, then adds/updates one allocation per (holding, goal). `POST /api/inventory`
is holdings-only (no `goal_id`). `derive_goal_progress` / `derive_inventory_rollup`
are unchanged — the goal endpoints feed them allocation-joined rows via
`db.list_goal_contributions()`, and the rollup now counts each holding once (no
double-count). `GET /api/inventory?owner=me` returns holdings with `committed`/
`available` + nested `allocations` (goal title + qty); the `#/inventory` "my
holdings" view renders parent→child with a "free" chip. Deleting a holding withdraws
its allocations; deleting a goal drops its allocations but keeps the holdings;
member-deletion cascades both. **Migration** (`db._migrate_inventory_allocations`,
run in `init`): each legacy `goal_id`-tagged inventory row → a holding (goal_id
cleared) + an allocation of its full qty; idempotent. Verified incl. the migration
on a seeded DB. Doc `docs/org-inventory-goals.md` updated.

**Problem.** Contributing to a goal **inserts a second inventory row** (`goal_id`
set) that is *independent* of the general holding it came from. Example: 50 Kopion
Horns at Baijini Point + donate 30 to a goal → the ledger shows a 50 row **and** a
separate 30-row, with no visible relationship and a misleading double-count in the
rollup (it reads 80 total). The member expects the 30 to be a **portion of** the 50,
shown as a nested commitment, with **20 still available**.

**Decision (the model change).** A goal contribution becomes an **allocation drawn
from a parent holding**, not a free-standing row:

- An inventory **holding** is `(owner, item, location, total qty)` — the parent.
- A goal **commitment** references a parent holding + a goal + a committed qty
  (a child). `committed ≤ total`; **available = total − Σ committed**.
- The `#/inventory` "my holdings" view shows each holding with its child commitments
  nested (parent→children), e.g. `Kopion Horn @ Baijini Point — 50 (20 available)`
  with a sub-row `↳ 30 → "Fund the Hull-C"`.
- `derive_goal_progress` reads the **allocations** (per goal), not the parent rows;
  `derive_inventory_rollup` counts each holding **once** (no double-count) and can
  surface committed-vs-available.
- The goal-detail **contribute** form lets the member pick **which holding/location**
  the commitment draws from (or create a holding if none), capping at available.

**Open design choice for build time (pick one):**
- (a) **`parent_id` on `inventory`** — a commitment is an inventory row with
  `parent_id` + `goal_id`; the parent's available is derived. Fewer tables, but the
  "row is sometimes a holding, sometimes an allocation" overloads the table.
- (b) **separate `inventory_allocations` table** `(id, inventory_id FK, goal_id,
  qty, created_at)` — holdings stay clean; allocations are their own thing. Cleaner
  invariant (Σ alloc ≤ holding.qty), one more table. **Leaning (b).**

**Migration note.** Existing goal-tagged inventory rows (the current "duplicate"
form) need a one-time convert into allocations against a matching/auto-created parent
holding, or a documented reset. Decide at build time; the dataset is young so a
guarded backfill is feasible.

**Touches.** `derive_goal_progress` + `derive_inventory_rollup` (read allocations /
de-dupe), the `inventory` schema (+ `inventory_allocations` or `parent_id` migration),
`/api/inventory` (POST split into "log holding" vs "commit to goal"; the goal
contribute path), and the `#/inventory` + goal-detail UI (nested parent→child render,
available cap). This is the largest of the five and supersedes the current
"contribution = inventory row with goal_id" shortcut documented in
`docs/org-inventory-goals.md` (update that doc when built).

## 17. Member identity, primary handle & directory

**Status:** **ALL FOUR STEPS BUILT 2026-06-29** (uncommitted, needs /deploy). Full design:
[`docs/member-identity-and-directory.md`](member-identity-and-directory.md).

Closes the identity inconsistency where the Marketplace shows no in-game handle
while POIs and the leaderboard do. Introduces a persistent `members` table
(discord_id, display_name, guild_nick, primary_handle, directory_opt_out) upserted
at login, a per-account **primary handle**, a denormalized `listings.seller_handle`
(unverified-allowed, verified computed live from the handle registry), and an
admin-only **member directory** cross-walking Discord display name ↔ in-game
handle(s). Privacy: admin-only, opt-out is cosmetic vs admins (the Discord↔handle
link already exists in the `handles` registry; this surfaces it). Build order:
1) `members` table + login upsert + `_resolve_member_name` rewrite, 2) primary
handle, 3) marketplace `seller_handle`, 4) directory + opt-out.

## Engagement & org-ops batch (2026-06-30) — priority order

User brainstorm 2026-06-30; ranked by user interest. #18–20 scoped (own docs);
#21–23 parked (interesting but lower priority / not org-specific).

## 18. Discord notifications (push integration)

**Status:** scoped, not built (2026-06-30). Full design:
[`docs/discord-notifications.md`](discord-notifications.md). **Priority #1.**

Highest engagement-per-effort: everything shipped so far is pull-only; this makes
the app push to where the org lives. Key constraint: **no bot** (OAuth scopes are
`identify guilds guilds.members.read`). Decision: ship **admin incoming-webhook
URL** (channel posts, ~zero cost) first; defer DMs/bot indefinitely. Fires on
event lifecycle (+ scheduled T-30 reminders via an `events.reminded_at` loop
modeled on `presence_broadcaster`), marketplace offers/confirms (with `<@id>`
mentions), goal-100%, and new hauling records. New `server/notify.py` dispatcher
(threaded, never raises, rate-limited); webhook URL stored masked + validated as a
`discord.com/api/webhooks` URL (anti-SSRF). Build: dispatcher+settings+test-send →
inline event pings → scheduled reminders → marketplace → goals/records.

## 19. Who's online + group finder (LFG / "rally now")

**Status:** scoped, not built (2026-06-30). Full design:
[`docs/who-is-online-lfg.md`](who-is-online-lfg.md). **Priority #2.**

Social glue for spontaneous play. Today's two online signals are insufficient:
`online_count` is faceless, `hub.presence` is **surface-only** + watcher-gated.
Add an identity-bearing, location-optional **online roster** (hub `online` map
keyed by discord_id, WS connect + heartbeat lifecycle like `presence` but not
surface-gated) with member-set **status/activity** ("available", "hauling",
"need 2 for a bunker run") + an "appear offline" consent separate from position
sharing. Plus a first-class **LFG feature**: two-direction entries (**looking for
members** w/ slots vs. **looking to join**), **playstyle tags** (PvE/PvP/FPS/
mining/…), an ephemeral in-memory store (auto-expire, don't overload `events`), a
**connect dashboard** (`#/lfg`) grouping groups-needing-players vs. players-
looking-to-join w/ playstyle filters + Join/Ping, and a per-entry **"announce to
Discord"** opt-in (rate-limited, via #18's webhook). New views `#/online` + `#/lfg`
+ launcher "🟢 N online" / "🔎 N LFG" badges. Build: roster → manual status →
LFG dashboard → Discord announce → suggested matches/promote-to-event.

## 20. Fleet roster / squad organizer (event group planning)

**Status:** **v1 BUILT 2026-07-01** (steps 1–4; ship-seat-templates + saved group
templates deferred to v1.1). Needs `/deploy`. Full design:
[`docs/fleet-roster-squad-organizer.md`](fleet-roster-squad-organizer.md).
**Priority #3.**

Turns "12 people signed up" into an op plan. Additive layer over the event
planner: two new tables — `event_groups` (id, event_id, parent_id for hierarchy,
name, kind=squad|squadron|crew|section|wing, ship, capacity, leader_id) and
`event_assignments` (event_id, discord_id, group_id, slot) — keeping
`events`/`event_signups` untouched. Organizer slots signed-up members into named
units (FPS squads, flight squadrons, ship crews with seats), with capacity +
target-roster (`events.roles`) cross-checks and a per-member "your assignment"
view. Endpoints under `/api/events/{id}/groups|assignments|manifest`; UI is an
unassigned-pool → group-cards board on the event detail. Manifest export → Discord
(#18). v1.1: ship-aware seat templates + saved group templates.

## Parked (lower priority — revisit later)

### 21. Trade-route finder (buy-low/sell-high on live terminal prices)
Pure commodity trading using the UEX feed (complements the *contract*-based cargo
planner): "given my ship SCU + location, top profit-per-SCU routes now." **Parked
— good idea but several non-org-specific tools already do this** (it duplicates
external sites and isn't org-differentiated). Revisit if we want a one-stop hub.

### 22. Refinery job tracker
Log refinery work orders (ore, station, yield, real-world ETA) → countdown →
Discord ping when ready (would lean on #18). Real SC pain point and fits the
Resource Manager. **Parked — not org-oriented** (per-player utility); save for
later.

### 23. Recognition badges
Milestone badges (aUEC hauled, events led) on the member directory. **Parked —
liked but can get tacky fast**; revisit with restraint (few, earned, tasteful).
