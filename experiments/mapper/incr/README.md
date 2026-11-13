# An incremental mapper: building a boulder and adding photos to it are one call

An experiment beside the mapper benchmark (`..`): a stateful reconstruction on pycolmap 4.2.1, where
`add(state, photos)` builds a boulder's model and mesh when the state is empty and adds the photos to it otherwise,
doing only the new work (features of new photos, matches of new pairs), on the GPU where that pays, with VGGT's
cameras as the start of a model and as the fallback for photos COLMAP can't register. Not in the app: shipping it
changes which photos are placed, so `MESH_RULES` would have to go up, which needs the user's OK.

## Running it

Everything bulky lives on /data (`BENCH`, default /data/tom/mapperbench); each step runs in its own container,
timed, through the benchmark's `bench.launch` (which refuses a tool tree `verify_build.py` hasn't passed).

```
./build-env.sh                                        # the tool tree, build-incr-full (2-3 h at 6 cores), checked
python3 boulder.py add <state dir> <photos or folder> [--masks dir] [--init auto|vggt|global|incremental|poses]
python3 boulder.py status <state dir>
python3 boulder.py mesh <state dir>                   # the mesh again, of the model as it is
```

For the tests and the benchmark (states under `<BENCH>/incr/r/<boulder>/<run>`):

```
python3 boulder.py first <boulder> <init> <seed> [--cond prod]                  every photo, from nothing
python3 boulder.py held <boulder> <init> <seed> <percent> --batches N           percent% first, the rest added in N
python3 boulder.py scenario <boulder> nothing|duplicates|other-rock|interrupted
python3 boulder.py queue <file>            one of the above a line; long runs: setsid nohup ... &
python3 boulder.py table | heldstats | compare
python3 ../../../tests/test_incr_state.py   # the state logic, without COLMAP or a GPU (a second)
```

## How it works

- **State** (`state.py`), one folder a boulder: the photos (by content: a photo already there under any name is not
  added again), COLMAP's database, the model in the frame the first build set (`model/`, the previous in
  `model.prev/`), the mesh (`dense/`), VGGT's cameras (`cache/`), a log folder per add, and `manifest.json` (each
  photo: hash, which add brought it, whether and how it is placed, how often it was tried; each add's record; the
  last full solve and the last mesh). An add writes `journal.json` first and becomes the state only when `commit()`
  renames the new model in and rewrites the manifest, so an add killed midway leaves the last state usable, and the
  next add takes up its photos.
- **Features** for new photos only, with the app's settings (affine shape and domain-size pooling: COLMAP has them
  only on the CPU). `--feat siftgpu|aliked` for GPU SIFT or ALIKED/LightGlue (they change which photos are placed).
- **Matching** of the pairs not tried yet (new × all), listed for COLMAP, guided, on the GPU; the rows of pairs
  matched before are checked to be unchanged afterwards.
- **Placement** (`core.py place`):
  - *No model yet* (or a re-solve): a start, then the rest registered against it. Starts, tried in turn
    (`--init auto`): VGGT's cameras for the photos best linked by verified matches (as many as the GPU has room for,
    80 at most), each checked against its pairs' two-view geometry (`vggt_start.py`'s check), then COLMAP
    triangulates and refines in three rounds of tightening tolerance (the app's `vggt_place` rounds, here in one
    pycolmap session so the solves can use the GPU); else COLMAP's global mapper; else its incremental mapper.
    `poses` takes starting cameras from a file in the same format (for the phone apps' tracked poses).
  - *A model there*: the new photos (and those left out before) are registered with every camera of the model held,
    pose and lens, so its frame, level and scale stay put; then only the added cameras and the points they see are
    refined (the problem's size follows the add, not the model).
  - Registration is COLMAP's own loop (best candidate next, triangulate, local refinement), run twice: on COLMAP's
    terms, then on the app's looser "rest" terms (15 inliers, 15%).
  - **A guard on refinements of everything**: one that moves a lens more than 1.3× either way, or loses cameras, is
    undone and done again with the lenses refined before held. On photos taken from about one place the matches fix
    neither lens nor depth, and each free refinement walked them further (measured below).
  - **VGGT for what registration misses**: photos joined to the model by verified matches but not registered are
    given to VGGT with 12 placed photos (anchors); VGGT's frame is laid over the model's through the anchors, and the
    photos are triangulated and refined from there, kept if 15 of their points survive.
- **Gates** (`state.py`, thresholds in `SETTINGS`, first guesses, reported with each decision):
  - *A full re-solve* when the placed photos have grown by half since the last full solve, when under half of the
    new photos linked to the model register, or when photos joined to the model by verified matches are left out
    (3 or more, and 10% of those placed): a full solve takes them together. The re-solved model is laid back over
    the old one's frame.
  - *A new mesh* when the add brings rock the mesh doesn't have (new 3D points off it, 2% of the model's) or covers
    the mesh better as the app's capture plan measures it (`backend/captureplan.py`'s `coverage()` and `NEED`, read
    only: 5 points more of its area covered as the plan asks). Otherwise the cameras are recorded and the mesh kept.
- **Dense, mesh, texture**: OpenMVS 2.4, densifying on the GPU. Depth maps are not kept (see Storage).

## Devices

RESULTS-DEVICES

## Results

RESULTS

## Storage

RESULTS-STORAGE

## What shipping it in the app would need

RESULTS-SHIP

## Next

RESULTS-NEXT
