# BoulderMesh — prototype photogrammetry web app for boulders

Upload photos or a video → reconstruct a mesh (COLMAP + OpenMVS) → view it in the browser.
Import climbs from Mountain Project / OpenBeta / Kaya, put them on a map, link meshes to
the problems on them, and try **rough photogrammetry from publicly available photos**
of a boulder (source-page photos, Google Images, geotagged Commons/Flickr photos, beta
video frames).

## Run it (no dependencies beyond Python 3.10+)

    python3 backend/server.py          # open http://localhost:8470 (PORT=<n> to change)

Optional extras, each unlocks a feature and is detected at runtime:

| extra | gives you |
|---|---|
| `pip install pillow` | demo-mode mesh, image de-duplication, blurry-frame rejection |
| `pip install opencv-python-headless numpy` | the view-graph diversity estimate (how many photos would register); rejecting harvested photos of other rocks |
| `pip install torch torchvision` | keeps climbers and spotters out of reconstructions (in the Docker image) |
| `pip install torch open_clip_torch` | also rejects harvested photos that don't look like a boulder at all (350 MB model download; rarely adds much) |
| `yt-dlp` + `ffmpeg` on PATH | frames from linked YouTube/Vimeo beta videos |
| `ffmpeg` on PATH | videos you upload to a project, turned into its photos (its 200 sharpest frames, spread over its length) |
| COLMAP + OpenMVS (see Docker section) | real photogrammetry instead of demo relief meshes |
| `GOOGLE_CSE_KEY` + `GOOGLE_CSE_CX` | Google Images via the Custom Search JSON API (100 free queries/day; existing keys only, see Known gaps) |
| `FLICKR_KEY` | geotagged Flickr photos around the climb's coordinates |
| `pip install segno` | QR codes of the notifications' topic on the admin page, for a phone to scan (in the Docker image) |

Set `BOULDERMESH_UA="BoulderMesh/0.2 (you@example.com)"` so the sites you fetch from can reach you.

Harvests also search DuckDuckGo images by default. That needs no key, but it's an unofficial
endpoint: if it breaks, the harvest logs that and carries on without it. `HARVEST_DDG=0` turns it
off.

Without COLMAP/OpenMVS the server runs in **demo mode**: Reconstruct produces a relief
mesh from the first photo (tagged `demo`) so every other feature can be exercised.

## Climb metadata — what each source gives you

| | Mountain Project | OpenBeta | Kaya |
|---|---|---|---|
| access | public HTML, parsed one page per import | open GraphQL API (`api.openbeta.io`), CC BY-SA data | **none** — JS-only web app, no public API, ToS forbids scraping |
| problem name / grade / FA / type | ✓ | ✓ | manual / your own export |
| coordinates | route-level GPS when set, else the boulder (area) GPS, with elevation | climb-level lat/lng when set, else the parent area | manual |
| boulder name | lowest area in the breadcrumb (e.g. "Big Columbia Boulder") | parent area with `isBoulder` | manual |
| photos | user photos (`…/assets/photos/climb/{id}_{size}_{ts}.jpg`; harvest takes `_large_`, ~4× `_medium_`) | user uploads, CC BY-SA | — |
| videos | the route's beta-video gallery + YouTube/Vimeo links in comments | links found in descriptions | paste a link |
| bulk | Route-Finder CSV export (`cli.py mp-csv <area_id>`, MP's own feature; area coords only) | name search, `cragsNear` proximity search | ask KAYA (support@kayaclimb.com) |
| cross-refs | `mp_id` | `mp_id` on climbs and areas → MP↔OpenBeta matching | — |

Terms: the Adventure Projects (Mountain Project) terms disallow automated/bulk scraping,
so that adapter is deliberately low-volume — one page per import, disk cache, robots.txt
honoured including its `Crawl-delay` (60 s on MP, so each further MP page or photo takes a
minute; documented APIs like Commons and Flickr are exempt from robots rules, which address
crawlers). Importing an area summarises its problems from the area page's own route table;
a problem's page (photos, description, route GPS) is fetched when you import it directly,
press *Fetch problem page*, or harvest it. Use OpenBeta for anything bulk. KAYA's terms
prohibit scraping and circumventing their app, so the Kaya adapter only records links
and imports files you already hold.

### CLI

    python cli.py fetch  https://www.mountainproject.com/route/105835104/midnight-lightning
    python cli.py fetch  https://www.mountainproject.com/area/109861444/big-columbia-boulder   # area + problem summaries
    python cli.py fetch  https://openbeta.io/climbs/<uuid>
    python cli.py search "Camp 4 Boulders"              # OpenBeta by name
    python cli.py search --near 37.7417 -119.6035 --radius 1500
    python cli.py mp-csv 105833471 --type boulder        # Mountain Project route-finder export
    python cli.py harvest <climb url> ./out              # photos + sources.json + diversity.json
    python cli.py diversity ./out/photos
    python cli.py relevance ./out/photos [--anchors DIR] [--move]   # which are of the boulder? (report only without --move)
    python cli.py jobs [--server URL]                   # what restarting the running server would interrupt
    python cli.py offtopic-split [--limit N]            # sort photos rejected before CLIP into bouldering or not

Records are `climbs.common.Climb` dataclasses (`to_dict()` → JSON): name, kind, grade(s),
fa, rating, lat/lng + `coord_source` (route | area | parent-area | manual), elevation,
area_path, boulder_name, description/location/protection, images[], videos[], cross_refs.

## Linking meshes to climbs

* Every climb gets a row in `climbs`; `mesh_links` is many-to-many. Meshes also carry their own
  lat/lng (inherited from the first linked climb, editable).
* Create a project with a climb selected, or harvest from a climb page, and the resulting
  mesh is auto-linked and geolocated.
* A climb's card lists its own meshes, then its boulder: the boulder's own mesh (below), and the
  other climbs on it, marked ▣ where they have meshes of their own. Climbs share a boulder when
  their area paths match, unless the last area's name says it holds several (`... Boulders`,
  `... Area`, `... Cluster`). Areas used to link a climb to the mesh made for another climb on its
  boulder rather than mesh it; those links show as "made for" that climb, as such a mesh may not
  show the other side of the rock.
* `GET /api/geojson` serves climbs + meshes for the map tab (Leaflet, OSM/OpenTopoMap).
* The *Meshes* tab sorts by newest, faces, photos placed, distinct viewpoints, coverage around the
  rock, or the share of its photos placed, either way, and *Filter* narrows it to the areas ticked
  and, for each of those numbers, between a least and a most. Distinct viewpoints count photos taken
  within 15° of the same direction from the rock once, so many frames of one video count about once;
  coverage is how much of a full circle round the rock its photos see from above (`meshmetrics.py`,
  from where its photos were placed, kept in each mesh's `metrics.json`). Each card names the areas
  it was made for, with its first climb's Mountain Project path on hovering. The choices are
  remembered in the browser.
* **Rating a mesh:** *Rate*, at the 3D view's bottom right, takes bad, OK or good and a comment, which grows as it's
  typed. They're kept in the browser and sent to the site whenever either changes; the site keeps one of each per
  browser, wherever it's sent from (`cli.py mesh-ratings` lists them, with the comments). The Meshes tab sorts by everyone's average
  or your own rating (unrated past the bad end), can hide those rated bad (1.5 or under on average, bad 1, OK 2, good
  3) or those you rated bad, and each card shows your rating and the average.
* In the Areas tab, the boulder a climb is harvested for names its Mountain Project page, and each of its climbs opens
  under Climbs, with its photos.

## Harvest → rough photogrammetry

1. Open a climb → *Harvest photos* (*Harvest again* once it has one). Queries are built from
   the boulder name, area and problem name; geo sources use the coordinates. Everything lands
   in `data/projects/<id>/photos/`, with provenance in `sources.json`. YouTube is searched, and its videos fetched
   (or volunteers asked for both), alongside the photos rather than after them, each with its own progress bar, as
   Mountain Project's minute between requests makes its photos slow.
2. Only photos found for the climb are kept, so that its mesh shows it. The page photos and video
   frames that other harvests of its boulder kept still count as known photos of the rock for the
   filter below.
3. De-duplication, then the relevance filter (`climbs/relevance.py`): title cards and blank
   frames go, then photos that don't look like bouldering at all (CLIP), and photos from
   area-wide sources (web search, Commons, Flickr) must match known photos of the boulder from
   its climbs' pages or videos, or photos already matched to them, or if there are none, the
   largest group of photos that match each other. This mostly saves time, as COLMAP ignores most
   junk anyway. Rejected photos stay in `rejected/`, with a reason in `relevance.json`, and the
   project card lists them by reason, those that look like bouldering first, to put any back.
   Untick *filter off-topic photos* to skip it. `python3 cli.py offtopic-split` sorts harvests
   filtered before the CLIP check the same way. Matching search photos against known ones is most
   of a harvest's time on a small server (about 150 ms a pair on one core, nearly all of them junk
   tried against every known photo), so what's worked out is kept in `data/cache/relevance.sqlite`,
   by the photos' contents: each pair's matches and CLIP's verdict. A climb harvested again, or a
   boulder pooling its climbs' photos, only works out what's new. The admin page's *Areas* section
   chooses how pairs are matched: brute force (the original, and the default) or FLANN's approximate
   nearest neighbours, 1.6 times as fast but missing about a tenth of the matches (design.md); each
   keeps its own record.
