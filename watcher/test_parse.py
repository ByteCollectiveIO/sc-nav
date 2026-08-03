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
import sc_nav_win32
from sc_nav_overlay import (
    age_color, compass_point, format_age, format_bearing, format_distance,
    format_eta, hud_lines, trim_name, MAX_NAME, STALE,
)
from sc_nav_watcher import (
    GameLogShardReader, heartbeat_due, parse_showlocation, parse_trade_txn,
)

_JOIN = ("<2026-06-20T00:30:29.237Z> [Notice] <Join PU> address[34.21.5.134] "
         "port[64317] shard[pub_use1b_12030094_130] locationId[562954248454145] "
         "[Team_GameServices][GIM][Matchmaking]\n")
_UPDATE = ("<2026-06-20T00:30:29.514Z> [Notice] <Update Shard Id> New Shard Id: "
           "{shard}. Old Shard Id [Team_OnlineTech][Telemetry][Services]\n")

# Real commodity-kiosk lines from the verified 2026-08-02 capture (#41), player
# ids scrubbed. The buy quantity is centi-SCU; the sell quantity is plain SCU.
_TXN_BUY = (
    "<2026-08-02T17:34:48.023Z> [Notice] "
    "<CEntityComponentCommodityUIProvider::SendCommodityBuyRequest> Sending "
    "SShopCommodityBuyRequest - playerId[123] shopId[729880064990] "
    "shopName[SCShop_ht_delta_rayari_m_store] kioskId[729880064989] "
    "price[98840.000000] shopPricePerCentiSCU[34.082699] "
    "resourceGUID[d5506a24-5729-4354-81fb-0959173357c4] autoLoading[0] "
    "quantity[2900.000000 cSCU] Cargo Box Data: boxSize[1.000000] | "
    "unitAmount[29] [Team_CoreGameplayFeatures][Shops][UI]\n")
_TXN_SELL = (
    "<2026-08-02T18:00:08.511Z> [Notice] "
    "<CEntityComponentCommodityUIProvider::SendCommoditySellRequest> Sending "
    "SShopCommoditySellRequest - playerId[123] shopId[730441176477] "
    "shopName[TDD_SCShop-001] kioskId[730441176513] amount[161211.000000] "
    "resourceGUID[d5506a24-5729-4354-81fb-0959173357c4] autoLoading[1] "
    "quantity[29] transactionMode[ResourceContainer] Cargo Box Data:  "
    "[boxSize[1] | unitAmount[29]] [Team_CoreGameplayFeatures][Shops][UI]\n")


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

    def test_collects_trade_transactions(self):
        # One tail, two consumers (#41): the same poll that tracks the shard
        # collects commodity transactions; pop drains them exactly once.
        self._write(_JOIN + _TXN_BUY + _TXN_SELL)
        self.reader.poll()
        txns = self.reader.pop_transactions()
        self.assertEqual([t["side"] for t in txns], ["buy", "sell"])
        self.assertEqual(self.reader.pop_transactions(), [])
        # Later appends are picked up incrementally, same as shard lines.
        self._write(_TXN_BUY)
        self.reader.poll()
        self.assertEqual(len(self.reader.pop_transactions()), 1)


