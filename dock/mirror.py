"""Hexpad_mirror_v1 - what the dock shows and does, for Orbit's VR panel (or any local client).

A SEPARATE local pipe next to AjazzDock_ipc_v1, which stays exactly as it was (that one pops the
configurator on any message but 'status', so a client must never probe it). One JSON object per
line, UTF-8.

Hexpad -> client
  {"t":"hello","app":"Hexpad","api":2,"caps":["hi","values","set","jog"],"keys":6,"tile":[w,h],
   "hi":[2w,2h],"rot":90,"mirror":false,"shift":[dx,dy],"inset":n}
  {"t":"state","connected":..,"display_on":..,"ambient":..,"profile":..,"page":..,"pages":..,"page_name":..,
   "folder":..,"folder_name":..,"folder_page":..,"folder_pages":..}
  {"t":"faces","gen":n,"dark":bool,"keys":{"0":"<base64 JPEG as sent to the key>"|null, ...},
   "hi":{"0":"<base64 JPEG>"|null, ...}}
      only the keys that changed; all six right after hello. The JPEG is the device's own: rotated
      90 degrees with the calibration shift / inset baked in (see hello). "hi" (api 2) only for a
      client whose hello asked "hi":2: the same face drawn clean at 2x (hello "hi" size; unrotated,
      no shift / inset), one entry per changed key; null = none (an animation frame, the knob HUD,
      a wallpaper...) -> use the device JPEG. A key counts as changed for such a client when either
      JPEG changed.
  {"t":"controls","key1":{"label":..,"danger":bool}, .., "btn7":{..}, "enc0":{"label":..,"left":..,
   "right":..,"push":..,"danger":bool,"slider":"abs"|"jog"|"","kind":"volume","max":100}, ..}
      what each input does on the current page / folder. slider (api 2): "abs" = Hexpad reads AND
      sets the turn binding's value (kind brightness / volume / monitor), "jog" = relative only,
      "" = no turn binding; kind = the turn action's type.
  {"t":"values","enc0":{"v":0.42},"enc1":{"v":1.0,"muted":false},"enc2":{"v":0.5,"off":false}}
      api 2, on change: each "abs" encoder's value 0..1 (0.01 steps; null = not known yet).
      muted: volume; off: the monitors are switched off (a set is ignored then).
  {"t":"input","id":"key3","down":true} | {"t":"input","id":"enc1","turn":1}   a PHYSICAL input (echo)
client -> Hexpad
  {"t":"hello","client":"Orbit","ver":"..","api":2,"hi":2}      api / hi are optional (api 1 client)
  {"t":"input","id":"key3","down":true|false} | {"t":"input","id":"enc1","turn":1|-1}
  {"t":"wake"}        leave the idle screen without firing anything
  {"t":"set","id":"enc1","kind":"volume","value":0.42,"final":false}
      an "abs" slider: applied on the dock loop, latest-wins per encoder, only while the encoder's
      CURRENT turn binding is that kind (else dropped); no knob HUD; wakes the idle screen and is
      not swallowed. final = the drag ended (dock brightness is saved then).
  {"t":"jog","id":"enc0","steps":-2}     fire the turn binding |steps| (<= 10) times, unaccelerated
Remote turns (input turn, jog) are never accelerated.

Pushes run on a 50 ms Qt timer only while a client is connected. With no client the whole cost is
the frame tap in AKP03 (one dict store per key write). Off switch: config "mirror": false. The 2x
faces cost nothing until a client asks for them (the renderers attach them only then).
"""
from __future__ import annotations

import base64
import collections
import json
import time
from typing import Callable, Dict, Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .config import BUTTONS, ENCODERS, LCD_KEYS

MIRROR_NAME = "Hexpad_mirror_v1"
API = 2
CAPS = ["hi", "values", "set", "jog"]
MAX_LINE = 64 * 1024               # a client line longer than this is not a client of ours
MAX_BACKLOG = 4 * 1024 * 1024      # a client that stops reading is dropped, not buffered forever
_DANGER_QUICK = {"recycle_empty", "lock"}
_POLL_S = 1.0                      # slider caches refreshed this often while an api-2 client watches
_PRIME_S = 30.0                    # an unknown monitor value is read from DDC at most this often


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _log(where: str, exc: BaseException) -> None:
    try:
        from .controller import _log_error
        _log_error(where, exc)
    except Exception:
        pass


