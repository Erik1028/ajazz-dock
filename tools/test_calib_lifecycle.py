"""Sticky-fix: a calibration session must fully end on apply/cancel, and a LATE preview that
fires after apply (Qt timer/GC race) must be ignored — the pattern can never re-arm."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dock.controller import DockController

c = DockController()                      # not started -> no device, safe
c._render_requested = False

# preview is IGNORED until a session is opened
c.preview_calibration(92, 120, 0, 0)
assert c._calib is None, "preview took effect without an open session"
print("OK preview ignored before begin")

c.begin_calibration()
c.preview_calibration(92, 120, -4, 6)
# _calib carries a 5th field now (the calibratable EDGE_INSET) — check the geometry it was
# given, and that an inset came along, rather than pinning the tuple width.
assert c._calib[:4] == (92, 120, -4, 6) and len(c._calib) == 5 and c._calib_dirty, c._calib
print("OK preview works inside a session")

c.apply_calibration(92, 120, -4, 6)
assert c._calib is None and not c._calib_active and c._render_requested
print("OK apply: session closed, _calib cleared, render requested")

# THE sticky bug: a late debounced preview after apply must NOT re-arm the pattern
c._render_requested = False
c.preview_calibration(70, 70, 10, 10)
assert c._calib is None, "late preview after apply re-armed the calib pattern  <-- sticky bug"
print("OK late preview after apply is ignored (no re-arm)")

# save() throwing must still clear + close
c.begin_calibration(); c._calib = (80, 80, 0, 0); c._render_requested = False
orig = c.config.save
c.config.save = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
c.apply_calibration(96, 130, 0, 0)
c.config.save = orig
assert c._calib is None and not c._calib_active and c._render_requested
print("OK apply-with-failing-save still clears + closes session")

# end (cancel)
c.begin_calibration(); c._calib = (80, 80, 0, 0); c._render_requested = False
c.end_calibration()
assert c._calib is None and not c._calib_active and c._render_requested
c.preview_calibration(60, 60, 0, 0)
assert c._calib is None, "preview after cancel re-armed"
print("OK cancel closes session, later preview ignored")

# dx/dy clamp protects against stale large saved shift
c.config.data["display"].update({"w": 88, "h": 88, "dx": 99, "dy": -99})
c._apply_geometry()
from dock import device
assert device.CONTENT_SHIFT == (20, -20), device.CONTENT_SHIFT
print("OK stale large shift clamped to +/-20:", device.CONTENT_SHIFT)
print("\nALL OK")
