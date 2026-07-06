# Resource Manager restructure & member profile (backlog #29 + #30)

**Status:** ✅ built 2026-07-05 (A + B together, one release; designed same day).
Both parts landed as specced; the only drift: the Inventory scope preference
persists as `localStorage.rmInvScope`, and the marketplace craftable-filter
empty-state now deep-links `#/blueprints`. Two related workstreams:
**A** — split the Resource Manager's blended UI into distinct **Goals**,
**Inventory**, and **Blueprints** sections (and pull the personal blueprint
library out of Settings, where it never belonged); **B** — a lightweight
**member profile** (preferred playstyles/roles) surfaced as tags in the member
directory and Who's Online roster. A is frontend-only; B adds one column and
one API field. They can ship in either order or together.

---

## Problem

1. **The Resource Manager blends two jobs.** `#/goals` and `#/inventory` are
   already separate hash routes sharing a small `rmTabs` sub-nav, but the app
   presents as one screen headed **"RESOURCE GOALS"** — the launcher card lands
   on it, the tabs sit *inside* the goals panel, and the Inventory screen reads
   as an appendix. Users can't tell at a glance that one section is *targets*
   (what the org wants gathered) and the other is *holdings* (what you have and
   where it's stashed). The "Resource Goals" name is also stale — goals now
   cover craft outputs, gear, anything in the catalog, not just resources.

2. **The personal blueprint library lives under Settings** (`#blueprints-panel`,
   index.html ~2295; JS section `// ---------- my blueprint library (#25.1)`).
   Settings is account plumbing — identity, watcher tokens, admin knobs. A
   library of *things you own* is inventory, and members hunting for it won't
   look in Settings.

3. **Members have no way to say what they like to play.** The org has a shared
   playstyle vocabulary (`PLAYSTYLE_TAGS`, app.py ~1269 — PvE, PvP, FPS,
   hauling, mining, …) used by online status and LFG posts, but nothing
   *persistent* — you can't mark yourself "PvP / FPS / bounty" once and have
   org-mates see it when browsing the directory or the online roster.

## Current state (grounded)

| Piece | Where it is today |
|---|---|
| RM screens | `#/goals` + `#/inventory`, shared `rmTabs()` sub-nav; goals list h2 = "RESOURCE GOALS", inventory h2 = "ORG INVENTORY" with an Org rollup / My holdings scope toggle (defaults to org) |
| Launcher card | "Resource Manager" → `#/goals` |
| Blueprint library | Static `#blueprints-panel` in `#settings-view`; JS `loadMyBlueprints`/`renderMyBlueprints`/`addMyBlueprint`; API `GET/POST/DELETE /api/me/blueprints`; consumed by commission board ("N can craft", craftable filter) and goal seeding ("🎯 Gather") |
| Playstyle vocab | `PLAYSTYLE_TAGS` served at `GET /api/playstyles`; used transiently by online status (`activity`) and LFG tags — never stored per-member |
| Profile-ish state | `members` table: identity + `primary_handle` + `directory_opt_out`; `PUT /api/me` body (`ProfileIn`) carries only `share_presence` |
| Directory | Admin-only `GET /api/intel/directory` → `#/intel/directory` table |

---

## Part A — Resource Manager restructure (#29)

### A1. Rename "Resource Goals" → "Goals"

- Goals list h2 `RESOURCE GOALS` → `GOALS` (both occurrences — list screen and
  detail screen header).
- Launcher card description reworded around the two sections (see A2); the app
  keeps the **Resource Manager** name — that's the umbrella, and the logo/brand
  stays.
- Grep for stray "Resource Goals" strings in copy (intros, notify text,
  docs/product-overview.md).

### A2. Give Goals and Inventory their own identities

Keep **one app, one shared catalog** (that coupling is real: goals fill from
inventory allocations). The fix is presentation, not a split:

- **App masthead**: one compact header panel shared by all RM screens — app
  name + the sub-nav tabs — instead of tabs embedded inside the Goals panel.
  Tabs become peers: **Goals · Inventory · Blueprints**.
- **Per-screen header + intro** under the masthead, so each section states its
  job in one line:
  - **GOALS** — "Procurement targets: what to gather, how much, by when.
    Org goals fill as members log contributions; personal goals stay private."
  - **INVENTORY** — "What you're holding and where it's stashed. Log holdings
    against catalog items and locations; the org rollup sums everyone's
    attributed stock."
  - **MY BLUEPRINTS** — "Crafting recipes you own. Powers 'N can craft' on
    commissions and seeds gather-goals."
- **Inventory defaults to "My holdings"** (the management use case the section
  exists for), with "Org rollup" one click away; remember the last scope in
  `localStorage` like other RM prefs. This is the single biggest "what is this
  for?" fix — landing on the org-wide table read as a report, not a tool.
- Launcher card copy: "Set procurement goals, track your holdings and where
  they're stashed, and keep your craftable-blueprint library."

No route changes for existing screens: `#/goals` and `#/inventory` keep their
hashes (deep links, notify links, and the goal-seed flow all keep working).

### A3. Move the blueprint library into the Resource Manager

New third tab **`#/blueprints`**:

- New `#blueprints-view` container + route in the view router; render the
  existing library UI there (the current panel is static DOM — port it to the
  JS-rendered pattern the other RM screens use, reusing `loadMyBlueprints` /
  `renderMyBlueprints` / `addMyBlueprint` unchanged).
- **Remove** `#blueprints-panel` from Settings outright (no tombstone —
  Settings decluttering is the point; the launcher card + tab make the new home
  discoverable).
- Unchanged consumers, verified: commission-board "can craft" logic is
  server-side off `db.blueprint_crafters`; the goal-composer "＋ my blueprints"
  inline add posts directly to `/api/me/blueprints`; the library's "🎯 Gather"
  button already deep-links `#/goals/new` — now an in-app hop.
- Nicety (cheap, do it): a one-line footer on the Blueprints screen linking to
  the commission board's craftable filter — "see open commissions you can
  craft →".

**Alternative considered:** blueprints as a third scope inside Inventory
("Org rollup / My holdings / My blueprints"). Rejected — recipes have no
quantity/location semantics, and mixing them back into the holdings table
re-creates the blending this pass is removing. A peer tab keeps each section
one-job-one-screen.

### A4. Out of scope for A

- Splitting RM into two launcher apps (shared catalog + goal↔inventory
  allocations make one app correct).
- Any backend change — A is entirely `index.html`.

---

## Part B — Member profile: preferred roles (#30)

### B1. Data + API

- **DB**: `members.playstyle_tags` TEXT (JSON list) via the established
  `_ensure_column` migration pattern; `db.set_member_playstyles` /
  read-through in the members directory load.
- **API**:
  - `GET /api/me` gains `playstyle_tags: [...]`.
  - `PUT /api/me` (`ProfileIn`) gains `playstyle_tags: list[str] | None` —
    validated exactly like LFG tags (app.py ~6433): dedup, allowlist against
    `PLAYSTYLE_TAGS`, cap at **6** (a profile is a signature, not a checklist;
    LFG caps similarly).
  - `GET /api/intel/directory` rows gain `playstyle_tags`.
  - Who's Online roster records gain `tags` (loaded with member prefs the same
    way `status`/`activity` are, so the broadcast path stays cheap — no per-tick
    DB reads).

