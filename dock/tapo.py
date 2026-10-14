"""Direct local control of a Tapo bulb via python-kasa — no Lumos app needed.

python-kasa is async; we keep one long-lived background asyncio loop and run coroutines on it
(mirrors Lumos's proven approach). Credentials (TP-Link account email + password) come from the
AjazzDock config; they're auto-imported once from a local Lumos config.json if present.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Optional, Tuple

_loop = None
_loop_lock = threading.Lock()
_devices: dict = {}            # host -> kasa Device (bound to the bg loop)
_dev_lock = threading.Lock()


def _bg_loop():
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="tapo-loop", daemon=True).start()
        return _loop


def _run(coro, timeout: float = 20.0):
    fut = asyncio.run_coroutine_threadsafe(coro, _bg_loop())
    try:
        return fut.result(timeout=timeout)
    except BaseException:
        # .result(timeout) does NOT stop the coroutine. Without this cancel an unreachable bulb
        # keeps the task queued on tapo-loop holding kasa's per-device _query_lock (~23 s of
        # retries), while the 1.5 s sampler enqueues another - the backlog grows without bound
        # and every key press then waits behind it.
        fut.cancel()
        raise


async def _connect(host: str, user: str, pw: str):
    from kasa import Discover
    dev = await Discover.discover_single(host, username=user, password=pw)
    if dev is None:
        raise RuntimeError(f"No device at {host}")
    await dev.update()
    return dev


def _get_dev(host: str, user: str, pw: str, reconnect: bool = False, timeout: float = 20.0):
    with _dev_lock:
        dev = _devices.get(host)
    if dev is None or reconnect:
        dev = _run(_connect(host, user, pw), timeout=timeout)
        with _dev_lock:
            _devices[host] = dev
    return dev


def _light(dev):
    from kasa import Module
    return dev.modules.get(Module.Light)


def apply(host: str, user: str, pw: str, mode: str,
          hsv: Optional[Tuple[int, int, int]] = None,
          brightness: Optional[int] = None, step: Optional[int] = None) -> Optional[bool]:
    """Apply a command to the bulb. Reconnects once on failure.

    mode: on / off / toggle / brightness (set) / brightness_up / brightness_down /
          color (set) / hue_up / hue_down. The *_up / *_down modes read the bulb's current
          level and nudge it by ``step`` — ideal for a rotary encoder (turn = dim/brighten/cycle).

    Returns the resulting power state (True=on / False=off) when known, so the caller can update
    a live key instantly without waiting for the next network poll; None if it can't be determined.
    """
    async def _do(dev):
        if mode == "on":
            await dev.turn_on()
            return True
        if mode == "off":
            await dev.turn_off()
            return False
        if mode == "toggle":
            await dev.update()                       # fresh power state before flipping
            if dev.is_on:
                await dev.turn_off()
                return False
            await dev.turn_on()
            return True
        if mode == "brightness":
            await dev.turn_on()                      # brightness implies the bulb is on
            light = _light(dev)
            if light is not None and brightness is not None:
                await light.set_brightness(max(1, min(100, int(brightness))))
            return True
        if mode in ("brightness_up", "brightness_down"):
            await dev.update()
            light = _light(dev)
            if light is None:
                return None
            cur = int(getattr(light, "brightness", None) or 50)
            d = abs(int(step)) if step else 10
            new = max(1, min(100, cur + (d if mode == "brightness_up" else -d)))
            if not dev.is_on:
                await dev.turn_on()
            await light.set_brightness(new)
            return True
        if mode in ("hue_up", "hue_down"):
            await dev.update()
            light = _light(dev)
            if light is None:
                return None
            cur = getattr(light, "hsv", None) or (0, 100, 100)
            h, s, v = int(cur[0]), int(cur[1]), int(cur[2])
            d = abs(int(step)) if step else 30
            nh = (h + (d if mode == "hue_up" else -d)) % 360
            if not dev.is_on:
                await dev.turn_on()
            await light.set_hsv(nh, max(1, s or 100), max(1, v or 100))
            return True
        if mode == "color":
            await dev.turn_on()
            light = _light(dev)
            if light is not None and hsv is not None:
                await light.set_hsv(int(hsv[0]), int(hsv[1]), int(hsv[2]))
            elif light is not None and brightness is not None:
                await light.set_brightness(int(brightness))
            return True
        return None

    dev = _get_dev(host, user, pw)
    try:
        return _run(_do(dev))
    except Exception:
        dev = _get_dev(host, user, pw, reconnect=True)
        return _run(_do(dev))


def is_on(host: str, user: str, pw: str, timeout: float = 5.0) -> bool:
    # Short timeout (not _run's 20s default): the LIGHT live-key sampler polls this every ~1.5s,
    # so a bulb that drops off the network must not freeze the live face for 20s per tick.
    dev = _get_dev(host, user, pw, timeout=timeout)
    try:
        _run(dev.update(), timeout=timeout)
    except Exception:
        with _dev_lock:                       # a cancelled query can leave the transport
            if _devices.get(host) is dev:     # half-open: drop it so the next poll reconnects
                del _devices[host]            # instead of piling onto a stuck device
        raise
    return bool(dev.is_on)


# ---- live (encoder) brightness / colour: coalesced set-point --------------------------------
# A rotary encoder fires many ticks fast. Doing a read-modify-write round-trip per tick floods the
# LAN and lags badly. Instead each tick just ADDS to a local delta; a single background task on the
# bulb's event loop applies the accumulated change and rate-limits, so a fast spin = one big, instant
# jump. State is seeded once from the bulb, then we own the set-point (we're the one driving it).
_lives: dict = {}                    # host -> _LiveLight
_lives_lock = threading.Lock()
_warned_no_light: set = set()        # hosts we've already flagged as having no Light module
_notify = None                       # optional callback(kind, value): fired after each apply (for the HUD)


def set_live_notify(fn) -> None:
    """Register a callback(kind, value) — kind 'bri' (0..100 %) or 'hue' (0..360°) — called after
    each coalesced brightness/colour apply, so the dock can show a live value HUD."""
    global _notify
    _notify = fn


def _emit(kind: str, value: int) -> None:
    fn = _notify
    if fn is not None:
        try:
            fn(kind, value)
        except Exception:
            pass


class _LiveLight:
    def __init__(self, host: str, user: str, pw: str):
        self.host, self.user, self.pw = host, user, pw
        self.bri = None              # current set-point (seeded from bulb)
        self.hue = None
        self.sat = 100
        self.val = 100
        self.d_bri = 0               # pending accumulated deltas (written by caller threads)
        self.d_hue = 0
        self._acc_lock = threading.Lock()
        self._ev: Optional[asyncio.Event] = None

    def add(self, loop, kind: str, delta: int) -> None:
        proj = None
        with self._acc_lock:
            if kind == "bri":
                self.d_bri += int(delta)
                if self.bri is not None:
                    proj = ("bri", max(1, min(100, self.bri + self.d_bri)))
            else:
                self.d_hue += int(delta)
                if self.hue is not None:
                    proj = ("hue", (self.hue + self.d_hue) % 360)
        # Optimistic HUD: surface the PROJECTED set-point the instant the tick lands, so the overlay
        # tracks the encoder in real time instead of stepping at the bulb's Wi-Fi round-trip rate.
        if proj is not None:
            _emit(proj[0], proj[1])
        if self._ev is not None:
            loop.call_soon_threadsafe(self._ev.set)

    async def run(self) -> None:
        self._ev = asyncio.Event()
        try:
            with _dev_lock:
                dev = _devices.get(self.host)        # reuse the poller's connection if it exists
            if dev is None:
                dev = await _connect(self.host, self.user, self.pw)
            with _dev_lock:
                _devices[self.host] = dev            # share the one connection with is_on()
            light = _light(dev)
            if light is None:
                # A non-light Tapo device (e.g. a smart plug) — encoder dim/colour has nothing to
                # drive. Bail once (the finally still cleans up _lives) instead of reconnecting and
                # failing on every encoder tick.
                if self.host not in _warned_no_light:
                    _warned_no_light.add(self.host)
                    print(f"[tapo] {self.host} has no Light module — brightness/colour encoder ignored")
                return
            self.bri = int(getattr(light, "brightness", None) or 50)
            h = getattr(light, "hsv", None) or (0, 100, 100)
            self.hue, self.sat, self.val = int(h[0]), int(h[1]) or 100, int(h[2]) or 100
            self._ev.set()                            # flush any ticks that arrived while connecting
            while True:
                await self._ev.wait()
                self._ev.clear()
                with self._acc_lock:
                    db, self.d_bri = self.d_bri, 0
                    dh, self.d_hue = self.d_hue, 0
                    if db:
                        self.bri = max(1, min(100, self.bri + db))   # advance set-point under the lock
                    if dh:                                            # so add()'s projection stays exact
                        self.hue = (self.hue + dh) % 360
                if db == 0 and dh == 0:
                    continue
                if not dev.is_on:
                    await dev.turn_on()
                # The HUD was already updated optimistically in add(); here we only push the set-point
                # to the bulb — NO re-emit, so a slower network confirm can't yank the overlay back.
                if db:
                    await light.set_brightness(self.bri)
                if dh:
                    await light.set_hsv(self.hue, max(1, self.sat), max(1, self.val))
                await asyncio.sleep(0.12)             # coalesce ticks landing during this window
        except Exception:
            pass
        finally:
            with _lives_lock:
                if _lives.get(self.host) is self:
                    del _lives[self.host]


def nudge(host: str, user: str, pw: str, kind: str, delta: int) -> bool:
    """Relative, coalesced adjust for encoders. kind: 'bri' (brightness %) or 'hue' (degrees).
    Returns immediately — never blocks the device loop on the network."""
    _stop_cycle(host)                        # a manual turn takes over from any running rainbow cycle
    loop = _bg_loop()
    with _lives_lock:
        lv = _lives.get(host)
        if lv is None:
            lv = _LiveLight(host, user, pw)
            _lives[host] = lv
            asyncio.run_coroutine_threadsafe(lv.run(), loop)
    lv.add(loop, kind, delta)
    return True


# ---- auto rainbow cycle: a software colour-loop effect for a single Tapo bulb -----------------
_cyclers: dict = {}
_cyclers_lock = threading.Lock()


class _Cycler:
    """Steps the bulb's hue around the wheel on the shared bg event loop until stopped."""

    def __init__(self, host, user, pw, step=8, interval=0.45):
        self.host, self.user, self.pw = host, user, pw
        self.step = max(1, int(step))
        self.interval = max(0.1, float(interval))
        self._stop = False

    async def run(self):
        try:
            with _dev_lock:
                dev = _devices.get(self.host)
            if dev is None:
                dev = await _connect(self.host, self.user, self.pw)
            with _dev_lock:
                _devices[self.host] = dev
            light = _light(dev)
            if light is None:
                return
            h = int((getattr(light, "hsv", None) or (0, 100, 100))[0])
            if not dev.is_on:
                await dev.turn_on()
            while not self._stop:
                h = (h + self.step) % 360
                await light.set_hsv(h, 100, 100)
                await asyncio.sleep(self.interval)
        except Exception:
            pass
        finally:
            with _cyclers_lock:
                if _cyclers.get(self.host) is self:
                    del _cyclers[self.host]


