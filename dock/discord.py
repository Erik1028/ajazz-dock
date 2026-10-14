r"""Control Discord voice (self-mute / deafen / disconnect) + read state over Discord's local RPC.

Discord runs a local IPC server on a Windows named pipe (\\.\pipe\discord-ipc-{0..9}). After a
one-time OAuth2 authorize (the user approves a popup inside the Discord app), we hold an
authenticated connection and can GET / SET the self-mute & deafen voice settings.

Requires a free Discord application (client_id + client_secret, with http://localhost added as an
OAuth2 redirect). As the app's OWNER the rpc scopes work without Discord approving the app — see the
in-app "Discord app…" dialog for the 3-step setup.
"""
from __future__ import annotations

import ctypes
import json
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import byref, wintypes
from typing import Optional, Tuple

_SCOPES = ["rpc", "rpc.voice.read", "rpc.voice.write"]
_REDIRECT = "http://localhost"
_TOKEN_URL = "https://discord.com/api/oauth2/token"

_GENERIC_RW = 0x80000000 | 0x40000000
_OPEN_EXISTING = 3
_INVALID = ctypes.c_void_p(-1).value

_k32 = ctypes.windll.kernel32
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                               ctypes.POINTER(wintypes.DWORD)]


class NeedsAuth(Exception):
    """Raised when we have a client_id but no valid token — the GUI must run authorize()."""


