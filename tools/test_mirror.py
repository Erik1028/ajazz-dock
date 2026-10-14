"""Hexpad_mirror_v1: the frame tap, remote input on the loop thread, idle-screen wake, the JSON pipe;
api 2: sharp 2x faces, knob sliders (set / jog / values).

Runs against a fake HID device and an in-process QLocalSocket on a TEST pipe name, so the real dock
and the real running Hexpad (and its mirror pipe) are never touched; no config is saved (save is
stubbed) and the key bindings are emptied, so no real action can fire. Knob sets go to a FAKE engine;
the real system-volume thread and the monitor dimmer run against a fake volume setter / fake DDC
session - the real system volume and monitors are never touched.
"""
import base64, io, json, math, os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtNetwork import QLocalSocket
from dock.config import Config, default_config
from dock.controller import DockController, remote_event
from dock.device import AKP03, LCD_KEYS, Event, encode_key_image
from dock import mirror as mx
from dock import images, device, actions, monitors

fails = []
def ck(n, cond):
    print(("  ok " if cond else "FAIL ") + n)
    if not cond:
        fails.append(n)

def wait_for(cond, s=2.0):
    t = time.time()
    while time.time() - t < s:
        if cond():
            return True
        time.sleep(0.01)
    return cond()

class FakeHid:
    def __init__(self): self.frames = []
    def write(self, b): self.frames.append(bytes(b)); return len(b)
    def read(self, n, t): return []
    def close(self): pass

# never the real DDC power state (is_off reads a state file on first use)
monitors.is_off = lambda: False
monitors.is_busy = lambda: False

# ---- 1. the frame tap: images count once flush() commits them ----
d = AKP03(); d.dev = FakeHid(); d._initialized = True
ck("starts empty", d.mirror == (0, (None,) * LCD_KEYS))
ck("full tap: (gen, device JPEGs, 2x JPEGs)", d.mirror_full == (0, (None,) * LCD_KEYS, (None,) * LCD_KEYS))
d.set_key_image(0, b"JPEG-A"); d.set_key_image(3, b"JPEG-B")
ck("not visible before flush", d.mirror[0] == 0)
d.flush()
gen, keys = d.mirror
ck("flush commits: gen 1, keys 0 and 3", gen == 1 and keys[0] == b"JPEG-A" and keys[3] == b"JPEG-B" and keys[1] is None)
d.set_key_image(0, b"JPEG-C"); d.clear_key(3); d.flush()
ck("second commit: key0 replaced, key3 cleared", d.mirror[0] == 2 and d.mirror[1][0] == b"JPEG-C" and d.mirror[1][3] is None)
d.flush()
ck("a flush with nothing new keeps gen", d.mirror[0] == 2)
d.clear_all(); d.flush()
ck("clear_all -> all None", all(k is None for k in d.mirror[1]))
d.sleep(); ck("sleep -> dark", d.mirror_dark is True)
d.wake(); ck("wake -> not dark", d.mirror_dark is False)

# ---- 1b. the 2x copy in the tap ----
d.set_key_image(2, b"DEV", hi=b"HI2"); d.flush()
g3, k3, h3 = d.mirror_full
ck("set_key_image(hi=) lands in the 2x tuple", k3[2] == b"DEV" and h3[2] == b"HI2" and len(h3) == LCD_KEYS)
ck("api-1 view unchanged by the 2x tuple", d.mirror == (g3, k3))
face = Image.new("RGB", device.KEY_SIZE, (40, 90, 160))
twox = Image.new("RGB", (2 * device.KEY_SIZE[0], 2 * device.KEY_SIZE[1]), (200, 60, 20))
face._mirror_hi = twox
plain = Image.new("RGB", device.KEY_SIZE, (40, 90, 160))                 # same pixels, no attribute
d.mirror_hi = False
d.dev.frames.clear(); d.set_key_pil(1, face); d.flush(); frames_off = list(d.dev.frames)
ck("mirror_hi off: no 2x JPEG", d.mirror_full[2][1] is None)
d.mirror_hi = True
d.dev.frames.clear(); d.set_key_pil(1, face); d.flush(); frames_on = list(d.dev.frames)
dev_on, hi_on = d.mirror_full[1][1], d.mirror_full[2][1]
ck("device HID frames byte-identical with the 2x copy on", frames_on == frames_off)
ck("device JPEG = the image without any attribute", dev_on == encode_key_image(plain))
him = Image.open(io.BytesIO(hi_on)) if hi_on else None
ck("2x JPEG is a plain JPEG of the 2x copy (unrotated)", him is not None and him.format == "JPEG"
   and him.size == twox.size and him.convert("RGB").getpixel((5, 5))[0] > 150)
