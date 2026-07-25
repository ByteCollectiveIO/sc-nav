"""Parser tests for sc_nav_watcher. Run: python3 test_parse.py"""

import os
import tempfile
import unittest

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


class StickyOverlayFlagTests(unittest.TestCase):
    """A boolean can't ride `_resolve_sticky`: False is an answer, not "unset"."""

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

    def _resolve(self, value):
        return sc_nav_watcher._resolve_sticky_flag(value, "overlay", default=False)

    def test_first_run_unanswered_is_off(self):
        self.assertFalse(self._resolve(None))

    def test_yes_sticks_across_runs(self):
        self.assertTrue(self._resolve(True))
        self.assertTrue(self._resolve(None))     # later run, no flag passed

    def test_explicit_no_turns_a_saved_yes_back_off(self):
        self._resolve(True)
        self.assertFalse(self._resolve(False))   # --no-overlay
        self.assertFalse(self._resolve(None))    # and the OFF is what sticks


if __name__ == "__main__":
    unittest.main(verbosity=2)
