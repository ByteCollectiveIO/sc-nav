"""nav_core tests against the real poi/containers dataset.

Run: python3 test_nav_core.py
"""

import json
import math
import time
import unittest
from datetime import datetime, timedelta, timezone
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
        # Starmap POIs (ids well below the synthesized-station range) are the
        # data-integrity snapshot; container-stations are added on top.
        starmap = [p for p in NAV.pois.values() if p.id < nav_core.CONTAINER_POI_START]
        self.assertEqual(len(starmap), 1885)
        self.assertIn("Stanton", NAV.systems)

    def test_container_stations_synthesized(self):
        synth = [p for p in NAV.pois.values() if p.id >= nav_core.CONTAINER_POI_START]
        self.assertTrue(synth)
        # Lagrange stations are searchable by their L-code (folded into the name).
        self.assertTrue(any(p.name.startswith("Wide Forest Station") and "ARC-L1" in p.name
                            for p in synth))
        # all are directly-QT-able space POIs at the container's position
        self.assertTrue(all(p.container_name is None and p.global_m is not None and p.qt_marker
                            for p in synth))

    def test_space_pois_have_global_coords(self):
        space = [p for p in NAV.pois.values()
                 if p.container_name is None and p.id < nav_core.CONTAINER_POI_START]
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

    def test_survey_marks_split_out_of_nearest_pois(self):
        # Survey marks (#36) must not crowd the distance-capped NEARBY list:
        # they land in `nearest_survey`, never `nearest_pois`, so real POIs
        # (even a farther one) still surface.
        t = time.time()
        ref = surface_pois("Yela")[0]
        pos = poi_global_m(NAV, ref, t)
        nav2 = load_data(DATA_DIR)
        normal = nav_core.custom_poi_from_position(
            nav2, pos, t, "Cache Z", "Cave", 1000010)
        nav2.pois[normal.id] = normal
        for i in range(20):                        # a pocket's worth of marks
            sp = nav_core.custom_poi_from_position(
                nav2, pos, t, f"Survey {i}", "survey", 1000020 + i,
                survey={"rocks": "none", "ores": [], "salvage": False})
            nav2.pois[sp.id] = sp
        state = compute_state(nav2, pos, t)
        near_ids = [n["id"] for n in state["nearest_pois"]]
        surv_ids = [n["id"] for n in state["nearest_survey"]]
        self.assertIn(1000010, near_ids)                       # real POI survives
        self.assertFalse(any(1000020 <= i < 1000040 for i in near_ids))
        self.assertEqual(len(surv_ids), 10)                    # capped, own list
        self.assertTrue(all(1000020 <= i < 1000040 for i in surv_ids))

    def test_owner_round_trips(self):
        t = time.time()
        pos = poi_global_m(NAV, surface_pois("Daymar")[0], t)
        poi = nav_core.custom_poi_from_position(
            NAV, pos, t, "Owned", "Stash", 1000003, owner_id=7, owner_handle="Pilot7"
        )
        back = nav_core.poi_from_custom_dict(nav_core.custom_poi_to_dict(poi))
        self.assertEqual(back.owner_id, 7)
        self.assertEqual(back.owner_handle, "Pilot7")

    def test_qt_marker_custom_poi_round_trips(self):
        t = time.time()
        pos = poi_global_m(NAV, surface_pois("Daymar")[0], t)
        poi = nav_core.custom_poi_from_position(
            NAV, pos, t, "Daymar OM-3", "Orbital Marker", 1000004, qt_marker=True
        )
        self.assertTrue(poi.qt_marker)
        # it's its own nearest QT marker
        self.assertEqual(poi.nearest_qt, "Daymar OM-3")
        self.assertEqual(poi.nearest_qt_dist_m, 0.0)
        # qt_marker survives the dict round trip (default-False path stays False)
        back = nav_core.poi_from_custom_dict(nav_core.custom_poi_to_dict(poi))
        self.assertTrue(back.qt_marker)
        self.assertFalse(
            nav_core.poi_from_custom_dict({"id": 1, "name": "x"}).qt_marker
        )

    def test_custom_qt_marker_becomes_nearest_for_others(self):
        # A user-added QT marker should become the nearest-jump answer for a
        # nearby non-marker entity once the index is rebuilt.
        t = time.time()
        nav2 = load_data(DATA_DIR)
        ref = [p for p in nav2.pois.values()
               if p.container_name == "Daymar" and not p.qt_marker and p.local_km][0]
        pos = poi_global_m(nav2, ref, t)
        marker = nav_core.custom_poi_from_position(
            nav2, pos, t, "Test OM", "Orbital Marker", 1000005, qt_marker=True
        )
        nav2.pois[marker.id] = marker
        nav_core.assign_qt_markers(nav2)
        self.assertIn(marker, nav2.qt_markers)
        self.assertEqual(nav2.pois[ref.id].nearest_qt, "Test OM")


class QtIncrementalTests(unittest.TestCase):
    """Single-marker QT index maintenance (scaling deferral #3): the full
    assign_qt_markers rebuild scales with total entities and runs under
    hub.lock, so the common events use incremental paths — qt_marker_added
    (improvement pass) and qt_marker_removed (dependents-only re-resolve).
    The gold standard for both is EXACT parity with the full rebuild,
    including the same-body-beats-any-off-body tier preference."""

    @staticmethod
    def _snap(nav):
        return {("p", p.id): (p.nearest_qt, p.nearest_qt_dist_m)
                for p in nav.pois.values()} | \
               {("o", o.id): (o.nearest_qt, o.nearest_qt_dist_m)
                for o in nav.observations.values()}

    def _assert_parity(self, nav):
        got = self._snap(nav)
        nav_core.assign_qt_markers(nav)
        self.assertEqual(got, self._snap(nav))

    def test_added_on_body_marker_matches_full_rebuild(self):
        t = time.time()
        nav = load_data(DATA_DIR)
        nav_core.assign_qt_markers(nav)
        ref = [p for p in nav.pois.values()
               if p.container_name == "Daymar" and not p.qt_marker and p.local_km][0]
        pos = poi_global_m(nav, ref, t)
        marker = nav_core.custom_poi_from_position(
            nav, pos, t, "Inc OM", "Orbital Marker", 1000010, qt_marker=True)
        nav.pois[marker.id] = marker
        nav_core.qt_marker_added(nav, marker)
        self.assertTrue(any(p is marker for p in nav.qt_markers))
        self.assertEqual((marker.nearest_qt, marker.nearest_qt_dist_m),
                         ("Inc OM", 0.0))                    # self-assignment
        self.assertEqual(nav.pois[ref.id].nearest_qt, "Inc OM")
        self._assert_parity(nav)

    def test_added_deep_space_marker_matches_full_rebuild(self):
        nav = load_data(DATA_DIR)
        nav_core.assign_qt_markers(nav)
        marker = nav_core.custom_poi_from_position(
            nav, (30_000_000e3, 1.0e9, 0.0), time.time(),
            "Deep Beacon", "Custom", 1000011, qt_marker=True)
        nav.pois[marker.id] = marker
        nav_core.qt_marker_added(nav, marker)
        self._assert_parity(nav)

    def test_removed_marker_reassigns_only_dependents_to_parity(self):
        nav = load_data(DATA_DIR)
        nav_core.assign_qt_markers(nav)
        victim = max(nav.qt_markers, key=lambda m: sum(
            1 for p in nav.pois.values() if p.nearest_qt == m.name))
        self.assertTrue(any(p.nearest_qt == victim.name
                            for p in nav.pois.values()))     # has dependents
        victim.qt_marker = False
        nav_core.qt_marker_removed(nav, victim.name)
        self.assertFalse(any(p is victim for p in nav.qt_markers))
        self.assertFalse(any(p.nearest_qt == victim.name
                             for p in nav.pois.values()))   # nobody points at it
        self._assert_parity(nav)

    def test_private_flip_round_trip_matches_full_rebuild(self):
        t = time.time()
        nav = load_data(DATA_DIR)
        nav_core.assign_qt_markers(nav)
        ref = [p for p in nav.pois.values()
               if p.container_name == "Daymar" and not p.qt_marker and p.local_km][0]
        marker = nav_core.custom_poi_from_position(
            nav, poi_global_m(nav, ref, t), t, "Flip OM", "Orbital Marker",
            1000012, qt_marker=True)
        nav.pois[marker.id] = marker
        nav_core.qt_marker_added(nav, marker)
        # goes private: leaves the shared index, dependents re-resolve
        marker.private = True
        nav_core.qt_marker_removed(nav, marker.name)
        self._assert_parity(nav)
        # back to public: the improvement pass restores it
        marker.private = False
        nav_core.qt_marker_added(nav, marker)
        self.assertEqual(nav.pois[ref.id].nearest_qt, "Flip OM")
        self._assert_parity(nav)

    def test_added_ignores_private_or_non_marker(self):
        nav = load_data(DATA_DIR)
        nav_core.assign_qt_markers(nav)
        before = self._snap(nav)
        plain = nav_core.custom_poi_from_position(
            nav, (1.0e9, 1.0e9, 0.0), time.time(), "Not a marker", "Custom",
            1000013, qt_marker=False)
        nav.pois[plain.id] = plain
        nav_core.qt_marker_added(nav, plain)          # must be a no-op
        self.assertFalse(any(p is plain for p in nav.qt_markers))
        after = {k: v for k, v in self._snap(nav).items() if k != ("p", plain.id)}
        self.assertEqual(before, after)


class PrivatePoiTests(unittest.TestCase):
    def _private_poi(self, nav, t, owner_id=7):
        ref = [p for p in nav.pois.values()
               if p.container_name == "Daymar" and p.local_km][0]
        pos = poi_global_m(nav, ref, t)
        poi = nav_core.custom_poi_from_position(
            nav, pos, t, "Secret Spot", "Stash", nav_core.CUSTOM_ID_START,
            owner_id=owner_id, owner_handle="Pilot7", private=True,
        )
        nav.pois[poi.id] = poi
        return poi, pos

    def test_private_flag_round_trips(self):
        t = time.time()
        nav2 = load_data(DATA_DIR)
        poi, _ = self._private_poi(nav2, t)
        self.assertTrue(poi.private)
        back = nav_core.poi_from_custom_dict(nav_core.custom_poi_to_dict(poi))
        self.assertTrue(back.private)
        # default path stays shared
        self.assertFalse(nav_core.poi_from_custom_dict({"id": 1, "name": "x"}).private)

    def test_visible_only_to_owner(self):
        t = time.time()
        nav2 = load_data(DATA_DIR)
        poi, _ = self._private_poi(nav2, t, owner_id=7)
        shared = poi  # alias for readability
        self.assertTrue(nav_core.poi_visible_to(shared, frozenset({7})))
        self.assertFalse(nav_core.poi_visible_to(shared, frozenset({99})))
        self.assertFalse(nav_core.poi_visible_to(shared, frozenset()))

    def test_search_hides_private_from_others(self):
        t = time.time()
        nav2 = load_data(DATA_DIR)
        self._private_poi(nav2, t, owner_id=7)
        # owner sees it
        owner_hits = [r["id"] for r in
                      search_pois(nav2, query="secret", viewer_owner_ids=frozenset({7}))]
        self.assertIn(nav_core.CUSTOM_ID_START, owner_hits)
        # everyone else does not
        other_hits = [r["id"] for r in
                      search_pois(nav2, query="secret", viewer_owner_ids=frozenset({99}))]
        self.assertNotIn(nav_core.CUSTOM_ID_START, other_hits)
        anon_hits = [r["id"] for r in search_pois(nav2, query="secret")]
        self.assertNotIn(nav_core.CUSTOM_ID_START, anon_hits)

    def test_compute_state_hides_private_from_others(self):
        t = time.time()
        nav2 = load_data(DATA_DIR)
        poi, pos = self._private_poi(nav2, t, owner_id=7)
        owner_ids = [n["id"] for n in
                     compute_state(nav2, pos, t, viewer_owner_ids=frozenset({7}))["nearest_pois"]]
        self.assertIn(poi.id, owner_ids)
        other_ids = [n["id"] for n in
                     compute_state(nav2, pos, t, viewer_owner_ids=frozenset({99}))["nearest_pois"]]
        self.assertNotIn(poi.id, other_ids)

    def test_compute_state_blocks_private_destination(self):
        t = time.time()
        nav2 = load_data(DATA_DIR)
        poi, pos = self._private_poi(nav2, t, owner_id=7)
        # a non-owner can't resolve the private POI as a destination
        state = compute_state(nav2, pos, t, destination_id=poi.id,
                              viewer_owner_ids=frozenset({99}))
        self.assertIsNone(state["destination"])
        # the owner can
        state = compute_state(nav2, pos, t, destination_id=poi.id,
                              viewer_owner_ids=frozenset({7}))
        self.assertIsNotNone(state["destination"])

    def test_private_qt_marker_excluded_from_index(self):
        # A private POI marked qt_marker must never enter the shared QT index,
        # or its name/location would leak via other entities' nearest_qt.
        t = time.time()
        nav2 = load_data(DATA_DIR)
        ref = [p for p in nav2.pois.values()
               if p.container_name == "Daymar" and not p.qt_marker and p.local_km][0]
        pos = poi_global_m(nav2, ref, t)
        marker = nav_core.custom_poi_from_position(
            nav2, pos, t, "Hidden OM", "Orbital Marker", nav_core.CUSTOM_ID_START,
            qt_marker=True, private=True, owner_id=7,
        )
        nav2.pois[marker.id] = marker
        nav_core.assign_qt_markers(nav2)
        self.assertNotIn(marker, nav2.qt_markers)
        self.assertNotEqual(nav2.pois[ref.id].nearest_qt, "Hidden OM")


def _obs(nav, ref_name, t, category, data, obs_id, **kw):
    ref = [p for p in nav.pois.values()
           if p.system == "Stanton" and p.container_name == ref_name][0]
    pos = poi_global_m(nav, ref, t)
    return ref, pos, nav_core.observation_from_position(nav, pos, t, category, data, obs_id, **kw)


class ObservationTests(unittest.TestCase):
    def test_quality_mapping(self):
        cases = {1: "Lowest", 2: "Low to Mid", 4: "Low to Mid",
                 5: "Good / High", 6: "Good / High", 7: "Very High", 8: "Perfect"}
        for band, label in cases.items():
            self.assertEqual(nav_core.quality_for_band(band), label)
        self.assertEqual(nav_core.quality_for_band(0), "Lowest")
        self.assertEqual(nav_core.quality_for_band(99), "Perfect")

    def test_resource_capture_geometry_and_derived_quality(self):
        t = time.time()
        ref, pos, obs = _obs(NAV, "Daymar", t, "resource", {"ore": "Quantanium", "band": 7},
                             nav_core.OBSERVATION_ID_START, biome="Desert", owner_handle="Miner3")
        self.assertEqual(obs.category, "resource")
        self.assertEqual(obs.data["quality"], "Very High")   # derived from band
        self.assertEqual(obs.container_name, "Daymar")
        self.assertIsNotNone(obs.height_m)                   # altitude auto-recorded
        self.assertAlmostEqual(obs.latitude, ref.latitude, places=4)
        g = poi_global_m(NAV, obs, t + 9999)
        ref_g = poi_global_m(NAV, ref, t + 9999)
        self.assertLess(nav_core.dist3(g, ref_g), 1.0)

    def test_unknown_band_yields_unk_quality(self):
        # Band is unknown until mined: "Unk"/None/garbage -> band None, quality "Unk".
        t = time.time()
        for raw in ("Unk", None, "", "xyz"):
            _r, _p, obs = _obs(NAV, "Daymar", t, "resource", {"ore": "Gold", "band": raw},
                               nav_core.OBSERVATION_ID_START + 50)
            self.assertIsNone(obs.data["band"], raw)
            self.assertEqual(obs.data["quality"], "Unk", raw)
        base = nav_core._observation_base(obs)
        self.assertEqual(base["name"], "Gold (B?)")          # display handles None band
        # a real band still derives normally
        _r, _p, good = _obs(NAV, "Daymar", t, "resource", {"ore": "Gold", "band": 7},
                            nav_core.OBSERVATION_ID_START + 51)
        self.assertEqual(good.data["band"], 7)
        self.assertEqual(good.data["quality"], "Very High")

    def test_wildlife_has_no_quality(self):
        t = time.time()
        _ref, _pos, obs = _obs(NAV, "Daymar", t, "wildlife", {"species": "Kopion"},
                               nav_core.OBSERVATION_ID_START + 5, biome="Desert")
        self.assertEqual(obs.category, "wildlife")
        self.assertEqual(obs.data, {"species": "Kopion"})
        self.assertNotIn("quality", obs.data)
        self.assertNotIn("band", obs.data)
        base = nav_core._observation_base(obs)
        self.assertEqual(base["kind"], "wildlife")
        self.assertEqual(base["name"], "Kopion")
        self.assertEqual(base["species"], "Kopion")

    def test_shard_id_flows_through_capture_and_serialization(self):
        t = time.time()
        _r, _p, obs = _obs(NAV, "Daymar", t, "resource", {"ore": "Gold", "band": 3},
                           nav_core.OBSERVATION_ID_START + 60,
                           shard_id="pub_use1b_12030094_130")
        self.assertEqual(obs.shard_id, "pub_use1b_12030094_130")
        # Survives the persistence round-trip and reaches the UI-facing dict.
        self.assertEqual(nav_core.observation_to_dict(obs)["shard_id"],
                         "pub_use1b_12030094_130")
        back = nav_core.observation_from_dict(nav_core.observation_to_dict(obs))
        self.assertEqual(back.shard_id, "pub_use1b_12030094_130")
        self.assertEqual(nav_core._observation_base(obs)["shard_id"],
                         "pub_use1b_12030094_130")
        # Legacy/untagged capture stays None (the "unknown shard" case).
        _r, _p, legacy = _obs(NAV, "Daymar", t, "resource", {"ore": "Gold", "band": 3},
                              nav_core.OBSERVATION_ID_START + 61)
        self.assertIsNone(legacy.shard_id)
        self.assertIsNone(nav_core._observation_base(legacy)["shard_id"])

    def test_dict_round_trip_both_categories(self):
        t = time.time()
        for i, (cat, data) in enumerate([("resource", {"ore": "Bexalite", "band": 5}),
                                         ("wildlife", {"species": "Marok"})]):
            _r, _p, obs = _obs(NAV, "Yela", t, cat, data, nav_core.OBSERVATION_ID_START + 10 + i)
            back = nav_core.observation_from_dict(nav_core.observation_to_dict(obs))
            self.assertEqual(back, obs)

    def test_categories_kept_separate_in_state(self):
        t = time.time()
        nav2 = load_data(DATA_DIR)
        ref, pos, r = _obs(nav2, "Yela", t, "resource", {"ore": "Gold", "band": 6}, 2000020)
        _r2, _p2, w = _obs(nav2, "Yela", t, "wildlife", {"species": "Kopion"}, 2000021)
        nav2.observations[r.id] = r
        nav2.observations[w.id] = w
        state = compute_state(nav2, pos, t)
        kinds = {o["kind"] for o in state["nearest_observations"]}
        ids = {o["id"] for o in state["nearest_observations"]}
        self.assertEqual(kinds, {"resource", "wildlife"})
        self.assertTrue({2000020, 2000021} <= ids)
        # observations never leak into the POI bucket
        self.assertFalse(ids & {p["id"] for p in state["nearest_pois"]})

    def test_observation_as_destination(self):
        t = time.time()
        nav2 = load_data(DATA_DIR)
        ref, pos, w = _obs(nav2, "Daymar", t, "wildlife", {"species": "Valakkar"}, 2000030)
        nav2.observations[w.id] = w
        away = (pos[0] + 5000.0, pos[1], pos[2])
        state = compute_state(nav2, away, t, destination_id=w.id,
                              prev_pos=(away[0] + 5000, away[1], away[2]), prev_t=t - 10)
        self.assertEqual(state["destination"]["kind"], "wildlife")
        self.assertEqual(state["destination"]["name"], "Valakkar")
        self.assertIsNotNone(state["destination"]["distance_m"])

    def test_search_filters_by_category(self):
        nav2 = load_data(DATA_DIR)
        t = time.time()
        _r, _p, r = _obs(nav2, "Daymar", t, "resource", {"ore": "Titanium", "band": 4}, 2000040)
        _r2, _p2, w = _obs(nav2, "Daymar", t, "wildlife", {"species": "Kopion"}, 2000041)
        nav2.observations.update({r.id: r, w.id: w})
        res = nav_core.search_observations(nav2, category="resource")
        self.assertTrue(all(o["kind"] == "resource" for o in res))
        wild = nav_core.search_observations(nav2, category="wildlife", type_value="Kopion")
        self.assertEqual([o["id"] for o in wild], [2000041])


    def test_legacy_flat_resource_record_preserves_fields(self):
        # Pre-generalization resource_nodes.json stored ore/band/quality at the
        # top level with no "data" key — these must survive a load.
        t = time.time()
        nav2 = load_data(DATA_DIR)
        ref = [p for p in nav2.pois.values()
               if p.system == "Stanton" and p.container_name == "Daymar"][0]
        pos = poi_global_m(nav2, ref, t)
        legacy = {
            "id": 2000099, "ore": "Taranite", "band": 6, "quality": "Good / High",
            "system": "Stanton", "container": "Daymar",
            "local_km": list(nav_core.global_to_local_km(nav2.container_of(ref), pos, t)),
            "global_m": None, "latitude": ref.latitude, "longitude": ref.longitude,
            "height_m": 100.0, "biome": "Desert", "note": "old",
            "owner_id": 2, "owner_handle": "Legacy", "observed_at": "2026-01-01T00:00:00+00:00",
            # NOTE: no "category", no "data"
        }
        nav_core.merge_observations(nav2, [legacy], "resource")
        obs = nav2.observations[2000099]
        self.assertEqual(obs.category, "resource")
        self.assertEqual(obs.data["ore"], "Taranite")   # not "Unknown"
        self.assertEqual(obs.data["band"], 6)            # not 1
        self.assertEqual(obs.data["quality"], "Good / High")
        self.assertEqual(obs.owner_handle, "Legacy")


    def test_merge_skips_bad_records_without_aborting(self):
        # One malformed/unknown-category record must not stop the rest loading.
        nav2 = load_data(DATA_DIR)
        t = time.time()
        _r, _p, good = _obs(nav2, "Daymar", t, "resource", {"ore": "Gold", "band": 5}, 2000200)
        good_d = nav_core.observation_to_dict(good)
        bad_category = {"id": 2000201, "category": "made_up", "data": {}}
        bad_missing_id = {"category": "resource", "data": {"ore": "X", "band": 1}}
        nav_core.merge_observations(nav2, [bad_category, good_d, bad_missing_id])
        self.assertIn(2000200, nav2.observations)          # good one loaded
        self.assertNotIn(2000201, nav2.observations)        # unknown category skipped
        # custom POIs likewise tolerate a junk record
        before = len(nav2.pois)
        nav_core.merge_custom_pois(nav2, [{"not": "a poi"}])
        self.assertEqual(len(nav2.pois), before)

    def test_observation_base_data_cannot_clobber_canonical_fields(self):
        # A data dict carrying a reserved key must not override the real field.
        t = time.time()
        nav2 = load_data(DATA_DIR)
        _r, _p, obs = _obs(nav2, "Daymar", t, "wildlife", {"species": "Kopion"}, 2000210)
        obs.data["container"] = "EVIL"      # hostile/legacy data key
        obs.data["kind"] = "poi"
        base = nav_core._observation_base(obs)
        self.assertEqual(base["kind"], "wildlife")          # canonical wins
        self.assertEqual(base["container"], "Daymar")


class NearestQtTests(unittest.TestCase):
    def test_only_qtmarker_1_counts(self):
        # QTMarker is 1 / -1 / 0 / null; only 1 is a real jump marker.
        raw = [
            {"item_id": 1, "PoiName": "Active", "System": "Stanton", "Planet": "Daymar",
             "Type": "Outpost", "XCoord": 1, "YCoord": 2, "ZCoord": 3, "QTMarker": 1},
            {"item_id": 2, "PoiName": "Minus", "System": "Stanton", "Planet": "Daymar",
             "Type": "Outpost", "XCoord": 1, "YCoord": 2, "ZCoord": 3, "QTMarker": -1},
            {"item_id": 3, "PoiName": "Null", "System": "Stanton", "Planet": "Daymar",
             "Type": "Outpost", "XCoord": 1, "YCoord": 2, "ZCoord": 3, "QTMarker": None},
            {"item_id": 4, "PoiName": "Zero", "System": "Stanton", "Planet": "Daymar",
             "Type": "Outpost", "XCoord": 1, "YCoord": 2, "ZCoord": 3, "QTMarker": 0},
        ]
        nav2 = nav_core.parse_data([], raw)
        self.assertTrue(nav2.pois[1].qt_marker)
        for i in (2, 3, 4):
            self.assertFalse(nav2.pois[i].qt_marker, f"item {i} QTMarker should not count")

    def test_landing_zone_type_counts_but_only_exact(self):
        # Type == "Landing Zone" (exact) is a QT marker even if QTMarker isn't 1
        # (e.g. Area18). "Landing Zone3" / "Landing Zone 3" must NOT match.
        def poi(i, t, qt):
            return {"item_id": i, "PoiName": f"p{i}", "System": "Stanton", "Planet": "X",
                    "Type": t, "XCoord": 0, "YCoord": 0, "ZCoord": 0, "QTMarker": qt}
        nav2 = nav_core.parse_data([], [
            poi(1, "Landing Zone", 0),       # Area18 case -> marker
            poi(2, "Landing Zone3", 0),      # near-miss -> NOT a marker
            poi(3, "Landing Zone 3", 0),     # near-miss -> NOT a marker
            poi(4, "  Landing Zone  ", 0),   # whitespace only -> marker
            poi(5, "Outpost", 1),            # QTMarker path still works
        ])
        self.assertTrue(nav2.pois[1].qt_marker)
        self.assertFalse(nav2.pois[2].qt_marker)
        self.assertFalse(nav2.pois[3].qt_marker)
        self.assertTrue(nav2.pois[4].qt_marker)
        self.assertTrue(nav2.pois[5].qt_marker)

    def test_qt_marker_poi_is_its_own_nearest(self):
        nav2 = load_data(DATA_DIR)
        nav_core.assign_qt_markers(nav2)
        kudre = next(p for p in nav2.pois.values()
                     if p.container_name == "Daymar" and p.name == "Kudre Ore")
        self.assertTrue(kudre.qt_marker)
        self.assertEqual(kudre.nearest_qt, "Kudre Ore")

    def test_non_qt_poi_gets_a_same_body_marker(self):
        nav2 = load_data(DATA_DIR)
        nav_core.assign_qt_markers(nav2)
        # a Daymar POI that is NOT itself a QT marker
        p = next(p for p in nav2.pois.values()
                 if p.container_name == "Daymar" and not p.qt_marker and p.local_km)
        self.assertIsNotNone(p.nearest_qt)
        # the assigned marker is a real QT marker on the same body
        target = next(q for q in nav2.pois.values()
                      if q.container_name == "Daymar" and q.name == p.nearest_qt)
        self.assertTrue(target.qt_marker)

    def test_observation_gets_nearest_qt(self):
        nav2 = load_data(DATA_DIR)
        nav_core.assign_qt_markers(nav2)            # build the index first
        t = time.time()
        ref = next(p for p in nav2.pois.values()
                   if p.container_name == "Daymar" and p.name == "Kudre Ore")
        pos = poi_global_m(nav2, ref, t)
        obs = nav_core.observation_from_position(nav2, pos, t, "resource",
                                                 {"ore": "Gold", "band": 4}, 2000300)
        # captured right at Kudre Ore (a QT marker) -> that's the nearest, ~0 m
        self.assertEqual(obs.nearest_qt, "Kudre Ore")
        self.assertLess(obs.nearest_qt_dist_m, 1.0)
        self.assertEqual(nav_core._observation_base(obs)["nearest_qt"], "Kudre Ore")

    def test_nearest_qt_distance(self):
        nav2 = load_data(DATA_DIR)
        nav_core.assign_qt_markers(nav2)
        # a QT marker itself: distance 0
        kudre = next(p for p in nav2.pois.values()
                     if p.container_name == "Daymar" and p.name == "Kudre Ore")
        self.assertEqual(kudre.nearest_qt_dist_m, 0.0)
        # a non-QT Daymar POI: positive distance in meters, and it equals the
        # local-frame distance to the assigned marker.
        p = next(p for p in nav2.pois.values()
                 if p.container_name == "Daymar" and not p.qt_marker and p.local_km)
        marker = next(q for q in nav2.pois.values()
                      if q.container_name == "Daymar" and q.name == p.nearest_qt)
        self.assertGreater(p.nearest_qt_dist_m, 0.0)
        expected_m = nav_core.dist3(p.local_km, marker.local_km) * 1000.0
        self.assertAlmostEqual(p.nearest_qt_dist_m, expected_m, places=3)


class BreadcrumbHelperTests(unittest.TestCase):
    def test_surface_distance_matches_great_circle(self):
        # 1 degree of latitude on a 1000 km radius body
        d = nav_core.surface_distance_m(0, 0, 1, 0, 1_000_000)
        self.assertAlmostEqual(d, math.radians(1) * 1_000_000, places=3)
        # zero move -> ~zero distance (well under the 250 m gate; acos rounding
        # leaves a sub-meter residue, never exactly 0.0)
        self.assertLess(nav_core.surface_distance_m(10, 20, 10, 20, 1_000_000), 1.0)


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
        # If an id somehow exists in BOTH pois and observations, the summarizer
        # must follow the resolved entity (pois wins) and not crash.
        nav2 = load_data(DATA_DIR)
        t = time.time()
        ref = [p for p in nav2.pois.values()
               if p.system == "Stanton" and p.container_name == "Daymar"][0]
        pos = poi_global_m(nav2, ref, t)
        shared_id = 1234567
        poi = nav_core.custom_poi_from_position(nav2, pos, t, "Dup", "Stash", shared_id)
        obs = nav_core.observation_from_position(nav2, pos, t, "resource", {"ore": "Gold", "band": 3}, shared_id)
        nav2.pois[shared_id] = poi
        nav2.observations[shared_id] = obs
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


