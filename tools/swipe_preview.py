"""Render the page-swipe frames as 2x3 grids laid left-to-right (visual QA)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image                                          # noqa: E402
from dock.images import render_face, page_swipe_frames, KEY_SIZE  # noqa: E402

w = KEY_SIZE[0]


def grid_img(faces, gap=3, sc=2):
    gw, gh = 3 * w + 2 * gap, 2 * w + gap
    g = Image.new("RGB", (gw, gh), (10, 12, 16))
    for i, f in enumerate(faces):
        r, c = divmod(i, 3)
        g.paste(f, (c * (w + gap), r * (w + gap)))
    return g.resize((gw * sc, gh * sc), Image.NEAREST)


old = [render_face({"label": f"A{i+1}", "color": "#1e6fd0"}) for i in range(6)]
new = [render_face({"label": f"B{i+1}", "color": "#159a5a"}) for i in range(6)]
frames = page_swipe_frames(old, new, 1)            # next: green slides in from the right

grids = [grid_img(fr) for fr in frames]
gap = 10
W = sum(g.width for g in grids) + gap * (len(grids) + 1)
H = grids[0].height + 2 * gap
sheet = Image.new("RGB", (W, H), (22, 24, 28))
x = gap
for g in grids:
    sheet.paste(g, (x, gap))
    x += g.width + gap
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "swipe_montage.png")
sheet.save(out)
print("saved", out, "frames", len(frames))
