"""Hexpad_mirror_v1 - what the dock shows and does, for Orbit's VR panel (or any local client).

A SEPARATE local pipe next to AjazzDock_ipc_v1, which stays exactly as it was (that one pops the
configurator on any message but 'status', so a client must never probe it). One JSON object per
line, UTF-8.

Hexpad -> client
  {"t":"hello","app":"Hexpad","api":1,"keys":6,"tile":[w,h],"rot":90,"mirror":false,"shift":[dx,dy],"inset":n}
  {"t":"state","connected":..,"display_on":..,"ambient":..,"profile":..,"page":..,"pages":..,"page_name":..,
   "folder":..,"folder_name":..,"folder_page":..,"folder_pages":..}
  {"t":"faces","gen":n,"dark":bool,"keys":{"0":"<base64 JPEG as sent to the key>"|null, ...}}
      only the keys that changed; all six right after hello. The JPEG is the device's own: rotated
      90 degrees with the calibration shift / inset baked in (see hello).
  {"t":"controls","key1":{"label":..,"danger":bool}, .., "btn7":{..}, "enc0":{"label":..,"left":..,
   "right":..,"push":..,"danger":bool}, ..}      what each input does on the current page / folder
  {"t":"input","id":"key3","down":true} | {"t":"input","id":"enc1","turn":1}   a PHYSICAL input (echo)
client -> Hexpad
  {"t":"hello","client":"Orbit","ver":".."}
  {"t":"input","id":"key3","down":true|false} | {"t":"input","id":"enc1","turn":1|-1}
  {"t":"wake"}        leave the idle screen without firing anything

Pushes run on a 50 ms Qt timer only while a client is connected. With no client the whole cost is
the frame tap in AKP03 (one dict store per key write). Off switch: config "mirror": false.
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
API = 1
MAX_LINE = 64 * 1024               # a client line longer than this is not a client of ours
MAX_BACKLOG = 4 * 1024 * 1024      # a client that stops reading is dropped, not buffered forever
_DANGER_QUICK = {"recycle_empty", "lock"}


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
    __slots__ = ("sock", "buf", "keys", "dark", "state", "controls", "name")

    def __init__(self, sock: QLocalSocket) -> None:
        self.sock = sock
        self.buf = b""
        self.keys = None                 # the 6-tuple last sent (None = nothing yet)
        self.dark = None
        self.state = None
        self.controls = None
        self.name = "?"


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
        elif t == "hello":
            cl.name = str(msg.get("client") or "?")[:40]

    # ---- what we publish -----------------------------------------------------------------------
    def _hello(self) -> dict:
        from . import device
        d = self.c.dock
        return {"t": "hello", "app": "Hexpad", "api": API, "keys": len(LCD_KEYS),
                "tile": list(device.KEY_SIZE), "rot": int(getattr(d, "image_rotation", 90) or 0),
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
        for e in ENCODERS:
            left, right, push = c._binding_for(e + "-"), c._binding_for(e + "+"), c._binding_for(e)
            turn = (right or left or {}).get("action") or {}
            name = self._short.get(turn.get("type") or "", "") if turn.get("type") not in (None, "", "none") else ""
            out[e] = {"label": name, "left": self._label(left), "right": self._label(right), "push": self._label(push),
                      "danger": is_dangerous(push)}
        self._controls_cache, self._controls_at = out, now
        return out

    def _push(self, cl: _Client) -> None:
        st = self._state()
        if st != cl.state:
            cl.state = st
            self._send(cl, st)
        ctl = self._controls()
        if ctl != cl.controls:
            cl.controls = ctl
            self._send(cl, ctl)
        gen, keys = getattr(self.c.dock, "mirror", (0, (None,) * len(LCD_KEYS)))
        dark = bool(getattr(self.c.dock, "mirror_dark", False))
        changed = {}
        for i, jpeg in enumerate(keys):
            if cl.keys is None or jpeg is not cl.keys[i]:
                changed[str(i)] = base64.b64encode(jpeg).decode("ascii") if jpeg else None
        if changed or dark != cl.dark:
            cl.keys, cl.dark = keys, dark
            self._send(cl, {"t": "faces", "gen": gen, "dark": dark, "keys": changed})

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
            for cl in list(self._clients):
                for m in echoes:
                    self._send(cl, m)
                if cl in self._clients:
                    self._push(cl)
        except Exception as e:
            _log("mirror tick", e)
