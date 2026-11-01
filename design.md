# Design notes: which photos, and which pixels, reach a reconstruction

Measured on Midnight Lightning (Big Columbia Boulder, Camp 4) on 2026-10-30, unless noted. Each
run is the real COLMAP + OpenMVS pipeline at 10 threads. COLMAP's results vary a lot between
identical runs, so anything from one or two seeds is only suggestive.

## Relevance filter (`backend/climbs/relevance.py`)

Web search and geotagged sources mostly return other things: trucks, ravens, signs, valley
vistas, climbing gyms, and other boulders, which are the hard case. Scored against hand labels:
27 clear targets and 71 clear non-targets (8 of them other boulders) from web and Commons
results, with 18 too ambiguous to score, plus the 72 photos and frames from the climb's page
and videos.

| Method | Web targets kept | Non-targets rejected | Other boulders rejected |
|---|---|---|---|
| CLIP zero-shot alone | 27/27 | 52/71 | 0/8 |
| SIFT, anchors = page + videos | 24/27 | 70/71 | 8/8 |
| SIFT, anchors = 9 page photos | 24/27 | 70/71 | 8/8 |
| SIFT, no anchors (largest matching group) | 24/27 | 70/71 | 8/8 |
| SIFT + CLIP, anchors = page + videos | 24/27 | 70/71 | 8/8 |

So the geometric check does the work and needs only OpenCV. CLIP is safe but can't tell boulders
apart, and on top of SIFT it only saves some time, so it stays an optional extra. It may still
be worth having for videos with off-topic stretches, as trusted sources skip the geometric check.
The one non-target kept is a stock photo, linked through another with the same watermark.

**Verification details matter.** Plain ratio-test matches + MAGSAC let a climbing gym score 49
inliers and a flyer 35, because many of their points matched the same few points on the rock
(6 and 5 distinct ones). Mutual matches + a 1 px classic RANSAC drop the highest non-target score
to 12, with 23 of 27 targets above it, hence `MIN_INLIERS = 16`. On a second boulder (Lucid
Dreaming, Grandpa Peabody Boulder, only 2 anchors, no videos) noise also stopped at 12 and real
matches started at 22, though that set isn't hand-labelled: it was checked by eye.

**Chaining.** Confirmed web photos become anchors for the rest. Without it the filter kept 19/27
targets (14/27 with only the 9 page photos) and rejected 71/71, and the second boulder kept 8 of
66 photos rather than 13. It failed with the weak matcher, when false matches spread, but noise
this far below the threshold doesn't propagate.

**Ideas that didn't earn a place:** EXIF GPS (present in 1 of about 190 photos); "rock-like"
colour and texture (targets median 0.76, non-targets 0.62, other boulders 0.66).

The no-anchor case only works because the target dominates these results. It picks the largest
group that matches, whatever that shows, and a pair of photos of the same truck could form one.

## Does filtering help the mesh?

Not much: web photos do, but filtering them mostly saves time. Times are rough, as these runs
shared a machine at low priority.

| Input | Masks | Seeds | Photos placed | Dense points | Faces | Time |
|---|---|---|---|---|---|---|
| Page + videos (72) | yes | 3 | 25, 35, 36 | 0.56-0.72M | 156-207k | 34-36 min |
| + all web + Commons (188) | no | 2 | 38, 73 | 1.14-2.42M | 339-586k | 62-64 min |
| + all web + Commons (188) | yes | 1 | 67 | 2.08M | 551k | 76 min |
| + filter without chaining (95) | no | 2 | 58, 60 | 1.84-1.88M | 471-475k | 35-51 min |
| + filter without chaining (95) | yes | 2 | 59, 63 | 1.77-2.21M | 509-554k | 35-51 min |
| + filter (105) | yes | 2 | 69, 67 | 1.92-2.10M | 526-554k | 46-55 min |

- **Web photos are where the gain is**: about twice the photos placed and 3x the faces. Google's
  search API is closing, so DuckDuckGo's unofficial endpoint is on by default despite being
  brittle (`HARVEST_DDG=0` turns it off): a failed search only costs its photos.
- **COLMAP rejects junk well by itself**: of the 71 non-targets, one got into one model across all
  these runs. So junk costs matching time (which grows with the square of the photos), not quality.
- **The filter saves about a third of the time**, and may make results steadier (unfiltered placed
  38 and 73 photos), though two runs can't show that. It can also only lose photos: without
  chaining it rejected 7 target photos that the best unfiltered run placed.
- **Chaining placed more photos but no more surface here**, and somewhat more fragments. It's kept
  because it stops the filter depending on how many anchors a climb has, which matters more for
  less photographed boulders. That's inferred from the filter scores above: there are no meshes
  from a poorly anchored boulder.

