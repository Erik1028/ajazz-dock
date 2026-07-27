"""Central design tokens — one palette + one radius scale, referenced by BOTH the
QSS string and the custom paint code (CircleControl / KeyTile / colour wells), so
keys, knobs, buttons and tabs all light up in the same brand accent.

Design direction: a dark, near-black green-charcoal "desk" on which the Ajazz dock
sits as the hero — an elevated, shadowed slab whose selected control glows in ONE
brand accent (electric mint #35e08a), replacing the old generic blue everywhere.
"""
from string import Template

# ---- palette ---------------------------------------------------------------
# Canvas: a deep green that fades to near-black (stepped surface levels below it).
BG_TOP       = "#0c241b"
BG_MID       = "#0a1712"
BG_BOT       = "#06100c"

# Sidebar — slightly lifted off the canvas so the control rail reads as separate.
SIDEBAR_TOP  = "#102a20"
SIDEBAR_BOT  = "#0b1611"

# Native Windows 11 title-bar colour (DwmSetWindowAttribute) — a clearly-green, on-brand caption so
# the OS chrome connects with the app instead of the default grey.
CAPTION_BG   = "#16412e"

# Three stepped surfaces for cards / inputs / raised buttons.
SURFACE_1    = "#13211b"   # cards, groupboxes, the inspector column
SURFACE_2    = "#172620"   # inputs (line edits, combos, spinboxes)
SURFACE_3    = "#1b2c25"   # buttons, segmented controls

# The device slab — a faintly lifted dark-green object that floats on its drop
# shadow, distinct from the canvas behind it (top catches a little light).
DEVICE_TOP   = "#13241c"
DEVICE_BOT   = "#0a120e"
KEY_BG       = "#000000"

BORDER       = "#26402f"
BORDER_HOVER = "#3c6149"

# Brand accent — every selection / focus / primary / slider signal uses this.
ACCENT       = "#35e08a"
ACCENT_HOVER = "#57ef9f"
ACCENT_DIM   = "#1f5c3d"   # quiet accent (slider fill, faint borders)
ACCENT_INK   = "#06140d"   # text / icon drawn on top of an accent fill


def _rgba(hexc: str, a: float) -> str:
    h = hexc.lstrip("#")
    return f"rgba({int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}, {a})"


# Translucent accent (scrollbar handles, soft hovers) — derived so they track ACCENT.
ACCENT_GLASS_DIM = _rgba(ACCENT, 0.35)
ACCENT_GLASS_MID = _rgba(ACCENT, 0.65)

# Text contrast steps (lifted from the old greys for AA on the dark panels).
TEXT         = "#e7efe9"
TEXT_DIM     = "#acc3b6"
TEXT_FAINT   = "#7e9889"

# Round-control fills (molded-plastic gradient: lighter top, darker bottom).
KNOB_TOP     = "#243029"
KNOB_BOT     = "#161f1a"
BTN_TOP      = "#141b17"
BTN_BOT      = "#0a0f0d"
TICK         = "#7d937f"

DANGER       = "#ff6b6b"


def _hex_rgba(hexc: str, a: int = 255):
    h = hexc.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


def _glyph_path(name, draw_fn, size=(24, 24)) -> str:
    """Draw a tiny PNG glyph to a temp file at import time and return a forward-slash path — so QSS
    can theme the combo/spin arrows + the checkbox tick (Qt otherwise draws grey native Windows
    glyphs). A FILE path is used (not a data: URI) because Qt's stylesheet url() does not load data
    URIs; and PNG (not SVG) so no QtSvg image plugin is required."""
    from PIL import Image, ImageDraw
    import tempfile
    import os as _os
    d = _os.path.join(tempfile.gettempdir(), "hexpad_glyphs")
    _os.makedirs(d, exist_ok=True)
    p = _os.path.join(d, name + ".png")
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(im))
    im.save(p, "PNG")
    return p.replace("\\", "/")               # QSS url() wants forward slashes, even on Windows


# Themed glyphs: a dark tick for the mint checkbox, TEXT_DIM chevrons for combo/spin arrows.
CHECK_PNG    = _glyph_path("check", lambda d: d.line([(6, 15), (12, 21), (22, 8)], fill=(10, 15, 13, 255),
                                                     width=4, joint="curve"), (28, 28))
