#!/usr/bin/env python3
"""Shared Win32 plumbing for both overlays (backlog #40.1, slice I1).

Two overlays, one set of window questions: *is the game in front right now*,
*should we be click-through*, *is that window hung*. Both used to answer them
separately — the light HUD with a bare `WinDLL` and no `argtypes` at all, which
is the trap the heavy module's docstring calls the first thing to check in any
Win32 work here. This file is the single argtypes-correct answer.

⚠ **`argtypes`/`restype` are mandatory, on every function, forever.** Without
them ctypes assumes C `int` — 32 bits — for arguments and returns, so a 64-bit
HWND is silently TRUNCATED and the call operates on a handle that does not
exist. It fails with no error and no output. #40 paid for this once already
(heavy mode opened the browser and never pinned it); the light HUD has been
quietly getting away with it because HWND values usually happen to fit.

Everything above the `Win32` class is pure and unit-tested off Windows; the
class itself is constructed only where `ctypes.WinDLL` exists and is `None`
everywhere else, so importing this module on the dev Mac is harmless.
"""

import os

# --- what "the game is in front" means -------------------------------------
#
# Matched on the executable's base name, lower-cased. A tuple, and overridable
# from watcher_config.json (`game_exe`), because this is the one string that
# would silently disable the whole slice if CIG renamed the binary — every rule
# below degrades to "always interactive", i.e. exactly today's behaviour.
# The RSI launcher is deliberately NOT here: while it's in front you are not
# flying, and an inert HUD would just be a HUD you can't move.
GAME_EXES = ("starcitizen.exe",)

# Virtual-key code for the interact key. F is deliberate: it is the same key SC
# itself uses to free the cursor for in-game menus, so "cursor free" and "HUD
# live" are the same gesture rather than a second thing to remember.
DEFAULT_INTERACT_KEY = "F"

# Ex-style bits and messages we touch. Spelled out rather than imported so the
# numbers are greppable against MSDN.
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
WM_CLOSE = 0x0010
MONITOR_DEFAULTTONEAREST = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001


# ---------------------------------------------------------------------------
# Pure helpers (no ctypes — unit-tested in test_parse.py on any platform)
# ---------------------------------------------------------------------------


def exe_name(path):
    """Base name of a process image path, lower-cased. `''` for nothing."""
    if not path:
        return ""
    return os.path.basename(str(path).replace("\\", "/")).lower()


def is_game(path, names=GAME_EXES):
    return exe_name(path) in tuple(names)


def overlay_interactive(known, game_foreground, key_held=False,
                        busy=False):
    """Whether the overlay window should accept mouse input right now.

    The rule both overlays share, and the whole point of slice I1:

      * **The game is in front → be inert.** This is the bug reported from
        flight: swinging a tractor-beamed box across the screen walks the cursor
        onto the overlay, which happily eats the click and drops the beam. A
        click-through window is not a hit-test target at all, so the cursor is
        over the *game* as far as Windows is concerned.
      * **Anything else is in front → be normal.** You alt-tabbed to it, or the
        game isn't running; there is nothing to protect.
      * **`key_held` overrides** so the light HUD can still be dragged without
        alt-tabbing (§3.3). Heavy mode passes False and uses alt-tab only —
        see its docstring for why a momentary key is the wrong gate for a
        window you type and pick menu items in.
      * **`busy` pins us interactive** mid-gesture: flipping click-through on
        during a drag would drop the drag on the floor.

    Fails SAFE. Until the foreground watcher has answered once (`known` False)
    we behave exactly as the shipped versions do — interactive. A broken or
    unavailable helper must never leave the window silently unclickable, which
    is a far worse failure than the one being fixed."""
    if not known:
        return True
    if busy or key_held:
        return True
    return not game_foreground


