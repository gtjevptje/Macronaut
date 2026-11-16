"""Unsaved-work recovery, and reopening where you left off.

⚠ **The bug these exist for.** Until 4 September 2026, closing Macronaut threw
away whatever was on the canvas — no prompt, no autosave, no restore. Half an
hour of work, gone, on the *normal* path out of the app.

The half of this file that matters most is not `recovery.py`'s functions, which
are small and easy. It is the **wiring**: that `_save_state` really calls the
writer on the way out, that the timer really runs, that Save really retires the
copy, and that answering the dialog really deletes it. A safety net whose
mechanism is perfect and whose caller was never hooked up is exactly the shape
of the original bug, and it would test green all day.
"""
import json
import os
import time

import pytest

import flow
import recovery


@pytest.fixture(autouse=True)
def _clean_slate():
    """No test may inherit another's recovery file, or leave one behind.

    ⚠ `recovery.RECOVERY_FILE` is a module constant resolved at import, so it
    is the sandbox path conftest installed before any test module loaded — the
    same trap documented there for `SETTINGS_FILE`. Asserted rather than
    assumed, because the failure mode is silently reading and writing the
    developer's real `~/.macronaut/recovery.json`.
    """
    import settings as _settings
    assert str(recovery.RECOVERY_FILE).startswith(str(_settings.data_dir())), (
        "recovery is writing outside the test sandbox")
    recovery.clear()
    yield
    recovery.clear()


def _worked_graph(label: str = "hello"):
    """A flow with actual work in it — Start plus one Type step."""
    g = flow.FlowGraph()
    g.add_node(flow.N_START, {"name": flow.START_NAME}, x=-280, y=-20)
    g.add_node(flow.N_ACTION, {"kind": "type", "text": label}, x=0, y=0)
    assert flow.has_work(g)
    return g


# ── the mechanism ────────────────────────────────────────────────────────────

def test_a_flow_survives_the_round_trip_through_the_recovery_file():
    g = _worked_graph()
    assert recovery.write(g, "")
    back = recovery.offerable(recovery.read())
    assert back is not None
    assert back.to_dict() == g.to_dict()


def test_a_bare_start_node_is_never_offered_back():
    """Every launch begins with one. Offering to restore it is a box about
    nothing, and a box about nothing teaches people to dismiss boxes."""
    empty = flow.FlowGraph()
    empty.add_node(flow.N_START, {"name": flow.START_NAME}, x=-280, y=-20)
    assert recovery.write(empty, "")
    assert recovery.offerable(recovery.read()) is None


def test_a_flow_identical_to_its_saved_file_is_not_offered(tmp_path):
    """The common case: open a script from the Library, look at it, quit.

    Nothing was lost, so nothing should be asked. This is the whole reason the
    module compares content instead of keeping a dirty flag — there is no flag
    to fall out of step with the file.
    """
    path = tmp_path / "script.json"
    g = _worked_graph()
    g.save(str(path))
    assert recovery.write(g, str(path))
    assert recovery.offerable(recovery.read()) is None


def test_one_moved_node_is_enough_to_be_offered(tmp_path):
    """⚠ Positions count as work. Someone who spent ten minutes untangling a
    diagram and closed the window has lost ten minutes of work, even though
    every node and edge is still the one that is on disk."""
    path = tmp_path / "script.json"
    g = _worked_graph()
    g.save(str(path))
    moved = flow.FlowGraph.load(str(path))
    node = [n for n in moved.nodes.values() if n.type != flow.N_START][0]
    node.x += 3 * recovery.GRID          # three cells over — plainly a move
    assert recovery.write(moved, str(path))
    assert recovery.offerable(recovery.read()) is not None


def test_the_grid_here_is_the_canvas_grid(qapp):
    """`recovery.GRID` is a copy, so the copy is checked. Same reasoning as the
    Scoop/winget manifest agreement test: duplicating a constant is fine, and
    letting the duplicate drift is not."""
    import flow_canvas
    assert recovery.GRID == flow_canvas.GRID