class _Pipe:
    """A blocking Discord IPC named-pipe connection (op + length framed JSON)."""

    def __init__(self) -> None:
        self.h = None
        self.deadline = None           # absolute monotonic bound for the current exchange

    def connect(self) -> bool:
        for i in range(10):
            h = _k32.CreateFileW(r"\\.\pipe\discord-ipc-%d" % i, _GENERIC_RW, 0, None,
                                 _OPEN_EXISTING, 0, None)
            if h and h != _INVALID:
                self.h = h
                return True
        return False

    def _write(self, data: bytes) -> None:
        n = wintypes.DWORD()
        if not _k32.WriteFile(self.h, data, len(data), byref(n), None):
            raise OSError("discord pipe write failed")

    def _wait_readable(self) -> None:
        """Poll with PeekNamedPipe (non-blocking) until the pipe has bytes. The handle is a
        SYNCHRONOUS one, so a bare ReadFile on a silent-but-open Discord blocks forever — and
        every command runs under _Discord._lock, so that would wedge the sampler thread AND
        every key press behind it. A timeout is raised as OSError so _cmd reconnects."""
        while True:
            avail = wintypes.DWORD()
            if not _k32.PeekNamedPipe(self.h, None, 0, None, byref(avail), None):
                raise OSError("discord pipe peek failed")      # broken -> _cmd reconnects
            if avail.value:
                return
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise OSError("discord timed out")
            time.sleep(0.01)

    def _read(self, want: int) -> bytes:
        self._wait_readable()
        buf = ctypes.create_string_buffer(want)
        n = wintypes.DWORD()
        if not _k32.ReadFile(self.h, buf, want, byref(n), None) or n.value == 0:
            raise OSError("discord pipe read failed")
        return buf.raw[:n.value]

    def send(self, op: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self._write(struct.pack("<II", op, len(data)) + data)

    def recv(self) -> Tuple[int, dict]:
        head = b""
        while len(head) < 8:
            head += self._read(8 - len(head))
        op, length = struct.unpack("<II", head)
        body = b""
        while len(body) < length:
            body += self._read(length - len(body))
        return op, (json.loads(body.decode("utf-8")) if body else {})

    def close(self) -> None:
        if self.h:
            try:
                _k32.CloseHandle(self.h)
            except Exception:
                pass
            self.h = None


class _Discord:
    def __init__(self) -> None:
        self.client_id = ""
        self.client_secret = ""
        self.token = ""
        self.refresh = ""
        self._pipe = None
        self._authed = False
        self._nonce = 0
        self._on_token = None          # callback(token, refresh) to persist a fresh token
        self._in_vol = None            # cached input/output volume so an encoder dial avoids a GET/tick
        self._out_vol = None
        self._lock = threading.Lock()

    def configure(self, client_id, client_secret, token="", refresh="", on_token=None) -> None:
        with self._lock:
            cid = (client_id or "").strip()
            sec = (client_secret or "").strip()
            if (cid, sec) != (self.client_id, self.client_secret):
                self._drop()
            self.client_id, self.client_secret = cid, sec
            if token:
                self.token = token
            if refresh:
                self.refresh = refresh
            if on_token is not None:
                self._on_token = on_token

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # ---- low-level RPC ----------------------------------------------------
    def _nx(self) -> str:
        self._nonce += 1
        return str(self._nonce)

    def _cmd_once(self, cmd: str, args: Optional[dict] = None):
        nonce = self._nx()
        frame = {"cmd": cmd, "nonce": nonce}
        if args is not None:
            frame["args"] = args
        deadline = time.monotonic() + 8.0
        self._pipe.deadline = deadline                 # bounds the blocking reads below too
        self._pipe.send(1, frame)
        while time.monotonic() < deadline:
            op, data = self._pipe.recv()
            if op == 2:                                    # CLOSE
                raise OSError("discord closed the connection")
            if data.get("nonce") == nonce:                 # our reply (skip async events)
                if data.get("evt") == "ERROR":
                    raise OSError("discord: %s" % (data.get("data") or {}).get("message", "error"))
                return data.get("data")
        raise OSError("discord timed out")

    def _cmd(self, cmd: str, args: Optional[dict] = None):
        """Like _cmd_once, but if the pipe is dead (Discord was closed/reopened) reconnect and
        re-AUTHENTICATE with the stored token, then retry — so a Discord restart never needs a
        fresh authorize."""
        try:
            return self._cmd_once(cmd, args)
        except OSError:
            self._drop()
            self._connect()                                # new pipe + AUTHENTICATE(stored token)
            return self._cmd_once(cmd, args)

    def _handshake(self) -> _Pipe:
        if not self.client_id:
            raise RuntimeError("Discord app not configured")
        p = _Pipe()
        if not p.connect():
            raise OSError("Discord isn't running")
        self._drop()            # never overwrite a live _Pipe — its raw HANDLE would leak
        self._pipe = p
        p.deadline = time.monotonic() + 8.0
        p.send(0, {"v": 1, "client_id": self.client_id})   # HANDSHAKE
        p.recv()                                            # READY
        return p

    def _connect(self) -> None:
        self._handshake()
        try:
            if not self.token:
                raise NeedsAuth()
            try:
                self._cmd_once("AUTHENTICATE", {"access_token": self.token})
            except OSError:
                if self.refresh and self._refresh_token():
                    self._cmd_once("AUTHENTICATE", {"access_token": self.token})
                else:
                    raise NeedsAuth()
        except BaseException:
            self._drop()        # _ensure() retries while _authed is False: without this every
            raise               # retry opens (and abandons) another kernel pipe handle
        self._authed = True

    def _ensure(self) -> None:
        if self._pipe is None or not self._authed:
            self._connect()

    # ---- OAuth ------------------------------------------------------------
    def authorize(self) -> None:
        """Interactive one-time setup: AUTHORIZE (Discord shows a popup) -> code -> token ->
        AUTHENTICATE. Persists the token via the on_token callback. Run OFF the GUI thread."""
        with self._lock:
            self._drop()
            self._handshake()
            data = self._cmd_once("AUTHORIZE", {"client_id": self.client_id, "scopes": _SCOPES})
            self._exchange((data or {}).get("code", ""))
            self._cmd_once("AUTHENTICATE", {"access_token": self.token})
            self._authed = True
            if self._on_token:
                self._on_token(self.token, self.refresh)

    def _exchange(self, code: str) -> None:
        tok = self._post_token({"grant_type": "authorization_code", "code": code,
                                "redirect_uri": _REDIRECT})
        self.token = tok["access_token"]
        self.refresh = tok.get("refresh_token", "")

    def _refresh_token(self) -> bool:
        try:
            tok = self._post_token({"grant_type": "refresh_token", "refresh_token": self.refresh})
            self.token = tok["access_token"]
            self.refresh = tok.get("refresh_token", self.refresh)
            if self._on_token:
                self._on_token(self.token, self.refresh)
            return True
        except Exception:
            return False

    def _post_token(self, extra: dict) -> dict:
        body = urllib.parse.urlencode(
            {"client_id": self.client_id, "client_secret": self.client_secret, **extra}).encode()
        # Discord's API sits behind Cloudflare, which 403s the default "Python-urllib" agent — send a
        # real User-Agent (and Accept) so the token exchange isn't blocked.
        req = urllib.request.Request(_TOKEN_URL, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "AjazzDock/1.0 (+https://github.com/ajazzdock)",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except Exception:
                pass
            raise OSError(f"token exchange failed ({e.code}): {detail or e.reason}")

    # ---- voice ------------------------------------------------------------
    def _cache_vols(self, d: dict) -> None:
        iv = (d.get("input") or {}).get("volume")
        ov = (d.get("output") or {}).get("volume")
        if iv is not None:
            self._in_vol = float(iv)
        if ov is not None:
            self._out_vol = float(ov)

    def get_settings(self) -> dict:
        """The full GET_VOICE_SETTINGS dict (mute, deaf, input/output volume, mode, noise…)."""
        with self._lock:
            self._ensure()
            d = self._cmd("GET_VOICE_SETTINGS") or {}
            self._cache_vols(d)
            return d

    def get_voice(self) -> Tuple[bool, bool]:
        d = self.get_settings()
        return bool(d.get("mute")), bool(d.get("deaf"))

    def _nudge_vol(self, key: str, attr: str, delta: float, hi: float) -> int:
        with self._lock:
            self._ensure()
            cur = getattr(self, attr)
            if cur is None:                                # first turn -> read it once, then cache
                raw = ((self._cmd("GET_VOICE_SETTINGS") or {}).get(key) or {}).get("volume")
                cur = float(raw) if raw is not None else 100.0   # keep a genuine 0 (0.0 is falsy)
            cur = max(0.0, min(hi, cur + delta))
            setattr(self, attr, cur)
            v = int(round(cur))
            self._cmd("SET_VOICE_SETTINGS", {key: {"volume": v}})
            return v

    def nudge_input(self, delta: float) -> int:
        return self._nudge_vol("input", "_in_vol", delta, 100.0)

    def nudge_output(self, delta: float) -> int:
        return self._nudge_vol("output", "_out_vol", delta, 200.0)

    def toggle_mode(self) -> str:
        """Flip Push-to-talk <-> Voice activity; returns the new mode type."""
        with self._lock:
            self._ensure()
            cur = ((self._cmd("GET_VOICE_SETTINGS") or {}).get("mode") or {}).get("type") or "VOICE_ACTIVITY"
            new = "VOICE_ACTIVITY" if cur == "PUSH_TO_TALK" else "PUSH_TO_TALK"
            self._cmd("SET_VOICE_SETTINGS", {"mode": {"type": new}})
            return new

    def toggle_noise(self) -> bool:
        """Flip Discord's built-in noise suppression; returns the new state."""
        with self._lock:
            self._ensure()
            cur = bool((self._cmd("GET_VOICE_SETTINGS") or {}).get("noise_suppression"))
            self._cmd("SET_VOICE_SETTINGS", {"noise_suppression": not cur})
            return not cur

    def current_channel_id(self) -> str:
        with self._lock:
            self._ensure()
            d = self._cmd("GET_SELECTED_VOICE_CHANNEL")
            return (d or {}).get("id") or ""

    def join_channel(self, channel_id: str) -> None:
        if not channel_id:
            return
        with self._lock:
            self._ensure()
            self._cmd("SELECT_VOICE_CHANNEL", {"channel_id": channel_id, "force": True})

    def set_voice(self, mute=None, deaf=None) -> None:
        args = {}
        if mute is not None:
            args["mute"] = bool(mute)
        if deaf is not None:
            args["deaf"] = bool(deaf)
        with self._lock:
            self._ensure()
            self._cmd("SET_VOICE_SETTINGS", args)

    def toggle_mute(self) -> bool:
        m, _d = self.get_voice()
        self.set_voice(mute=not m)
        return not m

    def toggle_deaf(self) -> bool:
        _m, d = self.get_voice()
        self.set_voice(deaf=not d)
        return not d

    def disconnect_voice(self) -> None:
        with self._lock:
            self._ensure()
            self._cmd("SELECT_VOICE_CHANNEL", {"channel_id": None, "force": True})

    def get_channel(self) -> Tuple[bool, int, str]:
        """(in_call, people_count, channel_name) for the voice channel you're in (or False/0/"")."""
        with self._lock:
            self._ensure()
            d = self._cmd("GET_SELECTED_VOICE_CHANNEL")
            if not d:
                return False, 0, ""
            members = d.get("voice_states") or []
            return True, len(members), (d.get("name") or "")

    def _drop(self) -> None:
        if self._pipe:
            try:
                self._pipe.close()
            except Exception:
                pass
        self._pipe = None
        self._authed = False
        self._in_vol = self._out_vol = None


_discord = _Discord()


def configure(client_id, client_secret, token="", refresh="", on_token=None) -> None:
    _discord.configure(client_id, client_secret, token, refresh, on_token)


def configured() -> bool:
    return _discord.configured()


def authorize() -> None:
    _discord.authorize()


def get_voice() -> Tuple[bool, bool]:
    return _discord.get_voice()


def get_channel() -> Tuple[bool, int, str]:
    return _discord.get_channel()


def get_settings() -> dict:
    return _discord.get_settings()


def nudge_input(delta: float) -> int:
    return _discord.nudge_input(delta)


def nudge_output(delta: float) -> int:
    return _discord.nudge_output(delta)


def toggle_mode() -> str:
    return _discord.toggle_mode()


def toggle_noise() -> bool:
    return _discord.toggle_noise()


def current_channel_id() -> str:
    return _discord.current_channel_id()


def join_channel(channel_id: str) -> None:
    _discord.join_channel(channel_id)


def toggle_mute() -> bool:
    return _discord.toggle_mute()


def toggle_deaf() -> bool:
    return _discord.toggle_deaf()


def set_voice(mute=None, deaf=None) -> None:
    _discord.set_voice(mute=mute, deaf=deaf)


def disconnect_voice() -> None:
    _discord.disconnect_voice()