d.set_key_pil(1, face); d.flush()
ck("a re-shown face hands the tap the SAME 2x bytes object", d.mirror_full[2][1] is hi_on)
d.set_key_pil(1, face, quality=72, subsampling=2); d.flush()
ck("animation-quality write -> lighter 2x JPEG (q75 4:2:0)", ("hi", 75, 2) in twox._jpeg_cache
   and d.mirror_full[2][1] is twox._jpeg_cache[("hi", 75, 2)])
d.set_key_pil(1, Image.new("RGB", device.KEY_SIZE)); d.flush()
ck("a frame without a 2x copy -> null (device JPEG fallback)", d.mirror_full[2][1] is None)
d.set_key_pil(4, face); d.clear_key(4); d.flush()
ck("clear_key clears the 2x copy too", d.mirror_full[1][4] is None and d.mirror_full[2][4] is None)
d.mirror_hi = False

# ---- 1c. the renderers attach 2x copies, only on the loop thread with the flag on ----
images.KEY_SIZE = device.KEY_SIZE = (68, 68)
K2 = (136, 136)
icon_png = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dial.png")
items = {
    "emoji": {"label": "Mute", "icon": "\U0001F399️", "color": "#203040"},
    "fluent": {"label": "Volume up", "action": {"type": "volume", "volume": "up"}},
    "text": {"label": "Big text key"},
    "swatch": {"label": "Red", "action": {"type": "rgbscene", "color": "#ff2020"}},
    "image": {"label": "Dial", "icon": icon_png},
    "styled": {"label": "Tile", "icon": "\U0001F3AE", "icon_tile": True, "icon_shadow": True, "icon_border": 2},
}
def dev_bytes(img):
    return encode_key_image(img, 90, False)
images.set_mirror_hi(False); images.clear_face_cache()
off = {k: dev_bytes(images.render_face(it)) for k, it in items.items()}
ck("flag off: render_face attaches nothing", all(getattr(images.render_face(it), "_mirror_hi", None) is None
                                                for it in items.values()))
images.set_mirror_hi(True); images.clear_face_cache()
on = {k: images.render_face(it) for k, it in items.items()}
ck("flag on: every static face carries a 136px copy", all(getattr(f, "_mirror_hi", None) is not None
                                                         and f._mirror_hi.size == K2 for f in on.values()))
ck("flag on: device faces byte-identical", all(dev_bytes(on[k]) == off[k] for k in items))
ck("cache hit keeps the same 2x copy", images.render_face(items["emoji"])._mirror_hi is on["emoji"]._mirror_hi)
ck("explicit size (editor preview / mini face) gets no copy",
   getattr(images.render_face(items["text"], size=(40, 40)), "_mirror_hi", None) is None)
from PIL import ImageChops, ImageStat
def _layout_diff(f):                      # the 2x copy brought down to 68 vs the device face
    return sum(ImageStat.Stat(ImageChops.difference(f._mirror_hi.resize(f.size, Image.LANCZOS), f)).mean) / 3
