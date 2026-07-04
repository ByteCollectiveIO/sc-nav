"""nav_core tests against the real poi/containers dataset.

Run: python3 test_nav_core.py
"""

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


if __name__ == "__main__":
    unittest.main(verbosity=1)
