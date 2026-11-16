"""
Macronaut — main window, tabs, hotkey listener, and application bootstrap.
"""
import sys
import time
import json
import threading
import collections
from pathlib import Path
from datetime import datetime
from typing import List, Optional

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QLabel, QPushButton, QCheckBox, QRadioButton,
    QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox, QFileDialog, QDialog,
    QDialogButtonBox, QTableWidget, QTableWidgetItem, QHeaderView, QListWidget,
    QListWidgetItem, QButtonGroup, QSizePolicy, QFrame, QScrollArea,
    QTextEdit, QPlainTextEdit, QAbstractItemView, QMessageBox, QMenu, QSplitter,
    QLayout, QSizeGrip, QStyledItemDelegate, QStyle,
    QAbstractButton, QSlider, QAbstractSpinBox, QStyleOptionButton,
    QStackedWidget,
)
from PySide6.QtCore  import (Qt, QTimer, QThread, Signal, QObject, QSize,
                            QRect, QPoint, QPointF, QTime, QMimeData,
                            QPropertyAnimation, QUrl, QEvent)
from PySide6.QtGui   import (QFont, QColor, QPalette, QPainter, QPen, QBrush,
                            QPixmap, QIcon, QCursor, QKeySequence, QDrag,
                            QDesktopServices,
                            QAction)   # QAction moved QtWidgets -> QtGui in Qt6
from pynput import keyboard as _pk

import importlib.util

# Image matching needs OpenCV (pyautogui's confidence= matching backend).
# Detect it once so the UI can disable image features cleanly instead of
# failing silently at runtime.
_HAS_CV2 = importlib.util.find_spec("cv2") is not None
_NO_CV2_MSG = ("Image matching needs the opencv-python package. "
               "Install it with:  pip install opencv-python")

# OCR (text recognition) goes through the engine abstraction in ocr.py, which
# selects Windows.Media.Ocr first and falls back to RapidOCR. We only ask the
# abstraction whether *some* engine is available — never a specific one.
import ocr as _ocr
_HAS_OCR = _ocr.available()
_NO_OCR_MSG = ("Text recognition needs Windows OCR (pip install winsdk) "
               "or the rapidocr-onnxruntime fallback.")


# ── Accelerated spinbox factories ─────────────────────────────────────────────
# setAccelerated(True) makes the value change faster when the arrow is held down.

def _spin(lo: int, hi: int, val: int = 0, suffix: str = "",
          prefix: str = "", w: int = 0) -> QSpinBox:
    sb = QSpinBox()
    sb.setRange(lo, hi)
    sb.setValue(val)
    sb.setAccelerated(True)
    if suffix: sb.setSuffix(suffix)
    if prefix: sb.setPrefix(prefix)
    if w:      sb.setFixedWidth(w)
    return sb


def _dspin(lo: float, hi: float, val: float = 0.0, step: float = 0.1,
           suffix: str = "", w: int = 0) -> QDoubleSpinBox:
    sb = QDoubleSpinBox()
    sb.setRange(lo, hi)
    sb.setValue(val)
    sb.setSingleStep(step)
    sb.setAccelerated(True)
    if suffix: sb.setSuffix(suffix)
    if w:      sb.setFixedWidth(w)
    return sb


# Chrome a styled spin box spends on things that are not text: 2 px of border,
# 20 px of horizontal padding and the 20 px up/down button column, all set in
# `_QSS` below. `_fit_spin` adds it back so a box is sized by what it can show.
_SPIN_CHROME = 2 + 20 + 20


def _fit_spin(sb, sample: str, pad: int = 6):
    """Widen `sb` so `sample` — the widest text it can ever display — still fits.

    A fixed width guesses at the font; this measures it, so the suffix survives
    a larger UI font or a DPI the guess was not made at.
    """
    sb.setMinimumWidth(sb.fontMetrics().horizontalAdvance(sample)
                       + _SPIN_CHROME + pad)
    return sb


import re as _re


