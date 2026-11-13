"""How big a boulder is: metres per unit of its mesh, which photogrammetry alone can't say.

COLMAP's meshes come out at an arbitrary scale. Things in the photos placed in a mesh give it one:

  video      a video's climber's limb lengths, from a pose model's keypoints, at the depth where their
             hands and feet meet the mesh, against usual body proportions, scaled to the climber's height
             where their video or channel gives it (youtube.stats)
  photos     the same for climbers in other photos, a different person in each, together
  standing   people standing upright on level ground, from their heels to the top of their head
  stated     Mountain Project's height for the problem, against the rock's height near where the
             photos look: a fallback
  span       a video's climber's reach between hands on holds (beta.py's contacts): recorded, not yet used
  object     a bottle of known size that a project's photos are said to show (known_object()), standing on
             ground in the mesh: for your own photos, as in harvested ones they're too few and too small,
             and the ground under them isn't in the mesh

  poses.json         per placed photo with people: each person's keypoints and their scores
  scale_people.json  each person measured against the mesh (measure())
  objects.json       per placed photo looked at: the bottles found in it, {box, score}
  scale.json         {status, metres_per_unit, low, high, sd, method, height_m, heights, estimates, checks}

design.md has how far these agree.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
FILE, POSES_FILE = "scale.json", "poses.json"
FT = 0.3048

# ------------------------------------------------------------------ Mountain Project's height
TYPE_HEIGHT = re.compile(r"\b(\d{1,3})\s*ft\b")
TEXT_HEIGHT = re.compile(r"(?:about|roughly|around|maybe|approximately|tops? out at|is)?\s*(\d{1,2})(?:\s*(?:-|to)\s*(\d{1,2}))?\s*"
                         r"(?:ft\b|feet\b|foot\b|')\s*(?:tall|high\b|in height)", re.I)


def stated_height(climb: dict) -> dict | None:
    """A problem's height as its Mountain Project page gives it: its type line ("Boulder, 15 ft (5 m)"),
    or else its description ("about 16 ft high").

    Returns:
        {m, text, where}, or None. A range ("20-25 feet") counts as its middle.
    """
    kind = (climb.get("raw") or {}).get("type") or ""
    m = TYPE_HEIGHT.search(kind)
    if m and 3 <= int(m[1]) <= 60:
        return {"m": round(int(m[1]) * FT, 2), "text": kind, "where": "type"}
    for field in ("description", "location"):
        text = climb.get(field) or ""
        for m in TEXT_HEIGHT.finditer(text):
            if re.search(r"off the ground|fall", text[m.end():m.end() + 20], re.I):
                continue
            ft = (int(m[1]) + int(m[2] or m[1])) / 2
            if 3 <= ft <= 60:
                return {"m": round(ft * FT, 2), "text": text[max(0, m.start() - 30):m.end() + 10].strip(), "where": field}
    return None


# ------------------------------------------------------------------ the mesh
def kept_faces(mesh_dir: Path, faces: int):
    """Which faces cleanup kept (meshclean.py), or None if it hasn't run."""
    import numpy as np
    import meshclean
    record = meshclean.load(mesh_dir)
    path = mesh_dir / meshclean.FACES_FILE
    if not record or not path.exists() or record.get("faces") != faces:
        return None
    labels = np.frombuffer(path.read_bytes(), {2: "<u2", 4: "<u4"}[record["label_bytes"]])
    return ~np.isin(labels, record.get("removed") or [])


def _weighted_quantile(values, weights, qs):
    import numpy as np
    order = np.argsort(values)
    cum = np.cumsum(weights[order])
    return [float(values[order][min(np.searchsorted(cum, q * cum[-1]), len(values) - 1)]) for q in qs]


def heights(mesh, cams: list[dict], kept=None) -> dict:
    """The rock's height in the mesh's units, measured two ways, +Y being up in a levelled mesh.

    whole: the kept mesh's lowest 1% to highest 0.5% by area. around: the same, within the photos' focus,
    where the centres of the placed photos land on the mesh, widened by half again, as the mesh often
    reaches far beyond the boulder.

    Returns:
        {whole, around, focus: [x, y, z]}; around is None when too few photos' centres land on the mesh.
    """
    import numpy as np
    import beta
    area = np.linalg.norm(np.cross(mesh.e1, mesh.e2), axis=1) / 2
    keep = np.ones(len(area), bool) if kept is None else kept
    y = mesh.c[:, 1]
    lo, hi = _weighted_quantile(y[keep], area[keep], (0.01, 0.995))
    out = {"whole": round(hi - lo, 4), "around": None, "focus": None}
    hits = []
    for c in cams:
        size = (c["lens"]["width"], c["lens"]["height"]) if c.get("lens") else (1000, 750)
        X = mesh.hit(*beta.camera_ray(c, size, (size[0] / 2, size[1] / 2)))
        if X is not None:
            hits.append(X)
    if len(hits) < 3:
        return out
    hits = np.array(hits)
    focus = np.median(hits, axis=0)
    spread = np.linalg.norm((hits - focus)[:, [0, 2]], axis=1)
    radius = 1.5 * float(np.quantile(spread, 0.9))
    near = keep & (np.linalg.norm((mesh.c - focus)[:, [0, 2]], axis=1) <= radius)
    if near.sum() > 100:
        lo, hi = _weighted_quantile(y[near], area[near], (0.01, 0.995))
        out["around"] = round(hi - lo, 4)
    out["focus"] = [round(float(v), 4) for v in focus]
    return out


