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

**What it rejects in an area (Nov 8).** Over the 400 latest harvests, almost all from the Joshua Tree and
Tramway areas, DuckDuckGo gave 42,419 photos and the filter kept 9%: per climb a median of 3 web photos
kept and 104 rejected, against a median of 1 anchor (149 climbs had none). The rejected weren't near
misses: 25,584 had 8-11 SIFT inliers and 11,109 none, all noise. CLIP's boulder prompts called 57% of
3,000 of them bouldering (a median of 64% per climb), so most are of climbing, not junk. But LoFTR
(kornia, outdoor weights), which found about 10 times SIFT's inliers between the photos the filter kept and
their anchors (Swing Dance's: 160-672, against 16-67), matched only 5 of 379 rejected photos from 4 climbs
to their anchors, with noise at 13-18. So most are of other
boulders nearby: Swing Dance's search found other Tramway boulders, swing dancers and a college. A learned
matcher would add about 1% for the price of a GPU (LoFTR took 18 s a pair on 8 CPU threads, DISK and
LightGlue 3.8 s), and looser thresholds would let in what sits just below them.

What helps is the boulder. 1,565 of 1,808 problems share one, with a median of 1 page photo each but 4
between a boulder's problems. Areas meshed each boulder once, but harvested each problem alone until one
was meshed: 248 boulders had no mesh after 2 or more of their problems were each skipped, from 811 harvests,
and 117 skipped climbs were on a boulder meshed later, never linked (Swing Dance, 4th, and Line Dance,
11th, before Square Dance, 66th). So for a while a harvest pooled what the boulder's earlier harvests
found, those boulders could be tried again from all of it, and skipped climbs were linked once their
boulder had a mesh. CLIP runs first in the image now, so that what didn't match is the plausible half,
listed apart.

**Each climb its own mesh, and each boulder one too (Nov 9).** By then 141 of 199 meshes were linked to more
than one climb (473 links "same boulder as" another), and on the 142 meshed boulders with two or more
climbs, 320 of their 619 climbs had been linked without ever being harvested: not even their pages had
been read, so none of their photos or videos were in any mesh, and a mesh made from the photos of a problem
on one side of a rock was shown as the mesh of problems on the other. Only 18 meshes held pooled photos (459
of the 8,115 photos behind them). Areas now harvest and mesh every climb from its own photos (the boulder's
other harvests only anchor the filter), and a boulder gets a mesh of its own from all of them, apart from
its climbs' meshes, with each climb placed on it by its own page photos and video frames.

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

## The viewpoint check (`backend/climbs/diversity.py`)

After the filter, ORB features (2,500 at 1,024 px) are matched between every pair of up to 120 photos. A
pair with 40 matches that fit one fundamental matrix is two views of the same thing, unless a 2-D similarity
explains them (a crop, or a fixed camera's frames), and the largest connected group of views estimates how
many photos COLMAP could place. Below 6, a harvest wasn't meshed.

On 219 harvests that went on to COLMAP, it placed a median 1.14 times the estimate, but anywhere from 2 to
24 photos for estimates of 6-7. Below 6 nothing had been tried, so COLMAP's sparse stage, as the pipeline
runs it (with people unmasked), was run on 8 harvests skipped with estimates of 3-5 and at least 15 photos,
out of 25 such: 6 placed 7-43 photos (Slash Boulder, pooled: 43 of 60, estimated 5), and the other two gave
up in 24 and 208 seconds, 3 mapper attempts included. Of 3 skipped with estimates of 0-2 but 65-121 photos,
all web results kept unfiltered as their climbs had no anchors, COLMAP placed 2 photos of each, giving up
in 72-297 seconds, so they stay skipped.

So a set with 3 connected views among at least 10 photos is now marginal, and COLMAP decides: placing fewer
than 6 stops it within minutes, and an area counts that as skipped, not failed. Verdicts are worked out
again from the numbers a harvest kept (`judge()`), so older harvests pass by the new rule too: 24 skipped
area climbs did, besides pooled retries. Of the 1,225 harvests skipped as poor then, 928 had under 10
photos and estimates of 0-2.

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

It also retries if a run ends with too few photos, as the result is partly luck. La Migra (25 photos)
placed 23, 18, 14 and 7 on four runs of the same matches, 23 and 7 from the same seed, and 3 on a fifth,
which failed. That fifth grew a model to 21 photos and then lost all of them at once (`Filtered images:
21`) after its points were filtered away. 7 of 31 mapper runs on Joshua Tree lost at least half of a model
like this, every time at 20-21 photos. The others recovered because they had more photos left to start
again from. Retries only happen below the 6 photos a mesh needs: small sets above that still get whatever
the first run gives.

Placing 6 photos doesn't mean there's a mesh. By 2026-11-09, 23 of 231 meshes, all from harvests, had next to
no surface: OpenMVS's mesh from their points had 0 to 7,352 faces, and its cleaning step then removed every
one in 21 of them, which were kept as 136-byte untextured `mesh.ply`s once TextureMesh failed on nothing. The
other two were textured scraps of 513 and 574 faces. The smallest real mesh then had 3,870, and most had
over 50k. So a reconstruction now fails when its mesh has under 1,000 faces (`MIN_FACES`), and an area counts
that as skipped, like too few photos placed.

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

**Matching** tries all 6328 pairs of those photos, and 30% have 15 or more inliers. The app's COLMAP is
now 3.9.1 built with CUDA, Ubuntu's own build having none, which allows a fair test: the job's own
features, the same COLMAP, and only the device changing.

| Matching, from the job's features | Time | Useful pairs |
|---|---|---|
| CPU, guided, all cores (the job) | 12.2 min | 1889 |
| CPU, guided, 12 cores | 25.7 min | 1897 |
| GPU, guided, twice, COLMAP's minimum | 3.0 min | 1814, 1811 |
| GPU, unguided | 2.1 min | 1841 |
| GPU, guided, a minimum of 12 (the app) | 1.9 min | 1971 |

The GPU was also running other people's jobs at full load, with some more room for the last one, and
used about 2.5 GB. COLMAP 4.2.1 took
60 s on it earlier, under a lighter load, but found 1680 pairs from its own different features: most of
that shortfall was the version, not the GPU.

**Why the GPU finds fewer pairs.** It loses 51 (2.8%) of the 1854 pairs that both CPU runs found useful,
while 78 pairs differ between those two CPU runs anyway. The lost pairs are borderline ones: a median
of 17 raw matches on the CPU, against COLMAP's minimum of 15. The GPU has its own SIFT matcher, which
finds about 4% fewer raw matches on a typical pair (why isn't clear), and 38 of the 51 end up below the
minimum, so they're discarded before guided matching, which could have taken one from 16 raw matches to
1035 inliers. They are spread over 54 of the 113 photos, and aren't weak once matched.

So the GPU's minimum is 12, which only affects a run on the GPU:

| GPU's minimum | Useful pairs | Lost, of the 1854 | Extra pairs | Inliers |
|---|---|---|---|---|
| 15 (COLMAP's) | 1814 | 51 | 5 | 1.03M |
| 14 | 1857 | 28 | 19 | 1.04M |
| **12** | 1971 | 7 | 97 | 1.05M |
| 10 | 2097 | 0 | 208 | 1.06M |

The extra pairs do no visible harm. From CPU matches the mapper placed 59 and 72 photos (error 0.98,
1.03); from GPU matches at a minimum of 12, 72 and 77 (1.06, 1.02), in about the same time, going on to
0.98M and 1.05M faces against the job's 1.06M, and looking as coherent by eye. At COLMAP's minimum, 46
and 66. That's two seeds on one boulder, whose results vary a lot, so this shows no harm rather than a
gain. 10 wasn't tried further, as its extra pairs are twice as many for 7 more.

On 29 frames of one video, with the settings for your own photos, CPU and GPU matches are equally good:
317 useful pairs each and 164k inliers, and 3.1 minutes for the whole job rather than 4.4. The mapper
placed 27 photos on all 6 seeds from the CPU's, and on 5 of 6 from the GPU's. The other seed split them
into models of 14 and 13, and it's seed 0, the only one the app tries unless the mapper aborts or places
too few photos.

Turning guided matching off halves CPU time on a 36-photo sample and keeps the useful pairs, but on the
full set leaves 40% fewer inliers (0.60M against 1.01M, where guided GPU matching has 1.03M). Cutting features from 8192 to 4096 saved 37% but
lost 12% of the pairs.

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
needs work to use, and uses COLMAP 4.2.1, not the app's 3.9.1:

- OpenMVS 2.3 aborts on 4.2.1's models. Writing the model as text and undistorting it with 3.9.1 works.
- It left one photo with camera settings the undistorter wouldn't convert (focal length 1560, likely
  an unrefined guess), and OpenMVS aborts on those too. That photo has to be dropped first.
- It warns that it relies on focal length priors, which most web photos lack.
- The official image's Ceres has no CUDA, so its bundle adjustment stays on the CPU regardless.

**The GPU is shared, so it can't be assumed.** COLMAP aborts outright on a CUDA out-of-memory error,
which happened when other jobs took 23.9 of its 24 GB, so anything using it needs a CPU fallback.

Not tried: downloading from different hosts in parallel, keeping each host's rate limit; choosing
pairs instead of trying all of them.

## The backs of meshes

Many meshes are open sheets, as photos only show part of the rock. From behind, a sheet used to show its
front's photos through it, mirrored, which looked like real rock. The viewer now draws back faces in flat
grey, in its shader, so no mesh had to be rebuilt and the files are unchanged. This relies on faces having
their front towards the photos. On the 19 meshes with saved cameras, a median of 98% of faces have most of
their photos in front (lowest 87%), and at most 3% have under a fifth in front. On all 48 meshes, at most
1.8% of shared edges disagree about which way is front. So a few faces can come out the wrong way round.
Uploaded meshes keep colour on both sides, as their faces may not follow this. The grey matches
`--empty-color`, for faces no photo saw, so grey means no photo of that side either way.

## Textures that won't load

Plain grey everywhere means the viewer got no texture: GLTFLoader only logs one it can't load, and
the viewer gives anything without a texture a grey material. That happened once with Midnight
Lightning's 8192 x 8192 atlas, which takes 270 MB decoded. The cause wasn't caught, as the app's
restart that day lost the server log, so it may have been the browser's memory or that restart.

So new textures are capped at 4096, with larger atlases split: that scene became two 4096 textures
(130 MB) with the same detail, and packing them took 23 s rather than 4 min 19 s. For meshes already
made, the viewer retries at 4096 and 2048, which the server makes on request and keeps. Whatever
happens, the view says what happened and why, having fetched the file again to find out.

## Which way is down (`backend/orientation.py`)

COLMAP's `IMAGE-ORIENTATION` assumes most photos were taken upright, and takes the typical direction of
their bottom edges as down. iPhones record the real direction of gravity in each photo's maker note
(AccelerationVector), so a mesh is levelled from those when any of its placed photos has one. Measured on
2026-11-01, on the real steps up to levelling, running each project's kept photos twice:

| Project | Photos placed | Readings kept / placed | Estimate off by |
|---|---|---|---|
| Fry Problem | 16, 16 | 1 / 1 | 7.7°, 7.6° |
| False Hueco Traverse | 97, 97 | 1 / 1 | 16.0°, 16.1° |
| Mystical Vibrations | 18, 25 | 2 / 0 | no reading placed |
| Hobbit Roof | 24, 26 | 1 / 0 | no reading placed |
| White Rastafarian | 33 (one run) | 1 / 0 | no reading placed |

JBMFP and The Inquisition, which also have a reading each, weren't run. So where a reading can be used the
estimate is well off, and consistently so between runs, but that's two photos on two boulders, each looking
up about 10°. Readings are also rare: 63 of 4,724 stored photos, 8 of them kept, on 7 of 29 meshes, and
three of the five tried didn't get placed. One reading can also be wrong in itself, and nothing checks that
unless there are several. A reading over 45° from the estimate is reported but not used.

The reading points up, in the phone's axes. Files nearly always have their pixels turned upright since, with
the EXIF flag gone, so a reading's quarter turn is worked out from the reading. Shape bears that out: of 63
files, all 25 landscape readings are landscape files, and 34 of 40 portrait readings are portrait files, the
rest likely crops. Across all 63, people look up and down about equally (median -0.6°, 10th to 90th
percentile -33° to +31°), so a lean isn't a given.

Meshes keep their cameras from now on (`cameras.json`), as without them a reading can't be related to a mesh.
The 27 made before this deleted theirs, so they can only say what their photos hold; rebuilding one makes a
new mesh, which can be worse: Mystical Vibrations went from 191k faces to 34k.

## Stray pieces and sheets

Each reconstruction hides its stray pieces (`backend/meshclean.py`), using the second rule below.
That's measured on 29 meshes on 2026-11-01, with candidate filters applied to copies and five of the
worst looked at by eye.

The piece with the most faces holds a median 98.6% of them, but only 92% of the surface, and as little
as 6%. The Chube's holds 95% of its faces and 6% of its surface, while a piece with 2.3% of the faces has
54%: giant sparse triangles, often distant scenery. So pieces should be judged by faces, not surface.
Keeping only the largest piece isn't safe either: Mystical Vibrations' second piece has 33% of its faces
at 2.5x the density, and looks like real rock.

| Keep | Faces kept, median [worst] | Surface kept | Box left | By eye |
|---|---|---|---|---|
| Pieces with at least 1% of the main piece's faces | 99.75% [96.7] | 97% [52] | 85% [9] | leaves the sparse scenery |
| ...that are also no more than 5x sparser than it | 99.52% [91.7] | 95% [6] | 76% [7] | clean on all five, real pieces kept |
| Triangles with edges under 20x the median, then pieces as in the first row | 99.38% [94.7] | 84% [5] | 81% [6] | tears holes in rock, and can keep far-off scenery |

"Main" is only the piece with the most faces, which nothing checks is the boulder in question. And the
rule can hide real rock: the piece beside White Rastafarian looks real, at 7x sparser than the main one.

So it's a step on top of the mesh rather than a change to it: which pieces are hidden is kept beside the
mesh's files, can be edited piece by piece, and can be deleted. It costs at most 3 MB and 2 s per mesh.
The list of faces is what the viewer needs to hide pieces without working them out itself. A cleaned
file is only made when someone downloads one.

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

**A restart only pauses a run.** It carries on by itself, unless someone had stopped it, and its climb
in progress carries on from its last finished step: each step of a reconstruction records when it's
done, and a partial harvest starts again, which is quick, as its downloads are cached. Within a step,
matching keeps the pairs it's finished, and the mapper starts over. Densifying also keeps its depth maps: restarted at 28 of 31, it reached all 31 in 13 s. Tried for real on White
Rastafarian, restarting during matching, the mapper and densifying, which finished in the same project
with no errors. Six deployments had interrupted Joshua Tree before this, each early in a climb. Unattended
tools run under `nice`, as this machine is shared. Each climb's `work/` directory is deleted when it finishes: at about 800 MB, it's most
of what a job leaves on disk. A climb that fails keeps it, so that trying again carries on from its last finished step.

**Retrying** uses the same list rather than the job queue, so it survives a restart and *Stop* works on it.
A retried climb is flagged and goes first. An area that wasn't running runs only flagged climbs and then
stops, as pressing Retry on something you'd stopped shouldn't set off the rest of its list.

## When a job fails (`backend/diagnostics.py`)

Every tool's run logs its time, peak memory and CPU. A failure says how it ended: its exit code, a crash
(SIGSEGV, SIGABRT and so on), or a signal from elsewhere. The app only ever kills a tool with SIGKILL, when
someone stops a job, and that ends as a stop rather than a failure, so any other kill didn't come from it.

Its parent only learns which signal killed a tool. `signal_note.c`, preloaded into each tool, prints who
sent it: a pid and uid inside the container, or pid 0 and a uid from outside. On hal, uid 61876 is earlyoom,
and uid 0 is root. SIGKILL can't be caught, so for that the log has the kernel's out-of-memory kills during
the run. Memory has recovered by the time a tool is dead, so it's sampled every 2 s while the tool runs, and
the lowest it reached is logged, with the machine's load, memory pressure, disk and GPU memory.

**Leather and Lace** was the first to raise this: DensifyPointCloud got SIGTERM 51 s in, at 04:37 on 2026-11-02,
and the signal's sender wasn't recorded yet. What's known:

- The tools run as root, so only root or something with CAP_KILL could signal it.
- It wasn't the app, which has no SIGTERM anywhere, or a restart, or the kernel's OOM killer, which uses SIGKILL.
- No Claude session on the account was active from 3 am to noon. Only that account had logins open.
- earlyoom was armed: it needs memory at 8% and swap at 5%, and swap had been full for 30 hours, because of
  test data left in the RAM-backed `/tmp`. The only memory reclaim from 04:10 to 05:00 was in the 10 minutes
  it died in, though 12 such bursts happened in two days.
- But earlyoom picks the highest `oom_score`, and adds 300 to python's. The app's own server scores 974, and
  sits ignoring SIGTERM as its container's init. Densify would need about 118 GB to beat that. Rerun on the same
  photos, it peaked at 1.7 GB, for a score of about 670.

So earlyoom probably wasn't it, unless that run behaved very differently. The likeliest remaining cause is a
script or person using `kill`, whose default is SIGTERM, with root. Only the system logs can settle it, and
they aren't readable without `adm`.

## Where photos come from, and their licences (`backend/provenance.py`)

The harvest records each photo's source, URL, page, credit and licence in its project's `sources.json`,
and each mesh keeps a copy for its own photos in `photo_sources.json`, as projects change. Only Commons
(and Flickr, unused without a key) give a licence per photo. The rest are one licence per source in the
UI. Across the 56 meshes on Nov 2, their photos were 75% video frames (1,946), 16% DuckDuckGo (418) and
9% Mountain Project (224). Video frames hadn't recorded their video; the frame's name holds a hash of its
URL, which is matched against the climb's videos.

Could more be scraped per photo? Sampled Nov 2:

| Where | What it held |
|---|---|
| Embedded in the files (EXIF, IPTC, XMP) | Any rights metadata in 5% of 1,259 used DuckDuckGo images, a licence or rights URL in 1%; 3% of 769 Mountain Project photos |
| The pages DuckDuckGo images came from | Of 36: a site-wide "©" or "all rights reserved" on 32, a schema.org copyright field on 3, a licence field on 2, a Creative Commons link on none |
| yt-dlp's `license` for the videos | None for 12 of 174 in use, which means YouTube's standard licence: it reports "Creative Commons Attribution license (reuse allowed)" for a CC BY video |

So a video's licence is worth recording (yt-dlp has it at download time, and backfilling is one
metadata request per video), while a web image's is almost never stated anywhere.

## Videos found by searching, and climbers' sizes (`backend/climbs/youtube.py`)

Only 120 of 317 fetched climb pages linked any video, and Relic's linked none. One YouTube search
per climb, through yt-dlp, for its name, V grade and area ("Relic V9 Joshua Tree") returned 15
results, 6 of them Relic, from 5 uploaders. A result counts only if its title names the climb as a
whole word, and its title or description also gives its area, boulder or grade: that kept exactly
those 6, and none of the other Joshua Tree V9s. Videos over 15 minutes are left out, as they're
mostly compilations. Up to 4 found videos are used, and 6 in all with the page's own, which share 150
frames: a video that fails to download leaves its share to the rest.

Heights, wingspans and ape indexes are read from the title and description of each video, and from
its uploader's channel description, and kept with the text they came from. A number only counts as a
height with a word saying it's a person's (tall, height, I'm, climber), unless it's in feet and
inches, and not next to a word about the rock, as a "6 ft tall boulder" is common. Nothing says the
person in the video is the uploader: `first_person` records whether the text said "I'm". The size of
a mesh uses them for its climbers, as below.

**When YouTube refuses.** From Nov 1 to 9, 2026, harvests on hal (one Caltech campus address) asked YouTube
for about 1,700 videos, and 640 of those were refused with its bot check ("Sign in to confirm you're not a
bot"). The refusals came in spells: about ten, of 1-11 hours each, mostly after a few hours of harvests
taking 20-30 videos an hour, well under the limit below. Each spell ended by itself, even with harvests still asking (and failing) throughout,
and searches never got the check. yt-dlp's wiki puts YouTube's limit for a session that isn't signed in at
about 300 videos an hour, and suggests 5-10 s between downloads. Nothing used to space them: several
workers each fetched up to 6 videos back to back, and kept asking for each through a refusal. So now
every yt-dlp run waits its turn (one at a time, 8-15 s apart, `--sleep-requests 0.75` within one), and a
refusal leaves YouTube alone for 1, 2, 4, then 8 hours, with the next video after that as the test.
Signing in with a throwaway account's cookies would raise the limit, at the risk of that account being
banned. A PO-token provider (bgutil) doesn't get past refusals based on the address, by its own account.
A cloud server's address is refused far more often than a home or campus one, so the public server never asks YouTube
for a video itself: volunteers fetch them (below).

People's masks used to be made in `work/` and deleted with it. They're now kept in the project's
`masks/`, which also makes a rebuild skip photos that already have one.

## Volunteers fetching videos (`backend/volunteers.py`)

Volunteers do only what needs a residential or campus address: asking YouTube for a video, or a video's details.
Searching stays with the server, as YouTube never refused searches, and so does everything after the download (the
frames, the filtering, finding beta), so the script needs nothing but yt-dlp and Deno, and the server decides which
frames to keep. A volunteer gets a video's id, never an address, so a compromised server can't point their computer at
anything else. It sends the video whole rather than frames: the harvest takes 1 frame a second and finding beta 2 a
second of the first 150 s at 720p, and frames chosen there couldn't be chosen again later without another download,
where a YouTube request is the scarce thing, not bandwidth. Kept in the cache, one download serves both, where before
finding beta downloaded every video a second time.

Searching could be refused too. From Nov 1 to 9, 2026, 1,474 harvests on hal searched YouTube, through every spell of
refused downloads, and one search failed, with an error fetching the results page rather than the bot check: a search is
one request for a page of results, and a download also asks YouTube's player for the video, which is where the bot check
is. A cloud server's address is another matter, so when the public server harvests, its searches go to volunteers too,
as a third kind of work, with channels' descriptions (for climbers' heights) as a fourth: the server sends a search's
words, or a channel's id, never an address; the script runs `yt-dlp --flat-playlist ytsearch15:<words>` within the
same limits, and sends back each result's id, title, description, length and channel, which the server keeps by the
words for 30 days, its own searches too. On hal, a refused search pauses searching the way a refused download pauses
downloads, 1, 2, 4 then 8 hours, apart from them, and asks volunteers meanwhile.

**A climb waits for volunteers, to be meshed once.** With only volunteers searching, an area's every climb would be
meshed first without any videos, and again once the results and then their videos came: twice the work for the machines
that mesh, for a first mesh that's often poor. So an area's climb whose harvest asked volunteers for anything waits; the
results coming have it harvested again, which may ask for the videos they found, and those coming have it harvested again
and meshed. 24 hours of waiting (`VOLUNTEER_WAIT_HOURS`) is the most, as volunteers come and go: then it's meshed with
what's there, and what comes later rebuilds it as before.

**Room on the public server.** Its disk is 75 GB, with about 59 free. Of hal's 97 GB of projects on Nov 10, 2026, 92
were photos the filter rejected (kept only to put one back by hand), 9.4 the photos themselves (3 for the projects of
current meshes), 4.4 work folders kept by failed climbs (which a server meshing elsewhere never has) and 0.1 masks. So
the public setup caps its data at 50 GB: harvesting climbs is what fills it, so over the cap volunteers are given nothing
and areas take no new climb, paused rather than stopped, until room is made, which deleting the rejected photos mostly
does. hal's photos and masks go to it once (ship.sh's PHOTOS=1), as rebuilding, retexturing, sizes and boulders' own
meshes need them.

Checked on Nov 10, 2026, from the public server's address (Hetzner's): Mountain Project's pages, its area export and its
photos, OpenBeta, DuckDuckGo's image search and the photos it found, and Wikimedia Commons' search and photos all
answered the first requests. If any of them starts refusing a cloud address, as YouTube does, its requests could go to
volunteers the same way.

The limits are hal's, halved: one video at a time, 8-15 s apart, at most 15 an hour (hal's refusals came after hours at
20-30) and 100 a day, shared by everyone at one address so that two volunteers on one network don't double it. A
refused volunteer rests as the server would, 1, 2, 4, then 8 hours, and the script enforces all of it itself as well,
so a misconfigured server can't make it go faster. A video is sent in parts of 64 MB, under Cloudflare's 100 MB a
request, resuming after a dropped connection. The server checks the file's length against the video's, which catches
a wrong or cut-short file but not a deliberate fake, so tokens go to people you know, and can be revoked. yt-dlp's
full details carry the volunteer's IP address in YouTube's signed download links, so only the fields the harvest keeps
are sent, and the server keeps a volunteer's last address only to share the limits.

