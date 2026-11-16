# BoulderMesh: benchmarking the meshing pipeline

What has been measured about making a mesh faster without making it worse, what was decided from it, and what to do
next. For Tom, and for AI coding agents picking the work up with no other context. Two sessions keep it: "hal"
(sections marked `<!-- owner: hal -->`) and "atlas" (`<!-- owner: atlas -->`: every number measured on atlas). Each
edits only its own sections and proposes changes to the other's in the handoff file ("Where things are", at the end).

**Where things stand, 2026-11-15.** On atlas, the new pipeline (COLMAP 4.2.1's global mapper on the GPU with its
untied cameras taken out, OpenMVS 2.4 given 2.3's values, people masks on the GPU) makes a whole job 2.6 to 3.5 times
faster than the live one (Drifter: 1,276 s against 3,376 s), with as many photos placed on 31de and Drifter and
meshes as dense. It was deployed on Nov 15 at 15:37 (the server and the machines' image are 8963d55); no mesh
has been made by it on the site's own machines yet, as hal's worker is stopped. Its measured costs are a surface on
Drifter about 0.4% of the scene's size from the old mapper's (a smooth bend; which is nearer the real rock is not
known) and two segfaults of OpenMVS 2.4's dense step in about 65 runs, with a fallback nobody has seen run after a real
crash. The decisions that matter most next are Tom's: which measured candidates to take (texture from every second
photo, one geometric pass, a video's repeated frames dropped, GPU SIFT). Nothing has been timed on hal since the
handoff, so jobs at once on a worker there is not measured. **Atlas's benchmarking ended on Nov 15 at 21:14**, at
Tom's word: its chain finished (the last feature sets gained nothing), the CPU-only runs are on hold ("Benchmarking left
on hold", the last section), and atlas is to make meshes for the site.

## Next steps, in order

**Nov 15, 21:45: Tom told atlas "go with your recommendations"** (handoff section 66). Into the app: one geometric
pass (step 5); GPU SIFT with the CPU SIFT as its fallback when its model fails the check or places under 70% of the
matched photos (step 8); a video's repeated frames dropped in the harvest, slots not refilled (step 6). Not taken: no
side models (step 7). `MESH_RULES` is not raised for them (Tom, section 67:
"defer MESH_RULES"; meshes from before the new boulder grouping are remade first, and the ~3 made between the grouping
and the new pipeline only far down the line, maybe in a lower tier: ask him before queuing them). A hal session writes
them once Tom confirms there (it came by relay); atlas checks each before it's deployed, then times
them together with `views2` (step 10).

<!-- owner: hal -->

"Atlas's chain" is the queue running unattended on atlas; "a hal session" is an AI coding agent working in this repo
on hal. Names in quotes are atlas's sections under "Results on atlas". GLBs are in
`/local/tensor/Tom/bm_opt_handoff/atlas/meshes/`; `<boulder>` is `ce7571e0a4a3` (Drifter) or `31de9a59303a`.

1. **`surface.py`'s start and its guard: fixed on hal, Nov 15** (done; kept here until step 2 confirms it on the pairs
   that failed). It started from the cameras' fit, which was 25% off on one 31de pair, and its ICP shrank a mesh on
   another. Now scale and shift are tried from the models' own 3D points as well as the cameras, at several scales;
   surfaces are matched both ways, near the rock only; the best fit is kept; a row is flagged when two starts end
   equally good at different scales. Checked two ways: on made-up rock (`test_surface.py`) a start a quarter off
   ends within 0.5% of the true scale, where the old matching ended 13% small; and on hal's own Drifter meshes (one
   model through OpenMVS 2.3 and 2.4) a start spoiled by 25% in scale and 15% of the scene in place gives the same
   row as the unspoiled one (near the rock 0.14% and 0.16%, floor 0.10%). A comparison takes 6 to 22 minutes on one
   core, the longer when the start is far off.
2. **31de's surface comparisons, rerun with the fixed tool: done by atlas, Nov 15.** All four pass rule 4 (near the
   rock: `siftgpu391` 0.30%, `pairs30` 0.33%, `pairs50` 0.44%, `pairs20` 0.50%, against 0.35 to 0.82% between seeds;
   no row flagged). The 2 to 7% before were the tool.
3. **Atlas's chain: finished, Nov 15 21:14; benchmarking on atlas stopped there (Tom).** Its last results:
   COLMAP 4.2.1's own SIFT (`sift421`) is no faster to extract and makes the mapper slower (306 and 262 s against 186
   and 112): no gain. ALIKED (`aliked421`) extracts in 22-33 s but matches every pair 6-9 times slower than SIFT, and ran
   out of the 12 GB card's memory on Drifter: worth more only with a pair list and smaller images (step 15). The
   CPU-only runs are on hold, with what each would settle, in "Benchmarking left on hold".
4. **Texture from every second photo (`views2`): in the app, Tom's yes on Nov 15** (not deployed yet). The admin
   page's "Texture meshes from every second photo" (`mesh.texture_every_second`, ticked by default) has TextureMesh
   told which photos through `--views-file` (`pipeline.texture_views()`; OpenMVS 2.4 on, and only with 20 photos or
   more), and is sent with jobs to machines when unticked. Measured on atlas: 335 to 159 s and half the CPU-seconds
   on Drifter. Still true: no number settles texture quality, and 31de's sharpness number fell with it; the GLBs to
   compare are `<boulder>-texture-same.glb` and `-texture-views2.glb`.
