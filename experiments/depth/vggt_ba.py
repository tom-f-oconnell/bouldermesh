"""VGGT's own way to a COLMAP model (its demo_colmap.py with --use_ba), for test3.py's "vggtba" variant: VGGT's cameras
and depth, points tracked across the photos by VGGSfM's tracker from ALIKED and SuperPoint keypoints, then pycolmap's
bundle adjustment. It doesn't use COLMAP's SIFT matches at all, so it can link photos SIFT couldn't.

    python vggt_ba.py <photos dir> <model dir>      (VGGT_DIR: a clone of github.com/facebookresearch/vggt)

Needs, besides test3's environment: pycolmap, trimesh, and LightGlue (git+https://github.com/cvg/LightGlue). The
tracker's weights and DINOv2 download the first time.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")    # 10 full-size frames run out without
import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402

if os.environ.get("VGGT_DIR"):
    sys.path.insert(0, os.environ["VGGT_DIR"])
from vggt.dependency.np_to_pycolmap import batch_np_matrix_to_pycolmap  # noqa: E402
from vggt.dependency.track_predict import predict_tracks  # noqa: E402
from vggt.models.vggt import VGGT  # noqa: E402
from vggt.utils.geometry import unproject_depth_map_to_point_map  # noqa: E402
from vggt.utils.load_fn import load_and_preprocess_images_square  # noqa: E402
from vggt.utils.pose_enc import pose_encoding_to_extri_intri  # noqa: E402

LOAD, RES = 1024, 518       # photos loaded square at 1024 for tracking, given to VGGT at 518, as the demo does


def main(photos: Path, out: Path, max_reproj_error: float = 8.0, vis_thresh: float = 0.2, query_frames: int = 8,
         max_query_pts: int = 4096) -> None:
    np.random.seed(42)
    torch.manual_seed(42)
    dtype = torch.bfloat16 if torch.cuda.get_device_capability()[0] >= 8 else torch.float16
    model = VGGT.from_pretrained("facebook/VGGT-1B", enable_point=False, enable_track=False).eval().to("cuda")
    paths = sorted(str(p) for p in photos.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    images, coords = load_and_preprocess_images_square(paths, LOAD)
    images, coords = images.to("cuda"), coords.to("cuda")
    small = F.interpolate(images, size=(RES, RES), mode="bilinear", align_corners=False)[None]
    with torch.no_grad(), torch.autocast("cuda", dtype=dtype):
        tokens, ps_idx = model.aggregator(small)
        pose_enc = model.camera_head(tokens)[-1]
        ext, intr = pose_encoding_to_extri_intri(pose_enc, small.shape[-2:])
        depth, conf = model.depth_head(tokens, small, ps_idx)
    ext, intr = ext[0].float().cpu().numpy(), intr[0].float().cpu().numpy()
    depth, conf = depth[0].float().cpu().numpy(), conf[0].float().cpu().numpy()
    points = unproject_depth_map_to_point_map(depth, ext, intr)
    del model
    torch.cuda.empty_cache()
    with torch.no_grad(), torch.autocast("cuda", dtype=dtype):
        tracks, vis, _, points, rgb = predict_tracks(images, conf=conf, points_3d=points, masks=None, max_query_pts=max_query_pts,
                                                     query_frame_num=min(query_frames, len(paths)), keypoint_extractor="aliked+sp",
                                                     fine_tracking=True)
    intr[:, :2, :] *= LOAD / RES
    recon, _ = batch_np_matrix_to_pycolmap(points, ext, intr, tracks, np.array(images.shape[-2:]), masks=vis > vis_thresh,
                                           max_reproj_error=max_reproj_error, shared_camera=False, camera_type="PINHOLE",
                                           points_rgb=rgb)
    if recon is None:
        raise SystemExit("no model could be built from the tracks")
    import pycolmap
    pycolmap.bundle_adjustment(recon, pycolmap.BundleAdjustmentOptions())
    coords = coords.cpu().numpy()
    for image_id in recon.images:      # back to each photo's own name, size and pixels (PINHOLE, as OpenMVS takes no other)
        image = recon.images[image_id]
        camera = recon.cameras[image.camera_id]
        size = coords[image_id - 1, -2:]
        ratio = max(size) / LOAD
        params = np.array(camera.params) * ratio
        params[-2:] = size / 2
        camera.params, camera.width, camera.height = params, int(size[0]), int(size[1])
        image.name = Path(paths[image_id - 1]).name
        for p in image.points2D:
            p.xy = (p.xy - coords[image_id - 1, :2]) * ratio
    out.mkdir(parents=True, exist_ok=True)
    recon.write(str(out))
    print(f"{recon.num_reg_images()} photos, {recon.num_points3D()} points", flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
