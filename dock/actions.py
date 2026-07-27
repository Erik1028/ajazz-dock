"""Action engine: execute a binding's action (launch/hotkey/media/volume/mic/nav)."""
from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

# `keyboard` (SendInput-based; no admin needed) is imported lazily inside the few functions that
# emit keys — it's only needed when an action fires, so it stays out of the startup import chain.

# ---- name maps -------------------------------------------------------------
_HOTKEY_ALIASES = {
    "win": "windows", "cmd": "windows", "super": "windows", "meta": "windows",
    "opt": "alt", "option": "alt", "return": "enter", "esc": "escape",
    "del": "delete", "ins": "insert", "pgup": "page up", "pgdn": "page down",
}
_MEDIA_KEYS = {
    "play_pause": "play/pause media", "play": "play/pause media",
    "pause": "play/pause media", "next": "next track", "prev": "previous track",
    "previous": "previous track", "stop": "stop media",
}
_VOLUME_KEYS = {"up": "volume up", "down": "volume down", "mute": "volume mute"}

# Calm brand accent for non-volume HUDs (brightness / RGB / bulb); volume HUDs use the heat ramp.
_HUD_MINT = "#35e08a"


import queue as _queue


class _ComAudio:
    """The SINGLE owner thread for all transient pycaw/comtypes audio COM.

    A comtypes IUnknown.Release() from a thread other than the object's creator is a native access
    violation (STA or MTA alike — verified). So every short-lived audio COM object created by
    _system_volume() / _AppVolume is created, USED and cyclically COLLECTED on this one thread, which
    also runs the process's gc.collect() (threshold GC is disabled everywhere else). Cross-thread
    release can therefore never happen. The mic endpoint is cached (never garbage) so it is exempt."""
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._q = _queue.Queue()
        threading.Thread(target=self._run, name="com-audio", daemon=True).start()

    @classmethod
    def get(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = _ComAudio()
        return cls._instance

    def _run(self):
        import gc
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        last_gc = time.monotonic()
        while True:
            try:
                job = self._q.get(timeout=2.0)
            except _queue.Empty:
                job = None
            if job is not None:
                fn, box = job
                try:
                    box[0] = fn()
                except Exception as e:
                    box[1] = e
                finally:
                    box[2].set()
            now = time.monotonic()
            if now - last_gc >= 2.0:               # bound COM garbage: collect on THIS thread ~2s
                try:
                    gc.collect()
                except Exception:
                    pass
                last_gc = now

    def call(self, fn, timeout=3.0):
        box = [None, None, threading.Event()]
        self._q.put((fn, box))
        return box[0] if box[2].wait(timeout) else None


def _audio(fn, timeout=3.0):
    """Run a pycaw/COM callable on the single com-audio thread; returns its result (None on error or
    timeout). Confines every audio COM object to that one thread so GC can release it safely there."""
    return _ComAudio.get().call(fn, timeout)


_sysvol_chain = None          # (enum, dev, iface, ep, dev_id) kept ALIVE so the endpoint is never Released
_sysvol_retired = []          # chains retired after a device change — kept alive too (see below)


def _system_volume():
    """(percent 0..100, muted) of the default output device, or None if it can't be read.

    Grabs the raw default render endpoint (eRender=0, eConsole=0) via the MMDevice enumerator, like
    _Mic's fallback. CRITICAL: the endpoint is CACHED and a pycaw audio COM object is NEVER released
    — comtypes double-Releases the aliased `cast()` interface pointer when it is freed (by refcount
    OR by the cyclic GC), which is a native access violation (crash.log: dumps while adjusting volume
    fast). The mic never crashed because its endpoint is likewise cached forever. So we keep every
    endpoint alive for the process lifetime; a device change just retires the old chain (never frees
    it). Reads reuse the cached endpoint -> zero new COM objects per call -> nothing for GC to free."""
    def _impl():
        global _sysvol_chain
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL, CoCreateInstance
        from pycaw.constants import CLSID_MMDeviceEnumerator
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.pycaw import IAudioEndpointVolume
        for _ in (1, 2):                                  # one rebuild if the cached device vanished
            try:
                enum = _sysvol_chain[0] if _sysvol_chain else \
                    CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
                # Re-check the CURRENT default endpoint every read: the default output changes at
                # runtime (wireless headphones connect/disconnect, manual switch). A still-valid but
                # no-longer-default cached endpoint returns a STALE volume WITHOUT raising, so the HUD
                # freezes on the old device's level. Rebuild when the default device id changes.
                dev = enum.GetDefaultAudioEndpoint(0, 0)         # eRender, eConsole = default speakers
                dev_id = dev.GetId()
                if _sysvol_chain is None or _sysvol_chain[4] != dev_id:
                    if _sysvol_chain is not None:
                        _sysvol_retired.append(_sysvol_chain)    # retire, don't free (double-Release)
                    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    _sysvol_chain = (enum, dev, iface, cast(iface, POINTER(IAudioEndpointVolume)), dev_id)
                ep = _sysvol_chain[3]
                return int(round(ep.GetMasterVolumeLevelScalar() * 100)), bool(ep.GetMute())
            except Exception:
                if _sysvol_chain is not None:
                    _sysvol_retired.append(_sysvol_chain)        # retire, don't free (double-Release)
                    _sysvol_chain = None
        return None
    return _audio(_impl)

# "Quick actions": common Windows tasks as one-press presets. Most are really just a
# standard system shortcut; the rest (recycle bin, clipboard, settings) need a real call.
_QUICK_HOTKEYS = {
    "show_desktop": "windows+d", "minimize_all": "windows+m",
    "task_manager": "ctrl+shift+esc", "explorer": "windows+e",
    "run_dialog": "windows+r", "snip": "windows+shift+s",
    "clipboard_history": "windows+v", "emoji_panel": "windows+.",
    "project": "windows+p",
}


def _normalize_hotkey(spec: str) -> str:
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    return "+".join(_HOTKEY_ALIASES.get(p, p) for p in parts)


# ---- integrations: Tapo bulb (direct, via python-kasa) + Prisma (RGB) -----
# The bulb is driven DIRECTLY over the LAN (no Lumos app needed); Prisma over its
# Stream-Deck-style CLI (a launch with args forwards to the running instance).
_TAPO_DEFAULT_HOST = "192.168.0.87"          # the user's Tapo L630
_RGB_EXE = r"C:\Users\Erik\Desktop\project\RGBCommander\dist\RGBCommander.exe"
_RGB_PROC_NAMES = ("Prisma.exe", "RGBCommander.exe")   # the RGB app (renamed Prisma; old name kept)


def _hex_to_hsv(hexcol: str):
    """'#RRGGBB' -> (hue 0-360, sat 0-100, val 0-100)."""
    import colorsys
    c = (hexcol or "").lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    return round(h * 360), round(s * 100), round(v * 100)


def _proc_running(name) -> bool:
    try:
        import psutil
    except Exception:
        return True                          # can't check -> assume up (avoid double-launch)
    names = {name.lower()} if isinstance(name, str) else {n.lower() for n in name}
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() in names:
                return True
        except Exception:
            pass
    return False


_rgb_run_cache = [0.0, False]                # [checked_at, running] — throttle _proc_running on dial spins


def _rgb_proc_running() -> bool:
    """Is the RGB helper (Prisma) up? Cached ~3s so a fast encoder spin doesn't re-enumerate every
    process on each 0.12s coalesce window; the dimmers refresh it to True right after they launch it."""
    now = time.monotonic()
    if now - _rgb_run_cache[0] < 3.0:
        return _rgb_run_cache[1]
    _rgb_run_cache[0], _rgb_run_cache[1] = now, _proc_running(_RGB_PROC_NAMES)
    return _rgb_run_cache[1]


class _DiscordVol:
    """Coalesces encoder ticks for Discord mic/output volume: one worker accumulates the signed
    delta and applies it via the RPC pipe per rate-limit window, so a fast spin doesn't flood it."""

    def __init__(self, controller=None):
        self.controller = controller
        self._d_in = 0
        self._d_out = 0
        self._lock = threading.Lock()
        self._ev = threading.Event()
        self._thread = None

    def nudge(self, which: str, delta: int) -> None:
        with self._lock:
            if which == "in":
                self._d_in += int(delta)
            else:
                self._d_out += int(delta)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="discord-vol", daemon=True)
                self._thread.start()
        self._ev.set()

    def _run(self) -> None:
        from . import discord as dc, live
        while True:
            self._ev.wait()
            self._ev.clear()
            with self._lock:
                di, self._d_in = self._d_in, 0
                do, self._d_out = self._d_out, 0
            if di == 0 and do == 0:
                continue
            try:
                hud = None
                if di:
                    nv = dc.nudge_input(di)
                    live.set_discord_volume(inp=nv)
                    hud = (nv, "Discord (mic)", 100)         # Discord input volume is 0..100
                if do:
                    nv = dc.nudge_output(do)
                    live.set_discord_volume(out=nv)
                    hud = (nv, "Discord", 200)               # output (others' volume) is 0..200
                if self.controller is not None:
                    if hud and hasattr(self.controller, "show_value_hud"):
                        self.controller.show_value_hud(hud[0], hud[1], vmax=hud[2])
                    if hasattr(self.controller, "refresh_live"):
                        self.controller.refresh_live()
            except getattr(dc, "NeedsAuth", Exception):
                pass
            except Exception as e:
                print(f"[discord-vol] {e}")
            time.sleep(0.09)          # rate-limit; coalesce ticks landing during this window


