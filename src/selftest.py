"""Headless diagnostic self-test — `Macronaut.exe --selftest`.

Image matching and OCR are the two features that fail *silently* in a frozen
build. `matcher.py` catches ImportError and degrades; `ocr.py` falls back to a
null engine. Nothing crashes, no dialog appears — the features simply stop
working, and only in the packaged .exe. That is how a build shipped with numpy
excluded while cv2 was bundled, and nobody could have noticed by launching it.

So this does not ask "does the module import?" — a bundled-but-broken dependency
answers yes. It runs each feature end to end against generated input with a
known-correct answer:

  * image match — screenshot the desktop, crop a patch out of it, then search
    for that patch and assert it is found at the coordinates it was cut from.
  * OCR        — render known text to an image, read it back, compare.

Exit code 0 = everything a release needs is working, 1 = something is broken.
Run it against `dist/Macronaut.exe`, not `python main.py`: passing from source
proves nothing about the bundle.
"""
from __future__ import annotations

import os
import sys
import traceback

# Anything false here means a broken release, not a degraded one.
CRITICAL = {"python", "PySide6", "numpy", "cv2", "image match", "legal files",
            "licensing"}


_LINES: list[str] = []


def _say(msg: str = "") -> None:
    """Record a line, and print it if there is anywhere to print to."""
    _LINES.append(msg)
    print(msg)


def _ensure_console() -> None:
    """Give a windowed build somewhere to write.

    Macronaut is built `console=False`, so a frozen run has no console of its
    own and CPython sets sys.stdout to None -- print() then returns silently. A
    self-test that reports nothing is worse than none at all, so attach to the
    console that launched us. Falls back to the report file if that fails
    (double-clicked, redirected, or launched from a GUI shell).
    """
    if sys.stdout is not None:
        return
    try:
        import ctypes
        ATTACH_PARENT_PROCESS = -1
        if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                              errors="replace", buffering=1)
            sys.stderr = sys.stdout
    except Exception:
        pass


class _Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, ok, detail))
        _say(f"  {'PASS' if ok else 'FAIL'}  {name:<14} {detail}")

    def check(self, name: str, fn) -> bool:
        """Run fn() -> detail string. Any exception is a failure, not a crash."""
        try:
            self.add(name, True, fn() or "")
            return True
        except Exception as exc:
            self.add(name, False, f"{type(exc).__name__}: {exc}")
            if os.environ.get("MACRONAUT_SELFTEST_TRACE"):
                traceback.print_exc()
            return False


# ── individual checks ─────────────────────────────────────────────────────────
def _check_python() -> str:
    frozen = getattr(sys, "frozen", False)
    where = getattr(sys, "_MEIPASS", "not frozen")
    return f"{sys.version.split()[0]}  frozen={bool(frozen)}  bundle={where}"


def _check_qt() -> str:
    import PySide6
    from PySide6 import QtCore
    # PyQt5 is GPL and must never be what actually loads at runtime.
    assert "PyQt5" not in sys.modules, "PyQt5 is loaded — GPL binding leaked in"
    return f"PySide6 {PySide6.__version__} / Qt {QtCore.__version__}"


def _check_numpy() -> str:
    import numpy
    return f"numpy {numpy.__version__}"


def _check_cv2() -> str:
    import cv2
    return f"opencv {cv2.__version__}"


def _noise_image(w: int, h: int, seed: int):
    """Deterministic RGB noise.

    Noise, never a flat colour: TM_CCOEFF_NORMED correlates against the mean, so
    a constant template has zero variance and scores degenerately -- it "matches"
    anywhere, usually reporting (0,0). Seeded so any failure reproduces instead
    of being a coin toss.
    """
    import random
    from PIL import Image
    rnd = random.Random(seed)
    return Image.frombytes("RGB", (w, h), bytes(rnd.randrange(256)
                                                for _ in range(w * h * 3)))


