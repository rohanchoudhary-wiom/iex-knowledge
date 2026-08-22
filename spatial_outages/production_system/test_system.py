import io
import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import DEFAULT_OUTAGE_URL, Service, fetch_open_outages, parse_outage_feed, validate_request
from attribution import AttributionEngine, Device, Inventory, StatusClient, _normalize_status_response, device_state
from attribution.spatial import comparison_polygon, directional_profile, inside_polygon, radius_core, radius_profile, strongest_window
from test_fixtures import TEST_OUTAGES, test_data


class AttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
        inventory, ages = test_data()

        def statuses(device_ids):
            return {
                device_id: self.now - timedelta(seconds=ages[device_id]) if ages[device_id] is not None else None
                for device_id in device_ids
            }

        def address_entities(addresses):
            return [{
                "house_details": [{"text": "H-1", "confidence": .88}],
                "road": [{"text": "Gali 2", "confidence": .84}],
                "locality": [{"text": "Delhi", "confidence": .90}],
            } if address == "H-1 Gali 2 Delhi" else {} for address in addresses]

        self.engine = AttributionEngine(inventory, statuses, address_reader=address_entities)

    def evaluate(self, index):
        outage = TEST_OUTAGES[index]
        return self.engine.evaluate(**{
            "outage_id": outage["outage_id"],
            "member_ids": outage["devices"],
            "ongoing_time": outage["ongoing_time"],
            "evaluated_at": self.now,
        })

    def compact_fibre_case(self, address_match=True, healthy=True, diffuse=False):
        coordinates = (
            [(28, 77 + index * .008) for index in range(4)]
            if diffuse else [(28, 77), (28.0002, 77), (28, 77.0002)]
        )
        devices = {
            f"D{index}": Device(f"D{index}", "C1", latitude, longitude, "Gali 3 Delhi")
            for index, (latitude, longitude) in enumerate(coordinates)
        }
        if healthy:
            devices["H"] = Device("H", "C1", 28.00005, 77.00005)
        devices.update({
            f"U{index}": Device(f"U{index}", "C1", 29 + index * .001, 78)
            for index in range(10)
        })
        down_ids = {device_id for device_id in devices if device_id.startswith("D")}
        engine = AttributionEngine(
            Inventory(devices),
            lambda ids: {
                device_id: self.now - timedelta(minutes=11 if device_id in down_ids else 1)
                for device_id in ids
            },
            address_reader=lambda addresses: [{
                "road": [{"text": "Gali 3", "confidence": .86}],
                "locality": [{"text": "Delhi", "confidence": .9}],
            } if address_match else {} for _ in addresses],
        )
        return engine.evaluate("COMPACT", sorted(down_ids), 900, self.now)

    def locality_fibre_case(self, control_up=8, down_count=6, radius_m=250):
        coordinates = [(
            28 + math.sin(2 * math.pi * index / down_count) * radius_m / 111_000,
            77 + math.cos(2 * math.pi * index / down_count) * radius_m / (111_000 * math.cos(math.radians(28))),
        ) for index in range(down_count)]
        devices = {
            f"D{index}": Device(f"D{index}", "C1", latitude, longitude, "Morna Sector 35")
            for index, (latitude, longitude) in enumerate(coordinates)
        }
        devices.update({
            f"C{index}": Device(f"C{index}", "C1", 28 + index * .00001, 77)
            for index in range(10)
        })
        outage_ids = {f"D{index}" for index in range(down_count)}
        engine = AttributionEngine(
            Inventory(devices),
            lambda ids: {
                device_id: self.now - timedelta(
                    minutes=1 if device_id.startswith("C") and int(device_id[1:]) < control_up else 11
                )
                for device_id in ids
            },
            address_reader=lambda addresses: [{
                "locality": [{"text": "Morna Sector 35", "confidence": .84}],
            } if address == "Morna Sector 35" else {} for address in addresses],
        )
        return engine.evaluate("LOCALITY", sorted(outage_ids), 900, self.now)

    def cross_csp_fibre_case(self, address_match=True, locality_only=False, concurrent=True):
        address = "Gali 7 Budh Vihar"
        devices = {
            "M": Device("M", "C1", 28, 77, address),
            "X1": Device("X1", "C2", 28.00003, 77, address),
            "X2": Device("X2", "C3", 28, 77.00003, address),
            **{
                f"H{index}": Device(
                    f"H{index}", "C1", 28.00001 + index * .000003, 77.00001
                )
                for index in range(6)
            },
            **{f"U{index}": Device(f"U{index}", "C1", 29 + index * .001, 78) for index in range(10)},
        }
        down = {"M", "X1", "X2"}
        down_minutes = {"M": 11, "X1": 11 if concurrent else 31, "X2": 11 if concurrent else 61}
        engine = AttributionEngine(
            Inventory(devices),
            lambda ids: {
                device_id: self.now - timedelta(minutes=down_minutes[device_id] if device_id in down else 1)
                for device_id in ids
            },
            address_reader=lambda addresses: [{
                **({} if locality_only else {"road": [{"text": "Gali 7", "confidence": .86}]}),
                "locality": [{"text": "Budh Vihar", "confidence": .9}],
            } if address_match and value == address else {} for value in addresses],
        )
        return engine.evaluate("CROSS-CSP", ["M"], 900, self.now)

    def directional_fibre_case(
        self, shape="line", down_count=15, control_up=10, control_unknown=0,
        mixed_member_csps=False,
    ):
        member_indices = set(range(down_count)) if down_count < 10 else {
            round(index * (down_count - 1) / 9) for index in range(10)
        }
        coordinates = []
        for index in range(down_count):
            if shape == "radial":
                angle = 2 * math.pi * index / down_count
                x, y = 80 * math.cos(angle), 80 * math.sin(angle)
            else:
                x = 0 if down_count == 1 else -70 + 140 * index / (down_count - 1)
                y = 0
            coordinates.append((
                28 + y / 111_000,
                77 + x / (111_000 * math.cos(math.radians(28))),
            ))
        devices, down_ids, member_ids = {}, set(), []
        for index, (latitude, longitude) in enumerate(coordinates):
            device_id = f"M{index}" if index in member_indices else f"X{index}"
            csp_id = "C2" if index not in member_indices or mixed_member_csps and index % 2 else "C1"
            devices[device_id] = Device(device_id, csp_id, latitude, longitude)
            down_ids.add(device_id)
            if index in member_indices:
                member_ids.append(device_id)
        control_total = control_up + control_unknown
        for index in range(control_total):
            x = -40 + 80 * index / max(1, control_total - 1)
            y = 25 if index % 2 else -25
            devices[f"H{index}"] = Device(
                f"H{index}", "C3", 28 + y / 111_000,
                77 + x / (111_000 * math.cos(math.radians(28))),
            )
        devices.update({
            f"U{index}": Device(f"U{index}", "C1", 29 + index * .001, 78)
            for index in range(20)
        })
        unknown_ids = {f"H{index}" for index in range(control_up, control_total)}
        engine = AttributionEngine(
            Inventory(devices),
            lambda ids: {
                device_id: None if device_id in unknown_ids else self.now - timedelta(
                    minutes=11 if device_id in down_ids else 1
                )
                for device_id in ids
            },
            address_reader=lambda addresses: [{} for _ in addresses],
        )
        return engine.evaluate("DIRECTIONAL", member_ids, 900, self.now)

    def test_compact_same_gali_with_healthy_control_is_fibre(self):
        public, detail = self.compact_fibre_case()

        self.assertEqual(("FIBRE_CUT", ["D0", "D1", "D2"]), (
            public["attribution"], public["affected_device_ids"]
        ))
        self.assertFalse(detail["groups"][0]["supported"])
        self.assertEqual(["MIN_DOWN_MEMBERS"], detail["groups"][0]["review_reasons"])
        self.assertIsNone(detail["groups"][0]["local_csp_evidence"])
        self.assertTrue(detail["groups"][0]["address_evidence"]["matched"])
        self.assertEqual(("RULE_4A", "RULE_4A_HOUSE_GALI"), (
            detail["groups"][0]["address_evidence"]["path"], detail["groups"][0]["decision_rule"]
        ))

    def test_locality_counterfactual_fallback_is_fibre(self):
        public, detail = self.locality_fibre_case()
        group = detail["groups"][0]

        self.assertEqual(("FIBRE_CUT", .6, [f"D{index}" for index in range(6)]), (
            public["attribution"], public["confidence"], public["affected_device_ids"]
        ))
        self.assertEqual("RULE_4B_LOCALITY_CONTROLS", group["decision_rule"])
        self.assertEqual(("RULE_4B", 10, .8, 1.0), (
            group["address_evidence"]["path"],
            group["address_evidence"]["known_control_count"],
            group["address_evidence"]["control_up_share"],
            group["timing"]["strongest_10m_share"],
        ))

    def test_locality_fallback_rejects_low_control_up_share(self):
        public, detail = self.locality_fibre_case(control_up=6)
        evidence = detail["groups"][0]["address_evidence"]

        self.assertEqual("UNKNOWN", public["attribution"])
        self.assertEqual(.6, evidence["control_up_share"])
        self.assertFalse(evidence["matched"])

    def test_supported_locality_fallback_uses_500m_limit(self):
        public, detail = self.locality_fibre_case(down_count=10, radius_m=400)
        rejected, rejected_detail = self.locality_fibre_case(down_count=10, radius_m=550)

        self.assertTrue(detail["groups"][0]["supported"])
        self.assertEqual(("FIBRE_CUT", "RULE_4B_LOCALITY_CONTROLS"), (
            public["attribution"], detail["groups"][0]["decision_rule"]
        ))
        self.assertEqual("UNKNOWN", rejected["attribution"])
        self.assertGreater(rejected_detail["groups"][0]["radii_m"]["r90"], 500)

    def test_cross_csp_comparison_down_devices_are_fibre_candidates(self):
        public, detail = self.cross_csp_fibre_case()
        evidence = detail["groups"][0]["address_evidence"]

        self.assertEqual(("FIBRE_CUT", ["M", "X1", "X2"]), (
            public["attribution"], public["affected_device_ids"]
        ))
        self.assertEqual(["M", "X1", "X2"], evidence["candidate_down_ids"])
        self.assertEqual(["M"], evidence["affected_member_ids"])
        self.assertEqual(["X1", "X2"], evidence["affected_comparison_ids"])

    def test_unmatched_comparison_down_devices_remain_unaffected(self):
        public, detail = self.cross_csp_fibre_case(address_match=False)
        evidence = detail["groups"][0]["address_evidence"]

        self.assertEqual(("UNKNOWN", []), (public["attribution"], public["affected_device_ids"]))
        self.assertEqual(["M", "X1", "X2"], evidence["candidate_down_ids"])
        self.assertEqual([], evidence["affected_member_ids"])
        self.assertEqual([], evidence["affected_comparison_ids"])

    def test_cross_csp_locality_fibre_does_not_require_timing_concurrency(self):
        public, detail = self.cross_csp_fibre_case(locality_only=True, concurrent=False)
        group = detail["groups"][0]
        evidence = group["address_evidence"]

        self.assertEqual(("FIBRE_CUT", "RULE_4B_LOCALITY_CONTROLS"), (
            public["attribution"], group["decision_rule"]
        ))
        self.assertEqual(["M", "X1", "X2"], public["affected_device_ids"])
        self.assertLess(evidence["strongest_10m_share"], .8)
        self.assertEqual(("budh vihar", 3, 6, 1.0), (
            evidence["shared_locality"], evidence["shared_locality_count"],
            evidence["known_control_count"], evidence["control_up_share"],
        ))

    def test_directional_all_csp_cluster_is_fibre(self):
        public, detail = self.directional_fibre_case()
        evidence = detail["groups"][0]["address_evidence"]

        self.assertEqual(("FIBRE_CUT", "RULE_4C_DIRECTIONAL_CLUSTER", .6), (
            public["attribution"], detail["groups"][0]["decision_rule"], public["confidence"]
        ))
        self.assertGreaterEqual(len(evidence["affected_device_ids"]), 10)
        self.assertTrue(evidence["affected_member_ids"])
        self.assertTrue(evidence["affected_comparison_ids"])
        self.assertGreaterEqual(evidence["directionality_ratio"], 3)
        self.assertLessEqual(evidence["perpendicular_p90_m"], 50)

    def test_directional_fibre_rejects_small_radial_and_unhealthy_patterns(self):
        cases = (
            self.directional_fibre_case(down_count=4),
            self.directional_fibre_case(shape="radial", mixed_member_csps=True),
            self.directional_fibre_case(control_up=3, control_unknown=2),
        )
        for public, detail in cases:
            with self.subTest(detail=detail["groups"][0]["address_evidence"]):
                self.assertEqual("UNKNOWN", public["attribution"])
                self.assertNotEqual("RULE_4C_DIRECTIONAL_CLUSTER", detail["groups"][0]["decision_rule"])

    def test_directional_fibre_affects_only_selected_component(self):
        line = [Device(f"D{index}", "C1", 28, 77 + index * .0002) for index in range(6)]
        outlier = Device("O", "C2", 28.004, 77)
        healthy = [Device(f"H{index}", "C3", 28.001, 77 + index * .00005) for index in range(8)]
        devices = [*line, outlier, *healthy]
        states = {
            device.device_id: (
                "DOWN" if device.device_id.startswith(("D", "O")) else "UP",
                (self.now - timedelta(minutes=11 if device.device_id.startswith(("D", "O")) else 1)).isoformat(),
            )
            for device in devices
        }
        engine = AttributionEngine(
            Inventory({device.device_id: device for device in devices}), lambda ids: {},
            address_reader=lambda addresses: [{} for _ in addresses],
        )

        evidence = engine._fibre_evidence(line, devices, states, {device.device_id for device in line})

        self.assertEqual(("RULE_4C", [device.device_id for device in line]), (
            evidence["path"], evidence["affected_device_ids"]
        ))
        self.assertIn("O", evidence["candidate_down_ids"])
        self.assertNotIn("O", evidence["affected_device_ids"])
        self.assertGreaterEqual(evidence["directional_component_count"], 2)

    def test_compact_fibre_requires_address_control_and_spatial_validity(self):
        missing_address, missing_address_detail = self.compact_fibre_case(address_match=False)
        no_control, no_control_detail = self.compact_fibre_case(healthy=False)
        self.assertEqual(["H"], missing_address_detail["groups"][0]["address_evidence"]["healthy_device_ids"])
        self.assertEqual([], no_control_detail["groups"][0]["address_evidence"]["healthy_device_ids"])
        for public, detail in ((missing_address, missing_address_detail), (no_control, no_control_detail)):
            group = detail["groups"][0]
            self.assertEqual("UNKNOWN", public["attribution"])
            self.assertIsNone(group["local_csp_evidence"])
            self.assertFalse(group["address_evidence"]["matched"])
            self.assertEqual("RULE_5_NO_HOUSE_GALI", group["decision_rule"])

        public, detail = self.compact_fibre_case(diffuse=True)
        group = detail["groups"][0]
        self.assertEqual("UNKNOWN", public["attribution"])
        self.assertIn("R90_OVER_1KM", group["review_reasons"])
        self.assertIsNone(group["local_csp_evidence"])
        self.assertIsNone(group["address_evidence"])
        self.assertEqual("SPATIAL_REVIEW", group["decision_rule"])

    def test_compact_fibre_rollup_ignores_unmatched_review_noise(self):
        devices = {
            "D0": Device("D0", "C1", 28, 77, "Gali 3 Delhi"),
            "D1": Device("D1", "C1", 28.0002, 77, "Gali 3 Delhi"),
            "D2": Device("D2", "C1", 28, 77.0002, "Gali 3 Delhi"),
            "H": Device("H", "C1", 28.00005, 77.00005),
            "NOISE": Device("NOISE", "C1", 28.1, 77.1),
            **{f"U{index}": Device(f"U{index}", "C1", 29 + index * .001, 78) for index in range(10)},
        }
        down_ids = {"D0", "D1", "D2", "NOISE"}
        engine = AttributionEngine(
            Inventory(devices),
            lambda ids: {
                device_id: self.now - timedelta(minutes=11 if device_id in down_ids else 1)
                for device_id in ids
            },
            address_reader=lambda addresses: [{
                "road": [{"text": "Gali 3", "confidence": .86}],
                "locality": [{"text": "Delhi", "confidence": .9}],
            } if address == "Gali 3 Delhi" else {} for address in addresses],
        )

        public, detail = engine.evaluate("MIXED", sorted(down_ids), 900, self.now)

        self.assertEqual(("FIBRE_CUT", ["D0", "D1", "D2"]), (
            public["attribution"], public["affected_device_ids"]
        ))
        self.assertEqual(["NOISE"], detail["review_device_ids"])

    def test_all_attributions_and_status_semantics(self):
        fibre, fibre_detail = self.evaluate(0)
        power, power_detail = self.evaluate(1)
        isp, _ = self.evaluate(2)
        unknown, _ = self.evaluate(3)
        self.assertEqual("FIBRE_CUT", fibre["attribution"])
        self.assertEqual("PREMISE_POWER", power["attribution"])
        self.assertEqual("ISP_OLT_CSP_SIDE", isp["attribution"])
        self.assertEqual("UNKNOWN", unknown["attribution"])
        self.assertEqual(0.0, unknown["confidence"])
        self.assertEqual(["FA00", "FA01"], fibre["affected_device_ids"])
        self.assertEqual({"device_id", "status", "latest_ping_at", "csp", "in_polygon"}, set(fibre["device_pings"][0]))
        fibre_member = next(device for device in fibre_detail["devices"] if device["device_id"] == "FA10")
        power_peer = next(device for device in power_detail["devices"] if device["device_id"] == "PB00")
        self.assertTrue(fibre_member["member"])
        self.assertEqual("UP", fibre_member["state"])
        self.assertFalse(power_peer["member"])
        self.assertEqual("DOWN", power_peer["state"])

    def test_ten_minute_boundary_and_missing_ping(self):
        self.assertEqual("UP", device_state(self.now - timedelta(seconds=599), self.now)[0])
        self.assertEqual("DOWN", device_state(self.now - timedelta(seconds=600), self.now)[0])
        self.assertEqual("UNKNOWN", device_state(None, self.now)[0])
        self.assertEqual("UNKNOWN", device_state(self.now + timedelta(seconds=1), self.now)[0])

    def test_radius_tail_and_timing_evidence(self):
        times = [self.now, self.now + timedelta(minutes=5), self.now + timedelta(minutes=15)]
        self.assertEqual(2, strongest_window(times))
        spatial = [Device(f"S{index}", "C1", 28, 77 + index * .0001) for index in range(10)]
        center, radii = radius_profile(spatial)
        core, tails = radius_core(spatial, center)
        self.assertLessEqual(radii["r70"], radii["r80"])
        self.assertLessEqual(radii["r80"], radii["r90"])
        self.assertLessEqual(radii["r90"], radii["r100"])
        self.assertEqual(9, len(core))
        self.assertEqual(1, len(tails))

    def test_directional_profile_is_orientation_independent(self):
        profiles = []
        for angle in (0, math.pi / 2, math.pi / 4):
            devices = []
            for index, along in enumerate(range(-60, 61, 20)):
                across = 5 if index % 2 else -5
                x = along * math.cos(angle) - across * math.sin(angle)
                y = along * math.sin(angle) + across * math.cos(angle)
                devices.append(Device(
                    f"D{index}", "C1", 28 + y / 111_000,
                    77 + x / (111_000 * math.cos(math.radians(28))),
                ))
            profiles.append(directional_profile(devices))
        for profile in profiles:
            self.assertAlmostEqual(120, profile["length_m"], delta=1)
            self.assertAlmostEqual(5, profile["perpendicular_p90_m"], delta=1)
            self.assertGreater(profile["directionality_ratio"], 7)
        self.assertAlmostEqual(
            profiles[0]["directionality_ratio"], profiles[2]["directionality_ratio"], delta=.2
        )

    def test_review_component_still_has_comparison_polygon(self):
        device = Device("D1", "C1", 28.6, 77.2)
        boundary = comparison_polygon([device], (28.6, 77.2), 0)
        self.assertEqual(24, len(boundary))
        self.assertTrue(inside_polygon(device, boundary))

    def test_ping_proxy_provenance_and_recovered_members(self):
        _, detail = self.evaluate(0)
        self.assertEqual("LAST_PING_PROXY", detail["groups"][0]["timing"]["source"])
        self.assertIn("FA10", detail["recovered_member_ids"])

    def test_customer_v2_csv_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "customers.csv"
            path.write_text("device_id,csp_id,latitude,longitude,is_active,customer_address\nD1,C1,28,77,true,Delhi\nD1,C1,28,77,true,Delhi\nD2,C1,29,78,false,Noida\n")
            inventory = Inventory.from_csv(path)
            self.assertEqual(["D1"], sorted(inventory.devices))
            self.assertEqual("Delhi", inventory.devices["D1"].address)

    def test_status_response_and_request_validation(self):
        response = {
            "status": 0,
            "data": {
                "requested": 2,
                "resolved": 1,
                "notFound": 1,
                "truncated": False,
                "devices": [
                    {"deviceId": "D1", "latestPing": "08/19/2026 12:00:00", "source": "influx"},
                    {"deviceId": "D2", "latestPing": None, "source": "none"},
                ],
            },
        }
        self.assertEqual(
            {"D1": "08/19/2026 12:00:00", "D2": None},
            _normalize_status_response(response),
        )
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            _normalize_status_response({"status": 0, "data": {"truncated": True, "devices": []}})
        with patch("attribution.status.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as urlopen:
            self.assertEqual(response["data"]["devices"][0]["latestPing"], StatusClient("https://status.test")(["D1", "D2"])["D1"])
            request = urlopen.call_args.args[0]
            self.assertEqual("POST", request.get_method())
            self.assertEqual({"deviceIds": ["D1", "D2"]}, json.loads(request.data))
            self.assertEqual("IEX-Outage-Attribution/1.0", request.headers["User-agent"])
        self.assertEqual("DOWN", device_state("08/19/2026 11:50:00", self.now)[0])
        self.assertEqual("UP", device_state("08/19/2026 17:29:00", self.now)[0])
        self.assertEqual((1, ["D1"], 0), validate_request({"outage_id": 1, "devices": ["D1"], "ongoing_time": 0}))
        self.assertEqual((1, ["D1", "D2"], 0), validate_request({"outage_id": 1, "devices": ["D1"], "recovered_devices": ["D2"], "ongoing_time": 0}))
        with self.assertRaisesRegex(ValueError, "duplicates"):
            validate_request({"outage_id": 1, "devices": ["D1", "D1"], "ongoing_time": 0})

    def test_real_feed_refresh_is_validated_and_atomic(self):
        as_of = datetime.now(timezone.utc)
        parsed = parse_outage_feed({"count": 1, "as_of": as_of.timestamp(), "outages": [TEST_OUTAGES[0]]})
        service = Service(self.engine, lambda: parsed)
        service.refresh()
        payload = service.map_data()
        self.assertEqual("LIVE", payload["source"])
        self.assertEqual("ok", payload["status"])
        self.assertEqual(["TEST-FIBRE"], [row["outage_id"] for row in payload["results"]])
        with self.assertRaisesRegex(ValueError, "count"):
            parse_outage_feed({"count": 2, "as_of": as_of.timestamp(), "outages": [TEST_OUTAGES[0]]})

    def test_production_view_uses_open_feed_and_replaces_atomically(self):
        self.assertEqual(
            "https://router-outage-detection.i2e1.in/get_outage_attribution?status=OPEN",
            DEFAULT_OUTAGE_URL,
        )
        with self.assertRaisesRegex(ValueError, "status=OPEN"):
            fetch_open_outages("https://router-outage-detection.i2e1.in/get_outage_attribution?status=ALL")
        snapshots = [
            (self.now, [TEST_OUTAGES[0]]),
            (self.now + timedelta(minutes=1), [TEST_OUTAGES[1]]),
        ]
        service = Service(self.engine, lambda: snapshots.pop(0))
        service.refresh()
        service.refresh()
        self.assertEqual(["TEST-POWER"], [row["outage_id"] for row in service.map_data()["results"]])
        service.outage_reader = lambda: (_ for _ in ()).throw(RuntimeError("open feed unavailable"))
        with self.assertRaisesRegex(RuntimeError, "unavailable") as error:
            service.refresh()
        service.fail(error.exception)
        self.assertEqual("stale", service.health()["status"])
        self.assertEqual(["TEST-POWER"], [row["outage_id"] for row in service.map_data()["results"]])

    def test_runtime_has_no_v3_dependency_and_uses_ping_proxy(self):
        root = Path(__file__).parent
        source = "\n".join((root / path).read_text() for path in (
            "../sql/outage_devices.sql", "attribution/models.py", "attribution/engine.py",
        )).upper()
        self.assertNotIn("OUTAGE_MEMBER_V3", source)
        self.assertNotIn("OUTAGE_V3", source)
        _, detail = self.evaluate(0)
        self.assertEqual({"LAST_PING_PROXY"}, {group["timing"]["source"] for group in detail["groups"]})

    def test_missing_customer_v2_member_keeps_known_real_state(self):
        inventory = Inventory({"D1": Device("D1", "C1", 28.6, 77.2)})
        engine = AttributionEngine(inventory, lambda ids: {"D1": self.now - timedelta(minutes=11)})
        public, detail = engine.evaluate("O1", ["D1", "MISSING"], 900, self.now)
        self.assertEqual(("O1", "ISP_OLT_CSP_SIDE", .8), (
            public["outage_id"], public["attribution"], public["confidence"]
        ))
        self.assertEqual({"D1", "MISSING"}, {row["device_id"] for row in public["device_pings"]})
        self.assertEqual("CSP_DOWN_SHARE", detail["rule"])
        self.assertEqual("DOWN", detail["devices"][0]["state"])
        self.assertEqual(["MISSING"], detail["noise_device_ids"])

    def test_unknown_states_stay_in_denominator_without_vetoing_rules(self):
        devices = {f"D{index}": Device(f"D{index}", "C1", 28.6, 77.2) for index in range(10)}
        engine = AttributionEngine(Inventory(devices), lambda ids: {
            device_id: self.now - timedelta(minutes=11) if int(device_id[1:]) < 8 else None
            for device_id in ids
        })

        public, detail = engine.evaluate("O1", ["D0"], 900, self.now)

        self.assertEqual(("ISP_OLT_CSP_SIDE", .8), (public["attribution"], public["confidence"]))
        self.assertEqual(.8, detail["parent_evidence"][0]["down_share"])
        self.assertEqual("PREMISE_POWER", AttributionEngine._local_decision("C1", [
            {"csp_id": "C1", "down_share": .8, "unknown": 0, "qualified": True},
            {"csp_id": "C2", "down_share": .7, "unknown": 0, "qualified": True},
            {"csp_id": "C3", "down_share": 0, "unknown": 1, "qualified": False},
        ])[0])

        devices = {f"S{index}": Device(f"S{index}", "C2", 28.6, 77.2) for index in range(20)}
        engine = AttributionEngine(Inventory(devices), lambda ids: {
            device_id: self.now - timedelta(minutes=11 if int(device_id[1:]) < 15 else 1) for device_id in ids
        })
        public, detail = engine.evaluate("O2", ["S0"], 900, self.now)
        self.assertEqual(("ISP_OLT_CSP_SIDE", .8), (public["attribution"], public["confidence"]))
        self.assertEqual(.8, detail["csp_signal"]["policy_score"])

    def test_csp_policy_score_boundaries(self):
        self.assertIsNone(AttributionEngine._csp_signal_confidence(.60))
        self.assertIsNone(AttributionEngine._csp_signal_confidence(.65))
        self.assertEqual(.8, AttributionEngine._csp_signal_confidence(.70))
        self.assertEqual(.8, AttributionEngine._csp_signal_confidence(.75))
        self.assertEqual(.8, AttributionEngine._csp_signal_confidence(.80))

    def test_csp_gate_is_seventy_percent_for_all_connection_sizes(self):
        def evaluate(size, down):
            inventory = Inventory({f"D{index}": Device(f"D{index}", "C1", 28, 77) for index in range(size)})
            engine = AttributionEngine(inventory, lambda ids: {
                device_id: self.now - timedelta(minutes=11 if int(device_id[1:]) < down else 1)
                for device_id in ids
            })
            return engine.evaluate("O1", ["D0"], 900, self.now)

        large_below, large_below_detail = evaluate(100, 69)
        large_match, large_match_detail = evaluate(100, 70)
        small_below, small_below_detail = evaluate(10, 6)
        small_match, small_match_detail = evaluate(10, 7)
        for result in (large_below, small_below):
            self.assertNotEqual("ISP_OLT_CSP_SIDE", result["attribution"])
        for result in (large_match, small_match):
            self.assertEqual(("ISP_OLT_CSP_SIDE", .8), (result["attribution"], result["confidence"]))
        for detail in (large_below_detail, large_match_detail, small_below_detail, small_match_detail):
            self.assertEqual(.7, detail["csp_signal"]["gate_threshold"])

    def test_csp_gate_still_returns_live_peer_states_inside_polygon(self):
        devices = {
            f"T{index}": Device(f"T{index}", "TARGET", 28 + index // 5 * .0002, 77 + index % 5 * .0001)
            for index in range(10)
        }
        devices.update({
            "T_OUT": Device("T_OUT", "TARGET", 29, 78),
            "P_UP": Device("P_UP", "PEER", 28.0001, 77.0002),
            "P_DOWN": Device("P_DOWN", "PEER", 28.0001, 77.00025),
            "P_OUT": Device("P_OUT", "PEER", 29, 78),
        })
        engine = AttributionEngine(Inventory(devices), lambda ids: {
            device_id: self.now - timedelta(minutes=1 if device_id == "P_UP" else 11)
            for device_id in ids
        })

        public, detail = engine.evaluate("O1", [f"T{index}" for index in range(10)], 900, self.now)

        peers = {device["device_id"]: device["state"] for device in detail["devices"] if not device["member"]}
        self.assertEqual("ISP_OLT_CSP_SIDE", public["attribution"])
        self.assertEqual({"P_DOWN": "DOWN", "P_UP": "UP"}, peers)
        self.assertNotIn("T_OUT", {device["device_id"] for device in detail["devices"]})
        self.assertIn("T_OUT", {device["device_id"] for device in public["device_pings"]})
        self.assertTrue(detail["groups"][0]["supported"])

    def test_local_rule_funnel(self):
        provider = lambda csp, share: {"csp_id": csp, "down_share": share, "qualified": True}
        self.assertEqual(("CSP_SPECIFIC_LOCAL", .8, "RULE_2A_LOCAL_CSP_PEERS"),
                         AttributionEngine._local_decision("C1", [provider("C1", .8), provider("C2", .2)]))
        self.assertEqual(("CSP_SPECIFIC_LOCAL", .6, "RULE_2B_LOCAL_CSP_MONOPOLY"),
                         AttributionEngine._local_decision("C1", [provider("C1", .9)]))
        self.assertEqual(("PREMISE_POWER", .7, "RULE_3A_POWER_MULTI_CSP"),
                         AttributionEngine._local_decision("C1", [provider("C1", .8), provider("C2", .7)]))
        self.assertEqual(("PREMISE_POWER", .6, "RULE_3B_POWER_MONOPOLY"),
                         AttributionEngine._local_decision("C1", [provider("C1", .7)]))
        self.assertEqual("UNKNOWN", AttributionEngine._local_decision(
            "C1", [provider("C1", .8), provider("C2", .7), provider("C3", .6)]
        )[0])

    def test_local_polygon_csp_isolation(self):
        devices = {
            f"T{index}": Device(f"T{index}", "TARGET", 28 + index // 5 * .0003, 77 + index % 5 * .0001)
            for index in range(10)
        }
        devices.update({
            "T10": Device("T10", "TARGET", 28.0001, 77.00015),
            "T11": Device("T11", "TARGET", 28.0002, 77.00025),
        })
        for index in range(12):
            devices[f"P{index}"] = Device(f"P{index}", "PEER", 28.00005 + index // 5 * .0001, 77.00005 + index % 5 * .00008)
        for index in range(12, 22):
            devices[f"T{index}"] = Device(f"T{index}", "TARGET", 29 + index * .001, 78)
        devices["T_OUT"] = Device("T_OUT", "TARGET", 30, 79)
        down = {f"T{index}" for index in range(10)} | {"P9", "P10", "T_OUT"}
        engine = AttributionEngine(Inventory(devices), lambda ids: {
            device_id: self.now - timedelta(minutes=11 if device_id in down else 1) for device_id in ids
        })

        public, detail = engine.evaluate("LOCAL-CSP", [*[f"T{index}" for index in range(10)], "T_OUT"], 900, self.now)

        self.assertEqual(("CSP_SPECIFIC_LOCAL", .8), (public["attribution"], public["confidence"]))
        self.assertEqual("RULE_2A_LOCAL_CSP_PEERS", detail["rule"])
        self.assertEqual([22, 11, .8182, .8182], [
            detail["polygon_evidence"]["polygon_devices"], detail["polygon_evidence"]["peer_devices"],
            detail["polygon_evidence"]["target_down_share"], detail["polygon_evidence"]["peer_up_share"],
        ])
        self.assertIn("T_OUT", detail["review_device_ids"])
        self.assertTrue(detail["groups"][0]["supported"])

    def test_missing_status_dependency_stays_unknown(self):
        service = Service(self.engine, lambda: (self.now, [TEST_OUTAGES[0]]), warning="status unavailable")
        service.engine = AttributionEngine(self.engine.inventory, lambda device_ids: {})
        service.refresh()
        payload = service.map_data()
        self.assertEqual("degraded", payload["status"])
        self.assertEqual("UNKNOWN", payload["results"][0]["attribution"])

    def test_dashboard_explains_the_ordered_rule_funnel(self):
        html = (Path(__file__).parent / "static/index.html").read_text()
        self.assertIn("Attribution checklist", html)
        for rule in (
            "CSP-wide outage", "Local CSP with healthy peers", "Local CSP without peers",
            "Multi-CSP premise power", "Monopoly premise power",
            "Same house or gali fibre cut", "Locality fibre cluster",
            "Directional gali fibre cluster", "Unknown",
        ):
            self.assertIn(rule, html)
        for state in ("MATCHED", "NOT MATCHED", "SKIPPED", "SELECTED"):
            self.assertIn(state, html)
        self.assertIn("all-CSP DOWN candidates", html)
        self.assertIn("Timing ${percent(addressEvidence?.strongest_10m_share)} is diagnostic only", html)
        self.assertIn("directionality", html)
        self.assertIn("directional_control_up_share", html)
        self.assertIn("Final decision", html)
        self.assertIn("Winning rule", html)
        self.assertNotIn("${escapeHtml(result.rule)}", html)

    def test_directional_rule_contract_has_implementation_thresholds(self):
        rules = (Path(__file__).parent / "RULES.md").read_text()
        for threshold in (
            "at least 5 DOWN candidates", "50 m through 500 m",
            "directionality ratio at least 3.0", "perpendicular P90 width at most 50 m",
            "at least 70% of 5 or more known non-component controls are UP",
        ):
            self.assertIn(threshold, rules)


if __name__ == "__main__":
    unittest.main()
