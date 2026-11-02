# BoulderMesh — prototype photogrammetry web app for boulders

Upload photos → reconstruct a mesh (COLMAP + OpenMVS) → view it in the browser.
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
| COLMAP + OpenMVS (see Docker section) | real photogrammetry instead of demo relief meshes |
| `GOOGLE_CSE_KEY` + `GOOGLE_CSE_CX` | Google Images via the Custom Search JSON API (100 free queries/day; existing keys only, see Known gaps) |
| `FLICKR_KEY` | geotagged Flickr photos around the climb's coordinates |

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

Records are `climbs.common.Climb` dataclasses (`to_dict()` → JSON): name, kind, grade(s),
fa, rating, lat/lng + `coord_source` (route | area | parent-area | manual), elevation,
area_path, boulder_name, description/location/protection, images[], videos[], cross_refs.

## Linking meshes to climbs

* Every climb gets a row in `climbs`; `mesh_links` is many-to-many (one boulder mesh ↔
  several problems). Meshes also carry their own lat/lng (inherited from the first linked
  climb, editable).
* Create a project with a climb selected, or harvest from a climb page, and the resulting
  mesh is auto-linked and geolocated.
* `GET /api/geojson` serves climbs + meshes for the map tab (Leaflet, OSM/OpenTopoMap).

## Harvest → rough photogrammetry

1. Open a climb → *Harvest photos* (*Harvest again* once it has one). Queries are built from
   the boulder name, area and problem name; geo sources use the coordinates. Everything lands
   in `data/projects/<id>/photos/`, with provenance in `sources.json`.
2. De-duplication, then the relevance filter (`climbs/relevance.py`): title cards and blank
   frames go, and photos from area-wide sources (web search, Commons, Flickr) must match
   known photos of the boulder from its climb page or videos, or photos already matched to
   them, or if there are none, the largest group of photos that match each other. This mostly
   saves time, as COLMAP ignores most junk anyway. Rejected photos stay in `rejected/`, with
   a reason in `relevance.json`, and the project card can put any back. Untick *filter
   off-topic photos* to skip it.
3. Diversity check (`climbs/diversity.py`): with OpenCV, ORB matching + fundamental-matrix
   RANSAC between every pair to build a view graph. Pairs explained by a 2-D similarity
   (crops, re-encodes, pan/zoom) are discounted because they carry no parallax. The largest
   connected component ≈ number of photos COLMAP could register → verdict
   **good** (≥12) / **marginal** (6–11) / **poor** (<6). Poor sets are not reconstructed
   automatically; add your own photos to the project and press Reconstruct to force it.
4. Reconstruction runs COLMAP in internet-collection mode (per-image intrinsics,
   affine-shape SIFT, domain-size pooling, guided matching), picks the largest sparse model
   and requires ≥6 registered images before densifying with OpenMVS. With torch, people are
   masked out at every stage, so they don't get painted onto the rock.

Expect *rough* results: web photos mix cameras, seasons, chalk and people; a famous
boulder with dozens of distinct angles (e.g. Camp 4 classics) is realistic, an obscure
problem with three topo shots is not. Beta videos that pan around the rock are a good
source, and web search adds the most: DuckDuckGo took Midnight Lightning from about 30
placed photos to about 65, and from about 190k faces to about 540k.

Downloaded images stay on your server for reconstruction only; most are copyrighted by
their photographers. `sources.json` lets you credit or remove any of them.

## Mesh a whole area

On the *Areas* tab, paste a Mountain Project area URL, such as
`https://www.mountainproject.com/area/105720495/joshua-tree-national-park`, and pick an order:

- **Most popular**, Mountain Project's own ranking.
- **Highest rated**, by average stars.
- **Most photos and videos**, judged from the pages of the next few popular climbs.

Its boulder problems are then meshed one at a time until you stop, or the list runs out. A
problem on a boulder that already has a mesh is linked to it rather than meshed again. A climb
without enough photos is skipped, and the run moves on.

*Stop now* drops the climb in progress and *Stop after this climb* finishes it. Either way, every
finished mesh stays, and *Resume* carries on from there. A server restart also stops the run, so
press *Resume* afterwards. It also stops by itself after 5 climbs in a row fail before finishing a
harvest, which usually means a network problem, or with under 50 GB of disk left
(`CAMPAIGN_MIN_FREE_GB`).

Jobs you start by hand go first, between climbs. Expect each climb to take most of an hour on CPU,
and about 170 MB of disk plus up to a few hundred MB of download cache. Mountain Project's export
stops at 1000 climbs: in a bigger area, these are its most popular 1000.

## Real photogrammetry

    docker compose up --build      # COLMAP + OpenMVS (+ OpenCV, ffmpeg, yt-dlp); first build ~10-40 min depending on cores

(`PORT=<n> docker compose up --build` if 8470 is taken), or natively on Ubuntu: `apt install colmap python3-opencv ffmpeg`, build OpenMVS (see
Dockerfile, including its one-line patch), `pip install yt-dlp`, then `python3 backend/server.py`.