def _check_image_match() -> str:
    """Prove cv2 template matching actually works, then prove capture works.

    Split deliberately. The matching half runs against a generated haystack so
    it has a known answer and cannot depend on what happens to be on screen --
    an earlier version cut its patch from a fixed screen position, hit a blank
    area one run, and failed on the degenerate-template effect above rather than
    on anything real. The capture half then exercises the live screen path,
    which is the part that only breaks when frozen.
    """
    import tempfile
    import matcher
    from PIL.ImageDraw import Draw as _ImageDraw

    if not matcher.ENABLED:
        raise RuntimeError("matcher.ENABLED is False - image matching is off")

    tmp_dir = tempfile.gettempdir()
    hay = _noise_image(640, 480, seed=20260731)
    px, py, side = 211, 133, 64          # deliberately not (0,0) or centred

    patch = os.path.join(tmp_dir, "macronaut_selftest_patch.png")
    neg = os.path.join(tmp_dir, "macronaut_selftest_neg.png")
    hay.crop((px, py, px + side, py + side)).save(patch)
    _noise_image(side, side, seed=1234567).save(neg)   # unrelated noise
    try:
        hit = matcher.find(patch, confidence=0.8, screenshot=hay)
        if hit is None:
            raise RuntimeError("a patch cut from an image was not found in "
                               "that same image")
        dx, dy = abs(hit.left - px), abs(hit.top - py)
        if dx > 4 or dy > 4:
            raise RuntimeError(f"found at ({hit.left},{hit.top}), expected "
                               f"({px},{py}) - off by ({dx},{dy})")

        # Negative control: a matcher that says yes to everything is as broken
        # as one that says no.
        if matcher.find(neg, confidence=0.95, screenshot=hay) is not None:
            raise RuntimeError("matched an image that is not present")
    finally:
        _rm(patch)
        _rm(neg)

    # ── the two silent-wrong-answer paths ────────────────────────────────
    #
    # ⚠ `_noise_image` above uses noise *specifically to avoid* the flat and
    # cut-out cases -- its docstring says so. Both were silent wrong answers
    # until 8 September 2026: a plain-coloured template scored a perfect 1.0
    # at (0,0) whether or not it was on screen, and a transparent one was
    # matched by whatever RGB the exporter left under the alpha. Neither
    # failed loudly, and both are exactly the kind of thing that works from
    # source and breaks frozen, which is what this file is for.
    from PIL import Image as _Im

    flat_hay = hay.copy()
    flat_hay.paste(_Im.new("RGB", (70, 40), (0, 128, 96)), (300, 210))
    flat = os.path.join(tmp_dir, "macronaut_selftest_flat.png")
    absent = os.path.join(tmp_dir, "macronaut_selftest_flat_absent.png")
    _Im.new("RGB", (70, 40), (0, 128, 96)).save(flat)
    _Im.new("RGB", (70, 40), (190, 40, 200)).save(absent)   # nowhere on it
    try:
        got = matcher.find(flat, confidence=0.8, screenshot=flat_hay)
        if got is None:
            raise RuntimeError("a plain-coloured template was not found")
        if abs(got.left - 300) > 4 or abs(got.top - 210) > 4:
            raise RuntimeError(
                f"plain template found at ({got.left},{got.top}), planted at "
                "(300,210) - the degenerate all-positions-score-1.0 answer")
        if matcher.find(absent, confidence=0.8, screenshot=flat_hay) is not None:
            raise RuntimeError("a plain colour that is NOT on screen was found")
    finally:
        _rm(flat)
        _rm(absent)

    cut_path = os.path.join(tmp_dir, "macronaut_selftest_cutout.png")
    cut = _Im.new("RGBA", (56, 56), (0, 0, 0, 0))
    _ImageDraw(cut).ellipse([4, 4, 51, 51], fill=(245, 190, 40, 255))
    cut.save(cut_path)
    try:
        cut_hay = hay.copy()
        cut_hay.paste(cut, (150, 300), cut)      # over noise, not over black
        got = matcher.find(cut_path, confidence=0.8, screenshot=cut_hay)
        if got is None:
            raise RuntimeError("a cut-out (transparent) template was not found "
                               "- the alpha channel is not being used as a mask")
        if abs(got.left - 150) > 4 or abs(got.top - 300) > 4:
            raise RuntimeError(
                f"cut-out found at ({got.left},{got.top}), planted at (150,300)")
    finally:
        _rm(cut_path)

    # Live capture. Only sanity-checked for size, because screen CONTENT is not
    # ours to assert on -- a blank desktop is not a broken build.
    shot = matcher.grab_all_screens()
    if shot is None:
        raise RuntimeError("grab_all_screens() returned None (no PIL?)")
    w, h = shot.size
    if w < 320 or h < 240:
        raise RuntimeError(f"screen capture looks wrong: {w}x{h}")

    scale = "multi-scale" if matcher.MULTISCALE else "single-scale"
    return f"{scale}, patch relocated at ({hit.left},{hit.top}); capture {w}x{h}"


