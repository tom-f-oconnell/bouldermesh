#!/usr/bin/env python3
"""Both apps' icons from the site's own favicon (frontend/favicon.svg), its facets on a dark background; written into the
apps, and kept in git, so run this only when the favicon changes:

    python3 app/make_icons.py        (Pillow, for the iPhone's)

- iPhone: ios/xcode/Assets.xcassets/AppIcon.appiconset/AppIcon.png, 1024 px, opaque (the App Store wants no
  transparency); iOS rounds its corners.
- Android: an adaptive icon (minSdk 26): its foreground the facets as a vector drawable, within the 66 of 108 units every
  launcher's mask leaves whole; its background the same colour (res/mipmap-anydpi-v26, res/drawable, res/values).
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SVG = HERE.parent / "frontend" / "favicon.svg"
BACKGROUND = "#1f2b30"

svg = SVG.read_text()
x0, y0, w, h = (float(v) for v in re.search(r'viewBox="([^"]+)"', svg)[1].split())
facets = [([tuple(float(c) for c in p.split(",")) for p in points.split()], fill)
          for points, fill in re.findall(r'<polygon points="([^"]+)" fill="([^"]+)"', svg)]


def ios(size: int = 1024, supersample: int = 4, margin: float = 0.16) -> Path:
    from PIL import Image, ImageDraw
    big = size * supersample
    scale, off = big * (1 - 2 * margin) / max(w, h), big * margin
    img = Image.new("RGB", (big, big), BACKGROUND)
    draw = ImageDraw.Draw(img)
    for pts, fill in facets:
        draw.polygon([(off + (x - x0) * scale, off + (y - y0) * scale) for x, y in pts], fill=fill, outline=fill, width=supersample * 2)
    out = HERE / "ios" / "xcode" / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon.png"
    img.resize((size, size), Image.LANCZOS).save(out)
    return out


def android(content: float = 60.0) -> list[Path]:
    res = HERE / "android" / "src" / "main" / "res"
    scale = content / max(w, h)
    off_x, off_y = (108 - w * scale) / 2, (108 - h * scale) / 2
    paths = "\n".join(
        f'    <path android:fillColor="{fill}" android:strokeColor="{fill}" android:strokeWidth="0.15" android:strokeLineJoin="round" '
        f'android:pathData="M{" L".join(f"{off_x + (x - x0) * scale:.2f},{off_y + (y - y0) * scale:.2f}" for x, y in pts)} Z"/>'
        for pts, fill in facets)
    files = {
        res / "drawable" / "ic_launcher_foreground.xml":
            '<?xml version="1.0" encoding="utf-8"?>\n<!-- made by app/make_icons.py from frontend/favicon.svg -->\n'
            '<vector xmlns:android="http://schemas.android.com/apk/res/android" android:width="108dp" android:height="108dp"\n'
            f'    android:viewportWidth="108" android:viewportHeight="108">\n{paths}\n</vector>\n',
        res / "values" / "ic_launcher_background.xml":
            f'<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <color name="ic_launcher_background">{BACKGROUND}</color>\n</resources>\n',
    }
    adaptive = ('<?xml version="1.0" encoding="utf-8"?>\n<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
                '    <background android:drawable="@color/ic_launcher_background"/>\n'
                '    <foreground android:drawable="@drawable/ic_launcher_foreground"/>\n'
                '    <monochrome android:drawable="@drawable/ic_launcher_foreground"/>\n</adaptive-icon>\n')
    for name in ("ic_launcher", "ic_launcher_round"):
        files[res / "mipmap-anydpi-v26" / f"{name}.xml"] = adaptive
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return list(files)


if __name__ == "__main__":
    for p in [ios(), *android()]:
        print(f"wrote {p.relative_to(HERE)}")