ck("2x copies are the same layout at 2x (label band, fonts, margins scaled)",
   all(_layout_diff(f) < 12 for f in on.values()))
folder_item = {"label": "Apps", "action": {"type": "folder", "folder": "f1"}}
contents = {"key1": items["emoji"], "key2": items["fluent"], "key3": items["swatch"]}
images.set_mirror_hi(False); images.clear_face_cache()
fo_off = dev_bytes(images.folder_face(folder_item, contents))
images.set_mirror_hi(True); images.clear_face_cache()
fo = images.folder_face(folder_item, contents)
ck("folder face: 2x copy + device bytes unchanged", fo._mirror_hi.size == K2 and dev_bytes(fo) == fo_off)
lv_args = ({"label": "CPU"}, "42%", "CPU", 0.42, "percent")
images.set_mirror_hi(False)
lv_off = images.live_face(*lv_args)
images.set_mirror_hi(True)
lv_on = images.live_face(*lv_args)
ck("live face: 2x copy from its 3x render, device pixels identical",
   lv_on._mirror_hi.size == K2 and lv_on.tobytes() == lv_off.tobytes() and not hasattr(lv_off, "_mirror_hi"))
tm = images.timer_face(75, 300, True, False, False, label="Tea")
images.set_mirror_hi(False)
tm_off = images.timer_face(75, 300, True, False, False, label="Tea")
ck("timer face: 2x copy, device pixels identical", tm._mirror_hi.size == K2 and tm.tobytes() == tm_off.tobytes())
amb_ok = True
for style in ("classic", "aurora", "flip", "minimal", "night"):
    images.set_mirror_hi(False)
    a_off = images.ambient_clock_tiles("21", "47", "WED", "02", "JUL", style=style, phase=3.0)
    images.set_mirror_hi(True)
    a_on = images.ambient_clock_tiles("21", "47", "WED", "02", "JUL", style=style, phase=3.0)
    amb_ok = amb_ok and all(getattr(t, "_mirror_hi", None) is not None and t._mirror_hi.size == K2
                            for t in a_on) and [t.tobytes() for t in a_on] == [t.tobytes() for t in a_off]
ck("idle tiles (5 designs): 2x copies, device pixels identical", amb_ok)
other = {}
def _other_thread():
    other["flag"] = images.mirror_hi()
    other["live"] = getattr(images.live_face(*lv_args), "_mirror_hi", None)
th = threading.Thread(target=_other_thread); th.start(); th.join()
ck("the flag is per thread (the GUI thread pays nothing)", other == {"flag": False, "live": None})
images.set_mirror_hi(True)
again = [images.render_face(it) for it in items.values()]
had = all(getattr(f, "_mirror_hi", None) is not None for f in again)
images.drop_mirror_hi()
ck("drop_mirror_hi frees the cached faces' copies", had and all(getattr(f, "_mirror_hi", None) is None for f in again)
   and not any(hasattr(f, "_mirror_hi") for f in images._FACE_CACHE.values()))
images.set_mirror_hi(False); images.clear_face_cache()

# ---- 2. remote input ----
ck("key3 down -> key index 2", (lambda e: e.kind == "key" and e.index == 2 and e.pressed)(remote_event("key3", True, 0)))
ck("btn7 -> button index 6 (input_id btn7)", remote_event("btn7", False, 0).input_id == "btn7")
ck("enc1 turn -1 -> encoder_turn", (lambda e: e.kind == "encoder_turn" and e.index == 1 and e.delta == -1)(remote_event("enc1", None, -1)))
ck("enc2 push", remote_event("enc2", True, 0).input_id == "enc2")
ck("rubbish rejected", remote_event("key9", True, 0) is None and remote_event("btn1", True, 0) is None
   and remote_event("bogus", True, 0) is None and remote_event("key1", None, 0) is None)
ck("remote events are marked remote; the dock's own are not", remote_event("enc1", None, 1).remote is True
   and Event("encoder_turn", 1, delta=1).remote is False)

