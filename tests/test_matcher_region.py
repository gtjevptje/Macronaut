"""
Search-area support in matcher.

The one thing a region must never do is move the answer. A match found inside a
crop is reported at crop-relative coordinates, and every caller — the click on
a found image, the box the Test-match preview draws — reads the result as
full-screenshot pixels. Getting the shift wrong doesn't fail loudly; it clicks
somewhere else on the screen.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matcher

pytestmark = pytest.mark.skipif(not matcher.ENABLED,
                                reason="needs opencv-python + Pillow")


@pytest.fixture
def haystack(tmp_path):
    """A 400x300 black screen with a distinctive 24x18 red patch at (260, 190),
    and that patch on its own as the template."""
    from PIL import Image
    shot = Image.new("RGB", (400, 300), (0, 0, 0))
    patch = Image.new("RGB", (24, 18), (220, 30, 30))
    for x in range(0, 24, 6):                     # some structure to match on
        for y in range(0, 18, 6):
            patch.putpixel((x, y), (250, 250, 40))
    shot.paste(patch, (260, 190))
    tpl = tmp_path / "patch.png"
    patch.save(tpl)
    return shot, str(tpl), (260, 190)


def test_a_region_does_not_move_the_answer(haystack):
    shot, tpl, (tx, ty) = haystack
    whole = matcher.find(tpl, 0.9, screenshot=shot)
    inside = matcher.find(tpl, 0.9, screenshot=shot, region=(240, 170, 120, 100))
    assert whole is not None and inside is not None
    assert (inside.left, inside.top) == (whole.left, whole.top) == (tx, ty), \
        "a match found in a crop must come back in full-screenshot coordinates"


def test_a_region_that_excludes_the_target_finds_nothing(haystack):
    shot, tpl, _ = haystack
    assert matcher.find(tpl, 0.9, screenshot=shot, region=(0, 0, 100, 100)) is None


def test_an_unusable_region_searches_everything_rather_than_nothing(haystack):
    """A search area is an optimisation. Failing it closed would silently stop a
    working flow from ever matching, which is the worse of the two wrongs."""
    shot, tpl, (tx, ty) = haystack
    for bad in (None, (), "nonsense", (10, 10, 0, 0), (5000, 5000, 10, 10),
                (-500, -500, 100, 100)):
        m = matcher.find(tpl, 0.9, screenshot=shot, region=bad)
        assert m is not None and (m.left, m.top) == (tx, ty), f"region={bad!r}"


def test_a_region_hanging_off_the_edge_is_clipped_not_discarded(haystack):
    """Overhang is normal — a region dragged to the corner of a screen, then the
    screenshot grabbed at a different DPI. It still has to search what's left."""
    shot, tpl, (tx, ty) = haystack
    m = matcher.find(tpl, 0.9, screenshot=shot, region=(240, 170, 9000, 9000))
    assert m is not None and (m.left, m.top) == (tx, ty)


def test_best_match_reports_the_same_box_with_and_without_a_region(haystack):
    """best_match feeds the Test-match preview, which draws its box on the FULL
    screenshot — so it has to shift too, not just find()."""
    shot, tpl, (tx, ty) = haystack
    m = matcher.best_match(tpl, screenshot=shot, region=(200, 150, 150, 120))
    assert m is not None
    assert (m.left, m.top) == (tx, ty)


# ── Early exit ───────────────────────────────────────────────────────────────
#
# `best_match` used to run two complete twelve-scale searches — colour, then
# grey — even when the very first comparison scored a perfect 1.0000. Measured
# through `best_match` itself on 4 September 2026, on a synthetic desktop with
# the template planted in it:
#
#     1920x1080  2042 ms -> 141 ms      3840x1080  4060 ms -> 264 ms
#
# Same score, same location, ~15x less time. These tests pin the three things
# that make that safe: the answer does not change, the work really is skipped,
# and a match that is NOT near-perfect still gets the full search.

def test_a_perfect_match_still_returns_the_same_answer(haystack):
    """The whole point: faster, not different."""
    shot, tpl, (tx, ty) = haystack
    m = matcher.best_match(tpl, screenshot=shot)
    assert m is not None
    assert (m.left, m.top) == (tx, ty)
    assert m.score >= matcher._EARLY_EXIT


def test_a_perfect_match_stops_instead_of_searching_every_scale(haystack, monkeypatch):
    """⚠ Wiring, not mechanism. A threshold constant nothing breaks out of is
    an unused constant, and the suite would stay green."""
    shot, tpl, _ = haystack
    calls = []
    real = matcher._cv2.matchTemplate
    monkeypatch.setattr(matcher._cv2, "matchTemplate",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])

    matcher.best_match(tpl, screenshot=shot, grayscale=True)

    # One comparison: colour, scale 1.0. Without the early exit this is 24 —
    # twelve scales in colour and twelve more in grey.
    assert len(calls) == 1, (
        f"{len(calls)} matchTemplate calls for a perfect match; the early exit "
        "is not being taken")