class PoiOverrideTests(unittest.TestCase):
    """Admin quality-control overrides: flag bad + force QT, keyed by name-key
    so they survive re-imports (nav_core.apply_poi_overrides)."""

    def _space(self, pid, name, qt, gx):
        return nav_core.Poi(
            id=pid, name=name, system="Stanton", container_name=None,
            type="Station", local_km=None, global_m=(gx, 0.0, 0.0),
            latitude=None, longitude=None, height_m=None, qt_marker=qt)

    def _nav(self):
        nav = nav_core.NavData()
        nav.pois[10] = self._space(10, "Port Olisar", True, 1e6)
        nav.pois[11] = self._space(11, "Grim HEX", False, 2e6)
        # sibling of #10 with the same order-insensitive name-key (collision case)
        nav.pois[12] = self._space(12, "Olisar Port", True, 3e6)
        return nav

    def _ov(self, poi, bad=False, qt=None):
        return {"key": nav_core.poi_override_key(poi), "bad": bad, "qt_override": qt}

    def test_key_is_order_insensitive(self):
        nav = self._nav()
        self.assertEqual(nav_core.poi_override_key(nav.pois[10]),
                         nav_core.poi_override_key(nav.pois[12]))

    def test_bad_excludes_from_routing_and_search(self):
        nav = self._nav()
        nav_core.apply_poi_overrides(nav, [self._ov(nav.pois[10], bad=True)])
        nav_core.assign_qt_markers(nav)
        # Both siblings sharing the key are disabled (collision applies to all).
        self.assertFalse(nav_core.poi_active(nav.pois[10]))
        self.assertFalse(nav_core.poi_active(nav.pois[12]))
        self.assertNotIn(nav.pois[10], nav.qt_markers)
        self.assertNotIn(nav.pois[12], nav.qt_markers)
        names = [r["name"] for r in search_pois(nav, query="olisar")]
        self.assertEqual(names, [])

    def test_qt_force_on_and_off(self):
        nav = self._nav()
        # Force the non-marker on, force a marker off.
        nav_core.apply_poi_overrides(nav, [
            self._ov(nav.pois[11], qt=1),
            self._ov(nav.pois[10], qt=0),
        ])
        nav_core.assign_qt_markers(nav)
        self.assertTrue(nav.pois[11].qt_marker)
        self.assertIn(nav.pois[11], nav.qt_markers)
        self.assertFalse(nav.pois[10].qt_marker)
        self.assertNotIn(nav.pois[10], nav.qt_markers)

    def test_clear_restores_imported_qt(self):
        nav = self._nav()
        nav_core.apply_poi_overrides(nav, [self._ov(nav.pois[10], qt=0)])
        self.assertFalse(nav.pois[10].qt_marker)
        # Removing the override reverts to the imported value with no reload.
        nav_core.apply_poi_overrides(nav, [])
        self.assertTrue(nav.pois[10].qt_marker)
        self.assertTrue(nav_core.poi_active(nav.pois[10]))

    def test_survives_reimport(self):
        # A fresh import builds new Poi objects; overrides re-apply by key.
        overrides = [self._ov(self._space(10, "Port Olisar", True, 1e6), bad=True)]
        fresh = self._nav()
        nav_core.apply_poi_overrides(fresh, overrides)
        nav_core.assign_qt_markers(fresh)
        self.assertFalse(nav_core.poi_active(fresh.pois[10]))

    def test_compute_state_nulls_bad_destination(self):
        t = time.time()
        pois = surface_pois("Daymar")
        here, dest = pois[0], pois[1]
        pos = poi_global_m(NAV, here, t)
        # Baseline: destination resolves normally.
        base = compute_state(NAV, pos, t, destination_id=dest.id)
        self.assertIsNotNone(base["destination"])
        try:
            dest.disabled = True
            state = compute_state(NAV, pos, t, destination_id=dest.id)
            self.assertIsNone(state["destination"])
        finally:
            dest.disabled = False


class ResourceStatsTests(unittest.TestCase):
    R = 295_000.0

    def _body_nav(self):
        nav = nav_core.NavData()
        nav.containers[("Stanton", "Yela")] = nav_core.Container(
            name="Yela", system="Stanton", type="Moon", internal_name="",
            pos=(0, 0, 0), body_radius=self.R, om_radius=0, grid_radius=0,
            rotation_speed=0, rotation_adjustment=0,
        )
        self._oid = nav_core.OBSERVATION_ID_START
        return nav

    def _add(self, nav, lat, lon, ore, body="Yela", band=None):
        nav.observations[self._oid] = nav_core.Observation(
            id=self._oid, category="resource", system="Stanton",
            container_name=body, local_km=None, global_m=None,
            latitude=lat, longitude=lon, height_m=0.0, biome=None, note=None,
            owner_id=None, owner_handle=None, observed_at="2026-01-01",
            data={"ore": ore, "band": band},
        )
        self._oid += 1

    def test_grid_cell_center_round_trips(self):
        for lat, lon in [(0, 0), (45, 90), (-60, -150), (88, 179), (-88, -179)]:
            i, j = nav_core.grid_cell(lat, lon, self.R)
            clat, clon = nav_core.grid_cell_center(i, j, self.R)
            self.assertEqual((i, j), nav_core.grid_cell(clat, clon, self.R))

    def test_equal_area_cells(self):
        # Equal-area: a cell near the pole and one at the equator cover ~same m².
        # Cell count is uniform in (lon, sin lat), so this is structural — assert
        # the dims are the documented equal-area dimensions.
        n_lon, n_lat = nav_core.grid_dims(self.R)
        self.assertEqual(n_lon, round(2 * math.pi * self.R / nav_core.RESOURCE_CELL_M))
        self.assertEqual(n_lat, round(2 * self.R / nav_core.RESOURCE_CELL_M))

    def test_forecast_none_without_data(self):
        nav = self._body_nav()
        self.assertIsNone(nav_core.resource_forecast(nav, "Stanton", "Yela", 0, 0, self.R))

    def test_local_cluster_sharpens_above_base_rate(self):
        nav = self._body_nav()
        for _ in range(15):
            self._add(nav, 10.0, 20.0, "Quantanium")
        for _ in range(3):
            self._add(nav, 10.02, 20.02, "Bexalite")
        for _ in range(6):                      # far cluster, pulls the base rate down
            self._add(nav, -40.0, 100.0, "Bexalite")
        base, base_n = nav_core.body_base_rate(nav, "Stanton", "Yela")
        self.assertEqual(base_n, 24)
        fc = nav_core.resource_forecast(nav, "Stanton", "Yela", 10.0, 20.0, self.R)
        top = fc["ranked"][0]
        self.assertEqual(top["ore"], "Quantanium")
        self.assertEqual(fc["n_local"], 18)     # the far cluster is out of the neighborhood
        # Local evidence pushes Quantanium above its body-wide share.
        self.assertGreater(top["p"], base["Quantanium"])

    def test_empty_neighborhood_falls_back_to_base_rate(self):
        nav = self._body_nav()
        for _ in range(10):
            self._add(nav, 10.0, 20.0, "Quantanium")
        fc = nav_core.resource_forecast(nav, "Stanton", "Yela", -70.0, -120.0, self.R)
        self.assertEqual(fc["n_local"], 0)
        self.assertAlmostEqual(fc["ranked"][0]["p"], 1.0, places=6)   # only ore seen on body

    def test_cells_only_for_visited_areas(self):
        nav = self._body_nav()
        for _ in range(4):
            self._add(nav, 10.0, 20.0, "Quantanium")
        self._add(nav, -40.0, 100.0, "Bexalite")
        cells = nav_core.resource_cells(nav, "Stanton", "Yela", self.R)
        self.assertEqual(len(cells), 2)
        comp_sums = [round(sum(c["comp"].values()), 6) for c in cells]
        self.assertTrue(all(s == 1.0 for s in comp_sums))   # each cell is a distribution
        self.assertEqual({c["top"] for c in cells}, {"Quantanium", "Bexalite"})

    def test_local_km_latlon_round_trips(self):
        for lat, lon in [(0, 0), (30, -45), (-72, 120)]:
            x, y, z = nav_core.local_km_from_latlon(lat, lon, self.R)
            blat, blon, _ = nav_core.latlon_from_local((x, y, z))
            self.assertAlmostEqual(blat, lat, places=4)
            self.assertAlmostEqual(blon, lon, places=4)

    def test_ore_names_listed(self):
        nav = self._body_nav()
        self._add(nav, 0, 0, "Quantanium")
        self._add(nav, 1, 1, "Bexalite")
        self.assertEqual(nav_core.resource_ore_names(nav), ["Bexalite", "Quantanium"])

    def test_hotspots_rank_by_confidence_not_raw_rate(self):
        # 8/10 (well sampled) should outrank 3/3 (lucky), despite the lower rate.
        nav = self._body_nav()
        nav.containers[("Stanton", "Daymar")] = nav_core.Container(
            name="Daymar", system="Stanton", type="Moon", internal_name="",
            pos=(1e9, 0, 0), body_radius=self.R, om_radius=0, grid_radius=0,
            rotation_speed=0, rotation_adjustment=0,
        )
        for _ in range(8):
            self._add(nav, 10, 20, "Quantanium", body="Daymar", band=7)
        for _ in range(2):
            self._add(nav, 10, 20, "Bexalite", body="Daymar")
        for _ in range(3):
            self._add(nav, -30, 80, "Quantanium", body="Yela", band=4)
        hs = nav_core.resource_hotspots(nav, "Quantanium")
        self.assertEqual(hs[0]["body"], "Daymar")     # 8/10 beats 3/3
        self.assertEqual(hs[1]["body"], "Yela")
        self.assertAlmostEqual(hs[0]["p"], 0.8)
        self.assertEqual(hs[0]["avg_band"], 7.0)
        self.assertGreater(hs[0]["score"], hs[1]["score"])

    def test_moon_two_hop_travel_from_outside_its_system(self):
        nav = nav_core.NavData()

        def cont(name, internal, pos):
            return nav_core.Container(
                name=name, system="Stanton", type="Planet", internal_name=internal,
                pos=pos, body_radius=200_000, om_radius=0, grid_radius=0,
                rotation_speed=0, rotation_adjustment=0,
            )
        nav.containers[("Stanton", "Crusader")] = cont("Crusader", "Stanton2", (0, 0, 0))
        nav.containers[("Stanton", "Daymar")] = cont("Daymar", "Stanton2b", (1e6, 0, 0))
        nav.containers[("Stanton", "ArcCorp")] = cont("ArcCorp", "Stanton3", (5e10, 0, 0))
        nav.containers[("Stanton", "Wala")] = cont("Wala", "Stanton3b", (5e10 + 1e6, 0, 0))
        self.assertEqual(nav_core.parent_planet(nav, nav.containers[("Stanton", "Daymar")]).name, "Crusader")

        self._oid = nav_core.OBSERVATION_ID_START
        for _ in range(4):
            self._add(nav, 10, 20, "Quantanium", body="Daymar", band=7)

        # From ArcCorp's neighborhood you can't jump straight to Daymar (a
        # Crusader moon) — it routes via Crusader and costs more.
        far = nav_core.resource_hotspots(nav, "Quantanium", from_pos=(5e10, 0, 0), sort="near")[0]
        self.assertEqual(far["via"], "Crusader")
        # Standing at Crusader, the jump is direct.
        near = nav_core.resource_hotspots(nav, "Quantanium", from_pos=(0, 0, 0), sort="near")[0]
        self.assertIsNone(near["via"])
        self.assertGreater(far["travel_m"], near["travel_m"])

    def test_hotspot_carries_nearest_qt_marker(self):
        nav = self._body_nav()
        # a jumpable QT marker on the body, near the ore cluster
        nav.pois[1] = nav_core.Poi(
            id=1, name="Yela OM-1", system="Stanton", container_name="Yela",
            type="Outpost", local_km=nav_core.local_km_from_latlon(10, 20, self.R),
            global_m=None, latitude=10, longitude=20, height_m=None, qt_marker=True,
        )
        nav_core.index_qt_markers(nav)
        for _ in range(4):
            self._add(nav, 10, 20, "Quantanium")
        hs = nav_core.resource_hotspots(nav, "Quantanium")
        self.assertEqual(hs[0]["nearest_qt"], "Yela OM-1")
        self.assertIsNotNone(hs[0]["nearest_qt_dist_m"])


def _line_nav(coords):
    """A toy system of directly-QT-able space POIs at the given xyz coords, so
    travel_cost reduces to straight-line distance (no via-hops, no gates)."""
    nav = nav_core.NavData()
    nav.systems = ["Test"]
    for i, (x, y, z) in enumerate(coords):
        nav.pois[i] = nav_core.Poi(
            id=i, name=f"P{i}", system="Test", container_name=None,
            type="Orbital Station", local_km=None, global_m=(float(x), float(y), float(z)),
            latitude=None, longitude=None, height_m=None, qt_marker=True,
        )
    nav_core.index_qt_markers(nav)
    return nav


class TravelCostTests(unittest.TestCase):
    def test_intra_system_straight_line(self):
        nav = _line_nav([(0, 0, 0), (10, 0, 0)])
        leg = nav_core.travel_cost(nav, nav.pois[0], nav.pois[1])
        self.assertFalse(leg["cross_system"])
        self.assertAlmostEqual(leg["distance_m"], 10.0)
        self.assertEqual(leg["qt_marker"], "P1")

    def test_moon_two_hop_via_parent(self):
        # A surface POI on a moon, reached from far away, routes via its planet.
        port = next(p for p in NAV.pois.values()
                    if p.name == "Port Tressler" and p.system == "Stanton")
        daymar = next(p for p in NAV.pois.values()
                      if p.system == "Stanton" and p.container_name == "Daymar")
        leg = nav_core.travel_cost(NAV, port, daymar)
        self.assertEqual(leg["via"], "Crusader")
        self.assertFalse(leg["cross_system"])

    def test_system_path_chains_through_pyro(self):
        self.assertEqual(nav_core.system_path("Stanton", "Pyro"), ["Stanton", "Pyro"])
        self.assertEqual(nav_core.system_path("Stanton", "Nyx"),
                         ["Stanton", "Pyro", "Nyx"])
        self.assertEqual(nav_core.system_path("Stanton", "Stanton"), ["Stanton"])

    def test_cross_system_leg(self):
        area = next(p for p in NAV.pois.values()
                    if p.name == "Area18" and p.system == "Stanton")
        orb = next(p for p in NAV.pois.values()
                   if p.name == "Orbituary" and p.system == "Pyro")
        leg = nav_core.travel_cost(NAV, area, orb)
        self.assertTrue(leg["cross_system"])
        self.assertEqual(leg["via_gate"], ["Stanton", "Pyro"])
        self.assertGreater(leg["distance_m"], 0)


class PlanRouteTests(unittest.TestCase):
    def _coords(self):
        return [(0, 0, 0), (10, 0, 0), (20, 0, 0), (30, 0, 0)]

    def test_empty_is_trivially_feasible(self):
        res = nav_core.plan_route(_line_nav(self._coords()), [], usable_scu=100)
        self.assertTrue(res["summary"]["feasible"])
        self.assertEqual(res["stops"], [])

    def test_merges_shared_pickup_into_one_stop(self):
        nav = _line_nav(self._coords())
        pkgs = [{"id": "A", "commodity": "x", "scu": 10, "from_id": 0, "to_id": 2},
                {"id": "B", "commodity": "y", "scu": 10, "from_id": 0, "to_id": 3}]
        res = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0)
        self.assertEqual(res["summary"]["num_stops"], 3)       # P0 not duplicated
        self.assertEqual(res["stops"][0]["stop_id"], 0)
        self.assertEqual(len(res["stops"][0]["pickups"]), 2)

    def test_precedence_pickup_before_dropoff(self):
        nav = _line_nav(self._coords())
        pkgs = [{"id": "A", "commodity": "x", "scu": 10, "from_id": 3, "to_id": 0},
                {"id": "B", "commodity": "y", "scu": 10, "from_id": 1, "to_id": 2}]
        res = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0)
        pos = {s["stop_id"]: i for i, s in enumerate(res["stops"])}
        for p in pkgs:
            self.assertLess(pos[p["from_id"]], pos[p["to_id"]])

    def test_chooses_shorter_tour(self):
        nav = _line_nav(self._coords())
        pkgs = [{"id": "A", "commodity": "x", "scu": 10, "from_id": 0, "to_id": 1},
                {"id": "B", "commodity": "y", "scu": 10, "from_id": 0, "to_id": 3}]
        res = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0)
        self.assertEqual([s["stop_id"] for s in res["stops"]], [0, 1, 3])
        self.assertAlmostEqual(res["summary"]["total_distance_m"], 30.0)

    def test_capacity_infeasible_reports_min_capacity(self):
        nav = _line_nav(self._coords())
        # both load at the same stop -> 200 SCU aboard simultaneously, unavoidable
        pkgs = [{"id": "A", "commodity": "x", "scu": 100, "from_id": 0, "to_id": 1},
                {"id": "B", "commodity": "y", "scu": 100, "from_id": 0, "to_id": 1}]
        res = nav_core.plan_route(nav, pkgs, usable_scu=150, start_id=0)
        self.assertFalse(res["summary"]["feasible"])
        self.assertEqual(res["summary"]["min_capacity_scu"], 200)
        self.assertEqual(res["stops"], [])
        ok = nav_core.plan_route(nav, pkgs, usable_scu=200, start_id=0)
        self.assertTrue(ok["summary"]["feasible"])
        self.assertEqual(ok["summary"]["peak_load_scu"], 200)

    def test_running_onboard_load(self):
        nav = _line_nav(self._coords())
        pkgs = [{"id": "A", "commodity": "x", "scu": 40, "from_id": 0, "to_id": 2}]
        res = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0)
        by_id = {s["stop_id"]: s for s in res["stops"]}
        self.assertEqual(by_id[0]["onboard_scu"], 40.0)
        self.assertEqual(by_id[2]["onboard_scu"], 0.0)

    def test_unknown_poi_raises(self):
        nav = _line_nav(self._coords())
        with self.assertRaises(ValueError):
            nav_core.plan_route(nav, [{"scu": 1, "from_id": 0, "to_id": 999}],
                                usable_scu=100)

    def test_cross_system_plan(self):
        area = next(p for p in NAV.pois.values()
                    if p.name == "Area18" and p.system == "Stanton")
        orb = next(p for p in NAV.pois.values()
                   if p.name == "Orbituary" and p.system == "Pyro")
        pkgs = [{"id": "A", "commodity": "Gold", "scu": 50,
                 "from_id": area.id, "to_id": orb.id}]
        res = nav_core.plan_route(NAV, pkgs, usable_scu=696, start_id=area.id)
        self.assertTrue(res["summary"]["feasible"])
        self.assertEqual(res["summary"]["num_stops"], 2)
        self.assertTrue(res["stops"][1]["leg"]["cross_system"])


class MultiPickupGroupTests(unittest.TestCase):
    """Multi-pickup deliveries: one commodity total spread over several pickup
    locations with an unknown per-location split. Rows share a `group` id and
    carry the delivery total in `group_scu`; the solver counts that total once,
    holds it conservatively from the first pickup, and requires every pickup
    before the drop."""

    def _coords(self):
        return [(0, 0, 0), (10, 0, 0), (20, 0, 0), (30, 0, 0)]

    def _group(self, total, from_ids, to_id):
        return [{"id": f"g-{fid}", "commodity": "Scrap", "scu": 0,
                 "group": "G", "group_scu": total, "from_id": fid, "to_id": to_id}
                for fid in from_ids]

    def test_visits_all_pickups_before_drop(self):
        nav = _line_nav(self._coords())
        # total 3 SCU of Scrap from P0 and P1, delivered to P3
        pkgs = self._group(3, [0, 1], 3)
        res = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0)
        self.assertTrue(res["summary"]["feasible"])
        pos = {s["stop_id"]: i for i, s in enumerate(res["stops"])}
        self.assertIn(0, pos); self.assertIn(1, pos)        # both pickups visited
        self.assertLess(pos[0], pos[3])
        self.assertLess(pos[1], pos[3])

    def test_total_counted_once_conservatively_from_first_pickup(self):
        nav = _line_nav(self._coords())
        pkgs = self._group(3, [0, 1], 3)
        res = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0)
        by_id = {s["stop_id"]: s for s in res["stops"]}
        # full total aboard from the very first pickup (not 1.5 split), once only
        self.assertEqual(by_id[0]["onboard_scu"], 3.0)
        self.assertEqual(by_id[1]["onboard_scu"], 3.0)
        self.assertEqual(by_id[3]["onboard_scu"], 0.0)
        self.assertEqual(res["summary"]["peak_load_scu"], 3.0)

    def test_peak_is_group_total_not_sum_of_rows(self):
        nav = _line_nav(self._coords())
        # two independent groups of 3 SCU each to the same drop; conservative peak
        # = both totals held together = 6 (never 4 rows * something)
        pkgs = (self._group(3, [0, 1], 3))
        for p in self._group(3, [1, 2], 3):
            p["id"] = p["id"] + "-2"; p["group"] = "H"
            pkgs.append(p)
        res = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0)
        self.assertEqual(res["summary"]["peak_load_scu"], 6.0)

    def test_capacity_margin_uses_full_total(self):
        nav = _line_nav(self._coords())
        pkgs = self._group(3, [0, 1], 3)
        infeasible = nav_core.plan_route(nav, pkgs, usable_scu=2, start_id=0)
        self.assertFalse(infeasible["summary"]["feasible"])
        self.assertEqual(infeasible["summary"]["min_capacity_scu"], 3.0)
        ok = nav_core.plan_route(nav, pkgs, usable_scu=3, start_id=0)
        self.assertTrue(ok["summary"]["feasible"])

    def test_group_total_mixes_with_normal_packages(self):
        nav = _line_nav(self._coords())
        pkgs = self._group(3, [0, 1], 3)
        pkgs.append({"id": "N", "commodity": "Gold", "scu": 10,
                     "from_id": 0, "to_id": 3})
        res = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0)
        by_id = {s["stop_id"]: s for s in res["stops"]}
        self.assertEqual(by_id[0]["onboard_scu"], 13.0)   # 3 (group) + 10 (normal)
        self.assertEqual(res["summary"]["peak_load_scu"], 13.0)

    def test_packages_scu_counts_group_once(self):
        recs = [{"group": "G", "group_scu": 3, "scu": 0},
                {"group": "G", "group_scu": 3, "scu": 0},
                {"group": None, "scu": 10}]
        self.assertEqual(nav_core.packages_scu(recs), 13.0)


class QuickPicksTests(unittest.TestCase):
    def setUp(self):
        self.nav = _line_nav([(0, 0, 0), (10, 0, 0), (20, 0, 0), (30, 0, 0)])

    def _run(self, ship, pkgs):
        # mirror the persisted shape: packages keyed by id, plus a stops list
        return {"ship": ship, "stops": [{}], "packages":
                {str(i): {**p, "id": str(i)} for i, p in enumerate(pkgs)}}

    def test_lanes_ranked_by_frequency_with_names(self):
        runs = [
            self._run("Hull C", [{"commodity": "Gold", "scu": 50, "from_id": 0, "to_id": 1}]),
            self._run("Hull C", [{"commodity": "Gold", "scu": 50, "from_id": 0, "to_id": 1}]),
            self._run("MOLE", [{"commodity": "Iron", "scu": 10, "from_id": 2, "to_id": 3}]),
        ]
        picks = nav_core.derive_quick_picks(self.nav, runs)
        self.assertEqual([(l["from_id"], l["to_id"], l["count"]) for l in picks["lanes"]],
                         [(0, 1, 2), (2, 3, 1)])
        self.assertEqual(picks["lanes"][0]["from_name"], "P0")
        self.assertEqual(picks["lanes"][0]["to_name"], "P1")

    def test_lane_with_unresolvable_poi_is_dropped(self):
        runs = [self._run("Hull C", [{"commodity": "x", "scu": 1, "from_id": 0, "to_id": 999}])]
        self.assertEqual(nav_core.derive_quick_picks(self.nav, runs)["lanes"], [])

    def test_commodity_carries_most_common_scu(self):
        runs = [
            self._run("A", [{"commodity": "Gold", "scu": 50, "from_id": 0, "to_id": 1}]),
            self._run("A", [{"commodity": "Gold", "scu": 50, "from_id": 0, "to_id": 1}]),
            self._run("A", [{"commodity": "Gold", "scu": 32, "from_id": 0, "to_id": 1}]),
        ]
        picks = nav_core.derive_quick_picks(self.nav, runs)
        gold = next(c for c in picks["commodities"] if c["commodity"] == "Gold")
        self.assertEqual(gold["count"], 3)
        self.assertEqual(gold["scu"], 50.0)

    def test_ships_ranked(self):
        runs = [self._run("MOLE", []), self._run("Hull C", []), self._run("Hull C", [])]
        self.assertEqual([s["ship"] for s in nav_core.derive_quick_picks(self.nav, runs)["ships"]],
                         ["Hull C", "MOLE"])

    def test_run_packages_falls_back_to_stops(self):
        run = {"stops": [{"pickups": [{"commodity": "x", "scu": 1, "from_id": 0, "to_id": 1}]}]}
        self.assertEqual(len(nav_core.run_packages(run)), 1)


class RunStatsTests(unittest.TestCase):
    def test_total_reward_prefers_stored_total(self):
        self.assertEqual(nav_core.run_total_reward({"total_reward": 5000}), 5000.0)

    def test_total_reward_sums_rewards_map_when_no_total(self):
        run = {"rewards": {"A": 1000, "B": 2500, "": 0}}
        self.assertEqual(nav_core.run_total_reward(run), 3500.0)

    def test_total_reward_zero_when_absent(self):
        self.assertEqual(nav_core.run_total_reward({}), 0.0)

    def test_stats_totals_and_auec_per_hour(self):
        runs = [
            {"total_reward": 360000, "total_time_s": 3600, "total_distance_m": 5e9,
             "packages": {"0": {"scu": 100, "from_id": 0, "to_id": 1}}},
            {"total_reward": 180000, "total_time_s": 1800, "total_distance_m": 2e9,
             "packages": {"0": {"scu": 50, "from_id": 0, "to_id": 1}}},
        ]
        s = nav_core.derive_run_stats(runs)
        self.assertEqual(s["num_runs"], 2)
        self.assertEqual(s["total_reward"], 540000.0)
        self.assertEqual(s["total_scu"], 150.0)
        self.assertEqual(s["total_distance_m"], 7e9)
        self.assertEqual(s["total_time_s"], 5400.0)
        # 540000 aUEC over 1.5 h
        self.assertEqual(s["auec_per_hour"], 360000.0)

    def test_stats_auec_per_hour_null_without_time(self):
        s = nav_core.derive_run_stats([{"total_reward": 1000, "packages": {}}])
        self.assertIsNone(s["auec_per_hour"])

    def test_stats_empty(self):
        s = nav_core.derive_run_stats([])
        self.assertEqual(s["num_runs"], 0)
        self.assertEqual(s["total_reward"], 0.0)
        self.assertIsNone(s["auec_per_hour"])


class RunRecordTests(unittest.TestCase):
    """derive_run_record: a run only sets a record when it strictly beats an
    established prior best — the first qualifying haul isn't a 'record'."""

    def test_beats_prior_total_and_rate(self):
        run = {"total_reward": 900000, "total_time_s": 1800}   # 1.8M/hr
        prior = [{"total_reward": 500000, "total_time_s": 3600},   # 500k/hr
                 {"total_reward": 700000, "total_time_s": 1800}]   # 1.4M/hr
        rec = nav_core.derive_run_record(run, prior)
        self.assertEqual(rec["total"], 900000)
        self.assertEqual(rec["rate"], 1800000.0)

    def test_only_the_metric_that_wins_is_reported(self):
        # Big total but slow -> a total record but no rate record.
        run = {"total_reward": 1000000, "total_time_s": 36000}   # 100k/hr
        prior = [{"total_reward": 500000, "total_time_s": 1800}]  # 1M/hr
        rec = nav_core.derive_run_record(run, prior)
        self.assertEqual(rec["total"], 1000000)
        self.assertNotIn("rate", rec)

    def test_ties_do_not_break_a_record(self):
        run = {"total_reward": 500000, "total_time_s": 3600}
        prior = [{"total_reward": 500000, "total_time_s": 3600}]
        self.assertEqual(nav_core.derive_run_record(run, prior), {})

    def test_first_qualifying_run_is_not_a_record(self):
        # Nothing to beat -> no ping, even though the run has a positive reward/rate.
        run = {"total_reward": 400000, "total_time_s": 1800}
        self.assertEqual(nav_core.derive_run_record(run, []), {})
        # Priors with no usable reward/time still don't establish a baseline.
        self.assertEqual(nav_core.derive_run_record(run, [{"total_reward": 0}]), {})

    def test_rate_needs_time_to_qualify(self):
        run = {"total_reward": 999999}   # no time -> no rate
        prior = [{"total_reward": 100, "total_time_s": 3600}]
        rec = nav_core.derive_run_record(run, prior)
        self.assertEqual(rec["total"], 999999)
        self.assertNotIn("rate", rec)


