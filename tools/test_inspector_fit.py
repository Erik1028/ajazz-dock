"""Regression for the bottom-bar (Stream Deck) inspector: every action / control type must build
its editor as columns without raising, and the bar stays a fixed-height strip (it never grows to fit
a tall editor — columns scroll on their own). NOTE: per-column WIDTH fit is verified on the REAL
Windows renderer (tools/shot_layout.py), not here — the offscreen platform substitutes a wider font
that inflates combo size hints, so an offscreen width assertion is a known false-positive."""
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QScrollArea, QFrame
app = QApplication([])
from dock.config import Config, default_config
from dock.controller import DockController
from dock.gui import ConfigWindow

fails = []
TOL = 28          # offscreen substitutes a wider font; tolerate that, flag only gross overflow


def inspector_height(win):
    for f in win.findChildren(QFrame):
        if f.objectName() == "inspector":
            return f.height()
    return None


# ONE window for the whole run. Building a fresh ConfigWindow per case is quadratic: every window
# and its DockController leave app-wide filters, timers and sampler threads behind (threshold GC is
# disabled process-wide — see the COM-crash fix), so by the ~9th case each build took ~10 s and the
# run never finished. Rebuilding the editor via refresh()+select() exercises exactly what this test
# is about: that every action type's editor builds as columns without raising.
_data = default_config()
_data["folder_back"] = "key6"          # so the "back-tile" case has a Back tile to select
_data["profiles"][0]["folders"] = {"f1": {"name": "F", "items": {"key1": {"label": "a"}}}}
_cfg = Config(_data)
WIN = ConfigWindow(DockController(_cfg))
WIN.resize(1340, 860); WIN.show(); app.processEvents()


def check(label, item, sel="key1", folder=False):
    data = _cfg.data
    data["profiles"][0]["pages"][0]["items"]["key1"] = item
    try:
        win = WIN
        win._exit_folder_edit() if win.view_folder else None
        win.refresh()
        app.processEvents()
        if folder:
            win._enter_folder_edit("f1"); win.select("key6")
        else:
            win.select(sel)
        app.processEvents(); app.processEvents()
        ncols = win.editor_cols.count()        # editor_cols is now a QSplitter (resizable cards)
        wide = []                              # informational only (offscreen font inflates these)
        for i in range(ncols):
            w = win.editor_cols.widget(i)
            if isinstance(w, QScrollArea) and w.widget() is not None:
                need = w.widget().sizeHint().width()
                if need > w.width() + TOL:
                    wide.append(f"{need}>{w.width()}")
        h = inspector_height(win)
        ok = ncols > 0 and h is not None and h <= 460
        note = "" if not wide else f"(offscreen-wide: {wide})"
        print(f"  {'ok ' if ok else 'FAIL'} {label:14} cols={ncols} barH={h} {note}")
        if not ok:
            fails.append(label)
    except Exception as e:
        print(f"  FAIL {label:14} raised {type(e).__name__}: {e}")
        fails.append(label)


ACTIONS = ["open", "hotkey", "text", "media", "volume", "mic", "sound", "discord",
           "substance", "quick", "system", "monitor", "page", "folder", "profile",
           "brightness", "macro", "obs", "smartlight", "rgbscene", "appvolume", "toggle",
           "monpower"]
for t in ACTIONS:
    it = {"icon": "C:/x.png", "action": {"type": t}}
    if t == "open":
        it["action"]["target"] = "https://www.icloud.com/some/long/path"
    if t == "folder":
        it["action"]["folder"] = "f1"
        check(t, it, folder=False)
    else:
        check(t, it)
check("back-tile", {}, folder=True)
check("encoder", {}, sel="enc0-")
check("button", {}, sel="btn7")
# Discord sub-modes carry their own (longer) hints — must still fit their column.
check("discord-outvol", {"action": {"type": "discord", "mode": "outvol_down", "step": 10}})
check("discord-invol", {"action": {"type": "discord", "mode": "invol_up", "step": 5}})
check("discord-join", {"action": {"type": "discord", "mode": "join"}})
check("discord-noise", {"action": {"type": "discord", "mode": "noise_toggle"}})
check("sound-stop", {"action": {"type": "sound", "mode": "stop"}})

print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