4. Diversity check (`climbs/diversity.py`): with OpenCV, ORB matching + fundamental-matrix
   RANSAC between every pair to build a view graph. Pairs explained by a 2-D similarity
   (crops, re-encodes, pan/zoom) are discounted because they carry no parallax. The largest
   connected component ≈ number of photos COLMAP could register → verdict
   **good** (≥12) / **marginal** (6–11, or 3–5 among at least 10 photos, as the estimate is often far
   too low there, and COLMAP gives up within minutes when it can't place 6) / **poor** (the rest).
   Poor sets are not reconstructed automatically; add your own photos to the project and press
   Reconstruct to force it. In an area, COLMAP placing too few, or those it places making a mesh of under
   1,000 faces, counts as skipped, not failed.
5. Reconstruction runs COLMAP in internet-collection mode (per-image intrinsics,
   affine-shape SIFT, domain-size pooling, guided matching), picks the largest sparse model
   and requires ≥6 registered images before densifying with OpenMVS. With torch, people are
   masked out at every stage, so they don't get painted onto the rock.

Expect *rough* results: web photos mix cameras, seasons, chalk and people; a famous
boulder with dozens of distinct angles (e.g. Camp 4 classics) is realistic, an obscure
problem with three topo shots is not. Beta videos that pan around the rock are a good
source, and web search adds the most: DuckDuckGo took Midnight Lightning from about 30
placed photos to about 65, and from about 190k faces to about 540k.

Videos come from the climb's page and from a YouTube search for its name, V grade and area,
through yt-dlp. A result is kept only if its title names the climb, and the title or description
also gives its area, boulder or grade, as names like "Relic" aren't unique. Up to 6 videos are used,
the page's first. Each video's uploader, description and licence are recorded, and the uploader's
channel description too, along with any climber's height, wingspan or ape index stated in them
(`GET /api/videos`, and on the climb's card), for scaling meshes later.

YouTube is asked for one thing at a time across the whole server, 8-15 s apart. When it answers with its
bot check ("Sign in to confirm you're not a bot"), the server leaves it alone for an hour, then 2, 4 and 8
hours while it still does. Meanwhile harvests skip videos, finding beta waits, and the *Beta* tab says until
when; volunteers fetch them if there are any ("Volunteers fetching YouTube videos"), or else an area's *look for new
videos* gets them later. A refused search pauses searches the same way, apart from downloads, and harvests meanwhile go
without searching (design.md has when searching would move to volunteers).

People are found in every photo before reconstruction, and their masks are kept in
`data/projects/<id>/masks/` (`<photo>.png`, black where people are), with the share of each photo
that is people in `masks/people.json`. Only photos without an up-to-date mask are looked at again.

Downloaded images stay on your server for reconstruction only; most are copyrighted by
their photographers. `sources.json` lets you credit or remove any of them.

## Mesh a whole area

On the *Areas* tab, paste a Mountain Project area URL, such as
`https://www.mountainproject.com/area/105720495/joshua-tree-national-park` (or several, one a line, which go one after
another), and pick an order:

- **Most popular**, Mountain Project's own ranking.
- **Highest rated**, by average stars.
- **Most photos and videos**, judged from the pages of the next few popular climbs.

*First N meshes of each* stops each area once it has made that many meshes, in its order, so that, say, 1 meshes the
most popular climb of each of several areas unattended. A boulder meshed from several of the area's climbs (below) counts
once, and all of the area's climbs on it are harvested for it, wherever they come in the order. A climb or boulder that
fails, or is skipped for too few viewpoints, brings in the next, unless *Count failures towards limit* is on. Empty means
no limit. Entering an area again, or changing N on its card, takes it up again with the new settings: raising N goes on
from where it got to, without meshing again what's done (as long as the order hasn't changed). It keeps the list
Mountain Project gave when it was first entered, unless *For areas listed already, check Mountain Project for climbs
added since* is on, which adds those after the rest.

Areas whose lists overlap, as a subarea entered before or after the area holding it, share what's been made: a climb
meshed by one, on its own or with its boulder, is done in the other with that mesh. Those skipped or failed are tried
again.

*More options*, when starting an area or on its card, both on by default:
- *Don't mesh a boulder again from the same photos*: a boulder whose current mesh was made from the photos its climbs'
  harvests hold now isn't meshed again when one of its climbs comes round again (as after a restart interrupted it); that
  climb is done with the boulder's mesh. New photos, or a new climb on it, still mesh it again, as does *Rebuild*.
- *Keep the mesh before when a rebuild from the same photos places fewer*: COLMAP can place fewer photos of the same set
  from one run to the next. When a rebuild had every photo the mesh before placed and places fewer, the mesh before stays
  the current one, and the new one is kept as an older version of it.
- *When COLMAP places under 75% of the photos, try 1 more seed, keeping the best*: within one meshing, COLMAP's placing of
  the photos is tried again with another random seed (a minute or so for most) while the model it keeps places under that
  share of them, and the model placing most is used. 0 in either turns it off. Meshes made outside areas use 75% and 1.

Its boulder problems are then taken one at a time until you stop, or the list runs out. A climb that shares its boulder
with others on the list is only harvested; once all of the list's climbs on that boulder are, the boulder is meshed from
all their photos (below), and each climb is done with that mesh, seen from where its own photos were taken. While one
of those climbs or its boulder is worked on, its card lists the boulder's climbs: grey until each is harvested, then green
with the photos (page photos and video frames too) it gives, or red if none; once pooled, what each gives after the
boulder's filter, and what the boulder's own search found. ★ marks the climb that brought the boulder in, the first of
them in the area's order. With more areas running than workers, those being worked on are listed first, the one with
the newest mesh first, and the rest say *queued* until a worker is free for them. A climb alone
on its boulder, or on an area holding several (as "… Boulders"), is harvested and meshed on its own. A climb or boulder
without enough photos is skipped, and the run moves on. A climb isn't meshed until YouTube has been searched for its
videos, here or by a volunteer: without one to search, it waits, tried again each day, and *Fetch the videos and searches
harvests went without* on the admin page asks a volunteer added since. *Mesh climbs without searching YouTube for their
videos first*, when starting an area or on its card, meshes them anyway. The admin page's *Areas* setting meshes every climb on its own
instead, as areas did from Nov 9 to 10, 2026, as a mesh made for a problem on one side of a rock may not show one on
the other side.

*Mesh N boulders from all their climbs' photos* makes each boulder with more than one climb on the
list a mesh of its own (below), harvesting first its climbs the list hasn't reached. *Try N skipped
climbs again* meshes climbs skipped for too few viewpoints that the viewpoint check would now let
COLMAP try.

*Stop now* pauses the climb in progress and *Stop after this climb* finishes it. Every finished mesh
stays, and *Resume* carries on from the last step the climb finished. A server restart loses no more
than that, and it carries on by itself afterwards, unless you'd stopped it. It also stops by itself after 5 climbs in a row fail before finishing a
harvest, which usually means a network problem, or with under 50 GB of disk left
(`CAMPAIGN_MIN_FREE_GB`).

*Retry N failed* tries an area's failed climbs again, each from the last step it finished. If the area
is running they go next, and if it isn't, only they run.

*Harvest again* gathers the photos and videos of every climb in the area that has photos of its own
again, from scratch, keeping their meshes, so it finds what's been posted since. *Rebuild N changed*
then meshes those whose photos changed, and *Rebuild all* meshes every climb with enough viewpoints
again, as after a change to the pipeline. These are queued like retries. A rebuilt mesh takes over
its climbs, and the one before it stays as an older version: *Show older versions* on the *Meshes*
tab lists those, and *Use this version* goes back to one. The API can also look only for new videos
(`{"videos_only": true}`), and rebuild each climb as soon as it has new photos (`{"then_rebuild": true}`). *Retry* on a failed project does the same for
that one, and its card and log say what ended it, including when something outside the app killed a tool.

Jobs you start by hand go first, between climbs. Expect each climb to take most of an hour on CPU,
and about 170 MB of disk plus up to a few hundred MB of download cache. Mountain Project's export
stops at 1000 climbs: in a bigger area, these are its most popular 1000.

## A boulder's own mesh

*Mesh the boulder from all its climbs' photos*, on a climb's card, makes a mesh of the whole rock from
every photo its climbs' harvests kept, in a project and mesh of its own (`<boulder> (boulder)`), which
leaves each climb's own mesh and harvest as they are. Climbs on it that have never been harvested are
harvested first, each in a project of its own, as an area would, which an area then meshes from when it
gets there. The pool takes each harvest's page photos and video frames first, the harvests taking turns,
then their other photos, up to 400; `climbs.json` in the project records which harvests were pooled for
each climb, and which of their photos are the climb's own.

Each climb is then placed on the mesh by its own photos, the ones from its page and its videos (a web
search finds the boulder, not the problem): it's seen from the middle one of those the mesh placed,
looking at where they all look (`backend/viewpoints.py`, `views.json` in the mesh). The climb's *Mesh*
button and the boulder chip on its card open the boulder's mesh from there, and the viewer's *Seen
from* menu moves between the climbs on it. Meshing the boulder again pools again and makes a new
version; the old one stays, as for rebuilt meshes.

A test copy's boulders can be moved to another server with what made them:

    python3 cli.py export-boulders bundle.tar --data <copy's data> [--boulder "Dino's Egg"] [--since 2026-11-10]
    docker compose exec -T bouldermesh python3 cli.py import-bundle /data/bundle.tar   # the bundle in ./data

The import adds the boulders' projects and meshes, the harvests behind them, and their climbs, matched
by their Mountain Project id, and replaces nothing. A public server's database is replaced each time
`deploy/ship.sh` runs, so import into the server you ship from.

## Beta: the holds climbers use

The *Beta* tab shows a mesh with its holds coloured as the Tension Board app does: green start holds,
blue hand holds, magenta foot-only holds and red finish holds (start and hand holds can be used as feet
too). *Find beta* looks at each video with frames placed in the mesh. It finds the climber's hands and
feet with a pose model, casts them onto the mesh, and keeps the points where one stayed still for at
least a second. The first holds the hands used are start holds, the last ones finish holds, and holds
only feet touched are foot holds. Each video has its own holds and tracks, which can be shown or
hidden, and a picture of its frame with the tracks and holds drawn on it. Expect a few minutes per
video on a CPU. Meshes built before Nov 3 didn't keep their cameras' lenses, and those before Nov 1
where their photos were placed, so they need rebuilding first. Each new mesh with video frames placed in it
has its beta found straight after, while its videos are still kept (*Find beta in each new mesh* on the
admin page's *Areas*); on machines that connect here, meshing comes first, then beta and size.

*Mark holds* lets you set the holds yourself: clicking the rock adds a start hold, and clicking a hold
again makes it a hand hold, then a finish hold, then a foot hold, then removes it, as setting a climb in
the Tension Board app does. These are saved with the mesh, apart from those found in videos.

The tab lists meshes with the most holds marked by hand first, then those with the most found in videos.
*Only show meshes with beta* hides those with neither.

*Find beta in all N not done* queues every mesh with video frames whose beta hasn't been found, and *Stop
finding beta* unqueues them. Beta is found one mesh at a time, on a thread of its own at the lowest CPU
priority, so it never holds up meshing, and a restart only pauses it. 148 meshes had 364 videos to look
at (Nov 2026): about 10 hours on the CPU, more while meshing runs alongside.

## How big a boulder is

Photogrammetry alone can't tell a boulder from a pebble. Once a mesh is reconstructed, it's sized from
the climbers in its photos: their limb lengths against usual body proportions, at the depth where their
hands and feet meet the rock. Where a video or its channel gives the climber's height or ape index, that's
used. People standing near the rock and Mountain Project's height for the problem count too, for less.
The panel at the bottom left of the viewer then says how tall the rock is where the photos look, with a
range. Videos of one rock agree to about 10% on the median rock, and Mountain Project's height to about
25% for half of them ([design.md](design.md) has the rest). *Find size* finds it again. Meshes made
before Nov 8 haven't been sized: `curl -X POST localhost:8470/api/meshes/scale-all` queues them all (with
`-H "Authorization: Bearer $EDIT_TOKEN"` on a site with an editing token), at a
few minutes each, at low priority. A mesh is sized again when its beta has been found, and
`python3 cli.py span-check` (with `DATA_DIR`) compares the climbers' reach there with their limbs.

For your own photos, a bottle of known size gives a mesh its size better than any of these. Stand a 32 oz
Nalgene (or another bottle) upright at the foot of the rock, on ground that the photos also show, and get it
in several of them. On the project's card, pick it under *Something of known size in these photos* ("another
bottle…" asks its height). Finding the size then looks for bottles in the placed photos (Faster R-CNN, a 175 MB
download the first time), measures each one standing on the ground in the mesh, and goes by it most.

