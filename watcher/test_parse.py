"""Parser tests for sc_nav_watcher. Run: python3 test_parse.py"""

import unittest

from sc_nav_watcher import parse_showlocation


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
