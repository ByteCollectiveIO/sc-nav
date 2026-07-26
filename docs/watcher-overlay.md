# Watcher in-game overlay — target, distance, bearing on the glass (backlog #40) — design plan

**Status: 🔨 SLICE W1 BUILT 2026-07-25, not yet shipped or flown.** Scoped after
a question about firewall/network impact (§4 is the answer: there is none). The
data this needs is already computed and currently discarded (§2), so the server
half is small; the real work is restructuring the watcher process around a UI
event loop (§6) and the Windows window management (§8).

**W1 build deviations** (two things pulled forward from W2, both because W1
would have been worse without them):

1. **Bearing display shipped in W1**, not W2 — it was the original ask, and once
   the formatters existed it was one label. In-space closing/opening is still W2.
2. **Drag-to-place + position persistence shipped in W1.** Without click-through
   (still W2) the window is interactive anyway, and a HUD nailed to a fixed
   corner over someone's cockpit is a bad first impression. Position is clamped
   back on-screen at startup so a saved spot from a detached monitor can't
   strand it.

**Verification status:** 778 server tests + 37 watcher tests green. The full
chain (clipboard → POST → `nav` body → HUD strings) runs against a stub HTTP
server, and **the window now renders under real tk 9.0** (installed on the dev
Mac mid-build) — every HUD state was driven through live widgets and read back.
Rendering it for real caught three things the stub could not, all fixed:

- **The window resized on every update** (253 → 261 → 402 px) as the target name
  and distance changed length. A HUD that changes shape in peripheral vision
  reads as an event. Now pinned by `_freeze_size` + `trim_name` (§8).
- **Glyph coverage.** The HUD renders in **Consolas** on Windows, and the dev box
  can't verify Consolas' coverage of `⟳` (U+27F3, Supplemental Arrows-B) or the
  diagonal arrows `↗ ↘`. A tofu box in the pilot's one readable line is a bad way
  to find out, so bearings became **compass points** (`SE 118°` — also better at
  9px than an arrow), `⟳` became `fix`, and em dashes became `--`.