class _ObsVolume:
    """Coalesces encoder ticks for an OBS input's volume (0..1 multiplier): one worker applies a
    single SetInputVolume per rate-limit window and pushes the on-screen HUD. Push = mute toggle."""

    def __init__(self, controller=None):
        self.controller = controller
        self._lock = threading.Lock()
        self._ev = threading.Event()
        self._pending: Dict[str, int] = {}     # input -> accumulated step (percent points)
        self._mul: Dict[str, float] = {}       # input -> last-known volume mul
        self._muted: Dict[str, bool] = {}      # input -> last-known mute state
        self._thread = None

    def _hud(self, inp):
        if self.controller is not None and hasattr(self.controller, "show_volume_hud"):
            pct = int(round(max(0.0, min(1.0, self._mul.get(inp, 0.0))) * 100))
            self.controller.show_volume_hud(pct, bool(self._muted.get(inp)), inp)

    def nudge(self, inp: str, delta_pct: int) -> None:
        with self._lock:
            self._pending[inp] = self._pending.get(inp, 0) + int(delta_pct)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="obs-vol", daemon=True)
                self._thread.start()
        self._ev.set()

    def mute(self, inp: str) -> None:
        def work():
            try:
                from . import obs
                self._muted[inp] = obs.toggle_input_mute(inp)
                if inp not in self._mul:
                    m = obs.input_volume_mul(inp)
                    if m is not None:
                        self._mul[inp] = m
                self._hud(inp)
            except Exception as e:
                print(f"[obs-vol] {e}")
        threading.Thread(target=work, name="obs-mute", daemon=True).start()

    def _run(self) -> None:
        from . import obs
        while True:
            self._ev.wait()
            self._ev.clear()
            with self._lock:
                pend, self._pending = self._pending, {}
            pend = {k: v for k, v in pend.items() if v}
            if not pend:
                continue
            try:
                for inp, dp in pend.items():
                    cur = self._mul.get(inp)
                    if cur is None:
                        cur = obs.input_volume_mul(inp)
                        cur = 1.0 if cur is None else cur
                        self._muted[inp] = obs.input_muted(inp)
                    nv = max(0.0, min(1.0, cur + dp / 100.0))
                    self._mul[inp] = nv
                    obs.set_input_volume_mul(inp, nv)
                    self._hud(inp)
            except Exception as e:
                print(f"[obs-vol] {e}")
            time.sleep(0.09)          # rate-limit; coalesce ticks landing during this window


class _RgbDimmer:
    """Coalesces encoder brightness ticks for Prisma: one worker thread accumulates the signed delta
    and fires a single `--brightness +N` per rate-limit window (Prisma resolves +/- against its own
    brightness bar), so a fast spin never launches a process per click."""

    def __init__(self):
        self._delta = 0
        self._exe = _RGB_EXE
        self._lock = threading.Lock()
        self._ev = threading.Event()
        self._thread = None

    def nudge(self, exe: str, delta: int) -> None:
        with self._lock:                          # restart inside the lock (no TOCTOU on _thread)
            self._delta += int(delta)
            self._exe = exe or self._exe
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="rgb-dimmer", daemon=True)
                self._thread.start()
        self._ev.set()

    def _run(self) -> None:
        while True:
            self._ev.wait()
            self._ev.clear()
            with self._lock:
                d, self._delta = self._delta, 0
                exe = self._exe
            if d == 0:
                continue
            try:
                if not _rgb_proc_running():
                    subprocess.Popen([exe, "--minimized"])
                    time.sleep(1.8)
                    _rgb_run_cache[0], _rgb_run_cache[1] = time.monotonic(), True
                sign = "+" if d > 0 else "-"
                # Always pass --minimized: a running instance strips it before pipe-forwarding
                # (Program.cs:136), and if the 3s run-cache was stale (Prisma just died) the
                # misfired launch becomes a minimized primary instead of popping its window mid-spin.
                subprocess.Popen([exe, "--minimized", "--brightness", f"{sign}{abs(d)}"])
            except Exception as e:
                print(f"[rgbscene] {e}")
            time.sleep(0.12)          # rate-limit; coalesce ticks landing during this window


