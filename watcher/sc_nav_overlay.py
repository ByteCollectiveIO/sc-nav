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

tkinter is stdlib but not present on every Python build, so `available()` lets
the watcher degrade to console-only instead of dying. Everything above the
`Overlay` class is pure and testable without a display.
"""

import queue

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

    def __init__(self, config=None, on_close=None, log=print):
        import tkinter as tk

        self._tk = tk
        self._log = log
        self._on_close = on_close
        self._config = config or {}
        self._nav = None
        self._fix_t = None          # monotonic stamp of the last real fix
        self._closing = False

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
        self._hint = tk.Label(
            frame, text="drag to move · Ctrl-C in the console to quit",
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
        """Click-drag to reposition. Click-through (so the HUD can never eat a
        shot) is slice W2; until then the window is interactive, which is
        exactly what makes dragging possible."""
        state = {}

        def press(ev):
            state["x"], state["y"] = ev.x_root, ev.y_root
            state["gx"] = self._root.winfo_x()
            state["gy"] = self._root.winfo_y()

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

        for widget in (self._root, *self._all_children(self._root)):
            widget.bind("<Button-1>", press)
            widget.bind("<B1-Motion>", drag)
            widget.bind("<ButtonRelease-1>", release)

    def _all_children(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self._all_children(child)

    # -- painting ----------------------------------------------------------
    def _age_seconds(self):
        if self._fix_t is None:
            return None
        import time

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
            drained = None
            try:
                while True:
                    drained = updates.get_nowait()
            except queue.Empty:
                pass
            if drained is not None:
                self.update(*drained)
            self._render()          # repaint every tick so the age keeps moving
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