**With an NVIDIA GPU** (and its container toolkit), photo matching takes 2-3 minutes rather than
12 for 100 photos. Give the container the GPU with `docker-compose.gpu.yml`, which this makes the
default on that machine:

    echo COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml >> .env

It's used if COLMAP has CUDA, and has 4 GB of its memory free when matching starts. Otherwise, or if
it runs out partway, matching uses the CPU, so a shared GPU is fine. The top of the page says which
it would use now, and each job's log says which it did. `USE_GPU=0` keeps it on the CPU.

A reconstruction produces a textured `mesh.glb` (texture PNGs alongside), or an untextured
`mesh.ply` if texturing fails, levelled assuming most photos were taken upright. On CPU,
OpenMVS's 11-photo sample set takes 3-4 minutes, and a 100-photo harvest about an hour.

If a browser can't load a mesh's texture, the viewer tries it smaller, then says why on the view.

## Layout

    backend/server.py              HTTP server, SQLite, uploads, job worker (reconstruct + harvest)
    backend/pipeline.py            COLMAP/OpenMVS driver (single-camera and internet-collection modes) + demo fallback
    backend/occlusion.py           person masks for reconstruction
    backend/progress.py            per-step job progress, behind the UI's progress bars
    backend/climbs/common.py       Climb/Media types, cached + rate-limited + robots-aware HTTP client
    backend/climbs/mountainproject.py, openbeta.py, kaya.py   adapters
    backend/climbs/harvest.py      photo sources, download, provenance
    backend/climbs/relevance.py    filter for photos substantially of the boulder
    backend/climbs/diversity.py    de-dup + view-graph diversity estimate
    backend/climbs/htmlmini.py     tiny DOM on html.parser (no bs4 needed)
    frontend/index.html            UI: Climbs / Projects / Meshes tabs, three.js viewer, Leaflet map
    cli.py                         command-line access to the same code
    data/                          created at runtime; DATA_DIR overrides

## API

    GET  /api/status
    GET  /api/projects · POST /api/projects {name, climb_id?} · GET|DELETE /api/projects/{id}
    POST /api/projects/{id}/photos (multipart "photos") · POST /api/projects/{id}/reconstruct
    GET  /api/projects/{id}/harvest                     sources.json + diversity.json
    POST /api/projects/{id}/restore {file}              put a photo the filter rejected back
    GET  /api/meshes · POST /api/meshes (multipart "files", +name, +lat/lng, +climb_id) · PATCH|DELETE /api/meshes/{id}
    POST /api/meshes/{id}/links {climb_id, note} · DELETE /api/meshes/{id}/links/{climb_id}
    GET  /api/climbs?q=&source= · GET|PATCH|DELETE /api/climbs/{id}
    POST /api/climbs/import {url, include_children}     MP / OpenBeta / Kaya URL
    POST /api/climbs/search {q, lat, lng, radius_m}     OpenBeta live search
    POST /api/climbs/manual {...} · POST /api/climbs/kaya-export (multipart csv/json)
    POST /api/climbs/{id}/harvest {sources:[climb,google,ddg,commons,flickr,youtube], reconstruct, filter}
    GET  /api/campaigns · POST /api/campaigns {area, order:popularity|rating|data, start, csv}
    POST /api/campaigns/{id}/start · POST /api/campaigns/{id}/stop {now} · DELETE /api/campaigns/{id}
    GET  /api/geojson · GET /meshes/{id}/{file} · GET /photos/{pid}/{file} · GET /photos/{pid}/rejected/{file}

## Development

[design.md](design.md) has the measurements behind the relevance filter and people masking.
`python3 -m venv venv && ./venv/bin/pip install pillow numpy opencv-python-headless torch torchvision open_clip_torch`
gets every optional check; the tests also pass without them.

    python3 tests/test_parsers.py     # offline: MP/OpenBeta/Kaya parsers on fixtures
    python3 tests/test_relevance.py   # offline: relevance filter on synthetic photos, progress tracking (CLIP downloads its model)
    python3 tests/test_pipeline.py    # offline: reconstruction steps with COLMAP/OpenMVS stubbed, progress parsing
    python3 tests/test_campaign.py    # offline: meshing an area, with harvest and reconstruction stubbed: order, stopping, restarts
    python3 tests/test_api.py         # spins up the server on a free port, runs the whole flow incl. a harvest job

## Known gaps

- The Mountain Project parser and OpenBeta queries were checked against the live site and
  API on 2026-10-30, and the test fixtures mirror that markup. MP changes markup
  occasionally — `parse_route()` / `parse_area()` are the functions to adjust; OpenBeta
  surfaces the API's own error text if a field is renamed.
- No auth, single in-process job queue — intranet prototype, not a public service.
- Google Images needs the paid-after-100/day Custom Search API; direct scraping of
  Google Images is not implemented on purpose. That API is reported as closed to new
  customers and shutting down on 2027-01-01, so it only helps with a key you already hold.
- With Docker's default user, everything in `./data` ends up owned by root.
