# Third-party notices — Macronaut

Macronaut is free software under the GPL, v3.0 or later (see
[LICENSE](LICENSE)). It is built on third-party components that remain licensed
under their own terms, which govern those components.

Versions below are the ones the build was verified against on 2026-07-31.

---

## ⚠ Copyleft components — status

**Macronaut is itself GPL-3.0-or-later as of 30 August 2026, so copyleft is
no longer a blocker — it is the licence.** Every component below is
GPL-3-compatible: LGPL-3 and MIT and BSD and PSF flow into GPL-3, and Apache-2.0
is one-way compatible with GPL-3 (though not with GPL-2, which is why the "or
later" matters).

This section used to record two components kept *out* to protect a closed-source
release. That reason is gone; the entries stay because the decisions did:

| Component | License | Status |
|---|---|---|
| ~~PyQt5~~ | GPL v3 or Riverbank Commercial | **Removed** — replaced by PySide6 on 2026-07-31 |
| ~~mouseinfo~~ | GPL v3+ | **Excluded** from the build (`macronaut.spec`) |

⚠ Neither exclusion is a licence requirement any more. PyQt5's GPL v3 would be
perfectly clean today. Keep PySide6 regardless — it is the Qt Company's own
binding and tracks Qt releases directly — and keep `mouseinfo` excluded, because
the reason there was always weight, not licence: it is a transitive dependency
of `pyautogui` that Macronaut never calls.

`mouseinfo` is a transitive dependency of `pyautogui` and is imported at
`import pyautogui`, so it would otherwise be bundled. Macronaut never calls it
(`pyautogui.mouseInfo()` is a developer utility), and `pyautogui`, `matcher.py`
and the image-matching paths were verified to work with it absent, so it is
listed in the spec's `excludes`.

---

## LGPL components

These permit use in a proprietary application provided the user can replace the
library with a modified version. Macronaut does not modify any of them.

| Component | Version | License | Upstream |
|---|---|---|---|
| PySide6 (and Qt 6) | 6.11.1 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://www.qt.io/ |
| PySide6_Essentials | 6.11.1 | (same) | https://www.qt.io/ |
| PySide6_Addons | 6.11.1 | (same) | https://www.qt.io/ |
| shiboken6 | 6.11.1 | (same) | https://www.qt.io/ |
| pynput | 1.8.1 | LGPL v3 | https://github.com/moses-palmer/pynput |

PySide6 is offered under a **choice** of licences.
**Macronaut elects LGPL-3.0-only** for PySide6, PySide6_Essentials,
PySide6_Addons and shiboken6.
The election is kept as it was — GPL-3.0-only would now be equally clean, and
changing a recorded election buys nothing.

The LGPL relinking right used to be honoured by an *offer of source*, because
Macronaut ships as a PyInstaller one-file executable and its own source was
closed. It is now honoured the direct way: Macronaut's complete source is
published, so anyone can rebuild the executable against a modified PySide6 with
`pyinstaller macronaut.spec`. The upstream links above are the unmodified
source of the components themselves.

The **Interception** kernel driver is *not* distributed with Macronaut. Users
install it themselves from https://github.com/oblitum/Interception, under its
own licence. Only the MIT-licensed `interception-python` binding is bundled.

---

## Permissive components

| Component | Version | License |
|---|---|---|
| pyautogui | 0.9.54 | BSD 3-Clause |
| PyMsgBox | 2.0.1 | BSD 3-Clause in the file, GPLv3+ in the metadata — see below |
| PyScreeze | 1.0.1 | MIT |
| PyGetWindow | 0.0.9 | BSD 3-Clause |
| PyRect | 0.2.0 | BSD 3-Clause |
| pytweening | 1.2.0 | MIT |
| pyperclip | 1.11.0 | BSD 3-Clause |
| opencv-python (OpenCV) | 4.13.0.92 | Apache 2.0 |
| numpy | 2.4.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib |
| Pillow | 12.2.0 | MIT-CMU (HPND) |
| pywin32 | 311 | Python Software Foundation License |
| winrt-runtime + winrt-Windows.* | 3.2.1 | MIT |
| interception-python | 1.13.6 | MIT |
| six | 1.17.0 | MIT |
| typing-extensions | 4.15.0 | PSF-2.0 |

⚠ **`PyMsgBox` and `numpy` were missing from this table until 12 September
2026, and both ship.** `numpy` is the larger omission by far: `macronaut.spec`
says in as many words that it must not be excluded, because `cv2` imports it,
and its OpenBLAS DLL is about 6.5 MB of the download. `PyMsgBox` arrives at
`import pyautogui` — verified, not assumed — and nothing excludes it.

⚠ **`PyMsgBox` carries the same `GPLv3+` classifier as the excluded
`mouseinfo`**, while its own licence text is BSD 3-Clause; Al Sweigart's
packages are inconsistent about this. It is kept rather than excluded, and the
reason is the one that changed on 30 August 2026: Macronaut is GPL-3.0-or-later
itself, so GPLv3+ is simply compatible. `mouseinfo` stays excluded for weight,
not licence — as the copyleft section above already says.

Windows OCR (`Windows.Media.Ocr`) is part of Windows and is used through the
system, not redistributed.

`rapidocr-onnxruntime` (and with it onnxruntime, shapely and pyclipper) was
listed here until 2.0.12. It is no longer a dependency and no longer ships.

---

## Build tooling (not distributed)

| Component | Version | License |
|---|---|---|
| PyInstaller | 6.21.0 | GPL v2-or-later **with the bootloader exception** |

PyInstaller's exception explicitly permits building and distributing
proprietary applications; only the bootloader ships, and the exception covers
it. This one is fine as-is.

---

## Keeping this file honest

Regenerate the licence facts rather than trusting this table after a
dependency bump:

```bash
python -c "import importlib.metadata as md; [print(n, md.version(n), md.metadata(n).get('License') or md.metadata(n).get('License-Expression'), [c for c in (md.metadata(n).get_all('Classifier') or []) if 'Licen' in c]) for n in ['PySide6','shiboken6','pynput','pyautogui','opencv-python','Pillow','pywin32','winrt-runtime','winrt-Windows.Media.Ocr','interception-python','PyScreeze','PyGetWindow','pytweening','pyperclip','six','numpy','PyMsgBox','PyRect','typing-extensions']]"
```

*Compiled from installed package metadata, not from memory. It is a
developer's inventory, not legal advice — have it reviewed before charging
money for a build.*
