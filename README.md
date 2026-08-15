# LOUDNESS (`loudness`)

A Raspberry Pi appliance that renders a real-time LED-matrix-style audio
spectrum visualizer over composite video, driven by a USB microphone.

Built for a Raspberry Pi 3B+ running Raspberry Pi OS Bookworm, output via
the analog composite video jack to a CRT. Shares its console/framebuffer
architecture with [BARS](https://github.com/LawtonBarnes/bars) -- headless
pygame, direct `/dev/fb0` writes, raw `evdev` keyboard input.

## Keyboard / remote controls

| Key | Action |
|---|---|
| `↑` / `↓` | Adjust gain bias up/down |
| `Home` | Quit to the app menu |
| `Q` / `Esc` / `Back` | Quit to the app menu |
| `Power` | Shutdown/restart confirm dialog |

## Configuration

All visual and audio-processing parameters (mic device, bar count,
underscan, LED segment sizing, auto-gain, frequency range, EQ tilt, noise
gating) are tunable in `settings.ini` without a restart -- see the
comments in that file for what each one does.

## Requirements

- A USB microphone, addressed via ALSA (`device = plughw:X,Y` in
  `settings.ini` -- run `arecord -l` on the Pi to find the right card).
- `pygame`, `evdev`, `numpy` (system packages, no venv).
