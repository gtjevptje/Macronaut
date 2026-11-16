"""
Unified on-screen image matching for Macronaut.

One module, so matching behaves identically everywhere it is asked for.

⚠ This said "Both the Basic clicker (clicker.py) and the Sequence engine
(recorder.py) use this single module" until 4 September 2026. Both named
modules are dead — `clicker.py` and `recorder.PlaybackWorker` were replaced by
the flow engine and never removed. The live callers are:

  * `flow_exec` — the autoclick image gate, the Wait-for-image step, and the
    Detect/If-Else sensor. This is everything that runs during an automation.
  * `main` — the Test-match preview in the image-step editor.
  * `selftest` — the "image match" check in the frozen binary.

Verified by grep rather than assumed, because trusting the previous version of
this sentence is exactly what cost a session elsewhere in this repo.

Key improvement over plain pyautogui.locate():
  * MULTI-SCALE matching — the template is searched at a range of sizes, so an
    image captured on one monitor/DPI still matches on a different
    resolution or Windows display-scaling setting (the #1 cause of
    "works on my machine, fails on theirs").
  * Optional GRAYSCALE fallback for minor colour/theme differences.
  * Returns the best similarity SCORE (not just found/not-found) so the UI can
    show a live confidence read-out.

Graceful degradation:
  * If OpenCV (cv2) is available  -> fast, robust multi-scale matching.
  * If only pyautogui is available -> falls back to single-scale locate().
  * If neither / PIL missing       -> ENABLED is False; callers should treat
    "can't check" as "don't block" so automation still runs.

All coordinates returned are in the pixel space of the screenshot that was
searched (for an all-screens grab that is PHYSICAL pixels), matching what the
callers already expect from pyautogui.locate().
"""

from collections import namedtuple
from typing import List, Optional

# Match in the coordinate space of the searched screenshot.
Match = namedtuple("Match", ["left", "top", "width", "height", "score"])

# ── Optional dependencies ──────────────────────────────────────────────────
try:
    import numpy as _np
    import cv2 as _cv2
    _HAS_CV2 = True
except Exception:
    _HAS_CV2 = False

try:
    from PIL import ImageGrab as _ImageGrab, Image as _Image
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

try:
    import pyautogui as _pyautogui
    _pyautogui.FAILSAFE = False
    _HAS_PYAUTOGUI = True
except Exception:
    _HAS_PYAUTOGUI = False

# Matching is possible if we can grab the screen (PIL) and match it somehow.
ENABLED = _HAS_PIL and (_HAS_CV2 or _HAS_PYAUTOGUI)
# True only when the upgraded multi-scale engine is active.
MULTISCALE = _HAS_CV2 and _HAS_PIL

# ⚠ This is a lookup table of exact ratios, NOT a search, and the difference
# is the whole story. Measured 8 September 2026 by planting a pure resample of
# the template (no re-rendering, so scale is the only variable) and asking
# `best_match`: every rung scores ~0.99 and everything between them scores
# ~0.58 — while an ABSENT template scores 0.567-0.599 on the same desktop, so a
# target at the wrong scale is very nearly indistinguishable from one that is
# not there. Sweeping finely shows why: the correlation peak is about ±2.5%
# wide and it is not unimodal (a spurious 0.72 bump sits at x1.44 when the
# target is at x1.75), so hill-climbing from a neighbouring rung cannot find it
# either. That is normalised cross-correlation behaving normally.
#
# So the only lever is which rungs exist, and the ratios that occur in practice
# are a known discrete set: Windows display scaling (100/125/150/175/200%) and
# browser zoom. ⚠ The comment here used to claim it covered "100/125/150/175/
# 200 % and their inverses". It did not — 1.75 and 0.571 were both absent, so a
# template captured at 100% was invisible on a 175% screen and vice versa, and
# 175% is also one of Chrome's zoom steps. Eight of the twenty ordered pairs of
# Windows scalings are still uncovered; `tests/test_matcher_scales.py` keeps
# that list honest.
#
# ⚠ Rungs are not free and must be argued for one at a time. A miss runs every
# one of them in colour and again in grey, and that is exactly what a
# wait-for-image pays on every poll while it waits: 2005 ms at 1080p for 12
# rungs, 2563 for these 16, and 3679 if all twelve missing ratios were added.
# Doubling the ladder to cover the rest doubles the latency of every miss.
#
# 1.0 is first because it is the overwhelmingly common case — the template was
# captured on this screen at this DPI — and `_EARLY_EXIT` then makes it one
# comparison rather than thirty-two.
DEFAULT_SCALES: List[float] = [
    1.0, 0.9, 1.1, 0.8, 1.25, 0.833, 1.2, 0.75, 1.33, 0.67,
    1.5, 0.6, 1.75, 0.571, 2.0, 0.5,
]

