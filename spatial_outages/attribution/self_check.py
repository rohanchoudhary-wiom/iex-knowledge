import csv
import tempfile
from collections import defaultdict
from pathlib import Path

from .csv_io import INPUT_COLUMNS, read_input, write_outputs
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
        }
        devices = {
            (csp, h3_id): [f"{csp}_{h3_id}_{index}" for index in range(10)]
            for csp, h3s in coverage.items()
            for h3_id in h3s
        }
        devices[("GOLF", "L1")] = devices[("GOLF", "L1")][:3]
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
        fail("O_LOCAL", "GOLF", "L1", 3, "2026-01-02 16:00:00")
        fail("O_NOISE", "JULIET", "N1", 2, "2026-01-02 18:00:00")
        fail("O_UNKNOWN", "INDIA", "U1", 1, "2026-01-02 20:00:00")

        with source.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(INPUT_COLUMNS)
            for (csp, h3_id), members in devices.items():
                for device in members:
                    memberships = failures[device] or [("", "")]
                    for outage, time in memberships:
                        writer.writerow((device, csp, h3_id, outage, time))

        fleet, outages = read_input(source)
        write_outputs(output, AttributionEngine(Thresholds()).classify(fleet, outages))
        with (output / "outage_buckets.csv").open(newline="") as handle:
            actual = {row["outage_id"]: row["bucket"] for row in csv.DictReader(handle)}
        with (output / "csp_h3_states.csv").open(newline="") as handle:
            states = {
                (row["csp_id"], row["h3_id"]): row["csp_h3_state"]
                for row in csv.DictReader(handle)
            }
        assert states[("INDIA", "U1")] == "DOWN"
        assert actual == {
            "O_AREA_A": "AREA-SHARED", "O_AREA_B": "AREA-SHARED",
            "O_REG_G": "REGIONAL", "O_REG_D": "REGIONAL",
            "O_ISP": "ISP / OLT", "O_LOCAL": "LOCAL CSP FAULT",
            "O_NOISE": "NOISE", "O_UNKNOWN": "UNKNOWN",
        }, actual
