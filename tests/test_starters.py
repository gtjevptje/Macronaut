"""The starter flows, and the promises they make to a first-time user.

Each test here pins one of the reasons these exist. They are cheap to break by
accident — a starter is just data, and nothing about editing one tells you that
it has to stay runnable, free, and stoppable.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entitlements
import flow
import settings
import starters


def _load_back(graph, tmp_path, name="x"):
    """Save and reload, because that is the only path the library ever uses."""
    p = tmp_path / f"{name}.json"
    graph.save(str(p))
    return flow.FlowGraph.load(str(p))


@pytest.mark.parametrize("name", [n for n, _ in starters.STARTERS])
def test_a_starter_has_something_to_run(name, tmp_path):
    """Pressing Play is the first thing anyone does. It has to do something."""
    g = _load_back(starters.build_all()[name], tmp_path)
    assert flow.has_work(g), name


_FREE = [n for n, _ in starters.STARTERS if n != starters.PRO_EXAMPLE]


@pytest.mark.parametrize("name", _FREE)
def test_a_starter_is_inside_the_free_tier(name, tmp_path):
    """A paywall on the first flow a new user opens is the opposite of what
    these are for: nobody has seen the product work yet, so there is nothing
    for the upgrade dialog to be arguing from.

    ⚠ `runs_on_free`, not `check` — `check` consults the licence, so on a
    machine holding a key this would pass no matter what the flow contained.
    """
    g = _load_back(starters.build_all()[name], tmp_path)
    assert entitlements.runs_on_free(g), name


def test_the_pro_example_is_the_only_one_that_is_not_free():
    """`free_starters()` is what the README counts. If a starter quietly
    changes tier, the page starts advertising a number of runnable automations
    that is not the number a visitor gets."""
    assert starters.PRO_EXAMPLE in dict(starters.STARTERS), \
        "PRO_EXAMPLE names a starter that no longer exists"
    assert list(starters.free_starters()) == _FREE


def test_the_pro_example_is_refused_for_the_reason_it_exists(tmp_path):
    """It is here to show what the paid half does, so it has to actually use
    both halves of it — the watching and the deciding — and the refusal has to
    say so in words a person can act on."""
    g = _load_back(starters.build_all()[starters.PRO_EXAMPLE], tmp_path)
    feats = entitlements.pro_features_used(g)
    assert "Wait for text" in feats and "Loop" in feats, feats
    assert not entitlements.runs_on_free(g)


def test_the_pro_example_touches_nothing(tmp_path):
    """Someone who buys Pro and presses Play on the example straight away must
    get a flow that reads the screen and stops — not one that clicks somewhere
    nobody asked for. A Detect only sends input when `click` is on."""
    g = _load_back(starters.build_all()[starters.PRO_EXAMPLE], tmp_path)
    for node in g.nodes.values():
        step = (node.data.get("step") or {})
        if not step:
            continue
        assert step.get("kind") in flow.DETECT_KINDS, \
            f"{node.id} is a {step.get('kind')} step, which sends input"
        assert not (step.get("data") or {}).get("click"), \
            f"{node.id} clicks what it finds"


def test_the_pro_example_is_wired_on_ports_that_exist(tmp_path):
    """⚠ An edge on a port a node does not have still *draws*, so a flow can
    look wired while being one that could never run. That is the worst thing
    to ship inside an example whose whole job is to be read and believed."""
    g = _load_back(starters.build_all()[starters.PRO_EXAMPLE], tmp_path)
    for e in g.edges:
        ports = g.nodes[e.src].ports()
        assert e.src_port in ports, \
            f"{g.nodes[e.src].type} has no {e.src_port!r} port (only {ports})"

    reached, stack = set(), [g.start_node().id]
    out = {}
    for e in g.edges:
        out.setdefault(e.src, []).append(e.dst)
    while stack:
        nid = stack.pop()
        if nid in reached:
            continue
        reached.add(nid)
        stack += out.get(nid, [])
    stranded = [n.id for n in g.nodes.values()
                if n.id not in reached and n.type != flow.N_FRAME]
    assert not stranded, f"unreachable from Start: {stranded}"


@pytest.mark.parametrize("name", [n for n, _ in starters.STARTERS])
def test_a_starter_needs_nothing_filled_in(name, tmp_path):
    """The promise is "press Play". A coordinate picked on this machine, or a
    reference to an image file that only exists here, breaks it silently — the
    flow still opens, and then clicks somewhere the user did not ask for."""
    g = _load_back(starters.build_all()[name], tmp_path)
    for node in g.nodes.values():
        step = (node.data.get("step") or {})
        d = step.get("data") or {}
        assert not d.get("image_path"), f"{name}: {node.id} needs an image"
        assert not d.get("use_fixed"), f"{name}: {node.id} pins a coordinate"
        # ⚠ The Pro example is exempt from this one clause and only this one:
        # a Detect step *is* the thing it exists to show. It still has to need
        # nothing filled in, which the two assertions above pin for it too.
        if name != starters.PRO_EXAMPLE:
            assert step.get("kind") not in flow.DETECT_KINDS, \
                f"{name}: {node.id} waits for something on this screen"


@pytest.mark.parametrize("name", [n for n, _ in starters.STARTERS])
def test_a_starter_that_never_stops_says_how_to_stop_it(name, tmp_path):
    """An auto-clicker with no limit takes the mouse away from you. The note on
    the canvas is the only place a first-time user will look for the way out,
    and both shipped defaults belong in it."""
    g = _load_back(starters.build_all()[name], tmp_path)
    unbounded = any(
        (n.data.get("step") or {}).get("kind") == "autoclick"
        and not ((n.data.get("step") or {}).get("data") or {}).get("click_limit")
        and not ((n.data.get("step") or {}).get("data") or {}).get("stop_after_secs")
        for n in g.nodes.values())
    if not unbounded:
        pytest.skip(f"{name} stops on its own")
    notes = " ".join(n.data.get("text", "") for n in g.nodes.values()
                     if n.type in (flow.N_FRAME, flow.N_COMMENT)).lower()
    assert settings.AppSettings().panic_hotkey.lower() in notes, name
    assert settings.AppSettings().start_stop_hotkey.lower() in notes, name


@pytest.mark.parametrize("name", [n for n, _ in starters.STARTERS])
def test_a_starter_explains_itself_on_the_canvas(name, tmp_path):
    """The note is the documentation. A starter without one is a stranger's
    flow that a new user has to reverse-engineer before daring to run it."""
    g = _load_back(starters.build_all()[name], tmp_path)
    frames = [n for n in g.nodes.values() if n.type == flow.N_FRAME]
    assert len(frames) == 1, name
    assert flow.frame_title(frames[0]), name
    assert len(flow.frame_body(frames[0])) > 80, name


def test_every_note_fits_the_header_that_draws_it(qapp):
    """⚠ The frame's note is capped at `FrameItem.NOTE_MAX_H` and anything
    past it is silently cut off mid-line. Nothing about the text says so; the
    graph is valid, the flow runs, and the only symptom is a sentence that
    stops in the middle. It has now happened twice — once when the starters
    shipped, and again when this one's note was reworded.

    ⚠ Skipped unless a real font database is present. Under Qt's offscreen
    platform `QFontDatabase.families()` is **empty**, so QFontMetrics answers
    from a fallback that is nothing like the shipped UI font: measured there,
    all six notes report as clipped while all six are fine on Windows. A test
    that fails on correct data is worse than no test, so this one refuses to
    guess — run the suite on a real Windows desktop to get the answer.
    """
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics
    import flow_canvas as fc

    if not QFontDatabase.families():
        pytest.skip("no font database on this platform; metrics would be "
                    "measuring a fallback font, not the one users see")

    cap = fc.FrameItem.NOTE_MAX_H
    for name, build in starters.STARTERS:
        g = build()
        frame = [n for n in g.nodes.values() if n.type == flow.N_FRAME][0]
        width = float(frame.data.get("w", 0))
        note = flow.frame_body(frame)
        f = QFont()
        f.setPointSize(9)
        box = QFontMetrics(f).boundingRect(
            QRect(0, 0, int(width) - 24, 10_000),
            int(Qt.TextWordWrap | Qt.AlignLeft), note)
        assert box.height() <= cap, (
            f"{name}: the note needs {box.height():.0f}px and the header "
            f"gives it {cap:.0f}px, so the last line is cut in half")


def test_the_seeded_files_are_the_names_the_library_shows():
    """The library lists file stems, and `script_hotkeys` binds launcher keys
    by stem too. A name that will not survive a Windows filename is a starter
    that silently never appears."""
    bad = set('<>:"/\\|?*')
    for name, _ in starters.STARTERS:
        assert name.strip() == name, name
        assert not (bad & set(name)), name


def test_seeding_writes_every_starter(tmp_path):
    written = starters.seed(tmp_path)
    assert sorted(written) == sorted(n for n, _ in starters.STARTERS)
    on_disk = sorted(p.stem for p in tmp_path.glob("*.json"))
    assert on_disk == sorted(n for n, _ in starters.STARTERS)
    for p in tmp_path.glob("*.json"):
        json.loads(p.read_text(encoding="utf-8"))      # valid, and reloadable
        assert flow.FlowGraph.load(str(p)).nodes


def test_seeding_keeps_its_hands_off_a_library_someone_is_using(tmp_path):
    """Five files you did not make, appearing next to work you did, is the
    kind of thing that makes people distrust an update."""
    (tmp_path / "my own flow.json").write_text("{}", encoding="utf-8")
    assert starters.seed(tmp_path) == []
    assert [p.name for p in tmp_path.glob("*.json")] == ["my own flow.json"]


def test_a_deleted_starter_stays_deleted(tmp_path):
    """`seed_once` records the flag whether or not it wrote anything, so
    clearing the library later does not bring them back. Without this, deleting
    a starter is impossible and the app looks broken."""
    class _Mgr:
        def __init__(self):
            self.s = settings.AppSettings()
        def set(self, key, value):
            setattr(self.s, key, value)

    mgr = _Mgr()
    assert starters.seed_once(mgr, tmp_path)
    assert mgr.s.starters_seeded is True
    for p in tmp_path.glob("*.json"):
        p.unlink()
    assert starters.seed_once(mgr, tmp_path) == []
    assert list(tmp_path.glob("*.json")) == []


def test_add_missing_fills_the_gaps_without_touching_anything_else(tmp_path):
    """The manual route, for everyone whose library was already occupied when
    the starters shipped — the automatic path skips them forever, and they are
    the people who have used it longest."""
    (tmp_path / "my own flow.json").write_text("{}", encoding="utf-8")
    added = starters.add_missing(tmp_path)
    assert sorted(added) == sorted(n for n, _ in starters.STARTERS)
    assert (tmp_path / "my own flow.json").read_text(encoding="utf-8") == "{}"


def test_add_missing_never_overwrites_an_edited_starter(tmp_path):
    """⚠ The worst thing this module could do. A file under a starter's name
    is that starter after somebody changed it, and replacing their work with
    the factory copy destroys it with no undo and no warning."""
    mine = tmp_path / "Auto-clicker.json"
    mine.write_text('{"mine": true}', encoding="utf-8")
    added = starters.add_missing(tmp_path)
    assert "Auto-clicker" not in added
    assert mine.read_text(encoding="utf-8") == '{"mine": true}'


def test_add_missing_is_quiet_when_there_is_nothing_to_add(tmp_path):
    starters.seed(tmp_path)
    assert starters.add_missing(tmp_path) == []


def test_the_library_says_what_adding_examples_did(qapp, tmp_path, monkeypatch):
    """⚠ Pins an ordering trap, not a string. `_refresh` ends in
    `_sync_buttons`, which unconditionally rewrites the status line — and so
    does selecting an item. Setting the message before either leaves the user
    reading "7 scripts · 1 selected" and no idea whether the button worked,
    which is exactly what the first version of `_add_examples` did.
    """
    import main as main_mod
    monkeypatch.setattr(main_mod, "scripts_dir", lambda: tmp_path)
    dlg = main_mod.ScriptLibraryDialog(None, main_mod.SettingsManager())
    try:
        dlg._add_examples()
        assert "Added" in dlg._status.text(), dlg._status.text()
        assert dlg._list.count() == len(starters.STARTERS)

        # And again, with nothing left to do.
        dlg._add_examples()
        assert "already have" in dlg._status.text(), dlg._status.text()
        assert dlg._list.count() == len(starters.STARTERS)
    finally:
        dlg.hide()


def test_an_unwritable_library_does_not_stop_the_app(tmp_path):
    """Everything in `seed` is cosmetic. Nothing in it may raise on the path
    that runs before the main window is on screen."""
    blocked = tmp_path / "not-a-folder"
    blocked.write_text("", encoding="utf-8")           # a file where a dir goes
    assert starters.seed(blocked) == []


@pytest.mark.parametrize("name", ["index.html", "README.md"])
def test_the_page_counts_the_starters_the_app_actually_ships(name):
    """Both are generated from the starter list, and both name the number in
    prose. Adding a starter without rebuilding leaves the page naming fewer.

    ⚠ The number is `free_starters()`, not `STARTERS`. The library also ships
    one example that needs Pro, and counting it here would put a figure on the
    page one larger than the number of things a visitor can actually run after
    downloading — which is the sort of small lie a refund is made of."""
    built = Path(__file__).resolve().parent.parent / "site" / name
    if not built.exists():
        pytest.skip(f"{name} not built in this checkout")
    text = built.read_text(encoding="utf-8")

    # ⚠ The NUMBER is the subject, not the sentence around it. This pinned the
    # exact phrase "N automations already built" until 16 September 2026, when
    # rewriting the README moved that promise into a numbered
    # list and reworded it — and the test then failed for a page that was
    # entirely correct. A test that breaks on a rewrite teaches people to edit
    # the test, which is one step away from deleting it.
    import re

    def _counts(n):
        """Is `n` named right next to the word it is counting?"""
        return re.search(r"\b%d\b[^.\n]{0,80}\b(scripts?|automations?)\b" % n,
                         text, re.I)

    free = len(starters.free_starters())
    assert _counts(free), (
        f"{name} no longer tells a visitor how many ready-made scripts they "
        f"get ({free})")

    # And the Pro-inclusive figure must not be the one on the page — that is
    # the lie this test was written for: a number one larger than what someone
    # can actually run after downloading.
    everything = len(starters.STARTERS)
    if everything != free:
        assert not _counts(everything), (
            f"{name} counts the Pro example among what a new user can run")


def test_the_app_seeds_on_startup():
    """The wiring, not the module: `starters` importing cleanly proves nothing
    if nobody calls it."""
    import ast
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "seed_once"
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "starters"]
    assert calls, "nothing calls starters.seed_once"


@pytest.mark.parametrize("name", [n for n, _ in starters.STARTERS])
def test_every_node_in_a_starter_can_be_opened(name, tmp_path):
    """⚠ This exact mistake shipped in five starter scripts once.

    `flow.py` interprets more node types than the UI can edit. A flow written
    straight against the engine validates, runs, and still contains a node
    that answers a double-click with nothing — and a starter is the *first*
    flow a new user opens, so the trap lands on somebody who has not yet
    learned that anything else in the app works.

    `main._edit_node` opens an editor for exactly four types: action, if, loop
    and goto. Frames carry their own text dialog on the canvas, and Start, End,
    Label and Reroute have nothing to configure. Everything else — `set_var`
    most of all, which is fully implemented and unreachable from the palette —
    is a node the user cannot open.

    Reads the type list out of `main.py` rather than restating it, so adding an
    editor relaxes this test on its own instead of leaving a stale copy behind.
    """
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    body = src.split("def _edit_node", 1)[1].split("\n    def ", 1)[0]
    editable = {getattr(flow, m) for m in re.findall(r"flow\.(N_[A-Z_]+)", body)
                if hasattr(flow, m)}
    assert editable, "could not read the editable types out of _edit_node"

    # Types with nothing to configure, so having no editor is correct.
    nothing_to_edit = {getattr(flow, n) for n in
                       ("N_START", "N_END", "N_LABEL", "N_REROUTE", "N_FRAME")
                       if hasattr(flow, n)}

    g = _load_back(starters.build_all()[name], tmp_path, name="openable")
    for node in g.nodes.values():
        assert node.type in editable | nothing_to_edit, (
            f"{name}: a {node.type!r} node cannot be opened by double-clicking "
            "it, and it is not a type with nothing to configure. This is the "
            "trap that shipped in five starters once — either give the type an "
            "editor or do not put it in a starter.")


class _RecordingExecutor:
    """The four methods `flow.ExecutorProtocol` documents, recording instead
    of acting. Nothing reaches the mouse, the keyboard or the screen.

    ⚠ `running()` counts down. Two of the starters loop until stopped, which is
    the correct behaviour for an auto-clicker and an infinite loop in a test —
    this stands in for the user pressing Stop.
    """

    def __init__(self, budget=400):
        self.actions = []
        self.sensors = []
        self.slept = 0.0
        self._budget = budget

    def running(self) -> bool:
        self._budget -= 1
        return self._budget > 0

    def sleep(self, secs: float):
        self.slept += secs          # never actually waits

    def do_action(self, step: dict, variables) -> bool:
        self.actions.append(step.get("kind"))
        return True

    def eval_sensor(self, cond: dict, variables) -> bool:
        self.sensors.append(cond.get("type"))
        return False                # nothing is on this screen


@pytest.mark.parametrize("name", [n for n, _ in starters.STARTERS])
def test_a_starter_actually_runs(name, tmp_path):
    """⚠ Nothing had ever executed one, and "open one and press Play" is the
    promise the website makes about them by name.

    Every other test here checks a starter's *shape* — that it has work, stays
    inside the free tier, carries a note, needs nothing filled in. A flow can
    satisfy all of that and still abort on its first step: a step kind the
    interpreter does not handle, a Go to naming a label that is not there, a
    loop whose body never reaches its own end.

    Driven through `FlowInterpreter` with a recording executor, so nothing
    touches the mouse, the keyboard or the screen. `running()` counts down,
    standing in for the user pressing Stop — two of these deliberately run
    until stopped.
    """
    import flow

    g = _load_back(starters.build_all()[name], tmp_path, name="run")
    ex = _RecordingExecutor()
    log = []
    status = flow.FlowInterpreter(g, ex, on_log=log.append).run()

    assert status in ("done", "stopped"), (
        f"{name} ended as {status!r}. Log tail: {log[-3:]}")
    errors = [e for e in log if e.get("kind") in ("error", "abort")]
    assert not errors, f"{name} logged {errors}"
    assert ex.actions, f"{name} ran to completion without doing anything"


def test_the_pro_example_reads_the_screen_and_touches_nothing(tmp_path):
    """The one starter that exists to show what Pro does. It must exercise a
    sensor — that is the whole point of it — and it must still send no input,
    which `test_the_pro_example_touches_nothing` pins structurally and this
    pins by running it."""
    import flow

    g = _load_back(starters.build_all()[starters.PRO_EXAMPLE], tmp_path,
                   name="pro")
    ex = _RecordingExecutor()
    status = flow.FlowInterpreter(g, ex).run()

    assert status in ("done", "stopped"), status
    assert ex.actions, "the Pro example did nothing at all"
    # ⚠ It reads the screen through Detect *steps*, not through an If/Loop
    # condition, so the work lands in do_action rather than eval_sensor. The
    # first draft of this test asserted the opposite and failed — the model
    # was wrong, not the starter.
    assert all(a in flow.DETECT_KINDS for a in ex.actions), (
        f"the Pro example sent input: {ex.actions} — somebody who has just "
        "paid should not have their mouse moved by the example")
    assert "wait_text" in ex.actions, (
        "the Pro example no longer demonstrates the OCR step it is named for")