def test_merely_opening_a_shipped_starter_is_not_treated_as_editing_it(qapp,
                                                                      tmp_path):
    """⚠ The nag that would have shipped, and the reason `recovery._norm` exists.

    All six bundled flows are stored off-grid. Putting one on the canvas snaps
    every node to the 26 px grid and writes the snapped value straight back onto
    the node — so the in-memory flow differs from its own file before the user
    has touched anything. Raw, that asks **every new user** to recover a starter
    they only looked at, on their second launch; and a box that cries wolf on
    launch two is a box nobody reads on the day it matters.

    Driven through the real `FlowCanvas` rather than by rounding numbers by
    hand, because the claim being tested is about what the canvas does.
    """
    import flow_canvas
    import starters

    offered = []
    for name, graph in starters.build_all().items():
        path = tmp_path / f"{name}.json"
        graph.save(str(path))                      # as the Library seeds it

        opened = flow.FlowGraph.load(str(path))    # as opening it does
        canvas = flow_canvas.FlowCanvas(opened)
        canvas.set_graph(opened)
        try:
            recovery.clear()
            assert recovery.write(opened, str(path))
            if recovery.offerable(recovery.read()) is not None:
                offered.append(name)
        finally:
            canvas.hide()

    assert not offered, (
        "opening these and closing the app would ask to 'recover' them:\n  "
        + "\n  ".join(offered))


def test_a_nudge_too_small_to_move_a_node_is_not_work(tmp_path):
    """The other side of the same coin. Sub-grid deltas do not survive the
    canvas — the node lands back in the cell it was already in — so there is
    nothing on screen to restore and nothing to ask about."""
    path = tmp_path / "script.json"
    g = _worked_graph()
    g.save(str(path))
    nudged = flow.FlowGraph.load(str(path))
    node = [n for n in nudged.nodes.values() if n.type != flow.N_START][0]
    node.x += 2                          # well inside one grid cell
    assert recovery.write(nudged, str(path))
    assert recovery.offerable(recovery.read()) is None


def test_a_flow_whose_file_has_vanished_is_still_offered(tmp_path):
    """An unreadable source is a reason to offer, not to decline — the canvas
    copy may be the only one left."""
    path = tmp_path / "gone.json"
    g = _worked_graph()
    g.save(str(path))
    assert recovery.write(g, str(path))
    os.unlink(path)
    assert recovery.offerable(recovery.read()) is not None


@pytest.mark.parametrize("damage,label", [
    ("", "empty file"),
    ("{", "truncated JSON"),
    ("[1, 2, 3]", "valid JSON, not a payload"),
    ('{"format": 99, "flow": {}}', "a format from the future"),
    ('{"format": 1, "flow": "not a graph"}', "flow is not a dict"),
])
def test_a_damaged_recovery_file_never_reaches_the_startup_path(damage, label):
    """⚠ This is read while the window is coming up. There is no version of
    "Macronaut would not open" that is an acceptable price for a safety net."""
    recovery.RECOVERY_FILE.write_text(damage, encoding="utf-8")
    assert recovery.read() is None, label
    assert recovery.offerable(recovery.read()) is None, label


