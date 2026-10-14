r"""Discord IPC hardening: no leaked pipe handles, and no unbounded blocking read.

Both bugs are 24/7 ones — Hexpad runs elevated from boot, so a handle leaked per poll or a read
that never returns costs the app for the whole session. The read test uses a REAL Windows named
pipe (its own uniquely-named one, never Discord's) so it exercises the actual ReadFile path.
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import byref, wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dock import discord as D                                            # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not cond:
        fails.append(name)


# ---- 1. a real pipe that never answers must time out, not block forever -------------------------
_k32 = ctypes.windll.kernel32
_k32.CreateNamedPipeW.restype = wintypes.HANDLE
_k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
_k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]

NAME = r"\\.\pipe\hexpad-test-%d" % os.getpid()
srv = _k32.CreateNamedPipeW(NAME, 3, 0, 1, 4096, 4096, 0, None)          # PIPE_ACCESS_DUPLEX, byte
check("test pipe server created", srv and srv != D._INVALID)

threading.Thread(target=lambda: _k32.ConnectNamedPipe(srv, None), daemon=True).start()
time.sleep(0.15)
cli = _k32.CreateFileW(NAME, D._GENERIC_RW, 0, None, D._OPEN_EXISTING, 0, None)
check("test pipe client connected", cli and cli != D._INVALID)

p = D._Pipe()
p.h = cli
p.deadline = time.monotonic() + 0.8                    # the bound _cmd_once/_handshake now set
t0 = time.monotonic()
try:
    p.recv()                                            # server never writes a byte
    timed_out = False
except OSError:
    timed_out = True
dt = time.monotonic() - t0
check("a silent pipe raises instead of blocking forever", timed_out)
check("it gives up at the deadline, not later", 0.5 < dt < 2.5, "took %.2fs" % dt)

p.deadline = None
p.close()
_k32.CloseHandle(srv)

# ---- 2. a failed connect must not abandon the kernel handle it just opened ----------------------
opened, closed = [], []


class FakePipe:
    """Stands in for _Pipe: records its own lifecycle instead of touching a real pipe."""

    def __init__(self):
        self.deadline = None
        self.id = len(opened)
        opened.append(self.id)

    def connect(self):
        return True

    def send(self, op, payload):
        pass

    def recv(self):
        return 1, {}

    def close(self):
        closed.append(self.id)


real_pipe = D._Pipe
D._Pipe = FakePipe
try:
    d = D._Discord()
    d.client_id, d.client_secret = "cid", "sec"
    d.token = ""                                        # authorized? no -> _connect raises NeedsAuth
    for _ in range(25):                                 # what the 1.5 s sampler does all day
        try:
            d._ensure()
        except D.NeedsAuth:
            pass
        except Exception:
            pass
    leaked = sorted(set(opened) - set(closed))
    check("no pipe handle is abandoned by a failed connect", not leaked,
          "opened=%d closed=%d leaked=%s" % (len(opened), len(closed), leaked))
    check("a failed connect leaves no pipe attached", d._pipe is None)

    # a SUCCESSFUL connect keeps exactly one pipe open, and reconnecting closes the old one
    opened.clear(), closed.clear()
    d2 = D._Discord()
    d2.client_id, d2.client_secret, d2.token = "cid", "sec", "tok"
    d2._cmd_once = lambda cmd, args=None: {}            # pretend AUTHENTICATE succeeds
    d2._connect()
    check("a good connect holds one open pipe", len(opened) == 1 and not closed)
    d2._authed = False                                  # Discord restarted -> reconnect
    d2._connect()
    check("reconnect closes the previous pipe", len(opened) == 2 and len(closed) == 1,
          "opened=%d closed=%d" % (len(opened), len(closed)))
finally:
    D._Pipe = real_pipe

# ---- 3. the deadline is actually armed by the command path --------------------------------------
armed = {}


class ProbePipe(FakePipe):
    def send(self, op, payload):
        armed["deadline"] = self.deadline

    def recv(self):
        return 1, {"nonce": "1", "data": {"ok": True}}


d3 = D._Discord()
d3._pipe = ProbePipe()
d3._cmd_once("GET_VOICE_SETTINGS")
check("_cmd_once arms the read deadline before sending",
      armed.get("deadline") is not None and armed["deadline"] > time.monotonic())

print("\nRESULT: " + ("ALL PASS" if not fails else "%d FAIL: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
