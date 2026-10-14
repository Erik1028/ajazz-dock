"""Knob HUD ("Hi-Fi LED ladder"): the transient overlay any encoder pops on the 6 LCD keys.

Ported from the concept Erik approved (hud_concepts/segments, 2026-10-11): same look, same
timings. The controller builds one state dict `st` per frame (see DockController.
_advance_volume_hud) and KnobHud.render(st) returns the 6 key tiles.

Layout (each text element lives inside ONE key, graphics may span the bezels):

    [ name (word-wrapped) + pill ]  [ odometer digits + superscript unit ]  [ direction chevrons / mute ]
    [ LED ladder 0-7             ]  [ LED ladder 8-15                    ]  [ LED ladder 16-23           ]

* 24 discrete rounded LED segments (8 per bottom key) rising as a volume wedge, coloured by
  position (green -> yellow -> orange) or the accent; every lit segment carries its own additive
  glow and a glossy floor reflection. UNLIT segments are dim lenses with a brighter 2 px top cap,
  so the 24-step scale stays visible even at backlight 2/100.
* RED MEANS MUTED, nothing else (the one deliberate change from the approved concept, which used
  the heat ramp's red for its top zone): an unmuted ladder, number unit and chevrons never turn
  red, also above 100 % (vmax > 100). An explicit accent still wins (a hue knob may BE red).
* PEAK-HOLD: the highest reached segment stays lit white-hot for 0.7 s after the level drops,
  then falls with gravity and merges into the level. The leading segment glows hotter while the
  knob spins and cools at rest.
* The number is a mechanical odometer drum, clipped hard to a 41 px band (12+ px of black above
  and below, faded at the band edges), cell pitch > band so a resting digit never shows a sliver
  of its neighbour. Slow detents ROLL with a Geneva dwell (a digit sits for 3/4 of a unit and
  rolls only in the middle quarter, higher wheels carry in sync). A fast spin (|vel| > 40/s,
  hysteresis) SNAPS each digit and gives the changed column a 2 px kick in the turn direction, so
  every frame of a spin reads as a clean number. Leading zeros are blank, the group re-centres
  when a wheel appears; 3-digit values drop to a 10% smaller face (>= 5 px side margins).
* The UNIT is a small superscript beside the digits (dropped for 3-digit values, where the face
  needs the room); the top-right key is a stack of 3 direction chevrons that CASCADE per detent
  (white-hot flash, hold, decay to a dim resting glow), stay lit during a continuous spin (one
  static image -> no extra USB traffic), and become a red speaker-X when muted.
* ENTER (0.14 s): a scan-line wipes the page into the HUD left -> right. On a FRESH HUD (st
  "fresh": none shown in the last 5 s) the segments it passes give a short white self-test
  sparkle; a quick follow-up turn skips it (a power-on flourish every few seconds would nag).
  The real level is shown immediately, no count-up.
* EXIT (last 0.27 s): segments extinguish right -> left and a soft band dissolves the HUD back
  into the real page, ending pixel-identical to the page. A detent DURING the exit re-sweeps the
  scan-line from where the band currently is (no hard cut).
* MUTED: a red wave runs down the ladder (each segment drops to a dim red ghost of the muted
  level, so the level stays readable), the digits cross-fade to red, the chevrons become a red
  speaker-X.
* RELATIVE (value=None): nothing is held lit (no fake level). Each detent launches a pulse that
  races along the whole ladder in the turn direction with a white-hot head and a fading comet
  tail, in the accent colour of that detent (a hue knob paints a rainbow of pulses); the centre
  key shows the accent as a glowing swatch that pulses per detent (or, without an accent, a dial
  whose pointer turns 30 deg per detent); the top-right chevrons cascade per detent.
* Dark accents are lifted to a minimum luminance so they survive the 2/100 backlight.

Geometry: everything is designed on a native 68x68 key (the AKP03E's calibrated cell, where the
calibrated content shift (-1, 2) hides tile column 0 and rows 66-67). The calibration is never
assumed: images.KEY_SIZE / device.CONTENT_SHIFT are read on every frame; another shift translates
the HUD layer so it stays centred in what the glass shows, and another key size gets a LANCZOS
resize of the finished tile (the page faces underneath are never resampled for a pure-page tile,
so a session still ends pixel-identical to the page).

USB thrift: every tile is rendered from quantised state and cached; a tile whose state did not
change returns the very same image, so while turning only the ladder key(s) under the moving edge
and the number key change. All animation state decays to a fixed point -> zero changes at rest.
Geometry, digit strips and glyph masks are module-level caches (one instance per HUD is free);
every colour-keyed cache is a bounded LRU. No wall-clock reads: time comes only from st["t"].
"""
from __future__ import annotations

import math
from collections import OrderedDict

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import device as D
from . import images as I

