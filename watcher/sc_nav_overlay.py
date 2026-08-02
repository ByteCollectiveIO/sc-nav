#!/usr/bin/env python3
"""SC Nav Watcher — in-game overlay (backlog #40, slice W1).

A small always-on-top window showing the current target, distance, ETA and how
stale the fix is, so a single-monitor pilot doesn't have to alt-tab to the
browser to read one line.

Design constraints this file deliberately honors (docs/watcher-overlay.md §3):

  * It only draws over the game in BORDERLESS/WINDOWED mode. Exclusive
    fullscreen owns the swap chain and this window will not be composited.
  * Bearing is a great-circle SURFACE bearing and exists only on a body. In
    space it is absent — /showlocation reports position, not attitude, so
    there is no heading to point against. The HUD omits the field rather than
    inventing a compass.
  * The reading is only as fresh as the last /showlocation. The age is a
    first-class element, and turns amber then red as it grows: a two-minute-old
    distance shown as if it were live is worse than no overlay.
  * This is an ordinary sibling window — no injection, no memory reads, no
    synthetic input. Same class of citizen as the Discord overlay.

Slice I1 (#40.1) makes it **inert while you fly**: click-through whenever Star
Citizen is the foreground app, so a cursor that drifts across it cannot eat a
click meant for the game — hold the interact key (F by default, the same key SC
uses to free the cursor) to make it live and drag it. It also hides itself once
you alt-tab away to something that isn't the game, and stops re-asserting
always-on-top on a blind timer now that it can see when focus actually changed.
All of it fails safe: if the foreground watcher can't answer, the window
behaves exactly as it did in W1, because a HUD you cannot click is a worse bug
than the one being fixed.

tkinter is stdlib but not present on every Python build, so `available()` lets
the watcher degrade to console-only instead of dying. Everything above the
`Overlay` class is pure and testable without a display.
"""

import queue
import time

try:
    import sc_nav_win32 as w32
except Exception:                                # pragma: no cover - packaging
    w32 = None

# --- palette (DESIGN.md tokens) --------------------------------------------
BG = "#141a23"          # panel
BORDER = "#243044"
TEXT = "#d8e1ee"
DIM = "#8693a6"
ACCENT = "#4fc3f7"      # instrument cyan
DEST = "#ffb74d"        # caution amber — destination names
STALE = "#ef5350"       # bad

# Age thresholds (seconds) for the "fix" readout: fresh → amber → red.
AGE_WARN_S = 45
AGE_STALE_S = 120

# How long after the game was last in front we keep auto-hiding for another
# app. Without a horizon the HUD would vanish for good the moment you quit SC
# — "the game was running once" is not a state worth remembering forever — and
# with it, quitting the game brings the HUD back on its own.
GAME_RECENT_S = 300

# Compass points for the on-body bearing, 45° per sector starting at N.
#
# Deliberately letters, not arrow glyphs (↗ ↘ …): this HUD renders in Consolas
# on Windows, whose coverage of the diagonal arrows and of symbols like ⟳ is not
# something we can verify from a dev Mac — and a tofu box in the one line a pilot
# is squinting at mid-flight is a bad way to find out. Compass points are ASCII,
# unambiguous, and already the vocabulary anyone reading a bearing expects.
_POINTS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

# Shown where a number does not exist. Plain dashes, not an em dash: same
# font-coverage caution as _POINTS — every glyph this HUD draws stays inside
# Latin-1, which Consolas certainly covers.
NO_VALUE = "--"

# Longest target name we'll show before trimming. Names come from the POI
# catalog and the odd survey mark runs long ("Keeger Belt — survey pocket …").
MAX_NAME = 24


# ---------------------------------------------------------------------------
# Formatting (pure — no tkinter, unit-tested in test_parse.py)
# ---------------------------------------------------------------------------


def format_distance(m):
    """Distance in the same units the SPA uses (m / km / Mm / Gm), so the HUD
    and the browser never disagree about a number the pilot is comparing."""
    if m is None:
        return NO_VALUE
    if m < 1000:
        return f"{m:.0f} m"
    if m < 1e6:
        return f"{m / 1000:.1f} km"
    if m < 1e9:
        return f"{m / 1e6:.2f} Mm"
    return f"{m / 1e9:.3f} Gm"


def format_eta(s):
    """m:ss, or h+mm past an hour. Mirrors the SPA's fmtEta."""
    if s is None:
        return NO_VALUE
    try:
        s = round(float(s))
    except (TypeError, ValueError):
        return NO_VALUE
    if s < 0:
        return NO_VALUE
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}:{sec:02d}"


