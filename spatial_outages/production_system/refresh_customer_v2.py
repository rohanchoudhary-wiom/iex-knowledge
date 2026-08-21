import argparse
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the real Customer V2 attribution snapshot")
    parser.add_argument("--output", type=Path, default=ROOT.parent / "data/input/outage_devices.csv")
    parser.add_argument("--booking-truth", type=Path, default=ROOT.parents[2] / "booking_truth")
    args = parser.parse_args()
    load_dotenv(ROOT.parents[1] / ".env")
    sys.path.insert(0, str(args.booking_truth))
    from data_lib.data_fetch.wiom_data import WiomData

    frame = WiomData("snowflake").query((ROOT.parent / "sql/outage_devices.sql").read_text())
    frame.columns = [str(column).lower() for column in frame.columns]
    required = {"device_id", "csp_id", "latitude", "longitude", "is_active"}
    if missing := required - set(frame.columns):
        raise RuntimeError(f"Customer V2 query is missing columns: {', '.join(sorted(missing))}")
    if frame.empty:
        raise RuntimeError("Customer V2 query returned no rows")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".customer-v2.", suffix=".csv", dir=args.output.parent)
    os.close(descriptor)
    try:
        frame.to_csv(temporary, index=False)
        os.replace(temporary, args.output)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    print(f"refreshed {len(frame):,} Customer V2 rows at {args.output}")


if __name__ == "__main__":
    main()
