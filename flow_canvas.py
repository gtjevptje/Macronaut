"""
Visual node-graph canvas for the Macronaut Sequence builder.

A QGraphicsView/Scene editor where every step, decision and loop is a draggable
node, connected by bezier "flow lines". Output ports are dragged onto another
node's input port to wire the flow. Backed by a flow.FlowGraph.

The canvas is intentionally view-only of the model: it builds NodeItems/EdgeItems
from a FlowGraph and writes structural edits straight back into that graph, so
saving is just graph.to_dict().
"""
import json
import os
import time
from typing import Dict, List, Optional

from PySide6.QtCore import (Qt, QRect, QRectF, QPointF, Signal, QObject,
                            QTimer)
from PySide6.QtGui import (QColor, QPen, QBrush, QPainter, QPainterPath, QFont,
                         QFontMetrics, QPainterPathStroker, QGuiApplication,
                         QPixmap, QIcon, QPolygonF, QTransform)
from PySide6.QtWidgets import (QGraphicsView, QGraphicsScene, QGraphicsItem,
                             QGraphicsObject, QGraphicsEllipseItem,
                             QGraphicsPathItem, QMenu, QLabel, QInputDialog,
                             QFrame, QRubberBand, QDialog, QPlainTextEdit)
# Tells a live wrapper from one whose C++ item Qt has already destroyed. Ships
# with PySide6 (and is pinned in macronaut.spec) — not an extra dependency.
from shiboken6 import isValid

import entitlements
import flow

# -- Palette (dark theme to match the app) -------------------------------------
NODE_BG     = QColor("#252a3a")
NODE_BORDER = QColor("#3b4252")
NODE_SEL    = QColor("#89b4fa")
TEXT_COL    = QColor("#cdd6f4")
SUB_COL     = QColor("#9399b2")
EDGE_COL    = QColor("#6c7086")
GRID_COL    = QColor("#2a2756")   # cosmic dot grid (matches the 2.0 mockup)
CANVAS_BG   = QColor("#0d0c20")   # cosmic dark, matches the compact face

# Colour code shared by the canvas node headers AND the "Add node" palette
# buttons in main.py, so the two read as the same family at a glance.
FAMILY_COLORS = {
    "click":  "#3b82f6",   # blue
    "type":   "#22b8cf",   # cyan
    "wait":   "#14b8a6",   # teal
    "detect": "#ec4899",   # pink
    "if":     "#f59e0b",   # amber
    "loop":   "#a855f7",   # purple
    "goto":   "#6b7a99",   # slate
    "end":    "#ef4444",   # red
    "start":  "#22c55e",   # green
    # A comment box is not a step, so it takes the one neutral tone in the set
    # rather than a colour that would read as another kind of work.
    "comment": "#64748b",
}

FAMILY_ICONS  = {"click": "🖱", "type": "⌨", "wait": "⏱", "detect": "🔍"}
FAMILY_TITLES = {"click": "Click", "type": "Type", "wait": "Wait", "detect": "Detect"}

# Map a stored action "kind" to one of the four user-facing action families.
_KIND_FAMILY = {
    "click": "click", "move": "click", "scroll": "click", "drag": "click",
    "key": "type", "combo": "type", "text": "type",
    "wait": "wait",
    "wait_image": "detect", "wait_text": "detect", "wait_pixel": "detect",
}


def family_for_kind(kind: str) -> str:
    return _KIND_FAMILY.get(kind, "click")


# Control-flow node header colours (non-action).
HEADER_COLORS = {
    flow.N_START:   QColor(FAMILY_COLORS["start"]),
    flow.N_END:     QColor(FAMILY_COLORS["end"]),
    flow.N_ACTION:  QColor(FAMILY_COLORS["click"]),
    flow.N_IF:      QColor(FAMILY_COLORS["if"]),
    flow.N_LOOP:    QColor(FAMILY_COLORS["loop"]),
    flow.N_GOTO:    QColor(FAMILY_COLORS["goto"]),
    # legacy types kept only so old saved files still render:
    flow.N_SETVAR:  QColor("#06b6d4"),
    flow.N_LABEL:   QColor("#64748b"),
    flow.N_COMMENT: QColor("#475569"),
    # A reroute wears the wire's own colour, because that is what it is.
    flow.N_REROUTE: QColor(EDGE_COL),
}


# Titles for the control-flow types, where "if".title() would read worse than
# the name the palette and the editor use.
TYPE_TITLES = {flow.N_IF: "If / Else"}


# Tints the user can put on an individual node, offered in its right-click menu
# and stored as node.data["color"].
#
# HEADER_COLORS above says what a node *is*. It is keyed by type, so it can
# never say what a node is *for* — which of six identical Click nodes is the
# login one, which branch is the risky one, which three steps belong together.
# That is the job this does, and it is the one thing type-keyed colour cannot
# be stretched to cover.
#
# Deliberately a short fixed list rather than a colour picker: eight tints stay
# tellable apart at a glance across a whole canvas, and a graph whose nodes are
# each a slightly different blue carries less than one with no colour at all.
# They are also the frame palette — a frame and the nodes it holds should be
# able to agree.
NODE_TINTS = [
    ("Rose",    "#f43f5e"), ("Amber",   "#f59e0b"),
    ("Lime",    "#84cc16"), ("Emerald", "#10b981"),
    ("Sky",     "#0ea5e9"), ("Indigo",  "#6366f1"),
    ("Violet",  "#a855f7"), ("Slate",   "#64748b"),
]


def node_tint(node: "flow.FlowNode") -> Optional[QColor]:
    """The user's own colour for this node, or None to use its type's.

    Parsed through flow.color_tuple rather than handed to QColor: a hand-edited
    flow can hold anything, and QColor("nonsense") is an *invalid* colour that
    paints black rather than raising — so one typo would give you a node-shaped
    hole in the canvas and no clue why.
    """
    rgb = flow.color_tuple(node.data.get("color"))
    return QColor(*rgb) if rgb else None


def color_swatch(hexv: str, size: int = 12) -> QIcon:
    """A filled square for a menu entry, so the colour names are checkable."""
    px = QPixmap(size, size)
    px.fill(QColor(hexv))
    return QIcon(px)


def node_header_color(node: "flow.FlowNode") -> QColor:
    tint = node_tint(node)
    if tint is not None:
        return tint
    # flow.action_kind() falls back to the palette family, so a node shows its
    # real colour from the moment it appears — not only once its editor is OK'd.
    if node.type == flow.N_ACTION:
        return QColor(FAMILY_COLORS[family_for_kind(flow.action_kind(node))])
    return HEADER_COLORS.get(node.type, QColor(FAMILY_COLORS["click"]))


# Kinds whose family name would be a lie written across the node. A scroll and
# a move are both mouse work and keep the mouse colour, but "Click" on a node
# that never clicks costs more than one extra word in the vocabulary does.
KIND_LABELS = {"scroll": ("🖱", "Scroll"), "move": ("🖱", "Move"),
               "drag": ("🖱", "Drag")}


def node_header_label(node: "flow.FlowNode"):
    """(icon, base-title) for a node header."""
    if node.type == flow.N_ACTION:
        kind = flow.action_kind(node)
        if kind in KIND_LABELS:
            return KIND_LABELS[kind]
        fam = family_for_kind(kind)
        return FAMILY_ICONS.get(fam, "⚙"), FAMILY_TITLES.get(fam, "Action")
    return (flow.node_icon(node.type),
            TYPE_TITLES.get(node.type, node.type.replace("_", " ").title()))


def node_image_path(node: "flow.FlowNode") -> str:
    """The image this node matches against, or '' if it doesn't match one.

    Covers the If and Loop nodes too, not just Detect: wiring both outputs of an
    image Detect node *promotes it into* an If (flow.convert_detect_to_if), and
    the picture disappearing at that moment would read as a bug.
    """
    if node.type == flow.N_ACTION:
        step = node.data.get("step") or {}
        if step.get("kind") == "wait_image":
            return ((step.get("data") or {}).get("image_path") or "")
        return ""
    if node.type == flow.N_IF or (node.type == flow.N_LOOP and
                                  node.data.get("mode") in ("while", "until")):
        cond = node.data.get("condition") or {}
        if cond.get("type") == "image":
            return cond.get("image_path") or ""
    return ""


def thumb_caption(node: "flow.FlowNode") -> str:
    """All that is left to say once the picture is on the node itself.

    Which image it is, is now visible. Whether the step also *clicks* it is not,
    and that is the difference between watching and acting — so that one word
    stays.
    """
    if node.type != flow.N_ACTION:
        return ""
    d = (node.data.get("step") or {}).get("data") or {}
    if not d.get("click"):
        return ""
    return "click ×2" if d.get("clicks", 1) == 2 else "click"


def node_thumbnail(path: str) -> Optional[QPixmap]:
    """Cached, pre-scaled thumbnail for `path`; None if it can't be read.

    Scaling happens once per file, not once per paint — paint runs on every
    pan, zoom and selection change, and QPixmap.scaled() of a full-screen crop
    on each of those is how a canvas starts to feel heavy.
    """
    if not path:
        return None
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    px = _THUMB_CACHE.get(key)
    if px is None:
        src = QPixmap(path)
        if src.isNull():
            return None
        px = src.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio,
                        Qt.SmoothTransformation)
        if len(_THUMB_CACHE) >= _THUMB_CACHE_MAX:
            _THUMB_CACHE.pop(next(iter(_THUMB_CACHE)))
        _THUMB_CACHE[key] = px
    return px


PORT_COLORS = {
    "out":   QColor("#9399b2"),
    "true":  QColor("#22c55e"),
    "false": QColor("#ef4444"),
    "body":  QColor("#a855f7"),
    "done":  QColor("#9399b2"),
    "error": QColor("#ef4444"),
    "in":    QColor("#cdd6f4"),
}

# What each output port is labelled on the node. The glyph carries the meaning
# at a glance; the word is there for anyone who doesn't read glyphs.
PORT_LABELS = {
    "true":  "✓ true",
    "false": "✗ false",
    "body":  "↻ body",
    "done":  "done",
    "error": "⚠ error",
}

NODE_W   = 184
HEADER_H = 26
# Every node is the same height, whether it has one output port or two. Sizing
# each node to its own port count made Start, End and Go to visibly shorter
# than an If, which reads as an accident rather than a distinction.
NODE_H   = HEADER_H + 16 + 2 * 20   # 82 — fits the tallest case (two ports)
# ...except the terminators. Start and End hold no settings and have no body to
# summarise, so a full-height card is 52 px of empty space pretending to be a
# step. They are drawn as a single bar instead — same width, same left edge and
# same port x as every other node, so nothing about the column moves.
TERMINAL_H     = 30
TERMINAL_TYPES = (flow.N_START, flow.N_END)

# A reroute is a bend in a wire, so it is drawn as one: a dot the width of two
# ports, with nothing written on it. Anything card-shaped would claim to be a
# step, and the whole point is that it is not one — it costs no time, holds no
# settings, and the flow reads identically with it and without it.
REROUTE_W = 22

# A node that matches a picture shows the picture. "Image «capture 31544.png»"
# names a file nobody chose the name of, so the only way to know which image a
# node watches for was to open its editor.
#
# The thumbnail REPLACES that filename rather than joining it — same node, same
# height as every other node, no growing. It fits in the space the summary line
# used because the body's free rectangle is bounded on three sides by things
# already drawn there, all measured rather than guessed:
#   · the port label ("⚠ error" is the widest at 77 px, right-aligned to
#     x=172) starts at x≈95, so the well stops at 86;
#   · the retry / pre-delay badges own the last 18 px, so it stops at 62;
#   · what's left to the right of it, above the port label, holds the caption.
THUMB_W, THUMB_H = 76, 32
THUMB_LEFT       = 10
THUMB_TOP        = HEADER_H + 4        # where the summary line used to start
CAPTION_LEFT     = THUMB_LEFT + THUMB_W + 6
CAPTION_H        = 20                  # stops above the port-label band at y=54

# Scaled pixmaps, keyed by (path, mtime) so re-capturing over the same filename
# still redraws. Bounded: a long-lived canvas would otherwise hold every image
# any node ever pointed at.
_THUMB_CACHE: Dict[tuple, QPixmap] = {}
_THUMB_CACHE_MAX = 64

PORT_R   = 6
GRID     = 26   # matches dot-grid step in drawBackground


def _snap(v: float) -> float:
    """Snap a scene coordinate to the nearest grid line."""
    return round(v / GRID) * GRID


# How close two background dots may get on screen before the grid coarsens.
# 13 px is exactly GRID at 50% zoom, so nothing about the background changes at
# or above half zoom — the doubling only ever happens further out than that,
# where the dots were merging into a haze anyway.
MIN_DOT_PX = 13.0


def _grid_step(scale: float):
    """`(step, dot width)` for the background at this zoom.

    The dot grid is drawn in *scene* coordinates, so the number of dots on
    screen grows as 1/zoom². A 500-node flow zoomed to fit was drawing 37,000
    of them per repaint and spending more time on the background than on every
    node and wire in the flow put together.

    The step doubles until the dots are at least MIN_DOT_PX apart on screen,
    which holds the count roughly constant at any zoom. *Doubling* specifically
    — 26, 52, 104 — so every dot that survives is still on a real grid line and
    the background still says where a node will snap to. The dot grows with the
    step to keep its on-screen size, or zooming out would fade the canvas to an
    empty black field.
    """
    scale = scale or 1.0
    step = GRID
    while step * scale < MIN_DOT_PX and step < GRID * 64:
        step *= 2
    return step, 2.2 * step / GRID


