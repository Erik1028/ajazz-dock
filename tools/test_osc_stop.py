r"""The VRChat OSC listener must RELEASE udp/9001 on shutdown.

Port 9001 is a single fixed port that exactly one process may hold, and Hexpad autostarts elevated
at boot — so a listener that never stops silently wins the port against VRCFaceTracking, VRCOSC and
OVRToolkit for the whole session. Worse, the error those apps then show blames the wrong app.
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


def port_free(port=vrcosc.RECV_PORT):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((vrcosc.HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


if not port_free():
    print("SKIP: udp/%d is already in use (VRChat or another OSC app is running)"
          % vrcosc.RECV_PORT)
    sys.exit(0)

check("the shutdown flag is a real Event, not a dead bool", hasattr(vrcosc, "_stop_ev"))
check("a stop() entry point exists", callable(getattr(vrcosc, "stop", None)))

vrcosc.start()
for _ in range(50):                                   # give the thread a moment to bind
    if vrcosc.status().get("listening") and not port_free():
        break
    time.sleep(0.02)
check("start() binds udp/%d" % vrcosc.RECV_PORT, not port_free())
check("status() reports it as listening", vrcosc.status().get("listening") is True)

t0 = time.monotonic()
vrcosc.stop()
dt = time.monotonic() - t0
check("stop() releases the port for other OSC apps", port_free())
check("stop() returns promptly", dt < 3.0, "took %.2fs" % dt)
check("status() no longer claims to be listening", not vrcosc.status().get("listening"))

vrcosc.start()                                        # must be restartable, not one-shot
for _ in range(50):
    if not port_free():
        break
    time.sleep(0.02)
check("start() works again after a stop()", not port_free())
vrcosc.stop()
check("and releases it again", port_free())

vrcosc.stop()                                         # idempotent: shutdown may call it twice
check("stop() is safe to call twice", port_free())

print("\nRESULT: " + ("ALL PASS" if not fails else "%d FAIL: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
