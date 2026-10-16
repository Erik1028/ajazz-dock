"""UI simplification: searchable action picker, quick-start presets, collapsed appearance."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("AJAZZDOCK_CONFIG", os.path.join(os.environ.get("TEMP", "/tmp"), "_ui_cfg.json"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QScrollArea
app = QApplication([])

from dock.config import Config, default_config
from dock.controller import DockController
from dock.gui import (ConfigWindow, ActionPickerDialog, _CollapsibleSection, PRESETS,
                      ACTION_CATEGORIES, ACTION_EMOJI, ACTION_DESC, ACTION_TYPES, COMMON_ACTIONS)

# ---- 1) metadata coverage: every real action type has an emoji, a description, a category -----
cats = {t for _, ts in ACTION_CATEGORIES for t in ts}
for t in ACTION_TYPES:
    if t == "none":
        continue
    assert t in ACTION_EMOJI and t in ACTION_DESC, ("missing meta", t)
    assert t in cats, ("not in any category", t)
print("OK every action has emoji + description + category")

# ---- 2) action GRID picker: filter + Common grid + collapsible category grids + keyboard nav --
dlg = ActionPickerDialog("mic", None)
# search flattens to a filtered grid of tiles
dlg.search.setText("light")
assert sorted(t.t for t in dlg._tiles) == ["smartlight"], [t.t for t in dlg._tiles]
dlg.search.setText("volume")
assert sorted(t.t for t in dlg._tiles) == ["appvolume", "volume"], [t.t for t in dlg._tiles]
# browse mode: Common grid present, current action's category auto-expanded ('mic' -> Media & sound)
dlg.search.setText("")
types = {t.t for t in dlg._tiles}
assert set(COMMON_ACTIONS).issubset(types), "Common tiles missing in browse"
assert "mic" in types, "current action's category not expanded"
# toggling a category expands its grid (the 'second menu under the first')
before = len(dlg._tiles)
dlg._toggle_cat("Apps & files")
assert "open" in {t.t for t in dlg._tiles} and len(dlg._tiles) > before, "expand category grid"
dlg._toggle_cat("Apps & files")
assert "Apps & files" not in dlg._expanded, "collapse category grid"
# keyboard nav highlights a tile
dlg.search.setText("o")
dlg._move(1)
assert dlg._nav >= 0 and dlg._tiles, "keyboard nav highlights"
print("OK action GRID picker: filter + Common + collapsible category grids + keyboard nav")

# ---- 3) empty key: the editor stays lean (presets live in the right-click menu, live data in
#         Appearance); applying a preset still configures the key --------------------------------
win = ConfigWindow(DockController(Config(default_config())))
win.show()
for _ in range(3):
    app.processEvents()
win.items().pop("key1", None)
win.select("key1")
for _ in range(2):
    app.processEvents()
from PySide6.QtWidgets import QPushButton
texts = [b.text() for b in win.editor_host.findChildren(QPushButton)]
assert not any("Mic mute" in t for t in texts), ("quick-start presets must NOT clutter the editor", texts[:6])
assert any("Show live data" in t for t in texts), \
    ("live-data entry point missing from Appearance", texts[:8])
assert "Nothing" in win._action_picker_btn.text()
from PySide6.QtWidgets import QLabel as _QL
appears = [l for l in win.editor_host.findChildren(_QL)
           if l.objectName() == "cardhdr" and "Appearance" in l.text()]
assert appears, "an LCD key must show an Appearance card in the bottom inspector"
# apply the stateful 'Mic mute' preset
win._apply_preset(next(p for p in PRESETS if p["name"] == "Mic mute"))
for _ in range(2):
    app.processEvents()
b = win.items()["key1"]
assert b["action"]["type"] == "mic" and b.get("live", {}).get("source") == "mic"
assert "Microphone" in win._action_picker_btn.text()
assert not any("Quick start" == t for t in (lb.text() for lb in win.editor_host.findChildren(type(win.ed_title))))
print("OK presets: empty -> apply -> configured")

# ---- 4) bottom-bar inspector builds its editor as columns for each action type ----------------
def built_columns():
    return win.editor_cols.count() > 0      # the bottom bar lays the editor out as scroll columns

win.items().pop("key1", None)
win.select("key1")                      # empty (presets shown)
for _ in range(2):
    app.processEvents()
assert built_columns(), "empty-key editor built no columns"
for t in ("open", "smartlight", "appvolume", "macro"):
    win.items()["key1"] = {"action": {"type": t}}
    win.select("key1")
    for _ in range(2):
        app.processEvents()
    assert built_columns(), f"{t} editor built no columns"
print("OK bottom-bar inspector builds columns (empty + open/smartlight/appvolume/macro)")

# ---- 5) design audit fixes: profile anchor, encoder 3-in-1, page rename/move ------------------
from PySide6.QtWidgets import QToolButton
assert win.profile_combo.currentText() == "Default", win.profile_combo.currentText()
win.select("enc0-")
for _ in range(2):
    app.processEvents()
segs = [b for b in win.editor_host.findChildren(QToolButton) if b.objectName() == "encseg"]
assert len(segs) == 3, ("encoder should show all 3 sub-actions", len(segs))
assert any("Turn left" in s.text() for s in segs) and any("Volume" in s.text() for s in segs)
# page move operates on the right page (rename uses a modal dialog; tested structurally)
win.pages().append({"name": "Two", "items": {}})
win.pages()[0]["name"] = "One"
win._move_page(0, 1)
assert win.pages()[1]["name"] == "One", "move-page failed"
assert all(hasattr(win, m) for m in ("_rename_page", "_page_tab_menu", "_del_page", "_edit_app_rule"))
# AppRuleDialog pre-fills when editing
from dock.gui import AppRuleDialog
dlg = AppRuleDialog(win.cfg, win, rule={"app": "obs64.exe", "profile": "Default", "page": None})
assert dlg.app_combo.currentText() == "obs64.exe" and dlg.profile_combo.currentData() == "Default"
print("OK profile anchor + encoder 3-in-1 + page rename/move + rule edit")

# the trash button deletes the SELECTED page, not page 0 (regression: clicked-bool passed as idx)
from PySide6.QtWidgets import QMessageBox
_q = QMessageBox.question
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
win.pages()[:] = [{"name": n, "items": {}} for n in ["Home", "Media", "Work"]]
win.cur_page = 2
win._del_page()                              # the no-arg trash-button path
QMessageBox.question = _q
assert [p["name"] for p in win.pages()] == ["Home", "Media"], "trash must delete the selected page"
print("OK trash deletes the selected (current) page")

# ---- 6) wheel guard: scrolling must NOT change combo/spin/slider values unless focused --------
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QScrollArea, QSpinBox, QLineEdit, QVBoxLayout, QWidget, QComboBox
from dock.gui import _WheelGuard

guard = _WheelGuard()
def _wheel():
    return QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, -120), QPoint(0, -120),
                       Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)

combo = QComboBox(); combo.addItems(["a", "b", "c"]); combo.setCurrentIndex(1)
assert not combo.hasFocus()
assert guard.eventFilter(combo, _wheel()) is True, "wheel over an unfocused combo must be swallowed"
assert combo.currentIndex() == 1, "wheel must not change an unfocused combo's value"

spin = QSpinBox(); spin.setRange(0, 10); spin.setValue(4)
assert guard.eventFilter(spin, _wheel()) is True and spin.value() == 4, "wheel must not change a spinbox"

# guarded widget inside a scroll area: the wheel is forwarded to the panel (no crash) and blocked
sa = QScrollArea(); inner = QWidget(); lay = QVBoxLayout(inner)
c2 = QComboBox(); c2.addItems(["x", "y"]); c2.setCurrentIndex(0); lay.addWidget(c2); sa.setWidget(inner)
assert guard.eventFilter(c2, _wheel()) is True and c2.currentIndex() == 0, "scroll-area combo blocked"

# non-guarded widgets and non-wheel events pass straight through
assert guard.eventFilter(QLineEdit(), _wheel()) is False, "line edit wheel must pass through"
assert guard.eventFilter(combo, QEvent(QEvent.Type.Enter)) is False, "non-wheel events pass through"
print("OK wheel guard blocks accidental dropdown/spin/slider edits while scrolling")

# ---- 7) smart-bulb relative actions + one-click dial set-up -----------------------------------
from dock.gui import _action_summary
assert _action_summary({"action": {"type": "smartlight", "mode": "brightness", "brightness": 65}}) == "Light: 65%"
assert _action_summary({"action": {"type": "smartlight", "mode": "brightness_up", "step": 10}}) == "Light: bright +10%"
assert _action_summary({"action": {"type": "smartlight", "mode": "brightness_down", "step": 15}}) == "Light: bright −15%"
assert _action_summary({"action": {"type": "smartlight", "mode": "hue_up", "step": 30}}) == "Light: colour +30°"

# one-click 'Bulb dimmer' maps all three dial roles (turn-left/push/turn-right)
win._setup_dial("enc0", "bright")
it = win.page().get("items", {})
assert it["enc0-"]["action"]["mode"] == "brightness_down" and it["enc0-"]["action"]["step"] == 10
assert it["enc0"]["action"]["mode"] == "toggle"
assert it["enc0+"]["action"]["mode"] == "brightness_up"
# 'Colour cycle' maps hue up/down
win._setup_dial("enc1", "hue")
it = win.page().get("items", {})
assert it["enc1-"]["action"]["mode"] == "hue_down" and it["enc1+"]["action"]["mode"] == "hue_up"
assert it["enc1+"]["action"]["step"] == 30

# the relative-mode editor shows a Step slider (objectName sliderval readout)
win.items()["key1"] = {"action": {"type": "smartlight", "mode": "brightness_up", "host": "192.0.2.10", "step": 10}}
win.select("key1")
for _ in range(2):
    app.processEvents()
from PySide6.QtWidgets import QLabel
svs = [l.text() for l in win.editor_host.findChildren(QLabel) if l.objectName() == "sliderval"]
assert any("%" in s for s in svs), ("step readout missing", svs)
print("OK smart-bulb up/down/set actions + one-click dial set-up")

# ---- 8) 'No action' tile in the grid + Prisma RGB brightness + dial --------------------------
assert "none" in COMMON_ACTIONS, "grid needs a clear/none option"
dlg = ActionPickerDialog("open", None)
assert any(t.t == "none" for t in dlg._tiles), "browse grid must show the No-action tile"
dlg.search.setText("clear")                      # searchable by 'clear'
assert "none" in {t.t for t in dlg._tiles}, "No-action must be findable by 'clear'"
# picking 'none' in the grid sets the action to none -> the key reads as cleared
assert _action_summary({"action": {"type": "none"}}) == "Not set"

# Prisma RGB brightness summaries
assert _action_summary({"action": {"type": "rgbscene", "mode": "bright_set", "brightness": 80}}) == "RGB: 80%"
assert _action_summary({"action": {"type": "rgbscene", "mode": "bright_up", "step": 10}}) == "RGB: bright +10%"
assert _action_summary({"action": {"type": "rgbscene", "mode": "bright_down", "step": 5}}) == "RGB: bright −5%"

# one-click 'RGB dim' dial maps Prisma brightness up/down
win._setup_dial("enc2", "rgb")
it = win.page().get("items", {})
assert it["enc2-"]["action"] == {"type": "rgbscene", "mode": "bright_down", "step": 10}
assert it["enc2+"]["action"]["mode"] == "bright_up"
assert it["enc2"]["action"]["mode"] == "toggle"
print("OK No-action grid tile + Prisma RGB brightness + RGB dial")

# ---- 9) the extra live-data sources are all wired -------------------------------------------
import dock.live as _live
ids = _live.source_ids()
for s in ("cpu_clock", "vram", "vram_temp", "gpu_clock", "gpu_fan", "ram_gb", "swap",
          "net_up", "uptime", "procs"):
    assert s in ids, ("missing live source", s)
    txt, cap, frac, kind = _live.value(s)         # each provider returns a well-formed 4-tuple
    assert isinstance(txt, str) and isinstance(cap, str)
assert len(ids) >= 24, ("expected many more live sources", len(ids))
print(f"OK {len(ids)} live-data sources incl. VRAM / clocks / fan / uptime")

# ---- 10) live-data GRID picker: every source categorised + tiled + searchable ----------------
from dock.gui import LiveDataPickerDialog
cat_sources = [s for _c, ss in _live.LIVE_CATEGORIES for s in ss]
assert sorted(cat_sources) == sorted(ids), "every live source must be in exactly one category"
assert len(cat_sources) == len(set(cat_sources)), "no source in two categories"
for s in ids:
    assert s in _live.LIVE_EMOJI and s in _live.LIVE_SHORT, ("live tile metadata missing", s)
ldlg = LiveDataPickerDialog("cpu", None)
assert len(ldlg._tiles) == len(ids), ("browse grid must tile every source", len(ldlg._tiles))
ldlg.search.setText("temp")                      # cpu/gpu/vram temperature sensors
assert {t.source for t in ldlg._tiles} == {"cpu_temp", "gpu_temp", "vram_temp"}, \
    [t.source for t in ldlg._tiles]
ldlg.search.setText("fan")
assert [t.source for t in ldlg._tiles] == ["gpu_fan"], [t.source for t in ldlg._tiles]
ldlg.search.setText("")
ldlg._refresh_tiles()                            # live preview refresh must not throw
print("OK live-data grid picker: categorised tiles + live preview + search")

print("\nRESULT: ALL PASS")
sys.stdout.flush()
os._exit(0)
