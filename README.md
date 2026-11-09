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
    python cli.py jobs [--server URL]                   # what restarting the running server would interrupt
    python cli.py offtopic-split [--limit N]            # sort photos rejected before CLIP into bouldering or not

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
2. The photos earlier harvests of the same boulder found are added, for any of its problems: what
   each kept, and what didn't match known photos, as a search for one problem often finds another's
   side of the rock. Their page photos and video frames count as known photos of it too.
3. De-duplication, then the relevance filter (`climbs/relevance.py`): title cards and blank
   frames go, then photos that don't look like bouldering at all (CLIP), and photos from
   area-wide sources (web search, Commons, Flickr) must match known photos of the boulder from
   its climbs' pages or videos, or photos already matched to them, or if there are none, the
   largest group of photos that match each other. This mostly saves time, as COLMAP ignores most
   junk anyway. Rejected photos stay in `rejected/`, with a reason in `relevance.json`, and the
   project card lists them by reason, those that look like bouldering first, to put any back.
   Untick *filter off-topic photos* to skip it. `python3 cli.py offtopic-split` sorts harvests
   filtered before the CLIP check the same way.
4. Diversity check (`climbs/diversity.py`): with OpenCV, ORB matching + fundamental-matrix
   RANSAC between every pair to build a view graph. Pairs explained by a 2-D similarity
   (crops, re-encodes, pan/zoom) are discounted because they carry no parallax. The largest
   connected component ≈ number of photos COLMAP could register → verdict
   **good** (≥12) / **marginal** (6–11) / **poor** (<6). Poor sets are not reconstructed
   automatically; add your own photos to the project and press Reconstruct to force it.
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

People are found in every photo before reconstruction, and their masks are kept in
`data/projects/<id>/masks/` (`<photo>.png`, black where people are), with the share of each photo
that is people in `masks/people.json`. Only photos without an up-to-date mask are looked at again.

Downloaded images stay on your server for reconstruction only; most are copyrighted by
their photographers. `sources.json` lets you credit or remove any of them.

## Mesh a whole area

On the *Areas* tab, paste a Mountain Project area URL, such as
`https://www.mountainproject.com/area/105720495/joshua-tree-national-park`, and pick an order:

- **Most popular**, Mountain Project's own ranking.
- **Highest rated**, by average stars.
- **Most photos and videos**, judged from the pages of the next few popular climbs.

Its boulder problems are then meshed one at a time until you stop, or the list runs out. A
problem on a boulder that already has a mesh is linked to it rather than meshed again, and so are
those skipped earlier once their boulder gets one. A climb without enough photos is skipped, and the
run moves on; the next on its boulder starts with the photos its harvest found.

*Try N skipped boulders with all their photos* tries boulders that every climb so far was skipped on
again, each from the photos all their harvests found together, without downloading anything.

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

## Beta: the holds climbers use

The *Beta* tab shows a mesh with its holds coloured as the Tension Board app does: green start holds,
blue hand holds, magenta foot-only holds and red finish holds (start and hand holds can be used as feet
too). *Find beta* looks at each video with frames placed in the mesh. It finds the climber's hands and
feet with a pose model, casts them onto the mesh, and keeps the points where one stayed still for at
least a second. The first holds the hands used are start holds, the last ones finish holds, and holds
only feet touched are foot holds. Each video has its own holds and tracks, which can be shown or
hidden, and a picture of its frame with the tracks and holds drawn on it. Expect a few minutes per
video on a CPU. Meshes built before Nov 3 didn't keep their cameras' lenses, and those before Nov 1
where their photos were placed, so they need rebuilding first.

*Mark holds* lets you set the holds yourself: clicking the rock adds a start hold, and clicking a hold
again makes it a hand hold, then a finish hold, then a foot hold, then removes it, as setting a climb in
the Tension Board app does. These are saved with the mesh, apart from those found in videos.

The tab lists meshes with the most holds marked by hand first, then those with the most found in videos.
*Only show meshes with beta* hides those with neither.

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
few minutes each, at low priority.

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

If a browser can't load a mesh's texture, the viewer tries it smaller, then says why on the view. Grey
on a mesh means no photo shows that side, including the backs of surfaces.

On a phone, the page shows either the controls or the view. Viewing a mesh fills the screen, and
*Controls* goes back. Nothing is shown over the mesh there, so texture problems, stray-piece cleanup
and the iPhone gravity comparison are only shown on wider screens (over 860 px).

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

**Restarting.** A restart only pauses areas' climbs, which carry on afterwards. Jobs started outside an
area (a harvest or reconstruction started by hand, a retry, *Find beta*, recolouring from the admin page)
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
    echo EDIT_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))") >> .env
    docker compose -f docker-compose.yml -f docker-compose.public.yml up -d --build

Ports 80 and 443 must be open to the world. To run it under systemd as above, put
`COMPOSE_FILE=docker-compose.yml:docker-compose.public.yml` in `.env` too (with `docker-compose.gpu.yml` between them on a
machine with a GPU) and install the unit.