### B2. Profile UI

A **PROFILE** panel in Settings' "Your account" group, next to IN-GAME
IDENTITY: the shared playstyle vocabulary rendered as toggle chips (same chip
look LFG entry tags use), selection saved via `PUT /api/me`, cap enforced in
UI + server. One line of hint copy: "Shown next to your name in the member
directory and Who's Online."

v1 is tags only. Bio / timezone / preferred ships are explicitly deferred —
each needs its own display surface and moderation thinking; tags reuse an
existing, curated vocabulary and existing chip UI.

### B3. Where tags surface

1. **Member directory** (`#/intel/directory`, admin-only today): a tags column
   of small chips. Note: the directory stays admin-only in this pass; a
   member-facing directory is a separate decision (see open questions).
2. **Who's Online roster** (`#/online`): chips on the roster card under
   name/activity. This is the highest-value surface — it's member-facing today
   and it's where "who wants to do FPS right now?" gets answered.
3. **Fast-follow (not in v1):** LFG suggested-matches (`lfgIsMatch`) weighting
   profile tags, so the ✨ match logic works even when a member hasn't set a
   transient activity.

### B4. Privacy

Tags are self-declared, org-visible-by-design (the whole point is being
found). `directory_opt_out` continues to govern the Discord↔handle *link*,
not tags; an opted-out member's tags still show on the online roster (they set
them, and the roster is already opt-out-able via appear-offline). No new
consent surface needed — but say it in the panel hint.

---

## Test plan

- **A**: existing `test_app.py` suite green (no API surface changes); JS parse
  smoke; hand-check the four RM routes + goal-seed flow + commission
  "can craft" filter + settings no longer shows the panel.
- **B**: `test_app.py` — PUT `/api/me` tag validation (allowlist, dedup, cap,
  clear-to-empty), GET `/api/me` echo, directory + online-roster carry;
  `_ensure_column` idempotence on an existing DB copy.

## Rollout

One minor release is fine (A and B don't touch the same files' hot areas), or
A first if B's roster-surface details need a second look. No data migration
beyond `_ensure_column`; no watcher impact.

## Open questions (flagged, not blockers)

1. **Member-facing directory** — profile tags make a member-visible directory
   (names + handles + tags, opt-out-respecting) genuinely useful for the first
   time. Deliberately out of scope here; park as a #30 fast-follow.
2. **Vocabulary governance** — `PLAYSTYLE_TAGS` is a hardcoded list shared by
   three features once profiles land. Fine at current size; if orgs want custom
   tags it becomes an org setting (parked).
