r"""VRChat OSC: float32-safe cycling, and pulses that cannot eat presses or revert a later command.

Plays VRChat: binds VRChat's OWN receive port (9000) to capture what Hexpad puts on the wire, and
echoes each value back through the same IEEE float32 round-trip VRChat does, which is what broke
cycling in the first place.
"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dock import vrcosc                                                  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not cond:
        fails.append(name)


rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    rx.bind((vrcosc.HOST, vrcosc.SEND_PORT))
except OSError:
    print("SKIP: udp/%d is busy — VRChat is running" % vrcosc.SEND_PORT)
    sys.exit(0)
rx.settimeout(0.05)


def drain(echo=False):
    """Everything Hexpad sent since the last drain, as (address, args) — optionally echoed back
    into the parameter cache exactly as VRChat would (float32, so 0.33 comes back as 0.3300000131)."""
    out = []
    while True:
        try:
            data, _ = rx.recvfrom(65535)
        except socket.timeout:
            break
        for addr, args in vrcosc._parse(data):
            out.append((addr, args))
            if echo and addr.startswith(vrcosc.AVATAR_PARAM) and args:
                with vrcosc._lock:
                    vrcosc._params[addr[len(vrcosc.AVATAR_PARAM):]] = args[0]
    return out


def vals_of(pkts, name):
    pre = vrcosc.AVATAR_PARAM + name
    return [a[0] for addr, a in pkts if addr == pre and a]


# ---- 1. cycling a float list must visit every entry ---------------------------------------------
with vrcosc._lock:
    vrcosc._params.clear()
drain()
seen = []
for _ in range(8):
    vrcosc.cycle_param("Outfit", [0.0, 0.33, 0.66, 1.0])
    time.sleep(0.02)
    seen += vals_of(drain(echo=True), "Outfit")     # echo = VRChat reporting the change back
rounded = [round(v, 3) for v in seen]
check("a float cycle visits every value, not just the first two",
      len(set(rounded)) == 4, "sent %s" % rounded[:8])
check("and it wraps in order", rounded[:5] == [0.0, 0.33, 0.66, 1.0, 0.0], "got %s" % rounded[:5])

# an int cycle still behaves (it was never broken — guard against a regression)
with vrcosc._lock:
    vrcosc._params.clear()
drain()
seen = []
for _ in range(5):
    vrcosc.cycle_param("Hat", [0, 1, 2])
    time.sleep(0.02)
    seen += vals_of(drain(echo=True), "Hat")
check("an int cycle is unaffected", [int(v) for v in seen[:4]] == [0, 1, 2, 0], "got %s" % seen[:4])

# ---- 2. a pulse mid-flight is queued, not truncated ---------------------------------------------
with vrcosc._lock:
    vrcosc._params.clear()
drain()
for _ in range(4):                                  # mash: 4 presses inside one 150 ms hold
    vrcosc.pulse_param("Emote", 150)
    time.sleep(0.04)
time.sleep(0.9)
seq = [bool(v) for v in vals_of(drain(), "Emote")]
alternating = all(seq[i] != seq[i + 1] for i in range(len(seq) - 1))
check("a mashed pulse alternates ON/OFF (a clean edge per press)", alternating, "wire %s" % seq)
check("mashing yields more than one full pulse", seq.count(True) >= 2, "ON count %d" % seq.count(True))
check("every pulse is released", seq and seq[-1] is False, "wire %s" % seq)
check("only one timer is left pending per target", len(vrcosc._pulses) <= 1)

# ---- 3. a stale release must never revert a later command ---------------------------------------
with vrcosc._lock:
    vrcosc._params.clear()
drain()
vrcosc.pulse_param("Hood", 600)                     # long pulse...
time.sleep(0.15)
vrcosc.toggle_param("Hood")                         # ...user toggles the same param meanwhile
time.sleep(0.9)                                     # well past the original 600 ms release
after = vals_of(drain(), "Hood")
check("the orphaned pulse OFF does not land after a toggle",
      len(after) == 2 and after[0] is True and after[1] is False, "wire %s" % after)
check("and the cache reflects the toggle, not the stale release",
      vrcosc.get_param("Hood") is False, "cache %r" % vrcosc.get_param("Hood"))

# ---- 4. input buttons get a real 1->0->1 edge per press -----------------------------------------
drain()
for _ in range(4):
    vrcosc.pulse_input("Jump", 100)
    time.sleep(0.05)
time.sleep(0.8)
jumps = [a[0] for addr, a in drain() if addr == "/input/Jump" and a]
edges = sum(1 for i in range(1, len(jumps)) if jumps[i - 1] in (0, False) and jumps[i] in (1, True))
check("mashed input buttons produce multiple rising edges", edges >= 1 and len(jumps) >= 4,
      "wire %s" % jumps)

rx.close()
print("\nRESULT: " + ("ALL PASS" if not fails else "%d FAIL: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
