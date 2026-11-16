Shift is no longer injected while typing.

*Written 3 September 2026 from the development record. This release shipped
before the project kept a notes file per release, and its page had been blank
since — see the note at the foot.*

The third report in this chain: "only the first two characters arrive, at every
speed."

Driving the exact 156-character step through the real engine, under a hook that
swallows every keystroke, produced **every character, exactly, in order, at
200, 80 and 10 characters a second.** A fixed count that ignores speed is not a
timing symptom, and the app was not dropping anything. So the thing to change
was not what Macronaut sent but what it was making the receiver do.

**Typing released Shift after every character.** Because the paced path calls
the typist once per character, a 156-character line sprayed **156 real Shift
releases** into the target, interleaved with the text — 540 events where there
should have been 312. Shift is now released only when it was actually pressed.

**And Shift injection is off by default.** It was the one thing both broken
releases had in common, and typing without it is the only version that ever
filled that chat box. The trade is deliberate: a receiver that reads the
character gets the capital for free, and one that reads the keyboard state
instead now costs a setting rather than costing everyone their text. A whole
line in the wrong case beats two characters in the right one.

> This was still not the root cause. 2.0.17 found it: on this backend, capitals
> and digits were travelling by a completely different mechanism from ordinary
> letters, and the game could only see one of the two.

**A note on method, which cost three releases.** Each of these was diagnosed
from the symptom rather than from a measurement of the running app. The crash
breadcrumbs — version, backend, run start and stop — were there the whole time
and were not read until the third round.

---

*This page was backfilled. The release itself is unchanged — the binary, its
SHA-256 and its publication date are exactly as they were in August 2026. Only
this description was missing, and eight releases were missing one.*