class GuildLeaderboardTests(unittest.TestCase):
    def _run(self, did, name, reward, time_s, pkgs):
        return {"discord_id": did, "display_name": name, "total_reward": reward,
                "total_time_s": time_s, "packages":
                {str(i): {**p, "id": str(i)} for i, p in enumerate(pkgs)}}

    def test_groups_runs_by_member(self):
        runs = [
            self._run("a", "Alice", 100000, 3600, [{"scu": 50}]),
            self._run("a", "Alice", 50000, 1800, [{"scu": 25}]),
            self._run("b", "Bob", 20000, 3600, [{"scu": 10}]),
        ]
        rows = {r["discord_id"]: r for r in nav_core.derive_guild_leaderboard(runs)}
        self.assertEqual(set(rows), {"a", "b"})
        self.assertEqual(rows["a"]["num_runs"], 2)
        self.assertEqual(rows["a"]["total_reward"], 150000.0)
        self.assertEqual(rows["a"]["total_scu"], 75.0)
        self.assertEqual(rows["b"]["total_reward"], 20000.0)

    def test_name_lifted_from_freshest_run_that_has_one(self):
        # runs arrive freshest-first; the freshest carries no name, the next does.
        runs = [self._run("a", None, 100, 0, []),
                self._run("a", "Alice", 100, 0, [])]
        rows = nav_core.derive_guild_leaderboard(runs)
        self.assertEqual(rows[0]["display_name"], "Alice")

    def test_runs_without_discord_id_skipped(self):
        runs = [{"total_reward": 5, "packages": {}}]
        self.assertEqual(nav_core.derive_guild_leaderboard(runs), [])


class GuildCargoStatsTests(unittest.TestCase):
    def setUp(self):
        self.nav = _line_nav([(0, 0, 0), (10, 0, 0), (20, 0, 0), (30, 0, 0)])

    def _run(self, did, ship, pkgs, **extra):
        return {"discord_id": did, "ship": ship, "packages":
                {str(i): {**p, "id": str(i)} for i, p in enumerate(pkgs)}, **extra}

    def test_headline_totals_and_hauler_count(self):
        runs = [
            self._run("a", "Hull C", [{"commodity": "Gold", "scu": 100, "from_id": 0, "to_id": 1}],
                      total_reward=200000, total_time_s=3600),
            self._run("b", "MOLE", [{"commodity": "Gold", "scu": 50, "from_id": 0, "to_id": 1}],
                      total_reward=100000, total_time_s=3600),
        ]
        s = nav_core.derive_guild_cargo_stats(self.nav, runs)
        self.assertEqual(s["num_runs"], 2)
        self.assertEqual(s["num_haulers"], 2)
        self.assertEqual(s["total_reward"], 300000.0)
        self.assertEqual(s["total_scu"], 150.0)

    def test_top_commodities_ranked_by_scu(self):
        runs = [
            self._run("a", "A", [{"commodity": "Gold", "scu": 30, "from_id": 0, "to_id": 1},
                                 {"commodity": "Iron", "scu": 80, "from_id": 0, "to_id": 1}]),
        ]
        s = nav_core.derive_guild_cargo_stats(self.nav, runs)
        self.assertEqual([c["commodity"] for c in s["top_commodities"]], ["Iron", "Gold"])
        self.assertEqual(s["top_commodities"][0]["scu"], 80.0)

    def test_lanes_resolve_names_and_drop_unresolvable(self):
        runs = [
            self._run("a", "A", [{"commodity": "x", "scu": 1, "from_id": 0, "to_id": 1}]),
            self._run("a", "A", [{"commodity": "y", "scu": 1, "from_id": 0, "to_id": 999}]),
        ]
        s = nav_core.derive_guild_cargo_stats(self.nav, runs)
        self.assertEqual([(l["from_name"], l["to_name"]) for l in s["top_lanes"]],
                         [("P0", "P1")])


class MarketStatsTests(unittest.TestCase):
    def _deal(self, seller, buyer, mode, qty, final_auec, item="Gold"):
        return {"seller_id": seller, "buyer_id": buyer, "mode": mode, "qty": qty,
                "final_auec": final_auec, "item_name": item}

    def test_totals_and_trader_count(self):
        deals = [
            self._deal("a", "b", "sale", 2, 1000),
            self._deal("a", "c", "auction", 1, 5000),
        ]
        s = nav_core.derive_market_stats(deals)
        self.assertEqual(s["num_deals"], 2)
        self.assertEqual(s["auec_volume"], 6000.0)
        self.assertEqual(s["items_moved"], 3.0)
        self.assertEqual(s["num_traders"], 3)        # a, b, c
        self.assertEqual(s["auec_deals"], 2)

    def test_barter_excluded_from_auec_but_counted(self):
        deals = [
            self._deal("a", "b", "sale", 1, 2000),
            self._deal("a", "c", "barter", 1, None, item="Iron"),
        ]
        s = nav_core.derive_market_stats(deals)
        self.assertEqual(s["auec_volume"], 2000.0)   # barter contributes no aUEC
        self.assertEqual(s["barter_deals"], 1)
        self.assertEqual(s["auec_deals"], 1)
        self.assertEqual(s["items_moved"], 2.0)      # but its qty still moved

    def test_top_sellers_ranked_by_auec(self):
        deals = [
            self._deal("a", "x", "sale", 1, 1000),
            self._deal("b", "x", "sale", 1, 9000),
            self._deal("a", "x", "sale", 1, 500),
        ]
        s = nav_core.derive_market_stats(deals)
        self.assertEqual([t["discord_id"] for t in s["top_sellers"]], ["b", "a"])
        self.assertEqual(s["top_sellers"][1]["auec"], 1500.0)
        self.assertEqual(s["top_sellers"][1]["deals"], 2)

    def test_top_items_ranked_by_qty(self):
        deals = [
            self._deal("a", "x", "sale", 3, 100, item="Gold"),
            self._deal("a", "x", "sale", 8, 100, item="Iron"),
        ]
        s = nav_core.derive_market_stats(deals)
        self.assertEqual([i["item"] for i in s["top_items"]], ["Iron", "Gold"])
        self.assertEqual(s["top_items"][0]["qty"], 8.0)

    def test_empty(self):
        s = nav_core.derive_market_stats([])
        self.assertEqual(s["num_deals"], 0)
        self.assertEqual(s["auec_volume"], 0)
        self.assertEqual(s["top_sellers"], [])


class EventFillTests(unittest.TestCase):
    def _event(self, roster, min_players=0, max_players=None):
        return {"min_players": min_players, "max_players": max_players,
                "roles": [{"role": r, "needed": n} for r, n in roster]}

    def _signup(self, did, roles, status="going"):
        return {"discord_id": did, "roles": roles, "status": status}

    def test_headline_totals_and_min_met(self):
        # 3 signups, min 3, max 5: min met, 2 spots left, Medical 2/2, Escort n/a.
        ev = self._event([("Medical", 2)], min_players=3, max_players=5)
        signups = [self._signup("a", ["Medical"]), self._signup("b", ["Escort"]),
                   self._signup("c", ["Medical"])]
        f = nav_core.derive_event_fill(ev, signups)
        self.assertEqual(f["total_going"], 3)
        self.assertEqual(f["spots_left"], 2)
        self.assertTrue(f["min_met"])
        self.assertFalse(f["is_full"])
        self.assertEqual({r["role"]: r["filled"] for r in f["roster"]}["Medical"], 2)

    def test_min_not_met(self):
        ev = self._event([], min_players=4)
        f = nav_core.derive_event_fill(ev, [self._signup("a", [])])
        self.assertFalse(f["min_met"])
        self.assertEqual(f["total_going"], 1)

    def test_double_count_rule(self):
        # Two members each cover Medical AND Escort: headline counts them once,
        # both role bars fill.
        ev = self._event([("Medical", 2), ("Escort", 2)], min_players=2, max_players=5)
        signups = [self._signup("a", ["Medical", "Escort"]),
                   self._signup("b", ["Medical", "Escort"])]
        f = nav_core.derive_event_fill(ev, signups)
        self.assertEqual(f["total_going"], 2)
        roster = {r["role"]: r for r in f["roster"]}
        self.assertEqual(roster["Medical"]["filled"], 2)
        self.assertEqual(roster["Escort"]["filled"], 2)
        self.assertEqual(roster["Medical"]["short"], 0)

    def test_short_count(self):
        ev = self._event([("Surveyor", 3)])
        f = nav_core.derive_event_fill(ev, [self._signup("a", ["Surveyor"])])
        roster = {r["role"]: r for r in f["roster"]}
        self.assertEqual(roster["Surveyor"]["filled"], 1)
        self.assertEqual(roster["Surveyor"]["short"], 2)

    def test_surplus_clamps_short_to_zero(self):
        ev = self._event([("Medical", 1)])
        signups = [self._signup("a", ["Medical"]), self._signup("b", ["Medical"])]
        roster = {r["role"]: r
                  for r in nav_core.derive_event_fill(ev, signups)["roster"]}
        self.assertEqual(roster["Medical"]["filled"], 2)
        self.assertEqual(roster["Medical"]["short"], 0)

    def test_unlimited_max(self):
        ev = self._event([], max_players=None)
        f = nav_core.derive_event_fill(ev, [self._signup("a", []),
                                            self._signup("b", [])])
        self.assertIsNone(f["spots_left"])
        self.assertFalse(f["is_full"])

    def test_full_when_max_reached(self):
        ev = self._event([], max_players=2)
        f = nav_core.derive_event_fill(ev, [self._signup("a", []),
                                            self._signup("b", [])])
        self.assertTrue(f["is_full"])
        self.assertEqual(f["spots_left"], 0)

    def test_maybe_and_withdrawn_excluded(self):
        ev = self._event([("Medical", 2)], max_players=5)
        signups = [self._signup("a", ["Medical"], status="going"),
                   self._signup("b", ["Medical"], status="maybe"),
                   self._signup("c", ["Medical"], status="withdrawn")]
        f = nav_core.derive_event_fill(ev, signups)
        self.assertEqual(f["total_going"], 1)
        self.assertEqual({r["role"]: r["filled"] for r in f["roster"]}["Medical"], 1)

    def test_missing_status_counts_as_going(self):
        ev = self._event([], max_players=5)
        f = nav_core.derive_event_fill(ev, [{"discord_id": "a", "roles": []}])
        self.assertEqual(f["total_going"], 1)

    def test_duplicate_member_deduped(self):
        # Defensive: a stray double signup for one member counts once.
        ev = self._event([("Medical", 2)], max_players=5)
        f = nav_core.derive_event_fill(
            ev, [self._signup("a", ["Medical"]), self._signup("a", ["Medical"])])
        self.assertEqual(f["total_going"], 1)
        self.assertEqual({r["role"]: r["filled"] for r in f["roster"]}["Medical"], 1)

    def test_empty(self):
        ev = self._event([("Medical", 2)], min_players=1, max_players=4)
        f = nav_core.derive_event_fill(ev, [])
        self.assertEqual(f["total_going"], 0)
        self.assertEqual(f["spots_left"], 4)
        self.assertFalse(f["min_met"])
        self.assertFalse(f["is_full"])
        self.assertEqual(f["roster"][0]["filled"], 0)
        self.assertEqual(f["roster"][0]["short"], 2)


class DeriveEventPhaseTests(unittest.TestCase):
    """Lifecycle phase derived from timestamps (open/closed/live/ended) + the
    cancelled/completed overrides. `now` is fixed; events are placed relative."""
    def setUp(self):
        self.now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)

    def _ev(self, start_delta_min, *, deadline_delta_min=None, duration_min=None,
            status="scheduled"):
        def iso(delta):
            return (self.now + timedelta(minutes=delta)).isoformat()
        ev = {"start_at": iso(start_delta_min), "status": status,
              "duration_min": duration_min}
        if deadline_delta_min is not None:
            ev["signup_deadline"] = iso(deadline_delta_min)
        return ev

    def _phase(self, ev):
        return nav_core.derive_event_phase(ev, self.now)

    def test_open_before_deadline(self):
        p = self._phase(self._ev(60 * 24, deadline_delta_min=60 * 12))
        self.assertEqual(p["phase"], "open")
        self.assertTrue(p["signups_open"])

    def test_open_no_deadline_before_start(self):
        p = self._phase(self._ev(120))
        self.assertEqual(p["phase"], "open")

    def test_closed_after_deadline_before_start(self):
        p = self._phase(self._ev(300, deadline_delta_min=-60))
        self.assertEqual(p["phase"], "closed")
        self.assertFalse(p["signups_open"])

    def test_live_within_duration(self):
        p = self._phase(self._ev(-30, duration_min=120))
        self.assertEqual(p["phase"], "live")
        self.assertFalse(p["signups_open"])

    def test_ended_after_duration(self):
        p = self._phase(self._ev(-300, duration_min=60))
        self.assertEqual(p["phase"], "ended")

    def test_no_duration_live_within_grace(self):
        # Started 30 min ago, no duration → still live inside the 3h grace window.
        p = self._phase(self._ev(-30))
        self.assertEqual(p["phase"], "live")

    def test_no_duration_ended_after_grace(self):
        # Started past the grace window with no duration → finished.
        p = self._phase(self._ev(-(nav_core.EVENT_LIVE_GRACE_MIN + 30)))
        self.assertEqual(p["phase"], "ended")

    def test_cancelled_override(self):
        p = self._phase(self._ev(120, status="cancelled"))
        self.assertEqual(p["phase"], "cancelled")
        self.assertFalse(p["signups_open"])

    def test_completed_override_before_end(self):
        # Marked completed even though the clock says it'd be live → ended.
        p = self._phase(self._ev(-10, duration_min=120, status="completed"))
        self.assertEqual(p["phase"], "ended")

    def test_signup_close_falls_back_to_start(self):
        p = self._phase(self._ev(120))
        self.assertEqual(p["signup_close"], self._ev(120)["start_at"])


class RosterBoardTests(unittest.TestCase):
    def _signup(self, did, roles=None, status="going"):
        return {"discord_id": did, "roles": roles or [], "status": status}

    def _group(self, gid, name="Alpha", **kw):
        g = {"id": gid, "parent_id": None, "name": name, "kind": "squad",
             "ship": None, "capacity": None, "leader_id": None, "notes": None, "sort": 0}
        g.update(kw)
        return g

    def test_members_land_in_their_group_pool_gets_the_rest(self):
        groups = [self._group(1), self._group(2, name="Bravo")]
        signups = [self._signup("a"), self._signup("b"), self._signup("c")]
        assignments = [{"discord_id": "a", "group_id": 1, "slot": "pilot"},
                       {"discord_id": "b", "group_id": 2, "slot": None}]
        b = nav_core.derive_roster_board(groups, assignments, signups)
        g1 = next(g for g in b["groups"] if g["id"] == 1)
        self.assertEqual([m["discord_id"] for m in g1["members"]], ["a"])
        self.assertEqual(g1["members"][0]["slot"], "pilot")
        self.assertEqual([m["discord_id"] for m in b["unassigned"]], ["c"])
        self.assertEqual(b["assigned_count"], 2)
        self.assertEqual(b["total_going"], 3)

    def test_withdrawn_or_unknown_assignment_is_dropped(self):
        # 'b' withdrew, 'z' was never a signup: both fall out of the plan.
        groups = [self._group(1)]
        signups = [self._signup("a"), self._signup("b", status="withdrawn")]
        assignments = [{"discord_id": "a", "group_id": 1, "slot": None},
                       {"discord_id": "b", "group_id": 1, "slot": None},
                       {"discord_id": "z", "group_id": 1, "slot": None}]
        b = nav_core.derive_roster_board(groups, assignments, signups)
        g1 = b["groups"][0]
        self.assertEqual([m["discord_id"] for m in g1["members"]], ["a"])
        self.assertEqual(b["assigned_count"], 1)
        self.assertEqual(b["unassigned"], [])

    def test_capacity_and_short(self):
        groups = [self._group(1, capacity=3)]
        signups = [self._signup("a"), self._signup("b")]
        assignments = [{"discord_id": "a", "group_id": 1, "slot": None},
                       {"discord_id": "b", "group_id": 1, "slot": None}]
        g1 = nav_core.derive_roster_board(groups, assignments, signups)["groups"][0]
        self.assertEqual(g1["filled"], 2)
        self.assertEqual(g1["capacity"], 3)
        self.assertEqual(g1["short"], 1)

    def test_no_capacity_leaves_short_none(self):
        g1 = nav_core.derive_roster_board([self._group(1)], [], [])["groups"][0]
        self.assertIsNone(g1["capacity"])
        self.assertIsNone(g1["short"])

    def test_leader_sorts_first_and_is_flagged(self):
        groups = [self._group(1, leader_id="b")]
        signups = [self._signup("a"), self._signup("b")]
        names = {"a": "Aaron", "b": "Zed"}
        assignments = [{"discord_id": "a", "group_id": 1, "slot": None},
                       {"discord_id": "b", "group_id": 1, "slot": None}]
        g1 = nav_core.derive_roster_board(groups, assignments, signups, names)["groups"][0]
        self.assertEqual([m["discord_id"] for m in g1["members"]], ["b", "a"])
        self.assertTrue(g1["members"][0]["is_leader"])

    def test_names_map_used_else_stub(self):
        b = nav_core.derive_roster_board(
            [self._group(1)],
            [{"discord_id": "123456789", "group_id": 1, "slot": None}],
            [self._signup("123456789")], {"123456789": "Ace"})
        self.assertEqual(b["groups"][0]["members"][0]["name"], "Ace")
        b2 = nav_core.derive_roster_board(
            [], [], [self._signup("987654321")])
        self.assertEqual(b2["unassigned"][0]["name"], "Member 4321")

    def test_duplicate_assignment_seats_member_once(self):
        # A stray double (member somehow in two groups): first wins, no double count.
        groups = [self._group(1), self._group(2)]
        signups = [self._signup("a")]
        assignments = [{"discord_id": "a", "group_id": 1, "slot": None},
                       {"discord_id": "a", "group_id": 2, "slot": None}]
        b = nav_core.derive_roster_board(groups, assignments, signups)
        self.assertEqual(b["assigned_count"], 1)
        seated = sum(len(g["members"]) for g in b["groups"])
        self.assertEqual(seated, 1)


class EventManifestTests(unittest.TestCase):
    def test_manifest_renders_groups_seats_leader_and_pool(self):
        ev = {"title": "Bunker Op", "location": "Everus Harbor"}
        board = {
            "groups": [
                {"id": 1, "name": "Alpha", "ship": "Cutlass", "capacity": 3,
                 "filled": 2, "members": [
                     {"name": "Zed", "slot": "pilot", "is_leader": True},
                     {"name": "Ana", "slot": "gunner", "is_leader": False}]},
                {"id": 2, "name": "Bravo", "ship": None, "capacity": None,
                 "filled": 0, "members": []},
            ],
            "unassigned": [{"name": "Cid"}],
        }
        text = nav_core.build_event_manifest(ev, board)
        self.assertIn("**Bunker Op — Fleet Manifest**", text)
        self.assertIn("Rally: Everus Harbor", text)
        self.assertIn("__Alpha (Cutlass, 2/3)__", text)
        self.assertIn("• Zed — pilot ⭐", text)
        self.assertIn("• Ana — gunner", text)
        self.assertIn("_(empty)_", text)
        self.assertIn("__Unassigned (1)__", text)
        self.assertIn("• Cid", text)

    def test_manifest_omits_empty_pool_and_rally(self):
        text = nav_core.build_event_manifest(
            {"title": "Solo"}, {"groups": [], "unassigned": []})
        self.assertIn("**Solo — Fleet Manifest**", text)
        self.assertNotIn("Rally:", text)
        self.assertNotIn("Unassigned", text)


class ShipSeatTemplateTests(unittest.TestCase):
    def test_single_seat_is_just_pilot(self):
        self.assertEqual(nav_core.ship_seat_template(1), ["Pilot"])

    def test_two_seat_adds_copilot(self):
        self.assertEqual(nav_core.ship_seat_template(2), ["Pilot", "Co-Pilot"])

    def test_length_always_matches_crew(self):
        for n in range(1, 12):
            self.assertEqual(len(nav_core.ship_seat_template(n)), n)

    def test_extra_seats_become_turrets(self):
        self.assertEqual(nav_core.ship_seat_template(4),
                         ["Pilot", "Co-Pilot", "Turret 1", "Turret 2"])

    def test_role_flag_flavors_one_specialist_seat(self):
        # A 3-crew medical ship gets a Medic seat in place of a turret.
        self.assertEqual(nav_core.ship_seat_template(3, {"is_medical"}),
                         ["Pilot", "Co-Pilot", "Medic"])

    def test_specialist_only_when_room_and_first_flag_wins(self):
        # 2-crew has no room for a specialist; priority order is deterministic.
        self.assertEqual(nav_core.ship_seat_template(2, {"is_medical"}),
                         ["Pilot", "Co-Pilot"])
        self.assertEqual(nav_core.ship_seat_template(4, {"is_salvage", "is_mining"}),
                         ["Pilot", "Co-Pilot", "Mining Op", "Turret 1"])

    def test_junk_crew_coerces_to_one(self):
        self.assertEqual(nav_core.ship_seat_template(None), ["Pilot"])
        self.assertEqual(nav_core.ship_seat_template("x"), ["Pilot"])
        self.assertEqual(nav_core.ship_seat_template(0), ["Pilot"])


class EventTaxonomyTests(unittest.TestCase):
    def test_flat_roles_match_groups(self):
        import event_taxonomy
        flat = [r for g in event_taxonomy.ROLE_GROUPS for r in g["roles"]]
        self.assertEqual(event_taxonomy.ROLES, flat)

    def test_taxonomy_payload_shape(self):
        import event_taxonomy
        t = event_taxonomy.taxonomy()
        self.assertEqual(set(t), {"types", "categories", "role_groups", "roles"})
        self.assertIn("Survey Op", t["types"])
        self.assertIn("Surveyor", t["roles"])
        # "Event" and "Race" categories were added in the multi-type pass.
        self.assertIn("Event", t["categories"])
        self.assertIn("Race", t["categories"])


class CatalogTests(unittest.TestCase):
    def test_feed_id_synthesis_and_dedupe(self):
        import catalog
        items = catalog.feed_items(
            ["Titanium", "Medical Supplies", "Titanium"],   # dup collapses
            [{"name": "Argo MOLE", "scu": 96}])
        ids = {it["item_id"] for it in items}
        self.assertIn("commodity:titanium", ids)
        self.assertIn("commodity:medical-supplies", ids)
        self.assertIn("ship:argo-mole", ids)
        self.assertEqual(sum(1 for it in items if it["item_id"] == "commodity:titanium"), 1)
        com = next(it for it in items if it["item_id"] == "commodity:titanium")
        self.assertEqual(com["unit"], "SCU")
        ship = next(it for it in items if it["item_id"] == "ship:argo-mole")
        self.assertEqual((ship["kind"], ship["unit"]), ("ship", "each"))

    def test_equipment_items_in_feed(self):
        import catalog
        items = catalog.feed_items(
            ["Titanium"], [],
            ["Omnisky III Cannon", "Omnisky III Cannon"])   # dup collapses
        eq = next(it for it in items if it["item_id"] == "item:omnisky-iii-cannon")
        self.assertEqual((eq["kind"], eq["unit"]), ("item", "each"))
        self.assertEqual(sum(1 for it in items if it["item_id"] == "item:omnisky-iii-cannon"), 1)
        # the equipment names also flow through build() + are searchable
        cat = catalog.build(["Titanium"], [], [], ["Omnisky III Cannon"])
        self.assertTrue(any(h["name"] == "Omnisky III Cannon" for h in catalog.search(cat, "omni")))

    def test_custom_item_and_build_override(self):
        import catalog
        cust = catalog.custom_item({"id": 7, "name": "Size 3 Shield", "kind": "component"})
        self.assertEqual(cust["item_id"], "custom:7")
        self.assertEqual(cust["unit"], "each")          # default for component
        cat = catalog.build(["Titanium"], [], [{"id": 7, "name": "Aaa Gear", "kind": "gear"}])
        self.assertEqual(cat[0]["name"], "Aaa Gear")    # sorts ahead of Titanium

    def test_search_prefix_ranks_above_contains(self):
        import catalog
        cat = catalog.build(["Titanium", "Astatine", "Quantanium"], [], [])
        hits = catalog.search(cat, "tan")
        names = [h["name"] for h in hits]
        self.assertTrue(names)
        self.assertTrue(all("tan" in n.lower() for n in names))
        self.assertEqual(len(catalog.search(cat, "", limit=2)), 2)


class InventoryRollupTests(unittest.TestCase):
    def test_rollup_sums_and_breaks_down(self):
        rows = [
            {"item_id": "commodity:titanium", "item_name": "Titanium", "unit": "SCU",
             "qty": 80, "owner_id": "A", "location": "Area18"},
            {"item_id": "commodity:titanium", "item_name": "Titanium", "unit": "SCU",
             "qty": 40, "owner_id": "B", "location": "Area18"},
            {"item_id": "commodity:titanium", "item_name": "Titanium", "unit": "SCU",
             "qty": 30, "owner_id": "A", "location": "Lorville"},
            {"item_id": "commodity:laranite", "item_name": "Laranite", "unit": "SCU",
             "qty": 10, "owner_id": "A", "location": ""},
        ]
        roll = nav_core.derive_inventory_rollup(rows)
        self.assertEqual(roll[0]["item_id"], "commodity:titanium")
        self.assertEqual(roll[0]["total"], 150)
        self.assertEqual(roll[0]["holders"], 2)         # A and B
        self.assertEqual(roll[0]["by_owner"][0], {"owner_id": "A", "qty": 110})
        self.assertEqual(roll[1]["by_location"][0]["location"], "—")

    def test_empty(self):
        self.assertEqual(nav_core.derive_inventory_rollup([]), [])


class GoalProgressTests(unittest.TestCase):
    GOAL = {"line_items": [
        {"item_id": "commodity:titanium", "item_name": "Titanium", "unit": "SCU",
         "qty_needed": 500},
        {"item_id": "commodity:laranite", "item_name": "Laranite", "unit": "SCU",
         "qty_needed": 300},
    ]}

    def test_per_line_and_overall(self):
        rows = [
            {"item_id": "commodity:titanium", "qty": 320, "owner_id": "A"},
            {"item_id": "commodity:titanium", "qty": 80, "owner_id": "B"},
            {"item_id": "commodity:laranite", "qty": 150, "owner_id": "A"},
        ]
        p = nav_core.derive_goal_progress(self.GOAL, rows)
        tit = next(l for l in p["lines"] if l["item_id"] == "commodity:titanium")
        self.assertEqual((tit["have"], tit["needed"]), (400, 500))
        self.assertEqual(tit["pct"], 80.0)
        self.assertEqual(tit["short"], 100)
        self.assertEqual(p["overall_pct"], 68.8)        # (400+150)/(500+300)
        self.assertFalse(p["is_met"])
        self.assertEqual(p["per_contributor"][0], {"owner_id": "A", "qty": 470})

    def test_oversupply_one_line_does_not_mask_shortfall(self):
        rows = [
            {"item_id": "commodity:titanium", "qty": 5000, "owner_id": "A"},  # way over
            {"item_id": "commodity:laranite", "qty": 0, "owner_id": "A"},
        ]
        p = nav_core.derive_goal_progress(self.GOAL, rows)
        self.assertEqual(p["overall_pct"], 62.5)        # 500/800, capped line
        self.assertFalse(p["is_met"])

    def test_met_when_all_lines_full(self):
        rows = [
            {"item_id": "commodity:titanium", "qty": 500, "owner_id": "A"},
            {"item_id": "commodity:laranite", "qty": 300, "owner_id": "B"},
        ]
        p = nav_core.derive_goal_progress(self.GOAL, rows)
        self.assertEqual(p["overall_pct"], 100.0)
        self.assertTrue(p["is_met"])

    def test_no_line_items_is_not_met(self):
        p = nav_core.derive_goal_progress({"line_items": []}, [])
        self.assertEqual(p["overall_pct"], 100.0)       # vacuous need → 100%
        self.assertFalse(p["is_met"])                   # but an empty goal isn't "met"


class AuctionStateTests(unittest.TestCase):
    NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)

    def _auction(self, **over):
        a = {"mode": "auction", "status": "open", "start_price": 100,
             "buyout_auec": None, "ends_at": "2026-06-25T18:00:00+00:00"}
        a.update(over)
        return a

    def _bid(self, oid, bidder, amount, created_at, status="active"):
        return {"id": oid, "bidder_id": bidder, "amount_auec": amount,
                "status": status, "created_at": created_at}

    def test_high_bid_and_next_min(self):
        offers = [self._bid(1, "A", 100, "2026-06-25T10:00:00+00:00"),
                  self._bid(2, "B", 250, "2026-06-25T10:05:00+00:00")]
        s = nav_core.derive_auction_state(self._auction(), offers, self.NOW)
        self.assertEqual((s["high_bid"], s["high_bidder"]), (250, "B"))
        self.assertEqual(s["bid_count"], 2)
        self.assertEqual(s["next_min_bid"], 251)
        self.assertFalse(s["is_closed"])

    def test_next_min_is_start_price_with_no_bids(self):
        s = nav_core.derive_auction_state(self._auction(start_price=150), [], self.NOW)
        self.assertIsNone(s["high_bid"])
        self.assertEqual(s["next_min_bid"], 150)

    def test_equal_amount_ties_to_earliest(self):
        offers = [self._bid(1, "A", 200, "2026-06-25T10:00:00+00:00"),
                  self._bid(2, "B", 200, "2026-06-25T10:05:00+00:00")]
        s = nav_core.derive_auction_state(self._auction(), offers, self.NOW)
        self.assertEqual(s["high_bidder"], "A")          # earliest holds the lead

    def test_withdrawn_and_lost_bids_ignored(self):
        offers = [self._bid(1, "A", 500, "2026-06-25T10:00:00+00:00", "withdrawn"),
                  self._bid(2, "B", 300, "2026-06-25T10:05:00+00:00", "lost"),
                  self._bid(3, "C", 200, "2026-06-25T10:06:00+00:00")]
        s = nav_core.derive_auction_state(self._auction(), offers, self.NOW)
        self.assertEqual((s["high_bid"], s["high_bidder"]), (200, "C"))
        self.assertEqual(s["bid_count"], 1)

    def test_buyout_short_circuits_before_end(self):
        offers = [self._bid(1, "A", 300, "2026-06-25T10:00:00+00:00"),
                  self._bid(2, "B", 1000, "2026-06-25T10:05:00+00:00")]   # >= buyout
        s = nav_core.derive_auction_state(
            self._auction(buyout_auec=1000), offers, self.NOW)
        self.assertTrue(s["bought_out"])
        self.assertTrue(s["is_closed"])                  # closed though ends_at is future
        self.assertEqual((s["winner_id"], s["winning_amount"]), ("B", 1000))

    def test_buyout_winner_is_earliest_to_hit_it(self):
        offers = [self._bid(1, "A", 1200, "2026-06-25T10:00:00+00:00"),
                  self._bid(2, "B", 1500, "2026-06-25T10:05:00+00:00")]
        s = nav_core.derive_auction_state(
            self._auction(buyout_auec=1000), offers, self.NOW)
        self.assertEqual(s["winner_id"], "A")            # first past the buyout wins

    def test_closes_and_picks_winner_at_end_time(self):
        offers = [self._bid(1, "A", 100, "2026-06-25T10:00:00+00:00"),
                  self._bid(2, "B", 250, "2026-06-25T10:05:00+00:00")]
        past = datetime(2026, 6, 25, 19, 0, tzinfo=timezone.utc)   # after ends_at
        s = nav_core.derive_auction_state(self._auction(), offers, past)
        self.assertTrue(s["time_up"])
        self.assertTrue(s["is_closed"])
        self.assertEqual((s["winner_id"], s["winning_amount"]), ("B", 250))

    def test_expired_with_no_bids_has_no_winner(self):
        past = datetime(2026, 6, 25, 19, 0, tzinfo=timezone.utc)
        s = nav_core.derive_auction_state(self._auction(), [], past)
        self.assertTrue(s["is_closed"])
        self.assertIsNone(s["winner_id"])

    def test_terminal_status_is_closed(self):
        s = nav_core.derive_auction_state(
            self._auction(status="completed"), [], self.NOW)
        self.assertTrue(s["is_closed"])