## Real photogrammetry

    docker compose up --build      # COLMAP + OpenMVS (+ OpenCV, ffmpeg, yt-dlp); first build ~10-40 min depending on cores

(`PORT=<n> docker compose up --build` if 8470 is taken), or natively on Ubuntu: `apt install colmap python3-opencv ffmpeg`, build OpenMVS (see
Dockerfile, including its one-line patch), `pip install yt-dlp`, then `python3 backend/server.py`.

**With an NVIDIA GPU** (and its container toolkit), photo matching takes 2-3 minutes rather than
12 for 100 photos. Give the container the GPU with `docker-compose.gpu.yml`, which this makes the
default on that machine:

    echo COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml >> .env

It hands the GPU over as a CDI device, which needs the toolkit's spec: `nvidia-ctk cdi list` should show
`nvidia.com/gpu=all` (toolkit 1.18 and later make it at boot; otherwise
`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`). Docker's `gpus: all` would lose the GPU
whenever the container's limits change (`docker update`) or systemd reloads.

It's used if COLMAP has CUDA, and has 4 GB of its memory free when matching starts. Otherwise, or if
it runs out partway, matching uses the CPU, so a shared GPU is fine. The top of the page says which
it would use now, and each job's log says which it did. `USE_GPU=0` keeps it on the CPU.

**When COLMAP can't place the photos** (its mapper places fewer than 6), VGGT places them instead, and COLMAP refines
that. This needs the GPU with about 7 GB of its memory free. The first time, it downloads its 4.7 GB of weights into
`data/cache/models`. A mesh made that way has a `placement.json` beside it. `VGGT_FALLBACK=0` turns it off.
[design.md](design.md#when-colmap-cant-place-the-photos-backendvggt_placepy) has how it works and how well it did.

A reconstruction produces a textured `mesh.glb` (texture PNGs alongside), or an untextured
`mesh.ply` if texturing fails (which can still be sized, and have its beta found, but not cleaned up),
levelled assuming most photos were taken upright. On CPU,
OpenMVS's 11-photo sample set takes 3-4 minutes, and a 100-photo harvest about an hour.

If a browser can't load a mesh's texture, the viewer tries it smaller, then says why on the view. Grey
on a mesh means no photo shows that side, including the backs of surfaces.

*Solid* and *Scene* in the header choose what's behind a mesh: the page's colour, or a sky over ground the colour of the
rock's foot, with a soft shadow under it (the default, until a browser chooses *Solid*). Around a reconstructed mesh with a splat (`scene.splat`, from `backend/splat.py`),
*Scene* also shows what its photos saw around the rock, where enough of them saw it, and the plain sky and ground
elsewhere. Fitting splats is off (`SPLATS=1` turns it on for new meshes, and `python3 cli.py splat <mesh_id>` fits one),
as it needs CUDA builds of torch and gsplat, and the few photos most meshes are placed from make poor ones: design.md,
"Scenes around meshes". A splat is learnt from all of a mesh's photos, so it's shown only where the site shows all its textures.

The climbs list hides climbs without meshes until that box is unticked. Each climb's *View* opens its mesh, and the one
in view is blue. *Random* opens any mesh; it's blue until one is in view.

On a phone, the page shows either the controls or the view. The controls' header holds *Viewer* (grey until a mesh is
opened, then with the climb in view named under it), *Random*, a ⚙ button (the mesh as *Surface* or *Wireframe*, the
background, and colours), *Volunteer*, and *Edit…* or *Editing*. The view's bar holds ← back to the controls, *Random*,
⚙, *Sway*, and at its right end *Map*, which swaps the mesh for the map, and then reads *3D*. While a mesh loads, its
progress shows over whichever is on screen, and the view takes over once it's ready. Over
the mesh it shows only the climb's name, small, at the top, so texture problems, stray-piece cleanup, *Hide notes* and
the iPhone gravity comparison are only shown on wider screens (over 860 px). A long press on the view gives a link to the
mesh, to copy or share.

The map opens around the mesh in view, its narrower side 1 km across (the admin page's *The map opens around a boulder
this many km across*), with the point of its climb open as if clicked: the climb whose *View* opened it, or the one a
boulder's mesh is seen from, else its own climbs or its boulder's, if they're all at one point. Once the map has been
moved, it's left where it is.

The page's address follows the mesh in view (`#mesh=<id>`, and `&climb=<id>` for a boulder's mesh seen from one of its
climbs), so it can be copied from there too, and opening such a link shows that mesh.

Each mesh's card has a bar of where its photos came from, by source, in the same colour for each
source everywhere. Hovering it, focusing it or tapping it shows each source's licence terms, and the
licence recorded for each photo where the source gives one (Commons, Flickr). It counts the photos
placed in the mesh, or for meshes from before Nov 1, every photo it was given.
`GET /api/meshes/{id}/sources` lists each photo with its URL, page, credit and licence.

A reconstruction is levelled from the gravity readings iPhones record in their photos, if any of its
placed photos has one. Otherwise down is estimated by assuming most photos were taken upright. The
view shows both and how far apart they are, or why there's no reading. Meshes have no real-world size.

Reconstructions come with stray pieces, so each one hides those that have under 1% of the main piece's
faces, or are over 5x sparser. This never changes the mesh's own files: on the view, *Edit* lets you
click any piece to hide or show it, and *Remove cleanup* shows the whole mesh again. The download
button leaves out whatever is hidden. It needs numpy, and only works on GLB meshes.

## Run it as a service

To have systemd start the Docker Compose stack at boot, restart it if it fails, and keep its log:

    sudo deploy/install-systemd.sh              # writes /etc/systemd/system/bouldermesh.service
    docker compose down                         # if you'd started it by hand
    sudo systemctl enable --now bouldermesh
    journalctl -u bouldermesh -f                # its log

