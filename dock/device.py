"""Low-level driver for the Ajazz AKP03E rev.2 stream controller.

USB VID 0x0300 / PID 0x3002, vendor HID interface (usage_page 0xFFA0, usage 1).

Wire protocol (source: 4ndv/mirajazz, 4ndv/opendeck-akp03), proto version 3:

  * Output reports, report-ID 0x00, frame length 1025 bytes (1 + 1024 payload).
  * Every command frame is:  00 "CRT" 00 00 <3-byte opcode> <params...>  zero-padded.
  * Input reports are 512 bytes, header ASCII "ACK", data[9]=code, data[10]=state.

Physical inputs: 6 LCD keys (codes 1..6) + 3 plain buttons (0x25/0x30/0x31)
+ 3 rotary encoders (twist 0x90/91, 0x50/51, 0x60/61 ; push 0x33/0x35/0x34).
LCD keys render a 60x60 JPEG, rotated 90deg.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

import hid
from PIL import Image

VID = 0x0300
PID = 0x3002
VENDOR_USAGE_PAGE = 0xFFA0

REPORT_LEN = 1025          # 1 report-id byte + 1024 payload
PAYLOAD_CHUNK = 1024       # image data chunk size (proto v3)

LCD_KEYS = 6               # number of LCD display keys (host indices 0..5)
KEY_SIZE = (88, 88)        # per-key native tile (w, h); the physical cell is taller than wide,
                           # so width/height are tuned independently in the display calibrator.
                           # Bigger than the cell spills into neighbour keys (the 6 are one panel).
JPEG_QUALITY = 90
EDGE_INSET = 2             # px black frame: keep content off the device's ragged screen edge
# The firmware anchors each key image to the TOP-RIGHT with a fixed dark margin on the
# left/bottom (measured on-device). Shift the content down-left so it sits visually centred
# with a symmetric border (tunable). paste-offset (x, y): negative x = left, positive y = down.
CONTENT_SHIFT = (-6, 8)

# ---- input event codes (data[9]) -------------------------------------------
_LCD_CODES = {0x01: 0, 0x02: 1, 0x03: 2, 0x04: 3, 0x05: 4, 0x06: 5}
_BUTTON_CODES = {0x25: 6, 0x30: 7, 0x31: 8}          # round buttons -> index 6,7,8
_ENC_TURN = {                                         # code -> (encoder, delta)
    0x90: (0, -1), 0x91: (0, +1),
    0x50: (1, -1), 0x51: (1, +1),
    0x60: (2, -1), 0x61: (2, +1),
}
_ENC_PUSH = {0x33: 0, 0x35: 1, 0x34: 2}              # note: non-sequential


@dataclass
class Event:
    """A decoded input event from the device."""
    kind: str                 # 'key' | 'button' | 'encoder_push' | 'encoder_turn' | 'release_all'
    index: int = -1           # key 0..5 | button 6..8 | encoder 0..2
    pressed: bool = False      # for key/button/encoder_push
    delta: int = 0            # for encoder_turn: +1 / -1
    raw_code: int = 0
    remote: bool = False      # injected by a mirror client (Orbit), not the dock: never accelerated

    @property
    def input_id(self) -> str:
        """Canonical binding id, e.g. 'key1', 'btn7', 'enc0', 'enc0+', 'enc0-'."""
        if self.kind == "key":
            return f"key{self.index + 1}"
        if self.kind == "button":
            return f"btn{self.index + 1}"
        if self.kind == "encoder_push":
            return f"enc{self.index}"
        if self.kind == "encoder_turn":
            return f"enc{self.index}{'+' if self.delta > 0 else '-'}"
        return "none"


def encode_key_image(img: Image.Image, rotation: int = 90, mirror: bool = False,
                     quality: int = 95, subsampling: int = 0,
                     size=None, shift=None, inset=None) -> bytes:
    """Convert a PIL image to the AKP03 on-wire JPEG (rotated 90deg).

    `size`/`shift`/`inset` override the module KEY_SIZE / CONTENT_SHIFT / EDGE_INSET (used by the
    live calibrator); normal rendering passes None and uses the current calibrated module values.

    The resulting JPEG is cached ON the source image object (keyed by geometry + quality), so a
    cached STATIC face (see images._FACE_CACHE) re-uploaded on a page flip isn't re-encoded. Live /
    animated faces are freshly rendered each frame → they simply never hit and their cache dies with
    them: no global cache to grow, no id()-reuse hazard, auto-invalidated when the face re-renders.
    """
    sz = size or KEY_SIZE
    sh = CONTENT_SHIFT if shift is None else shift
    ins = EDGE_INSET if inset is None else inset
    ckey = (rotation, bool(mirror), quality, subsampling, tuple(sz), tuple(sh), ins)
    src = img                                            # the (possibly cache-shared) source object
    _jc = getattr(src, "_jpeg_cache", None)
    if _jc is not None:
        hit = _jc.get(ckey)
        if hit is not None:
            return hit
    w, h = sz
    img = img.convert("RGB")
    if img.size != sz:
        img = img.resize(sz, Image.LANCZOS)
    if ins > 0:
        # Keep content off the device's ragged screen edge: scale the face down UNIFORMLY
        # (never per-axis — that distorts the aspect ratio) and centre it in a black tile.
        f = min((w - 2 * ins) / w, (h - 2 * ins) / h)
        nw, nh = max(1, round(w * f)), max(1, round(h * f))
        framed = Image.new("RGB", sz, (0, 0, 0))
        framed.paste(img.resize((nw, nh), Image.LANCZOS), ((w - nw) // 2, (h - nh) // 2))
        img = framed
    if sh != (0, 0):
        # Pure TRANSLATE (no scaling -> aspect preserved) to counter the firmware's corner
        # anchor / nudge centring. This actually MOVES the content (X/Y are a fine nudge);
        # use Width/Height to fill the cell — that's what reaches the bottom edge.
        shifted = Image.new("RGB", sz, (0, 0, 0))
        shifted.paste(img, sh)
        img = shifted
    if mirror:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if rotation:
        # Exact lossless rotation (transpose) — img.rotate() resamples and left a
        # ragged 1px edge. Rot90 clockwise == ROTATE_270.
        tmap = {90: Image.ROTATE_270, 180: Image.ROTATE_180, 270: Image.ROTATE_90}
        img = img.transpose(tmap[rotation]) if rotation in tmap else img.rotate(-rotation, expand=True)
    buf = io.BytesIO()
    # Default quality 95 + 4:4:4 chroma: crisp edges on text/solids. Animation frames pass a
    # lower quality + chroma subsampling so the JPEG is much smaller -> faster transfer -> higher fps.
    img.save(buf, format="JPEG", quality=quality, subsampling=subsampling)
    out = buf.getvalue()
    try:                                                 # stash on the source face; a fresh face has
        if _jc is None:                                  # no cache yet, a shared static face reuses it
            _jc = src._jpeg_cache = {}
        _jc[ckey] = out
    except (AttributeError, TypeError):
        pass                                             # exotic image object that won't take an attr
    return out


def encode_hi(img: Image.Image, quality: int = 90, subsampling: int = 0) -> bytes:
    """A face's 2x mirror copy (images: img._mirror_hi) as a plain JPEG for Orbit: unrotated, no
    calibration shift/inset. Cached on the image like encode_key_image, so a cached static face
    shown again hands the mirror the very same bytes object (= unchanged, nothing re-sent)."""
    ckey = ("hi", quality, subsampling)
    _jc = getattr(img, "_jpeg_cache", None)
    if _jc is not None:
        hit = _jc.get(ckey)
        if hit is not None:
            return hit
    buf = io.BytesIO()
    (img if img.mode == "RGB" else img.convert("RGB")).save(buf, format="JPEG", quality=quality,
                                                            subsampling=subsampling)
    out = buf.getvalue()
    try:
        if _jc is None:
            _jc = img._jpeg_cache = {}
        _jc[ckey] = out
    except (AttributeError, TypeError):
        pass
    return out


class AKP03:
    """Connection to a single AKP03E device."""

    def __init__(self) -> None:
        self.dev: Optional[hid.device] = None
        self._initialized = False
        self.image_rotation = 90
        self.image_mirror = False
        # Frame tap for dock/mirror.py (Orbit's VR panel): the JPEGs the keys show, exactly as sent,
        # committed by flush(). One tuple swap per flush, so another thread reads the old or the new
        # set, never half of each. None = a cleared key. (gen, device JPEGs, 2x JPEGs): the 2x copy
        # is only made while mirror_hi is on (a client asked for it), else None = use the device one.
        self._mirror_all = (0, (None,) * LCD_KEYS, (None,) * LCD_KEYS)
        self.mirror_hi = False                    # set on the loop thread (controller.set_mirror_hi)
        self.mirror_dark = False                  # sleep() turned the backlight off
        self._mx_pending: dict = {}               # key -> (device JPEG, 2x JPEG | None)

    @property
    def mirror(self):
        """(gen, device JPEGs) - the api-1 view of the tap."""
        return self._mirror_all[:2]

    @property
    def mirror_full(self):
        """(gen, device JPEGs, 2x JPEGs) in one atomic read."""
        return self._mirror_all

    # ---- connection --------------------------------------------------------
    @staticmethod
    def find_path() -> Optional[bytes]:
        for d in hid.enumerate(VID, PID):
            if d.get("usage_page") == VENDOR_USAGE_PAGE or d.get("interface_number") == 0:
                return d["path"]
        return None

    @staticmethod
    def is_present() -> bool:
        return AKP03.find_path() is not None

    def open(self) -> "AKP03":
        path = self.find_path()
        if not path:
            raise RuntimeError("AKP03 device not found (VID 0x0300 / PID 0x3002).")
        self.dev = hid.device()
        self.dev.open_path(path)
        return self

    def close(self) -> None:
        if self.dev:
            try:
                self.dev.close()
            finally:
                self.dev = None
                self._initialized = False

    def __enter__(self) -> "AKP03":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- frame builders ----------------------------------------------------
    @staticmethod
    def _frame(payload: bytes) -> bytes:
        buf = bytearray(REPORT_LEN)        # zero-filled, leading byte 0x00 = report id
        buf[1:1 + len(payload)] = payload
        return bytes(buf)

    @classmethod
    def _cmd(cls, opcode: bytes, params: bytes = b"") -> bytes:
        return cls._frame(b"CRT\x00\x00" + opcode + params)

    def _write(self, frame: bytes) -> None:
        if not self.dev:
            raise OSError("device not open")   # OSError (not RuntimeError) so the loop's reconnect path catches it
        self.dev.write(frame)

    # ---- protocol ----------------------------------------------------------
    def _ensure_init(self) -> None:
        if self._initialized:
            return
        self._write(self._cmd(b"DIS"))                       # display on
        self._write(self._cmd(b"LIG", bytes([0x00, 0x00, 0])))  # brightness 0
        self._initialized = True

    def set_brightness(self, percent: int) -> None:
        self._ensure_init()
        percent = max(0, min(100, int(percent)))
        self._write(self._cmd(b"LIG", bytes([0x00, 0x00, percent])))

    def sleep(self) -> None:
        """Put the panel into TRUE sleep — backlight fully off — via the HAN opcode (proto v3,
        source mirajazz Device::sleep). Deeper than brightness 0, which on this panel only dims to
        a minimum glow. The MCU stays alive (this is NOT shutdown), so input events keep flowing and
        a key/button press still reaches the app; call wake() (or any render) to bring it back."""
        self._ensure_init()
        self._write(self._cmd(b"HAN"))
        self.mirror_dark = True

    def wake(self) -> None:
        """Re-assert display power (DIS) after sleep(). The caller restores brightness + re-renders."""
        self._ensure_init()
        self._write(self._cmd(b"DIS"))
        self.mirror_dark = False

    def set_key_image(self, key: int, jpeg: bytes, hi: Optional[bytes] = None) -> None:
        """Upload a JPEG (from encode_key_image) to LCD key 0..5. Call flush() after. `hi` is only
        for the mirror tap (the key's 2x JPEG); the device never sees it."""
        if not 0 <= key < LCD_KEYS:
            raise ValueError(f"key out of range: {key}")
        self._ensure_init()
        n = len(jpeg)
        header = bytes([0x00, 0x00, (n >> 8) & 0xFF, n & 0xFF, key + 1])
        self._write(self._cmd(b"BAT", header))
        for off in range(0, n, PAYLOAD_CHUNK):
            chunk = jpeg[off:off + PAYLOAD_CHUNK]
            buf = bytearray(REPORT_LEN)
            buf[1:1 + len(chunk)] = chunk
            self._write(bytes(buf))
        self._mx_pending[key] = (jpeg, hi)

    def set_key_pil(self, key: int, img: Image.Image,
                    quality: int = 95, subsampling: int = 0,
                    size=None, shift=None) -> None:
        jpeg = encode_key_image(img, self.image_rotation, self.image_mirror, quality, subsampling,
                                size, shift)
        self.set_key_image(key, jpeg)
        # The mirror's 2x copy is encoded AFTER the USB write (device latency unchanged), on this
        # same loop thread; only bytes leave it. Animation-quality writes get a lighter 2x JPEG.
        src = getattr(img, "_mirror_hi", None) if self.mirror_hi else None
        if src is not None:
            try:
                q, ss = (90, 0) if quality >= 90 else (75, 2)
                self._mx_pending[key] = (jpeg, encode_hi(src, q, ss))
            except Exception:
                pass                                     # no 2x copy -> Orbit shows the device JPEG

    def clear_key(self, key: int) -> None:
        self._ensure_init()
        self._write(self._cmd(b"CLE", bytes([0x00, 0x00, 0x00, key + 1])))
        self._mx_pending[key] = (None, None)

    def clear_all(self) -> None:
        self._ensure_init()
        self._write(self._cmd(b"CLE", bytes([0x00, 0x00, 0x00, 0xFF])))
        for k in range(LCD_KEYS):
            self._mx_pending[k] = (None, None)

    def flush(self) -> None:
        """Commit written images to the screen (required)."""
        self._write(self._cmd(b"STP"))
        if self._mx_pending:
            gen, keys, his = self._mirror_all
            keys, his = list(keys), list(his)
            for k, (jpeg, hi) in self._mx_pending.items():
                keys[k], his[k] = jpeg, hi
            self._mx_pending = {}
            self._mirror_all = (gen + 1, tuple(keys), tuple(his))

    # ---- input -------------------------------------------------------------
    def read_event(self, timeout_ms: int = 200) -> Optional[Event]:
        if not self.dev:
            raise OSError("device not open")   # OSError (not RuntimeError) so the loop's reconnect path catches it
        data = self.dev.read(512, timeout_ms)
        if not data or len(data) < 11:
            return None
        if bytes(data[0:3]) != b"ACK":
            return None
        code = data[9]
        state = data[10]
        if code == 0x00:
            return Event(kind="release_all", raw_code=code)
        if code in _LCD_CODES:
            return Event(kind="key", index=_LCD_CODES[code], pressed=bool(state), raw_code=code)
        if code in _BUTTON_CODES:
            return Event(kind="button", index=_BUTTON_CODES[code], pressed=bool(state), raw_code=code)
        if code in _ENC_PUSH:
            return Event(kind="encoder_push", index=_ENC_PUSH[code], pressed=bool(state), raw_code=code)
        if code in _ENC_TURN:
            enc, delta = _ENC_TURN[code]
            return Event(kind="encoder_turn", index=enc, delta=delta, raw_code=code)
        return Event(kind="unknown", raw_code=code)
