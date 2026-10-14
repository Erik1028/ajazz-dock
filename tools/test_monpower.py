"""Monitors off/on key ('monpower'): every monitor's panel off over DDC (D6=4, never 5), Windows
untouched; wake = D6=1, plus a display signal cycle + D6=1 again for monitors whose wake can't be
confirmed by read-back; plan saved BEFORE any change; retry-safe partial failure; toggle; startup
restore; legacy 'dim' state; live face; engine gate; controller hooks.
Runs on FAKE monitors + a temp state file — never touches the real monitors, displays or %APPDATA%."""
import os, sys, tempfile, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dock import monitors as M

fails = []
def ck(n, cond):
    print(("  ok " if cond else "FAIL ") + n)
    if not cond: fails.append(n)

cycles = []
M._signal_cycle = lambda: cycles.append(1)   # NEVER blink the real displays from a test
M._monitorpower = lambda state: (_ for _ in ()).throw(AssertionError("real SC_MONITORPOWER"))
M._nudge_mouse = lambda: (_ for _ in ()).throw(AssertionError("real mouse input"))

AOC_CAPS = "(prot(monitor)model(24G4X)vcp(10 12 D6(01 04 05) DC(00 0B)))"
XMI_CAPS = "(vcp(02 04 10 12 14(05 06))model(P24FBA-RAGL)mswhql(1))"

class PM:                                   # a fake physical monitor (the real one is a ctypes struct)
    def __init__(self, caps, vcp, honest_d6=True, wake_ok=True):
        self.caps, self.vcp, self.handle = caps, dict(vcp), None
        self.honest_d6, self.wake_ok = honest_d6, wake_ok
        self.power, self.writes = 1, []
    def read(self, code):
        if code == 0xD6 and not self.honest_d6:
            return 1                        # the Xiaomi always claims "on"
        return self.power if code == 0xD6 else self.vcp.get(code)

def fake_rig(monitors):
    M._caps_cache.clear()
    M._enum_keyed = lambda: list(monitors.items())
    M._destroy = lambda phys: None
    M._caps = lambda key, pm: pm.caps
    M._vcp_get = lambda pm, code: pm.read(code)
    def _set(pm, code, v):
        pm.writes.append((code, v))
        if code == 0xD6:
            if v == 1 and not pm.wake_ok:
                return True                 # accepted but ignored (like the left Xiaomi did)
            pm.power = v
        else:
            pm.vcp[code] = v
        return True
    M._vcp_set = _set

tmds = {"set": set()}                      # which outputs are HDMI/DVI: none until the HDMI section
M._tmds_devices = lambda: set(tmds["set"])
M._device_names = lambda: {"-1920,0": r"\\.\DISPLAY1", "0,0": r"\\.\DISPLAY2", "1920,0": r"\\.\DISPLAY3"}

tmp = tempfile.mkdtemp(prefix="monpower-test-")
M._state_path = lambda: os.path.join(tmp, "monitor_power.json")
M._power_off = None
_orig_sleep = time.sleep
M.time.sleep = lambda s: None               # no real DDC delays in the test

left = PM(XMI_CAPS, {0x10: 50, 0x12: 75}, honest_d6=False)
mid = PM(AOC_CAPS, {0x10: 50, 0x12: 50})
right = PM(XMI_CAPS, {0x10: 50, 0x12: 75}, honest_d6=False)
rig = {"-1920,0": left, "0,0": mid, "1920,0": right}
fake_rig(rig)

# ---- method choice ---------------------------------------------------------------------------
ck("AOC (D6(01 04 05)) -> power, verified", M._power_method("0,0", mid) == ("power", True))
ck("Xiaomi (no D6 in caps) -> power, blind", M._power_method("-1920,0", left) == ("power", False))
ck("D6 without 04 -> blind", M._power_method("x", PM("vcp(10 D6(01 05))", {0x10: 1})) == ("power", False))
ck("caps unreadable but brightness works -> blind", M._power_method("x", PM("", {0x10: 1})) == ("power", False))
ck("no DDC at all -> skip", M._power_method("x", PM("", {})) == ("skip", False))
ck("starts ON", M.is_off() is False)

# ---- off: plan on disk BEFORE the first write; every panel D6=4; nothing else touched ----------
seen = {}
real_set = M._vcp_set
def spy_set(pm, code, v):
    if not seen:
        seen["state"] = M._load_state()
    return real_set(pm, code, v)
M._vcp_set = spy_set
M.power_off()
M._vcp_set = real_set
ck("state file written before any DDC write", seen.get("state", {}).get("off") is True)
by = {e["key"]: e for e in M._load_state()["monitors"]}
ck("plan: AOC verified, Xiaomis blind", by["0,0"]["verify"] is True and by["-1920,0"]["verify"] is False
   and by["1920,0"]["verify"] is False and all(e["method"] == "power" for e in by.values()))