It runs as the user who ran the installer, who must be in the `docker` group, from this
directory, and reads `.env` here (`COMPOSE_FILE` for the GPU, `PORT`). Each start rebuilds the image
first, which takes seconds when nothing has changed, so `sudo systemctl restart bouldermesh` deploys
new code. To remove it: `sudo systemctl disable --now bouldermesh`, then delete the unit file.

**Restarting.** A restart only pauses areas' climbs and finding beta, which carry on afterwards. Jobs started outside an
area (a harvest or reconstruction started by hand, a retry, recolouring from the admin page)
can't carry on by themselves. So when it's stopped, the server takes no new work and first waits up to 10
minutes for those jobs, if they look like finishing by then. Its log and the top of the page say what it's
waiting for, and stopping it again stops it at once. Projects it doesn't wait for come back *interrupted*,
not failed: *Retry*, or *Retry N interrupted* on the *Projects* tab, carries on with each from the last step
it finished, as it was asked for. To see what a restart would interrupt now:

    python3 cli.py jobs                         # exits 1 if a restart would interrupt anything

## Putting it on the public internet

As it comes, anyone who can reach the port can change anything, which suits a LAN and nothing else. The public
setup puts Caddy in front for HTTPS, keeps the app's own ports on the machine itself, runs the container as an
ordinary user, and makes the site view-only for everyone but you:

    mkdir data                                  # owned by the user the container runs as: uid 1000, a fresh machine's first user
    echo SITE_HOST=boulders.example.com >> .env  # its name in DNS, pointed at this machine, for the certificate
    echo WWW_HOST=www.boulders.example.com >> .env  # optional: a second name, also pointed here, that redirects to the first
    echo EDIT_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))") >> .env
    docker compose -f docker-compose.yml -f docker-compose.public.yml up -d

Visitors see meshes, climbs, beta and areas (how far each has got, and what's done and next, without the controls that
start or change one); *Edit…*, with the token, brings the rest.

The image isn't built there: compiling COLMAP wants cores and memory a small server hasn't got, so the public setup runs
one built elsewhere. On a machine with the meshes to show:

    deploy/build.sh                          # from the commit checked out: bouldermesh:public (2.5 GB), bouldermesh:gpu (10.4 GB)
    deploy/ship.sh you@boulders.example.com bouldermesh:public   # pushes both, the server pulls its own; meshes, database

The server's image has torch's CPU build, as it meshes nothing itself. The GPU one, with the CUDA libraries VGGT needs, is
for the machines that mesh (see "Running reconstructions elsewhere"). The server pulls its image from the
registry, so a ship moves only the layers that changed, usually a few MB. Its earlier builds' images go once nothing runs
them. `NO_PUSH=1` sends the image whole instead. `GPU=0 deploy/build.sh` builds only the server's.

Areas are stopped in the copy, so nothing runs there by itself. Projects' photos don't go, so their cards show none, and
the database there is replaced each time, all but the settings changed on its admin page. Once the public server harvests
by itself instead ([volunteers.md](volunteers.md), "The public server harvests"), `HARVESTS_HERE=1` in its `.env` makes
ship.sh send it only the image, as its database and meshes are then the only copies (`IMAGE_ONLY=1` does that for
any machine). `PHOTOS=1` sends projects' photos and masks as well (about 9 GB from hal's 1,667 projects on Nov 10, 2026),
which rebuilding, retexturing and finding size there need, never replacing any it has. Or push the image to
a registry (see "Running reconstructions elsewhere"), put `BM_IMAGE=ghcr.io/<github-user>/bouldermesh:latest` in `.env`,
and `docker compose -f docker-compose.yml -f docker-compose.public.yml pull`. Ports 80 and 443 must be open to the world.
To run it under systemd as above, put `COMPOSE_FILE=docker-compose.yml:docker-compose.public.yml` in `.env` too (with
`docker-compose.gpu.yml` between them on a machine with a GPU) and install the unit; its build step then has nothing to build.

**Deploying, once the public server harvests by itself** (`HARVESTS_HERE=1` in its `.env`), from hal's checkout, where
the images are built (hal's own copy of the site was stopped on Nov 10, 2026: a deploy is only the public server's):

    ./deploy.sh

which runs `deploy/build.sh && deploy/ship.sh bouldermesh bouldermesh:public && ssh bouldermesh 'cd bouldermesh && git
pull --ff-only && sudo systemctl restart bouldermesh'`, and then, if hal's worker runs here as a system service
(`bouldermesh-worker`, loaded and active), restarts it with sudo, so that it runs the worker script as checked out.

- It builds what's committed, other sessions' commits too: `git log` first says what would go.
- Changes to the database apply themselves when the server starts, and the Caddyfile and Compose files come with the
  pull. A change that needs a new setting in the server's `.env` says so; it goes in before the restart.
- The restart cuts off work in progress: a climb hal is meshing starts over as a new job (up to an hour of hal's), areas'
  climbs pause and carry on, and jobs started by hand get 10 minutes. On the server,
  `docker compose exec -T bouldermesh python3 cli.py jobs` says what's running.
- Changes only to what runs on hal (the worker script, `deploy/backup.sh`, the systemd units) need no deploy: restart the
  worker (`sudo systemctl restart bouldermesh-worker`), or copy a changed unit into `/etc/systemd/system/` and
  `sudo systemctl daemon-reload`.
- Volunteers' and machines' scripts from version 3 update themselves from the server once it serves a newer one (a
  higher `VERSION` in `volunteer/bouldermesh_volunteer.py` or `worker/bouldermesh_worker.py`), between fetches or jobs.
  Bump `VERSION` when a change should reach them. Older copies stay as they are until downloaded again, and older
  volunteer scripts still fetch videos, not searches.
- `PHOTOS=1` isn't needed again. Never take `HARVESTS_HERE=1` out: the next ship would replace the server's database
  with hal's.
- Room for images: each build shares all but its last few layers (the code, under a MB) with the one before, and Docker
  keeps a layer once however many images have it, so a deploy that changes code takes almost no room. Only a change to
  the Dockerfile's earlier steps (the packages it installs, as for VGGT) brings new layers of GBs. build.sh keeps this
  build's names and the one before's (`KEEP_BUILDS`, 2) and drops older ones', deleting the images that leaves nameless,
  unless a container uses them; ship.sh deletes the server's unused images before sending one. `GPU=0` skips the
  machines' build when nothing about meshing changed. `docker image prune` is safe whenever: it deletes only images with
  no name that no container uses. `docker image prune -a` would also delete named ones no container runs, such as the
  build hal's worker runs between jobs, and `docker builder prune` the build cache, after which the next build compiles
  COLMAP and OpenMVS again.

**Changing things.** With `EDIT_TOKEN` set, the page has an *Edit…* button in its header: enter the token there (it
stays in that browser) and the controls appear, with the Areas and Projects tabs. The button then says *Editing*, and
*Forget edit token* beside it forgets the token in that browser. Without it, the site shows only what's
there, and every change sent to it is refused before its body is read, and logged with the sender's address. Scripts
send `Authorization: Bearer <token>` with each POST, PATCH or DELETE. Viewers need nothing. Leave `EDIT_TOKEN` out of
`.env`, or set `READ_ONLY=1`, for a site no one can change from a browser; the comments in `deploy/Caddyfile` show how
to refuse changes at the edge as well. Only one token exists, so the site doesn't know who changed what: that's for
accounts, later.

**The admin page** stays on this machine's loopback address (`ADMIN_BIND` is refused if it isn't private). Reach it through
an SSH tunnel, `ssh -L 36184:127.0.0.1:36184 you@boulders.example.com`, at `http://localhost:36184`, and set
`ADMIN_PASSWORD` in `.env` anyway. What the site may show is set there too, but the public setup already starts it as
*Source photos and video frames: free to copy, with links to the rest*, so the copies here of photos that aren't clearly
free to copy aren't served (most harvested photos are copyrighted: [license_considerations.md](license_considerations.md)),
and *Mesh textures: all*. `SITE_SOURCES` and `SITE_TEXTURES` in `.env` change those starting points; choices made on its
admin page last across `deploy/ship.sh`, which keeps the machine's own settings when it replaces the rest of its database.

**What else the public setup does.** The container has no capabilities, can't gain privileges, and runs as uid 1000
(`BM_USER` in `.env` changes it; `./data` must be writable by that user). Pages come with a Content-Security-Policy that
allows only the site's own scripts, by their hashes, and the two CDNs; images and map tiles from anywhere; and no framing.
If you change `frontend/index.html`, the hashes follow, but a new inline `onclick` or another CDN needs the policy changed
in `backend/server.py` (`CSP=0` in `.env` turns it off, and `CSP=<policy>` replaces it). Files under `data/` are served only
as meshes, textures, JSON or bytes, so nothing uploaded there can run as a page of the site, and uploaded meshes keep only
mesh, texture and material files. Meshes, photos and static files are sent with `Last-Modified`, and may be cached for an
hour (`FILE_CACHE_SECONDS`). The page itself is sent with `Cache-Control: no-transform`, so that a proxy in front leaves
it as it is: Cloudflare, for one, adds its Web Analytics script to free plans' pages unless that's turned off, and the
page's policy would only block it. Harvests fetch only `http(s)` URLs, and never from private, loopback or link-local addresses,
such as a cloud machine's metadata service (`HTTP_ALLOW_PRIVATE=1` lifts that, for tests).

