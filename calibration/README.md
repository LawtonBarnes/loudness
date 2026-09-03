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
