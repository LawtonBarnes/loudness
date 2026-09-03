import csv
from pathlib import Path

CSV_PATH = Path(__file__).parent / "calibration_data.csv"
BANDS = [125, 250, 500, 750, 1000, 1500, 2000, 4000, 8000]
BAND_COL = {hz: f"band_{hz}_db" for hz in BANDS}
PUPPETS = ["P1", "P2", "P3", "P4"]
CAP_DB = 12.0

# matrix[puppet][band_hz] = that puppet's reading in the band matching the tone played at that freq
matrix = {p: {} for p in PUPPETS}
with open(CSV_PATH, newline="") as f:
    for row in csv.DictReader(f):
        if row["condition"] != "tone":
            continue
        hz = int(row["tone_hz"])
        matrix[row["puppet"]][hz] = float(row[BAND_COL[hz]])

print("Raw per-band tone readings (dB):")
print(f"{'Hz':>6}", *[f"{p:>8}" for p in PUPPETS], f"{'avg':>8}")
reference = {}
for hz in BANDS:
    vals = [matrix[p][hz] for p in PUPPETS]
    avg = sum(vals) / len(vals)
    reference[hz] = avg
    print(f"{hz:>6}", *[f"{v:>8.2f}" for v in vals], f"{avg:>8.2f}")

print("\nRaw correction needed (reference - puppet), before capping:")
print(f"{'Hz':>6}", *[f"{p:>8}" for p in PUPPETS])
raw_corrections = {p: {} for p in PUPPETS}
for hz in BANDS:
    row_vals = []
    for p in PUPPETS:
        corr = reference[hz] - matrix[p][hz]
        raw_corrections[p][hz] = corr
        row_vals.append(corr)
    print(f"{hz:>6}", *[f"{v:>+8.2f}" for v in row_vals])

print(f"\nCapped correction (+/-{CAP_DB}dB) -- final band_offsets:")
print(f"{'Hz':>6}", *[f"{p:>8}" for p in PUPPETS])
capped = {p: {} for p in PUPPETS}
for hz in BANDS:
    row_vals = []
    for p in PUPPETS:
        c = max(-CAP_DB, min(CAP_DB, raw_corrections[p][hz]))
        capped[p][hz] = c
        row_vals.append(c)
    print(f"{hz:>6}", *[f"{v:>+8.2f}" for v in row_vals])

print("\nsettings.ini lines per puppet:")
for p in PUPPETS:
    values = ", ".join(f"{capped[p][hz]:+.1f}" for hz in BANDS)
    print(f"{p}: band_offsets = {values}")
