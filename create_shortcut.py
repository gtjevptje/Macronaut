"""
Run this once to install the desktop shortcut and generate the app icon.
  python create_shortcut.py
"""
import sys
import os
import math
import struct
import zlib
from pathlib import Path

# Must match the value the running app sets via
# SetCurrentProcessExplicitAppUserModelID (main.py) so the taskbar groups them.
APP_USER_MODEL_ID = "Macronaut.App.1"


# ── Icon generation (pure stdlib + Pillow) ────────────────────────────────────

def _make_frame(size: int) -> "Image":
    from PIL import Image, ImageDraw, ImageFilter

    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s    = size

    # ── Background: dark rounded square ──────────────────────────────
    r = s // 5
    draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=r,
                           fill=(30, 30, 46, 255))

    # Subtle inner glow ring
    draw.rounded_rectangle([2, 2, s - 3, s - 3], radius=r - 2,
                           outline=(137, 180, 250, 40), width=max(1, s // 64))

    # ── Mouse body ────────────────────────────────────────────────────
    mx  = s * 0.22
    my  = s * 0.13
    mw  = s * 0.56
    mh  = s * 0.68
    br  = mw / 2.8

    # Shadow
    sh_off = max(1, s // 32)
    draw.rounded_rectangle(
        [mx + sh_off, my + sh_off, mx + mw + sh_off, my + mh + sh_off],
        radius=br, fill=(0, 0, 0, 80))

    # Body fill — light blue-white
    body_color = (205, 214, 244, 255)
    draw.rounded_rectangle([mx, my, mx + mw, my + mh],
                           radius=br, fill=body_color)

    # Left button highlight
    draw.rounded_rectangle([mx + 2, my + 2, mx + mw / 2 - 1, my + mh * 0.42],
                           radius=br - 2, fill=(220, 228, 250, 255))

    # Button divider line
    mid_x = mx + mw / 2
    div_color = (30, 30, 46, 180)
    lw = max(1, s // 48)
    draw.line([(mid_x, my + 2), (mid_x, my + mh * 0.43)],
              fill=div_color, width=lw)

    # Scroll wheel
    ww = mw * 0.22
    wh = mh * 0.20
    wx = mid_x - ww / 2
    wy = my + mh * 0.11
    draw.rounded_rectangle([wx, wy, wx + ww, wy + wh],
                           radius=min(ww, wh) / 2.5,
                           fill=(137, 180, 250, 255))

    # Bottom body outline
    draw.rounded_rectangle([mx, my, mx + mw, my + mh],
                           radius=br,
                           outline=(137, 180, 250, 200),
                           width=max(1, s // 48))

    # ── Click spark ───────────────────────────────────────────────────
    # A small starburst in the bottom-right to suggest a click action
    cx = mx + mw + s * 0.04
    cy = my + mh - s * 0.04
    n_rays = 8
    inner  = s * 0.055
    outer  = s * 0.115
    spark_pts = []
    for i in range(n_rays * 2):
        angle = math.radians(i * 180 / n_rays - 90)
        r2    = outer if i % 2 == 0 else inner
        spark_pts.append((cx + r2 * math.cos(angle),
                          cy + r2 * math.sin(angle)))
    draw.polygon(spark_pts, fill=(249, 226, 175, 230))   # #f9e2af yellow

    # Inner bright core
    core = s * 0.04
    draw.ellipse([cx - core, cy - core, cx + core, cy + core],
                 fill=(255, 245, 210, 255))

    # ── Soft vignette ─────────────────────────────────────────────────
    if size >= 64:
        vign = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        vd   = ImageDraw.Draw(vign)
        for i in range(min(8, s // 16)):
            alpha = int(30 * (1 - i / 8))
            vd.rounded_rectangle([i, i, s - 1 - i, s - 1 - i],
                                 radius=r - i,
                                 outline=(0, 0, 0, alpha), width=1)
        img = Image.alpha_composite(img, vign)

    return img


def build_ico(dest: Path):
    try:
        from PIL import Image
    except ImportError:
        print("Pillow not found — skipping icon generation.")
        return False

    sizes   = [16, 32, 48, 64, 128, 256]
    frames  = [_make_frame(s) for s in sizes]
    frames[0].save(
        str(dest), format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=frames[1:],
    )
    print(f"  Icon written -> {dest}")
    return True


# ── Shortcut creation (win32com) ──────────────────────────────────────────────

def create_shortcut(icon_path: Path):
    try:
        import win32com.client
    except ImportError:
        print("pywin32 not installed — run:  pip install pywin32")
        return

    # Use pythonw.exe so no console window appears
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    if not pythonw.exists():
        pythonw = Path(sys.executable)   # fallback to python.exe

    script  = Path(__file__).parent / "main.py"
    work    = script.parent

    wsh = win32com.client.Dispatch("WScript.Shell")
    # Resolve the real Desktop folder (handles OneDrive redirection, etc.)
    desktop  = Path(wsh.SpecialFolders("Desktop"))
    lnk      = desktop / "Macronaut.lnk"

    shortcut  = wsh.CreateShortCut(str(lnk))
    shortcut.TargetPath      = str(pythonw)
    shortcut.Arguments       = f'"{script}"'
    shortcut.WorkingDirectory = str(work)
    shortcut.Description     = "Macronaut — automate anything, no code"
    if icon_path.exists():
        shortcut.IconLocation = str(icon_path)
    shortcut.save()

    _set_shortcut_appid(lnk, APP_USER_MODEL_ID)
    print(f"  Shortcut created -> {lnk}")


def _set_shortcut_appid(lnk_path, app_id):
    """Stamp the shortcut with the same AppUserModelID the running app sets
    (SetCurrentProcessExplicitAppUserModelID), so launching it groups under the
    pinned taskbar icon instead of spawning a second taskbar button (#1)."""
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        GPS_READWRITE = 0x00000002
        store = propsys.SHGetPropertyStoreFromParsingName(
            str(lnk_path), None, GPS_READWRITE, propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID,
                       propsys.PROPVARIANTType(app_id, pythoncom.VT_LPWSTR))
        store.Commit()
        print(f"  AppUserModelID stamped -> {app_id}")
    except Exception as e:
        print(f"  (could not set AppUserModelID: {e})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Macronaut — shortcut installer\n")

    assets = Path(__file__).parent / "assets"
    assets.mkdir(exist_ok=True)
    icon   = assets / "macronaut.ico"

    # The Macronaut helmet logo ships in assets/macronaut.ico. Use it as-is for
    # the desktop / taskbar icon — only generate a fallback if it's missing, so
    # we never overwrite the real logo.
    if icon.exists():
        print(f"Using bundled logo icon -> {icon}")
    else:
        print("Logo icon missing — generating a placeholder…")
        build_ico(icon)

    print("Creating desktop shortcut…")
    create_shortcut(icon)

    print("\nDone! Look for 'Macronaut' on your Desktop.")


if __name__ == "__main__":
    main()
