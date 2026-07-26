#!/usr/bin/env python3
"""SC Nav Watcher — heavy overlay, BETA (backlog #40 §13).

Pins the real web app over the game in a borderless app-mode browser window,
instead of re-drawing its maps in a second renderer we'd have to maintain
forever. You get every map the SPA has — navigator, Prospector, coverage,
radar — and they update **live over the WebSocket**, which the watcher itself
can never have (`/ws` is cookie-only; the browser has the cookie, we don't).

The catch, stated plainly because it's counterintuitive: **your own marker is
the stale one.** Teammates move in real time; you only move when you run
/showlocation. The light overlay is uniformly as old as your last fix — here
everything else is live and you are the ghost.

Two Chromium behaviors drive the odd-looking bits below:

  * `--app=` REUSES an already-running browser, so our child process exits
    immediately and its PID tells us nothing. Windows are therefore found by
    class+title and closed with WM_CLOSE — never by killing our subprocess.
  * A normal tab on the same app has the SAME window title, so title matching
    alone could pin the user's ordinary browser window over their game. We
    snapshot matching windows BEFORE launching and adopt only a new one.

BETA because every Win32 path here is unverifiable off Windows. It must never
take the watcher down with it: any failure logs one line and returns.
"""

import os
import shutil
import subprocess
import time

# Chromium's window class on Windows, shared by Edge and Chrome.
_CHROMIUM_CLASS = "Chrome_WidgetWin_1"

# The SPA's <title>. Used to recognize our window among all browser windows.
DEFAULT_TITLE_MATCH = "Org Navigator"

# How long to wait for the browser window to appear before giving up.
WINDOW_TIMEOUT_S = 20.0

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


def browser_command(exe, url, geometry=None):
    """The app-mode launch command.

    NO --user-data-dir on purpose: the default profile is what carries the
    signed-in session cookie, so the window opens already authenticated. A
    private profile would strand the user at a Discord OAuth prompt inside a
    window with no address bar."""
    cmd = [exe, f"--app={url}"]
    if geometry:
        x, y, w, h = geometry
        cmd.append(f"--window-size={int(w)},{int(h)}")
        cmd.append(f"--window-position={int(x)},{int(y)}")
    return cmd


def heavy_url(server, view="#/"):
    """Full URL for the app-mode window. `--app=` demands an absolute URL."""
    base = (server or "").rstrip("/")
    return f"{base}/{view.lstrip('/')}" if base else ""


# ---------------------------------------------------------------------------
# Win32 window plumbing
# ---------------------------------------------------------------------------


def pick_window(before, windows, title_match):
    """Choose the window we just opened. Pure, so the selection rule is
    testable without Windows.

    `before` = handles seen BEFORE launching, `windows` = [(hwnd, cls, title)].

    Prefer a NEW window whose title matches, but fall back to ANY new browser
    window: if the browser profile wasn't signed in, `--app` lands on Discord's
    OAuth page — titled "Discord", not "Org Navigator" — and a strict title gate
    would refuse to adopt it and silently never pin anything. A window that is
    both new and a browser window, moments after we launched a browser, is ours."""
    fresh = [w for w in windows if w[0] not in before]
    if not fresh:
        return None
    wanted = (title_match or "").lower()
    for hwnd, _cls, title in fresh:
        if wanted and wanted in (title or "").lower():
            return hwnd
    return fresh[-1][0]


