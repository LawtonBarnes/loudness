#!/usr/bin/env python3
"""LOUDNESS mic calibration probe -- standalone, NOT part of the live
loudness.py app. Captures raw audio directly via `arecord` (same
invocation as loudness.py's MicCapture) and reports per-band dBFS with
zero shaping applied: no auto-gain, no tilt EQ, no squelch, no
noise_gate. The point is a ground-truth measurement of what each mic
actually sees, for building per-mic calibration profiles -- anything
adaptive here would hide exactly the differences we're trying to
measure. Band math (centers, edges, reference level) is duplicated from
loudness.py's SpectrumAnalyzer, not imported, matching this project's
no-shared-library convention -- keep the two in sync by hand if
BAND_CENTERS_HZ or the reference formula ever changes there.

Usage: calibrate.py [--duration SECONDS] [--device DEV] [--label TEXT]
Prints one JSON object to stdout.
"""
import argparse
import configparser
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100
CHUNK = 1024
BAND_CENTERS_HZ = (125, 250, 500, 750, 1000, 1500, 2000, 4000, 8000)
SETTINGS_PATH = Path(__file__).resolve().parent / "settings.ini"


def default_device():
    parser = configparser.ConfigParser()
    parser.read(SETTINGS_PATH)
    return parser.get("vizmic", "device", fallback="plughw:1,0")


def band_masks_and_reference(low_freq=50.0, high_freq=12000.0):
    window = np.hanning(CHUNK)
    reference = 32767 * np.sum(window) / 2
    freqs = np.fft.rfftfreq(CHUNK, d=1 / SAMPLE_RATE)
    centers = np.array(BAND_CENTERS_HZ, dtype=float)
    inner_edges = np.sqrt(centers[:-1] * centers[1:])
    edges = np.concatenate(([low_freq], inner_edges, [high_freq]))
    masks = [(freqs >= edges[i]) & (freqs < edges[i + 1]) for i in range(len(centers))]
    return window, reference, freqs, centers, masks


def capture_frames(device, duration):
    chunk_bytes = CHUNK * 2  # S16_LE mono
    proc = subprocess.Popen(
        [
            "arecord", "-D", device, "-f", "S16_LE", "-r", str(SAMPLE_RATE),
            "-c", "1", "-t", "raw",
            "--period-size", str(CHUNK), "--buffer-size", str(CHUNK * 4),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    frames = []
    deadline = time.monotonic() + duration
    try:
        while time.monotonic() < deadline:
            data = proc.stdout.read(chunk_bytes)
            if len(data) < chunk_bytes:
                break
            frames.append(np.frombuffer(data, dtype=np.int16).astype(np.float64))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=5.0)
    ap.add_argument("--device", default=None)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    device = args.device or default_device()
    window, reference, freqs, centers, masks = band_masks_and_reference()

    frames = capture_frames(device, args.duration)
    if len(frames) < 5:
        print(json.dumps({"error": f"only captured {len(frames)} frames -- device busy or missing?"}))
        sys.exit(1)

    per_frame_band_db = []
    per_frame_broadband_db = []
    for samples in frames:
        magnitudes = np.abs(np.fft.rfft(samples * window))
        interpolated = np.interp(centers, freqs, magnitudes)
        raw = np.array([
            magnitudes[mask].mean() if mask.any() else interpolated[i]
            for i, mask in enumerate(masks)
        ])
        raw_db = 20 * np.log10(raw / reference + 1e-9)
        per_frame_band_db.append(raw_db)
        rms = np.sqrt(np.mean(samples ** 2))
        per_frame_broadband_db.append(20 * np.log10(rms / 32767 + 1e-9))

    band_arr = np.array(per_frame_band_db)  # (n_frames, n_bands)
    result = {
        "hostname": socket.gethostname(),
        "device": device,
        "label": args.label,
        "duration_s": args.duration,
        "n_frames": len(frames),
        "band_centers_hz": list(BAND_CENTERS_HZ),
        "band_db_median": [round(x, 2) for x in np.median(band_arr, axis=0)],
        "band_db_min": [round(x, 2) for x in np.min(band_arr, axis=0)],
        "band_db_max": [round(x, 2) for x in np.max(band_arr, axis=0)],
        "broadband_db_median": round(float(np.median(per_frame_broadband_db)), 2),
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
