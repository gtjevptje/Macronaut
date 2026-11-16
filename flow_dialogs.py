"""
Configuration dialogs for the control-flow node types (If, Loop, Goto) plus a
per-node On-error editor, a reusable condition editor widget, and the Auto-Click
editor. Action nodes are edited with the existing StepDialog in main.py.

Note: Set-variable and Label nodes were removed from the product, as was the old
`comment` *node* — the interpreter still tolerates all three in old saved files,
but none can be created, so their dialogs no longer live here. (The Comment the
palette offers today is `flow.N_FRAME`, a box drawn behind the graph; it is
edited on the canvas, not through a dialog.)
"""
from typing import Optional, List

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QPixmap
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QLabel, QComboBox, QSpinBox, QDoubleSpinBox,
                             QLineEdit, QCheckBox, QPushButton, QDialogButtonBox,
                             QGroupBox, QFileDialog, QWidget, QSizePolicy,
                             QButtonGroup)

import flow


# -- small helpers -------------------------------------------------------------
def _spin(lo, hi, val=0, suffix="", w=90):
    s = QSpinBox(); s.setRange(lo, hi); s.setValue(val)
    if suffix:
        s.setSuffix(suffix)
    if w:
        s.setFixedWidth(w)
    return s


def _dspin(lo, hi, val=0.0, step=0.05, suffix="", w=90):
    s = QDoubleSpinBox(); s.setRange(lo, hi); s.setSingleStep(step); s.setValue(val)
    if suffix:
        s.setSuffix(suffix)
    if w:
        s.setFixedWidth(w)
    return s


def _pane(layout=None) -> QWidget:
    """A layout-only container widget that does not paint its own background.

    The app stylesheet sets `background: $bg` on *every* QWidget, so a plain
    container dropped inside a QGroupBox paints a near-black rectangle over the
    box it sits in. main.py has carried the "fieldRow" object name for this since
    the beginning; these dialogs never adopted it, which is what put a big black
    square inside the If editor's Check box. Every container here goes through
    this function so it cannot happen again.
    """
    w = QWidget()
    w.setObjectName("fieldRow")
    if layout is not None:
        w.setLayout(layout)
    return w


def _branch_legend() -> QLabel:
    """The green ✓ true / red ✗ false key, in the exact colours of the ports on
    the canvas. Keeping the two in step is the point: the same glyph and the same
    colour in the editor and on the node means you never have to work out which
    wire is which."""
    lbl = QLabel(
        "<table cellspacing='0' cellpadding='2'>"
        "<tr><td><b style='color:#22c55e'>✓&nbsp;true</b></td>"
        "<td>&nbsp;— taken when the check holds</td></tr>"
        "<tr><td><b style='color:#ef4444'>✗&nbsp;false</b></td>"
        "<td>&nbsp;— taken when it doesn't, or times out</td></tr></table>")
    lbl.setTextFormat(Qt.RichText)
    return lbl


class RegionSelect(QWidget):
    """"Whole screen" vs "a region" as a two-segment pill.

    This used to be a status label ("Whole screen" / "Region set (320x140)")
    sitting beside two ordinary push buttons — so a sentence told you the state
    and the buttons, which look identical whichever one is in force, told you
    nothing. Here the selected half *is* the state: it is the filled one, and
    when it is the region half it carries the size inline. Same segWrap/seg
    styling as every other segmented choice in the app.

    Lives here rather than in main.py because main imports this module, not the
    other way round. The screen overlay is reached through a lazy `import main`
    at click time, which is what the region picking already did.
    """

    changed = Signal()

    _PICK_TIP = ("Drag a box around just the area to use. Faster and more "
                 "reliable than the whole desktop.")
    _WHOLE_TIP = "Search the entire desktop (all monitors)."

    def __init__(self, parent=None):
        super().__init__(parent)
        # The stylesheet's blanket `background: $bg` QWidget rule matches
        # subclasses too — see _pane().
        self.setObjectName("fieldRow")
        self._region = None
        # One row tall, always. Dropped into a QGridLayout cell it would
        # otherwise absorb whatever spare vertical space the grid had and
        # leave the pill floating in the middle of it.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        wrap = QWidget(); wrap.setObjectName("segWrap")
        h = QHBoxLayout(wrap); h.setContentsMargins(3, 3, 3, 3); h.setSpacing(2)
        self._group = QButtonGroup(self)
        self._whole = QPushButton("Whole screen")
        self._pick = QPushButton("Select region…")
        for i, b in enumerate((self._whole, self._pick)):
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self._group.addButton(b, i)
            h.addWidget(b)
        self._whole.setToolTip(self._WHOLE_TIP)
        self._pick.setToolTip(self._PICK_TIP)
        self._whole.setChecked(True)
        self._whole.clicked.connect(self._choose_whole)
        self._pick.clicked.connect(self._choose_region)
        lay.addWidget(wrap)
        lay.addStretch(1)

    # -- state -----------------------------------------------------------
    def region(self):
        return self._region

    def set_region(self, r):
        self._region = tuple(int(v) for v in r) if r else None
        if self._region:
            x, y, w, h = self._region
            self._pick.setText("Region %d×%d" % (w, h))
            self._pick.setToolTip("%d×%d at (%d, %d) — click to re-select" % (w, h, x, y))
            self._pick.setChecked(True)
        else:
            self._pick.setText("Select region…")
            self._pick.setToolTip(self._PICK_TIP)
            self._whole.setChecked(True)
        self.changed.emit()

    # -- interaction -----------------------------------------------------
    def _choose_whole(self):
        self.set_region(None)

    def _choose_region(self):
        # Clicking the half already moved the check, so a cancelled pick has to
        # put it back where it was — otherwise "cancel" silently reads as
        # "region", with no region.
        previous = self._region
        try:
            import main
            main._launch_region_picker(
                lambda x, y, w, h: self.set_region((x, y, w, h)),
                parent_window=self.window(),
                on_cancel=lambda: self.set_region(previous))
        except Exception:
            self.set_region(previous)