def _seg_hits(x0: float, y0: float, x1: float, y1: float,
              l: float, t: float, r: float, b: float) -> bool:
    """True if any part of the segment (x0,y0)-(x1,y1) lies inside l/t/r/b.

    Exact (Liang–Barsky slab clipping), not sampled. Sampling every 8 px let a
    segment clip a rect *corner* with no sample landing inside, so the router
    believed a grazing lane was clear and drew the wire through the node —
    which only showed up once the lane search got good enough to prefer those
    near-misses over a wide, genuinely clear bow.

    Plain floats, no QPointF/QRectF, because this is the innermost loop of the
    whole canvas: routing the wires of a 500-node flow called it 1.6 million
    times and spent more of that time crossing into Qt to read four rect edges
    and four point coordinates than doing the arithmetic. Two early exits carry
    almost all of it:

    * **Bounding boxes that miss.** Most obstacles a route is checked against
      are nowhere near the segment being checked.
    * **Axis-aligned segments.** Every segment a lane route is made of is
      horizontal or vertical, and for those "the bounding boxes overlap" is not
      an approximation of the answer — it *is* the answer. Only the direct
      point-to-point line is ever diagonal, so the slab clipping below runs
      once per route rather than once per candidate segment.
    """
    if x0 < x1:
        lo_x, hi_x = x0, x1
    else:
        lo_x, hi_x = x1, x0
    if hi_x < l or lo_x > r:
        return False
    if y0 < y1:
        lo_y, hi_y = y0, y1
    else:
        lo_y, hi_y = y1, y0
    if hi_y < t or lo_y > b:
        return False
    if x0 == x1 or y0 == y1:
        return True

    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - l), (dx, r - x0), (-dy, y0 - t), (dy, b - y0)):
        if -1e-9 < p < 1e-9:
            if q < 0:
                return False          # parallel and outside this slab
            continue
        tt = q / p
        if p < 0:
            if tt > t1:
                return False
            if tt > t0:
                t0 = tt
        else:
            if tt < t0:
                return False
            if tt < t1:
                t1 = tt
    return t0 <= t1


def _seg_hits_rect(a: QPointF, b: QPointF, rect: QRectF) -> bool:
    """Qt-flavoured `_seg_hits`, for callers that already hold Qt objects."""
    return _seg_hits(a.x(), a.y(), b.x(), b.y(),
                     rect.left(), rect.top(), rect.right(), rect.bottom())


STUB = 26.0        # how far a wire leaves a port before it may turn
CORNER_R = 12.0    # rounding applied at each waypoint

# ── Run-state drawing ─────────────────────────────────────────────────────────
# A node that is running fills a bar along its bottom edge. Nothing in the body
# moves for it: the bar lives inside the rounded rect, under everything, so a
# node looks identical whether or not a run is happening.
PROGRESS_H       = 4.0
PROGRESS_UNKNOWN = -1.0    # "running, no idea how long" -> a shimmer, not a fill
# The bar is a *prediction* (see flow.estimate) and predictions run out. It
# stops here instead of sitting full and lying, then snaps away when the node
# genuinely ends — a bar parked at 96% is usefully saying "this is running long".
PROGRESS_CAP     = 0.96
TROUGH_COL       = QColor("#12141d")
# Held keys get their own colour, shared with the timeline strip, because "this
# is still pressed" is one idea in two places.
HELD_COL         = QColor("#f97316")


def _polyline_clear(pts, obstacles) -> bool:
    """True if no segment of the polyline passes through any obstacle."""
    return not any(_seg_hits_rect(pts[i], pts[i + 1], r)
                   for i in range(len(pts) - 1) for r in obstacles)


def _run_clear(x0: float, y0: float, x1: float, y1: float, boxes) -> bool:
    """True if one segment misses every box. Boxes are (l, t, r, b) tuples."""
    for l, t, r, b in boxes:
        if _seg_hits(x0, y0, x1, y1, l, t, r, b):
            return False
    return True


def _lane_route(p1: QPointF, p2: QPointF, lane: float,
                out_x: float, in_x: float):
    """Leave right, run along `lane`, come back in from the left."""
    return [p1,
            QPointF(out_x, p1.y()), QPointF(out_x, lane),
            QPointF(in_x, lane), QPointF(in_x, p2.y()),
            p2]


def _quick_route(p1: QPointF, p2: QPointF):
    """A route that costs nothing, for while a node is being dragged.

    No obstacle search at all: forward wires are a straight line, backward ones
    take a fixed lane under both ends so a loop-back doesn't cut through its own
    source. It is wrong in the way a rubber band is wrong — it shows where the
    wire goes, and the real route is computed once, on release.
    """
    if p2.x() >= p1.x() + 2 * STUB:
        return [p1, p2]
    lane = max(p1.y(), p2.y()) + 70.0
    return _lane_route(p1, p2, lane, p1.x() + STUB, p2.x() - STUB)


def _route_points(p1: QPointF, p2: QPointF, obstacles, gap: float = 24.0):
    """Waypoints from p1 to p2 that clear every obstacle — and are checked.

    Wires leave a node on the right and arrive on the left, so a *backward*
    wire — a loop-back, the only backward wire our layouts use — can never be
    a straight line. It has to step out past the source, run along a clear
    lane, and come back in on the far side of the destination.

    The previous version computed one pair of turn-x positions for both
    directions, which collapsed (left >= right) on every loop-back and fell
    back to hugging the obstacle's own bounds. That is why loop-backs were the
    one case that drew straight through a node.

    Two things are different now. The lane is *tried* rather than assumed:
    above and below, at increasing distances, and the first candidate whose
    every segment misses every obstacle wins. And the result is a polyline
    whose corridor the drawn curve is guaranteed to stay inside — see
    _smooth_path, which rounds corners instead of splining through them.
    """
    ax, ay, bx, by = p1.x(), p1.y(), p2.x(), p2.y()
    # One crossing into Qt per obstacle instead of four per obstacle per
    # candidate segment. Everything below is arithmetic on these tuples.
    boxes = [(r.left(), r.top(), r.right(), r.bottom()) for r in obstacles]

    backward = bx < ax + 2 * STUB
    hits = [bo for bo in boxes if _seg_hits(ax, ay, bx, by, *bo)]
    if not backward and not hits:
        return [p1, p2]

    if hits:
        s_l = min(bo[0] for bo in hits)
        s_t = min(bo[1] for bo in hits)
        s_r = max(bo[2] for bo in hits)
        s_b = max(bo[3] for bo in hits)
    else:
        s_l, s_r = (ax, bx) if ax <= bx else (bx, ax)
        s_t, s_b = (ay, by) if ay <= by else (by, ay)

    out_x = ax + STUB
    in_x = bx - STUB
    if backward and hits:
        # The turn-outs must sit clear of both nodes, not between them.
        out_x = max(out_x, s_r + gap)
        in_x = min(in_x, s_l - gap)

    midy = (ay + by) / 2.0

    # Candidate lanes come from the obstacles themselves — just above or just
    # below each one is where a free corridor actually is. Only bowing around
    # the union of the blocking rects finds nothing in a dense graph, because
    # the union spans everything and its outside edge is blocked by whatever
    # else is out there. Ordered nearest-first so the wire stays as close to
    # the direct line as it can.
    lanes = {midy}
    for l, t, r, b in boxes:
        lanes.add(t - gap)
        lanes.add(b + gap)
    for extra in (0.0, 40.0, 90.0, 160.0, 260.0, 380.0):
        lanes.add(s_t - gap - extra)
        lanes.add(s_b + gap + extra)
    lanes = sorted(lanes, key=lambda y: abs(y - midy))

    # The turn-out columns can be blocked too, so give them somewhere to go.
    out_cols = sorted({out_x} | {r + gap for _, _, r, _ in boxes
                                 if r + gap > out_x})[:4]
    in_cols = sorted({in_x} | {l - gap for l, _, _, _ in boxes
                               if l - gap < in_x}, reverse=True)[:4]

    # Two reorderings of the same (lanes x columns) search, worth several times
    # its old cost between them, and neither changes which route wins: the
    # candidates that survive are the same ones, visited in the same order —
    # nearest lane first, and within a lane the turn-outs nearest the ports.
    #
    #  * The stub leaving p1 depends only on its column, and the stub arriving
    #    at p2 only on its own. Both were being re-tested for every lane. A
    #    column whose stub is blocked cannot appear in any winning candidate,
    #    so drop it once, up front, rather than rediscovering it per lane.
    out_cols = [ox for ox in out_cols if _run_clear(ax, ay, ox, ay, boxes)]
    in_cols = [ix for ix in in_cols if _run_clear(ix, by, bx, by, boxes)]

    #  * Of the three segments left, the long horizontal *is* the lane, and is
    #    by far the likeliest to be blocked — choosing a lane is exactly the
    #    business of finding one that is not. Testing it before the two short
    #    verticals fails most candidates on their first segment instead of
    #    their third.
    for lane in lanes:
        for ox in out_cols:
            for ix in in_cols:
                if (_run_clear(ox, lane, ix, lane, boxes)
                        and _run_clear(ox, ay, ox, lane, boxes)
                        and _run_clear(ix, lane, ix, by, boxes)):
                    return _lane_route(p1, p2, lane, ox, ix)

    # Nothing clean exists — nodes stacked across every way round. Take the
    # widest bow rather than the straight line, so the wire is at least
    # followable rather than hidden inside a node.
    # out_x/in_x, not ax + STUB / bx - STUB: on a backward wire those have been
    # pushed clear of both nodes, and the whole point of the fallback is that
    # the nodes are in the way.
    return _lane_route(p1, p2, s_b + gap + 380.0, out_x, in_x)


def _smooth_path(pts) -> QPainterPath:
    """Round the corners of the waypoint polyline.

    Deliberately not a Catmull-Rom spline any more. A spline does not stay
    inside the polygon of its control points — it overshoots on tight turns —
    so a route that had been checked clear could still be drawn through a
    node. Corner rounding is bounded by construction: each arc lives inside
    the two segments that meet at it, so if the polyline clears the obstacles
    the drawn curve does too.
    """
    if len(pts) == 2:
        p1, p2 = pts
        path = QPainterPath(p1)
        dx = max(40, abs(p2.x() - p1.x()) * 0.5)
        path.cubicTo(p1 + QPointF(dx, 0), p2 - QPointF(dx, 0), p2)
        return path

    path = QPainterPath(pts[0])
    for i in range(1, len(pts) - 1):
        prev, cur, nxt = pts[i - 1], pts[i], pts[i + 1]
        r = min(CORNER_R,
                _dist(prev, cur) / 2.0,
                _dist(cur, nxt) / 2.0)
        if r <= 0.5:
            path.lineTo(cur)
            continue
        path.lineTo(_towards(cur, prev, r))
        path.quadTo(cur, _towards(cur, nxt, r))
    path.lineTo(pts[-1])
    return path


def _dist(a: QPointF, b: QPointF) -> float:
    dx, dy = b.x() - a.x(), b.y() - a.y()
    return (dx * dx + dy * dy) ** 0.5


def _towards(origin: QPointF, target: QPointF, d: float) -> QPointF:
    """Point `d` px from `origin` along the line to `target`."""
    n = _dist(origin, target)
    if n <= 1e-6:
        return QPointF(origin)
    t = d / n
    return QPointF(origin.x() + (target.x() - origin.x()) * t,
                   origin.y() + (target.y() - origin.y()) * t)


# ==============================================================================
#  Port
# ==============================================================================
class PortItem(QGraphicsEllipseItem):
    def __init__(self, node_item: "NodeItem", name: str, is_output: bool):
        super().__init__(-PORT_R, -PORT_R, PORT_R * 2, PORT_R * 2, node_item)
        self.node_item = node_item
        self.name = name
        self.is_output = is_output
        self.setBrush(QBrush(PORT_COLORS.get(name, SUB_COL)))
        self.setPen(QPen(NODE_BG, 2))
        self.setZValue(3)
        self.setAcceptHoverEvents(True)
        self.setToolTip(("Out: " if is_output else "In: ") + name)

    def hoverEnterEvent(self, e):
        self.setPen(QPen(NODE_SEL, 2)); super().hoverEnterEvent(e)

    def hoverLeaveEvent(self, e):
        self.setPen(QPen(NODE_BG, 2)); super().hoverLeaveEvent(e)