# ------------------------------------------------------------------ people
# Halpe26 keypoints (beta.POSE's model): shoulders 5, 6; elbows 7, 8; wrists 9, 10; hips 11, 12; knees 13, 14;
# ankles 15, 16; neck 18; mid-hip 19; big toes 20, 21; heels 24, 25.
# Each segment: keypoint pairs, and its usual length as a share of height (Winter, Biomechanics and Motor
# Control of Human Movement, fig. 4.1); arms' scale with wingspan rather than height. The model's shoulder,
# neck and mid-hip points aren't the joints Winter measured between, so the upper arm's and torso's are
# what 95 videos' climbers gave against their forearms and legs (design.md): Winter's are 0.186 and 0.288.
SEGMENTS = {"upper_arm": (((5, 7), (6, 8)), 0.169, True), "forearm": (((7, 9), (8, 10)), 0.146, True),
            "thigh": (((11, 13), (12, 14)), 0.245, False), "shank": (((13, 15), (14, 16)), 0.246, False),
            "torso": (((18, 19),), 0.354, False)}
ANCHORS = {"lh": None, "rh": None, "lf": (20, 15, 24), "rf": (21, 16, 25)}    # hands from beta.limb_points
KP_MIN = 0.3
HEIGHT_CM, HEIGHT_SD_CM = 174.0, 9.0      # an unknown climber: men and women together
APE_CM = 2.0                              # an unknown climber's wingspan less their height


def anchor_points(kp, sc) -> dict:
    """Where a person meets the rock or ground, if they do: each hand's point of contact (beta.limb_points),
    and each foot's big toe, or else ankle, or heel."""
    import beta
    out = {k: v for k, v in beta.limb_points(kp, sc).items() if k in ("lh", "rh")}
    for k, idx in ANCHORS.items():
        if idx:
            out[k] = next((tuple(map(float, kp[i])) for i in idx if sc[i] >= KP_MIN), None)
    return out


def measure_person(view, kp, sc) -> dict | None:
    """One person in a placed photo: how long their segments look, at unit depth, and how far away the
    points where they could be touching the mesh are.

    Args:
        view: beta.View of the photo.
        kp, sc: the person's keypoints, in pixels, and their scores.

    Returns:
        {segments: {name: [length at unit depth, ...]}, anchors: {limb: depth}, ys: {limb: height on the mesh},
        points: {limb: [x, y, z]}, behind: {"5" | "6" | "18": the rock behind each shoulder and the neck},
        elbows: {"l" | "r": angle in the photo, degrees}, standing: standing()}, or None without any segment.
    """
    import numpy as np
    import beta
    kp, sc = np.asarray(kp, float), np.asarray(sc, float)
    norm = {}

    def n(i):
        if i not in norm:
            norm[i] = np.array(beta.normalized(view.cam, view.size, kp[i]))
        return norm[i]
    segments = {}
    for name, (pairs, _, _) in SEGMENTS.items():
        lengths = [round(float(np.linalg.norm(n(a) - n(b))), 6) for a, b in pairs if sc[a] >= KP_MIN and sc[b] >= KP_MIN]
        if lengths:
            segments[name] = lengths
    if not segments:
        return None
    anchors, ys, points = {}, {}, {}
    inside = lambda uv: 0 <= uv[0] < view.size[0] and 0 <= uv[1] < view.size[1]  # noqa: E731
    for limb, uv in anchor_points(kp, sc).items():
        if uv is None or not inside(uv):
            continue
        X, z = view.hit(uv)
        if X is not None and z > 0:
            anchors[limb], ys[limb], points[limb] = round(z, 5), round(float(X[1]), 5), [round(float(v), 5) for v in X]
    behind = {}
    for i in (5, 6, 18):      # the rock behind each shoulder and the neck, which a span between the hands goes in front of
        if sc[i] >= KP_MIN and inside(kp[i]):
            X, _ = view.hit(kp[i])
            if X is not None:
                behind[str(i)] = [round(float(v), 5) for v in X]
    elbows = {}
    for side, (s, e, w) in (("l", (5, 7, 9)), ("r", (6, 8, 10))):
        if min(sc[s], sc[e], sc[w]) >= KP_MIN:
            a, b = kp[s] - kp[e], kp[w] - kp[e]
            elbows[side] = round(float(np.degrees(np.arccos(np.clip(a @ b / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-9), -1, 1)))), 1)
    stands = standing(view, kp, sc)
    return {"segments": segments, "anchors": anchors, "ys": ys, "points": points, "behind": behind, "elbows": elbows,
            "standing": None if stands is None else round(stands, 5)}


UPRIGHT_DEG = 20      # torso and legs within this of vertical: standing
GROUND_COS = 0.8      # ground's normal is within 37 degrees of vertical; a ray past the ground meets steeper rock


def vertical_plane(view, base):
    """A function taking a pixel of a placed photo to where its ray meets the vertical plane through
    `base` (a point on the mesh) that faces the camera, or to None; its .base is `base`. Things standing
    upright there can be measured on it at their full height, however the camera is tilted."""
    import numpy as np
    import beta
    normal = (view.origin - base) * np.array([1.0, 0.0, 1.0])
    if np.linalg.norm(normal) < 1e-9:
        return None
    normal /= np.linalg.norm(normal)

    def on_plane(uv):
        _, d = beta.camera_ray(view.cam, view.size, uv)
        den = normal @ d
        return None if abs(den) < 1e-6 else view.origin + d * float(normal @ (base - view.origin) / den)
    on_plane.base = np.asarray(base, float)
    return on_plane