**Changing things.** With `EDIT_TOKEN` set, the page has an *Edit…* button in its header: enter the token there (it stays in
that browser) and the controls appear; without it, the site shows only what's there, and every change sent to it is refused
before its body is read, and logged with the sender's address. Scripts send `Authorization: Bearer <token>` with each POST,
PATCH or DELETE. Viewers need nothing. Leave `EDIT_TOKEN` out of `.env`, or set `READ_ONLY=1`, for a site no one can change
from a browser; the comments in `deploy/Caddyfile` show how to refuse changes at the edge as well. Only one token exists, so
the site doesn't know who changed what: that's for accounts, later.

**The admin page** stays on this machine's loopback address (`ADMIN_BIND` is refused if it isn't private). Reach it through
an SSH tunnel, `ssh -L 36184:127.0.0.1:36184 you@boulders.example.com`, at `http://localhost:36184`, and set
`ADMIN_PASSWORD` in `.env` anyway. Before the site is public, set what it may show there: most harvested photos are
copyrighted ([license_considerations.md](license_considerations.md)), and *Source photos and video frames: free to copy, with
links to the rest* keeps their copies here from being served.

**What else the public setup does.** The container has no capabilities, can't gain privileges, and runs as uid 1000
(`BM_USER` in `.env` changes it; `./data` must be writable by that user). Pages come with a Content-Security-Policy that
allows only the site's own scripts, by their hashes, and the two CDNs; images and map tiles from anywhere; and no framing.
If you change `frontend/index.html`, the hashes follow, but a new inline `onclick` or another CDN needs the policy changed
in `backend/server.py` (`CSP=0` in `.env` turns it off, and `CSP=<policy>` replaces it). Files under `data/` are served only
as meshes, textures, JSON or bytes, so nothing uploaded there can run as a page of the site, and uploaded meshes keep only
mesh, texture and material files. Meshes, photos and static files are sent with `Last-Modified`, and may be cached for an
hour (`FILE_CACHE_SECONDS`). Harvests fetch only `http(s)` URLs, and never from private, loopback or link-local addresses,
such as a cloud machine's metadata service (`HTTP_ALLOW_PRIVATE=1` lifts that, for tests).

**What it doesn't do.** There's no rate limiting, and `GET /api/meshes` reads every mesh's records for each request, so a
busy site wants a cache in front: Cloudflare's free plan, with the name proxied and SSL mode *Full (strict)*, caches the
meshes and textures, limits requests, and hides the machine's address, and Caddy still gets its certificate through it. Each
project's log is public, as the Projects tab shows it, so it reveals where photos came from and the names of compute
machines, not credentials. Back up `data/` yourself: a mesh is an hour of compute. Compute credentials are kept as before
(encrypted on the admin page, or in `.env`), and cloud machines accept SSH only from this server's address.

## The admin page

A separate page, on port 36184, sets what the main site shows and where its reconstructions run. It
listens only on this machine's loopback address unless `.env` says otherwise, and never on every address:

    ADMIN_BIND=10.0.0.5                         # this machine's LAN address, to reach it from the LAN
    BOULDERMESH_SECRETS_KEY=...                 # to save credentials on it (see below)
    ADMIN_PASSWORD=...                          # optional: then it asks for user "admin" and this password

Then `docker compose up -d` and open `http://10.0.0.5:36184`. Anyone who can reach it can change settings,
so keep it off public addresses (it refuses to start on one). It refuses changes sent by other sites'
pages, and requests naming a host that isn't an address or a LAN name (`ADMIN_HOSTNAMES` adds names).

**What the site shows.** "Free to copy" means public domain, CC0, or a Creative Commons licence without
No Derivatives, or your own uploads ([license_considerations.md](license_considerations.md) has why).
- *Mesh textures*: all of them; or only those made from photos free to copy, with a version of each
  other mesh coloured from only those photos where *Make textures from only the photos free to copy*
  has made one (else plain grey); or none. Hidden textures aren't served at all, and meshes come without them.
- *Source photos and video frames*: all; or those free to copy, with only links to the rest, whose copies
  here aren't served; or only those free to copy.

The coloured versions are made per vertex from the photos free to copy, so they're less sharp than a
texture, and grey where none of those photos sees the rock. Meshes built before Nov 3 need rebuilding
first, as they didn't keep their cameras' lenses.

