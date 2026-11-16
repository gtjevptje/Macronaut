Crash reporting fixes — 2.0.9 could report crashes that never happened, and
stay silent about ones that did.

**Fixed**

- A crash whose process ID had been reused by Windows was never reported at
  all, and its files were never cleaned up. Reboot after a crash and the number
  that identified the dead run can belong to something else entirely, at which
  point the report was deferred on every launch, forever. From the outside that
  looked exactly like the app never crashing.
- If crash capture failed to start — a read-only folder, no free file handles —
  it left a marker behind that made the *next* launch report a completely
  healthy session as a crash, in the category reserved for the most serious
  failures. Every launch, until the underlying problem cleared.
- A truncated breadcrumb trail now says it was truncated instead of quietly
  looking complete.
- Editing a step on the canvas could leave the app drawing connections it had
  already thrown away. It kept working, so nothing was visibly wrong — the
  2.0.9 crash reporter is what found it, which is the first bug it has caught
  in the wild.
- Sending a crash report now updates the count in Settings, rather than leaving
  it showing the number from when the window opened.

**Housekeeping**

Removed the old click engine, replaced by the Auto-Click node in 2.0, along
with a handful of other things nothing referenced any more.

No changes to how the app is used. If crash reporting is on, nothing about what
is collected has changed: never your scripts, your keystrokes, what is on your
screen, or your name.
