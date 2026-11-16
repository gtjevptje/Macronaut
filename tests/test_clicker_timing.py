"""Pacing: the interval asked for has to be the interval delivered.

⚠ **Read this before trusting the git history of this file.** It was first
written against `clicker.ClickWorker`, where a real 15.625 ms clock bug was
found, fixed, measured and reported as an improvement to the shipped
auto-clicker. `clicker.py` is dead — nothing imports it, it is not in the .exe,
and its own first line says so. The live pacing primitive is
`flow_exec.FlowWorker.sleep`, which every timed thing in the running engine goes
through, and which was already correct. See `tests/test_module_reachability.py`,
which exists because of that mistake.

So these now test the live path. What the bug was, kept because the shape of it
recurs: a deadline timed with `time.monotonic()` is timed with GetTickCount64 on
Windows, resolution 15.625 ms, so every wait rounds up to the next tick — a 5 ms
*and* a 10 ms wait both take 16.00 ms, 50 ms takes 62.50, 200 ms takes 203.00.
`time.sleep` is not the culprit; it is accurate to well under a millisecond.

Measured on the live primitive, which is what the numbers should always have
been about:

    target      median     error
      5 ms      5.48 ms    +9.5%     <- time.sleep's own ~0.5 ms overhead
     50 ms     50.34 ms    +0.7%
    100 ms    100.37 ms    +0.4%
"""
import ast
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def pacer():
    """A FlowWorker built without Qt, for `sleep` alone.

    `sleep` needs exactly two things: `self._running` and the clock. Building a
    real FlowWorker would drag in a QThread and an interpreter for no benefit.
    """
    import flow_exec
    w = flow_exec.FlowWorker.__new__(flow_exec.FlowWorker)
    w._running = True
    return w


# ── the rule, pinned across every module ─────────────────────────────────────

# Functions that legitimately read the coarse clock while also sleeping. Each
# measures WHOLE SECONDS, where 15.625 ms is 0.05% and nobody can tell.
#
# ⚠ Every entry is asserted to still match something, so a rename or a deletion
# fails this test rather than quietly leaving a permanent exemption behind.
_COARSE_IS_FINE = {
    ("clicker.py", "run"):
        "⚰ dead module; t0 is a start time for a whole-seconds limit anyway",
    ("updater.py", "_wait_for_exit"):
        "waits seconds for another process to exit",
    ("interception_backend.py", "_cli"):
        "a __main__ diagnostic that times whole seconds",
    # ⚠ `flow_exec._do_autoclick` was listed here and should not have been: it
    # reads time.monotonic() for a whole-seconds run limit but never calls
    # time.sleep — it paces through `self.sleep`, so the sweep never matches it
    # and the exemption was dead on arrival. The anti-rot assertion below found
    # that on the first run. An allowlist you do not check is a list of guesses.
}


def test_no_pacing_loop_reads_the_coarse_clock():
    """A function that both sleeps and reads `time.monotonic()` is pacing on a
    15.625 ms clock, whatever it thinks it is doing.

    ⚠ This is the part of the original work that survived contact with the
    facts: it sweeps every module rather than the one place a bug was noticed,
    so it covers the live engine and not only the module that happened to be
    read first.
    """
    offenders = []
    seen = set()
    for name in sorted(os.listdir(ROOT)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
            try:
                tree = ast.parse(fh.read())
            except SyntaxError:                 # not ours to police
                continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            sleeps = coarse = False
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                if not isinstance(f, ast.Attribute) or \
                        not isinstance(f.value, ast.Name) or f.value.id != "time":
                    continue
                if f.attr == "sleep":
                    sleeps = True
                elif f.attr == "monotonic":
                    coarse = True
            if not (sleeps and coarse):
                continue
            key = (name, node.name)
            seen.add(key)
            if key not in _COARSE_IS_FINE:
                offenders.append(f"  {name}:{node.lineno}  {node.name}()")

    assert not offenders, (
        "these pace a sleep against time.monotonic(), which on Windows has "
        "15.625 ms resolution — every wait quantises up to the next tick:\n"
        + "\n".join(offenders)
        + "\nUse time.perf_counter() for the deadline, or add it to "
          "_COARSE_IS_FINE with a reason if it only measures whole seconds.")

    stale = sorted(k for k in _COARSE_IS_FINE if k not in seen)
    assert not stale, (
        "these are exempted but no longer match anything — renamed, deleted, "
        f"or already fixed:\n  {stale}\nDrop them from _COARSE_IS_FINE, or "
        "the next function to take that name inherits the exemption.")


# ── the live pacing primitive ────────────────────────────────────────────────

def _typical(fn, tries=9):
    """Median of N.

    ⚠ This was best-of-N first, on the reasoning that a loaded machine can make
    a sample slow but not fast, so the minimum is the honest floor. That is
    right for overhead and **wrong for quantisation**: where a deadline lands
    relative to the next 15.625 ms tick is uniform, so a coarse clock produces
    waits from nearly 0 up to 15.6 ms and its *minimum is fast*. Two tests using
    it passed with the bug deliberately reinstated. The median moves under
    quantisation and shrugs off load spikes.
    """
    xs = []
    for _ in range(tries):
        t0 = time.perf_counter()
        fn()
        xs.append(time.perf_counter() - t0)
    return sorted(xs)[len(xs) // 2]


@pytest.mark.parametrize("ms,ceiling_ms", [(5, 12), (20, 30), (50, 60)])
def test_a_short_wait_is_not_rounded_up_to_the_next_tick(pacer, ms, ceiling_ms):
    """⚠ Each ceiling sits below the tick multiple that quantisation would
    force and above what a correct implementation costs. Quantised, a 5 ms wait
    took 16.00 ms, 20 ms took 31.25, 50 ms took 62.50 — so a coarse clock fails
    all three, while `time.sleep`'s own ~0.5 ms of overhead fits comfortably.
    """
    took = _typical(lambda: pacer.sleep(ms / 1000.0))
    assert took * 1000 < ceiling_ms, (
        f"sleep({ms} ms) took {took * 1000:.2f} ms; {ms} ms rounded up to a "
        f"15.625 ms tick would be about "
        f"{15.625 * (int(ms / 15.625) + 1):.2f} ms")


def test_a_long_interval_lands_where_it_was_asked_to(pacer):
    """The rates the website's interval table promises are built out of this."""
    took = _typical(lambda: pacer.sleep(0.1), tries=5) * 1000
    assert 99 <= took < 108, f"sleep(100 ms) took {took:.2f} ms"


def test_stop_still_cuts_a_long_wait_short(pacer):
    """`sleep` is interruptible on purpose — Stop must not wait out a ten-second
    interval. It is the property a rewrite of this loop could plausibly break.
    """
    import threading

    threading.Timer(0.05, lambda: setattr(pacer, "_running", False)).start()
    t0 = time.perf_counter()
    pacer.sleep(10.0)
    took = time.perf_counter() - t0

    assert took < 1.0, f"sleep ignored the stop flag for {took:.2f} s"
    assert took >= 0.04, "returned before the stop could have been requested"


def test_a_negative_or_zero_wait_returns_at_once(pacer):
    """`max(0.0, secs)` — a node whose deadline has already passed must not
    block, and must not throw."""
    assert _typical(lambda: pacer.sleep(-1.0), tries=3) < 0.005
    assert _typical(lambda: pacer.sleep(0.0), tries=3) < 0.005
