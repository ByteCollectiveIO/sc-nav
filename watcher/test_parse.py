"""Parser tests for sc_nav_watcher. Run: python3 test_parse.py"""

import json
import os
import queue
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import sc_nav_heavy
import sc_nav_watcher
from sc_nav_overlay import (
    age_color, compass_point, format_age, format_bearing, format_distance,
    format_eta, hud_lines, trim_name, MAX_NAME, STALE,
)
from sc_nav_watcher import GameLogShardReader, heartbeat_due, parse_showlocation

_JOIN = ("<2026-06-20T00:30:29.237Z> [Notice] <Join PU> address[34.21.5.134] "
         "port[64317] shard[pub_use1b_12030094_130] locationId[562954248454145] "
         "[Team_GameServices][GIM][Matchmaking]\n")
_UPDATE = ("<2026-06-20T00:30:29.514Z> [Notice] <Update Shard Id> New Shard Id: "
           "{shard}. Old Shard Id [Team_OnlineTech][Telemetry][Services]\n")


class ParseShowLocationTests(unittest.TestCase):
    def test_typical_output(self):
        text = "Coordinates: x:-18930539540.392 y:-2610158765.392 z:0.0"
        self.assertEqual(
            parse_showlocation(text),
            {"x": -18930539540.392, "y": -2610158765.392, "z": 0.0},
        )

    def test_singular_label_and_commas(self):
        text = "Coordinate: x:12,850,457,093.5, y:0.0, z:-42.25"
        self.assertEqual(
            parse_showlocation(text),
            {"x": 12850457093.5, "y": 0.0, "z": -42.25},
        )

    def test_equals_separator_and_spacing(self):
        text = "pos x = 1.5  y = -2  z = 3"
        self.assertEqual(parse_showlocation(text), {"x": 1.5, "y": -2.0, "z": 3.0})

    def test_multiline_and_surrounding_text(self):
        text = "You are here:\nx: 100\ny: 200\nz: 300\nCopied to clipboard."
        self.assertEqual(parse_showlocation(text), {"x": 100.0, "y": 200.0, "z": 300.0})

    def test_integer_values(self):
        text = "x:22462085252 y:37185744964 z:0"
        self.assertEqual(
            parse_showlocation(text),
            {"x": 22462085252.0, "y": 37185744964.0, "z": 0.0},
        )

    def test_rejects_missing_axis(self):
        self.assertIsNone(parse_showlocation("x:1 y:2"))

    def test_rejects_ordinary_text(self):
        self.assertIsNone(parse_showlocation("meet me at port olisar"))
        self.assertIsNone(parse_showlocation(""))
        self.assertIsNone(parse_showlocation(None))

    def test_rejects_huge_clipboard(self):
        self.assertIsNone(parse_showlocation("x:1 y:2 z:3" + "a" * 5000))

    def test_word_boundary_does_not_match_inside_words(self):
        # 'max: 5' must not be read as axis x
        self.assertIsNone(parse_showlocation("max: 5 stay: 2 fuzz: 9"))


class GameLogShardReaderTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix="Game.log")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        self.reader = GameLogShardReader(self.path)

    def _write(self, text, mode="a"):
        with open(self.path, mode, encoding="utf-8") as fh:
            fh.write(text)

    def test_join_pu_line(self):
        self._write(_JOIN)
        self.assertEqual(self.reader.poll(), "pub_use1b_12030094_130")

    def test_update_shard_id_line(self):
        self._write(_UPDATE.format(shard="pub_use1c_99999999_42"))
        self.assertEqual(self.reader.poll(), "pub_use1c_99999999_42")

    def test_latest_shard_wins(self):
        self._write(_JOIN)
        self._write(_UPDATE.format(shard="pub_eu1a_12030094_7"))
        self.assertEqual(self.reader.poll(), "pub_eu1a_12030094_7")

    def test_only_reads_appended_bytes(self):
        self._write(_UPDATE.format(shard="pub_use1b_12030094_130"))
        self.assertEqual(self.reader.poll(), "pub_use1b_12030094_130")
        # Nothing new appended -> shard unchanged, still reported.
        self.assertEqual(self.reader.poll(), "pub_use1b_12030094_130")
        # A shard change later in the same file is picked up incrementally.
        self._write(_UPDATE.format(shard="pub_use1b_12030094_131"))
        self.assertEqual(self.reader.poll(), "pub_use1b_12030094_131")

    def test_truncation_reseeks(self):
        # A few lines so the read offset is well past a freshly truncated file.
        self._write(_JOIN + _UPDATE.format(shard="pub_use1b_12030094_130"))
        self.assertEqual(self.reader.poll(), "pub_use1b_12030094_130")
        # Game relaunch truncates the log; the shorter file (size < old offset)
        # is detected as a rotation and re-read from the start.
        self._write(_UPDATE.format(shard="pub_use1b_12030094_555"), mode="w")
        self.assertEqual(self.reader.poll(), "pub_use1b_12030094_555")

    def test_no_shard_lines(self):
        self._write("<2026-06-20T00:30:29.000Z> [Notice] just some other log line\n")
        self.assertIsNone(self.reader.poll())

    def test_missing_file(self):
        self.assertIsNone(GameLogShardReader("/no/such/Game.log").poll())


