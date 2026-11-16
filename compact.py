"""
Macronaut — the Basic face: a plain auto-clicker in Macronaut's own theme.

It says what OP Auto Clicker 2.1 says, because that is the vocabulary the
people who want an auto-clicker already have: click interval in
hours/mins/secs/ms, which mouse button, repeat N times or until stopped, and
where to click. Macronaut-only additions OP lacks — Max speed, Human mode,
Stop-after, a record/play script row and the Advanced breadcrumb — sit in their
own places. Self-sufficient: someone who only ever wants a clicker never has to
open a node.

It does not *look* like OP. See the note on `_build` for the layout and on
`_QSS_TMPL` for the chrome.

The *colours* are not OP's and are not this file's either — see `_tokens`. The
face follows the app's live theme.

Decoupled from MainWindow: it reads/writes the shared SettingsManager and talks
to the window purely through signals. `autoclick_data()` returns the dict the
flow `Auto-Click` node (and its executor) consume, which is what makes Basic
and Advanced two views of one document rather than two programs.

⚠ Free, permanently, and by construction rather than by a rule written down
somewhere: an Auto-Click node is in neither `entitlements.PRO_ACTION_KINDS` nor
`PRO_NODE_TYPES`, and a Basic-shaped flow is one working step against a limit
of twenty. A test pins that so a licence check can never quietly swallow it.
"""
from PySide6.QtCore import Qt, Signal, QSize, QTimer, QPointF
from PySide6.QtGui import QPainter, QColor, QFont, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QButtonGroup,
    QApplication, QComboBox, QSpinBox, QCheckBox, QSizePolicy, QGroupBox,
    QAbstractButton,
    QRadioButton, QFrame,
)

# ── Theme ─────────────────────────────────────────────────────────────────────
# This face used to carry its own hardcoded "cosmic" palette from 2.0, which is
# why it looked like a different application the moment anyone picked Graphite
# or Daylight. It now reads the app's live theme tokens instead, so there is
# exactly one place colours are decided (`main.PALETTES`) and this face follows
# whatever the rest of the window is wearing.
#
# ⚠ The mapping is here rather than in main.py on purpose: main imports compact,
# so compact must never import main. The palette arrives as a plain dict.
_FALLBACK_PALETTE = {
    "bg": "#070b16", "panel": "#0f1830", "panel2": "#152144", "hover": "#1c2950",
    "border": "#26345a", "text": "#e8edf8", "muted": "#8694b8",
    "accent": "#6c7cff", "accentHover": "#8a96ff", "accentText": "#ffffff",
    "inputBg": "#0a1124", "green": "#2fd3a4", "greenHover": "#49ddb4",
    "red": "#ff5a6a", "amber": "#f5b14b", "selBg": "#22306a",
}


def _tokens(pal: dict) -> dict:
    """The names this face's stylesheet uses, resolved from an app palette.

    Two deliberate reassignments from the 2.0 original, so Basic agrees with the
    rest of Macronaut rather than with OP Auto Clicker's colour choices:
    Start is green and Stop is red, matching `#btn_start` / `#btn_stop` on the
    canvas. In 2.0 Start was accent-purple and Stop went green when armed, which
    read as "green means go" on the button that halts everything.
    """
    p = dict(_FALLBACK_PALETTE)
    p.update(pal or {})
    return dict(
        bg=p["bg"], panel=p["panel"], panel2=p["panel2"], grp=p["panel"],
        tb=p["inputBg"], ctl=p["panel2"], line=p["border"], acc=p["accent"],
        acc2=p["accentHover"], accTxt=p["accentText"], hov=p["hover"],
        txt=p["text"], txt2=p["muted"], mut=p["muted"], sel=p["selBg"],
        go=p["green"], go2=p["greenHover"], stop=p["red"], warn=p["amber"],
    )


# Live tokens for anything that paints itself rather than using the stylesheet
# (TitleButton, and the two coloured glyph buttons on the script row).
_C = _tokens(_FALLBACK_PALETTE)