class DurationSpinBox(QSpinBox):
    """Spin box whose VALUE is always milliseconds, but which displays the
    value as 'ms' below 1000 and as 's' once it reaches 1000 — switching the
    unit automatically (and back) as the value crosses the 1-second mark."""

    def __init__(self, lo_ms: int, hi_ms: int, val_ms: int = 0, w: int = 0):
        super().__init__()
        self.setRange(lo_ms, hi_ms)
        self.setValue(val_ms)
        self.setAccelerated(True)
        if w:
            self.setMinimumWidth(w)

    def textFromValue(self, v: int) -> str:
        if v >= 1000:
            secs = v / 1000.0
            txt = (f"{secs:.2f}".rstrip("0").rstrip("."))
            return f"{txt} s"
        return f"{v} ms"

    def valueFromText(self, text: str) -> int:
        t = text.strip().lower()
        m = _re.search(r"[0-9]*\.?[0-9]+", t)
        if not m:
            return self.value()
        num = float(m.group())
        if "ms" in t:
            return int(round(num))
        if "s" in t:                      # seconds
            return int(round(num * 1000))
        # bare number: interpret in the unit currently shown
        return int(round(num * 1000)) if self.value() >= 1000 else int(round(num))

    def validate(self, text: str, pos: int):
        from PySide6.QtGui import QValidator
        t = text.strip().lower()
        if t == "":
            return (QValidator.Intermediate, text, pos)
        if _re.fullmatch(r"[0-9]*\.?[0-9]*\s*(m?s)?", t):
            return (QValidator.Acceptable, text, pos)
        return (QValidator.Invalid, text, pos)

    # Step sizes, by the unit the value is being read in: 50 ms under a second,
    # 100 ms (0.1 s) up to a minute, 1 s above it. Without the third tier a
    # ten-minute wait is 5400 clicks away from zero.
    @staticmethod
    def _grid(v: int) -> int:
        return 1000 if v >= 60_000 else (100 if v >= 1000 else 50)

    def stepBy(self, steps: int):
        """Step ONTO a round number rather than BY a fixed amount.

        ⚠ It used to add the step to the value, so stepping up from 950 ms gave
        1.05 s, then 1.15, 1.25 — every value in the second range off by 50 ms
        for the rest of the box's life, and "1 s" unreachable from below without
        typing it. Snapping to the grid means a value already off it (typed, or
        loaded from an old flow) lands on the grid first and steps cleanly after.
        """
        v = self.value()
        for _ in range(abs(steps)):
            if steps > 0:
                g = self._grid(v)
                v = (v // g + 1) * g
            else:
                # The tier is chosen from just *below* v so stepping down off a
                # boundary drops into the finer grid: 1 s -> 950 ms, not 900.
                g = self._grid(v - 1)
                v = -(-v // g) * g - g
        self.setValue(v)


def _durspin(lo_ms: int, hi_ms: int, val_ms: int = 0, w: int = 0) -> "DurationSpinBox":
    return DurationSpinBox(lo_ms, hi_ms, val_ms, w)


# Breathing room on top of the measured requirement for an Add-node label —
# roughly two characters at the palette's font size. Enough to absorb any
# disagreement between what the font metrics promise and what the glyphs
# actually paint, which is the last thing that could still take a letter off.
# It was 10, which was a guess stacked on a guess; the requirement underneath it
# is measured now (see `_label_width`), so this is margin, not the whole answer.
PALETTE_SLACK = 16


def _label_width(b: QPushButton) -> int:
    """How wide `b` must be for its whole label to fit — measured, not asked for.

    ⚠ **`sizeHint()` is not big enough to hold the text it was computed from**,
    and that — not the emoji, not the font, not the DPI — is why "Comment" kept
    losing its last letter. Measured here, on the real fonts: the string advances
    **92 px**, and the 127 px hint leaves a content box of **90 px** once the
    style's own border and padding come off it. Qt does not elide the overflow,
    it clips it, so the final glyph is cut — and it survived on this machine only
    because `t` happens to ink 2 px narrower than it advances. Any font whose
    last glyph fills its advance loses the letter outright.

    So: ask the font how wide the string lays out, ask the *style* how much of a
    button is not content, and add them. Both halves come from the machine the
    app is running on, neither is a guess, and the arithmetic is the one Qt
    itself skipped. `PALETTE_SLACK` then sits on top of a real number instead of
    standing in for one.

    Never returns less than the hint — the hint still owns the icon, the frame
    and every other thing a button reserves room for that has nothing to do with
    text.
    """
    b.ensurePolished()
    hint = b.sizeHint().width()
    try:
        # Chrome (borders + padding) is constant in the width, so it can be read
        # off any probe rect: whatever a 1000 px button does not give to content.
        opt = QStyleOptionButton()
        opt.initFrom(b)
        opt.rect = QRect(0, 0, 1000, max(34, b.sizeHint().height()))
        opt.text = b.text()
        cr = b.style().subElementRect(QStyle.SE_PushButtonContents, opt, b)
        if not (0 < cr.width() <= 1000):
            return hint
        chrome = 1000 - cr.width()
        return max(hint, b.fontMetrics().horizontalAdvance(b.text()) + chrome)
    except Exception:
        return hint

from keystrokes import display_combo
# ⚠ SequenceManager is deliberately NOT imported here. It owns the pre-2.0
# linear playback path (recorder.PlaybackWorker), which nothing in this file
# has called since flow_exec.FlowWorker replaced it — it was imported and
# unused, which reads as "this is how playback works" to anyone following the
# imports. Playback is `flow_exec.FlowWorker`; see the note on those two
# classes in recorder.py.
from recorder   import SequenceRecorder, SeqStep
import settings as settings_mod
from settings   import SettingsManager, data_dir, scripts_dir
from stats      import StatsManager, Session
from input_backends import BACKEND_LABELS, interception_available
from input_backends import safe_type_cps as _safe_type_cps
from input_backends import MAX_TYPE_CPS as _MAX_TYPE_CPS


def _max_type_cps() -> float:
    return _MAX_TYPE_CPS


try:
    from sendinput_backend import layout_family
except Exception:      # pragma: no cover - non-Windows / import edge
    def layout_family() -> str:
        return ""

import updater
import updater_ui
import version
import crashreport
import crashsend
import crash_ui
import star_ui

import licensing
import licensing_ui
import entitlements
import starters

import flow
import recovery
import flow_exec
import flow_timeline
import runstats
import flow_canvas
import flow_dialogs
# The Basic face, and the painted window buttons both faces use. TitleButton
# moved into main.py for the one release that had no Basic face; it lives back
# in compact.py now, which is where it started — main imports compact, so this
# is the direction that does not need a third module to break a cycle.
from compact    import CompactFace, TitleButton
from tray       import SystemTray


import os as _os


# In a source checkout the modules live in `src/`, and `assets/`, LICENSE and
# the notices sit one level up, at the repository root. The frozen build has
# all of them together under `_MEIPASS` and never reads this.
_SOURCE_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _asset(name: str) -> str:
    """Resolve a bundled asset path (works both in dev and PyInstaller)."""
    base = getattr(sys, "_MEIPASS", _SOURCE_ROOT)
    return _os.path.join(base, "assets", name)


def _legal_text(name: str) -> str:
    """Read a bundled legal document (LICENSE, THIRD-PARTY-NOTICES.md).

    macronaut.spec ships these at the bundle root so a user who only ever has
    Macronaut.exe still has the terms they agreed to.
    """
    base = getattr(sys, "_MEIPASS", _SOURCE_ROOT)
    try:
        with open(_os.path.join(base, name), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return (f"{name} could not be read from this build.\n\n"
                "The current terms are always available from the project page.")


def app_icon() -> QIcon:
    """Macronaut application icon (helmet on cosmic background)."""
    for fn in ("macronaut.ico", "macronaut_icon.png", "icon.ico"):
        path = _asset(fn)
        if _os.path.exists(path):
            return QIcon(path)
    return QIcon()


def logo_pixmap(height: int = 30) -> QPixmap:
    """Transparent helmet+cursor logo for window headers."""
    for fn in ("macronaut_logo_128.png", "macronaut_icon.png"):
        path = _asset(fn)
        if _os.path.exists(path):
            pm = QPixmap(path)
            if not pm.isNull():
                return pm.scaledToHeight(height, Qt.SmoothTransformation)
    return QPixmap()


# ═══════════════════════════════════════════════════════════════════════════════
#  Stylesheets
# ═══════════════════════════════════════════════════════════════════════════════

from string import Template

# A single token-driven stylesheet, instantiated for the dark (default) and
# light palettes.  $tokens are filled from the palette dicts below.
_QSS = Template('''
* { outline: none; }
QMainWindow, QDialog, QWidget {
    background: $bg; color: $text;
    font-family: "Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", system-ui, Arial, sans-serif;
    font-size: 15px; }

/* ── Tabs ─────────────────────────────────────────────── */
QTabWidget::pane { border: none; background: $bg; top: -1px; }
QTabBar { qproperty-drawBase: 0; background: transparent; }
QTabBar::tab { background: transparent; color: $muted; padding: 9px 18px;
    margin-right: 2px; border: none; border-bottom: 2px solid transparent; font-size: 15px; }
QTabBar::tab:selected { color: $text; border-bottom: 2px solid $accent; }
QTabBar::tab:hover { color: $text; }

/* ── Buttons ──────────────────────────────────────────── */
QPushButton { background: $panel2; color: $text; border: 1px solid $border;
    padding: 7px 16px; border-radius: 9px; font-size: 14px; }
QPushButton:hover { background: $hover; border-color: $accent; }
QPushButton:pressed { background: $border; }
QPushButton:disabled { background: $panel; color: $muted; border-color: $border; }

QPushButton#btnPrimary { background: $accent; color: $accentText; border: none; font-weight: 600; }
QPushButton#btnPrimary:hover { background: $accentHover; }
QPushButton#btnPrimary:pressed { background: $accentPress; }
QPushButton#btnPrimary:disabled { background: $panel2; color: $muted; }

QPushButton#btnDanger { background: $red; color: #000000; border: none; font-weight: 700; }
QPushButton#btnDanger:hover { background: $redHover; color: #000000; }

QPushButton#btnGhost { background: transparent; border: none; color: $muted; padding: 6px 10px; }
QPushButton#btnGhost:hover { color: $text; background: $panel2; }

QPushButton#btn_start { background: $green; color: $accentText; border: none;
    font-size: 17px; font-weight: 700; border-radius: 11px; }
QPushButton#btn_start:hover { background: $greenHover; }
QPushButton#btn_stop { background: $red; color: $accentText; border: none;
    font-size: 17px; font-weight: 700; border-radius: 11px; }
QPushButton#btn_stop:hover { background: $redHover; }

QPushButton#btn_record { background: $accent; color: $accentText; border: none; font-weight: 700; border-radius: 9px; }
QPushButton#btn_record:hover { background: $accentHover; }
QPushButton#btn_play { background: $green; color: $accentText; border: none; font-weight: 700; border-radius: 9px; }
QPushButton#btn_play:hover { background: $greenHover; }

QPushButton#palette_btn { background: $panel2; border: 1px solid $border; color: $text;
    text-align: left; padding-left: 14px; border-radius: 9px; }
QPushButton#palette_btn:hover { background: $hover; border-color: $accent; }
QPushButton#palette_btn:disabled { background: $panel; color: $muted; }

QPushButton#speedPreset { background: $panel2; border: 1px solid $border; color: $muted;
    padding: 4px 4px; border-radius: 7px; font-weight: 600; }
QPushButton#speedPreset:hover { border-color: $accent; color: $text; }
QPushButton#speedPreset:checked { background: $accent; color: $accentText; border-color: $accent; }

QWidget#fieldRow { background: transparent; }
QWidget#segWrap { background: $inputBg; border: 1px solid $border; border-radius: 10px; }
QPushButton#seg { background: transparent; border: none; color: $muted;
    padding: 7px 15px; font-weight: 600; border-radius: 8px; }
QPushButton#seg:hover { color: $text; }
QPushButton#seg:checked { background: $accent; color: $accentText; }

/* ── Inputs ───────────────────────────────────────────── */
QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox, QPlainTextEdit#textBody {
    background: $inputBg; color: $text; border: 1px solid $border;
    padding: 6px 10px; border-radius: 9px; selection-background-color: $accent;
    selection-color: $accentText; }
QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus, QComboBox:focus,
QPlainTextEdit#textBody:focus { border-color: $accent; }
QSpinBox:disabled, QDoubleSpinBox:disabled, QLineEdit:disabled, QComboBox:disabled {
    color: $muted; background: $panel; }
QLineEdit::placeholder { color: $muted; }
QComboBox::drop-down { border: none; width: 22px; }
/* ⚠ Styling ::drop-down at all stops the style drawing its own arrow, and
   nothing replaced it — so every combo in the app read as a plain text field
   and there was no sign it could be opened. The spin boxes below always had
   theirs. */
QComboBox::down-arrow { image: url($arrowDown); width: 11px; height: 11px; }
QComboBox::down-arrow:disabled { image: none; }
QComboBox QAbstractItemView { background: $panel; color: $text; border: 1px solid $border;
    border-radius: 9px; selection-background-color: $accent; selection-color: $accentText; outline: none; }
QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right;
    width: 20px; border: none; background: transparent; border-top-right-radius: 9px; }
QSpinBox::down-button, QDoubleSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right;
    width: 20px; border: none; background: transparent; border-bottom-right-radius: 9px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover { background: $hover; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url($arrowUp); width: 11px; height: 11px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url($arrowDown); width: 11px; height: 11px; }
QSpinBox::up-arrow:disabled, QSpinBox::down-arrow:disabled,
QDoubleSpinBox::up-arrow:disabled, QDoubleSpinBox::down-arrow:disabled { image: none; }

/* ── Cards ────────────────────────────────────────────── */
QGroupBox#card { background: $panel; border: 1px solid $border; border-radius: 16px;
    margin-top: 16px; padding-top: 8px; }
QGroupBox#card::title { subcontrol-origin: margin; subcontrol-position: top left;
    left: 18px; top: 3px; color: $text; font-size: 15px; font-weight: 700; padding: 0 2px; }
QGroupBox { background: $panel; border: 1px solid $border; border-radius: 16px;
    margin-top: 16px; padding-top: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 18px; color: $accent; font-weight: 700; }

/* ── Labels ───────────────────────────────────────────── */
QLabel { color: $text; background: transparent; }
QLabel#hint { color: $muted; font-size: 13px; }
QLabel#h1 { font-size: 20px; font-weight: 800; color: $text; }
QLabel#sub { color: $muted; font-size: 13px; }
QLabel#seq_title { font-size: 20px; font-weight: 800; color: $text; }
QLabel#seq_subtitle { color: $muted; font-size: 13px; }
QLabel#seq_section { color: $muted; font-weight: 700; font-size: 11px; }
QLabel#seq_summary { color: $muted; font-size: 13px; }
QLabel#label_status { font-size: 15px; font-weight: 700; }
QLabel#label_counter { font-size: 26px; font-weight: 800; color: $accent; }
QLabel#brand { font-size: 17px; font-weight: 800; color: $text; }
QLabel#brandTag { color: $accentText; background: $accent; border-radius: 7px;
    padding: 2px 8px; font-size: 11px; font-weight: 800; }
QLabel#chip { color: $muted; font-size: 13px; }
QLabel#imgPreview { border: 1px solid $border; border-radius: 8px; color: $muted; background: $inputBg; }

/* ── Table ────────────────────────────────────────────── */
QTableWidget { background: $panel; color: $text; gridline-color: transparent;
    border: 1px solid $border; border-radius: 14px; alternate-background-color: $panel; }
QTableWidget::item { padding: 7px 8px; border: none; }
QTableWidget::item:selected { background: $selBg; color: $text; }
QHeaderView::section { background: transparent; color: $muted; padding: 9px 8px;
    border: none; border-bottom: 1px solid $border; font-weight: 700; font-size: 13px; }
QHeaderView { background: $panel; }
QTableCornerButton::section { background: $panel; border: none; }

/* ── Scrollbars ───────────────────────────────────────── */
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
QScrollBar::handle:vertical { background: $handle; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: $accent; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 2px; }
QScrollBar::handle:horizontal { background: $handle; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: $accent; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

/* ── Checkboxes & radios ──────────────────────────────── */
QCheckBox, QRadioButton { spacing: 8px; background: transparent; }
QCheckBox::indicator, QRadioButton::indicator { width: 18px; height: 18px;
    border: 2px solid $border; background: $inputBg; }
QCheckBox::indicator { border-radius: 5px; }
QRadioButton::indicator { border-radius: 10px; }
QCheckBox::indicator:checked { background: $accent; border-color: $accent; }
QRadioButton::indicator:checked { background: $accent; border-color: $accent; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: $accent; }

/* ── Lists & menus ────────────────────────────────────── */
QListWidget { background: $inputBg; color: $text; border: 1px solid $border;
    border-radius: 11px; padding: 4px; alternate-background-color: $panel2; }
QListWidget::item { padding: 5px 8px; border-radius: 7px; color: $text; background: transparent; }
QListWidget::item:alternate { color: $text; }
QListWidget::item:selected { background: $selBg; color: $text; }
QMenu { background: $panel; color: $text; border: 1px solid $border; border-radius: 11px; padding: 6px; }
QMenu::item { padding: 7px 22px; border-radius: 7px; }
QMenu::item:selected { background: $accent; color: $accentText; }
QMenu::item:disabled { color: $muted; }
QMenu::separator { height: 1px; background: $border; margin: 5px 8px; }
QToolTip { background: $panel2; color: $text; border: 1px solid $border; border-radius: 7px; padding: 5px 8px; }

/* ── Chrome ───────────────────────────────────────────── */
QFrame#topbar { background: $panel; border: none; border-bottom: 1px solid $border; }
QFrame#footer { background: $panel; border: none; border-top: 1px solid $border; }
''')

# ── Mission Control (default) — cosmic navy console, indigo + cyan ──────────
_MISSION_PALETTE = {
    "bg": "#070b16", "panel": "#0f1830", "panel2": "#152144", "hover": "#1c2950",
    "border": "#26345a", "text": "#e8edf8", "muted": "#8694b8",
    "accent": "#6c7cff", "accentHover": "#8a96ff", "accentPress": "#5462e0",
    "accentText": "#ffffff", "inputBg": "#0a1124",
    "green": "#2fd3a4", "greenHover": "#49ddb4", "red": "#ff5a6a", "redHover": "#ff7682",
    "amber": "#f5b14b", "amberHover": "#ffc266", "selBg": "#22306a", "handle": "#33406a",
}

# ── Graphite Studio — pro dark, one blue accent, tight ─────────────────────
_GRAPHITE_PALETTE = {
    "bg": "#15171c", "panel": "#1e2128", "panel2": "#262a33", "hover": "#2e333d",
    "border": "#333845", "text": "#edeff3", "muted": "#99a1b0",
    "accent": "#4f8cff", "accentHover": "#6fa0ff", "accentPress": "#3d77e6",
    "accentText": "#ffffff", "inputBg": "#121419",
    "green": "#3ecf8e", "greenHover": "#52d99c", "red": "#f0566a", "redHover": "#f47082",
    "amber": "#e0a13a", "amberHover": "#eeb152", "selBg": "#243049", "handle": "#39404d",
}

# ── Daylight — light, airy, friendly ───────────────────────────────────────
_DAYLIGHT_PALETTE = {
    "bg": "#eef1f7", "panel": "#ffffff", "panel2": "#f4f6fb", "hover": "#e9edf6",
    "border": "#dfe4ee", "text": "#1b2233", "muted": "#5f6981",
    "accent": "#6366f1", "accentHover": "#5559e8", "accentPress": "#4548c9",
    "accentText": "#ffffff", "inputBg": "#ffffff",
    "green": "#16a34a", "greenHover": "#138a3f", "red": "#e11d48", "redHover": "#c01840",
    "amber": "#d97706", "amberHover": "#c46a05", "selBg": "#e3e4fb", "handle": "#c4c9d6",
}

def _asset_url(name: str) -> str:
    """assets/<name> as a forward-slash URL for use inside QSS url()."""
    return _asset(name).replace("\\", "/")

# ── Cosmic — the purple Macronaut actually wore, made a real theme ─────────
#
# ⚠ For most of this project's life the app was navy (_MISSION_PALETTE, in the
# initial commit) with *purple chrome bolted on*: compact.py carried its own
# hardcoded dict and main.py hardcoded the canvas header, its grip and the
# settings drawer. So the purple was never a theme — it was four copies of a
# palette that no picker could reach and that the light theme could not undo,
# which is why Daylight used to show a dark purple title bar.
#
# Making the chrome follow the theme (28 August) fixed that and removed the last
# of the purple, which is the moment the app stopped looking like itself. This
# palette is the answer: the same family, deepened, as one definition that both
# faces and every piece of chrome read.
_COSMIC_PALETTE = {
    "bg": "#0a0818", "panel": "#15122c", "panel2": "#1d1840", "hover": "#272052",
    "border": "#3a2f7a", "text": "#ece9fb", "muted": "#a79fd4",
    "accent": "#8b5cf6", "accentHover": "#a279f8", "accentPress": "#7548e0",
    "accentText": "#ffffff", "inputBg": "#100d24",
    "green": "#2fd3a4", "greenHover": "#49ddb4", "red": "#ff5a6a", "redHover": "#ff7682",
    "amber": "#f5b14b", "amberHover": "#ffc266", "selBg": "#2e2566", "handle": "#443a86",
}

# Per-theme spinbox arrow glyphs (light on the dark themes, dark on Daylight).
for _pal, _suf in ((_COSMIC_PALETTE, "light"), (_MISSION_PALETTE, "light"),
                   (_GRAPHITE_PALETTE, "light"), (_DAYLIGHT_PALETTE, "dark")):
    _pal["arrowUp"]   = _asset_url(f"arrow_up_{_suf}.png")
    _pal["arrowDown"] = _asset_url(f"arrow_down_{_suf}.png")

COSMIC   = _QSS.substitute(_COSMIC_PALETTE)
MISSION  = _QSS.substitute(_MISSION_PALETTE)
GRAPHITE = _QSS.substitute(_GRAPHITE_PALETTE)
DAYLIGHT = _QSS.substitute(_DAYLIGHT_PALETTE)

# Theme registry. Order = how they appear in the picker. Cosmic is default.
#
# ⚠ The picker builds its segments from THEME_ORDER and reads back by index, so
# a theme's position here is its button id. Adding one at the FRONT renumbers
# every id — which is fine because both ends go through this list, and would be
# a silent mis-selection the moment anything hardcoded an index.
THEME_ORDER = ["cosmic", "mission", "graphite", "daylight"]
THEME_LABELS = {"cosmic": "Cosmic", "mission": "Mission Control",
                "graphite": "Graphite", "daylight": "Daylight"}
THEMES = {"cosmic": COSMIC, "mission": MISSION,
          "graphite": GRAPHITE, "daylight": DAYLIGHT}
DEFAULT_THEME = settings_mod.DEFAULT_THEME

# Track the live theme so theme-aware widgets (e.g. the starfield top bar) can
# paint differently. Updated by MainWindow._apply_theme and at startup.
CURRENT_THEME = DEFAULT_THEME

# The raw colour tokens behind each theme's stylesheet. THEMES holds the
# *substituted QSS strings*, which a custom painter cannot read a colour out
# of — anything that draws itself needs the palette instead.
PALETTES = {"cosmic": _COSMIC_PALETTE, "mission": _MISSION_PALETTE,
            "graphite": _GRAPHITE_PALETTE, "daylight": _DAYLIGHT_PALETTE}


def theme_color(key: str) -> str:
    """One colour token of the live theme, e.g. theme_color("muted")."""
    return PALETTES.get(CURRENT_THEME, _COSMIC_PALETTE).get(key, "#a79fd4")

# Back-compat aliases (older code/tests referenced DARK / LIGHT).
DARK, LIGHT = MISSION, DAYLIGHT


# ═══════════════════════════════════════════════════════════════════════════════
#  Reusable UI building blocks (keep the whole app visually consistent)
# ═══════════════════════════════════════════════════════════════════════════════

def _hint(text: str) -> QLabel:
    """A small muted helper line explaining a feature."""
    lbl = QLabel(text)
    lbl.setObjectName("hint")
    lbl.setWordWrap(True)
    return lbl


class _PaletteButton(QPushButton):
    """Palette button that can also be dragged onto the flow canvas to drop a
    new node at the cursor. A plain click still adds the node (at view center)."""
    NODE_MIME = "application/x-macronaut-node"

    def __init__(self, text: str, ntype: str, parent=None):
        super().__init__(text, parent)
        self._ntype = ntype
        self._press_pos = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_pos = e.pos()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (self._press_pos is not None and
                (e.pos() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            self._press_pos = None
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(self.NODE_MIME, self._ntype.encode())
            drag.setMimeData(mime)
            drag.exec(Qt.CopyAction)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._press_pos = None
        super().mouseReleaseEvent(e)


def _palette_entry_is_pro(emit: str) -> bool:
    """Whether a palette button's `emit` string names a paid feature.

    The palette speaks in "action:<kind>" strings and bare node types, while
    `entitlements` speaks in nodes — so the translation lives here rather than
    duplicating the policy. Deliberately asks `entitlements` both ways instead
    of holding its own list: a fifth Detect kind must not become free simply
    because nobody remembered this function existed.
    """
    if emit.startswith("action:"):
        return emit.split(":", 1)[1] in entitlements.PRO_ACTION_KINDS
    return emit in entitlements.PRO_NODE_TYPES


def _btn(text: str, kind: str = "default", min_h: int = 34, tip: str = "") -> QPushButton:
    """Make a consistently-styled button. kind: default|primary|danger|ghost."""
    b = QPushButton(text)
    name = {"primary": "btnPrimary", "danger": "btnDanger", "ghost": "btnGhost"}.get(kind)
    if name:
        b.setObjectName(name)
    b.setMinimumHeight(min_h)
    _f = b.font(); _f.setLetterSpacing(QFont.AbsoluteSpacing, 0.4); b.setFont(_f)
    b.setCursor(Qt.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    return b


def _card(title: str, description: str = ""):
    """Return (groupbox, vlayout) for a titled card with an optional description."""
    gb = QGroupBox(title)
    gb.setObjectName("card")
    v = QVBoxLayout(gb)
    v.setContentsMargins(18, 20, 18, 16)
    v.setSpacing(12)
    if description:
        v.addWidget(_hint(description))
    return gb, v


def _pane(layout=None) -> QWidget:
    """A layout-only container that does not paint its own background.

    The stylesheet above sets `background: $bg` on *every* QWidget, so a plain
    container placed inside a card paints a near-black rectangle over it. Anything
    that exists only to hold a layout goes through here (or carries the same
    "fieldRow" name, which is what _field() does)."""
    w = QWidget()
    w.setObjectName("fieldRow")
    if layout is not None:
        w.setLayout(layout)
    return w


def _field(label_text: str, *widgets, grow=None) -> QWidget:
    """A label on the left, control(s) on the right — consistent form row.

    `grow` names the one widget that should absorb spare width (a path box, for
    instance). Without it a trailing stretch keeps the controls hugging the
    label, which is what every fixed-width control wants."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    lbl = QLabel(label_text)
    lbl.setMinimumWidth(150)
    row.addWidget(lbl)
    for wd in widgets:
        row.addWidget(wd, 1 if wd is grow else 0)
    if grow is None:
        row.addStretch(1)
    cont = QWidget()
    cont.setObjectName("fieldRow")
    cont.setLayout(row)
    return cont


def _mod_checks(what: str) -> dict:
    """Ctrl / Alt / Shift / Win checkboxes, in flow's canonical order.

    A pointer step can be performed with modifiers held — ctrl-click to add to
    a selection, shift-click to extend one, alt-drag to copy, shift-scroll to
    go sideways. The recorder captures them; this is how you build one by hand.
    """
    boxes = {}
    for m in flow.MOD_ORDER:
        cb = QCheckBox(m.capitalize())
        cb.setToolTip(f"Hold {m.capitalize()} down for the whole {what}, the "
                      f"way you would with your other hand. It goes down "
                      f"before the {what} and comes back up after.")
        boxes[m] = cb
    return boxes


def _segmented(options, group, checked_id: int = 0):
    """A segmented (pill) control backed by `group` (a QButtonGroup). Buttons get
    ids 0..n-1, so existing checkedId()/button(i) logic keeps working."""
    wrap = QWidget(); wrap.setObjectName("segWrap")
    h = QHBoxLayout(wrap); h.setContentsMargins(3, 3, 3, 3); h.setSpacing(2)
    btns = []
    for i, o in enumerate(options):
        b = QPushButton(o); b.setObjectName("seg")
        b.setCheckable(True); b.setCursor(Qt.PointingHandCursor)
        group.addButton(b, i)
        if i == checked_id:
            b.setChecked(True)
        h.addWidget(b); btns.append(b)
    return wrap, btns


class FlowLayout(QLayout):
    """A layout that arranges children left-to-right and wraps to a new line when
    it runs out of width — so button rows never clip in a small/windowed view."""

    def __init__(self, parent=None, margin=0, hspacing=8, vspacing=8):
        super().__init__(parent)
        self._items = []
        self._hspace = hspacing
        self._vspace = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        line_height = 0
        right = rect.right() - m.right()
        for item in self._items:
            hint = item.sizeHint()
            w, h = hint.width(), hint.height()
            if x + w > right and line_height > 0:
                x = rect.x() + m.left()
                y = y + line_height + self._vspace
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = x + w + self._hspace
            line_height = max(line_height, h)
        return y + line_height - rect.y() + m.bottom()


class _WheelGuard(QObject):
    """Makes the wheel scroll the page instead of editing whatever it rolls over.

    ⚠ A combo box, a spin box and a slider all treat a wheel notch as "change my
    value", and Qt sends the wheel to the widget under the cursor rather than to
    the scroll area. On a settings page that is a booby trap: scrolling past the
    controls silently retunes the backend, the typing speed and the key hold
    time, and nothing says so — the reader is looking further down the page.

    The event is forwarded to the enclosing scroll area rather than merely
    swallowed. Returning True from an event filter counts as handled, so the
    event would not bubble up on its own and the page would refuse to scroll at
    all while the pointer sat over a control.

    A focused control keeps its wheel: by then the value *is* what is being
    worked on, and the arrow keys already do the same job.
    """

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.Wheel or obj.hasFocus():
            return False
        area = obj.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if area is not None:
            QApplication.sendEvent(area.viewport(), ev)
        return True


def _guard_wheel(root: QWidget) -> None:
    """Install `_WheelGuard` on every value control under `root`.

    ⚠ `QSlider`, never `QAbstractSlider`: a `QScrollBar` is one of those, so the
    sweep would guard the scroll area's own bar — which then forwards the wheel
    to the viewport that drives it, and the two hand it back and forth until the
    stack runs out. The controls to guard are the ones holding a *setting*.
    """
    guard = _WheelGuard(root)       # parented, so it outlives this call
    for w in root.findChildren(QWidget):
        if isinstance(w, (QComboBox, QAbstractSpinBox, QSlider)):
            # Without this a wheel notch would focus the control on the way past,
            # handing it the next one.
            w.setFocusPolicy(Qt.StrongFocus)
            w.installEventFilter(guard)


def _scroll_page(max_width: int = 0):
    """A full-width scrollable page: content fills the available width (no dead
    space on the sides) and scrolls vertically when the window is short."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    content = QWidget()
    scroll.setWidget(content)
    v = QVBoxLayout(content)
    v.setContentsMargins(20, 18, 22, 20)
    v.setSpacing(14)
    return scroll, v


# ═══════════════════════════════════════════════════════════════════════════════
#  Global hotkey listener
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_hotkey(hk: str) -> str:
    """Pretty-print a hotkey string, e.g. 'ctrl+shift+a' → 'Ctrl+Shift+A'."""
    hk = (hk or "").strip()
    if not hk:
        return ""
    return "+".join(p.strip().capitalize() for p in hk.split("+") if p.strip())


class HotkeyBridge(QObject):
    # Carries the hotkey that fired ("f13", "ctrl+shift+a"). One listener can
    # watch many keys, so the receiver has to be able to tell them apart —
    # that is what routes a launcher key to its own script.
    triggered = Signal(str)


class HotkeyListener:
    """
    A single global keyboard listener that can watch several hotkeys at once.

    Each registered hotkey fires the shared bridge signal when its final key is
    pressed while all its modifiers are held. The OS sends repeated key-press
    events while a key is held down — these are filtered out so holding the key
    fires exactly once per physical press (no rapid start/stop flicker).
    """

    def __init__(self, bridge: HotkeyBridge):
        self._bridge   = bridge
        self._hotkeys: List[str] = []          # normalised "ctrl+shift+a" strings
        self._pressed  = set()                 # keys currently held down
        self._active   = set()                 # hotkeys already fired this hold
        self._lst: Optional[_pk.Listener] = None

    def set_hotkeys(self, hotkeys: List[str]):
        """Replace the watched set. Blank / duplicate entries are ignored."""
        cleaned = []
        for hk in hotkeys:
            hk = (hk or "").lower().strip()
            if hk and hk not in cleaned:
                cleaned.append(hk)
        self._hotkeys = cleaned
        self.restart()

    def start(self):
        self.stop()
        self._pressed.clear()
        self._active.clear()
        self._lst = _pk.Listener(on_press=self._on_press,
                                  on_release=self._on_release)
        self._lst.daemon = True
        self._lst.start()

    def restart(self):
        self.start()

    def stop(self):
        if self._lst:
            self._lst.stop()
            self._lst = None

    # pynput reports left/right modifier variants; captured hotkeys use the base
    # name, so fold the variants together for reliable combo matching.
    _MOD_ALIASES = {
        "ctrl_l": "ctrl", "ctrl_r": "ctrl",
        "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
        "shift_l": "shift", "shift_r": "shift",
        "cmd": "win", "cmd_l": "win", "cmd_r": "win",
    }

    @classmethod
    def _norm(cls, key) -> str:
        try:
            if hasattr(key, "char") and key.char:
                return key.char.lower()
        except Exception:
            pass
        name = str(key).replace("Key.", "").lower()
        return cls._MOD_ALIASES.get(name, name)

    def _on_press(self, key):
        k = self._norm(key)
        # Ignore OS auto-repeat: only react to the initial press transition.
        if k in self._pressed:
            return
        self._pressed.add(k)
        for hk in self._hotkeys:
            parts  = [p.strip() for p in hk.split("+")]
            target = parts[-1]
            mods   = set(parts[:-1])
            if k == target and mods.issubset(self._pressed) and hk not in self._active:
                self._active.add(hk)
                self._bridge.triggered.emit(hk)

    def _on_release(self, key):
        k = self._norm(key)
        self._pressed.discard(k)
        # Re-arm any hotkey whose final key was just released.
        for hk in list(self._active):
            if hk.split("+")[-1].strip() == k:
                self._active.discard(hk)


# ═══════════════════════════════════════════════════════════════════════════════
#  Region selector overlay
# ═══════════════════════════════════════════════════════════════════════════════

class RegionSelector(QWidget):
    """Full-screen overlay for dragging a bounding box."""

    region_selected = Signal(int, int, int, int)   # x, y, w, h

    def __init__(self):
        super().__init__(None,
                         Qt.Window | Qt.FramelessWindowHint |
                         Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)
        # Kept in global (virtual-desktop) coordinates — see below.
        self._start: Optional[QPoint] = None
        self._end:   Optional[QPoint] = None
        # Span the whole virtual desktop, exactly as ScreenshotSelector does.
        # primaryScreen().geometry() covers only the primary monitor, so the
        # overlay never appears on any other screen and a region there simply
        # cannot be drawn. It looked correct on a single-monitor machine because
        # the primary screen starts at (0,0), which makes local and global
        # coordinates identical — and a monitor placed left of the primary has
        # negative x, where that assumption breaks completely.
        # show(), not showFullScreen(): showFullScreen() overrides the geometry
        # and locks the window to whichever single screen it lands on.
        geo = QApplication.primaryScreen().virtualGeometry()
        self.setGeometry(geo)
        self.show()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))

        if self._start and self._end:
            # Selection is global; painting is local.
            tl = self.mapFromGlobal(QPoint(
                min(self._start.x(), self._end.x()),
                min(self._start.y(), self._end.y())))
            x, y = tl.x(), tl.y()
            w = abs(self._end.x() - self._start.x())
            h = abs(self._end.y() - self._start.y())
            rect = QRect(x, y, w, h)
            # Punch clear hole
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(rect, Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            # Blue border
            p.setPen(QPen(QColor("#89b4fa"), 2))
            p.drawRect(rect)
            # Dimension label
            p.setPen(QColor("white"))
            font = p.font(); font.setBold(True); font.setPointSize(11)
            p.setFont(font)
            p.drawText(x + 5, max(y - 6, 14), f"{w} × {h}")
        else:
            # Instructions
            p.setPen(QColor("white"))
            f = p.font(); f.setPointSize(16); f.setBold(True)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Drag to select region\n\nPress Esc to cancel")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start = e.globalPos()
            self._end   = e.globalPos()

    def mouseMoveEvent(self, e):
        if self._start:
            self._end = e.globalPos()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._start:
            self._end = e.globalPos()
            x = min(self._start.x(), self._end.x())
            y = min(self._start.y(), self._end.y())
            w = abs(self._end.x() - self._start.x())
            h = abs(self._end.y() - self._start.y())
            if w > 10 and h > 10:
                # Global coordinates: what the clicker clamps against.
                self.region_selected.emit(x, y, w, h)
            self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  Key-capture dialog
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
#  Screenshot selector overlay
# ═══════════════════════════════════════════════════════════════════════════════

class ScreenshotSelector(QWidget):
    """
    Full-screen overlay for selecting a screen region to capture.
    Emits region_selected BEFORE closing so the caller can schedule the
    actual grab after the overlay has fully disappeared.
    """

    region_selected = Signal(int, int, int, int)   # x, y, w, h
    cancelled       = Signal()

    def __init__(self):
        # No Qt.Tool — tool windows are hidden when the app minimises,
        # which would kill the overlay immediately.
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint |
                         Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)
        # Store selection in global (virtual-desktop) coordinates so that
        # selections on any monitor map correctly when all_screens=True.
        self._start: Optional[QPoint] = None
        self._end:   Optional[QPoint] = None
        # Span the full virtual desktop so all monitors are covered.
        # Use show() not showFullScreen() — showFullScreen() overrides the
        # geometry and locks the window to whichever single screen it lands on.
        # Qt6 removed QApplication.desktop(). virtualGeometry() is the correct
        # replacement here, NOT primaryScreen().geometry() — this overlay has to
        # span every monitor, and a secondary monitor can sit at negative x.
        geo = QApplication.primaryScreen().virtualGeometry()
        self.setGeometry(geo)
        # Application-modal so this overlay receives input even when launched
        # from a modal dialog (which holds Qt's input grab). Must be set before
        # show() — the window is already realised once shown.
        self.setWindowModality(Qt.ApplicationModal)
        self.show()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))

        if self._start and self._end:
            # Convert global coords to local widget coords for drawing
            tl = self.mapFromGlobal(QPoint(
                min(self._start.x(), self._end.x()),
                min(self._start.y(), self._end.y())))
            x, y = tl.x(), tl.y()
            w = abs(self._end.x() - self._start.x())
            h = abs(self._end.y() - self._start.y())
            rect = QRect(x, y, w, h)
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(rect, Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.setPen(QPen(QColor("#f9e2af"), 2))
            p.drawRect(rect)
            p.setPen(QColor("white"))
            f = p.font(); f.setBold(True); f.setPointSize(10); p.setFont(f)
            p.drawText(x + 5, max(y - 6, 14), f"{w} × {h}")
        else:
            p.setPen(QColor("white"))
            f = p.font(); f.setPointSize(15); f.setBold(True); p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Drag to select the region to capture\n\nPress Esc to cancel")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start = e.globalPos()
            self._end   = e.globalPos()

    def mouseMoveEvent(self, e):
        if self._start:
            self._end = e.globalPos()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._start:
            self._end = e.globalPos()
            x = min(self._start.x(), self._end.x())
            y = min(self._start.y(), self._end.y())
            w = abs(self._end.x() - self._start.x())
            h = abs(self._end.y() - self._start.y())
            if w > 8 and h > 8:
                # Emit first, then close — caller must delay the grab
                self.region_selected.emit(x, y, w, h)
            else:
                self.cancelled.emit()
            self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()


def _grab_region(x: int, y: int, w: int, h: int) -> Optional[str]:
    """
    Capture a screen region (virtual-desktop coordinates) and save to
    ~/.macronaut/captures/.  all_screens=True makes Pillow use the
    full virtual desktop as the coordinate origin, matching Qt's coordinates.
    """
    try:
        from PIL import ImageGrab
        # Qt widget coordinates are already in virtual-desktop space (can be
        # negative on multi-monitor setups where monitor 2 is to the left).
        img  = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        dest = data_dir() / "captures"
        dest.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:21]
        path = dest / f"capture_{ts}.png"
        img.save(str(path))
        return str(path)
    except Exception as exc:
        QMessageBox.critical(None, "Capture failed", str(exc))
        return None


class KeyCaptureDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Capture Hotkey")
        self.captured = ""
        self.setModal(True)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Press the desired key or key combination:"))

        self._lbl = QLabel("—")
        self._lbl.setAlignment(Qt.AlignCenter)
        f = self._lbl.font(); f.setPointSize(18); f.setBold(True)
        self._lbl.setFont(f)
        self._lbl.setMinimumHeight(60)
        lay.addWidget(self._lbl)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.setMinimumWidth(320)

    def keyPressEvent(self, e):
        key = e.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.accept(); return
        if key == Qt.Key_Escape:
            self.reject(); return

        mods = e.modifiers()
        parts = []
        if mods & Qt.ControlModifier: parts.append("ctrl")
        if mods & Qt.AltModifier:     parts.append("alt")
        if mods & Qt.ShiftModifier:   parts.append("shift")
        if mods & Qt.MetaModifier:    parts.append("win")

        seq = QKeySequence(key).toString().lower()
        # Filter out lone modifier keys
        if seq and seq not in ("ctrl", "alt", "shift", "meta", "super"):
            parts.append(seq)

        if parts:
            self.captured = "+".join(parts)
            self._lbl.setText(self.captured.upper())


# ═══════════════════════════════════════════════════════════════════════════════
#  Click-point picker widget
# ═══════════════════════════════════════════════════════════════════════════════

class ClickPointPicker(QWidget):
    """
    Shows an image and lets the user click to place a target point.
    The point is stored as (offset_x, offset_y) from the image center.
    """
    point_changed = Signal(int, int)   # offset_x, offset_y from center

    _W, _H = 240, 150   # display size

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.CrossCursor)
        self.setToolTip("Click to set the target point")
        self._pixmap: Optional[QPixmap] = None
        self._px: Optional[int] = None   # click position in widget coords
        self._py: Optional[int] = None
        self._img_w = 1
        self._img_h = 1

    def load_image(self, path: str):
        if path and Path(path).is_file():
            px = QPixmap(path)
            if not px.isNull():
                self._img_w = px.width()
                self._img_h = px.height()
                self._pixmap = px.scaled(self._W, self._H,
                                         Qt.KeepAspectRatio, Qt.SmoothTransformation)
                # Reset point to center when image changes
                self._px = self._W // 2
                self._py = self._H // 2
                self.update()
                self.point_changed.emit(0, 0)
                return
        self._pixmap = None
        self._px = self._py = None
        self.update()

    def set_offset(self, ox: int, oy: int):
        """Restore a saved offset (e.g. when editing an existing step)."""
        if self._pixmap is None:
            return
        pw = self._pixmap.width()
        ph = self._pixmap.height()
        scale_x = pw / self._img_w
        scale_y = ph / self._img_h
        margin_x = (self._W - pw) // 2
        margin_y = (self._H - ph) // 2
        self._px = int(margin_x + pw // 2 + ox * scale_x)
        self._py = int(margin_y + ph // 2 + oy * scale_y)
        self.update()

    def offset(self):
        """Return (offset_x, offset_y) from center in image pixels."""
        if self._pixmap is None or self._px is None:
            return 0, 0
        pw = self._pixmap.width()
        ph = self._pixmap.height()
        margin_x = (self._W - pw) // 2
        margin_y = (self._H - ph) // 2
        scale_x = self._img_w / pw if pw else 1
        scale_y = self._img_h / ph if ph else 1
        ox = int((self._px - margin_x - pw // 2) * scale_x)
        oy = int((self._py - margin_y - ph // 2) * scale_y)
        return ox, oy

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._px = e.x()
            self._py = e.y()
            ox, oy = self.offset()
            self.point_changed.emit(ox, oy)
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#181825"))
        p.setPen(QPen(QColor("#45475a"), 1))
        p.drawRect(0, 0, self._W - 1, self._H - 1)

        if self._pixmap:
            mx = (self._W - self._pixmap.width())  // 2
            my = (self._H - self._pixmap.height()) // 2
            p.drawPixmap(mx, my, self._pixmap)

        if self._px is not None:
            x, y = self._px, self._py
            p.setPen(QPen(QColor("#f38ba8"), 1))
            p.drawLine(x - 10, y, x + 10, y)
            p.drawLine(x, y - 10, x, y + 10)
            p.setPen(QPen(QColor("#f38ba8"), 2))
            p.drawEllipse(x - 4, y - 4, 8, 8)
        else:
            p.setPen(QColor("#585b70"))
            f = p.font(); f.setPointSize(9); p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, "Load an image first")


# ═══════════════════════════════════════════════════════════════════════════════
#  Add / Edit sequence step dialog
# ═══════════════════════════════════════════════════════════════════════════════

class StepDialog(QDialog):
    """Dialog for adding or editing a SeqStep."""

    MAX_WIDTH = 620   # see _refit()

    def __init__(self, step: Optional[SeqStep] = None, parent=None,
                 default_text_cps: float = 10.0, family: str = None):
        super().__init__(parent)
        self.setWindowTitle("Add Step" if step is None else "Edit Step")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._result_step: Optional[SeqStep] = None
        self._default_text_cps = default_text_cps

        lay = QVBoxLayout(self)

        # Step type selector (hidden when the dialog is scoped to one family)
        self._family = family
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Type:"))
        self._type_combo = QComboBox()
        # The three Detect kinds are named for WHAT they watch, not for the
        # waiting — the waiting is the whole family's job, so "Wait for" was on
        # every one of them and told you nothing.
        # Scroll and Drag are appended rather than filed next to Click, because
        # every index in here is a stored position that _on_ok/_load already
        # speak. The segmented toggle below puts them in a sensible order.
        # ⚠ Appended, like Scroll and Drag before it: every index in here is a
        # stored position that _on_ok/_load already speak, so inserting Move
        # next to Click would renumber four kinds.
        self._type_combo.addItems(["Click", "Key / Combo", "Type Text", "Wait",
                                   "Image", "Text", "Pixel", "Scroll", "Drag",
                                   "Move"])
        self._type_combo.currentIndexChanged.connect(self._on_type_change)
        type_row.addWidget(self._type_combo)
        self._type_row_w = _pane(type_row)
        lay.addWidget(self._type_row_w)

        # Opened from a palette family -> replace the full combo with either
        # nothing (Click/Wait) or a two-way segmented toggle (Type / Detect).
        self._fam_group = None
        self._fam_indices = None
        _FAM = {
            # Press, drag and turn are the mouse's three verbs, so they live
            # behind the one Click button rather than taking a palette button
            # each — the palette is eight buttons and three of them for one
            # device would be a worse trade than one toggle inside the editor.
            # Ordered press → drag → turn, which is neither the combo order nor
            # the order they were built in; _fam_indices maps segment to index.
            "click":  ([0, 9, 8, 7], ["Click", "Move", "Drag", "Scroll"],
                       "Click"),
            "wait":   ([3], None, "Wait"),
            "type":   ([1, 2], ["Key / combo", "Type text"], "Type"),
            "detect": ([4, 5, 6], ["Image", "Text", "Pixel"], "Detect"),
        }
        if family in _FAM:
            indices, seg_labels, fam_title = _FAM[family]
            self.setWindowTitle(("Add " if step is None else "Edit ") + fam_title)
            self._type_row_w.setVisible(False)
            self._fam_indices = indices
            if seg_labels:
                self._fam_group = QButtonGroup(self)
                seg_wrap, _segbtns = _segmented(seg_labels, self._fam_group, 0)
                # idClicked(int), NOT buttonClicked[int]: the indexed overload of
                # buttonClicked was PyQt5-only and Qt6 removed it, so the old
                # spelling raises IndexError while *building the dialog* — the
                # Type and Detect palette buttons opened nothing at all and left
                # an unconfigured (therefore Click-looking) node on the canvas.
                # _segmented() registers the buttons with ids 0..n-1, so the id
                # is already the index into _fam_indices.
                self._fam_group.idClicked.connect(
                    lambda i: self._type_combo.setCurrentIndex(self._fam_indices[i]))
                lay.addWidget(seg_wrap)
            # Set the scoped default without firing _on_type_change before the
            # stacked panels below exist; the explicit call at __init__ end shows it.
            self._type_combo.blockSignals(True)
            self._type_combo.setCurrentIndex(indices[0])
            self._type_combo.blockSignals(False)

        # "Delay before" is built inside the Click panel — it is a Click-only
        # setting (every other kind carries its delay on the node itself), and a
        # loose row floating above the cards never lined up with them.
        # Stacked param areas
        self._stack_click  = self._build_click_panel()
        self._stack_key    = self._build_key_panel()
        self._stack_text   = self._build_text_panel()
        self._stack_wait   = self._build_wait_panel()
        self._stack_imgwait = self._build_imgwait_panel()
        self._stack_textwait = self._build_textwait_panel()
        self._stack_pixwait = self._build_pixwait_panel()
        self._stack_scroll = self._build_scroll_panel()
        self._stack_drag = self._build_drag_panel()
        self._stack_move = self._build_move_panel()

        for w in (self._stack_click, self._stack_key,
                  self._stack_text, self._stack_wait,
                  self._stack_imgwait, self._stack_textwait,
                  self._stack_pixwait, self._stack_scroll,
                  self._stack_drag, self._stack_move):
            lay.addWidget(w)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        # Kept so _fit_to_screen() can pin it below the scroll area — the one
        # row that must never be the one scrolled out of reach.
        self._btns = btns
        self._scrolled = False

        self._on_type_change(self._type_combo.currentIndex())
        # Same trap as the settings page: this dialog scrolls, and a wheel notch
        # over a spin box is "change the timeout" rather than "scroll down".
        _guard_wheel(self)

        if step is not None:
            self._load(step)
        self._sync_family_seg()

    # ── Panel builders ────────────────────────────────────────────────
    # From flow, so the dialog and the engine cannot disagree about what a
    # button is called. Stored by NAME in the step (see the build path), so
    # extending this list does not renumber anything already saved.
    CLICK_BUTTONS = list(flow.BUTTONS)
    CLICK_BUTTON_LABELS = [flow.button_label(b) for b in flow.BUTTONS]

    def _build_click_panel(self) -> QWidget:
        """Where, then how — the two questions a click actually asks.

        Was a seven-row grid of ragged columns with Double-click and Hold as two
        checkboxes that silently unticked each other. They are one choice, so
        they are one segmented control now, matching the Basic face.
        """
        w = _pane()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        where, wl = _card("Where", "Desktop coordinates, so a second monitor "
                                   "works the same as the first.")
        self._click_x = _spin(-32000, 32000, w=96)
        self._click_x.setToolTip("Horizontal position, in desktop pixels")
        self._click_y = _spin(-32000, 32000, w=96)
        self._click_y.setToolTip("Vertical position, in desktop pixels")
        self._btn_pick_step = QPushButton("⊕  Pick position (3 s)")
        self._btn_pick_step.setToolTip(
            "Move your cursor to the target and wait — position is captured after 3 seconds")
        self._btn_pick_step.clicked.connect(self._start_pick)
        wl.addWidget(_field("Position", self._click_x, self._click_y,
                            self._btn_pick_step))
        v.addWidget(where)

        how, hl = _card("How", "Which button, and what kind of press.")
        self._click_btn_grp = QButtonGroup(self)
        seg_btn, _ = _segmented(self.CLICK_BUTTON_LABELS, self._click_btn_grp, 0)
        seg_btn.setToolTip(
            "Back and Forward are the two side buttons — Mouse 4 and Mouse 5 "
            "if you game with them. They were recorded as left clicks until "
            "8 September 2026.")
        hl.addWidget(_field("Button", seg_btn))

        self._click_mode_grp = QButtonGroup(self)
        seg_mode, _ = _segmented(["Single", "Double", "Hold"], self._click_mode_grp, 0)
        self._click_mode_grp.idClicked.connect(self._on_click_mode)
        hl.addWidget(_field("Press", seg_mode))

        self._click_hold_ms = _durspin(50, 600000, 1000, w=130)
        self._click_hold_row = _field("Hold for", self._click_hold_ms)
        self._click_hold_row.setVisible(False)
        hl.addWidget(self._click_hold_row)

        self._click_mods = _mod_checks("click")
        hl.addWidget(_field("Holding", *self._click_mods.values()))

        self._delay = _durspin(0, 60000, 0, w=130)
        self._delay.setToolTip("Pause before this click. Every other step kind "
                               "carries its delay on the node instead.")
        hl.addWidget(_field("Delay before", self._delay))
        v.addWidget(how)

        self._pick_timer_step: Optional[QTimer] = None
        self._pick_countdown_step = 0
        return w

    # Directions, in the order _segmented() gives them ids. Stored as flow's own
    # constants so a saved step never carries a button index.
    SCROLL_DIRS = (flow.SCROLL_UP, flow.SCROLL_DOWN,
                   flow.SCROLL_LEFT, flow.SCROLL_RIGHT)
    SCROLL_AT_CURSOR, SCROLL_AT_POS = 0, 1

    def _build_move_panel(self) -> QWidget:
        """Where to put the pointer, and how long to wait first.

        The thinnest panel in the dialog, and deliberately: a Move is a Click
        with the click taken off. It exists as its own kind because the
        recorder produces one for every hover — a pause over a menu that opens
        on mouse-enter is a real step, and replaying the click without it
        clicks a menu that never opened.

        ⚠ Its own delay spin rather than the Click panel's `self._delay`. That
        row is built inside the Click card, and reading it here would let a
        value typed there ride along invisibly after switching the family
        toggle — the same trap Scroll and Drag each document.
        """
        w = _pane()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        where, wl = _card("Where", "Desktop coordinates, so a second monitor "
                                   "works the same as the first.")
        self._move_x = _spin(-32000, 32000, w=96)
        self._move_x.setToolTip("Horizontal position, in desktop pixels")
        self._move_y = _spin(-32000, 32000, w=96)
        self._move_y.setToolTip("Vertical position, in desktop pixels")
        self._btn_pick_move = QPushButton("⊕  Pick position (3 s)")
        self._btn_pick_move.setToolTip(
            "Move your cursor to the target and wait — position is captured "
            "after 3 seconds")
        self._btn_pick_move.clicked.connect(
            lambda: self._begin_pick(self._btn_pick_move,
                                     self._move_x, self._move_y))
        wl.addWidget(_field("Position", self._move_x, self._move_y,
                            self._btn_pick_move))
        v.addWidget(where)

        how, hl = _card("How long", "The pause before the pointer sets off. A "
                                    "recorded hover keeps the dwell that made "
                                    "it a hover.")
        self._move_delay = _durspin(0, 60000, 0, w=130)
        self._move_delay.setToolTip("Pause before this move.")
        hl.addWidget(_field("Delay before", self._move_delay))
        v.addWidget(how)
        return w

    def _build_scroll_panel(self) -> QWidget:
        """Where, then how — the same two questions the Click panel asks, in the
        same two cards, because it is the same device.

        "Where" is a choice here and not two coordinate boxes, unlike Click: the
        wheel acts on whatever sits under the pointer, and the common case is
        scrolling the thing you just clicked. Making people re-type a position
        they already have the cursor on is the papercut, and defaulting to
        (0, 0) would silently scroll the corner of the screen.
        """
        w = _pane()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        where, wl = _card("Where", "The wheel turns whatever the pointer is "
                                   "over, so this is about the pointer.")
        self._scroll_where_grp = QButtonGroup(self)
        seg_where, _ = _segmented(["Where the cursor is", "At a position"],
                                  self._scroll_where_grp, 0)
        self._scroll_where_grp.idClicked.connect(self._on_scroll_where)
        wl.addWidget(_field("Point at", seg_where))

        self._scroll_x = _spin(-32000, 32000, w=96)
        self._scroll_y = _spin(-32000, 32000, w=96)
        self._btn_pick_scroll = QPushButton("⊕  Pick position (3 s)")
        self._btn_pick_scroll.setToolTip(
            "Move your cursor to the target and wait — position is captured "
            "after 3 seconds")
        self._btn_pick_scroll.clicked.connect(self._start_pick_scroll)
        self._scroll_pos_row = _field("Position", self._scroll_x, self._scroll_y,
                                      self._btn_pick_scroll)
        self._scroll_pos_row.setVisible(False)
        wl.addWidget(self._scroll_pos_row)
        v.addWidget(where)

        how, hl = _card("How much", "Notches, the way a real wheel counts. How "
                                    "far one moves the page is the app's own "
                                    "business.")
        self._scroll_dir_grp = QButtonGroup(self)
        seg_dir, _ = _segmented(["↑ Up", "↓ Down", "← Left", "→ Right"],
                                self._scroll_dir_grp, 1)
        hl.addWidget(_field("Direction", seg_dir))

        self._scroll_amount = _spin(1, 999, 3, w=96)
        self._scroll_amount.setToolTip(
            "How many notches to turn. One notch is one detent of a real wheel "
            "— Windows' default is three lines of text.")
        hl.addWidget(_field("Notches", self._scroll_amount))

        # No fixed width: _spin's `w` is a *fixed* width, and the special value
        # text is far longer than any number in the range — pinning it would
        # clip the one string that has to be readable to be believed.
        # ⚠ A DOUBLE spin, since 8 September 2026. The recorder measures a
        # spin's real rate now — 33.06 notches/s for a flick, 6.67 for a
        # slow scroll — and an integer box silently rounded that on the
        # first Open-then-OK. Trivial for a flick, 10% for a slow one, and
        # a loss of something that was measured rather than typed. Same
        # shape as the Type-text rate beside it, which is also a rate.
        self._scroll_speed = _dspin(0, float(flow.MAX_SCROLL_CPS), 0.0,
                                    step=1.0, suffix=" notches/s")
        self._scroll_speed.setDecimals(1)
        self._scroll_speed.setSpecialValueText("as fast as possible")
        self._scroll_speed.setToolTip(
            "Notches per second. 0 sends them as fast as the backend will "
            "take them, which is right for a short flick. Slow it down when a "
            "list scrolls smoothly or lazily loads as you go — a receiver that "
            "reads input once a frame only takes so much per pass.")
        hl.addWidget(_field("Speed", self._scroll_speed))

        self._scroll_mods = _mod_checks("scroll")
        hl.addWidget(_field("Holding", *self._scroll_mods.values()))
        v.addWidget(how)

        self._pick_timer_scroll: Optional[QTimer] = None
        self._pick_countdown_scroll = 0
        return w

    def _on_scroll_where(self, mode: int):
        self._scroll_pos_row.setVisible(mode == self.SCROLL_AT_POS)
        self._refit()

    def _start_pick_scroll(self):
        self._pick_countdown_scroll = 3
        self._btn_pick_scroll.setEnabled(False)
        self._btn_pick_scroll.setText(f"Capturing in {self._pick_countdown_scroll}s…")
        self._pick_timer_scroll = QTimer(self)
        self._pick_timer_scroll.timeout.connect(self._pick_tick_scroll)
        self._pick_timer_scroll.start(1000)

    def _pick_tick_scroll(self):
        self._pick_countdown_scroll -= 1
        if self._pick_countdown_scroll <= 0:
            self._pick_timer_scroll.stop()
            pos = QCursor.pos()
            self._scroll_x.setValue(pos.x())
            self._scroll_y.setValue(pos.y())
            self._btn_pick_scroll.setText("⊕  Pick position (3 s)")
            self._btn_pick_scroll.setEnabled(True)
        else:
            self._btn_pick_scroll.setText(
                f"Capturing in {self._pick_countdown_scroll}s…")

    def _build_drag_panel(self) -> QWidget:
        """From, to, and how long the journey takes.

        Two positions rather than one is the whole difference between this and
        a Click, so "Where" is two rows with a picker each. Duration is in the
        "How" card beside the button because it is not a delay — it is the
        gesture's own speed, and a receiver measuring a swipe is measuring
        exactly that.
        """
        w = _pane()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        where, wl = _card("Where", "Press at the first point, let go at the "
                                   "second. Desktop coordinates, like a click.")
        self._drag_x = _spin(-32000, 32000, w=96)
        self._drag_y = _spin(-32000, 32000, w=96)
        self._btn_pick_drag_from = QPushButton("⊕  Pick position (3 s)")
        self._btn_pick_drag_from.setToolTip(
            "Move your cursor to where the drag starts and wait — position is "
            "captured after 3 seconds")
        self._btn_pick_drag_from.clicked.connect(
            lambda: self._begin_pick(self._btn_pick_drag_from,
                                     self._drag_x, self._drag_y))
        wl.addWidget(_field("From", self._drag_x, self._drag_y,
                            self._btn_pick_drag_from))

        self._drag_to_x = _spin(-32000, 32000, w=96)
        self._drag_to_y = _spin(-32000, 32000, w=96)
        self._btn_pick_drag_to = QPushButton("⊕  Pick position (3 s)")
        self._btn_pick_drag_to.setToolTip(
            "Move your cursor to where the drag ends and wait — position is "
            "captured after 3 seconds")
        self._btn_pick_drag_to.clicked.connect(
            lambda: self._begin_pick(self._btn_pick_drag_to,
                                     self._drag_to_x, self._drag_to_y))
        wl.addWidget(_field("To", self._drag_to_x, self._drag_to_y,
                            self._btn_pick_drag_to))
        v.addWidget(where)

        how, hl = _card("How", "Which button is held, and how long the pointer "
                               "takes to travel.")
        self._drag_btn_grp = QButtonGroup(self)
        seg_btn, _ = _segmented(self.CLICK_BUTTON_LABELS, self._drag_btn_grp, 0)
        hl.addWidget(_field("Button", seg_btn))

        self._drag_ms = _durspin(0, flow.MAX_DRAG_MS, flow.DEFAULT_DRAG_MS, w=130)
        self._drag_ms.setToolTip(
            "How long the pointer spends travelling. This is the gesture's own "
            "speed, not a pause: a control that measures a swipe is measuring "
            "this number, and one that only checks the pointer moved will take "
            "any value. Macronaut sends one move per frame for the whole "
            "duration, so the target sees a path rather than a jump.")
        hl.addWidget(_field("Travel time", self._drag_ms))

        self._drag_mods = _mod_checks("drag")
        hl.addWidget(_field("Holding", *self._drag_mods.values()))
        v.addWidget(how)
        return w

    def _begin_pick(self, button: QPushButton, sx, sy):
        """3-2-1 grab of the cursor position into two spin boxes.

        One helper for however many pickers a panel has: the drag panel alone
        would otherwise have been the third and fourth copy of the same
        countdown. The timer is parented to the button so two pickers in one
        dialog cannot overwrite each other's state — which is exactly what a
        shared `self._pick_timer` would have done here.
        """
        label = button.text()
        state = {"n": 3}
        timer = QTimer(button)

        def tick():
            state["n"] -= 1
            if state["n"] > 0:
                button.setText(f"Capturing in {state['n']}s…")
                return
            timer.stop()
            pos = QCursor.pos()
            sx.setValue(pos.x())
            sy.setValue(pos.y())
            button.setText(label)
            button.setEnabled(True)

        button.setEnabled(False)
        button.setText(f"Capturing in {state['n']}s…")
        timer.timeout.connect(tick)
        timer.start(1000)

    # Press modes, in the order _segmented() gave them ids.
    CLICK_SINGLE, CLICK_DOUBLE, CLICK_HOLD = 0, 1, 2

    def _on_click_mode(self, mode: int):
        self._click_hold_row.setVisible(mode == self.CLICK_HOLD)
        self._refit()

    def _click_mode(self) -> int:
        return max(0, self._click_mode_grp.checkedId())

    def _start_pick(self):
        self._pick_countdown_step = 3
        self._btn_pick_step.setEnabled(False)
        self._btn_pick_step.setText(f"Capturing in {self._pick_countdown_step}s…")
        self._pick_timer_step = QTimer(self)
        self._pick_timer_step.timeout.connect(self._pick_tick_step)
        self._pick_timer_step.start(1000)

    def _pick_tick_step(self):
        self._pick_countdown_step -= 1
        if self._pick_countdown_step <= 0:
            self._pick_timer_step.stop()
            pos = QCursor.pos()
            self._click_x.setValue(pos.x())
            self._click_y.setValue(pos.y())
            self._btn_pick_step.setText("⊕ Pick Position (3 s)")
            self._btn_pick_step.setEnabled(True)
        else:
            self._btn_pick_step.setText(f"Capturing in {self._pick_countdown_step}s…")

    # Mode order must match KEY_MODE_ITEMS below and is stored as flow's own
    # constants, so a saved flow never carries a combo-box index.
    KEY_MODES = (flow.KEY_TAP, flow.KEY_HOLD, flow.KEY_DOWN, flow.KEY_UP)
    # Segment order for the Type panel's "Send as", stored as flow's constants
    # for the same reason: a saved flow must never carry a button index.
    SEND_MODES = (flow.SEND_AUTO, flow.SEND_CHARS, flow.SEND_KEYS)
    KEY_MODE_ITEMS = (
        ("Tap — press and release",
         "A quick press. Held just long enough (Settings → Key hold time) for a "
         "game that polls once a frame to see it at all."),
        ("Hold for a set time",
         "Presses, waits, releases — all inside this node. The script does not "
         "move on until the hold is over."),
        ("Hold down — keep it pressed",
         "Presses and moves straight on, so the nodes after this one run with "
         "the key still down. Take it back up with a Release node; anything "
         "still held is freed automatically when the run ends or you press Stop."),
        ("Release — take keys back up",
         "Releases the captured keys. Capture nothing and it releases "
         "everything a Hold down left pressed."),
    )

    def _build_key_panel(self) -> QWidget:
        w = _pane()
        g = QGridLayout(w)

        self._captured_keys: List[str] = []
        self._capturing_key = False
        self._chord: List[str] = []
        self._chord_down: set = set()

        g.addWidget(QLabel("Keys:"), 0, 0)
        self._key_capture_btn = QPushButton("Click here, then press a key…")
        self._key_capture_btn.setObjectName("btnPrimary")
        self._key_capture_btn.setMinimumHeight(36)
        self._key_capture_btn.setCheckable(True)
        self._key_capture_btn.clicked.connect(self._start_key_capture)
        g.addWidget(self._key_capture_btn, 0, 1)

        g.addWidget(_hint("Press one or more keys together — W+A to strafe, or Ctrl+C. "
                          "Capture ends when you let go. Esc cancels."), 1, 0, 1, 2)

        g.addWidget(QLabel("Action:"), 2, 0)
        self._key_mode = QComboBox()
        self._key_mode.addItems([t for t, _h in self.KEY_MODE_ITEMS])
        g.addWidget(self._key_mode, 2, 1)

        self._key_mode_hint = _hint("")
        g.addWidget(self._key_mode_hint, 3, 0, 1, 2)

        self._key_hold_row = _pane()
        _hr3 = QHBoxLayout(self._key_hold_row); _hr3.setContentsMargins(0, 0, 0, 0)
        _hr3.addWidget(QLabel("Hold for:"))
        self._key_hold_ms = _durspin(50, 600000, 1000, w=120)
        _hr3.addWidget(self._key_hold_ms); _hr3.addStretch(1)
        g.addWidget(self._key_hold_row, 4, 0, 1, 3)

        self._key_repeat_row = _pane()
        _hr4 = QHBoxLayout(self._key_repeat_row); _hr4.setContentsMargins(0, 0, 0, 0)
        _hr4.addWidget(QLabel("Repeat:"))
        self._key_repeat = _spin(1, 10000, 1, w=100)
        _hr4.addWidget(self._key_repeat); _hr4.addStretch(1)
        g.addWidget(self._key_repeat_row, 5, 0, 1, 3)

        self._key_mode.currentIndexChanged.connect(self._on_key_mode)
        self._on_key_mode(0)
        return w

    def _on_key_mode(self, idx: int):
        mode = self.KEY_MODES[max(0, min(idx, len(self.KEY_MODES) - 1))]
        self._key_mode_hint.setText(self.KEY_MODE_ITEMS[
            max(0, min(idx, len(self.KEY_MODE_ITEMS) - 1))][1])
        self._key_hold_row.setVisible(mode == flow.KEY_HOLD)
        # Repeating a Hold down would press a key that is already down, and
        # repeating a Release would release one that is already up. Neither is
        # a thing to offer.
        self._key_repeat_row.setVisible(mode in (flow.KEY_TAP, flow.KEY_HOLD))
        self._refit()

    # ── chord capture ─────────────────────────────────────────────────
    # Modifiers first, then the rest in the order they were pressed. The engine
    # presses keys[:-1] before keys[-1], so Ctrl+C still means "ctrl, then c" —
    # while W+A, which has no modifier, keeps the order the fingers used.
    _MOD_ORDER = ("ctrl", "alt", "shift", "win")
    _MOD_KEYS = {Qt.Key_Control: "ctrl", Qt.Key_Alt: "alt",
                 Qt.Key_Shift: "shift", Qt.Key_Meta: "win"}

    @classmethod
    def _key_name(cls, qtkey) -> str:
        """A key's stored name, or '' for something unnameable."""
        if qtkey in cls._MOD_KEYS:
            return cls._MOD_KEYS[qtkey]
        seq = QKeySequence(qtkey).toString().lower()
        return seq if seq and seq not in ("ctrl", "alt", "shift", "meta") else ""

    def _start_key_capture(self):
        self._capturing_key = True
        self._chord = []
        self._chord_down = set()
        self._key_capture_btn.setChecked(True)
        self._key_capture_btn.setText("Press keys…")
        self.grabKeyboard()

    def _finish_key_capture(self):
        self._capturing_key = False
        self._chord_down = set()
        self._key_capture_btn.setChecked(False)
        self.releaseKeyboard()
        if self._chord:
            mods = [m for m in self._MOD_ORDER if m in self._chord]
            rest = [n for n in self._chord if n not in self._MOD_ORDER]
            self._set_captured_keys(mods + rest)
        else:
            self._set_captured_keys(self._captured_keys)

    def _set_captured_keys(self, keys: List[str]):
        self._captured_keys = keys
        self._key_capture_btn.setText(display_combo(keys) if keys
                                      else "Click here, then press a key…")

    def keyPressEvent(self, e):
        # While capturing, swallow the keystroke and add it to the chord. Every
        # key is collected, modifiers included: capture used to bail out on a
        # bare Ctrl waiting for "a real key", which made W+A — two real keys,
        # no modifier — impossible to express, and that is exactly the shape
        # movement in a game takes.
        if getattr(self, "_capturing_key", False):
            if e.key() == Qt.Key_Escape:
                self._chord = []
                self._finish_key_capture()
                e.accept(); return
            if not e.isAutoRepeat():
                name = self._key_name(e.key())
                if name:
                    self._chord_down.add(e.key())
                    if name not in self._chord:
                        self._chord.append(name)
                        self._key_capture_btn.setText(
                            display_combo(self._chord) + " …")
            e.accept(); return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        # Letting go is what ends the capture — the gesture is "hold the keys
        # you want held". Auto-repeat is ignored on both sides, because a held
        # key emits release/press pairs and would otherwise end the capture
        # while it is still being pressed.
        if getattr(self, "_capturing_key", False):
            if not e.isAutoRepeat():
                self._chord_down.discard(e.key())
                if not self._chord_down:
                    self._finish_key_capture()
            e.accept(); return
        super().keyReleaseEvent(e)

    def _build_text_panel(self) -> QWidget:
        w = _pane()
        g = QGridLayout(w)
        g.addWidget(QLabel("Text to type:"), 0, 0)
        # Multi-line on purpose. A QLineEdit stores the whole string but only
        # shows what fits — scrolled to the cursor, so a long paragraph looked
        # like its last ~30 characters and the start could not be read without
        # arrowing back. It also cannot hold a newline at all.
        self._text_edit = QPlainTextEdit()
        self._text_edit.setObjectName("textBody")   # opts into the input styling
        self._text_edit.setTabChangesFocus(True)
        self._text_edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        fm = self._text_edit.fontMetrics()
        self._text_edit.setMinimumHeight(fm.lineSpacing() * 3 + 18)
        self._text_edit.setMaximumHeight(fm.lineSpacing() * 8 + 18)
        g.addWidget(self._text_edit, 0, 1)
        g.addWidget(QLabel("Speed (chars/s):"), 1, 0)
        # 0 is the *safe* pace, not the dial's top: every key held long enough
        # for a game to see it, ~33 ch/s. Higher rates are available and are
        # delivered literally, but the extra speed comes out of that hold, so
        # they are opt-in rather than the default.
        safe, top = _safe_type_cps(), _max_type_cps()
        self._text_speed = _dspin(0.0, top, 0.0, 0.5, " ch/s")
        self._text_speed.setSpecialValueText(f"as fast as reliable (~{safe:.0f} ch/s)")
        self._text_speed.setToolTip(
            f"0 types at about {safe:.0f} characters a second, holding each key "
            f"long enough\nfor a game to notice it. Any rate is delivered "
            f"exactly — slower for targets\nthat need human-paced typing, or up "
            f"to {top:.0f} ch/s if you want speed.\n\nAbove ~{safe:.0f} the keys "
            f"are held more briefly, so a game that reads its input\nonce a "
            f"frame may start missing characters. If text comes out with gaps "
            f"or\ntypos, lower this.")
        g.addWidget(self._text_speed, 1, 1)

        # ⚠ This is a property of what you are typing *into*, which is why it is
        # here and not in Settings. It used to be decided by the input backend —
        # one global switch that also governs keys and clicks — so making typing
        # work in a game broke it in every ordinary window, and fixing that broke
        # the game again. Two releases were spent moving the breakage around.
        g.addWidget(QLabel("Send as:"), 2, 0)
        self._text_send_grp = QButtonGroup(self)
        send_wrap, _ = _segmented(["Automatic", "Characters", "Key presses"],
                                  self._text_send_grp, 0)
        send_wrap.setToolTip(
            "Automatic follows the input backend in Settings: characters on\n"
            "pynput, key presses on SendInput and Interception.\n\n"
            "Characters send the letter itself. Correct on any keyboard layout\n"
            "and right for ordinary windows — but most games never see them.\n\n"
            "Key presses send the key's position, which a game does read. The\n"
            "target decides which letter that position makes, so if it arrives\n"
            "as QWERTY on an AZERTY board, switch Settings → Typing layout.")
        g.addWidget(send_wrap, 2, 1)
        hint = QLabel("Text vanishing in a game? Try Key presses. Coming out as "
                      "the wrong letters? Try Characters.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        g.addWidget(hint, 3, 1)

        return w

    def _build_wait_panel(self) -> QWidget:
        w = _pane()
        g = QGridLayout(w)
        g.addWidget(QLabel("Wait duration (ms):"), 0, 0)
        self._wait_ms = _durspin(0, 600000, 500, w=120)
        g.addWidget(self._wait_ms, 0, 1)
        return w

    # ── Detect panels ─────────────────────────────────────────────────
    # All three share one skeleton — What to look for / How long to wait /
    # When found — so switching between them moves fields around as little as
    # possible. They used to be three unrelated grids whose columns were sized
    # by whatever landed in them, which is why nothing lined up.
    def _detect_when_found(self, label: str, on_toggle):
        """The "When found" card, identical for image / text / pixel."""
        card, cl = _card("When found", "Optionally click the thing you were "
                                       "waiting for, instead of just continuing.")
        check = QCheckBox(label)
        check.toggled.connect(on_toggle)
        cl.addWidget(check)
        return card, cl, check

    def _click_opts_row(self, attr_prefix: str):
        """Button + double-click, the same controls in all three Detect panels."""
        combo = QComboBox()
        # The label is shown and the stored name travels as item data, so the
        # list can say "Back" while the step says "x1" — and so the raw
        # lowercase names stop appearing in the UI, which they did until now.
        for b in flow.BUTTONS:
            combo.addItem(flow.button_label(b), b)
        combo.setFixedWidth(110)
        dbl = QCheckBox("Double-click")
        setattr(self, attr_prefix + "_btn", combo)
        setattr(self, attr_prefix + "_double", dbl)
        row = _field("Button", combo, dbl)
        row.setVisible(False)
        return row

    def _build_imgwait_panel(self) -> QWidget:
        w = _pane()
        v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)

        look, ll = _card("What to look for",
                         "A small, distinctive crop matches far better than a "
                         "large one with a changing background.")
        self._imgwait_path = QLineEdit()
        self._imgwait_path.setPlaceholderText("Path to PNG/JPEG…")
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._imgwait_browse)
        btn_cap = QPushButton("Capture…")
        btn_cap.setToolTip("Drag a box on screen and use that crop as the image")
        btn_cap.clicked.connect(self._imgwait_capture)
        ll.addWidget(_field("Image", self._imgwait_path, btn_browse, btn_cap,
                            grow=self._imgwait_path))

        # Per-step search area, same control the text panel uses. Narrowing it
        # is both faster (less pixels to scan at every scale) and safer — an
        # icon that appears in two places matches the one you meant.
        self._imgwait_region_sel = flow_dialogs.RegionSelect()
        ll.addWidget(_field("Search area", self._imgwait_region_sel))

        self._imgwait_conf = _dspin(0.1, 1.0, 0.8, 0.05, w=90)
        self._imgwait_conf.setToolTip("How close the match has to be. Lower it if "
                                      "a script stops matching on another screen.")
        btn_test = QPushButton("Test match")
        btn_test.clicked.connect(lambda: _run_match_test(
            self._imgwait_path.text(), self._imgwait_conf.value(),
            self, self._imgwait_test_result, self,
            region=self._imgwait_region))
        ll.addWidget(_field("Match confidence", self._imgwait_conf, btn_test))

        self._imgwait_test_result = QLabel("")
        self._imgwait_test_result.setObjectName("hint")
        self._imgwait_test_result.setWordWrap(True)
        ll.addWidget(self._imgwait_test_result)

        # ONE image: it is both the captured-region preview and the click-target
        # picker (click it to choose where the click lands when clicking is on).
        # Hidden until there is an image, so the panel is not an empty grey slab.
        self._imgwait_img_label = _hint("Captured region:")
        self._imgwait_picker = ClickPointPicker()
        self._imgwait_preview = _pane()
        pv = QVBoxLayout(self._imgwait_preview)
        pv.setContentsMargins(0, 0, 0, 0); pv.setSpacing(4)
        pv.addWidget(self._imgwait_img_label)
        pv.addWidget(self._imgwait_picker, 0, Qt.AlignLeft)
        self._imgwait_preview.setVisible(False)
        self._imgwait_path.textChanged.connect(self._on_imgwait_path_changed)
        ll.addWidget(self._imgwait_preview)

        if not _HAS_CV2:
            warn = _hint("⚠  " + _NO_CV2_MSG)
            warn.setStyleSheet("color:#f59e0b;")
            ll.addWidget(warn)
        v.addWidget(look)

        wait, wl = _card("How long to wait")
        self._imgwait_timeout = _spin(0, 3600, 5, " s", w=110)
        self._imgwait_timeout.setSpecialValueText("∞ (no timeout)")
        wl.addWidget(_field("Give up after", self._imgwait_timeout))
        v.addWidget(wait)

        found, fl, self._imgwait_do_click = self._detect_when_found(
            "Click the image when it appears", self._on_imgwait_click_toggled)
        self._imgwait_click_opts = self._click_opts_row("_imgwait")
        fl.addWidget(self._imgwait_click_opts)
        v.addWidget(found)
        return w

    @property
    def _imgwait_region(self):
        # Logical virtual-desktop coords, exactly like _textwait_region — the
        # runtime maps them onto screenshot pixels with to_physical_region().
        return self._imgwait_region_sel.region()

    def _on_imgwait_path_changed(self, path: str):
        self._imgwait_picker.load_image(path)
        self._imgwait_preview.setVisible(bool(path.strip()))
        self._refit()

    def _on_imgwait_click_toggled(self, checked: bool):
        self._imgwait_click_opts.setVisible(checked)
        self._imgwait_img_label.setText(
            "Click target — click the image to set where the click lands:"
            if checked else "Captured region:")
        if checked and self._imgwait_path.text():
            self._imgwait_picker.load_image(self._imgwait_path.text())
        self._refit()

    def _imgwait_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Image", "", "Images (*.png *.jpg *.jpeg *.bmp)")
        if path:
            self._imgwait_path.setText(path)

    def _imgwait_capture(self):
        _launch_screenshot_selector(self._imgwait_path, parent_window=self)

    def _build_textwait_panel(self) -> QWidget:
        w = _pane()
        v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)

        look, ll = _card("What to look for",
                         "A region is both faster and more accurate than the "
                         "whole desktop — OCR downscales small text.")
        self._textwait_text = QLineEdit()
        self._textwait_text.setPlaceholderText("e.g. Loading complete")
        ll.addWidget(_field("Text", self._textwait_text, grow=self._textwait_text))

        # Per-step search area: whole screen (default) or a specific region.
        # Each step stores its own, so different steps can watch different
        # windows in the same sequence.
        self._textwait_region_sel = flow_dialogs.RegionSelect()
        ll.addWidget(_field("Search area", self._textwait_region_sel))

        self._textwait_case = QCheckBox("Case-sensitive")
        # Tolerate OCR misreads: still match when a few characters are wrong
        # (OCR often reads e.g. "Macronaut" as "Macconaut"). On by default.
        self._textwait_fuzzy = QCheckBox("Tolerate minor OCR misreads")
        self._textwait_fuzzy.setChecked(True)
        self._textwait_fuzzy.setToolTip(
            "Match even when OCR reads a few characters wrong. "
            "Uncheck for strict exact matching.")
        ll.addWidget(_field("Matching", self._textwait_case, self._textwait_fuzzy))

        self._textwait_score = _dspin(0.1, 1.0, 0.5, 0.05, w=90)
        btn_test = QPushButton("Test text")
        btn_test.clicked.connect(self._textwait_test)
        ll.addWidget(_field("Min. confidence", self._textwait_score, btn_test))

        # Live tester — the result can be several lines (per-word tier
        # breakdown), so it scrolls instead of stretching the dialog.
        self._textwait_test_result = QLabel("")
        self._textwait_test_result.setObjectName("hint")
        self._textwait_test_result.setWordWrap(True)
        self._textwait_test_result.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._textwait_test_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._textwait_result_scroll = QScrollArea()
        self._textwait_result_scroll.setWidgetResizable(True)
        self._textwait_result_scroll.setFrameShape(QFrame.NoFrame)
        self._textwait_result_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._textwait_result_scroll.setMaximumHeight(150)
        self._textwait_result_scroll.setWidget(self._textwait_test_result)
        # Hidden until a test has actually run — an empty 150 px scroll area
        # reserved a blank band in the middle of the panel.
        self._textwait_result_scroll.setVisible(False)
        ll.addWidget(self._textwait_result_scroll)

        # Engine status: warn if there is no OCR at all. There is one engine now
        # (the RapidOCR fallback went in 2.0.12), so it is available or it isn't.
        if not _HAS_OCR:
            warn = _hint("⚠  " + _NO_OCR_MSG)
            warn.setStyleSheet("color:#f59e0b;")
            ll.addWidget(warn)
        v.addWidget(look)

        wait, wl = _card("How long to wait")
        self._textwait_timeout = _spin(0, 3600, 5, " s", w=110)
        self._textwait_timeout.setSpecialValueText("∞ (no timeout)")
        wl.addWidget(_field("Give up after", self._textwait_timeout))
        v.addWidget(wait)

        found, fl, self._textwait_do_click = self._detect_when_found(
            "Click the text when it appears",
            lambda c: (self._textwait_click_opts.setVisible(c), self._refit()))
        self._textwait_click_opts = self._click_opts_row("_textwait")
        fl.addWidget(self._textwait_click_opts)
        v.addWidget(found)
        return w

    @property
    def _textwait_region(self):
        # The search area lives in the segmented control now — it owns both the
        # value and the way it is shown, so there is nothing left to keep in
        # step by hand. Same overlay as image capture underneath: it emits
        # LOGICAL virtual-desktop coords, which to_physical_region() maps onto
        # the screenshot pixels at OCR time.
        return self._textwait_region_sel.region()

    def _textwait_test(self):
        target = self._textwait_text.text().strip()
        rl = self._textwait_test_result
        self._textwait_result_scroll.setVisible(True)   # there is a result now
        self._refit()
        if not target:
            rl.setText("Enter the text to find first.")
            rl.setStyleSheet("color:#959db1;")
            return
        try:
            import ocr
            import matcher
        except Exception:
            ocr = None
        if ocr is None or not ocr.available():
            msg = ocr.status_message() if ocr is not None else _NO_OCR_MSG
            rl.setText("⚠  " + msg)
            rl.setStyleSheet("color:#f59e0b;")
            return
        engine = ocr.get_engine()
        # Dim BOTH this dialog and the main window so neither covers the target.
        # Dimmed, not hidden: hiding a modal dialog ends its exec() as Rejected
        # and the step being edited is discarded (see _Dimmed).
        main = self.parent().window() if self.parent() is not None else None
        shot = pr = err = None
        with _Dimmed(self, main, settle=0.35):
            try:
                shot = matcher.grab_all_screens()
                region_phys = ocr.to_physical_region(self._textwait_region, shot)
                word_thresh = 0.8 if self._textwait_fuzzy.isChecked() else 0.99
                pr = engine.match_phrase(
                    target, shot, region=region_phys,
                    case_sensitive=self._textwait_case.isChecked(),
                    word_thresh=word_thresh)
            except Exception as exc:
                err = str(exc)
        if err:
            rl.setText(f"Test failed: {err}"); rl.setStyleSheet("color:#ef4444;")
            return

        gate = self._textwait_score.value()          # the step's min-confidence
        # Matched but below the configured confidence gate → amber caution.
        if pr is not None and pr.matched and pr.score < gate:
            rl.setText(pr.summary + f"\n(below your {int(round(gate*100))}% "
                       f"confidence setting — would NOT trigger; lower it to use this match)")
            rl.setStyleSheet("color:#f59e0b;")
            if pr.box is not None:
                self._safe_preview(shot, pr.box, pr.score, gate)
            return
        if pr is None or not pr.matched:
            rl.setText(pr.summary if pr is not None else "✗  No result.")
            rl.setStyleSheet("color:#f59e0b;")
            return
        # Confirmed match — Tier 1 (gold) green, Tier 2 (compensatory) lime.
        rl.setText(pr.summary)
        rl.setStyleSheet("color:#22c55e;" if pr.tier == 1 else "color:#a3e635;")
        if pr.box is not None:
            self._safe_preview(shot, pr.box, pr.score, gate)

    def _safe_preview(self, shot, box, score, need):
        try:
            _show_match_preview(self, shot, box, (34, 197, 94),
                                int(round(score * 100)), int(round(need * 100)))
        except Exception:
            pass

    def _build_pixwait_panel(self) -> QWidget:
        w = _pane()
        v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)

        look, ll = _card("What to look for",
                         "Waits until one screen pixel turns a given colour.")
        self._pixwait_x = _spin(0, 32000, 0, w=96)
        self._pixwait_y = _spin(0, 32000, 0, w=96)
        # Pick button — 3-second countdown, mirrors ConditionWidget._pick_pixel.
        self._pixwait_pick_btn = QPushButton("⊕  Pick pixel (3 s)")
        self._pixwait_pick_btn.setToolTip(
            "Move your cursor to the target pixel and wait — "
            "X, Y and colour will be filled automatically.")
        self._pixwait_pick_btn.clicked.connect(self._pixwait_start_pick)
        ll.addWidget(_field("Position", self._pixwait_x, self._pixwait_y,
                            self._pixwait_pick_btn))

        self._pixwait_color = QLineEdit("#ffffff")
        self._pixwait_color.setFixedWidth(110)
        self._pixwait_color.setPlaceholderText("#rrggbb")
        self._pixwait_tol = _spin(0, 255, 10, w=90)
        self._pixwait_tol.setToolTip(
            "How much each R/G/B channel may differ from the target colour (0 = exact).")
        ll.addWidget(_field("Colour", self._pixwait_color,
                            QLabel("± tolerance"), self._pixwait_tol))
        v.addWidget(look)

        wait, wl = _card("How long to wait")
        self._pixwait_timeout = _spin(0, 3600, 5, " s", w=110)
        self._pixwait_timeout.setSpecialValueText("∞ (no timeout)")
        wl.addWidget(_field("Give up after", self._pixwait_timeout))
        v.addWidget(wait)

        # No button/double-click row here: a pixel step clicks the pixel it was
        # watching, with the left button. Offering the other two would be a new
        # capability dressed up as a layout change.
        found, fl, self._pixwait_do_click = self._detect_when_found(
            "Click the pixel when it matches", lambda _c: self._refit())
        v.addWidget(found)

        self._pixwait_pick_countdown = 0
        self._pixwait_pick_timer: Optional[QTimer] = None
        return w

    def _pixwait_start_pick(self):
        self._pixwait_pick_countdown = 3
        self._pixwait_pick_btn.setEnabled(False)
        self._pixwait_pick_btn.setText("Capturing in 3s…")
        self._pixwait_pick_timer = QTimer(self)
        self._pixwait_pick_timer.timeout.connect(self._pixwait_pick_tick)
        self._pixwait_pick_timer.start(1000)

    def _pixwait_pick_tick(self):
        self._pixwait_pick_countdown -= 1
        if self._pixwait_pick_countdown > 0:
            self._pixwait_pick_btn.setText(
                f"Capturing in {self._pixwait_pick_countdown}s…")
            return
        self._pixwait_pick_timer.stop()
        pos = QCursor.pos()
        self._pixwait_x.setValue(pos.x())
        self._pixwait_y.setValue(pos.y())
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab(all_screens=True).convert("RGB")
            r, g, b = img.getpixel((pos.x(), pos.y()))
            self._pixwait_color.setText("#%02x%02x%02x" % (r, g, b))
        except Exception:
            pass
        self._pixwait_pick_btn.setText("Pick pixel (3 s)")
        self._pixwait_pick_btn.setEnabled(True)

    # ── UI logic ──────────────────────────────────────────────────────
    def _sync_family_seg(self):
        """Reflect the current step kind in the family segmented toggle."""
        if self._fam_group is not None and self._fam_indices:
            idx = self._type_combo.currentIndex()
            if idx in self._fam_indices:
                btn = self._fam_group.button(self._fam_indices.index(idx))
                if btn:
                    btn.setChecked(True)

    def _on_type_change(self, idx: int):
        panels = [self._stack_click, self._stack_key,
                  self._stack_text, self._stack_wait,
                  self._stack_imgwait, self._stack_textwait,
                  self._stack_pixwait, self._stack_scroll,
                  self._stack_drag, self._stack_move]
        for i, p in enumerate(panels):
            p.setVisible(i == idx)
        self._refit()

    def _refit(self):
        """Resize to exactly what is showing now.

        adjustSize() alone will not shrink a dialog reliably: the layout caches
        the size hint it had while the taller panel was still visible, so
        switching from Wait-for-image to Wait-for-pixel left a band of empty
        window below the fields. Invalidating first, then resizing to the fresh
        hint, makes the window follow the panel in both directions."""
        lay = self.layout()
        if lay is None:
            return
        lay.invalidate()
        lay.activate()
        # Capped: a wrapped hint line reports its *unwrapped* width as its size
        # hint, so one long sentence could otherwise stretch the dialog across
        # the screen. Past the cap the text wraps instead, which is the point.
        w = max(self.minimumWidth(), min(self.sizeHint().width(), self.MAX_WIDTH))
        h = lay.heightForWidth(w) if lay.hasHeightForWidth() else self.sizeHint().height()
        self.setMinimumHeight(0)
        h = max(h, lay.minimumSize().height())
        avail = self._available_height()
        if avail and h > avail:
            # Taller than the screen. Qt clamps the geometry to the monitor but
            # the layout's minimum keeps demanding the full height, so the last
            # row — OK/Cancel — ends up below the bottom edge, unreachable.
            self._fit_to_screen()
            lay = self.layout()
            lay.invalidate()
            lay.activate()
            h = avail
        self.resize(w, h)

    def _available_height(self) -> int:
        """Usable height of the screen this dialog is on, minus window chrome.

        0 when there is no screen to ask (offscreen platform in tests), which
        reads as "no limit" — the same behaviour this method replaced.
        """
        scr = self.screen() or QApplication.primaryScreen()
        if scr is None:
            return 0
        frame = max(0, self.frameGeometry().height() - self.height())
        return max(200, scr.availableGeometry().height() - frame)

    def _fit_to_screen(self):
        """Move the body into a scroll area, leaving OK/Cancel pinned below.

        Done lazily and only once: a step editor is only ever this tall with
        every optional panel showing, and a dialog that fits should keep its
        plain layout so nothing about the common case changes.
        """
        if self._scrolled:
            return
        self._scrolled = True
        lay = self.layout()

        host = QWidget()
        host.setObjectName("fieldRow")     # layout-only: _QSS would paint it black
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(lay.spacing())

        moved = []
        while lay.count():
            item = lay.takeAt(0)
            if item.widget() is self._btns:
                continue               # re-added below the scroll area
            moved.append(item)
        for item in moved:
            if item.widget() is not None:
                hl.addWidget(item.widget())
            elif item.layout() is not None:
                hl.addLayout(item.layout())
            else:
                hl.addItem(item)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(host)
        lay.addWidget(area, 1)
        lay.addWidget(self._btns)

    def _on_ok(self):
        idx = self._type_combo.currentIndex()
        delay = self._delay.value()

        if idx == 0:   # Click
            btn  = self.CLICK_BUTTONS[max(0, self._click_btn_grp.checkedId())]
            x, y = self._click_x.value(), self._click_y.value()
            mode = self._click_mode()
            clk  = 2 if mode == self.CLICK_DOUBLE else 1
            hold = mode == self.CLICK_HOLD
            self._result_step = SeqStep(SeqStep.CLICK, self._with_mods(
                {"button": btn, "x": x, "y": y,
                 "clicks": clk, "hold": hold,
                 "hold_ms": self._click_hold_ms.value()},
                self._click_mods), delay)
        elif idx == 9:  # Move
            # Its own delay, never self._delay — see _build_move_panel.
            self._result_step = SeqStep(
                SeqStep.MOVE,
                {"x": self._move_x.value(), "y": self._move_y.value()},
                self._move_delay.value())
        elif idx == 7:  # Scroll
            # Delay 0, deliberately: "Delay before" is a row inside the *Click*
            # panel, and a scroll step carries its delay on the node like every
            # other kind (flow.delay_applies allows it). Reading self._delay
            # here would let a value typed into the Click panel ride along
            # invisibly after toggling to Scroll.
            delay = 0
            at_cursor = self._scroll_where_grp.checkedId() != self.SCROLL_AT_POS
            data = {"direction": self.SCROLL_DIRS[max(0, self._scroll_dir_grp.checkedId())],
                    "amount": self._scroll_amount.value(),
                    "speed_nps": self._scroll_speed.value(),
                    "at_cursor": at_cursor}
            if not at_cursor:
                data["x"] = self._scroll_x.value()
                data["y"] = self._scroll_y.value()
            self._result_step = SeqStep(SeqStep.SCROLL, self._with_mods(data, self._scroll_mods), delay)
        elif idx == 8:  # Drag
            # Delay 0, for the same reason Scroll uses 0: "Delay before" is a
            # row inside the *Click* panel, so reading it here would let a
            # value typed there ride along invisibly after toggling to Drag.
            # A drag carries its delay on the node (flow.delay_applies).
            self._result_step = SeqStep(SeqStep.DRAG, self._with_mods({
                "button": self.CLICK_BUTTONS[max(0, self._drag_btn_grp.checkedId())],
                "x": self._drag_x.value(), "y": self._drag_y.value(),
                "to_x": self._drag_to_x.value(), "to_y": self._drag_to_y.value(),
                "duration_ms": self._drag_ms.value(),
            }, self._drag_mods), 0)
        elif idx == 1: # Key / Combo
            keys = list(self._captured_keys)
            mode = self.KEY_MODES[max(0, self._key_mode.currentIndex())]
            if not keys and mode != flow.KEY_UP:
                QMessageBox.information(self, "No key captured",
                                       "Click the button and press the key you want first.")
                return
            kind = SeqStep.COMBO if len(keys) > 1 else SeqStep.KEY
            key_data = {"keys": keys, "mode": mode}
            if mode in (flow.KEY_TAP, flow.KEY_HOLD):
                key_data["repeat"] = self._key_repeat.value()
            if mode == flow.KEY_HOLD:
                key_data["hold_ms"] = self._key_hold_ms.value()
            self._result_step = SeqStep(kind, key_data, delay)
        elif idx == 2: # Text
            self._result_step = SeqStep(SeqStep.TEXT,
                {"text": self._text_edit.toPlainText(),
                 "speed_cps": self._text_speed.value(),
                 "send_as": self.SEND_MODES[
                     max(0, self._text_send_grp.checkedId())]}, delay)
        elif idx == 3:  # Wait
            self._result_step = SeqStep(SeqStep.WAIT,
                {"ms": self._wait_ms.value()}, delay)
        elif idx == 4:  # Detect: Image
            self._result_step = SeqStep(SeqStep.WAIT_IMAGE, {
                "image_path": self._imgwait_path.text().strip(),
                "confidence": self._imgwait_conf.value(),
                "timeout_s":  self._imgwait_timeout.value(),
                "click":      self._imgwait_do_click.isChecked(),
                "button":     self._imgwait_btn.currentData(),
                "clicks":     2 if self._imgwait_double.isChecked() else 1,
                "offset_x":   self._imgwait_picker.offset()[0],
                "offset_y":   self._imgwait_picker.offset()[1],
                "region":     self._imgwait_region,
            }, delay)
        elif idx == 5:  # Detect: Text
            if not self._textwait_text.text().strip():
                QMessageBox.information(self, "No text entered",
                                       "Type the text you want to wait for first.")
                return
            self._result_step = SeqStep(SeqStep.WAIT_TEXT, {
                "text":           self._textwait_text.text().strip(),
                "case_sensitive": self._textwait_case.isChecked(),
                "min_score":      self._textwait_score.value(),
                "timeout_s":      self._textwait_timeout.value(),
                "click":          self._textwait_do_click.isChecked(),
                "button":         self._textwait_btn.currentData(),
                "clicks":         2 if self._textwait_double.isChecked() else 1,
                "region":         self._textwait_region,
                "fuzzy":          self._textwait_fuzzy.isChecked(),
            }, delay)
        else:           # Detect: Pixel
            self._result_step = SeqStep(SeqStep.WAIT_PIXEL, {
                "x":         self._pixwait_x.value(),
                "y":         self._pixwait_y.value(),
                "color":     self._pixwait_color.text().strip() or "#ffffff",
                "tolerance": self._pixwait_tol.value(),
                "timeout_s": self._pixwait_timeout.value(),
                "click":     self._pixwait_do_click.isChecked(),
                "button":    "left",
                "clicks":    1,
            }, delay)

        self.accept()

    @staticmethod
    def _load_mods(d: dict, boxes: dict) -> None:
        """Tick the boxes a step was recorded with.

        Through flow's accessor, like every other read here: a step written by
        hand can say "Ctrl" or "CTRL" or list them in either order, and it has
        to reopen as the thing it actually does.
        """
        held = set(flow.pointer_mods(d))
        for m, cb in boxes.items():
            cb.setChecked(m in held)

    @staticmethod
    def _with_mods(data: dict, boxes: dict) -> dict:
        """Add the ticked modifiers to a pointer step's data.

        Absent when none are ticked rather than an empty list: `flow.pointer_mods`
        reads the absence of the field as "no modifiers", which is what every
        flow saved before they existed says, and writing `"mods": []` into every
        click from now on would say the same thing in a way that looks like a
        setting somebody chose.
        """
        mods = [m for m, cb in boxes.items() if cb.isChecked()]
        if mods:
            data["mods"] = mods
        return data

    def _load(self, s: SeqStep):
        self._delay.setValue(int(s.delay_ms))
        kind_map = {SeqStep.CLICK: 0, SeqStep.KEY: 1, SeqStep.COMBO: 1,
                    SeqStep.TEXT: 2, SeqStep.WAIT: 3, SeqStep.WAIT_IMAGE: 4,
                    SeqStep.WAIT_TEXT: 5, SeqStep.WAIT_PIXEL: 6,
                    SeqStep.SCROLL: 7, SeqStep.DRAG: 8,
                    SeqStep.MOVE: 9}
        self._type_combo.setCurrentIndex(kind_map.get(s.kind, 0))
        d = s.data
        if s.kind == SeqStep.CLICK:
            btn = d.get("button", "left")
            i = self.CLICK_BUTTONS.index(btn) if btn in self.CLICK_BUTTONS else 0
            self._click_btn_grp.button(i).setChecked(True)
            self._click_x.setValue(d.get("x", 0))
            self._click_y.setValue(d.get("y", 0))
            mode = (self.CLICK_HOLD if d.get("hold")
                    else self.CLICK_DOUBLE if d.get("clicks", 1) == 2
                    else self.CLICK_SINGLE)
            self._click_mode_grp.button(mode).setChecked(True)
            self._on_click_mode(mode)
            self._click_hold_ms.setValue(int(d.get("hold_ms", 1000)))
            self._load_mods(d, self._click_mods)
        elif s.kind == SeqStep.MOVE:
            self._move_x.setValue(int(d.get("x", 0) or 0))
            self._move_y.setValue(int(d.get("y", 0) or 0))
            self._move_delay.setValue(int(s.delay_ms))
        elif s.kind == SeqStep.SCROLL:
            # Read through flow's own accessors, so a hand-written or older step
            # reopens as whatever it actually does rather than as the first item
            # in each control.
            self._scroll_dir_grp.button(
                self.SCROLL_DIRS.index(flow.scroll_direction(d))).setChecked(True)
            self._scroll_amount.setValue(flow.scroll_notches(d))
            self._scroll_speed.setValue(float(flow.scroll_cps(d)))
            where = (self.SCROLL_AT_CURSOR if d.get("at_cursor", True)
                     else self.SCROLL_AT_POS)
            self._scroll_where_grp.button(where).setChecked(True)
            self._scroll_x.setValue(int(d.get("x", 0) or 0))
            self._scroll_y.setValue(int(d.get("y", 0) or 0))
            self._on_scroll_where(where)
            self._load_mods(d, self._scroll_mods)
        elif s.kind == SeqStep.DRAG:
            btn = d.get("button", "left")
            self._drag_btn_grp.button(
                self.CLICK_BUTTONS.index(btn) if btn in self.CLICK_BUTTONS
                else 0).setChecked(True)
            self._drag_x.setValue(int(d.get("x", 0) or 0))
            self._drag_y.setValue(int(d.get("y", 0) or 0))
            self._drag_to_x.setValue(int(d.get("to_x", 0) or 0))
            self._drag_to_y.setValue(int(d.get("to_y", 0) or 0))
            # Through flow's accessor, so a hand-written step with no duration
            # reopens as the default it actually runs at rather than as 0.
            self._drag_ms.setValue(int(flow.drag_duration_ms(d)))
            self._load_mods(d, self._drag_mods)
        elif s.kind in (SeqStep.KEY, SeqStep.COMBO):
            self._set_captured_keys(list(d.get("keys", [])))
            self._key_repeat.setValue(int(d.get("repeat", 1) or 1))
            hold_ms = int(d.get("hold_ms", 0) or 0)
            self._key_hold_ms.setValue(hold_ms if hold_ms > 0 else 1000)
            # flow.key_mode, not d["mode"]: a flow saved before Hold down
            # existed has no mode at all, and must reopen as the thing it has
            # always done rather than as the first item in the list.
            mode = flow.key_mode(d)
            self._key_mode.setCurrentIndex(self.KEY_MODES.index(mode))
            self._on_key_mode(self._key_mode.currentIndex())
        elif s.kind == SeqStep.TEXT:
            self._text_edit.setPlainText(d.get("text", ""))
            self._text_speed.setValue(d.get("speed_cps", 10.0))
            # Through flow's accessor, so a step saved before this existed
            # reopens on Automatic — which is what it has been doing all along.
            self._text_send_grp.button(
                self.SEND_MODES.index(flow.send_as(d))).setChecked(True)
        elif s.kind == SeqStep.WAIT:
            self._wait_ms.setValue(d.get("ms", 500))
        elif s.kind == SeqStep.WAIT_IMAGE:
            self._imgwait_path.setText(d.get("image_path", ""))
            self._imgwait_conf.setValue(d.get("confidence", 0.8))
            self._imgwait_timeout.setValue(d.get("timeout_s", 0))
            self._imgwait_do_click.setChecked(d.get("click", False))
            self._imgwait_btn.setCurrentIndex(
                max(0, self._imgwait_btn.findData(d.get("button", "left"))))
            self._imgwait_double.setChecked(d.get("clicks", 1) == 2)
            self._imgwait_picker.load_image(d.get("image_path", ""))
            self._imgwait_picker.set_offset(d.get("offset_x", 0), d.get("offset_y", 0))
            self._imgwait_region_sel.set_region(d.get("region"))
        elif s.kind == SeqStep.WAIT_TEXT:
            self._textwait_text.setText(d.get("text", ""))
            self._textwait_case.setChecked(d.get("case_sensitive", False))
            self._textwait_score.setValue(d.get("min_score", 0.5))
            self._textwait_timeout.setValue(d.get("timeout_s", 0))
            self._textwait_do_click.setChecked(d.get("click", False))
            self._textwait_btn.setCurrentIndex(
                max(0, self._textwait_btn.findData(d.get("button", "left"))))
            self._textwait_double.setChecked(d.get("clicks", 1) == 2)
            self._textwait_region_sel.set_region(d.get("region"))
            self._textwait_fuzzy.setChecked(d.get("fuzzy", True))
        elif s.kind == SeqStep.WAIT_PIXEL:
            self._pixwait_x.setValue(d.get("x", 0))
            self._pixwait_y.setValue(d.get("y", 0))
            self._pixwait_color.setText(d.get("color", "#ffffff"))
            self._pixwait_tol.setValue(d.get("tolerance", 10))
            self._pixwait_timeout.setValue(d.get("timeout_s", 0))
            self._pixwait_do_click.setChecked(d.get("click", False))

    @property
    def step(self) -> Optional[SeqStep]:
        return self._result_step


# ═══════════════════════════════════════════════════════════════════════════════
#  Shared image-trigger helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _update_preview(label: QLabel, path: str):
    """Show a thumbnail of the trigger image inside label."""
    if not path or not Path(path).is_file():
        label.clear()
        label.setText("no image")
        return
    px = QPixmap(path)
    if px.isNull():
        label.setText("?")
        return
    label.setPixmap(px.scaled(label.width(), label.height(),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation))


# Module-level ref so the overlay widget isn't garbage-collected while in use.
_active_selector: list = []


def _launch_screenshot_selector(path_field: QLineEdit, parent_window=None):
    """
    Open a full-screen capture overlay. The overlay is application-modal so it
    receives input even when launched from a modal dialog (a modal dialog holds
    Qt's input grab and would otherwise swallow the overlay's mouse clicks).
    The parent dialog is dimmed to opacity 0 — not hidden — while the overlay is
    live: hide() makes a modal dialog's exec() return Rejected and discard the
    result, whereas opacity keeps its exec_ loop alive. Opacity is restored once
    the region is captured or the capture is cancelled.
    A module-level list keeps the selector alive until it closes.
    """
    def _on_region(x, y, w, h):
        # Wait for the overlay to fully disappear before grabbing pixels
        QTimer.singleShot(300, lambda: _do_grab(x, y, w, h))

    def _do_grab(x, y, w, h):
        _active_selector.clear()
        path = _grab_region(x, y, w, h)
        if path:
            path_field.setText(path)
        if parent_window:
            parent_window.setWindowOpacity(1.0)
            parent_window.raise_()
            parent_window.activateWindow()

    def _on_cancel():
        _active_selector.clear()
        if parent_window:
            parent_window.setWindowOpacity(1.0)
            parent_window.raise_()
            parent_window.activateWindow()

    if parent_window:
        parent_window.setWindowOpacity(0.0)
    sel = ScreenshotSelector()
    sel.region_selected.connect(_on_region)
    sel.cancelled.connect(_on_cancel)
    _active_selector.clear()
    _active_selector.append(sel)   # prevent GC
    sel.raise_()
    sel.activateWindow()


def _launch_region_picker(on_region, parent_window=None, on_cancel=None):
    """
    Open the same full-screen overlay used for image capture, but purely to
    PICK a region — no pixel grab. Calls on_region(x, y, w, h) with logical
    virtual-desktop coords (what to_physical_region() expects). Reuses the
    module-level keep-alive list so the overlay isn't garbage-collected.

    `on_cancel` lets a caller undo whatever it did in anticipation of a pick —
    the segmented Search-area control needs it, because clicking its "Select
    region…" half moves the selection before the overlay has said anything.
    """
    def _on_region(x, y, w, h):
        _active_selector.clear()
        on_region(int(x), int(y), int(w), int(h))
        if parent_window:
            parent_window.setWindowOpacity(1.0)
            parent_window.raise_()
            parent_window.activateWindow()

    def _on_cancel():
        _active_selector.clear()
        if on_cancel:
            on_cancel()
        if parent_window:
            parent_window.setWindowOpacity(1.0)
            parent_window.raise_()
            parent_window.activateWindow()

    if parent_window:
        parent_window.setWindowOpacity(0.0)
    sel = ScreenshotSelector()
    sel.region_selected.connect(_on_region)
    sel.cancelled.connect(_on_cancel)
    _active_selector.clear()
    _active_selector.append(sel)   # prevent GC
    sel.raise_()
    sel.activateWindow()


def _show_match_preview(parent, shot_pil, m, box_rgb, pct, need):
    """Pop a dialog showing the captured screen with the match region boxed."""
    from PIL import ImageDraw
    img  = shot_pil.copy()
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = m.left, m.top, m.left + m.width, m.top + m.height
    for t in range(3):                       # thicker rectangle
        draw.rectangle([x0 - t, y0 - t, x1 + t, y1 + t], outline=box_rgb)

    max_w = 760                              # downscale for display
    if img.width > max_w:
        r = max_w / img.width
        img = img.resize((max_w, max(1, int(img.height * r))))

    dest = data_dir() / "captures"
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / "_match_test.png"
    img.save(str(tmp))

    dlg = QDialog(parent)
    dlg.setWindowTitle("Match test")
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(f"Best match: {pct}%   (threshold {need}%)   "
                         "— box shows where it matched on screen"))
    pic = QLabel(); pic.setPixmap(QPixmap(str(tmp)))
    lay.addWidget(pic)
    bb = QDialogButtonBox(QDialogButtonBox.Ok)
    bb.accepted.connect(dlg.accept)
    lay.addWidget(bb)
    dlg.exec()


class _Dimmed:
    """Get our own windows out of a screen grab without hiding them.

    hide() on a modal QDialog makes its exec() return **Rejected** — so pressing
    "Test match" while adding a Detect node threw the node away before OK was
    ever pressed, and the dialog vanished mid-edit. Opacity 0 keeps the exec
    loop alive and is just as invisible to ImageGrab. Same trap, and the same
    fix, as _launch_screenshot_selector.
    """

    def __init__(self, *widgets, settle: float = 0.30):
        self._settle = settle
        self._w = []
        for w in widgets:
            if w is not None and w.isVisible() and w not in self._w:
                self._w.append(w)

    def __enter__(self):
        for w in self._w:
            w.setWindowOpacity(0.0)
        QApplication.processEvents()
        if self._w:
            time.sleep(self._settle)
        return self

    def __exit__(self, *exc):
        for w in self._w:
            w.setWindowOpacity(1.0)
        if self._w:
            self._w[-1].raise_()
            self._w[-1].activateWindow()
        return False


def _run_match_test(image_path: str, confidence: float, hide_widget, result_label,
                    parent=None, region=None):
    """
    Grab the screen now and report how well `image_path` matches, updating
    result_label with the score and popping a boxed-screenshot preview.
    `region` is an optional LOGICAL (x, y, w, h) search area — the same one the
    step will use at runtime, so the test answers the question the step asks.
    """
    if not image_path or not Path(image_path).is_file():
        result_label.setText("Pick or capture an image first.")
        result_label.setStyleSheet("color:#959db1;")
        return
    try:
        import matcher
    except Exception:
        matcher = None
    if matcher is None or not matcher.ENABLED:
        result_label.setText("⚠  Image matching unavailable (need opencv-python + Pillow).")
        result_label.setStyleSheet("color:#f59e0b;")
        return

    # Briefly get our own window(s) out of the way so they don't cover the
    # target — dimmed, never hidden (see _Dimmed).
    parent_win = None
    if hide_widget is not None and hide_widget.parent() is not None:
        parent_win = hide_widget.parent().window()
    shot = m = err = None
    with _Dimmed(hide_widget,
                 hide_widget.window() if hide_widget else None,
                 parent_win):
        try:
            shot = matcher.grab_all_screens()
            region_phys = None
            if region:
                try:
                    import ocr as _o
                    region_phys = _o.to_physical_region(region, shot)
                except Exception:
                    region_phys = None
            m = matcher.best_match(image_path, screenshot=shot,
                                   region=region_phys)
        except Exception as exc:
            err = str(exc)

    if err:
        result_label.setText(f"Test failed: {err}")
        result_label.setStyleSheet("color:#ef4444;")
        return
    if m is None or shot is None:
        result_label.setText("Couldn't read the template image.")
        result_label.setStyleSheet("color:#ef4444;")
        return

    pct, need = int(round(m.score * 100)), int(round(confidence * 100))
    if m.score >= confidence:
        result_label.setText(f"✓  Match {pct}%   (≥ {need}% needed)")
        result_label.setStyleSheet("color:#22c55e;")
        box_rgb = (34, 197, 94)
    else:
        result_label.setText(f"✗  Best {pct}%   (below {need}% — lower confidence or recapture)")
        result_label.setStyleSheet("color:#f59e0b;")
        box_rgb = (245, 158, 11)

    try:
        _show_match_preview(parent, shot, m, box_rgb, pct, need)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  Tab: Sequence
# ═══════════════════════════════════════════════════════════════════════════════

class StepTable(QTableWidget):
    """Steps table with internal drag-to-reorder and a friendly empty state."""

    rows_reordered = Signal(int, int)   # from_row, to_row

    def __init__(self, rows: int, cols: int):
        super().__init__(rows, cols)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, event):
        # We reorder the backing model ourselves and rebuild the table. We must
        # therefore tell Qt the action was NOT a move — otherwise QAbstractItemView
        # deletes the "source" row *after* our rebuild, which previously wiped a
        # row's contents (the reported "dragged item gets disabled / blanked" bug).
        if event.source() is not self:
            event.ignore()
            return
        src = self.currentRow()
        if src < 0:
            event.ignore()
            return
        pos = event.pos()
        index = self.indexAt(pos)
        if index.isValid():
            dst = index.row()
            rect = self.visualRect(index)
            if pos.y() > rect.center().y():   # dropped on lower half → after
                dst += 1
        else:
            dst = self.rowCount()             # dropped past the last row
        event.setDropAction(Qt.IgnoreAction)  # prevent Qt's own row removal
        event.accept()
        self.rows_reordered.emit(src, dst)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.rowCount() == 0:
            p = QPainter(self.viewport())
            p.setPen(QColor("#585b70"))
            f = p.font(); f.setPointSize(11); p.setFont(f)
            p.drawText(self.viewport().rect(), Qt.AlignCenter,
                       "No steps yet.\n\n"
                       "Pick an action on the left to add your first step,\n"
                       "or press  ⏺ Record  to capture what you do.")


DELETED_DIRNAME = "_deleted"


def deleted_dir():
    """Where deleted scripts go. A sibling of the library, not a subfolder of
    it — `scripts_dir().glob("*.json")` would otherwise have to learn to skip
    it, and every future scan of that folder would have to remember to."""
    return data_dir() / DELETED_DIRNAME


class _ScriptItemDelegate(QStyledItemDelegate):
    """Name on the left, meta right-aligned and dimmed, on one row.

    A QListWidgetItem holds one string, so the old layout padded the name with
    literal spaces to fake a second column — which only lined up for names of
    a similar length. Painting the two halves separately lets the meta sit hard
    against the right edge whatever the name does.
    """
    PAD_X = 12
    ROW_H = 34

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.ROW_H)

    def paint(self, painter, option, index):
        painter.save()
        style = option.widget.style() if option.widget else QApplication.style()
        # Let the stylesheet draw selection/hover, then put our own text on top.
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter,
                            option.widget)
        rect = option.rect.adjusted(self.PAD_X, 0, -self.PAD_X, 0)
        name = index.data(Qt.DisplayRole) or ""
        meta = index.data(Qt.UserRole + 1) or ""

        painter.setFont(option.font)
        if meta:
            meta_w = painter.fontMetrics().horizontalAdvance(meta)
            painter.setPen(QColor(theme_color("muted")))
            painter.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, meta)
            rect = rect.adjusted(0, 0, -(meta_w + 16), 0)

        painter.setPen(QColor(theme_color("text")))
        painter.drawText(
            rect, Qt.AlignLeft | Qt.AlignVCenter,
            painter.fontMetrics().elidedText(name, Qt.ElideRight, rect.width()))
        painter.restore()