# ==============================================================================
#  Node
# ==============================================================================
class NodeItem(QGraphicsObject):
    geometry_changed = Signal()

    def __init__(self, node: "flow.FlowNode"):
        super().__init__()
        self.node = node
        self._ports: Dict[str, PortItem] = {}
        self.setFlags(QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemSendsGeometryChanges)
        self.setZValue(1)
        self.setPos(node.x, node.y)

        # Ports are built once, so remember which ones this item was built with:
        # an action node's outputs depend on its step kind, which an edit can
        # change under us (see FlowScene.refresh_node).
        self.out_ports = list(node.ports())
        self.terminal = node.type in TERMINAL_TYPES
        # A reroute is a dot, not a card — its own width and its own paint path.
        self.dot = node.type == flow.N_REROUTE
        self._w = REROUTE_W if self.dot else NODE_W
        # Presence, not readability: a node whose image file has gone missing
        # still shows the well and says so inside it. Nothing here changes the
        # node's size — the thumbnail lives in the body it already had.
        self.thumb_path = node_image_path(node)
        self._h = (REROUTE_W if self.dot else
                   TERMINAL_H if self.terminal else
                   max(NODE_H, HEADER_H + 16 + len(self.out_ports) * 20))
        # Run state, set by the scene during playback and never saved.
        #   progress  None = no bar; 0..1 = a fill; PROGRESS_UNKNOWN = shimmer
        #   live_keys keys this node pressed that are STILL DOWN — the node has
        #             finished, but its effect has not, and that is a state the
        #             graph could not express before Hold down existed.
        self.progress: Optional[float] = None
        self.live_keys: List[str] = []
        self._build_ports(self.out_ports)

    # -- geometry --
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._w, self._h)

    def _build_ports(self, out_ports):
        if self.node.type != flow.N_START:
            p = PortItem(self, "in", False)
            p.setPos(0, self._h / 2)
            self._ports["in"] = p
        if out_ports:
            if self.terminal or self.dot:
                # No header/body split on a bar — the port sits on its middle.
                ys = [self._h / 2] * len(out_ports)
            else:
                gap = (self._h - HEADER_H) / (len(out_ports) + 1)
                ys = [HEADER_H + gap * (i + 1) for i in range(len(out_ports))]
            for name, y in zip(out_ports, ys):
                p = PortItem(self, name, True)
                p.setPos(self._w, y)
                self._ports[name] = p

    def port_pos(self, name: str) -> QPointF:
        p = self._ports.get(name)
        if p:
            return p.scenePos()
        return self.scenePos() + QPointF(self._w / 2, self._h / 2)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange:
            # Snap proposed position to grid before the move is committed.
            return QPointF(_snap(value.x()), _snap(value.y()))
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.node.x = self.pos().x()
            self.node.y = self.pos().y()
            self.geometry_changed.emit()
        return super().itemChange(change, value)

    def refresh(self):
        # Re-read rather than repaint blindly: an edit can point the node at a
        # different image, and the path is cached on the item.
        self.thumb_path = node_image_path(self.node)
        self.update()

    # -- paint --
    # Below this on-screen scale a node's 9pt text is under 4pt — a grey smear
    # that costs a font-metrics pass, an elide and a glyph run per node to
    # produce. Zoomed out that far the question being asked of the canvas is
    # "where is everything", which shape and header colour answer on their own.
    LOD_TEXT = 0.42

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        r = self.boundingRect()
        # How big this node currently is on screen, so a zoomed-out canvas can
        # skip what would not be legible anyway. option is None only if someone
        # calls paint() by hand; assume full detail then.
        lod = (option.levelOfDetailFromTransform(painter.worldTransform())
               if option is not None else 1.0)

        if self.dot:
            # No header, no body, no label. The wire's ports are already drawn
            # on top of this circle, so what is left to say is only "the wire
            # bends here" — and a ring says that in less space than a word.
            col = node_header_color(self.node)
            painter.setBrush(QBrush(col.darker(160)))
            painter.setPen(QPen(NODE_SEL if self.isSelected() else col,
                                3 if self.isSelected() else 2))
            painter.drawEllipse(r.adjusted(2, 2, -2, -2))
            return

        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)

        if self.terminal:
            # Start / End: the header *is* the node. Filled bar, centred label,
            # nothing else — there is nothing else to say about them.
            col = node_header_color(self.node)
            painter.fillPath(path, QBrush(col))
            painter.setPen(QPen(NODE_SEL if self.isSelected() else col.darker(140),
                                2 if self.isSelected() else 1))
            painter.drawPath(path)
            if lod < self.LOD_TEXT:
                return
            painter.setPen(QColor("#0b0e14"))
            ft = QFont(); ft.setBold(True); ft.setPointSize(9); painter.setFont(ft)
            icon, base = node_header_label(self.node)
            nm = self.node.data.get("name", "")
            if nm.strip().lower() == base.strip().lower():
                nm = ""
            label = f"{icon}  {base}" + (f"  ·  {nm}" if nm else "")
            painter.drawText(r, Qt.AlignCenter,
                             QFontMetrics(ft).elidedText(label, Qt.ElideRight,
                                                         NODE_W - 16))
            return

        painter.fillPath(path, QBrush(NODE_BG))

        hdr = QRectF(0, 0, NODE_W, HEADER_H)
        painter.setClipPath(path)
        painter.fillRect(hdr, node_header_color(self.node))
        painter.setClipping(False)

        # A node whose keys are still down keeps a stripe down its left edge,
        # so "what is still holding W" is answerable by looking rather than by
        # remembering which node you passed. Inside the clip, so it follows the
        # corner radius; under the border, so selection still reads as selection.
        if self.live_keys:
            painter.setClipPath(path)
            painter.fillRect(QRectF(0, 0, 3, self._h), HELD_COL)
            painter.setClipping(False)

        pen = QPen(NODE_SEL if self.isSelected() else
                   HELD_COL if self.live_keys else NODE_BORDER,
                   2 if (self.isSelected() or self.live_keys) else 1)
        painter.setPen(pen)
        painter.drawPath(path)

        if lod < self.LOD_TEXT:
            # Everything above this line is a shape or a colour and survives
            # being shrunk: the card, the header band, the held stripe, the
            # selection border, the run bar. Everything below it is text, a
            # thumbnail or a badge, and at this scale each one costs a font
            # setup and a glyph run to produce something under 4 pt. Skipping
            # them took a zoomed-out repaint of a 500-node flow from 8 fps to
            # a rate that keeps up with the mouse.
            self._paint_progress(painter, path)
            return

        # A PRO chip on any step this copy is not licensed to run.
        #
        # It is drawn on the node rather than only enforced at Play because the
        # alternative is a surprise: someone builds a twelve-step flow around a
        # Wait-for-image, presses Play, and only then finds out. Saying it while
        # they are still building is both the honest moment and the one where
        # the feature has just demonstrated why it is worth paying for.
        #
        # ⚠ Disappears entirely once licensed. A permanent "PRO" label on a step
        # someone has already bought is an advert shown to a customer.
        badge_w = 0
        if entitlements.show_pro_badge(self.node):
            badge_w = 30
            chip = QRectF(NODE_W - 8 - badge_w, (HEADER_H - 14) / 2.0, badge_w, 14)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(11, 14, 20, 140)))
            painter.drawRoundedRect(chip, 4, 4)
            fb = QFont(); fb.setBold(True); fb.setPointSize(7); painter.setFont(fb)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(chip, Qt.AlignCenter, "PRO")
            painter.setBrush(Qt.NoBrush)

        # header text: icon + family/type (+ optional node name)
        painter.setPen(QColor("#0b0e14"))
        f = QFont(); f.setBold(True); f.setPointSize(9); painter.setFont(f)
        icon, base = node_header_label(self.node)
        name = self.node.data.get("name", "")
        # "▶ Start · start" says the same thing twice — the Start node now carries
        # "start" as a name so a Go to can reach it, and that shouldn't cost a
        # duplicated word in the header.
        if name.strip().lower() == base.strip().lower():
            name = ""
        title = f"{icon}  {base}" + (f"  ·  {name}" if name else "")
        fm0 = QFontMetrics(f)
        # ⚠ The chip's width comes off the title's, in both the elide and the
        # rect. Eliding to the full width and then drawing the chip over the
        # result would clip the last word under the badge instead of ending it
        # with an ellipsis — which reads as a rendering fault rather than as a
        # long name, and is invisible to any test that checks the string.
        title_w = NODE_W - 16 - (badge_w + 6 if badge_w else 0)
        title = fm0.elidedText(title, Qt.ElideRight, title_w)
        painter.drawText(QRectF(8, 0, title_w, HEADER_H),
                         Qt.AlignVCenter | Qt.AlignLeft, title)

        # body summary — unless the picture is the summary (below).
        summary_txt = "" if self.thumb_path else self.node.summary()
        # The header already says "If" — repeating it below leaves less room for
        # the part that differs between two If nodes, which is the condition.
        if self.node.type == flow.N_IF and summary_txt.startswith("If "):
            summary_txt = summary_txt[3:]
        if summary_txt.strip().lower() != base.strip().lower():
            painter.setPen(TEXT_COL)
            f2 = QFont(); f2.setPointSize(9); painter.setFont(f2)
            fm = QFontMetrics(f2)
            summary = fm.elidedText(summary_txt, Qt.ElideRight, NODE_W - 20)
            painter.drawText(QRectF(10, HEADER_H + 4, NODE_W - 20, 22),
                             Qt.AlignLeft | Qt.AlignVCenter, summary)

        # The image the node is looking for, drawn where its filename used to be
        # the only clue. Sunken well behind it so a light screenshot still reads
        # as a picture inside the node instead of a hole in it.
        if self.thumb_path:
            well = QRectF(THUMB_LEFT, THUMB_TOP, THUMB_W, THUMB_H)
            painter.setPen(QPen(NODE_BORDER, 1))
            painter.setBrush(QBrush(QColor("#1b1f2b")))
            painter.drawRoundedRect(well, 4, 4)
            painter.setBrush(Qt.NoBrush)
            px = node_thumbnail(self.thumb_path)
            if px is not None:
                tgt = QRectF(well.center().x() - px.width() / 2.0,
                             well.center().y() - px.height() / 2.0,
                             px.width(), px.height())
                painter.drawPixmap(tgt, px, QRectF(px.rect()))
            else:
                painter.setPen(QColor("#f59e0b"))
                fmiss = QFont(); fmiss.setPointSize(8); painter.setFont(fmiss)
                painter.drawText(well, Qt.AlignCenter, "missing")
            cap = thumb_caption(self.node)
            if cap:
                # 7 pt, and CAPTION_H stops short of the port-label band: at 8 pt
                # "click ×2" is 88 px in an 82 px column, and a second line would
                # land on top of "⚠ error".
                painter.setPen(SUB_COL)
                fc_ = QFont(); fc_.setPointSize(7); painter.setFont(fc_)
                painter.drawText(QRectF(CAPTION_LEFT, THUMB_TOP,
                                        NODE_W - CAPTION_LEFT - 10, CAPTION_H),
                                 Qt.AlignLeft | Qt.AlignVCenter, cap)

        # Output port labels, in their port's own colour — reading grey "false"
        # next to a red dot made you check twice which branch was which.
        f3 = QFont(); f3.setPointSize(8); f3.setBold(True); painter.setFont(f3)
        for name_, p in self._ports.items():
            if p.is_output and name_ != "out":
                y = p.pos().y()
                painter.setPen(PORT_COLORS.get(name_, SUB_COL))
                painter.drawText(QRectF(NODE_W - 84, y - 9, 72, 18),
                                 Qt.AlignRight | Qt.AlignVCenter,
                                 PORT_LABELS.get(name_, name_))
        # on-error / retry badge for action nodes
        if self.node.type == flow.N_ACTION:
            oe = self.node.data.get("on_error", {})
            badge = []
            if oe.get("retries"):
                badge.append(f"↻{oe['retries']}")
            mode = oe.get("mode")
            if mode and mode != "stop":
                badge.append(mode)
            if badge:
                painter.setPen(QColor("#f59e0b"))
                painter.drawText(QRectF(10, self._h - 18, NODE_W - 20, 16),
                                 Qt.AlignLeft, "  ".join(badge))

        # pre-delay badge (only nodes where the delay actually fires at runtime)
        ms = self.node.data.get("delay_before_ms")
        if ms and flow.delay_applies(self.node):
            painter.setPen(QColor("#22b8cf"))
            fd = QFont(); fd.setPointSize(8); painter.setFont(fd)
            painter.drawText(QRectF(10, self._h - 18, NODE_W - 20, 16),
                             Qt.AlignRight | Qt.AlignVCenter,
                             f"⏱ {flow.format_duration(ms)}")

        self._paint_progress(painter, path)

    def _paint_progress(self, painter: QPainter, path: QPainterPath):
        """The load bar along the node's bottom edge."""
        if self.progress is None:
            return
        painter.setClipPath(path)
        bar = QRectF(0, self._h - PROGRESS_H, NODE_W, PROGRESS_H)
        painter.fillRect(bar, TROUGH_COL)
        col = node_header_color(self.node)
        if self.progress == PROGRESS_UNKNOWN:
            # Nothing can say how long this takes — a Detect with no timeout,
            # a while-loop. A fill would be a number we do not have, so it
            # slides instead: motion means "working", width means nothing.
            w = NODE_W * 0.28
            x = (NODE_W + w) * self._shimmer_phase() - w
            painter.fillRect(QRectF(x, bar.top(), w, PROGRESS_H), col)
        else:
            frac = max(0.0, min(PROGRESS_CAP, float(self.progress)))
            painter.fillRect(QRectF(0, bar.top(), NODE_W * frac, PROGRESS_H), col)
        painter.setClipping(False)

    def _shimmer_phase(self) -> float:
        sc = self.scene()
        return getattr(sc, "shimmer_phase", 0.0) if sc is not None else 0.0