data = default_config()
for p in data["profiles"]:                 # no bindings anywhere: nothing real can fire
    for pg in p.get("pages", []):
        pg["items"] = {}
    p["globals"] = {}
c = DockController(Config(data)); c.dock = d; c.connected = True
saves = []
c.config.save = lambda *a, **k: saves.append(1)          # never the real config file
seen = []
c._handle_event = lambda ev: seen.append(ev.input_id + ("" if ev.kind == "encoder_turn" else (":down" if ev.pressed else ":up")))
ck("inject accepts a key", c.inject("key2", True) and c.inject("key2", False) and c.inject("enc1", turn=1))
ck("inject rejects rubbish", not c.inject("key0", True))
ck("nothing handled before the loop drains", seen == [])
c._drain_remote()
ck("drained in order on the loop", seen == ["key2:down", "key2:up", "enc1+"])

# idle screen: a wake request leaves it, fires nothing, resets the idle timer
del c._handle_event
fired = []
c._fire_action = lambda *a, **k: fired.append(a)
c._ambient = {"style": "test"}; c._last_input = 0
c.request_wake(); c._drain_remote()
ck("wake leaves the idle screen", c._ambient is None)
ck("wake resets the idle timer", time.time() - c._last_input < 2)
ck("wake fires no binding", fired == [])
del c._fire_action

st = c.status()
ck("status has api 1 + folder/display/ambient", st.get("api") == 1 and "folder" in st and st.get("display_on") is True and st.get("ambient") is False)

ck("danger: system / empty bin / lock; not volume", mx.is_dangerous({"action": {"type": "system", "system": "sleep"}})
   and mx.is_dangerous({"action": {"type": "media"}, "action_hold": {"type": "quick", "op": "recycle_empty"}})
   and mx.is_dangerous({"action": {"type": "quick", "op": "lock"}})
   and not mx.is_dangerous({"action": {"type": "volume", "volume": "up"}}) and not mx.is_dangerous(None))

# ---- 2b. knob sliders: request_set -> loop drain -> a FAKE engine ----
class FakeEngine:
    def __init__(self):
        self.run, self.vol, self.mon, self.polls, self.primed, self.cur = [], [], [], 0, [], {}
    def execute(self, a): self.run.append(a)
    def set_system_volume(self, pct): self.vol.append(pct)
    def set_monitor_brightness(self, pct, idx, quiet=True): self.mon.append((pct, idx, quiet))
    def poll_system_volume(self): self.polls += 1
    def prime_monitor(self, idx): self.primed.append(idx)
    def monitor_value(self, idx): return self.cur.get(idx)

B_UP = {"type": "brightness", "mode": "up", "step": 2}
B_DN = {"type": "brightness", "mode": "down", "step": 2}
knobs = {"enc0+": {"action": B_UP}, "enc0-": {"action": B_DN},
         "enc1+": {"action": {"type": "volume", "volume": "up"}}, "enc1-": {"action": {"type": "volume", "volume": "down"}},
         "enc2+": {"action": {"type": "monitor", "monitor": "up", "index": "all", "value": 10}},
         "enc2-": {"action": {"type": "monitor", "monitor": "down", "index": "all"}}}
data2 = default_config()
for p in data2["profiles"]:
    p["globals"] = dict(knobs)
    for pg in p.get("pages", []):
        pg["items"] = {}
prof = data2["profiles"][0]
while len(prof["pages"]) < 2:
    prof["pages"].append({"name": "P2", "items": {}})
prof["pages"][1]["items"] = {                   # page 2: other turn bindings
    "enc1+": {"action": {"type": "smartlight", "mode": "brightness_up", "step": 10}},
    "enc1-": {"action": {"type": "smartlight", "mode": "brightness_down", "step": 10}},
    "enc2+": {"action": {"type": "monitor", "monitor": "up", "index": 0}},
    "enc2-": {"action": {"type": "monitor", "monitor": "down", "index": 1}},
    "enc0+": {"action": {"type": "none"}}, "enc0-": {"action": {"type": "none"}}}
