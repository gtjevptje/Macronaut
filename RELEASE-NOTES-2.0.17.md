The actual cause of four releases' worth of missing text.

*Written 3 September 2026 from the development record. This release shipped
before the project kept a notes file per release, and its page had been blank
since — see the note at the foot.*

**Half of every sentence was travelling by a mechanism the game could not
see, and which half depended on whether the character needed a modifier.**

The default input library decides per character. If the character needs no
modifier on your keyboard layout, it sends a **real key event**. If it does, it
gives up on the key and sends a **bare text message** — without pressing the
modifier at all. Measured on a Belgian layout: `a e z space !` went out as real
keys; `A E Z 1 .` went out as text messages. The target game reads real keys
and ignores text messages entirely.

So "everything types except capitals" was never a bug about *case*. It was
"characters that need a modifier travel by a mechanism this target cannot see",
and capitals are simply the most visible members of that set.

**Every fix from 2.0.14 onward had moved *more* of the text onto the
unreadable path.** Batched it — "missing most". Paced it — "first two
characters". Stripped the modifiers off it — "types nothing", by then 100%
text messages. The symptom got worse in exact proportion to how much of the
sentence went by the wrong route, and that steady worsening was the signal. It
was read as three unrelated bugs.

Typing on this backend now presses real keys throughout, the same path the
driver-level backends already used. Verified through the real engine under a
swallowing hook: **51 real key events, zero text messages**, and the string
reconstructs exactly.

**The rule that would have saved three releases:** when input *partly* arrives,
ask which mechanism carried each part before asking about timing, rate or
ordering. The split is never random.

---

*This page was backfilled. The release itself is unchanged — the binary, its
SHA-256 and its publication date are exactly as they were in August 2026. Only
this description was missing, and eight releases were missing one.*