ck("all three panels got D6=4", all(p.power == 4 for p in (left, mid, right)))
ck("never D6=5 (hard off)", all((0xD6, 5) not in p.writes for p in (left, mid, right)))
ck("brightness / contrast never touched", all(c == 0xD6 for p in (left, mid, right) for c, _ in p.writes))
ck("no signal cycle when turning OFF", cycles == [])
ck("is_off after off", M.is_off() is True)
ck("second off is a no-op", M.power_off() == ["already off"])

# ---- on: D6=1 + signal cycle (blind monitors present) + D6=1 again ------------------------------
for p in (left, mid, right):
    p.writes.clear()
M.power_on()
ck("all three back on", all(p.power == 1 for p in (left, mid, right)))
ck("exactly one signal cycle", cycles == [1])
ck("Xiaomis got D6=1 before AND after the cycle", all(p.writes.count((0xD6, 1)) >= 2 for p in (left, right)))
ck("is_off False after on", M.is_off() is False and M._load_state().get("off") is False)
ck("second on is a no-op", M.power_on() == ["already on"])

# ---- the stubborn Xiaomi: ignores D6=1, woken by the signal cycle -------------------------------
left.wake_ok = False
def cycle_wakes():
    cycles.append(1)
    for p in rig.values():                  # the returning signal wakes whatever is in standby
        p.power = 1
M._signal_cycle = cycle_wakes
M.power_off(); M.power_on()
ck("left Xiaomi ignoring D6=1 is woken by the cycle", left.power == 1 and not M.is_off())
left.wake_ok = True
M._signal_cycle = lambda: cycles.append(1)

# ---- only verifiable monitors -> no screen blink -----------------------------------------------
solo = {"0,0": PM(AOC_CAPS, {0x10: 50})}
fake_rig(solo)
n = len(cycles)
M.power_off(); M.power_on()
ck("AOC-only setup: woken without a signal cycle", len(cycles) == n and solo["0,0"].power == 1)
fake_rig(rig)

# ---- verified monitor that doesn't confirm -> one retry, then never stuck at OFF ---------------
mid.wake_ok = False
M.power_off(); M.power_on()
ck("unconfirmed AOC wake keeps state OFF once", M.is_off() is True and M._load_state().get("attempts") == 1)
ck("...only the AOC stays pending", [e["key"] for e in M._load_state()["monitors"]] == ["0,0"])
ck("...the Xiaomis were woken anyway", left.power == 1 and right.power == 1)
mid.wake_ok = True
M.power_toggle()
ck("next press retries and finishes", M.is_off() is False and mid.power == 1 and "attempts" not in M._load_state())
mid.wake_ok = False
M.power_off(); M.power_on(); log = M.power_on()
ck("still unconfirmed on the 2nd try -> gives up, key usable again",
   M.is_off() is False and any("gave up" in s for s in log) and M.power_off() != ["already off"])
mid.wake_ok = True
M.power_on()
ck("...and the key keeps toggling normally", M.is_off() is False and mid.power == 1)

# ---- a monitor that vanished / moved while off never pins the key at OFF -----------------------
M.power_off()
del rig["1920,0"]
n = len(cycles)
M.power_on()
ck("missing monitor -> others on, state ON (not stuck)", left.power == 1 and mid.power == 1 and not M.is_off())
ck("...layout change forces the signal cycle", len(cycles) == n + 1)
rig["1920,0"] = right
right.power = 1
M.power_off()                               # plan: -1920,0 / 0,0 / 1920,0 -> all off
rig["3840,0"] = rig.pop("1920,0")           # the right monitor MOVED while off
right.writes.clear()
M.power_on()
ck("moved monitor (new position) still gets D6=1", right.power == 1 and (0xD6, 1) in right.writes and not M.is_off())
rig["1920,0"] = rig.pop("3840,0")
noddc = PM("", {})
rig["5760,0"] = noddc
M.power_off()
ck("a no-DDC monitor is planned as skip", {e["key"]: e["method"] for e in M._load_state()["monitors"]}["5760,0"] == "skip")
del rig["5760,0"]
M.power_on()
ck("...and a vanished skip monitor doesn't block", not M.is_off())

# ---- a saved "dim" entry is restored (brightness + contrast) ---------------------------
left.vcp.update({0x10: 0, 0x12: 0})
M._save_state({"off": True, "at": "x", "monitors": [
    {"key": "-1920,0", "model": "P24FBA-RAGL", "method": "dim", "brightness": 50, "contrast": 75}]})
M._power_off = None
ck("legacy dim state reads as OFF", M.is_off() is True)
M.power_on()
ck("legacy dim state: brightness/contrast restored", left.vcp[0x10] == 50 and left.vcp[0x12] == 75 and not M.is_off())

# ---- startup restore (app quit/crashed/rebooted while off) --------------------------------------
M.power_off()
M._power_off = None                          # fresh process: in-memory state gone, file remains
ck("fresh process reads OFF from disk", M.is_off() is True)
M.restore_if_left_off()
ck("startup restore brings them back", not M.is_off() and all(p.power == 1 for p in (left, mid, right)))
ck("startup restore with nothing to do = no writes", M.restore_if_left_off() == [])