c2 = DockController(Config(data2)); c2.dock = d; c2.connected = True
c2.config.save = lambda *a, **k: saves.append(2)
fe = FakeEngine(); c2.engine = fe
c2.page_index = 0
ck("slider_info: brightness / volume / all-monitor knobs are abs",
   [c2.slider_info(e) for e in ("enc0", "enc1", "enc2")] == [("abs", "brightness", None), ("abs", "volume", None),
                                                             ("abs", "monitor", None)])
c2.page_index = 1
ck("slider_info: bulb -> jog, +/- on different monitors -> jog, none -> ''",
   [c2.slider_info(e)[0] for e in ("enc0", "enc1", "enc2")] == ["", "jog", "jog"] and c2.slider_info("enc1")[1] == "smartlight")
c2.page_index = 0
ck("request_set rejects rubbish", not c2.request_set("enc5", "volume", 0.5) and not c2.request_set("key1", "volume", 0.5)
   and not c2.request_set("enc1", "volume", float("nan")) and not c2.request_set("enc1", "volume", None))
ck("request_set accepts a slider value", c2.request_set("enc1", "volume", 0.30) and c2.request_set("enc1", "volume", 0.42))
ck("nothing applied before the loop drains", fe.vol == [])
c2._drain_remote()
ck("volume: latest-wins, applied once on the loop", fe.vol == [42])
c2.config.brightness = 70; saves.clear()
c2.request_set("enc0", "brightness", 0.3, False); c2._drain_remote()
ck("dock brightness: live, no save per tick", c2.config.brightness == 30 and c2._pending_brightness == 30 and saves == [])
c2.request_set("enc0", "brightness", 0.35, True); c2._drain_remote()
ck("dock brightness: one save on final", c2.config.brightness == 35 and saves == [2] and c2._bright_save_at == 0.0)
saves.clear()
c2.request_set("enc0", "brightness", 0.4, True); c2.request_set("enc0", "brightness", 0.45, False); c2._drain_remote()
ck("a final is never lost to a later tick", c2.config.brightness == 45 and saves == [2])
saves.clear()
c2.request_set("enc0", "brightness", 0.5, False); c2._drain_remote()
ck("no final: not saved yet", saves == [])
c2._bright_save_at = time.time() - 1.5; c2._drain_remote()
ck("no final: saved once after 1 s of rest", saves == [2] and c2._bright_save_at == 0.0)
c2.request_set("enc2", "monitor", 0.55); c2._drain_remote()
ck("monitor: quiet set through the dimmer, index all", fe.mon == [(55, None, True)])
monitors.is_off = lambda: True
c2.request_set("enc2", "monitor", 0.9); c2._drain_remote()
monitors.is_off = lambda: False
ck("monitor: dropped while the monitors are switched off", fe.mon == [(55, None, True)])
c2.request_set("enc1", "monitor", 0.5); c2.request_set("enc0", "volume", 0.5); c2._drain_remote()
ck("kind mismatch -> dropped", fe.mon == [(55, None, True)] and fe.vol == [42])
c2.request_set("enc1", "volume", 0.6); c2.page_index = 1; c2._drain_remote(); c2.page_index = 0
ck("page switched under the drag (volume knob became a bulb) -> dropped", fe.vol == [42])
c2._ambient = {"style": "test"}; c2._last_input = 0
c2.request_set("enc1", "volume", 0.61); c2._drain_remote()
ck("a set wakes the idle screen AND is applied (not swallowed)", c2._ambient is None and fe.vol == [42, 61]
   and time.time() - c2._last_input < 2)
ck("no knob HUD for sets", c2._volume_hud is None)

