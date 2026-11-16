"""
The scale ladder is a lookup table, not a search.

`DEFAULT_SCALES` is described as multi-scale matching, which invites the
assumption that a template captured at *some* other size will be found. It will
not. Measured 8 September 2026 by planting a pure LANCZOS resample of the
template — no re-rendering, so the only variable is scale — on a 1920x1080
synthetic desktop and asking `best_match`:

    ratio   in the ladder   score
    x0.500      yes         0.9833
    x0.571      --          0.5806
    x0.667      yes         0.9922
    x0.714      --          0.5700
    x0.750      yes         0.9931
    x1.000      yes         1.0000
    x1.143      --          0.5958
    x1.200      --          0.5775
    x1.250      yes         0.9893
    x1.400      --          0.5967
    x1.500      yes         0.9922
    x1.750      --          0.6130
    x2.000      yes         0.9906

Every rung scores ~0.99. Everything between them scores ~0.58, which is a miss
at any usable confidence — and an absent template scores 0.567-0.599 on the
same desktop, so the two are barely distinguishable. Sweeping the scale finely
shows why: the peak is about **±2.5% wide**, and it is not unimodal (a spurious
0.72 bump sits at x1.44 when the target is at x1.75), so no hill-climb from a
neighbouring rung can find it either.

That is a property of normalised cross-correlation rather than a bug. What IS
fixable is which rungs exist, because the ratios that occur in practice are a
known discrete set: Windows display scaling (100/125/150/175/200%) and browser
zoom (…90, 100, 110, 125, 150, 175, 200%). Of the twenty ordered pairs of
Windows scalings, the ladder covered eight.

⚠ This file pins the rungs that were added and — just as deliberately — names
the ones that were not. Adding all twelve missing ratios doubles the ladder and
doubles the cost of every miss (1832 -> 3679 ms at 1080p, 3639 -> 7226 ms on an
ultrawide), which is paid by every poll of a wait-for-image that has not
appeared yet. Four were added instead, for +28%.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matcher

pytestmark = pytest.mark.skipif(not matcher.ENABLED,
                                reason="needs opencv-python + Pillow")

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw, ImageFont

# Windows display scaling, and Chrome's zoom levels over the same range.
WINDOWS_SCALINGS = [1.0, 1.25, 1.5, 1.75, 2.0]
BROWSER_ZOOM = [0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]

# How far off a rung a target can sit and still be found. Measured, not chosen:
# the correlation peak falls from 0.93 to 0.52 between x1.750 and x1.794.
CAPTURE_WIDTH = 0.025


def _covered(ratio, ladder=None):
    ladder = matcher.DEFAULT_SCALES if ladder is None else ladder
    return any(abs(ratio - s) / ratio < CAPTURE_WIDTH for s in ladder)


def _button(w=120, h=34):
    img = Image.new("RGB", (w, h), (58, 110, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, h - 1], outline=(230, 236, 250))
    try:
        f = ImageFont.truetype("segoeui.ttf", 15)
    except Exception:
        f = ImageFont.load_default()
    d.text((12, 9), "Save", font=f, fill=(255, 255, 255))
    for i in range(0, w, 17):
        d.point((i, h // 2), fill=(250, 220, 60))
    return img


def _desktop(w=1920, h=1080):
    img = Image.new("RGB", (w, h), (36, 38, 44))
    d = ImageDraw.Draw(img)
    for i in range(80):
        d.rectangle([40 + (i % 7) * 260, 40 + (i // 7) * 100,
                     40 + (i % 7) * 260 + 200, 40 + (i // 7) * 100 + 54],
                    fill=(52, 56, 64))
    return img


def _find_at(ratio):
    """Plant a pure resample of the template at `ratio` and search for it."""
    tpl = _button()
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    tpl.save(path)
    try:
        shot = _desktop()
        big = tpl.resize((max(1, int(round(120 * ratio))),
                          max(1, int(round(34 * ratio)))), Image.LANCZOS)
        shot.paste(big, (900, 500))
        return matcher.best_match(path, screenshot=shot)
    finally:
        os.unlink(path)


# ── the rungs that were added ────────────────────────────────────────────────

@pytest.mark.parametrize("ratio,why", [
    (1.75, "Windows 100% -> 175%, and Chrome's 175% zoom"),
    (0.571, "Windows 175% -> 100%"),
    (1.20, "Windows 125% -> 150%, the two commonest non-100% scalings"),
    (0.833, "Windows 150% -> 125%"),
])
def test_a_common_scaling_ratio_has_a_rung(ratio, why):
    assert _covered(ratio), f"x{ratio} has no rung — {why}"


@pytest.mark.parametrize("ratio", [1.75, 0.571, 1.20, 0.833])
def test_a_template_at_that_ratio_is_actually_found(ratio):
    """⚠ Not the same assertion as the one above, and this is the pair that
    matters: a rung in the list is worthless if the search does not reach it,
    and a rung whose neighbours crowd it out would still be listed."""
    m = _find_at(ratio)
    assert m is not None, f"nothing found at x{ratio}"
    assert abs(m.left - 900) <= 6 and abs(m.top - 500) <= 6, \
        f"found at {(m.left, m.top)}, planted at (900, 500)"
    assert m.score >= 0.85, (
        f"x{ratio} scored {m.score:.4f} — a rung it should match exactly")


def test_every_browser_zoom_level_in_range_has_a_rung():
    """Chrome's zoom steps are a fixed list, and a template captured from a
    page at one zoom is searched for at another all the time."""
    missing = [z for z in BROWSER_ZOOM if not _covered(z)]
    assert not missing, f"no rung for browser zoom ratios {missing}"


# ── the rungs that were deliberately NOT added ───────────────────────────────

def test_the_uncovered_scaling_ratios_are_the_ones_we_think_they_are():
    """⚠ This test exists to keep an honest list, not to demand a fix.

    Eight of the twenty Windows scaling pairs are still uncovered, and adding
    them means doubling the ladder and so doubling the cost of every miss —
    which every poll of a wait-for-image pays. If someone later decides that
    trade is worth making, this is the list; if the ladder changes for another
    reason, this fails and the note gets revisited rather than silently rotting.
    """
    uncovered = sorted({round(b / a, 4)
                        for a in WINDOWS_SCALINGS for b in WINDOWS_SCALINGS
                        if not _covered(b / a)})
    assert uncovered == [0.625, 0.7143, 0.8571, 0.875, 1.1429, 1.1667, 1.4, 1.6], \
        f"the uncovered set moved: {uncovered}"


def test_the_ladder_still_leads_with_no_scaling():
    """The overwhelmingly common case is a template captured on this screen at
    this DPI, and `_EARLY_EXIT` turns that into one comparison instead of
    twenty-four. Any reordering has to keep 1.0 first."""
    assert matcher.DEFAULT_SCALES[0] == 1.0


def test_the_ladder_has_not_quietly_doubled():
    """⚠ The cost guard. A miss runs every rung in both colour and grey, and
    that is what a wait-for-image pays on every poll: 2005 ms at 1080p for 12
    rungs, 2563 for 16, 3679 for 24. Rungs are not free and must be argued for
    one at a time."""
    assert len(matcher.DEFAULT_SCALES) <= 16, (
        f"{len(matcher.DEFAULT_SCALES)} rungs — each one costs every miss, "
        "and misses are what polling is made of")


def test_no_two_rungs_are_close_enough_to_be_the_same_rung():
    """A rung within the capture width of another buys nothing and costs a full
    pass over the screen."""
    s = sorted(matcher.DEFAULT_SCALES)
    for a, b in zip(s, s[1:]):
        assert (b - a) / a >= CAPTURE_WIDTH, (
            f"x{a} and x{b} are within {CAPTURE_WIDTH:.1%} of each other")