CHEVDOWN_PNG = _glyph_path("chevdown", lambda d: d.line([(6, 9), (12, 15), (18, 9)], fill=_hex_rgba(TEXT_DIM),
                                                        width=3, joint="curve"))
CHEVUP_PNG   = _glyph_path("chevup", lambda d: d.line([(6, 15), (12, 9), (18, 15)], fill=_hex_rgba(TEXT_DIM),
                                                      width=3, joint="curve"))

# ---- radius scale (4 steps — collapsed from the old 11) --------------------
R_SM   = 8
R_MD   = 12
R_LG   = 16
R_PILL = 999

TOKENS = {k: v for k, v in globals().items()
          if k.isupper() and isinstance(v, (str, int))}


_QSS = Template("""
QWidget { font-family: 'Segoe UI'; font-size: 13px; color: $TEXT; }
QMainWindow, QWidget#root { background: qlineargradient(x1:0, y1:0, x2:0.4, y2:1,
    stop:0 $BG_TOP, stop:0.55 $BG_MID, stop:1 $BG_BOT); }
QLabel#display { font-size: 22px; font-weight: 700; letter-spacing: 0.5px; }
QLabel#h1 { font-size: 16px; font-weight: 600; }
QLabel#dim { color: $TEXT_DIM; }
QLabel#section { color: $TEXT_DIM; font-size: 11px; font-weight: 600; letter-spacing: 0.5px; }
QLabel#sliderval { color: $ACCENT; font-size: 12px; font-weight: 700; }
QLabel#statchip { background: $SURFACE_2; border: 1px solid $BORDER; border-radius: 9px;
    padding: 3px 10px; color: $TEXT_DIM; font-size: 11px; font-weight: 600; }
QLabel#nodevbanner { background: $SURFACE_2; border: 1px solid $BORDER;
    border-radius: ${R_PILL}px; padding: 5px 16px; color: $TEXT_FAINT; font-size: 11.5px; font-weight: 600; }
QToolButton#infobtn { border: 1px solid $BORDER; border-radius: 11px; min-width: 22px; max-width: 22px;
    min-height: 22px; max-height: 22px; padding: 0; color: $TEXT_DIM; background: $SURFACE_2; font-size: 12px; }
QToolButton#infobtn:hover { color: $ACCENT; border-color: $ACCENT; }
QFrame#nowplaying { background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_LG}px; }
QFrame#nowplaying:hover { border-color: $BORDER_HOVER; }
QLabel#nptitle { color: $TEXT; font-size: 12px; font-weight: 600; }
QLabel#npsub { color: $TEXT_DIM; font-size: 11px; }
QLabel#npicon { color: $ACCENT; font-size: 11px; font-weight: 700; }
QToolButton#collsec { border: none; font-weight: 600; padding: 4px 2px; color: $TEXT; }
QToolButton#collsec:hover, QToolButton#collsec:focus { color: $ACCENT; }
QToolButton#cathdr { border: none; font-weight: 700; padding: 7px 2px; color: $TEXT_DIM; }
QToolButton#cathdr:hover { color: $TEXT; }
QToolButton#cathdr:focus { color: $ACCENT; }
QGroupBox { background: $SURFACE_1; border: 1px solid $BORDER; border-radius: ${R_LG}px; margin-top: 16px; padding: 12px; }
QGroupBox#panel { margin-top: 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; color: $TEXT_DIM; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {
    background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_MD}px; padding: 6px 9px; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: $ACCENT; }
QLineEdit:hover, QPlainTextEdit:hover, QComboBox:hover, QSpinBox:hover { border-color: $BORDER_HOVER; }
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: $TEXT_FAINT; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; width: 20px; border: none; }
QComboBox::down-arrow { image: url("$CHEVDOWN_PNG"); width: 12px; height: 12px; }
QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; width: 18px; border: none; background: transparent; }
QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; width: 18px; border: none; background: transparent; }
QSpinBox::up-arrow { image: url("$CHEVUP_PNG"); width: 11px; height: 11px; }
QSpinBox::down-arrow { image: url("$CHEVDOWN_PNG"); width: 11px; height: 11px; }
QPushButton, QToolButton { background: $SURFACE_3; border: 1px solid $BORDER; border-radius: ${R_MD}px; padding: 7px 12px; }
QPushButton:hover, QToolButton:hover { border-color: $BORDER_HOVER; }
QPushButton:focus, QToolButton:focus { border-color: $ACCENT; }
QPushButton:pressed, QToolButton:pressed { background: $ACCENT_DIM; border-color: $ACCENT; }
QPushButton:disabled, QToolButton:disabled { color: $TEXT_FAINT; }
QPushButton#primary { background: $ACCENT; border-color: $ACCENT; color: $ACCENT_INK; font-weight: 600; }
QPushButton#primary:hover { background: $ACCENT_HOVER; border-color: $ACCENT_HOVER; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 5px; border: 1px solid $BORDER; background: $SURFACE_2; }
QCheckBox::indicator:hover { border-color: $BORDER_HOVER; }
QCheckBox::indicator:checked { background: $ACCENT; border-color: $ACCENT; image: url("$CHECK_PNG"); }
QCheckBox::indicator:focus { border-color: $ACCENT; }
QLabel#key { background: $KEY_BG; border: 2px solid $BORDER; border-radius: ${R_LG}px; padding: 0; }
QLabel#key:hover { background: $DEVICE_TOP; }   /* the border glow now eases in via KeyTile's paint */
QLabel#key:focus { border-color: $ACCENT; }
QLabel#key[selected="true"] { border: 2px solid $ACCENT; }
QWidget#main { background: transparent; }
QFrame#sidebar { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
    stop:0 $SIDEBAR_TOP, stop:1 $SIDEBAR_BOT); border-right: 1px solid $BORDER; }
QFrame#sidebar QPushButton { text-align: left; padding: 7px 10px; }
QFrame#card { background: rgba(255,255,255,0.022); border: 1px solid $BORDER; border-radius: ${R_MD}px; }
QLabel#cardtitle { color: $TEXT_DIM; font-size: 11px; font-weight: 700; letter-spacing: 0.7px; }
QFrame#inspector { background: $SURFACE_1; border-top: 1px solid $BORDER; }
QFrame#hsep { background: $BORDER; border: none; }
/* Draggable section seams — grab between the main area & sidebar, or between editor cards. */
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 6px; }
QSplitter::handle:vertical { height: 6px; }
QSplitter#bodysplit::handle, QSplitter#mainvsplit::handle, QSplitter#editorsplit::handle { background: $BORDER; }
QSplitter::handle:hover { background: $ACCENT_GLASS_MID; }
QSplitter::handle:pressed { background: $ACCENT; }
/* Stream Deck-style top header: device + profile selectors, settings gear */
/* The header has no surface of its own — it sits on the window's green gradient so it reads as one
   continuous surface with the device canvas below (no separate grey strip). */
QFrame#headerbar { background: transparent; }
QLabel#hdrtitle { font-size: 18px; font-weight: 800; letter-spacing: 0.3px; }
QComboBox#hdrprofile { background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_MD}px; padding: 5px 9px; }
QComboBox#hdrprofile:hover { border-color: $BORDER_HOVER; }
QToolButton#hdraddbtn { background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_MD}px;
    min-width: 30px; max-width: 30px; min-height: 28px; max-height: 28px; padding: 0; font-size: 15px; color: $TEXT_DIM; }
QToolButton#hdraddbtn:hover { border-color: $ACCENT; color: $ACCENT; }
QToolButton#hdriconbtn { background: transparent; border: 1px solid $BORDER; border-radius: ${R_MD}px;
    min-width: 34px; max-width: 34px; min-height: 30px; max-height: 30px; padding: 0; font-size: 17px; color: $TEXT_DIM; }
QToolButton#hdriconbtn:hover { border-color: $ACCENT; color: $ACCENT; }
/* Right-hand actions list (drag onto a control to bind it) */
QFrame#actionsidebar { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
    stop:0 $SIDEBAR_TOP, stop:1 $SIDEBAR_BOT); border-left: 1px solid $BORDER; }
QFrame#actionrow { background: $SURFACE_2; border: 1px solid transparent; border-radius: ${R_MD}px; }
QFrame#actionrow:hover { background: $SURFACE_3; border-color: $ACCENT; }
QLabel#rowname { color: $TEXT; font-size: 12.5px; }
QLabel#rowemoji { font-size: 16px; }
QFrame#actionchip { background: $SURFACE_2; border: 1px solid transparent; border-radius: ${R_MD}px; }
QFrame#actionchip:hover { background: $SURFACE_3; border-color: $ACCENT; }
QLabel#chipname { color: $TEXT_DIM; font-size: 10.5px; }
QLabel#chipemoji { font-size: 18px; }
QToolButton#viewtoggle { background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_SM}px;
    min-width: 26px; max-width: 26px; min-height: 22px; max-height: 22px; padding: 0; color: $TEXT_DIM; font-size: 13px; }
QToolButton#viewtoggle:hover, QToolButton#viewtoggle:checked { border-color: $ACCENT; color: $ACCENT; }
/* Windows-Settings-style preferences: left section nav + right content pane */
QDialog#prefsdialog { background: $BG_MID; }
QListWidget#prefsnav { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
    stop:0 $SIDEBAR_TOP, stop:1 $SIDEBAR_BOT); border: none; border-right: 1px solid $BORDER;
    outline: none; padding: 10px 6px; font-size: 13px; }
QListWidget#prefsnav::item { padding: 10px 12px; border-radius: ${R_MD}px; color: $TEXT_DIM; margin: 2px 4px; }
QListWidget#prefsnav::item:hover { background: rgba(255,255,255,0.05); color: $TEXT; }
QListWidget#prefsnav::item:selected { background: $SURFACE_3; color: $ACCENT; }
QLabel#prefshdr { font-size: 19px; font-weight: 700; letter-spacing: 0.2px; }
QPushButton#edclear { background: transparent; border: 1px solid $BORDER; border-radius: ${R_MD}px;
    padding: 5px 14px; color: $TEXT_DIM; }
QPushButton#edclear:hover { border-color: $DANGER; color: $DANGER; }
QPushButton#edtest { background: transparent; border: 1px solid $BORDER; border-radius: ${R_MD}px;
    padding: 5px 14px; color: $TEXT_DIM; }
QPushButton#edtest:hover { border-color: $ACCENT; color: $ACCENT; }
QPushButton#edtest:disabled { color: $TEXT_FAINT; border-color: $BORDER; }
/* Inspector sections are TILED, not floating: same SURFACE_1 plane as the inspector tray, no
   shadow/border/radius — adjacent cards are divided only by the editor splitter's 1px seam, so the
   whole bottom bar reads as one cohesive surface (inputs stay SURFACE_2 so they still pop). */
QFrame#editorcard { background: $SURFACE_1; border: none; border-radius: 0; }
QLabel#cardhdr { color: $TEXT; font-size: 13px; font-weight: 700; letter-spacing: 0.3px; }
/* Compaction — inspector-scoped only (header / sidebar / Settings / dialogs keep their sizing) */
#editorcard QPushButton, #editorcard QToolButton { padding: 6px 11px; }
#editorcard QPushButton#primary { padding: 5px 12px; }
#editorcard QLineEdit, #editorcard QComboBox, #editorcard QSpinBox { padding: 5px 9px; }
/* The 'current action' chip — a slim status pill (what's bound; click to swap). Assigning actions
   lives on the right ACTIONS sidebar, so this is deliberately quiet, not a second picker. */
QPushButton#actionchip { background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_MD}px;
    padding: 6px 10px; color: $TEXT; text-align: left; font-weight: 500; }
QPushButton#actionchip:hover { border-color: $ACCENT; color: $TEXT; }
/* First-run guided-tour callout */
QFrame#tourcard { background: $SURFACE_2; border: 1px solid $ACCENT; border-radius: ${R_LG}px; }
QLabel#tourtitle { font-size: 15px; font-weight: 700; color: $TEXT; }
QLabel#tourbody { color: $TEXT_DIM; }
QLabel#tourdots { color: $ACCENT; font-size: 11px; letter-spacing: 2px; }
QFrame#device { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
    stop:0 $DEVICE_TOP, stop:1 $DEVICE_BOT); border: 1px solid $BORDER_HOVER; border-radius: ${R_LG}px; }
QLabel#wordmark { color: $TEXT_FAINT; font-size: 12px; font-weight: 600; letter-spacing: 3px; }
QLabel#profilechip { color: $TEXT_DIM; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; }
QLabel#scopehint { color: $TEXT_FAINT; font-size: 10px; }
QLabel#livechip { color: $ACCENT; font-weight: 600; padding: 5px 2px; }
QToolButton#seg { background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_MD}px; padding: 6px 9px; }
QToolButton#seg:checked { background: $ACCENT; border-color: $ACCENT; color: $ACCENT_INK; }
QToolButton#seg:focus { border-color: $ACCENT; }
/* Encoder sub-action tiles — show turn-left / push / turn-right + their current binding at once. */
QToolButton#encseg { background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_MD}px;
    padding: 7px 8px; color: $TEXT; font-size: 11px; text-align: left; }
QToolButton#encseg:hover { border-color: $BORDER_HOVER; }
QToolButton#encseg:checked { border-color: $ACCENT; background: $SURFACE_3; }
/* Tap / Double-tap / Hold gesture selector */
QToolButton#gslot { background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_MD}px;
    padding: 5px 4px; color: $TEXT_DIM; font-size: 11px; font-weight: 600; }
QToolButton#gslot:hover { border-color: $BORDER_HOVER; color: $TEXT; }
QToolButton#gslot:checked { border-color: $ACCENT; background: $SURFACE_3; color: $TEXT; }
QFrame#tabbar { background: rgba(255,255,255,0.05); border: 1px solid $BORDER; border-radius: ${R_LG}px; }
QToolButton#tab { background: transparent; border: 1px solid transparent; border-radius: ${R_MD}px; padding: 7px 18px; color: $TEXT_DIM; }
QToolButton#tab:hover { background: rgba(255,255,255,0.08); color: $TEXT; }
QToolButton#tab:checked { background: $ACCENT; color: $ACCENT_INK; font-weight: 600; }
QToolButton#tabicon { background: rgba(255,255,255,0.05); border: 1px solid $BORDER; border-radius: ${R_MD}px; padding: 0; color: $TEXT_DIM; font-size: 15px; }
QToolButton#tabicon:hover { background: rgba(255,255,255,0.09); border-color: $ACCENT; color: $TEXT; }
/* Bottom page switcher: numbered pills + add, active page highlighted in the accent. */
QToolButton#pagepill { background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_SM}px;
    color: $TEXT_DIM; font-weight: 600; min-width: 22px; padding: 5px 9px; }
QToolButton#pagepill:hover { border-color: $BORDER_HOVER; color: $TEXT; }
QToolButton#pagepill:checked { background: $ACCENT; border-color: $ACCENT; color: $ACCENT_INK; }
QToolButton#pageadd { background: transparent; border: 1px solid $BORDER; border-radius: ${R_SM}px;
    color: $TEXT_DIM; min-width: 22px; padding: 5px 9px; }
QToolButton#pageadd:hover { border-color: $ACCENT; color: $TEXT; }
QSlider::groove:horizontal { height: 5px; background: $SURFACE_3; border-radius: 2px; }
QSlider::sub-page:horizontal { background: $ACCENT_DIM; border-radius: 2px; }
QSlider::handle:horizontal { background: $ACCENT; width: 14px; margin: -6px 0; border-radius: 7px; }
QSlider::handle:horizontal:hover { background: $ACCENT_HOVER; }
QSlider::handle:horizontal:pressed { background: $ACCENT_HOVER; width: 16px; margin: -7px 0; border-radius: 8px; }
QSlider::handle:horizontal:disabled { background: $BORDER_HOVER; }
/* NB: deliberately NO QSlider:focus rule — a :focus rule on a slider sub-control makes Qt paint an
   ugly full-width frame around the whole widget. Hover/pressed already give the handle feedback. */
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
/* Thin, rounded, mint scrollbars (overlay-style) so they match the brand accent and never
   collide with content — replaces the chunky default Windows bar with steppers. */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 2px 2px 0; border: none; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0 2px 2px 2px; border: none; }
QScrollBar::handle:vertical { background: $ACCENT_GLASS_DIM; border-radius: ${R_SM}px; min-height: 36px; }
QScrollBar::handle:horizontal { background: $ACCENT_GLASS_DIM; border-radius: ${R_SM}px; min-width: 36px; }
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: $ACCENT_GLASS_MID; }
QScrollBar::handle:vertical:pressed, QScrollBar::handle:horizontal:pressed { background: $ACCENT; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; width: 0; border: none; background: none; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; height: 0; border: none; background: none; }
QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical,
QScrollBar::left-arrow:horizontal, QScrollBar::right-arrow:horizontal { background: none; image: none; width: 0; height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QScrollBar::corner { background: transparent; }
/* Dropdown / context / tray menus + submenu fly-outs — dark-mint, rounded, padded. */
QMenu { background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_MD}px; padding: 6px; }
QMenu::item { padding: 7px 16px 7px 14px; border-radius: ${R_SM}px; margin: 1px 3px; color: $TEXT; }
QMenu::item:selected { background: $ACCENT; color: $ACCENT_INK; }
QMenu::item:disabled { color: $TEXT_FAINT; background: transparent; }
QMenu::separator { height: 1px; background: $BORDER; margin: 5px 10px; }
QMenu::right-arrow { width: 10px; height: 10px; margin-right: 6px; }
QMenu::icon { padding-left: 8px; }
/* Combo-box pop-up lists (Media, Mode, Target… in the editor) — match the menus. */
QComboBox QAbstractItemView {
    background: $SURFACE_2; border: 1px solid $BORDER; border-radius: ${R_SM}px;
    padding: 4px; outline: none;
    selection-background-color: $ACCENT; selection-color: $ACCENT_INK; }
QComboBox QAbstractItemView::item { min-height: 26px; padding: 4px 8px; border-radius: ${R_SM}px; }
/* Tiles in the searchable action GRID — hover + keyboard-highlight. */
QFrame#actiontile { background: $SURFACE_1; border: 1px solid $BORDER; border-radius: ${R_MD}px; }
QFrame#actiontile:hover { background: $SURFACE_3; border-color: $BORDER_HOVER; }
QFrame#actiontile[active="true"] { background: $SURFACE_3; border-color: $ACCENT; }
QFrame#actiontile QLabel { background: transparent; }
QLabel#tileemoji { font-size: 26px; }
QLabel#tilename { font-size: 11px; font-weight: 600; color: $TEXT; }
QToolButton#emoji { background: transparent; border: none; border-radius: ${R_SM}px; padding: 0; }
QToolButton#emoji:hover { background: $SURFACE_3; }
QToolButton#emojicat { background: transparent; border: 1px solid transparent; border-radius: ${R_SM}px; padding: 0; }
QToolButton#emojicat:hover { background: rgba(255,255,255,0.07); }
QToolButton#emojicat:checked { background: rgba(255,255,255,0.10); border-color: $ACCENT; }
QDialog { background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
    stop:0 $BG_MID, stop:1 $BG_BOT); }
""")


