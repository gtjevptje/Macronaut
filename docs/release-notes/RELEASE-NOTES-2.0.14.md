Typing finally goes through the input backend you chose, and wires stopped
crossing nodes.

*Written 3 September 2026 from the development record. This release shipped
before the project kept a notes file per release, and its page had been blank
since — see the note at the foot.*

**Typing ignored the input backend setting entirely.** Key presses and
releases went through whichever backend you selected; *typing* did not. It
always used the Windows path that carries a character rather than a key
position — which produces only a text message, never a keystroke. Notepad
reads text messages. A game reading raw input never sees them.

That is the whole explanation for the strangest bug this project has had: a
flow that pressed T, typed a line and pressed Enter worked perfectly in Notepad
and typed **nothing** in a game, while the T and the Enter arrived fine. You
pick a driver-level backend precisely because the target ignores ordinary
input, and typing was using ordinary input.

Both scancode backends now press real keys when typing. That needs the shift
state as well as the key position, because on a Belgian layout a digit is a
*shifted* key and the position alone types the punctuation printed on it.

**Per-key timing is not politeness.** A game reads input once a frame, so a key
that goes down and back up inside one frame can be missed entirely. The hold
and gap now straddle a 60 Hz frame, which puts the safe pace near 33
characters a second — slower than a batch, and the price of being seen at all.

**Wires stopped being drawn through nodes.** Measured across 300 realistic
layouts: 53 wires were painted straight through a node they were supposed to
clear. Three causes, each enough alone — the drawn curve overshot the route
that had been checked, backward wires (every loop has one) collapsed their
turn-out to the wrong side, and the clearance test sampled every 8 pixels
instead of being exact. Down to 5.

> The typing changes here were right about the mechanism and wrong about the
> rate: batching the text meant about five characters arrived per frame, and a
> game takes a bounded number per frame. 2.0.15 fixed that.

---

*This page was backfilled. The release itself is unchanged — the binary, its
SHA-256 and its publication date are exactly as they were in August 2026. Only
this description was missing, and eight releases were missing one.*