class TradeTerminalCrosswalkTests(unittest.TestCase):
    """nav_core.match_terminals resolves UEX commodity terminals onto routable
    nav POIs by name (the trade-route planner's placement step, #21)."""

    def _nav(self):
        nav = nav_core.NavData()

        def poi(pid, name, system="Stanton"):
            nav.pois[pid] = nav_core.Poi(
                id=pid, name=name, system=system, container_name=None, type="Station",
                local_km=None, global_m=(float(pid), 0.0, 0.0), latitude=None,
                longitude=None, height_m=None, qt_marker=True)

        poi(1, "Wide Forest Station (ARC-L1)")   # synth_container_pois' Lagrange fold
        poi(2, "Everus Harbor")                  # plain station, exact match
        poi(3, "Gateway Station Pyro")           # gateway, word-order flipped vs UEX
        poi(4, "Area18")                         # city
        return nav

    def _term(self, **kw):
        # Minimal UEX terminal row shape (only the fields the matcher reads).
        row = {"id": kw.get("id", 100), "type": "commodity", "star_system_name": "Stanton",
               "displayname": None, "space_station_name": None, "outpost_name": None,
               "city_name": None, "nickname": None, "name": None}
        row.update(kw)
        return row

    def test_lagrange_word_order_flip(self):
        # UEX 'ARC-L1 Wide Forest Station' -> synth 'Wide Forest Station (ARC-L1)'.
        rows = [self._term(id=1, name="Admin - ARC-L1",
                           displayname="ARC-L1 Wide Forest Station",
                           space_station_name="ARC-L1 Wide Forest Station")]
        resolved, unmatched = nav_core.match_terminals(self._nav(), rows)
        self.assertEqual(unmatched, [])
        self.assertEqual(resolved[0]["poi_id"], 1)
        self.assertEqual(resolved[0]["id"], 1)

    def test_shop_prefix_in_name_is_ignored(self):
        # The `name` field carries a shop-type prefix ('Admin - ...'); the physical
        # location must come from the place-name fields, and several shops at one
        # station all resolve to the same POI.
        rows = [self._term(id=10, name="Admin - Everus Harbor",
                           space_station_name="Everus Harbor"),
                self._term(id=11, name="Platinum Bay - Everus Harbor",
                           space_station_name="Everus Harbor")]
        resolved, unmatched = nav_core.match_terminals(self._nav(), rows)
        self.assertEqual(unmatched, [])
        self.assertEqual({r["poi_id"] for r in resolved}, {2})

    def test_gateway_word_order_flip(self):
        rows = [self._term(id=20, name="Admin - Pyro Gateway (Stanton)",
                           displayname="Pyro Gateway",
                           space_station_name="Pyro Gateway (Stanton)")]
        resolved, _ = nav_core.match_terminals(self._nav(), rows)
        self.assertEqual(resolved[0]["poi_id"], 3)

    def test_city_exact_match(self):
        rows = [self._term(id=30, name="TDD - Area18", city_name="Area18")]
        resolved, _ = nav_core.match_terminals(self._nav(), rows)
        self.assertEqual(resolved[0]["poi_id"], 4)

    def test_unresolved_terminal_is_reported_not_placed(self):
        rows = [self._term(id=40, name="Admin - Rat's Nest",
                           displayname="Rat's Nest", space_station_name="Rat's Nest")]
        resolved, unmatched = nav_core.match_terminals(self._nav(), rows)
        self.assertEqual(resolved, [])
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0]["id"], 40)

    def test_resolved_record_shape(self):
        rows = [self._term(id=50, displayname="Everus Harbor",
                           space_station_name="Everus Harbor")]
        resolved, _ = nav_core.match_terminals(self._nav(), rows)
        self.assertEqual(set(resolved[0]), {"id", "name", "system", "poi_id", "poi_name"})
        self.assertEqual(resolved[0]["poi_name"], "Everus Harbor")


class TradeStopKindTests(unittest.TestCase):
    """nav_core.terminal_stop_kinds / stop_exclusions — which stops a ship can
    physically use (#34). A Hull-C has no landing gear: it can only moor at a
    station cargo dock."""

    def _nav(self):
        nav = nav_core.NavData()

        def poi(pid, name, system="Stanton"):
            nav.pois[pid] = nav_core.Poi(
                id=pid, name=name, system=system, container_name=None, type="Station",
                local_km=None, global_m=(float(pid), 0.0, 0.0), latitude=None,
                longitude=None, height_m=None, qt_marker=True)

        poi(1, "Everus Harbor")          # station with a cargo dock
        poi(2, "HDMS-Bezdek")            # surface outpost
        poi(3, "Gateway Station Pyro")   # gateway: dock asserted from architecture
        poi(4, "Levski")                 # planetary city that HAS a cargo dock
        poi(5, "ARC-L1 Station")         # station, no cargo dock
        return nav

    def _term(self, poi_name, *, ttype="commodity", station=0, city=0, outpost=0,
              loading_dock=0, tid=1):
        return {"id": tid, "type": ttype, "star_system_name": "Stanton",
                "displayname": poi_name, "space_station_name": poi_name if station else None,
                "city_name": poi_name if city else None,
                "outpost_name": poi_name if outpost else None,
                "id_space_station": station, "id_city": city, "id_outpost": outpost,
                "has_loading_dock": loading_dock, "nickname": None, "name": poi_name}

    def _kinds(self):
        rows = [
            self._term("Everus Harbor", station=1, loading_dock=1, tid=1),
            self._term("HDMS-Bezdek", outpost=1, tid=2),
            self._term("Gateway Station Pyro", station=1, tid=3),        # feed omits the dock
            # Levski states its dock on a NON-commodity desk only — the whole reason
            # the classifier runs over the unfiltered feed.
            self._term("Levski", city=1, tid=4),
            self._term("Levski", city=1, loading_dock=1, ttype="item", tid=5),
            self._term("ARC-L1 Station", station=1, tid=6),
        ]
        return nav_core.terminal_stop_kinds(self._nav(), rows)

    def test_places_classified_from_uex_location_ids(self):
        k = self._kinds()
        self.assertEqual(k[1]["place"], "station")
        self.assertEqual(k[2]["place"], "outpost")
        self.assertEqual(k[4]["place"], "city")

    def test_dock_flag_is_ored_across_all_terminals_at_the_stop(self):
        # Levski's commodity desk has no dock flag; its item desk does. A
        # commodity-only view would wrongly call Levski undockable.
        self.assertTrue(self._kinds()[4]["dock"])

    def test_gateway_dock_asserted_despite_missing_feed_flag(self):
        # UEX omits has_loading_dock on several gateways; every gateway has a cargo
        # deck, so architecture wins over an incomplete feed.
        self.assertTrue(self._kinds()[3]["dock"])

    def test_station_without_a_dock_is_not_dockable(self):
        k = self._kinds()
        self.assertEqual(k[5]["place"], "station")
        self.assertFalse(k[5]["dock"])

    def test_stations_mode_excludes_surface_and_city_stops(self):
        ex = nav_core.stop_exclusions(self._kinds(), "stations")
        self.assertIn(2, ex)            # outpost
        self.assertIn(4, ex)            # Levski is a planetary city...
        self.assertNotIn(1, ex)

    def test_dock_mode_keeps_a_planetary_stop_that_has_a_dock(self):
        # The two modes are independent axes, not nested: Levski is planetary but
        # dockable, so "cargo dock" keeps the stop "stations only" drops.
        ex = nav_core.stop_exclusions(self._kinds(), "dock")
        self.assertNotIn(4, ex)         # ...yet a Hull-C can dock there
        self.assertIn(2, ex)            # outpost: no dock
        self.assertIn(5, ex)            # station, but no dock

    def test_any_and_unknown_modes_exclude_nothing(self):
        # An unrecognized restriction must never silently drop stops.
        self.assertEqual(nav_core.stop_exclusions(self._kinds(), "any"), frozenset())
        self.assertEqual(nav_core.stop_exclusions(self._kinds(), "bogus"), frozenset())


class TradeRankingTests(unittest.TestCase):
    """nav_core.rank_trades — best buy->sell trades over the live price feed (#21
    step 2). Price points use the shape /api/trade/prices emits."""

    def _pt(self, commodity, terminal_id, poi_id, system="Stanton", buy=None,
            sell=None, scu_buy=0, scu_sell_stock=0, updated_at=None):
        return {"commodity": commodity, "terminal_id": terminal_id,
                "terminal": f"T{terminal_id}", "system": system, "poi_id": poi_id,
                "buy": buy, "sell": sell, "scu_buy": scu_buy,
                "scu_sell_stock": scu_sell_stock, "updated_at": updated_at}

    def test_budget_caps_load_to_affordable(self):
        pts = [self._pt("Gold", 1, 101, buy=100), self._pt("Gold", 2, 102, sell=200)]
        out = nav_core.rank_trades(nav_core.NavData(), pts, capacity_scu=96,
                                   sort="margin", budget=1000)
        self.assertEqual(out[0]["max_scu"], 10)          # floor(1000 / 100 buy)
        self.assertEqual(out[0]["buy_cost"], 1000)
        self.assertEqual(out[0]["trade_profit"], 10 * 100)

    def test_budget_below_one_unit_drops_trade(self):
        pts = [self._pt("Gold", 1, 101, buy=100), self._pt("Gold", 2, 102, sell=200)]
        out = nav_core.rank_trades(nav_core.NavData(), pts, capacity_scu=96, budget=50)
        self.assertEqual(out[0]["max_scu"], None)        # can't afford a single SCU

    def test_max_age_drops_stale_and_undated_points(self):
        now = 1_000_000.0
        fresh = [self._pt("Gold", 1, 101, buy=100, updated_at=now - 3600),
                 self._pt("Gold", 2, 102, sell=200, updated_at=now - 3600)]
        self.assertEqual(len(nav_core.rank_trades(nav_core.NavData(), fresh,
                         max_age_s=7200, now_ts=now)), 1)
        stale = [self._pt("Gold", 1, 101, buy=100, updated_at=now - 99999),
                 self._pt("Gold", 2, 102, sell=200, updated_at=now - 3600)]
        self.assertEqual(nav_core.rank_trades(nav_core.NavData(), stale,
                         max_age_s=7200, now_ts=now), [])
        undated = [self._pt("Gold", 1, 101, buy=100),          # updated_at None
                   self._pt("Gold", 2, 102, sell=200, updated_at=now)]
        self.assertEqual(nav_core.rank_trades(nav_core.NavData(), undated,
                         max_age_s=7200, now_ts=now), [])

    def test_pairs_buy_low_sell_high(self):
        pts = [self._pt("Gold", 1, 101, buy=100),    # buy here for 100
               self._pt("Gold", 2, 102, sell=180),   # best sell
               self._pt("Gold", 3, 103, sell=150)]   # weaker sell
        out = nav_core.rank_trades(nav_core.NavData(), pts, sort="margin")
        self.assertEqual(out[0]["profit_per_scu"], 80)
        self.assertEqual(out[0]["sell_terminal_id"], 2)
        self.assertEqual(out[0]["buy_terminal_id"], 1)
        self.assertEqual(out[1]["profit_per_scu"], 50)

    def test_no_positive_margin_returns_empty(self):
        pts = [self._pt("Gold", 1, 101, buy=200), self._pt("Gold", 2, 102, sell=150)]
        self.assertEqual(nav_core.rank_trades(nav_core.NavData(), pts), [])

    def test_same_dock_is_not_a_trade(self):
        pts = [self._pt("Gold", 1, 101, buy=100), self._pt("Gold", 2, 101, sell=200)]
        self.assertEqual(nav_core.rank_trades(nav_core.NavData(), pts), [])

    def test_min_margin_filter(self):
        pts = [self._pt("Gold", 1, 101, buy=100), self._pt("Gold", 2, 102, sell=140)]
        self.assertEqual(nav_core.rank_trades(nav_core.NavData(), pts, min_margin=50), [])
        self.assertEqual(len(nav_core.rank_trades(nav_core.NavData(), pts, min_margin=40)), 1)

    def test_commodity_filter(self):
        pts = [self._pt("Gold", 1, 101, buy=100), self._pt("Gold", 2, 102, sell=200),
               self._pt("Iron", 3, 103, buy=10), self._pt("Iron", 4, 104, sell=90)]
        out = nav_core.rank_trades(nav_core.NavData(), pts, commodity="Iron")
        self.assertEqual({r["commodity"] for r in out}, {"Iron"})

    def test_system_filter_excludes_cross_system_pairs(self):
        pts = [self._pt("Gold", 1, 101, system="Stanton", buy=100),
               self._pt("Gold", 2, 102, system="Pyro", sell=300)]
        self.assertEqual(nav_core.rank_trades(nav_core.NavData(), pts, system="Stanton"), [])

    def test_capacity_clamps_to_supply_and_demand(self):
        pts = [self._pt("Gold", 1, 101, buy=100, scu_buy=30),
               self._pt("Gold", 2, 102, sell=200, scu_sell_stock=50)]
        out = nav_core.rank_trades(nav_core.NavData(), pts, capacity_scu=96, sort="margin")
        self.assertEqual(out[0]["max_scu"], 30)          # min(96, 30, 50)
        self.assertEqual(out[0]["trade_profit"], 30 * 100)
        self.assertEqual(out[0]["buy_cost"], 30 * 100)

    def test_unknown_stock_does_not_clamp(self):
        pts = [self._pt("Gold", 1, 101, buy=100), self._pt("Gold", 2, 102, sell=200)]
        out = nav_core.rank_trades(nav_core.NavData(), pts, capacity_scu=96, sort="margin")
        self.assertEqual(out[0]["max_scu"], 96)          # 0 stock = unknown, hold caps it

    def test_per_hour_folds_in_travel_and_capacity(self):
        # Two real Stanton space POIs -> travel_cost yields a finite leg + ETA.
        spc = [p for p in NAV.pois.values() if p.system == "Stanton" and p.global_m][:2]
        a, b = spc[0], spc[1]
        pts = [self._pt("Gold", 1, a.id, buy=100, scu_buy=100),
               self._pt("Gold", 2, b.id, sell=300, scu_sell_stock=100)]
        out = nav_core.rank_trades(NAV, pts, capacity_scu=96, sort="per_hour")
        self.assertEqual(len(out), 1)
        self.assertIsNotNone(out[0]["distance_m"])
        self.assertGreater(out[0]["profit_per_hour"], 0)


class TradeSolverTests(unittest.TestCase):
    """nav_core.plan_trade_route + cost_trade_legs — the multi-leg trade solver
    and manual coster (#21 step 3). Uses three real Stanton space POIs so the
    reused travel_cost yields finite legs."""

    @classmethod
    def setUpClass(cls):
        spc = [p for p in NAV.pois.values() if p.system == "Stanton" and p.global_m][:3]
        cls.A, cls.B, cls.C = (p.id for p in spc)

    def _pt(self, commodity, terminal_id, poi_id, buy=None, sell=None,
            scu_buy=0, scu_sell_stock=0, updated_at=None):
        return {"commodity": commodity, "terminal_id": terminal_id,
                "terminal": f"T{terminal_id}", "system": "Stanton", "poi_id": poi_id,
                "buy": buy, "sell": sell, "scu_buy": scu_buy,
                "scu_sell_stock": scu_sell_stock, "updated_at": updated_at}

    def _prices(self):
        # Gold: buy @A 100 -> sell @B 300.  Iron: buy @B 50 -> sell @C 200.
        return [self._pt("Gold", 1, self.A, buy=100, scu_buy=500),
                self._pt("Gold", 2, self.B, sell=300, scu_sell_stock=500),
                self._pt("Iron", 3, self.B, buy=50, scu_buy=500),
                self._pt("Iron", 4, self.C, sell=200, scu_sell_stock=500)]

    def test_auto_chains_two_legs(self):
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, max_stops=6, sort="profit")
        s = plan["summary"]
        self.assertTrue(s["feasible"])
        self.assertEqual(s["legs"], 2)
        # (300-100)*100 + (200-50)*100
        self.assertEqual(s["total_profit"], 35000)
        self.assertEqual({lg["commodity"] for lg in plan["legs"]}, {"Gold", "Iron"})

    def test_buying_where_you_just_sold_merges_the_stop(self):
        # Sell Gold at B then buy Iron at B -> B is one physical stop, not two.
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, max_stops=6, sort="profit")
        self.assertEqual(plan["summary"]["stops"], 3)   # A(buy) B(sell+buy) C(sell)

    def test_filtered_mode_restricts_commodities(self):
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, commodities=["Gold"], sort="profit")
        self.assertEqual({lg["commodity"] for lg in plan["legs"]}, {"Gold"})

    def test_max_stops_caps_legs(self):
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, max_stops=2, sort="profit")
        self.assertEqual(plan["summary"]["legs"], 1)    # max_stops//2

    def test_excluded_stop_is_dropped_from_both_ends(self):
        # C is unusable (say: a surface outpost, and we're flying a Hull-C). The Iron
        # leg sells at C, so it must vanish — leaving only the Gold leg.
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, max_stops=6, sort="profit",
            exclude_poi_ids=frozenset({self.C}))
        self.assertEqual({lg["commodity"] for lg in plan["legs"]}, {"Gold"})
        for lg in plan["legs"]:
            self.assertNotIn(self.C, (lg["buy_poi_id"], lg["sell_poi_id"]))

    def test_excluding_every_stop_is_infeasible_not_a_bad_route(self):
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, max_stops=6, sort="profit",
            exclude_poi_ids=frozenset({self.A, self.B, self.C}))
        self.assertFalse(plan["summary"]["feasible"])
        self.assertEqual(plan["legs"], [])

    def test_held_cargo_is_never_sold_at_an_unusable_stop(self):
        # The regression that matters: a re-plan must not route a Hull-C to a stop it
        # can't dock at just because that's where the buyer is. B is the only Gold
        # buyer; excluding it must strand the cargo loudly, not sell it there anyway.
        # (Contrast avoid_poi_ids, which the held-sell leg deliberately ignores — you
        # can run a pirate blockade, but you cannot land a Hull-C on a moon.)
        held = {"commodity": "Gold", "scu": 100, "buy_price": 100}
        plan = nav_core.replan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, held=held, max_stops=6,
            exclude_poi_ids=frozenset({self.B}))
        self.assertEqual(plan["legs"], [])
        self.assertIn("STOPS", plan["summary"]["reason"])

    def test_held_cargo_still_sells_at_a_usable_stop(self):
        held = {"commodity": "Gold", "scu": 100, "buy_price": 100}
        plan = nav_core.replan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, held=held, max_stops=6,
            exclude_poi_ids=frozenset({self.C}))
        self.assertTrue(plan["legs"])
        self.assertEqual(plan["legs"][0]["sell_poi_id"], self.B)
        self.assertTrue(plan["legs"][0]["held"])

    def test_no_trades_is_infeasible(self):
        losing = [self._pt("Gold", 1, self.A, buy=300, scu_buy=500),
                  self._pt("Gold", 2, self.B, sell=100, scu_sell_stock=500)]
        plan = nav_core.plan_trade_route(NAV, losing, 100, start_id=self.A)
        self.assertFalse(plan["summary"]["feasible"])
        self.assertEqual(plan["legs"], [])

    def test_start_ref_reports_origin(self):
        plan = nav_core.plan_trade_route(NAV, self._prices(), 100, start_id=self.A)
        self.assertEqual(plan["start"]["id"], self.A)

    def test_manual_costs_given_legs(self):
        legs = [{"commodity": "Gold", "buy_terminal_id": 1, "sell_terminal_id": 2},
                {"commodity": "Iron", "buy_terminal_id": 3, "sell_terminal_id": 4}]
        plan = nav_core.cost_trade_legs(
            NAV, self._prices(), legs, 100, start_id=self.A)
        self.assertEqual(plan["summary"]["legs"], 2)
        self.assertEqual(plan["summary"]["total_profit"], 35000)

    def test_manual_honors_scu_cap(self):
        legs = [{"commodity": "Gold", "buy_terminal_id": 1, "sell_terminal_id": 2, "scu": 10}]
        plan = nav_core.cost_trade_legs(NAV, self._prices(), legs, 100, start_id=self.A)
        self.assertEqual(plan["legs"][0]["scu"], 10)
        self.assertEqual(plan["summary"]["total_profit"], 2000)   # (300-100)*10

    def test_manual_rejects_unknown_terminal(self):
        legs = [{"commodity": "Gold", "buy_terminal_id": 999, "sell_terminal_id": 2}]
        with self.assertRaises(ValueError):
            nav_core.cost_trade_legs(NAV, self._prices(), legs, 100, start_id=self.A)

    def test_budget_caps_solver_capital(self):
        # Gold buy @100: budget 3000 affords 30 SCU even though the 100-SCU hold
        # and 500-SCU supply would allow more -> peak capital never exceeds budget.
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, budget=3000, sort="profit")
        self.assertTrue(plan["summary"]["feasible"])
        self.assertLessEqual(plan["summary"]["peak_capital"], 3000)
        gold = next(lg for lg in plan["legs"] if lg["commodity"] == "Gold")
        self.assertEqual(gold["scu"], 30)                # floor(3000 / 100)

    def test_summary_splits_deadhead_and_loaded_time(self):
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, sort="profit")
        s = plan["summary"]
        self.assertIn("deadhead_time_s", s)
        self.assertIn("loaded_time_s", s)
        self.assertGreater(s["loaded_time_s"], 0)
        self.assertGreaterEqual(s["deadhead_time_s"], 0)
        # move time (deadhead + loaded) plus dwell must reconcile with the total.
        self.assertAlmostEqual(
            s["total_time_s"],
            s["deadhead_time_s"] + s["loaded_time_s"] + 2 * nav_core.STOP_DWELL_S * s["legs"])
        self.assertTrue(0 <= s["loaded_pct"] <= 100)

    def test_legs_carry_price_freshness(self):
        now = 1_000_000.0
        prices = [self._pt("Gold", 1, self.A, buy=100, scu_buy=500, updated_at=now - 10),
                  self._pt("Gold", 2, self.B, sell=300, scu_sell_stock=500, updated_at=now - 20)]
        plan = nav_core.plan_trade_route(NAV, prices, 100, start_id=self.A, sort="profit")
        lg = plan["legs"][0]
        self.assertEqual(lg["buy_updated_at"], now - 10)
        self.assertEqual(lg["sell_updated_at"], now - 20)
        self.assertEqual(plan["summary"]["oldest_updated_at"], now - 20)

    def test_max_age_filters_solver_candidates(self):
        now = 1_000_000.0
        prices = self._prices()
        for p in prices:                                 # make the Gold sell stale
            p["updated_at"] = now if p["terminal_id"] != 2 else now - 99999
        plan = nav_core.plan_trade_route(
            NAV, prices, 100, start_id=self.A, sort="profit",
            max_age_s=7200, now_ts=now)
        self.assertEqual({lg["commodity"] for lg in plan["legs"]}, {"Iron"})

    def test_route_score_penalizes_empty_flight(self):
        # Equal profit, different empty-hold time: weight>1 must rank the fuller
        # (less deadhead) route higher; weight 1 leaves both objectives unchanged.
        low = {"total_profit": 1000, "total_time_s": 4000, "deadhead_time_s": 500}
        high = {"total_profit": 1000, "total_time_s": 4000, "deadhead_time_s": 2000}
        self.assertEqual(nav_core._route_score(low, "profit", 1.0), 1000)
        self.assertGreater(nav_core._route_score(low, "profit", 3.0),
                           nav_core._route_score(high, "profit", 3.0))
        base = nav_core._route_score(low, "per_hour", 1.0)
        self.assertAlmostEqual(base, 1000 / (4000 / 3600.0))
        self.assertGreater(nav_core._route_score(low, "per_hour", 3.0),
                           nav_core._route_score(high, "per_hour", 3.0))


class TradeDangerAvoidTests(unittest.TestCase):
    """nav_core pirate danger-board avoidance (#24): the avoid_poi_ids / avoid_pairs
    solver filter and the trade_avoid_sets / trade_leg_warnings pure helpers. Reuses
    the Gold A->B, Iron B->C fixture from the solver tests."""

    @classmethod
    def setUpClass(cls):
        spc = [p for p in NAV.pois.values() if p.system == "Stanton" and p.global_m][:3]
        cls.A, cls.B, cls.C = (p.id for p in spc)

    def _pt(self, commodity, terminal_id, poi_id, buy=None, sell=None,
            scu_buy=0, scu_sell_stock=0):
        return {"commodity": commodity, "terminal_id": terminal_id,
                "terminal": f"T{terminal_id}", "system": "Stanton", "poi_id": poi_id,
                "buy": buy, "sell": sell, "scu_buy": scu_buy,
                "scu_sell_stock": scu_sell_stock, "updated_at": None}

    def _prices(self):
        return [self._pt("Gold", 1, self.A, buy=100, scu_buy=500),
                self._pt("Gold", 2, self.B, sell=300, scu_sell_stock=500),
                self._pt("Iron", 3, self.B, buy=50, scu_buy=500),
                self._pt("Iron", 4, self.C, sell=200, scu_sell_stock=500)]

    # --- trade_avoid_sets ---------------------------------------------------
    def test_avoid_sets_split_point_and_lane(self):
        warnings = [{"kind": "point", "anchor_a_poi": self.A, "anchor_b_poi": None},
                    {"kind": "lane", "anchor_a_poi": self.B, "anchor_b_poi": self.C}]
        pois, pairs = nav_core.trade_avoid_sets(warnings)
        self.assertEqual(pois, frozenset({self.A}))
        self.assertEqual(pairs, frozenset({frozenset({self.B, self.C})}))

    def test_avoid_sets_skip_unanchored_and_degenerate(self):
        warnings = [{"kind": "point", "anchor_a_poi": None, "anchor_b_poi": None},
                    {"kind": "lane", "anchor_a_poi": self.A, "anchor_b_poi": None},
                    {"kind": "lane", "anchor_a_poi": self.A, "anchor_b_poi": self.A}]
        self.assertEqual(nav_core.trade_avoid_sets(warnings), (frozenset(), frozenset()))

    def test_avoid_sets_empty_input(self):
        self.assertEqual(nav_core.trade_avoid_sets(None), (frozenset(), frozenset()))
        self.assertEqual(nav_core.trade_avoid_sets([]), (frozenset(), frozenset()))

    # --- avoid in the solver ------------------------------------------------
    def test_avoid_poi_drops_every_trade_touching_it(self):
        # B is Gold's sell and Iron's buy — warning at B kills both trades.
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, sort="profit",
            avoid_poi_ids={self.B})
        self.assertEqual(plan["legs"], [])

    def test_avoid_poi_keeps_unaffected_trades(self):
        # C is only Iron's sell — warning at C drops Iron, Gold A->B survives.
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, sort="profit",
            avoid_poi_ids={self.C})
        self.assertEqual({lg["commodity"] for lg in plan["legs"]}, {"Gold"})

    def test_avoid_pair_drops_only_the_snared_lane(self):
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, sort="profit",
            avoid_pairs={frozenset({self.B, self.C})})
        self.assertEqual({lg["commodity"] for lg in plan["legs"]}, {"Gold"})

    def test_no_avoid_matches_baseline(self):
        base = nav_core.plan_trade_route(NAV, self._prices(), 100,
                                         start_id=self.A, sort="profit")
        same = nav_core.plan_trade_route(NAV, self._prices(), 100, start_id=self.A,
                                         sort="profit", avoid_poi_ids=None, avoid_pairs=None)
        self.assertEqual(same["summary"]["total_profit"], base["summary"]["total_profit"])
        self.assertEqual(len(same["legs"]), len(base["legs"]))

    # --- trade_leg_warnings -------------------------------------------------
    def test_leg_warnings_point_touches_an_endpoint(self):
        leg = {"buy_poi_id": self.A, "sell_poi_id": self.B}
        w = {"id": 1, "kind": "point", "anchor_a_poi": self.B, "anchor_b_poi": None,
             "severity": "deadly"}
        self.assertEqual([h["id"] for h in nav_core.trade_leg_warnings(leg, [w])], [1])

    def test_leg_warnings_lane_needs_both_endpoints(self):
        leg = {"buy_poi_id": self.A, "sell_poi_id": self.B}
        exact = {"id": 1, "kind": "lane", "anchor_a_poi": self.B, "anchor_b_poi": self.A,
                 "severity": "active"}   # same pair, order-independent
        partial = {"id": 2, "kind": "lane", "anchor_a_poi": self.A, "anchor_b_poi": self.C,
                   "severity": "active"}  # shares only A -> no transit modeling in v1
        self.assertEqual([h["id"] for h in nav_core.trade_leg_warnings(leg, [exact, partial])], [1])

    def test_leg_warnings_deadliest_first(self):
        leg = {"buy_poi_id": self.A, "sell_poi_id": self.B}
        ws = [{"id": 1, "kind": "point", "anchor_a_poi": self.A, "severity": "sighted"},
              {"id": 2, "kind": "point", "anchor_a_poi": self.B, "severity": "deadly"}]
        self.assertEqual([h["id"] for h in nav_core.trade_leg_warnings(leg, ws)], [2, 1])

    def test_leg_warnings_none_when_untouched(self):
        leg = {"buy_poi_id": self.A, "sell_poi_id": self.B}
        w = {"id": 1, "kind": "point", "anchor_a_poi": self.C, "severity": "deadly"}
        self.assertEqual(nav_core.trade_leg_warnings(leg, [w]), [])