**Room.** The public setup caps its data at 60 GB (`DATA_CAP_GB` starts it, and the admin page's *Room for data*
changes it; no cap elsewhere): measured every 10 minutes, and over it, volunteers are given nothing and areas take no new
climb (they say they're paused) until there's room again. *Room for data* also shows what takes it, and deletes the
photos the filter rejected, which are only kept to put one back by hand and were 92 of hal's 97 GB of projects. Fetched
videos have a cap of their own within it, `VIDEO_CACHE_GB`. With notifications on, your phone is told when less than
0.5% of the cap is left (about 5 climbs' and boulders' worth at 60 GB), and when the cap is reached, each once until
there's room again; *Room for data* changes the %, or sets a number of GB instead, and says how many climbs' worth both
are. Above 70% of the cap, what the backup holds and nothing uses is offloaded to it ("Offloading to the backup", below).
ship.sh deletes the server's images no container uses before sending a new one, so that each ship leaves only the build
before.

**What it doesn't do.** There's no rate limiting, and `GET /api/meshes` reads every mesh's records for each request, so a
busy site wants a cache in front: Cloudflare's free plan, with the name proxied and SSL mode *Full (strict)*, caches the
textures by itself and the meshes once a cache rule makes `/meshes/` eligible, limits requests, and hides the machine's
address, and Caddy still gets its certificate through it. Its proxy refuses any request over 100 MB, so the page sends
files in requests under 80 MB, and a bigger one in parts of 64 MB (`POST /api/uploads`), which carry on from where they
got to if the connection drops. Caddy's access log
(`docker compose logs caddy`) still names each visitor, as `client_ip`, because `deploy/Caddyfile` believes Cloudflare's
addresses when they say who it is, and so does the app's own log. A firewall can then let ports 80 and 443 in only from
those addresses ([cloudflare.com/ips](https://www.cloudflare.com/ips/)), so that no one goes around the proxy. Cloudflare
changes them rarely; the public setup checks its list once a day, and the admin page says when it no longer matches the
Caddyfile's, which the firewall should follow too. Each project's log is
public, as the Projects tab shows it, so it reveals where photos came from and the names of compute machines, not
credentials. Back up `data/` yourself: a mesh is an hour of compute. Compute credentials are kept as before
(encrypted on the admin page, or in `.env`), and cloud machines accept SSH only from this server's address.

**Backups.** Once the public server harvests by itself, its data exists nowhere else. `deploy/backup.sh`, on another
machine (hal), copies it each night over SSH, with nothing connecting to that machine, into `BACKUP_DIR`:

- `current/`: the server's `data/` and `.env` as they are now, but not its caches of web pages and models, uploads in
  progress or work folders. The `.env` holds the editing token and the key saved credentials are encrypted with, so the
  folder is readable only by you.
- `db/<date>/`: that night's databases. The server copies them every hour into `data/snapshots/` with SQLite's backup
  API (`SNAPSHOT_MINUTES`), as copying a database in use isn't safe.
- `changed/<date>/`: whatever that night's copy replaced or deleted in `current/`, so a mesh deleted on the server can be
  had back. Both kept 30 days (`KEEP_DAYS`).

It uses a key of its own with no passphrase, which the server takes only for reading its checkout with rsync. On hal:

    ssh-keygen -t ed25519 -N '' -C 'bouldermesh backup' -f ~/.ssh/bouldermesh_backup
    printf 'command="rrsync -ro /home/tom/bouldermesh",restrict %s\n' "$(cat ~/.ssh/bouldermesh_backup.pub)" | ssh bouldermesh 'cat >> ~/.ssh/authorized_keys'
    BACKUP_DIR=/data/tom/bouldermesh-backups deploy/backup.sh tom@178.105.53.11:     # once by hand, then each night:
    sudo cp deploy/bouldermesh-backup.service deploy/bouldermesh-backup.timer /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now bouldermesh-backup.timer

To restore onto a new server, put `current/data` in its checkout's `data/` and `current/.env` beside it, then copy a
night's `db/<date>/bouldermesh.sqlite.backup` and `volunteers.sqlite.backup` into `data/`, and `machines.sqlite.backup`
into `data/compute/`, each without its `.backup`.

**Offloading to the backup.** Above 70% of the cap (*Room for data*'s *offload* %, `storage.offload_percent`; 0 never),
the server deletes, down to that %, what the backup holds and nothing uses any more, oldest first in each kind and in this
order: photos the filter rejected, after 3 days; fetched videos, once every climb they're of has been meshed (or skipped
or failed) and beta has been found in each of its meshes; the photos and people masks of climbs and boulders meshed 2
weeks ago, or skipped a month ago, none of whose boulder's climbs is still to be harvested or meshed; and the big files of
older versions of meshes replaced 2 weeks ago. The web cache's month-old pages go too (the backup keeps none). Meshes,
the databases, and each climb's and mesh's records always stay. In two steps, so that nothing goes that the backup can't
be shown to hold: what's to go is marked (`data/offload.list`, which the backup reads first, and keeps whatever the server
does); it's deleted once the backup says a copy that began after the mark went well. A job that needs photos that went
(rebuilding, a boulder's mesh pooling a climb's, finding beta or size) asks for them back and waits, as for a machine;
the backup sends them back (into `data/restore-inbox/`, which the server puts in place). *Room for data* shows what's
offloaded, marked, and asked back. That needs a second key on hal, which the server takes only for the handshake
(`deploy/backup-handshake.py`: saying when a good copy began, what's wanted back, and sending it):

    ssh-keygen -t ed25519 -N '' -C 'bouldermesh backup handshake' -f ~/.ssh/bouldermesh_handshake
    printf 'command="python3 /home/tom/bouldermesh/deploy/backup-handshake.py",restrict %s\n' "$(cat ~/.ssh/bouldermesh_handshake.pub)" | ssh bouldermesh 'cat >> ~/.ssh/authorized_keys'
    sudo cp deploy/bouldermesh-restore.service deploy/bouldermesh-restore.timer /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now bouldermesh-restore.timer     # what's wanted back, half-hourly

Without that key nothing is offloaded. backup.sh finds it as `~/.ssh/bouldermesh_handshake` (`BACKUP_HANDSHAKE_KEY`).

**Troubleshooting.** The commands in `cli.py` that read the data run inside the container, where it is (`/data`), from
the checkout whose `.env` names the compose files. On the server:

    cd bouldermesh
    docker compose exec -T bouldermesh python3 cli.py client-errors -n 50   # what failed in visitors' browsers, newest last
    docker compose exec -T bouldermesh python3 cli.py mesh-views --hours 24  # the meshes they've looked at lately, and how often
    docker compose exec -T bouldermesh python3 cli.py mesh-ratings          # how they rated meshes, and their comments
    docker compose logs --since 2h bouldermesh | grep "browser error"         # the same, as they came, in the app's log
    docker compose exec -T bouldermesh python3 cli.py jobs                   # what a restart would interrupt now
    docker compose logs --since 2h bouldermesh                               # everything else the app logged (or journalctl -u bouldermesh)

or from your own machine, `ssh you@server 'cd bouldermesh && docker compose exec -T bouldermesh python3 cli.py client-errors'`.
A browser's report has its time, the build it was using, what failed (a mesh that wouldn't load, with its id, or an error in
the page), and the browser, but not who sent it. Each mesh shown is kept too (`data/mesh-views.jsonl`, and a "viewed mesh"
line in the log): which, how it was opened (random, climb, meshes, map, area, project, beta, link), how long it took, and its note,
such as a texture loaded smaller, but not who or with what. The requests for the mesh files don't tell you that, as browsers
and Cloudflare keep copies for an hour. The same commands work on any machine running it under Docker.

## The admin page

A separate page, on port 36184, sets what the main site shows and where its reconstructions run. It
listens only on this machine's loopback address unless `.env` says otherwise, and never on every address:

    ADMIN_BIND=10.0.0.5                         # this machine's LAN address, to reach it from the LAN
    BOULDERMESH_SECRETS_KEY=...                 # to save credentials on it (see below)
    ADMIN_PASSWORD=...                          # optional: then it asks for user "admin" and this password

Then `docker compose up -d` and open `http://10.0.0.5:36184`, or press *Admin* at the top of the main page,
which editors see. Where the browser can't reach it, *Admin* shows how to forward its port over SSH instead:
`ssh -N -L 36184:127.0.0.1:36184 you@server`, then `http://localhost:36184`. `ADMIN_SSH=you@203.0.113.7`
in `.env` fills in the address (behind Cloudflare, the server's own, not the site's name). Anyone who can
reach it can change settings, so keep it off public addresses (it refuses to start on one). It refuses
changes sent by other sites' pages, and requests naming a host that isn't an address or a LAN name
(`ADMIN_HOSTNAMES` adds names).

**What the site shows.** "Free to copy" means public domain, CC0, or a Creative Commons licence without
No Derivatives, or your own uploads ([license_considerations.md](license_considerations.md) has why).
- *Mesh textures*: all of them; or only those made from photos free to copy, with a version of each
  other mesh coloured from only those photos where *Make textures from only the photos free to copy*
  has made one (else plain grey); or none. Hidden textures aren't served at all, and meshes come without them.
- *Source photos and video frames*: all; or those free to copy, with only links to the rest, whose copies
  here aren't served; or only those free to copy.
- *The map opens around a boulder this many km across*: its narrower side, when it opens on a mesh (1 by default).

The coloured versions are made per vertex from the photos free to copy, so they're less sharp than a
texture, and grey where none of those photos sees the rock. Meshes built before Nov 3 need rebuilding
first, as they didn't keep their cameras' lenses.

**How long meshes take.** Each mesh keeps how long it took (`timings.json` in its folder): where it was made (this
server, or which machine), each tool's time, CPU time and memory, how many photos were placed and whether VGGT placed
them, and for one another machine made, the wait for it and the time from start to finish. Its details on the main
page say so (*Made:*), and the admin page gives medians by what made them and how many photos went in, over the last
week or all. Meshes made before Nov 10 get theirs from their job logs at startup: only their tools' times.

**Credentials** are saved encrypted with `BOULDERMESH_SECRETS_KEY`, which stays in `.env`, outside
`data/`. Make one with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
They're never shown again. Without that key, give them as environment variables instead
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` in `.env`).

## Running reconstructions elsewhere

By default this server does everything. *Reconstructions at once on this server* can be raised, which
runs climbs side by side (two areas then share the workers), though most steps already use every core, or set to 0,
for a small server that only harvests: then every climb waits for another machine to mesh it.
On the admin page, *Run more on* adds up to *At most this many other machines at once* (2 by default):

- **Machines reached over SSH**: any you keep running, with Docker (and NVIDIA's container toolkit for
  the GPU), listed as `user@host[:port]`, all reached with one private key saved as a credential.
- **AWS EC2**: instances started when there's a climb to mesh, a spot `g4dn.2xlarge` (T4 GPU, 8 cores)
  by default, from AWS's Deep Learning GPU image. Each one is reached over SSH from this server's address
  only, and ended after *End an idle cloud machine after* minutes without work. One also ends itself
  after a while with no job running, in case this server has gone, and any left running from before a
  restart are ended. Its credentials need EC2 (run, describe and terminate instances, security groups)
  and SSM's GetParameter. *Test the connection* checks them without starting anything.
- **Machines that connect here for work**: for any this server can't reach, such as one behind a lab's firewall. On the
  machine, which needs Python 3.9, Docker you may use, and NVIDIA's container toolkit for the GPU, run:

      curl -O https://<site>/worker/bouldermesh_worker.py      # or worker/bouldermesh_worker.py in this repository
      python3 bouldermesh_worker.py --server https://<site> --cpus 12 --check

  The first time, it makes the machine's token, asks a name (optional) and registers it; the machine then waits under
  *Machines* (shown with this choice) for *Approve*, here or from a notification on your phone (see "Approving
  volunteers and machines" below). Or *Add machine* there shows a command with a token of the admin page's, approved
  from the start (clicking the machine in the list shows it again).

  It asks this server for a climb every 15 s over HTTPS, fetches its photos, runs the job in Docker as you, with no
  capabilities, at niceness 10, and sends the log as it goes and then the mesh. Ctrl-C gives the climb back. The
  server, token and options are remembered (in `~/.bouldermesh-worker`); `--check` only registers or checks the token, after which
  `deploy/bouldermesh-worker.service` runs it under systemd. If this server won't take its token (unknown, revoked or
  turned down), it forgets it, warns, and registers a new one, as with none, which waits for *Approve* again. When the
  server serves a newer version of the script, it replaces itself with that between jobs, never during one, and starts
  again (`--no-auto-update` stops that; the repository's own copy is left to git, so `deploy/bouldermesh-worker.service`
  runs a downloaded one, in `~/.bouldermesh-worker`; the admin page shows why a machine doesn't update). If the script is too old and can't update itself, it says so and exits with status 3, which the unit doesn't
  restart. `--forget-token` forgets the
  remembered token, and `--register` makes a new one in its place. Jobs' photos and results, and the models they download (a
  few GB), go there too, or in `--data DIR`, such as on a bigger disk. Before starting, and before each job, it checks
  there's room there for two climbs (3 GB each) and the models still to download (5.5 GB the first time; `--need-gb`
  changes that), and before pulling an image, room in Docker's storage for about four times its download (18 GB if the
  registry won't say); short of either, it gives the job back and stops, saying what's short. Each job's folder goes when
  it's done, and an image it pulled goes once it pulls a newer one. It shares the machine: it takes no climb while others
  keep its GPU busier than 50% (`--busy-gpu-util`; a median over 3 min, and quiet again over 5 before it carries on), or
  while less than 24 GB of memory is free (`--need-ram-gb`; also `BOULDERMESH_NEED_RAM_GB`), and only one whose GPU
  memory fits in what's free (what VGGT may need to place 20 of its photos: 7.6-9.3 GB; 4 GB for beta or size). Its CPU
  use is held down by `--nice` and `--cpus` instead. A climb it has started, it finishes. It tells this server why it's
  held back, which keeps each machine's state as it changes (working, idle, held back, awaiting approval, offline once
  it stops asking for 2 min) and logs each change: *Machines* on the admin page shows each one's now and for how long,
  its share of the last day and week in each, and the latest changes, and a climb waiting for held-back machines says
  why in its log. An idle machine's state says what this server is doing instead (areas harvesting, climbs harvested
  and waiting for the rest of their boulder's, beta waiting for videos). Every job is kept in `machine_jobs` (its
  machine, task, when it was made, taken and ended, and how: done, failed, released, or gone), each ending is logged
  with how long it took and waited, and once an hour the server logs each machine's minutes in each state. The script
  keeps its own record in `usage.jsonl` in `~/.bouldermesh-worker` (each job, and each change between working, waiting
  for climbs and held back), and logs once an hour how the hour went. Its other options: `--gpu auto|all|cdi|none` (`cdi` for
  a host whose GPU is a CDI device), `--docker-arg=--memory=64g` (repeatable), and `--image-prefix ghcr.io/<owner>/`
  to refuse any other image, should the server be taken over. The public setup's Caddy passes `/worker/` to a port that
  answers only that (`WORKER_PORT`, 47614); elsewhere, publish that port to where the machines are. With
  *Reconstructions at once on this server* at 0, finding beta and finding a mesh's size go to these machines too: a
  mesh's files, its placed photos and masks, and for beta its videos from the cache, cut to the 150 s it looks at
  (they never ask YouTube themselves). One waiting for a machine is tried again once one is there.

Machines run the same commit as this server, in its GPU build (torch's CUDA libraries, for VGGT), which they pull from a
registry. `deploy/build.sh` names both builds for GitHub's by their commit: `ghcr.io/<owner>/bouldermesh:<commit>` for
the server, and `:<commit>-gpu` for machines (`REGISTRY` for another registry). `deploy/ship.sh` pushes them there, and
as `:latest` and `:latest-gpu`, before the server gets its own. Left empty, *Docker image the machines run* is then the
GPU build of the commit the server runs, so each ship moves the machines with it, and a machine that has an older build pulls the new one
by its new name; the admin page shows which. Pushing to GitHub's registry needs gh's token to be allowed to write packages
(`gh auth refresh -h github.com -s write:packages`, once); ship.sh logs in with it for the push only. The package starts
private: make it public in its settings on GitHub (*Change visibility*), or `docker login` wherever it's pulled. Naming
an image on the admin page instead pins the machines to it, as for a private image on ECR, which works with an
*Instance profile* that may pull it. Each climb's photos and people masks go to the machine, and the mesh, its cameras and
the masks come back. Harvesting stays here. A climb whose machine can't be had or is lost waits, rather
than failing, and that worker tries again in 5 minutes. Its area's card says so: which kind of machine it waits for
(one that connects here, one reached over SSH, or an AWS instance), to mesh which climb or boulder, since when, why,
when it tries again, and for machines that connect here, when one last asked for work. design.md has what offloading costs and saves.
The AWS side has been tested against stand-ins for AWS, not against AWS itself.

## Volunteers fetching YouTube videos

YouTube refuses a server's downloads after a while (a cloud server's at once), so people can lend their computers to
fetch videos from their own addresses, slowly. Their script makes its own token, asks their name (optional), and
registers it; they then wait under *Volunteers* on the admin page for *Approve* (see "Approving volunteers and machines"
below). A token this server won't take, as one revoked or turned down, the script forgets, saying so, and registers a
new one, which waits for approval again; `--forget-token` forgets it by hand. Or *Add volunteer* there shows a token, approved from the start, and the command to give them. `https://<site>/volunteer/`, which the main page's *Volunteer* button opens wherever this
server answers volunteers, has three ways to help, as buttons. *Collect videos* has the script and says what it does.
*Compute meshes* has the machines' script (above), what a computer needs for it (an NVIDIA GPU, as Docker can't use a
Mac's), and how to run it as the user's own systemd service, which starts with the computer. *Data collection* says
what's coming: uploading one's own photos and videos, and a way to take photos with a scale reference in them, to scale
and orient existing meshes. The video script needs Python 3.8 and
installs yt-dlp and Deno in `~/.bouldermesh-volunteer` (with uv's Python 3.12 there, under a Python older than yt-dlp's
3.11), and on first run explains the costs (YouTube's terms, and the
bot check their whole network may get) and asks them to agree. [volunteers.md](volunteers.md) has every step, for
volunteers and for connecting the public site to whichever server harvests.

- **What they fetch:** only YouTube ids and search words this server gives them, one thing at a time, 8-15 s apart, at
  most 15 an hour and 100 a day for each volunteer and each address (less, on the admin page), resting 1, 2, 4 then 8
  hours when YouTube refuses them. A video comes back whole (up to 1080p, no sound, 500 MB), with its details but
  nothing of theirs. It must be as long as they say, and as long as another volunteer's search listed it, if one did:
  their own word alone only catches a file gone wrong; a search, its results (kept 30 days); a channel, its description, for climbers' heights.
- **How YouTube treats them:** with each result, the script (version 5 on) says what yt-dlp warned of and how fast the
  video came. A fetch that failed as YouTube holds back (PO tokens, SABR, signature and challenge failures, formats
  missing) is *degraded*; one stopped partway (HTTP 403), or a video far slower than the volunteer's usual, *throttled*,
  and it rests an hour. The admin page's Volunteers list shows each one's state, why, its last speed and yt-dlp; your
  phone is told when one gets worse (with notifications on), and when several are degraded at once, which is likely
  YouTube changing something that a newer yt-dlp fixes, the page says so. Warnings when a fetch worked are only noted.
- **An area's climbs wait for them:** a climb whose harvest asked volunteers for a search or videos is harvested again
  when they've come, and meshed once, rather than meshed without them first; after 24 h (`VOLUNTEER_WAIT_HOURS`) it's
  meshed with what's come.
- **What's asked of them:** with *Fetch videos from YouTube* at its default, videos a harvest or finding beta couldn't
  have because YouTube is refusing this server, once there's a volunteer. *Fetch the videos harvests went without* asks
  for earlier ones too, and *Fetch videos' missing details* for licences never read. A climb is harvested again once its
  videos have come: an area's as *harvest again and rebuild*, which a finished area runs by itself and a stopped one when
  it's started. *Only through volunteers* is for a cloud server, which shouldn't ask YouTube itself at all.
- **Fetched videos** are kept (`data/cache/videos`, up to `VIDEO_CACHE_GB`, 50 by default and 10 in the public setup)
  for finding beta and harvesting again, so YouTube isn't asked for one twice; this server's own downloads too. Beta
  can only use a video with a frame placed in a mesh, which was about half of those harvests used (387 of 739 on hal):
  the rest are deleted (`prune_videos()`, hourly) once every climb that took frames from them, and its boulder's mesh,
  is settled, and they haven't been used for 6 hours; their details stay, and only harvesting again would fetch them
  again. One offloaded to hal's backup is forgotten there. The admin page's *Areas* turns this off.

Volunteers reach the public site's `/volunteer/`, which Caddy passes to a port that answers nothing else (`VOLUNTEER_PORT`,
47613 in Compose, on loopback only). By default that's the public server's own app, which harvests, with its meshes made
by machines that connect to it for work (above; volunteers.md, "The public server harvests", has every step). The other
way, while another machine harvests (as hal did until Nov 10, 2026), `deploy/volunteer-tunnel.service` there keeps an SSH connection to the public
server that brings `/volunteer/` back to it, through a socket in the public server's `data/tunnel/`. It runs with no one
logged in, so it has a key of its own, with no passphrase, which the public server takes for forwarding only:

- on the harvesting machine, make it: `ssh-keygen -t ed25519 -C 'volunteer tunnel' -f ~/.ssh/bouldermesh_tunnel`, with
  an empty passphrase;
- on the public server, add its `.pub` to `~/.ssh/authorized_keys` as a line starting
  `restrict,port-forwarding,command="/bin/false" `, put `StreamLocalBindUnlink yes` in
  `/etc/ssh/sshd_config.d/20-tunnel.conf` and reload sshd, so a socket left from a dropped connection is replaced, and
  `mkdir -p ~/bouldermesh/data/tunnel` before Caddy is restarted, so that folder is yours rather than Docker's.

Then install the unit (its first lines say how), put `VOLUNTEER_UPSTREAM=unix//run/bm-tunnel/volunteer.sock` in the public
server's `.env` and restart it, and put `VOLUNTEER_URL=https://<site>` in the harvesting machine's `.env`, for the command
the admin page gives. Each server keeps its volunteers in `data/volunteers.sqlite`, which `deploy/ship.sh` doesn't send.

## Approving volunteers and machines

Both scripts register the token they make, and each one waits for *Approve* on the admin page (beside it in its list)
or on your phone. Until then, under *Volunteers and machines that register themselves*:
- **Volunteers waiting** may fetch, at most 3 an hour and 10 a day by default, or nothing if unticked.
- **Machines waiting** get nothing by default. Ticked, they may mesh (only that), and a climb keeps the mesh one made
  for when you approve the machine; turned down, the climb is meshed again elsewhere.
- **Approved volunteers'** limits are set there too (15 an hour and 100 a day at most).
- **Registering:** at most 5 a day from one address, and 30 waiting at once, of each kind; ones never approved go after
  14 days without being heard from.

**On your phone:** install the free ntfy app (Android or iOS). On the admin page, *Make new topics*, then subscribe to
the topic it shows in the app (on ntfy.sh unless you name another server). Its QR codes save typing it: on Android,
the camera's scan of the first subscribes the app; on an iPhone, the second opens the topic's page, whose address ends
in the topic's name. Then *Send a test*: its *Reply* button checks
the phone's taps reach this server, which says so in a second notification. Each registration then comes with *Approve*
and *Deny*, or after three in ten minutes, one summary of the rest. The taps go through ntfy to a second topic this
server follows, signed so that no one else can make them; but anyone who learns the first topic's name can read the
notifications, and so tap them, so keep it private, and *Make new topics* if it gets out. design.md has why.

**Joining and leaving:** your phone is also told, by name, when a worker (a machine lending itself) or a downloader (a
volunteer) joins, as it starts asking for work, and leaves: its script stopped (it says goodbye, from worker script 5 and
volunteer script 6) or it hasn't been heard from for 10 minutes (a worker) or 30 (a volunteer: its script says it's still
there while it waits for its limits or rests; 26 hours for scripts before 6, which wait without a word). Not as it's
busy, held back for the GPU or memory, or resting, nor when its script restarts (back within 2 minutes). Each one is told of at most once an hour, so one that drops out and
back in is told of where it ended up, at the end of the hour; one that registered itself was told of by its registration.