# ==============================================================================
#  Frame (comment box)
# ==============================================================================
FRAME_DEF_COL = QColor("#64748b")


class FrameItem(QGraphicsObject):
    """A titled box drawn behind the graph, which carries what stands on it.

    Two things about the hit area decide how this feels, and both are the
    opposite of what a plain rectangle would do:

    * **Only the title bar and the grip are clickable.** shape() returns those
      and not the fill, so the box is transparent to the mouse everywhere else
      — nodes inside stay clickable, a marquee still works over it, and empty
      space inside it still pans. A frame that swallowed clicks would make the
      region it is labelling harder to work in than the rest of the canvas,
      which is precisely backwards.
    * **It is dragged by its title, and it takes its contents with it.** That is
      the entire feature: a region of a flow becomes one thing you can move.
    """
    geometry_changed = Signal()

    TITLE_H = 26.0
    GRIP = 18.0
    # A note longer than this is elided rather than allowed to eat the box.
    NOTE_MAX_H = 66.0

    def __init__(self, node: "flow.FlowNode"):
        super().__init__()
        self.node = node
        self.setFlags(QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemSendsGeometryChanges)
        self.setPos(node.x, node.y)
        self.setAcceptHoverEvents(True)
        self._hdr_h = None
        self._sync_z()

    # -- geometry --
    def size(self):
        return flow.frame_size(self.node)

    def boundingRect(self) -> QRectF:
        w, h = self.size()
        return QRectF(0, 0, w, h)

    def _sync_z(self):
        # Always behind the wires (0) and the nodes (1), and bigger frames sit
        # behind smaller ones so a frame inside a frame reads as nested rather
        # than as one covering the other. Recomputed from the area on every
        # resize, so the stacking maintains itself and there is no separate
        # ordering to keep in step.
        w, h = self.size()
        self.setZValue(-5.0 - w * h / 1_000_000.0)

    def resize_to(self, w: float, h: float):
        self.prepareGeometryChange()
        flow.set_frame_size(self.node, _snap(w), _snap(h))
        self._hdr_h = None      # the note re-wraps at the new width
        self._sync_z()
        self.update()

    def grip_rect(self) -> QRectF:
        w, h = self.size()
        return QRectF(w - self.GRIP, h - self.GRIP, self.GRIP, self.GRIP)

    def header_h(self) -> float:
        """Title bar height, grown to hold the note if there is one.

        The note lives in the *header*, not loose inside the box. The interior
        is where nodes stand, and a frame is behind them — text drawn there is
        text with node-shaped holes in it. Growing the bar keeps the note fully
        readable and makes the grab handle bigger at the same time.
        """
        if self._hdr_h is not None:
            return self._hdr_h
        h = self.TITLE_H
        note = flow.frame_body(self.node)
        if note:
            w, _ = self.size()
            f = QFont(); f.setPointSize(9)
            box = QFontMetrics(f).boundingRect(
                QRect(0, 0, int(w) - 24, 10_000),
                int(Qt.TextWordWrap | Qt.AlignLeft), note)
            h += min(self.NOTE_MAX_H, box.height()) + 10
        self._hdr_h = h
        return h

    def title_rect(self) -> QRectF:
        w, _h = self.size()
        return QRectF(0, 0, w, self.header_h())

    def shape(self) -> QPainterPath:
        p = QPainterPath()
        p.addRect(self.title_rect())
        p.addRect(self.grip_rect())
        return p

    def contains_item(self, item: "NodeItem") -> bool:
        """A node is on this frame only if the frame holds *all* of it.

        Half in is not in. Carrying a node that merely overlaps the edge would
        pull it out of whatever it actually belongs to, and "which of these did
        I just move" is not a question a drag should leave you with.
        """
        return self.sceneBoundingRect().contains(item.sceneBoundingRect())

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange:
            return QPointF(_snap(value.x()), _snap(value.y()))
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.node.x = self.pos().x()
            self.node.y = self.pos().y()
            self.geometry_changed.emit()
        return super().itemChange(change, value)

    def refresh(self):
        self.prepareGeometryChange()
        self._hdr_h = None
        self._sync_z()
        self.update()

    # -- paint --
    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        r = self.boundingRect()
        col = node_tint(self.node) or FRAME_DEF_COL

        body = QPainterPath()
        body.addRoundedRect(r, 12, 12)
        # Deliberately faint. A frame is behind the whole flow, so anything
        # solid enough to read as a surface makes every node on it harder to
        # read than the nodes beside it.
        fill = QColor(col)
        fill.setAlpha(38 if not self.isSelected() else 58)
        painter.fillPath(body, QBrush(fill))
        painter.setPen(QPen(NODE_SEL if self.isSelected() else col,
                            2 if self.isSelected() else 1.5))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(body)

        # Header: the solid part, and the only part you can grab.
        hdr_h = self.header_h()
        bar = QPainterPath()
        # ⚠ Winding, not the default odd-even. The band is a rounded rect with a
        # plain rect laid over its bottom edge to square off the two lower
        # corners, and under odd-even the OVERLAP counts as outside — so the
        # bottom 12 px of the header was punched out and filled with the faint
        # body tint instead. On a 26 px bar that is nearly half of it, which is
        # what "the top of the comment node is half dark" was.
        bar.setFillRule(Qt.WindingFill)
        bar.addRoundedRect(QRectF(0, 0, r.width(), hdr_h), 12, 12)
        bar.addRect(QRectF(0, hdr_h - 12, r.width(), 12))
        painter.setClipPath(body)
        painter.fillPath(bar, QBrush(col))
        painter.setClipping(False)

        lod = (option.levelOfDetailFromTransform(painter.worldTransform())
               if option is not None else 1.0)
        if lod < NodeItem.LOD_TEXT:
            return

        title = flow.frame_title(self.node) or "Comment"
        f = QFont(); f.setBold(True); f.setPointSize(9)
        painter.setFont(f)
        painter.setPen(QColor("#0b0e14"))
        painter.drawText(
            QRectF(10, 0, r.width() - 20, self.TITLE_H),
            Qt.AlignVCenter | Qt.AlignLeft,
            QFontMetrics(f).elidedText(title, Qt.ElideRight, int(r.width()) - 20))

        note = flow.frame_body(self.node)
        if note:
            f2 = QFont(); f2.setPointSize(9)
            painter.setFont(f2)
            # Dark ink on the header band, like every other header in the app.
            painter.setPen(QColor("#0b0e14"))
            painter.drawText(
                QRectF(12, self.TITLE_H - 4, r.width() - 24,
                       hdr_h - self.TITLE_H),
                Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, note)

        # Resize grip: three strokes in the corner, the standard shorthand.
        g = self.grip_rect()
        painter.setPen(QPen(col, 2))
        for off in (0.0, 5.0, 10.0):
            painter.drawLine(QPointF(g.right() - 3 - off, g.bottom() - 3),
                             QPointF(g.right() - 3, g.bottom() - 3 - off))


# ==============================================================================
#  Edge
# ==============================================================================
class EdgeItem(QGraphicsPathItem):
    def __init__(self, edge: "flow.FlowEdge", scene: "FlowScene"):
        super().__init__()
        self.edge = edge
        self._scene = scene
        self.setZValue(0)
        self.setPen(QPen(EDGE_COL, 2))
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.update_path()

    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(14)
        return stroker.createStroke(self.path())

    # A route can only ever leave the box its two ends span by the width of one
    # turn-out plus the deepest lane the search will try, so a node further away
    # than this cannot change the answer. Feeding every node in the graph to
    # _route_points instead made one reroute O(nodes^2) — the lane set alone
    # grows two entries per node — which is most of what made a 150-node canvas
    # unusable. Keep this comfortably above the largest lane offset in
    # _route_points (380 + gap) or a wire may be drawn through a node it should
    # have cleared.
    ROUTE_REACH = 560.0

    def update_path(self):
        src = self._scene.node_item(self.edge.src)
        dst = self._scene.node_item(self.edge.dst)
        if not src or not dst:
            return
        p1 = src.port_pos(self.edge.src_port)
        p2 = dst.port_pos("in")
        if self._scene.fast_routing:
            # Mid-drag: show where the wire goes, pay nothing for it. The real
            # route is computed once, on release.
            self.setPath(_smooth_path(_quick_route(p1, p2)))
            self._apply_pen()
            return
        near = QRectF(p1, p2).normalized().adjusted(
            -self.ROUTE_REACH, -self.ROUTE_REACH,
            self.ROUTE_REACH, self.ROUTE_REACH)
        obstacles = [r for nid, r in self._scene.obstacles()
                     if nid != self.edge.src and nid != self.edge.dst
                     and r.intersects(near)]
        self.setPath(_smooth_path(_route_points(p1, p2, obstacles)))
        self._apply_pen()

    def itemChange(self, change, value):
        # An edge re-pens *itself* when its own selection changes. The scene
        # used to do it for all of them on every selectionChanged, which is one
        # signal per setSelected() call and a walk of every edge inside each —
        # so selecting a 500-node flow was 250,000 setPen calls and two thirds
        # of a second of frozen UI. This is the same work, done once per edge
        # that actually changed, and it needs no event loop to arrive, so
        # nothing has to be flushed before the colour is right.
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self._apply_pen()
        return super().itemChange(change, value)

    def _apply_pen(self):
        """Set just the pen (selected vs port colour) without re-routing the
        path — cheap enough to call on every selection change."""
        col = PORT_COLORS.get(self.edge.src_port, EDGE_COL)
        self.setPen(QPen(NODE_SEL if self.isSelected() else col,
                         3 if self.isSelected() else 2))


