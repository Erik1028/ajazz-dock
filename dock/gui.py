"""Native PySide6 desktop app for the Ajazz AKP03 — no web tech.

A real Qt window + system tray that drives the verified backend
(dock.device / dock.actions / dock.controller) directly, in-process.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading

from PySide6.QtCore import (Qt, QObject, QEvent, Signal, QTimer, QSize, QRect, QRectF, QPoint,
                            QPointF, QVariantAnimation, QEasingCurve, QAbstractAnimation, QMimeData,
                            QPropertyAnimation, QParallelAnimationGroup)
from PySide6.QtGui import (QPixmap, QImage, QIcon, QColor, QAction, QFont, QFontDatabase,
                           QKeySequence, QPainter, QPen, QLinearGradient, QDrag, QPainterPath,
                           QShortcut, QRegion)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PIL import Image
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QToolButton, QLineEdit,
    QComboBox, QSpinBox, QSlider, QColorDialog, QFileDialog, QPlainTextEdit, QGridLayout,
    QHBoxLayout, QVBoxLayout, QFormLayout, QGroupBox, QScrollArea, QSystemTrayIcon, QMenu,
    QSizePolicy, QFrame, QInputDialog, QMessageBox, QButtonGroup, QCheckBox, QKeySequenceEdit,
    QDialog, QListWidget, QListWidgetItem, QGraphicsOpacityEffect, QGraphicsDropShadowEffect,
    QToolTip, QStackedWidget, QSplitter,
)

from . import appicon, autostart, backups
from . import images
from . import tokens as T
from .config import Config, LCD_KEYS, config_dir, backups_dir
from .controller import DockController
from .iconart import icon_image
from .actionart import action_art
from . import live as livesrc
from .images import (render_face, folder_face, live_face, panel_frames, slice_fullscreen,
                     effective_fit, emoji_image, has_swatch, DEFAULT_BG,
                     PRESS_ANIM_ORDER, PRESS_ANIM_LABELS,
                     FOLDER_ANIM_ORDER, FOLDER_ANIM_LABELS, LIVE_STYLE_ORDER, LIVE_STYLE_LABELS)
from .emoji_data import CATEGORIES
from .ui_toast import Toast

APP_TITLE = "Hexpad"                     # the DISPLAY name (window title, tray, header, icon)
# NB: the config dir, single-instance IPC/mutex, QSettings, exe filename, install path and the
# autostart task all deliberately keep the old "AjazzDock" id so existing settings/installs survive.
IPC_NAME = "AjazzDock_ipc_v1"


class _WheelGuard(QObject):
    """App-wide filter: the mouse wheel must NEVER change a combo/spin/slider's
    value just because the cursor is hovering over it. Scrolling is for moving
    the panel, not silently editing the selected action — an easy, infuriating
    mistake. We only let the wheel change a value when the widget actually has
    keyboard focus (i.e. the user clicked into it on purpose). Otherwise we eat
    the event and re-deliver it to the enclosing QScrollArea so the inspector
    still scrolls as expected.
    """

    _GUARDED = (QComboBox, QSpinBox, QSlider)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Wheel and isinstance(obj, self._GUARDED) and not obj.hasFocus():
            w = obj.parentWidget()
            while w is not None and not isinstance(w, QScrollArea):
                w = w.parentWidget()
            if w is not None:                       # forward the scroll to the panel
                QApplication.sendEvent(w.viewport(), ev)
            return True                             # ...and block the value change
        return False


class _HScroll(QScrollArea):
    """A scroll area whose mouse wheel scrolls HORIZONTALLY — used for the bottom inspector row,
    which lays its editor cards out left-to-right (a normal vertical wheel would do nothing)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().installEventFilter(self)

    def _wheel_to_h(self, ev):
        d = ev.angleDelta()
        step = d.y() if d.y() else d.x()
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - step)

    def wheelEvent(self, ev):                       # wheel over a card's background bubbles up here
        self._wheel_to_h(ev)
        ev.accept()

    def eventFilter(self, obj, ev):                 # wheel over a combo/slider is forwarded here
        if obj is self.viewport() and ev.type() == QEvent.Wheel:
            self._wheel_to_h(ev)
            return True
        return super().eventFilter(obj, ev)


# Hold the singleton mutex handle for the whole process lifetime (module global so it is never GC'd).
_SINGLETON_HANDLE = None


def _acquire_singleton() -> bool:
    """Return True iff we are the FIRST/only AjazzDock instance (Windows named mutex).

    A QLocalSocket probe-then-listen guard is a TOCTOU race: if autostart (--tray) and a manual
    launch start within the same window, BOTH can become primaries, BOTH open the (non-exclusive)
    HID device, and fight over the panel — which looks exactly like the calibration pattern
    'sticking'. An OS mutex is atomic, so only one process is ever primary.
    """
    global _SINGLETON_HANDLE
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\AjazzDock_singleton_v1")
        if not handle:
            return True                       # can't create a mutex -> don't block startup
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _SINGLETON_HANDLE = handle            # keep it alive for the process lifetime
        return True
    except Exception:
        return True                           # never let the guard itself prevent launching

ACTION_TYPES = ["none", "open", "hotkey", "text", "media", "volume", "appvolume", "mic", "sound",
                "discord", "substance", "quick", "system", "monitor", "smartlight", "rgbscene", "obs",
                "page", "folder", "profile", "brightness", "macro", "toggle", "http", "timer"]
# Prisma effect names (must match its CLI's TryParseEffect aliases).
_RGB_EFFECTS = ["Rainbow", "Static", "ColorCycle", "Breathing", "Strobe", "Gradient",
                "Comet", "Twinkle", "Ambient", "Fire", "Music", "Off"]
_RGB_MODES = [("Solid colour", "color"),
              ("Colour — cycle ▲", "hue_up"),
              ("Colour — cycle ▼", "hue_down"),
              ("Effect", "effect"), ("Saved profile", "profile"),
              ("Brightness — set", "bright_set"),
              ("Brightness — up ▲", "bright_up"),
              ("Brightness — down ▼", "bright_down"),
              ("Toggle on / off", "toggle"), ("All off", "off")]
# Quick actions — common Windows tasks as one-press presets (label -> op key).
_QUICK_OPS = [
    ("Empty Recycle Bin", "recycle_empty"),
    ("Open Recycle Bin", "recycle_open"),
    ("Show / hide desktop", "show_desktop"),
    ("Minimize all windows", "minimize_all"),
    ("Task Manager", "task_manager"),
    ("File Explorer", "explorer"),
    ("Windows Settings", "settings"),
    ("Run dialog", "run_dialog"),
    ("Screenshot snip", "snip"),
    ("Clipboard history", "clipboard_history"),
    ("Clear clipboard", "clipboard_clear"),
    ("Emoji panel", "emoji_panel"),
    ("Project / 2nd screen", "project"),
    ("Lock PC", "lock"),
]
_QUICK_LABEL = {op: label for label, op in _QUICK_OPS}
_QUICK_HINTS = {
    "recycle_empty": "⚠ Permanently empties the Recycle Bin — no confirmation, can't be undone.",
    "snip": "Opens the Snipping Bar to grab a region to the clipboard (Win+Shift+S).",
    "clipboard_clear": "Empties the Windows clipboard.",
    "clipboard_history": "Opens clipboard history (Win+V).",
    "project": "Opens the projection panel — duplicate / extend / second screen (Win+P).",
    "settings": "Opens the Windows Settings app.",
    "task_manager": "Opens Task Manager (Ctrl+Shift+Esc).",
}
# Substance 3D Painter operations -> the shortcut they send (verified defaults; "" = custom).
_SUBSTANCE_OPS = [
    ("Brush size  +", "]"), ("Brush size  −", "["),
    ("Paint tool", "1"), ("Eraser", "2"), ("Projection", "3"),
    ("Polygon fill", "4"), ("Smudge", "5"), ("Clone", "6"),
    ("Symmetry on / off", "l"),
    ("Undo", "ctrl+z"), ("Redo", "ctrl+y"), ("Save project", "ctrl+s"),
    ("Hide UI", "tab"), ("Perspective view", "f5"), ("Orthographic view", "f6"),
    ("Material mode", "m"), ("Center on mesh", "f"),
    ("Custom (assign in Painter)…", ""),
]
_FOLDER_BACK = {"label": "Back", "icon": "⬅️", "color": "#222a33"}

QSS = T.build_qss()


def _dpr(widget=None) -> float:
    """The device-pixel-ratio to render pixmaps at, so icons stay crisp under Windows
    display scaling (a 1x pixmap on a 150% monitor gets stretched blurry otherwise)."""
    try:
        if widget is not None:
            return max(1.0, float(widget.devicePixelRatioF()))
        scr = QApplication.primaryScreen()
        return max(1.0, float(scr.devicePixelRatio())) if scr is not None else 1.0
    except Exception:
        return 1.0


def pil_to_pixmap(img, dpr: float = 0.0) -> QPixmap:
    img = img.convert("RGBA")
    w, h = img.size
    qimg = QImage(img.tobytes("raw", "RGBA"), w, h, QImage.Format_RGBA8888).copy()
    pm = QPixmap.fromImage(qimg)
    if dpr and dpr > 1.0:
        pm.setDevicePixelRatio(dpr)      # the PIL source was rendered dpr× large; shown at logical size
    return pm


def face_pixmap(pil_img, logical_px: int, widget=None) -> QPixmap:
    """A key-face pixmap scaled to `logical_px` device-independent pixels at the monitor's
    real pixel density — sharp where a plain .scaled(logical_px) would be upscaled by Windows."""
    dpr = _dpr(widget)
    px = max(2, int(round(logical_px * dpr)))
    pm = pil_to_pixmap(pil_img).scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    return pm


_FLUENT_FAMILY = None


def _fluent_family() -> str:
    """The installed Segoe icon-font family ('' when neither the Win11 nor Win10 one exists)."""
    global _FLUENT_FAMILY
    if _FLUENT_FAMILY is None:
        fams = set(QFontDatabase.families())
        _FLUENT_FAMILY = ("Segoe Fluent Icons" if "Segoe Fluent Icons" in fams else
                          "Segoe MDL2 Assets" if "Segoe MDL2 Assets" in fams else "")
    return _FLUENT_FAMILY


_GLYPH_PM_CACHE: dict = {}


def _glyph_pm(name: str, color: str, size: int, dpr: float):
    """One Segoe Fluent glyph as a DPR-tagged pixmap, or None if the glyph/font is missing."""
    cp = images.FLUENT_ICONS.get(name)
    fam = _fluent_family()
    if cp is None or not fam:
        return None
    key = (name, color, size, round(dpr * 4))
    pm = _GLYPH_PM_CACHE.get(key)
    if pm is not None:
        return pm
    px = max(2, int(round(size * dpr)))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    f = QFont(fam)
    f.setPixelSize(max(2, int(round(px * 0.92))))     # fluent glyphs sit inside the em square
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(QRect(0, 0, px, px), Qt.AlignCenter, chr(cp))
    p.end()
    pm.setDevicePixelRatio(dpr)
    _GLYPH_PM_CACHE[key] = pm
    return pm


def fluent_qicon(name, normal=None, active=None, disabled=None, size=18, widget=None):
    """A QIcon of one Segoe Fluent glyph with hover/highlight + disabled colours baked in.
    Returns None when unavailable so callers can keep a text fallback."""
    dpr = _dpr(widget)
    nrm = _glyph_pm(name, normal or T.TEXT_DIM, size, dpr)
    if nrm is None:
        return None
    ic = QIcon()
    ic.addPixmap(nrm, QIcon.Normal)
    act = _glyph_pm(name, active or T.ACCENT_INK, size, dpr)
    ic.addPixmap(act, QIcon.Active)       # highlighted menu row / auto-raise hover
    ic.addPixmap(act, QIcon.Selected)     # selected nav / list row
    ic.addPixmap(_glyph_pm(name, disabled or T.TEXT_FAINT, size, dpr), QIcon.Disabled)
    return ic


def menu_icon(name, widget=None):
    """Context-menu item icon: muted at rest, ink-on-accent while the row is highlighted."""
    return fluent_qicon(name, normal=T.TEXT_DIM, active=T.ACCENT_INK, size=16, widget=widget)


def _preset_qicon(binding, widget=None):
    """A crisp menu icon for a starter preset: its fluent icon if it has one, else its emoji
    rendered through PIL at the real pixel density (emoji as button TEXT would blur)."""
    ic = (binding.get("icon") or "").strip()
    if ic.startswith("fluent:"):
        return menu_icon(ic[7:], widget)
    if ic:
        dpr = _dpr(widget)
        em = emoji_image(ic, int(round(16 * dpr)))
        if em is not None:
            return QIcon(pil_to_pixmap(em, dpr))
    return None


class GlyphButton(QToolButton):
    """A QToolButton whose face is a crisp Fluent glyph that tints to the accent on hover
    (falls back to the given text glyph if the icon font is unavailable)."""

    def __init__(self, glyph, fallback_text, tooltip="", object_name="hdriconbtn",
                 size=17, normal=None, hover=None):
        super().__init__(objectName=object_name)
        self.setCursor(Qt.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)
        n, h = normal or T.TEXT_DIM, hover or T.ACCENT
        self._ic_n = fluent_qicon(glyph, normal=n, active=h, size=size, widget=self)
        self._ic_h = fluent_qicon(glyph, normal=h, active=h, size=size, widget=self) \
            if self._ic_n is not None else None
        if self._ic_n is not None:
            self.setIcon(self._ic_n)
            self.setIconSize(QSize(size, size))
        else:
            self.setText(fallback_text)

    def enterEvent(self, ev):
        if self._ic_h is not None and self.isEnabled():
            self.setIcon(self._ic_h)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        if self._ic_n is not None:
            self.setIcon(self._ic_n)
        super().leaveEvent(ev)


def app_icon() -> QIcon:
    ic = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(pil_to_pixmap(icon_image(s)))
    return ic


ACTION_LABELS = {
    "none": "Nothing", "open": "Open app / file / URL", "hotkey": "Keyboard shortcut",
    "text": "Type text", "media": "Media control", "volume": "System volume",
    "appvolume": "App volume (mixer)", "mic": "Microphone mute", "sound": "Play sound (soundboard)", "discord": "Discord mute / deafen",
    "system": "System (lock / sleep)", "monitor": "Monitor brightness", "page": "Switch page",
    "folder": "Open folder", "profile": "Switch profile", "brightness": "Dock screen brightness",
    "macro": "Macro (advanced)", "substance": "Substance 3D Painter",
    "quick": "Quick action", "smartlight": "Smart light (Tapo / Lumos)",
    "rgbscene": "RGB scene (Prisma)", "obs": "OBS Studio", "toggle": "Toggle (2 states)",
    "http": "HTTP request (webhook)", "timer": "Timer / countdown",
}
_SHORT = {"none": "No action",
          "open": "Open", "hotkey": "Hotkey", "text": "Text", "media": "Media", "volume": "Volume",
          "appvolume": "App vol", "mic": "Mic", "sound": "Sound", "discord": "Discord",
          "system": "System", "monitor": "Monitors", "page": "Page", "folder": "Folder",
          "profile": "Profile", "brightness": "Brightness", "macro": "Macro", "substance": "Painter",
          "quick": "Quick", "smartlight": "Light", "rgbscene": "RGB", "obs": "OBS", "toggle": "Toggle",
          "http": "HTTP", "timer": "Timer"}
# Compact "what this control does" labels drawn INSIDE the knobs / round buttons on the stage.
_CTRL_SHORT = {"open": "Open", "hotkey": "Key", "text": "Text", "media": "Media",
               "volume": "Volume", "appvolume": "App vol", "mic": "Mic", "sound": "Sound",
               "discord": "Discord", "system": "System", "monitor": "Screen", "page": "Page",
               "folder": "Folder", "profile": "Profile", "brightness": "Bright", "macro": "Macro",
               "substance": "Brush", "quick": "Quick", "smartlight": "Light", "rgbscene": "RGB",
               "obs": "OBS", "http": "HTTP", "timer": "Timer"}

# Icon + one-line description per action, and a category grouping — used by the searchable picker.
ACTION_EMOJI = {
    "none": "🚫", "open": "📂", "hotkey": "⌨️", "text": "🔤", "media": "⏯️", "volume": "🔊",
    "appvolume": "🎚️", "mic": "🎙️", "sound": "🔉", "discord": "💬", "substance": "🎨",
    "quick": "⚡", "system": "🖥️", "monitor": "☀️", "smartlight": "💡", "rgbscene": "🌈", "obs": "🎬",
    "page": "📄", "folder": "📁", "profile": "👤", "brightness": "🔆", "macro": "🧩", "toggle": "🔁",
    "http": "🌐", "timer": "⏱️",
}
ACTION_DESC = {
    "none": "Clear this key — remove its action (do nothing)",
    "open": "Launch a program, file, folder or website",
    "hotkey": "Send a keyboard shortcut or mouse click",
    "text": "Type a snippet of text",
    "media": "Play / pause / next / previous track",
    "volume": "System volume up, down or mute",
    "appvolume": "Volume of the focused app — great on a dial",
    "mic": "Mute / unmute your microphone",
    "sound": "Play a sound clip (soundboard)",
    "discord": "Mute or deafen in Discord",
    "substance": "Substance 3D Painter shortcuts",
    "quick": "Common Windows shortcuts (lock, snip…)",
    "system": "Lock, sleep, or turn the screen off",
    "monitor": "Change your PC monitor brightness",
    "smartlight": "Turn your Tapo bulb on/off or set a colour",
    "rgbscene": "Set an RGB scene or brightness via Prisma",
    "obs": "Switch scene, record, stream or mute in OBS Studio",
    "page": "Switch the dock to another page",
    "folder": "Open a sub-page of keys",
    "profile": "Switch to another profile",
    "brightness": "Change the dock's own screen brightness",
    "macro": "Run a sequence of actions",
    "toggle": "One key, two actions — each press flips to the other",
    "http": "Send a web request (webhook / REST API)",
    "timer": "Countdown or stopwatch, live on the key — tap starts / pauses, beeps at zero",
}
ACTION_CATEGORIES = [
    ("Apps & files", ["open"]),
    ("Keyboard & text", ["hotkey", "text", "substance"]),
    ("Media & sound", ["media", "volume", "appvolume", "mic", "sound"]),
    ("Streaming", ["obs"]),
    ("Smart home & RGB", ["smartlight", "rgbscene"]),
    ("System & Windows", ["system", "monitor", "brightness", "quick", "discord"]),
    ("Dock navigation", ["page", "folder", "profile"]),
    ("Advanced", ["macro", "toggle", "http", "timer"]),
]
COMMON_ACTIONS = ["open", "hotkey", "media", "volume", "appvolume", "mic", "smartlight", "page", "none"]
# One-click starter bindings for an empty LCD key (showcase stateful + smart-home keys).
PRESETS = [
    {"name": "Launch app", "binding": {"label": "App", "icon": "📂", "action": {"type": "open", "target": ""}}},
    {"name": "Mic mute", "binding": {"label": "Mic", "icon": "🎙️", "action": {"type": "mic", "mic": "toggle"}, "live": {"source": "mic"}}},
    {"name": "Play / Pause", "binding": {"label": "Play", "icon": "⏯️", "action": {"type": "media", "media": "play_pause"}}},
    {"name": "Mute volume", "binding": {"label": "Mute", "icon": "🔇", "action": {"type": "volume", "volume": "mute"}}},
    {"name": "Smart light", "binding": {"label": "Light", "icon": "💡", "action": {"type": "smartlight", "mode": "toggle", "host": "192.168.0.87"}, "live": {"source": "light"}}},
    {"name": "Next page", "binding": {"label": "Next", "icon": "➡️", "action": {"type": "page", "page": "next"}}},
]


def _action_summary(item) -> str:
    a = (item or {}).get("action") or {}
    t = a.get("type")
    if not t or t == "none":
        return "Not set"
    if t == "folder":
        return "Folder"
    if t == "substance":
        return f"Painter: {a.get('keys', '')}"
    if t == "quick":
        return f"Quick: {_QUICK_LABEL.get(a.get('op'), a.get('op') or '')}"
    if t == "smartlight":
        m = a.get("mode", "toggle")
        if m == "brightness":
            return f"Light: {int(a.get('brightness', 50))}%"
        if m in ("brightness_up", "brightness_down"):
            return f"Light: bright {'+' if m.endswith('up') else '−'}{int(a.get('step', 10))}%"
        if m in ("hue_up", "hue_down"):
            return f"Light: colour {'+' if m.endswith('up') else '−'}{int(a.get('step', 30))}°"
        return f"Light: {m}"
    if t == "rgbscene":
        m = a.get("mode", "color")
        if m == "bright_set":
            return f"RGB: {int(a.get('brightness', 100))}%"
        if m in ("bright_up", "bright_down"):
            return f"RGB: bright {'+' if m == 'bright_up' else '−'}{int(a.get('step', 10))}%"
        return "RGB: " + {"color": a.get("color", ""), "effect": a.get("effect", ""),
                          "profile": a.get("profile", "")}.get(m, m)
    if t == "obs":
        m = a.get("mode", "scene")
        if m in ("scene", "preview", "mute") and a.get("target"):
            return f"OBS: {a.get('target')}"
        return f"OBS: {m}"
    if t == "http":
        from urllib.parse import urlparse
        host = urlparse(a.get("url", "")).netloc
        m = (a.get("method") or "GET").upper()
        return f"{m} {host}" if host else "HTTP"
    if t == "timer":
        if (a.get("mode") or "countdown") == "stopwatch":
            return "Stopwatch"
        return f"Timer {int(a.get('minutes', 0) or 0):02d}:{int(a.get('seconds', 0) or 0):02d}"
    detail = {"open": a.get("target"), "hotkey": a.get("keys"), "text": a.get("text"),
              "media": a.get("media"), "volume": a.get("volume"), "mic": a.get("mic"),
              "sound": a.get("file"), "discord": a.get("mode") or a.get("discord"), "system": a.get("system"),
              "monitor": a.get("monitor"), "page": a.get("page"), "profile": a.get("name"),
              "brightness": a.get("mode") or a.get("value"), "macro": None}.get(t)
    if t in ("open", "sound") and detail:
        detail = os.path.basename(str(detail))
    label = _SHORT.get(t, t)
    if not detail:
        return label
    d = str(detail)
    return f"{label}: {d[:14]}" + ("…" if len(d) > 14 else "")


class Bridge(QObject):
    status = Signal()


class _ResultBridge(QObject):
    """Marshal a worker-thread result (message, colour) back to the GUI thread via a queued signal."""
    done = Signal(str, str)


class _SaveBridge(QObject):
    """Marshal config-save notifications onto the GUI thread (a queued signal is thread-safe even if
    the controller saves from its event thread) so the undo/redo history snapshots safely."""
    saved = Signal()


class _HotkeyRecorder(QObject):
    """Capture a shortcut: a keyboard combo OR a mouse button/scroll, with modifiers.

    Low-level keyboard hook (suppress=True) so the Windows key is intercepted before
    the OS acts; a mouse hook adds buttons + scroll. Captures the first non-modifier
    trigger (key or mouse) together with any held Ctrl/Alt/Shift/Win.
    """
    captured = Signal(str)
    _MODS = ("ctrl", "alt", "shift", "windows")
    _PURE = ("ctrl", "left ctrl", "right ctrl", "alt", "left alt", "right alt", "alt gr",
             "shift", "left shift", "right shift", "windows", "left windows", "right windows")

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        hk = ""
        try:
            import time as _t
            import keyboard
            import mouse
            _t.sleep(0.3)                      # let the activating click settle
            done = threading.Event()
            result = {"hk": ""}

            def pressed(m):
                try:
                    return keyboard.is_pressed(m)
                except Exception:
                    return False

            def finish(token):
                if not done.is_set():
                    mods = [m for m in self._MODS if pressed(m)]
                    result["hk"] = "+".join(mods + [token]) if token else ""
                    done.set()

            def on_key(e):
                if e.event_type != "down":
                    return
                n = (e.name or "").lower()
                if n in self._PURE:
                    return
                finish(None if n in ("esc", "escape") else n)

            def on_mouse(ev):
                if isinstance(ev, mouse.ButtonEvent):
                    if ev.event_type == "down":
                        b = {"left": "mouse:left", "right": "mouse:right", "middle": "mouse:middle",
                             "x": "mouse:back", "x2": "mouse:forward"}.get(ev.button)
                        if b:
                            finish(b)
                elif isinstance(ev, mouse.WheelEvent):
                    finish("mouse:wheel_up" if ev.delta > 0 else "mouse:wheel_down")

            kb = keyboard.hook(on_key, suppress=True)
            ms = mouse.hook(on_mouse)
            done.wait(timeout=12)
            for unhook, h in ((keyboard.unhook, kb), (mouse.unhook, ms)):
                try:
                    unhook(h)
                except Exception:
                    pass
            hk = result["hk"]
        except Exception:
            hk = ""
        self.captured.emit(hk)


def hotkey_to_qseq(spec: str) -> QKeySequence:
    s = (spec or "").strip().replace("windows", "Meta").replace("win", "Meta")
    return QKeySequence(s)


def qseq_to_hotkey(seq: QKeySequence) -> str:
    s = seq.toString(QKeySequence.PortableText) if seq else ""
    s = s.split(",")[0].strip().lower()          # first chord only
    return s.replace("meta", "win")


def _lerp_color(a: "QColor", b: "QColor", t: float) -> "QColor":
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


ACTION_MIME = "application/x-ajazzdock-actiontype"   # a dragged action from the right-hand sidebar


def _lerp_color(a, b, t: float) -> QColor:
    """Blend two colours (hex or QColor) — the paint-side of an animated hover."""
    ca, cb = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t),
                  round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t))


class _HoverFX:
    """Animated hover (0→1) + press-ripple (1→0) state for custom-painted controls — the
    150 ms OutCubic micro-motion that makes the stage feel responsive instead of snappy-static."""

    def _init_fx(self):
        self._hover_t = 0.0
        self._pulse = 0.0
        self._hover_anim = None
        self._pulse_anim = None

    def _animate_hover(self, to: float):
        an = self._hover_anim
        if an is not None:
            an.stop()
        an = QVariantAnimation(self)
        an.setDuration(150)
        an.setStartValue(self._hover_t)
        an.setEndValue(float(to))
        an.setEasingCurve(QEasingCurve.OutCubic)
        an.valueChanged.connect(self._set_hover_t)
        self._hover_anim = an
        an.start()

    def _set_hover_t(self, v):
        self._hover_t = float(v)
        self.update()

    def _press_pulse(self):
        an = self._pulse_anim
        if an is not None:
            an.stop()
        an = QVariantAnimation(self)
        an.setDuration(340)
        an.setStartValue(1.0)
        an.setEndValue(0.0)
        an.setEasingCurve(QEasingCurve.OutCubic)
        an.valueChanged.connect(self._set_pulse)
        self._pulse_anim = an
        an.start()

    def _set_pulse(self, v):
        self._pulse = float(v)
        self.update()


class KeyTile(_HoverFX, QLabel):
    """A clickable LCD-key preview with a soft animated hover glow + press ripple.
    Scales with the canvas."""
    BASE = 84                                 # tile size at scale 1.0
    FACE = 76                                 # face size at scale 1.0
    clicked = Signal()

    MIME = "application/x-ajazzdock-key"

    def __init__(self, kid: str):
        super().__init__(objectName="key")
        self.kid = kid
        self.win = None                       # set by ConfigWindow (for drag / context menu)
        self._scale = 1.0
        self._src = None                      # source PIL face (re-scaled on rescale)
        self.setFixedSize(self.BASE, self.BASE)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(f"LCD key {kid[3:]}")
        self.setToolTip("Click to edit · drag to another key to swap · right-click to copy / paste")
        self.setAcceptDrops(True)
        self._base_pix = None
        self._face_key = None                 # (id(face), scale, dpr) of last applied face -> skip redundant rebuilds
        self._press_pos = None
        self._drag_over = False               # a drag is hovering this key -> armed-target ring in paintEvent
        self._init_fx()

    def enterEvent(self, ev):
        self._animate_hover(1.0)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._animate_hover(0.0)
        super().leaveEvent(ev)

    def paintEvent(self, ev):
        super().paintEvent(ev)                # QSS background/border + the face pixmap
        t, pulse = self._hover_t, self._pulse
        if t <= 0.01 and pulse <= 0.01 and not self._drag_over:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(Qt.NoBrush)
        rad = max(4.0, float(T.R_LG) - 1.0)   # hug the tile's QSS corner rounding
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        if self._drag_over:                   # a drag is armed on this key: a soft glow + a crisp ring
            for w, a in ((7.0, 46), (2.6, 240)):
                c = QColor(T.ACCENT)
                c.setAlpha(a)
                pen = QPen(c)
                pen.setWidthF(w)
                p.setPen(pen)
                p.drawRoundedRect(r, rad, rad)
        if t > 0.01:                          # hover: an accent glow ring that eases in
            c = QColor(T.ACCENT)
            c.setAlpha(int(95 * t))
            pen = QPen(c)
            pen.setWidthF(2.0)
            p.setPen(pen)
            p.drawRoundedRect(r, rad, rad)
        if pulse > 0.01:                      # press: a brighter ring that blooms and decays
            c = QColor(T.ACCENT)
            c.setAlpha(int(120 * pulse))
            pen = QPen(c)
            pen.setWidthF(2.0 + 2.5 * (1.0 - pulse))
            p.setPen(pen)
            p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), rad + 1, rad + 1)
        p.end()

    def rescale(self, s: float):
        self._scale = s
        self.setFixedSize(round(self.BASE * s), round(self.BASE * s))
        if self._src is not None:
            self._apply_face()

    def set_face(self, pil):
        # Skip the PIL->QPixmap + rounded-clip rebuild when face object, scale and DPR are unchanged.
        # render_face() returns cached, stable-identity faces, so id() is a valid key; self._src keeps
        # the object alive, so its id can't be recycled under us.
        key = (id(pil), self._scale, _dpr(self))
        if key == self._face_key and self._base_pix is not None:
            return
        self._face_key = key
        self._src = pil
        self._apply_face()

    def _apply_face(self):
        f = max(8, round(self.FACE * self._scale))
        dpr = _dpr(self)
        px = max(8, int(round(f * dpr)))              # render at real pixel density -> crisp faces
        src = pil_to_pixmap(self._src).scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        # The tile's QSS border-radius rounds the BORDER, but a QLabel never clips its pixmap — so a
        # square face pokes its corners (often black) past the rounding. Round the pixmap to match.
        rounded = QPixmap(src.size())
        rounded.fill(Qt.transparent)
        p = QPainter(rounded)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        rad = max(2.0, src.width() * 0.16)            # ≈ the tile's 16px radius, scaled to the face
        path.addRoundedRect(0, 0, src.width(), src.height(), rad, rad)
        p.setClipPath(path)
        p.drawPixmap(0, 0, src)
        p.end()
        rounded.setDevicePixelRatio(dpr)
        self._base_pix = rounded
        self.setPixmap(rounded)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press_pos = ev.position().toPoint()
            self._press_pulse()
            self.setFocus()
            self.clicked.emit()

    def mouseMoveEvent(self, ev):
        if (not (ev.buttons() & Qt.LeftButton) or self._press_pos is None or self.win is None):
            return
        if (ev.position().toPoint() - self._press_pos).manhattanLength() < 8:
            return
        if self._is_fixed_back():
            return                            # the folder Back key is fixed
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(self.MIME, self.kid.encode())
        drag.setMimeData(mime)
        if self._base_pix is not None:
            drag.setPixmap(self._base_pix)
            lw = int(self._base_pix.width() / max(1.0, self._base_pix.devicePixelRatio()))
            lh = int(self._base_pix.height() / max(1.0, self._base_pix.devicePixelRatio()))
            drag.setHotSpot(QPoint(lw // 2, lh // 2))   # hotspot is in logical px
        drag.exec(Qt.MoveAction)

    def _is_fixed_back(self):
        return self.win is not None and self.win._is_back_key(self.kid)

    def dragEnterEvent(self, ev):
        md = ev.mimeData()
        if self._is_fixed_back():
            return                            # the folder Back key rejects every drag (incl. key-swap)
        if md.hasFormat(self.MIME) or md.hasFormat(ACTION_MIME) or md.hasUrls() or md.hasText():
            ev.acceptProposedAction()         # a key-swap, a sidebar action, or a file/app/URL
            self._drag_over = True            # arm the target ring so the user sees where it will land
            self.update()

    def dragLeaveEvent(self, ev):
        if self._drag_over:
            self._drag_over = False
            self.update()

    def dropEvent(self, ev):
        md = ev.mimeData()
        if self._drag_over:
            self._drag_over = False
            self.update()
        if self.win is None:
            return
        if md.hasFormat(ACTION_MIME) and not self._is_fixed_back():
            self.win._assign_dropped_action(self.kid, bytes(md.data(ACTION_MIME)).decode())
            ev.acceptProposedAction()
            return
        if md.hasFormat(self.MIME):
            src = bytes(md.data(self.MIME)).decode()
            if src != self.kid:
                self.win._swap_or_move_binding(src, self.kid)
                ev.acceptProposedAction()
            return
        if (md.hasUrls() or md.hasText()) and not self._is_fixed_back():
            self.win._assign_dropped_external(self.kid, md)   # program / file / URL from Windows
            ev.acceptProposedAction()

    def contextMenuEvent(self, ev):
        if self.win is not None:
            self.win._slot_context_menu(self.kid, ev.globalPos(), is_key=True)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(ev)


class CircleControl(_HoverFX, QWidget):
    """A custom-painted round control (knob or button) with soft animated hover + press
    feedback, echoing the device's own key animations."""
    clicked = Signal()

    def __init__(self, diameter: int, knob: bool = False):
        super().__init__()
        self.setFixedSize(diameter, diameter)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._init_fx()
        self._base_d = diameter               # diameter at scale 1.0 (for dynamic rescale)
        self._d = diameter
        self._knob = knob
        self._selected = False
        self._hovered = False
        self._caption = ""
        self.win = None                       # set by ConfigWindow
        self.sid = None                       # slot id (e.g. "btn7") for copy/paste
        self.setAcceptDrops(True)             # accept an action dragged from the sidebar
        self._drag_over = False               # a drag is hovering this control -> armed ring in paintEvent

    def rescale(self, s: float):
        self._d = max(10, round(self._base_d * s))
        self.setFixedSize(self._d, self._d)
        self.update()

    def dragEnterEvent(self, ev):
        md = ev.mimeData()
        if self.sid and (md.hasFormat(ACTION_MIME) or md.hasUrls() or md.hasText()):
            ev.acceptProposedAction()
            self._drag_over = True
            self.update()

    def dragLeaveEvent(self, ev):
        if self._drag_over:
            self._drag_over = False
            self.update()

    def dropEvent(self, ev):
        md = ev.mimeData()
        if self._drag_over:
            self._drag_over = False
            self.update()
        if self.win is None or not self.sid:
            return
        if md.hasFormat(ACTION_MIME):
            self.win._assign_dropped_action(self.sid, bytes(md.data(ACTION_MIME)).decode())
            ev.acceptProposedAction()
        elif md.hasUrls() or md.hasText():
            self.win._assign_dropped_external(self.sid, md)   # program / file / URL from Windows
            ev.acceptProposedAction()

    def contextMenuEvent(self, ev):
        # right-click copy/paste/clear: round buttons copy one binding; knobs copy all 3 sub-actions
        if self.win is None or not self.sid:
            return
        if self.sid.startswith("btn"):
            self.win._slot_context_menu(self.sid, ev.globalPos(), is_key=False)
        elif self.sid.startswith("enc"):
            self.win._knob_context_menu(self.sid, ev.globalPos())

    def setSelected(self, on):
        if on != self._selected:
            self._selected = on
            self.update()

    def setCaption(self, text):
        text = text or ""
        if text != self._caption:
            self._caption = text
            self.update()

    def enterEvent(self, ev):
        self._hovered = True
        self._animate_hover(1.0)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hovered = False
        self._animate_hover(0.0)
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press_pulse()
            self.setFocus()
            self.clicked.emit()
        super().mousePressEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(ev)

    def focusInEvent(self, ev):
        self.update()
        super().focusInEvent(ev)

    def focusOutEvent(self, ev):
        self.update()
        super().focusOutEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self._d / 2.0, self._d / 2.0
        r = self._d / 2.0 - 4
        focused = self.hasFocus()

        if self._drag_over:                   # a drag is armed on this control: soft glow + crisp ring
            for w, a in ((6.0, 46), (2.4, 240)):
                c = QColor(T.ACCENT)
                c.setAlpha(a)
                pen = QPen(c)
                pen.setWidthF(w)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(cx, cy), r + 2, r + 2)

        # soft accent halo so the actively-edited control "lights up" in the brand colour
        if self._selected:
            halo = QColor(T.ACCENT)
            halo.setAlpha(46)
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            p.drawEllipse(QPointF(cx, cy), r + 3, r + 3)

        # press ripple: an accent ring that blooms outward and fades (mirrors the device tap)
        if self._pulse > 0.01:
            ring = QColor(T.ACCENT)
            ring.setAlpha(int(110 * self._pulse))
            rp = QPen(ring)
            rp.setWidthF(1.6 + 1.6 * (1.0 - self._pulse))
            p.setPen(rp)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r + 1 + 3.5 * (1.0 - self._pulse),
                          r + 1 + 3.5 * (1.0 - self._pulse))

        # molded-plastic gradient fill (lighter top, darker bottom)
        grad = QLinearGradient(0, 0, 0, self._d)
        if self._knob:
            grad.setColorAt(0, QColor(T.KNOB_TOP)); grad.setColorAt(1, QColor(T.KNOB_BOT))
        else:
            grad.setColorAt(0, QColor(T.BTN_TOP)); grad.setColorAt(1, QColor(T.BTN_BOT))
        p.setBrush(grad)

        if self._selected:
            border, bw, dash = QColor(T.ACCENT), 2.2, False
        elif focused and not self._hovered:
            border, bw, dash = QColor(T.ACCENT), 1.6, True
        else:                                 # rest ⇄ hover eases between the two border tones
            border = _lerp_color(T.BORDER, T.BORDER_HOVER, self._hover_t)
            bw, dash = 1.5 + 0.2 * self._hover_t, False
        pen = QPen(border)
        pen.setWidthF(bw)
        if dash:
            pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.drawEllipse(QPointF(cx, cy), r, r)

        # top highlight arc for a physical, molded look
        p.setBrush(Qt.NoBrush)
        hl = QPen(QColor(255, 255, 255, 26))
        hl.setWidthF(1.4)
        hl.setCapStyle(Qt.RoundCap)
        p.setPen(hl)
        ir = r - 3
        p.drawArc(QRectF(cx - ir, cy - ir, ir * 2, ir * 2), 35 * 16, 110 * 16)

        if self._knob:
            if self._selected or focused:
                tick = QColor(T.ACCENT)
            else:                             # hover eases the tick toward the accent
                tick = _lerp_color(T.TICK, T.ACCENT, self._hover_t)
            tp = QPen(tick)
            tp.setWidthF(3)
            tp.setCapStyle(Qt.RoundCap)
            p.setPen(tp)
            top = -r + 8
            p.drawLine(QPointF(cx, cy + top), QPointF(cx, cy + top + r * 0.34))

        # what the control does — a short, muted label inside it (tick stays clear)
        if self._caption:
            fs = max(7, min(11, int(round(self._d * 0.135))))
            f = QFont("Segoe UI", fs)
            f.setWeight(QFont.DemiBold)
            p.setFont(f)
            txt = p.fontMetrics().elidedText(self._caption, Qt.ElideRight, int(self._d * 0.80))
            p.setPen(QColor(T.ACCENT) if self._selected else QColor(T.TEXT_DIM))
            cyoff = r * 0.32 if self._knob else 0.0    # knob: below the tick; button: centered
            rect = QRectF(cx - self._d / 2.0, cy + cyoff - fs, float(self._d), fs * 2.0)
            p.drawText(rect, Qt.AlignHCenter | Qt.AlignVCenter, txt)
        p.end()


