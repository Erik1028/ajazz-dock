"""Lay out the Jump animation frames in a grid to judge the arc (smoothness/speed)."""
import os
from PIL import Image
from dock.images import render_face, press_frames

SCALE = 3
GAP = 6
COLS = 11
item = {"label": "Files", "icon": "\U0001F4C1", "color": "#c8881f"}
frames = press_frames(render_face(item), "jump")
cell = 60 * SCALE
rows = (len(frames) + COLS - 1) // COLS
W = GAP + COLS * (cell + GAP)
H = GAP + rows * (cell + GAP)
sheet = Image.new("RGB", (W, H), (18, 20, 26))
for i, f in enumerate(frames):
    r, c = divmod(i, COLS)
    x = GAP + c * (cell + GAP)
    y = GAP + r * (cell + GAP)
    sheet.paste(f.resize((cell, cell), Image.NEAREST), (x, y))
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "jump_grid.png")
sheet.save(out)
print("saved", out, "frames", len(frames))
