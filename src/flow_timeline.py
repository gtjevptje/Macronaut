"""The strip under the canvas: every node on one line, plus what is still held.

Two axes, one widget
--------------------
**Order** — one box per node in the order a reader walks the graph. Always
available, always exact, and it needs no durations at all. This is the axis
that makes Hold down safe to use: a key pressed in one node and released four
nodes later is a bar spanning those four boxes, and a key that is never
released is a bar running off the end with a warning on it. You can see that
before you ever press Play.

**Time** — the same boxes, widths proportional to how long each node takes.
This axis cannot be honest on its own: a Detect waits for a picture and a
while-loop runs until a condition flips, so their durations are unknowable
until they have happened. So the widths come from three different places and
are drawn three different ways (see flow.estimate):

    exact      the settings decide it — a Wait of 1.5 s. Solid.
    measured   this machine has timed it. Solid, with a tint and a sample count.
    ceiling    an upper bound — a Detect's timeout. Outlined, not filled.
    unknown    never run, nothing to say. Hatched, fixed narrow width.

Which is why the second run's picture is worth more than the first, and why
that is a feature rather than an apology: you write a flow, run it once, and
the strip becomes a schedule.

Nothing here streams from the worker per step. The running node's bar is
animated locally off one "node started" signal, the same way the canvas does
it, because the interpreter can enter nodes far faster than any GUI can draw.

It scrolls, and it folds away
----------------------------
The widths used to be normalised to the widget so the strip could never run off
its own right edge — the objection being that overflow silently hides the end of
the flow, which is where End is and where an unreleased key would show. The
answer to that objection is an affordance, not a squeeze: MIN_SEG_W is a hard
floor now and the lane pans, with a scrollbar, a fade at whichever end continues,
and the running node kept in view on its own. A 60-node flow gets boxes you can
read and click instead of sixty slivers, and nothing about it is silent.

The chevron at the head of the row folds the whole strip down to that one row —
the same bargain the run log offers, for the same reason: sometimes you want the
canvas back. ⚠ That choice is deliberately NOT remembered across launches: it
used to be (settings.timeline_open), and one look at a run's timing then left
the strip unfolded in every session afterwards. The strip starts folded, always.
It is the one run-state view showing nothing the canvas does not already show,
so it has to be asked for by the run that wants it.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer, Signal
from PySide6.QtGui import (QColor, QPainter, QPen, QBrush, QFont, QFontMetrics,
                           QPainterPath, QLinearGradient)
from PySide6.QtWidgets import QWidget, QSizePolicy

import flow
import flow_canvas as fc

# ── metrics ───────────────────────────────────────────────────────────────────
PAD_X, PAD_TOP = 10, 6
HEAD_H      = 18        # the header row: chevron, note, axis toggle
LANE_H      = 24        # the node lane
BAR_H       = 5         # the pan scrollbar under the node lane
BAR_GAP     = 4
KEY_LANE_H  = 14        # one per held key
KEY_GAP     = 3
LABEL_W     = 58        # left gutter holding the key names
MIN_SEG_W   = 26.0      # a node never narrower than something clickable
SEG_GAP     = 2.0
MAX_KEY_LANES = 3       # past this it scrolls the list rather than the widget
CHEV_W      = 14        # the collapse chevron at the head of the row
FADE_W      = 22.0      # edge fade that says "the flow continues this way"
WHEEL_PX    = 64.0      # pixels panned per wheel notch (~2.5 boxes)
BAR_GRAB    = 4.0       # slop around the scrollbar, which is 5 px tall

BG          = QColor("#141230")
TROUGH      = QColor("#1b1940")
LINE        = QColor("#2a2756")
TXT         = QColor("#cdd6f4")
MUTED       = QColor("#6f6aa8")
ACCENT      = QColor("#7F77DD")
HELD        = fc.HELD_COL
WARN        = QColor("#ef4444")
CUR         = QColor("#89b4fa")
ON_ACCENT   = QColor("#ffffff")     # text on the selected axis segment
NOTE_PT     = 8                     # header / fold-row text


def apply_palette(pal: dict, light: bool = False) -> None:
    """Re-tint the strip from an app palette, IN PLACE (see flow_canvas).

    The strip continues the canvas inside one framed well, so its fill is the
    canvas colour and its top edge a hairline of the theme border.
    """
    set_ = fc._set
    canvas = pal.get("canvas", pal["inputBg"])
    set_(BG, canvas)
    set_(TROUGH, pal["panel2"])
    set_(LINE, pal["border"])
    set_(TXT, pal["text"])
    set_(MUTED, pal["muted"])
    set_(ACCENT, pal["accent"])
    set_(CUR, pal["accentHover"] if not light else pal["accentPress"])
    set_(ON_ACCENT, pal["accentText"])


def _seg_color(node) -> QColor:
    return fc.node_header_color(node)


class TimelineStrip(QWidget):
    """Node lane + held-key lanes. Emits node_clicked when a box is picked."""

    node_clicked = Signal(str)
    mode_changed = Signal(str)
    collapsed_changed = Signal(bool)

    ORDER, TIME = "order", "time"

    def __init__(self, graph: "flow.FlowGraph", parent=None):
        super().__init__(parent)
        self.graph = graph
        self._mode = self.ORDER
        self._order: List[str] = []
        self._shape = None                      # the graph `_order` was derived from
        self._measured: Dict[str, int] = {}
        self._speed = 1.0
        self._held: List[Tuple[str, str]] = []      # (key name, node id)
        self._held_since: Dict[str, float] = {}
        self._cur: Optional[str] = None
        self._cur_t0 = 0.0
        self._cur_ms = 0.0
        self._done: set = set()
        self._running = False
        self._hover: Optional[str] = None
        self._hot: Optional[str] = None         # chrome under the cursor: chev|thumb
        self._boxes: List[Tuple[str, QRectF]] = []
        self._toggle_rect = QRectF()
        self._chev_rect = QRectF()
        self._track_rect = QRectF()
        self._thumb_rect = QRectF()
        self._collapsed = False
        self._scroll = 0.0                      # px of lane hidden off the left
        self._content_w = 0.0                   # width every box needs, laid end to end
        self._grab: Optional[float] = None      # cursor -> thumb.left() while dragging

        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = QTimer(self)
        self._anim.setInterval(50)
        self._anim.timeout.connect(self.update)
        self.rebuild()

    # ── model ─────────────────────────────────────────────────────────
    def set_graph(self, graph):
        self.graph = graph
        self.rebuild()

    def rebuild(self):
        # flow.ANNOTATION_TYPES are drawing, not flow. A reroute sits in the
        # middle of a wire and is walked like any other node, but it is not a
        # step anyone is waiting for — a lane that says "reroute" four times is
        # describing the picture instead of the run.
        self._order = [nid for nid in flow.linearise(self.graph)
                       if self.graph.nodes.get(nid) is not None
                       and self.graph.nodes[nid].type not in flow.ANNOTATION_TYPES]
        self._shape = self._graph_shape()
        self._fit_height()
        self.update()

    def _graph_shape(self):
        """Everything the node order depends on, cheap enough to ask per paint."""
        g = self.graph
        return (tuple((nid, n.type) for nid, n in g.nodes.items()),
                tuple((e.src, e.src_port, e.dst) for e in g.edges))

    def _sync(self):
        """Re-derive the order if the graph was edited since it was worked out.

        ⚠ Nothing tells the strip about an edit. The canvas mutates the very
        FlowGraph object this widget holds, so `rebuild()` ran when a script was
        loaded and never again — and a node deleted with the strip unfolded
        stayed in `_order`, where the next paint looked it up in a graph that no
        longer had it (KeyError out of `_estimates`, reported from the field).
        A node added on the canvas was missing from the strip for the same
        reason, just silently.

        Asked here rather than signalled from the canvas because the failure is
        a missed notification, and a second notification path is one more thing
        to miss. Comparing the shape is two tuples per paint; walking the graph
        again only happens when it really changed.
        """
        if self._graph_shape() != self._shape:
            self._order = [nid for nid in flow.linearise(self.graph)
                           if self.graph.nodes.get(nid) is not None
                           and self.graph.nodes[nid].type
                           not in flow.ANNOTATION_TYPES]
            self._shape = self._graph_shape()
            self._boxes = []

    def set_measured(self, measured: Dict[str, int]):
        self._measured = dict(measured or {})
        self.update()

    def set_speed(self, speed: float):
        self._speed = max(0.01, float(speed or 1.0))
        self.update()

    def set_mode(self, mode: str):
        mode = self.TIME if mode == self.TIME else self.ORDER
        if mode != self._mode:
            self._mode = mode
            self.mode_changed.emit(mode)
            self.update()

    def mode(self) -> str:
        return self._mode

    # ── collapse ──────────────────────────────────────────────────────
    def set_collapsed(self, collapsed: bool):
        """Fold to the header row, or unfold. Emits collapsed_changed."""
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._boxes = []
        self._hot = self._hover = None
        self._grab = None
        self._fit_height()
        self.collapsed_changed.emit(collapsed)
        self.update()

    def is_collapsed(self) -> bool:
        return self._collapsed

    # ── run state ─────────────────────────────────────────────────────
    def run_started(self):
        self._running = True
        self._done = set()
        self._cur = None
        self._held = []
        self._held_since = {}
        self._anim.start()
        self.update()

    def run_finished(self):
        self._running = False
        self._cur = None
        self._held = []
        self._held_since = {}
        self._anim.stop()
        self._fit_height()
        self.update()

    def node_started(self, nid: str):
        if self._cur:
            self._done.add(self._cur)
        self._cur = nid
        self._cur_t0 = time.perf_counter()
        node = self.graph.nodes.get(nid)
        self._cur_ms = float(flow.estimate(node, self._speed,
                                           self._measured).ms) if node else 0.0
        self._ensure_visible(nid)
        self.update()

    def set_held(self, pairs):
        """[(key name, node id), ...] — straight from FlowWorker.held_changed."""
        pairs = [(str(k), str(n)) for k, n in (pairs or [])]
        now = time.perf_counter()
        names = {k for k, _ in pairs}
        for k in names:
            self._held_since.setdefault(k, now)
        for k in list(self._held_since):
            if k not in names:
                self._held_since.pop(k, None)
        self._held = pairs
        self._fit_height()
        self.update()

    # ── geometry ──────────────────────────────────────────────────────
    def _fit_height(self):
        if self._collapsed:
            self.setFixedHeight(int(PAD_TOP + HEAD_H + 4))
            return
        lanes = min(len(self._held), MAX_KEY_LANES)
        # The scrollbar lane is reserved whether or not there is anything to
        # scroll: it is 9 px, and a strip that changed height every time the
        # axis switched would shove the canvas up and down under the cursor.
        h = PAD_TOP + HEAD_H + LANE_H + BAR_GAP + BAR_H + 4
        if lanes:
            h += lanes * (KEY_LANE_H + KEY_GAP)
        self.setFixedHeight(int(h))

    def _avail(self) -> float:
        """Width of the visible lane — what a box has to fit inside to be seen."""
        return max(50.0, self.width() - 2 * PAD_X - LABEL_W)

    def _viewport(self) -> QRectF:
        return QRectF(PAD_X + LABEL_W, float(PAD_TOP + HEAD_H),
                      self._avail(), float(LANE_H))

    def max_scroll(self) -> float:
        """How far the lane can pan. 0 when the whole flow already fits."""
        return max(0.0, self._content_w - self._avail())

    def _set_scroll(self, x: float) -> bool:
        x = max(0.0, min(float(x), self.max_scroll()))
        if abs(x - self._scroll) < 0.01:
            return False
        self._scroll = x
        self._boxes = []
        self.update()
        return True

    def _ensure_visible(self, nid: str):
        """Pan just enough to bring a node into the lane, with a little margin.

        Called when a node starts, so a long flow follows itself instead of
        running along behind the right edge.
        """
        if self._collapsed:
            return
        # _hit_boxes() first: it is _layout() that measures _content_w, and
        # max_scroll() reads that.
        boxes = dict(self._hit_boxes())
        if self.max_scroll() <= 0.5:
            return
        r = boxes.get(nid)
        if r is None:
            return
        vp, pad = self._viewport(), 28.0
        if r.left() < vp.left() + pad:
            self._set_scroll(self._scroll - (vp.left() + pad - r.left()))
        elif r.right() > vp.right() - pad:
            self._set_scroll(self._scroll + (r.right() - vp.right() + pad))

    def _estimates(self) -> Dict[str, "flow.Estimate"]:
        self._sync()
        return {nid: flow.estimate(self.graph.nodes[nid], self._speed,
                                   self._measured) for nid in self._order}

    def _widths(self, avail: float,
                est: Optional[Dict[str, "flow.Estimate"]] = None
                ) -> Dict[str, float]:
        """Box width per node, for whichever axis is showing.

        MIN_SEG_W is a **hard** floor, and the sum is free to exceed `avail` —
        the lane pans. It used to be the other way round, normalised so the
        strip could never run off its own right edge, because overflow silently
        hid the end of the flow (where End is, and where an unreleased key would
        show). Nothing about that overflow is silent now: there is a scrollbar,
        a fade on whichever side continues, and the running node pans itself into
        view. So a 60-node flow gets boxes you can read and click, instead of
        sixty two-pixel slivers that showed you everything and told you nothing.
        """
        ids = self._order
        if not ids:
            return {}
        space = max(1.0, avail - SEG_GAP * max(0, len(ids) - 1))
        floor = MIN_SEG_W

        if self._mode == self.ORDER:
            return {nid: max(floor, space / len(ids)) for nid in ids}

        est = est if est is not None else self._estimates()
        # An unknown contributes no weight and takes the floor: nothing can be
        # inferred about its length, so it must not be able to squeeze what *is*
        # known down to a sliver.
        weights = {nid: (float(e.ms) if e.source != flow.UNKNOWN else 0.0)
                   for nid, e in est.items()}
        total = sum(weights.values())
        if total <= 0:
            return {nid: max(floor, space / len(ids)) for nid in ids}
        pool = max(1.0, space - floor * sum(1 for w in weights.values() if w <= 0))
        return {nid: (floor if wt <= 0 else max(floor, pool * wt / total))
                for nid, wt in weights.items()}

    def _layout(self, est: Optional[Dict[str, "flow.Estimate"]] = None
                ) -> List[Tuple[str, QRectF]]:
        self._sync()
        widths = self._widths(self._avail(), est)
        ids = self._order
        self._content_w = ((sum(widths.values()) + SEG_GAP * max(0, len(ids) - 1))
                           if ids else 0.0)
        # Clamp here rather than on resize: the content can shrink under a
        # scrolled lane (a node deleted, the axis switched) and leave the view
        # parked past the end.
        self._scroll = max(0.0, min(self._scroll, self.max_scroll()))
        y = float(PAD_TOP + HEAD_H)
        x = PAD_X + LABEL_W - self._scroll
        boxes = []
        for nid in ids:
            w = widths.get(nid, MIN_SEG_W)
            boxes.append((nid, QRectF(x, y, w, LANE_H)))
            x += w + SEG_GAP
        return boxes

    # ── painting ──────────────────────────────────────────────────────
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), BG)
        p.setPen(QPen(LINE, 1))
        p.drawLine(0, 0, self.width(), 0)

        self._sync()        # before anything reads _order, the folded count too
        if self._collapsed:
            self._boxes = []
            self._toggle_rect = QRectF()
            self._thumb_rect = QRectF()
            self._paint_collapsed(p)
            p.end()
            return

        est = self._estimates()
        self._boxes = self._layout(est)
        self._paint_header(p, est)
        self._paint_nodes(p, est)
        self._paint_edges(p)
        self._paint_scrollbar(p)
        self._paint_keys(p)
        p.end()

    def _chevron(self, p: QPainter, open_: bool):
        """The fold control, drawn at the head of the header row."""
        y = PAD_TOP + 1
        self._chev_rect = QRectF(PAD_X, y - 1, CHEV_W, 15)
        p.save()
        p.setPen(QPen(ACCENT if self._hot == "chev" else MUTED, 1.4,
                      Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        c = self._chev_rect.center()
        if open_:                       # ▾ — click to fold
            pts = [QPointF(c.x() - 3.5, c.y() - 2.0), QPointF(c.x(), c.y() + 2.0),
                   QPointF(c.x() + 3.5, c.y() - 2.0)]
        else:                           # ▸ — click to unfold
            pts = [QPointF(c.x() - 2.0, c.y() - 3.5), QPointF(c.x() + 2.0, c.y()),
                   QPointF(c.x() - 2.0, c.y() + 3.5)]
        p.drawPolyline(pts)
        p.restore()

    def _paint_collapsed(self, p: QPainter):
        f = QFont(); f.setPointSize(NOTE_PT); p.setFont(f)
        self._chevron(p, False)
        n = len(self._order)
        note = f"Timeline  ·  {n} node{'' if n == 1 else 's'}"
        if self._held:
            note += f" · {len(self._held)} key held"
        p.setPen(MUTED)
        p.drawText(QRectF(PAD_X + CHEV_W + 4, PAD_TOP + 1, 380, 14),
                   Qt.AlignLeft | Qt.AlignVCenter, note)

    def _paint_header(self, p: QPainter, est: Dict[str, "flow.Estimate"]):
        f = QFont(); f.setPointSize(NOTE_PT); p.setFont(f)
        self._chevron(p, True)
        p.setPen(MUTED)
        y = PAD_TOP + 1
        if self._mode == self.ORDER:
            note = "Node order  ·  not time"
        else:
            n = sum(1 for e in est.values() if e.source == flow.MEASURED)
            total = sum(e.ms for e in est.values())
            unknown = any(e.source == flow.UNKNOWN for e in est.values())
            # "+" is doing real work here: with an unbounded node in the flow,
            # the total is a lower bound and saying "4.3 s" flat would be a lie.
            note = (f"Time  ·  {total / 1000:.1f} s"
                    + ("+ (unbounded)" if unknown else "")
                    + (f" · {n} measured" if n else " · no runs yet"))
        p.drawText(QRectF(PAD_X + CHEV_W + 4, y, 380, 14),
                   Qt.AlignLeft | Qt.AlignVCenter, note)

        # segmented Order|Time toggle, right-aligned
        tw, th = 92.0, 15.0
        self._toggle_rect = QRectF(self.width() - PAD_X - tw, y - 1, tw, th)
        path = QPainterPath()
        path.addRoundedRect(self._toggle_rect, 7, 7)
        p.fillPath(path, QBrush(TROUGH))
        p.setPen(QPen(LINE, 1)); p.drawPath(path)
        half = tw / 2
        sel = QRectF(self._toggle_rect.left() + (0 if self._mode == self.ORDER else half),
                     self._toggle_rect.top(), half, th)
        selp = QPainterPath(); selp.addRoundedRect(sel, 7, 7)
        p.fillPath(selp, QBrush(ACCENT))
        for i, (label, m) in enumerate((("Order", self.ORDER), ("Time", self.TIME))):
            p.setPen(ON_ACCENT if self._mode == m else MUTED)
            p.drawText(QRectF(self._toggle_rect.left() + i * half,
                              self._toggle_rect.top(), half, th),
                       Qt.AlignCenter, label)

    def _paint_nodes(self, p: QPainter, ests: Dict[str, "flow.Estimate"]):
        f = QFont(); f.setPointSize(7); f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        vp = self._viewport()
        p.save()
        # A little vertical slack so the 2 px running border is not shaved off.
        p.setClipRect(QRectF(vp.left(), vp.top() - 2, vp.width(), vp.height() + 4))
        for nid, r in self._boxes:
            if r.right() < vp.left() - 1 or r.left() > vp.right() + 1:
                continue                # scrolled out of the lane entirely
            node = self.graph.nodes.get(nid)
            if node is None:
                continue
            est = ests.get(nid) or flow.estimate(node, self._speed, self._measured)
            col = _seg_color(node)
            path = QPainterPath(); path.addRoundedRect(r, 4, 4)

            if est.source == flow.UNKNOWN:
                # Hatched: this box's width is a placeholder, not a duration.
                p.fillPath(path, QBrush(col.darker(260)))
                p.setPen(QPen(col.darker(150), 1, Qt.DashLine))
                p.drawPath(path)
            elif est.source == flow.CEILING:
                # An upper bound is not a duration either — outline, no fill.
                p.fillPath(path, QBrush(col.darker(300)))
                p.setPen(QPen(col, 1, Qt.DotLine))
                p.drawPath(path)
            else:
                g = QLinearGradient(r.topLeft(), r.bottomLeft())
                base = col if est.source == flow.EXACT else col.darker(125)
                g.setColorAt(0.0, base.lighter(112))
                g.setColorAt(1.0, base)
                p.fillPath(path, QBrush(g))
                p.setPen(QPen(base.darker(140), 1))
                p.drawPath(path)

            if nid in self._done:
                p.fillPath(path, QBrush(QColor(13, 12, 32, 90)))

            if nid == self._cur:
                self._paint_running(p, r, path, col)
                p.setPen(QPen(CUR, 2)); p.drawPath(path)
            elif nid == self._hover:
                p.setPen(QPen(TXT, 1)); p.drawPath(path)

            if r.width() > 34:
                p.setPen(QColor("#0b0e14") if est.source in (flow.EXACT,
                                                             flow.MEASURED)
                         else MUTED)
                icon, base_t = fc.node_header_label(node)
                label = node.data.get("name") or base_t
                p.drawText(r.adjusted(4, 0, -4, 0), Qt.AlignCenter,
                           fm.elidedText(label, Qt.ElideRight, int(r.width()) - 8))
        p.restore()

    def _paint_edges(self, p: QPainter):
        """Fade whichever edge the flow continues past.

        Over the node lane only — laid across the scrollbar it would hide the
        thumb at exactly the scroll position where the thumb is parked.
        """
        mx = self.max_scroll()
        if mx <= 0.5:
            return
        vp = self._viewport()
        clear = QColor(BG); clear.setAlpha(0)
        band = QRectF(vp.left(), vp.top() - 1, vp.width(), vp.height() + 2)
        if self._scroll > 0.5:
            g = QLinearGradient(band.left(), 0, band.left() + FADE_W, 0)
            g.setColorAt(0.0, BG); g.setColorAt(1.0, clear)
            p.fillRect(QRectF(band.left(), band.top(), FADE_W, band.height()),
                       QBrush(g))
        if self._scroll < mx - 0.5:
            g = QLinearGradient(band.right() - FADE_W, 0, band.right(), 0)
            g.setColorAt(0.0, clear); g.setColorAt(1.0, BG)
            p.fillRect(QRectF(band.right() - FADE_W, band.top(), FADE_W,
                              band.height()), QBrush(g))

    def _paint_scrollbar(self, p: QPainter):
        vp = self._viewport()
        self._track_rect = QRectF(vp.left(), vp.bottom() + BAR_GAP,
                                  vp.width(), float(BAR_H))
        mx = self.max_scroll()
        if mx <= 0.5:
            # Nothing to pan — no trough either. A permanent empty groove reads
            # as a control that is broken rather than one that is unneeded.
            self._thumb_rect = QRectF()
            return
        frac = min(1.0, vp.width() / max(1.0, self._content_w))
        tw = max(28.0, self._track_rect.width() * frac)
        tx = self._track_rect.left() + (self._track_rect.width() - tw) * (self._scroll / mx)
        self._thumb_rect = QRectF(tx, self._track_rect.top(), tw, float(BAR_H))
        rad = BAR_H / 2.0
        tr = QPainterPath(); tr.addRoundedRect(self._track_rect, rad, rad)
        p.fillPath(tr, QBrush(TROUGH))
        th = QPainterPath(); th.addRoundedRect(self._thumb_rect, rad, rad)
        hot = self._hot == "thumb" or self._grab is not None
        p.fillPath(th, QBrush(ACCENT if hot else MUTED))

    def _paint_running(self, p: QPainter, r: QRectF, path: QPainterPath,
                       col: QColor):
        p.save(); p.setClipPath(path)
        if self._cur_ms > 0:
            frac = min(fc.PROGRESS_CAP,
                       (time.perf_counter() - self._cur_t0) * 1000.0 / self._cur_ms)
            p.fillRect(QRectF(r.left(), r.top(), r.width() * frac, r.height()),
                       QBrush(col.lighter(150)))
        else:
            w = r.width() * 0.35
            ph = (time.perf_counter() * 0.9) % 1.0
            p.fillRect(QRectF(r.left() + (r.width() + w) * ph - w, r.top(),
                              w, r.height()), QBrush(col.lighter(150)))
        p.restore()

    def _paint_keys(self, p: QPainter):
        if not self._held:
            return
        f = QFont(); f.setPointSize(7); p.setFont(f)
        fm = QFontMetrics(f)
        y = PAD_TOP + HEAD_H + LANE_H + BAR_GAP + BAR_H + 4
        left = PAD_X + LABEL_W
        right = max(left + 10.0, float(self.width() - PAD_X))
        box_of = dict(self._boxes)
        now = time.perf_counter()
        for key, nid in self._held[:MAX_KEY_LANES]:
            # The name gutter does not pan: it says which key this lane is, and
            # scrolling that off the edge would leave an anonymous orange bar.
            p.setPen(TXT)
            p.drawText(QRectF(PAD_X, y, LABEL_W - 6, KEY_LANE_H),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(key.upper(), Qt.ElideRight, LABEL_W - 8))
            lane = QRectF(left, y, right - left, KEY_LANE_H)
            lp = QPainterPath(); lp.addRoundedRect(lane, 3, 3)
            p.fillPath(lp, QBrush(TROUGH))
            # The bar starts at the node that pressed the key and runs to the
            # node running now — which is exactly the span of flow that is
            # happening with this key down. Both ends come from the scrolled
            # boxes, so the bar pans with them and is clipped to the lane.
            x0 = box_of[nid].left() if nid in box_of else left
            x1 = (box_of[self._cur].right() if self._cur in box_of
                  else right)
            bar = QRectF(min(x0, x1), y, max(6.0, abs(x1 - x0)), KEY_LANE_H)
            p.save(); p.setClipPath(lp)
            bp = QPainterPath(); bp.addRoundedRect(bar, 3, 3)
            p.fillPath(bp, QBrush(HELD))
            p.setPen(QColor("#2a1005"))
            secs = now - self._held_since.get(key, now)
            vis = bar.intersected(lane)
            if vis.width() > 46:
                p.drawText(vis.adjusted(5, 0, -5, 0),
                           Qt.AlignLeft | Qt.AlignVCenter, f"held {secs:.1f} s")
            p.restore()
            y += KEY_LANE_H + KEY_GAP
        if len(self._held) > MAX_KEY_LANES:
            p.setPen(MUTED)
            p.drawText(QRectF(PAD_X, y - KEY_GAP, 200, KEY_LANE_H),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       f"+{len(self._held) - MAX_KEY_LANES} more held")

    # ── interaction ───────────────────────────────────────────────────
    def resizeEvent(self, e):
        self._boxes = []
        super().resizeEvent(e)

    def _hit_boxes(self):
        """The boxes to test a click against.

        paintEvent caches them, but a click can arrive before the first paint —
        offscreen, or on a tab that has never been shown — and hit-testing an
        empty cache silently does nothing.
        """
        if self._collapsed:
            return []
        if not self._boxes:
            self._boxes = self._layout()
        return self._boxes

    def _node_at(self, pos) -> Optional[str]:
        """Which node box the cursor is over — the visible lane only.

        A box scrolled past the gutter is still in `_boxes` (culling happens at
        paint time), so without the viewport test the key-name gutter would
        answer clicks on behalf of a node nobody can see.
        """
        vp = self._viewport()
        if not vp.contains(QPointF(pos.x(), vp.center().y())):
            return None
        for nid, r in self._hit_boxes():
            if r.contains(pos):
                return nid
        return None

    def _drag_to(self, x: float):
        """Pan so the thumb's left edge lands under the cursor."""
        room = self._track_rect.width() - self._thumb_rect.width()
        if room <= 0.5:
            return
        want = x - (self._grab or 0.0) - self._track_rect.left()
        self._set_scroll(want / room * self.max_scroll())

    def wheelEvent(self, e):
        """One wheel notch pans about two and a half boxes.

        A plain vertical wheel pans, rather than demanding Shift: the strip has
        exactly one axis, so there is nothing else the gesture could mean, and
        making people hold a modifier over a one-dimensional lane is a papercut.
        Either wheel works, whichever a given mouse or trackpad happens to send.
        """
        if self._collapsed or self.max_scroll() <= 0.5:
            e.ignore()
            return
        d = e.angleDelta()
        step = d.x() if abs(d.x()) >= abs(d.y()) else d.y()
        self._set_scroll(self._scroll - step / 120.0 * WHEEL_PX)
        e.accept()

    def mouseMoveEvent(self, e):
        pos = e.position() if hasattr(e, "position") else e.pos()
        if self._grab is not None:
            self._drag_to(pos.x())
            return

        hot = None
        if self._chev_rect.contains(pos):
            hot = "chev"
        elif (not self._thumb_rect.isNull()
                and self._track_rect.adjusted(0, -BAR_GRAB, 0, BAR_GRAB).contains(pos)):
            hot = "thumb"
        if hot != self._hot:
            self._hot = hot
            self.update()

        hit = None if hot else self._node_at(pos)
        if hit != self._hover:
            self._hover = hit
            node = self.graph.nodes.get(hit) if hit else None
            self.setToolTip(self._tip(node) if node is not None else "")
            self.update()
        live = hot or hit or self._toggle_rect.contains(pos)
        self.setCursor(Qt.PointingHandCursor if live else Qt.ArrowCursor)
        super().mouseMoveEvent(e)

    def _tip(self, node) -> str:
        est = flow.estimate(node, self._speed, self._measured)
        why = {flow.EXACT:    "from its settings",
               flow.MEASURED: "measured on this machine",
               flow.CEILING:  "at most — that is its timeout",
               flow.UNKNOWN:  "unknown until it has run"}[est.source]
        head = node.summary() if hasattr(node, "summary") else node.type
        if est.source == flow.UNKNOWN:
            return f"{head}\nDuration: unknown until it has run"
        return f"{head}\n{est.ms / 1000:g} s — {why}"

    def mousePressEvent(self, e):
        pos = e.position() if hasattr(e, "position") else e.pos()
        # The chevron is the one control that exists in both states, so it is
        # asked first and the folded strip answers nothing else.
        if self._chev_rect.contains(pos):
            self.set_collapsed(not self._collapsed)
            return
        if self._collapsed:
            super().mousePressEvent(e)
            return
        if self._toggle_rect.contains(pos):
            self.set_mode(self.TIME if self._mode == self.ORDER else self.ORDER)
            return
        if not self._thumb_rect.isNull() and \
                self._track_rect.adjusted(0, -BAR_GRAB, 0, BAR_GRAB).contains(pos):
            if self._thumb_rect.adjusted(-2, -BAR_GRAB, 2, BAR_GRAB).contains(pos):
                self._grab = pos.x() - self._thumb_rect.left()   # pick it up where it was grabbed
            else:
                self._grab = self._thumb_rect.width() / 2.0      # bare track: jump, centred
                self._drag_to(pos.x())
            self.update()
            return
        nid = self._node_at(pos)
        if nid is not None:
            self.node_clicked.emit(nid)
            return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if self._grab is not None:
            self._grab = None
            self.update()
        super().mouseReleaseEvent(e)

    def leaveEvent(self, e):
        if self._hover is not None or self._hot is not None:
            self._hover = self._hot = None
            self.update()
        super().leaveEvent(e)