class HeartbeatDueTests(unittest.TestCase):
    def test_not_due_before_interval(self):
        # 30s elapsed, 60s interval, shard unchanged -> nothing to send.
        self.assertEqual(heartbeat_due(30.0, 0.0, 60.0, "s1", "s1"), "")

    def test_due_after_interval(self):
        self.assertEqual(heartbeat_due(60.0, 0.0, 60.0, "s1", "s1"), "interval")
        self.assertEqual(heartbeat_due(75.0, 0.0, 60.0, "s1", "s1"), "interval")

    def test_shard_change_sends_immediately(self):
        # Only 1s elapsed, but the shard changed -> send now, don't wait.
        self.assertEqual(heartbeat_due(1.0, 0.0, 60.0, "s2", "s1"), "shard")

    def test_shard_change_wins_over_interval(self):
        self.assertEqual(heartbeat_due(999.0, 0.0, 60.0, "s2", "s1"), "shard")

    def test_zero_interval_disables_timed_but_not_shard(self):
        self.assertEqual(heartbeat_due(999.0, 0.0, 0.0, "s1", "s1"), "")
        self.assertEqual(heartbeat_due(1.0, 0.0, 0.0, "s2", "s1"), "shard")

    def test_no_shard_both_none_is_not_a_change(self):
        # Shard tagging off (no Game.log): None == None, so no spurious sends.
        self.assertEqual(heartbeat_due(10.0, 0.0, 60.0, None, None), "")
        self.assertEqual(heartbeat_due(60.0, 0.0, 60.0, None, None), "interval")