def test_writing_the_recovery_copy_never_replaces_a_good_one_with_a_stub(tmp_path,
                                                                        monkeypatch):
    """Atomic, for the reason `FlowGraph.save` is.

    On Windows `open(path, "w")` empties the file as part of *opening* it, and
    raises afterwards — so a naive writer that fails mid-way leaves zero bytes
    where the recovery copy used to be. That is strictly worse than having no
    recovery file at all, because it is the one thing standing between the user
    and the loss this module exists to undo.
    """
    good = _worked_graph("keep me")
    assert recovery.write(good, "")
    before = recovery.RECOVERY_FILE.read_bytes()

    def _explode(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(recovery.json, "dump", _explode)
    assert recovery.write(_worked_graph("lose me"), "") is False
    assert recovery.RECOVERY_FILE.read_bytes() == before
    # And no debris beside it — the temp file is cleaned up on the way out.
    leftovers = [p for p in recovery.RECOVERY_FILE.parent.iterdir()
                 if p.name.startswith(".recovery-")]
    assert leftovers == []


def test_describe_says_how_much_and_how_long_ago():
    g = _worked_graph()
    recovery.write(g, "")
    payload = recovery.read()
    text = recovery.describe(payload)
    # One Type step; the Start node is not a step and must not be counted.
    assert "1 step" in text and "2 step" not in text
    assert "moments ago" in text

    payload["saved_at"] = time.time() - 7200
    assert "2 hours ago" in recovery.describe(payload)
    payload["saved_at"] = time.time() - 90000
    assert "yesterday" in recovery.describe(payload)


def test_clear_is_happy_when_there_is_nothing_to_clear():
    recovery.clear()
    recovery.clear()          # twice, from a cold start — must not raise
    assert recovery.read() is None


# ── the wiring (this is the half that would have caught the original bug) ────

@pytest.fixture
def window(qapp):
    """A MainWindow that is shut down but never close()d — see
    test_gui_offscreen's identical fixture for why close() ends the run."""
    import main
    w = main.MainWindow()
    yield w
    try:
        w._shutdown()
    except Exception:
        pass
    w.hide()


def test_the_close_path_actually_writes_the_canvas_out(window):
    """⚠ The bug was never in a function — it was that nothing called one.

    `_save_state` is what both quit paths run (there is a test next door
    asserting that), so hooking it is what makes closing the window safe. This
    drives the real method rather than `recovery.write`, because a perfect
    writer nobody calls is precisely the thing that shipped.
    """
    tab = window._sequence_tab
    tab._graph = _worked_graph("half an hour of work")
    tab._canvas.set_graph(tab._graph)
    recovery.clear()

    window._save_state()

    back = recovery.offerable(recovery.read())
    assert back is not None, "closing the app still discards the canvas"
    assert back.to_dict() == tab._graph.to_dict()


def test_the_autosave_timer_is_running_so_a_crash_is_survivable(window):
    """`closeEvent` does not run when the app crashes. The timer is the only
    part of this feature that covers that case, so "is it actually started" is
    load-bearing rather than cosmetic."""
    tab = window._sequence_tab
    assert tab._recovery_timer.isActive()
    assert tab._recovery_timer.interval() <= 60_000

    tab._graph = _worked_graph("crash me")
    recovery.clear()
    tab._recovery_timer.timeout.emit()      # what the timer does when it fires
    assert recovery.offerable(recovery.read()) is not None


def test_an_idle_canvas_does_not_rewrite_the_file_every_tick(window, monkeypatch):
    """The write is skipped when the serialised graph has not changed, which
    while nobody is editing is every tick. Asserted because "autosave every
    twenty seconds forever" is otherwise a real cost on a laptop battery.

    ⚠ This counts calls rather than watching the file's mtime, and the first
    version did the latter and was flaky. Windows stamps file times from the
    same coarse 15.625 ms system clock that `clicker._sleep` was caught pacing
    on, so two writes inside one tick share an mtime exactly — the test then
    reports "it skipped the write" about a write that plainly happened. One run
    in eight failed. Counting is what the assertion was always about anyway.
    """
    tab = window._sequence_tab
    writes = []
    real = recovery.write
    monkeypatch.setattr(recovery, "write",
                        lambda *a, **k: (writes.append(1), real(*a, **k))[1])

    tab._graph = _worked_graph("steady")
    tab._write_recovery()
    assert len(writes) == 1

    tab._write_recovery()
    tab._write_recovery()
    assert len(writes) == 1, "an unchanged canvas was written out again"

    # ...but an edit is picked up.
    tab._graph.add_node(flow.N_ACTION, {"kind": "type", "text": "more"}, x=90, y=0)
    tab._write_recovery()
    assert len(writes) == 2, "an edited canvas was not written out"

    # ...and the close path writes regardless of whether anything changed.
    tab._write_recovery(force=True)
    assert len(writes) == 3, "the forced close-path write was skipped"


def test_saving_the_flow_retires_the_recovery_copy(window, tmp_path, monkeypatch):
    """Half of the "when does it stop offering" rule. After a Save the work IS
    the file, so being asked about it on next launch would be nonsense."""
    import main
    tab = window._sequence_tab
    tab._graph = _worked_graph("save me")
    tab._write_recovery()
    assert recovery.read() is not None

    target = tmp_path / "saved.json"
    monkeypatch.setattr(main.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **kw: (str(target), "")))
    tab._save()

    assert target.exists(), "the save itself must still have happened"
    assert recovery.read() is None
    assert tab._recovery_blob is None