class TradeTxnParseTests(unittest.TestCase):
    """parse_trade_txn (#41) against the captured, independently-verified
    lines: 29 SCU Medical Supplies bought at 3,408.27/SCU (98,840 total) and
    sold at 5,559/SCU (161,211 total)."""

    def test_buy_line(self):
        t = parse_trade_txn(_TXN_BUY.strip())
        self.assertEqual(t["side"], "buy")
        self.assertEqual(t["shop"], "SCShop_ht_delta_rayari_m_store")
        self.assertEqual(t["guid"], "d5506a24-5729-4354-81fb-0959173357c4")
        self.assertEqual(t["total"], 98840.0)
        self.assertEqual(t["scu"], 29.0)              # 2900 cSCU
        self.assertAlmostEqual(t["unit_price"], 3408.27, places=2)
        self.assertEqual(t["t"], "2026-08-02T17:34:48.023Z")
        # Cargo handling: this buy went to the freight elevator, as 29 boxes
        # of 1 SCU (box count is what a load actually costs time on).
        self.assertIs(t["auto_load"], False)
        self.assertEqual(t["box_size"], 1.0)
        self.assertEqual(t["box_count"], 29)

    def test_sell_line(self):
        t = parse_trade_txn(_TXN_SELL.strip())
        self.assertEqual(t["side"], "sell")
        self.assertEqual(t["shop"], "TDD_SCShop-001")
        self.assertEqual(t["total"], 161211.0)
        self.assertEqual(t["scu"], 29.0)              # sell qty is plain SCU
        self.assertAlmostEqual(t["unit_price"], 5559.0, places=2)
        # Sold straight off the ship — and the sell line writes its box data in
        # its own bracket style, which must parse the same as the buy's.
        self.assertIs(t["auto_load"], True)
        self.assertEqual(t["box_size"], 1.0)
        self.assertEqual(t["box_count"], 29)

    def test_cargo_handling_is_optional(self):
        # A patch that stops writing autoLoading / Cargo Box Data must cost us
        # those fields only — the money still lands.
        line = _TXN_BUY.strip().replace("autoLoading[0] ", "").replace(
            "Cargo Box Data: boxSize[1.000000] | unitAmount[29] ", "")
        t = parse_trade_txn(line)
        self.assertEqual(t["total"], 98840.0)
        self.assertNotIn("auto_load", t)
        self.assertNotIn("box_size", t)
        self.assertNotIn("box_count", t)

    def test_foreign_lines_return_none(self):
        self.assertIsNone(parse_trade_txn(_JOIN.strip()))
        self.assertIsNone(parse_trade_txn(""))
        # The kiosk-board inventory line mentions the component but is not a
        # transaction — it must not parse as one.
        self.assertIsNone(parse_trade_txn(
            "<2026-08-02T17:33:56.573Z> [Notice] "
            "<CEntityComponentCommodityUIProvider::LoadShopInventoryData::"
            "<lambda_1>::operator ()> AddingCommodityBox - playerId[123] "
            "shopName[SCShop_x] commodityName[ResourceType.Iron] "
            "Available Box Sizes:  boxSize[1] [Team_CoreGameplayFeatures][Shops][UI]"))

    def test_unstamped_or_zero_quantity_rejected(self):
        # No leading timestamp -> not a real transaction line.
        self.assertIsNone(parse_trade_txn(_TXN_BUY.strip().split("> ", 1)[1]))
        self.assertIsNone(parse_trade_txn(
            _TXN_SELL.strip().replace("quantity[29]", "quantity[0]")))


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
    def withdraw(self): self.shown = False
    def deiconify(self): self.shown = True

    shown = True

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
        # This is the no-Win32 path — the fallback that keeps the W1 behaviour
        # on any box where the foreground watcher can't run. With Win32 the
        # re-assert is driven by focus changes instead (LightOverlayInertTests).
        def calls_over(ticks):
            ov = self._overlay()
            seen = []
            ov._keep_on_top = lambda force=False: seen.append(1)
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


class _StubWin32:
    """Stand-in for sc_nav_win32.Win32 — the shape both overlays call through.

    Not a mock of Windows, and it can't be: it proves the *decisions* (adopt,
    pin, go inert, recover) are wired to the right conditions. Whether
    WS_EX_TRANSPARENT actually stops a click reaching Chromium is a question
    only the Windows box can answer."""

    def __init__(self, windows=None):
        self.windows = list(windows or [])
        self.pinned, self.closed, self.terminated = [], [], []
        self.styles = {}
        self.topmost, self.foreground, self.hung = True, False, False
        self.fg_hwnd, self.fg_path = 0, ""
        self.path_lookups = 0
        self.key = False
        self.rect, self.monitor = (0, 0, 2560, 1440), (0, 0, 2560, 1440)

    # window discovery / state
    def windows_of_class(self, _cls):
        return list(self.windows)

    def alive(self, hwnd):
        return any(w[0] == hwnd for w in self.windows)

    def is_topmost(self, _hwnd):
        return self.topmost

    def is_foreground(self, _hwnd):
        return self.foreground

    def is_hung(self, _hwnd):
        return self.hung

    # control
    def pin(self, hwnd):
        self.pinned.append(hwnd)
        self.topmost = True
        return True

    def click_through(self, hwnd, on, layered=False):
        self.styles[hwnd] = (on, layered)
        return True

    def close(self, hwnd):
        self.closed.append(hwnd)

    def terminate(self, pid):
        self.terminated.append(pid)
        return True

    # identity
    def foreground_window(self):
        return self.fg_hwnd

    def window_pid(self, hwnd):
        return 1000 + int(hwnd)

    def own_pid(self):
        return 1

    def process_path(self, _pid):
        self.path_lookups += 1
        return self.fg_path

    def key_down(self, _vk):
        return self.key

    def window_rect(self, _hwnd):
        return self.rect

    def monitor_rect(self, _hwnd):
        return self.monitor


