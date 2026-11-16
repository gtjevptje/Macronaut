"""Unsaved-work recovery for the flow canvas.

⚠ **Why this exists.** Until 4 September 2026, closing Macronaut threw away
whatever was on the canvas. No prompt, no autosave, no restore — `SequenceTab`
started from a fresh graph every launch and nothing read the flow back. Build
something for half an hour, close the window, and it was gone. That is a worse
path than any save bug, because it is the *normal* one: you do not have to be
unlucky, you just have to close the app.

Three answers were on the table:
prompt on close, autosave to a recovery file, or reopen the last saved path.
This module is the second, chosen because the app's whole posture is that you
should not have to know about saving, and because it is the only one of the
three that also survives a crash — `closeEvent` never runs then, but the timer
that calls `write()` has already been running all along.

**The design rule that keeps it from becoming annoying:** nothing here tracks a
"dirty" flag. The canvas is written out as-is, and at startup the file is
offered *only* if it differs from the flow already saved on disk at the path it
came from. Open a script, look at it, close the app — the payload matches the
file, so you are not asked. Edit one node first and you are. Content, not
bookkeeping, decides, which means there is no dirty flag to get out of step
with reality.

**When it stops being offered:** the file is deleted once the question has been
answered either way, and on any successful Save, because at that moment the
flow on disk *is* the work. So the offer appears at most once per loss.

Every public function here swallows its own errors. Two of them are called from
`closeEvent`, which ends in `os._exit(0)` after disarming the crash reporter —
an exception raised on that path would be both fatal and invisible.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from typing import Optional

import flow
import settings as _settings

# ⚠ Must equal `flow_canvas.GRID`, and is duplicated rather than imported
# because that module pulls in PySide6 and this one is read during startup and
# from tests that have no QApplication. `test_recovery` asserts the two agree,
# the same way the Scoop and winget manifests are checked against each other.
#
# It is here at all because of `_norm` below — see the comment there. The short
# version: the canvas snaps every node to this grid the moment it draws it, so
# without this the six flows Macronaut ships with all look edited the instant
# they are opened.
GRID = 26

# Beside settings.json, not beside the user's scripts. This is machine state
# rather than something they made, and `scripts_dir()` is a folder the Library
# lists — a recovery file appearing in it would look like a script they do not
# remember creating.
RECOVERY_FILE = _settings.data_dir() / "recovery.json"

# Serialisation format of the payload wrapper, not of the flow inside it (that
# carries `flow.VERSION` of its own). Bumped only if this wrapper's shape
# changes; `read()` refuses anything newer rather than guessing.
FORMAT = 1


def write(graph, source_path: str = "") -> bool:
    """Save the canvas as the recovery copy. True if something was written.

    Atomic, for the same reason `FlowGraph.save` is: the alternative is opening
    the real file for writing, and on Windows that empties it *before* it can
    fail. A recovery file truncated to zero bytes by a crash mid-write is worse
    than no recovery file, because it is the one thing standing between the
    user and the loss it exists to undo.

    Deliberately **no fsync**. This runs on a timer and again on every close;
    `settings.save` made the same call for the same reason. What fsync buys is
    survival of a power cut, and what it costs is a disk flush every twenty
    seconds forever. The failure this guards against is a crash or a careless
    close, and `os.replace` is enough for both.
    """
    try:
        payload = {
            "format": FORMAT,
            "saved_at": time.time(),
            "path": source_path or "",
            "flow": graph.to_dict(),
        }
        directory = RECOVERY_FILE.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".recovery-",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, str(RECOVERY_FILE))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


def read() -> Optional[dict]:
    """The stored payload, or None if there isn't a usable one.

    Returns None rather than raising for every kind of damage — missing,
    truncated, not JSON, not a dict, from a future format. A recovery file is
    read at startup, and there is no version of "the app would not open" that
    is an acceptable price for a feature whose entire job is being a safety net.
    """
    try:
        with open(RECOVERY_FILE, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("format") != FORMAT:
        return None
    if not isinstance(payload.get("flow"), dict):
        return None
    return payload


def clear() -> None:
    """Forget the recovery copy. Never raises, and a missing file is success."""
    try:
        os.unlink(RECOVERY_FILE)
    except OSError:
        pass
    except Exception:
        pass


def _norm(graph) -> dict:
    """The graph as compared: node coordinates rounded to the canvas grid.

    ⚠ **This function is the difference between a useful feature and one every
    user turns off.** Opening a flow snaps each node to the 26 px grid and
    writes the snapped value back onto the node — so a flow whose file holds
    off-grid coordinates differs from its own file the instant it is displayed,
    without anybody touching anything.

    That is not hypothetical. All six flows Macronaut ships with are stored
    off-grid (`starters.build_all()` places them at -40, -150, 0, 40 …), so a
    raw comparison asked *every new user* to recover a starter they had merely
    looked at, on their second launch. Measured: six of six differ raw, zero of
    six differ once normalised.

    Nothing is lost by rounding. Snapping is idempotent and a real drag lands in
    a different cell; a drag that stays inside one cell puts the node back
    exactly where it was on screen, so there is nothing there to recover.
    """
    d = copy.deepcopy(graph.to_dict())
    for node in d.get("nodes", []):
        try:
            node["x"] = round(float(node.get("x", 0)) / GRID) * GRID
            node["y"] = round(float(node.get("y", 0)) / GRID) * GRID
        except (TypeError, ValueError):
            pass
    return d


def offerable(payload: Optional[dict]):
    """The graph worth offering back, or None.

    Two things disqualify a payload, and both matter:

    **It contains no work.** A bare Start node is what every launch begins
    with; offering to restore one would be the app asking a question about
    nothing. `flow.has_work` is the same predicate Play uses, so "worth
    restoring" and "worth running" cannot drift apart.

    **It matches what is already saved.** The common case by far is opening a
    script from the Library and closing the app without touching it. The
    payload then round-trips to exactly the file it names, nothing was lost,
    and asking would train the user to dismiss the box — at which point the
    one time it mattered gets dismissed too.

    ⚠ The comparison is the whole serialised graph, node positions included.
    Dragging a node and closing counts as unsaved work, because it is.
    """
    if not payload:
        return None
    try:
        graph = flow.FlowGraph.from_dict(payload["flow"])
    except Exception:
        return None
    if not flow.has_work(graph):
        return None

    source = payload.get("path") or ""
    if source:
        try:
            on_disk = flow.FlowGraph.load(source)
        except Exception:
            # Unreadable or gone. That is a *reason* to offer, not to decline:
            # the canvas copy may now be the only one left.
            return graph
        try:
            if _norm(on_disk) == _norm(graph):
                return None
        except Exception:
            pass
    return graph


def describe(payload: dict) -> str:
    """One line for the dialog: how much, and how long ago.

    Written here rather than in the dialog so it can be tested without a Qt
    event loop, and so the vocabulary stays the app's ("steps", not "nodes").
    """
    try:
        nodes = payload.get("flow", {}).get("nodes", [])
        steps = max(0, len(nodes) - 1)     # the Start node is not a step
    except Exception:
        steps = 0
    step_text = "1 step" if steps == 1 else f"{steps} steps"

    try:
        age = max(0.0, time.time() - float(payload.get("saved_at", 0)))
    except (TypeError, ValueError):
        age = 0.0
    if age < 90:
        when = "moments ago"
    elif age < 3600:
        when = f"{int(age // 60)} minutes ago"
    elif age < 86400:
        hours = int(age // 3600)
        when = "an hour ago" if hours == 1 else f"{hours} hours ago"
    else:
        days = int(age // 86400)
        when = "yesterday" if days == 1 else f"{days} days ago"
    return f"{step_text}, last on the canvas {when}"
