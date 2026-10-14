"""External-monitor brightness control via DDC/CI (Windows Monitor Configuration API).

Works for monitors that expose VCP brightness over DDC/CI (most modern displays).
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes, byref, POINTER, Structure

_dxva2 = ctypes.windll.dxva2
_user32 = ctypes.windll.user32


class _PhysMon(Structure):
    _fields_ = [("handle", wintypes.HANDLE), ("description", wintypes.WCHAR * 128)]


_MONENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                  POINTER(wintypes.RECT), wintypes.LPARAM)


def _enum_physical():
    hmons = []

    def _cb(hmon, hdc, lprc, lparam):
        hmons.append(hmon)
        return True

    _user32.EnumDisplayMonitors(None, None, _MONENUMPROC(_cb), 0)
    phys = []
    for h in hmons:
        n = wintypes.DWORD()
        if _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(h, byref(n)) and n.value:
            arr = (_PhysMon * n.value)()
            if _dxva2.GetPhysicalMonitorsFromHMONITOR(h, n.value, arr):
                phys.extend(arr)
    return phys


def _destroy(phys):
    for pm in phys:
        try:
            _dxva2.DestroyPhysicalMonitor(pm.handle)
        except Exception:
            pass


def _read(pm):
    mn, cur, mx = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    if _dxva2.GetMonitorBrightness(pm.handle, byref(mn), byref(cur), byref(mx)):
        return mn.value, cur.value, mx.value
    return None


def read_all():
    """Return [(index, description, min, cur, max) | (index, description, None)]."""
    phys = _enum_physical()
    try:
        out = []
        for i, pm in enumerate(phys):
            r = _read(pm)
            out.append((i, pm.description, *(r if r else (None,))))
        return out
    finally:
        _destroy(phys)


def count():
    phys = _enum_physical()
    try:
        return len(phys)
    finally:
        _destroy(phys)


def read_percent(index=None):
    """Current brightness as 0..100 for monitor `index` (None = average of all), or None.
    Normalises the monitor's native min..max range. Slow (DDC) — call off the device loop."""
    phys = _enum_physical()
    try:
        if not phys:
            return None
        if index is not None and 0 <= index < len(phys):
            phys = [phys[index]]
        vals = []
        for pm in phys:
            r = _read(pm)
            if r:
                mn, cur, mx = r
                if mx > mn:
                    vals.append((cur - mn) / (mx - mn) * 100.0)
        return int(round(sum(vals) / len(vals))) if vals else None
    finally:
        _destroy(phys)


def set_brightness(percent: int, index=None) -> None:
    """Set brightness 0..100 on monitor `index` (None = all DDC/CI monitors)."""
    percent = max(0, min(100, int(percent)))
    phys = _enum_physical()
    try:
        for i, pm in enumerate(phys):
            if index is not None and i != index:
                continue
            r = _read(pm)
            if not r:
                continue
            mn, _cur, mx = r
            _dxva2.SetMonitorBrightness(pm.handle, int(mn + (mx - mn) * percent / 100))
    finally:
        _destroy(phys)


def adjust_brightness(delta: int, index=None) -> None:
    """Nudge brightness by `delta` percent on monitor `index` (None = all)."""
    phys = _enum_physical()
    try:
        for i, pm in enumerate(phys):
            if index is not None and i != index:
                continue
            r = _read(pm)
            if not r:
                continue
            mn, cur, mx = r
            rng = (mx - mn) or 100
            cur_pct = (cur - mn) * 100.0 / rng
            _dxva2.SetMonitorBrightness(pm.handle, int(mn + rng * max(0, min(100, cur_pct + delta)) / 100))
    finally:
        _destroy(phys)