_QSS_TMPL = """
#compactRoot {{ background:{bg}; }}
#cbody {{ background:transparent; }}

/* Window chrome. ⚠ These are the same tokens the canvas's own header uses, and
   that is the point: the two faces are one window, so switching between them
   must not change the colour of the title bar. It used to -- Basic followed the
   theme while the canvas header was hardcoded #1d1b3f, which also left the
   canvas wearing a dark purple bar in the light theme. */
#titleBar {{ background:{panel2}; border-bottom:1px solid {line}; }}
#brandName {{ color:{txt}; font-size:12.5px; font-weight:600; }}
#tbIcon {{ color:{txt2}; background:transparent; border:none; font-size:13px;
          border-radius:6px; padding:0; }}
#tbIcon:hover {{ color:{txt}; background:{hov}; }}
#tbIcon:checked {{ color:{acc}; }}

/* Sections. ⚠ Deliberately NOT QGroupBox. Its title sits in a notch cut out of
   its own border, and at this size that notch -- five of them, in accent purple
   -- was the least tidy thing on the face. A small label above a plain rounded
   panel says exactly the same and draws cleanly at any DPI. */
QFrame#card {{ background:{panel}; border:1px solid {line}; border-radius:11px; }}
#sectionLabel {{ color:{txt2}; font-size:10px; font-weight:700;
                letter-spacing:.1em; }}
/* The label column. Fixed width, so every control in the card starts at the
   same x -- which is most of what "tidy" means in a form this small. */
#fieldLabel {{ color:{txt2}; font-size:12px; }}
QLabel {{ color:{txt}; font-size:12px; }}
#unit {{ color:{txt2}; font-size:11.5px; }}

QRadioButton {{ color:{txt}; font-size:12px; spacing:7px; }}
QCheckBox {{ color:{txt}; font-size:12px; spacing:7px; }}
/* ⚠ Stated in full. This face sets its own widget stylesheet, which does NOT
   inherit the app sheet's `border-radius` for these sub-controls -- so
   overriding only width/height (all the 2.0 rule did) left both painting as
   SQUARES. A radio that looks like a checkbox says "10 times" and "until
   stopped" are independent when they are exclusive. */
QCheckBox::indicator, QRadioButton::indicator {{
            width:14px; height:14px; background:{tb};
            border:1px solid {line}; }}
QCheckBox::indicator {{ border-radius:4px; }}
QRadioButton::indicator {{ border-radius:8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
            background:{acc}; border-color:{acc}; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color:{acc}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color:{mut}; }}

QSpinBox {{ background:{tb}; color:{txt}; border:1px solid {line};
            border-radius:7px; padding:4px 7px; font-size:12.5px;
            selection-background-color:{acc}; selection-color:{accTxt}; }}
QSpinBox:focus {{ border-color:{acc}; }}
QSpinBox:disabled {{ color:{mut}; background:{panel2}; }}

QComboBox {{ background:{tb}; color:{txt}; border:1px solid {line};
            border-radius:7px; padding:5px 9px; font-size:11.5px; }}
QComboBox:hover {{ border-color:{acc}; }}
QComboBox QAbstractItemView {{ background:{panel2}; color:{txt};
            border:1px solid {line};
            selection-background-color:{acc}; selection-color:{accTxt}; }}

/* Segmented choices. Smaller than 2.0's: at 6px/16px they were taller than the
   Start button's own text and read as the loudest thing on the panel. */
QPushButton#seg {{ background:{tb}; color:{txt2}; border:1px solid {line};
                  border-radius:7px; padding:5px 13px; font-size:12px; }}
QPushButton#seg:hover {{ color:{txt}; border-color:{acc}; }}
QPushButton#seg:checked {{ background:{acc}; color:{accTxt}; border-color:{acc};
                          font-weight:600; }}
QPushButton#seg:disabled {{ color:{mut}; border-color:{line}; }}

/* One primary. Start is the only filled button on the face; Stop is an outline
   until a run makes it meaningful, and then it turns red. */
QPushButton#start {{ background:{go}; color:{accTxt}; border:none;
            border-radius:9px; font-size:13.5px; font-weight:700; padding:10px 0; }}
QPushButton#start:hover {{ background:{go2}; }}
QPushButton#start:disabled {{ background:{panel2}; color:{mut}; }}
QPushButton#stop {{ background:transparent; color:{mut}; border:1px solid {line};
            border-radius:9px; font-size:13.5px; font-weight:600; padding:10px 0; }}
QPushButton#stop:enabled {{ background:{stop}; color:{accTxt}; border-color:{stop}; }}
/* ⚠ Text, not a chip. It was a filled block the same height as Start and Stop,
   sitting right beside them -- so the one thing on that row you cannot press
   looked exactly like the two you can. */
#hotkey {{ color:{txt2}; font-size:11px; }}

QPushButton#iconBtn {{ background:{tb}; border:1px solid {line};
                      border-radius:7px; padding:5px 9px; font-size:12.5px; }}
QPushButton#iconBtn:hover {{ border-color:{acc}; }}
QPushButton#iconBtn:disabled {{ color:{mut}; }}

#footLink {{ color:{txt2}; background:transparent; border:none; font-size:11.5px; }}
#footLink:hover {{ color:{acc}; }}
#note {{ color:{warn}; font-size:11px; }}
"""


