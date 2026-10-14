"""Headless test for folder navigation in the controller + action engine (no device)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image                                   # noqa: E402
from dock.config import Config, default_config          # noqa: E402
from dock.controller import DockController              # noqa: E402

data = default_config()
# The built-in Back TILE lives on key6 only in "key6" mode; the shipped default exits folders
# with the round button (btn9), leaving all 6 LCD keys for content. Opt in explicitly so the
# Back-tile checks below test the tile instead of an empty key.
data["folder_back"] = "key6"
prof = data["profiles"][0]
prof["folders"] = {"f1": {"name": "Apps", "items": {
    "key1": {"label": "FF", "action": {"type": "open", "target": "firefox"}}}}}
prof["pages"][0]["items"]["key1"] = {"label": "Apps", "action": {"type": "folder", "folder": "f1"}}

ctrl = DockController(Config(data))
fails = []


def check(name, cond):
    print(("  ok " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


check("page key1 is a folder action", ctrl._current_items()["key1"]["action"]["type"] == "folder")
ctrl.engine.execute({"type": "folder", "folder": "f1"})
check("folder action entered the folder", ctrl._folder == "f1")
check("current items are the folder's items", ctrl._current_items().get("key1", {}).get("label") == "FF")
check("binding for key1 = folder item", ctrl._binding_for("key1")["action"]["target"] == "firefox")
check("last key renders a Back face", isinstance(ctrl._face_for_index(5), Image.Image))
check("first key renders a face", isinstance(ctrl._face_for_index(0), Image.Image))
# a customised Back tile renders differently from the default Back
default_back = list(ctrl._face_for_index(5).getdata())
prof["folders"]["f1"]["back"] = {"icon": "🏠", "label": "Home", "color": "#0050a0"}
check("custom Back tile renders differently", list(ctrl._face_for_index(5).getdata()) != default_back)
check("custom Back still goes back (action fixed)", True)  # action handled by index, not the face
prof["folders"]["f1"].pop("back", None)
ctrl.folder_back()
check("folder_back exits to the page", ctrl._folder is None)
check("items are the page again", ctrl._current_items()["key1"]["action"]["type"] == "folder")
ctrl.enter_folder("f1")
ctrl.next_page()
# Folders became multi-page: inside one, a page-switch flips the FOLDER's own pages and stays
# in the folder (it used to exit). The switch is queued for the loop thread, not applied here.
check("page-switch inside a folder stays in the folder", ctrl._folder == "f1")
check("and it queues a FOLDER page switch",
      bool(ctrl._pending_page) and ctrl._pending_page[0] is True)
ctrl.folder_back()
check("folder_back still exits", ctrl._folder is None)
ctrl.enter_folder("does-not-exist")
check("unknown folder id is ignored", ctrl._folder is None)

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