# remote turns: never accelerated, never touch the knob's timing
c2._enc_last = {}
for _ in range(5):
    c2.inject("enc0", turn=1)
c2._drain_remote()
ck("5 batched remote turns: 5 unaccelerated steps", fe.run[-5:] == [B_UP] * 5 and len(fe.run) == 5)
ck("remote turns leave _enc_last alone", c2._enc_last == {})
fe.run.clear()
c2._handle_event(Event("encoder_turn", 0, delta=1)); c2._handle_event(Event("encoder_turn", 0, delta=1))
ck("(physical turns still accelerate)", fe.run[0] == B_UP and fe.run[1].get("step", 0) > B_UP["step"] and 0 in c2._enc_last)
fe.run.clear(); c2._enc_last = {}
ck("jog rejects rubbish", not c2.jog("key1", 2) and not c2.jog("enc9", 2) and not c2.jog("enc0", 0)
   and not c2.jog("enc0", "x"))
c2._ambient = {"style": "test"}
ck("jog accepted", c2.jog("enc0", -3))
c2._drain_remote()
ck("jog -3: 3 unaccelerated 'down' steps, idle screen woken, none swallowed",
   fe.run == [B_DN] * 3 and c2._ambient is None and c2._enc_last == {})
fe.run.clear(); c2.jog("enc0", 25); c2._drain_remote()
ck("jog is capped at 10 steps", fe.run == [B_UP] * 10)
fe.run.clear()

# values: from caches only, deduped by the server (section 3)
actions._sysvol_last = (42, True, time.monotonic())
fe.cur[None] = 55.0
c2.config.brightness = 30
sl = {e: c2.slider_info(e) for e in ("enc0", "enc1", "enc2")}
ck("slider_values from caches", c2.slider_values(sl) == {"enc0": {"v": 0.3}, "enc1": {"v": 0.42, "muted": True},
                                                         "enc2": {"v": 0.55, "off": False}})
actions._sysvol_last = None; fe.cur.clear()
ck("unknown values are null", c2.slider_values(sl)["enc1"]["v"] is None and c2.slider_values(sl)["enc2"]["v"] is None)

# the real ordered system-volume thread, with a FAKE volume setter (no COM, never the real volume)
vol_calls, huds = [], []
_real_sv = actions._system_volume
def fake_sysvol(set_pct=None):
    vol_calls.append(set_pct)
    time.sleep(0.01)
    return (set_pct if set_pct is not None else 33), False
actions._system_volume = fake_sysvol
class HudCtl:
    def show_value_hud(self, *a, **k): huds.append(a)
eng = actions.ActionEngine(HudCtl())
eng.set_system_volume(20); eng.set_system_volume(25); eng.set_system_volume(30)
ck("volume set: latest value reaches the setter, no HUD", wait_for(lambda: vol_calls and vol_calls[-1] == 30)
   and huds == [] and set(vol_calls) <= {20, 25, 30})
vol_calls.clear()
eng.poll_system_volume()
ck("quiet poll: one read, no HUD", wait_for(lambda: vol_calls == [None]) and huds == [])
vol_calls.clear()
eng._system_volume_hud()
ck("a knob detent still reads + pops the HUD on the same thread", wait_for(lambda: len(huds) == 1)
   and vol_calls == [None] and huds[0][0] == 33)
ck("one thread does it all", sum(1 for t in threading.enumerate() if t.name == "sysvol-hud") == 1)
actions._system_volume = _real_sv

# the monitor dimmer with a FAKE DDC session (never the real monitors)
ddc = []
class FakeSession:
    def __init__(self, idx): self.idx = idx; ddc.append(("open", idx))
    def current_pct(self): ddc.append(("read", self.idx)); return 50
    def set_pct(self, v): ddc.append(("set", self.idx, v))
    def close(self): pass
