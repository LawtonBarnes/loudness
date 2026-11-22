"""Instrument-style LOUDNESS looks (2026-11-22): SPECTRUM PRO, SCOPE,
LISSAJOUS and RIPPLE, then batch 2: SPIROGRAPH, MANDALA, KALEIDOSCOPE, FRACTAL.

Same contract as LampShow/VUMeter in loudness.py: `.ok`, and
`.render(canvas, raw)` drawing one 720x480 frame onto a black canvas. They
read the SpectrumAnalyzer they're given for everything else: `displayed`
(smoothed 0..1 bar levels), `raw_levels` (unsmoothed, for beats),
`last_samples` (the latest 1024-sample mono chunk, int16 scale, or None while
the network mic is stalled) and `gain_bias` (for the title).

The idea throughout: today's CPU, pre-2000 screens. Everything is drawn in
code at NTSC resolution, so it stays crisp on the 480i puppets and scales
cleanly to QUAD's 1024x768.
"""
import time
from pathlib import Path

import numpy as np
import pygame

W, H = 720, 480
SAMPLE_RATE = 44100
BASE_DIR = Path(__file__).resolve().parent

ORANGE = (255, 165, 0)
GRID_ORANGE = (230, 150, 0)


def format_hz(hz):
    if hz >= 1000:
        k = hz / 1000
        return f"{k:g}K" if k == int(k) else f"{int(k)}K{int(round((k - int(k)) * 10))}"
    return f"{hz:g}"


class BeatDetector:
    """A beat is the loudest band in a group jumping well above its own ~1s
    running average, in units of its own running deviation (same measure as
    LampShow), with a minimum gap so one kick fires once."""

    def __init__(self, bands, trigger=1.3, min_gap=0.22):
        self.bands = np.asarray(bands)
        self.trigger = trigger
        self.min_gap = min_gap
        self.mean = 0.0
        self.var = 0.01 ** 2
        self.last = 0.0
        self.strength = 0.0      # z-score of the last beat, for how hard it hits

    def update(self, raw):
        level = float(raw[self.bands].max()) if self.bands.size else 0.0
        excess = level - self.mean
        self.mean += excess * 0.05
        self.var += (excess * excess - self.var) * 0.05
        z = excess / max(np.sqrt(self.var), 0.01)
        now = time.monotonic()
        if z >= self.trigger and now - self.last >= self.min_gap and level > 0.05:
            self.last = now
            self.strength = z
            return True
        return False


# --------------------------------------------------------------------- SPECTRUM PRO

