"""Which steps a licence covers, and what happens when a flow uses one.

`licensing.py` answers "is this copy licensed?". This module answers "does that
matter for what is about to run?" — in one file, so the rule is a tuple and a
constant rather than checks scattered through the UI.

⚠ **Nothing is checked.** `ENFORCED` is False: every step runs for everyone,
nothing is marked on the canvas and no licence dialog can appear. What follows
is what the check does when it is on, and it stays under test so that it does
not rot.

## The two groups

**Open-loop steps need nothing.** Clicking, typing, key presses, drags,
scrolls, waits, and sequences of them up to `FREE_MAX_STEPS`: they happen on a
timer whether or not they worked.

**Closed-loop steps are the covered group** — the flow looks at the screen and
decides:

- **Seeing the screen** — Wait-for-Image, Wait-for-Text (OCR), Wait-for-Pixel.
- **Reacting to what it sees** — If/Else, Loop, Go to.

⚠ **Variables are engine-only.** `flow.N_SETVAR`, `flow.apply_set_var` and the
`var` condition kind are all implemented and all run, but nothing in the UI
can create either one: the palette is nine buttons and Set Var is not among
them, and `flow_dialogs.ConditionWidget.TYPES` offers image / text / pixel /
always. `PRO_NODE_TYPES` still lists the node so that an old saved flow
containing one is classified correctly — that is the only reason.

## Where the check runs

At the moment Play is pressed, and nowhere else. Specifically **not**:

- **Not when a flow is loaded or edited.** Anyone must be able to open, read,
  edit and save a flow whatever steps it uses. A gate that refused to open a
  file would destroy the user's own work.
- **Not per node, mid-run.** A flow that stops halfway has already moved the
  mouse around someone's screen for thirty seconds and then abandoned it in an
  unknown state. The check is up-front and total: it runs, or it does not
  start.
- **Not by hiding the steps.** They stay in the palette, openable and
  configurable.
"""
from __future__ import annotations

from typing import List, Tuple

import flow
import licensing

# Detect steps: the flow looks at the screen. `flow.DETECT_KINDS` is the same
# tuple, and is imported rather than copied so that adding a fourth detector
# cannot silently leave it out of the covered group.
PRO_ACTION_KINDS = frozenset(flow.DETECT_KINDS)

# Control flow: the flow decides. `N_LABEL` is deliberately absent — a label is
# a name for a place, useless on its own, and only reachable by the Go to that
# is listed. `N_REROUTE`, `N_COMMENT` and `N_FRAME` are absent because they are
# drawing, not behaviour.
PRO_NODE_TYPES = frozenset({flow.N_IF, flow.N_LOOP, flow.N_SETVAR, flow.N_GOTO})

# How many working steps a flow may run without a licence.
#
# ⚠ Move it *up* rather than down: tightening it takes a working flow away
# from someone who already had it.
FREE_MAX_STEPS = 20

BUY_URL = "https://gtjevptje.github.io/Macronaut/#buy"

# Where the source is. Macronaut is GPL-3.0-or-later since 30 August 2026, and
# this link is what makes that checkable.
#
# ⚠ It earns its place in the *app* — Settings ▸ About opens it — because of
# what Macronaut is: an unsigned .exe that installs a global keyboard hook.
# "Read the code" is the only answer to that which does not ask for trust, and
# it has to be reachable from inside the program itself.
#
# ⚠ One repository since 2.3.6. Every copy up to 2.3.5 opens
# `gtjevptje/macronaut-source` instead, which is archived and points here.
SOURCE_URL = "https://github.com/gtjevptje/Macronaut"

# Shown in the licence dialogs. Here rather than in `licensing_ui` so that
# reading them does not require importing Qt.
PRICE = "€9.99"
TERMS = "One payment. Yours forever, on every computer you own."

# Where to reach the author. `licensing_ui.py` shows it.
CONTACT_EMAIL = "gerbenvanpoucke0@gmail.com"

