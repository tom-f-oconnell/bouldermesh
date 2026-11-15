"""Find people in photos, so that reconstruction can ignore them.

Climbers and spotters are in nearly every photo of a boulder problem, and left alone they get
painted onto the rock as texture. Needs torch + torchvision (optional): a DeepLabV3 MobileNetV3
segmentation model, a 42 MB download the first time. On a GPU with room, if torch has CUDA (the
machines' image), else on the CPU: 0.4 s a photo there at 4 threads, nearly all of it the model
(PEOPLE_GPU=0 keeps it on the CPU). design.md has the measurements behind how this is used.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
Progress = Callable[..., None]   # progress(fraction, detail)
PHOTO_EXT = {".jpg", ".jpeg", ".png"}
SHARES = "people.json"   # the share of each photo that is people, next to its masks
GPU_MIN_FREE_MB = 1500   # free GPU memory it wants before using one: the model's own peak is a few hundred MB
_model = None
_where = None            # "cuda" or "cpu": where the model is, once loaded
_gpu_wanted = True       # False while write_masks() was told to keep off the GPU


def available() -> bool:
    have = lambda m: importlib.util.find_spec(m) is not None  # noqa: E731
    return all(have(m) for m in ("torch", "torchvision", "numpy", "cv2", "PIL"))


def _device(log: Log) -> str:
    """Where the model should run: a GPU if torch can use one and it has room, else the CPU."""
    import torch
    if not _gpu_wanted or os.environ.get("PEOPLE_GPU", "1") == "0" or not torch.cuda.is_available():
        return "cpu"
    try:
        free = torch.cuda.mem_get_info()[0] // 2 ** 20
    except RuntimeError as e:
        log(f"occlusion: the GPU can't be used ({e}): finding people on the CPU")
        return "cpu"
    if free < GPU_MIN_FREE_MB:
        log(f"occlusion: only {free} MB of GPU memory free, and it wants {GPU_MIN_FREE_MB}: finding people on the CPU")
        return "cpu"
    return "cuda"


def _load(log: Log):
    global _model, _where
    if _model is None:
        import torch
        from torchvision.models.segmentation import DeepLabV3_MobileNet_V3_Large_Weights, deeplabv3_mobilenet_v3_large
        from climbs.common import CACHE_DIR
        torch.hub.set_dir(str(CACHE_DIR.parent / "models"))
        _where = _device(log)
        log(f"occlusion: loading the person segmentation model (a 42 MB download the first time), on the {'GPU' if _where == 'cuda' else 'CPU'}")
        weights = DeepLabV3_MobileNet_V3_Large_Weights.COCO_WITH_VOC_LABELS_V1
        _model = (deeplabv3_mobilenet_v3_large(weights=weights).eval().to(_where), weights.transforms(),
                  weights.meta["categories"].index("person"))
    return _model


def release() -> None:
    """Let go of the model if it's on a GPU, so that the steps after have the memory; the next mask loads it again."""
    global _model, _where
    if _where == "cuda":
        import torch
        _model = _where = None
        torch.cuda.empty_cache()


def person_mask(path: Path, log: Log, grow: float = 0.02):
    """Where the people are in a photo: a bool array the size of the image.

    Args:
        grow: dilate by this share of the image's longer side, as the model's edges run right
            along people's silhouettes.
    """
    global _model, _where
    import cv2
    import numpy as np
    import torch
    from PIL import Image
    model, prep, person = _load(log)
    with Image.open(path) as im:
        im = im.convert("RGB")
    x = prep(im).unsqueeze(0)
    try:
        with torch.no_grad():
            found = model(x.to(_where))["out"][0].argmax(0).cpu().numpy() == person
    except RuntimeError as e:      # the GPU ran out of memory, or was lost: this photo and the rest on the CPU
        if _where != "cuda":
            raise
        log(f"occlusion: the GPU failed ({str(e).splitlines()[0][:200]}): carrying on with the CPU")
        model = model.to("cpu")
        _model, _where = (model, prep, person), "cpu"
        torch.cuda.empty_cache()
        with torch.no_grad():
            found = model(x)["out"][0].argmax(0).numpy() == person
    mask = cv2.resize(found.astype(np.uint8), im.size, interpolation=cv2.INTER_NEAREST)
    k = max(3, int(grow * max(im.size)) | 1)
    return cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))).astype(bool)