def _check_ocr() -> str:
    """Render known text, read it back."""
    import ocr

    if not ocr.available():
        raise RuntimeError(ocr.status_message())

    from PIL import Image, ImageDraw, ImageFont
    phrase = "Macronaut OCR 2026"
    img = Image.new("RGB", (620, 130), "white")
    draw = ImageDraw.Draw(img)
    font = None
    for path in (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf"):
        try:
            font = ImageFont.truetype(path, 56)
            break
        except OSError:
            continue
    # The default bitmap font is too small for any engine to read reliably, so
    # a missing TrueType font is a bad test, not a bad build.
    if font is None:
        return f"{ocr.active_backend()} available (no TrueType font to test with)"
    draw.text((24, 30), phrase, fill="black", font=font)

    # Name the engine in every outcome. Which engine got selected IS the
    # diagnosis: ocr.py picks it at runtime by dynamic import, so a frozen build
    # can quietly select a different one than the source run did.
    tag = ocr.active_backend()

    # PhraseResult.matched -- not `.found`, which silently reads as False on a
    # namedtuple and would fail every run.
    got = ocr.match_phrase(phrase, img)
    if not got.matched:
        words = [t.text for t in ocr.read_regions(img)]
        raise RuntimeError(f"engine {tag} read {words!r} from rendered text "
                           f"{phrase!r} (score {got.score:.2f})")

    # ── a screen too wide for the engine ─────────────────────────────────
    #
    # ⚠ Windows.Media.Ocr refuses any bitmap past OcrEngine.MaxImageDimension
    # and "refuses" means it returns an EMPTY result -- no exception. Three
    # 3440px ultrawides is 10320. An oversized grab is scaled to fit and the
    # boxes scaled back; if that ever stops happening, Detect-text reports
    # "not on screen" for every word on a wide desktop, silently.
    eng = ocr.get_engine()
    extra = ""
    limit = getattr(eng, "max_dimension", 0)
    if limit:
        # ⚠ Shape chosen against measurement, not for convenience. A single
        # phrase in 56px on a 10400x150 strip reads as NOTHING -- and so does
        # the same strip at 9000px, which is inside the limit and never
        # scaled, so that is the engine being odd about very wide, very
        # sparsely-filled images rather than anything to do with fitting.
        # 40px repeated across the width is steady at every height tried
        # (150 through 1000) and is what a real wide desktop looks like:
        # text all over it.
        try:
            wide_font = ImageFont.truetype(font.path, 40)
        except Exception:
            wide_font = font
        wide = Image.new("RGB", (limit + 400, 260), "white")
        _wd = ImageDraw.Draw(wide)
        for _i in range(6):
            _wd.text((60 + _i * (wide.width // 6), 110), phrase,
                     fill="black", font=wide_font)
        wide_got = ocr.match_phrase(phrase, wide)
        if not wide_got.matched:
            raise RuntimeError(
                f"engine {tag} read nothing from a {wide.width}px-wide image "
                f"(its limit is {limit}) - an oversized screen is not being "
                "scaled to fit, so a wide desktop finds no text at all")
        extra = f"; {wide.width}px wide ok"

    # ── text too small to read at native size ────────────────────────────
    #
    # Adaptive on purpose, in the same spirit as the missing-font branch
    # above: whether an 11px rendering needs the rescue depends on the font
    # and the machine, and a self-test that fails on that is a bad test
    # rather than a bad build. What must never happen is the *rescue* being
    # the thing that broke.
    small = Image.new("RGB", (420, 90), "white")
    try:
        tiny = ImageFont.truetype(font.path, 11)
    except Exception:
        tiny = None
    if tiny is not None:
        ImageDraw.Draw(small).text((14, 34), phrase, fill="black", font=tiny)
        plain = ocr.get_engine().match_phrase(phrase, small, _zoom=False)
        rescued = ocr.match_phrase(phrase, small)
        if plain.matched:
            extra += "; 11px read unaided"
        elif rescued.matched:
            extra += "; 11px read by the zoom rescue"
        else:
            extra += "; 11px unreadable even enlarged"
        if plain.matched and not rescued.matched:
            raise RuntimeError(
                "the zoom rescue LOST a phrase the plain read found - it must "
                "only ever add answers, never replace a good one")

    return f"{tag}, read {phrase!r} score={got.score:.2f}{extra}"


def _check_legal() -> str:
    """The licence has to be readable from inside the .exe, not just the
    repo — GPL §4 requires the licence to travel with the program, and
    Settings ▸ About opens this exact file."""
    # Source checkout: this file is in src/, LICENSE at the root and the
    # notices in docs/. The frozen build has both at the bundle root.
    frozen = getattr(sys, "_MEIPASS", None)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = []
    for name, needle, where in (
            ("LICENSE", "GNU GENERAL PUBLIC LICENSE", ""),
            ("THIRD-PARTY-NOTICES.md", "elects LGPL-3.0-only", "docs")):
        path = os.path.join(frozen or os.path.join(root, where), name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        if needle not in text:
            raise RuntimeError(f"{name} is present but missing {needle!r}")
        out.append(f"{name} {len(text)//1024}KB")
    return ", ".join(out)


# A licence key signed offline with a THROWAWAY keypair, so that a frozen build
# can prove it verifies signatures without the real signing key being involved.
# It has to be regenerated if the payload format ever changes.
_SELFTEST_PUBKEY = "4ce37870b7c6e0e3e2fd13bc0b4ed9701860fbc94774ec328aead18833b08c6b"
_SELFTEST_KEY = (
    "MN1-AEAQAAAI-KNCUYRSU-IVJVIGTT-MVWGM5DF-ON2EA3LB-MNZG63TB-OV"
    "2C42LO-OZQWY2LE-5EGUKC3N-HGT2BGIE-LAGEAKFI-W367COTG-C4J6JK5X"
    "-WSWJF6ZY-TNILXY5S-XZKYLRXL-L6ZEGJNW-CJ6SBLGS-7ULWNCDY-R7BRT"
    "KGJ-ZBGLECI"
)


def _check_starters() -> str:
    """Prove the frozen build can still build the flows it seeds on first run.

    Not critical — a missing starter costs a new user their first success, not
    their data. It is checked because the failure is silent: `starters.seed`
    swallows everything so that nothing here can stop the app from opening,
    which is right, and which also means a `starters` module dropped from the
    bundle would show up as an empty library and nothing else.
    """
    import entitlements
    import flow
    import starters

    built = starters.build_all()
    if len(built) != len(starters.STARTERS):
        raise RuntimeError("a starter failed to build")
    for name, graph in built.items():
        if not flow.has_work(graph):
            raise RuntimeError(f"{name!r} has nothing to run")
        # ⚠ `runs_on_free`, not `check`: `check` consults the licence, so on a
        # machine holding a key every starter would pass this regardless of
        # what it contains — and a developer's build is exactly the machine
        # most likely to hold one.
        free = entitlements.runs_on_free(graph)
        if name == starters.PRO_EXAMPLE:
            # The one deliberate exception, and it has to stay an exception:
            # if it ever became runnable on Free it would stop demonstrating
            # anything.
            if free:
                raise RuntimeError(f"{name!r} no longer needs Pro")
        elif not free:
            # A starter behind the paywall is a new user's first click into a
            # sales dialog, which is the opposite of what these are for.
            raise RuntimeError(f"{name!r} is not in the free tier")
    return (f"{len(built)} starter scripts, "
            f"{len(starters.free_starters())} in the free tier")


def _check_licensing() -> str:
    """Prove the frozen build can actually verify a licence key.

    ⚠ Critical, and the reason is the failure mode rather than the feature: if
    `ed25519` or `licensing` were ever dropped from the bundle, or the public
    key shipped as its placeholder, the .exe would reject **every** real key —
    and would do it with the same message a genuinely wrong key gets. "Invalid
    key" looks identical whether the customer mistyped it or the build is
    broken, so the person who discovers it is a paying customer, by e-mail,
    after the fact.

    Verified against a **test vector generated by this project's own signer**,
    not against the live signing key: the private key is not on a customer's
    machine, is not in the repo, and must never be needed to check a build.
    """
    import ed25519
    import entitlements
    import licensing

    # Signed offline with a throwaway keypair, then pasted here. It has to be
    # regenerated if the payload
    # format ever changes; the point is only that the maths and the plumbing
    # work, so the key it was signed with is deliberately not the real one.
    pub = _SELFTEST_PUBKEY
    lic = licensing.parse_key(_SELFTEST_KEY, pub)
    if lic is None or not lic.is_pro:
        raise RuntimeError("a known-good key did not verify")
    if lic.email != "selftest@macronaut.invalid":
        raise RuntimeError(f"payload decoded wrong: {lic.email!r}")

    # And the negative, because a verifier that accepts everything would sail
    # through the line above.
    tampered = _SELFTEST_KEY[:-4] + ("AAAA" if _SELFTEST_KEY[-4:] != "AAAA"
                                     else "BBBB")
    if licensing.parse_key(tampered, pub) is not None:
        raise RuntimeError("a corrupted key verified - the gate is not real")
    if licensing.parse_key(_SELFTEST_KEY) is not None:
        raise RuntimeError("the test key verified against the SHIPPED public "
                           "key - that key is not the one it should be")

    if len(licensing.PUBLIC_KEY_HEX) != 64 or set(licensing.PUBLIC_KEY_HEX) == {"0"}:
        raise RuntimeError("no real public key is baked into this build")

    # ⚠ Report whether the tier is actually ENFORCED, not just that the key
    # machinery works. Those are different facts and only one of them decides
    # what a user experiences -- a build that verifies keys perfectly while
    # gating nothing is the build being shipped on purpose right now, and the
    # day that stops being on purpose this line is where it shows.
    gate = ("enforced" if entitlements.ENFORCED
            else "NOT enforced - everything is free")
    return (f"verified ok, rejects tampering; free tier "
            f"{entitlements.FREE_MAX_STEPS} steps; {gate}")


def _check_updater() -> str:
    import version
    return f"v{version.__version__} -> {version.UPDATE_REPO}"


def _check_crash_reporting() -> str:
    """Arm capture, abandon the session, and prove a report comes back.

    End to end against a temp directory rather than an import check, for the
    same reason as everything else here: the interesting failure is a crash
    reporter that loads perfectly and records nothing. This one would be
    especially quiet, because the only way to notice it in the field is a crash
    that never gets reported — which looks exactly like no crashes.

    Also asserts the arming refusal, since getting THAT wrong points the other
    way: --apply-update runs on every single update, so a reporter that armed
    there would file a crash report for every successful one.
    """
    import json
    import tempfile
    from pathlib import Path

    import crashreport
    import crashsend

    tmp = Path(tempfile.mkdtemp(prefix="mn-selftest-crash-"))
    if crashreport.install(tmp, argv=["x", "--apply-update"]):
        raise AssertionError("armed during --apply-update")

    # A session that never disarmed, exactly as an aborted process leaves one.
    d = crashreport.crash_dir(tmp)
    base = d / "session-999999997-1000"
    base.with_suffix(".json").write_text(json.dumps({
        "schema": crashreport.SCHEMA, "pid": 999_999_997, "started": 1.0,
        "version": "0.0.1-selftest", "frozen": True, "os": "Windows"}),
        encoding="utf-8")
    base.with_suffix(".fatal").write_text(json.dumps({
        "kind": "qt_fatal", "text": "QThread: Destroyed while thread is "
        "still running"}) + "\n", encoding="utf-8")

    out = crashreport.harvest(tmp)
    if len(out) != 1:
        raise AssertionError(f"harvest produced {len(out)} reports, expected 1")
    rep = crashreport.load(out[0])
    if rep.get("version") != "0.0.1-selftest":
        raise AssertionError("report lost the version that crashed")
    ev = crashsend.to_event(rep)
    if ev.get("release") != "0.0.1-selftest" or not ev.get("fingerprint"):
        raise AssertionError("event would not group correctly in Sentry")

    dest = "configured" if crashsend.enabled() else "LOCAL ONLY (no DSN)"
    return f"capture ok, {dest}"


def _check_input_backends() -> str:
    """Both factories return (controller, actual_backend_id, warning).

    The interesting value is `actual` -- it is what the app FELL BACK to, which
    differs from the configured backend whenever the Interception driver is
    missing. A warning here is informational, not a failed build.
    """
    import input_backends
    out = []
    for label, make in (("kb", input_backends.make_keyboard),
                        ("mouse", input_backends.make_mouse)):
        _ctl, actual, warn = make()
        out.append(f"{label}={actual}" + (f" ({warn.split('—')[0].strip()})"
                                          if warn else ""))
    return "  ".join(out)


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ── driver ────────────────────────────────────────────────────────────────────
def run() -> int:
    _ensure_console()
    _say("Macronaut self-test")
    _say("-" * 72)
    rep = _Report()
    rep.check("python", _check_python)
    rep.check("PySide6", _check_qt)
    rep.check("numpy", _check_numpy)
    rep.check("cv2", _check_cv2)
    rep.check("image match", _check_image_match)
    rep.check("OCR", _check_ocr)
    rep.check("legal files", _check_legal)
    rep.check("licensing", _check_licensing)
    rep.check("starters", _check_starters)
    rep.check("updater", _check_updater)
    rep.check("crash reports", _check_crash_reporting)
    rep.check("input", _check_input_backends)
    _say("-" * 72)

    failed = [n for n, ok, _ in rep.rows if not ok]
    fatal = [n for n in failed if n in CRITICAL]
    if fatal:
        _say(f"BROKEN: {', '.join(fatal)} - do not ship this build.")
        code = 1
    elif failed:
        _say(f"DEGRADED: {', '.join(failed)} - optional features unavailable.")
        code = 1
    else:
        _say("OK: all checks passed.")
        code = 0

    # Always leave a copy on disk: if the console attach failed, this file is
    # the only record of why a build was rejected.
    try:
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "macronaut-selftest.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_LINES) + "\n")
        _say(f"report: {path}")
    except OSError:
        pass
    return code


if __name__ == "__main__":
    sys.exit(run())
