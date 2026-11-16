"""
On-screen text recognition (OCR) for Macronaut.

Architecture (engine-agnostic abstraction layer):

    IOcrEngine            -- the contract every engine fulfils
      ├─ WindowsOcrService  -- native Windows.Media.Ocr (winsdk)
      └─ NullOcrService     -- DISABLED: nothing available

There used to be a RapidOCR/onnxruntime fallback here. It was removed in 2.0.12:
its .onnx models and config.yaml were never collected into the frozen build, so
it could not start in any published .exe — while still costing every user 13.2 MB
of onnxruntime, shapely and pyclipper to run nothing. If a second engine is ever
wanted, add it as an IOcrEngine subclass and put it in _ENGINE_PRIORITY; the
selector below is already written for more than one, and the packaging cost is
the thing to check first (see macronaut.spec).

The rest of the app NEVER calls a specific engine. It calls:

    eng = ocr.get_engine()
    eng.read_regions(screenshot, region=...)
    eng.find_text(target, screenshot, region=..., fuzzy=..., ...)

Swapping in a new engine (e.g. Tesseract) means adding ONE new IOcrEngine
subclass and registering it in the selector — no call-site changes.

All bounding boxes are returned in the pixel space of the screenshot that was
read (for an all-screens grab that is PHYSICAL pixels), matching matcher.py and
the existing click-coordinate conversion.
"""

import sys
import abc
import difflib as _difflib
from collections import namedtuple
from typing import List, Optional, Tuple

# Standardised text fragment returned by every engine.
TextMatch = namedtuple("TextMatch", ["left", "top", "width", "height", "text", "score"])

# Result of a multi-word phrase match.
#   matched  : bool — was the full phrase confirmed in sequence?
#   tier     : 1 Gold (all words >90%), 2 Compensatory (confirmed, some low),
#              3 Best-effort (no full sequence) / 0 nothing.
#   score    : weighted-average similarity of the phrase (0..1).
#   box      : TextMatch spanning the matched phrase, or None.
#   per_word : list of (word_text, similarity).
#   summary  : human-readable one-liner for the UI.
PhraseResult = namedtuple(
    "PhraseResult", ["matched", "tier", "score", "box", "per_word", "summary"])


def _phrase_summary(tier, score, per_word):
    pct = int(round(score * 100))
    if tier == 1:
        return f"✓ Gold match — every word ≥90% (phrase {pct}%)."
    if tier == 2:
        detail = ",  ".join(f"“{w}” {int(round(s*100))}%" for w, s in per_word)
        return f"✓ Match {pct}% (weighted average).\nWords:  {detail}"
    if per_word:
        detail = ",  ".join(f"“{w}” {int(round(s*100))}%" for w, s in per_word)
        return f"✗ Best effort {pct}% — closest pieces:  {detail}"
    return "✗ Not found."

# ── Engine-agnostic utilities ────────────────────────────────────────────────
def text_similarity(needle: str, hay: str) -> float:
    """
    Best similarity (0..1) of `needle` against any window of `hay`, tolerant of
    OCR character errors (substitutions, a dropped/extra letter). 1.0 means an
    exact substring. Lets a search for "Macronaut" still match an OCR misread
    like "Macconaut". Pure stdlib (difflib), no extra dependencies.
    """
    if not needle:
        return 0.0
    if needle in hay:
        return 1.0
    hay = hay[:400]                     # cap work on very long lines
    n = len(needle)
    H = len(hay)
    if H == 0:
        return 0.0
    sm = _difflib.SequenceMatcher(autojunk=False)
    sm.set_seq1(needle)
    best = 0.0
    window_lengths = {max(1, n - 2), max(1, n - 1), n, n + 1, n + 2}
    for wlen in window_lengths:
        if wlen >= H:
            sm.set_seq2(hay)
            r = sm.ratio()
            if r > best:
                best = r
            continue
        for i in range(0, H - wlen + 1):
            sm.set_seq2(hay[i:i + wlen])
            r = sm.ratio()
            if r > best:
                best = r
                if best >= 0.999:
                    return best
    return best


