# Watcher modular HUD — nav, survey, and mining widgets on the glass (#40.2) — exploratory design

**Status: 📐 exploratory design, not built, not committed to.** Written
2026-08-01 at the user's request: *"what might be possible to provide our
navigation, survey, and mining tools in a seamless UI overlay,"* with
[`SubliminalsTV-Projects/sc-overlay`](https://github.com/SubliminalsTV-Projects/sc-overlay)
as the aesthetic reference. This deliberately deviates from the shipped
one-line-HUD trajectory to survey the ceiling; whether to climb toward it is a
separate decision.

Builds on [`watcher-overlay.md`](watcher-overlay.md) (#40, shipped) and
[`watcher-hud-interaction.md`](watcher-hud-interaction.md) (#40.1, designed).
**This is a superset of #40.1, not a rival**: #40.1's I1 (inert HUD) and I2
(auth + targets endpoint) are the foundation slices here, unchanged. Nothing
below makes sense to build before them.

**Licensing, restated:** `sc-overlay` is FSL-1.1-MIT. As with #40.1, everything
taken from it is a *concept* — widget grammar, interaction findings — never
code. What we adopt is re-derived for tk/ctypes.

---

## 1. The idea

W1 shipped one line: your target and how far. #40.1 designs the next honest
step: make it inert, then let it retarget. This doc asks the larger question
the `sc-overlay` survey begs: **what if the glass carried a small constellation
of widgets** — navigation, Prospector drop guidance, survey capture, a mining
RS reference, danger warnings, the active trade leg — each independently
placeable, each opt-in, sharing one visual grammar?

The pitch in one sentence: **everything the pilot alt-tabs for mid-flight,
delivered as a widget that already knows.**

What makes this plausible rather than fantasy is that the server already
computes nearly all of it. The SPA's ten apps are fed by endpoints that return
small JSON; the watcher is already authenticated, already ticking, already
rendering. The gaps are (a) auth — almost every wanted endpoint is cookie-only
today (§5), (b) a lean way to fetch per-widget data without re-creating the
polling cliff (§6), and (c) the window/interaction framework #40.1 already
designs (§4).

## 2. What "the sc-overlay look" actually is, and what of it transfers

Their surface qualities, from the survey:

1. **Independent draggable widgets** — position each where your cockpit has
   glass to spare; resize; stack as tabs in shared frames.
2. **Cohesive theming** — sixteen skins, fifteen derived from real SC cockpit
   UIs, auto-switching to match your current ship.
3. **A quiet default** — click-through everywhere except over a widget you're
   deliberately engaging.
4. **Edit mode as a distinct state** — arranging widgets is a different
   activity from flying with them.

What transfers and what doesn't:

| Their quality | Our take |
|---|---|
| Independent draggable widgets | **Adopt** — as N small opaque tk windows, not one canvas (§4.1) |
| Click-through except when engaged | **Adopt** — #40.1 §3 verbatim, generalized from one rect to N (§4.2) |
| Edit mode | **Adopt, cheaply** — a console/config toggle that makes all widgets visible + draggable at once (§4.4) |
| Tabs/stacking in shared frames | **Skip** — that's window management; our widget count is ~6, placement suffices |
| Cockpit-derived skins | **Skip in tk, honestly** (§8.1); the high-fidelity tier is heavy mode's `#/hud` dock (§8.2) |
| Full-screen transparent canvas | **Reject**, still — it is what drags in the AMD device-lost CTD (#40.1 §2.5) and the compositing complexity; opaque windows sidestep all of it |

The deeper lesson from their design is not visual: it is that **each widget is
a small, single-purpose answer to one in-flight question**. Their mining
scanner doesn't port the refinery app; it answers "what did I just scan."
That framing — one question per widget — is the discipline that keeps this
plan from becoming "port the SPA to tk," which #40 §13.1 already refused and
which heavy mode already solves.

## 3. The widget roster

Each widget states: the question it answers · what it shows · data source ·
interaction class (§4.3) · staleness behavior. Ordered by confidence.

### 3.1 NAV — "how far to my target?" *(shipped, becomes the first tenant)*

The W1 HUD as it exists: target · distance · ETA · bearing on-body · fix age.
Fed by the `POST /api/position` piggyback; zero requests. Interaction: inert,
plus the #40.1 chooser as its focus-dialog. Unchanged except re-housed in the
widget framework.

### 3.2 TARGET CHOOSER — "what next?" *(#40.1 I3, unchanged)*

Not strictly a widget — a focus-taking dialog owned by NAV. Designed in full
in #40.1 §4–§5. Everything there holds.

### 3.3 DROP — "am I in the band?" *(Prospector, the highest-value addition)*

The question a belt miner actually has mid-QT: *how far to my exit point, and
when I drop, did I hit?* The server's `GET /api/halo/locate` already
classifies the latest fix per its system's belt — Stanton band/void, Nyx
pocket/ring-void, Pyro field/space — and with `?target_poi_id` adds the miss
distance (the Refine loop). That is precisely a three-line widget:

```
DROP  ·  BAND 4 (peak)           fix 12s
2.31 Gm to drop      ETA 4:10
last drop: IN BAND · miss 8,410 km
```

- **Data:** the armed plan + `GET /api/halo/locate` on each new fix (fixes are
  manual `/showlocation` events — one GET per fix is negligible).
- **Where does the armed plan live?** Today it is SPA-side state. Two options:
  - *(a)* the widget arms its own plan: a focus-dialog (chooser-pattern) that
    calls `POST /api/halo/plan` with the current fix and a picked target. The
    watcher owns what it displays; no SPA coupling.
  - *(b)* server-resident armed plan (per-member, in `Session`), so SPA and
    HUD share one armed state.
  Leaning *(a)* — it reuses the chooser machinery wholesale and adds no server
  state; *(b)* is the better long-term model but is a new
  cross-surface-consistency obligation. Decide when building, not before.
- **Interaction:** inert display; arming is a focus dialog.
- **Staleness:** distance-to-drop suppressed past `AGE_STALE_S`, same as NAV.

### 3.4 SURVEY — "mark this pocket" *(the ⛏ on the glass)*

The belt-survey capture flow is *already server-armed*: the SPA arms a capture
via `POST /api/capture/start` (with the survey payload — density none→dense,
ores, salvage) and it resolves on the **next position post**. The widget is
just a second client of that flow: a focus-dialog with the density seg + ore
toggles, then "armed ⛏ — copy /showlocation to drop the mark" on the glass.
This is the single biggest alt-tab eliminated per hour of belt survey work.

Plus the radar-drift nudge, which is client-side arithmetic the watcher can do
locally: distance from the last mark's fix exceeds half the envelope radius →
show the nudge dot (`radarMarkAt` logic, re-derived in pure Python, tested in
`test_parse.py`).

- **Data:** `POST /api/capture/start` (401 today — §5); envelope radius from
  the armed zone via `GET /api/hud` (§6).
- **Interaction:** focus dialog to compose; inert armed-state display.
- **Staleness:** the armed state can't go stale (it resolves on the next fix
  by construction). The nudge suppresses itself when the fix is stale.

### 3.5 ORE REF — "what ore is this RS?" *(mining, pure reference)*

The FIELD tab's RS cheat sheet, miniaturized. A miner pings a rock, reads an
RS number off the game's scanner, and wants "RS 1700 = 2× Quantainium (850)"
without leaving the seat. Two layers:

- **Reference table** for the current zone/belt: expected ores with RS bases
  and `$`-tier chips — the `mergeRsRows` result, not a re-derivation (§6.3).
- **RS lookup**: a focus dialog where the pilot types a number; the widget
  shows which known signatures divide it (integer-multiple test — pure
  function, trivially testable).

Uniquely, the two feeds behind this are **already token-reachable**
(`/api/resource_values` and `/api/ore_signatures` have no auth dependency),
and both are static per patch — fetch once per watcher session.

⚠ **Attribution is a condition of use, not decoration** — the Strata card in
the SPA renders it, and this widget must too, even at tk fidelity. A `data:
Strata/CELD` line in the widget footer. If that line doesn't fit the design,
the widget doesn't ship.

### 3.6 DANGER — "is my route hot?" *(pirate warnings)*

Nearest active warnings from `GET /api/warnings`, distance-sorted against the
current fix, severity-colored, one line each, max 3. The board already ages
itself off server-side; the widget inherits that honesty. Poll: on each new
fix + a slow refresh (≥60 s) only while visible and the game foregrounded.

### 3.7 TRADE — "what's my next leg?" *(run mode companion)*

The active trade run's current leg: `BUY 240 SCU Laranite @ ARC-L1 → SELL
@ Everus`. `Session.trade_run_view` already computes it; `GET /api/trade/run`
returns it (cookie-only today). Fetch on open + on fix + slow poll (the leg
only changes when the pilot confirms a buy/sell — in the browser, for now).
The buy/sell/advance *actions* stay in the SPA in this pass: they are
confirm-gated, consequence-bearing flows, exactly what §4.3's "one question
per widget" rules out of a HUD.

### 3.8 Explicitly out of the roster

Their Twitch chat, web embed, journal, infographic viewer: heavy mode already
pins the whole browser — embedding web content is *its* job. Their
mission/blueprint tracker and unlock alerts are built on Game.log event
parsing we don't do — but see §9, because that door is worth peeking through.
Their OCR: no. Screen-scraping the game is a class jump we decline.

## 4. The framework — a constellation of small windows

### 4.1 N opaque windows, not one transparent canvas

`sc-overlay` hosts ten widgets in one full-screen transparent Electron canvas
and maintains a list of interactive rects. We invert it: **each widget is its
own small opaque tk `Toplevel`** (one hidden root owns the shared tick).
Consequences, all favorable at our scale:

- The hit-test "rect list" is just the window list — a widget's rect *is* its
  window bounds. #40.1 §3.2's one-rect test generalizes by iteration, nothing
  more.
- No transparency compositing → the AMD device-lost hazard (#40.1 §2.5) stays
  irrelevant, as does `-transparentcolor` flakiness.
- Each widget keeps its own `_freeze_size` — fixed footprint per widget, the
  no-shape-change rule (#40.1 §4.3) enforced locally.
- A crashed widget repaint is contained by the existing guarded-tick pattern
  (#40 §10.1.2), now per-window.

Shared infrastructure (one instance, all widgets ride it): the 250 ms tick ·
the foreground watcher (#40.1 §3.1) · the click-through poller (§3.2) · the
hold-to-interact gate (§3.3) · auto-hide when SC isn't foregrounded · the
topmost re-assert-on-change.

### 4.2 Interaction inherits #40.1 wholesale

Nothing new is designed here — that is the point of sequencing this after
#40.1. Widgets are inert (click-through) by default; hover + held interact key
makes the one under the cursor interactive; anything requiring typed input or
list navigation is a **focus-taking dialog** (chooser pattern, §4.1–§4.2 of
#40.1: focus parks the ship, one give-it-back path, Escape + watchdog +
taskbar entry). The chooser, the DROP armer, the SURVEY composer, and the RS
lookup are four instances of one dialog pattern — build it once.

### 4.3 Interaction classes, so widgets stay small

- **Inert** — text on glass, never clickable beyond drag: NAV, DROP display,
  DANGER, TRADE, ORE REF table.
- **Focus dialog** — deliberate, ship-parked, focus-returned: chooser, DROP
  armer, SURVEY composer, RS lookup.
- **Nothing in between.** No inline buttons on inert widgets, no
  hover-revealed controls. The moment a widget wants a button, it wants a
  dialog or it wants to stay in the SPA.

### 4.4 Placement, config, edit mode

`watcher_config.json` grows a `widgets` map keyed by widget id:
`{"drop": {"enabled": true, "x": 1620, "y": 40, "collapsed": false}, ...}`.
All optional, all defaulted, no migration (the file is best-effort JSON).
Positions anchored to the game window rect once #40.1 §3.1 lands, so layouts
survive resolution changes.

Enablement follows the overlay's own pattern: the startup prompt gains nothing
(mode stays `off·light·heavy`); widgets beyond NAV are enabled from a small
`[+]` affordance on the NAV widget while interactive, or by editing the
config. **Every widget defaults off except NAV.** Six windows appearing over a
cockpit unbidden is the surprise #40 §7 exists to prevent, six times over.

**Edit mode** is the one genuinely new interaction: a state where all enabled
widgets become visible + draggable simultaneously (no hold-F needed),
entered/exited from the `[+]` affordance. Cheap to build, and it makes
first-time layout not-miserable.

### 4.5 A shared widget chrome

One visual grammar so the constellation reads as one instrument, not six apps
(the actual "look" lesson from sc-overlay):

- 1 px hairline border (`BORDER`), panel `BG`, per-widget **accent color** on
  the title glyph only — DESIGN.md tokens throughout.
- Uniform title row: `GLYPH NAME` left (Consolas bold, accent), fix-age chip
  right (the amber/red staleness vocabulary, identical in every widget).
- Body: Consolas, sizes 9–13, Latin-1 only (`safe_text` everywhere — the
  glyph-coverage rule from W1 is a hard constraint, so widget glyphs come from
  the Latin-1 + proven set: `⛏` is out, `*` chips are in).
- Collapse: click the title row (while interactive) → title bar only.
- No resize. Fixed sizes per widget, chosen once, frozen.

## 5. The auth wall — one finding, generalized

#40.1 §5.1 found `POST /api/destination` is cookie-only. Checked today, so is
**everything else this plan wants**:

| Endpoint | Dep today | Wanted by |
|---|---|---|
| `POST /api/destination` | `require_session` | chooser (#40.1) |
| `POST /api/capture/start` | `require_session` | SURVEY |
| `POST /api/halo/plan` | `require_session` | DROP armer |
| `GET /api/halo/locate` | `require_session` | DROP |
| `GET /api/warnings` | `require_session` | DANGER |
| `GET /api/trade/run` | `require_session` | TRADE |
| `GET /api/pois` | `current_user` (token → empty owner ids) | chooser |
| `GET /api/resource_values`, `/api/ore_signatures` | none | ORE REF ✓ works today |

So #40.1 I2 grows from "one dependency change + one endpoint" into **the**
server slice of this whole plan: a deliberate pass converting the table above
to `require_user` (or token-aware equivalents). The trust argument is
unchanged and still sound — the token already authenticates the member for
`/api/position`, which mutates more than any of these — but it should be made
**once, as a policy** ("a watcher token is a first-class member credential for
member-scoped read/write"), reviewed as one diff, rather than endpoint by
endpoint as widgets appear. Admin and org-settings surfaces stay cookie-only.

## 6. Data plumbing — staying under the volume cliff

The scaling constraint is unchanged (#40 §4.3): single worker, position posts
serialize behind `hub.lock`, and a 2 s poll per widget per player would be
ruinous. Three rules keep the constellation cheap:

### 6.1 Fetch triggers, in order of preference

1. **Piggyback** — NAV rides `POST /api/position` today; the response `nav`
   block may gain a *few small scalars* (active trade leg id, armed-capture
   flag) but **not lists** — `nav_summary`'s must-stay-lean docstring is load-
   bearing and this plan re-affirms it.
2. **On new fix** — fixes are manual events, a handful per hour. Each fix
   fans out one `GET` per enabled widget that cares (locate, warnings
   proximity). Bounded by human copy-paste rate; effectively free.
3. **On dialog open** — chooser targets, plan candidates. One RTT behind a
   deliberate keypress (#40.1 §5.3's argument, reused).
4. **Slow poll, gated** — ≥60 s, only while the widget is enabled *and* the
   game is foregrounded (the §4.1 watcher knows), for data that changes
   server-side without a fix: warnings board, trade leg. Auto-hide pauses it.

### 6.2 `GET /api/hud` — one door, widget-shaped

Rather than N bespoke endpoints, one composite:
`GET /api/hud?widgets=drop,survey,trade` returns a block per requested widget
(armed zone + envelope radius, trade leg view, warning summaries…), omitting
whatever isn't asked for. Cost scales with what the member enabled. Reads
session snapshots without `hub.lock`; supports `If-None-Match` on a cheap
`changed_at` so the steady state is 304s. This is #40.1's
`GET /api/nav/targets` generalized — the chooser's targets become
`widgets=targets`. (W3's conditional `/api/nav/summary` stays deferred; its
2 s-poll objection still doesn't apply to any trigger in §6.1.)

### 6.3 Never re-derive server logic in the watcher

The `mergeRsRows` datamined-vs-org-scan merge, value-tier assignment, zone
models — all stay server/SPA-side. Where a widget needs a derived view the
SPA computes in JS (the RS reference table), the server should serve the
derived rows through `/api/hud` rather than the watcher growing a Python twin
— the "second implementation that must track the first" risk (#40 §13.1) is
the failure mode this plan is most exposed to, six widgets over. The pure
logic the watcher *does* own (drift distance, RS divisibility, formatting)
is arithmetic on scalars, testable in `test_parse.py`.

## 7. What this costs where

Rough shape, assuming #40.1's I1/I2 land first (they are M0/M1 below):

| Piece | Where | Riskiness |
|---|---|---|
| Widget framework (§4) | watcher, tk/ctypes | Medium — Windows-only verification, but I1 already carries that risk; this adds iteration count, not novelty |
| Auth policy pass (§5) | server | Low — mechanical, fully testable in `test_app.py` |
| `/api/hud` (§6.2) | server | Low — read-only composition of existing views |
| Dialog pattern (§4.2) | watcher | Carried by #40.1 I3 — the risky part (focus lifecycle) is built and flown once, reused thrice |
| Each additional widget | both | Small — a renderer + an `/api/hud` block; the framework amortizes |

The honest total is "several I1-sized slices," dominated by Windows manual
verification time, not code volume.

## 8. The look, honestly

### 8.1 What tk can and cannot give

Can: a cohesive dark-instrument grammar (§4.5) that reads as one system and
sits quietly over a cockpit — W1 already proved the fidelity level in flight.
Cannot: cockpit-derived skins, ship-matched theming, gradients, tab frames,
smooth alpha — the qualities that make sc-overlay screenshots striking. No tk
effort closes that gap, and pretending otherwise burns time on brand fidelity
that #40 §8 already ruled subordinate to legibility.

### 8.2 The high-fidelity tier already exists: a `#/hud` dock for heavy mode

If the sc-overlay *look* is the goal, the vehicle is heavy mode, which is
already the full SPA with real CSS. A compact `#/hud` hash route — a narrow
dark dock rendering the same widget set as cards (DESIGN.md-styled, live over
WS, no fix-staleness asymmetry for teammates) — pinned as a strip beside the
cockpit UI. Costs a view in `index.html` and nothing in the watcher beyond a
`--app=` URL. Its trade stays heavy mode's trade: opaque window, browser
process, own-marker staleness (#40 §13.2), no click-through granularity.

The two tiers are complements, not rivals: **tk widgets for glass-over-game
minimalism, the heavy dock for a second-monitor-quality panel on one
monitor.** Both feed from `/api/hud`, so widgets added to one are cheap in the
other — same data door, two renderers at different fidelity.

## 9. Two doors worth peeking through

### 9.0 Door one: Game.log events

sc-overlay proves Game.log carries far more than the shard id we currently
read from it. Our watcher already tails that file correctly
(rotation-by-shrink, offset seeking). Events arriving from the log come *in
real time* — no `/showlocation`, no staleness — which is what makes this door
worth the look.

### 9.1 What the source survey settled (2026-08-01, spike part 1)

Read from their parser sources (findings, not code; line shapes they verified
against 4.8.2-LIVE):

**Confirmed IN the log:** the full mission lifecycle — accept with friendly
title + missionId (`SHUDEvent_OnNotification`), objective markers with
contract names (`CLocalMissionPhaseMarker::CreateMarker`), active-objective
changes, mission end with completed/abandoned state (`MissionEnded` /
`EndMission`), aUEC reward + blueprint-received notifications — plus session
boundaries (`Context Establisher Done` frontend-vs-PU, `Channel Destroyed`,
`SystemQuit`), party-marker stream-in/out (member *count*, names only leaking
on despawn), player identity lines (geid/accountId/handle), `Actor Death`,
and chat. Notably, **streaming-zone names leak into entity-attachment lines**
(`StreamingSOC_hangar_lrgtop_001_orison`) — possible container-transition
signal, which would be position-adjacent freshness we currently only get from
`/showlocation`.

**Confirmed NOT in the log — the correction:** mining scan signatures and
refinery job state. Their "mining scanner" widget is fed by *screen OCR*, not
the log — they built an OCR pipeline precisely because the log carries
neither. So the auto-survey hopes above ("scanned here" annotations) are
**probably dead**, and anything wanting scanner numbers on our side would need
OCR, which §12 declines. What their parser can't answer: whether
mining-adjacent events *they never needed* exist (fracture/extraction,
quantum-travel lifecycle, zone transitions) — their coverage proves presence,
never absence.

### 9.2 Spike part 2 — capture a mining session (pending)

`tools/gamelog_spike.py` ingests captured Game.log files and reports an
event-tag inventory plus keyword buckets (mining, scanning, refinery,
quantum, location/zone, cargo, danger…) with scrubbed sample lines — the
player's handle/ids and IPs are folded out by default and chat lines never
sampled. Runbook: fly a normal mining loop (scan several rocks, fracture,
extract, refine, QT between markers, dock/undock), quit, then copy
`LIVE\Game.log` (per-launch history in `LIVE\logbackups\`) off the Windows
box and run the tool. **An empty bucket after a real session is a finding**:
that activity never reaches the log.

Same class-of-citizen rules as always — we read a file the game writes on our
own disk; nothing injected, nothing scraped off the screen. The standing
caveats: log content shifts patch to patch, and the format is not a contract.
A widget built on a log line is a widget one patch from silence, so anything
shipped on this must degrade to absence, loudly documented.

### 9.2.1 Capture 1 findings — cargo session (2026-08-02, 27 min, clean quit)

`docs/game_log_files/game_log_file_cargo_run_20260702.log`, analyzed with the
spike tool. What a cargo loop actually logs, most valuable first:

1. **`<Calculate Route>` names the player's current location in the clear** —
   "Projected Start Location is **Port Tressler** for route to destination
   ObjectContainer_RestStop … fuel estimate 773056.19". Fires on every QT
   target selection. This is a real-time, named "player is at/near X" signal
   with no `/showlocation` involved — the single biggest thing in the file.
   Sibling lines: `<Player Selected Quantum Target - Local>` (destination at
   container granularity) and the obstruction-routing pair, which enumerate
   the computed route's *named waypoints* (Port Tressler → ArcCorp → Baijini
   Point) plus the obstructing body (`OOC_Stanton_4_Microtech`).
2. **HUD notifications are a structured event stream**
   (`SHUDEvent_OnNotification`). Observed vocabulary: **"New Objective:
   Deliver 0/3 SCU of Carbon to Everus Harbor"** (commodity + SCU +
   destination, fully parseable), jurisdiction transitions ("Entered
   microTech Jurisdiction"), Monitored Space enter/exit, Armistice Zone
   enter/leave, "Hangar Request Completed", QT-obstructed warnings — and
   hauling contract titles that carry the route ("Rookie | DIRECT Small Haul
   | Port Tressler > Everus Harbor").
3. **Mining scanning notifications appeared, without numbers** ("Mineral
   deposit detected. Fly closer…") — but this is passive proximity detection:
   **the pilot never ran the active scanner this session, so the capture is
   inconclusive on RS signatures** (user correction 2026-08-02). §9.1's
   they-built-OCR-for-a-reason inference stands as indirect evidence, but our
   own test still needs a session that actually pings rocks — it stays on the
   §9.2 mining-loop checklist.
4. **State machines with [Cargo] tags**: freight-elevator loading platform
   states (ClosedIdle → OpenIdle), docking-tube states, ASOP vehicle fetch
   (`OnRequestFetchVehicles`), plus a cluster of inventory-management request
   tags (`Query Inventory`, `Add Inventory Management Move`) not yet read in
   depth.
5. **Absent from this launch:** completion/abandon/reward lines — the
   session's contract accepts are the spawn-in re-emission sc-overlay
   documented, and the runs ended in other launches (`logbackups\`). Their
   parser has completion verified on 4.8.2; our own capture of it is still
   owed.

**Widget implications:** the Calculate Route line could update presence and
the NAV widget's "where am I" *between* fixes at named-POI granularity, and
flag game-target ≠ nav-target mismatch the moment a pilot plots a jump;
Deliver-N-SCU objectives could surface on the TRADE widget; jurisdiction /
monitored-space transitions are DANGER-widget context. All of it arrives
through the tail the watcher already runs — near-zero added cost. Standing
caveat: one patch's wording, one session's evidence; parse defensively,
degrade to absence.

### 9.2.2 Corpus findings — 70 sessions, Apr–Aug 2026 (`gamelog_backups/`)

268k lines across seven game builds (11674325 → 12344265), including real
mining, salvage, and the Keeger survey trips. What three months of play adds:

1. **Rock ore types appear in the log in the clear** — entity class names
   are `MineableRock_<Scale><Rarity>_<Ore>`:
   `MineableRock_AsteroidLegendary_Savrilium`,
   `MineableRock_SurfaceLegendary_Quantainium`, `MineableRock_FPS_Hadanite`…
   (12 distinct classes seen). **But the channels are incidental**, not a
   stream-in census: 337 mentions in 13 of 70 sessions, arriving via
   mission-marker detach on rock removal (×224), a VFX error line that
   happens to name the entity, and one `FatalCollision`. Useful as free
   opportunistic annotations ("mission rock here was Ice"); **cannot power a
   survey census — the §9.4 scan-sweep remains the census path.**
2. **No RS signature number appears anywhere in 70 sessions** that include
   actual mining. The only "signature" hits are tutorial notification prose.
   This upgrades §9.1's inference to strong corroboration; the definitive
   close-out is still the paired test (pilot notes on-screen values → grep).
3. **Mission lifecycle vocabulary now verified from our own capture**:
   `MissionEnded … MISSION_STATE_COMPLETED`, `Contract Complete: <title>`,
   `Awarded 45500 aUEC` — matching sc-overlay's parser shapes exactly.
4. **Refinery completion reaches the log**: `"A Refinery Work Order has been
   Completed at ARC-L1 Wide Forest Station"` — a refinery-done alarm needs
   no OCR at all (sc-overlay OCRs the console countdown; the *completion*
   event is free). Their OCR remains needed only for job *contents*
   (material/yield/remaining).
5. **Shop transactions log with named shop and price**:
   `SendShopBuyRequest … shopName[SCShop_Entity_CubbyBlast_Area18]
   client_price[4757.00] …` (+ a response line). Observed at a weapons shop;
   **whether commodity-kiosk trades log the same way is the open question
   that matters** — if yes, trade-run buy/sell confirmation could be
   auto-detected instead of tapped in the SPA. Goes on the next capture
   list: buy/sell a commodity at a trade terminal, then grep.
6. **CrimeStat notifications** ("CrimeStat Rating Increased") — danger-board
   context. No `Actor Death` lines in this corpus (no deaths to log).
7. **Deep-space start naming works**: a Keeger trip logs `Projected Start
   Location is Nyx System for route to destination
   social_001_keeger_segment_rckcrk_112` — segment-container granularity for
   destinations, and a system-level answer exactly where `/showlocation`
   ambiguity lives (§9.2.1 finding holds off-grid too).

### 9.2.3 What the commodity/trade log data could improve (2026-08-02, user question)

Ranked by value, all contingent on the §9.2.2 item-5 capture test (does a
*commodity kiosk* log like the weapons shop did, and is there a sell-side
line):

1. **Trade-run auto-confirm.** Run mode's buy/sell/advance taps are the most
   friction-laden part of the loop, and the most error-prone (a forgotten
   confirm corrupts realized stats). A detected transaction at the active
   leg's POI during the matching phase → auto-confirm, or a one-tap "detected
   buy at Port Tressler — confirm?" nudge on the TRADE widget. Even with no
   item detail, shop-flow lines at the right place and phase are a strong
   hint; with item + quantity they're the whole confirmation.
2. **Actual-price capture → the org price layer (#39 slice 0).** The log's
   `client_price` is what the pilot actually paid, with a named shop and a
   timestamp. Auto-filed as org price observations, it feeds the unblocked
   "org-reported prices beat the ≤6 h UEX scrape" overlay with zero manual
   entry — and unlike #39's outbound half, no screenshot rule applies because
   it never leaves our platform. Realized-profit stats also upgrade from
   planned prices to paid prices.
3. **Auto stock/low-stock evidence.** If transaction lines carry quantity,
   the <50%-of-plan low-stock auto-report stops depending on the pilot
   typing the SCU they got; a buy that never appears while the pilot was at
   the terminal is soft stockout evidence (weaker — absence needs care).
4. **Mission-hauling integration (new surface).** Contract titles carry
   routes, objectives carry commodity + SCU + destination, progress
   (`0/3 → 3/3`) updates live, completion pays a logged `Awarded N aUEC`.
   That is enough to auto-assemble a member's active hauling contracts into
   a cargo-planner run (multi-contract stop ordering is exactly what the
   solver does), track per-leg progress on the TRADE widget, and record
   actual payouts into cargo analytics — a mission-hauling companion the SPA
   currently has no data for.
5. **Arrival/at-terminal detection.** Freight-elevator platform activity +
   `Calculate Route`'s named start location are "docked and moving cargo"
   signals between stale fixes — auto-advancing run-mode arrival instead of
   waiting on the next `/showlocation`.

**GATE PASSED — verified capture 2026-08-02** (`Game(1).log`, a real trade
run: 29 SCU Medical Supplies, Hicks Research Outpost → Area18 TDD, pilot
recorded the numbers independently). Commodity kiosks log **richer** lines
than the weapons shop, under `CEntityComponentCommodityUIProvider`:

- **Buy**: `SendCommodityBuyRequest … shopName[SCShop_ht_delta_rayari_m_store]
  price[98840.00] shopPricePerCentiSCU[34.0827] resourceGUID[d5506a24-…]
  quantity[2900.00 cSCU] … unitAmount[29]` — total, unit price (×100 =
  3408.27/SCU), SCU, commodity GUID, shop, kiosk, timestamp. Everything.
- **Sell**: `SendCommoditySellRequest … shopName[TDD_SCShop-001]
  amount[161211.00] resourceGUID[same] quantity[29]` — total + SCU (unit =
  amount/quantity); the GUID is stable across shops.
- **Kiosk board**: opening a terminal logs `AddingCommodityBox` per
  commodity with `commodityName[ResourceType.MedicalSupplies]` + available
  box sizes — the terminal's tradeable-commodity list in the clear (no stock
  quantities, no prices; prices only appear on transactions).
- Field validation of the honesty argument: the pilot reported the sell
  total as 161,214 from memory; the log's 161,211.00 is exactly 5559 × 29.
  **The log out-remembered the pilot on its first test.**

Two mapping tasks replace the old caveats, both tractable: `resourceGUID` →
commodity name (correlation-learnable server-side: an active run leg for
commodity X + a transaction firing = one mapping learned, org-wide, no
external data; the kiosk-board names give the candidate vocabulary), and
`shopName` string → our terminal/POI ids (same correlation trick via the
run's expected stop, or a small static table). Patch-fragility rule still
applies: assists that degrade to absence, never load-bearing.

### 9.3 Door two: piggyback sc-overlay's sidecar API (2026-08-02, user question)

Rather than duplicating their features, could members who *run* sc-overlay
feed our platform from its output? Source survey answer: **yes, and by their
design.** The app runs a local sidecar HTTP server (default port 8778, env
`PORT`), read endpoints unauthenticated, and their own comments describe
"external consumers (stream overlays via `GET /api/ship` + the SSE)" —
a third-party integration surface is a *feature* of their app.

**The one tap that matters:** `GET /api/mining` (snapshot) and
`GET /mining/events` (SSE push on change) expose the OCR pipeline's product —
the current scan as `{signature, matches: [{name, rarity, count}], at,
confirmed, verdict}` plus refinery jobs with absolute end times. This is
precisely the data §9.1 declared unreachable ("scan signatures are not in the
log; OCR is out of scope") — **their OCR revives our auto-survey idea without
us building OCR.** Concretely:

- **Auto-RS on survey marks:** an armed ⛏ capture (§3.4) attaches the recent
  `confirmed` scan reads as `rs_seen` evidence — the pilot scans, marks, and
  the signature numbers ride along instead of being typed. Honesty guard: the
  scan is real-time but our fix is manual, so only reads within a short
  window of the resolving `/showlocation` should attach.
- **ORE REF live tile** (§3.5): show the last scan's verdict + matched rocks
  passively, no manual RS entry, when the feed is present.
- **Refinery jobs** could someday feed RM "incoming materials" pledges —
  noted, not designed.

**Scope discipline:** their missions/party endpoints are Game.log-derived —
we read the log ourselves (§9.0) rather than proxying it through their app.
The integration's unique value is OCR-derived data only.

**Rejected taps:** their `mining.json` state file persists targets + refinery
jobs but *not* the scan (in-memory only), and `sidecar.log` is unstructured
debug text. The HTTP API dominates both.

**Posture and guards:**

- *License:* members run the genuine, unmodified app; we consume a localhost
  API it deliberately exposes. No code vendored, none of their bundled
  datasets committed (same non-redistribution posture as Strata — rock names
  arrive at runtime through the API, and we store only what the member's own
  gameplay produced). Far cleaner than any code-reuse path, but courtesy says
  ask: contact Subliminal before shipping, and ideally propose a small stable
  "integration contract" upstream so the shape we read is documented rather
  than internal.
- *Fragility:* `/api/mining` is their internal view model, versioned by their
  releases, not a contract. Feature-detect fields, read the app version from
  `/api/diagnostics`, and degrade to absence loudly — the §9.2 rule, same
  reason.
- *Member cost:* this asks a member to install and run a third-party Electron
  app **and enable its OCR capture** (off by default there). Strictly
  per-member opt-in; every consuming feature must be a complement that
  vanishes gracefully, never a prerequisite.
- *Coexistence unverified:* their full-screen transparent topmost canvas and
  our topmost tk windows have never flown together — topmost-churn interplay
  goes on the same manual-verification list as everything else in §3 of
  #40.1.

### 9.4 The scan-sweep survey method (2026-08-02, user idea)

The strongest version of §9.3 is not auto-filling one mark — it is a **survey
methodology**. Protocol: fly to a spot, hold position, `/showlocation` once,
then rotate in place scanning every contact in an arc. Every scan the pilot
lands emits a signature through the sidecar feed; a station's sweep yields N
signatures = **a census of everything scannable from that point**. Because
the pilot doesn't move, every read belongs to the one fix *by construction* —
the staleness problem that haunts every other live-data idea in this file
simply doesn't arise. Tile a pocket with a handful of stations and the zone's
ore composition falls out of an afternoon's rotating instead of a week of
per-rock marking.

**What one read actually is:** a signature integer + timestamp + `confirmed`
flag. **Not distance** — the game shows it, but their OCR doesn't read it
(checked `screen-read.ts`; the view model carries signature only). No
bearing either — `/showlocation` is position-not-attitude and the scanner
adds nothing. So rocks are not individually placeable; a station's product is
an *aggregate sample over the scanner-range sphere around the fix* — which is
exactly the granularity our survey model (pockets, envelopes, zone stats)
already works at. Distance would upgrade stations to radial shells; it goes
on the upstream-conversation list (§9.3), not in this design.

**The inference is server-side and already half-built.** Signature = ore base
× cluster count, so each read maps to candidate (ore, count) pairs by
divisibility against `ore_signatures` + org `rs_bases` — the exact test
`surveyRsCard`/`mergeRsRows` already run, and the aggregation shelf exists
(`_survey_scan_stats` `rs_seen`/`rs_bases`). Per §6.3 the watcher ships raw
integers; the server infers. Ambiguity (one value dividing two bases) is real
per-read and shrinks under aggregation: across dozens of reads, an ore that
is ever the *sole* candidate is confirmed present; always-ambiguous ores stay
"possible".

**Schema landing:** a `sweep` block on the survey payload — one mark per
station carrying the read list — NOT one mark per rock (N marks piled on one
fix would read as a dense activity cluster to `survey_pockets` and distort
every density stat). Bonus: `len(reads)` is an **objective density measure**
sitting beside the subjective none/sparse/medium/dense seg — the first
instrument-derived number in the survey model.

**Known biases, all acceptable if stated:**

- *Dedup undercount:* their tracker suppresses consecutive identical
  signatures (only a changed value re-emits), so same-signature runs count
  once. Presence unaffected; abundance is a lower bound.
- *Floor:* reads below their 2,000 floor never surface — small contacts are
  invisible to the sweep.
- *Pacing:* their OCR polls ~3 s, so the rotation protocol is scan → hold a
  beat → next. A too-fast sweep silently drops reads; the widget should show
  a live read counter so the pilot can see the take rate.
- *Stationarity guard:* a mid-sweep fix that disagrees with the sweep fix by
  more than a few km voids or splits the station — drift while rotating is
  the one way this method lies.
- *Overlap:* adjacent stations closer than ~2× scanner range double-count.
  Fine for presence, noted for density.

## 10. Slices

- **M0 = #40.1 I1** — inert HUD: foreground watcher, click-through,
  hold-to-interact. Unchanged, still first, still independently shippable.
- **M1 = #40.1 I2, widened** — the §5 auth policy pass + `/api/hud`
  (subsuming `/api/nav/targets`). Server-only, fully testable on the Mac.
- **M2 = #40.1 I3** — the chooser, which also births the shared dialog
  pattern (§4.2).
- **M3 — the framework** (§4): multi-window, shared chrome, config schema,
  edit mode; NAV re-housed. First moment this doc's thesis is visible on
  glass.
- **M4 — DROP + SURVEY** (§3.3–§3.4): the Prospector pair; highest value,
  exercises dialog reuse + `/api/hud` blocks.
- **M5 — ORE REF · DANGER · TRADE** (§3.5–§3.7): one small slice each, in
  whatever order flight time says.
- **M6 (parallel, optional) — the heavy `#/hud` dock** (§8.2): SPA-only,
  independent of M3–M5, shippable any time after M1.
- **M-spike (any time) — Game.log survey** (§9): read-only research,
  no product commitment.

M0–M2 are exactly #40.1 — this plan changes nothing about the near-term work,
which is the strongest evidence the two documents are compatible.

## 11. Open questions

1. **Is the tk fidelity ceiling acceptable for the constellation, or is the
   heavy dock the real product?** (§8) Cheapest way to find out: build M3 with
   NAV only and fly it; the framework is required either way for click-through
   alone.
2. **Armed-plan residency** (§3.3): watcher-owned (a) vs server-resident (b).
3. **How many widgets before the glass is noise?** sc-overlay users run ten;
   our instinct says 2–3 concurrently. Defaults-off + edit mode make this the
   pilot's problem, but the README should carry an opinion.
4. **All of #40.1 §8's open questions** — focus cost, does-the-ship-park,
   cap-vs-interleave — inherited unchanged, and the park question now gates
   four dialogs instead of one.
5. **Game.log contents** (§9) — half answered by the source survey (§9.1:
   missions/deaths/party/zones in, scan signatures out); the capture (§9.2)
   answers the rest.
6. **The sc-overlay piggyback** (§9.3) — is there member appetite for running
   their app with OCR enabled, and does Subliminal welcome the integration?
   Both are conversations, not code; have them before designing the
   `rs_seen` attach flow.

## 12. Not doing

Everything in #40.1 §9 (keyboard hook, transparent canvas, their log tailer,
Electron, synthetic input), plus:

- **OCR / screen capture** — reading the game's rendered pixels is a class
  jump from reading our own server's JSON, and nothing in the roster needs
  it. Where OCR-derived data is genuinely wanted (scan signatures), the
  answer is consuming sc-overlay's sidecar output from members who already
  run it (§9.3), not building a pipeline of our own.
- **Skins / per-ship theming** — the cost lands on every widget forever;
  DESIGN.md tokens are the one theme.
- **Widget tabs, stacking, resize** — window management for a ten-widget
  ecosystem we don't have.
- **In-widget trade actions** (buy/sell/advance) — consequence-bearing
  confirms stay in the SPA (§3.7). Revisit only if flight time demands it,
  as its own decision.
