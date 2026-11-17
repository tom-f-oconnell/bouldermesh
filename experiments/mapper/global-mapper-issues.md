# The global mapper's misplaced cameras (Nov 15-17, 2026)

COLMAP 4.2.1's global mapper was the app's default from Nov 15 15:37 (8963d55) to Nov 17. Many of its meshes came out
with ghost surfaces: sheets beside or through the rock, made from groups of cameras placed in the wrong spot. On Nov 17
the default went back to 3.9.1's incremental mapper (admin page: "Which mapper places a mesh's cameras"), and every
current mesh it made was queued to be made again. This is what went wrong, and what to test before it's the default
again.

## What went wrong

Three kinds of fault, seen on the site's meshes:

| Fault | Example | What it looks like | Measured |
|---|---|---|---|
| A block misplaced | JBMF Boulder (775fe99d957d, Nov 17) | sheets crossing the rock, 1.5 m off it | one video's 14 frames placed as a rigid block in the wrong spot; pair median 4.93°, worst tenth 46° off |
| Two halves joined wrongly | Caveman Boulder (a59b6916f650, 1a3c2970399f) | two copies of the cave at a large angle | 46% of cameras in the largest group agreeing within 12° |
| Two halves a few degrees apart | Caveman Boulder (eb199a55d8a2, Nov 16 18:48) | a sheet in the middle of the cave, in front of the real wall; holes where the copies fight | 45% agree within 5°, 89% within 8°, 99% within 12° |

The incremental mapper doesn't do this the same way. A group it can't tie in well is left out (fewer photos placed)
rather than placed wrongly. Caveman's Nov 12 incremental mesh (0d0e7b24d7a1) has none of these sheets.

## How widespread

Of the 124 current global meshes whose reconstruction was kept (Nov 17):

- 42 failed the check as tightened that evening: under 80% of cameras agreeing within 12°, or a pair median over 5°.
  Worst: Railer 14%, Wedge Boulder 18%, Turnbuckle Rock 21%, Derivative Boulder 47%, Morgan Crack 48%.
- At 8° the largest group falls under 80% in 22; at 5°, in 39. Caveman's eb199 shows that some of the subtle ones are
  real faults, but web photos' guessed lenses scatter good models too, so these lines can't yet tell them apart.

Rough share of meshes affected: at least a third, perhaps more. No incremental mesh has been measured the same way in
numbers: their reconstructions weren't kept (two that were scored 30% at 5°, so the measure isn't comparable as is).

## Why benchmarking missed it

- **Three boulders.** The global mapper was judged on 31de, 9f3b and Drifter. All are well covered by photos linked
  strongly to each other. Caveman (a cave, photos from many sources) and JBMF (videos weakly tied to the stills) are
  the kind it fails on, and none of that kind was in the set.
- **The surface measure compared it with itself.** The seven rules judged later changes against `global_tied` as the
  reference. The global mapper's own switch was judged by photos placed, the self-check and the surface gap against
  base on the same three boulders.
- **The self-check's line was too loose.** A pair median of 5° let JBMF (4.93) through. A Caspar model at 3.42 was
  wrong too. The median can't see a minority of cameras placed badly.
- **Disconnected pieces aren't enough either.** After JBMF, Caveman's eb199 was judged good because it had no piece
  apart from the rock. Its doubled surface is joined to the real one.

## What's in the app now

- The incremental mapper is the default. The global mapper stays available on the admin page and with `MAPPER=global`.
- Under the global mapper: a placing that fails the check (pairs over 5°, or under 80% agreeing within 12°) is placed
  again incrementally, and the better kept; the cameras outside the largest agreeing group are dropped. This fixed JBMF
  on hal (9 cameras dropped, no crossing sheets), but would not have caught Caveman's eb199.
- Under the incremental mapper, nothing is dropped: placings are left as they were before Nov 15.

## Tests to make the global mapper safe again

The bar is the one benchmarking set: faster without being worse. "Worse" now has to include these faults, judged on
boulders like the ones that failed.

1. **A hard set of boulders.** Add Caveman, JBMF, Railer, Wedge, Turnbuckle and Morgan Crack to Drifter, 31de and 9f3b.
   Keep each one's photos and features (like `experiments/mapper`'s fixtures). Tom judges each incremental mesh good or
   bad by eye once, as ground truth.
2. **A doubled-surface measure.** On the dense points: the share of points with a second surface close behind them
   along their normal (a few cm to ~0.5 m). Ghost sheets show as a high share; one rock face as near zero. Check that it
   ranks the known-bad meshes above the good ones before using it as a rule.
3. **Agreement measured against the incremental model, not alone.** For each boulder, align the global model to the
   incremental one (on the cameras both place) and count cameras more than ~2° or ~5% of the boulder's size away. A
   block misplaced shows up directly, whatever the lenses.
4. **The global mapper's own settings.** COLMAP 4.2.1's options for its view graph (lens calibration from the pairs,
   minimum inliers per pair) and for rotation averaging and track filtering. Look up which exist, then try stricter
   ones on the hard set. Web photos' unknown lenses are the likeliest cause, so known focal lengths from EXIF are worth
   a run of their own.
5. **Weak links cut first.** Before the global mapper, drop pairs with few inliers, or pairs tying a video to the rest
   through one or two frames. Then place each well-linked part on its own and merge only where they agree.
6. **Hybrid placing.** The incremental mapper on the stills (or the best-linked core), and the global mapper or image
   registration for the rest. Or COLMAP's hierarchical mapper, never benchmarked.
7. **A check that catches Caveman's eb199.** Find a measure separating "two halves 5-8° apart" from good web-photo
   scatter (per-group rotation offset, residuals of each half against the other's points). Calibrate it on the hard set
   with Tom's labels; no line without that (see the per-area and no-untested-thresholds rules).

Each test should report, per boulder: photos placed, the doubled-surface share, cameras off the incremental model, and
the time saved. Only a method passing all of these on the hard set goes back to being the default.
