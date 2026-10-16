"""Live data providers for dynamic keys.

Each provider returns ``(text, caption, frac)`` — a value string, a short caption, and an
optional 0..1 fill fraction (for gauges / bars / battery). ``value()`` adds the source's
``kind`` so ``images.live_face`` can draw a fitting, dynamic icon (gauge ring, battery, clock…).

Providers must be cheap + non-blocking (they run on the device loop, once a second): CPU uses
the non-blocking ``interval=None`` sampler and GPU is read from a background sampler thread.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

try:
    import psutil
except Exception:                       # psutil is optional; system stats degrade to "--"
    psutil = None

_net_last: Dict[str, float] = {}
_net_up_last: Dict[str, float] = {}
_net_rate_lock = threading.Lock()   # net rate is read from both the GUI picker and the dock loop

# id -> (picker label, kind, provider). kind drives the icon: gauge / battery / clock / date / net.
_META: Dict[str, Tuple[str, str]] = {
    "clock":     ("Clock (HH:MM)", "clock"),
    "clock_sec": ("Clock (HH:MM:SS)", "clock"),
    "date":      ("Date", "date"),
    "cpu":       ("CPU load %", "percent"),
    "ram":       ("RAM used %", "percent"),
    "gpu":       ("GPU load %", "percent"),
    "cpu_temp":  ("CPU temperature °C", "percent"),
    "gpu_temp":  ("GPU temperature °C", "percent"),
    "cpu_clock": ("CPU clock GHz", "percent"),
    "vram":      ("GPU memory used %", "percent"),
    "vram_temp": ("GPU memory temp °C", "percent"),
    "gpu_clock": ("GPU clock MHz", "gauge"),
    "gpu_fan":   ("GPU fan RPM", "gauge"),
    "ram_gb":    ("RAM used GB", "percent"),
    "swap":      ("Swap used %", "percent"),
    "disk":      ("Disk C: used %", "percent"),
    "battery":   ("Battery %", "battery"),
    "net":       ("Network down KB/s", "net"),
    "net_up":    ("Network up KB/s", "net"),
    "ping":      ("Internet latency / connection health", "gauge"),
    "uptime":    ("System uptime", "gauge"),
    "procs":     ("Process count", "gauge"),
    "mic":       ("Mic mute state", "state"),
    "caps":      ("Caps Lock state", "state"),
    "light":     ("Smart bulb on/off", "state"),
    "rgb":       ("RGB lights on/off", "state"),
    "monitors":  ("PC monitors on/off", "state"),
    "vrc":       ("VRChat — connected / avatar", "state"),
    "vrc_mute":  ("VRChat mic mute (MuteSelf)", "state"),
    "vrc_afk":   ("VRChat AFK", "state"),
    "discord":   ("Discord mute / deafen", "state"),
    "discord_mic": ("Discord mic mute", "state"),
    "discord_deaf": ("Discord deafen", "state"),
    "discord_voice": ("Discord — in a call", "state"),
    "discord_count": ("Discord — people in call", "gauge"),
    "discord_invol": ("Discord mic input volume", "vol"),
    "discord_outvol": ("Discord output volume", "vol"),
    "discord_mode": ("Discord push-to-talk / voice", "state"),
    "discord_noise": ("Discord noise suppression", "state"),
    "obs_streaming":  ("OBS — streaming", "state"),
    "obs_recording":  ("OBS — recording", "state"),
    "obs_virtualcam": ("OBS — virtual camera", "state"),
    "obs_scene":      ("OBS — current scene", "gauge"),
    "obs_replay":     ("OBS — replay buffer", "state"),
    "media":     ("Now playing (title / artist)", "media"),
    "weather":   ("Weather — current conditions", "weather"),
    "wx_feels":   ("Weather — feels-like °C", "wxmetric"),
    "wx_humidity": ("Weather — humidity %", "wxmetric"),
    "wx_wind":    ("Weather — wind km/h", "wxmetric"),
    "wx_uv":      ("Weather — UV index", "wxmetric"),
    "wx_precip":  ("Weather — chance of rain %", "wxmetric"),
    "wx_hi":      ("Weather — today's high °C", "wxmetric"),
    "wx_lo":      ("Weather — today's low °C", "wxmetric"),
    "wx_sunrise": ("Weather — sunrise time", "wxmetric"),
    "wx_sunset":  ("Weather — sunset time", "wxmetric"),
    "wx_d1": ("Weather — forecast, tomorrow", "wxforecast"),
    "wx_d2": ("Weather — forecast, in 2 days", "wxforecast"),
    "wx_d3": ("Weather — forecast, in 3 days", "wxforecast"),
    "wx_d4": ("Weather — forecast, in 4 days", "wxforecast"),
    "wx_d5": ("Weather — forecast, in 5 days", "wxforecast"),
    "wx_d6": ("Weather — forecast, in 6 days", "wxforecast"),
}

# Short tile names + emoji + grouping for the grid picker (so 24 sources are easy to browse).
LIVE_SHORT: Dict[str, str] = {
    "clock": "Clock", "clock_sec": "Clock (sec)", "date": "Date",
    "cpu": "CPU load", "cpu_clock": "CPU clock", "cpu_temp": "CPU temp",
    "ram": "RAM used", "ram_gb": "RAM (GB)", "swap": "Swap",
    "gpu": "GPU load", "gpu_clock": "GPU clock", "gpu_temp": "GPU temp",
    "vram": "VRAM used", "vram_temp": "VRAM temp", "gpu_fan": "GPU fan",
    "disk": "Disk", "net": "Net down", "net_up": "Net up", "ping": "Ping",
    "battery": "Battery", "uptime": "Uptime", "procs": "Processes",
    "mic": "Mic", "caps": "Caps Lock", "light": "Smart bulb", "rgb": "RGB lights",
    "monitors": "Monitors",
    "discord": "Discord", "media": "Now playing",
    "vrc": "VRChat", "vrc_mute": "VRC mic", "vrc_afk": "VRC AFK",
    "weather": "Weather",
    "wx_feels": "Feels like", "wx_humidity": "Humidity", "wx_wind": "Wind", "wx_uv": "UV index",
    "wx_precip": "Rain chance", "wx_hi": "Today high", "wx_lo": "Today low",
    "wx_sunrise": "Sunrise", "wx_sunset": "Sunset",
    "wx_d1": "Tomorrow", "wx_d2": "In 2 days", "wx_d3": "In 3 days",
    "wx_d4": "In 4 days", "wx_d5": "In 5 days", "wx_d6": "In 6 days",
    "discord_mic": "Discord mic", "discord_deaf": "Discord deafen",
    "discord_voice": "In a call", "discord_count": "Call size",
    "discord_invol": "Mic volume", "discord_outvol": "Output volume",
    "discord_mode": "Talk mode", "discord_noise": "Noise filter",
    "obs_streaming": "Streaming", "obs_recording": "Recording", "obs_virtualcam": "Virtual cam",
    "obs_scene": "OBS scene", "obs_replay": "Replay buffer",
}
LIVE_EMOJI: Dict[str, str] = {
    "clock": "🕐", "clock_sec": "⏱️", "date": "📅",
    "cpu": "⚙️", "cpu_clock": "⏲️", "cpu_temp": "🌡️",
    "ram": "🧠", "ram_gb": "📊", "swap": "💾",
    "gpu": "🎮", "gpu_clock": "⚡", "gpu_temp": "🌡️",
    "vram": "🎞️", "vram_temp": "🌡️", "gpu_fan": "🌀",
    "disk": "💽", "net": "🔽", "net_up": "🔼", "ping": "📶",
    "battery": "🔋", "uptime": "⏳", "procs": "🔢",
    "mic": "🎙️", "caps": "🔠", "light": "💡", "rgb": "🌈", "discord": "💬", "media": "🎵",
    "monitors": "🖥️",
    "vrc": "🥽", "vrc_mute": "🔇", "vrc_afk": "💤",
    "discord_mic": "🎙️", "discord_deaf": "🎧", "discord_voice": "📞", "discord_count": "👥",
    "discord_invol": "🎚️", "discord_outvol": "🔊", "discord_mode": "🎯", "discord_noise": "🛡️",
    "obs_streaming": "🔴", "obs_recording": "⏺️", "obs_virtualcam": "📹", "obs_scene": "🎬", "obs_replay": "⏮️",
    "weather": "🌤️",
    "wx_feels": "🌡️", "wx_humidity": "💧", "wx_wind": "💨", "wx_uv": "😎", "wx_precip": "🌧️",
    "wx_hi": "🔺", "wx_lo": "🔻", "wx_sunrise": "🌅", "wx_sunset": "🌇",
    "wx_d1": "🗓️", "wx_d2": "🗓️", "wx_d3": "🗓️", "wx_d4": "🗓️", "wx_d5": "🗓️", "wx_d6": "🗓️",
}
LIVE_CATEGORIES = [
    ("Time & date", ["clock", "clock_sec", "date"]),
    ("Processor", ["cpu", "cpu_clock", "cpu_temp"]),
    ("Memory", ["ram", "ram_gb", "swap"]),
    ("Graphics", ["gpu", "gpu_clock", "gpu_temp", "vram", "vram_temp", "gpu_fan"]),
    ("Storage & network", ["disk", "net", "net_up", "ping"]),
    ("Power & system", ["battery", "uptime", "procs"]),
    ("Status", ["mic", "caps", "light", "rgb", "monitors"]),
    ("Discord", ["discord", "discord_mic", "discord_deaf", "discord_voice", "discord_count",
                 "discord_invol", "discord_outvol", "discord_mode", "discord_noise"]),
    ("OBS Studio", ["obs_streaming", "obs_recording", "obs_virtualcam", "obs_scene", "obs_replay"]),
    ("Media", ["media"]),
    ("VRChat", ["vrc", "vrc_mute", "vrc_afk"]),
    ("Weather — now", ["weather", "wx_feels", "wx_humidity", "wx_wind", "wx_uv", "wx_precip"]),
    ("Weather — today", ["wx_hi", "wx_lo", "wx_sunrise", "wx_sunset"]),
    ("Weather — 6-day forecast", ["wx_d1", "wx_d2", "wx_d3", "wx_d4", "wx_d5", "wx_d6"]),
]


def _clock() -> Tuple[str, str, Optional[float]]:
    return datetime.datetime.now().strftime("%H:%M"), "", None


def _clock_sec() -> Tuple[str, str, Optional[float]]:
    return datetime.datetime.now().strftime("%H:%M:%S"), "", None


def _date() -> Tuple[str, str, Optional[float]]:
    n = datetime.datetime.now()
    return n.strftime("%d"), n.strftime("%a %b").upper(), None


def _cpu() -> Tuple[str, str, Optional[float]]:
    if not psutil:
        return "--", "CPU", None
    p = psutil.cpu_percent(interval=None)
    return f"{int(p)}%", "CPU", p / 100.0


def _ram() -> Tuple[str, str, Optional[float]]:
    if not psutil:
        return "--", "RAM", None
    p = psutil.virtual_memory().percent
    return f"{int(p)}%", "RAM", p / 100.0


def _disk() -> Tuple[str, str, Optional[float]]:
    try:
        u = shutil.disk_usage("C:/")
        p = u.used / u.total
        return f"{int(p * 100)}%", "DISK", p
    except Exception:
        return "--", "DISK", None


def _battery() -> Tuple[str, str, Optional[float]]:
    if not psutil:
        return "--", "BATT", None
    b = psutil.sensors_battery()
    if not b:
        return "--", "BATT", None
    return f"{int(b.percent)}%", ("CHARGING" if b.power_plugged else "BATTERY"), b.percent / 100.0


def _net() -> Tuple[str, str, Optional[float]]:
    if not psutil:
        return "--", "NET", None
    now = time.time()
    io = psutil.net_io_counters()
    cur = {"t": now, "rx": io.bytes_recv}
    with _net_rate_lock:                       # GUI picker + dock loop both sample this
        prev = _net_last.copy()
        _net_last.update(cur)
    if not prev:
        return "0", "KB/S", None
    dt = max(0.001, now - prev["t"])
    rate = max(0, cur["rx"] - prev["rx"]) / dt / 1024.0   # clamp: a NIC reset can drop the counter
    return f"{int(rate)}", "KB/S", None


# ---- sampler parking --------------------------------------------------------------------------
# The heavy daemon samplers (LHM .NET sensor sweep, now-playing WinRT, Tapo LAN, mic COM) run on
# their own threads. Each reader stamps "I was just read" via _touch(group); the sampler loop parks
# on a threading.Event (zero CPU, zero IPC) once nobody has read its source for _IDLE_GRACE seconds,
# and wakes instantly on the next read. So switching to a page with no live key, turning the screen
# off, disconnecting, or hiding the GUI all let the samplers go quiet within ~5s.
_IDLE_GRACE = 5.0
# The "lhm" group is backed by a CHILD PROCESS, not a thread: parking it TERMINATES it, and
# waking it costs a process spawn + pythonnet CLR init + 5 assembly loads + Computer.Open()
# (~0.4-1.4 s), during which every GPU/temp tile reads "--". A 5 s grace tuned for in-process
# samplers therefore rebuilt the worker on every page flip, folder visit or volume HUD. Short
# gaps now keep it; a genuinely idle dock still kills it and goes quiet.
_GROUP_GRACE = {"lhm": 90.0}
_last_read: Dict[str, float] = {}
_wake: Dict[str, "threading.Event"] = {}


def _touch(group: str) -> None:
    _last_read[group] = time.monotonic()
    ev = _wake.get(group)
    if ev is not None:
        ev.set()


def _arm_park(group: str) -> None:
    """Create the wake Event + seed the timestamp before a sampler thread starts (so its first
    iteration runs immediately rather than parking)."""
    _wake[group] = threading.Event()
    _last_read[group] = time.monotonic()


def _park_if_idle(group: str) -> None:
    ev = _wake.get(group)
    if ev is None:
        return
    if time.monotonic() - _last_read.get(group, 0.0) > _GROUP_GRACE.get(group, _IDLE_GRACE):
        ev.clear()
        ev.wait()                       # block here at zero cost until a reader calls _touch()


# ---- GPU load + CPU/GPU temps via LibreHardwareMonitor ----------------------------------------
# One sampler reads everything: AMD's GPU "Load > GPU Core" sensor (via ADLX) is the true GPU
# usage Task Manager shows — the Windows "GPU Engine" perf counters badly underreport AMD cards.
# GPU load + GPU temp work without admin; CPU temp needs admin (ring0 MSR), else reads 0 -> "--".
_lhm_lock = threading.Lock()
# temps °C / loads % / clocks MHz / mem MB / fan RPM, or None
_lhm = {"cpu": None, "gpu": None, "gpu_load": None, "gpu_clock": None, "gpu_fan": None,
        "vram_temp": None, "vram_used": None, "vram_total": None}
_lhm_started = False
_LHM_SHIMS = ("System.Buffers", "System.Numerics.Vectors",
              "System.Runtime.CompilerServices.Unsafe", "System.Memory")


def _lhm_dir() -> str:
    import sys
    import os
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "native")


# The LibreHardwareMonitor sweep touches .NET (pythonnet) + ring0 hardware access, which can
# NATIVELY access-violate on some driver/hardware states (GPU sleep/reset, ADLX). A native crash
# can't be caught by try/except and would take the whole app down silently. So it runs in an
# ISOLATED child process (run.py --lhm-worker): if the sensor code crashes, only the child dies and
# the supervisor respawns it; the main app never imports the CLR and keeps running (temps -> "--").

def _read_lhm_once(comp) -> dict:
    """One sensor sweep -> readings dict. Runs in the WORKER process (a native fault dies HERE)."""
    cpu = cpu_pref = gpu_t = gpu_t_core = gpu_load = None
    gpu_clk = gpu_fan = vram_t = vram_u = vram_tot = None
    for hw in comp.Hardware:
        try:
            hw.Update()
        except Exception:
            continue
        ht = str(hw.HardwareType)
        if ht != "Cpu" and not ht.startswith("Gpu"):
            continue
        is_gpu = ht.startswith("Gpu")
        for s in hw.Sensors:
            if s.Value is None:
                continue
            st = str(s.SensorType)
            v = float(s.Value)
            name = str(s.Name)
            if st == "Temperature":
                if ht == "Cpu":
                    cpu = v if cpu is None else max(cpu, v)
                    if any(k in name for k in ("Tctl", "Tdie", "Package")):
                        cpu_pref = v
                else:                          # Gpu*
                    if name == "GPU Memory":
                        vram_t = v
                    elif "Core" in name:
                        gpu_t_core = v
                    elif gpu_t is None:
                        gpu_t = v
            elif st == "Load" and is_gpu and name == "GPU Core":
                gpu_load = v if gpu_load is None else max(gpu_load, v)
            elif st == "Clock" and is_gpu and name == "GPU Core":
                gpu_clk = v
            elif st == "Fan" and is_gpu:
                gpu_fan = v if gpu_fan is None else max(gpu_fan, v)
            elif st == "SmallData" and is_gpu:
                if name == "GPU Memory Used":
                    vram_u = v
                elif name == "GPU Memory Total":
                    vram_tot = v
    cpu_v = cpu_pref if cpu_pref is not None else cpu
    gpu_v = gpu_t_core if gpu_t_core is not None else gpu_t
    return {
        "cpu": cpu_v if (cpu_v and cpu_v > 0) else None,
        "gpu": gpu_v if (gpu_v and gpu_v > 0) else None,
        "gpu_load": gpu_load,
        "gpu_clock": gpu_clk,
        "gpu_fan": gpu_fan,
        "vram_temp": vram_t if (vram_t and vram_t > 0) else None,
        "vram_used": vram_u,
        "vram_total": vram_tot,
    }


def _parent_alive(ppid: int) -> bool:
    if not ppid:
        return True
    try:
        import psutil
        return psutil.pid_exists(ppid)
    except Exception:
        return True                          # can't tell -> assume alive (parent atexit still kills us)


def lhm_worker_main(out_path: str, ppid: int = 0) -> None:
    """WORKER-PROCESS entry (run.py `--lhm-worker`): open LibreHardwareMonitor and publish readings
    to `out_path` as JSON, one sweep per second. Isolated so a native sensor crash dies HERE."""
    import os
    import json
    try:
        import clr                            # pythonnet
        d = _lhm_dir()
        for shim in _LHM_SHIMS:                # 0.9.6 needs these netstandard shims
            p = os.path.join(d, shim + ".dll")
            if os.path.exists(p):
                try:
                    clr.AddReference(p)
                except Exception:
                    pass
        clr.AddReference(os.path.join(d, "LibreHardwareMonitorLib.dll"))
        from LibreHardwareMonitor.Hardware import Computer  # type: ignore
        comp = Computer()
        comp.IsCpuEnabled = True
        comp.IsGpuEnabled = True
        comp.Open()
    except Exception:
        return
    tmp = out_path + ".tmp"
    while _parent_alive(ppid):
        data = _read_lhm_once(comp)
        data["ts"] = time.time()
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, out_path)          # atomic publish -> parent never reads a half-written file
        except Exception:
            pass
        time.sleep(1.0)


# ---- parent-side worker supervision -----------------------------------------------------------
_lhm_proc = None
_lhm_out = None
_lhm_shutdown = False                        # set at app exit so the supervisor stops respawning the worker


def _lhm_spawn() -> None:
    global _lhm_proc, _lhm_out
    import os
    import sys
    import subprocess
    import tempfile
    if _lhm_out is None:
        _lhm_out = os.path.join(tempfile.gettempdir(), f"hexpad_lhm_{os.getpid()}.json")
    try:
        if os.path.exists(_lhm_out):
            os.remove(_lhm_out)
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        args = [sys.executable, "--lhm-worker", _lhm_out, str(os.getpid())]
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        args = [sys.executable, os.path.join(root, "run.py"), "--lhm-worker", _lhm_out, str(os.getpid())]
    _lhm_proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000)            # CREATE_NO_WINDOW (elevated parent -> child inherits, no UAC)


def _lhm_kill() -> None:
    global _lhm_proc
    p, _lhm_proc = _lhm_proc, None
    if p is not None:
        try:
            p.terminate()
        except Exception:
            pass


def _lhm_cleanup() -> None:
    global _lhm_shutdown
    _lhm_shutdown = True                      # stop the supervisor respawning the worker during shutdown
    _lhm_kill()
    try:
        import os
        if _lhm_out and os.path.exists(_lhm_out):
            os.remove(_lhm_out)
    except Exception:
        pass


def _start_lhm() -> None:
    """Idempotently start the supervisor: keep an isolated LHM worker alive while a GPU/temp key
    consumes it, read its published JSON into `_lhm`, and respawn it (with backoff) if it crashes."""
    global _lhm_started
    _touch("lhm")                            # every consumer call keeps the worker awake / wakes it
    if _lhm_started:
        return
    _lhm_started = True

    def supervise() -> None:
        import json
        backoff = 0.0
        while not _lhm_shutdown:
            if time.monotonic() - _last_read.get("lhm", 0.0) > _GROUP_GRACE["lhm"]:
                _lhm_kill()                   # no GPU/temp key -> stop sweeping
                with _lhm_lock:
                    for k in _lhm:
                        _lhm[k] = None
            _park_if_idle("lhm")              # block at zero cost until a consumer calls _touch()
            if not _lhm_shutdown and (_lhm_proc is None or _lhm_proc.poll() is not None):
                if _lhm_proc is not None:      # the worker exited/crashed -> clear stale readings
                    with _lhm_lock:
                        for k in _lhm:
                            _lhm[k] = None
                if backoff:
                    time.sleep(backoff)
                try:
                    _lhm_spawn()
                except Exception:
                    pass
                backoff = min(10.0, (backoff or 1.0) * 2)   # ramp respawn delay if it keeps dying
            try:
                with open(_lhm_out, encoding="utf-8") as f:
                    data = json.load(f)
                if time.time() - data.get("ts", 0.0) < 5.0:
                    backoff = 0.0             # a healthy sample -> reset the respawn backoff
                    with _lhm_lock:
                        for k in _lhm:
                            _lhm[k] = data.get(k)
                else:
                    _lhm_kill()               # stale -> the worker hung; the poll()-branch respawns it
            except Exception:
                pass
            time.sleep(1.0)

    _arm_park("lhm")
    import atexit
    atexit.register(_lhm_cleanup)
    threading.Thread(target=supervise, name="lhm-supervisor", daemon=True).start()


def _gpu() -> Tuple[str, str, Optional[float]]:
    _start_lhm()
    with _lhm_lock:
        v = _lhm["gpu_load"]
    if v is None:
        return "--", "GPU", None
    return f"{int(v)}%", "GPU", min(1.0, v / 100.0)


def _temp(key: str, cap: str, lo: float = 30.0, hi: float = 100.0) -> Tuple[str, str, Optional[float]]:
    _start_lhm()
    with _lhm_lock:
        v = _lhm[key]
    if v is None:
        return "--", cap, None
    # Map °C over a sensible thermal window (idle floor -> throttle ceiling) so the gauge tracks
    # heat headroom instead of "percent of 100°C" (a 70°C chip shouldn't look like 70% load).
    frac = max(0.0, min(1.0, (v - lo) / (hi - lo)))
    return f"{int(round(v))}°C", cap, frac


def _cpu_temp() -> Tuple[str, str, Optional[float]]:
    return _temp("cpu", "CPU")


def _gpu_temp() -> Tuple[str, str, Optional[float]]:
    return _temp("gpu", "GPU")


def _vram() -> Tuple[str, str, Optional[float]]:
    _start_lhm()
    with _lhm_lock:
        u, tot = _lhm["vram_used"], _lhm["vram_total"]
    if not u or not tot:
        return "--", "VRAM", None
    f = max(0.0, min(1.0, u / tot))
    return f"{int(f * 100)}%", "VRAM", f


def _vram_temp() -> Tuple[str, str, Optional[float]]:
    return _temp("vram_temp", "VRAM", lo=30.0, hi=105.0)   # GDDR6/6X junctions run hotter


def _gpu_clock() -> Tuple[str, str, Optional[float]]:
    _start_lhm()
    with _lhm_lock:
        v = _lhm["gpu_clock"]
    if v is None:
        return "--", "MHZ", None
    return f"{int(v)}", "MHZ", None


def _gpu_fan() -> Tuple[str, str, Optional[float]]:
    _start_lhm()
    with _lhm_lock:
        v = _lhm["gpu_fan"]
    if v is None:
        return "--", "RPM", None
    return f"{int(v)}", "RPM", None


def _cpu_clock() -> Tuple[str, str, Optional[float]]:
    if not psutil:
        return "--", "GHZ", None
    try:
        f = psutil.cpu_freq()
        if not f or not f.current:
            return "--", "GHZ", None
        return f"{f.current / 1000.0:.1f}", "GHZ", min(1.0, f.current / 5000.0)
    except Exception:
        return "--", "GHZ", None


def _ram_gb() -> Tuple[str, str, Optional[float]]:
    if not psutil:
        return "--", "GB", None
    vm = psutil.virtual_memory()
    return f"{vm.used / 1e9:.1f}", "GB", vm.percent / 100.0


def _swap() -> Tuple[str, str, Optional[float]]:
    if not psutil:
        return "--", "SWAP", None
    p = psutil.swap_memory().percent
    return f"{int(p)}%", "SWAP", p / 100.0


def _net_up() -> Tuple[str, str, Optional[float]]:
    if not psutil:
        return "--", "UP KB/S", None
    now = time.time()
    io = psutil.net_io_counters()
    cur = {"t": now, "tx": io.bytes_sent}
    with _net_rate_lock:
        prev = _net_up_last.copy()
        _net_up_last.update(cur)
    if not prev:
        return "0", "UP KB/S", None
    dt = max(0.001, now - prev["t"])
    rate = max(0, cur["tx"] - prev["tx"]) / dt / 1024.0
    return f"{int(rate)}", "UP KB/S", None


def _uptime() -> Tuple[str, str, Optional[float]]:
    if not psutil:
        return "--", "UPTIME", None
    secs = max(0, int(time.time() - psutil.boot_time()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _s = divmod(rem, 60)
    if d:
        return f"{d}d{h}h", "UPTIME", None
    if h:
        return f"{h}h{m}m", "UPTIME", None
    return f"{m}m", "UPTIME", None


def _procs() -> Tuple[str, str, Optional[float]]:
    if not psutil:
        return "--", "PROCS", None
    try:
        return str(len(psutil.pids())), "PROCS", None
    except Exception:
        return "--", "PROCS", None


# ---- state sources (mic mute / caps lock / smart bulb) — frac 1.0=active, 0.0=inactive --------
# Mic mute is read from BOTH the dock loop and the GUI live-picker. _Mic caches a COM endpoint
# bound to the thread that created it, so a single shared object read from two threads is a
# cross-apartment COM violation. A dedicated sampler thread owns its own endpoint; readers just
# read a cached bool — same pattern as the bulb / now-playing samplers below.
_mic_lock = threading.Lock()
_mic_muted: Optional[bool] = None
_mic_started = False


def _start_mic_sampler() -> None:
    global _mic_started
    _touch("mic")
    if _mic_started:
        return
    _mic_started = True

    def run() -> None:
        global _mic_muted
        from .actions import _Mic
        mic = _Mic()                            # this thread's own CoInitialize + endpoint
        while True:
            _park_if_idle("mic")
            try:
                m = mic.is_muted()
            except Exception:
                m = None
            with _mic_lock:
                _mic_muted = m
            time.sleep(1.0)

    _arm_park("mic")
    threading.Thread(target=run, name="mic-sampler", daemon=True).start()


def _mic() -> Tuple[str, str, Optional[float]]:
    """frac 1.0 == MUTED (the 'active'/alert state)."""
    _start_mic_sampler()
    with _mic_lock:
        m = _mic_muted
    if m is None:
        return "--", "MIC", None
    return ("MUTED" if m else "LIVE"), "MIC", (1.0 if m else 0.0)


def _caps() -> Tuple[str, str, Optional[float]]:
    try:
        import ctypes
        on = bool(ctypes.windll.user32.GetKeyState(0x14) & 1)   # VK_CAPITAL toggle bit
        return ("ON" if on else "OFF"), "CAPS", (1.0 if on else 0.0)
    except Exception:
        return "--", "CAPS", None


def _monitors_state() -> Tuple[str, str, Optional[float]]:
    """PC monitors switched on/off by the 'monpower' action (state is the app's own, persisted)."""
    from . import monitors
    off = monitors.is_off()
    if monitors.is_busy():
        return "…", "SCREENS", (0.0 if off else 1.0)
    return ("OFF" if off else "ON"), "SCREENS", (0.0 if off else 1.0)


# ---- internet latency ("ping") — TCP connect timing, no ICMP/raw sockets needed ----------------
_ping_lock = threading.Lock()
_ping_ms: Optional[float] = None        # None = no sample yet; -1.0 = internet DOWN
_ping_started = False
_PING_HOST = ("1.1.1.1", 443)           # Cloudflare anycast edge: always up, geographically near


def _start_ping_sampler() -> None:
    global _ping_started
    _touch("ping")
    if _ping_started:
        return
    _ping_started = True

    def run() -> None:
        global _ping_ms
        import socket
        while True:
            _park_if_idle("ping")
            try:
                t0 = time.perf_counter()
                s = socket.create_connection(_PING_HOST, timeout=2.0)
                val = (time.perf_counter() - t0) * 1000.0
                s.close()
            except OSError:
                val = -1.0              # timeout / no route -> connection is down
            except Exception:
                val = None
            with _ping_lock:
                _ping_ms = val
            time.sleep(2.0)

    _arm_park("ping")
    threading.Thread(target=run, name="ping-sampler", daemon=True).start()


def _ping() -> Tuple[str, str, Optional[float]]:
    """Round-trip latency of a TCP connect to 1.1.1.1:443 — a red-flag DOWN face when unreachable.
    The gauge fills with latency (150 ms = full), and value() feeds the graph style's history."""
    _start_ping_sampler()
    with _ping_lock:
        v = _ping_ms
    if v is None:
        return "--", "PING", None
    if v < 0:
        return "DOWN", "NET", 1.0       # pegged gauge + unmissable text while offline
    return f"{int(round(v))}", "MS", max(0.0, min(1.0, v / 150.0))


# Smart bulb state — polled by a slow background sampler (network call; don't hit it per tick).
_tapo_host = ""                       # set from the config (tapo.bulb_host) at start
_tapo_email: Optional[str] = None
_tapo_pw: Optional[str] = None
_light_lock = threading.Lock()
_light_on: Optional[bool] = None
_light_started = False


def set_tapo_creds(email: Optional[str], pw: Optional[str], host: Optional[str] = None) -> None:
    global _tapo_email, _tapo_pw, _tapo_host
    _tapo_email, _tapo_pw = email, pw
    if host:
        _tapo_host = host


def set_light_state(on: Optional[bool]) -> None:
    """Optimistically push a freshly-known bulb state (e.g. right after the dock toggles it) so the
    live key reflects it immediately, instead of waiting up to one poll interval for confirmation."""
    global _light_on
    with _light_lock:
        _light_on = on


def _start_light_sampler() -> None:
    global _light_started
    _touch("light")
    if _light_started:
        return
    _light_started = True

    def run() -> None:
        global _light_on
        from . import tapo
        while True:
            _park_if_idle("light")
            try:
                if _tapo_email and _tapo_pw and _tapo_host:
                    on = tapo.is_on(_tapo_host, _tapo_email, _tapo_pw)
                    with _light_lock:
                        _light_on = on
            except Exception:
                with _light_lock:
                    _light_on = None
            time.sleep(1.5)        # cheap LAN dev.update(); keep external changes responsive

    _arm_park("light")
    threading.Thread(target=run, name="tapo-light-sampler", daemon=True).start()


def _light() -> Tuple[str, str, Optional[float]]:
    _start_light_sampler()
    with _light_lock:
        on = _light_on
    if on is None:
        return "--", "LIGHT", None
    return ("ON" if on else "OFF"), "LIGHT", (1.0 if on else 0.0)


# ---- Prisma (RGB) on/off ---------------------------------------------------------------------
# Prisma can't report its state, so the RGB live key reflects what the dock last COMMANDED
# (set_rgb_state) combined with whether Prisma is running (process gone -> lights off).
_rgb_lock = threading.Lock()
_rgb_on: Optional[bool] = None
_RGB_PROC = ("prisma.exe", "rgbcommander.exe")
_rgb_run_cache = (0.0, True)                      # (checked_at, running) — throttle process_iter to ~12s


def set_rgb_state(on: Optional[bool]) -> None:
    """Optimistically record Prisma's on/off (Prisma can't be polled) so the RGB live key updates
    the instant the dock fires a colour/effect/off/toggle action."""
    global _rgb_on, _rgb_run_cache
    with _rgb_lock:
        _rgb_on = on
    if on:
        # The dock just commanded (and possibly launched) Prisma — stamp the run cache as running
        # so the live key flips ON instantly instead of holding the stale not-running verdict for
        # a full TTL; the next TTL re-check corrects a failed launch. (A bare invalidation would
        # re-check DURING refresh_live, before the ~1.8s launch completes, and re-cache False.)
        _rgb_run_cache = (time.monotonic(), True)


def _rgb_running() -> bool:
    global _rgb_run_cache
    at, val = _rgb_run_cache
    now = time.monotonic()
    if now - at < 12.0:                            # RGB on/off is a rare, user-driven event
        return val
    running = True
    if psutil is not None:
        running = False
        try:
            for p in psutil.process_iter(["name"]):
                if (p.info.get("name") or "").lower() in _RGB_PROC:
                    running = True
                    break
        except Exception:
            running = True
    _rgb_run_cache = (now, running)
    return running


def rgb_is_on() -> bool:
    """Effective RGB state: on when Prisma is running and the dock hasn't turned it off."""
    with _rgb_lock:
        cmd = _rgb_on
    return _rgb_running() and cmd is not False


def _rgb_state() -> Tuple[str, str, Optional[float]]:
    on = rgb_is_on()
    return ("ON" if on else "OFF"), "RGB", (1.0 if on else 0.0)


# Discord voice state (self-mute / deafen / in-call / volume / mode) via the local RPC — sampler.
_discord_lock = threading.Lock()
_discord_state: Optional[Tuple[bool, bool]] = None   # (mute, deaf) or None if unknown
_discord_channel: Optional[Tuple[bool, int, str]] = None   # (in_call, count, name) or None
_discord_vol: Optional[Tuple[float, float]] = None   # (input %, output %) or None
_discord_mode: Optional[str] = None                  # "PUSH_TO_TALK" / "VOICE_ACTIVITY"
_discord_noise: Optional[bool] = None                # noise-suppression on?
_discord_started = False


def set_discord_volume(inp: Optional[float] = None, out: Optional[float] = None) -> None:
    """Optimistic push so a dial's gauge updates instantly."""
    global _discord_vol
    with _discord_lock:
        i, o = _discord_vol or (None, None)
        if inp is not None:
            i = float(inp)
        if out is not None:
            o = float(out)
        _discord_vol = (i, o)


def set_discord_state(mute: Optional[bool] = None, deaf: Optional[bool] = None) -> None:
    """Optimistically push a freshly-known state (right after the dock toggles it) so the live key
    reflects it immediately instead of waiting for the next poll. deaf implies mute in Discord."""
    global _discord_state
    with _discord_lock:
        m, d = _discord_state or (False, False)
        if mute is not None:
            m = bool(mute)
        if deaf is not None:
            d = bool(deaf)
            if d:
                m = True
        _discord_state = (m, d)


def _start_discord_sampler() -> None:
    global _discord_started
    _touch("discord")
    if _discord_started:
        return
    _discord_started = True

    def run() -> None:
        global _discord_state, _discord_channel, _discord_vol, _discord_mode, _discord_noise
        from . import discord as dc
        while True:
            _park_if_idle("discord")
            try:
                if dc.configured():
                    s = dc.get_settings()              # one call -> mute/deaf/volume/mode/noise
                    iv = (s.get("input") or {}).get("volume")
                    ov = (s.get("output") or {}).get("volume")
                    md = (s.get("mode") or {}).get("type")
                    with _discord_lock:
                        _discord_state = (bool(s.get("mute")), bool(s.get("deaf")))
                        if iv is not None or ov is not None:
                            _discord_vol = (float(iv) if iv is not None else None,
                                            float(ov) if ov is not None else None)
                        _discord_mode = md
                        if "noise_suppression" in s:
                            _discord_noise = bool(s.get("noise_suppression"))
                    try:
                        ch = dc.get_channel()
                        with _discord_lock:
                            _discord_channel = ch
                    except Exception:
                        pass
            except Exception:
                pass                            # not running / not authed -> keep last (or "--")
            time.sleep(1.5)

    _arm_park("discord")
    threading.Thread(target=run, name="discord-sampler", daemon=True).start()


def _discord_src() -> Tuple[str, str, Optional[float]]:
    """Combined: frac 1.0 == muted/deafened (the 'active'/alert state)."""
    _start_discord_sampler()
    with _discord_lock:
        st = _discord_state
    if st is None:
        return "--", "DISCORD", None
    mute, deaf = st
    if deaf:
        return "DEAF", "DISCORD", 1.0
    if mute:
        return "MUTED", "DISCORD", 1.0
    return "LIVE", "DISCORD", 0.0


def _discord_mic() -> Tuple[str, str, Optional[float]]:
    """Self-mute only (mic glyph). frac 1.0 == muted."""
    _start_discord_sampler()
    with _discord_lock:
        st = _discord_state
    if st is None:
        return "--", "DMIC", None
    return ("MUTED" if st[0] else "LIVE"), "DMIC", (1.0 if st[0] else 0.0)


def _discord_deaf() -> Tuple[str, str, Optional[float]]:
    """Deafen only (headphones glyph). frac 1.0 == deafened."""
    _start_discord_sampler()
    with _discord_lock:
        st = _discord_state
    if st is None:
        return "--", "DDEAF", None
    return ("DEAF" if st[1] else "HEAR"), "DDEAF", (1.0 if st[1] else 0.0)


def _discord_voice() -> Tuple[str, str, Optional[float]]:
    """In a voice call or not. frac 1.0 == connected."""
    _start_discord_sampler()
    with _discord_lock:
        ch = _discord_channel
    if ch is None:
        return "--", "DCALL", None
    return ("IN CALL" if ch[0] else "IDLE"), "DCALL", (1.0 if ch[0] else 0.0)


def _discord_count() -> Tuple[str, str, Optional[float]]:
    """How many people are in your voice channel."""
    _start_discord_sampler()
    with _discord_lock:
        ch = _discord_channel
    if ch is None:
        return "--", "IN CALL", None
    return (str(ch[1]) if ch[0] else "0"), "IN CALL", None


def _discord_invol() -> Tuple[str, str, Optional[float]]:
    """Discord mic input volume (0..100%)."""
    _start_discord_sampler()
    with _discord_lock:
        v = _discord_vol[0] if _discord_vol else None
    if v is None:
        return "--", "MIC VOL", None
    return f"{int(round(v))}%", "MIC VOL", max(0.0, min(1.0, v / 100.0))


def _discord_outvol() -> Tuple[str, str, Optional[float]]:
    """Discord output volume (0..200%, shown vs 200)."""
    _start_discord_sampler()
    with _discord_lock:
        v = _discord_vol[1] if _discord_vol else None
    if v is None:
        return "--", "OUT VOL", None
    return f"{int(round(v))}%", "OUT VOL", max(0.0, min(1.0, v / 200.0))


def _discord_mode_src() -> Tuple[str, str, Optional[float]]:
    """Push-to-talk vs Voice activity. frac 1.0 == push-to-talk (the 'active' choice)."""
    _start_discord_sampler()
    with _discord_lock:
        m = _discord_mode
    if m is None:
        return "--", "DMODE", None
    ptt = (m == "PUSH_TO_TALK")
    return ("PTT" if ptt else "VOICE"), "DMODE", (1.0 if ptt else 0.0)


def _discord_noise_src() -> Tuple[str, str, Optional[float]]:
    """Discord noise suppression on/off. frac 1.0 == on."""
    _start_discord_sampler()
    with _discord_lock:
        n = _discord_noise
    if n is None:
        return "--", "DNOISE", None
    return ("ON" if n else "OFF"), "DNOISE", (1.0 if n else 0.0)


# OBS Studio state (streaming / recording / virtual-cam / scene / replay) over its WebSocket — sampler.
_obs_lock = threading.Lock()
_obs_state: Optional[dict] = None                    # {streaming, recording, rec_paused, vcam, scene, replay} or None
_obs_started = False


def set_obs_state(**kw) -> None:
    """Optimistic push right after the dock toggles an OBS output, so the live key flips instantly
    instead of waiting for the next poll."""
    global _obs_state
    with _obs_lock:
        cur = dict(_obs_state or {})
        cur.update({k: v for k, v in kw.items() if v is not None})
        _obs_state = cur


def _start_obs_sampler() -> None:
    global _obs_started
    _touch("obs")
    if _obs_started:
        return
    _obs_started = True

    def run() -> None:
        global _obs_state
        from . import obs
        while True:
            _park_if_idle("obs")
            try:
                def _rd(req):
                    r = obs.request(req)
                    return (r or {}).get("responseData", {}) if r else None
                st = _rd("GetStreamStatus")
                if st is None:
                    raise RuntimeError("OBS unreachable")     # bail fast: WS off / OBS not running
                rc = _rd("GetRecordStatus") or {}
                vc = _rd("GetVirtualCamStatus") or {}
                sc = _rd("GetCurrentProgramScene") or {}
                rp = _rd("GetReplayBufferStatus") or {}
                with _obs_lock:
                    _obs_state = {
                        "streaming": bool(st.get("outputActive")),
                        "recording": bool(rc.get("outputActive")),
                        "rec_paused": bool(rc.get("outputPaused")),
                        "vcam": bool(vc.get("outputActive")),
                        "scene": sc.get("currentProgramSceneName") or sc.get("sceneName") or "",
                        "replay": bool(rp.get("outputActive")),
                    }
                time.sleep(1.5)
            except Exception:
                with _obs_lock:
                    _obs_state = None                          # OBS down -> "--"
                time.sleep(3.0)                                # back off while unreachable

    _arm_park("obs")
    threading.Thread(target=run, name="obs-sampler", daemon=True).start()


def _obs_field(key):
    _start_obs_sampler()
    with _obs_lock:
        return None if _obs_state is None else _obs_state.get(key)


def _obs_streaming() -> Tuple[str, str, Optional[float]]:
    """frac 1.0 == live (streaming)."""
    v = _obs_field("streaming")
    if v is None:
        return "--", "STREAM", None
    return ("LIVE" if v else "OFF"), "STREAM", (1.0 if v else 0.0)


def _obs_recording() -> Tuple[str, str, Optional[float]]:
    """frac 1.0 == recording (PAUSE if paused)."""
    _start_obs_sampler()
    with _obs_lock:
        s = dict(_obs_state) if _obs_state is not None else None
    if s is None:
        return "--", "REC", None
    if s.get("recording"):
        return ("PAUSE" if s.get("rec_paused") else "REC"), "REC", 1.0
    return "OFF", "REC", 0.0


def _obs_virtualcam() -> Tuple[str, str, Optional[float]]:
    v = _obs_field("vcam")
    if v is None:
        return "--", "V-CAM", None
    return ("ON" if v else "OFF"), "V-CAM", (1.0 if v else 0.0)


def _obs_scene() -> Tuple[str, str, Optional[float]]:
    v = _obs_field("scene")
    if not v:
        return "--", "SCENE", None
    return v, "SCENE", None


def _obs_replay() -> Tuple[str, str, Optional[float]]:
    v = _obs_field("replay")
    if v is None:
        return "--", "REPLAY", None
    return ("ON" if v else "OFF"), "REPLAY", (1.0 if v else 0.0)


# Now playing — the Windows system media session (SMTC) via WinRT, polled on a slow background
# thread (the API is async/IO; never hit it per render tick). Reports the current track's title +
# artist (the metadata the user's own media app already publishes) and whether it's playing.
_media_lock = threading.Lock()
_media: Optional[Tuple[str, str, bool]] = None   # (title, artist, playing) or None
_media_art = None                                # the current track's cover image (PIL) or None
_media_art_title = None                          # the title _media_art belongs to (refetch on change)
_media_pos = None                                # (position_s, duration_s, sampled_monotonic, playing)
_media_started = False
_KEEP = object()                                 # sentinel: "track unchanged, keep the cached art"


def _start_media_sampler() -> None:
    global _media_started
    _touch("media")
    if _media_started:
        return
    _media_started = True

    def run() -> None:
        global _media, _media_art, _media_art_title
        import asyncio
        try:
            from winrt.windows.media.control import \
                GlobalSystemMediaTransportControlsSessionManager as MM
        except Exception:
            return                          # WinRT not present -> the media key just shows "--"

        async def fetch(prev_title):
            mgr = await MM.request_async()
            s = mgr.get_current_session()
            if not s:
                return None
            info = await s.try_get_media_properties_async()
            pb = s.get_playback_info()
            title = (info.title or "").strip()
            artist = (info.artist or "").strip()
            if not title and not artist:
                return None
            playing = int(pb.playback_status) == 4                  # 4 == Playing
            pos_s = dur_s = None                                     # track position for a progress bar
            try:
                tl = s.get_timeline_properties()
                start_s = tl.start_time.total_seconds()
                pos_s = tl.position.total_seconds() - start_s   # position is absolute -> make it relative to start
                dur_s = tl.end_time.total_seconds() - start_s
            except Exception:
                pos_s = dur_s = None
            art = _KEEP
            if title != prev_title:                                 # only fetch cover when track changes
                art = None
                stream = r = None
                try:
                    thumb = info.thumbnail
                    if thumb:
                        from winrt.windows.storage.streams import DataReader
                        import io as _io
                        from PIL import Image as _Image
                        stream = await thumb.open_read_async()
                        size = stream.size
                        if size:
                            r = DataReader(stream)
                            await r.load_async(size)
                            im = _Image.open(_io.BytesIO(bytes(r.read_buffer(size)))).convert("RGB")
                            im.thumbnail((256, 256), _Image.LANCZOS)
                            art = im
                except Exception:
                    art = None
                finally:
                    for obj in (r, stream):                          # don't leak native WinRT handles
                        try:
                            if obj is not None:
                                obj.close()
                        except Exception:
                            pass
            return (title, artist, playing, art, pos_s, dur_s)

        loop = asyncio.new_event_loop()                             # one loop for the thread's life
        asyncio.set_event_loop(loop)
        while True:
            _park_if_idle("media")
            try:
                res = loop.run_until_complete(fetch(_media_art_title))
                with _media_lock:
                    if res is None:
                        _media = _media_art = _media_art_title = _media_pos = None
                    else:
                        title, artist, playing, art, pos_s, dur_s = res
                        _media = (title, artist, playing)
                        _media_pos = ((pos_s, dur_s, time.monotonic(), playing)
                                      if pos_s is not None and dur_s else None)
                        if art is not _KEEP:
                            _media_art, _media_art_title = art, title
            except Exception:
                with _media_lock:                                   # keep art consistent with its title key
                    _media = _media_art = _media_art_title = _media_pos = None
            time.sleep(2.0)

    _arm_park("media")
    threading.Thread(target=run, name="nowplaying-sampler", daemon=True).start()


def media_artwork():
    """The current track's cover image (PIL RGB) or None — drawn behind the now-playing key."""
    with _media_lock:
        return _media_art


def media_snapshot() -> Tuple[str, str, Optional[float], object]:
    """(text, caption, frac, artwork) read together under ONE lock, so a track change between
    reads can't pair a new cover with an old title (or vice-versa)."""
    _start_media_sampler()
    with _media_lock:
        m, art = _media, _media_art
    if not m:
        return "--", "MEDIA", None, None
    title, artist, playing = m
    return (title or artist or "--"), artist, (1.0 if playing else 0.0), art


def media_position() -> Optional[float]:
    """The current track's play position as a 0..1 fraction, or None if unknown — interpolated
    forward from the last sample so an idle progress bar advances smoothly between 2 s polls."""
    with _media_lock:
        mp = _media_pos
    if not mp:
        return None
    pos, dur, at, playing = mp
    if not dur or dur <= 0:
        return None
    if playing:
        pos = pos + (time.monotonic() - at)          # advance since the sample was taken
    return max(0.0, min(1.0, pos / dur))


def _media_now() -> Tuple[str, str, Optional[float]]:
    _start_media_sampler()
    with _media_lock:
        m = _media
    if not m:
        return "--", "MEDIA", None
    title, artist, playing = m
    return (title or artist or "--"), artist, (1.0 if playing else 0.0)


# ---- weather (Open-Meteo — no API key) -------------------------------------------------------
# ONE forecast call feeds the whole "Weather center" folder (current + today + 6-day). The last
# good reading + resolved location are cached to disk so a relaunch shows data INSTANTLY instead of
# waiting on the cold IP-geo -> geocode -> forecast HTTP chain; a fetch error keeps the last reading
# (marked stale) rather than blanking the tiles.
_weather_started = False
_weather_state: Optional[dict] = None
_weather_lock = threading.Lock()
_weather_place = ""          # user override: a city name or "lat,lon"; "" = locate by IP
_weather_loc = None          # resolved (lat, lon, label), cached
_weather_cache_loaded = False
_weather_units = "c"         # "c" = °C/km·h, "f" = °F/mph
_weather_refetch = threading.Event()   # set -> sampler refetches now (interrupts the poll sleep)


def set_weather_units(units: Optional[str]) -> None:
    """Set temperature/wind units: 'c' (°C, km/h) or 'f' (°F, mph). Forces a refetch on change."""
    global _weather_units
    u = "f" if str(units or "").lower().startswith("f") else "c"
    if u != _weather_units:
        _weather_units = u
        _weather_refetch.set()      # refetch in the new units; keep showing the old value until it lands
        _touch("weather")


def set_weather_location(place: Optional[str]) -> None:
    """Set the weather location (a city name or 'lat,lon'); empty = locate by IP."""
    global _weather_place, _weather_loc, _weather_state
    place = (place or "").strip()
    if place != _weather_place:
        _weather_place = place
        _weather_loc = None                       # force a re-resolve
        with _weather_lock:
            _weather_state = None
        _weather_refetch.set()                     # refetch NOW even if the sampler is mid-poll-sleep
        _touch("weather")                          # also un-park the sampler if it was idle-parked


def _isnum(s) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _wxf(v):
    """float() that tolerates None / bad values (Open-Meteo can omit a field)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _http_json(url, timeout=6):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "AjazzDock"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _weather_cache_file():
    try:
        from .config import config_dir
        return os.path.join(config_dir(), "weather_cache.json")   # NOT config.json — a disposable cache
    except Exception:
        return None


def _load_weather_cache() -> None:
    """Seed _weather_state (stale) + the resolved location from disk for an instant first paint."""
    global _weather_state, _weather_loc
    f = _weather_cache_file()
    if not f or not os.path.exists(f):
        return
    try:
        with open(f, "r", encoding="utf-8") as fh:
            c = json.load(fh)
        same_place = c.get("place", "") == _weather_place      # don't show a different city's data
        st = c.get("state")
        same_units = isinstance(st, dict) and (st.get("units") or "c") == _weather_units
        if same_place and same_units and isinstance(st, dict) and st.get("temp") is not None:
            st["stale"] = True
            with _weather_lock:
                _weather_state = st
        loc = c.get("loc")
        if same_place and _weather_loc is None and isinstance(loc, (list, tuple)) and len(loc) >= 2:
            try:                                  # tolerate a hand-edited / legacy / partial cache
                _weather_loc = (float(loc[0]), float(loc[1]), str(loc[2]) if len(loc) > 2 else "")
            except (TypeError, ValueError):
                _weather_loc = None               # bad shape -> re-resolve instead of looping on it
    except Exception:
        pass


def _save_weather_cache() -> None:
    f = _weather_cache_file()
    if not f:
        return
    try:
        with _weather_lock:
            st = dict(_weather_state) if _weather_state else None
        data = {"place": _weather_place,
                "loc": list(_weather_loc) if _weather_loc else None,
                "state": st}
        tmp = f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, f)                        # atomic; never leaves a half-written cache
    except Exception:
        pass


def _resolve_location():
    global _weather_loc
    if _weather_loc is not None:
        return _weather_loc
    place = _weather_place
    try:
        parts = place.split(",")
        if place and len(parts) == 2 and all(_isnum(p) for p in parts):
            _weather_loc = (float(parts[0]), float(parts[1]), place)
        elif place:
            import urllib.parse
            g = _http_json("https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&name="
                           + urllib.parse.quote(place))
            res = (g.get("results") or [None])[0]
            if res:
                _weather_loc = (res["latitude"], res["longitude"], res.get("name") or place)
        if _weather_loc is None:                  # no override (or geocode failed) -> IP geolocation
            ip = _http_json("http://ip-api.com/json/?fields=lat,lon,city")
            if ip.get("lat") is not None:
                _weather_loc = (ip["lat"], ip["lon"], ip.get("city") or "")
    except Exception:
        _weather_loc = None
    return _weather_loc


def _parse_weather(wj, label, units=None) -> dict:
    cur = wj.get("current") or {}
    daily = wj.get("daily") or {}
    t = cur.get("temperature_2m")
    if t is None:
        raise RuntimeError("no reading")

    def col(k):
        v = daily.get(k)
        return v if isinstance(v, list) else []

    def first(k, d=None):
        a = col(k)
        return a[0] if a else d

    def hhmm(s):
        s = str(s or "")
        return s[11:16] if len(s) >= 16 else "--:--"

    times, dmax, dmin, dcode = col("time"), col("temperature_2m_max"), col("temperature_2m_min"), col("weather_code")
    days = []
    for i, ds in enumerate(times):
        try:
            dow = datetime.date.fromisoformat(ds).strftime("%a").upper()
        except Exception:
            dow = ""
        days.append({"dow": dow,
                     "hi": _wxf(dmax[i]) if i < len(dmax) else None,
                     "lo": _wxf(dmin[i]) if i < len(dmin) else None,
                     "code": int(dcode[i]) if i < len(dcode) and dcode[i] is not None else 0})
    return {
        "label": label,
        "temp": _wxf(t), "feels": _wxf(cur.get("apparent_temperature", t)),
        "code": int(cur.get("weather_code", 0) or 0),
        "humidity": _wxf(cur.get("relative_humidity_2m")),
        "wind": _wxf(cur.get("wind_speed_10m")),
        "precip_mm": _wxf(cur.get("precipitation")),
        "uv": _wxf(first("uv_index_max")),
        "pop": _wxf(first("precipitation_probability_max")),
        "hi": _wxf(first("temperature_2m_max")), "lo": _wxf(first("temperature_2m_min")),
        "sunrise": hhmm(first("sunrise")), "sunset": hhmm(first("sunset")),
        "is_day": int(cur.get("is_day", 1) or 0),
        "units": units or _weather_units,
        "days": days,
        "stale": False,
    }


def _start_weather_sampler() -> None:
    global _weather_started, _weather_cache_loaded
    _touch("weather")
    if _weather_started:
        return
    _weather_started = True
    if not _weather_cache_loaded:                 # show last-known immediately, before the first fetch
        _weather_cache_loaded = True
        _load_weather_cache()

    def run() -> None:
        global _weather_state
        while True:
            _park_if_idle("weather")
            _weather_refetch.clear()               # arm BEFORE fetching so a location change mid-cycle isn't lost
            try:
                loc = _resolve_location()
                if loc is None:
                    raise RuntimeError("no location")
                lat, lon, label = loc
                units = _weather_units                 # snapshot: the stamped units must match the request,
                tunit = "fahrenheit" if units == "f" else "celsius"   # even if the user toggles mid-fetch
                wunit = "mph" if units == "f" else "kmh"
                wj = _http_json(
                    f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                    "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "precipitation,weather_code,wind_speed_10m,is_day"
                    "&daily=weather_code,temperature_2m_max,temperature_2m_min,uv_index_max,"
                    "precipitation_probability_max,sunrise,sunset"
                    f"&timezone=auto&forecast_days=7&temperature_unit={tunit}&wind_speed_unit={wunit}",
                    timeout=8)
                st = _parse_weather(wj, label, units)
                with _weather_lock:
                    _weather_state = st
                _save_weather_cache()
                wait = 900                          # weather changes slowly: poll every 15 min
            except Exception:
                with _weather_lock:
                    if _weather_state:             # keep the last reading visible, just flag it stale
                        _weather_state = {**_weather_state, "stale": True}
                wait = 60                           # back off (offline / rate-limited)
            _weather_refetch.wait(wait)             # interruptible: a location change refetches at once

    _arm_park("weather")
    threading.Thread(target=run, name="weather-sampler", daemon=True).start()


def _wmo_short(code: int) -> str:
    c = int(code)
    if c == 0: return "Clear"
    if c in (1, 2): return "Cloudy"
    if c == 3: return "Overcast"
    if c in (45, 48): return "Fog"
    if 51 <= c <= 57: return "Drizzle"
    if (61 <= c <= 67) or (80 <= c <= 82): return "Rain"
    if (71 <= c <= 77) or (85 <= c <= 86): return "Snow"
    if 95 <= c <= 99: return "Storm"
    return "Weather"


def _wx():
    """Ensure the sampler runs + return the current weather state dict (or None)."""
    _start_weather_sampler()
    with _weather_lock:
        return _weather_state


def _weather() -> Tuple[str, str, Optional[float]]:
    st = _wx()
    if not st or st.get("temp") is None:
        return "--", "WEATHER", None
    return f"{round(st['temp'])}°", _wmo_short(st["code"]), None


def weather_payload() -> Optional[dict]:
    """A small render payload for the weather tiles: current condition, day/night, place + hi/lo/feels.
    Returns None if there's no reading yet. Read from cached state — cheap, safe to call every tick."""
    st = _wx()
    if not st:
        return None

    def _r(v):
        return None if v is None else round(v)
    return {
        "cond": _wmo_short(st.get("code", 0)).lower(),
        "night": not bool(st.get("is_day", 1)),
        "label": st.get("label") or "",
        "temp": _r(st.get("temp")), "feels": _r(st.get("feels")),
        "hi": _r(st.get("hi")), "lo": _r(st.get("lo")),
        "stale": bool(st.get("stale")),
    }


def solar_times() -> Optional[Tuple[int, int, bool]]:
    """(sunrise_minutes, sunset_minutes, is_day) for the resolved location, or None if not known yet.
    Reads the cached weather state (and starts the sampler, so the Solar-sky idle screen gets real
    sun times). Minutes are since local midnight; sunrise/sunset come from Open-Meteo daily data."""
    st = _wx()
    if not st:
        return None

    def _min(s):
        try:
            hh, mm = str(s).split(":")
            return int(hh) * 60 + int(mm)
        except (ValueError, AttributeError, TypeError):
            return None
    sr, ss = _min(st.get("sunrise")), _min(st.get("sunset"))
    if sr is None or ss is None or ss <= sr:
        return None
    return (sr, ss, bool(st.get("is_day", 1)))


def weather_place_label() -> str:
    """The resolved location's display name (city or 'lat,lon'), or '' if not resolved yet."""
    st = _wx()
    if st and st.get("label"):
        return st["label"]
    loc = _weather_loc
    return loc[2] if loc and len(loc) > 2 else ""


def _wx_feels() -> Tuple[str, str, Optional[float]]:
    st = _wx()
    if not st or st.get("feels") is None:
        return "--", "FEELS", None
    return f"{round(st['feels'])}°", "FEELS", None


def _wx_humidity() -> Tuple[str, str, Optional[float]]:
    st = _wx()
    if not st or st.get("humidity") is None:
        return "--", "HUMIDITY", None
    h = st["humidity"]
    return f"{round(h)}%", "HUMIDITY", max(0.0, min(1.0, h / 100.0))


def _wx_wind() -> Tuple[str, str, Optional[float]]:
    st = _wx()
    if not st or st.get("wind") is None:
        return "--", "WIND", None
    w = st["wind"]
    if (st.get("units") or _weather_units) == "f":
        return f"{round(w)}", "MPH", max(0.0, min(1.0, w / 40.0))
    return f"{round(w)}", "KM/H", max(0.0, min(1.0, w / 60.0))


def _wx_uv() -> Tuple[str, str, Optional[float]]:
    st = _wx()
    if not st or st.get("uv") is None:
        return "--", "UV INDEX", None
    uv = st["uv"]
    return f"{uv:.0f}", "UV INDEX", max(0.0, min(1.0, uv / 11.0))


def _wx_precip() -> Tuple[str, str, Optional[float]]:
    st = _wx()
    if not st or st.get("pop") is None:
        return "--", "RAIN", None
    p = st["pop"]
    return f"{round(p)}%", "RAIN", max(0.0, min(1.0, p / 100.0))


def _wx_hi() -> Tuple[str, str, Optional[float]]:
    st = _wx()
    if not st or st.get("hi") is None:
        return "--", "HIGH", None
    return f"{round(st['hi'])}°", "HIGH", None


def _wx_lo() -> Tuple[str, str, Optional[float]]:
    st = _wx()
    if not st or st.get("lo") is None:
        return "--", "LOW", None
    return f"{round(st['lo'])}°", "LOW", None


def _wx_sunrise() -> Tuple[str, str, Optional[float]]:
    st = _wx()
    return ((st or {}).get("sunrise") or "--:--"), "SUNRISE", None


def _wx_sunset() -> Tuple[str, str, Optional[float]]:
    st = _wx()
    return ((st or {}).get("sunset") or "--:--"), "SUNSET", None


def _wx_day(n: int) -> Tuple[str, str, Optional[float]]:
    """Forecast tile for day +n. Packs (dow|low|condition) into the caption; the renderer splits it."""
    st = _wx()
    days = (st or {}).get("days") or []
    if n >= len(days):
        return "--", "|--|", None
    dd = days[n]
    dow = dd.get("dow") or ""
    hi, lo, code = dd.get("hi"), dd.get("lo"), dd.get("code", 0)
    hit = f"{round(hi)}°" if hi is not None else "--"
    lot = f"{round(lo)}°" if lo is not None else "--"
    return hit, f"{dow}|{lot}|{_wmo_short(code).lower()}", None


# ---- VRChat OSC ------------------------------------------------------------------------------
# Live tiles read ONLY the vrcosc cache (a background UDP listener fills it), so a dock tick never
# touches the network. A parameter tile uses the pseudo-source "vrc:<ParamName>" — see value().
VRC_PREFIX = "vrc:"


def _vrc_fmt(v):
    """(text, frac) for a parameter value: bools as ON/OFF, ints plain, floats trimmed."""
    if v is None:
        return "--", None
    if isinstance(v, bool):
        return ("ON" if v else "OFF"), (1.0 if v else 0.0)
    if isinstance(v, int):
        return str(v), None
    if isinstance(v, float):
        return (f"{v:.2f}".rstrip("0").rstrip(".") or "0"), max(0.0, min(1.0, v))
    return str(v)[:8], None


def _vrc_param(name: str) -> Tuple[str, str, Optional[float]]:
    from . import vrcosc
    vrcosc.start()
    text, frac = _vrc_fmt(vrcosc.get_param(name))
    cap = (name.split("/")[-1] or "VRC")[:9].upper()
    return text, cap, frac


def _vrc_conn() -> Tuple[str, str, Optional[float]]:
    from . import vrcosc
    vrcosc.start()
    st = vrcosc.status()
    if not st["connected"]:
        return "OFF", "VRCHAT", 0.0
    nm = st.get("avatar_name") or "ON"
    return str(nm)[:9], "VRCHAT", 1.0


def _vrc_mute() -> Tuple[str, str, Optional[float]]:
    from . import vrcosc
    vrcosc.start()
    v = vrcosc.get_param("MuteSelf")
    if v is None:
        return "--", "VRC MIC", None
    return ("MUTED" if v else "LIVE"), "VRC MIC", (1.0 if v else 0.0)


def _vrc_afk() -> Tuple[str, str, Optional[float]]:
    from . import vrcosc
    vrcosc.start()
    v = vrcosc.get_param("AFK")
    if v is None:
        return "--", "VRC AFK", None
    return ("AFK" if v else "HERE"), "VRC AFK", (1.0 if v else 0.0)


_PROVIDERS: Dict[str, Callable[[], Tuple[str, str, Optional[float]]]] = {
    "clock": _clock, "clock_sec": _clock_sec, "date": _date, "cpu": _cpu, "ram": _ram,
    "gpu": _gpu, "cpu_temp": _cpu_temp, "gpu_temp": _gpu_temp,
    "cpu_clock": _cpu_clock, "vram": _vram, "vram_temp": _vram_temp,
    "gpu_clock": _gpu_clock, "gpu_fan": _gpu_fan, "ram_gb": _ram_gb, "swap": _swap,
    "disk": _disk, "battery": _battery, "net": _net, "net_up": _net_up,
    "uptime": _uptime, "procs": _procs,
    "mic": _mic, "caps": _caps, "light": _light, "rgb": _rgb_state, "ping": _ping,
    "monitors": _monitors_state,
    "discord": _discord_src,
    "discord_mic": _discord_mic, "discord_deaf": _discord_deaf,
    "discord_voice": _discord_voice, "discord_count": _discord_count,
    "discord_invol": _discord_invol, "discord_outvol": _discord_outvol,
    "discord_mode": _discord_mode_src, "discord_noise": _discord_noise_src,
    "obs_streaming": _obs_streaming, "obs_recording": _obs_recording,
    "obs_virtualcam": _obs_virtualcam, "obs_scene": _obs_scene, "obs_replay": _obs_replay,
    "media": _media_now,
    "vrc": _vrc_conn, "vrc_mute": _vrc_mute, "vrc_afk": _vrc_afk,
    "weather": _weather,
    "wx_feels": _wx_feels, "wx_humidity": _wx_humidity, "wx_wind": _wx_wind,
    "wx_uv": _wx_uv, "wx_precip": _wx_precip, "wx_hi": _wx_hi, "wx_lo": _wx_lo,
    "wx_sunrise": _wx_sunrise, "wx_sunset": _wx_sunset,
    "wx_d1": lambda: _wx_day(1), "wx_d2": lambda: _wx_day(2), "wx_d3": lambda: _wx_day(3),
    "wx_d4": lambda: _wx_day(4), "wx_d5": lambda: _wx_day(5), "wx_d6": lambda: _wx_day(6),
}


def source_label(source: str) -> str:
    if source.startswith(VRC_PREFIX):
        return "VRChat · " + source[len(VRC_PREFIX):]
    m = _META.get(source)
    return m[0] if m else source


def source_kind(source: str) -> str:
    if source.startswith(VRC_PREFIX):
        return "state"
    m = _META.get(source)
    return m[1] if m else "gauge"


def source_short(source: str) -> str:
    return LIVE_SHORT.get(source, source_label(source))


def source_emoji(source: str) -> str:
    return LIVE_EMOJI.get(source, "📊")


def source_ids() -> List[str]:
    return list(_META.keys())


_history: Dict[str, "deque"] = {}
_hist_last: Dict[str, float] = {}


def _push_history(source: str, frac: Optional[float]) -> None:
    """Append a sample to the source's rolling history (~one per second) for the graph style."""
    if frac is None:
        return
    now = time.time()
    if now - _hist_last.get(source, 0.0) >= 0.8:        # de-dupe multiple renders within a tick
        _hist_last[source] = now
        _history.setdefault(source, deque(maxlen=48)).append(float(frac))


def history(source: str) -> List[float]:
    return list(_history.get(source, ()))


def value(source: str) -> Tuple[str, str, Optional[float], str]:
    """Return (text, caption, frac, kind) for a live source id."""
    if source.startswith(VRC_PREFIX):          # "vrc:<ParamName>" -> any avatar parameter
        try:
            text, caption, frac = _vrc_param(source[len(VRC_PREFIX):])
            return text, caption, frac, "state"
        except Exception:
            return "--", "VRC", None, "state"
    fn = _PROVIDERS.get(source)
    kind = source_kind(source)
    if not fn:
        return "--", source.upper(), None, kind
    try:
        text, caption, frac = fn()
        _push_history(source, frac)
        return text, caption, frac, kind
    except Exception:
        return "--", source.upper(), None, kind


def prime() -> None:
    """Warm up the non-blocking CPU sampler (the first cpu_percent() call returns 0.0).

    Does NOT start the LibreHardwareMonitor thread — that .NET/ring0 sensor sweep is expensive and
    every LHM consumer (_gpu/_temp/_vram/_gpu_clock/_gpu_fan) starts it lazily on first read, so a
    user with no GPU/temp key never pays for it.
    """
    if psutil:
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