class OverlayFormatTests(unittest.TestCase):
    """The overlay's pure layer (#40 §8) — no tkinter, no display needed."""

    def test_distance_units_match_the_spa(self):
        self.assertEqual(format_distance(None), "--")
        self.assertEqual(format_distance(412.0), "412 m")
        self.assertEqual(format_distance(41234.0), "41.2 km")
        self.assertEqual(format_distance(8.2e6), "8.20 Mm")
        self.assertEqual(format_distance(1.85e10), "18.500 Gm")

    def test_eta(self):
        self.assertEqual(format_eta(None), "--")
        self.assertEqual(format_eta(96), "1:36")
        self.assertEqual(format_eta(9), "0:09")
        self.assertEqual(format_eta(3720), "1h02m")
        self.assertEqual(format_eta("nonsense"), "--")

    def test_age(self):
        self.assertEqual(format_age(0), "0s")
        self.assertEqual(format_age(44.6), "44s")
        self.assertEqual(format_age(75), "1m15s")
        self.assertEqual(format_age(3700), "1h01m")

    def test_age_color_escalates_and_unknown_reads_as_stale(self):
        self.assertNotEqual(age_color(5), STALE)
        self.assertNotEqual(age_color(50), age_color(5))     # amber warn band
        self.assertEqual(age_color(300), STALE)
        # No fix at all must never render as "fresh" (#40 §3.3).
        self.assertEqual(age_color(None), STALE)

    def test_compass_point_sectors(self):
        self.assertEqual(compass_point(0), "N")
        self.assertEqual(compass_point(90), "E")
        self.assertEqual(compass_point(181), "S")
        self.assertEqual(compass_point(359), "N")           # wraps to north
        self.assertEqual(compass_point(None), "")           # in space: nothing
        self.assertEqual(format_bearing(118.4), "SE 118°")

    def test_every_glyph_we_emit_stays_inside_latin1(self):
        # Consolas coverage of arrows and symbols (↘ ⟳ — …) isn't verifiable
        # from the dev Mac, and a tofu box in the pilot's one readable line is a
        # bad way to find out. Latin-1 (which includes °) is certainly covered.
        emitted = [format_distance(None), format_eta(None), format_age(None)]
        emitted += [format_bearing(d) for d in range(0, 360, 7)]
        emitted += [format_distance(4.2e8), format_age(3700), format_eta(96)]
        for row in hud_lines({"destination": {
                "name": "Keeger Belt — survey pocket SVY-14",
                "distance_m": None, "surface_distance_m": None,
                "bearing_deg": None, "eta_s": None}}, None):
            emitted.append(row)
        for text in emitted:
            self.assertTrue(all(ord(c) < 0x100 for c in text), repr(text))

    def test_trim_name_keeps_one_width(self):
        self.assertEqual(trim_name("Lorville"), "LORVILLE")
        long = trim_name("Keeger Belt - survey pocket SVY-14")
        self.assertLessEqual(len(long), MAX_NAME)
        self.assertTrue(long.endswith("..."))
        self.assertNotIn(" ...", long)          # no dangling space before the dots
        self.assertEqual(trim_name(None), "TARGET")

    def test_hud_without_a_fix(self):
        self.assertEqual(hud_lines(None, None)[0], "waiting for /showlocation")

    def test_hud_without_a_target(self):
        target, where, age = hud_lines(
            {"system": "Stanton", "container": "Hurston", "destination": None}, 12)
        self.assertEqual(target, "no target set")
        self.assertEqual(where, "Hurston")
        self.assertEqual(age, "12s")

    def test_hud_on_a_body_prefers_surface_distance_and_shows_bearing(self):
        target, readout, _ = hud_lines({"destination": {
            "name": "Lorville", "distance_m": 812345.0,
            "surface_distance_m": 41234.0, "bearing_deg": 118.4,
            "eta_s": 96.0, "same_container": True}}, 4)
        self.assertEqual(target, "LORVILLE")
        # Surface distance, not the 3D line through the planet.
        self.assertIn("41.2 km", readout)
        self.assertNotIn("812", readout)
        self.assertIn("SE 118°", readout)
        self.assertIn("ETA 1:36", readout)

    def test_hud_in_space_omits_bearing_entirely(self):
        _, readout, _ = hud_lines({"destination": {
            "name": "Aaron Halo band 4", "distance_m": 1.85e10,
            "surface_distance_m": None, "bearing_deg": None,
            "eta_s": None, "same_container": False}}, 8)
        self.assertEqual(readout, "18.500 Gm")   # no fake compass, no "—" ETA

    def test_hud_standing_on_the_target_does_not_fall_back_to_3d(self):
        # Explicit-None guard: surface distance 0.0 is a real answer.
        _, readout, _ = hud_lines({"destination": {
            "name": "Pad", "distance_m": 900.0, "surface_distance_m": 0.0,
            "bearing_deg": None, "eta_s": None}}, 1)
        self.assertEqual(readout, "0 m")


class _FakeWidget:
    """Just enough tkinter to build the Overlay without a display."""

    def __init__(self, master=None, **kw):
        self.kw = dict(kw)
        self._kids = []
        self.binds = {}
        if isinstance(master, _FakeWidget):
            master._kids.append(self)

    def pack(self, **kw): pass
    def pack_propagate(self, _v): pass
    def configure(self, **kw): self.kw.update(kw)
    config = configure
    def bind(self, seq, fn): self.binds[seq] = fn
    def winfo_children(self): return list(self._kids)
    def winfo_x(self): return 40
    def winfo_y(self): return 40
    def winfo_id(self): return 1
    def winfo_screenwidth(self): return 1920
    def winfo_screenheight(self): return 1080
    def winfo_reqheight(self): return 85
    def update_idletasks(self): pass


class _FakeTkRoot(_FakeWidget):
    def __init__(self, **kw):
        super().__init__(None, **kw)
        self.pending = []
        self.attrs = {}
        self.destroyed = False

    tick_budget = 0     # how many after-callbacks mainloop() will pump

    def title(self, _t): pass
    def overrideredirect(self, _v): pass
    def attributes(self, name, value=None): self.attrs[name] = value
    def geometry(self, _g): pass
    def after(self, _ms, fn): self.pending.append(fn)
    def destroy(self): self.destroyed = True

    def mainloop(self):
        """Pump the after-chain like the real loop would. Bounded, because a
        self-rescheduling tick would otherwise spin forever."""
        for _ in range(self.tick_budget):
            if not self.pending or self.destroyed:
                break
            self.pending.pop(0)()


