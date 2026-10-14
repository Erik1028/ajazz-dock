"""Offscreen test of the folder editor wiring in ConfigWindow (no real display/device)."""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication                # noqa: E402
from dock.config import Config, default_config            # noqa: E402
from dock.controller import DockController                # noqa: E402
from dock.gui import ConfigWindow                         # noqa: E402

app = QApplication([])
data = default_config()
# The Back TILE only exists in "key6" mode; the shipped default exits folders with the round
# button (btn9), which leaves all 6 LCD keys free for content. Opt in explicitly so this test
# exercises the tile editor rather than depending on a missing-key fallback.
data["folder_back"] = "key6"
prof = data["profiles"][0]
prof["pages"][0]["items"]["key2"] = {"label": "Apps", "action": {"type": "folder", "folder": "folder1"}}
prof["folders"] = {"folder1": {"name": "Apps", "items": {
    "key1": {"label": "FF", "action": {"type": "open", "target": "firefox"}}}}}

win = ConfigWindow(DockController(Config(data)))
fails = []


def check(name, cond):
    print(("  ok " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


check("starts on a page (not a folder)", win.view_folder is None)
check("page items include the folder key", win.items().get("key2", {}).get("action", {}).get("type") == "folder")

win.select("key2")                                        # open the folder key's editor (folder fields)
check("selecting folder key ok", win.sel == "key2")

win._enter_folder_edit("folder1")
check("entered folder edit", win.view_folder == "folder1")
check("items() now = folder items", win.items().get("key1", {}).get("label") == "FF")

win.select("key1")
check("editing a folder key", win.sel == "key1")

win.select("key6")                                        # the Back tile — now editable, no exit
check("Back tile selectable (stays in folder)", win.view_folder == "folder1" and win.sel == "key6")
check("Back editor seeded defaults", win.cfg.folder("folder1").get("back", {}).get("icon") == "⬅️")

win._set_face(win.cfg.folder("folder1")["back"], "icon", "🏠")
check("Back icon customised", win.cfg.folder("folder1")["back"]["icon"] == "🏠")

win._reset_back()
_b = win.cfg.folder("folder1").get("back", {})
check("Reset Back restores default look", _b.get("icon", "⬅️") == "⬅️" and _b.get("label", "Back") == "Back")

win._exit_folder_edit()                                   # breadcrumb is the exit now
check("breadcrumb exits the folder", win.view_folder is None)

nid = win._new_folder_id()
check("new folder id is unique", nid not in win.cfg.folders_of())

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
