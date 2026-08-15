import csv
import tempfile
from collections import defaultdict
from pathlib import Path

from .csv_io import INPUT_COLUMNS, OPTIONAL_INPUT_COLUMNS, read_input, write_outputs
from .domain.thresholds import Thresholds
from .engine import AttributionEngine


def run_self_check() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, output = root / "outage_devices.csv", root / "output"
        coverage = {
            "ALPHA": ("A1", "A2"), "BETA": ("A1", "A2"),
            "GAMMA": ("R1", "R2"), "DELTA": ("R1", "R2"),
            "ECHO": ("I1", "I2"), "FOXTROT": ("I1", "I2"),
            "GOLF": ("L1", "L2"), "HOTEL": ("L1", "L2"),
            "JULIET": ("N1", "N2"), "INDIA": ("U1",),
            "KILO": ("P1", "P2"), "LIMA": ("P1", "P2"),
        }
        devices = {
            (csp, h3_id): [f"{csp}_{h3_id}_{index}" for index in range(10)]
            for csp, h3s in coverage.items()
            for h3_id in h3s
        }
        devices[("GOLF", "L1")] = devices[("GOLF", "L1")][:4]
        devices[("INDIA", "U1")] = devices[("INDIA", "U1")][:1]
        failures: dict[str, list[tuple[str, str]]] = defaultdict(list)

        def fail(outage: str, csp: str, h3_id: str, count: int, time: str) -> None:
            for device in devices[(csp, h3_id)][:count]:
                failures[device].append((outage, time))

        fail("O_AREA_A", "ALPHA", "A1", 8, "2026-01-02 10:00:00")
        fail("O_AREA_B", "BETA", "A1", 8, "2026-01-02 10:10:00")
        for csp, outage in (("GAMMA", "O_REG_G"), ("DELTA", "O_REG_D")):
            fail(outage, csp, "R1", 8, "2026-01-02 12:00:00")
            fail(outage, csp, "R2", 8, "2026-01-02 12:00:00")
        fail("O_ISP", "ECHO", "I1", 8, "2026-01-02 14:00:00")
        fail("O_ISP", "ECHO", "I2", 8, "2026-01-02 14:00:00")
        fail("O_LOCAL", "GOLF", "L1", 4, "2026-01-02 16:00:00")
        fail("O_NOISE", "JULIET", "N1", 2, "2026-01-02 18:00:00")
        fail("O_UNKNOWN", "INDIA", "U1", 1, "2026-01-02 20:00:00")
        fail("O_POLYGON", "KILO", "P1", 3, "2026-01-02 22:00:00")

        with source.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(INPUT_COLUMNS + OPTIONAL_INPUT_COLUMNS)
            cell_number = 0
            origins: dict[tuple[str, str], tuple[float, float]] = {}
            for (csp, h3_id), members in devices.items():
                cell_number += 1
                for index, device in enumerate(members):
                    latitude = 28 + cell_number * 0.02 + (0.01 if index % 3 == 1 else 0)
                    longitude = 77 + cell_number * 0.02 + (0.01 if index % 3 == 2 else 0)
                    origins.setdefault((csp, h3_id), (latitude, longitude))
                    if csp == "LIMA" and h3_id == "P1" and index == 0:
                        latitude, longitude = origins[("KILO", "P1")]
                    row_h3 = h3_id
                    if device == "GOLF_L1_2":
                        longitude += 0.5  # Distant membership still produces one polygon.
                    if device == "GOLF_L1_3":
                        latitude = longitude = row_h3 = ""  # Frozen but not located.
                    memberships = failures[device] or [("", "")]
                    for outage, time in memberships:
                        writer.writerow(
                            (device, csp, latitude, longitude, row_h3, outage, time, "true", time)
                        )

        fleet, outages = read_input(source)
        rows = AttributionEngine(Thresholds()).classify(fleet, outages)
        write_outputs(output, rows)
        write_outputs(output, AttributionEngine(Thresholds()).classify(fleet, outages))

        with (output / "outage_attributions.csv").open(newline="") as handle:
            actual = {row["outage_id"]: row for row in csv.DictReader(handle)}
        with (output / "csp_h3_states.csv").open(newline="") as handle:
            states = {
                (row["csp_id"], row["h3_id"]): row["csp_h3_state"]
                for row in csv.DictReader(handle)
            }
        with (output / "outage_evidence.csv").open(newline="") as handle:
            evidence = {row["outage_id"]: row for row in csv.DictReader(handle)}

        assert states[("INDIA", "U1")] == "DOWN"
        assert set(actual) == {outage.outage_id for outage in outages}
        assert actual["O_ISP"]["root_cause"] == "CSP_SIDE"
        assert evidence["O_ISP"]["rule_matched"] == "R0_CSP_DOWN_SHARE"
        assert evidence["O_ISP"]["csp_down_share"] == "0.8"
        assert evidence["O_POLYGON"]["rule_matched"] == "R0_POLYGON_PEER_UP"
        assert actual["O_POLYGON"]["root_cause"] == "CSP_SIDE"
        assert {row["root_cause"] for row in actual.values()} <= {
            "CSP_SIDE", "ACCESS_FIBRE", "PREMISE_POWER", "UNKNOWN"
        }
        assert actual["O_REG_G"]["spatial_extent"] == "REGIONAL"
        assert actual["O_LOCAL"]["unhealthy_device_count"] == "4"
        assert actual["O_LOCAL"]["located_unhealthy_device_count"] == "3"
        assert actual["O_LOCAL"]["geometry"].startswith("POLYGON")
        assert actual["O_UNKNOWN"]["geometry"] == ""
        assert actual["O_UNKNOWN"]["root_cause"] == "UNKNOWN"
        assert actual["O_AREA_A"]["outage_id"] != actual["O_AREA_B"]["outage_id"]
        assert {row["revision"] for row in actual.values()} == {"1"}

        changed = AttributionEngine(Thresholds()).classify(fleet, outages)
        changed[3][0]["event_pattern"] = "RECURRING"
        changed_id = changed[3][0]["outage_id"]
        write_outputs(output, changed)
        with (output / "outage_attributions.csv").open(newline="") as handle:
            revised = {row["outage_id"]: row for row in csv.DictReader(handle)}
        assert revised[changed_id]["revision"] == "2"
