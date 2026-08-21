import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import Service, parse_outage_feed, validate_request
from attribution import AttributionEngine, Device, Inventory, StatusClient, _normalize_status_response, device_state
from attribution.spatial import anchored_time_groups, radius_core, radius_profile, strongest_window
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

        self.engine = AttributionEngine(inventory, statuses)

    def evaluate(self, index):
        outage = TEST_OUTAGES[index]
        return self.engine.evaluate(**{
            "outage_id": outage["outage_id"],
            "member_ids": outage["devices"],
            "ongoing_time": outage["ongoing_time"],
            "evaluated_at": self.now,
        })

    def test_all_attributions_and_status_semantics(self):
        fibre, fibre_detail = self.evaluate(0)
        power, power_detail = self.evaluate(1)
        isp, _ = self.evaluate(2)
        unknown, _ = self.evaluate(3)
        self.assertEqual("FIBRE_CUT", fibre["attribution"])
        self.assertEqual("PREMISE_POWER", power["attribution"])
        self.assertEqual("ISP_OLT_CSP_SIDE", isp["attribution"])
        self.assertEqual({"outage_id": "TEST-UNKNOWN", "attribution": "FIBRE_CUT", "confidence": "LOW"}, unknown)
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

    def test_anchored_time_windows_and_radius_tail(self):
        devices = [Device(f"D{index}", "C1", 28, 77 + index * .0001) for index in range(4)]
        times = {
            "D0": self.now,
            "D1": self.now + timedelta(minutes=25),
            "D2": self.now + timedelta(minutes=30),
            "D3": self.now + timedelta(minutes=50),
        }
        self.assertEqual([["D0", "D1", "D2"], ["D3"]], [
            [device.device_id for device in group] for group in anchored_time_groups(devices, times)
        ])
        self.assertEqual(2, strongest_window(list(times.values())))
        spatial = [Device(f"S{index}", "C1", 28, 77 + index * .0001) for index in range(10)]
        center, radii = radius_profile(spatial)
        core, tails = radius_core(spatial, center)
        self.assertLessEqual(radii["r70"], radii["r80"])
        self.assertLessEqual(radii["r80"], radii["r90"])
        self.assertLessEqual(radii["r90"], radii["r100"])
        self.assertEqual(9, len(core))
        self.assertEqual(1, len(tails))

    def test_v3_time_provenance_and_recovered_members(self):
        inventory, ages = test_data()
        v3_time = self.now - timedelta(minutes=12)
        inventory.outage_failure_times[("TEST-FIBRE", "FA00")] = v3_time
        engine = AttributionEngine(inventory, lambda ids: {
            device_id: self.now - timedelta(seconds=ages[device_id]) if ages[device_id] is not None else None
            for device_id in ids
        })
        _, detail = engine.evaluate("TEST-FIBRE", TEST_OUTAGES[0]["devices"], 1800, self.now)
        self.assertEqual("MIXED_V3_AND_LAST_PING_PROXY", detail["groups"][0]["timing"]["source"])
        self.assertIn("FA10", detail["recovered_member_ids"])

    def test_customer_v2_csv_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "customers.csv"
            path.write_text("device_id,csp_id,latitude,longitude,is_active,customer_address\nD1,C1,28,77,true,Delhi\nD1,C1,28,77,true,Delhi\nD2,C1,29,78,false,Noida\n")
            inventory = Inventory.from_csv(path)
            self.assertEqual(["D1"], sorted(inventory.devices))
            self.assertEqual("Delhi", inventory.devices["D1"].address)

    def test_customer_v2_loads_v3_failure_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "customers.csv"
            path.write_text("device_id,csp_id,latitude,longitude,outage_id,member_first_fail_at_ist\nD1,C1,28,77,12.0,2026-08-19 17:30:00\n")
            inventory = Inventory.from_csv(path)
            self.assertEqual(self.now, inventory.outage_failure_times[("12", "D1")])

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

    def test_missing_customer_v2_member_keeps_known_real_state(self):
        inventory = Inventory({"D1": Device("D1", "C1", 28.6, 77.2)})
        engine = AttributionEngine(inventory, lambda ids: {"D1": self.now - timedelta(minutes=11)})
        public, detail = engine.evaluate("O1", ["D1", "MISSING"], 900, self.now)
        self.assertEqual({"outage_id": "O1", "attribution": "ISP_OLT_CSP_SIDE", "confidence": .9}, public)
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

        self.assertEqual({"outage_id": "O1", "attribution": "ISP_OLT_CSP_SIDE", "confidence": .9}, public)
        self.assertEqual(.8, detail["parent_evidence"][0]["down_share"])
        self.assertEqual("PREMISE_POWER", AttributionEngine._local_cause("C1", [
            {"csp_id": "C1", "down_share": .8, "unknown": 0, "qualified": True},
            {"csp_id": "C2", "down_share": .7, "unknown": 0, "qualified": True},
            {"csp_id": "C3", "down_share": 0, "unknown": 1, "qualified": False},
        ]))

        devices = {f"S{index}": Device(f"S{index}", "C2", 28.6, 77.2) for index in range(20)}
        engine = AttributionEngine(Inventory(devices), lambda ids: {
            device_id: self.now - timedelta(minutes=11 if int(device_id[1:]) < 15 else 1) for device_id in ids
        })
        public, detail = engine.evaluate("O2", ["S0"], 900, self.now)
        self.assertEqual({"outage_id": "O2", "attribution": "ISP_OLT_CSP_SIDE", "confidence": .8}, public)
        self.assertEqual(.8, detail["csp_signal"]["policy_score"])

    def test_csp_policy_score_boundaries(self):
        self.assertEqual(.5, AttributionEngine._csp_signal_confidence(.60))
        self.assertEqual(.6, AttributionEngine._csp_signal_confidence(.65))
        self.assertEqual(.75, AttributionEngine._csp_signal_confidence(.70))
        self.assertEqual(.8, AttributionEngine._csp_signal_confidence(.75))
        self.assertEqual(.9, AttributionEngine._csp_signal_confidence(.80))

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
            self.assertEqual({"outage_id": "O1", "attribution": "ISP_OLT_CSP_SIDE", "confidence": .75}, result)
        for detail in (large_below_detail, large_match_detail, small_below_detail, small_match_detail):
            self.assertEqual(.7, detail["csp_signal"]["gate_threshold"])

    def test_csp_gate_still_returns_live_peer_states_inside_polygon(self):
        devices = {
            f"T{index}": Device(f"T{index}", "TARGET", 28 + index // 5 * .0002, 77 + index % 5 * .0001)
            for index in range(10)
        }
        devices.update({
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
        self.assertTrue(detail["groups"][0]["supported"])

    def test_low_confidence_fallback(self):
        self.assertEqual(("FIBRE_CUT", "LOW_CONFIDENCE_SINGLE_CSP_SIGNAL"), AttributionEngine._low_confidence_cause("C1", []))
        self.assertEqual(("PREMISE_POWER", "LOW_CONFIDENCE_MULTI_CSP_SIGNAL"), AttributionEngine._low_confidence_cause("C1", [{"providers": [
            {"csp_id": "C1", "qualified": True, "down": 2},
            {"csp_id": "C2", "qualified": True, "down": 2},
        ]}]))

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

        self.assertEqual({"outage_id": "LOCAL-CSP", "attribution": "CSP_SPECIFIC_LOCAL", "confidence": .8}, public)
        self.assertEqual("LOCAL_CSP_ISOLATION", detail["rule"])
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


if __name__ == "__main__":
    unittest.main()