def _spin(overlay, ticks=None):
    """Run enough ticks to cross the adoption throttle at least once."""
    for _ in range(ticks or sc_nav_heavy.HeavyOverlay.ADOPT_EVERY):
        overlay.keep_pinned()


class Win32HelperTests(unittest.TestCase):
    """The shared pure layer (#40.1 I1) — no ctypes, runs anywhere."""

    def test_game_detection_is_by_exe_name(self):
        self.assertTrue(sc_nav_win32.is_game(r"D:\Games\LIVE\Bin64\StarCitizen.exe"))
        self.assertTrue(sc_nav_win32.is_game("/mnt/g/StarCitizen.EXE"))
        self.assertFalse(sc_nav_win32.is_game(r"C:\...\RSI Launcher.exe"))
        self.assertFalse(sc_nav_win32.is_game(""))

    def test_interactive_fails_safe_until_the_watcher_answers(self):
        # The failure to design out: an overlay that is silently unclickable
        # is worse than one that occasionally eats a click.
        self.assertTrue(sc_nav_win32.overlay_interactive(
            known=False, game_foreground=True))

    def test_inert_only_while_the_game_is_in_front(self):
        self.assertFalse(sc_nav_win32.overlay_interactive(
            known=True, game_foreground=True))
        self.assertTrue(sc_nav_win32.overlay_interactive(
            known=True, game_foreground=False))

    def test_interact_key_and_drag_both_override(self):
        self.assertTrue(sc_nav_win32.overlay_interactive(
            known=True, game_foreground=True, key_held=True))
        # Mid-drag the key may already be released; flipping click-through on
        # then would drop the window somewhere between where it was and where
        # the pilot was putting it.
        self.assertTrue(sc_nav_win32.overlay_interactive(
            known=True, game_foreground=True, key_held=False, busy=True))

    def test_covers_monitor_tolerates_a_pixel_or_two(self):
        mon = (0, 0, 2560, 1440)
        self.assertTrue(sc_nav_win32.covers_monitor((0, 0, 2560, 1440), mon))
        self.assertTrue(sc_nav_win32.covers_monitor((-1, 0, 2561, 1441), mon))
        self.assertFalse(sc_nav_win32.covers_monitor((100, 100, 900, 700), mon))
        self.assertFalse(sc_nav_win32.covers_monitor(None, mon))

    def test_vk_code_defaults_rather_than_raising(self):
        self.assertEqual(sc_nav_win32.vk_code("f"), ord("F"))
        self.assertEqual(sc_nav_win32.vk_code("4"), ord("4"))
        # Hand-edited config: a typo must not take the overlay down.
        self.assertEqual(sc_nav_win32.vk_code("Left Alt"), ord("F"))
        self.assertEqual(sc_nav_win32.vk_code(None), ord("F"))

    def test_foreground_watcher_resolves_a_path_once_per_window(self):
        win = _StubWin32()
        win.fg_hwnd, win.fg_path = 10, r"D:\LIVE\Bin64\StarCitizen.exe"
        fg = sc_nav_win32.ForegroundWatcher(win)

        self.assertTrue(fg.poll())              # first sight = a change
        self.assertTrue(fg.known)
        self.assertTrue(fg.game_foreground)
        for _ in range(20):                     # a flight's worth of ticks
            fg.poll()
        self.assertEqual(win.path_lookups, 1,
                         "the image path must be cached against the HWND")

        win.fg_hwnd, win.fg_path = 11, r"C:\Discord\Discord.exe"
        self.assertTrue(fg.poll())
        self.assertFalse(fg.game_foreground)
        self.assertEqual(win.path_lookups, 2)
        # Back to the game: still cached, and the game HWND is remembered so
        # the fullscreen check has something to measure.
        win.fg_hwnd, win.fg_path = 10, ""
        fg.poll()
        self.assertTrue(fg.game_foreground)
        self.assertEqual(fg.game_hwnd, 10)
        self.assertEqual(win.path_lookups, 2)

    def test_watcher_without_win32_stays_unknown(self):
        fg = sc_nav_win32.ForegroundWatcher(None)
        self.assertFalse(fg.poll())
        self.assertFalse(fg.known)