_MIN_TEMPLATE_PX = 8   # ignore degenerate scaled templates

# Stop searching once a match is this good.
#
# ⚠ This is a 15x speed-up of the app's headline feature, not a micro-tune.
# Measured 4 September 2026 on a synthetic desktop with the template planted in
# it, through `best_match` itself:
#
#     1920x1080   2042 ms   score 1.0000     3840x1080   4060 ms
#
# Two whole multi-scale passes ran — twelve scales in colour, then twelve more
# in grey — every one of them after the *first* comparison had already returned
# a perfect 1.0000. `DEFAULT_SCALES` leads with 1.0 precisely because that is
# the overwhelmingly common case (the template was captured on this screen, at
# this DPI), and the loop then spent two seconds proving nothing could beat it.
#
# Scores are capped at 1.0, so stopping at 0.995 forfeits at most 0.005 of
# score — no caller can distinguish that, and every caller compares against a
# confidence threshold well below it. A *poor* match still searches every scale
# and both colour modes, which is the case the multi-scale engine exists for.
_EARLY_EXIT = 0.995

# ── Templates with no detail in them ────────────────────────────────────────
#
# ⚠ A template cropped from a plain-coloured button used to be found, with a
# perfect 1.0000 score, in the top-left corner of the screen — whether or not
# it was on screen at all. So did a colour that was nowhere on the display.
#
# TM_CCOEFF_NORMED correlates MEAN-SUBTRACTED patches: it divides by the
# template's standard deviation, and a uniform template has none. Every
# position scores the same 0/0, OpenCV reports that as 1.0, and `minMaxLoc`
# hands back the first pixel it looked at. Every caller believes it. A Detect
# step clicks (0, 0), an If/Else takes its true branch forever, and a
# wait-for-image returns immediately — none of them with anything to report,
# because a perfect match is not an error.
#
# Measured 8 September 2026: the failure is confined to EXACTLY zero variance.
# A template with a standard deviation of 0.56 — one grey level of dither —
# already scores 1.0000 in the right place. So this is not a low-detail
# heuristic with a threshold to tune; it is the singular case, tested for
# exactly, and everything else goes down the path it always did.
#
# For a uniform template the question is not "does this correlate" but "is
# this region uniformly that colour", so it is located by squared difference
# and then scored by how much of the box really is the colour asked for.
# Squared difference alone is not enough to score with: it normalises by the
# image's own energy and rates a grey block that is nowhere on screen 0.98.
#
# ⚠ Known and not fixed: the degeneracy is symmetric, so a template with a
# LITTLE detail over a screen region with NONE correlates to about zero as
# well — a template that is nearly but not quite uniform, because it came
# through a lossy step or was rendered slightly differently, sitting on a
# perfectly flat button. That fails to match, which is visible and safe, where
# the case above failed by matching the wrong place and calling it certain.
# Widening the test to "nearly flat" needs a threshold, and the measurement
# says there is nowhere natural to put one: half a grey level of detail
# already correlates perfectly, as long as both sides have it.
_FLAT_TOL = 12          # per channel, out of 255: anti-aliasing, not a change