def _fake_tkinter():
    mod = types.ModuleType("tkinter")
    mod.Tk, mod.Frame, mod.Label = _FakeTkRoot, _FakeWidget, _FakeWidget
    mod.TclError = Exception
    return mod


class OverlayLoopResilienceTests(unittest.TestCase):
    """Field report 2026-07-25: the overlay sometimes froze or vanished and only
    a watcher restart brought it back. Two distinct causes, both guarded here."""

    def _overlay(self):
        import sc_nav_overlay
        with mock.patch.dict(sys.modules, {"tkinter": _fake_tkinter()}):
            return sc_nav_overlay.Overlay(config={})

    def test_a_raising_repaint_does_not_stop_the_loop(self):
        # Tk drops the `after` chain on an unhandled exception, which left the
        # HUD on screen showing a distance that would never update again — the
        # worst possible state, because a frozen overlay lies.
        ov = self._overlay()
        ov._root.tick_budget = 5
        logged = []
        ov._log = logged.append
        attempts = []

        def bad_paint():
            attempts.append(1)
            raise RuntimeError("bad paint")
        ov._render = bad_paint

        ov.run(queue.Queue(), stop=threading.Event())

        # Every scheduled tick ran despite each one raising — that IS the fix.
        self.assertEqual(len(attempts), 5)
        self.assertTrue(logged, "a persistent repaint fault must be visible")
        self.assertLess(len(logged), 5, "but it must not flood the console")

    def test_topmost_is_re_asserted_periodically(self):
        # Setting -topmost once isn't enough: Windows supersedes it, and an
        # overrideredirect popup has no taskbar button to click it back with.
        def calls_over(ticks):
            ov = self._overlay()
            seen = []
            ov._keep_on_top = lambda: seen.append(1)
            ov._root.tick_budget = ticks
            ov.run(queue.Queue(), stop=threading.Event())
            return len(seen)

        self.assertEqual(calls_over(self._every() - 1), 0,
                         "shouldn't re-assert on every tick")
        self.assertEqual(calls_over(self._every()), 1)
        self.assertEqual(calls_over(self._every() * 2 + 1), 2,
                         "re-asserts once per TOPMOST_EVERY ticks")

    def _every(self):
        import sc_nav_overlay
        return sc_nav_overlay.Overlay.TOPMOST_EVERY

    def test_worker_death_closes_the_window(self):
        ov = self._overlay()
        ov._root.tick_budget = 1
        stop = threading.Event()
        stop.set()                              # watcher thread already gone
        ov.run(queue.Queue(), stop=stop)
        self.assertTrue(ov._root.destroyed)