def covers_monitor(win_rect, mon_rect, slack=2):
    """True when a window covers its whole monitor (within `slack` px).

    Note what this can and cannot tell you: borderless-windowed and exclusive
    fullscreen produce the SAME rect, so this is not a fullscreen-mode detector.
    It is only ever used to phrase a conditional hint — "if you can't see the
    HUD, you're in exclusive fullscreen" — which is the honest form of the
    warning the parent doc promised."""
    if not win_rect or not mon_rect:
        return False
    wl, wt, wr, wb = win_rect
    ml, mt, mr, mb = mon_rect
    return (wl <= ml + slack and wt <= mt + slack
            and wr >= mr - slack and wb >= mb - slack)


def point_in_rect(x, y, rect):
    if not rect:
        return False
    left, top, right, bottom = rect
    return left <= x < right and top <= y < bottom


def vk_code(key):
    """Virtual-key code for a single-letter/digit interact key.

    Anything we don't recognise falls back to F rather than raising — this comes
    out of a hand-edited config file, and a typo there must not take the
    overlay down."""
    text = (key or "").strip().upper()
    if len(text) == 1 and (text.isalpha() or text.isdigit()):
        return ord(text)
    return ord(DEFAULT_INTERACT_KEY)


# ---------------------------------------------------------------------------
# The ctypes wrapper
# ---------------------------------------------------------------------------