class FluentIconDialog(QDialog):
    """A gallery of crisp Segoe Fluent icons; picking one sets the key icon to 'fluent:<name>'."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fluent icons")
        self.chosen = None
        from PySide6.QtGui import QFont
        from .images import FLUENT_ICONS
        gf = QFont("Segoe Fluent Icons"); gf.setPointSize(19)
        v = QVBoxLayout(self); v.setContentsMargins(10, 10, 10, 10)
        v.addWidget(QLabel("Crisp built-in icons — cohesive across the dock.", objectName="dim"))
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setObjectName("emojiScroll")
        host = QWidget(); grid = QGridLayout(host); grid.setSpacing(6)
        cols = 6
        for i, (name, cp) in enumerate(sorted(FLUENT_ICONS.items())):
            b = QPushButton(chr(cp)); b.setFont(gf); b.setFixedSize(54, 46)
            b.setToolTip(name); b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, n=name: self._choose(n))
            grid.addWidget(b, i // cols, i % cols)
        scroll.setWidget(host); v.addWidget(scroll, 1)
        self.resize(420, 480)

    def _choose(self, name):
        self.chosen = name
        self.accept()


class EmojiPicker(QDialog):
    """A categorized emoji picker — category tabs, keyword search and recently-used.

    Data comes from dock.emoji_data (a curated, real-colour-emoji-only set). Recents
    live in QSettings so they persist without touching the portable config file.
    """
    picked = Signal(str)
    _RECENT_KEY = "recent_emoji"
    _RECENT_MAX = 32

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick an emoji")
        self.resize(540, 600)
        self._cat = CATEGORIES[0]["key"]
        self._index = [(ch, name, terms, c["key"])
                       for c in CATEGORIES for (ch, name, terms) in c["emoji"]]

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search emoji — e.g. fire, cat, heart, party…")
        self._search.setClearButtonEnabled(True)
        v.addWidget(self._search)

        strip = QHBoxLayout()
        strip.setSpacing(4)
        self._catgroup = QButtonGroup(self)
        self._catgroup.setExclusive(True)
        self._catbtns = {}
        catfont = QFont("Segoe UI Emoji", 15)

        def add_cat(key, icon, tip):
            b = QToolButton(objectName="emojicat")
            self._set_emoji(b, icon, 22, catfont)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(40, 34)
            b.clicked.connect(lambda _=False, k=key: self._show_cat(k))
            self._catgroup.addButton(b)
            self._catbtns[key] = b
            strip.addWidget(b)

        add_cat("recent", "🕘", "Recently used")
        for c in CATEGORIES:
            add_cat(c["key"], c["icon"], c["label"])
        strip.addStretch(1)
        v.addLayout(strip)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        v.addWidget(self._scroll, 1)
        self._count = QLabel("", objectName="dim")
        v.addWidget(self._count)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(130)
        self._timer.timeout.connect(self._apply_search)
        self._search.textChanged.connect(lambda _=None: self._timer.start())

        start = "recent" if self._recents() else CATEGORIES[0]["key"]
        self._catbtns[start].setChecked(True)
        self._show_cat(start)

    # ---- recently used (QSettings, kept out of the portable config) ----
    @staticmethod
    def _settings():
        from PySide6.QtCore import QSettings
        return QSettings("AjazzDock", "AjazzDock")

    def _recents(self):
        raw = self._settings().value(self._RECENT_KEY, "")
        return [c for c in str(raw or "").split("") if c]

    def _push_recent(self, ch):
        rec = [ch] + [c for c in self._recents() if c != ch]
        self._settings().setValue(self._RECENT_KEY, "".join(rec[:self._RECENT_MAX]))

    def _name(self, ch):
        return next((n for c, n, _t, _k in self._index if c == ch), "")

    # ---- views ----
    def _show_cat(self, key):
        self._cat = key
        if key in self._catbtns and not self._catbtns[key].isChecked():
            self._catbtns[key].setChecked(True)
        if self._search.text():
            self._search.blockSignals(True)
            self._search.clear()
            self._search.blockSignals(False)
        if key == "recent":
            items = [(ch, self._name(ch)) for ch in self._recents()]
            label, empty = "Recently used", "No recent emoji yet — pick one and it'll show here."
        else:
            cat = next(c for c in CATEGORIES if c["key"] == key)
            items = [(ch, name) for ch, name, _t in cat["emoji"]]
            label = empty = cat["label"]
        self._render(items)
        self._count.setText(f"{label} · {len(items)}" if items else empty)

    def _apply_search(self):
        q = self._search.text().strip().lower()
        if not q:
            self._show_cat(self._cat)
            return
        self._catgroup.setExclusive(False)         # allow unchecking all during search
        for b in self._catgroup.buttons():
            b.setChecked(False)
        self._catgroup.setExclusive(True)
        words = q.split()
        out = []
        for ch, name, terms, _k in self._index:
            if all(w in (name + " " + terms) for w in words):
                out.append((ch, name))
                if len(out) >= 400:
                    break
        self._render(out)
        self._count.setText((f"{len(out)} result" + ("" if len(out) == 1 else "s")) if out else "No matches")

    def _render(self, items):
        host = QWidget()
        grid = QGridLayout(host)
        grid.setSpacing(4)
        grid.setContentsMargins(2, 2, 2, 2)
        fallback = QFont("Segoe UI Emoji", 22)
        cols = 7
        for i, (ch, name) in enumerate(items):
            b = QToolButton(objectName="emoji")
            b.setFixedSize(58, 58)
            self._set_emoji(b, ch, 42, fallback)
            if name:
                b.setToolTip(name)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, e=ch: self._pick(e))
            grid.addWidget(b, i // cols, i % cols)
        grid.setRowStretch(len(items) // cols + 1, 1)
        self._scroll.setWidget(host)

    @staticmethod
    def _set_emoji(btn, glyph, px, fallback_font):
        """Show the emoji as a PIL-rendered icon (color emoji don't scale as button text)."""
        dpr = _dpr(btn)
        em = emoji_image(glyph, int(round(px * dpr)))
        if em is not None:
            pm = pil_to_pixmap(em, dpr)
            btn.setIcon(QIcon(pm))
            btn.setIconSize(QSize(px, px))
        else:
            btn.setText(glyph)
            btn.setFont(fallback_font)

    def _pick(self, ch):
        self._push_recent(ch)
        self.picked.emit(ch)
        self.accept()


class CropArea(QWidget):
    """Image with a movable / zoomable crop box (square-locked by default).

    Drag the box to move it; scroll wheel or zoom() to resize; center() recenters.
    """

    def __init__(self, pil_img, maxside=460, parent=None):
        super().__init__(parent)
        self.orig = pil_img.convert("RGBA")
        ow, oh = self.orig.size
        s = maxside / max(ow, oh)
        self.dw, self.dh = max(1, int(ow * s)), max(1, int(oh * s))
        self.scale = ow / self.dw                      # original px per display px
        self.pix = pil_to_pixmap(self.orig.resize((self.dw, self.dh), Image.LANCZOS))
        self.setFixedSize(self.dw, self.dh)
        self.setCursor(Qt.OpenHandCursor)
        self.square = True
        side = int(min(self.dw, self.dh) * 0.8)
        self.box = QRect(0, 0, side, side)
        self.box.moveCenter(QPoint(self.dw // 2, self.dh // 2))
        self._drag = None

    def _clamp_box(self):
        b = self.box
        w, h = min(b.width(), self.dw), min(b.height(), self.dh)
        x = max(0, min(self.dw - w, b.x()))
        y = max(0, min(self.dh - h, b.y()))
        self.box = QRect(x, y, w, h)

    def set_square(self, on):
        self.square = on
        if on:
            side = min(self.box.width(), self.box.height())
            c = self.box.center()
            self.box = QRect(0, 0, side, side)
            self.box.moveCenter(c)
        self._clamp_box()
        self.update()

    def center(self):
        self.box.moveCenter(QPoint(self.dw // 2, self.dh // 2))
        self._clamp_box()
        self.update()

    def zoom(self, factor):
        c = self.box.center()
        w = max(24, min(self.dw, int(self.box.width() * factor)))
        h = max(24, min(self.dh, int(self.box.height() * factor)))
        if self.square:
            w = h = min(w, h)
        self.box = QRect(0, 0, w, h)
        self.box.moveCenter(c)
        self._clamp_box()
        self.update()

    def wheelEvent(self, e):
        self.zoom(0.9 if e.angleDelta().y() > 0 else 1.1)   # scroll up = zoom in

    def mousePressEvent(self, e):
        if self.box.contains(e.position().toPoint()):
            self._drag = (e.position().toPoint(), QRect(self.box))
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if not self._drag:
            return
        start, box0 = self._drag
        self.box = QRect(box0)
        self.box.translate(e.position().toPoint() - start)
        self._clamp_box()
        self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None
        self.setCursor(Qt.OpenHandCursor)

    def paintEvent(self, e):
        p = QPainter(self)
        p.drawPixmap(0, 0, self.pix)
        if self.box.width() > 1:
            p.fillRect(self.rect(), QColor(0, 0, 0, 120))
            p.drawPixmap(self.box, self.pix, self.box)
            pen = QPen(QColor(T.ACCENT))
            pen.setWidth(2)
            p.setPen(pen)
            p.drawRect(self.box.adjusted(0, 0, -1, -1))
        p.end()

    def cropped(self):
        if self.box.width() < 4 or self.box.height() < 4:
            return self.orig
        ow, oh = self.orig.size
        x = int(self.box.x() * self.scale)
        y = int(self.box.y() * self.scale)
        x2 = min(ow, int((self.box.x() + self.box.width()) * self.scale))
        y2 = min(oh, int((self.box.y() + self.box.height()) * self.scale))
        return self.orig.crop((x, y, x2, y2))


class ImageCropDialog(QDialog):
    def __init__(self, pil_img, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crop image")
        self.result_image = None
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Drag the box to move it · scroll or use Zoom to resize.",
                           objectName="dim"))
        self.area = CropArea(pil_img)
        wrap = QHBoxLayout()
        wrap.addStretch(1)
        wrap.addWidget(self.area)
        wrap.addStretch(1)
        v.addLayout(wrap)
        ctl = QHBoxLayout()
        cbtn = QPushButton("Center")
        cbtn.clicked.connect(self.area.center)
        zin = QPushButton("Zoom +")
        zin.clicked.connect(lambda: self.area.zoom(0.85))
        zout = QPushButton("Zoom −")
        zout.clicked.connect(lambda: self.area.zoom(1.18))
        sq = QCheckBox("Square crop (matches the key shape)")
        sq.setChecked(True)
        sq.toggled.connect(self.area.set_square)
        ctl.addWidget(cbtn)
        ctl.addWidget(zin)
        ctl.addWidget(zout)
        ctl.addStretch(1)
        ctl.addWidget(sq)
        v.addLayout(ctl)
        btns = QHBoxLayout()
        full = QPushButton("Use whole image")
        full.clicked.connect(lambda: (setattr(self, "result_image", self.area.orig), self.accept()))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Crop && use")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        btns.addWidget(full)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        v.addLayout(btns)

    def accept(self):
        if self.result_image is None:
            self.result_image = self.area.cropped()
        super().accept()


class BackupsDialog(QDialog):
    """Backup history with one-click restore (always backs up the current state first)."""

    def __init__(self, win: "ConfigWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Backups")
        self.setMinimumSize(480, 360)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        intro = QLabel("Restore points, newest first. Restoring backs up your current "
                       "setup first, so nothing is ever lost.", objectName="dim")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._restore())
        v.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.restore_btn = QPushButton("Restore selected")
        self.restore_btn.clicked.connect(self._restore)
        now_btn = QPushButton("Back up now")
        now_btn.clicked.connect(self._backup_now)
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(self._open_folder)
        row.addWidget(self.restore_btn)
        row.addWidget(now_btn)
        row.addWidget(open_btn)
        row.addStretch(1)
        v.addLayout(row)

        row2 = QHBoxLayout()
        exp = QPushButton("Export…")
        exp.clicked.connect(self.win._export_config)
        imp = QPushButton("Import…")
        imp.clicked.connect(self._import)
        row2.addWidget(exp)
        row2.addWidget(imp)
        row2.addStretch(1)
        close = QPushButton("Close"); close.setObjectName("primary"); close.setDefault(True)
        close.clicked.connect(self.accept)
        row2.addWidget(close)
        v.addLayout(row2)
        self._reload()

    def _reload(self):
        self.list.clear()
        for b in backups.list_backups():
            when = b["when"].strftime("%b %d   %H:%M:%S")
            if b["ok"]:
                text = f"{when}      {b['pages']} pages · {b['keys']} keys"
            else:
                text = f"{when}      (unreadable)"
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, b["path"])
            if not b["ok"]:
                it.setForeground(QColor(T.DANGER))
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.restore_btn.setEnabled(self.list.count() > 0)

    def _selected_path(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _restore(self):
        path = self._selected_path()
        if not path:
            return
        if QMessageBox.question(self, "Restore",
                                "Restore this backup? Your current setup is saved first.") \
                != QMessageBox.Yes:
            return
        if backups.restore(path):
            self.win._after_external_config_change()
            self._reload()
            QMessageBox.information(self, "Restored", "Backup restored to the dock.")
        else:
            QMessageBox.warning(self, "Restore failed", "That backup couldn't be read.")

    def _backup_now(self):
        backups.snapshot("manual")
        self._reload()

    def _open_folder(self):
        try:
            os.startfile(backups_dir())            # Windows
        except OSError:
            pass

    def _import(self):
        if self.win._import_config():
            self._reload()


class CalibrationDialog(QDialog):
    """Live per-key image calibration — drag Size / X / Y and watch the dock update."""

    def __init__(self, win):
        super().__init__(win)
        self.controller = win.controller
        self.setWindowTitle("Display calibration")
        self.setMinimumWidth(400)
        self._closed = False                       # guards a late debounce tick after Save/Cancel
        self.controller.begin_calibration()        # open the session (controller ignores stray previews otherwise)
        d = self.controller.config.display()
        self.w = int(d.get("w", 88))
        self.h = int(d.get("h", 88))
        self.dx = max(-20, min(20, int(d.get("dx", 0))))
        self.dy = max(-20, min(20, int(d.get("dy", 0))))
        self.inset = max(0, min(8, int(d.get("inset", 2))))

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        intro = QLabel("Watch the dock while you drag. Each key shows an alignment GRID with white "
                       "corner brackets, a yellow edge frame, a CYAN band at the TOP and a RED band "
                       "at the BOTTOM. Grow Width & Height until the yellow frame and all four corner "
                       "brackets reach the glass (back off if it bleeds into a neighbour); count the "
                       "faint grid cells to see how much an edge is clipped. Use X / Y for a centring "
                       "nudge (push left / down to kill a left-bottom gap), and lower “Edge frame” to "
                       "0 to reach the very edge. If red ISN'T at the bottom, tell me — the axes are "
                       "rotated and I'll flip them.", objectName="dim")
        intro.setWordWrap(True)
        v.addWidget(intro)
        if not self.controller.status().get("connected"):
            warn = QLabel("⚠ The dock isn't connected — plug it in to see the live preview.",
                          objectName="dim")
            warn.setWordWrap(True)
            v.addWidget(warn)
        self.val = QLabel("", objectName="h1")
        v.addWidget(self.val)
        self.s_w = self._slider("Width  (fills left↔right)", 60, 160, self.w, "w", v)
        self.s_h = self._slider("Height  (fills top↕bottom)", 60, 160, self.h, "h", v)
        self.s_dx = self._slider("Nudge horizontal", -20, 20, self.dx, "dx", v)
        self.s_dy = self._slider("Nudge vertical", -20, 20, self.dy, "dy", v)
        self.s_inset = self._slider("Edge frame  (0 = reach the very edge)", 0, 8, self.inset, "inset", v)

        row = QHBoxLayout()
        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset)
        row.addWidget(reset)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(save)
        v.addLayout(row)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._preview)
        self._refresh()
        self._preview()

    def _slider(self, label, lo, hi, val, attr, parent):
        parent.addWidget(QLabel(label, objectName="section"))
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(val)
        s.valueChanged.connect(lambda x, a=attr: self._set(a, x))
        parent.addWidget(s)
        return s

    def _set(self, attr, x):
        setattr(self, attr, x)
        self._refresh()
        self._timer.start()

    def _refresh(self):
        self.val.setText(f"W {self.w}  ·  H {self.h}  ·  X {self.dx:+d}  ·  Y {self.dy:+d}  ·  Edge {self.inset}")

    def _preview(self):
        if self._closed:
            return
        self.controller.preview_calibration(self.w, self.h, self.dx, self.dy, self.inset)

    def _reset(self):
        self.s_w.setValue(88)
        self.s_h.setValue(88)
        self.s_dx.setValue(0)
        self.s_dy.setValue(0)
        self.s_inset.setValue(2)

    def _save(self):
        self._closed = True                        # stop any pending preview from re-arming calib
        self._timer.stop()
        self.controller.apply_calibration(self.w, self.h, self.dx, self.dy, self.inset)
        self.accept()

    def reject(self):
        self._closed = True
        self._timer.stop()
        self.controller.end_calibration()
        super().reject()

    def closeEvent(self, ev):
        # The title-bar X / Alt+F4 don't always route through reject(); make sure the session
        # is closed and the page restored no matter how the dialog is dismissed.
        if not self._closed:
            self._closed = True
            self._timer.stop()
            self.controller.end_calibration()
        super().closeEvent(ev)


class IconStyleDialog(QDialog):
    """Live, sectioned editor for a key's look: icon transform, shape & effects, background."""

    _KEYS = ("icon_scale", "icon_radius", "icon_tile", "icon_tile_color", "icon_dx", "icon_dy",
             "icon_rotate", "icon_opacity", "icon_border", "icon_border_color", "icon_shadow",
             "bg2", "bg_dir", "fit")

    def __init__(self, win, item):
        super().__init__(win)
        self.win = win
        self.item = item
        self.setWindowTitle("Customize key")
        self.setMinimumWidth(380)
        self._orig = {k: item.get(k) for k in self._KEYS}
        self._tilecol = item.get("icon_tile_color") or "#1e6fd0"
        self._bordcol = item.get("icon_border_color") or "#ffffff"
        self._bg2 = item.get("bg2") or "#1e2b3a"

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        self.preview = QLabel()
        self.preview.setFixedSize(108, 108)
        self.preview.setAlignment(Qt.AlignCenter)
        self._closed = False
        self._timer = QTimer(self)                    # coalesce slider-drag re-renders (~50ms)
        self._timer.setSingleShot(True)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._do_refresh)
        self.finished.connect(lambda _r: self._timer.stop())
        prow = QHBoxLayout()
        prow.addStretch(1); prow.addWidget(self.preview); prow.addStretch(1)
        root.addLayout(prow)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        v = QVBoxLayout(host)
        v.setSpacing(8); v.setContentsMargins(0, 0, 12, 0)   # right gutter clears the 10px scrollbar
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        # --- Icon transform ---
        _, gi = self._group("Icon  (image or emoji)", v)
        self.s_zoom = self._slider(gi, "Zoom", 30, 130, int(round(float(item.get("icon_scale", 1.0)) * 100)),
                                   lambda x: self._set("icon_scale", x / 100.0), fmt=lambda x: f"{x}%")
        self.s_dx = self._slider(gi, "Position X", -40, 40, int(item.get("icon_dx", 0)),
                                 lambda x: self._set("icon_dx", int(x)), fmt=lambda x: f"{x:+d} px")
        self.s_dy = self._slider(gi, "Position Y", -40, 40, int(item.get("icon_dy", 0)),
                                 lambda x: self._set("icon_dy", int(x)), fmt=lambda x: f"{x:+d} px")
        self.s_rot = self._slider(gi, "Rotation", -180, 180, int(item.get("icon_rotate", 0)),
                                  lambda x: self._set("icon_rotate", int(x)), fmt=lambda x: f"{x}°")
        self.s_op = self._slider(gi, "Opacity", 10, 100, int(item.get("icon_opacity", 100)),
                                 lambda x: self._set("icon_opacity", int(x)), fmt=lambda x: f"{x}%")
        self.s_round = self._slider(gi, "Corner roundness", 0, 50, int(item.get("icon_radius", 0)),
                                    lambda x: self._set("icon_radius", int(x)), fmt=lambda x: f"{x} px")

        # --- Shape & effects ---
        _, gs = self._group("Shape && effects", v)
        self.cb_tile = QCheckBox("App tile  (icon on a rounded background)")
        self.cb_tile.setChecked(bool(item.get("icon_tile")))
        self.cb_tile.toggled.connect(self._toggle_tile)
        gs.addWidget(self.cb_tile)
        self.tile_btn = QPushButton(); self.tile_btn.setFixedSize(46, 24)
        self.tile_row = self._color_row("Tile colour", self.tile_btn, self._tilecol,
                                        self._pick_tile, self._auto_tile)
        gs.addWidget(self.tile_row); self.tile_row.setVisible(bool(item.get("icon_tile")))
        self.s_border = self._slider(gs, "Border width", 0, 8, int(item.get("icon_border", 0)),
                                     lambda x: self._set("icon_border", int(x)), fmt=lambda x: f"{x} px")
        self.bord_btn = QPushButton(); self.bord_btn.setFixedSize(46, 24)
        gs.addWidget(self._color_row("Border colour", self.bord_btn, self._bordcol, self._pick_border, None))
        self.cb_shadow = QCheckBox("Drop shadow")
        self.cb_shadow.setChecked(bool(item.get("icon_shadow")))
        self.cb_shadow.toggled.connect(lambda on: self._set("icon_shadow", bool(on)))
        gs.addWidget(self.cb_shadow)
        self.cb_fill = QCheckBox("Crop to fill  (off = show the whole icon)")
        self.cb_fill.setChecked(effective_fit(item) == "cover")
        self.cb_fill.toggled.connect(lambda on: self._set("fit", "cover" if on else "contain"))
        gs.addWidget(self.cb_fill)

        # --- Background ---
        _, gb = self._group("Background", v)
        self.cb_grad = QCheckBox("Gradient")
        self.cb_grad.setChecked(bool(item.get("bg2")))
        self.cb_grad.toggled.connect(self._toggle_grad)
        gb.addWidget(self.cb_grad)
        self.bg2_btn = QPushButton(); self.bg2_btn.setFixedSize(46, 24)
        self.grad_row = self._color_row("2nd colour", self.bg2_btn, self._bg2, self._pick_bg2, None)
        gb.addWidget(self.grad_row)
        self.dir_row = QWidget(); dl = QHBoxLayout(self.dir_row); dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(QLabel("Direction", objectName="section"))
        self.dir_combo = QComboBox()
        for lab, d in (("Vertical", "v"), ("Horizontal", "h"), ("Diagonal ↘", "d"), ("Diagonal ↙", "d2")):
            self.dir_combo.addItem(lab, d)
        di = self.dir_combo.findData(item.get("bg_dir", "v"))
        self.dir_combo.setCurrentIndex(di if di >= 0 else 0)
        self.dir_combo.currentIndexChanged.connect(lambda _i: self._set("bg_dir", self.dir_combo.currentData()))
        dl.addWidget(self.dir_combo); dl.addStretch(1)
        gb.addWidget(self.dir_row)
        self.grad_row.setVisible(bool(item.get("bg2"))); self.dir_row.setVisible(bool(item.get("bg2")))

        v.addStretch(1)
        row = QHBoxLayout()
        reset = QPushButton("Reset"); reset.clicked.connect(self._reset)
        row.addWidget(reset); row.addStretch(1)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        save = QPushButton("Save"); save.setObjectName("primary"); save.setDefault(True)
        save.clicked.connect(self._save)
        row.addWidget(cancel); row.addWidget(save)
        root.addLayout(row)
        self.resize(400, 660)
        self._refresh()

    # ---- builders ----
    def _group(self, title, parent):
        g = QGroupBox(title)
        lay = QVBoxLayout(g); lay.setSpacing(6)
        parent.addWidget(g)
        return g, lay

    def _slider(self, parent, label, lo, hi, val, setter, fmt=str):
        head = QWidget()
        hl = QHBoxLayout(head); hl.setContentsMargins(0, 0, 0, 0); hl.setSpacing(6)
        hl.addWidget(QLabel(label, objectName="section"))
        hl.addStretch(1)
        readout = QLabel(fmt(val), objectName="sliderval")        # live numeric value
        readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        hl.addWidget(readout)
        parent.addWidget(head)
        s = QSlider(Qt.Horizontal); s.setRange(lo, hi); s.setValue(val)
        s.valueChanged.connect(setter)
        s.valueChanged.connect(lambda x, lb=readout, f=fmt: lb.setText(f(x)))
        parent.addWidget(s)
        return s

    def _color_row(self, label, btn, initial, on_pick, on_auto):
        w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QLabel(label, objectName="section"))
        self._restyle(btn, initial)
        btn.clicked.connect(lambda: on_pick(btn))
        h.addWidget(btn)
        if on_auto:
            a = QPushButton("Auto"); a.setToolTip("Use the icon's own dominant colour")
            a.clicked.connect(on_auto); h.addWidget(a)
        h.addStretch(1)
        return w

    @staticmethod
    def _restyle(btn, color):
        btn.setStyleSheet(f"background:{color}; border:1px solid {T.BORDER}; border-radius:{T.R_SM}px;")

    # ---- handlers ----
    def _set(self, key, value):
        self.item[key] = value
        self._refresh()

    def _toggle_tile(self, on):
        self.item["icon_tile"] = bool(on)
        if on:
            self.item["icon_tile_color"] = self._tilecol
        self.tile_row.setVisible(on)
        self._refresh()

    def _toggle_grad(self, on):
        if on:
            self.item["bg2"] = self._bg2
        else:
            self.item.pop("bg2", None)
        self.grad_row.setVisible(on); self.dir_row.setVisible(on)
        self._refresh()

    def _pick_tile(self, btn):
        col = QColorDialog.getColor(QColor(self._tilecol), self, "Tile colour")
        if col.isValid():
            self._tilecol = col.name(); self.item["icon_tile_color"] = self._tilecol
            self._restyle(btn, self._tilecol); self._refresh()

    def _auto_tile(self):
        self.item.pop("icon_tile_color", None)        # None -> renderer derives from the icon
        self._refresh()

    def _pick_border(self, btn):
        col = QColorDialog.getColor(QColor(self._bordcol), self, "Border colour")
        if col.isValid():
            self._bordcol = col.name(); self.item["icon_border_color"] = self._bordcol
            self._restyle(btn, self._bordcol); self._refresh()

    def _pick_bg2(self, btn):
        col = QColorDialog.getColor(QColor(self._bg2), self, "2nd colour")
        if col.isValid():
            self._bg2 = col.name(); self.item["bg2"] = self._bg2
            self._restyle(btn, self._bg2); self._refresh()

    def _refresh(self):
        self._timer.start()                           # debounced -> _do_refresh

    def _do_refresh(self):
        if self._closed:
            return
        try:
            face = self.win._face(self.item)          # render once; reuse for preview + tile
        except Exception:
            return
        try:
            self.preview.setPixmap(face_pixmap(face, 108, self.preview))
        except Exception:
            pass
        self.win._refresh_key_preview(self.win.sel, face=face)
        self.win._render_timer.start()

    def _reset(self):
        for k in self._KEYS:
            if k != "fit":
                self.item.pop(k, None)
        for s, val in ((self.s_zoom, 100), (self.s_dx, 0), (self.s_dy, 0), (self.s_rot, 0),
                       (self.s_op, 100), (self.s_round, 0), (self.s_border, 0)):
            s.blockSignals(True); s.setValue(val); s.blockSignals(False)
        for cb in (self.cb_tile, self.cb_shadow, self.cb_grad):
            cb.blockSignals(True); cb.setChecked(False); cb.blockSignals(False)
        self.tile_row.setVisible(False); self.grad_row.setVisible(False); self.dir_row.setVisible(False)
        self._refresh()

    def _save(self):
        self._closed = True
        self._timer.stop()
        self.win.cfg.save()
        self.win.controller.request_render()
        self.accept()

    def reject(self):
        self._closed = True
        self._timer.stop()
        for k, val in self._orig.items():             # discard live edits
            if val is None:
                self.item.pop(k, None)
            else:
                self.item[k] = val
        self.win._refresh_key_preview(self.win.sel)
        self.win.controller.request_render()
        super().reject()


# Curated background gradients (name, start, end, direction) — vibrant, modern, one-click.
GRADIENT_PRESETS = [
    ("Sky",       "#2E8BFF", "#1CC8EE", "d"),
    ("Azure",     "#1CC8EE", "#2E6BFF", "d"),
    ("Indigo",    "#5B6CFF", "#8F4BFF", "d"),
    ("Grape",     "#A24BFF", "#E14ECE", "d"),
    ("Bubblegum", "#FF5E9C", "#FF8F6B", "d"),
    ("Rose",      "#FF4D6D", "#C9184A", "d"),
    ("Sunset",    "#FF6A3D", "#FFB23E", "d"),
    ("Gold",      "#FFB13D", "#FFE259", "d"),
    ("Lime",      "#28D17C", "#9CFF57", "d"),
    ("Teal",      "#14B8A6", "#2EE6C7", "d"),
    ("Forest",    "#0BA360", "#3CBA92", "d"),
    ("Ocean",     "#2B5876", "#4E4376", "d"),
    ("Mauve",     "#834D9B", "#D04ED6", "d"),
    ("Slate",     "#3A4A5A", "#1E2A38", "d"),
]


def _grad_qss(c1, c2, direction="d"):
    """A Qt-stylesheet linear-gradient string for the swatch previews (mirrors images._grad_mask)."""
    coords = {"v":  "x1:0, y1:0, x2:0, y2:1",
              "h":  "x1:0, y1:0, x2:1, y2:0",
              "d":  "x1:0, y1:0, x2:1, y2:1",
              "d2": "x1:1, y1:0, x2:0, y2:1"}.get(direction, "x1:0, y1:0, x2:1, y2:1")
    return f"qlineargradient({coords}, stop:0 {c1}, stop:1 {c2})"


