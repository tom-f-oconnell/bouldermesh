"""Tests of masking text in photos for texturing (textmask.py): merging it into the people masks with a stand-in for the
detector, and, if the detector is in the models cache (or MODELS=1 lets it download), finding drawn captions and not
plain rock. Offline unless MODELS=1."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
try:
    import cv2
    import numpy as np
except ImportError:
    print("numpy or OpenCV not installed: skipping")
    sys.exit(0)
import textmask  # noqa: E402

# ---- merged into the people masks, with a stand-in detector
real_boxes = textmask.boxes
textmask.boxes = lambda im, models=None: [(10, 20, 50, 30)] if im[0, 0, 2] > 128 else []
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td) / "images"
    d.mkdir()
    red, grey = np.zeros((60, 80, 3), np.uint8), np.full((60, 80, 3), 90, np.uint8)
    red[:] = (0, 0, 255)
    for name, im in (("a.jpg", red), ("b.jpg", grey), ("c.jpg", red)):
        cv2.imwrite(str(d / name), im)
    people = np.full((60, 80), 255, np.uint8)
    people[40:, :10] = 0
    cv2.imwrite(str(d / "a.mask.png"), people)
    lines = []
    args = textmask.for_texturing(d, [], lines.append)
    assert args == ["--ignore-mask-label", "0"], args
    a = cv2.imread(str(d / "a.mask.png"), cv2.IMREAD_GRAYSCALE)
    assert (a[20:30, 10:50] == 0).all() and (a[40:, :10] == 0).all(), "text added, people kept"
    assert (a[:20] == 255).all() and (a[30:40, 10:] == 255).all(), "nothing else masked"
    c = cv2.imread(str(d / "c.mask.png"), cv2.IMREAD_GRAYSCALE)
    assert c is not None and (c[20:30, 10:50] == 0).all() and c.mean() > 200, "a mask made where there was none"
    assert not (d / "b.mask.png").exists(), "no mask written for a photo without text"
    shares = json.loads((d.parent / textmask.SHARES).read_text())
    assert set(shares) == {"a.jpg", "b.jpg", "c.jpg"} and shares["b.jpg"] == 0 and shares["a.jpg"] > 0, shares
    assert any("found in 2 of 3 photos" in s for s in lines), lines
    assert textmask.for_texturing(d, ["--ignore-mask-label", "0"], print) == ["--ignore-mask-label", "0"]

    def broken(im, models=None):
        raise textmask.Unavailable("offline")
    textmask.boxes = broken
    lines = []
    assert textmask.for_texturing(d, [], lines.append) == [], "nothing to mask with: textured as before"
    assert any("not masked" in s for s in lines), lines
    os.environ["TEXT_MASKS"] = "0"
    textmask.boxes = lambda *a, **k: (_ for _ in ()).throw(AssertionError("looked for text with TEXT_MASKS=0"))
    assert textmask.for_texturing(d, [], print) == []
    del os.environ["TEXT_MASKS"]
textmask.boxes = real_boxes
print("merging OK")

# ---- the real detector: a drawn caption found, rock-like noise left alone
models = textmask.models_dir()
if not textmask.available() or not ((models / "text" / textmask.MODEL_FILE).exists() or os.environ.get("MODELS") == "1"):
    print("text detector not in the models cache (MODELS=1 downloads it): skipping the detector")
    sys.exit(0)
rng = np.random.default_rng(0)
rock = cv2.GaussianBlur(rng.integers(60, 200, (540, 960, 3)).astype(np.uint8), (0, 0), 3)
captioned = rock.copy()
cv2.putText(captioned, "LA MARIE-ROSE (6A/V2+)", (120, 470), cv2.FONT_HERSHEY_DUPLEX, 1.6, (255, 255, 255), 3)
found = textmask.boxes(captioned, models)
assert found, "the caption wasn't found"
assert any(x0 <= 150 and x1 >= 600 and y0 <= 445 and y1 >= 470 for x0, y0, x1, y1 in found), found
assert not textmask.boxes(rock, models), "text found in plain rock"
print("detector OK")
