#!/usr/bin/env python3
"""SC Nav Watcher — heavy overlay, BETA (backlog #40 §13, #40.1 slice I1).

Pins the real web app over the game in a borderless app-mode browser window,
instead of re-drawing its maps in a second renderer we'd have to maintain
forever. You get every map the SPA has — navigator, Prospector, coverage,
radar — and they update **live over the WebSocket**, which the watcher itself
can never have (`/ws` is cookie-only; the browser has the cookie, we don't).

The catch, stated plainly because it's counterintuitive: **your own marker is
the stale one.** Teammates move in real time; you only move when you run
/showlocation. The light overlay is uniformly as old as your last fix — here
everything else is live and you are the ghost.

**It is inert while you are flying.** The window is click-through whenever Star
Citizen is the foreground app, so the cursor crossing it cannot eat a click —
reported from flight: swinging a tractor-beamed box to the right walked the
cursor onto the overlay and dropped the beam, repeatedly. Alt-tab to the window
(it has a taskbar button, unlike the light HUD) and it becomes an ordinary
window again; click back into the game and it goes inert. There is deliberately
no hold-a-key-to-interact gate here, unlike the light HUD: this is a window you
type into and open menus in, and a momentary key would turn the window
click-through underneath an open dropdown.

**It runs in its own browser profile** (`overlay-profile/` beside this script).
That costs one Discord sign-in the first time and buys three things that the
default profile cannot give:

  * **Our command-line flags actually apply.** `--app=` handed to an
    ALREADY-RUNNING browser is forwarded to that process, which was started
    with its own command line — so process-level switches are silently dropped.
    With a private `--user-data-dir` nothing is reused and the flags below are
    guaranteed. This matters because they are the freeze fix (§13.9).
  * **The window is ours, provably.** Our child process IS the browser, so a
    window can be matched by pid instead of by title — no chance of pinning the
    user's own browser window over their game.
  * **Recovery is safe.** A wedged window can be killed by pid without any risk
    of taking down the browsing session the pilot had open.

BETA because every Win32 path here is unverifiable off Windows. It must never
take the watcher down with it: any failure logs one line and returns.
"""

import os
import shutil
import subprocess
import time

try:
    import sc_nav_win32 as w32
except Exception:                                # pragma: no cover - packaging
    w32 = None

# Chromium's window class on Windows, shared by Edge and Chrome.
_CHROMIUM_CLASS = "Chrome_WidgetWin_1"

# The SPA's <title>. Used to recognize our window among all browser windows.
DEFAULT_TITLE_MATCH = "Org Navigator"

# How long to wait for the browser window to appear before giving up.
WINDOW_TIMEOUT_S = 20.0

# Browser profile directory, beside this script so it travels with the unzipped
# watcher folder and is obvious to delete if it ever needs resetting.
PROFILE_DIRNAME = "overlay-profile"

# Flags that keep the window alive under a fullscreen game.
#
# `CalculateNativeWinOcclusion` is Chromium deciding a covered window need not
# paint — and a window pinned over a fullscreen game looks exactly like a
# covered one to that heuristic. #40.1 §2.5 predicted this as the first suspect
# if heavy mode ever appeared to stall, and a flight then reported precisely
# that: the overlay froze and only a watcher restart brought it back. The
# backgrounding switches are the same story for timers and the renderer: a
# throttled renderer holds a stale frame, and a stale frame that still says
# "12.3 Mm" is the exact failure the whole overlay design refuses to ship
# (a frozen overlay lies).
STABILITY_FLAGS = (
    "--disable-features=CalculateNativeWinOcclusion",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    # A fresh profile otherwise opens a first-run interstitial inside a window
    # that has no address bar to escape it with.
    "--no-first-run",
    "--no-default-browser-check",
)