def is_dangerous(item) -> bool:
    """Would any gesture of this binding end a VR session or lose data? Lock / sleep / shut down /
    sign out (every 'system' action), Empty Recycle Bin, Lock PC. Orbit asks before pressing it."""
    if not isinstance(item, dict):
        return False
    for slot in ("action", "action_double", "action_hold"):
        a = item.get(slot)
        if not isinstance(a, dict):
            continue
        t = (a.get("type") or "").lower()
        if t == "system":
            return True
        if t == "quick" and a.get("op") in _DANGER_QUICK:
            return True
        if t == "macro":
            for step in a.get("steps") or []:
                if isinstance(step, dict) and is_dangerous({"action": step}):
                    return True
    return False


class _Client:
    __slots__ = ("sock", "buf", "keys", "dark", "state", "controls", "name", "api", "hi", "hikeys",
                 "values")

    def __init__(self, sock: QLocalSocket) -> None:
        self.sock = sock
        self.buf = b""
        self.keys = None                 # the 6-tuple last sent (None = nothing yet)
        self.dark = None
        self.state = None
        self.controls = None
        self.name = "?"
        self.api = 1                     # what its hello says (api 1 until then)
        self.hi = False                  # it asked for the 2x faces ("hi": 2)
        self.hikeys = None               # the 2x 6-tuple last sent
        self.values = None


