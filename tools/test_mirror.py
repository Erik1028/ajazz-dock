"""Hexpad_mirror_v1: the frame tap, remote input on the loop thread, idle-screen wake, the JSON pipe.

Runs against a fake HID device and an in-process QLocalSocket on a TEST pipe name, so the real dock
and the real running Hexpad (and its mirror pipe) are never touched; no config is saved and the
bindings are emptied, so no real action can fire.
"""
import base64, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtNetwork import QLocalSocket
from dock.config import Config, default_config
from dock.controller import DockController, remote_event
from dock.device import AKP03, LCD_KEYS
from dock import mirror as mx

fails = []
def ck(n, cond):
    print(("  ok " if cond else "FAIL ") + n)
    if not cond:
        fails.append(n)

class FakeHid:
    def __init__(self): self.frames = []
    def write(self, b): self.frames.append(bytes(b)); return len(b)
    def read(self, n, t): return []
    def close(self): pass

# ---- 1. the frame tap: images count once flush() commits them ----
d = AKP03(); d.dev = FakeHid(); d._initialized = True
ck("starts empty", d.mirror == (0, (None,) * LCD_KEYS))
d.set_key_image(0, b"JPEG-A"); d.set_key_image(3, b"JPEG-B")
ck("not visible before flush", d.mirror[0] == 0)
d.flush()
gen, keys = d.mirror
ck("flush commits: gen 1, keys 0 and 3", gen == 1 and keys[0] == b"JPEG-A" and keys[3] == b"JPEG-B" and keys[1] is None)
d.set_key_image(0, b"JPEG-C"); d.clear_key(3); d.flush()
ck("second commit: key0 replaced, key3 cleared", d.mirror[0] == 2 and d.mirror[1][0] == b"JPEG-C" and d.mirror[1][3] is None)
d.flush()
ck("a flush with nothing new keeps gen", d.mirror[0] == 2)
d.clear_all(); d.flush()
ck("clear_all -> all None", all(k is None for k in d.mirror[1]))
d.sleep(); ck("sleep -> dark", d.mirror_dark is True)
d.wake(); ck("wake -> not dark", d.mirror_dark is False)

# ---- 2. remote input ----
ck("key3 down -> key index 2", (lambda e: e.kind == "key" and e.index == 2 and e.pressed)(remote_event("key3", True, 0)))
ck("btn7 -> button index 6 (input_id btn7)", remote_event("btn7", False, 0).input_id == "btn7")
ck("enc1 turn -1 -> encoder_turn", (lambda e: e.kind == "encoder_turn" and e.index == 1 and e.delta == -1)(remote_event("enc1", None, -1)))
ck("enc2 push", remote_event("enc2", True, 0).input_id == "enc2")
ck("rubbish rejected", remote_event("key9", True, 0) is None and remote_event("btn1", True, 0) is None
   and remote_event("bogus", True, 0) is None and remote_event("key1", None, 0) is None)

data = default_config()
for p in data["profiles"]:                 # no bindings anywhere: nothing real can fire
    for pg in p.get("pages", []):
        pg["items"] = {}
    p["globals"] = {}
c = DockController(Config(data)); c.dock = d; c.connected = True
seen = []
c._handle_event = lambda ev: seen.append(ev.input_id + ("" if ev.kind == "encoder_turn" else (":down" if ev.pressed else ":up")))
ck("inject accepts a key", c.inject("key2", True) and c.inject("key2", False) and c.inject("enc1", turn=1))
ck("inject rejects rubbish", not c.inject("key0", True))
ck("nothing handled before the loop drains", seen == [])
c._drain_remote()
ck("drained in order on the loop", seen == ["key2:down", "key2:up", "enc1+"])

# idle screen: a wake request leaves it, fires nothing, resets the idle timer
del c._handle_event
fired = []
c._fire_action = lambda *a, **k: fired.append(a)
c._ambient = {"style": "test"}; c._last_input = 0
c.request_wake(); c._drain_remote()
ck("wake leaves the idle screen", c._ambient is None)
ck("wake resets the idle timer", time.time() - c._last_input < 2)
ck("wake fires no binding", fired == [])

st = c.status()
ck("status has api 1 + folder/display/ambient", st.get("api") == 1 and "folder" in st and st.get("display_on") is True and st.get("ambient") is False)

ck("danger: system / empty bin / lock; not volume", mx.is_dangerous({"action": {"type": "system", "system": "sleep"}})
   and mx.is_dangerous({"action": {"type": "media"}, "action_hold": {"type": "quick", "op": "recycle_empty"}})
   and mx.is_dangerous({"action": {"type": "quick", "op": "lock"}})
   and not mx.is_dangerous({"action": {"type": "volume", "volume": "up"}}) and not mx.is_dangerous(None))

# ---- 3. the pipe, in process, on a test name ----
app = QCoreApplication.instance() or QCoreApplication(sys.argv)
name = f"Hexpad_mirror_test_{os.getpid()}"
d.set_key_image(1, b"\xff\xd8FACE1"); d.flush()
srv = mx.MirrorServer(c, name=name)
ck("server starts", srv.start())
ck("echo hook installed", c.on_input == srv._on_physical)

def pump(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

sock = QLocalSocket(); sock.connectToServer(name)
ck("client connects", sock.waitForConnected(1000))
got = []
buf = [b""]
def read():
    buf[0] += bytes(sock.readAll())
    while b"\n" in buf[0]:
        line, buf[0] = buf[0].split(b"\n", 1)
        got.append(json.loads(line))
sock.readyRead.connect(read)
pump(300)
kinds = [m["t"] for m in got]
ck("hello, state, controls, faces arrive", kinds[:4] == ["hello", "state", "controls", "faces"])
ck("hello says api 1 and 6 keys", got and got[0].get("api") == 1 and got[0].get("keys") == 6)
ck("the server counts the client (fast loop)", c.mirror_clients == 1)
faces = [m for m in got if m["t"] == "faces"]
ck("first faces has all 6 keys", faces and len(faces[0]["keys"]) == 6)
ck("key 1 decodes to what the dock got", faces and base64.b64decode(faces[0]["keys"]["1"]) == b"\xff\xd8FACE1")

got.clear()
d.set_key_image(4, b"\xff\xd8FACE4"); d.flush()
pump(200)
f2 = [m for m in got if m["t"] == "faces"]
ck("a new frame pushes only the changed key", f2 and list(f2[-1]["keys"].keys()) == ["4"])

got.clear(); pump(200)
ck("no change -> nothing sent", got == [])

seen.clear()
c._handle_event = lambda ev: seen.append(ev.input_id)
sock.write(b'{"t":"hello","client":"test"}\n{"t":"input","id":"key5","down":true}\n{"t":"input","id":"key5","down":false}\n{"t":"wake"}\n')
sock.flush(); pump(200)
ck("client inputs queued, nothing handled off the loop", seen == [] and len(c._remote) == 2 and c._wake_requested)
c._drain_remote()
ck("then handled on the loop", seen == ["key5", "key5"])

from dock.device import Event
got.clear()
c.on_input(Event("key", 0, True)); c.on_input(Event("encoder_turn", 1, delta=-1))
pump(200)
ins = [m for m in got if m["t"] == "input"]
ck("physical inputs echoed", ins == [{"t": "input", "id": "key1", "down": True}, {"t": "input", "id": "enc1", "turn": -1}])

sock.write(b"x" * (mx.MAX_LINE + 10)); sock.flush(); pump(300)
ck("an endless line drops the client", c.mirror_clients == 0)
srv.stop()
ck("stop removes the echo hook", c.on_input is None)

print("\n" + ("ALL OK" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
