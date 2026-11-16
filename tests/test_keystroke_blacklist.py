"""
The key blacklist — the safety net that was not held by anything.

Settings ▸ Key blacklist says, in the app's own words: "any key listed here
will never be sent by scripts or keystroke automation (e.g. block Win or
Alt+F4)". It is what stops a script from opening the Start menu or closing the
window it is working in, and it is the sort of thing somebody switches on
precisely because a script has already misbehaved once.

⚠ It survived a mutation. A mutation audit replaced

    def _blocked(self, key_str): return key_str.lower() in self._blockset

with `return False` — every blocked key sent, the whole feature off — and the
entire suite stayed green (8 September 2026). The audit's record said "17
mutations, all caught"; this one is not in that set.

Both paths matter and they are different code: a key/combo step checks each key
before pressing any of them, and a text step filters blocked characters out of
the string.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flow
import flow_exec


class _Spy:
    """Stands in for the keyboard backend and records everything asked of it."""

    def __init__(self):
        self.pressed = []
        self.released = []
        self.typed = []

    def press(self, key):
        self.pressed.append(str(key))

    def release(self, key):
        self.released.append(str(key))

    def type(self, text, **kw):
        self.typed.append(text)


def _worker(blacklist):
    fw = flow_exec.FlowWorker(flow.FlowGraph(), blacklist=blacklist)
    fw._kb = _Spy()
    fw._running = True
    fw.sleep = lambda s: None
    return fw, fw._kb


# ── a key step ───────────────────────────────────────────────────────────────

def test_a_blacklisted_key_is_never_pressed():
    fw, kb = _worker(["f4"])
    fw.do_action({"kind": "key", "data": {"keys": ["f4"]}}, {})
    assert kb.pressed == [] and kb.released == [], (
        f"f4 was sent anyway: pressed={kb.pressed}")


def test_blocking_is_case_insensitive():
    """The list is typed by hand into a text box. "F4" and "f4" are the same
    key, and a net with a hole that size is not a net."""
    fw, kb = _worker(["F4"])
    fw.do_action({"kind": "key", "data": {"keys": ["f4"]}}, {})
    assert kb.pressed == []
    fw2, kb2 = _worker(["win"])
    fw2.do_action({"kind": "key", "data": {"keys": ["WIN"]}}, {})
    assert kb2.pressed == []


def test_one_blocked_key_stops_the_whole_combo():
    """⚠ Alt+F4 is the example in the settings text, and it is the reason this
    is all-or-nothing. Pressing the alt and skipping the f4 would leave a
    modifier down and send half a gesture; the combo is one thing."""
    fw, kb = _worker(["f4"])
    fw.do_action({"kind": "combo", "data": {"keys": ["alt", "f4"]}}, {})
    assert kb.pressed == [], f"part of Alt+F4 was sent: {kb.pressed}"


def test_a_key_not_on_the_list_still_works():
    """The net must not be a wall — this is the path every ordinary flow
    takes, and a blacklist that blocked everything would pass every test
    above."""
    fw, kb = _worker(["f4"])
    fw.do_action({"kind": "key", "data": {"keys": ["a"]}}, {})
    assert kb.pressed, "an ordinary key was blocked too"


def test_an_empty_blacklist_blocks_nothing():
    fw, kb = _worker([])
    fw.do_action({"kind": "key", "data": {"keys": ["f4"]}}, {})
    assert kb.pressed, "nothing is on the list, so nothing should be blocked"


def test_a_held_key_cannot_be_blacklisted_into_staying_down():
    """⚠ Hold-down is the dangerous mode: a key that goes down and is then
    blocked on the way up would be held forever. The check happens before the
    press, so a blocked key never goes down in the first place."""
    fw, kb = _worker(["win"])
    fw.do_action({"kind": "key", "data": {"keys": ["win"],
                                          "mode": flow.KEY_DOWN}}, {})
    assert kb.pressed == []
    assert not fw._held, f"a blocked key was registered as held: {fw._held}"


# ── a text step ──────────────────────────────────────────────────────────────

def test_a_blocked_character_is_filtered_out_of_typed_text():
    """A different code path from the key step: the string is filtered rather
    than the step refused, so the rest of the sentence still arrives."""
    fw, kb = _worker(["x"])
    fw.do_action({"kind": "text", "data": {"text": "axbxc"}}, {})
    sent = "".join(kb.typed)
    assert "x" not in sent, f"typed {sent!r}"
    assert sent == "abc", f"typed {sent!r} — the rest of the text should stand"


def test_text_with_nothing_blocked_is_untouched():
    fw, kb = _worker(["x"])
    fw.do_action({"kind": "text", "data": {"text": "hello"}}, {})
    assert "".join(kb.typed) == "hello"


# ── the wiring ───────────────────────────────────────────────────────────────

def test_the_running_flow_is_given_the_users_blacklist():
    """⚠ Read with `ast`. Every test above builds the worker with a blacklist
    by hand; if the app never passes the setting, the feature is off for real
    users and all of them still pass.
    """
    import ast

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    passes_it = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = getattr(f, "attr", None) or getattr(f, "id", None)
        if name != "FlowWorker":
            continue
        for kw in node.keywords:
            if kw.arg == "blacklist":
                passes_it = True
    assert passes_it, (
        "main.py builds a FlowWorker without a blacklist= argument, so the "
        "user's blocked keys are never given to the engine that sends keys")