class Win32:
    """Every user32/kernel32 call the overlays make, with explicit types.

    Constructed only on Windows — use `load()`, which returns None elsewhere."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

        self._RECT, self._MONITORINFO = RECT, MONITORINFO

        self._enum_proto = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        sig = [
            (user32, "GetForegroundWindow", [], wintypes.HWND),
            (user32, "IsWindow", [wintypes.HWND], wintypes.BOOL),
            (user32, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
            (user32, "IsHungAppWindow", [wintypes.HWND], wintypes.BOOL),
            (user32, "GetAsyncKeyState", [ctypes.c_int], ctypes.c_short),
            (user32, "GetWindowRect", [wintypes.HWND, ctypes.POINTER(RECT)],
             wintypes.BOOL),
            (user32, "MonitorFromWindow", [wintypes.HWND, wintypes.DWORD],
             wintypes.HANDLE),
            (user32, "GetMonitorInfoW",
             [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)], wintypes.BOOL),
            (user32, "GetWindowThreadProcessId",
             [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (user32, "SetWindowPos",
             [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
              ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            (user32, "PostMessageW",
             [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
             wintypes.BOOL),
            (user32, "GetClassNameW",
             [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (user32, "GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int),
            (user32, "GetWindowTextW",
             [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (user32, "EnumWindows", [self._enum_proto, wintypes.LPARAM],
             wintypes.BOOL),
            (user32, "GetParent", [wintypes.HWND], wintypes.HWND),
            (kernel32, "OpenProcess",
             [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (kernel32, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            (kernel32, "TerminateProcess", [wintypes.HANDLE, wintypes.UINT],
             wintypes.BOOL),
            (kernel32, "GetCurrentProcessId", [], wintypes.DWORD),
            (kernel32, "QueryFullProcessImageNameW",
             [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
              ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        ]
        for dll, name, argtypes, restype in sig:
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = argtypes, restype

        # The Ptr variants exist only in the 64-bit user32; on x86 the plain
        # ones ARE the pointer-sized ones. c_ssize_t either way, so a style
        # word is never truncated on the way in or out.
        self._get_long = getattr(user32, "GetWindowLongPtrW", None) or \
            user32.GetWindowLongW
        self._set_long = getattr(user32, "SetWindowLongPtrW", None) or \
            user32.SetWindowLongW
        self._get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self._get_long.restype = ctypes.c_ssize_t
        self._set_long.argtypes = [wintypes.HWND, ctypes.c_int,
                                   ctypes.c_ssize_t]
        self._set_long.restype = ctypes.c_ssize_t

        self.user32, self.kernel32 = user32, kernel32
        self.last_error = 0

    # -- identity ----------------------------------------------------------
    def foreground_window(self):
        return self.user32.GetForegroundWindow()

    def own_pid(self):
        return int(self.kernel32.GetCurrentProcessId())

    def window_pid(self, hwnd):
        pid = self._wintypes.DWORD(0)
        self.user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
        return int(pid.value)

    def process_path(self, pid):
        """Full image path for a pid, or ''.

        PROCESS_QUERY_LIMITED_INFORMATION (not ..._QUERY_INFORMATION) is the
        point: it is the right that works against a process running at a
        different integrity level, which the game frequently is. The wider
        right would simply be denied and we'd report 'unknown foreground app'
        for the one window we care most about."""
        if not pid:
            return ""
        handle = self.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = self._wintypes.DWORD(1024)
            buf = self._ctypes.create_unicode_buffer(size.value)
            ok = self.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, self._ctypes.byref(size))
            return buf.value if ok else ""
        finally:
            self.kernel32.CloseHandle(handle)

    def terminate(self, pid):
        if not pid:
            return False
        handle = self.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(self.kernel32.TerminateProcess(handle, 1))
        finally:
            self.kernel32.CloseHandle(handle)

    # -- window state ------------------------------------------------------
    def alive(self, hwnd):
        return bool(self.user32.IsWindow(hwnd))

    def is_hung(self, hwnd):
        """Whether the window has stopped pumping its message queue.

        This is the same test Explorer uses to draw "(Not Responding)": true
        after ~5 s without a message pumped. It is the only cheap way to tell a
        wedged overlay from a working one, and without it the watcher happily
        keeps re-pinning a dead window forever (`IsWindow` stays true)."""
        return bool(self.user32.IsHungAppWindow(hwnd))

    def is_topmost(self, hwnd):
        return bool(self._get_long(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)

    def is_foreground(self, hwnd):
        fg = self.foreground_window()
        try:
            return bool(fg) and int(fg) == int(hwnd)
        except (TypeError, ValueError):
            return False

    def key_down(self, vk):
        """GetAsyncKeyState's high bit = "down right now".

        Passive: it reads keyboard state and consumes nothing, so the keystroke
        still reaches the game. That is also why it works where RegisterHotKey
        does not — SC takes the keyboard through raw input and a registered
        hotkey never fires in-game (#40.1 §2.4)."""
        return bool(self.user32.GetAsyncKeyState(int(vk)) & 0x8000)

    def window_rect(self, hwnd):
        rect = self._RECT()
        if not self.user32.GetWindowRect(hwnd, self._ctypes.byref(rect)):
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)

    def monitor_rect(self, hwnd):
        mon = self.user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not mon:
            return None
        info = self._MONITORINFO()
        info.cbSize = self._ctypes.sizeof(self._MONITORINFO)
        if not self.user32.GetMonitorInfoW(mon, self._ctypes.byref(info)):
            return None
        r = info.rcMonitor
        return (r.left, r.top, r.right, r.bottom)

    # -- window control ----------------------------------------------------
    def pin(self, hwnd):
        """Raise to topmost WITHOUT stealing focus (SWP_NOACTIVATE)."""
        ok = bool(self.user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE))
        self.last_error = 0 if ok else self._ctypes.get_last_error()
        return ok

    def click_through(self, hwnd, on, layered=False):
        """Add/remove WS_EX_TRANSPARENT so the mouse passes to the game.

        Deliberately does NOT follow up with SetWindowPos(SWP_FRAMECHANGED).
        Hit-testing reads the ex-style live, so the frame change buys nothing —
        and it would fire WM_WINDOWPOSCHANGED, which Chromium answers by
        dismissing any open `<select>` popup. That exact call, on a 2 s timer,
        is what put a fuse on every dropdown in the app during #40 (§13.8).

        `layered` is an escape hatch, not a default. WS_EX_TRANSPARENT alone is
        enough for hit-testing to skip a window, and the light HUD is layered
        already (tk's `-alpha` sets it). Adding the bit to a Chromium window
        means touching its DirectComposition surface, so heavy mode only does
        it if a flight report says pass-through didn't take."""
        style = int(self._get_long(hwnd, GWL_EXSTYLE))
        want = style
        bits = WS_EX_TRANSPARENT | (WS_EX_LAYERED if layered else 0)
        if on:
            want |= bits
        else:
            # Never clear WS_EX_LAYERED we didn't set — the light HUD's alpha
            # depends on it, and dropping it would paint the window opaque.
            want &= ~WS_EX_TRANSPARENT
        if want == style:
            return False
        self._set_long(hwnd, GWL_EXSTYLE, want)
        return True

    def close(self, hwnd):
        self.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    def windows_of_class(self, class_name):
        """Every visible window of one class as (hwnd, class, title)."""
        ctypes = self._ctypes
        found = []

        def callback(hwnd, _lparam):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd, cls, 256)
            if cls.value != class_name:
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            if length:
                self.user32.GetWindowTextW(hwnd, buf, length + 1)
            found.append((hwnd, cls.value, buf.value))
            return True

        # Hold a reference to the trampoline for the whole call — letting it be
        # collected mid-enumeration crashes the interpreter.
        cb = self._enum_proto(callback)
        self.user32.EnumWindows(cb, 0)
        return found