def test_declining_the_offer_retires_it_for_good(window, monkeypatch):
    """The other half of the rule. A recovery copy that outlives its own
    question comes back next launch as a flow the user already said no to."""
    import main
    tab = window._sequence_tab
    before = tab._graph.to_dict()
    recovery.write(_worked_graph("not this"), "")

    monkeypatch.setattr(main.QMessageBox, "question",
                        staticmethod(lambda *a, **kw: main.QMessageBox.No))
    window._offer_recovery()

    assert recovery.read() is None
    assert tab._graph.to_dict() == before, "declining must not touch the canvas"


def test_accepting_the_offer_puts_the_flow_back_on_the_canvas(window, monkeypatch):
    import main
    tab = window._sequence_tab
    wanted = _worked_graph("give it back")
    recovery.write(wanted, "")

    monkeypatch.setattr(main.QMessageBox, "question",
                        staticmethod(lambda *a, **kw: main.QMessageBox.Yes))
    window._offer_recovery()

    # ⚠ Normalised, because putting the flow on the canvas snaps it to the grid
    # — the very effect `_norm` exists for. Comparing raw here would fail for a
    # restore that is in fact perfect.
    assert recovery._norm(tab._graph) == recovery._norm(wanted)
    texts = [n.data.get("text") for n in tab._graph.nodes.values()
             if n.type == flow.N_ACTION]
    assert texts == ["give it back"], "the work itself has to come back, not a shell"
    assert tab._canvas.scene_().graph is tab._graph, (
        "the canvas must be showing the restored flow, not still drawing the old one")
    assert recovery.read() is None


def test_nothing_is_asked_when_there_is_nothing_to_recover(window, monkeypatch):
    """The overwhelmingly common launch. If this box can appear when no work
    was lost, every other test here is worthless — people stop reading it."""
    import main
    asked = []
    monkeypatch.setattr(main.QMessageBox, "question",
                        staticmethod(lambda *a, **kw: asked.append(1)
                                     or main.QMessageBox.No))
    recovery.clear()
    window._offer_recovery()
    assert asked == []

    # ...and the same for a stale copy that matches nothing worth restoring.
    empty = flow.FlowGraph()
    empty.add_node(flow.N_START, {"name": flow.START_NAME}, x=0, y=0)
    recovery.write(empty, "")
    window._offer_recovery()
    assert asked == []
    assert recovery.read() is None, "a payload not worth offering is not kept"


# ── where you left off ───────────────────────────────────────────────────────
#
# `_restore_last_script` is the cheap third answer to the same problem: it
# reopens the last *saved* script, so on its own it recovers nothing unsaved.
# It is here rather than in test_gui_offscreen because the two features share a
# canvas and the interesting cases are the ones where they disagree.


@pytest.fixture
def fresh_window(qapp):
    """A MainWindow built *after* the test has arranged settings on disk.

    ⚠ `_restore_last_script` runs during construction, so the `window` fixture
    above is useless for it — by the time a test can set `last_sequence_path`
    the window has already decided what to open. Every test here therefore
    builds its own, and hands back the constructor so the settings can be
    written first.
    """
    import main
    built = []

    def build():
        w = main.MainWindow()
        built.append(w)
        return w

    yield build
    for w in built:
        try:
            w._shutdown()
        except Exception:
            pass
        w.hide()