def write_masks(images: Path, out: Path, name_for: Callable[[Path], str], log: Log,
                progress: Progress | None = None, gpu: bool = True) -> dict[str, float]:
    """Write a mask for every photo, 0 where there are people and 255 elsewhere, and the share of
    each photo that is people to out/people.json. A mask newer than its photo is kept as it is.

    Args:
        name_for: mask file name for a photo.
        gpu: False to keep off the GPU even if there's one with room.

    Returns:
        The share of each photo that is people, by file name.
    """
    global _gpu_wanted
    import json

    import cv2
    import numpy as np
    if gpu != _gpu_wanted:      # a model loaded for the other device is loaded again where it's wanted
        _gpu_wanted = gpu
        if _where == "cuda" and not gpu:
            release()
    out.mkdir(parents=True, exist_ok=True)
    photos = sorted(p for p in images.iterdir() if p.suffix.lower() in PHOTO_EXT and not p.name.endswith(".mask.png"))
    record = out / SHARES
    shares = json.loads(record.read_text()) if record.exists() else {}
    todo = [p for p in photos if not ((out / name_for(p)).exists() and (out / name_for(p)).stat().st_mtime >= p.stat().st_mtime
                                     and p.name in shares)]
    if len(todo) < len(photos):
        log(f"occlusion: {len(photos) - len(todo)} of {len(photos)} photos already have a mask")
    for i, p in enumerate(todo):
        if progress:
            progress(i / max(1, len(todo)), f"{i} of {len(todo)} photos")
        mask = person_mask(p, log)
        shares[p.name] = float(mask.mean())
        cv2.imwrite(str(out / name_for(p)), np.where(mask, 0, 255).astype(np.uint8))
        if i % 20 == 19:   # so an interrupted run keeps what it did
            record.write_text(json.dumps(shares))
    record.write_text(json.dumps(shares))
    if todo:
        release()
    return {p.name: shares[p.name] for p in photos}


def warp_masks(first: Path, cameras: list[dict], undistorted: list[dict], out: Path, name_for: Callable[[Path], str],
               log: Log, progress: Progress | None = None) -> list[str]:
    """The masks of undistorted photos, made from the masks of the photos as taken (write_masks()'s, in `first` as
    <photo name>.png) by moving each pixel as COLMAP's undistorter moved the photo's: no second look for people, and
    the dense steps leave out exactly what the features did. Written to out/ as write_masks() writes them, with
    their shares in out/people.json, so that write_masks() on out/ afterwards does only the photos this couldn't.

    Args:
        cameras: the model's photos as placed, each with its lens (orientation.read_cameras() of the model).
        undistorted: the same of the undistorted model (dense/sparse), whose lenses have no distortion.
        name_for: the mask's file name for an undistorted photo.

    Returns:
        The undistorted photos it wrote no mask for: no first mask, a lens model it doesn't know, or sizes that
        don't fit the lens.
    """
    import json

    import cv2
    import numpy as np
    import splat
    before = {c["name"]: c.get("lens") for c in cameras}
    out.mkdir(parents=True, exist_ok=True)
    record = out / SHARES
    shares = json.loads(record.read_text()) if record.exists() else {}
    left = []
    for i, c in enumerate(undistorted):
        if progress:
            progress(i / max(1, len(undistorted)), f"{i} of {len(undistorted)} photos")
        name, new, old = c["name"], c.get("lens"), before.get(c["name"])
        mask = cv2.imread(str(first / f"{name}.png"), cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION)
        try:
            if mask is None or not new or not old or (mask.shape[1], mask.shape[0]) != (old["width"], old["height"]):
                raise ValueError("no mask of the photo as taken, or it doesn't fit its lens")
            K, dist, _ = splat.intrinsics(old, 1.0)
            Kn, flat, _ = splat.intrinsics(new, 1.0)
            if np.any(flat):
                raise ValueError("the undistorted lens still has distortion")
        except ValueError:
            left.append(name)
            continue
        # COLMAP has a pixel's middle at .5 and OpenCV at 0: both lenses' centres move half a pixel
        K[:2, 2] -= 0.5
        Kn[:2, 2] -= 0.5
        mx, my = cv2.initUndistortRectifyMap(K, dist, None, Kn, (new["width"], new["height"]), cv2.CV_32FC1)
        warped = cv2.remap(mask, mx, my, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=255)
        warped = np.where(warped > 127, 255, 0).astype(np.uint8)
        cv2.imwrite(str(out / name_for(Path(name))), warped)
        shares[name] = float((warped == 0).mean())
    record.write_text(json.dumps(shares))
    return left
