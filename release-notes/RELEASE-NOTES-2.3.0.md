# Macronaut 2.3.0 — Basic is back

**Macronaut opens as a plain auto-clicker again.**

Set the interval, pick a mouse button, choose how many times, press **Start**.
No nodes, no wires, no diagram. If that is all you ever wanted from this, you
are finished on the first screen.

The node canvas has not gone anywhere — it is behind the **Advanced ›** link at
the bottom of that screen, and there is a **‹ Basic** link to come back. The two
are views of the same automation, so a clicker you set up in Basic is a real
flow on the canvas if you ever go looking.

**It is free, permanently.** Not a sample of the free tier — the reason the free
tier exists.

---

## Why it came back

2.1.0 removed it. That was a mistake, and the honest version of why is short:
one window is simpler to build and maintain, and it was argued for on those
grounds. What it cost was the person who searched for "auto clicker", downloaded
a 78 MB file, opened it, and was shown a blank canvas with a palette of nodes.

---

## It remembers how you left it

- **Close it in Basic and it opens in Basic.** Close it in Advanced and it opens
  in Advanced.
- **Each face keeps its own size and position.** Basic parked in a corner beside
  the window it is clicking, Advanced as big as your monitor — switching between
  them no longer moves or resizes the other one.
- The first time you open Basic it sizes itself to its own content. After that,
  whatever size you drag it to is the size it comes back at.

## It matches the rest of the app now

The Basic face used to carry its own fixed colour scheme from 2.0, so it looked
like a different program the moment you chose Graphite or Daylight in Settings.
It follows the theme you picked, like everything else.

Two smaller things while it was open:

- **Start is green and Stop is red**, matching the buttons on the canvas.
- **The Repeat options are round radio buttons** instead of squares. They are
  mutually exclusive — "repeat 10 times" *or* "repeat until stopped" — and
  drawing them as checkboxes said otherwise.

## Also fixed

- **Fit no longer magnifies.** On a new install, with a single Start node on the
  canvas, pressing Fit zoomed to 4× — past the limit the mouse wheel itself
  enforces, as the first thing anybody saw after downloading. Fit shows you
  everything; it does not make things bigger than life size.

---

## Everything is free

Every feature — including the ones that watch the screen and branch on what
they find — is available to everyone, with nothing to enter and no account.

Feedback, bug reports and questions: **gerbenvanpoucke0@gmail.com**. It is one
person, not a ticket queue.

## Installing

Download `Macronaut.exe` and run it. Windows will say *"Windows protected your
PC"* on first run, because the file is not code-signed yet — click **More info**,
then **Run anyway**. Existing installs update themselves on restart.
