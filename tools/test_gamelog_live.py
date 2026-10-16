"""Tests for gamelog_live.py (run: python tools/test_gamelog_live.py)."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gamelog_live as gl  # noqa: E402


def at(sec):
    return datetime(2026, 10, 15, 1, 0, sec, tzinfo=timezone.utc)


def line(sec, tag, msg="x"):
    return f"<2026-10-15T01:00:{sec:02d}.000Z> [Notice] <{tag}> {msg} [Team_X]"


def note(sec, text):
    return line(sec, "SHUDEvent_OnNotification", f'Added notification "{text}" [3] to queue.')


QT = "[ItemNavigation][CL][2840] | NOT AUTH | {v}[{id}]|CSCItemNavigation::X|{msg}"


class ProfilerTest(unittest.TestCase):
    def setUp(self):
        self.printed = []
        self.p = gl.Profiler(baseline={"Known"}, echo="Echo Me", out=self.printed.append)

    def test_new_tag_flagged_once_and_numbers_folded(self):
        self.p.feed(line(1, "Known"))
        self.p.feed(line(2, "StatObjLoad 0x800 Format"))
        self.p.feed(line(3, "StatObjLoad 0x900 Format"))
        news = [s for s in self.printed if s.startswith("NEW")]
        self.assertEqual(len(news), 1)
        self.assertIn("StatObjLoad # Format", self.p.seen_keys())

    def test_marker_window_ranks_concentrated_tag_first(self):
        for s in range(0, 40, 2):                  # background all session long
            self.p.feed(line(s, "Known"))
        self.p.feed(line(19, "Rare Thing"))
        self.p.feed(line(25, "Rare Thing"))
        self.p.feed(line(50, "Rare Thing"))        # outside the window
        mk = self.p.add_marker(t=at(20), label="did the thing")
        ranked = self.p.marker_ranking(mk)
        self.assertEqual(ranked[0][:3], ("Rare Thing", 2, 3))
        self.assertIn("did the thing", self.p.report())

    def test_marker_window_bounds(self):
        self.p.feed(line(14, "Too Early"))         # 6s before: outside PRE (5s)
        self.p.feed(line(16, "Inside"))
        self.p.feed(line(41, "Too Late"))          # 21s after: outside POST (20s)
        mk = self.p.add_marker(t=at(20))
        self.assertEqual({t for _d, t, _r in self.p.window(mk)}, {"Inside"})

    def test_retro_marker_from_local_clock(self):
        for s in (1, 30, 59):
            self.p.feed(line(s, "Known"))
        local = at(30).astimezone()
        mk = self.p.add_marker_at_clock(f"{local:%H:%M:%S} scanned rock")
        self.assertEqual(mk.t, at(30))
        self.assertEqual(mk.label, "scanned rock")
        self.assertIsNone(self.p.add_marker_at_clock("not a time"))

    def test_identity_scrubbed_in_report(self):
        self.p.feed("<2026-10-15T01:00:01.000Z> [Notice] <AccountLoginCharacterStatus_Character> "
                    "Character: geid 204698941918 - accountId 385725 - name Pilot01 - state STATE_CURRENT")
        self.p.feed(line(2, "Brand New", "Player[Pilot01] at 10.1.2.3"))
        self.p.feed(note(3, "You have joined channel 'Drake Golem : Pilot01'."))
        rep = self.p.report()
        self.assertNotIn("Pilot01", rep)
        self.assertNotIn("10.1.2.3", rep)

    def test_timeline_hides_hints_and_repeats(self):
        self.p.feed(note(1, "Entered Armistice Zone: "))
        self.p.feed(note(2, "Entered Armistice Zone: "))
        self.p.feed(note(3, "Stamina: Monitor your Heart Rate to avoid becoming fatigued."))
        self.p.feed(note(4, "Quantum Travel: Once you've set a destination in the Map..."))
        self.p.feed(note(5, "Quantum Travel: Your destination is obstructed."))
        texts = [e[2] for e in self.p.timeline]
        self.assertEqual(texts, ["Entered Armistice Zone", "Quantum Travel: Your destination is obstructed."])
        self.assertEqual(sum(self.p.hints.values()), 2)
        self.assertIn("2 tutorial hints hidden", self.p.report())

    def test_qt_lifecycle_and_other_ship_flagged(self):
        self.p.feed(line(1, "CEntityComponentShipListProvider::SetVehicleSpawningInformations",
                         "SetVehicleSpawningInformations - VehicleEntityId: [868584930160], LandingArea: X"))
        self.p.feed(line(2, "Quantum Drive Arrived - Arrived at Final Destination",
                         QT.format(v="ANVL_Asgard_883419175264", id=883419175264, msg="arrived")))
        self.p.feed(line(3, "Player Selected Quantum Target - Local",
                         QT.format(v="DRAK_Golem_868584930160", id=868584930160,
                                   msg="Player has selected point OOC_Stanton_4b_Clio as their destination")))
        self.p.feed(line(3, "Calculate Route", QT.format(
            v="DRAK_Golem_868584930160", id=868584930160,
            msg="Projected Start Location is Stanton System for route to destination OOC_Stanton_4b_Clio")))
        self.p.feed(line(3, "Calculate Route", QT.format(
            v="DRAK_Golem_868584930160", id=868584930160,
            msg="Successfully calculated route to OOC_Stanton_4b_Clio fuel estimate 779.780762")))
        qt = [(e[2], e[3]) for e in self.p.timeline if e[1] == "qt"]
        self.assertEqual(qt, [
            ("QT arrived (ANVL_Asgard)", True),
            ("QT target selected: OOC_Stanton_4b_Clio (DRAK_Golem)", False),
            ("QT route: Stanton System → OOC_Stanton_4b_Clio (DRAK_Golem)", False),
            ("QT fuel estimate to OOC_Stanton_4b_Clio: 779.78 (DRAK_Golem)", False),
        ])
        echoed = [s for s in self.printed if not s.startswith("NEW")]
        self.assertFalse(any("Asgard" in s for s in echoed))   # not echoed live
        self.assertIn("~~QT arrived (ANVL_Asgard)~~", self.p.report())

    def test_location_and_shop_lines(self):
        self.p.feed(line(1, "RequestLocationInventory",
                         "Player[P] requested inventory for Location[RR_MIC_L1]"))
        self.p.feed(line(2, "CEntityComponentShopUIProvider::SendShopBuyRequest",
                         "Sending SShopBuyRequest - shopId[1] shopName[SCShop_RefineryAdmin_ITEM] "
                         "client_price[42.0] itemName[Drink_bottle_cruz_01_lux_a] quantity[6]"))
        self.assertEqual([e[2] for e in self.p.timeline], [
            "at RR_MIC_L1", "buy at SCShop_RefineryAdmin_ITEM: Drink_bottle_cruz_01_lux_a × 6"])

    def test_echo_extra_regex(self):
        self.p.feed(line(3, "Known", "Echo Me please"))
        self.assertTrue(any("Echo Me" in s for s in self.printed))

    def test_label(self):
        self.p.add_marker(t=at(1))
        self.assertTrue(self.p.label(1, "died"))
        self.assertFalse(self.p.label(2, "nope"))
        self.assertEqual(self.p.markers[0].label, "died")


class ClockTest(unittest.TestCase):
    def test_midnight_crossing_picks_the_day_inside_the_span(self):
        tz = datetime.now().astimezone().tzinfo
        start = datetime(2026, 10, 15, 23, 50, tzinfo=tz)
        end = start + timedelta(minutes=40)        # 00:30 next day
        got = gl.resolve_clock(0, 10, 0, start, end)
        self.assertEqual(got, datetime(2026, 10, 16, 0, 10, tzinfo=tz).astimezone(timezone.utc))
        got = gl.resolve_clock(23, 55, 0, start, end)
        self.assertEqual(got, datetime(2026, 10, 15, 23, 55, tzinfo=tz).astimezone(timezone.utc))

    def test_parse_clock(self):
        self.assertEqual(gl.parse_clock("19:16 scanned taranite"), (19, 16, 0, "scanned taranite"))
        self.assertEqual(gl.parse_clock("7:05:09"), (7, 5, 9, ""))
        self.assertIsNone(gl.parse_clock("25:00 nope"))


class TailTest(unittest.TestCase):
    def test_partial_lines_and_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "Game.log"
            f.write_bytes(b"one\ntw")
            t = gl.Tail(f)
            self.assertEqual(t.poll(), (["one"], False))
            with open(f, "ab") as fh:
                fh.write(b"o\n")
            self.assertEqual(t.poll(), (["two"], False))
            f.write_bytes(b"new\n")                 # relaunch truncates
            self.assertEqual(t.poll(), (["new"], True))


class EdgeTest(unittest.TestCase):
    def test_one_press_per_down_stroke(self):
        e = gl.EdgeDetector()
        self.assertEqual([e.update(d) for d in (False, True, True, True, False, True)],
                         [False, True, False, False, False, True])


class VkTest(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(gl.vk_for("F9"), 0x78)
        self.assertEqual(gl.vk_for("f1"), 0x70)
        self.assertEqual(gl.vk_for("Pause"), 0x13)
        self.assertEqual(gl.vk_for("k"), ord("K"))
        self.assertEqual(gl.vk_for("-"), 0xBD)
        self.assertEqual(gl.vk_for("Num5"), 0x65)
        self.assertEqual(gl.vk_for("num-"), 0x6D)
        self.assertIsNone(gl.vk_for("F25"))
        self.assertIsNone(gl.vk_for("banana"))


if __name__ == "__main__":
    unittest.main()