class MirrorServer(QObject):
    def __init__(self, controller, summary: Optional[Callable] = None, short: Optional[Dict[str, str]] = None,
                 name: str = MIRROR_NAME, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.c = controller
        self._summary = summary or (lambda item: ((item or {}).get("action") or {}).get("type") or "")
        self._short = short or {}
        self.name = name
        self._server: Optional[QLocalServer] = None
        self._clients: list[_Client] = []
        self._echo = collections.deque(maxlen=256)        # physical inputs, appended on the loop thread
        self._controls_cache = None
        self._controls_at = 0.0
        self._sliders: dict = {}                           # enc -> controller.slider_info, with controls
        self._polled = -1e9
        self._primed: dict = {}                            # monitor index -> last prime time
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

    # ---- lifecycle -----------------------------------------------------------------------------
    def start(self) -> bool:
        if not self.c.config.data.get("mirror", True):
            return False
        QLocalServer.removeServer(self.name)       # we hold the single-instance mutex: a stale name is ours
        s = QLocalServer(self)
        s.setSocketOptions(QLocalServer.UserAccessOption)
        if not s.listen(self.name):
            _log("mirror listen", RuntimeError(s.errorString()))
            return False
        s.newConnection.connect(self._on_connect)
        self._server = s
        self.c.on_input = self._on_physical
        return True

    def stop(self) -> None:
        self._timer.stop()
        if getattr(self.c, "on_input", None) == self._on_physical:
            self.c.on_input = None
        for cl in list(self._clients):
            self._drop(cl)
        if self._server is not None:
            self._server.close()
            self._server = None

    # ---- connections ---------------------------------------------------------------------------
    def _on_connect(self) -> None:
        while self._server is not None and self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is None:
                break
            cl = _Client(sock)
            self._clients.append(cl)
            sock.readyRead.connect(lambda cl=cl: self._on_read(cl))
            sock.disconnected.connect(lambda cl=cl: self._drop(cl))
            self.c.mirror_clients = len(self._clients)
            self._send(cl, self._hello())
            self._push(cl)
            if not self._timer.isActive():
                self._timer.start()

    def _drop(self, cl: _Client) -> None:
        if cl not in self._clients:
            return
        self._clients.remove(cl)
        self.c.mirror_clients = len(self._clients)
        try:
            cl.sock.abort()
            cl.sock.deleteLater()
        except Exception:
            pass
        if not self._clients:
            self._timer.stop()
        self._sync_hi()

    def _sync_hi(self) -> None:
        """2x faces are made only while at least one connected client asked for them."""
        fn = getattr(self.c, "set_mirror_hi", None)
        if fn is not None:
            fn(any(cl.hi for cl in self._clients))

    def _send(self, cl: _Client, msg: dict) -> None:
        try:
            if cl.sock.bytesToWrite() > MAX_BACKLOG:
                self._drop(cl)
                return
            cl.sock.write(json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n")
        except Exception as e:
            _log("mirror send", e)
            self._drop(cl)

    def _on_read(self, cl: _Client) -> None:
        try:
            cl.buf += bytes(cl.sock.readAll())
            while b"\n" in cl.buf:
                line, cl.buf = cl.buf.split(b"\n", 1)
                if line.strip():
                    self._handle(cl, json.loads(line.decode("utf-8")))
            if len(cl.buf) > MAX_LINE:
                self._drop(cl)
        except Exception as e:
            _log("mirror read", e)
            self._drop(cl)

    def _handle(self, cl: _Client, msg: dict) -> None:
        t = msg.get("t")
        if t == "input":
            down = msg.get("down")
            self.c.inject(str(msg.get("id") or ""), None if down is None else bool(down), int(msg.get("turn") or 0))
        elif t == "wake":
            self.c.request_wake()
        elif t == "set":                           # only recorded here; the dock loop applies it
            self.c.request_set(str(msg.get("id") or ""), str(msg.get("kind") or ""), msg.get("value"),
                               bool(msg.get("final")))
        elif t == "jog":
            self.c.jog(str(msg.get("id") or ""), msg.get("steps"))
        elif t == "hello":
            cl.name = str(msg.get("client") or "?")[:40]
            cl.api = max(1, _int(msg.get("api")))
            hi = _int(msg.get("hi")) >= 2
            if hi != cl.hi:
                cl.hi, cl.hikeys = hi, None        # the next push sends all six with their 2x copy
                self._sync_hi()

    # ---- what we publish -----------------------------------------------------------------------
    def _hello(self) -> dict:
        from . import device
        d = self.c.dock
        w, h = device.KEY_SIZE
        return {"t": "hello", "app": "Hexpad", "api": API, "caps": list(CAPS), "keys": len(LCD_KEYS),
                "tile": [w, h], "hi": [2 * w, 2 * h], "rot": int(getattr(d, "image_rotation", 90) or 0),
                "mirror": bool(getattr(d, "image_mirror", False)), "shift": list(device.CONTENT_SHIFT),
                "inset": int(device.EDGE_INSET)}

    def _state(self) -> dict:
        s = self.c.status()
        return {"t": "state", "connected": bool(s.get("connected")), "display_on": bool(s.get("display_on", True)),
                "ambient": bool(s.get("ambient")), "profile": s.get("profile") or "", "page": s.get("page_index", 0),
                "pages": s.get("page_count", 1), "page_name": s.get("page_name") or "",
                "folder": s.get("folder"), "folder_name": s.get("folder_name") or "",
                "folder_page": s.get("folder_page", 0), "folder_pages": s.get("folder_pages", 0)}

    def _label(self, item) -> str:
        try:
            a = (item or {}).get("action") or {}
            if not a.get("type") or a.get("type") == "none":
                return ""
            return str(self._summary(item) or "")
        except Exception:
            return ""

    def _controls(self) -> dict:
        now = time.monotonic()
        if self._controls_cache is not None and now - self._controls_at < 0.25:
            return self._controls_cache
        c = self.c
        out: dict = {"t": "controls"}
        in_folder = getattr(c, "_folder", None) is not None
        back = c.config.data.get("folder_back", "key6")
        for iid in list(LCD_KEYS) + list(BUTTONS):
            if in_folder and iid == back:
                out[iid] = {"label": "Back", "danger": False}
                continue
            item = c._binding_for(iid)
            out[iid] = {"label": self._label(item), "danger": is_dangerous(item)}
        sliders = {}
        info_of = getattr(c, "slider_info", None)
        for e in ENCODERS:
            left, right, push = c._binding_for(e + "-"), c._binding_for(e + "+"), c._binding_for(e)
            turn = (right or left or {}).get("action") or {}
            name = self._short.get(turn.get("type") or "", "") if turn.get("type") not in (None, "", "none") else ""
            info = info_of(e) if info_of is not None else ("", "", None)
            sliders[e] = info
            out[e] = {"label": name, "left": self._label(left), "right": self._label(right), "push": self._label(push),
                      "danger": is_dangerous(push), "slider": info[0], "kind": info[1], "max": 100}
        self._controls_cache, self._controls_at, self._sliders = out, now, sliders
        return out

    def _values(self) -> dict:
        """The 'abs' sliders' current values, from Hexpad's caches only (never a device read)."""
        self._controls()                           # keeps self._sliders as fresh as the controls
        fn = getattr(self.c, "slider_values", None)
        out: dict = {"t": "values"}
        if fn is not None:
            out.update(fn(self._sliders))
        return out

    def _poll(self, now: float) -> None:
        """~1 Hz while an api-2 client watches: refresh the caches the values come from. A quiet
        system-volume read (through the ONE ordered reader thread) and, for a monitor slider whose
        value is still unknown, one DDC read on the dimmer thread. Nothing here blocks."""
        if now - self._polled < _POLL_S:
            return
        self._polled = now
        eng = getattr(self.c, "engine", None)
        if eng is None:
            return
        self._controls()
        vol = False
        for slider, kind, idx in self._sliders.values():
            if slider != "abs":
                continue
            if kind == "volume" and not vol:
                vol = True
                eng.poll_system_volume()
            elif kind == "monitor" and eng.monitor_value(idx) is None:
                if now - self._primed.get(idx, -1e9) < _PRIME_S:
                    continue
                from . import monitors
                if monitors.is_off() or monitors.is_busy():
                    continue
                self._primed[idx] = now
                eng.prime_monitor(idx)

    def _push(self, cl: _Client, vals: Optional[dict] = None) -> None:
        st = self._state()
        if st != cl.state:
            cl.state = st
            self._send(cl, st)
        ctl = self._controls()
        if ctl != cl.controls:
            cl.controls = ctl
            self._send(cl, ctl)
        if cl.api >= 2:                            # an api-1 client never gets values it didn't ask for
            vals = self._values() if vals is None else vals
            if vals != cl.values:
                cl.values = vals
                self._send(cl, vals)
        n = len(LCD_KEYS)
        full = getattr(self.c.dock, "mirror_full", None)
        if full is not None:
            gen, keys, his = full
        else:
            (gen, keys), his = getattr(self.c.dock, "mirror", (0, (None,) * n)), (None,) * n
        dark = bool(getattr(self.c.dock, "mirror_dark", False))
        changed, hi = {}, {}
        for i, jpeg in enumerate(keys):
            h = his[i]
            if (cl.keys is None or jpeg is not cl.keys[i]
                    or (cl.hi and (cl.hikeys is None or h is not cl.hikeys[i]))):
                changed[str(i)] = base64.b64encode(jpeg).decode("ascii") if jpeg else None
                if cl.hi:
                    hi[str(i)] = base64.b64encode(h).decode("ascii") if h else None
        if changed or dark != cl.dark:
            cl.keys, cl.dark = keys, dark
            msg = {"t": "faces", "gen": gen, "dark": dark, "keys": changed}
            if cl.hi:
                cl.hikeys = his
                msg["hi"] = hi
            self._send(cl, msg)

    def _on_physical(self, ev) -> None:            # loop thread: just queue it
        if ev.kind == "encoder_turn":
            self._echo.append({"t": "input", "id": f"enc{ev.index}", "turn": 1 if ev.delta > 0 else -1})
        elif ev.kind in ("key", "button", "encoder_push"):
            self._echo.append({"t": "input", "id": ev.input_id, "down": bool(ev.pressed)})

    def _tick(self) -> None:
        try:
            echoes = []
            while self._echo:
                echoes.append(self._echo.popleft())
            vals = None
            if any(cl.api >= 2 for cl in self._clients):
                try:
                    self._poll(time.monotonic())
                except Exception as e:
                    _log("mirror poll", e)
                vals = self._values()              # once per tick, shared by every api-2 client
            for cl in list(self._clients):
                for m in echoes:
                    self._send(cl, m)
                if cl in self._clients:
                    self._push(cl, vals)
        except Exception as e:
            _log("mirror tick", e)