class TitleButton(QAbstractButton):
    """A painted window button — minimize ("min") or close ("close").

    Drawn rather than typed. The glyphs these used to be (an en-dash and U+2715)
    come from whatever font happens to own them, which is why the X looked
    hairline next to the rest of the bar and the minimize dash sat off-centre.
    Two strokes of a known width, centred in a known box, look the same on every
    machine and stay crisp at any DPI. Close hovers red, the way a window button
    is expected to.
    """
    W, H = 30, 24
    _BOX  = 9          # side of the square the strokes are drawn in
    _PEN  = 1.4        # stroke width — thin enough to be quiet, thick to read

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_Hover, True)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hot = self.underMouse()
        if hot:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#E5484D") if self.kind == "close"
                       else QColor(_C["tb"]))
            p.drawRoundedRect(self.rect().adjusted(1, 2, -1, -2), 5, 5)
        col = QColor("#ffffff") if hot else QColor(_C["txt2"])
        p.setPen(QPen(col, self._PEN, Qt.SolidLine, Qt.RoundCap))
        cx, cy = self.width() / 2, self.height() / 2
        r = self._BOX / 2
        if self.kind == "close":
            p.drawLine(QPointF(cx - r, cy - r), QPointF(cx + r, cy + r))
            p.drawLine(QPointF(cx - r, cy + r), QPointF(cx + r, cy - r))
        else:
            p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
        p.end()

    # Hover is painted, so the widget has to know when it changed.
    def enterEvent(self, e):
        self.update(); super().enterEvent(e)

    def leaveEvent(self, e):
        self.update(); super().leaveEvent(e)


def _seg(options, group, parent):
    """A segmented toggle: a row of mutually-exclusive checkable buttons."""
    row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(4)
    for i, label in enumerate(options):
        b = QPushButton(label); b.setObjectName("seg"); b.setCheckable(True)
        b.setCursor(Qt.PointingHandCursor)
        group.addButton(b, i)
        row.addWidget(b)
    row.addStretch(1)
    w = QWidget(parent); w.setLayout(row)
    return w


class _PickOverlay(QWidget):
    """Full-virtual-desktop overlay; emits (x, y) on left-click, closes on Esc."""
    picked    = Signal(int, int)
    cancelled = Signal()

    def __init__(self):
        super().__init__(None,
                         Qt.Window | Qt.FramelessWindowHint |
                         Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)
        geom = QApplication.primaryScreen().virtualGeometry()
        self.setGeometry(geom)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 60))
        p.setPen(QColor(255, 255, 255, 200))
        f = QFont(); f.setPointSize(13); p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter,
                   "Click to set position  •  Esc to cancel")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            # Qt6 replaced globalX()/globalY() with globalPosition(), which is a
            # QPointF — toPoint() so the signal still carries ints.
            gp = e.globalPosition().toPoint()
            self.picked.emit(gp.x(), gp.y())
            self.close()