The whole flow through the app, with web search on and the filter without chaining, took 70 minutes
on 20 CPUs alongside other jobs, placed 63 of 100 photos and made a 592k-face mesh.

## People (`backend/occlusion.py`)

Nearly every photo of a boulder problem has a climber or spotter in it (6.6% of the pixels on
average, up to 31%). Options tried, on the 72 page and video photos:

| Variant | Seeds | Photos placed | Faces | Reprojection error | People painted on |
|---|---|---|---|---|---|
| Baseline | 3 | 28, 28, 25 | 135-139k | 1.13 | 0.94%, 0.51%, 0.00% |
| Mask features only (COLMAP) | 2 | 28, 29 | 130-137k | 1.08 | 0.01%, 0.23% |
| **Mask all stages** (COLMAP, densify, texture) | 3 | 25, 35, 36 | 156-207k | **1.03, 1.03, 0.99** | **0.01%, 0.03%, 0.00%** |
| Drop photos over 15% people | 2 | 32, 35 | 164-201k | 1.05, 1.11 | 0.05%, 0.27% |
| Drop + mask all stages | 4 | 31, 32, 33, 28 | 126-169k | 1.03, 1.00, 1.04, 1.08 | 0.01%, 0.20%, 0.00%, 0.11% |
| Require 5 agreeing views (not 3) | 1 | 28 | 108k | 1.13 | 0.00% |

*People painted on* is DeepLab run over the texture atlas, counting only pixels that mesh
triangles use. It's noisy (see the baseline's 0.00%), and its atlas patches aren't natural images,
so it's a rough guide. Don't look at the raw atlas instead: it stores whole rectangles, so it shows
climbers that are never mapped onto the mesh. Running a person detector on rendered views was
worse still: it reported 39% "person" on plain rock.

Masking at every stage is the one to use: lower error in every run, more surface in two of three,
and climbers neatly cut out of the texture. It held up on web photos too: with the filter, masks
took faces from 471-475k to 509-554k, and painted-on people from 0.40% and 0.21% to 0.03% and 0.15%. Dropping photos helped alone, but added nothing on top
of masking. Stricter fusion removes people by removing a fifth of the surface. Overall shape
barely changes in any variant: the limit is which sides of the rock the photos saw.

DeepLabV3 MobileNetV3 was as accurate as Mask R-CNN by eye and 10x faster (0.2 s vs 1.8 s per
photo on a CPU), and found a climber Mask R-CNN missed. It only knows people: crash pads, packs
and shadows stay in. A static-camera motion mask could catch those on video frames, but wasn't
tried, as most of these videos pan.

Masks can't be undistorted by COLMAP along with the photos, so they're computed twice: on the
originals for COLMAP, and on the undistorted photos for OpenMVS.

COLMAP's mapper aborted in 2 of 25 runs (`NumRegImages() >= 2`), both with masks on, out of 16 such
runs against 9 without. That's too few to blame masks, but one of them left only a 5-photo model,
so the pipeline retries with another seed. The retry doesn't repeat matching.

## Where the time goes

One Midnight Lightning job through the app on 2026-10-31: 113 photos, every core of a shared
machine, CPU only, 49 minutes in all. The mapper and matching are over half of it.

| Step | Time |
|---|---|
| Find and download photos | 7 min |
| Video frames | 47 s |
| De-duplicate and filter | 3.8 min |
| Viewpoint check | 41 s |
| Find people (twice) | 35 s |
| Features | 33 s |
| **Matching** | **12.2 min** |
| **Mapper** | **14.9 min** |
| Densify | 2.4 min |
| Mesh | 34 s |
| Texture | 5.2 min |

Mountain Project's photos were already cached in that job. On a new climb its 60 s Crawl-delay adds
about 13 minutes (a page and 12 photos). Matching also shrinks with the square of the photos, so a
less photographed climb looks quite different: White Rastafarian kept 59 and took 31 minutes, of which
21 were the harvest and 10 the reconstruction.

**Matching** tries all 6328 pairs of those photos, and 30% have 15 or more inliers.

| Matching | Time | Useful pairs | Inliers |
|---|---|---|---|
| CPU, guided (the app) | 12.2 min | 1889 | 1.01M |
| COLMAP 4.2.1 on the 3090, guided | 60 s | 1680 | 0.92M |
| COLMAP 4.2.1 on the 3090, unguided | 45 s | 1717 | 0.59M |

The GPU figures are with another user's job holding the card at full load, and took about 3 GB of its
memory. They're different builds with different features, which may explain the 11% fewer pairs. On a
36-photo sample on CPU, turning guided matching off halved the time and kept the useful pairs, and
halved the inliers. Cutting features from 8192 to 4096 saved 37% but lost 12% of the pairs.

