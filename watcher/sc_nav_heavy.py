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


class _Win32:
    """Thin ctypes wrapper. Constructed only on Windows; None elsewhere."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)

    def matching_windows(self, title_match):
        """Visible Chromium windows whose title contains `title_match`."""
        ctypes, wintypes = self._ctypes, self._wintypes
        found = []
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd, cls, 256)
            if cls.value != _CHROMIUM_CLASS:
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buf, length + 1)
            if title_match.lower() in buf.value.lower():
                found.append(hwnd)
            return True

        self.user32.EnumWindows(proto(callback), 0)
        return found

    def pin(self, hwnd):
        # HWND_TOPMOST=-1; SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE — raise it
        # without stealing focus from the game.
        self.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

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
        before = set(self._win.matching_windows(title_match)) if self._win else set()

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
            fresh = [h for h in self._win.matching_windows(title_match)
                     if h not in before]
            if fresh:
                self.hwnd = fresh[-1]
                self._win.pin(self.hwnd)
                return True
            time.sleep(0.25)
        self.log("heavy overlay: the browser window never appeared — it may "
                 "still be loading, or you may need to sign in. Not pinned.")
        return True

    def keep_pinned(self):
        """Re-assert topmost; re-adopt if the window went away. Same reason as
        the light overlay: topmost is lost, not sticky."""
        if self._win is None:
            return
        try:
            if self.hwnd and self._win.alive(self.hwnd):
                self._win.pin(self.hwnd)
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
