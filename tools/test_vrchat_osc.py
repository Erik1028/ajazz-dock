"""VRChat OSC bridge: codec, listener cache, action dispatch and live tiles — all verified WITHOUT
VRChat by binding VRChat's own ports locally (we play VRChat on both sides of the link)."""
import os, sys, socket, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dock import vrcosc as V
from dock import live
from dock.actions import ActionEngine

fails = []
def ck(n, cond):
    print(("  ok " if cond else "FAIL ") + n)
    if not cond: fails.append(n)

# ---- 1) OSC 1.0 codec round-trip ----
for addr, args in (("/avatar/parameters/Hat", [True]),
                   ("/avatar/parameters/Outfit", [3]),
                   ("/avatar/parameters/Blend", [0.25]),
                   ("/chatbox/input", ["hi there", True, False]),
                   ("/avatar/change", ["avtr_x"])):
    pkt = V._pack(addr, *args)
    got = V._parse(pkt)
    a2, ar2 = got[0]
    same = a2 == addr and len(ar2) == len(args) and all(
        (abs(x - y) < 1e-5 if isinstance(y, float) else x == y) for x, y in zip(ar2, args))
    ck(f"codec round-trip {addr.split('/')[-1]:<8} {args}", same and len(pkt) % 4 == 0)

bundle = b"#bundle\x00" + b"\x00" * 8
for addr, args in (("/avatar/parameters/A", [True]), ("/avatar/parameters/B", [2])):
    m = V._pack(addr, *args)
    bundle += len(m).to_bytes(4, "big") + m
ck("bundle decodes to 2 messages", len(V._parse(bundle)) == 2)
ck("garbage packet does not raise", V._parse(b"\x01\x02\x03") == [])

# ---- 2) listener caches what "VRChat" sends ----
V.start(); time.sleep(0.5)
st = V.status()
if st.get("error"):
    print("  !! port 9001 busy (%s) — is VRChat or another OSC app running? skipping listener tests"
          % st["error"])
else:
    vrc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    def from_vrchat(addr, *a):
        vrc.sendto(V._pack(addr, *a), ("127.0.0.1", V.RECV_PORT))
    from_vrchat("/avatar/change", "avtr_test_1")
    from_vrchat("/avatar/parameters/MuteSelf", True)
    from_vrchat("/avatar/parameters/AFK", False)
    from_vrchat("/avatar/parameters/Outfit", 2)
    time.sleep(0.7)
    ck("listener reports connected", V.connected())
    ck("bool parameter cached", V.get_param("MuteSelf") is True)
    ck("int parameter cached", V.get_param("Outfit") == 2)
    ck("live tile vrc_mute -> MUTED", live.value("vrc_mute")[0] == "MUTED")
    ck("live tile vrc_afk -> HERE", live.value("vrc_afk")[0] == "HERE")
    ck("parameterized tile vrc:Outfit -> 2", live.value("vrc:Outfit")[0] == "2")
    ck("parameterized tile kind is 'state'", live.value("vrc:Outfit")[3] == "state")
    from_vrchat("/avatar/change", "avtr_test_2")
    time.sleep(0.5)
    ck("avatar switch clears stale parameters", V.get_param("MuteSelf") is None)

# ---- 3) actions actually put the right bytes on the wire (we listen as VRChat) ----
rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    rx.bind(("127.0.0.1", V.SEND_PORT))
    rx.settimeout(1.0)
except OSError as e:
    print("  !! could not bind VRChat's port 9000 (%s) — skipping action tests" % e)
    rx = None

def drain(n=1, wait=0.6):
    out = []
    end = time.time() + wait
    while len(out) < n and time.time() < end:
        try:
            data, _ = rx.recvfrom(65535)
        except socket.timeout:
            break
        out.extend(V._parse(data))
    return out

if rx is not None:
    eng = ActionEngine(None)
    V._params.clear()

    eng.execute({"type": "vrchat", "mode": "set", "param": "Hat", "vtype": "bool", "value": "true"})
    got = drain()
    ck("set bool -> /avatar/parameters/Hat True",
       got and got[0][0] == "/avatar/parameters/Hat" and got[0][1] == [True])

    eng.execute({"type": "vrchat", "mode": "toggle", "param": "Hat"})
    got = drain()
    ck("toggle flips the cached True -> False", got and got[0][1] == [False])

    eng.execute({"type": "vrchat", "mode": "toggle", "param": "NeverSeen"})
    got = drain()
    ck("toggle of an unseen parameter turns it ON", got and got[0][1] == [True])

    eng.execute({"type": "vrchat", "mode": "set", "param": "Outfit", "vtype": "int", "value": "3"})
    got = drain()
    ck("set int -> i 3", got and got[0][1] == [3])

    eng.execute({"type": "vrchat", "mode": "set", "param": "Blend", "vtype": "float", "value": "0.5"})
    got = drain()
    ck("set float -> f 0.5", got and abs(got[0][1][0] - 0.5) < 1e-5)

    eng.execute({"type": "vrchat", "mode": "pulse", "param": "Emote", "hold": 60})
    got = drain(2, wait=1.2)
    ck("pulse sends ON then OFF",
       len(got) >= 2 and got[0][1] == [True] and got[-1][1] == [False])

    V._params.clear()
    for expect in (0, 1, 2, 0):
        eng.execute({"type": "vrchat", "mode": "cycle", "param": "Look",
                     "vtype": "int", "values": "0, 1, 2"})
        got = drain()
        ok = bool(got) and got[0][1] == [expect]
        if not ok:
            break
    ck("cycle steps 0 -> 1 -> 2 -> wraps to 0", ok)

    eng.execute({"type": "vrchat", "mode": "chatbox", "text": "hello dock"})
    got = drain()
    ck("chatbox -> /chatbox/input with text + immediate",
       got and got[0][0] == "/chatbox/input" and got[0][1][0] == "hello dock" and got[0][1][1] is True)

    eng.execute({"type": "vrchat", "mode": "input", "input": "Jump", "hold": 60})
    got = drain(2, wait=1.2)
    ck("input button -> /input/Jump 1 then 0",
       len(got) >= 2 and got[0][0] == "/input/Jump" and got[0][1] == [1] and got[-1][1] == [0])

    eng.execute({"type": "vrchat", "mode": "toggle", "param": ""})   # must be a no-op, not a crash
    ck("empty parameter name is ignored", not drain(1, wait=0.3))
    rx.close()

# ---- 4) on-disk avatar parameter discovery (uses the real VRChat OSC folder if present) ----
cfgs = V.avatar_configs()
if cfgs:
    ps = V.avatar_params()
    ck("avatar configs parsed (BOM-tolerant)", len(cfgs) > 0 and isinstance(cfgs[0]["name"], str))
    ck("parameters have name/type/settable", bool(ps) and {"name", "type", "settable"} <= set(ps[0]))
    ck("PhysBone filter removes contact noise",
       len([p for p in ps if V.is_physbone(p["name"])]) >= 0
       and V.is_physbone("Chest_Squish") and not V.is_physbone("VF12_Jacket/Toggle"))
else:
    print("  -- no VRChat avatar configs on this machine; discovery test skipped")

print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
