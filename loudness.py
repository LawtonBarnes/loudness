#!/usr/bin/env python3
"""USB-mic audio-reactive spectrum visualizer for Raspberry Pi.

Standalone appliance, deployed alongside (not integrated with) `bars` --
see /opt/bars/bars.py on this same Pi for the sibling project. The
framebuffer-write and console-mode handling below is intentionally a
copy of bars.py's approach, not a shared import, since each app is
deployed as its own self-contained directory under /opt: the same
vc4-fkms-v3d / no-flip() / headless-SDL / evdev-keyboard constraints
documented in bars/README.md ("Why it's built this way") apply here
too, since it's the same Pi 3B+ + Bookworm + composite-video setup.
"""
import configparser
import fcntl
import mmap
import os
import re
import select
import selectors
import signal
import subprocess
import sys
import time
from colorsys import hsv_to_rgb
from pathlib import Path

import evdev
import numpy as np
from evdev import ecodes

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402  (must come after SDL env vars are set)

VERSION = "1.6"

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.ini"
FONT_PATH = BASE_DIR / "VCR_OSD_MONO_1.001.ttf"
SPLASH_PATH = BASE_DIR / "splash.png"  # optional -- see show_splash()
SPLASH_SECONDS = 5.0

FRAME_W, FRAME_H = 720, 480
SAMPLE_RATE = 44100
CHUNK = 1024  # ~23ms/frame at 44.1kHz
TARGET_FPS = 30  # NTSC is 29.97fps -- no point rendering faster than that

# Fixed band layout (2026-08-31), replacing the old log-spaced
# low_freq/high_freq/num_bars auto-computation -- these are the exact
# center frequencies requested, not derived. format_freq_label() below
# already renders these correctly as-is (125, 250, 500, 750, 1K, 1K5,
# 2K, 4K, 8K), no label-formatting change needed.
BAND_CENTERS_HZ = (125, 250, 500, 750, 1000, 1500, 2000, 4000, 8000)

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
ORANGE = (0xFF, 0xA5, 0x00)
TITLE_PREFIX = "SPECTRUM ANALYZER"

POWER_OPTIONS = ["NO", "YES", "RESTART"]
MOUSE_MOVE_THRESHOLD = 12  # cumulative REL_X/REL_Y units before it counts as one direction press

# See menu.py -- Home exits with this so menu.py's launch_app() knows to
# hand off to Health Monitor instead of redrawing its own app list.
EXIT_GOTO_HOME = 42

KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01

ATTACK = 0.6   # blend factor toward a rising target (fast rise)
DECAY = 0.08   # blend factor toward a falling target (slower fall, less flicker)


def load_settings():
    parser = configparser.ConfigParser()
    parser["vizmic"] = {
        "device": "default",
        "sensitivity": "30",
        "low_freq": "50",
        "high_freq": "12000",
        "treble_boost": "12",
        "underscan_scale": "0.8",
        "bar_height_scale": "1.0",
        "bar_gap": "14",
        "led_height": "10",
        "led_gap": "4",
        "min_rungs": "1",
        "noise_gate_db": "-20",
        "bass_cut_db": "16",
        "squelch_db": "10",
        "gain_bias": "0.0",
        "band_offsets": "0,0,0,0,0,0,0,0,0",
    }
    parser.read(SETTINGS_PATH)
    section = parser["vizmic"]
    band_offsets = [float(x) for x in section.get("band_offsets").split(",")]
    if len(band_offsets) != len(BAND_CENTERS_HZ):
        print(
            f"band_offsets has {len(band_offsets)} values, expected {len(BAND_CENTERS_HZ)} "
            "-- ignoring, using 0 for all bands.",
            file=sys.stderr,
        )
        band_offsets = [0.0] * len(BAND_CENTERS_HZ)
    return {
        "device": section.get("device"),
        "gain_bias": section.getfloat("gain_bias"),
        "sensitivity": section.getfloat("sensitivity"),
        "underscan_scale": section.getfloat("underscan_scale"),
        "bar_height_scale": section.getfloat("bar_height_scale"),
        "min_rungs": section.getint("min_rungs"),
        "noise_gate_db": section.getfloat("noise_gate_db"),
        "low_freq": section.getfloat("low_freq"),
        "high_freq": section.getfloat("high_freq"),
        "treble_boost": section.getfloat("treble_boost"),
        "bass_cut_db": section.getfloat("bass_cut_db"),
        "squelch_db": section.getfloat("squelch_db"),
        "band_offsets": band_offsets,
        "bar_gap": section.getint("bar_gap"),
        "led_height": section.getint("led_height"),
        "led_gap": section.getint("led_gap"),
    }