def load(log=None):
    """A `Win32`, or None off Windows / if ctypes can't bind. Never raises."""
    try:
        import ctypes

        if not hasattr(ctypes, "WinDLL"):
            return None
        return Win32()
    except Exception as exc:                      # pragma: no cover - Windows
        if log:
            log(f"overlay: no Win32 access ({exc})")
        return None


# ---------------------------------------------------------------------------
# Foreground watcher
# ---------------------------------------------------------------------------


class ForegroundWatcher:
    """Answers "is the game in front?" on a tick, cheaply.

    The cost that matters is `QueryFullProcessImageNameW`, so the image path is
    resolved **only when the foreground HWND changes** and cached against it.
    Steady state — which is every tick of a flight, because the game stays in
    front — is one `GetForegroundWindow` and a dict lookup.

    `known` is False until the first successful answer, so callers can fail
    safe rather than guess (see `overlay_interactive`)."""

    def __init__(self, win=None, game_exes=GAME_EXES):
        self._win = win
        self._exes = tuple(game_exes)
        self._hwnd = None
        self._path = ""
        self._cache = {}
        self.known = False
        self.game_foreground = False
        self.changed = False
        self.game_hwnd = None

    def poll(self):
        """Refresh and return True if the foreground window changed.

        Never raises: this rides the repaint tick of a HUD whose whole design
        rule is that it must not be one bad call away from freezing."""
        self.changed = False
        if self._win is None:
            return False
        try:
            hwnd = self._win.foreground_window()
        except Exception:
            return False
        key = int(hwnd) if hwnd else 0
        if key != self._hwnd:
            self._hwnd = key
            self.changed = True
            self._path = self._resolve(key)
            # Bound the cache: a long session cycles through a lot of windows
            # and this must not become a slow leak.
            if len(self._cache) > 64:
                self._cache.clear()
        self.game_foreground = is_game(self._path, self._exes)
        if self.game_foreground:
            self.game_hwnd = key
        self.known = True
        return self.changed

    def _resolve(self, hwnd):
        if not hwnd:
            return ""
        if hwnd in self._cache:
            return self._cache[hwnd]
        try:
            path = self._win.process_path(self._win.window_pid(hwnd))
        except Exception:
            path = ""
        self._cache[hwnd] = path
        return path

    @property
    def foreground_path(self):
        return self._path

    def foreground_is(self, pid):
        """True when the foreground window belongs to process `pid` — how an
        overlay tells "the user is working in ME" from "some other app"."""
        if self._win is None or not self._hwnd:
            return False
        try:
            return int(self._win.window_pid(self._hwnd)) == int(pid)
        except Exception:
            return False