# ==============================================================================
#  Scene
# ==============================================================================
class FlowScene(QGraphicsScene):
    def __init__(self, graph: "flow.FlowGraph"):
        super().__init__()
        self.graph = graph
        self.setBackgroundBrush(QBrush(CANVAS_BG))
        self._nodes: Dict[str, NodeItem] = {}
        self._edges: Dict[str, EdgeItem] = {}
        # ⚠ Frames are deliberately NOT in _nodes. obstacles() is built from
        # _nodes, so a frame in there would become something every wire has to
        # route around — and a frame is a region of the graph, so every wire
        # inside it would be forced out of it. Wires cross frames freely.
        self._frames: Dict[str, FrameItem] = {}
        self._last_added = None
        self._highlighted = None
        # ── routing cost control ─────────────────────────────────────────
        # Rerouting one wire is an obstacle search; doing it per moved node per
        # mouse-move is what made dragging a selection through a large graph
        # crawl (measured: 543 ms for a single drag step at 150 nodes). Three
        # things keep it flat — the rects every route needs are built once and
        # kept until something actually moves, which edges touch a node is an
        # index rather than a scan of all of them, and a drag draws cheap wires
        # and reroutes for real on release.
        self._obstacles: Optional[List[tuple]] = None
        self._edge_index: Optional[Dict[str, List[str]]] = None
        self.fast_routing = False
        # Above ASYNC_ROUTE_MIN wires, the reroute that follows a drag (or an
        # open) is spread across frames instead of done in one blocking pass —
        # see _queue_routes. Below it, everything stays synchronous, so a normal
        # flow behaves exactly as it always has and no caller has to know this
        # exists.
        self._pending: List[str] = []
        self._pending_at = 0
        self._route_timer = QTimer(self)
        self._route_timer.setSingleShot(True)
        self._route_timer.setInterval(0)
        self._route_timer.timeout.connect(self._drain_routes)
        # ── run state ────────────────────────────────────────────────────
        # The bar is animated HERE, not streamed from the worker. The engine
        # drives the interpreter at ~692k events/sec in a tight loop, and a
        # progress signal per step is the 2.0.8 log flood rebuilt: the queue
        # fills four orders of magnitude faster than the GUI drains it. So the
        # worker says "node X started" once, the scene looks up how long X
        # should take (flow.estimate) and draws the rest itself. Per-step cost
        # of a running bar: zero.
        self._live: Dict[str, List[str]] = {}
        self._run_node: Optional[str] = None
        self._run_t0: float = 0.0
        self._run_ms: float = 0.0
        self.shimmer_phase = 0.0
        self._anim = QTimer(self)
        self._anim.setInterval(33)          # ~30 Hz; a bar needs no more
        self._anim.timeout.connect(self._tick)
        # No selectionChanged handler: an edge re-pens itself in
        # EdgeItem.itemChange. That also retires the teardown hazard the scene
        # -wide version carried, since nothing walks self._edges from a slot Qt
        # can fire part-way through clear().
        self.rebuild()

    def node_item(self, nid: str) -> Optional[NodeItem]:
        return self._nodes.get(nid)

    # ── routing caches ───────────────────────────────────────────────────
    def obstacles(self):
        """`[(node id, padded scene rect), …]`, rebuilt only when one moves."""
        if self._obstacles is None:
            self._obstacles = [
                (nid, it.sceneBoundingRect().adjusted(-6, -6, 6, 6))
                for nid, it in self._nodes.items()]
        return self._obstacles

    def _edges_of(self, nid: str) -> List[str]:
        if self._edge_index is None:
            idx: Dict[str, List[str]] = {}
            for eid, ei in self._edges.items():
                idx.setdefault(ei.edge.src, []).append(eid)
                if ei.edge.dst != ei.edge.src:
                    idx.setdefault(ei.edge.dst, []).append(eid)
            self._edge_index = idx
        return self._edge_index.get(nid, [])

    def set_fast_routing(self, on: bool):
        """While True, wires skip the obstacle search (see EdgeItem)."""
        self.fast_routing = bool(on)

    def reroute_nodes(self, nids):
        """Full reroute of every wire touching `nids`, one cache rebuild total.

        Going through _refresh_edges per node would drop the obstacle cache
        again for each one, which is the cost this exists to avoid.
        """
        self._obstacles = None
        seen = set()
        for nid in nids:
            seen.update(self._edges_of(nid))
        self._queue_routes(seen)

    # ── spreading a big reroute across frames ────────────────────────────
    # Under this many wires, route them all now. A small flow finishes inside
    # a frame either way, and staying synchronous means the paths are correct
    # the instant the call returns — which every caller and every test written
    # before this existed is entitled to assume.
    ASYNC_ROUTE_MIN = 150
    # How long one slice may run. Half a 60 Hz frame: long enough that the
    # queue drains in a handful of frames, short enough that the canvas still
    # redraws and the window still answers the mouse while it does.
    ROUTE_SLICE_S = 0.008

    def _queue_routes(self, eids):
        """Route these wires — now if there are few, over the next frames if
        there are many.

        Dropping a 1000-node selection used to freeze for a third of a second
        while every wire it touched was routed. The work is the same; what
        changes is that the canvas keeps drawing while it happens, and the
        wires you are looking at are put right first.
        """
        eids = [eid for eid in eids if eid in self._edges]
        if len(eids) < self.ASYNC_ROUTE_MIN:
            for eid in eids:
                ei = self._edges.get(eid)
                if ei is not None and isValid(ei):
                    ei.update_path()
            return
        # On screen first. A wire that is off in the dark can be wrong for
        # three more frames and nobody is any the wiser; the one under the
        # cursor cannot.
        view = QRectF()
        for v in self.views():
            view = view.united(v.mapToScene(v.viewport().rect()).boundingRect())
        if not view.isNull():
            eids.sort(key=lambda eid: not self._edges[eid]
                      .sceneBoundingRect().intersects(view))
        # Merge rather than replace: a second drag while the first is still
        # settling must not strand the wires the first one queued.
        rest = self._pending[self._pending_at:]
        self._pending = eids + [e for e in rest if e not in set(eids)]
        self._pending_at = 0
        self._drain_routes()

    def _drain_routes(self):
        if self.fast_routing:
            # Mid-drag: precise routes would only be thrown away on the next
            # mouse-move. Hold the queue and pick it up when the drag ends.
            self._route_timer.start()
            return
        t0 = time.perf_counter()
        pend, i, n = self._pending, self._pending_at, len(self._pending)
        while i < n:
            ei = self._edges.get(pend[i])
            i += 1
            if ei is not None and isValid(ei):
                ei.update_path()
            if time.perf_counter() - t0 >= self.ROUTE_SLICE_S:
                break
        self._pending_at = i
        if i < n:
            self._route_timer.start()
        else:
            self._pending, self._pending_at = [], 0

    def flush_routes(self):
        """Finish any queued routing right now.

        For the few things that need the wires to be final rather than nearly
        final — saving a picture of the canvas, or a test that would otherwise
        be asserting against a route three frames early.
        """
        self._route_timer.stop()
        pend, self._pending = self._pending[self._pending_at:], []
        self._pending_at = 0
        for eid in pend:
            ei = self._edges.get(eid)
            if ei is not None and isValid(ei):
                ei.update_path()

    def rebuild(self):
        last = self._last_added
        # A rebuild throws the items away, so whatever was lit no longer is —
        # forget it, or re-lighting the same node would be skipped as a no-op.
        self._highlighted = None
        # Drop the lookups BEFORE clear(), not after. QGraphicsScene.clear()
        # destroys the C++ items, and deselecting them on the way out emits
        # selectionChanged *synchronously* — straight into
        # _on_selection_changed, which re-pens every edge in self._edges. Clear
        # these afterwards and that slot walks wrappers whose C++ object is
        # already gone, which is a shiboken RuntimeError, not a Qt warning.
        # Raised inside a slot called from C++ it never reaches the caller
        # either: it goes to sys.excepthook, so the canvas kept working and
        # nothing failed a test. Only the 2.0.9 crash reporter ever saw it.
        self._nodes.clear()
        self._edges.clear()
        self._frames.clear()
        self._obstacles = None
        self._edge_index = None
        self._pending, self._pending_at = [], 0
        self.clear()
        for node in self.graph.nodes.values():
            self._add_node_item(node)
        # Build the wires cheap, then queue the real routing. Opening a large
        # script used to route every wire before the window could paint once,
        # so the flow appeared all at once, late. Now it appears immediately
        # with approximate wires and they straighten over the next few frames,
        # nearest the viewport first. Under ASYNC_ROUTE_MIN wires _queue_routes
        # is synchronous, so a normal flow is finished before this returns.
        was_fast, big = self.fast_routing, len(self.graph.edges) >= self.ASYNC_ROUTE_MIN
        if big:
            self.fast_routing = True
        for edge in self.graph.edges:
            self._add_edge_item(edge)
        if big:
            self.fast_routing = was_fast
            self._queue_routes(list(self._edges))
        # A rebuild is a redraw, not a reset: the auto-chain anchor survives if
        # its node does. It has to — saving a step can change which ports a node
        # has, which forces a rebuild in the middle of adding that very node.
        self._last_added = last if last in self._nodes else None
        # Same for the held stripe. The keys are still down whatever the canvas
        # did — throwing the marks away with the items would say they came up.
        live, self._live = self._live, {}
        self.set_live(live)

    def _add_node_item(self, node):
        if node.type == flow.N_FRAME:
            item = FrameItem(node)
            self.addItem(item)
            self._frames[node.id] = item
            return item
        item = NodeItem(node)
        item.geometry_changed.connect(lambda nid=node.id: self._refresh_edges(nid))
        self.addItem(item)
        self._nodes[node.id] = item
        self._obstacles = None
        return item

    # ── frames ───────────────────────────────────────────────────────────
    def frame_item(self, nid: str) -> Optional[FrameItem]:
        return self._frames.get(nid)

    def frame_items(self):
        return list(self._frames.values())

    def add_frame(self, x: float, y: float, w: float = None,
                  h: float = None, text: str = "") -> FrameItem:
        node = self.graph.add_node(
            flow.N_FRAME,
            {"text": text,
             "w": w if w is not None else flow.FRAME_DEF_W,
             "h": h if h is not None else flow.FRAME_DEF_H},
            _snap(x), _snap(y))
        return self._add_node_item(node)

    def nodes_on_frame(self, nid: str) -> List[NodeItem]:
        """The node items a frame would carry if it were dragged now.

        Recomputed at the moment of the drag rather than stored on the frame:
        membership is "where things are", and a node dragged out of a frame has
        left it — no bookkeeping, nothing to get out of step with the picture.
        """
        item = self._frames.get(nid)
        if item is None:
            return []
        return [n for n in self._nodes.values() if item.contains_item(n)]

    def _add_edge_item(self, edge):
        item = EdgeItem(edge, self)
        self.addItem(item)
        self._edges[edge.id] = item
        self._edge_index = None
        return item

    def _refresh_edges(self, nid: str):
        # This node moved, so every cached rect that mentions it is stale.
        self._obstacles = None
        for eid in self._edges_of(nid):
            ei = self._edges.get(eid)
            if ei is not None and isValid(ei):
                ei.update_path()

    def add_node(self, ntype: str, x: float, y: float, data=None) -> NodeItem:
        # Pure add (grid-snapped). Auto-place + auto-connect for interactive
        # palette adds lives in MainWindow._add_node so the recorder, which also
        # calls this, keeps its own layout/wiring.
        node = self.graph.add_node(ntype, data or {}, _snap(x), _snap(y))
        return self._add_node_item(node)

    def delete_node(self, nid: str):
        if nid in self._frames:
            # The frame only. Deleting a comment box is "I don't need this
            # label", never "delete this part of my flow" — and the second
            # meaning would be an unrecoverable click on the first one's
            # gesture. What was inside it stays where it is.
            self.removeItem(self._frames.pop(nid))
            self.graph.remove_node(nid)
            return
        if self._nodes.get(nid) and self._nodes[nid].node.type == flow.N_START:
            return  # never delete the single entry point
        if self._nodes.get(nid) and self._nodes[nid].node.type == flow.N_REROUTE:
            # A bend is deleted to stop bending the wire, never to cut it —
            # there is no other reason to have put one there. Cutting is still
            # one click away: select the wire itself and delete that.
            self._dissolve_reroute(nid)
            return
        for ei in [e for e in self._edges.values()
                   if e.edge.src == nid or e.edge.dst == nid]:
            self.removeItem(ei)
            self._edges.pop(ei.edge.id, None)
        if nid in self._nodes:
            self.removeItem(self._nodes[nid])
            self._nodes.pop(nid, None)
        self._obstacles = None
        self._edge_index = None
        self.graph.remove_node(nid)

    def _dissolve_reroute(self, nid: str):
        """Take the bend out and put the wire back through where it was.

        Re-points the surviving EdgeItems rather than calling rebuild(): the
        Delete key walks a list of selected items and deletes them one by one,
        and a rebuild part-way through that loop leaves the caller holding
        wrappers whose C++ item is already gone.
        """
        incoming = [e.id for e in self.graph.edges if e.dst == nid]
        onward = next((e.id for e in self.graph.edges if e.src == nid), None)
        if not flow.dissolve_reroute(self.graph, nid):
            return
        if onward and onward in self._edges:
            self.removeItem(self._edges.pop(onward))
        if nid in self._nodes:
            self.removeItem(self._nodes.pop(nid))
        self._obstacles = None
        self._edge_index = None
        # The incoming edges are the same FlowEdge objects, now pointing past
        # the node that used to be here — so they only need re-routing.
        for eid in incoming:
            ei = self._edges.get(eid)
            if ei is not None and isValid(ei):
                ei.update_path()

    def insert_reroute(self, edge_id: str, x: float, y: float) -> Optional[NodeItem]:
        """Split a wire around a new reroute node centred on (x, y)."""
        node = flow.insert_reroute(self.graph, edge_id,
                                   _snap(x - REROUTE_W / 2.0),
                                   _snap(y - REROUTE_W / 2.0))
        if node is None:
            return None
        item = self._add_node_item(node)
        onward = self.graph.out_edge(node.id, "out")
        if onward is not None:
            self._add_edge_item(onward)
        first = self._edges.get(edge_id)
        if first is not None and isValid(first):
            first.update_path()
        return item

    def delete_edge(self, eid: str):
        if eid in self._edges:
            self.removeItem(self._edges[eid])
            self._edges.pop(eid, None)
        self._edge_index = None
        self.graph.remove_edge(eid)

    def connect_ports(self, src_id: str, src_port: str, dst_id: str,
                      toggle: bool = False) -> bool:
        """Wire `src_port` to `dst_id`. Returns True if a wire now exists.

        With `toggle`, repeating the gesture on a pair that is already wired
        *removes* the wire instead of replacing it with an identical one. The
        replacement was a no-op, so the same drag that made a connection is the
        obvious way to take it back — no hunting for a thin curve to select.
        """
        if src_id == dst_id:
            return False
        existing = [e for e in self._edges.values()
                    if e.edge.src == src_id and e.edge.src_port == src_port]
        if toggle and any(e.edge.dst == dst_id for e in existing):
            for ei in existing:
                if ei.edge.dst == dst_id:
                    self.delete_edge(ei.edge.id)
            return False
        for ei in existing:
            self.removeItem(ei)
            self._edges.pop(ei.edge.id, None)
        self._edge_index = None
        edge = self.graph.add_edge(src_id, dst_id, src_port)
        self._add_edge_item(edge)
        return True

    def refresh_node(self, nid: str):
        item = self._nodes.get(nid)
        if item is None:
            return
        if item.out_ports != list(item.node.ports()):
            # The edit changed which outputs this node has (Detect gains an
            # "error" port, everything else loses it). Ports are built with the
            # item, so rebuild — after dropping any wire left hanging off a
            # port that no longer exists. (A changed image needs no rebuild:
            # the thumbnail lives inside the body the node already had, and
            # NodeItem.refresh() re-reads the path.)
            flow.prune_orphan_edges(self.graph)
            self.rebuild()
            return
        item.refresh()

    def node_names(self):
        return [n.data.get("name", "") for n in self.graph.nodes.values()
                if n.data.get("name")]

    def highlight(self, nid: Optional[str]):
        # Cheap when nothing moved. setSelected fires selectionChanged, which
        # re-pens every edge, so re-lighting the node that is already lit is
        # O(edges) of work for no visible change — and during playback this is
        # called once per node entered. (The opacity loop that used to run here
        # reset every node to 1.0; nothing ever set it to anything else.)
        if nid == self._highlighted:
            return
        self._highlighted = nid
        if nid and nid in self._nodes:
            self._nodes[nid].setSelected(True)

    # ── run state: the load bar and the still-held stripe ──────────────
    def begin_node(self, nid: Optional[str], speed: float = 1.0,
                   measured: Optional[dict] = None):
        """A node just started. Work out how long it should take and animate."""
        prev = self._run_node
        if prev and prev in self._nodes:
            self._nodes[prev].progress = None
            self._nodes[prev].update()
        self._run_node = nid
        self._run_t0 = time.perf_counter()
        node = self.graph.nodes.get(nid) if nid else None
        est = flow.estimate(node, speed, measured) if node is not None else None
        self._run_ms = float(est.ms) if est else 0.0
        if nid and nid in self._nodes:
            self._nodes[nid].progress = (0.0 if self._run_ms > 0
                                         else PROGRESS_UNKNOWN)
            self._nodes[nid].update()
        if nid and not self._anim.isActive():
            self._anim.start()

    def set_live(self, mapping: Dict[str, List[str]]):
        """node id -> key names it pressed that are still down."""
        mapping = {k: list(v) for k, v in (mapping or {}).items() if v}
        if mapping == self._live:
            return
        for nid in set(self._live) | set(mapping):
            item = self._nodes.get(nid)
            if item is None:
                continue
            item.live_keys = mapping.get(nid, [])
            item.setToolTip("Still held: " + "+".join(item.live_keys).upper()
                            if item.live_keys else "")
            item.update()
        self._live = mapping

    def end_run(self):
        self._anim.stop()
        self._run_node = None
        self.set_live({})
        for item in self._nodes.values():
            if item.progress is not None:
                item.progress = None
                item.update()

    def _tick(self):
        # 0.9 Hz sweep: fast enough to read as motion, slow enough that a whole
        # canvas of them is not a strobe.
        self.shimmer_phase = (self.shimmer_phase + 0.033 * 0.9) % 1.0
        item = self._nodes.get(self._run_node) if self._run_node else None
        if item is None:
            return
        if self._run_ms > 0:
            item.progress = (time.perf_counter() - self._run_t0) * 1000.0 / self._run_ms
            # A finished determinate bar has nothing left to animate; the
            # shimmer does, so only that keeps the timer honest.
            item.update()
        else:
            item.progress = PROGRESS_UNKNOWN
            item.update()