class ScriptLibraryDialog(QDialog):
    """Browse saved scripts: open one, merge several, or delete them."""

    def __init__(self, parent=None, settings: "SettingsManager" = None):
        super().__init__(parent)
        self.setWindowTitle("Script Library")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumSize(620, 500)
        self.result_graph = None
        self.result_name = ""
        self.bindings_changed = False
        self._settings = settings
        self._paths: list = []

        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        # ── Header: title left, search right ─────────────────────────────
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Saved scripts"); title.setObjectName("seq_section")
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search…")
        self._search.setClearButtonEnabled(True)
        self._search.setMaximumWidth(240)
        self._search.textChanged.connect(self._apply_filter)
        head.addWidget(title); head.addStretch(1); head.addWidget(self._search)
        lay.addWidget(_pane(head))

        lay.addWidget(_hint("Double-click to open · Ctrl-click to select several, "
                            "then Merge or Delete"))

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._list.setItemDelegate(_ScriptItemDelegate(self._list))
        self._list.setUniformItemSizes(True)
        self._list.itemDoubleClicked.connect(lambda *_: self._open())
        self._list.itemSelectionChanged.connect(self._sync_buttons)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._context_menu)
        lay.addWidget(self._list, 1)

        # Delete needs a keyboard route too — reaching for the Del key is the
        # first thing anyone does in a list. It hangs off the *list*, not the
        # dialog: a shortcut outranks a key press, so on the dialog it would
        # fire while the user was pressing Del to edit the search box.
        self._del_action = QAction("Delete", self._list)
        self._del_action.setShortcut(QKeySequence.Delete)
        self._del_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self._del_action.triggered.connect(self._delete)
        self._list.addAction(self._del_action)

        self._status = _hint("")
        lay.addWidget(self._status)

        # ── Buttons: library actions left, selection actions right ───────
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._btn_import = _btn("Import…", tip="Copy a .json script into your library")
        # The only route to the starters for anyone who was already using
        # Macronaut before they existed: their library is not empty, so the
        # first-run seeding correctly skips them forever. It is also the only
        # way to get one back after deleting it.
        self._btn_examples = _btn(
            "Add examples", "ghost",
            tip="Put back any of the built-in example scripts you do not have.\n"
                "Never overwrites a script you already have under that name.")
        self._btn_folder = _btn("Open folder", "ghost",
                                tip=f"Show the library in Explorer\n{scripts_dir()}")
        self._btn_delete = _btn("Delete", "danger",
                                tip="Move the selected scripts to the deleted folder")
        self._btn_merge  = _btn("Merge", tip="Append the selected scripts "
                                             "end-to-end into one new script")
        self._btn_open   = _btn("Open", "primary",
                                tip="Load the selected script into the builder")
        btn_close = _btn("Close")
        self._btn_import.clicked.connect(self._import)
        self._btn_examples.clicked.connect(self._add_examples)
        self._btn_folder.clicked.connect(self._open_folder)
        self._btn_delete.clicked.connect(self._delete)
        self._btn_merge.clicked.connect(self._merge)
        self._btn_open.clicked.connect(self._open)
        btn_close.clicked.connect(self.reject)
        row.addWidget(self._btn_import); row.addWidget(self._btn_examples)
        row.addWidget(self._btn_folder)
        row.addStretch(1)
        row.addWidget(self._btn_delete); row.addWidget(self._btn_merge)
        row.addWidget(self._btn_open); row.addWidget(btn_close)
        # ⚠ On the buttons, not only on the dialog — the lesson the palette
        # paid for four times. Widening a container does nothing for a child
        # asking for too little, and extra dialog width here goes to the
        # stretch in the middle of the row rather than to any button.
        # `_btn` sets no minimum width, so a squeezed layout will silently
        # shave a label rather than refuse to shrink.
        for b in (self._btn_import, self._btn_examples, self._btn_folder,
                  self._btn_delete, self._btn_merge, self._btn_open, btn_close):
            b.setMinimumWidth(_label_width(b) + PALETTE_SLACK)
        buttons = _pane(row)
        lay.addWidget(buttons)

        # And the dialog cannot be narrower than the row it contains. 620 was
        # not enough even before "Add examples": measured at that width,
        # "Import…" clipped by 3px and "Open folder" by 13.
        need = buttons.sizeHint().width() + 2 * lay.contentsMargins().left()
        self.setMinimumSize(max(620, need), 500)

        self._refresh()

    # ── Data ─────────────────────────────────────────────────────────────
    def _scan(self):
        try:
            return sorted(scripts_dir().glob("*.json"),
                          key=lambda p: p.stem.lower())
        except Exception:
            return []

    def _meta_for(self, path) -> str:
        try:
            g = flow.FlowGraph.load(str(path))
            ls = g.to_linear_steps()
            # ⚠ The fallback counts flow.WORK_TYPES, not N_ACTION. A branching
            # flow is the only thing that reaches it, and counting actions alone
            # means the branches — the reason it branched — are invisible: a
            # flow built entirely of If nodes reads "0 steps" while Play runs it
            # perfectly, because has_work() asks WORK_TYPES. That is the same
            # split 2.0.7 fixed in has_content(); one definition, asked here too.
            n = len(ls) if ls is not None else sum(
                1 for nn in g.nodes.values() if nn.type in flow.WORK_TYPES)
            steps = f"{n} step{'s' if n != 1 else ''}"
        except Exception:
            return "unreadable"
        try:
            when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%d %b %Y")
            return f"{steps}  ·  {when}"
        except Exception:
            return steps

    def _refresh(self):
        self._list.clear()
        self._paths = self._scan()
        for path in self._paths:
            it = QListWidgetItem(path.stem)
            it.setData(Qt.UserRole, str(path))
            it.setData(Qt.UserRole + 1, self._meta_for(path))
            if self._bindings_for(path.stem):
                it.setToolTip("Bound to a launcher key")
            self._list.addItem(it)
        self._apply_filter()

    def _apply_filter(self):
        needle = self._search.text().strip().lower()
        shown = 0
        for i in range(self._list.count()):
            it = self._list.item(i)
            hit = needle in it.text().lower()
            it.setHidden(not hit)
            shown += bool(hit)
        self._sync_buttons(shown)

    def _sync_buttons(self, shown: int = -1):
        n = len(self._selected_paths())
        total = self._list.count()
        if shown < 0:
            shown = sum(1 for i in range(total) if not self._list.item(i).isHidden())
        self._btn_open.setEnabled(n == 1)
        self._btn_merge.setEnabled(n >= 2)
        self._btn_delete.setEnabled(n >= 1)
        self._del_action.setEnabled(n >= 1)
        if not total:
            self._status.setText("No saved scripts yet — save a script, or import one.")
        elif shown < total:
            self._status.setText(f"{shown} of {total} shown"
                                 + (f"  ·  {n} selected" if n else ""))
        else:
            self._status.setText(f"{total} script{'s' if total != 1 else ''}"
                                 + (f"  ·  {n} selected" if n else ""))

    def _selected_paths(self):
        return [it.data(Qt.UserRole) for it in self._list.selectedItems()
                if it.data(Qt.UserRole) and not it.isHidden()]

    # ── Launcher-key bindings ────────────────────────────────────────────
    def _bindings_for(self, *names) -> dict:
        """{hotkey: script name} for any of `names` — a binding points at a
        script *name*, so deleting the file strands the key."""
        if self._settings is None:
            return {}
        wanted = set(names)
        bound = getattr(self._settings.s, "script_hotkeys", None) or {}
        return {hk: nm for hk, nm in bound.items() if nm in wanted}

    # ── Actions ──────────────────────────────────────────────────────────
    def _context_menu(self, pos):
        it = self._list.itemAt(pos)
        if it is not None and not it.isSelected():
            self._list.setCurrentItem(it)
        if not self._selected_paths():
            return
        menu = QMenu(self)
        act_open = menu.addAction("Open")
        act_open.setEnabled(len(self._selected_paths()) == 1)
        act_merge = menu.addAction("Merge selected")
        act_merge.setEnabled(len(self._selected_paths()) >= 2)
        menu.addSeparator()
        act_del = menu.addAction("Delete")
        chosen = menu.exec(self._list.viewport().mapToGlobal(pos))
        if chosen is act_open:
            self._open()
        elif chosen is act_merge:
            self._merge()
        elif chosen is act_del:
            self._delete()

    def _open_folder(self):
        try:
            scripts_dir().mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(scripts_dir())))
        except Exception as e:
            QMessageBox.warning(self, "Could not open folder", str(e))

    def _import(self):
        import shutil
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import scripts", "", "JSON Scripts (*.json)")
        for p in paths:
            try:
                shutil.copy(p, scripts_dir() / Path(p).name)
            except Exception:
                pass
        self._refresh()

    def _add_examples(self):
        """Put back any built-in example the library does not have.

        ⚠ Says what happened either way. "Nothing appeared and nothing was
        said" is indistinguishable from a broken button, and the commonest
        outcome here is genuinely that there was nothing to add.
        """
        added = starters.add_missing(scripts_dir())
        # ⚠ Order matters and the obvious order is wrong. `_refresh` ends in
        # `_sync_buttons`, which unconditionally rewrites `_status` — and so
        # does selecting an item, through `itemSelectionChanged`. Setting the
        # message first leaves the user reading "7 scripts · 1 selected" and
        # wondering whether the button did anything. It goes last.
        self._refresh()
        if added:
            self._select_by_name(added[0])
            self._status.setText(
                f"Added {len(added)} example{'s' if len(added) != 1 else ''}: "
                + ", ".join(added))
        else:
            self._status.setText("You already have all the examples.")

    def _select_by_name(self, name: str):
        """Select the item whose file stem is `name`, if it is on screen.

        A search filter can be hiding it — in which case leave the selection
        alone rather than selecting something the user cannot see.
        """
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.text() == name and not item.isHidden():
                self._list.setCurrentItem(item)
                self._list.scrollToItem(item)
                return

    def _delete(self):
        sel = [Path(p) for p in self._selected_paths()]
        if not sel:
            return
        names = [p.stem for p in sel]
        bound = self._bindings_for(*names)

        if len(sel) == 1:
            body = f"Delete “{names[0]}”?"
        else:
            listed = "\n".join(f"  •  {n}" for n in names[:8])
            more = f"\n  …and {len(names) - 8} more" if len(names) > 8 else ""
            body = f"Delete {len(sel)} scripts?\n\n{listed}{more}"
        if bound:
            keys = ", ".join(_fmt_hotkey(hk) for hk in sorted(bound))
            body += (f"\n\n{keys} would no longer launch anything. "
                     f"The binding will be cleared.")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Delete scripts" if len(sel) > 1 else "Delete script")
        box.setText(body)
        # Recoverable, and the dialog says so — a hand-built flow is worth more
        # than the disk space, and there is no undo anywhere else in the app.
        box.setInformativeText(f"They are moved to {deleted_dir()}, "
                               f"so you can still get them back.")
        box.setStandardButtons(QMessageBox.Cancel)
        yes = box.addButton("Delete", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not yes:
            return

        failed = []
        for path in sel:
            try:
                self._move_to_deleted(path)
            except Exception as e:
                failed.append(f"{path.stem}: {e}")

        if bound and not failed:
            self._clear_bindings(set(bound))
        self._refresh()
        if failed:
            QMessageBox.warning(self, "Could not delete",
                                "\n".join(failed[:8]))

    def _move_to_deleted(self, path: Path):
        """Move one script aside. Never overwrites a previous deletion of the
        same name — the second one would silently destroy the first, which is
        exactly what this folder exists to prevent."""
        dest_dir = deleted_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        if dest.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = dest_dir / f"{path.stem}-{stamp}{path.suffix}"
        path.replace(dest)

    def _clear_bindings(self, hotkeys: set):
        bound = dict(getattr(self._settings.s, "script_hotkeys", None) or {})
        for hk in hotkeys:
            bound.pop(hk, None)
        self._settings.set("script_hotkeys", bound)
        self.bindings_changed = True

    def _open(self):
        sel = self._selected_paths()
        if not sel:
            QMessageBox.information(self, "Pick a script", "Select a script to open.")
            return
        try:
            self.result_graph = flow.FlowGraph.load(sel[0])
            self.result_name = Path(sel[0]).stem
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Open Error", str(e))

    def _merge(self):
        sel = self._selected_paths()
        if len(sel) < 2:
            QMessageBox.information(self, "Pick at least two",
                "Select two or more scripts (Ctrl/Shift-click) to merge them.")
            return
        steps = []
        for p in sel:
            try:
                g = flow.FlowGraph.load(p)
                ls = g.to_linear_steps()
                if ls is None:   # branching flow → flatten its action steps
                    ls = [nn.data["step"] for nn in g.nodes.values()
                          if nn.type == flow.N_ACTION and "step" in nn.data]
                steps.extend(ls)
            except Exception as e:
                QMessageBox.critical(self, "Merge Error", f"{Path(p).name}: {e}")
                return
        if not steps:
            QMessageBox.information(self, "Nothing to merge",
                                    "The selected scripts have no action steps.")
            return
        self.result_graph = flow.FlowGraph.migrate_linear(steps)
        self.result_name = "merged"
        self.accept()


class SequenceTab(QWidget):
    """Visual node-graph builder for automations (Phase 2)."""

    # Deleting a script from the library can clear its launcher-key binding,
    # and the hotkey listener has to be re-armed for that to take effect.
    script_hotkeys_changed = Signal()

    # What the run log keeps. LOG_MAX_ROWS is the smaller of the two on
    # purpose: rows are widgets and cost far more than dicts, and nobody
    # scrolls back thousands of lines in a 320 px panel. Export reads the
    # deque, so it can afford to remember more than the panel shows.
    LOG_MAX_ROWS   = 1500
    LOG_MAX_EVENTS = 5000

    def __init__(self, settings: SettingsManager):
        super().__init__()
        self._settings = settings
        self._recorder = SequenceRecorder()
        self._graph    = self._new_graph()
        # Which bucket of measured node durations belongs to what is on the
        # canvas. Timings are per-flow because a Detect in one script has
        # nothing to say about a Detect in another.
        self._stats_key = runstats.key_for("")
        self._thread: Optional[QThread] = None
        self._worker = None
        # Both of these used to be unbounded, and a fast flow fills them faster
        # than anyone can read them. The deque keeps the tail Export needs; the
        # list widget keeps a shorter tail still, because a QListWidget's cost
        # per row grows with the rows already in it.
        self._run_events = collections.deque(maxlen=self.LOG_MAX_EVENTS)
        self._retired: list = []
        self._recorder.recording_stopped.connect(self._on_recording_stopped)
        self._setup_ui()

        # ── Unsaved-work recovery ──────────────────────────────────
        #
        # The canvas used to be discarded on close; `recovery.py` explains the
        # whole thing. The timer is the half that survives a crash, since
        # `closeEvent` does not run then.
        #
        # ⚠ Twenty seconds, and the write is skipped when the serialised graph
        # is byte-identical to the last one — which, while nobody is editing,
        # is every single tick. The cost of an idle app is therefore one
        # `to_dict()` and one string compare per 20 s, not a file write.
        self._recovery_blob: Optional[str] = None
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setInterval(20_000)
        self._recovery_timer.timeout.connect(self._write_recovery)
        self._recovery_timer.start()

    def _write_recovery(self, force: bool = False):
        """Keep the recovery copy current. Never raises — see `recovery.py`.

        `force` is for the close path, where the timer may be up to twenty
        seconds stale and there is no next tick coming.
        """
        try:
            blob = json.dumps(self._graph.to_dict(), sort_keys=True)
        except Exception:
            return
        if blob == self._recovery_blob and not force:
            return
        if recovery.write(self._graph, self._settings.s.last_sequence_path):
            self._recovery_blob = blob

    def _forget_recovery(self):
        """Called once the flow on disk *is* the work — after a successful Save.

        Also resets the cached blob, so the next edit writes a fresh recovery
        copy instead of being mistaken for "nothing changed since last time".
        """
        recovery.clear()
        self._recovery_blob = None

    @staticmethod
    def _new_graph() -> "flow.FlowGraph":
        g = flow.FlowGraph()
        g.add_node(flow.N_START, {"name": flow.START_NAME}, x=-280, y=-20)
        return g

    # ── UI ─────────────────────────────────────────────────────────
    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # Compact action row — the cosmic header above carries the breadcrumb,
        # so we only need Record / Play here (right-aligned).
        header = QHBoxLayout(); header.setSpacing(8)
        hint = QLabel("Drag a port onto another node to wire · double-click to edit "
                      "· Ctrl+C / Ctrl+V to copy nodes")
        hint.setObjectName("seq_subtitle")
        header.addWidget(hint); header.addStretch(1)
        self._btn_rec = QPushButton("⏺  Record")
        self._btn_rec.setObjectName("btn_record"); self._btn_rec.setMinimumHeight(32)
        self._btn_rec.setToolTip("Capture live input into a chain of action nodes (F8/Esc to stop)")
        self._btn_rec.clicked.connect(self._toggle_record)
        header.addWidget(self._btn_rec)
        self._btn_play = QPushButton("▶  Play")
        self._btn_play.setObjectName("btn_play"); self._btn_play.setMinimumHeight(32)
        self._btn_play.clicked.connect(self._toggle_play)
        header.addWidget(self._btn_play)
        root.addLayout(header)

        # -- Body: left palette sidebar · canvas · run-log panel (the mockup) --
        body = QHBoxLayout(); body.setSpacing(8)

        side = QVBoxLayout(); side.setSpacing(6)
        add_lbl = QLabel("ADD NODE"); add_lbl.setObjectName("seq_section")
        side.addWidget(add_lbl)
        palette_btns = []
        # ⚠ No Auto-Click button, deliberately, since 2.3.2. The Basic face is
        # the auto-clicker now — it writes and owns that node — so a palette
        # entry for it was a second way to build the same thing, sitting in the
        # one place the first way is not.
        #
        # ⚠⚠ The node TYPE is emphatically not gone, and must not be. It is in
        # real saved flows; it is what Basic runs; and it is the only way to
        # click repeatedly on the free tier. A plain Click step has no repeat
        # and no interval — it clicks once — so "click until I stop it" built
        # out of Click needs a Loop, and Loop is in `entitlements.PRO_NODE_TYPES`.
        # Measured: Click-in-Loop reports `runs_on_free False`, Auto-Click
        # reports True. Deleting the node would move the free tier's whole
        # reason to exist behind the paywall. `test_flow.py` pins that.
        for icon, label, emit, family in [
            ("🖱", "Click",     "action:click",      "click"),
            ("⌨", "Type",          "action:key",        "type"),
            ("⏱", "Wait",          "action:wait",       "wait"),
            ("🔍", "Detect",    "action:wait_image", "detect"),
            ("❓", "If / Else",     flow.N_IF,           "if"),
            ("🔁", "Loop",      flow.N_LOOP,         "loop"),
            ("↪️", "Go to",         flow.N_GOTO,         "goto"),
            ("⏹", "End",           flow.N_END,          "end"),
            ("💬", "Comment",       flow.N_FRAME,        "comment"),
        ]:
            b = self._palette_btn(icon, label, emit, family)
            b.clicked.connect(lambda _, t=emit: self._palette_add(t))
            side.addWidget(b)
            palette_btns.append(b)
        side.addStretch(1)
        sidew = QWidget(); sidew.setLayout(side)
        # ⚠ Measured, not written down. This was a fixed 132 px, which fitted the
        # eight labels that existed when it was chosen and clipped the ninth the
        # day one was added — "Comment" lost its last letters. A label is text in
        # a font the theme picks, so the only width that cannot go stale is the
        # one the buttons ask for.
        #
        # ⚠ And the width has to be the BUTTON's, not just the sidebar's. Both
        # earlier attempts widened only `sidew` and left every button asking for
        # a hard-coded 124 px — which is 89 px of content once the style's
        # borders and padding come off, for a "Comment" that lays out at 92.
        # So the label was three pixels from being cut at all times, and stayed
        # whole only for as long as the sidebar happened to be the thing setting
        # the width. Anything that made it not so — a smaller measurement, the
        # 132 px floor winning, a squeeze from the layout — took the last glyph
        # off, silently, because Qt clips rather than complains.
        #
        # `_label_width` is the real requirement (text advance + the style's own
        # chrome, both read from this machine), `PALETTE_SLACK` is margin on top
        # of it, and the result goes on the buttons themselves so nothing
        # downstream can quietly take it away again.
        #
        # ⚠ And it is measured AGAIN on first show — see `_size_palette`. The
        # root cause of the clipped label was that all of this ran before the
        # application stylesheet existed, so it was measuring a 9 pt default-styled
        # button and sizing it for a 14 pt themed one. `main()` now applies the
        # theme first, and re-measuring on show means a future reordering cannot
        # quietly bring the bug back.
        self._palette_btns = palette_btns
        self._palette_side = sidew
        self._size_palette()
        body.addWidget(sidew)

        self._canvas = flow_canvas.FlowCanvas(self._graph)
        self._canvas.node_edit_requested.connect(self._edit_node)
        self._canvas.node_error_requested.connect(self._edit_node_error)
        self._canvas.add_node_requested.connect(self._add_node)

        # The strip lives under the canvas rather than beside it: it shares the
        # canvas's horizontal axis on purpose, so a node's box is roughly under
        # the node it stands for.
        self._timeline = flow_timeline.TimelineStrip(self._graph)
        self._timeline.node_clicked.connect(self._reveal_node)
        self._timeline.set_measured(runstats.medians(self._stats_key))
        # ⚠ Always collapsed at launch, and the fold is NOT remembered. It used
        # to persist as settings.timeline_open, which meant one look at a run's
        # timing cost every later session a strip it never asked for — and the
        # strip is the one run-state view showing nothing the canvas does not
        # already show, since the canvas highlights the running node. Opening it
        # is "let me look at this run", which is a thing you do, not a
        # preference you hold.
        self._timeline.set_collapsed(True)
        canvas_col = _pane()
        cc = QVBoxLayout(canvas_col)
        cc.setContentsMargins(0, 0, 0, 0); cc.setSpacing(0)
        cc.addWidget(self._canvas, 1)
        cc.addWidget(self._timeline)
        body.addWidget(canvas_col, 1)

        self._log_toggle = QPushButton("RUN LOG  ◂")
        self._log_toggle.setObjectName("speedPreset")
        self._log_toggle.setCheckable(True)
        self._log_toggle.setCursor(Qt.PointingHandCursor)
        self._log_toggle.setToolTip("Show / hide the run log")
        self._log_toggle.clicked.connect(self._toggle_log)
        body.addWidget(self._log_toggle)

        self._log_panel = QWidget()
        lp = QVBoxLayout(self._log_panel); lp.setContentsMargins(0, 0, 0, 0); lp.setSpacing(6)
        log_head = QHBoxLayout()
        lh = QLabel("RUN LOG"); lh.setObjectName("seq_section")
        log_head.addWidget(lh); log_head.addStretch(1)
        btn_exp = _btn("Export…", tip="Save the last run report to a file")
        btn_exp.clicked.connect(self._export_log)
        btn_clr = _btn("Clear", tip="Clear the run log")
        btn_clr.clicked.connect(lambda: (self._run_log.clear(), self._run_events.clear()))
        log_head.addWidget(btn_exp); log_head.addWidget(btn_clr)
        lp.addLayout(log_head)
        self._run_log = QListWidget()
        self._run_log.setAlternatingRowColors(True)
        # Every row is the same one-line height, so let Qt skip measuring each
        # one — without this the widget re-lays-out the whole list on insert.
        self._run_log.setUniformItemSizes(True)
        lp.addWidget(self._run_log, 1)
        self._log_panel.setFixedWidth(320)
        self._log_panel.setVisible(False)
        body.addWidget(self._log_panel)
        root.addLayout(body, 1)

        # Footer
        footer = QHBoxLayout(); footer.setSpacing(8)
        footer.addWidget(QLabel("Speed"))
        self._speed_presets = {}
        for _mult in (0.5, 1.0, 2.0, 5.0):
            _pb = QPushButton(f"{_mult:g}×")
            _pb.setObjectName("speedPreset"); _pb.setCheckable(True)
            _pb.setCursor(Qt.PointingHandCursor); _pb.setFixedWidth(46)
            _pb.clicked.connect(lambda _=False, m=_mult: self._set_speed_preset(m))
            self._speed_presets[_mult] = _pb
            footer.addWidget(_pb)
        self._speed = _dspin(0.1, 50.0, self._settings.s.seq_speed, 0.1, "×")
        # Speed steps by 0.1, so the second decimal never carried information —
        # it only made the box wide enough for "50,00×". One decimal fits the
        # widest value ("50,0×") in ~102 px instead of 120.
        self._speed.setDecimals(1)
        _fit_spin(self._speed, "50.0×", pad=0)
        self._speed.setToolTip("Custom playback speed (2× = twice as fast). "
                               "Use the arrows to fine-tune.")
        footer.addWidget(self._speed)
        self._speed.valueChanged.connect(self._sync_speed_presets)
        self._sync_speed_presets(self._speed.value())
        footer.addStretch(1)
        for text, kind, tip, fn in [
            ("Fit",  None, "Fit the whole script in view", self._canvas.fit),
            ("Overall settings", None,
                               "Change delays, detection timeouts, match confidence "
                               "and error handling across the whole script at once",
             self._bulk_edit),
            ("Library", None, "Browse and merge your saved scripts", self._open_library),
            ("Save", None, "Save this script to a .json file", self._save),
            ("Clear", "danger", "Remove all nodes", self._clear),
        ]:
            b = _btn(text, kind, tip=tip) if kind else _btn(text, tip=tip)
            b.clicked.connect(fn)
            footer.addWidget(b)
        root.addLayout(footer)

    def _palette_btn(self, icon, label, emit, family):
        """One Add-node button. `emit` is what `_palette_add` will be handed:
        either a bare node type, or "action:<kind>" for a step."""
        b = _PaletteButton(f"{icon}   {label}", emit)
        b.setObjectName("palette_btn")
        col = flow_canvas.FAMILY_COLORS.get(family, "#3b82f6")
        b.setStyleSheet(
            "QPushButton#palette_btn{border-left:4px solid %s;}"
            "QPushButton#palette_btn:hover{border-color:%s;}" % (col, col))
        tip = f"Add a {label} node  ·  drag onto the canvas to place it"
        # Say it here rather than only at Play. The button still adds the node —
        # someone has to be able to build the thing they would be buying — but
        # finding out a step is paid *after* designing a flow around it is a
        # worse way to learn it than a tooltip.
        #
        # ⚠ Read at build time, so the label itself is left alone: it feeds
        # `_label_width`, and the palette's width machinery has already been the
        # cause of four separate clipped-label bugs. A tooltip cannot clip.
        # ⚠ And never while nothing is enforced — every step runs, so a
        # tooltip saying otherwise would be false.
        if (entitlements.ENFORCED and _palette_entry_is_pro(emit)
                and not licensing.is_pro()):
            tip += f"\n\nPart of Macronaut Pro — {licensing_ui.PRICE}, once."
        b.setToolTip(tip)
        b.setMinimumHeight(34); b.setMinimumWidth(124)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _size_palette(self):
        """Fit the Add-node sidebar to the labels it actually has to draw.

        ⚠ Called at build time *and* from `showEvent`, and the second call is
        the one that matters. Widths measured before the application stylesheet
        is in force are measured in Qt's default 9 pt font with the default
        padding — far narrower than the themed 14 pt button that eventually gets
        painted — and a width frozen from that measurement clips its own label
        forever. `main()` applies the theme before building anything now; this
        is the belt to that braces, so the bug cannot come back by reordering.
        """
        btns = getattr(self, "_palette_btns", None)
        side = getattr(self, "_palette_side", None)
        if not btns or side is None:
            return
        lm, _t, rm, _b = side.layout().getContentsMargins()
        need = max(_label_width(b) for b in btns) + PALETTE_SLACK
        for b in btns:
            b.setMinimumWidth(need)
        side.setFixedWidth(max(132, need + lm + rm))

    def showEvent(self, e):
        super().showEvent(e)
        self._size_palette()
        # ⚠ And again on the next turn of the event loop. Measured: at showEvent
        # time a widget whose application stylesheet changed after it was built
        # has NOT been repolished yet, so it still answers with the old font's
        # metrics and the old style's padding — re-measuring here alone moved
        # the button from 111 px to 114 when it needed 127. One deferred call
        # lands after the repolish, when the numbers are finally the ones that
        # will be painted. A no-op in the normal order, where they already are.
        QTimer.singleShot(0, self._size_palette)

    # ── node add / edit ────────────────────────────────────────────
    def _palette_add(self, emit: str):
        if emit == flow.N_FRAME and self._canvas.selected_node_ids():
            # Clicking the button with something selected means "put a comment
            # round this" — which is what asking for one almost always means,
            # and is why every other editor binds it to a key. *Dragging* the
            # button to a spot is the other request and lands in _add_node,
            # which places one there instead.
            if self._canvas.wrap_selection_in_frame() is not None:
                return
        c = self._canvas.mapToScene(self._canvas.viewport().rect().center())
        self._add_node(emit, c.x(), c.y())

    def _add_node(self, ntype: str, x: float, y: float):
        # Palette/menu encode action families as "action:<kind>".
        preset_kind = None
        if ":" in ntype:
            ntype, preset_kind = ntype.split(":", 1)
        scene = self._canvas.scene_()
        if ntype == flow.N_FRAME:
            # A comment box has no ports, so there is nothing to chain it onto
            # and no step editor to open — it asks for its text instead, and
            # only then places the box, so cancelling leaves nothing behind.
            # (Wrapping the selection is _palette_add's job; arriving here means
            # a position was named, by a drop or a right-click.)
            self._canvas.new_frame_at(x, y)
            return
        # Auto-place + auto-connect after the most-recently-added node (#16).
        prev = scene.node_item(scene._last_added) if scene._last_added else None
        if prev is None:
            # Nothing has been added yet: chain onto Start instead. The first
            # node dropped on an empty canvas belongs on the end of the entry
            # point, not floating beside it waiting to be wired by hand.
            start = self._graph.start_node()
            if start is not None and not self._graph.out_edge(start.id, "out"):
                prev = scene.node_item(start.id)
        # Record the family the palette asked for *before* the editor opens, so
        # the node on the canvas already looks like a Detect (or Type, or Wait)
        # while you are still configuring it. Dropped again the moment a real
        # step is saved — see _edit_node.
        item = scene.add_node(ntype, x, y,
                              {"preset_kind": preset_kind} if preset_kind else None)
        if prev is not None:
            # Line the new node up on `prev`'s *centre*, not its top edge —
            # Start and End are short bars, so equal `y` would leave the wire
            # entering at a slant. Placed after the item exists so its own
            # height is known rather than assumed.
            item.setPos(prev.node.x + flow_canvas.NODE_W + 60,
                        prev.node.y + prev.boundingRect().height() / 2
                        - item.boundingRect().height() / 2)
            outs = prev.node.ports()
            if outs:
                scene.connect_ports(prev.node.id, outs[0], item.node.id)
        # New nodes carry NO pre-delay. FIXES #12 used to seed control nodes with
        # 500 ms, which meant every If/Loop/Goto/End silently cost half a second
        # nobody asked for. A delay is now something you add on purpose
        # (right-click → Delay before…), not something you have to remember to
        # remove.
        scene._last_added = item.node.id
        self._edit_node(item.node.id, is_new=True, preset_kind=preset_kind)

    def _labels(self):
        return [n.data.get("name", "") for n in self._graph.nodes.values()
                if n.type == flow.N_LABEL and n.data.get("name")]

    def _edit_node(self, nid: str, is_new: bool = False, preset_kind: str = None):
        node = self._graph.nodes.get(nid)
        if not node:
            return
        t = node.type
        scene = self._canvas.scene_()
        if t == flow.N_ACTION:
            stepdict = node.data.get("step")
            kind = preset_kind or (stepdict.get("kind") if stepdict else None)
            if kind == "autoclick":
                # This node used to be edited on the Basic face, and when that
                # went, double-clicking it answered with a message box pointing
                # at a face that no longer existed. It has its own editor now —
                # it is a real node in real saved flows, and the one node you
                # cannot open is exactly the trap this codebase already knows
                # about.
                dlg = flow_dialogs.AutoClickDialog(
                    (stepdict or {}).get("data", {}), parent=self)
                if dlg.exec() == QDialog.Accepted:
                    node.data["step"] = {"kind": "autoclick",
                                         "data": dlg.result_data()}
                    node.data.pop("preset_kind", None)   # the real step supersedes it
                elif is_new and not node.data.get("step"):
                    # Same contract as the StepDialog branch below: cancelling
                    # the editor of a node that was created to be edited means
                    # "never mind", not "leave an unconfigured node behind".
                    scene.delete_node(nid)
                    return
                scene.refresh_node(nid)
                return
            family = flow_canvas.family_for_kind(kind) if kind else None
            step = SeqStep.from_dict(stepdict) if stepdict else None
            dlg = StepDialog(step, parent=self,
                             default_text_cps=self._settings.s.typing_speed_cps,
                             family=family)
            if dlg.exec() == QDialog.Accepted and dlg.step:
                node.data["step"] = dlg.step.to_dict()
                node.data.pop("preset_kind", None)   # the real step supersedes it
                # No automatic pre-delay — see _add_node.
            elif is_new and not node.data.get("step"):
                scene.delete_node(nid)
                return
        elif t == flow.N_IF:
            dlg = flow_dialogs.IfDialog(node.data, self)
            if dlg.exec() == QDialog.Accepted:
                node.data.update(dlg.data())
        elif t == flow.N_LOOP:
            dlg = flow_dialogs.LoopDialog(node.data, self)
            if dlg.exec() == QDialog.Accepted:
                node.data.update(dlg.data())
        elif t == flow.N_GOTO:
            dlg = flow_dialogs.GotoDialog(node.data, scene.node_names(), self)
            if dlg.exec() == QDialog.Accepted:
                node.data.update(dlg.data())
        scene.refresh_node(nid)

    def _bulk_edit(self):
        """One setting, changed across the whole flow — see BulkEditDialog."""
        scene = self._canvas.scene_()
        selected = self._canvas.selected_node_ids()
        dlg = flow_dialogs.BulkEditDialog(len(self._graph.nodes), len(selected), self)
        if dlg.exec() != QDialog.Accepted:
            return
        ops = dlg.ops()
        if not ops:
            QMessageBox.information(self, "Overall settings",
                                    "No row was ticked, so nothing changed.")
            return
        ids = selected if dlg.selection_only() else list(self._graph.nodes.keys())
        n = flow.bulk_apply(self._graph, ids, ops)
        for nid in ids:
            scene.refresh_node(nid)
        QMessageBox.information(
            self, "Overall settings",
            f"Updated {n} node{'' if n == 1 else 's'}."
            if n else "Nothing to change — no node in range uses those settings.")

    def _edit_node_error(self, nid: str):
        node = self._graph.nodes.get(nid)
        if not node or node.type != flow.N_ACTION:
            return
        dlg = flow_dialogs.OnErrorDialog(node.data.get("on_error", {}),
                                         self._canvas.scene_().node_names(), self)
        if dlg.exec() == QDialog.Accepted:
            node.data["on_error"] = dlg.data()
            self._canvas.scene_().refresh_node(nid)

    # ── recording ──────────────────────────────────────────────────
    def _toggle_record(self):
        if self._recorder.is_recording:
            self._recorder.stop()
        else:
            self._btn_rec.setText("⏹  Stop")
            self._btn_rec.setStyleSheet(
                "background:#ef4444;color:#fff;font-weight:700;border:none;border-radius:9px;")
            self._btn_play.setEnabled(False)
            self._recorder.start()

    def _on_recording_stopped(self):
        steps = self._recorder.steps
        self._btn_rec.setText("⏺  Record"); self._btn_rec.setStyleSheet("")
        self._btn_play.setEnabled(True)
        if not steps:
            return
        scene = self._canvas.scene_()
        xs = [n.x for n in self._graph.nodes.values()] or [0]
        x = max(xs) + 240
        y = -20.0
        prev, first = None, None
        for st in steps:
            item = scene.add_node(flow.N_ACTION, x, y, {"step": st.to_dict()})
            if prev:
                scene.connect_ports(prev, "out", item.node.id)
            else:
                first = item.node.id
            prev = item.node.id
            y += 104
        start = self._graph.start_node()
        if start and first and not self._graph.out_edge(start.id, "out"):
            scene.connect_ports(start.id, "out", first)
        if prev:
            scene._last_added = prev   # chain future manual adds after the tail
        self._canvas.fit()

    # ── playback ───────────────────────────────────────────────────
    def has_content(self) -> bool:
        return flow.has_work(self._graph)

    def is_playing(self) -> bool:
        if self._thread is not None and self._thread.isRunning():
            return True
        # A retired run can still be executing: stop_playback() waits only 1.5 s
        # and _on_playback_done() clears the references while the worker may
        # still be on the CPU. Reporting "not playing" there let Play start a
        # *second* worker alongside the first.
        #
        # ⚠ The original reason was that "a detection step (OCR, matcher.find,
        # _grab) is not interruptible". `matcher.find` is, as of 4 September
        # 2026 — it takes a `should_continue` and asks between template scales,
        # which is what made Stop responsive during a Detect. That was the long
        # one, at a second or two. OCR (~33 ms cold, ~4 ms warm) and a screen
        # grab are still indivisible and still short. So this net catches a
        # narrower case than it used to, and is kept because "narrower" is not
        # "none" — a worker can also be mid-click or mid-sleep.
        return any(t is not None and t.isRunning() for t, _w in self._retired)

    @property
    def graph(self):
        return self._graph

    def refresh_licence_state(self):
        """Redraw the things whose appearance depends on the licence.

        Two of them, and neither notices on its own: the canvas draws a PRO chip
        per node (a repaint re-reads the licence, but nothing was asking it to
        repaint), and the palette's tooltips were written at build time.

        ⚠ Tooltips are rebuilt rather than patched, because the *label* is
        deliberately not touched by any of this — it feeds `_label_width`, and
        the palette's sizing has already caused four separate clipped-label
        bugs. Nothing here may change a button's text.
        """
        canvas = getattr(self, "_canvas", None)
        if canvas is not None:
            canvas.viewport().update()
        for b in getattr(self, "_palette_btns", []) or []:
            tip = b.toolTip().split("\n\n")[0]
            emit = getattr(b, "_ntype", "")
            if (emit and entitlements.ENFORCED and _palette_entry_is_pro(emit)
                    and not licensing.is_pro()):
                tip += f"\n\nPart of Macronaut Pro — {licensing_ui.PRICE}, once."
            b.setToolTip(tip)

    def _licensed_to_run(self, g) -> bool:
        """The licence gate. This is the only one in the app, deliberately.

        It sits here — after "is there anything to run?" and before a single
        thread, worker or input event exists — because that is the last moment
        at which refusing costs the user nothing. A check any later leaves the
        mouse somewhere they did not put it; a check any earlier (on load, on
        edit, on save) would take away their ability to work on a flow they are
        perfectly entitled to own.

        ⚠ Re-asks `entitlements.check` after an activation rather than assuming
        the dialog's success means this flow may now run. They are different
        questions the moment there is ever more than one kind of licence, and the
        version of this that assumes is the version that silently stops
        enforcing anything.
        """
        allowed, reason, features = entitlements.check(g)
        if allowed:
            return True
        if not licensing_ui.prompt_for_upgrade(self, reason, features):
            return False
        self.refresh_licence_state()
        allowed, _, _ = entitlements.check(g)
        return allowed

    def start_playback(self, graph=None):
        """Play `graph`, or the canvas graph when it is None.

        A launcher hotkey passes its own graph so running a bound script never
        touches the canvas — loading it into `self._graph` would silently
        discard whatever unsaved editing was in progress.
        """
        detached = graph is not None
        g = graph if detached else self._graph
        if self.is_playing() or not flow.has_work(g):
            return None
        if not self._licensed_to_run(g):
            return None
        speed = self._speed.value()
        worker = flow_exec.FlowWorker(
            g.clone(),             # snapshot: live graph stays editable mid-run
            speed_factor=1.0 / max(0.1, speed),
            blacklist=self._settings.s.keystroke_blacklist)
        t = QThread()
        worker.moveToThread(t)
        t.started.connect(worker.run)
        worker.finished.connect(t.quit)
        # The canvas is showing a different flow during a detached run, so its
        # node ids are not ours — highlighting them would light nothing at best
        # and the wrong node at worst.
        if not detached:
            worker.node_started.connect(self._canvas.highlight)
            worker.node_started.connect(self._on_node_started)
            worker.held_changed.connect(self._on_held_changed)
            self._timeline.set_speed(speed)
            self._timeline.run_started()
            # Only a non-detached run. A launcher run is a different graph with
            # its own node ids, and folding those into this flow's bucket would
            # invent measurements for nodes that were never run.
            worker.timings_ready.connect(self._on_timings)
        worker.node_started.connect(self._note_node)
        worker.log_batch.connect(self._on_log_batch)
        worker.finished.connect(self._on_playback_done)
        self._run_log.clear(); self._run_events.clear()
        # A crash during a run is the failure mode this whole thing exists for,
        # so the report should say what was running. Node count and backend,
        # not the script's contents.
        crashreport.breadcrumb("run_start", nodes=len(g.nodes),
                               speed=speed, detached=detached,
                               backend=self._settings.s.input_backend)
        if not self._log_panel.isVisible():
            self._toggle_log()
        t.start()
        self._thread, self._worker = t, worker
        self._btn_play.setText("⏹  Stop")
        self._btn_rec.setEnabled(False)
        return worker

    def _toggle_log(self):
        show = not self._log_panel.isVisible()
        self._log_panel.setVisible(show)
        self._log_toggle.setChecked(show)
        self._log_toggle.setText("RUN LOG  ▸" if show else "RUN LOG  ◂")

    def _set_speed_preset(self, mult: float):
        self._speed.setValue(mult)

    def _sync_speed_presets(self, val: float):
        for m, btn in getattr(self, "_speed_presets", {}).items():
            btn.setChecked(abs(val - m) < 1e-6)

    def stop_playback(self):
        # Stop-while-detecting is the exact path that used to abort the process,
        # so a crash report wants to know Stop was pressed and that the thread
        # had not finished when it was.
        crashreport.breadcrumb(
            "stop_requested",
            running=bool(self._thread is not None and self._thread.isRunning()))
        if self._worker:
            self._worker.request_stop()
        # Retired-but-still-running workers must hear it too. `if self._worker:`
        # alone could not reach one, so a run that outlived its 1.5 s wait kept
        # going with no way left to stop it.
        for _t, w in list(self._retired):
            if w is not None:
                w.request_stop()
        t = self._thread
        if t is not None and t.isRunning():
            t.quit()
            # A short courtesy wait, and nothing hangs on it: the thread can
            # still be inside something indivisible whatever deadline we pick.
            # ⚠ Image matching used to be the worst of those at a second or
            # two; it takes a stop callback now. What is left is short — an OCR
            # read, a screen grab, one click — so this wait succeeds far more
            # often than it did, and must still not be relied on. This
            # used to wait 3 s and then drop the reference regardless — and
            # destroying a *running* QThread is a Qt abort, not an exception,
            # which is why Stop could take the whole app with it.
            #
            # Not when called *from* that thread: a thread waiting on itself
            # returns instantly and Qt only warns, so the wait would look like
            # it had succeeded. _retire() below is what actually keeps the pair
            # alive, so skipping the wait costs nothing but the pause.
            if QThread.currentThread() is not t:
                t.wait(1500)
        self._on_playback_done()

    def _retire(self, thread, worker):
        """Let a run's QThread and FlowWorker go, whenever it is safe to.

        Both objects are held here until the thread genuinely finishes. Nothing
        used to release the worker at all, so the next Play dropped a QObject
        whose thread affinity pointed at a dead QThread — undefined behaviour,
        and the other half of the random crashes.
        """
        if thread is None and worker is None:
            return
        pair = (thread, worker)
        self._retired.append(pair)

        def _release():
            if worker is not None:
                worker.deleteLater()
            try:
                self._retired.remove(pair)
            except ValueError:
                pass

        # isRunning(), not isFinished(): a QThread that was never started is
        # neither running nor finished, and waiting for a finished signal it
        # will never send would hold the pair forever.
        if thread is None or not thread.isRunning():
            _release()
        else:
            # Queued so the release lands on the GUI thread rather than on the
            # dying worker thread — a plain callable has no receiver context,
            # so Qt would otherwise run it wherever finished() was emitted.
            thread.finished.connect(_release, Qt.QueuedConnection)

    def shutdown(self):
        """Stop playback and recording so no worker or input-listener thread
        survives application close."""
        try:
            self.stop_playback()
        except Exception:
            pass
        try:
            if self._recorder.is_recording:
                self._recorder.stop()
        except Exception:
            pass

    def _toggle_play(self):
        if self.is_playing():
            self.stop_playback()
            return
        if not self.has_content():
            QMessageBox.information(self, "Nothing to play",
                                    "Add an Action node (or record one) first.")
            return
        self.start_playback()

    def _on_node_started(self, nid: str):
        """One signal per node — the bars animate themselves from here.

        Nothing streams progress. The interpreter can enter nodes far faster
        than any widget can repaint (the 2.0.8 flood was 692k events/sec), so
        the canvas and the strip are told *what* started and work out the rest
        from flow.estimate plus a local 30 Hz timer.
        """
        self._canvas.begin_node(nid, self._speed.value(),
                                self._timeline._measured)
        self._timeline.node_started(nid)

    def _on_held_changed(self, pairs: list):
        by_node: dict = {}
        for key, nid in pairs:
            by_node.setdefault(nid, []).append(key)
        self._canvas.set_live(by_node)
        self._timeline.set_held(pairs)

    def _on_timings(self, timings: dict):
        """Fold one run's measurements in; the strip's Time axis gets realer."""
        try:
            self._timeline.set_measured(
                runstats.record(self._stats_key, timings))
        except Exception:
            pass    # a timeline that cannot remember is still a timeline

    def _on_playback_done(self, *_):
        self._btn_play.setText("▶  Play")
        self._btn_rec.setEnabled(True)
        self._canvas.highlight(None)
        self._canvas.end_run()
        self._timeline.run_finished()
        # Reached both from the worker's finished signal and from Stop, so it
        # has to be idempotent — hence clearing before retiring.
        t, w = self._thread, self._worker
        self._thread = self._worker = None
        self._retire(t, w)
        crashreport.breadcrumb("run_end")

    def _note_node(self, nid):
        """Tell the crash reporter which node is executing.

        Deliberately `state()` and not `breadcrumb()`: this fires once per node
        entered, which in a tight loop is hundreds of thousands a second.
        state() overwrites one small file and throttles itself, so the cost does
        not scale with how fast the flow runs — while a breadcrumb here would be
        the 2.0.8 log-flood bug rebuilt on disk.
        """
        node = self._graph.nodes.get(nid) if nid else None
        crashreport.state(node=nid or "",
                          kind=getattr(node, "type", "") if node else "")

    # ── run log ────────────────────────────────────────────────────
    def _on_log_batch(self, evs: list):
        """Absorb one coalesced batch of run-log events.

        Per-event this used to be an append plus a scrollToBottom, which is why
        the panel slowed from ~200 rows/sec to ~50 as it filled while the
        interpreter kept producing hundreds of thousands a second. One batch is
        now one addItems, one trim and at most one scroll.
        """
        self._run_events.extend(evs)
        lines = [ln for ln in (self._fmt_log(e) for e in evs) if ln]
        if not lines:
            return
        bar = self._run_log.verticalScrollBar()
        # Only follow the tail if the reader was already at it — scrolling back
        # to look at something should not be yanked away on the next batch.
        at_bottom = bar.value() >= bar.maximum() - 2
        self._run_log.addItems(lines)
        for _ in range(max(0, self._run_log.count() - self.LOG_MAX_ROWS)):
            self._run_log.takeItem(0)
        if at_bottom:
            self._run_log.scrollToBottom()

    @staticmethod
    def _fmt_log(ev: dict) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(ev.get("t", time.time())))
        k = ev.get("kind")
        if k == "run_start":
            return f"[{ts}]  ▶ Run started"
        if k == "backend":
            return (f"[{ts}]  ⌨ input: keyboard={ev.get('keyboard')}, "
                    f"mouse={ev.get('mouse')}")
        if k == "run_end":
            return f"[{ts}]  ⏹ Run ended — {ev.get('status')}  ({ev.get('steps')} steps)"
        if k == "node_enter":
            nm, desc = ev.get("name") or "", ev.get("desc", "")
            label = f"{nm} — {desc}" if nm and desc and nm != desc else (nm or desc)
            return f"[{ts}]  ▸ {label}"
        if k == "action":
            nm, desc = ev.get("name") or "", ev.get("desc", "") or "action"
            body = f"{nm}: {desc}" if nm else desc
            return f"[{ts}]      {'✓' if ev.get('ok') else '✗'} {body}" + \
                   (f" (try {ev.get('attempt')})" if ev.get('attempt', 1) > 1 else "")
        if k == "branch":
            return f"[{ts}]      → {ev.get('port')} ({ev.get('result')})"
        if k == "loop_iter":
            return f"[{ts}]      ↻ iteration {ev.get('iter')}"
        if k == "loop_done":
            return f"[{ts}]      ↺ loop done ({ev.get('iters')} iters)"
        if k == "retry":
            return f"[{ts}]      ⟳ retry {ev.get('attempt')}/{ev.get('of')}"
        if k == "recover":
            return f"[{ts}]      ⚠ recover via {ev.get('via')}"
        if k == "goto":
            return f"[{ts}]      ↪ goto {ev.get('target')}"
        if k == "set_var":
            return f"[{ts}]      {ev.get('name')} = {ev.get('value')}"
        if k == "abort":
            return f"[{ts}]  ✗ ABORT — {ev.get('reason','')}"
        if k == "error":
            return f"[{ts}]  ! error: {ev.get('msg','')}"
        if k == "dropped":
            # Emitted by the worker's batcher. Saying so beats a log that is
            # quietly missing most of a fast loop.
            return (f"[{ts}]  … {ev.get('n', 0):,} more events — the script is "
                    f"running faster than the log can show")
        return ""

    def _export_log(self):
        if not self._run_events:
            QMessageBox.information(self, "Empty", "No run log to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export run log", "run_log.txt",
                                              "Text (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for ev in self._run_events:
                    f.write(self._fmt_log(ev) + "\n")
        except Exception as e:
            QMessageBox.critical(self, "Export error", str(e))

    # ── save / load / clear ────────────────────────────────────────
    def _open_library(self):
        dlg = ScriptLibraryDialog(self, self._settings)
        accepted = dlg.exec() == QDialog.Accepted
        if dlg.bindings_changed:
            # Deleting a bound script clears its launcher key. The listener
            # watches a fixed set of keys, so without this the key stays armed
            # and falls through to Start/Stop — worse than doing nothing.
            self.script_hotkeys_changed.emit()
        if accepted and dlg.result_graph is not None:
            self._graph = dlg.result_graph
            self._canvas.set_graph(self._graph)
            self._canvas.fit()
            self._set_stats_key(dlg.result_name)

    def _set_stats_key(self, name: str):
        """Point the strip at this flow's own measurements."""
        self._stats_key = runstats.key_for(name)
        self._timeline.set_graph(self._graph)
        self._timeline.set_measured(runstats.medians(self._stats_key))

    def _reveal_node(self, nid: str):
        """Clicking a box in the strip selects and centres that node."""
        self._canvas.highlight(nid)
        item = self._canvas._scene.node_item(nid)
        if item is not None:
            self._canvas.centerOn(item)

    def _save(self):
        if not self._graph.nodes:
            QMessageBox.information(self, "Empty", "Nothing to save.")
            return
        default_path = self._settings.s.last_sequence_path or str(scripts_dir())
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Script", default_path,
            "JSON Scripts (*.json)")
        if path:
            try:
                self._graph.save(path)
                self._settings.set("last_sequence_path", path)
                # Saving under a name is what gives this flow its own bucket of
                # measurements; until then it shares the unsaved one.
                self._set_stats_key(Path(path).stem)
                # The work is on disk under a name the user chose, so there is
                # nothing left to recover and the offer must not appear next
                # launch. This is half of the "when does it stop offering"
                # rule; the other half is answering the dialog.
                self._forget_recovery()
            except Exception as e:
                # ⚠ Say that nothing was lost, because since 4 September 2026
                # that is true and it is the only thing the person in front of
                # this box actually needs. `FlowGraph.save` writes beside the
                # target and moves it over, so a failure leaves any previous
                # version of the file exactly as it was — and leaves the flow
                # on the canvas untouched either way, which is the copy that
                # matters most.
                existed = Path(path).exists()
                QMessageBox.critical(
                    self, "Save Error",
                    f"Couldn't save to {Path(path).name}.\n\n{e}\n\n"
                    + ("The version already on disk has not been changed, and "
                       "your script is still open here — try somewhere else, or "
                       "close whatever else is using the file."
                       if existed else
                       "Your script is still open here and nothing has been "
                       "lost. Try a different folder — this one may be "
                       "read-only or full."))

    # There is no _load here on purpose. It opened a file picker and replaced
    # the graph — which is exactly what the Library's Open does, from a list
    # that shows what each script *is* instead of a folder full of names. The
    # one thing a picker could still do (reach a .json outside the library) is
    # the Library's Import…, which copies it in first, so the file is findable
    # again next time instead of only from the last-used path.

    def _clear(self):
        if len(self._graph.nodes) <= 1:
            return
        r = QMessageBox.question(self, "Clear Script", "Delete all nodes?",
                                 QMessageBox.Yes | QMessageBox.No)
        if r == QMessageBox.Yes:
            self._graph = self._new_graph()
            self._canvas.set_graph(self._graph)
            self._set_stats_key("")

    # ── misc / compat ──────────────────────────────────────────────
    @property
    def steps(self):
        return []

    def save_to_settings(self):
        self._settings.s.seq_speed = self._speed.value()


