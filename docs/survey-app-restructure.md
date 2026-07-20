# Survey app restructure — from "Halo Finder + addons" to a cohesive Exploration & Survey app (backlog #38) — design plan

**Status: ✅ SHIPPED v0.73.0 (PR #77, 2026-07-19 — designed, built and
released the same day). Suites 736 green; browser-verified via the preview
harness (DROP/FIELD/ATLAS in both themes, FLY IT handoff, cold `#/halo/field`
deep link, ATLAS zone list + Keeger coverage on Nyx, Prospector launcher card
+ logo). User-confirmed live on the server.**
Frontend information-architecture refactor of the `#/halo` app; zero backend
changes (confirmed — no server diffs). Companion to
[survey-platform.md](survey-platform.md) (#37) — this doc restructures the
*container* the remaining #37 slices (staleness, **import**) will land in.

**Build deviations from the design (2026-07-19):**
- **No last-tab memory** (§6 R1 row): bare `#/halo` always opens DROP —
  restore-magic would make the DROP tab's own `href="#/halo"` bounce back and
  fight predictability. The FIELD nudge dot covers the "come back" case.
- The FIELD pin reuses the full `haloDropHtml` block (legs + drop code +
  fallback) via a `copyId` param, not a slimmed card; DROP keeps the complete
  card including map + alternates (§9.4 resolved: no trimming).
- CSS gotcha worth keeping: `.halo-tab-dot` sets an author `display`, which
  beats the UA `[hidden]` rule — the explicit `.halo-tab-dot[hidden]
  { display: none; }` companion is required (house pattern, see `.seg[hidden]`).

---

## 1. The problem: an app that outgrew its name

The app accreted honestly, one good feature at a time:

- **#31** Halo Finder — drop into an Aaron Halo band (one system, one job).
- **#35** multi-system — Nyx pockets + Pyro fields joined the target picker.
- **#36/#36.1** belt survey + zones — the ⛏ mark flow, zone management, and
  export were appended to the bottom of AFTER THE DROP.
- **#37 slices 0–5** — radar heat layers, value tiers, ore-first routing,
  scan detail, coverage gaps, the overview map: each grafted onto whichever
  panel was nearest.

The result is one ~230-line vertical scroll (`#halo-view`) that interleaves
three different jobs. Today's stack, annotated by job:

| Panel | Contents | Job |
|---|---|---|
| intro | "DROP INTO THE ROCKS" | plan |
| TARGET | system seg · goal seg (band/POI/ore) · band strip / pockets / fields · **overview map + NEXT GAP** | plan **+ org data** |
| START FROM | start POI / live fix | plan |
| SHIP | ship · QD · staging · avoid · **Plan my drop** | plan |
| result | drop card + arm button | plan → field |
| AFTER THE DROP | live verdict · **pocket radar + heat layers** · **⛏ SURVEY block** (zone select/new/export/status · zone banner · **zone ▸ details** · density/ores/salvage mark form · scan detail) | field **+ org data** |

The survey block — arguably now the app's most differentiated capability, the
one that feeds the Keeger planner, the `⛏ Ore` goal, the element finder's
IN THE BELTS section, and the Org Intel Surveying section — is a `fld-label`
row buried below the radar, behind a border-top, inside a panel titled for a
different activity. Zone management (create/switch/export/details — *data
administration*) shares a route-row with the one-tap mark button (*cockpit
action*). The coverage map (an org-data surface) lives inside TARGET (a
planning form). The user's read is correct: it presents as an addon pile, not
a coherent set of tools.

## 2. Three journeys, one scroll

Untangling starts from who is standing in front of the screen:

1. **The miner** ("drop me into good rocks tonight"): pick target/ore → plan
   → jump → verify landing → radar to the rocks → mine. Touches *plan* then
   *field*, in one session, in that order.
2. **The surveyor** (deliberate mapping expedition): create/pick a zone → fly
   → ⛏ mark → drift → ⛏ mark → transcribe scans → check coverage → plan a
   hop to the next gap. Lives in *field*, consults *org data*, uses *plan*
   only as a taxi.
3. **The org officer / analyst** (no live position, maybe not even in game):
   which zones are worth mining hours, what's stale, what did last night's
   expedition add, export for an allied org. Pure *org data*.

The scroll forces journey 3 to hunt through two planning panels, and journeys
1–2 to scroll past each other's UI mid-flight. But note what the journeys
*share*: the field surface. The miner's post-drop verify and the surveyor's
mark loop are the same moment — a live `/showlocation` fix, the radar, the
same pocket. That shared moment is the crux of the split question.

## 3. Should it be a separate app? Options considered

### Option A — two launcher apps: "Halo Finder" (routing) + "Survey" (data)

The Group Finder precedent (#19): LFG split cleanly out of Who's Online into
its own app. Rejected here, because that split worked precisely because the
two loops were independent — a board post doesn't need the roster's state.
Here the loops are coupled in both directions, at the same live moment:

- The **mark flow needs the drop context.** A ⛏ mark wants the live fix, the
  pocket-membership verdict, and the radar you're steering by — the exact
  surface the drop plan armed. A separate Survey app must either duplicate
  the whole AFTER-THE-DROP/radar stack or force the pilot to app-switch
  mid-flight between "where am I" and "mark what I see". Both are worse than
  today's jumble.
- The **planner needs the survey outputs.** Keeger pockets *are* survey
  marks; the `⛏ Ore` goal ranks survey clusters; zone "Plan a drop here" and
  the NEXT GAP chip seed the plan form. Every survey surface wants a one-tap
  path into the planner. Two apps turn each of those into a cross-app jump
  with state handoff.
- Two apps also mean two places whose system seg, live-fix subscription, and
  zone state must stay synchronized over the same WS frames.

### Option B — one app, three-tab masthead (RECOMMENDED)

The Resource Manager precedent (#29): when RM's Goals/Inventory/Blueprints
outgrew one scroll, it got a shared masthead (`rmMast`) with hash-routed tabs
— and it worked (RM cleared the impeccable bar at 35+ post-restructure).
Same shape here: **separate the surfaces, not the app.** Three tabs matching
the three journeys, one shared live-state substrate underneath, plus a rename
that tells the truth about what the app now is.

### Option C — keep the scroll, reorder panels

Promote ⛏ SURVEY to its own top-level panel, move zone management out of the
mark row. Cheapest, and strictly better than today — but it neither fixes
the identity problem (an app named for one band of one system) nor gives the
analyst journey a destination, and the scroll keeps growing as #37's import
and stats slices land. Rejected as the end-state; its panel-boundary choices
are however exactly the move-lists slice R1/R2 below execute.

## 4. Proposed information architecture

### 4.1 The app: rename + one launcher card

The app is no longer a Halo finder; it plans drops into any rock space and
maintains the org's map of it. Rename the launcher card to **Prospector**
(DECIDED — user, 2026-07-19: ~90% of what gets mapped is ore nodes, so the
mining-first name tells the truth; "Halo Finder" survives as the DROP tab's
Stanton heritage, and the cstone credit line stays). Card copy shifts
from "drop out of quantum inside the Aaron Halo" to the three-job pitch:
*plan a drop into unmarked rock space · navigate and survey it live · build
the org's belt atlas*. One card, not two — a second "Survey" card deep-linking
to a tab was considered and declined (launcher clutter; the masthead makes the
tabs discoverable from any entry).

`#/halo` stays the canonical hash (every existing deep link — navigator belt
chip, element finder ore seed, zone links — keeps working). Tabs get
sub-hashes, `#/intel/trading`-style:

| Tab | Hash | Journey | Default when |
|---|---|---|---|
| **DROP** | `#/halo` (default) | miner: plan | cold open |
| **FIELD** | `#/halo/field` | miner+surveyor: live | armed plan / in-belt fix |
| **ATLAS** | `#/halo/atlas` | surveyor+analyst: org data | — |

Masthead = app title + tab row + the **system seg (STANTON | NYX | PYRO)
promoted out of TARGET into the masthead**, because all three tabs are
system-scoped (targets, zones list, export link, maps) and it is the one
control every journey shares. Single source of truth, no per-tab drift.

### 4.2 DROP — plan a drop (today's top half, minus org data)

Intro panel · TARGET (goal seg, band strip, Nyx belts + pocket pickers, Pyro
fields, POI, ore) · **overview map stays here** (its click-to-pin *is* target
picking; it renders coverage tints anyway) · START FROM · SHIP/QD/staging/
avoid · Plan my drop · result card.

The result card gains one affordance: **→ FLY IT**, which switches to FIELD
with the armed plan pinned. Explicit, not automatic — mirrors the trade
planner's plan→run handoff. The existing arm/await-location button (v0.52.2)
moves onto the pinned FIELD copy of the card; the DROP copy keeps the drop
number and legs for pre-jump reading.

### 4.3 FIELD — the cockpit surface (today's AFTER THE DROP, focused)

Pinned armed-plan card (drop number, refine loop) · live verdict
(`halo-after-body`) · pocket radar + heat/window segs · the ⛏ mark form +
scan-detail expander — **verbatim; the one-tap invariant is untouched**.

Zone management leaves; in its place one compact line:
`filing into: KEEGER-EAST ▾ · manage zones →` — the ▾ is the existing zone
select (switching zones mid-expedition stays one tap, in FIELD), "manage"
links to ATLAS. Zone banner stays (it's live feedback on the mark you just
dropped); zone ▸ details moves out.

If a live fix arrives while another tab is open, FIELD's tab label shows a
nudge dot (the `☠ N`-badge pattern) — no auto-switching, the pilot may be
mid-form elsewhere.

### 4.4 ATLAS — the org's belt atlas (today's scattered data surfaces, gathered)

- **Zones**: list with `$$$` value chips + staleness (when §6.1 builds),
  zone banner/detail expansion (timeline, contributors, ore breakdown, RS
  multiples table), create/rename/clear-active, "Plan a drop here" (seeds
  DROP and switches to it).
- **Coverage**: the overview map re-mounted in coverage emphasis + the NEXT
  GAP card + gap-plan chip (same seed-DROP handoff). Same draw functions,
  second mount point — canvases are cheap, duplication is in pixels not code.
- **Export / import**: the export link grows into a proper row (per-system,
  versioned `_meta` note); **#37 §6.2 import lands here** — the
  pending/review queue finally has a home that isn't a cockpit panel.
- Keeger/zone progress lines, org-survey badges, and a link out to the Org
  Intel Surveying section (#37 slice 5) for the stats/leaderboard view —
  intel keeps analytics, ATLAS keeps the working map. (Slice-5 surfaces are
  untouched by this restructure.)

### 4.5 Cross-app entry points (retargeted, not changed)

- Navigator `haloWhereChip` in-belt links → `#/halo/field` (you're already
  in the rocks; the useful surface is the radar).
- Element finder IN THE BELTS "open in Halo Finder" ore seed → `#/halo`
  (DROP, ore goal) — and the seed-application gotcha (same-view hash change
  doesn't re-fire the router, `applyHaloOreSeed`) now must also handle
  cross-*tab* application.
- Org Intel Surveying zone rows → `#/halo/atlas`.

## 5. What does NOT change

- **Zero backend.** No API, schema, solver, or payload changes; `/api/halo/*`
  and the survey endpoints are untouched. This is an index.html-only refactor
  plus copy and docs.
- The ⛏ mark flow's field set and one-tap invariant; the radar; the plan
  solver and result content; all #37 invariants (derived-never-stored, etc.).
- `#/halo` deep links, the credit line, the fansite disclaimer.
- Org Intel's Surveying section stays in Intel.

## 6. Build order (each slice ships alone, browser-verified per house harness)

| Slice | Contents | Risk |
|---|---|---|
| **R1** | Masthead (`haloMast`, rmMast pattern) + system seg promotion + FIELD split: move AFTER THE DROP wholesale into `#/halo/field`, sub-hash routing in the view router, → FLY IT handoff, FIELD nudge dot, last-tab memory (localStorage, deep links win) | router + state plumbing — the big one |
| **R2** | ATLAS assembly: move zone management/detail, second overview-map mount w/ NEXT GAP, export row; FIELD compact zone line; seed-DROP handoffs from zone/gap chips | mostly moves; seeding across tabs |
| **R3** | Rename: launcher card (name, copy, logo asset), masthead title, intro copy, retargeted cross-app links, docs (CLAUDE.md map, README index, product-overview) | copy + docs only |

R1 without R2 is already coherent (plan tabs vs field tab; survey block still
carries its zone row until R2 relocates it). R3 lands last so the new name
describes a shape that exists.

## 7. Test plan

- No server-suite changes expected (frontend-only); JS parse smoke + the
  preview harness per slice, both themes: tab switching preserves form state
  and armed plan; live WS fix updates FIELD while DROP is open (nudge dot,
  no focus theft); ore-seed and gap/zone seeding across tabs; deep links
  (`#/halo/field` cold open with no plan → verdict-only state); last-tab
  restore; the same-URL-hash-navigation harness gotcha.
- Impeccable pass on all three tabs after R2 (the app cleared >35 as one
  scroll; each tab must clear it standing alone — empty states now matter:
  ATLAS with zero zones, FIELD with no fix and no plan).

## 8. Risks & mitigations

- **View-router growth**: today `halo` is one boolean among ten; sub-hash
  parsing must not regress the launcher/last-app logic. Precedent exists
  (`#/intel/trading`, `#/blueprints` as RM's third tab) — follow it.
- **Muscle memory**: the org knows the scroll. Mitigation: DROP is the
  default tab so the cold-open experience is unchanged minus the moved
  survey block; the FIELD nudge dot appears the moment a fix arrives.
- **Discoverability regression**: tabs hide what a scroll exposes. The
  masthead names all three jobs on every visit, and the launcher copy (R3)
  sells surveying explicitly — arguably *better* discovery than a
  border-top row below the radar.
- **Docs drift**: CLAUDE.md's view/banner map, memory files, and the #37 doc
  reference `#/halo` sections by their current shape — R1/R3 must update the
  map in the same PR (house rule).

## 9. Open questions (answer before R1)

1. ~~**The name**~~ — DECIDED (user, 2026-07-19): **Prospector**. ~90% of
   what gets mapped is ore nodes, so the mining-first name fits; the slight
   collision with the RSI ship name reads as a feature. Revisit only if
   #37 §7.1 mark kinds (wrecks/ice/gas) ever dominate.
2. ~~**ATLAS scope**~~ — DECIDED (user, 2026-07-19): **system-scoped**,
   following the masthead seg like every other surface; an all-systems
   rollup is an Org Intel job.
3. ~~**FIELD entry on armed plan**~~ — DECIDED (user, 2026-07-19): explicit
   **→ FLY IT** button only, no auto-switch (trade-run precedent); revisit
   after an evening of real use.
4. Does the DROP result card keep the full leg list once FIELD pins a copy,
   or slim to the drop number? (Cosmetic; decide in R1 review.)

## 10. Not in scope

- Any backend or API change; any change to mark/zone/export payloads.
- Relocating the Org Intel Surveying section.
- The remaining #37 slices themselves (staleness, import, kinds) — this doc
  only builds the shelf import will sit on.
- A standalone second launcher card (declined, §4.1).