def standing(view, kp, sc) -> float | None:
    """How tall a person standing upright on the mesh is, in its units: from where their heels meet it to
    the top of their head (Halpe26's head point), on the vertical plane through their heels that faces
    the camera, so that the camera's tilt doesn't shorten them.

    Returns:
        None unless their head, neck, mid-hip and a whole leg are found, their heels or ankles land on
        mesh at ground (GROUND_COS), and their torso and legs are within UPRIGHT_DEG of vertical (+Y, in a
        levelled mesh).
    """
    import numpy as np
    ok = lambda *idx: all(sc[i] >= KP_MIN for i in idx)  # noqa: E731
    legs = [leg for leg in ((11, 13, 15, 24), (12, 14, 16, 25)) if ok(*leg[:3])]
    if not (ok(17, 18, 19) and legs):
        return None
    feet = []
    for *_, ankle, heel in legs:
        uv = kp[heel] if sc[heel] >= KP_MIN else kp[ankle]
        if 0 <= uv[0] < view.size[0] and 0 <= uv[1] < view.size[1]:
            X, _, n = view.hit(uv, normal=True)
            if X is not None and abs(n[1]) >= GROUND_COS:
                feet.append(X)
    if not feet:
        return None
    on_plane = vertical_plane(view, min(feet, key=lambda X: X[1]))
    if on_plane is None:
        return None
    P = {i: on_plane(kp[i]) for i in {17, 18, 19, *(j for leg in legs for j in leg[:3])}}
    if any(p is None for p in P.values()):
        return None
    foot = on_plane.base

    def upright(a, b):
        """Whether b is above a, within UPRIGHT_DEG of vertical."""
        v = P[b] - P[a]
        return np.degrees(np.arccos(np.clip(v[1] / max(np.linalg.norm(v), 1e-12), -1, 1))) <= UPRIGHT_DEG
    if not (upright(19, 18) and all(upright(knee, hip) and upright(ankle, knee) for hip, knee, ankle, _ in legs)):
        return None
    height = float(P[17][1] - foot[1])
    return height if height > 0 else None


# ------------------------------------------------------------------ objects of known size
# Each: its height and width in metres, as made, and what it is. Height includes the lid.
KNOWN = {"nalgene_wide_32": (0.215, 0.089, "a 32 oz wide-mouth Nalgene"),
         "nalgene_narrow_32": (0.235, 0.089, "a 32 oz narrow-mouth Nalgene")}
OBJECTS_FILE = "objects.json"
OBJECT_LABEL = "bottle"     # the COCO class looked for
OBJECT_MIN_SCORE = 0.5
OBJECT_SHAPE = 1.6          # its measured width for its height within this factor of the real one's: it, not something else
OBJECT_TRIM = 1.5           # measurements this far from the median are another bottle, or a bad one


def known_object(spec: dict | None) -> dict | None:
    """What a project says its photos show, as {what, height, width} in metres, or None.

    Args:
        spec: {"kind": a KNOWN key}, or {"what", "height_cm", "width_cm" (optional)} for another bottle.
    """
    if not spec:
        return None
    if spec.get("kind") in KNOWN:
        h, w, what = KNOWN[spec["kind"]]
        return {"what": what, "height": h, "width": w}
    try:
        h = float(spec["height_cm"]) / 100
        w = float(spec["width_cm"]) / 100 if spec.get("width_cm") else None
    except (KeyError, TypeError, ValueError):
        return None
    if not 0.05 <= h <= 1.0 or (w is not None and not 0.01 <= w <= 0.5):
        return None
    return {"what": str(spec.get("what") or "a bottle")[:80], "height": h, "width": w}


def _detector(log: Log):
    """torchvision's COCO Faster R-CNN, untrained further, at a larger size than its default, as a bottle is a
    small part of a photo of a boulder."""
    import torch
    from torchvision.models.detection import FasterRCNN_ResNet50_FPN_V2_Weights, fasterrcnn_resnet50_fpn_v2
    from climbs.common import CACHE_DIR
    torch.hub.set_dir(str(CACHE_DIR.parent / "models"))
    weights = FasterRCNN_ResNet50_FPN_V2_Weights.COCO_V1
    model = fasterrcnn_resnet50_fpn_v2(weights=weights, min_size=1600, max_size=2400, box_score_thresh=OBJECT_MIN_SCORE).eval()
    log("scale: object detector Faster R-CNN (COCO)")
    return model, weights.meta["categories"].index(OBJECT_LABEL)


def find_objects(mesh_dir: Path, photos: Path, names: list[str], log: Log, progress: Callable[[str], None] = lambda s: None,
                 detect=None) -> dict:
    """The bottles in each of a mesh's placed photos, kept in objects.json: photos already there aren't looked at again.

    Args:
        detect: photo path -> [{box, score}], instead of the detector (for tests).

    Returns:
        {photo name: [{box: [x0, y0, x1, y1], score}]}
    """
    from climbs.common import jobctl
    path = mesh_dir / OBJECTS_FILE
    try:
        found = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        found = {}
    todo = [n for n in names if n not in found and (photos / n).exists()]
    if todo and detect is None:
        import torch
        from PIL import Image
        from torchvision.transforms.functional import to_tensor
        model, label = _detector(log)

        def detect(f):
            with Image.open(f) as im, torch.no_grad():
                out = model([to_tensor(im.convert("RGB"))])[0]
            return [{"box": [round(float(v), 1) for v in b], "score": round(float(sc), 3)}
                    for b, lab, sc in zip(out["boxes"], out["labels"], out["scores"]) if int(lab) == label]
    for i, n in enumerate(todo):
        jobctl.check()
        progress(f"looking for it in photo {i + 1} of {len(todo)}")
        found[n] = detect(photos / n)
        path.write_text(json.dumps(found))
    return found


