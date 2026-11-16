Two things that never worked, and a library that is no longer empty.

**Every feature is free.** Nothing is watermarked, nothing expires, and there
is no account.

**"Select region on screen…" works.** In Settings, under *Keep clicks inside a
region*, it did nothing at all — the overlay was collected by Python the
instant it opened, so the window died before you could drag in it. The same
control reached from a Detect step was always fine, which is why this went
unnoticed for so long.

**Auto-Click can be added to a flow.** The node type was real, saved flows
contained it, the engine ran it and its editor worked — there was simply no
button that created one. Two Settings cards, click region and pause-on-focus,
read their values from an Auto-Click node and were therefore inert for every
flow anybody could actually build.

**The Script Library arrives with six automations in it.** A new install used
to open on an empty canvas and an empty library. Now there is an auto-clicker,
one that stops after 100 clicks, one that clicks every 30 seconds to keep a
session awake, one that types a block of text, one that presses a key 50 times,
and an example that watches the screen for a word. None need setting up: open
one and press Play. Each carries a note on the canvas saying what it does, how
to stop it, and which node to change.

If you already have scripts saved, nothing was added to your library — that
would be five files you did not make appearing next to work you did. Press
**Add examples** in the Library when you want them. It never overwrites
anything you already have under the same name, and it is also how you get one
back after deleting it.

**Macronaut no longer opens on top of everything.** Always-on-top defaulted to
on, so a first launch parked the window over whatever you were doing before
asking. It helps while a flow runs and is purely in the way while one is being
built. Still in Settings → Appearance.

**The window title says the version it actually is.** It had read "2.0" since
2.0, which is unhelpful in a screenshot attached to a bug report.

---

Windows will show "Windows protected your PC" on first run — it does that for
any app without a signing certificate. Click More info, then Run anyway.

Something not working? gerbenvanpoucke0@gmail.com. It is one person, and you
will be talking to whoever wrote the part that broke.
