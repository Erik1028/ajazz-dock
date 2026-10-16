"""Render the configurator while editing a folder sub-page (for visual QA).

Mirrors gui.main()'s --screenshot path (Segoe UI font + QSS, normal platform).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication                # noqa: E402
from PySide6.QtGui import QFont                           # noqa: E402
from PySide6.QtCore import QTimer                         # noqa: E402
from dock.config import Config, default_config            # noqa: E402
from dock.controller import DockController                # noqa: E402
from dock.gui import ConfigWindow, QSS, APP_TITLE         # noqa: E402

app = QApplication([])
app.setApplicationName(APP_TITLE)
app.setFont(QFont("Segoe UI", 9))
app.setStyleSheet(QSS)

data = default_config()
prof = data["profiles"][0]
prof["pages"][0]["items"]["key3"] = {"label": "Apps", "icon": "\U0001F4C1",
                                     "color": "#c8881f", "fit": "cover",
                                     "action": {"type": "folder", "folder": "folder1"}}
prof["folders"] = {"folder1": {"name": "Apps", "items": {
    "key1": {"label": "Firefox", "icon": "\U0001F98A", "action": {"type": "open", "target": "firefox.exe"}},
    "key2": {"label": "Steam", "icon": "\U0001F3AE", "action": {"type": "open", "target": "steam.exe"}},
    "key3": {"label": "iCloud", "icon": "☁️", "action": {"type": "open", "target": "icloud.exe"}},
    "key4": {"label": "Discord", "icon": "\U0001F4AC", "action": {"type": "discord", "discord": "mute"}},
    "key5": {"label": "Spotify", "icon": "\U0001F3B5", "action": {"type": "open", "target": "spotify.exe"}},
}}}

win = ConfigWindow(DockController(Config(data)))
win.resize(1260, 820)
win._enter_folder_edit("folder1")
win.select("key1")
win.show()
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "gui_folder.png")
QTimer.singleShot(700, lambda: (win.grab().save(out), print("saved", out), app.quit()))
app.exec()
