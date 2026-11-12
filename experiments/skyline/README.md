# Facing from the skyline

This test asks whether a mesh's facing (the compass bearing of its -Z axis) can be found by matching the skyline in its
placed photos against the horizon computed from elevation data at its coordinates. It's an experiment for deciding what
to build, and nothing in the app uses it.

## How it works

`skyline.py`, for each mesh:
1. **The horizon.** It computes the elevation angle of the horizon every 0.25° of bearing, seen 1.6 m above the ground
   at the climb's coordinates, with the earth's curve and refraction. Heights come from
   [AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/) (Terrarium PNGs): zoom 14 (about 8 m) out to
   2.5 km, then zoom 10 (about 120 m) out to 50 km. USGS's 3DEP service, which the viewer's ground uses, refused a 60 km
   square (502).
2. **The photos' skylines.** SegFormer-b0 (ADE20K, ONNX on the CPU) labels each placed photo. A skyline point is a
   column where sky runs down from the top edge and meets mountain or hill, away from the mesh's own outline in that
   photo. The photo's camera turns each point into a bearing and elevation in the mesh's frame.
3. **Matching.** Every heading is tried, each with the best constant elevation offset within ±4° (for an "up" a little
   off). The loss is robust and asymmetric: points above the horizon count 0.15 as much, because trees, rocks and near
   ground add height that the elevation data doesn't have. Each photo counts once. The margin is the loss of the best
   heading at least 15° away, divided by the best's.
4. **`--parallax`** keeps only points that another photo, taken at least 8 m away, sees at the same bearing and
   elevation (within 0.5°). Those must be about a kilometre away or more, so they're terrain the elevation data has,
   not a dome or tree nearby. This needs the mesh's scale (scale.json).
5. **Checks without ground truth.** There's no reliable facing to compare against: 11 meshes have a compass reading,
   each from a single photo.
   - `--halves` matches two halves of the photos separately, with each video's frames kept in one half.
   - `--pairs` takes meshes that share photos (a pooled boulder mesh and its climbs' own meshes) and fits the rotation
     between their frames from the shared cameras. That rotation says what the difference between their headings
     must be, which can be compared with what their skylines say.

## What it found (2026-11-11)

**Joshua Tree and the Tramway: no.** I ran hal's 31 meshes there (21 at Joshua Tree, 10 at the Tramway on San
Jacinto).
- Without the filter, the 10 with both a compass reading and a skyline came out 23-175° from it. Margins were mostly
  1.0-1.1, a flat loss.
- Near domes, boulders and trees fill the skyline. Their horizons are mostly 100-700 m away, at elevation angles that
  change with every few metres of position.
- With `--parallax`, 29 of 31 lost all their points. White Rastafarian kept 596 points and gave 141.5° (margin 3.8),
  the same as without the filter (142.25°). Gunsmoke Wall kept 8 points, 3.3° from its compass reading, which proves
  little.

**The Buttermilks (Bishop): to about ±10°.** I pulled four meshes from the server, all at the same Mountain Project
point. Their horizon is a mix of the White Mountains and the Sierra, with its median 7 km away. With `--parallax`:

| mesh | photos with a skyline | heading | offset | margin | halves | compass | text |
|---|---|---|---|---|---|---|---|
| Drifter/Smoking Boulder (pooled, 139 photos) | 58 | 126.25° | -4.0° | 2.3 | 125.75°, 127.5° | 143° (one photo) | 189° |
| Smoking | 3 | 113° | -4.0° | 5.8 | 112.75°, 113° | | |
| East Face Sit | 7 | 117.75° | -2.0° | 7.2 | 117.75°, 117.5° | | |
| Les Trois Maunets | 7 | 114.5° | -2.0° | 8.6 | 114.25°, 115° | | |

The halves agree within 2°, but the pairs show that this overstates the precision:

| pair (shared photos) | frames say h_a - h_b | skylines say | off by | ups differ by |
|---|---|---|---|---|
| Les Trois Maunets - East Face Sit (15) | -3.6° | -3.25° | 0.3° | 0.2° |
| Smoking - East Face Sit (15) | -1.0° | -4.75° | 3.8° | 3.6° |
| Smoking - Les Trois Maunets (15) | 2.6° | -1.5° | 4.1° | 3.7° |
| Drifter - Les Trois Maunets (13) | 0.9° | 11.75° | 10.9° | 1.9° |
| Drifter - East Face Sit (13) | -3.0° | 8.5° | 11.5° | 1.7° |
| Smoking - Drifter (20) | 1.7° | -13.25° | 15.0° | 7.0° |

