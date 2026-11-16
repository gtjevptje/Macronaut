"""Templates with no detail in them — a crop of a plain-coloured button.

TM_CCOEFF_NORMED correlates mean-subtracted patches, so it divides by the
template's standard deviation. A uniform template has none, every position
scores the same 0/0, OpenCV reports that as 1.0, and minMaxLoc hands back the
first pixel it looked at. The result was a perfect match in the top-left corner
of the screen, for a colour that need not have been on screen at all — and
every caller believed it, because a perfect match is not an error.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matcher

pytestmark = pytest.mark.skipif(not matcher.MULTISCALE,
                                reason="needs opencv-python + Pillow")


@pytest.fixture
def desk(tmp_path):
    """A 480x360 desktop with a plain blue button on it, a textured area, and
    an orange panel whose BRIGHTNESS is mid-grey — 206/103/26 is luminance 125,
    which is what a greyscale rescue pass would happily call a grey match."""
    from PIL import Image
    import numpy as np

    rng = np.random.default_rng(4)
    arr = rng.integers(0, 60, (360, 480, 3), dtype=np.uint8)   # dark texture
    arr[40:80, 60:180] = (40, 90, 200)          # the plain blue button
    arr[200:260, 300:420] = (206, 103, 26)      # orange, luminance 125
    # The same button one grey level away from flat — a gradient, a dithered
    # fill, anti-aliasing. Half a standard deviation, and enough.
    dither = np.random.default_rng(9).integers(-1, 2, (40, 120, 3))
    arr[280:320, 60:180] = np.clip(
        np.full((40, 120, 3), (40, 90, 200), np.int16) + dither, 0, 255)

    def save(a, name):
        p = tmp_path / name
        Image.fromarray(np.ascontiguousarray(a).astype(np.uint8), "RGB").save(p)
        return str(p)

    return Image.fromarray(arr, "RGB"), arr, save


def test_a_plain_coloured_button_is_found_where_it_actually_is(desk):
    shot, arr, save = desk
    tpl = save(arr[40:80, 60:180], "button.png")
    m = matcher.best_match(tpl, screenshot=shot)
    assert m is not None
    assert (m.left, m.top) == (60, 40), \
        "a uniform template used to score 1.0 in the corner instead"
    assert m.score > 0.99


def test_a_colour_that_is_not_on_screen_is_not_found_in_the_corner(desk):
    """The headline of the bug. Nothing on this desktop is pure mid-grey."""
    import numpy as np
    shot, arr, save = desk
    tpl = save(np.full((40, 120, 3), 128, np.uint8), "grey.png")
    m = matcher.best_match(tpl, screenshot=shot)
    assert m is None or m.score < 0.5, f"scored {m and m.score} at {m and (m.left, m.top)}"
    assert matcher.present(tpl, 0.8, screenshot=shot) is False
    assert matcher.find(tpl, 0.8, screenshot=shot) is None


def test_a_uniform_template_is_not_rescued_by_brightness(desk):
    """The greyscale pass exists to save a match that colour differences
    spoiled. A plain-coloured template IS its colour and nothing else, so in
    grey there is one number left and any region of that brightness answers to
    it — here, an orange panel answering a search for mid-grey."""
    import numpy as np
    shot, arr, save = desk
    tpl = save(np.full((60, 120, 3), 125, np.uint8), "lum.png")
    m = matcher.best_match(tpl, screenshot=shot, grayscale=True)
    # Asserted on the SCORE, not the location. `best_match` reports the best
    # it found whatever the score, so the UI can show a confidence read-out —
    # it will still point at wherever the squared difference bottomed out, and
    # saying so with a 0.0 beside it is the correct answer. The bug was that
    # the grey pass called that same place a 1.0.
    assert m is None or m.score < 0.5, f"scored {m and m.score:.4f} on brightness"
    assert matcher.present(tpl, 0.8, screenshot=shot) is False


def test_one_grey_level_of_detail_is_enough_for_the_ordinary_path(desk):
    """The failure is confined to EXACTLY zero variance — a template with a
    standard deviation of half a grey level already correlates perfectly. This
    is the singular case, not a low-detail heuristic with a threshold, and a
    test that let the flat path creep upwards would hide that."""
    import numpy as np
    from PIL import Image
    shot, arr, save = desk
    tpl = save(arr[280:320, 60:180], "dithered.png")

    assert not matcher._is_flat(np.asarray(Image.open(tpl).convert("RGB")))
    m = matcher.best_match(tpl, screenshot=shot)
    assert m is not None and m.score > 0.9
    assert (m.left, m.top) == (60, 280)


def test_flatness_is_measured_per_channel():
    """A plain blue button is one colour, and the standard deviation of the
    whole array is 66.8 because red, green and blue differ from each other.
    Asking the array as a whole answers "not flat" for every uniform colour
    that is not a shade of grey — which is almost all of them, and is why the
    first version of this fix changed nothing."""
    import numpy as np
    blue = np.full((20, 40, 3), (40, 90, 200), np.uint8)
    assert matcher._is_flat(blue) is True
    assert float(blue.std()) > 60, "the trap this guards against"

    grey = np.full((20, 40, 3), 128, np.uint8)
    assert matcher._is_flat(grey) is True

    textured = np.random.default_rng(2).integers(0, 255, (20, 40, 3), dtype=np.uint8)
    assert matcher._is_flat(textured) is False


def test_an_ordinary_template_still_matches_the_way_it_did(desk):
    """The whole of this change has to be invisible to every template with any
    detail in it, which is every template anyone has ever saved."""
    shot, arr, save = desk
    tpl = save(arr[120:160, 200:290], "textured.png")
    m = matcher.best_match(tpl, screenshot=shot)
    assert m is not None and m.score > 0.99
    assert (m.left, m.top) == (200, 120)


# ═════════════════════════════════════════════════════════════════════════════
#  Templates with transparent pixels
# ═════════════════════════════════════════════════════════════════════════════
#
# `Image.open(path).convert("RGB")` drops the alpha channel and keeps whatever
# RGB was under it, so a cut-out icon was matched as though its transparent
# background were part of the picture — and what colour that is gets decided by
# whichever tool wrote the file. Same file, same screen, same shape, and the
# answer depended on a colour nobody can see.

@pytest.fixture
def disc(tmp_path):
    """A green disc on a busy dark desktop, and the disc cut out of it with
    three different colours left under the transparency."""
    from PIL import Image
    import numpy as np

    rng = np.random.default_rng(11)
    arr = rng.integers(0, 70, (300, 400, 3), dtype=np.uint8)
    yy, xx = np.mgrid[0:60, 0:60]
    round_ = ((xx - 30) ** 2 + (yy - 30) ** 2) <= 26 ** 2
    arr[100:160, 150:210][round_] = (30, 160, 90)

    def cut(fill, name):
        rgba = np.zeros((60, 60, 4), np.uint8)
        rgba[..., :3] = arr[100:160, 150:210]
        rgba[..., 3] = np.where(round_, 255, 0)
        rgba[~round_, :3] = fill
        p = tmp_path / name
        Image.fromarray(rgba, "RGBA").save(p)
        return str(p)

    return Image.fromarray(arr, "RGB"), arr, cut, round_, tmp_path


def test_a_cut_out_icon_is_found_whatever_is_under_the_transparency(disc):
    """The bug in one assertion: white under the alpha used to score 0.69 in
    the wrong place and report the icon absent, while black scored 0.94 and
    found it. Nothing on screen could explain the difference."""
    shot, arr, cut, _round, _tmp = disc
    for fill, label in ((0, "black"), (255, "white"), (128, "grey")):
        p = cut(fill, f"disc_{label}.png")
        m = matcher.best_match(p, screenshot=shot)
        assert m is not None, label
        assert (m.left, m.top) == (150, 100), f"{label}: found at {(m.left, m.top)}"
        assert m.score > 0.95, f"{label}: {m.score}"
        assert matcher.find(p, 0.8, screenshot=shot) is not None, label


def test_a_cut_out_icon_that_is_not_there_is_still_not_found(disc):
    """Masking is a way to ignore pixels, not a way to match anything. ⚠ Of
    the three metrics OpenCV will mask, TM_CCORR_NORMED scores 0.9962 against
    a screen the template is nowhere on — it locates well and cannot reject."""
    import numpy as np
    from PIL import Image
    shot, arr, cut, round_, tmp = disc

    # A disc the colour of the DESKTOP rather than of the button. Chosen
    # deliberately: squared difference rates this 0.72 because it normalises
    # by the image's own energy and the two are close in absolute value, while
    # counting the pixels that actually agree gives 0.05. A magenta disc would
    # be rejected by either and would test nothing.
    rgba = np.zeros((60, 60, 4), np.uint8)
    rgba[..., :3] = (35, 35, 35)
    rgba[..., 3] = np.where(round_, 255, 0)
    p = tmp / "absent.png"
    Image.fromarray(rgba, "RGBA").save(p)

    m = matcher.best_match(str(p), screenshot=shot)
    assert m is None or m.score < 0.2,         f"scored {m and m.score:.4f} — squared difference alone rates it ~0.72"
    assert matcher.present(str(p), 0.8, screenshot=shot) is False


def test_an_opaque_alpha_channel_changes_nothing(disc):
    """An RGBA file whose alpha says "all of it" is an ordinary template, and
    has to take the ordinary path — every screenshot saved as PNG-with-alpha
    by some tool or other would otherwise change how it matches."""
    import numpy as np
    from PIL import Image
    shot, arr, cut, _round, tmp = disc

    rgba = np.dstack([arr[100:160, 150:210],
                      np.full((60, 60), 255, np.uint8)])
    p = tmp / "opaque_rgba.png"
    Image.fromarray(rgba, "RGBA").save(p)

    img, keep = matcher._load_template(str(p))
    assert keep is None, "an alpha channel that says nothing must not mask"
    m = matcher.best_match(str(p), screenshot=shot)
    assert (m.left, m.top) == (150, 100) and m.score > 0.99


def test_a_template_that_is_entirely_transparent_finds_nothing(disc):
    """There is no picture in it to look for."""
    import numpy as np
    from PIL import Image
    shot, arr, cut, _round, tmp = disc
    p = tmp / "empty.png"
    Image.fromarray(np.zeros((40, 40, 4), np.uint8), "RGBA").save(p)

    assert matcher._load_template(str(p)) == (None, None)
    assert matcher.best_match(str(p), screenshot=shot) is None
    assert matcher.find(str(p), 0.8, screenshot=shot) is None


def test_the_mask_is_scaled_with_the_template(disc):
    """Multi-scale is the reason this module exists. A mask left at its
    original size against a resized template either raises or, worse, lines up
    with the wrong pixels."""
    shot, arr, cut, _round, _tmp = disc
    p = cut(255, "disc_scaled.png")
    m = matcher.best_match(p, screenshot=shot, scales=[0.75, 1.0, 1.5])
    assert m is not None and m.score > 0.95
    assert (m.left, m.top) == (150, 100)


def test_a_black_cut_out_icon_is_findable_at_all(tmp_path):
    """⚠ TM_SQDIFF_NORMED divides by sqrt(sum(T²·M)·sum(I²·M)), so a template
    whose opaque pixels are all zero puts a zero on top of that fraction and
    every position in the result comes back `inf` — not one bad answer, the
    whole surface. A black icon on a transparent background is an ordinary
    thing to export, and it was not found anywhere, ever.
    """
    from PIL import Image
    import numpy as np

    arr = np.random.default_rng(3).integers(60, 200, (300, 400, 3), dtype=np.uint8)
    yy, xx = np.mgrid[0:40, 0:40]
    round_ = ((xx - 20) ** 2 + (yy - 20) ** 2) <= 16 ** 2
    arr[150:190, 200:240][round_] = 0            # a black disc, on screen
    shot = Image.fromarray(arr, "RGB")

    rgba = np.zeros((40, 40, 4), np.uint8)       # all-black, disc-shaped alpha
    rgba[..., 3] = np.where(round_, 255, 0)
    p = tmp_path / "black_icon.png"
    Image.fromarray(rgba, "RGBA").save(p)

    m = matcher.best_match(str(p), screenshot=shot)
    assert m is not None and m.score > 0.95, f"scored {m and m.score}"
    # Within a pixel, not on it: a disc is symmetric, so shifting it by one
    # still covers almost all of the same black pixels. Pinning it exactly
    # would be pinning the noise around the shape, not the shape.
    assert abs(m.left - 200) <= 2 and abs(m.top - 150) <= 2, (m.left, m.top)


def test_a_black_area_on_screen_does_not_swallow_the_search(tmp_path):
    """⚠ The other half of the same division. A position whose pixels are all
    zero UNDER THE MASK puts a zero on the bottom of the fraction, and that
    position comes back `inf` — a pure black panel on screen is enough. Sorted
    the wrong way those undefined positions win the minimum, and the true
    match, which is one position out of a quarter of a million, is never
    looked at. The search does not fail loudly; it reports the icon absent.
    """
    from PIL import Image
    import numpy as np

    arr = np.random.default_rng(5).integers(60, 200, (300, 400, 3), dtype=np.uint8)
    arr[10:120, 10:140] = 0                       # a pure black panel
    yy, xx = np.mgrid[0:40, 0:40]
    round_ = ((xx - 20) ** 2 + (yy - 20) ** 2) <= 16 ** 2
    arr[200:240, 300:340][round_] = (30, 160, 90)  # the icon, well away from it
    shot = Image.fromarray(arr, "RGB")

    rgba = np.zeros((40, 40, 4), np.uint8)
    rgba[..., :3] = (30, 160, 90)
    rgba[..., 3] = np.where(round_, 255, 0)
    p = tmp_path / "green_icon.png"
    Image.fromarray(rgba, "RGBA").save(p)

    m = matcher.best_match(str(p), screenshot=shot)
    assert m is not None and m.score > 0.95, f"scored {m and m.score}"
    assert abs(m.left - 300) <= 2 and abs(m.top - 200) <= 2, (m.left, m.top)