def format_age(seconds):
    """How old the underlying position fix is. Seconds up to a minute, then
    m:ss — short enough to sit in a corner without wrapping."""
    if seconds is None:
        return NO_VALUE
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m{sec:02d}s"
    return f"{m // 60}h{m % 60:02d}m"


def age_color(seconds):
    if seconds is None or seconds >= AGE_STALE_S:
        return STALE
    if seconds >= AGE_WARN_S:
        return DEST
    return DIM


def compass_point(deg):
    """Nearest compass point for a surface bearing (0 = N, 90 = E), or "" if
    there is no bearing — the normal case in space (§3.2)."""
    if deg is None:
        return ""
    try:
        deg = float(deg) % 360
    except (TypeError, ValueError):
        return ""
    return _POINTS[int((deg + 22.5) % 360 // 45)]


def format_bearing(deg):
    point = compass_point(deg)
    if not point:
        return ""
    return f"{point} {float(deg) % 360:.0f}°"


# Typography that shows up in POI names and our own capture notes ("Keeger
# Belt — survey pocket SVY-14"), mapped to Latin-1 equivalents. Anything else
# out of range degrades to '?' rather than a tofu box.
_SAFE_CHARS = str.maketrans({
    "—": "-", "–": "-", "…": "...", "’": "'", "‘": "'", "“": '"', "”": '"',
    "×": "x", "•": "*",
})


def safe_text(text):
    """Fold a string into Latin-1, which the HUD font certainly covers.

    Target names are not ours: they come from the wiki catalog, from members
    typing custom POI names, and from our own survey notes — all of which carry
    typographic characters Consolas may not have."""
    folded = (text or "").translate(_SAFE_CHARS)
    return folded.encode("latin-1", "replace").decode("latin-1")


def trim_name(name):
    """Keep the HUD one predictable width. Trimmed with plain dots rather than
    an ellipsis character, same font-coverage caution as _POINTS."""
    name = safe_text(name or "target").upper()
    if len(name) <= MAX_NAME:
        return name
    return name[:MAX_NAME - 3].rstrip() + "..."


def hud_lines(nav, age_s):
    """Render a nav summary (the `nav` object from POST /api/position) into the
    three HUD rows: (target, readout, age).

    Kept pure so every state the pilot can land in — no fix yet, no target,
    target on a body, target in space — is testable without a display."""
    if nav is None:
        return ("waiting for /showlocation", "", "")
    dest = nav.get("destination")
    if not dest:
        where = nav.get("container") or nav.get("system") or ""
        return ("no target set", where, format_age(age_s))
    name = trim_name(dest.get("name"))
    # Surface distance is the honest one when both feet are on the same body;
    # 3D distance through the planet is not the trip you're about to fly.
    # Explicit None check: standing on it (0.0) must not fall back to 3D.
    guide = dest.get("surface_distance_m")
    if guide is None:
        guide = dest.get("distance_m")
    parts = [format_distance(guide)]
    bearing = format_bearing(dest.get("bearing_deg"))
    if bearing:
        parts.append(bearing)
    eta = dest.get("eta_s")
    if eta is not None:
        parts.append(f"ETA {format_eta(eta)}")
    return (name, "   ".join(parts), format_age(age_s))


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def available():
    """True when this Python can actually build a window. Some Microsoft Store
    and trimmed installs ship without tcl/tk, and losing position reporting to a
    missing overlay dependency would be a bad trade."""
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


class Overlay:
    """The HUD window. Owns the main thread (tk requires it); the watcher's
    clipboard loop runs on a daemon thread and hands updates over a Queue.

    Nothing here may be called from the worker thread — tk is not thread-safe.
    The queue is drained by a periodic `after` tick, which also gives the
    interpreter a chance to run signal handlers so Ctrl-C still quits (inside a
    bare mainloop on Windows it otherwise never lands)."""

    TICK_MS = 250       # queue drain + age repaint + Ctrl-C window
    WIDTH = 300         # fixed: see _freeze_size
    TOPMOST_EVERY = 8   # ticks between re-asserts (~2 s): see _keep_on_top

    def __init__(self, config=None, on_close=None, log=print):
        import tkinter as tk

        self._tk = tk
        self._log = log
        self._on_close = on_close
        self._config = config or {}
        self._nav = None
        self._fix_t = None          # monotonic stamp of the last real fix
        self._closing = False
        self._ticks = 0
        self._tick_errors = 0
        # Win32, or None everywhere but Windows. Shared with heavy mode so
        # there is exactly one argtypes-correct binding — this used to be a
        # bare WinDLL with no argtypes at all, which is the documented way to
        # get a silently truncated HWND and a re-assert that does nothing.
        self._win = w32.load(log) if w32 is not None else None
        self._hwnd = None
        self._fg = w32.ForegroundWatcher(
            self._win, self._game_exes()) if w32 is not None else None
        self._pid = None
        self._vk = w32.vk_code(
            self._config.get("overlay_interact_key")) if w32 is not None else 0
        self._dragging = False
        self._inert = None          # None = never applied
        self._hidden = False
        self._game_seen_t = None
        self._fullscreen_warned = False

        root = tk.Tk()
        root.title("SC Nav")
        root.overrideredirect(True)             # no title bar / chrome
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", float(self._config.get("overlay_alpha", 0.82)))
        except (TypeError, ValueError, tk.TclError):
            pass
        root.configure(bg=BORDER)               # 1px hairline via padding
        self._root = root

        frame = tk.Frame(root, bg=BG, padx=10, pady=6)
        frame.pack(padx=1, pady=1)
        self._frame = frame

        top = tk.Frame(frame, bg=BG)
        top.pack(fill="x")
        self._target = tk.Label(
            top, text="", bg=BG, fg=DEST, font=("Consolas", 11, "bold"), anchor="w"
        )
        self._target.pack(side="left")
        self._age = tk.Label(
            top, text="", bg=BG, fg=DIM, font=("Consolas", 9), anchor="e", padx=8
        )
        self._age.pack(side="right")

        self._readout = tk.Label(
            frame, text="", bg=BG, fg=ACCENT, font=("Consolas", 13), anchor="w"
        )
        self._readout.pack(fill="x")
        # In game the cursor is captive until you hold F — the same key that
        # frees it for in-game menus — so the drag reads as a normal SC
        # interaction rather than a thing this tool invented. As of I1 the key
        # is also literally what makes the window clickable, so this line is
        # now the instruction rather than the convention. Non-obvious enough
        # that it belongs on the glass, not just in the README.
        # (Latin-1 only, per _POINTS: the middle dot is U+00B7, Consolas-safe.)
        key = safe_text(str(self._config.get("overlay_interact_key")
                            or "F")).upper()[:1] or "F"
        self._hint = tk.Label(
            frame, text=f"hold {key} to drag · Ctrl-C in console to quit",
            bg=BG, fg=DIM, font=("Consolas", 8), anchor="w",
        )
        self._hint.pack(fill="x")

        self._render()
        self._freeze_size()
        self._place()
        self._bind_drag()

    # -- geometry ----------------------------------------------------------
    def _freeze_size(self):
        """Pin the window to a fixed size.

        Left to itself the window resizes to fit its text, so it twitched wider
        and narrower on every /showlocation as the target name and distance
        changed length — a HUD that changes shape in peripheral vision reads as
        something happening. Fixed width + `trim_name` keeps it still; the height
        is measured once from the real laid-out content rather than guessed."""
        self._root.update_idletasks()
        height = max(self._root.winfo_reqheight(), 1)
        self._size = (self.WIDTH, height)
        self._frame.configure(width=self.WIDTH, height=height)
        self._frame.pack_propagate(False)

    def _geometry(self, x, y):
        w, h = getattr(self, "_size", (self.WIDTH, 0))
        return f"{w}x{h}+{x}+{y}" if h else f"+{x}+{y}"

    # -- placement ---------------------------------------------------------
    def _place(self):
        x = self._config.get("overlay_x")
        y = self._config.get("overlay_y")
        try:
            x, y = int(x), int(y)
        except (TypeError, ValueError):
            x, y = 40, 40
        # Clamp back on-screen: a saved position from a monitor that is no
        # longer attached would otherwise strand the HUD off in the void.
        w, h = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
        x = max(0, min(x, max(0, w - 120)))
        y = max(0, min(y, max(0, h - 60)))
        self._root.geometry(self._geometry(x, y))

    def _bind_drag(self):
        """Click-drag to reposition, while the interact key is held.

        The window is click-through whenever the game is in front (I1), so a
        press can only reach us if you asked for it — which is what the on-glass
        hint has always said. `_dragging` pins the window interactive for the
        length of the gesture: flipping click-through back on mid-drag, because
        you happened to let go of F, would drop the window on the floor
        somewhere between where it was and where you wanted it."""
        state = {}

        def press(ev):
            state["x"], state["y"] = ev.x_root, ev.y_root
            state["gx"] = self._root.winfo_x()
            state["gy"] = self._root.winfo_y()
            self._dragging = True

        def drag(ev):
            if "x" not in state:
                return
            nx = state["gx"] + (ev.x_root - state["x"])
            ny = state["gy"] + (ev.y_root - state["y"])
            self._root.geometry(self._geometry(nx, ny))

        def release(_ev):
            self._config["overlay_x"] = self._root.winfo_x()
            self._config["overlay_y"] = self._root.winfo_y()
            state.clear()
            self._dragging = False

        for widget in (self._root, *self._all_children(self._root)):
            widget.bind("<Button-1>", press)
            widget.bind("<B1-Motion>", drag)
            widget.bind("<ButtonRelease-1>", release)

    def _all_children(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self._all_children(child)

    # -- staying visible ---------------------------------------------------
    def _game_exes(self):
        name = self._config.get("game_exe")
        return (str(name).lower(),) if name else w32.GAME_EXES

    def _window(self):
        """Our top-level HWND, resolved once.

        tk's `winfo_id` is the frame; on Windows the top-level that carries the
        ex-style bits is its parent. Resolved lazily because it only exists once
        the window has been mapped."""
        if self._hwnd or self._win is None:
            return self._hwnd
        try:
            child = self._root.winfo_id()
            self._hwnd = self._win.user32.GetParent(child) or child
            self._pid = self._win.own_pid()
        except Exception:
            self._hwnd = None
        return self._hwnd

    def _keep_on_top(self, force=False):
        """Re-assert always-on-top when it has actually been lost.

        Setting `-topmost` once at construction is NOT enough: Windows drops or
        supersedes topmost in ordinary situations — another topmost window
        appears, the game toggles fullscreen, a UAC prompt hits the secure
        desktop, the display mode changes. And because this is an
        `overrideredirect` popup it has **no taskbar button and no alt-tab
        entry**, so once it slips behind the game there is no handle left to
        click and the only recovery is restarting the watcher. Reported from
        real flight, hence this.

        W1 re-asserted on a blind ~2 s timer. Now that the foreground watcher
        can say *when* focus changed, the call happens on the events that
        actually drop the flag plus a cheap ex-style check — the same lesson
        heavy mode learned expensively (a periodic SetWindowPos is not free;
        it dismisses open menus).

        SetWindowPos with SWP_NOACTIVATE is the Windows-correct way — it raises
        the window WITHOUT stealing focus from the game, which the Tk attribute
        route can't promise. Everything else falls back to re-setting the
        attribute, which is a harmless no-op when it's already true."""
        try:
            hwnd = self._window()
            if hwnd:
                if force or not self._win.is_topmost(hwnd):
                    self._win.pin(hwnd)
            else:
                self._root.attributes("-topmost", True)
        except Exception:
            # Never let a cosmetic re-assert break the update loop.
            pass

    # -- being inert (#40.1 I1) -------------------------------------------
    def _interact(self):
        """One tick of the interaction layer: who's in front, and what that
        means for click-through, visibility and the topmost flag."""
        if self._fg is None or self._win is None:
            # No Win32 (not Windows, or ctypes couldn't bind): nothing here is
            # available, so fall back to W1's blind periodic re-assert via the
            # Tk attribute. Everything else in I1 is simply absent.
            if self._ticks % self.TOPMOST_EVERY == 0:
                self._keep_on_top()
            return
        # Resolve our own window first, unconditionally: `_apply_autohide`
        # needs the pid to tell "the pilot is using the HUD" from "some other
        # app has focus", and turning click-through off must not cost it that.
        self._window()
        changed = self._fg.poll()
        if self._fg.game_foreground:
            self._game_seen_t = time.monotonic()
            self._warn_fullscreen()
        self._apply_click_through()
        self._apply_autohide()
        if changed:
            # A foreground change is the moment topmost is most likely to have
            # been superseded, and the only moment worth spending a call on.
            self._keep_on_top(force=True)
        elif self._ticks % self.TOPMOST_EVERY == 0:
            self._keep_on_top()

    def _apply_click_through(self):
        """Inert while the game is in front, unless you're asking for it."""
        if not self._config.get("overlay_clickthrough", True):
            return
        hwnd = self._window()
        if not hwnd:
            return
        key_held = False
        if self._fg.game_foreground and self._vk:
            try:
                key_held = self._win.key_down(self._vk)
            except Exception:
                key_held = False
        inert = not w32.overlay_interactive(
            self._fg.known, self._fg.game_foreground,
            key_held=key_held, busy=self._dragging)
        if inert == self._inert:
            return
        # `layered` is never passed: tk's `-alpha` already made this window
        # layered, and asking for the bit again would be a no-op at best.
        self._win.click_through(hwnd, inert)
        self._inert = inert

    def _apply_autohide(self):
        """Get out of the way of everything that isn't the game.

        The HUD is topmost and has no taskbar button, so left alone it sits on
        top of Discord, the browser and everything else for the whole session.
        It hides only once the game has actually been in front (so it is
        visible and draggable before you ever launch SC, and while you're
        setting it up) and comes back on its own if the game exits."""
        if not self._config.get("overlay_autohide", True):
            return
        if self._game_seen_t is None:
            return
        recent = (time.monotonic() - self._game_seen_t) < GAME_RECENT_S
        ours = self._pid is not None and self._fg.foreground_is(self._pid)
        want_hidden = recent and not (self._fg.game_foreground or ours)
        if want_hidden == self._hidden:
            return
        self._hidden = want_hidden
        if want_hidden:
            self._root.withdraw()
        else:
            self._root.deiconify()
            # Coming back from withdrawn drops the flag and can re-order us
            # behind the game; re-assert without taking focus.
            self._keep_on_top(force=True)

    def _warn_fullscreen(self):
        """Say the one thing that explains an invisible HUD, once.

        Borderless and exclusive fullscreen produce the same window rect, so
        this cannot detect the mode — which is why it is phrased as a
        conditional and logged exactly once per run. It is still the answer to
        the only "I see nothing" question this design can produce."""
        if self._fullscreen_warned or not self._fg.game_hwnd:
            return
        self._fullscreen_warned = True
        try:
            rect = self._win.window_rect(self._fg.game_hwnd)
            monitor = self._win.monitor_rect(self._fg.game_hwnd)
        except Exception:
            return
        if w32.covers_monitor(rect, monitor):
            self._log("overlay: Star Citizen is covering the whole screen. If "
                      "you can't see the HUD, it's running in exclusive "
                      "Fullscreen — switch Graphics > Display Mode to "
                      "Borderless and it will appear.")

    # -- painting ----------------------------------------------------------
    def _age_seconds(self):
        if self._fix_t is None:
            return None
        return time.monotonic() - self._fix_t

    def _render(self):
        age = self._age_seconds()
        target, readout, age_text = hud_lines(self._nav, age)
        self._target.config(text=target)
        self._readout.config(text=readout)
        self._age.config(text=(f"fix {age_text}" if age_text else ""), fg=age_color(age))

    def update(self, nav, fix_t):
        self._nav, self._fix_t = nav, fix_t

    # -- lifecycle ---------------------------------------------------------
    def run(self, updates, stop=None):
        """Main loop. `updates` is a Queue of (nav, fix_monotonic) tuples from
        the watcher thread; `stop` is the shared shutdown Event."""
        def tick():
            if self._closing:
                return
            if stop is not None and stop.is_set():
                # The watcher thread died or was asked to stop — don't leave a
                # frozen window on screen showing a distance that will never
                # update again.
                self.close()
                return
            # EVERYTHING below is guarded and the reschedule lives in `finally`.
            # Tk drops the `after` chain on an unhandled exception, which froze
            # the HUD permanently — still on screen, still showing a distance,
            # never updating again — while the watcher carried on reporting
            # position. A frozen overlay lies; it must never be one bad repaint
            # away.
            try:
                drained = None
                try:
                    while True:
                        drained = updates.get_nowait()
                except queue.Empty:
                    pass
                if drained is not None:
                    self.update(*drained)
                self._render()      # repaint every tick so the age keeps moving
                self._ticks += 1
                try:
                    self._interact()   # foreground -> click-through/hide/pin
                except Exception as exc:
                    # Its own guard: a Win32 hiccup must not read as a failed
                    # repaint, and must never cost the pilot the readout.
                    if self._ticks % 240 == 0:
                        self._log(f"overlay: window state check failed ({exc})")
                self._tick_errors = 0
            except Exception as exc:
                self._tick_errors += 1
                # Log the first, then every ~30 s, so a persistent fault is
                # visible in the console without flooding it.
                if self._tick_errors == 1 or self._tick_errors % 120 == 0:
                    self._log(f"overlay repaint failed ({exc}); still running")
            finally:
                if not self._closing:
                    self._root.after(self.TICK_MS, tick)

        self._root.after(self.TICK_MS, tick)
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self._on_close is not None:
            try:
                self._on_close(self._config)
            except Exception:
                pass
        try:
            self._root.destroy()
        except Exception:
            pass


def start(updates, config=None, on_close=None, log=print, stop=None):
    """Build and run the overlay, returning False if it couldn't be created.

    Never raises: a broken overlay must not take the watcher's actual job —
    reporting position — down with it."""
    try:
        overlay = Overlay(config=config, on_close=on_close, log=log)
    except Exception as exc:
        log(f"overlay could not start ({exc}) — continuing without it")
        return False
    overlay.run(updates, stop=stop)
    return True