**Cloudflare, in front:** a rate-limiting rule catches floods before they reach the server. The free plan has one, under
Security, then WAF, then *Rate limiting rules* (or *Security rules*, then a rate limiting rule, in newer dashboards):
- **If incoming requests match:** *URI Path* equals `/volunteer/api/register`, *Or* *URI Path* equals
  `/worker/api/register`; that is, `(http.request.uri.path eq "/volunteer/api/register") or (http.request.uri.path eq "/worker/api/register")`.
- **With the same characteristics:** IP.
- **When rate exceeds:** 5 requests in 10 seconds. **Then:** *Block*, for 10 seconds (the free plan's only period and
  duration). The server's own limits handle anything slower.

## Layout

    backend/server.py              HTTP server, SQLite, uploads, job worker (reconstruct + harvest)
    backend/pipeline.py            COLMAP/OpenMVS driver (single-camera and internet-collection modes) + demo fallback
    backend/occlusion.py           person masks for reconstruction
    backend/progress.py            per-step job progress, behind the UI's progress bars
    backend/climbs/common.py       Climb/Media types, cached + rate-limited + robots-aware HTTP client
    backend/climbs/mountainproject.py, openbeta.py, kaya.py   adapters
    backend/climbs/harvest.py      photo sources, download, provenance
    backend/climbs/youtube.py      finding climbs' videos, and climbers' heights and ape indexes in their text
    backend/provenance.py          where each mesh's photos came from, kept with the mesh
    backend/viewpoints.py          where each climb on a boulder's own mesh is seen from
    backend/bundle.py              moving boulders' meshes and their harvests from one copy of the data to another
    backend/beta.py                the holds climbers use, from their hands and feet in videos
    backend/scale.py               how big a mesh is, from the people in its photos and Mountain Project's height
    backend/admin.py               the admin page's server; frontend/admin.html is the page
    backend/settings.py            the admin page's settings, and credentials kept encrypted
    backend/volunteers.py          volunteers fetching YouTube videos: their tokens, the queue, and their port
    backend/splat.py               splats of the scene around a mesh, for the viewer's Scene background (off: SPLATS=1)
    volunteer/bouldermesh_volunteer.py  the script volunteers run
    backend/proxy_ranges.py        whether the Caddyfile's trusted proxies are still Cloudflare's
    backend/policy.py              which photos are free to copy, and what the site may show
    backend/retexture.py           colouring a mesh from only the photos free to copy
    backend/compute.py             running reconstructions on other machines, and starting and ending them
    backend/pullwork.py            machines that connect here for work: their tokens, the jobs, and their port
    backend/presence.py            telling of machines and volunteers joining and leaving, once an hour at most
    backend/registration.py        volunteers and machines registering their own tokens: names, limits, approval
    backend/notify.py              notifications on a phone through ntfy, with Approve and Deny buttons that reach back
    backend/remote_job.py          what such a machine runs
    worker/bouldermesh_worker.py   the script a machine that connects here runs
    backend/climbs/relevance.py    filter for photos substantially of the boulder
    backend/climbs/diversity.py    de-dup + view-graph diversity estimate
    backend/climbs/htmlmini.py     tiny DOM on html.parser (no bs4 needed)
    frontend/index.html            UI: Climbs / Projects / Meshes tabs, three.js viewer, Leaflet map
    cli.py                         command-line access to the same code
    deploy/                        systemd services and an installer, the public setup's Caddyfile, and ship.sh for a public server
    data/                          created at runtime; DATA_DIR overrides

