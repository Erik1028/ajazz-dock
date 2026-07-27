"""Regressions for the visual-bug-hunt fixes: dark swatch stays visible on the black tile, the label
band uses readable (light) auto-contrast text on its dark scrim + elides long labels, and the folder
mini-grid includes auto-icon action keys (not just icon/label/live/swatch)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw
from dock import images
from dock.images import render_face, folder_face, resolve_auto_icon, _elide, _font, clear_face_cache

fails = []
def ck(n, cond):
    print(("  ok " if cond else "FAIL ") + n)
    if not cond: fails.append(n)

def lum(p): return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]
def face(item):
    clear_face_cache()
    return render_face(item, (88, 88), show_label=True)
images.AUTO_ICONS = True

# #2/#8 — a near-black colour swatch must still get a visible LIGHT rim on the black tile.
darkimg = face({"action": {"type": "rgbscene", "mode": "color", "color": "101830"}})
maxlum = max(lum(p) for p in darkimg.getdata())
ck("dark swatch has a visible light rim (not lost on black)", maxlum > 55)

# #1 — a LIGHT key with a label: the scrim-band text must be light (readable), not dark-on-dark.
lightlbl = face({"color": "#f2c400", "label": "Save", "action": {"type": "media", "mode": "play_pause"}})
band = [lightlbl.getpixel((x, y)) for y in range(88 - 17, 88) for x in range(88)]
ck("label band has readable light text on a light key", max(lum(p) for p in band) > 180)

# #3 — a long label is elided to one line (keeps the start, adds an ellipsis), not truncated to the tail.
d = ImageDraw.Draw(Image.new("RGB", (88, 88)))
f = _font(12)
el = _elide(d, "Mute Microphone", f, 84)
ck("long label elided keeps the start + ellipsis", el.startswith("Mute") and el.endswith("…") and el != "Mute Microphone")
ck("short label is left intact by elide", _elide(d, "Mute", f, 84) == "Mute")

# #6 — a folder of icon-only auto-icon action keys (no icon/label/live/swatch) must render content,
# not collapse to the bare folder fallback.
clear_face_cache()
media_folder = folder_face({"label": "F", "action": {"type": "folder"}},
                           {"key1": {"action": {"type": "media", "mode": "play_pause"}}}, (88, 88), show_label=False)
clear_face_cache()
empty_folder = folder_face({"label": "F", "action": {"type": "folder"}}, {}, (88, 88), show_label=False)
ck("media action has an auto-icon", bool(resolve_auto_icon({"action": {"type": "media"}})))
ck("folder with an auto-icon key renders differently than an empty folder",
   media_folder.tobytes() != empty_folder.tobytes())

print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
