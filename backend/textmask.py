"""Find text in photos (thumbnails' titles, channels' logos, videos' captions), so that it isn't painted onto a mesh.

A web photo or video frame often has text laid over it, and TextureMesh would copy it onto the rock like any other
pixels. Its masks (the people masks, occlusion.py) take it out of the photos it would be copied from, for texturing only:
the cameras, depth maps and mesh are made as before, so which photos are placed doesn't change. Faces a masked photo
would have coloured are coloured from the next best photo that sees them, as around people.

The detector is PaddleOCR's PP-OCRv3 English text detector (a DB net, 2.4 MB, Apache 2.0), as RapidOCR converted it to
ONNX: downloaded and checked the first time, run with onnxruntime on the CPU. Only its probability map is used, as boxes
grown around each piece of text; nothing is read.
"""
from __future__ import annotations

import hashlib
import os
import threading
import urllib.request
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
MODEL_URL = "https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv4/en_PP-OCRv3_det_infer.onnx"
MODEL_SHA256 = "f139598bc2af4e4b6fe98dec11574e30edfdd91fc94ac1425c18ace3bd5a866b"
MODEL_FILE = "en_PP-OCRv3_det.onnx"
SIDE = 960          # the longer side the detector sees (a multiple of 32), as PaddleOCR's own default
PIXEL = 0.3         # a pixel's probability of text to count as text, as PaddleOCR's
BOX = 0.6           # the least mean probability over a piece for it to be text (PaddleOCR's 0.5, a little stricter)
LEAST = 8           # the least height, in the detector's pixels, of a piece that is text and not a speck in the rock
GROW = 0.6          # each piece grown by this share of its height on every side (PaddleOCR's unclip, and a margin)
SHARES = "text.json"    # the share of each photo that is text, beside the masks

_lock = threading.Lock()
_session = None


class Unavailable(RuntimeError):
    """The detector can't be had: onnxruntime missing, or the model can't be downloaded."""


def available() -> bool:
    import importlib.util
    return all(importlib.util.find_spec(m) is not None for m in ("onnxruntime", "numpy", "cv2"))


def model_path(models: Path) -> Path:
    """The detector, downloaded (and checked) the first time."""
    f = models / "text" / MODEL_FILE
    if not f.exists():
        try:
            with urllib.request.urlopen(urllib.request.Request(MODEL_URL, headers={"User-Agent": "BoulderMesh"}),
                                        timeout=60) as r:
                data = r.read()
        except OSError as e:
            raise Unavailable(f"the text detector couldn't be downloaded ({e})") from e
        if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
            raise Unavailable(f"{MODEL_URL} isn't the model expected")
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".partial")
        tmp.write_bytes(data)
        tmp.replace(f)
    return f


def models_dir() -> Path:
    """Where downloaded models are kept: DATA_DIR's cache/models, as the machines keep theirs."""
    return Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parent.parent / "data") / "cache" / "models"


def _load(models: Path | None = None):
    global _session
    with _lock:
        if _session is None:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            cores = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
            opts.intra_op_num_threads, opts.inter_op_num_threads = int(os.environ.get("TEXT_THREADS") or max(1, min(cores, 4))), 1
            _session = ort.InferenceSession(str(model_path(models or models_dir())), opts, providers=["CPUExecutionProvider"])
        return _session


def boxes(image, models: Path | None = None) -> list[tuple[int, int, int, int]]:
    """The text in an image (BGR, as cv2 reads it), as boxes (x0, y0, x1, y1) in its pixels, grown by GROW."""
    import cv2
    import numpy as np
    sess = _load(models)
    h, w = image.shape[:2]
    s = SIDE / max(h, w)
    nh, nw = max(32, round(h * s / 32) * 32), max(32, round(w * s / 32) * 32)
    x = cv2.resize(image, (nw, nh)).astype(np.float32) / 255
    x = ((x - (0.485, 0.456, 0.406)) / (0.229, 0.224, 0.225)).transpose(2, 0, 1)[None].astype(np.float32)
    prob = sess.run(None, {sess.get_inputs()[0].name: x})[0][0, 0]
    n, labels, stats, _ = cv2.connectedComponentsWithStats((prob > PIXEL).astype(np.uint8), connectivity=8)
    out = []
    fx, fy = w / nw, h / nh
    for i in range(1, n):
        x0, y0, bw, bh, _area = stats[i]
        if min(bw, bh) < LEAST or prob[labels == i].mean() < BOX:
            continue
        g = GROW * min(bw, bh)
        out.append((max(0, int((x0 - g) * fx)), max(0, int((y0 - g) * fy)),
                    min(w, int((x0 + bw + g) * fx) + 1), min(h, int((y0 + bh + g) * fy) + 1)))
    return out


def text_mask(path: Path, models: Path | None = None):
    """Where the text is in a photo: a bool array the size of the image (None if it can't be read)."""
    import cv2
    import numpy as np
    im = cv2.imread(str(path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if im is None:
        return None
    m = np.zeros(im.shape[:2], bool)
    for x0, y0, x1, y1 in boxes(im, models):
        m[y0:y1, x0:x1] = True
    return m


def add_to_masks(images: Path, name_for: Callable[[Path], str], log: Log, models: Path | None = None) -> dict[str, float]:
    """Mask the text in every photo in images/ too: each photo's mask (name_for(photo), beside it; 0 where it's masked,
    255 elsewhere, as occlusion.write_masks() writes them) gets 0 where there's text, and is written where it had none.
    The share of each photo that is text goes to text.json beside images/ (not in it, where it'd be taken for a photo).

    Returns:
        The share of each photo that is text, by file name, for the photos it could read.
    """
    import json

    import cv2
    import numpy as np
    photos = sorted(p for p in images.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")
                    and not p.name.endswith(".mask.png"))
    shares, masked = {}, 0
    for p in photos:
        t = text_mask(p, models)
        if t is None:
            continue
        shares[p.name] = float(t.mean())
        if not t.any():
            continue
        f = images / name_for(p)
        old = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE) if f.exists() else None
        if old is None or old.shape != t.shape:
            old = np.full(t.shape, 255, np.uint8)
        cv2.imwrite(str(f), np.where(t, 0, old).astype(np.uint8))
        masked += 1
    (images.parent / SHARES).write_text(json.dumps(shares))
    log(f"text: found in {masked} of {len(photos)} photos, masked for texturing")
    return shares


def for_texturing(images: Path, mask_args: list[str], log: Log) -> list[str]:
    """Mask the text in the undistorted photos in images/ (add_to_masks(), as <stem>.mask.png beside them, where
    TextureMesh looks), after the depth maps were made from them: TextureMesh's mask arguments: given if any photo
    has a mask now. Never fails texturing: if the text can't be looked for, the photos are textured from as they were."""
    if os.environ.get("TEXT_MASKS", "1") == "0" or not available():
        return mask_args
    try:
        shares = add_to_masks(images, lambda f: f"{f.stem}.mask.png", log)
    except Exception as e:  # noqa: BLE001 - only costs the text masks
        log(f"text: not masked ({type(e).__name__}: {e})")
        return mask_args
    return mask_args or (["--ignore-mask-label", "0"] if any(shares.values()) else [])
