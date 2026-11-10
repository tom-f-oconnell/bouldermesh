"""Test 2: when a climb has photos from few viewpoints, do depth models help? Each reference mesh's photos are cut down
to a poor subset (8 from one side, or 10 frames of one video), and against the full mesh, as the truth, compare:

- COLMAP + OpenMVS, as the app reconstructs (3 runs, as COLMAP varies from run to run);
- that mesh's holes filled from MoGe-2's depth, fitted per photo to the mesh where it has one;
- VGGT given the same photos: where it puts the cameras, and the points from its depth.

Everything is aligned to the full mesh by a similarity fitted to the camera positions of the photos both placed. Scores,
in the rock's box only: accuracy (how far points are from the full mesh, as % of the rock's size) and completeness
(% of the full mesh's surface within 2% of the rock's size of something).

    python test2.py prepare | run | evaluate      (outputs in $DEPTHTEST_OUT, by default out/ here)

`run` uses the app's image (deploy/build.sh tags it bouldermesh:public) and the GPU, as compute.py runs jobs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))
import meshclean  # noqa: E402
import splat  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("DEPTHTEST_OUT", HERE / "out"))
JOBS = OUT / "t2"
CLIMBS = {"2cd16f494134": ("Monkey Gone to Heaven", "e58c93acf94b"), "dd872c9a20b6": ("Little Chucky", "50099afdedf8"),
          "ba864cab10ff": ("Square Dance", "0777cc139076"), "12fae5f39a3e": ("Midnight Lightning", "880e1a362d40")}
RUNS = 3
TAU = 0.02           # completeness: within 2% of the rock's size


def centre_of(c):
    return -np.array(splat.rotation(c["q"])).T @ np.array(c["t"])


def subsets(mid):
    """{name: [photo names]}: 8 from the side most photos look from, and 10 frames of the video with most placed."""
    cams = json.loads((REPO / "data/meshes" / mid / "cameras.json").read_text())
    lo, hi = splat.rock_box(REPO / "data/meshes" / mid)
    c = (lo + hi) / 2
    az = {x["name"]: np.degrees(np.arctan2(*(centre_of(x) - c)[[2, 0]])) for x in cams}      # +Y is up: around it
    best = max(range(0, 360, 5), key=lambda a: sum(abs((v - a + 180) % 360 - 180) <= 30 for v in az.values()))
    side = sorted(n for n, v in az.items() if abs((v - best + 180) % 360 - 180) <= 30)
    out = {"side": side[:: max(1, len(side) // 8)][:8]}
    tags = {}
    for x in cams:
        if x["name"].startswith("yt_"):
            tags.setdefault(x["name"].split("_")[1], []).append(x["name"])
    if tags:
        frames = sorted(max(tags.values(), key=len))
        out["video"] = frames[:: max(1, len(frames) // 10)][:10]
    return out


def prepare():
    for mid, (name, pid) in CLIMBS.items():
        proj = REPO / "data/projects" / pid
        for sub, names in subsets(mid).items():
            for run in range(1, RUNS + 1):
                job = JOBS / mid / sub / f"run{run}"
                shutil.rmtree(job, ignore_errors=True)
                (job / "photos").mkdir(parents=True)
                (job / "masks").mkdir()
                for n in names:
                    shutil.copy(proj / "photos" / n, job / "photos" / n)
                    if (proj / "masks" / f"{n}.png").exists():
                        shutil.copy(proj / "masks" / f"{n}.png", job / "masks" / f"{n}.png")
            print(f"{name} {sub}: {len(names)} photos", flush=True)


def run():
    data = OUT / "t2data"
    data.mkdir(parents=True, exist_ok=True)
    for job in sorted(JOBS.glob("*/*/run*")):
        if (job / "out" / "cameras.json").exists() or (job / "failed").exists():
            continue
        t0 = time.time()
        r = subprocess.run(["nice", "-n", "10", "docker", "run", "--rm", "--device", "nvidia.com/gpu=all", "--cpus", "12",
                            "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-v", f"{data}:/data",
                            "-v", f"{REPO / 'data/cache/models'}:/data/cache/models:ro", "-v", f"{job}:/job", "bouldermesh:public",
                            "python3", "/app/backend/remote_job.py", "/job", "--internet", "--gpu"], capture_output=True, text=True)
        (job / "log.txt").write_text(r.stdout + r.stderr)
        placed = len(json.loads((job / "out" / "cameras.json").read_text())) if (job / "out" / "cameras.json").exists() else 0
        if not placed:
            (job / "failed").write_text((r.stdout + r.stderr)[-2000:])
        print(f"{job.relative_to(JOBS)}: {placed} placed, {time.time() - t0:.0f} s", flush=True)


def umeyama(src, dst):
    """Scale s, rotation R, translation t with dst ~ s R src + t."""
    ms, md = src.mean(0), dst.mean(0)
    a, b = src - ms, dst - md
    U, S, Vt = np.linalg.svd(b.T @ a / len(src))
    D = np.eye(3)
    D[2, 2] = np.sign(np.linalg.det(U @ Vt))
    R = U @ D @ Vt
    s = np.trace(np.diag(S) @ D) / (a ** 2).sum(axis=1).mean()
    return s, R, md - s * R @ ms


def surface_points(V, F, n, rng):
    tri = V[F]
    area = np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    i = rng.choice(len(F), n, p=area / area.sum())
    u, v = rng.random((2, n))
    flip = u + v > 1
    u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
    return tri[i, 0] + u[:, None] * (tri[i, 1] - tri[i, 0]) + v[:, None] * (tri[i, 2] - tri[i, 0])


def rays_for(K, Rw, tw, h, w):
    """Camera centre and per-pixel world directions (camera z = 1) for a pinhole camera."""
    u, v = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    d_cam = np.stack([(u - K[0, 2]) / K[0, 0], (v - K[1, 2]) / K[1, 1], np.ones_like(u)], -1).reshape(-1, 3)
    return -Rw.T @ tw, d_cam @ Rw


def robust_sim3(src, dst, iters=4, keep=0.8):
    """Umeyama on corresponding points, refitted on the closest 80% a few times."""
    sel = np.arange(len(src))
    for _ in range(iters):
        s, R, t = umeyama(src[sel], dst[sel])
        r = np.linalg.norm(s * src @ R.T + t - dst, axis=1)
        sel = np.argsort(r)[: int(len(r) * keep)]
    return s, R, t


def rel_rot_err(Ra, Rb):
    """Median angle (degrees) between the rotations from each camera to each other one: no alignment needed."""
    errs = []
    for i in range(len(Ra)):
        for j in range(i + 1, len(Ra)):
            M = (Ra[j] @ Ra[i].T) @ (Rb[j] @ Rb[i].T).T
            errs.append(np.degrees(np.arccos(np.clip((np.trace(M) - 1) / 2, -1, 1))))
    return float(np.median(errs)) if errs else None


def evaluate(jobs: Path = JOBS, out_name: str = "out2.json", with_vggt: bool = True):
    """Each method's points are put in the full mesh's frame by a similarity fitted to pixel correspondences: what its
    geometry says is at each pixel of its photos, against what the full mesh says is there from the same photo (as the
    full model placed it). That's the usual way to score shape (benchmarks align to the truth); camera centres alone
    can't fix it here, as several subsets' cameras barely move. Camera placement is scored separately, without
    alignment (relative rotations between photos) and after it (position and direction)."""
    import open3d as o3d
    import torch
    from scipy.spatial import cKDTree
    from moge.model.v2 import MoGeModel
    if os.environ.get("VGGT_DIR"):         # a clone of github.com/facebookresearch/vggt, if it isn't installed
        sys.path.insert(0, os.environ["VGGT_DIR"])
    from vggt.models.vggt import VGGT
    from vggt.utils.load_fn import load_and_preprocess_images
    from vggt.utils.pose_enc import pose_encoding_to_extri_intri
    from vggt.utils.geometry import unproject_depth_map_to_point_map
    from PIL import Image
    dev, rng = "cuda", np.random.default_rng(0)
    moge = MoGeModel.from_pretrained("Ruicheng/moge-2-vitl-normal").to(dev).eval()
    vggt = VGGT.from_pretrained("facebook/VGGT-1B").to(dev).eval() if with_vggt else None
    results = []

    def mesh_scene(V, F):
        tm = o3d.t.geometry.TriangleMesh()
        tm.vertex.positions = o3d.core.Tensor(np.asarray(V, np.float32))
        tm.triangle.indices = o3d.core.Tensor(np.asarray(F, np.int32))
        sc = o3d.t.geometry.RaycastingScene()
        sc.add_triangles(tm)
        return sc

    def cast(scene, C, d):
        rays = np.concatenate([np.repeat(C[None], len(d), 0), d], 1).astype(np.float32)
        return scene.cast_rays(o3d.core.Tensor(rays))["t_hit"].numpy()

    def lens_K(lens, max_side):
        sc = min(1.0, max_side / max(lens["width"], lens["height"]))
        return splat.intrinsics(lens, sc)[2], round(lens["height"] * sc), round(lens["width"] * sc)

    for mid, (name, pid) in CLIMBS.items():
        ref_dir = REPO / "data/meshes" / mid
        rV, rF, _ = meshclean._geometry(*meshclean._read_glb(meshclean.mesh_file(ref_dir)))
        rV, rF = np.asarray(rV, float), np.asarray(rF)
        lo, hi = splat.rock_box(ref_dir)
        size = float(np.linalg.norm(hi - lo))
        inbox = lambda P: np.all((P >= lo) & (P <= hi), axis=1)  # noqa: E731
        ref_scene = mesh_scene(rV, rF)
        ref_pts = surface_points(rV, rF, 200_000, rng)
        ref_pts = ref_pts[inbox(ref_pts)]
        ref_cams = {c["name"]: c for c in json.loads((ref_dir / "cameras.json").read_text())}
        ref_R = {n: np.array(splat.rotation(c["q"])) for n, c in ref_cams.items()}

        def ref_hits(n, K, h, w):
            """The full mesh's world points along each pixel of photo n (K for an h x w image of it); NaN for misses."""
            C, d = rays_for(K, ref_R[n], np.array(ref_cams[n]["t"]), h, w)
            t = cast(ref_scene, C, d)
            return np.where(np.isfinite(t)[:, None], C + d * t[:, None], np.nan)

        def seen_by(names):
            """Which of the full mesh's points at least one of these photos sees (from where the full model put it)."""
            seen = np.zeros(len(ref_pts), bool)
            for n in names:
                c = ref_cams.get(n)
                if not c or not c.get("lens"):
                    continue
                K = splat.intrinsics(c["lens"], 1.0)[0]
                Rw, tw = ref_R[n], np.array(c["t"])
                Xc = ref_pts @ Rw.T + tw
                z = np.maximum(Xc[:, 2], 1e-9)
                u, v = Xc[:, 0] / z * K[0, 0] + K[0, 2], Xc[:, 1] / z * K[1, 1] + K[1, 2]
                inside = np.flatnonzero((Xc[:, 2] > 0) & (u >= 0) & (u < c["lens"]["width"]) & (v >= 0) & (v < c["lens"]["height"]))
                C = -Rw.T @ tw
                dd = ref_pts[inside] - C
                dist = np.linalg.norm(dd, axis=1)
                hit = cast(ref_scene, C, dd / dist[:, None])
                seen[inside[hit > dist - 0.005 * size]] = True
            return seen

        def score(P, seen=None):
            """Accuracy (median and 90th percentile distance to the full mesh, % of the rock's size) and completeness, of
            all the full mesh and of the part the photos see."""
            P = P[inbox(P)]
            if len(P) < 100:
                return {"points": int(len(P))}
            d = ref_scene.compute_distance(o3d.core.Tensor(P.astype(np.float32))).numpy()
            near = cKDTree(P).query(ref_pts, k=1)[0] < TAU * size
            out = {"points": int(len(P)), "acc_med": float(np.median(d) / size * 100), "acc_90": float(np.percentile(d, 90) / size * 100),
                   "complete": float(np.mean(near) * 100)}
            if seen is not None and seen.any():
                out["complete_seen"] = float(np.mean(near[seen]) * 100)
            return out

        def cam_scores(names, Rs, Cs, sim):
            """Camera placement: relative rotations (no alignment), and after the similarity, position (% of the rock's
            size) and direction errors."""
            s, R, t = sim
            pos = [np.linalg.norm(s * R @ c + t - centre_of(ref_cams[n])) / size * 100 for n, c in zip(names, Cs)]
            ang = [np.degrees(np.arccos(np.clip((np.trace((Rv @ R.T) @ ref_R[n].T) - 1) / 2, -1, 1))) for n, Rv in zip(names, Rs)]
            return {"rel_rot": rel_rot_err(Rs, [ref_R[n] for n in names]), "pos_err": float(np.median(pos)), "rot_err": float(np.median(ang))}

        for sub_dir in sorted((jobs / mid).iterdir()) if (jobs / mid).is_dir() else []:
            sub = sub_dir.name
            names = sorted(p.name for p in (sub_dir / "run1" / "photos").iterdir())
            seen = seen_by(names)
            row = {"climb": name, "subset": sub, "photos": len(names), "seen": float(seen.mean() * 100), "colmap": [], "filled": []}
            for job in sorted(sub_dir.glob("run*")):
                out = job / "out"
                if not (out / "cameras.json").exists():
                    row["colmap"].append({"placed": 0})
                    continue
                cams = [c for c in json.loads((out / "cameras.json").read_text()) if c["name"] in ref_cams and c.get("lens")]
                mf = meshclean.mesh_file(out)
                V, F, _ = meshclean._geometry(*meshclean._read_glb(mf))
                V, F = np.asarray(V, float), np.asarray(F)
                sub_scene = mesh_scene(V, F)
                src, dst = [], []
                for c in cams:
                    K, h, w = lens_K(c["lens"], 256)
                    C, d = rays_for(K, np.array(splat.rotation(c["q"])), np.array(c["t"]), h, w)
                    ts = cast(sub_scene, C, d)
                    rp = ref_hits(c["name"], lens_K(ref_cams[c["name"]]["lens"], 256)[0], h, w)
                    ok = np.isfinite(ts) & np.isfinite(rp[:, 0])
                    src.append((C + d * ts[:, None])[ok])
                    dst.append(rp[ok])
                src, dst = np.concatenate(src), np.concatenate(dst)
                if len(src) < 500:
                    row["colmap"].append({"placed": len(cams), "aligned": False, "correspondences": int(len(src))})
                    continue
                pick = rng.choice(len(src), min(len(src), 60_000), replace=False)
                sim = robust_sim3(src[pick], dst[pick])
                s, R, t = sim
                to_ref = lambda P: s * P @ R.T + t  # noqa: E731
                mesh_pts = to_ref(surface_points(V, F, 200_000, rng))
                row["colmap"].append({"placed": len(cams), **cam_scores([c["name"] for c in cams], [np.array(splat.rotation(c["q"])) for c in cams],
                                                                       [centre_of(c) for c in cams], sim), **score(mesh_pts, seen)})
                # holes filled from MoGe's depth: per placed photo, where the subset mesh has no surface
                filled = []
                tmp = job / "_cams"
                tmp.mkdir(exist_ok=True)
                (tmp / "cameras.json").write_text(json.dumps(cams))
                for view in splat.views(tmp, job / "photos", job / "masks", 512):
                    h, w = view["valid"].shape
                    C, d_world = rays_for(view["K"], view["viewmat"][:3, :3], view["viewmat"][:3, 3], h, w)
                    z = cast(sub_scene, C, d_world).reshape(h, w)
                    with torch.no_grad():
                        res = moge.infer(torch.tensor(view["image"], device=dev).permute(2, 0, 1))
                    md, mm = res["depth"].cpu().numpy(), res["mask"].cpu().numpy().astype(bool) & view["valid"]
                    have = mm & np.isfinite(z) & (md > 0)
                    if have.sum() < 200:
                        continue
                    sc = float(np.median(z[have] / md[have]))
                    hole = mm & ~np.isfinite(z) & (md > 0)
                    pts = (C + (d_world.reshape(h, w, 3) * (sc * md)[..., None]))[hole]
                    filled.append(pts[rng.choice(len(pts), min(len(pts), 20_000), replace=False)] if len(pts) else pts)
                fill = to_ref(np.concatenate(filled)) if filled else np.zeros((0, 3))
                row["filled"].append({"fill": score(fill, seen), "mesh_and_fill": score(np.concatenate([mesh_pts, fill]), seen)})
            if not with_vggt:
                results.append(row)
                print(json.dumps(row), flush=True)
                continue
            # VGGT on the same photos
            paths = [str(sub_dir / "run1" / "photos" / n) for n in names]
            imgs = load_and_preprocess_images(paths).to(dev)
            with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
                pred = vggt(imgs)
            ext, intr = pose_encoding_to_extri_intri(pred["pose_enc"], imgs.shape[-2:])
            ext, intr = ext[0].float().cpu().numpy(), intr[0].float().cpu().numpy()
            depth, conf = pred["depth"][0].float().cpu().numpy(), pred["depth_conf"][0].float().cpu().numpy()
            P = unproject_depth_map_to_point_map(depth, ext, intr)
            keep = conf > np.percentile(conf, 50)
            H, W = imgs.shape[-2:]
            src, dst = [], []
            for i, (n, path) in enumerate(zip(names, paths)):
                W0, H0 = Image.open(path).size
                nh = round(H0 * (518 / W0) / 14) * 14
                crop = (nh - 518) // 2 if nh > 518 else 0
                pad = (H - min(nh, 518)) // 2
                lens = ref_cams[n]["lens"]
                K = splat.intrinsics(lens, 1.0)[0]
                K = np.diag([518 / lens["width"], nh / lens["height"], 1.0]) @ K
                K[1, 2] += pad - crop
                rp = ref_hits(n, K, H, W)
                ok = keep[i].reshape(-1) & np.isfinite(rp[:, 0])
                src.append(P[i].reshape(-1, 3)[ok])
                dst.append(rp[ok])
            src, dst = np.concatenate(src), np.concatenate(dst)
            pick = rng.choice(len(src), min(len(src), 60_000), replace=False)
            sim = robust_sim3(src[pick], dst[pick])
            s, R, t = sim
            Rs = [e[:3, :3] for e in ext]
            row["vggt"] = {**cam_scores(names, Rs, [-e[:3, :3].T @ e[:3, 3] for e in ext], sim),
                           **score(s * P[keep].reshape(-1, 3) @ R.T + t, seen)}
            results.append(row)
            print(json.dumps(row), flush=True)
    (OUT / out_name).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    {"prepare": prepare, "run": run, "evaluate": evaluate}[sys.argv[1]]()
