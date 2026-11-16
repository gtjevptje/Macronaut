Stop now stops, and typed text stopped losing characters.

*Written 3 September 2026 from the development record. This release shipped
before the project kept a notes file per release, and its page had been blank
since — see the note at the foot.*

**Stop could be silently thrown away.** Pressing Stop while a run was still
starting up landed before the worker had begun, and starting cleared it. The
button then read "Play" while the flow ran to completion, and pressing Stop
again reached nobody. A stop request now sticks: the worker checks for one
before it starts.

**Stop could not reach a worker that had already been retired.** After the
wait for a slow step timed out, the worker was still running but no longer
reachable — and the window believed nothing was playing, so Play would have
started a *second* one beside it. Stop now signals retired workers too, and
"is something playing" counts them.

**Typed text lost characters, and Enter was the wrong key.** Text now goes out
in one batch rather than one call per character. And a line break is sent as a
real Return key: the character U+000A is, at the Windows keyboard layer,
literally Ctrl+Enter — which most applications read as "send", so a newline in
the middle of a message would submit it and close the box.

**"Text to type" is a proper text box.** It was a single-line field: it stored
the whole string but showed only the last thirty characters or so, and could
not hold a line break at all.

**The typing speed box takes 0 for "as fast as possible"**, which is the
default and also the most reliable setting. A slower rate paces the keys for
applications that genuinely need it.

> Worth saying plainly: the batching in this release is what broke typing in
> games, and 2.0.15 undid it. The reasoning behind it had been measured
> against the wrong thing — see the 2.0.15 and 2.0.17 notes.

---

*This page was backfilled. The release itself is unchanged — the binary, its
SHA-256 and its publication date are exactly as they were in August 2026. Only
this description was missing, and eight releases were missing one.*