def save_setting(section, key, value):
    # Deliberately doesn't use configparser's own .write() -- it discards
    # every comment in the file on rewrite, and settings.ini's comments
    # are the actual documentation for each option. This is a surgical
    # text edit instead: replace key's existing line in place if it's
    # already there, otherwise append it right after the section header
    # (creating the section too, if the file doesn't have it yet at
    # all). Every other line, comment or not, is left byte-identical.
    # Duplicated from bars.py rather than shared, matching this
    # project's no-shared-library convention.
    text = SETTINGS_PATH.read_text() if SETTINGS_PATH.exists() else ""
    lines = text.splitlines(keepends=True)
    section_header = f"[{section}]"
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    in_section = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == section_header:
            in_section = True
            continue
        if in_section and stripped.startswith("[") and stripped != section_header:
            lines.insert(i, f"{key} = {value}\n")
            SETTINGS_PATH.write_text("".join(lines))
            return
        if in_section and key_re.match(line):
            lines[i] = f"{key} = {value}\n"
            SETTINGS_PATH.write_text("".join(lines))
            return
    if in_section:
        lines.append(f"{key} = {value}\n")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"\n{section_header}\n{key} = {value}\n")
    SETTINGS_PATH.write_text("".join(lines))


class FrameBuffer:
    """Direct writer for /dev/fb0, bypassing DRM page-flips entirely.

    Copied from bars.py -- see that file's module docstring for why
    flip()-based rendering doesn't work on this hardware/driver combo.
    """

    def __init__(self, dev="/dev/fb0"):
        sys_dir = Path("/sys/class/graphics") / Path(dev).name
        self.width, self.height = (int(x) for x in (sys_dir / "virtual_size").read_text().split(","))
        self.bpp = int((sys_dir / "bits_per_pixel").read_text())
        self.stride = int((sys_dir / "stride").read_text())
        self.bypp = self.bpp // 8
        self.row_bytes = self.width * self.bypp
        size = self.stride * self.height
        self.fd = os.open(dev, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        if self.bpp not in (16, 32):
            raise RuntimeError(f"Unsupported framebuffer depth: {self.bpp}bpp")

    def write_surface(self, surface):
        if surface.get_size() != (self.width, self.height):
            surface = pygame.transform.scale(surface, (self.width, self.height))
        arr = pygame.surfarray.pixels3d(surface).transpose(1, 0, 2)  # (H, W, RGB) uint8
        if self.bpp == 16:
            r = arr[:, :, 0].astype(np.uint16) >> 3
            g = arr[:, :, 1].astype(np.uint16) >> 2
            b = arr[:, :, 2].astype(np.uint16) >> 3
            raw = ((r << 11) | (g << 5) | b).astype("<u2").tobytes()
        else:
            alpha = np.zeros((self.height, self.width, 1), dtype=np.uint8)
            raw = np.concatenate([arr[:, :, ::-1], alpha], axis=2).astype(np.uint8).tobytes()

        if self.stride == self.row_bytes:
            self.mm.seek(0)
            self.mm.write(raw)
        else:
            for y in range(self.height):
                self.mm.seek(y * self.stride)
                self.mm.write(raw[y * self.row_bytes : (y + 1) * self.row_bytes])

    def close(self):
        self.mm.close()
        os.close(self.fd)


def show_splash(fb):
    """Blocking splash shown once at launch, before the app's real
    content appears -- copied from bars.py (see that file's version for
    the full rationale), same no-shared-library convention as the rest
    of this file's duplicated FrameBuffer/find_keyboard_devices code.
    Shown at the source image's own native resolution, centered on
    black -- deliberately NOT scaled to fill the frame (user's explicit
    call, 2026-08-18); a larger-than-frame image would just get cropped
    to the center, not shrunk to fit."""
    if not SPLASH_PATH.exists():
        return
    try:
        img = pygame.image.load(str(SPLASH_PATH)).convert()
    except (pygame.error, OSError) as exc:
        print(f"Splash load failed: {exc}", file=sys.stderr)
        return
    canvas = pygame.Surface((FRAME_W, FRAME_H))
    canvas.fill(BLACK)
    img_w, img_h = img.get_size()
    canvas.blit(img, ((FRAME_W - img_w) // 2, (FRAME_H - img_h) // 2))
    fb.write_surface(canvas)
    time.sleep(SPLASH_SECONDS)


def find_keyboard_devices():
    devices = []
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if dev.capabilities().get(ecodes.EV_KEY):
            devices.append(dev)
    if not devices:
        # Puppets in the McBrain fleet run unattended, with no keyboard/
        # remote physically attached -- STRINGS launches this app
        # headless. An empty device list here just means the evdev
        # selector loop below never has anything to read, so the app
        # runs its normal render loop forever with no input reactivity,
        # rather than refusing to start. Production/dev use with a real
        # remote attached is unaffected.
        print("No keyboard input device found -- running headless/unattended.", file=sys.stderr)
    return devices


def format_freq_label(hz):
    """3-character-ish frequency label: 57, 134, 400, 1K, 2K2, 6K3, 16K."""
    if hz < 1000:
        return str(round(hz))
    k = round(hz / 1000, 1)
    whole = int(k)
    tenth = round((k - whole) * 10)
    if tenth == 10:  # rounding carried over, e.g. 1.95 -> 2.0
        whole += 1
        tenth = 0
    return f"{whole}K{tenth}" if tenth else f"{whole}K"


class MicCapture:
    """Raw PCM capture via `arecord`, not a Python ALSA binding.

    Debian Bookworm's python3-alsaaudio package (0.8.4) crashes on
    PCM.read() under Python 3.10+ ("PY_SSIZE_T_CLEAN macro must be
    defined") -- a known upstream bug fixed only in 0.9.0, which isn't
    what apt ships. Shelling out to `arecord` (already present via
    alsa-utils) sidesteps the broken binding entirely.
    """

    def __init__(self, device):
        self.chunk_bytes = CHUNK * 2  # S16_LE mono: 2 bytes/sample
        self.proc = subprocess.Popen(
            [
                "arecord",
                "-D",
                device,
                "-f",
                "S16_LE",
                "-r",
                str(SAMPLE_RATE),
                "-c",
                "1",
                "-t",
                "raw",
                # Without these, ALSA picked a 500ms buffer / 125ms period
                # for this USB mic by default (confirmed via
                # /proc/asound/.../hw_params on a running capture) -- fine
                # for glitch-free playback, but it meant a hand clap took
                # about that long to show up on screen. A period close to
                # our own CHUNK size keeps arecord handing us audio in
                # small enough pieces to stay responsive.
                "--period-size",
                str(CHUNK),
                "--buffer-size",
                str(CHUNK * 4),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def read_chunk(self):
        # BufferedReader.read(n) blocks until n bytes are available or EOF,
        # so this returns a full chunk (or a short one only once arecord
        # has actually died).
        data = self.proc.stdout.read(self.chunk_bytes)
        if len(data) < self.chunk_bytes:
            return None
        # If we fell behind real-time even briefly (a slow frame, startup
        # jitter), stale chunks queue up in the pipe -- draining down to
        # whatever's most recently available keeps that from turning into
        # a persistent, growing lag between real sound and the display.
        while select.select([self.proc.stdout], [], [], 0)[0]:
            newer = self.proc.stdout.read(self.chunk_bytes)
            if len(newer) < self.chunk_bytes:
                break
            data = newer
        return np.frombuffer(data, dtype=np.int16).astype(np.float64)

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


FLOOR_DB = -60  # dynamic range mapped onto the bar height, in dB below full scale
TARGET_HEADROOM_DB = -4  # auto-gain aims for recent peaks to sit this far below clipping
PEAK_DECAY_DB_PER_SEC = 8  # how fast the peak-hold backs off once things get quieter
GAIN_BLEND = 0.03  # per-frame blend toward the target gain (~1s settle time at 23ms/frame)


class SpectrumAnalyzer:
    """Turns raw mic chunks into a smoothed, log-binned bar spectrum.

    Levels are computed on a dB scale (like a VU meter), not a raw
    log1p(magnitude)*gain scaling -- the latter was tried first and
    turned out to saturate almost every band to full height across a
    60dB range of realistic input levels (confirmed with synthetic
    tones from -10dBFS to -70dBFS), because a linear gain on top of a
    logarithm compresses far too aggressively to give usable dynamics.

    Gain is auto-adjusted rather than fixed: a peak-hold envelope (fast
    attack, slow decay -- like a VU meter's peak light) tracks the
    loudest recent input, and gain is continuously retargeted so that
    peak sits at TARGET_HEADROOM_DB, blended in gradually to avoid
    audible/visible gain pumping. This is what keeps loud passages from
    pegging every bar at full height without needing a manual sensitivity
    tweak -- added for running headless, with no keyboard to press
    Up/Down. `max_gain` (from settings.ini, fixed -- no longer
    Up/Down-adjustable, see `gain_bias` below) is a ceiling the
    auto-gain's own computed target never exceeds: during real silence
    the peak-hold decays and the target gain formula alone would climb
    without bound chasing a peak that isn't there, endlessly amplifying
    mic self-noise/room hiss.

    `gain_bias` is what Up/Down actually adjusts now (previously they
    adjusted `max_gain` directly, but since that only ever acts as a
    ceiling, raising or lowering it had no visible effect except near
    total silence -- the auto-computed target sits well under the
    ceiling at any normal input level, so the ceiling was essentially
    never the binding constraint, and Up/Down looked broken). `gain_bias`
    is instead added on top of the auto-gain's target unconditionally,
    so it's always audible/visible regardless of input level, while
    auto-gain still does the actual loudness-tracking work underneath it.

    `noise_gate_db` is a second, distinct floor: the peak-hold envelope
    never reads below it, even from an actual quiet-frame measurement.
    Without this, a "quiet room" isn't actually constant -- mic
    self-noise/room hiss still wobbles slightly from frame to frame, the
    peak-hold's fast attack chases every little upward wobble, gain
    then eases back down and recovers on each one, and the *net effect*
    is bars visibly breathing/pumping even with nothing real happening
    (this was reported after `min_rungs` shipped: still "quite a bit of
    bar movement" with the room and AC both quiet -- the floor on
    displayed rungs doesn't stop the underlying gain from moving). With
    noise_gate_db, gain settles at a fixed value once the input is
    below it and simply stops reacting, instead of continuously
    resettling around a moving target.

    `squelch_db` addresses a separate, per-band version of the same
    problem: even with gain rock-stable, ~60dB is mapped across
    ~20-something rungs, so each rung is only a couple dB wide -- normal
    statistical variance in the FFT magnitude estimate (present in any
    real signal, silence included) is enough to cross several rung
    boundaries on its own, independent of anything the shared gain is
    doing. Bass in particular tends to carry more of this variance
    (real low-frequency rumble, and 1/f-ish mic self-noise). Below
    squelch_db (a dead zone at the bottom of each band's own range,
    applied after gain), a band reads as exactly 0 -- combined with
    min_rungs' floor at render time, that's a rock-stable single
    division rather than jitter across a few. Above it, the band is
    fully reactive, remapped to still use the full display range rather
    than losing headroom to the dead zone.
    """

    def __init__(
        self,
        sensitivity,
        low_freq,
        high_freq,
        treble_boost=0.0,
        noise_gate_db=-40.0,
        bass_cut_db=0.0,
        squelch_db=0.0,
        gain_bias=0.0,
        band_offsets=None,
    ):
        num_bars = len(BAND_CENTERS_HZ)
        self.num_bars = num_bars
        self.max_gain = sensitivity
        self.gain = sensitivity
        # Manual offset, Up/Down-adjustable -- see class docstring.
        # Initializes from whatever was last live-tuned (settings.ini's
        # gain_bias, see handle_keycode/save_setting) rather than always
        # starting back at 0.0.
        self.gain_bias = gain_bias
        self.noise_gate_db = noise_gate_db
        self.peak_hold_db = noise_gate_db
        self.squelch_db = squelch_db
        self.peak_decay_per_frame = PEAK_DECAY_DB_PER_SEC * CHUNK / SAMPLE_RATE
        self.window = np.hanning(CHUNK)
        # Music skews toward bass/mid energy, so a flat dB scale makes the
        # spectrum trail off toward the right -- treble_boost ramps in extra
        # dB by the last band to compensate, bass_cut_db cuts the bottom
        # band to match. A simple linear ramp over band *index* (not exact
        # octave spacing -- BAND_CENTERS_HZ isn't perfectly uniform in log
        # terms, e.g. 750Hz/1500Hz break the octave pattern) is still a
        # fine approximation for this purpose.
        #
        # This same tilt now feeds the auto-gain's peak tracking too (see
        # update()), not just the display -- it wasn't always this way.
        # The auto-gain's gain is one shared value, computed as
        # TARGET_HEADROOM_DB minus whatever it thinks the peak is; earlier,
        # peak tracking used a *separate*, bass_cut-less version of this
        # tilt, on the theory that if bass_cut_db were part of the peak
        # calculation too, cutting the loudest band's tilt would just make
        # gain rise to compensate -- canceling the cut out exactly, with
        # bass's own displayed height not budging (confirmed in testing at
        # the time). That was true, but it meant gain was always sized to
        # protect *uncut* bass from clipping, which -- once bass actually
        # is cut for display -- left every band capped well short of
        # TARGET_HEADROOM_DB even at max volume (reported as "the chart
        # only reaches 2/3 of the screen"). The fix: now that
        # noise_gate_db alone reliably holds gain steady in a quiet room
        # (confirmed separately), the cancellation only ever mattered for
        # *loud* signals anyway -- so tracking the same cut tilt as
        # display is strictly better: whichever band ends up tallest after
        # EQ is the one gain now targets at TARGET_HEADROOM_DB, cut bands
        # included.
        # band_offsets (2026-09-02): per-mic hardware calibration, layered on
        # top of the shared musical tilt rather than replacing it -- cheap
        # USB electret capsules vary unit-to-unit above ~1kHz (measured via a
        # tone sweep across real hardware, see loudness repo's calibration/),
        # not correctable by a straight-line tilt since the deviation isn't
        # monotonic (e.g. one mic reads weak specifically at 4kHz but fine at
        # 2kHz and 8kHz). Defaults to all-zero (load_settings() guarantees a
        # 9-length list), so a puppet with no calibration data behaves
        # identically to before this feature existed.
        self.tilt_db = np.linspace(-bass_cut_db, treble_boost, num_bars) + np.array(band_offsets or [0.0] * num_bars)
        # Reference magnitude: approximately what a full-scale (0dBFS)
        # sine produces after this window, so magnitude/reference is a
        # fraction of full scale and 20*log10(...) is dBFS.
        self.reference = 32767 * np.sum(self.window) / 2
        self.freqs = np.fft.rfftfreq(CHUNK, d=1 / SAMPLE_RATE)
        # Band boundaries: geometric mean of each pair of adjacent fixed
        # centers for the 8 internal edges (standard graphic-EQ convention
        # for turning named centers into ranges), with low_freq/high_freq
        # (still settings.ini-configurable) bounding the outermost edge of
        # the bottom (125) and top (8K) bands.
        centers = np.array(BAND_CENTERS_HZ, dtype=float)
        inner_edges = np.sqrt(centers[:-1] * centers[1:])
        edges = np.concatenate(([low_freq], inner_edges, [high_freq]))
        # Per band: which FFT bins fall in [edges[i], edges[i+1]), with the
        # fixed center frequency itself as an interpolation fallback for
        # when there are none. With a 1024-sample FFT at 44.1kHz, bins are
        # ~43Hz apart, so a narrow band (e.g. 125's ~50-177Hz range) can
        # still contain zero bins -- interpolation patches those. It's a
        # fallback rather than the default for every band, though: sampling
        # a single point at a *wide* high-frequency band's center missed
        # off-center narrowband content entirely in testing (an 8kHz tone
        # landed well outside the FFT window's main lobe when sampled this
        # way, and read as near-silent) -- real bin-averaging is correct
        # wherever there are bins to average.
        self.band_masks = [(self.freqs >= edges[i]) & (self.freqs < edges[i + 1]) for i in range(num_bars)]
        self.band_centers = centers
        self.displayed = np.zeros(num_bars)

    def update(self, samples):
        magnitudes = np.abs(np.fft.rfft(samples * self.window))
        interpolated = np.interp(self.band_centers, self.freqs, magnitudes)
        raw = np.array(
            [
                magnitudes[mask].mean() if mask.any() else interpolated[i]
                for i, mask in enumerate(self.band_masks)
            ]
        )
        raw_db = 20 * np.log10(raw / self.reference + 1e-9)
        # tilt_db (treble_boost, bass_cut_db -- see __init__) is a fixed
        # per-band adjustment, not something gain should have to fight:
        # track the peak *after* tilt, so gain targets whichever band is
        # actually tallest post-EQ, not the pre-EQ raw signal.
        tilted_db = raw_db + self.tilt_db

        # Clamp what this frame can *report* to the peak-hold, not just
        # what the peak-hold decays to -- otherwise a quiet frame that's
        # still above the true (lower) FLOOR_DB, but below the gate,
        # would still yank the peak-hold upward on its own.
        frame_peak = max(tilted_db.max(), self.noise_gate_db)
        self.peak_hold_db = max(frame_peak, self.peak_hold_db - self.peak_decay_per_frame)
        target_gain = np.clip(TARGET_HEADROOM_DB - self.peak_hold_db, 0.0, self.max_gain) + self.gain_bias
        self.gain += (target_gain - self.gain) * GAIN_BLEND

        db = tilted_db + self.gain
        # Dead zone: below squelch_threshold reads as exactly 0 (small FFT
        # variance can't cross rung boundaries there), above it the
        # remaining range up to 0dB is remapped to the full 0..1 display
        # range rather than losing headroom to the dead zone.
        squelch_threshold = FLOOR_DB + self.squelch_db
        targets = np.where(
            db < squelch_threshold,
            0.0,
            np.clip((db - squelch_threshold) / (-FLOOR_DB - self.squelch_db), 0.0, 1.0),
        )
        rising = targets > self.displayed
        blend = np.where(rising, ATTACK, DECAY)
        self.displayed += (targets - self.displayed) * blend
        return self.displayed


class VizApp:
    def __init__(self):
        settings = load_settings()
        self._quit_requested = False
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        pygame.init()
        pygame.display.set_mode((FRAME_W, FRAME_H))  # headless (dummy driver)
        self.clock = pygame.time.Clock()

        self.fb = FrameBuffer()

        self.mic = MicCapture(settings["device"])

        self.analyzer = SpectrumAnalyzer(
            settings["sensitivity"],
            settings["low_freq"],
            settings["high_freq"],
            settings["treble_boost"],
            settings["noise_gate_db"],
            settings["bass_cut_db"],
            settings["squelch_db"],
            settings["gain_bias"],
            settings["band_offsets"],
        )
        self.num_bars = self.analyzer.num_bars
        self.draw_w = int(FRAME_W * settings["underscan_scale"])
        # bar_height_scale shrinks only the bar area vertically, on top of
        # underscan_scale -- offset_y (top/bottom margin, used to size and
        # place the title and legend) is derived from the result, so this
        # grows both margins for breathing room without touching font sizes
        # or the horizontal layout.
        self.draw_h = int(FRAME_H * settings["underscan_scale"] * settings["bar_height_scale"])
        self.offset_x = (FRAME_W - self.draw_w) // 2
        self.offset_y = (FRAME_H - self.draw_h) // 2
        self.bar_gap = settings["bar_gap"]
        self.bar_w = self.draw_w / self.num_bars
        self.led_height = settings["led_height"]
        self.led_pitch = settings["led_height"] + settings["led_gap"]
        # LED-matrix-style display: each column is a fixed grid of lit/unlit
        # segments (rungs), not one continuously-scaled rectangle.
        self.total_rungs = max(1, self.draw_h // self.led_pitch)
        self.min_rungs = settings["min_rungs"]

        self.legend_surfaces, self.legend_y, self.legend_font = self._build_legend()
        self.title_surface, self.title_x, self.title_y = self._build_title(self.legend_font)

        self.osd_font = pygame.font.Font(str(FONT_PATH), 36)
        self.option_font = pygame.font.Font(str(FONT_PATH), 32)
        self.power_dialog_active = False
        self.power_dialog_selection = 0  # index into POWER_OPTIONS, defaults to NO
        self.pending_power_action = None  # None, "shutdown", or "restart"
        self.pending_exit_code = 0  # 0 or EXIT_GOTO_HOME
        self._rel_accum = {"x": 0, "y": 0}

        self.kbd_devices = find_keyboard_devices()
        self.selector = selectors.DefaultSelector()
        for dev in self.kbd_devices:
            self.selector.register(dev, selectors.EVENT_READ)
            # Exclusive grab (EVIOCGRAB): neither bars.py nor anything else
            # reading raw evdev sees these keys while vizmic runs. Without
            # this, both apps read every keystroke independently and both
            # write to the shared /dev/fb0 -- observed as bars.py's SMPTE
            # pattern flashing on screen for one frame on every Up/Down
            # press when it was left running in the background.
            try:
                dev.grab()
            except OSError as exc:
                print(f"Couldn't grab {dev.path} exclusively: {exc}", file=sys.stderr)

        # Same console-graphics-mode dance as bars.py -- keeps the kernel's
        # text-console layer (cursor, leftover text) from drawing over our
        # framebuffer writes. Paired with the KD_TEXT restore in run().
        self.tty_fd = None
        self.console_graphics_mode = False
        try:
            self.tty_fd = os.open("/dev/tty", os.O_RDWR)
            fcntl.ioctl(self.tty_fd, KDSETMODE, KD_GRAPHICS)
            self.console_graphics_mode = True
        except OSError as exc:
            print(f"Console graphics mode not available: {exc}", file=sys.stderr)

        show_splash(self.fb)

    @staticmethod
    def _fit_font(texts, available_w, available_h, max_size=32, min_size=6):
        """Largest VCR OSD font size at which every string in `texts` fits
        within available_w x available_h. Falls back to min_size if even
        that doesn't fit (better a clipped label than a crash)."""
        for size in range(max_size, min_size - 1, -1):
            candidate = pygame.font.Font(str(FONT_PATH), size)
            widest = max(candidate.size(text)[0] for text in texts)
            if widest <= available_w and candidate.get_height() <= available_h:
                return candidate
        return pygame.font.Font(str(FONT_PATH), min_size)

    def _build_legend(self):
        """Pre-render one frequency-label surface per band, sized to fit
        within a bar column -- static per band, so this runs once at
        startup rather than re-rendering text every frame.
        """
        labels = [format_freq_label(hz) for hz in self.analyzer.band_centers]
        available_w = self.bar_w - self.bar_gap
        available_h = self.offset_y - 4  # small padding before the frame edge
        font = self._fit_font(labels, available_w, available_h)

        surfaces = []
        for i, label in enumerate(labels):
            hue = i / self.num_bars
            r, g, b = (int(c * 255) for c in hsv_to_rgb(hue, 0.85, 1.0))
            surf = font.render(label, True, (r, g, b))
            x = self.offset_x + int(i * self.bar_w) + (int(self.bar_w - self.bar_gap) - surf.get_width()) // 2
            surfaces.append((surf, x))
        legend_y = self.offset_y + self.draw_h + 4
        return surfaces, legend_y, font

    def _build_title(self, legend_font):
        """Pre-render the title bar, centered in the top margin above the
        bars, at the same point size as the legend labels. Falls back to
        an independently fit size only if the legend's size doesn't
        actually fit up there (narrow underscan margins, tiny num_bars
        making for a big legend font, etc.). Includes the live gain_bias
        readout (2026-08-31), so this isn't just called once at startup
        like the legend -- handle_keycode's Up/Down handlers call this
        again after changing gain_bias, to keep the header in sync."""
        title_text = f"{TITLE_PREFIX} - GAIN {self.analyzer.gain_bias:+.0f} DB"
        available_w = self.draw_w
        available_h = self.offset_y - 8
        font = legend_font
        if font.size(title_text)[0] > available_w or font.get_height() > available_h:
            font = self._fit_font([title_text], available_w, available_h)
        surf = font.render(title_text, True, ORANGE)
        x = self.offset_x + (self.draw_w - surf.get_width()) // 2
        y = (self.offset_y - surf.get_height()) // 2
        return surf, x, y

    def _handle_signal(self, signum, frame):
        self._quit_requested = True

    def handle_keycode(self, code):
        # Home is distinct from Back/Menu/Q/Esc -- see menu.py's
        # launch_app() and bars.py's handle_keycode for the full rationale
        # (Home skips the App Menu and jumps straight to Health Monitor).
        if self.power_dialog_active:
            return self.handle_power_dialog_keycode(code)
        if code in (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME):
            return "quit_home"
        elif code in (ecodes.KEY_Q, ecodes.KEY_ESC, ecodes.KEY_BACK, ecodes.KEY_COMPOSE):
            return "quit"
        elif code == ecodes.KEY_UP:
            self.analyzer.gain_bias += 3.0
            print(f"gain_bias: {self.analyzer.gain_bias:+.0f}dB", file=sys.stderr)
            save_setting("vizmic", "gain_bias", self.analyzer.gain_bias)
            self.title_surface, self.title_x, self.title_y = self._build_title(self.legend_font)
        elif code == ecodes.KEY_DOWN:
            self.analyzer.gain_bias -= 3.0
            print(f"gain_bias: {self.analyzer.gain_bias:+.0f}dB", file=sys.stderr)
            save_setting("vizmic", "gain_bias", self.analyzer.gain_bias)
            self.title_surface, self.title_x, self.title_y = self._build_title(self.legend_font)
        elif code == ecodes.KEY_POWER:
            self.power_dialog_active = True
            self.power_dialog_selection = 0

    def handle_power_dialog_keycode(self, code):
        if code in (ecodes.KEY_LEFT, ecodes.KEY_UP):
            self.power_dialog_selection = (self.power_dialog_selection - 1) % len(POWER_OPTIONS)
        elif code in (ecodes.KEY_RIGHT, ecodes.KEY_DOWN):
            self.power_dialog_selection = (self.power_dialog_selection + 1) % len(POWER_OPTIONS)
        elif code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT, ecodes.BTN_MOUSE):
            choice = POWER_OPTIONS[self.power_dialog_selection]
            if choice == "NO":
                self.power_dialog_active = False
            else:
                return "shutdown" if choice == "YES" else "restart"
        elif code in (ecodes.KEY_ESC, ecodes.KEY_BACK, ecodes.KEY_POWER):
            self.power_dialog_active = False

    def handle_rel_event(self, code, value):
        """Translates the remote's air-mouse-mode movement into the same
        discrete direction presses its keyboard-mode D-pad sends, so
        navigation works no matter which mode the remote happens to be in."""
        if code == ecodes.REL_X:
            axis = "x"
        elif code == ecodes.REL_Y:
            axis = "y"
        else:
            return None
        self._rel_accum[axis] += value
        accum = self._rel_accum[axis]
        if abs(accum) < MOUSE_MOVE_THRESHOLD:
            return None
        self._rel_accum[axis] = 0
        if axis == "x":
            synthetic = ecodes.KEY_RIGHT if accum > 0 else ecodes.KEY_LEFT
        else:
            synthetic = ecodes.KEY_DOWN if accum > 0 else ecodes.KEY_UP
        return self.handle_keycode(synthetic)

    def draw_power_dialog(self, canvas):
        lines = ["ARE YOU SURE YOU WANT", "TO SHUT DOWN?"]
        line_surfs = [self.osd_font.render(line, True, WHITE) for line in lines]
        option_surfs = [
            self.option_font.render(opt, True, BLACK if i == self.power_dialog_selection else ORANGE)
            for i, opt in enumerate(POWER_OPTIONS)
        ]

        pad_x, pad_y, gap = 40, 24, 40
        options_w = sum(s.get_width() for s in option_surfs) + gap * (len(option_surfs) - 1)
        content_w = max(max(s.get_width() for s in line_surfs), options_w)
        content_h = (sum(s.get_height() for s in line_surfs) + 10 * (len(line_surfs) - 1)
                     + 30 + option_surfs[0].get_height())

        box = pygame.Surface((content_w + pad_x * 2, content_h + pad_y * 2))
        box.fill(BLACK)
        pygame.draw.rect(box, ORANGE, box.get_rect(), 3)

        y = pad_y
        for surf in line_surfs:
            box.blit(surf, ((box.get_width() - surf.get_width()) // 2, y))
            y += surf.get_height() + 10
        y += 20

        x = (box.get_width() - options_w) // 2
        for i, surf in enumerate(option_surfs):
            if i == self.power_dialog_selection:
                highlight = pygame.Rect(x - 10, y - 6, surf.get_width() + 20, surf.get_height() + 12)
                pygame.draw.rect(box, ORANGE, highlight)
            box.blit(surf, (x, y))
            x += surf.get_width() + gap

        canvas.blit(box, ((FRAME_W - box.get_width()) // 2, (FRAME_H - box.get_height()) // 2))

    def render(self, levels):
        canvas = pygame.Surface((FRAME_W, FRAME_H))
        canvas.fill(BLACK)
        canvas.blit(self.title_surface, (self.title_x, self.title_y))
        for i, level in enumerate(levels):
            lit_rungs = max(self.min_rungs, int(level * self.total_rungs))
            if lit_rungs <= 0:
                continue
            hue = i / self.num_bars  # rainbow sweep, low freq -> red, high -> violet
            r, g, b = (int(c * 255) for c in hsv_to_rgb(hue, 0.85, 1.0))
            x = self.offset_x + int(i * self.bar_w)
            for rung in range(lit_rungs):
                y = self.offset_y + self.draw_h - rung * self.led_pitch - self.led_height
                pygame.draw.rect(canvas, (r, g, b), (x, y, int(self.bar_w - self.bar_gap), self.led_height))
        for surf, x in self.legend_surfaces:
            canvas.blit(surf, (x, self.legend_y))
        if self.power_dialog_active:
            self.draw_power_dialog(canvas)
        self.fb.write_surface(canvas)

    def run(self):
        try:
            running = True
            while running and not self._quit_requested:
                for key, _ in self.selector.select(timeout=0):
                    device = key.fileobj
                    for event in device.read():
                        if event.type == ecodes.EV_KEY and event.value == 1:  # 1 == key down
                            result = self.handle_keycode(event.code)
                        elif event.type == ecodes.EV_REL:
                            result = self.handle_rel_event(event.code, event.value)
                        else:
                            continue
                        if result == "quit":
                            running = False
                        elif result == "quit_home":
                            running = False
                            self.pending_exit_code = EXIT_GOTO_HOME
                        elif result in ("shutdown", "restart"):
                            self.pending_power_action = result
                            running = False
                if not running:
                    break

                samples = self.mic.read_chunk()
                if samples is None:
                    sys.exit("Lost the mic capture stream (arecord exited) -- check `device` in settings.ini")
                levels = self.analyzer.update(samples)
                self.render(levels)
                self.clock.tick(TARGET_FPS)
        finally:
            for dev in self.kbd_devices:
                try:
                    dev.ungrab()
                except OSError:
                    pass
            self.mic.close()
            self.fb.close()
            if self.console_graphics_mode:
                fcntl.ioctl(self.tty_fd, KDSETMODE, KD_TEXT)
                os.write(self.tty_fd, b"\033[2J\033[H")  # clear screen, home cursor
            if self.tty_fd is not None:
                os.close(self.tty_fd)
            pygame.quit()

        if self.pending_power_action == "shutdown":
            subprocess.run(["sudo", "shutdown", "-h", "now"])
        elif self.pending_power_action == "restart":
            subprocess.run(["sudo", "shutdown", "-r", "now"])

        sys.exit(self.pending_exit_code)


def main():
    VizApp().run()


if __name__ == "__main__":
    main()
