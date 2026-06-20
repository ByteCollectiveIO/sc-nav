"""Parser tests for sc_nav_watcher. Run: python3 test_parse.py"""

import os
import tempfile
import unittest

from sc_nav_watcher import GameLogShardReader, parse_showlocation

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
