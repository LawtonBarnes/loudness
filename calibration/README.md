# Mic calibration

`calibrate.py` is a standalone probe (not part of the live app) that
captures raw audio via `arecord` and reports per-band dBFS with **zero
shaping** -- no auto-gain, no tilt EQ, no squelch. It's meant to be
copied to `/opt/loudness/` directly (alongside `loudness.py` and
`settings.ini`) on the puppet being measured, not run from this
`calibration/` subfolder -- it looks for `settings.ini` in its own
directory to find the mic device.

Requires the ALSA capture device to be free (LOUDNESS itself must not
be running on that puppet at the time -- reassign it to another app via
STRINGS's `/assign` first, since `arecord` needs exclusive access to
the hardware device).

Usage: `python3 calibrate.py --duration 6 --label <condition>`, prints
one JSON object to stdout with the 9-band median/min/max dBFS
(`BAND_CENTERS_HZ = (125, 250, 500, 750, 1000, 1500, 2000, 4000, 8000)`,
matching `loudness.py`'s bands exactly) plus overall broadband dBFS.

## How to repeat this test (e.g. after the per-unit fan upgrade)

Run everything from a machine with SSH access to all 4 puppets (`p1`-`p4`
aliases) and curl access to their STRINGS API on port 8420.

1. **Reset to a clean baseline** on each puppet: `amixer -c 1 sset Mic 16`
   (100% ALSA capture gain) + `sudo alsactl store`, and set `gain_bias = 0.0`
   in `/opt/loudness/settings.ini`. Leave `band_offsets` as whatever it
   currently is (or zero it out first if you want a from-scratch profile
   rather than refining the existing one).
2. **Free the mic devices** -- `arecord` needs exclusive access, so
   reassign every puppet away from `loudness` first:
   `curl -X POST http://<puppet-ip>:8420/assign -d '{"app":"bars"}'`
   for each of the 4 IPs.
3. **Deploy the tool**: `scp calibration/calibrate.py <puppet>:/opt/loudness/calibrate.py`
   to each puppet (it reads `settings.ini` from its own directory for the
   device path, so it must sit next to `loudness.py`, not in this
   `calibration/` folder).
4. **For each test condition** (get the room into that state first, then
   capture all 4 puppets in parallel so they're measuring the same
   moment):
   ```
   for h in p1 p2 p3 p4; do
     ssh $h "cd /opt/loudness && python3 calibrate.py --duration 6 --label <condition>" > ${h}_<condition>.json &
   done
   wait
   ```
   Then append to the running dataset: `python3 build_csv.py <condition> <tone_hz_or_empty> p1_x.json p2_x.json p3_x.json p4_x.json`
   (appends to `calibration_data.csv` in the current directory; pass `""`
   for `<tone_hz_or_empty>` on every non-tone condition -- it's a
   required positional argument, not optional, and a missing empty
   string silently eats the first json filename as the tone value).
   The original run used: `quiet`, `hvac`, `music` (comfortable volume),
   `music_max` (full blast), `speech`, and tones at each of `125 250 500
   750 1000 1500 2000 4000 8000` (matching `BAND_CENTERS_HZ` exactly --
   keep using these same 9 frequencies so results compare directly
   against the table below).
5. **Derive the correction profile**: `python3 build_profile.py` (reads
   `calibration_data.csv` in the current directory, prints the raw
   per-band tone readings, the fleet average, and the capped correction
   per puppet -- edit `CAP_DB` at the top if you want a different cap
   than +/-12dB).
6. **Apply it**: replace the `band_offsets = ...` line in each puppet's
   `settings.ini` with that puppet's row from step 5's output (keep the
   existing explanatory comment block above it).
7. **Reassign back to `loudness`** via the same `/assign` endpoint, and
   sanity-check with `arecord`/`amixer` that nothing silently regressed
   (see the ALSA-persistence-across-reboot gotcha elsewhere in this
   project -- worth re-confirming capture gain is still 100% before
   trusting a fresh run's numbers).
8. Commit `calibration_data.csv` (should now have both the old and new
   runs, so drift over time/hardware changes is visible) and the updated
   `band_offsets` lines, and update this README's results section below.

## 2026-09-02 fleet calibration run

Baseline conditions before testing: all 4 puppets' ALSA capture gain
reset to 100% (`amixer -c 1 sset Mic 16`), `gain_bias` reset to 0.0
(clearing all prior AC-noise tuning) -- a clean, uniform starting point
across P1-P4, all with the "USB PnP Sound Device" mic at `plughw:1,0`.

`calibration_data.csv` holds 14 conditions x 4 puppets = 56 readings:
`quiet` (HVAC off, silent room), `hvac` (HVAC running), `music`
(comfortable volume), `music_max` (full blast), `speech` ("check one
two", spoken live), and 9 discrete tones at LOUDNESS's exact band
centers (125Hz through 8kHz), played back at a fixed comfortable
volume through a stereo tone generator.

**Key finding:** below 1kHz, all 4 mics agree within ~2dB on every
condition -- essentially identical, nothing to correct. Above 1kHz they
diverge substantially and non-uniformly (e.g. P2 reads clean at 2kHz
but ~20dB weak specifically at 4kHz) -- real capsule-to-capsule
manufacturing variance in these cheap USB mics, not something a linear
tilt (`treble_boost`/`bass_cut_db`) can correct since the deviation
isn't a straight line across bands.

**Also notable:** the per-mic spread that's large at moderate volume
(HVAC, comfortable-volume music, the tone sweep) shrinks back down to
~1dB at full-blast volume (`music_max`) -- consistent with each mic's
own noise floor/gain-chain quirks dominating when the real signal is
relatively quiet, rather than a fixed level-independent sensitivity
mismatch. This means a correction profile built from one volume level
won't necessarily be exactly right at every other level -- the profile
below was deliberately built from the **tone sweep at a fixed
comfortable volume** (the cleanest single measurement of each band,
and representative of typical/comfortable-listening use, which is when
people actually watch the display).

## Correction profile (`band_offsets` in each puppet's `settings.ini`)

For each of the 9 bands, computed the 4-puppet average tone-sweep
reading as the target, then `correction = average - this_puppet's_reading`,
capped at +/-12dB (chosen so correcting a real weak spot doesn't also
amplify that band's self-noise/hiss too aggressively during quiet
moments -- no band in this run actually needed the cap; the largest
raw correction was P1's -10.4dB at 4kHz).

| Band | P1 | P2 | P3 | P4 |
|------|-----|-----|-----|-----|
| 125 Hz  | +0.9  | -0.2  | -0.0  | -0.7  |
| 250 Hz  | +0.8  | -0.2  | +0.2  | -0.8  |
| 500 Hz  | -0.3  | -0.6  | +0.6  | +0.3  |
| 750 Hz  | -0.2  | -0.5  | +0.6  | +0.1  |
| 1 kHz   | +3.9  | -0.0  | -1.0  | -2.9  |
| 1.5 kHz | +3.4  | +6.4  | -2.3  | -7.5  |
| 2 kHz   | +3.7  | -3.3  | -1.0  | +0.6  |
| 4 kHz   | -10.4 | +10.1 | -4.9  | +5.1  |
| 8 kHz   | -1.3  | +0.9  | -5.8  | +6.1  |

Applied via `loudness.py`'s new `band_offsets` setting (added
2026-09-02, see that file's `SpectrumAnalyzer` docstring/comments),
layered on top of the existing `treble_boost`/`bass_cut_db` tilt rather
than replacing it -- defaults to all-zero, so a puppet with no
calibration data (or a freshly-swapped mic) behaves exactly as before
this feature existed.

**Re-run this whole test if a mic is ever swapped or physically moved**
-- these offsets are tied to that specific mic unit's real hardware
response, not a property of the puppet itself.