def _remember(path) -> None:
    """Write `last_sequence_path` the way the app does, then flush it."""
    from settings import SettingsManager
    sm = SettingsManager()
    sm.set("last_sequence_path", str(path))
    sm.save()


def test_the_script_you_had_open_is_open_again_next_launch(fresh_window,
                                                           tmp_path):
    """⚠ The setting was written by three code paths and read by none.

    Someone who picked a script in Basic, used it, and quit came back to
    "— no script —" the next morning, and the morning after that.
    """
    path = tmp_path / "my clicker.json"
    g = _worked_graph("the one I was using")
    g.save(str(path))
    _remember(path)

    w = fresh_window()
    texts = [n.data.get("text") for n in w._sequence_tab._graph.nodes.values()
             if n.type == flow.N_ACTION]
    assert texts == ["the one I was using"]


def test_the_basic_dropdown_agrees_with_what_is_loaded(fresh_window):
    """A restored canvas the Basic face still calls "— no script —" is worse
    than not restoring it: the two disagree, and one of them is lying."""
    import settings as _settings
    path = _settings.scripts_dir() / "chosen.json"
    _worked_graph("chosen").save(str(path))
    _remember(path)

    w = fresh_window()
    assert w._compact.current_script() == "chosen"


def test_someone_who_never_chose_a_script_sees_no_change(fresh_window):
    """The blast radius. `last_sequence_path` is empty until you save a flow or
    pick one, so the plain auto-clicker user must open on the same empty canvas
    the app has always given them."""
    _remember("")
    w = fresh_window()
    assert not flow.has_work(w._sequence_tab._graph)
    assert w._compact.current_script() == ""


@pytest.mark.parametrize("damage,label", [
    (None, "the file was deleted"),
    ("{", "the file is truncated"),
    ('{"nodes": "nonsense"}', "valid JSON, not a flow"),
])
def test_an_unopenable_last_script_is_not_allowed_to_block_startup(
        fresh_window, tmp_path, damage, label):
    """⚠ Silent on purpose. A dialog here greets somebody who has not asked for
    anything yet, on a launch where the app is otherwise perfectly usable."""
    path = tmp_path / "broken.json"
    if damage is not None:
        path.write_text(damage, encoding="utf-8")
    _remember(path)

    w = fresh_window()                    # must not raise
    assert not flow.has_work(w._sequence_tab._graph), label


def test_choosing_no_script_is_remembered_too(window):
    """Otherwise "— no script —" is a setting that only holds until you close
    the window, and the script you just dismissed is back in the morning."""
    import settings as _settings
    path = _settings.scripts_dir() / "dismiss me.json"
    _worked_graph("dismiss me").save(str(path))

    window._on_script_selected("dismiss me")
    assert window._settings.s.last_sequence_path

    window._on_script_selected("— no script —")
    assert window._settings.s.last_sequence_path == ""
    assert not flow.has_work(window._sequence_tab._graph)


def test_unsaved_work_outranks_the_script_you_had_open(fresh_window, tmp_path,
                                                       monkeypatch):
    """⚠ The two features share one canvas, so the order they run in is the
    whole design. `_restore_last_script` runs during construction and the
    recovery offer fires later from the event loop, which means the flow the
    user said "yes, that one" to has the last word — as it must, since it is
    the copy that is not on disk anywhere else."""
    import main

    saved = tmp_path / "on disk.json"
    _worked_graph("the saved one").save(str(saved))
    _remember(saved)
    recovery.write(_worked_graph("the unsaved one"), "")

    monkeypatch.setattr(main.QMessageBox, "question",
                        staticmethod(lambda *a, **kw: main.QMessageBox.Yes))
    w = fresh_window()
    assert flow.has_work(w._sequence_tab._graph), "construction restores the saved one"
    w._offer_recovery()                   # what singleShot(0) fires

    texts = [n.data.get("text") for n in w._sequence_tab._graph.nodes.values()
             if n.type == flow.N_ACTION]
    assert texts == ["the unsaved one"]


