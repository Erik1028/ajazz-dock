"""Render key faces (color + icon/emoji + label) to PIL images for the LCD keys."""
from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple

import colorsys
import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

KEY_SIZE = (88, 88)        # native per-key tile (~88; bigger spills into neighbour keys)
DEFAULT_BG = "#000000"      # keys default to a true-black background (matches the device bezel)
AUTO_ICONS = True          # give iconless action keys a crisp Fluent icon (Settings ▸ Appearance)
DEFAULT_FG = "#ffffff"

_TEXT_FONTS = ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"]
_BOLD_FONTS = ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"]
_EMOJI_FONT = "C:/Windows/Fonts/seguiemj.ttf"


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for p in (_BOLD_FONTS if bold else _TEXT_FONTS):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


@lru_cache(maxsize=16)
def _emoji_font(size: int) -> Optional[ImageFont.FreeTypeFont]:
    if os.path.exists(_EMOJI_FONT):
        try:
            # Segoe UI Emoji ships fixed bitmap strikes; 109 is a valid PPEM.
            return ImageFont.truetype(_EMOJI_FONT, size)
        except OSError:
            return None
    return None


def parse_color(c: Optional[str], fallback: str = DEFAULT_BG) -> Tuple[int, int, int]:
    c = (c or fallback).strip()
    if c.startswith("#"):
        c = c[1:]
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except (ValueError, IndexError):
        return (35, 39, 46)


def _looks_like_path(s: str) -> bool:
    return ("/" in s or "\\" in s or s.lower().endswith(
        (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".ico"))) and os.path.exists(s)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:2]