T = 68                              # native key the HUD is designed on (resized if KEY_SIZE differs)
GAP = 18
COLW = T + GAP                      # 86: board pitch of one key column
BW = 3 * T + 2 * GAP                # 240: board width
N, PER = 24, 8                      # segments total / per key
SEG_W, SEG_P = 6, 8                 # segment width / pitch (2 px gap)
X0 = 3                              # first segment x inside a key (visible cols are 1..67)
YB = 58                             # segment bottom (visible rows are 0..65)
H0, H1 = 14, 50                     # wedge: first / last segment height
CX = 34                             # visible centre column of a key (cols 1..67)
BGc = (6, 8, 11)
WHITE = (240, 245, 250)
MINT = (53, 224, 138)
RED = (236, 78, 70)                 # MUTED - and only muted (see zone_col)
NAMEC = (214, 224, 234)
NEUTRAL = (150, 158, 172)           # lens tint of a neutral (relative / muted) ladder
# Position zones of an absolute value: the heat ramp's green and amber (which reads yellow on the
# LCD), and ORANGE where the ramp had red - red would say MUTED on a merely loud level.
ZONE_GREEN = (53, 224, 138)
ZONE_YELLOW = (245, 188, 50)
ZONE_ORANGE = (255, 136, 40)        # hue ~27 deg: clearly apart from RED (~2 deg) at backlight 2
OFF_BODY, OFF_CAP = 0.17, 0.50      # unlit lens body / top-cap mix toward the segment colour
GS = 0.55                           # segment glow strength
Q = 16.0                            # intensity quantisation (pixel-identical rest states)
DECAY = 0.085                       # LED afterglow time constant (s)
DECAY_EXIT = 0.035
HDECAY = 0.07                       # white-hot core cooling
ENTER = 0.14                        # scan-line sweep duration
EXIT, EXIT_END = 0.27, 0.03         # exit window (remaining s) and the final all-page tail
HOLD, GRAV = 0.70, 70.0             # peak hold (s) and fall acceleration (segments/s^2)
PEAK_HOLD = False                   # a knob value is a set-point, not an audio peak: OFF
STAG = 0.010                        # mute wave stagger per segment (s)
GHOST = 0.25                        # muted ghost brightness
# odometer
P = 46                              # wheel pitch (px of drum arc between digits) > band height
R = 26.0                            # drum radius (px) - digits foreshorten toward the band edges
SW = 24                             # strip / slot width
YC = 33                             # digit band centre row
WH = 20                             # band half height -> rows 13..53
VS_ON, VS_OFF = 40.0, 12.0          # |vel| (units/s) above which digits snap instead of roll
KICK, KICK_TAU = 0.0, 0.035         # snap kick (px) - OFF: on fast turns it read as bouncing
FACES = {38: (21, 20), 34: (19, 18), 26: (15, 14)}     # font px -> (slot pitch, ink width)
UNIT_PX, UNIT_GAP = 12, 2
# relative pulses
PSPEED = 24 / 0.20                  # segments per second (a pulse crosses the bar in 0.2 s)
PTAIL = 0.022                       # comet tail time constant (s)
PFLOOR = 0.12                       # tail end is cut here: comets stay compact, no wash
# chevrons
CH_FLASH, CH_HOLD, CH_TAU, CH_STAG = 0.03, 0.07, 0.11, 0.028
CH_SPIN_HOLD = 0.30                 # chevrons stay fully lit this long after the last detent
CH_DEPTH = (1.0, 0.78, 0.56)        # static lead -> tail gradient (direction without motion)
CH_REST = 0.40
# The layout above is centred in the window the reference calibration leaves visible on a 68 px
# key: shift (-1, 2) hides column 0 and rows 66-67 -> visible cols 1..67, rows 0..65.
REF_VIS_CX, REF_VIS_CY = 34.5, 33.0  # centre of that window (pixel edges, not pixel indices)
MAX_NUDGE = 6                       # a wild saved shift must not slide the HUD off the key


def clamp(x, a=0.0, b=1.0):
    return a if x < a else b if x > b else x


def qr(x):
    return round(x * Q) / Q


def qf(x):
    return math.floor(x * Q + 1e-9) / Q


def smooth(x):
    x = clamp(x)
    return x * x * (3.0 - 2.0 * x)


def roll(f):
    """Geneva dwell: a digit SITS for 3/4 of a unit and rolls only in the middle quarter."""
    return smooth((f - 0.375) / 0.25)


def lerp3(a, b, u):
    return tuple(int(round(a[i] + (b[i] - a[i]) * u)) for i in range(3))


def seg_tile(g):
    return g // PER


def seg_x(g):
    return X0 + (g % PER) * SEG_P


def seg_h(g):
    return int(round(H0 + (H1 - H0) * g / (N - 1)))


def seg_X(g):
    """Segment centre in board coordinates (for the sweeps)."""
    return seg_tile(g) * COLW + seg_x(g) + SEG_W / 2.0


def zone_col(g):
    """Colour of segment g on an absolute scale: the heat ramp's thresholds (green < 60 % of the
    ladder, yellow < 82 %, then orange) - never red, which is reserved for MUTED."""
    f = (g + 0.5) / N
    if f < 0.6:
        return ZONE_GREEN
    if f < 0.82:
        return ZONE_YELLOW
    return ZONE_ORANGE


def _rgb(c):
    if c is None:
        return None
    if isinstance(c, str):
        c = c.strip().lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        try:
            return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
        except ValueError:
            return None
    try:
        return tuple(max(0, min(255, int(v))) for v in c[:3])
    except (TypeError, ValueError):
        return None


def acc_vis(c, ymin=105.0):
    """Lift a dark accent so it survives the 2/100 backlight: max channel >= 200, then mix toward
    white until the luminance reaches ymin (hue is kept)."""
    if c is None:
        return None
    m = max(c) or 1
    if m < 200:
        c = tuple(min(255.0, v * 200.0 / m) for v in c) if max(c) else (200.0, 200.0, 200.0)
    y = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    if y < ymin:
        f = (ymin - y) / (255.0 - y)
        c = tuple(v + f * (255.0 - v) for v in c)
    return tuple(int(round(v)) for v in c)


def acc_swatch(c):
    """The swatch shows the colour itself at full drive: scale it so its brightest channel is 255
    (keeps hue AND saturation, a dark accent gets the most light the LCD can give it at 2/100)."""
    m = max(c)
    if not m:
        return (200, 200, 200)
    return tuple(int(round(min(255.0, v * 255.0 / m))) for v in c)


def _resample(img, size):
    """Resize a finished tile to another key size WITHOUT ringing. LANCZOS' negative lobes push
    the dim orange glow tails of the top zone toward red at 88 px (measured: ~(42,23,17) halo
    pixels) - and red must only ever mean MUTED. Bilinear up / box down have no overshoot."""
    grow = size[0] > img.size[0] or size[1] > img.size[1]
    return img.resize(size, Image.BILINEAR if grow else Image.BOX)


def _ss_mask(draw_fn, S=4):
    """Anti-aliased 68x68 L mask from a drawing done at S x supersampling."""
    im = Image.new("L", (T * S, T * S), 0)
    draw_fn(ImageDraw.Draw(im), S)
    return im.resize((T, T), Image.BOX)


class _LRU(OrderedDict):
    def __init__(self, cap):
        super().__init__()
        self.cap = cap

    def get_or(self, k, fn):
        if k in self:
            self.move_to_end(k)
            return self[k]
        v = fn()
        self[k] = v
        if len(self) > self.cap:
            self.popitem(last=False)
        return v