def test_reopening_the_same_script_untouched_asks_nothing(fresh_window,
                                                          tmp_path, monkeypatch):
    """The two features agreeing, which is the common case: quit with a saved
    script open, and the next launch restores it and says nothing at all."""
    import main

    saved = tmp_path / "steady.json"
    g = _worked_graph("steady")
    g.save(str(saved))
    _remember(saved)

    w = fresh_window()
    # ⚠ Asserted before the interesting part, or this test passes for the wrong
    # reason: with nothing restored the canvas is empty, an empty canvas is
    # never offered back, and "asked nothing" becomes true of a broken app.
    assert flow.has_work(w._sequence_tab._graph)
    w._save_state()                       # close

    asked = []
    monkeypatch.setattr(main.QMessageBox, "question",
                        staticmethod(lambda *a, **kw: asked.append(1)
                                     or main.QMessageBox.No))
    w2 = fresh_window()                   # next launch
    w2._offer_recovery()
    assert asked == [], "restoring a script must not also offer to recover it"


def test_a_damaged_payload_is_dropped_rather_than_asked_about_forever(window,
                                                                     monkeypatch):
    """⚠ The `finally` in `_offer_recovery`. A payload that raises mid-restore
    must still be retired, or it asks the same failing question at every launch
    from now on."""
    import main
    recovery.RECOVERY_FILE.write_text(
        json.dumps({"format": recovery.FORMAT, "saved_at": time.time(),
                    "path": "", "flow": {"nodes": "broken"}}),
        encoding="utf-8")
    monkeypatch.setattr(main.QMessageBox, "question",
                        staticmethod(lambda *a, **kw: main.QMessageBox.Yes))
    window._offer_recovery()
    assert recovery.read() is None


def test_starting_the_app_makes_the_offer_without_being_asked(fresh_window,
                                                              monkeypatch):
    """⚠ Wiring. Every other test in this file calls `_offer_recovery()` itself.

    Found by mutation audit on 4 September 2026: deleting
    `QTimer.singleShot(0, self._offer_recovery)` from `MainWindow.__init__`
    broke **nothing** in 897 tests. The whole feature would have stopped
    working — an unsaved canvas kept faithfully and never once offered back —
    and the suite would have stayed green.

    So this one never calls it. It writes a payload, builds a window, lets the
    event loop turn, and expects the flow to be back on the canvas.
    """
    import main
    from PySide6.QtWidgets import QApplication

    # ⚠ Drain first, and this is not superstition. Every MainWindow built in
    # this file schedules its own `singleShot(0, _offer_recovery)`, and almost
    # no test turns the event loop — so by the time this one runs there is a
    # queue of pending offers belonging to earlier, still-alive windows. The
    # first `processEvents()` below fires all of them; one of those old windows
    # then takes the payload, restores it into *its* canvas and clears the
    # file, and this test sees an empty canvas and fails. It passed in
    # isolation and failed in the file, which is the signature of exactly this.
    for _ in range(8):
        QApplication.processEvents()

    recovery.write(_worked_graph("offer me unprompted"), "")
    monkeypatch.setattr(main.QMessageBox, "question",
                        staticmethod(lambda *a, **kw: main.QMessageBox.Yes))

    w = fresh_window()
    for _ in range(8):                      # singleShot(0) needs one turn
        QApplication.processEvents()

    texts = [n.data.get("text") for n in w._sequence_tab._graph.nodes.values()
             if n.type == flow.N_ACTION]
    assert texts == ["offer me unprompted"], (
        "the app started and never offered the unsaved script back")
    assert recovery.read() is None, "the payload was not retired"