- **Names aren't ours.** They come from the wiki catalog, from members typing
  custom POI names, and from our own capture notes ("Keeger Belt — survey
  pocket SVY-14"). `safe_text` folds them into Latin-1 at the boundary.

Three caveats stand. The window has only been rendered **on macOS**, never on
Windows — where the font, `overrideredirect`, and always-on-top behavior are all
different code paths in Tk. **The rewritten `run_watcher.bat` has never been run
by cmd.exe** (no Windows here), which is why §9.1's probe uses the most
conservative batch constructs available. And **nothing has been flown**. §12.3
(does the org fly borderless) is still open and still worth asking before anyone
builds W2.

Companion to [`multi-user-migration.md`](multi-user-migration.md) (watcher token
auth) and the watcher's own [`../watcher/README.md`](../watcher/README.md).

---

## 1. The idea

The navigator already knows your target, your distance to it, and your ETA — but
it lives in a browser on your second monitor. In a single-monitor cockpit you
are alt-tabbing out of the game to read one line of text.

Ship that one line as a small always-on-top window over the game: **target name ·
distance · ETA · bearing (when it exists) · how stale the fix is**. The watcher
is already running on that machine, already authenticated, and already talking
to the server — it is the natural host for the overlay.

**Opt-in, always.** The overlay is a window that appears over someone's game. It
must never show up unasked. §7 covers the enrollment question at startup.

## 2. What already exists (and is being thrown away)

`nav_core.compute_state()` builds a `destination` block with exactly the fields
we want — `name`, `distance_m`, `surface_distance_m`, `bearing_deg`, `eta_s`,
`same_container` (`nav_core.py` ~1044–1082, assembled by `_poi_summary` /
`_observation_summary` ~768–790).

`post_position` computes the whole frame and returns nothing useful:

```python
frame = sess.state_frame()          # app.py ~2764
...
return {"ok": True}                 # app.py ~2767
```

So on every `/showlocation` and every 60 s heartbeat, the server has already done
100% of the work and discards it. **The cheapest possible version of this feature
is to stop discarding it.**

Auth is likewise already solved: `require_user` (`app.py` ~1418) accepts either a
browser session *or* a watcher bearer token via `token_user` (~1385), which is
what `POST /api/position` already uses. No new credential, no new token type.

## 3. Four constraints that shape everything below

These are not implementation details — they decide what the feature can honestly
promise. Read them before §5.

### 3.1 Exclusive fullscreen defeats an always-on-top window

A normal top-level window composites over the game in **Borderless / Windowed**
mode only. In exclusive Fullscreen the game owns the swap chain and our window is
simply not drawn. Star Citizen defaults to borderless for most players, so this
mostly works — but "mostly" means a subset of users will see nothing and file it
as a bug.

**Decision:** document it prominently, and detect-and-warn where we cheaply can
(if the overlay has been enabled and the SC window is fullscreen-exclusive, print
a console line explaining it). We do **not** solve this properly — the only real
fix is hooking DirectX present, which §3.4 rules out.

### 3.2 Bearing only exists on a planet surface

`bearing_deg` is a great-circle surface bearing and is populated only when the
player and the target resolve to the same body, with lat/lon and a surface radius
in hand (`nav_core.py` ~783). In space it is `None`, and that is not an oversight
we can fix: `/showlocation` reports **position, not attitude**. There is no ship
heading in the data, so there is no reference frame to draw a "point your nose
here" arrow against.

**Decision:** the overlay shows a bearing arrow **on a body** and, in space,
shows what is actually true and actually useful:

- distance + ETA,
- **closing / opening** — is this number going down since the previous fix,
- the target's name and system.

A "warmer / colder" readout is genuinely useful for the deep-space work this org
does (belt drops especially), and it is honest. A fake compass would not be.

### 3.3 The reading is only as fresh as the last `/showlocation`

The watcher learns position when the player copies `/showlocation` output. The
60 s heartbeat re-sends the **same coordinates** to keep the player live on
teammates' maps — it does not produce a new fix. So between manual copies the
distance is frozen.

**Decision:** the staleness age is a **first-class element of the overlay**, not
a tooltip. `⟳ 4s` reads fine; `⟳ 2m 10s` in amber tells the player the number
is a memory, not a measurement. An overlay that silently displays a two-minute-old
distance as if it were live is worse than no overlay.

**Explicitly rejected:** auto-typing `/showlocation` into the game to refresh the
fix. It is synthetic input injection into a live MMO — the exact class of thing
that gets accounts actioned, and not a risk we ask our members to take for a
convenience feature.

### 3.4 Stay a dumb sibling window

No process injection, no DLL loading, no game-memory reads, no synthetic input.
The overlay is an ordinary top-level window that reads our own server's JSON —
functionally the same class of citizen as the Discord or Steam overlay, and
uncontroversial under CIG's fan-content and fair-play rules.

This is written down here so it doesn't drift later when someone wants to solve
§3.1 or §3.3 "properly."

## 4. Network shape — no inbound traffic, no firewall changes

This was the question that prompted the doc, so it gets its own section.

**Nothing here listens.** Both halves of the design are client-initiated
outbound HTTP from the player's PC to the same host, port, scheme, and bearer
token the watcher already uses:

| Direction | Today | With the overlay |
|---|---|---|
| Player PC → server | `POST /api/position` | unchanged (+ a response body it now reads) |
| Player PC → server | — | optional `GET /api/nav/summary` (slice W3 only) |
| server → Player PC | *nothing* | *nothing* |

Windows Firewall's default posture blocks **inbound** and permits **outbound**,
so no prompt and no rule. The server side is behind the Cloudflare tunnel, which
is itself an outbound connection from the VM — there is no exposed port to open
there either. A player on a restrictive corporate or campus network who can
already reach the nav server can already reach this.

Two consequences worth recording:

**4.1 The WebSocket is not available to the watcher, and we don't want it to be.**
`/ws` authenticates from the session cookie only (`app.py` ~9009) — there is no
token path. Giving bearer tokens a WS route would widen a deliberately
browser-only surface for a feature that gains nothing from push. Polling and
piggybacking both avoid it. (Note that a WS would *also* have been outbound —
the push/pull choice never had firewall implications either way.)

**4.2 Cloudflare will treat a new GET exactly like it treated the POST.** The
watcher already carries a custom `User-Agent` because Cloudflare's bot filtering
403s `Python-urllib/x.y` before the request reaches the app, with dedicated
handling for it (`sc_nav_watcher.py` ~271, ~283–299). Any new request must reuse
the same header and the same 401/403 branch, and is covered by the same
"WAF Skip rule for `/api/*`" the watcher's own error message already recommends.

**4.3 The real cost is request volume, not firewalls.** Today a watcher sends
roughly one request per `/showlocation` plus one per 60 s. A 2 s overlay poll is
~1,800 requests/hour/player — 20–30× — against a single-worker origin where
position posts serializing behind `hub.lock` is a *documented* scaling cliff
(the delayed-updates + 502s seen with two watchers live). This is why §5 leads
with the piggyback and treats polling as a later, conditional slice.

## 5. Server changes

### 5.1 Slice W1 — return the frame we already built (zero new requests)

`POST /api/position` returns a lean destination slice instead of `{"ok": True}`:

```jsonc
{
  "ok": true,
  "nav": {
    "t": 1753440000.0,
    "system": "Stanton",
    "container": "Hurston",           // name only
    "destination": {                   // null when no target is set
      "name": "Lorville",
      "distance_m": 812345.0,
      "surface_distance_m": 41234.0,   // null off-body
      "bearing_deg": 118.4,            // null off-body — see §3.2
      "eta_s": 96.0,
      "same_container": true
    }
  }
}
```

Built from the frame already snapshotted at `app.py` ~2764 — no extra compute, no
extra lock time, no extra request. Existing clients ignoring the body are
unaffected.

The overlay therefore updates on every `/showlocation` and every heartbeat. Its
only blind spot: a destination changed **in the browser** isn't reflected until
the next post (≤60 s).

### 5.2 Slice W3 (conditional) — `GET /api/nav/summary`

Only if the ≤60 s browser-retarget lag proves annoying in real flight. `/api/state`
is not a candidate: it's `require_session` (cookie-only, `app.py` ~2937) and
returns `nearest_pois` + forecasts, far too heavy to poll.

The new endpoint would be `Depends(require_user)`, return the §5.1 `nav` object
and nothing else, and must:

- poll at **5 s**, not 2 s, and only while the overlay is actually visible;
- read a session snapshot **without taking `hub.lock`** (§4.3);
- **not** call `hub.touch_presence` or `record_crumb` — a read-only poll must not
  churn presence or pad the breadcrumb trail;
- support `If-None-Match` against a cheap `changed_at` token so the steady state
  is a 304.

**Rejected alternative:** shortening the heartbeat to ~10 s when the overlay is
on. Same apparent effect, but heartbeats *write* (presence upsert + breadcrumb,
`app.py` ~2748–2749), so it pays the expensive path to get a cheap read.

## 6. Watcher restructure — the actual work

Tkinter (stdlib, so the zero-dependency rule survives) must own the main thread.
Today `run()` is a blocking `while True` on main (`sc_nav_watcher.py` ~441–502).
So:

- **main thread** → `tk.mainloop()`;
- **daemon thread** → the existing clipboard / shard / send loop, with a
  `threading.Event` stop flag;
- **communication** → a `queue.Queue` of nav payloads, drained by
  `root.after(200, ...)`. Never touch a tk widget from the worker thread.

Two gotchas to budget for:

1. **Ctrl-C is inert inside a tk mainloop on Windows** unless a periodic `after`
   tick gives the interpreter a chance to run signal handlers. The existing
   `KeyboardInterrupt` → `log("stopped")` path (~556) must keep working, since
   that's how everyone quits the watcher today.
2. **Window close and worker death must each shut down the other** — a closed
   overlay leaving an orphan daemon posting positions, or a crashed worker leaving
   a frozen window on screen showing a stale distance, are both worse than a clean
   exit.

With `--overlay` off, the process must behave **exactly** as it does now:
same main-thread loop, no tk import, no behavioral delta. The overlay is a
strictly additive branch.

## 7. Enabling it — the startup question

Modeled directly on the existing handle prompt, which is the pattern members
already know (`run_watcher.bat` ~28–31: ask, then remember it in
`watcher_config.json` so later runs don't re-ask).

### 7.1 In the launcher

```bat
rem Blank = keep whatever you chose last time.
if "%OVERLAY%"=="" set /p OVERLAY=Show the in-game overlay (target/distance)? [Y/N, blank = use saved]:
```

The bat currently branches into two `%PYTHON% ...` invocations for
handle/no-handle. Adding a second tri-state would make four combinations, so
build the argument list up instead:

```bat
set ARGS=--server %SERVER%
if not "%HANDLE%"=="" set ARGS=%ARGS% --handle "%HANDLE%"
if /i "%OVERLAY%"=="Y" set ARGS=%ARGS% --overlay
if /i "%OVERLAY%"=="N" set ARGS=%ARGS% --no-overlay
%PYTHON% sc_nav_watcher.py %ARGS%
```

This also sidesteps the paren-block hazard the bat already documents at ~29–30
(prompt text containing parentheses breaks a `(...)` block — note the prompt
above deliberately contains `(target/distance)`, so it must stay a single-line
`if`).

### 7.2 Sticky, like the handle

`--overlay` / `--no-overlay` via `argparse.BooleanOptionalAction` with
`default=None`, so "not specified" is distinguishable from "explicitly off".
Then a boolean sibling of `_resolve_sticky` (~375) persists the answer into
`watcher_config.json` under `overlay`, alongside `handle`, `token`, `game_log`.

Resolution order: explicit flag → saved value → **off**.

**First-run default is off** (blank answer, nothing saved). A window appearing
unbidden over someone's cockpit the first time they double-click the bat is
exactly the surprise we're trying to avoid, and the prompt is right there every
run until they answer it. The console prints one line on startup —
`overlay: off (answer Y at the prompt to enable)` — so it stays discoverable.

### 7.3 Degrade, never crash

`import tkinter` fails on Python builds without tcl/tk (some Microsoft Store and
trimmed installs). Catch it, log
`overlay unavailable on this Python (no tkinter) — continuing without it`, and
run the normal loop. Same for any failure constructing the window. **The watcher's
core job — reporting position — must never be lost to an overlay problem.**

## 8. The overlay window

~150–200 lines, stdlib tkinter plus a little ctypes:

- borderless `Toplevel`: `overrideredirect(True)`, `-topmost True`,
  `-alpha 0.75`, `-transparentcolor` for the background;
- **click-through** so it can't eat a mouse click meant for the game:
  `SetWindowLongW(hwnd, GWL_EXSTYLE, ... | WS_EX_LAYERED | WS_EX_TRANSPARENT)`
  via ctypes (~20 lines, same `WinDLL` approach as `WindowsClipboard`);
- **drag to place**: a toggle that temporarily drops `WS_EX_TRANSPARENT` so the
  window can be dragged, then restores it; position persisted to config;
- content, per §3: target name · distance · ETA · bearing arrow *(on a body)* or
  closing/opening *(in space)* · staleness age · a dot when a capture is armed.

Follow [`../DESIGN.md`](../DESIGN.md) tokens for color where tk allows it, but
this is a 3-line HUD over a dark game — legibility (weight, contrast, a subtle
outline) beats brand fidelity.

**All formatting logic — distance units, age strings, closing/opening, bearing
arrow selection — lives in pure functions with no tk dependency**, so it is
testable headlessly in `watcher/test_parse.py`.

## 9. Bundle & packaging

Genuinely trivial. `_build_watcher_zip` (`app.py` ~9500) iterates
`WATCHER_BUNDLE_FILES` (~73), so:

- add `"sc_nav_overlay.py"` to that tuple — the zip picks it up automatically;
- the `run_watcher.bat` rewrite from §7.1 (the `set SERVER=` regex rewrite at
  ~9515 is unaffected — keep that line's shape intact);
- a README section covering the fullscreen caveat (§3.1) and how to turn it off;
- a line on the SPA Setup page's step 2 so people know the download now has it.

### 9.1 tkinter is not installable, so the launcher guides instead

Worth stating plainly because the instinct is to add an install step and there
isn't one: **`tkinter` cannot be pip-installed.** It ships with CPython, sourced
from the **"tcl/tk and IDLE"** checkbox in the python.org Windows installer —
ticked by default. So it's absent only when someone unticked it, or on a trimmed
or embedded distribution. `pip install tkinter` / `tk` resolves to unrelated
packages on PyPI and must never appear in our instructions.

The launcher therefore *detects and directs*: when the answer is `Y`, it probes
`%PYTHON% -c "import tkinter"` and, on failure, prints the actual repair
(Settings → Apps → Python → Modify → tick the box) and continues. The watcher
still runs; only the overlay is missing. Implemented with `goto`/`errorlevel`
rather than a `||` nested inside an `if (...)` block, which batch parses
inconsistently.

The probe fires only on a run where the user *types* `Y`. Someone who answered
`Y` previously and now presses blank (sticky-on) gets the Python-side log line
from §7.3 instead — quieter, and they've already been told once.

Three surfaces carry the same fact: the launcher probe, `watcher/README.md`
(install step + a troubleshooting entry), and the SPA Setup page's step 1
alongside the existing "Add python.exe to PATH" instruction.

`watcher_config.json` gains `overlay`, `overlay_x`, `overlay_y`, `overlay_alpha`
— all optional, all defaulted, no migration (the file is already best-effort
JSON, `_load_config` ~353 swallows a malformed one).

## 10. Slices

Each ships alone.

**W1 — the honest minimum. ✅ BUILT** (see the status header for the two
deviations). §5.1 piggyback response · §6 threading split · §7 opt-in prompt +
sticky config · §8 window showing target/distance/bearing/ETA/age, always-on-top,
drag-to-place, no click-through yet. **Zero new requests, zero new endpoints.**

**W2 — make it pleasant.** Click-through (`WS_EX_TRANSPARENT`) · opacity/scale
controls · in-space closing/opening · capture-armed dot. *(bearing arrow and
drag-to-place already landed in W1.)* Half a day.

**W3 — conditional.** §5.2 `GET /api/nav/summary` **only if** W1's ≤60 s
browser-retarget lag actually annoys people in flight. Then: nearest-POI line,
active pirate warnings near the route.

Deliberately sequenced so the request-volume question in §4.3 is answered by
real use rather than guessed at up front.

## 11. Testing

- `server/test_app.py` — the §5.1 response shape: with a destination, without
  one, and (W3) that the summary endpoint accepts a watcher token, rejects an
  anonymous caller, and does not mutate presence.
- `watcher/test_parse.py` — the §8 pure formatters and the §7.2 sticky-flag
  resolution (flag → saved → off). No display required.
- Manual, on the Windows box: borderless vs fullscreen (§3.1), no-tkinter
  degradation (§7.3), Ctrl-C and window-close shutdown (§6).

## 13. Heavy mode (beta) — pin the real app over the game

**Decided 2026-07-25 (user):** rather than port the SPA's maps to a tk canvas
(§13.1), offer the **whole web app** in a pinned app-mode browser window as an
opt-in **beta** alongside the light overlay. Startup becomes a three-way choice:
**none · light · heavy**.

### 13.1 Why not a tk canvas map

The obvious ask after W1 is "let me see the resource map over the game." Drawing
it in `tk.Canvas` is very doable (stdlib, no bundler) but the projection,
culling, labels and legend already exist in `index.html` — a Python port is a
**second implementation that must track the first forever**, and every map
change becomes two edits. Estimated 2.5–4 days plus permanent drift risk.

Heavy mode gets *every* map — navigator, Prospector, coverage, radar — for a
fraction of that, and it can never drift, because it **is** the SPA. It also
gets **live WebSocket updates**, which the watcher itself can't have (`/ws` is
cookie-only, §4.1) — so teammates move in real time, which the tk port could
only fake by polling.

### 13.2 The catch worth knowing up front

Your **own** marker is the stale one. Everything else on that page updates live
over the WS; your position only moves when you run `/showlocation`. This inverts
W1's honesty model — there, *everything* was as old as the fix. Heavy mode is
honest by construction (the SPA already renders teammate freshness), but the
asymmetry will feel odd until you know it.

### 13.3 Two Chromium behaviors that shape the implementation

1. **`--app=` reuses a running browser.** If the user's browser is already open
   on the default profile, our launch spawns a window in the *existing* process
   and our child exits immediately. So **PID-based window finding and teardown
   both break.** Windows are found by class + title, and closed with
   `WM_CLOSE`, never by killing our subprocess.
2. **A normal tab on the app has the same window title** (`Org Navigator`), so
   title matching alone could pin the user's ordinary browser window over their
   game. Fix: **snapshot matching HWNDs before launching** and adopt only a
   window that wasn't there before.

Use the **default profile** deliberately — that's what makes the session cookie
(and therefore login) already work. A `--user-data-dir` would force a fresh
Discord OAuth in a chrome-less window.

### 13.4 Shape

- Browser discovery: **Edge first** (guaranteed on Win10/11), then Chrome,
  standard install paths plus `PATH`. Neither found → log and fall back.
- Launch: `--app=<url>`, `--window-size`, `--window-position` (geometry via
  launch flags is more reliable than moving it afterwards).
- Pin: the same `SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE)` re-assert loop the
  light overlay uses, for the same reason — topmost is lost, not sticky.
- Teardown: `WM_CLOSE` to the adopted window when the watcher exits.
- Never blocks the watcher: no browser, no window within the timeout, or a
  non-Windows host → log one line and keep reporting position.

### 13.5 Config migration

`watcher_config.json` `overlay` was a **boolean**. It becomes a mode string;
legacy values migrate `true → "light"`, `false → "off"`. `--overlay` /
`--no-overlay` stay as aliases for light/off so existing launchers keep working;
`--overlay-mode {off,light,heavy}` is the new authoritative flag.

### 13.6 Why beta

Everything in §13.3 is Windows-specific and **cannot be verified from the dev
Mac** — browser discovery paths, HWND enumeration, the snapshot-diff adoption,
`WM_CLOSE` teardown. The light overlay at least rendered on macOS. Heavy mode
ships labelled beta because its riskiest half has never executed.

### 13.7 First in-game test failed — three causes (v0.84.1)

Reported: *"the browser opened, it would not stay on top."* Beta earned its
label. Three defects, any one of which produces exactly that:

1. **No `argtypes`/`restype` on the ctypes calls — the fatal one.** ctypes
   assumes C `int` (32-bit) for unspecified arguments and returns, so a 64-bit
   `HWND` was **silently truncated** and `SetWindowPos` operated on a handle
   that doesn't exist, failing with no error. This would fail 100% of the time
   on 64-bit Windows. Every function now declares its signature.
2. **The title gate was too strict.** If the browser profile isn't signed in,
   `--app` lands on Discord's OAuth page, titled *"Discord"* — so the window was
   never adopted. Selection now prefers a title match but falls back to **any
   new browser window**, which is safe because pre-existing windows are already
   excluded by the snapshot.
3. **Adoption ran only during `start()`.** A window appearing later — after a
   sign-in, or a slow first paint — was never picked up. `keep_pinned()` now
   keeps hunting, so the overlay simply starts working whenever the window
   turns up.

Selection is now the pure `pick_window(before, windows, title_match)`, tested
against all four cases (new match, pre-existing only, OAuth title, blank title
while loading). A failed pin logs the **Win32 error code** — with no Windows
here to reproduce on, that's the one diagnostic worth having.

**Unrelated red herring:** Discord may pop its own "enable overlay for this
app?" prompt when the browser launches. That's Discord's game overlay reacting
to a new process; it has nothing to do with this feature, and answering either
way changes nothing.

### 13.8 Second playtest — pinning works; two interaction findings (v0.84.2)

**The window now sits over the game correctly.** Two things surfaced once it
was actually usable:

1. **Every dropdown in the app snapped shut before you could pick anything —
   self-inflicted, by §13.7's fix.** Chromium dismisses an open `<select>`
   popup when its parent window receives `WM_WINDOWPOSCHANGED`, and
   `SetWindowPos` fires that **even with `SWP_NOMOVE|SWP_NOSIZE`**. Re-asserting
   every 2 s therefore put a ≤2 s fuse on every menu. Fixed by
   `should_repin(is_topmost, is_foreground)`: only touch the window when the
   topmost flag has genuinely been **lost**, and never while it is the
   foreground window — if the user is working in it, it's visible anyway, and
   interrupting is precisely the bug. In the steady state we now send *nothing*.
   **General lesson: an idle keep-alive that pokes a window is not free.**

2. **Mouse movement still drives the ship while the browser has the cursor.**
   Star Citizen reads the mouse via raw input regardless of which window has
   focus, so hovering the overlay can still turn you. **Not fixable from
   outside**: it would need input interception, which §3.4's no-injection rule
   rules out — and should. Alt-tabbing to the browser and back is the reliable
   way to park the game's controls; holding `F` frees the cursor but does not
   stop the game reading it. Documented in the watcher README rather than
   worked around.

## 12. Open questions

1. **First-run default** (§7.2) — designed as *off*. If you'd rather the
   downloaded bundle show it by default, that's a one-line change, but it means a
   window appears over the game before anyone has agreed to it.
2. **Second-monitor users** — for them the overlay is strictly worse than the
   browser. Worth a README line saying so rather than pretending it's for
   everyone.
3. **Does the org actually fly borderless?** (§3.1) One question in Discord
   before building W1 could save the whole feature from landing on players who
   can't see it.
