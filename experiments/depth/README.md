# Depth models on poorly covered climbs

These tests ask whether pretrained single-image depth models (Depth Anything V2, MoGe-2) or VGGT can help with climbs
whose photos come from few viewpoints. They use climbs that already have good meshes as the truth. They're experiments
for deciding what to build, and nothing in the app uses them.

## What they found (2026-11-10)

The climbs were Monkey Gone to Heaven, Little Chucky, Square Dance and Midnight Lightning. Errors are given as a % of the
rock's size. "Covered" is the share of the surface the subset's photos see that ended up within 2% of the full mesh.

- **Test 1, single-image depth against each photo's view of the full mesh** (362 photos; AbsRel, and δ1 = share of pixels
  within 25%):

  | method | AbsRel | δ1 |
  |---|---|---|
  | one flat depth (baseline) | 0.21 | 64% |
  | Depth Anything V2 Small | 0.109 | 86% |
  | Depth Anything V2 Large | 0.096 | 90% |
  | MoGe-2 | 0.094 | 87% |

  MoGe-2's metres came out at 0.6-0.93x scale.py's sizes, varying ±20-30% from photo to photo, so it isn't a way to size
  a rock.
- **Test 2, poor subsets** (8 photos from one side, or 10 frames of one video; COLMAP 3 times each, as the app runs it):
  - When COLMAP works, it's the most accurate (0.1-0.2%).
  - It placed nothing for Monkey's side subset, and for Square Dance's in 2 of 3 runs; the third run was wrong. Its runs
    on Monkey's video were broken (relative rotations 12-18° off).
  - VGGT placed every subset (relative rotations 3-6° off), with its depth points 0.4-1% off.
  - MoGe-2 filling the holes of a good subset mesh took it from 64-90% to 97-99% covered. The filled part is about 10x
    less accurate (1.3-2% median, 4-5% at the 90th percentile). On a bad mesh, filling made it worse.
  - Videos whose camera barely moves give no real 3D with any method.