class BrightnessSession:
    """A fast burst handle for an encoder spin: enumerate the target monitors + read each one's
    native min/max range ONCE, then `set_pct` issues ONLY the DDC SET (no per-set enumeration or
    read-back — those are what made a spun encoder lag). Call close() when the burst ends."""

    def __init__(self, index=None):
        self._phys = _enum_physical()
        self.targets = []                         # [handle, mn, rng, last_pct]
        for i, pm in enumerate(self._phys):
            if index is not None and i != index:
                continue
            r = _read(pm)
            if r:
                mn, cur, mx = r
                rng = (mx - mn) or 100
                self.targets.append([pm.handle, mn, rng, (cur - mn) * 100.0 / rng])

    def current_pct(self):
        if not self.targets:
            return None
        return int(round(sum(t[3] for t in self.targets) / len(self.targets)))

    def set_pct(self, pct) -> None:
        pct = max(0.0, min(100.0, float(pct)))
        for t in self.targets:
            try:
                _dxva2.SetMonitorBrightness(t[0], int(t[1] + t[2] * pct / 100))
                t[3] = pct
            except Exception:
                pass

    def close(self) -> None:
        _destroy(self._phys)
        self._phys = []
        self.targets = []


# ---- smooth transitions -----------------------------------------------------------------------
def _collect(phys, index):
    """Snapshot the target monitors' min/max/range + current % (one enumeration for the whole fade)."""
    out = []
    for i, pm in enumerate(phys):
        if index is not None and i != index:
            continue
        r = _read(pm)
        if not r:
            continue
        mn, cur, mx = r
        rng = (mx - mn) or 100
        out.append({"pm": pm, "mn": mn, "mx": mx, "rng": rng, "start": (cur - mn) * 100.0 / rng})
    return out


def _write_pct(t, pct):
    pct = max(0.0, min(100.0, pct))
    _dxva2.SetMonitorBrightness(t["pm"].handle, int(t["mn"] + t["rng"] * pct / 100.0))


def _ramp(targets, duration, should_abort=None):
    """Ease each target from its 'start' to its 'end' percent over `duration` (smoothstep)."""
    maxd = max((abs(t["end"] - t["start"]) for t in targets), default=0.0)
    if maxd < 1.0:                                   # already there -> one direct write, no fade
        for t in targets:
            _write_pct(t, t["end"])
        return
    steps = max(2, min(12, int(round(maxd / 5.0))))
    dt = duration / steps
    for s in range(1, steps + 1):
        if should_abort and should_abort():          # a newer request arrived -> bail to it
            return
        f = s / steps
        e = f * f * (3 - 2 * f)                       # smoothstep ease-in/out
        for t in targets:
            _write_pct(t, t["start"] + (t["end"] - t["start"]) * e)
        if s < steps:
            time.sleep(dt)


def fade_brightness(percent: int, index=None, duration: float = 0.28, should_abort=None) -> None:
    """Smoothly ramp to an absolute `percent` (0..100) on monitor `index` (None = all)."""
    percent = max(0.0, min(100.0, float(percent)))
    phys = _enum_physical()
    try:
        targets = _collect(phys, index)
        for t in targets:
            t["end"] = percent
        if targets:
            _ramp(targets, duration, should_abort)
    finally:
        _destroy(phys)


def fade_adjust(delta: int, index=None, duration: float = 0.24, should_abort=None) -> None:
    """Smoothly ramp by a relative `delta` percent (each monitor from its own current)."""
    phys = _enum_physical()
    try:
        targets = _collect(phys, index)
        for t in targets:
            t["end"] = max(0.0, min(100.0, t["start"] + delta))
        if targets:
            _ramp(targets, duration, should_abort)
    finally:
        _destroy(phys)


