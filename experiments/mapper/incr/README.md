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
  - *No model yet* (or a re-solve): a start, then the rest registered against it. Starts, in turn (`--init auto`):
    VGGT's cameras for the photos best linked by verified matches (as many as the GPU has room for, 80 at most),
    each checked against its pairs' two-view geometry (`vggt_start.py`'s check), then COLMAP triangulates and
    refines in three rounds of tightening tolerance (the app's `vggt_place` rounds, in one pycolmap session); then
    COLMAP's global mapper; then its incremental mapper. `poses` takes starting cameras from a file in the same
    format (for the phone apps' tracked poses).
  - **VGGT starts a model only with room for two thirds of the linked photos** (`vggt_min_share`): with fewer, most
    photos would be registered one by one against VGGT's few, and `auto` starts from the global mapper. Whatever
    the start, its cameras seeing fewer than 15 of the model's points are taken out first (the global mapper places
    some it has nothing to tie down) and tried again like any photo left out.
  - **Each start's model checks itself before it is kept** (`selfcheck.py`; thresholds in `state.py`): its points'
    reprojection errors (mean over 2 px or 90th percentile over 3 fails), its cameras against their photos'
    pairwise geometry (the median angle between the turn from one camera to another in the model and the turn their
    fundamental matrix gives through the model's lenses: over 5 degrees fails), and its lenses (a fifth that can't
    be real fails). A model that fails is solved again from the next start; of those tried, one that passes is
    kept, else the one failing least, and the manifest (`check`, `attempts`, `starts`), the log and `out/check.json`
    say which and why. After an add, a model that passed and now fails is solved again in full. The check says a
    model is at odds with itself, not that one is right: a model can pass and be wrong.
  - *A model there*: the new photos (and those left out before) are registered with every camera of the model held,
    pose and lens, so its frame, level and scale stay put; then only the added cameras and the points they see are
    refined (the problem's size follows the add, not the model).
  - Registration is COLMAP's own loop (best candidate next, triangulate, local refinement), run twice: on COLMAP's
    terms, then on the app's looser "rest" terms (15 inliers, 15%).
  - **A guard on refinements of everything**: one that moves a lens more than 1.3× either way, or loses a camera
    to a degenerate lens, is undone and done again with the lenses refined before held. On photos taken from about
    one place the matches fix neither lens nor depth, and each free refinement walked them further (measured
    below). A camera dropped for seeing too little doesn't hold the lenses (it did until 2026-11-14, see Results).
  - **VGGT for what registration misses**: photos joined to the model by verified matches but not registered are
    given to VGGT with 12 placed photos (anchors); VGGT's frame is laid over the model's through the anchors, and the
    photos are triangulated and refined from there, kept if 15 of their points survive. If the model passed its
    own check before and fails with them in, they're left out and the model stays as it was.
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

Every number below is from the tool tree `build-incr-full`, which the benchmark's `verify_build.py` passes:
Ceres master with SuiteSparse (CHOLMOD), METIS, OpenBLAS, CUDA and cuDSS; COLMAP 4.2.1 patched
(`../patch_colmap.py`) with CUDA, Caspar, ONNX Runtime's CUDA provider and OpenMP; pycolmap built against it;
OpenMVS 2.4 with CUDA and OpenMP; everything `-O3 -march=x86-64-v3`. The libraries a run really loaded are in each
step's record (`build`): `libcholmod`, `libcudss`, `libcusolver`, `libopenblasp`. VGGT runs in the app's `-gpu` image
(torch 2.14, cuDNN 9.1, on the 3090; it refuses to run anywhere but CUDA). The device each step used is from its
logs (the patched COLMAP names each bundle adjustment's device and solver) and the GPU sampler, not assumed.

| Step | Device |
|---|---|
| features (the app's affine-shape, domain-size-pooling SIFT) | CPU: COLMAP has those only on the CPU |
| matching (guided) | GPU |
| VGGT's cameras | GPU (9-10 GB for 32-72 photos) |
| refinements of everything (`--ba auto`) | CPU: Ceres with SuiteSparse (since 2026-11-14; Caspar on the GPU with `INCR_AUTO_BA=caspar` or `--ba caspar`: 7 times faster and less exact, see Results) |
| local refinements (6 photos), an add's own refinement | CPU (Ceres) |
| densify | GPU (OpenMVS CUDA) |
| mesh, texture | CPU |

## Results

### Timed on atlas, and what they showed (2026-11-14)

Another session timed `first <boulder> auto 0 --cond prod` on atlas (i7-8700K, 6 cores; RTX 3060, 12 GB), a quiet
machine, production conditions, one seed, with the code as of a655737: before the fixes below. Its tables are in
`/local/tensor/Tom/bm_opt_handoff/atlas/consensus-*.md`; models are compared by `../consensus.py` (each camera's
viewing rays through its own lens, on the cameras that agree).

| Boulder | Whole | Placed | Rays within 1 / 2 / 5 degrees of the 3.9.1 baseline's | The baseline against itself | Mean reprojection error |
|---|---|---|---|---|---|
| e6de (33) | 153 s | 32 of 33 | | | |
| 31de (82) | 399 s | 72 | 65% / 68% / 94% | 96% / 100% / 100% | 0.77 px |
| 9f3b (150) | 971 s | 133 | 0% / 0% / 5% (median 12 degrees) | (its second run is another model) | 5.70 px (90th percentile 15) |
| Drifter (234) | 1,404 s | 148 | 86% / 94% / 96% | 93% / 94% / 96% | 2.88 px |

Step times on Drifter: features 284 s, match 257, VGGT 35, place 182, assists 111, dense 179, mesh 44, texture 307.
For scale, the global mapper with its untied cameras taken out and the app's rest step (`global_tied`) agreed with
the baseline on 89% / 100% / 100% of 31de's cameras in 132 s, and on 61% / 85% / 94% of Drifter's in 255 s.

**On 9f3b the pipeline came out wrong and did not know it**: 133 photos placed, none untied, nothing in its counts
amiss, and its cameras 12 degrees (median) from the baseline's and no nearer any other method's. (No two methods agree
on that boulder: the baseline is the same camera as another model's for a quarter of its cameras in the middle case,
and one of its own runs is another model.) On 31de it was looser than the global mapper. What was behind each, from
atlas's logs:

- **The 2.9 and 5.7 px were the VGGT assist's doing, and are COLMAP's own figure** (each point's mean error over its
  track, `compute_mean_reprojection_error()`). Placement left 9f3b at 1.12 px and Drifter at 0.83; the assist step
  after it took them to 5.70 and 2.88, with 13,000 and 17,000 points fewer and longer tracks. To take in VGGT's rough
  cameras it triangulates at up to 150 px and then tightens, and its refinement completed and merged tracks across
  the whole model at those tolerances while filtering only in the added photos: old photos' points were merged and
  given observations far off, and nothing took them out. The cameras didn't move (they're held), but the points the
  dense steps start from did. Not the 1-degree triangulation angle (31de, with no assist, is at 0.77 px), nor the
  filter thresholds, which are COLMAP's (4 px).
- **The lenses were held from the first refinement on, in all three larger boulders**, because that refinement
  dropped a camera: the guard took any lost camera for lenses walking off, with none walked. So every lens stayed
  where VGGT's three rounds or its first refinement left it.
- **VGGT had room for 42 photos**: on 12 GB it started 31de from 42 of 72 linked photos, and 9f3b and Drifter from 42 of
  150 and 234, and the other 90 to 100 photos it placed were registered one by one against that base.
- **Caspar** solved every refinement but the last (see below).

Changed since (2026-11-14, **not yet run**: hal is held, so these are code and quick tests only):

- the assist's and the phone-pose placement's refinement completes and merges only the added photos' own points, and
  only in the last round, at COLMAP's tolerances; and a model that passed its check before them and fails with them is
  left as it was;
- a camera dropped for seeing too little no longer holds the lenses;
- VGGT starts a model only with room for two thirds of the linked photos; otherwise the global mapper does, with its
  untied cameras taken out;
- `--ba auto` is Ceres on the CPU throughout;
- every model checks itself before it's kept, and is solved again from the next start when it fails.

What that check says of the models on hal (`selfcheck.py table`, 29 of them): it passes every model of 31de (cameras
1.8-2.7 degrees, median, from their pairs' geometry), Drifter's baseline (3.0) and this pipeline's of e6de (1.0-1.5);
it fails every model of 9f3b (11-16 degrees: the baseline's too, which also has 24 of 114 lenses that can't be real)
and both baseline runs of 38f3 (10-12 degrees; they placed 111 and 181 photos), and on e6de the baseline's and four
other methods' (6-35 degrees; it passes one at 4.3). It fails the three pipeline models the assist had spoilt on their error. So it fails
where runs don't repeat, on the numbers so far; whether the pipeline now passes it on 9f3b from any start, atlas's
next runs will say. If none does, the model kept says so.

### On hal, in development

All indicative: development conditions (6 cores, niced, other work on hal at times), one seed, and with the faults
above still in (Caspar where it says `auto`; the 150-photo build's lenses held; the assist's merging wherever an
assist ran, which is the 150-photo build and the 33-photo adds).

**First builds** (seconds per step; placed = photos in the model):

| Boulder | Photos | Placed | Features | Match | VGGT | Place | Dense | Mesh | Texture | Benchmark, for comparison |
|---|---|---|---|---|---|---|---|---|---|---|
| e6de (33) | 33 | 32 | 52 | 4 | 23 | 53 | 6 | 2 | 3 | 3.9.1 baseline: 21 placed after rest, mapper 353 s; global0: 32 in 39 s |
| 31de (82), Ceres | 82 | 72 | 146 | 14 | 49 | 169 | 29 | 35 | 139 | 3.9.1 baseline (first scripts, 4 cores): 71 placed |
| 31de (82), `--ba auto` | 82 | 72 | 140 | 13 | 41 | 72 | 67 | 66 | 254 | (dense-texture slowed by a run beside it) |
| 9f3b (150), Ceres | 150 | 139 | 300 | 45 | 52 | 418 | - | - | - | |

**Bundle adjustment backends**, the 82-photo boulder from the same VGGT cameras, placement alone: Caspar throughout
26 s, `auto` (Caspar, then Ceres last) 88 s, Ceres on the CPU 181 s, Ceres on the GPU (cuDSS) 211 s; all place the same
72 photos. Their cameras, laid over each other (median / 90th percentile, degrees): CPU vs GPU 0.05 / 0.7, Caspar vs
CPU 0.34 / 3.5, `auto` vs Caspar 0.10 / 0.7 (the last Ceres round doesn't pull Caspar's cameras to Ceres's: the two
settle apart from the start). For scale: two 3.9.1 runs (baseline and `lessba`) differ by 0.19 / 3.6, and our CPU
model and the 3.9.1 baseline by 0.12 / 5.7. One bundle adjustment of the finished 72-photo model: CPU 18 s, GPU 12.9 s,
Caspar 0.38 s. Ceres on the GPU is no faster than on the CPU for these sizes (dense Schur, 70 photos).

**Adding photos** (a random 70% or 90% first, the rest added in one batch or three):

| Boulder | Run | Placed (from scratch) | Adds' seconds | Old cameras moved | Added vs from scratch (median / p90 deg) |
|---|---|---|---|---|---|
| 31de | 70% + 30% | 72 (72) | 222 | 0 | 0.74 / 6.7 |
| 31de | 90% + 10% | 72 (72) | 27 | 0 | 0.38 / 2.9 |
| 31de | 70% + 3 batches | 72 (72) | 122, 154, 194 | 0 | 0.97 / 8.5 |
| 31de | 70% + 30%, `auto` | 72 (72) | 125 | 0 | 0.91 / 15.9 |
| e6de | 70% + 3 batches | 32 (32) | 48, then re-solves 110, 103 | 0 | 1.8 / 46 |
| e6de | 90% + 10% (before the joined-photos gate) | 24 (32) | 42 | 0 | 3.1 / 36 |

An add keeps the old cameras exactly where they were (moved 0.0); the price is that the first 70%'s errors stay, so
added cameras sit about 0.4-1 degree (median) from a from-scratch build's, above the 0.1-0.4 between methods. The
33-photo boulder is a poor yardstick for accuracy: every method's cameras there disagree with every other's by 4-12
degrees (median), the photos having almost no parallax.

**Scenarios** (33 photos): nothing new is a no-op; duplicates (by content, under other names) are a no-op; 12 photos
of another rock are left unplaced and the model untouched; an add killed after matching leaves the state as it was,
and the next add takes up its photos (and re-solves, 32 placed).

**From a finished mesh** (`bootstrap`, from cameras.json, 82 photos): the 72 cameras set unchanged (0.0 units,
1e-6 degrees, lenses identical), 42,274 points at 0.79 px; 203 s, of which features 140, matching 19, triangulating 44.

What measurements changed in the design: lenses walked off on low-parallax photos in refinements with them free (one to
0.12 of its focal length), hence the guard; at COLMAP's 1.5-degree least triangulation angle a low-parallax model kept
under 600 of its 15,000 points, so new photos had nothing to register against, hence 1 degree; VGGT's assist with 3
anchor photos was laid 0.25-0.5 of the scene off, hence 12; adds that joined photos by their matches but couldn't place
them (8 of 33 left out where the from-scratch build placed them), hence the joined-photos re-solve gate; Ceres on the
GPU was slower than on the CPU here; Caspar was 7 times faster than either and its cameras 0.34 degrees (median) from
Ceres's, 3.5 at the 90th percentile, where Ceres's on the CPU and the GPU are 0.05 and 0.7 apart, and atlas found
it wrong on 31de in COLMAP's own mapper: hence `auto` is Ceres on the CPU and Caspar is to be asked for.

## Storage

Measured on the 82-photo boulder's state (bootstrapped from its cameras): **about 2.0 MB a photo** besides the photos
themselves. COLMAP's database is 1.8 MB of it: SIFT descriptors 1.16 MB (about 9,000 a photo, kept so later photos
can be matched against these), keypoints 0.22, raw matches 0.15, verified matches 0.18. The model is 0.26 MB. The
benchmark's web photos are 0.46 MB each, so a state is about 4 times its photos; next to a phone's 3-5 MB photos it
would be about half. That is why the site keeps only `reconstruction.tar.gz` (`backend/reconstruction.py`: the model
and verified pairs, 0.1-0.25 MB a photo) and leaves descriptors out: re-extracting them is bit-identical within one
COLMAP build on the CPU. A state here keeps no depth maps (6 MB a photo at resolution level 2): an add changes
photos' neighbours, and densifying on the GPU takes seconds.

## What shipping it in the app would need

- **Which photos are placed changes** (VGGT's start, 1-degree points, the re-solve gate, Caspar): `MESH_RULES` would
  have to go up, which needs the user's OK first.
- The app's mapper step becomes `add()`: the server keeps a state per mesh (2 MB a photo, or rebuilds one from
  `reconstruction.tar.gz` when photos come), and a worker runs the steps here in place of `pipeline.py`'s mapper.
- The tool tree (pycolmap on COLMAP 4.2.1 with Caspar; OpenMVS 2.4) into the worker images, verified by
  `verify_build.py`.
- A capture's outputs (`out/`: mesh.glb, cameras.json, frame.json, level.json) are made as a remote job returns
  them (`backend/compute.py` `take_results()`), so the server takes them as it takes any mesh.

## Next

- Atlas's reruns of the four first builds with the fixes above, and whether each model's own check agrees with what
  `consensus.py` says of it; then 5 seeds on the small and medium boulders against the baseline's own spread.
- Simulated captures end to end (sim_capture.py, `boulder.py simcap`): placed, pose agreement against the truth,
  level against the 1-degree goal, pairs and time against capture x all, the capture's texture share, add against
  refit against the server's own "cameras" tie.
- Hold detail meshes for printed replicas, with the card's scale.