class TradeStockTests(unittest.TestCase):
    """Stock reports (#21): the buy-side avoid_buys solver filter and the
    stock_avoid_buys / trade_leg_stock pure helpers. Reuses the Gold A->B,
    Iron B->C fixture from the danger-avoid tests."""

    @classmethod
    def setUpClass(cls):
        spc = [p for p in NAV.pois.values() if p.system == "Stanton" and p.global_m][:3]
        cls.A, cls.B, cls.C = (p.id for p in spc)

    def _pt(self, commodity, terminal_id, poi_id, buy=None, sell=None,
            scu_buy=0, scu_sell_stock=0):
        return {"commodity": commodity, "terminal_id": terminal_id,
                "terminal": f"T{terminal_id}", "system": "Stanton", "poi_id": poi_id,
                "buy": buy, "sell": sell, "scu_buy": scu_buy,
                "scu_sell_stock": scu_sell_stock, "updated_at": None}

    def _prices(self):
        return [self._pt("Gold", 1, self.A, buy=100, scu_buy=500),
                self._pt("Gold", 2, self.B, sell=300, scu_sell_stock=500),
                self._pt("Iron", 3, self.B, buy=50, scu_buy=500),
                self._pt("Iron", 4, self.C, sell=200, scu_sell_stock=500)]

    # --- stock_avoid_buys ---------------------------------------------------
    def test_avoid_buys_from_out_reports_only(self):
        reports = [{"poi_id": self.A, "commodity": "GOLD", "kind": "out"},
                   {"poi_id": self.B, "commodity": "Iron", "kind": "low", "scu": 10},
                   {"poi_id": None, "commodity": "Gold", "kind": "out"},
                   {"poi_id": self.C, "commodity": "", "kind": "out"}]
        self.assertEqual(nav_core.stock_avoid_buys(reports),
                         frozenset({(self.A, "gold")}))

    def test_avoid_buys_empty_input(self):
        self.assertEqual(nav_core.stock_avoid_buys(None), frozenset())
        self.assertEqual(nav_core.stock_avoid_buys([]), frozenset())

    # --- avoid_buys in the solver -------------------------------------------
    def test_out_report_drops_only_that_buy(self):
        # Gold's buy at A reported out -> Gold gone; Iron (buys at B) survives.
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, sort="profit",
            avoid_buys={(self.A, "gold")})
        self.assertEqual({lg["commodity"] for lg in plan["legs"]}, {"Iron"})

    def test_out_report_does_not_block_selling_there(self):
        # A Gold report anchored at B touches only Gold's *sell* end — nothing
        # buys Gold at B, so both trades survive untouched.
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, sort="profit",
            avoid_buys={(self.B, "gold")})
        self.assertEqual({lg["commodity"] for lg in plan["legs"]}, {"Gold", "Iron"})

    def test_replan_threads_avoid_buys(self):
        # No held cargo -> replan degrades to plan; the filter must still apply.
        plan = nav_core.replan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, sort="profit",
            avoid_buys={(self.A, "gold")})
        self.assertEqual({lg["commodity"] for lg in plan["legs"]}, {"Iron"})

    def test_no_avoid_buys_matches_baseline(self):
        base = nav_core.plan_trade_route(NAV, self._prices(), 100,
                                         start_id=self.A, sort="profit")
        same = nav_core.plan_trade_route(NAV, self._prices(), 100, start_id=self.A,
                                         sort="profit", avoid_buys=None)
        self.assertEqual(same["summary"]["total_profit"], base["summary"]["total_profit"])
        self.assertEqual(len(same["legs"]), len(base["legs"]))

    # --- trade_leg_stock ----------------------------------------------------
    def test_leg_stock_matches_buy_end_and_commodity_only(self):
        leg = {"buy_poi_id": self.A, "sell_poi_id": self.B, "commodity": "Gold"}
        rs = [{"poi_id": self.A, "commodity": "gold", "kind": "low", "created": 1},
              {"poi_id": self.B, "commodity": "Gold", "kind": "out", "created": 2},
              {"poi_id": self.A, "commodity": "Iron", "kind": "out", "created": 3}]
        hits = nav_core.trade_leg_stock(leg, rs)
        self.assertEqual([h["kind"] for h in hits], ["low"])

    def test_leg_stock_out_ranks_before_low(self):
        leg = {"buy_poi_id": self.A, "sell_poi_id": self.B, "commodity": "Gold"}
        rs = [{"poi_id": self.A, "commodity": "Gold", "kind": "low", "created": 5},
              {"poi_id": self.A, "commodity": "Gold", "kind": "out", "created": 1}]
        self.assertEqual([h["kind"] for h in nav_core.trade_leg_stock(leg, rs)],
                         ["out", "low"])

    def test_leg_stock_safe_on_missing_anchor(self):
        self.assertEqual(nav_core.trade_leg_stock({"commodity": "Gold"}, [
            {"poi_id": self.A, "commodity": "Gold", "kind": "out"}]), [])
        self.assertEqual(nav_core.trade_leg_stock(
            {"buy_poi_id": self.A, "commodity": "Gold"}, None), [])

    # --- demand side (sell end) ----------------------------------------------
    def test_avoid_sets_split_by_side(self):
        reports = [{"poi_id": self.A, "commodity": "Gold", "kind": "out"},  # legacy: supply
                   {"poi_id": self.B, "commodity": "Gold", "kind": "out", "side": "demand"},
                   {"poi_id": self.C, "commodity": "Iron", "kind": "low", "side": "demand"}]
        self.assertEqual(nav_core.stock_avoid_buys(reports),
                         frozenset({(self.A, "gold")}))
        self.assertEqual(nav_core.stock_avoid_sells(reports),
                         frozenset({(self.B, "gold")}))

    def test_no_demand_drops_only_that_sell(self):
        # Gold's sell at B reported not buying -> Gold gone; Iron (which BUYS at
        # B and sells at C) survives untouched.
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, sort="profit",
            avoid_sells={(self.B, "gold")})
        self.assertEqual({lg["commodity"] for lg in plan["legs"]}, {"Iron"})

    def test_no_demand_does_not_block_buying_there(self):
        # An Iron demand report at B touches only a *sell* that doesn't exist
        # there (Iron sells at C) — both trades survive.
        plan = nav_core.plan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, sort="profit",
            avoid_sells={(self.B, "iron")})
        self.assertEqual({lg["commodity"] for lg in plan["legs"]}, {"Gold", "Iron"})

    def test_replan_held_cargo_avoids_no_demand_buyer(self):
        # Held Gold aboard; B pays best but was just reported not buying —
        # the held-cargo sell leg must route to the lesser buyer C instead.
        prices = self._prices() + [self._pt("Gold", 5, self.C, sell=250,
                                            scu_sell_stock=500)]
        held = {"commodity": "Gold", "scu": 50, "buy_price": 100}
        free = nav_core.replan_trade_route(
            NAV, prices, 100, start_id=self.A, sort="profit", held=held)
        self.assertEqual(free["legs"][0]["sell_poi_id"], self.B)   # baseline: best price
        steered = nav_core.replan_trade_route(
            NAV, prices, 100, start_id=self.A, sort="profit", held=held,
            avoid_sells={(self.B, "gold")})
        self.assertEqual(steered["legs"][0]["sell_poi_id"], self.C)

    def test_replan_held_cargo_unsellable_when_all_buyers_reported(self):
        held = {"commodity": "Gold", "scu": 50, "buy_price": 100}
        plan = nav_core.replan_trade_route(
            NAV, self._prices(), 100, start_id=self.A, sort="profit", held=held,
            avoid_sells={(self.B, "gold")})       # B is Gold's only buyer
        self.assertEqual(plan["legs"], [])
        self.assertIn("no known buyer", plan["summary"]["reason"])

    def test_leg_stock_demand_matches_sell_end_only(self):
        leg = {"buy_poi_id": self.A, "sell_poi_id": self.B, "commodity": "Gold"}
        rs = [{"poi_id": self.B, "commodity": "Gold", "kind": "out",
               "side": "demand", "created": 1},
              {"poi_id": self.A, "commodity": "Gold", "kind": "out",
               "side": "demand", "created": 2},   # demand report at the BUY end: inert
              {"poi_id": self.B, "commodity": "Gold", "kind": "low",
               "created": 3}]                     # supply report at the SELL end: inert
        hits = nav_core.trade_leg_stock(leg, rs)
        self.assertEqual([(h["side"], h["poi_id"]) for h in hits],
                         [("demand", self.B)])


class TradeReplanTests(unittest.TestCase):
    """nav_core.replan_trade_route — mid-run re-solve from the live position,
    carrying forward sunk (bought-not-sold) cargo (#21 step 5). Uses real Stanton
    space POIs so the reused travel_cost yields finite legs."""

    @classmethod
    def setUpClass(cls):
        spc = [p for p in NAV.pois.values() if p.system == "Stanton" and p.global_m][:4]
        cls.A, cls.B, cls.C, cls.D = (p.id for p in spc)
        cls.posA = next(p for p in NAV.pois.values() if p.id == cls.A).global_m

    def _pt(self, commodity, terminal_id, poi_id, buy=None, sell=None,
            scu_buy=0, scu_sell_stock=0):
        return {"commodity": commodity, "terminal_id": terminal_id,
                "terminal": f"T{terminal_id}", "system": "Stanton", "poi_id": poi_id,
                "buy": buy, "sell": sell, "scu_buy": scu_buy,
                "scu_sell_stock": scu_sell_stock, "updated_at": None}

    def test_no_held_cargo_is_a_plain_replan(self):
        prices = [self._pt("Gold", 1, self.A, buy=100, scu_buy=500),
                  self._pt("Gold", 2, self.B, sell=300, scu_sell_stock=500)]
        plan = nav_core.replan_trade_route(NAV, prices, 100, start_pos=self.posA,
                                           held=None, sort="profit")
        self.assertTrue(plan["summary"]["feasible"])
        self.assertFalse(any(lg.get("held") for lg in plan["legs"]))

    def test_held_cargo_sold_first_at_richest_buyer(self):
        # Holding 40 SCU of Gold bought @100; two buyers, the richer one wins.
        prices = [self._pt("Gold", 2, self.B, sell=250, scu_sell_stock=500),
                  self._pt("Gold", 3, self.C, sell=300, scu_sell_stock=500)]
        held = {"commodity": "Gold", "scu": 40, "buy_price": 100}
        plan = nav_core.replan_trade_route(NAV, prices, 100, start_pos=self.posA,
                                           held=held, sort="profit")
        first = plan["legs"][0]
        self.assertTrue(first["held"])
        self.assertEqual(first["commodity"], "Gold")
        self.assertEqual(first["scu"], 40)
        self.assertEqual(first["buy_cost"], 0)             # already paid — sunk
        self.assertIsNone(first["to_buy"])                 # no empty approach to a buy
        self.assertEqual(first["sell_price"], 300)         # richest reachable buyer
        self.assertEqual(first["profit"], (300 - 100) * 40)
        self.assertEqual(plan["summary"]["carried_commodity"], "Gold")
        self.assertEqual(plan["summary"]["carried_scu"], 40)

    def test_held_sell_then_chains_more_trades(self):
        # After offloading Gold at C, an Iron trade (buy@C -> sell@D) chains on.
        prices = [self._pt("Gold", 2, self.C, sell=300, scu_sell_stock=500),
                  self._pt("Iron", 3, self.C, buy=50, scu_buy=500),
                  self._pt("Iron", 4, self.D, sell=200, scu_sell_stock=500)]
        held = {"commodity": "Gold", "scu": 40, "buy_price": 100}
        plan = nav_core.replan_trade_route(NAV, prices, 100, start_pos=self.posA,
                                           held=held, sort="profit")
        self.assertTrue(plan["legs"][0]["held"])
        self.assertGreaterEqual(len(plan["legs"]), 2)
        self.assertIn("Iron", {lg["commodity"] for lg in plan["legs"]})

    def test_held_leg_uses_no_forward_capital(self):
        # peak_capital across the route ignores the sunk buy (buy already paid).
        prices = [self._pt("Gold", 2, self.C, sell=300, scu_sell_stock=500)]
        held = {"commodity": "Gold", "scu": 40, "buy_price": 100}
        plan = nav_core.replan_trade_route(NAV, prices, 100, start_pos=self.posA,
                                           held=held, sort="profit")
        self.assertEqual(plan["summary"]["peak_capital"], 0)

    def test_unsellable_held_cargo_reports_reason(self):
        # Nobody buys Platinum -> the hold can't be cleared -> infeasible w/ reason.
        prices = [self._pt("Iron", 3, self.C, buy=50, scu_buy=500),
                  self._pt("Iron", 4, self.D, sell=200, scu_sell_stock=500)]
        held = {"commodity": "Platinum", "scu": 40, "buy_price": 100}
        plan = nav_core.replan_trade_route(NAV, prices, 100, start_pos=self.posA, held=held)
        self.assertFalse(plan["summary"]["feasible"])
        self.assertIn("Platinum", plan["summary"]["reason"])
        self.assertEqual(plan["legs"], [])


class TradeLegRealizedTests(unittest.TestCase):
    """nav_core.trade_leg_realized — realized profit from entered actuals, falling
    back to the plan when a side (or the whole leg) was left unentered (#21 step 5,
    actual-figures pass)."""

    def _leg(self, **over):
        base = {"buy_price": 100, "sell_price": 300, "scu": 40, "profit": 8000}
        base.update(over)
        return base

    def test_no_actuals_returns_planned(self):
        self.assertEqual(nav_core.trade_leg_realized(self._leg()), 8000)

    def test_full_actuals_override(self):
        # bought 40 @110, sold 40 @320 -> (320-110)*40
        leg = self._leg(actual_buy_price=110, actual_buy_scu=40,
                        actual_sell_price=320, actual_sell_scu=40)
        self.assertEqual(nav_core.trade_leg_realized(leg), (320 - 110) * 40)

    def test_partial_actuals_fall_back_per_side(self):
        # only the sell was entered (got 350); buy falls back to planned 100/40.
        leg = self._leg(actual_sell_price=350)
        self.assertEqual(nav_core.trade_leg_realized(leg), 350 * 40 - 100 * 40)

    def test_actual_scu_only(self):
        # moved only 25 SCU (short fill); prices planned.
        leg = self._leg(actual_buy_scu=25, actual_sell_scu=25)
        self.assertEqual(nav_core.trade_leg_realized(leg), (300 - 100) * 25)

    def test_held_leg_buy_falls_back_to_sunk_price(self):
        # held cargo: no actual buy entered -> sunk buy_price/scu; sell actual given.
        leg = self._leg(held=True, buy_price=100, actual_sell_price=280, actual_sell_scu=40)
        self.assertEqual(nav_core.trade_leg_realized(leg), 280 * 40 - 100 * 40)


class TradeHistoryStatsTests(unittest.TestCase):
    """nav_core trade history/statistics derivations (#21 step 6): realized totals,
    quick-picks, guild aggregates, and the top-traders board over trade_runs
    blobs. POIs 0..3 resolve; 999 never does (drops stale lanes)."""

    def setUp(self):
        self.nav = _line_nav([(0, 0, 0), (10, 0, 0), (20, 0, 0), (30, 0, 0)])

    def _leg(self, commodity, b_poi, s_poi, scu, buy, sell, **over):
        """A sold-leg record shaped like _cost_route emits (terminal id == poi id
        here for simplicity)."""
        leg = {
            "commodity": commodity,
            "buy_terminal_id": b_poi, "buy_terminal": f"T{b_poi}",
            "buy_poi_id": b_poi, "buy_system": "Test",
            "sell_terminal_id": s_poi, "sell_terminal": f"T{s_poi}",
            "sell_poi_id": s_poi, "sell_system": "Test",
            "buy_price": buy, "sell_price": sell, "scu": scu,
            "profit": (sell - buy) * scu,
        }
        leg.update(over)
        return leg

    def _run(self, did, ship, legs, dist=100.0, time_s=3600.0, **extra):
        return {"discord_id": did, "ship": ship, "legs": legs,
                "leg_states": ["sold"] * len(legs),
                "summary": {"total_distance_m": dist, "total_time_s": time_s}, **extra}

    def test_realized_prefers_actuals(self):
        # planned 8000, but actual sell price 320 on 40 SCU bought @110 -> 8400.
        leg = self._leg("Gold", 0, 1, 40, 100, 300,
                        actual_buy_price=110, actual_sell_price=320)
        run = self._run("a", "Caterpillar", [leg])
        self.assertEqual(nav_core.trade_run_realized(run), (320 - 110) * 40)
        self.assertEqual(nav_core.trade_run_scu(run), 40.0)

    def test_only_sold_legs_count(self):
        legs = [self._leg("Gold", 0, 1, 40, 100, 300),
                self._leg("Iron", 1, 2, 20, 50, 90)]
        run = self._run("a", "Cat", legs)
        run["leg_states"] = ["sold", "pending"]     # 2nd not transacted yet
        self.assertEqual(nav_core.trade_run_realized(run), (300 - 100) * 40)
        self.assertEqual(nav_core.trade_run_scu(run), 40.0)

    def test_skipped_legs_never_count_as_realized(self):
        # A bailed / stock-out leg parks in 'sold' to move the cursor but was
        # never transacted — planned profit and SCU must not leak into stats.
        legs = [self._leg("Gold", 0, 1, 40, 100, 300),
                self._leg("Iron", 1, 2, 20, 50, 90, skipped=True, stockout=True)]
        run = self._run("a", "Cat", legs)          # states: all 'sold'
        self.assertEqual(nav_core.trade_run_realized(run), (300 - 100) * 40)
        self.assertEqual(nav_core.trade_run_scu(run), 40.0)

    def test_skipped_legs_dropped_on_the_stateless_fallback_too(self):
        legs = [self._leg("Gold", 0, 1, 40, 100, 300),
                self._leg("Iron", 1, 2, 20, 50, 90, skipped=True)]
        run = self._run("a", "Cat", legs)
        run["leg_states"] = ["sold"]               # mis-sized -> fallback path
        self.assertEqual(nav_core.trade_run_realized(run), (300 - 100) * 40)

    def test_run_stats_headline_and_per_hour(self):
        runs = [
            self._run("a", "Cat", [self._leg("Gold", 0, 1, 40, 100, 300)],
                      dist=1000, time_s=3600),                      # profit 8000, 1h
            self._run("a", "Cat", [self._leg("Iron", 1, 2, 10, 50, 150)],
                      dist=500, time_s=3600),                       # profit 1000, 1h
        ]
        s = nav_core.derive_trade_run_stats(runs)
        self.assertEqual(s["num_runs"], 2)
        self.assertEqual(s["total_profit"], 9000)
        self.assertEqual(s["total_scu"], 50.0)
        self.assertEqual(s["total_distance_m"], 1500.0)
        self.assertEqual(s["auec_per_hour"], 4500.0)               # 9000 / 2h

    def test_run_stats_empty(self):
        s = nav_core.derive_trade_run_stats([])
        self.assertEqual(s["num_runs"], 0)
        self.assertEqual(s["total_profit"], 0)
        self.assertIsNone(s["auec_per_hour"])

    def test_quick_picks_lanes_commodities_ships(self):
        runs = [
            self._run("a", "Caterpillar", [self._leg("Gold", 0, 1, 40, 100, 300)]),
            self._run("a", "Caterpillar", [self._leg("Gold", 0, 1, 40, 100, 300)]),
            self._run("a", "Freelancer", [self._leg("Iron", 1, 2, 20, 50, 90)]),
        ]
        picks = nav_core.derive_trade_quick_picks(self.nav, runs)
        # Gold@0->1 run twice, so it leads; carries terminals for one-click re-entry.
        self.assertEqual(picks["lanes"][0]["commodity"], "Gold")
        self.assertEqual((picks["lanes"][0]["buy_terminal_id"],
                          picks["lanes"][0]["sell_terminal_id"]), (0, 1))
        self.assertEqual(picks["lanes"][0]["count"], 2)
        self.assertEqual([c["commodity"] for c in picks["commodities"]], ["Gold", "Iron"])
        self.assertEqual(picks["commodities"][0]["scu"], 40.0)     # most-moved amount
        self.assertEqual(picks["ships"][0]["ship"], "Caterpillar")

    def test_quick_picks_drop_unresolvable_lane(self):
        runs = [self._run("a", "Cat", [self._leg("x", 0, 999, 10, 10, 20)])]
        self.assertEqual(nav_core.derive_trade_quick_picks(self.nav, runs)["lanes"], [])

    def test_held_leg_counts_commodity_not_lane(self):
        # A carried-cargo leg (re-plan) has no real buy terminal.
        leg = self._leg("Gold", None, 1, 40, 100, 300, held=True,
                        buy_terminal_id=None, buy_terminal="carried cargo")
        picks = nav_core.derive_trade_quick_picks(self.nav, [self._run("a", "Cat", [leg])])
        self.assertEqual(picks["lanes"], [])
        self.assertEqual(picks["commodities"][0]["commodity"], "Gold")

    def test_guild_stats_totals_and_boards(self):
        runs = [
            self._run("a", "Cat", [self._leg("Gold", 0, 1, 30, 100, 300)]),
            self._run("b", "Freelancer", [self._leg("Iron", 1, 2, 80, 50, 90)]),
        ]
        s = nav_core.derive_guild_trade_stats(self.nav, runs)
        self.assertEqual(s["num_runs"], 2)
        self.assertEqual(s["num_traders"], 2)
        # Iron moved 80 SCU > Gold 30, so Iron leads the commodity board.
        self.assertEqual([c["commodity"] for c in s["top_commodities"]], ["Iron", "Gold"])
        self.assertEqual({l["commodity"] for l in s["top_lanes"]}, {"Gold", "Iron"})

    def test_leaderboard_one_row_per_member(self):
        runs = [
            self._run("a", "Cat", [self._leg("Gold", 0, 1, 40, 100, 300)],
                      display_name="Ana"),
            self._run("a", "Cat", [self._leg("Gold", 0, 1, 40, 100, 300)]),
            self._run("b", "Cat", [self._leg("Iron", 1, 2, 10, 50, 150)]),
        ]
        rows = {r["discord_id"]: r for r in nav_core.derive_trade_leaderboard(runs)}
        self.assertEqual(set(rows), {"a", "b"})
        self.assertEqual(rows["a"]["num_runs"], 2)
        self.assertEqual(rows["a"]["total_profit"], 16000)
        self.assertEqual(rows["a"]["display_name"], "Ana")


def _space_poi(pid, name, xyz, system="Test", qt=True):
    """A directly-QT-able space POI at a fixed global position — the minimal
    fixture for the snare-detour geometry tests (no rotating body involved)."""
    return nav_core.Poi(
        id=pid, name=name, system=system, container_name=None, type="Station",
        local_km=None, global_m=(float(xyz[0]), float(xyz[1]), float(xyz[2])),
        latitude=None, longitude=None, height_m=None, qt_marker=qt)


def _synthetic_nav(pois, system="Test"):
    nav = nav_core.NavData()
    for p in pois:
        nav.pois[p.id] = p
    nav.systems = [system]
    nav_core.index_qt_markers(nav)
    return nav


class SegmentGeometryTests(unittest.TestCase):
    """Closed-form segment distance primitives behind snare-detour hazard tests
    (#24 v2). Known-answer cases: parallel, crossing, skew, clamped, degenerate."""

    def test_seg_point_perpendicular(self):
        self.assertAlmostEqual(
            nav_core._seg_point_dist((0, 0, 0), (10, 0, 0), (5, 5, 0)), 5.0)

    def test_seg_point_on_segment_is_zero(self):
        self.assertAlmostEqual(
            nav_core._seg_point_dist((0, 0, 0), (10, 0, 0), (5, 0, 0)), 0.0)

    def test_seg_point_clamps_past_each_end(self):
        self.assertAlmostEqual(
            nav_core._seg_point_dist((0, 0, 0), (10, 0, 0), (-5, 0, 0)), 5.0)
        self.assertAlmostEqual(
            nav_core._seg_point_dist((0, 0, 0), (10, 0, 0), (15, 0, 0)), 5.0)

    def test_seg_point_degenerate_segment(self):
        self.assertAlmostEqual(
            nav_core._seg_point_dist((0, 0, 0), (0, 0, 0), (3, 4, 0)), 5.0)

    def test_seg_seg_parallel(self):
        self.assertAlmostEqual(
            nav_core._seg_seg_dist((0, 0, 0), (10, 0, 0), (0, 5, 0), (10, 5, 0)), 5.0)

    def test_seg_seg_crossing_is_zero(self):
        self.assertAlmostEqual(
            nav_core._seg_seg_dist((0, 0, 0), (10, 0, 0), (5, -5, 0), (5, 5, 0)), 0.0)

    def test_seg_seg_skew(self):
        # seg1 along x at z=0, seg2 along y at z=4 crossing over x=5 -> gap is 4.
        self.assertAlmostEqual(
            nav_core._seg_seg_dist((0, 0, 0), (10, 0, 0), (5, -5, 4), (5, 5, 4)), 4.0)

    def test_seg_seg_clamped_endpoints(self):
        self.assertAlmostEqual(
            nav_core._seg_seg_dist((0, 0, 0), (10, 0, 0), (20, 0, 0), (20, 10, 0)), 10.0)

    def test_seg_seg_both_degenerate(self):
        self.assertAlmostEqual(
            nav_core._seg_seg_dist((0, 0, 0), (0, 0, 0), (3, 4, 0), (3, 4, 0)), 5.0)


class HazardVolumeTests(unittest.TestCase):
    """nav_core.hazard_volumes — danger warnings + personal blacklist -> the
    sphere/capsule volumes the detour engine tests against (#24 v2)."""

    def setUp(self):
        self.A = _space_poi(1, "A", (0, 0, 0))
        self.B = _space_poi(2, "B", (20e6, 0, 0))
        self.nav = _synthetic_nav([self.A, self.B])

    def test_point_warning_is_a_sphere(self):
        w = {"id": 7, "kind": "point", "severity": "active",
             "anchor_a_poi": 1, "anchor_b_poi": None}
        vols = nav_core.hazard_volumes(self.nav, [w], 0.0, radius_m=1e6)
        self.assertEqual(len(vols), 1)
        v = vols[0]
        self.assertEqual(v["kind"], "sphere")
        self.assertEqual(v["a"], (0.0, 0.0, 0.0))
        self.assertIsNone(v["b"])
        self.assertEqual(v["r"], 1e6)                  # active -> ×1.0
        self.assertEqual(v["warning_id"], 7)
        self.assertEqual(v["system"], "Test")

    def test_lane_warning_is_a_capsule(self):
        w = {"id": 8, "kind": "lane", "severity": "deadly",
             "anchor_a_poi": 1, "anchor_b_poi": 2}
        vols = nav_core.hazard_volumes(self.nav, [w], 0.0, radius_m=1e6)
        v = vols[0]
        self.assertEqual(v["kind"], "capsule")
        self.assertEqual(v["a"], (0.0, 0.0, 0.0))
        self.assertEqual(v["b"], (20e6, 0.0, 0.0))
        self.assertEqual(v["r"], 1.5e6)                # deadly -> ×1.5

    def test_severity_scaling(self):
        def r(sev):
            w = {"id": 1, "kind": "point", "severity": sev, "anchor_a_poi": 1}
            return nav_core.hazard_volumes(self.nav, [w], 0.0, radius_m=1000.0)[0]["r"]
        self.assertEqual(r("sighted"), 500.0)
        self.assertEqual(r("active"), 1000.0)
        self.assertEqual(r("deadly"), 1500.0)

    def test_unanchored_and_unknown_contribute_nothing(self):
        ws = [{"id": 1, "kind": "point", "severity": "active", "anchor_a_poi": None},
              {"id": 2, "kind": "lane", "severity": "active",
               "anchor_a_poi": 1, "anchor_b_poi": None},
              {"id": 3, "kind": "point", "severity": "active", "anchor_a_poi": 999}]
        self.assertEqual(nav_core.hazard_volumes(self.nav, ws, 0.0), [])

    def test_blacklist_ids_become_spheres(self):
        vols = nav_core.hazard_volumes(self.nav, [], 0.0, radius_m=1e6,
                                       extra_point_ids=[2])
        self.assertEqual(len(vols), 1)
        self.assertEqual(vols[0]["kind"], "sphere")
        self.assertEqual(vols[0]["a"], (20e6, 0.0, 0.0))
        self.assertEqual(vols[0]["r"], 1e6)            # blacklist -> ×1.0
        self.assertIsNone(vols[0]["warning_id"])