SIFT stays on the CPU however it's built, as COLMAP has no GPU version of the affine shape and
domain-size pooling these settings use. It only takes 33 s.

**The mapper** spends 91% of its time in bundle adjustment (71% in global), and 33% on models that
aren't the largest and so are thrown away. All from the job's own matches:

| Mapper | Threads | Time | Photos placed | Error |
|---|---|---|---|---|
| COLMAP 3.9.1 (the app's settings) | 8 | 34-35 min | 72, 59 | 1.03, 0.98 |
| Global bundle adjustment less often | 8 | 11-13 min | 78, 77 | 1.02, 1.14 |
| COLMAP 4.2.1 `global_mapper`, CPU | 8 | 2.7-3.5 min | 110, 110, 110 | 1.24, 1.30, 1.19 |
| The same from GPU matches | 10 | 3.6 min | 110, 110 | 1.03, 1.02 |

Those runs shared the machine at low priority, so compare them with each other, not with the table
above. "Less often" is `--Mapper.ba_global_images_ratio 1.4 --Mapper.ba_global_points_ratio 1.4
--Mapper.ba_global_max_num_iterations 25`, and it stopped short of a mesh.

The global mapper from GPU matches went on to make meshes of 0.99M and 1.04M faces, with 86-87% of the
surface in one piece, against 1.06M and 74% for the job's. By eye, each is one coherent rock face with
fragments around it, as good as the job's or cleaner. That's only two runs on one boulder. It also
needs work to use:

- OpenMVS 2.3 aborts on 4.2.1's models. Writing the model as text and undistorting it with 3.9.1 works.
- It left one photo with camera settings the undistorter wouldn't convert (focal length 1560, likely
  an unrefined guess), and OpenMVS aborts on those too. That photo has to be dropped first.
- It warns that it relies on focal length priors, which most web photos lack.
- The official image's Ceres has no CUDA, so its bundle adjustment stays on the CPU regardless.

**The GPU is shared, so it can't be assumed.** COLMAP aborts outright on a CUDA out-of-memory error,
which happened when other jobs took 23.9 of its 24 GB, so anything using it needs a CPU fallback.

Not tried: downloading from different hosts in parallel, keeping each host's rate limit; choosing
pairs instead of trying all of them.

## Meshing a whole area (`backend/server.py`, "campaigns")

**Staying within Mountain Project's terms decides most of this.** They disallow automated or bulk
scraping, and robots.txt asks for 60 s between requests. So an area is listed with one request to
MP's own Route Finder CSV export, and a climb's page is only read when its turn comes.

**"Popular" means MP's ranking, not a tick count.** Ticks are only on each climb's stats page, so
ranking by them would take a request per climb before meshing anything: about 17 hours of crawling
for Joshua Tree's list. The export can sort by MP's `popularity` instead. What goes into that isn't
documented: its top Joshua Tree results are the area's classics, but it also tracks stars closely, so
it isn't purely ascents. The export has average stars but no vote counts, so *Highest rated* can put a
problem with one enthusiastic vote ahead of a real classic.

**"Most photos and videos" looks a few pages ahead.** Reading every page to rank them is the same
crawl, so this reads the next 8 popular climbs' pages and takes the richest, counting a video as 20
photos, about what one gives after filtering. Each later climb then reads one new page, which it
would have read anyway. It's a local ordering: the richest climb of the whole area may wait its turn.

**One mesh per boulder.** The 1000 most popular Joshua Tree problems are on 364 lowest-level areas,
nearly all single boulders, so linking problems to a mesh of their boulder saves about 60% of the work. Areas with
group names (`GROUP_AREA`, e.g. "Manx Boulders - Northwest Face") hold several rocks, so don't
share. If a boulder's first problem didn't get a mesh, the next one still tries, as its name finds
different photos.

**A restart stops the run rather than resuming it.** This machine is shared, and a job that comes
back by itself after a reboot, using every core, is an unpleasant surprise. Unattended tools run
under `nice`. Each climb's `work/` directory is deleted when it finishes: at about 800 MB, it's most
of what a job leaves on disk, and only a re-run would use it, which recreates it anyway.

## Why job logs aren't in the database

COLMAP prints tens of thousands of lines. `UPDATE projects SET log = log || ?` rewrites the whole
log for each one: on ZFS that managed 8,700 of a real job's 46,700 lines in 150 s and kept slowing
down, while appending to a file does all of them in about a second. This is the likely reason a
reconstruction once took 54 minutes through the app and 26 outside it (not re-measured).