def _is_flat(a) -> bool:
    """True if every pixel of `a` is the same — the degenerate case above.

    ⚠ PER CHANNEL. A plain blue button is one colour and `a.std()` over the
    whole array is 66.8, because red, green and blue differ from each other;
    the variation that matters is the variation OpenCV subtracts, which is
    within each channel. Asking the array as a whole silently answers "not
    flat" for every uniform colour that is not a shade of grey — that is to
    say, for almost all of them.
    """
    return bool(_np.all(a.std(axis=(0, 1)) == 0))


def _agreement(hay, tmpl, loc, keep=None) -> float:
    """How much of `tmpl`, placed at `loc`, is really the colour it asks for.

    The honest score behind both of the special paths below. Squared
    difference finds the right place and cannot be trusted to say how good it
    is — it normalises by the image's own energy, and rates a grey block that
    is nowhere on screen 0.98 — so what it finds is scored by counting pixels
    instead. `keep` restricts the count to a template's opaque part.
    """
    x, y = loc
    th, tw = tmpl.shape[:2]
    if y < 0 or x < 0 or y + th > hay.shape[0] or x + tw > hay.shape[1]:
        return 0.0
    under = hay[y:y + th, x:x + tw].astype(_np.int16)
    close = _np.abs(under - tmpl.astype(_np.int16)) <= _FLAT_TOL
    if close.ndim == 3:                          # colour pass: every channel
        close = close.all(axis=2)
    if keep is not None:
        return float(close[keep].mean()) if keep.any() else 0.0
    return float(close.mean())


def _flat_match(hay, tmpl):
    """(score, (x, y)) for a template that is all one colour. See _FLAT_TOL."""
    res = _cv2.matchTemplate(hay, tmpl, _cv2.TM_SQDIFF_NORMED)
    _, _, min_loc, _ = _cv2.minMaxLoc(res)      # squared difference: low wins
    return _agreement(hay, tmpl, min_loc), min_loc


def _masked_match(hay, tmpl, keep):
    """(score, (x, y)) for a template with transparent pixels. See _MASK note.

    ⚠ Of the three metrics OpenCV will mask, only this one is usable.
    `TM_CCOEFF_NORMED` with a mask comes back all-NaN on this build, and
    `TM_CCORR_NORMED` is not mean-subtracted, so it scores 0.9962 against a
    screen the template is nowhere on — it locates well and cannot reject at
    all. Located by squared difference, scored by `_agreement`, exactly as the
    uniform case is and for the same reason.
    """
    m3 = keep.astype(_np.uint8) * 255
    if tmpl.ndim == 3:
        m3 = _np.repeat(m3[:, :, None], tmpl.shape[2], axis=2)

    # ⚠ A black cut-out is unmatchable without this. TM_SQDIFF_NORMED divides
    # by sqrt(sum(T²·M) · sum(I²·M)), so a template whose OPAQUE pixels are all
    # zero has a zero on the top of that fraction and every position in the
    # result comes back `inf` — not one bad answer, the whole surface. A black
    # icon on a transparent background is a normal thing to export, and it
    # simply was not found, anywhere, ever.
    #
    # Lifting those pixels one level off zero costs a comparison of 1/255 in a
    # difference that `_agreement` tolerates twelve of, and puts the
    # denominator back. Only when it is needed: everything else is untouched.
    if not tmpl[keep].any():
        tmpl = tmpl.copy()
        tmpl[keep] = 1

    res = _cv2.matchTemplate(hay, tmpl, _cv2.TM_SQDIFF_NORMED, mask=m3)
    # Individual positions can still be undefined — a region that is itself all
    # zero under the mask. They are not matches, and must not win a minimum:
    # the true match is one position out of a quarter of a million, and a NaN
    # or an -inf allowed to sort below it loses the whole search.
    res = _np.nan_to_num(res, nan=1e9, posinf=1e9, neginf=1e9)
    _, _, min_loc, _ = _cv2.minMaxLoc(res)
    return _agreement(hay, tmpl, min_loc, keep), min_loc