def object_estimates(mesh, cams: dict, sizes: dict, found: dict, obj: dict) -> list[dict]:
    """Metres per unit from a known object standing on the mesh in the photos it was found in (measure_upright()):
    one estimate, from the median of its measurements that agree, as they're mostly of the same bottle."""
    import numpy as np
    heights = []
    for name, boxes in found.items():
        if name not in cams or name not in sizes:
            continue
        view = None
        for b in boxes:
            view = view or mesh.view(cams[name], sizes[name])
            m = measure_upright(view, b["box"])
            if not m or m["height"] <= 0:
                continue
            if obj["width"] and not 1 / OBJECT_SHAPE <= (m["width"] / m["height"]) / (obj["width"] / obj["height"]) <= OBJECT_SHAPE:
                continue
            heights.append(m["height"])
    if not heights:
        return []
    mid = float(np.median(heights))
    kept = [h for h in heights if 1 / OBJECT_TRIM <= h / mid <= OBJECT_TRIM]
    return [{"kind": "object", "group": "object", "metres_per_unit": obj["height"] / float(np.median(kept)),
             "sd": METHOD_SD["object"], "n": len(kept), "of": len(heights), "what": obj["what"]}]


def measure_upright(view, box) -> dict | None:
    """Something standing upright on the mesh, from its box in a placed photo: its height and width in the
    mesh's units, on the vertical plane through where the middle of its base meets the mesh.

    Args:
        box: (x0, y0, x1, y1), in pixels.

    Returns:
        {height, width, base: [x, y, z], depth}, or None unless its base lands on the mesh at ground
        (GROUND_COS), as one that misses the ground meets steeper rock behind.
    """
    import numpy as np
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2
    base, z, n = view.hit((cx, y1 - 0.02 * (y1 - y0)), normal=True)
    if base is None or abs(n[1]) < GROUND_COS:
        return None
    on_plane = vertical_plane(view, base)
    if on_plane is None:
        return None
    top, bottom = on_plane((cx, y0)), on_plane((cx, y1))
    left, right = on_plane((x0, (y0 + y1) / 2)), on_plane((x1, (y0 + y1) / 2))
    if any(p is None for p in (top, bottom, left, right)):
        return None
    return {"height": float(top[1] - bottom[1]), "width": float(np.linalg.norm((right - left)[[0, 2]])),
            "base": [round(float(v), 5) for v in base], "depth": round(float(z), 5)}


# ------------------------------------------------------------------ people in a mesh's photos
def _photo_size(path: Path) -> tuple[int, int] | None:
    from PIL import Image
    try:
        with Image.open(path) as im:
            return im.size
    except OSError:
        return None


def _read_turned(entry: dict, photo: Path) -> bool:
    """Whether people were found in this photo turned a quarter by its EXIF Orientation: what was recorded as its
    size is its stored size, width and height swapped."""
    size = entry.get("size") if isinstance(entry, dict) else None
    if not isinstance(size, list) or len(size) != 2 or size[0] == size[1] or not photo.exists():
        return False
    return tuple(size) == tuple(reversed(_photo_size(photo) or ()))


def _pose_model(log: Log):
    """beta.py's pose model, for single photos rather than a video's frames."""
    from rtmlib import BodyWithFeet
    from rtmlib.tools.file import download_checkpoint
    import beta
    from climbs.common import CACHE_DIR
    where = str(CACHE_DIR.parent / "models" / "rtmlib")
    det, pose = download_checkpoint(beta.POSE["det"], where), download_checkpoint(beta.POSE["pose"], where)
    log(f"scale: pose model {Path(pose).name}")
    return BodyWithFeet(det=det, det_input_size=beta.POSE["det_input_size"], pose=pose, pose_input_size=beta.POSE["pose_input_size"],
                        backend="onnxruntime", device="cpu")


