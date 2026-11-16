A crash fix, and the source code moves in with the downloads.

**Editing a script with the timeline open could crash it.** The timeline
strip under the canvas kept its own list of the steps, made when the script
was opened, and never looked again. Delete a step and the strip went on asking
about a step that was gone, which ended in an error; add one and it never
appeared in the strip at all until the script was reopened. A crash report
from somebody this happened to is how it was found. The strip now checks the
script every time it draws, so it follows deleting, adding and rewiring as you
do them.

**The source code and the downloads are one place now:**
<https://github.com/gtjevptje/Macronaut>. They were two repositories for no
better reason than the order things were set up in, which meant the page with
the download was not the page with the code, the tests or the issue tracker.
The **Source code** button in Settings → About opens the new address. The old
one stays up, with a pointer.

**Macronaut asks one question, once.** After you have opened it a few times
and run something, it asks whether you would give it a star on GitHub, with a
button for reporting a problem beside it. It asks at start-up, never during a
run, it does not block anything, and it never asks again whatever you answer.
Both links are also in Settings → About if you want them later.

**If you installed with the installer, this is your first update, and
Macronaut comes back when it is done.** Press Install and the app closes, the
installer runs over it with a small progress window, and the new version opens
again by itself. That last step was missing: rehearsing this update before
release, the new version installed correctly and then nothing reopened. The
single-file download updates the way it always has.

## Verified before release

The built program tests itself (image matching, text recognition and the
rest, against generated input with a known answer); a launch check confirms
it really opens a window; and the published 2.3.5 is downloaded and upgraded
in place to this build, with the upgraded copy passing its own self-test
afterwards. The installer's folder build passes the same self-test, and the
published 2.3.5 installer was installed and updated through this one, both
with the app already closed and with it still running.

## Notes

- Still unsigned, so SmartScreen will still warn on first run. Click
  **More info → Run anyway**. The source is public if you would rather check:
  <https://github.com/gtjevptje/Macronaut>
- Your scripts and settings are untouched by updating.
