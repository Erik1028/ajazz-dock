"""Capture a frame mid page-slide to verify the transition renders."""
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

win = ConfigWindow(DockController(Config(default_config())))
win.show()
ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")


def trigger():
    win._goto_page(1)


def grab_mid():
    win.grab().save(os.path.join(ASSETS, "gui_slide_mid.png"))
    print("mid-slide grabbed; cur_page =", win.cur_page,
          "slide_anim:", getattr(win, "_slide_anim", None) is not None)


def fin():
    win.grab().save(os.path.join(ASSETS, "gui_slide_end.png"))
    app.quit()


QTimer.singleShot(600, trigger)
QTimer.singleShot(720, grab_mid)        # ~120ms into the 300ms slide
QTimer.singleShot(1100, fin)
app.exec()