def to_physical_region(region_logical, screenshot_pil):
    """
    Convert a virtual-desktop LOGICAL (x, y, w, h) region — as emitted by the
    Qt region-selector overlay — into PHYSICAL pixel coords that line up with an
    all-screens screenshot, so it can be passed as `region` to read_regions /
    find_text. Mirrors the physical->logical math in recorder._click_physical.
    Returns (x, y, w, h) ints, or None if conversion isn't possible (callers
    then fall back to scanning the whole screen).
    """
    if not region_logical or screenshot_pil is None:
        return None
    try:
        import ctypes
        u32 = ctypes.windll.user32
        vd_x = u32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN (logical origin)
        vd_y = u32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        vd_w = u32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN (logical size)
        vd_h = u32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        if vd_w <= 0 or vd_h <= 0:
            return None
        sx = screenshot_pil.width  / vd_w   # logical -> physical scale
        sy = screenshot_pil.height / vd_h
        lx, ly, lw, lh = (float(v) for v in region_logical)
        px = int(round((lx - vd_x) * sx))
        py = int(round((ly - vd_y) * sy))
        pw = int(round(lw * sx))
        ph = int(round(lh * sy))
        if pw <= 0 or ph <= 0:
            return None
        return (max(0, px), max(0, py), pw, ph)
    except Exception:
        return None


def _crop_rgb(screenshot, region):
    """Crop `screenshot` to `region` (pixel x,y,w,h) and ensure RGB.
    Returns (image, offset_x, offset_y)."""
    img = screenshot
    ox = oy = 0
    if region:
        x, y, w, h = (int(v) for v in region)
        x, y = max(0, x), max(0, y)
        img = screenshot.crop((x, y, x + w, y + h))
        ox, oy = x, y
    if getattr(img, "mode", "RGB") != "RGB":
        try:
            img = img.convert("RGB")
        except Exception:
            pass
    return img, ox, oy


# ═══════════════════════════════════════════════════════════════════════════════
#  Phase 1 — the contract
# ═══════════════════════════════════════════════════════════════════════════════

