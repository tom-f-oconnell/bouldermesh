#!/usr/bin/env python3
"""The iPhone app's icon (Assets.xcassets/AppIcon.appiconset/AppIcon.png, 1024 px, kept in git) from the site's own
favicon, frontend/favicon.svg: its facets drawn on an opaque background, as the App Store wants no transparency.

    python3 make_icon.py        (needs Pillow; only when the favicon changes)
"""
import re
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SVG = HERE.parents[2] / "frontend" / "favicon.svg"
OUT = HERE / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon.png"
SIZE, SUPER, MARGIN, BACKGROUND = 1024, 4, 0.16, "#1f2b30"

svg = SVG.read_text()
x0, y0, w, h = (float(v) for v in re.search(r'viewBox="([^"]+)"', svg)[1].split())
scale = SIZE * SUPER * (1 - 2 * MARGIN) / max(w, h)
off = SIZE * SUPER * MARGIN
img = Image.new("RGB", (SIZE * SUPER, SIZE * SUPER), BACKGROUND)
draw = ImageDraw.Draw(img)
for points, fill in re.findall(r'<polygon points="([^"]+)" fill="([^"]+)"', svg):
    xy = [(off + (float(a) - x0) * scale, off + (float(b) - y0) * scale) for a, b in (p.split(",") for p in points.split())]
    draw.polygon(xy, fill=fill, outline=fill, width=SUPER * 2)
img.resize((SIZE, SIZE), Image.LANCZOS).save(OUT)
print(f"wrote {OUT}")