class TravelCostAvoidTests(unittest.TestCase):
    """travel_cost(avoid=, memo=) + _detour_via — the snare-detour engine (#24 v2).
    Uses a synthetic 3-marker system so the geometry is exact and controllable."""

    def setUp(self):
        # A --- (capsule across the middle) --- B ; W is off to the side.
        self.A = _space_poi(1, "A", (0, 0, 0))
        self.B = _space_poi(2, "B", (20e6, 0, 0))
        self.W = _space_poi(3, "W-marker", (10e6, 10e6, 0))
        self.nav = _synthetic_nav([self.A, self.B, self.W])
        # A vertical capsule at x=10e6 spanning y∈[-5e6,5e6], radius 2e6 — the
        # direct A->B line pierces it; the A->W->B dogleg clears it.
        self.capsule = {"kind": "capsule", "a": (10e6, -5e6, 0), "b": (10e6, 5e6, 0),
                        "r": 2e6, "warning_id": 42, "system": "Test"}

    def test_avoid_none_is_byte_identical(self):
        base = nav_core._base_travel_cost(self.nav, self.A, self.B, 0.0)
        self.assertEqual(nav_core.travel_cost(self.nav, self.A, self.B, 0.0), base)
        self.assertEqual(
            nav_core.travel_cost(self.nav, self.A, self.B, 0.0, avoid=[]), base)
        # No detour keys leak onto the fast path.
        for k in ("waypoints", "detour_m", "dodged", "blocked"):
            self.assertNotIn(k, nav_core.travel_cost(self.nav, self.A, self.B, 0.0))

    def test_avoid_none_matches_on_real_data(self):
        spc = [p for p in NAV.pois.values() if p.system == "Stanton" and p.global_m][:5]
        for a in spc:
            for b in spc:
                if a is b:
                    continue
                self.assertEqual(
                    nav_core.travel_cost(NAV, a, b, avoid=None),
                    nav_core._base_travel_cost(NAV, a, b))

    def test_detour_inserts_waypoint(self):
        leg = nav_core.travel_cost(self.nav, self.A, self.B, 0.0, avoid=[self.capsule])
        self.assertEqual([w["id"] for w in leg["waypoints"]], [3])
        self.assertGreater(leg["detour_m"], 0)
        self.assertEqual(leg["dodged"], [42])
        self.assertNotIn("blocked", leg)
        # distance_m folds in the honest detour distance.
        direct = nav_core._base_travel_cost(self.nav, self.A, self.B, 0.0)["distance_m"]
        self.assertAlmostEqual(leg["distance_m"], direct + leg["detour_m"])

    def test_clear_leg_gets_no_detour(self):
        far = {"kind": "sphere", "a": (0, -50e6, 0), "b": None, "r": 1e6,
               "warning_id": 9, "system": "Test"}
        leg = nav_core.travel_cost(self.nav, self.A, self.B, 0.0, avoid=[far])
        for k in ("waypoints", "detour_m", "dodged", "blocked"):
            self.assertNotIn(k, leg)

    def test_endpoint_inside_volume_is_blocked(self):
        camped = {"kind": "sphere", "a": (20e6, 0, 0), "b": None, "r": 3e6,
                  "warning_id": 5, "system": "Test"}
        leg = nav_core.travel_cost(self.nav, self.A, self.B, 0.0, avoid=[camped])
        self.assertEqual(leg["blocked"], [5])
        self.assertNotIn("waypoints", leg)
        self.assertNotIn("dodged", leg)

    def test_no_clearing_marker_is_blocked(self):
        # Drop W: only A and B remain, and neither clears the capsule.
        nav = _synthetic_nav([self.A, self.B])
        leg = nav_core.travel_cost(nav, self.A, self.B, 0.0, avoid=[self.capsule])
        self.assertEqual(leg["blocked"], [42])
        self.assertNotIn("waypoints", leg)

    def test_other_system_volumes_are_ignored(self):
        elsewhere = dict(self.capsule, system="Pyro")
        leg = nav_core.travel_cost(self.nav, self.A, self.B, 0.0, avoid=[elsewhere])
        self.assertNotIn("blocked", leg)
        self.assertNotIn("waypoints", leg)

    def test_leg_hazards_flags_flypast_without_rerouting(self):
        ids = nav_core.leg_hazards(self.nav, self.A, self.B, [self.capsule], 0.0)
        self.assertEqual(ids, [42])
        # blacklist volume (warning_id None) contributes no id.
        anon = dict(self.capsule, warning_id=None)
        self.assertEqual(nav_core.leg_hazards(self.nav, self.A, self.B, [anon], 0.0), [])


class SnareDetourSolverTests(unittest.TestCase):
    """Solver-level snare-detour behavior (#24 v2): trade legs detoured not
    dropped, camped endpoints still dropped, manual/cargo blocked-badging, and
    the avoid_volumes=None fast path staying byte-identical."""

    def setUp(self):
        self.A = _space_poi(1, "A", (0, 0, 0))
        self.B = _space_poi(2, "B", (20e6, 0, 0))
        self.W = _space_poi(3, "W-marker", (10e6, 10e6, 0))
        self.nav = _synthetic_nav([self.A, self.B, self.W])
        self.capsule = {"kind": "capsule", "a": (10e6, -5e6, 0), "b": (10e6, 5e6, 0),
                        "r": 2e6, "warning_id": 42, "system": "Test"}

    def _pt(self, commodity, terminal_id, poi_id, buy=None, sell=None,
            scu_buy=0, scu_sell_stock=0):
        return {"commodity": commodity, "terminal_id": terminal_id,
                "terminal": f"T{terminal_id}", "system": "Test", "poi_id": poi_id,
                "buy": buy, "sell": sell, "scu_buy": scu_buy,
                "scu_sell_stock": scu_sell_stock, "updated_at": None}

    def _prices(self):
        # Gold: buy @A -> sell @B (the snared lane).
        return [self._pt("Gold", 1, 1, buy=100, scu_buy=500),
                self._pt("Gold", 2, 2, sell=300, scu_sell_stock=500)]

    def test_snared_lane_detoured_not_dropped(self):
        plan = nav_core.plan_trade_route(
            self.nav, self._prices(), 100, start_id=1, sort="profit",
            avoid_volumes=[self.capsule])
        self.assertEqual([lg["commodity"] for lg in plan["legs"]], ["Gold"])
        haul = plan["legs"][0]["haul"]
        self.assertEqual([w["id"] for w in haul["waypoints"]], [3])
        self.assertEqual(haul["dodged"], [42])

    def test_camped_sell_endpoint_still_dropped(self):
        # Sphere on B (the sell terminal) — no detour saves a camped destination,
        # so the blocked haul is skipped and the plan is empty.
        camped = {"kind": "sphere", "a": (20e6, 0, 0), "b": None, "r": 3e6,
                  "warning_id": 5, "system": "Test"}
        plan = nav_core.plan_trade_route(
            self.nav, self._prices(), 100, start_id=1, sort="profit",
            avoid_volumes=[camped])
        self.assertEqual(plan["legs"], [])

    def test_manual_leg_blocked_but_never_dropped(self):
        camped = {"kind": "sphere", "a": (20e6, 0, 0), "b": None, "r": 3e6,
                  "warning_id": 5, "system": "Test"}
        legs = [{"commodity": "Gold", "buy_terminal_id": 1, "sell_terminal_id": 2}]
        plan = nav_core.cost_trade_legs(
            self.nav, self._prices(), legs, 100, start_id=1, avoid_volumes=[camped])
        self.assertEqual(len(plan["legs"]), 1)               # never dropped
        self.assertEqual(plan["legs"][0]["haul"]["blocked"], [5])

    def test_cargo_plan_detours_and_flags(self):
        pkgs = [{"id": 1, "commodity": "Gold", "scu": 10, "from_id": 1, "to_id": 2}]
        plan = nav_core.plan_route(self.nav, pkgs, 100, start_id=1,
                                   avoid_volumes=[self.capsule])
        self.assertTrue(plan["summary"]["feasible"])
        arrival = plan["stops"][-1]["leg"]                   # the A->B haul
        self.assertEqual([w["id"] for w in arrival["waypoints"]], [3])
        self.assertEqual(arrival["dodged"], [42])

    def test_cargo_avoid_none_is_identical(self):
        pkgs = [{"id": 1, "commodity": "Gold", "scu": 10, "from_id": 1, "to_id": 2}]
        base = nav_core.plan_route(self.nav, pkgs, 100, start_id=1)
        same = nav_core.plan_route(self.nav, pkgs, 100, start_id=1, avoid_volumes=None)
        self.assertEqual(same, base)


class QuantumFuelRangeTests(unittest.TestCase):
    """#27 — quantum fuel burn + max-range annotation/constraint in both planners.
    Distances use a toy line system (1 Gm = 1e9 m) so fuel math is exact."""

    def _nav3(self):
        return _line_nav([(0, 0, 0), (1e9, 0, 0), (2e9, 0, 0)])

    # --- helper ---
    def test_leg_fuel_scu_unit_conversion(self):
        # fuel_req is SCU/Gm; 1 Gm at 0.5 -> 0.5 SCU
        self.assertAlmostEqual(nav_core.leg_fuel_scu(1e9, 0.5), 0.5)
        self.assertAlmostEqual(nav_core.leg_fuel_scu(2e9, 0.0098), 0.0196)
        self.assertIsNone(nav_core.leg_fuel_scu(1e9, None))   # unknown ship
        self.assertIsNone(nav_core.leg_fuel_scu(None, 0.5))   # unroutable leg

    # --- cargo ---
    def test_cargo_leg_annotation_and_over_range_boundary(self):
        nav = _line_nav([(0, 0, 0), (1e9, 0, 0)])
        pkgs = [{"id": "A", "commodity": "x", "scu": 10, "from_id": 0, "to_id": 1}]
        res = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0,
                                  fuel_req=0.5, max_range_m=2e9)
        leg = next(s["leg"] for s in res["stops"] if s["stop_id"] == 1)
        self.assertAlmostEqual(leg["fuel_scu"], 0.5)          # 1 Gm @ 0.5
        self.assertFalse(leg["over_range"])                   # 1 Gm < 2 Gm tank
        tight = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0,
                                    fuel_req=0.5, max_range_m=0.5e9)
        leg = next(s["leg"] for s in tight["stops"] if s["stop_id"] == 1)
        self.assertTrue(leg["over_range"])                    # 1 Gm > 0.5 Gm tank

    def test_cargo_summary_fuel_totals(self):
        nav = self._nav3()
        pkgs = [{"id": "A", "commodity": "x", "scu": 10, "from_id": 0, "to_id": 1},
                {"id": "B", "commodity": "y", "scu": 10, "from_id": 0, "to_id": 2}]
        res = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0,
                                  fuel_req=0.5, max_range_m=2e9)
        s = res["summary"]
        self.assertAlmostEqual(s["total_fuel_scu"], 1.0)      # two 1-Gm legs @ 0.5
        self.assertEqual(s["over_range_count"], 0)
        self.assertAlmostEqual(s["worst_leg_m"], 1e9)

    def test_cargo_in_range_only_infeasible_flags_range(self):
        nav = self._nav3()
        pkgs = [{"id": "A", "commodity": "x", "scu": 10, "from_id": 0, "to_id": 2}]
        blocked = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0,
                                      fuel_req=0.5, max_range_m=1e9, in_range_only=True)
        self.assertFalse(blocked["summary"]["feasible"])      # only hop is 2 Gm
        self.assertTrue(blocked["summary"]["range_infeasible"])
        ok = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0,
                                 fuel_req=0.5, max_range_m=3e9, in_range_only=True)
        self.assertTrue(ok["summary"]["feasible"])

    def test_cargo_unknown_ship_adds_no_fuel_fields(self):
        nav = self._nav3()
        pkgs = [{"id": "A", "commodity": "x", "scu": 10, "from_id": 0, "to_id": 1}]
        base = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0)
        withq = nav_core.plan_route(nav, pkgs, usable_scu=100, start_id=0)
        self.assertEqual(base, withq)
        self.assertNotIn("total_fuel_scu", base["summary"])
        for st in base["stops"]:
            if st["leg"]:
                self.assertNotIn("fuel_scu", st["leg"])

    # --- trade ---
    def _trade_setup(self):
        spc = [p for p in NAV.pois.values() if p.system == "Stanton" and p.global_m][:3]
        A, B, C = (p.id for p in spc)

        def pt(commodity, tid, poi, buy=None, sell=None, scu_buy=0, scu_sell_stock=0):
            return {"commodity": commodity, "terminal_id": tid, "terminal": f"T{tid}",
                    "system": "Stanton", "poi_id": poi, "buy": buy, "sell": sell,
                    "scu_buy": scu_buy, "scu_sell_stock": scu_sell_stock, "updated_at": None}
        prices = [pt("Gold", 1, A, buy=100, scu_buy=500),
                  pt("Gold", 2, B, sell=300, scu_sell_stock=500)]
        return A, B, C, prices

    def test_trade_leg_gets_fuel_annotation(self):
        A, B, C, prices = self._trade_setup()
        plan = nav_core.plan_trade_route(NAV, prices, 100, start_id=A, sort="profit",
                                         fuel_req=0.01)
        self.assertTrue(plan["summary"]["feasible"])
        haul = plan["legs"][0]["haul"]
        self.assertIsNotNone(haul["fuel_scu"])
        self.assertAlmostEqual(haul["fuel_scu"],
                               nav_core.leg_fuel_scu(haul["distance_m"], 0.01))
        self.assertIn("total_fuel_scu", plan["summary"])

    def test_trade_in_range_only_drops_over_range_trade(self):
        A, B, C, prices = self._trade_setup()
        # measure the real haul distance with no drive, then squeeze the tank under it
        base = nav_core.plan_trade_route(NAV, prices, 100, start_id=A, sort="profit")
        haul_m = base["legs"][0]["haul"]["distance_m"]
        dropped = nav_core.plan_trade_route(NAV, prices, 100, start_id=A, sort="profit",
                                            fuel_req=0.01, max_range_m=haul_m / 2,
                                            in_range_only=True)
        self.assertEqual(dropped["legs"], [])                 # over-range trade unusable
        kept = nav_core.plan_trade_route(NAV, prices, 100, start_id=A, sort="profit",
                                         fuel_req=0.01, max_range_m=haul_m / 2)
        self.assertEqual(len(kept["legs"]), 1)                # off: kept but flagged
        self.assertTrue(kept["legs"][0]["haul"]["over_range"])

    def test_trade_manual_annotates_but_never_drops(self):
        A, B, C, prices = self._trade_setup()
        legs = [{"commodity": "Gold", "buy_terminal_id": 1, "sell_terminal_id": 2}]
        base = nav_core.cost_trade_legs(NAV, prices, legs, 100, start_id=A)
        haul_m = base["legs"][0]["haul"]["distance_m"]
        plan = nav_core.cost_trade_legs(NAV, prices, legs, 100, start_id=A,
                                        fuel_req=0.01, max_range_m=haul_m / 2)
        self.assertEqual(len(plan["legs"]), 1)                # manual legs never dropped
        self.assertTrue(plan["legs"][0]["haul"]["over_range"])


class QuantumDataArtifactTest(unittest.TestCase):
    """Backlog #26 — keep the committed poi/quantum_*.json artifacts honest.

    These files are distilled offline by tools/sync_quantum.py from the SC Wiki
    API and committed; this guards them against silent corruption on refresh.
    """

    @classmethod
    def setUpClass(cls):
        import json
        cls.drives = json.loads((DATA_DIR / "quantum_drives.json").read_text())
        cls.profiles = json.loads((DATA_DIR / "quantum_profiles.json").read_text())

    def test_meta_stamped_with_game_version(self):
        for doc in (self.drives, self.profiles):
            self.assertIn("game_version", doc["_meta"])
            self.assertEqual(doc["_meta"]["license"], "CC BY-SA 4.0")

    def test_drive_catalog_wellformed(self):
        cat = self.drives["drives"]
        self.assertGreater(len(cat), 40)
        for cn, d in cat.items():
            self.assertGreater(d["fuel_req"], 0, cn)      # SCU/Gm, must be positive
            self.assertIn(d["size"], (1, 2, 3, 4), cn)

    def test_every_profile_has_exactly_one_default_drive(self):
        for slug, p in self.profiles["profiles"].items():
            self.assertTrue(p["drives"], f"{slug} has no drives")
            defaults = [d for d in p["drives"] if d["is_default"]]
            self.assertEqual(len(defaults), 1, f"{slug} default count {len(defaults)}")

    def test_default_drive_range_matches_wiki_range(self):
        # the identity fuel_capacity / fuel_req == max_range_Gm, within rounding
        for slug, p in self.profiles["profiles"].items():
            dfl = next(d for d in p["drives"] if d["is_default"])
            self.assertAlmostEqual(dfl["range_m"], p["wiki_range_m"],
                                   delta=p["wiki_range_m"] * 0.01, msg=slug)

    def test_per_drive_range_identity(self):
        for slug, p in self.profiles["profiles"].items():
            cap = p["fuel_scu"]
            for d in p["drives"]:
                expect = round(cap / d["fuel_req"] * 1e9)
                # synthetic stock drives carry the wiki range verbatim; both agree to rounding
                self.assertAlmostEqual(d["range_m"], expect, delta=max(expect * 1e-6, 2),
                                       msg=f"{slug}/{d['name']}")

    def test_uexcorp_map_points_at_real_profiles(self):
        slugs = set(self.profiles["profiles"])
        self.assertGreater(len(self.profiles["uexcorp"]), 80)
        for name_full, slug in self.profiles["uexcorp"].items():
            self.assertIn(slug, slugs, name_full)


class BlueprintCommissionTests(unittest.TestCase):
    """Pure blueprint helpers for craft commissions (#25) — manifest math,
    stat-driver inversion, quality interpolation, and the board quote state.
    Fixtures mirror real feed records (Omnisky III / LumaCore shapes)."""

    # An Omnisky-shaped weapon: one resource aspect + two gem aspects, the two
    # gems both driving the same stat (cross-aspect stacking).
    WEAPON = {
        "name": "Omnisky III Cannon", "cat": "Weapon Gun", "time_s": 540,
        "default": False,
        "aspects": [
            {"slot": "Frame", "kind": "resource", "input": "Agricium",
             "scu": 0.36, "min_q": 1,
             "mods": [{"prop": "Integrity", "dir": "higher", "mode": "multiplier",
                       "ranges": [{"q0": 0, "q1": 1000, "v0": 0.9, "v1": 1.1}]}]},
            {"slot": "Emitter", "kind": "item", "input": "Hadanite", "qty": 7,
             "mods": [{"prop": "Impact Force", "dir": "higher", "mode": "multiplier",
                       "ranges": [{"q0": 0, "q1": 1000, "v0": 0.95, "v1": 1.05}]}]},
            {"slot": "Aperture Iris", "kind": "item", "input": "Dolivine", "qty": 7,
             "mods": [{"prop": "Impact Force", "dir": "higher", "mode": "multiplier",
                       "ranges": [{"q0": 0, "q1": 1000, "v0": 0.95, "v1": 1.05}]}]},
        ],
    }

    # A LumaCore-shaped power plant: piecewise segmented multiplier + an
    # additive (Power Pips) modifier, plus a duplicated resource across slots
    # with different min qualities.
    PLANT = {
        "name": "LumaCore", "cat": "Power Plant", "time_s": 1200, "default": True,
        "aspects": [
            {"slot": "Shell", "kind": "resource", "input": "Borase", "scu": 1.5,
             "min_q": 500,
             "mods": [{"prop": "Integrity", "dir": "higher", "mode": "multiplier",
                       "ranges": [{"q0": 0, "q1": 500, "v0": 0.8, "v1": 1.0},
                                  {"q0": 501, "q1": 1000, "v0": 1.0, "v1": 1.2}]}]},
            {"slot": "Stator Cores", "kind": "resource", "input": "Borase",
             "scu": 0.5,
             "mods": [{"prop": "Power Pips", "dir": "higher", "mode": "additive",
                       "ranges": [{"q0": 0, "q1": 399, "v0": 1, "v1": 1},
                                  {"q0": 400, "q1": 899, "v0": 2, "v1": 2},
                                  {"q0": 900, "q1": 1000, "v0": 3, "v1": 3}]}]},
        ],
    }

    # -- manifest --

    def test_manifest_aggregates_both_kinds(self):
        m = nav_core.blueprint_manifest(self.WEAPON, qty=1)
        self.assertEqual([r["input"] for r in m["resources"]], ["Agricium"])
        self.assertAlmostEqual(m["resources"][0]["scu"], 0.36)
        gems = {r["input"]: r["qty"] for r in m["items"]}
        self.assertEqual(gems, {"Hadanite": 7, "Dolivine": 7})
        self.assertEqual(m["time_s"], 540)
        self.assertEqual(m["total_time_s"], 540)

    def test_manifest_multiplies_by_qty(self):
        m = nav_core.blueprint_manifest(self.WEAPON, qty=4)
        self.assertAlmostEqual(m["resources"][0]["scu"], 1.44)
        self.assertEqual(m["items"][0]["qty"], 28)
        self.assertEqual(m["total_time_s"], 2160)

    def test_manifest_sums_duplicate_resource_and_max_wins_min_quality(self):
        m = nav_core.blueprint_manifest(self.PLANT, qty=1)
        self.assertEqual(len(m["resources"]), 1)
        row = m["resources"][0]
        self.assertAlmostEqual(row["scu"], 2.0)
        self.assertEqual(sorted(row["slots"]), ["Shell", "Stator Cores"])
        self.assertEqual(row["min_q"], 500)     # strictest slot wins
        self.assertEqual(m["max_min_q"], 500)

    # -- stat drivers --

    def test_stat_drivers_invert_and_stack_across_aspects(self):
        drivers = {d["prop"]: d for d in nav_core.blueprint_stat_drivers(self.WEAPON)}
        self.assertEqual(set(drivers), {"Integrity", "Impact Force"})
        imp = drivers["Impact Force"]
        self.assertEqual(len(imp["drivers"]), 2)
        # two independent ×0.95–×1.05 sliders compose multiplicatively
        self.assertAlmostEqual(imp["combined_min"], 0.9025)
        self.assertAlmostEqual(imp["combined_max"], 1.1025)
        integ = drivers["Integrity"]
        self.assertEqual(integ["drivers"][0]["slot"], "Frame")
        self.assertAlmostEqual(integ["combined_min"], 0.9)
        self.assertAlmostEqual(integ["combined_max"], 1.1)

    def test_stat_drivers_multi_range_extremes(self):
        drivers = {d["prop"]: d for d in nav_core.blueprint_stat_drivers(self.PLANT)}
        integ = drivers["Integrity"]
        self.assertAlmostEqual(integ["combined_min"], 0.8)
        self.assertAlmostEqual(integ["combined_max"], 1.2)
        pips = drivers["Power Pips"]
        self.assertEqual(pips["mode"], "additive")
        self.assertEqual(pips["combined_min"], 1)
        self.assertEqual(pips["combined_max"], 3)

    # -- quality interpolation --

    def test_quality_effect_linear_midpoint_and_bounds(self):
        mod = self.WEAPON["aspects"][0]["mods"][0]
        self.assertAlmostEqual(nav_core.blueprint_quality_effect(mod, 500), 1.0)
        self.assertAlmostEqual(nav_core.blueprint_quality_effect(mod, 0), 0.9)
        self.assertAlmostEqual(nav_core.blueprint_quality_effect(mod, 1000), 1.1)
        # out-of-span clamps
        self.assertAlmostEqual(nav_core.blueprint_quality_effect(mod, -50), 0.9)
        self.assertAlmostEqual(nav_core.blueprint_quality_effect(mod, 2000), 1.1)

    def test_quality_effect_piecewise_segments(self):
        mod = self.PLANT["aspects"][0]["mods"][0]
        self.assertAlmostEqual(nav_core.blueprint_quality_effect(mod, 250), 0.9)
        self.assertAlmostEqual(nav_core.blueprint_quality_effect(mod, 500), 1.0)
        # second segment interpolates 1.0→1.2 over 501→1000
        v = nav_core.blueprint_quality_effect(mod, 750)
        self.assertAlmostEqual(v, 1.0 + 0.2 * (750 - 501) / (1000 - 501), places=6)
        self.assertAlmostEqual(nav_core.blueprint_quality_effect(mod, 1000), 1.2)

    def test_quality_effect_additive_steps(self):
        mod = self.PLANT["aspects"][1]["mods"][0]
        self.assertEqual(nav_core.blueprint_quality_effect(mod, 0), 1)
        self.assertEqual(nav_core.blueprint_quality_effect(mod, 399), 1)
        self.assertEqual(nav_core.blueprint_quality_effect(mod, 400), 2)
        self.assertEqual(nav_core.blueprint_quality_effect(mod, 950), 3)

    def test_stat_preview_defaults_to_base_and_combines(self):
        base = {s["prop"]: s["value"] for s in nav_core.blueprint_stat_preview(self.WEAPON)}
        self.assertAlmostEqual(base["Integrity"], 1.0)
        self.assertAlmostEqual(base["Impact Force"], 1.0)   # 1.0 × 1.0
        boosted = {s["prop"]: s["value"] for s in nav_core.blueprint_stat_preview(
            self.WEAPON, {"Emitter": 1000, "Aperture Iris": 1000})}
        self.assertAlmostEqual(boosted["Impact Force"], 1.1025)
        self.assertAlmostEqual(boosted["Integrity"], 1.0)   # Frame untouched

    # -- board quote state --

    def test_commission_board_state_best_quote(self):
        listing = {"mode": "commission", "status": "open", "price_auec": 45000,
                   "ends_at": None}
        offers = [
            {"amount_auec": 50000, "status": "active"},
            {"amount_auec": 42000, "status": "active"},
            {"amount_auec": 30000, "status": "withdrawn"},   # gone — not a quote
            {"amount_auec": None, "status": "active"},        # note-only, no amount
        ]
        st = nav_core.commission_board_state(listing, offers)
        self.assertEqual(st["quote_count"], 2)
        self.assertEqual(st["best_quote"], 42000)
        self.assertEqual(st["budget"], 45000)

    def test_commission_board_state_empty(self):
        st = nav_core.commission_board_state(
            {"mode": "commission", "status": "open", "price_auec": None}, [])
        self.assertEqual(st["quote_count"], 0)
        self.assertIsNone(st["best_quote"])
        self.assertIsNone(st["budget"])

    # -- goal seeding (Resource Manager craft goals) --

    @staticmethod
    def _resolver(known):
        """A stand-in catalog resolver: known names → a commodity item, else None."""
        def resolve(name):
            if name in known:
                return {"item_id": f"commodity:{name.lower()}", "name": name}
            return None
        return resolve

    def test_goal_lines_maps_both_kinds_and_scales_qty(self):
        resolve = self._resolver({"Agricium", "Hadanite", "Dolivine"})
        out = nav_core.blueprint_goal_lines(self.WEAPON, 2, resolve)
        self.assertEqual(out["unmapped"], [])
        by_id = {l["item_id"]: l for l in out["lines"]}
        agri = by_id["commodity:agricium"]
        self.assertEqual(agri["unit"], "SCU")
        self.assertAlmostEqual(agri["qty_needed"], 0.72)     # 0.36 SCU × 2
        hada = by_id["commodity:hadanite"]
        self.assertEqual(hada["unit"], "each")               # gem count, not SCU
        self.assertEqual(hada["qty_needed"], 14)             # 7 × 2
        self.assertEqual(agri["item_name"], "Agricium")

    def test_goal_lines_surfaces_min_quality_and_dedups_resource(self):
        resolve = self._resolver({"Borase"})
        out = nav_core.blueprint_goal_lines(self.PLANT, 1, resolve)
        self.assertEqual(len(out["lines"]), 1)               # Borase summed across slots
        line = out["lines"][0]
        self.assertAlmostEqual(line["qty_needed"], 2.0)      # 1.5 + 0.5 SCU
        self.assertEqual(line["min_q"], 500)                 # strictest slot wins

    def test_goal_lines_reports_unmapped_inputs(self):
        resolve = self._resolver({"Agricium"})               # gems unresolved
        out = nav_core.blueprint_goal_lines(self.WEAPON, 1, resolve)
        self.assertEqual([l["item_id"] for l in out["lines"]], ["commodity:agricium"])
        self.assertEqual(sorted(out["unmapped"]), ["Dolivine", "Hadanite"])

    def test_goal_lines_input_qs_raise_target_quality(self):
        # Spec-builder sliders ({slot: q}) lift each line above the recipe minimum.
        resolve = self._resolver({"Agricium", "Hadanite", "Dolivine"})
        out = nav_core.blueprint_goal_lines(
            self.WEAPON, 1, resolve, {"Frame": 800, "Emitter": 650})
        by_id = {l["item_id"]: l for l in out["lines"]}
        self.assertEqual(by_id["commodity:agricium"]["min_q"], 800)   # ask > recipe's 1
        self.assertEqual(by_id["commodity:hadanite"]["min_q"], 650)
        self.assertEqual(by_id["commodity:dolivine"]["min_q"], 0)     # slot not asked

    # -- estimated material cost (#25.1 §12) --

    def test_material_cost_prices_resources_only(self):
        # Resource (SCU) inputs price out; gem/item counts have no per-unit price
        # source and land in unpriced alongside unknown resources.
        prices = {"Agricium": 2000}
        cost = nav_core.blueprint_material_cost(self.WEAPON, lambda n: prices.get(n))
        self.assertEqual(cost["total"], 720)                 # 0.36 SCU × 2,000
        self.assertEqual(sorted(cost["unpriced"]), ["Dolivine", "Hadanite"])

    def test_material_cost_none_when_nothing_priced(self):
        cost = nav_core.blueprint_material_cost(self.WEAPON, lambda n: None)
        self.assertIsNone(cost["total"])
        self.assertEqual(sorted(cost["unpriced"]),
                         ["Agricium", "Dolivine", "Hadanite"])

    def test_goal_lines_recipe_minimum_wins_over_lower_ask(self):
        # A slider below the recipe's own demand never lowers the target; a shared
        # input takes the strictest ask across its slots.
        resolve = self._resolver({"Borase"})
        out = nav_core.blueprint_goal_lines(
            self.PLANT, 1, resolve, {"Shell": 200, "Stator Cores": 700})
        self.assertEqual(out["lines"][0]["min_q"], 700)   # max(recipe 500, asks 200/700)


