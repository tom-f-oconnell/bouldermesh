# Facing and place from the lidar's boulders

This test asks whether a mesh's facing, and where it really stands, can be found by fitting its shape against the
boulders that USGS's 1 m lidar keeps as bumps in the ground. It's an experiment for deciding what to build, and nothing
in the app uses it. It followed [experiments/skyline](../skyline/README.md), which fails where near rock fills the
skyline, as at Joshua Tree.

## How it works

`footprint.py`, for each mesh:
1. **The ground.** It uses the viewer's 300 m lidar square around the climb's coordinates (`backend/terrain.py`, cached
   in `data/cache/terrain`). That becomes a signed distance field: how far each place is above the lidar's surface, or
   under it. A mesh's walls then count as well as its tops.
2. **The mesh.** Its cleanup-kept vertices are put in metres (scale.json) and thinned to one per 0.5 m (0.25 m when
   refining), with its lowest at 0. Its photos' cameras are placed the same way.
3. **The search.** It tries every 5° of heading and every metre within 40 m of the coordinates, with the mesh's foot at
   the lowest ground near its middle (±1.5 m).
   - Points in the air above the lidar count 1. Points under its surface count 0.5, as lidar fills in overhangs. Each
     counts up to 2 m.
   - Cameras under the ground add as much again.
   - Distance from the coordinates adds a little.
   - The best four headings are refined to 1° and 0.5 m.
   - Optionally, it also tries tilts of the mesh's "up" (`--tilt`) and a wider foot height (`--lift`).
4. **Checks.** Each mesh is compared with its compass reading, Mountain Project's text and the skyline's heading.
   `--pairs` takes meshes that share photos, as experiments/skyline does. Their frames' known turn says how far apart
   their headings must be, and their shared photos should land in the same places.

## What it found (2026-11-11)

**hal's meshes (Joshua Tree, the Tramway, Yosemite): no better than chance.** 182 of 196 were placed; 13 had no
scale. Agreement with the other sources, at the default 40 m reach and at 15 m with a stronger pull to the coordinates:

| | 40 m | 15 m | chance |
|---|---|---|---|
| text within 45° (67 and 65 meshes) | 34% | 29% | 25% |
| text within 90° | 67% | 55% | 50% |
| compass within 45° (10 and 9) | 30% | 44% | 25% |
| meshes of one boulder (13 pairs): headings within 15° | 15% | 54% | 8% |
| the same: their shared photos placed within 3 m | 15% | 23% | |

The ones that looked confident (margin of 0.3 or more, fitting at least 40% better than flat ground) did no better.
The 15 m pairs agree more because both meshes are pulled to the same coordinates. At Camp 4, the lidar has no bump for
Midnight Lightning's boulder near its coordinates.

**The Buttermilks: consistent, but backwards.** The four Bishop meshes from experiments/skyline all landed on the
Drifter boulder's bump.
- **Consistent:** within 0.4-6 m of each other, with headings within 20° once their frames' turns are allowed for.
- **Backwards:** they came out at 309-336°, half a turn from everything else. The skyline gives 113-126° and the
  compass 143°, and that compass photo looked east (89°). The photos themselves were taken from uphill, west of the
  boulder, looking across the Owens Valley to the White Mountains.
- **Fitting the shape alone:** that put the truth second, at 129-141°. Drifter scored 0.62 there, against 0.50 for the
  backwards fit.
- **Other options didn't help:** tilting the mesh's "up" by up to 4° each way, letting its foot move 4 m, and leaving
  the cameras out all kept the backwards fit.

**Why it fails**
- **One face fits many walls.** Most meshes are one face of a rock: Wrangler's is 5.2 m wide and 1.3 m deep. A wall
  fits the side of most boulders, and a Joshua Tree pile has many within 40 m.
- **Blurred, symmetric bumps.** Bare-earth lidar at 1 m blurs a boulder into a rounded bump, and a big rounded boulder
  looks much the same turned half way round.
- **The camera check misfires on slopes.** Cameras underground should rule out the backwards placement. But on a slope,
  a few degrees' error in a mesh's "up" puts its uphill cameras a metre or two underground. At Drifter that ruled out
  the right answer instead.

## Cost

The lidar for hal's meshes is 146 squares of 300 m (51 MB), which the viewer's Nearby uses too. At a 40 m reach, a
mesh takes about 28 s on one core, and all 196 took about 12 minutes on 8 cores. At 15 m they take 4 s each.

## What could still work

- **As a refinement only,** within about 30° of a facing from the skyline, a compass or a hand turn. At the
  Buttermilks, its second-best fit was 15° from the skyline's answer, about as far apart as the skyline's own
  estimates were from each other. That isn't shown to help.
- **USGS 3DEP's full lidar point clouds** (on AWS, in EPT format) rather than the bare-earth grid. They keep boulders'
  tops and some sides at several points a square metre. That would sharpen shapes, but not fix one face fitting many
  walls.
- **Ground truth by hand.** Turning meshes by hand in Nearby is still needed for checking any of these.

## Running it

It needs scipy in the venv, and the data folder experiments/skyline describes. The lidar is fetched (once) as the viewer
fetches it.

    venv/bin/python experiments/footprint/footprint.py --data data --jobs 8 --pairs --all

The results go to `out/` here (ignored by git):
- **A plot per mesh.** On the left is the lidar's bumps 50 m around the coordinates: lighter is higher than the ground
  around. The mesh is drawn in red (lighter is higher), its cameras in cyan, the coordinates as a blue cross and the
  mesh's middle as a red cross. On the right is the loss by heading, with the other sources' headings marked.
- **`results.jsonl` and `pairs.jsonl`.**

`--skyline` takes experiments/skyline's `results.jsonl` to compare against. The other options are in `--help`.