def find_poses(mesh_dir: Path, photos: Path, masks: Path | None, log: Log, progress: Callable[[str], None] = lambda s: None,
               look: bool = True) -> dict:
    """The people in each placed photo, kept in poses.json: photos already there aren't looked at again,
    and only those whose people mask (occlusion.py) found people are looked at, or all without masks.
    People found in a photo as its EXIF Orientation turned it (as photos were read before) are in the wrong axes
    for its camera: they're dropped, and looked for again.

    Args:
        look: False to only read what's there, as without the pose model.

    Returns:
        {photo: {size: [w, h], people: [{kp, sc}]}}, people with at least 6 keypoints found.
    """
    path = mesh_dir / POSES_FILE
    try:
        found = json.loads(path.read_text())["photos"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        found = {}
    found = {n: f for n, f in found.items() if not _read_turned(f, photos / n)}
    shares = {}
    if masks and (masks / "people.json").exists():
        shares = json.loads((masks / "people.json").read_text())
    names = [c["name"] for c in json.loads((mesh_dir / "cameras.json").read_text())]
    todo = [n for n in names if n not in found and (not shares or shares.get(n, 0) > 0.005) and (photos / n).exists()]
    if not todo or not look:
        return found
    import cv2
    model = _pose_model(log)
    from climbs.common import jobctl
    for i, n in enumerate(todo):
        jobctl.check()
        if i % 5 == 0:
            progress(f"people in photo {i + 1} of {len(todo)}")
        im = cv2.imread(str(photos / n), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)    # COLMAP's pixels, as cameras.json has them
        if im is None:
            continue
        kps, scs = model(im)
        found[n] = {"size": [im.shape[1], im.shape[0]],
                    "people": [{"kp": [[round(float(a), 1), round(float(b), 1)] for a, b in kp], "sc": [round(float(s), 3) for s in sc]}
                               for kp, sc in zip(kps, scs) if (sc >= KP_MIN).sum() >= 6]}
        if i % 20 == 19:
            path.write_text(json.dumps({"photos": found}))
    path.write_text(json.dumps({"photos": found}))
    log(f"scale: looked for people in {len(todo)} photos")
    return found


def _by_size(people: list[dict]) -> list[dict]:
    """People largest first, by the box around their keypoints found: the climber, in a climbing photo."""
    import numpy as np

    def area(p):
        kp, sc = np.asarray(p["kp"]), np.asarray(p["sc"])
        good = kp[sc >= KP_MIN]
        return float(np.prod(good.max(axis=0) - good.min(axis=0))) if len(good) else 0.0
    return sorted(people, key=area, reverse=True)


def measure(mesh, cams: dict, poses: dict, sources: dict, beta_poses: dict | None = None, photos: Path | None = None,
            check: Callable[[], None] = lambda: None) -> list[dict]:
    """Every person found in a mesh's placed photos, and each frame's climber that the beta search kept.

    Args:
        cams: cameras.json entries, by photo.
        poses: find_poses().
        sources: photo_sources.json entries, by photo: a video's frames are grouped by its url.
        beta_poses: beta.POSES_FILE, if there is one: frames matched to a placed frame are measured in it,
            their keypoints mapped by the homography the match found.
        photos: where the placed photos are, for the sizes of those beta's frames matched.

    Returns:
        [{photo, group, rank, segments, anchors, ys, standing}], group a video's url, or "photo:<name>".
    """
    import numpy as np
    views, rows = {}, []

    def view(name, size):
        if name not in views:
            views[name] = mesh.view(cams[name], tuple(size))
        return views[name]
    for name, ph in poses.items():
        if name not in cams or not ph["people"]:
            continue
        check()
        s = sources.get(name, {})
        group = s["url"] if s.get("source") == "youtube" and s.get("url") else f"photo:{name}"
        for rank, p in enumerate(_by_size(ph["people"])):
            m = measure_person(view(name, ph["size"]), p["kp"], p["sc"])
            if m:
                rows.append({"photo": name, "group": group, "rank": rank, **m})
    sizes = {}
    for url, frames in (beta_poses or {}).items():
        for f in frames:
            ref, H = f.get("ref"), f.get("H")
            if not ref or not H or ref not in cams:
                continue
            check()
            if ref not in sizes:
                sizes[ref] = (tuple(poses[ref]["size"]) if ref in poses else _photo_size(photos / ref) if photos else None)
            if not sizes[ref]:
                continue
            kp = np.asarray(f["kp"], float)
            q = np.c_[kp, np.ones(len(kp))] @ np.asarray(H, float).reshape(3, 3).T
            m = measure_person(view(ref, sizes[ref]), q[:, :2] / q[:, 2:], f["sc"])
            if m:
                rows.append({"photo": ref, "group": url, "rank": 0, "beta": True, "t": f["t"], **m})
    return rows


# ------------------------------------------------------------------ estimates
Q = 0.75              # a segment's length: this quantile of how long it looks across a video's frames
TRIM = 1.5            # frames whose own estimate is this far from the video's median are left out
DEPTH_AGREE = 0.15    # a person's depth: their nearest hand or foot on the mesh with another this close
MIN_SEGMENT = 5       # measurements a segment needs, in one video, to count
METHOD_SD = {"video": 0.12, "photos": 0.25, "standing": 0.3, "stated": 0.33, "span": 0.2,   # log sd of one estimate; see design.md
             "object": 0.1}
PHONE_FLOOR_SD = 0.015
FLOOR_SD = 0.05       # however many agree: what's common to them all (proportions, depth), as a log sd


def _mad(x) -> float:
    import numpy as np
    x = np.asarray(x, float)
    return float(1.4826 * np.median(np.abs(x - np.median(x)))) if len(x) else 0.0


def stature(info: dict | None) -> tuple[float, float, float | None]:
    """A climber's height and wingspan (cm), and the sd of the height, from what their video or channel
    says (server.py's videos and channels rows: height_cm, wingspan_cm, ape_index_cm), or usual values."""
    info = info or {}
    h = info.get("height_cm") or info.get("channel_height_cm")
    span = info.get("wingspan_cm") or info.get("channel_wingspan_cm")
    ape = info.get("ape_index_cm") if info.get("ape_index_cm") is not None else info.get("channel_ape_index_cm")
    if ape is None and h and span:
        ape = span - h
    return (h or HEIGHT_CM), (h or HEIGHT_CM) + (ape if ape is not None else APE_CM), (2.0 if h else HEIGHT_SD_CM)


def person_depth(anchors: dict) -> float | None:
    """How far a person is from the camera: the nearest of their hands and feet on the mesh that another is
    within DEPTH_AGREE of. One alone can be a ray past the rock's edge, meeting rock far behind."""
    z = sorted(anchors.values())
    for i, a in enumerate(z):
        if any(abs(b - a) <= DEPTH_AGREE * a for j, b in enumerate(z) if j != i):
            return a
    return None


def people_estimates(rows: list[dict], videos: dict) -> list[dict]:
    """Metres per unit from people: one estimate per video from its climber's limbs, one from climbers in
    other photos together, and one from everyone found standing upright.

    A video's: each frame gives its own (the median of its segments'), and frames more than TRIM from the
    median of those are left out; then each segment's Q quantile across the rest, against its usual share of
    the climber's height (or wingspan, for arms), and the median of the segments.

    Args:
        rows: measure().
        videos: what each video (by url) says about its climber, for stature().

    Returns:
        [{kind: video | photos | standing, group, metres_per_unit, sd, n, height_cm, stated}], sd a log sd.
    """
    import numpy as np
    groups, standing = {}, []
    for r in rows:
        if r["standing"] is not None:
            standing.append(r)
        z = person_depth(r["anchors"]) if r["rank"] == 0 else None
        if z:
            groups.setdefault(r["group"] if not r["group"].startswith("photo:") else "photos", []).append((r, z))
    out = []
    for g, frames in groups.items():
        h, span, sd_cm = stature(videos.get(g)) if g != "photos" else (HEIGHT_CM, HEIGHT_CM + APE_CM, HEIGHT_SD_CM)
        size = {name: share * (span if arm else h) / 100 for name, (_, share, arm) in SEGMENTS.items()}

        def own(r, z, size=size):
            v = [size[name] / (length * z) for name, lengths in r["segments"].items() for length in lengths if length > 0]
            return float(np.median(v)) if v else None
        scored = [(r, z, own(r, z)) for r, z in frames]
        scored = [x for x in scored if x[2]]
        if not scored:
            continue
        mid = float(np.median([x[2] for x in scored]))
        segs, kept = {}, 0
        for r, z, s in scored:
            if mid / TRIM <= s <= mid * TRIM:
                kept += 1
                for name, lengths in r["segments"].items():
                    segs.setdefault(name, []).extend(length * z for length in lengths if length > 0)
        per = {name: size[name] / float(np.quantile(segs[name], Q)) for name in SEGMENTS if len(segs.get(name, ())) >= MIN_SEGMENT}
        if len(per) < 2:
            continue
        logs = np.log(list(per.values()))
        kind = "photos" if g == "photos" else "video"
        sd = float(np.sqrt((sd_cm / h) ** 2 + _mad(logs) ** 2 / len(logs) + METHOD_SD[kind] ** 2))
        out.append({"kind": kind, "group": g, "metres_per_unit": float(np.exp(np.median(logs))), "sd": round(sd, 4),
                    "n": kept, "height_cm": h, "stated": sd_cm < HEIGHT_SD_CM,
                    "segments": {k: round(v, 6) for k, v in per.items()}})
    if standing:
        each = []
        for r in standing:
            h, _, _ = stature(videos.get(r["group"])) if r["rank"] == 0 else (HEIGHT_CM, None, None)
            each.append(h / 100 / r["standing"])
        logs = np.log(each)
        sd = float(np.sqrt(((HEIGHT_SD_CM / HEIGHT_CM) ** 2 + _mad(logs) ** 2 + METHOD_SD["standing"] ** 2) / len(logs)))
        out.append({"kind": "standing", "group": "standing", "metres_per_unit": float(np.exp(np.median(logs))), "sd": round(sd, 4),
                    "n": len(logs), "height_cm": HEIGHT_CM, "stated": False})
    return out


PALM_SPAN = 2 * (0.169 + 0.146 + 0.35 * 0.146) + 0.12   # palm point to palm point, arms straight, as a share of wingspan
SHOULDER_OFF_M = 0.25     # a climber's shoulders in front of the rock behind them
STRAIGHT_DEG = 150        # an elbow at least this open, in the frame, counts as straight
SPAN_Q = 0.9              # of a video's spans: how far its climber's arms reach when straight


def span_paths(mesh, cams: dict, sizes: dict, record: dict, frames: dict, metres_per_unit: float) -> dict[str, list[float]]:
    """How far apart a video's climber's hands are, through their shoulders, while both hands are on holds
    and both elbows look straight: hand, shoulder, shoulder, hand, each hand at its hold on the mesh and each
    shoulder SHOULDER_OFF_M in front of the rock behind it. With straight arms that's their palm-to-palm
    reach, PALM_SPAN of their wingspan, in any direction, and around the rock rather than through it.

    Args:
        cams: cameras.json entries, by photo; sizes: those photos' sizes.
        record: beta.json; frames: beta.POSES_FILE, both by video.
        metres_per_unit: a first estimate, to place the shoulders.

    Returns:
        {video url: [path in units, per frame]}.
    """
    import numpy as np
    out, views = {}, {}
    for v in record.get("videos") or []:
        on = {"lh": [], "rh": []}
        for h in v.get("holds") or []:
            for c in h["contacts"]:
                if c["limb"] in on:
                    on[c["limb"]].append((c["t0"], c["t1"], np.asarray(h["p"], float)))
        paths = []
        for f in frames.get(v["url"]) or []:
            hands = [next((p for t0, t1, p in on[limb] if t0 <= f["t"] <= t1), None) for limb in ("lh", "rh")]
            ref, H = f.get("ref"), f.get("H")
            if any(h is None for h in hands) or not ref or not H or ref not in cams or ref not in sizes:
                continue
            kp, sc = np.asarray(f["kp"], float), np.asarray(f["sc"], float)
            if min(sc[[5, 6, 7, 8, 9, 10]]) < KP_MIN:
                continue
            angles = []
            for s_, e, w in ((5, 7, 9), (6, 8, 10)):
                a, b = kp[s_] - kp[e], kp[w] - kp[e]
                angles.append(np.degrees(np.arccos(np.clip(a @ b / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-9), -1, 1))))
            if min(angles) < STRAIGHT_DEG:
                continue
            if ref not in views:
                views[ref] = mesh.view(cams[ref], sizes[ref])
            view = views[ref]
            q = np.c_[kp[[5, 6]], np.ones(2)] @ np.asarray(H, float).reshape(3, 3).T
            shoulders = []
            for uv in q[:, :2] / q[:, 2:]:
                X, _ = view.hit(uv)
                if X is None:
                    break
                u = view.origin - X
                shoulders.append(X + u / np.linalg.norm(u) * SHOULDER_OFF_M / metres_per_unit)
            if len(shoulders) < 2:
                continue
            paths.append(float(np.linalg.norm(hands[0] - shoulders[0]) + np.linalg.norm(shoulders[0] - shoulders[1])
                               + np.linalg.norm(shoulders[1] - hands[1])))
        if paths:
            out[v["url"]] = paths
    return out


