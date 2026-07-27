"""A colour-/effect-setting action renders a rounded FOREGROUND swatch: a solid colour for a colour
scene (rgbscene / smartlight colour mode), or a representative gradient for an effect (Rainbow, Fire).
Centred, size-adjustable, drawn OVER any background (a leftover bg never suppresses it). A custom
icon, live data, non-gradient effects, or AUTO_ICONS off are all respected."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dock import images
from dock.images import render_face, folder_face, resolve_swatch, has_swatch, clear_face_cache

fails = []
def ck(n, cond):
    print(("  ok " if cond else "FAIL ") + n)
    if not cond: fails.append(n)

def face(item):
    clear_face_cache()
    return render_face(item, (88, 88), show_label=False)
def px(item, xy=(44, 44)):
    return face(item).getpixel(xy)

PURPLE = "B47AE8"; RGBP = (0xB4, 0x7A, 0xE8)
CORAL = "#C56A45"; CORALP = (0xC5, 0x6A, 0x45)
images.AUTO_ICONS = True

# ---- solid-colour swatch ----
ck("resolve: rgbscene colour -> ('solid', hex)",
   resolve_swatch({"action": {"type": "rgbscene", "mode": "color", "color": PURPLE}}) == ("solid", PURPLE))
ck("resolve: rgbscene default mode is colour",
   resolve_swatch({"action": {"type": "rgbscene", "color": PURPLE}}) == ("solid", PURPLE))
ck("resolve: smartlight colour -> ('solid', hex)",
   resolve_swatch({"action": {"type": "smartlight", "mode": "color", "color": CORAL}}) == ("solid", CORAL))
ck("resolve: smartlight default (toggle) -> None",
   resolve_swatch({"action": {"type": "smartlight", "color": CORAL}}) is None)

rgb = {"action": {"type": "rgbscene", "mode": "color", "color": PURPLE}}
ck("solid centre is the exact colour", px(rgb) == RGBP)
ck("solid corner stays black (inset square)", px(rgb, (3, 3)) == (0, 0, 0))
leftover = {"color": "#101014", "action": {"type": "rgbscene", "mode": "color", "color": PURPLE}}
ck("solid shows OVER a leftover bg (centre = swatch)", px(leftover) == RGBP)
ck("...and the bg is still visible in the corner", px(leftover, (3, 3)) == (0x10, 0x10, 0x14))
ck("size 70%: near-top pixel inside the swatch",
   px({"action": {"type": "rgbscene", "mode": "color", "color": PURPLE}, "swatch_size": 70}, (44, 20)) == RGBP)
ck("size 40%: same pixel outside (black)",
   px({"action": {"type": "rgbscene", "mode": "color", "color": PURPLE}, "swatch_size": 40}, (44, 20)) == (0, 0, 0))

# ---- gradient (effect) swatch ----
rain = {"action": {"type": "rgbscene", "mode": "effect", "effect": "Rainbow"}}
ck("resolve: rgbscene Rainbow -> ('grad','rainbow')", resolve_swatch(rain) == ("grad", "rainbow"))
ck("resolve: rgbscene ColorCycle -> rainbow",
   resolve_swatch({"action": {"type": "rgbscene", "mode": "effect", "effect": "ColorCycle"}}) == ("grad", "rainbow"))
imr = face(rain)
ck("rainbow swatch varies across x (a gradient)", imr.getpixel((18, 44)) != imr.getpixel((70, 44)))
ck("rainbow swatch centre is not black", imr.getpixel((44, 44)) != (0, 0, 0))
ck("rainbow corner stays black (inset square)", imr.getpixel((3, 3)) == (0, 0, 0))

fire = {"action": {"type": "rgbscene", "mode": "effect", "effect": "Fire"}}
ck("resolve: rgbscene Fire -> ('grad','fire')", resolve_swatch(fire) == ("grad", "fire"))
imf = face(fire)
ck("fire swatch varies across x", imf.getpixel((18, 44)) != imf.getpixel((70, 44)))
cp = imf.getpixel((44, 44)); ck("fire swatch is warm (red > blue)", cp[0] > cp[2])

ck("non-gradient effect (Static) is NOT a swatch",
   not has_swatch({"action": {"type": "rgbscene", "mode": "effect", "effect": "Static"}}))

# ---- folder preview must include swatch-only keys (was falling back to a plain folder emoji) ----
def has_color(img, target, tol=48):
    return any(abs(p[0] - target[0]) < tol and abs(p[1] - target[1]) < tol and abs(p[2] - target[2]) < tol
               for p in img.getdata())
fitem = {"label": "F", "action": {"type": "folder", "folder": "f1"}}
clear_face_cache()
fimg = folder_face(fitem, {"key1": {"action": {"type": "rgbscene", "mode": "color", "color": PURPLE}}},
                   (88, 88), show_label=False)
ck("folder preview INCLUDES a colour-swatch key", has_color(fimg, RGBP))
clear_face_cache()
ck("empty folder does NOT show that colour", not has_color(folder_face(fitem, {}, (88, 88), show_label=False), RGBP))

# ---- overrides ----
ck("a custom icon suppresses the swatch",
   px({"icon": "\U0001F308", "action": {"type": "rgbscene", "mode": "color", "color": PURPLE}}) != RGBP)
images.AUTO_ICONS = False
ck("AUTO_ICONS off -> no solid swatch (centre black)", px(rgb) == (0, 0, 0))
ck("AUTO_ICONS off -> no gradient swatch (centre black)", px(rain) == (0, 0, 0))
images.AUTO_ICONS = True

print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