So in Drifter's frame, the three small meshes put its heading at 111.5-115°, and its own photos at 126°. Its web photos
alone give 125.75°, and its video frames alone give 126°. Which photos and which points go in shifts the answer by up to
about 10°.
- **The cause.** Most skyline points lie east to southeast, where the horizon rises steadily. There, turning the heading
  a few degrees looks much like raising or lowering every point, so heading trades off against the elevation offset.
  The offset that should absorb a slightly wrong "up" often sits at the -4° bound.
- **The evidence.** The same photo's skyline points land at bearings within 1-3° of each other in two meshes, but at
  elevations 0.3-1.8° apart, and that moved the best heading by 5° or more. One shared photo was placed about 140°
  differently in the two meshes.
- **Fitting the tilt doesn't fix it.** Fitting the mesh's tilt (2 more parameters, within 3°) along with the offset
  moved Drifter only to 123.5°, and the others hardly at all. Drifter's points also fit its horizon 10-20 times worse
  than the small meshes' do.

All the estimates for Drifter fall within 111-127°. Its text guess, 189°, is 60° or more away. The single compass
reading (143°, true north) is 16-32° away. Phone compasses are often that far off, so this doesn't settle which is right.

**The lidar ground** in USGS's 1 m data shows boulders as bumps of their own: 15 m within 40 m of Drifter's point, and
6 m with 28 and 58 m² footprints near Wrangler at Joshua Tree. A mesh's footprint could be matched against those bumps,
which would give its facing and position in exactly the places where skylines fail.
[experiments/footprint](../footprint/README.md) tried that next, and it didn't work.

## Cost

- **Storage.** 126 Terrarium tiles (12 MB) covered both areas, and areas share them. The segmentation model is 15 MB.
- **Compute.** 3-45 s a mesh on 6 of hal's CPU threads, almost all of it segmentation (0.15-0.3 s a photo). It needs
  no GPU.

## What it would take to use

Only where the skyline is distant terrain: the Buttermilks, Bishop's other areas and open alpine areas, not Joshua
Tree's domes or forest like the Tramway's and Fontainebleau's. Before building it into the app:
1. **Ground truth.** Turn a few Buttermilks meshes by hand in Nearby (to the boulders around them and the trail), then
   compare these estimates against them.
2. **Weigh the alternatives.** Footprint matching against the lidar ground
   ([experiments/footprint](../footprint/README.md)) did no better than chance at Joshua Tree, and turned the Buttermilks
   meshes half way round.
3. **Make it hold up.** Weight points by how much they constrain the heading (peaks and notches over steady slopes),
   and fit each photo's own small tilt rather than one offset per mesh.
4. **Skip what has no clear skyline, before segmenting.** Segmentation is nearly all of the cost, and on hal's 31
   meshes only 48% of the 1,222 photos had any skyline at all.
   - Skip a whole mesh whose horizon is near, from the elevation data alone. Its median distance was 7.4 km in the
     Buttermilks, against 0.1-3.3 km at Joshua Tree and the Tramway, where nothing survived the parallax filter.
   - Segment only a few frames of each video. The 935 frames came from 86 videos, and neighbouring frames repeat the
     same skyline.
   - Try a cheap test for sky along a photo's top edge (bright and smooth) before segmenting it.
   - Skipping photos whose camera points too far down saves little: only 2% had their top edge below level.

## Running it

It needs onnxruntime in the venv, the segmentation model, and a data folder like the app's:

    VIRTUAL_ENV=venv uv pip install onnxruntime
    mkdir -p data/cache/models/skyline
    curl -L -o data/cache/models/skyline/segformer-b0-ade.onnx \
        https://huggingface.co/Xenova/segformer-b0-finetuned-ade-512-512/resolve/main/onnx/model.onnx

    venv/bin/python experiments/skyline/skyline.py --data data --parallax --halves --pairs MESH_ID...

`--data` is a folder holding `meshes/<id>` and `projects/<project>/photos`. That's the app's own `data/`, or a few
meshes copied from the server, with `--index` given a saved copy of `GET /api/meshes` for their places. Results go to
`out/` here (ignored by git): a plot per mesh (the horizon in grey, each photo's skyline points coloured, and the loss
by heading below), `results.jsonl`, and `pairs.jsonl`. `--photos 'yt_*'` (or `'ddg_*'`) uses only video frames (or
only web photos). Tiles are cached in `data/cache/terrarium`.
