# Watcher HUD — interaction pass: click-through, focus, and an in-game target chooser (#40.1) — design plan

**Status: 📐 design, not built.** Written 2026-08-01 after surveying
[`SubliminalsTV-Projects/sc-overlay`](https://github.com/SubliminalsTV-Projects/sc-overlay),
a mature Electron overlay for the same game, and re-reading our own endpoints.

Continues [`watcher-overlay.md`](watcher-overlay.md) (#40, shipped v0.83.0–v0.84.2).
That doc's **W2 is still open** and this one replaces it: the original W2 was
blocked on its own ordering problem ("click-through would break the F-key drag,
so W2 needs a non-mouse way to reposition first"), and the survey dissolves that
blocker rather than solving it.

Two findings reshaped this from what was first sketched:

1. **The raw-input fact changes the sign of "taking focus."** #40's backlog entry
   records that SC reads the mouse via raw input regardless of window focus, so
   hovering the overlay still turns the ship — and that alt-tab parks the
   controls. A chooser that takes focus therefore **parks the ship while you
   pick**. Focus is not the cost this design pays; it is the mechanism that makes
   it safe (§4.1).
2. **The watcher cannot set a destination today.** `POST /api/destination` is
   `require_session` — cookie only. The token gets a 401 (§5.1).

**On licensing:** everything below is a technique or a conclusion, read from
`sc-overlay`'s source and re-derived for tk/ctypes. **No code is copied.**
`sc-overlay` is FSL-1.1-MIT, which would not permit vendoring into this repo, and
this project's standing practice is to refuse license-encumbered inputs
(erkul rejected outright; the Strata feed deliberately uncommitted). Where a
finding is theirs, §2 says so — they paid for it in flight time we haven't spent.

---

## 1. The idea

W1 put the navigator's one useful line on the glass. Two things are still wrong
with it in a cockpit:

- **It is interactive when it should be inert.** The window accepts clicks
  everywhere, always, so it can eat one meant for the game.
- **It is read-only.** You can see your target and its distance, but you cannot
  *change* the target without alt-tabbing to the browser — which is the exact
  cost W1 existed to remove. The HUD answers "how far?" and cannot answer
  "what next?".

This pass fixes both: make the window inert except when deliberately engaged,
then let it retarget in place.

## 2. What the survey found

`sc-overlay` is a full-screen transparent Electron canvas hosting ten widgets,
fed by a local server that tails `game.log`. Architecturally it is not what we
want (§10.1) — but its comments are scar tissue, dated and attributed, from
problems we have or are about to have.

### 2.1 Hover-polled click-through (adopted — §3.2)

Their window is click-through by default. The renderer reports its interactive
rects; the main process polls the OS cursor and flips the window interactive
**only while the cursor is inside one**. Two details they paid for:

- They explicitly rejected Electron's `{forward: true}` because on Windows it
  installs a **system-wide low-level mouse hook per window**, and several
  overlays' worth stutters the whole cursor once the app is elevated (UIPI stops
  masking them).
- The poll retunes: fast near a rect, slow when far. Boundary crossings feel
  instant; idle cost is a handful of polls a second.

This is what dissolves W2's ordering blocker. We do not need a non-mouse
reposition, because drag survives click-through unchanged.

### 2.2 Hold-to-interact, gated on game focus (adopted — §3.3)

Their interact key doesn't hint that the cursor is free (which is all ours does
today) — it gates whether the window accepts clicks at all. The field-learned
nuance: **only require the hold while the game is the foreground app.** Their
hook is passive and non-consuming, so it cannot swallow the keystroke —
demanding the hold on the desktop meant pressing `F` over Discord and typing an
`f` into whatever had focus. And they fail *safe*: until the foreground watcher
has answered once, fall back to always-hold, so a broken helper cannot silently
make the overlay click-grabby mid-flight.

### 2.3 A foreground-window watcher (adopted — §3.1)

They run one long-lived helper that reports the focused window's process name
and rect, resolving the process name **only when the window handle changes** (the
naive version was ~20 process launches a minute), printing only on change, and
running only while some feature has asked for it.

We need the same answers and can get them far more cheaply — `ctypes` is already
loaded in `Overlay.__init__` and we already tick at 250 ms.

### 2.4 Global hotkeys are dead in-game (recorded, not adopted)

Electron's `globalShortcut` (= Win32 `RegisterHotKey`) does **not** fire while SC
has focus: the game takes the keyboard through Raw Input / DirectInput and the
key never becomes `WM_HOTKEY`. Their fix is a passive `WH_KEYBOARD_LL` hook via a
native module — the same technique OBS, Discord and RTSS use, and EAC-safe
because it observes rather than injects.

**We should not need this** (§4.2 explains why the chooser takes focus instead),
but it is the answer if a global toggle key is ever wanted, and it saves
re-discovering that `RegisterHotKey` silently does nothing in the one context
that matters.

### 2.5 An unrelated hazard in our heavy mode (worth a separate look)

They disable hardware acceleration by default because a transparent
always-on-top window GPU-composited over SC's Vulkan swapchain caused **AMD
device-lost CTDs**. Their AMD-compat mode also passes
`--disable-features=CalculateNativeWinOcclusion`.

Our light HUD is opaque GDI and immune. But **heavy mode is a Chromium window
pinned over a fullscreen game** (`sc_nav_heavy.py:102` passes `--app=` and
nothing else), and `CalculateNativeWinOcclusion` is Chromium deciding a covered
window need not paint. If heavy mode ever appears to stall or hold a stale
frame, that flag is the first suspect. **Out of scope here** — noted so it isn't
lost.

## 3. Part one — make the HUD inert (replaces W2)

### 3.1 Foreground watcher (enables everything else)

On the existing tick: `GetForegroundWindow` → `GetWindowThreadProcessId` →
`QueryFullProcessImageNameW`, resolving the process name only when the HWND
changes (§2.3). No subprocess; we are strictly better placed than they are.

⚠ **`argtypes`/`restype` are mandatory.** #40 already paid for this: ctypes
without them **truncates a 64-bit HWND** — silent, unconditional failure. It is
the first thing to check in any Win32 work here.

Four things fall out, three of which are owed:

- **Auto-hide when SC isn't in front.** Today the HUD sits topmost over Discord
  and the browser forever.
- **The exclusive-fullscreen warning §3.1 of the parent doc promised** and we
  never built — compare the foreground rect against the monitor rect.
- **Anchor to the game window rect** instead of absolute screen coords, so a
  saved position survives a resolution change or the game moving monitors.
  Better than today's clamp-back-on-screen.
- **Re-assert topmost on foreground *change*, not every 2 s.** §13.8 of the
  parent doc found the unconditional re-assert has a real cost —
  `SetWindowPos` fires `WM_WINDOWPOSCHANGED` even with `NOMOVE|NOSIZE`, and
  Chromium kills open `<select>` popups on it, so the keep-alive closed every
  dropdown in the app. The watcher tells us exactly when the re-assert is
  actually needed.

### 3.2 Hover-polled click-through

`WS_EX_TRANSPARENT | WS_EX_LAYERED` via `SetWindowLong`, cleared while
`GetCursorPos()` is inside our bounds. Our hit test is **one rect** — vastly
simpler than their multi-widget region list — and rides the tick we already run.

Drag survives untouched, so **the "non-mouse reposition first" prerequisite is
deleted from the plan, not satisfied.**

### 3.3 Hold-to-interact

Interactive only when the cursor is over the HUD **and** the interact key is
held, and only demand the hold while SC is the foreground app (§2.2). We do not
need a keyboard hook for this: `GetAsyncKeyState(0x46)` on the existing tick
answers "is F down right now", which is the whole question.

Fail safe, as they do: until the foreground watcher has answered once, behave as
today.

## 4. Part two — the target chooser

### 4.1 The constraint that shapes it: raw input, and why focus is the answer

SC reads the mouse via raw input regardless of window focus, so **moving the
cursor over the HUD turns the ship** — known, recorded in #40, and not fixable
without input interception that the no-injection rule forbids. Alt-tab parks the
controls.

The first sketch of this feature had a "tier 1" list navigated by keyboard with
**no** focus change, on the theory that focus was the thing to avoid. That is
wrong twice over:

- Our keyboard observation would be passive and non-consuming, so **every
  keystroke used to drive the list also reaches the game.** Arrowing through a
  list would fire whatever those keys are bound to in the cockpit.
- Parking the controls is *desirable* while picking. A cursor drifting over an
  inert HUD turning the ship is precisely the failure to design out.

**So the chooser takes focus, deliberately, for its whole lifetime.** Keys and
clicks belong to us, the ship parks, and focus is handed back on commit. This is
simpler than the alternative and safer than the status quo.

### 4.2 Handing focus back is a safety property, not a nicety

The dangerous failure is not taking focus — it is **failing to return it**. Your
next WASD types into a search box while the ship drifts.

The foreground watcher (§3.1) is what makes the return possible: we watched the
game lose focus to us, so we hold its HWND. `SetForegroundWindow` succeeds
because we own the foreground at that moment (Windows only lets the
foreground-owning process reassign it). Commit, Escape, and a blur timeout all
route through one give-it-back path.

⚠ **The chooser must be recoverable.** The HUD is `overrideredirect(True)` — no
taskbar button, no alt-tab entry — and §10.1 of the parent doc is the story of
what that costs when a window gets stuck. A focused chooser with no taskbar
presence and a failed focus-return would strand the pilot with no handle to
grab. Guards: `Escape` always closes, an idle watchdog auto-closes and returns
focus, and the chooser (unlike the HUD) gets a taskbar entry so alt-tab can
always reach it.

### 4.3 A separate window, not a taller HUD

`_freeze_size` exists because the HUD resized on every update as names and
distances changed length, and a window that changes shape in peripheral vision
reads as an event. A chooser that grows out of the HUD re-opens that wound.

The chooser is a sibling `Toplevel` anchored to the HUD: appears on demand,
vanishes on commit, **HUD never changes shape**. This also keeps §3.2 clean —
the HUD stays click-through-except-on-hover, the chooser is an ordinary focused
window.

### 4.4 What it shows

Two sections in one window, both keyboard-driven. Both take focus (§4.1); the
split is about *how many keystrokes*, not about focus.

**Likely targets — no typing.** Ranked by what is actually plausible in flight:

- the next stop on an active trade or cargo run (`Session.trade_run_view`
  already knows)
- the armed Prospector plan target
- nearby POIs, already computed in the state frame
- a watcher-local MRU of recent destinations (no server involvement, renders
  instantly, so the window is never blank)

Mid-flight you rarely need to search a 4M-id catalog; you need *"the next leg of
my run"* or *"that station I just left."* This section should answer most picks
in one keystroke.

**Search — the escape hatch.** Type-ahead, debounced, for everything else.

**The framing that keeps this small: the HUD is not becoming a POI browser, it
is answering "what is my next target."** §13.1 of the parent doc already refused
to port SPA views into tk, and that refusal holds here. A results list is fine —
it is a list of strings. A chooser with system/type/container filters is the
thing to say no to; that is what heavy mode is for.

### 4.5 Staleness has to propagate

A "nearest POI" list built on a four-minute-old fix during quantum travel is
misleading in a way the one-line HUD never is, because **a list looks
authoritative**. Past `AGE_STALE_S`, suppress the distances rather than show
wrong ones — same amber/red chip, same principle as the rest of the file. A
frozen overlay lies; a confidently-wrong list lies louder.

## 5. What the server already has, and what this costs

### 5.1 Auth — the one real blocker

The `auth_gate` middleware accepts `token_user(request)` (`app.py:1201`), so a
watcher bearer token already passes for every `/api/*` path. Reachability is not
the problem. Three specifics:

| Endpoint | Today | Needed |
|---|---|---|
| `POST`/`DELETE /api/destination` | `require_session` — **401 for the watcher** | `require_user` |
| `GET /api/pois` | `current_user` → `None` for a token caller → **empty `viewer_owner_ids`** | token-aware optional dep |
| `GET /api/observations` | no route-level dep at all | works today, unchanged |

The middle row is the nastier one: not an error, just **silently missing
results**. `viewer_owner_ids` is what lets a member see their own private POIs,
and since survey marks are `custom_pois` with `type="survey"`, an empty set
filters out the pilot's own marks. Observations are unaffected —
`search_observations` takes no visibility parameter at all, so mapped ore nodes
are org-wide.

**This breaks W1's "zero new endpoints, zero new requests" property**, and that
should be a deliberate decision rather than something noticed mid-build. The
widening is small: a watcher token gains destination-setting for the member it
already authenticates as, having already been trusted with `/api/position`,
which mutates considerably more. Note also that the server keeps enforcing the
rules — `/api/destination` already rejects private-not-yours and admin-flagged
POIs (`app.py:3082-3088`), so the chooser renders a 404 rather than
re-implementing the checks.

### 5.2 Search is two calls, not one

`search_pois` iterates `nav.pois.values()` only (`nav_core.py:1592`).
Observations are a separate index behind `search_observations`
(`nav_core.py:1614`) at `/api/observations`. The SPA fans out and concatenates
(`index.html:5696-5700`), then calls one `setDestination` for either — ids share
a namespace and `/api/destination` resolves
`nav.pois.get(id) or nav.observations.get(id)`.

The chooser should do the same. Two calls is fine, and matching the SPA keeps
ranking consistent between the two surfaces.

**The ranking problem is real, though.** The two searches sort on different
keys: POIs by (prefix-match, name length, name); observations by
newest-observed. Concatenated, you get every POI and then every observation. The
SPA absorbs that in a 100-row scrolling table. A chooser showing ~8 rows on the
glass cannot: type `quant` and a wall of Quantanium-adjacent POI names pushes
every ore node off the bottom — and an ore node is exactly the target class you
most want mid-flight.

Two options, decide before building:

- **Cap each side** (say 5 POIs + 3 observations). Trivial, no new logic, mildly
  arbitrary.
- **Interleave by distance from the current fix.** More useful in flight, and we
  hold the position to do it — but it is watcher-side ranking the SPA does not
  have, which is a small serving of the "second implementation that must track
  the first" risk §13.1 warns about.

Leaning to the cap: it is the one that cannot drift.

### 5.3 Where the "likely targets" list comes from

`nav_summary`'s docstring is explicit that it "must not grow the nearest-POI
lists or forecasts that make `/api/state` too heavy to send this often" — it
crosses the wire on every `/showlocation` and every 60 s heartbeat of every
watcher. Piggybacking `nearby` contradicts that in writing.

So: **a `GET /api/nav/targets`, fetched when the chooser opens.** Zero
steady-state cost, always fresh, and one round trip is imperceptible when the
pilot has just pressed a key to open a dialog. The local MRU renders immediately
so the window is never blank while it lands.

This is W3's conditional `GET /api/nav/summary` arriving through a different
door. The parent doc deferred it until real use demanded it (§4.3), on the
grounds that 2 s polling would be 20–30× current volume against the
single-worker `hub.lock` cliff. **That objection does not apply here**: this is
one request per chooser-open, not a poll. The volume argument stays intact and
W3 stays deferred.

## 6. Slices

**I1 — inert HUD.** §3.1 foreground watcher · §3.2 hover click-through · §3.3
hold-to-interact · auto-hide · fullscreen warning · re-assert on change. All in
`sc_nav_overlay.py`, no server change. This is the parent doc's W2 minus its
blocker, and it is independently shippable.

**I2 — auth + targets endpoint.** §5.1 dependency changes · `GET
/api/nav/targets`. Server-only, testable in `test_app.py` without a display, and
independently useful (it is what any future client would want).

**I3 — the chooser.** §4 window, focus lifecycle, likely-targets list, search,
staleness suppression. Depends on I1 for the game HWND and on I2 for data.

Sequenced so the two risky halves are separated: I1 is all Windows behaviour
that cannot be verified off Windows, I2 is all server behaviour that can be
fully tested on the dev Mac.

## 7. Testing

- `server/test_app.py` — a watcher token can set and clear a destination; an
  anonymous caller still cannot; `/api/pois` with a token returns the caller's
  own private POIs (the §5.1 regression, which is invisible if you only assert
  status codes); `/api/nav/targets` shape, and that it does not mutate presence.
- `watcher/test_parse.py` — pure helpers only, as today: rect hit-testing,
  ranking/cap of merged results, staleness suppression. No display required.
- Manual on the Windows box, because none of §3 or §4 can be verified elsewhere:
  click-through actually passes clicks to the game · the interact key gates as
  intended and is not demanded on the desktop · **focus returns on commit,
  Escape, and watchdog** · the chooser is reachable by alt-tab if focus-return
  fails.

## 8. Open questions

1. **Does taking focus in borderless windowed cost anything visible?** The game
   stays drawn, but FPS behaviour under lost focus is unverified. If SC throttles
   an unfocused window hard, the chooser's few seconds are still fine — but it
   should be seen, not assumed.
2. **Does the ship reliably park?** The whole safety argument in §4.1 rests on
   the backlog's "alt-tab parks the controls." A focus change that is *not* an
   alt-tab may not behave identically. **Verify before I3 is called done** — if
   it is false, §4.1 needs rethinking, not patching.
3. **Cap or interleave** (§5.2).
4. **Should the chooser be openable at all when the fix is stale?** Arguably it
   is *most* wanted then — you are lost and want a target — but the list is at
   its least trustworthy. §4.5 suppresses distances; whether that is enough is a
   judgement call best made looking at it.

## 9. Not doing

- **A keyboard hook.** §2.4 records that `RegisterHotKey` is dead in-game and
  that a passive `WH_KEYBOARD_LL` hook is the answer, but §4.1 removes the need:
  a focused window gets its keys the ordinary way. A native hook is a large
  swallow for a feature that no longer wants it.
- **The full-screen transparent canvas.** Right for ten draggable widgets, wrong
  for one line of text — and it is what drags in the AMD compositing crash
  (§2.5). Our small opaque window sidesteps all of it.
- **Their log tailer.** We already do rotation-by-shrink and offset seeking
  (`sc_nav_watcher.py:200-223`). No gain.
- **Electron.** Their whole shell exists to host a browser; we have `--app=`
  heavy mode for that and tk for this, with no bundler and no 200 MB runtime —
  consistent with the repo's no-build-step guardrail.
- **Auto-typing anything into the game.** Unchanged from #40: no injection, no
  memory reads, no synthetic input. Same class of citizen as the Discord
  overlay.

## 10. A note on inherited conclusions

`sc-overlay`'s comments are dated and attributed, and read like findings from
weeks of real flight. **We would be adopting the conclusions without the field
time.** §3.3's focus gating and §4.2's focus hand-back in particular are the
kind of thing that works perfectly on a dev box and fails on someone's second
monitor.

Treat I1 and I3 the way W1 was treated: unproven until flown, with the
expectation that first flight finds something no test could.