class WikiCatalogArtifactTests(unittest.TestCase):
    """Backlog #28 — keep the committed poi/locations.json artifact honest.

    Distilled offline by tools/sync_locations.py from the SC Wiki API; this
    guards it against silent corruption on a per-patch re-run.
    """

    @classmethod
    def setUpClass(cls):
        import json
        cls.doc = json.loads((DATA_DIR / "locations.json").read_text())
        cls.records = cls.doc["locations"]

    def test_meta_stamped_with_game_version(self):
        self.assertIn("game_version", self.doc["_meta"])
        self.assertEqual(self.doc["_meta"]["license"], "CC BY-SA 4.0")

    def test_records_wellformed(self):
        self.assertGreater(len(self.records), 500)
        self.assertGreater(sum(1 for r in self.records if r["qt_valid"]), 400)
        for r in self.records:
            # exactly one frame: body-local (with a resolvable container) or
            # static system-global — never both, never neither.
            self.assertNotEqual(r["local_km"] is None, r["global_m"] is None, r["name"])
            if r["local_km"] is not None:
                self.assertIn((r["system"], r["container"]), NAV.containers, r["name"])
            self.assertIn(r["system"], ("Stanton", "Pyro", "Nyx"), r["name"])

    def test_names_unique_within_system(self):
        keys = [(r["system"], nav_core.wiki_name_key(r["name"])) for r in self.records]
        self.assertEqual(len(keys), len(set(keys)))


class WikiPoiImportTests(unittest.TestCase):
    """Backlog #28a/b — wiki-catalog POI import (dedup, id namespace, frames)
    and the always-on arrival-radius enrichment, against the real artifacts."""

    @classmethod
    def setUpClass(cls):
        import json
        cls.locations = json.loads((DATA_DIR / "locations.json").read_text())["locations"]

    def setUp(self):
        # A fresh NavData per test: add_wiki_pois mutates, and NAV is shared.
        self.nav = load_data(DATA_DIR)

    def test_import_adds_wiki_only_pois_in_reserved_id_range(self):
        added = nav_core.add_wiki_pois(self.nav, self.locations)
        self.assertGreater(added, 200)      # ~241 wiki-only places as of 4.8.2
        wiki = [p for p in self.nav.pois.values() if p.source == "wiki"]
        self.assertEqual(len(wiki), added)
        self.assertTrue(all(p.id >= nav_core.WIKI_POI_START for p in wiki))
        # A known wiki-only Pyro asteroid cluster landed as a routable space POI.
        cluster = next(p for p in wiki if p.name == "Cluster BGR-560")
        self.assertTrue(cluster.qt_marker)
        self.assertIsNotNone(cluster.global_m)
        # A known wiki-only Stanton surface entity landed on its body.
        comm = next(p for p in wiki if p.local_km is not None and p.system == "Stanton")
        self.assertIn((comm.system, comm.container_name), self.nav.containers)

    def test_dedup_never_doubles_a_known_place(self):
        # The starmap catalog already repeats generic names (numbered caves,
        # 'Derelict Outpost') — the invariant is that the wiki import never
        # ADDS to an existing name: every name it had before keeps its exact
        # count, and every new name appears exactly once.
        from collections import Counter
        key = lambda p: (p.system.lower(), nav_core.wiki_name_key(p.name))
        before = Counter(key(p) for p in self.nav.pois.values())
        nav_core.add_wiki_pois(self.nav, self.locations)
        after = Counter(key(p) for p in self.nav.pois.values())
        for k, n in after.items():
            self.assertEqual(n, before.get(k, 0) or 1, k)
        # e.g. Everus Harbor (a synthesized container-station) stayed single.
        ek = ("stanton", nav_core.wiki_name_key("Everus Harbor"))
        self.assertEqual(before[ek], 1)
        self.assertEqual(after[ek], 1)

    def test_import_is_idempotent(self):
        nav_core.add_wiki_pois(self.nav, self.locations)
        self.assertEqual(nav_core.add_wiki_pois(self.nav, self.locations), 0)

    def test_surface_frame_is_body_local(self):
        # An imported surface outpost sits at ~body radius in the rotating
        # frame — the same convention every starmap surface POI uses. (Comm
        # arrays are deliberately high above their body, so outposts only.)
        nav_core.add_wiki_pois(self.nav, self.locations)
        checked = 0
        for p in self.nav.pois.values():
            if p.source != "wiki" or p.local_km is None or p.type != "Outpost":
                continue
            c = self.nav.containers[(p.system, p.container_name)]
            if not c.is_body:
                continue
            r_km = math.dist((0, 0, 0), p.local_km)
            self.assertLess(abs(r_km - c.body_radius / 1000), c.body_radius / 1000 * 0.2,
                            p.name)
            checked += 1
        self.assertGreater(checked, 5)

    def test_qt_upgrade_promotes_matched_starmap_pois(self):
        # Places both catalogs know (deduped away as POIs) still gain the
        # game's QT flag — e.g. Ghost Hollow, a starmap derelict outpost that
        # 4.8 made a QT destination. Generic repeated names never upgrade.
        ghost = next(p for p in self.nav.pois.values()
                     if p.name == "Ghost Hollow" and p.system == "Stanton")
        self.assertFalse(ghost.qt_marker)
        nav_core.add_wiki_pois(self.nav, self.locations)
        promoted = nav_core.upgrade_qt_markers(self.nav, self.locations)
        self.assertGreater(promoted, 50)
        self.assertTrue(ghost.qt_marker)
        derelicts = [p for p in self.nav.pois.values()
                     if nav_core.wiki_name_key(p.name) == ("derelict", "outpost")]
        self.assertTrue(all(not p.qt_marker for p in derelicts))

    def test_arrival_radii_annotate_without_import(self):
        # Enrichment is independent of the POI toggle: the synthesized Everus
        # Harbor station gets its wiki QT arrival radius by name.
        hit = nav_core.annotate_arrival_radii(self.nav, self.locations)
        self.assertGreater(hit, 0)
        everus = next(p for p in self.nav.pois.values() if p.name == "Everus Harbor")
        self.assertEqual(everus.arrival_radius_m, 24000)
        # POIs the wiki doesn't know keep None and the flat threshold applies.
        self.assertTrue(any(p.arrival_radius_m is None for p in self.nav.pois.values()))




# ---------------------------------------------------------------------------
# Halo Finder (#31) — Aaron Halo band geometry, classifier, drop planner.
# ---------------------------------------------------------------------------

def _halo_marker(nav, code):
    return next(p for p in nav.pois.values()
                if code in p.name and p.qt_marker and p.system == "Stanton")


