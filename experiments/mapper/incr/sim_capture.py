"""Simulated phone captures, in the apps' own formats (app/FORMAT.md v2), from a boulder whose cameras are known.

    sim_capture.py <state dir> <out dir> [--share 0.3] [--seed 0] [--drift 0.01] [--noise-m 0.01] [--noise-deg 0.5]
                   [--accel-deg 1.5] [--spells 3] [--resets 1] [--jump-m 0.05] [--metres-across 8]

From a state's model (the truth): a random share of its placed photos becomes the capture, the rest the former mesh
(its cameras.json: those cameras as the truth has them, so the former mesh's frame is the truth's). The capture's
photos are copied as cap_<id>_<nnn>.jpg with BMSH1 readings in EXIF UserComment, made from their true cameras:

  - the mesh is put in a tracking world by a made-up fit (scale from --metres-across, a yaw, a small tilt, a shift),
    which the record carries as the fit the app found, with the error a fit has (--fit-err-deg, --fit-err-m);
  - ar {s, p, q} is each camera in that world, plus a drift that grows with the way walked (--drift of it, as a random
    walk) and noise (--noise-m, --noise-deg); the photos are in --spells spells of tracking ("s", "s.2", ...), each
    with its own drift; --resets of them have a jump (--jump-m, a few degrees) partway that the app didn't flag;
  - down is the true gravity in each picture's axes, off by --accel-deg at random (the accelerometer's error);
  - k is the true lens as a pinhole (fx = fy = f, cx, cy), turn 0 (stored upright).

Writes <out>/photos/, <out>/capture.json, <out>/capture-points.bin (the model's points in the tracking world, noisy
to a centimetre), <out>/former/cameras.json, and <out>/truth.json: each capture photo's original name and true camera,
the up the truth has, and what was done to which spell. The truth's up is the mean of its cameras' "up the picture"
(photos are mostly taken level), as the app's model_orientation_aligner levels a model.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import struct
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capture as cp  # noqa: E402


def read_model(model: Path):
    """name -> (R world to camera, centre, lens {fx, fy, cx, cy, w, h}) and the 3D points, via pycolmap if there, else
    the benchmark's stats.py readers."""
    sys.path.insert(0, str(HERE.parent))
    import stats
    poses, cams, imgs = stats.poses(model), stats.cameras(model), stats.images(model)
    sizes = {}
    with open(model / "cameras.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            cid, mid = struct.unpack("<ii", f.read(8))
            w, h = struct.unpack("<QQ", f.read(16))
            f.read(8 * stats.NUM_PARAMS[mid])
            sizes[cid] = (mid, int(w), int(h))
    out = {}
    for n, (R, t) in poses.items():
        cid = imgs[n]
        mid, w, h = sizes[cid]
        p = cams[cid]
        if mid in (1, 4, 5):        # fx, fy, cx, cy first
            k = {"fx": p[0], "fy": p[1], "cx": p[2], "cy": p[3], "w": w, "h": h}
        else:                       # f, cx, cy first
            k = {"fx": p[0], "fy": p[0], "cx": p[1], "cy": p[2], "w": w, "h": h}
        out[n] = (np.asarray(R), -np.asarray(R).T @ np.asarray(t), k)
    pts = []
    with open(model / "points3D.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            f.read(8)
            pts.append(struct.unpack("<3d", f.read(24)))
            f.read(3 + 8)
            f.read(8 * struct.unpack("<Q", f.read(8))[0])
    return out, np.array(pts)


def small_turn(rng, deg):
    a = rng.normal(size=3)
    return cp.axis_angle(a, math.radians(deg) * abs(rng.normal()))


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("state", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--share", type=float, default=0.3)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--drift", type=float, default=0.01)
    p.add_argument("--noise-m", type=float, default=0.01)
    p.add_argument("--noise-deg", type=float, default=0.5)
    p.add_argument("--accel-deg", type=float, default=1.5)
    p.add_argument("--spells", type=int, default=3)
    p.add_argument("--resets", type=int, default=1)
    p.add_argument("--jump-m", type=float, default=0.05)
    p.add_argument("--metres-across", type=float, default=8.0)
    p.add_argument("--fit-err-deg", type=float, default=1.0)
    p.add_argument("--fit-err-m", type=float, default=0.05)
    a = p.parse_args(argv)
    rng = np.random.default_rng(a.seed)
    truth, P = read_model(a.state / "model")
    names = sorted(truth)
    take = sorted(random.Random(a.seed).sample(names, round(len(names) * a.share)))
    former = [n for n in names if n not in take]

    # the truth's up: the mean of its cameras' "up the picture" (-y of each camera, in the world)
    ups = np.array([-truth[n][0].T @ np.array([0.0, 1.0, 0.0]) for n in names])
    up = ups.mean(axis=0)
    up /= np.linalg.norm(up)
    centres = np.array([truth[n][1] for n in names])
    size = float(np.percentile(np.linalg.norm(centres - np.median(centres, axis=0), axis=1), 90)) * 2 or 1.0
    scale = a.metres_across / size        # metres a unit

    # the mesh's frame to the tracking world: levelled (the world's +Y is up), a yaw, and a shift
    level = cp.qfrom_matrix(_rotation_between(up, np.array([0.0, 1.0, 0.0])))
    world_rot = cp.qunit(cp.qmul(cp.axis_angle([0, 1, 0], rng.uniform(0, 2 * math.pi)), level))
    fit = cp.Similarity(scale, world_rot, rng.normal(size=3) * 2)
    # what the app's fit found: off by a little
    found = cp.Similarity(scale * (1 + rng.normal() * 0.01), cp.qunit(cp.qmul(small_turn(rng, a.fit_err_deg), world_rot)),
                          fit.shift + rng.normal(size=3) * a.fit_err_m)

    # the capture's photos in the order walked: round the rock (by bearing about the middle), cut into spells
    mid = np.median(centres, axis=0)
    flat = lambda v: v - (v @ up) * up        # noqa: E731
    ref = flat(truth[take[0]][1] - mid)
    side = np.cross(up, ref)
    bearing = {n: math.atan2(flat(truth[n][1] - mid) @ side, flat(truth[n][1] - mid) @ ref) for n in take}
    order = sorted(take, key=bearing.get)
    cuts = sorted(random.Random(a.seed + 1).sample(range(1, len(order)), min(a.spells - 1, len(order) - 1))) if len(order) > 1 else []
    groups = np.split(np.array(order), cuts)
    reset_spells = set(random.Random(a.seed + 2).sample(range(len(groups)), min(a.resets, len(groups))))

    out = a.out
    shutil.rmtree(out, ignore_errors=True)
    (out / "photos").mkdir(parents=True)
    (out / "former").mkdir()
    cap_id = f"sim{a.seed}"
    shots, truth_out, spell_info = [], {"photos": {}, "up": up.tolist(), "fit": _sim(fit), "fit_found": _sim(found),
                                        "metres_per_unit": scale, "spells": {}}, {}
    i = 0
    from PIL import Image
    src_photos = a.state / "photos"
    for g, group in enumerate(groups):
        s = "c" + ("" if g == 0 else f".{g + 1}")
        drift_t, drift_r, walked, last = np.zeros(3), np.array([1.0, 0, 0, 0]), 0.0, None
        jump_at = len(group) // 2 if g in reset_spells and len(group) > 2 else None
        jump_t, jump_r = rng.normal(size=3) * a.jump_m, small_turn(rng, 3.0)
        for j, n in enumerate(group):
            R, c, k = truth[n]
            cam = cp.Pose(c, cp.qfrom_matrix(R.T), k)        # image axes to the mesh's frame
            w = cp.Pose(fit.apply(cam.centre), cp.qmul(fit.rot, cam.rot), k)       # in the tracking world
            if last is not None:
                step = float(np.linalg.norm(w.centre - last))
                walked += step
                drift_t += rng.normal(size=3) * a.drift * step
                drift_r = cp.qunit(cp.qmul(small_turn(rng, a.drift * step * 57.3 / 2), drift_r))
            last = w.centre
            pc, pr = w.centre + drift_t + rng.normal(size=3) * a.noise_m, cp.qmul(drift_r, cp.qmul(small_turn(rng, a.noise_deg), w.rot))
            if jump_at is not None and j >= jump_at:
                pc, pr = cp.qrot(jump_r, pc) + jump_t, cp.qmul(jump_r, pr)
            down_img = cp.qrot(cp.qconj(w.rot), np.array([0.0, -1.0, 0.0]))        # true gravity in the picture's axes
            down = cp.qrot(small_turn(rng, a.accel_deg), down_img)
            i += 1
            name = f"cap_{cap_id}_{i:03d}.jpg"
            blob = {"v": 1, "app": "sim 0", "device": "simulated", "cap": cap_id, "i": i, "t": 1.8e9 + i * 3,
                    "down": [round(float(x), 6) for x in down / np.linalg.norm(down)], "down_src": "turn", "down_acc": None,
                    "down_track": None, "turn": 0, "heading": None, "heading_acc": None, "gps": None,
                    "ar": {"s": s, "p": [round(float(x), 5) for x in pc], "q": [round(float(x), 7) for x in cp.qunit(pr)], "track": "normal"},
                    "k": {kk: (round(float(v), 3) if kk not in ("w", "h") else int(v)) for kk, v in k.items()},
                    "exp": None, "q": {"sharp": 1.0, "blur_px": 0.0, "clip_hi": 0.0, "clip_lo": 0.0, "ok": True}}
            with Image.open(src_photos / n) as im:
                exif = im.getexif()
                exif.get_ifd(0x8769)[0x9286] = b"ASCII\0\0\0" + ("BMSH1:" + json.dumps(blob, separators=(",", ":"))).encode()
                exif[0x0112] = 1
                im.save(out / "photos" / name, quality=95, exif=exif.tobytes())
            shots.append({"name": name, "blob": blob, "kept": True, "why": [], "notes": [], "helped": []})
            truth_out["photos"][name] = {"was": n, "q": list(map(float, cp.qfrom_matrix(R))), "centre": c.tolist(), "spell": s}
        spell_info[s] = {"photos": len(group), "walked_m": round(walked, 2), "reset_at": jump_at,
                         "drift_end_m": round(float(np.linalg.norm(drift_t)), 3)}
    truth_out["spells"] = spell_info
    rec = {"v": 2, "id": cap_id, "app": "sim 0", "device": "simulated", "started": 1.8e9, "ended": 1.8e9 + i * 3,
           "plan": None, "mesh": "former", "climbs": [], "mode": "plan",
           "fit": {"scale": found.scale, "rot": list(map(float, found.rot)), "shift": found.shift.tolist(),
                   "tilt_deg": None, "taps": 4, "matched": 4, "rms_m": a.fit_err_m},
           "place": None, "calibration": None,
           "level": {"down": [0.0, -1.0, 0.0], "tilt_deg": 0.0, "offset": None, "offset_deg": None, "spread_deg": a.accel_deg,
                     "sectors": None, "samples": None},
           "down_agree_deg": a.accel_deg, "skipped": {}, "progress": None, "shots": shots}
    (out / "capture.json").write_text(json.dumps(rec, indent=1))
    # the tracking's points: the model's, in the world, a centimetre of noise, the first spell's
    W = np.array([fit.apply(x) for x in P[rng.choice(len(P), min(len(P), 20000), replace=False)]]) + rng.normal(size=(min(len(P), 20000), 3)) * 0.01
    with open(out / "capture-points.bin", "wb") as f:
        f.write(b"BMP1" + struct.pack("<I", len(W)))
        for x in W:
            f.write(struct.pack("<3fH", *x, 1))
    # the former mesh's cameras.json, as the app keeps them (name, q, t world to camera, lens)
    cams = []
    for n in former:
        R, c, k = truth[n]
        cams.append({"name": n, "q": list(map(float, cp.qfrom_matrix(R))), "t": (-R @ c).tolist(),
                     "lens": {"model": 1, "width": k["w"], "height": k["h"], "params": [k["fx"], k["fy"], k["cx"], k["cy"]]}})
    (out / "former" / "cameras.json").write_text(json.dumps(cams))
    (out / "truth.json").write_text(json.dumps(truth_out, indent=1))
    print(f"capture of {len(take)} photos in {len(groups)} spells ({sorted(reset_spells)} with a reset); former mesh of {len(former)}; "
          f"{scale:.3f} m a unit")


def _rotation_between(a, b):
    """The shortest rotation taking unit a to unit b."""
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(a @ b)
    if c < -0.999999:
        return np.diag([1.0, -1.0, -1.0])
    K = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + K + K @ K / (1 + c)


def _sim(s: cp.Similarity) -> dict:
    return {"scale": s.scale, "rot": list(map(float, s.rot)), "shift": s.shift.tolist()}


if __name__ == "__main__":
    main()