- **Test 3, VGGT's placements → COLMAP triangulation and bundle adjustment → the app's OpenMVS steps** (one run each).
  Errors are the median, as a % of the rock's size, then the share of what the photos see that's covered:

  | subset | COLMAP alone (test 2) | VGGT + COLMAP (the app's fallback) | focal lengths kept 2 rounds | VGGT's own tracks |
  |---|---|---|---|---|
  | Monkey, side | nothing placed | 15° off; 0.79%, 29% | 11° off; 0.48%, 40% | **3° off; 0.29%, 27%** |
  | Monkey, video | broken | **0.31%, 79%** | 0.32%, 80% | 0.99%, 50% |
  | Little Chucky, side | 0.10%, 70% | 0.19%, 69% | 0.18%, 69% | 16° off; 0.33%, 57% |
  | Little Chucky, video (still camera) | 0.8-0.9%, 33-60% | 0.75%, 62% | 1.15%, 49% | 0.72%, 52% |
  | Square Dance, side | nothing, or wrong | no mesh: too few matched pairs to densify | 31° off; 1.28%, 16% | no mesh |
  | Square Dance, video | 0.2%, 52-57% | 0.23%, 53% | 0.65%, 31% | 0.30%, 40% |
  | Midnight Lightning, side | 6 of 8 placed, 0.2%, 64-90% | all 8; 0.29%, 84% | 0.34%, 84% | 0.14%, 74% |
  | Midnight Lightning, video | 0.6-1.5%, 23-67%, inconsistent | **0.33%, 66%** | 0.46%, 66% | 1.31%, 52% |

  The columns after COLMAP alone:
  - **VGGT + COLMAP:** VGGT's cameras, then COLMAP triangulating its own SIFT matches from them and adjusting
    everything, over three tightening rounds. As a fallback when COLMAP's mapper fails, it rescues some climbs, and
    where COLMAP works it's about as good. It's now the app's fallback (`backend/vggt_place.py`).
  - **Focal lengths kept:** VGGT's focal lengths held for the first two rounds, then refined. Kept through all three, it
    was worse still on most. With few photos from close viewpoints, a longer lens farther away looks much like a
    shorter one closer in, and refining can slide into a wrong answer that still fits the matches. Holding VGGT's
    helped Monkey's side photos, but cost wherever VGGT's own focal length was off.
  - **VGGT's own tracks:** VGGT's route to a model, with no COLMAP matches (`vggt_ba.py`, after VGGT's
    `demo_colmap.py --use_ba`). VGGSfM's tracker follows ALIKED and SuperPoint keypoints across the photos, then
    pycolmap adjusts. It placed Monkey's side photos best, but covered less everywhere, and was worse on four others.

  VGGT-1B's weights are licensed for non-commercial use only.

How the depth models could guide the viewer's scene splats, where few photos look, and how to test that, is in
[design.md](../../design.md#depth-as-a-guide-where-few-photos-look-not-built). That hasn't been run yet.

Shapes are compared after a similarity fitted to pixel correspondences between each method's geometry and the full
mesh, as benchmarks align to the truth. Camera centres alone can't fix that similarity when a video's camera barely
moves. Because of this, the shape scores flatter a broken reconstruction a little. Camera placement is scored separately.

## Running them

They need an NVIDIA GPU. Test 2 and 3 run the app's own reconstruction in its image (`deploy/build.sh` tags
`bouldermesh:public`), and read reference meshes and photos from `data/` (the climbs are hard-coded in `test1.py` and
`test2.py`).

    uv venv venv --python 3.12
    VIRTUAL_ENV=venv uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130
    VIRTUAL_ENV=venv uv pip install open3d scipy opencv-python-headless pillow transformers==4.49.0 \
        "git+https://github.com/microsoft/MoGe.git" einops safetensors huggingface_hub
    VIRTUAL_ENV=venv uv pip install --no-deps "git+https://github.com/facebookresearch/vggt"

    ./venv/bin/python test1.py
    ./venv/bin/python test2.py prepare && ./venv/bin/python test2.py run && ./venv/bin/python test2.py evaluate
    for v in base fixf fixf2 vggtba; do
        ./venv/bin/python test3.py prepare $v && ./venv/bin/python test3.py run $v && ./venv/bin/python test3.py evaluate $v
    done

The `vggtba` variant needs more besides: `VIRTUAL_ENV=venv uv pip install pycolmap==3.10.0 trimesh hydra-core omegaconf
"lightglue @ git+https://github.com/cvg/LightGlue"`. VGGT's code is written for that pycolmap, and later ones fail.

Outputs go to `out/` here (ignored by git), or to `$DEPTHTEST_OUT`. The models download to the Hugging Face cache
(about 7 GB; `HF_HOME` moves it). Test 2's runs take about 4 minutes each (24 runs), test 3's about 2 (8 runs).

## Queued for when videos are back

These wait for YouTube video again, as their questions need fresh frames, and climbs that failed for want of them:
- **The fallback against COLMAP alone, on real failures.** Mesh the climbs COLMAP failed or skipped (The Tramway's,
  say) from fresh frames, with and without the fallback, 3 runs each. Count meshes made, look at them, and check how
  well their cameras agree between runs.
- **More reference climbs.** Repeat tests 2 and 3 on 6-8 climbs with good meshes and new video subsets, 3 runs each:
  COLMAP alone, the fallback (`base`), focal lengths kept 2 rounds (`fixf2`), VGGT's own tracks (`vggtba`), VGGT even
  where the mapper works, and VGGT's tracks added to COLMAP's SIFT matches (not built yet).
- **More frames for VGGT.** It doesn't need photos far apart, so give it 30, 60 or 80 frames of the same videos.
- **Settling a new default pipeline.** Run these alongside the frame-selection test, so frames and placement are chosen
  together.
