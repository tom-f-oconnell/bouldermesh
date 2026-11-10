"""Test 3: VGGT's camera placements, refined by COLMAP and meshed by the app's own pipeline. In test 2, COLMAP couldn't
place the photos of some poor subsets (its mapper has to start from a pair of photos seen from far enough apart), while
VGGT placed every one, a few degrees off. Here, for each of test 2's subsets:

1. VGGT places the photos: positions, directions and focal lengths.
2. COLMAP triangulates test 2's verified matches from those placements. It then refines placements, points and lenses
   together (bundle adjustment), tightening its tolerances over three rounds as the placements improve.
3. The app's pipeline carries on from there, as it would after COLMAP's own mapper: levelling, then OpenMVS's
   densifying, meshing and texturing.

It's then scored like test 2 (test2.evaluate), against the same full meshes.

Variants of step 2, each with jobs and scores of its own (t3-<variant>, out3-<variant>.json; "base" is t3, out3.json):
- base: focal lengths refined in every round, from test 2's SIFT matches.
- fixf: VGGT's focal lengths kept throughout. With few photos from close viewpoints, a longer lens farther away looks
  much like a shorter one closer in, and refining them can slide into a wrong answer that still fits the matches.
- fixf2: VGGT's focal lengths kept for the first two rounds, refined in the last.
- vggtba: none of that, nor COLMAP's matches: VGGT's own way to a model (vggt_ba.py), with points tracked across the
  photos by VGGSfM's tracker, then pycolmap's bundle adjustment. The app's pipeline carries on from its model.

    python test3.py prepare | run | evaluate [variant]     (after test2.py prepare and run; outputs in $DEPTHTEST_OUT)
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test2 import OUT, JOBS as T2  # noqa: E402

VARIANT = sys.argv[2] if len(sys.argv) > 2 else "base"
JOBS = OUT / ("t3" if VARIANT == "base" else f"t3-{VARIANT}")
# rounds of (largest angle error in degrees, largest reprojection error in pixels, whether focal lengths are refined)
# for triangulating, keeping points and adjusting
ROUNDS = {"base": ((8, 150, 1), (3, 20, 1), (2, 4, 1)),
          "fixf": ((8, 150, 0), (3, 20, 0), (2, 4, 0)),
          "fixf2": ((8, 150, 0), (3, 20, 0), (2, 4, 1)),
          "vggtba": ()}[VARIANT]


def quaternion(R):
    """COLMAP's w, x, y, z for a rotation matrix."""
    from scipy.spatial.transform import Rotation
    x, y, z, w = Rotation.from_matrix(R).as_quat()
    return (w, x, y, z) if w >= 0 else (-w, -x, -y, -z)


def vggt_placements(model, paths):
    """{photo name: (world-to-camera R, t, focal length in the photo's own pixels)} from VGGT."""
    import torch
    from PIL import Image
    from vggt.utils.load_fn import load_and_preprocess_images
    from vggt.utils.pose_enc import pose_encoding_to_extri_intri
    imgs = load_and_preprocess_images([str(p) for p in paths]).to("cuda")
    with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
        pred = model(imgs)
    ext, intr = pose_encoding_to_extri_intri(pred["pose_enc"], imgs.shape[-2:])
    ext, intr = ext[0].float().cpu().numpy(), intr[0].float().cpu().numpy()
    out = {}
    for p, e, K in zip(paths, ext, intr):
        W0, H0 = Image.open(p).size
        nh = round(H0 * (518 / W0) / 14) * 14      # VGGT's resizing: 518 wide, then cropped or padded in height
        f = (K[0, 0] * W0 / 518 + K[1, 1] * H0 / nh) / 2
        out[p.name] = (e[:3, :3], e[:3, 3], float(f))
    return out