def span_estimates(paths: dict[str, list[float]], videos: dict, min_frames: int = 5) -> list[dict]:
    """Metres per unit from each video's climber's reach (span_paths()): its SPAN_Q quantile taken as PALM_SPAN
    of their wingspan."""
    import numpy as np
    out = []
    for url, ps in paths.items():
        if len(ps) < min_frames:
            continue
        h, span, sd_cm = stature(videos.get(url))
        out.append({"kind": "span", "group": url, "metres_per_unit": PALM_SPAN * span / 100 / float(np.quantile(ps, SPAN_Q)),
                    "sd": round(float(np.hypot(sd_cm / h, METHOD_SD["span"])), 4), "n": len(ps), "height_cm": h, "stated": sd_cm < HEIGHT_SD_CM})
    return out


def stated_estimate(climbs: list[dict], measured: dict) -> dict | None:
    """Metres per unit from Mountain Project's height for the problem, against the rock's height near where
    the photos look (heights())."""
    stated = next((s for s in map(stated_height, climbs) if s), None)
    if not stated or not measured.get("around"):
        return None
    return {"kind": "stated", "group": stated["where"], "metres_per_unit": stated["m"] / measured["around"],
            "sd": METHOD_SD["stated"], "n": 1, "stated_m": stated["m"], "text": stated["text"]}