# ── Templates with transparent pixels ───────────────────────────────────────
#
# ⚠ `Image.open(path).convert("RGB")` DROPS the alpha channel and keeps
# whatever RGB was sitting under it, so a template exported with a transparent
# background was matched as though those pixels were part of the picture — and
# what colour they are is decided by whichever tool wrote the file. Measured
# 8 September 2026 on a round button cut out of its background: with black
# under the transparency it scored 0.94 and was found; with white, 0.69, in
# the wrong place, and `find(0.8)` reported it absent. Same file, same screen,
# same shape, and the answer depends on a colour nobody can see.
#
# Macronaut's own capture writes opaque RGB, so this is not the common case —
# it is what happens when somebody points a Detect step at an icon they
# already had, which is a reasonable thing to do and used to fail for reasons
# nothing on screen could explain.
#
# A pixel counts as part of the template at or above this alpha. Half-
# transparent is a judgement either way; anti-aliased edges sit near the ends.
_ALPHA_MIN = 128


def _load_template(path):
    """(RGB image, keep-mask or None). The mask is None for an opaque file, so
    every template anyone has ever saved takes exactly the path it did."""
    raw = _Image.open(path)
    has_alpha = raw.mode in ("RGBA", "LA", "PA") or (
        raw.mode == "P" and "transparency" in raw.info)
    if not has_alpha:
        return raw.convert("RGB"), None
    rgba = raw.convert("RGBA")
    a = _np.asarray(rgba)[:, :, 3]
    keep = a >= _ALPHA_MIN
    if keep.all():
        return rgba.convert("RGB"), None     # an alpha channel that says nothing
    if not keep.any():
        return None, None                    # nothing but transparency to find
    return rgba.convert("RGB"), keep


# ── Screen capture ──────────────────────────────────────────────────────────
def grab_all_screens():
    """Return a PIL RGB image of the full virtual desktop (physical pixels)."""
    if not _HAS_PIL:
        return None
    return _ImageGrab.grab(all_screens=True).convert("RGB")


