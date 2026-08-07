# Feature backlog

The working list of what's **next, small, or parked** — not a history book.
Consolidated 2026-07-04 (as of **v0.36.0**): shipped features are one line in
the [Shipped log](#shipped-log) with a pointer to their spec doc; the full
historical design prose that used to live here is preserved verbatim in
[`archive/feature-backlog-full-2026-07-04.md`](archive/feature-backlog-full-2026-07-04.md).

**How this file works**
- Numbering continues from the historical backlog (#1–25); new items start at #26.
- An active entry captures *decisions*, so it can be picked up without re-deriving.
- When something ships it collapses to a Shipped-log row; its spec doc (if any)
  is the lasting reference. Doc statuses live in [`docs/README.md`](README.md).

---

## Now / next

### 45. `HaloFinderApiTests` depends on test ordering (latent, CI-invisible) 🐞 OPEN
Found during the 2026-08 security review. `HaloFinderApiTests.setUpClass` does
`next(p for p in app.nav.pois.values() if "(ARC-L1)" in p.name and p.qt_marker)`,
but ARC-L1 only enters `app.nav` once **`WikiCatalogTests`** has flipped
`wiki_pois_enabled` and rebuilt the catalog — a global mutation it relies on
from another class. Run the class alone and all 21 of its tests error.

It's invisible on CI because CI pins **Python 3.12**, whose unittest loader
walks classes in *definition* order (`WikiCatalogTests` at ~line 3386 precedes
`HaloFinderApiTests` at ~line 4566). On **3.14** the loader sorts
alphabetically, Halo runs first, and `python test_app.py` fails — so the bug
surfaces the day CI's `python-version` moves past 3.12, or when anyone runs the
suite locally on a newer Python. `pytest` is unaffected (file order).

Fix: make the class self-sufficient — enable the wiki catalog in its own
`setUpClass` and rebuild, rather than inheriting another class's global state.
Worth auditing the other `db.init`-per-class suites for the same assumption.

### 44. Trade planner: sort by return + "Stations & cities" stops 🔨 BUILT

Two follow-ons in one pass. **`sort="return"`** — the option #43 predicted would
be needed once members had a percentage to chase, so the solver optimizes what
they're filtering on. **`stops="no_outposts"`** — a member asked to skip small
surface outposts (suit up, haul boxes across the dirt) while keeping cities,
which load indoors through a freight elevator. Detail in
[`trade-route-planner.md`](trade-route-planner.md).

**The bug worth remembering:** greedy is myopic about a *ratio* in a way it
isn't about a *sum*. Route return is `total_profit / peak_capital`, so appending
a leg with a bigger buy cost re-denominates the whole route — chaining a
30%-on-100k trade after a 200%-on-1k one drops it from 200% to 32%, and the
greedy did exactly that until an accept test stopped it. Profit and per-hour
modes can't lose by adding a positive leg; a ratio objective can.

**"Stations only" was the wrong tool for the outpost preference** and had been
since #34: `place` already carried station/city/outpost, but nothing consumed
`city`, so `stations` threw the five biggest hubs away along with the outposts.
Live: 45 stations · 5 cities · 78 outposts; `no_outposts` plans 1.28M vs 825k
for `stations` at 96 SCU — the cities were carrying real value.

Sort-by-return is deliberately narrow: 874% on 1,005 aUEC of Waste at 96 SCU.
Maximizing a ratio ignores volume by construction. The tooltip says so, TOTAL
PROFIT sits beside RETURN, and the genuinely useful combination remains
*min-return floor + sort by profit/hour*.

### 43. Trade planner: profit margin % + minimum-return threshold 🔨 BUILT

Members wanted a percentage next to the aUEC, and a floor to filter by.
**Definition: return on capital (profit ÷ buy cost)**, not the accounting
profit ÷ revenue — capital is the binding constraint, and the same trade reads
63% one way and 39% the other, so the choice is recorded in
`nav_core.trade_return_pct`. Per-leg + per-route `return_pct`, `min_return_pct`
filtering in `_trade_candidates`, manual legs badged (`low_return`) not dropped,
board honors the same floor. Detail in
[`trade-route-planner.md`](trade-route-planner.md).

Worth not re-deriving: route return is against **`peak_capital`**, not summed
buys (sequential trades recycle the same aUEC); a **held leg has no return**
(cargo already paid for, `buy_cost` 0); and `min_return_pct` is deliberately
NOT merged with the board's older `min_margin`, which is an aUEC/SCU spread —
they answer different questions and both apply.

**The tension is real and intentional:** the solver still optimizes profit/hour,
so the floor buys percentage with absolute money. Live measurements at 96 SCU —
no floor: 60% return / 423k profit · 60% floor: 290% / 49k · 200% floor: 874% /
1,005 aUEC of Waste. Both numbers sit together in the summary so it's visible.
If members start chasing the percentage, the next move is a "sort by return"
option, not a smarter default.

### 42. Trade planner: cargo legality filter 🔨 BUILT

Tester ask: plan over the whole market, **legal goods only**, or **contraband
only**. `legality` on `TradePlanIn`/`TradeReplanIn` (`any|legal|illicit`) reads
UEX's `is_illegal` flag (18 of 204 commodities) through
`nav_core.legality_allows` into `_trade_candidates`; every leg carrying
contraband gets a ☠ badge in *all* modes, because an Any-mode plan is exactly
where you want to spot the leg that draws a scan. Detail in
[`trade-route-planner.md`](trade-route-planner.md).

Two decisions worth keeping: legality is a **preference, not physics** — like
`avoid_poi_ids` and unlike #34's stop exclusion, it filters what you newly buy
and never touches a held-cargo sell leg, so switching to LEGAL mid-run can't
strand contraband already aboard. And an unlisted commodity counts as **legal**
(a new commodity must not vanish from a legal-only plan), while an empty flag
set makes ILLICIT match **nothing** rather than silently returning the legal
market.

**⚠️ Depends on `wiki_pois_enabled`.** Contraband's *sellers* are lawless
outposts (Rat's Nest, The Golden Riviera, Fallow Field), which reach the map
only via the wiki locations catalog (#28) — with it off, every contraband buy
terminal fails the crosswalk, the market looks sell-only, and ILLICIT can never
plan. Verified locally: toggle off → 21/114 terminals resolve and zero
contraband buys; toggle on → 114/114 and a real 2.35M run (Osoian Hides,
Golden Riviera → Devlin Scrap & Salvage). An empty ILLICIT plan says so
(`_explain_empty_illicit`) instead of blaming the filters.

### 41. Trade transaction capture — the log confirms the trade 🔨 BUILT, awaiting live test

[`trade-transaction-capture.md`](trade-transaction-capture.md). Born from the
#40.2 Game.log spike: commodity kiosks log both sides of every trade (total,
unit price, SCU, commodity GUID, shop — verified against a real run). The
watcher now tails them out of the log it already tails and POSTs to
`/api/trade/transactions` (token-auth, on by default, `--no-trade-capture`
opts out); run mode surfaces a "⚡ terminal reported" confirm nudge with the
real numbers pre-filled; confirming teaches the guid→commodity and shop→POI
mappings and files rows into the #39 §6.1 `price_reports` ledger. Server +
watcher + SPA + tests done on the Mac; **needs one live run on the Windows
box** (tail → POST → nudge end-to-end). **The #39 slice-0 planner overlay
followed immediately (v0.87.0)** — org prices now overlay the UEX feed with
`⚡ org` badges. The same lines' **cargo-handling fields** (`autoLoading`,
`boxSize`/`unitAmount`) are now captured into the ledger too (doc §6): nothing
reads them yet, but they're the raw material for a real loading-time model to
replace the flat `STOP_DWELL_S` in every aUEC/hour figure, and unlike price
they can't be reconstructed after the run. Still parked in the doc §4:
auto-confirm, kiosk commodity boards, mission hauling.

### 40.1 Watcher HUD interaction — click-through, focus, in-game target chooser 🔨 I1 BUILT

**Status: I1 built 2026-08-02, awaiting a flight. I2/I3 still design.** Full
plan: [`watcher-hud-interaction.md`](watcher-hud-interaction.md), whose **§11 is
the flight report that pulled I1 forward** and specifies the heavy-mode half.

Two things happened in one session on v0.87.0, and they were linked. The heavy
overlay **ate clicks meant for the game** (swing a tractor-beamed box across the
screen, the cursor crosses the overlay, the beam drops) — §1's first bullet,
reported before the fix existed, and *not* the raw-input problem #40 recorded as
unfixable: that one is the game reading movement it doesn't own, this one is our
window accepting input it should never be a candidate for. Then, after several
of those crossings, **the overlay wedged** and only a watcher restart brought it
back — which **§2.5 had called in advance**: `CalculateNativeWinOcclusion`,
Chromium deciding a window pinned over a fullscreen game need not paint.

Shipped in I1: shared `watcher/sc_nav_win32.py` (one argtypes-correct binding —
the light HUD's re-assert had been a bare `WinDLL` with none), click-through
gated on the game being foreground, auto-hide, focus-change-driven topmost
re-assert; and for heavy mode a **private browser profile** (the only way
`--disable-features=…` is guaranteed to apply — `--app=` handed to a running
browser is forwarded to a process that never saw it), **pid-based window
adoption** and an `IsHungAppWindow` **watchdog** that reopens a wedged window up
to 3×. Root cause of the wedge is unconfirmed; the flags are the hypothesis, the
watchdog is what makes it survivable either way.

Heavy's interaction rule deliberately differs from the HUD's (§11.1): inert
whenever SC is in front, interactive on alt-tab, **no** hold-to-interact — it is
a window you type in, and a momentary key would turn it click-through under an
open dropdown. Hover-polled click-through (§3.2's cursor-in-rect) was **not**
built: the key gate already answers the question (§11.5).

**Replaces #40's
open W2** — W2 was blocked on its own ordering problem ("click-through would
break the F-key drag, so it needs a non-mouse reposition first"), and
hover-polled click-through **dissolves that blocker instead of solving it**: the
window is inert except while the cursor is inside its one rect, so drag survives
unchanged and the corner-presets prerequisite is deleted from the plan.

Scoped by surveying [`sc-overlay`](https://github.com/SubliminalsTV-Projects/sc-overlay)
(Electron, same game, mature). **Techniques and conclusions only — no code
copied**; it is FSL-1.1-MIT, which would not permit vendoring, consistent with
erkul-rejected and Strata-uncommitted.

Decisions worth not re-deriving:
- **Taking focus is the mechanism, not the cost.** #40 recorded that SC reads
  the mouse via raw input regardless of focus (hovering the HUD turns the ship)
  and that alt-tab parks the controls. So a chooser that takes focus **parks the
  ship while you pick**. The first sketch had a no-focus keyboard-driven list;
  that is wrong twice — our key observation would be passive and
  non-consuming, so every keystroke driving the list *also* reaches the cockpit.
  **Failing to return focus is the real danger** (your next WASD types into a
  search box while the ship drifts), so `Escape` + idle watchdog + a taskbar
  entry, because the HUD's `overrideredirect` has no alt-tab handle — which is
  exactly what made §10.1's stuck window unrecoverable.
- **The watcher can't set a destination today.** `POST /api/destination` is
  `require_session` → 401 for a bearer token. Nastier: `/api/pois` takes
  `current_user`, so a token caller gets empty `viewer_owner_ids` and **their own
  survey marks silently vanish from search** — missing rows, not an error.
  `/api/observations` has no route-level dep and works as-is. **This breaks W1's
  "zero new endpoints" property**, deliberately.
- **Search is two calls** — `search_pois` never returns observations; the SPA
  fans out and concatenates. Their sort keys differ (POIs by prefix/length,
  observations by recency), which a 100-row table absorbs and an 8-row HUD list
  cannot: type `quant` and ore nodes fall off the bottom. Cap each side rather
  than interleave — the cap can't drift from the SPA.
- **`GET /api/nav/targets` on chooser-open, not on the heartbeat.**
  `nav_summary`'s docstring forbids growing it with nearest-POI lists. This is
  W3's endpoint through a different door, and W3's volume objection (2 s polling
  vs the single-worker `hub.lock` cliff) **does not apply** to one request per
  open — W3 stays deferred.
- **A separate `Toplevel`, never a taller HUD** — `_freeze_size` exists because
  a window that changes shape in peripheral vision reads as an event.
- **Not a POI browser** — it answers "what is my next target." §13.1's refusal to
  port SPA views into tk holds; a list of strings is fine, filters are heavy
  mode's job.

Also found, unrelated and **out of scope**: heavy mode passes `--app=` and
nothing else, and Chromium's `CalculateNativeWinOcclusion` decides a covered
window need not paint. First suspect if heavy mode ever stalls or holds a stale
frame over the game.

Slices: **I1** inert HUD (watcher-only, no server change) · **I2** auth +
targets endpoint (server-only, fully testable off Windows) · **I3** the chooser.
Split so the unverifiable-off-Windows half and the fully-testable half don't
ship together.

### 40. Watcher in-game overlay — target/distance on the glass ✅ SHIPPED

**Status: SHIPPED + IN-GAME VERIFIED 2026-07-25. Light HUD v0.83.0 (+ two
first-flight fixes in v0.84.0); heavy mode BETA v0.84.0, working after v0.84.1
and v0.84.2.** Six releases in a day; every failure after the first was
Windows-specific and none reproducible on the dev Mac. **The gotchas worth
keeping** (detail in the doc §10.1, §13.7, §13.8): ctypes with no
`argtypes`/`restype` **truncates a 64-bit HWND** — silent, unconditional
failure, check it first in any Win32 work · **`SetWindowPos` fires
`WM_WINDOWPOSCHANGED` even with `NOMOVE|NOSIZE`**, and Chromium kills open
`<select>` popups on it, so a 2 s topmost re-assert closed every dropdown in the
app (an idle keep-alive that pokes a window is not free) · Tk drops the `after`
chain on an unhandled exception, freezing the HUD on a stale distance forever ·
`--app=` reuses a running browser so its PID is meaningless, and a normal tab
shares the window title, so windows are snapshotted before launch. **Known and
NOT fixable: SC reads the mouse via raw input regardless of window focus**, so
hovering the overlay still turns the ship — alt-tab parks the controls; fixing
it needs input interception, which the no-injection rule forbids.
**Remaining: W2** (click-through, opacity/scale, in-space closing/opening,
capture dot) — note click-through would break the F-key drag, so it needs a
non-mouse reposition first. Full plan:
[`watcher-overlay.md`](watcher-overlay.md). Put the navigator's one useful line
— **target · distance · ETA · bearing · staleness** — in a small always-on-top
window over the game, hosted by the watcher (already running on that box,
already authenticated). **Opt-in via a startup question in `run_watcher.bat`,
modeled on the existing handle prompt and sticky in `watcher_config.json`;
first-run default off** (§7) — an unasked-for window over someone's cockpit is
the thing to avoid.

Decisions worth not re-deriving:
- **Network: nothing inbound, no firewall changes** (§4). Both halves are
  client-initiated outbound HTTP to the same host/port/token the watcher already
  uses; the tunnel means no exposed port server-side either. `/ws` is
  cookie-only and stays that way — bearer tokens don't get a WS route for this.
- **The server half is nearly free** (§5.1): `post_position` already builds
  `state_frame()` and returns `{"ok": True}`, discarding it. Returning a lean
  destination slice = zero new requests, zero new endpoints. A polling
  `GET /api/nav/summary` is **slice W3 and conditional** — 2 s polling is 20–30×
  current volume against the known single-worker `hub.lock` cliff, so it ships
  only if W1's ≤60 s browser-retarget lag actually annoys people (§4.3).
- **Three honesty constraints** (§3): exclusive fullscreen defeats an
  always-on-top window (document, don't hook DirectX) · `bearing_deg` is a
  surface bearing and is `None` in space because `/showlocation` gives position
  not attitude — so in space we show closing/opening, not a fake compass ·
  the reading is only as fresh as the last `/showlocation`, so staleness age is
  a first-class element. **Auto-typing `/showlocation` is rejected** (synthetic
  input into a live MMO).
- **Stay a dumb sibling window** — no injection, no memory reads, no synthetic
  input; same class as the Discord overlay.

Real work is the watcher restructure (tk owns main, watch loop to a daemon
thread + queue) and the ctypes click-through, not the data. W1 ≈ most of a day.
Three §12 questions open — first-run default, second-monitor framing, and
whether the org actually flies borderless (worth asking in Discord *before* W1).

### 38. Survey app restructure — Halo Finder → Prospector ✅ SHIPPED v0.73.0

**Status: SHIPPED v0.73.0 (PR #77 squash-merged + auto-tagged + deployed,
user-confirmed live 2026-07-19 — designed + built + shipped same day),
suites 736 green, browser-verified via the preview harness. In-game FIELD
pass (radar/verdict behind the tab switch) still worth a flight.** Build deviations (no last-tab memory; FIELD pin
reuses the full drop block; `.halo-tab-dot[hidden]` CSS gotcha) recorded in
the doc's status header. Full plan:
[`survey-app-restructure.md`](survey-app-restructure.md). The `#/halo` app
accreted #31→#35→#36→#37 into one ~230-line scroll that interleaves three
jobs; the ⛏ survey block — now the app's most differentiated capability —
reads as an addon buried under AFTER THE DROP. Decision: **separate the
surfaces, not the app** (the field loop couples planning and surveying at the
same live moment; survey outputs feed the planner). One app, RM-masthead
tabs (#29 precedent): **DROP** (plan, `#/halo` default) · **FIELD** (armed
plan + verdict + radar + one-tap ⛏ mark, `#/halo/field`) · **ATLAS** (zones,
coverage/NEXT GAP, export — and the future #37 import home,
`#/halo/atlas`); system seg promoted to the masthead; app renamed
**Prospector** (DECIDED — user 2026-07-19: ~90% of what's mapped is ore
nodes). Frontend-only, zero API changes; 3 slices R1–R3, each ships alone.
All §9 pre-build questions DECIDED 2026-07-19 (ATLAS system-scoped; explicit
→ FLY IT, no auto-switch) — **ready to build**; only §9.4 (DROP result-card
trim) rides along in R1 review.

### 37. Survey platform — value, direction, lifecycle, scope 🔨 SLICES 0–5 SHIPPED

**Status: slice 0 (radar reference layers §5.4–5.5, v0.64.0 — Pocket Radar
POI overlay + in-pocket survey heatmap w/ ALL/7D/24H age window), slice 1
(value layer §3.2–3.3, v0.65.0 — $$$ tiers on every survey surface,
per-system tercile pool, salvage ⚙ lane, price-refresh re-tiering), slice 2
(ore-first routing §4, v0.66.0 — `/api/survey/find` + the element finder's
IN THE BELTS section + the halo `⛏ Ore` goal + ⛏ mined-out reports w/ 4 h
age-off) slice 3 (scan detail + zone detail view §3.1/§3.4, v0.67.0 —
after-the-drop scanner transcription via `PATCH .../survey`, the "scanned"
value basis, pool-relative routing comp% term, zone ▸ details timeline),
the v0.68.0 routing fix (arrival plans + staging cost sanity from a live
in-game report, pocket picker parity, radar ☀ sun compass), slice 4
(coverage gaps §5.1 + the always-on 3-system overview map w/ click-to-pin
+ NEXT GAP, v0.69.0) and slice 5 (survey stats + Org Intel Surveying
section §5.2 + Discord `survey` milestones + radar drift nudge §5.3,
v0.72.0) are SHIPPED. Remaining slices (staleness, import, kinds) stay
design.**
Full plan:
[`survey-platform.md`](survey-platform.md). Evolves the shipped #36/#36.1
survey stack from a mapping tool into an org **prospecting** suite, in
independently shippable slices: **(1) value layer** — per-zone `$$$` tiers
from the existing #32 price machinery (ores/density bases work on today's
marks; optional after-the-fact scan detail adds a "scanned" basis); **(2)
ore-first routing — the payoff loop (user's framing: mirror the planetary
element finder)** — pick an ore → ranked high-probability survey clusters
(likelihood shrinkage × travel cost, plannability-gated per the miss-ceiling
lesson) → one tap to a drop plan; element finder grows a DEEP SPACE section
+ halo `⛏ Ore` goal; works on today's marks; **(3) direction** — honest
coverage-gap targeting (plannable vs expedition gaps; a routing miss links
to NEXT GAP), derived survey stats + Org Intel section + Discord `survey`
milestones, radar drift nudge; **(4) lifecycle** — watcher game-build
stamping → automatic staleness badges, cross-org import with a
pending/review queue + dedupe, maintainer promotion tooling; **(5) scope** —
mark kinds (salvage/ice/gas/derelict/hazard w/ Danger Board cross-file),
surface zones (own doc #37.1 before build), value-aware mining circuit.
Invariants: derived-never-stored, one-tap stays one-tap, tiers-with-basis
honesty, no gamification. Build order in doc §9; slices 1+2 need zero new
inputs and complete the survey→mine loop on existing data.

### 36. Belt survey — crowd-sourced field mapping (Keeger first) ✅ BUILT

**Status: built 2026-07-16 (same day as the design), browser-verified
end-to-end (mark → live pocket → plan at 635 m miss → in-pocket verdict);
NOT in-game verified. Suites 376/252 green.** Two build discoveries worth
knowing: Keeger had to join the guarded system-disambiguation ladder (a
hint-less watcher fix at 48 Gm was getting stamped "Stanton" and losing the
mark), and a pocket-miss ceiling (100,000 km) now rejects un-plannable
deep-belt marks with a contract-marker explanation instead of emitting
multi-Gm "drop" cards — with sparse Nyx markers, the drop-plannable sweet
spot is rocks on station approach chords. Full design + build notes:
[`belt-survey.md`](belt-survey.md). The user's idea: players drop one-tap
**survey marks** (custom POIs, `type="survey"` + one JSON payload column —
density incl. first-class "nothing here" negatives, ores, salvage) while
flying unmapped belts; **surveyed pockets go live org-wide from the FIRST
rock mark** (a mark is ground truth — centroid target w/ mark-count
confidence badge; nearby marks merge and refine), feeding the #35
pocket-mode planner as `surveyed` pockets; the statistical field model
(ring width/height/coverage) gates at ~25 marks and is the exportable
artifact — export → review → committed constants for every deployment
(Cornerstone precedent, industrialized).
Bootstrap: Keeger contracts spawn QT markers deep in-belt — any fix taken
there is a measurement. **Prerequisite slice ships alone: Keeger becomes a
named region** (stations ring the belt at exactly 48.000 Gm; wiki live data
confirms `HPP_Nyx_KeegerBelt` mining ~10% + salvage — the #35 doc's "not
physicalized" call was wrong, corrected in its §4). Fits are derived at
nav-rebuild, never stored; no new tables; solver untouched.

### 35. Halo Finder multi-system expansion — Glaciem Ring + Pyro fields ✅ BUILT

**Status: built 2026-07-15 (same day as the design), browser-verified via the
preview harness (Nyx pocket hit 1,463 km staged from Levski; Akiro fly-by
6,258 km via RAB-JAK; Stanton band flow regression-clean). NOT in-game
verified — the design doc's §7 unknowns (do Wtn pockets spawn rocks
contract-free, ring QT obstruction, RMB rock density) still need a flight.**
Suites 367/244 green. Full design + build notes:
[`halo-finder-expansion.md`](halo-finder-expansion.md). Extends #31 to the
**Nyx Glaciem Ring** (circumstellar ring at 15.000 Gm — but only ~4% of the
circumference holds rocks, in 381 datamined pocket containers we already ship
in `containers.json`, so the planner aims chords at **pocket centers**, not a
radius crossing) and **Pyro's 102 unmarked resource fields** (PYR L-points +
RMB sites, coords already in `locations.json`; Akiro Cluster = the PYR1-L3
field). Key decisions: per-system belt registry on `NavData` (`bands` /
`ring+pockets` / `fields`), new pocket mode = POI closest-approach over a
target *set*, `glaciem_contains` joins the system-disambiguation ladder with a
fresh-sticky-beats-geometry rule (Stanton traffic crosses 15 Gm constantly —
regression case pinned in the doc), band mode deliberately NOT offered for
Nyx. **Pyro VI / Pyro V planetary rings don't exist** (researched — lore only)
and the Keeger Belt isn't physicalized yet; both explicitly out of scope. No
new tables/deps/sync tools. In-game unknowns to verify listed in the doc §7.

### 34. Trade planner: stop kinds for big haulers ✅ BUILT

**Status: built 2026-07-12, browser-verified (headless harness, real Hull-C
plan).** A Hull-C has no landing gear — it can *only* moor at a station cargo
dock — and even ships that can land planetside find surface outposts a chore in
a big hull. The planner now takes `stops` = `any | stations | dock`:
`stations` drops planet/moon surface stops, `dock` keeps only stations with a
cargo dock.

The win was that **the data already knew**: uexcorp flags `is_loading_dock` on
exactly five ships (Hull C/D/E, Kraken ×2) and `has_loading_dock` on terminals —
so we didn't need a hand-curated station list. The catch is that the terminal
flag is per *desk*, not per station (Levski declares its dock on "Cargo
Services", never on its commodity desk), so it has to be OR-ed across the
unfiltered feed. Plus a gateway rule (UEX omits the flag on the Nyx-side
gateways; every gateway has a cargo deck). Yields exactly the in-game Hull-C
set of 14 stops. Magnus Gateway is absent from the feed entirely — nothing to
do until UEX carries it.

Design detail worth remembering: `exclude_poi_ids` is a **separate** solver set
from `avoid_poi_ids`, because the held-cargo re-plan deliberately *ignores*
danger (you can run a blockade to offload sunk cargo) but must never ignore
physics (no daring lands a Hull-C on a moon). Full write-up in
[`trade-route-planner.md`](trade-route-planner.md#stop-kinds--the-big-hauler-filter-34--as-built).

**Possible follow-on:** the cargo-hauling planner (#12) has the same problem —
its contract stops are player-entered, so it can't *drop* them, but it could
badge a stop the chosen ship can't use. Not built.

### 33. Scheduled UEX feed refresh (admin-configurable) ✅ BUILT

**Status: built 2026-07-11 with #32, browser-verified.** Before this, uexcorp
feeds (commodities/items/terminals/prices) loaded **once at process startup**;
the only later refresh was the curl-only admin `POST /api/refresh` — prices
were as old as the last deploy. Now `feed_refresh_loop()` (started alongside
the presence broadcaster) re-pulls the feeds on a schedule: org setting
`feed_refresh_h`, **default 6h, hard 2h floor** (be kind to the community-run
UEX API — admins can go longer, never shorter; API rejects <2 with 400, the
reader clamps hand-edited meta rows up), `0` = off, cap 720h. Setting changes
apply without restart (re-read every 5-min tick). Shared `_refresh_feeds()`
body now also backs the manual endpoint — which finally has UI: ORG SETTINGS
"UEX PRICE DATA" panel with interval input, "prices as of" readout
(`feeds_refreshed_at`), and a **Refresh now** button. The scheduled pass
refreshes feeds only (starmap stays manual — it only changes with game
patches). Bonus fix: `/api/refresh` previously dropped per-ship quantum
enrichment (#27) until restart; `_refresh_feeds` re-applies it.

### 32. Ore value badges — "pause and mine, or keep surveying?" ✅ BUILT

**Status: built 2026-07-11, browser-verified via the preview harness.** While
surveying, every place an ore/harvestable name appears in the navigator now
carries a relative-value badge (`$$$`/`$$`/`$`, tooltip = ≈aUEC/SCU sell ref)
so the player knows instantly whether a scanned node is worth stopping for.
Data: the already-cached uexcorp commodities feed — raw ores without their own
sell price fall back to their refined commodity ("Quantainium (Raw)" →
"Quantainium"); those badges carry a trailing **asterisk** (`$$$*`, tooltip
states the refined basis) so raw-vs-refined pricing is never conflated
silently. Genuinely unpriced names (Ice, rubble) get **no** badge rather
than a misleading "low". Tiers are rank terciles *within* each category
(ores vs ores, harvestables vs harvestables; `nav_core.resource_value_tiers`),
so buckets survive patch-day price rebalances and the 23M-aUEC Jaclium outlier
can't squash the scale. New `GET /api/resource_values` (rebuilt on
`/api/refresh`); badges on: resource forecast, NEARBY detail, element-finder
picker options + status line, destination panel, ADD RESOURCE NODE live hint +
capture confirmations. 37/44 ores + 7/10 harvestables badged with today's cache.

### 31. Halo Finder — Aaron Halo drop planner (tenth app) ✅ SHIPPED

**Status: built 2026-07-10 (same day as the design).** Full spec + build notes:
[`halo-finder.md`](halo-finder.md). The tenth app (`#/halo`): pick a density
band (band 5 = the ~3×-dense jackpot) or a deep-space custom POI, get "set
destination X, exit QT at D km" with an enter/peak/exit drop *window*
(+ seconds at your drive's speed), a staging hop when the sun/geometry blocks
every direct chord, the patch-proof star-marker fallback number, and post-drop
`/showlocation` verdicts ("you're in band 5, 12,400 km past the inner edge")
with a Refine loop for POI targeting. Passive extras: the navigator's
"☄ Halo band N" deep-space chip and automatic band annotation on deep-space
captures. Geometry golden-tested against Cornerstone's published chart numbers
(4 fixtures, all ≤5,000 km off); the prereq `_frame_at` Unknown-system fix
(deep-space captures were unroutable) shipped with it.

### 26. SC Wiki API reference-data layer (foundation) ✅ COMPLETE

**Status:** all three slices shipped — vehicles/quantum **v0.37.0**
(`tools/sync_quantum.py` → `poi/quantum_{drives,profiles}.json`, 230 profiles /
57 drives / 81% hauler coverage), blueprints **v0.40.0**
(`tools/sync_blueprints.py` → `poi/blueprints.json`, 1,559 recipes), locations
**v0.46.0 with #28** (`tools/sync_locations.py` → `poi/locations.json`, 634
records). Footer carries the CC BY-SA 4.0 attribution; every artifact is
version-stamped, no runtime API calls, manual per-patch re-run. Kept below as
the wiki-API reference:

`https://api.star-citizen.wiki` (OpenAPI at `/api/openapi`) is a public,
game-version-scoped JSON API — no auth for game data, pagination
`page[size]` ≤ 200, license **CC BY-SA 4.0 with attribution** (unlike erkul's
CC BY-NC-ND, which we rejected). Use **English fields only** (German strings are
BY-NC-SA). It resolves the project's two standing data blockers and opens three
enrichment paths (#27, #25, #28).

**Deliverable:** a sync/distill script (same convention as
[`quantum-data-pipeline.md`](quantum-data-pipeline.md): fetch → distill →
committed `poi/*.json`, **no live runtime calls**), each output stamped with the
game version from `GET /api/game-versions/default` (currently
`4.8.2-LIVE.12030094`). Add a one-line attribution ("Game data:
Star Citizen Wiki, CC BY-SA 4.0") to the site footer/about. Manual per-patch
re-run is the cadence; runtime auto-refresh is deliberately not v1.

Key endpoints (all probed): `/api/vehicles` (290 ships, incl. per-ship `quantum`
block + `fuel` + `cargo_grids` + `insurance` + `uex_prices`),
`/api/vehicle-items?filter[type]=QuantumDrive` (full drive stats),
`/api/blueprints` (1,559 recipes w/ ingredients, craft time, dismantle returns),
`/api/locations/positions?filter[system]=` (x/y/z + `qt_valid` + parent, 809
Stanton entities), `/api/locations/{id}` (per-POI `quantum_travel` radii +
`amenities`), `/api/commodities` (box sizes, mineable/harvestable/salvage flags).

### 27. Quantum fuel & max jump-range (cargo + trade planners)

**Status:** **SHIPPED v0.37.0.** Fuel burn + max-range are
live in both planners: nav_core annotation/summary/`in_range_only`, app.py
`/api/ships` `quantum` enrichment + `_resolve_drive` + solver wiring, and a SHIP-
panel drive picker + in-range checkbox + per-leg fuel + range callout (drive
remembered in localStorage, no DB migration). Unmatched ships degrade gracefully.
Spec + build notes: [`quantum-fuel-range.md`](quantum-fuel-range.md).

Decisions locked: default drive + override picker; max-range as **advisory
warning** with an opt-in "only in-range routes" hard constraint; unmatched ships
degrade gracefully (no fabricated numbers). The original blocker — an early
datamined pass covered only ~49% of hauler ships (that raw mine has since been
removed) — is solved: source the per-ship `quantum` block from `/api/vehicles`
(**95% coverage**, 230/242 spaceships; the 12 missing are drive-less snubs,
correctly absent) and the drive catalog from
`/api/vehicle-items?filter[type]=QuantumDrive`. The SCU/Gm fuel math and JSON
shapes from the design carry over unchanged; only the source is the wiki API.

### 25.1 Craft commissions v1.1 (follow-on to the shipped #25)

**Status: CLOSED — everything build-worthy shipped v0.40.0–v0.44.0** (see the
Shipped log). Spec: [`blueprint-craft-commissions.md`](blueprint-craft-commissions.md)
§10–§12.

- ~~**Member blueprint library** (§10)~~ — **SHIPPED v0.42.0**:
  `member_blueprints` table + "My Blueprints" settings picker; commission board
  shows "⚒ N can craft" + a "✨ Requests I can craft" filter (LFG match pattern).
- ~~**§11 sell-side ripples**~~ — **SHIPPED v0.43.0 + v0.44.0**: canonical
  stat-name autocomplete (§11.2, `/api/blueprints/stat-names` + datalist,
  v0.43.0); `blueprint:` identity for sale/auction listings (§11.3 — market
  picker offers ⚒ recipes, `blueprint_key` stamped on any mode, `kind=blueprint`
  filter finds crafted goods, v0.44.0); auto-estimated stat panel (§11.4 —
  per-slot asks for commissions, uniform-at-Qn for an advertised overall
  quality, assumption stated in-UI, v0.44.0). Still open from §11: plausibility
  nudges (§11.5), price↔quality intelligence (§11.6), numeric stat values
  (§11.7) — post-bedding-in ideas, grab opportunistically.
- ~~**Estimated material cost** (§12)~~ — **SHIPPED v0.43.0**:
  `nav_core.blueprint_material_cost` × market reference prices → "mats ≈" on
  both spec-builder forms, commission cards/detail, crafted-sale detail, and
  the craft-goal header; gem/item inputs degrade to a named *unpriced* list
  (still no per-gem price source). All 1,559 feed recipes price out.
- ~~**Choice-group picker**~~ — **DECIDED SKIP 2026-07-05** (per this item's
  own conditional): the feed's 9 `sel` aspects sit on exactly 3 fringe recipes
  (the Aztalan Legs armor variants, each "pick 2 of 3" over the same slots).
  The manifest lists all 3 options — a slight over-count on those 3 recipes
  only. Revisit if a game patch puts choice groups on recipes players care
  about.
- ~~**Announce name-check**~~ — **SHIPPED v0.43.0**: the WANTED announce
  @-mentions library-matched crafters (poster excluded, capped at 15).

---

## Fast-follows by app

Small, unblocked items harvested (2026-07-04) from every spec doc's
Deferred/Open sections, so they stop hiding in eighteen files. Grab
opportunistically; none is urgent.

- **Trade planner (#21):** teammate-lane-awareness ("someone's already running
  this lane" — needs a presence-side design pass first) · exact B&B "thorough"
  solver option under a ≤4-stop cap · pad-size-vs-ship warning on stops (#28c
  chips already show the stop's max hangar/pad; needs ship size class plumbed
  through `sync_quantum.py` — the uexcorp feed has none) · ~~local price
  overlay~~ ✅ **BUILT v0.87.0** (#39 slice 0, fed by #41's log-derived
  observations instead of typed confirms — see
  [`uex-data-contribution.md`](uex-data-contribution.md) status header).
- **Danger board / routing (#24):** two-waypoint detour fallback (v2.1 — a
  `# v2.1` marker sits at the spot in `nav_core`) · severity-scale + radius
  tuning once the board has real data (partly superseded by #28b).
- **Marketplace (#15):** inventory bridge (list from holdings; one-click list
  surplus from met goals) · price history → "fair price" hint from completed
  deals · WTB saved searches (largely realized by #25) · richer reputation
  (only if abuse appears).
- **Resource Manager (#14):** map→goal badging ("needed for N goals" in the
  finder) · contribution history/leaderboard · goal-met → marketplace bridge.
  (Recipe-BOM goal seeding + personal goals shipped v0.42.0, #14.2; ship-BOM
  templates still open.)
- **Events (#13/#20):** POI-linked event location (autocomplete exists; still
  stores freeform text) · recurring events via a "clone event" shortcut ·
  attendance / organizer leaderboard · per-user timezone setting ·
  edit/start-time-change notifications.
- **Cargo planner (#12):** start-from-chosen-POI (`start_id`) + free start ·
  contract-selection advisor (reward-per-hour is already captured) · per-leg
  drive-accurate ETA (lands with #27).
- **Identity / profiles (#17/#30):** member-facing directory surface (opt-out
  already honored; #30's playstyle tags now make it genuinely useful) ·
  directory avatars (hash captured; rendering is one CDN call) · LFG
  ✨-suggested-matches weighting persistent profile tags, so matching works even
  when a member hasn't set a transient activity · `PLAYSTYLE_TAGS` vocabulary
  governance (custom org tags as a setting) if orgs ask.
- **Notifications (#18):** auction "ending soon" ping (needs a scheduled loop) ·
  goal milestone pings at 50/75% (off by default).
- **Platform:** capture-side Discord-id attribution (`owner_id` still =
  `player_id` on capture; deletes are already discord-scoped — the last
  migration tail) · cosmetic handle editing via `PUT /api/me`.

---

## Parked (deliberate, with reasons)

- **#39 UEX data contribution — post prices back to the community feed** —
  [`uex-data-contribution.md`](uex-data-contribution.md), designed 2026-07-25,
  **parked, not built**. Run mode already collects the exact observation UEX's
  submit API wants (the typed price + SCU at the terminal) and throws it away;
  legs already carry UEX's `id_terminal`. Parked because the value is lopsided:
  the doc's **slice 0 — a local price overlay, org-reported prices beating the
  ≤6 h feed scrape for our own planner — is pure win with zero external
  dependency and can be lifted out and built alone**, while the outbound half
  adds a credential store, a privacy-policy change, and a way for one member's
  typo to cost the org its read access to the feed the trade planner runs on.
  Two blockers on the outbound half: new UEX datarunners must attach a
  **screenshot** for 90 days and we're a server-side app that cannot produce
  one (so per-user tokens may simply fail), and the header contract (app
  Bearer key vs per-user `secret-key` vs both) needs ~30 min of verification
  against a real key before any estimate holds. Unpark triggers in doc §7.
- **#22 Refinery job tracker** — real SC pain point but per-player utility, not
  org-oriented.
- **#23 Recognition badges** — liked, but can get tacky fast; revisit with
  restraint (few, earned, tasteful).
- **OCR contract ingestion (#12)** — the only remaining cargo-entry automation;
  brittle across game UI patches, a project in its own right.
- **3D box bin-packing (#12/#21)** — scalar "usable SCU" is good enough.
- **Watcher packaging (.exe)** — stay a Python script until adoption feedback
  says otherwise; PyInstaller + code-signing (~$200/yr) is the plan if revisited.
- **Monetization / CIG permission inquiry** —
  [`monetization-and-deployment.md`](monetization-and-deployment.md); draft the
  CIG ask only if a paid hosting tier becomes real. Non-commercial rule stands.
- **Discord DMs / bot** — webhook-only stands; revisit only if members ask for
  private alerts.
- **Redis / multi-worker** — won't-do at org size; the in-process hub requires a
  single worker (documented loudly in the migration doc).

---

## Shipped log

Everything below is live (deploy = merge to `origin/main`; a git-based Portainer
stack auto-redeploys within ~5 min). Full design/build notes: the spec doc where
listed, else the [archived backlog](archive/feature-backlog-full-2026-07-04.md).

| # | Feature | Shipped | Reference |
|---|---------|---------|-----------|
| — | Multi-user / org migration (OAuth, SQLite, presence, admin) | 2026-06-18 | [multi-user-migration.md](multi-user-migration.md) |
| 1 | Fresh-only observation markers | 2026-06-19 | archive |
| 2 | Custom-POI notes + upstream comments | 2026-06-19 | archive |
| 3 | Dedicated settings page (first hash route) | 2026-06-19 | archive |
| 4 | Custom org logo | 2026-06-19 | archive |
| 5 | Drop ETA readout (keep calc) | 2026-06-19 | archive |
| 6 | Panel reorder (teammates above map) | 2026-06-19 | archive |
| 7 | Harvestables capture | 2026-06-19 | archive |
| 8 | Harvestables forecast/finder/heatmap | 2026-06-19 | archive |
| 9 | Nonce-based CSP (closed the security batch) | 2026-06-30 | archive |
| 10 | Per-shard nodes | 2026-06-20 | archive |
| 11 | Mobile-responsive CSS | 2026-06-20 | archive |
| 12 | Cargo-hauling planner v1 (+ multi-pickup, rewards, guild boards) | 2026-06-21 | [cargo-hauling-planner.md](cargo-hauling-planner.md) |
| 13 | Guild event planner v1 (+ 7-item UI pass) | 2026-06-23/24 · v0.2.x–0.3.0 | [event-planner.md](event-planner.md), [event-planner-todo.md](event-planner-todo.md) |
| 14 | Resource Manager (inventory + goals) | 2026-06-24 · v0.5.0 | [org-inventory-goals.md](org-inventory-goals.md) |
| 15 | Org marketplace (sale/auction/barter) + scale/search pass | 2026-06-25/26 · v0.6.0–v0.7.1 | [marketplace.md](marketplace.md) |
| 16 | Resource Manager v1.1 (units, POI locations, edit, allocations) | 2026-06-25 | [org-inventory-goals.md](org-inventory-goals.md) |
| 17 | Member identity, primary handle & directory | 2026-06-29 | [member-identity-and-directory.md](member-identity-and-directory.md) |
| 18 | Discord notifications (webhook, per-category) | v0.14.0–v0.17.0 | [discord-notifications.md](discord-notifications.md) |
| 19 | Who's online + Group Finder (LFG) | v0.18.0–v0.22.0 | [who-is-online-lfg.md](who-is-online-lfg.md) |
| 20 | Fleet roster / squad organizer (+ seat & group templates) | v0.23.0–v0.24.1 | [fleet-roster-squad-organizer.md](fleet-roster-squad-organizer.md) |
| — | Team-tracking multiplayer fixes · watcher heartbeat | v0.25.0 · v0.26.0 | memory/commits |
| — | Impeccable design sweeps (every surface >35/40) | v0.26.1 · v0.27.0 | `.impeccable/critique/` |
| 21 | Trade Route Planner (solver, run mode, history/stats, favorites, freshness UX) | v0.28.1–v0.33.0 | [trade-route-planner.md](trade-route-planner.md) |
| 24 | Pirate danger warnings v1 + v2 snare-detour routing | v0.34.0 · v0.35.0 | [pirate-warnings.md](pirate-warnings.md), [snare-detour-routing.md](snare-detour-routing.md) |
| — | Launcher reorganization (3 themed groups) | v0.36.0 | PR #13 |
| 26/27 | Quantum data slice (wiki API) + fuel/range in both planners | v0.37.0 | [quantum-fuel-range.md](quantum-fuel-range.md), [quantum-data-pipeline.md](quantum-data-pipeline.md) |
| 21 | Trade planner stock + demand-side reports (STOCK WATCH) | v0.38.0 · v0.39.0 | [trade-route-planner.md](trade-route-planner.md) |
| 25 | Blueprint craft commissions v1 (+ blueprint feed, spec builder, slider-driven quality minimums) | v0.40.0 · v0.41.0 | [blueprint-craft-commissions.md](blueprint-craft-commissions.md) |
| 14.2 / 25.1 | Personal + blueprint-seeded craft goals · member blueprint library · commission crafter-matching (§10) | v0.42.0 (⚒ glyph fix v0.42.1) | [blueprint-craft-commissions.md](blueprint-craft-commissions.md) §10 |
| 25.1 | Craft-goal spec builder (per-slot quality targets) · estimated materials cost (§12) · stat-name autocomplete (§11.2) · WANTED announce pings capable crafters | v0.43.0 | [blueprint-craft-commissions.md](blueprint-craft-commissions.md) §11–§12 |
| 25.1 | `blueprint:` identity for sale/auction listings (§11.3) · expected-stats panel on blueprint-linked listings (§11.4) — closes #25.1 | v0.44.0 | [blueprint-craft-commissions.md](blueprint-craft-commissions.md) §11 |
| 29/30 | Resource Manager restructure (Goals · Inventory · Blueprints peer tabs, library out of Settings, My-holdings default) · member playstyle profile (Settings PROFILE chips → Who's Online + directory) | v0.45.0 | [rm-restructure-and-profile.md](rm-restructure-and-profile.md) |
| 26/28 | Wiki locations catalog: `wiki_pois_enabled` import (241 wiki-only POIs + 206 QT-marker promotions → 508 QT destinations) · per-POI QT arrival radii in run-mode arrival · trade-stop amenity chips (freight elevator / loading dock / hangar-pad / clinic) | v0.46.0 | [wiki-poi-enrichment.md](wiki-poi-enrichment.md) |
| 31 | Halo Finder (tenth app): Aaron Halo band/POI drop planner, staging hops, star-marker fallback, post-drop verify + Refine, navigator belt chip, capture band annotation (+ `_frame_at` deep-space fix) | 2026-07-10 | [halo-finder.md](halo-finder.md) |
| 31 | Halo Finder fixes + map: endpoint-aware obstruction (low-orbit/surface starts, v0.51.1) · HALO MAP system view + drop-zone inset · sticky session system for deep-space ambiguity (v0.52.0) | v0.51.1 · v0.52.0 | [halo-finder.md](halo-finder.md) |
| 31 | Halo Finder deep-space system fixes: `halo_contains` ring-envelope makes `system_at` resolve an in-belt fix to Stanton (v0.52.1) · plan/locate resolve start system confidence-first (container > in-belt > sticky > guess) so a stale sticky no longer false-rejects, "my current location" start now arms→awaits next /showlocation (v0.52.2) — user-verified in-game | v0.52.1 · v0.52.2 | [halo-finder.md](halo-finder.md) |
