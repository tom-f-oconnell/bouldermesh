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
- **Test 3, VGGT's placements → COLMAP triangulation and bundle adjustment → the app's OpenMVS steps** (one run each):

  | subset | COLMAP alone (test 2) | test 3 |
  |---|---|---|
  | Monkey, side | nothing placed | all 8, but rotations drifted to 15° off; 0.79%, 29% covered |
  | Monkey, video | broken | **0.31%, 79% covered**, rotations 1° off |
  | Little Chucky, side | 0.10%, 70% | 0.19%, 69% |
  | Little Chucky, video (still camera) | 0.8-0.9%, 33-60% | 0.75%, 62% |
  | Square Dance, side | nothing, or wrong | placed well, but too few matched pairs for OpenMVS to densify: no mesh |
  | Square Dance, video | 0.2%, 52-57% | 0.23%, 53% |
  | Midnight Lightning, side | 6 of 8 placed, 0.2%, 64-90% | all 8, 0.29%, 84% |
  | Midnight Lightning, video | 0.6-1.5%, 23-67%, inconsistent | **0.33%, 66%** |

  As a fallback when COLMAP's mapper fails, this rescues some climbs. Where COLMAP works, it's about as good. VGGT-1B's
  weights are licensed for non-commercial use only.

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
    ./venv/bin/python test3.py prepare && ./venv/bin/python test3.py run && ./venv/bin/python test3.py evaluate

Outputs go to `out/` here (ignored by git), or to `$DEPTHTEST_OUT`. The models download to the Hugging Face cache
(about 7 GB; `HF_HOME` moves it). Test 2's runs take about 4 minutes each (24 runs), test 3's about 2 (8 runs).