# ---- power off / on (one key toggles all monitors) --------------------------------------------
# Every monitor's PANEL is switched off over DDC/CI (VCP 0xD6 = 4, "off (DPM)"; never 05 = hard
# off) while Windows keeps the monitor connected: the desktop is still rendered on it, so SteamVR /
# Virtual Desktop keep showing every monitor in VR. (Taking monitors out of the desktop was tried
# 2026-09-27 and rejected for exactly that reason; dimming to black wasn't "off" enough.)
# Waking is the hard part. A monitor that lists D6 with 04 in its capability string (AOC 24G4X:
# D6(01 04 05)) wakes on D6=1 and reports it, so the wake is confirmed by read-back ("verify").
# The Xiaomi P24FBA doesn't list D6: it does go off on D6=4, but its read-back always says 1 and
# one of the two ignored D6=1 (it needed its power button). For those "blind" monitors the wake is
# D6=1 + a display SIGNAL CYCLE (Windows turns every display off for ~2 s and back on; monitors
# wake on the returning signal, like after PC sleep) + D6=1 again. Tested on Erik's monitors.
# A blind monitor on HDMI/DVI can't be kept off over DDC at all: a TMDS sink turns itself back on
# when it sees an active link (the right Xiaomi did, for D6 = 2/3/4/5). Those are DIMMED instead
# ("dim": brightness + contrast 0, their own values restored) - Erik's choice 2026-09-27, after AMD's
# per-output power-off (ADL MonitorPowerState) left that monitor stuttering until power-cycled.
# The per-monitor plan is written to disk BEFORE anything changes, so a crash / reboot while "off"
# still gets undone (restore_if_left_off at startup, controller.stop on quit).
_VCP_BRIGHT, _VCP_CONTRAST, _VCP_POWER = 0x10, 0x12, 0xD6
_D6_ON, _D6_OFF = 1, 4

_power_lock = threading.Lock()          # one off/on sequence at a time
_power_off = None                       # bool once loaded from disk
_power_busy = False
_closing = False                        # app is quitting: no more switching off (shutdown())
_caps_cache: dict = {}                  # monitor key -> capability string ("" if unreadable)


def _state_path() -> str:
    from .config import config_dir
    return os.path.join(config_dir(), "monitor_power.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(data: dict) -> None:
    p = _state_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)


def _enum_keyed():
    """[(key, phys)] - key = the monitor's desktop position "x,y" (stable while the layout is)."""
    hmons = []

    def _cb(hmon, hdc, lprc, lparam):
        r = lprc.contents
        hmons.append((hmon, f"{r.left},{r.top}"))
        return True

    _user32.EnumDisplayMonitors(None, None, _MONENUMPROC(_cb), 0)
    out = []
    for h, key in hmons:
        n = wintypes.DWORD()
        if _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(h, byref(n)) and n.value:
            arr = (_PhysMon * n.value)()
            if _dxva2.GetPhysicalMonitorsFromHMONITOR(h, n.value, arr):
                for j, pm in enumerate(arr):
                    out.append((key if j == 0 else f"{key}#{j}", pm))
    return out