def _stop_cycle(host: str) -> bool:
    """Stop a running rainbow cycle on `host`. Returns True if one was running."""
    with _cyclers_lock:
        c = _cyclers.pop(host, None)
    if c is not None:
        c._stop = True
        return True
    return False


def cycle(host: str, user: str, pw: str, step: int = 8, interval: float = 0.45) -> bool:
    """Toggle a software rainbow cycle on the bulb. Returns the new state (True = now cycling)."""
    if _stop_cycle(host):
        return False                          # was cycling -> this press turns it off
    c = _Cycler(host, user, pw, step, interval)
    with _cyclers_lock:
        _cyclers[host] = c
    asyncio.run_coroutine_threadsafe(c.run(), _bg_loop())
    return True


# ---- credentials ---------------------------------------------------------------------------
_LUMOS_CONFIGS = [
    r"C:\Users\Erik\Desktop\project\Lumos\dist\config.json",
    os.path.join(os.environ.get("APPDATA", ""), "Lumos", "config.json"),
]


def import_lumos_creds() -> Optional[Tuple[str, str]]:
    """Read TP-Link email/password from a local Lumos config.json, if one exists."""
    for p in _LUMOS_CONFIGS:
        try:
            with open(p, encoding="utf-8") as f:
                c = json.load(f)
            user, pw = c.get("username"), c.get("password")
            if user and pw:
                return user, pw
        except Exception:
            pass
    return None


def tapo_creds(config) -> Tuple[Optional[str], Optional[str]]:
    """(email, password) from the AjazzDock config, auto-importing from Lumos once if empty."""
    t = config.data.setdefault("tapo", {})
    email, pw = (t.get("email") or "").strip(), (t.get("password") or "")
    if not (email and pw):
        imported = import_lumos_creds()
        if imported:
            email, pw = imported
            t["email"], t["password"] = email, pw
            try:
                config.save()
            except Exception:
                pass
    return (email or None, pw or None)