class LightOverlayInertTests(unittest.TestCase):
    """The light HUD's I1 pass: inert while you fly, out of the way otherwise.

    Same caveat as heavy mode — these prove the decisions, not that Windows
    honours them."""

    _GAME = r"D:\LIVE\Bin64\StarCitizen.exe"

    def _overlay(self, **config):
        import sc_nav_overlay
        with mock.patch.dict(sys.modules, {"tkinter": _fake_tkinter()}):
            ov = sc_nav_overlay.Overlay(config=config, log=lambda *_a: None)
        win = _StubWin32()
        ov._win, ov._hwnd, ov._pid = win, 1, 7
        ov._fg = sc_nav_win32.ForegroundWatcher(win)
        ov._vk = ord("F")
        return ov, win

    def _front(self, ov, win, hwnd, path):
        win.fg_hwnd, win.fg_path = hwnd, path
        ov._interact()

    def test_click_through_follows_the_game(self):
        ov, win = self._overlay()
        self._front(ov, win, 99, self._GAME)
        self.assertEqual(win.styles[1], (True, False))
        self._front(ov, win, 12, r"C:\Discord\Discord.exe")
        self.assertEqual(win.styles[1], (False, False))

    def test_holding_the_interact_key_makes_it_live(self):
        # F is the same key SC uses to free the cursor, so "cursor free" and
        # "HUD clickable" are one gesture — and it's what the on-glass hint has
        # always said the drag needs.
        ov, win = self._overlay()
        self._front(ov, win, 99, self._GAME)
        self.assertEqual(win.styles[1], (True, False))
        win.key = True
        ov._interact()
        self.assertEqual(win.styles[1], (False, False))

    def test_a_drag_in_progress_is_never_interrupted(self):
        ov, win = self._overlay()
        win.key = True
        self._front(ov, win, 99, self._GAME)
        ov._dragging = True
        win.key = False                      # let go of F mid-gesture
        ov._interact()
        self.assertEqual(win.styles[1], (False, False))
        ov._dragging = False
        ov._interact()
        self.assertEqual(win.styles[1], (True, False))

    def test_it_hides_for_other_apps_but_only_once_the_game_has_run(self):
        ov, win = self._overlay()
        # Before the game has ever been in front the HUD must stay put — this
        # is the window you position while you're setting it up, from a console
        # that necessarily has focus.
        self._front(ov, win, 12, r"C:\Discord\Discord.exe")
        self.assertTrue(ov._root.shown)

        self._front(ov, win, 99, self._GAME)
        self.assertTrue(ov._root.shown)
        self._front(ov, win, 12, r"C:\Discord\Discord.exe")
        self.assertFalse(ov._root.shown, "topmost over Discord all session was "
                                         "the complaint")
        self._front(ov, win, 99, self._GAME)
        self.assertTrue(ov._root.shown)

    def test_the_hud_comes_back_when_the_game_exits(self):
        # Otherwise "the game ran once" would hide the HUD for the rest of the
        # session, with no taskbar button to get it back.
        ov, win = self._overlay()
        self._front(ov, win, 99, self._GAME)
        self._front(ov, win, 12, r"C:\Discord\Discord.exe")
        self.assertFalse(ov._root.shown)
        ov._game_seen_t -= (sc_nav_overlay_module().GAME_RECENT_S + 1)
        ov._interact()
        self.assertTrue(ov._root.shown)

    def test_autohide_and_clickthrough_can_be_switched_off(self):
        ov, win = self._overlay(overlay_clickthrough=False,
                                overlay_autohide=False)
        self._front(ov, win, 99, self._GAME)
        self.assertEqual(win.styles, {})
        self._front(ov, win, 12, r"C:\Discord\Discord.exe")
        self.assertTrue(ov._root.shown)

    def test_topmost_is_re_asserted_on_a_focus_change_not_a_timer(self):
        ov, win = self._overlay()
        self._front(ov, win, 99, self._GAME)
        self.assertEqual(win.pinned, [1])
        for _ in range(20):                  # same window still in front
            ov._interact()
        self.assertEqual(win.pinned, [1], "a periodic re-assert is not free")
        win.topmost = False                  # something superseded us
        ov._interact()
        self.assertEqual(win.pinned, [1, 1])

    def test_fullscreen_hint_is_logged_once(self):
        ov, win = self._overlay()
        logged = []
        ov._log = logged.append
        for _ in range(5):
            self._front(ov, win, 99, self._GAME)
        self.assertEqual(len(logged), 1)
        self.assertIn("Borderless", logged[0])

    def test_no_fullscreen_hint_for_a_windowed_game(self):
        ov, win = self._overlay()
        logged = []
        ov._log = logged.append
        win.rect = (100, 100, 1900, 1100)
        self._front(ov, win, 99, self._GAME)
        self.assertEqual(logged, [])