# ---- module-level caches (built once per process, shared by every HUD instance) ----------------
# _STRIPS (3 face sizes x blank) and _CHEV (3 directions x 3 chevrons) are bounded by their keys;
# everything keyed by a colour / name is an LRU, so a hue knob cannot grow memory without bound.
_GEO = None
_STRIPS = {}
_CHEV = {}
_BASES = _LRU(8)                    # ladder background per (key column, scheme)
_STATIC = _LRU(160)                 # composed static faces (name, chevrons, swatch, dial, unit)


def _geo():
    global _GEO
    if _GEO is not None:
        return _GEO
    G = {"sl": [], "body": [], "core": [], "glow": [], "refl": [], "cap": []}
    for g in range(N):
        x, h = seg_x(g), seg_h(g)
        bm = _ss_mask(lambda d, S: d.rounded_rectangle(
            [x * S, (YB - h) * S, (x + SEG_W) * S - 1, YB * S - 1], radius=2 * S, fill=255))
        cm = _ss_mask(lambda d, S: d.rounded_rectangle(
            [(x + 1.5) * S, (YB - h + 1.5) * S, (x + SEG_W - 1.5) * S - 1, (YB - 1.5) * S - 1],
            radius=1 * S, fill=255))
        capm = _ss_mask(lambda d, S: d.rounded_rectangle(
            [x * S, (YB - h) * S, (x + SEG_W) * S - 1, int((YB - h + 2.2) * S) - 1],
            radius=1 * S, fill=255))
        gm = bm.filter(ImageFilter.GaussianBlur(2.6))
        b = np.asarray(bm, np.float32) / 255.0
        c = np.asarray(cm, np.float32) / 255.0
        cp = np.minimum(np.asarray(capm, np.float32) / 255.0, b)
        gl = np.asarray(gm, np.float32) / 255.0
        rf = np.zeros((T, T), np.float32)                         # glossy floor reflection
        for k in range(1, 7):
            rf[YB + k, :] = b[YB - k, :] * 0.30 * (1.0 - (k - 1) / 6.0)
        ys, xs = np.nonzero((gl > 1.0 / 255.0) | (rf > 0))
        sl = (slice(ys.min(), ys.max() + 1), slice(xs.min(), xs.max() + 1))
        G["sl"].append(sl)
        G["body"].append(b[sl][..., None])
        G["core"].append(c[sl][..., None])
        G["glow"].append(gl[sl][..., None])
        G["refl"].append(rf[sl][..., None])
        G["cap"].append(cp[sl][..., None])
    G["X"] = [seg_X(g) for g in range(N)]
    # odometer drum: a band row at offset dy shows the strip at arc R*asin(dy/R) (2x samples),
    # shaded by the drum's cosine and faded hard at the band edge
    rows2 = (np.arange(2 * (2 * WH + 1), dtype=np.float64) + 0.5) / 2.0 - (WH + 0.5)
    G["drum_src"] = 2.0 * R * np.arcsin(np.clip(rows2 / R, -1, 1)) - 0.5
    dy = np.arange(-WH, WH + 1, dtype=np.float64)
    cos = np.cos(np.arcsin(np.clip(dy / R, -1, 1)))
    shade = np.clip((cos - 0.30) / 0.45, 0, 1) * np.clip((WH + 0.5 - np.abs(dy)) / 3.5, 0, 1)
    G["drum_shade"] = shade.astype(np.float32)[:, None]
    _GEO = G
    return G


def _strip(size, blank):
    """Odometer wheel strip (2x): cells k=0..12 hold digit (k-1)%10, glyph centred at (k+.5)*P."""
    k = (size, blank)
    if k not in _STRIPS:
        f = I._font(size * 2, bold=True)
        bb = f.getbbox("0", anchor="ls")
        mid = (bb[1] + bb[3]) / 2.0
        im = Image.new("L", (SW * 2, 13 * P * 2), 0)
        d = ImageDraw.Draw(im)
        for c in range(13):
            dg = (c - 1) % 10
            if blank and dg == 0:
                continue
            d.text((SW, (c + 0.5) * P * 2 - mid), str(dg), font=f, fill=255, anchor="ms")
        _STRIPS[k] = np.asarray(im, np.float32) / 255.0
    return _STRIPS[k]


def _chev_masks(d, i):
    """Chevron i (cascade order) pointing d (+1 up / -1 down / 0 = neutral pair), mask + halo."""
    k = (d, i)
    if k not in _CHEV:
        if d == 0:
            y, dd = (24, 1) if i == 0 else (43, -1)
        else:
            y, dd = 33 + d * (13 - 13 * i), d
        pts = [(CX - 11, y + 5 * dd), (CX, y - 6 * dd), (CX + 11, y + 5 * dd)]
        m = _ss_mask(lambda dr, S: dr.line([(px * S, py * S) for px, py in pts],
                                           fill=255, width=int(4.2 * S), joint="curve"))
        h = m.filter(ImageFilter.GaussianBlur(2.6))
        _CHEV[k] = (np.asarray(m, np.float32)[..., None] / 255.0,
                    np.asarray(h, np.float32)[..., None] / 255.0)
    return _CHEV[k]


def _to_img(arr):
    return Image.fromarray(np.clip(arr + 0.5, 0, 255).astype(np.uint8), "RGB")


def _bg():
    arr = np.empty((T, T, 3), np.float32)
    arr[:] = BGc
    return arr


def _geometry():
    """(key size, (ox, oy)): the device tile size and how far the HUD layer must move so it stays
    centred in the window the calibrated content shift leaves visible. (0, 0) for the reference
    calibration (68 px, shift (-1, 2)), so that case renders exactly the approved pixels."""
    w, h = (max(1, int(v)) for v in I.KEY_SIZE)
    dx, dy = D.CONTENT_SHIFT
    sx, sy = dx * T / float(w), dy * T / float(h)                # the shift in native-key px
    x0, x1 = max(0.0, -sx), min(float(T), T - sx)                # what the glass still shows
    y0, y1 = max(0.0, -sy), min(float(T), T - sy)
    ox = int(round((x0 + x1) / 2.0 - REF_VIS_CX))
    oy = int(round((y0 + y1) / 2.0 - REF_VIS_CY))
    ox = max(-MAX_NUDGE, min(MAX_NUDGE, ox))
    oy = max(-MAX_NUDGE, min(MAX_NUDGE, oy))
    return (w, h), (ox, oy)