class GradientPicker(QDialog):
    """Choose a key background: a flat colour, or a two-colour gradient. A gallery of curated presets
    for one-click selection, plus custom start/end colours + direction. Edits the key live (so the
    device + preview update as you browse); Cancel restores what was there before."""
    _COLS = 5

    def __init__(self, win, item):
        super().__init__(win)
        self.win = win
        self.item = item
        self._orig = {k: item.get(k) for k in ("color", "bg2", "bg_dir")}
        self.setWindowTitle("Background")
        self.setMinimumWidth(430)
        root = QVBoxLayout(self)
        root.setSpacing(10)

        self.preview = QLabel()
        self.preview.setFixedSize(96, 96)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet(f"border:1px solid {T.BORDER}; border-radius:{T.R_MD}px;")
        root.addWidget(self.preview, 0, Qt.AlignHCenter)

        root.addWidget(QLabel("Presets", objectName="section"))
        grid = QGridLayout()
        grid.setSpacing(7)
        solid = QPushButton("Solid")
        solid.setToolTip("No gradient — a single flat colour")
        solid.setFixedHeight(40)
        solid.setCursor(Qt.PointingHandCursor)
        solid.clicked.connect(self._make_solid)
        grid.addWidget(solid, 0, 0)
        for i, (name, c1, c2, d) in enumerate(GRADIENT_PRESETS):
            sw = QPushButton()
            sw.setToolTip(name)
            sw.setFixedHeight(40)
            sw.setCursor(Qt.PointingHandCursor)
            sw.setStyleSheet(f"border:1px solid {T.BORDER}; border-radius:{T.R_SM}px; "
                             f"background:{_grad_qss(c1, c2, d)};")
            sw.clicked.connect(lambda _=False, a=c1, b=c2, dd=d: self._apply(a, b, dd))
            idx = i + 1
            grid.addWidget(sw, idx // self._COLS, idx % self._COLS)
        root.addLayout(grid)

        root.addWidget(QLabel("Custom", objectName="section"))
        crow = QHBoxLayout()
        crow.addWidget(QLabel("Start", objectName="dim"))
        self.c1_btn = QPushButton(); self.c1_btn.setFixedSize(40, 24)
        self.c1_btn.setCursor(Qt.PointingHandCursor)
        self.c1_btn.clicked.connect(lambda: self._pick("color"))
        crow.addWidget(self.c1_btn)
        crow.addSpacing(8)
        crow.addWidget(QLabel("End", objectName="dim"))
        self.c2_btn = QPushButton(); self.c2_btn.setFixedSize(40, 24)
        self.c2_btn.setCursor(Qt.PointingHandCursor)
        self.c2_btn.clicked.connect(lambda: self._pick("bg2"))
        crow.addWidget(self.c2_btn)
        crow.addSpacing(8)
        crow.addWidget(QLabel("Angle", objectName="dim"))
        self.dir_combo = QComboBox()
        for lab, d in (("Vertical", "v"), ("Horizontal", "h"), ("Diagonal ↘", "d"), ("Diagonal ↙", "d2")):
            self.dir_combo.addItem(lab, d)
        self.dir_combo.currentIndexChanged.connect(lambda _i: self._set_dir(self.dir_combo.currentData()))
        crow.addWidget(self.dir_combo)
        crow.addStretch(1)
        root.addLayout(crow)

        root.addStretch(1)
        brow = QHBoxLayout()
        brow.addStretch(1)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        done = QPushButton("Done"); done.setObjectName("primary"); done.setDefault(True)
        done.clicked.connect(self.accept)
        brow.addWidget(cancel); brow.addWidget(done)
        root.addLayout(brow)

        self._sync_custom()
        self._refresh()

    def _apply(self, c1, c2, direction):
        self.item["color"] = c1
        self.item["bg2"] = c2
        self.item["bg_dir"] = direction
        self._sync_custom()
        self._refresh()

    def _make_solid(self):
        self.item.pop("bg2", None)
        self._sync_custom()
        self._refresh()

    def _set_dir(self, d):
        self.item["bg_dir"] = d
        if self.item.get("bg2"):
            self._refresh()

    def _pick(self, key):
        cur = self.item.get(key) or (DEFAULT_BG if key == "color" else "#5b6cff")
        col = QColorDialog.getColor(QColor(cur), self, "Pick colour")
        if col.isValid():
            self.item[key] = col.name()
            self._sync_custom()
            self._refresh()

    def _sync_custom(self):
        c1 = self.item.get("color") or DEFAULT_BG
        c2 = self.item.get("bg2") or c1
        self.c1_btn.setStyleSheet(f"background:{c1}; border:1px solid {T.BORDER}; border-radius:{T.R_SM}px;")
        self.c2_btn.setStyleSheet(f"background:{c2}; border:1px solid {T.BORDER}; border-radius:{T.R_SM}px;")
        di = self.dir_combo.findData(self.item.get("bg_dir", "v"))
        self.dir_combo.blockSignals(True)
        self.dir_combo.setCurrentIndex(di if di >= 0 else 0)
        self.dir_combo.blockSignals(False)

    def _refresh(self):
        try:
            face = self.win._face(self.item)
        except Exception:
            return
        try:
            self.preview.setPixmap(face_pixmap(face, 88, self.preview))
        except Exception:
            pass
        self.win._refresh_key_preview(self.win.sel, face=face)
        self.win._render_timer.start()

    def reject(self):
        for k, v in self._orig.items():
            if v is None:
                self.item.pop(k, None)
            else:
                self.item[k] = v
        self.win._refresh_key_preview(self.win.sel)
        self.win.controller.request_render()
        super().reject()


class AppRuleDialog(QDialog):
    """Add an app-auto-switch rule: when <app> is focused, switch to a profile and/or page."""

    def __init__(self, cfg, parent=None, rule=None):
        super().__init__(parent)
        self.setWindowTitle("Edit auto-switch rule" if rule else "Add auto-switch rule")
        self.setMinimumWidth(360)
        form = QFormLayout(self)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.app_combo = QComboBox()
        self.app_combo.setEditable(True)
        self.app_combo.setInsertPolicy(QComboBox.NoInsert)
        self.app_combo.setToolTip("Pick a running program or type its .exe name (e.g. obs64.exe)")
        try:
            from .apppoller import running_app_names
            self.app_combo.addItems(running_app_names())
        except Exception:
            pass
        self.app_combo.setCurrentText((rule or {}).get("app", ""))
        form.addRow("When app is active", self.app_combo)

        self.profile_combo = QComboBox()
        self.profile_combo.addItem("(no change)", None)
        for nm in cfg.profile_names():
            self.profile_combo.addItem(nm, nm)
        form.addRow("Switch to profile", self.profile_combo)

        self.page_combo = QComboBox()
        self.page_combo.addItem("(no change)", None)
        for i, pg in enumerate(cfg.pages()):
            self.page_combo.addItem(f"{i + 1}. {pg.get('name', 'Page')}", i)
        form.addRow("…and / or page", self.page_combo)

        if rule:                                    # pre-fill when editing an existing rule
            pi = self.profile_combo.findData(rule.get("profile"))
            self.profile_combo.setCurrentIndex(pi if pi >= 0 else 0)
            gi = self.page_combo.findData(rule.get("page"))
            self.page_combo.setCurrentIndex(gi if gi >= 0 else 0)

        hint = QLabel("Leave one as “(no change)”. The exe name is matched case-insensitively; "
                      "page numbers refer to the target profile.", objectName="dim")
        hint.setWordWrap(True)
        form.addRow(hint)

        brow = QHBoxLayout()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        ok = QPushButton("Add"); ok.setObjectName("primary"); ok.setDefault(True)
        ok.clicked.connect(self.accept)
        brow.addStretch(1); brow.addWidget(cancel); brow.addWidget(ok)
        form.addRow(brow)

    def result_rule(self):
        app = self.app_combo.currentText().strip().lower()
        if not app:
            return None
        prof = self.profile_combo.currentData()
        page = self.page_combo.currentData()
        if not prof and page is None:
            return None
        return {"app": app, "profile": prof, "page": page}


class _CollapsibleSection(QWidget):
    """A header you click to show / hide a content widget (progressive disclosure)."""

    def __init__(self, title, content, expanded=False, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        self._btn = QToolButton()
        self._btn.setText(title.replace("&", "&&"))               # && so '&' isn't eaten as a mnemonic
        self._btn.setCheckable(True)
        self._btn.setChecked(expanded)
        self._btn.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self._btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.setObjectName("collsec")        # accent hover/focus lives in QSS so it follows the theme
        self._content = content
        content.setVisible(expanded)
        self._btn.clicked.connect(self._toggle)
        v.addWidget(self._btn)
        v.addWidget(content)

    def _toggle(self):
        on = self._btn.isChecked()
        self._btn.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self._content.setVisible(on)


class _ActionTile(QFrame):
    """An art tile in the action grid: a big (crisp PIL-rendered) emoji + short name, with
    hover + keyboard-highlight states."""
    picked = Signal(str)

    def __init__(self, t):
        super().__init__(objectName="actiontile")
        self.t = t
        self.setProperty("active", "false")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"{ACTION_LABELS.get(t, t)} — {ACTION_DESC.get(t, '')}")
        self.setFixedHeight(96)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 9, 6, 7)
        v.setSpacing(3)
        em = QLabel(objectName="tileemoji")
        em.setAlignment(Qt.AlignCenter)
        dpr = _dpr(self)
        art = (action_art(t, int(round(44 * dpr)))                       # custom glyph, else emoji
               or emoji_image(ACTION_EMOJI.get(t, ""), int(round(42 * dpr))))
        if art is not None:
            em.setPixmap(pil_to_pixmap(art, dpr))
        else:
            em.setText(ACTION_EMOJI.get(t, "•"))
        nm = QLabel(_SHORT.get(t, ACTION_LABELS.get(t, t)), objectName="tilename")
        nm.setAlignment(Qt.AlignCenter)
        nm.setWordWrap(True)
        v.addWidget(em)
        v.addWidget(nm)
        v.addStretch(1)

    def set_active(self, on):
        self.setProperty("active", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, ev):
        self.picked.emit(self.t)


class _CatHeader(QToolButton):
    """A clickable category header that expands / collapses its action rows (the 'submenu').
    Chevron + label only — the rows below carry the artwork, so headers stay calm."""

    def __init__(self, title):
        super().__init__()
        self.setText(f"  {title.replace('&', '&&')}")     # && so '&' isn't eaten as a mnemonic
        self.setCheckable(True)
        self.setArrowType(Qt.RightArrow)
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("cathdr")              # accent focus lives in QSS so it follows the theme

    def set_expanded(self, on):
        self.setChecked(on)
        self.setArrowType(Qt.DownArrow if on else Qt.RightArrow)


class _DragAction(QFrame):
    """Base for a draggable sidebar action (row or grid tile): drag onto a key/knob/button to
    bind it, click to assign to the current selection, right-click to pin/unpin a favorite."""
    picked = Signal(str)
    menu = Signal(str)

    def __init__(self, t, object_name):
        super().__init__(objectName=object_name)
        self.t = t
        self.setCursor(Qt.OpenHandCursor)
        self._press = None

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = ev.position().toPoint()

    def mouseMoveEvent(self, ev):
        if not (ev.buttons() & Qt.LeftButton) or self._press is None:
            return
        if (ev.position().toPoint() - self._press).manhattanLength() < 8:
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(ACTION_MIME, self.t.encode())
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)
        self._press = None

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._press is not None:
            if (ev.position().toPoint() - self._press).manhattanLength() < 8:
                self.picked.emit(self.t)        # a click, not a drag -> assign to current selection
        self._press = None

    def contextMenuEvent(self, ev):
        self.menu.emit(self.t)


# The sidebar action rail draws its glyphs in a calm neutral (not the mint accent) so the brand
# colour reads only on selection / active controls — the device key faces still use mint.
RAIL_ICON_COLOR = "#aec0b4"


class _ActionRow(_DragAction):
    """The list-view action item: emoji + full name in a row."""

    def __init__(self, t):
        super().__init__(t, "actionrow")
        self.setToolTip(f"{ACTION_LABELS.get(t, t)} — {ACTION_DESC.get(t, '')}\n"
                        f"Drag onto a key, knob or button · click to assign · right-click to favourite.")
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 5, 8, 5)
        h.setSpacing(9)
        em = QLabel(objectName="rowemoji")
        em.setFixedWidth(24)
        em.setAlignment(Qt.AlignCenter)
        dpr = _dpr(self)
        art = (action_art(t, int(round(22 * dpr)), color=RAIL_ICON_COLOR)
               or emoji_image(ACTION_EMOJI.get(t, ""), int(round(20 * dpr))))
        if art is not None:
            em.setPixmap(pil_to_pixmap(art, dpr))
        else:
            em.setText(ACTION_EMOJI.get(t, "•"))
        h.addWidget(em)
        nm = QLabel(ACTION_LABELS.get(t, t), objectName="rowname")
        nm.setWordWrap(False)
        h.addWidget(nm, 1)


class _ActionChip(_DragAction):
    """The grid-view action item: a compact icon tile + short name."""

    def __init__(self, t):
        super().__init__(t, "actionchip")
        self.setToolTip(f"{ACTION_LABELS.get(t, t)} — {ACTION_DESC.get(t, '')}\n"
                        f"Drag onto a key, knob or button · click to assign · right-click to favourite.")
        self.setFixedHeight(64)
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 7, 4, 5)
        v.setSpacing(2)
        em = QLabel(objectName="chipemoji")
        em.setAlignment(Qt.AlignCenter)
        dpr = _dpr(self)
        art = (action_art(t, int(round(26 * dpr)), color=RAIL_ICON_COLOR)
               or emoji_image(ACTION_EMOJI.get(t, ""), int(round(24 * dpr))))
        if art is not None:
            em.setPixmap(pil_to_pixmap(art, dpr))
        else:
            em.setText(ACTION_EMOJI.get(t, "•"))
        nm = QLabel(_SHORT.get(t, ACTION_LABELS.get(t, t)), objectName="chipname")
        nm.setAlignment(Qt.AlignCenter)
        nm.setWordWrap(True)
        v.addWidget(em)
        v.addWidget(nm)


class ActionPickerDialog(QDialog):
    """Grid action chooser. Browse = a Common grid + collapsible category grids (each category's
    actions appear as a grid right under its header — the 'second menu under the first'); typing
    flattens to a filtered grid. Arrow keys + Enter navigate."""
    _COLS = 3

    def __init__(self, current, parent=None):
        super().__init__(parent)
        self.chosen = None
        self._nav = -1
        self._expanded = {cat for cat, ts in ACTION_CATEGORIES if current in ts}   # open current's
        self.setWindowTitle("Choose an action")
        self.setMinimumSize(540, 600)
        v = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search actions…  (e.g. volume, light, page)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._rebuild())
        self.search.installEventFilter(self)            # arrow keys / Enter drive the grid
        v.addWidget(self.search)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._host = QWidget()
        self._box = QVBoxLayout(self._host)
        self._box.setSpacing(8)
        self._scroll.setWidget(self._host)
        v.addWidget(self._scroll, 1)
        self._blob = {t: f"{ACTION_LABELS.get(t, t)} {ACTION_DESC.get(t, '')} {t}".lower()
                      for t in ACTION_EMOJI}
        self._tiles = []
        self._rebuild()
        self.search.setFocus()

    def _grid(self, types):
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(8)
        for c in range(self._COLS):
            g.setColumnStretch(c, 1)
        for i, t in enumerate(types):
            tile = _ActionTile(t)
            tile.picked.connect(self._choose)
            g.addWidget(tile, i // self._COLS, i % self._COLS)
            self._tiles.append(tile)
        return w

    def _rebuild(self):
        while self._box.count():                        # clear (detach now so nothing lingers)
            it = self._box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif it.layout():
                it.layout().deleteLater()
        self._tiles = []
        self._nav = -1
        q = self.search.text().strip().lower()
        if q:                                           # search -> one flat grid of matches
            order = list(dict.fromkeys(                  # 'none' (clear) + commons + every category
                COMMON_ACTIONS + [t for _c, ts in ACTION_CATEGORIES for t in ts]))
            hits = [t for t in order if q in self._blob[t]]
            self._box.addWidget(self._grid(hits) if hits
                                else QLabel("No matching actions.", objectName="dim"))
        else:                                           # browse -> Common grid + category grids
            self._box.addWidget(QLabel("COMMON", objectName="cardtitle"))
            self._box.addWidget(self._grid(COMMON_ACTIONS))
            for cat, types in ACTION_CATEGORIES:
                hdr = _CatHeader(cat)
                hdr.set_expanded(cat in self._expanded)
                hdr.clicked.connect(lambda _=False, c=cat: self._toggle_cat(c))
                self._box.addWidget(hdr)
                if cat in self._expanded:
                    self._box.addWidget(self._grid(types))
        self._box.addStretch(1)
        if q and self._tiles:
            self._set_nav(0)                            # pre-select first hit so Enter just works

    def _toggle_cat(self, cat):
        self._expanded.discard(cat) if cat in self._expanded else self._expanded.add(cat)
        self._rebuild()

    def _choose(self, t):
        self.chosen = t
        self.accept()

    def _set_nav(self, idx):
        if not self._tiles:
            self._nav = -1
            return
        idx = max(0, min(len(self._tiles) - 1, idx))
        for i, tile in enumerate(self._tiles):
            tile.set_active(i == idx)
        self._nav = idx
        self._scroll.ensureWidgetVisible(self._tiles[idx], 0, 60)

    def _move(self, delta):
        if not self._tiles:
            return
        if self._nav < 0:
            self._set_nav(0 if delta > 0 else len(self._tiles) - 1)
        else:
            self._set_nav(self._nav + delta)

    def eventFilter(self, obj, ev):
        if obj is self.search and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k == Qt.Key_Right:
                self._move(1); return True
            if k == Qt.Key_Left:
                self._move(-1); return True
            if k == Qt.Key_Down:
                self._move(self._COLS); return True
            if k == Qt.Key_Up:
                self._move(-self._COLS); return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if self._tiles:
                    self._choose(self._tiles[self._nav if 0 <= self._nav < len(self._tiles) else 0].t)
                return True
        return super().eventFilter(obj, ev)


class _LiveTile(QFrame):
    """A live-data art tile: emoji + short name + the source's CURRENT value (refreshed live)."""
    picked = Signal(str)

    def __init__(self, source):
        super().__init__(objectName="actiontile")
        self.source = source
        self.setProperty("active", "false")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(livesrc.source_label(source))
        self.setFixedHeight(104)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 8, 6, 6)
        v.setSpacing(2)
        em = QLabel(objectName="tileemoji")
        em.setAlignment(Qt.AlignCenter)
        art = emoji_image(livesrc.source_emoji(source), int(round(34 * _dpr(self))))
        if art is not None:
            em.setPixmap(pil_to_pixmap(art, _dpr(self)))
        else:
            em.setText(livesrc.source_emoji(source))
        nm = QLabel(livesrc.source_short(source), objectName="tilename")
        nm.setAlignment(Qt.AlignCenter)
        nm.setWordWrap(True)
        self.vallbl = QLabel("", objectName="sliderval")
        self.vallbl.setAlignment(Qt.AlignCenter)
        v.addWidget(em)
        v.addWidget(nm)
        v.addWidget(self.vallbl)
        v.addStretch(1)
        self.refresh()

    def refresh(self):
        try:
            txt, _cap, _frac, _kind = livesrc.value(self.source)
        except Exception:
            txt = "--"
        self.vallbl.setText(txt if txt and txt != "--" else "—")

    def set_active(self, on):
        self.setProperty("active", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, ev):
        self.picked.emit(self.source)


class LiveDataPickerDialog(QDialog):
    """Grid chooser for the 24 live-data sources — categorised tiles with a LIVE value preview
    and a search box, so finding the right metric is far easier than a long dropdown."""
    _COLS = 3

    def __init__(self, current, parent=None):
        super().__init__(parent)
        self.chosen = None
        self._nav = -1
        self.setWindowTitle("Choose live data")
        self.setMinimumSize(560, 620)
        v = QVBoxLayout(self)
        intro = QLabel("Show a live metric on this key — the value updates on the dock every second. "
                       "Previews below are live too.", objectName="dim")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search live data…  (e.g. cpu, temp, gpu, net, fan)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._rebuild())
        self.search.installEventFilter(self)
        v.addWidget(self.search)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._host = QWidget()
        self._box = QVBoxLayout(self._host)
        self._box.setSpacing(8)
        self._scroll.setWidget(self._host)
        v.addWidget(self._scroll, 1)
        self._blob = {s: f"{livesrc.source_label(s)} {livesrc.source_short(s)} {s}".lower()
                      for s in livesrc.source_ids()}
        self._tiles = []
        self._rebuild()
        self._timer = QTimer(self)                       # keep the value previews ticking
        self._timer.timeout.connect(self._refresh_tiles)
        self._timer.start(1000)
        self.finished.connect(lambda _=0: self._timer.stop())   # stop ticking ~24 sources/s once the dialog closes
        self.search.setFocus()

    def _grid(self, sources):
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(8)
        for c in range(self._COLS):
            g.setColumnStretch(c, 1)
        for i, s in enumerate(sources):
            tile = _LiveTile(s)
            tile.picked.connect(self._choose)
            g.addWidget(tile, i // self._COLS, i % self._COLS)
            self._tiles.append(tile)
        return w

    def _rebuild(self):
        while self._box.count():
            it = self._box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._tiles = []
        self._nav = -1
        q = self.search.text().strip().lower()
        if q:                                            # search -> one flat grid of matches
            hits = [s for s in livesrc.source_ids() if q in self._blob[s]]
            self._box.addWidget(self._grid(hits) if hits
                                else QLabel("No matching live data.", objectName="dim"))
        else:                                            # browse -> a grid per category
            for cat, sources in livesrc.LIVE_CATEGORIES:
                self._box.addWidget(QLabel(cat.upper(), objectName="cardtitle"))
                self._box.addWidget(self._grid(sources))
        self._box.addStretch(1)
        if q and self._tiles:
            self._set_nav(0)

    def _refresh_tiles(self):
        for t in self._tiles:
            t.refresh()

    def _choose(self, s):
        self.chosen = s
        self.accept()

    def _set_nav(self, idx):
        if not self._tiles:
            self._nav = -1
            return
        idx = max(0, min(len(self._tiles) - 1, idx))
        for i, tile in enumerate(self._tiles):
            tile.set_active(i == idx)
        self._nav = idx
        self._scroll.ensureWidgetVisible(self._tiles[idx], 0, 60)

    def _move(self, delta):
        if not self._tiles:
            return
        self._set_nav(0 if self._nav < 0 else self._nav + delta)

    def eventFilter(self, obj, ev):
        if obj is self.search and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k == Qt.Key_Right:
                self._move(1); return True
            if k == Qt.Key_Left:
                self._move(-1); return True
            if k == Qt.Key_Down:
                self._move(self._COLS); return True
            if k == Qt.Key_Up:
                self._move(-self._COLS); return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if self._tiles:
                    self._choose(self._tiles[self._nav if 0 <= self._nav < len(self._tiles) else 0].source)
                return True
        return super().eventFilter(obj, ev)


class _DevicePanel(QWidget):
    """The device-canvas surface. Emits `resized` so the dock mock can scale up to fill it (so it
    isn't a tiny island of keys in a big, maximised window)."""
    resized = Signal()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.resized.emit()


class _TourOverlay(QWidget):
    """A first-run coach-mark overlay: dims the window and spotlights one widget at a time with a
    callout (Next / Skip). Clicks outside the callout are swallowed so the tour stays in control."""

    def __init__(self, win, steps, on_done):
        super().__init__(win.centralWidget())
        self.win = win
        self.steps = steps
        self._on_done = on_done
        self.i = 0
        self._hole = None
        self.setAttribute(Qt.WA_TranslucentBackground, True)   # so the Clear-punched hole shows through
        self.setGeometry(self.parentWidget().rect())
        self._card = QFrame(self, objectName="tourcard")
        cv = QVBoxLayout(self._card)
        cv.setContentsMargins(18, 15, 18, 14)
        cv.setSpacing(7)
        self._title = QLabel(objectName="tourtitle"); self._title.setWordWrap(True)
        self._body = QLabel(objectName="tourbody"); self._body.setWordWrap(True)
        cv.addWidget(self._title)
        cv.addWidget(self._body)
        row = QHBoxLayout()
        self._dots = QLabel(objectName="tourdots")
        row.addWidget(self._dots)
        row.addStretch(1)
        skip = QPushButton("Skip"); skip.setCursor(Qt.PointingHandCursor); skip.clicked.connect(self._finish)
        self._next = QPushButton("Next", objectName="primary"); self._next.setCursor(Qt.PointingHandCursor)
        self._next.clicked.connect(self._advance)
        row.addWidget(skip)
        row.addWidget(self._next)
        cv.addLayout(row)
        self._card.setFixedWidth(338)
        self.show()
        self.raise_()
        self._render_step()

    def _render_step(self):
        self.setGeometry(self.parentWidget().rect())
        st = self.steps[self.i]
        tgt = st.get("target")
        w = tgt() if callable(tgt) else None
        if w is not None and w.isVisible():
            tl = w.mapTo(self.parentWidget(), QPoint(0, 0))
            self._hole = QRect(tl, w.size()).adjusted(-6, -6, 6, 6)
        else:
            self._hole = None
        self._apply_mask()
        self._title.setText(st["title"])
        self._body.setText(st["body"])
        self._dots.setText("   ".join("●" if j == self.i else "○" for j in range(len(self.steps))))
        self._next.setText("Done" if self.i == len(self.steps) - 1 else "Next")
        self._card.adjustSize()
        self._place_card()
        self.update()

    def _place_card(self):
        r = self.rect(); cw = self._card.width(); ch = self._card.height()
        if self._hole is None:
            x = (r.width() - cw) // 2; y = (r.height() - ch) // 2
        else:
            h = self._hole
            x = min(max(12, h.center().x() - cw // 2), r.width() - cw - 12)
            if h.bottom() + ch + 24 <= r.height():
                y = h.bottom() + 18
            elif h.top() - ch - 18 >= 0:
                y = h.top() - ch - 18
            else:
                y = (r.height() - ch) // 2
        self._card.move(x, y)

    def _advance(self):
        if self.i >= len(self.steps) - 1:
            self._finish()
        else:
            self.i += 1
            self._render_step()

    def _finish(self):
        cb = self._on_done
        self._on_done = None
        if cb:
            cb()
        self.deleteLater()

    def _apply_mask(self):
        """Physically cut the spotlight hole out of the overlay so the real widget shows through
        (a child overlay can't reveal siblings via Clear-compositing, but a mask removes its pixels)."""
        reg = QRegion(self.rect())
        if self._hole is not None:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self._hole), 12, 12)
            reg = reg.subtracted(QRegion(path.toFillPolygon().toPolygon()))
        self.setMask(reg)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 165))      # dims everything except the masked-out hole
        if self._hole is not None:
            pen = QPen(QColor(T.ACCENT)); pen.setWidth(2)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(self._hole, 12, 12)

    def mousePressEvent(self, e):
        e.accept()        # swallow clicks outside the callout — the tour drives navigation

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape,):
            self._finish()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Right):
            self._advance()
        else:
            super().keyPressEvent(e)


