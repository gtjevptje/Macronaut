"""Reading the screen again, bigger, when reading it once found nothing.

The screenshot went to the OCR engine at native size, and screen UI text is
small — these engines are trained on document scans where a capital letter is
forty pixels tall rather than eleven. Measured over 120 readings of UI phrases
at 9-13 px in three fonts: 58 came back at native size, 98 at twice the size.

Most of this is tested against a fake engine that can only read large text,
because the mechanics — when the second read happens, and where its
coordinates end up — are the part that can be wrong in a way nobody notices.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ocr
from ocr import IOcrEngine


class _NeedsGlasses(IOcrEngine):
    """Reads one word, and only when the image has been enlarged.

    Stands in for the real failure: below a certain glyph size Windows OCR
    stops returning a word at all rather than returning it wrongly.
    """
    name = "fake"
    MIN_W = 400

    def __init__(self, word="Cancel", at=(30, 40), size=(50, 12)):
        self.word = word
        self.at = at
        self.size = size
        self.reads = []
        super().__init__()

    def _check_availability(self) -> bool:
        return True

    def _recognize(self, pil_img):
        self.reads.append(pil_img.size)
        if pil_img.width < self.MIN_W:
            return []
        # The fake is handed an image enlarged by an exact factor, so it
        # reports the word where the enlargement put it.
        f = pil_img.width / 200.0
        return [(int(self.at[0] * f), int(self.at[1] * f),
                 int(self.size[0] * f), int(self.size[1] * f), self.word, 1.0)]


def _shot(w=200, h=150):
    from PIL import Image
    return Image.new("RGB", (w, h), (240, 240, 240))


pytestmark = pytest.mark.skipif(
    not getattr(ocr, "_HAS_PIL", True) and False, reason="needs Pillow")


# ── when the second read happens ─────────────────────────────────────────────
def test_text_too_small_to_read_once_is_read_on_the_second_try():
    eng = _NeedsGlasses()
    pr = eng.match_phrase("Cancel", _shot())
    assert pr.matched, "the first read found nothing and nothing tried again"
    assert len(eng.reads) == 2, eng.reads
    assert eng.reads[1][0] > eng.reads[0][0], "the second read has to be bigger"


def test_a_first_read_that_succeeds_costs_no_second_one():
    """The rescue must be free on the path that already worked."""
    eng = _NeedsGlasses()
    eng.MIN_W = 0                       # reads fine at native size
    pr = eng.match_phrase("Cancel", _shot())
    assert pr.matched
    assert len(eng.reads) == 1, "read the screen twice when once was enough"


def test_the_rescue_can_be_turned_off():
    eng = _NeedsGlasses()
    pr = eng.match_phrase("Cancel", _shot(), _zoom=False)
    assert not pr.matched
    assert len(eng.reads) == 1


def test_a_rescue_that_also_fails_keeps_the_first_answer():
    """The summary goes in front of the user verbatim. A near-miss reported
    from an enlarged image describes a picture they are not looking at."""
    eng = _NeedsGlasses()
    eng.MIN_W = 10 ** 6                 # never reads anything
    plain = eng.match_phrase("Cancel", _shot(), _zoom=False)
    both = eng.match_phrase("Cancel", _shot())
    assert both == plain


# ── where the coordinates end up ─────────────────────────────────────────────
def test_the_box_comes_back_in_the_coordinates_of_the_real_screen():
    """⚠ This is the one that fails silently. A box left in the enlarged
    image's coordinates is twice as far from the origin as it should be, and
    the click lands somewhere else entirely — on a screen, off the bottom."""
    eng = _NeedsGlasses(at=(30, 40), size=(50, 12))
    pr = eng.match_phrase("Cancel", _shot())
    assert pr.box is not None
    assert abs(pr.box.left - 30) <= 1 and abs(pr.box.top - 40) <= 1
    assert abs(pr.box.width - 50) <= 1 and abs(pr.box.height - 12) <= 1


def test_a_search_area_offset_survives_the_rescue_too():
    """The crop origin has to be added back on, and it is added back AFTER
    the enlargement is divided out — the offset is in screen pixels and the
    box is in enlarged ones, so doing it the other way doubles the offset."""
    eng = _NeedsGlasses(at=(30, 40), size=(50, 12))
    pr = eng.match_phrase("Cancel", _shot(400, 300), region=(100, 60, 200, 150))
    assert pr.box is not None
    assert abs(pr.box.left - (100 + 30)) <= 1
    assert abs(pr.box.top - (60 + 40)) <= 1


# ── the bound on how much work this can be ───────────────────────────────────
#
# ⚠ These two tests used to assert that a 4K grab is never enlarged, on the
# reasoning that "nothing between the bound and 2x is worth the second read".
# That sentence was a guess and it was wrong, which is worth recording because
# the test read like a decision rather than an assumption.
#
# 12 MP allows a 4K desktop exactly x1.203, and measured 8 September 2026 on a
# 3840x2160 canvas — the eight UI phrases at 9-13 px in three fonts, 120 reads:
#
#     native   61/120
#     x1.203   90/120      half the misses, for 187 ms
#
# The recovery curve has no cliff at all; on a smaller canvas x1.05 already
# recovers 5 of 53 misses and it climbs smoothly from there (11 at x1.10, 15 at
# x1.15, 30 at x1.30, 35 at x1.50). So `ZOOM_MIN_FACTOR` is not the edge of a
# regime where enlarging stops working — it is a cost/benefit cut, and 1.5 put
# it above every 4K-and-larger desktop. It is 1.15 now, which admits 4K.

def test_a_four_k_screen_is_enlarged_as_far_as_the_budget_allows():
    """⚠ The one this file used to assert the opposite of. 4K is the ordinary
    big desktop, and it was the only size the rescue never ran on."""
    eng = _NeedsGlasses()
    f = eng._zoom_factor(3840, 2160)
    assert f > 1.0, "a 4K grab gets no rescue at all"
    assert f == pytest.approx(1.203, abs=0.01), (
        f"x{f:.3f} — the megapixel budget should decide this, not the floor")


def test_the_enlargement_is_still_bounded():
    """The budget is what stops a wait-for-text spending 33 MP per poll on a
    screen the text has not appeared on yet."""
    eng = _NeedsGlasses()
    assert eng._zoom_factor(1920, 1080) == 2.0
    assert eng._zoom_factor(400, 120) == 2.0, "a search area always is"
    for w, h in ((3840, 2160), (5120, 2880), (7680, 4320)):
        f = eng._zoom_factor(w, h)
        assert w * h * f * f <= eng.ZOOM_MAX_PX * 1.01, (
            f"{w}x{h} at x{f:.2f} is {w * h * f * f / 1e6:.0f} MP per poll")


def test_an_enlargement_too_small_to_help_is_not_paid_for():
    """Below the floor the second read costs its full time to recover almost
    nothing, so it is skipped and the honest first answer stands."""
    eng = _NeedsGlasses()
    assert eng._zoom_factor(20000, 12000) == 0.0
    eng.reads.clear()
    eng.match_phrase("Cancel", _shot(20000, 12000))
    assert len(eng.reads) == 1, "paid for an enlargement it decided against"


# ── against the real engine, when there is one ───────────────────────────────
@pytest.mark.skipif(not ocr.get_engine().available, reason="no OCR engine here")
def test_the_real_engine_reads_small_text_it_could_not_read_before():
    from PIL import Image, ImageDraw, ImageFont
    if not os.path.exists("C:/Windows/Fonts/segoeui.ttf"):
        pytest.skip("needs Segoe UI")
    eng = ocr.get_engine()
    img = Image.new("RGB", (1200, 700), (243, 243, 243))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 9)
    phrases = ["Delete permanently", "Open recent project", "Sign in to continue"]
    for i, p in enumerate(phrases):
        d.text((40, 30 + 42 * i), p, font=f, fill=(28, 28, 28))

    plain = sum(1 for p in phrases
                if eng.match_phrase(p, img, _zoom=False).matched)
    rescued = sum(1 for p in phrases if eng.match_phrase(p, img).matched)
    assert rescued > plain, f"plain {plain}, rescued {rescued} — of {len(phrases)}"


@pytest.mark.skipif(not ocr.get_engine().available, reason="no OCR engine here")
def test_the_real_engine_rescues_a_screen_the_old_floor_excluded():
    """⚠ End to end on a size that got NO rescue until 8 September 2026.

    3000x2000 is 6 MP, so the budget allows x1.41 — real, useful, and under the
    old 1.5 floor, which meant every screen from about 5 MP up (4K included)
    read once and gave up. Measured here: two of three 9 px phrases come back
    that the single read could not see at all.
    """
    from PIL import Image, ImageDraw, ImageFont
    if not os.path.exists("C:/Windows/Fonts/segoeui.ttf"):
        pytest.skip("needs Segoe UI")
    eng = ocr.get_engine()
    assert eng._zoom_factor(3000, 2000) > 1.0, "this size gets no rescue at all"

    img = Image.new("RGB", (3000, 2000), (245, 245, 245))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 9)
    phrases = ["Save changes", "Open settings", "Delete account"]
    for i, p in enumerate(phrases):
        d.text((90, 120 + i * 200), p, font=f, fill=(20, 20, 20))

    plain = sum(1 for p in phrases
                if eng.match_phrase(p, img, _zoom=False).matched)
    if plain == len(phrases):
        pytest.skip("this engine reads 9px text unaided; nothing to recover")
    rescued = sum(1 for p in phrases if eng.match_phrase(p, img).matched)
    assert rescued > plain, (
        f"plain {plain}, rescued {rescued} of {len(phrases)} — the enlargement "
        "this size is now allowed recovered nothing")
