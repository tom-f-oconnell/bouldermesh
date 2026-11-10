"""Test 1: how well do single-image depth models match a well-built mesh, photo by photo?

For each placed photo of a reference mesh: the mesh's own depth from that photo's camera (ray cast with Open3D) against
- Depth Anything V2 Small and Large (relative inverse depth: fitted per photo with a scale and an offset), and
- MoGe-2 (depth in metres: fitted per photo with a scale only; how much that scale varies over one rock's photos says
  how consistent its metres are, and the median gives an independent size for the mesh),
- and a flat baseline (one depth for the whole photo, fitted the same way), to say what the numbers mean.
Only rock pixels the mesh covers, and not people (the project's masks).

    python test1.py      (outputs in $DEPTHTEST_OUT/out1, by default out/ here)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import open3d as o3d
import torch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))
import meshclean  # noqa: E402
import splat  # noqa: E402

OUT = Path(os.environ.get("DEPTHTEST_OUT", Path(__file__).resolve().parent / "out")) / "out1"
OUT.mkdir(parents=True, exist_ok=True)
CLIMBS = {"2cd16f494134": ("Monkey Gone to Heaven", "e58c93acf94b"), "dd872c9a20b6": ("Little Chucky", "50099afdedf8"),
          "ba864cab10ff": ("Square Dance", "0777cc139076"), "12fae5f39a3e": ("Midnight Lightning", "880e1a362d40")}
MAX_SIDE = 640
dev = "cuda"


def gt_depth(scene, view):
    """z depth of the mesh from this photo's camera, inf where a ray misses."""
    h, w = view["valid"].shape
    K, Vm = view["K"], view["viewmat"]
    R, t = Vm[:3, :3], Vm[:3, 3]
    C = -R.T @ t
    u, v = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    d_cam = np.stack([(u - K[0, 2]) / K[0, 0], (v - K[1, 2]) / K[1, 1], np.ones_like(u)], -1).reshape(-1, 3)
    d_world = d_cam @ R          # R^T applied to each row
    rays = np.concatenate([np.repeat(C[None], len(d_world), 0), d_world], 1).astype(np.float32)
    hit = scene.cast_rays(o3d.core.Tensor(rays))["t_hit"].numpy().reshape(h, w)
    return hit                     # t along a direction whose camera z is 1: z depth


def fit_disparity(pred, gt, m):
    """Scale and offset on inverse depth, least squares; the aligned depth."""
    A = np.stack([pred[m], np.ones(m.sum())], 1)
    s, o = np.linalg.lstsq(A, 1 / gt[m], rcond=None)[0]
    return 1 / np.clip(s * pred + o, 1e-6, None)


def metrics(z, gt, m):
    r = z[m] / gt[m]
    return {"absrel": float(np.mean(np.abs(z[m] - gt[m]) / gt[m])), "d1": float(np.mean(np.maximum(r, 1 / r) < 1.25))}