class _MONITORINFOEXW(Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


def _device_names() -> dict:
    """{key "x,y": Windows display device name, e.g. r'\\\\.\\DISPLAY3'}."""
    out = {}

    def _cb(hmon, hdc, lprc, lparam):
        mi = _MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        if _user32.GetMonitorInfoW(hmon, byref(mi)):
            r = lprc.contents
            out[f"{r.left},{r.top}"] = mi.szDevice
        return True

    _user32.EnumDisplayMonitors(None, None, _MONENUMPROC(_cb), 0)
    return out


# Which outputs are HDMI / DVI (Windows' own display config - vendor neutral, read-only)
class _CcdLUID(Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _CcdPathSource(Structure):
    _fields_ = [("adapterId", _CcdLUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class _CcdPathTarget(Structure):
    _fields_ = [("adapterId", _CcdLUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32),
                ("scaling", ctypes.c_uint32), ("refreshNum", ctypes.c_uint32), ("refreshDen", ctypes.c_uint32),
                ("scanLineOrdering", ctypes.c_uint32), ("targetAvailable", wintypes.BOOL),
                ("statusFlags", ctypes.c_uint32)]


class _CcdPath(Structure):
    _fields_ = [("sourceInfo", _CcdPathSource), ("targetInfo", _CcdPathTarget), ("flags", ctypes.c_uint32)]


class _CcdMode(Structure):
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", _CcdLUID),
                ("info", ctypes.c_uint64 * 6)]


class _CcdSourceName(Structure):         # DISPLAYCONFIG_SOURCE_DEVICE_NAME
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", _CcdLUID),
                ("id", ctypes.c_uint32), ("viewGdiDeviceName", wintypes.WCHAR * 32)]


_TMDS_OUTPUTS = (4, 5)                  # DISPLAYCONFIG_OUTPUT_TECHNOLOGY_DVI / _HDMI


def _tmds_devices() -> set:
    """GDI names (\\\\.\\DISPLAYn) of the active outputs on HDMI or DVI."""
    try:
        n_paths, n_modes = ctypes.c_uint32(), ctypes.c_uint32()
        if _user32.GetDisplayConfigBufferSizes(2, byref(n_paths), byref(n_modes)):   # QDC_ONLY_ACTIVE_PATHS
            return set()
        paths, modes = (_CcdPath * n_paths.value)(), (_CcdMode * n_modes.value)()
        if _user32.QueryDisplayConfig(2, byref(n_paths), paths, byref(n_modes), modes, None):
            return set()
        out = set()
        for p in paths[:n_paths.value]:
            if p.targetInfo.outputTechnology in _TMDS_OUTPUTS:
                req = _CcdSourceName()
                req.type, req.size = 1, ctypes.sizeof(req)               # ..._GET_SOURCE_NAME
                req.adapterId, req.id = p.sourceInfo.adapterId, p.sourceInfo.id
                if _user32.DisplayConfigGetDeviceInfo(byref(req)) == 0:
                    out.add(req.viewGdiDeviceName)
        return out
    except Exception:
        return set()


def _vcp_get(pm, code):
    ty, cur, mx = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    if _dxva2.GetVCPFeatureAndVCPFeatureReply(pm.handle, code, byref(ty), byref(cur), byref(mx)):
        return cur.value
    return None


def _vcp_set(pm, code, value) -> bool:
    ok = bool(_dxva2.SetVCPFeature(pm.handle, code, int(value)))
    time.sleep(0.06)                    # DDC/CI wants a gap between commands
    return ok


def _caps(key, pm) -> str:
    if key not in _caps_cache:
        caps = ""
        n = wintypes.DWORD()
        if _dxva2.GetCapabilitiesStringLength(pm.handle, byref(n)) and n.value:
            buf = ctypes.create_string_buffer(n.value)
            if _dxva2.CapabilitiesRequestAndCapabilitiesReply(pm.handle, buf, n.value):
                caps = buf.value.decode("ascii", "replace")
        _caps_cache[key] = caps
    return _caps_cache[key]


def _power_method(key, pm):
    """("power", verify) for a DDC/CI monitor - verify = its D6 wake can be confirmed by
    read-back (it lists D6 with 04); ("skip", False) for a monitor without DDC/CI."""
    caps = _caps(key, pm).upper()
    i = caps.find("D6(")
    if i >= 0 and "04" in caps[i + 3:caps.find(")", i)].split():
        return "power", True
    if caps or _vcp_get(pm, _VCP_BRIGHT) is not None:
        return "power", False
    return "skip", False


def _model(caps: str) -> str:
    i = caps.find("model(")
    return caps[i + 6:caps.find(")", i)] if i >= 0 else ""


# ---- display signal cycle (wakes monitors that ignore the DDC wake command) --------------------
_PostMessageW = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)(("PostMessageW", _user32))
_SendMessageTimeoutW = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, POINTER(ctypes.c_size_t))(("SendMessageTimeoutW", _user32))
_FindWindowW = ctypes.WINFUNCTYPE(wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)(("FindWindowW", _user32))


