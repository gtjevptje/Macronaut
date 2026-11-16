The text 2.0.14 was losing.

*Written 3 September 2026 from the development record. This release shipped
before the project kept a notes file per release, and its page had been blank
since — see the note at the foot.*

2.0.14 fixed typing into games by sending real keys, and broke it by sending
them all at once. The report came back: "missing most of the text."

The measurement is the useful part. Using a hook that **swallows** every
keystroke, so nothing reaches any window and no receiver is involved:

| | characters | span | gap between | frames |
|---|---|---|---|---|
| 2.0.14, batched | 39 | 134 ms | 3.1 ms | 8 — about **5 per frame** |
| 2.0.15, paced | 39 | 1423 ms | 32.2 ms | 85 — about 2 frames each |

**Injection was exact in both cases.** Windows accepted every keystroke either
way. But a game reads its input once a frame and takes a bounded amount per
pass, so five per frame arrives mostly missing. That is why this looked
flawless in Notepad and lost text in a chat box, and why no test bench could
settle it.

Typing is paced now: one keystroke per call, hold and gap straddling a 60 Hz
frame, about 33 characters a second. There is deliberately **one** mode, so
that "text is missing" can never be answered by turning the speed up. Stop is
checked between characters, so it cuts within one.

**The rate that matters is the one the target reads, never the one Windows
accepts.**

---

*This page was backfilled. The release itself is unchanged — the binary, its
SHA-256 and its publication date are exactly as they were in August 2026. Only
this description was missing, and eight releases were missing one.*