class HaloFinderTests(unittest.TestCase):
    """Band model + chord geometry golden-tested against CaptSheppard /
    Cornerstone's published ARC-L1<->CRU-L4 route-chart values (cstone.space,
    3.16.1 survey, unchanged through 4.x). Published numbers are photo-survey
    measurements against the live game; ours are exact marker geometry — they
    agree within ~0.013%% of the route, so tolerance is 5,000 km."""

    GOLDEN_TOL_M = 5_000e3
    # Published chart values (km -> m): drops to the destination, both
    # directions, band 5 (densest) and band 1; the two directions of the same
    # crossing sum to the published route length.
    ROUTE_M = 24_001_764e3
    B5_FWD_M = 14_292_609e3      # ARC-L1 -> CRU-L4, band-5 densest point
    B5_REV_M = 9_709_155e3       # CRU-L4 -> ARC-L1, same point
    B1_FWD_M = 12_744_803e3
    B1_REV_M = 11_256_961e3

    @classmethod
    def setUpClass(cls):
        cls.nav = load_data(DATA_DIR)
        nav_core.assign_qt_markers(cls.nav)
        cls.t = nav_core.ROTATION_EPOCH
        cls.arc = _halo_marker(cls.nav, "(ARC-L1)")
        cls.cru = _halo_marker(cls.nav, "(CRU-L4)")
        cls.p_arc = poi_global_m(cls.nav, cls.arc, cls.t)
        cls.p_cru = poi_global_m(cls.nav, cls.cru, cls.t)

    # --- band table + primitives -------------------------------------------

    def test_band_table_shape(self):
        self.assertEqual([b["band"] for b in nav_core.HALO_BANDS], list(range(1, 11)))
        for b in nav_core.HALO_BANDS:
            self.assertLess(b["inner_m"], b["peak_m"])
            self.assertLess(b["peak_m"], b["outer_m"])
            self.assertLessEqual(b["half_height_m"], 5_000e3)
        radii = [x for b in nav_core.HALO_BANDS for x in (b["inner_m"], b["outer_m"])]
        self.assertEqual(radii, sorted(radii))     # bands ordered, never overlap

    def test_ring_crossings_known_answer(self):
        # Straight radial chord through the origin: crosses r=5 at t=0.25/0.75.
        ts = nav_core._ring_crossings((-10, 0, 0), (10, 0, 0), 5.0)
        self.assertEqual(len(ts), 2)
        self.assertAlmostEqual(ts[0], 0.25)
        self.assertAlmostEqual(ts[1], 0.75)
        # A chord entirely outside the radius misses.
        self.assertEqual(nav_core._ring_crossings((10, 10, 0), (10, -10, 0), 5.0), [])

    def test_body_volumes_star_not_at_origin(self):
        vols = nav_core.body_volumes(self.nav, "Stanton")
        star = next(v for v in vols if v["body"] == "Stanton Star")
        self.assertGreater(nav_core.dist3(star["a"], (0, 0, 0)), 1e9)
        self.assertAlmostEqual(star["r"], 696_000e3 * nav_core.HALO_BODY_MARGIN)

    # --- golden chart values ------------------------------------------------

    def test_golden_route_length(self):
        self.assertAlmostEqual(nav_core.dist3(self.p_arc, self.p_cru),
                               self.ROUTE_M, delta=self.GOLDEN_TOL_M)

    def _peak_drop(self, p0, p1, band_n):
        crossings = nav_core.halo_band_crossings(p0, p1, nav_core.halo_band(band_n))
        self.assertEqual(len(crossings), 1)     # endpoint inside the belt: one pass
        return crossings[0]

    def test_golden_band5_both_directions(self):
        fwd = self._peak_drop(self.p_arc, self.p_cru, 5)
        rev = self._peak_drop(self.p_cru, self.p_arc, 5)
        self.assertAlmostEqual(fwd["peak_m"], self.B5_FWD_M, delta=self.GOLDEN_TOL_M)
        self.assertAlmostEqual(rev["peak_m"], self.B5_REV_M, delta=self.GOLDEN_TOL_M)
        # window brackets the peak, in travel order
        self.assertGreater(fwd["enter_m"], fwd["peak_m"])
        self.assertGreater(fwd["peak_m"], fwd["exit_m"])

    def test_golden_band1_both_directions(self):
        fwd = self._peak_drop(self.p_arc, self.p_cru, 1)
        rev = self._peak_drop(self.p_cru, self.p_arc, 1)
        self.assertAlmostEqual(fwd["peak_m"], self.B1_FWD_M, delta=self.GOLDEN_TOL_M)
        self.assertAlmostEqual(rev["peak_m"], self.B1_REV_M, delta=self.GOLDEN_TOL_M)

    def test_sum_identity(self):
        # The two directions reference the same crossing point, so their drop
        # distances sum to the route length exactly (Cornerstone's own
        # validation identity for the published charts).
        route = nav_core.dist3(self.p_arc, self.p_cru)
        fwd = self._peak_drop(self.p_arc, self.p_cru, 5)
        rev = self._peak_drop(self.p_cru, self.p_arc, 5)
        self.assertAlmostEqual(fwd["peak_m"] + rev["peak_m"], route, delta=1e3)

    def test_star_fallback_reads_peak_radius(self):
        # At <= 5,000 km of z over ~20 Gm of radius, the 3D distance to the
        # origin marker and the cylindrical peak radius agree within meters.
        fwd = self._peak_drop(self.p_arc, self.p_cru, 5)
        self.assertAlmostEqual(fwd["star_dist_peak_m"],
                               nav_core.halo_band(5)["peak_m"], delta=1e3)

    def test_gateway_chords_never_touch_the_belt(self):
        # Jump-point stations sit Gm off-plane; their chords cross the band
        # radii far above the rocks — the z gate must reject every band.
        gate = _halo_marker(self.nav, "Jumppoint Pyro")
        p_gate = poi_global_m(self.nav, gate, self.t)
        for band in nav_core.HALO_BANDS:
            self.assertEqual(
                nav_core.halo_band_crossings(p_gate, self.p_cru, band), [],
                f"band {band['band']} should be crossed off-plane only")

    def test_z_gate_synthetic(self):
        band5 = nav_core.halo_band(5)
        flat = nav_core.halo_band_crossings(
            (-25e9, 0, 0), (25e9, 0, 0), band5)
        high = nav_core.halo_band_crossings(
            (-25e9, 0, 8_000e3), (25e9, 0, 8_000e3), band5)
        self.assertEqual(len(flat), 2)          # through the interior: two passes
        self.assertEqual(high, [])              # same chord 8,000 km up: none

    # --- halo_locate ----------------------------------------------------------

    def test_locate_band_and_offplane(self):
        peak = nav_core.halo_band(5)["peak_m"]
        hit = nav_core.halo_locate((peak, 0, 800e3))
        self.assertEqual((hit["status"], hit["band"]), ("band", 5))
        self.assertAlmostEqual(hit["off_peak_m"], 0.0, delta=1.0)
        lofted = nav_core.halo_locate((peak, 0, 8_000e3))
        self.assertEqual((lofted["status"], lofted["band"]), ("band_offplane", 5))

    def test_locate_void_and_outside(self):
        void = nav_core.halo_locate((0, 19_750_000e3, 0))
        self.assertEqual(void["status"], "void")
        self.assertEqual(void["between"], [1, 2])
        inside = nav_core.halo_locate((19_000_000e3, 0, 0))
        self.assertEqual((inside["status"], inside["side"]), ("outside", "inward"))
        beyond = nav_core.halo_locate((0, 22_000_000e3, 0))
        self.assertEqual((beyond["status"], beyond["side"]), ("outside", "outward"))

    # --- system disambiguation (#31 in-halo bug) ------------------------------

    def test_in_halo_resolves_to_stanton_every_angle(self):
        # Regression: a deep-space fix inside the belt used to resolve to
        # Pyro/Nyx at some bearings, because the nearest-container fallback
        # mixes per-system frames (Pyro/Nyx bodies sit at their own origin,
        # ~20 Gm from the Stanton-frame halo ring). It must be Stanton from
        # every angle around the ring — the halo is a Stanton landmark.
        peak = nav_core.halo_band(5)["peak_m"]
        for deg in range(0, 360, 15):
            a = math.radians(deg)
            pos = (peak * math.cos(a), peak * math.sin(a), 0.0)
            self.assertTrue(nav_core.halo_contains(pos))
            self.assertEqual(nav_core.system_at(self.nav, pos),
                             nav_core.HALO_SYSTEM, msg=f"bearing {deg}")

    def test_halo_contains_bounds(self):
        peak = nav_core.halo_band(5)["peak_m"]
        # off-plane but within tolerance -> still in the halo
        self.assertTrue(nav_core.halo_contains((peak, 0, 5e8)))
        # far off-plane -> not the halo (radius-only match is not enough)
        self.assertFalse(nav_core.halo_contains((peak, 0, 2e9)))
        # inside the inner edge / beyond the outer edge -> not the halo
        self.assertFalse(nav_core.halo_contains((10_000_000e3, 0, 0)))
        self.assertFalse(nav_core.halo_contains((30_000_000e3, 0, 0)))

    # --- plan_halo_drop -------------------------------------------------------

    def test_plan_validates_inputs(self):
        with self.assertRaises(ValueError):
            nav_core.plan_halo_drop(self.nav, start=self.arc)          # neither
        with self.assertRaises(ValueError):
            nav_core.plan_halo_drop(self.nav, start=self.arc, band=5,
                                    target=self.cru)                   # both
        with self.assertRaises(ValueError):
            nav_core.plan_halo_drop(self.nav, start=self.arc, band=11)

    def test_plan_band_peak_aim(self):
        vols = nav_core.body_volumes(self.nav, "Stanton")
        plan = nav_core.plan_halo_drop(self.nav, start=self.arc, band=5,
                                       aim="peak", volumes=vols)
        drop = plan["drop"]
        # peak aim lands the drop point on the surveyed densest radius
        r = math.hypot(drop["crossing_xyz"][0], drop["crossing_xyz"][1])
        self.assertAlmostEqual(r, nav_core.halo_band(5)["peak_m"], delta=1_000e3)
        self.assertGreater(drop["steep_deg"], 45)
        self.assertGreater(drop["enter_m"], drop["peak_m"])
        self.assertGreater(drop["peak_m"], drop["exit_m"])
        self.assertGreaterEqual(drop["exit_m"], nav_core.HALO_DROP_MIN_M)
        self.assertEqual(plan["legs"][-1]["kind"], "drop")
        self.assertFalse(plan["staged"])
        self.assertNotIn(drop["marker_id"], nav_core.HALO_PLACEHOLDER_POI_IDS)
        # alternates are distinct markers, each a full drop+leg pair the
        # client can promote without a re-plan round trip
        ids = [drop["marker_id"]] + [a["drop"]["marker_id"] for a in plan["alternates"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(a["leg"]["kind"] == "drop" for a in plan["alternates"]))

    def test_plan_band_aim_stretches_window(self):
        vols = nav_core.body_volumes(self.nav, "Stanton")
        peak = nav_core.plan_halo_drop(self.nav, start=self.arc, band=5,
                                       aim="peak", volumes=vols)
        band = nav_core.plan_halo_drop(self.nav, start=self.arc, band=5,
                                       aim="band", volumes=vols)
        self.assertGreater(band["drop"]["window_s"], peak["drop"]["window_s"])

    def test_plan_poi_mode(self):
        rock = nav_core.Poi(
            id=1000001, name="Big Q Rock", system="Stanton", container_name=None,
            type="asteroid", local_km=None,
            global_m=(14_000_000e3, -14_800_000e3, 900e3),
            latitude=None, longitude=None, height_m=None, qt_marker=False,
            custom=True)
        vols = nav_core.body_volumes(self.nav, "Stanton")
        direct = nav_core.plan_halo_drop(self.nav, start=self.arc, target=rock,
                                         volumes=vols, allow_staging=False)
        best = nav_core.plan_halo_drop(self.nav, start=self.arc, target=rock,
                                       volumes=vols)
        self.assertIn("expected_miss_m", direct["drop"])
        # the staged (T, M) pair scan must materially beat the direct chord
        self.assertTrue(best["staged"])
        self.assertLess(best["drop"]["expected_miss_m"],
                        0.6 * direct["drop"]["expected_miss_m"])
        self.assertEqual(best["legs"][0]["kind"], "travel")
        self.assertEqual(best["legs"][1]["kind"], "drop")
        self.assertEqual(best["target"]["name"], "Big Q Rock")

    def test_plan_staging_synthetic(self):
        # Start lofted 50,000 km above the belt: every band radius is crossed
        # far off-plane, so no direct chord exists; the planner must stage
        # through the inner marker and drop on the way back out.
        m_in = _space_poi(11, "Inner", (19_000_000e3, 0, 0), system="Stanton")
        m_out = _space_poi(12, "Outer", (23_000_000e3, 0, 0), system="Stanton")
        nav2 = _synthetic_nav([m_in, m_out], system="Stanton")
        start = nav_core.Poi(
            id=-1, name="your location", system="Stanton", container_name=None,
            type="", local_km=None, global_m=(20_320_000e3, 0, 50_000e3),
            latitude=None, longitude=None, height_m=None, qt_marker=False)
        plan = nav_core.plan_halo_drop(nav2, start=start, band=5,
                                       markers=[m_in, m_out])
        self.assertTrue(plan["staged"])
        self.assertEqual(plan["legs"][0]["to"], "Inner")
        self.assertEqual(plan["drop"]["marker_name"], "Outer")
        with self.assertRaises(ValueError):
            nav_core.plan_halo_drop(nav2, start=start, band=5,
                                    markers=[m_in, m_out], allow_staging=False)

    def test_second_crossing_reported(self):
        # A chord through the belt interior crosses the band twice; the far
        # pass is surfaced as the alternate window.
        a = _space_poi(21, "A", (23_000_000e3, 0, 0), system="Stanton")
        b = _space_poi(22, "B", (-23_000_000e3, 1_000_000e3, 0), system="Stanton")
        nav2 = _synthetic_nav([a, b], system="Stanton")
        plan = nav_core.plan_halo_drop(nav2, start=a, band=5, markers=[b])
        self.assertIn("second_crossing", plan["drop"])
        second = plan["drop"]["second_crossing"]
        self.assertLess(second["peak_m"], plan["drop"]["peak_m"])

    def test_plan_carries_map_coordinates(self):
        # The HALO MAP needs real positions: plan start, staging hop, and each
        # drop's marker. crossing_xyz was already there.
        vols = nav_core.body_volumes(self.nav, "Stanton")
        plan = nav_core.plan_halo_drop(self.nav, start=self.arc, band=5,
                                       volumes=vols)
        self.assertEqual(tuple(plan["start"]["xyz"]), tuple(self.p_arc))
        r = math.hypot(plan["drop"]["marker_xyz"][0], plan["drop"]["marker_xyz"][1])
        self.assertGreater(r, 1e9)          # a real system position, not a stub
        self.assertTrue(all("marker_xyz" in a["drop"] for a in plan["alternates"]))
        # staged plans position the hop too (synthetic staging fixture)
        m_in = _space_poi(11, "Inner", (19_000_000e3, 0, 0), system="Stanton")
        m_out = _space_poi(12, "Outer", (23_000_000e3, 0, 0), system="Stanton")
        nav2 = _synthetic_nav([m_in, m_out], system="Stanton")
        lofted = nav_core.Poi(
            id=-1, name="your location", system="Stanton", container_name=None,
            type="", local_km=None, global_m=(20_320_000e3, 0, 50_000e3),
            latitude=None, longitude=None, height_m=None, qt_marker=False)
        staged = nav_core.plan_halo_drop(nav2, start=lofted, band=5,
                                         markers=[m_in, m_out])
        self.assertEqual(tuple(staged["legs"][0]["to_xyz"]), (19_000_000e3, 0, 0))

    # --- obstruction: endpoint-in-volume rule (v0.51.1 regression) -----------

    def test_chord_obstructed_endpoint_in_margin(self):
        vols = [{"kind": "sphere", "a": (0, 0, 0), "b": None, "r": 960e3,
                 "body_r": 800e3, "warning_id": None, "system": "Stanton",
                 "body": "X"}]
        orbit = (910e3, 0, 0)          # inside the margin, outside the body
        self.assertFalse(nav_core.chord_obstructed(orbit, (5e9, 0, 0), vols))
        self.assertTrue(nav_core.chord_obstructed(orbit, (-5e9, 0, 0), vols))
        surface = (795e3, 0, 0)        # terrain sits under the body sphere
        self.assertFalse(nav_core.chord_obstructed(surface, (5e9, 0, 0), vols))
        self.assertTrue(nav_core.chord_obstructed(surface, (-5e9, 0, 0), vols))
        # the margin still applies unchanged to fly-past chords
        self.assertTrue(nav_core.chord_obstructed(
            (5e9, 900e3, 0), (-5e9, 900e3, 0), vols))
        self.assertFalse(nav_core.chord_obstructed(
            (5e9, 1_000e3, 0), (-5e9, 1_000e3, 0), vols))

    def test_plan_from_low_orbit_station(self):
        # Baijini Point orbits 910 km from ArcCorp's center — inside the 960 km
        # margin sphere. v0.51.0 rejected every chord from it ("no viable drop
        # route from here"); the endpoint rule must let departures through.
        vols = nav_core.body_volumes(self.nav, "Stanton")
        baijini = next(p for p in self.nav.pois.values()
                       if "Baijini" in p.name and p.system == "Stanton")
        plan = nav_core.plan_halo_drop(self.nav, start=baijini, band=5,
                                       volumes=vols)
        self.assertEqual(plan["legs"][-1]["kind"], "drop")

    def test_plan_from_every_stanton_marker(self):
        # The invariant the app promises: a band drop is plannable from any
        # marker in the system (orbital stations, surface outposts, all of it).
        vols = nav_core.body_volumes(self.nav, "Stanton")
        fails = []
        for p in self.nav.qt_markers:
            if p.system != "Stanton":
                continue
            try:
                nav_core.plan_halo_drop(self.nav, start=p, band=5, volumes=vols)
            except ValueError:
                fails.append(p.name)
        self.assertEqual(fails, [])

    def test_frame_at_system_hint_disambiguates_deep_space(self):
        # Every system's data centers on its own (0,0,0) in one numeric space,
        # so this genuine Stanton position sits NEARER a Pyro container than any
        # Stanton one — the raw heuristic names the wrong system. Lofted 3 Gm
        # off-plane so it's outside the Aaron Halo envelope (see
        # test_in_halo_resolves_to_stanton_every_angle) — this isolates the
        # system_hint mechanism, not the halo_contains geometric shortcut.
        ambiguous = (14_000_000e3, -14_800_000e3, 3_000_000e3)
        self.assertFalse(nav_core.halo_contains(ambiguous))
        self.assertNotEqual(nav_core.system_at(self.nav, ambiguous), "Stanton")
        poi = nav_core.custom_poi_from_position(
            self.nav, ambiguous, self.t, "Halo Rock", "asteroid", 1000010,
            system_hint="Stanton")
        self.assertEqual(poi.system, "Stanton")
        # near a container the detection wins — a hint can't mis-stamp it
        daymar = next(c for c in self.nav.containers.values()
                      if c.system == "Stanton" and c.name == "Daymar")
        surface = (daymar.pos[0] + daymar.body_radius + 1_000,
                   daymar.pos[1], daymar.pos[2])
        near = nav_core.custom_poi_from_position(
            self.nav, surface, self.t, "Camp", "outpost", 1000011,
            system_hint="Pyro")
        self.assertEqual(near.system, "Stanton")

    # --- deep-space capture regression (#31 prerequisite) ---------------------

    def test_frame_at_deep_space_resolves_system(self):
        # A capture 20 Gm out detects no container; it must still stamp the
        # nearest-container system (not "Unknown", which made travel_cost
        # treat the POI as cross-system and unroutable).
        pos = (20_320_000e3, 0.0, 0.0)
        poi = nav_core.custom_poi_from_position(
            self.nav, pos, self.t, "Halo Rock", "asteroid", 1000009)
        self.assertEqual(poi.system, "Stanton")
        self.assertIsNone(poi.container_name)
        self.assertEqual(poi.global_m, pos)


class BeltRegistryTests(unittest.TestCase):
    """Multi-system expansion (#35): the per-system belt registry (Glaciem
    Ring pockets from the datamined containers, Pyro fields from the wiki
    locations feed), the Nyx/Pyro locate classifiers, system disambiguation,
    and pocket-mode drop planning. Registry counts/geometry are pinned against
    the committed feeds; solver fixtures are SELF-DERIVED from datamined
    coordinates (no community survey exists for these systems — the in-game
    verification pass is the external oracle, docs/halo-finder-expansion.md §7)."""

    @classmethod
    def setUpClass(cls):
        cls.nav = load_data(DATA_DIR)
        nav_core.assign_qt_markers(cls.nav)
        locs = json.loads((DATA_DIR / "locations.json").read_text())["locations"]
        cls.belts = nav_core.build_belt_registry(cls.nav, locs)

    # --- registry shape (pinned against the committed feeds) -----------------

    def test_glaciem_pocket_registry(self):
        nyx = self.belts["Nyx"]
        self.assertEqual(nyx["kind"], "ring")
        pockets = nyx["pockets"]
        kinds = {k: sum(1 for p in pockets if p["kind"] == k)
                 for k in ("general", "mission", "levski")}
        self.assertEqual(len(pockets), 381)
        self.assertEqual(kinds, {"general": 300, "mission": 80, "levski": 1})
        for p in pockets:
            r = math.hypot(p["xyz"][0], p["xyz"][1])
            self.assertAlmostEqual(r, nav_core.GLACIEM_R_M, delta=5e6)
            self.assertLessEqual(abs(p["xyz"][2]), 1e6)     # razor-thin plane
            self.assertGreater(p["grid_radius_m"], 0)
            self.assertNotIn("Fstreamable", p["key"])
        # sorted by ring angle (stable order for the UI's arc rendering)
        angles = [math.atan2(p["xyz"][1], p["xyz"][0]) for p in pockets]
        self.assertEqual(angles, sorted(angles))

    def test_pyro_field_registry(self):
        fields = self.belts["Pyro"]["fields"]
        self.assertEqual(len(fields), 102)                  # 16 L-points + 86 RMBs
        akiro = [f for f in fields if "Akiro" in f["name"]]
        self.assertEqual(len(akiro), 1)
        self.assertEqual(akiro[0]["shell"], "Pyro I")
        # a Terminus-shell field groups under the player-facing planet name
        pyr6 = next(f for f in fields if f["name"].startswith("PYR6"))
        self.assertEqual(pyr6["shell"], "Terminus (Pyro VI)")
        # only unmarked resource fields: every entry has coords + uuid
        for f in fields:
            self.assertTrue(f["uuid"])
            self.assertEqual(len(f["xyz"]), 3)

    def test_stanton_row_unchanged(self):
        st = self.belts["Stanton"]
        self.assertEqual(st["kind"], "bands")
        self.assertIs(st["bands"], nav_core.HALO_BANDS)
        self.assertIn("Cornerstone", st["attribution"])

    # --- system disambiguation ------------------------------------------------

    def test_glaciem_contains_bounds(self):
        r = nav_core.GLACIEM_R_M
        self.assertTrue(nav_core.glaciem_contains((r, 0, 0)))
        self.assertTrue(nav_core.glaciem_contains((0, -r, 50e6)))
        # outside the deliberately razor-thin envelope
        self.assertFalse(nav_core.glaciem_contains((r + 200e6, 0, 0)))
        self.assertFalse(nav_core.glaciem_contains((r, 0, 200e6)))
        # the Aaron Halo is NOT the Glaciem Ring (non-overlapping radii)
        self.assertFalse(nav_core.glaciem_contains((20_320_000e3, 0, 0)))
        self.assertFalse(nav_core.halo_contains((r, 0, 0)))

    def test_system_at_resolves_all_belts(self):
        self.assertEqual(nav_core.system_at(self.nav, (20_320_000e3, 0, 0)),
                         "Stanton")
        self.assertEqual(
            nav_core.system_at(self.nav, (nav_core.GLACIEM_R_M, 0, 0)), "Nyx")
        # Keeger (#36): a hint-less fix at the 48 Gm ring must resolve Nyx —
        # the nearest-container guess names Stanton out here (whose outermost
        # body orbits 28.9 Gm), which stamped survey marks into the wrong
        # system and dropped them from the org's Nyx map.
        self.assertEqual(
            nav_core.system_at(self.nav, (nav_core.KEEGER_R_M, 1.0e9, 0)),
            "Nyx")

    # --- locate classifiers ---------------------------------------------------

    def test_glaciem_locate_verdicts(self):
        pockets = self.belts["Nyx"]["pockets"]
        pk = next(p for p in pockets if p["kind"] == "general")
        at = nav_core.glaciem_locate(pk["xyz"], pockets)
        self.assertEqual(at["status"], "pocket")
        self.assertEqual(at["pocket"]["key"], pk["key"])
        self.assertLess(at["pocket"]["center_off_m"], 1.0)
        # a ring point rotated half the typical gap sits in the void, with the
        # arc distance back to the nearest pocket reported
        a = math.atan2(pk["xyz"][1], pk["xyz"][0]) + math.radians(0.4)
        mid = (nav_core.GLACIEM_R_M * math.cos(a),
               nav_core.GLACIEM_R_M * math.sin(a), 0.0)
        void = nav_core.glaciem_locate(mid, pockets)
        self.assertEqual(void["status"], "ring_void")
        self.assertGreater(void["pocket"]["arc_m"], 10_000e3)
        off = nav_core.glaciem_locate((10.0e9, 0, 0), pockets)
        self.assertEqual(off["status"], "off_ring")
        self.assertAlmostEqual(off["to_ring_m"], 5.0e9, delta=1e7)

    def _survey_mark(self, xyz, rocks, ores=(), salvage=False, mid=1):
        return {"id": 1_000_000 + mid, "name": f"Survey {mid}", "xyz": tuple(xyz),
                "rocks": rocks, "positive": rocks != "none",
                "ores": list(ores), "salvage": salvage, "owner_handle": None}

    def test_annotate_glaciem_survey_barren_and_rich(self):
        pockets = self.belts["Nyx"]["pockets"]
        pk = next(p for p in pockets if p["kind"] == "general")
        other = next(p for p in pockets
                     if p["kind"] == "general" and p["key"] != pk["key"])
        c = pk["xyz"]
        # Three empty marks inside pk (nearest to center = 4 km); one dense
        # Iron mark inside `other`; one far mark that belongs to NEITHER pocket.
        marks = [
            self._survey_mark((c[0] + 4_000, c[1], c[2]), "none", mid=1),
            self._survey_mark((c[0], c[1] + 900_000, c[2]), "none", mid=2),
            self._survey_mark((c[0] - 2_000_000, c[1], c[2]), "none", mid=3),
            self._survey_mark((other["xyz"][0], other["xyz"][1] + 50_000,
                               other["xyz"][2]), "dense", ores=["Iron (Ore)"], mid=4),
            self._survey_mark((10.0e9, 0.0, 0.0), "dense", mid=5),  # off-ring
        ]
        ann = nav_core.annotate_glaciem_survey(pockets, marks)
        by = {p["key"]: p for p in ann}
        self.assertEqual(by[pk["key"]]["survey"]["status"], "barren")
        self.assertEqual(by[pk["key"]]["survey"]["marks"], 3)
        self.assertEqual(by[pk["key"]]["survey"]["positive"], 0)
        self.assertAlmostEqual(by[pk["key"]]["survey"]["closest_center_m"],
                               4_000, delta=1.0)
        self.assertEqual(by[other["key"]]["survey"]["status"], "dense")
        self.assertEqual(by[other["key"]]["survey"]["ores"], ["Iron (Ore)"])
        # unsurveyed pockets carry no overlay; the off-ring mark is unassigned
        self.assertEqual(sum(1 for p in ann if "survey" in p), 2)
        # originals never mutated
        self.assertNotIn("survey", pk)

    def test_glaciem_pocket_survey_lookup(self):
        pockets = self.belts["Nyx"]["pockets"]
        pk = next(p for p in pockets if p["kind"] == "general")
        marks = [self._survey_mark((pk["xyz"][0] + 3_000, pk["xyz"][1],
                                    pk["xyz"][2]), "none", mid=1)]
        ann = nav_core.annotate_glaciem_survey(pockets, marks)
        inside = nav_core.glaciem_pocket_survey(
            (pk["xyz"][0] + 1_000, pk["xyz"][1], pk["xyz"][2]), ann)
        self.assertEqual(inside["status"], "barren")
        # a ring-void point between pockets sees no verdict
        a = math.atan2(pk["xyz"][1], pk["xyz"][0]) + math.radians(0.4)
        mid = (nav_core.GLACIEM_R_M * math.cos(a),
               nav_core.GLACIEM_R_M * math.sin(a), 0.0)
        self.assertIsNone(nav_core.glaciem_pocket_survey(mid, ann))

    def test_barren_pocket_downranked_not_dropped(self):
        # Identical geometry, one pocket surveyed barren: the unsurveyed pocket
        # wins, but the barren one keeps a positive score (still selectable).
        barren = {"key": "Wtn-barren", "survey": {"status": "barren"}}
        cands = [
            {"hit": True, "miss_m": 1_000.0, "flown_m": 1e9, "pocket": barren},
            {"hit": True, "miss_m": 1_000.0, "flown_m": 1e9,
             "pocket": {"key": "Wtn-unknown"}},
        ]
        nav_core._score_halo_pocket_cands(cands)
        self.assertLess(cands[0]["score"], cands[1]["score"])
        self.assertGreater(cands[0]["score"], 0.0)
        self.assertAlmostEqual(
            cands[0]["score"], cands[1]["score"] * nav_core.GLACIEM_BARREN_PENALTY,
            delta=1e-9)

    def test_glaciem_locate_carries_radar_geometry(self):
        # The live in-pocket radar (#36) needs center + envelope + signed offset.
        pockets = self.belts["Nyx"]["pockets"]
        pk = next(p for p in pockets if p["kind"] == "general")
        c = pk["xyz"]
        pos = (c[0] + 300_000, c[1] - 120_000, c[2] - 8_000)
        v = nav_core.glaciem_locate(pos, pockets)
        self.assertEqual(v["status"], "pocket")
        rp = v["pocket"]
        self.assertAlmostEqual(rp["dx"], 300_000, delta=1.0)
        self.assertAlmostEqual(rp["dy"], -120_000, delta=1.0)
        self.assertAlmostEqual(rp["dz"], -8_000, delta=1.0)
        self.assertEqual([round(x) for x in rp["center_xyz"]],
                         [round(x) for x in c])
        self.assertGreater(rp["grid_radius_m"], 0)
        # radar geometry rides along even in the ring-void (nearest pocket)
        a = math.atan2(c[1], c[0]) + math.radians(0.4)
        void = nav_core.glaciem_locate(
            (nav_core.GLACIEM_R_M * math.cos(a),
             nav_core.GLACIEM_R_M * math.sin(a), 0.0), pockets)
        self.assertEqual(void["status"], "ring_void")
        self.assertIn("center_xyz", void["pocket"])

    def test_field_locate_verdicts(self):
        fields = self.belts["Pyro"]["fields"]
        akiro = next(f for f in fields if "Akiro" in f["name"])
        at = nav_core.field_locate(akiro["xyz"], fields)
        self.assertEqual(at["status"], "field")
        self.assertEqual(at["field"]["name"], akiro["name"])
        far = nav_core.field_locate((0.0, 0.0, 5.0e9), fields)
        self.assertEqual(far["status"], "space")
        self.assertIn("field", far)                     # nearest still reported

    # --- pocket-mode planning (synthetic: exact known-answer geometry) --------

    def _ring_fixture(self):
        # Radial chord Inner(13 Gm) -> Outer(17 Gm) passes dead through a
        # pocket on the ring; a second pocket a quarter-turn away is off-chord.
        inner = _space_poi(11, "Inner", (13.0e9, 0, 0), system="Nyx")
        outer = _space_poi(12, "Outer", (17.0e9, 0, 0), system="Nyx")
        on = {"key": "Wtn-001", "kind": "general",
              "xyz": (nav_core.GLACIEM_R_M, 0.0, 0.0),
              "grid_radius_m": nav_core.GLACIEM_POCKET_RADIUS_M}
        off = {"key": "Wtn-090", "kind": "general",
               "xyz": (0.0, nav_core.GLACIEM_R_M, 0.0),
               "grid_radius_m": nav_core.GLACIEM_POCKET_RADIUS_M}
        return _synthetic_nav([inner, outer], system="Nyx"), inner, outer, [on, off]

    def test_pocket_plan_hits_on_chord_pocket(self):
        nav2, inner, outer, pockets = self._ring_fixture()
        plan = nav_core.plan_halo_drop(nav2, start=inner, pockets=pockets,
                                       system="Nyx", markers=[outer])
        self.assertEqual((plan["system"], plan["mode"]), ("Nyx", "pocket"))
        d = plan["drop"]
        self.assertEqual(d["pocket"]["key"], "Wtn-001")
        self.assertTrue(d["pocket"]["hit"])
        self.assertLess(d["expected_miss_m"], 1.0)      # dead-on radial chord
        # drop number = distance from the ring crossing to Outer = 2 Gm
        self.assertAlmostEqual(d["peak_m"], 2.0e9, delta=1e6)
        self.assertIn("Star Citizen game data", plan["attribution"])

    def test_pocket_plan_stages_when_direct_chords_miss(self):
        # Lofted 1 Gm above the ring: the direct chord to Outer passes far
        # over every pocket, so the planner must stage through Inner and hit
        # the pocket on the flat Inner -> Outer chord.
        nav2, inner, outer, pockets = self._ring_fixture()
        start = nav_core.Poi(
            id=-1, name="your location", system="Nyx", container_name=None,
            type="", local_km=None, global_m=(nav_core.GLACIEM_R_M, 0, 1.0e9),
            latitude=None, longitude=None, height_m=None, qt_marker=False)
        plan = nav_core.plan_halo_drop(nav2, start=start, pockets=pockets,
                                       system="Nyx", markers=[inner, outer])
        self.assertTrue(plan["staged"])
        self.assertTrue(plan["drop"]["pocket"]["hit"])
        self.assertEqual(plan["legs"][0]["kind"], "travel")
        self.assertEqual(plan["legs"][1]["kind"], "drop")

    def test_pocket_plan_validates_inputs(self):
        nav2, inner, outer, pockets = self._ring_fixture()
        with self.assertRaises(ValueError):
            nav_core.plan_halo_drop(nav2, start=inner, pockets=pockets, band=5,
                                    system="Nyx", markers=[outer])   # two goals
        with self.assertRaises(ValueError):
            nav_core.plan_halo_drop(nav2, start=inner, pockets=[],
                                    system="Nyx", markers=[outer])   # no ring data

    # --- pocket-mode planning (real dataset, self-derived fixture) -----------

    def test_pocket_plan_from_gateway_real_data(self):
        # Raw dataset (wiki catalog off) has only ~5 Nyx markers, so a true
        # in-pocket hit isn't guaranteed — but the plan must exist, target a
        # real pocket near the ring radius, and miss by no more than a couple
        # of grid radii. (With the wiki markers folded in, the same start
        # yields sub-2,000 km in-pocket hits — exercised in test_app.)
        gate = next(p for p in self.nav.qt_markers
                    if p.system == "Nyx" and "Pyro" in p.name)
        gen = [p for p in self.belts["Nyx"]["pockets"] if p["kind"] == "general"]
        plan = nav_core.plan_halo_drop(
            self.nav, start=gate, pockets=gen, system="Nyx",
            volumes=nav_core.body_volumes(self.nav, "Nyx"))
        d = plan["drop"]
        r = math.hypot(d["crossing_xyz"][0], d["crossing_xyz"][1])
        self.assertAlmostEqual(r, nav_core.GLACIEM_R_M, delta=50e6)
        self.assertLess(d["expected_miss_m"], 3 * nav_core.GLACIEM_POCKET_RADIUS_M)
        # alternates offer distinct markers (pocket mode generates one
        # candidate per (marker, pocket) pair — no rescored twins)
        ids = [d["marker_id"]] + [a["drop"]["marker_id"] for a in plan["alternates"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_pyro_field_plan_real_data(self):
        # Single-point POI mode with a registry target: plannable from a real
        # Pyro station against the raw dataset's markers.
        fields = self.belts["Pyro"]["fields"]
        akiro = next(f for f in fields if "Akiro" in f["name"])
        target = nav_core.Poi(
            id=-2, name=akiro["name"], system="Pyro", container_name=None,
            type="Asteroid Field", local_km=None, global_m=akiro["xyz"],
            latitude=None, longitude=None, height_m=None, qt_marker=False)
        start = next(p for p in self.nav.qt_markers
                     if p.system == "Pyro" and "Ruin" in p.name)
        plan = nav_core.plan_halo_drop(
            self.nav, start=start, target=target, system="Pyro",
            volumes=nav_core.body_volumes(self.nav, "Pyro"))
        self.assertEqual((plan["system"], plan["mode"]), ("Pyro", "poi"))
        self.assertEqual(plan["target"]["name"], akiro["name"])
        self.assertIn("expected_miss_m", plan["drop"])
        self.assertIn("CC BY-SA", plan["attribution"])


def _survey_mark(pid, xyz, rocks="dense", ores=(), private=False,
                 system="Nyx", payload=True):
    return nav_core.Poi(
        id=pid, name=f"mark {pid}", system=system, container_name=None,
        type="survey", local_km=None,
        global_m=(float(xyz[0]), float(xyz[1]), float(xyz[2])),
        latitude=None, longitude=None, height_m=None, qt_marker=False,
        custom=True, private=private,
        survey=({"rocks": rocks, "ores": list(ores)} if payload else None))


class BeltSurveyTests(unittest.TestCase):
    """Belt survey (#36): the Keeger region, tier-1 surveyed pockets (live
    from the FIRST rock mark — a mark is ground truth), the tier-2 field
    model's sample gate, the keeger locate verdicts, and planning into a
    surveyed pocket. All synthetic fixtures — the survey exists precisely
    because no game data does."""

    KR = nav_core.KEEGER_R_M

    def _nav_with(self, marks, extra_pois=()):
        nav = _synthetic_nav(list(extra_pois), system="Nyx")
        for m in marks:
            nav.pois[m.id] = m
        return nav

    def test_keeger_contains_bounds(self):
        self.assertTrue(nav_core.keeger_contains((self.KR, 0, 0)))
        self.assertTrue(nav_core.keeger_contains((0, -self.KR - 2.0e9, 0.5e9)))
        self.assertFalse(nav_core.keeger_contains((self.KR + 3.0e9, 0, 0)))
        self.assertFalse(nav_core.keeger_contains((self.KR, 0, 1.5e9)))
        # the Glaciem Ring is not the Keeger Belt (and vice versa)
        self.assertFalse(nav_core.keeger_contains((nav_core.GLACIEM_R_M, 0, 0)))
        self.assertFalse(nav_core.glaciem_contains((self.KR, 0, 0)))

    def test_first_mark_is_a_live_pocket(self):
        nav = self._nav_with([_survey_mark(1_000_001, (self.KR, 0, 0))])
        pockets = nav_core.survey_pockets(nav, "Nyx")
        self.assertEqual(len(pockets), 1)
        pk = pockets[0]
        self.assertEqual(pk["key"], "SVY-1")
        self.assertEqual((pk["kind"], pk["marks"]), ("surveyed", 1))
        self.assertEqual(pk["xyz"], (self.KR, 0.0, 0.0))
        self.assertEqual(pk["grid_radius_m"], nav_core.GLACIEM_POCKET_RADIUS_M)

    def test_nearby_marks_merge_and_refine(self):
        off = nav_core.SURVEY_MERGE_M * 0.5
        nav = self._nav_with([
            _survey_mark(1_000_001, (self.KR, 0, 0), ores=("Aluminum",)),
            _survey_mark(1_000_002, (self.KR + off, 0, 0), rocks="sparse",
                         ores=("Gold",)),
            _survey_mark(1_000_003, (0, self.KR, 0)),        # far: own pocket
        ])
        pockets = nav_core.survey_pockets(nav, "Nyx")
        self.assertEqual(len(pockets), 2)
        merged = next(p for p in pockets if p["marks"] == 2)
        self.assertEqual(merged["key"], "SVY-1")             # anchor = lowest id
        self.assertAlmostEqual(merged["xyz"][0], self.KR + off / 2, delta=1.0)
        self.assertEqual(merged["density"], "dense")          # densest member
        self.assertEqual(merged["ores"], ["Aluminum", "Gold"])

    def test_negative_marks_bound_but_never_target(self):
        nav = self._nav_with([_survey_mark(1_000_001, (self.KR, 0, 0),
                                           rocks="none")])
        self.assertEqual(nav_core.survey_pockets(nav, "Nyx"), [])
        # a negative near a positive caps the pocket's envelope
        near = nav_core.GLACIEM_POCKET_RADIUS_M            # negative one pocket-radius out
        nav2 = self._nav_with([
            _survey_mark(1_000_001, (self.KR, 0, 0)),
            _survey_mark(1_000_002, (self.KR + near, 0, 0), rocks="none"),
        ])
        pk = nav_core.survey_pockets(nav2, "Nyx")[0]
        self.assertLess(pk["grid_radius_m"], nav_core.GLACIEM_POCKET_RADIUS_M)

    def test_private_glaciem_and_payloadless_marks(self):
        nav = self._nav_with([
            _survey_mark(1_000_001, (self.KR, 0, 0), private=True),   # excluded
            _survey_mark(1_000_002, (nav_core.GLACIEM_R_M, 0, 0)),    # glaciem: excluded
            _survey_mark(1_000_003, (0, self.KR, 0), payload=False),  # defaults +medium
        ])
        pockets = nav_core.survey_pockets(nav, "Nyx")
        self.assertEqual(len(pockets), 1)
        self.assertEqual((pockets[0]["key"], pockets[0]["density"]),
                         ("SVY-3", "medium"))

    def test_field_model_gate_and_stats(self):
        few = [_survey_mark(1_000_000 + i,
                            (self.KR * math.cos(i * 0.2),
                             self.KR * math.sin(i * 0.2), 0))
               for i in range(10)]
        nav = self._nav_with(few)
        self.assertIsNone(nav_core.survey_field_model(
            nav_core.survey_marks(nav, "Nyx")))
        many = [_survey_mark(
                    1_000_000 + i,
                    ((self.KR + (i % 5 - 2) * 1e8) * math.cos(i * 0.21),
                     (self.KR + (i % 5 - 2) * 1e8) * math.sin(i * 0.21),
                     (i % 3 - 1) * 2e8))
                for i in range(30)]
        nav2 = self._nav_with(many)
        model = nav_core.survey_field_model(nav_core.survey_marks(nav2, "Nyx"))
        self.assertEqual(model["samples"], 30)
        self.assertAlmostEqual(model["r_med_m"], self.KR, delta=3e8)
        self.assertLessEqual(model["half_height_m"], 2.1e8)
        self.assertGreater(model["coverage"], 0.05)

    def test_keeger_locate_verdicts(self):
        nav = self._nav_with([_survey_mark(1_000_001, (self.KR, 0, 0))])
        pockets = nav_core.survey_pockets(nav, "Nyx")
        at = nav_core.keeger_locate((self.KR, 0, 100e3), pockets)
        self.assertEqual(at["status"], "keeger_pocket")
        self.assertEqual(at["pocket"]["key"], "SVY-1")
        region = nav_core.keeger_locate((0, self.KR + 1.0e9, 0), pockets)
        self.assertEqual(region["status"], "keeger")
        self.assertAlmostEqual(region["to_ring_m"], 1.0e9, delta=1e6)
        self.assertIsNone(nav_core.keeger_locate((20e9, 0, 0), pockets))

    def test_plan_into_surveyed_pocket(self):
        # Radial chord Inner(44 Gm) -> Outer(52 Gm) passes dead through the
        # org's first surveyed pocket at 48 Gm: plannable from mark #1.
        inner = _space_poi(11, "Inner", (44.0e9, 0, 0), system="Nyx")
        outer = _space_poi(12, "Outer", (52.0e9, 0, 0), system="Nyx")
        mark = _survey_mark(1_000_001, (self.KR, 0, 0), ores=("Aluminum",))
        nav = self._nav_with([mark], extra_pois=[inner, outer])
        plan = nav_core.plan_halo_drop(
            nav, start=inner, pockets=nav_core.survey_pockets(nav, "Nyx"),
            system="Nyx", markers=[outer])
        d = plan["drop"]
        self.assertTrue(d["pocket"]["hit"])
        self.assertEqual(d["pocket"]["kind"], "surveyed")
        self.assertEqual(d["pocket"]["marks"], 1)          # confidence badge
        self.assertEqual(d["pocket"]["ores"], ["Aluminum"])
        self.assertAlmostEqual(d["peak_m"], 4.0e9, delta=1e6)


class ResourceValueTierTests(unittest.TestCase):
    """resource_value_tiers: the mining-value badge buckets (#32)."""

    def test_terciles_split_a_nine_price_spread(self):
        prices = {f"ore{i}": float(i) for i in range(1, 10)}  # 1..9
        tiers = nav_core.resource_value_tiers(prices)
        self.assertEqual({n for n, v in tiers.items() if v["tier"] == "low"},
                         {"ore1", "ore2", "ore3"})
        self.assertEqual({n for n, v in tiers.items() if v["tier"] == "medium"},
                         {"ore4", "ore5", "ore6"})
        self.assertEqual({n for n, v in tiers.items() if v["tier"] == "high"},
                         {"ore7", "ore8", "ore9"})

    def test_rank_based_so_outliers_cannot_squash_the_scale(self):
        # A 23M outlier (Jaclium) must not push everything else into "low".
        prices = {"Jaclium": 23_000_000, "A": 30_000, "B": 20_000,
                  "C": 10_000, "D": 5_000, "E": 1_200}
        tiers = nav_core.resource_value_tiers(prices)
        self.assertEqual(tiers["Jaclium"]["tier"], "high")
        self.assertEqual(tiers["A"]["tier"], "high")
        self.assertEqual(tiers["E"]["tier"], "low")

    def test_unpriced_and_nonpositive_names_are_omitted(self):
        tiers = nav_core.resource_value_tiers(
            {"priced": 100.0, "zero": 0, "none": None, "bogus": "n/a"})
        self.assertEqual(set(tiers), {"priced"})

    def test_flat_or_single_price_is_medium_not_high(self):
        # No contrast -> no verdict; everything reads "medium".
        self.assertEqual(
            nav_core.resource_value_tiers({"a": 5.0, "b": 5.0, "c": 5.0}),
            {"a": {"sell": 5, "tier": "medium"}, "b": {"sell": 5, "tier": "medium"},
             "c": {"sell": 5, "tier": "medium"}})
        self.assertEqual(nav_core.resource_value_tiers({"only": 9.0}),
                         {"only": {"sell": 9, "tier": "medium"}})

    def test_empty_input(self):
        self.assertEqual(nav_core.resource_value_tiers({}), {})

    def test_sell_reference_is_rounded_to_whole_auec(self):
        self.assertEqual(nav_core.resource_value_tiers({"a": 10.6})["a"]["sell"], 11)


class ScopeIndexTests(unittest.TestCase):
    """compute_state pulls candidates from a cached per-container index instead of
    scanning the whole dataset every call (the per-recompute cost that a 50-player
    mapping party multiplies). The index must scope exactly like the old
    _in_scope filter did AND self-invalidate when the dataset changes."""

    R = 295_000.0

    def _nav(self):
        nav = nav_core.NavData()
        # Two bodies, far apart so a fix at one isn't within the other's radius.
        for name, pos in [("Yela", (0.0, 0.0, 0.0)), ("Daymar", (5e9, 0.0, 0.0))]:
            nav.containers[("Stanton", name)] = nav_core.Container(
                name=name, system="Stanton", type="Moon", internal_name="",
                pos=pos, body_radius=self.R, om_radius=0, grid_radius=0,
                rotation_speed=0, rotation_adjustment=0,
            )
        return nav

    def _obs(self, nav, oid, body, gm):
        # Explicit global_m so distance resolves; container_name drives scoping.
        nav.observations[oid] = nav_core.Observation(
            id=oid, category="resource", system="Stanton", container_name=body,
            local_km=None, global_m=gm, latitude=None, longitude=None, height_m=0.0,
            biome=None, note=None, owner_id=None, owner_handle=None,
            observed_at="2026-01-01", data={"ore": "Quantanium"},
        )

    def test_build_scope_index_buckets_by_container(self):
        nav = self._nav()
        self._obs(nav, 1, "Yela", (0.0, 0.0, 0.0))
        self._obs(nav, 2, "Yela", (0.0, 0.0, 0.0))
        self._obs(nav, 3, "Daymar", (5e9, 0.0, 0.0))
        _pois, obs = nav_core.build_scope_index(nav)
        self.assertEqual({o.id for o in obs[("Stanton", "Yela")]["resource"]}, {1, 2})
        self.assertEqual({o.id for o in obs[("Stanton", "Daymar")]["resource"]}, {3})

    def test_scope_index_caches_and_rebuilds_on_count_change(self):
        nav = self._nav()
        self._obs(nav, 1, "Yela", (0.0, 0.0, 0.0))
        _, obs_a = nav_core.scope_index(nav)
        _, obs_a2 = nav_core.scope_index(nav)
        self.assertIs(obs_a, obs_a2)                 # same count -> cached object
        self._obs(nav, 2, "Yela", (0.0, 0.0, 0.0))   # count changed
        _, obs_b = nav_core.scope_index(nav)
        self.assertIsNot(obs_a, obs_b)               # rebuilt, not stale
        self.assertEqual({o.id for o in obs_b[("Stanton", "Yela")]["resource"]}, {1, 2})

    def test_obs_by_category_caches_and_invalidates_on_touch(self):
        # The whole-dataset per-category pool (deep-space fixes + the element
        # finder) shares scope_index's invalidation: same-version reads are
        # the cached object; a touch()'d mutation rebuilds it.
        nav = self._nav()
        self._obs(nav, 1, "Yela", (0.0, 0.0, 0.0))
        a = nav_core.obs_by_category(nav)
        self.assertIs(a, nav_core.obs_by_category(nav))       # cached
        self.assertEqual({o.id for o in a["resource"]}, {1})
        self._obs(nav, 2, "Daymar", (5e9, 0.0, 0.0))
        nav.touch()
        b = nav_core.obs_by_category(nav)
        self.assertEqual({o.id for o in b["resource"]}, {1, 2})

    def test_obs_on_body_reads_scope_index(self):
        # _obs_on_body used to scan every observation the org ever recorded
        # (×4 per on-body fix via resource_forecast); it must now resolve from
        # the scoped buckets — and still exclude lat/lon-less sightings.
        nav = self._nav()
        self._obs(nav, 1, "Yela", (0.0, 0.0, 0.0))
        nav.observations[1].latitude, nav.observations[1].longitude = 1.0, 2.0
        self._obs(nav, 2, "Yela", (0.0, 0.0, 0.0))            # no lat/lon
        self._obs(nav, 3, "Daymar", (5e9, 0.0, 0.0))
        nav.observations[3].latitude, nav.observations[3].longitude = 3.0, 4.0
        got = nav_core._obs_on_body(nav, "Stanton", "Yela")
        self.assertEqual({o.id for o in got}, {1})

    def test_compute_state_scopes_to_current_body(self):
        nav = self._nav()
        self._obs(nav, 1, "Yela", (100_000.0, 0.0, 0.0))
        self._obs(nav, 2, "Daymar", (5e9, 0.0, 0.0))
        at_yela = (100_000.0, 0.0, 0.0)   # inside Yela's detection radius
        self.assertEqual(detect_container(nav, at_yela).name, "Yela")
        ids = {o["id"] for o in compute_state(nav, at_yela, time.time())["nearest_observations"]}
        self.assertIn(1, ids)          # this body's node surfaces
        self.assertNotIn(2, ids)       # the other body's node is scoped out entirely
        # Cache invalidation end-to-end: a freshly-added node on this body appears
        # on the next recompute (no stale index).
        self._obs(nav, 3, "Yela", (100_000.0, 0.0, 0.0))
        ids2 = {o["id"] for o in compute_state(nav, at_yela, time.time())["nearest_observations"]}
        self.assertIn(3, ids2)

    def test_deep_space_considers_all_systems(self):
        nav = self._nav()
        self._obs(nav, 1, "Yela", (100_000.0, 0.0, 0.0))
        self._obs(nav, 2, "Daymar", (5e9, 0.0, 0.0))
        # No container in deep space -> everything is in scope (matches old behavior).
        deep = (9e12, 9e12, 9e12)
        self.assertIsNone(detect_container(nav, deep))
        ids = {o["id"] for o in compute_state(nav, deep, time.time())["nearest_observations"]}
        self.assertEqual(ids, {1, 2})


if __name__ == "__main__":
    unittest.main(verbosity=1)
