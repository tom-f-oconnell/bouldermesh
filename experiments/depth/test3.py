"""Test 3: VGGT's camera placements, refined by COLMAP and meshed by the app's own pipeline. In test 2, COLMAP couldn't
place the photos of some poor subsets (its mapper has to start from a pair of photos seen from far enough apart), while
VGGT placed every one, a few degrees off. Here, for each of test 2's subsets:

1. VGGT places the photos: positions, directions and focal lengths.
2. COLMAP triangulates test 2's verified matches from those placements. It then refines placements, points and lenses
   together (bundle adjustment), tightening its tolerances over three rounds as the placements improve.
3. The app's pipeline carries on from there, as it would after COLMAP's own mapper: levelling, then OpenMVS's
   densifying, meshing and texturing.

It's then scored like test 2 (test2.evaluate), against the same full meshes.

    python test3.py prepare | run | evaluate      (after test2.py prepare and run; outputs in $DEPTHTEST_OUT)
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

JOBS = OUT / "t3"
# rounds of (largest angle error in degrees, largest reprojection error in pixels) for triangulating and keeping points
ROUNDS = ((8, 150), (3, 20), (2, 4))


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
    model to start from."""
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
  a=${{r%,*}}; e=${{r#*,}}
  mkdir -p tri$e ba$e
  echo "== round: angle error $a degrees, reprojection error $e px"
  colmap point_triangulator --database_path database.db --image_path /job/photos --input_path $model --output_path tri$e \
    --Mapper.tri_create_max_angle_error $a --Mapper.tri_continue_max_angle_error $a --Mapper.tri_re_max_angle_error $a \
    --Mapper.tri_merge_max_reproj_error $e --Mapper.tri_complete_max_reproj_error $e --Mapper.filter_max_reproj_error $e \
    --Mapper.tri_ignore_two_view_tracks 0 --Mapper.tri_min_angle 1 --Mapper.filter_min_tri_angle 1 > tri$e.log 2>&1
  colmap bundle_adjuster --input_path tri$e --output_path ba$e > ba$e.log 2>&1
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
    rounds = " ".join(f"{a},{e}" for a, e in ROUNDS)
    for job in sorted(JOBS.glob("*/*/run*")):
        if (job / "out" / "cameras.json").exists() or (job / "failed").exists():
            continue
        t0 = time.time()
        uid = f"{os.getuid()}:{os.getgid()}"
        r = subprocess.run(["nice", "-n", "10", "docker", "run", "--rm", "--device", "nvidia.com/gpu=all", "--cpus", "12",
                            "--user", uid, "-e", "HOME=/tmp", "-v", f"{data}:/data",
                            "-v", f"{Path(__file__).resolve().parents[2] / 'data/cache/models'}:/data/cache/models:ro",
                            "-v", f"{job}:/job", "bouldermesh:public", "sh", "-c", SCRIPT.format(rounds=rounds)],
                           capture_output=True, text=True)
        (job / "log.txt").write_text(r.stdout + r.stderr)
        placed = len(json.loads((job / "out" / "cameras.json").read_text())) if (job / "out" / "cameras.json").exists() else 0
        if not placed:
            (job / "failed").write_text((r.stdout + r.stderr)[-2000:])
        rounds_seen = [ln.split("]")[-1].strip() for ln in r.stdout.splitlines() if any(k in ln for k in ("Registered images", "Points:", "Mean reprojection"))]
        print(f"{job.relative_to(JOBS)}: {placed} placed, {time.time() - t0:.0f} s; {' | '.join(rounds_seen)}", flush=True)


def evaluate():
    import test2
    test2.evaluate(JOBS, "out3.json", with_vggt=False)


if __name__ == "__main__":
    {"prepare": prepare, "run": run, "evaluate": evaluate}[sys.argv[1]]()
