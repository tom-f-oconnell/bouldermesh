"""Find people in photos, so that reconstruction can ignore them.

Climbers and spotters are in nearly every photo of a boulder problem, and left alone they get
painted onto the rock as texture. Needs torch + torchvision (optional): a DeepLabV3 MobileNetV3
segmentation model, about 0.2 s per photo on a CPU and a 42 MB download the first time.
design.md has the measurements behind how this is used.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
Progress = Callable[..., None]   # progress(fraction, detail)
PHOTO_EXT = {".jpg", ".jpeg", ".png"}
_model = None


def available() -> bool:
    have = lambda m: importlib.util.find_spec(m) is not None  # noqa: E731
    return all(have(m) for m in ("torch", "torchvision", "numpy", "cv2", "PIL"))


def _load(log: Log):
    global _model
    if _model is None:
        import torch
        from torchvision.models.segmentation import DeepLabV3_MobileNet_V3_Large_Weights, deeplabv3_mobilenet_v3_large
        from climbs.common import CACHE_DIR
        torch.hub.set_dir(str(CACHE_DIR.parent / "models"))
        log("occlusion: loading the person segmentation model (a 42 MB download the first time)")
        weights = DeepLabV3_MobileNet_V3_Large_Weights.COCO_WITH_VOC_LABELS_V1
        _model = (deeplabv3_mobilenet_v3_large(weights=weights).eval(), weights.transforms(),
                  weights.meta["categories"].index("person"))
    return _model


def person_mask(path: Path, log: Log, grow: float = 0.02):
    """Where the people are in a photo: a bool array the size of the image.

    Args:
        grow: dilate by this share of the image's longer side, as the model's edges run right
            along people's silhouettes.
    """
    import cv2
    import numpy as np
    import torch
    from PIL import Image
    model, prep, person = _load(log)
    with Image.open(path) as im:
        im = im.convert("RGB")
    with torch.no_grad():
        found = model(prep(im).unsqueeze(0))["out"][0].argmax(0).numpy() == person
    mask = cv2.resize(found.astype(np.uint8), im.size, interpolation=cv2.INTER_NEAREST)
    k = max(3, int(grow * max(im.size)) | 1)
    return cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))).astype(bool)


def write_masks(images: Path, out: Path, name_for: Callable[[Path], str], log: Log,
                progress: Progress | None = None) -> dict[str, float]:
    """Write a mask for every photo, 0 where there are people and 255 elsewhere.

    Args:
        name_for: mask file name for a photo.

    Returns:
        The share of each photo that is people, by file name.
    """
    import cv2
    import numpy as np
    out.mkdir(parents=True, exist_ok=True)
    photos = sorted(p for p in images.iterdir() if p.suffix.lower() in PHOTO_EXT and not p.name.endswith(".mask.png"))
    shares = {}
    for i, p in enumerate(photos):
        if progress:
            progress(i / max(1, len(photos)), f"{i} of {len(photos)} photos")
        mask = person_mask(p, log)
        shares[p.name] = float(mask.mean())
        cv2.imwrite(str(out / name_for(p)), np.where(mask, 0, 255).astype(np.uint8))
    return shares