class _MOUSEINPUT(Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSE_INPUT(Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("_pad", ctypes.c_byte * 32)]   # INPUT union = 32 bytes
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _monitorpower(state: int) -> None:
    """WM_SYSCOMMAND / SC_MONITORPOWER (2 = off, -1 = on), SENT to one responsive top-level window -
    Explorer's desktop ("Progman"); its DefWindowProc applies it system-wide. Not a broadcast: SENT
    it waits on every unresponsive window (~2 s each here -> a 15 s wake), POSTED a slow window may
    process a late 'off' AFTER the wake. Posted broadcast only if there's no desktop window."""
    hwnd = _FindWindowW("Progman", None)
    if hwnd:
        res = ctypes.c_size_t()
        if _SendMessageTimeoutW(hwnd, 0x0112, 0xF170, state, 0x0002, 3000, byref(res)):  # SMTO_ABORTIFHUNG
            return
    _PostMessageW(0xFFFF, 0x0112, 0xF170, state)


def _nudge_mouse() -> None:
    for dx in (1, -1):                  # a 1 px move and back: user input wakes the displays
        inp = _MOUSE_INPUT(type=0)      # INPUT_MOUSE
        inp.mi = _MOUSEINPUT(dx, 0, 0, 0x0001, 0, 0)            # MOUSEEVENTF_MOVE (relative)
        _user32.SendInput(1, byref(inp), ctypes.sizeof(inp))


def _signal_cycle() -> None:
    """Every display off for ~2 s, then back on - the returning signal wakes a monitor that is in
    DDC standby but ignores D6=1. Only used when turning ON (a blink is harmless then)."""
    _monitorpower(2)
    time.sleep(2.0)
    _nudge_mouse()
    _monitorpower(-1)
    _nudge_mouse()
    time.sleep(1.5)


def is_off() -> bool:
    """True while the monitors are switched off by power_off() (persisted across restarts)."""
    global _power_off
    if _power_off is None:
        _power_off = bool(_load_state().get("off"))
    return _power_off


def is_busy() -> bool:
    return _power_busy


def warm_up() -> None:
    """Read the capability strings now (~1 s per monitor, blocking) so the first press is quick."""
    with _power_lock:
        phys = _enum_keyed()
        try:
            for key, pm in phys:
                _caps(key, pm)
        finally:
            _destroy([pm for _k, pm in phys])


def shutdown(timeout: float = 20.0) -> list:
    """App quit: refuse any further switching off, let a sequence already running finish (killing
    a thread inside a dxva2 call at process exit is an access violation), then turn the monitors
    back on if they're off - the app never quits leaving them dark."""
    global _closing
    _closing = True
    if not _power_lock.acquire(timeout=timeout):
        return ["still busy"]
    _power_lock.release()
    return power_on() if is_off() else []


def power_off() -> list:
    """Switch every monitor's panel off (Windows keeps them connected). Returns a log."""
    global _power_off, _power_busy
    log = []
    with _power_lock:
        if _closing:
            return ["app is quitting"]
        if is_off():
            return ["already off"]
        _power_busy = True
        phys = _enum_keyed()
        try:
            tmds = None
            plan = []
            for key, pm in phys:
                method, verify = _power_method(key, pm)
                entry = {"key": key, "model": _model(_caps(key, pm)), "method": method, "verify": verify}
                if method == "power" and not verify:        # blind: on HDMI/DVI it won't stay off
                    if tmds is None:
                        names = _device_names()
                        tmds = {k for k, dev in names.items() if dev in _tmds_devices()}
                    if key in tmds:
                        entry.update(method="dim", brightness=_vcp_get(pm, _VCP_BRIGHT),
                                     contrast=_vcp_get(pm, _VCP_CONTRAST))
                plan.append((entry, pm))
            _save_state({"off": True, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "monitors": [e for e, _pm in plan]})      # the plan (+ originals) on disk FIRST
            _power_off = True
            for entry, pm in plan:
                if entry["method"] == "dim":
                    ok = _vcp_set(pm, _VCP_BRIGHT, 0)
                    if entry.get("contrast") is not None:
                        ok = _vcp_set(pm, _VCP_CONTRAST, 0) and ok
                else:
                    ok = entry["method"] == "power" and _vcp_set(pm, _VCP_POWER, _D6_OFF)
                log.append(f"{entry['model'] or entry['key']}: {entry['method']} off ok={ok}")
        finally:
            _destroy([pm for _k, pm in phys])
            _power_busy = False
    return log


def power_on() -> list:
    """Undo power_off(): D6=1 everywhere, a signal cycle for monitors that can't confirm it."""
    global _power_off, _power_busy
    log = []
    with _power_lock:
        state = _load_state()
        if not state.get("off"):
            _power_off = False
            return ["already on"]
        _power_busy = True
        phys = dict(_enum_keyed())
        try:
            # 'skip' monitors (no DDC/CI) were never touched -> nothing to undo, never block on them
            entries = [e for e in state.get("monitors") or [] if e.get("method") in ("power", "dim")]
            planned = {e.get("key") for e in entries}
            live = [(e, phys[e.get("key")]) for e in entries if e.get("key") in phys]
            missing = [e for e in entries if e.get("key") not in phys]
            for e in missing:
                log.append(f"{e.get('model') or e.get('key')}: not at {e.get('key')} any more")
            powered = [pm for e, pm in live if e.get("method") == "power"]
            blind = [pm for e, pm in live if e.get("method") == "power" and not e.get("verify")]
            if missing:         # the layout changed while off: a monitor may have MOVED -> wake every
                extra = [pm for key, pm in phys.items() if key not in planned]   # monitor there is
                powered += extra
                blind += extra
            for pm in powered:                          # 1) the plain wake command
                _vcp_set(pm, _VCP_POWER, _D6_ON)
            if blind:                                   # 2) can't confirm those -> signal cycle
                _signal_cycle()
                for pm in blind:                        # 3) + the command once more
                    _vcp_set(pm, _VCP_POWER, _D6_ON)
            pending = []
            for entry, pm in live:
                ok = True
                if entry.get("method") == "power" and entry.get("verify"):
                    ok = False
                    for _try in range(5):               # waking takes a moment; confirm read-back
                        time.sleep(0.4)
                        if _vcp_get(pm, _VCP_POWER) == _D6_ON:
                            ok = True
                            break
                        _vcp_set(pm, _VCP_POWER, _D6_ON)
                elif entry.get("method") == "dim":      # blind HDMI/DVI monitor: its own values back
                    for code, k in ((_VCP_BRIGHT, "brightness"), (_VCP_CONTRAST, "contrast")):
                        v = entry.get(k)
                        if v is None:
                            continue
                        for _try in range(3):
                            _vcp_set(pm, code, v)
                            if _vcp_get(pm, code) == v:
                                break
                        else:
                            ok = False
                if not ok:
                    pending.append(entry)
                log.append(f"{entry.get('model') or entry.get('key')}: {entry.get('method')} on ok={ok}")
            attempts = int(state.get("attempts") or 0) + 1
            if pending and attempts < 2:
                # a monitor that reports its state says it's still off: stay OFF so the next press
                # retries just that one - but only once, the key must never get stuck at OFF
                _save_state({"off": True, "at": state.get("at"), "attempts": attempts,
                             "monitors": pending})
            else:
                if pending:
                    log.append("gave up on: " + ", ".join(e.get("model") or e.get("key") for e in pending))
                state["off"] = False
                state.pop("attempts", None)
                state["restored"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _save_state(state)
                _power_off = False
        finally:
            _destroy(list(phys.values()))
            _power_busy = False
    return log


def power_toggle() -> list:
    return power_on() if is_off() else power_off()


def restore_if_left_off() -> list:
    """At startup: if the app went away while the monitors were off (crash, reboot), bring them back."""
    return power_on() if _load_state().get("off") else []