# ==============================================================================
#  View
# ==============================================================================
class FlowCanvas(QGraphicsView):
    node_edit_requested  = Signal(str)   # node id (double-click / context)
    node_error_requested = Signal(str)   # node id (action: error handling)
    add_node_requested   = Signal(str, float, float)  # type(:kind), x, y
    graph_changed        = Signal()

    def __init__(self, graph: "flow.FlowGraph"):
        super().__init__()
        self._scene = FlowScene(graph)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setSceneRect(-2000, -2000, 4000, 4000)
        self.setMinimumHeight(220)
        # Force the cosmic dark viewport (the global app stylesheet otherwise
        # leaves the QGraphicsView viewport painting the default Qt grey).
        self.setFrameShape(QFrame.NoFrame)
        self.setBackgroundBrush(QBrush(CANVAS_BG))
        self.setStyleSheet("QGraphicsView{background:#0d0c20;border:none;}")
        self.viewport().setStyleSheet("background:#0d0c20;")

        self._connecting: Optional[PortItem] = None
        self._temp_edge: Optional[QGraphicsPathItem] = None

        self._rconnect_src = None       # NodeItem
        self._rconnect_port = None      # str
        self._rconnect_press = None
        self._rconnect_started = False
        self._suppress_context = False

        self._panning = False
        self._pan_last = None
        self._pan_press = None    # where an empty-canvas press started
        self.setAcceptDrops(True)

        # Ctrl+drag marquee. A plain left-drag on empty canvas pans (which is
        # what a big flow wants most of the time), so the marquee takes the
        # modifier and works from anywhere, node or not.
        self._band = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._band_origin = None
        self.setToolTip("Drag to pan · click empty space to deselect · "
                        "Ctrl+drag to select a region · scroll to zoom · "
                        "right-drag a node to wire it, again to unwire")

        # node drag-to-delete / explicit move
        self._drag_node: Optional[NodeItem] = None
        self._drag_started = False
        self._drag_press = None
        self._drag_offsets: Dict[NodeItem, QPointF] = {}
        self._resize_frame: Optional["FrameItem"] = None
        self._trash = QLabel("🗑", self.viewport())
        self._trash.setAlignment(Qt.AlignCenter)
        self._trash.setFixedSize(58, 58)
        self._trash.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._set_trash_hover(False)
        self._trash.hide()

        # A flow can be longer than the default rect, and until 12 Aug 2026 the
        # rect only ever grew from a drag — so opening a long script left the
        # part past 2000 px unreachable until you nudged a node to make the
        # canvas notice it was there.
        self._grow_scene()

    # -- public API --
    @property
    def graph(self):
        return self._scene.graph

    def set_graph(self, graph):
        self._scene.graph = graph
        self._scene.rebuild()
        self._grow_scene()
        self.graph_changed.emit()

    def scene_(self) -> FlowScene:
        return self._scene

    def highlight(self, nid):
        self._scene.highlight(nid)

    # -- run state (pass-through; the scene owns the animation) --
    def begin_node(self, nid, speed: float = 1.0, measured: Optional[dict] = None):
        self._scene.begin_node(nid, speed, measured)

    def set_live(self, mapping):
        self._scene.set_live(mapping)

    def end_run(self):
        self._scene.end_run()

    # Fit shows you everything; it does not magnify. When the flow already fits
    # at its natural size there is nothing to gain by blowing it up, and one
    # case makes that obvious: a brand-new install is a single Start node, and
    # fitting it landed at **4.0** — past the wheel's own ZOOM_MAX of 3.0, as
    # the first thing anybody sees after downloading.
    FIT_MAX = 1.0

    def fit(self):
        """Frame the whole flow, zooming out as far as needed and in no further
        than `FIT_MAX`.

        ⚠ `fitInView` consults nothing. That is already documented at the other
        end of the range — see `min_zoom`, where a fixed wheel floor plus an
        unbounded `fit` made a big flow inescapable — and the same asymmetry
        sits at the top: nothing stopped Fit scaling past the maximum the wheel
        itself enforces.
        """
        items_rect = self._scene.itemsBoundingRect()
        if items_rect.isNull():
            return
        padded = items_rect.adjusted(-40, -40, 40, 40)
        self.fitInView(padded, Qt.KeepAspectRatio)
        if self.transform().m11() > self.FIT_MAX:
            self.setTransform(QTransform().scale(self.FIT_MAX, self.FIT_MAX))
            self.centerOn(items_rect.center())

    # -- trashcan overlay --
    _TRASH_NORMAL = ("background:#3a1d22; border:2px dashed #ef4444;"
                     "border-radius:15px; color:#ef4444; font-size:24px;")
    _TRASH_HOVER  = ("background:#ef4444; border:2px solid #ef4444;"
                     "border-radius:15px; color:#ffffff; font-size:27px;")

    def _set_trash_hover(self, over: bool):
        self._trash.setStyleSheet(self._TRASH_HOVER if over else self._TRASH_NORMAL)
        self._trash.setToolTip("Release here to delete the node")

    def _position_trash(self):
        m = 18
        self._trash.move(self.viewport().width() - self._trash.width() - m,
                         self.viewport().height() - self._trash.height() - m)

    def _show_trash(self):
        self._position_trash()
        self._trash.show()
        self._trash.raise_()

    def _hide_trash(self):
        self._trash.hide()

    def _trash_contains(self, view_pos) -> bool:
        return self._trash.isVisible() and self._trash.geometry().contains(view_pos)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._position_trash()

    # -- auto-grow scene so nodes never hit the boundary (#7) --
    _GROW_MARGIN = 1200

    def _grow_scene(self):
        items_r = self._scene.itemsBoundingRect()
        if items_r.isNull():
            return
        self._grow_to(items_r)

    def _grow_to(self, rect: QRectF):
        """Grow the scene rect around `rect`, if it isn't already inside it.

        The full _grow_scene() walks every item in the scene, which is why a
        drag calls this with just the rects it is actually moving.
        """
        if rect.isNull() or self.sceneRect().contains(rect):
            return
        m = self._GROW_MARGIN
        self.setSceneRect(self.sceneRect().united(
            rect.adjusted(-m, -m, m, m)))

    # -- zoom --
    ZOOM_MAX = 3.0
    # The floor when the flow is small enough not to need one. Below this a
    # five-node graph is a few specks in the middle of an empty viewport.
    ZOOM_MIN_DEFAULT = 0.25
    # Absolute floor. Only a genuinely enormous flow reaches it, and something
    # has to stop the scale approaching zero.
    ZOOM_MIN_HARD = 0.02

    def min_zoom(self) -> float:
        """The furthest out the wheel may go: far enough to see the whole flow.

        ⚠ A fixed floor is what made a big flow inescapable. `fit()` sets the
        transform through `fitInView`, which never consulted the wheel's 0.25 —
        so Fit could land at 0.08, and from there one notch of zoom-in was a
        one-way door: the wheel refused to go back below 0.25 and the rest of
        the flow was gone. The floor has to be derived from what is actually on
        the canvas, not written down.

        A little past the fitting scale, so "everything visible" is inside the
        range rather than exactly on its edge.
        """
        r = self._scene.itemsBoundingRect()
        vp = self.viewport().rect()
        if r.isNull() or r.width() <= 0 or r.height() <= 0 \
                or vp.width() <= 0 or vp.height() <= 0:
            return self.ZOOM_MIN_DEFAULT
        fit = min(vp.width() / r.width(), vp.height() / r.height())
        return max(self.ZOOM_MIN_HARD,
                   min(self.ZOOM_MIN_DEFAULT, fit * 0.8))

    def wheelEvent(self, e):
        factor = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        cur = self.transform().m11()
        if factor > 1:
            if cur > self.ZOOM_MAX:
                return
        elif cur < self.min_zoom():
            return
        self.scale(factor, factor)

    # -- drag nodes in from the palette --
    _NODE_MIME = "application/x-macronaut-node"

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(self._NODE_MIME):
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(self._NODE_MIME):
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(self._NODE_MIME):
            ntype = bytes(md.data(self._NODE_MIME)).decode()
            sp = self.mapToScene(e.pos())
            self.add_node_requested.emit(ntype, float(sp.x()), float(sp.y()))
            e.acceptProposedAction()
        else:
            super().dropEvent(e)

    # -- grid background --
    def drawBackground(self, painter, rect):
        # Paint our own cosmic fill (don't rely on the styled viewport bg).
        painter.fillRect(rect, CANVAS_BG)
        step, width = _grid_step(abs(painter.worldTransform().m11()))
        pen = QPen(GRID_COL, width)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        left = int(rect.left()) - (int(rect.left()) % step)
        top = int(rect.top()) - (int(rect.top()) % step)
        xs = range(left, int(rect.right()) + step, step)
        pts = [QPointF(x, y)
               for y in range(top, int(rect.bottom()) + step, step)
               for x in xs]
        if pts:
            painter.drawPoints(QPolygonF(pts))

    # -- helpers --
    def _port_at(self, view_pos) -> Optional[PortItem]:
        for item in self.items(view_pos):
            if isinstance(item, PortItem):
                return item
        return None

    def _node_at(self, view_pos) -> Optional[NodeItem]:
        for item in self.items(view_pos):
            if isinstance(item, NodeItem):
                return item
            if isinstance(item, PortItem):
                return item.node_item
        return None

    def _edge_at(self, view_pos) -> Optional[EdgeItem]:
        for item in self.items(view_pos):
            if isinstance(item, EdgeItem):
                return item
        return None

    def _frame_at(self, view_pos) -> Optional[FrameItem]:
        """The frame under the cursor — title bar or grip only.

        FrameItem.shape() is what makes that true, so this is an ordinary hit
        test: the body of a frame is not part of it as far as the mouse is
        concerned.
        """
        for item in self.items(view_pos):
            if isinstance(item, FrameItem):
                return item
        return None

    # -- connection dragging / node-drag detection --
    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            node = self._node_at(e.pos())
            outs = node.node.ports() if node else []
            if node is not None and outs:
                self._rconnect_src = node
                self._rconnect_port = outs[0]
                self._rconnect_press = e.pos()
                self._rconnect_started = False
            e.accept()
            return
        if e.button() == Qt.LeftButton and (e.modifiers() & Qt.ControlModifier):
            self._band_origin = e.pos()
            self._band.setGeometry(QRect(self._band_origin, self._band_origin))
            self._band.show()
            e.accept()
            return
        if e.button() == Qt.LeftButton:
            port = self._port_at(e.pos())
            if port and port.is_output:
                self._connecting = port
                self._temp_edge = QGraphicsPathItem()
                self._temp_edge.setPen(QPen(NODE_SEL, 2, Qt.DashLine))
                self._temp_edge.setZValue(5)
                self._scene.addItem(self._temp_edge)
                e.accept()
                return
            node = self._node_at(e.pos())
            if node is None:
                # After the node, not before: a node sitting on a frame's title
                # bar is still the thing you clicked on.
                frame = self._frame_at(e.pos())
                if frame is not None:
                    scene_pt = self.mapToScene(e.pos())
                    if frame.grip_rect().translated(frame.pos()).contains(scene_pt):
                        self._resize_frame = frame
                    else:
                        self._begin_frame_drag(frame, e)
                    e.accept()
                    return
                edge = self._edge_at(e.pos())
                if edge is not None:
                    self._scene.clearSelection()
                    edge.setSelected(True)
                    edge.update_path()
                    e.accept()
                    return
                self._panning = True
                self._pan_last = e.pos()
                self._pan_press = e.pos()
                self.setCursor(Qt.ClosedHandCursor)
                e.accept()
                return
            # pressing on a node — own the move/select; do NOT fall to super()
            self._drag_node = node
            self._drag_started = False
            self._drag_press = e.pos()
            if not node.isSelected():
                self._scene.clearSelection()
                node.setSelected(True)
            else:
                # The node was already selected, so nothing was cleared — but a
                # wire selected by an earlier click would still be highlighted,
                # which reads as "I clicked the node and it selected the wire".
                for it in self._scene.selectedItems():
                    if isinstance(it, EdgeItem):
                        it.setSelected(False)
            anchor = self.mapToScene(e.pos())
            self._drag_offsets = {}
            for it in self._scene.selectedItems():
                if isinstance(it, (NodeItem, FrameItem)):
                    self._drag_offsets[it] = it.scenePos() - anchor
                # A selected frame brings its contents even when it is only
                # part of a wider selection.
                if isinstance(it, FrameItem):
                    for inner in self._scene.nodes_on_frame(it.node.id):
                        self._drag_offsets.setdefault(
                            inner, inner.scenePos() - anchor)
            # ensure the pressed node is always in the offset table
            self._drag_offsets[node] = node.scenePos() - anchor
            e.accept()
            return
        super().mousePressEvent(e)

    def _begin_frame_drag(self, frame: FrameItem, e):
        """Grab a frame by its title, and everything standing on it with it.

        The carried nodes are put in the offset table but deliberately *not*
        selected: the selection is what a Delete or a colour change acts on,
        and moving a comment box must not silently arm either of those against
        the whole region it labels.
        """
        self._drag_node = frame
        self._drag_started = False
        self._drag_press = e.pos()
        if not frame.isSelected():
            self._scene.clearSelection()
            frame.setSelected(True)
        anchor = self.mapToScene(e.pos())
        self._drag_offsets = {frame: frame.scenePos() - anchor}
        for it in self._scene.selectedItems():
            if isinstance(it, (NodeItem, FrameItem)):
                self._drag_offsets[it] = it.scenePos() - anchor
        for it in self._scene.nodes_on_frame(frame.node.id):
            self._drag_offsets.setdefault(it, it.scenePos() - anchor)

    def mouseMoveEvent(self, e):
        if self._resize_frame is not None and (e.buttons() & Qt.LeftButton):
            f = self._resize_frame
            p = self.mapToScene(e.pos()) - f.pos()
            f.resize_to(p.x(), p.y())
            self._grow_to(f.sceneBoundingRect())
            e.accept()
            return
        if self._band_origin is not None:
            self._band.setGeometry(QRect(self._band_origin, e.pos()).normalized())
            e.accept()
            return
        if self._panning and self._pan_last is not None:
            delta = e.pos() - self._pan_last
            self._pan_last = e.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())
            e.accept()
            return
        if self._connecting and self._temp_edge:
            p1 = self._connecting.scenePos()
            p2 = self.mapToScene(e.pos())
            dx = max(40, abs(p2.x() - p1.x()) * 0.5)
            path = QPainterPath(p1)
            path.cubicTo(p1 + QPointF(dx, 0), p2 - QPointF(dx, 0), p2)
            self._temp_edge.setPath(path)
            e.accept()
            return
        if self._rconnect_src is not None and (e.buttons() & Qt.RightButton):
            if (not self._rconnect_started and self._rconnect_press is not None and
                    (e.pos() - self._rconnect_press).manhattanLength() >= 6):
                self._rconnect_started = True
                self._temp_edge = QGraphicsPathItem()
                self._temp_edge.setPen(QPen(NODE_SEL, 2, Qt.DashLine))
                self._temp_edge.setZValue(5)
                self._scene.addItem(self._temp_edge)
            if self._rconnect_started and self._temp_edge:
                p1 = self._rconnect_src.port_pos(self._rconnect_port)
                p2 = self.mapToScene(e.pos())
                dx = max(40, abs(p2.x() - p1.x()) * 0.5)
                path = QPainterPath(p1)
                path.cubicTo(p1 + QPointF(dx, 0), p2 - QPointF(dx, 0), p2)
                self._temp_edge.setPath(path)
            e.accept()
            return
        if self._drag_node is not None and (e.buttons() & Qt.LeftButton):
            if (not self._drag_started and self._drag_press is not None and
                    (e.pos() - self._drag_press).manhattanLength() >= 6):
                self._drag_started = True
                # Wires go cheap for the length of the drag and are routed for
                # real on release. Nothing else about a drag scales with the
                # size of the graph; the obstacle search does, quadratically.
                self._scene.set_fast_routing(True)
                self._show_trash()
            if self._drag_started:
                cur = self.mapToScene(e.pos())
                moved = QRectF()
                for it, off in self._drag_offsets.items():
                    np = cur + off
                    it.setPos(np.x(), np.y())   # itemChange snaps to grid
                    moved = moved.united(it.sceneBoundingRect())
                self._set_trash_hover(self._trash_contains(e.pos()))
                # Only what is moving can reach the boundary, so don't measure
                # the whole scene on every mouse-move to find that out.
                self._grow_to(moved)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._resize_frame is not None:
            self._resize_frame = None
            self._grow_scene()
            self.graph_changed.emit()
            e.accept()
            return
        if self._band_origin is not None:
            rect = QRect(self._band_origin, e.pos()).normalized()
            self._band_origin = None
            self._band.hide()
            if rect.width() < 4 and rect.height() < 4:
                # Ctrl+click, not a drag: add/remove one node from the selection.
                node = self._node_at(e.pos())
                if node is not None:
                    node.setSelected(not node.isSelected())
                else:
                    self._scene.clearSelection()
            else:
                self.select_in_rect(self.mapToScene(rect).boundingRect())
            e.accept()
            return
        if self._panning:
            dragged = (self._pan_press is None or
                       (e.pos() - self._pan_press).manhattanLength() >= 4)
            self._panning = False
            self._pan_last = None
            self._pan_press = None
            self.setCursor(Qt.ArrowCursor)
            if not dragged:
                # A click on empty canvas that never became a pan means "never
                # mind" — drop the selection, which is what every other editor
                # does and what people reach for after a marquee.
                self._scene.clearSelection()
            e.accept()
            return
        if self._connecting:
            target = self._node_at(e.pos())
            if self._temp_edge:
                self._scene.removeItem(self._temp_edge)
                self._temp_edge = None
            if target and target.node.id != self._connecting.node_item.node.id:
                src_id = self._connecting.node_item.node.id
                self._scene.connect_ports(src_id, self._connecting.name,
                                          target.node.id, toggle=True)
                self._maybe_promote_detect(src_id)
                self.graph_changed.emit()
            self._connecting = None
            e.accept()
            return
        if self._drag_node is not None:
            dn, started = self._drag_node, self._drag_started
            moved_ids = [it.node.id for it in self._drag_offsets]
            self._drag_node = None
            self._drag_started = False
            self._drag_press = None
            self._drag_offsets = {}
            self._scene.set_fast_routing(False)
            drop_on_trash = started and self._trash_contains(e.pos())
            self._hide_trash()
            if drop_on_trash:
                targets = {dn}
                for it in self._scene.selectedItems():
                    if isinstance(it, NodeItem):
                        targets.add(it)
                for it in targets:
                    self._scene.delete_node(it.node.id)
                self.graph_changed.emit()
                e.accept()
                return
            if started:
                # The wires have been drawn straight since the drag began; this
                # is where they find their way around the nodes again.
                self._scene.reroute_nodes(moved_ids)
            self._grow_scene()
        if self._rconnect_src is not None and e.button() == Qt.RightButton:
            started = self._rconnect_started
            src, port = self._rconnect_src, self._rconnect_port
            self._rconnect_src = None
            self._rconnect_started = False
            if self._temp_edge:
                self._scene.removeItem(self._temp_edge)
                self._temp_edge = None
            if started:
                target = self._node_at(e.pos())
                if target and target.node.id != src.node.id:
                    self._scene.connect_ports(src.node.id, port, target.node.id,
                                              toggle=True)
                    self._maybe_promote_detect(src.node.id)
                    self.graph_changed.emit()
                self._suppress_context = True   # we dragged -> swallow the menu
                e.accept()
                return
        super().mouseReleaseEvent(e)

    # -- editing --
    def mouseDoubleClickEvent(self, e):
        node = self._node_at(e.pos())
        if node:
            if node.node.type == flow.N_REROUTE:
                # A reroute has nothing to edit, so the gesture that opens every
                # other node undoes this one instead — double-click the wire to
                # put a bend in, double-click the bend to take it out again.
                self._scene.delete_node(node.node.id)
                self.graph_changed.emit()
            else:
                self.node_edit_requested.emit(node.node.id)
            e.accept()
            return
        frame = self._frame_at(e.pos())
        if frame is not None:
            self.edit_frame_text(frame)
            e.accept()
            return
        edge = self._edge_at(e.pos())
        if edge is not None:
            self._add_reroute(edge.edge.id, self.mapToScene(e.pos()))
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    # -- frames --
    def frame_text_dialog(self, initial: str = "") -> QInputDialog:
        """The comment-text prompt, built but not run.

        Multi-line on purpose. The first line is the title in the bar, and
        anything after it is a note drawn under it — so one field covers both
        "label this region" and "explain what it does".

        ⚠ Built by hand rather than through QInputDialog.getMultiLineText,
        for one reason: the app's stylesheet sets `background: $bg` on **every**
        QWidget, and that rule matches subclasses. The editor QInputDialog makes
        for itself carries no object name, so it was painted the dialog's own
        background with no border — a comment box was asked for through a dialog
        holding a title, two buttons and *no visible field*. `textBody` is the
        name the Type step's editor already uses to opt into the input styling;
        the re-polish is what makes a name set after construction take effect.

        Separate from ask_frame_text so a test can look at it: exec() is modal,
        and this is exactly the kind of bug that only shows up when something
        renders it.
        """
        dlg = QInputDialog(self)
        dlg.setOption(QInputDialog.UsePlainTextEditForTextInput, True)
        dlg.setWindowTitle("Comment")
        dlg.setLabelText(
            "First line is the title; the rest is a note inside the box:")
        dlg.setTextValue(initial)
        for ed in dlg.findChildren(QPlainTextEdit):
            ed.setObjectName("textBody")
            ed.style().unpolish(ed)
            ed.style().polish(ed)
        dlg.resize(480, 320)
        return dlg

    def ask_frame_text(self, initial: str = "") -> Optional[str]:
        """Ask for a comment's text. None means cancelled."""
        dlg = self.frame_text_dialog(initial)
        if dlg.exec() != QDialog.Accepted:
            return None
        return dlg.textValue()

    def edit_frame_text(self, frame: FrameItem):
        text = self.ask_frame_text(frame.node.data.get("text", "") or "")
        if text is None:
            return
        frame.node.data["text"] = text
        frame.refresh()
        self.graph_changed.emit()

    def new_frame_at(self, x: float, y: float) -> Optional[FrameItem]:
        """Ask for the text, *then* place the box.

        The other way round leaves an unasked-for box on the canvas whenever the
        dialog is cancelled — and cancelling is exactly how you say you did not
        want one.
        """
        text = self.ask_frame_text()
        if text is None:
            return None
        return self.add_frame(x, y, text=text)

    def add_frame(self, x: float, y: float, text: str = "") -> FrameItem:
        item = self._scene.add_frame(x, y, text=text)
        self._scene.clearSelection()
        item.setSelected(True)
        self._grow_scene()
        self.graph_changed.emit()
        return item

    FRAME_PAD = 34.0

    def wrap_selection_in_frame(self) -> Optional[FrameItem]:
        """Put a comment box around whatever is selected, sized to fit it.

        This is the gesture the feature exists for — every other tool binds it
        to a single key (Unreal C, Blender Ctrl+J, n8n Shift+S) because the
        thing you want to label is almost always already selected.
        """
        rect = QRectF()
        for it in self._scene.selectedItems():
            if isinstance(it, NodeItem):
                rect = rect.united(it.sceneBoundingRect())
        if rect.isNull():
            return None
        rect = rect.adjusted(-self.FRAME_PAD,
                             -self.FRAME_PAD - FrameItem.TITLE_H,
                             self.FRAME_PAD, self.FRAME_PAD)
        item = self._scene.add_frame(rect.x(), rect.y(),
                                     rect.width(), rect.height())
        self._scene.clearSelection()
        item.setSelected(True)
        self._grow_scene()
        self.graph_changed.emit()
        return item

    def selected_frame_ids(self):
        return [it.node.id for it in self._scene.selectedItems()
                if isinstance(it, FrameItem)]

    def _add_reroute(self, edge_id: str, scene_pos: QPointF):
        item = self._scene.insert_reroute(edge_id, scene_pos.x(), scene_pos.y())
        if item is None:
            return None
        self._scene.clearSelection()
        item.setSelected(True)
        self._grow_scene()
        self.graph_changed.emit()
        return item

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            for item in list(self._scene.selectedItems()):
                if isinstance(item, (NodeItem, FrameItem)):
                    self._scene.delete_node(item.node.id)
                elif isinstance(item, EdgeItem):
                    self._scene.delete_edge(item.edge.id)
            self.graph_changed.emit()
            e.accept()
            return
        if (e.key() == Qt.Key_C and not e.modifiers()
                and self.selected_node_ids()):
            # Bare C, the Unreal binding. Ctrl+C is already Copy, and this is
            # the gesture the whole feature turns on.
            self.wrap_selection_in_frame()
            e.accept()
            return
        if e.modifiers() & Qt.ControlModifier:
            key = e.key()
            if key == Qt.Key_C:
                self.copy_selection(); e.accept(); return
            if key == Qt.Key_X:
                self.cut_selection(); e.accept(); return
            if key == Qt.Key_V:
                self.paste(); e.accept(); return
            if key == Qt.Key_D:
                self.duplicate_selection(); e.accept(); return
            if key == Qt.Key_A:
                for it in self._scene._nodes.values():
                    it.setSelected(True)
                for it in self._scene.frame_items():
                    it.setSelected(True)
                e.accept(); return
        super().keyPressEvent(e)

    # -- selection --
    def select_in_rect(self, rect: QRectF, additive: bool = False) -> int:
        """Select everything inside `rect` (scene coordinates). Returns the count.

        A node counts when the marquee touches it, which is how every canvas
        editor behaves. A connection counts when the marquee holds *all* of it,
        or when both of the nodes it joins were selected — the second rule is
        what makes dragging a box around a chunk of flow select the chunk's
        internal wiring too, instead of only the wires that happened to bow
        inside the box.
        """
        if not additive:
            self._scene.clearSelection()
        picked = set()
        for it in self._scene._nodes.values():
            if rect.intersects(it.sceneBoundingRect()):
                it.setSelected(True)
                picked.add(it.node.id)
        n = len(picked)
        for ei in self._scene._edges.values():
            if ((ei.edge.src in picked and ei.edge.dst in picked)
                    or rect.contains(ei.sceneBoundingRect())):
                ei.setSelected(True)
                n += 1
        # A frame counts only when the marquee holds all of it — the same rule
        # as a wire, and for the same reason. A frame is usually the biggest
        # thing on the canvas, so "touched" would pick one up on almost every
        # marquee ever drawn.
        for fi in self._scene.frame_items():
            if rect.contains(fi.sceneBoundingRect()):
                fi.setSelected(True)
                n += 1
        return n

    # -- Detect → If promotion --
    def _maybe_promote_detect(self, nid: str) -> bool:
        """A Detect node with both outputs wired is an If/Else in all but name.

        Rather than leave the user with a two-way branch dressed as an action,
        the node becomes a real If node the moment the second wire lands — same
        check, same position, both wires kept. Nothing happens for a Detect step
        that also clicks what it finds, since an If node cannot click.
        """
        node = self.graph.nodes.get(nid)
        if node is None or node.type != flow.N_ACTION:
            return False
        wired = {e.src_port for e in self.graph.edges if e.src == nid}
        if not {"out", "error"} <= wired:
            return False
        if not flow.convert_detect_to_if(self.graph, nid):
            return False
        self._scene.rebuild()
        return True

    # -- copy / paste --
    PASTE_OFFSET = GRID * 2   # far enough that the copy is obviously a new node

    def selected_node_ids(self):
        return [it.node.id for it in self._scene.selectedItems()
                if isinstance(it, NodeItem)]

    def selected_ids(self):
        """Everything a copy, a cut or a duplicate should act on.

        ⚠ Comment boxes belong here. They are selectable, Ctrl+A picks them up
        and a marquee that holds one picks it up — so leaving them out of this
        made Copy, Cut and Duplicate quietly drop whatever was selected but not
        a step. Select a region and paste it and the labels were gone; cut it
        and the boxes stayed behind over an empty stretch of canvas.
        """
        return self.selected_node_ids() + self.selected_frame_ids()

    def copy_selection(self) -> int:
        """Put the selection on the system clipboard. Returns how many items."""
        payload = flow.copy_subgraph(self._scene.graph, self.selected_ids())
        if not payload["nodes"]:
            return 0
        QGuiApplication.clipboard().setText(json.dumps(payload))
        return len(payload["nodes"])

    def cut_selection(self) -> int:
        n = self.copy_selection()
        if n:
            for nid in self.selected_ids():
                self._scene.delete_node(nid)
            self.graph_changed.emit()
        return n

    def _clipboard_payload(self):
        try:
            payload = json.loads(QGuiApplication.clipboard().text() or "")
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) and flow.CLIP_KEY in payload else None

    def can_paste(self) -> bool:
        return self._clipboard_payload() is not None

    def paste(self) -> int:
        payload = self._clipboard_payload()
        if payload is None:
            return 0
        return self._paste_payload(payload)

    def duplicate_selection(self) -> int:
        """Copy + paste in one step, without touching the system clipboard —
        so duplicating a node doesn't cost you whatever you had copied."""
        payload = flow.copy_subgraph(self._scene.graph, self.selected_ids())
        return self._paste_payload(payload) if payload["nodes"] else 0

    def _paste_payload(self, payload) -> int:
        off = self.PASTE_OFFSET
        new_ids = flow.paste_subgraph(self._scene.graph, payload, off, off)
        if not new_ids:
            return 0
        self._scene.rebuild()
        # Select the copies, not the originals: the next drag (or paste) should
        # act on what was just added.
        self._scene.clearSelection()
        for nid in new_ids:
            item = self._scene.node_item(nid) or self._scene.frame_item(nid)
            if item:
                item.setSelected(True)
        # Chain further palette adds after the pasted block, not after whatever
        # was added before it — the last pasted *step*, since a comment box has
        # no ports and nothing can be chained onto it.
        steps = [nid for nid in new_ids if nid in self._scene._nodes]
        self._scene._last_added = steps[-1] if steps else None
        self._grow_scene()
        self.graph_changed.emit()
        return len(new_ids)

    def _rename_node(self, node_item: "NodeItem"):
        cur = node_item.node.data.get("name", "")
        name, ok = QInputDialog.getText(
            self, "Name node",
            "Node name (lets a Go to / error-recovery jump here):", text=cur)
        if ok:
            node_item.node.data["name"] = name.strip()
            node_item.refresh()
            self.graph_changed.emit()

    def _colour_menu(self, parent: QMenu, suffix: str = "") -> QMenu:
        """The tint submenu, shared by the node menu and the frame menu."""
        m = QMenu(f"🎨  Colour{suffix}", parent)
        m.addAction("Default (by type)", lambda: self._set_color(None))
        m.addSeparator()
        for name, hexv in NODE_TINTS:
            act = m.addAction(name, lambda h=hexv: self._set_color(h))
            act.setIcon(color_swatch(hexv))
        return m

    def _set_color(self, hexv: Optional[str]):
        """Tint every selected node (and frame). None clears back to the type's.

        Acts on the selection rather than the one node under the cursor, for the
        same reason Duplicate does: colour is almost always applied to a group,
        and having to repeat it eight times is how people stop bothering.
        """
        touched = 0
        for nid in self.selected_node_ids() + self.selected_frame_ids():
            node = self._scene.graph.nodes.get(nid)
            if node is None:
                continue
            if hexv:
                node.data["color"] = hexv
            else:
                node.data.pop("color", None)
            item = (self._scene.node_item(nid)
                    or self._scene.frame_item(nid))
            if item is not None:
                item.refresh()
            touched += 1
        if touched:
            self.graph_changed.emit()

    def _delay_applies(self, node: "flow.FlowNode") -> bool:
        """Pre-delay is offered on every node except Start and click-type actions."""
        return flow.delay_applies(node)

    def _set_delay(self, node_item: "NodeItem"):
        # Fallback 0, not 500: a node without the key has no delay, so opening
        # this dialog must show what the node actually does. Offering 500 made
        # "just checking the value" turn into "accidentally added half a second".
        cur = int(node_item.node.data.get("delay_before_ms", 0) or 0)
        ms, ok = QInputDialog.getInt(
            self, "Delay before",
            "Pause before this node runs (milliseconds):", cur, 0, 600000, 50)
        if ok:
            if ms > 0:
                node_item.node.data["delay_before_ms"] = ms
            else:
                node_item.node.data.pop("delay_before_ms", None)
            node_item.refresh()
            self.graph_changed.emit()

    # -- right-click: add nodes / edit / rename / delete --
    def contextMenuEvent(self, e):
        if self._suppress_context:
            self._suppress_context = False
            e.accept()
            return
        scene_pos = self.mapToScene(e.pos())
        edge = self._edge_at(e.pos())
        if edge is not None:
            menu = QMenu(self)
            menu.addAction("•  Add reroute point\tdouble-click",
                           lambda eid=edge.edge.id, p=scene_pos: self._add_reroute(eid, p))
            menu.addAction("🗑  Delete connection",
                           lambda eid=edge.edge.id: (self._scene.delete_edge(eid),
                                                     self.graph_changed.emit()))
            menu.exec(e.globalPos())
            return
        node = self._node_at(e.pos())
        menu = QMenu(self)
        if node is None:
            frame = self._frame_at(e.pos())
            if frame is not None:
                if not frame.isSelected():
                    self._scene.clearSelection()
                    frame.setSelected(True)
                menu.addAction("✏  Edit text…\tdouble-click",
                               lambda: self.edit_frame_text(frame))
                menu.addMenu(self._colour_menu(menu))
                menu.addSeparator()
                menu.addAction("⧉  Duplicate\tCtrl+D", self.duplicate_selection)
                menu.addAction("📋  Copy\tCtrl+C", self.copy_selection)
                menu.addSeparator()
                menu.addAction("🗑  Delete comment (keeps what's inside)",
                               lambda: (self._scene.delete_node(frame.node.id),
                                        self.graph_changed.emit()))
                menu.exec(e.globalPos())
                return
        if node:
            # Right-clicking a node outside the selection acts on that node —
            # otherwise Copy would silently copy something else entirely.
            if not node.isSelected():
                self._scene.clearSelection()
                node.setSelected(True)
            n_sel = len(self.selected_node_ids())
            suffix = f" ({n_sel} nodes)" if n_sel > 1 else ""
            if node.node.type == flow.N_REROUTE:
                # Nothing to edit, nothing to name, nothing to delay. Offering
                # those anyway would be four dead entries out of five.
                menu.addAction("↔  Remove bend (rejoin the wire)",
                               lambda: (self._scene.delete_node(node.node.id),
                                        self.graph_changed.emit()))
                menu.addMenu(self._colour_menu(menu, suffix))
                menu.exec(e.globalPos())
                return
            menu.addAction("✏  Edit…", lambda: self.node_edit_requested.emit(node.node.id))
            menu.addAction("🏷  Name…", lambda: self._rename_node(node))
            menu.addMenu(self._colour_menu(menu, suffix))
            menu.addSeparator()
            menu.addAction(f"⧉  Duplicate{suffix}\tCtrl+D", self.duplicate_selection)
            menu.addAction(f"📋  Copy{suffix}\tCtrl+C", self.copy_selection)
            if self.can_paste():
                menu.addAction("📌  Paste\tCtrl+V", self.paste)
            menu.addSeparator()
            if self._delay_applies(node.node):
                menu.addAction("⏱  Delay before…", lambda: self._set_delay(node))
            if node.node.type == flow.N_ACTION:
                menu.addAction("⚠  Error handling…",
                               lambda: self.node_error_requested.emit(node.node.id))
            if node.node.type != flow.N_START:
                menu.addAction("🗑  Delete node",
                               lambda: (self._scene.delete_node(node.node.id),
                                        self.graph_changed.emit()))
            menu.exec(e.globalPos())
            return

        add = menu.addMenu("➕  Add node")
        items = [
            ("🖱  Click",     "action:click"),
            ("⌨  Type",      "action:key"),
            ("⏱  Wait",      "action:wait"),
            ("🔍  Detect",    "action:wait_image"),
            ("❓  If / Else",  flow.N_IF),
            ("🔁  Loop",      flow.N_LOOP),
            ("↪️  Go to",      flow.N_GOTO),
            ("⏹  End",        flow.N_END),
        ]
        for label, ntype in items:
            add.addAction(label,
                          lambda t=ntype: self.add_node_requested.emit(
                              t, scene_pos.x(), scene_pos.y()))
        menu.addSeparator()
        if self.selected_node_ids():
            menu.addAction("💬  Comment around selection\tC",
                           self.wrap_selection_in_frame)
        menu.addAction("💬  Comment box",
                       lambda p=scene_pos: self.new_frame_at(p.x(), p.y()))
        if self.can_paste():
            menu.addAction("📌  Paste\tCtrl+V", self.paste)
        menu.addAction("Fit to view", self.fit)
        menu.exec(e.globalPos())