_real_bs = monitors.BrightnessSession
monitors.BrightnessSession = FakeSession
huds.clear()
dm = actions._MonitorDimmer(HudCtl())
ck("dimmer value unknown before any read", dm.value(None) is None)
dm.prime(None)
ck("prime: one DDC read seeds the value, nothing set", wait_for(lambda: dm.value(None) == 50.0)
   and not any(x[0] == "set" for x in ddc))
dm.set(70, None, quiet=True)
ck("quiet set: applied, value cached, no HUD", wait_for(lambda: ("set", None, 70.0) in ddc) and dm.value(None) == 70.0
   and huds == [])
dm.set(40, None)
ck("(a normal set still shows the HUD)", wait_for(lambda: len(huds) >= 1))
monitors.BrightnessSession = _real_bs

# ---- 3. the pipe, in process, on a test name ----
app = QCoreApplication.instance() or QCoreApplication(sys.argv)
name = f"Hexpad_mirror_test_{os.getpid()}"
d.set_key_image(1, b"\xff\xd8FACE1"); d.flush()
# the pipe's controller: empty keys + the slider knobs, on the FAKE engine
for p in c.config.data["profiles"]:
    p["globals"] = dict(knobs)
c.engine = FakeEngine()
srv = mx.MirrorServer(c, name=name)
ck("server starts", srv.start())
ck("echo hook installed", c.on_input == srv._on_physical)

