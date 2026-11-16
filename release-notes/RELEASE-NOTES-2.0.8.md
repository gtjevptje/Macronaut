The crashes while a flow was running.

*Written 3 September 2026 from the development record. This release shipped
before the project kept a notes file per release, and its page had been blank
since — see the note at the foot.*

The report was "a ton of crashes, mostly when running a script". It was three
separate bugs feeding one symptom, and measuring came first.

**The run log could bury the program.** A tight loop drives the engine at
roughly 692,000 log events a second. The window could absorb about 196 a
second at 2,000 rows, and 47 a second at 6,000 — each one adding a row to a
list and scrolling it. Events therefore queued up four orders of magnitude
faster than they drained: memory climbed, the window froze, and the process
died. The engine now batches log events and reports how many it dropped rather
than losing them silently, and the window adds them in one go and only scrolls
when you were already at the bottom. Measured again on the same forever-loop:
400,005 deliveries became 7, and hours of frozen window became 0.9 seconds.

**Stop could kill the app outright.** Stopping waited three seconds for the
worker and then dropped it regardless — and discarding a thread that is still
running is a fatal error at the Qt level, not something Python can catch. The
wait times out routinely, because reading the screen and matching an image are
not interruptible. So Stop crashed the app on exactly the flows that watch the
screen. The worker is now held until it genuinely finishes.

**The canvas re-highlighted the running node on every step**, and re-drawing
the selection re-draws every wire with it. It is a no-op now when nothing has
moved.

**The If / Else image condition can capture from the screen.** It was the only
image field in the app with nothing but a Browse button — and that same widget
is the If / Else, the Loop-while and the Loop-until editors, so one missing
button meant three dialogs you could not use without a file already on disk.
It now has Capture, a preview and Test match, like the Detect step.

**"Search area" is one control** — a two-part switch that shows the chosen
region's size inline, instead of two similar-looking buttons and a status
label repeated in three dialogs.

---

*This page was backfilled. The release itself is unchanged — the binary, its
SHA-256 and its publication date are exactly as they were in August 2026. Only
this description was missing, and eight releases were missing one.*
