"""VRChat OSC bridge — send avatar-parameter changes and cache the state VRChat sends back.

VRChat speaks OSC over UDP on loopback: an app SENDS to 127.0.0.1:9000 and VRChat SENDS OUT to
127.0.0.1:9001 (overridable with the launch arg --osc=inPort:senderIP:outPort). The user must enable
OSC in-game: Action Menu > Options > OSC > Enabled — it is OFF by default, and VRChat advertises its
OSC/OSCQuery mDNS services even while disabled, so discovery proves nothing about it being on.

Parameter names may themselves contain slashes (creators group menus as "Toggles/Hat"), so the name
is EVERYTHING after the fixed "/avatar/parameters/" prefix — never just the last path segment.
VRChat coerces single-argument values, so a Bool parameter accepts ,T / ,i 1 / ,f 1.0 alike.

No third-party OSC library: OSC 1.0 packets are trivial to build by hand (see _pack/_parse), so this
adds ZERO dependencies — the same rule the rest of the app follows.

Threading: `send_*` is fire-and-forget UDP from whatever thread calls it (never blocks the device
loop). One daemon listener thread owns the receive socket and publishes into `_params` under
`_lock`; readers (live tiles) only ever read the cache, so a dock tick never touches the network.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

SEND_PORT = 9000                 # we -> VRChat
RECV_PORT = 9001                 # VRChat -> us
HOST = "127.0.0.1"

AVATAR_PARAM = "/avatar/parameters/"
AVATAR_CHANGE = "/avatar/change"
CHATBOX_INPUT = "/chatbox/input"

_ALIVE_S = 10.0                  # "connected" = heard from VRChat within this many seconds

_lock = threading.RLock()
_params: Dict[str, Any] = {}     # parameter name -> last value VRChat reported
_avatar_id: Optional[str] = None
_avatar_name: Optional[str] = None
_last_rx = 0.0
_rx_count = 0
_listener: Optional[threading.Thread] = None
_sock_out: Optional[socket.socket] = None
_listen_err: Optional[str] = None
_stop_ev = threading.Event()    # set by stop() to release the port; cleared by start()


# ---- OSC 1.0 wire format ---------------------------------------------------
def _pad(b: bytes) -> bytes:
    """OSC strings are null-terminated and padded to a multiple of 4 bytes."""
    b = b + b"\x00"
    return b + b"\x00" * ((4 - len(b) % 4) % 4)


def _pack(address: str, *args) -> bytes:
    """Build an OSC message. Supports bool (T/F — no payload), int (i), float (f), str (s)."""
    tags = ","
    body = b""
    for a in args:
        if isinstance(a, bool):                 # MUST precede int — bool is an int subclass
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"
            body += struct.pack(">i", max(-2147483648, min(2147483647, a)))
        elif isinstance(a, float):
            tags += "f"
            body += struct.pack(">f", a)
        else:
            tags += "s"
            body += _pad(str(a).encode("utf-8"))
    return _pad(address.encode("utf-8")) + _pad(tags.encode("utf-8")) + body


def _parse(data: bytes) -> List[Tuple[str, list]]:
    """Decode an OSC packet into [(address, args), ...]. Handles #bundle recursively; a malformed
    packet yields what was read so far rather than raising (the socket is untrusted input)."""
    out: List[Tuple[str, list]] = []
    try:
        if data[:8] == b"#bundle\x00":
            i = 16                                          # skip '#bundle\0' + 8-byte timetag
            while i + 4 <= len(data):
                (n,) = struct.unpack_from(">i", data, i)
                i += 4
                if n < 0 or i + n > len(data):
                    break
                out.extend(_parse(data[i:i + n]))
                i += n
            return out
        z = data.index(b"\x00")
        addr = data[:z].decode("utf-8", "replace")
        i = (z + 4) & ~3
        z2 = data.index(b"\x00", i)
        tags = data[i:z2].decode("ascii", "replace")
        i = (z2 + 4) & ~3
        args: list = []
        for t in tags[1:]:                                  # tags[0] is the leading comma
            if t == "T":
                args.append(True)
            elif t == "F":
                args.append(False)
            elif t == "N":
                args.append(None)
            elif t == "i":
                args.append(struct.unpack_from(">i", data, i)[0])
                i += 4
            elif t == "f":
                args.append(struct.unpack_from(">f", data, i)[0])
                i += 4
            elif t == "s":
                z3 = data.index(b"\x00", i)
                args.append(data[i:z3].decode("utf-8", "replace"))
                i = (z3 + 4) & ~3
            else:                                           # unknown tag -> keep what we have
                break
        out.append((addr, args))
    except Exception:
        return out
    return out


# ---- sending ---------------------------------------------------------------
def _out() -> socket.socket:
    global _sock_out
    if _sock_out is None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)                    # fire-and-forget: never stall a caller
        _sock_out = s
    return _sock_out


def send(address: str, *args) -> bool:
    """Send one OSC message to VRChat. True if it went out (UDP: no delivery guarantee)."""
    try:
        _out().sendto(_pack(address, *args), (HOST, SEND_PORT))
        return True
    except OSError:
        return False


def send_param(name: str, value) -> bool:
    """Set an avatar parameter. bool -> T/F, int -> i, float -> f (VRChat is type-strict)."""
    if not name:
        return False
    ok = send(AVATAR_PARAM + str(name).lstrip("/"), value)
    if ok:
        with _lock:
            _params[name] = value               # optimistic; VRChat echoes it back if it applied
    return ok


def get_param(name: str, default=None):
    with _lock:
        return _params.get(name, default)


def toggle_param(name: str):
    """Flip a bool parameter from the cached state. VRChat only reports CHANGES, so a parameter we
    have never seen is assumed False and this turns it on."""
    cancel_pulse(name)                  # a pending pulse OFF must not revert this a moment later
    new = not bool(get_param(name))
    send_param(name, new)
    return new


# ---- pulses ----------------------------------------------------------------
# A pulse is ON now + OFF later, so it owns a target for a while. Without bookkeeping a second
# press spawns a second timer thread whose stale OFF lands on top of whatever came after it
# (it can silently switch a toggled outfit back off), and mashing a key spawns one OS thread
# per press. One pending release per target, invalidated by generation, fixes both.
_PULSE_GAP = 0.03                # s of OFF between back-to-back pulses (VRChat needs the edge)
_pulses: Dict[Any, "threading.Timer"] = {}   # (kind, name) -> pending release timer
_pulse_repeat: Dict[Any, bool] = {}          # at most ONE queued repeat per target
_pulse_gen: Dict[Any, int] = {}              # invalidates a release already in flight


def cancel_pulse(name: str, kind: str = "param") -> None:
    """Drop any pending release for this target so its OFF can never land on a later command."""
    key = (kind, name)
    with _lock:
        t = _pulses.pop(key, None)
        _pulse_repeat.pop(key, None)
        _pulse_gen[key] = _pulse_gen.get(key, 0) + 1
    if t is not None:
        t.cancel()


def _arm(key, delay: float, fn) -> None:
    with _lock:
        gen = _pulse_gen.get(key, 0)

    def fire():
        with _lock:
            if _pulse_gen.get(key, 0) != gen:
                return                  # a newer press (or a toggle/set) owns this target now
            _pulses.pop(key, None)
        fn()                            # send OUTSIDE the lock
    t = threading.Timer(delay, fire)
    t.daemon = True
    with _lock:
        _pulses[key] = t
    t.start()


def _pulse(key, on_fn, off_fn, hold: float) -> None:
    def release():
        off_fn()
        with _lock:
            again = _pulse_repeat.pop(key, None)
        if again:                       # a press arrived mid-hold -> re-fire after the gap
            _arm(key, _PULSE_GAP, lambda: _pulse(key, on_fn, off_fn, hold))
    on_fn()
    _arm(key, hold, release)


def pulse_param(name: str, hold_ms: int = 150, value=True) -> None:
    """Set a parameter then reset it after `hold_ms` — how an emote/trigger bound to a bool is
    fired. A press while one is still held is QUEUED (coalesced to one) rather than truncating
    it: VRChat can miss a pulse shorter than a frame."""
    off = False if isinstance(value, bool) else (0 if isinstance(value, int) else 0.0)
    hold = max(0.01, hold_ms / 1000.0)
    key = ("param", name)
    with _lock:
        if key in _pulses or key in _pulse_repeat:
            _pulse_repeat[key] = True
            return
    _pulse(key, lambda: send_param(name, value), lambda: send_param(name, off), hold)


def pulse_input(name: str, hold_ms: int = 120) -> None:
    """A VRChat input button press: 1 then 0, guaranteeing the 1->0->1 edge on repeats (two 1s
    in a row are ONE press as far as VRChat is concerned)."""
    hold = max(0.01, hold_ms / 1000.0)
    key = ("input", name)
    with _lock:
        if key in _pulses or key in _pulse_repeat:
            _pulse_repeat[key] = True
            return
    _pulse(key, lambda: send_input(name, 1), lambda: send_input(name, 0), hold)


def _same_val(a, b) -> bool:
    """Equality for cycling. VRChat echoes floats back through IEEE float32 (see _parse's
    '>f'), so a value sent as the double 0.33 returns as 0.33000001311302185 — exact == never
    matches, idx falls to -1 and the cycle bounces between the first two entries forever."""
    if isinstance(a, bool) or isinstance(b, bool):
        return (isinstance(a, (bool, int, float)) and isinstance(b, (bool, int, float))
                and bool(a) == bool(b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        a, b = float(a), float(b)
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))   # float32 ~7 significant digits
    return a == b


def cycle_param(name: str, values: list):
    """Step an int/float parameter through a list of values (wrapping) — e.g. outfit variants."""
    vals = [v for v in (values or []) if v is not None]
    if not vals:
        return None
    cancel_pulse(name)                  # a pending pulse OFF must not fight the new value
    cur = get_param(name)
    idx = -1
    for i, v in enumerate(vals):
        if _same_val(v, cur):
            idx = i
            break
    nxt = vals[(idx + 1) % len(vals)]
    send_param(name, nxt)
    return nxt


def send_chatbox(text: str, immediate: bool = True, sfx: bool = False) -> bool:
    """Put text in the VRChat chatbox (144 chars max). immediate=False opens the keyboard instead."""
    return send(CHATBOX_INPUT, str(text)[:144], bool(immediate), bool(sfx))


def send_input(name: str, value) -> bool:
    """A VRChat input axis/button, e.g. 'Voice', 'Jump', 'Run'.

    Buttons take 1 (down) then 0 (up) — and MUST go back to 0 before they can fire again (two 1s in a
    row are one press), which is why the action always schedules the release. Axes are floats that
    latch until reset to 0.0.
    """
    return send("/input/" + str(name).lstrip("/"), value)


# ---- listening -------------------------------------------------------------
def _listen_loop() -> None:
    global _last_rx, _rx_count, _avatar_id, _avatar_name, _listen_err
    sock = None
    while not _stop_ev.is_set():
        if sock is None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                # NO SO_REUSEADDR: 9001 is a single fixed legacy port that only ONE process may hold.
                # Reusing it does not share the traffic — on Windows a second binder silently steals
                # or splits the datagrams. Fail loudly instead so the editor can say who has it.
                sock.bind((HOST, RECV_PORT))
                sock.settimeout(1.0)
                _listen_err = None
            except OSError as e:                 # another OSC app owns 9001 -> report + retry slowly
                _listen_err = ("port %d is in use by another OSC app (VRCFaceTracking, VRCOSC, "
                               "OVRToolkit\u2026): %s" % (RECV_PORT, e))
                sock = None
                if _stop_ev.wait(5.0):      # interruptible: stop() must not wait out the retry
                    break
                continue
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError as e:
            _listen_err = str(e)
            try:
                sock.close()
            except OSError:
                pass
            sock = None
            continue
        now = time.time()
        new_avatar = None
        for addr, args in _parse(data):
            if addr.startswith(AVATAR_PARAM):
                nm = addr[len(AVATAR_PARAM):]
                with _lock:
                    _params[nm] = args[0] if args else None
                    _last_rx = now
                    _rx_count += 1
            elif addr == AVATAR_CHANGE:
                aid = args[0] if args else None
                with _lock:
                    if aid != _avatar_id:
                        _params.clear()          # parameters belong to the WORN avatar only
                        _avatar_id = aid
                        _avatar_name = None
                        new_avatar = aid
                    _last_rx = now
            else:
                with _lock:
                    _last_rx = now
        if new_avatar:                            # disk lookup OUTSIDE the lock (file I/O)
            nm = _lookup_avatar_name(str(new_avatar))
            with _lock:
                if _avatar_id == new_avatar:
                    _avatar_name = nm
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass


def start() -> None:
    """Start the listener thread once (idempotent, daemon — never blocks shutdown)."""
    global _listener
    with _lock:
        if _listener is not None and _listener.is_alive():
            return
        _stop_ev.clear()
        _listener = threading.Thread(target=_listen_loop, name="vrc-osc", daemon=True)
        _listener.start()


def stop(join: bool = True) -> None:
    """Release UDP 9001. Port 9001 is a single fixed port only ONE process may hold, and
    Hexpad autostarts elevated at boot — so without this it wins the port against every other
    OSC consumer (VRCFaceTracking, VRCOSC, OVRToolkit) for the whole session, and the error
    those apps then show points at the wrong culprit."""
    global _listener
    with _lock:
        t, _listener = _listener, None
    _stop_ev.set()
    # join OUTSIDE the lock: the listener takes _lock on every packet it parses
    if t is not None and join and t is not threading.current_thread():
        t.join(timeout=2.5)             # wakes within its 1.0 s socket timeout


def connected() -> bool:
    """True if VRChat sent us something recently (i.e. it is running WITH OSC enabled)."""
    with _lock:
        return (time.time() - _last_rx) < _ALIVE_S if _last_rx else False


def status() -> dict:
    with _lock:
        return {"connected": connected(), "avatar_id": _avatar_id, "avatar_name": _avatar_name,
                "params": len(_params), "rx": _rx_count, "error": _listen_err,
                "listening": bool(_listener and _listener.is_alive())}


# ---- avatar parameter discovery (on-disk OSC config) -----------------------
def _osc_root() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    return os.path.join(os.path.dirname(base), "LocalLow", "VRChat", "VRChat", "OSC")


def _avatar_files() -> List[str]:
    """Every avatar OSC config on disk, newest first."""
    root = _osc_root()
    out: List[str] = []
    try:
        for usr in os.listdir(root):
            d = os.path.join(root, usr, "Avatars")
            if os.path.isdir(d):
                out += [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".json")]
    except OSError:
        return []
    out.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    return out


def _read_cfg(path: str) -> Optional[dict]:
    try:
        # VRChat writes these with a UTF-8 BOM -> utf-8-sig, or json.load raises. Avatar names can be
        # non-ASCII (e.g. 'Face Tracking Car (Garn47)' with fullwidth parens) — never assume ASCII.
        with open(path, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _lookup_avatar_name(avatar_id: str) -> Optional[str]:
    for p in _avatar_files():
        d = _read_cfg(p)
        if d and d.get("id") == avatar_id:
            return d.get("name")
    return None


def avatar_configs() -> List[dict]:
    """[{id, name, path, count}] for every avatar config on disk, newest first (for the picker)."""
    out = []
    for p in _avatar_files():
        d = _read_cfg(p)
        if d:
            out.append({"id": d.get("id"), "name": d.get("name") or "?", "path": p,
                        "count": len(d.get("parameters") or [])})
    return out


def avatar_params(avatar_id: Optional[str] = None) -> List[dict]:
    """Parameters of one avatar as [{name, type, settable}] for the editor's parameter picker.

    `avatar_id` defaults to the avatar VRChat last reported wearing, else the newest config on disk.
    type is 'Bool' | 'Int' | 'Float'; settable is False for output-only parameters.
    """
    aid = avatar_id or status().get("avatar_id")
    files = _avatar_files()
    path = None
    if aid:
        for p in files:
            d = _read_cfg(p)
            if d and d.get("id") == aid:
                path = p
                break
    if path is None:
        path = files[0] if files else None
    if path is None:
        return []
    d = _read_cfg(path) or {}
    out = []
    for p in (d.get("parameters") or []):
        if not isinstance(p, dict) or not p.get("name"):
            continue
        io_ = p.get("input") or p.get("output") or {}
        out.append({"name": p["name"], "type": io_.get("type") or "Float",
                    "settable": bool(p.get("input"))})
    return out


# PhysBone contact parameters explode the list (a 703-parameter avatar is mostly these) and are
# never what a user binds to a key — the picker hides them behind a toggle.
_PHYSBONE_SUFFIX = ("_Squish", "_Stretch", "_Angle", "_IsPosed", "_IsGrabbed")


def is_physbone(name: str) -> bool:
    return str(name).endswith(_PHYSBONE_SUFFIX)