def pump(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

class Cli:
    def __init__(self):
        self.sock = QLocalSocket(); self.sock.connectToServer(name)
        self.ok = self.sock.waitForConnected(1000)
        self.got, self.buf = [], b""
        self.sock.readyRead.connect(self.read)
    def read(self):
        self.buf += bytes(self.sock.readAll())
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self.got.append(json.loads(line))
    def send(self, *msgs):
        self.sock.write(b"".join(json.dumps(m).encode() + b"\n" for m in msgs)); self.sock.flush()
    def of(self, t): return [m for m in self.got if m["t"] == t]

c1 = Cli()
ck("client connects", c1.ok)
sock, got = c1.sock, c1.got
pump(300)
kinds = [m["t"] for m in got]
ck("hello, state, controls, faces arrive", kinds[:4] == ["hello", "state", "controls", "faces"])
h0 = got[0] if got else {}
ck("hello says api 2, caps, 6 keys, the 2x size", h0.get("api") == 2 and h0.get("keys") == 6
   and h0.get("caps") == ["hi", "values", "set", "jog"] and h0.get("hi") == [2 * h0["tile"][0], 2 * h0["tile"][1]])
ck("the server counts the client (fast loop)", c.mirror_clients == 1)
faces = c1.of("faces")
ck("first faces has all 6 keys", faces and len(faces[0]["keys"]) == 6)
ck("key 1 decodes to what the dock got", faces and base64.b64decode(faces[0]["keys"]["1"]) == b"\xff\xd8FACE1")
ctl = c1.of("controls")[0] if c1.of("controls") else {}
ck("controls: encoders carry slider / kind / max", ctl.get("enc1", {}).get("slider") == "abs"
   and ctl["enc1"].get("kind") == "volume" and ctl["enc1"].get("max") == 100 and ctl["enc0"]["kind"] == "brightness"
   and ctl["key1"].get("slider") is None)
ck("an api-1 client gets no values and no 2x faces", c1.of("values") == [] and all("hi" not in f for f in faces))
ck("an api-1 client does not turn the 2x copies on", c._mirror_hi_req is False)

got.clear()
d.set_key_image(4, b"\xff\xd8FACE4"); d.flush()
pump(200)
f2 = c1.of("faces")
ck("a new frame pushes only the changed key", f2 and list(f2[-1]["keys"].keys()) == ["4"])

got.clear(); pump(200)
ck("no change -> nothing sent", got == [])

# an api-2 client that asks for sharp faces
c3 = Cli()
c3.send({"t": "hello", "client": "Orbit", "ver": "test", "api": 2, "hi": 2})
pump(300)
ck("a hi client turns the 2x copies on (applied on the loop)", c._mirror_hi_req is True and c._mirror_hi is False)
v3 = c3.of("values")
ck("api-2 client gets values", len(v3) == 1 and v3[0]["enc0"] == {"v": round(c.config.brightness / 100, 2)}
   and v3[0]["enc1"] == {"v": None, "muted": False} and v3[0]["enc2"] == {"v": None, "off": False})
ck("the 1 Hz poll asked for a quiet volume read + one monitor prime", c.engine.polls >= 1 and c.engine.primed == [None])
c.config.data["display"] = {"w": 68, "h": 68, "dx": 0, "dy": 0, "inset": 0}
c._apply_geometry()
c._service_requests()                      # the loop pass: applies the flag, re-renders the page
ck("the loop applied it: flag + tap + re-render", c._mirror_hi and images.mirror_hi() and d.mirror_hi
   and all(h is not None for h in d.mirror_full[2]))
c1.got.clear(); c3.got.clear()
pump(250)
f3 = c3.of("faces")
hi3 = {}
for f in f3:
    hi3.update(f.get("hi") or {})
ck("hi client: faces carry the 2x JPEGs (136x136)", len(hi3) == 6 and all(
    Image.open(io.BytesIO(base64.b64decode(v))).size == (136, 136) for v in hi3.values() if v) and all(hi3.values()))
ck("api-1 client: same faces, still no 'hi'", c1.of("faces") and all("hi" not in f for f in c1.of("faces")))
c3.got.clear(); pump(200)
ck("values deduped: nothing new while nothing changes", c3.of("values") == [] and c3.of("faces") == [])
c.config.brightness = 12; pump(200)
ck("a changed value is sent once", [m["enc0"] for m in c3.of("values")] == [{"v": 0.12}])
c3.send({"t": "set", "id": "enc1", "kind": "volume", "value": 0.37, "final": False},
        {"t": "set", "id": "enc1", "kind": "volume", "value": 0.38, "final": True},
        {"t": "jog", "id": "enc0", "steps": -2})
pump(200)
ck("set + jog only queued off the loop", c.engine.vol == [] and c._remote_set.get(1) == ("volume", 0.38, True)
   and len(c._remote) == 2)
c._drain_remote()
ck("then applied on the loop", c.engine.vol == [38] and c.engine.run == [knobs["enc0-"]["action"]] * 2)
c3.sock.abort(); pump(200)
ck("the hi client leaving turns the 2x copies off", c._mirror_hi_req is False)
c._service_requests()
ck("... applied on the loop", not c._mirror_hi and not images.mirror_hi() and not d.mirror_hi)

seen.clear()
c._handle_event = lambda ev: seen.append(ev.input_id)
sock.write(b'{"t":"hello","client":"test"}\n{"t":"input","id":"key5","down":true}\n{"t":"input","id":"key5","down":false}\n{"t":"wake"}\n')
sock.flush(); pump(200)
ck("client inputs queued, nothing handled off the loop", seen == [] and len(c._remote) == 2 and c._wake_requested)
c._drain_remote()
ck("then handled on the loop", seen == ["key5", "key5"])

got.clear()
c.on_input(Event("key", 0, True)); c.on_input(Event("encoder_turn", 1, delta=-1))
pump(200)
ins = [m for m in got if m["t"] == "input"]
ck("physical inputs echoed", ins == [{"t": "input", "id": "key1", "down": True}, {"t": "input", "id": "enc1", "turn": -1}])

sock.write(b"x" * (mx.MAX_LINE + 10)); sock.flush(); pump(300)
ck("an endless line drops the client", c.mirror_clients == 0)
srv.stop()
ck("stop removes the echo hook", c.on_input is None)
ck("no real config save happened", all(s in (1, 2) for s in saves))

print("\n" + ("ALL OK" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