# ---- accent themes ---------------------------------------------------------
# (accent, hover, dim, ink) — pick via the configurator; recolours the whole UI.
ACCENTS = {
    "mint":   ("#35e08a", "#57ef9f", "#1f5c3d", "#06140d"),
    "blue":   ("#4a9bff", "#74b4ff", "#1f3a66", "#04101f"),
    "violet": ("#a87bff", "#c3a3ff", "#43306e", "#120a1f"),
    "amber":  ("#f5b830", "#ffce5c", "#6e5316", "#1f1604"),
    "pink":   ("#ff5fa8", "#ff86bf", "#6e2347", "#1f0612"),
}


def set_accent(name: str) -> None:
    """Switch the brand accent (recomputes the derived glass tints + the TOKENS table)."""
    global ACCENT, ACCENT_HOVER, ACCENT_DIM, ACCENT_INK, ACCENT_GLASS_DIM, ACCENT_GLASS_MID, TOKENS
    ACCENT, ACCENT_HOVER, ACCENT_DIM, ACCENT_INK = ACCENTS.get(name) or ACCENTS["mint"]
    ACCENT_GLASS_DIM = _rgba(ACCENT, 0.35)
    ACCENT_GLASS_MID = _rgba(ACCENT, 0.65)
    TOKENS = {k: v for k, v in globals().items() if k.isupper() and isinstance(v, (str, int))}


def build_qss() -> str:
    return _QSS.substitute(TOKENS)