# ---- name wrapping -------------------------------------------------------------------------------
def _wrap(d, words, f, maxw, maxl):
    """Greedy word wrap; None if a word does not fit or more than maxl lines are needed."""
    lines, cur = [], ""
    for w in words:
        if d.textlength(w, font=f) > maxw:
            return None
        cand = (cur + " " + w) if cur else w
        if d.textlength(cand, font=f) <= maxw:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines if len(lines) <= maxl else None


def _chop(d, word, f, maxw):
    """Break an over-long single word into chunks that fit (last resort)."""
    out, cur = [], ""
    for ch in word:
        if cur and d.textlength(cur + ch, font=f) > maxw:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return out


def layout_name(name, S=2, maxw=60):
    """-> (lines, font_px). Word-wrap first, then shrink, and only the LAST line is ellipsized."""
    d = ImageDraw.Draw(Image.new("L", (4, 4)))
    txt = (name or "Value").rsplit(".exe", 1)[0].strip()[:40] or "Value"
    words = txt.split()
    for sz in (16, 15, 14, 13, 12, 11):
        f = I._font(sz * S, bold=True)
        maxl = 1 if sz > 16 else (2 if sz >= 15 else 3)
        lines = _wrap(d, words, f, maxw * S, maxl)
        if lines:
            return lines, sz
    sz = 12                                                       # last resort: chop + ellipsize
    f = I._font(sz * S, bold=True)
    toks = []
    for w in words:
        toks += _chop(d, w, f, maxw * S) if d.textlength(w, font=f) > maxw * S else [w]
    lines, cur = [], ""
    for w in toks:
        cand = (cur + " " + w) if cur else w
        if d.textlength(cand, font=f) <= maxw * S or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    if len(lines) > 3:
        lines = lines[:2] + [" ".join(lines[2:])]
    lines[-1] = I._ellipsize(d, lines[-1], f, maxw * S)
    return lines, sz