# ═══════════════════════════════════════════════════════════════════════════════
#  Tab: Settings
# ═══════════════════════════════════════════════════════════════════════════════

class SettingsTab(QWidget):
    hotkey_changed     = Signal(str)
    trigger_changed    = Signal(str)
    appearance_changed = Signal(str)   # theme name
    always_on_top_changed = Signal(bool)
    failsafe_changed   = Signal()       # panic / window-guard settings
    script_hotkeys_changed = Signal()   # launcher-key bindings
    # Activating or removing a key changes what the CANVAS draws (the PRO chips)
    # and what the palette's tooltips say — neither of which lives in this tab.
    licence_changed    = Signal()

    def __init__(self, settings: SettingsManager):
        super().__init__()
        self._settings = settings
        self._setup_ui()
        # After the page exists and before anything is loaded into it: the wheel
        # must scroll this page, never edit the control it passes over.
        _guard_wheel(self)
        self._load()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll, col = _scroll_page(720)
        root.addWidget(scroll)

        head = QLabel("Settings")
        head.setObjectName("h1")
        col.addWidget(head)
        col.addWidget(_hint("Global options that apply everywhere. Changes are saved automatically."))

        col.addWidget(self._hotkey_group())
        col.addWidget(self._script_hotkeys_group())
        col.addWidget(self._failsafe_group())
        col.addWidget(self._region_group())
        col.addWidget(self._focus_group())
        col.addWidget(self._blacklist_group())
        col.addWidget(self._pointer_group())
        col.addWidget(self._keystroke_group())
        col.addWidget(self._appearance_group())
        col.addWidget(self._licence_group())
        col.addWidget(self._updates_group())
        col.addWidget(self._crash_group())
        col.addWidget(self._about_group())
        col.addStretch(1)

        self._use_region.toggled.connect(lambda *_: self._update_enabled_states())
        self._use_focus.toggled.connect(lambda *_: self._update_enabled_states())
        self._smooth_mouse.toggled.connect(lambda *_: self._update_enabled_states())

    def _update_enabled_states(self):
        on = self._use_region.isChecked()
        for sp in (self._reg_x, self._reg_y, self._reg_w, self._reg_h):
            sp.setEnabled(on)
        self._focus_title.setEnabled(self._use_focus.isChecked())
        self._travel_speed.setEnabled(self._smooth_mouse.isChecked())

    def _hotkey_group(self) -> QGroupBox:
        gb, v = _card("Hotkeys",
                      "Keyboard shortcuts that start and stop automation even when the app is in "
                      "the background. Click “Capture…”, then press the key you want.")

        self._hk_startstop = QLineEdit(); self._hk_startstop.setFixedWidth(150)
        cap1 = _btn("Capture…", tip="Press a key to use as Start/Stop")
        cap1.clicked.connect(lambda: self._capture_hotkey(self._hk_startstop))
        v.addWidget(_field("Start / Stop", self._hk_startstop, cap1))

        self._hk_trigger = QLineEdit(); self._hk_trigger.setFixedWidth(150)
        self._hk_trigger.setPlaceholderText("optional")
        cap2 = _btn("Capture…", tip="A second key that also starts/stops")
        cap2.clicked.connect(lambda: self._capture_hotkey(self._hk_trigger))
        v.addWidget(_field("Extra trigger key", self._hk_trigger, cap2))
        v.addWidget(_hint("The extra trigger is optional — leave it blank to use only the main hotkey."))

        apply_row = QHBoxLayout(); apply_row.setContentsMargins(0, 0, 0, 0)
        btn_apply = _btn("Apply hotkeys", "primary", tip="Activate the keys above")
        btn_apply.clicked.connect(self._apply_hotkeys)
        apply_row.addWidget(btn_apply)
        apply_row.addStretch(1)
        aw = QWidget(); aw.setObjectName("fieldRow"); aw.setLayout(apply_row)
        v.addWidget(aw)
        return gb

    # ── Script launcher keys ──────────────────────────────────────
    def _script_names(self) -> List[str]:
        try:
            return sorted(p.stem for p in scripts_dir().glob("*.json"))
        except Exception:
            return []

    def _script_hotkeys_group(self) -> QGroupBox:
        gb, v = _card("Script launcher keys",
                      "Give a saved script its own key. Pressing it runs that script "
                      "wherever you are; pressing it again stops it. Pressing a "
                      "different one switches — only one script runs at a time, "
                      "because two would be sharing one mouse and one keyboard.")

        self._sh_rows: List[tuple] = []
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        self._sh_body = body
        v.addWidget(_pane(body))

        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(8)
        add = _btn("＋  Add binding", tip="Bind another script to a key")
        add.clicked.connect(lambda: self._add_script_hotkey_row())
        ap = _btn("Apply launcher keys", "primary", tip="Activate the bindings above")
        ap.clicked.connect(self._apply_script_hotkeys)
        row.addWidget(add); row.addWidget(ap); row.addStretch(1)
        v.addWidget(_pane(row))

        v.addWidget(_hint(
            "F13–F24 are the safest keys to bind: no application uses them, so they "
            "can never collide with something you meant to type. Macro keys (G1, G2…) "
            "usually need remapping in your keyboard's own software first — many send "
            "an ordinary key such as “1”, which Windows cannot tell apart from the "
            "real one."))
        return gb

    def _add_script_hotkey_row(self, hk: str = "", name: str = ""):
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(8)
        key = QLineEdit(hk)
        key.setFixedWidth(120)
        key.setPlaceholderText("no key")
        cap = _btn("Capture…", min_h=30, tip="Press the key you want to bind")
        cap.clicked.connect(lambda _=False, e=key: self._capture_hotkey(e))
        combo = QComboBox()
        combo.addItem("— none —")
        combo.addItems(self._script_names())
        if name:
            i = combo.findText(name)
            if i < 0:
                # A binding can outlive its script. Keep the name visible so it can
                # be re-pointed instead of vanishing into "— none —" on next save.
                combo.addItem(f"{name}  (missing)")
                combo.setCurrentIndex(combo.count() - 1)
            else:
                combo.setCurrentIndex(i)
        holder = _pane(row)
        rm = _btn("✕", "ghost", min_h=30, tip="Remove this binding")
        rm.clicked.connect(lambda _=False, w=holder: self._remove_script_hotkey_row(w))
        row.addWidget(key); row.addWidget(cap); row.addWidget(combo, 1); row.addWidget(rm)
        self._sh_body.addWidget(holder)
        self._sh_rows.append((holder, key, combo))
        # These rows are built after the page is, so the constructor's sweep
        # never saw them — and this is the worst combo in the app to change by
        # accident: it decides which script a launcher key runs.
        _guard_wheel(holder)

    def _remove_script_hotkey_row(self, holder: QWidget):
        self._sh_rows = [r for r in self._sh_rows if r[0] is not holder]
        holder.setParent(None)
        holder.deleteLater()

    def _collect_script_hotkeys(self) -> tuple:
        """Rows -> ({hotkey: script}, [problem, ...]). Rows that are half-filled
        are skipped silently; a row that would break something reports why."""
        s = self._settings.s
        reserved = {
            (s.start_stop_hotkey or "").lower().strip(): "Start/Stop",
            (s.trigger_key or "").lower().strip(): "the extra trigger key",
            (s.panic_hotkey or "").lower().strip(): "the panic key",
        }
        reserved.pop("", None)
        out, problems = {}, []
        for _holder, key, combo in self._sh_rows:
            hk = key.text().lower().strip()
            name = combo.currentText().strip()
            if name.endswith("  (missing)"):
                name = name[:-11]
            if not hk or not name or name.startswith("—"):
                continue
            if hk in reserved:
                problems.append(f"{_fmt_hotkey(hk)} is already {reserved[hk]}.")
                continue
            if hk in out:
                problems.append(f"{_fmt_hotkey(hk)} is bound twice.")
                continue
            if len(hk) == 1:
                problems.append(
                    f"{_fmt_hotkey(hk)} is a single character — it would fire every "
                    f"time you type it. Use a function key or add a modifier.")
                continue
            out[hk] = name
        return out, problems

    def _apply_script_hotkeys(self):
        mapping, problems = self._collect_script_hotkeys()
        if problems:
            QMessageBox.warning(self, "Launcher keys",
                                "Not applied:\n\n• " + "\n• ".join(problems))
            return
        self._settings.set("script_hotkeys", mapping)
        self.script_hotkeys_changed.emit()
        if mapping:
            lines = "\n".join(f"{_fmt_hotkey(k)}  →  {v}" for k, v in mapping.items())
        else:
            lines = "No launcher keys bound."
        QMessageBox.information(self, "Launcher keys applied", lines)

    def _failsafe_group(self) -> QGroupBox:
        gb, v = _card("Failsafe  ·  safety",
                      "A panic key that always aborts automation, plus an optional guard "
                      "that stops a script if an unexpected window comes to the front.")

        self._panic_enabled = QCheckBox("Enable panic hotkey (always aborts)")
        v.addWidget(self._panic_enabled)
        self._hk_panic = QLineEdit(); self._hk_panic.setFixedWidth(150)
        capp = _btn("Capture…", tip="Press a key to use as the panic/abort key")
        capp.clicked.connect(lambda: self._capture_hotkey(self._hk_panic))
        v.addWidget(_field("Panic key", self._hk_panic, capp))

        self._guard_enabled = QCheckBox("Abort if an unexpected window appears")
        v.addWidget(self._guard_enabled)
        self._guard_mode = QComboBox()
        self._guard_mode.addItems(["Abort if title matches (deny-list)",
                                   "Abort if title does NOT match (allow-list)"])
        v.addWidget(_field("Guard mode", self._guard_mode))
        self._guard_titles = QLineEdit()
        self._guard_titles.setPlaceholderText("comma-separated window titles, e.g. Notepad, Chrome")
        v.addWidget(_field("Window titles", self._guard_titles))

        apply_row = QHBoxLayout(); apply_row.setContentsMargins(0, 0, 0, 0)
        btn_apply = _btn("Apply failsafe", "primary", tip="Activate the panic key & guard")
        btn_apply.clicked.connect(self._apply_failsafe)
        apply_row.addWidget(btn_apply); apply_row.addStretch(1)
        aw = QWidget(); aw.setObjectName("fieldRow"); aw.setLayout(apply_row)
        v.addWidget(aw)
        return gb

    def _apply_failsafe(self):
        self._save_failsafe()
        self.failsafe_changed.emit()
        QMessageBox.information(self, "Failsafe applied",
                               "Panic key: " + (_fmt_hotkey(self._hk_panic.text().strip())
                                                if self._panic_enabled.isChecked() else "off"))

    def _save_failsafe(self):
        s = self._settings.s
        s.panic_enabled = self._panic_enabled.isChecked()
        s.panic_hotkey  = self._hk_panic.text().strip() or "esc"
        s.guard_enabled = self._guard_enabled.isChecked()
        s.guard_mode    = "deny" if self._guard_mode.currentIndex() == 0 else "allow"
        s.guard_titles  = [t.strip() for t in self._guard_titles.text().split(",")
                           if t.strip()]
        self._settings.save()

    def _region_group(self) -> QGroupBox:
        gb, v = _card("Click region  ·  advanced",
                      "Keep every click inside a rectangle on screen, even in cursor-follow mode. "
                      "Useful to avoid clicking outside a game or app window.")

        self._use_region = QCheckBox("Keep clicks inside a region")
        v.addWidget(self._use_region)

        self._reg_x = _spin(-32000, 32000, prefix="X: ", w=110)
        self._reg_y = _spin(-32000, 32000, prefix="Y: ", w=110)
        self._reg_w = _spin(1, 32000, 800, prefix="W: ", w=110)
        self._reg_h = _spin(1, 32000, 600, prefix="H: ", w=110)
        v.addWidget(_field("Top-left", self._reg_x, self._reg_y))
        v.addWidget(_field("Size", self._reg_w, self._reg_h))

        btn = _btn("Select region on screen…", tip="Drag a rectangle to set the region")
        btn.clicked.connect(self._select_region)
        v.addWidget(btn)
        return gb

    def _focus_group(self) -> QGroupBox:
        gb, v = _card("Auto-pause on focus loss",
                      "Automatically pause clicking whenever your chosen window isn’t the active "
                      "one — so it won’t click on your desktop by mistake. (Needs pywin32.)")

        self._use_focus = QCheckBox("Pause when the target window isn’t focused")
        v.addWidget(self._use_focus)

        self._focus_title = QLineEdit()
        self._focus_title.setPlaceholderText("e.g. Notepad")
        v.addWidget(_field("Window title contains", self._focus_title))
        return gb

    def _blacklist_group(self) -> QGroupBox:
        gb, v = _card("Key blacklist  ·  advanced",
                      "A safety net: any key listed here will never be sent by scripts or "
                      "keystroke automation (e.g. block Win or Alt+F4).")

        self._blacklist = QListWidget()
        self._blacklist.setMaximumHeight(110)
        v.addWidget(self._blacklist)

        self._bl_input = QLineEdit()
        self._bl_input.setPlaceholderText("key name, e.g. f4 or win")
        btn_add = _btn("Add", tip="Block this key")
        btn_add.clicked.connect(self._bl_add)
        btn_rm = _btn("Remove", tip="Unblock the selected key")
        btn_rm.clicked.connect(self._bl_remove)
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(8)
        row.addWidget(self._bl_input, 1); row.addWidget(btn_add); row.addWidget(btn_rm)
        rw = QWidget(); rw.setObjectName("fieldRow"); rw.setLayout(row)
        v.addWidget(rw)
        return gb

    def _pointer_group(self) -> QGroupBox:
        gb, v = _card("Pointer movement",
                      "How the cursor gets to the place a step clicks — a glide across the "
                      "screen, or straight there in one jump.")

        self._smooth_mouse = QCheckBox("Move the cursor to the target instead of jumping")
        self._smooth_mouse.setToolTip(
            "Clicks, moves, drags and “click what you found” steps travel to "
            "their target the way a hand would, accelerating away and slowing "
            "onto it.\n\n"
            "Some targets need this: a program reads the cursor once per frame "
            "and works out what happened from where it has been, so a pointer "
            "that appears on a button having never approached it can leave "
            "hover effects unfired and menus unopened — and the click then "
            "does nothing.\n\n"
            "Turn it off for the fastest possible clicking on targets that "
            "don't care.")
        v.addWidget(self._smooth_mouse)

        self._travel_speed = _spin(int(flow.MIN_TRAVEL_PPS), int(flow.MAX_TRAVEL_PPS),
                                   int(flow.DEFAULT_TRAVEL_PPS), " px/s", w=150)
        self._travel_speed.setSingleStep(250)
        self._travel_speed.setToolTip(
            "How fast the cursor travels. Every glide still takes at least "
            f"{flow.MIN_TRAVEL_MS:.0f} ms and never more than "
            f"{flow.MAX_TRAVEL_MS / 1000:.1f} s, so a nudge is still a "
            "movement and crossing a large desktop is not a pause.")
        v.addWidget(_field("Travel speed", self._travel_speed))
        v.addWidget(_hint("Human mode (on the Basic face) also bows the path slightly and "
                          "adds a pixel of tremor."))
        return gb

    def _keystroke_group(self) -> QGroupBox:
        gb, v = _card("Typing defaults",
                      "Defaults used by “Type Text” steps when they don’t set their own values.")

        # Same ceiling as the Type step's own box — 500 here meant the default
        # a new step started from could be a rate nothing could ever deliver.
        self._type_speed = _dspin(0.5, _max_type_cps(), 10.0, 0.5, " chars/s", w=150)
        self._type_speed.setToolTip(
            f"Macronaut types up to about {_max_type_cps():.0f} characters a "
            f"second.\nEach key is held long enough for a game to see it, which "
            f"is what sets that ceiling.")
        v.addWidget(_field("Typing speed", self._type_speed))
        self._ks_interval = _spin(0, 10000, 50, " ms", w=150)
        v.addWidget(_field("Gap between keys", self._ks_interval))

        self._input_backend = QComboBox()
        for bid, label in BACKEND_LABELS.items():
            if bid == "interception" and not interception_available():
                label += "  (driver not installed)"
            self._input_backend.addItem(label, bid)
        self._input_backend.setToolTip(
            "How clicks and keys are sent. Games that ignore automation usually "
            "need SendInput scancodes or the Interception driver.\n\n"
            "Interception addresses the mouse and the keyboard as separate "
            "devices — run 'python interception_backend.py --identify' and "
            "'--identify-mouse' once each, or input can silently go nowhere.")
        v.addWidget(_field("Input backend", self._input_backend))
        self._key_hold = _spin(0, 2000, 60, " ms", w=150)
        self._key_hold.setToolTip(
            "How long a tapped key stays pressed. Games sample input once per "
            "frame and miss instant taps — 40–100 ms is reliable.")
        v.addWidget(_field("Key hold time", self._key_hold))

        # Named as the two layouts it actually chooses between. An earlier
        # version described the symptom instead ("Games that type the wrong
        # letters"), which reads as talking down to someone who already knows
        # what AZERTY is — and hid what the switch does.
        mine = layout_family() or ""
        self._type_positions = QComboBox()
        self._type_positions.addItem(
            f"{mine} — this keyboard" if mine else "This keyboard's layout", "layout")
        self._type_positions.addItem("QWERTY (US)", "us")
        if mine == "QWERTY":
            # Both options resolve to the same key positions, so there is
            # nothing here to get wrong — say so rather than leaving a live
            # control that does nothing.
            self._type_positions.setEnabled(False)
            self._type_positions.setToolTip(
                "This keyboard is already QWERTY, so both options send the "
                "same keys. Nothing to choose.")
        else:
            self._type_positions.setToolTip(
                "Which keyboard the target believes it is reading.\n\n"
                "A key press carries a position, not a letter — which letter "
                "it becomes is decided by whoever receives it. Windows apps "
                "ask your layout and get it right. Some games ignore layouts "
                "and read every key through a US table, so the key you know as "
                "“a” arrives as “q”. Switch to QWERTY (US) for those.\n\n"
                "Affects Type Text on the SendInput and Interception backends.")
        v.addWidget(_field("Typing layout", self._type_positions))

        return gb

    def _on_theme_picked(self, *_):
        name = THEME_ORDER[self._theme_grp.checkedId()]
        self._settings.s.theme_chosen = True
        self.appearance_changed.emit(name)

    def _appearance_group(self) -> QGroupBox:
        gb, v = _card("Appearance", "Pick a theme — the whole app restyles instantly.")
        self._theme_grp = QButtonGroup(self)
        seg, _ = _segmented(
            [THEME_LABELS[t] for t in THEME_ORDER], self._theme_grp, 0)
        v.addWidget(_field("Theme", seg))
        # ⚠ Clicking here is the ONLY thing that marks the theme as chosen.
        # `save_to_settings` writes `s.theme` on every save whether or not
        # anyone touched it, so it cannot tell a preference from a default --
        # see `settings.theme_chosen`.
        self._theme_grp.buttonClicked.connect(self._on_theme_picked)
        v.addWidget(_hint("Cosmic is the default · Mission Control is a navy console · "
                          "Graphite is a tight pro dark look · Daylight is a clean "
                          "light theme."))

        # Set-once setup, so it belongs on this screen rather than in the title
        # bar beside minimize and close, which are things you press all the time.
        self._on_top = QCheckBox("Keep Macronaut above other windows")
        self._on_top.toggled.connect(self.always_on_top_changed.emit)
        v.addWidget(self._on_top)
        v.addWidget(_hint("Useful while automating something fullscreen — you can "
                          "still see whether a script is running."))
        return gb

    def _updates_group(self) -> QGroupBox:
        gb, v = _card("Updates",
                      "Macronaut can check for new versions and install them for you. "
                      "Downloads are verified before anything is replaced.")

        self._version_lbl = QLabel(f"Macronaut {version.__version__}")
        self._upd_check_btn = _btn("Check now", tip="Look for a new version right now")
        v.addWidget(_field("This version", self._version_lbl, self._upd_check_btn))

        self._upd_auto = QCheckBox("Check for updates automatically")
        self._upd_auto_dl = QCheckBox("Download updates in the background")
        v.addWidget(self._upd_auto)
        v.addWidget(self._upd_auto_dl)

        self._upd_status = _hint("")
        v.addWidget(self._upd_status)
        self._upd_install_btn = _btn("Install and restart",
                                     tip="Apply the downloaded update now")
        self._upd_install_btn.setVisible(False)
        v.addWidget(self._upd_install_btn)

        if not updater.is_frozen():
            v.addWidget(_hint("You're running from source, so updates only report "
                              "what's available — use git pull to update."))

        self._upd_ctl = updater_ui.UpdateController(self)
        self._upd_ctl.result.connect(self._on_update_result)
        self._upd_ctl.error.connect(self._on_update_error)
        self._upd_ctl.staged.connect(self._on_update_staged)
        self._upd_ctl.progress.connect(self._on_update_progress)
        self._upd_check_btn.clicked.connect(self._check_updates_now)
        self._upd_install_btn.clicked.connect(self._install_update)
        self._upd_auto.toggled.connect(
            lambda on: self._settings.set("auto_check_updates", bool(on)))
        self._upd_auto_dl.toggled.connect(
            lambda on: self._settings.set("auto_download_updates", bool(on)))
        return gb

    def _crash_group(self) -> QGroupBox:
        gb, v = _card("Crash reports",
                      "If Macronaut closes unexpectedly it can tell me what "
                      "went wrong, so it gets fixed instead of staying broken.")

        self._crash_on = QCheckBox("Send a report when Macronaut crashes")
        v.addWidget(self._crash_on)
        v.addWidget(_hint("Sent: the error, this version, your Windows version, "
                          "and which step was running. Never sent: your "
                          "scripts, your keystrokes, anything on screen, or "
                          "your name — Windows account names are stripped from "
                          "file paths before anything is written to disk."))

        self._crash_view_btn = _btn("View pending reports",
                                    tip="Read the exact data that would be sent")
        self._crash_status = _hint("")
        v.addWidget(_field("Waiting to send", self._crash_status,
                           self._crash_view_btn))
        if not crashsend.enabled():
            v.addWidget(_hint("This build has no reporting endpoint configured, "
                              "so reports are kept on this machine only."))

        self._crash_on.toggled.connect(
            lambda on: self._settings.set("crash_reports", "on" if on else "off"))
        self._crash_view_btn.clicked.connect(lambda: crash_ui.show_reports(self))
        return gb

    def _refresh_crash_status(self):
        try:
            n = len(crashreport.pending())
        except Exception:
            n = 0
        self._crash_status.setText(
            "none" if not n else
            "%d report%s" % (n, "" if n == 1 else "s"))
        self._crash_view_btn.setEnabled(bool(n))

    def _licence_group(self) -> QGroupBox:
        """Licence status, and the two buttons that change it.

        Above Updates and About rather than buried under them, because on a
        free copy this card is the only place in Settings that says what Pro
        is — and someone who came here looking for it should not have to scroll
        past the crash-reporting checkbox to find out.
        """
        gb, v = _card("Your licence")
        # Kept so `_refresh_licence_status` can hide the whole card — see there.
        self._lic_group = gb

        self._lic_status = QLabel("")
        self._lic_status.setObjectName("label_status")
        v.addWidget(self._lic_status)

        self._lic_note = _hint("")
        v.addWidget(self._lic_note)

        self._lic_activate = _btn("Enter a licence key",
                                  tip="Paste the key from your purchase e-mail")
        self._lic_buy = _btn("Get Macronaut Pro", kind="primary",
                             tip="Opens the Macronaut website")
        self._lic_remove = _btn("Remove from this computer",
                                tip="The key keeps working — use it elsewhere")
        row = QHBoxLayout()
        row.addWidget(self._lic_activate)
        row.addWidget(self._lic_buy)
        row.addWidget(self._lic_remove)
        row.addStretch(1)
        v.addLayout(row)

        self._lic_activate.clicked.connect(self._on_activate_licence)
        self._lic_buy.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(entitlements.BUY_URL)))
        self._lic_remove.clicked.connect(self._on_remove_licence)
        self._refresh_licence_status()
        return gb

    def _refresh_licence_status(self):
        pro = licensing.is_pro()
        lic = licensing.current()
        self._lic_status.setText(lic.describe())
        # ⚠ Three states, not two. A card that describes a step limit and a
        # paid half while `entitlements.ENFORCED` is False is describing an app
        # the user is not running — and this is the one screen someone opens
        # specifically to find out what they are and are not allowed to do.
        if pro:
            note = ("Thank you. Every feature is unlocked, on every computer "
                    "you own.")
        elif not entitlements.ENFORCED:
            # Never shown — the card is hidden in this state, below — but kept
            # free of any paid wording in case something ever un-hides it.
            note = ("Every feature is unlocked, for everyone. There is no step "
                    "limit and nothing is held back.")
        else:
            note = ("Clicking, typing, dragging, scrolling and waiting are "
                    "free and always will be, in scripts of up to "
                    f"{entitlements.FREE_MAX_STEPS} steps.\n\nPro adds the "
                    "steps that watch the screen and decide what to do — "
                    "Wait for image, Wait for text, Wait for pixel, If / Else, "
                    "Loop, variables and Go to — and removes the step limit. "
                    f"{licensing_ui.PRICE}, once. {licensing_ui.TERMS}")
        self._lic_note.setText(note)
        # Buying and entering a key are noise once there is a licence; removing
        # one is noise until there is.
        self._lic_activate.setVisible(not pro)
        # ⚠ The button goes while nothing is enforced: it would lead nowhere.
        self._lic_buy.setVisible(not pro and entitlements.ENFORCED)
        self._lic_remove.setVisible(pro)
        # ⚠⚠ The whole card goes while nothing is enforced and no key is
        # entered: every step runs, so a licence card would describe a
        # distinction that does not exist. A copy that already holds a key
        # still sees its card, because removing it must stay possible.
        group = getattr(self, "_lic_group", None)
        if group is not None:
            group.setVisible(pro or entitlements.ENFORCED)

    def _on_activate_licence(self):
        if licensing_ui.prompt_for_key(self):
            self._refresh_licence_status()
            self.licence_changed.emit()

    def _on_remove_licence(self):
        answer = QMessageBox.question(
            self, "Remove licence",
            "Remove the licence key from this computer?\n\n"
            "The key itself keeps working — nothing about it is tied to this "
            "machine — so you can enter it again here or on another computer.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            licensing.deactivate()
            self._refresh_licence_status()
            self.licence_changed.emit()

    def _about_group(self) -> QGroupBox:
        gb, v = _card("About & legal",
                      "Macronaut is free software. You can read it, change it "
                      "and pass it on — the licence is the GNU GPL.")

        v.addWidget(_hint(f"Macronaut {version.__version__} · "
                          "© 2026 Gerben van Poucke · GPL-3.0-or-later"))

        lic = _btn("Licence", tip="Read the GNU General Public License")
        tpn = _btn("Third-party notices",
                   tip="The open-source components Macronaut is built on")
        lic.clicked.connect(
            lambda: self._show_legal("Macronaut — Licence", "LICENSE"))
        tpn.clicked.connect(
            lambda: self._show_legal("Third-party notices",
                                     "THIRD-PARTY-NOTICES.md"))
        # ⚠ In the app, not only on the site. Someone deciding whether to
        # trust this is looking at the app they have already been warned
        # about; a source link they have to go and find on a web page
        # is not offered to the person who most needs it.
        src = _btn("Source code", tip="Read the source on GitHub")
        src.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(entitlements.SOURCE_URL)))
        row = QHBoxLayout()
        row.addWidget(lic)
        row.addWidget(tpn)
        row.addWidget(src)
        row.addStretch(1)
        v.addLayout(row)

        # Always here, for whoever goes looking — the one-time question in
        # `star_ui` reaches people once, and somebody who dismissed it and
        # changed their mind a month later should not have to find the repo.
        star = _btn(star_ui.STAR_LABEL,
                    tip="Free to give, and it is how other people find Macronaut")
        bug = _btn(star_ui.FEEDBACK_LABEL,
                   tip="Tell us what broke or what is missing")
        star.clicked.connect(star_ui.open_star_page)
        bug.clicked.connect(star_ui.open_feedback_page)
        self._about_star, self._about_feedback = star, bug
        row2 = QHBoxLayout()
        row2.addWidget(star)
        row2.addWidget(bug)
        row2.addStretch(1)
        v.addLayout(row2)

        v.addWidget(_hint(
            "Reminder: many online games and services forbid automated input. "
            "Automating against them can cost you your account — check their "
            "rules first."))
        return gb

    def _show_legal(self, title: str, filename: str):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(720, 560)
        lay = QVBoxLayout(dlg)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QTextEdit.WidgetWidth)
        view.setFont(QFont("Consolas", 9))
        view.setPlainText(_legal_text(filename))
        lay.addWidget(view)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept)
        lay.addWidget(bb)
        dlg.exec()

    # ── Updates ───────────────────────────────────────────────────
    def _check_updates_now(self):
        # A manual check ignores the "skip this version" choice — the user is
        # explicitly asking, so hiding a known update would just be confusing.
        self._settings.set("skip_version", "")
        if self._upd_ctl.start(download=updater.is_frozen()
                               and bool(self._settings.s.auto_download_updates)):
            self._upd_status.setText("Checking…")
            self._upd_check_btn.setEnabled(False)

    def _on_update_result(self, info):
        self._upd_check_btn.setEnabled(True)
        if info is None:
            self._upd_status.setText(
                f"You're up to date ({version.__version__}).")
            return
        self._pending_info = info
        self._upd_status.setText(f"Macronaut {info.version} is available.")

    def _on_update_error(self, msg: str):
        self._upd_check_btn.setEnabled(True)
        self._upd_status.setText(msg)

    def _on_update_progress(self, done: int, total: int):
        if total:
            self._upd_status.setText(
                f"Downloading… {done * 100 // total}%")

    def _on_update_staged(self, path: str):
        self._staged_update = path
        self._settings.set("pending_update",
                           getattr(self, "_pending_info", None).version
                           if getattr(self, "_pending_info", None) else "")
        self._upd_status.setText("Downloaded and verified — ready to install.")
        self._upd_install_btn.setVisible(True)

    def _install_update(self):
        path = getattr(self, "_staged_update", "")
        if not path:
            return
        if updater_ui.apply_and_quit(path, self):
            QApplication.quit()

    # ── Helpers ───────────────────────────────────────────────────
    def _capture_hotkey(self, target: QLineEdit):
        dlg = KeyCaptureDialog(self)
        if dlg.exec() == QDialog.Accepted and dlg.captured:
            target.setText(dlg.captured)

    def _apply_hotkeys(self):
        hk = self._hk_startstop.text().strip() or "f8"
        trig = self._hk_trigger.text().strip()
        self._settings.set("start_stop_hotkey", hk)
        self._settings.set("trigger_key", trig)
        self.hotkey_changed.emit(hk)
        self.trigger_changed.emit(trig)
        QMessageBox.information(self, "Hotkeys applied",
                                "Start/Stop: " + _fmt_hotkey(hk) +
                                (("\nTrigger: " + _fmt_hotkey(trig)) if trig else ""))

    def _select_region(self):
        # ⚠ The overlay MUST go in the module-level keep-alive list. A bare
        # local is garbage-collected the instant this method returns, so the
        # window died before anyone could drag in it — reported as a flat
        # "select region doesn't work", and true since the first commit.
        # `_launch_region_picker`'s docstring has spelled this out all along;
        # this was the one call site of two that never got the memo.
        # RegionSelector shows itself in __init__ and carries WA_DeleteOnClose,
        # so `destroyed` is what releases it — that covers a picked region,
        # Escape, and a drag too small to count, all three of which close().
        sel = RegionSelector()
        sel.region_selected.connect(self._on_region_selected)
        sel.destroyed.connect(lambda *_: _active_selector.clear())
        _active_selector.clear()
        _active_selector.append(sel)   # prevent GC
        sel.raise_()
        sel.activateWindow()

    def _on_region_selected(self, x, y, w, h):
        self._reg_x.setValue(x); self._reg_y.setValue(y)
        self._reg_w.setValue(w); self._reg_h.setValue(h)
        self._use_region.setChecked(True)

    def _bl_add(self):
        key = self._bl_input.text().strip().lower()
        if key and not self._blacklist.findItems(key, Qt.MatchExactly):
            self._blacklist.addItem(key)
            self._bl_input.clear()

    def _bl_remove(self):
        for item in self._blacklist.selectedItems():
            self._blacklist.takeItem(self._blacklist.row(item))

    def _load(self):
        s = self._settings.s
        self._hk_startstop.setText(s.start_stop_hotkey)
        self._hk_trigger.setText(s.trigger_key)
        for holder, _key, _combo in list(self._sh_rows):
            self._remove_script_hotkey_row(holder)
        for hk, name in (getattr(s, "script_hotkeys", None) or {}).items():
            self._add_script_hotkey_row(hk, name)
        self._panic_enabled.setChecked(getattr(s, "panic_enabled", True))
        self._hk_panic.setText(getattr(s, "panic_hotkey", "esc"))
        self._guard_enabled.setChecked(getattr(s, "guard_enabled", False))
        self._guard_mode.setCurrentIndex(0 if getattr(s, "guard_mode", "deny") == "deny" else 1)
        self._guard_titles.setText(", ".join(getattr(s, "guard_titles", []) or []))
        self._use_region.setChecked(s.use_region)
        self._reg_x.setValue(s.region_x); self._reg_y.setValue(s.region_y)
        self._reg_w.setValue(s.region_w); self._reg_h.setValue(s.region_h)
        self._use_focus.setChecked(s.pause_on_focus_loss)
        self._focus_title.setText(s.focus_window_title)
        for k in s.keystroke_blacklist:
            self._blacklist.addItem(k)
        self._on_top.setChecked(bool(getattr(s, "always_on_top", False)))
        self._type_speed.setValue(s.typing_speed_cps)
        self._ks_interval.setValue(s.keystroke_interval_ms)
        idx = self._input_backend.findData(getattr(s, "input_backend", "pynput"))
        self._input_backend.setCurrentIndex(idx if idx >= 0 else 0)
        self._key_hold.setValue(getattr(s, "key_hold_ms", 60))
        self._smooth_mouse.setChecked(bool(getattr(s, "smooth_mouse", True)))
        self._travel_speed.setValue(int(flow.travel_pps(
            getattr(s, "mouse_travel_pps", None))))
        idx = self._type_positions.findData(getattr(s, "type_key_positions", "layout"))
        self._type_positions.setCurrentIndex(idx if idx >= 0 else 0)
        try:
            self._theme_grp.button(THEME_ORDER.index(getattr(s, "theme", DEFAULT_THEME))).setChecked(True)
        except Exception:
            self._theme_grp.button(0).setChecked(True)
        self._upd_auto.setChecked(bool(getattr(s, "auto_check_updates", True)))
        self._upd_auto_dl.setChecked(bool(getattr(s, "auto_download_updates", True)))
        # "ask" shows as off, because off is what it currently behaves as —
        # ticking it here is a valid way to answer the question early.
        self._crash_on.setChecked(
            str(getattr(s, "crash_reports", "ask") or "ask") == "on")
        self._refresh_crash_status()
        self._update_enabled_states()

    def save_to_settings(self):
        s = self._settings.s
        s.start_stop_hotkey   = self._hk_startstop.text().strip() or "f8"
        s.trigger_key         = self._hk_trigger.text().strip()
        # Only the valid rows; a row that collides is dropped rather than saved,
        # matching what Apply would have refused to write.
        s.script_hotkeys      = self._collect_script_hotkeys()[0]
        s.panic_enabled       = self._panic_enabled.isChecked()
        s.panic_hotkey        = self._hk_panic.text().strip() or "esc"
        s.guard_enabled       = self._guard_enabled.isChecked()
        s.guard_mode          = "deny" if self._guard_mode.currentIndex() == 0 else "allow"
        s.guard_titles        = [t.strip() for t in self._guard_titles.text().split(",")
                                 if t.strip()]
        s.use_region          = self._use_region.isChecked()
        s.region_x            = self._reg_x.value()
        s.region_y            = self._reg_y.value()
        s.region_w            = self._reg_w.value()
        s.region_h            = self._reg_h.value()
        s.pause_on_focus_loss = self._use_focus.isChecked()
        s.focus_window_title  = self._focus_title.text()
        s.keystroke_blacklist = [self._blacklist.item(i).text()
                                  for i in range(self._blacklist.count())]
        s.typing_speed_cps    = self._type_speed.value()
        s.keystroke_interval_ms = self._ks_interval.value()
        s.input_backend       = self._input_backend.currentData() or "pynput"
        s.key_hold_ms         = self._key_hold.value()
        s.smooth_mouse        = self._smooth_mouse.isChecked()
        s.mouse_travel_pps    = self._travel_speed.value()
        s.type_key_positions  = self._type_positions.currentData() or "layout"
        s.theme               = THEME_ORDER[self._theme_grp.checkedId()] if self._theme_grp.checkedId() >= 0 else DEFAULT_THEME