# ⚠⚠ NOTHING IS ENFORCED. Everything below still classifies steps; this
# decides whether the classification stops anything, and it does not. Every
# step runs for everyone.
#
# ⚠ Releases up to 2.1.1 shipped no licence check at all, so switching this on
# would take working automations away from people who already have them.
#
# ⚠ It gates enforcement ONLY, never classification. `pro_features_used`,
# `is_node_pro`, `runs_on_free` and `PRO_*` go on answering exactly as before,
# so the split stays under test rather than rotting behind a flag.
ENFORCED = False


def _label_for_kind(kind: str) -> str:
    return {"wait_image": "Wait for image", "wait_text": "Wait for text",
            "wait_pixel": "Wait for pixel"}.get(kind, kind)


def _label_for_type(ntype: str) -> str:
    return {flow.N_IF: "If / Else", flow.N_LOOP: "Loop",
            flow.N_SETVAR: "Set variable", flow.N_GOTO: "Go to"}.get(ntype, ntype)


def pro_features_used(graph) -> List[str]:
    """The Pro features this flow contains, named the way the palette names
    them, de-duplicated, in a stable order. Empty for a flow that runs free.

    Reports *what is in the flow*, not what the licence permits — so the same
    call can label the canvas whether or not this copy is licensed.
    """
    found = []
    for node in graph.nodes.values():
        if node.type in PRO_NODE_TYPES:
            found.append(_label_for_type(node.type))
        elif flow.action_kind(node) in PRO_ACTION_KINDS:
            found.append(_label_for_kind(flow.action_kind(node)))
    seen, ordered = set(), []
    for name in found:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def step_count(graph) -> int:
    """Working steps, by the same definition Play already uses for "is this
    flow empty?" — so the number the limit counts is the number the footer
    shows, and a user is never told they have 21 steps while looking at 19."""
    return sum(1 for n in graph.nodes.values() if n.type in flow.WORK_TYPES)


def is_node_pro(node) -> bool:
    """Whether this node is a paid feature. True regardless of licence."""
    return (node.type in PRO_NODE_TYPES
            or flow.action_kind(node) in PRO_ACTION_KINDS)


def show_pro_badge(node) -> bool:
    """Whether the canvas should mark this node PRO right now.

    ⚠ The licence check is half the answer: a chip on a step this copy can run
    is noise.

    ⚠ And nothing is badged while `ENFORCED` is False. The chip's whole meaning
    is "pressing Play on this will stop"; with nothing enforced that does not
    happen, so the badge would be marking a step that works perfectly as one
    that does not.
    """
    return ENFORCED and is_node_pro(node) and not licensing.is_pro()


def runs_on_free(graph) -> bool:
    """Whether the free tier could run this flow — regardless of any licence.

    ⚠ Deliberately licence-blind, unlike `check`. It answers a question about
    the *flow* ("is this inside the free tier?"), which is what a build script
    counting free starters and a test pinning one need — and both would report
    every flow as free when run on a machine that happens to hold a key.

    `check` is the licence-aware caller and shares this definition, so the
    boundary is stated once.
    """
    return not pro_features_used(graph) and step_count(graph) <= FREE_MAX_STEPS


def check(graph) -> Tuple[bool, str, List[str]]:
    """(may it run, why not, which features are responsible).

    The message is written to be shown to a person as-is, and names the steps
    in their flow that are responsible.
    """
    # ⚠ The gate is switched off at the top, not wired away lower down. Every
    # call site, every dialog and every test below this line goes on working
    # exactly as written, so turning the tier on later is this one constant and
    # not an archaeology exercise. See `ENFORCED`.
    if not ENFORCED:
        return True, "", []

    if licensing.is_pro():
        return True, "", []

    if runs_on_free(graph):
        return True, "", []

    features = pro_features_used(graph)
    steps = step_count(graph)
    over = steps > FREE_MAX_STEPS

    reasons = []
    if features:
        reasons.append(
            f"This script uses {_join(features)}, which "
            f"{'are' if len(features) > 1 else 'is'} part of Macronaut Pro.")
    if over:
        reasons.append(
            f"It has {steps} steps; the free tier runs up to {FREE_MAX_STEPS}.")
    return False, " ".join(reasons), features


def _join(names: List[str]) -> str:
    """"A", "A and B", "A, B and C" — the shape a sentence needs."""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]