# ==============================================================================
#  Condition editor (shared by If + Loop while/until)
# ==============================================================================
class ConditionWidget(QWidget):
    """Edits a condition dict: {type, ...params, region, timeout_s, negate}.

    When show_timeout is True (If/Else use), image/text conditions actively
    wait up to timeout_s seconds for the target before deciding; a timeout
    counts as "not found". Loops pass show_timeout=False so each iteration's
    check stays an instant one-shot.
    """

    TYPES = [("Image on screen", "image"),
             ("Text on screen", "text"),
             ("Pixel colour", "pixel"),
             ("Always", "always")]

    def __init__(self, cond: Optional[dict] = None, parent=None, show_timeout: bool = True):
        super().__init__(parent)
        # Transparent for the same reason as _pane(): the stylesheet's blanket
        # QWidget rule matches subclasses too, so this widget would paint the
        # window background over whatever it is dropped into.
        self.setObjectName("fieldRow")
        self._show_timeout = show_timeout
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.addWidget(QLabel("Condition:"))
        self._type = QComboBox()
        for label, _ in self.TYPES:
            self._type.addItem(label)
        self._type.currentIndexChanged.connect(self._on_type)
        top.addWidget(self._type, 1)
        self._negate = QCheckBox("NOT (invert)")
        self._negate.setToolTip("Flip the result: the check counts as met when the "
                                "thing is *not* there.")
        top.addWidget(self._negate)
        lay.addLayout(top)

        # -- image panel --
        self._p_image = _pane(); gi = QGridLayout(self._p_image)
        gi.setContentsMargins(0, 0, 0, 0)
        gi.addWidget(QLabel("Image:"), 0, 0)
        self._img_path = QLineEdit()
        self._img_path.setPlaceholderText("Path to PNG/JPEG…")
        gi.addWidget(self._img_path, 0, 1)
        b = QPushButton("Browse..."); b.clicked.connect(self._browse_img)
        gi.addWidget(b, 0, 2)
        # Capturing a crop off the screen is how you actually make one of these
        # images; browsing to a file you already have is the rare case. This
        # panel was the one image field in the app that only offered the rare
        # one.
        cap = QPushButton("Capture…")
        cap.setToolTip("Drag a box on screen and use that crop as the image")
        cap.clicked.connect(self._capture_img)
        gi.addWidget(cap, 0, 3)
        gi.addWidget(QLabel("Confidence:"), 1, 0)
        self._img_conf = _dspin(0.1, 1.0, 0.85, 0.01)
        gi.addWidget(self._img_conf, 1, 1)
        test = QPushButton("Test match")
        test.setToolTip("Grab the screen now and report how well this image matches")
        test.clicked.connect(self._test_img)
        gi.addWidget(test, 1, 2)

        self._img_preview = QLabel()
        self._img_preview.setObjectName("hint")
        self._img_preview.setVisible(False)
        gi.addWidget(self._img_preview, 2, 1, 1, 3)
        self._img_test_result = QLabel("")
        self._img_test_result.setObjectName("hint")
        self._img_test_result.setWordWrap(True)
        self._img_test_result.setVisible(False)
        gi.addWidget(self._img_test_result, 3, 1, 1, 3)
        self._img_path.textChanged.connect(self._on_img_path_changed)

        gi.addWidget(QLabel("Search area:"), 4, 0)
        self._img_region_sel = RegionSelect()
        gi.addWidget(self._img_region_sel, 4, 1, 1, 3)
        self._img_timeout = _spin(0, 3600, 5, " s", w=90)
        self._img_timeout.setSpecialValueText("instant (0 s)")
        self._img_timeout.setToolTip("Wait up to this long for the image before the "
                                     "condition counts as not-found.")
        gi.addWidget(QLabel("Wait up to:"), 5, 0)
        gi.addWidget(self._img_timeout, 5, 1)
        lay.addWidget(self._p_image)

        # -- text panel --
        self._p_text = _pane(); gt = QGridLayout(self._p_text)
        gt.setContentsMargins(0, 0, 0, 0)
        gt.addWidget(QLabel("Text:"), 0, 0)
        self._txt = QLineEdit()
        gt.addWidget(self._txt, 0, 1, 1, 2)
        self._txt_case = QCheckBox("Case sensitive")
        self._txt_fuzzy = QCheckBox("Fuzzy (tolerate OCR slips)"); self._txt_fuzzy.setChecked(True)
        gt.addWidget(self._txt_case, 1, 1)
        gt.addWidget(self._txt_fuzzy, 2, 1)
        gt.addWidget(QLabel("Search area:"), 3, 0)
        self._txt_region_sel = RegionSelect()
        gt.addWidget(self._txt_region_sel, 3, 1, 1, 2)
        self._txt_timeout = _spin(0, 3600, 5, " s", w=90)
        self._txt_timeout.setSpecialValueText("instant (0 s)")
        self._txt_timeout.setToolTip("Wait up to this long for the text before the "
                                     "condition counts as not-found.")
        gt.addWidget(QLabel("Wait up to:"), 4, 0)
        gt.addWidget(self._txt_timeout, 4, 1)
        lay.addWidget(self._p_text)

        # -- pixel panel --
        self._p_pixel = _pane(); gp = QGridLayout(self._p_pixel)
        gp.setContentsMargins(0, 0, 0, 0)
        gp.addWidget(QLabel("X:"), 0, 0); self._px = _spin(0, 32000)
        gp.addWidget(self._px, 0, 1)
        gp.addWidget(QLabel("Y:"), 0, 2); self._py = _spin(0, 32000)
        gp.addWidget(self._py, 0, 3)
        gp.addWidget(QLabel("Colour (#hex):"), 1, 0)
        self._pcolor = QLineEdit("#ffffff"); self._pcolor.setFixedWidth(90)
        gp.addWidget(self._pcolor, 1, 1)
        gp.addWidget(QLabel("Tolerance:"), 1, 2)
        self._ptol = _spin(0, 255, 10)
        gp.addWidget(self._ptol, 1, 3)
        pick = QPushButton("Pick pixel (3 s)"); pick.clicked.connect(self._pick_pixel)
        gp.addWidget(pick, 2, 0, 1, 4)
        lay.addWidget(self._p_pixel)

        if not show_timeout:
            self._img_timeout.setValue(0)
            self._txt_timeout.setValue(0)
            self._img_timeout.setVisible(False)
            self._txt_timeout.setVisible(False)

        if cond:
            self.load(cond)
        else:
            self._type.setCurrentIndex(0)
        self._on_type()

    def _on_type(self, *_):
        t = self.TYPES[self._type.currentIndex()][1]
        self._p_image.setVisible(t == "image")
        self._p_text.setVisible(t == "text")
        self._p_pixel.setVisible(t == "pixel")

    def _browse_img(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select image", "",
                                              "Images (*.png *.jpg *.jpeg *.bmp)")
        if path:
            self._img_path.setText(path)

    def _capture_img(self):
        try:
            import main
            main._launch_screenshot_selector(self._img_path,
                                             parent_window=self.window())
        except Exception:
            pass

    def _on_img_path_changed(self, path: str):
        pm = QPixmap(path.strip()) if path.strip() else QPixmap()
        if pm.isNull():
            self._img_preview.setVisible(False)
            return
        # Cap it: a full-screen capture would otherwise stretch the dialog to
        # the width of the monitor it came from.
        if pm.width() > 240 or pm.height() > 120:
            pm = pm.scaled(240, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._img_preview.setPixmap(pm)
        self._img_preview.setVisible(True)

    def _test_img(self):
        self._img_test_result.setVisible(True)
        try:
            import main
            # Same search area the condition will use at runtime — a test that
            # scans the whole desktop answers a different question than the one
            # the flow is going to ask.
            main._run_match_test(self._img_path.text().strip(),
                                 self._img_conf.value(),
                                 self, self._img_test_result, self.window(),
                                 region=self._img_region_sel.region())
        except Exception as exc:
            self._img_test_result.setText("Test failed: %s" % exc)

    def _pick_pixel(self):
        self._cd = 3
        self.sender().setText("Capturing in 3s...")
        self._pbtn = self.sender()
        self._t = QTimer(self); self._t.timeout.connect(self._pick_tick)
        self._t.start(1000)

    def _pick_tick(self):
        self._cd -= 1
        if self._cd > 0:
            self._pbtn.setText("Capturing in %ds..." % self._cd)
            return
        self._t.stop()
        pos = QCursor.pos()
        self._px.setValue(pos.x()); self._py.setValue(pos.y())
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab(all_screens=True).convert("RGB")
            r, g, b = img.getpixel((pos.x(), pos.y()))
            self._pcolor.setText("#%02x%02x%02x" % (r, g, b))
        except Exception:
            pass
        self._pbtn.setText("Pick pixel (3 s)")

    def load(self, cond: dict):
        t = cond.get("type", "image")
        idx = next((i for i, (_, v) in enumerate(self.TYPES) if v == t), 0)
        self._type.setCurrentIndex(idx)
        self._negate.setChecked(bool(cond.get("negate")))
        if t == "image":
            self._img_path.setText(cond.get("image_path", ""))
            self._img_conf.setValue(cond.get("confidence", 0.85))
            self._img_region_sel.set_region(cond.get("region"))
            if self._show_timeout:
                self._img_timeout.setValue(int(cond.get("timeout_s", 5)))
        elif t == "text":
            self._txt.setText(cond.get("text", ""))
            self._txt_case.setChecked(cond.get("case_sensitive", False))
            self._txt_fuzzy.setChecked(cond.get("fuzzy", True))
            self._txt_region_sel.set_region(cond.get("region"))
            if self._show_timeout:
                self._txt_timeout.setValue(int(cond.get("timeout_s", 5)))
        elif t == "pixel":
            self._px.setValue(cond.get("x", 0)); self._py.setValue(cond.get("y", 0))
            self._pcolor.setText(cond.get("color", "#ffffff"))
            self._ptol.setValue(cond.get("tolerance", 10))

    def condition(self) -> dict:
        t = self.TYPES[self._type.currentIndex()][1]
        c = {"type": t, "negate": self._negate.isChecked()}
        if t == "image":
            c["image_path"] = self._img_path.text().strip()
            c["confidence"] = self._img_conf.value()
            r = self._img_region_sel.region()
            c["region"] = list(r) if r else None
            c["timeout_s"] = self._img_timeout.value() if self._show_timeout else 0
        elif t == "text":
            c["text"] = self._txt.text().strip()
            c["case_sensitive"] = self._txt_case.isChecked()
            c["fuzzy"] = self._txt_fuzzy.isChecked()
            c["min_score"] = 0.5
            r = self._txt_region_sel.region()
            c["region"] = list(r) if r else None
            c["timeout_s"] = self._txt_timeout.value() if self._show_timeout else 0
        elif t == "pixel":
            c["x"] = self._px.value(); c["y"] = self._py.value()
            c["color"] = self._pcolor.text().strip()
            c["tolerance"] = self._ptol.value()
        return c


# ==============================================================================
#  Node dialogs
# ==============================================================================
class IfDialog(QDialog):
    """This editor is about one thing: the check. The two branches are spelled
    out in the same green/red as the ports on the canvas, so the dialog and the
    node read as one thing.

    There is deliberately no "Node name" field: every node is named the same way,
    from right-click → Name…, and a second place to do it here was one more thing
    to scan past on the way to the only setting that matters."""

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("If / Else")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        box = QGroupBox("Check")
        bl = QVBoxLayout(box)
        self._cond = ConditionWidget(data.get("condition"), show_timeout=True)
        bl.addWidget(self._cond)
        lay.addWidget(box)

        lay.addWidget(_branch_legend())

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def data(self) -> dict:
        # No "name" key: leaving it out means node.data.update(dlg.data()) keeps
        # whatever the node was already called.
        return {"condition": self._cond.condition()}


class LoopDialog(QDialog):
    MODES = [("Repeat N times", "repeat_n"),
             ("While condition true", "while"),
             ("Until condition true", "until"),
             ("Forever", "forever")]

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Loop")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)

        # No "Node name" row here either — see IfDialog. Naming lives in one
        # place, right-click → Name…, for every node type alike.
        row = QHBoxLayout()
        row.addWidget(QLabel("Mode:"))
        self._mode = QComboBox()
        for label, _ in self.MODES:
            self._mode.addItem(label)
        self._mode.currentIndexChanged.connect(self._on_mode)
        row.addWidget(self._mode, 1)
        lay.addLayout(row)

        crow = QHBoxLayout()
        crow.addWidget(QLabel("Repeat count:"))
        self._count = _spin(1, 1000000, data.get("count", 5))
        crow.addWidget(self._count); crow.addStretch()
        self._count_row = _pane(crow)
        lay.addWidget(self._count_row)

        # Loop conditions are evaluated once per iteration -> instant (no waiting).
        self._cond = ConditionWidget(data.get("condition"), show_timeout=False)
        lay.addWidget(self._cond)

        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Safety cap (max iterations):"))
        self._maxit = _spin(1, 100000000, data.get("max_iters", 100000), w=120)
        mrow.addWidget(self._maxit); mrow.addStretch()
        lay.addLayout(mrow)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        mode = data.get("mode", "repeat_n")
        self._mode.setCurrentIndex(
            next((i for i, (_, v) in enumerate(self.MODES) if v == mode), 0))
        self._on_mode()

    def _on_mode(self, *_):
        mode = self.MODES[self._mode.currentIndex()][1]
        self._count_row.setVisible(mode == "repeat_n")
        self._cond.setVisible(mode in ("while", "until"))

    def data(self) -> dict:
        mode = self.MODES[self._mode.currentIndex()][1]
        d = {"mode": mode, "max_iters": self._maxit.value()}
        if mode == "repeat_n":
            d["count"] = self._count.value()
        if mode in ("while", "until"):
            d["condition"] = self._cond.condition()
        return d


class GotoDialog(QDialog):
    """Jump to any named node. No visual wire -- the node shows where it leads."""
    def __init__(self, data: dict, node_names: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Go to node")
        self.setMinimumWidth(360)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Jump to a node by its name. Give the target node a "
                             "name first (in its editor)."))
        row = QHBoxLayout()
        row.addWidget(QLabel("Go to node:"))
        self._combo = QComboBox(); self._combo.setEditable(True)
        self._combo.addItems([n for n in node_names if n])
        if data.get("target_name"):
            self._combo.setCurrentText(data["target_name"])
        elif data.get("target_label"):
            self._combo.setCurrentText(data["target_label"])
        row.addWidget(self._combo, 1)
        lay.addLayout(row)
        if not any(node_names):
            warn = QLabel("No named nodes yet -- name a node first, then pick it here.")
            warn.setStyleSheet("color:#f59e0b;"); warn.setWordWrap(True)
            lay.addWidget(warn)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def data(self) -> dict:
        return {"target_name": self._combo.currentText().strip()}