class SpectrumPro:
    """The blue/orange analyzer from the 2026-11-21 mockup: segmented blue
    bars behind an orange measurement grid (0-30 dB scale both sides, a line
    every 5 dB, ticks at every segment), and peak caps that hold, then fall
    under gravity, and get kicked back up when a bar slams into them."""

    TOP, BOTTOM = 113, 378          # 30 dB and 0 dB lines (the mockup's own geometry)
    LEFT, RIGHT = 100, 606          # grid line ends
    BARS_X0, BARS_X1 = 128, 578     # bar area
    SEGMENTS = 20
    SEG_FILL = 0.68                 # lit part of each segment pitch
    BAR_GAP = 7
    DB_RANGE = 30
    ON = (20, 88, 255)
    ON_TOP = (90, 150, 255)         # the highest lit segment, a touch hotter
    OFF = (8, 22, 66)
    CAP = (190, 225, 255)
    PEAK_HOLD = 0.33                # seconds a cap sits still before it falls
    GRAVITY = 4.6                   # full scales per second squared (fall takes 3/4 of 1.18's first try)
    KICK = 0.41                     # share of a bar's rise speed that throws the cap up

    pillarbox = True                # 4:3 with black side bars on 16:9 screens (HIDEF), never stretched

    def __init__(self, analyzer, font_path):
        self.analyzer = analyzer
        self.ok = True
        n = analyzer.num_bars
        self.n = n
        self.peak = np.zeros(n)
        self.vel = np.zeros(n)       # downward is positive
        self.hold = np.zeros(n)
        self.prev = np.zeros(n)
        self.last_time = None
        span = self.BARS_X1 - self.BARS_X0
        self.pitch_x = span / n
        self.bar_w = int(self.pitch_x - self.BAR_GAP)
        self.seg_pitch = (self.BOTTOM - self.TOP) / self.SEGMENTS
        self.seg_h = max(2, int(round(self.seg_pitch * self.SEG_FILL)))
        self.font = pygame.font.Font(str(font_path), 18)
        self.small = pygame.font.Font(str(font_path), 15)
        self.grid = self._build_grid(analyzer)
        self.title_gain = None
        self.title_surf = None
        try:
            self.script = pygame.image.load(str(BASE_DIR / "spectrumpro" / "TITLE.png")).convert_alpha()
        except (pygame.error, FileNotFoundError):
            self.script = None

    def _seg_rect(self, bar, seg):
        x = int(self.BARS_X0 + bar * self.pitch_x + self.BAR_GAP / 2)
        y_bottom = self.BOTTOM - seg * self.seg_pitch
        return pygame.Rect(x, int(round(y_bottom - self.seg_h)) - 1, self.bar_w, self.seg_h)

    def _build_grid(self, analyzer):
        """Everything orange that never moves, on a transparent layer drawn over the bars."""
        g = pygame.Surface((W, H), pygame.SRCALPHA)
        for db in range(0, self.DB_RANGE + 1, 5):
            y = round(self.BOTTOM - db / self.DB_RANGE * (self.BOTTOM - self.TOP))
            pygame.draw.line(g, GRID_ORANGE + (235,), (self.LEFT, y), (self.RIGHT, y), 1)
            label = self.font.render(str(db), True, ORANGE)
            g.blit(label, (self.LEFT - 12 - label.get_width(), y - label.get_height() // 2))
            g.blit(label, (self.RIGHT + 12, y - label.get_height() // 2))
        for seg in range(self.SEGMENTS + 1):          # a tick at every segment boundary
            y = round(self.BOTTOM - seg * self.seg_pitch)
            pygame.draw.line(g, GRID_ORANGE + (220,), (self.LEFT - 7, y), (self.LEFT, y), 1)
            pygame.draw.line(g, GRID_ORANGE + (220,), (self.RIGHT, y), (self.RIGHT + 7, y), 1)
        for x in (self.LEFT, self.RIGHT):
            pygame.draw.line(g, GRID_ORANGE + (200,), (x, self.TOP), (x, self.BOTTOM), 1)
        db = self.small.render("dB", True, ORANGE)
        g.blit(db, (self.LEFT - 12 - db.get_width(), self.TOP - 26))
        g.blit(db, (self.RIGHT + 12, self.TOP - 26))
        hz = self.small.render("(Hz)", True, ORANGE)
        ly = self.BOTTOM + 10
        g.blit(hz, (self.LEFT - 12 - hz.get_width(), ly))
        g.blit(hz, (self.RIGHT + 12, ly))
        for i, c in enumerate(analyzer.band_centers):
            s = self.small.render(format_hz(c), True, ORANGE)
            cx = self.BARS_X0 + (i + 0.5) * self.pitch_x
            g.blit(s, (round(cx - s.get_width() / 2), ly))
        return g

    def _title(self):
        gain = round(self.analyzer.gain_bias)
        if gain != self.title_gain:
            self.title_gain = gain
            self.title_surf = self.small.render(f"SPECTRUM ANALYZER - GAIN {gain:+d} DB", True, ORANGE)
        return self.title_surf

    def _physics(self, level, dt):
        rising = level > self.peak
        speed = np.maximum(0.0, (level - self.prev) / max(dt, 1e-3))
        # A bar slamming into its cap throws it upward; a slow climb just carries it.
        throw = rising & (speed > 1.5)
        self.vel = np.where(throw, -np.minimum(speed * self.KICK, 1.65), self.vel)
        self.vel = np.where(rising & ~throw, 0.0, self.vel)
        self.peak = np.where(rising, level, self.peak)
        self.hold = np.where(rising & ~throw, self.PEAK_HOLD, self.hold)
        flying = self.vel < 0
        falling = (self.hold <= 0) | flying
        self.hold = np.where(falling, self.hold, self.hold - dt)
        self.vel = np.where(falling, self.vel + self.GRAVITY * dt, self.vel)
        self.peak = np.where(falling, self.peak - self.vel * dt, self.peak)
        top = self.peak > 1.0
        self.peak = np.where(top, 1.0, self.peak)
        self.vel = np.where(top & (self.vel < 0), 0.0, self.vel)
        landed = self.peak <= level
        self.peak = np.where(landed, level, self.peak)
        self.vel = np.where(landed & (self.vel > 0), 0.0, self.vel)
        self.hold = np.where(landed & falling & ~flying, self.PEAK_HOLD * 0.5, self.hold)
        self.prev = level.copy()

    def render(self, canvas, raw):
        now = time.monotonic()
        dt = 1 / 30 if self.last_time is None else min(now - self.last_time, 0.1)
        self.last_time = now
        level = np.clip(np.asarray(self.analyzer.displayed, dtype=float), 0.0, 1.0)
        self._physics(level, dt)
        lit = (level * self.SEGMENTS).astype(int)
        for b in range(self.n):
            for s in range(self.SEGMENTS):
                color = (self.ON_TOP if s == lit[b] - 1 else self.ON) if s < lit[b] else self.OFF
                canvas.fill(color, self._seg_rect(b, s))
            cap_seg = min(self.SEGMENTS - 1, int(self.peak[b] * self.SEGMENTS))
            if self.peak[b] > 0.02 and cap_seg >= lit[b]:
                r = self._seg_rect(b, 0)
                y = round(self.BOTTOM - self.peak[b] * (self.BOTTOM - self.TOP)) - 4
                canvas.fill(self.CAP, (r.x, max(self.TOP - 4, y), r.w, 4))
        canvas.blit(self.grid, (0, 0))
        if self.script is not None:
            canvas.blit(self.script, (144, 48))
            canvas.blit(self._title(), (340, 72))
        else:
            t = self._title()
            canvas.blit(t, ((W - t.get_width()) // 2, 70))


# --------------------------------------------------------------------- SCOPE / LISSAJOUS

class Graticule:
    """A Tektronix-style 10x8 graticule (redrawn from the user's SCOPE-GRID
    reference): orange divisions, a center crosshair with five minor ticks
    per division, and the dotted 0%/100% lines with their 10/90 marks used
    for rise-time measurements."""

    X0, Y0, DIV_W, DIV_H = 60, 40, 60, 50     # 600 x 400 screen area

    def __init__(self, font_path):
        self.surf = pygame.Surface((W, H), pygame.SRCALPHA)
        g, c = self.surf, GRID_ORANGE + (190,)
        x1, y1 = self.X0 + 10 * self.DIV_W, self.Y0 + 8 * self.DIV_H
        for i in range(11):
            x = self.X0 + i * self.DIV_W
            pygame.draw.line(g, c, (x, self.Y0), (x, y1), 2 if i in (0, 10) else 1)
        for j in range(9):
            y = self.Y0 + j * self.DIV_H
            pygame.draw.line(g, c, (self.X0, y), (x1, y), 2 if j in (0, 8) else 1)
        cx, cy = self.center
        for k in range(1, 50):
            x = self.X0 + k * self.DIV_W / 5
            if k % 5:
                pygame.draw.line(g, c, (x, cy - 4), (x, cy + 4), 1)
        for k in range(1, 40):
            y = self.Y0 + k * self.DIV_H / 5
            if k % 5:
                pygame.draw.line(g, c, (cx - 4, y), (cx + 4, y), 1)
        font = pygame.font.Font(str(font_path), 14)
        for frac in (-2.5, 2.5):                    # dotted 0% and 100% lines
            y = cy + frac * self.DIV_H
            for x in np.arange(self.X0 + 6, x1, 6):
                pygame.draw.line(g, c, (x, y), (x + 1, y), 1)
        for text, y in (("100", cy - 2.5 * self.DIV_H), ("90", cy - 2 * self.DIV_H),
                        ("10", cy + 2 * self.DIV_H), ("0%", cy + 2.5 * self.DIV_H)):
            s = font.render(text, True, ORANGE)
            g.fill((0, 0, 0, 255), (self.X0 + 2, y - s.get_height() // 2, s.get_width() + 6, s.get_height()))
            g.blit(s, (self.X0 + 5, y - s.get_height() // 2))

    @property
    def center(self):
        return self.X0 + 5 * self.DIV_W, self.Y0 + 4 * self.DIV_H


class Phosphor:
    """Green P31-style trace with afterglow: the last few traces are redrawn
    darker each frame, and every trace gets a soft halo under its bright core."""

    TRAILS = ((70, 255, 120), (30, 150, 70), (16, 90, 42), (8, 48, 22))
    HALO = (12, 70, 32)

    def __init__(self):
        self.history = []

    def draw(self, canvas, pts):
        if pts is not None and len(pts) > 1:
            self.history.insert(0, pts)
        del self.history[len(self.TRAILS):]
        for age in range(len(self.history) - 1, -1, -1):
            p = self.history[age]
            if age == 0:
                pygame.draw.lines(canvas, self.HALO, False, p, 4)
                pygame.draw.aalines(canvas, self.TRAILS[0], False, p)
            else:
                pygame.draw.lines(canvas, self.TRAILS[age], False, p, 1)


def bandwidth(samples, taps):
    """A scope's limited bandwidth: a short moving average takes the hiss off
    the trace the way a real analog front end would."""
    if taps <= 1:
        return samples
    return np.convolve(samples, np.ones(taps) / taps, mode="same")


class Envelope:
    """Slow-release peak follower, so the trace auto-ranges like a scope with
    a smart operator on the volts/div knob, and silence stays a flat line."""

    def __init__(self, floor=300.0):
        self.floor = floor
        self.value = floor

    def update(self, samples):
        peak = float(np.abs(samples).max()) if samples.size else 0.0
        self.value = peak if peak > self.value else self.value * 0.97 + peak * 0.03
        return max(self.value, self.floor)


class Scope:
    """TRACE mode: the waveform, triggered on a rising zero crossing so it
    stands still like a locked scope, scaled to about +-2.5 divisions."""

    WINDOW = 480     # samples across the screen (~11 ms)

    def __init__(self, analyzer, font_path):
        self.analyzer = analyzer
        self.ok = True
        self.grat = Graticule(font_path)
        self.phosphor = Phosphor()
        self.env = Envelope()
        x0 = self.grat.X0
        self.xs = x0 + np.arange(self.WINDOW) * (10 * self.grat.DIV_W) / (self.WINDOW - 1)

    def render(self, canvas, raw):
        s = self.analyzer.last_samples
        pts = None
        cx, cy = self.grat.center
        if s is not None and s.size >= self.WINDOW * 2:
            amp = self.env.update(bandwidth(s, 5))
            hyst = 0.08 * amp
            search = s[: s.size - self.WINDOW]
            ups = np.nonzero((search[:-1] < 0) & (search[1:] >= 0))[0] + 1
            ups = [i for i in ups if i >= 6 and s[i - 6] < -hyst]
            start = ups[0] if ups else 0
            seg = bandwidth(s[max(0, start - 4):start + self.WINDOW + 4], 5)[4:4 + self.WINDOW] / amp
            ys = cy - np.clip(seg, -1.6, 1.6) * 2.5 * self.grat.DIV_H
            pts = np.column_stack([self.xs, ys]).tolist()
        else:
            pts = [(self.xs[0], cy), (self.xs[-1], cy)]
        canvas.blit(self.grat.surf, (0, 0))
        self.phosphor.draw(canvas, pts)


class Lissajous:
    """X-Y mode from a mono mic: the signal against itself a quarter period
    of its strongest frequency later. A steady tone draws a circle, chords
    draw knots and rosettes, drums scribble across the screen."""

    def __init__(self, analyzer, font_path):
        self.analyzer = analyzer
        self.ok = True
        self.grat = Graticule(font_path)
        self.phosphor = Phosphor()
        self.env = Envelope()
        self.lag = 20.0
        self.freqs = np.fft.rfftfreq(1024, 1 / SAMPLE_RATE)
        self.window = np.hanning(1024)

    def render(self, canvas, raw):
        s = self.analyzer.last_samples
        cx, cy = self.grat.center
        pts = [(cx, cy), (cx + 1, cy)]
        if s is not None and s.size >= 1024:
            amp = self.env.update(bandwidth(s, 9))
            mag = np.abs(np.fft.rfft(s[:1024] * self.window))
            band = (self.freqs > 60) & (self.freqs < 4000)
            f0 = self.freqs[band][np.argmax(mag[band])]
            lag = np.clip(SAMPLE_RATE / (4 * f0), 2, 180)
            self.lag += (lag - self.lag) * 0.25
            k = int(round(self.lag))
            smooth = bandwidth(s, 9)[8:-8]
            x, y = smooth[:-k:2] / amp, smooth[k::2] / amp
            r = 3.4 * self.grat.DIV_H
            pts = np.column_stack([cx + np.clip(x, -1.5, 1.5) * r, cy - np.clip(y, -1.5, 1.5) * r]).tolist()
        canvas.blit(self.grat.surf, (0, 0))
        self.phosphor.draw(canvas, pts)


# --------------------------------------------------------------------- RIPPLE

class Ripple:
    """The 1980s wireframe ripple surface, drawn with the floating-horizon
    trick those machines used (front-to-back slices; a column only shows
    where it rises above everything in front of it), in numpy instead of 30
    minutes of BASIC. The bass sets the height of the center spike, the
    overall level stirs the standing ripples, and every beat launches a ring
    that rolls out across the surface."""

    SLICES = 46
    COLS = 360                      # computed at 360 wide (period-correct), doubled to 720
    COLOR_FRONT = np.array([70, 255, 120], dtype=np.float32)
    COLOR_BACK = np.array([18, 110, 48], dtype=np.float32)
    MAX_RINGS = 6
    RING_SPEED = 0.9                # world units per second
    ZOOM = 1.5                      # front edge runs off the sides, plane fills the lower half
    LINK = 5                        # px: longest vertical run used to join neighbouring columns

    def __init__(self, analyzer, font_path):
        self.analyzer = analyzer
        self.ok = True
        n = self.SLICES
        C = self.COLS
        cols = np.arange(C, dtype=np.float32)
        self.z = np.linspace(-1, 1, n, dtype=np.float32)[:, None]          # front to back
        self.depth = 0.55 + 0.45 * (1 - self.z) / 2
        x = (cols[None, :] - C / 2) / (C * 0.47 * self.depth * self.ZOOM)
        self.inside = np.abs(x) <= 1
        self.r = np.sqrt(x ** 2 + self.z ** 2).astype(np.float32)
        self.decay = np.exp(-self.r * 1.5)
        self.spike = np.exp(-(self.r * 11.0) ** 2)
        self.base_y = (H / 2 + ((1 - self.z) / 2 - 0.5) * H * 0.60 * self.ZOOM).astype(np.float32)
        self.scale = 58 * self.depth * self.ZOOM
        t = (np.linspace(0, 1, n, dtype=np.float32) ** 0.8)[:, None]
        self.color = (self.COLOR_FRONT * (1 - t[..., None]) + self.COLOR_BACK * t[..., None]).astype(np.uint8)

        bands = np.asarray(analyzer.band_centers)
        self.bass = np.nonzero(bands < 400)[0]
        self.beats = BeatDetector(self.bass)
        self.rings = []
        self.phase = 0.0
        self.bass_env = 0.0
        self.level_env = 0.0
        self.last_time = None
        self.img = np.zeros((C, H, 3), dtype=np.uint8)
        self.small = pygame.Surface((C, H))

    def render(self, canvas, raw):
        now = time.monotonic()
        dt = 1 / 30 if self.last_time is None else min(now - self.last_time, 0.1)
        self.last_time = now
        raw = np.asarray(raw, dtype=float)
        bass = float(raw[self.bass].max()) if raw.size else 0.0
        level = float(np.mean(self.analyzer.displayed)) if len(self.analyzer.displayed) else 0.0
        self.bass_env += (bass - self.bass_env) * (0.5 if bass > self.bass_env else 0.12)
        self.level_env += (level - self.level_env) * 0.1
        if raw.size and self.beats.update(raw):
            self.rings.append((now, min(1.0, 0.45 + 0.2 * self.beats.strength)))
            del self.rings[:-self.MAX_RINGS]
        self.phase += dt * (2.0 + 6.0 * self.level_env)

        hgt = (np.cos(self.r * 9.0 - self.phase) * self.decay * (0.25 + 0.75 * self.level_env)
               + self.spike * (0.8 + 3.4 * self.bass_env))
        alive = []
        for born, strength in self.rings:
            radius = (now - born) * self.RING_SPEED
            if radius > 1.6:
                continue
            alive.append((born, strength))
            fade = strength * (1 - radius / 1.6)
            hgt = hgt + fade * 1.5 * np.exp(-((self.r - radius) * 8.0) ** 2)
        self.rings = alive

        y = (self.base_y - hgt * self.scale).astype(np.int32)
        y = np.where(self.inside, np.clip(y, 0, H - 1), H)
        front = np.minimum.accumulate(np.vstack([np.full((1, self.COLS), H, np.int32), y[:-1]]), axis=0)
        vis = (y < front) & self.inside
        img = self.img
        img.fill(0)
        # join each visible point to its left neighbour with a short vertical run
        left = np.concatenate([y[:, :1], y[:, :-1]], axis=1)
        run = np.clip(left - y, 0, self.LINK)
        rise = np.clip(y - left, 0, self.LINK)
        si, ci = np.nonzero(vis)
        ys = y[si, ci]
        col = self.color[si, 0]
        img[ci, ys] = col
        # the joins: every point gets a run of 1..LINK pixels, laid out in one go
        for lengths, sign, limit in ((run[si, ci], 1, front[si, ci] - 1), (rise[si, ci], -1, None)):
            total = int(lengths.sum())
            if not total:
                continue
            owner = np.repeat(np.arange(lengths.size), lengths)
            step = np.arange(total) - np.repeat(np.cumsum(lengths) - lengths, lengths) + 1
            yy = ys[owner] + sign * step
            yy = np.minimum(yy, limit[owner]) if limit is not None else np.maximum(yy, 0)
            img[ci[owner], yy] = col[owner]
        pygame.surfarray.blit_array(self.small, img)
        pygame.transform.scale(self.small, (W, H), canvas)


# --------------------------------------------------------------------- shared helpers for batch 2

# The 16-colour EGA/VGA default palette, minus black -- batch 2's colour set.
EGA = [(0, 0, 170), (0, 170, 0), (0, 170, 170), (170, 0, 0), (170, 0, 170), (170, 85, 0), (170, 170, 170),
       (85, 85, 85), (85, 85, 255), (85, 255, 85), (85, 255, 255), (255, 85, 85), (255, 85, 255),
       (255, 255, 85), (255, 255, 255)]
EGA_BRIGHT = [EGA[i] for i in (8, 9, 10, 11, 12, 13, 14)]


class MusicState:
    """What every batch-2 look reads each frame: overall level, bass, a beat
    flag with its strength, and smoothed envelopes of each."""

    def __init__(self, analyzer):
        self.analyzer = analyzer
        bands = np.asarray(analyzer.band_centers)
        self.bass_bands = np.nonzero(bands < 400)[0]
        self.beats = BeatDetector(self.bass_bands)
        self.level = 0.0
        self.bass = 0.0
        self.kick = 0.0          # jumps to 1 on a beat, decays to 0
        self.beat = False
        self.last_time = None
        self.dt = 1 / 30

    def update(self, raw):
        now = time.monotonic()
        self.dt = 1 / 30 if self.last_time is None else min(now - self.last_time, 0.1)
        self.last_time = now
        raw = np.asarray(raw, dtype=float)
        shown = np.asarray(self.analyzer.displayed, dtype=float)
        level = float(shown.mean()) if shown.size else 0.0
        bass = float(raw[self.bass_bands].max()) if raw.size else 0.0
        self.level += (level - self.level) * 0.15
        self.bass += (bass - self.bass) * (0.5 if bass > self.bass else 0.12)
        self.beat = bool(raw.size) and self.beats.update(raw)
        self.kick = 1.0 if self.beat else self.kick * 0.86
        return shown


# --------------------------------------------------------------------- SPIROGRAPH

class Spirograph:
    """A plastic Spirograph set: a gear rolls inside a ring and the pen in one
    of its holes draws the figure, live, a few seconds per design. The music
    sets the pen speed, every beat kicks the pen out of its hole for a moment,
    and when a design closes it fades and a new gear and hole are picked."""

    RING_R = 215                    # px, the fixed outer ring
    TEETH = (96, 105, 120, 144, 150)  # ring sizes in teeth...
    GEARS = (24, 30, 32, 36, 40, 42, 45, 48, 52, 56, 60, 63, 64, 72, 75, 80, 84)  # ...and gear sizes

    def __init__(self, analyzer, font_path):
        self.music = MusicState(analyzer)
        self.ok = True
        self.ink = pygame.Surface((W, H))
        self.rng = np.random.default_rng()
        self.cx, self.cy = W / 2, H / 2
        self.fading = 0
        self.holding = 0
        self._new_design()

    def _new_design(self):
        # only gear/ring pairs that take 5-30 laps to close: the rich, many-lobed figures
        pairs = [(R, g) for R in self.TEETH for g in self.GEARS
                 if g < R - 8 and 5 <= g // np.gcd(g, R) <= 30]
        ring, gear = pairs[int(self.rng.integers(len(pairs)))]
        self.R = self.RING_R
        self.r = self.RING_R * gear / ring
        # two pens: one in a hole near the gear's rim, one near its middle
        self.ds = [self.r * float(self.rng.uniform(0.75, 0.95)), self.r * float(self.rng.uniform(0.30, 0.55))]
        laps = gear // np.gcd(gear, ring)                            # turns of the gear around the ring to close
        self.theta_end = 2 * np.pi * laps
        self.theta = 0.0
        a = int(self.rng.integers(len(EGA_BRIGHT)))
        b = (a + int(self.rng.integers(2, len(EGA_BRIGHT) - 1))) % len(EGA_BRIGHT)   # never a near neighbour
        self.colors = [EGA_BRIGHT[a], EGA_BRIGHT[b]]
        self.swell = 0.0
        self.lasts = [self._pen(0.0, d, 0.0) for d in self.ds]
        self.ink.fill((0, 0, 0))

    def _pen(self, theta, d, wobble):
        R, r, d = self.R, self.r, d * (1 + wobble)
        x = (R - r) * np.cos(theta) + d * np.cos((R - r) / r * theta)
        y = (R - r) * np.sin(theta) - d * np.sin((R - r) / r * theta)
        return self.cx + x, self.cy + y

    def render(self, canvas, raw):
        m = self.music
        m.update(raw)
        if self.holding:                    # admire the finished design for a moment
            self.holding -= 1
            if self.holding == 0:
                self.fading = 30
            canvas.blit(self.ink, (0, 0))
            return
        if self.fading:
            self.fading -= 1
            self.ink.fill((215, 215, 215), special_flags=pygame.BLEND_MULT)
            if self.fading == 0:
                self._new_design()
            canvas.blit(self.ink, (0, 0))
            return
        # pen speed: ~10 s per design at an average level, faster when it's loud
        speed = self.theta_end / 10.0 * (0.6 + 2.0 * m.level)
        step = speed * m.dt
        n = max(2, int(step / 0.03))
        thetas = self.theta + np.linspace(0, step, n + 1)[1:]
        # a beat swells the pen arms out and back over a few frames (no kinks)
        self.swell += ((0.10 if m.kick > 0.5 else 0.0) - self.swell) * 0.25
        for k, d in enumerate(self.ds):
            px, py = self._pen(thetas, d, self.swell)
            pts = [self.lasts[k]] + list(zip(px.tolist(), py.tolist()))
            pygame.draw.lines(self.ink, self.colors[k], False, pts, 2)
            self.lasts[k] = pts[-1]
        self.theta = thetas[-1]
        canvas.blit(self.ink, (0, 0))
        # the plastic: outer ring, the rolling gear, its pen hole
        pygame.draw.circle(canvas, (60, 60, 60), (round(self.cx), round(self.cy)), self.R, 1)
        gx = self.cx + (self.R - self.r) * np.cos(self.theta)
        gy = self.cy + (self.R - self.r) * np.sin(self.theta)
        pygame.draw.circle(canvas, (110, 110, 110), (round(gx), round(gy)), round(self.r), 1)
        for px, py in self.lasts:
            pygame.draw.line(canvas, (110, 110, 110), (round(gx), round(gy)), (round(px), round(py)), 1)
            pygame.draw.circle(canvas, (255, 255, 255), (round(px), round(py)), 3)
        if self.theta >= self.theta_end:
            self.holding = 90       # three seconds on show, one to fade, then a new design


# --------------------------------------------------------------------- MANDALA

class Mandala:
    """Nine rings of petals, bass at the center out to treble at the edge.
    Each petal stretches with its band's level, the rings turn in alternating
    directions, and a beat flashes the ring that caused it."""

    def __init__(self, analyzer, font_path):
        self.music = MusicState(analyzer)
        self.ok = True
        n = analyzer.num_bars
        self.n = n
        self.radius = 34 + np.arange(n) * 22.0          # ring base radii
        self.petals = 6 + 2 * np.arange(n)              # petals per ring
        self.spin = np.where(np.arange(n) % 2, -1.0, 1.0) * (0.25 + 0.05 * np.arange(n))
        self.angle = np.zeros(n)
        self.flash = np.zeros(n)
        self.prev = np.zeros(n)
        hues = np.linspace(0.0, 0.8, n)
        self.colors = []
        for h in hues:
            c = pygame.Color(0)
            c.hsva = (h * 360, 90, 100, 100)
            self.colors.append((c.r, c.g, c.b))
        self.cx, self.cy = W / 2, H / 2

    def render(self, canvas, raw):
        m = self.music
        shown = m.update(raw)
        raw = np.asarray(raw, dtype=float)
        if raw.size == self.n:
            jump = raw - self.prev
            self.flash = np.where(jump > 0.25, 1.0, self.flash * 0.85)
            self.prev = raw.copy()
        self.angle += self.spin * m.dt * (0.4 + 1.6 * m.level)
        # back to front: outer rings first, so inner petals sit on top
        for i in range(self.n - 1, -1, -1):
            lvl = float(shown[i]) if i < len(shown) else 0.0
            k = int(self.petals[i])
            base = self.radius[i]
            length = 10 + 30 * lvl
            width = np.pi / k * (0.55 + 0.35 * lvl)
            a = self.angle[i] + np.arange(k) * 2 * np.pi / k
            col = self.colors[i]
            bright = 0.35 + 0.65 * max(lvl, self.flash[i])
            fill = tuple(int(c * bright * 0.45) for c in col)
            edge = tuple(min(255, int(c * bright + 60 * self.flash[i])) for c in col)
            for ang in a:
                p = [(self.cx + base * np.cos(ang - width), self.cy + base * np.sin(ang - width)),
                     (self.cx + (base + length * 0.6) * np.cos(ang - width * 0.8),
                      self.cy + (base + length * 0.6) * np.sin(ang - width * 0.8)),
                     (self.cx + (base + length) * np.cos(ang), self.cy + (base + length) * np.sin(ang)),
                     (self.cx + (base + length * 0.6) * np.cos(ang + width * 0.8),
                      self.cy + (base + length * 0.6) * np.sin(ang + width * 0.8)),
                     (self.cx + base * np.cos(ang + width), self.cy + base * np.sin(ang + width))]
                pygame.draw.polygon(canvas, fill, p)
                pygame.draw.aalines(canvas, edge, True, p)
        core = 14 + 22 * m.bass + 10 * m.kick
        pygame.draw.circle(canvas, self.colors[0], (round(self.cx), round(self.cy)), round(core))
        pygame.draw.circle(canvas, (255, 255, 255), (round(self.cx), round(self.cy)), round(core * 0.45))


# --------------------------------------------------------------------- KALEIDOSCOPE

class Kaleidoscope:
    """A wedge of coloured glass mirrored eight ways. One blob per band
    orbits in the wedge at a size set by its level, leaving trails; beats
    spin the whole set of pieces. Drawn at 120x120 chunky pixels per
    eighth, so the mirror seams stay sharp like the real tube."""

    S = 240                         # mirrored square, drawn at half size then doubled

    def __init__(self, analyzer, font_path):
        self.music = MusicState(analyzer)
        self.ok = True
        n = analyzer.num_bars
        self.n = n
        self.src = pygame.Surface((self.S, self.S))
        self.big = pygame.Surface((self.S * 2, self.S * 2))
        rng = np.random.default_rng(7)
        k = 2 * n                                    # two pieces of glass per band
        self.band = np.arange(k) % n
        self.orbit = rng.uniform(4, 70, k)
        self.speed = rng.uniform(0.3, 1.1, k) * rng.choice((-1, 1), k)
        self.phase = rng.uniform(0, 2 * np.pi, k)
        self.colors = [EGA_BRIGHT[i % len(EGA_BRIGHT)] for i in range(k)]
        self.twist = 0.0
        self.tri = np.tri(self.S // 2, dtype=bool)[..., None]

    def render(self, canvas, raw):
        m = self.music
        shown = m.update(raw)
        self.twist += m.dt * (0.15 + 0.4 * m.level) + 0.35 * (1.0 if m.beat else 0.0)
        self.src.fill((6, 6, 6), special_flags=pygame.BLEND_SUB)            # trails fade
        h = self.S / 2 - 38                          # near the corner that becomes the picture's center
        pts = []
        for i in range(len(self.band)):
            lvl = float(shown[self.band[i]]) if self.band[i] < len(shown) else 0.0
            a = self.phase[i] + self.twist * self.speed[i] * 3
            r = self.orbit[i] * (0.7 + 0.3 * np.sin(a * 0.7))
            x, y = h + r * np.cos(a), h + r * np.sin(a)
            pts.append((x, y))
            size = 4 + 16 * lvl
            pygame.draw.circle(self.src, self.colors[i], (round(x), round(y)), round(size))
        # shards of glass between neighbouring pieces, drawn dim so the blobs sit on them
        for i in range(0, len(pts) - 2, 3):
            c = tuple(v // 3 for v in self.colors[i])
            pygame.draw.polygon(self.src, c, pts[i:i + 3])
        arr = pygame.surfarray.pixels3d(self.src)
        q = np.array(arr[:self.S // 2, :self.S // 2])
        del arr
        q = np.where(self.tri, q, q.transpose(1, 0, 2))                     # mirror across the diagonal
        top = np.concatenate([q, q[::-1]], axis=0)
        full = np.concatenate([top, top[:, ::-1]], axis=1)
        pygame.surfarray.blit_array(self.src, full)
        pygame.transform.scale(self.src, (self.S * 2, self.S * 2), self.big)
        x0 = (W - self.S * 2) // 2
        canvas.blit(self.big, (x0, 0))
        # the tube's edge: mirror the outer columns into the side margins
        canvas.blit(pygame.transform.flip(self.big.subsurface((0, 0, x0, H)), True, False), (0, 0))
        canvas.blit(pygame.transform.flip(self.big.subsurface((self.S * 2 - x0, 0, x0, H)), True, False),
                    (W - x0, 0))


# --------------------------------------------------------------------- FRACTAL

class Fractal:
    """A Julia set at CGA-ish 180x120 chunky pixels. Its seed walks the edge
    of the Mandelbrot set's main cardioid (where the shapes are most alive)
    at a pace set by the music; the bass breathes the zoom, and the colours
    cycle through a 16-colour palette like a demo-scene palette rotation,
    jumping ahead on every beat."""

    GW, GH = 180, 120
    ITER = 24

    def __init__(self, analyzer, font_path):
        self.music = MusicState(analyzer)
        self.ok = True
        x = np.linspace(-1.6, 1.6, self.GW)
        y = np.linspace(-1.07, 1.07, self.GH)
        self.grid = (x[:, None] + 1j * y[None, :]).astype(np.complex64)
        self.walk = 0.0
        self.cycle = 0.0
        pal = []
        for i in range(32):                         # 16 colours there and back, so the cycle has no seam
            t = i / 16 if i < 16 else (32 - i) / 16
            c = pygame.Color(0)
            c.hsva = ((200 + 300 * t) % 360, 85, 35 + 65 * t, 100)
            pal.append((c.r, c.g, c.b))
        self.palette = np.array(pal, dtype=np.uint8)
        self.small = pygame.Surface((self.GW, self.GH))

    def render(self, canvas, raw):
        m = self.music
        m.update(raw)
        self.walk += m.dt * (0.05 + 0.35 * m.level)
        self.cycle += m.dt * (4 + 20 * m.level) + (6 if m.beat else 0)
        # a point on the main cardioid's edge, pulled slightly inside so the set stays connected
        t = self.walk
        mu = 0.5 * np.exp(1j * t) * 0.985
        c = mu * (1 - mu)
        zoom = 1.0 - 0.18 * m.bass
        z = (self.grid * zoom).ravel()
        c = np.complex64(c)
        count = np.full(z.size, -1, dtype=np.int32)          # -1 = never escaped (inside the set)
        where = np.arange(z.size)
        for k in range(self.ITER):
            z = z * z + c
            esc = (z.real * z.real + z.imag * z.imag) > 4.0
            if esc.any():
                count[where[esc]] = k
                keep = ~esc
                z, where = z[keep], where[keep]
            if not z.size:
                break
        idx = (count + int(self.cycle)) % len(self.palette)
        img = self.palette[idx]
        img[count < 0] = (0, 0, 0)
        img = img.reshape(self.GW, self.GH, 3)
        pygame.surfarray.blit_array(self.small, img)
        pygame.transform.scale(self.small, (W, H), canvas)