def combine(estimates: list[dict]) -> dict | None:
    """One scale from several: their median in log space, each weighted by 1/sd^2, so that one far off moves
    it little; its range from the weights, widened by how much they disagree, and never narrower than FLOOR_SD.

    Returns:
        {metres_per_unit, low, high, sd, method}: method the kind that weighed most.
    """
    import numpy as np
    if not estimates:
        return None
    logs = np.log([e["metres_per_unit"] for e in estimates])
    w = np.array([1 / e["sd"] ** 2 for e in estimates])
    order = np.argsort(logs)
    cum = np.cumsum(w[order])
    i = int(np.searchsorted(cum, cum[-1] / 2))
    middle = float(logs[order][i]) if cum[i] > cum[-1] / 2 or i + 1 == len(logs) else float(logs[order][i:i + 2].mean())
    sd = 1 / np.sqrt(w.sum())
    if len(logs) > 1:
        sd *= max(1.0, np.sqrt(float(np.sum(w * (logs - middle) ** 2)) / (len(logs) - 1)))
    weight = {}
    for e, wi in zip(estimates, w):
        weight[e["kind"]] = weight.get(e["kind"], 0) + wi
    # the floor is what estimates from people share (proportions, depth): not a phone's own measure of how far it moved
    sd = float(np.hypot(sd, PHONE_FLOOR_SD if max(weight, key=weight.get) == "phone" else FLOOR_SD))
    return {"metres_per_unit": round(float(np.exp(middle)), 6), "low": round(float(np.exp(middle - sd)), 6),
            "high": round(float(np.exp(middle + sd)), 6), "sd": round(sd, 4), "method": max(weight, key=weight.get)}


# ------------------------------------------------------------------ a mesh
MEASURED_FILE = "scale_people.json"


def available() -> bool:
    """Whether people can be looked for (the pose model's packages); without, only Mountain Project's height is used."""
    import beta
    return beta.available()