def test_a_poor_match_still_searches_everything(haystack, monkeypatch):
    """The case the multi-scale engine exists for must not be short-circuited.

    A template that is nowhere on the screen has to be *proved* absent, which
    means every scale and both colour modes.
    """
    from PIL import Image
    shot, _, _ = haystack
    stranger = Image.new("RGB", (20, 16), (10, 200, 90))
    for x in range(0, 20, 4):
        stranger.putpixel((x, x % 16), (250, 0, 250))
    import tempfile, os as _os
    fd, path = tempfile.mkstemp(suffix=".png")
    _os.close(fd)
    stranger.save(path)

    calls = []
    real = matcher._cv2.matchTemplate
    monkeypatch.setattr(matcher._cv2, "matchTemplate",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    try:
        m = matcher.best_match(path, screenshot=shot, grayscale=True)
    finally:
        _os.unlink(path)

    assert m is None or m.score < matcher._EARLY_EXIT
    assert len(calls) > 12, (
        f"only {len(calls)} comparisons for an absent template — the grey pass "
        "was skipped, and that is the pass that rescues a colour-shifted match")


def test_the_template_is_still_found_at_a_different_size(haystack):
    """Multi-scale is the module's reason for existing. An early exit that
    fired before the right scale was tried would break the #1 case it was
    written for: an image captured at one DPI, searched for at another."""
    from PIL import Image
    shot, tpl, (tx, ty) = haystack
    big = Image.open(tpl).convert("RGB")
    grown = big.resize((int(big.width / 0.75), int(big.height / 0.75)),
                       Image.LANCZOS)
    import tempfile, os as _os
    fd, path = tempfile.mkstemp(suffix=".png")
    _os.close(fd)
    grown.save(path)
    try:
        m = matcher.best_match(path, screenshot=shot)
    finally:
        _os.unlink(path)

    assert m is not None
    assert abs(m.left - tx) <= 3 and abs(m.top - ty) <= 3, (
        f"found at {(m.left, m.top)}, planted at {(tx, ty)}")
    assert m.score > 0.8


# ── Interruptibility ─────────────────────────────────────────────────────────
#
# A full-screen multi-scale search is ~2 s of uninterruptible C. `main.
# SequenceTab.is_playing` documents what that costs: stop_playback() waits
# 1.5 s, the worker is still on the CPU when that expires, so the run has to be
# *retired* and tracked separately to stop Play launching a second worker
# beside the first. `should_continue` addresses the cause — the scale loop asks
# between templates, so Stop lands within one matchTemplate.

def test_a_search_can_be_abandoned_part_way(haystack):
    """The callback is consulted, and an abandoned search reports nothing."""
    shot, tpl, _ = haystack
    calls = []

    def stop_after_one():
        calls.append(1)
        return len(calls) <= 1

    m = matcher.best_match(tpl, screenshot=shot, should_continue=stop_after_one)
    assert m is None, "an abandoned search must not report a match"
    assert len(calls) >= 2, "should_continue was never polled"


def test_abandoning_reports_not_found_rather_than_a_partial_best(haystack):
    """⚠ The safe reading, and not a detail: `_do_wait_image` *clicks* what it
    is handed. A partial best returned on Stop would be a click nobody asked
    for, at coordinates chosen by a half-finished search."""
    shot, tpl, _ = haystack
    assert matcher.find(tpl, confidence=0.5, screenshot=shot,
                        should_continue=lambda: False) is None
    assert matcher.present(tpl, confidence=0.5, screenshot=shot,
                           should_continue=lambda: False) is False


def test_a_callback_that_never_stops_changes_nothing(haystack):
    """The guard has to be free when nobody is stopping — this is on the hot
    path of every Detect step."""
    shot, tpl, (tx, ty) = haystack
    plain = matcher.best_match(tpl, screenshot=shot)
    guarded = matcher.best_match(tpl, screenshot=shot,
                                 should_continue=lambda: True)
    assert plain == guarded
    assert guarded is not None and (guarded.left, guarded.top) == (tx, ty)


def test_stopping_between_the_colour_and_grey_passes_is_noticed(haystack):
    """⚠ The gap the first draft left open. A pass that is abandoned returns
    None, which is exactly what "not on screen" looks like — so without a check
    *between* the passes, stopping during the colour search would send the grey
    search off on another full sweep."""
    from PIL import Image
    import tempfile, os as _os

    shot, _, _ = haystack
    stranger = Image.new("RGB", (20, 16), (10, 200, 90))
    for x in range(0, 20, 4):
        stranger.putpixel((x, x % 16), (250, 0, 250))
    fd, path = tempfile.mkstemp(suffix=".png")
    _os.close(fd)
    stranger.save(path)

    seen = []
    real = matcher._best_match_cv2

    # **kw so this stays a stand-in rather than a second signature to keep in
    # step: it is here to count the passes, not to restate the argument list.
    def counting(hay, needle, scales, grayscale, should_continue=None, **kw):
        seen.append(grayscale)
        return real(hay, needle, scales, grayscale, should_continue, **kw)

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(matcher, "_best_match_cv2", counting)
    try:
        # Runs the whole colour pass, then reports stopped.
        state = {"n": 0}

        def stop_after_colour():
            state["n"] += 1
            return state["n"] <= len(matcher.DEFAULT_SCALES)

        m = matcher.best_match(path, screenshot=shot,
                               should_continue=stop_after_colour)
    finally:
        mp.undo()
        _os.unlink(path)

    assert m is None
    assert seen == [False], (
        f"passes run: {seen} — the grey pass started after the search had "
        "already been abandoned")


def test_the_running_flow_actually_passes_the_stop_callback():
    """⚠ Wiring, read with `ast`. The mechanism above is worthless if the three
    live call sites do not opt in, and they would still pass every test here.

    Every `matcher.find` / `matcher.present` call in `flow_exec` runs inside a
    flow, which is precisely where Stop has to work. The UI's preview and the
    self-test are not flows and correctly leave it off.
    """
    import ast
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "flow_exec.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name) and f.value.id == "matcher"
                and f.attr in ("find", "present", "best_match")):
            continue
        if not any(k.arg == "should_continue" for k in node.keywords):
            missing.append(f"flow_exec.py:{node.lineno}  matcher.{f.attr}(...)")

    assert not missing, (
        "these searches cannot be interrupted, so Stop waits out a full "
        "multi-scale search:\n  " + "\n  ".join(missing)
        + "\nPass should_continue=self.running.")
