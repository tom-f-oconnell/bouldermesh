# Making a mesh faster without making it worse: a plan for an agent to carry out

Written 2026-11-14 on atlas, for an Opus agent starting with no other context. Tom's priority, in his words: "fast is
*the* priority, as long as it isn't making quality worse", and "it is overall throughput that matters" (meshes an
hour, not one job's time). `MESH_RULES` is not a concern until there is a fast pipeline of similar or better quality.

Read first: `bench.py`'s docstring, `variants.py`'s, and the tail of `/mnt/tensor/Tom/bm_opt_handoff.md` (the two
sessions' log; append only, one write per message, under `## <n>. From atlas|hal, <time>`).

## Where a job's time goes now

The app's own pipeline in `bouldermesh:global-gpu` (COLMAP 4.2.1's global mapper on the GPU, OpenMVS 2.4), Drifter,
234 photos, atlas (6 cores / 12 threads, RTX 3060): 1,276 s and 7,746 CPU-seconds.

| Step | Seconds | Bound by | Notes |
|---|---|---|---|
| texture | 334 | CPU, ~8 threads | 271 s is choosing a view per face; no GPU path in OpenMVS |
| features | 250 | CPU, every thread (~3,300 CPU-s) | CPU SIFT: affine shape and domain-size pooling have no GPU version |
| matching | 220 | GPU | every pair: 27,261 on Drifter |
| dense | 182 | one CPU thread, GPU at ~25% | estimation 41 s, two geometric passes 84 s, filtering 34 s, fusion 13 s |
| mapper | 161 | GPU for the main model | 44 CPU solves are three side models of 8-9 photos nobody uses |
| rest | 55 | CPU | |
| mesh | 46 | CPU | |
| people, both passes | 18 | GPU | |

For throughput the budget is CPU-seconds and GPU-held seconds per job, not wall time. Drifter needs about 7,750
CPU-s (features 43%, texture 36%) and holds the GPU about 560 s. On atlas's 12 threads that allows 5.6 jobs an hour
by CPU and 6.4 by GPU; one at a time it gets 2.8. On hal (48 threads, RTX 3090) the GPU is the limit.

## How "not worse" is judged (the same for every change)

Run the change and its reference on 31de (`31de9a59303a`, 82 photos) and Drifter (`ce7571e0a4a3`, 234), both with
`global_tied`, seed 0, `--cond prod --build complete --post complete --mvs as23`. 9f3b is the hard boulder (every
model fails there): report it, don't judge by it. e6de is chaotic: never judge by it. Timings one job at a time,
through `chain.py <queue file>` (systemd unit `bm-bench`), never beside other load.

A change passes if all of these hold on both boulders:

1. **Photos placed** after the rest step: no fewer than the reference less 2 (`bench.py table`, `after_rest`).
2. **The model's own check** (`python3 incr/selfcheck.py table $BENCH`): `ok` stays True, and `pair_med_deg` is within
   0.5 of the reference's (references read 2.5-3.5).
3. **Cameras** (`python3 consensus.py <project>`, system python3): rays within 2 degrees of the reference for at
   least as large a share as the reference's two seeds have with each other.
4. **Surface** (`python3 surface.py <project> <run>/<post> <reference run>/<post>`): near the rock, the median and
   the share beyond 1% no more than 1.5 times what two seeds of the reference give against each other
   (`global_tied` seeds 0, 1, 2 with meshes are made by `queue-check-1.txt`).
5. **Mesh** (`python3 meshes.py <project>`): fused points and faces within 15% of the reference's, unless the change
   is meant to alter them, and then Tom judges the GLB by eye (`meshes.py export
   /mnt/tensor/Tom/bm_opt_handoff/atlas/meshes`).
6. **Texture**, for texture changes: `bench.py texture report`: `used_mpx` and `sharpness` beside the reference's,
   and the GLB for Tom's eye. There is no number that settles texture quality; say so.
7. **Cost**: wall seconds and CPU-seconds of the step changed, and of the whole job. A change that saves wall time
   by using more CPU-seconds is a loss for throughput: report both.

Post each result in the handoff file with run names, and "passes"/"fails" per rule. Anything that changes which
photos are placed or how a mesh looks goes to Tom before it goes into `backend/`.

## Steps, in order

Steps 1-6 are written and queued (`queue-opt-1.txt`, whose jobs are also at the head of `queue-check-1.txt` so that
the chain as it runs reaches them). Steps 7-10 need code.

### 1. Texture settings (queued)

