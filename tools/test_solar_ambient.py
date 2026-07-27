"""The Solar-sky idle design: a real-sun-tracking sky (sunrise/day/sunset/night) with a sun/moon disc,
registered as a static ambient style. Verifies day vs night differ, graceful fallback, and registry."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dock.images import (ambient_clock_tiles, ambient_preview, AMBIENT_STYLE_ORDER,
                         AMBIENT_STYLE_LABELS, AMBIENT_ANIMATED, KEY_SIZE)

fails = []
def ck(n, cond):
    print(("  ok " if cond else "FAIL ") + n)
    if not cond: fails.append(n)

def bright(tiles):
    tot = n = 0
    for t in tiles:
        for p in t.getdata():
            tot += 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]; n += 1
    return tot / max(1, n)

SR, SS = 6 * 60 + 30, 19 * 60 + 30                    # sunrise 06:30, sunset 19:30
midday = ambient_clock_tiles("12", "30", "MON", "07", "JUL", style="solar", solar=(SR, SS, True))
night = ambient_clock_tiles("02", "30", "MON", "07", "JUL", style="solar", solar=(SR, SS, False))
dawn = ambient_clock_tiles("06", "40", "MON", "07", "JUL", style="solar", solar=(SR, SS, True))
dusk = ambient_clock_tiles("19", "50", "MON", "07", "JUL", style="solar", solar=(SR, SS, False))

ck("solar returns 6 tiles at key size", len(midday) == 6 and all(t.size == KEY_SIZE for t in midday))
ck("midday sky is much brighter than the night sky", bright(midday) > bright(night) + 25)
ck("dawn differs from full night (twilight in between)", abs(bright(dawn) - bright(night)) > 8)
ck("dusk is warmer/darker than midday", bright(dusk) < bright(midday))

fb = ambient_clock_tiles("14", "00", "MON", "07", "JUL", style="solar", solar=None)   # no live data yet
ck("solar fallback (no sun data) still renders", len(fb) == 6 and all(t.size == KEY_SIZE for t in fb))
bad = ambient_clock_tiles("14", "00", "MON", "07", "JUL", style="solar", solar=(None, None, True))
ck("solar with malformed data still renders", len(bad) == 6)

ck("solar registered in the design order", "solar" in AMBIENT_STYLE_ORDER)
ck("solar has a picker label", "solar" in AMBIENT_STYLE_LABELS)
ck("solar is a STATIC design (near-zero CPU, not ~15fps)", "solar" not in AMBIENT_ANIMATED)

try:
    pv = ambient_preview("solar")
    ck("solar preview renders", pv.width > 0 and pv.height > 0)
except Exception as e:
    ck("solar preview renders", False); print("     preview err:", repr(e))

print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