**Credentials** are saved encrypted with `BOULDERMESH_SECRETS_KEY`, which stays in `.env`, outside
`data/`. Make one with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
They're never shown again. Without that key, give them as environment variables instead
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` in `.env`).

## Running reconstructions elsewhere

By default this server does everything. *Reconstructions at once on this server* can be raised, which
runs climbs side by side (two areas then share the workers), though most steps already use every core.
On the admin page, *Run more on* adds up to *At most this many other machines at once* (2 by default):

- **Machines reached over SSH**: any you keep running, with Docker (and NVIDIA's container toolkit for
  the GPU), listed as `user@host[:port]`, all reached with one private key saved as a credential.
- **AWS EC2**: instances started when there's a climb to mesh, a spot `g4dn.2xlarge` (T4 GPU, 8 cores)
  by default, from AWS's Deep Learning GPU image. Each one is reached over SSH from this server's address
  only, and ended after *End an idle cloud machine after* minutes without work. One also ends itself
  after a while with no job running, in case this server has gone, and any left running from before a
  restart are ended. Its credentials need EC2 (run, describe and terminate instances, security groups)
  and SSM's GetParameter. *Test the connection* checks them without starting anything.

Machines run the same Docker image as this server, which they pull from a registry:

    docker tag bouldermesh-bouldermesh ghcr.io/you/bouldermesh:latest && docker push ghcr.io/you/bouldermesh:latest

and set that as *Docker image the machines run*. A private image on ECR works with an *Instance profile*
that may pull it. Each climb's photos and people masks go to the machine, and the mesh, its cameras and
the masks come back. Harvesting stays here. A climb whose machine can't be had or is lost waits, rather
than failing, and that worker tries again in 5 minutes. design.md has what offloading costs and saves.
The AWS side has been tested against stand-ins for AWS, not against AWS itself.

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
    backend/beta.py                the holds climbers use, from their hands and feet in videos
    backend/scale.py               how big a mesh is, from the people in its photos and Mountain Project's height
    backend/admin.py               the admin page's server; frontend/admin.html is the page
    backend/settings.py            the admin page's settings, and credentials kept encrypted
    backend/policy.py              which photos are free to copy, and what the site may show
    backend/retexture.py           colouring a mesh from only the photos free to copy
    backend/compute.py             running reconstructions on other machines, and starting and ending them
    backend/remote_job.py          what such a machine runs
    backend/climbs/relevance.py    filter for photos substantially of the boulder
    backend/climbs/diversity.py    de-dup + view-graph diversity estimate
    backend/climbs/htmlmini.py     tiny DOM on html.parser (no bs4 needed)
    frontend/index.html            UI: Climbs / Projects / Meshes tabs, three.js viewer, Leaflet map
    cli.py                         command-line access to the same code
    deploy/                        systemd service, and its installer
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
    GET  /api/campaigns · POST /api/campaigns {area, order:popularity|rating|data, start, csv}
    POST /api/campaigns/{id}/start · POST /api/campaigns/{id}/stop {now} · DELETE /api/campaigns/{id}
    POST /api/campaigns/{id}/retry                      its failed climbs, ahead of the rest
    POST /api/campaigns/{id}/harvest-again {videos_only, then_rebuild} · POST /api/campaigns/{id}/rebuild {changed_only}
    POST /api/campaigns/{id}/pool                       its boulders every climb was skipped on, again from all their photos
    GET  /api/videos                                    every video, with the climber's size if stated
    POST /api/meshes/{id}/make-current                  go back to an older version of a rebuilt mesh
    GET|POST /api/meshes/{id}/beta                      the holds found in its videos, and those marked; POST finds them
    POST /api/meshes/{id}/holds {holds: [{p, role}]}    the holds marked by hand, roles start, hand, finish or foot
    POST /api/meshes/{id}/scale                         find its size (scale.json: metres per unit, and each estimate)
    POST /api/meshes/scale-all {again}                  size every reconstructed mesh not yet sized, or all of them
    GET|POST|DELETE /api/meshes/{id}/cleanup · PATCH /api/meshes/{id}/cleanup {removed} · GET /meshes/{id}/mesh.glb?cleaned=1
    GET  /api/meshes/{id}/sources                       each photo in the mesh: source, URL, page, credit, licence
    On the admin page's port: GET /api/admin/state · POST /api/admin/settings · POST /api/admin/secrets
    POST /api/admin/actions/{test,end-nodes,retexture}
    GET  /api/geojson · GET /meshes/{id}/{file} · GET /photos/{pid}/{file} · GET /photos/{pid}/rejected/{file}

## Development

[design.md](design.md) has the measurements behind the relevance filter and people masking.
`python3 -m venv venv && ./venv/bin/pip install pillow numpy opencv-python-headless torch torchvision open_clip_torch`
gets every optional check; the tests also pass without them.

    python3 tests/test_parsers.py     # offline: MP/OpenBeta/Kaya parsers on fixtures
    python3 tests/test_relevance.py   # offline: relevance filter on synthetic photos, progress tracking (CLIP downloads its model)
    python3 tests/test_pipeline.py    # offline: reconstruction steps with COLMAP/OpenMVS stubbed, progress parsing
    python3 tests/test_campaign.py    # offline: meshing an area, with harvest and reconstruction stubbed: order, stopping, restarts, retries
    python3 tests/test_diagnostics.py # offline: what's recorded when real processes fail or get killed
    python3 tests/test_meshclean.py   # offline: finding and hiding stray pieces on small GLBs, the cleaned download
    python3 tests/test_orientation.py # offline: iPhone gravity readings, and comparing them with a model's down
    python3 tests/test_scale.py       # offline: sizing people, objects and the rock on scenes built with known sizes
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
