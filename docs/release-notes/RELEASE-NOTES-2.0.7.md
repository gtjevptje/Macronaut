Play stopped refusing to run flows that were not empty.

*Written 3 September 2026 from the development record. This release shipped
before the project kept a notes file per release, and its page had been blank
since — see the note at the foot.*

**"Nothing to run" on a flow that plainly had work in it.** Play counted
*action* nodes to decide whether there was anything to do. Promoting a flow's
only Detect node into an If / Else — which 2.0.6 had just made possible — left
a working script looking empty, and Play refused it. There is now one
definition of "has work", covering action, If / Else, Loop, Set Var and Go to,
with Start, End, Label and Comment counted as scaffolding.

The same mistake had a quieter second half. The Basic face used the same "no
action nodes" test to decide a flow was empty enough to hold a plain
auto-clicker, so it would have **written an Auto-Click node into a real
branching flow**. Both now ask the same question.

**Start and End are bars, not cards.** They carry no settings, so they no
longer take a full node's height. Same width, same left edge, same port
position, so the column does not shift.

**Clicking empty canvas clears the selection.** A press that never travels is
"never mind"; a press that turns into a drag still pans.

**Dragging between two already-wired ports removes the wire** instead of
replacing it with an identical one, so the gesture that connects a pair also
disconnects it.

**The first node added to an empty flow attaches to Start**, and new nodes are
placed level with the previous node rather than above it.

---

*This page was backfilled. The release itself is unchanged — the binary, its
SHA-256 and its publication date are exactly as they were in August 2026. Only
this description was missing, and eight releases were missing one.*
