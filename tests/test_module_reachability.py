"""Is every module in this repo actually part of the program?

⚠ **The mistake this exists for.** On 4 September 2026 a real bug — a 15.625 ms
clock quantisation — was found in `clicker.py`, fixed, benchmarked, tested and
reported as an improvement to the shipped auto-clicker. `clicker.py` is imported
by nothing and is not in the `.exe`. Basic-face clicking runs through
`flow_exec.FlowWorker._do_autoclick`, which its own comment calls a port of the
code in `clicker.py`. The port landed; the original stayed.

`test_no_new_public_code_becomes_unreachable` in test_packaging did not catch it
and could not: it looks for unreferenced *names*, and every name in `clicker.py`
is referenced — by the other names in `clicker.py`. **An orphaned module is
internally consistent.** Reachability has to be asked from the entry point, and
that is what this file does.

The .exe is built from `main.py` and PyInstaller collects what is imported, so
"reachable from main.py" is the same question as "does this ship".
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# What the program is built from. `macronaut.spec` passes Analysis(["main.py"]).
ENTRY = "main"

# Modules that are deliberately not part of the app. Each says why, and each is
# asserted to still be unreachable — an entry that starts being imported is as
# much a surprise as one that stops.
_NOT_PART_OF_THE_APP = {
    "clicker": "⚰ dead — the 2.0 rewrite ported it to flow_exec._do_autoclick "
               "and left the original behind. Its own docstring says so.",
    "orb": "⚰ dead — the compact/orb faces were removed in the one-window "
           "rewrite; kept because the mount refuses deletes.",
    "recorder": None,          # resolved below: it IS imported by main
    "release": "a release tool, run by hand; never imported by the app.",
    # ⚠ `selftest` was on this list when it was written, described as "reached
    # only through --selftest". Wrong: main() imports it, lazily but really, and
    # it ships — test_selftest_is_bundled asserts the .exe carries it. The
    # anti-rot test below caught that on its first run, which is the entire
    # point of asserting an allowlist is still true rather than trusting it.
    "create_shortcut": "a one-off desktop-shortcut helper, run by hand.",
    "make_node_test_script": "a developer script that writes a sample flow.",
}
_NOT_PART_OF_THE_APP = {k: v for k, v in _NOT_PART_OF_THE_APP.items()
                        if v is not None}


def _imports_of(module: str) -> set:
    """Every top-level module name this module imports, at any nesting depth.

    Walks the whole tree rather than the top of the file: several imports here
    are deliberately inside functions (`interception_backend` is loaded lazily,
    `selftest` only under --selftest), and those are still real edges.
    """
    path = os.path.join(ROOT, module + ".py")
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                out.add(node.module.split(".")[0])
    return out


def _local_modules() -> set:
    return {f[:-3] for f in os.listdir(ROOT)
            if f.endswith(".py") and not f.startswith("_")}


def _reachable_from(entry: str) -> set:
    local = _local_modules()
    seen, stack = set(), [entry]
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        for dep in _imports_of(m) & local:
            if dep not in seen:
                stack.append(dep)
    return seen


def test_no_module_is_orphaned_from_the_app():
    """Every .py here either ships or is on the list of things that do not.

    ⚠ This is the check that would have caught a whole session spent fixing,
    testing and reporting a bug in code that is not in the product.
    """
    local = _local_modules()
    reachable = _reachable_from(ENTRY)
    orphans = sorted(local - reachable - set(_NOT_PART_OF_THE_APP))

    assert not orphans, (
        "these are not reachable from main.py, so they are not in the .exe:\n  "
        + "\n  ".join(orphans)
        + "\nEither something should import them, or they belong in "
          "_NOT_PART_OF_THE_APP with a reason. Do not 'fix' code here until "
          "you know which.")


def test_the_not_part_of_the_app_list_is_still_true():
    """⚠ Anti-rot, and it points both ways.

    An entry that quietly becomes reachable is the more interesting failure:
    it means dead code was wired back into the program without anyone saying
    so, and every warning in its docstring is now live.
    """
    reachable = _reachable_from(ENTRY)
    local = _local_modules()

    resurrected = sorted(m for m in _NOT_PART_OF_THE_APP if m in reachable)
    assert not resurrected, (
        f"these are listed as not part of the app but main.py now reaches "
        f"them: {resurrected}\nIf that is deliberate, remove them from the "
        "list — and re-read their docstrings first, because several say they "
        "are out of date in ways that matter.")

    gone = sorted(m for m in _NOT_PART_OF_THE_APP if m not in local)
    assert not gone, (
        f"these are listed but no longer exist: {gone}. Drop them.")


def test_the_dead_clicker_is_still_dead():
    """Named explicitly because it is the one that cost a session.

    ⚠ If this ever fails, the whole of `clicker.py` is suddenly live — including
    a pacing loop that predates the selectable input backends and talks to
    pynput directly, which cannot reach the games that are the main reason
    anyone installs this.
    """
    assert "clicker" not in _reachable_from(ENTRY)
    with open(os.path.join(ROOT, "clicker.py"), encoding="utf-8") as fh:
        head = fh.read(200)
    assert "DEAD CODE" in head, (
        "clicker.py lost the banner that says it is dead — that banner is the "
        "only thing standing between the next reader and the same mistake")


@pytest.mark.parametrize("module", sorted(_NOT_PART_OF_THE_APP))
def test_nothing_that_ships_imports_a_module_that_does_not(module):
    """The reachable set is computed transitively, so this is a sharper error
    message rather than extra coverage: it names the importer."""
    reachable = _reachable_from(ENTRY)
    culprits = sorted(m for m in reachable if module in _imports_of(m))
    assert not culprits, f"{culprits} import {module!r}, which does not ship"