class HeavyOverlayTests(unittest.TestCase):
    """Heavy mode's pure layer (#40 §13). The Win32 half can't be tested off
    Windows — that's exactly why the mode ships labelled beta."""

    def test_browser_search_prefers_edge(self):
        # Edge is guaranteed on Win10/11; Chrome may not be installed at all.
        env = {"ProgramFiles": r"C:\PF", "ProgramFiles(x86)": r"C:\PF86"}
        both = {r"C:\PF86\Microsoft\Edge\Application\msedge.exe",
                r"C:\PF\Google\Chrome\Application\chrome.exe"}
        path, name = sc_nav_heavy.find_browser(
            env=env, exists=both.__contains__, which=lambda _c: None)
        self.assertEqual(name, "Edge")

    def test_falls_back_to_chrome_then_path_then_nothing(self):
        env = {"ProgramFiles": r"C:\PF", "ProgramFiles(x86)": r"C:\PF86"}
        chrome_only = {r"C:\PF\Google\Chrome\Application\chrome.exe"}
        _p, name = sc_nav_heavy.find_browser(
            env=env, exists=chrome_only.__contains__, which=lambda _c: None)
        self.assertEqual(name, "Chrome")

        _p, name = sc_nav_heavy.find_browser(
            env=env, exists=lambda _p: False,
            which=lambda c: "/usr/bin/msedge" if c == "msedge" else None)
        self.assertEqual(name, "Edge")

        path, name = sc_nav_heavy.find_browser(
            env=env, exists=lambda _p: False, which=lambda _c: None)
        self.assertIsNone(path)

    def test_missing_env_var_does_not_yield_a_literal_path(self):
        # A box with no %ProgramFiles(x86)% must skip that candidate, not hand
        # back a path with the unexpanded variable still in it.
        seen = []

        def exists(p):
            seen.append(p)
            return False
        sc_nav_heavy.find_browser(env={"ProgramFiles": r"C:\PF"}, exists=exists,
                                  which=lambda _c: None)
        self.assertTrue(seen, "should still probe the vars it does have")
        for p in seen:
            self.assertNotIn("%", p)

    def test_launch_command_uses_the_default_profile(self):
        cmd = sc_nav_heavy.browser_command("msedge.exe", "https://nav/#/",
                                           (10, 20, 800, 600))
        self.assertIn("--app=https://nav/#/", cmd)
        self.assertIn("--window-size=800,600", cmd)
        self.assertIn("--window-position=10,20", cmd)
        # A private profile would strand the user at an OAuth prompt inside a
        # window with no address bar — the default profile carries the cookie.
        self.assertFalse([a for a in cmd if "user-data-dir" in a])

    def test_window_pick_prefers_a_new_matching_title(self):
        before = {1}
        windows = [(1, "Chrome_WidgetWin_1", "Org Navigator"),      # pre-existing
                   (2, "Chrome_WidgetWin_1", "Inbox"),
                   (3, "Chrome_WidgetWin_1", "Org Navigator")]      # ours
        self.assertEqual(sc_nav_heavy.pick_window(before, windows, "Org Navigator"), 3)

    def test_window_pick_never_adopts_a_pre_existing_window(self):
        # The user's ordinary browser window welded on top of their game would
        # be the worst possible bug here.
        before = {1}
        windows = [(1, "Chrome_WidgetWin_1", "Org Navigator")]
        self.assertIsNone(sc_nav_heavy.pick_window(before, windows, "Org Navigator"))

    def test_window_pick_falls_back_when_the_title_is_the_oauth_page(self):
        # Not signed in: --app lands on Discord's OAuth page, so the title is
        # "Discord". A strict title gate refused to adopt it and silently never
        # pinned anything — the reported failure.
        before = set()
        windows = [(7, "Chrome_WidgetWin_1", "Discord")]
        self.assertEqual(sc_nav_heavy.pick_window(before, windows, "Org Navigator"), 7)

    def test_window_pick_handles_a_blank_title_while_loading(self):
        before = set()
        windows = [(9, "Chrome_WidgetWin_1", "")]
        self.assertEqual(sc_nav_heavy.pick_window(before, windows, "Org Navigator"), 9)

    def test_repin_only_when_topmost_was_actually_lost(self):
        # Reported: every dropdown in the app snapped shut ~instantly. Chromium
        # dismisses an open <select> popup when its parent gets
        # WM_WINDOWPOSCHANGED, which SetWindowPos fires even with NOMOVE|NOSIZE
        # — so a blind 2 s re-assert put a fuse on every menu.
        self.assertFalse(sc_nav_heavy.should_repin(is_topmost=True,
                                                   is_foreground=False))
        self.assertFalse(sc_nav_heavy.should_repin(is_topmost=True,
                                                   is_foreground=True))
        # Never interrupt the window the user is actively working in.
        self.assertFalse(sc_nav_heavy.should_repin(is_topmost=False,
                                                   is_foreground=True))
        # Genuinely lost it, and not in use -> put it back.
        self.assertTrue(sc_nav_heavy.should_repin(is_topmost=False,
                                                  is_foreground=False))

    def test_pin_is_not_re_sent_while_the_flag_still_holds(self):
        class _FakeWin:
            def __init__(self):
                self.windows = [(5, "Chrome_WidgetWin_1", "Org Navigator")]
                self.pinned, self.topmost, self.foreground = [], True, False

            def browser_windows(self):
                return list(self.windows)

            def pin(self, hwnd):
                self.pinned.append(hwnd)
                self.topmost = True
                return True

            def alive(self, hwnd):
                return any(w[0] == hwnd for w in self.windows)

            def is_topmost(self, _h):
                return self.topmost

            def is_foreground(self, _h):
                return self.foreground

        ov = sc_nav_heavy.HeavyOverlay("http://x/", log=lambda *_a: None)
        win = _FakeWin()
        ov._win, ov._before, ov._title_match = win, set(), "Org Navigator"
        ov.keep_pinned()                      # adopts + pins once
        self.assertEqual(win.pinned, [5])

        for _ in range(20):                   # ~40 s of ticks, flag intact
            ov.keep_pinned()
        self.assertEqual(win.pinned, [5], "must not touch a window that is "
                                          "already topmost")

        win.topmost = False                   # something stole topmost...
        win.foreground = True                 # ...but the user is using it
        ov.keep_pinned()
        self.assertEqual(win.pinned, [5], "never interrupt the focused window")

        win.foreground = False                # user went back to the game
        ov.keep_pinned()
        self.assertEqual(win.pinned, [5, 5])

    def test_adoption_keeps_retrying_after_startup(self):
        # Reported: browser opened, never pinned. Adoption used to run only
        # during start(), so a window that showed up late — after a sign-in, or
        # a slow first paint — was never picked up at all.
        class _FakeWin:
            def __init__(self):
                self.windows, self.pinned = [], []

            def browser_windows(self):
                return list(self.windows)

            def pin(self, hwnd):
                self.pinned.append(hwnd)
                return True

            def alive(self, hwnd):
                return any(w[0] == hwnd for w in self.windows)

            def is_topmost(self, _h):
                return True                    # pin held; nothing to re-assert

            def is_foreground(self, _h):
                return False

        ov = sc_nav_heavy.HeavyOverlay("http://x/", log=lambda *_a: None)
        win = _FakeWin()
        ov._win, ov._before, ov._title_match = win, set(), "Org Navigator"

        ov.keep_pinned()                       # nothing to adopt yet
        self.assertIsNone(ov.hwnd)
        self.assertEqual(win.pinned, [])

        win.windows.append((5, "Chrome_WidgetWin_1", "Org Navigator"))
        ov.keep_pinned()                       # it appeared -> adopt + pin
        self.assertEqual(ov.hwnd, 5)
        self.assertEqual(win.pinned, [5])

        win.windows.clear()                    # user closed it
        ov.keep_pinned()
        self.assertIsNone(ov.hwnd)

    def test_url_building(self):
        self.assertEqual(sc_nav_heavy.heavy_url("https://nav.example.org/"),
                         "https://nav.example.org/#/")
        self.assertEqual(sc_nav_heavy.heavy_url("http://box:8765", "#/halo"),
                         "http://box:8765/#/halo")
        self.assertEqual(sc_nav_heavy.heavy_url(None), "")