## API

With `EDIT_TOKEN` set, every POST, PATCH and DELETE needs `Authorization: Bearer <token>`; with `READ_ONLY=1`, none is
accepted. `GET /api/status` says which (`editing`: open, token or none, and `editor` for the caller).

    GET  /api/status
    GET  /api/projects · POST /api/projects {name, climb_id?} · GET|DELETE /api/projects/{id}
    POST /api/projects/{id}/photos (multipart "photos") · POST /api/projects/{id}/reconstruct
    POST /api/projects/{id}/retry                       a failed or interrupted project, from its last finished step
    POST /api/projects/retry-interrupted                every project a restart interrupted
    GET  /api/jobs                                      what a restart would interrupt (jobs outside areas) or pause (areas' climbs)
    GET  /api/projects/{id}/harvest                     sources.json + diversity.json
    POST /api/projects/{id}/restore {file}              put a photo the filter rejected back
    GET  /api/meshes · POST /api/meshes (multipart "files", +name, +lat/lng, +climb_id) · PATCH|DELETE /api/meshes/{id}
    POST /api/meshes/{id}/links {climb_id, note} · DELETE /api/meshes/{id}/links/{climb_id}
    GET  /api/climbs?q=&source=&meshed=&doubtful=       the newest 500, or of every climb those with each word of q (name, boulder,
                                                        area or grade), with meshes, or whose coordinates look wrong
    GET  /api/climbs/count · GET|PATCH|DELETE /api/climbs/{id}
    POST /api/climbs/import {url, include_children}     MP / OpenBeta / Kaya URL
    POST /api/climbs/search {q, lat, lng, radius_m}     OpenBeta live search
    POST /api/climbs/manual {...} · POST /api/climbs/kaya-export (multipart csv/json)
    POST /api/climbs/{id}/harvest {sources:[climb,google,ddg,commons,flickr,youtube], reconstruct, filter}
    POST /api/climbs/{id}/boulder                       mesh its boulder from every photo of its climbs, harvesting those never harvested
    GET  /api/campaigns · POST /api/campaigns {area, order:popularity|rating|data, start, csv, without_youtube, max_meshes, count_failures, check_new,
                                               same_photos_once, keep_better, mapper_below, mapper_more}
    POST /api/campaigns/{id}/options {without_youtube, max_meshes, count_failures, same_photos_once, keep_better, mapper_below, mapper_more}   an area listed again is taken up again
    POST /api/campaigns/{id}/start · POST /api/campaigns/{id}/stop {now} · DELETE /api/campaigns/{id}
    POST /api/campaigns/{id}/retry                      its failed climbs, ahead of the rest
    POST /api/campaigns/{id}/harvest-again {videos_only, then_rebuild} · POST /api/campaigns/{id}/rebuild {changed_only, skipped_only}
    POST /api/campaigns/{id}/boulders                   its boulders with more than one climb, each meshed from all their photos
    GET  /api/videos                                    every video, with the climber's size if stated
    POST /api/meshes/{id}/make-current                  go back to an older version of a rebuilt mesh
    GET|POST /api/meshes/{id}/beta                      the holds found in its videos, and those marked; POST finds them
    POST /api/meshes/{id}/holds {holds: [{p, role}]}    the holds marked by hand, roles start, hand, finish or foot
    POST /api/meshes/{id}/scale                         find its size (scale.json: metres per unit, and each estimate)
    POST /api/meshes/scale-all {again}                  size every reconstructed mesh not yet sized, or all of them
    GET|POST|DELETE /api/meshes/{id}/cleanup · PATCH /api/meshes/{id}/cleanup {removed} · GET /meshes/{id}/mesh.glb?cleaned=1
    GET  /api/meshes/{id}/sources                       each photo in the mesh: source, URL, page, credit, licence
    On the admin page's port: GET /api/admin/state · POST /api/admin/settings · POST /api/admin/secrets
    POST /api/admin/actions/{test,end-nodes,retexture,machine-add,machine-revoke,volunteer-add,volunteer-revoke,
         volunteer-approve,volunteer-deny,machine-approve,machine-deny,notify-topics,notify-test,...}
    On VOLUNTEER_PORT and WORKER_PORT, with the token a script made: POST /volunteer/api/register · POST /worker/api/register {name, version, client}
    On WORKER_PORT, with a machine's token: POST /worker/api/work · GET /worker/api/jobs/{id}/input?offset=
    POST /worker/api/jobs/{id}/{log,output,done} · GET /worker/api/jobs/{id}/output (how much has come)
    GET  /api/geojson · GET /meshes/{id}/{file} · GET /photos/{pid}/{file} · GET /photos/{pid}/rejected/{file}
    POST /api/meshes/{id}/rating {browser, rating, comment}   a visitor's rating (1 bad, 2 OK, 3 good, or null) and comment,
                                                        from anyone, as client-errors are taken (120 an hour an address): one
                                                        each per browser, replaced; mesh_ratings; `cli.py mesh-ratings`
    POST /api/mesh-views {mesh, via, seconds, note?}    a mesh a visitor's browser showed, as client-errors are taken (120 in 10
                                                        min an address): kept in data/mesh-views.jsonl; `cli.py mesh-views`
    POST /api/client-errors {kind, message, mesh?, file?, page?, source?}   what failed in a visitor's browser, from anyone
                                                        viewing the site (not other sites' pages; 8 KB, 20 in 10 min an address):
                                                        kept in data/client-errors.jsonl; `python3 cli.py client-errors` lists them

## Development

[design.md](design.md) has the measurements behind the relevance filter and people masking.
`python3 -m venv venv && ./venv/bin/pip install pillow numpy opencv-python-headless torch torchvision open_clip_torch`
gets every optional check; the tests also pass without them.

    python3 tests/test_parsers.py     # offline: MP/OpenBeta/Kaya parsers on fixtures
    python3 tests/test_relevance.py   # offline: relevance filter on synthetic photos, progress tracking (CLIP downloads its model)
    python3 tests/test_meshmetrics.py # offline: the Meshes tab's numbers: distinct viewpoints, coverage round the rock
    python3 tests/test_pipeline.py    # offline: reconstruction steps with COLMAP/OpenMVS stubbed, progress parsing
    python3 tests/test_vggt_place.py  # offline: which photos VGGT places, its cameras as a COLMAP model, the rounds after
    python3 tests/test_splat.py       # offline: splats' cameras and lenses, their file, the rock's box, photos for fitting
    python3 tests/test_campaign.py    # offline: meshing an area, with harvest and reconstruction stubbed: order, stopping, restarts, retries, boulders' meshes
    python3 tests/test_viewpoints.py  # offline: placing each climb on a boulder's mesh by its own photos
    python3 tests/test_bundle.py      # offline: moving a boulder's mesh and its harvests to another copy of the data
    python3 tests/test_diagnostics.py # offline: what's recorded when real processes fail or get killed
    python3 tests/test_meshclean.py   # offline: finding and hiding stray pieces on small GLBs, the cleaned download
    python3 tests/test_orientation.py # offline: iPhone gravity readings, and comparing them with a model's down
    python3 tests/test_scale.py       # offline: sizing people, objects and the rock on scenes built with known sizes
    python3 tests/test_youtube.py     # offline: telling a climb's videos apart, climbers' sizes in text, leaving YouTube alone when it refuses
    python3 tests/test_beta.py        # offline: cameras, rays, contacts and roles for beta; waiting out YouTube and carrying on
    python3 tests/test_provenance.py  # offline: where each mesh's photos came from
    python3 tests/test_compute.py     # offline: running reconstructions on other machines, through stand-ins for ssh and AWS
    python3 tests/test_pullwork.py    # offline: machines that connect here for work, through the script and a stand-in for docker; registering, approval, held meshes
    python3 tests/test_notify.py      # offline: ntfy notifications, their signed Approve and Deny, following the replies
    python3 tests/test_backup.py      # offline: the server's database snapshots, and deploy/backup.sh copying a stand-in server
    python3 tests/test_admin.py       # offline: the admin page's settings, credentials and photo permissions
    python3 tests/test_api.py         # spins up the server on a free port, runs the whole flow incl. a harvest job
    python3 tests/test_public.py      # the editing token and read-only modes, other sites' pages refused, the page's policy and headers, what's served how

## Known gaps

- The Mountain Project parser and OpenBeta queries were checked against the live site and
  API on 2026-10-30, and the test fixtures mirror that markup. MP changes markup
  occasionally — `parse_route()` / `parse_area()` are the functions to adjust; OpenBeta
  surfaces the API's own error text if a field is renamed.
- One editing token or none, rather than accounts, so who changed what isn't recorded; and a single in-process job
  queue. No rate limiting: see "Putting it on the public internet".
- Google Images needs the paid-after-100/day Custom Search API; direct scraping of
  Google Images is not implemented on purpose. That API is reported as closed to new
  customers and shutting down on 2027-01-01, so it only helps with a key you already hold.
- With Docker's default user, everything in `./data` ends up owned by root.