# ---- a blind monitor on HDMI/DVI is DIMMED (DDC standby never sticks on a TMDS link) ------------
tmds["set"] = {r"\\.\DISPLAY2", r"\\.\DISPLAY3"}      # AOC + right Xiaomi on HDMI (as on Erik's PC)
right.vcp.update({0x10: 100, 0x12: 75})
for p in (left, mid, right):
    p.power = 1
    p.writes.clear()
n = len(cycles)
M.power_off()
by = {e["key"]: e for e in M._load_state()["monitors"]}
ck("HDMI: right (blind, HDMI) -> dim; AOC (verified, HDMI) + left (blind, DP) -> D6",
   by["1920,0"]["method"] == "dim" and by["0,0"]["method"] == "power" and by["-1920,0"]["method"] == "power")
ck("dim plan saved the right's own values (100 / 75)", (by["1920,0"]["brightness"], by["1920,0"]["contrast"]) == (100, 75))
ck("right: brightness + contrast 0, never a D6 write",
   right.vcp[0x10] == 0 and right.vcp[0x12] == 0 and not any(c == 0xD6 for c, _ in right.writes))
ck("left + AOC: D6=4", left.power == 4 and mid.power == 4)
M.power_on()
ck("on: right back to 100 / 75", right.vcp[0x10] == 100 and right.vcp[0x12] == 75)
ck("on: left + AOC on, signal cycle for the blind DP one", left.power == 1 and mid.power == 1 and len(cycles) == n + 1)
ck("on: state ON", not M.is_off())
M.power_off()
M._power_off = None
M.restore_if_left_off()
ck("startup restore brings the dimmed one back too", right.vcp[0x10] == 100 and right.vcp[0x12] == 75 and not M.is_off())
tmds["set"] = set()
M.power_off()
ck("output type unknown -> the blind Xiaomi falls back to D6",
   {e["key"]: e["method"] for e in M._load_state()["monitors"]}["1920,0"] == "power" and right.power == 4)
M.power_on()

# ---- live face source ---------------------------------------------------------------------------
from dock import live
ck("live ON", live.value("monitors")[:3] == ("ON", "SCREENS", 1.0) and live.source_kind("monitors") == "state")
M.power_off()
ck("live OFF", live.value("monitors")[:3] == ("OFF", "SCREENS", 0.0))
M._power_busy = True
ck("live busy shows …", live.value("monitors")[0] == "…")
M._power_busy = False
M.power_on()

from dock import images
f_on = images.live_face({"label": "Monitors", "live": {"source": "monitors"}}, "ON", "SCREENS", 1.0, "state")
f_off = images.live_face({"label": "Monitors", "live": {"source": "monitors"}}, "OFF", "SCREENS", 0.0, "state")
ck("faces render and differ", f_on.size == f_off.size and f_on.tobytes() != f_off.tobytes())

# ---- action engine: runs off-thread, a press during a switch is ignored ------------------------
from dock.actions import ActionEngine
eng = ActionEngine(None)
gate = threading.Event(); calls = []
def slow_toggle():
    calls.append(1); gate.wait(2); return ["fake"]
M.power_toggle = slow_toggle
t0 = time.perf_counter()
eng.execute({"type": "monpower", "mode": "toggle"})
ck("execute returns immediately", time.perf_counter() - t0 < 0.2)
eng.execute({"type": "monpower", "mode": "toggle"})     # pressed again while the first runs
gate.set(); _orig_sleep(0.2)
ck("second press while busy ignored", len(calls) == 1)
eng.execute({"type": "monpower"})                       # after it finished -> runs again
_orig_sleep(0.2)
ck("press after finishing runs", len(calls) == 2)

# ---- controller: caps warm-up only when a monpower key exists; quit = shutdown() --------------
from dock.config import Config, default_config
from dock.controller import DockController
warmed = []
M.warm_up = lambda: warmed.append(1)
M.restore_if_left_off = lambda: []
data = default_config()
c = DockController(Config(data))
c._monitors_startup()
ck("no monpower key -> no caps warm-up", warmed == [])
data["profiles"][0]["pages"][0]["items"]["key1"] = {"action": {"type": "monpower", "mode": "toggle"}}
c._monitors_startup()
ck("monpower key -> caps warm-up at startup", warmed == [1])

M.power_toggle = lambda: M.power_on() if M.is_off() else M.power_off()   # un-stub (engine test)
fake_rig(rig)
M.power_off()                                  # monitors dark when the user quits
held = threading.Event()
def in_flight():                               # a DDC sequence still running at quit time
    with M._power_lock:
        held.set(); _orig_sleep(0.5)
threading.Thread(target=in_flight, daemon=True).start(); held.wait(1)
t0 = time.perf_counter(); c.stop(); dt = time.perf_counter() - t0
ck("quit waits for the in-flight DDC sequence", 0.3 < dt < 5.0)
ck("quit turns the monitors back on", not M.is_off() and all(p.power == 1 for p in (left, mid, right)))
ck("after quit, switching OFF is refused", M.power_off() == ["app is quitting"] and not M.is_off())
M._closing = False

print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
