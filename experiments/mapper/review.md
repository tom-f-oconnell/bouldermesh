# Review of the mapper benchmark plan (2026-11-13)

For whoever carries the benchmark on. Written after the first boulder's runs (e6de7747fab5, 33 photos), from the
server's saved timings, the scripts here and COLMAP 4.2.1's source as built in /data/tom/mapperbench/build/opt/colmap.

## Where a mesh's time goes

From meshes' timings.json on boulderme.sh: the 7 made since 2026-11-12 (pooled boulders, 85-192 photos), all on hal-2.

| Step | Share of step time | Median | Device now |
|---|---|---|---|
| mapper | 67% (58% over all 24 with timings) | 55 min | CPU |
| texture (TextureMesh) | 11% | 9 min | CPU |
| dense (DensifyPointCloud) | 7% | 5 min | CPU (OpenMVS built without CUDA) |
| people + people-undistorted | 6% | 3 min | torchvision DeepLabV3: check which device |
| rest (image_registrator + bundle_adjuster) | 2% | 16 s | CPU |
| features | 1.4% | 74 s | CPU (no GPU affine shape / DSP) |
| matching | 0.9% | 44 s | GPU |

- A mapper 3x faster makes a whole job 1.8x faster; 10x faster, 2.5x. Past that, texture, dense and people are most
  of what's left, so they're the next targets, not features or matching.
- Those 7 jobs waited 26,700 s for a machine and ran tools for 31,300 s: one machine, one mesh at a time. Anything
  that shortens the mapper shortens the queue as well.
- The mapper averaged 7.6 cores (11,265 CPU-s over 1,482 s on one job), so it's partly serial.
- Mapper time doesn't follow photo count: 85 photos took 614 s, 89 took 2,529 s, 138 took 1,485 s, 111 took 4,071 s.
  Scale estimates by observations and 3D points (and number of models tried), not photos squared.

## Problems with the results so far

1. **The first "GPU" runs didn't use the GPU.** COLMAP 4.2.1 only uses the GPU solver when the problem has at least
   50 images (`min_num_images_gpu_solver = 50` in estimators/bundle_adjustment_ceres.h and global_positioning.h;
   `use_gpu && num_images >= min_num_images_gpu_solver`). The first boulder has 33. Their logs name no GPU, and the
   sampler wasn't running yet. So gpuba's 79 s against cpu421's 108 s is not a GPU speed-up. The mapper has no
   command-line option for that threshold (only `BundleAdjustmentCeres.min_num_images_gpu_solver`, for
   `bundle_adjuster`), so local bundle adjustment (a handful of images) never goes to the GPU, and global only once
   the model has 50. To test the GPU on small problems, set it through pycolmap or patch the default. Report, per
   run, how many bundle adjustments ran on each device.
2. **4.2.1's 2.6x over 3.9.1 looks like less work, not the same work faster.** On that boulder the logs have 114
   "global bundle adjustment" lines for 3.9.1 and 27 for 4.2.1 (lessba 66, gpuba 32, caspar 19), and 1 model against
   2. Find out why (changed defaults such as `ba_global_frames_ratio`, fewer refinements, structure-less
   registration, PoseLib, or just logging) by diffing `colmap mapper -h` between the two and running 4.2.1 with
   3.9.1's values. If it does less, it isn't "the same photos placed" by construction, and the same settings on
   3.9.1 may give most of the gain with no upgrade.
3. **The benchmark's conditions aren't production's.** map.sh gives a run 6 cores and `--Mapper.num_threads 6`, two
   runs at once (queue.sh). The live worker's containers have no `--cpus` at all (NanoCpus 0, only `nice -n 10`), and
   pipeline.py never passes a thread count, so COLMAP and OpenMVS start 48 threads. Time the final comparison one
   run at a time, with the cores and thread settings a job really gets, and never two GPU methods together. Also
   measure production's own setting against an explicit thread count: it may be a free gain, or a loss.
4. **hal was busy** (load average 26-46 of 48 cores during the first runs). One run per configuration isn't a
   timing. Repeat, record the load and others' GPU use (gpusample.py), and report CPU-seconds beside wall time.
5. **The baseline disagrees with itself.** It placed 72 and 59 photos of one set on two seeds (design.md), and made 1
   or 2 models here. "Places the same photos as now" has to mean "within the baseline's own seed-to-seed spread":
   5+ seeds on the small and medium boulders, comparing the sets of photos placed (overlap), not only counts.
6. **Compare what the app keeps, not the mapper's raw model.** After the mapper the app runs `image_registrator`
   and `bundle_adjuster` on the photos left out (`_place_rest`, REST_ARGS), then VGGT if too few were placed. Count
   photos placed after those. The app also keeps the smaller models (`--Mapper.min_model_size 6`, `write_models()`,
   other sides or rocks) and decides boulder or group from each climb's own photos placed: check what each method
   does with photos outside the main model (`GlobalMapper.multiple_models`, `GlobalMapper.min_model_size`), and
   run the app's own decisions on each result. That's what MESH_RULES is about.
7. **The global runs use defaults.** Give them the app's equivalents (`GlobalMapper.min_num_matches 20`,
   `ba_refine_principal_point 1`) and look at `keep_max_num_tracks`, `track_required_tracks_per_view`,
   `ba_min_track_length`, `skip_retriangulation` and `ra_use_gravity` (the app reads iPhone gravity: orientation.py).
   Most web photos have no focal length in EXIF: report results for those separately.
