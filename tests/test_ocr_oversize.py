"""
Screens too wide for the OCR engine.

Windows.Media.Ocr refuses any bitmap whose longest side exceeds
`OcrEngine.MaxImageDimension` — 10000 pixels. "Refuses" is the problem: it
does not raise and it does not read part of the image, it returns an empty
result. One pixel over the line and every word on the screen disappears.

Measured 8 September 2026, a 400px-tall strip with "Save changes" in 16px
Segoe UI, read through the live engine:

    width  9990   2 words   find_text FOUND
    width 10001   0 words   find_text none
    width 12000   0 words   find_text none

To a flow that is indistinguishable from "the text is not on the screen", so
Detect-text waits out its timeout and the If/Else takes the false branch — on a
desktop where the user can read the words perfectly well. Two ways in:

  * a native grab of a wide desktop. Three 3440px ultrawides side by side is
    10320 pixels; so is any four- or five-monitor row.
  * the zoom rescue added earlier the same day, which doubles the grab before
    re-reading it. That puts every desktop wider than 5000px over the limit —
    two 2560x1440 monitors, the single most ordinary two-screen setup there is.
    The rescue then costs ~700ms per poll to read nothing at all.

The fix is to scale an oversized image down to what the engine accepts and map
the boxes back, rather than hand it over and get silence.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ocr

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw, ImageFont


def _font(px):
    for name in ("segoeui.ttf", "tahoma.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except Exception:
            continue
    return ImageFont.load_default()


def _strip(width, height=400, text="Save changes", at=(60, 180), px=16):
    """A wide, mostly-empty screenshot with one phrase on it."""
    img = Image.new("RGB", (width, height), (245, 245, 245))
    ImageDraw.Draw(img).text(at, text, font=_font(px), fill=(10, 10, 10))
    return img


# ── The limit itself ─────────────────────────────────────────────────────────

def test_the_engine_declares_a_size_limit():
    """⚠ Wiring. The number lives in the WinRT API, not in our heads; an engine
    that cannot say what it accepts cannot be protected from oversized input,
    and every test below would still pass by accident on a narrow screen."""
    eng = ocr.get_engine()
    lim = eng.max_dimension
    assert isinstance(lim, int)
    assert lim >= 0, "a negative limit would scale every image to nothing"
    if eng.name.startswith("Windows"):
        assert lim > 0, "the Windows engine has a hard MaxImageDimension"


def test_the_declared_limit_is_the_engines_real_one():
    """Not a constant we chose. If Microsoft ships a different cap, ours moves
    with it — a hardcoded 10000 that drifts high would silently reopen the bug
    this file exists for."""
    eng = ocr.get_engine()
    if not eng.name.startswith("Windows"):
        pytest.skip("Windows engine not active")
    root = None
    for mod in ("winrt.windows.media.ocr", "winsdk.windows.media.ocr"):
        try:
            root = __import__(mod, fromlist=["OcrEngine"])
            break
        except Exception:
            continue
    if root is None:
        pytest.skip("no WinRT projection importable")
    assert eng.max_dimension == int(root.OcrEngine.max_image_dimension)


def test_a_narrow_image_is_not_resized_at_all():
    """The guard must be free on every ordinary screen — this runs on every
    poll of every Detect-text step."""
    eng = ocr.get_engine()
    img = _strip(1920)
    fitted, scale = eng._fit(img)
    assert scale == 1.0
    assert fitted is img, "an image within the limit was copied for no reason"


# ── Reading past the limit ───────────────────────────────────────────────────

@pytest.mark.skipif(not ocr.ENABLED, reason="needs a working OCR engine")
def test_text_on_an_oversized_screen_is_still_found():
    """⚠ The bug. Before the fix this returned None: the engine was handed
    10400 pixels, said nothing, and the flow concluded the text was absent."""
    eng = ocr.get_engine()
    if not eng.max_dimension:
        pytest.skip("this engine has no size limit to exceed")
    over = eng.max_dimension + 400
    tm = eng.find_text("Save changes", _strip(over))
    assert tm is not None, (
        f"nothing found on a {over}px-wide screen; the engine's "
        f"{eng.max_dimension}px limit is still swallowing the whole read")


@pytest.mark.skipif(not ocr.ENABLED, reason="needs a working OCR engine")
def test_the_box_comes_back_in_screenshot_pixels_not_scaled_ones():
    """⚠ The half-fix that would pass the test above and still be wrong.

    Scaling the image down without scaling the answer back up reports the
    phrase at a fraction of its true x — and `_do_wait_text` *clicks* what it
    is handed. A 10400px screen scaled to fit reports x≈577 for a word that is
    at x≈600: close enough to look right in a test, 23 pixels wrong on screen.
    """
    eng = ocr.get_engine()
    if not eng.max_dimension:
        pytest.skip("this engine has no size limit to exceed")
    over = eng.max_dimension + 400
    at = (over - 900, 180)
    tm = eng.find_text("Save changes", _strip(over, at=at, px=22))
    assert tm is not None
    assert abs(tm.left - at[0]) <= 60, (
        f"found at x={tm.left}, planted at x={at[0]} — the box was not mapped "
        "back out of the scaled-down image")
    assert tm.left + tm.width <= over


@pytest.mark.skipif(not ocr.ENABLED, reason="needs a working OCR engine")
def test_words_and_regions_agree_on_an_oversized_screen():
    """Both entry points crop-then-recognise, and both have to fit. read_words
    feeds phrase matching, read_regions feeds the single-word fallback; fixing
    one and not the other leaves half of Detect-text blind."""
    eng = ocr.get_engine()
    if not eng.max_dimension:
        pytest.skip("this engine has no size limit to exceed")
    img = _strip(eng.max_dimension + 400, px=22)
    words = eng.read_words(img)
    lines = eng.read_regions(img)
    assert words, "read_words came back empty on an oversized screen"
    assert lines, "read_regions came back empty on an oversized screen"
    assert any("save" in w.text.lower() for w in words)


@pytest.mark.skipif(not ocr.ENABLED, reason="needs a working OCR engine")
def test_a_region_on_an_oversized_screen_is_cropped_before_fitting():
    """A search area is the cheap way out of this: crop first, and a wide
    desktop is back under the limit at full resolution. The offset still has to
    survive."""
    eng = ocr.get_engine()
    if not eng.max_dimension:
        pytest.skip("this engine has no size limit to exceed")
    over = eng.max_dimension + 400
    at = (over - 900, 180)
    img = _strip(over, at=at, px=16)
    tm = eng.find_text("Save changes", img,
                       region=(at[0] - 60, 140, 600, 120))
    assert tm is not None
    assert abs(tm.left - at[0]) <= 25, f"found at {tm.left}, planted at {at[0]}"


# ── The zoom rescue must not walk over the limit ─────────────────────────────

def test_the_rescue_never_enlarges_past_what_the_engine_accepts():
    """⚠ Wide and shallow is the shape that gets past the megapixel budget.

    A whole wide desktop is already too many pixels to enlarge, so `ZOOM_MAX_PX`
    turns the rescue off before the dimension limit is anywhere near — which is
    why the first version of this test was vacuous: it asserted `0 <= limit` and
    passed with the clamp deleted.

    A *search area* is what reaches the limit. Crop to a toolbar or a status bar
    across three monitors and it is 5760x190 — 1.1 megapixels, comfortably
    inside the budget, and 11520 pixels wide once doubled. Without the clamp
    that read comes back empty, so a Detect-text aimed at a strip somebody
    picked deliberately would be the one place the rescue never worked.
    """
    eng = ocr.get_engine()
    lim = eng.max_dimension
    if not lim:
        pytest.skip("this engine has no size limit to exceed")
    shapes = [(5760, 190), (5120, 240), (6400, 320), (10320, 200),
              (5120, 1440), (7680, 2160)]
    reached = 0
    for w, h in shapes:
        f = eng._zoom_factor(w, h)
        if f:
            reached += 1
        assert max(w, h) * f <= lim, (
            f"{w}x{h} rescued at x{f:.2f} -> {int(max(w, h) * f)}px, past the "
            f"engine's {lim}px limit; that read returns nothing")
    assert reached >= 3, (
        "every shape here had its rescue switched off by the megapixel budget "
        "before the dimension limit could apply — this test is not reaching "
        "the clamp it exists to check")


def test_an_ordinary_screen_still_gets_the_full_rescue():
    """The limit must not quietly cost the machines that were working. A single
    1080p screen doubled is 3840 — nowhere near the cap."""
    eng = ocr.get_engine()
    assert eng._zoom_factor(1920, 1080) == pytest.approx(2.0)
    assert eng._zoom_factor(1366, 768) == pytest.approx(2.0)
