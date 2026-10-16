"""Render a montage of every press animation (rows) x frames (cols) for visual QA."""
import os
from PIL import Image, ImageDraw
from dock.images import render_face, press_frames, PRESS_ANIM_ORDER, PRESS_ANIM_LABELS, _font

SCALE = 2
GAP = 6
LABEL_W = 86
item = {"label": "Files", "icon": "\U0001F4C1", "color": "#c8881f"}
face = render_face(item)

rows = []
maxcols = 0
for name in PRESS_ANIM_ORDER:
    frames = press_frames(face, name)
    maxcols = max(maxcols, len(frames))
    rows.append((name, frames))

cell = 60 * SCALE
W = LABEL_W + maxcols * (cell + GAP) + GAP
H = GAP + len(rows) * (cell + GAP)
sheet = Image.new("RGB", (W, H), (18, 20, 26))
d = ImageDraw.Draw(sheet)
font = _font(15, bold=True)

y = GAP
for name, frames in rows:
    d.text((8, y + cell // 2 - 8), PRESS_ANIM_LABELS[name], font=font, fill=(220, 224, 230))
    x = LABEL_W
    for f in frames:
        sheet.paste(f.resize((cell, cell), Image.NEAREST), (x, y))
        x += cell + GAP
    y += cell + GAP

out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "anim_montage.png")
sheet.save(out)
print("saved", out, "rows", len(rows), "maxcols", maxcols)