def estimate(mesh_dir: Path, photos: Path, masks: Path | None, climbs: list[dict], videos: dict, log: Log,
             progress: Callable[[str], None] = lambda s: None, known: dict | None = None, detect=None) -> dict:
    """Find a mesh's scale and save scale.json, with people's measurements in scale_people.json. Where the
    beta search kept its frames, climbers' reach (span_estimates()) is recorded as a check, not yet used.

    Args:
        photos, masks: the mesh's project's photos and people masks (occlusion.py).
        climbs: the climbs it's linked to, as climbs' data.
        videos: what each of its videos (by url) says about its climber: {height_cm, wingspan_cm,
            ape_index_cm, channel_height_cm, ...}.
        known: what its project says its photos show, for known_object().
        detect: find_objects()'s, for tests.

    Returns:
        The record saved.
    """
    import beta
    import provenance
    from climbs.common import jobctl
    save(mesh_dir, {"status": "running", "started": time.time(), "progress": "measuring the rock"})
    mesh = beta.Mesh.load(mesh_dir)
    cams = {c["name"]: c for c in json.loads((mesh_dir / "cameras.json").read_text())}
    measured = heights(mesh, list(cams.values()), kept_faces(mesh_dir, len(mesh.v0)))

    def step(s):
        save(mesh_dir, {"status": "running", "progress": s})
        progress(s)
    poses = find_poses(mesh_dir, photos, masks, log, step, look=available())
    step("measuring people")
    sources = {p["file"]: p for p in (provenance.load(mesh_dir) or {}).get("photos", [])}
    try:
        beta_poses = json.loads((mesh_dir / beta.POSES_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        beta_poses = None
    rows = measure(mesh, cams, poses, sources, beta_poses, photos, check=jobctl.check)
    (mesh_dir / MEASURED_FILE).write_text(json.dumps(rows))
    estimates = people_estimates(rows, videos)
    import phonedata    # photos BoulderMesh's apps took say how far apart they were taken, and where
    taken = phonedata.blobs(photos, list(cams))
    by_phone = phonedata.scale_estimates(list(cams.values()), taken) if taken else []
    if taken:
        log(f"scale: {len(taken)} placed photos taken with the app" + (f": {', '.join(str(e['metres_per_unit']) for e in by_phone)} m per unit "
            f"by the phone's motion tracking, from {sum(e['n'] for e in by_phone)} of them" if by_phone else ", too few tracked to size it by"))
    estimates += by_phone
    obj = known_object(known)
    if obj:
        found = find_objects(mesh_dir, photos, list(cams), log, step, detect)
        sizes = {n: tuple(poses[n]["size"]) if n in poses else _photo_size(photos / n) for n in found}
        objects = object_estimates(mesh, cams, {n: v for n, v in sizes.items() if v}, found, obj)
        log(f"scale: {obj['what']}: found in {sum(1 for b in found.values() if b)} of {len(found)} photos"
            + (f", {objects[0]['n']} measurements agreeing" if objects else ", never standing on ground in the mesh"))
        estimates += objects
    stated = stated_estimate(climbs, measured)
    final = combine(estimates + ([stated] if stated else []))
    checks = []
    found = beta.load(mesh_dir)
    if final and found and beta_poses:
        sizes = {n: tuple(poses[n]["size"]) if n in poses else _photo_size(photos / n) for n in cams}
        paths = span_paths(mesh, cams, {n: v for n, v in sizes.items() if v}, found, beta_poses, final["metres_per_unit"])
        checks = span_estimates(paths, videos)
    record = {"status": "done" if final else "nothing to go by", "finished": time.time(), "progress": None, "heights": measured,
              "people": len(rows), "estimates": estimates + ([stated] if stated else []), "checks": checks, **(final or {})}
    if final and measured.get("around"):
        record["height_m"] = round(measured["around"] * final["metres_per_unit"], 2)
    if taken:   # and where it is, from their GPS fixes, if they say
        import heading
        north = heading.best(heading.load(mesh_dir))
        record["phone"] = {"photos": len(taken), "position": phonedata.position(
            list(cams.values()), taken, final["metres_per_unit"], measured.get("focus"), north["deg"] if north else None) if final else None}
    save(mesh_dir, record)
    log(f"scale: {len(rows)} people measured, {len(estimates)} estimates from them" + (", and Mountain Project's height" if stated else "")
        + (f": {final['metres_per_unit']:.4g} m per unit ({final['low']:.4g} to {final['high']:.4g}), by {final['method']}" if final else ": nothing to go by"))
    return record


RESULT = ("metres_per_unit", "low", "high", "method", "height_m", "estimates", "finished")    # what a finished search found


def save(mesh_dir: Path, record: dict) -> None:
    """Save a mesh's record. One without a size (queued, running, stopped, failed, or finding nothing) keeps the last
    size found as its "previous", so that finding it again loses nothing until a new one is found (summary() and
    the server's metres_per_unit() go by it meanwhile)."""
    if not record.get("metres_per_unit") and "previous" not in record:
        before = load(mesh_dir) or {}
        prev = {k: before[k] for k in RESULT if k in before} if before.get("metres_per_unit") else before.get("previous")
        if prev:
            record = {**record, "previous": prev}
    tmp = mesh_dir / (FILE + ".tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(mesh_dir / FILE)


def load(mesh_dir: Path) -> dict | None:
    try:
        return json.loads((mesh_dir / FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def summary(record: dict | None) -> dict | None:
    """For the list of meshes: how finding it got on, and what it found."""
    if not record:
        return None
    if not record.get("metres_per_unit") and record.get("previous"):    # the last size found, while looked for again
        record = {**record, **record["previous"], "status": record.get("status"), "progress": record.get("progress"), "earlier": True}
    out = {k: record.get(k) for k in ("status", "progress", "metres_per_unit", "low", "high", "method", "height_m", "earlier", "phone")}
    kinds = [e["kind"] for e in record.get("estimates") or []]
    out.update(videos=kinds.count("video"), photos="photos" in kinds, standing=next((e["n"] for e in record.get("estimates") or [] if e["kind"] == "standing"), 0),
               stated=next(({"m": e["stated_m"], "text": e["text"], "metres_per_unit": e["metres_per_unit"]}
                            for e in record.get("estimates") or [] if e["kind"] == "stated"), None),
               object=next(({"what": e["what"], "n": e["n"]} for e in record.get("estimates") or [] if e["kind"] == "object"), None))
    return out


def carry_on(mesh_dir: Path) -> bool:
    """Mark a search that a restart stopped (queued, or under way) as queued again; whether it was one, for the caller
    to queue it (server.queue_scale() won't, as it's queued)."""
    record = load(mesh_dir)
    if record and record["status"] in ("queued", "running"):
        save(mesh_dir, {**record, "status": "queued", "progress": None})
        return True
    return False
