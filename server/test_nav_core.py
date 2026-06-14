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
        # captured right at Kudre Ore (a QT marker) -> that's the nearest
        self.assertEqual(obs.nearest_qt, "Kudre Ore")
        self.assertEqual(nav_core._observation_base(obs)["nearest_qt"], "Kudre Ore")


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


if __name__ == "__main__":
    unittest.main(verbosity=1)