# ═══════════════════════════════════════════════════════════════════════════════
#  Tab: Stats
# ═══════════════════════════════════════════════════════════════════════════════

class StatsTab(QWidget):
    def __init__(self, stats: StatsManager):
        super().__init__()
        self._stats = stats
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll, col = _scroll_page(820)
        root.addWidget(scroll)

        head = QLabel("Statistics")
        head.setObjectName("h1")
        col.addWidget(head)
        col.addWidget(_hint("Live activity while running, plus a history of past sessions."))

        # Live counters
        live_gb, live_v = _card("This session")
        live_grid = QGridLayout()
        live_grid.setHorizontalSpacing(8)
        self._lbl_cps     = self._counter("0.00", "Clicks / sec", live_grid, 0)
        self._lbl_kps     = self._counter("0.00", "Keys / sec",   live_grid, 1)
        self._lbl_elapsed = self._counter("00:00", "Elapsed",     live_grid, 2)
        self._lbl_clicks  = self._counter("0", "Total clicks",    live_grid, 3)
        self._lbl_keys    = self._counter("0", "Total keys",      live_grid, 4)
        gw = QWidget(); gw.setLayout(live_grid)
        live_v.addWidget(gw)
        col.addWidget(live_gb)

        # Session history
        hist_gb, hist_v = _card("History")
        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(
            ["Start", "End", "Duration", "Clicks", "Keys", "CPS", "KPS"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setMinimumHeight(220)
        hist_v.addWidget(self._table)

        btn_row = QHBoxLayout(); btn_row.setContentsMargins(0, 0, 0, 0)
        btn_export = _btn("Export CSV…", tip="Save the history table to a .csv file")
        btn_export.clicked.connect(self._export_csv)
        btn_clear = _btn("Clear history", "danger", tip="Delete all saved sessions")
        btn_clear.clicked.connect(self._clear_history)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_export); btn_row.addWidget(btn_clear)
        bw = QWidget(); bw.setLayout(btn_row)
        hist_v.addWidget(bw)
        col.addWidget(hist_gb)
        col.addStretch(1)

    def _counter(self, val: str, lbl: str, grid: QGridLayout, col: int) -> QLabel:
        val_lbl = QLabel(val)
        val_lbl.setObjectName("label_counter")
        val_lbl.setAlignment(Qt.AlignCenter)
        name_lbl = QLabel(lbl)
        name_lbl.setObjectName("chip")
        name_lbl.setAlignment(Qt.AlignCenter)
        grid.addWidget(val_lbl, 0, col)
        grid.addWidget(name_lbl, 1, col)
        return val_lbl

    def refresh_live(self, running: bool):
        if running:
            self._lbl_cps.setText(f"{self._stats.current_cps():.2f}")
            self._lbl_kps.setText(f"{self._stats.current_kps():.2f}")
            elapsed = self._stats.elapsed()
            m, s = divmod(int(elapsed), 60)
            h, m = divmod(m, 60)
            self._lbl_elapsed.setText(f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}")
            self._lbl_clicks.setText(str(self._stats.total_clicks))
            self._lbl_keys.setText(str(self._stats.total_keystrokes))

    def refresh_history(self):
        self._table.setRowCount(0)
        for sess in self._stats.sessions:
            r = self._table.rowCount()
            self._table.insertRow(r)
            for c, val in enumerate(sess.to_csv_row()):
                self._table.setItem(r, c, QTableWidgetItem(val))

    def _export_csv(self):
        if not self._stats.sessions:
            QMessageBox.information(self, "No Data", "No session history to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", "", "CSV Files (*.csv)")
        if path:
            try:
                self._stats.export_csv(path)
                QMessageBox.information(self, "Exported", f"Saved to:\n{path}")
            except Exception as e:
                QMessageBox.critical(self, "Export Error", str(e))

    def _clear_history(self):
        r = QMessageBox.question(self, "Clear History", "Delete all session history?",
                                 QMessageBox.Yes | QMessageBox.No)
        if r == QMessageBox.Yes:
            self._stats.clear_history()
            self._table.setRowCount(0)


# ═══════════════════════════════════════════════════════════════════════════════
#  Main window
# ═══════════════════════════════════════════════════════════════════════════════

class StarfieldBar(QFrame):
    """Top bar that paints a faint starfield when the Mission Control theme is
    active — the app's signature space touch. Plain in other themes."""

    def __init__(self):
        super().__init__()
        import random as _random
        rng = _random.Random(42)
        self._stars = [(rng.random(), rng.random(),
                        rng.choice([1.0, 1.0, 1.0, 1.5, 2.0]), rng.randint(35, 150))
                       for _ in range(48)]

    def paintEvent(self, e):
        super().paintEvent(e)               # QSS background + bottom border
        if CURRENT_THEME != "mission":
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        w, h = self.width(), self.height()
        for fx, fy, r, a in self._stars:
            p.setBrush(QColor(201, 214, 255, a))
            p.drawEllipse(int(fx * w - r), int(fy * h - r), int(2 * r), int(2 * r))
        p.end()


class _DragBar(QFrame):
    """A custom title bar: dragging it moves the frameless window."""
    def __init__(self, win):
        super().__init__()
        self._win = win
        self._drag = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPos() - self._win.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self._win.move(e.globalPos() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag = None


class _FaceStack(QStackedWidget):
    """The two faces, in a fixed order. Named so call sites read as English
    rather than as `setCurrentIndex(1)`.

    ⚠ Deliberately nothing else. The 2.0 version of this class overrode
    `sizeHint`/`minimumSizeHint` to answer for the current page only, on the
    reasoning that a QStackedWidget otherwise reports the largest of all its
    pages and the window could never shrink to Basic. That reasoning is sound
    and the code was still useless: `_show_basic` sets the window's minimum and
    geometry explicitly, which is what actually decides the size, and measuring
    it three ways showed Basic landing at exactly 500x542 with the overrides,
    without them, and without the `QSizePolicy.Ignored` trick that was added to
    prop them up.

    What actually caused the window to open 140 px too wide was a saved
    geometry that was never Basic's — see `_live_face`. Two plausible fixes
    were written and measured before the real cause was found; neither is here,
    because code kept "just in case" with an explanation that does not hold is
    worse than no code at all.
    """
    BASIC, ADVANCED = 0, 1


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # ⚠ Read, never typed. This said "2.0" through every release up to
        # 2.2.0 — in the taskbar, in alt-tab, and in the title bar of every
        # screenshot attached to a bug report, where a stale version is worse
        # than none because it will be believed.
        self.setWindowTitle(f"Macronaut · {version.__version__}")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(0, 0)

        # Core managers
        self._settings = SettingsManager()
        # ⚠ Every component must read *this* instance. SettingsManager re-reads
        # the JSON on construction, so anything building its own saw the file on
        # disk rather than what the Settings window is showing — a backend
        # chosen but not yet saved was ignored by every run.
        settings_mod.set_active(self._settings)
        # A first-time user opens the Library and finds nothing in it, on the
        # one screen where having something to run is the whole difference
        # between a download and a user. Seeded once, never into a library
        # that already has something in it.
        starters.seed_once(self._settings, scripts_dir())
        self._stats    = StatsManager()
        self._running  = False
        self._force_quit = False
        self._tray_hint_shown = False
        # Which face is actually on screen, or None before the first one is
        # shown.
        #
        # ⚠ Deliberately NOT read off `_stack.currentIndex()`, which is the
        # obvious source and is wrong: a fresh QStackedWidget reports index 0,
        # so the very first `_show_advanced()` believed it was leaving Basic and
        # saved the untouched 640x480 default into `basic_*`. Basic then opened
        # at that size forever after, because a saved size beats the content
        # fit — a first impression permanently broken by a geometry the user
        # never chose. "No face yet" has to be representable.
        self._live_face: Optional[str] = None
        self._countdown_remaining = 0
        self._countdown_timer: Optional[QTimer] = None
        # Launcher-key state: which bound hotkey started the current run (so the
        # same key stops it), the script it named, and the graph waiting out a
        # start delay.
        self._active_hotkey = ""
        self._active_script = ""
        self._pending_graph = None

        # One global keyboard listener watches both the start/stop hotkey and the
        # optional alternate trigger key. Using a single low-level hook (instead of
        # two) is lighter and less likely to trip antivirus heuristics.
        self._hk_bridge  = HotkeyBridge()
        self._hk_bridge.triggered.connect(self._toggle)
        self._hk_listener = HotkeyListener(self._hk_bridge)

        # Global failsafe: a separate always-on panic hotkey that aborts any
        # automation no matter what (Phase 2, item 6). Kept on its own bridge so
        # it can never be confused with the start/stop toggle.
        self._panic_bridge = HotkeyBridge()
        self._panic_bridge.triggered.connect(self._panic)
        self._panic_listener = HotkeyListener(self._panic_bridge)

        self._build_ui()
        self._wire_run_buttons()
        self._init_window()

        # System tray
        self._tray = SystemTray(self)
        self._tray.start_requested.connect(self._start)
        self._tray.stop_requested.connect(self._stop)
        self._tray.show_requested.connect(self._bring_to_front)
        self._tray.quit_requested.connect(self._quit_app)

        # Live-stats refresh timer
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_stats)
        self._refresh_timer.start(250)

        # Show any session history from previous runs
        self._stats_tab.refresh_history()

        # Start hotkey listener (watches main hotkey + optional trigger key)
        self._refresh_hotkeys()
        self._refresh_panic()
        self._update_hotkey_labels(self._settings.s.start_stop_hotkey)

        # Connect settings hotkey changes
        self._settings_tab.hotkey_changed.connect(self._on_hotkey_changed)
        self._settings_tab.trigger_changed.connect(lambda *_: self._refresh_hotkeys())
        self._settings_tab.script_hotkeys_changed.connect(self._refresh_hotkeys)
        self._sequence_tab.script_hotkeys_changed.connect(self._refresh_hotkeys)
        self._settings_tab.appearance_changed.connect(self._apply_theme)
        self._settings_tab.always_on_top_changed.connect(self._apply_always_on_top)
        self._settings_tab.failsafe_changed.connect(lambda *_: self._refresh_panic())
        self._settings_tab.licence_changed.connect(
            self._sequence_tab.refresh_licence_state)

        # Background update check, a few seconds after the window is up so it
        # never competes with startup or delays the first paint.
        self._upd_ctl = updater_ui.UpdateController(self)
        self._upd_info = None
        self._upd_staged = ""
        self._upd_dialog = None
        self._upd_ctl.result.connect(self._on_startup_update_result)
        self._upd_ctl.staged.connect(self._on_startup_update_staged)
        self._upd_ctl.progress.connect(self._on_startup_update_progress)
        self._upd_ctl.error.connect(self._on_startup_update_error)
        QTimer.singleShot(4000, self._maybe_check_updates)

        # Crash reports from a previous session, if there are any and the user
        # has agreed. Deliberately after the update check rather than racing it
        # for the network, and it asks nothing of someone who has never crashed.
        crash_ui.schedule(self, self._settings)

        # Where you left off: the script that was open when the app closed.
        # Synchronous, and before the recovery offer below, so that accepting
        # an unsaved flow still has the last word on what is on the canvas.
        self._restore_last_script()

        # Unsaved work from a session that closed or crashed with a flow on the
        # canvas. First of the three startup questions on purpose: it is about
        # something the user made and might be missing right now, whereas an
        # update and a crash report can both wait. Zero delay so it is answered
        # and gone before the update check four seconds later can stack a second
        # box on top of it.
        QTimer.singleShot(0, self._offer_recovery)

        # Last and least of the startup questions, and the only one about us
        # rather than about the user's work: once, ever, to somebody who has
        # been using the app. It stands down if any of the others is on screen.
        star_ui.schedule(self, self._settings, self._stats)

    def _offer_recovery(self):
        """Offer back a canvas that was never saved. See `recovery.py`.

        ⚠ The file is deleted whichever button is pressed, and also when there
        is nothing worth offering. A recovery copy that outlives its own
        question comes back on the next launch as a flow the user has already
        said no to, and the second time they will stop reading the box.
        """
        try:
            payload = recovery.read()
            graph = recovery.offerable(payload)
            if graph is None:
                if payload is not None:
                    recovery.clear()
                return
            r = QMessageBox.question(
                self, "Unsaved script",
                "Macronaut closed with a script on the canvas that was never "
                f"saved.\n\n{recovery.describe(payload)}\n\nOpen it again?",
                QMessageBox.Yes | QMessageBox.No)
            if r == QMessageBox.Yes:
                self._sequence_tab._graph = graph
                self._sequence_tab._canvas.set_graph(graph)
                self._refresh_basic_face()
                self._sequence_tab._canvas.fit()
        except Exception:
            # A safety net is not allowed to be the thing that breaks startup.
            pass
        finally:
            # ⚠ Inside `finally` so an exception mid-restore still retires the
            # file. The alternative is a payload that failed to load once
            # asking the same question at every launch from now on.
            try:
                recovery.clear()
                self._sequence_tab._recovery_blob = None
            except Exception:
                pass

    def _on_crash_reports_sent(self, sent: int, remaining: int):
        """An upload finished. Queued onto this thread — see _start_upload.

        Without this the Settings panel keeps showing the count it read when
        the tab was built, so a successful upload looks exactly like nothing
        having happened. The breadcrumb matters more: it is the only record
        anywhere that reports do in fact leave this machine, and it rides along
        on the *next* crash — which is where the question actually gets asked.
        """
        try:
            crashreport.breadcrumb("crash_reports_sent",
                                   sent=int(sent), remaining=int(remaining))
        except Exception:
            pass
        try:
            self._settings_tab._refresh_crash_status()
        except Exception:
            pass

    # ── Updates ───────────────────────────────────────────────────────
    UPDATE_CHECK_INTERVAL = 6 * 60 * 60  # seconds between automatic checks

    def _maybe_check_updates(self):
        """Automatic startup check — silent about everything except an actual
        update. Someone offline, behind a proxy, or on a build whose update repo
        isn't published yet must never see an error popup on launch."""
        s = self._settings.s
        if not getattr(s, "auto_check_updates", True):
            return
        last = float(getattr(s, "last_update_check", 0.0) or 0.0)
        if time.time() - last < self.UPDATE_CHECK_INTERVAL:
            return
        self._settings.set("last_update_check", time.time())
        self._upd_ctl.start(download=updater.is_frozen()
                            and bool(getattr(s, "auto_download_updates", True)))

    def _on_startup_update_result(self, info):
        if info is None:
            return
        if info.version == (getattr(self._settings.s, "skip_version", "") or ""):
            return  # user asked not to be told about this one again
        self._upd_info = info
        self._show_update_dialog()

    def _on_startup_update_progress(self, done: int, total: int):
        if self._upd_dialog is not None:
            self._upd_dialog.on_progress(done, total)

    def _on_startup_update_error(self, msg: str):
        # Silent by design (see _maybe_check_updates) — surfaced only if a
        # dialog is already open because the user is watching a download.
        if self._upd_dialog is not None:
            self._upd_dialog.on_error(msg)

    def _on_startup_update_staged(self, path: str):
        self._upd_staged = path
        if self._upd_info is not None:
            self._settings.set("pending_update", self._upd_info.version)
        if self._upd_dialog is not None:
            self._upd_dialog.set_staged(path)

    def _show_update_dialog(self):
        if self._upd_info is None or self._upd_dialog is not None:
            return
        dlg = updater_ui.UpdateDialog(self._upd_info, self._upd_staged or None, self)
        self._upd_dialog = dlg
        try:
            dlg.exec()
            if dlg.choice == dlg.SKIP:
                self._settings.set("skip_version", self._upd_info.version)
            elif dlg.choice == dlg.INSTALL and self._upd_staged:
                if updater_ui.apply_and_quit(self._upd_staged, self):
                    self._quit_app()
        finally:
            self._upd_dialog = None

    def _apply_theme(self, name: str):
        global CURRENT_THEME
        CURRENT_THEME = name if name in THEMES else DEFAULT_THEME
        QApplication.instance().setStyleSheet(THEMES[CURRENT_THEME])
        bar = getattr(self, "_topbar", None)
        if bar is not None:
            bar.update()
        # ⚠ The Basic face sets its OWN stylesheet, and a widget stylesheet
        # beats the application one — so it does not follow the app sheet the
        # way every other widget does and has to be told. Left out, picking a
        # theme restyles the whole app except the one face, which is exactly
        # how it looked when it carried a hardcoded palette.
        face = getattr(self, "_compact", None)
        if face is not None:
            face.apply_theme(PALETTES.get(CURRENT_THEME, _MISSION_PALETTE))
        # ⚠ Same problem, three more widgets. These set their own stylesheet
        # too, so they do not follow the application sheet either -- and
        # they were hardcoded to the 2.0 cosmic palette, which is what left
        # a dark purple title bar sitting on top of the Daylight theme.
        bar = getattr(self, "_adv_bar", None)
        if bar is not None:
            bar.setStyleSheet(self._hdr_qss())
        grip = getattr(self, "_adv_grip", None)
        if grip is not None:
            grip.setStyleSheet(self._grip_qss())
        drawer = getattr(self, "_drawer_panel", None)
        if drawer is not None:
            drawer.setStyleSheet(self._drawer_qss())

    def _on_hotkey_changed(self, hk: str):
        self._refresh_hotkeys()
        self._update_hotkey_labels(hk)

    def _refresh_hotkeys(self):
        """(Re)bind the single listener to the main + trigger + launcher keys.

        Still one low-level hook for all of them — see the note where the
        listener is constructed. `set_hotkeys` drops blanks and duplicates, so a
        launcher key colliding with Start/Stop is watched once; `_toggle` decides
        which meaning wins, and the Settings UI refuses to create the collision
        in the first place.
        """
        self._hk_listener.set_hotkeys([
            self._settings.s.start_stop_hotkey,
            self._settings.s.trigger_key,
            *(self._settings.s.script_hotkeys or {}).keys(),
        ])

    def _refresh_panic(self):
        """(Re)bind the always-on panic hotkey."""
        s = self._settings.s
        if getattr(s, "panic_enabled", True) and getattr(s, "panic_hotkey", ""):
            self._panic_listener.set_hotkeys([s.panic_hotkey])
        else:
            self._panic_listener.set_hotkeys([])

    def _panic(self, hk: str = ""):
        """Abort everything immediately (panic hotkey or window guard).

        `hk` arrives from the bridge signal and is unused — the panic listener
        watches exactly one key, and it aborts whatever fired it."""
        if self._running or (self._countdown_timer and self._countdown_timer.isActive()):
            self._stop()
            try:
                self._tray.notify("Macronaut", "Automation aborted (failsafe).")
            except Exception:
                pass

    def _check_guard(self):
        """Abort if an unexpected foreground window appears during a flow run."""
        s = self._settings.s
        if not (self._running and getattr(s, "guard_enabled", False)):
            return
        titles = [t.lower() for t in getattr(s, "guard_titles", []) if t.strip()]
        if not titles:
            return
        try:
            import win32gui
            fg = win32gui.GetWindowText(win32gui.GetForegroundWindow()).lower()
        except Exception:
            return
        matched = any(t in fg for t in titles)
        mode = getattr(s, "guard_mode", "deny")
        # deny: abort if the foreground matches a forbidden title.
        # allow: abort if the foreground is NOT one of the allowed titles.
        violated = matched if mode == "deny" else (not matched)
        if violated:
            self._panic()

    def _update_hotkey_labels(self, hk: str):
        disp = _fmt_hotkey(hk)
        self._compact.set_hotkey_label(disp)
        self._tray.set_hotkey_label(disp)

    # ── Build UI (two faces, one window) ──────────────────────────
    def _build_ui(self):
        self._sequence_tab = SequenceTab(self._settings)
        self._settings_tab = SettingsTab(self._settings)
        self._stats_tab    = StatsTab(self._stats)

        self._compact = CompactFace(self._settings)
        self._compact.apply_theme(PALETTES.get(CURRENT_THEME, _MISSION_PALETTE))
        c = self._compact
        c.start_stop_requested.connect(self._toggle)
        c.record_requested.connect(self._toggle_record)
        c.play_script_requested.connect(self._toggle)
        c.advanced_requested.connect(self._show_advanced)
        c.settings_requested.connect(self._open_gear)
        c.minimize_requested.connect(self._minimize)
        c.close_requested.connect(self.close)
        c.pin_toggled.connect(self._apply_always_on_top)
        c.script_changed.connect(self._on_script_selected)
        c.config_changed.connect(self._on_compact_config_changed)

        self._stack = _FaceStack()
        self._stack.addWidget(self._compact)          # index 0 — BASIC
        self._stack.addWidget(self._build_advanced()) # index 1 — ADVANCED
        self.setCentralWidget(self._stack)

    # ── window chrome, from the live theme ────────────────────────
    #
    # ⚠⚠ This was a hardcoded 2.0 cosmic palette (#1d1b3f / #3C3489 / #EEEDFE)
    # and it produced two visible bugs. In **Daylight** the whole app went light
    # except this bar, which stayed dark purple — the single most obviously
    # "wrong colour for the theme" thing in the window. And once Basic came back
    # wearing real theme tokens, the two faces of the SAME window had different
    # title bars: #152144 on Basic against #1d1b3f here, so switching face
    # changed the colour of the chrome around it.
    #
    # A method rather than a constant, because it has to be rebuilt whenever the
    # theme changes; `_apply_theme` re-applies it to the bar it kept a handle on.
    def _hdr_qss(self) -> str:
        p = PALETTES.get(CURRENT_THEME, _MISSION_PALETTE)
        return ("#advHeader{background:%(panel2)s;"
                "border-bottom:1px solid %(border)s;}"
                "#advHeader QLabel{color:%(text)s;}"
                "#hdrLink{color:%(muted)s;background:transparent;border:none;"
                "font-size:13px;padding:3px 6px;border-radius:6px;}"
                "#hdrLink:hover{color:%(text)s;background:%(hover)s;}"
                "#hdrIcon{color:%(muted)s;background:transparent;border:none;"
                "font-size:14px;padding:0;border-radius:6px;}"
                "#hdrIcon:hover{color:%(text)s;background:%(hover)s;}" % p)

    def _drawer_qss(self) -> str:
        p = PALETTES.get(CURRENT_THEME, _MISSION_PALETTE)
        return ("#gearDrawer{background:%(panel)s;"
                "border-left:1px solid %(border)s;}" % p)

    def _grip_qss(self) -> str:
        p = PALETTES.get(CURRENT_THEME, _MISSION_PALETTE)
        return ("background:%(panel2)s;border-top:1px solid %(border)s;" % p)

    def _header(self, title: str) -> tuple:
        """A draggable title bar. Returns (bar, layout) to add controls."""
        bar = _DragBar(self); bar.setObjectName("advHeader"); bar.setFixedHeight(38)
        bar.setStyleSheet(self._hdr_qss())
        self._adv_bar = bar          # so _apply_theme can re-colour it
        hb = QHBoxLayout(bar); hb.setContentsMargins(10, 0, 8, 0); hb.setSpacing(6)
        return bar, hb

    def _build_advanced(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        bar, hb = self._header("Macronaut")
        self._adv_title = QLabel("Macronaut — node builder")
        self._adv_title.setStyleSheet("font-size:12px;font-weight:600;")
        # ⚠ No pin here. Always-on-top came across when the compact face was
        # removed and was given a 📌 in this bar, which put a piece of *setup* in
        # the title bar of a window that has a Settings screen — it is set once
        # and then never touched, unlike the buttons beside it. It lives in
        # Settings → Appearance instead; `_apply_always_on_top` is still the one
        # place that re-applies the flags.
        # The way back to the simple face. A breadcrumb rather than a tab strip:
        # there are two of these, one is a subset of the other, and a two-tab
        # QTabBar spends a permanent strip of the window saying so.
        back = QPushButton("‹ Basic"); back.setObjectName("hdrLink")
        back.setCursor(Qt.PointingHandCursor)
        back.setToolTip("The plain auto-clicker")
        back.clicked.connect(self._show_basic)
        gear2 = QPushButton("⚙"); gear2.setObjectName("hdrIcon")
        gear2.setCursor(Qt.PointingHandCursor); gear2.clicked.connect(self._open_gear)
        gear2.setFixedSize(TitleButton.W, TitleButton.H)   # match the painted pair
        mn = TitleButton("min", bar); mn.setToolTip("Minimize")
        mn.clicked.connect(self._minimize)
        cl = TitleButton("close", bar); cl.setToolTip("Quit")
        cl.clicked.connect(self.close)
        hb.addWidget(back)
        hb.addWidget(self._adv_title)
        hb.addStretch(1)
        hb.addWidget(gear2); hb.addWidget(mn); hb.addWidget(cl)
        v.addWidget(bar)
        v.addWidget(self._sequence_tab, 1)

        # Bottom strip with a resize grip (frameless windows lose native resize).
        gripbar = QWidget(); gripbar.setObjectName("advGrip")
        gripbar.setStyleSheet(self._grip_qss())
        self._adv_grip = gripbar
        gl = QHBoxLayout(gripbar); gl.setContentsMargins(0, 0, 2, 0); gl.setSpacing(0)
        gl.addStretch(1); gl.addWidget(QSizeGrip(gripbar), 0, Qt.AlignBottom | Qt.AlignRight)
        v.addWidget(gripbar)
        return page

    # ── run-button wiring ─────────────────────────────────────────
    def _wire_run_buttons(self):
        self._sequence_tab._recorder.recording_stopped.connect(
            self._on_record_stopped_ui)
        # Route the canvas's own Play / Record buttons through the MainWindow
        # run path, so there is a single run mechanism (F8, the tray, a launcher
        # key and these buttons all agree on state).
        try:
            self._sequence_tab._btn_play.clicked.disconnect()
            self._sequence_tab._btn_play.clicked.connect(self._toggle)
            self._sequence_tab._btn_rec.clicked.disconnect()
            self._sequence_tab._btn_rec.clicked.connect(self._toggle_record)
        except Exception:
            pass

    def _init_window(self):
        # Frameless with our own chrome, set ONCE — including across a face
        # switch.
        #
        # ⚠⚠ This is the whole reason `_apply_face_flags` is not coming back
        # with the Basic face. `setWindowFlags` on a visible window is a
        # destroy-and-recreate of the native window on Windows, and the old
        # two-face shell ran it on *every* switch: that is what the frameless
        # flicker was. It ran it because Basic was
        # always-on-top and Advanced was not — a distinction that no longer
        # exists, since `always_on_top` became one setting for the whole window
        # and `_apply_always_on_top` is the only thing allowed to re-apply
        # flags. Switching face must never touch them.
        flags = Qt.Window | Qt.FramelessWindowHint
        if self._settings.s.always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setMaximumSize(16777215, 16777215)
        # Come back on the face the app was closed on.
        if self._settings.s.last_face == "basic":
            self._show_basic()
        else:
            self._show_advanced()
        self.show()

    # ── shared graph helpers ──────────────────────────────────────
    def _script(self) -> "flow.FlowGraph":
        return self._sequence_tab.graph

    # ── the Auto-Click node behind the Basic face ─────────────────
    def _autoclick_node(self):
        for n in self._script().nodes.values():
            if n.type == flow.N_ACTION and \
                    (n.data.get("step") or {}).get("kind") == "autoclick":
                return n
        return None

    def _is_basic_shaped(self) -> bool:
        """Whether the current flow is one the Basic face can honestly show."""
        g = self._script()
        actions = [n for n in g.nodes.values() if n.type == flow.N_ACTION]
        if not actions:
            # An empty flow is basic-shaped and gets an Auto-Click node built
            # into it. ⚠ A flow whose work lives in If / Loop / Go to nodes
            # holds no action node either, and must never have one injected —
            # that would silently add a click to somebody's real script. Ask
            # `has_work`, which is the one definition of "empty" (2.0.7 shipped
            # the `not actions` version and this is the half it got wrong).
            return not flow.has_work(g)
        return (len(actions) == 1 and
                (actions[0].data.get("step") or {}).get("kind") == "autoclick")

    def _sync_autoclick_node(self):
        """Push the Basic face's settings into the single Auto-Click node,
        creating Start → Auto-Click if the graph is empty/basic-shaped.

        This is what makes the two faces two views of one document: pressing
        Start on Basic runs the same flow engine, through the same entitlement
        gate, as pressing Play on the canvas.
        """
        if not self._is_basic_shaped():
            return
        g = self._script()
        step = {"kind": "autoclick", "data": self._compact.autoclick_data()}
        node = self._autoclick_node()
        if node is None:
            node = g.add_node(flow.N_ACTION, {"step": step}, x=0, y=120)
            start = g.start_node()
            if start and not g.out_edge(start.id, "out"):
                g.add_edge(start.id, node.id, "out")
        else:
            node.data["step"] = step
        if self._stack.currentIndex() == _FaceStack.ADVANCED:
            try:
                self._sequence_tab._canvas.set_graph(g)
            except Exception:
                pass

    def _on_compact_config_changed(self):
        if self._is_basic_shaped() and not self._running:
            self._sync_autoclick_node()

    def _refresh_basic_face(self):
        cur = self._compact.current_script()
        self._compact.set_basic_shaped(self._is_basic_shaped())
        try:
            names = sorted(p.stem for p in scripts_dir().glob("*.json"))
        except Exception:
            names = []
        self._compact.set_scripts(names, cur)

    def _restore_last_script(self):
        """Reopen the script that was open when the app last closed.

        ⚠ Until 4 September 2026 `last_sequence_path` was written by three code
        paths and read by none — it was the Save dialog's default folder and
        nothing else. So somebody who picked a script in Basic, used it, and
        quit was back at "— no script —" the next morning, every morning, on
        the app's single commonest journey.

        This is the cheap third of the three answers to the canvas-loss problem
        (`recovery.py` is the other, and the real one). It restores the last
        *saved* file, so on its own it never recovers unsaved work — which is
        exactly why it was not the answer, and why it is still worth having.

        **Blast radius is deliberately small.** The setting is empty until you
        either save a flow or pick one in Basic, so anyone who has only ever
        used the plain auto-clicker sees no change at all; the people this
        affects are the ones who chose a script, which is the population it is
        for.

        Ordering matters: this runs during construction, and the recovery offer
        fires later from the event loop, so accepting an unsaved flow still
        wins. And a recovery copy that *equals* this file is never offered, so
        the two cannot fight over the canvas.
        """
        try:
            raw = (self._settings.s.last_sequence_path or "").strip()
            if not raw:
                return
            path = Path(raw)
            if not path.is_file():
                return          # deleted, renamed, or on a drive not mounted
            g = flow.FlowGraph.load(str(path))
        except Exception:
            # ⚠ Silent, and it must stay silent. A damaged or unreachable last
            # script is not a reason to put a dialog in front of somebody who
            # has not asked for anything yet — they get the empty canvas the
            # app has always opened on, which is a working app.
            return
        try:
            self._sequence_tab._graph = g
            self._sequence_tab._canvas.set_graph(g)
            self._sequence_tab._set_stats_key(path.stem)
            if path.parent == scripts_dir():
                # Keep the Basic face's dropdown honest about what is loaded.
                # `set_scripts` blocks signals, so this cannot re-enter
                # `_on_script_selected`.
                names = sorted(p.stem for p in scripts_dir().glob("*.json"))
                self._compact.set_scripts(names, path.stem)
            self._compact.set_basic_shaped(self._is_basic_shaped())
        except Exception:
            pass

    def _on_script_selected(self, name: str):
        if not name or name.startswith("—"):
            # "— no script —" returns the Basic face to a fresh clicker.
            self._sequence_tab._graph = self._sequence_tab._new_graph()
            self._sequence_tab._canvas.set_graph(self._sequence_tab._graph)
            self._compact.set_basic_shaped(True)
            # ⚠ And forget it, or `_restore_last_script` hands the script back
            # on the next launch — turning "no script" into a setting that only
            # holds until you close the window.
            self._settings.set("last_sequence_path", "")
            return
        path = scripts_dir() / f"{name}.json"
        if not path.exists():
            return
        try:
            g = flow.FlowGraph.load(str(path))
            self._sequence_tab._graph = g
            self._sequence_tab._canvas.set_graph(g)
            self._settings.set("last_sequence_path", str(path))
        except Exception as e:
            QMessageBox.critical(self, "Load error", str(e))
        self._refresh_basic_face()

    # ── face switching ────────────────────────────────────────────
    #
    # ⚠ Neither of these touches the window flags. See `_init_window`.
    def _show_advanced(self):
        if self._live_face == "basic":
            self._save_basic_geometry()
        self._sequence_tab._canvas.set_graph(self._script())
        self._stack.setCurrentIndex(_FaceStack.ADVANCED)
        self._settings.s.last_face = "advanced"
        self._live_face = "advanced"
        self.setMinimumSize(560, 380)
        self._restore_advanced_geometry()
        QTimer.singleShot(0, self._sequence_tab._canvas.fit)

    def _show_basic(self):
        if self._live_face == "advanced":
            self._save_advanced_geometry()
        self._stack.setCurrentIndex(_FaceStack.BASIC)
        self._settings.s.last_face = "basic"
        self._live_face = "basic"
        self._refresh_basic_face()
        # The face knows its own tight footprint; the window must be allowed
        # down to it, or Advanced's 560x380 floor would keep Basic oversized.
        hint = self._compact.minimumSizeHint()
        self.setMinimumSize(max(256, hint.width()), hint.height())
        self._restore_basic_geometry()

    # ── geometry, one rectangle per face ──────────────────────────
    #
    # ⚠ Save the face that is on screen, never "the current face" read from
    # settings — `_show_*` writes `last_face` as part of switching, so by the
    # time anything else looks the setting already names the face being moved
    # TO. The stack index is the only thing that says what is actually visible.
    def _save_face_geometry(self):
        """Record the geometry of whichever face is showing.

        Nothing is recorded before a face has been shown -- see `_live_face`.
        """
        if self._live_face == "basic":
            self._save_basic_geometry()
        elif self._live_face == "advanced":
            self._save_advanced_geometry()

    def _save_advanced_geometry(self):
        g = self.geometry(); s = self._settings.s
        s.advanced_x, s.advanced_y = g.x(), g.y()
        s.advanced_w, s.advanced_h = g.width(), g.height()

    def _save_basic_geometry(self):
        g = self.geometry(); s = self._settings.s
        s.basic_x, s.basic_y = g.x(), g.y()
        s.basic_w, s.basic_h = g.width(), g.height()

    def _restore_basic_geometry(self):
        """Put Basic back where and how it was left.

        A never-sized face (w/h still -1) is fitted to its own content instead —
        the tight footprint the layout was drawn for. After that the user's size
        wins, including one they made deliberately larger.
        """
        s = self._settings.s
        scr = QApplication.primaryScreen().availableGeometry()
        hint = self._compact.sizeHint()
        w = s.basic_w if s.basic_w > 0 else hint.width()
        h = s.basic_h if s.basic_h > 0 else hint.height()
        w = max(256, min(w, scr.width())); h = max(200, min(h, scr.height()))
        x, y = s.basic_x, s.basic_y
        if x < 0 or y < 0 or x > scr.x() + scr.width() - 40 or \
                y > scr.y() + scr.height() - 40:
            # First run, or the monitor it was parked on has gone. Top-right,
            # which is out of the way of whatever it is about to click.
            x = scr.x() + scr.width() - w - 40
            y = scr.y() + 60
        self.setGeometry(x, y, w, h)

    def _restore_advanced_geometry(self):
        s = self._settings.s
        scr = QApplication.primaryScreen().availableGeometry()
        x, y, w, h = s.advanced_x, s.advanced_y, s.advanced_w, s.advanced_h
        w = max(560, min(w, scr.width())); h = max(380, min(h, scr.height()))
        if x < 0 or y < 0 or x > scr.x() + scr.width() - 60 or \
                y > scr.y() + scr.height() - 60:
            x = scr.x() + (scr.width() - w) // 2
            y = scr.y() + (scr.height() - h) // 2
        self.setGeometry(x, y, w, h)

    # ── minimize / always-on-top ──────────────────────────────────
    def _minimize(self):
        # Plain taskbar minimize. Keeps a taskbar presence so the app is never
        # "running with no window in the taskbar" (#2).
        self._save_face_geometry()
        self.showMinimized()

    def _apply_always_on_top(self, on: bool):
        """Put the window above (or back among) the others. Settings calls this.

        ⚠ The only thing in the app that re-applies window flags, which on
        Windows is a destroy-and-recreate of the native window: it hides, so it
        has to be shown again — and shown *where it was*, since a re-show can
        otherwise land at the default position.
        """
        on = bool(on)
        if on == bool(self.windowFlags() & Qt.WindowStaysOnTopHint):
            return          # nothing to do, and the flicker is not free
        self._settings.s.always_on_top = on
        geo = self.geometry()
        flags = Qt.Window | Qt.FramelessWindowHint
        if on:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setGeometry(geo)
        self.show()

    def _ensure_drawer(self):
        if getattr(self, "_drawer", None) is None:
            self._drawer = self._build_drawer()
            self._drawer_anim = None
        return self._drawer

    def _scroll(self, w):
        sa = QScrollArea(); sa.setWidgetResizable(True); sa.setWidget(w)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        return sa

    def _build_drawer(self) -> QWidget:
        """Settings & Stats as a slide-in drawer overlaying the current face —
        same window, not a separate dialog or full-page swap."""
        d = QWidget(self); d.setObjectName("gearDrawer")
        d.setStyleSheet(self._drawer_qss())
        self._drawer_panel = d
        v = QVBoxLayout(d); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)
        bar, hb = self._header("Settings")
        back = QPushButton("‹ Back"); back.setObjectName("hdrLink")
        back.setCursor(Qt.PointingHandCursor); back.clicked.connect(self._close_gear)
        title = QLabel("Settings & Stats")
        title.setStyleSheet("font-size:12px;font-weight:600;")
        cl = QPushButton("✕"); cl.setObjectName("hdrIcon"); cl.setToolTip("Close")
        cl.setCursor(Qt.PointingHandCursor); cl.clicked.connect(self._close_gear)
        hb.addWidget(back); hb.addSpacing(6); hb.addWidget(title)
        hb.addStretch(1); hb.addWidget(cl)
        v.addWidget(bar)
        tabs = QTabWidget()
        tabs.addTab(self._scroll(self._settings_tab), "Settings")
        tabs.addTab(self._scroll(self._stats_tab), "Stats")
        v.addWidget(tabs, 1)
        d.hide()
        return d

    def _open_gear(self):
        self._stats_tab.refresh_history()
        d = self._ensure_drawer()
        if self._drawer_anim is not None:
            self._drawer_anim.stop()   # cancel an in-flight close so it can't hide us
        w, h = self.width(), self.height()
        d.setGeometry(w, 0, w, h)
        d.show(); d.raise_()
        anim = QPropertyAnimation(d, b"geometry", self)
        anim.setDuration(180)
        anim.setStartValue(QRect(w, 0, w, h))
        anim.setEndValue(QRect(0, 0, w, h))
        anim.start()
        self._drawer_anim = anim

    def _close_gear(self):
        d = getattr(self, "_drawer", None)
        if d is None or not d.isVisible():
            return
        if self._drawer_anim is not None:
            self._drawer_anim.stop()   # cancel an in-flight open before closing
        w, h = self.width(), self.height()
        anim = QPropertyAnimation(d, b"geometry", self)
        anim.setDuration(160)
        anim.setStartValue(QRect(0, 0, w, h))
        anim.setEndValue(QRect(w, 0, w, h))
        anim.finished.connect(d.hide)
        anim.start()
        self._drawer_anim = anim

    def resizeEvent(self, e):
        super().resizeEvent(e)
        d = getattr(self, "_drawer", None)
        if d is not None and d.isVisible():
            d.setGeometry(0, 0, self.width(), self.height())

    # ── recording ─────────────────────────────────────────────────
    def _toggle_record(self):
        # Don't start a recording on top of a live run.
        if self._running and not self._sequence_tab._recorder.is_recording:
            return
        self._sequence_tab._toggle_record()
        rec = self._sequence_tab._recorder.is_recording
        # Suspend the start/stop + trigger hotkeys while recording so the F8 that
        # ends the capture can't also race into launching playback. (The panic
        # hotkey stays live; the recorder catches F8/Esc itself.) Re-armed in
        # _on_record_stopped_ui.
        if rec:
            self._hk_listener.set_hotkeys([])
        self._compact.set_recording(rec)

    def _on_record_stopped_ui(self):
        self._compact.set_recording(False)
        self._refresh_hotkeys()     # re-arm the start/stop hotkey
        self._refresh_basic_face()

    # ── Toggle / Start / Stop ─────────────────────────────────────
    def _toggle(self, hk: str = ""):
        hk = (hk or "").lower().strip()
        s = self._settings.s
        reserved = {(s.start_stop_hotkey or "").lower().strip(),
                    (s.trigger_key or "").lower().strip()}
        bound = s.script_hotkeys or {}
        # Start/Stop wins a collision. The Settings UI prevents one, but a
        # hand-edited settings.json can still contain it, and losing Start/Stop
        # is a much worse failure than a launcher key that does nothing.
        if hk and hk not in reserved and hk in bound:
            self._toggle_bound_script(hk, bound[hk])
            return
        if self._running or (self._countdown_timer and self._countdown_timer.isActive()):
            self._stop()
        else:
            self._start()

    def _toggle_bound_script(self, hk: str, name: str):
        """A launcher key fired. One flow runs at a time — two flows would share
        one mouse and one keyboard and interleave into nonsense — so pressing a
        launcher key stops whatever is running, and starts its own script only
        if that was not already the thing running."""
        busy = self._running or (self._countdown_timer and self._countdown_timer.isActive())
        if busy:
            was = self._active_hotkey
            self._stop()
            if was == hk:
                return                      # same key twice = stop, don't relaunch
        path = scripts_dir() / f"{name}.json"
        if not path.exists():
            # The binding outlives a deleted or renamed script; say so instead of
            # failing silently on a key press with no window in sight.
            self._tray.notify("Macronaut",
                              f"{_fmt_hotkey(hk)}: script “{name}” not found")
            return
        try:
            g = flow.FlowGraph.load(str(path))
        except Exception as exc:
            self._tray.notify("Macronaut", f"{_fmt_hotkey(hk)}: {exc}")
            return
        self._active_hotkey = hk
        self._active_script = name
        self._tray.notify("Macronaut", f"Running “{name}”")
        self._start(graph=g)

    def _start(self, graph=None):
        # While recording, the start/stop hotkey (and the same F8 that stops the
        # recorder) must not also kick off playback.
        if self._sequence_tab._recorder.is_recording:
            return
        # Held across the countdown: _do_start() runs a second or more later.
        self._pending_graph = graph
        delay = self._settings.s.start_delay_seconds
        if delay > 0:
            self._countdown_remaining = delay
            self._update_status(f"Starting in {delay}…")
            self._compact.set_countdown(delay)
            self._countdown_timer = QTimer(self)
            self._countdown_timer.timeout.connect(self._countdown_tick)
            self._countdown_timer.start(1000)
        else:
            self._do_start()

    def _countdown_tick(self):
        self._countdown_remaining -= 1
        if self._countdown_remaining <= 0:
            self._countdown_timer.stop()
            self._do_start()
        else:
            self._update_status(f"Starting in {self._countdown_remaining}…")
            self._compact.set_countdown(self._countdown_remaining)

    def _do_start(self):
        # Everything plays through the one FlowGraph + FlowInterpreter.
        graph = getattr(self, "_pending_graph", None)
        self._pending_graph = None
        if graph is None:
            # ⚠ On the Basic face a bare clicker is (re)materialised as the
            # single Auto-Click node here, immediately before running. The
            # config-changed signal already syncs on every edit, but a fresh
            # launch straight into Basic has had no edit — so without this,
            # pressing Start on an untouched Basic face answers "Nothing to
            # run" while the user is looking at a fully configured clicker.
            if self._live_face == "basic" and self._is_basic_shaped():
                self._sync_autoclick_node()
            if not self._sequence_tab.has_content():
                QMessageBox.information(
                    self, "Nothing to run",
                    "Add a node, record something, or load a script first.")
                return
        elif not flow.has_work(graph):
            # A launcher key has no window to put a dialog in front of.
            self._tray.notify("Macronaut",
                              f"“{self._active_script}” has nothing to run")
            self._active_hotkey = ""
            return
        worker = self._sequence_tab.start_playback(graph)
        if worker is None:
            self._active_hotkey = ""
            return
        self._flow_graph_snapshot = graph if graph is not None else self._sequence_tab.graph
        self._live_count = 0
        worker.node_started.connect(self._on_flow_node)
        worker.progress.connect(self._on_autoclick_progress)
        worker.finished.connect(lambda *_: self._on_finished())
        worker.error_occurred.connect(self._on_error)

        self._running = True
        self._stats.start_session()
        self._update_status("Running")
        self._compact.set_running(True, 0.0)
        self._tray.set_state(True)

    def _stop(self):
        if self._countdown_timer and self._countdown_timer.isActive():
            self._countdown_timer.stop()
        self._pending_graph = None
        self._active_hotkey = ""
        self._sequence_tab.stop_playback()
        self._running = False
        self._stats.end_session()
        self._update_status("Idle")
        self._compact.set_running(False)
        self._tray.set_state(False)
        self._stats_tab.refresh_history()

    # ── Worker callbacks ──────────────────────────────────────────
    def _on_autoclick_progress(self, count: int):
        """Live auto-click count (the Auto-Click node ticks ~5×/s). Feed the
        delta into stats; the refresh timer turns it into live CPS."""
        delta = count - getattr(self, "_live_count", 0)
        if delta > 0:
            for _ in range(delta):
                self._stats.record_click()
            self._live_count = count

    def _on_flow_node(self, node_id: str):
        """Feed node-graph playback into the live stats counters."""
        g = getattr(self, "_flow_graph_snapshot", None)
        if not g:
            return
        node = g.nodes.get(node_id)
        if not node or node.type != flow.N_ACTION:
            return
        step = node.data.get("step", {})
        if not step.get("data", {}).get("enabled", True):
            return
        kind = step.get("kind")
        d = step.get("data", {})
        if kind == "click":
            self._stats.record_click()
        elif kind in ("key", "combo"):
            self._stats.record_keystroke()
        elif kind == "text":
            for _ in d.get("text", ""):
                self._stats.record_keystroke()
        elif kind in ("wait_image", "wait_text", "wait_pixel") and d.get("click"):
            self._stats.record_click()

    def _on_status_changed(self, status: str):
        pass  # status already managed by _do_start / _stop

    def _on_finished(self):
        if self._running:   # natural completion (limit reached / loops done)
            self._stop()
            self._tray.notify("Macronaut", "Automation finished.")

    def _on_error(self, msg: str):
        self._stop()
        QMessageBox.critical(self, "Automation Error", msg)

    # ── Status helpers ────────────────────────────────────────────
    def _update_status(self, text: str, dot_color: str = "#959db1"):
        self._status_text = text

    def _refresh_stats(self):
        running = self._running
        self._check_guard()
        self._stats_tab.refresh_live(running)
        if running:
            self._compact.update_live_cps(self._stats.current_cps())

    # ── Geometry persistence ──────────────────────────────────────
    def _restore_geometry(self):
        s = self._settings.s
        if s.window_x >= 0:
            self.setGeometry(s.window_x, s.window_y, s.window_w, s.window_h)
        else:
            self.resize(s.window_w, s.window_h)
            screen = QApplication.primaryScreen().availableGeometry()
            self.move((screen.width() - s.window_w) // 2,
                      (screen.height() - s.window_h) // 2)

    def _bring_to_front(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ── Lifecycle ─────────────────────────────────────────────────
    def _shutdown(self):
        """Tear everything down so nothing keeps running after the app closes:
        stop any automation, release the global F8 keyboard hook, drop the tray
        icon, and stop timers. Safe to call more than once."""
        if getattr(self, "_shutting_down", False):
            return
        self._shutting_down = True
        try:
            self._stop()                       # stop clicker / sequence + end session
        except Exception:
            pass
        try:
            self._sequence_tab.shutdown()      # stop any recorder/playback thread
        except Exception:
            pass
        try:
            # A Hold-down node's key is released by the worker's own finally —
            # but both quit paths end in _os._exit(0), which runs no atexit and
            # gives that thread no chance to reach it. A W left down outlives
            # the app that pressed it, so it comes up here, from this thread,
            # while there is still a process to do it with.
            flow_exec.release_all_held()
        except Exception:
            pass
        try:
            if self._refresh_timer:
                self._refresh_timer.stop()
        except Exception:
            pass
        try:
            self._hk_listener.stop()           # release the global keyboard hook
        except Exception:
            pass
        try:
            self._panic_listener.stop()        # release the panic hotkey hook
        except Exception:
            pass
        try:
            self._tray.hide()                  # remove the tray icon
        except Exception:
            pass

    def _quit_app(self):
        # Fully exit: stop the hotkey hook and any automation, then end the loop.
        self._force_quit = True
        self._save_state()
        self._shutdown()
        # This exit is deliberate, so retire the session file. It has to happen
        # here: _os._exit skips atexit, so there is no later opportunity.
        crashreport.disarm()
        QApplication.instance().quit()
        _os._exit(0)

    def closeEvent(self, event):
        # Closing the window fully quits the app — the global hotkey and any
        # running automation are stopped so nothing lingers in the background.
        self._save_state()
        self._shutdown()
        # Same as _quit_app: the process is about to end with _os._exit(0),
        # which runs no atexit handler, so "we meant to close" is recorded now
        # or never. Miss this and every clean shutdown reports as a crash.
        crashreport.disarm()
        super().closeEvent(event)
        QApplication.instance().quit()
        # Guarantee the process dies even if a background input listener or
        # worker thread is still winding down (state is already persisted).
        _os._exit(0)

    def _save_state(self):
        self._save_face_geometry()
        self._compact.save_to_settings()
        self._sequence_tab.save_to_settings()
        self._settings_tab.save_to_settings()
        self._settings.save()
        # ⚠ Last, and inside its own guard. Both quit paths run this, and both
        # end in `_os._exit(0)` — an exception escaping here would lose the
        # geometry and settings that were just written, to protect a canvas
        # nobody had asked to keep. `recovery.write` swallows its own errors
        # too; this is the belt to that pair of braces.
        try:
            self._sequence_tab._write_recovery(force=True)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    # Update hand-off: when Macronaut is launched with --apply-update it is the
    # NEWLY DOWNLOADED copy, whose only job is to wait for the old process to
    # exit, replace it and relaunch. No GUI, no settings, nothing else — so this
    # must be the very first thing main() does.
    if updater.APPLY_FLAG in sys.argv:
        sys.exit(updater.run_apply_mode(sys.argv))

    # Diagnostic self-test. Exercises the dependencies that fail SILENTLY when
    # frozen (numpy/cv2 image matching, OCR) plus the bundled legal text, then
    # exits without ever building a GUI. This is the only cheap way to tell a
    # working release from one where image matching quietly does nothing.
    if "--selftest" in sys.argv:
        import selftest
        sys.exit(selftest.run())

    # Crash capture, armed as early as there is anything worth capturing — but
    # strictly AFTER the two early exits above, because --apply-update and
    # --selftest both end without a GUI and would otherwise each look like a
    # crash. Apply-mode runs on every update, so that false positive would
    # drown the real ones. (crashreport.install refuses on its own too.)
    #
    # Harvest first, arm second: harvesting is what turns last session's
    # abandoned session file into a report, and arming immediately writes a new
    # one that must not be mistaken for it.
    try:
        crashreport.harvest()
        crashreport.install()
        # Qt's message handler is the only thing that ever sees a qFatal's text
        # before the abort — e.g. "QThread: Destroyed while thread is still
        # running", which is what 2.0.8's Stop crash actually printed.
        crashreport.install_qt_handler()
    except Exception:
        pass

    # Remove the previous version left behind by a completed update, plus any
    # stale downloads. Cheap, best-effort, and keeps the folder from growing.
    try:
        updater.cleanup()
    except Exception:
        pass

    # Startup OCR dependency check (Phase 3): report which engine is active and
    # warn clearly if there is none.
    try:
        print(f"[OCR] {_ocr.status_message()}")
        if not _ocr.available():
            print("[OCR] Text-recognition steps are disabled until an engine is available.")
    except Exception:
        pass

    # Give the process an explicit AppUserModelID so Windows shows OUR window
    # icon (the Macronaut helmet) in the taskbar instead of pythonw's icon.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Macronaut.App.1")
    except Exception:
        pass

    # High-DPI awareness: a frameless window sized and positioned by us is
    # mis-placed on scaled displays (125%/150%) without it.
    #
    # Qt6 enables high-DPI scaling unconditionally, so AA_EnableHighDpiScaling and
    # AA_UseHighDpiPixmaps are no-ops kept only for source compatibility — setting
    # them does nothing. What IS still tunable is how fractional scale factors are
    # rounded, and the Qt6 default (round 125% -> 100%) puts every saved geometry
    # and every screen-coordinate pick out by a fifth on exactly the displays
    # this was guarding. PassThrough keeps the real fractional factor. Must be
    # set before the QApplication is constructed.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("Macronaut")
    app.setApplicationVersion(version.__version__)
    app.setWindowIcon(app_icon())
    # Quitting is handled by closeEvent (_os._exit(0)), not by Qt's
    # lastWindowClosed mechanism — disabling that so hiding the main window
    # (e.g. during coordinate pick) doesn't silently terminate the process.
    app.setQuitOnLastWindowClosed(False)

    # ⚠ The stylesheet has to be in force BEFORE the first widget is built, and
    # it used to be applied straight after `MainWindow()` instead. Every size a
    # widget measures during construction is measured through whatever style is
    # active at that moment, so the whole UI was sizing itself in Qt's default
    # 9 pt font with the default padding and then being repainted in the theme's
    # 14 pt one. That is what clipped "Comment" in the Add-node palette: the
    # buttons' size hints came back far too small, the `max(132, ...)` floor won
    # on width, and they were frozen at ~110 px of content for a label that
    # needs 92 px once the real font arrives — so the last letters were cut, and
    # no amount of slack on the measured term could help because the measured
    # term was never the one being used.
    #
    # Reading the theme early costs one extra parse of settings.json (MainWindow
    # reads it again for itself), which is cheap and happens once. It also means
    # `CURRENT_THEME` is correct *during* construction — widgets that branch on
    # it were previously all built as though the theme were the default.
    global CURRENT_THEME
    try:
        _theme = SettingsManager().s.theme
    except Exception:
        _theme = DEFAULT_THEME
    CURRENT_THEME = _theme if _theme in THEMES else DEFAULT_THEME
    app.setStyleSheet(THEMES[CURRENT_THEME])

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