class OnErrorDialog(QDialog):
    """Per-action recovery: retries, then a fallback action."""
    FALLBACK = [("Stop the whole run", "stop"),
                ("Skip & continue", "skip"),
                ("Jump to a named node", "goto")]

    def __init__(self, on_error: dict, node_names: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Error handling for this step")
        self.setMinimumWidth(380)
        on_error = on_error or {}
        lay = QVBoxLayout(self)

        gb = QGroupBox("Retry")
        gg = QGridLayout(gb)
        gg.addWidget(QLabel("Retry attempts:"), 0, 0)
        self._retries = _spin(0, 100, on_error.get("retries", 0))
        gg.addWidget(self._retries, 0, 1)
        gg.addWidget(QLabel("Delay between (s):"), 1, 0)
        self._delay = _dspin(0.0, 60.0, on_error.get("retry_delay_s", 0.5), 0.1, " s")
        gg.addWidget(self._delay, 1, 1)
        lay.addWidget(gb)

        row = QHBoxLayout()
        row.addWidget(QLabel("If it still fails:"))
        self._mode = QComboBox()
        for label, _ in self.FALLBACK:
            self._mode.addItem(label)
        mode = on_error.get("mode", "stop")
        self._mode.setCurrentIndex(next((i for i, (_, v) in enumerate(self.FALLBACK)
                                         if v == mode), 0))
        self._mode.currentIndexChanged.connect(self._on_mode)
        row.addWidget(self._mode, 1)
        lay.addLayout(row)

        lrow = QHBoxLayout()
        lrow.addWidget(QLabel("Recovery node name:"))
        self._target = QComboBox(); self._target.setEditable(True)
        self._target.addItems([n for n in node_names if n])
        if on_error.get("goto_name"):
            self._target.setCurrentText(on_error["goto_name"])
        elif on_error.get("goto_label"):
            self._target.setCurrentText(on_error["goto_label"])
        lrow.addWidget(self._target, 1)
        self._target_row = _pane(lrow)
        lay.addWidget(self._target_row)

        note = QLabel("Tip: connecting this step's red <b>error</b> port to another "
                      "node overrides this and routes failures there visually.")
        note.setWordWrap(True); note.setStyleSheet("color:#9399b2;")
        lay.addWidget(note)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._on_mode()

    def _on_mode(self, *_):
        self._target_row.setVisible(self.FALLBACK[self._mode.currentIndex()][1] == "goto")

    def data(self) -> dict:
        d = {"mode": self.FALLBACK[self._mode.currentIndex()][1],
             "retries": self._retries.value(),
             "retry_delay_s": self._delay.value()}
        if d["mode"] == "goto":
            d["goto_name"] = self._target.currentText().strip()
        return d


class BulkEditDialog(QDialog):
    """Change one setting across a whole flow instead of node by node.

    Every row is opt-in: nothing is written unless its checkbox is ticked, so
    "apply to all nodes" can never quietly reset the four settings you spent an
    hour tuning. Rows that don't apply to a node are skipped there — setting a
    match confidence doesn't give your Wait nodes one.
    """

    FALLBACK = [("Stop the whole run", "stop"), ("Skip & continue", "skip")]

    def __init__(self, n_total: int, n_selected: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Overall settings")
        self.setMinimumWidth(500)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        lay.addWidget(QLabel("Tick a row to change it everywhere. Untouched rows "
                             "are left exactly as they are."))

        srow = QHBoxLayout()
        srow.addWidget(QLabel("Apply to:"))
        self._scope = QComboBox()
        self._scope.addItem(f"All nodes  ({n_total})")
        self._scope.addItem(f"Selected nodes  ({n_selected})")
        if n_selected:
            self._scope.setCurrentIndex(1)   # a selection is a stated intent
        else:
            self._scope.model().item(1).setEnabled(False)
        srow.addWidget(self._scope, 1)
        lay.addLayout(srow)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        r = 0

        # -- delay before --
        self._c_delay = QCheckBox("Delay before")
        self._delay_mode = QComboBox()
        self._delay_mode.addItems(["set to", "add"])
        self._delay_ms = _spin(-600000, 600000, 0, " ms", w=110)
        grid.addWidget(self._c_delay, r, 0)
        grid.addWidget(self._delay_mode, r, 1)
        grid.addWidget(self._delay_ms, r, 2)
        grid.addWidget(self._hint("0 clears it · skipped on Start and clicks, "
                                  "where it never fires"), r + 1, 1, 1, 3)
        r += 2

        # -- wait duration --
        self._c_wait = QCheckBox("Wait duration")
        self._wait_mode = QComboBox()
        self._wait_mode.addItems(["set to", "scale by"])
        self._wait_ms = _spin(0, 3600000, 500, " ms", w=110)
        self._wait_scale = _dspin(0.1, 20.0, 1.0, 0.1, " ×", w=110)
        grid.addWidget(self._c_wait, r, 0)
        grid.addWidget(self._wait_mode, r, 1)
        grid.addWidget(self._wait_ms, r, 2)
        grid.addWidget(self._wait_scale, r, 3)
        grid.addWidget(self._hint("Wait steps only. The Speed control below the "
                                  "canvas scales these at run time instead."),
                       r + 1, 1, 1, 3)
        r += 2

        # -- detection --
        self._c_timeout = QCheckBox("Give up looking after")
        self._timeout_s = _spin(0, 3600, 5, " s", w=110)
        grid.addWidget(self._c_timeout, r, 0)
        grid.addWidget(self._timeout_s, r, 2)
        grid.addWidget(self._hint("Every Detect step and every image/text check "
                                  "inside an If or Loop."), r + 1, 1, 1, 3)
        r += 2

        self._c_conf = QCheckBox("Image match confidence")
        self._conf = _dspin(0.10, 1.00, 0.85, 0.01, "", w=110)
        grid.addWidget(self._c_conf, r, 0)
        grid.addWidget(self._conf, r, 2)
        grid.addWidget(self._hint("Lower it when a script stops matching on a "
                                  "different screen or resolution."), r + 1, 1, 1, 3)
        r += 2

        # -- error handling --
        self._c_err = QCheckBox("Error handling")
        self._retries = _spin(0, 100, 0, " ×", w=110)
        self._retry_delay = _dspin(0.0, 60.0, 0.5, 0.1, " s", w=110)
        self._err_mode = QComboBox()
        for label, _ in self.FALLBACK:
            self._err_mode.addItem(label)
        grid.addWidget(self._c_err, r, 0)
        grid.addWidget(self._retries, r, 1)
        grid.addWidget(self._retry_delay, r, 2)
        grid.addWidget(self._err_mode, r, 3)
        grid.addWidget(self._hint("Retries, the pause between them, and what to do "
                                  "if it still fails. Action nodes only."),
                       r + 1, 1, 1, 3)
        r += 2
        lay.addLayout(grid)

        lay.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Apply")
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._rows = [
            (self._c_delay, [self._delay_mode, self._delay_ms]),
            (self._c_wait,  [self._wait_mode, self._wait_ms, self._wait_scale]),
            (self._c_timeout, [self._timeout_s]),
            (self._c_conf,  [self._conf]),
            (self._c_err,   [self._retries, self._retry_delay, self._err_mode]),
        ]
        for cb, widgets in self._rows:
            cb.toggled.connect(self._sync)
        self._wait_mode.currentIndexChanged.connect(self._sync)
        self._sync()

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("hint")
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#9399b2;")
        return lbl

    def _sync(self, *_):
        for cb, widgets in self._rows:
            for w in widgets:
                w.setEnabled(cb.isChecked())
        scaling = self._wait_mode.currentIndex() == 1
        self._wait_ms.setVisible(not scaling)
        self._wait_scale.setVisible(scaling)

    def selection_only(self) -> bool:
        return self._scope.currentIndex() == 1

    def ops(self) -> dict:
        """Only the ticked rows — see the class docstring."""
        o = {}
        if self._c_delay.isChecked():
            key = "delay_before_ms" if self._delay_mode.currentIndex() == 0 else "delay_add_ms"
            o[key] = self._delay_ms.value()
        if self._c_wait.isChecked():
            if self._wait_mode.currentIndex() == 0:
                o["wait_ms"] = self._wait_ms.value()
            else:
                o["wait_scale"] = self._wait_scale.value()
        if self._c_timeout.isChecked():
            o["timeout_s"] = self._timeout_s.value()
        if self._c_conf.isChecked():
            o["confidence"] = self._conf.value()
        if self._c_err.isChecked():
            o["error_retries"] = self._retries.value()
            o["error_retry_delay_s"] = self._retry_delay.value()
            o["error_mode"] = self.FALLBACK[self._err_mode.currentIndex()][1]
        return o


# ══════════════════════════════════════════════════════════════════════════════
#  Auto-Click
# ══════════════════════════════════════════════════════════════════════════════
class AutoClickDialog(QDialog):
    """Editor for the Auto-Click node.

    This node used to be edited on the Basic face, which is gone, and
    double-clicking it answered with a message box pointing at a face that no
    longer exists. It is a real node in real saved flows — one long-lived step
    that clicks in a loop until something stops it — so it gets a real editor
    rather than being left as the one node in the app you cannot open.

    ⚠ Only the fields a person would come here to change are shown. The node's
    data also carries randomise, human jitter and a region clamp, which the
    engine still honours; the incoming dict is copied and those keys are written
    back untouched, so opening this dialog on an old node and pressing OK cannot
    quietly drop settings it does not display.
    """
    STOP_NEVER, STOP_CLICKS, STOP_SECS = 0, 1, 2

    def __init__(self, data: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Auto-Click")
        self._data = dict(data or {})
        d = self._data

        lay = QVBoxLayout(self); lay.setSpacing(10)

        # -- what it clicks --
        g1 = QGroupBox("Click"); f1 = QGridLayout(g1); f1.setSpacing(8)
        self._button = QComboBox(); self._button.addItems(["Left", "Right", "Middle"])
        self._button.setCurrentIndex(
            {"left": 0, "right": 1, "middle": 2}.get(d.get("button", "left"), 0))
        f1.addWidget(QLabel("Button"), 0, 0)
        f1.addWidget(self._button, 0, 1)

        self._type = QComboBox()
        self._type.addItems(["Single", "Double", "Hold"])
        self._type.setCurrentIndex(
            {"single": 0, "double": 1, "hold": 2}.get(d.get("click_type", "single"), 0))
        self._type.currentIndexChanged.connect(self._sync)
        f1.addWidget(QLabel("Type"), 1, 0)
        f1.addWidget(self._type, 1, 1)

        self._hold = _spin(1, 60000,
                           int(d.get("hold_duration_ms", d.get("hold_ms", 100)) or 100),
                           " ms", w=110)
        self._hold_lbl = QLabel("Hold for")
        f1.addWidget(self._hold_lbl, 2, 0)
        f1.addWidget(self._hold, 2, 1)
        lay.addWidget(g1)

        # -- how fast --
        g2 = QGroupBox("Rate"); f2 = QGridLayout(g2); f2.setSpacing(8)
        self._max = QCheckBox("As fast as possible")
        self._max.setChecked(bool(d.get("max_speed")))
        self._max.toggled.connect(self._sync)
        f2.addWidget(self._max, 0, 0, 1, 2)
        self._interval = _spin(1, 600000, int(d.get("interval_ms", 1000) or 1000),
                               " ms", w=110)
        self._interval_lbl = QLabel("Every")
        f2.addWidget(self._interval_lbl, 1, 0)
        f2.addWidget(self._interval, 1, 1)
        lay.addWidget(g2)

        # -- where --
        g3 = QGroupBox("Where"); f3 = QGridLayout(g3); f3.setSpacing(8)
        self._where = QComboBox()
        self._where.addItems(["Wherever the cursor is", "At a fixed point"])
        self._where.setCurrentIndex(
            1 if d.get("use_fixed", d.get("position") == "fixed") else 0)
        self._where.currentIndexChanged.connect(self._sync)
        f3.addWidget(self._where, 0, 0, 1, 3)
        self._x = _spin(-32000, 32000, int(d.get("fixed_x", 0) or 0), w=80)
        self._y = _spin(-32000, 32000, int(d.get("fixed_y", 0) or 0), w=80)
        self._pos_lbl = QLabel("X / Y")
        f3.addWidget(self._pos_lbl, 1, 0)
        f3.addWidget(self._x, 1, 1)
        f3.addWidget(self._y, 1, 2)
        lay.addWidget(g3)

        # -- when it stops --
        g4 = QGroupBox("Stop"); f4 = QGridLayout(g4); f4.setSpacing(8)
        self._stop = QComboBox()
        self._stop.addItems(["Only when I stop it", "After a number of clicks",
                             "After a length of time"])
        limit = int(d.get("click_limit", 0) or 0)
        secs = float(d.get("stop_after_secs", 0) or 0)
        self._stop.setCurrentIndex(self.STOP_CLICKS if limit else
                                   self.STOP_SECS if secs else self.STOP_NEVER)
        self._stop.currentIndexChanged.connect(self._sync)
        f4.addWidget(self._stop, 0, 0, 1, 2)
        self._limit = _spin(1, 10_000_000, limit or 100, " clicks", w=130)
        self._secs = _dspin(0.5, 86400.0, secs or 30.0, 1.0, " s", w=130)
        f4.addWidget(self._limit, 1, 0)
        f4.addWidget(self._secs, 1, 1)
        lay.addWidget(g4)

        # -- gates --
        g5 = QGroupBox("Only click while…"); f5 = QVBoxLayout(g5); f5.setSpacing(6)
        self._focus = QCheckBox("a window with this in its title is in front")
        self._focus.setChecked(bool(d.get("pause_on_focus")))
        self._focus.toggled.connect(self._sync)
        self._focus_txt = QLineEdit(d.get("focus_window", ""))
        self._focus_txt.setPlaceholderText("part of the window title")
        f5.addWidget(self._focus)
        f5.addWidget(self._focus_txt)
        lay.addWidget(g5)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._sync()

    def _sync(self, *_):
        self._hold.setVisible(self._type.currentIndex() == 2)
        self._hold_lbl.setVisible(self._type.currentIndex() == 2)
        on = not self._max.isChecked()
        self._interval.setVisible(on); self._interval_lbl.setVisible(on)
        fixed = self._where.currentIndex() == 1
        for w in (self._x, self._y, self._pos_lbl):
            w.setVisible(fixed)
        mode = self._stop.currentIndex()
        self._limit.setVisible(mode == self.STOP_CLICKS)
        self._secs.setVisible(mode == self.STOP_SECS)
        self._focus_txt.setVisible(self._focus.isChecked())
        # adjustSize does not re-fit after rows are shown or hidden — the
        # layout keeps the size hint it cached (the trap StepDialog._refit
        # exists for).
        self.layout().invalidate()
        self.layout().activate()
        QTimer.singleShot(0, lambda: self.resize(self.sizeHint()))

    def result_data(self) -> dict:
        """The node's data dict, with the untouched keys carried through."""
        d = dict(self._data)
        d["button"] = ["left", "right", "middle"][self._button.currentIndex()]
        d["click_type"] = ["single", "double", "hold"][self._type.currentIndex()]
        d["hold_duration_ms"] = self._hold.value()
        d["max_speed"] = self._max.isChecked()
        d["interval_ms"] = 0 if self._max.isChecked() else self._interval.value()
        d["cps"] = (1000.0 / d["interval_ms"]) if d["interval_ms"] else 0.0
        d["use_fixed"] = self._where.currentIndex() == 1
        d["fixed_x"], d["fixed_y"] = self._x.value(), self._y.value()
        mode = self._stop.currentIndex()
        d["click_limit"] = self._limit.value() if mode == self.STOP_CLICKS else 0
        d["stop_after_secs"] = self._secs.value() if mode == self.STOP_SECS else 0.0
        d["pause_on_focus"] = self._focus.isChecked()
        d["focus_window"] = self._focus_txt.text().strip()
        return d