8. **4 h timeout:** production's mapper took 6,232 s on 192 photos with more cores than map.sh gives. Give the
   baseline on Drifter (234) long enough to finish, or there is nothing to compare with.
9. **Reprojection error isn't accuracy.** It's what bundle adjustment minimises, and a model with fewer points
   scores better. Add: camera poses compared between methods after aligning the models; points and mean track
   length; an independent check from the iPhone gravity readings (orientation.check(): how far placed photos'
   readings disagree in the model's frame); and backend/meshmetrics.py on the meshes.

## Approaches not in the plan yet

Roughly in order of gain for the effort.

1. **Settings on the mapper we have** (no new library): `ba_global_max_refinements` (5 by default, each up to 50
   iterations), `ba_global_images_ratio` / `points_ratio` (1.1), `ba_global_max_num_iterations`,
   `ba_local_max_refinements`, `max_num_models`, `init_num_trials`, `max_model_overlap` (design.md: 33% of mapper
   time went on models that weren't the largest), and thread count. lessba is one point in this space and "stopped
   short of a mesh" last time: find out why before dropping it.
2. **A smaller problem for the mapper.** Bundle adjustment's cost follows observations, and 8,192 features a photo
   with affine shape and DSP gives far more than poses need. Run the mapper on a thinned copy of the database (say
   the 1,500-2,500 keypoints a photo with the longest tracks, spread over the image), then `point_triangulator` on
   the full database with those poses and one final bundle adjustment (on the GPU). The global mapper has this
   built in (`keep_max_num_tracks`). May change which weakly connected photos are placed: measure.
3. **Solver choice.** These are one object seen by every camera, so the reduced camera matrix is dense, and a dense
   Schur solver may beat the sparse one well past COLMAP's switch at 50 images (`max_num_images_direct_dense_*`).
   Ceres's CUDA support only moves the factorisation to the GPU, not the Jacobians or the Schur elimination, which
   is likely most of the time: expect little from Ceres on the GPU and more from Caspar (whole solve on the GPU),
   but only on big problems. Check Caspar's precision (float32?) and its overhead on small ones. With OpenBLAS
   compiled in, try `OPENBLAS_NUM_THREADS=1` against threaded: it can fight Ceres's own threads.
4. **VGGT's placements as the starting point for every job, not only the fallback.** vggt_place.py already does
   VGGT → COLMAP triangulation → bundle adjustment, and took 95 s where it ran. It skips incremental mapping
   altogether. Limits: GPU memory with 200+ photos, accuracy before refinement, GPU-only. Other models of the kind
   to try if it's promising: MapAnything, pi3, MASt3R-SfM (check licences: MASt3R's is non-commercial, and the site
   takes donations).
5. **Other global or GPU SfM:** InstantSfM (global SfM on the GPU, research code), NVIDIA's cuSfM (made for rigs and
   video; may not suit unordered web photos). Lower priority than COLMAP's own global mapper.
6. **After the mapper:** texture is the second largest step and has no GPU path in OpenMVS. Look at where its time
   goes (view selection or atlas packing), `--resolution-level`, decimating the mesh first, fewer views, seam
   levelling options, and mvs-texturing as an alternative. People masking runs twice (6%): confirm it uses the
   GPU when one is free, or mask once and warp.
7. **Two jobs at once.** While one job's mapper has the CPU, the GPU is idle: another job's matching, dense or
   VGGT step could run then. Only worth it if cores allow.

## For shipping whichever wins

- **Mapper only, or the whole pipeline on 4.2.1?** The benchmark feeds 4.2.1 a 3.9.1 database. Shipping means either
  both versions in the image (3.9.1 for features, matching and undistorting; only the mapper changes, which is the
  least risk to MESH_RULES, at the cost of image size), or everything on 4.2.1 (then benchmark that from photos too:
  its SIFT, matching and two-view geometry differ).
- **What pipeline.py depends on in 3.9.1:** option names (4.x renamed several, e.g. `--random_seed`), log lines the
  progress bars parse, the error text the seed retry matches ("NumRegImages() >= 2"), "with CUDA" in `colmap -h`,
  `image_registrator`'s REST_ARGS, `model_orientation_aligner`, vggt_place.py's calls, and OpenMVS 2.3 reading the
  model.
- **The benchmark build won't run elsewhere as it is:** `CMAKE_CUDA_ARCHITECTURES=86` is the 3090's generation only
  (the app's Dockerfile uses all-major), and the native variant is hal's CPU only. Check the oldest NVIDIA driver
  CUDA 12.9 accepts, and how much cuDSS, cuDNN and ONNX Runtime add to the image volunteers download.
- **Ceres is pinned to an unreleased commit.** Note it, and whether a release would do.
- **The GPU is shared.** Record each GPU step's peak memory (for `gpu_need_mb()` and GPU_MIN_FREE_MB), keep a CPU
  fallback for every step, and have the job report which device each step used, so the server can show a fallback.
- **A gate by size is fine** (global mapper was slower than incremental at 33 photos), if it's automatic and
  measured: find the crossover.
