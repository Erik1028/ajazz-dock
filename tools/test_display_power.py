"""Dock screen on/off via the middle button: long-press toggles, short-press = normal action."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dock.config import Config, default_config
from dock.controller import DockController
from dock.device import Event

class FakeDock:
    image_rotation, image_mirror = 90, False
    def __init__(self): self.bright = None; self.cleared = 0; self.pushes = 0; self.slept = 0; self.woke = 0
    def set_brightness(self, b): self.bright = b
    def clear_all(self): self.cleared += 1
    def set_key_pil(self, *a, **k): self.pushes += 1
    def set_key_image(self, *a, **k): self.pushes += 1
    def sleep(self): self.slept += 1                     # HAN: true backlight-off
    def wake(self): self.woke += 1                       # DIS: re-assert display power
    def flush(self): pass

data = default_config()
data["brightness"] = 70
data["page_fx"] = False                                 # page change snaps (no swipe) for the test
data["profiles"][0]["globals"]["btn8"] = {"action": {"type": "page", "page": "goto", "target": 1}}
c = DockController(Config(data)); c.dock = FakeDock(); c.connected = True
fails = []
def ck(n, cond):
    print(("  ok " if cond else "FAIL ") + n)
    if not cond: fails.append(n)

def apply_display():                                    # HID writes are deferred to the loop thread
    if c._pending_display is not None:
        c._apply_display(c._pending_display); c._pending_display = None

ck("starts on", c._display_on is True)
c.set_display(False)
ck("set_display only FLAGS (deferred to the loop)", c._pending_display is False and c._display_on is True)
apply_display()
ck("off -> _display_on False", c._display_on is False)
ck("off -> brightness 0 + cleared", c.dock.bright == 0 and c.dock.cleared >= 1)
ck("off -> HAN sleep sent (true backlight off)", c.dock.slept == 1)
ck("off -> render gated", (c._render_page() or True) and c.dock.pushes == 0)
c.set_display(True); apply_display()
ck("on -> brightness restored", c._display_on is True and c.dock.bright == 70)
ck("on -> DIS wake sent", c.dock.woke == 1)
ck("on -> idle timer reset (manual wake won't re-enter ambient)", time.time() - c._last_input < 1.0)

# LONG press toggles the screen (btn8's implicit hold) — flagged, then applied by the loop
before = c._display_on
c._handle_event(Event(kind="button", index=7, pressed=True))
ck("btn8 press deferred (no page change)", c.page_index == 0 and "btn8" in c._press)
c._press["btn8"]["t"] -= 1.0                            # simulate held >0.5s
c._check_gestures()
ck("long-press FLAGS a screen toggle", c._pending_display == (not before) and c._press["btn8"]["hold_fired"])
apply_display()
ck("long-press toggled the screen", c._display_on == (not before))
c._handle_event(Event(kind="button", index=7, pressed=False))
ck("long-press release fires no action", c.page_index == 0)

# SHORT tap runs btn8's normal action (go to page 1), no toggle
c.set_display(True); apply_display(); disp = c._display_on
c._handle_event(Event(kind="button", index=7, pressed=True))
c._handle_event(Event(kind="button", index=7, pressed=False))
c._service_requests()                                   # process the deferred page change
ck("short tap ran btn8 action (page change)", c.page_index == 1)
ck("short tap did not toggle the screen", c._display_on == disp)

print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