# Standard install locations, Edge first: it's guaranteed on Windows 10/11,
# Chrome may simply not be installed.
_BROWSER_CANDIDATES = (
    (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", "Edge"),
    (r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe", "Edge"),
    (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", "Chrome"),
    (r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe", "Chrome"),
)
_PATH_CANDIDATES = (("msedge", "Edge"), ("chrome", "Chrome"),
                    ("google-chrome", "Chrome"))


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a browser)
# ---------------------------------------------------------------------------


def find_browser(env=None, exists=None, which=None):
    """(path, name) of a Chromium browser to drive, or (None, None).

    Injectable lookups so the search order is testable without Windows."""
    env = os.environ if env is None else env
    exists = os.path.isfile if exists is None else exists
    which = shutil.which if which is None else which

    for template, name in _BROWSER_CANDIDATES:
        path = _expand(template, env)
        if path and exists(path):
            return path, name
    for command, name in _PATH_CANDIDATES:
        found = which(command)
        if found:
            return found, name
    return None, None


def _expand(template, env):
    """Expand %VAR% against a supplied environment. Returns None if a variable
    is missing — on a 32-bit-less box `%ProgramFiles(x86)%` simply isn't set,
    and os.path.expandvars would leave the literal in place and 'find' nothing."""
    out = template
    while "%" in out:
        start = out.find("%")
        end = out.find("%", start + 1)
        if end == -1:
            break
        name = out[start + 1:end]
        if name not in env:
            return None
        out = out[:start] + env[name] + out[end + 1:]
    return out


def default_profile_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        PROFILE_DIRNAME)


def browser_command(exe, url, geometry=None, profile_dir=None, flags=None):
    """The app-mode launch command.

    `profile_dir=None` reproduces the original behaviour — the DEFAULT profile,
    which carries the signed-in session cookie so the window opens already
    authenticated. It is kept as an escape hatch (`heavy_shared_profile` in
    watcher_config.json) but is no longer the default, because a reused browser
    process silently discards STABILITY_FLAGS and those are the freeze fix."""
    cmd = [exe, f"--app={url}"]
    if profile_dir:
        cmd.append(f"--user-data-dir={profile_dir}")
    cmd.extend(STABILITY_FLAGS if flags is None else flags)
    if geometry:
        x, y, w, h = geometry
        cmd.append(f"--window-size={int(w)},{int(h)}")
        cmd.append(f"--window-position={int(x)},{int(y)}")
    return cmd


def heavy_url(server, view="#/"):
    """Full URL for the app-mode window. `--app=` demands an absolute URL."""
    base = (server or "").rstrip("/")
    return f"{base}/{view.lstrip('/')}" if base else ""


def should_repin(is_topmost, is_foreground):
    """Whether to actually call SetWindowPos this round.

    Re-pinning blindly is what broke every dropdown in the app: Chromium
    dismisses an open <select> popup when its parent window receives
    WM_WINDOWPOSCHANGED, and SetWindowPos fires that **even with
    SWP_NOMOVE|SWP_NOSIZE**. A 2 s re-assert therefore put a ≤2 s fuse on every
    menu the user tried to open.

    So: only re-assert when the topmost flag has genuinely been lost, and never
    while the window is in the foreground — if it's foreground the user is
    working in it, it's plainly visible, and interrupting that is exactly the
    bug. Nothing to fix means nothing to send."""
    if is_topmost:
        return False
    return not is_foreground


def pick_window(before, windows, title_match, pids=None, want_pid=None):
    """Choose the window we just opened. Pure, so the selection rule is
    testable without Windows.

    `before` = handles seen BEFORE launching, `windows` = [(hwnd, cls, title)],
    `pids` = optional {hwnd: pid}.

    **Pid wins when we have one.** With a private profile our child process IS
    the browser (nothing is reused), so a window owned by that pid is ours with
    certainty — which retires the whole class of "we pinned the user's own
    browser window over their game" bug that title matching can only mitigate.

    Otherwise: prefer a NEW window whose title matches, but fall back to ANY new
    browser window. If the profile isn't signed in, `--app` lands on Discord's
    OAuth page — titled "Discord", not "Org Navigator" — and a strict title gate
    would refuse to adopt it and silently never pin anything. A window that is
    both new and a browser window, moments after we launched a browser, is
    ours."""
    fresh = [w for w in windows if w[0] not in before]
    if not fresh:
        return None
    if want_pid and pids:
        for hwnd, _cls, _title in fresh:
            if pids.get(hwnd) == want_pid:
                return hwnd
    wanted = (title_match or "").lower()
    for hwnd, _cls, title in fresh:
        if wanted and wanted in (title or "").lower():
            return hwnd
    return fresh[-1][0]


def should_recover(hung_ticks, threshold, recoveries, limit):
    """Whether to tear the window down and relaunch it.

    Two guards, both from the reported failure. `threshold` demands the window
    has been hung for several consecutive polls — Chromium legitimately stops
    pumping for a moment on a heavy first paint, and a hair-trigger would
    relaunch the app out from under a pilot who was only waiting for a map to
    draw. `limit` stops an unrecoverable cause (a GPU that keeps dying) from
    becoming an infinite relaunch loop, which would be worse than the freeze."""
    return hung_ticks >= threshold and recoveries < limit


# ---------------------------------------------------------------------------
# The mode
# ---------------------------------------------------------------------------


class HeavyOverlay:
    """Launch the app-mode window, adopt it, keep it pinned and inert while the
    game is in front, notice when it wedges, and close it on exit."""

    # Polls per second drive how quickly the window becomes clickable after an
    # alt-tab, so this is a responsiveness number, not a keep-alive: the pin
    # itself is still only re-asserted when the flag has genuinely been lost.
    POLL_EVERY_S = 0.25
    # ~1 s between EnumWindows sweeps while we're still hunting for the window.
    ADOPT_EVERY = 4
    # ~5 s of a hung window before we act — on top of the ~5 s IsHungAppWindow
    # itself waits before saying yes.
    HANG_TICKS = 20
    MAX_RECOVERIES = 3

    def __init__(self, url, config=None, log=print):
        self.url = url
        self.config = config or {}
        self.log = log
        self.hwnd = None
        self._win = w32.load(log) if w32 is not None else None
        self._fg = w32.ForegroundWatcher(
            self._win, self._game_exes()) if w32 is not None else None
        self._proc = None
        self._before = set()
        self._title_match = DEFAULT_TITLE_MATCH
        self._misses = 0
        self._ticks = 0
        self._hung_ticks = 0
        self._recoveries = 0
        self._inert = None            # None = never applied yet
        self._warned_no_clickthrough = False

    # -- config ------------------------------------------------------------
    def _game_exes(self):
        name = self.config.get("game_exe")
        return (str(name).lower(),) if name else w32.GAME_EXES

    def _profile_dir(self):
        """None when the user has opted back into the shared default profile."""
        if self.config.get("heavy_shared_profile"):
            return None
        return self.config.get("heavy_profile_dir") or default_profile_dir()

    def _clickthrough_mode(self):
        """`on` (default) · `layered` (adds WS_EX_LAYERED) · `off`.

        `layered` exists because WS_EX_TRANSPARENT alone is the documented way
        for hit-testing to skip a window and is what we try first, but adding
        the layered bit is the belt-and-braces form used elsewhere and none of
        it can be verified off Windows. One config line beats a code change and
        a redeploy if the first flight says clicks still land on the app."""
        mode = str(self.config.get("heavy_clickthrough", "on")).lower()
        return mode if mode in ("on", "off", "layered") else "on"

    def _geometry(self):
        try:
            return (int(self.config.get("heavy_x", 60)),
                    int(self.config.get("heavy_y", 60)),
                    int(self.config.get("heavy_w", 720)),
                    int(self.config.get("heavy_h", 520)))
        except (TypeError, ValueError):
            return (60, 60, 720, 520)

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        """Launch and adopt the window. False if heavy mode can't run."""
        exe, name = find_browser()
        if not exe:
            self.log("heavy overlay: no Edge or Chrome found — falling back. "
                     "Install either, or use the light overlay.")
            return False
        self._title_match = self.config.get("heavy_title", DEFAULT_TITLE_MATCH)

        # Snapshot first: a normal tab already on the app shares this title, and
        # pinning the user's ordinary browser window over their game would be a
        # genuinely bad bug (§13.3). Belt and braces now that pid matching
        # exists — the snapshot costs one EnumWindows.
        self._before = {w[0] for w in self._browser_windows()}

        profile = self._profile_dir()
        if profile and not os.path.isdir(profile):
            self.log("heavy overlay: first run uses its own browser profile, "
                     "so the window will ask you to sign in once. After that "
                     "it opens straight into the app.")
        cmd = browser_command(exe, self.url, self._geometry(), profile)
        try:
            self._proc = subprocess.Popen(cmd)
        except OSError as exc:
            self.log(f"heavy overlay: could not launch {name} ({exc})")
            return False
        self.log(f"heavy overlay (BETA): opened the app in {name}")

        if self._win is None:
            self.log("heavy overlay: not Windows — the window won't be pinned "
                     "on top (it's a normal browser window)")
            return True

        deadline = time.monotonic() + WINDOW_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._adopt():
                return True
            time.sleep(0.25)
        # NOT a failure: signing in, or a slow first paint, can easily outlast
        # the startup window. tick() goes on trying, so the overlay simply
        # starts working whenever the window finally shows up.
        self.log("heavy overlay: no window yet (still loading, or you may need "
                 "to sign in) — will keep watching for it")
        return True

    def _browser_windows(self):
        if self._win is None:
            return []
        try:
            return self._win.windows_of_class(_CHROMIUM_CLASS)
        except Exception:
            return []

    def _child_pid(self):
        """Our browser process id — meaningful ONLY with a private profile.

        On the shared profile `--app=` is forwarded to an already-running
        browser and our child exits immediately, so its pid identifies nothing;
        `poll()` catching that exit is what keeps us from matching a recycled
        pid later."""
        if self._proc is None or self._profile_dir() is None:
            return None
        try:
            if self._proc.poll() is not None:
                return None
        except Exception:
            return None
        return self._proc.pid

    def _adopt(self):
        """Find and pin the window we opened. Safe to call repeatedly."""
        windows = self._browser_windows()
        pids = None
        want = self._child_pid()
        if want:
            try:
                pids = {h: self._win.window_pid(h) for h, _c, _t in windows}
            except Exception:
                pids = None
        hwnd = pick_window(self._before, windows, self._title_match, pids, want)
        if hwnd is None:
            return False
        self.hwnd = hwnd
        self._inert = None                 # re-apply on the next tick
        title = next((t for h, _c, t in windows if h == hwnd), "")
        if self._win.pin(hwnd):
            self.log(f'heavy overlay: pinned "{title}"')
        else:
            # Surface the Win32 error code: if this ever fires it is the one
            # piece of information worth having, and there's no Windows here
            # to reproduce it on.
            err = getattr(self._win, "last_error", 0)
            self.log(f'heavy overlay: found "{title}" but Windows refused to '
                     f"pin it (error {err})")
        return True

    # -- per-tick work -----------------------------------------------------
    def keep_pinned(self):
        """One tick: foreground → click-through → hang check → pin.

        Named for what it did in W1 and still does; everything added here rides
        the same tick rather than starting timers of its own. Wholly guarded —
        the watcher's actual job is reporting position, and no overlay
        bookkeeping may interrupt it."""
        if self._win is None:
            return
        self._ticks += 1
        try:
            if self._fg is not None:
                self._fg.poll()
            if self.hwnd and self._win.alive(self.hwnd):
                # Each capability is guarded on its own: an old or unusual
                # Windows that can't do one of these must still get a pinned
                # window, which is the feature that actually shipped.
                try:
                    self._apply_click_through()
                except Exception:
                    pass
                if self._check_hung():
                    return
                # Touch the window ONLY when the flag has actually been lost —
                # see should_repin(). An unconditional re-assert closed every
                # dropdown in the app within 2 s.
                if should_repin(self._win.is_topmost(self.hwnd),
                                self._win.is_foreground(self.hwnd)):
                    self._win.pin(self.hwnd)
                return
            self.hwnd = None
            self._hung_ticks = 0
            # Hunt on the first tick and every ADOPT_EVERY after it. Adoption
            # is an EnumWindows sweep and this state is not always transient —
            # close the overlay window and keep playing, and we'd otherwise
            # enumerate every window on the box four times a second forever.
            if (self._ticks - 1) % self.ADOPT_EVERY:
                return
            self._misses += 1
            # Quietly retry; say something once, well after the obvious causes
            # (sign-in, slow load) would have resolved.
            if self._adopt():
                self._misses = 0
            elif self._misses == 60:
                self.log("heavy overlay: still no browser window to pin. If you "
                         "signed in, try relaunching; otherwise use the light "
                         "overlay (answer L at the launcher).")
        except Exception:
            pass

    def _apply_click_through(self):
        """Inert while the game is in front, ordinary window otherwise."""
        mode = self._clickthrough_mode()
        if mode == "off" or self._fg is None:
            return
        inert = not w32.overlay_interactive(
            self._fg.known, self._fg.game_foreground)
        if inert == self._inert:
            return
        self._win.click_through(self.hwnd, inert, layered=(mode == "layered"))
        self._inert = inert
        if inert and not self._warned_no_clickthrough:
            self._warned_no_clickthrough = True
            self.log("heavy overlay: click-through is on while Star Citizen is "
                     "in front — alt-tab to the overlay to use it.")

    def _check_hung(self):
        """True when this tick was spent recovering a wedged window."""
        try:
            hung = self._win.is_hung(self.hwnd)
        except Exception:
            return False
        if not hung:
            self._hung_ticks = 0
            return False
        self._hung_ticks += 1
        if not should_recover(self._hung_ticks, self.HANG_TICKS,
                              self._recoveries, self.MAX_RECOVERIES):
            return False
        if not self.config.get("heavy_autorecover", True):
            if self._hung_ticks == self.HANG_TICKS:
                self.log("heavy overlay: the window has stopped responding "
                         "(auto-recovery is off — close it and restart the "
                         "watcher).")
            return False
        self._recoveries += 1
        self.log(f"heavy overlay: the window stopped responding — reopening it "
                 f"({self._recoveries}/{self.MAX_RECOVERIES}). Your trade or "
                 f"cargo run is server-side and is not affected.")
        self._hung_ticks = 0
        self.stop(quiet=True)
        if not self.start():
            self.log("heavy overlay: could not reopen the window — carrying on "
                     "without it.")
        elif self._recoveries >= self.MAX_RECOVERIES:
            self.log("heavy overlay: that's the last automatic reopen. If it "
                     "wedges again, use the light overlay (answer L at the "
                     "launcher) and please report it.")
        return True

    def stop(self, quiet=False):
        """Close the window WE opened — never the user's other browser windows.

        WM_CLOSE first: it is the clean shutdown, and it is the only correct
        path on the shared profile where our child exited instantly and its pid
        means nothing. On our own profile the child really is the browser, so a
        window that ignores WM_CLOSE — which is exactly what a hung window
        does — can be killed by pid with no risk to anything the pilot had
        open. That safety is bought by the private profile, and it is why
        recovery from a wedge is possible at all."""
        hwnd = self.hwnd
        try:
            if self._win is not None and hwnd and self._win.alive(hwnd):
                self._win.close(hwnd)
        except Exception:
            pass
        self.hwnd = None
        self._inert = None
        try:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.wait(timeout=2.0)     # WM_CLOSE may have done it
                except Exception:
                    self._proc.terminate()
        except Exception:
            pass
        # A hung window will not have processed WM_CLOSE. Own profile only.
        try:
            if (self._win is not None and hwnd and self._win.alive(hwnd)
                    and self._profile_dir() is not None):
                pid = self._win.window_pid(hwnd)
                if pid and pid != self._win.own_pid():
                    self._win.terminate(pid)
                    if not quiet:
                        self.log("heavy overlay: the window ignored a close "
                                 "request and was ended.")
        except Exception:
            pass


def run(url, config=None, log=print, stop=None):
    """Blocking heavy-mode loop: launch, then hold the window on top until
    `stop` is set. Returns False if heavy mode couldn't start at all."""
    overlay = HeavyOverlay(url, config=config, log=log)
    try:
        if not overlay.start():
            return False
    except Exception as exc:
        log(f"heavy overlay: failed to start ({exc}) — continuing without it")
        return False
    try:
        while stop is None or not stop.is_set():
            overlay.keep_pinned()
            if stop is None:
                break
            stop.wait(HeavyOverlay.POLL_EVERY_S)
    finally:
        overlay.stop()
    return True
