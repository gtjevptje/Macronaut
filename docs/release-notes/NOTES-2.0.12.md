The download is **38% smaller** — 124.8 MB to 77.6 MB — and text detection had a
bug worth telling you about.

**Changed**

- **Macronaut is 47 MB lighter.** Nothing was removed from the app: every file
  dropped was one no part of Macronaut could reach. A video-decoding library
  (image matching never opens a video), an image codec for a format screen
  captures are never saved in, Qt's networking stack (updates and crash reports
  use Python's own), translations for a UI that only speaks English, and an
  on-screen keyboard. Windows text recognition moved to the modern, split
  Microsoft packages: same engine, same results, 10 MB instead of 38.
- The one thing you might notice: on a non-English Windows, the standard
  *Open* / *Save* file dialog now reads English like the rest of Macronaut,
  instead of your system language.

**Fixed**

- **A text-recognition setup that silently found nothing.** Windows OCR needs a
  support package that Microsoft's own OCR package doesn't ask for. Without it
  Macronaut reported text recognition as working, then read nothing from every
  screen, with no error to go on — and the setup instructions shipped with
  Macronaut recommended exactly that incomplete set. It is now checked when the
  engine starts, so it either works or tells you what is missing.
- **A second text engine that could never have run.** A backup recognition
  engine was included in every release since 1.0 without the data files it needs
  to start, so it always reported itself unavailable while taking 13 MB of your
  download to do it. Removed. Windows OCR is the one engine, which is what was
  actually running all along.

Faster to download, faster to update, and quicker through a virus scanner. No
change to how anything runs, and none to what crash reporting collects: never
your scripts, your keystrokes, what is on your screen, or your name.