5. **One geometric pass (`as23g1`): only Tom's eye is left, then one flag.** It passes the surface rule (at the
   measuring floor on both boulders) and rule 5 (fused points and faces within 2% of two passes'), for 60 s less dense
   time on Drifter. `<boulder>-global_tied-s0+complete@prod-post-complete+as23.glb` and `...+as23g1.glb`. If yes, a hal
   session adds `--geometric-iters 1` to the dense step's arguments in `backend/pipeline.py`, with a test.
6. **A video's repeated frames dropped (`dedup90`): Tom's OK, then a hal session writes it.** Atlas calls rule 4
   passed: its 0.37% on Drifter is a smooth bend like the one two seeds have (patches fitted alone: 0.36% to 0.14%,
   against 0.29% to 0.13% seed against seed), and rules 1 to 3 pass. What's left is the real saving: dropping the
   frames in the harvest, before extraction (`backend/climbs/`), checked against `dedup90`'s choice on the five
   boulders' videos, then judged again by atlas. It changes which photos a mesh is made from, so it needs Tom's word.
7. **`global_one` (no side models): passes the benchmark's rules; Tom's decision, because the app uses side models.**
   Mapper 186 to 117 s on Drifter and 112 to 88 s on 31de, the same photos placed, check and surface within the
   seeds' spread. But the benchmark only looks at the mesh's own model. The app also records every other model the
   mapper made (`models.json`, `pipeline.write_models()`), and the server reads them to say which climbs' photos landed
   in a model together (pooling), and to offer a boulder's other models as meshes of what its mesh left out
   (`backend/server.py`, "rest" meshes). With one model only, those would have nothing to go on. That is "what a
   boulder's mesh leaves out", which the repo's rules send to Tom first. A middle way, not measured: keep side models
   only for pooled boulders and groups, where they're read.
8. **GPU SIFT: Tom's decision.** Its surface now passes on both boulders (Drifter 0.26%, 31de 0.30%), and against
   the same database its model checks no worse on Drifter (2.91 against 3.43 degrees) and a little worse on 31de
   (2.94 against 2.55). What stays: it places different photos (Drifter 20 gained, 3 lost) and its cameras agree with
   the CPU model's less than two seeds do (rays within 2 degrees: 81-82% against 95-97%), with nothing to say which
   is nearer the truth. The prize is 250 to 8 s of extraction and about 2,950 CPU-seconds a Drifter job. Either
   `GPU_SIFT` goes on, stays off, or is tried for video frames only with the app's CPU SIFT for stills (not written).
9. **Pair lists: Tom's decision, or a better list.** They pass the surface rule on both boulders, but on Drifter every
   list places more than 2 photos fewer than the reference (139 against 142 at K = 50), which rule 1 as written
   doesn't allow, and the list missed half of the pairs that verify. Saves about 130 s of GPU matching on Drifter.
10. **Time the passing changes together** as one whole app job on 31de and Drifter (atlas's chain, before it stopped;
    now it would need Tom to give atlas's GPU back to benchmarking, or hal's hold lifted). Each gain so far
    was measured alone; whether they add up is not measured. Atlas can queue it once Tom has said which to take
    (`as23g1` and `views2` are `bench.py` options already; `dedup90` is a feature set).
11. **Jobs at once on a hal worker, and thread counts** (`queue-together-app.txt`, `queue-threads-hal.txt`). Needs Tom
    to lift the hold on hal, and the new image built there. Settled by jobs an hour at 1, 2, 3 and more at once, for
    `worker/bouldermesh_worker.py`. Do not also cap each job's cores.
12. **Watch the new pipeline now it's deployed** (Nov 15, 15:37, 8963d55; a hal session, read-only on the server).
    Both images' meshes from atlas, for Tom's eye: `<boulder>-app-b5607e9-gpu.glb` (the old) and
    `<boulder>-app-global-gpu.glb`. To check on the site's first meshes: each one's `mapper.json` (global, on the GPU,
    no fallback), its `check.json`, its step times beside atlas's, and any "falling back" line in a job's log.
    - Known when it went live: Drifter's surface about 0.4% from the old mapper's, against 0.13% between the old
      mapper's two runs; the two segfaults and the unseen fallback (step 13); nothing run on hal's hardware, or on a
      GPU others are using; a machine with no GPU not measured (step 3); texturing 20 to 42 s slower on 31de and 9f3b;
      meshes differ from those made before, with `MESH_RULES` not raised.
    - hal's worker has been stopped since Nov 15, 11:15: nothing meshes on hal until Tom starts it
      (`sudo systemctl start bouldermesh-worker`).
13. **See the OpenMVS fallback work** (a hal session): a job whose GPU dense step is made to fail on purpose. Settled
    by a job that finishes on the CPU build and says so in its log.
14. **The model check's line (5 degrees): look again** (a hal session with atlas). One model known to disagree with
    every other passes it at 3.42 (the incremental pipeline's section). First measure that model's surface against
    the baseline's, to see that it is wrong and not the comparison; then try the line at 3.5 over every model on both
    machines. In the app the check only reports, so nothing breaks meanwhile. Fixed meanwhile (396dfdd): the check read
    a few point errors of 1e150 px and more that COLMAP 4.2.1 never set (atlas's `aliked421` and `sift421` models of
    31de failed on "error" for them); they are now left out and counted (`err_unread`). With it both pass
    (64 of 7,043 points and 13 of 30,335 left out). The app converts the global mapper's model to 3.9.1 before its own
    steps, so its meshes may not have such points; it matters if a 4.2.1 bundle adjustment ever runs after the mapper.
15. **Lower:** why the dense step holds one CPU thread (plan step 9); ALIKED with a pair list (plan step 10, not
    written; ALIKED placed 78 on 31de with 97% of cameras within
    2 degrees, but ran out of memory on Drifter, so only with a pair list and `max_image_size` lowered); `together report` saying "no mesh made" of jobs that
    made one; CDI in atlas's Docker (Tom's sudo; the worker's `--setup` now
    offers it, cefd417); the incremental pipeline's simulated, then real, captures; and,
    when this round is over, whether `MESH_RULES` goes back (ask before raising it) or stays lifted: Tom's to say.

## What was asked, and how "not worse" is judged

<!-- owner: hal -->

**Tom's priority, in his words** (Nov 14): "fast is *the* priority, as long as it isn't making quality worse", and
"it is overall throughput that matters": meshes an hour, not one job's time. So a change is weighed by a job's
CPU-seconds and the seconds it holds the GPU; one that saves wall time by spending more CPU-seconds is a loss. Also
his: "i want to get a good pipeline that assumes a GPU working as a higher priority than being to run it on CPU".
It began on Nov 12 as a benchmark of faster mappers: on the site's own timings the mapper was 67% of a mesh's step
time (55 min at the median), on the CPU only (`experiments/mapper/review.md`).

| Boulder | Name (from its project's files) | Photos | Its part |
|---|---|---|---|
| Drifter (`ce7571e0a4a3`) | Drifter/Smoking Boulder, Buttermilks | 234 | judged by; the largest |
| 31de (`31de9a59303a`) | Dino's Egg, Joshua Tree | 82 | judged by |
| 9f3b (`9f3b4b740847`) | the climb Gunsmoke, Joshua Tree | 150 | the hard one: every model of it fails its check; reported, never judged by |
| e6de (`e6de7747fab5`) | Tidal Wave Boulder, Joshua Tree | 33 | never judged by: the same job places 9 to 21 photos |
| 38f3 (`38f31439985f`) | Gunsmoke Wall, Joshua Tree | 194 | hal only; its two baseline runs placed 111 and 181 photos |

**The machines.** atlas: i7-8700K (6 cores / 12 threads), 45 GB RAM, RTX 3060 12 GB, nobody else on it. hal: 48
threads, RTX 3090 24 GB, shared; benchmark jobs are held there since Nov 13.

**The seven rules** (`experiments/mapper/optimization-plan.md`). A change and its reference both run on 31de and
Drifter with `global_tied`, seed 0, production conditions, OpenMVS 2.4 `as23`. It passes if all hold on both:

1. Photos placed after the rest step: no fewer than the reference less 2.
2. The model's own check still passes, with its pair angle within 0.5 degrees of the reference's.
3. Cameras: rays within 2 degrees of the reference's for as large a share as the reference's two seeds have.
4. Surface: near the rock, the median gap and the share beyond 1% no more than 1.5 times what two seeds give.
5. Mesh: fused points and faces within 15%, unless the change means to alter them; then Tom judges the GLB.
6. Texture changes: pixels used and the sharpness number beside the reference's, and the GLB for Tom's eye.
7. Cost: wall seconds and CPU-seconds, of the step changed and of the whole job, both reported.

Anything that changes which photos are placed or how a mesh looks goes to Tom before it goes into `backend/`.

**The method** (Tom's requirements from Nov 12-13, which still hold): a build missing a performance component
(SuiteSparse, OpenBLAS, CUDA, cuDSS, Caspar, OpenMP, AVX2) fails `verify_build.py` and is not timed; the device each
step ran on is read from its logs and GPU samples, not its settings; final timings are of one job at a time, in
Docker, as a real job runs (`--cond prod`: every thread, `nice -n 10`), with the app's own image as the baseline and
other users' load recorded; "places the same photos" is judged after the app's rest step, against the baseline's own
run-to-run spread; results are labelled timed or indicative, and two machines' timings never share a table.

## Decisions, and what's in the app

<!-- owner: hal -->

**Deployed on Nov 15, 15:37: everything in the table** (the server and the machines' image run 8963d55). Until then
the site ran 5cd56f1: COLMAP 3.9.1's incremental mapper, OpenMVS 2.3 on the CPU, people masks on the CPU in two passes,
no model check. Every timing in this file was taken before that, in the benchmark, not on the site.

| What | Decided | Commits | Deployed | Gain measured | Known cost or risk |
|---|---|---|---|---|---|
| **The global mapper is the default** (`MAPPER`): COLMAP 4.2.1 with CUDA and cuDSS; cameras seeing fewer than 15 of its points taken out, then the app's rest step (`global_tied`); on the GPU from 1 GB free, on the CPU if that run fails | Tom, Nov 14 ~21:00; "with CUDA" at 21:23 | 4380ece, 6a9df81, 53b3c34, 273d08e | Nov 15 | mapper step 1,608 to 161 s on Drifter in the app's own job ("The app's whole job") | Drifter's surface about 0.4% of the scene's size from the old mapper's, against 0.13% between the old mapper's two runs: a smooth bend ("Is the global mapper's model right?"); its check reads a little higher (3.19 against 2.79 on Drifter); one seed leaves different cameras untied from run to run; its stage adds 2.5 GB to the machines' image; its speed without a GPU not measured |
| **Fallback to 3.9.1's incremental mapper**: with `MAPPER=incremental`, no `colmap4`, a failed global run or conversion, or fewer than 6 tied photos | with the above | 4380ece | Nov 15 | | run only when chosen (31de: 1,114 s), never after a failure |
| **OpenMVS 2.4** in both images, depth maps on the GPU, with 2.3's fusion and mesh values (`as23`) | Tom, Nov 14 ~21:00 | 4749d2c, 7efafce, 2de8198, e5bf07a | Nov 15 | dense step 691 to 182 s on Drifter, meshes as dense ("OpenMVS 2.4 against 2.3") | 2 segfaults in about 65 dense runs, cause unknown; the fallback (2.4's CPU build, same fusion) never seen after a real crash; 2.4's depth maps on the CPU not timed yet |
| **People masks on the GPU**, the second pass warped from the first | written Nov 14, at Tom's ask | e0898a3 | Nov 15 | both passes 100 to 18 s on Drifter (atlas); 51 against 410 ms a photo (hal) | masks match the CPU's (IoU 0.997 at worst, hal); the warp within a one-pixel line of COLMAP's undistorter on e6de and 31de |
| **The model's own check**: `check.json` beside each mesh, shown in the meshes list | Tom, Nov 14 ~19:30 | 4af0d9c, ece3cd4 | Nov 15 | fails the two models known wrong on atlas and every model of 9f3b ("How models are judged") | says a model is at odds with its own photos, not that one is right; its line (5 degrees) comes from five boulders |
| **`GPU_SIFT=1`**, off unless set | not decided | c4c3b33 | Nov 15 | atlas's table of changes | changes which photos are placed (step 8) |
| **Kept reconstruction** (`reconstruction.tar.gz`: the model and verified pairs, no features) | Tom, Nov 13 | 89bee94 | in the commit the server runs | 0.1 to 0.25 MB a photo | |
| **EXIF orientation** read the same way by every step | Tom, Nov 13 | 2b6f23f | in the commit the server runs | | |

Tom's other decisions:

- **`MESH_RULES` is lifted for this round** (Nov 14): "no point sticking to MESH_RULES where meshes are this slow to
  compute". The mapper and OpenMVS change without raising it (still 1 in `backend/server.py`), and old meshes are not
  made again; each mesh's `mapper.json` (4380ece) says how its cameras were placed, to find and redo meshes by later.
- **A failed model check is shown, not acted on** (Nov 14): `backend/pipeline.py` writes `check.json` and a log line
  and "decides nothing about the mesh and never fails one".
- **The GPU pipeline first, CPU fallbacks last** (Nov 14, 21:25). The first `colmap4` stage had no CUDA, which none of
  the timings behind the switch had measured; Tom had it rebuilt (6a9df81). The CPU-only build is kept for the
  server's image, which never meshes.
- **Tom is told of any result that argues against switching the mapper.** Atlas marks them "Against the switch". So
  far: the global model's slightly worse numbers and its surface gap on Drifter, texturing 20 to 42 s slower on 31de
  and 9f3b, and OpenMVS 2.4's second segfault.

## Results on hal

<!-- owner: hal -->

Timed on hal (48 threads, RTX 3090, shared) on Nov 12-13, before the hold. All provisional: no method on the newer
COLMAP was timed there on a verified build. hal's timings are not to be put beside atlas's.

The live mapper (COLMAP 3.9.1 in the app's image, seed 0, production conditions, one job at a time):

| Boulder | Photos | Mapper | Mapper CPU-s | Models | Placed after rest | Dense | Mesh | Texture |
|---|---|---|---|---|---|---|---|---|
| e6de | 33 | 353 s | 5,425 | 15+6 | 21 | 28 s | 6 s | 25 s |
| 31de | 82 | 553 s | 8,126 | 70+7+6+4 | 71 | 138 s | 30 s | 183 s |
| 9f3b | 150 | 1,650 s | 14,924 | 95+27+23+16+15+3 | 114 | 171 s | 34 s | 177 s |
| 38f3 | 194 | 2,173 s | 30,073 | 107+79+22+6 | 111 | 140 s | 35 s | 185 s |
| 38f3, repeat | 194 | 4,991 s | 40,090 | 172 | 181 | not run | | |
| Drifter | 234 | 1,554 s | 17,443 | 137+10+9+8 | 143 | 276 s | 46 s | 343 s |

- **The live mapper does not repeat itself.** 38f3's two runs, same seed, placed 111 and 181 photos. Neither was
  clean (the first overlapped a rebuild, the repeat ran under others' load): that explains the times, not the result.
- **Side models** took this share of the live mapper's time: 38f3 49% (1,062 of 2,173 s), 9f3b 36% (592 of 1,650 s),
  e6de 20%, Drifter 7%, 31de 3%.
- **A video's repeated frames**, by `backend/climbs/diversity.py`'s measure (`dedup90` / `dedup75` / `move2`): Drifter
  51 / 60 / 30 of 176 frames; 38f3 27 / 30 / 29 of 143; 31de 25 / 33 / 18 of 65; 9f3b 2 / 6 / 5 of 114.
- **OpenMVS 2.4 as it comes keeps a fifth of the points.** One Drifter model: 2.3 on the CPU 4,851,742 fused points
  and 1,595,286 faces, depth maps in 276 s; 2.4 with CUDA 1,064,672 and 1,085,194, in 56 s. That led to `as23`.
- **The model's check over hal's 29 models:** passes every model of 31de (1.8 to 2.7 degrees) and Drifter's baseline
  (3.0); fails every model of 9f3b (11 to 16) and both of 38f3's baseline runs (10.1 and 11.7).
- Newer COLMAP ran on e6de only, on a build without SuiteSparse or AVX2: nothing to conclude from it. The live
  mapper's times at 48 threads match atlas's at 6 cores, so it barely uses cores.

What went wrong on hal:

- A "GPU" result that had not used the GPU (COLMAP 4.2.1 solves on it only from 50 photos up), and a build missing
  SuiteSparse and AVX2, found a day late. Hence `verify_build.py`, and the device read from each run's logs.
- The AI coding agents running it stopped twice (an API outage, about 6.5 hours lost; a session moved into tmux).
  Anything long now runs detached, with a log and a state file.
- Timed runs overlapped other work on a shared machine, and hal's root pool (Docker's data, on ZFS) filled to 0 B.
- **The hold.** Since Nov 13, 11:20, no benchmark job starts on hal until another user's work there is done; it was
  still in force at its last mention (Nov 15, 00:05). Freezing the chain did not stop it at first: a second unit was
  waiting to restart it. To resume: `/data/tom/mapperbench/HELD.txt`.

## Results on atlas

<!-- owner: atlas -->

Everything in this part was measured on atlas between 2026-11-13 and 11-15: Intel i7-8700K (6 cores, 12 threads,
AVX2), 45 GB RAM, RTX 3060 12 GB, driver 595.99, Docker, results under `/mnt/d0/mapperbench/r2`. Timings are of one
job at a time under production conditions (`--cond prod`: every thread, `nice 10`) with nothing else running, unless
a row says otherwise. Run names are the folders under `r2/<boulder>/<feature set>/`. The boulders: 31de (`31de9a59303a`,
82 photos), 9f3b (`9f3b4b740847`, 150), Drifter (`ce7571e0a4a3`, 234), e6de (`e6de7747fab5`, 33).

Two boulders are not yardsticks. **e6de** is chaotic: the same job places 9, 14 or 21 photos from run to run.
**9f3b** is the hard one: every model of it, by any method, fails the model's own check.

### The app's whole job: the live image against the new one

`bench.py app <boulder> --cond prod [--image ...]` runs the app's own `pipeline.reconstruct()` from photos to textured
mesh. Live is `ghcr.io/tom-f-oconnell/bouldermesh:b5607e9-gpu` (COLMAP 3.9.1's incremental mapper, OpenMVS 2.3 on the
CPU). New is `bouldermesh:global-gpu`, built from 6a9df81 + 53b3c34 (COLMAP 4.2.1's global mapper with CUDA and
cuDSS, untied cameras taken out for the rest step, OpenMVS 2.4 with CUDA depth maps and 2.3's own fusion and meshing
values, people masks on the GPU, the second mask pass warped from the first). Runs `r2/<boulder>/app/<image>@prod`.

| | live | new | faster | CPU-seconds, live / new | photos placed | self-check, pairs' degrees |
|---|---|---|---|---|---|---|
| 31de | 1,393 s | 538 s | 2.6x | 10,358 / 3,375 | 71 / 71 | 2.51 / 2.79, both pass |
| 9f3b | 2,752 s | 780 s | 3.5x | 18,934 / 4,669 | 130 / 137 | 10.65 / 10.20, both fail |
| Drifter | 3,376 s | 1,276 s | 2.6x | 23,607 / 7,746 | 143 / 142 | 2.79 / 3.19, both pass |

Per step, seconds, live / new:

| | people | features | matching | mapper | rest | people, undistorted | dense | mesh | texture |
|---|---|---|---|---|---|---|---|---|---|
| 31de | 23 / 8 | 119 / 120 | 45 / 44 | 691 / 96 | 32 / 38 | 19 / 1 | 310 / 54 | 28 / 31 | 118 / 138 |
| 9f3b | 44 / 12 | 205 / 203 | 97 / 97 | 1,776 / 86 | 34 / 22 | 52 / 2 | 359 / 124 | 41 / 48 | 134 / 175 |
| Drifter | 63 / 16 | 250 / 250 | 220 / 220 | 1,608 / 161 | 92 / 55 | 37 / 2 | 691 / 182 | 57 / 46 | 349 / 334 |

- The new image with its incremental mapper (`MAPPER=incremental`) on 31de: 1,114 s, mapper 684 s, the other steps
  within a few seconds of the global run. So the mapper is 595 s of the 855 s saved there.
- Meshes are as dense as before: fused points and faces, live / new: 31de 2.14M / 2.59M and 0.96M / 1.07M; 9f3b
  2.64M / 2.78M and 1.51M / 1.60M; Drifter 4.97M / 4.58M and 1.55M / 1.31M. Median triangle edge near the rock, of the
  scene's size: Drifter 0.178% / 0.175%, 31de 0.50% / 0.52%.
- In the new image on Drifter the global mapper solved its 151-photo model on CUDA with cuDSS (14 solves) and three
  side models of 8-9 photos on the CPU (44 solves); it peaked at about 0.45-0.6 GB of GPU memory. No job fell back to
  the incremental mapper or to the CPU.
- With the mapper fast, a Drifter job's largest steps are texture (334 s), features (250 s, 43% of its CPU-seconds),
  matching (220 s), dense (182 s) and the mapper (161 s).

### The mappers, on one set of features

Each on the same features (the app's SIFT, made once on hal), seed 0. Mapper seconds, and photos placed after the
app's rest step (`bench.py table`). base is COLMAP 3.9.1 in the app's image; the rest are COLMAP 4.2.1 on the
benchmark's verified "complete" build.

| Method | 31de | 9f3b | Drifter | Verdict |
|---|---|---|---|---|
| base, two runs | 584 / 660 s, 71 | 1,608 / 1,300 s, 136-137 | 1,675 / 1,647 s, 142 | the yardstick |
| global | 93 s, 71 | 118 s, 143 | 187 s, 150 | fast, but see untied cameras |
| **global_tied** | **112 s, 71** | **86 s, 141** | **186 s, 142** | **what the app now does** |
| caspar | 505 s, 71 | 589 s, 135 | 685 s, 142 | wrong on 31de |
| cpu421 (4.2.1 incremental, CPU) | 593 s, 65 | 1,302 s, 136 | 1,489 s, 143 | no gain |
| base with OpenBLAS | 755 s, 57 | 1,976 s, 137 | 1,499 s, 142 | no gain; split 31de's model |
| vggt start | 228 s, 55 | 199 s, 92 | 183 s, 134 | places fewer; the 12 GB card fits 44 photos |

- **caspar's model of 31de is wrong**: 13% of its cameras look within 2 degrees of any other model's, and the
  self-check reads 26.9 degrees where the eight others read 2.0-2.9. It placed the usual 71 photos. On Drifter it is
  fine. Caspar runs in float32 unless COLMAP is built with `-DCASPAR_USE_DOUBLE=ON`; not tried. A second run
  (`caspar-s0+complete@prod-r2`, 548 s) is wrong again and differently: 7.2 degrees (median) from base, 23 from the
  first run, check 9.8. With the pipeline's Caspar build, that is three Caspar models of 31de, none right.
- **The baseline can be silently wrong too**: its second run on 9f3b placed 137 photos in a model 56 degrees (median)
  from every other.
- `global_one` (no side models): mapper 117 s on Drifter and 88 s on 31de, same photos placed, self-check 3.36 and
  2.67, surface within the seeds' spread. Passes.

### The global mapper's untied cameras

Its models carry cameras that see none of the model's points: 3 of 71 on 31de, 18 of 143 on 9f3b, 19-20 of 150 on
Drifter. They are in its own output, they count as placed, and they are its wrong cameras (12-20 degrees from the
baseline's, where its cameras seeing 500 points or more are within 2 degrees for 89-96%). So "global places more
photos" was not real: counting cameras that see 50 points or more it has 68 / 120 / 129 against the baseline's 71 /
136 / 141. No incremental model has any. `global_tied` deletes cameras seeing fewer than 15 points before the rest
step, which then places them as it does any photo left out; it ends with the baseline's photos exactly on 31de and
Drifter. The global mapper isn't repeatable either: the same seed left 3 and then 6 untied on 31de.

### How models are judged

- **Photo counts don't show a wrong model** (caspar on 31de, base run 2 on 9f3b: both placed the usual number).
- **Comparing camera rotations overstates disagreement.** With a lens per photo and the principal point refined, a
  camera can turn a few degrees and shift its principal point to match. On 31de the baseline's two runs have 79% of
  cameras within 2 degrees by rotation and 100% by where their pixels look. `consensus.py` compares five viewing rays
  per camera, after laying one model over the other on the largest set of cameras that agree.
- **Where cameras stand is weaker evidence still**: a camera can stand further off with a longer lens and see the
  same. On Drifter, global against base, the gap between a camera's two positions lies 76% along its line of sight
  and its focal lengths follow its distances with slope 1.04, correlation 0.98 (`consensus.py trade`).
- **The model's own check** (`incr/selfcheck.py`, now in the app): the turn between two cameras in the model against
  the turn their photos' own two-view geometry gives. It agrees with the comparison between models on every model
  here: 2.0-3.5 degrees for those that agree with each other, 9.6-15.3 for every 9f3b model, 26.9 for caspar on
  31de. Its line is at 5. It needs only the model and its database. It must be read against one database: GPU SIFT's
  model read 4.40 against its own and 2.91 against the reference's.
- **The surface** (`surface.py`): two meshes laid over each other and the distance between them, near the rock, as a
  share of the scene's size (the median distance of the cameras from their middle). Until 19c334e it started only
  from the cameras' fit, which was 25% off on one 31de pair, and its ICP shrank a mesh on another: the 2-7% it gave for
  three 31de pairs were the tool's. It now tries starts from the models' own points too, matches both ways and keeps
  the best fit; those pairs are measured again below (`atlas/surface-31de-1115.md`).

### Is the global mapper's model right?

Surface gap near the rock, median, both meshes through the same OpenMVS:

| | Drifter | 31de |
|---|---|---|
| base run 1 against run 2 | 0.13% | 0.36% |
| global_tied seeds against each other | 0.13%, 0.14%, 0.26% | 0.35%, 0.54%, 0.82% |
| global_tied against base | 0.36-0.44% | 0.34-0.76% |

- On 31de global's surface is within what its own seeds and the baseline's runs spread.
- On Drifter it is a steady 0.4% from the baseline's, about three times either mapper's own spread, with 13-15% of the
  surface more than 1% away and under 1% of it more than 2%. If cameras stand 4 m off (assumed; there is no measured
  scale) that is 1.5 cm at the median.
- **That gap is a smooth bend, not lost detail.** It grows from 0.23% near the rock's middle to 0.51% at the edge, and
  fitting patches of 5% of the scene's size each on its own takes it from 0.38% to 0.16% (floor 0.10-0.14%).
- Rays within 2 degrees of the other models, median: Drifter base 94%, global_tied 85%; 31de both 93%.
- Which mapper is nearer the real rock is not known: nothing here measures truth. The gravity readings that could
  have were in 1, 0 and 0 of these boulders' photos.

### OpenMVS 2.4 against 2.3

Same model (Drifter, base run 1; 31de in brackets where measured):

| | dense | fused points | mesh | faces | texture | after the mapper, in all |
|---|---|---|---|---|---|---|
| 2.3 on the CPU | 721 s (358) | 4.95M (2.30M) | 59 s | 1.60M (1.04M) | 346 s | 1,131 s (529) |
| 2.4 with CUDA, as it comes | 174 s (60) | 0.99M (0.55M) | 32 s | 1.05M (0.71M) | 297 s | 507 s (186) |

And one Drifter `global_tied` model through 2.4 four ways:

| | dense | fused points | mesh | faces | texture | in all |
|---|---|---|---|---|---|---|
| as it comes | 155 s | 0.63M | 20 s | 0.70M | 233 s | 411 s |
| `fuse1` (`--fusion-filter 1`) | 166 s | 7.38M | 138 s | 2.50M | 1,087 s | 1,394 s |
| `like23` (+ `--number-views-fuse 3 --postprocess-dmaps 7`) | 182 s | 4.71M | 79 s | 2.23M | 532 s | 797 s |
| **`as23`** (+ `--min-point-distance 2.5` in the mesh step) | 161 s | 4.73M | 50 s | 1.28M | 316 s | 530 s |

- 2.4's speed as it comes is partly thinner output: a fifth of the points. Its defaults changed in four places; with
  2.3's values back (`as23`, what the app passes) it makes 2.3's cloud and a like mesh in under half 2.3's time.
- The surface doesn't depend on these: `like23` against 2.4 as it comes, same model, is 0.13% apart (floor 0.09%).
- `as23g1` (one geometric-consistency pass, not two): dense 161 to 101 s on Drifter, 66 to 52 s on 31de; surface
  0.11% and 0.27% from `as23`'s, at the floor. Passes.
- **2.4's dense step segfaulted twice in about 65 runs**, both on Drifter, both while fusing depth maps: once with its
  own fusion, once with `as23`'s. Eleven reruns of the second on its saved depth maps didn't crash. 2.3 on the CPU never
  crashed in about 15 runs. The app falls back to the CPU build; that path hasn't been seen after a real crash.
- 2.4 gives no depth map to a camera that sees no points.

### Throughput: jobs side by side

`bench.py together`: Drifter, 9f3b and 31de as whole app jobs in `bouldermesh:global-gpu`, with no lock on the GPU.

| | wall | jobs an hour | against one at a time | card's peak |
|---|---|---|---|---|
| one at a time | 2,617 s | 4.13 | | 4.2 GB |
| two, six cores each | 2,061 s | 5.24 | +27% | 6.8 GB |
| two, every core each | 2,033 s | 5.31 | +29% | 6.8 GB |
| three, every core each | 1,946 s | 5.55 | +34% | 7.1 GB |
| three, four cores each | 4,484 s | 2.41 | -42% | 4.9 GB |

- atlas runs out of CPU, not GPU: shared, Drifter's features went from 251 s to 480-650 s. A machine with more cores
  should gain more; not measured.
- Splitting the cores three ways made Drifter's texture step take 2,626 s (318 alone). Don't cap a job's cores when
  running several.
- A job's CPU-seconds, which is what a shared machine's throughput follows, fell 3-4 times with the new image.

### Making a job faster without making it worse

The plan and its rules are in `experiments/mapper/optimization-plan.md`. A change passes if it places as many photos,
keeps the model's check, keeps the cameras and the surface within what seeds differ by, and costs less wall time and
CPU-seconds. All with `global_tied`; reference numbers in brackets.

| Change | What it saves | Photos placed | Model | Verdict |
|---|---|---|---|---|
| texture from every second photo (`views2`) | texture 335 to 159 s on Drifter, 150 to 80 s on 31de; CPU-seconds halved | same mesh | sharpness number 637 (634) on Drifter, 244 (366) on 31de | for Tom's eye |
| every third (`views3`) | 113 s, 65 s | | 611, 210 | for Tom's eye |
| `dec50`, `noseams` | 199 s, 288 s on Drifter | | half the faces; visible seams | not pursued |
| half-size photos (`res1`), virtual faces (`vface3`) | slower: 407 s, 427 s | | | no |
| a video's repeated frames dropped (`dedup90`) | Drifter 51 of 176 frames; mapper to texture 782 to 487 s | no kept photo lost on Drifter, 1 on 31de | check 2.82 (3.43), 2.37 (2.55); surface 0.37% on Drifter (seeds to 0.26%), 0.62% on 31de (within seeds) | passes. Drifter's surface gap is 1.4 times the widest seed pair, but it is the same smooth bend seeds have: patches of 5% fitted alone go 0.36% to 0.14% (seed against seed: 0.29% to 0.13%) |
| `dedup75` | 60 frames | loses 7 kept photos on 31de | | no |
| plain SIFT on the GPU (`siftgpu391`) | extraction 250 to 8 s on Drifter, 2,957 to 12 CPU-seconds | 159 (142), 74 (71), 9f3b 133 (141) | check against the reference's database 2.91 (3.43), 2.94 (2.55); rays within 2 degrees of the reference 82%, 81% (seeds 95-97%); surface 0.26% on Drifter, 0.30% on 31de, both within the seeds' | passes the surface rule on both; still a different set of cameras by more than seeds differ (a fifth of 31de's shared cameras over 5 degrees off), and it changes which photos are placed. Tom's |
| matching a list of pairs (a video's neighbours + the K most alike by CLIP) | matching 220 s to 39 / 54 / 82 s on Drifter for K = 20 / 30 / 50 | 137 / 138 / 139 (142); 71 / 71 / 71 | surface 0.29% (K 20), 0.18% (K 50) on Drifter; 0.50% / 0.33% / 0.44% (K 20 / 30 / 50) on 31de, within its seeds' 0.35-0.82% | passes the surface rule; places 3-5 photos fewer on Drifter, which rule 1 doesn't allow |
| one geometric pass (`as23g1`) | dense 161 to 101 s on Drifter, 66 to 52 s on 31de | same model | surface at the floor; fused points 4.63M (4.73M) and 2.48M (2.55M), faces 1.27M (1.28M) and 1.09M (1.09M) | passes |
| no side models (`global_one`) | mapper 186 to 117 s on Drifter, 112 to 88 s on 31de | same (142, 71) | check 3.36, 2.67 (3.43, 2.55); surface 0.13% on Drifter and 0.31% on 31de from the reference's, within the seeds' | passes |
| 1600 px pictures for SIFT | extraction 250 to 208 s | same | check 2.96, 2.89 | small gain |
| no guided matching | matching 220 to 186 s, but the mapper 186 to 292 s | 138, 70 | check 2.39, 2.14 | a loss on time |
| COLMAP 4.2.1's own SIFT (`sift421`, the app's settings) | none: extraction 361-566 s on Drifter (the app's 607 s @prod), 313-331 s on 31de (238 s); mapper slower (306 and 262 s against 186 and 112) | 151 (142), 72 (71), 136 (141) | check 2.57 on Drifter; rays within 2 degrees of the reference 88%, 72% | no gain |
| ALIKED + LightGlue (`aliked421`) | extraction 22-33 s, but matching every pair 289 s on 31de (48 s) and 957 s on 9f3b (106 s); **extraction ran out of the 12 GB card's memory on Drifter** (ONNX Runtime, rc 134) | 78 (71) on 31de, 140 (141) on 9f3b | 31de: rays within 2 degrees of the reference 97% | only with a pair list and smaller pictures; Drifter not measured |

- GPU SIFT gained 20 photos and lost 3 on Drifter: 14 of the gained see 200 points or more (one video's run of
  frames), 6 under 100, and `dedup90` would drop 5 of them.
- The pair lists kept 26% / 35% / 50% of the exhaustive set's verified pairs on Drifter: "alike" by CLIP misses real
  overlaps. Embedding took 10 s.

### Close-ups of a hold (simulated)

`sim_closeups.py`: windows round one hold, cut from the photos that see it and enlarged 4 times (or 2 and 4, a
ladder), added to the boulder's photos and run through features, `global_tied`, rest and OpenMVS 2.4 `as23`. An
enlarged window is a longer lens from the same spot: no new parallax and no new texture, so this is easier than real
close-ups. Projects `hold4-31de`, `hold24-31de`, `hold4-ce75`, `hold24-ce75`.

| | 31de, x4 | 31de, x2 and x4 | Drifter, x4 | Drifter, x2 and x4 |
|---|---|---|---|---|
| close-ups placed | 12 of 12 | 24 of 24 | 24 of 24 | 48 of 48 |
| by the global mapper / by the rest step | 0 / 12 | 3 / 21 | 3 / 21 | 19 / 29 |
| a close-up's points seen, median | 547 | 2,751 | 671 | 2,573 |
| looking off, median degrees | 1.2 | 4.6 | 2.0 | 1.5 |
| triangle edge over the hold, of the scene's size | 0.56% to 0.16% | 0.56% to 0.16% | 0.29% to 0.087% | 0.29% to 0.086% |
| mapper, rest, dense, texture seconds (Drifter without: 186, 69, 161, 316) | 139, 48, 86, 253 | 132, 76, 110, 351 | 269, 149, 236, 849 | 377, 376, 299, 1,136 |

The rest step places them, the mesh is 3.4-3.5 times finer over the hold and the same surface, the ladder
quadruples their ties, their cameras look right and stand loose, and one hold's 48 close-ups nearly triple a
Drifter job. What that means for the app is in `design.md` ("What that means for the app's close-ups of a set's
holds").

### The incremental pipeline's first builds

`incr/boulder.py first <boulder> auto 0 --cond prod`, photos to textured mesh, before its fixes of Nov 14:

| | whole | placed | against base, rays within 2 degrees | its own reprojection error |
|---|---|---|---|---|
| e6de | 153 s | 32 of 33 | | |
| 31de | 399 s | 72 of 82 | 68% | 0.77 px |
| 9f3b | 971 s | 133 of 150 | 0% | 5.70 px |
| Drifter | 1,404 s | 148 of 234 | 94% | 2.88 px |

Good on Drifter, loose on 31de, wrong on 9f3b without saying so. The high errors came from its VGGT assist step
spoiling old points (found from these logs on hal, fixed in 2511e7f). With its fixes (a5844d0; `first-<start>-s0[-caspar]@prod-v2`), photos to textured mesh. Ceres on the CPU unless said:

| | start | whole | placing | placed | its own check: pairs' degrees, error | against base, rays within 2 / 5 degrees |
|---|---|---|---|---|---|---|
| e6de | VGGT | 129 s | 27 s | 32 | 2.04, 1.10 px: passes | |
| 31de | global (VGGT had room for 42 of 72) | 662 s | 237 s | 72 | 2.54, 0.72 px: passes | 90% / 97% |
| 31de, VGGT start forced | VGGT | 724 s | 245 s | 72 | 2.07, 0.76 px: passes | 66% / 99% |
| 31de, Caspar for every solve | global | 459 s | 103 s | 72 | **3.42, 1.10 px: passes** | **0% / 1%, median 6.7 degrees** |
| 9f3b | global, then incremental: both fail, the less bad kept | 2,863 s | 1,904 s | 124 | 11.13, 1.25 px: fails, and says so | 31% / 48% |
| Drifter | global | 2,375 s | 952 s | 171 | 2.49, 0.94 px: passes | 91% / 96% |
| Drifter, VGGT start forced | VGGT | 1,775 s | 493 s | 149 | 2.45, 0.84 px: passes | 93% / 96% |

- **Drifter: 171 placed where the baseline places 142**, and its shared cameras are as close to the baseline's as the
  baseline's own runs are to global's. Of its 34 photos the baseline doesn't place, 28 see 100 points or more (median
  654; the model's 2,397). It misses 5 the baseline places.
- **9f3b now says it failed** instead of returning a wrong model as if it were right, at three times the time.
- **Caspar again gives a model of 31de that nothing else agrees with, and this time the model's own check passes
  it** (3.42 degrees, under the line at 5; the Ceres runs read 2.1-2.5). So a pass is not proof, and Caspar on 31de
  stays ruled out. This is the second such model (the first: `caspar-s0+complete@prod`, which the check fails at 26.9).
- Placing is slow now: Ceres on the CPU takes 237 s on 31de where Caspar took 59 s before the fixes, and 952 s on Drifter.
- A first try of these (before a5844d0) failed on every start but VGGT's: the check couldn't be imported in the tools'
  container.

### Problems met on atlas

- **A running container lost the GPU** when systemd reloaded its units (a snap refresh): with `--gpus`, the device
  is added behind the runtime's back. The benchmark uses CDI where Docker has it and runs a step again once. CDI is
  still not switched on in atlas's Docker.
- The MVS=24 image didn't build until its stage installed libjxl (e5bf07a); the colmap4 stage's check read COLMAP
  4.2.1's help from stdout, where 4.2.1 writes it to stderr (53b3c34).
- `together report` says "no mesh made" of every app job that made one.

### Not measured, or not to be trusted

- Anything on hal's 48 threads and RTX 3090: every timing here is atlas's.
- The global mapper on the CPU alone, OpenMVS 2.4's depth maps on the CPU, the old build against the complete one: on
  hold ("Benchmarking left on hold").
- ALIKED on Drifter: its extraction ran out of GPU memory; its `global_tied` run (32 placed) is of a partial database and
  means nothing.
- The model check read a mean reprojection error of about 1e150 px for the `aliked421` and `sift421` models of 31de (their
  90th percentiles are 1.7 px), and so failed them on "error": one value it reads is not a real error. Until that's
  found, a model the check fails on "error" alone needs a second look.
- The v3 and native builds, the hierarchical mapper, the thinned database, thread counts, 38f3: not run.
- Why the dense step holds one CPU thread at 100% and the GPU at 25%.
- Texture quality: there is no number that settles it.
- Scale: every distance is a share of the scene's size.

## The incremental pipeline (adding photos to an existing model)

<!-- owner: hal -->

`experiments/mapper/incr`: a pipeline on pycolmap 4.2.1 where building a boulder and adding photos to one are the
same call; the next stage after the batch pipeline, and the basis for meshing the phone apps' captures. Not in the
app. Its first builds on atlas are in "The incremental pipeline's first builds" above and the close-ups in "Close-ups
of a hold (simulated)"; `incr/README.md` has the rest.

- **What works** (hal, development runs: 6 cores, one seed, indicative). An add leaves the old cameras exactly where
  they were; added cameras land 0.4 to 1 degree (median) from a from-scratch build's, because holding the old model
  keeps its errors. Adding nothing or duplicates changes nothing, photos of another rock are left out, and an add
  killed midway leaves the last state usable. A state costs about 2 MB a photo besides the photos.
- **What failed, and was fixed** (2511e7f, b861520). The first builds' high errors came from the VGGT assist merging
  old photos' tracks at loose tolerances; a guard held every lens from the first refinement on; on atlas's 12 GB card
  VGGT started from 42 photos; Caspar solved all but the last refinement. Now the assist touches only the added
  photos' points, `auto` starts from the global mapper when VGGT has room for under two thirds of the photos, every
  solve is Ceres on the CPU, and each model checks itself before it is kept.
- **Run with the fixes, on atlas (a5844d0; the table is in "The incremental pipeline's first builds"):** all seven
  builds ran through. 31de from the global start: 662 s, 72 placed, passes its check, 90% of cameras' rays within 2
  degrees of the baseline's (66% from the VGGT start, so sending 31de to the global start was right). Drifter from
  the global start: 2,375 s, **171 placed** (baseline 142), passes, 91% within 2 degrees; 28 of its 34 extra photos
  see 100 points or more. 9f3b fails its check from both starts, as every model of it does, keeps the less bad one
  and says so. Placing with Ceres on the CPU is 4 to 5 times slower than it was with Caspar (237 s on 31de, 952 s on
  Drifter).
- **Against the model's check as it stands:** with Caspar's solves, 31de's model passes the check (3.42 degrees, under
  the line at 5) and is 6.7 degrees (median) from the baseline, with 1% of its cameras within 5. Atlas marks it "to
  check" (its overlay could be at fault; that model's surface isn't measured). If it holds, a line nearer 3.5 would
  have caught it and still passed every model that agrees with the others (2.0 to 3.5). The same check is in the app.
- **Never run:** a capture's add, refit, capture-first texture, re-levelling, hold detail meshes and card scale, on
  simulated or real captures. They are written and unit-tested only.
- **The simulated close-ups' advice for whatever meshes a capture** (`design.md`, "What that means for the app's
  close-ups of a set's holds"): keep the rest step in that path (it placed three quarters of them); keep the ladder's
  needed steps; hold an app photo's own lens in the solve; mesh each hold's region by itself.

## Where things are

<!-- owner: hal -->

| What | Where |
|---|---|
| The handoff file: the two sessions' log | `/local/tensor/Tom/bm_opt_handoff.md` on hal, `/mnt/tensor/Tom/bm_opt_handoff.md` on atlas |
| Shared inputs, hal's first results, atlas's tables | `bm_opt_handoff/` beside it: `projects/`, `runs/`, `cache/`, `hal/`, `atlas/` (`consensus-*.md`, `surface-*.md`, `selfcheck-*.tsv`, `holdsim-1115.md`, `benchmarking-atlas.md`, `incr/`) |
| Meshes exported for viewing | `bm_opt_handoff/atlas/meshes/`: `<boulder>-<run>-<post>.glb`, `<boulder>-app-<image>.glb`, `<boulder>-texture-<variant>.glb`, `hold*-*.glb` |
| The benchmark on atlas | `/mnt/d0/mapperbench`: `r2/<boulder>/<feature set>/<run>`, `r2/<boulder>/app/<image>@prod`, `incr/r/`, `together/`, `chain.log`, `STATE.txt` |
| The benchmark on hal | `/data/tom/mapperbench`: `r2/`, the tool trees `build-plain` and `build-incr-full`, `incr/`, `HELD.txt`, `STATE.txt` |
| The plan and its rules; the first review | `experiments/mapper/optimization-plan.md`; `experiments/mapper/review.md` |
| What the app does, and why | `design.md`: "Depth maps on the GPU", "The global mapper" |

**The handoff file's conventions:** append only, in one write (`cat >> file <<'EOF'`), never edited in place (atlas's
NFS mount caches attributes for 30 minutes and has no locking). Each message is a section `## <n>. From atlas|hal,
<date time>: <subject>`. Numbers clash when two messages cross (12, 32, 45, 48 and 49 each exist twice): the times
tell them apart. Later sections correct earlier ones.

**Scripts** (`experiments/mapper`):

| Script | What it does |
|---|---|
| `bench.py` | one step or one job in a container, timed, with the machine sampled; `app` runs the app's own pipeline in any image; `table`, `apps`, `texture report` and `together report` print results |
| `chain.py` | runs queue files (`queue-*.txt`) one job at a time, detached (systemd user unit `bm-bench`) |
| `variants.py` | the methods, the derived feature sets (dedup, pairs, lens, thin), the texture variants, `together` |
| `consensus.py` | models compared by their cameras' viewing rays; `trade` tests the trade of lens for distance |
| `surface.py` | two meshes laid over each other, and the gap between them; `test_surface.py` beside it |
| `incr/selfcheck.py` | a model checked against its own photos; `table` runs it over every model under `BENCH` |
| `meshes.py` | depth maps, fused points, faces and seconds from OpenMVS's logs; `export` copies the GLBs out |
| `verify_build.py`, `sim_closeups.py`, `incr/boulder.py` | the check a tool tree must pass to be timed; the simulated close-ups; the incremental pipeline |

Last updated: 2026-11-15 23:55 PST, through section 67 of the handoff file.

## Benchmarking left on hold

<!-- owner: atlas -->

On 2026-11-15 Tom put the rest of the benchmarking on hold after the feature-set runs: atlas is the one GPU he has
free rein over, and it is to make meshes now. Nothing below is queued. Each item says what it would settle, what to
run, and about how long it takes on atlas. Run them through `chain.py <queue file>` (one job at a time, nothing else
on the machine), judge by the rules in `experiments/mapper/optimization-plan.md`, and add the results to "Results on
atlas".

**What's left on atlas to resume with** (cleared on 2026-11-15 at Tom's word): the build trees (`build-plain`,
`build-complete`, `build-incr-full` in `/mnt/d0/mapperbench`), every run's records, logs, models, meshes (GLB, PNG,
`scene_dense_mesh.ply`) and features databases, and VGGT's weights. Deleted: depth maps, dense point clouds and
undistorted photos of every post folder (a texture or OpenMVS experiment on an old post now needs its post run again),
the app runs' job folders, BuildKit's cache, and the Docker images (`docker pull nvidia/cuda:12.9.1-devel-ubuntu24.04`
and `ghcr.io/tom-f-oconnell/bouldermesh:b5607e9-gpu` before any run; the app images are built again from the
Dockerfile).

### The CPU fallbacks (`experiments/mapper/queue-cpu-held.txt`, 12 jobs, 2-4 h)

What a machine with no GPU, or a GPU with no room, would get. The app falls back to these paths; none has been timed.

- **The global mapper on the CPU alone** (`global_tied_cpu` on Drifter twice, 31de, 9f3b, with `as23` meshes). Settles
  how much of the mapper's 10x gain needs the GPU, and whether a CPU-only worker should use it or the incremental
  mapper. Compare its mapper seconds and placed photos with `global_tied`'s (112, 86, 186 s) and base's.
- **OpenMVS 2.4's depth maps on the CPU** (`post --mvs as23cpu` on base run 1 of 31de and Drifter). Settles whether
  the fallback after a GPU failure is slower than OpenMVS 2.3 was (721 s on Drifter), which it replaces.
- **The old build against the complete one** (`cpu421` and `global_cpu` on the plain tree, e6de, 31de, 9f3b). Settles
  what SuiteSparse is worth to Ceres on the CPU. 9f3b's `cpu421` without it has no estimate and may take hours: run it
  last.

To run: `chain.py queue-cpu-held.txt`.

### Seeing the fallbacks work

- **OpenMVS 2.4's dense step segfaulted twice in about 65 runs.** The app then runs the CPU build. Nobody has seen
  that path after a real crash: make the GPU dense step fail on purpose in a job (a hal session) and check the job
  finishes and says so.
- **What crashes it** is unknown. A backtrace needs a crash under gdb; it took 11 reruns of one crashed step without
  one, so loop a Drifter dense step some hundred times (2 min each) with core dumps on.

### Not run at all

- **Jobs at once on hal** (`queue-together-app.txt`, `queue-threads-hal.txt`): atlas gained 30% and ran out of CPU;
  hal has 48 threads. Needs the hold on hal lifted and the image built there. This is the largest untested gain.
- **The passing changes together in one job**: one geometric pass, repeated frames dropped, texture from every second
  photo, each measured alone. One `bench.py app` run each on 31de and Drifter once they are in an image.
- **Repeated frames dropped before extraction** (plan step 2, second half): not written. `dedup90` as measured drops
  them after matching, so it saves nothing on features and matching yet.
- **Why the dense step holds one CPU thread** with the GPU at 25% (plan step 9): a few stack samples of
  `DensifyPointCloud` during a run.
- **The model check's line** (5 degrees): a Caspar model of 31de passes at 3.42 and is 6.7 degrees from the
  baseline's. Its surface against base first, then every model on both machines against a line of 3.5.
- **A real close-up capture** of a hold: the simulation can't show new parallax or texture (`design.md`).
- **The v3 and native builds** (AVX2, LTO), the hierarchical mapper, the thinned database, thread counts on atlas,
  38f3: dropped as unlikely to matter now that the mapper is 13% of a job. `queue-hier-atlas.txt`, `queue-thin.txt`
  and `queue-threads-atlas.txt` are there if that changes.
- **CDI in atlas's Docker** (`setup-gpu-host.sh`, needs sudo): until it is on, a systemd reload can take the GPU from a
  running container. Worth doing before atlas makes meshes for the site.