def _elide(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    """Trim `text` with a trailing ellipsis so the WHOLE label fits one line (the label band shows a
    single line, so wrapping would silently drop the leading words)."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    s = text
    while s and draw.textlength(s + "…", font=font) > max_w:
        s = s[:-1]
    return (s + "…") if s else text


@lru_cache(maxsize=4096)
def emoji_image(glyph: str, target: int = 44) -> Optional[Image.Image]:
    """A single color emoji cropped to its ink box and scaled so its long side ≈ target px
    (transparent RGBA). None if the emoji font is unavailable or the glyph is empty.

    Color emoji are bitmap glyphs that DON'T scale with a Qt font's point size, so the picker
    renders them through this (PIL, embedded_color) and shows them as icons instead of text.
    """
    font = _emoji_font(109)                  # Segoe UI Emoji is bitmap; native strike is 109px
    if font is None:
        return None
    try:
        # Big canvas + top-left anchor: variation-selector emoji (…+FE0F like 🎙️/⚙️)
        # mis-center with anchor="mm" and get clipped off the edge. "la" + the crop below
        # recenter by the actual ink box, so nothing is lost.
        tmp = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
        d = ImageDraw.Draw(tmp)
        d.text((24, 24), glyph, font=font, anchor="la", embedded_color=True)
        bbox = tmp.getbbox()
        if not bbox:
            return None
        tmp = tmp.crop(bbox)
        scale = target / max(tmp.width, tmp.height)
        return tmp.resize((max(1, int(tmp.width * scale)), max(1, int(tmp.height * scale))),
                          Image.LANCZOS)
    except (OSError, ValueError):
        return None


def _draw_emoji(img: Image.Image, glyph: str, cx: int, cy: int, target: int,
                max_box: Optional[Tuple[int, int]] = None) -> bool:
    """Draw a color emoji centered at (cx, cy). Returns False if unavailable.

    `max_box` (max_w, max_h) clamps the glyph so it can NEVER overflow/clip the tile — the
    glyph is scaled down to fit the box (aspect preserved) before being pasted.
    """
    em = emoji_image(glyph, target)
    if em is None:
        return False
    if max_box is not None:
        mw, mh = max_box
        if em.width > mw or em.height > mh:
            s = min(mw / em.width, mh / em.height)
            em = em.resize((max(1, int(em.width * s)), max(1, int(em.height * s))), Image.LANCZOS)
    img.paste(em, (cx - em.width // 2, cy - em.height // 2), em)
    return True


# ---- Segoe Fluent Icons (crisp, cohesive line/duotone icons shipped with Windows 11) --------------
_FLUENT_FONT = "C:/Windows/Fonts/SegoeIcons.ttf"
FLUENT_ICONS = {                                  # friendly name -> Segoe Fluent Icons codepoint
    "mic": 0xE720, "mic_off": 0xEC54, "folder": 0xE8B7, "folder_open": 0xE838, "app": 0xE71D,
    "play": 0xE768, "pause": 0xE769, "stop": 0xE71A, "next": 0xE893, "prev": 0xE892,
    "record": 0xE7C8, "volume": 0xE767, "mute": 0xE74F, "headphones": 0xE7F6,
    "settings": 0xE713, "light": 0xE781, "globe": 0xE774, "files": 0xEC50, "search": 0xE721,
    "timer": 0xE916, "clock": 0xE917, "calendar": 0xE787, "home": 0xE80F, "power": 0xE7E8,
    "camera": 0xE722, "screen": 0xE7F4, "keyboard": 0xE765, "star": 0xE734, "heart": 0xEB51,
    "rocket": 0xEC15, "chat": 0xE8BD, "mail": 0xE715, "game": 0xE7FC, "brightness": 0xE706,
    "wifi": 0xE701, "bluetooth": 0xE702, "battery": 0xE83F, "lock": 0xE72E, "link": 0xE71B,
    "copy": 0xE8C8, "paste": 0xE77F, "cut": 0xE8C6, "refresh": 0xE72C, "pin": 0xE718,
    "up": 0xE74A, "down": 0xE74B, "left": 0xE72B, "right": 0xE72A, "flag": 0xE7C1,
    "grid": 0xF0E2, "list": 0xEA37, "photo": 0xE91B, "video": 0xE714, "phone": 0xE717,
    "add": 0xE710, "cancel": 0xE711, "more": 0xE712, "check": 0xE73E, "delete": 0xE74D,
    "save": 0xE74E, "cloud": 0xE753, "chevron_left": 0xE76B, "chevron_right": 0xE76C,
    "unpin": 0xE77A, "contact": 0xE77B, "color": 0xE790, "redo": 0xE7A6, "undo": 0xE7A7,
    "page": 0xE7C3, "taskview": 0xE7C4, "help": 0xE897, "rename": 0xE8AC,
    "switchapps": 0xE8F9, "bolt": 0xE945, "equalizer": 0xE9E9,
}
# Auto-pick a Fluent icon from a key's action when it has no icon of its own.
_ACTION_FLUENT = {
    "mic": "mic", "smartlight": "light", "rgbscene": "light", "folder": "folder",
    "page": "right", "profile": "grid", "brightness": "brightness", "macro": "rocket",
    "hotkey": "keyboard", "sound": "volume", "monitor": "screen", "obs": "record",
    "discord": "chat", "system": "power", "appvolume": "volume", "rgbhue": "light",
    "media": "play", "volume": "volume", "text": "keyboard", "quick": "bolt",
    "toggle": "refresh", "http": "globe", "timer": "timer", "monpower": "screen",
}


@lru_cache(maxsize=48)
def _fluent_font(size: int):
    return ImageFont.truetype(_FLUENT_FONT, max(6, int(size)))


def _draw_fluent(img: Image.Image, name: str, cx: int, cy: int, target: int,
                 color, max_box: Optional[Tuple[int, int]] = None) -> bool:
    """Draw a monochrome Segoe Fluent icon centered at (cx, cy), scaled to fit `max_box`."""
    cp = FLUENT_ICONS.get(name)
    if cp is None:
        return False
    d = ImageDraw.Draw(img)
    size = max(6, int(target))
    glyph = chr(cp)
    if max_box is not None:
        mw, mh = max_box
        for _ in range(24):
            b = d.textbbox((0, 0), glyph, font=_fluent_font(size))
            if (b[2] - b[0]) <= mw and (b[3] - b[1]) <= mh:
                break
            size = int(size * 0.9)
            if size <= 6:
                break
    d.text((cx, cy), glyph, font=_fluent_font(size), fill=color, anchor="mm")
    return True


def resolve_auto_icon(item: Dict[str, Any]) -> str:
    """A 'fluent:<name>' icon derived from the key's action type, or '' if none fits."""
    a = item.get("action")
    a = a if isinstance(a, dict) else {}           # tolerate a hand-edited non-dict action
    t = (a.get("type") or "").lower()
    if t == "open":                                # launch: url -> globe, else app
        tgt = str(a.get("target") or "").lower()
        return "fluent:globe" if tgt.startswith(("http://", "https://", "www.")) else "fluent:app"
    name = _ACTION_FLUENT.get(t)
    return f"fluent:{name}" if name else ""


# rgbscene effect name (lower) -> a representative gradient swatch kind. Hue-cycling effects get a
# rainbow sweep; Fire gets a warm sweep. Effects not listed keep the normal auto-icon.
_EFFECT_GRAD = {"rainbow": "rainbow", "colorcycle": "rainbow", "spectrum": "rainbow",
                "gradient": "rainbow", "fire": "fire"}


def resolve_swatch(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """The swatch a colour-/effect-setting action should show, as (kind, value), or None:
      ('solid', '<hex>')       — rgbscene / smartlight in colour mode
      ('grad',  'rainbow'|'fire') — an rgbscene effect with a representative gradient
    Drawn as a rounded FOREGROUND square over whatever background (a leftover bg never hides it).
    rgbscene defaults to colour mode; smartlight must be explicit (its default is toggle)."""
    a = item.get("action")
    a = a if isinstance(a, dict) else {}           # tolerate a hand-edited non-dict action
    t = (a.get("type") or "").lower()
    mode = (a.get("mode") or "").lower()
    if (t == "rgbscene" and mode in ("", "color")) or (t == "smartlight" and mode == "color"):
        c = (a.get("color") or "").strip()
        return ("solid", c) if c else None
    if t == "rgbscene" and mode == "effect":
        g = _EFFECT_GRAD.get((a.get("effect") or "").strip().lower())
        return ("grad", g) if g else None
    return None


def has_swatch(item: Dict[str, Any]) -> bool:
    """True if this key renders a colour/gradient swatch (so the editor offers a size slider)."""
    return resolve_swatch(item) is not None


def _grad_swatch_tile(size: Tuple[int, int], kind: str) -> Image.Image:
    """A horizontal gradient tile: 'rainbow' sweeps the full hue wheel; 'fire' sweeps red->gold."""
    w, h = max(1, size[0]), max(1, size[1])
    row = Image.new("RGB", (w, 1))
    px = row.load()
    if kind == "fire":
        stops = ((255, 58, 0), (255, 138, 0), (255, 210, 74))
        for x in range(w):
            f = x / max(1, w - 1) * (len(stops) - 1)
            i = min(len(stops) - 2, int(f)); g = f - i
            a0, b0 = stops[i], stops[i + 1]
            px[x, 0] = tuple(int(a0[k] + (b0[k] - a0[k]) * g) for k in range(3))
    else:                                            # rainbow: hue 0..1 across the width
        for x in range(w):
            r, g, b = colorsys.hsv_to_rgb(x / w, 0.85, 1.0)
            px[x, 0] = (int(r * 255), int(g * 255), int(b * 255))
    return row.resize((w, h))


def _draw_swatch(img: Image.Image, swatch: Tuple[str, str], item: Dict[str, Any],
                 size: Tuple[int, int], has_label: bool) -> None:
    """Draw a centred rounded-square swatch (solid colour or gradient) on `img`. Its side is
    item['swatch_size'] percent of the tile (40..100, default 70); corners are rounded."""
    kind, val = swatch
    w, h = size
    band = 18 if has_label else 0
    avail = min(w, h - band)
    try:
        pct = int(float(item.get("swatch_size", 70)))
    except (TypeError, ValueError):
        pct = 70
    frac = max(40, min(100, pct)) / 100.0
    sq = max(4, min(avail, int(round(avail * frac))))    # never exceed the tile/cell
    cx, cy = w // 2, (h - band) // 2
    x0, y0 = cx - sq // 2, cy - sq // 2
    rad = max(2, int(sq * 0.24))
    # Solid + gradient share one path: paste through an anti-aliased rounded mask (smooth corners),
    # then a fixed LIGHT rim so even a near-black colour stays separated from the black tile/bezel.
    tile = (_grad_swatch_tile((sq, sq), val) if kind == "grad"
            else Image.new("RGB", (sq, sq), parse_color(val, DEFAULT_BG)))
    img.paste(tile, (x0, y0), _rounded_mask((sq, sq), rad))
    ImageDraw.Draw(img).rounded_rectangle([x0, y0, x0 + sq - 1, y0 + sq - 1], radius=rad,
                                          outline=(78, 82, 90), width=max(1, sq // 34))


def effective_fit(item: Dict[str, Any]) -> str:
    """'cover' (fill the key) or 'contain' (small, centered). Default: images fill, emoji don't."""
    f = item.get("fit")
    if f in ("cover", "contain"):
        return f
    icon = (item.get("icon") or "").strip()
    return "cover" if _looks_like_path(icon) else "contain"


def _dominant_color(src: Image.Image) -> Tuple[int, int, int]:
    """Average colour of the icon's opaque pixels (for an app-tile background default)."""
    sm = src.convert("RGBA").resize((16, 16), Image.LANCZOS)
    px = [p for p in sm.getdata() if p[3] > 40]
    if not px:
        return (58, 63, 75)
    n = len(px)
    return (sum(p[0] for p in px) // n, sum(p[1] for p in px) // n, sum(p[2] for p in px) // n)


def _grad_mask(size, direction: str) -> Image.Image:
    """A linear 0->255 mask across `size` in the given direction (v / h / d / d2)."""
    g = Image.linear_gradient("L")                   # 256x256, 0 (top) -> 255 (bottom)
    if direction == "h":
        return g.transpose(Image.ROTATE_90).resize(size)
    if direction == "d":                             # top-left -> bottom-right
        return Image.blend(g.resize(size), g.transpose(Image.ROTATE_90).resize(size), 0.5)
    if direction == "d2":                            # top-right -> bottom-left
        return Image.blend(g.resize(size),
                           g.transpose(Image.ROTATE_90).transpose(Image.FLIP_LEFT_RIGHT).resize(size), 0.5)
    return g.resize(size)                             # vertical


def background_image(item: Dict[str, Any], size) -> Image.Image:
    """The key's base background: a solid colour, or a two-colour gradient if `bg2` is set."""
    bg = parse_color(item.get("color"), DEFAULT_BG)
    if not item.get("bg2"):
        return Image.new("RGB", size, bg)
    c2 = parse_color(item.get("bg2"), bg)
    base = Image.new("RGB", size, bg)
    top = Image.new("RGB", size, c2)
    return Image.composite(top, base, _grad_mask(size, item.get("bg_dir", "v")))


def has_icon_style(item: Dict[str, Any]) -> bool:
    """True if the item carries any custom icon styling (zoom / move / rotate / tile / fx)."""
    return (abs(float(item.get("icon_scale", 1.0)) - 1.0) > 1e-3
            or int(item.get("icon_radius", 0)) > 0
            or bool(item.get("icon_tile"))
            or int(item.get("icon_dx", 0)) != 0 or int(item.get("icon_dy", 0)) != 0
            or int(item.get("icon_rotate", 0)) != 0
            or int(item.get("icon_opacity", 100)) != 100
            or int(item.get("icon_border", 0)) > 0
            or bool(item.get("icon_shadow")))


def _render_styled_icon(base: Image.Image, src: Image.Image, item: Dict[str, Any],
                        size, has_label: bool, fit: str):
    """Composite a fully-styled icon (image OR emoji) onto `base`: zoom, move, rotate, opacity,
    rounded corners, app-tile, border, drop shadow. `src` is an RGBA source image. Returns RGB.
    """
    w, h = size
    src = src.convert("RGBA")
    band = 18 if has_label else 0
    scale = max(0.25, min(1.5, float(item.get("icon_scale", 1.0))))
    bw = max(8, int(round(w * scale)))
    bh = max(8, int(round((h - band) * scale)))
    radius_pct = max(0, min(50, int(item.get("icon_radius", 0))))

    if item.get("icon_tile"):
        tcol = (parse_color(item["icon_tile_color"]) if item.get("icon_tile_color")
                else _dominant_color(src))
        pad = max(2, int(min(bw, bh) * 0.16))
        inner = ImageOps.contain(src, (max(1, bw - 2 * pad), max(1, bh - 2 * pad)), Image.LANCZOS)
        content = Image.new("RGBA", (bw, bh), tuple(tcol) + (255,))
        content.alpha_composite(inner, ((bw - inner.width) // 2, (bh - inner.height) // 2))
        rad = int(min(bw, bh) * (radius_pct or 22) / 100.0)
    else:
        if fit == "cover":
            content = ImageOps.fit(src, (bw, bh), Image.LANCZOS).convert("RGBA")
        else:
            fitted = ImageOps.contain(src, (bw, bh), Image.LANCZOS)
            content = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            content.alpha_composite(fitted, ((bw - fitted.width) // 2, (bh - fitted.height) // 2))
        rad = int(min(bw, bh) * radius_pct / 100.0)

    # border / stroke that follows the (rounded) shape
    border_w = max(0, min(10, int(item.get("icon_border", 0))))
    if border_w > 0:
        bcol = parse_color(item.get("icon_border_color"), "#ffffff")
        off = border_w / 2.0
        ImageDraw.Draw(content).rounded_rectangle(
            [off, off, bw - 1 - off, bh - 1 - off], radius=max(0, rad - border_w // 2),
            outline=bcol + (255,), width=border_w)

    if rad > 0:                                      # round the corners (clip alpha)
        content.putalpha(ImageChops.multiply(content.split()[3], _rounded_mask((bw, bh), rad)))

    rot = int(item.get("icon_rotate", 0)) % 360
    if rot:
        content = content.rotate(-rot, expand=True, resample=Image.BICUBIC)

    opacity = max(0, min(100, int(item.get("icon_opacity", 100))))
    if opacity < 100:
        content.putalpha(content.split()[3].point(lambda a: int(a * opacity / 100)))

    cw, ch = content.size
    ox = (w - cw) // 2 + int(round(w * int(item.get("icon_dx", 0)) / 100.0))
    oy = (h - band - ch) // 2 + int(round((h - band) * int(item.get("icon_dy", 0)) / 100.0))

    out = base.convert("RGBA")
    if item.get("icon_shadow"):                      # soft drop shadow behind the icon
        sh = Image.new("RGBA", (cw + 12, ch + 12), (0, 0, 0, 0))
        silhouette = Image.new("RGBA", content.size, (0, 0, 0, 0))
        silhouette.putalpha(content.split()[3].point(lambda a: int(a * 0.55)))
        sh.alpha_composite(silhouette, (6, 6))
        sh = sh.filter(ImageFilter.GaussianBlur(3))
        out.alpha_composite(sh, (ox - 6 + 1, oy - 6 + 2))
    out.alpha_composite(content, (ox, oy))
    return out.convert("RGB")


def _render_image_icon(base: Image.Image, icon_path: str, item: Dict[str, Any],
                       size, has_label: bool, fit: str):
    """Open an image file and run it through the styling pipeline (None if it won't open)."""
    try:
        src = Image.open(icon_path).convert("RGBA")
    except (OSError, ValueError):
        return None
    return _render_styled_icon(base, src, item, size, has_label, fit)


# ---- rendered-face cache -------------------------------------------------------------------
# Static faces (render_face / folder_face) are pure functions of their inputs, but each render
# pays a full supersampled PIL composition — so returning to a page re-paid six unchanged keys.
# Cache by a VALUE-signature of everything the render reads. Live faces are never cached (they
# change every tick by design, and the controller already skips unchanged pushes).
_FACE_CACHE: "OrderedDict[tuple, Image.Image]" = OrderedDict()
_FACE_CACHE_MAX = 96                    # ≈ 2 MB of 88px tiles — plenty for pages + folders
_FACE_LOCK = threading.Lock()           # faces render from both the dock loop and the GUI thread


def _face_sig(kind: str, item: Dict[str, Any], size, show_label, extra: tuple = ()):
    """A hashable signature of a static face render, or None to skip caching. Icon files fold
    in mtime+size so an edited/re-cropped image re-renders instead of serving a stale tile."""
    try:
        blob = json.dumps(item, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    icon = (item.get("icon") or "").strip()
    st: tuple = ()
    if icon and _looks_like_path(icon):
        try:
            s = os.stat(icon)
            st = (s.st_mtime_ns, s.st_size)
        except OSError:
            st = ("missing",)
    return (kind, blob, tuple(size), bool(show_label), AUTO_ICONS, st) + extra


def _face_cache_get(sig):
    if sig is None:
        return None
    with _FACE_LOCK:
        img = _FACE_CACHE.get(sig)
        if img is not None:
            _FACE_CACHE.move_to_end(sig)
        return img


def _face_cache_put(sig, img):
    if sig is not None:
        with _FACE_LOCK:
            _FACE_CACHE[sig] = img
            _FACE_CACHE.move_to_end(sig)
            while len(_FACE_CACHE) > _FACE_CACHE_MAX:
                _FACE_CACHE.popitem(last=False)
    return img


def clear_face_cache() -> None:
    with _FACE_LOCK:
        _FACE_CACHE.clear()


def render_face(item: Dict[str, Any], size: Optional[Tuple[int, int]] = None,
                show_label: bool = True) -> Image.Image:
    """Render a binding's visual face. `item` may have label / icon / color / text_color.
    Identical inputs come back from the face cache instead of re-compositing."""
    if size is None:
        size = KEY_SIZE                          # read the CURRENT (calibrated) module value
    sig = _face_sig("face", item, size, show_label)
    hit = _face_cache_get(sig)
    if hit is not None:
        return hit
    return _face_cache_put(sig, _render_face_impl(item, size, show_label))


def _render_face_impl(item: Dict[str, Any], size: Tuple[int, int],
                      show_label: bool) -> Image.Image:
    bg = parse_color(item.get("color"), DEFAULT_BG)
    if item.get("text_color"):
        fg = parse_color(item.get("text_color"), DEFAULT_FG)
    else:
        # auto-contrast: dark text on light keys, white on dark — so labels never vanish.
        lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
        fg = (24, 26, 30) if lum > 140 else parse_color(DEFAULT_FG)
    img = background_image(item, size)               # solid colour, or a gradient if bg2 is set
    draw = ImageDraw.Draw(img)
    w, h = size

    icon = (item.get("icon") or "").strip()
    label = (item.get("label") or "").strip() if show_label else ""
    has_icon = bool(icon)
    has_label = bool(label)
    # A colour-/effect-setting action (rgbscene, or smartlight colour) with no custom icon shows a
    # rounded SWATCH — a solid colour for a colour scene, or a representative gradient for an effect
    # (Rainbow etc.). Drawn over the background, so a leftover bg never hides it. Replaces the icon.
    swatch = resolve_swatch(item) if (not has_icon and AUTO_ICONS) else None
    if not has_icon and AUTO_ICONS and not swatch:   # cohesive default icon derived from the action
        icon = resolve_auto_icon(item)
        has_icon = bool(icon)

    fit = effective_fit(item)

    def _label_overlay():
        nonlocal img, draw
        overlay = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle([0, h - 18, w, h], fill=(0, 0, 0, 150))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)
        font = _font(12)
        # The scrim band is always dark: white auto-contrast reads on it (the key's own light-bg dark
        # fg would be dark-on-dark). Respect an explicit text_color. Elide so no leading word is lost.
        band_fg = fg if item.get("text_color") else parse_color(DEFAULT_FG)
        draw.text((w // 2, h - 9), _elide(draw, label, font, w - 4), font=font, fill=band_fg, anchor="mm")

    if swatch:                                       # colour-/effect-action key -> a rounded swatch
        _draw_swatch(img, swatch, item, size, has_label)
        if has_label:
            _label_overlay()
        return img

    # A crisp Segoe Fluent icon ("fluent:<name>"), tinted with the key's text colour.
    if has_icon and icon.startswith("fluent:"):
        band = 18 if has_label else 0
        avail_h = h - band
        if _draw_fluent(img, icon[7:], w // 2, avail_h // 2, int(min(w, avail_h) * 0.60),
                        fg, max_box=(w - 8, avail_h - 8)):
            if has_label:
                _label_overlay()
            return img
        has_icon = False                             # unknown name -> fall through to nothing/label

    # Custom image icon -> fill the key bounds. cover = crop to fill; contain = the WHOLE
    # image letterboxed (never crops a non-square PNG), padded with the background colour.
    if has_icon and _looks_like_path(icon):
        if has_icon_style(item):                     # zoom / rounded corners / app-tile
            styled = _render_image_icon(img, icon, item, size, has_label, fit)
            if styled is not None:
                img = styled
                if has_label:
                    _label_overlay()
                return img
        try:
            src = Image.open(icon).convert("RGB")
            if fit == "contain":
                src = ImageOps.pad(src, size, color=bg, centering=(0.5, 0.5))
            else:
                src = ImageOps.fit(src, size, Image.LANCZOS)
            img.paste(src, (0, 0))
            if has_label:
                _label_overlay()
            return img
        except (OSError, ValueError):
            pass

    # Emoji icon WITH custom styling -> route through the SAME pipeline as image icons so the
    # Customize controls (zoom / move / rotate / opacity / roundness / tile / border / shadow)
    # apply to emoji too. Rendered big for crisp scaling; always aspect-preserving (no crop).
    if has_icon and not _looks_like_path(icon) and has_icon_style(item):
        em = emoji_image(icon, int(max(w, h) * 1.8))
        if em is not None:
            styled = _render_styled_icon(img, em, item, size, has_label, "contain")
            if styled is not None:
                img = styled
                if has_label:
                    _label_overlay()
                return img

    # Emoji icon -> fill the key with a big glyph (cover), unless fit=="contain".
    if has_icon and not _looks_like_path(icon) and fit == "cover":
        band = 18 if has_label else 0                     # reserve the bottom label strip
        avail_h = h - band
        # Size off the SMALLER of width / available-height so a wide or tall glyph (or a
        # non-square calibrated tile) is never clipped; max_box is a hard clamp.
        target = int(min(w, avail_h) * 0.98)
        cy = avail_h // 2
        if _draw_emoji(img, icon, w // 2, cy, target, max_box=(w - 2, avail_h - 2)):
            if has_label:
                _label_overlay()
            return img

    icon_drawn = False
    if has_icon:
        icon_cy = int(h * 0.29) if has_label else h // 2
        target = int((h * 0.44) if has_label else (h * 0.62))
        if _looks_like_path(icon):
            try:
                src = Image.open(icon).convert("RGBA")
                src.thumbnail((target, target), Image.LANCZOS)
                img.paste(src, (w // 2 - src.width // 2, icon_cy - src.height // 2), src)
                icon_drawn = True
            except (OSError, ValueError):
                icon_drawn = False
        else:
            avail_h = (h - 18) if has_label else h
            icon_drawn = _draw_emoji(img, icon, w // 2, icon_cy, target,
                                     max_box=(w - 2, avail_h - 2))
        # If an emoji/icon failed, treat its text as a label fallback.
        if not icon_drawn and not has_label:
            label, has_label = icon, True

    if has_label:
        if icon_drawn:
            font = _font(12)
            lines = _wrap(draw, label, font, w - 4)
            # Center the label within the bottom band so nothing clips at the edge.
            band_top = int(h * 0.58)
            total = len(lines) * font.size + (len(lines) - 1)
            y = band_top + (h - band_top - total) // 2 + font.size // 2
            for ln in lines:
                draw.text((w // 2, y), ln, font=font, fill=fg, anchor="mm")
                y += font.size + 1
        else:
            # Center, larger; shrink font to fit up to 2 lines.
            for fs in (22, 19, 16, 14, 12):
                font = _font(fs)
                lines = _wrap(draw, label, font, w - 6)
                if all(draw.textlength(ln, font=font) <= w - 6 for ln in lines):
                    break
            total_h = len(lines) * (font.size + 2)
            y = (h - total_h) // 2 + font.size // 2
            for ln in lines:
                draw.text((w // 2, y), ln, font=font, fill=fg, anchor="mm")
                y += font.size + 2

    return img


def blank_face(color: str = DEFAULT_BG, size: Optional[Tuple[int, int]] = None) -> Image.Image:
    return Image.new("RGB", size or KEY_SIZE, parse_color(color))


def _rounded_mask(size: Tuple[int, int], radius: int) -> Image.Image:
    ss = 4                                                # supersample -> smooth (anti-aliased) corners
    big = (max(1, size[0] * ss), max(1, size[1] * ss))
    m = Image.new("L", big, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, big[0] - 1, big[1] - 1], radius=radius * ss, fill=255)
    return m.resize(size, Image.LANCZOS)


# Folder contents are previewed from these slots (key6 becomes the 'Back' tile at runtime).
_FOLDER_PREVIEW_KEYS = ("key1", "key2", "key3", "key4", "key5", "key6")


def folder_face(item: Dict[str, Any], contents: Dict[str, Any],
                size: Optional[Tuple[int, int]] = None, show_label: bool = True) -> Image.Image:
    """An iOS/macOS-style folder tile: the contained keys' icons in a mini-grid on a tray,
    so a folder key is instantly recognisable and shows what's inside.

    `item` is the folder key's binding (colour/label); `contents` is the folder's items dict.
    Cached like render_face — the contents dict is part of the signature.
    """
    if size is None:
        size = KEY_SIZE
    try:
        cblob = json.dumps(contents, sort_keys=True, default=str)
    except (TypeError, ValueError):
        cblob = None
    sig = _face_sig("folder", item, size, show_label, (cblob,)) if cblob is not None else None
    hit = _face_cache_get(sig)
    if hit is not None:
        return hit
    return _face_cache_put(sig, _folder_face_impl(item, contents, size, show_label))


def _folder_face_impl(item: Dict[str, Any], contents: Dict[str, Any],
                      size: Tuple[int, int], show_label: bool) -> Image.Image:
    w, h = size
    bg = parse_color(item.get("color"), DEFAULT_BG)
    lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    fg = (24, 26, 30) if lum > 140 else parse_color(DEFAULT_FG)
    img = Image.new("RGB", size, bg)

    label = (item.get("label") or "").strip() if show_label else ""
    has_label = bool(label)
    band = 18 if has_label else 0

    pad = max(3, int(min(w, h) * 0.08))
    tx0, ty0, tx1, ty1 = pad, pad, w - pad, h - band - pad
    tw, th = max(1, tx1 - tx0), max(1, ty1 - ty0)

    # A "frosted glass" folder tray — a dark, translucent rounded panel holding up to 6 rounded
    # thumbnails (3x2, or 2x2 for <=4) with recessed wells + hairline edges. Drawn on a SEPARATE layer
    # so the whole thing can be resized per key. (Design-panel "frosted-panel", tuned darker.)
    content = Image.new("RGBA", size, (0, 0, 0, 0))
    prad = max(4, int(min(tw, th) * 0.20))
    pmask = _rounded_mask((tw, th), prad)
    panel = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    panel.paste(Image.new("RGBA", (tw, th), (26, 30, 39, 208)), (0, 0), pmask)   # dark frosted fill
    pdraw = ImageDraw.Draw(panel)
    pdraw.rounded_rectangle([0, 0, tw - 1, th - 1], radius=prad,               # soft top-lit rim
                            outline=(255, 255, 255, 40), width=max(1, int(tw * 0.012)))
    content.alpha_composite(panel, (tx0, ty0))

    picks = [contents.get(k) for k in _FOLDER_PREVIEW_KEYS]
    # An item is previewable if it has ANY visible face: an icon, a label, live data, or — only when
    # auto-icons are on (which is what actually draws them) — a colour/gradient swatch or an
    # action-derived Fluent auto-icon. Mirrors exactly what _render_face_impl paints for the mini.
    picks = [it for it in picks
             if it and (it.get("icon") or it.get("label") or it.get("live")
                        or (AUTO_ICONS and (has_swatch(it) or resolve_auto_icon(it))))][:6]
    if picks:
        n = len(picks)
        cols = 2 if n <= 4 else 3
        ipad = max(2, int(min(tw, th) * 0.11))
        gap = max(2, int(min(tw, th) * 0.07))
        gw, gh = tw - ipad * 2, th - ipad * 2
        cell = max(6, min((gw - gap * (cols - 1)) // cols, (gh - gap) // 2))
        crad = max(2, int(cell * 0.24))
        cmask = _rounded_mask((cell, cell), crad)
        grid_w, grid_h = cell * cols + gap * (cols - 1), cell * 2 + gap
        ox, oy = tx0 + (tw - grid_w) // 2, ty0 + (th - grid_h) // 2
        cdraw = ImageDraw.Draw(content)
        for idx, it in enumerate(picks):
            r, c = divmod(idx, cols)
            x, y = ox + c * (cell + gap), oy + r * (cell + gap)
            cdraw.rounded_rectangle([x, y, x + cell - 1, y + cell - 1], radius=crad, fill=(0, 0, 0, 80))
            mini = render_face(it, size=(cell, cell), show_label=False).convert("RGBA")
            content.paste(mini, (x, y), cmask)
            cdraw.rounded_rectangle([x, y, x + cell - 1, y + cell - 1], radius=crad,
                                    outline=(255, 255, 255, 36), width=max(1, int(cell * 0.03)))
    else:
        _draw_emoji(content, "📁", w // 2, (h - band) // 2, int(min(w, h - band) * 0.62),
                    max_box=(w - 6, (h - band) - 6))

    sc = max(0.4, min(1.6, float(item.get("icon_scale", 1.0))))   # resize the folder icon per key
    if abs(sc - 1.0) > 0.01:
        nw, nh = max(1, int(w * sc)), max(1, int(h * sc))
        scaled = content.resize((nw, nh), Image.LANCZOS)
        content = Image.new("RGBA", size, (0, 0, 0, 0))
        content.paste(scaled, (int(w / 2 * (1 - sc)), int((h - band) / 2 * (1 - sc))), scaled)
    img = Image.alpha_composite(img.convert("RGBA"), content).convert("RGB")

    if has_label:
        overlay = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle([0, h - 18, w, h], fill=(0, 0, 0, 150))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        d = ImageDraw.Draw(img)
        font = _font(12)
        band_fg = fg if item.get("text_color") else parse_color(DEFAULT_FG)   # white on the dark scrim
        d.text((w // 2, h - 9), _elide(d, label, font, w - 4), font=font, fill=band_fg, anchor="mm")

    return img


def calib_pattern(size) -> Image.Image:
    """A measurable alignment GRID for the display calibrator — it makes the firmware's edge
    clipping (the black gap) obvious and shows which way is up.

    `size` is (w, h) — or a bare int for a square tile. Deliberately NOT symmetric so the axis
    mapping is self-evident on the device:
      * a faint grid (8 cells each way) lets you COUNT how much of an edge is lost to the gap,
      * a THICK yellow edge frame + white CORNER BRACKETS reveal whether content reaches each
        edge/corner (grow W/H + drop Edge frame until the frame hugs the glass on all sides),
      * a CYAN band marks the TOP, a RED band marks the BOTTOM (is "down" really down?),
      * a green crosshair + white ring/dot marks the centre.
    """
    w, h = (size, size) if isinstance(size, int) else size
    img = Image.new("RGB", (w, h), (16, 22, 30))
    d = ImageDraw.Draw(img)
    # faint reference grid (8 columns/rows) — count the cells a gap eats
    sx, sy = max(4, w // 8), max(4, h // 8)
    for x in range(0, w, sx):
        d.line([(x, 0), (x, h)], fill=(58, 78, 94), width=1)
    for y in range(0, h, sy):
        d.line([(0, y), (w, y)], fill=(58, 78, 94), width=1)
    # orientation bands (thin, so they don't hide the edge frame)
    band = max(4, h // 9)
    d.rectangle([0, 0, w - 1, band], fill=(0, 190, 210))             # TOP = cyan
    d.rectangle([0, h - 1 - band, w - 1, h - 1], fill=(220, 40, 40)) # BOTTOM = red
    # centre crosshair + ring + dot
    cx, cy = w / 2.0, h / 2.0
    d.line([(cx, 0), (cx, h)], fill=(0, 255, 170), width=2)
    d.line([(0, cy), (w, cy)], fill=(0, 255, 170), width=2)
    r = min(w, h) * 0.26
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255), width=2)
    d.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill=(255, 255, 255))
    # thick yellow edge frame (drawn over the bands so the very edge stays visible)
    for i in range(3):
        d.rectangle([i, i, w - 1 - i, h - 1 - i], outline=(255, 210, 0))
    # bright white corner brackets — confirm every corner reaches the glass
    cl = max(8, min(w, h) // 5)
    for ax, ay, dxs, dys in ((0, 0, 1, 1), (w - 1, 0, -1, 1), (0, h - 1, 1, -1), (w - 1, h - 1, -1, -1)):
        d.line([(ax, ay), (ax + dxs * cl, ay)], fill=(255, 255, 255), width=3)
        d.line([(ax, ay), (ax, ay + dys * cl)], fill=(255, 255, 255), width=3)
    return img


def _heat_color(frac: float) -> Tuple[int, int, int]:
    """Green -> amber -> red as frac rises 0..1 (for gauges / battery / temps)."""
    frac = max(0.0, min(1.0, frac))
    if frac < 0.6:
        return (53, 224, 138)
    if frac < 0.82:
        return (245, 188, 50)
    return (236, 78, 70)


def _fit_font_h(d, text, max_w, base_h):
    """Largest bold font ~base_h px tall that still fits text in max_w (shrinks if needed)."""
    s = base_h
    while s > base_h * 0.42:
        f = _font(max(8, int(s)), bold=True)
        if d.textlength(text or "--", font=f) <= max_w:
            return f
        s *= 0.9
    return _font(max(8, int(base_h * 0.42)), bold=True)


def _ellipsize(d, text, font, max_w):
    """Trim text to fit max_w, adding an ellipsis (for long song titles on a tiny key)."""
    text = text or ""
    if d.textlength(text, font=font) <= max_w:
        return text
    while text and d.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return (text.rstrip() + "…") if text else "…"


def _glyph_cpu(d, cx, cy, s, col, wd):
    d.rounded_rectangle([cx - s, cy - s, cx + s, cy + s], radius=max(2, s // 4), outline=col, width=wd)
    i = s * 0.42
    d.rectangle([cx - i, cy - i, cx + i, cy + i], outline=col, width=wd)
    for k in (-0.45, 0.45):                                    # pins on all four sides
        for t in (-1, 1):
            d.line([(cx + s * k, cy + s * t), (cx + s * k, cy + s * t * 1.5)], fill=col, width=wd)
            d.line([(cx + s * t, cy + s * k), (cx + s * t * 1.5, cy + s * k)], fill=col, width=wd)


def _glyph_gpu(d, cx, cy, s, col, wd):
    d.rounded_rectangle([cx - s * 1.25, cy - s * 0.7, cx + s * 1.05, cy + s * 0.7],
                        radius=max(2, s // 5), outline=col, width=wd)
    fx = cx + s * 0.25
    d.ellipse([fx - s * 0.5, cy - s * 0.5, fx + s * 0.5, cy + s * 0.5], outline=col, width=wd)
    d.ellipse([fx - wd, cy - wd, fx + wd, cy + wd], fill=col)
    for a in (0, 90, 180, 270):                               # fan blades
        x = fx + math.cos(math.radians(a)) * s * 0.42
        y = cy + math.sin(math.radians(a)) * s * 0.42
        d.line([(fx, cy), (x, y)], fill=col, width=max(1, wd // 2))
    d.line([(cx - s * 1.25, cy + s * 0.7), (cx - s * 1.25, cy + s * 1.15)], fill=col, width=wd)


def _glyph_ram(d, cx, cy, s, col, wd):
    # a memory module: wide board + a row of chips + a gold-contact edge with an off-centre notch
    bw, bh = s * 2.3, s * 1.5
    x0, y0, x1, y1 = cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2
    d.rounded_rectangle([x0, y0, x1, y1], radius=max(2, int(s * 0.18)), outline=col, width=wd)
    cw_, ch_ = bw * 0.16, bh * 0.30                           # filled chips on the upper board
    cyk = y0 + bh * 0.34
    for i in range(4):
        xk = x0 + bw * 0.11 + i * (cw_ + bw * 0.055)
        d.rectangle([xk, cyk - ch_ / 2, xk + cw_, cyk + ch_ / 2], fill=col)
    top, bot = y1 - bh * 0.26, y1 - wd                        # contact teeth, one gap = the notch
    n, notch = 7, 4
    step = (bw * 0.8) / (n - 1)
    sx = x0 + bw * 0.1
    for i in range(n):
        if i == notch:
            continue
        tx = sx + i * step
        d.line([(tx, top), (tx, bot)], fill=col, width=max(1, int(wd * 0.85)))


def _glyph_disk(d, cx, cy, s, col, wd):
    # a hard drive: body + spinning platter + actuator arm
    d.rounded_rectangle([cx - s * 1.1, cy - s, cx + s * 1.1, cy + s],
                        radius=max(2, int(s * 0.2)), outline=col, width=wd)
    d.ellipse([cx - s * 0.62, cy - s * 0.62, cx + s * 0.62, cy + s * 0.62], outline=col, width=wd)
    d.ellipse([cx - wd, cy - wd, cx + wd, cy + wd], fill=col)
    d.line([(cx + s * 0.9, cy - s * 0.75), (cx + s * 0.2, cy + s * 0.12)], fill=col, width=wd)


_GAUGE_GLYPHS = {"CPU": _glyph_cpu, "GPU": _glyph_gpu, "RAM": _glyph_ram, "DISK": _glyph_disk}

# ---- state glyphs (for stateful keys: mic / caps lock / smart bulb) -------------------------
_ON = (53, 224, 138)        # mint  = good / active
_ALERT = (236, 78, 70)      # red   = muted / alert
_OFFCOL = (130, 140, 152)   # grey  = off / inactive
_GLOW = (255, 200, 70)      # warm  = bulb on


def _cut_slash(d, p0, p1, col, lw):
    """Discord-style mute slash: a transparent 'cut' (the icon's solid shape gets a gap) with the
    coloured slash drawn inside it. Works because the icon is on a transparent RGBA content layer."""
    d.line([p0, p1], fill=(0, 0, 0, 0), width=lw + 6)
    d.line([p0, p1], fill=col, width=lw)


def _glyph_mic(d, cx, cy, s, on, lw):
    """Microphone — Discord-style FILLED silhouette. `on` == MUTED -> red + a cut slash; else mint."""
    col = _ALERT if on else _ON
    aw = max(2, int(lw * 0.9))
    cw = s * 0.6
    d.rounded_rectangle([cx - cw, cy - s * 1.3, cx + cw, cy + s * 0.05], radius=cw, fill=col)   # head
    d.arc([cx - s * 0.98, cy - s * 0.45, cx + s * 0.98, cy + s * 0.72], start=8, end=172,
          fill=col, width=aw)                                                                   # cradle
    d.line([(cx, cy + s * 0.72), (cx, cy + s * 1.22)], fill=col, width=aw)                       # stem
    d.line([(cx - s * 0.5, cy + s * 1.24), (cx + s * 0.5, cy + s * 1.24)], fill=col, width=aw)   # foot
    if on:
        _cut_slash(d, (cx - s * 1.25, cy - s * 1.5), (cx + s * 1.25, cy + s * 1.2), col, lw)


def _glyph_caps(d, cx, cy, s, on, lw):
    """Caps-lock chevron over a bar; filled mint when on, grey outline when off."""
    col = _ON if on else _OFFCOL
    d.line([(cx, cy - s * 1.1), (cx - s, cy)], fill=col, width=lw)
    d.line([(cx, cy - s * 1.1), (cx + s, cy)], fill=col, width=lw)
    for sx in (-s * 0.5, s * 0.5):
        d.line([(cx + sx, cy), (cx + sx, cy + s * 0.55)], fill=col, width=lw)
    d.line([(cx - s * 0.5, cy + s * 0.55), (cx + s * 0.5, cy + s * 0.55)], fill=col, width=lw)
    d.line([(cx - s, cy + s * 1.05), (cx + s, cy + s * 1.05)], fill=col, width=lw)


def _glyph_bulb(d, cx, cy, s, on, lw):
    """Light bulb; glowing amber + rays when on, grey outline when off."""
    col = _GLOW if on else _OFFCOL
    r = s
    d.ellipse([cx - r, cy - r * 1.15, cx + r, cy + r * 0.75],
              outline=col, width=lw, fill=(_GLOW if on else None))
    d.line([(cx - r * 0.4, cy + r * 0.75), (cx - r * 0.4, cy + r * 1.05)], fill=col, width=lw)
    d.line([(cx + r * 0.4, cy + r * 0.75), (cx + r * 0.4, cy + r * 1.05)], fill=col, width=lw)
    d.line([(cx - r * 0.32, cy + r * 1.25), (cx + r * 0.32, cy + r * 1.25)], fill=col, width=lw)
    if on:
        for a in (235, 270, 305):
            x, y = cx + math.cos(math.radians(a)) * r * 1.7, cy + math.sin(math.radians(a)) * r * 1.7
            x2, y2 = cx + math.cos(math.radians(a)) * r * 2.2, cy + math.sin(math.radians(a)) * r * 2.2
            d.line([(x, y), (x2, y2)], fill=col, width=lw)


def _glyph_rgb(d, cx, cy, s, on, lw):
    """RGB lights — three overlapping R/G/B discs (the additive-colour mark); grey rings when off."""
    r = s * 0.62
    o = s * 0.46
    trio = ((cx, cy - o, (255, 66, 74)),
            (cx - o * 0.95, cy + o * 0.62, (70, 214, 108)),
            (cx + o * 0.95, cy + o * 0.62, (74, 132, 255)))
    for px, py, col in trio:
        box = [px - r, py - r, px + r, py + r]
        if on:
            d.ellipse(box, fill=col)
        else:
            d.ellipse(box, outline=_OFFCOL, width=lw)


def _glyph_discord(d, cx, cy, s, on, lw):
    """A headset (Discord voice) — Discord-style FILLED. Muted/deafened -> red + cut slash; live -> mint."""
    col = _ALERT if on else _ON
    aw = max(3, int(lw * 1.1))
    d.arc([cx - s * 1.1, cy - s * 1.15, cx + s * 1.1, cy + s * 0.65], start=180, end=360,
          fill=col, width=aw)                                          # headband
    d.rounded_rectangle([cx - s * 1.22, cy - s * 0.15, cx - s * 0.58, cy + s * 0.92],
                        radius=s * 0.3, fill=col)                      # left cup (filled)
    d.rounded_rectangle([cx + s * 0.58, cy - s * 0.15, cx + s * 1.22, cy + s * 0.92],
                        radius=s * 0.3, fill=col)                      # right cup (filled)
    d.arc([cx - s * 0.15, cy + s * 0.45, cx + s * 1.0, cy + s * 1.6], start=265, end=360,
          fill=col, width=aw)                                          # mic boom
    d.ellipse([cx + s * 0.78, cy + s * 1.0, cx + s * 1.14, cy + s * 1.36], fill=col)   # boom tip
    if on:
        _cut_slash(d, (cx - s * 1.4, cy - s * 1.3), (cx + s * 1.4, cy + s * 1.3), col, lw)


def _glyph_deaf(d, cx, cy, s, on, lw):
    """Headphones — Discord-style FILLED. Deafened -> red + cut slash; listening -> mint."""
    col = _ALERT if on else _ON
    aw = max(3, int(lw * 1.15))
    d.arc([cx - s * 1.1, cy - s * 1.15, cx + s * 1.1, cy + s * 0.78], start=180, end=360,
          fill=col, width=aw)                                          # band
    d.rounded_rectangle([cx - s * 1.22, cy - s * 0.02, cx - s * 0.56, cy + s * 1.05],
                        radius=s * 0.32, fill=col)                     # left cup (filled)
    d.rounded_rectangle([cx + s * 0.56, cy - s * 0.02, cx + s * 1.22, cy + s * 1.05],
                        radius=s * 0.32, fill=col)                     # right cup (filled)
    if on:
        _cut_slash(d, (cx - s * 1.4, cy - s * 1.3), (cx + s * 1.4, cy + s * 1.3), col, lw)


def _glyph_call(d, cx, cy, s, on, lw):
    """In a voice call — sound waves around a dot; connected -> mint, idle -> grey."""
    col = _ON if on else _OFFCOL
    d.ellipse([cx - s * 0.32, cy - s * 0.32, cx + s * 0.32, cy + s * 0.32], fill=col)
    for r in (s * 0.85, s * 1.4):
        d.arc([cx - r, cy - r, cx + r, cy + r], start=-52, end=52, fill=col, width=lw)
        d.arc([cx - r, cy - r, cx + r, cy + r], start=128, end=232, fill=col, width=lw)


def _glyph_dmode(d, cx, cy, s, on, lw):
    """Talk mode — push-to-talk (a pressable button) vs voice activity (a waveform)."""
    col = _ON if on else _OFFCOL
    if on:                                                   # PUSH-TO-TALK = a button with a dot
        d.rounded_rectangle([cx - s, cy - s, cx + s, cy + s], radius=s * 0.4, outline=col, width=lw)
        d.ellipse([cx - s * 0.34, cy - s * 0.34, cx + s * 0.34, cy + s * 0.34], fill=col)
    else:                                                    # VOICE ACTIVITY = a little waveform
        bw = max(2, int(lw * 0.85))
        for i, hgt in enumerate((0.5, 0.95, 0.65, 1.15, 0.55)):
            x = cx + (i - 2) * s * 0.55
            d.line([(x, cy - s * hgt * 0.75), (x, cy + s * hgt * 0.75)], fill=col, width=bw)


def _glyph_speaker(d, cx, cy, s, frac, lw, accent, dim):
    """A speaker with sound waves whose count tracks the level (and a slash at 0)."""
    d.rectangle([cx - s, cy - s * 0.45, cx - s * 0.4, cy + s * 0.45], fill=accent)           # magnet box
    d.polygon([(cx - s * 0.4, cy - s * 0.45), (cx + s * 0.3, cy - s),
               (cx + s * 0.3, cy + s), (cx - s * 0.4, cy + s * 0.45)], fill=accent)           # cone
    nwaves = 0 if (frac is None or frac <= 0.01) else (1 if frac < 0.4 else (2 if frac < 0.8 else 3))
    ww = max(2, int(lw * 0.9))
    for i in range(3):
        rr = s * (0.55 + i * 0.42)
        col = accent if i < nwaves else dim
        d.arc([cx + s * 0.3 - rr, cy - rr, cx + s * 0.3 + rr, cy + rr], start=-50, end=50,
              fill=col, width=ww)
    if frac is not None and frac <= 0.01:
        d.line([(cx - s * 1.1, cy - s), (cx + s * 1.35, cy + s)], fill=accent, width=lw + 1)


def _glyph_dnoise(d, cx, cy, s, on, lw):
    """Noise suppression — a shield, filled mint when on, grey outline when off."""
    col = _ON if on else _OFFCOL
    pts = [(cx, cy - s * 1.2), (cx + s, cy - s * 0.65), (cx + s, cy + s * 0.25),
           (cx, cy + s * 1.2), (cx - s, cy + s * 0.25), (cx - s, cy - s * 0.65)]
    d.polygon(pts, outline=col, width=lw, fill=(col if on else None))


def _glyph_obs_rec(d, cx, cy, s, on, lw):
    """OBS recording — a solid red dot when rolling, a grey ring when idle."""
    col = _ALERT if on else _OFFCOL
    if on:
        d.ellipse([cx - s, cy - s, cx + s, cy + s], fill=col)
    else:
        d.ellipse([cx - s, cy - s, cx + s, cy + s], outline=col, width=lw)


def _glyph_obs_stream(d, cx, cy, s, on, lw):
    """OBS streaming — a broadcast dot with signal arcs; red 'on air' when live, grey when off."""
    col = _ALERT if on else _OFFCOL
    ww = max(2, int(lw * 0.9))
    d.ellipse([cx - s * 0.42, cy - s * 0.42, cx + s * 0.42, cy + s * 0.42], fill=col)   # the dot
    for r in (s * 0.95, s * 1.5):                                                        # signal arcs
        d.arc([cx - r, cy - r, cx + r, cy + r], start=-55, end=55, fill=col, width=ww)
        d.arc([cx - r, cy - r, cx + r, cy + r], start=125, end=235, fill=col, width=ww)


def _glyph_obs_cam(d, cx, cy, s, on, lw):
    """OBS virtual camera — a video-camera silhouette; mint when on, grey when off."""
    col = _ON if on else _OFFCOL
    d.rounded_rectangle([cx - s * 1.15, cy - s * 0.7, cx + s * 0.32, cy + s * 0.7],
                        radius=s * 0.22, outline=col, width=lw)                          # body
    d.polygon([(cx + s * 0.42, cy - s * 0.05), (cx + s * 1.15, cy - s * 0.55),
               (cx + s * 1.15, cy + s * 0.55), (cx + s * 0.42, cy + s * 0.05)],
              outline=col, width=lw)                                                      # lens horn


def _glyph_obs_replay(d, cx, cy, s, on, lw):
    """OBS replay buffer — a double rewind chevron (◀◀); mint when armed, grey when off."""
    col = _ON if on else _OFFCOL
    for dx in (s * 0.25, s * -0.65):
        d.line([(cx + dx + s * 0.55, cy - s * 0.8), (cx + dx - s * 0.2, cy)], fill=col, width=lw)
        d.line([(cx + dx - s * 0.2, cy), (cx + dx + s * 0.55, cy + s * 0.8)], fill=col, width=lw)


def _glyph_screens(d, cx, cy, s, on, lw):
    """PC monitors — a screen on a stand; lit mint screen when on, grey outline + cut slash when off."""
    col = _ON if on else _OFFCOL
    d.rounded_rectangle([cx - s * 1.3, cy - s * 1.0, cx + s * 1.3, cy + s * 0.6],
                        radius=s * 0.18, outline=col, width=lw, fill=(col if on else None))
    d.line([(cx, cy + s * 0.6), (cx, cy + s * 1.05)], fill=col, width=lw)             # neck
    d.line([(cx - s * 0.6, cy + s * 1.1), (cx + s * 0.6, cy + s * 1.1)], fill=col, width=lw)
    if not on:
        _cut_slash(d, (cx - s * 1.4, cy - s * 1.3), (cx + s * 1.4, cy + s * 1.3), col, lw)


_STATE_GLYPHS = {"MIC": _glyph_mic, "CAPS": _glyph_caps, "LIGHT": _glyph_bulb, "RGB": _glyph_rgb,
                 "DISCORD": _glyph_discord, "DMIC": _glyph_mic, "DDEAF": _glyph_deaf,
                 "DCALL": _glyph_call, "DMODE": _glyph_dmode, "DNOISE": _glyph_dnoise,
                 "STREAM": _glyph_obs_stream, "REC": _glyph_obs_rec,
                 "V-CAM": _glyph_obs_cam, "REPLAY": _glyph_obs_replay, "SCREENS": _glyph_screens}
_MEDIA_BG_CACHE: dict = {}      # 1-entry cache of the scrimmed cover-art background (marquee reuse)
LIVE_STYLE_ORDER = ["gauge", "ring", "bar", "dial", "card", "graph", "minimal"]
LIVE_STYLE_LABELS = {"gauge": "Gauge", "ring": "Ring", "bar": "Bar", "dial": "Dial",
                     "card": "Card", "graph": "Graph", "minimal": "Minimal"}


def _card_tile(content, d, w, h, text, caption, frac, accent, fg, dim, track):
    """A modern dashboard 'card' live-tile: big value top-left, dim caption, and a smooth accent
    sparkline hugging the bottom (fill height + wave ride with `frac`). From a design-panel bake-off."""
    text = "" if text is None else str(text)
    caption = "" if caption is None else str(caption)
    pad = int(w * 0.12)

    vsize = int(h * 0.36)
    vf = _font(vsize, bold=True)

    def measure(f, s):
        b = d.textbbox((0, 0), s, font=f)
        return b[2] - b[0], b[3] - b[1], b[1]

    max_vw = w - pad * 2
    tw, th, toff = measure(vf, text or "--")
    while tw > max_vw and vsize > int(h * 0.16):
        vsize -= max(1, int(h * 0.02))
        vf = _font(vsize, bold=True)
        tw, th, toff = measure(vf, text or "--")
    vy = int(h * 0.10)
    d.text((pad, vy - toff), text or "--", font=vf, fill=fg)

    cap = caption.upper()
    csize = max(9, int(h * 0.135))
    cf = _font(csize, bold=True)
    cy = vy + th + int(h * 0.04)
    cx = pad
    trk = max(1, int(csize * 0.10))
    for ch in cap:
        d.text((cx, cy), ch, font=cf, fill=dim)
        cw = measure(cf, ch if ch != " " else " ")[0]
        cx += cw + trk
        if cx > w - pad:
            break

    f = 0.0 if frac is None else max(0.0, min(1.0, float(frac)))
    band_top = int(h * 0.60); band_bot = h - int(h * 0.06); band_h = band_bot - band_top
    x0, x1 = pad, w - pad
    span = max(1, x1 - x0)
    rest = band_bot - int(band_h * (0.18 + 0.64 * f))
    amp = band_h * (0.10 + 0.16 * f)
    phase = f * math.pi

    def wave_y(x):
        t = (x - x0) / span
        y = rest - amp * math.sin(t * math.pi * 2.0 + phase) \
                 - amp * 0.35 * math.sin(t * math.pi * 4.0 + phase * 0.5)
        return max(band_top, min(band_bot, y))

    step = max(1, int(w / 120))
    pts = [(x, wave_y(x)) for x in range(x0, x1 + 1, step)]
    if pts and pts[-1][0] != x1:
        pts.append((x1, wave_y(x1)))
    if len(pts) >= 2:
        fill_img = _vgrad(w, h, accent, (0, 0, 0))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).polygon(pts + [(x1, band_bot), (x0, band_bot)], fill=110)
        content.paste(fill_img, (0, 0), mask)
        d.line(pts, fill=accent, width=max(2, int(w * 0.012)), joint="curve")
        ex, ey = pts[-1]
        r = max(2, int(w * 0.018))
        d.ellipse((ex - r, ey - r, ex + r, ey + r), fill=fg)
    d.line([(x0, band_bot), (x1, band_bot)], fill=track, width=max(1, int(h * 0.006)))


_MEDIA_TITLE_FRAC = 0.155          # title font height as a fraction of the (supersampled) tile
_MEDIA_AVAIL_FRAC = 0.92           # usable text width as a fraction of the tile
_MEDIA_SS = 3                      # live_face supersample (kept in sync with `ss` below)


def media_overflows(text: str, size: Optional[Tuple[int, int]] = None) -> bool:
    """True if a now-playing title is too wide to fit a key (so it should scroll, not ellipsize)."""
    if not text or text == "--":
        return False
    size = size or KEY_SIZE
    h = size[1] * _MEDIA_SS
    avail = size[0] * _MEDIA_SS * _MEDIA_AVAIL_FRAC
    d = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    return d.textlength(text, font=_font(int(h * _MEDIA_TITLE_FRAC), bold=True)) > avail


def _vgrad(w, h, top, bot):
    """Vertical linear gradient RGB image: `top` at the top edge -> `bot` at the bottom."""
    w, h = max(1, int(w)), max(1, int(h))
    m = Image.linear_gradient("L").resize((w, h))            # 0 (top) .. 255 (bottom)
    return Image.composite(Image.new("RGB", (w, h), bot), Image.new("RGB", (w, h), top), m)


def _rgrad(dia, inner, outer):
    """Radial gradient RGB square: `inner` at the centre -> `outer` at the rim."""
    dia = max(1, int(dia))
    m = Image.radial_gradient("L").resize((dia, dia))        # 0 (centre) .. 255 (edge)
    return Image.composite(Image.new("RGB", (dia, dia), outer), Image.new("RGB", (dia, dia), inner), m)


def _safe_ac(base, layer, x, y):
    """alpha_composite `layer` onto `base` at (x, y), clipping anything off-canvas (negative ok)."""
    bw, bh = base.size; lw, lh = layer.size
    sx, sy = max(0, -int(x)), max(0, -int(y))
    dx, dy = max(0, int(x)), max(0, int(y))
    cw, ch = min(lw - sx, bw - dx), min(lh - sy, bh - dy)
    if cw <= 0 or ch <= 0:
        return
    base.alpha_composite(layer.crop((sx, sy, sx + cw, sy + ch)), dest=(dx, dy))


def _wglow(content, mask, x0, y0, color, pad, blur, alpha):
    """Composite a soft blurred glow of `color` behind shape `mask` (an "L" image) placed at (x0, y0)."""
    pad = max(0, int(pad))
    big = Image.new("L", (mask.size[0] + 2 * pad, mask.size[1] + 2 * pad), 0)
    big.paste(mask, (pad, pad))
    big = big.filter(ImageFilter.GaussianBlur(blur))
    lay = Image.new("RGBA", big.size, tuple(color) + (0,))
    lay.putalpha(big.point(lambda v: int(v * alpha / 255)))
    _safe_ac(content, lay, x0 - pad, y0 - pad)


def _cloud_puffy(content, cx, cy, s, top, bot, glow_white=False):
    """A plump, fluffy cumulus cloud centred at (cx, cy): a single union-mask silhouette (one dominant
    crown lobe + smaller puffs + a wide softly-rounded base) filled by ONE vertical gradient, with a
    soft cool inner shadow under the lobes and a top rim highlight for volume, plus a cool halo (and an
    extra white halo when `glow_white`) so it lifts off the dark tile. From a design-panel bake-off."""
    W, H = content.size
    x0 = max(0, int(cx - 2.0 * s)); y0 = max(0, int(cy - 1.7 * s))
    x1 = min(W, int(cx + 2.0 * s)); y1 = min(H, int(cy + 1.3 * s))
    mw, mh = x1 - x0, y1 - y0
    if mw <= 0 or mh <= 0:
        return
    lcx, lcy = cx - x0, cy - y0

    def _ell(drw, ox, oy, rx, ry, fill):
        ex, ey = lcx + ox * s, lcy + oy * s
        drw.ellipse([ex - rx * s, ey - ry * s, ex + rx * s, ey + ry * s], fill=fill)

    mask = Image.new("L", (mw, mh), 0); md = ImageDraw.Draw(mask)
    md.rounded_rectangle([lcx - 1.22 * s, lcy + 0.42 * s, lcx + 1.30 * s, lcy + 0.92 * s],
                         radius=0.46 * s, fill=255)                       # wide softly-rounded base
    _ell(md, -1.05, 0.55, 0.55, 0.50, 255); _ell(md, -0.30, 0.62, 0.62, 0.55, 255)
    _ell(md, 0.55, 0.60, 0.62, 0.55, 255); _ell(md, 1.20, 0.55, 0.50, 0.48, 255)  # gentle bottom lobes
    _ell(md, -1.05, 0.12, 0.60, 0.60, 255); _ell(md, -0.45, -0.28, 0.82, 0.82, 255)  # upper-left puffs
    _ell(md, 0.48, -0.52, 1.08, 1.08, 255); _ell(md, 1.10, -0.10, 0.80, 0.80, 255)   # dominant crown

    halo = tuple(max(0, int(c * 0.55)) for c in bot)
    _wglow(content, mask, x0, y0, halo, int(0.4 * s), 0.35 * s, 70)
    if glow_white:
        _wglow(content, mask, x0, y0, (255, 255, 255), int(0.34 * s), 0.26 * s, 150)

    hi = tuple(min(255, int(t + (255 - t) * 0.18)) for t in top)
    body = _vgrad(mw, mh, hi, bot).convert("RGBA"); body.putalpha(mask)

    shadow_src = Image.new("L", (mw, mh), 0); sd = ImageDraw.Draw(shadow_src)   # inner shadow under lobes
    for ox, oy, rx, ry, st in [(0.48, 0.32, 1.0, 0.66, 160), (-0.55, 0.30, 0.85, 0.55, 130),
                               (-1.0, 0.45, 0.6, 0.45, 110), (1.1, 0.35, 0.6, 0.45, 110),
                               (0.0, 0.05, 0.5, 0.4, 120)]:
        _ell(sd, ox, oy, rx, ry, st)
    shadow_src = ImageChops.multiply(shadow_src.filter(ImageFilter.GaussianBlur(0.32 * s)), mask)
    shadow_layer = Image.new("RGBA", (mw, mh), tuple(max(0, int(c * 0.55)) for c in bot) + (0,))
    shadow_layer.putalpha(shadow_src.point(lambda p: int(p * 0.5)))
    body = Image.alpha_composite(body, shadow_layer)

    rim_src = Image.new("L", (mw, mh), 0); rd = ImageDraw.Draw(rim_src)         # top rim highlight
    for ox, oy, rx, ry, st in [(0.48, -0.70, 0.88, 0.72, 205), (-0.45, -0.44, 0.6, 0.5, 150),
                               (-1.05, -0.10, 0.45, 0.4, 120)]:
        _ell(rd, ox, oy, rx, ry, st)
    rim_src = ImageChops.multiply(rim_src.filter(ImageFilter.GaussianBlur(0.22 * s)), mask)
    rim_layer = Image.new("RGBA", (mw, mh), tuple(min(255, int(t + (255 - t) * 0.5)) for t in top) + (0,))
    rim_layer.putalpha(rim_src.point(lambda p: int(p * 0.45)))
    body = Image.alpha_composite(body, rim_layer)

    _safe_ac(content, body, x0, y0)


def _weather_glyph(content, cx, cy, s, cond, dim, lw, night=False):
    """A polished weather-condition icon (sun / cloud / rain / snow / fog / storm) for the weather
    tile — radial/linear gradients, soft glows and rounded edges, drawn onto the RGBA `content` layer.
    With `night`, clear/cloudy show a crescent moon instead of the sun."""
    d = ImageDraw.Draw(content)
    w = max(2, lw)

    def _glow(mask, x0, y0, color, pad, blur, alpha):
        _wglow(content, mask, x0, y0, color, pad, blur, alpha)

    def _disc(scx, scy, r, inner, outer):
        m = Image.new("L", (2 * r, 2 * r), 0)
        ImageDraw.Draw(m).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
        content.paste(_rgrad(2 * r, inner, outer), (scx - r, scy - r), m)
        # crescent highlight in the upper-left for a glassy sheen
        hr = int(r * 0.6)
        hm = Image.new("L", (2 * hr, 2 * hr), 0)
        ImageDraw.Draw(hm).ellipse([0, 0, 2 * hr - 1, 2 * hr - 1], fill=70)
        hl = Image.new("RGBA", (2 * hr, 2 * hr), (255, 255, 255, 0)); hl.putalpha(hm)
        _safe_ac(content, hl, scx - int(r * 0.7), scy - int(r * 0.7))

    def _sun(ox=0, oy=0, rr=0.62, glow=True):
        r = max(3, int(s * rr)); scx, scy = int(cx + ox), int(cy + oy)
        if glow:
            gm = Image.new("L", (2 * r, 2 * r), 0)
            ImageDraw.Draw(gm).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
            _glow(gm, scx - r, scy - r, (255, 190, 70), int(r * 0.9), r * 0.55, 120)
        for a in range(0, 360, 45):                          # rays with rounded tips
            ca, sa = math.cos(math.radians(a)), math.sin(math.radians(a))
            x1, y1 = scx + ca * r * 1.28, scy + sa * r * 1.28
            x2, y2 = scx + ca * r * 1.72, scy + sa * r * 1.72
            d.line([(x1, y1), (x2, y2)], fill=(255, 190, 56), width=w)
            d.ellipse([x2 - w / 2, y2 - w / 2, x2 + w / 2, y2 + w / 2], fill=(255, 190, 56))
        _disc(scx, scy, r, (255, 241, 176), (255, 158, 30))

    def _cloud(top=(252, 253, 255), bot=(200, 212, 230), oy=0.0, scale=1.0, glow_white=False):
        _cloud_puffy(content, cx, int(cy + oy), max(6, int(s * scale)), top, bot, glow_white)

    def _teardrop(scx, scy, dw, dh, top, bot):
        m = Image.new("L", (dw, dh), 0); md = ImageDraw.Draw(m)
        md.ellipse([0, dh - dw, dw - 1, dh - 1], fill=255)
        md.polygon([(dw / 2, 0), (1, dh - dw * 0.5), (dw - 1, dh - dw * 0.5)], fill=255)
        _glow(m, scx, scy, (60, 140, 240), max(2, dw // 2), dw * 0.4, 70)
        content.paste(_vgrad(dw, dh, top, bot), (scx, scy), m)

    def _moon(ox=0, oy=0, rr=0.72):
        r = max(3, int(s * rr)); scx, scy = int(cx + ox), int(cy + oy)
        gm = Image.new("L", (2 * r, 2 * r), 0)
        ImageDraw.Draw(gm).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
        _glow(gm, scx - r, scy - r, (200, 214, 255), int(r * 0.8), r * 0.5, 95)
        for sx, sy, sr in [(1.35, -0.85, 0.10), (1.55, 0.25, 0.07), (0.95, 1.25, 0.08)]:   # a few stars
            rr2 = max(1, int(s * sr))
            d.ellipse([scx + s * sx - rr2, scy + s * sy - rr2, scx + s * sx + rr2, scy + s * sy + rr2],
                      fill=(225, 232, 250))
        pad = 3
        layer = Image.new("RGBA", (2 * r + 2 * pad, 2 * r + 2 * pad), (0, 0, 0, 0))
        m = Image.new("L", (2 * r, 2 * r), 0)
        ImageDraw.Draw(m).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
        layer.paste(_rgrad(2 * r, (250, 250, 240), (206, 214, 230)), (pad, pad), m)
        off = int(r * 0.58)                              # carve a crescent: erase a shifted disc to transparent
        ImageDraw.Draw(layer).ellipse([pad + off, pad - int(r * 0.22),
                                       pad + off + 2 * r, pad - int(r * 0.22) + 2 * r], fill=(0, 0, 0, 0))
        _safe_ac(content, layer, scx - r - pad, scy - r - pad)

    if night and ("clear" in cond or cond in ("", "weather")):
        _moon(rr=0.78)
    elif night and ("cloud" in cond or "overcast" in cond):
        _moon(ox=int(s * 1.1), oy=-int(s * 1.05), rr=0.5)
        _cloud(oy=s * 0.12, scale=0.92, glow_white=True)
    elif cond == "clear":
        _sun(rr=0.82)
    elif "storm" in cond:
        _cloud((210, 218, 230), (150, 160, 176))
        bx0, by0 = int(cx - s * 0.7), int(cy + s * 0.6)
        bw2, bh2 = int(s * 1.4), int(s * 1.15)
        m = Image.new("L", (max(1, bw2), max(1, bh2)), 0)
        pts = [(s * 0.52, 0), (s * 0.18, s * 0.62), (s * 0.5, s * 0.62),
               (s * 0.28, s * 1.15), (s * 1.0, s * 0.42), (s * 0.62, s * 0.42), (s * 0.86, 0)]
        ImageDraw.Draw(m).polygon([(px, py) for px, py in pts], fill=255)
        _glow(m, bx0, by0, (255, 200, 60), max(2, int(s * 0.25)), s * 0.22, 130)
        content.paste(_vgrad(bw2, bh2, (255, 236, 130), (255, 158, 24)), (bx0, by0), m)
    elif "rain" in cond or "drizzle" in cond:
        _cloud()
        dw = max(3, int(s * 0.34)); dh = int(dw * 2.0)
        for dx in (-0.6, 0.0, 0.6):
            _teardrop(int(cx + s * dx - dw / 2), int(cy + s * 0.92), dw, dh,
                      (150, 208, 255), (40, 120, 240))
    elif "snow" in cond:
        _cloud()
        rr = max(3, int(s * 0.2))
        for dx in (-0.6, 0.0, 0.6):
            scx, scy = int(cx + s * dx), int(cy + s * 1.18)
            gm = Image.new("L", (2 * rr, 2 * rr), 0)
            ImageDraw.Draw(gm).ellipse([0, 0, 2 * rr - 1, 2 * rr - 1], fill=255)
            _glow(gm, scx - rr, scy - rr, (255, 255, 255), rr, rr * 0.6, 120)
            _disc(scx, scy, rr, (255, 255, 255), (198, 222, 255))
    elif "fog" in cond:
        bh3 = max(2, int(w * 1.1))
        for i, yy in enumerate((0.12, 0.56, 1.0)):
            wfrac = (0.78, 1.0, 0.86)[i]
            x0 = int(cx - s * wfrac); x1 = int(cx + s * wfrac)
            by = int(cy + s * yy)
            grad = _vgrad(x1 - x0, bh3, (208, 216, 228), (168, 178, 196))
            m = Image.new("L", (x1 - x0, bh3), 0)
            ImageDraw.Draw(m).rounded_rectangle([0, 0, x1 - x0 - 1, bh3 - 1], radius=bh3 // 2, fill=235)
            content.paste(grad, (x0, by), m)
    elif "overcast" in cond:
        _cloud((184, 194, 210), (132, 144, 162), oy=-s * 0.32, scale=0.78)
        _cloud((214, 222, 234), (158, 168, 184), oy=s * 0.12)
    else:                                            # cloudy / partly: sun peeking out top-right of a cloud
        _sun(ox=int(s * 1.15), oy=-int(s * 1.12), rr=0.52)
        _cloud(oy=s * 0.12, scale=0.92, glow_white=True)


def _wx_metric_glyph(content, cx, cy, s, which, cf):
    """A small themed icon for a Weather-center metric tile (thermometer / droplet / wind / sun …),
    drawn with gradients + a soft glow onto the RGBA `content` layer. `which` is the live source id."""
    d = ImageDraw.Draw(content)
    lw = max(2, int(s * 0.22))

    def _disc(scx, scy, r, inner, outer, glow=None):
        r = max(2, int(r))
        m = Image.new("L", (2 * r, 2 * r), 0)
        ImageDraw.Draw(m).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
        if glow:
            _wglow(content, m, int(scx - r), int(scy - r), glow, int(r * 0.8), r * 0.55, 110)
        content.paste(_rgrad(2 * r, inner, outer), (int(scx - r), int(scy - r)), m)

    def _drop(scx, scy, dw, dh, top, bot):
        m = Image.new("L", (dw, dh), 0); md = ImageDraw.Draw(m)
        md.ellipse([0, dh - dw, dw - 1, dh - 1], fill=255)
        md.polygon([(dw / 2, 0), (1, dh - dw * 0.5), (dw - 1, dh - dw * 0.5)], fill=255)
        _wglow(content, m, int(scx), int(scy), (60, 140, 240), max(2, dw // 2), dw * 0.4, 70)
        content.paste(_vgrad(dw, dh, top, bot), (int(scx), int(scy)), m)
        hr = max(2, int(dw * 0.18))                              # glassy highlight
        d.ellipse([scx + dw * 0.34, scy + dh * 0.5, scx + dw * 0.34 + hr, scy + dh * 0.5 + hr * 1.6],
                  fill=(255, 255, 255, 150))

    def _sun(scx, scy, r, inner, outer, rays=True):
        if rays:
            for a in range(0, 360, 45):
                ca, sn = math.cos(math.radians(a)), math.sin(math.radians(a))
                x2, y2 = scx + ca * r * 1.7, scy + sn * r * 1.7
                d.line([(scx + ca * r * 1.25, scy + sn * r * 1.25), (x2, y2)], fill=outer, width=lw)
                d.ellipse([x2 - lw / 2, y2 - lw / 2, x2 + lw / 2, y2 + lw / 2], fill=outer)
        _disc(scx, scy, r, inner, outer, glow=outer)

    def _thermo(col_lo, col_hi):
        tw = max(3, int(s * 0.46)); th = int(s * 1.5); br = int(s * 0.62)
        tx, ty = cx - tw // 2, int(cy - s * 0.95)
        by = ty + th
        shell = (232, 237, 245)
        d.rounded_rectangle([tx, ty, tx + tw, ty + th], radius=tw // 2, fill=shell)
        d.ellipse([cx - br, by - br, cx + br, by + br], fill=shell)
        _disc(cx, by, int(br * 0.74), col_hi, col_lo)            # mercury bulb
        cw = max(2, int(tw * 0.5)); cx0 = cx - cw // 2
        col_top = int(ty + th * 0.34)                            # column rises ~2/3 up
        grad = _vgrad(cw, by - col_top, col_hi, col_lo)
        m = Image.new("L", (cw, by - col_top), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, cw - 1, (by - col_top) - 1], radius=cw // 2, fill=255)
        content.paste(grad, (cx0, col_top), m)

    if which in ("wx_feels", "wx_hi", "wx_lo"):
        if which == "wx_hi":
            _thermo((255, 196, 120), (255, 96, 72))              # warm red
        elif which == "wx_lo":
            _thermo((170, 220, 255), (70, 150, 255))             # cool blue
        else:
            _thermo((255, 214, 150), (255, 150, 60))             # amber
    elif which == "wx_humidity":
        dw = max(6, int(s * 1.05)); _drop(cx - dw / 2, cy - s * 0.95, dw, int(dw * 1.9),
                                          (150, 208, 255), (40, 120, 240))
    elif which == "wx_precip":
        for i, dx in enumerate((-0.62, 0.0, 0.62)):              # three falling drops
            dw = max(4, int(s * 0.5)); _drop(cx + s * dx - dw / 2, cy - s * 0.55 + abs(dx) * s * 0.2,
                                             dw, int(dw * 1.9), (150, 208, 255), (40, 120, 240))
    elif which == "wx_wind":
        col = (176, 206, 230)
        for i, (yy, wf) in enumerate(((-0.5, 1.15), (0.1, 1.4), (0.7, 0.95))):
            x0 = int(cx - s * wf); x1 = int(cx + s * (wf - 0.35)); yv = int(cy + s * yy)
            d.line([(x0, yv), (x1, yv)], fill=col, width=lw)
            d.ellipse([x0 - lw / 2, yv - lw / 2, x0 + lw / 2, yv + lw / 2], fill=col)
            d.arc([x1 - int(s * 0.5), yv - int(s * 0.5), x1 + int(s * 0.5), yv + int(s * 0.5)],
                  -90, 170, fill=col, width=lw)                  # curl at the end
    elif which == "wx_uv":
        inner = (255, 244, 190)
        outer = (255, 196, 60) if (cf or 0) < 0.55 else ((255, 150, 40) if (cf or 0) < 0.82 else (255, 96, 64))
        _sun(cx, cy, int(s * 0.66), inner, outer)
    elif which in ("wx_sunrise", "wx_sunset"):
        rise = which == "wx_sunrise"
        hy = int(cy + s * 0.62)                                  # horizon line
        inner = (255, 242, 186); outer = (255, 172, 64) if rise else (255, 118, 78)
        r = int(s * 0.58); sun_cy = hy - int(s * 0.34)
        for a in range(200, 341, 35):                           # upward rays only
            ca, sn = math.cos(math.radians(a)), math.sin(math.radians(a))
            x1, y1 = cx + ca * r * 1.3, sun_cy + sn * r * 1.3
            x2, y2 = cx + ca * r * 1.7, sun_cy + sn * r * 1.7
            d.line([(x1, y1), (x2, y2)], fill=outer, width=lw)
            d.ellipse([x2 - lw / 2, y2 - lw / 2, x2 + lw / 2, y2 + lw / 2], fill=outer)
        _disc(cx, sun_cy, r, inner, outer, glow=outer)
        d.line([(int(cx - s * 1.5), hy), (int(cx + s * 1.5), hy)], fill=(150, 200, 240), width=lw)
        ax = cx + s * 1.05; ay = hy - s * (0.5 if rise else -0.05)  # direction chevron beside the sun
        tip = ay - s * 0.32 if rise else ay + s * 0.32
        d.line([(ax - s * 0.26, ay), (ax, tip)], fill=(190, 220, 245), width=lw)
        d.line([(ax, tip), (ax + s * 0.26, ay)], fill=(190, 220, 245), width=lw)
    else:
        _disc(cx, cy, int(s * 0.7), (220, 230, 245), (150, 170, 200))


def _weather_bg(cond, night):
    """A dark, condition + day/night aware vertical gradient (top, bottom) for weather tiles —
    tinted but still dark so the black default holds for everything else."""
    c = (cond or "").lower()
    if night:
        if "clear" in c:                     return (16, 22, 52), (3, 4, 12)     # deep starry indigo
        if "rain" in c or "drizzle" in c:    return (12, 22, 40), (3, 5, 12)
        if "snow" in c:                      return (22, 30, 44), (5, 8, 14)
        if "storm" in c:                     return (24, 18, 40), (4, 4, 10)
        if "fog" in c:                       return (24, 26, 32), (6, 7, 10)
        if "cloud" in c or "overcast" in c:  return (22, 26, 38), (5, 6, 12)
        return (16, 20, 36), (3, 4, 10)
    if "clear" in c:                         return (48, 33, 12), (9, 7, 5)      # warm amber day
    if "cloud" in c:                         return (34, 41, 53), (8, 10, 14)    # partly: soft blue-grey
    if "overcast" in c:                      return (30, 33, 38), (8, 9, 11)
    if "rain" in c or "drizzle" in c:        return (16, 31, 51), (4, 7, 14)     # cool blue
    if "snow" in c:                          return (32, 43, 57), (9, 13, 19)    # icy
    if "storm" in c:                         return (29, 22, 47), (6, 5, 12)     # purple
    if "fog" in c:                           return (32, 34, 40), (10, 11, 13)
    return (22, 26, 34), (6, 7, 10)


def _coordish(s):
    p = (s or "").split(",")
    if len(p) != 2:
        return False
    try:
        float(p[0]); float(p[1]); return True
    except ValueError:
        return False


def timer_face(shown_s: int, total_s: int, running: bool, finished: bool, flash_on: bool,
               label: str = "", size: Optional[Tuple[int, int]] = None) -> Image.Image:
    """A countdown / stopwatch key face: big MM:SS (H:MM:SS over an hour), a caption on top, a
    remaining-fraction bar for countdowns, dim digits while paused, and a red flash while the
    just-finished alarm is showing. Rendered by the CONTROLLER from its per-key timer state."""
    w, h = size or KEY_SIZE
    if finished and flash_on:
        img = Image.new("RGB", (w, h), (186, 46, 42))            # alarm flash frame
        fg, dim, bar_bg = (255, 255, 255), (255, 216, 212), (140, 30, 28)
    else:
        img = Image.new("RGB", (w, h), (14, 18, 24))
        fg = (240, 245, 250) if (running or finished) else (150, 158, 168)   # dim when paused
        dim, bar_bg = (120, 130, 142), (44, 52, 62)
    d = ImageDraw.Draw(img)
    s = max(0, int(shown_s))
    txt = f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"
    d.text((w // 2, int(h * 0.46)), txt, font=_fit_font_h(d, txt, w * 0.90, h * 0.34),
           anchor="mm", fill=fg)
    cap = (label or ("TIMER" if total_s else "STOPWATCH")).upper()[:12]
    d.text((w // 2, int(h * 0.13)), cap, font=_font(max(8, int(h * 0.115)), bold=True),
           anchor="mm", fill=dim)
    if total_s:                                                  # countdown: remaining-fraction bar
        frac = max(0.0, min(1.0, s / float(total_s)))
        bw, by, bh = int(w * 0.78), int(h * 0.80), max(3, h // 22)
        x0 = (w - bw) // 2
        d.rounded_rectangle([x0, by, x0 + bw, by + bh], radius=bh // 2, fill=bar_bg)
        if frac > 0:
            col = (236, 78, 70) if finished else (53, 224, 138)
            d.rounded_rectangle([x0, by, x0 + int(bw * frac), by + bh], radius=bh // 2, fill=col)
    elif not running and s > 0:                                  # paused stopwatch
        d.text((w // 2, int(h * 0.82)), "PAUSED", font=_font(max(8, int(h * 0.10)), bold=True),
               anchor="mm", fill=dim)
    return img


def live_face(item: Dict[str, Any], text: str, caption: str, frac: Optional[float], kind: str,
              size: Optional[Tuple[int, int]] = None, show_label: bool = True,
              style: str = "gauge", history=None, scroll=None, artwork=None,
              weather=None) -> Image.Image:
    """A dynamic-data tile whose icon fits the source. `style` picks the metric design
    (gauge / ring / bar / dial / minimal). Rendered at 3x and downscaled for anti-aliasing.
    `scroll` (px) marquees an overflowing now-playing title; None = static (ellipsised) for previews.
    """
    if size is None:
        size = KEY_SIZE
    ss = 3
    w, h = size[0] * ss, size[1] * ss                         # supersample, downscale at the end
    bg = parse_color(item.get("color"), DEFAULT_BG)
    lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    fg = (24, 26, 30) if lum > 140 else parse_color(DEFAULT_FG)
    dim = (90, 95, 105) if lum > 140 else (150, 160, 172)
    track = (70, 76, 86) if lum <= 140 else (200, 204, 210)
    accent = _heat_color(frac) if frac is not None else (61, 139, 255)
    img = background_image(item, (w, h))
    if kind == "media" and artwork is not None and text and text != "--":
        # The LANCZOS cover-fit + scrim composite is the heaviest op here and is identical across
        # the ~20 marquee frames of one track — cache the scrimmed background (1 entry). Only the
        # title strip (drawn on `content` below) moves.
        key = (id(artwork), w, h)
        ce = _MEDIA_BG_CACHE
        if ce.get("key") == key:
            img = ce["bg"]
        else:
            from PIL import ImageOps
            cover = ImageOps.fit(artwork.convert("RGB"), (w, h), Image.LANCZOS)   # cover art fills the tile
            scrim = Image.new("RGBA", (w, h), (0, 0, 0, 165))                     # darken so text stays legible
            img = Image.alpha_composite(cover.convert("RGBA"), scrim).convert("RGB")
            _MEDIA_BG_CACHE.update(key=key, bg=img, art=artwork)                  # keep `art` ref -> no id() reuse
        fg = (245, 248, 250)                                                  # light text over the cover
        dim = (200, 206, 214)
    if kind in ("weather", "wxmetric", "wxforecast") and not item.get("color"):
        if kind == "wxforecast":                          # tint a forecast tile by ITS OWN day's weather
            parts = (caption or "").split("|")
            wcond = parts[2] if len(parts) > 2 else ""
            wnight = False
        else:
            wcond = (weather or {}).get("cond") or (caption or "").lower()
            wnight = bool((weather or {}).get("night"))
        img = _vgrad(w, h, *_weather_bg(wcond, wnight)).convert("RGB")
        fg = parse_color(DEFAULT_FG); dim = (170, 178, 192); track = (66, 72, 84)
    content = Image.new("RGBA", (w, h), (0, 0, 0, 0))    # the icon -> a separate layer we can resize
    d = ImageDraw.Draw(content)
    cx = w // 2
    lw = max(2, int(min(w, h) * 0.035))
    gs = int(min(w, h) * 0.11)
    glyph = _GAUGE_GLYPHS.get((caption or "").upper())

    def putval(yf, max_w, base):
        d.text((cx, int(h * yf)), text or "--", font=_fit_font_h(d, text, max_w, base), fill=fg, anchor="mm")

    if kind == "percent":
        cf = max(0.0, min(1.0, frac)) if frac is not None else 0.0
        if style == "card":
            _card_tile(content, d, w, h, text, caption, frac, accent, fg, dim, track)
        elif style == "minimal":
            if glyph:
                glyph(d, cx, int(h * 0.26), gs, dim, lw)
            putval(0.62, w * 0.88, h * 0.34)
        elif style == "bar":
            if glyph:
                glyph(d, cx, int(h * 0.24), gs, dim, lw)
            putval(0.54, w * 0.82, h * 0.30)
            bx0, bx1, by = int(w * 0.14), int(w * 0.86), int(h * 0.83)
            bh = max(3, int(h * 0.07))
            d.rounded_rectangle([bx0, by, bx1, by + bh], radius=bh // 2, fill=track)
            if frac is not None:
                d.rounded_rectangle([bx0, by, bx0 + int((bx1 - bx0) * cf), by + bh], radius=bh // 2, fill=accent)
        elif style == "ring":
            r = int(min(w, h) * 0.40)
            cyr = h // 2
            d.ellipse([cx - r, cyr - r, cx + r, cyr + r], outline=track, width=lw)
            if frac is not None and cf > 0:
                d.arc([cx - r, cyr - r, cx + r, cyr + r], 270, 270 + 360 * cf, fill=accent, width=lw)
            if glyph:
                glyph(d, cx, int(h * 0.32), int(gs * 0.8), dim, lw)
            putval(0.56, r * 1.2, h * 0.26)
        elif style == "dial":
            r = int(min(w, h) * 0.40)
            cyr = int(h * 0.64)
            d.arc([cx - r, cyr - r, cx + r, cyr + r], 180, 360, fill=track, width=lw)
            if frac is not None:
                ang = math.radians(180 + 180 * cf)
                d.line([(cx, cyr), (cx + math.cos(ang) * r * 0.92, cyr + math.sin(ang) * r * 0.92)],
                       fill=accent, width=lw)
            d.ellipse([cx - lw, cyr - lw, cx + lw, cyr + lw], fill=accent)
            if glyph:
                glyph(d, cx, int(h * 0.24), int(gs * 0.85), dim, lw)
            putval(0.84, w * 0.7, h * 0.22)
        elif style == "graph":                                # label + value on top, history graph below
            if caption:
                d.text((int(w * 0.5), int(h * 0.16)), caption, font=_font(int(h * 0.15), bold=True),
                       fill=dim, anchor="mm")
            putval(0.37, w * 0.9, h * 0.26)
            gx0, gx1 = int(w * 0.05), int(w * 0.95)
            gy0, gy1 = int(h * 0.52), int(h * 0.93)
            gw, gh = gx1 - gx0, gy1 - gy0
            gcol = (64, 212, 124)                             # graph green (task-manager style)
            fillc = tuple(int(gcol[i] * 0.42 + bg[i] * 0.58) for i in range(3))
            pts = list(history or [])
            if frac is not None:
                pts = pts + [cf]
            pts = pts[-48:]
            if pts:
                if len(pts) == 1:
                    yv = gy1 - pts[0] * gh
                    d.rectangle([gx0, yv, gx1, gy1], fill=fillc)
                    d.line([(gx0, yv), (gx1, yv)], fill=gcol, width=lw)
                else:
                    n = len(pts)
                    xy = [(gx0 + gw * i / (n - 1), gy1 - max(0.0, min(1.0, p)) * gh)
                          for i, p in enumerate(pts)]
                    d.polygon([(gx0, gy1)] + xy + [(gx1, gy1)], fill=fillc)
                    d.line(xy, fill=gcol, width=lw, joint="curve")
            d.line([(gx0, gy1), (gx1, gy1)], fill=track, width=max(1, lw // 2))
        else:                                                 # gauge (default): 270deg arc
            r = int(min(w, h) * 0.33)
            gy = int(h * 0.58)
            box = [cx - r, gy - r, cx + r, gy + r]
            d.arc(box, 135, 45, fill=track, width=lw)
            if frac is not None and cf > 0:
                d.arc(box, 135, 135 + 270 * cf, fill=accent, width=lw)
            if glyph:
                glyph(d, cx, int(h * 0.22), gs, dim, lw)
            putval(0.62, r * 1.55, h * 0.24)

    elif kind == "vol":                                       # a speaker + level (volume-specific)
        cf = max(0.0, min(1.0, frac)) if frac is not None else 0.0
        vcol = (74, 155, 255)                                 # steady volume blue (not heat-coloured)
        _glyph_speaker(d, int(w * 0.42), int(h * 0.34), int(min(w, h) * 0.17), frac, lw, vcol, track)
        putval(0.62, w * 0.9, h * 0.30)
        bx0, bx1, by = int(w * 0.14), int(w * 0.86), int(h * 0.84)
        bh = max(3, int(h * 0.07))
        d.rounded_rectangle([bx0, by, bx1, by + bh], radius=bh // 2, fill=track)
        if frac is not None:
            d.rounded_rectangle([bx0, by, bx0 + int((bx1 - bx0) * cf), by + bh], radius=bh // 2, fill=vcol)

    elif kind == "battery":
        if style == "minimal":
            putval(0.5, w * 0.9, h * 0.34)
        else:
            bw, bh = int(w * 0.62), int(h * 0.30)
            x0, y0 = cx - bw // 2, int(h * 0.20)
            d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=max(2, bh // 6), outline=fg, width=lw)
            nub = max(2, int(w * 0.04))
            d.rounded_rectangle([x0 + bw, y0 + bh * 0.3, x0 + bw + nub, y0 + bh * 0.7], radius=nub // 2, fill=fg)
            if frac is not None:
                pad = lw
                iw = int((bw - 2 * pad) * max(0.0, min(1.0, frac)))
                d.rounded_rectangle([x0 + pad, y0 + pad, x0 + pad + iw, y0 + bh - pad],
                                    radius=max(1, bh // 8), fill=_heat_color(1.0 - frac))
            if (caption or "").upper().startswith("CHARG"):
                d.line([(cx, y0 + lw), (cx - bw * 0.08, y0 + bh // 2), (cx + bw * 0.05, y0 + bh // 2),
                        (cx - bw * 0.03, y0 + bh - lw)], fill=(20, 22, 26), width=lw)
            putval(0.70, w * 0.9, h * 0.26)

    elif kind == "clock":
        if style == "minimal":
            putval(0.5, w * 0.92, h * 0.30)
        else:
            r = int(min(w, h) * 0.42)
            cyc = h // 2
            d.ellipse([cx - r, cyc - r, cx + r, cyc + r], outline=fg, width=lw)
            for a in range(0, 360, 30):
                x1 = cx + math.cos(math.radians(a)) * r
                y1 = cyc + math.sin(math.radians(a)) * r
                x2 = cx + math.cos(math.radians(a)) * r * 0.86
                y2 = cyc + math.sin(math.radians(a)) * r * 0.86
                d.line([(x1, y1), (x2, y2)], fill=dim, width=max(1, lw // 2))
            parts = (text or "0:0").split(":")
            try:
                hh, mm = int(parts[0]), int(parts[1])
                sec = int(parts[2]) if len(parts) > 2 else None
            except ValueError:
                hh, mm, sec = 0, 0, None

            def hand(turn, length, width, col):
                ang = math.radians(-90 + 360 * turn)
                d.line([(cx, cyc), (cx + math.cos(ang) * r * length, cyc + math.sin(ang) * r * length)],
                       fill=col, width=width)
            hand(((hh % 12) + mm / 60.0) / 12.0, 0.5, lw, fg)
            hand(mm / 60.0, 0.8, max(2, int(lw * 0.7)), fg)
            if sec is not None:
                hand(sec / 60.0, 0.9, max(1, lw // 2), (236, 78, 70))
            d.ellipse([cx - lw, cyc - lw, cx + lw, cyc + lw], fill=fg)

    elif kind == "date":
        cw_, ch_ = int(w * 0.6), int(h * 0.62)            # natural size; the whole icon scales below
        x0, y0 = cx - cw_ // 2, (h - ch_) // 2                            # centred so it can grow/shrink
        hdr = max(2 * lw, int(ch_ * 0.28))
        rad = max(2, int(min(cw_, ch_) * 0.09))
        d.rounded_rectangle([x0, y0, x0 + cw_, y0 + ch_], radius=rad, outline=fg, width=lw)
        d.rounded_rectangle([x0, y0, x0 + cw_, y0 + hdr], radius=rad, fill=(236, 78, 70))
        d.rectangle([x0, y0 + hdr - lw, x0 + cw_, y0 + hdr], fill=(236, 78, 70))
        for rx in (0.3, 0.7):
            d.line([(x0 + cw_ * rx, y0 - lw), (x0 + cw_ * rx, y0 + lw)], fill=fg, width=lw)
        mon = (caption or "").split()[-1] if caption else ""
        if mon:
            d.text((cx, y0 + hdr // 2), mon[:3], font=_font(max(8, int(ch_ * 0.20)), bold=True),
                   fill=(255, 255, 255), anchor="mm")
        d.text((cx, y0 + hdr + (ch_ - hdr) // 2), text,
               font=_fit_font_h(d, text, cw_ * 0.9, ch_ * 0.5), fill=fg, anchor="mm")

    elif kind == "net":
        ay = int(h * 0.30)
        u = min(w, h) * 0.12
        d.polygon([(cx, ay + u), (cx - u, ay - u * 0.4), (cx - u * 0.34, ay - u * 0.4),
                   (cx - u * 0.34, ay - u * 1.1), (cx + u * 0.34, ay - u * 1.1),
                   (cx + u * 0.34, ay - u * 0.4), (cx + u, ay - u * 0.4)], fill=accent)
        putval(0.62, w * 0.9, h * 0.28)
        d.text((cx, int(h * 0.84)), caption or "KB/S", font=_font(int(h * 0.11), bold=True),
               fill=dim, anchor="mm")

    elif kind == "state":
        on = bool(frac)
        sgly = _STATE_GLYPHS.get((caption or "").upper())
        if sgly:
            sgly(d, cx, int(h * 0.40), int(min(w, h) * 0.19), on, lw)
        else:
            d.ellipse([cx - gs, int(h * 0.40) - gs, cx + gs, int(h * 0.40) + gs],
                      outline=(_ON if on else _OFFCOL), width=lw)
        d.text((cx, int(h * 0.82)), text or ("ON" if on else "OFF"),
               font=_fit_font_h(d, text, w * 0.86, h * 0.22),
               fill=(fg if on or (caption or "").upper() != "MIC" else _ALERT), anchor="mm")

    elif kind == "weather":
        cond = (caption or "").lower()
        wp = weather or {}
        wnight = bool(wp.get("night"))
        rich = wp.get("temp") is not None and (wp.get("hi") is not None or wp.get("lo") is not None)
        if rich:                                       # mini widget: place · icon + big temp · feels / hi-lo
            loc = wp.get("label") or ""
            head = loc.upper() if (loc and not _coordish(loc)) else (caption or "WEATHER").upper()
            d.text((cx, int(h * 0.115)), head,
                   font=_fit_font_h(d, head, w * 0.92, int(h * 0.125)), fill=dim, anchor="mm")
            _weather_glyph(content, int(w * 0.29), int(h * 0.45), int(min(w, h) * 0.125), cond,
                           dim, max(2, int(min(w, h) * 0.04)), night=wnight)
            tf = _fit_font_h(d, text or "--", w * 0.46, int(h * 0.30))
            d.text((int(w * 0.64), int(h * 0.44)), text or "--", font=tf, fill=fg, anchor="mm")
            hi, lo, feels = wp.get("hi"), wp.get("lo"), wp.get("feels")
            bits = []
            if feels is not None:
                bits.append(f"~{feels}°")
            if hi is not None and lo is not None:
                bits.append(f"{hi}° / {lo}°")
            strip = "   ".join(bits)
            if strip:
                d.text((cx, int(h * 0.85)), strip,
                       font=_fit_font_h(d, strip, w * 0.94, int(h * 0.135)), fill=dim, anchor="mm")
        else:
            _weather_glyph(content, cx, int(h * 0.36), int(min(w, h) * 0.205), cond,
                           dim, max(2, int(min(w, h) * 0.05)), night=wnight)
            putval(0.74, w * 0.94, h * 0.32)          # big temperature below the condition icon

    elif kind == "wxmetric":                          # a single weather metric: icon + value + label
        src = (item.get("live") or {}).get("source", "")
        cf = max(0.0, min(1.0, frac)) if frac is not None else None
        _wx_metric_glyph(content, cx, int(h * 0.27), max(8, int(min(w, h) * 0.15)), src, cf)
        putval(0.58, w * 0.86, h * 0.30)
        if caption:
            d.text((cx, int(h * 0.80)), caption,
                   font=_fit_font_h(d, caption, w * 0.92, int(h * 0.135)), fill=dim, anchor="mm")
        if cf is not None:                            # thin progress bar (humidity / UV / rain / wind)
            bx0, bx1, by = int(w * 0.16), int(w * 0.84), int(h * 0.90)
            bh = max(3, int(h * 0.055))
            wxc = (74, 155, 255)
            d.rounded_rectangle([bx0, by, bx1, by + bh], radius=bh // 2, fill=track)
            d.rounded_rectangle([bx0, by, bx0 + int((bx1 - bx0) * cf), by + bh], radius=bh // 2, fill=wxc)

    elif kind == "wxforecast":                        # a forecast day: weekday · icon · high / low
        parts = (caption or "").split("|")
        dow = parts[0] if parts else ""
        lo = parts[1] if len(parts) > 1 else ""
        cond = parts[2] if len(parts) > 2 else ""
        d.text((cx, int(h * 0.15)), dow or "--", font=_font(int(h * 0.17), bold=True),
               fill=(150, 196, 255), anchor="mm")
        _weather_glyph(content, cx, int(h * 0.46), int(min(w, h) * 0.135), cond,
                       dim, max(2, int(min(w, h) * 0.035)))
        hi = text or "--"
        hf = _font(int(h * 0.20), bold=True)
        lof = _font(int(h * 0.165))
        gap = int(w * 0.06)
        hw = d.textlength(hi, font=hf); lw2 = d.textlength(lo, font=lof)
        total = hw + gap + lw2
        x = cx - total / 2
        d.text((x, int(h * 0.86)), hi, font=hf, fill=fg, anchor="lm")
        d.text((x + hw + gap, int(h * 0.86)), lo, font=lof, fill=dim, anchor="lm")

    elif kind == "media":
        # Now playing: a play/pause indicator, the track title (truncated), then the artist.
        playing = bool(frac)
        live_c = (53, 224, 138)
        gy, gr = int(h * 0.21), int(min(w, h) * 0.12)
        if text and text != "--":
            if playing:                                   # ▶ triangle
                d.polygon([(cx - gr * 0.5, gy - gr), (cx - gr * 0.5, gy + gr), (cx + gr, gy)],
                          fill=live_c)
            else:                                         # ⏸ two bars
                bw = max(2, int(gr * 0.45))
                d.rectangle([cx - int(gr * 0.75), gy - gr, cx - int(gr * 0.75) + bw, gy + gr], fill=dim)
                d.rectangle([cx + int(gr * 0.30), gy - gr, cx + int(gr * 0.30) + bw, gy + gr], fill=dim)
            tfont = _font(int(h * _MEDIA_TITLE_FRAC), bold=True)
            avail = int(w * _MEDIA_AVAIL_FRAC)
            ty = int(h * 0.52)
            tw = d.textlength(text, font=tfont)
            if tw <= avail or scroll is None:
                # fits, or a static preview -> centre (ellipsise if a long title can't scroll here)
                shown = text if tw <= avail else _ellipsize(d, text, tfont, avail)
                d.text((cx, ty), shown, font=tfont, fill=fg, anchor="mm")
            else:
                # marquee: scroll the title right->left, looping seamlessly with a gap
                gap = int(w * 0.30)
                period = int(tw) + gap
                off = int(scroll) % period
                strip_h = int(h * 0.26)
                strip = Image.new("RGBA", (avail, strip_h), (0, 0, 0, 0))
                sd = ImageDraw.Draw(strip)
                fg4 = (fg[0], fg[1], fg[2], 255)
                sy = strip_h // 2
                sd.text((-off, sy), text, font=tfont, fill=fg4, anchor="lm")
                sd.text((period - off, sy), text, font=tfont, fill=fg4, anchor="lm")
                content.paste(strip, ((w - avail) // 2, ty - strip_h // 2), strip)
            if caption:
                artist = _ellipsize(d, caption, _font(int(h * 0.115)), w * 0.92)
                d.text((cx, int(h * 0.78)), artist, font=_font(int(h * 0.115)), fill=dim, anchor="mm")
        else:                                             # a drawn note (the ♪ glyph isn't in the font)
            nr = int(min(w, h) * 0.08)
            nx, ny = cx - int(w * 0.03), int(h * 0.32)
            d.line([(nx + nr, ny), (nx + nr, ny - int(h * 0.17))], fill=dim, width=lw)
            d.ellipse([nx - nr, ny - int(nr * 0.8), nx + nr, ny + int(nr * 0.8)], fill=dim)
            d.text((cx, int(h * 0.66)), "Nothing\nplaying", font=_font(int(h * 0.13), bold=True),
                   fill=dim, anchor="mm", align="center")

    else:
        if caption:
            d.text((cx, int(h * 0.22)), caption, font=_font(int(h * 0.13), bold=True), fill=dim, anchor="mm")
        putval(0.58, w * 0.9, h * 0.40)

    # Resize the whole live icon (gauge / number / glyph) around its centre — adjustable per key.
    sc = max(0.4, min(1.6, float(item.get("icon_scale", 1.0))))
    if abs(sc - 1.0) > 0.01:
        nw, nh = max(1, int(w * sc)), max(1, int(h * sc))
        scaled = content.resize((nw, nh), Image.LANCZOS)
        content = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        content.paste(scaled, ((w - nw) // 2, (h - nh) // 2), scaled)
    img = Image.alpha_composite(img.convert("RGBA"), content).convert("RGB")
    return img.resize(size, Image.LANCZOS)


def _parse_rgb(c, default=(53, 224, 138)):
    if isinstance(c, (tuple, list)) and len(c) >= 3:
        return tuple(int(x) for x in c[:3])
    if isinstance(c, str) and c:
        h = c.lstrip("#")
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except ValueError:
            pass
    return default


def value_hud_tiles(value, name, *, muted: bool = False, accent=None, unit: str = "%",
                    relative: int = 0, vmax: int = 100, gap: int = 22):
    """A transient knob-feedback overlay spanning the whole 2×3 key grid — used by EVERY encoder
    that changes a value (volume, brightness, RGB, …). Layout is bezel-aware: the parameter NAME
    auto-fits across the TOP row (shrinks so it never clips into the bezels), and the BOTTOM row
    shows the big value + a full-width fill bar — so all six little screens are used.

    `value`  : 0..100 for an absolute reading (draws the % + a filled bar), or None when only the
               direction is known (`relative` = +1 / -1 → draws a ▲ / ▼ instead of a bar).
    `accent` : an explicit bar/value colour ("#rrggbb" or rgb tuple); defaults to a heat ramp.
    """
    kw, kh = KEY_SIZE
    W, H = 3 * kw + 2 * gap, 2 * kh + gap
    S = 2                                                              # supersample
    w2, h2 = W * S, H * S
    vmax = max(1, vmax)
    has_val = value is not None and not relative
    frac = (max(0, min(vmax, value)) / vmax) if has_val else 0.0
    if muted:
        col = (236, 78, 70)
    elif accent is not None:
        col = _parse_rgb(accent)
    elif has_val:
        col = _heat_color(frac)
    else:
        col = (53, 224, 138)

    # Faint accent wash so ALL six little screens light up as one overlay (not a black void with a
    # bar). Each key is its own screen separated by bezels, so every text element is centred INSIDE
    # a single key (never spanning a gap) and shrinks / ellipsises to fit — it can't clip.
    base = tuple(int(9 + (c - 9) * 0.16) for c in col) if not muted else (28, 14, 14)
    img = Image.new("RGB", (w2, h2), base)
    d = ImageDraw.Draw(img)
    key_w = kw * S                                                    # one key's width (the safe text box)
    cx = w2 // 2

    def fit(text, target_w, start_frac, floor_frac):
        fs = int(h2 * start_frac)
        floor = int(h2 * floor_frac)
        while fs > floor and d.textlength(text, font=_font(fs, bold=True)) > target_w:
            fs -= 2
        f = _font(fs, bold=True)
        if d.textlength(text, font=f) > target_w and len(text) > 1:    # still too wide -> ellipsise
            while len(text) > 1 and d.textlength(text + "…", font=f) > target_w:
                text = text[:-1]
            text += "…"
        return f, text

    # --- NAME: top-centre key (auto-fit / ellipsise to one key width) ---
    nm = (name or "Value").rsplit(".exe", 1)[0].strip()[:28] or "Value"
    nf, nm = fit(nm, key_w * 0.94, 0.115, 0.058)
    d.text((cx, int(h2 * 0.225)), nm, font=nf, fill=(214, 224, 234), anchor="mm")

    # --- VALUE / direction: bottom-centre key, above the bar ---
    if muted:
        vtext = "MUTED"
    elif has_val:
        vtext = f"{max(0, min(vmax, int(value)))}{unit}"
    else:
        vtext = "▲" if relative > 0 else "▼"
    vf, vtext = fit(vtext, key_w * 0.94, 0.20, 0.10)
    # The big value is drawn HIGH-CONTRAST white so it stays legible on ANY accent — drawing the
    # number in the accent colour (e.g. a blue hue) on its own background-wash read as muddy. The
    # accent colour is still carried by the bar below (and the ▲/▼ direction glyph keeps it).
    vcol = (236, 78, 70) if muted else ((240, 245, 250) if has_val else col)
    d.text((cx, int(h2 * 0.70)), vtext, font=vf, fill=vcol, anchor="mm")

    # --- BAR: spans the full bottom row (bars tolerate bezels — they read as one) ---
    bx0, bx1 = int(w2 * 0.06), int(w2 * 0.94)
    by, bh = int(h2 * 0.875), int(h2 * 0.075)
    d.rounded_rectangle([bx0, by, bx1, by + bh], radius=bh // 2, fill=(40, 46, 56))
    if has_val and not muted and value > 0:
        fx = bx0 + int((bx1 - bx0) * frac)
        d.rounded_rectangle([bx0, by, max(bx0 + bh, fx), by + bh], radius=bh // 2, fill=col)
    elif not has_val and not muted:                                   # relative: a centred nub
        seg = int((bx1 - bx0) * 0.14)
        d.rounded_rectangle([cx - seg, by, cx + seg, by + bh], radius=bh // 2, fill=col)

    img = img.resize((W, H), Image.LANCZOS)
    return [img.crop((c * (kw + gap), r * (kh + gap), c * (kw + gap) + kw, r * (kh + gap) + kh))
            for r in range(2) for c in range(3)]


def volume_hud_tiles(vol: int, muted: bool, name: str, gap: int = 22):
    """Back-compat wrapper: a volume HUD is just a value HUD (absolute %)."""
    return value_hud_tiles(None if (vol is not None and vol < 0) else vol, name,
                           muted=muted, gap=gap)


def _ambient_tile(text, color, frac, drift=(0, 0)):
    """One ambient key: centred auto-fit text on a near-black face (+ a few px burn-in drift)."""
    kw, kh = KEY_SIZE
    S = 2
    w2, h2 = kw * S, kh * S
    img = Image.new("RGB", (w2, h2), (6, 9, 12))
    d = ImageDraw.Draw(img)
    fs = int(h2 * frac)
    while fs > int(h2 * 0.16) and d.textlength(text, font=_font(fs, bold=True)) > w2 * 0.86:
        fs -= 2
    dx, dy = int(drift[0]) * S, int(drift[1]) * S
    d.text((w2 // 2 + dx, h2 // 2 + dy), text, font=_font(fs, bold=True), fill=color, anchor="mm")
    return img.resize((kw, kh), Image.LANCZOS)


# ---- ambient / idle-screen designs -----------------------------------------------------------
AMBIENT_STYLE_ORDER = ["classic", "rainbow", "aurora", "ocean", "ember", "matrix", "snow",
                       "synthwave", "starfield", "plasma", "lava", "flip", "nixie",
                       "night", "solar", "minimal"]
AMBIENT_STYLE_LABELS = {
    "classic": "Classic — mint grid",
    "rainbow": "Rainbow wave  (animated)",
    "aurora": "Aurora — northern lights  (animated)",
    "ocean": "Ocean waves  (animated)",
    "ember": "Ember glow  (animated)",
    "matrix": "Matrix rain  (animated)",
    "snow": "Snowfall  (animated)",
    "synthwave": "Synthwave sunset  (animated)",
    "starfield": "Starfield — warp  (animated)",
    "plasma": "Plasma  (animated)",
    "lava": "Lava lamp  (animated)",
    "flip": "Flip clock — split-flap",
    "nixie": "Nixie tubes — retro",
    "night": "Night sky",
    "solar": "Solar sky — tracks the real sun",
    "minimal": "Minimal — barely there",
}
AMBIENT_ANIMATED = {"rainbow", "aurora", "ocean", "ember", "matrix", "snow", "synthwave",
                    "starfield", "plasma", "lava"}   # re-rendered continuously (~15 fps)
_AMBIENT_GAP = 22                       # bezel approximation for canvas-spanning designs
_AMBIENT_PREVIEW_VER = 9                # bump when any design changes -> preview cache refreshes


def _ambient_canvas(scale: int = 1):
    kw, kh = KEY_SIZE
    g = _AMBIENT_GAP
    return (3 * kw + 2 * g) * scale, (2 * kh + g) * scale


def _slice_ambient(canvas: Image.Image, scale: int = 1):
    """Slice a full-canvas design across the 2×3 grid (same gap math as panel_frames)."""
    kw, kh = KEY_SIZE
    g = _AMBIENT_GAP
    return [canvas.crop(((c * (kw + g)) * scale, (r * (kh + g)) * scale,
                         (c * (kw + g) + kw) * scale, (r * (kh + g) + kh) * scale))
            for r in range(2) for c in range(3)]


@lru_cache(maxsize=2)
def _rainbow_base(w: int, h: int) -> Image.Image:
    """A 2×-wide seamless rainbow field with a baked sine wave — the animation just scrolls a
    crop window across it, so per-frame cost is ~zero. Built at quarter res and upscaled
    (smooth hue fields lose nothing)."""
    bw, bh = max(8, (w * 2) // 4), max(8, h // 4)
    px = []
    for y in range(bh):
        # two stacked sines make the bands undulate instead of staying straight
        wob = math.sin(y / bh * 2 * math.pi) * 0.10 + math.sin(y / bh * 6.3) * 0.035
        for x in range(bw):
            hue = (x / (bw / 2) + wob) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 0.80, 0.66)
            px.append((int(r * 255), int(g * 255), int(b * 255)))
    img = Image.new("RGB", (bw, bh))
    img.putdata(px)
    return img.resize((w * 2, h), Image.BILINEAR)


_OCEAN_LAYERS = [  # (surface-y frac, amp frac, waves per period, phase, colour, strength)
    (0.38, 0.050, 2, 0.0, (22, 95, 135), 0.55),
    (0.56, 0.060, 3, 1.7, (18, 125, 155), 0.60),
    (0.74, 0.050, 4, 3.9, (30, 165, 185), 0.65),
]


@lru_cache(maxsize=2)
def _aurora_field(w: int, h: int) -> Image.Image:
    """Northern lights: soft green/teal/violet ribbons snaking over an arctic night sky.
    Same contract as _rainbow_base: returns a seamless 2×-wide field. Built at HALF res
    (quarter left the ribbon cores slightly blocky)."""
    bw, bh = max(8, (w * 2) // 2), max(8, h // 2)
    period = bw / 2.0
    img = _vgrad(bw, bh, (4, 8, 20), (1, 2, 8))
    p = img.load()
    ribbons = [  # (centre-y frac, sway amp frac, waves per period, phase, colour)
        (0.32, 0.11, 2, 0.0, (46, 235, 150)),
        (0.50, 0.09, 3, 2.1, (80, 190, 235)),
        (0.66, 0.07, 1, 4.4, (170, 110, 235)),
    ]
    sig = bh * 0.06
    for x in range(bw):
        t = (x / period) * 2 * math.pi
        for cyf, ampf, k, ph, col in ribbons:
            cy = (cyf + ampf * math.sin(k * t + ph)) * bh
            y0, y1 = max(0, int(cy - 3 * sig)), min(bh - 1, int(cy + 3 * sig))
            for y in range(y0, y1 + 1):
                f = math.exp(-((y - cy) ** 2) / (2 * sig * sig)) * 0.80
                r, g, b = p[x, y]
                p[x, y] = (int(r + (col[0] - r) * f), int(g + (col[1] - g) * f),
                           int(b + (col[2] - b) * f))
    out = img.resize((w * 2, h), Image.BILINEAR)
    import random
    rnd = random.Random(7)                            # deterministic starscape
    d = ImageDraw.Draw(out)
    for _ in range(120):
        x, y = rnd.randrange(w * 2), rnd.randrange(h)
        b = rnd.randint(70, 170)
        d.point((x, y), fill=(b, b, min(255, b + 20)))
    return out


@lru_cache(maxsize=2)
def _ocean_field(w: int, h: int) -> Image.Image:
    """Layered teal wave crests drifting across a deep-blue sea (seamless 2×-wide field).
    Depth fills build at half res; the foam crest is drawn ANALYTICALLY at full res so it
    stays a crisp line instead of upscaled quarter-res steps."""
    bw, bh = max(8, (w * 2) // 2), max(8, h // 2)
    period = bw / 2.0
    img = _vgrad(bw, bh, (8, 18, 38), (2, 5, 12))
    p = img.load()
    for x in range(bw):
        t = (x / period) * 2 * math.pi
        for cyf, ampf, k, ph, col, s in _OCEAN_LAYERS:
            ys = (cyf + ampf * math.sin(k * t + ph)) * bh
            for y in range(max(0, int(ys)), bh):
                f = max(0.16, s - (y - ys) / bh * 1.15)
                r, g, b = p[x, y]
                p[x, y] = (int(r + (col[0] - r) * f), int(g + (col[1] - g) * f),
                           int(b + (col[2] - b) * f))
    out = img.resize((w * 2, h), Image.BILINEAR)
    d = ImageDraw.Draw(out)
    W, H = out.size
    per = W / 2.0
    for cyf, ampf, k, ph, col, _s in _OCEAN_LAYERS:   # crisp foam line along each crest
        foam = (min(255, col[0] + 95), min(255, col[1] + 105), min(255, col[2] + 105))
        pts = [(x, (cyf + ampf * math.sin(k * (x / per) * 2 * math.pi + ph)) * H)
               for x in range(0, W, 3)]
        d.line(pts, fill=foam, width=2, joint="curve")
    return out


# The matrix charset: halfwidth katakana (the film look) + digits. Yu Gothic ships with
# Windows 10/11; fall back to Segoe + ASCII if it's missing.
_MATRIX_KANA = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛ0123456789"
_MATRIX_ASCII = "0123456789ACDEFHKLMNPRTXZ<>+*"
_MATRIX_LEVELS = [  # trail colours, dimmest -> the white-green head
    (8, 66, 26), (12, 100, 38), (16, 140, 52), (24, 180, 70),
    (40, 220, 90), (120, 255, 140), (190, 255, 200),
]


@lru_cache(maxsize=2)
def _matrix_atlas(fs: int):
    """Pre-rendered glyph tiles: every charset character at every trail brightness —
    per-frame drawing is then just fast alpha pastes, no text rasterisation."""
    font = None
    chars = _MATRIX_ASCII
    for path in ("C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/YuGothM.ttc",
                 "C:/Windows/Fonts/msgothic.ttc"):
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, fs)
                chars = _MATRIX_KANA
                break
            except OSError:
                font = None
    if font is None:
        font = _font(fs, bold=True)
    probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    cw = chh = 1
    for ch in chars:
        b = probe.textbbox((0, 0), ch, font=font)
        cw = max(cw, b[2] - b[0])
        chh = max(chh, b[3] - b[1])
    cw += 2
    chh += 3
    atlas = []
    for col in _MATRIX_LEVELS:
        row = []
        for ch in chars:
            g = Image.new("RGBA", (cw, chh), (0, 0, 0, 0))
            ImageDraw.Draw(g).text((cw // 2, chh // 2), ch, font=font, anchor="mm", fill=col)
            row.append(g)
        atlas.append(row)
    return atlas, cw, chh, len(chars)


def _mx(a: int, b: int = 0, c: int = 0) -> int:
    """A tiny deterministic hash — the rain is pure f(phase), no hidden state, so it
    resumes/looks identical for a given moment (and python's salted hash() can't be used)."""
    return ((a * 73856093) ^ (b * 19349663) ^ (c * 83492791)) & 0x7FFFFFFF


def _ambient_matrix(hh, mm, weekday, day, month, drift, phase):
    """Digital rain, SIMULATED per frame: every column has its own speed, trail length and
    gap; cells mutate their glyph over time and the bright head flickers fast — not a
    scrolling picture."""
    w, h = _ambient_canvas()
    S = 2
    kw, kh = KEY_SIZE
    W, H = w * S, h * S
    atlas, cw, chh, n_chars = _matrix_atlas(max(10, int(H * 0.055)))
    levels = len(atlas)
    canvas = Image.new("RGB", (W, H), (0, 4, 1))
    n_cols = max(1, W // cw)
    n_rows = H // chh + 2
    for k in range(n_cols):
        kk = _mx(k + 1)
        speed = 2.1 + (kk % 1000) / 1000.0 * 3.1      # rows/sec, per-column — film-slow (~2–5)
        trail = 9 + (kk >> 5) % 9                     # cells of fading tail
        gap = 5 + (kk >> 10) % 13                     # dark rows before the column restarts
        span = n_rows + trail + gap
        head = (float(kk % 977) + phase * speed) % span
        x = k * cw
        for dist in range(trail):
            ri = int(head) - dist
            if ri < 0 or ri >= n_rows:
                continue
            if dist == 0:                             # the head: brightest, gently reshuffles
                lvl = levels - 1
                ci = _mx(kk, ri, int(phase * 3.5)) % n_chars
            else:
                lvl = max(0, levels - 2 - dist * (levels - 1) // trail)
                cellseed = _mx(kk, ri)
                if (cellseed >> 7) % 13 == 0:         # occasional glitter in the tail
                    lvl = min(levels - 2, lvl + 2)
                # each cell mutates its glyph at its own slow rate (~0.4–1.4 Hz)
                ci = (cellseed + int(phase * (0.4 + cellseed % 3 * 0.5))) % n_chars
            g = atlas[lvl][ci]
            canvas.paste(g, (x, ri * chh), g)
    tiles = _slice_ambient(canvas, S)
    _ambient_grid_text(tiles, hh, mm, weekday, day, month, drift, S,
                       fill=(210, 255, 215), sub_fill=(150, 230, 165), halo=True)
    return [t.resize((kw, kh), Image.LANCZOS) for t in tiles]


@lru_cache(maxsize=2)
def _snow_field(w: int, h: int) -> Image.Image:
    """Quiet snowfall on a cold night — flakes at three depths, seamless vertically."""
    import random
    rnd = random.Random(4)
    img = _vgrad(w, 2 * h, (7, 10, 20), (13, 17, 27))
    d = ImageDraw.Draw(img)
    for _ in range(150):
        x, y = rnd.randrange(w), rnd.uniform(0, h)
        depth = rnd.random()
        r = 1 if depth < 0.55 else (2 if depth < 0.85 else 3)
        b = rnd.randint(90, 150) if depth < 0.55 else rnd.randint(160, 235)
        for yy in (y, y + h):                         # the seamless period copy
            d.ellipse([x - r, yy - r, x + r, yy + r], fill=(b, b, min(255, b + 8)))
    return img


@lru_cache(maxsize=2)
def _ember_field(w: int, h: int) -> Image.Image:
    """Warm fire-light: soft orange/red glows drifting in the dark (seamless 2×-wide field)."""
    bw, bh = max(8, (w * 2) // 4), max(8, h // 4)
    period = int(bw / 2)
    img = _vgrad(bw, bh, (12, 5, 3), (26, 9, 3))
    p = img.load()
    import random
    rnd = random.Random(5)
    palette = [(255, 120, 20), (255, 60, 12), (255, 170, 40), (235, 90, 30)]
    blobs = []
    for _ in range(8):
        blobs.append((rnd.uniform(0, period), rnd.uniform(0.18, 0.85) * bh,
                      rnd.uniform(0.10, 0.20) * bh, rnd.choice(palette),
                      rnd.uniform(0.45, 0.75)))
    for bx, by, br, col, strength in blobs:
        for cx in (bx, bx + period):                  # a copy one period over keeps it seamless
            x0, x1 = int(cx - 3 * br), int(cx + 3 * br)
            y0, y1 = max(0, int(by - 3 * br)), min(bh - 1, int(by + 3 * br))
            for x in range(max(0, x0), min(bw - 1, x1) + 1):
                for y in range(y0, y1 + 1):
                    d2 = (x - cx) ** 2 + (y - by) ** 2
                    f = math.exp(-d2 / (2 * br * br)) * strength
                    if f > 0.02:
                        r, g, b = p[x, y]
                        p[x, y] = (min(255, int(r + col[0] * f)),
                                   min(255, int(g + col[1] * f)),
                                   min(255, int(b + col[2] * f)))
    return img.resize((w * 2, h), Image.BILINEAR)


# Animated field styles: builder, sweep seconds (negative = reverse/falling), time colour,
# date colour, scroll axis ("x" fields are 2×-wide; "y" fields are 2×-tall).
_FIELD_STYLES = {
    "rainbow": (_rainbow_base, 18.0, (255, 255, 255), (240, 243, 246), "x"),
    "aurora": (_aurora_field, 34.0, (235, 245, 250), (172, 192, 208), "x"),
    "ocean": (_ocean_field, 26.0, (228, 242, 248), (162, 188, 202), "x"),
    "ember": (_ember_field, 48.0, (255, 238, 218), (222, 182, 156), "x"),
    "snow": (_snow_field, -21.0, (232, 238, 246), (168, 180, 198), "y"),
}


@lru_cache(maxsize=64)
def _ambient_text_overlay(txt, frac, col, w2, h2, dx, dy, S, halo):
    """Cached transparent RGBA overlay for ONE ambient clock key (auto-fit text + soft dark halo).
    A pure function of its args, so animated ambient frames reuse it — the strings change only
    ~once/minute — instead of re-rasterising text + GaussianBlur every ~15fps frame."""
    _probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    fs = int(h2 * frac)
    while fs > int(h2 * 0.16) and _probe.textlength(txt, font=_font(fs)) > w2 * 0.86:
        fs -= 2
    cx, cy = w2 // 2 + dx * S, h2 // 2 + dy * S
    ov = Image.new("RGBA", (w2, h2), (0, 0, 0, 0))
    if halo:                                          # soft dark glow -> readable on any hue
        hl = Image.new("L", (w2, h2), 0)
        ImageDraw.Draw(hl).text((cx, cy), txt, font=_font(fs), anchor="mm", fill=175)
        hl = hl.filter(ImageFilter.GaussianBlur(max(3, h2 // 16)))
        glow = Image.new("RGBA", (w2, h2), (8, 10, 14, 255))
        glow.putalpha(hl)
        ov = Image.alpha_composite(ov, glow)
    ImageDraw.Draw(ov).text((cx, cy), txt, font=_font(fs), anchor="mm", fill=(col[0], col[1], col[2], 255))
    return ov


def _ambient_grid_text(tiles, hh, mm, weekday, day, month, drift, S, *,
                       fill=(255, 255, 255), sub_fill=(235, 240, 245), halo=True):
    """Classic per-key clock text over background tiles: 21 | : | 47 / WED | 02 | JUL — every
    key self-contained and sized to FILL its key. (The device's real bezels are wider than the
    canvas gap approximation, so grid-spanning digits got visibly chopped on the hardware.)"""
    spec = [(hh, 0.66, fill), (":", 0.66, fill), (mm, 0.66, fill),
            (weekday, 0.42, sub_fill), (day, 0.42, fill), (month, 0.42, sub_fill)]
    dx, dy = int(drift[0]), int(drift[1])
    for tile, (txt, frac, col) in zip(tiles, spec):
        w2, h2 = tile.size
        ov = _ambient_text_overlay(txt, frac, tuple(col), w2, h2, dx, dy, S, halo)
        tile.paste(ov, (0, 0), ov)                    # composite the cached text+halo onto the tile


def _ambient_halo_text(tile, txt, frac, col, dx, dy, *, minfrac=0.14):
    """Auto-fit centred text on one ambient key with a soft dark halo (readable on any bg)."""
    if not txt:
        return
    w2, h2 = tile.size
    d = ImageDraw.Draw(tile)
    fs = int(h2 * frac)
    while fs > int(h2 * minfrac) and d.textlength(txt, font=_font(fs)) > w2 * 0.88:
        fs -= 2
    cx, cy = w2 // 2 + dx, h2 // 2 + dy
    hl = Image.new("L", (w2, h2), 0)
    ImageDraw.Draw(hl).text((cx, cy), txt, font=_font(fs), anchor="mm", fill=160)
    hl = hl.filter(ImageFilter.GaussianBlur(max(3, h2 // 18)))
    tile.paste((6, 8, 12), None, hl)
    d.text((cx, cy), txt, font=_font(fs), anchor="mm", fill=col)


def ambient_weather_tiles(wx, hh, mm, drift=(0, 0)):
    """Idle WEATHER layer: a condition-tinted background with one datum per key — big temperature,
    a drawn weather glyph, the clock, feels-like, hi/lo and place (nothing spans a bezel)."""
    w, h = _ambient_canvas()
    S = 2
    kw, kh = KEY_SIZE
    wx = wx or {}
    cond = wx.get("cond", "")
    night = bool(wx.get("night"))
    canvas = _vgrad(w * S, h * S, *_weather_bg(cond, night)).convert("RGB")
    tiles = _slice_ambient(canvas, S)
    dx, dy = int(drift[0]) * S, int(drift[1]) * S
    temp, feels = wx.get("temp"), wx.get("feels")
    hi, lo = wx.get("hi"), wx.get("lo")
    raw_label = wx.get("label") or ""
    label = "WEATHER" if _coordish(raw_label) else raw_label.upper()
    WH, DIM = (240, 246, 252), (178, 192, 206)
    _ambient_halo_text(tiles[0], f"{temp}°" if temp is not None else "--", 0.60, WH, dx, dy)
    t1 = tiles[1].convert("RGBA")                        # key1: drawn weather glyph
    gl = Image.new("RGBA", t1.size, (0, 0, 0, 0))
    gw, gh = t1.size
    _weather_glyph(gl, gw // 2 + dx, gh // 2 + dy, int(min(gw, gh) * 0.30), cond,
                   DIM, max(2, gw // 22), night=night)
    tiles[1] = Image.alpha_composite(t1, gl).convert("RGB")
    _ambient_halo_text(tiles[2], f"{hh}:{mm}", 0.42, WH, dx, dy)
    _ambient_halo_text(tiles[3], f"~{feels}°" if feels is not None else "", 0.34, DIM, dx, dy)
    hilo = f"{hi}° {lo}°" if hi is not None and lo is not None else ""
    _ambient_halo_text(tiles[4], hilo, 0.28, DIM, dx, dy)
    _ambient_halo_text(tiles[5], label[:9], 0.26, DIM, dx, dy)
    return [t.resize((kw, kh), Image.LANCZOS) for t in tiles]


def _str_hue(s: str) -> float:
    """A stable 0..1 hue derived from a string (FNV-1a) — every track gets its own colour."""
    v = 2166136261
    for c in (s or "x"):
        v = ((v ^ ord(c)) * 16777619) & 0xFFFFFFFF
    return (v % 360) / 360.0


@lru_cache(maxsize=4)
def _np_tint(W: int, H: int, hue: float) -> Image.Image:
    """The backdrop's title-tinted gradient. It depends only on the title, so it is built once per
    track rather than 15 times a second. Callers must not draw on it — copy first."""
    top = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.34))
    bot = tuple(int(c * 255) for c in colorsys.hsv_to_rgb((hue + 0.07) % 1.0, 0.9, 0.05))
    return _vgrad(W, H, top, bot).convert("RGB")


def _now_playing_backdrop(W, H, phase, title):
    """Cover-less now-playing backdrop: a title-tinted gradient with a glowing equalizer that
    dances to a pseudo-beat — far nicer than a flat purple wash when a browser gives no art."""
    _kw, kh = KEY_SIZE
    S = 2
    top_row = kh * S                                    # the equalizer lives WHOLLY in the top row
    hue = _str_hue(title)
    canvas = _np_tint(W, H, hue)
    # The bars (and their bloom) never reach below the top row, so build and blur them in a band
    # that size instead of over the whole canvas — same pixels, about half the work per frame.
    band = min(H, top_row + 32)                         # + margin so the blur is not clipped
    bars = Image.new("RGB", (W, band), (0, 0, 0))       # bars on black -> additive neon glow
    bd = ImageDraw.Draw(bars)
    n = 13
    x0, x1 = int(W * 0.06), int(W * 0.94)
    baseline = int(top_row * 0.90)                      # sit near the bottom of the top row
    max_h = int(top_row * 0.74)                         # tall bars, fully inside the top row
    bwid = (x1 - x0) / n
    barcol = tuple(int(c * 255) for c in colorsys.hsv_to_rgb((hue + 0.12) % 1.0, 0.62, 1.0))
    for i in range(n):
        v = 0.42 + 0.32 * math.sin(phase * 4.2 + i * 0.7) + 0.26 * math.sin(phase * 6.7 + i * 1.9)
        v = max(0.12, min(1.0, v))
        bh = int(max_h * v)
        bx = int(x0 + i * bwid + bwid * 0.16)
        bx2 = int(x0 + (i + 1) * bwid - bwid * 0.16)
        bd.rounded_rectangle([bx, baseline - bh, bx2, baseline],
                             radius=max(2, int(bwid * 0.2)), fill=barcol)
    bars = ImageChops.add(bars, bars.filter(ImageFilter.GaussianBlur(3)))   # bloom
    out = canvas.copy()
    out.paste(ImageChops.add(canvas.crop((0, 0, W, band)), bars), (0, 0))
    return out


# ---- now-playing idle layer -------------------------------------------------------------------
# This layer is the app's whole idle CPU budget: it runs at ~15 fps for as long as music plays, and
# music plays for hours. So everything that does NOT change between frames — the LANCZOS-resampled
# cover, the dark veil, the play/pause pip, the bottom scrim — is baked ONCE into a cached base, and
# a frame is a memcpy of that base plus the ticker text and the progress bar. `now_playing_sig()`
# goes further and lets the caller skip the render entirely when neither of those two moved.
_NP_BASE: dict = {"key": None, "img": None, "art": None}


def _np_title(title, artist) -> str:
    """The ticker string: 'Title · Artist', or just the title when the artist adds nothing."""
    title = (title or "--").strip()
    if artist and artist not in ("MEDIA", "--") and artist.lower() not in title.lower():
        return f"{title}   ·   {artist}"
    return title


@lru_cache(maxsize=8)
def _np_metrics(text: str, band_h: int, W: int):
    """(width, fits?, scroll span) for the ticker — measured once per track, not once per frame."""
    tf = _font(int(band_h * 0.46), bold=True)
    tw = int(ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(text, font=tf))
    avail = int(W * 0.92)
    return tw, tw <= avail, tw + int(W * 0.34)


def _np_scroll(phase: float, span: int, W: int, H: int) -> int:
    """Left edge of the scrolling ticker — one full title plus a gap, seamless."""
    return int(W * 0.02) - int((phase * H * 0.11) % span)


@lru_cache(maxsize=4)
def _np_overlay(W: int, H: int, band_h: int, playing: bool) -> Image.Image:
    """The static chrome over any backdrop: the play/pause pip in its translucent disc, plus the
    bottom scrim the title sits on. One cached RGBA layer -> one composite per frame."""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    pr = int(H * 0.055)
    pcx, pcy = int(W * 0.075), int(H * 0.11)
    d.ellipse([pcx - pr, pcy - pr, pcx + pr, pcy + pr], fill=(255, 255, 255, 46))
    if playing:                                          # play triangle
        t = pr * 0.5
        d.polygon([(pcx - t * 0.7, pcy - t), (pcx - t * 0.7, pcy + t), (pcx + t, pcy)],
                  fill=(255, 255, 255, 255))
    else:                                                # pause bars
        bw2, bh2 = int(pr * 0.22), int(pr * 0.62)
        d.rectangle([pcx - int(pr * 0.42), pcy - bh2, pcx - int(pr * 0.42) + bw2, pcy + bh2],
                    fill=(255, 255, 255, 255))
        d.rectangle([pcx + int(pr * 0.20), pcy - bh2, pcx + int(pr * 0.20) + bw2, pcy + bh2],
                    fill=(255, 255, 255, 255))
    gm = Image.linear_gradient("L").resize((W, band_h)).transpose(Image.FLIP_TOP_BOTTOM)
    scrim = Image.new("RGBA", (W, band_h), (0, 0, 0, 0))
    scrim.putalpha(gm.point(lambda a: int(a * 0.80)))    # dark enough that the title pops on any art
    ov.alpha_composite(scrim, (0, H - band_h))
    return ov


def _np_base(art, W: int, H: int, band_h: int, playing: bool) -> Image.Image:
    """The unchanging part of a cover frame (fitted cover + veil + chrome), baked once per track.
    Keyed on the art object's identity — the artwork reference is held in the cache so the id()
    cannot be recycled onto a different image."""
    key = (id(art), art.size, W, H, band_h, playing)
    if _NP_BASE["key"] == key and _NP_BASE["img"] is not None:
        return _NP_BASE["img"]
    cover = ImageOps.fit(art.convert("RGB"), (W, H), Image.LANCZOS, centering=(0.5, 0.42))
    canvas = Image.alpha_composite(cover.convert("RGBA"), Image.new("RGBA", (W, H), (0, 0, 0, 55)))
    canvas.alpha_composite(_np_overlay(W, H, band_h, playing))
    img = canvas.convert("RGB")
    _NP_BASE.update(key=key, img=img, art=art)
    return img


def _np_geom():
    """Canvas geometry shared by the renderer and the signature, so the two cannot drift."""
    w, h = _ambient_canvas()
    kw, kh = KEY_SIZE
    S = 2
    return S, kw, kh, w * S, h * S, int(kh * S * 0.52)


def now_playing_sig(title, artist, art, phase, playing=True, pos=None):
    """A cheap signature of what the next now-playing frame would LOOK like, computed without
    rendering it. Returns None when the frame animates unconditionally (the cover-less equaliser
    backdrop). With a cover, only the ticker offset and the progress-bar width can change, so a
    track whose title fits redraws ~once instead of 15 times a second."""
    if art is None or min(art.size) < 64:
        return None                                      # equaliser backdrop -> always redraw
    _S, _kw, _kh, W, H, band_h = _np_geom()
    text = _np_title(title, artist)
    _tw, fits, span = _np_metrics(text, band_h, W)
    scroll = None if fits else _np_scroll(phase, span, W, H)
    fillw = None if pos is None else int(W * max(0.0, min(1.0, float(pos))))
    return (id(art), art.size, bool(playing), text, scroll, fillw)


def ambient_now_playing_tiles(title, artist, art, phase, playing=True, pos=None):
    """Idle NOW-PLAYING layer: the cover art (when present) spread across the grid under a bottom
    veil, a play/pause pip in the corner, the title scrolling along the bottom row, and (when the
    media app reports a timeline) a thin neon progress bar along the very bottom. With no usable
    cover it falls back to a title-tinted equalizer backdrop instead of a flat wash."""
    S, kw, kh, W, H, band_h = _np_geom()
    if art is not None and min(art.size) >= 64:         # a real cover -> show it big
        canvas = _np_base(art, W, H, band_h, playing).copy()
    else:                                               # no/*tiny* cover -> the visualiser backdrop
        canvas = _now_playing_backdrop(W, H, phase, (title or "--").strip()).convert("RGBA")
        canvas.alpha_composite(_np_overlay(W, H, band_h, playing))
        canvas = canvas.convert("RGB")
    # title ticker along a slim band at the very bottom (bezels break it into a retro ticker)
    d = ImageDraw.Draw(canvas)
    text = _np_title(title, artist)
    tf = _font(int(band_h * 0.46), bold=True)
    ty = H - band_h + int(band_h * 0.56)
    _tw, fits, span = _np_metrics(text, band_h, W)
    if fits:
        d.text((W // 2, ty), text, font=tf, anchor="mm", fill=(247, 249, 252))
    else:
        base = _np_scroll(phase, span, W, H)             # a little slower -> easier to read
        for off in (base, base + span):
            d.text((off, ty), text, font=tf, anchor="lm", fill=(247, 249, 252))
    if pos is not None:                                  # a thin neon progress bar near the bottom
        pf = max(0.0, min(1.0, float(pos)))
        bh = max(3, int(H * 0.014))
        margin = max(bh, int(H * 0.06))                  # OFF the absolute edge: the device EDGE_INSET
        y1 = H - margin                                  # + CONTENT_SHIFT(dy) eat the last rows
        y0 = y1 - bh
        # dim full-width track: composite just the bar's own rows, not two conversions of the
        # whole canvas (same pixels as compositing the full frame, ~120x less work)
        box = (0, y0, W, y1)
        strip = canvas.crop(box).convert("RGBA")
        strip.alpha_composite(Image.new("RGBA", (W, bh), (255, 255, 255, 38)))
        canvas.paste(strip.convert("RGB"), box)
        fillw = int(W * pf)
        if fillw > 0:
            d.rectangle([0, y0, fillw, y1], fill=(255, 92, 170))  # neon-pink played portion
            d.rectangle([max(0, fillw - 2), y0, fillw, y1], fill=(255, 205, 236))   # bright head
    return [t.resize((kw, kh), Image.LANCZOS) for t in _slice_ambient(canvas, S)]


def _ambient_field(style, hh, mm, weekday, day, month, drift, phase):
    """Any animated-field design: scroll a crop window across the style's pre-built seamless
    field, then draw the classic per-key clock over the slices. A negative sweep scrolls the
    other way (rain/snow falling)."""
    fn, sweep, fill, sub_fill, axis = _FIELD_STYLES[style]
    w, h = _ambient_canvas()
    S = 2
    kw, kh = KEY_SIZE
    base = fn(w * S, h * S)
    if axis == "y":
        off = int(((phase / sweep) % 1.0) * h * S)
        canvas = base.crop((0, off, w * S, off + h * S))
    else:
        off = int(((phase / sweep) % 1.0) * w * S)
        canvas = base.crop((off, 0, off + w * S, h * S))
    tiles = _slice_ambient(canvas, S)
    _ambient_grid_text(tiles, hh, mm, weekday, day, month, drift, S,
                       fill=fill, sub_fill=sub_fill, halo=True)
    return [t.resize((kw, kh), Image.LANCZOS) for t in tiles]


def _nixie_tile(text, frac, drift):
    """One retro nixie tube: a warm glowing digit inside a glass envelope on a dark base."""
    kw, kh = KEY_SIZE
    S = 2
    w2, h2 = kw * S, kh * S
    img = Image.new("RGB", (w2, h2), (11, 8, 6))
    d = ImageDraw.Draw(img)
    inset = int(w2 * 0.12)
    top, bot = int(h2 * 0.07), h2 - int(h2 * 0.08)
    rad = int(w2 * 0.16)
    d.rounded_rectangle([inset, top, w2 - inset, bot], radius=rad,
                        fill=(17, 12, 9), outline=(56, 41, 30), width=3)
    d.rounded_rectangle([inset + 6, bot - int(h2 * 0.075), w2 - inset - 6, bot - 4],
                        radius=6, fill=(32, 25, 20))                     # socket base
    fs = int(h2 * frac)
    while fs > int(h2 * 0.16) and d.textlength(text, font=_font(fs)) > (w2 - 2 * inset) * 0.82:
        fs -= 2
    cx = w2 // 2 + int(drift[0]) * S
    cy = (top + bot) // 2 - int(h2 * 0.02) + int(drift[1]) * S
    # warm glow: two blurred passes of the digit mask, then the sharp filament on top
    mask = Image.new("L", (w2, h2), 0)
    ImageDraw.Draw(mask).text((cx, cy), text, font=_font(fs), anchor="mm", fill=255)
    for blur, alpha, col in ((int(h2 * 0.10), 110, (255, 90, 12)),
                             (int(h2 * 0.035), 160, (255, 130, 30))):
        glow = Image.new("RGBA", (w2, h2), col + (0,))
        glow.putalpha(mask.filter(ImageFilter.GaussianBlur(blur)).point(
            lambda a, al=alpha: int(a * al / 255)))
        img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    d = ImageDraw.Draw(img)
    d.text((cx, cy), text, font=_font(fs), anchor="mm", fill=(255, 196, 110))
    # a thin glass reflection down the left of the envelope
    d.line([(inset + int(w2 * 0.055), top + rad), (inset + int(w2 * 0.055), bot - rad)],
           fill=(148, 136, 124), width=1)
    return img.resize((kw, kh), Image.LANCZOS)


def _ambient_nixie(hh, mm, weekday, day, month, drift):
    """A row of warm retro tubes: 21 | : | 47 up top, WED | 02 | JUL below."""
    return [
        _nixie_tile(hh, 0.52, drift),
        _nixie_tile(":", 0.52, drift),
        _nixie_tile(mm, 0.52, drift),
        _nixie_tile(weekday, 0.30, drift),
        _nixie_tile(day, 0.30, drift),
        _nixie_tile(month, 0.30, drift),
    ]


@lru_cache(maxsize=2)
def _synthwave_sky(W, H, horizon):
    """Static retrowave backdrop — indigo->hot-magenta gradient, a jagged mountain ridge silhouette
    and a glowing horizon band. Cached, so per frame only the stars, pulsing sun and rolling grid
    are recomputed (keeps this animated design low-CPU)."""
    import random
    sky = Image.new("RGB", (W, H), (10, 4, 22))
    sky.paste(_vgrad(W, horizon, (20, 6, 50), (206, 28, 122)), (0, 0))
    rnd = random.Random(23)                              # a distant ridge for depth (deterministic)
    lo = int(horizon * 0.82)
    pts, x = [(0, horizon)], 0
    while x < W:
        x += rnd.randint(int(W * 0.05), int(W * 0.11))
        pts.append((min(x, W), horizon - rnd.randint(0, horizon - lo)))
    pts.append((W, horizon))
    ImageDraw.Draw(sky).polygon(pts, fill=(15, 7, 28))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rectangle([0, horizon - 2, W, horizon + 3], fill=(255, 120, 205))   # glowing horizon band
    gd.line(pts, fill=(255, 70, 195), width=3)                             # neon rim on the ridge
    sky = ImageChops.add(sky, glow.filter(ImageFilter.GaussianBlur(7)))
    ImageDraw.Draw(sky).line(pts, fill=(255, 158, 224), width=1)           # crisp bright ridge edge
    return sky


def _ambient_synthwave(hh, mm, weekday, day, month, drift, phase=0.0):
    """Retrowave: a gradient sky with a distant ridge, a glowing pulsing sun with drifting venetian
    slats, and a FAST dual-neon perspective grid (magenta near -> cyan far) rushing toward you with a
    warm sun reflection and a bloom pass."""
    import random
    w, h = _ambient_canvas()
    S = 2
    W, H = w * S, h * S
    kw, kh = KEY_SIZE
    horizon = int(H * 0.56)
    cx = W // 2
    canvas = _synthwave_sky(W, H, horizon).copy()
    d = ImageDraw.Draw(canvas)
    rnd = random.Random(9)
    for j in range(96):                               # more stars, quicker twinkle
        x, y = rnd.randrange(W), rnd.randrange(int(horizon * 0.72))
        base = rnd.randint(90, 205)
        b = int(base * (0.42 + 0.58 * (0.5 + 0.5 * math.sin(phase * 3.0 + j * 1.7))))
        d.point((x, y), fill=(b, int(b * 0.8), min(255, b + 15)))
    # an occasional shooting star streaking across the upper sky (deterministic per cycle)
    period, dur = 7.3, 0.8
    sp = phase % period
    if sp < dur:
        k = int(phase / period)
        t = sp / dur
        hx = int((_mx(k) % W) + t * W * 0.55)
        hy = int((_mx(k + 99) % max(1, int(horizon * 0.45))) + t * horizon * 0.18)
        ln = int(W * 0.14)
        d.line([(hx - ln, hy - int(ln * 0.34)), (hx, hy)], fill=(255, 232, 246), width=2)
    # a soft warm bloom behind the sun (cheap: draw + blur at 1/4 scale, then upscale)
    pulse = 1.0 + 0.035 * math.sin(phase * 1.7)
    R = max(8, int(H * 0.30 * pulse))
    sc = 4
    bw, bh = W // sc, H // sc
    bloom = Image.new("RGB", (bw, bh), (0, 0, 0))
    br = R // sc
    ImageDraw.Draw(bloom).ellipse([cx // sc - int(br * 1.6), horizon // sc - int(br * 1.6),
                                   cx // sc + int(br * 1.6), horizon // sc + int(br * 0.7)],
                                  fill=(150, 24, 92))
    canvas = ImageChops.add(canvas, bloom.filter(ImageFilter.GaussianBlur(5)).resize((W, H), Image.BILINEAR))
    # the sun disc: warm gradient, drifting venetian slats
    mask = Image.new("L", (2 * R, 2 * R), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 2 * R - 1, 2 * R - 1], fill=255)
    md = ImageDraw.Draw(mask)
    slat_drift = (phase * 0.06) % 0.11
    for i in range(5):                                # widening slats, slowly drifting upward
        yf = 0.52 + i * 0.11 - slat_drift
        if yf <= 0.30:
            continue
        y0 = int(2 * R * yf)
        md.rectangle([0, y0, 2 * R, y0 + 3 + i * 2], fill=0)
    sun = _vgrad(2 * R, 2 * R, (255, 236, 130), (255, 44, 140))
    canvas.paste(sun, (cx - R, horizon - R), mask)
    # the neon floor grid on its own layer so a blur pass gives it a real glow
    floor_h = H - horizon
    floor = Image.new("RGB", (W, floor_h), (12, 5, 26))
    col_w = max(2, int(W * 0.12))                     # warm sun reflection down the centre (behind grid)
    floor.paste(_vgrad(col_w, floor_h, (86, 20, 56), (12, 5, 26)), (cx - col_w // 2, 0))
    fd = ImageDraw.Draw(floor)
    for k in range(-15, 16):                           # verticals converge on the vanishing point
        fd.line([(cx + k * int(W * 0.007), 0), (cx + k * int(W * 0.102), floor_h)], fill=(150, 70, 235), width=1)
    N = 15
    scroll = (phase * 0.85) % 1.0                      # faster rush toward you (~1.2 s / loop)
    near_c, far_c = (255, 58, 190), (86, 214, 255)     # magenta near -> cyan far (depth)
    for i in range(N + 2):
        dd = (i + scroll) / N                          # depth 0 (far) -> 1 (underfoot), rolling
        if dd <= 0.02:
            continue
        gy = floor_h * (dd ** 2.4)                     # perspective: far lines crowd the horizon
        if gy < 1 or gy > floor_h:
            continue
        fade = max(0.28, min(1.0, dd * 1.35))
        col = tuple(int((far_c[j] + (near_c[j] - far_c[j]) * dd) * fade) for j in range(3))
        fd.line([(0, int(gy)), (W, int(gy))], fill=col, width=1 if dd < 0.4 else 3)
    for i in range(6):                                 # the sun's warm reflection mirrored on the grid
        ry = int(floor_h * (0.015 + i * 0.05))
        rw = int(R * (0.92 - i * 0.14))
        if rw < 3:
            break
        c = max(24, 150 - i * 22)
        fd.line([(cx - rw, ry), (cx + rw, ry)],
                fill=(min(255, c + 80), int(c * 0.4), int(c * 0.55)), width=2 if i < 3 else 1)
    floor = ImageChops.add(floor, floor.filter(ImageFilter.GaussianBlur(3)))   # neon bloom
    canvas.paste(floor, (0, horizon))
    ImageDraw.Draw(canvas).line([(0, horizon), (W, horizon)], fill=(255, 170, 230), width=2)
    tiles = _slice_ambient(canvas, S)
    _ambient_grid_text(tiles, hh, mm, weekday, day, month, drift, S,
                       fill=(255, 244, 250), sub_fill=(232, 180, 224), halo=True)
    return [t.resize((kw, kh), Image.LANCZOS) for t in tiles]


def _ambient_starfield(hh, mm, weekday, day, month, drift, phase):
    """Warp starfield: stars stream out from the centre, accelerating with motion-blur streaks —
    per-frame, pure f(phase) like the matrix rain (deterministic, no hidden state)."""
    w, h = _ambient_canvas()
    S = 2
    kw, kh = KEY_SIZE
    W, H = w * S, h * S
    canvas = Image.new("RGB", (W, H), (2, 3, 8))
    d = ImageDraw.Draw(canvas)
    cx, cy = W / 2.0, H / 2.0
    maxR = math.hypot(cx, cy)
    for i in range(110):
        seed = _mx(i + 1)
        ang = (seed % 62832) / 10000.0                 # 0..2π, fixed per star
        speed = 0.05 + (seed % 100) / 100.0 * 0.11
        t = ((seed % 1000) / 1000.0 + phase * speed) % 1.0
        dist = t * t * maxR                            # accelerate outward
        dx, dy = math.cos(ang), math.sin(ang)
        x, y = cx + dx * dist, cy + dy * dist
        if not (0 <= x < W and 0 <= y < H):
            continue
        b = int(40 + t * 215)
        streak = t * t * 26                            # longer trails near the edge (fast)
        d.line([(x - dx * streak, y - dy * streak), (x, y)],
               fill=(b, b, min(255, b + 18)), width=1 if t < 0.55 else 2)
    tiles = _slice_ambient(canvas, S)
    _ambient_grid_text(tiles, hh, mm, weekday, day, month, drift, S,
                       fill=(232, 238, 248), sub_fill=(150, 166, 192), halo=True)
    return [t.resize((kw, kh), Image.LANCZOS) for t in tiles]


@lru_cache(maxsize=4)
def _plasma_rad(pw, ph):
    """Radial distance grid — constant for a fixed (pw,ph), so compute it once, not per frame."""
    cxp, cyp = pw / 2.0, ph / 2.0
    return [[math.hypot(x - cxp, y - cyp) for x in range(pw)] for y in range(ph)]


@lru_cache(maxsize=2)
def _plasma_palette(sat, val):
    """256-entry hue LUT for the plasma (sat/val are constant) — replaces 6,864 hsv_to_rgb/frame."""
    return tuple(tuple(int(c * 255) for c in colorsys.hsv_to_rgb(i / 256.0, sat, val)) for i in range(256))


def _ambient_plasma(hh, mm, weekday, day, month, drift, phase):
    """Classic demoscene plasma — interfering sine fields whose hue flows over time. Computed at
    low res per frame then bilinear-upscaled (smooth colour fields lose nothing)."""
    w, h = _ambient_canvas()
    S = 2
    kw, kh = KEY_SIZE
    W, H = w * S, h * S
    pw, ph = 104, 66
    rad = _plasma_rad(pw, ph)                     # cached radius grid (hypot is frame-invariant)
    pal = _plasma_palette(0.62, 0.52)             # cached hue LUT
    sinx = [math.sin(x / 8.0 + phase * 1.3) for x in range(pw)]   # per-x, hoisted out of the y-loop
    sxy = [math.sin(k / 10.0 + phase * 0.9) for k in range(pw + ph)]
    px = []
    for y in range(ph):
        sy = math.sin(y / 6.5 + phase * 1.1)
        radrow = rad[y]
        for x in range(pw):
            v = sinx[x] + sy + sxy[x + y] + math.sin(radrow[x] / 7.0 - phase * 1.6)
            px.append(pal[int((v / 8.0 + 0.5 + phase * 0.04) % 1.0 * 256) & 255])
    small = Image.new("RGB", (pw, ph))
    small.putdata(px)
    canvas = small.resize((W, H), Image.BILINEAR)
    tiles = _slice_ambient(canvas, S)
    _ambient_grid_text(tiles, hh, mm, weekday, day, month, drift, S,
                       fill=(255, 255, 255), sub_fill=(238, 242, 248), halo=True)
    return [t.resize((kw, kh), Image.LANCZOS) for t in tiles]


def _ambient_lava(hh, mm, weekday, day, month, drift, phase):
    """Lava lamp: warm blobs drift and merge in the dark — a big blur fuses them into molten
    meta-blobs (additive over a warm gradient)."""
    w, h = _ambient_canvas()
    S = 2
    kw, kh = KEY_SIZE
    W, H = w * S, h * S
    canvas = _vgrad(W, H, (24, 4, 30), (44, 9, 16)).convert("RGB")
    blobs = Image.new("RGB", (W, H), (0, 0, 0))
    bd = ImageDraw.Draw(blobs)
    specs = [  # (x frac, vertical amp, radius frac, drift speed, colour)
        (0.28, 0.34, 0.20, 0.33, (255, 92, 28)),
        (0.55, 0.40, 0.17, 0.27, (255, 44, 110)),
        (0.74, 0.30, 0.19, 0.38, (255, 150, 40)),
        (0.42, 0.44, 0.14, 0.23, (236, 70, 40)),
    ]
    for i, (bxf, amp, rf, spd, col) in enumerate(specs):
        y = (0.5 + amp * math.sin(phase * spd + i * 1.7)) * H
        x = bxf * W + 0.05 * W * math.sin(phase * (spd * 0.8) + i * 2.3)
        rr = int(rf * H * (1.0 + 0.16 * math.sin(phase * 0.5 + i * 2.0)))
        bd.ellipse([x - rr, y - rr, x + rr, y + rr], fill=col)
    blobs = blobs.filter(ImageFilter.GaussianBlur(int(H * 0.065)))   # fuse into meta-blobs
    canvas = ImageChops.add(canvas, blobs)
    tiles = _slice_ambient(canvas, S)
    _ambient_grid_text(tiles, hh, mm, weekday, day, month, drift, S,
                       fill=(255, 246, 236), sub_fill=(230, 188, 168), halo=True)
    return [t.resize((kw, kh), Image.LANCZOS) for t in tiles]


def _flip_tile(text, frac, drift):
    """One split-flap card: a two-half plate with the digit crossing the fold line."""
    kw, kh = KEY_SIZE
    S = 2
    w2, h2 = kw * S, kh * S
    img = Image.new("RGB", (w2, h2), (9, 10, 12))
    d = ImageDraw.Draw(img)
    inset = int(w2 * 0.07)
    top, bot = int(h2 * 0.09), h2 - int(h2 * 0.09)
    mid = (top + bot) // 2
    rad = int(w2 * 0.12)
    d.rounded_rectangle([inset, top, w2 - inset, bot], radius=rad, fill=(40, 44, 51))
    d.rounded_rectangle([inset, mid, w2 - inset, bot], radius=rad, fill=(31, 34, 40))
    d.rectangle([inset, mid, w2 - inset, mid + rad], fill=(31, 34, 40))   # square the fold edge
    fs = int(h2 * frac)
    while fs > int(h2 * 0.16) and d.textlength(text, font=_font(fs)) > (w2 - 2 * inset) * 0.86:
        fs -= 2
    cx = w2 // 2 + int(drift[0]) * S
    cy = mid + int(drift[1]) * S
    d.text((cx, cy), text, font=_font(fs), anchor="mm", fill=(236, 240, 246))
    d.line([(inset, mid), (w2 - inset, mid)], fill=(10, 11, 13), width=max(2, S * 2))
    d.line([(inset, mid + max(2, S * 2)), (w2 - inset, mid + max(2, S * 2))],
           fill=(58, 63, 72), width=1)
    pw, ph = int(w2 * 0.045), int(h2 * 0.10)          # the side pivots
    d.rectangle([inset - 2, mid - ph // 2, inset + pw, mid + ph // 2], fill=(14, 15, 17))
    d.rectangle([w2 - inset - pw, mid - ph // 2, w2 - inset + 2, mid + ph // 2],
                fill=(14, 15, 17))
    return img.resize((kw, kh), Image.LANCZOS)


def _ambient_flip(hh, mm, weekday, day, month, drift):
    """A split-flap departures board: 21 | : | 47 up top, WED | 02 | JUL below."""
    return [
        _flip_tile(hh, 0.60, drift),
        _flip_tile(":", 0.60, drift),
        _flip_tile(mm, 0.60, drift),
        _flip_tile(weekday, 0.34, drift),
        _flip_tile(day, 0.34, drift),
        _flip_tile(month, 0.34, drift),
    ]


def _ambient_minimal(hh, mm, weekday, day, month, drift):
    """Nearly-dark: a small dim time on the top-middle key, a fainter date below it."""
    kw, kh = KEY_SIZE
    blank = Image.new("RGB", (kw, kh), (6, 9, 12))
    tiles = [blank.copy() for _ in range(6)]
    tiles[1] = _ambient_tile(f"{hh}:{mm}", (118, 128, 138), 0.34, drift)
    tiles[4] = _ambient_tile(f"{day} {month}", (66, 74, 82), 0.20, drift)
    return tiles


def _ambient_night(hh, mm, weekday, day, month, drift):
    """A quiet night sky: deep-blue gradient, scattered stars, a crescent moon, soft clock."""
    import random
    w, h = _ambient_canvas()
    S = 2
    W, H = w * S, h * S
    canvas = _vgrad(W, H, (10, 14, 30), (3, 5, 12)).convert("RGB")
    d = ImageDraw.Draw(canvas)
    rnd = random.Random(11)                          # deterministic -> the sky never flickers
    for _ in range(150):
        x, y = rnd.randrange(W), rnd.randrange(H)
        b = rnd.randint(90, 230)
        r = S if rnd.random() < 0.14 else 1
        d.ellipse([x - r, y - r, x + r, y + r], fill=(b, b, min(255, b + 25)))
    # crescent moon peeking from the top-right key's corner (drawn first; digits go on top)
    kw, kh = KEY_SIZE
    g = _AMBIENT_GAP
    mr = int(kh * S * 0.17)
    mx = int((2 * (kw + g) + kw * 0.82) * S)
    my = int(kh * 0.18 * S)
    moon = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    md = ImageDraw.Draw(moon)
    md.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(224, 229, 240, 225))
    md.ellipse([mx - mr + int(mr * 0.55), my - mr - int(mr * 0.18),
                mx + mr + int(mr * 0.55), my + mr - int(mr * 0.18)], fill=(0, 0, 0, 0))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), moon).convert("RGB")
    tiles = _slice_ambient(canvas, S)
    _ambient_grid_text(tiles, hh, mm, weekday, day, month, drift, S,
                       fill=(212, 222, 240), sub_fill=(132, 146, 170), halo=False)
    return [t.resize((kw, kh), Image.LANCZOS) for t in tiles]


def _solar_sky(now_min, sr, ss):
    """(top_rgb, bottom_rgb) sky gradient for local minute `now_min`, given sunrise/sunset minutes.
    Smoothly crosses pre-dawn -> sunrise -> day -> sunset -> dusk -> night through a twilight span."""
    def L(a, b, t):
        t = max(0.0, min(1.0, t))
        return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
    NT, NB = (9, 11, 30), (3, 4, 12)          # deep night
    RT, RB = (78, 104, 158), (245, 158, 98)   # sunrise — warm horizon
    DT, DB = (48, 118, 214), (140, 192, 236)  # day — blue
    ST, SB = (70, 58, 120), (236, 120, 74)    # sunset — warm
    UT, UB = (24, 24, 58), (64, 42, 74)       # dusk — purple
    tw = 55                                    # twilight span (min) around each event
    if sr - tw <= now_min < sr:
        return L(NT, RT, (now_min - (sr - tw)) / tw), L(NB, RB, (now_min - (sr - tw)) / tw)
    if sr <= now_min < sr + tw:
        return L(RT, DT, (now_min - sr) / tw), L(RB, DB, (now_min - sr) / tw)
    if sr + tw <= now_min < ss - tw:
        return DT, DB
    if ss - tw <= now_min < ss:
        return L(DT, ST, (now_min - (ss - tw)) / tw), L(DB, SB, (now_min - (ss - tw)) / tw)
    if ss <= now_min < ss + tw:
        return L(ST, UT, (now_min - ss) / tw), L(SB, UB, (now_min - ss) / tw)
    if ss + tw <= now_min < ss + 2 * tw:
        return L(UT, NT, (now_min - (ss + tw)) / tw), L(UB, NB, (now_min - (ss + tw)) / tw)
    return NT, NB


def _ambient_solar(hh, mm, weekday, day, month, drift, solar):
    """A sky that tracks the REAL solar day for the configured location: pre-dawn indigo, a warm
    sunrise, blue midday, a golden sunset, dusk and a starry night — with a sun (day) or crescent
    moon (night) disc riding the day arc. `solar` = (sunrise_min, sunset_min, is_day) or None."""
    import random
    w, h = _ambient_canvas()
    S = 2
    W, H = w * S, h * S
    try:
        now_min = int(hh) * 60 + int(mm)
    except (TypeError, ValueError):
        now_min = 12 * 60
    if solar and solar[0] is not None and solar[1] is not None and solar[1] > solar[0]:
        sr, ss = int(solar[0]), int(solar[1])
    else:
        sr, ss = 6 * 60 + 30, 19 * 60 + 30     # graceful default until real sun times arrive
    is_day = sr <= now_min < ss
    top, bot = _solar_sky(now_min, sr, ss)
    canvas = _vgrad(W, H, top, bot).convert("RGB")
    d = ImageDraw.Draw(canvas)
    # stars fade in as the sky darkens (night + twilight), keyed off the top-of-sky brightness
    sky_lum = 0.299 * top[0] + 0.587 * top[1] + 0.114 * top[2]
    sb = max(0.0, min(1.0, (46 - sky_lum) / 40.0))
    if sb > 0.08:
        rnd = random.Random(11)                          # deterministic -> the sky never flickers
        for _ in range(130):
            x, y = rnd.randrange(W), rnd.randrange(int(H * 0.72))
            b = int(min(255, rnd.randint(90, 220)) * sb)
            if b <= 12:
                continue
            r = S if rnd.random() < 0.12 else 1
            d.ellipse([x - r, y - r, x + r, y + r], fill=(b, b, min(255, b + 20)))
    # sun / moon riding an arc across the panel (frac 0..1 = rise -> set)
    if is_day:
        frac = (now_min - sr) / max(1, ss - sr)
        near = 1.0 - abs(frac - 0.5) * 2.0               # 0 at horizon, 1 at midday
        lo, hi = (255, 172, 88), (255, 240, 186)
        body = tuple(int(lo[i] + (hi[i] - lo[i]) * max(0.0, near)) for i in range(3))
        glow = (255, 208, 148)
    else:
        frac = ((now_min - ss) % 1440) / max(1, sr + 1440 - ss)
        body, glow = (226, 231, 246), (150, 168, 210)
    margin = int(W * 0.14)
    bx = int(margin + max(0.0, min(1.0, frac)) * (W - 2 * margin))
    by = int(H * 0.80 - math.sin(max(0.0, min(1.0, frac)) * math.pi) * H * 0.6)
    br = max(6, int(min(w, h) * 0.18 * S))
    disc = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(disc).ellipse([bx - br * 2, by - br * 2, bx + br * 2, by + br * 2], fill=glow + (75,))
    disc = disc.filter(ImageFilter.GaussianBlur(br))
    ImageDraw.Draw(disc).ellipse([bx - br, by - br, bx + br, by + br], fill=body + (255,))
    if not is_day:                                       # carve the crescent
        off = int(br * 0.5)
        ImageDraw.Draw(disc).ellipse([bx - br + off, by - br - int(br * 0.14),
                                      bx + br + off, by + br - int(br * 0.14)], fill=(0, 0, 0, 0))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), disc).convert("RGB")
    tiles = _slice_ambient(canvas, S)
    _ambient_grid_text(tiles, hh, mm, weekday, day, month, drift, S,   # light text + halo reads on
                       fill=(244, 248, 253), sub_fill=(214, 224, 238), halo=True)   # bright or dark sky
    kw, kh = KEY_SIZE
    return [t.resize((kw, kh), Image.LANCZOS) for t in tiles]


def ambient_clock_tiles(hh, mm, weekday, day, month, *, accent=None, drift=(0, 0),
                        style="classic", phase=0.0, solar=None):
    """The idle/ambient clock, in the configured design. Text is ALWAYS per-key (real bezels
    are wider than the canvas gap, so spanning text gets chopped on hardware); only the smooth
    backgrounds (rainbow/aurora/ocean/ember fields, night sky) span the grid. `phase` (seconds
    since ambient started) drives the animated styles."""
    if style == "matrix":                    # simulated per frame, not a scrolling field
        return _ambient_matrix(hh, mm, weekday, day, month, drift, phase)
    if style in _FIELD_STYLES:
        return _ambient_field(style, hh, mm, weekday, day, month, drift, phase)
    if style == "synthwave":
        return _ambient_synthwave(hh, mm, weekday, day, month, drift, phase)
    if style == "starfield":
        return _ambient_starfield(hh, mm, weekday, day, month, drift, phase)
    if style == "plasma":
        return _ambient_plasma(hh, mm, weekday, day, month, drift, phase)
    if style == "lava":
        return _ambient_lava(hh, mm, weekday, day, month, drift, phase)
    if style == "flip":
        return _ambient_flip(hh, mm, weekday, day, month, drift)
    if style == "nixie":
        return _ambient_nixie(hh, mm, weekday, day, month, drift)
    if style == "minimal":
        return _ambient_minimal(hh, mm, weekday, day, month, drift)
    if style == "night":
        return _ambient_night(hh, mm, weekday, day, month, drift)
    if style == "solar":
        return _ambient_solar(hh, mm, weekday, day, month, drift, solar)
    col = _parse_rgb(accent) if accent is not None else (130, 235, 175)   # soft mint
    sub = (140, 160, 175)
    BIG, MED = 0.66, 0.42
    return [
        _ambient_tile(hh, col, BIG, drift),
        _ambient_tile(":", col, BIG, drift),
        _ambient_tile(mm, col, BIG, drift),
        _ambient_tile(weekday, sub, MED, drift),
        _ambient_tile(day, col, MED, drift),
        _ambient_tile(month, sub, MED, drift),
    ]


def ambient_preview(style: str, target_w: int = 280) -> Image.Image:
    """A little 6-key board rendering of one ambient design (for the Settings design picker).
    Disk-cached under the config dir — with 12 designs (several building whole fields) a cold
    render of every preview would lag the configurator's startup."""
    kw, kh = KEY_SIZE
    try:
        from .config import config_dir
        cdir = os.path.join(config_dir(), "cache")
        os.makedirs(cdir, exist_ok=True)
        cpath = os.path.join(cdir,
                             f"ambient_{style}_{kw}x{kh}_w{target_w}_v{_AMBIENT_PREVIEW_VER}.png")
        if os.path.exists(cpath):
            return Image.open(cpath).convert("RGB")
    except Exception:
        cpath = None
    tiles = ambient_clock_tiles("21", "47", "WED", "02", "JUL", style=style, phase=7.0)
    g = 8                                            # slim bezel reads better at preview size
    W, H = 3 * kw + 2 * g, 2 * kh + g
    board = Image.new("RGB", (W, H), (12, 14, 18))
    for i, t in enumerate(tiles):
        r, c = divmod(i, 3)
        board.paste(t.resize((kw, kh), Image.LANCZOS) if t.size != (kw, kh) else t,
                    (c * (kw + g), r * (kh + g)))
    s = target_w / W
    out = board.resize((target_w, max(1, int(round(H * s)))), Image.LANCZOS)
    if cpath:
        try:
            out.save(cpath)
        except OSError:
            pass
    return out


def panel_frames(path: str, gap: int = 22, max_frames: int = 240):
    """Slice an image (or every frame of an animated GIF) across the 2×3 key grid.

    Returns a list of frames; each frame is a list of 6 RGB tiles (row-major, key1..key6) at
    the current KEY_SIZE. A static image yields a single frame. `gap` approximates the bezel
    so the picture reads as continuous across the keys.
    """
    im = Image.open(path)
    try:
        n = min(int(getattr(im, "n_frames", 1)), max_frames)
    except Exception:
        n = 1
    kw, kh = KEY_SIZE
    total = (3 * kw + 2 * gap, 2 * kh + gap)
    frames = []
    for fi in range(n):
        try:
            im.seek(fi)
        except EOFError:
            break
        src = ImageOps.pad(im.convert("RGB"), total, color=(0, 0, 0), centering=(0.5, 0.5))
        tiles = [src.crop((c * (kw + gap), r * (kh + gap),
                           c * (kw + gap) + kw, r * (kh + gap) + kh))
                 for r in range(2) for c in range(3)]
        frames.append(tiles)
    return frames


def press_effect(img: Image.Image) -> Image.Image:
    """A 'pressed' variant of a face: a soft brighten + accent border (key-press feedback)."""
    base = img.convert("RGB")
    # blend toward white (won't clip already-light faces like a multiply would), + 2px border.
    out = Image.blend(base, Image.new("RGB", base.size, (255, 255, 255)), 0.22)
    d = ImageDraw.Draw(out)
    w, h = out.size
    d.rectangle([0, 0, w - 1, h - 1], outline=(61, 139, 255), width=2)
    return out


def _bg_sample(face: Image.Image) -> Tuple[int, int, int]:
    """Pick a fill colour for exposed borders by sampling the face's four corners."""
    w, h = face.size
    pts = [(1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2)]
    cols = []
    for p in pts:
        c = face.getpixel(p)
        cols.append(c[:3] if isinstance(c, tuple) else (c, c, c))
    return tuple(int(sorted(c[i] for c in cols)[2]) for i in range(3))  # type: ignore[return-value]


def _scaled_face(face: Image.Image, s: float, bg: Tuple[int, int, int]) -> Image.Image:
    """Scale `face` about its centre by `s`; shrink letterboxes with `bg`, grow crops to size."""
    w, h = face.size
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    scaled = face.resize((nw, nh), Image.LANCZOS)
    if s <= 1.0:
        out = Image.new("RGB", (w, h), bg)
        out.paste(scaled, ((w - nw) // 2, (h - nh) // 2))
        return out
    left, top = (nw - w) // 2, (nh - h) // 2
    return scaled.crop((left, top, left + w, top + h))


def _translate_face(face: Image.Image, dx: float, dy: float,
                    bg: Tuple[int, int, int]) -> Image.Image:
    if not dx and not dy:
        return face.copy()
    out = Image.new("RGB", face.size, bg)
    out.paste(face, (int(round(dx)), int(round(dy))))
    return out


def _vsquash(face: Image.Image, sy: float, bg: Tuple[int, int, int]) -> Image.Image:
    """Squash vertically (keep width), anchored to the bottom — an 'impact' on landing."""
    w, h = face.size
    nh = max(1, round(h * sy))
    out = Image.new("RGB", (w, h), bg)
    out.paste(face.resize((w, nh), Image.LANCZOS), (0, h - nh))
    return out


def _rotate_face(face: Image.Image, deg: float, bg: Tuple[int, int, int]) -> Image.Image:
    return face.rotate(deg, resample=Image.BICUBIC, fillcolor=bg)


def _glow(face: Image.Image, g: float) -> Image.Image:
    if g <= 0:
        return face
    return Image.blend(face, Image.new("RGB", face.size, (255, 255, 255)), g)


# --- press-animation styles: each maps a face -> a list of 60x60 frames ---------
PRESS_BOUNCE = (0.84, 0.74, 0.88, 1.10, 1.03, 0.98, 1.0)
PRESS_GLOW = (0.12, 0.18, 0.11, 0.05, 0.0, 0.0, 0.0)


def _anim_bounce(face, bg):
    """Squash-and-stretch: press in, springy overshoot, settle."""
    return [_glow(_scaled_face(face, s, bg), g) for s, g in zip(PRESS_BOUNCE, PRESS_GLOW)]


def _anim_jump(face, bg):
    """macOS-Dock style: a smooth, slow hop that lifts toward the top of the key.

    The icon NEVER clips: the upward travel is bounded by the headroom that opens up as
    the icon shrinks, so the top edge always stays on-screen while a gap grows below it
    (which is what reads as 'jumping up'). Gravity easing + many frames keeps it fluid.
    """
    h = face.size[1]

    def hop(n, s_min):
        out = []
        for i in range(n):
            t = i / (n - 1)
            a = 4.0 * t * (1.0 - t)           # 0 -> 1 (apex) -> 0, slow near the top
            s = 1.0 - (1.0 - s_min) * a
            f = _scaled_face(face, s, bg) if s < 0.999 else face
            top_margin = (h - h * s) / 2.0    # empty space above the centred, shrunk icon
            dy = -top_margin * a              # rise into exactly that headroom: top stays >= 0
            out.append(_translate_face(f, 0, dy, bg))
        return out

    return hop(20, 0.64) + hop(14, 0.82)[1:]  # a tall hop, then a smaller settle hop


def _anim_pop(face, bg):
    """Quick scale-up flash and back."""
    seq = ((1.0, 0.10), (1.18, 0.16), (1.12, 0.07), (1.04, 0.0), (1.0, 0.0))
    return [_glow(_scaled_face(face, s, bg), g) for s, g in seq]


def _anim_pulse(face, bg):
    """A brightness pulse — no movement, subtle."""
    return [_glow(face, g) for g in (0.0, 0.12, 0.22, 0.30, 0.22, 0.12, 0.04, 0.0)]


def _anim_shake(face, bg):
    """A quick horizontal wiggle."""
    return [_translate_face(face, dx, 0, bg) for dx in (0, 5, -5, 4, -4, 2, -2, 1, 0)]


def _anim_spin(face, bg):
    """A full 360 spin of the icon."""
    return [_rotate_face(face, d, bg) for d in (0, 45, 90, 135, 180, 225, 270, 315, 360)]


def _anim_sink(face, bg):
    """A physical key-press feel: sink in and ease back, no overshoot."""
    seq = ((0.93, 0.10), (0.87, 0.15), (0.84, 0.10), (0.88, 0.04), (0.94, 0.0), (1.0, 0.0))
    return [_glow(_scaled_face(face, s, bg), g) for s, g in seq]


def _anim_flash(face, bg):
    """The classic brighten + accent-border blink."""
    return [press_effect(face), _glow(face, 0.12), face]


# ---- extra dynamic helpers -----------------------------------------------------
def _scale_xy(face, sx, sy, bg):
    """Non-uniform scale about the centre (used for flips/squash)."""
    w, h = face.size
    nw, nh = max(1, round(w * sx)), max(1, round(h * sy))
    out = Image.new("RGB", (w, h), bg)
    out.paste(face.resize((nw, nh), Image.LANCZOS), ((w - nw) // 2, (h - nh) // 2))
    return out


def _tint(face, rgb, alpha):
    if alpha <= 0:
        return face.convert("RGB")
    return Image.blend(face.convert("RGB"), Image.new("RGB", face.size, rgb), min(1.0, alpha))


def _rgb_split(face, off):
    r, g, b = face.convert("RGB").split()
    return Image.merge("RGB", (ImageChops.offset(r, off, 0), g, ImageChops.offset(b, -off, 0)))


def _ring(face, radius, alpha, color=(255, 255, 255), width=3):
    w, h = face.size
    base = face.convert("RGBA")
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    cx, cy = w / 2, h / 2
    d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
              outline=color + (int(alpha),), width=width)
    return Image.alpha_composite(base, ov).convert("RGB")


# ---- extra dynamic press animations -------------------------------------------
def _anim_flip(face, bg):
    """A horizontal card-flip (squash the width to a sliver and back), twice."""
    out = []
    n = 16
    for i in range(n):
        sx = abs(math.cos(i / (n - 1) * 2 * math.pi))
        f = _scale_xy(face, max(0.05, sx), 1.0, bg)
        if sx < 0.25:
            f = _glow(f, 0.25)                       # highlight as it passes edge-on
        out.append(f)
    return out


def _anim_flipv(face, bg):
    """A vertical flip."""
    out = []
    n = 14
    for i in range(n):
        sy = abs(math.cos(i / (n - 1) * math.pi))
        out.append(_scale_xy(face, 1.0, max(0.05, sy), bg))
    return out


def _anim_swing(face, bg):
    """A pendulum swing that settles."""
    return [_rotate_face(face, a, bg)
            for a in (0, -15, -22, -16, -4, 9, 16, 11, 2, -6, -2, 0)]


def _anim_wobble(face, bg):
    """Jelly wobble: rotation and scale combined."""
    seq = [(0, 1.0), (-13, 1.09), (10, 0.94), (-7, 1.05), (4, 0.98), (-2, 1.01), (0, 1.0)]
    return [_scaled_face(_rotate_face(face, a, bg), s, bg) for a, s in seq]


def _anim_zoom(face, bg):
    """Zoom-blast: lunges toward you with a flash, then snaps back."""
    seq = [(1.0, 0.0), (1.3, 0.12), (1.7, 0.22), (2.2, 0.34), (1.5, 0.16), (1.0, 0.0)]
    return [_glow(_scaled_face(face, s, bg), g) for s, g in seq]


def _anim_drop(face, bg):
    """Drops in from above and bounces to rest."""
    dys = (-46, -34, -22, -10, 0, -7, -2, -5, 0, -2, 0)
    return [_translate_face(face, 0, dy, bg) for dy in dys]


def _anim_heartbeat(face, bg):
    """A double-thump pulse."""
    seq = [(1.0, 0.06), (1.17, 0.18), (1.02, 0.04), (1.15, 0.16), (1.0, 0.02), (1.0, 0.0)]
    return [_glow(_scaled_face(face, s, bg), g) for s, g in seq]


def _anim_ripple(face, bg):
    """A water-ripple ring expands from the centre."""
    out = []
    n = 11
    for i in range(n):
        t = i / (n - 1)
        out.append(_ring(face, int(t * 42), 200 * (1 - t), width=3))
    out.append(face)
    return out


def _anim_colorflash(face, bg):
    """Flashes a cool accent colour over the face."""
    return [_tint(face, (61, 139, 255), a) for a in (0.0, 0.4, 0.28, 0.16, 0.06, 0.0)]


def _anim_glitch(face, bg):
    """A digital RGB-split glitch jitter."""
    out = []
    for off in (0, 4, -3, 5, -2, 3, -1, 0):
        out.append(face if off == 0 else _rgb_split(face, off))
    return out


def _anim_sparkle(face, bg):
    """Little twinkles pop across the face."""
    pts = [(13, 15), (47, 17), (19, 45), (42, 43), (31, 9), (30, 50)]
    out = []
    n = 9
    for i in range(n):
        t = i / (n - 1)
        f = face.convert("RGBA")
        ov = Image.new("RGBA", f.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        for j, (x, y) in enumerate(pts):
            ph = t - j * 0.11
            a = math.sin(ph * math.pi) if 0 <= ph <= 1 else 0
            if a > 0:
                v = int(255 * a)
                d.line([(x - 4, y), (x + 4, y)], fill=(255, 255, 255, v), width=1)
                d.line([(x, y - 4), (x, y + 4)], fill=(255, 255, 255, v), width=1)
        out.append(Image.alpha_composite(f, ov).convert("RGB"))
    return out


def _anim_neon(face, bg):
    """A rainbow neon border races around the key."""
    out = []
    n = 12
    for i in range(n):
        hue = (i / n) % 1.0
        r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.9, 1.0))
        f = face.convert("RGB").copy()
        d = ImageDraw.Draw(f)
        for wdt in range(3):
            d.rectangle([wdt, wdt, 59 - wdt, 59 - wdt], outline=(r, g, b))
        out.append(f)
    out.append(face)
    return out


def _anim_squash(face, bg):
    """Anticipation squash & stretch (wide-short then tall-thin then settle)."""
    seq = [(1.0, 1.0), (1.18, 0.84), (0.86, 1.16), (1.08, 0.95), (0.97, 1.02), (1.0, 1.0)]
    return [_scale_xy(face, sx, sy, bg) for sx, sy in seq]


def _anim_spin3d(face, bg):
    """A faster full spin with a mid-spin brightness kick."""
    out = []
    n = 12
    for i in range(n):
        deg = 360 * i / (n - 1)
        f = _rotate_face(face, deg, bg)
        out.append(_glow(f, 0.18) if 120 < deg < 240 else f)
    return out


PRESS_ANIMS = {
    "bounce": _anim_bounce, "jump": _anim_jump, "pop": _anim_pop, "pulse": _anim_pulse,
    "shake": _anim_shake, "spin": _anim_spin, "sink": _anim_sink, "flash": _anim_flash,
    "flip": _anim_flip, "flipv": _anim_flipv, "swing": _anim_swing, "wobble": _anim_wobble,
    "zoom": _anim_zoom, "drop": _anim_drop, "heartbeat": _anim_heartbeat, "ripple": _anim_ripple,
    "colorflash": _anim_colorflash, "glitch": _anim_glitch, "sparkle": _anim_sparkle,
    "neon": _anim_neon, "squash": _anim_squash, "spin3d": _anim_spin3d,
}
PRESS_ANIM_ORDER = [
    "bounce", "jump", "pop", "drop", "squash", "wobble", "swing", "pulse", "heartbeat",
    "shake", "glitch", "flip", "flipv", "spin", "spin3d", "zoom", "ripple", "sparkle",
    "neon", "colorflash", "sink", "flash",
]
PRESS_ANIM_LABELS = {
    "bounce": "Bounce", "jump": "Jump (macOS)", "pop": "Pop", "drop": "Drop in",
    "squash": "Squash & stretch", "wobble": "Wobble", "swing": "Swing", "pulse": "Pulse",
    "heartbeat": "Heartbeat", "shake": "Shake", "glitch": "Glitch", "flip": "Flip",
    "flipv": "Flip vertical", "spin": "Spin", "spin3d": "Spin+", "zoom": "Zoom blast",
    "ripple": "Ripple", "sparkle": "Sparkle", "neon": "Neon border", "colorflash": "Colour flash",
    "sink": "Press-in", "flash": "Flash",
}


def press_frames(face: Image.Image, name: str = "bounce") -> list[Image.Image]:
    """Frames for a key-press animation; `name` selects the style (see PRESS_ANIMS)."""
    face = face.convert("RGB")
    bg = _bg_sample(face)
    return PRESS_ANIMS.get(name, _anim_bounce)(face, bg)


def _ease_io(t):
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2     # easeInOutCubic


def page_swipe_frames(old_faces, new_faces, direction, frames=9):
    """A smartphone-style horizontal page swipe across the 2x3 key grid.

    old_faces / new_faces: 6 RGB 60x60 key images each (row-major: 0-2 top, 3-5 bottom).
    direction: +1 = next (new slides in from the right), -1 = prev (from the left).
    Returns `frames` steps; each step is a list of 6 faces (row-major) to push to the keys.
    """
    w, h = KEY_SIZE
    rev = direction < 0
    old = [f.convert("RGB") for f in old_faces]
    new = [f.convert("RGB") for f in new_faces]

    def strip(row_old, row_new):
        seq = (row_new + row_old) if rev else (row_old + row_new)       # 6 images wide
        s = Image.new("RGB", (w * 6, h), (0, 0, 0))
        for i, img in enumerate(seq):
            s.paste(img, (i * w, 0))
        return s

    rows = (strip(old[0:3], new[0:3]), strip(old[3:6], new[3:6]))
    span = 3 * w                                                        # one page width
    out = []
    for fr in range(frames):
        t = _ease_io(fr / (frames - 1))
        p = int((1 - t) * span) if rev else int(t * span)
        faces = []
        for row_strip in rows:
            for c in range(3):
                x = p + c * w
                faces.append(row_strip.crop((x, 0, x + w, h)))
        out.append(faces)
    return out


def _ft_zoom(old, new, opening, frames):
    """Contents grow out of a point (open) / shrink away revealing the page (close)."""
    out = []
    for fr in range(1, frames + 1):
        t = _ease_io(fr / frames)
        faces = []
        for i in range(len(old)):
            if opening:
                base, mover, a, s = old[i], new[i], t, 0.35 + 0.65 * t
            else:
                base, mover, a, s = new[i], old[i], 1.0 - t, 1.0 - 0.65 * t
            scaled = _scaled_face(mover, max(0.05, s), (0, 0, 0))
            faded = Image.blend(base, Image.new("RGB", base.size, (0, 0, 0)), 0.45 * a)
            faces.append(Image.blend(faded, scaled, a))
        out.append(faces)
    return out


def _ft_fade(old, new, opening, frames):
    """A simple cross-dissolve between the page and the folder."""
    return [[Image.blend(old[i], new[i], _ease_io(fr / frames)) for i in range(len(old))]
            for fr in range(1, frames + 1)]


def _ft_slide(old, new, opening, frames):
    """Contents push in horizontally (open from the right, close back to the left)."""
    w, h = old[0].size
    dirn = 1 if opening else -1
    out = []
    for fr in range(1, frames + 1):
        t = _ease_io(fr / frames)
        faces = []
        for i in range(len(old)):
            c = Image.new("RGB", (w, h), (0, 0, 0))
            c.paste(old[i], (int(-dirn * t * w), 0))
            c.paste(new[i], (int(dirn * (1 - t) * w), 0))
            faces.append(c)
        out.append(faces)
    return out


def _ft_flip(old, new, opening, frames):
    """A card flip: squash the page to a sliver, then unfold the folder."""
    w, h = old[0].size
    out = []
    for fr in range(1, frames + 1):
        t = fr / frames
        faces = []
        for i in range(len(old)):
            src, s = (old[i], 1.0 - 2 * t) if t < 0.5 else (new[i], 2 * t - 1.0)
            nw = max(1, int(w * abs(s)))
            c = Image.new("RGB", (w, h), (0, 0, 0))
            c.paste(src.resize((nw, h), Image.LANCZOS), ((w - nw) // 2, 0))
            faces.append(c)
        out.append(faces)
    return out


def _ft_drop(old, new, opening, frames):
    """Contents drop in from the top (open) / fall away downward (close)."""
    w, h = old[0].size
    out = []
    for fr in range(1, frames + 1):
        t = _ease_io(fr / frames)
        faces = []
        for i in range(len(old)):
            c = Image.new("RGB", (w, h), (0, 0, 0))
            if opening:
                c.paste(old[i], (0, 0))
                c.paste(new[i], (0, int(-(1 - t) * h)))
            else:
                c.paste(new[i], (0, 0))
                c.paste(old[i], (0, int(t * h)))
            faces.append(c)
        out.append(faces)
    return out


def expand_gen(old_faces, new_faces, opening, frames, src):
    """iPhone-style folder open/close as a LAZY per-frame generator (avoids a ~100ms upfront
    stall): the folder preview tile grows out of the pressed key and cross-fades into the full
    contents (open), reversed on close. Returns (n, frame_fn) where frame_fn(i) -> 6 RGB faces.
    """
    old = [f.convert("RGB") for f in old_faces]
    new = [f.convert("RGB") for f in new_faces]
    ss = 2
    kw, kh = old[0].size
    bk_w, bk_h = kw * ss, kh * ss
    gap = max(2, bk_w // 8)
    cols = 3
    pw, ph = cols * bk_w + (cols - 1) * gap, 2 * bk_h + gap

    def panel(faces):
        p = Image.new("RGB", (pw, ph), (0, 0, 0))
        for i, f in enumerate(faces):
            r, c = divmod(i, cols)
            p.paste(f.resize((bk_w, bk_h), Image.LANCZOS), (c * (bk_w + gap), r * (bk_h + gap)))
        return p

    src = max(0, min(5, src))
    sr, sc = divmod(src, cols)
    kx0, ky0 = sc * (bk_w + gap), sr * (bk_h + gap)
    kx1, ky1 = kx0 + bk_w, ky0 + bk_h
    page = panel(old if opening else new)            # built once, reused for every frame
    content = panel(new if opening else old)
    tile = (old[src] if opening else new[src]).resize((bk_w, bk_h), Image.LANCZOS)
    black = Image.new("RGB", (pw, ph), (0, 0, 0))

    def frame(i):
        u = _ease_io((i + 1) / frames)
        t = u if opening else (1.0 - u)              # t: 0 = at the key (icon), 1 = full
        x0, y0 = kx0 * (1 - t), ky0 * (1 - t)
        x1, y1 = kx1 + (pw - kx1) * t, ky1 + (ph - ky1) * t
        bw, bh = max(1, int(x1 - x0)), max(1, int(y1 - y0))
        f = max(0.0, min(1.0, (t - 0.22) / 0.6))
        f = f * f * (3 - 2 * f)                      # smoothstep icon -> contents cross-fade
        layer = Image.blend(tile.resize((bw, bh), Image.LANCZOS),
                            content.resize((bw, bh), Image.LANCZOS), f)
        comp = Image.blend(page, black, 0.5 * t)
        comp.paste(layer, (int(x0), int(y0)))
        faces = []
        for k in range(6):
            r, c = divmod(k, cols)
            x, y = c * (bk_w + gap), r * (bk_h + gap)
            faces.append(comp.crop((x, y, x + bk_w, y + bk_h)).resize((kw, kh), Image.LANCZOS))
        return faces

    return frames, frame


def _ft_expand(old, new, opening, frames, src):
    n, frame = expand_gen(old, new, opening, frames, src)
    return [frame(i) for i in range(n)]


FOLDER_ANIMS = {"zoom": _ft_zoom, "slide": _ft_slide, "flip": _ft_flip,
                "fade": _ft_fade, "drop": _ft_drop}
FOLDER_ANIM_ORDER = ["zoom", "expand", "slide", "flip", "fade", "drop"]
FOLDER_ANIM_LABELS = {"zoom": "Zoom", "expand": "Expand (iPhone)", "slide": "Slide",
                      "flip": "Flip", "fade": "Fade", "drop": "Drop in"}


def folder_transition_frames(old_faces, new_faces, opening, name="zoom", frames=10, src=0):
    """Open/close transition for a folder across all 6 keys; `name` selects the style.
    `src` is the pressed key index (used by the 'expand' style).

    Returns `frames` steps; each step is a list of 6 RGB faces (row-major).
    """
    old = [f.convert("RGB") for f in old_faces]
    new = [f.convert("RGB") for f in new_faces]
    if name == "expand":
        return _ft_expand(old, new, opening, frames, src)
    return FOLDER_ANIMS.get(name, _ft_zoom)(old, new, opening, frames)


def slice_fullscreen(path: str, dest_dir: str, tag: str = "fs", gap: int = 22):
    """Slice one image across the 2x3 LCD grid -> 6 tile paths (key1..key6, row-major).

    `gap` approximates the physical bezel gap so the picture looks continuous across keys.
    """
    os.makedirs(dest_dir, exist_ok=True)
    kw, kh = KEY_SIZE
    total = (3 * kw + 2 * gap, 2 * kh + gap)
    # pad (contain), not fit (cover): show the whole image; never crop a non-square one.
    src = ImageOps.pad(Image.open(path).convert("RGB"), total, color=(0, 0, 0),
                       centering=(0.5, 0.5))
    paths = []
    for r in range(2):
        for c in range(3):
            x, y = c * (kw + gap), r * (kh + gap)
            tile = src.crop((x, y, x + kw, y + kh))
            p = os.path.join(dest_dir, f"_{tag}_{r}{c}.png")
            tile.save(p)
            paths.append(p)
    return paths