class KnobHud:
    """One knob-HUD renderer. render(st) -> 6 PIL RGB tiles at images.KEY_SIZE; reset() starts a
    new session (a reused instance also resets itself when st["t"] or st["events"] goes back).

    `st` keys (built by DockController._advance_volume_hud): t, since_event, remaining, events,
    name, unit, vmax, value, disp, vel, dir, relative, muted, accent, page (the 6 page faces under
    the HUD), tile, gap, fresh."""

    def __init__(self):
        self.G = _geo()
        self.geo = None
        self.reset()

    def reset(self):
        """Forget everything of the previous session (animation state, page faces, tile caches)."""
        self.cache = [None] * 6
        self.layer = [None] * 6
        self.page_src, self.page_out, self.page_arr = None, None, None
        self._reset_session()

    def _reset_session(self):
        """Per-HUD animation state (a reused instance starts clean on the next HUD)."""
        self.t_prev = None
        self.Ld = [0.0] * N
        self.Hd = [0.0] * N
        self.segcol = [MINT] * N
        self.flashed = [False] * N
        self.peak_f, self.peak_v, self.peak_hold = 0.0, 0.0, 0.0
        self.muted = False
        self.mute_t, self.unmute_t, self.mute_top = -10.0, -10.0, -1
        self.mf = 0.0
        self.hot = 0.0
        self.events = 0
        self.kick_t, self.kick_dir = -10.0, 0
        self.kicks = []                                            # recent detent times (chevrons)
        self.pulses = []                                           # relative: (t0, dir, colour)
        self.dial_a = self.dial_t = 0.0
        self.snap = False
        self.n_shown, self.nk_t, self.nk_dir, self.nk_cols = None, -10.0, 1, ()
        self.xx_last, self.re = None, None

    # ---- device geometry -----------------------------------------------------------------------
    def _sync_geometry(self):
        geo = _geometry()
        if geo != self.geo:                     # calibration changed: every cached tile is stale
            self.geo = geo
            self.cache = [None] * 6
            self.layer = [None] * 6
            self.page_src = None
        self.ks, self.nudge = geo

    def _fit(self, img):
        """A finished 68 px tile at the device's key size (no-op on the native 68 px key)."""
        return img if self.ks == (T, T) else _resample(img, self.ks)

    def _place(self, img):
        """Move a HUD layer so it sits centred in the visible window of THIS calibration."""
        if self.nudge == (0, 0):
            return img
        out = Image.new("RGB", (T, T), BGc)
        out.paste(img, self.nudge)
        return out

    # ---- page underneath -----------------------------------------------------------------------
    def _pages(self, page):
        if self.page_src is not page:
            self.page_src = page
            rgb = [p.convert("RGB") for p in page]
            # a pure-page tile is the face itself (never resampled -> the exit ends pixel-exact);
            # the 68 px copies only feed the enter/exit blends
            self.page_out = [p if p.size == self.ks else _resample(p, self.ks) for p in rgb]
            self.page_arr = [np.asarray(p if p.size == (T, T) else _resample(p, (T, T)),
                                        np.float32) for p in rgb]
            self.cache = [None] * 6             # a cached pure-page / blended tile shows the OLD page

    # ---- ladder ----------------------------------------------------------------------------------
    @staticmethod
    def _lens(scheme, g):
        if scheme[0] == "zone":
            return zone_col(g)
        if scheme[0] == "acc":
            return scheme[1]
        return NEUTRAL

    def _base(self, c, scheme):
        def make():
            G = self.G
            arr = _bg()
            for j in range(PER):
                g = c * PER + j
                sl, b, cp = G["sl"][g], G["body"][g], G["cap"][g]
                lc = np.array(self._lens(scheme, g), np.float32)
                bgv = np.array(BGc, np.float32)
                body = bgv + OFF_BODY * (lc - bgv)
                cap = bgv + OFF_CAP * (lc - bgv)
                arr[sl] = arr[sl] * (1.0 - b) + body * b
                arr[sl] = arr[sl] * (1.0 - cp) + cap * cp
            tick = scheme[2]
            if tick is not None and seg_tile(tick) == c:           # the 100 % mark (vmax > 100)
                x = seg_x(tick) - 1
                yt = YB - seg_h(tick) - 9
                m = _ss_mask(lambda d, S: d.polygon(
                    [((x - 3.5) * S, yt * S), ((x + 3.5) * S, yt * S), (x * S, (yt + 5) * S)],
                    fill=255))
                a = np.asarray(m, np.float32)[..., None] / 255.0
                arr = arr * (1 - a) + np.array((200, 206, 214), np.float32) * a
            return arr
        return _BASES.get_or((c, scheme), make)

    def _ladder_layer(self, c, scheme, cols, Ls, Hs):
        G = self.G
        arr = self._base(c, scheme).copy()
        for j in range(PER):
            g = c * PER + j
            L, H = Ls[g], Hs[g]
            if L <= 0 and H <= 0:
                continue
            sl = G["sl"][g]
            col = np.array(cols[g], np.float32)
            if L > 0:
                b = G["body"][g]
                arr[sl] = arr[sl] * (1.0 - L * b) + L * (b * col + (G["glow"][g] * GS + G["refl"][g]) * col)
            if H > 0:                                              # white-hot core: lerp toward white
                arr[sl] += H * (G["core"][g] * ((255.0 - arr[sl]) * 0.85) + G["glow"][g] * 55.0)
        return arr

    # ---- static faces --------------------------------------------------------------------------------
    def _name_face(self, name, pill):
        def make():
            S = 2
            lines, sz = layout_name(name, S)
            f = I._font(sz * S, bold=True)
            im = Image.new("RGB", (T * S, T * S), BGc)
            d = ImageDraw.Draw(im)
            lh = round(sz * 1.18)
            block = len(lines) * lh + 5 + 3
            y = 33 - block / 2.0 + lh / 2.0
            for ln in lines:
                d.text((CX * S, y * S), ln, font=f, fill=NAMEC, anchor="mm")
                y += lh
            py = y - lh / 2.0 + 5
            d.rounded_rectangle([(CX - 9) * S, py * S, (CX + 9) * S - 1, (py + 3) * S - 1],
                                radius=int(1.5 * S), fill=pill)
            return im.resize((T, T), Image.LANCZOS)
        return _STATIC.get_or(("name", name, pill), make)

    def _glow_face(self, key, draw_fn, col, glow=0.55, blur=3.0):
        """A coloured glyph drawn by draw_fn on a supersampled mask, with a soft halo."""
        def make():
            m = _ss_mask(draw_fn)
            halo = m.filter(ImageFilter.GaussianBlur(blur))
            mm = np.asarray(m, np.float32)[..., None] / 255.0
            hh = np.asarray(halo, np.float32)[..., None] / 255.0
            cc = np.array(col, np.float32)
            arr = _bg() * (1 - mm) + cc * mm + cc * hh * glow * (1 - mm)
            return _to_img(arr)
        return _STATIC.get_or(key, make)

    def _mute_face(self, scale_q):
        sc = 1.0 + scale_q / 8.0

        def draw(d, S):
            def p(x, y):
                return ((CX + (x - 34) * sc) * S, (33 + (y - 33) * sc) * S)
            d.polygon([p(13, 27), p(20, 27), p(30, 18), p(30, 48), p(20, 39), p(13, 39)], fill=255)
            w = int(4 * S * sc)
            d.line([p(38, 26), p(52, 40)], fill=255, width=w)
            d.line([p(38, 40), p(52, 26)], fill=255, width=w)
        return self._glow_face(("mute", scale_q), draw, RED)

    def _chev_face(self, col, d, levels):
        """3 stacked chevrons in cascade order; level q (0..5): 0 = resting glow, 4 = lit, 5 = flash."""
        def make():
            arr = _bg()
            cc = np.array(col, np.float32)
            wh = np.array((255, 255, 255), np.float32)
            rest = cc * CH_REST
            if rest.max() < 110.0:                                 # resting glow must survive 2/100
                rest = rest * (110.0 / max(1.0, rest.max()))
            for i, q in enumerate(levels):
                m, h = _chev_masks(d, i)
                L = q / 4.0
                if L <= 1.0:
                    c = rest + (cc - rest) * L
                    gl = 0.25 + 0.45 * L
                else:
                    c = cc + (wh - cc) * min(1.0, (L - 1.0) * 2.0)
                    gl = 0.8
                arr = arr * (1 - m) + c * m + cc * h * gl * (1 - m)
            return _to_img(arr)
        return _STATIC.get_or(("chev", col, d, levels), make)

    def _swatch_face(self, col, r):
        def make():
            m = _ss_mask(lambda d, S: d.ellipse([(CX - r) * S, (33 - r) * S, (CX + r) * S - 1,
                                                 (33 + r) * S - 1], fill=255))
            ring = _ss_mask(lambda d, S: d.ellipse([(CX - r) * S, (33 - r) * S, (CX + r) * S - 1,
                                                    (33 + r) * S - 1], outline=255, width=int(1.6 * S)))
            halo = m.filter(ImageFilter.GaussianBlur(4.0))
            mm = np.asarray(m, np.float32)[..., None] / 255.0
            rr = np.asarray(ring, np.float32)[..., None] / 255.0
            hh = np.asarray(halo, np.float32)[..., None] / 255.0
            cc = np.array(col, np.float32)
            arr = _bg() * (1 - mm) + cc * mm + cc * hh * 0.6 * (1 - mm)
            arr = arr * (1 - rr * 0.45) + 255.0 * rr * 0.45
            return _to_img(arr)
        return _STATIC.get_or(("sw", col, r), make)

    def _dial_face(self, col, a_q):
        """A step knob without a value: a dial whose pointer turns per detent."""
        def make():
            a = math.radians(a_q - 90.0)
            ring = _ss_mask(lambda d, S: d.ellipse([(CX - 21) * S, 12 * S, (CX + 21) * S - 1, 54 * S - 1],
                                                   outline=255, width=int(3 * S)))
            ptr = _ss_mask(lambda d, S: d.line(
                [((CX + 6 * math.cos(a)) * S, (33 + 6 * math.sin(a)) * S),
                 ((CX + 15 * math.cos(a)) * S, (33 + 15 * math.sin(a)) * S)],
                fill=255, width=int(5 * S)))
            rm = np.asarray(ring, np.float32)[..., None] / 255.0
            pm = np.asarray(ptr, np.float32)[..., None] / 255.0
            ph = np.asarray(ptr.filter(ImageFilter.GaussianBlur(3.0)), np.float32)[..., None] / 255.0
            cc = np.array(col, np.float32)
            arr = _bg() * (1 - rm) + cc * 0.62 * rm
            arr = arr + cc * ph * 0.7 * (1 - pm)
            arr = arr * (1 - pm) + np.array(WHITE, np.float32) * pm
            return _to_img(arr)
        return _STATIC.get_or(("dial", col, a_q), make)

    def _unit_mask(self, unit, ux):
        def make():
            f = I._font(UNIT_PX * 3, bold=True)
            bb = f.getbbox(unit, anchor="ls")
            m = _ss_mask(lambda d, S: d.text(((ux - bb[0] / 3.0) * S, (YC - 13 - bb[1] / 3.0) * S),
                                             unit, font=f, fill=255, anchor="ls"), S=3)
            return np.asarray(m, np.float32)[..., None] / 255.0
        return _STATIC.get_or(("unitm", unit, ux), make)

    # ---- odometer --------------------------------------------------------------------------------
    @staticmethod
    def _unit_w(unit):
        if not unit:
            return 0
        bb = I._font(UNIT_PX, bold=True).getbbox(unit)
        w = bb[2] - bb[0]
        return w if w <= 18 else 0

    def _odo(self, v, vmax, unit, t, sp, dt):
        """-> layer key for the number key: (face px, wheels ((y0q, blank, x)...), unit, ux)."""
        nw = max(1, len(str(int(vmax))))
        if self.snap and sp < VS_OFF:
            self.snap = False
        elif not self.snap and sp > VS_ON:
            self.snap = True
        n = int(round(v))
        if self.n_shown is not None and n != self.n_shown:
            a, b = str(self.n_shown).rjust(nw), str(n).rjust(nw)
            self.nk_cols = tuple(k for k in range(nw) if a[nw - 1 - k] != b[nw - 1 - k])
            self.nk_dir = 1 if n > self.n_shown else -1
            self.nk_t = t if self.snap else -10.0
        self.n_shown = n
        kick = KICK * math.exp(-(t - self.nk_t) / KICK_TAU) if t - self.nk_t < 0.2 else 0.0
        wheels = []
        if self.snap:
            n_eff = float(len(str(n)))
            for k in range(nw):
                p10 = 10 ** k
                hidden = k > 0 and n < p10
                pos = float((n // p10) % 10)
                if k in self.nk_cols and kick > 0:
                    pos -= self.nk_dir * kick / P
                wheels.append((pos, k > 0 and n < p10, hidden))
        else:
            n_eff = 1.0 + sum(roll(clamp(v - (10 ** k - 1))) for k in range(1, nw))
            for k in range(nw):
                p10 = 10 ** k
                if k == 0:
                    pos = (math.floor(v) % 10) + roll(v - math.floor(v))
                else:
                    base = math.floor(v / p10) % 10
                    lower = v - math.floor(v / p10) * p10
                    pos = base + (roll(lower - (p10 - 1)) if lower > p10 - 1 else 0.0)
                blank = k > 0 and v < p10
                wheels.append((pos, blank, blank and pos <= 1e-6))
        n_vis = int(math.ceil(n_eff - 1e-6))
        size = 38 if n_vis <= 2 else (34 if n_vis == 3 else 26)
        sp_, ink = FACES[size]
        uw = self._unit_w(unit) if n_vis <= 2 else 0
        W = (n_eff - 1.0) * sp_ + ink
        cxg = CX - ((uw + UNIT_GAP) / 2.0 if uw else 0.0)
        right = cxg + W / 2.0 - ink / 2.0                          # centre of the ones wheel
        out = []
        for k, (pos, blank, hidden) in enumerate(wheels):
            if hidden:
                continue
            x = int(round(right - k * sp_))
            out.append((int(round((pos + 1.5) * P * 4)), blank, x))
        ux = int(round(cxg + W / 2.0 + UNIT_GAP)) if uw else None
        return size, tuple(out), (unit if uw else ""), ux

    def _odo_face(self, size, wheels, unit, ux, ucol, mq):
        G = self.G
        col = np.array(lerp3(WHITE, RED, mq / 8.0), np.float32)
        m = np.zeros((T, T), np.float32)
        for (y0, blank, cx) in wheels:
            strip = _strip(size, blank)
            rows = y0 / 2.0 + G["drum_src"]                       # 2x strip rows
            r0 = np.floor(rows).astype(np.int32)
            w = (rows - r0).astype(np.float32)[:, None]
            win = strip[r0] * (1.0 - w) + strip[r0 + 1] * w
            win = win.reshape(2 * WH + 1, 2, SW, 2).mean(axis=(1, 3)) * G["drum_shade"]
            x = cx - SW // 2
            a0, a1 = max(1, x), min(T, x + SW)
            reg = m[YC - WH:YC + WH + 1, a0:a1]
            np.maximum(reg, win[:, a0 - x:a1 - x], out=reg)
        mm = m[..., None]
        bl = np.asarray(Image.fromarray((m * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(2.4)),
                        np.float32)[..., None] / 255.0
        arr = _bg() * (1 - mm) + col * mm + col * bl * 0.30 * (1 - mm)
        if unit:
            um = self._unit_mask(unit, ux)
            arr = arr * (1 - um) + np.array(ucol, np.float32) * um
        return arr

    # ---- compositing with the page ----------------------------------------------------------------
    def _mix(self, idx, layer_img, mode, pos, line_col):
        c = idx % 3
        X = c * COLW + np.arange(T, dtype=np.float32)
        hud = np.asarray(layer_img, np.float32)
        page = self.page_arr[idx]
        if mode == "e":
            a = np.clip((pos - X) / 6.0, 0, 1)
            dd = X - (pos - 3.0)
            li = np.exp(-(dd / 2.0) ** 2) + 0.28 * np.exp(-np.abs(dd) / 6.0)
        else:
            a = np.clip((pos - X) / 16.0, 0, 1)
            a = a * a * (3 - 2 * a)
            li = None
        out = page * (1 - a)[None, :, None] + hud * a[None, :, None]
        if li is not None:
            out = out + li[None, :, None] * np.array(line_col, np.float32) * 0.95
        return _to_img(out)

    @staticmethod
    def _mix_mode(idx, xe, xx):
        c = idx % 3
        lo, hi = c * COLW, c * COLW + T
        if xe is not None:
            if xe + 20 < lo:
                return "page"
            if xe - 26 > hi:
                return "hud"
            return ("e", int(round(xe)))
        if xx is not None:
            if xx <= lo:
                return "page"
            if xx - 16 >= hi:
                return "hud"
            return ("x", int(round(xx)))
        return "hud"

    def _tile(self, idx, layer_key, layer_fn, mix, line_col):
        key = (layer_key, mix, line_col if mix != "hud" else None)
        if self.cache[idx] is not None and self.cache[idx][0] == key:
            return self.cache[idx][1]
        if mix == "page":
            img = self.page_out[idx]
        else:
            if self.layer[idx] is not None and self.layer[idx][0] == layer_key:
                layer = self.layer[idx][1]
            else:
                layer = layer_fn()
                if isinstance(layer, np.ndarray):
                    layer = _to_img(layer)
                layer = self._place(layer)
                self.layer[idx] = (layer_key, layer)
            img = self._fit(layer if mix == "hud" else self._mix(idx, layer, mix[0], mix[1], line_col))
        self.cache[idx] = (key, img)
        return img

    # ---- chevron cascade ---------------------------------------------------------------------------
    def _chev_levels(self, t):
        """Chevron brightness 0..4 per chevron. Driven by the LAST detent only: fully lit while the
        knob keeps turning (a fast spin lands a detent every 40-60 ms, well inside CH_SPIN_HOLD),
        then ONE smooth fade. The old per-detent flash + staggered cascade restarted on every
        detent and flickered on a fast turn. Direction stays readable from the static gradient."""
        if not self.kicks:
            return (0, 0, 0)
        s = t - self.kicks[-1]
        lv = 1.0 if s < CH_SPIN_HOLD else math.exp(-(s - CH_SPIN_HOLD) / CH_TAU)
        return tuple(int(math.floor(lv * k * 4.0 + 0.5)) for k in CH_DEPTH)

    # ---- frame -------------------------------------------------------------------------------------
    def render(self, st):
        t = st["t"]
        if self.t_prev is not None and (t < self.t_prev - 1e-9 or st["events"] < self.events):
            self._reset_session()                                 # a new HUD on a reused instance
        dt = 0.0 if self.t_prev is None else max(0.0, t - self.t_prev)
        self.t_prev = t
        self._sync_geometry()
        self._pages(st["page"])
        G = self.G
        vmax = max(1, int(st["vmax"]))
        rem = st["remaining"]
        relative = bool(st["relative"]) or st["value"] is None
        accent_raw = _rgb(st["accent"])
        accent = acc_vis(accent_raw)
        muted = bool(st["muted"])
        fresh = bool(st.get("fresh", True))
        new_ev = st["events"] - self.events
        self.events = st["events"]
        if new_ev > 0:
            d0 = int(st["relative"] or st["dir"] or 0)
            if d0:
                if d0 != self.kick_dir:
                    self.kicks = []
                self.kick_dir = d0
                self.kicks = (self.kicks + [t])[-4:]
            self.kick_t = t
            if relative:
                kd = self.kick_dir or 1
                for _ in range(min(new_ev, 3)):
                    self.pulses.append((t, kd, accent or MINT))
                self.pulses = self.pulses[-4:]
                self.dial_t += 30.0 * kd * new_ev

        # ---- enter / exit sweeps (+ re-entry when a detent lands during the exit) ----------------
        xe = None
        if t < ENTER:
            u = clamp(t / ENTER)
            xe = 6.0 + (BW + 20.0) * (1.0 - (1.0 - u) ** 1.5)
        xx = None
        if rem < EXIT:
            xx = (BW + 16.0) * clamp((rem - EXIT_END) / (EXIT - EXIT_END))
            self.re = None
        elif self.xx_last is not None:
            self.re = (t, max(0.0, self.xx_last - 8.0))
        if self.re is not None and xx is None:
            t0, x0 = self.re
            dur = max(0.05, ENTER * (BW + 26.0 - x0) / (BW + 20.0))
            u = clamp((t - t0) / dur)
            xr = x0 + (BW + 26.0 - x0) * (1.0 - (1.0 - u) ** 1.5)
            if u >= 1.0:
                self.re = None
            else:
                xe = xr if xe is None else min(xe, xr)
        self.xx_last = xx

        # ---- value being shown --------------------------------------------------------------
        v = 0.0
        if not relative:
            v = clamp(float(st["disp"] if st["disp"] is not None else st["value"]), 0.0, float(vmax))
        f = v / vmax * N

        # ---- mute bookkeeping ---------------------------------------------------------------
        if muted and not self.muted:
            self.mute_t = t
            self.mute_top = int(math.ceil(f)) - 1
        elif not muted and self.muted:
            self.unmute_t = t
        self.muted = muted
        mtarget = 1.0 if muted else 0.0
        step = dt / 0.12
        self.mf = mtarget if abs(self.mf - mtarget) <= step else self.mf + math.copysign(step, mtarget - self.mf)

        # ---- segment targets ------------------------------------------------------------------
        Lt = [0.0] * N
        Ht = [0.0] * N
        cols = list(self.segcol)
        lead = int(math.ceil(f - 1e-9)) - 1
        if relative:
            scheme = ("neutral", None, None)
            alive = []
            for (t0, d, pc) in self.pulses:
                el = t - t0
                if el > (N - 1) / PSPEED + 6.0 * PTAIL:
                    continue
                alive.append((t0, d, pc))
                for g in range(N):
                    pos = g if d > 0 else (N - 1 - g)
                    tp = pos / PSPEED
                    if el < tp:
                        continue
                    amp = 1.0 - 0.55 * pos / (N - 1.0)
                    L = amp * math.exp(-(el - tp) / PTAIL)
                    if L < PFLOOR:
                        continue
                    if L > Lt[g]:
                        Lt[g] = L
                        cols[g] = pc
                    hd = (el - tp) * PSPEED
                    if hd < 1.5:
                        Ht[g] = max(Ht[g], 0.6 * amp * (1.0 - hd / 1.5))
            self.pulses = alive
        else:
            base_col = accent
            scheme = ("acc", accent, None) if accent else ("zone", None, None)
            if muted:
                scheme = ("neutral", None, None)
            # the 100 % mark only means something on a percent scale (Discord's 0..200 %), not on
            # a hue knob's 0..360 degrees
            if vmax > 100 and (st.get("unit") or "") == "%":
                tk = int(round(N * 100.0 / vmax))
                if 0 < tk < N:
                    scheme = (scheme[0], scheme[1], tk)
            segv = abs(st["vel"] or 0.0) * N / vmax                # velocity heat of the leading edge
            self.hot = max(clamp(segv / 18.0, 0, 0.8), self.hot * math.exp(-dt / 0.12))
            for g in range(N):
                cols[g] = base_col or zone_col(g)
                Lt[g] = clamp(f - g)
            if f >= self.peak_f - 1e-6 or muted:                  # peak hold
                self.peak_f, self.peak_v, self.peak_hold = f, 0.0, t + HOLD
            elif t > self.peak_hold:
                self.peak_v += GRAV * dt
                self.peak_f -= self.peak_v * dt
                if self.peak_f <= f:
                    self.peak_f, self.peak_v = f, 0.0
            pk = int(math.ceil(self.peak_f - 1e-9)) - 1
            if PEAK_HOLD and not muted and pk > lead and pk >= 0:
                Lt[pk] = 1.0
                Ht[pk] = 0.75
            if lead >= 0 and not muted:
                Ht[lead] = max(Ht[lead], qr(self.hot) * Lt[lead])
            if muted or t - self.unmute_t < N * STAG + 0.05:       # mute wave: top -> bottom
                for g in range(N):
                    if muted:
                        dropped = (t - self.mute_t) >= (self.mute_top - g) * STAG
                    else:
                        dropped = (t - self.unmute_t) < g * STAG
                    if dropped:
                        cols[g] = RED
                        Lt[g] = GHOST * clamp(f - g)
                        Ht[g] = 0.0
        if xx is not None:                                         # exit: extinguish ahead of the band
            for g in range(N):
                if G["X"][g] > xx - 34:
                    Lt[g] = 0.0
                    Ht[g] = 0.0
        if xe is not None:                                         # entry: short self-test sparkle,
            for g in range(N):                                     # only on a FRESH HUD
                if not self.flashed[g] and xe - 3.0 >= G["X"][g]:
                    self.flashed[g] = True
                    if fresh:
                        self.Hd[g] = max(self.Hd[g], 0.5)
        kd = math.exp(-dt / (DECAY_EXIT if xx is not None else (PTAIL if relative else DECAY)))
        kh = math.exp(-dt / HDECAY)
        for g in range(N):
            if Lt[g] > 0:
                self.segcol[g] = cols[g]
            ld = qf(self.Ld[g] * kd)
            if relative and ld < PFLOOR:
                ld = 0.0
            self.Ld[g] = max(qr(Lt[g]), ld)
            self.Hd[g] = max(qr(Ht[g]), qf(self.Hd[g] * kh))
        cols = self.segcol

        line_col = lerp3(accent if (relative and accent) else MINT, (255, 255, 255), 0.55)
        tiles = [None] * 6

        # ---- top-left: name -------------------------------------------------------------------
        pill = RED if muted else ((150, 158, 170) if relative else (accent or MINT))
        nm = st["name"]
        tiles[0] = self._tile(0, ("name", nm, pill), lambda: self._name_face(nm, pill),
                              self._mix_mode(0, xe, xx), line_col)

        # ---- top-centre: odometer / swatch / dial ----------------------------------------------------
        since_kick = t - self.kick_t
        if relative:
            acc = accent or MINT
            if accent:                       # a colour knob: the colour itself is the value
                sw = acc_swatch(accent_raw)
                r = 18 + int(round(4.0 * math.exp(-since_kick / 0.04)))
                tiles[1] = self._tile(1, ("sw", sw, r), lambda: self._swatch_face(sw, r),
                                      self._mix_mode(1, xe, xx), line_col)
            else:                            # a plain step knob: a dial that turns per detent
                if dt > 0:
                    self.dial_a += (self.dial_t - self.dial_a) * (1.0 - math.exp(-dt / 0.05))
                if abs(self.dial_t - self.dial_a) < 1.5:
                    self.dial_a = self.dial_t
                aq = int(round(self.dial_a / 6.0)) * 6 % 360
                tiles[1] = self._tile(1, ("dial", acc, aq), lambda: self._dial_face(acc, aq),
                                      self._mix_mode(1, xe, xx), line_col)
            ccol = acc
        else:
            ucol = RED if muted else (accent or zone_col(max(0, lead)))
            nv = clamp(float(st["value"]), 0.0, float(vmax)) if st["value"] is not None else v
            size, wheels, unit, ux = self._odo(nv, vmax, st["unit"], t, abs(st["vel"] or 0.0), dt)
            mq = int(round(self.mf * 8))
            tiles[1] = self._tile(1, ("odo", size, wheels, unit, ux, ucol, mq),
                                  lambda: self._odo_face(size, wheels, unit, ux, ucol, mq),
                                  self._mix_mode(1, xe, xx), line_col)
            ccol = accent or zone_col(max(0, lead))

        # ---- top-right: direction chevrons (or the mute glyph) --------------------------------------
        if muted and not relative:
            sq = int(round(3.0 * math.exp(-(t - self.mute_t) / 0.06)))
            tiles[2] = self._tile(2, ("mute", sq), lambda: self._mute_face(sq),
                                  self._mix_mode(2, xe, xx), line_col)
        else:
            d = self.kick_dir
            lv = self._chev_levels(t) if d else (0, 0, 0)
            lv = lv if d else (0, 0)
            tiles[2] = self._tile(2, ("chev", ccol, d, lv), lambda: self._chev_face(ccol, d, lv),
                                  self._mix_mode(2, xe, xx), line_col)

        # ---- bottom row: the ladder -----------------------------------------------------------
        for c in range(3):
            gs = range(c * PER, (c + 1) * PER)
            lk = ("lad", scheme, tuple((self.Ld[g], self.Hd[g], cols[g] if self.Ld[g] > 0 else None)
                                       for g in gs))               # (H on an unlit lens is colour-free)
            Ls, Hs, cl = list(self.Ld), list(self.Hd), list(cols)
            tiles[3 + c] = self._tile(3 + c, lk, lambda c=c: self._ladder_layer(c, scheme, cl, Ls, Hs),
                                      self._mix_mode(3 + c, xe, xx), line_col)
        return tiles