def _theme_titlebar(widget):
    """Paint the native Windows 11 title bar (caption + text + border) to match the app's green, so
    the OS chrome connects with the UI instead of staying default grey. No-op off Windows 11."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        # DWMWA_CAPTION_COLOR=35, DWMWA_TEXT_COLOR=36, DWMWA_BORDER_COLOR=34; COLORREF = 0x00BBGGRR
        for attr, hx in ((35, T.CAPTION_BG), (36, T.TEXT), (34, T.CAPTION_BG)):
            h = hx.lstrip("#")
            colorref = (int(h[4:6], 16) << 16) | (int(h[2:4], 16) << 8) | int(h[0:2], 16)
            c = ctypes.c_uint(colorref)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(c), ctypes.sizeof(c))
    except Exception:
        pass


class ConfigWindow(QMainWindow):
    def __init__(self, controller: DockController):
        super().__init__()
        self.controller = controller
        self.cfg = controller.config
        self.cur_page = 0
        self.clipboard_binding = None        # copy/paste a key's whole binding
        self.clipboard_encoder = None        # copy/paste a knob's three sub-actions as one unit
        self.sel = "key1"
        self._gesture_slot = "tap"       # which gesture's action the inspector is editing
        self.view_folder = None          # folder id being edited in-place, or None
        self.view_folder_page = 0        # which page WITHIN that folder is being edited
        self._toggle_edit = 0            # which state of a toggle action the inspector is editing
        self._ready = False              # suppress the editor popup during startup
        self._quitting = False

        self.setWindowTitle(f"{APP_TITLE} — Configurator")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(1160, 760)
        self.resize(1410, 822)

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(250)
        self._render_timer.timeout.connect(self._persist_and_render)

        self.key_btns = {}
        self.slot_btns = {}

        # Stop the wheel from accidentally changing dropdowns/spins/sliders while scrolling.
        self._wheel_guard = _WheelGuard(self)
        QApplication.instance().installEventFilter(self._wheel_guard)

        # ---- edit history (undo / redo) ----------------------------------
        # Coalesced whole-config snapshots: every editor mutation calls cfg.save(); we wrap it to
        # arm a short timer, then snapshot once the burst settles (so typing = one undo step).
        import copy as _copy
        self._copy = _copy
        self._undo, self._redo = [], []
        self._restoring = True                 # suppress history while the UI builds + seeds defaults
        self._active_toast = None
        self._hist_base = _copy.deepcopy(self.cfg.data)
        self._hist_timer = QTimer(self)
        self._hist_timer.setSingleShot(True)
        self._hist_timer.setInterval(450)
        self._hist_timer.timeout.connect(self._commit_history)
        self._mirror_timer = QTimer(self)             # live keys tick in the editor while visible
        self._mirror_timer.setInterval(1000)
        self._mirror_timer.timeout.connect(self._tick_live_mirror)
        self._save_bridge = _SaveBridge()
        self._save_bridge.saved.connect(self._on_config_saved)     # queued -> GUI thread
        self._install_save_hook()

        self._build_ui()
        self.refresh()
        self.select("key1")
        self._restoring = False                # startup done — real edits now record history
        self._hist_base = _copy.deepcopy(self.cfg.data)
        for seq, slot in ((QKeySequence.Undo, self._undo_edit),
                          (QKeySequence.Redo, self._redo_edit),
                          (QKeySequence("Ctrl+Shift+Z"), self._redo_edit)):
            sc = QShortcut(seq, self)
            sc.activated.connect(slot)
        self._ready = True
        self._tour = None
        if not self.cfg.data.get("onboarded"):
            QTimer.singleShot(600, self._start_tour)   # first-run guided tour

    # ---- layout ------------------------------------------------------------
    def _build_ui(self):
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        body = QHBoxLayout(root)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        # LEFT column: header on top, the device canvas in the middle, the inspector along the bottom.
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        lv.addWidget(self._build_header_bar(), 0)
        # Device canvas above, inspector below, split by a draggable horizontal handle — grab the
        # seam to make the bottom editor taller or shorter.
        canvas = self._build_device_panel()
        canvas.setMinimumHeight(320)
        self._main_vsplit = QSplitter(Qt.Vertical, objectName="mainvsplit")
        self._main_vsplit.setChildrenCollapsible(False)
        self._main_vsplit.setHandleWidth(6)
        self._main_vsplit.addWidget(canvas)
        self._main_vsplit.addWidget(self._build_inspector())
        self._main_vsplit.setStretchFactor(0, 1)      # the canvas absorbs vertical slack on resize
        self._main_vsplit.setStretchFactor(1, 0)
        self._main_vsplit.setSizes([440, 366])
        lv.addWidget(self._main_vsplit, 1)
        # The main area and the actions sidebar are split by a draggable handle — grab the seam
        # between them to widen/narrow the sidebar.
        sidebar = self._build_actions_sidebar()
        sidebar.setMinimumWidth(210)
        self._body_split = QSplitter(Qt.Horizontal, objectName="bodysplit")
        self._body_split.setChildrenCollapsible(False)
        self._body_split.setHandleWidth(6)
        self._body_split.addWidget(left)
        self._body_split.addWidget(sidebar)
        self._body_split.setStretchFactor(0, 1)         # the main area takes the slack on resize
        self._body_split.setStretchFactor(1, 0)
        self._body_split.setSizes([1130, 280])
        body.addWidget(self._body_split)
        self._build_prefs_dialog()      # all settings now live behind the header gear

    @staticmethod
    def _hsep():
        line = QFrame(objectName="hsep")
        line.setFixedHeight(1)
        return line

    @staticmethod
    def _side_card(title):
        """A grouped sidebar section — a subtle rounded card with a small header."""
        card = QFrame(objectName="card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(12, 10, 12, 12)
        cv.setSpacing(8)
        cv.addWidget(QLabel(title.upper(), objectName="cardtitle"))
        return card, cv

    def _build_header_bar(self):
        """Stream Deck-style top header: app/device name + profile picker on the left;
        connection status + a settings gear on the right (all the old left-rail settings
        now live behind that gear)."""
        bar = QFrame(objectName="headerbar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(18, 10, 14, 10)
        h.setSpacing(12)
        idblock = QVBoxLayout()
        idblock.setContentsMargins(0, 0, 0, 0)
        idblock.setSpacing(2)
        idblock.addWidget(QLabel(APP_TITLE, objectName="hdrtitle"))
        prow = QHBoxLayout()
        prow.setContentsMargins(0, 0, 0, 0)
        prow.setSpacing(6)
        self.profile_combo = QComboBox(objectName="hdrprofile")
        self.profile_combo.setMinimumWidth(150)
        self.profile_combo.setToolTip("Active profile")
        self.profile_combo.currentTextChanged.connect(self._on_profile_changed)
        prow.addWidget(self.profile_combo, 0)
        add_p = GlyphButton("add", "＋", "New profile", object_name="hdraddbtn", size=13)
        add_p.clicked.connect(self._add_profile)
        prow.addWidget(add_p)
        prow.addStretch(1)
        idblock.addLayout(prow)
        h.addLayout(idblock)
        h.addStretch(1)
        self._undo_btn = GlyphButton("undo", "↶", "Undo  (Ctrl+Z)")
        self._undo_btn.setEnabled(False)
        self._undo_btn.clicked.connect(self._undo_edit)
        h.addWidget(self._undo_btn)
        self._redo_btn = GlyphButton("redo", "↷", "Redo  (Ctrl+Shift+Z)")
        self._redo_btn.setEnabled(False)
        self._redo_btn.clicked.connect(self._redo_edit)
        h.addWidget(self._redo_btn)
        self.conn_lbl = QLabel("…", objectName="dim")
        h.addWidget(self.conn_lbl)
        help_btn = GlyphButton("help", "?", "Replay the welcome tour")
        help_btn.clicked.connect(self._start_tour)
        h.addWidget(help_btn)
        gear = GlyphButton("settings", "⚙",
                           "Settings — brightness, accent, behaviour, app auto-switch, backups…")
        gear.clicked.connect(self._open_prefs)
        self._gear_btn = gear
        h.addWidget(gear)
        return bar

    _NAV_FLUENT = {"display": "brightness", "appearance": "color", "behaviour": "equalizer",
                   "appswitch": "taskview", "thispage": "photo", "backup": "save"}

    @staticmethod
    def _nav_icon(name):
        """A small monochrome glyph for a Settings section — Segoe Fluent when available (crisp
        at any display scale, selected row tints to the accent), else the painted fallback."""
        fl = ConfigWindow._NAV_FLUENT.get(name)
        if fl:
            ic = fluent_qicon(fl, normal=T.TEXT_DIM, active=T.ACCENT, size=18)
            if ic is not None:
                return ic
        import math
        pm = QPixmap(36, 36)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(T.TEXT_DIM))
        pen.setWidthF(2.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if name == "display":                                    # sun
            p.drawEllipse(QPointF(18, 18), 5.5, 5.5)
            for ang in range(0, 360, 45):
                a = math.radians(ang)
                p.drawLine(QPointF(18 + math.cos(a) * 9, 18 + math.sin(a) * 9),
                           QPointF(18 + math.cos(a) * 12, 18 + math.sin(a) * 12))
        elif name == "appearance":                               # half-filled disc (theme/contrast)
            p.drawEllipse(QPointF(18, 18), 9, 9)
            p.setBrush(QColor(T.TEXT_DIM))
            p.drawChord(QRectF(9, 9, 18, 18), -90 * 16, 180 * 16)
        elif name == "behaviour":                                # two sliders
            p.drawLine(QPointF(8, 14), QPointF(28, 14))
            p.drawLine(QPointF(8, 23), QPointF(28, 23))
            p.setBrush(QColor(T.KNOB_TOP))
            p.drawEllipse(QPointF(22, 14), 3.2, 3.2)
            p.drawEllipse(QPointF(13, 23), 3.2, 3.2)
        elif name == "appswitch":                                # two opposed arrows
            p.drawLine(QPointF(9, 14), QPointF(26, 14))
            p.drawLine(QPointF(26, 14), QPointF(22, 11))
            p.drawLine(QPointF(26, 14), QPointF(22, 17))
            p.drawLine(QPointF(27, 22), QPointF(10, 22))
            p.drawLine(QPointF(10, 22), QPointF(14, 19))
            p.drawLine(QPointF(10, 22), QPointF(14, 25))
        elif name == "thispage":                                 # picture
            p.drawRoundedRect(QRectF(8, 9, 20, 18), 2.5, 2.5)
            p.drawEllipse(QPointF(14, 15), 2, 2)
            p.drawPolyline([QPointF(10, 25), QPointF(16, 19), QPointF(20, 22), QPointF(26, 16)])
        elif name == "backup":                                   # floppy disk
            p.drawRoundedRect(QRectF(9, 9, 18, 18), 2.5, 2.5)
            p.drawLine(QPointF(13, 9), QPointF(13, 14))
            p.drawLine(QPointF(13, 14), QPointF(21, 14))
            p.drawLine(QPointF(21, 14), QPointF(21, 9))
            p.drawRect(QRectF(13, 19, 10, 6))
        p.end()
        return QIcon(pm)

    def _build_prefs_dialog(self):
        """A Windows-Settings-style two-pane dialog: section nav on the left, the selected
        section's controls on the right. Built once (eagerly, so refresh() can touch the
        widgets) and just shown/hidden by the header gear."""
        dlg = QDialog(self)
        dlg.setObjectName("prefsdialog")
        dlg.setWindowTitle("Settings")
        dlg.resize(640, 552)
        h = QHBoxLayout(dlg)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        nav = QListWidget(objectName="prefsnav")
        nav.setFixedWidth(184)
        nav.setFrameShape(QFrame.NoFrame)
        nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav.setIconSize(QSize(18, 18))
        stack = QStackedWidget()
        sections = [
            ("Display", "Display", self._prefs_display, "display"),
            ("Appearance", "Appearance", self._prefs_appearance, "appearance"),
            ("Behaviour", "Behaviour", self._prefs_behaviour, "behaviour"),
            ("App switching", "App switching", self._prefs_appswitch, "appswitch"),
            ("This page", "This page", self._prefs_thispage, "thispage"),
            ("Backup", "Backup & export", self._prefs_backup, "backup"),
        ]
        for navlabel, title, builder, icon in sections:
            it = QListWidgetItem(self._nav_icon(icon), navlabel, nav)
            stack.addWidget(self._prefs_page(title, builder()))
        def _switch_section(i):
            stack.setCurrentIndex(i)
            self._fade_in_transient(stack.currentWidget(), start=0.4, dur=140)
        nav.currentRowChanged.connect(_switch_section)
        nav.setCurrentRow(0)
        h.addWidget(nav)
        h.addWidget(stack, 1)
        self._refresh_accent_swatches()
        self._refresh_rules_list()
        self._prefs_dialog = dlg

    def _open_prefs(self):
        dlg = getattr(self, "_prefs_dialog", None)
        if dlg is None:
            return
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    @staticmethod
    def _prefs_page(title, content):
        """Wrap one settings section: a big header + its scrollable controls."""
        page = QWidget()
        pv = QVBoxLayout(page)
        pv.setContentsMargins(26, 22, 18, 18)
        pv.setSpacing(16)
        pv.addWidget(QLabel(title, objectName="prefshdr"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        pv.addWidget(scroll, 1)
        return page

    @staticmethod
    def _prefs_section(spacing=10):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 8, 0)
        v.setSpacing(spacing)
        return w, v

    def _prefs_display(self):
        w, v = self._prefs_section()
        v.addWidget(QLabel("Brightness", objectName="section"))
        brow = QHBoxLayout()
        brow.setSpacing(8)
        self.bright = QSlider(Qt.Horizontal)
        self.bright.setRange(0, 100)
        self.bright.setValue(self.cfg.brightness)
        self.bright.valueChanged.connect(self._on_brightness)
        # Debounce the disk write: the device tracks the slider live, but config.save() only fires
        # ~300ms after the last move (or immediately on release) instead of on every drag tick.
        self._bright_save_timer = QTimer(self)
        self._bright_save_timer.setSingleShot(True)
        self._bright_save_timer.setInterval(300)
        self._bright_save_timer.timeout.connect(self._save_brightness)
        self.bright.sliderReleased.connect(self._save_brightness)
        self.bright_val = QLabel(f"{self.cfg.brightness}%", objectName="dim")
        self.bright_val.setFixedWidth(40)
        self.bright_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        brow.addWidget(self.bright, 1)
        brow.addWidget(self.bright_val)
        v.addLayout(brow)
        d1 = QLabel("How brightly the dock's keys and screen glow.", objectName="dim")
        d1.setWordWrap(True)
        v.addWidget(d1)
        v.addSpacing(18)
        v.addWidget(QLabel("Key image alignment", objectName="section"))
        d2 = QLabel("Icons can sit slightly off-centre on the physical keys. Calibrate their size "
                    "and position once and preview it live on the device.", objectName="dim")
        d2.setWordWrap(True)
        v.addWidget(d2)
        v.addSpacing(6)
        calib_btn = QPushButton("Display calibration…")
        calib_btn.setToolTip("Fine-tune how images sit on the dock's keys (size + position), live")
        calib_btn.clicked.connect(self._open_calibration)
        calib_btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        v.addWidget(calib_btn)

        v.addSpacing(18)
        v.addWidget(QLabel("Idle screen", objectName="section"))
        idle = self.cfg.data.setdefault("idle", {"enabled": True, "delay": 120, "dim": 25})
        self.idle_chk = QCheckBox("Show an ambient clock when the dock is idle")
        self.idle_chk.setChecked(bool(idle.get("enabled", True)))
        self.idle_chk.toggled.connect(self._toggle_idle)
        v.addWidget(self.idle_chk)
        d3 = QLabel("After a while untouched, the six screens dim and show a clock; the next press "
                    "wakes it (that press is ignored, not fired).", objectName="dim")
        d3.setWordWrap(True)
        v.addWidget(d3)
        v.addSpacing(4)
        irow = QHBoxLayout()
        irow.setSpacing(8)
        irow.addWidget(QLabel("Show after", objectName="dim"))
        self.idle_delay = QSpinBox()
        self.idle_delay.setRange(10, 3600)
        self.idle_delay.setSingleStep(10)
        self.idle_delay.setSuffix(" s")
        self.idle_delay.setValue(int(idle.get("delay", 120)))
        self.idle_delay.valueChanged.connect(self._set_idle_delay)
        irow.addWidget(self.idle_delay)
        irow.addSpacing(14)
        irow.addWidget(QLabel("Dim to", objectName="dim"))
        self.idle_dim = QSpinBox()
        self.idle_dim.setRange(2, 100)
        self.idle_dim.setSingleStep(5)
        self.idle_dim.setSuffix(" %")
        self.idle_dim.setValue(int(idle.get("dim", 25)))
        self.idle_dim.valueChanged.connect(self._set_idle_dim)
        irow.addWidget(self.idle_dim)
        irow.addStretch(1)
        v.addLayout(irow)
        v.addSpacing(6)
        srow = QHBoxLayout()
        srow.setSpacing(8)
        srow.addWidget(QLabel("Design", objectName="dim"))
        self.idle_style = QComboBox()
        dpr = _dpr(self)
        for s in images.AMBIENT_STYLE_ORDER:                # each design shows a live mini-board
            pv = images.ambient_preview(s, target_w=int(round(96 * dpr)))
            pm = pil_to_pixmap(pv, dpr)
            ic = QIcon(pm)
            ic.addPixmap(pm, QIcon.Selected)                # pin: no auto-lightened hover blend
            ic.addPixmap(pm, QIcon.Active)
            self.idle_style.addItem(ic, images.AMBIENT_STYLE_LABELS.get(s, s), s)
        self.idle_style.setIconSize(QSize(96, 62))
        cur = str(idle.get("style", "classic") or "classic")
        ci = self.idle_style.findData(cur)
        self.idle_style.setCurrentIndex(ci if ci >= 0 else 0)
        self.idle_style.currentIndexChanged.connect(
            lambda i: self._set_idle_style(self.idle_style.itemData(i)))
        srow.addWidget(self.idle_style, 1)
        v.addLayout(srow)
        d4 = QLabel("The animated designs drift gently; the others hold a still frame.",
                    objectName="dim")
        d4.setWordWrap(True)
        v.addWidget(d4)
        v.addSpacing(10)
        v.addWidget(QLabel("Dynamic content", objectName="section"))
        self.idle_playing = QCheckBox("Show album art while music is playing")
        self.idle_playing.setToolTip("When something is playing, the idle screen shows the cover "
                                     "art + a scrolling title instead of the clock.")
        self.idle_playing.setChecked(bool(idle.get("playing", True)))
        self.idle_playing.toggled.connect(lambda on: self._set_idle_flag("playing", on))
        v.addWidget(self.idle_playing)
        self.idle_weather = QCheckBox("Rotate a weather screen with the clock")
        self.idle_weather.setToolTip("The idle screen alternates between the clock design and a "
                                     "weather screen (temperature, conditions, hi/lo) every ~14 s.")
        self.idle_weather.setChecked(bool(idle.get("weather", False)))
        self.idle_weather.toggled.connect(lambda on: self._set_idle_flag("weather", on))
        v.addWidget(self.idle_weather)
        v.addStretch(1)
        return w

    def _toggle_idle(self, on):
        self.cfg.data.setdefault("idle", {})["enabled"] = bool(on)
        self.cfg.save()

    def _set_idle_delay(self, val):
        self.cfg.data.setdefault("idle", {})["delay"] = int(val)
        self.cfg.save()

    def _set_idle_dim(self, val):
        self.cfg.data.setdefault("idle", {})["dim"] = int(val)
        self.cfg.save()

    def _set_idle_flag(self, key, on):
        self.cfg.data.setdefault("idle", {})[key] = bool(on)
        self.cfg.save()                # a showing ambient picks it up on its next advance

    def _set_idle_style(self, tok):
        self.cfg.data.setdefault("idle", {})["style"] = str(tok or "classic")
        self.cfg.save()                # a showing ambient picks it up on its next advance

    def _prefs_appearance(self):
        w, v = self._prefs_section()
        v.addWidget(QLabel("Accent colour", objectName="section"))
        trow = QHBoxLayout()
        trow.setSpacing(8)
        self._accent_swatches = {}
        for name in ("mint", "blue", "violet", "amber", "pink"):
            col = T.ACCENTS[name][0]
            sw = QPushButton()
            sw.setFixedSize(28, 28)
            sw.setCheckable(True)
            sw.setCursor(Qt.PointingHandCursor)
            sw.setToolTip(name.title())
            sw.setStyleSheet(f"QPushButton {{ background:{col}; border:2px solid transparent; "
                             f"border-radius:14px; }} QPushButton:checked {{ border:2px solid {T.TEXT}; }}")
            sw.clicked.connect(lambda _=False, n=name: self._set_accent(n))
            self._accent_swatches[name] = sw
            trow.addWidget(sw)
        trow.addStretch(1)
        v.addLayout(trow)
        v.addSpacing(8)
        self.titles_chk = QCheckBox("Labels under icons")
        self.titles_chk.setToolTip("Show text labels under the icons on the keys")
        self.titles_chk.setChecked(self.cfg.data.get("show_labels", True))
        self.titles_chk.toggled.connect(self._toggle_titles)
        v.addWidget(self.titles_chk)
        self.autoicon_chk = QCheckBox("Auto icons for actions")
        self.autoicon_chk.setToolTip("Give keys with no icon a crisp built-in Fluent icon based on their "
                                     "action (mic, folder, launch, hotkey, …)")
        self.autoicon_chk.setChecked(self.cfg.data.get("auto_icons", True))
        self.autoicon_chk.toggled.connect(self._toggle_auto_icons)
        v.addWidget(self.autoicon_chk)
        v.addSpacing(6)
        v.addWidget(QLabel("Live-data key style", objectName="section"))
        self.live_combo = QComboBox()
        self.live_combo.setFocusPolicy(Qt.StrongFocus)
        self.live_combo.setToolTip("Design for live-data keys (CPU/RAM/GPU/disk gauges)")
        for tok in LIVE_STYLE_ORDER:
            self.live_combo.addItem(LIVE_STYLE_LABELS.get(tok, tok), tok)
        li = self.live_combo.findData(self.cfg.data.get("live_style", "gauge"))
        self.live_combo.setCurrentIndex(li if li >= 0 else 0)
        self.live_combo.currentIndexChanged.connect(
            lambda _i: self._set_live_style(self.live_combo.currentData()))
        v.addWidget(self.live_combo)
        v.addWidget(QLabel("Folder open / close animation", objectName="section"))
        self.folder_combo = QComboBox()
        self.folder_combo.setFocusPolicy(Qt.StrongFocus)
        self.folder_combo.setToolTip("How a folder opens / closes on the dock")
        for tok in FOLDER_ANIM_ORDER:
            self.folder_combo.addItem(FOLDER_ANIM_LABELS.get(tok, tok), tok)
        fi = self.folder_combo.findData(self.cfg.data.get("folder_anim", "zoom"))
        self.folder_combo.setCurrentIndex(fi if fi >= 0 else 0)
        self.folder_combo.currentIndexChanged.connect(
            lambda _i: self._set_folder_anim(self.folder_combo.currentData()))
        v.addWidget(self.folder_combo)
        v.addStretch(1)
        return w

    def _set_folder_back(self, tok):
        tok = tok if tok in ("key6", "btn7", "btn8", "btn9") else "key6"
        if tok == self.cfg.data.get("folder_back", "key6"):
            return
        self.cfg.data["folder_back"] = tok
        self.cfg.save()
        self._refresh_all_slots()                     # key6 flips between Back tile <-> content key
        if self.view_folder is not None:
            self.select(self.sel)                     # rebuild the inspector for the now-(non)Back key6
        self.controller.request_render()
        if tok == "key6":
            self.toast("Folders exit with the 6th key", "ok")
        else:
            self.toast(f"Folders now exit with {tok} — the 6th key is free for content", "ok")

    def _prefs_behaviour(self):
        w, v = self._prefs_section()
        self.pressfx_chk = QCheckBox("Press effects")
        self.pressfx_chk.setToolTip("Play an animation on a key when you press it on the dock")
        self.pressfx_chk.setChecked(self.cfg.data.get("press_fx", True))
        self.pressfx_chk.toggled.connect(self._toggle_pressfx)
        v.addWidget(self.pressfx_chk)
        self.anim_combo = QComboBox()
        self.anim_combo.setFocusPolicy(Qt.StrongFocus)
        self.anim_combo.setToolTip("Press-effect style played on the dock")
        for tok in PRESS_ANIM_ORDER:
            self.anim_combo.addItem(PRESS_ANIM_LABELS.get(tok, tok), tok)
        i = self.anim_combo.findData(self.cfg.data.get("press_anim", "bounce"))
        self.anim_combo.setCurrentIndex(i if i >= 0 else 0)
        self.anim_combo.setEnabled(self.pressfx_chk.isChecked())
        self.anim_combo.currentIndexChanged.connect(
            lambda _i: self._set_press_anim(self.anim_combo.currentData()))
        v.addWidget(self.anim_combo)
        v.addSpacing(8)
        self.encaccel_chk = QCheckBox("Encoder acceleration")
        self.encaccel_chk.setToolTip("Spin a knob fast for bigger jumps (volume / brightness / "
                                     "colour); turn slowly for fine control")
        self.encaccel_chk.setChecked(self.cfg.data.get("encoder_accel", True))
        self.encaccel_chk.toggled.connect(self._toggle_encaccel)
        v.addWidget(self.encaccel_chk)
        hint = QLabel("Spin a knob fast for bigger jumps; turn slowly for fine control.",
                      objectName="dim")
        hint.setWordWrap(True)
        v.addWidget(hint)
        v.addSpacing(10)
        v.addWidget(QLabel("Folders", objectName="section"))
        fbhint = QLabel("Exit a folder with the 6th-key Back tile, or hide that tile and use a round "
                        "button to go back (frees the 6th key for content). btn7/btn8 still flip folder pages.",
                        objectName="dim")
        fbhint.setWordWrap(True)
        v.addWidget(fbhint)
        self.folderback_combo = QComboBox()
        self.folderback_combo.setFocusPolicy(Qt.StrongFocus)
        self.folderback_combo.setToolTip("How you go back / exit a folder.")
        for tok, lbl in (("key6", "Show Back tile on the 6th key"),
                         ("btn9", "No Back tile — go back with btn9  (Mic button)"),
                         ("btn7", "No Back tile — go back with btn7  (Prev-page button)"),
                         ("btn8", "No Back tile — go back with btn8  (Next-page button)")):
            self.folderback_combo.addItem(lbl, tok)
        bi = self.folderback_combo.findData(self.cfg.data.get("folder_back", "key6"))
        self.folderback_combo.setCurrentIndex(bi if bi >= 0 else 0)
        self.folderback_combo.currentIndexChanged.connect(
            lambda _i: self._set_folder_back(self.folderback_combo.currentData()))
        v.addWidget(self.folderback_combo)
        v.addSpacing(10)
        v.addWidget(QLabel("Weather", objectName="section"))
        self.weather_edit = QLineEdit(self.cfg.data.get("weather", ""))
        self.weather_edit.setPlaceholderText("City (e.g. Budapest) · or 'lat,lon' · blank = auto by IP")
        self.weather_edit.setToolTip("Used by the Weather live-data keys. Blank locates you by IP "
                                     "(often wrong on a VPN). Powered by Open-Meteo — no account needed.")
        self.weather_edit.editingFinished.connect(self._set_weather_loc)
        v.addWidget(self.weather_edit)
        self.weather_status = QLabel("", objectName="dim")
        self.weather_status.setWordWrap(True)
        v.addWidget(self.weather_status)
        urow = QHBoxLayout(); urow.setSpacing(8)
        urow.addWidget(QLabel("Units"))
        self.weather_units = QComboBox()
        self.weather_units.addItem("Celsius  (°C, km/h)", "c")
        self.weather_units.addItem("Fahrenheit  (°F, mph)", "f")
        _u = "f" if str(self.cfg.data.get("weather_units", "c")).lower().startswith("f") else "c"
        self.weather_units.setCurrentIndex(max(0, self.weather_units.findData(_u)))
        self.weather_units.currentIndexChanged.connect(
            lambda _i: self._set_weather_units(self.weather_units.currentData()))
        urow.addWidget(self.weather_units, 1)
        v.addLayout(urow)
        wcbtn = QPushButton("Build Weather center on a key")
        wcic = fluent_qicon("cloud", normal=T.TEXT_DIM, active=T.TEXT, size=15, widget=wcbtn)
        if wcic is not None:
            wcbtn.setIcon(wcic)
            wcbtn.setIconSize(QSize(15, 15))
        wcbtn.setToolTip("Creates a folder of weather tiles (forecast · UV · rain · wind · humidity · "
                         "sun) on the selected key, or the first free one.")
        wcbtn.clicked.connect(self._build_weather_center_from_panel)
        v.addWidget(wcbtn)
        v.addStretch(1)
        self._update_weather_status()
        return w

    def _update_weather_status(self):
        try:
            from . import live
            place = live.weather_place_label()
        except Exception:
            place = ""
        cur = (self.cfg.data.get("weather", "") or "").strip()
        if place:
            self.weather_status.setText(f"📍  Using:  {place}")
        elif cur:
            self.weather_status.setText("Locating…")
        else:
            self.weather_status.setText("Auto-locating by IP — set a city for accuracy on a VPN.")

    def _set_weather_loc(self):
        place = self.weather_edit.text().strip()
        if place == (self.cfg.data.get("weather", "") or ""):
            return
        self.cfg.data["weather"] = place
        self.cfg.save()
        try:
            from . import live
            live.set_weather_location(place)
            self.controller.refresh_live()
        except Exception:
            pass
        self.weather_status.setText("Locating…")
        QTimer.singleShot(2000, self._update_weather_status)   # show the resolved place once fetched

    def _set_weather_units(self, u):
        u = "f" if str(u or "").lower().startswith("f") else "c"
        if u == (self.cfg.data.get("weather_units", "c") or "c"):
            return
        self.cfg.data["weather_units"] = u
        self.cfg.save()
        try:
            from . import live
            live.set_weather_units(u)
            self.controller.refresh_live()
        except Exception:
            pass
        QTimer.singleShot(2000, self._update_weather_status)

    def _build_weather_center_from_panel(self):
        was_in_folder = self.view_folder is not None
        if was_in_folder:                              # never nest a Weather center inside a folder
            self._exit_folder_edit()                   # NB: this resets self.sel to "key1"

        def _empty(k):
            it = self.page().setdefault("items", {}).get(k) or {}
            return not it.get("live") and (it.get("action") or {}).get("type", "none") in ("", "none")
        target = next((k for k in LCD_KEYS if _empty(k)), None)   # prefer a free key — never clobber blindly
        # Only fall back to the selected key if we did NOT just force a folder exit (which pinned sel=key1).
        if target is None and not was_in_folder and self.sel.startswith("key") and not self._is_back_key(self.sel):
            target = self.sel                          # page full -> replace the key the user has selected
        if target is None:
            self.toast("Select a free key for the Weather center first", "warn")
            return
        self.select(target)
        self._build_weather_center(target)

    def _prefs_appswitch(self):
        w, v = self._prefs_section()
        self.auto_chk = QCheckBox("Switch by active app")
        self.auto_chk.setToolTip("When the focused program changes, jump to a matching profile / page")
        self.auto_chk.setChecked(bool(self.cfg.data.get("auto_switch", False)))
        self.auto_chk.toggled.connect(self._toggle_auto_switch)
        v.addWidget(self.auto_chk)
        v.addWidget(QLabel("When a program is in front, jump to its profile / page.", objectName="dim"))
        self.rules_list = QListWidget()
        self.rules_list.setMinimumHeight(140)
        self.rules_list.setToolTip("Rules: which app switches to which profile / page (double-click to edit)")
        self.rules_list.itemDoubleClicked.connect(lambda _it: self._edit_app_rule())
        v.addWidget(self.rules_list, 1)
        rrow = QHBoxLayout()
        addr = QPushButton("＋ Add"); addr.clicked.connect(self._add_app_rule)
        remr = QPushButton("－ Remove"); remr.clicked.connect(self._remove_app_rule)
        rrow.addWidget(addr); rrow.addWidget(remr); rrow.addStretch(1)
        v.addLayout(rrow)
        return w

    def _prefs_thispage(self):
        w, v = self._prefs_section()
        v.addWidget(QLabel("Apply an image across the whole current page.", objectName="dim"))
        fs_btn = QPushButton("Full-screen image…")
        fs_btn.clicked.connect(self._set_fullscreen_image)
        v.addWidget(fs_btn)
        panel_btn = QPushButton("Animated wallpaper… (GIF)")
        panel_btn.setToolTip("Play an animated GIF (or static image) across all 6 keys on this page")
        panel_btn.clicked.connect(self._set_panel_wallpaper)
        v.addWidget(panel_btn)
        fs_clear = QPushButton("Clear images / wallpaper")
        fs_clear.clicked.connect(self._clear_page_images)
        v.addWidget(fs_clear)
        v.addStretch(1)
        return w

    def _prefs_backup(self):
        w, v = self._prefs_section()
        note = QLabel("Your whole setup saves automatically — use this to keep a history, restore "
                      "an older version, or export / import it to another PC.", objectName="dim")
        note.setWordWrap(True)
        v.addWidget(note)
        backups_btn = QPushButton("Backups && export…")
        backups_btn.setToolTip("Backup history, restore, export / import")
        backups_btn.clicked.connect(self._open_backups)
        v.addWidget(backups_btn)
        v.addStretch(1)
        return w

    def _rescale_device(self):
        """Scale the dock mock (keys, knobs, buttons, gaps) to fill the canvas — so it grows in a
        maximised window instead of sitting tiny in the middle."""
        panel = getattr(self, "_dev_panel", None)
        if panel is None or not hasattr(self, "_dev_grid"):
            return
        availW = max(1, panel.width() - 44)
        availH = max(1, panel.height() - 128)         # leave room for the banner pill + page row
        natW, natH = 440, 278                          # device frame natural size at scale 1.0
        s = max(1.0, min(min(availW / natW, availH / natH), 3.0))
        if abs(s - getattr(self, "_dev_scale", 0.0)) < 0.03:
            return
        self._dev_scale = s
        for b in self.key_btns.values():
            b.rescale(s)
        for b in self.slot_btns.values():
            b.rescale(s)
        self._dev_grid.setHorizontalSpacing(round(8 * s))
        self._dev_grid.setVerticalSpacing(round(8 * s))
        self._dev_cols.setSpacing(round(18 * s))
        self._dev_left.setSpacing(round(8 * s))
        self._dev_right.setSpacing(round(10 * s))
        self._dev_small.setSpacing(round(16 * s))
        self._dev_dv.setContentsMargins(round(14 * s), round(9 * s), round(14 * s), round(10 * s))

    def _build_device_panel(self):
        panel = _DevicePanel(objectName="main")
        self._dev_panel = panel
        panel.resized.connect(self._rescale_device)
        v = QVBoxLayout(panel)
        v.setContentsMargins(10, 6, 10, 6)
        v.setSpacing(10)

        # The active profile now lives in the header dropdown; the page switcher (numbered pills)
        # is built at the BOTTOM.
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)

        # A quiet, centred status pill shown only while the dock is unplugged (editing still
        # works). Deliberately low-key — the device, not this notice, stays the hero (Elgato
        # handles "no device" the same way: a small chip, never a glowing full-width bar).
        self._nodev_banner = QLabel("○   Dock not connected — your layout is saved and applies "
                                    "the moment you plug in", objectName="nodevbanner")
        self._nodev_banner.setVisible(False)
        nb_row = QHBoxLayout()
        nb_row.addStretch(1)
        nb_row.addWidget(self._nodev_banner)
        nb_row.addStretch(1)
        v.addLayout(nb_row)

        # The dock drawn to the real AKP03's proportions: 6 keys top-left, big knob
        # top-right, 3 small buttons bottom-left, 2 medium knobs bottom-right. Each control
        # is clickable and opens its settings in a popup — nothing else fills the window.
        device = QFrame(objectName="device")
        shadow = QGraphicsDropShadowEffect(device)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 7)
        shadow.setColor(QColor(0, 0, 0, 150))
        device.setGraphicsEffect(shadow)
        self._dev_frame = device
        dv = QVBoxLayout(device)
        dv.setContentsMargins(14, 9, 14, 10)
        dv.setSpacing(8)
        self._dev_dv = dv
        wm = QHBoxLayout()
        wm.addWidget(QLabel("AJAZZ", objectName="wordmark"))
        wm.addStretch(1)
        wm.addWidget(QLabel("AKP03", objectName="wordmark"))
        dv.addLayout(wm)

        cols = QHBoxLayout()
        cols.setSpacing(18)
        self._dev_cols = cols

        left = QVBoxLayout()
        left.setSpacing(8)
        self._dev_left = left
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self._dev_grid = grid
        for i, kid in enumerate(LCD_KEYS):
            b = KeyTile(kid)
            b.win = self                      # enables drag-to-swap + right-click menu
            b.clicked.connect(lambda k=kid: self.select(k))
            self.key_btns[kid] = b
            grid.addWidget(b, i // 3, i % 3)
        # one round button centered under each key column
        for col, n in enumerate((7, 8, 9)):
            grid.addWidget(self._dev_button(f"btn{n}", f"Button {n}"), 2, col, Qt.AlignHCenter)
        left.addLayout(grid)
        shared_hint = QLabel("round buttons — shared on every page", objectName="scopehint")
        shared_hint.setAlignment(Qt.AlignCenter)
        left.addWidget(shared_hint)
        left.addStretch(1)

        # Physical mapping (verified on the device): the big knob is Encoder 1, the
        # bottom-left small knob is Encoder 0, the bottom-right small knob is Encoder 2.
        right = QVBoxLayout()
        right.setSpacing(10)
        self._dev_right = right
        # Match the real device: a large knob sitting a little below centre, with the two smaller
        # knobs lifted up off the bottom edge.
        right.addStretch(3)
        right.addWidget(self._dev_knob(1, big=True), 0, Qt.AlignHCenter)
        right.addStretch(2)
        small = QHBoxLayout()
        small.setSpacing(16)
        self._dev_small = small
        small.addWidget(self._dev_knob(0))
        small.addWidget(self._dev_knob(2))
        right.addLayout(small)
        right.addStretch(1)

        cols.addLayout(left)
        cols.addStretch(1)
        cols.addLayout(right)
        dv.addLayout(cols)

        center = QHBoxLayout()
        center.addStretch(1)
        center.addWidget(device)
        center.addStretch(1)

        # Page switcher at the BOTTOM: "Pages:" + numbered pills + add (active page highlighted).
        self.pages_row = QHBoxLayout()
        self.pages_row.setSpacing(6)
        pages_bar = QWidget()
        pages_bar.setLayout(self.pages_row)

        # Only the device + its page switcher live on the editor surface — the PC-stats row,
        # now-playing card and brightness/keys chips were removed at the user's request so the
        # Ajazz dock is the single thing shown here.
        v.addSpacing(6)
        v.addLayout(center)
        v.addSpacing(10)
        v.addWidget(pages_bar)
        v.addStretch(1)
        return panel

    def _dev_button(self, sid, label):
        # No visible caption (cleaner) — identity lives in the tooltip + accessible name.
        b = CircleControl(40, knob=False)
        b.win = self
        b.sid = sid
        b.setToolTip(f"{label} — click to edit · right-click to copy / paste")
        b.setAccessibleName(label)
        b.clicked.connect(lambda s=sid: self.select(s))
        self.slot_btns[sid] = b
        return b

    def _dev_knob(self, n, big=False):
        # The top knob (Encoder 1) reads a touch larger than the two lower knobs, matching how the
        # real AKP03E looks; all are kept compact so the device mock stays small.
        base = f"enc{n}"
        b = CircleControl(96 if big else 52, knob=True)
        b.win = self                          # REQUIRED for the right-click menu to fire
        b.setToolTip(f"Encoder {n} (per page) — a 3-in-1 dial: click to edit · "
                     f"right-click to copy / paste the whole dial")
        b.setAccessibleName(f"Encoder {n}")
        b.sid = base                          # enables right-click copy/paste of the whole dial
        b.clicked.connect(lambda s=f"{base}-": self.select(s))
        self.slot_btns[base] = b
        return b

    def _build_inspector(self):
        """The control editor, docked as a full-width bar along the BOTTOM (Stream Deck style).
        Each section is a 'floating' card (its own elevated surface + shadow, like the device);
        the row scrolls HORIZONTALLY if the cards overflow — cards never scroll vertically."""
        insp = QFrame(objectName="inspector")
        insp.setMinimumHeight(350)                    # a floor; the vertical splitter sets its size
        v = QVBoxLayout(insp)
        v.setContentsMargins(18, 10, 14, 12)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.ed_title = QLabel("", objectName="h1")
        head.addWidget(self.ed_title)
        self.ed_scope = QLabel("", objectName="dim")
        head.addWidget(self.ed_scope)
        head.addStretch(1)
        self._ed_test_btn = QPushButton("Test", objectName="edtest")
        tic = fluent_qicon("play", normal=T.TEXT_DIM, active=T.TEXT, size=13,
                           widget=self._ed_test_btn)
        if tic is not None:
            self._ed_test_btn.setIcon(tic)
            self._ed_test_btn.setIconSize(QSize(13, 13))
        self._ed_test_btn.setToolTip("Run this control's action once, right now")
        self._ed_test_btn.clicked.connect(self._test_current)
        head.addWidget(self._ed_test_btn)
        self._ed_clear_btn = QPushButton("Clear", objectName="edclear")
        self._ed_clear_btn.setToolTip("Reset this control")
        self._ed_clear_btn.clicked.connect(self._clear_current)
        head.addWidget(self._ed_clear_btn)
        v.addLayout(head)
        # The editor sections live in a horizontal SPLITTER — grab the seam between two cards to
        # re-apportion their width (drag the Action / Appearance boundary).
        self.editor_cols = QSplitter(Qt.Horizontal, objectName="editorsplit")
        self.editor_cols.setChildrenCollapsible(False)
        self.editor_cols.setHandleWidth(6)
        self.editor_host = self.editor_cols          # _fit_inspector_width walks its children
        self._card_prefs = []
        v.addWidget(self.editor_cols, 1)
        return insp

    def _add_editor_col(self, content, width=360, title=None):
        """Add one editor section as a fixed-width 'floating' card: its own elevated surface +
        drop shadow, sized to its content and top-aligned. The inspector row scrolls horizontally
        if the cards overflow; cards do not scroll vertically (the bar is tall enough)."""
        card = QFrame(objectName="editorcard")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(16, 10, 16, 11)
        cv.setSpacing(7)
        if title:
            cv.addWidget(QLabel(title, objectName="cardhdr"))
        cv.addWidget(content)
        cv.addStretch(1)                              # content stays top-anchored; the surface fills
        card.setMinimumWidth(min(width, 300))         # resizable: a floor, not a fixed width
        card.setMinimumHeight(240)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)   # fill the pane (no dead band)
        holder = QWidget()                            # tiled — no shadow gutter; cards share a 1px seam
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(card)                            # the card fills its splitter pane
        holder.setMinimumWidth(min(width, 300) + 12)
        self._card_prefs.append(width + 12)           # preferred pane width for the initial split
        self.editor_cols.addWidget(holder)
        return card

    # ---- right-hand actions sidebar (drag onto a control to assign) --------
    def _build_actions_sidebar(self):
        """Persistent right-hand actions list (Stream Deck style): searchable + categorized,
        with Favourites / Recent / Common at the top and a grid-or-list view toggle. Drag a row
        onto a key / knob / button — or click it to bind to the currently-selected control —
        or drag a program / file / URL straight from Windows onto a key."""
        bar = QFrame(objectName="actionsidebar")
        bar.setFixedWidth(272)
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(12, 12, 8, 12)
        outer.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(6)
        _act_hdr = QLabel("ACTIONS", objectName="cardtitle")
        _act_hdr.setToolTip("Drag onto a key, knob or button to bind it — or drag a program, file or "
                            "link from Windows straight onto a key. Right-click any action to ★ pin it.")
        head.addWidget(_act_hdr)
        head.addStretch(1)
        self._act_grid = bool(self.cfg.data.get("actions_grid", False))
        self._grid_btn = QToolButton(objectName="viewtoggle")
        self._grid_btn.setCheckable(True)
        self._grid_btn.setChecked(self._act_grid)
        self._grid_btn.setCursor(Qt.PointingHandCursor)
        self._grid_btn.setToolTip("Toggle grid / list view")
        self._grid_btn.clicked.connect(self._toggle_grid)
        self._sync_grid_btn_face()
        head.addWidget(self._grid_btn)
        outer.addLayout(head)
        self._act_search = QLineEdit()
        self._act_search.setPlaceholderText("Search actions…")
        self._act_search.setClearButtonEnabled(True)
        self._act_search.textChanged.connect(lambda _t: self._rebuild_actions_list())
        outer.addWidget(self._act_search)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._act_host = QWidget()
        self._act_box = QVBoxLayout(self._act_host)
        self._act_box.setContentsMargins(0, 0, 6, 0)
        self._act_box.setSpacing(3)
        scroll.setWidget(self._act_host)
        outer.addWidget(scroll, 1)
        self._act_blob = {t: f"{ACTION_LABELS.get(t, t)} {ACTION_DESC.get(t, '')} {t}".lower()
                          for t in ACTION_EMOJI}
        # which categories are open is remembered across restarts (default: all collapsed — the
        # Favourites / Recent / Common groups up top cover the essentials).
        self._act_expanded = set(self.cfg.data.get("actions_open_cats", []))
        self._rebuild_actions_list()
        return bar

    def _add_actions(self, box, types):
        """Append a set of actions to the sidebar as draggable rows (list) or tiles (grid)."""
        if self._act_grid:
            gw = QWidget()
            g = QGridLayout(gw)
            g.setContentsMargins(0, 2, 0, 4)
            g.setSpacing(6)
            for c in range(3):
                g.setColumnStretch(c, 1)
            for i, t in enumerate(types):
                chip = _ActionChip(t)
                chip.picked.connect(self._assign_action_to_current)
                chip.menu.connect(self._action_ctx_menu)
                g.addWidget(chip, i // 3, i % 3)
            box.addWidget(gw)
        else:
            for t in types:
                r = _ActionRow(t)
                r.picked.connect(self._assign_action_to_current)
                r.menu.connect(self._action_ctx_menu)
                box.addWidget(r)

    def _rebuild_actions_list(self):
        box = self._act_box
        while box.count():
            it = box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        q = self._act_search.text().strip().lower()
        if q:                                  # search -> one flat filtered set (hide the "none" tile)
            order = [t for t in dict.fromkeys(
                COMMON_ACTIONS + [t for _c, ts in ACTION_CATEGORIES for t in ts]) if t != "none"]
            hits = [t for t in order if q in self._act_blob.get(t, t)]
            if hits:
                self._add_actions(box, hits)
            else:
                box.addWidget(QLabel("No matching actions.", objectName="dim"))
        else:                                  # browse -> Favourites / Recent / Common + categories
            first = [True]
            def _sep():                        # section rhythm: headers breathe + a faint divider
                if first[0]:
                    first[0] = False
                    return
                box.addSpacing(12)
                div = QFrame(objectName="hsep")
                div.setFixedHeight(1)
                box.addWidget(div)
                box.addSpacing(8)
            favs = [t for t in self.cfg.data.get("fav_actions", []) if t in ACTION_EMOJI]
            if favs:
                _sep()
                box.addWidget(QLabel("★  FAVOURITES", objectName="cardtitle"))
                self._add_actions(box, favs)
            recent = [t for t in self.cfg.data.get("recent_actions", [])
                      if t in ACTION_EMOJI and t not in favs][:6]
            if recent:
                _sep()
                box.addWidget(QLabel("RECENT", objectName="cardtitle"))
                self._add_actions(box, recent)
            _sep()
            box.addWidget(QLabel("COMMON", objectName="cardtitle"))
            self._add_actions(box, [t for t in COMMON_ACTIONS if t != "none"])
            for cat, types in ACTION_CATEGORIES:
                _sep()
                hdr = _CatHeader(cat)
                hdr.set_expanded(cat in self._act_expanded)
                hdr.clicked.connect(lambda _=False, c=cat: self._toggle_act_cat(c))
                box.addWidget(hdr)
                if cat in self._act_expanded:
                    self._add_actions(box, types)
        box.addStretch(1)

    def _toggle_grid(self):
        self._act_grid = not self._act_grid
        self.cfg.data["actions_grid"] = self._act_grid
        self.cfg.save()
        if getattr(self, "_grid_btn", None) is not None:
            self._grid_btn.setChecked(self._act_grid)
            self._sync_grid_btn_face()
        self._rebuild_actions_list()

    def _sync_grid_btn_face(self):
        """The view toggle mirrors the current view — a crisp fluent glyph, text fallback."""
        btn = self._grid_btn
        ic = fluent_qicon("grid" if self._act_grid else "list",
                          normal=T.TEXT_DIM, active=T.ACCENT, size=15, widget=btn)
        if ic is not None:
            btn.setIcon(ic)
            btn.setIconSize(QSize(15, 15))
            btn.setText("")
        else:
            btn.setText("▦" if self._act_grid else "≣")

    def _toggle_act_cat(self, cat):
        if cat in self._act_expanded:
            self._act_expanded.discard(cat)
        else:
            self._act_expanded.add(cat)
        self.cfg.data["actions_open_cats"] = sorted(self._act_expanded)   # remember across restarts
        self.cfg.save()
        self._rebuild_actions_list()

    def _action_ctx_menu(self, atype):
        """Right-click an action in the sidebar -> pin / unpin it as a favourite."""
        from PySide6.QtGui import QCursor
        favs = self.cfg.data.get("fav_actions", [])
        m = QMenu(self)
        if atype in favs:
            m.addAction(menu_icon("unpin", self) or QIcon(), "Unpin from favourites",
                        lambda: self._toggle_fav(atype))
        else:
            m.addAction(menu_icon("pin", self) or QIcon(), "Pin to favourites",
                        lambda: self._toggle_fav(atype))
        m.exec(QCursor.pos())

    def _toggle_fav(self, atype):
        if not atype or atype == "none":
            return                            # the clear/"Nothing" action is never a favourite
        favs = self.cfg.data.setdefault("fav_actions", [])
        if atype in favs:
            favs.remove(atype)
        else:
            favs.insert(0, atype)
            del favs[12:]
        self.cfg.save()
        self._rebuild_actions_list()

    def _note_recent(self, atype):
        """Push an action type to the front of the recently-used list (in memory; caller saves)."""
        if not atype or atype == "none":
            return
        rec = self.cfg.data.setdefault("recent_actions", [])
        if atype in rec:
            rec.remove(atype)
        rec.insert(0, atype)
        del rec[8:]

    def _assign_action_to_current(self, atype):
        sid = self.sel
        if self._is_back_key(sid):
            return                            # the fixed folder Back key has no action
        self._assign_dropped_action(sid, atype)

    def _assign_dropped_action(self, sid, atype):
        """Bind a dropped/clicked action to a slot, then open its editor to tune it. Keys/buttons
        get it on the Tap slot; a knob gets it on its push action."""
        self.select(sid)                      # makes this the active control + resets the slot to Tap
        item = self._store_item(sid)
        self._set_act(item, {"type": atype})
        item.pop("live", None)                # the new action replaces any live-data face on the key
        self._note_recent(atype)
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self.select(sid)                      # rebuild the editor with the new action + its fields
        self._rebuild_actions_list()          # surface the just-used action under "Recent"

    def _assign_dropped_external(self, sid, mime):
        """Drop a program / file / .lnk shortcut or a URL from Windows onto a control -> an Open
        action with that target, auto-grabbing the app's real icon for LCD keys."""
        target = ""
        if mime.hasUrls():
            for u in mime.urls():
                target = u.toLocalFile() if u.isLocalFile() else u.toString()
                if target:
                    break
        if not target and mime.hasText():
            for line in mime.text().splitlines():
                if line.strip():
                    target = line.strip()
                    break
        if not target:
            return
        is_url = target.lower().startswith(("http://", "https://"))
        if is_url:
            host = target.split("://", 1)[-1]
            if host.lower().startswith("www."):
                host = host[4:]
            label = host.split("/")[0][:14]
        else:
            label = os.path.splitext(os.path.basename(target.rstrip("\\/")))[0][:14]
        self.select(sid)
        item = self._store_item(sid)
        self._set_act(item, {"type": "open", "target": target})
        item["label"] = label                 # the key becomes this app/site (label + icon below)
        item.pop("live", None)                # the new binding replaces any live-data face
        self._note_recent("open")
        if sid.startswith("key") and not is_url:
            self._apply_app_icon(item, silent=True)   # additionally grab the app's real icon
        # Always persist + render — even if no icon could be extracted, the binding must NOT be lost.
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self.select(sid)
        self._rebuild_actions_list()

    def _effective_binding(self, sid):
        """The binding actually in effect for a slot — for encoders, the per-page override OR
        the profile-global fallback (so the at-a-glance summary matches what the dock does)."""
        if sid.startswith("enc"):
            return self.page().get("items", {}).get(sid) or self._globals().get(sid)
        return self._store(sid).get(sid)

    def _enc_name_row(self, base):
        """Rename a knob — the name shown on the dial in this editor (per page)."""
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(QLabel("Dial name", objectName="section"))
        e = QLineEdit(self._custom_caption(base) or "")
        e.setPlaceholderText(self._auto_caption(base) or "e.g. Volume, Light")
        e.setToolTip("Shown on the knob here in the editor. Leave blank to auto-name from the action.")
        e.textChanged.connect(lambda t, bs=base: self._set_dial_caption(bs, t))
        lay.addWidget(e, 1)
        return w

    def _enc_segment_row(self, sid):
        """One dial = three actions. Show all three at once (with their current binding) so it
        reads as ONE knob with three roles, not three separate controls. Click one to edit it."""
        n = sid[3]
        base = f"enc{n}"
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        hint = QLabel("This dial has three actions — click one to edit:", objectName="dim")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        for sub, name in (("-", "↺ Turn left"), ("", "⊙ Push"), ("+", "↻ Turn right")):
            tid = f"{base}{sub}"
            summ = _action_summary(self._effective_binding(tid) or {})
            seg = QToolButton(objectName="encseg")
            seg.setText(f"{name}\n{summ}")
            seg.setCheckable(True)
            seg.setChecked(tid == sid)
            seg.setCursor(Qt.PointingHandCursor)
            # shrink below the text width so 3 tiles never force the fixed-width inspector wider
            seg.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            seg.setMinimumWidth(64)
            seg.setToolTip(f"{name.strip()} — {summ}")
            seg.clicked.connect(lambda _=False, s=tid: self._select_sub(s))
            row.addWidget(seg, 1)
        outer.addLayout(row)
        return w

    def _enc_quick_row(self, base):
        """One-click recipes that map a whole dial (turn-left / push / turn-right) at once —
        so 'use the knob for brightness / colour' is a single click, not three bindings."""
        w = QWidget()
        lay = QVBoxLayout(w); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(6)
        lab = QLabel("Quick set-up — map this whole dial in one click:", objectName="dim")
        lab.setWordWrap(True); lay.addWidget(lab)
        grid = QGridLayout(); grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6); grid.setVerticalSpacing(6)
        for i, (text, kind, tip) in enumerate((
                ("Bulb dim", "bright", "Turn = dim / brighten the bulb · push = on/off"),
                ("Bulb colour", "hue", "Turn = scroll the bulb's colour wheel · push = on/off"),
                ("RGB dim", "rgb", "Turn = dim / brighten Prisma RGB · push = on/off"),
                ("OBS audio", "obsaudio", "Turn = an OBS source's volume · push = mute (shows a HUD)"))):
            b = QPushButton(text); b.setToolTip(tip)
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed); b.setMinimumWidth(50)
            b.clicked.connect(lambda _=False, k=kind, bs=base: self._setup_dial(bs, k))
            grid.addWidget(b, i // 2, i % 2)
        lay.addLayout(grid)
        return w

    def _setup_dial(self, base, kind):
        """Fill a knob's three roles (turn-left / push / turn-right) for one-click device control."""
        host = "192.168.0.87"
        if kind == "bright":
            plan = {f"{base}-": {"type": "smartlight", "mode": "brightness_down", "host": host, "step": 10},
                    base:        {"type": "smartlight", "mode": "toggle", "host": host},
                    f"{base}+":  {"type": "smartlight", "mode": "brightness_up", "host": host, "step": 10}}
        elif kind == "hue":
            plan = {f"{base}-": {"type": "smartlight", "mode": "hue_down", "host": host, "step": 30},
                    base:        {"type": "smartlight", "mode": "toggle", "host": host},
                    f"{base}+":  {"type": "smartlight", "mode": "hue_up", "host": host, "step": 30}}
        elif kind == "obsaudio":                         # OBS audio source on the dial (+ mute on push)
            src = self._ask_obs_source()
            if not src:
                return
            plan = {f"{base}-": {"type": "obs", "mode": "vol_down", "input": src, "step": 5},
                    base:        {"type": "obs", "mode": "vol_mute", "input": src},
                    f"{base}+":  {"type": "obs", "mode": "vol_up", "input": src, "step": 5}}
        else:                                            # Prisma RGB brightness
            plan = {f"{base}-": {"type": "rgbscene", "mode": "bright_down", "step": 10},
                    base:        {"type": "rgbscene", "mode": "toggle"},
                    f"{base}+":  {"type": "rgbscene", "mode": "bright_up", "step": 10}}
        page_items = self.page().setdefault("items", {})
        for tid, act in plan.items():
            page_items[tid] = {"action": act}
        self._after_binding_change(self.sel)

    def _ask_obs_source(self):
        """Prompt for an OBS audio source — a dropdown of OBS's inputs if reachable, else free text."""
        o = self.cfg.data.get("obs", {})
        names = []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            from . import obs as _obs
            _obs.configure(o.get("host"), o.get("port"), o.get("password"))
            names = _obs.input_list()
        except Exception:
            names = []
        finally:
            QApplication.restoreOverrideCursor()
        if names:
            name, ok = QInputDialog.getItem(self, "OBS audio source",
                                            "OBS source for this knob:", names, 0, False)
        else:
            name, ok = QInputDialog.getText(self, "OBS audio source",
                                            "Exact OBS source name (e.g. Mic/Aux) — OBS unreachable:")
        return name.strip() if (ok and name and name.strip()) else None

    def _pick_obs_input(self, a, edit):
        src = self._ask_obs_source()
        if src:
            edit.setText(src)            # fires _set_action -> a["input"]

    # ---- data helpers ------------------------------------------------------
    def pages(self):
        return self.cfg.pages()

    def page(self):
        ps = self.pages()
        self.cur_page = min(self.cur_page, len(ps) - 1)
        return ps[self.cur_page]

    def items(self):
        if self.view_folder is not None:
            return self.cfg.folder_items(self.view_folder, self.view_folder_page)
        return self.page().setdefault("items", {})

    def _globals(self):
        return self.cfg.globals_of()

    def _store(self, sid):
        # LCD keys: per-page (or folder). Knobs: per-page. Round buttons: profile-global.
        if sid.startswith("key"):
            return self.items()
        if sid.startswith("enc"):
            return self.page().setdefault("items", {})
        return self._globals()

    # ---- copy / paste / clear / duplicate / drag-swap ----------------------
    def _is_back_key(self, sid):
        # The 6th key is the fixed Back tile ONLY when the folder-exit control is "key6"; when a round
        # button exits folders, key6 becomes an ordinary, fully-editable content key.
        return (self.view_folder is not None and sid == LCD_KEYS[-1]
                and self.cfg.data.get("folder_back", "key6") == "key6")

    def _after_binding_change(self, focus_sid=None):
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self.select(focus_sid or self.sel)

    def _slot_context_menu(self, sid, gpos, is_key):
        import copy
        back = self._is_back_key(sid)
        menu = QMenu(self)
        a_copy = menu.addAction(menu_icon("copy", self) or QIcon(), "Copy")
        a_paste = menu.addAction(menu_icon("paste", self) or QIcon(), "Paste")
        a_paste.setEnabled(self.clipboard_binding is not None and not back)
        dup = None
        wx_act = None
        preset_acts = []
        if is_key and not back:
            menu.addSeparator()
            dup = menu.addAction(menu_icon("add", self) or QIcon(), "Duplicate → next empty key")
            sub = menu.addMenu(menu_icon("star", self) or QIcon(), "Apply preset")
            for p in PRESETS:                          # each starter keeps its own colourful face
                act = sub.addAction(_preset_qicon(p["binding"], self) or QIcon(), p["name"])
                preset_acts.append((act, p))
            if self.view_folder is None:               # a Weather center opens its own folder -> never nest one
                wx_act = menu.addAction(menu_icon("cloud", self) or QIcon(),
                                        "Build Weather center (folder)")
        menu.addSeparator()
        a_clear = menu.addAction(menu_icon("delete", self) or QIcon(), "Clear")
        a_clear.setEnabled(not back)
        chosen = menu.exec(gpos)
        if chosen is None:
            return
        if chosen is a_copy:
            b = self._store(sid).get(sid)
            self.clipboard_binding = copy.deepcopy(b) if b else None
        elif chosen is a_paste and self.clipboard_binding is not None:
            self._store(sid)[sid] = copy.deepcopy(self.clipboard_binding)
            self._after_binding_change(sid)
        elif dup is not None and chosen is dup:
            self._duplicate_to_empty(sid)
        elif wx_act is not None and chosen is wx_act:
            self._build_weather_center(sid)
        elif chosen is a_clear:
            self._store(sid)[sid] = {"action": {"type": "none"}}
            self._after_binding_change(sid)
        else:
            for act, preset in preset_acts:
                if chosen is act:
                    self._store(sid)[sid] = copy.deepcopy(preset["binding"])
                    self._after_binding_change(sid)
                    break

    _ENC_SUBS = ("-", "", "+")               # turn-left · push · turn-right

    def _knob_context_menu(self, base, gpos):
        """Copy / paste / clear a whole knob — all three roles (turn-left, push, turn-right) at once,
        so 'copy every setting and paste every setting' works on the dials like it does on keys."""
        menu = QMenu(self)
        a_copy = menu.addAction(menu_icon("copy", self) or QIcon(), "Copy dial  (all 3 actions)")
        a_paste = menu.addAction(menu_icon("paste", self) or QIcon(), "Paste dial")
        a_paste.setEnabled(self.clipboard_encoder is not None)
        menu.addSeparator()
        a_clear = menu.addAction(menu_icon("delete", self) or QIcon(), "Clear dial")
        chosen = menu.exec(gpos)
        if chosen is a_copy:
            self._copy_knob(base)
        elif chosen is a_paste:
            self._paste_knob(base)
        elif chosen is a_clear:
            self._clear_knob(base)

    def _copy_knob(self, base):
        """Snapshot all 3 of a dial's roles — what it CURRENTLY does (per-page override or global)."""
        import copy
        self.clipboard_encoder = {s: copy.deepcopy(self._effective_binding(f"{base}{s}")
                                                   or {"action": {"type": "none"}})
                                  for s in self._ENC_SUBS}

    def _paste_knob(self, base):
        import copy
        if self.clipboard_encoder is None:
            return
        page_items = self.page().setdefault("items", {})
        for s in self._ENC_SUBS:
            page_items[f"{base}{s}"] = copy.deepcopy(
                self.clipboard_encoder.get(s) or {"action": {"type": "none"}})
        self._after_binding_change(f"{base}-")            # select the dial so the result is visible

    def _clear_knob(self, base):
        page_items = self.page().setdefault("items", {})
        for s in self._ENC_SUBS:
            page_items[f"{base}{s}"] = {"action": {"type": "none"}}
        self._after_binding_change(f"{base}-")

    def _duplicate_to_empty(self, sid):
        import copy
        items = self.items()
        src = items.get(sid)
        if not src:
            return
        for kid in LCD_KEYS:
            if self._is_back_key(kid):
                continue                      # never overwrite the folder Back key
            b = items.get(kid)
            if not b or (b.get("action") or {}).get("type", "none") == "none":
                items[kid] = copy.deepcopy(src)
                self._after_binding_change(kid)
                return
        QMessageBox.information(self, "Duplicate", "No empty key on this page to copy into.")

    def _swap_or_move_binding(self, src, dst):
        if src == dst or not src.startswith("key") or not dst.startswith("key"):
            return
        if self._is_back_key(src) or self._is_back_key(dst):
            return
        items = self.items()
        a = items.pop(src, None)
        b = items.pop(dst, None)
        if b is not None:
            items[src] = b
        if a is not None:
            items[dst] = a
        self._after_binding_change(dst)

    def _store_item(self, sid):
        """The editable binding dict for sid. The first per-page knob edit seeds itself from
        the shared global default, so the editor opens on the knob's current behaviour."""
        if sid.startswith("enc"):
            page_items = self.page().setdefault("items", {})
            if sid not in page_items:
                import copy
                g = self._globals().get(sid)
                page_items[sid] = copy.deepcopy(g) if isinstance(g, dict) else {}
            return page_items[sid]
        return self._store(sid).setdefault(sid, {})

    def _face(self, item):
        g = self.cfg.data.get("show_labels", True)
        item = item or {}
        show = item.get("show_label", g)
        if item.get("live"):
            src = (item.get("live") or {}).get("source", "")
            text, caption, frac, kind = livesrc.value(src)
            art = livesrc.media_artwork() if kind == "media" else None
            face = live_face(item, text, caption, frac, kind, show_label=show,
                             style=self.cfg.data.get("live_style", "gauge"),
                             history=livesrc.history(src), artwork=art)
            return self._overlay_gesture_badge(face, item)
        action = item.get("action") or {}
        if action.get("type") == "toggle":            # preview the currently-edited state's look
            states = [s for s in (action.get("states") or []) if isinstance(s, dict)]
            if states:
                st = states[max(0, min(getattr(self, "_toggle_edit", 0), len(states) - 1))]
                face = {k: st.get(k) for k in ("label", "icon", "color", "text_color")
                        if st.get(k) not in (None, "")}
                return self._overlay_gesture_badge(
                    render_face(face, show_label=st.get("show_label", show)), item)
        if action.get("type") == "timer":             # editor preview: the configured duration face
            dur = 0 if (action.get("mode") or "countdown") == "stopwatch" else \
                max(1, int(action.get("minutes", 0) or 0) * 60 + int(action.get("seconds", 0) or 0))
            return self._overlay_gesture_badge(
                images.timer_face(dur, dur, False, False, False, label=item.get("label", "")), item)
        if action.get("type") == "folder":
            fid = action.get("folder")
            f = self.cfg.folders_of().get(fid) if fid else None
            contents = self.cfg._norm_folder(f)["pages"][0].get("items", {}) if f else {}
            return self._overlay_gesture_badge(folder_face(item, contents, show_label=show), item)
        if self._is_empty_key(item):                  # editor-only "add here" cue on blank keys
            return self._empty_key_face(item)
        return self._overlay_gesture_badge(render_face(item, show_label=show), item)

    def _overlay_gesture_badge(self, img, item):
        """Editor-only: a tiny accent pip per extra gesture (double / hold) in the top-right corner,
        so you can see at a glance which keys have hidden actions. The dock face is unchanged."""
        extra = [k for k in ("action_double", "action_hold") if self._nonempty_action((item or {}).get(k))]
        if not extra:
            return img
        from PIL import ImageDraw
        im = img.convert("RGB").copy()
        d = ImageDraw.Draw(im)
        w, h = im.size
        acc = images.parse_color(T.ACCENT, "#35e08a")
        r = max(2, int(w * 0.05))
        m = max(2, int(w * 0.06))
        cy = m + r
        for i in range(len(extra)):
            cx = w - m - r - i * (2 * r + max(2, r))
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=acc, outline=(10, 12, 16))
        return im

    @staticmethod
    def _is_empty_key(item):
        item = item or {}
        has_action = any((item.get(k) or {}).get("type", "none") not in (None, "", "none")
                         for k in ("action", "action_double", "action_hold"))   # any gesture counts
        return (not has_action and not item.get("icon")
                and not item.get("live") and not (item.get("label") or "").strip())

    def _empty_key_face(self, item):
        """A faint corner-bracket + '+' placeholder so blank keys read as 'click to assign'
        (shown ONLY in the configurator — the dock itself stays blank for an empty key)."""
        from PIL import Image, ImageDraw
        w, h = images.KEY_SIZE
        ss = 3
        W, H = w * ss, h * ss
        bg = images.parse_color((item or {}).get("color"), "#0f1318")
        # Honour a custom colour / gradient so a deliberately-styled blank tile previews truthfully;
        # the cue is overlaid on top rather than replacing the background.
        img = images.background_image(item or {}, (W, H)).convert("RGB")
        d = ImageDraw.Draw(img)
        acc = images.parse_color(T.ACCENT, "#35e08a")
        faint = tuple(int(a * 0.45 + c * 0.55) for a, c in zip(acc, bg))
        lw = max(2, int(W * 0.018))
        o, bl = int(W * 0.15), int(W * 0.15)
        for x, y, dx, dy in ((o, o, 1, 1), (W - o, o, -1, 1), (o, H - o, 1, -1), (W - o, H - o, -1, -1)):
            d.line([(x, y), (x + dx * bl, y)], fill=faint, width=lw)
            d.line([(x, y), (x, y + dy * bl)], fill=faint, width=lw)
        cx, cy, s = W // 2, H // 2, int(W * 0.14)
        pw = max(3, int(W * 0.03))
        d.line([(cx - s, cy), (cx + s, cy)], fill=faint, width=pw)
        d.line([(cx, cy - s), (cx, cy + s)], fill=faint, width=pw)
        return img.resize((w, h), Image.LANCZOS)

    # ---- folders -----------------------------------------------------------
    def _new_folder_id(self):
        existing = self.cfg.folders_of()
        i = 1
        while f"folder{i}" in existing:
            i += 1
        return f"folder{i}"

    # Pages of the auto-built Weather center folder (5 live keys each; key6 is the auto Back key).
    _WEATHER_PAGES = [
        ["weather", "wx_feels", "wx_humidity", "wx_wind", "wx_precip"],   # now
        ["wx_uv", "wx_hi", "wx_lo", "wx_sunrise", "wx_sunset"],           # today
        ["wx_d1", "wx_d2", "wx_d3"],                                      # forecast 1-3
        ["wx_d4", "wx_d5", "wx_d6"],                                      # forecast 4-6
    ]

    def _build_weather_center(self, sid):
        """Turn a key into a 'Weather center': a live current-weather face that opens a folder packed
        with forecast / UV / precipitation / wind / humidity / sunrise-sunset live tiles."""
        prev = (self._store(sid).get(sid) or {}).get("action") or {}
        if prev.get("type") == "folder" and prev.get("folder"):
            fid = prev["folder"]                      # rebuild in place -> no orphaned folders pile up
        else:
            fid = self._new_folder_id()
        folder = self.cfg.folder(fid)                 # creates + normalises to one page
        folder["name"] = "Weather"
        pages = folder["pages"]
        while len(pages) < len(self._WEATHER_PAGES):
            pages.append({"items": {}})
        del pages[len(self._WEATHER_PAGES):]          # trim extras if reusing a larger folder
        for pi, sources in enumerate(self._WEATHER_PAGES):
            items = pages[pi].setdefault("items", {})
            items.clear()
            for ki, src in enumerate(sources):        # key1..key5 (key6 stays the auto Back key)
                items[LCD_KEYS[ki]] = {"live": {"source": src}, "show_label": False}
        # the key itself: shows the live cloud + temperature, and opens the folder when pressed
        self._store(sid)[sid] = {"live": {"source": "weather"},
                                 "action": {"type": "folder", "folder": fid},
                                 "label": "Weather", "show_label": False}
        self._after_binding_change(sid)
        self.toast("Weather center created — press it on the dock to open", "ok")

    def _set_folder_name(self, fid, name):
        self.cfg.folder(fid)["name"] = name or "Folder"
        self.cfg.save()
        if self.view_folder == fid:
            self._refresh_tabs()

    def _enter_folder_edit(self, fid):
        self.cfg.folder(fid)                  # ensure it exists (+ normalise to pages)
        self.view_folder = fid
        self.view_folder_page = 0
        self.sel = "key1"
        self._refresh_tabs()
        self._refresh_all_slots()
        self._flash_canvas()
        self.select("key1")
        if not getattr(self, "_back_hint_shown", False):   # surface the (existing) editable Back key once
            self._back_hint_shown = True
            self.toast("Tip: click the Back key to customise how it looks", "info")

    def _exit_folder_edit(self):
        self.view_folder = None
        self.view_folder_page = 0
        self.sel = "key1"
        self._refresh_tabs()
        self._refresh_all_slots()
        self._flash_canvas()
        self.select("key1")

    # ---- folder pages (multi-page folders) ---------------------------------
    def _goto_folder_page(self, idx):
        self.view_folder_page = idx
        self.sel = "key1"
        self._refresh_tabs()
        self._refresh_all_slots()
        self._flash_canvas()
        self.select("key1")

    def _add_folder_page(self):
        f = self.cfg.folder(self.view_folder)
        f["pages"].append({"items": {}})
        self.cfg.save()
        self.view_folder_page = len(f["pages"]) - 1
        self.sel = "key1"
        self._refresh_tabs()
        self._refresh_all_slots()
        self.select("key1")
        self.controller.request_render()

    def _folder_page_menu(self, idx, gpos):
        f = self.cfg.folder(self.view_folder)
        if len(f.get("pages", [])) <= 1:
            return                            # a folder always keeps at least one page
        m = QMenu(self)
        m.addAction(menu_icon("delete", self) or QIcon(), "Delete this folder page",
                    lambda: self._delete_folder_page(idx))
        m.exec(gpos)

    def _delete_folder_page(self, idx):
        f = self.cfg.folder(self.view_folder)
        pages = f.get("pages", [])
        if len(pages) <= 1 or not (0 <= idx < len(pages)):
            return
        del pages[idx]
        self.cfg.save()
        self.view_folder_page = max(0, min(self.view_folder_page, len(pages) - 1))
        self.sel = "key1"
        self._refresh_tabs()
        self._refresh_all_slots()
        self.select("key1")
        self.controller.request_render()

    # ---- refresh -----------------------------------------------------------
    def refresh(self):
        self.cfg = self.controller.config
        self._install_save_hook()              # a rebound cfg (restore/import/reload) needs re-wrapping
        self.view_folder = None
        self.view_folder_page = 0
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(self.cfg.profile_names())
        self.profile_combo.setCurrentText(self.cfg.data.get("active_profile", ""))
        self.profile_combo.blockSignals(False)
        self.bright.blockSignals(True)
        self.bright.setValue(self.cfg.brightness)
        self.bright.blockSignals(False)
        self.bright_val.setText(f"{self.cfg.brightness}%")
        self.titles_chk.blockSignals(True)
        self.titles_chk.setChecked(self.cfg.data.get("show_labels", True))
        self.titles_chk.blockSignals(False)
        self.pressfx_chk.blockSignals(True)
        self.pressfx_chk.setChecked(self.cfg.data.get("press_fx", True))
        self.pressfx_chk.blockSignals(False)
        self.anim_combo.blockSignals(True)
        ai = self.anim_combo.findData(self.cfg.data.get("press_anim", "bounce"))
        self.anim_combo.setCurrentIndex(ai if ai >= 0 else 0)
        self.anim_combo.setEnabled(self.pressfx_chk.isChecked())
        self.anim_combo.blockSignals(False)
        self.folder_combo.blockSignals(True)
        fi = self.folder_combo.findData(self.cfg.data.get("folder_anim", "zoom"))
        self.folder_combo.setCurrentIndex(fi if fi >= 0 else 0)
        self.folder_combo.blockSignals(False)
        self.live_combo.blockSignals(True)
        li = self.live_combo.findData(self.cfg.data.get("live_style", "gauge"))
        self.live_combo.setCurrentIndex(li if li >= 0 else 0)
        self.live_combo.blockSignals(False)
        # Re-sync the rest of the Settings dialog too — it is built once and reused, so a backup
        # restore / config import (which swaps cfg then calls refresh) must NOT leave the app-rules
        # list, the toggles or the accent showing the OLD config (editing a stale rule would mutate
        # the wrong entry — silent config corruption).
        if getattr(self, "encaccel_chk", None) is not None:
            self.encaccel_chk.blockSignals(True)
            self.encaccel_chk.setChecked(self.cfg.data.get("encoder_accel", True))
            self.encaccel_chk.blockSignals(False)
        if getattr(self, "auto_chk", None) is not None:
            self.auto_chk.blockSignals(True)
            self.auto_chk.setChecked(bool(self.cfg.data.get("auto_switch", False)))
            self.auto_chk.blockSignals(False)
        if getattr(self, "rules_list", None) is not None:
            self._refresh_rules_list()
        # Re-apply the imported accent to the whole UI + the swatch highlight.
        if getattr(self, "_accent_swatches", None):
            T.set_accent(self.cfg.data.get("accent", "mint"))
            app = QApplication.instance()
            if app:
                app.setStyleSheet(T.build_qss())
            self._refresh_accent_swatches()
        self._refresh_tabs()
        self._refresh_all_slots()
        self._refresh_conn()

    def _install_save_hook(self):
        """Wrap cfg.save so every save re-arms the history timer. Idempotent, and re-applied whenever
        self.cfg is rebound — a Backups restore / import / tray reload swaps in a FRESH Config whose
        save is unwrapped, which would otherwise silently stop undo/redo from recording."""
        cfg = self.cfg
        if getattr(cfg.save, "_hexpad_wrapped", False):
            return
        _real_save = cfg.save

        def _wrapped_save(*a, **k):
            _real_save(*a, **k)
            if not self._restoring:
                self._save_bridge.saved.emit()
        _wrapped_save._hexpad_wrapped = True
        cfg.save = _wrapped_save

    # ---- undo / redo -------------------------------------------------------
    def _on_config_saved(self):
        """A save happened (any editor mutation) — (re)arm the coalescing history timer."""
        if not self._restoring:
            self._hist_timer.start()

    def _commit_history(self):
        if self._restoring:
            return
        cur = self.cfg.data
        if cur == self._hist_base:
            return                              # the burst of saves didn't actually change anything
        self._undo.append(self._hist_base)
        if len(self._undo) > 80:
            self._undo.pop(0)
        self._redo.clear()
        self._hist_base = self._copy.deepcopy(cur)
        self._sync_history_buttons()

    def _undo_edit(self):
        if not self._undo:
            self.toast("Nothing to undo", "info")
            return
        self._hist_timer.stop()
        self._redo.append(self._copy.deepcopy(self.cfg.data))
        self._apply_snapshot(self._undo.pop())
        self.toast("Undid last change", "ok")

    def _redo_edit(self):
        if not self._redo:
            self.toast("Nothing to redo", "info")
            return
        self._hist_timer.stop()
        self._undo.append(self._copy.deepcopy(self.cfg.data))
        self._apply_snapshot(self._redo.pop())
        self.toast("Redid change", "ok")

    def _apply_snapshot(self, snap):
        """Swap the whole config to a snapshot and rebuild every view (no new history entry)."""
        self._restoring = True
        try:
            self.cfg.data = snap
            self.cfg.save()                     # persist the restored state
            self._hist_base = self._copy.deepcopy(snap)
            self._reload_all()
        finally:
            self._restoring = False
        self._sync_history_buttons()

    def _reload_all(self):
        """Re-sync every view after the config was swapped wholesale (undo / redo)."""
        self.view_folder = None
        self.view_folder_page = 0
        self._toggle_edit = 0
        self._gesture_slot = "tap"
        pages = self.pages()
        self.cur_page = max(0, min(self.cur_page, len(pages) - 1)) if pages else 0
        self.refresh()
        try:
            self.select(self.sel)
        except Exception:
            self.sel = "key1"
            self.select("key1")
        self.controller.request_render()

    def _sync_history_buttons(self):
        if getattr(self, "_undo_btn", None) is None:
            return
        for btn, on in ((self._undo_btn, bool(self._undo)), (self._redo_btn, bool(self._redo))):
            btn.setEnabled(on)                              # clickability is immediate
            self._fade_widget(btn, 1.0 if on else 0.45)    # the affordance softly lights up / dims

    def _fade_widget(self, widget, to, dur=120):
        """Animate a widget's opacity (130 ms-ish OutCubic) — the shared Qt 'fade' micro-motion."""
        eff = widget.graphicsEffect()
        if not isinstance(eff, QGraphicsOpacityEffect):
            eff = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(eff)
        prev = getattr(widget, "_fade_anim", None)
        if prev is not None:
            prev.stop()
        an = QPropertyAnimation(eff, b"opacity", self)
        an.setDuration(dur)
        an.setStartValue(eff.opacity())
        an.setEndValue(to)
        an.setEasingCurve(QEasingCurve.OutCubic)
        widget._fade_anim = an                             # keep a ref so it isn't GC'd mid-flight
        an.start()

    def _fade_in_transient(self, widget, start=0.4, dur=140):
        """Fade a widget in, then REMOVE the opacity effect — graphics effects tax every later
        repaint, so heavyweight panels only carry one for the animation's 140 ms."""
        if widget is None:
            return
        prev = getattr(widget, "_fx_fade", None)
        if prev is not None:
            prev.stop()
        eff = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(eff)
        an = QPropertyAnimation(eff, b"opacity", self)
        an.setDuration(dur)
        an.setStartValue(start)
        an.setEndValue(1.0)
        an.setEasingCurve(QEasingCurve.OutCubic)

        def _done(w=widget):
            try:
                w.setGraphicsEffect(None)
            except RuntimeError:
                pass                                       # widget died mid-animation
        an.finished.connect(_done)
        widget._fx_fade = an                               # keep a ref so it isn't GC'd mid-flight
        an.start()

    def _flash_canvas(self):
        """A 150 ms bloom on the device stage when the visible page changes — the GUI's echo of
        the device's own swipe animation."""
        if getattr(self, "_ready", False):
            self._fade_in_transient(getattr(self, "_dev_panel", None), start=0.55, dur=150)

    # ---- first-run guided tour --------------------------------------------
    def _start_tour(self):
        if getattr(self, "_tour", None) is not None:
            try:
                self._tour.deleteLater()
            except RuntimeError:
                pass
        steps = [
            {"target": None, "title": "Welcome to Hexpad  👋",
             "body": "Your stream-dock, set up your way. Here's a 20-second tour — or skip and dive in."},
            {"target": lambda: getattr(self, "_dev_panel", None), "title": "Your dock",
             "body": "This mirrors your device. Click any key, knob or button to edit what it does and how it looks."},
            {"target": lambda: self.findChild(QFrame, "actionsidebar"), "title": "Add an action",
             "body": "Drag an action from here onto a key — or click one to bind it to the selected key. "
                     "You can also drop apps or files straight from Windows."},
            {"target": lambda: self.findChild(QFrame, "inspector"), "title": "Make it yours",
             "body": "Down here: the action, label, icon, gradient and Tap / Double-tap / Hold. "
                     "Everything saves automatically — and Ctrl+Z undoes."},
            {"target": lambda: getattr(self, "_gear_btn", None), "title": "Settings & more",
             "body": "Brightness, themes, app auto-switch, folders and backups all live behind the gear. Enjoy!"},
        ]
        self._tour = _TourOverlay(self, steps, self._finish_tour)

    def _finish_tour(self):
        self._tour = None
        if not self.cfg.data.get("onboarded"):
            self.cfg.data["onboarded"] = True
            self._restoring = True                 # marking onboarded isn't an undoable edit
            try:
                self.cfg.save()
            finally:
                self._restoring = False

    def toast(self, msg, kind="info"):
        """Show a brief, non-blocking notification near the bottom of the window."""
        try:
            if self._active_toast is not None:
                self._active_toast.deleteLater()
        except RuntimeError:
            pass
        try:
            self._active_toast = Toast(self, msg, kind)
        except Exception:
            self._active_toast = None

    def _refresh_conn(self):
        st = self.controller.status()
        connected = st["connected"]
        dot = "●" if connected else "○"
        self.conn_lbl.setText(f"{dot} {'Connected' if connected else 'No device'}")
        # 'No device' is a calm idle state, not an alarm — reserve DANGER red for destructive actions.
        self.conn_lbl.setStyleSheet(f"color: {T.ACCENT if connected else T.TEXT_DIM};")
        if getattr(self, "_nodev_banner", None) is not None:
            self._nodev_banner.setVisible(not connected)

    def _set_accent(self, name):
        """Switch the accent colour theme — recolour the whole UI and persist the choice."""
        T.set_accent(name)
        app = QApplication.instance()
        if app:
            app.setStyleSheet(T.build_qss())          # recolour every QSS-styled widget at once
        self.cfg.data["accent"] = name
        try:
            self.cfg.save()
        except Exception:
            pass
        self._refresh_accent_swatches()
        self._refresh_conn()                          # the connected dot uses the accent
        self._refresh_all_slots()                     # key tiles (empty-key cue uses the accent)
        for b in self.slot_btns.values():             # repaint knobs/buttons (selection glow)
            b.update()
        self.controller.request_render()

    def _refresh_accent_swatches(self):
        cur = self.cfg.data.get("accent", "mint")
        for n, sw in getattr(self, "_accent_swatches", {}).items():
            sw.setChecked(n == cur)

    def _refresh_tabs(self):
        """Build the bottom page switcher: 'Pages:' + numbered pills + add (active highlighted)."""
        self._refresh_profile_label()
        while self.pages_row.count():
            it = self.pages_row.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for b in self.tab_group.buttons():
            self.tab_group.removeButton(b)

        self.pages_row.addStretch(1)
        if self.view_folder is not None:                 # in a folder: breadcrumb + folder-page pills
            f = self.cfg.folder(self.view_folder)
            back = QToolButton(objectName="pageadd")
            back.setText("←  Back to pages")
            back.setCursor(Qt.PointingHandCursor)
            back.clicked.connect(self._exit_folder_edit)
            self.pages_row.addWidget(back)
            self.pages_row.addWidget(QLabel(f"📁  {f.get('name', 'Folder')}", objectName="section"))
            self.pages_row.addSpacing(10)
            fpages = f.get("pages", [{"items": {}}])
            self.view_folder_page = max(0, min(self.view_folder_page, len(fpages) - 1))
            for i in range(len(fpages)):
                t = QToolButton(objectName="pagepill")
                t.setCheckable(True)
                t.setText(str(i + 1))
                t.setToolTip(f"Folder page {i + 1} — click to open · right-click to delete")
                t.setChecked(i == self.view_folder_page)
                t.setCursor(Qt.PointingHandCursor)
                t.clicked.connect(lambda _=False, idx=i: self._goto_folder_page(idx))
                t.setContextMenuPolicy(Qt.CustomContextMenu)
                t.customContextMenuRequested.connect(
                    lambda pos, idx=i, btn=t: self._folder_page_menu(idx, btn.mapToGlobal(pos)))
                self.tab_group.addButton(t)
                self.pages_row.addWidget(t)
            addb = QToolButton(objectName="pageadd")
            addb.setText("＋")
            addb.setCursor(Qt.PointingHandCursor)
            addb.setToolTip("Add a page to this folder")
            addb.clicked.connect(self._add_folder_page)
            self.pages_row.addWidget(addb)
            self.pages_row.addStretch(1)
            return

        self.pages_row.addWidget(QLabel("Pages:", objectName="scopehint"))
        for i, pg in enumerate(self.pages()):
            t = QToolButton(objectName="pagepill")
            t.setCheckable(True)
            t.setText(str(i + 1))
            nm = pg.get("name", f"Page {i + 1}")
            t.setToolTip(f"{nm} — click to open · right-click to rename / move / delete")
            t.setChecked(i == self.cur_page)
            t.setCursor(Qt.PointingHandCursor)
            t.clicked.connect(lambda _=False, idx=i: self._goto_page(idx))
            t.setContextMenuPolicy(Qt.CustomContextMenu)
            t.customContextMenuRequested.connect(
                lambda pos, idx=i, btn=t: self._page_tab_menu(idx, btn.mapToGlobal(pos)))
            self.tab_group.addButton(t)
            self.pages_row.addWidget(t)

        addb = QToolButton(objectName="pageadd")
        addb.setText("＋")
        addb.setCursor(Qt.PointingHandCursor)
        addb.setToolTip("Add a page")
        addb.clicked.connect(lambda: self._add_page())
        self.pages_row.addWidget(addb)
        self.pages_row.addStretch(1)

    def _tick_live_mirror(self):
        """WYSIWYG: re-render the visible page's LIVE tiles each second so the editor mirrors the
        device instead of freezing at the last edit. Only runs while the window is visible (the
        timer stops on hideEvent); non-live keys are untouched, and a wallpaper preview page is
        skipped so the panel frame isn't overdrawn."""
        if not self.isVisible():
            return
        if self.view_folder is None and (self.page().get("panel") or {}).get("path"):
            return
        items = self.items()
        for kid, b in self.key_btns.items():
            if self._is_back_key(kid):
                continue
            item = items.get(kid)
            if item and item.get("live"):
                try:
                    b.set_face(self._face(item))
                except Exception:
                    pass

    def _refresh_all_slots(self):
        items = self.items()
        in_folder = self.view_folder is not None
        last = LCD_KEYS[-1]
        # WYSIWYG: if this page has an animated wallpaper, preview its first frame on the keys.
        # Cache the decode (path+mtime+gap+size) — _refresh_all_slots runs on every edit/page switch
        # and would otherwise re-read+decode the wallpaper from disk each time.
        panel_tiles = None
        panel = None if in_folder else self.page().get("panel")
        ppath = panel.get("path", "") if panel else ""
        if ppath and os.path.exists(ppath):
            try:
                gap = panel.get("gap", 22)
                key = (ppath, gap, images.KEY_SIZE, os.path.getmtime(ppath))
                cached = getattr(self, "_panel_preview_cache", None)
                if cached and cached[0] == key:
                    panel_tiles = cached[1]
                else:
                    fr = panel_frames(ppath, gap=gap, max_frames=1)
                    panel_tiles = fr[0] if fr else None
                    self._panel_preview_cache = (key, panel_tiles)
            except Exception:
                panel_tiles = None
        for kid, b in self.key_btns.items():
            b.setToolTip("")                              # stale back-key tooltip must not linger off-folder
            if self._is_back_key(kid):
                folder = self.cfg.folders_of().get(self.view_folder, {})
                back = {**_FOLDER_BACK, **(folder.get("back") or {})}
                b.set_face(render_face(back, show_label=back.get("show_label", True)))
                b.setProperty("selected", kid == self.sel)
                b.setToolTip("Back key — click to customise its look (label, icon, colour). "
                             "Its return-to-page action stays fixed.")
            elif panel_tiles is not None:
                b.set_face(panel_tiles[LCD_KEYS.index(kid)])
                b.setProperty("selected", kid == self.sel)
            else:
                b.set_face(self._face(items.get(kid)))
                b.setProperty("selected", kid == self.sel)
            b.style().unpolish(b)
            b.style().polish(b)
        for sid, b in self.slot_btns.items():
            b.setSelected(self._ctrl_selected(sid))
        self._refresh_slot_captions()

    def _ctrl_selected(self, sid):
        # buttons match exactly; a knob (enc0) matches any of its sub-actions (enc0-/enc0/enc0+).
        return self.sel == sid or (sid.startswith("enc") and self.sel.startswith(sid))

    def _effective(self, sid):
        """Read-only effective binding (no override materialised): enc -> page item else global;
        keys -> page/folder items; buttons -> global."""
        if sid.startswith("enc"):
            return self.page().get("items", {}).get(sid) or self._globals().get(sid)
        if sid.startswith("key"):
            return self.items().get(sid)
        return self._globals().get(sid)

    def _control_caption(self, sid):
        """Label drawn inside a knob on the stage — a user-set custom name if any, else auto."""
        if sid.startswith("enc"):
            custom = self._custom_caption(sid)
            if custom:
                return custom
        return self._auto_caption(sid)

    def _auto_caption(self, sid):
        """The default 'what it does' label, derived from the bound action(s)."""
        def short(b):
            t = ((b or {}).get("action") or {}).get("type")
            return _CTRL_SHORT.get(t, (t or "").title()) if t and t != "none" else ""
        if sid.startswith("enc"):
            n = sid[3]
            for sub in ("+", "", "-"):      # prefer the turn action, then push, then turn-left
                lbl = short(self._effective(f"enc{n}{sub}"))
                if lbl:
                    return lbl
            return ""
        return short(self._effective(sid))

    def _custom_caption(self, base):
        """A user-set name for a dial (per page), or None."""
        return (self.page().get("captions") or {}).get(base)

    def _set_dial_caption(self, base, text):
        """Rename a knob — store a per-page custom caption (blank reverts to the auto name)."""
        store = self.page().setdefault("captions", {})
        text = (text or "").strip()
        if text:
            store[base] = text
        else:
            store.pop(base, None)
        self._render_timer.start()                       # debounce the save (fires per keystroke)
        b = self.slot_btns.get(base)
        if b is not None:
            b.setCaption(self._control_caption(base))    # live update on the stage

    def _refresh_slot_captions(self):
        # only the knobs are big enough for a tidy inline label; the small round buttons
        # stay clean (their function lives in the tooltip).
        for sid, b in self.slot_btns.items():
            b.setCaption(self._control_caption(sid) if sid.startswith("enc") else "")

    def _refresh_key_preview(self, kid, face=None):
        if kid not in self.key_btns:
            return
        if self._is_back_key(kid):
            folder = self.cfg.folders_of().get(self.view_folder, {})
            back = {**_FOLDER_BACK, **(folder.get("back") or {})}
            self.key_btns[kid].set_face(render_face(back, show_label=back.get("show_label", True)))
        else:                                             # reuse an already-rendered face if given
            self.key_btns[kid].set_face(face if face is not None else self._face(self.items().get(kid)))

    # ---- selection / editor ------------------------------------------------
    def select(self, sid):
        # Inside a folder the last key is the Back tile — selectable so its look can be edited
        # (use the breadcrumb above to leave the folder). Its 'go back' action stays fixed.
        if sid != self.sel:
            self._toggle_edit = 0           # a new control resets toggle-state editing to State 1
        self.sel = sid
        self._gesture_slot = "tap"          # switching control resets the edited gesture to Tap
        for kid, b in self.key_btns.items():
            b.setProperty("selected", kid == sid)
            b.style().unpolish(b); b.style().polish(b)
        for s, b in self.slot_btns.items():
            b.setSelected(self._ctrl_selected(s))
        self._populate_editor()

    def _select_sub(self, sid):
        """Switch the edited encoder sub-action (segment buttons) — repopulate, no re-reveal."""
        self.sel = sid
        for s, b in self.slot_btns.items():
            b.setSelected(self._ctrl_selected(s))
        self._populate_editor()

    def _clear_editor(self):
        while self.editor_cols.count():            # editor_cols is a QSplitter — remove panes
            w = self.editor_cols.widget(0)
            w.setParent(None)                      # remove from view now, not on the next loop
            w.deleteLater()
        self._card_prefs = []

    def _delete_layout(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif it.layout():
                self._delete_layout(it.layout())

    def _populate_editor(self):
        self._clear_editor()
        sid = self.sel
        # The folder Back tile: customise its look; its action is fixed (return to the page).
        if self._is_back_key(sid):
            self._populate_back_editor()
            self._animate_editor_in()
            return
        item = self._store_item(sid)
        item.setdefault("action", {"type": "none"})
        is_lcd = sid.startswith("key")
        if is_lcd:
            self.ed_title.setText(f"Key {sid[3:]}")
            self.ed_scope.setText("Per page · this key's look and action.")
        elif sid.startswith("btn"):
            self.ed_title.setText(f"Button {sid[3:]}")
            self.ed_scope.setText("Shared on every page of this profile.")
        else:
            sub = {"+": "turn right", "-": "turn left"}.get(sid[-1], "push")
            self.ed_title.setText(f"Encoder {sid[3]} · {sub}")
            self.ed_scope.setText("Per page · each page can set this knob differently.")

        if sid.startswith("enc"):                 # knob: name+quick in one column, the 3 roles in another
            base = f"enc{sid[3]}"
            nq = QWidget()
            nqv = QVBoxLayout(nq)
            nqv.setContentsMargins(0, 0, 0, 0)
            nqv.setSpacing(10)
            nqv.addWidget(self._enc_name_row(base))
            nqv.addWidget(self._enc_quick_row(base))
            self._add_editor_col(nq, 300, "Dial")
            self._add_editor_col(self._enc_segment_row(sid), 312, "Roles")

        self._add_editor_col(self._build_action_group(item), 470, "Action")
        if is_lcd:                                # appearance is a wide 3-sub-column card
            self._add_editor_col(self._build_face_group(item), 556, "Appearance & icon")
        self._ed_clear_btn.setText("Clear")
        self._ed_clear_btn.setToolTip("Reset this slot — removes its action"
                                      + (" and appearance" if is_lcd else ""))
        self._ed_test_btn.setVisible(True)
        self._ed_test_btn.setEnabled((self._act(item).get("type") or "none") != "none")
        if self._card_prefs:
            self.editor_cols.setSizes(self._card_prefs)   # initial split = each card's preferred width
        self._fit_inspector_width()
        self._animate_editor_in()

    def _animate_editor_in(self):
        """Subtle bloom on the editor when a different control is selected — a 130 ms OutCubic opacity
        fade-in (content never fades OUT, so nothing delays the eye before the new editor lands)."""
        if not getattr(self, "_ready", False):
            return
        prev = getattr(self, "_editor_fade", None)
        if prev is not None:
            prev.stop()
        eff = QGraphicsOpacityEffect(self.editor_cols)
        self.editor_cols.setGraphicsEffect(eff)
        an = QPropertyAnimation(eff, b"opacity", self)
        an.setDuration(130)
        an.setStartValue(0.4)
        an.setEndValue(1.0)
        an.setEasingCurve(QEasingCurve.OutCubic)
        an.finished.connect(lambda: self.editor_cols.setGraphicsEffect(None))   # drop the effect when done
        self._editor_fade = an
        an.start()

    def _fit_inspector_width(self):
        """Keep each editor column tidy: cap combo min-widths (long audio-device / item names), cap
        input MAX widths so a lone full-width card (a screenless button) can't stretch its fields
        across the whole bar, and let wrapped labels shrink to the column instead of forcing it wider."""
        for cb in self.editor_host.findChildren(QComboBox):
            cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(8)
            cb.setMaximumWidth(300)
        for le in self.editor_host.findChildren(QLineEdit):
            le.setMaximumWidth(380)
        for sp in self.editor_host.findChildren(QSpinBox):
            sp.setMaximumWidth(130)
        for lb in self.editor_host.findChildren(QLabel):
            if lb.wordWrap():
                lb.setMinimumWidth(1)
                lb.setMaximumWidth(320)

    def _populate_back_editor(self):
        """Editor for the folder's Back tile — appearance only; the action is fixed."""
        folder = self.cfg.folder(self.view_folder)
        back = folder.setdefault("back", {})
        for k, v in _FOLDER_BACK.items():
            back.setdefault(k, v)                  # seed defaults so the fields show current look
        self.ed_title.setText("Back key")
        self.ed_scope.setText("Returns to the page · the action is fixed, customise how it looks.")
        self._add_editor_col(self._build_face_group(back, allow_live=False), 580,
                             "Appearance & icon")   # the Back tile never ticks -> no Live group
        self._ed_clear_btn.setText("Reset Back")
        self._ed_clear_btn.setToolTip("Reset the folder Back key to its default look")
        self._ed_test_btn.setVisible(False)
        if self._card_prefs:
            self.editor_cols.setSizes(self._card_prefs)
        self._fit_inspector_width()

    def _reset_back(self):
        self.cfg.folder(self.view_folder).pop("back", None)
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self.select(self.sel)

    def _clear_current(self):
        """The inspector's header Clear button — resets the Back key, else clears the selected slot."""
        if self._is_back_key(self.sel):
            self._reset_back()
        else:
            self._clear_binding()

    def _clear_binding(self):
        self._store(self.sel)[self.sel] = {"action": {"type": "none"}}
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self.select(self.sel)

    _LABEL_W = 88          # shared form-label column so every field lines up in the inspector

    def _align_labels(self, form):
        """Give every QFormLayout label the same fixed width, so the fields line up down
        the whole inspector (Appearance + Action) even across separate group boxes."""
        for r in range(form.rowCount()):
            it = form.itemAt(r, QFormLayout.LabelRole)
            if it is not None and it.widget() is not None:
                it.widget().setFixedWidth(self._LABEL_W)
                it.widget().setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

    def _color_field(self, initial, on_pick):
        """A colour well + its hex, as a single form field (so it aligns with the inputs)."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        hexlbl = QLabel(initial, objectName="dim")
        btn = self._color_button(initial, lambda c: (hexlbl.setText(c), on_pick(c)))
        h.addWidget(btn)
        h.addWidget(hexlbl)
        h.addStretch(1)
        return w

    def _build_face_group(self, item, allow_live=True):
        """Appearance editor as LAYER-ordered, labelled groups — Background · Icon · Live data ·
        Text — so every control's target is obvious: colours aren't mixed with display toggles,
        'Show label' sits next to the Label field, and it's explicit that live data REPLACES
        the icon (the icon group greys out while live is on)."""
        g = QWidget()                                 # the inspector card provides the frame + title
        outer = QHBoxLayout(g)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(22)
        live_on = bool((item.get("live") or {}).get("source"))
        self._insp_item = item                        # the binding this inspector edits (for previews)
        dpr = _dpr(g)

        def _bicon(btn, name):
            ic = fluent_qicon(name, normal=T.TEXT_DIM, active=T.TEXT, size=15, widget=btn)
            if ic is not None:
                btn.setIcon(ic)
                btn.setIconSize(QSize(15, 15))

        # ── column 1: live preview + the background layer BEHIND everything ──
        c1 = QVBoxLayout(); c1.setSpacing(8)
        self.face_preview = QLabel()
        self.face_preview.setFixedSize(self._PREVIEW, self._PREVIEW)
        self.face_preview.setAlignment(Qt.AlignCenter)
        self.face_preview.setPixmap(face_pixmap(self._face(item), self._PREVIEW, self.face_preview))
        prow = QHBoxLayout(); prow.addStretch(1); prow.addWidget(self.face_preview); prow.addStretch(1)
        c1.addLayout(prow)
        c1.addWidget(QLabel("Background", objectName="section"))
        c1.addWidget(self._color_field(
            item.get("color", DEFAULT_BG), lambda c: self._set_face(item, "color", c)))
        grad_btn = QPushButton("Gradient ✓" if item.get("bg2") else "Gradient…")
        grad_btn.setToolTip("Use a colour-gradient background — pick a preset or set your own colours")
        grad_btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        grad_btn.setMinimumWidth(78)
        grad_btn.clicked.connect(lambda: self._open_gradient(item))
        _bicon(grad_btn, "color")
        c1.addWidget(grad_btn)
        c1.addStretch(1)
        outer.addLayout(c1)

        # ── column 2: the icon layer (inert while live data owns the face) ──
        c2 = QVBoxLayout(); c2.setSpacing(6)
        c2.addWidget(QLabel("Icon", objectName="section"))
        btns = QGridLayout(); btns.setSpacing(6)
        emo = QPushButton("Emoji"); emo.clicked.connect(lambda: self._pick_emoji(item))
        em_pil = emoji_image("😀", int(round(15 * dpr)))     # the one colour icon — it IS the picker
        if em_pil is not None:
            emo.setIcon(QIcon(pil_to_pixmap(em_pil, dpr)))
            emo.setIconSize(QSize(15, 15))
        icn = QPushButton("Icons"); icn.setToolTip("Crisp built-in Fluent icons — cohesive line icons")
        icn.clicked.connect(lambda: self._pick_fluent(item))
        _bicon(icn, "grid")
        img = QPushButton("Image…"); img.clicked.connect(lambda: self._pick_image(item))
        _bicon(img, "photo")
        clr = QPushButton("Clear"); clr.setToolTip("Remove the icon (and its styling)")
        clr.clicked.connect(lambda: self._clear_icon(item))
        for b in (emo, icn, img, clr):
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed); b.setMinimumWidth(78)
        btns.addWidget(emo, 0, 0); btns.addWidget(icn, 0, 1)
        btns.addWidget(img, 1, 0); btns.addWidget(clr, 1, 1)
        c2.addLayout(btns)
        self.icon_edit = QLineEdit(item.get("icon", ""))
        self.icon_edit.setPlaceholderText("or type an emoji / image path")
        self.icon_edit.setClearButtonEnabled(True)
        self.icon_edit.textChanged.connect(lambda t: self._set_face(item, "icon", t))
        c2.addWidget(self.icon_edit)
        fill = QCheckBox("Crop image to fill")
        fill.setToolTip("On: crop the image to fill the key. Off: show the whole image, letterboxed.")
        fill.setChecked(effective_fit(item) == "cover")
        fill.toggled.connect(lambda on: self._set_flag(item, "fit", "cover" if on else "contain"))
        c2.addWidget(fill)
        # Colour-/effect-action key (Prisma/Tapo) with no custom icon: it shows a rounded swatch
        # (solid colour or a gradient for effects like Rainbow) — offer a size slider for it.
        if has_swatch(item) and images.AUTO_ICONS and not (item.get("icon") or "").strip() and not live_on:
            srow = QHBoxLayout(); srow.setSpacing(6)
            srow.addWidget(QLabel("Swatch size", objectName="dim"))
            ss = QSlider(Qt.Horizontal); ss.setRange(40, 100)
            try:
                ss.setValue(int(float(item.get("swatch_size") or 70)))
            except (TypeError, ValueError):
                ss.setValue(70)
            sslab = QLabel(f"{ss.value()}%", objectName="sliderval")
            sslab.setAlignment(Qt.AlignRight | Qt.AlignVCenter); sslab.setMinimumWidth(42)

            def _set_swatch_size(x, it=item, lb=sslab):
                it["swatch_size"] = x
                lb.setText(f"{x}%")
                if getattr(self, "face_preview", None) is not None:   # keep the big preview in sync too
                    try:
                        self.face_preview.setPixmap(
                            face_pixmap(self._face(it), self._PREVIEW, self.face_preview))
                    except RuntimeError:
                        pass
                self._refresh_key_preview(self.sel)
                self._render_timer.start()
            ss.valueChanged.connect(_set_swatch_size)
            srow.addWidget(ss, 1); srow.addWidget(sslab)
            c2.addLayout(srow)
        style_btn = QPushButton("Customize…")
        style_btn.setToolTip("Zoom · move · rotate · opacity · rounded corners · app tile · "
                             "border · shadow · background gradient")
        style_btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed); style_btn.setMinimumWidth(78)
        style_btn.clicked.connect(lambda: self._open_icon_style(item))
        _bicon(style_btn, "settings")
        c2.addWidget(style_btn)
        if live_on:                                   # live replaces the icon -> whole group inert
            for wdg in (emo, icn, img, clr, self.icon_edit, fill):
                wdg.setEnabled(False)
                wdg.setToolTip("Live data is on this key — remove it (on the right) to use an icon.")
        c2.addStretch(1)
        outer.addLayout(c2)

        # ── column 3: live data (replaces the icon) + the text layer on top ──
        c3 = QVBoxLayout(); c3.setSpacing(6)
        if allow_live:
            c3.addWidget(QLabel("Live data", objectName="section"))
            if live_on:
                src = (item.get("live") or {}).get("source", "")
                chip = QLabel(f"⟳  {livesrc.source_label(src)}", objectName="livechip")
                c3.addWidget(chip)
                lrow = QHBoxLayout(); lrow.setSpacing(6)
                ch = QPushButton("Change…"); ch.clicked.connect(lambda: self._pick_live(item))
                rm = QPushButton("Remove"); rm.setToolTip("Stop showing live data")
                _bicon(rm, "delete")
                rm.clicked.connect(lambda: self._clear_live(item))
                for b in (ch, rm):
                    b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed); b.setMinimumWidth(70)
                lrow.addWidget(ch); lrow.addWidget(rm)
                c3.addLayout(lrow)
                srow = QHBoxLayout(); srow.setSpacing(6)
                srow.addWidget(QLabel("Size", objectName="dim"))
                s = QSlider(Qt.Horizontal); s.setRange(40, 160)
                s.setValue(int(round(float(item.get("icon_scale", 1.0)) * 100)))
                slab = QLabel(f"{s.value()}%", objectName="sliderval")
                slab.setAlignment(Qt.AlignRight | Qt.AlignVCenter); slab.setMinimumWidth(42)

                def _set_live_size(x, it=item, lb=slab):
                    it["icon_scale"] = x / 100.0
                    lb.setText(f"{x}%")
                    self._refresh_key_preview(self.sel)
                    self._render_timer.start()
                s.valueChanged.connect(_set_live_size)
                srow.addWidget(s, 1); srow.addWidget(slab)
                c3.addLayout(srow)
            else:
                lb = QPushButton("Show live data…")
                lb.setToolTip("Clock, CPU, RAM, battery, weather… updated every second")
                lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed); lb.setMinimumWidth(78)
                _bicon(lb, "clock")
                lb.clicked.connect(lambda: self._pick_live(item))
                c3.addWidget(lb)
                cap = QLabel("Clock · CPU · weather — replaces the icon", objectName="dim")
                cap.setWordWrap(True)
                c3.addWidget(cap)
            c3.addSpacing(6)
        c3.addWidget(QLabel("Text", objectName="section"))
        tform = QFormLayout()
        tform.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        tform.setHorizontalSpacing(8); tform.setVerticalSpacing(8)
        label_edit = QLineEdit(item.get("label", ""))
        label_edit.textChanged.connect(lambda t: self._set_face(item, "label", t))
        tform.addRow("Label", label_edit)
        tform.addRow("Colour", self._color_field(
            item.get("text_color", "#ffffff"), lambda c: self._set_face(item, "text_color", c)))
        c3.addLayout(tform)
        gl = self.cfg.data.get("show_labels", True)
        title = QCheckBox("Show label")
        title.setToolTip("Show this key's label text on the dock.")
        title.setChecked(item.get("show_label", gl))
        title.toggled.connect(lambda on: self._set_flag(item, "show_label", bool(on)))
        c3.addWidget(title)
        c3.addStretch(1)
        outer.addLayout(c3)
        return g

    def _color_button(self, initial, on_pick):
        b = QPushButton()
        b.setFixedSize(50, 26)

        def style(c):
            b.setStyleSheet(f"background:{c}; border:1px solid {T.BORDER}; border-radius:{T.R_SM}px;")
        style(initial)
        b._color = initial

        def choose():
            col = QColorDialog.getColor(QColor(b._color), self, "Pick color")
            if col.isValid():
                hexc = col.name()
                b._color = hexc
                style(hexc)
                on_pick(hexc)
        b.clicked.connect(choose)
        return b

    def _pick_emoji(self, item):
        dlg = EmojiPicker(self)
        dlg.picked.connect(lambda e: self.icon_edit.setText(e))
        dlg.exec()

    def _pick_fluent(self, item):
        dlg = FluentIconDialog(self)
        if dlg.exec() and dlg.chosen:
            self.icon_edit.setText(f"fluent:{dlg.chosen}")   # textChanged -> _set_face updates the key

    def _open_icon_style(self, item):
        IconStyleDialog(self, item).exec()            # icon transform/effects + background gradient
        self._populate_editor()                       # resync the appearance fields

    def _open_gradient(self, item):
        """The prominent background picker: curated gradient presets + custom colours/direction."""
        GradientPicker(self, item).exec()             # edits item live; restores on Cancel
        self.cfg.save()
        self.controller.request_render()
        self.select(self.sel)                         # rebuild so the Background swatch + button resync

    def _pick_image(self, item):
        path, _ = QFileDialog.getOpenFileName(self, "Choose icon image", "",
                                              "Images (*.png *.jpg *.jpeg *.bmp *.gif *.ico)")
        if not path:
            return
        try:
            pil = Image.open(path)
        except (OSError, ValueError):
            return
        dlg = ImageCropDialog(pil, self)
        if dlg.exec() != QDialog.Accepted:
            return
        result = dlg.result_image or pil
        icons = os.path.join(config_dir(), "icons")
        os.makedirs(icons, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        dest = os.path.join(icons, f"{stem}.png")
        i = 1
        while os.path.exists(dest):
            dest = os.path.join(icons, f"{stem}_{i}.png")
            i += 1
        try:
            result.convert("RGBA").save(dest)
        except OSError:
            return
        self.icon_edit.setText(dest)

    def _pick_live(self, item):
        cur = (item.get("live") or {}).get("source")
        dlg = LiveDataPickerDialog(cur, self)
        if dlg.exec() and dlg.chosen:
            self._set_live(item, dlg.chosen)

    def _set_live(self, item, source):
        item["live"] = {"source": source}
        item.pop("icon", None)                       # live data replaces a static icon
        if getattr(self, "icon_edit", None):
            self.icon_edit.blockSignals(True)
            self.icon_edit.setText("")
            self.icon_edit.blockSignals(False)
        self.cfg.save()
        self._populate_editor()                      # rebuild inspector so the Live tag shows
        self._refresh_key_preview(self.sel)
        self.controller.request_render()

    def _clear_icon(self, item):
        item.pop("live", None)
        for sk in self._ICON_STYLE_KEYS:             # drop any zoom/round/tile styling too
            item.pop(sk, None)
        if getattr(self, "icon_edit", None):
            self.icon_edit.setText("")               # fires _set_face -> clears icon + refreshes
        self._populate_editor()
        self.controller.request_render()

    def _browse_sound(self, a, edit):
        path, _ = QFileDialog.getOpenFileName(self, "Choose sound", "",
                                              "Audio (*.wav *.ogg *.flac *.mp3 *.aiff)")
        if not path:
            return
        snd = os.path.join(config_dir(), "sounds")
        os.makedirs(snd, exist_ok=True)
        dest = os.path.join(snd, os.path.basename(path))
        try:
            shutil.copyfile(path, dest)
        except OSError:
            dest = path
        edit.setText(dest)

    def _build_soundboard(self):
        """Turn a folder/selection of audio clips into a ready board: one sound key per clip,
        spread across new pages (plus an optional Stop key), all routed to one output device."""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choose sound clips for the board", "",
            "Audio (*.wav *.ogg *.flac *.mp3 *.aiff *.m4a *.aac)")
        if not files:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Build soundboard")
        dlg.setMinimumWidth(390)
        form = QFormLayout(dlg)
        form.addRow(QLabel(f"{len(files)} clip(s) selected.", objectName="dim"))
        dev = QComboBox()
        dev.addItem("System default", "")
        try:
            from .sound import list_outputs
            for nm in list_outputs():
                dev.addItem(nm, nm)
        except Exception:
            pass
        dev.setCurrentIndex(max(0, dev.findData(self.cfg.data.get("sound_device", "") or "")))
        form.addRow("Output", dev)
        mon = QCheckBox("Also play on my speakers")
        mon.setChecked(True)
        form.addRow(mon)
        stopk = QCheckBox("Add a “Stop all” key")
        stopk.setChecked(True)
        form.addRow(stopk)
        info = QLabel("Adds new pages (6 clips per page) and jumps to the first — your existing "
                      "pages are untouched. For Discord, set Output to its mic (a virtual cable).",
                      objectName="dim")
        info.setWordWrap(True)
        form.addRow(info)
        row = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(dlg.reject)
        ok = QPushButton("Build")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(dlg.accept)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(ok)
        form.addRow(row)
        if not dlg.exec():
            return
        device = dev.currentData()
        monitor = mon.isChecked()
        self.cfg.data["sound_device"] = device
        snd = os.path.join(config_dir(), "sounds")
        os.makedirs(snd, exist_ok=True)
        keys = []
        if stopk.isChecked():
            keys.append({"label": "Stop", "icon": "⏹", "color": "#3a1414",
                         "action": {"type": "sound", "mode": "stop"}})
        for f in files:
            dest = os.path.join(snd, os.path.basename(f))      # keep clips alongside the config
            try:
                if os.path.abspath(f) != os.path.abspath(dest):
                    shutil.copyfile(f, dest)
            except OSError:
                dest = f
            name = os.path.splitext(os.path.basename(f))[0][:12]
            keys.append({"label": name, "icon": "🔊",
                         "action": {"type": "sound", "mode": "play", "file": dest,
                                    "device": device, "monitor": monitor, "gain": 1.0}})
        self._place_keys_across_pages(keys)
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self._refresh_tabs()
        self.select("key1")

    def _place_keys_across_pages(self, keys):
        """Append fresh 'Sounds N' pages and lay the keys out 6 per page; jump to the first."""
        pages = self.cfg.pages()
        per = len(LCD_KEYS)
        first = len(pages)
        for i, k in enumerate(keys):
            p = first + i // per
            while p >= len(pages):
                pages.append({"name": f"Sounds {len(pages) - first + 1}", "items": {}})
            pages[p].setdefault("items", {})[LCD_KEYS[i % per]] = k
        self.cur_page = min(first, len(pages) - 1)

    _PREVIEW = 86          # appearance live-preview size (label + every update must match)

    def _set_flag(self, item, key, value):
        """Always store value (booleans / fit), then refresh previews + device."""
        item[key] = value
        if getattr(self, "face_preview", None):
            try:
                self.face_preview.setPixmap(
                    face_pixmap(self._face(item), self._PREVIEW, self.face_preview))
            except RuntimeError:
                pass
        self._refresh_key_preview(self.sel)
        self._render_timer.start()

    _ICON_STYLE_KEYS = ("icon_scale", "icon_radius", "icon_tile", "icon_tile_color",
                        "icon_dx", "icon_dy", "icon_rotate", "icon_opacity",
                        "icon_border", "icon_border_color", "icon_shadow")

    def _set_face(self, item, key, value):
        if value:
            item[key] = value
            if key == "icon":
                item.pop("live", None)               # a static icon replaces live data
                item.pop("icon_auto", None)          # a hand-picked icon is no longer auto
                for sk in self._ICON_STYLE_KEYS:     # a new icon starts with a fresh style
                    item.pop(sk, None)
        else:
            item.pop(key, None)
        face = self._face(item)                          # render once; reuse for preview + tile
        if hasattr(self, "face_preview") and self.face_preview:
            try:
                self.face_preview.setPixmap(face_pixmap(face, self._PREVIEW, self.face_preview))
            except RuntimeError:
                pass
        self._refresh_key_preview(self.sel, face=face)
        self._render_timer.start()

    def _set_fullscreen_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Full-screen image (spread across all 6 keys)", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif)")
        if not path:
            return
        if QMessageBox.question(self, "Full-screen image",
                                "Replace all 6 keys on this page with image tiles?") != QMessageBox.Yes:
            return
        icons = os.path.join(config_dir(), "icons")
        tiles = slice_fullscreen(path, icons, tag=f"fs_p{self.cur_page}")
        items = self.items()
        for i, kid in enumerate(LCD_KEYS):
            it = items.setdefault(kid, {})
            it["icon"] = tiles[i]
            it["fit"] = "cover"
            it.pop("label", None)
            it.pop("color", None)
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self.select(self.sel)

    def _set_panel_wallpaper(self):
        if self.view_folder is not None:
            QMessageBox.information(self, "Animated wallpaper",
                                    "Exit the folder first — wallpaper is set per page.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Animated wallpaper (plays across all 6 keys)", "",
            "Images / GIF (*.gif *.png *.jpg *.jpeg *.bmp *.webp)")
        if not path:
            return
        icons = os.path.join(config_dir(), "icons")
        os.makedirs(icons, exist_ok=True)
        ext = os.path.splitext(path)[1].lower() or ".gif"
        dest = os.path.join(icons, f"panel_p{self.cur_page}{ext}")
        try:
            shutil.copyfile(path, dest)
        except OSError:
            dest = path
        fps, ok = QInputDialog.getInt(self, "Animated wallpaper",
                                      "Frames per second (1–60 — 6-key frames are USB-limited, "
                                      "~15–25 fps in practice):", 12, 1, 60, 1)
        if not ok:
            return                            # Cancel aborts — don't install the wallpaper
        self.page()["panel"] = {"path": dest, "fps": int(fps), "gap": 22}
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self.select(self.sel)

    def _clear_page_images(self):
        if QMessageBox.question(self, "Clear images",
                                "Clear the wallpaper plus the icon, label and colour on all 6 keys "
                                "of this page?") != QMessageBox.Yes:
            return
        self.page().pop("panel", None)
        items = self.items()
        for kid in LCD_KEYS:
            it = items.get(kid)
            if it:
                for k in ("icon", "fit", "label", "color", "text_color", "live"):
                    it.pop(k, None)
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self.select(self.sel)

    # ---- action editor -----------------------------------------------------
    # Gesture slots: a key/button can carry up to three actions (tap / double-tap / hold).
    _GESTURE_SLOTS = (("tap", "action", "Tap"),
                      ("double", "action_double", "Double-tap"),
                      ("hold", "action_hold", "Hold"))

    def _slot_key(self, slot=None):
        slot = slot or getattr(self, "_gesture_slot", "tap")
        return {"tap": "action", "double": "action_double", "hold": "action_hold"}.get(slot, "action")

    @staticmethod
    def _toggle_states(ta):
        """The states list of a toggle action — always ≥2 (each {label,color,action})."""
        sts = ta.setdefault("states", [])
        while len(sts) < 2:
            sts.append({"label": "", "color": "", "action": {"type": "none"}})
        return sts

    def _editing_toggle_state(self, item):
        """If this key's action is a toggle, the state dict currently being edited — else None."""
        ta = item.get(self._slot_key())
        if isinstance(ta, dict) and ta.get("type") == "toggle":
            states = self._toggle_states(ta)
            return states[max(0, min(getattr(self, "_toggle_edit", 0), len(states) - 1))]
        return None

    def _act(self, item):
        """The action dict currently being edited. For a toggle key that's the CURRENT state's
        nested action (so the whole picker + every action-type's fields work inside a toggle);
        otherwise the gesture slot's action."""
        st = self._editing_toggle_state(item)
        if st is not None:
            return st.setdefault("action", {"type": "none"})
        return item.setdefault(self._slot_key(), {"type": "none"})

    def _set_act(self, item, action):
        st = self._editing_toggle_state(item)
        if st is not None:
            st["action"] = action
        else:
            item[self._slot_key()] = action

    def _gestures_apply(self):
        """Tap/double/hold only make sense on the pressable controls (LCD keys + round buttons)."""
        return (self.sel.startswith("key") or self.sel.startswith("btn")) and not self._is_back_key(self.sel)

    def _build_gesture_tabs(self, item):
        """A Tap / Double-tap / Hold selector — each edits its own action; a dot marks a set slot."""
        row = QHBoxLayout()
        row.setSpacing(6)
        self._gesture_btns = {}
        for slot, key, label in self._GESTURE_SLOTS:
            set_ = self._nonempty_action(item.get(key))
            b = QToolButton(objectName="gslot")
            b.setText(("● " if set_ else "") + label)
            b.setCheckable(True)
            b.setChecked(slot == self._gesture_slot)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setToolTip({"tap": "Fires on a normal press",
                          "double": "Fires on two quick presses",
                          "hold": "Fires when held ~0.5s"}[slot])
            b.clicked.connect(lambda _=False, s=slot: self._select_gesture(s))
            self._gesture_btns[slot] = b
            row.addWidget(b)
        return row

    @staticmethod
    def _nonempty_action(a):
        return isinstance(a, dict) and (a.get("type") or "none") not in ("none", "", None)

    def _select_gesture(self, slot):
        self._gesture_slot = slot
        self._populate_editor()

    # ---- toggle action editor ---------------------------------------------
    def _build_toggle_header(self, item, ta):
        """A 'State 1 / State 2' switcher (like the gesture tabs) + the current state's label &
        colour. The action picker + fields below then edit THIS state's nested action."""
        states = self._toggle_states(ta)
        self._toggle_edit = max(0, min(getattr(self, "_toggle_edit", 0), len(states) - 1))
        box = QVBoxLayout()
        box.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(6)
        for i in range(len(states)):
            set_ = self._nonempty_action(states[i].get("action"))
            b = QToolButton(objectName="gslot")
            b.setText(("● " if set_ else "") + f"State {i + 1}")
            b.setCheckable(True)
            b.setChecked(i == self._toggle_edit)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.clicked.connect(lambda _=False, idx=i: self._select_toggle_state(idx))
            row.addWidget(b)
        box.addLayout(row)
        cap = QLabel("Each press flips to the next state and fires its action. Give this state a "
                     "label + colour, then set its action below.", objectName="dim")
        cap.setWordWrap(True)
        box.addWidget(cap)
        st = states[self._toggle_edit]
        frow = QHBoxLayout()
        frow.setSpacing(8)
        frow.addWidget(QLabel("Label", objectName="dim"))
        le = QLineEdit(st.get("label", ""))
        le.setMaximumWidth(150)
        le.setPlaceholderText(f"State {self._toggle_edit + 1}")
        le.textChanged.connect(lambda val: self._set_toggle_face(st, "label", val))
        frow.addWidget(le)
        frow.addSpacing(8)
        frow.addWidget(QLabel("Colour", objectName="dim"))
        cbtn = QPushButton()
        cbtn.setFixedSize(30, 24)
        cbtn.setCursor(Qt.PointingHandCursor)
        self._style_swatch(cbtn, st.get("color") or DEFAULT_BG)
        cbtn.clicked.connect(lambda: self._pick_toggle_colour(st, cbtn))
        frow.addWidget(cbtn)
        frow.addStretch(1)
        box.addLayout(frow)
        return box

    @staticmethod
    def _style_swatch(btn, col):
        btn.setStyleSheet(f"background:{col}; border:1px solid #3a4a40; border-radius:6px;")

    def _select_toggle_state(self, idx):
        self._toggle_edit = idx
        self._populate_editor()
        self._refresh_key_preview(self.sel)   # preview the now-edited state on the key

    def _set_toggle_face(self, st, key, val):
        st[key] = val
        self.cfg.save()
        self._refresh_key_preview(self.sel)
        self.controller.request_render()

    def _pick_toggle_colour(self, st, btn):
        c = QColorDialog.getColor(QColor(st.get("color") or DEFAULT_BG), self, "State colour")
        if c.isValid():
            st["color"] = c.name()
            self._style_swatch(btn, c.name())
            self.cfg.save()
            self._refresh_key_preview(self.sel)
            self.controller.request_render()

    def _build_action_group(self, item):
        """Laid out wide-and-short like the Appearance card: gestures span the top, then two
        sub-columns — the current-action chip on the left, its settings fields on the right.
        One job per card: live data lives in Appearance, starter presets in the key's
        right-click menu, so this card stays small."""
        g = QWidget()                                 # the inspector card provides the frame + title
        v = QVBoxLayout(g)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(7)
        if not self.sel.startswith("key"):
            hint = QLabel("This control has no screen — set its action below.", objectName="dim")
            hint.setWordWrap(True)
            v.addWidget(hint)
        ta = item.get(self._slot_key())
        is_toggle = isinstance(ta, dict) and ta.get("type") == "toggle"
        if is_toggle:
            v.addLayout(self._build_toggle_header(item, ta))   # State 1 / State 2 + per-state look
        elif self._gestures_apply():
            v.addLayout(self._build_gesture_tabs(item))   # full-width gesture row on top
            if self._gesture_slot != "tap":
                cap = QLabel("Editing the <b>%s</b> action — set it to “No action” to clear."
                             % dict(((s, l) for s, _k, l in self._GESTURE_SLOTS))[self._gesture_slot],
                             objectName="dim")
                cap.setWordWrap(True)
                v.addWidget(cap)
        a = self._act(item)

        cols = QHBoxLayout()
        cols.setSpacing(20)
        colAw = QWidget(); colAw.setFixedWidth(214)   # fixed so the action picker label isn't clipped
        colA = QVBoxLayout(colAw); colA.setContentsMargins(0, 0, 0, 0); colA.setSpacing(7)
        colBw = QWidget()
        colB = QVBoxLayout(colBw); colB.setContentsMargins(0, 0, 0, 0); colB.setSpacing(7)

        # The card title already says "Action"; show only a slim chip of what's bound. Assigning /
        # changing actions is the RIGHT sidebar's job (drag, or click to bind the selection) — this
        # chip just reflects the current action and opens the picker on click, so there's no second,
        # competing "add action" control down here.
        self._action_picker_btn = QPushButton(objectName="actionchip")
        self._action_picker_btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._action_picker_btn.setMinimumWidth(110)   # fills colA (fixed 214) so the label shows
        self._action_picker_btn.setCursor(Qt.PointingHandCursor)
        self._action_picker_btn.setToolTip("Current action — click to change, or drag one from the list on the right →")
        self._action_picker_btn.clicked.connect(lambda: self._open_action_picker(item))
        self._sync_action_chip(a.get("type", "none"))
        colA.addWidget(self._action_picker_btn)
        colA.addStretch(1)

        # ── right sub-column: the chosen action's settings fields ──
        self.action_fields_host = QWidget()
        self.action_fields_layout = QVBoxLayout(self.action_fields_host)
        self.action_fields_layout.setContentsMargins(0, 0, 0, 0)
        self._build_action_fields(item)
        colB.addWidget(self.action_fields_host)
        colB.addStretch(1)

        cols.addWidget(colAw, 0)
        cols.addWidget(colBw, 1)
        v.addLayout(cols)
        # Cap + left-align the whole editor so a lone full-width card (a screenless button/encoder
        # with only this card) doesn't stretch the gesture pills and fields across the entire bar.
        # A normal two-card key pane (~640px) is narrower than the cap, so it still fills cleanly.
        g.setMaximumWidth(680)
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(g)
        wl.addStretch(1)
        return wrap

    def _clear_live(self, item):
        item.pop("live", None)
        self.cfg.save()
        self._refresh_key_preview(self.sel)
        self.controller.request_render()
        self.select(self.sel)

    def _picker_label(self, t):
        return f"  {ACTION_LABELS.get(t, t)}        ▾"

    def _sync_action_chip(self, t):
        """Keep the current-action chip's text + line-art icon in step with the bound action."""
        btn = getattr(self, "_action_picker_btn", None)
        if btn is None:
            return
        btn.setText(self._picker_label(t))
        dpr = _dpr(btn)
        art = action_art(t, int(round(18 * dpr)), color=RAIL_ICON_COLOR)
        btn.setIcon(QIcon(pil_to_pixmap(art, dpr)) if art is not None else QIcon())
        btn.setIconSize(QSize(18, 18))

    def _open_action_picker(self, item):
        dlg = ActionPickerDialog(self._act(item).get("type", "none"), self)
        if dlg.exec() and dlg.chosen:
            self._set_act(item, {"type": dlg.chosen})
            self._refresh_key_preview(self.sel)
            self._refresh_slot_captions()
            self._render_timer.start()
            self.select(self.sel)            # full rebuild: new fields + picker label, presets gone

    def _apply_preset(self, preset):
        import copy
        self._store(self.sel)[self.sel] = copy.deepcopy(preset["binding"])
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self.select(self.sel)                # repopulate: now shows the action, presets hidden

    def _change_action_type(self, item, t):
        self._set_act(item, {"type": t})
        self._build_action_fields(item)
        if getattr(self, "_action_picker_btn", None):
            self._sync_action_chip(t)
            self._action_picker_btn.setToolTip(ACTION_DESC.get(t, ""))
        self._refresh_key_preview(self.sel)
        self._refresh_slot_captions()        # keep the knob/button's on-stage label live
        self._render_timer.start()

    def _mk_value_slider(self, a, key, lo, hi, default, unit):
        """A slider + live #sliderval readout that writes a[key] — shared by the bulb / Prisma editors."""
        if a.get(key) is None:
            a[key] = default
        row = QHBoxLayout()
        s = QSlider(Qt.Horizontal); s.setRange(lo, hi); s.setValue(int(a.get(key, default)))
        lab = QLabel(f"{s.value()}{unit}", objectName="sliderval")
        lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter); lab.setMinimumWidth(42)
        s.valueChanged.connect(lambda x, k=key: self._set_action(a, k, int(x)))
        s.valueChanged.connect(lambda x, lb=lab: lb.setText(f"{x}{unit}"))
        row.addWidget(s, 1); row.addWidget(lab)
        return row

    def _build_action_fields(self, item):
        while self.action_fields_layout.count():
            it = self.action_fields_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._delete_layout(it.layout())
        a = self._act(item)
        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        self.action_fields_layout.addLayout(form)
        t = a.get("type", "none")

        def line(key, ph=""):
            e = QLineEdit(str(a.get(key, "")))
            e.setPlaceholderText(ph)
            e.textChanged.connect(lambda v: self._set_action(a, key, v))
            return e

        def combo(key, opts, default):
            c = QComboBox(); c.setFocusPolicy(Qt.StrongFocus); c.addItems(opts)
            c.setCurrentText(a.get(key, default))
            if not a.get(key):
                a[key] = default
            c.currentTextChanged.connect(lambda v: self._set_action(a, key, v))
            return c

        def spin(key, lo, hi, default):
            s = QSpinBox(); s.setRange(lo, hi); s.setValue(int(a.get(key, default)))
            s.valueChanged.connect(lambda v: self._set_action(a, key, v))
            return s

        if t == "open":
            row = QHBoxLayout()
            e = line("target", "notepad.exe or https://…")
            br = QPushButton("…"); br.setFixedWidth(34)
            br.clicked.connect(lambda: self._browse_target(item, a, e))
            row.addWidget(e); row.addWidget(br)
            form.addRow("Target", row)
            form.addRow("Args", line("args", "optional"))
            if self.sel.startswith("key"):              # only LCD keys carry a face
                useicon = QPushButton("Use app icon")
                useicon.setToolTip("Grab the icon from the program above and show it on this key")
                useicon.clicked.connect(lambda: self._apply_app_icon(item))
                uic = fluent_qicon("app", normal=T.TEXT_DIM, active=T.TEXT, size=15, widget=useicon)
                if uic is not None:
                    useicon.setIcon(uic)
                    useicon.setIconSize(QSize(15, 15))
                form.addRow(useicon)
        elif t == "hotkey":
            edit = QLineEdit(a.get("keys", ""))
            edit.setPlaceholderText("e.g. ctrl+shift+t · mouse:middle  —  or click Record")
            edit.textChanged.connect(lambda v: self._set_action(a, "keys", v.strip()))
            form.addRow("Shortcut", edit)

            rec_btn = QPushButton("⏺ Record")
            rec_btn.setToolTip("Record a key combo or a mouse button / scroll")

            def do_record():
                rec_btn.setText("Press a key…  (Esc)")
                rec_btn.setEnabled(False)
                self._hk_rec = _HotkeyRecorder()

                def on_cap(hk):
                    rec_btn.setText("⏺ Record")
                    rec_btn.setEnabled(True)
                    if hk and hk not in ("esc", "escape"):
                        edit.setText(hk)
                self._hk_rec.captured.connect(on_cap)
                self._hk_rec.start()
            rec_btn.clicked.connect(do_record)

            clr = QPushButton("Clear")
            clr.setFixedWidth(60)
            clr.clicked.connect(edit.clear)
            row = QHBoxLayout()
            row.addWidget(rec_btn)
            row.addWidget(clr)
            row.addStretch(1)
            form.addRow(row)
            hint = QLabel("Click Record, then press a key combo OR a mouse button / scroll "
                          "(Ctrl/Alt/Shift/Win modifiers work too). Esc cancels.", objectName="dim")
            hint.setWordWrap(True)
            form.addRow(hint)
        elif t == "text":
            te = QPlainTextEdit(a.get("text", "")); te.setFixedHeight(70)
            te.textChanged.connect(lambda: self._set_action(a, "text", te.toPlainText()))
            form.addRow("Text", te)
        elif t == "media":
            form.addRow("Media", combo("media", ["play_pause", "next", "prev", "stop"], "play_pause"))
        elif t == "volume":
            form.addRow("Volume", combo("volume", ["up", "down", "mute"], "up"))
            form.addRow("Step", spin("step", 1, 10, 1))
        elif t == "appvolume":
            if not a.get("target"):
                a["target"] = "focused"
            form.addRow("Adjust", combo("mode", ["up", "down", "mute"], "up"))
            form.addRow("Step %", spin("step", 1, 25, 5))
            form.addRow("Target app", line("target", "focused  — or an .exe e.g. spotify.exe"))
            h = QLabel("Per-app volume on a dial: bind turn-right = Up, turn-left = Down, "
                       "press = Mute. “focused” follows the active window; or name an .exe. "
                       "A volume bar pops up on the keys.", objectName="dim")
            h.setWordWrap(True)
            form.addRow(h)
        elif t == "mic":
            form.addRow("Microphone", combo("mic", ["toggle", "mute", "unmute"], "toggle"))
        elif t == "sound":
            smode = QComboBox()
            smode.setFocusPolicy(Qt.StrongFocus)
            for label, m in (("Play a clip", "play"), ("Stop all sounds", "stop")):
                smode.addItem(label, m)
            scur = (a.get("mode") or "play").lower()
            a["mode"] = scur
            smode.setCurrentIndex(max(0, smode.findData(scur)))
            form.addRow("Action", smode)

            def _set_smode(_i=None):
                a["mode"] = smode.currentData()
                self._build_action_fields(item)
                self._refresh_key_preview(self.sel)
                self._render_timer.start()
            smode.currentIndexChanged.connect(_set_smode)

            if scur == "stop":
                _sl = QLabel("Cuts every clip that's currently playing — handy as a panic key.",
                             objectName="dim")
                _sl.setWordWrap(True)
                form.addRow(_sl)
            else:
                srow = QHBoxLayout()
                se = line("file", "path to .wav / .ogg / .flac / .mp3")
                sbr = QPushButton("…")
                sbr.setFixedWidth(34)
                sbr.clicked.connect(lambda: self._browse_sound(a, se))
                srow.addWidget(se)
                srow.addWidget(sbr)
                form.addRow("Sound file", srow)
                dev = QComboBox()
                dev.addItem("System default", "")
                try:
                    from .sound import list_outputs
                    for nm in list_outputs():
                        dev.addItem(nm, nm)
                except Exception:
                    pass
                pos = dev.findData(a.get("device", "") or "")
                dev.setCurrentIndex(pos if pos >= 0 else 0)
                dev.currentIndexChanged.connect(
                    lambda _i: self._set_action(a, "device", dev.currentData()))
                form.addRow("Output", dev)
                mon = QCheckBox("Also play on my speakers")
                mon.setToolTip("Monitor: also play through your normal speakers/headphones.")
                mon.setChecked(bool(a.get("monitor", False)))
                mon.toggled.connect(lambda on: self._set_action(a, "monitor", bool(on)))
                form.addRow(mon)
                gsp = QSpinBox()
                gsp.setRange(0, 200)
                gsp.setValue(int(float(a.get("gain", 1.0)) * 100))
                gsp.valueChanged.connect(lambda v: self._set_action(a, "gain", v / 100.0))
                form.addRow("Volume %", gsp)
                sb = QPushButton("Build a soundboard from files…")
                sic = fluent_qicon("volume", normal=T.TEXT_DIM, active=T.TEXT, size=15, widget=sb)
                if sic is not None:
                    sb.setIcon(sic)
                    sb.setIconSize(QSize(15, 15))
                sb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                sb.setMinimumWidth(110)
                sb.clicked.connect(self._build_soundboard)
                form.addRow(sb)
                sh = QLabel("Soundboard into Discord: set Output to the device Discord uses as its "
                            "microphone (a virtual cable); enable monitor to hear it too.",
                            objectName="dim")
                sh.setWordWrap(True)
                form.addRow(sh)
        elif t == "discord":
            mode_combo = QComboBox()
            mode_combo.setFocusPolicy(Qt.StrongFocus)
            _DMODES = [("Self-mute  (live state)", "mute"), ("Deafen  (live state)", "deafen"),
                       ("Mic volume — up (dial)", "invol_up"), ("Mic volume — down (dial)", "invol_down"),
                       ("Output volume — up (dial)", "outvol_up"),
                       ("Output volume — down (dial)", "outvol_down"),
                       ("Push-to-talk / Voice toggle", "mode_toggle"),
                       ("Noise suppression toggle", "noise_toggle"),
                       ("Join voice channel", "join"), ("Disconnect from voice", "disconnect"),
                       ("Keybind (hotkey)", "keybind")]
            for label, m in _DMODES:
                mode_combo.addItem(label, m)
            cur = (a.get("mode") or ("keybind" if a.get("keys") else "mute"))
            a["mode"] = cur
            mi = mode_combo.findData(cur)
            mode_combo.setCurrentIndex(mi if mi >= 0 else 0)
            form.addRow("Action", mode_combo)

            def _set_dmode(_i=None):
                a["mode"] = mode_combo.currentData()
                self._build_action_fields(item)        # rebuild: swap keybind <-> RPC fields
                self._refresh_key_preview(self.sel)
                self._render_timer.start()
            mode_combo.currentIndexChanged.connect(_set_dmode)

            if cur == "keybind":
                if not a.get("keys"):
                    a["keys"] = "f13"
                de = QLineEdit(a.get("keys", "f13"))
                de.textChanged.connect(lambda v: self._set_action(a, "keys", v.strip()))
                drec = QPushButton("⏺ Record")

                def _drec():
                    drec.setText("Press a key…")
                    drec.setEnabled(False)
                    self._hk_rec = _HotkeyRecorder()

                    def _dc(hk):
                        drec.setText("⏺ Record")
                        drec.setEnabled(True)
                        if hk and hk not in ("esc", "escape"):
                            de.setText(hk)
                    self._hk_rec.captured.connect(_dc)
                    self._hk_rec.start()
                drec.clicked.connect(_drec)
                drow = QHBoxLayout()
                drow.addWidget(de)
                drow.addWidget(drec)
                form.addRow("Send key", drow)
                dh = QLabel("In Discord → Settings → Keybinds bind the SAME key to Toggle Mute / "
                            "Deafen (F13–F15 are free). No Discord app needed — but no on-key state.",
                            objectName="dim")
                dh.setWordWrap(True)
                form.addRow(dh)
            else:
                conn = QPushButton("Discord app…")
                conn.clicked.connect(self._discord_connection)
                form.addRow("Setup", conn)
                from . import discord as _dc
                ready = _dc.configured()
                st = QLabel("✓ Discord app set up — press ▶ Test to fire it for real."
                            if ready else "⚠ Set up a free Discord app first (button above).",
                            objectName="dim")
                st.setStyleSheet(f"color: {T.ACCENT if ready else '#f1cf7d'};")
                st.setWordWrap(True)
                form.addRow(st)
                if cur in ("invol_up", "invol_down", "outvol_up", "outvol_down"):
                    sp = QSpinBox()
                    sp.setRange(1, 50)
                    sp.setValue(int(a.get("step", 5)))
                    sp.setSuffix(" %")
                    sp.valueChanged.connect(lambda v: self._set_action(a, "step", int(v)))
                    form.addRow("Step", sp)
                    vhint = QLabel("Put '— up' on a dial's turn-right and '— down' on turn-left "
                                   "for a Discord volume knob. Add the matching live gauge to see it.",
                                   objectName="dim")
                    vhint.setWordWrap(True)
                    form.addRow(vhint)
                elif cur == "join":
                    cap = QLabel(("✓ " + a.get("channel_name", "")) if a.get("channel_id")
                                 else "Not set — join a voice channel in Discord, then click below.",
                                 objectName="dim")
                    cap.setWordWrap(True)
                    form.addRow("Channel", cap)
                    usec = QPushButton("Use the channel I'm in now")

                    def _use_current():
                        try:
                            from . import discord as _d2
                            cid = _d2.current_channel_id()
                            if cid:
                                a["channel_id"] = cid
                                a["channel_name"] = (_d2.get_channel()[2] or "voice channel")
                                cap.setText("✓ " + a["channel_name"])
                            else:
                                cap.setText("Join a voice channel in Discord first, then click this.")
                        except Exception as e:
                            cap.setText(f"✕ {type(e).__name__}: {e}")
                    usec.clicked.connect(_use_current)
                    form.addRow(usec)
                else:
                    tip = QLabel("Add the matching Discord live source (Appearance → Show live data) "
                                 "to show the state right on the key.", objectName="dim")
                    tip.setWordWrap(True)
                    form.addRow(tip)
        elif t == "substance":
            op_combo = QComboBox()
            op_combo.setFocusPolicy(Qt.StrongFocus)
            for label, keys in _SUBSTANCE_OPS:
                op_combo.addItem(label, keys)
            cur = a.get("keys", "")
            idx = next((i for i, (l, k) in enumerate(_SUBSTANCE_OPS) if k and k == cur), -1)
            if idx < 0:
                idx = len(_SUBSTANCE_OPS) - 1 if cur else 0
            op_combo.setCurrentIndex(idx)
            if not a.get("keys") and _SUBSTANCE_OPS[idx][1]:
                a["keys"] = _SUBSTANCE_OPS[idx][1]
            form.addRow("Operation", op_combo)
            key_edit = QLineEdit(a.get("keys", ""))
            key_edit.setPlaceholderText("key, e.g. ]  or  ctrl+z")
            key_edit.textChanged.connect(lambda v: self._set_action(a, "keys", v.strip()))
            form.addRow("Sends key", key_edit)
            op_combo.currentIndexChanged.connect(
                lambda _i: key_edit.setText(op_combo.currentData()) if op_combo.currentData() else None)
            sh = QLabel("Sent by physical key, so it works on any keyboard layout. Tip: put Brush "
                        "size − / + on a knob's turn-left / turn-right for a brush dial. If a shortcut "
                        "still doesn't register (or for brush rotation, which has no default), reassign "
                        "it in Painter → Edit → Settings → Shortcuts (e.g. to F13–F24) and use Custom.",
                        objectName="dim")
            sh.setWordWrap(True)
            form.addRow(sh)
        elif t == "http":
            form.addRow("URL", line("url", "https://api.example.com/webhook"))
            form.addRow("Method", combo("method", ["GET", "POST", "PUT", "PATCH", "DELETE"], "GET"))
            bt = QPlainTextEdit(a.get("body", "")); bt.setFixedHeight(62)
            bt.setPlaceholderText('{"key": "value"}   — sent with POST / PUT / PATCH')
            bt.textChanged.connect(lambda: self._set_action(a, "body", bt.toPlainText()))
            form.addRow("Body", bt)
            form.addRow("Content-Type", line("content_type", "application/json"))
            hint = QLabel("Sends a web request when pressed — great for Home Assistant, Discord "
                          "webhooks, IFTTT or your own API. Body + Content-Type apply to "
                          "POST / PUT / PATCH.", objectName="dim")
            hint.setWordWrap(True)
            form.addRow(hint)
        elif t == "timer":
            form.addRow("Mode", combo("mode", ["countdown", "stopwatch"], "countdown"))
            form.addRow("Minutes", spin("minutes", 0, 180, 5))
            form.addRow("Seconds", spin("seconds", 0, 59, 0))
            bx = QCheckBox("Beep when the countdown hits zero")
            bx.setChecked(a.get("beep", True) is not False)
            bx.toggled.connect(lambda v: self._set_action(a, "beep", bool(v)))
            form.addRow(bx)
            form.addRow("Press does", combo("op", ["startpause", "reset"], "startpause"))
            thint = QLabel("The key shows a live MM:SS with a progress bar. Tap = start / pause / "
                           "resume; tapping a finished (flashing) timer resets it. Tip: add a second "
                           "timer action with 'reset' on this key's HOLD gesture for hold-to-reset.",
                           objectName="dim")
            thint.setWordWrap(True)
            form.addRow(thint)
        elif t == "quick":
            op_combo = QComboBox()
            op_combo.setFocusPolicy(Qt.StrongFocus)
            for label, op in _QUICK_OPS:
                op_combo.addItem(label, op)
            pos = op_combo.findData(a.get("op", ""))
            if pos < 0:
                pos = 0
                a["op"] = _QUICK_OPS[0][1]
            op_combo.setCurrentIndex(pos)
            form.addRow("Action", op_combo)
            qhint = QLabel("", objectName="dim")
            qhint.setWordWrap(True)
            form.addRow(qhint)

            def _set_quick(_i=None):
                op = op_combo.currentData()
                a["op"] = op
                qhint.setText(_QUICK_HINTS.get(op, "Runs this Windows action with one press."))
                self._refresh_key_preview(self.sel)
                self._render_timer.start()
            op_combo.currentIndexChanged.connect(_set_quick)
            qhint.setText(_QUICK_HINTS.get(a.get("op"), "Runs this Windows action with one press."))
        elif t == "system":
            form.addRow("System", combo("system", ["lock", "sleep", "monitor_off", "screensaver"], "lock"))
        elif t == "monitor":
            form.addRow("Brightness", combo("monitor", ["up", "down", "set"], "up"))
            form.addRow("Step", spin("step", 1, 50, 5))
            form.addRow("Level", spin("value", 0, 100, 50))
            disp = QComboBox()
            disp.addItem("All monitors", "all")
            try:
                from .monitors import count as _moncount
                n = _moncount()
            except Exception:
                n = 0
            for mi in range(n):
                disp.addItem(f"Monitor {mi + 1}", mi)
            cur = a.get("index", "all")
            pos = disp.findData(cur if cur is not None else "all")
            disp.setCurrentIndex(pos if pos >= 0 else 0)
            a.setdefault("index", "all")
            disp.currentIndexChanged.connect(lambda _i: self._set_action(a, "index", disp.currentData()))
            form.addRow("Display", disp)
        elif t == "smartlight":
            if not a.get("host"):
                a["host"] = "192.168.0.87"
            modes = [("Toggle", "toggle"), ("On", "on"), ("Off", "off"),
                     ("Rainbow cycle (auto)", "cycle"),
                     ("Brightness — set", "brightness"),
                     ("Brightness — up ▲", "brightness_up"),
                     ("Brightness — down ▼", "brightness_down"),
                     ("Colour — set", "color"),
                     ("Colour — cycle ▲", "hue_up"),
                     ("Colour — cycle ▼", "hue_down")]
            mc = QComboBox(); mc.setFocusPolicy(Qt.StrongFocus)
            for lbl, val in modes:
                mc.addItem(lbl, val)
            if not a.get("mode"):
                a["mode"] = "toggle"
            mi = mc.findData(a.get("mode", "toggle"))
            mc.setCurrentIndex(mi if mi >= 0 else 0)

            def _sl_mode(_i=None):
                self._set_action(a, "mode", mc.currentData())
                QTimer.singleShot(0, self._populate_editor)     # rebuild fields + swatch-size slider
            mc.currentIndexChanged.connect(_sl_mode)
            form.addRow("Action", mc)
            form.addRow("Bulb IP", line("host", "192.168.0.87"))

            mode = a.get("mode")
            if mode == "brightness":
                form.addRow("Brightness", self._mk_value_slider(a, "brightness", 1, 100, 50, "%"))
            elif mode in ("brightness_up", "brightness_down"):
                form.addRow("Step", self._mk_value_slider(a, "step", 1, 50, 10, "%"))
                eh = QLabel("± per press · bind to a dial to dim / brighten.", objectName="dim")
                eh.setWordWrap(True); form.addRow(eh)
            elif mode in ("hue_up", "hue_down"):
                form.addRow("Step", self._mk_value_slider(a, "step", 5, 90, 30, "°"))
                eh = QLabel("Each press rotates the bulb's colour around the wheel by this much. "
                            "Bind it to a dial to scroll through colours.", objectName="dim")
                eh.setWordWrap(True); form.addRow(eh)
            elif mode == "cycle":
                form.addRow("Speed", self._mk_value_slider(a, "step", 2, 30, 8, "°"))
                eh = QLabel("Press once to start an automatic rainbow cycle, press again to stop. "
                            "Higher = faster colour change.", objectName="dim")
                eh.setWordWrap(True); form.addRow(eh)
            if mode == "color":
                if not a.get("color"):
                    a["color"] = "FF8C32"
                crow = QHBoxLayout()
                ce = line("color", "FF8C32")
                pick = QPushButton("Pick…"); pick.setFixedWidth(64)
                pick.clicked.connect(lambda: self._pick_rgb_color(a, ce))
                crow.addWidget(ce); crow.addWidget(pick)
                form.addRow("Colour (hex)", crow)
                form.addRow("Brightness", self._mk_value_slider(a, "brightness", 1, 100, 100, "%"))
            acct = QPushButton("Tapo account…")
            acct.setToolTip("Your TP-Link account — set once; the dock then controls the bulb "
                            "directly over Wi-Fi (Lumos does not need to run)")
            acct.clicked.connect(self._tapo_account)
            form.addRow(acct)
        elif t == "rgbscene":
            mc = QComboBox(); mc.setFocusPolicy(Qt.StrongFocus)
            for lbl, val in _RGB_MODES:
                mc.addItem(lbl, val)
            if not a.get("mode"):
                a["mode"] = "color"
            mi = mc.findData(a.get("mode", "color"))
            mc.setCurrentIndex(mi if mi >= 0 else 0)

            def _rgb_mode(_i=None):
                self._set_action(a, "mode", mc.currentData())
                QTimer.singleShot(0, self._populate_editor)     # rebuild fields + swatch-size slider
            mc.currentIndexChanged.connect(_rgb_mode)
            form.addRow("Scene", mc)
            m = a.get("mode", "color")
            if m == "color":
                if not a.get("color"):
                    a["color"] = "FF0000"
                crow = QHBoxLayout()
                ce = line("color", "FF0000")
                pick = QPushButton("Pick…"); pick.setFixedWidth(64)
                pick.clicked.connect(lambda: self._pick_rgb_color(a, ce))
                crow.addWidget(ce); crow.addWidget(pick)
                form.addRow("Colour (hex)", crow)
            elif m == "effect":
                form.addRow("Effect", combo("effect", _RGB_EFFECTS, "Rainbow"))
            elif m == "profile":
                form.addRow("Profile", line("profile", "exact saved-profile name"))
            elif m == "bright_set":
                form.addRow("Brightness", self._mk_value_slider(a, "brightness", 0, 100, 100, "%"))
            elif m in ("bright_up", "bright_down"):
                form.addRow("Step", self._mk_value_slider(a, "step", 1, 50, 10, "%"))
                eh = QLabel("Each press changes Prisma's brightness by this much. Bind it to a dial: "
                            "turn-right = up, turn-left = down.", objectName="dim")
                eh.setWordWrap(True); form.addRow(eh)
            elif m in ("hue_up", "hue_down"):
                form.addRow("Step", self._mk_value_slider(a, "step", 5, 90, 20, "°"))
                eh = QLabel("Each press rotates the RGB colour around the wheel by this much — "
                            "bind it to a dial to scroll through colours, like the bulb.", objectName="dim")
                eh.setWordWrap(True); form.addRow(eh)
            form.addRow("Program", line("exe", r"C:\Users\Erik\Desktop\project\RGBCommander\dist\RGBCommander.exe"))
            rh = QLabel("Sends a command to Prisma (started if needed). Make one key per scene — "
                        "a colour, effect, saved profile, brightness, or Off — or a dial for brightness.",
                        objectName="dim")
            rh.setWordWrap(True)
            form.addRow(rh)
        elif t == "obs":
            obs_live = {"stream": "obs_streaming", "record": "obs_recording",
                        "virtualcam": "obs_virtualcam", "scene": "obs_scene",
                        "preview": "obs_scene", "replay": "obs_replay"}
            modes = [("Switch scene", "scene"), ("Set preview scene", "preview"),
                     ("Record  (toggle)", "record"), ("Stream  (toggle)", "stream"),
                     ("Virtual cam  (toggle)", "virtualcam"), ("Save replay buffer", "replay"),
                     ("Mute input  (toggle)", "mute"),
                     ("Source volume — up ▲", "vol_up"), ("Source volume — down ▼", "vol_down"),
                     ("Source mute  (toggle)", "vol_mute")]
            mc = QComboBox(); mc.setFocusPolicy(Qt.StrongFocus)
            for lbl, val in modes:
                mc.addItem(lbl, val)
            if not a.get("mode"):
                a["mode"] = "scene"
            mi = mc.findData(a.get("mode", "scene"))
            mc.setCurrentIndex(mi if mi >= 0 else 0)

            def _obs_mode(_i=None):
                self._set_action(a, "mode", mc.currentData())
                # keep a connected live-status source in sync with the chosen OBS action
                if self.sel.startswith("key") and (item.get("live") or {}).get("source", "").startswith("obs_"):
                    ns = obs_live.get(mc.currentData())
                    if ns:
                        item["live"] = {"source": ns}
                    else:
                        item.pop("live", None)
                    self.cfg.save(); self.controller.request_render(); self._refresh_key_preview(self.sel)
                QTimer.singleShot(0, lambda: self._build_action_fields(item))
            mc.currentIndexChanged.connect(_obs_mode)
            form.addRow("Do", mc)
            m = a.get("mode", "scene")
            if m in ("scene", "preview"):
                form.addRow("Scene name", line("target", "exact OBS scene name"))
            elif m == "mute":
                form.addRow("Input name", line("target", "exact source name, e.g. Mic/Aux"))
            elif m in ("vol_up", "vol_down", "vol_mute"):
                arow = QHBoxLayout()
                src_edit = line("input", "exact source name, e.g. Mic/Aux")
                arow.addWidget(src_edit, 1)
                pick = QPushButton("Pick…"); pick.setFixedWidth(58)
                pick.setToolTip("Fetch the audio sources from OBS")
                pick.clicked.connect(lambda _=False, e=src_edit: self._pick_obs_input(a, e))
                arow.addWidget(pick)
                form.addRow("Audio source", arow)
                if m in ("vol_up", "vol_down"):
                    form.addRow("Step", spin("step", 1, 50, 5))
                _ah = QLabel("Best on a knob — try the dial's “🎚 OBS audio” quick set-up. "
                             "Turning shows a volume HUD on the keys.", objectName="dim")
                _ah.setWordWrap(True)
                form.addRow(_ah)
            # Connect the key to OBS's live status (stateful key: glyph reflects OBS, press still fires).
            if self.sel.startswith("key") and obs_live.get(m):
                src = obs_live[m]
                chk = QCheckBox("Show live OBS status on this key")
                chk.setToolTip("The key reflects OBS's live state (updates automatically) and still "
                               "fires this action when pressed.")
                chk.setChecked((item.get("live") or {}).get("source") == src)

                def _link_obs_live(on, src=src):
                    if on:
                        self._set_live(item, src)
                    elif (item.get("live") or {}).get("source", "").startswith("obs_"):
                        item.pop("live", None)
                        self.cfg.save(); self.controller.request_render()
                    self._refresh_key_preview(self.sel)
                chk.toggled.connect(_link_obs_live)
                form.addRow(chk)
            conn = QPushButton("OBS connection…")
            conn.setToolTip("Host / port / password for OBS's WebSocket server")
            conn.clicked.connect(self._obs_connection)
            form.addRow(conn)
            oh = QLabel("Controls OBS over its WebSocket (enable in OBS: Tools → WebSocket Server "
                        "Settings). Tick the box above to mirror OBS's live status on the key.",
                        objectName="dim")
            oh.setWordWrap(True)
            form.addRow(oh)
        elif t == "page":
            form.addRow("Page", combo("page", ["next", "prev", "goto"], "next"))
            form.addRow("Index", spin("target", 0, 50, 0))
        elif t == "folder":
            if not a.get("folder"):
                a["folder"] = self._new_folder_id()
            fid = a["folder"]
            folder = self.cfg.folder(fid)
            name_e = QLineEdit(folder.get("name", "Folder"))
            name_e.textChanged.connect(lambda v: self._set_folder_name(fid, v))
            form.addRow("Folder name", name_e)
            editb = QPushButton("Edit folder contents  →")
            editb.setObjectName("primary")
            editb.clicked.connect(lambda: self._enter_folder_edit(fid))
            form.addRow(editb)
            # Resize the folder tile (tray + mini-grid) on this key.
            fsr = QHBoxLayout(); fsr.setSpacing(6)
            fss = QSlider(Qt.Horizontal); fss.setRange(40, 160)
            fss.setValue(int(round(float(item.get("icon_scale", 1.0)) * 100)))
            fsl = QLabel(f"{fss.value()}%", objectName="sliderval")
            fsl.setAlignment(Qt.AlignRight | Qt.AlignVCenter); fsl.setMinimumWidth(42)

            def _set_folder_size(x, it=item, lb=fsl):
                it["icon_scale"] = x / 100.0
                lb.setText(f"{x}%")
                self._refresh_key_preview(self.sel)
                self._render_timer.start()
            fss.valueChanged.connect(_set_folder_size)
            fsr.addWidget(fss, 1); fsr.addWidget(fsl)
            form.addRow("Icon size", fsr)
            fh = QLabel("Opens a sub-page of keys on the dock. The last key is an automatic "
                        "Back; buttons & knobs stay the same inside.", objectName="dim")
            fh.setWordWrap(True)
            form.addRow(fh)
        elif t == "profile":
            c = QComboBox(); c.addItems(self.cfg.profile_names())
            c.setCurrentText(a.get("name", self.cfg.profile_names()[0] if self.cfg.profile_names() else ""))
            c.currentTextChanged.connect(lambda v: self._set_action(a, "name", v))
            if not a.get("name") and self.cfg.profile_names():
                a["name"] = self.cfg.profile_names()[0]
            form.addRow("Switch to", c)
        elif t == "brightness":
            form.addRow("Mode", combo("mode", ["set", "up", "down"], "set"))
            form.addRow("Step", spin("step", 1, 50, 5))
            form.addRow("Level", spin("value", 0, 100, 70))
            hint = QLabel("This is the dock's own screen brightness. "
                          "For your PC displays use the 'monitor' action.", objectName="dim")
            hint.setWordWrap(True)
            form.addRow(hint)
        elif t == "macro":
            import json
            te = QPlainTextEdit(json.dumps(a.get("steps", []), indent=1)); te.setFixedHeight(110)

            def upd():
                import json as _j
                try:
                    a["steps"] = _j.loads(te.toPlainText())
                except ValueError:
                    pass
            te.textChanged.connect(upd)
            form.addRow("Steps", te)
        else:
            hint = QLabel("No action yet — click the chip on the left, or drag one in "
                          "from the list on the right.", objectName="dim")
            hint.setWordWrap(True)
            form.addRow(hint)

        self._align_labels(form)

    def _test_current(self):
        """Header ▶ Test button — fire the currently-edited control's action once."""
        if self._is_back_key(self.sel):
            return                                    # the fixed Back key has no action to test
        a = self._act(self._store_item(self.sel))
        if (a.get("type") or "none") != "none":
            self._test_action(a)

    # Action types that can block (sleep/DDC/file IO) yet are safe to run off the GUI thread —
    # they don't touch the engine's thread-bound mic COM endpoint or controller navigation state.
    _TESTABLE_OFFLOAD = {"macro", "monitor", "open", "launch", "run"}

    def _test_action(self, a):
        """Fire a key's action from the editor's ▶ Test button without freezing the configurator."""
        t = (a.get("type") or "").lower()
        offload = t in self._TESTABLE_OFFLOAD
        if t == "macro":   # a macro with a mic step must stay inline (mic COM is thread-bound)
            if any((s.get("type") or "").lower() == "mic" for s in a.get("steps", [])):
                offload = False
        if offload:
            threading.Thread(target=lambda: self.controller.engine.execute(a),
                             name="test-action", daemon=True).start()
        else:
            self.controller.engine.execute(a)

    def _browse_target(self, item, a, edit):
        path, _ = QFileDialog.getOpenFileName(self, "Choose program or file")
        if not path:
            return
        edit.setText(path)                              # fires _set_action -> a["target"]
        # Auto-show the app's icon, unless this key already has a custom (non-auto) icon.
        if self.sel.startswith("key") and (not item.get("icon") or item.get("icon_auto")):
            self._apply_app_icon(item, silent=True)

    def _apply_app_icon(self, item, silent=False):
        a = item.get("action") or {}
        QApplication.setOverrideCursor(Qt.WaitCursor)        # shell/COM icon extraction can stall briefly
        try:
            dest = appicon.save_icon(a.get("target", ""), os.path.join(config_dir(), "icons"))
        finally:
            QApplication.restoreOverrideCursor()
        if not dest:
            if not silent:
                QMessageBox.information(self, "App icon",
                                        "Couldn't read an icon from that program or file.")
            return
        item["icon"] = dest
        item["icon_auto"] = True                        # remember it was auto-grabbed
        item["fit"] = "contain"                         # show the whole icon, letterboxed
        item.pop("live", None)
        for sk in self._ICON_STYLE_KEYS:                # fresh style for the new icon
            item.pop(sk, None)
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self._populate_editor()                         # refresh the appearance preview/fields

    def _pick_rgb_color(self, a, edit):
        cur = (a.get("color", "FF0000") or "FF0000").lstrip("#")
        init = QColor("#" + cur)
        col = QColorDialog.getColor(init if init.isValid() else QColor("#FF0000"),
                                    self, "Pick RGB colour")
        if col.isValid():
            edit.setText(f"{col.red():02X}{col.green():02X}{col.blue():02X}")   # fires _set_action

    def _discord_connection(self):
        """Set up the Discord app (client id + secret) and run the one-time OAuth authorize."""
        d = self.cfg.data.setdefault("discord", {})
        dlg = QDialog(self)
        dlg.setWindowTitle("Discord app")
        dlg.setMinimumWidth(440)
        form = QFormLayout(dlg)
        helpl = QLabel(
            "One-time setup (about 3 minutes):<br>"
            "1. Open <a href='https://discord.com/developers/applications'>discord.com/developers</a> "
            "→ New Application.<br>"
            "2. OAuth2 → Redirects → add <code>http://localhost</code> → Save.<br>"
            "3. Copy the <b>Client ID</b> (General) and <b>Client Secret</b> (OAuth2) below, then "
            "click Authorize and approve the popup inside Discord.", objectName="dim")
        helpl.setWordWrap(True)
        helpl.setOpenExternalLinks(True)
        form.addRow(helpl)
        cid = QLineEdit(d.get("client_id", ""))
        cid.setPlaceholderText("Client ID")
        sec = QLineEdit(d.get("client_secret", ""))
        sec.setEchoMode(QLineEdit.Password)
        sec.setPlaceholderText("Client Secret  (leave blank to keep current)")
        form.addRow("Client ID", cid)
        form.addRow("Client Secret", sec)
        status = QLabel("", objectName="dim")
        status.setWordWrap(True)
        bridge = self._discord_auth_bridge = _ResultBridge()
        authb = QPushButton("Authorize")

        def _apply(msg, color):
            try:
                status.setText(msg)
                status.setStyleSheet(f"color: {color};")
                authb.setEnabled(True)
            except RuntimeError:
                pass
        bridge.done.connect(_apply)

        def _auth():
            cidv = cid.text().strip()
            secv = sec.text().strip() or d.get("client_secret", "")
            if not cidv or not secv:
                status.setText("Enter the Client ID and Client Secret first.")
                status.setStyleSheet(f"color: {T.DANGER};")
                return
            d["client_id"], d["client_secret"] = cidv, secv     # persist on the GUI thread
            try:
                self.cfg.save()
            except Exception:
                pass
            from . import discord as _dc
            from .config import load_discord_token, save_discord_token
            _dc.configure(cidv, secv, *load_discord_token(), on_token=save_discord_token)
            authb.setEnabled(False)
            status.setText("Approve the popup inside Discord…")
            status.setStyleSheet(f"color: {T.TEXT_DIM};")

            def work():
                try:
                    _dc.authorize()
                    bridge.done.emit("✓ Authorized — Discord voice control is ready.", T.ACCENT)
                except Exception as e:
                    bridge.done.emit(f"✕ {type(e).__name__}: {e}", T.DANGER)
            threading.Thread(target=work, name="discord-auth", daemon=True).start()
        authb.clicked.connect(_auth)
        brow = QHBoxLayout()
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        brow.addWidget(authb)
        brow.addStretch(1)
        brow.addWidget(close)
        form.addRow(status)
        form.addRow(brow)
        dlg.exec()

    def _tapo_account(self):
        """Enter / import the TP-Link account used for direct Tapo bulb control."""
        t = self.cfg.data.setdefault("tapo", {})
        dlg = QDialog(self); dlg.setWindowTitle("Tapo account"); dlg.setMinimumWidth(340)
        form = QFormLayout(dlg)
        email = QLineEdit(t.get("email", "")); email.setPlaceholderText("you@email.com")
        pw = QLineEdit(); pw.setEchoMode(QLineEdit.Password)
        pw.setPlaceholderText("•••••  (leave blank to keep current)")
        form.addRow("Email", email)
        form.addRow("Password", pw)
        hint = QLabel("Your TP-Link (Tapo app) account — used to control the bulb locally over "
                      "Wi-Fi. Stored only on this PC.", objectName="dim")
        hint.setWordWrap(True)
        form.addRow(hint)

        def _imp():
            from .tapo import import_lumos_creds
            c = import_lumos_creds()
            if c:
                email.setText(c[0]); pw.setText(c[1])
            else:
                self.toast("No saved Lumos credentials found", "warn")
        imp = QPushButton("Import from Lumos"); imp.clicked.connect(_imp)
        brow = QHBoxLayout()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(dlg.reject)
        ok = QPushButton("Save"); ok.setObjectName("primary"); ok.setDefault(True)
        ok.clicked.connect(dlg.accept)
        brow.addWidget(imp); brow.addStretch(1); brow.addWidget(cancel); brow.addWidget(ok)
        form.addRow(brow)
        if dlg.exec():
            t["email"] = email.text().strip()
            if pw.text():
                t["password"] = pw.text()
            self.cfg.save()

    def _obs_connection(self):
        """Host / port / password for OBS Studio's WebSocket server (with a Test button)."""
        o = self.cfg.data.setdefault("obs", {})
        dlg = QDialog(self); dlg.setWindowTitle("OBS connection"); dlg.setMinimumWidth(340)
        form = QFormLayout(dlg)
        host = QLineEdit(o.get("host", "localhost"))
        port = QLineEdit(str(o.get("port", 4455)))
        pw = QLineEdit(); pw.setEchoMode(QLineEdit.Password)
        pw.setPlaceholderText("•••••  (leave blank to keep / if none set)")
        form.addRow("Host", host)
        form.addRow("Port", port)
        form.addRow("Password", pw)
        hint = QLabel("OBS → Tools → WebSocket Server Settings → Enable Server. Use the same port "
                      "(default 4455) and password shown there.", objectName="dim")
        hint.setWordWrap(True)
        form.addRow(hint)
        status = QLabel("", objectName="dim")

        # The probe does blocking socket I/O — run it on a daemon thread and marshal the result
        # back via a queued signal so a bad host/port can't freeze the configurator.
        bridge = self._obs_test_bridge = _ResultBridge()

        def _apply(msg, color):
            try:
                status.setText(msg)
                status.setStyleSheet(f"color: {color};")
                testb.setEnabled(True)
            except RuntimeError:
                pass                      # dialog already closed — nothing to update
        bridge.done.connect(_apply)

        def _test():
            testb.setEnabled(False)
            status.setText("Testing…")
            status.setStyleSheet(f"color: {T.TEXT_DIM};")
            host_s, port_s = host.text().strip(), port.text().strip()
            pw_s = pw.text() if pw.text() else o.get("password", "")

            def work():
                from . import obs as _obs
                try:
                    _obs.configure(host_s, port_s, pw_s)
                    r = _obs.request("GetVersion")
                    v = (r or {}).get("responseData", {}).get("obsVersion") if r else None
                    bridge.done.emit("✓ Connected — OBS " + str(v) if v else "✓ Connected", T.ACCENT)
                except Exception as e:
                    bridge.done.emit(f"✕ {type(e).__name__}: {e}", T.DANGER)
            threading.Thread(target=work, name="obs-test", daemon=True).start()
        testb = QPushButton("Test"); testb.clicked.connect(_test)
        brow = QHBoxLayout()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(dlg.reject)
        ok = QPushButton("Save"); ok.setObjectName("primary"); ok.setDefault(True)
        ok.clicked.connect(dlg.accept)
        brow.addWidget(testb); brow.addStretch(1); brow.addWidget(cancel); brow.addWidget(ok)
        form.addRow(status)
        form.addRow(brow)
        if dlg.exec():
            o["host"] = host.text().strip() or "localhost"
            try:
                o["port"] = int(port.text().strip() or 4455)
            except ValueError:
                o["port"] = 4455
            if pw.text():
                o["password"] = pw.text()
            self.cfg.save()
            try:                              # let the live-status sampler reconnect with new settings
                from . import obs as _obs
                _obs.configure(o.get("host"), o.get("port"), o.get("password"))
            except Exception:
                pass

    def _set_action(self, a, key, value):
        a[key] = value
        # The rendered face can now depend on action fields (rgbscene/smartlight colour + effect ->
        # a swatch), so refresh the big inspector preview too — like _set_face/_set_flag do.
        item = getattr(self, "_insp_item", None)
        if item is not None and getattr(self, "face_preview", None) is not None:
            try:
                self.face_preview.setPixmap(
                    face_pixmap(self._face(item), self._PREVIEW, self.face_preview))
            except RuntimeError:
                pass
        self._refresh_key_preview(self.sel)
        self._render_timer.start()

    # ---- persistence / device ---------------------------------------------
    def _persist_and_render(self):
        try:
            self.cfg.save()
        except OSError:
            pass
        self.controller.request_render()

    # ---- page / profile / brightness --------------------------------------
    def _goto_page(self, idx):
        if idx == self.cur_page:
            return
        self.cur_page = idx
        self.controller.goto_page(idx)          # the device plays the swipe
        self._refresh_all_slots()
        self._flash_canvas()                    # …and the GUI answers with a soft 150 ms bloom
        self.select(self.sel)                   # keep the inspector in sync with the new page

    def _add_page(self):
        self.pages().append({"name": f"Page {len(self.pages())+1}", "items": {}})
        self.cur_page = len(self.pages()) - 1
        self.cfg.save()
        self._refresh_tabs(); self._refresh_all_slots()
        self.controller.goto_page(self.cur_page)

    def _del_page(self, idx=None):
        if len(self.pages()) < 2:
            return
        idx = self.cur_page if idx is None else idx
        if QMessageBox.question(self, "Delete page", f"Delete '{self.pages()[idx].get('name')}'?") \
                == QMessageBox.Yes:
            self.pages().pop(idx)
            self.cur_page = max(0, min(self.cur_page, len(self.pages()) - 1))
            self.cfg.save()
            self._refresh_tabs(); self._refresh_all_slots()
            self.controller.goto_page(self.cur_page)

    def _rename_page(self, idx):
        cur = self.pages()[idx].get("name", f"Page {idx + 1}")
        name, ok = QInputDialog.getText(self, "Rename page", "Page name:", text=cur)
        if ok and name.strip():
            self.pages()[idx]["name"] = name.strip()
            self.cfg.save()
            self._refresh_tabs()

    def _move_page(self, idx, delta):
        ps = self.pages()
        j = idx + delta
        if not (0 <= j < len(ps)):
            return
        ps[idx], ps[j] = ps[j], ps[idx]
        if self.cur_page == idx:
            self.cur_page = j
        elif self.cur_page == j:
            self.cur_page = idx
        self.cfg.save()
        self._refresh_tabs(); self._refresh_all_slots()

    def _page_tab_menu(self, idx, gpos):
        menu = QMenu(self)
        ren = menu.addAction(menu_icon("rename", self) or QIcon(), "Rename…")
        menu.addSeparator()
        ml = menu.addAction(menu_icon("chevron_left", self) or QIcon(), "Move left")
        ml.setEnabled(idx > 0)
        mr = menu.addAction(menu_icon("chevron_right", self) or QIcon(), "Move right")
        mr.setEnabled(idx < len(self.pages()) - 1)
        menu.addSeparator()
        dele = menu.addAction(menu_icon("delete", self) or QIcon(), "Delete page")
        dele.setEnabled(len(self.pages()) > 1)
        chosen = menu.exec(gpos)
        if chosen is ren:
            self._rename_page(idx)
        elif chosen is ml:
            self._move_page(idx, -1)
        elif chosen is mr:
            self._move_page(idx, 1)
        elif chosen is dele:
            self._del_page(idx)

    def _refresh_profile_label(self):
        if getattr(self, "profile_lbl", None) is not None:
            self.profile_lbl.setText(f"PROFILE · {self.cfg.data.get('active_profile', 'Default')}")

    def _add_profile(self):
        name, ok = QInputDialog.getText(self, "New profile", "Profile name:")
        if not ok or not name:
            return
        if name in self.cfg.profile_names():
            QMessageBox.warning(self, APP_TITLE, "A profile with that name exists.")
            return
        self.cfg.profiles.append({"name": name, "pages": [{"name": "Home", "items": {}}]})
        self.cfg.data["active_profile"] = name
        self.cur_page = 0
        self.cfg.save()
        self.controller.set_profile(name)
        self.refresh(); self.select("key1")

    def _on_profile_changed(self, name):
        if name and name != self.cfg.data.get("active_profile"):
            self.cur_page = 0
            self.controller.set_profile(name)
            self.refresh(); self.select("key1")

    def _on_brightness(self, v):
        self.bright_val.setText(f"{v}%")
        self.controller.set_brightness_live(v)   # instant device feedback, no disk write per tick
        self._bright_save_timer.start()          # persist once the drag settles (or on release)

    def _save_brightness(self):
        self._bright_save_timer.stop()
        try:
            self.controller.set_brightness(self.bright.value())   # writes config once
        except Exception:
            pass

    def _toggle_titles(self, on):
        self.cfg.data["show_labels"] = bool(on)
        self.cfg.save()
        self.controller.request_render()
        self._refresh_all_slots()
        self._populate_editor()

    def _toggle_auto_icons(self, on):
        self.cfg.data["auto_icons"] = bool(on)
        images.AUTO_ICONS = bool(on)                  # apply to the configurator previews immediately
        self.cfg.save()
        self.controller.request_render()              # controller re-applies from config too
        self._refresh_all_slots()

    def _toggle_pressfx(self, on):
        self.cfg.data["press_fx"] = bool(on)
        self.cfg.save()
        self.anim_combo.setEnabled(bool(on))

    def _toggle_encaccel(self, on):
        self.cfg.data["encoder_accel"] = bool(on)
        self.cfg.save()

    def _set_press_anim(self, name):
        if name:
            self.cfg.data["press_anim"] = name
            self.cfg.save()

    def _set_folder_anim(self, name):
        if name:
            self.cfg.data["folder_anim"] = name
            self.cfg.save()

    def _set_live_style(self, name):
        if name:
            self.cfg.data["live_style"] = name
            self.cfg.save()
            self.controller.request_render()
            self._refresh_all_slots()

    # ---- app auto-switch ---------------------------------------------------
    def _toggle_auto_switch(self, on):
        self.cfg.data["auto_switch"] = bool(on)
        self.cfg.save()

    def _refresh_rules_list(self):
        self.rules_list.clear()
        for r in self.cfg.data.get("app_rules", []):
            tgt = []
            if r.get("profile"):
                tgt.append(str(r["profile"]))
            if r.get("page") is not None:
                tgt.append(f"page {int(r['page']) + 1}")
            self.rules_list.addItem(f"{r.get('app', '?')}  →  {' · '.join(tgt) or '—'}")

    def _add_app_rule(self):
        dlg = AppRuleDialog(self.cfg, self)
        if dlg.exec():
            rule = dlg.result_rule()
            if rule:
                rules = self.cfg.data.setdefault("app_rules", [])
                rules[:] = [r for r in rules
                            if (r.get("app", "").lower() != rule["app"])]   # one rule per app
                rules.append(rule)
                self.cfg.save()
                self._refresh_rules_list()

    def _edit_app_rule(self):
        row = self.rules_list.currentRow()
        rules = self.cfg.data.get("app_rules", [])
        if not (0 <= row < len(rules)):
            return
        dlg = AppRuleDialog(self.cfg, self, rule=rules[row])
        if dlg.exec():
            new = dlg.result_rule()
            if new:
                rules[row] = new
                self.cfg.save()
                self._refresh_rules_list()

    def _remove_app_rule(self):
        row = self.rules_list.currentRow()
        rules = self.cfg.data.get("app_rules", [])
        if 0 <= row < len(rules):
            del rules[row]
            self.cfg.save()
            self._refresh_rules_list()

    # ---- calibration / backups / export / import ---------------------------
    def _open_calibration(self):
        CalibrationDialog(self).exec()

    def _open_backups(self):
        BackupsDialog(self).exec()

    def _after_external_config_change(self):
        """A restore/import rewrote the config file on disk — reload & re-render."""
        self.controller.replace_config(Config.load())
        self.cur_page = 0
        self.refresh()
        self.select("key1")

    def _export_config(self) -> bool:
        default = os.path.join(os.path.expanduser("~"), "Hexpad-profile.json")
        path, _ = QFileDialog.getSaveFileName(self, "Export configuration",
                                              default, "JSON (*.json)")
        if not path:
            return False
        if backups.export_to(path):
            self.toast("Configuration exported", "ok")
            return True
        self.toast("Export failed — couldn’t write that file", "err")
        return False

    def _import_config(self) -> bool:
        path, _ = QFileDialog.getOpenFileName(self, "Import configuration", "",
                                              "JSON (*.json)")
        if not path:
            return False
        if QMessageBox.question(
                self, "Import",
                "Replace your current setup with this file?\nYour current setup is backed up first."
        ) != QMessageBox.Yes:
            return False
        if backups.import_from(path):
            self._after_external_config_change()
            QMessageBox.information(self, "Imported", "Configuration imported.")
            return True
        QMessageBox.warning(self, "Import failed", "That file isn't a valid Hexpad config.")
        return False

    # ---- external status (device thread -> GUI) ----------------------------
    def on_status(self):
        if self.controller.page_index != self.cur_page:
            self.cur_page = self.controller.page_index
            self._refresh_tabs()
            self._refresh_all_slots()
            self.select(self.sel)               # keep the docked inspector on the live page
        self._refresh_conn()

    def show_raise(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
        else:
            event.ignore()
            self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        self._mirror_timer.start()           # live tiles tick only while the window can be seen
        if not getattr(self, "_tb_themed", False):
            self._tb_themed = True
            _theme_titlebar(self)            # green the native Windows 11 title bar (once realised)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._mirror_timer.stop()            # hidden to tray -> zero editor refresh work


def build_tray(get_win, controller, do_quit, refresh_open_win, parent) -> QSystemTrayIcon:
    tray = QSystemTrayIcon(app_icon(), parent)
    tray.setToolTip(APP_TITLE)
    menu = QMenu()

    open_act = menu.addAction(menu_icon("app", menu) or QIcon(), "Open Configurator")
    open_act.triggered.connect(lambda: get_win().show_raise())
    menu.addSeparator()

    prof_menu = menu.addMenu(menu_icon("contact", menu) or QIcon(), "Profile")

    def rebuild_profiles():
        prof_menu.clear()
        active = controller.config.data.get("active_profile")
        for nm in controller.config.profile_names():
            a = prof_menu.addAction(nm)
            a.setCheckable(True)
            a.setChecked(nm == active)
            a.triggered.connect(lambda _=False, n=nm: (controller.set_profile(n), refresh_open_win()))
    prof_menu.aboutToShow.connect(rebuild_profiles)

    menu.addAction(menu_icon("save", menu) or QIcon(), "Backups…") \
        .triggered.connect(lambda: (get_win().show_raise(), get_win()._open_backups()))
    menu.addAction(menu_icon("screen", menu) or QIcon(), "Toggle dock screen") \
        .triggered.connect(controller.toggle_display)
    menu.addAction(menu_icon("refresh", menu) or QIcon(), "Reload config") \
        .triggered.connect(controller.request_reload)
    auto_act = menu.addAction(menu_icon("power", menu) or QIcon(), "Start with Windows")
    auto_act.setCheckable(True)
    auto_act.setChecked(autostart.is_enabled())
    auto_act.triggered.connect(lambda checked: (autostart.enable() if checked else autostart.disable()))
    menu.addSeparator()
    menu.addAction(menu_icon("cancel", menu) or QIcon(), "Quit").triggered.connect(do_quit)

    tray.setContextMenu(menu)
    tray._menu = menu                                # keep the menu alive with the tray
    tray.activated.connect(lambda reason: get_win().show_raise()
                           if reason == QSystemTrayIcon.Trigger or reason == QSystemTrayIcon.DoubleClick
                           else None)
    tray.show()
    return tray


def _start_com_safe_gc() -> None:
    """Confine cyclic GC — and thus every comtypes COM Release — to ONE thread.

    A comtypes IUnknown.Release() from a thread other than the one that CREATED the object is a
    native access violation, in STA or MTA alike (verified: crash.log dumps 'Garbage-collecting ...
    comtypes Release' while adjusting volume fast; an MTA stress repro still AV'd). So ALL transient
    audio COM is funnelled onto the actions '_ComAudio' thread, which ALSO runs the process's
    gc.collect() — every pycaw object is created AND collected on that single thread. gc.disable()
    stops threshold GC on every other thread (incl. Qt's) so none of them ever releases a COM object;
    refcounting still frees acyclic objects instantly. (The mic endpoint is cached / never garbage,
    so it can stay on its own sampler thread.)"""
    import gc
    gc.disable()
    try:
        from .actions import _ComAudio
        _ComAudio.get()                        # start the single COM-owner + GC thread now
    except Exception:
        pass


def main():
    start_hidden = ("--tray" in sys.argv) or ("--hidden" in sys.argv)
    _start_com_safe_gc()                       # must run before any comtypes/pycaw object exists
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setWindowIcon(app_icon())
    app.setFont(QFont("Segoe UI", 9))
    app.setQuitOnLastWindowClosed(False)
    try:
        T.set_accent(Config.load().data.get("accent", "mint"))    # apply the saved accent theme
    except Exception:
        pass
    app.setStyleSheet(T.build_qss())

    if "--status" in sys.argv:
        # Query a running instance over the IPC socket (used for verification).
        sock = QLocalSocket()
        sock.connectToServer(IPC_NAME)
        if sock.waitForConnected(500):
            sock.write(b"status")
            sock.flush()
            sock.waitForBytesWritten(500)
            if sock.waitForReadyRead(900):
                print(bytes(sock.readAll()).decode())
            sock.disconnectFromServer()
        else:
            print('{"running": false}')
        return 0

    if "--probe-tapo" in sys.argv:
        # Diagnostic: confirm python-kasa is bundled + the bulb is reachable (read-only).
        import tempfile, os as _os
        out = _os.path.join(tempfile.gettempdir(), "ajazzdock_tapo.txt")
        try:
            from . import tapo as _tapo
            c = _tapo.import_lumos_creds()
            if not c:
                res = "no creds"
            else:
                res = f"is_on={_tapo.is_on('192.168.0.87', c[0], c[1])}"
        except Exception as _e:
            res = f"ERROR {type(_e).__name__}: {_e}"
        try:
            with open(out, "w", encoding="utf-8") as _f:
                _f.write(res)
        except Exception:
            pass
        return 0

    if "--probe-media" in sys.argv:
        # Diagnostic: confirm WinRT (now-playing) is bundled + readable in this build.
        from . import live as _live
        import time as _t, tempfile, os as _os
        _live.value("media")                      # start the SMTC sampler
        _t.sleep(4.0)
        try:
            with open(_os.path.join(tempfile.gettempdir(), "ajazzdock_media.txt"), "w",
                      encoding="utf-8") as _f:
                _f.write(repr(_live.value("media")))
        except Exception:
            pass
        return 0

    if "--probe-gpu" in sys.argv or "--probe-temp" in sys.argv:
        # Diagnostic: confirm GPU load + CPU/GPU temperature sampling works in this build.
        from . import live as _live
        import time as _t, tempfile, os as _os
        _live.prime()
        _t.sleep(5.0)
        try:
            out = {k: _live.value(k) for k in ("gpu", "cpu_temp", "gpu_temp")}
            with open(_os.path.join(tempfile.gettempdir(), "ajazzdock_gpu.txt"), "w", encoding="utf-8") as _f:
                _f.write(repr(out))
        except Exception:
            pass
        return 0

    if "--screenshot" in sys.argv:
        # Build the UI without touching the device; grab a frame and exit (verification).
        controller = DockController(Config.load())
        win = ConfigWindow(controller)
        win.show()
        path = sys.argv[sys.argv.index("--screenshot") + 1]
        QTimer.singleShot(700, lambda: (win.grab().save(path), app.quit()))
        return app.exec()

    # ---- single instance: OS mutex is authoritative, IPC just forwards show/status ----
    if not _acquire_singleton():
        # Another instance already owns the device — tell it to surface its window, then exit.
        probe = QLocalSocket()
        probe.connectToServer(IPC_NAME)
        if probe.waitForConnected(800):
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(300)
            probe.disconnectFromServer()
        return 0
    # We hold the mutex -> we are the sole primary, so clearing a stale socket name is safe.
    QLocalServer.removeServer(IPC_NAME)
    server = QLocalServer()
    server.listen(IPC_NAME)

    controller = DockController(Config.load())

    bridge = Bridge()
    controller.on_status = lambda: bridge.status.emit()

    # Lazy configurator: on the autostart/tray path (start_hidden) the window is never shown, so
    # defer building its ~500 widgets until the user first opens it — the tray appears sooner and
    # ~20 MB isn't spent mirroring an off-screen UI on every 24/7 login.
    _win = {"w": None}
    _quitting = {"v": False}

    def get_win():
        w = _win["w"]
        if w is None:
            w = ConfigWindow(controller)
            w._tray = tray
            w._server = server
            if _quitting["v"]:
                w._quitting = True
            bridge.status.connect(w.on_status, Qt.QueuedConnection)
            _win["w"] = w
            try:
                w.on_status()                        # sync the connection indicator immediately
            except Exception:
                pass
        return w

    def refresh_open_win():
        w = _win["w"]
        if w is not None:
            w.refresh()
            w.select("key1")

    def do_quit():
        _quitting["v"] = True
        w = _win["w"]
        if w is not None:
            w._quitting = True
        try:
            controller.stop()
        finally:
            app.quit()

    tray = build_tray(get_win, controller, do_quit, refresh_open_win, app)

    def _on_ipc():
        sock = server.nextPendingConnection()
        if sock is None:
            return
        sock.waitForReadyRead(300)
        msg = bytes(sock.readAll()).strip()
        if msg == b"status":
            import json
            sock.write(json.dumps(controller.status()).encode())
            sock.flush()
            sock.waitForBytesWritten(300)
        else:
            get_win().show_raise()
        sock.disconnectFromServer()
    server.newConnection.connect(_on_ipc)

    try:
        autostart.migrate()           # carry a legacy Run-key autostart over to the elevated task
    except Exception:
        pass
    controller.start()
    if not start_hidden:
        get_win().show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