class IOcrEngine(abc.ABC):
    """
    Canonical OCR interface. Concrete engines implement just the variant parts —
    `_check_availability()` and `_recognize()` — and inherit the standardised,
    engine-agnostic `read_regions` / `find_text` / `closest_text` / `present`.
    """

    name = "abstract"

    def __init__(self):
        self._available = False
        self._init_error = ""
        try:
            self._available = bool(self._check_availability())
        except Exception as exc:                       # never raise from a ctor
            self._init_error = self._init_error or f"{self.name} init failed: {exc}"
            self._available = False

    # ── Status ────────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return self._available

    @property
    def init_error(self) -> str:
        return self._init_error

    # ── Variant parts (engine-specific) ───────────────────────────────
    @abc.abstractmethod
    def _check_availability(self) -> bool:
        """Return True if this engine can run on this machine right now."""
        raise NotImplementedError

    @abc.abstractmethod
    def _recognize(self, pil_img) -> List[Tuple]:
        """
        Run the engine on an already-cropped RGB PIL image and return a list of
        (left, top, width, height, text, score) tuples in that image's pixels.
        """
        raise NotImplementedError

    def _warmup(self):
        """Optionally build heavy resources ahead of time. Default: no-op."""
        return

    # ── The size the engine will accept ───────────────────────────────
    #
    # ⚠ Windows.Media.Ocr refuses any bitmap whose longest side is over
    # OcrEngine.MaxImageDimension — 10000 px — and "refuses" means it hands
    # back an EMPTY result. No exception, no partial read. Measured
    # 8 September 2026 on a 400px strip with one phrase on it:
    #
    #     width  9990   2 words   find_text FOUND
    #     width 10001   0 words   find_text none
    #
    # Silence is the worst possible failure here, because it is exactly what
    # "the text is not on screen" looks like: Detect-text waits out its whole
    # timeout and If/Else takes the false branch, on a desktop where the user
    # can read the words themselves. Two ways in — a native grab of a wide
    # desktop (three 3440px ultrawides is 10320), and the zoom rescue below,
    # which doubles the grab and so puts every desktop wider than 5000px over
    # the line, two 2560x1440 monitors included.
    #
    # So an oversized image is scaled to fit and the boxes are scaled back,
    # rather than passed on to be swallowed. A shrunk read is a worse read;
    # it is not remotely as bad as no read.
    max_dimension = 0                   # 0 = this engine has no limit

    def _fit(self, img):
        """Return (an image the engine will accept, the scale applied to it).

        Free — and returns the very same object — when nothing needs doing,
        which is every ordinary screen and every poll of every flow.
        """
        lim = self.max_dimension
        if not lim:
            return img, 1.0
        longest = max(img.width, img.height)
        if longest <= lim:
            return img, 1.0
        scale = lim / float(longest)
        try:
            from PIL import Image as _PILImage
            small = img.resize((max(1, int(img.width * scale)),
                                max(1, int(img.height * scale))),
                               _PILImage.LANCZOS)
        except Exception:
            return img, 1.0
        return small, scale

    # ── Contract (standardised; shared by every engine) ───────────────
    def read_regions(self, screenshot, region=None) -> List[TextMatch]:
        """Recognise text; return TextMatch fragments in screenshot pixel space.
        `region` = (x, y, w, h) in screenshot pixels to limit the search."""
        if not self._available or screenshot is None:
            return []
        img, ox, oy = _crop_rgb(screenshot, region)
        img, sc = self._fit(img)
        try:
            raw = self._recognize(img)
        except Exception:
            return []
        out: List[TextMatch] = []
        for item in raw:
            try:
                l, t, w, h, text, score = item
                out.append(TextMatch(int(l / sc) + ox, int(t / sc) + oy,
                                     max(1, int(w / sc)), max(1, int(h / sc)),
                                     str(text), float(score)))
            except Exception:
                continue
        return out

    def _recognize_words(self, pil_img) -> List[Tuple]:
        """Word-level recognition: (l, t, w, h, word, score) in image pixels.
        Default derives words from `_recognize` lines by splitting the text and
        apportioning the line box; engines with real word boxes (Windows)
        override this for exact coordinates."""
        words: List[Tuple] = []
        for item in self._recognize(pil_img):
            try:
                l, t, w, h, text, score = item
            except Exception:
                continue
            toks = str(text).split()
            if not toks:
                continue
            if len(toks) == 1:
                words.append((l, t, w, h, text, score))
                continue
            total = sum(len(tk) for tk in toks) or 1
            char_px = w / (len(str(text)) or 1)     # rough px per character
            x = float(l)
            for tk in toks:
                wt = max(1, int(w * (len(tk) / total)))
                words.append((int(x), t, wt, h, tk, score))
                x += wt + char_px                   # advance past an approx space
        return words

    def read_words(self, screenshot, region=None) -> List[TextMatch]:
        """Recognise individual WORDS as TextMatch items in screenshot pixels."""
        if not self._available or screenshot is None:
            return []
        img, ox, oy = _crop_rgb(screenshot, region)
        img, sc = self._fit(img)
        try:
            raw = self._recognize_words(img)
        except Exception:
            return []
        out: List[TextMatch] = []
        for item in raw:
            try:
                l, t, w, h, text, score = item
                if not str(text).strip():
                    continue
                out.append(TextMatch(int(l / sc) + ox, int(t / sc) + oy,
                                     max(1, int(w / sc)), max(1, int(h / sc)),
                                     str(text), float(score)))
            except Exception:
                continue
        out.sort(key=lambda m: (m.top, m.left))     # reading order
        return out

    def match_phrase(self, target, screenshot, region=None, case_sensitive=False,
                     word_thresh: float = 0.8, med_thresh: float = 0.6,
                     _zoom: bool = True) -> PhraseResult:
        """Read the screen for `target`, and if that finds nothing, read it
        again bigger. See `_zoom_rescue` for why and for what it costs."""
        pr = self._match_phrase_once(target, screenshot, region, case_sensitive,
                                     word_thresh, med_thresh)
        if pr.matched or not _zoom:
            return pr
        return self._zoom_rescue(target, screenshot, region, case_sensitive,
                                 word_thresh, med_thresh) or pr

    # ⚠ The screenshot went to the OCR engine at native size, and screen UI
    # text is small — these engines are trained on document scans, where a
    # capital letter is forty pixels tall rather than eleven.
    #
    # Measured 8 September 2026 against Windows OCR: eight UI phrases rendered
    # at 9-13 px in Segoe UI, Tahoma and Consolas, 120 readings in all.
    # Read at native size, 58 of 120 came back. Read at twice the size, 98.
    # It is not a marginal gain and it is not evenly spread: at 12 px and up
    # the plain read is already near-perfect and the enlargement changes
    # nothing, while at 9-10 px the plain read gets almost none of them.
    #
    # So this is a rescue, not a policy: the plain read runs first and pays
    # nothing, and the second only happens when the answer would otherwise be
    # "not found" — which is exactly when somebody is staring at a step that
    # cannot see text they can read perfectly well themselves. It costs about
    # 0.4 ms when the first read succeeds and about 190 ms when it does not.
    #
    # ⚠ LANCZOS, and that was measured too, not assumed. An integer upscale
    # with NEAREST replicates pixels exactly and looked equal on the first
    # sample — 4 phrases out of 4 — which is how it nearly got chosen. Over
    # the full 120 it scores 88 against LANCZOS's 98, and on 9 px Consolas it
    # gets none at all where LANCZOS gets five. Sharper is not better here;
    # the engine wants the smooth edges it was trained on.
    # ⚠ It also catches something the paragraph above does not describe, and
    # which is worth knowing before anyone decides the rescue is only for tiny
    # text. Windows OCR sometimes returns **nothing at all** for perfectly
    # legible text, and the trigger does not follow a rule. Measured
    # 8 September 2026 — 22 px Segoe UI, one phrase, canvas 1080 tall, plain
    # read with the rescue switched off:
    #
    #        y      1920   2560   3840   5760
    #     1000        ok     ok     ok     ok
    #     1020        ok     ok   MISS   MISS
    #     1040        ok     ok     ok     ok
    #
    # Twenty pixels either side of that row is fine, and the row *closer* to
    # the edge is fine, so it is not a margin, not a canvas size and not text
    # size — it is the engine, and it reproduced 3 times out of 3. The rescue
    # reads those cases correctly.
    #
    # ⚠ Do not try to predict it. Anything that looked like a rule here would
    # be fitted to one machine's build of one engine; the rescue already covers
    # it by re-reading, which is the only thing that generalises.
    ZOOM = 2
    # Upscaling a whole 4K grab is 33 megapixels of work on every poll of a
    # wait-for-text that has not appeared yet. Bounded, and skipped entirely
    # when the bound would make the enlargement too small to be worth it —
    # a search area crops first and brings a big screen back under it.
    ZOOM_MAX_PX = 12_000_000
    # ⚠ 1.5 here was a guess, and it switched the rescue off for every 4K and
    # larger desktop — the budget allows a 4K grab exactly x1.203, which fell
    # under the floor. Measured 8 September 2026 on a 3840x2160 canvas, the
    # eight phrases at 9-13 px in three fonts: native 61/120, x1.203 **90/120**.
    # Half the misses, for 187 ms, on a path that only runs when the answer
    # would otherwise be "not found".
    #
    # There is no cliff to sit above: on a smaller canvas x1.05 already
    # recovers 5 misses of 53, x1.10 eleven, x1.15 fifteen, x1.30 thirty. So
    # this is a cost/benefit cut and nothing more principled — below it the
    # second read costs its full time to recover almost nothing.
    ZOOM_MIN_FACTOR = 1.15

    def _zoom_factor(self, w: int, h: int) -> float:
        px = max(1, w * h)
        factor = min(float(self.ZOOM), (self.ZOOM_MAX_PX / px) ** 0.5)
        # Never enlarge past what the engine will look at: over its limit the
        # read comes back empty, so the rescue would cost ~700 ms a poll to
        # learn nothing. See `max_dimension`.
        if self.max_dimension:
            factor = min(factor, self.max_dimension / float(max(1, w, h)))
        return factor if factor >= self.ZOOM_MIN_FACTOR else 0.0

    def _zoom_rescue(self, target, screenshot, region, case_sensitive,
                     word_thresh, med_thresh):
        """Re-read an enlarged copy. None if it could not or did not help."""
        try:
            from PIL import Image as _PILImage
            img, ox, oy = _crop_rgb(screenshot, region)
            factor = self._zoom_factor(img.width, img.height)
            if not factor:
                return None
            big = img.resize((int(img.width * factor), int(img.height * factor)),
                             _PILImage.LANCZOS)
            pr = self._match_phrase_once(target, big, None, case_sensitive,
                                         word_thresh, med_thresh)
        except Exception:
            return None
        if not pr.matched or pr.box is None:
            # The honest first answer is kept. A near-miss reported from an
            # enlarged image would describe a picture the user is not looking
            # at, and the summary is shown to them verbatim.
            return None
        b = pr.box
        return pr._replace(box=TextMatch(
            int(round(b.left / factor)) + ox, int(round(b.top / factor)) + oy,
            max(1, int(round(b.width / factor))),
            max(1, int(round(b.height / factor))), b.text, b.score))

    def _match_phrase_once(self, target, screenshot, region=None,
                           case_sensitive=False, word_thresh: float = 0.8,
                           med_thresh: float = 0.6) -> PhraseResult:
        """
        Sequential, coordinate-aware phrase matcher.

        Instead of demanding a perfect match of the whole sentence (which fails
        as OCR character errors accumulate), this finds the first target word,
        then confirms each following target word is BOTH a fuzzy match AND
        physically adjacent (same line within a tolerance buffer, to the right
        within a small gap). The phrase score is the length-weighted average of
        the per-word similarities. See PhraseResult for the tiers.
        """
        words = self.read_words(screenshot, region)
        raw_targets = target.split()
        if not raw_targets or not words:
            msg = "No text detected." if not words else "Enter text to find."
            return PhraseResult(False, 0, 0.0, None, [], msg)
        norm = (lambda s: s) if case_sensitive else (lambda s: s.lower())
        tgs = [norm(t) for t in raw_targets]

        heights = sorted(m.height for m in words)
        medh = heights[len(heights) // 2] if heights else 12
        vtol = max(5, int(0.7 * medh))      # same-line tolerance (jitter buffer)
        htol = 5                            # horizontal overlap tolerance (±5 px)
        max_gap = max(10, int(2.2 * medh))  # max space between adjacent words

        def cy(m):
            return m.top + m.height / 2.0

        # ── single word ───────────────────────────────────────────────
        if len(tgs) == 1:
            best = None
            for m in words:
                sim = text_similarity(tgs[0], norm(m.text))
                if sim >= word_thresh and (best is None or sim > best[1]
                                           or (sim == best[1] and m.score > best[0].score)):
                    best = (m, sim)
            if best:
                m, sim = best
                tier = 1 if sim > 0.9 else 2
                pw = [(m.text, sim)]
                return PhraseResult(True, tier, sim, m, pw,
                                    _phrase_summary(tier, sim, pw))
            bw, bq = self.closest_text(raw_targets[0], words, case_sensitive)
            pw = [(bw.text, bq)] if bw else []
            return PhraseResult(False, 3, bq, None, pw, _phrase_summary(3, bq, pw))

        # ── multi-word sequential match ───────────────────────────────
        best_seq = None                     # (avg_score, [(TextMatch, sim), ...])
        for start in words:
            s0 = text_similarity(tgs[0], norm(start.text))
            if s0 < med_thresh:
                continue
            seq = [(start, s0)]
            prev = start
            ok = True
            for ti in range(1, len(tgs)):
                cand, cbest = None, 0.0
                for m in words:
                    if m is prev:
                        continue
                    if abs(cy(m) - cy(prev)) > vtol:        # must be on same line
                        continue
                    gap = m.left - (prev.left + prev.width)  # must follow to right
                    if gap < -htol or gap > max_gap:
                        continue
                    sim = text_similarity(tgs[ti], norm(m.text))
                    if sim >= med_thresh and sim > cbest:
                        cbest, cand = sim, m
                if cand is None:
                    ok = False
                    break
                seq.append((cand, cbest))
                prev = cand
            if not ok:
                continue
            weights = [len(t) for t in tgs]
            avg = sum(s * wt for (_, s), wt in zip(seq, weights)) / (sum(weights) or 1)
            if best_seq is None or avg > best_seq[0]:
                best_seq = (avg, seq)

        if best_seq is not None and best_seq[0] >= med_thresh:
            avg, seq = best_seq
            l = min(m.left for m, _ in seq)
            t = min(m.top for m, _ in seq)
            r = max(m.left + m.width for m, _ in seq)
            b = max(m.top + m.height for m, _ in seq)
            text = " ".join(m.text for m, _ in seq)
            box = TextMatch(l, t, r - l, b - t, text, avg)
            tier = 1 if (all(s > 0.9 for _, s in seq) and avg >= word_thresh) else 2
            pw = [(m.text, s) for m, s in seq]
            return PhraseResult(True, tier, avg, box, pw,
                                _phrase_summary(tier, avg, pw))

        # ── Tier 3 best effort: closest single target word anywhere ────
        best_single = None
        for raw in raw_targets:
            bw, bq = self.closest_text(raw, words, case_sensitive)
            if bw and (best_single is None or bq > best_single[1]):
                best_single = (bw, bq)
        if best_single and best_single[1] >= med_thresh:
            pw = [(best_single[0].text, best_single[1])]
            return PhraseResult(False, 3, best_single[1], None, pw,
                                _phrase_summary(3, best_single[1], pw))
        return PhraseResult(False, 3, 0.0, None, [],
                            "✗ No part of the phrase was found.")

    def find_text(self, target: str, screenshot, region=None,
                  case_sensitive: bool = False, min_score: float = 0.5,
                  fuzzy: bool = True, fuzz_ratio: float = 0.8) -> Optional[TextMatch]:
        """
        Best TextMatch for `target`, at/above `min_score`.

        Multi-word phrases use sequential, coordinate-aware matching (each word
        matched independently and confirmed physically adjacent), so cumulative
        OCR error on long sentences no longer breaks the match. Single words use
        precise word matching, then fall back to line-level substring/fuzzy.
        Returns None if nothing qualifies.

        ⚠ `min_score` means two different things on the two paths, and it is
        harmless only by accident. The phrase path compares it against a text
        SIMILARITY (`PhraseResult.score`, how well the words matched); the
        single-word line fallback compares it against `TextMatch.score`, which
        is the engine's own CONFIDENCE that it read the pixels correctly. Two
        unrelated quantities behind one argument.

        It cannot be observed today because Windows OCR reports a flat 1.0 for
        every fragment — checked 8 September 2026, lines and words alike — so
        the confidence filter never rejects anything. Add an engine that
        reports real confidences and one number will start meaning both, with
        the caller's 0.5 quietly doing something different depending on how
        many words they typed. Split the argument in that same change; this is
        the same shape as the RapidOCR engine that reported itself available
        while never having been able to run.
        """
        if not target or not self._available:
            return None
        word_thresh = fuzz_ratio if fuzzy else 0.99
        pr = self.match_phrase(target, screenshot, region,
                               case_sensitive=case_sensitive, word_thresh=word_thresh)
        if pr.matched and pr.score >= min_score:
            return pr.box
        if len(target.split()) >= 2:
            return None     # phrase didn't confirm; line-level won't do better

        # Single-word line-level fallback (handles partial words / whole lines).
        needle = target if case_sensitive else target.lower()
        best: Optional[TextMatch] = None
        best_quality = 0.0
        for tm in self.read_regions(screenshot, region):
            if tm.score < min_score:
                continue
            hay = tm.text if case_sensitive else tm.text.lower()
            if needle in hay:
                quality = 1.0
            elif fuzzy:
                quality = text_similarity(needle, hay)
                if quality < fuzz_ratio:
                    continue
            else:
                continue
            if (best is None or quality > best_quality
                    or (quality == best_quality and tm.score > best.score)):
                best = tm
                best_quality = quality
        return best

    def closest_text(self, target: str, regions: List[TextMatch],
                     case_sensitive: bool = False):
        """From already-read `regions`, return (TextMatch, similarity) nearest to
        `target`, regardless of threshold. Used by the UI tester to show a
        near-miss. (None, 0.0) if there are no regions."""
        if not target or not regions:
            return None, 0.0
        needle = target if case_sensitive else target.lower()
        best_tm = None
        best_q = 0.0
        for tm in regions:
            hay = tm.text if case_sensitive else tm.text.lower()
            q = text_similarity(needle, hay)
            if q > best_q:
                best_q = q
                best_tm = tm
        return best_tm, best_q

    def present(self, target: str, screenshot, region=None,
                case_sensitive: bool = False, min_score: float = 0.5,
                fuzzy: bool = True, fuzz_ratio: float = 0.8) -> bool:
        """Convenience boolean: is `target` text on screen right now?"""
        return self.find_text(target, screenshot, region=region,
                              case_sensitive=case_sensitive, min_score=min_score,
                              fuzzy=fuzzy, fuzz_ratio=fuzz_ratio) is not None


# ═══════════════════════════════════════════════════════════════════════════════
#  Phase 2 — concrete engines
# ═══════════════════════════════════════════════════════════════════════════════

def _run_sync(coro):
    """Run a winsdk async operation to completion on a private event loop, so it
    works from the Qt main thread and from the playback QThread alike."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class WindowsOcrService(IOcrEngine):
    """PRIMARY engine: the native Windows.Media.Ocr API (Win10/11). Accurate on
    screen text, ships with the OS (nothing to bundle), offline, fast.

    Works with either WinRT projection: the newer namespaced `winrt-*` packages
    or the older monolithic `winsdk` (the import root differs, the API is the
    same).

    `winrt` is tried FIRST because it is what ships. winsdk projects the entire
    WinRT surface through one 38.5 MB .pyd to give us four namespaces; the split
    packages weigh 3.1 MB for the same four, which is ~10 MB off every download.
    winsdk stays as a fallback so a source checkout that only has it still runs.
    """

    name = "Windows.Media.Ocr"
    # WinRT projection roots to try, in order.
    _ROOTS = ("winrt", "winsdk")

    def __init__(self):
        self._engine = None
        self._root = None          # which projection root actually worked
        self._mods = None          # (ocr, imaging, streams, globalization)
        super().__init__()

    def _load_modules(self):
        """Import the WinRT submodules from whichever projection is present.
        Returns (ocr, imaging, streams, globalization|None) or raises ImportError.

        ⚠ `windows.foundation` is imported even though nothing here names it.
        It is what makes `recognize_async` awaitable, and the split winrt-*
        packages do NOT declare it as a dependency of the OCR package — so a
        projection missing it imports cleanly, constructs an OcrEngine, reports
        itself available, and then raises ModuleNotFoundError on the first
        recognise. read_regions catches that and returns [] (correctly — an OCR
        failure must not kill a running flow), so the visible symptom is
        "text detection finds nothing, ever" with no error anywhere. Loading it
        here turns that into an ordinary unavailable engine that says why.
        """
        import importlib
        last_err = None
        for root in self._ROOTS:
            try:
                ocr_mod = importlib.import_module(root + ".windows.media.ocr")
                imaging = importlib.import_module(root + ".windows.graphics.imaging")
                streams = importlib.import_module(root + ".windows.storage.streams")
                importlib.import_module(root + ".windows.foundation")
                try:
                    glob = importlib.import_module(root + ".windows.globalization")
                except Exception:
                    glob = None
                self._root = root
                return ocr_mod, imaging, streams, glob
            except Exception as exc:
                last_err = exc
        raise ImportError(last_err if last_err else "no WinRT projection installed")

    def _create_engine(self):
        ocr_mod, _imaging, _streams, glob = self._mods
        eng = ocr_mod.OcrEngine.try_create_from_user_profile_languages()
        if eng is None and glob is not None:
            # Fall back to an explicit English engine if the profile yields none.
            try:
                eng = ocr_mod.OcrEngine.try_create_from_language(glob.Language("en-US"))
            except Exception:
                eng = None
        return eng

    def _check_availability(self) -> bool:
        if not sys.platform.startswith("win"):
            self._init_error = "Windows OCR needs Windows."
            return False
        try:
            self._mods = self._load_modules()
        except Exception as exc:
            self._init_error = (
                "Windows OCR projection not installed or incomplete "
                f"({exc}). Install: winrt-Windows.Media.Ocr, "
                "winrt-Windows.Graphics.Imaging, winrt-Windows.Storage.Streams, "
                "winrt-Windows.Globalization and winrt-Windows.Foundation.")
            return False
        try:
            self._engine = self._create_engine()
        except Exception as exc:
            self._init_error = f"Windows OCR could not initialise: {exc}"
            return False
        if self._engine is None:
            self._init_error = ("No Windows OCR language pack found "
                                "(Settings ▸ Time & language ▸ Language ▸ add English).")
            return False
        # Asked once, here, rather than on every read: it is a WinRT static
        # property, and `_fit` sits on the hot path of every Detect-text poll.
        # Read from the API rather than hardcoded, so a future Windows that
        # accepts more (or less) is honoured without a code change.
        try:
            self.max_dimension = int(self._mods[0].OcrEngine.max_image_dimension)
        except Exception:
            self.max_dimension = 10000      # documented value, since Win10 1507
        return True

    def _ocr_result(self, pil_img):
        """Run the Windows OCR engine once; return the raw OcrResult (or None)."""
        _ocr_mod, _imaging, _streams, _glob = self._mods
        w, h = pil_img.size
        if w <= 0 or h <= 0:
            return None
        bgra = pil_img.convert("RGBA").tobytes("raw", "BGRA")  # Windows wants BGRA8

        async def _go():
            writer = _streams.DataWriter()
            writer.write_bytes(bgra)
            buffer = writer.detach_buffer()
            bitmap = _imaging.SoftwareBitmap.create_copy_from_buffer(
                buffer, _imaging.BitmapPixelFormat.BGRA8, w, h)
            return await self._engine.recognize_async(bitmap)

        return _run_sync(_go())

    def _recognize(self, pil_img) -> List[Tuple]:
        result = self._ocr_result(pil_img)
        items: List[Tuple] = []
        if result is None:
            return items
        for line in result.lines:
            words = list(line.words)
            if not words:
                continue
            lefts, tops, rights, bottoms = [], [], [], []
            for wd in words:
                r = wd.bounding_rect
                lefts.append(r.x); tops.append(r.y)
                rights.append(r.x + r.width); bottoms.append(r.y + r.height)
            l, t = min(lefts), min(tops)
            ww, hh = max(rights) - l, max(bottoms) - t
            # Windows OCR exposes no per-line confidence; report 1.0 so the
            # min_score gate is a no-op and fuzzy matching does the real work.
            items.append((l, t, ww, hh, line.text, 1.0))
        return items

    def _recognize_words(self, pil_img) -> List[Tuple]:
        """Exact per-word boxes straight from Windows OCR (no apportioning)."""
        result = self._ocr_result(pil_img)
        items: List[Tuple] = []
        if result is None:
            return items
        for line in result.lines:
            for wd in line.words:
                r = wd.bounding_rect
                items.append((r.x, r.y, r.width, r.height, wd.text, 1.0))
        return items


class NullOcrService(IOcrEngine):
    """DISABLED: no OCR engine available."""

    name = "none"

    def _check_availability(self) -> bool:
        self._init_error = ("No OCR engine available. Windows OCR needs the "
                            "language pack for your display language "
                            "(Settings ▸ Language ▸ Optional features ▸ "
                            "Basic typing / Optical character recognition).")
        return False

    def _recognize(self, pil_img) -> List[Tuple]:
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  Phase 3 — selector / factory (Windows -> disabled)
# ═══════════════════════════════════════════════════════════════════════════════

# Priority order; first available one wins. One entry today — kept as a tuple so
# adding an engine is a one-line change rather than a rewrite of get_engine().
_ENGINE_PRIORITY = (WindowsOcrService,)

_ENGINE: Optional[IOcrEngine] = None


def get_engine() -> IOcrEngine:
    """Return the active OCR engine singleton, picking the highest-priority one
    that is available on this machine (Windows OCR, then a disabled Null
    engine)."""
    global _ENGINE
    if _ENGINE is None:
        chosen = None
        for cls in _ENGINE_PRIORITY:
            try:
                eng = cls()
            except Exception:
                continue
            if eng.available:
                chosen = eng
                break
        _ENGINE = chosen if chosen is not None else NullOcrService()
    return _ENGINE


def reset_engine():
    """Drop the cached engine so the next get_engine() re-selects (e.g. after a
    dependency is installed)."""
    global _ENGINE
    _ENGINE = None


def available() -> bool:
    return get_engine().available


def active_backend() -> str:
    return get_engine().name


def status_message() -> str:
    """Human-readable line describing the active engine / why it's unavailable."""
    eng = get_engine()
    if eng.available:
        return f"Text recognition: {eng.name}."
    return eng.init_error or "Text recognition unavailable."


def warmup() -> bool:
    """⚠ Does nothing for the engine that ships, and returns True anyway.

    The docstring here used to promise it built heavy resources ahead of time.
    `_warmup()` is a base-class no-op and the Windows OCR engine — the only one
    left since RapidOCR was removed in 2.0.12 — does not override it. So this
    returns True having done nothing, which is the worst shape a claim can
    take: someone wiring it up to remove a cold start would measure no change
    and conclude the cold start was imaginary.

    Nothing calls it. Measured 3 September 2026, and the cold start is real but
    small and *not* where this function looks for it: `warmup()` on a cold
    engine takes **0.0 ms**, while the first `read_words` takes **33 ms**
    against **4 ms** once warm. The ~29 ms lives in the first recognition
    itself, not in constructing the engine, so calling this earlier moves
    nothing.

    Kept because a future engine that genuinely has resources to build would
    override `_warmup()` and this would start meaning something. Until then it
    is an interface with one implementation that declines to implement it.
    """
    eng = get_engine()
    if not eng.available:
        return False
    try:
        eng._warmup()
        return True
    except Exception:
        return False


# ── Backward-compatible module-level facade ─────────────────────────────────
# Existing/diagnostic code may call ocr.read_regions(...) etc.; these simply
# route through the active engine, so there are still no direct engine refs.
def read_regions(screenshot, region=None) -> List[TextMatch]:
    return get_engine().read_regions(screenshot, region=region)


def find_text(target, screenshot, region=None, case_sensitive=False,
              min_score=0.5, fuzzy=True, fuzz_ratio=0.8) -> Optional[TextMatch]:
    return get_engine().find_text(target, screenshot, region=region,
                                  case_sensitive=case_sensitive, min_score=min_score,
                                  fuzzy=fuzzy, fuzz_ratio=fuzz_ratio)


def read_words(screenshot, region=None):
    return get_engine().read_words(screenshot, region=region)


def match_phrase(target, screenshot, region=None, case_sensitive=False,
                 word_thresh=0.8, med_thresh=0.6):
    return get_engine().match_phrase(target, screenshot, region=region,
                                     case_sensitive=case_sensitive,
                                     word_thresh=word_thresh, med_thresh=med_thresh)


def closest_text(target, regions, case_sensitive=False):
    return get_engine().closest_text(target, regions, case_sensitive=case_sensitive)


def present(target, screenshot, region=None, case_sensitive=False,
            min_score=0.5, fuzzy=True, fuzz_ratio=0.8) -> bool:
    return get_engine().present(target, screenshot, region=region,
                                case_sensitive=case_sensitive, min_score=min_score,
                                fuzzy=fuzzy, fuzz_ratio=fuzz_ratio)


# Whether ANY OCR engine is usable (computed once at import; cheap — Windows
# engine creation is fast, RapidOCR only does a find_spec here).
ENABLED = get_engine().available
ACTIVE_BACKEND = get_engine().name