def prepare():
    """Each of test 2's subsets as a job whose matching is done (test 2's run 1), with VGGT's placements as a COLMAP
    model to start from. Variants start from copies of base's."""
    if VARIANT != "base":
        base = OUT / "t3"
        for src in sorted(base.glob("*/*/run1")):
            job = JOBS / src.relative_to(base)
            shutil.rmtree(job, ignore_errors=True)
            job.mkdir(parents=True)
            for d in ("photos", "masks"):
                shutil.copytree(src / d, job / d)
            (job / "work").mkdir()
            shutil.copytree(src / "work" / "vggt", job / "work" / "vggt")
            shutil.copytree(src / "work" / "finished", job / "work" / "finished", ignore=shutil.ignore_patterns(
                "mapper", "level", "undistort", "people-undistorted", "import", "dense", "mesh"))
            shutil.copy(src / "work" / "database.db", job / "work" / "database.db")
            if VARIANT == "vggtba":      # a process each, as its models hold on to GPU memory
                r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "vggt_ba.py"), str(job / "photos"),
                                    str(job / "work" / "sparse" / "0")], capture_output=True, text=True)
                print(r.stdout.strip().splitlines()[-1] if r.returncode == 0 else f"failed: {r.stderr[-300:]}", flush=True)
                if r.returncode == 0:
                    (job / "work" / "finished" / "mapper").touch()
                else:
                    (job / "failed").write_text(r.stderr[-2000:])
            print(f"{job.relative_to(JOBS)}: from base", flush=True)
        return
    import torch
    if os.environ.get("VGGT_DIR"):
        sys.path.insert(0, os.environ["VGGT_DIR"])
    from vggt.models.vggt import VGGT
    model = VGGT.from_pretrained("facebook/VGGT-1B").to("cuda").eval()
    for src in sorted(T2.glob("*/*/run1")):
        if not (src / "work" / "database.db").exists():
            continue
        job = JOBS / src.relative_to(T2)
        shutil.rmtree(job, ignore_errors=True)
        (job / "work" / "finished").mkdir(parents=True)
        for d in ("photos", "masks"):
            shutil.copytree(src / d, job / d)
        shutil.copy(src / "work" / "database.db", job / "work" / "database.db")
        for step in ("people", "features", "matching"):
            (job / "work" / "finished" / step).touch()
        db = sqlite3.connect(job / "work" / "database.db")
        images = db.execute("select image_id, name, camera_id from images").fetchall()
        sizes = dict((c, (w, h)) for c, w, h in db.execute("select camera_id, width, height from cameras"))
        db.close()
        placed = vggt_placements(model, [job / "photos" / n for _, n, _ in images])
        m = job / "work" / "vggt"
        m.mkdir()
        cams, ims = [], []
        for image_id, name, cam in images:
            R, t, f = placed[name]
            w, h = sizes[cam]
            cams.append(f"{cam} SIMPLE_RADIAL {w} {h} {f:.4f} {w / 2} {h / 2} 0")
            ims.append(f"{image_id} {' '.join(f'{x:.12f}' for x in quaternion(R))} {' '.join(f'{x:.12f}' for x in t)} {cam} {name}\n")
        (m / "cameras.txt").write_text("\n".join(cams) + "\n")
        (m / "images.txt").write_text("\n".join(ims) + "\n")
        (m / "points3D.txt").write_text("")
        print(f"{job.relative_to(JOBS)}: {len(images)} photos placed by VGGT", flush=True)


SCRIPT = r"""
set -e
cd /job/work
model=vggt
for r in {rounds}; do
  a=${{r%%,*}}; rest=${{r#*,}}; e=${{rest%%,*}}; f=${{rest#*,}}
  mkdir -p tri$e ba$e
  echo "== round: angle error $a degrees, reprojection error $e px, focal lengths refined: $f"
  colmap point_triangulator --database_path database.db --image_path /job/photos --input_path $model --output_path tri$e \
    --Mapper.tri_create_max_angle_error $a --Mapper.tri_continue_max_angle_error $a --Mapper.tri_re_max_angle_error $a \
    --Mapper.tri_merge_max_reproj_error $e --Mapper.tri_complete_max_reproj_error $e --Mapper.filter_max_reproj_error $e \
    --Mapper.tri_ignore_two_view_tracks 0 --Mapper.tri_min_angle 1 --Mapper.filter_min_tri_angle 1 > tri$e.log 2>&1
  colmap bundle_adjuster --input_path tri$e --output_path ba$e --BundleAdjustment.refine_focal_length $f > ba$e.log 2>&1
  colmap model_analyzer --path ba$e 2>&1 | grep -E "Registered images|Points:|Mean reprojection error" || true
  model=ba$e
done
mkdir -p sparse/0
cp $model/* sparse/0/
touch finished/mapper
python3 /app/backend/remote_job.py /job --internet --gpu
"""


def run():
    data = OUT / "t2data"
    data.mkdir(parents=True, exist_ok=True)
    rounds = " ".join(f"{a},{e},{f}" for a, e, f in ROUNDS)
    for job in sorted(JOBS.glob("*/*/run*")):
        if (job / "out" / "cameras.json").exists() or (job / "failed").exists():
            continue
        t0 = time.time()
        uid = f"{os.getuid()}:{os.getgid()}"
        r = subprocess.run(["nice", "-n", "10", "docker", "run", "--rm", "--device", "nvidia.com/gpu=all", "--cpus", "12",
                            "--user", uid, "-e", "HOME=/tmp", "-v", f"{data}:/data",
                            "-v", f"{Path(__file__).resolve().parents[2] / 'data/cache/models'}:/data/cache/models:ro",
                            "-v", f"{job}:/job", "bouldermesh:public", "sh", "-c",
                            SCRIPT.format(rounds=rounds) if ROUNDS else "python3 /app/backend/remote_job.py /job --internet --gpu"],
                           capture_output=True, text=True)
        (job / "log.txt").write_text(r.stdout + r.stderr)
        placed = len(json.loads((job / "out" / "cameras.json").read_text())) if (job / "out" / "cameras.json").exists() else 0
        if not placed:
            (job / "failed").write_text((r.stdout + r.stderr)[-2000:])
        rounds_seen = [ln.split("]")[-1].strip() for ln in r.stdout.splitlines() if any(k in ln for k in ("Registered images", "Points:", "Mean reprojection"))]
        print(f"{job.relative_to(JOBS)}: {placed} placed, {time.time() - t0:.0f} s; {' | '.join(rounds_seen)}", flush=True)


def evaluate():
    import test2
    test2.evaluate(JOBS, "out3.json" if VARIANT == "base" else f"out3-{VARIANT}.json", with_vggt=False)


if __name__ == "__main__":
    {"prepare": prepare, "run": run, "evaluate": evaluate}[sys.argv[1]]()
