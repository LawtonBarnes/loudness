import json
import csv
import sys
from pathlib import Path

SCRATCH = Path(__file__).parent
CSV_PATH = SCRATCH / "calibration_data.csv"
COLUMNS = [
    "condition", "tone_hz", "puppet", "hostname", "device",
    "band_125_db", "band_250_db", "band_500_db", "band_750_db",
    "band_1000_db", "band_1500_db", "band_2000_db", "band_4000_db", "band_8000_db",
    "broadband_db", "n_frames", "duration_s",
]

PUPPET_BY_HOST = {"Puppet-1": "P1", "Puppet-2": "P2", "Puppet-3": "P3", "Puppet-4": "P4"}


def add_rows(json_files, condition, tone_hz=""):
    is_new = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(COLUMNS)
        for jf in json_files:
            d = json.loads(Path(jf).read_text())
            row = [
                condition, tone_hz, PUPPET_BY_HOST.get(d["hostname"], d["hostname"]),
                d["hostname"], d["device"],
                *d["band_db_median"],
                d["broadband_db_median"], d["n_frames"], d["duration_s"],
            ]
            writer.writerow(row)
    print(f"Added {len(json_files)} rows for condition={condition!r} tone_hz={tone_hz!r}")


if __name__ == "__main__":
    condition = sys.argv[1]
    tone_hz = sys.argv[2] if len(sys.argv) > 2 else ""
    json_files = sys.argv[3:]
    add_rows(json_files, condition, tone_hz)
