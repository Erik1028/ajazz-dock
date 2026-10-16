"""Smart-light / RGB-scene actions + app-aware auto-switching (no real devices touched)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AJAZZDOCK_CONFIG", os.path.join(os.environ.get("TEMP", "/tmp"), "_int_cfg.json"))

from dock import actions, tapo          # import tapo (-> asyncio) early, before any Popen mocking
from dock.actions import ActionEngine
from dock.config import Config, default_config, _migrate
from dock.apppoller import ForegroundPoller

# ---- 1) rgbscene CLI argument construction --------------------------------------------------
e = ActionEngine(None)
assert e._rgb_args("color", {"color": "#FF0000"}) == ["--effect", "static", "--color", "FF0000"]
assert e._rgb_args("effect", {"effect": "Breathing"}) == ["--effect", "Breathing"]
assert e._rgb_args("profile", {"profile": "Gaming"}) == ["--profile", "Gaming"]
assert e._rgb_args("profile", {}) is None
assert e._rgb_args("toggle", {}) == ["--toggle"]
assert e._rgb_args("off", {}) == ["--off"]
assert e._rgb_args("bright_set", {"brightness": 55}) == ["--brightness", "55"]
assert e._rgb_args("bright_set", {"brightness": 250}) == ["--brightness", "100"]   # clamps
# relative brightness is NOT a one-shot arg — it routes through the coalescing dimmer
assert e._rgb_args("bright_up", {"step": 10}) is None
print("OK rgb_args")

# ---- 1b) _proc_running accepts a single name OR a tuple (Prisma/RGBCommander) -----------------
assert actions._proc_running(("definitely_not_a_proc_xyz.exe", "also_not.exe")) is False
print("OK proc_running tuple")

# ---- 1c) RGB brightness encoder ticks COALESCE into one `--brightness +N` --------------------
import dock.actions as _A
rgb_calls = []
_A._proc_running = lambda name: True                     # pretend Prisma is already running
_A.subprocess.Popen = lambda argv, *a, **k: rgb_calls.append(argv)
ev = ActionEngine(None)
for _ in range(8):
    ev._rgbscene({"mode": "bright_up", "exe": "Prisma.exe", "step": 5})
time.sleep(0.5)
flushes = [c for c in rgb_calls if "--brightness" in c]
assert flushes, ("dimmer must send a --brightness command", rgb_calls)
assert len(flushes) <= 3, ("8 ticks must coalesce into a few sends", flushes)
assert flushes[-1][-1].startswith("+"), ("relative up -> +N", flushes[-1])
print(f"OK rgb brightness coalesced: 8 ticks -> {len(flushes)} send(s) ({flushes[-1]})")

# ---- 2) dispatch routes the new types -------------------------------------------------------
seen = []
e._smartlight = lambda a: seen.append(("light", a))
e._rgbscene = lambda a: seen.append(("rgb", a))
e.execute({"type": "smartlight", "mode": "toggle"})
e.execute({"type": "rgbscene", "mode": "off"})
assert [s[0] for s in seen] == ["light", "rgb"], seen
print("OK dispatch")

# ---- 3) handler request/command shapes (mocked transport, real handler code) ----------------
# hex -> hsv conversion (used by the colour mode)
assert actions._hex_to_hsv("#FF0000") == (0, 100, 100), actions._hex_to_hsv("#FF0000")
assert actions._hex_to_hsv("00FF00") == (120, 100, 100)

# smartlight drives the bulb directly via dock.tapo.apply; mock it + the credentials
from dock import tapo
tapo.tapo_creds = lambda config: ("e@x.com", "pw")
applied = []
nudged = []
tapo.apply = lambda host, user, pw, mode, hsv=None, brightness=None, step=None: applied.append(
    (host, mode, hsv, brightness, step))
tapo.nudge = lambda host, user, pw, kind, delta: nudged.append((host, kind, delta))


class _Ctl:
    class config:
        data = {}
e3 = ActionEngine(_Ctl())
e3._smartlight({"mode": "on", "host": "192.0.2.10"}); time.sleep(0.3)
e3._smartlight({"mode": "color", "host": "192.0.2.10", "color": "FF0000", "brightness": 60}); time.sleep(0.3)
assert ("192.0.2.10", "on", None, None, None) in applied, applied
assert ("192.0.2.10", "color", (0, 100, 60), 60, None) in applied, applied
# relative encoder modes are non-blocking -> go through the coalescing nudge(), not apply()
e3._smartlight({"mode": "brightness_up", "host": "192.0.2.10", "step": 10})
e3._smartlight({"mode": "brightness_down", "host": "192.0.2.10", "step": 15})
e3._smartlight({"mode": "hue_down", "host": "192.0.2.10", "step": 30})
assert ("192.0.2.10", "bri", 10) in nudged, nudged
assert ("192.0.2.10", "bri", -15) in nudged, nudged
assert ("192.0.2.10", "hue", -30) in nudged, nudged

e2 = ActionEngine(None)
popens = []
actions._proc_running = lambda name: True
actions.subprocess.Popen = lambda argv, *a, **k: popens.append(argv)
e2._rgbscene({"mode": "color", "color": "00C8AA"}); time.sleep(0.2)
assert popens and popens[-1][1:] == ["--minimized", "--effect", "static", "--color", "00C8AA"], popens
# ^ --minimized on every CLI send: a running Prisma strips it before pipe-forwarding (Program.cs:136);
#   on a stale run-cache miss the launch becomes a minimized primary instead of popping its window.
print("OK handler shapes")

# ---- 4) config defaults + migration ---------------------------------------------------------
d = default_config()
assert d["auto_switch"] is False and d["app_rules"] == []
old = {"profiles": default_config()["profiles"]}
_migrate(old)
assert "auto_switch" in old and "app_rules" in old
print("OK config")

# ---- 5) poller rule matching (case-insensitive) ---------------------------------------------
data = {"app_rules": [{"app": "obs64.exe", "profile": "OBS", "page": None}]}
assert ForegroundPoller._match("obs64.exe", data)
assert ForegroundPoller._match("chrome.exe", data) is None
print("OK match")

# ---- 6) controller switch + suppression -----------------------------------------------------
import dock.controller


class FakeDock:
    image_rotation, image_mirror = 90, False
    def set_key_pil(self, *a, **k): pass
    def set_key_image(self, *a, **k): pass
    def flush(self): pass


data = default_config()
data["profiles"].append({"name": "OBS", "globals": {}, "folders": {},
                         "pages": [{"name": "A", "items": {}}, {"name": "B", "items": {}},
                                   {"name": "C", "items": {}}]})
c = dock.controller.DockController(Config(data))
c.dock = FakeDock(); c.connected = True
c.request_app_switch("OBS", None)
assert c._pending_switch == ("OBS", None)
c._pending_switch = None
c._apply_app_switch("OBS", None)
assert c.config.data["active_profile"] == "OBS"
c._apply_app_switch(None, 2)
assert c._pending_page is not None and c._pending_page[2] == 2   # _pending_page = (folder, direction, idx)
# manual-nav suppression
c._pending_page = None; c._last_manual_nav = time.time()
c._apply_app_switch(None, 0)
assert c._pending_page is None
# folder suppression
c._last_manual_nav = 0; c._folder = "f1"
c._apply_app_switch("Default", None)
assert c.config.data["active_profile"] == "OBS"
print("OK switch + suppression")

print("\nRESULT: ALL PASS")