Where volunteers connect follows where harvesting is. The public server is the only machine they can reach. At first
(Nov 9, 2026) hal harvested, and an SSH connection from hal forwarded the public server's `/volunteer/` to hal's
volunteer port, which answers nothing else; it ended in a Unix socket that Caddy's container reads through a mounted
folder, which needs neither sshd's GatewayPorts nor Caddy on the host's network. That kept a way into the lab's network
open from outside, though, an exception to the lab's gateway in all but name, for a machine whose own address YouTube
keeps refusing. So the public server harvests, Caddy passes `/volunteer/` to its
own app, and hal meshes for it as a machine that connects to it for work ("Running climbs side by side, and
elsewhere"). The tunnel still works, for a harvesting server that can be reached from outside. Each server keeps its
volunteers in a database of its own, as ship.sh replaced the public server's main one.

The costs fall on volunteers: YouTube's terms forbid downloading except through YouTube itself, spreading downloads over
other addresses is plainly a way around its per-address limit, and a refused address gets the bot check for everyone on
that network for hours. The script says so and asks them to agree before it starts.

## Beta: which holds climbers use (`backend/beta.py`)

People's masks say where people are, not which of their parts are hands and which feet, so a pose
model finds those. On 12 of Relic's video frames, torchvision's Keypoint R-CNN (already in the image)
found the climber in 10, at 1.83 s a frame on 8 cores, and RTMPose's body-with-feet model (rtmlib) in
all 12, at 2.03 s, with toes as well as ankles, which is where feet stand on holds. RTMPose is used, with
people detected every 5th frame and tracked in between, on 2 frames a second of up to 150 s of each video.

A hand's point is 35% of a forearm past the wrist, and a foot's its big toe, or ankle. Each frame is
matched (ORB features, ignoring the climber) to the frame of the same video placed in the mesh that it
shares most with, and the point mapped into that frame by a homography, which treats the rock as
locally flat. The point is cast onto the mesh from that frame's camera, using its lens as COLMAP fitted
it (`cameras.json` keeps it from Nov 3). A point that moves under 3% of the rock's size between frames
for a second is a contact, and contacts within 4% are one hold. Feet within 4% of the mesh's lowest
part are taken to be on the ground. Roles follow the Tension Board: each hand's first hold is a start
hold, each hand's last a finish hold, other hands' holds hand holds, and those only feet touched foot
holds. These are rough rules: a climber who chalks up at a hold, falls and starts again, or tops out
onto a slab will confuse them, which is why each video's holds are kept apart, and why they can be
marked by hand.

Finding beta in every mesh with videos (148 of them, 364 videos, in Nov 2026) takes about 10 hours on the
CPU: the three earlier runs took 0.55 s a frame, about 100 s a video, most of it the pose model. It runs
on a thread of its own rather than as a job, as jobs come before areas' climbs on the same workers, so a
queue of 148 would hold up meshing for those 10 hours. That thread runs at nice 19, which the pose
model's threads inherit, and the CPU limit is shared, so it gets what meshing leaves. It needs little
memory. On the GPU, RTMPose took 10 ms a person against 266 ms on the CPU (an experiment outside the
image, Nov 8), but the image's onnxruntime is the CPU build: the GPU build needs cuDNN and more of CUDA's
libraries than the image has for COLMAP.

Marking by hand cycles a hold start, hand, finish, foot, then off. That's the order the Aurora apps
(Tension, Kilter) list hold roles in, which I couldn't confirm is the order the Tension app cycles them.

## How big a boulder is (`backend/scale.py`)

COLMAP's meshes have no scale of their own. On Nov 8 every placed photo of the 187 meshes with kept
cameras was searched for things of known size: 5,442 photos, 4,493 of them with people in their masks.

**People** are in nearly every photo. RTMPose (beta's model) finds each person's keypoints. The rays
through their hands and feet meet the mesh where those touch rock or ground, which gives how far away
they are. One alone is often a ray past the rock's edge, meeting rock far behind: Flake's hands met the
mesh at 60-68 units while its climber's feet were at 35, with the climber about 5 units tall. So a
person's depth is the nearest of their hands and feet that another is within 15% of. Each limb's
length then follows in mesh units. Limbs pointing toward the camera look shorter, so across a video's
frames, of one climber, each segment's 75th percentile is taken as its length, after leaving out
frames whose own estimate is more than 1.5 times off the video's median. Against Winter's proportions of
height (wingspan, for arms), using the climber's stated height where their video or channel gives one (40
of 177 videos), and 174 ± 9 cm otherwise, each segment gives metres per unit, and their median is the video's.

| Check, over the 137 meshes measured (107 with a video's climber) | Result |
|---|---|
| Two videos of one rock | median 10% apart; 61% within 15%, 76% within 25%; worst tenth over 55% |
| Segments of one video's climber | each within 4% of their median, after the two below |
| Standing people (head to heels, on the vertical plane through where their feet meet level ground) | 53 people in 24 meshes, 3.5% from the limbs' |
| Bottles standing on level ground in the mesh | 13 in 8 meshes, a median of 26 cm tall by people's scale |
| Rock heights near where the photos look | median 4.3 m; 96% between 1.5 and 10 m |

The quantile, the depth rule and the trimming were chosen by how well videos of one rock agree (most other
choices gave a median between 9% and 17% apart), and the absolute level is checked by standing people and
Mountain Project, which agree with it on average. The model's shoulder, neck and mid-hip points aren't the joints Winter measured, so the upper
arm read 10% long and the torso (neck to mid-hip) 21% short against the forearms and legs. Their shares
are calibrated to 0.169 and 0.354, from 0.186 and 0.288. Single photos, of a different person in each,
are pooled, and count for less. Standing people are rare, as their feet have to meet level ground in the
mesh, and the ground mostly isn't reconstructed.

**Wingspan.** With both hands on holds, both are points on the mesh, with no depth to guess. The way from
one hand through the shoulders to the other, with the shoulders 25 cm in front of the rock behind them, is
at most the climber's reach from palm to palm, 0.85 of their wingspan, and equals it with straight arms,
in any direction and around the rock, not through it. In single placed photos it came out 21% below
limbs, scattered by 30%: a hand that only looks to be on a hold casts its ray past it. The beta search's
contacts (a hand still for a second) say which are on holds, so it now keeps each frame's climber
(`beta_poses.json`), and the size job records this from them as a check, not yet used. Running beta again
to test it was blocked: YouTube refused hal's downloads that evening ("confirm you're not a bot").

The one video whose skeletons were kept before then (JBMFP's, 9 frames with both hands on holds and
both arms straight) put the rock at 0.59 of the size from limbs, where that mesh's three videos' limbs
agree within 7%. One video can't say whether that's the method or that climber, so it stays a check. A
mesh is now sized again whenever its beta finishes with skeletons, and `python3 cli.py span-check`
compares every video's span with the limbs, so *Find beta in all* will test it once YouTube allows.

**Mountain Project's height.** 1,379 of 1,808 boulder pages give one ("Boulder, 15 ft (5 m)"), mostly
round numbers: 10, 12, 15 and 20 ft account for 63%. Text gave it for only 16 more. Against the rock's
height within one and a half times the spread of where the placed photos' centres land, MP's scale is 4%
from people's on the median mesh, within 25% for 48% of them and 50% for 70%. Against the whole mesh,
often taking in a hillside, 34% and 62%. Some of that is the mesh: the rock near the photos is often
taller than the problem. So it's a fallback, weighted as a log sd of 0.33 against a video's 0.12.

**Objects of known size.** torchvision's COCO Faster R-CNN, without training, found 153 bottles in 52
meshes. Crash pads came out as suitcases and books, and climbing shoes as skateboards. CLIP (ViT-B/32) labelled 16 as Nalgenes,
mostly shoes, cans and other bottles, as most were 12-30 pixels wide in video frames. The likeliest
Nalgene, a narrow-mouth one in two frames of Fishbait, stands on ground that isn't in the mesh, and the
camera barely moved (0.1 degrees between the two frames' rays), so it can't be triangulated either. On
a level plane at the ground's height, bottles came out 33-134 cm tall. Crash pads vary too much (91 by
122 cm to 153 by 121 cm) to help unless their model is known, though many show their maker in large
letters (Mad Rock, Evolv, Organic in a sample of 10), which reading text could narrow down. Reverse image search would need a paid API
(Google Cloud Vision's web detection); a table of known sizes with CLIP picking among them is the offline
equivalent. `scale.KNOWN` and `measure_upright()` are there for one: a 32 oz Nalgene put at the foot of the
rock in your own photos, on ground that gets reconstructed, would be measured to about the box's pixel.

So a project of your own can say what its photos show (`known_object.json`: a Nalgene from `scale.KNOWN`, or a
bottle's height). Sizing then runs the detector on its placed photos at 1600-2400 px rather than its default
800-1333, as a bottle is a small part of a photo of a boulder, keeps the bottles whose base lands on ground
in the mesh and whose width for their height is within 1.6 times the real one's, and drops measurements 1.5
times off their median (another bottle). What's left is one estimate, at a log sd of 0.1: tighter than a
video's climber, as nothing about the object is guessed, but not as tight as the pixel, as the base point
and the vertical plane through it come from the mesh. It hasn't been tried on a real Nalgene photo set yet.

Reading crash pads' makers was tried on the 486 boxes at least 120 px wide that the detector called
suitcases or books (RapidOCR, on the GPU, 3.5 minutes): 130 had any text, but only 17 (in 10 meshes)
named a maker even loosely ("ROCK" for Mad Rock, "Black Diamono"), and a maker's pads still range from
about 100 to 160 cm long. So it isn't used.

Casting a ray at a million triangles took 120-190 ms. Each placed photo's camera now projects every
triangle's bounding sphere once (0.1-0.5 s) and files it by where it lands, so a ray takes 10-15 ms; the
beta search uses the same.

## Running climbs side by side, and elsewhere (`backend/compute.py`)

Until Nov 3 one worker thread did everything, one climb at a time, so a second area waited for the
first. Workers are now threads with their own job control, each claiming an area's next climb under a
lock: from the area fewest other workers are on, and never a climb on a boulder another worker is
meshing, which would make two meshes of it. Running two locally isn't measured. The heavy steps
(matching, bundle adjustment, densifying, texturing) already use every core, so it mostly helps by
overlapping one climb's downloads and Mountain Project's 60 s crawl delay with another's reconstruction.
That might be 1.2-1.6x, at the cost of memory (CPU matching of 30 photos peaked at 22 GB) and being a
heavier neighbour on a shared machine.

A remote worker sends only the reconstruction: a climb's photos and masks, typically 50-300 MB, out over
SSH, and the mesh, its cameras and new masks, 20-60 MB, back. The rest stays here. A new EC2 instance
takes a minute or two to boot, and another minute or so to pull the 2 GB image, so machines are kept
while there's work and ended when idle. A GPU only speeds up matching: with the internet-photo
settings, feature extraction has no GPU version in COLMAP, and the mapper, OpenMVS's densifying (built
without CUDA here) and texturing all run on the CPU. So a node with few cores is slower per climb than
this 48-core machine, and the gain is in doing several at once. Hence `g4dn.2xlarge` (8 cores) rather
than the 4-core `g4dn.xlarge`. Building OpenMVS with CUDA for nodes would speed densifying up.

**Machines that connect here for work** (`backend/pullwork.py`). SSH needs the server to reach the machine, and the
machine that meshes best, hal, is behind a lab's firewall, with the public server outside it. So such a machine runs a
script that asks the server for work instead, over HTTPS, through Cloudflare and Caddy like any visitor: nothing is
opened into its network. It sends the same things SSH does, in the same image: a climb's photos and masks (a tar,
fetched in parts) and the mesh, cameras and masks back (sent in parts, under Cloudflare's 100 MB a request), with the
job's log every 5 s, which is also how the server knows it's still there. A machine quiet for 15 minutes is taken as
lost and the climb waits, as for a lost SSH machine; a climb stopped on the server is gone the next time the machine
reports, and its container is ended. Jobs are kept only in memory, as a restart of the server ends the climbs' threads
anyway and they carry on as new jobs. The script runs jobs as its own user, so their files are its to delete, with no
capabilities, and only the server's image (`--image-prefix` makes sure of that); the server only ever names an image,
never a command. With *Reconstructions at once on this server* at 0, a server that's too small to mesh (the public one:
2 cores, 4 GB) harvests and keeps everything, and every mesh is made elsewhere. So is what's heavy after meshing: finding
beta (a pose model on 300 frames a video) and a mesh's size (the same model on its photos, and a detector for bottles),
which the same machines run as remote_job.py's other jobs, from the mesh's own files (not its textures), the photos
placed in it and their masks, and for beta the mesh's videos from the cache, cut to the first 150 s, which is all it
looks at, without re-encoding: the job's YouTube is set to never ask YouTube, so a video that isn't there fails it
rather than having hal ask for it. What they make comes back by name (beta.json, beta_poses.json, beta_<tag>.jpg;
scale.json, poses.json, scale_people.json, objects.json), and nothing else.

What's specific to a provider is only starting and ending machines, so the rest works with any machine
with Docker that SSH reaches. Adding GCP would be another class like `AWSProvider`.

## Scenes around meshes (`backend/splat.py`)

The viewer's *Scene* background is a sky over ground the colour of the rock's foot (the mean colour of its lowest 6% by
height, from its vertex colours or texture), with a soft shadow under it and a little fog, all drawn in the browser. A
Gaussian splat of what the photos saw around the rock can go in front of that sky and ground.

The splat is fitted with gsplat to the photos a reconstruction placed, from `cameras.json`, as COLMAP's own points
aren't kept: it starts from the mesh's vertices and a shell of points around the scene. People masks keep passers-by out,
and each photo gets a colour correction of its own while fitting, for the light it was taken in. A random background
each step keeps blobs only where photos show something. Afterwards it drops blobs fewer than 3 photos saw, blobs inside
the rock's box (the mesh shows the rock, and blobs over it would hide it), blobs by the cameras, and big blobs near the
scene. Where those were, the plain sky and ground show.

Tried on hal's 3090 with gsplat 1.5.3 (torch 2.4.1 with CUDA 12.4, Python 3.10, in a scratch environment) on Monkey Gone
to Heaven, whose 115 placed photos are the most of any mesh: 7,000 steps took 62 s, and kept 88,600 of 278,000 blobs
(2.8 MB). From the framing it shows trees and nearby rock in place, with smears where only a photo or two looked.

Most meshes are placed from far fewer photos: a median of 24, mostly frames of one or two videos. That's too few for
much of a splat, and the frames kept for the mesh are the sharpest, not the ones that best cover the surroundings. So
fitting is off until videos are kept again (the cache), when frames taken more densely for the splat could be placed
against the mesh's model. The image's torch is the CPU build, so fitting would also need a CUDA build of torch and
gsplat, or an image of its own. The viewer draws a splat with GaussianSplats3D, which sorts its blobs in a worker made
from a blob: URL, using WebAssembly; the page's policy allows both (`worker-src blob:`, `'wasm-unsafe-eval'`).

### Depth as a guide where few photos look (not built)

With few photos, a splat puts each blob at whatever depth reproduces the photos it was fitted to. Seen from anywhere
else, those blobs are the smears. Depth estimated from each photo could give those parts a shape to settle on. The
measurements behind this are in [experiments/depth](experiments/depth/README.md):
- MoGe-2's depth for a single photo is about 9% off the mesh's (AbsRel), and Depth Anything V2 Large's about 10%.
- VGGT's points, from all the photos together, are 0.4-1% of the rock's size off, and agree between photos.

None of them comes in the mesh's units. Each photo's depth would be fitted to the mesh where the mesh covers it (a
scale for MoGe-2; a scale and offset in inverse depth for Depth Anything), as those tests did. That fit can only be
trusted so far past the rock, which is one thing to measure. Ways it could help, cheapest first:

- **Starting points.** Points from each photo's fitted depth, outside what the mesh covers, would replace the shell. Each
  would be a pixel's worth, thinned, coloured from its pixel. Blobs would then start on the ground, trees and nearby
  rocks rather than on a sphere.
- **A depth loss.** gsplat renders each blob's expected depth along with its colour (`render_mode="RGB+ED"`). That depth
  would be compared with the photo's fitted depth where the mesh doesn't cover. The comparison would ignore any scale
  and offset left over per photo, using a correlation such as Pearson's (as FSGS, SparseGS and DNGaussian do). It would
  count less as the steps go on. Where the mesh covers, the mesh is better truth, and the rock's box is pruned anyway.
- **Sky.** MoGe-2's mask marks sky. No blobs would go there, and the dome would show through.
- **Floaters.** Blobs well in front of the fitted depth, in the photos that see them, would be dropped.
- **More frames.** VGGT can place frames the mesh's model never had, such as video frames taken densely for the splat
  (see above). Its weights are licensed for non-commercial use only.

**How to test it.** On the four climbs of `experiments/depth`, and on a few with typical coverage (20-30 placed
photos, from one or two videos):
- **Held-out photos.** Leave out every 8th placed photo. Also fit on test 2's poor subsets, scoring them on photos near
  them (within 30°) and beyond.
- **Variants.** As now (the mesh and a shell); with depth starting points; with the depth loss too; with floaters
  dropped; VGGT's points in place of MoGe-2's; sky masked or not. Each with 3 seeds, since densifying is random.
- **Scores on the held-out photos.** Compare the viewer's own picture (mesh, splat and sky together) with each photo,
  outside the rock (the mesh draws that) and away from people and sky, by PSNR, SSIM and LPIPS. Also compare the
  splat's rendered depth from those cameras with the full mesh's, where the mesh covers (AbsRel, as test 1). That
  catches blobs at the wrong depth that still colour the training views right.
- **Costs.** Blob count and file size, as phones download it, and fitting time.
- **By eye.** The viewer orbiting away from every photo, at a few angles per variant. The aim is a plausible backdrop,
  and no score tells a smear from something plausible.

A variant would become the default if it improves the held-out scores outside the rock on the typical climbs, without
making the well-covered ones worse, and looks better. All of this can run on frames already harvested, except the dense
frames, which need the video cache. It needs hal's GPU and two environments: gsplat's (torch 2.4.1, CUDA 12.4) and the
depth models' (`experiments/depth`, a newer torch). The depth maps would be written to files in one environment, then
fitted from in the other.

## Textures from only photos free to copy (`backend/retexture.py`)

Re-texturing with OpenMVS's TextureMesh from only the permitted photos, using the cameras kept in
`cameras.json`, failed on White Rastafarian with its 20 video frames standing in for permitted photos.
With seam levelling it crashed straight after assigning views. Without it, it ran for 15 minutes on 11
cores without finishing, probably packing the many small patches of a mesh mostly not seen. Importing a
camera-only model into OpenMVS also ran out of memory until it was given points, taken from the mesh.

So each vertex is coloured instead. It takes the median of its three best views (most squarely and
nearest, not blocked per a coarse depth buffer of the mesh's vertices, and not where a mask has a
person). That took 3 s for 457k vertices and keeps every face in order, so the mesh's cleanup still fits.
It's less sharp than a texture, and something in front of the rock in several frames of one video still
shows (a dark stripe on White Rastafarian's face), which a single best view showed much more strongly.

## Why job logs aren't in the database

COLMAP prints tens of thousands of lines. `UPDATE projects SET log = log || ?` rewrites the whole
log for each one: on ZFS that managed 8,700 of a real job's 46,700 lines in 150 s and kept slowing
down, while appending to a file does all of them in about a second. This is the likely reason a
reconstruction once took 54 minutes through the app and 26 outside it (not re-measured).

## Exposure: one token, not accounts (`backend/server.py`)

For a public copy of the site, the question was what to put between visitors and the write endpoints: harvests and
areas make this server fetch from Mountain Project, YouTube and DuckDuckGo for hours under its own address and user
agent, start cloud machines that cost money, and delete work; and an uploaded mesh folder served from the site's own
origin could carry a page. The data itself is disposable, the domain's and address's standing aren't.

Viewers get no token: a view-only site's risks are bandwidth and the copyright of what it shows, which tokens don't
touch and the licence settings and a cache in front do. There's one editing token, for the owner, sent as a bearer
header, which a page on another site can't make a browser send, so it also ends cross-site request forgery; an open
(LAN) site gets an Origin check for that instead. Accounts would add who-did-what and per-person limits, which a demo
with one editor doesn't need, and would fix now what the site's users should be allowed to change, which isn't known.

The rest is the usual: the standard library's server has no limits, so a reverse proxy takes TLS, slow clients and
request sizes, and an edge cache takes rate limiting; files from `data/` are typed from a short list or sent as
downloads, and uploads keep only mesh files, so nothing stored can run on the site's origin; the page's inline scripts
are allowed by hash, computed from the file when it changes, so the policy needs no maintenance until a new CDN or
inline handler appears; and the fetcher refuses private addresses because its URLs come from search results, which
anyone can seed, and a cloud machine's metadata service hands out credentials to whoever asks from inside.