class CompactFace(QWidget):
    start_stop_requested = Signal()
    record_requested     = Signal()
    play_script_requested = Signal()
    advanced_requested   = Signal()
    settings_requested   = Signal()
    minimize_requested   = Signal()
    close_requested      = Signal()
    pin_toggled          = Signal(bool)
    script_changed       = Signal(str)
    config_changed       = Signal()
    fit_requested        = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._running = False
        self._recording = False
        self._basic_shaped = True
        self._drag_pos = None
        self.setObjectName("compactRoot")
        self._build()
        # After _build, not before: apply_theme restyles widgets this creates.
        self.apply_theme(_FALLBACK_PALETTE)
        self.load_from_settings()

    def apply_theme(self, palette: dict):
        """Re-colour the face from an app palette (`main.PALETTES[theme]`).

        ⚠ Also refreshes the module-level `_C`, because TitleButton paints
        itself with QPainter and cannot read a colour out of a stylesheet — the
        same reason `main.PALETTES` exists alongside `main.THEMES`.
        """
        global _C
        _C = _tokens(palette)
        self.setStyleSheet(_QSS_TMPL.format(**_C))
        # Two glyphs carry meaning through colour alone, so they are set
        # directly rather than through an object name.
        self._rec.setStyleSheet(f"color:{_C['stop']};")
        self._play.setStyleSheet(f"color:{_C['go']};")
        self.update()

    # ── build ─────────────────────────────────────────────────────────
    #
    # Layout note. 2.0 laid this out as five OP Auto Clicker group boxes:
    # interval, options, repeat, cursor, extras -- two of them side by side and
    # each sized to its own content. Three problems compounded: the boxes were
    # ragged (two columns of different heights, stretched to match, so one was
    # always part empty), every control started at a different x, and "Extras"
    # was a large panel holding two widgets.
    #
    # It is three cards now, all full width, with one label column so every
    # control lines up. Same controls, same order of ideas, nothing removed.
    LABEL_W = 58

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        root.addWidget(self._build_titlebar())

        body = QWidget(); body.setObjectName("cbody")
        b = QVBoxLayout(body); b.setContentsMargins(14, 12, 14, 13); b.setSpacing(10)
        root.addWidget(body, 1)

        b.addWidget(self._build_interval_card())
        b.addWidget(self._build_click_card())
        b.addWidget(self._build_options_card())
        b.addLayout(self._build_run_row())
        b.addLayout(self._build_script_row())
        b.addLayout(self._build_foot_row())
        # ⚠ The slack goes at the BOTTOM, not above the Start row. Basic is
        # resizable now, and with the stretch in the middle a window dragged
        # taller opened a growing void between the last card and the buttons —
        # which reads as a layout that has come apart rather than as a panel
        # with room to spare. Everything stays gathered at the top instead.
        b.addStretch(1)

        self._note = QLabel("Edited on the canvas — open Advanced to change it")
        self._note.setObjectName("note"); self._note.setWordWrap(True)
        self._note.setVisible(False)
        b.addWidget(self._note)

    # ── small builders ────────────────────────────────────────────────
    def _card(self, title: str):
        """A titled section: (frame, content layout). See the note on _build for
        why this is not a QGroupBox."""
        f = QFrame(); f.setObjectName("card")
        v = QVBoxLayout(f); v.setContentsMargins(13, 10, 13, 12); v.setSpacing(9)
        lab = QLabel(title.upper()); lab.setObjectName("sectionLabel")
        v.addWidget(lab)
        inner = QVBoxLayout(); inner.setContentsMargins(0, 0, 0, 0); inner.setSpacing(8)
        v.addLayout(inner)
        return f, inner

    def _field(self, text: str) -> QLabel:
        """A label in the fixed left column. The fixed width is the whole point:
        it is what makes every control on the face start at the same x."""
        lab = QLabel(text); lab.setObjectName("fieldLabel")
        lab.setFixedWidth(self.LABEL_W)
        return lab

    def _spin(self, lo, hi, w=52, buttons=False):
        s = QSpinBox(); s.setRange(lo, hi); s.setAlignment(Qt.AlignRight)
        if not buttons:
            s.setButtonSymbols(QSpinBox.NoButtons)
        s.setMaximumWidth(w)
        s.valueChanged.connect(lambda *_: self.config_changed.emit())
        return s

    def _unit(self, text):
        lab = QLabel(text); lab.setObjectName("unit")
        return lab

    def _segment(self, label: str, group, ident: int):
        b = QPushButton(label); b.setObjectName("seg"); b.setCheckable(True)
        b.setCursor(Qt.PointingHandCursor)
        group.addButton(b, ident)
        return b

    # ── cards ─────────────────────────────────────────────────────────
    def _build_interval_card(self):
        card, v = self._card("Click interval")
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(5)
        self._iv_hr  = self._spin(0, 23, 44)
        self._iv_min = self._spin(0, 59, 44)
        self._iv_sec = self._spin(0, 59, 44)
        self._iv_ms  = self._spin(0, 999, 50)
        for spin, unit in ((self._iv_hr, "h"), (self._iv_min, "m"),
                           (self._iv_sec, "s"), (self._iv_ms, "ms")):
            h.addWidget(spin); h.addWidget(self._unit(unit))
            h.addSpacing(4)
        h.addStretch(1)
        self._max_chk = QCheckBox("Max speed")
        self._max_chk.setToolTip("Click as fast as this machine can")
        self._max_chk.toggled.connect(self._on_max_toggle)
        h.addWidget(self._max_chk)
        v.addLayout(h)
        return card

    def _build_click_card(self):
        card, v = self._card("Click")

        # Button
        self._btn_grp = QButtonGroup(self)
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(6)
        row.addWidget(self._field("Button"))
        for i, label in enumerate(["Left", "Right"]):
            row.addWidget(self._segment(label, self._btn_grp, i))
        row.addStretch(1)
        self._btn_grp.idClicked.connect(lambda *_: self.config_changed.emit())
        v.addLayout(row)

        # Where
        self._cursor_grp = QButtonGroup(self)
        row2 = QHBoxLayout(); row2.setContentsMargins(0, 0, 0, 0); row2.setSpacing(6)
        row2.addWidget(self._field("Where"))
        self._rb_current = self._segment("At the cursor", self._cursor_grp, 0)
        self._rb_pick = self._segment("Pick a spot", self._cursor_grp, 1)
        self._rb_pick.setToolTip("Capture coordinates from the screen")
        self._rb_current.setChecked(True)
        row2.addWidget(self._rb_current); row2.addWidget(self._rb_pick)
        row2.addStretch(1)
        self._fixed_x = self._spin(0, 32000, 54)
        self._fixed_y = self._spin(0, 32000, 54)
        self._fixed_x.setEnabled(False); self._fixed_y.setEnabled(False)
        row2.addWidget(self._unit("X")); row2.addWidget(self._fixed_x)
        row2.addSpacing(2)
        row2.addWidget(self._unit("Y")); row2.addWidget(self._fixed_y)
        self._cursor_grp.idClicked.connect(self._on_cursor_mode)
        self._rb_pick.clicked.connect(self._start_pick)
        v.addLayout(row2)

        # Repeat
        self._repeat_grp = QButtonGroup(self)
        row3 = QHBoxLayout(); row3.setContentsMargins(0, 0, 0, 0); row3.setSpacing(6)
        row3.addWidget(self._field("Repeat"))
        self._rb_until = QRadioButton("Until stopped")
        self._rb_count = QRadioButton("")
        self._repeat_count = self._spin(1, 10_000_000, 74)
        self._repeat_count.setValue(10)
        self._repeat_grp.addButton(self._rb_count, 1)
        self._repeat_grp.addButton(self._rb_until, 0)
        self._rb_until.setChecked(True)
        self._repeat_grp.idClicked.connect(self._on_repeat_mode)
        # ⚠ The two choices sit together, with the stretch AFTER them. Pushed to
        # opposite ends of the row — which is what a stretch between them does —
        # the second one reads as an orphaned radio button with no label at all,
        # because its label is the number box sitting next to it.
        self._rb_count.setToolTip("Stop after a set number of clicks")
        row3.addWidget(self._rb_until)
        row3.addSpacing(20)
        row3.addWidget(self._rb_count)
        row3.addWidget(self._repeat_count)
        row3.addWidget(self._unit("times"))
        row3.addStretch(1)
        v.addLayout(row3)
        return card

    def _build_options_card(self):
        card, v = self._card("Options")
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(12)
        self._human = QCheckBox("Human mode")
        self._human.setToolTip("Jitter the cursor so the movement is not machine-perfect")
        self._human.toggled.connect(lambda *_: self.config_changed.emit())
        h.addWidget(self._human)
        h.addStretch(1)
        h.addWidget(self._unit("Stop after"))
        self._stop_secs = self._spin(0, 86400, 78)
        self._stop_secs.setSuffix(" s"); self._stop_secs.setSpecialValueText("Never")
        h.addWidget(self._stop_secs)
        v.addLayout(h)
        return card

    # ── rows below the cards ──────────────────────────────────────────
    def _build_run_row(self):
        btns = QHBoxLayout(); btns.setSpacing(8)
        self._start = QPushButton("Start"); self._start.setObjectName("start")
        self._start.setCursor(Qt.PointingHandCursor)
        self._start.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._start.clicked.connect(self.start_stop_requested.emit)
        self._stop_btn = QPushButton("Stop"); self._stop_btn.setObjectName("stop")
        self._stop_btn.setCursor(Qt.PointingHandCursor)
        self._stop_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self.start_stop_requested.emit)
        self._chip = QLabel("F8"); self._chip.setObjectName("hotkey")
        self._chip.setAlignment(Qt.AlignCenter)
        btns.addWidget(self._start, 3); btns.addWidget(self._stop_btn, 2)
        btns.addSpacing(2)
        btns.addWidget(self._chip)
        return btns

    def _build_script_row(self):
        scr = QHBoxLayout(); scr.setContentsMargins(0, 0, 0, 0); scr.setSpacing(6)
        scr.addWidget(self._field("Script"))
        self._scripts = QComboBox()
        self._scripts.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._scripts.currentTextChanged.connect(self._on_script_changed)
        self._rec = QPushButton("●"); self._rec.setObjectName("iconBtn")
        self._rec.setToolTip("Record a script (F8/Esc to stop)")
        self._rec.setCursor(Qt.PointingHandCursor)
        self._rec.clicked.connect(self.record_requested.emit)
        self._play = QPushButton("▶"); self._play.setObjectName("iconBtn")
        self._play.setToolTip("Play the selected script")
        self._play.setCursor(Qt.PointingHandCursor)
        self._play.clicked.connect(self.play_script_requested.emit)
        scr.addWidget(self._scripts, 1); scr.addWidget(self._rec); scr.addWidget(self._play)
        return scr

    def _build_foot_row(self):
        ft = QHBoxLayout(); ft.setContentsMargins(0, 0, 0, 0)
        adv = QPushButton("Advanced ›"); adv.setObjectName("footLink")
        adv.setCursor(Qt.PointingHandCursor)
        adv.setToolTip("The node canvas — build a script with steps and branches")
        adv.clicked.connect(self.advanced_requested.emit)
        ft.addStretch(1); ft.addWidget(adv)
        return ft

    def _build_titlebar(self) -> QWidget:
        bar = QWidget(); bar.setObjectName("titleBar"); bar.setFixedHeight(34)
        bar.mousePressEvent = self._tb_press
        bar.mouseMoveEvent = self._tb_move
        lay = QHBoxLayout(bar); lay.setContentsMargins(11, 0, 8, 0); lay.setSpacing(6)
        brand = QLabel("\U0001F680  Macronaut"); brand.setObjectName("brandName")
        lay.addWidget(brand); lay.addStretch(1)

        self._btn_min = TitleButton("min", bar)
        self._btn_min.setToolTip("Minimize")
        self._btn_min.clicked.connect(self.minimize_requested.emit)
        self._btn_pin = QPushButton("\U0001F4CC"); self._btn_pin.setObjectName("tbIcon")
        self._btn_pin.setCheckable(True); self._btn_pin.setToolTip("Always on top")
        self._btn_pin.setCursor(Qt.PointingHandCursor)
        self._btn_pin.toggled.connect(self.pin_toggled.emit)
        self._btn_gear = QPushButton("⚙"); self._btn_gear.setObjectName("tbIcon")
        self._btn_gear.setToolTip("Settings & Stats")
        self._btn_gear.setCursor(Qt.PointingHandCursor)
        self._btn_gear.clicked.connect(self.settings_requested.emit)
        # Same footprint as the painted buttons, so the three sit on one rhythm.
        for _b in (self._btn_gear, self._btn_pin):
            _b.setFixedSize(TitleButton.W, TitleButton.H)
        self._btn_close = TitleButton("close", bar)
        self._btn_close.setToolTip("Quit Macronaut")
        self._btn_close.clicked.connect(self.close_requested.emit)
        for w in (self._btn_min, self._btn_gear, self._btn_close):
            lay.addWidget(w)
        self._btn_pin.setParent(bar)  # in the hierarchy but off-layout and hidden
        self._btn_pin.hide()
        return bar

    # Keep a tight, OP-like footprint: cap the width and let the content
    # (spinboxes/stretches) compress to fit instead of ballooning wide.
    _PREF_W = 500

    def sizeHint(self) -> QSize:
        return QSize(self._PREF_W, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        return QSize(self._PREF_W, super().minimumSizeHint().height())

    # ── frameless drag (title bar) ────────────────────────────────────
    def _tb_press(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPos() - self.window().frameGeometry().topLeft()
            e.accept()

    def _tb_move(self, e):
        if self._drag_pos is not None and e.buttons() & Qt.LeftButton:
            self.window().move(e.globalPos() - self._drag_pos)
            e.accept()

    # ── control handlers ──────────────────────────────────────────────
    def _on_max_toggle(self, on):
        for s in (self._iv_hr, self._iv_min, self._iv_sec, self._iv_ms):
            s.setEnabled(not on and self._basic_shaped)
        self.config_changed.emit()

    def _on_repeat_mode(self, mode_id):
        self._repeat_count.setEnabled(mode_id == 1 and self._basic_shaped)
        self.config_changed.emit()

    def _on_cursor_mode(self, mode_id):
        pick = (mode_id == 1)
        self._fixed_x.setEnabled(pick and self._basic_shaped)
        self._fixed_y.setEnabled(pick and self._basic_shaped)
        self.config_changed.emit()

    def _on_script_changed(self, name):
        self.script_changed.emit(name)

    def _start_pick(self):
        win = self.window()
        self._pick_overlay = _PickOverlay()
        self._pick_overlay.picked.connect(self._on_picked)
        self._pick_overlay.cancelled.connect(lambda: (
            win.setWindowOpacity(1.0), win.raise_(), win.activateWindow()
        ))
        win.setWindowOpacity(0.0)
        self._pick_overlay.show()
        self._pick_overlay.raise_()
        self._pick_overlay.activateWindow()

    def _on_picked(self, x, y):
        win = self.window()
        win.setWindowOpacity(1.0)
        win.raise_()
        win.activateWindow()
        self._rb_pick.setChecked(True)
        self._on_cursor_mode(1)
        self._fixed_x.setValue(x)
        self._fixed_y.setValue(y)

    def _interval_ms(self) -> int:
        return (self._iv_hr.value() * 3600000 + self._iv_min.value() * 60000 +
                self._iv_sec.value() * 1000 + self._iv_ms.value())

    # ── public API used by MainWindow ─────────────────────────────────
    def autoclick_data(self) -> dict:
        s = self._settings.s
        max_mode = self._max_chk.isChecked()
        interval_ms = 0 if max_mode else self._interval_ms()
        # An all-zero interval with Max unchecked means "as fast as possible" —
        # treat it as Max so it can't fall through to the ~200 CPS sleep floor.
        if not max_mode and interval_ms <= 0:
            max_mode = True
        use_fixed = self._rb_pick.isChecked()
        fx, fy = (self._fixed_x.value(), self._fixed_y.value()) if use_fixed else (0, 0)
        repeat_count = self._repeat_count.value() if self._rb_count.isChecked() else 0
        stop_secs = float(self._stop_secs.value())
        return {
            "button":          "right" if self._btn_grp.checkedId() == 1 else "left",
            "click_type":      "single",
            "hold_duration_ms": s.hold_duration_ms,
            "max_speed":       max_mode,
            "unit":            "ms",
            "cps":             (1000.0 / interval_ms) if interval_ms else 0.0,
            "interval_ms":     interval_ms,
            "use_fixed":       use_fixed,
            "fixed_x":         fx,
            "fixed_y":         fy,
            "randomize":       s.randomize_interval,
            "random_range_ms": s.random_range_ms,
            "human_mode":      self._human.isChecked(),
            "jitter_px":       s.cursor_jitter_px,
            "click_limit":     repeat_count,
            "stop_after_secs": stop_secs,
            "use_region":      False,
            "region":          (s.region_x, s.region_y, s.region_w, s.region_h),
            "pause_on_focus":  False,
            "focus_window":    "",
            "wait_for_image":  False,
            "image_path":      "",
            "image_confidence": s.image_trigger_confidence,
        }

    def _set_interval_fields(self, ms: int):
        ms = max(0, int(ms))
        self._iv_hr.setValue(min(23, ms // 3600000)); ms %= 3600000
        self._iv_min.setValue(ms // 60000); ms %= 60000
        self._iv_sec.setValue(ms // 1000); ms %= 1000
        self._iv_ms.setValue(ms)

    def load_from_settings(self):
        s = self._settings.s
        self._max_chk.setChecked(bool(s.max_speed))
        self._set_interval_fields(s.interval_ms if s.interval_ms else 100)
        self._on_max_toggle(self._max_chk.isChecked())
        self._btn_grp.button(1 if s.button == "right" else 0).setChecked(True)
        if s.limit_mode == "count" and s.limit_count > 0:
            self._rb_count.setChecked(True)
            self._repeat_count.setValue(s.limit_count)
            self._repeat_count.setEnabled(True)
        else:
            self._rb_until.setChecked(True)
            self._repeat_count.setEnabled(False)
        self._stop_secs.setValue(0)
        self._human.setChecked(s.human_mode)
        self._rb_current.setChecked(True)
        self._fixed_x.setEnabled(False); self._fixed_y.setEnabled(False)
        self._btn_pin.setChecked(s.always_on_top)

    def save_to_settings(self):
        s = self._settings.s
        s.max_speed = self._max_chk.isChecked()
        s.interval_ms = self._interval_ms() or s.interval_ms
        s.button = "right" if self._btn_grp.checkedId() == 1 else "left"
        s.human_mode = self._human.isChecked()
        s.always_on_top = self._btn_pin.isChecked()
        if self._rb_count.isChecked():
            s.limit_mode = "count"; s.limit_count = self._repeat_count.value()
        else:
            s.limit_mode = "infinite"

    def _set_knobs_enabled(self, enabled: bool):
        for s in (self._iv_hr, self._iv_min, self._iv_sec, self._iv_ms):
            s.setEnabled(enabled and not self._max_chk.isChecked())
        self._max_chk.setEnabled(enabled)
        for grp in (self._btn_grp, self._repeat_grp, self._cursor_grp):
            for btn in grp.buttons():
                btn.setEnabled(enabled)
        self._repeat_count.setEnabled(enabled and self._rb_count.isChecked())
        pick = enabled and self._rb_pick.isChecked()
        self._fixed_x.setEnabled(pick)
        self._fixed_y.setEnabled(pick)
        for w in (self._human, self._stop_secs):
            w.setEnabled(enabled)

    def set_running(self, running: bool, cps: float = 0.0):
        self._running = running
        self._start.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        for wdg in (self._rec, self._play, self._scripts):
            wdg.setEnabled(not running)
        self._set_knobs_enabled(not running and self._basic_shaped)
        if not running:
            self._start.setText("Start")

    def update_live_cps(self, cps: float):
        # CPS is intentionally not surfaced on the face; Start/Stop state shows
        # whether a run is active.
        pass

    def set_countdown(self, secs: int):
        """Show a pre-start countdown on the Start button."""
        self._start.setText(f"Starting {secs}s…")

    def set_recording(self, recording: bool):
        self._recording = recording
        self._rec.setText("■" if recording else "●")
        self._rec.setToolTip("Stop recording" if recording else
                             "Record a script (F8/Esc to stop)")

    def set_scripts(self, names, current=""):
        self._scripts.blockSignals(True)
        self._scripts.clear()
        self._scripts.addItem("— no script —")
        self._scripts.addItems(list(names))
        if current:
            i = self._scripts.findText(current)
            if i >= 0:
                self._scripts.setCurrentIndex(i)
        self._scripts.blockSignals(False)

    def current_script(self) -> str:
        t = self._scripts.currentText()
        return "" if t.startswith("—") else t

    def set_basic_shaped(self, basic: bool):
        """Graceful degradation: when the script isn't a bare clicker, disable
        the per-knob controls and lead with the script row."""
        self._basic_shaped = basic
        self._set_knobs_enabled(basic)
        self._note.setVisible(not basic)

    def set_hotkey_label(self, disp: str):
        self._chip.setText(disp or "—")
        self._chip.setVisible(bool(disp))
