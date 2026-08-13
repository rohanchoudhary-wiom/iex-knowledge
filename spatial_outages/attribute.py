import argparse
from pathlib import Path

from attribution.csv_io import read_input, write_outputs
from attribution.domain.thresholds import Thresholds
from attribution.engine import AttributionEngine
from attribution.self_check import run_self_check


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Assign spatial-outage buckets from one CSV")
    parser.add_argument("--input", type=Path, default=ROOT / "data/input/outage_devices.csv")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/output")
    parser.add_argument("--min-affected-share", type=float, default=0.7)
    parser.add_argument("--overlap-window-minutes", type=int, default=30)
    parser.add_argument("--almost-all-h3-share", type=float, default=0.8)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    if args.self_check:
        run_self_check()
        print("self-check passed")
        return 0
    try:
        thresholds = Thresholds(
            args.min_affected_share,
            args.overlap_window_minutes,
            args.almost_all_h3_share,
        )
    except ValueError as exc:
        parser.error(str(exc))
    fleet, outages = read_input(args.input)
    write_outputs(args.output_dir, AttributionEngine(thresholds).classify(fleet, outages))
    device_count = sum(len(devices) for devices in fleet.values())
    print(f"classified {len(outages):,} outages from {device_count:,} active devices")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
