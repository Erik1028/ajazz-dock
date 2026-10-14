"""Control OBS Studio over its built-in WebSocket server (obs-websocket v5).

A single long-lived authenticated connection (reconnects on failure). Used by the `obs` action so
a key can switch scenes, toggle recording / streaming / the virtual cam, or mute an input.

Enable it in OBS: Tools -> WebSocket Server Settings -> Enable (default port 4455).
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Any, Dict, Optional

try:
    import websocket            # websocket-client
except Exception:               # pragma: no cover - dependency missing
    websocket = None


class _OBS:
    def __init__(self) -> None:
        self.host = "localhost"
        self.port = 4455
        self.password = ""
        self._ws = None
        self._id = 0
        self._lock = threading.Lock()

    def configure(self, host: Optional[str], port, password: Optional[str]) -> None:
        host = (host or "localhost").strip() or "localhost"
        try:
            port = int(port or 4455)
        except (TypeError, ValueError):
            port = 4455
        password = password or ""
        with self._lock:
            if (host, port, password) != (self.host, self.port, self.password):
                self.host, self.port, self.password = host, port, password
                self._drop()

    def _drop(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _connect(self) -> None:
        if websocket is None:
            raise RuntimeError("websocket-client not available")
        ws = websocket.create_connection(f"ws://{self.host}:{self.port}", timeout=4)
        hello = json.loads(ws.recv())                         # op 0 Hello
        d = hello.get("d", {})
        ident: Dict[str, Any] = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
        auth = d.get("authentication")
        if auth:                                              # password required
            secret = base64.b64encode(
                hashlib.sha256((self.password + auth["salt"]).encode()).digest()).decode()
            ident["d"]["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        ws.send(json.dumps(ident))
        idd = json.loads(ws.recv())                           # op 2 Identified (or close on bad auth)
        if idd.get("op") != 2:
            ws.close()
            raise RuntimeError(f"OBS identify failed: {idd}")
        self._ws = ws

    def _request_once(self, req_type: str, data: Optional[dict]) -> Optional[dict]:
        if self._ws is None:
            self._connect()
        self._id += 1
        rid = str(self._id)
        self._ws.send(json.dumps({"op": 6, "d": {"requestType": req_type, "requestId": rid,
                                                 "requestData": data or {}}}))
        # Bound the whole recv phase to ~4s of wall-clock (events are off, but be tolerant) so a
        # silent / dribbling server can't stack up many 4s socket timeouts and hang the caller.
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            resp = json.loads(self._ws.recv())
            if resp.get("op") == 7 and resp["d"].get("requestId") == rid:
                return resp["d"]
        return None

    def request(self, req_type: str, data: Optional[dict] = None) -> Optional[dict]:
        """Send one request; transparently reconnects once on a dropped connection."""
        with self._lock:
            try:
                return self._request_once(req_type, data)
            except Exception:
                self._drop()
                self._connect()
                return self._request_once(req_type, data)

    @staticmethod
    def _rd(r):
        return ((r or {}).get("responseData") or {}) if r else {}

    def input_list(self):
        """The names of every OBS input (audio + AV sources)."""
        return [i.get("inputName") for i in self._rd(self.request("GetInputList")).get("inputs", [])
                if i.get("inputName")]

    def input_volume_mul(self, inp):
        """An input's volume as a 0..1 multiplier (1.0 == 0 dB / unity), or None if unknown."""
        return self._rd(self.request("GetInputVolume", {"inputName": inp})).get("inputVolumeMul")

    @staticmethod
    def _ok(r):
        return bool(((r or {}).get("requestStatus") or {}).get("result"))

    def set_input_volume_mul(self, inp, mul):
        """True only if OBS actually accepted the write."""
        return self._ok(self.request("SetInputVolume",
                                     {"inputName": inp, "inputVolumeMul": float(mul)}))

    def input_muted(self, inp):
        """The mute state, or None if OBS refused / did not answer — NOT the same as False."""
        d = self._rd(self.request("GetInputMute", {"inputName": inp}))
        return None if "inputMuted" not in d else bool(d["inputMuted"])

    def toggle_input_mute(self, inp):
        """The NEW mute state, or None if OBS refused / did not answer."""
        d = self._rd(self.request("ToggleInputMute", {"inputName": inp}))
        return None if "inputMuted" not in d else bool(d["inputMuted"])


_obs = _OBS()


def configure(host, port, password) -> None:
    _obs.configure(host, port, password)


def request(req_type: str, data: Optional[dict] = None) -> Optional[dict]:
    return _obs.request(req_type, data)


def input_list():
    return _obs.input_list()


def input_volume_mul(inp):
    return _obs.input_volume_mul(inp)


def set_input_volume_mul(inp, mul):
    return _obs.set_input_volume_mul(inp, mul)


def input_muted(inp):
    return _obs.input_muted(inp)


def toggle_input_mute(inp):
    return _obs.toggle_input_mute(inp)