`bench.py texture <project> <run> <variant> --tools complete --cond prod --mvs as23` reruns only TextureMesh on a
finished post folder (`variants.py` `TEXTURE`). Variants: `same` (the reference), `res1`/`res2` (photos at half and
quarter size), `views2`/`views3` (every second or third photo), `vface3` (`--virtual-face-images 3`), `noseams`,
`dec50`. Judge by rule 6 and 7. `dec50` halves the faces: only if nothing else works, as holds need geometry.
- If one passes: add its arguments to `MVS24_*`/TextureMesh's call in `backend/pipeline.py`, with a test in
  `tests/test_pipeline.py` of the command, as 7efafce did for the dense step.
- Then try pairs of the passing ones (add a combined entry to `TEXTURE`).

### 2. A video's repeated frames dropped (queued)

Feature sets `sift391_dedup90`, `_dedup75`, `_move2` (`variants.py`, made from the database after matching). Drifter
loses 51 of 176 video frames with dedup90. Judge by rules 1-5 against the full set's run.
- If it passes, the saving only counts when the frames are dropped *before* extraction. Implement in the harvest
  (`backend/climbs/`, where video frames are chosen; `diversity.py` has the no-parallax measure dedup uses): compare
  each new frame with the last kept one on downsized pictures and keep it only if it moved. Test: the kept set
  against dedup90's on the five boulders' videos (agreement of which frames go), then rerun the judge.

### 3. Plain SIFT on the GPU (queued)

Feature set `siftgpu391`; the app's switch is `GPU_SIFT=1` (c4c3b33), off. Removes about 3,300 CPU-s per Drifter
job. Judge by rules 1-5; this one is the most likely to place fewer photos.
- If it fails on placement, try GPU SIFT for video frames only (they overlap heavily) and the app's CPU SIFT for
  stills: a new feature set that extracts twice into one database (`--image_list_path` per group), then matches once.

### 4. Jobs side by side (queued, mapper to texture only)

`bench.py together <N> jobs-global3.txt` for N = 1, 2, 3; `bench.py together report` gives meshes an hour. It holds
each job to cores/N and takes `gpu.lock` for GPU steps, and leaves features and matching out. It is a first read;
step 7 is the real one.

### 5. Dense with one geometric pass; the mapper without side models (queued)

`post ... --mvs as23g1` (`--geometric-iters 1`; saves about 40 s on Drifter): rules 4 and 5 against the `as23` post
of the same model. `global_one` (`--GlobalMapper.multiple_models 0`): rules 1-3, and its mapper seconds.

### 6. Smaller pictures; no guided matching (queued)

Feature sets `sift391_1600` and `sift391_noguided` (bench.py). Rules 1-3, and extract and match seconds from their
`@prod` folders.

### 7. Whole app jobs side by side (written: 5dce8e5, `queue-together-app.txt`)

The measure Tom cares about. Extend `variants.together()` to take `app <project> --image ...` lines: no features to
link; each job as `--cond c<cores/N>` with `BENCH_R` as now; **no `gpu.lock`** for these (the app has none: add an
environment switch read in `bench.step()`), so GPU steps share the card as two workers would; `job_result()` from
`app.json` and `metrics.json`. Run N = 1, 2, 3 on 31de + 9f3b + Drifter in `bouldermesh:global-gpu`. Report meshes an
hour, each job's `steps.json`, peak GPU memory (gpusample), and any step that failed for want of GPU memory. Also
try N jobs each with every core (no `--cpus`), which is what unpartitioned workers would do.
- If 2 at a time gains over 1.5x: the change is in `worker/bouldermesh_worker.py` (jobs at once per machine), which
  is Tom's and hal's to decide.

### 8. Fewer pairs to match (written: the `pairs<K>` sets, `queue-opt-pairs.txt`)

A pair list in place of exhaustive matching: each video frame with its 10 neighbours either side in its video, and
each photo with its 30 most alike by image embedding (the off-topic filter already loads CLIP: `backend/climbs/
relevance.py`). In the benchmark: a derived feature set `sift391_pairs30` in `variants.py` that copies the database
without matches and runs `colmap matches_importer --match_type pairs` on the list. Judge by rules 1-5; report pairs
matched, verified pairs kept against the exhaustive set's, and match seconds. Try 20 and 50 as well.

### 9. Why the dense step uses one thread (needs a look)

`DensifyPointCloud` holds one CPU thread at 100% and the GPU at 25%. Find what that thread does (`perf top -p`, or
`py-spy` won't help: it is C++; `gdb -batch -ex "thread apply all bt"` a few times). If it is reading or scaling
images, more threads or a RAM disk may fix it; if it is the CUDA kernel launch loop, leave it.

### 10. Learned features (only after step 8)

`aliked421` extracts fast but matches slowly per pair, so it is only worth timing with a pair list. Needs COLMAP
4.2.1 for features and matching as well as the mapper.

## What not to do

- Don't judge anything on e6de, or on one run where the reference's own seeds differ more than the change does.
- Don't time anything while a build or another job runs; don't run containers without `--memory`.
- Don't put a change into `backend/` on speed alone: the rules above, then Tom.