class OverlayModeTests(unittest.TestCase):
    """off | light | heavy, sticky, with the W1 boolean migrated (#40 §13.5)."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)                     # start with no config at all
        self._real = sc_nav_watcher.CONFIG_PATH
        sc_nav_watcher.CONFIG_PATH = self.path

    def tearDown(self):
        sc_nav_watcher.CONFIG_PATH = self._real
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _resolve(self, mode=None, overlay=None):
        args = types.SimpleNamespace(overlay_mode=mode, overlay=overlay)
        return sc_nav_watcher.resolve_overlay(args)

    def _saved(self, value):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"overlay": value}, fh)

    def test_first_run_unanswered_is_off(self):
        self.assertEqual(self._resolve(), "off")

    def test_modes_stick_across_runs(self):
        self.assertEqual(self._resolve(mode="heavy"), "heavy")
        self.assertEqual(self._resolve(), "heavy")       # later run, no flag
        self.assertEqual(self._resolve(mode="light"), "light")
        self.assertEqual(self._resolve(), "light")

    def test_legacy_boolean_config_migrates(self):
        # W1 wrote a bool. An existing watcher_config.json must keep working.
        self._saved(True)
        self.assertEqual(self._resolve(), "light")
        self._saved(False)
        self.assertEqual(self._resolve(), "off")

    def test_old_flags_remain_aliases(self):
        self.assertEqual(self._resolve(overlay=True), "light")
        self.assertEqual(self._resolve(overlay=False), "off")

    def test_mode_flag_beats_the_legacy_flag(self):
        self.assertEqual(self._resolve(mode="heavy", overlay=True), "heavy")

    def test_garbage_saved_value_falls_back_to_off(self):
        self._saved("sideways")
        self.assertEqual(self._resolve(), "off")


if __name__ == "__main__":
    unittest.main(verbosity=2)