def _hsv_to_hex(h, s=100, v=100):
    import colorsys
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360.0, s / 100.0, v / 100.0)
    return f"{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"


class _RgbHue:
    """Coalesces encoder colour-cycle ticks for Prisma: accumulate a signed hue delta and fire ONE
    `--effect static --color <hex>` per rate-limit window (we own a local hue since Prisma can't
    report its colour), so spinning the dial scrolls colours without a process launch per click."""

    def __init__(self):
        self._delta = 0
        self._hue = 0.0
        self._exe = _RGB_EXE
        self._lock = threading.Lock()
        self._ev = threading.Event()
        self._thread = None

    def nudge(self, exe: str, delta: int) -> None:
        with self._lock:
            self._delta += int(delta)
            self._exe = exe or self._exe
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="rgb-hue", daemon=True)
                self._thread.start()
        self._ev.set()

    def _run(self) -> None:
        while True:
            self._ev.wait()
            self._ev.clear()
            with self._lock:
                d, self._delta = self._delta, 0
                exe = self._exe
            if d == 0:
                continue
            self._hue = (self._hue + d) % 360
            hexc = _hsv_to_hex(self._hue)
            try:
                if not _rgb_proc_running():
                    subprocess.Popen([exe, "--minimized"])
                    time.sleep(1.8)
                    _rgb_run_cache[0], _rgb_run_cache[1] = time.monotonic(), True
                subprocess.Popen([exe, "--minimized", "--effect", "static", "--color", hexc])   # see dimmer note
            except Exception as e:
                print(f"[rgbscene] {e}")
            time.sleep(0.12)          # rate-limit; coalesce ticks landing during this window


# ---- layout-independent scan-code sending ----------------------------------
# Some apps (e.g. Substance 3D Painter) bind shortcuts to PHYSICAL key positions. On a
# non-US layout (Hungarian QWERTZ: '[' needs AltGr, digits need Shift) `keyboard.send`
# sends the character, which never matches. Sending the raw Set-1 scan code presses the
# physical key directly, regardless of layout.
import ctypes
from ctypes import wintypes

_SCAN = {
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06, "6": 0x07, "7": 0x08,
    "8": 0x09, "9": 0x0A, "0": 0x0B, "minus": 0x0C, "equals": 0x0D, "tab": 0x0F,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15, "u": 0x16,
    "i": 0x17, "o": 0x18, "p": 0x19, "[": 0x1A, "]": 0x1B, "enter": 0x1C,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23, "j": 0x24,
    "k": 0x25, "l": 0x26, "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30,
    "n": 0x31, "m": 0x32, "space": 0x39, "esc": 0x01, "escape": 0x01,
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F, "f6": 0x40,
    "f7": 0x41, "f8": 0x42, "f9": 0x43, "f10": 0x44, "f11": 0x57, "f12": 0x58,
}
_SCAN_MODS = {"ctrl": 0x1D, "control": 0x1D, "alt": 0x38, "shift": 0x2A,
              "win": 0x5B, "windows": 0x5B}
_SCAN_EXT = {0x5B}