def sc_nav_overlay_module():
    import sc_nav_overlay
    return sc_nav_overlay


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

    def test_launch_command_carries_profile_and_stability_flags(self):
        cmd = sc_nav_heavy.browser_command("msedge.exe", "https://nav/#/",
                                           (10, 20, 800, 600), r"C:\w\profile")
        self.assertIn("--app=https://nav/#/", cmd)
        self.assertIn("--window-size=800,600", cmd)
        self.assertIn("--window-position=10,20", cmd)
        self.assertIn(r"--user-data-dir=C:\w\profile", cmd)
        # The freeze fix. These only take effect when Chromium actually starts
        # a browser process — which is precisely why the private profile is no
        # longer optional-by-default: --app= handed to an already-running
        # browser is forwarded to a process that never saw these.
        self.assertIn("--disable-features=CalculateNativeWinOcclusion", cmd)
        self.assertIn("--disable-renderer-backgrounding", cmd)
        # A fresh profile must not open a first-run interstitial in a window
        # with no address bar to escape it.
        self.assertIn("--no-first-run", cmd)

    def test_shared_profile_escape_hatch_omits_user_data_dir(self):
        cmd = sc_nav_heavy.browser_command("msedge.exe", "https://nav/#/")
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
        class _FakeWin(_StubWin32):
            def __init__(self):
                super().__init__([(5, "Chrome_WidgetWin_1", "Org Navigator")])
                self.topmost = True

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
        ov = sc_nav_heavy.HeavyOverlay("http://x/", log=lambda *_a: None)
        win = _StubWin32([])
        ov._win, ov._before, ov._title_match = win, set(), "Org Navigator"

        ov.keep_pinned()                       # nothing to adopt yet
        self.assertIsNone(ov.hwnd)
        self.assertEqual(win.pinned, [])

        win.windows.append((5, "Chrome_WidgetWin_1", "Org Navigator"))
        _spin(ov)                              # it appeared -> adopt + pin
        self.assertEqual(ov.hwnd, 5)
        self.assertEqual(win.pinned, [5])

        win.windows.clear()                    # user closed it
        ov.keep_pinned()
        self.assertIsNone(ov.hwnd)

    def test_window_pick_prefers_our_own_process(self):
        # With a private profile nothing is reused, so our child IS the
        # browser — a pid match is certainty where a title match is a guess.
        before = set()
        windows = [(4, "Chrome_WidgetWin_1", "Org Navigator"),
                   (6, "Chrome_WidgetWin_1", "Discord")]
        self.assertEqual(
            sc_nav_heavy.pick_window(before, windows, "Org Navigator",
                                     pids={4: 900, 6: 4242}, want_pid=4242), 6)
        # No pid to go on -> the old title rule, unchanged.
        self.assertEqual(
            sc_nav_heavy.pick_window(before, windows, "Org Navigator"), 4)

    def _adopted(self, **config):
        ov = sc_nav_heavy.HeavyOverlay("http://x/", config=config,
                                       log=lambda *_a: None)
        win = _StubWin32([(5, "Chrome_WidgetWin_1", "Org Navigator")])
        ov._win, ov._before, ov._title_match = win, set(), "Org Navigator"
        ov._fg = sc_nav_win32.ForegroundWatcher(win)
        ov.keep_pinned()                       # adopt
        return ov, win

    def test_goes_inert_while_the_game_is_in_front(self):
        # The reported bug: swinging a tractor-beamed box across the screen
        # walked the cursor onto the overlay, which ate the click and dropped
        # the beam. A click-through window is not a hit-test target at all.
        ov, win = self._adopted()
        win.fg_hwnd, win.fg_path = 99, r"D:\LIVE\Bin64\StarCitizen.exe"
        ov.keep_pinned()
        self.assertEqual(win.styles[5], (True, False))

        win.fg_hwnd, win.fg_path = 5, r"C:\Edge\msedge.exe"   # alt-tabbed to it
        ov.keep_pinned()
        self.assertEqual(win.styles[5], (False, False))

    def test_click_through_is_only_written_when_it_changes(self):
        # Same lesson as the pin: touching a Chromium window has costs, so
        # never write a style word that already says what we want.
        ov, win = self._adopted()
        win.fg_hwnd, win.fg_path = 99, r"D:\LIVE\Bin64\StarCitizen.exe"
        ov.keep_pinned()
        win.styles.clear()
        for _ in range(20):
            ov.keep_pinned()
        self.assertEqual(win.styles, {})

    def test_click_through_can_be_turned_off_or_made_layered(self):
        ov, win = self._adopted(heavy_clickthrough="off")
        win.fg_hwnd, win.fg_path = 99, r"D:\LIVE\Bin64\StarCitizen.exe"
        ov.keep_pinned()
        self.assertEqual(win.styles, {})

        ov, win = self._adopted(heavy_clickthrough="layered")
        win.fg_hwnd, win.fg_path = 99, r"D:\LIVE\Bin64\StarCitizen.exe"
        ov.keep_pinned()
        self.assertEqual(win.styles[5], (True, True))

    def test_a_wedged_window_is_reopened_but_not_forever(self):
        # Reported from flight: the overlay froze and stopped taking clicks;
        # the only way out was alt-tabbing to the console and restarting the
        # watcher. IsWindow stays true for a hung window, so nothing noticed.
        ov, win = self._adopted()
        restarts = []
        ov.start = lambda: (restarts.append(1), True)[1]

        win.hung = True
        _spin(ov, sc_nav_heavy.HeavyOverlay.HANG_TICKS - 2)
        self.assertEqual(restarts, [], "a slow first paint is not a wedge")

        _spin(ov, 2)
        self.assertEqual(len(restarts), 1)
        self.assertIn(5, win.closed)
        # WM_CLOSE is what a hung window ignores; on our own profile the
        # browser process is ours alone, so ending it is safe.
        self.assertEqual(win.terminated, [1005])

        _spin(ov, sc_nav_heavy.HeavyOverlay.HANG_TICKS * 8)
        self.assertEqual(len(restarts), sc_nav_heavy.HeavyOverlay.MAX_RECOVERIES,
                         "an unrecoverable cause must not become a relaunch loop")

    def test_recovery_can_be_switched_off(self):
        ov, win = self._adopted(heavy_autorecover=False)
        restarts = []
        ov.start = lambda: (restarts.append(1), True)[1]
        win.hung = True
        _spin(ov, sc_nav_heavy.HeavyOverlay.HANG_TICKS * 3)
        self.assertEqual(restarts, [])

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