# ── Core multi-scale matcher (OpenCV) ───────────────────────────────────────
def _best_match_cv2(hay_rgb, needle_rgb, scales, grayscale,
                    should_continue=None, keep=None) -> Optional[Match]:
    """Best match of needle within hay across scales. Score-only, no threshold.

    ⚠ `should_continue` is what makes Stop work during a Detect step. Without
    it a full-screen search is a single uninterruptible ~2 s block of C code,
    and `main.SequenceTab.is_playing` documents the consequence: `stop_playback`
    waits 1.5 s, the worker is still on the CPU when that expires, and the run
    has to be *retired* — tracked separately so Play cannot start a second
    worker alongside the first. That is the symptom being managed; this is the
    cause. Checked between scales, so Stop lands within one `matchTemplate`
    (~130 ms in colour, ~36 ms in grey) instead of within the whole search.

    Returns None when abandoned, never a partial best: the caller cannot tell
    "stopped early" from "this is genuinely the best" otherwise, and one of
    those answers gets *clicked*.
    """
    hay = _np.asarray(hay_rgb)
    needle = _np.asarray(needle_rgb)
    if grayscale:
        hay = _cv2.cvtColor(hay, _cv2.COLOR_RGB2GRAY)
        needle = _cv2.cvtColor(needle, _cv2.COLOR_RGB2GRAY)

    H, W = hay.shape[:2]
    nh, nw = needle.shape[:2]
    if nh == 0 or nw == 0:
        return None

    # Asked once, of the array actually about to be matched — the grey pass
    # converts first, and a template can be flat in grey without being flat in
    # colour. Scaling a uniform template leaves it uniform, so the answer
    # holds for every size below. See _FLAT_TOL.
    flat = _is_flat(needle)

    best: Optional[Match] = None
    for s in scales:
        if should_continue is not None and not should_continue():
            return None
        tw, th = int(round(nw * s)), int(round(nh * s))
        if tw < _MIN_TEMPLATE_PX or th < _MIN_TEMPLATE_PX or tw > W or th > H:
            continue
        interp = _cv2.INTER_AREA if s < 1.0 else _cv2.INTER_LINEAR
        tmpl = _cv2.resize(needle, (tw, th), interpolation=interp)
        if keep is not None:
            # NEAREST for the mask: it is a yes/no per pixel, and smoothing it
            # invents half-transparent pixels at every edge of the shape.
            km = _cv2.resize(keep.astype(_np.uint8), (tw, th),
                             interpolation=_cv2.INTER_NEAREST).astype(bool)
            if not km.any():
                continue          # scaled away to nothing
            max_val, max_loc = _masked_match(hay, tmpl, km)
        elif flat:
            max_val, max_loc = _flat_match(hay, tmpl)
        else:
            res = _cv2.matchTemplate(hay, tmpl, _cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = _cv2.minMaxLoc(res)
        if best is None or max_val > best.score:
            best = Match(int(max_loc[0]), int(max_loc[1]), tw, th, float(max_val))
        if best.score >= _EARLY_EXIT:
            # Near-perfect. Nothing left to win, and eleven more scales cost
            # more than everything else this function does. See _EARLY_EXIT.
            break
    return best


def _best_match_pyautogui(template_path, screenshot, confidence) -> Optional[Match]:
    """Fallback: single-scale locate via pyautogui. Returns Match or None."""
    try:
        needle = _Image.open(template_path).convert("RGB")
        box = _pyautogui.locate(needle, screenshot, confidence=confidence)
    except Exception:
        return None
    if box is None:
        return None
    return Match(int(box.left), int(box.top), int(box.width), int(box.height),
                 float(confidence))


# ── Search-area helpers ──────────────────────────────────────────────────────
def _crop_to_region(shot, region):
    """
    Crop `shot` to a PHYSICAL (x, y, w, h) box, returning (image, dx, dy).

    dx/dy is the crop origin, which every match found inside the crop has to be
    shifted by — a caller that clicks what it found needs full-screenshot
    coordinates, not coordinates inside a box only this module knows about.
    An unusable region degrades to "search everything" rather than to nothing:
    a search area is an optimisation, and failing it closed would silently stop
    a working flow from ever matching. "Unusable" includes a zero-size box and
    one that lies entirely off the screenshot — clamping those to a 1x1 sliver
    would technically be a search area and would never match anything again.
    A region that merely *overhangs* an edge is clipped and still used.
    """
    if not region or shot is None:
        return shot, 0, 0
    try:
        x, y, w, h = (int(v) for v in region)
    except Exception:
        return shot, 0, 0
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(shot.width, x + w), min(shot.height, y + h)
    if w <= 0 or h <= 0 or x1 <= x0 or y1 <= y0:
        return shot, 0, 0
    try:
        return shot.crop((x0, y0, x1, y1)), x0, y0
    except Exception:
        return shot, 0, 0


def _shifted(m: Optional[Match], dx: int, dy: int) -> Optional[Match]:
    """Move a Match found in a crop back into full-screenshot coordinates."""
    if m is None or (dx == 0 and dy == 0):
        return m
    return m._replace(left=m.left + dx, top=m.top + dy)


# ── Public API ───────────────────────────────────────────────────────────────
def best_match(template_path: str, screenshot=None, grayscale: bool = True,
               scales: Optional[List[float]] = None, region=None,
               should_continue=None) -> Optional[Match]:
    """
    Return the BEST match of the template anywhere on screen, regardless of
    threshold (so the UI can display the score). None if matching impossible.
    `screenshot` may be a pre-grabbed PIL RGB image; otherwise the full virtual
    desktop is grabbed. `region` is an optional PHYSICAL (x, y, w, h) search
    area; the returned box is still in full-screenshot coordinates either way.

    `should_continue` is an optional callable polled between template scales;
    returning False abandons the search and yields None. It is how a running
    flow makes Stop responsive — see `_best_match_cv2`. Callers that are not a
    running flow (the UI's match preview, the self-test) leave it out.
    """
    if not ENABLED or not template_path:
        return None
    shot = screenshot if screenshot is not None else grab_all_screens()
    if shot is None:
        return None
    shot, dx, dy = _crop_to_region(shot, region)

    if _HAS_CV2:
        try:
            needle, keep = _load_template(template_path)
        except Exception:
            return None
        if needle is None:
            return None
        sc = scales or DEFAULT_SCALES

        # ⚠ The greyscale rescue is switched off for a uniform template, and
        # this is not an optimisation. The grey pass exists to save a match
        # that colour differences spoiled — but a plain-coloured template IS
        # its colour and nothing else, so converting it discards the whole of
        # what was being looked for and leaves one number. A plain grey
        # template then matches any region of the same brightness: a block of
        # (206, 103, 26) is luminance 125, so an orange panel answers a search
        # for mid-grey with a perfect score. There is nothing there to rescue.
        if grayscale and (keep is not None or _is_flat(_np.asarray(needle))):
            grayscale = False

        def stopped() -> bool:
            # ⚠ Asked again *between* the passes, not only inside them. A pass
            # that was abandoned returns None, which is indistinguishable from
            # "this image is not on screen" — and reading it as the latter would
            # send the grey pass off on another full search after Stop.
            return should_continue is not None and not should_continue()

        color = _best_match_cv2(shot, needle, sc, grayscale=False,
                                should_continue=should_continue, keep=keep)
        if stopped():
            return None
        if not grayscale:
            return _shifted(color, dx, dy)
        if color is not None and color.score >= _EARLY_EXIT:
            # The grey pass exists to rescue a match that colour differences
            # spoiled. Colour just scored ~1.0, so there is nothing to rescue,
            # and this skips a second twelve-scale search. See _EARLY_EXIT.
            return _shifted(color, dx, dy)
        gray = _best_match_cv2(shot, needle, sc, grayscale=True,
                               should_continue=should_continue, keep=keep)
        if stopped():
            return None
        # Return whichever scored higher.
        if color is None:
            return _shifted(gray, dx, dy)
        if gray is None:
            return _shifted(color, dx, dy)
        return _shifted(color if color.score >= gray.score else gray, dx, dy)

    # No cv2: single-scale fallback (uses a default confidence just for the box).
    return _shifted(_best_match_pyautogui(template_path, shot, confidence=0.8),
                    dx, dy)


def find(template_path: str, confidence: float = 0.8, screenshot=None,
         grayscale: bool = True, scales: Optional[List[float]] = None,
         region=None, should_continue=None) -> Optional[Match]:
    """
    Return a Match if the template is present at or above `confidence`,
    else None. Drop-in replacement for the old pyautogui.locate() calls; the
    returned box uses the same screenshot pixel coordinates. `region` narrows
    the search to a PHYSICAL (x, y, w, h) box without changing that.

    `should_continue` makes the search abandonable — see `best_match`. An
    abandoned search reports "not found", which is the safe reading: the
    alternative is a partial best that a Wait-for-image step would *click*.
    """
    if not ENABLED or not template_path:
        return None
    shot = screenshot if screenshot is not None else grab_all_screens()
    if shot is None:
        return None

    if _HAS_CV2:
        m = best_match(template_path, screenshot=shot, grayscale=grayscale,
                       scales=scales, region=region,
                       should_continue=should_continue)
        return m if (m is not None and m.score >= confidence) else None

    # Fallback path honours confidence directly via pyautogui.
    shot, dx, dy = _crop_to_region(shot, region)
    return _shifted(_best_match_pyautogui(template_path, shot,
                                          confidence=confidence), dx, dy)


def present(template_path: str, confidence: float = 0.8, screenshot=None,
            region=None, should_continue=None) -> bool:
    """Convenience boolean: is the template on screen at/above confidence?"""
    return find(template_path, confidence=confidence, screenshot=screenshot,
                region=region, should_continue=should_continue) is not None
