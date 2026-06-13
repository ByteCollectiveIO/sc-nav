"""nav_core tests against the real poi/containers dataset.

Run: python3 test_nav_core.py
"""

import math
import time
import unittest
from pathlib import Path

import nav_core
from nav_core import (
    compute_state,
    detect_container,
    great_circle,
    global_to_local_km,
    latlon_from_local,
    load_data,
    local_km_to_global,
    poi_global_m,
    search_pois,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "poi"
NAV = load_data(DATA_DIR)


def surface_pois(container_name, system="Stanton"):
    return [
        p
        for p in NAV.pois.values()
        if p.system == system and p.container_name == container_name
    ]


class DataLoadingTests(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(len(NAV.containers), 496)
        self.assertEqual(len(NAV.pois), 1885)
        self.assertIn("Stanton", NAV.systems)

    def test_space_pois_have_global_coords(self):
        space = [p for p in NAV.pois.values() if p.container_name is None]
        self.assertEqual(len(space), 13)
        self.assertTrue(all(p.global_m is not None for p in space))


class LatLonConventionTests(unittest.TestCase):
    def test_latlon_matches_dataset(self):
        """Derived lat/lon must match the stored values for ~all surface POIs.
        (3 known data-entry outliers exist in the dataset, e.g. 'Test Entry #1'.)"""
        mismatches = 0
        checked = 0
        for p in NAV.pois.values():
            cont = NAV.container_of(p)
            if not cont or not cont.body_radius or p.local_km is None:
                continue
            lat, lon, r = latlon_from_local(p.local_km)
            if r == 0:
                continue
            checked += 1
            lat_err = abs(lat - p.latitude)
            lon_err = abs((lon - p.longitude + 180) % 360 - 180)
            if lat_err > 0.01 or lon_err > 0.01:
                mismatches += 1
        self.assertGreater(checked, 1800)
        self.assertLessEqual(mismatches, 3)


class TransformTests(unittest.TestCase):
    def test_round_trip_on_rotating_body(self):
        daymar = NAV.containers[("Stanton", "Daymar")]
        self.assertGreater(daymar.rotation_speed, 0)
        local = (100.0, -200.0, 150.0)
        for t in (0.0, time.time(), 1.7e9 + 1234.5):
            g = local_km_to_global(daymar, local, t)
            back = global_to_local_km(daymar, g, t)
            for a, b in zip(local, back):
                self.assertAlmostEqual(a, b, places=6)

    def test_rotation_moves_global_position(self):
        daymar = NAV.containers[("Stanton", "Daymar")]
        local = (daymar.body_radius / 1000.0, 0.0, 0.0)
        g1 = local_km_to_global(daymar, local, 0.0)
        # half a rotation later the surface point is on the far side
        g2 = local_km_to_global(daymar, local, daymar.rotation_speed * 1800.0)
        self.assertGreater(nav_core.dist3(g1, g2), daymar.body_radius * 1.9)

    def test_non_rotating_body_is_static(self):
        # Stars don't spin in the dataset
        star = NAV.containers[("Stanton", "Stanton Star")]
        local = (1000.0, 2000.0, 3000.0)
        self.assertEqual(
            local_km_to_global(star, local, 0.0),
            local_km_to_global(star, local, 1e9),
        )


class DetectionTests(unittest.TestCase):
    def test_detects_container_from_poi_position(self):
        t = time.time()
        for name in ("Daymar", "Microtech", "Hurston", "Yela"):
            poi = surface_pois(name)[0]
            pos = poi_global_m(NAV, poi, t)
            found = detect_container(NAV, pos)
            self.assertIsNotNone(found, name)
            self.assertEqual(found.name, name)

    def test_deep_space_detects_nothing(self):
        self.assertIsNone(detect_container(NAV, (9e12, 9e12, 9e12)))


class GreatCircleTests(unittest.TestCase):
    def test_due_north(self):
        d, b = great_circle(0, 50, 10, 50, 1000_000)
        self.assertAlmostEqual(b, 0.0, places=6)
        self.assertAlmostEqual(d, math.radians(10) * 1000_000, places=3)

    def test_due_east_on_equator(self):
        d, b = great_circle(0, 10, 0, 20, 1000_000)
        self.assertAlmostEqual(b, 90.0, places=6)

    def test_antimeridian_wrap(self):
        _, b = great_circle(0, 179, 0, -179, 1000_000)
        self.assertAlmostEqual(b, 90.0, places=6)  # shortest path is eastward


class ComputeStateTests(unittest.TestCase):
    def test_standing_at_poi(self):
        t = time.time()
        pois = surface_pois("Daymar")
        here = pois[0]
        dest = next(p for p in pois[1:] if p.id != here.id)
        pos = poi_global_m(NAV, here, t)

        state = compute_state(NAV, pos, t, destination_id=dest.id)

        self.assertEqual(state["system"], "Stanton")
        self.assertEqual(state["container"]["name"], "Daymar")
        # lat/lon must match the POI we're standing at
        self.assertAlmostEqual(state["latitude"], here.latitude, places=3)
        lon_err = abs((state["longitude"] - here.longitude + 180) % 360 - 180)
        self.assertLess(lon_err, 1e-3)
        # nearest POI is the one we're standing at, ~0 m away
        self.assertEqual(state["nearest_pois"][0]["id"], here.id)
        self.assertLess(state["nearest_pois"][0]["distance_m"], 1.0)
        # destination block is populated with same-container surface nav
        d = state["destination"]
        self.assertEqual(d["id"], dest.id)
        self.assertTrue(d["same_container"])
        self.assertIsNotNone(d["bearing_deg"])
        self.assertGreaterEqual(d["bearing_deg"], 0)
        self.assertLess(d["bearing_deg"], 360)
        self.assertGreater(d["surface_distance_m"], 0)
        self.assertGreater(d["distance_m"], 0)

    def test_speed_and_eta(self):
        t = time.time()
        pois = surface_pois("Daymar")
        here, dest = pois[0], pois[1]
        pos2 = poi_global_m(NAV, here, t)
        # fabricate a previous sample 10s earlier, 5 km away -> 500 m/s
        pos1 = (pos2[0] + 5000.0, pos2[1], pos2[2])
        state = compute_state(
            NAV, pos2, t, destination_id=dest.id, prev_pos=pos1, prev_t=t - 10
        )
        self.assertAlmostEqual(state["speed_ms"], 500.0, places=6)
        self.assertIsNotNone(state["destination"]["eta_s"])

    def test_space_poi_destination(self):
        t = time.time()
        space_poi = next(p for p in NAV.pois.values() if p.container_name is None)
        pos = poi_global_m(NAV, surface_pois("Daymar")[0], t)
        state = compute_state(NAV, pos, t, destination_id=space_poi.id)
        d = state["destination"]
        self.assertFalse(d["same_container"])
        self.assertIsNotNone(d["distance_m"])
        self.assertIsNone(d["bearing_deg"])


class CustomPoiTests(unittest.TestCase):
    def test_capture_on_body(self):
        t = time.time()
        ref = surface_pois("Daymar")[0]
        pos = poi_global_m(NAV, ref, t)
        poi = nav_core.custom_poi_from_position(
            NAV, pos, t, "My Stash", "Stash", nav_core.CUSTOM_ID_START
        )
        self.assertTrue(poi.custom)
        self.assertEqual(poi.container_name, "Daymar")
        self.assertEqual(poi.system, "Stanton")
        # captured at the reference POI -> same lat/lon and ~same local coords
        self.assertAlmostEqual(poi.latitude, ref.latitude, places=4)
        for a, b in zip(poi.local_km, ref.local_km):
            self.assertAlmostEqual(a, b, places=3)
        # and it resolves back to the same global position at any later time
        g = poi_global_m(NAV, poi, t + 12345)
        ref_g = poi_global_m(NAV, ref, t + 12345)
        self.assertLess(nav_core.dist3(g, ref_g), 1.0)

    def test_capture_in_deep_space(self):
        poi = nav_core.custom_poi_from_position(
            NAV, (9e12, 9e12, 9e12), time.time(), "Nowhere", "Landmark", 1000001
        )
        self.assertIsNone(poi.container_name)
        self.assertEqual(poi.global_m, (9e12, 9e12, 9e12))
        self.assertIsNone(poi.latitude)

    def test_dict_round_trip_and_merge(self):
        t = time.time()
        ref = surface_pois("Yela")[0]
        pos = poi_global_m(NAV, ref, t)
        poi = nav_core.custom_poi_from_position(NAV, pos, t, "Cache A", "Cave", 1000002)
        back = nav_core.poi_from_custom_dict(nav_core.custom_poi_to_dict(poi))
        self.assertEqual(back, poi)

        nav2 = load_data(DATA_DIR)
        nav_core.merge_custom_pois(nav2, [nav_core.custom_poi_to_dict(poi)])
        self.assertIn(1000002, nav2.pois)
        found = search_pois(nav2, query="cache a")
        self.assertEqual(found[0]["id"], 1000002)
        self.assertTrue(found[0]["custom"])
        # custom POI shows up in nav state nearest list at its position
        state = compute_state(nav2, pos, t)
        self.assertIn(1000002, [n["id"] for n in state["nearest_pois"]])

    def test_owner_round_trips(self):
        t = time.time()
        pos = poi_global_m(NAV, surface_pois("Daymar")[0], t)
        poi = nav_core.custom_poi_from_position(
            NAV, pos, t, "Owned", "Stash", 1000003, owner_id=7, owner_handle="Pilot7"
        )
        back = nav_core.poi_from_custom_dict(nav_core.custom_poi_to_dict(poi))
        self.assertEqual(back.owner_id, 7)
        self.assertEqual(back.owner_handle, "Pilot7")


class ResourceNodeTests(unittest.TestCase):
    def test_quality_mapping(self):
        cases = {1: "Lowest", 2: "Low to Mid", 4: "Low to Mid",
                 5: "Good / High", 6: "Good / High", 7: "Very High", 8: "Perfect"}
        for band, label in cases.items():
            self.assertEqual(nav_core.quality_for_band(band), label)
        # out-of-range clamps
        self.assertEqual(nav_core.quality_for_band(0), "Lowest")
        self.assertEqual(nav_core.quality_for_band(99), "Perfect")

    def test_node_capture_and_geometry(self):
        t = time.time()
        ref = surface_pois("Daymar")[0]
        pos = poi_global_m(NAV, ref, t)
        node = nav_core.resource_node_from_position(
            NAV, pos, t, "Quantanium", 7, nav_core.RESOURCE_ID_START,
            biome="Desert", note="big rock", owner_id=3, owner_handle="Miner3",
        )
        self.assertEqual(node.quality, "Very High")
        self.assertEqual(node.container_name, "Daymar")
        self.assertEqual(node.owner_handle, "Miner3")
        self.assertIsNotNone(node.height_m)  # altitude auto-recorded
        self.assertAlmostEqual(node.latitude, ref.latitude, places=4)
        # resolves back to the same global position later in the rotation
        g = poi_global_m(NAV, node, t + 9999)
        ref_g = poi_global_m(NAV, ref, t + 9999)
        self.assertLess(nav_core.dist3(g, ref_g), 1.0)

    def test_node_dict_round_trip_and_state(self):
        t = time.time()
        ref = surface_pois("Yela")[0]
        pos = poi_global_m(NAV, ref, t)
        node = nav_core.resource_node_from_position(
            NAV, pos, t, "Bexalite", 5, nav_core.RESOURCE_ID_START + 1
        )
        back = nav_core.node_from_dict(nav_core.resource_node_to_dict(node))
        self.assertEqual(back, node)

        nav2 = load_data(DATA_DIR)
        nav_core.merge_resource_nodes(nav2, [nav_core.resource_node_to_dict(node)])
        state = compute_state(nav2, pos, t)
        node_ids = [n["id"] for n in state["nearest_nodes"]]
        self.assertIn(nav_core.RESOURCE_ID_START + 1, node_ids)
        # POIs and nodes stay in separate buckets
        self.assertNotIn(nav_core.RESOURCE_ID_START + 1,
                         [p["id"] for p in state["nearest_pois"]])

    def test_node_as_destination(self):
        t = time.time()
        ref = surface_pois("Daymar")[0]
        pos = poi_global_m(NAV, ref, t)
        nav2 = load_data(DATA_DIR)
        node = nav_core.resource_node_from_position(
            NAV, pos, t, "Gold", 3, nav_core.RESOURCE_ID_START + 2
        )
        nav2.nodes[node.id] = node
        # stand 5 km away, moving, expect a destination block with bearing+eta
        away = (pos[0] + 5000.0, pos[1], pos[2])
        state = compute_state(nav2, away, t, destination_id=node.id,
                              prev_pos=(away[0] + 5000, away[1], away[2]), prev_t=t - 10)
        self.assertEqual(state["destination"]["kind"], "resource")
        self.assertEqual(state["destination"]["quality"], "Low to Mid")
        self.assertIsNotNone(state["destination"]["eta_s"])


class DestinationFixTests(unittest.TestCase):
    """Regressions for the code-review fixes to compute_state's destination."""

    def test_same_named_container_other_system_is_not_same_container(self):
        # Build a synthetic cross-system name collision: a container named
        # "Daymar" in a fake system, with a POI on it.
        nav2 = load_data(DATA_DIR)
        stanton_daymar = nav2.containers[("Stanton", "Daymar")]
        fake = nav_core.Container(
            name="Daymar", system="FakeSys", type="Planet", internal_name="x",
            pos=(5e11, 0, 0), body_radius=stanton_daymar.body_radius,
            om_radius=stanton_daymar.om_radius, grid_radius=stanton_daymar.grid_radius,
            rotation_speed=0, rotation_adjustment=0,
        )
        nav2.containers[("FakeSys", "Daymar")] = fake
        far_poi = nav_core.Poi(
            id=999001, name="Far", system="FakeSys", container_name="Daymar",
            type="Outpost", local_km=(fake.body_radius / 1000.0, 0, 0), global_m=None,
            latitude=0.0, longitude=0.0, height_m=0.0, qt_marker=False, custom=True,
        )
        nav2.pois[far_poi.id] = far_poi

        t = time.time()
        # Stand on Stanton's Daymar; destination is the FakeSys 'Daymar' POI.
        here = [p for p in nav2.pois.values()
                if p.system == "Stanton" and p.container_name == "Daymar"][0]
        pos = poi_global_m(nav2, here, t)
        state = compute_state(nav2, pos, t, destination_id=far_poi.id)
        # Names match but systems differ -> must NOT be treated as same container.
        self.assertFalse(state["destination"]["same_container"])
        self.assertIsNone(state["destination"]["bearing_deg"])

    def test_destination_summarizer_matches_resolved_entity(self):
        # If an id somehow exists in BOTH pois and nodes, the summarizer must
        # follow the resolved entity (pois wins) and not crash.
        nav2 = load_data(DATA_DIR)
        t = time.time()
        ref = [p for p in nav2.pois.values()
               if p.system == "Stanton" and p.container_name == "Daymar"][0]
        pos = poi_global_m(nav2, ref, t)
        shared_id = 1234567
        poi = nav_core.custom_poi_from_position(nav2, pos, t, "Dup", "Stash", shared_id)
        node = nav_core.resource_node_from_position(nav2, pos, t, "Gold", 3, shared_id)
        nav2.pois[shared_id] = poi
        nav2.nodes[shared_id] = node
        state = compute_state(nav2, pos, t, destination_id=shared_id)
        # pois.get wins -> summarized as a POI, no AttributeError
        self.assertEqual(state["destination"]["kind"], "poi")
        self.assertEqual(state["destination"]["name"], "Dup")

    def test_eta_zero_surface_distance_does_not_fall_back_to_3d(self):
        nav2 = load_data(DATA_DIR)
        t = time.time()
        ref = [p for p in nav2.pois.values()
               if p.system == "Stanton" and p.container_name == "Daymar"][0]
        cont = nav2.container_of(ref)
        # Same lat/lon as ref but +0.5 km altitude: scale the LOCAL radial vector
        # so direction (hence lat/lon) is unchanged, then convert back to global.
        lk = ref.local_km
        r = math.sqrt(sum(c * c for c in lk))
        higher_local = tuple(c * (r + 0.5) / r for c in lk)
        higher = nav_core.local_km_to_global(cont, higher_local, t)
        prev = (higher[0] + 5000.0, higher[1], higher[2])  # 5 km away, dt 5s -> moving
        state = compute_state(nav2, higher, t, destination_id=ref.id,
                              prev_pos=prev, prev_t=t - 5)
        d = state["destination"]
        # surface distance is ~0 (same lat/lon); ETA must be ~0, not derived
        # from the nonzero 3D altitude difference.
        self.assertTrue(d["same_container"])
        self.assertLess(d["surface_distance_m"], 50.0)
        self.assertLess(d["eta_s"], 1.0)


class SearchTests(unittest.TestCase):
    def test_search_by_name(self):
        results = search_pois(NAV, query="sand cave", system="Stanton")
        self.assertTrue(results)
        self.assertTrue(all("sand cave" in r["name"].lower() for r in results))

    def test_container_filter(self):
        results = search_pois(NAV, container="Daymar", limit=1000)
        self.assertTrue(results)
        self.assertTrue(all(r["container"] == "Daymar" for r in results))


if __name__ == "__main__":
    unittest.main(verbosity=1)