class _KBDIN(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KBDIN)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _scan_event(code: int, down: bool) -> None:
    flags = 0x0008                                   # KEYEVENTF_SCANCODE
    if code in _SCAN_EXT:
        flags |= 0x0001                              # KEYEVENTF_EXTENDEDKEY
    if not down:
        flags |= 0x0002                              # KEYEVENTF_KEYUP
    inp = _INPUT(type=1)                              # INPUT_KEYBOARD
    inp.ki = _KBDIN(0, code, flags, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


# All key/scan-code emission is serialised through one re-entrant lock: a macro runs on the
# device-loop thread while plain hotkey/text actions run on the off-loop worker, and both drive the
# global `keyboard` module (shared modifier state) — without this their down/up events can interleave
# and leak a held modifier (e.g. Ctrl stuck down).
_KBD_LOCK = threading.RLock()


def _kb_send(spec) -> None:
    import keyboard
    with _KBD_LOCK:
        keyboard.send(spec)


def _kb_write(text, **kw) -> None:
    import keyboard
    with _KBD_LOCK:
        keyboard.write(text, **kw)


def send_scancode_combo(spec: str) -> bool:
    """Press a hotkey by physical scan code. False (no-op) if any token is unmapped."""
    parts = [_HOTKEY_ALIASES.get(p.strip().lower(), p.strip().lower())
             for p in spec.split("+") if p.strip()]
    mods = [_SCAN_MODS[p] for p in parts if p in _SCAN_MODS]
    keys = []
    for p in parts:
        if p in _SCAN_MODS:
            continue
        sc = _SCAN.get(p)
        if sc is None:
            return False
        keys.append(sc)
    if not keys:
        return False
    with _KBD_LOCK:                        # whole press→release sequence is one critical section
        for sc in mods:
            _scan_event(sc, True)
        for sc in keys:
            _scan_event(sc, True)
        time.sleep(0.012)
        for sc in reversed(keys):
            _scan_event(sc, False)
        for sc in reversed(mods):
            _scan_event(sc, False)
    return True


# ---- microphone (Core Audio via pycaw) -------------------------------------
_mic_retired = []          # invalidated capture endpoints kept alive (never freed -> no double-Release)


class _Mic:
    """Lazily-bound default-communications capture endpoint volume control."""

    _EDATAFLOW_CAPTURE = 1
    _EROLE_COMMUNICATIONS = 2

    def __init__(self) -> None:
        self._endpoint = None
        self._dev_id = None

    def _ensure(self):
        import comtypes
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioEndpointVolume
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        dev = None
        try:
            from pycaw.pycaw import AudioUtilities
            dev = AudioUtilities.GetMicrophone()
        except Exception:
            dev = None
        if dev is None:
            # Manual enumerator fallback.
            from comtypes import CoCreateInstance
            from pycaw.constants import CLSID_MMDeviceEnumerator
            from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
            enum = CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
            dev = enum.GetDefaultAudioEndpoint(self._EDATAFLOW_CAPTURE, self._EROLE_COMMUNICATIONS)
        try:
            dev_id = dev.GetId()
        except Exception:
            dev_id = None
        # Rebind when the default comms mic CHANGES: a still-valid but no-longer-default endpoint
        # returns a STALE mute state WITHOUT raising (same class of bug as _system_volume). Keep the
        # cached endpoint when the id is unchanged (or unknown) to avoid per-call churn.
        if self._endpoint is not None and (dev_id is None or dev_id == self._dev_id):
            return self._endpoint
        if self._endpoint is not None:
            _mic_retired.append(self._endpoint)          # retire the old endpoint (don't free)
        iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        self._endpoint = cast(iface, POINTER(IAudioEndpointVolume))
        self._dev_id = dev_id
        return self._endpoint

    def _retire(self) -> None:
        # Drop the cached endpoint so the NEXT call re-binds to the current default capture device
        # (after a mobo swap / audio-device change the old endpoint raises AUDCLNT_E_DEVICE_INVALIDATED
        # forever). RETIRE, never free — comtypes double-Releases the cast()-aliased pointer, a native
        # crash (same reason _system_volume caches + retires; the mic never crashed only because it
        # never freed its endpoint). Rare event -> the leaked list stays tiny.
        if self._endpoint is not None:
            _mic_retired.append(self._endpoint)
            self._endpoint = None
        self._dev_id = None

    def set(self, muted: bool) -> None:
        for _ in (1, 2):
            try:
                self._ensure().SetMute(1 if muted else 0, None)
                return
            except Exception:
                self._retire()

    def toggle(self) -> bool:
        for _ in (1, 2):
            try:
                ep = self._ensure()
                new = 0 if ep.GetMute() else 1
                ep.SetMute(new, None)
                return bool(new)
            except Exception:
                self._retire()
        return False

    def is_muted(self) -> Optional[bool]:
        for _ in (1, 2):
            try:
                return bool(self._ensure().GetMute())
            except Exception:
                self._retire()
        return None


class _AppVolume:
    """Per-application volume via pycaw audio sessions. Target = the focused app, or an exe name."""

    def _sessions(self, target):
        from pycaw.pycaw import AudioUtilities
        import comtypes
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        pid = None
        name = target
        if target in ("", "focused", None):
            from .apppoller import foreground_pid, proc_name
            pid = foreground_pid()
            name = proc_name(pid)
        out = []
        for s in AudioUtilities.GetAllSessions():
            p = s.Process
            if p is None:
                continue
            try:
                if pid is not None:
                    if p.pid == pid:
                        out.append(s)
                elif (p.name() or "").lower() == (target or "").lower():
                    out.append(s)
                    name = p.name()
            except Exception:
                pass
        return out, (name or "Volume")

    def apply(self, target, mode, step):
        """Returns (volume_percent, muted, app_name) for the HUD, or None. Runs entirely on the
        com-audio thread so every session COM object is created + released there, never cross-thread."""
        return _audio(lambda: self._apply_impl(target, mode, step))

    def _apply_impl(self, target, mode, step):
        sess, name = self._sessions(target)
        if not sess:
            return None
        vols = []
        for s in sess:
            v = s.SimpleAudioVolume
            if mode == "mute":
                v.SetMute(0 if v.GetMute() else 1, None)
            else:
                cur = v.GetMasterVolume()
                nv = max(0.0, min(1.0, cur + (step / 100.0 if mode == "up" else -step / 100.0)))
                v.SetMasterVolume(nv, None)
                if nv > 0:
                    v.SetMute(0, None)                 # nudging volume unmutes
            vols.append(s.SimpleAudioVolume.GetMasterVolume())
        muted = bool(sess[0].SimpleAudioVolume.GetMute())
        pct = int(round((sum(vols) / len(vols)) * 100))
        return (pct, muted, name)


_MON_FADE_MIN = 7      # batched nudge >= this many % fades smoothly; smaller stays instant


class _MonitorDimmer:
    """Coalesces monitor-brightness changes so a fast-spun encoder stays smooth.

    DDC/CI is slow (~tens of ms per monitor), so instead of one slow call per detent we
    accumulate the deltas and apply the batched total in a background worker.
    """

    def __init__(self, controller=None) -> None:
        self.controller = controller
        self._lock = threading.Lock()
        self._pending: Dict[Any, int] = {}     # key -> accumulated delta
        self._target: Dict[Any, int] = {}      # key -> set-target percent
        self._cur: Dict[Any, float] = {}       # key -> locally-owned set-point % (seeded from DDC once/burst)
        self._wake = threading.Event()
        self._thread = None

    def _hud(self, idx, pct):
        c = self.controller
        if pct is not None and c is not None and hasattr(c, "show_value_hud"):
            try:
                c.show_value_hud(int(pct), "Monitor", accent=_HUD_MINT)
            except Exception:
                pass

    @staticmethod
    def _key(index):
        return "all" if index is None else int(index)

    def bump(self, delta, index) -> None:
        k = self._key(index)
        with self._lock:
            self._pending[k] = self._pending.get(k, 0) + delta
            self._target.pop(k, None)
            cur = self._cur.get(k)
            proj = None if cur is None else max(0, min(100, cur + self._pending[k]))
        if proj is not None:                       # optimistic — the HUD tracks the knob instantly
            self._hud(None if k == "all" else k, int(round(proj)))
        self._kick()

    def set(self, value, index) -> None:
        k = self._key(index)
        v = max(0, min(100, int(value)))
        with self._lock:
            self._target[k] = v
            self._pending.pop(k, None)
        self._hud(None if k == "all" else k, v)    # optimistic
        self._kick()

    def _kick(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        from . import monitors
        sessions: Dict[Any, Any] = {}              # key -> BrightnessSession (handles+range cached for the burst)

        def _sess(k, idx):
            s = sessions.get(k)
            if s is None:
                s = monitors.BrightnessSession(idx)
                sessions[k] = s
                cp = s.current_pct()               # re-sync to the monitor's true % at burst start
                if cp is not None:
                    with self._lock:
                        self._cur[k] = float(cp)
            return s

        try:
            while True:
                if not self._wake.wait(timeout=2.5):
                    return                          # idle -> exit (finally closes the DDC handles)
                self._wake.clear()
                with self._lock:
                    pend, self._pending = self._pending, {}
                    targ, self._target = self._target, {}
                for k, delta in pend.items():
                    if not delta:
                        continue
                    idx = None if k == "all" else k
                    try:
                        s = _sess(k, idx)
                        with self._lock:
                            cur = max(0.0, min(100.0, self._cur.get(k, 50.0) + delta))
                            self._cur[k] = cur
                        s.set_pct(cur)              # ONE DDC SET (no enum / no read-back) — snappy
                        self._hud(idx, int(round(cur)))
                    except Exception:
                        pass
                for k, val in targ.items():
                    idx = None if k == "all" else k
                    try:
                        s = _sess(k, idx)
                        with self._lock:
                            self._cur[k] = float(val)
                        s.set_pct(float(val))
                        self._hud(idx, int(round(val)))
                    except Exception:
                        pass
                time.sleep(0.025)                   # coalesce ticks landing during the DDC write
        finally:
            for s in sessions.values():
                try:
                    s.close()
                except Exception:
                    pass


class ActionEngine:
    def __init__(self, controller: Optional[Any] = None) -> None:
        self.controller = controller
        self.mic = _Mic()
        self._mon_dimmer = None
        self._app_vol = None
        self._rgb_dimmer = None
        self._discord_vol = None
        self._obs_vol = None
        self._rgb_est = None             # local brightness estimate for Prisma (can't be read back)
        self._rgb_hue = None             # encoder colour-cycle coalescer for Prisma
        self._rgb_hue_est = None         # local hue estimate (Prisma can't report colour) for the HUD
        self._tapo_notify_set = False    # registered the bulb bri/hue HUD callback yet?
        self._work_q = None              # lazily-created queue for the off-loop action worker

    # Actions that touch controller navigation state, or hold a thread-bound COM object, or already
    # self-thread, run INLINE (on the device loop thread). Everything else — blocking keyboard /
    # system side-effects — runs on a single ordered worker thread so the input loop never waits on
    # a keystroke simulation (keeps presses/turns snappy). mic/monitor stay inline (COM is bound to
    # the thread that created it); macro stays inline (it may contain navigation sub-steps).
    _INLINE = frozenset({"open", "page", "folder", "profile", "brightness", "sound",
                         "smartlight", "appvolume", "rgbscene", "obs", "mic", "monitor", "macro"})

    def execute(self, action: Optional[Dict[str, Any]]) -> None:
        if not action:
            return
        t = (action.get("type") or "").lower()
        if t in self._INLINE:
            try:
                self._dispatch(action)
            except Exception as e:  # never let a bad binding kill the event loop
                print(f"[action] error running {action!r}: {e}")
        else:
            self._enqueue(action)   # blocking keyboard/system action -> off the input loop

    def _enqueue(self, action: Dict[str, Any]) -> None:
        if self._work_q is None:
            import queue
            self._work_q = queue.Queue()
            threading.Thread(target=self._work_loop, name="action-worker", daemon=True).start()
        self._work_q.put(action)

    def _work_loop(self) -> None:
        while True:
            action = self._work_q.get()
            try:
                self._dispatch(action)
            except Exception as e:
                print(f"[action] error running {action!r}: {e}")
            # NB: do NOT gc.collect() here — it releases COM objects from cycles regardless of which
            # thread created them; under the MTA (run.py) and the single gc-collector thread that is
            # handled safely. Calling it on this worker thread was the volume-spam crash regression.

    # ------------------------------------------------------------------
    def _dispatch(self, action: Dict[str, Any]) -> None:
        t = (action.get("type") or "").lower()
        if t in ("open", "launch", "run"):
            self._open(action)
        elif t == "hotkey":
            keys = action.get("keys", "")
            if "mouse:" in keys:
                self._send_mouse(keys)
            else:
                _kb_send(_normalize_hotkey(keys))
        elif t == "text":
            _kb_write(action.get("text", ""), delay=0.005)
        elif t == "media":
            key = _MEDIA_KEYS.get((action.get("media") or "").lower())
            if key:
                _kb_send(key)
        elif t == "volume":
            key = _VOLUME_KEYS.get((action.get("volume") or "").lower())
            if key:
                repeat = max(1, int(action.get("step", 1)))
                for _ in range(repeat):
                    _kb_send(key)
                self._system_volume_hud()        # read the settled level -> bar HUD on the keys
        elif t == "mic":
            mode = (action.get("mic") or "toggle").lower()
            if mode == "toggle":
                self.mic.toggle()
            else:
                self.mic.set(mode == "mute")
        elif t == "page":
            self._page(action)
        elif t == "folder":
            self._call("enter_folder", action.get("folder"))
        elif t == "profile":
            self._call("set_profile", action.get("name"))
        elif t == "brightness":
            mode = (action.get("mode") or "set").lower()
            step = max(1, int(action.get("step", 10)))
            if mode == "up":
                self._call("adjust_brightness", step)
            elif mode == "down":
                self._call("adjust_brightness", -step)
            elif "delta" in action:
                self._call("adjust_brightness", int(action["delta"]))
            else:
                self._call("set_brightness", int(action.get("value", 70)))
            cfg = getattr(self.controller, "config", None)          # _call is sync -> value is current
            if cfg is not None:
                self._hud(max(0, min(100, int(cfg.brightness))), "Key brightness", accent=_HUD_MINT)
        elif t == "system":
            self._system((action.get("system") or "lock").lower())
        elif t == "monitor":
            self._monitor(action)
        elif t == "sound":
            from . import sound
            if (action.get("mode") or "play").lower() == "stop":
                sound.stop()                            # cut all currently-playing clips
            else:
                sound.play(action.get("file", ""), action.get("device") or None,
                           bool(action.get("monitor", False)), float(action.get("gain", 1.0) or 1.0))
        elif t == "discord":
            self._discord(action)
        elif t == "substance":
            keys = action.get("keys", "")
            if "mouse:" in keys:
                self._send_mouse(keys)
            elif keys:
                # Send by physical scan code so Painter shortcuts ([ ] brush size, digits, …)
                # fire on any keyboard layout; fall back to character send if unmapped.
                if not send_scancode_combo(keys):
                    _kb_send(_normalize_hotkey(keys))
        elif t == "http":
            self._http(action)
        elif t == "quick":
            self._quick(action.get("op"))
        elif t == "macro":
            for step in action.get("steps", []):
                try:
                    if (step.get("type") or "").lower() == "delay":
                        time.sleep(max(0, int(step.get("ms", 0))) / 1000.0)
                    else:
                        self._dispatch(step)
                except Exception as e:                  # one bad step must not abort the rest of the macro
                    print(f"[action] macro step {step.get('type')!r} failed: {e}")
        elif t == "smartlight":
            self._smartlight(action)
        elif t == "rgbscene":
            self._rgbscene(action)
        elif t == "appvolume":
            self._appvolume(action)
        elif t == "obs":
            self._obs(action)
        elif t == "timer":
            pass          # stateful: handled by the controller (needs the per-key identity + face)
        elif t == "toggle":
            # Per-key toggling (state + face) lives in the controller; here (encoder/macro use) we
            # just fire the first non-empty state's action so it still does something sensible.
            for st in (action.get("states") or []):
                sub = st.get("action") if isinstance(st, dict) else None
                if sub and (sub.get("type") or "none") not in ("none", "", None):
                    self._dispatch(sub)
                    break
        elif t in ("none", "", None):
            pass
        else:
            print(f"[action] unknown action type: {t!r}")

    def _http(self, action: Dict[str, Any]) -> None:
        """Fire a user-configured HTTP request (webhook / REST API) — runs on the off-loop worker
        so a slow endpoint never stalls the dock. GET/POST/PUT/PATCH/DELETE; an optional body is
        sent for the write verbs, with a Content-Type (default application/json)."""
        import urllib.request
        url = (action.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return                                      # only real HTTP(S) endpoints
        method = (action.get("method") or "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            method = "GET"
        body = action.get("body") or ""
        data = body.encode("utf-8") if (body and method in ("POST", "PUT", "PATCH", "DELETE")) else None
        headers = {"User-Agent": "AjazzDock"}
        if data:
            headers["Content-Type"] = (action.get("content_type") or "application/json").strip()
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read(64)                           # complete the exchange; body is discarded
        except Exception as e:
            print(f"[action] http {method} {url!r} failed: {e}")

    def _quick(self, op: Optional[str]) -> None:
        op = (op or "").lower()
        if op == "recycle_empty":
            self._empty_recycle_bin()
        elif op == "recycle_open":
            subprocess.Popen(["explorer.exe", "shell:RecycleBinFolder"])
        elif op == "clipboard_clear":
            self._clear_clipboard()
        elif op == "settings":
            os.startfile("ms-settings:")
        elif op == "lock":
            self._system("lock")
        elif op in _QUICK_HOTKEYS:
            _kb_send(_QUICK_HOTKEYS[op])
        else:
            print(f"[action] unknown quick action: {op!r}")

    @staticmethod
    def _empty_recycle_bin() -> None:
        # SHEmptyRecycleBin: no confirmation dialog + no progress UI, but keep the system
        # sound as audible feedback. Returns S_FALSE when already empty (ignored).
        SHERB_NOCONFIRMATION, SHERB_NOPROGRESSUI = 0x01, 0x02
        ctypes.windll.shell32.SHEmptyRecycleBinW(
            None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI)

    @staticmethod
    def _clear_clipboard() -> None:
        u = ctypes.windll.user32
        if u.OpenClipboard(0):
            try:
                u.EmptyClipboard()
            finally:
                u.CloseClipboard()

    def _system(self, what: str) -> None:
        import ctypes
        if what == "lock":
            ctypes.windll.user32.LockWorkStation()           # the proper way to do "Win+L"
        elif what == "sleep":
            ctypes.windll.powrprof.SetSuspendState(0, 0, 0)
        elif what in ("monitor_off", "screen_off"):
            ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)  # WM_SYSCOMMAND monitor off
        elif what == "screensaver":
            ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF140, 0)  # SC_SCREENSAVE
        else:
            print(f"[action] unknown system command: {what!r}")

    def _send_mouse(self, spec: str) -> None:
        """Send a mouse action (optionally with held modifiers), e.g. 'ctrl+mouse:wheel_up'."""
        import keyboard
        import mouse
        parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
        mtoken = next((p for p in parts if p.startswith("mouse:")), "")
        mods = [_HOTKEY_ALIASES.get(p, p) for p in parts if not p.startswith("mouse:")]
        with _KBD_LOCK:                    # hold the lock across press → click → release as one unit
            held = []
            for m in mods:
                try:
                    keyboard.press(m)
                    held.append(m)
                except Exception:
                    pass
            try:
                act = mtoken.split(":", 1)[1] if ":" in mtoken else ""
                if act == "left":
                    mouse.click("left")
                elif act == "right":
                    mouse.click("right")
                elif act == "middle":
                    mouse.click("middle")
                elif act == "double":
                    mouse.double_click("left")
                elif act == "back":
                    mouse.click("x")
                elif act == "forward":
                    mouse.click("x2")
                elif act == "wheel_up":
                    mouse.wheel(1)
                elif act == "wheel_down":
                    mouse.wheel(-1)
            finally:
                for m in reversed(held):
                    try:
                        keyboard.release(m)
                    except Exception:
                        pass

    def _hud(self, value, name, *, muted=False, accent=None, relative=0, unit="%", vmax=100):
        c = self.controller
        if c is not None and hasattr(c, "show_value_hud"):
            try:
                c.show_value_hud(value, name, muted=muted, accent=accent, relative=relative,
                                 unit=unit, vmax=vmax)
            except Exception:
                pass

    def _system_volume_hud(self):
        """Read the (just-changed) system output level off-thread and pop a bar HUD."""
        if self.controller is None or not hasattr(self.controller, "show_value_hud"):
            return
        def work():
            time.sleep(0.05)                      # let the media key settle
            r = _system_volume()
            if r is not None:
                pct, muted = r
                self._hud(pct, "Volume", muted=muted)
        threading.Thread(target=work, name="sysvol-hud", daemon=True).start()

    def _bulb_hud(self, kind, value):
        """tapo callback (its bg loop): show the bulb's true brightness % / colour hue°."""
        if kind == "hue":
            import colorsys
            r, g, b = colorsys.hsv_to_rgb((int(value) % 360) / 360.0, 1.0, 1.0)
            self._hud(int(value), "Bulb colour", unit="°", vmax=360,
                      accent=(int(r * 255), int(g * 255), int(b * 255)))
        else:
            self._hud(int(value), "Bulb", accent=_HUD_MINT)

    def _monitor(self, action: Dict[str, Any]) -> None:
        mode = (action.get("monitor") or "up").lower()
        idx = action.get("index")
        if idx in (None, "", "all", -1, "-1"):
            idx = None
        else:
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                idx = None
        step = max(1, int(action.get("step", 5)))
        if self._mon_dimmer is None:
            self._mon_dimmer = _MonitorDimmer(self.controller)
        if mode == "set":
            self._mon_dimmer.set(int(action.get("value", 50)), idx)
        elif mode == "down":
            self._mon_dimmer.bump(-step, idx)
        else:
            self._mon_dimmer.bump(step, idx)
        # the dimmer thread reads the true brightness after applying and pops the HUD itself

    # ------------------------------------------------------------------
    def _open(self, action: Dict[str, Any]) -> None:
        target = action.get("target") or action.get("path") or ""
        if not target:
            return
        args = action.get("args")
        if args:
            arglist: List[str] = args if isinstance(args, list) else shlex.split(args)
            subprocess.Popen([target, *arglist], shell=False)
        else:
            os.startfile(target)  # handles exe, file, folder, and URLs on Windows

    def _page(self, action: Dict[str, Any]) -> None:
        where = (action.get("page") or "next").lower()
        if where == "next":
            self._call("next_page")
        elif where == "prev":
            self._call("prev_page")
        elif where in ("goto", "set"):
            self._call("goto_page", int(action.get("target", 0)))

    # ---- integrations ---------------------------------------------------------
    # encoder relative modes -> (kind, sign, default-step)
    _REL_LIGHT = {"brightness_up": ("bri", 1, 10), "brightness_down": ("bri", -1, 10),
                  "hue_up": ("hue", 1, 18), "hue_down": ("hue", -1, 18)}   # finer = smoother colour cycle

    def _smartlight(self, action: Dict[str, Any]) -> None:
        """Control the Tapo bulb directly (on/off/toggle/colour/relative) — no Lumos app required."""
        host = (action.get("host") or _TAPO_DEFAULT_HOST).strip()
        mode = (action.get("mode") or "toggle").lower()
        color = action.get("color")
        brightness = action.get("brightness")
        config = getattr(self.controller, "config", None) if self.controller else None

        # Encoder turn: accumulate the change locally and let tapo coalesce/rate-limit the flush,
        # so spinning fast feels instant. nudge() never blocks, so no worker thread is needed.
        rel = self._REL_LIGHT.get(mode)
        if rel:
            kind, sign, default_step = rel
            step = int(action.get("step") or default_step)
            try:
                from . import tapo, live
                email, pw = tapo.tapo_creds(config) if config is not None else (None, None)
                if not (email and pw):
                    print("[smartlight] no Tapo credentials — set them in the key editor")
                    return
                if not self._tapo_notify_set:           # surface the bulb's true bri/hue on the HUD
                    tapo.set_live_notify(self._bulb_hud)
                    self._tapo_notify_set = True
                tapo.nudge(host, email, pw, kind, sign * step)
                live.set_light_state(True)              # a brightness/colour change implies it's on
                if self.controller is not None and hasattr(self.controller, "refresh_live"):
                    self.controller.refresh_live()
            except Exception as e:
                print(f"[smartlight] {e}")
            return

        # Auto rainbow cycle: one press starts a software hue-loop, the next press stops it.
        if mode == "cycle":
            def work_cycle() -> None:
                try:
                    from . import tapo, live
                    email, pw = tapo.tapo_creds(config) if config is not None else (None, None)
                    if not (email and pw):
                        print("[smartlight] no Tapo credentials — set them in the key editor")
                        return
                    tapo.cycle(host, email, pw, step=int(action.get("step") or 8))
                    live.set_light_state(True)          # cycling implies the bulb is on
                    if self.controller is not None and hasattr(self.controller, "refresh_live"):
                        self.controller.refresh_live()
                except Exception as e:
                    print(f"[smartlight] {e}")
            threading.Thread(target=work_cycle, daemon=True).start()
            return

        def work() -> None:
            try:
                from . import tapo
                email, pw = tapo.tapo_creds(config) if config is not None else (None, None)
                if not (email and pw):
                    print("[smartlight] no Tapo credentials — set them in the key editor")
                    return
                hsv = None
                if mode == "color" and color:
                    h, s, _v = _hex_to_hsv(color)
                    v = int(brightness) if brightness else _v
                    hsv = (h, s, max(1, min(100, v)))
                step = action.get("step")
                new_on = tapo.apply(host, email, pw, mode, hsv=hsv, brightness=brightness, step=step)
                if new_on is not None:                  # update the live key instantly, no poll wait
                    from . import live
                    live.set_light_state(new_on)
                    if self.controller is not None and hasattr(self.controller, "refresh_live"):
                        self.controller.refresh_live()
            except Exception as e:
                print(f"[smartlight] {e}")

        threading.Thread(target=work, daemon=True).start()

    def _rgb_mark(self, on: Optional[bool]) -> None:
        """Record Prisma's on/off for the RGB live key and refresh it immediately (Prisma can't be polled)."""
        try:
            from . import live
            live.set_rgb_state(on)
        except Exception:
            return
        if self.controller is not None and hasattr(self.controller, "refresh_live"):
            self.controller.refresh_live()

    def _rgbscene(self, action: Dict[str, Any]) -> None:
        """Drive Prisma via its CLI (a launch with args forwards to the running instance)."""
        exe = (action.get("exe") or _RGB_EXE).strip()
        mode = (action.get("mode") or "color").lower()

        # Encoder brightness: coalesce ticks so a fast spin sends ONE `--brightness +N`, not one
        # process launch per click (Prisma resolves relative +/- against its own brightness bar).
        if mode in ("bright_up", "bright_down"):
            step = int(action.get("step") or 10)
            if self._rgb_dimmer is None:
                self._rgb_dimmer = _RgbDimmer()
            d = step if mode == "bright_up" else -step
            self._rgb_dimmer.nudge(exe, d)
            # Prisma can't report its level, so track a local estimate (seeded mid-scale) for the bar.
            self._rgb_est = max(0, min(100, (self._rgb_est if self._rgb_est is not None else 50) + d))
            self._hud(self._rgb_est, "RGB lights", accent=_HUD_MINT)
            self._rgb_mark(True)                            # adjusting brightness -> RGB is on
            return

        # Encoder colour cycle: coalesce ticks into one `--color` per window; HUD tracks the dial.
        if mode in ("hue_up", "hue_down"):
            step = int(action.get("step") or 20)
            if self._rgb_hue is None:
                self._rgb_hue = _RgbHue()
            d = step if mode == "hue_up" else -step
            self._rgb_hue.nudge(exe, d)
            import colorsys
            self._rgb_hue_est = ((self._rgb_hue_est if self._rgb_hue_est is not None else 0) + d) % 360
            r, g, b = colorsys.hsv_to_rgb(self._rgb_hue_est / 360.0, 1, 1)
            self._hud(int(self._rgb_hue_est), "RGB colour", unit="°", vmax=360,
                      accent=(int(r * 255), int(g * 255), int(b * 255)))
            self._rgb_mark(True)                            # cycling colour -> RGB is on
            return

        args = self._rgb_args(mode, action)
        if args is None:
            return
        if mode == "off":
            self._rgb_mark(False)
        elif mode == "toggle":
            from . import live
            self._rgb_mark(not live.rgb_is_on())
        elif mode in ("color", "effect", "profile", "bright_set"):
            self._rgb_mark(True)

        def work() -> None:
            try:
                if not _proc_running(_RGB_PROC_NAMES):     # no primary -> start it first
                    subprocess.Popen([exe, "--minimized"])
                    time.sleep(1.8)
                subprocess.Popen([exe, "--minimized", *args])   # stripped when forwarded; no window pop on a TOCTOU miss
            except Exception as e:
                print(f"[rgbscene] {e}")

        threading.Thread(target=work, daemon=True).start()

    def _appvolume(self, action: Dict[str, Any]) -> None:
        """Adjust the focused (or a named) app's volume; show a volume HUD on the keys."""
        target = action.get("target") or "focused"
        mode = (action.get("mode") or "up").lower()
        step = max(1, int(action.get("step", 5)))

        def work() -> None:
            try:
                if self._app_vol is None:
                    self._app_vol = _AppVolume()
                res = self._app_vol.apply(target, mode, step)
                if res and self.controller and hasattr(self.controller, "show_volume_hud"):
                    self.controller.show_volume_hud(*res)
            except Exception as e:
                print(f"[appvolume] {e}")
            # No gc.collect() here: the process MTA (run.py) makes these pycaw session wrappers
            # agile, so the gc-collector thread can release them safely after this thread exits.

        threading.Thread(target=work, daemon=True).start()

    def _obs(self, action: Dict[str, Any]) -> None:
        """Control OBS Studio over its WebSocket (scene / record / stream / virtual cam / mute /
        audio-source volume on a dial)."""
        mode = (action.get("mode") or "scene").lower()
        if mode in ("vol_up", "vol_down", "vol_mute"):          # audio mixer (encoder) — coalesced + HUD
            inp = (action.get("input") or action.get("target") or "").strip()
            if not inp:
                return
            if self._obs_vol is None:
                self._obs_vol = _ObsVolume(self.controller)
            if mode == "vol_mute":
                self._obs_vol.mute(inp)
            else:
                step = max(1, int(action.get("step", 5)))
                self._obs_vol.nudge(inp, step if mode == "vol_up" else -step)
            return
        target = (action.get("target") or "").strip()
        config = getattr(self.controller, "config", None) if self.controller else None

        def work() -> None:
            try:
                from . import obs
                if config is not None:
                    o = config.data.get("obs", {})
                    obs.configure(o.get("host"), o.get("port"), o.get("password"))
                req = {
                    "scene": ("SetCurrentProgramScene", {"sceneName": target}),
                    "preview": ("SetCurrentPreviewScene", {"sceneName": target}),
                    "record": ("ToggleRecord", None),
                    "stream": ("ToggleStream", None),
                    "virtualcam": ("ToggleVirtualCam", None),
                    "replay": ("SaveReplayBuffer", None),
                    "mute": ("ToggleInputMute", {"inputName": target}),
                }.get(mode)
                if req is None:
                    return
                obs.request(req[0], req[1])
            except Exception as e:
                print(f"[obs] {e}")

        threading.Thread(target=work, daemon=True).start()

    def _discord(self, action: Dict[str, Any]) -> None:
        """Discord voice over RPC: mute/deafen/disconnect, mic/output volume dials, PTT/noise
        toggles, join a channel — or a plain keybind."""
        mode = (action.get("mode") or "").lower()
        keys = action.get("keys")
        if mode in ("keybind", "ptt_key") or (not mode and keys):
            if keys:                                       # legacy / push-to-talk via a Discord keybind
                _kb_send(_normalize_hotkey(keys))
            return
        mode = mode or "mute"

        # Encoder volume dials: coalesce so a fast spin doesn't flood the RPC pipe (one worker).
        if mode in ("invol_up", "invol_down", "outvol_up", "outvol_down"):
            step = max(1, int(action.get("step", 5))) * (1 if mode.endswith("up") else -1)
            if self._discord_vol is None:
                self._discord_vol = _DiscordVol(self.controller)
            self._discord_vol.nudge("in" if mode.startswith("invol") else "out", step)
            return

        channel = action.get("channel_id")

        def work() -> None:
            from . import discord as dc, live
            try:
                if mode == "mute":
                    live.set_discord_state(mute=dc.toggle_mute())
                elif mode == "deafen":
                    live.set_discord_state(deaf=dc.toggle_deaf())
                elif mode == "disconnect":
                    dc.disconnect_voice()
                elif mode == "mode_toggle":
                    dc.toggle_mode()
                elif mode == "noise_toggle":
                    dc.toggle_noise()
                elif mode == "join":
                    dc.join_channel(channel or dc.current_channel_id())
                else:
                    return
                if self.controller is not None and hasattr(self.controller, "refresh_live"):
                    self.controller.refresh_live()
            except dc.NeedsAuth:
                print("[discord] not authorized yet — open the editor's “Discord app…” dialog and Authorize")
            except Exception as e:
                print(f"[discord] {e}")

        threading.Thread(target=work, name="discord", daemon=True).start()

    @staticmethod
    def _rgb_args(mode: str, action: Dict[str, Any]) -> Optional[List[str]]:
        if mode == "color":
            return ["--effect", "static", "--color", (action.get("color") or "00C8AA").lstrip("#")]
        if mode == "effect":
            return ["--effect", (action.get("effect") or "Rainbow")]
        if mode == "profile":
            name = (action.get("profile") or "").strip()
            return ["--profile", name] if name else None
        if mode == "bright_set":
            return ["--brightness", str(max(0, min(100, int(action.get("brightness", 100)))))]
        if mode == "toggle":
            return ["--toggle"]
        if mode == "off":
            return ["--off"]
        return None

    def _call(self, method: str, *args) -> None:
        if self.controller and hasattr(self.controller, method):
            getattr(self.controller, method)(*args)