class _Win32:
    """Thin ctypes wrapper. Constructed only on Windows; None elsewhere.

    EVERY function gets explicit argtypes/restype. Without them ctypes assumes
    C `int` — 32 bits — for arguments and returns, so a 64-bit HWND is silently
    TRUNCATED and calls like SetWindowPos operate on a handle that doesn't
    exist and fail with no error. That is exactly why the first cut of heavy
    mode opened the browser and never pinned it."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        self._proto = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [self._proto, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        self.user32 = user32

    def browser_windows(self):
        """Every visible Chromium-class window as (hwnd, class, title)."""
        ctypes = self._ctypes
        found = []

        def callback(hwnd, _lparam):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd, cls, 256)
            if cls.value != _CHROMIUM_CLASS:
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            if length:
                self.user32.GetWindowTextW(hwnd, buf, length + 1)
            found.append((hwnd, cls.value, buf.value))
            return True

        # Keep a reference to the trampoline for the duration of the call —
        # letting it be collected mid-enumeration would crash the interpreter.
        cb = self._proto(callback)
        self.user32.EnumWindows(cb, 0)
        return found

    def pin(self, hwnd):
        # HWND_TOPMOST=-1; SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE — raise it
        # without stealing focus from the game.
        ok = bool(self.user32.SetWindowPos(
            hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010))
        self.last_error = 0 if ok else self._ctypes.get_last_error()
        return ok

    def close(self, hwnd):
        self.user32.PostMessageW(hwnd, 0x0010, 0, 0)     # WM_CLOSE

    def alive(self, hwnd):
        return bool(self.user32.IsWindow(hwnd))


def _win32(log):
    try:
        import ctypes

        if not hasattr(ctypes, "WinDLL"):
            return None
        return _Win32()
    except Exception as exc:
        log(f"heavy overlay: no Win32 access ({exc})")
        return None


# ---------------------------------------------------------------------------
# The mode
# ---------------------------------------------------------------------------


class HeavyOverlay:
    """Launch the app-mode window, adopt it, keep it pinned, close it on exit."""

    PIN_EVERY_S = 2.0

    def __init__(self, url, config=None, log=print):
        self.url = url
        self.config = config or {}
        self.log = log
        self.hwnd = None
        self._win = _win32(log)
        self._proc = None
        self._before = set()
        self._title_match = DEFAULT_TITLE_MATCH
        self._misses = 0

    def _geometry(self):
        try:
            return (int(self.config.get("heavy_x", 60)),
                    int(self.config.get("heavy_y", 60)),
                    int(self.config.get("heavy_w", 720)),
                    int(self.config.get("heavy_h", 520)))
        except (TypeError, ValueError):
            return (60, 60, 720, 520)

    def start(self):
        """Launch and adopt the window. False if heavy mode can't run."""
        exe, name = find_browser()
        if not exe:
            self.log("heavy overlay: no Edge or Chrome found — falling back. "
                     "Install either, or use the light overlay.")
            return False
        title_match = self.config.get("heavy_title", DEFAULT_TITLE_MATCH)

        # Snapshot first: a normal tab already on the app shares this title, and
        # pinning the user's ordinary browser window over their game would be a
        # genuinely bad bug (§13.3).
        self._title_match = title_match
        self._before = {w[0] for w in self._win.browser_windows()} if self._win else set()

        cmd = browser_command(exe, self.url, self._geometry())
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
        # the startup window. keep_pinned() goes on trying, so the overlay
        # simply starts working whenever the window finally shows up.
        self.log("heavy overlay: no window yet (still loading, or you may need "
                 "to sign in) — will keep watching for it")
        return True

    def _adopt(self):
        """Find and pin the window we opened. Safe to call repeatedly."""
        windows = self._win.browser_windows()
        hwnd = pick_window(self._before, windows, self._title_match)
        if hwnd is None:
            return False
        self.hwnd = hwnd
        title = next((t for h, _c, t in windows if h == hwnd), "")
        pinned = self._win.pin(hwnd)
        if pinned:
            self.log(f'heavy overlay: pinned "{title}"')
        else:
            # Surface the Win32 error code: if this ever fires it is the one
            # piece of information worth having, and there's no Windows here
            # to reproduce it on.
            err = getattr(self._win, "last_error", 0)
            self.log(f'heavy overlay: found "{title}" but Windows refused to '
                     f"pin it (error {err})")
        return True

    def keep_pinned(self):
        """Re-assert topmost, and keep hunting for the window if we haven't
        adopted one yet. Two reasons this must retry rather than run once:
        topmost is lost rather than sticky (same as the light overlay), and the
        window may not exist until the user finishes signing in."""
        if self._win is None:
            return
        try:
            if self.hwnd and self._win.alive(self.hwnd):
                self._win.pin(self.hwnd)
                return
            self.hwnd = None
            self._misses += 1
            # Quietly retry; say something once, well after the obvious causes
            # (sign-in, slow load) would have resolved.
            if self._adopt():
                self._misses = 0
            elif self._misses == 30:
                self.log("heavy overlay: still no browser window to pin. If you "
                         "signed in, try relaunching; otherwise use the light "
                         "overlay (answer L at the launcher).")
        except Exception:
            pass

    def stop(self):
        """Close the window WE opened — never the user's other browser windows.

        WM_CLOSE to the adopted window is the correct path when the browser was
        already running (our child exited instantly and its PID means nothing).
        When it was NOT already running, our child really is the browser, so
        terminating it is both valid and the only way to clean up off Windows,
        where there is no WM_CLOSE to send. Try the window first, the process
        second; a browser the user opened themselves is never touched either way."""
        try:
            if self._win is not None and self.hwnd and self._win.alive(self.hwnd):
                self._win.close(self.hwnd)
        except Exception:
            pass
        self.hwnd = None
        try:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.wait(timeout=2.0)     # WM_CLOSE may have done it
                except Exception:
                    self._proc.terminate()
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
            stop.wait(HeavyOverlay.PIN_EVERY_S)
    finally:
        overlay.stop()
    return True