def main():
    from transformers import AutoImageProcessor, AutoModelForDepthEstimation
    from moge.model.v2 import MoGeModel
    da = {}
    for size in ("Small", "Large"):
        name = f"depth-anything/Depth-Anything-V2-{size}-hf"
        da[size] = (AutoImageProcessor.from_pretrained(name), AutoModelForDepthEstimation.from_pretrained(name).to(dev).eval())
    moge = MoGeModel.from_pretrained("Ruicheng/moge-2-vitl-normal").to(dev).eval()
    rows, shown = [], 0
    for mid, (name, pid) in CLIMBS.items():
        mesh_dir, proj = REPO / "data/meshes" / mid, REPO / "data/projects" / pid
        V, F, _ = meshclean._geometry(*meshclean._read_glb(meshclean.mesh_file(mesh_dir)))
        tm = o3d.t.geometry.TriangleMesh()
        tm.vertex.positions = o3d.core.Tensor(np.asarray(V, np.float32))
        tm.triangle.indices = o3d.core.Tensor(np.asarray(F, np.int32))
        scene = o3d.t.geometry.RaycastingScene()
        scene.add_triangles(tm)
        masks = proj / "masks"
        views = splat.views(mesh_dir, proj / "photos", masks if masks.is_dir() else None, MAX_SIDE)
        mpu = json.loads((mesh_dir / "scale.json").read_text()).get("metres_per_unit")
        t0 = time.time()
        for view in views:
            gt = gt_depth(scene, view)
            m = view["valid"] & np.isfinite(gt) & (gt > 0)
            if m.mean() < 0.02:
                continue
            img = view["image"]
            h, w = gt.shape
            row = {"mesh": mid, "climb": name, "photo": view["name"], "video": view["name"].startswith("yt_"), "covered": float(m.mean())}
            with torch.no_grad():
                for size, (proc, model) in da.items():
                    inp = proc(images=(img * 255).astype(np.uint8), return_tensors="pt").to(dev)
                    out = model(**inp).predicted_depth[None]
                    pred = torch.nn.functional.interpolate(out, (h, w), mode="bilinear", align_corners=False)[0, 0].cpu().numpy()
                    row[f"da_{size.lower()}"] = metrics(fit_disparity(pred, gt, m), gt, m)
                    if size == "Large":
                        da_large = fit_disparity(pred, gt, m)
                res = moge.infer(torch.tensor(img, device=dev).permute(2, 0, 1))
                md = res["depth"].cpu().numpy()
                mm = m & np.isfinite(md) & (md > 0) & res["mask"].cpu().numpy().astype(bool)
                if mm.sum() > 100:
                    s = float(np.median(gt[mm] / md[mm]))        # mesh units per metre, by MoGe
                    row["moge"] = {**metrics(s * md, gt, mm), "units_per_metre": s}
                    moge_z = s * md
                else:
                    moge_z = None
            flat = np.full_like(gt, 1 / np.mean(1 / gt[m]))
            row["flat"] = metrics(flat, gt, m)
            rows.append(row)
            if shown < 8 and row["covered"] > 0.15 and len([r for r in rows if r["mesh"] == mid]) % 9 == 1:
                shown += 1
                lo, hi = np.percentile(gt[m], [2, 98])
                col = lambda z: cv2.applyColorMap((np.clip((z - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8), cv2.COLORMAP_TURBO)  # noqa: E731
                tiles = [cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_RGB2BGR), col(np.where(m, gt, hi)), col(np.where(m, da_large, hi))]
                if moge_z is not None:
                    tiles.append(col(np.where(m, moge_z, hi)))
                cv2.imwrite(str(OUT / f"{name.replace(' ', '_')}_{view['name']}.jpg"), np.concatenate(tiles, 1))
        print(f"{name}: {len([r for r in rows if r['mesh'] == mid])} photos in {time.time() - t0:.0f} s, size by scale.py {mpu}", flush=True)
    (OUT / "rows.json").write_text(json.dumps(rows))

    def summary(sel, key):
        vals = [r[key] for r in sel if key in r]
        if not vals:
            return "-"
        return f"AbsRel {np.median([v['absrel'] for v in vals]):.3f}, d1 {np.mean([v['d1'] for v in vals]):.2f}"
    for mid, (name, _) in CLIMBS.items():
        sel = [r for r in rows if r["mesh"] == mid]
        print(f"\n{name} ({len(sel)} photos, {sum(r['video'] for r in sel)} video frames)")
        for key in ("flat", "da_small", "da_large", "moge"):
            print(f"  {key:9s} {summary(sel, key)}")
        for vid in (False, True):
            part = [r for r in sel if r["video"] == vid]
            print(f"  {'frames' if vid else 'photos'}: " + ", ".join(f"{k} {summary(part, k)}" for k in ("da_large", "moge")))
        ups = np.array([r["moge"]["units_per_metre"] for r in sel if "moge" in r])
        if len(ups):
            mpu = json.loads((REPO / "data/meshes" / mid / "scale.json").read_text()).get("metres_per_unit")
            print(f"  MoGe's size: {1 / np.median(ups):.3f} m per mesh unit (spread between photos: middle half "
                  f"{1 / np.percentile(ups, 75):.3f}-{1 / np.percentile(ups, 25):.3f}); scale.py's: {mpu}")
    for key in ("flat", "da_small", "da_large", "moge"):
        print(f"ALL {key:9s} {summary(rows, key)}")


if __name__ == "__main__":
    main()
