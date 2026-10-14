"""Knob HUD (dock/hud_fx.KnobHud, the approved Hi-Fi LED ladder) + its controller driver.

Covers: every state renders 6 KEY_SIZE tiles; red ONLY when muted; deterministic; a session ends
pixel-identical to the page; the controller pushes only changed keys, hands back and then pushes
nothing; a detent during the exit re-enters without a hard cut; no state leaks between sessions;
the 'fresh' flag gates the power-on sparkle; a knob switch wipes over the old HUD (no page flash).
Never touches the device or the user's config (in-memory Config + a fake dock + a fake clock).
"""
import os
import sys
import threading
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("AJAZZDOCK_CONFIG", os.path.join(os.environ.get("TEMP", "/tmp"), "_knobhud_cfg.json"))

import numpy as np                                                            # noqa: E402
from PIL import Image                                                         # noqa: E402

from dock import device, images                                               # noqa: E402
from dock import hud_fx                                                       # noqa: E402
from dock import controller as C                                              # noqa: E402
from dock.config import Config, default_config                               # noqa: E402

fails = []


def check(name, cond):
    print(("  ok " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def set_geo(size, shift):
    images.KEY_SIZE = size
    device.KEY_SIZE = size
    device.CONTENT_SHIFT = shift


def red_px(img):
    """Pixels that read as RED on the LCD: red is the top channel, hue within +-15 deg of 0,
    saturated, and bright enough to see (the dim unlit-lens tint of a red zone counts)."""
    a = np.asarray(img.convert("RGB"), np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(axis=2), a.min(axis=2)
    ch = np.maximum(mx - mn, 1e-6)
    hue = (g - b) / ch                                   # 60 deg units, valid where r is the max
    return int(np.count_nonzero((r >= mx) & (np.abs(hue) <= 0.25) & ((mx - mn) / np.maximum(mx, 1) >= 0.45)
                                & (mx >= 40)))


def gray_page(size):
    out = []
    for i in range(6):
        im = Image.new("RGB", size, (40 + 8 * i, 44 + 8 * i, 50 + 8 * i))
        im.paste((150, 150, 150), (size[0] // 4, size[1] // 4, size[0] // 2, size[1] // 2))
        out.append(im)
    return out


def st_for(t, page, *, value=50, name="Speakers", vmax=100, unit="%", muted=False, accent=None,
           relative=0, events=1, remaining=1.0, disp=None, vel=0.0, d=1, fresh=True):
    return {"t": t, "since_event": 0.0, "remaining": remaining, "events": events, "name": name,
            "unit": unit, "vmax": vmax, "value": value,
            "disp": (None if value is None else float(value)) if disp is None else disp,
            "vel": vel, "dir": d, "relative": relative, "muted": muted, "accent": accent,
            "page": page, "tile": hud_fx.T, "gap": hud_fx.GAP, "fresh": fresh}


def settle(hud, page, **kw):
    """Run one state long enough for every transient (enter, sparkle, peak, kicks) to settle."""
    tiles = None
    for k in range(60):
        tiles = hud.render(st_for(k * 0.02, page, **kw))
    return tiles


REF = ((68, 68), (-1, 2))          # the user's calibration
ALT = ((88, 88), (-6, 8))          # the factory default (another user's dock)

# ---- 1) geometry: the reference calibration renders as designed, others are adapted --------------
set_geo(*REF)
check("reference calibration -> native 68 px, no nudge", hud_fx._geometry() == ((68, 68), (0, 0)))
set_geo(*ALT)
check("88 px / shift (-6,8) -> resized, nudged into the visible window",
      hud_fx._geometry() == ((88, 88), (2, -2)))

# ---- 2) every state renders 6 KEY_SIZE tiles, red only when muted --------------------------------
STATES = [dict(value=v) for v in (0, 5, 50, 99, 100)]
STATES += [dict(value=v, vmax=200, name="Discord out") for v in (0, 100, 200)]
STATES += [dict(value=None, relative=r, accent=(40, 120, 255), d=r, name="RGB hue") for r in (1, -1)]
STATES += [dict(value=72, name="A thirty character long name!!"), dict(value=42, unit="")]
STATES += [dict(value=150, vmax=200, name="Discord", accent="#35e08a")]
MUTED = [dict(value=v, muted=True) for v in (0, 72, 100)]
for geo in (REF, ALT):
    set_geo(*geo)
    page = gray_page(geo[0])
    bad_size, red_unmuted, muted_red = [], [], []
    for kw in STATES + MUTED:
        hud = hud_fx.KnobHud()
        frames = [hud.render(st_for(k * 0.02, page, **kw)) for k in range(40)]
        if any(len(fr) != 6 or any(t.size != geo[0] or t.mode != "RGB" for t in fr) for fr in frames):
            bad_size.append(kw)
        reds = sum(red_px(t) for fr in frames for t in fr)
        if kw.get("muted"):
            muted_red.append(reds > 0)
        elif reds:
            red_unmuted.append((kw, reds))
    check(f"{geo[0][0]} px: every state renders 6 RGB tiles of KEY_SIZE", not bad_size)
    check(f"{geo[0][0]} px: no red pixel anywhere while NOT muted {red_unmuted[:2]}", not red_unmuted)
    check(f"{geo[0][0]} px: muted keeps the red treatment", all(muted_red))
check("zone ramp: green -> yellow -> orange, never red",
      [hud_fx.zone_col(g) for g in (0, 13, 14, 19, 20, 23)]
      == [hud_fx.ZONE_GREEN] * 2 + [hud_fx.ZONE_YELLOW] * 2 + [hud_fx.ZONE_ORANGE] * 2
      and red_px(Image.new("RGB", (4, 4), hud_fx.ZONE_ORANGE)) == 0
      and red_px(Image.new("RGB", (4, 4), hud_fx.RED)) == 16)
set_geo(*REF)
page = gray_page(REF[0])
hud = hud_fx.KnobHud()
t_mute = settle(hud, page, value=72, muted=True)
check("muted still shows the level (ghost segments lit up to 72 %, dark above)",
      np.asarray(t_mute[4]).max() > np.asarray(hud.render(st_for(2.0, page, value=0, muted=True))[4]).max())
hud = hud_fx.KnobHud()
tiles = settle(hud, page, value=95)
check("unmuted 95 %: the top ladder key is lit orange, not red",
      red_px(tiles[5]) == 0 and np.asarray(tiles[5])[..., 0].max() > 200)

# ---- 3) deterministic: the same st sequence gives the same pixels --------------------------------
def run_seq(page):
    hud = hud_fx.KnobHud()
    out = []
    v = 30.0
    for k in range(120):
        t = k * 0.012
        if k % 4 == 0 and k < 80:
            v = min(100.0, v + 3)
        out.append([x.tobytes() for x in hud.render(
            st_for(t, page, value=int(v), disp=v, events=1 + k // 4, vel=250.0 if k < 80 else 0.0,
                   remaining=max(0.0, 1.6 - t), muted=k > 100))])
    return out


check("deterministic for the same state sequence", run_seq(page) == run_seq(page))

# ---- 4) a session ends pixel-identical to the page faces (both calibrations) ---------------------
for geo in (REF, ALT):
    set_geo(*geo)
    pg = gray_page(geo[0])
    hud = hud_fx.KnobHud()
    for k in range(40):
        hud.render(st_for(k * 0.03, pg, value=64, remaining=max(0.0, 1.0 - k * 0.03)))
    last = hud.render(st_for(1.25, pg, value=64, remaining=0.0))
    check(f"{geo[0][0]} px: the exit ends pixel-identical to the page",
          all(a.tobytes() == b.tobytes() for a, b in zip(last, pg)))
set_geo(*REF)

# ---- 5) the fresh flag gates the power-on sparkle ------------------------------------------------
def enter_frames(fresh):
    hud = hud_fx.KnobHud()
    fr = [hud.render(st_for(k * 0.02, page, value=20, fresh=fresh)) for k in range(8)]
    return hud, fr


h_on, f_on = enter_frames(True)
h_off, f_off = enter_frames(False)
check("fresh HUD: the scan-line sparkles the unlit segments", max(h_on.Hd[8:]) > 0 or any(
    a[5].tobytes() != b[5].tobytes() for a, b in zip(f_on, f_off)))
check("not fresh: no sparkle (no white-hot core above the level)",
      all(h == 0.0 for h in h_off.Hd[5:]) and any(a[5].tobytes() != b[5].tobytes()
                                                    for a, b in zip(f_on, f_off)))
check("both settle to the same picture",
      [x.tobytes() for x in settle(h_on, page, value=20)] == [x.tobytes() for x in settle(h_off, page, value=20)])

# ---- 6) no state leaks between sessions (KnobHud.reset) ------------------------------------------
hud = hud_fx.KnobHud()
for k in range(50):                                      # a loud, muted, spun session first
    hud.render(st_for(k * 0.02, page, value=min(100, 40 + 3 * k), events=1 + k, vel=150.0,
                      muted=k > 35, d=1))
hud.reset()
fresh_hud = hud_fx.KnobHud()
seq = [st_for(k * 0.02, page, value=None, relative=-1, accent=(255, 0, 200), events=1 + k // 5, d=-1,
              name="RGB hue") for k in range(40)]
a = [[x.tobytes() for x in hud.render(dict(s))] for s in seq]
b = [[x.tobytes() for x in fresh_hud.render(dict(s))] for s in seq]
check("a reset() instance renders exactly like a brand-new one", a == b)


# ---- 7) the controller driver --------------------------------------------------------------------
class FakeDock:
    image_rotation, image_mirror = 90, False

    def __init__(self):
        self.log = []                 # (frame, key, bytes, quality)
        self.frame = 0
        self.flushes = 0

    def set_key_pil(self, i, img, quality=95, subsampling=0, **k):
        self.log.append((self.frame, i, img.convert("RGB").tobytes(), quality))

    def set_key_image(self, *a, **k):
        pass

    def flush(self):
        self.flushes += 1

    def set_brightness(self, *a):
        pass

    def clear_all(self):
        pass


def make_ctrl(geo=REF):
    data = default_config()
    data["display"] = {"w": geo[0][0], "h": geo[0][1], "dx": geo[1][0], "dy": geo[1][1], "inset": 0}
    c = C.DockController(Config(data))
    c.dock = FakeDock()
    c.connected = True
    clock = [1000.0]
    c._hud_clock = lambda: clock[0]
    return c, clock


def drive(c, clock, events, until, frames=None):
    """Device-faithful loop: one HUD frame per pass; a pass costs max(10 ms, the USB time of the
    keys it pushed) like the bench (6 x 1.35 ms HID writes per key + 1 flush)."""
    events = sorted(events, key=lambda e: e[0])
    t0 = clock[0]
    while clock[0] - t0 <= until:
        while events and events[0][0] <= clock[0] - t0 + 1e-9:
            _, kw = events.pop(0)
            c.show_value_hud(**kw)
        before = len(c.dock.log)
        c.dock.frame += 1
        c._advance_volume_hud()
        n = len(c.dock.log) - before
        if frames is not None:
            frames.append((clock[0] - t0, n, c._volume_hud is not None))
        clock[0] += max(0.010, (1.35 * (6 * n + 1) / 1000.0) if n else 0.0)


SPIN = [(0.0, dict(value=30, name="Speakers"))]
for k in range(24):
    SPIN.append((0.03 * (k + 1), dict(value=32 + 2 * k, name="Speakers")))
SPIN += [(1.3, dict(value=76, name="Speakers")), (1.46, dict(value=74, name="Speakers")),
         (1.96, dict(value=74, name="Speakers", muted=True))]

c, clock = make_ctrl()
page_faces = [c._face_for_index(i).convert("RGB").tobytes() for i in range(6)]
fr = []
drive(c, clock, list(SPIN), 3.6, fr)
log = c.dock.log
hud_push = [e for e in log if e[3] == C._ANIM_Q]
check("controller: the first frame pushes all 6 keys once (what the keys show is not known)",
      sorted(e[1] for e in log if e[0] == 1) == [0, 1, 2, 3, 4, 5])
last = {}
dup = 0
for f, i, raw, q in hud_push:
    if last.get(i) == raw:
        dup += 1
    last[i] = raw
check(f"controller: every HUD push changes that key's pixels (dups={dup})", dup == 0)
check("controller: frames push only the keys that changed (mostly 1-2, never all 6 mid-spin)",
      max(n for t, n, on in fr if 0.2 < t < 1.2) < 6 and
      sum(n for t, n, on in fr if on) / max(1, sum(1 for t, n, on in fr if on and n)) < 3.0)
# The spin's last detent lands at t=0.72. The leading-segment glow cools and the chevrons stay lit
# CH_SPIN_HOLD (0.30 s) then fade once, reaching a FIXED POINT ~0.55 s later - after that a resting
# HUD must cost nothing until the next detent at t=1.30.
_rest = [(t, n) for t, n, on in fr if 0.72 < t < 1.29 and n]
_settled = max((t for t, n in _rest), default=0.72)
check(f"controller: the HUD settles within 0.65 s of the last detent (last push at t={_settled:.2f})",
      _settled <= 0.72 + 0.65)
check("controller: once settled, nothing is pushed until the next detent",
      all(n == 0 for t, n, on in fr if _settled < t < 1.29))
end_frames = [e for e in log if e[3] != C._ANIM_Q]
check("controller: the HUD hands back with one full-quality page render",
      c._volume_hud is None and sorted(e[1] for e in end_frames) == [0, 1, 2, 3, 4, 5]
      and [e[2] for e in sorted(end_frames, key=lambda e: e[1])] == page_faces)
final_hud = {}
for f, i, raw, q in hud_push:
    final_hud[i] = raw
check("controller: the last HUD frame per key already IS the page (exit ends pixel-exact)",
      [final_hud.get(i) for i in range(6)] == page_faces)
n0 = len(log)
for _ in range(50):
    c._advance_volume_hud()
    clock[0] += 0.01
check("controller: after the hand-back nothing renders or pushes", len(c.dock.log) == n0
      and c._hud_keys is None and c._hud_fx.page_src is None)
check("controller: the loop drops out of its 10 ms HUD tier", not c._volume_hud)
frame_t = {k + 1: t for k, (t, n, on) in enumerate(fr)}         # pass number -> session time
check("controller: the unmuted spin never pushed red, the mute push did",
      not any(red_px(Image.frombytes("RGB", (68, 68), e[2])) for e in hud_push
              if 0.2 < frame_t[e[0]] < 1.95)
      and any(red_px(Image.frombytes("RGB", (68, 68), e[2])) for e in hud_push
              if frame_t[e[0]] > 1.96))

# determinism through the controller (same events, same fake clock)
c2, clock2 = make_ctrl()
drive(c2, clock2, list(SPIN), 3.6)
check("controller: deterministic push sequence", c2.dock.log == log)

# ---- 8) a detent during the exit re-enters without a hard cut ------------------------------------
c, clock = make_ctrl()
fr = []
drive(c, clock, [(0.0, dict(value=60, name="Speakers"))], 1.18, fr)     # exit runs from ~1.03 s
pre = dict((e[1], e[2]) for e in c.dock.log)                             # what the keys show now
right_page = pre.get(2) == page_faces[2] and pre.get(5) == page_faces[5]
mark = len(c.dock.log)
fr2 = []
drive(c, clock, [(0.0, dict(value=62, name="Speakers"))], 0.6, fr2)
post = c.dock.log[mark:]
f1 = post[0][0] if post else None
first = dict((e[1], e[2]) for e in post if e[0] == f1)
check("re-entry: the right column had already dissolved to the page", right_page)
check("re-entry: the first frame after the detent keeps the right column on the page (no cut)",
      first.get(2, page_faces[2]) == page_faces[2] and first.get(5, page_faces[5]) == page_faces[5])
c3, clock3 = make_ctrl()
drive(c3, clock3, [(0.0, dict(value=62, name="Speakers"))], 0.6)
settled = dict((e[1], e[2]) for e in c3.dock.log)                         # key 6 = unlit lenses
k5 = [e[2] for e in post if e[1] == 5 and e[3] == C._ANIM_Q]
check("re-entry: key 6 sweeps back in through a blended frame (neither page nor HUD) first",
      len(k5) >= 2 and k5[0] not in (page_faces[5], settled[5]))
check("re-entry: and settles on the same HUD as a straight session", k5 and k5[-1] == settled[5])

# ---- 9) sessions: no leaks, fresh gating, knob switch -------------------------------------------
c, clock = make_ctrl()
drive(c, clock, list(SPIN), 3.6)                                          # loud + muted first
clock[0] += 6.0                                                           # > 5 s later: fresh again
mark = len(c.dock.log)
B = [(0.0, dict(value=None, relative=1, accent=(255, 60, 200), name="RGB hue")),
     (0.09, dict(value=None, relative=1, accent=(200, 60, 255), name="RGB hue"))]
drive(c, clock, list(B), 2.0)
reused = [(e[0] - c.dock.log[mark][0], e[1], e[2], e[3]) for e in c.dock.log[mark:]]
cf, clockf = make_ctrl()
drive(cf, clockf, list(B), 2.0)
fresh_run = [(e[0] - cf.dock.log[0][0], e[1], e[2], e[3]) for e in cf.dock.log]
check("sessions: a later session renders exactly like one on a brand-new controller", reused == fresh_run)

c, clock = make_ctrl()
drive(c, clock, [(0.0, dict(value=40, name="Speakers"))], 2.0)
first_fresh = c._hud_fx is not None
clock[0] += 1.0
c.show_value_hud(41, "Speakers")
c._advance_volume_hud()
check("fresh: a HUD 1 s after the last one skips the sparkle", c._volume_hud["fresh"] is False)
drive(c, clock, [], 2.0)
clock[0] += 5.5
c.show_value_hud(42, "Speakers")
c._advance_volume_hud()
check("fresh: a HUD after 5 s of none plays it again", first_fresh and c._volume_hud["fresh"] is True)
drive(c, clock, [], 2.0)

c, clock = make_ctrl()
drive(c, clock, [(0.0, dict(value=40, name="Speakers"))], 0.5)
on_keys = dict((e[1], e[2]) for e in c.dock.log)
mark = len(c.dock.log)
c.dock.frame += 1
c.show_value_hud(70, "Monitor", accent="#35e08a")
c._advance_volume_hud()
sw = c.dock.log[mark:]
check("knob switch: a new name starts a new session", c._volume_hud["ev"]["name"] == "Monitor"
      and c._volume_hud["events"] == 1)
check("knob switch: its first frame pushes only the swept keys (no full re-push)", 0 < len(sw) < 6)
check("knob switch: no key flashes the page in between",
      all(e[2] != page_faces[e[1]] for e in sw))
drive(c, clock, [], 0.13)
check("knob switch: the enter wipe still shows the old HUD under the scan-line",
      all(e[2] != page_faces[e[1]] for e in c.dock.log[mark:] if e[3] == C._ANIM_Q))
drive(c, clock, [], 2.0)
check("knob switch: still ends on the page", c._volume_hud is None)

# ---- 10) the other calibration through the controller -------------------------------------------
c, clock = make_ctrl(ALT)
pf = [c._face_for_index(i).convert("RGB").tobytes() for i in range(6)]
drive(c, clock, list(SPIN), 3.6)
hp = [e for e in c.dock.log if e[3] == C._ANIM_Q]
check("88 px: HUD tiles are pushed at KEY_SIZE", all(len(e[2]) == 88 * 88 * 3 for e in hp))
fin = {}
for e in hp:
    fin[e[1]] = e[2]
check("88 px: the exit still ends pixel-identical to the page", [fin.get(i) for i in range(6)] == pf)
set_geo(*REF)

# ---- 11) robustness: worker-thread detents while the loop renders; display off ------------------
c, clock = make_ctrl()
c._hud_clock = time.time
stop = [False]
errs = []


def spam(name):
    try:
        for k in range(60):
            c.show_value_hud(k % 101, name, muted=(k % 17 == 0))
            time.sleep(0.002)
    except Exception as e:                       # pragma: no cover - a failure is the test result
        errs.append(e)


ths = [threading.Thread(target=spam, args=(n,)) for n in ("Speakers", "Speakers", "Discord")]
for th in ths:
    th.start()
t_end = time.time() + 0.4
while time.time() < t_end:
    c._advance_volume_hud()
for th in ths:
    th.join()
check("threads: concurrent show_value_hud + loop frames raise nothing", not errs)
c._display_on = False
c._advance_volume_hud()
check("display off: the HUD is dropped", c._volume_hud is None)
c._display_on = True

# ---- 12) speed: a frame stays cheap ---------------------------------------------------------------
set_geo(*REF)
hud = hud_fx.KnobHud()
ms = []
v = 20.0
for k in range(200):
    v = 20 + (k % 70)
    t0 = time.perf_counter()
    hud.render(st_for(k * 0.01, page, value=int(v), disp=v, events=1 + k, vel=200.0,
                      remaining=2.0))
    ms.append((time.perf_counter() - t0) * 1000.0)
check(f"render cost: mean {sum(ms) / len(ms):.2f} ms <= 2 ms while spinning", sum(ms) / len(ms) <= 2.0)

print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.stdout.flush()
os._exit(0 if not fails else 1)
