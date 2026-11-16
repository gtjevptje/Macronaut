"""Per-node run durations, measured on this machine.

Why this is a sidecar and not part of the flow's JSON
-----------------------------------------------------
How long a Detect takes is a property of *this* screen, *this* game and *this*
CPU — not of the script. A shared script must not arrive carrying someone
else's timings and then draw a timeline that is wrong for everybody who opens
it. Same reasoning that keeps the script-launcher hotkeys in settings rather
than in each flow: which key launches what is a fact about this keyboard.

So the measurements live beside the app, keyed by the flow, and a flow that
travels leaves them behind.

What is stored
--------------
The last ``KEEP`` per-visit averages for each node id::

    {"nodes": {"n4": [812, 799, 1204], "n7": [15, 16, 15]}}

The **median** is what gets read back. A mean would let one 30 s Detect that
happened to wait for a slow launch move the whole picture; a median needs half
the samples to agree before it moves at all, which is the behaviour you want
from a number that decides how wide a box is drawn.

Qt-free and failure-tolerant on purpose: this is decoration for a timeline. A
corrupt or unreadable file means "no measurements yet", never an error.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

KEEP = 9            # samples kept per node
_MAX_NODES = 2000   # a runaway graph must not grow the file without bound


def stats_dir() -> Path:
    from settings import data_dir
    d = data_dir() / "runstats"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def key_for(name: str) -> str:
    """A filename-safe key for a flow.

    An unsaved flow has no name and gets one shared bucket. That is deliberate:
    the alternative is a fresh file per unsaved graph, and the timings of the
    thing you were editing five minutes ago are not the timings of this one.
    """
    name = (name or "").strip()
    if not name:
        return "_unsaved"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    # A name of nothing but dots survives the character filter and would become
    # "." or ".." — a path component, not a filename.
    return (safe or "_unsaved")[:80]


def _path(key: str) -> Path:
    return stats_dir() / f"{key_for(key)}.json"


def load(key: str) -> Dict[str, List[int]]:
    try:
        raw = json.loads(_path(key).read_text(encoding="utf-8"))
        nodes = raw.get("nodes") or {}
        return {str(k): [int(v) for v in vs][:KEEP]
                for k, vs in nodes.items() if isinstance(vs, list)}
    except Exception:
        return {}


def medians(key: str) -> Dict[str, int]:
    """node id -> median measured duration in ms."""
    out: Dict[str, int] = {}
    for nid, samples in load(key).items():
        if not samples:
            continue
        s = sorted(samples)
        out[nid] = int(s[len(s) // 2])
    return out


def record(key: str, timings: Dict[str, float]) -> Dict[str, int]:
    """Fold one run's per-node averages in, newest first. Returns the medians.

    Newest first, and the trim takes from the tail, so a flow that was edited
    stops being described by what it used to do after ``KEEP`` runs rather than
    never.
    """
    if not timings:
        return medians(key)
    data = load(key)
    for nid, ms in list(timings.items())[:_MAX_NODES]:
        try:
            v = int(round(float(ms)))
        except (TypeError, ValueError):
            continue
        if v < 0:
            continue
        data[str(nid)] = ([v] + data.get(str(nid), []))[:KEEP]
    try:
        _path(key).write_text(
            json.dumps({"nodes": data}, separators=(",", ":")),
            encoding="utf-8")
    except Exception:
        pass    # a timeline that cannot remember is still a timeline
    out: Dict[str, int] = {}
    for nid, samples in data.items():
        if samples:
            s = sorted(samples)
            out[nid] = int(s[len(s) // 2])
    return out


def forget(key: str) -> None:
    try:
        _path(key).unlink()
    except Exception:
        pass
