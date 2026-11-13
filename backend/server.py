"""BoulderMesh server - standard library only (Python 3.10+).

  python backend/server.py            # http://localhost:8470

Data layout (DATA_DIR, default ./data):
  bouldermesh.sqlite
  projects/<project_id>/photos/*.jpg    uploaded or harvested photos (ONLY images in here)
  projects/<project_id>/sources.json    provenance of harvested photos
  projects/<project_id>/diversity.json  photo-set diversity report
  projects/<project_id>/rejected/       duplicates / too-small images set aside by the diversity check
  projects/<project_id>/work/           COLMAP/OpenMVS scratch
  meshes/<mesh_id>/<file>               reconstructed or uploaded meshes
  cache/http/                           cached web responses (climb pages, API calls, searches), pruned for room
  cache/http-kept/                      photos downloaded, kept for good (common.Http.get's keep)
"""
from __future__ import annotations

import base64
import csv
import email.utils
import gzip
import html
import hashlib
import hmac
import io
import json
import math
import mimetypes
import os
import queue
import re
import resource
import secrets
import shutil
import signal
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import admin as admin_mod  # noqa: E402
import beta as beta_mod  # noqa: E402
import scale as scale_mod  # noqa: E402
import compute  # noqa: E402
import diagnostics  # noqa: E402
import glbpack  # noqa: E402
import heading as heading_mod  # noqa: E402
import skyline as skyline_mod  # noqa: E402
import terrain as terrain_mod  # noqa: E402
import meshclean  # noqa: E402
import dbconn  # noqa: E402
import feedback as feedback_mod  # noqa: E402
import links as links_mod  # noqa: E402
import thumbs as thumbs_mod  # noqa: E402
import meshinputs  # noqa: E402
import meshmetrics  # noqa: E402
import meshopts  # noqa: E402
import offload as offload_mod  # noqa: E402
import orientation  # noqa: E402
import policy  # noqa: E402
import provenance  # noqa: E402
import retexture  # noqa: E402
import settings as settings_mod  # noqa: E402
import pipeline  # noqa: E402
import proxy_ranges  # noqa: E402
import pullwork  # noqa: E402
import splat as splat_mod  # noqa: E402
import timings as timings_mod  # noqa: E402
import steptimes as steptimes_mod  # noqa: E402
import viewpoints  # noqa: E402
import notify as notify_mod  # noqa: E402
import volunteers as volunteers_mod  # noqa: E402
import climbs as climbs_mod  # noqa: E402
from climbs import diversity as diversity_mod  # noqa: E402
from climbs import harvest as harvest_mod  # noqa: E402
from climbs import relevance as relevance_mod  # noqa: E402
from climbs import youtube as youtube_mod  # noqa: E402
from progress import Tracker  # noqa: E402
from climbs import kaya as kaya_mod  # noqa: E402
from climbs import mountainproject as mp_mod  # noqa: E402
from climbs.common import Cancelled, Climb, JobControl, jobctl, use_control  # noqa: E402
from climbs.common import _shared_control as shared_control  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()
PROJECTS = DATA / "projects"
MESHES = DATA / "meshes"
USE_GPU = os.environ.get("USE_GPU", "1") != "0"   # used if it's there and has room
SHRINK_SIDES = (2048, 4096, 8192)                # sizes a texture can be asked for, as a JPEG: the viewer shows 2048 first
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "512")) * 1024 * 1024
PHOTO_EXT = {".jpg", ".jpeg", ".png"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}   # uploaded to a project, turned into photos
VIDEO_FRAMES = 200      # frames kept from each such video: its sharpest, spread over its length
# Files sent in parts, kept here until the request that uses them: Cloudflare's free plan refuses any request over 100 MB
UPLOADS = DATA / "uploads"
PART_MAX = 64 * 2**20   # the largest part
UPLOAD_HOURS = 24       # how long an upload no request has used is kept
MESH_EXT = {".ply", ".obj", ".glb", ".gltf", ".stl"}
SAFE = re.compile(r"[^A-Za-z0-9._-]+")
# exposure: by default anyone who can reach the port can change anything, as on a LAN. EDIT_TOKEN makes every change need
# "Authorization: Bearer <token>"; READ_ONLY=1 refuses them all, and READ_ONLY=auto does unless there's a token (the public
# Compose setup's default). The README's "Putting it on the public internet" has the rest.
EDIT_TOKEN = os.environ.get("EDIT_TOKEN") or None
READ_ONLY = os.environ.get("READ_ONLY", "0") == "1" or (os.environ.get("READ_ONLY") == "auto" and not EDIT_TOKEN)
FILE_CACHE = int(os.environ.get("FILE_CACHE_SECONDS", "3600"))   # how long browsers and proxies may keep meshes, photos and static files
# The viewer adds a mesh's version (mesh_version) to the addresses of its files, which are then kept a year, as what's at
# them never changes; but textures and splats, which carry its photos, a day at most, so that one the site stops showing
# (as its photos' licences come to light) is gone from caches by then. Textures are kept that long without it too.
MESH_CACHE = int(os.environ.get("MESH_CACHE_SECONDS", str(365 * 86400)))
TEXTURE_CACHE = int(os.environ.get("TEXTURE_CACHE_SECONDS", "86400"))
# the header in which a proxy in front names the sender, for the log (X-Real-IP behind deploy/Caddyfile); only for a port
# nothing but that proxy can reach, as anyone else could write it
CLIENT_IP_HEADER = os.environ.get("CLIENT_IP_HEADER") or None
# which build this is, and the name it's pushed under; and the image remote machines run unless the admin page names
# another, the same commit's GPU build (or this one, from before there was a separate one): given by deploy/build.sh
# (the Dockerfile's last lines), and empty for any other build
VERSION = os.environ.get("BOULDERMESH_COMMIT") or None
OWN_IMAGE = os.environ.get("BOULDERMESH_IMAGE") or None
MACHINES_IMAGE = os.environ.get("BOULDERMESH_WORKER_IMAGE") or OWN_IMAGE
WORKER_CPU_IMAGE = os.environ.get("BOULDERMESH_WORKER_CPU_IMAGE") or None   # machines without a GPU's, x86-64 and ARM64
# the Caddyfile in front, whose trusted proxies are compared with Cloudflare's published ranges each day (proxy_ranges.py)
RANGES = proxy_ranges.Watch(Path(os.environ["CADDYFILE"])) if os.environ.get("CADDYFILE") else None
# volunteers fetching YouTube videos (volunteers.py), served on VOLUNTEER_PORT if it's set, for a proxy to pass on
# /volunteer/... to; VOLUNTEER_URL is the address they reach it at, for the command the admin page gives them
VOLUNTEER_PORT = int(os.environ.get("VOLUNTEER_PORT") or 0)
VOLUNTEER_URL = os.environ.get("VOLUNTEER_URL") or (f"https://{os.environ['SITE_HOST']}" if os.environ.get("SITE_HOST") else None)
# machines that connect here for work (pullwork.py), served on WORKER_PORT if it's set, for a proxy to pass on /worker/... to
WORKER_PORT = int(os.environ.get("WORKER_PORT") or 0)
MESH_EXTRA_EXT = {".png", ".jpg", ".jpeg", ".bin", ".mtl"}       # what may be uploaded beside a mesh: its textures and material
# how files under data/ are served; anything else is sent as bytes to save, so that nothing put there can run as a page of this site
SERVED_TYPES = {".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".json": "application/json", ".bin": "application/octet-stream", ".ply": "application/octet-stream",
                ".obj": "application/octet-stream", ".stl": "application/octet-stream", ".mtl": "application/octet-stream",
                ".splat": "application/octet-stream"}
HARVEST_SOURCES = harvest_mod.SOURCES
CAMPAIGN_ORDERS = ("popularity", "rating", "data")
CAMPAIGN_POLL = 2.0     # seconds the worker waits for a queued job before moving a campaign on
DATA_WINDOW = 8         # "data" order: how many waiting climbs' pages to have read before picking one
VIDEO_WORTH = 20        # "data" order: roughly how many usable photos a beta video gives
OUTAGE_LIMIT = 5        # stop a campaign after this many climbs in a row fail before finishing a harvest
CAMPAIGN_MIN_FREE = float(os.environ.get("CAMPAIGN_MIN_FREE_GB", "50")) * 2**30   # stop rather than fill a shared disk
# the most DATA_DIR may hold is the admin page's storage.cap_gb (DATA_CAP_GB starts it; 0: no limit): over it, volunteers
# are given nothing and areas take no new climb, until room is made (the admin page shows what takes it, and can delete
# photos the filter rejected)
USAGE_EVERY = 600       # seconds between measurings of DATA_DIR
# copies of the databases made safely while they're in use, for deploy/backup.sh to copy (0: none)
SNAPSHOTS = DATA / "snapshots"
SNAPSHOT_EVERY = float(os.environ.get("SNAPSHOT_MINUTES", "60")) * 60
# lowest areas named like these hold several rocks, so one mesh can't stand in for all their climbs; others are read
# (their description too) once an area reaches their climbs (boulder_kind())
GROUP_AREA = mp_mod.GROUP_NAME

# ----------------------------------------------------------------- database
_db_lock = threading.Lock()
_settings: settings_mod.Settings | None = None


def settings() -> settings_mod.Settings:
    """The admin page's settings (settings.py), kept in this database."""
    global _settings
    if _settings is None:
        DATA.mkdir(parents=True, exist_ok=True)
        _settings = settings_mod.Settings(DATA / "bouldermesh.sqlite", DATA)
    return _settings


def db() -> sqlite3.Connection:
    con = dbconn.connect(DATA / "bouldermesh.sqlite", check_same_thread=False)    # closed as its with block ends
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    for d in (PROJECTS, MESHES):
        d.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS projects(
          id TEXT PRIMARY KEY, name TEXT, created REAL,
          status TEXT DEFAULT 'new',   -- new | queued | harvesting | harvested | running | done | failed
          log TEXT DEFAULT '', mesh_id TEXT, demo INTEGER DEFAULT 0,
          kind TEXT DEFAULT 'user',    -- user | harvest | boulder (every harvest of a boulder's climbs, pooled)
          climb_id TEXT, harvest TEXT);
        CREATE TABLE IF NOT EXISTS meshes(
          id TEXT PRIMARY KEY, name TEXT, filename TEXT, source TEXT,  -- source: upload | project:<id>
          created REAL, size INTEGER, demo INTEGER DEFAULT 0, lat REAL, lng REAL);
        CREATE TABLE IF NOT EXISTS climbs(
          id TEXT PRIMARY KEY, source TEXT, source_id TEXT, url TEXT, name TEXT, kind TEXT, grade TEXT,
          lat REAL, lng REAL, coord_source TEXT, boulder_name TEXT, area_path TEXT, data TEXT, created REAL,
          UNIQUE(source, source_id));
        CREATE TABLE IF NOT EXISTS mesh_links(
          mesh_id TEXT, climb_id TEXT, note TEXT, created REAL, PRIMARY KEY(mesh_id, climb_id));
        -- what each lowest area of climbs is, as its page says (boulder_kind()): one boulder ("boulder"), several
        -- ("group"), or it doesn't say ("unknown"); key as boulder_key() has it
        CREATE TABLE IF NOT EXISTS boulders(
          key TEXT PRIMARY KEY, name TEXT, url TEXT, kind TEXT, why TEXT, routes INTEGER, checked REAL);
        -- whether two climbs' photos are of the same rock (link_climbs()): how many pairs of them match ("photos"), or
        -- how many of each landed in one of COLMAP's models together ("colmap"); a < b
        CREATE TABLE IF NOT EXISTS climb_links(
          a TEXT, b TEXT, pairs INTEGER, how TEXT, checked REAL, PRIMARY KEY(a, b));
        -- climbs of a lowest area, not one boulder, whose photos are of one rock: meshed together (pool_clusters());
        -- key as a boulder's, with "#<a climb's id>" after its path
        CREATE TABLE IF NOT EXISTS pools(
          key TEXT PRIMARY KEY, leaf TEXT, members TEXT, created REAL, updated REAL);
        -- visitors' ratings of meshes (1 bad, 2 ok, 3 good) and comments: one each per browser, with the address it last
        -- came from (rate_mesh())
        CREATE TABLE IF NOT EXISTS mesh_ratings(
          mesh_id TEXT, browser TEXT, address TEXT, rating INTEGER, comment TEXT, created REAL, updated REAL,
          PRIMARY KEY(mesh_id, browser));
        CREATE TABLE IF NOT EXISTS campaigns(
          id TEXT PRIMARY KEY, name TEXT, area_id TEXT, ord TEXT, created REAL, note TEXT,
          status TEXT,                 -- running | stopping | stopped | done
          only_retries INTEGER DEFAULT 0);   -- run only its retries, and then stop
        CREATE TABLE IF NOT EXISTS campaign_items(
          campaign_id TEXT, source_id TEXT, rank INTEGER, name TEXT, grade TEXT, stars REAL, url TEXT,
          boulder TEXT, data TEXT, media INTEGER, climb_id TEXT, project_id TEXT, mesh_id TEXT,
          note TEXT, updated REAL, retry INTEGER DEFAULT 0,
          status TEXT DEFAULT 'pending',   -- pending | running | waiting (for volunteers) | done | skipped | failed
          PRIMARY KEY(campaign_id, source_id));
        CREATE INDEX IF NOT EXISTS campaign_items_boulder ON campaign_items(boulder);
        -- a climb's meshes, counted for each climb listed (climb_row(), MESH_COUNT)
        CREATE INDEX IF NOT EXISTS mesh_links_climb ON mesh_links(climb_id);
        -- videos of climbs, found on their pages or by searching, and what their uploaders say about the
        -- climber's size (youtube.stats), for scaling meshes later
        CREATE TABLE IF NOT EXISTS videos(
          url TEXT PRIMARY KEY, title TEXT, uploader TEXT, channel_id TEXT, channel_url TEXT, duration REAL,
          upload_date TEXT, license TEXT, description TEXT, fetched REAL,
          height_cm REAL, wingspan_cm REAL, ape_index_cm REAL, first_person INTEGER, quotes TEXT);
        CREATE TABLE IF NOT EXISTS climb_videos(
          climb_id TEXT, url TEXT, found_by TEXT, frames INTEGER, created REAL, PRIMARY KEY(climb_id, url));
        CREATE TABLE IF NOT EXISTS channels(
          channel_url TEXT PRIMARY KEY, channel_id TEXT, name TEXT, description TEXT, fetched REAL,
          height_cm REAL, wingspan_cm REAL, ape_index_cm REAL, first_person INTEGER, quotes TEXT);
        -- what the admin page decided of a mesh and a newer one under the same rules over the same climbs, that fit
        -- neither kind of old (old_by_data()): "old" (it's old by data after all) or "keep" (both stay current)
        CREATE TABLE IF NOT EXISTS old_decisions(
          mesh_id TEXT, newer TEXT, verdict TEXT, decided REAL, PRIMARY KEY(mesh_id, newer));
        """)
        con.execute(steptimes_mod.SCHEMA)   # how long each step of each run took, for estimates (steptimes.py)
        offload_mod.migrate(con)            # what's been offloaded to the backup, or marked to be, and its files (offload.py)
        # migrations for databases created by v0.1
        cols = {r[1] for r in con.execute("PRAGMA table_info(projects)")}
        # job: the last job queued for it outside an area, to run again, as it was asked for, if a restart stops it;
        # boulder: for a boulder's project, the area path of its climbs (as climbs.area_path has it)
        # job_at: when that job was queued, to queue again those a restart found waiting in the order they were
        for c, t in (("kind", "TEXT DEFAULT 'user'"), ("climb_id", "TEXT"), ("harvest", "TEXT"), ("progress", "TEXT"), ("job", "TEXT"),
                     ("boulder", "TEXT"), ("job_at", "REAL")):
            if c not in cols:
                con.execute(f"ALTER TABLE projects ADD COLUMN {c} {t}")
        cols = {r[1] for r in con.execute("PRAGMA table_info(meshes)")}
        for c in ("lat", "lng"):
            if c not in cols:
                con.execute(f"ALTER TABLE meshes ADD COLUMN {c} REAL")
        if "boulder" not in cols:   # a mesh of a whole boulder (a boulder project's): the area path of its climbs
            con.execute("ALTER TABLE meshes ADD COLUMN boulder TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS meshes_boulder ON meshes(boulder)")    # its climbs' count of them (MESH_COUNT)
        cols = {r[1] for r in con.execute("PRAGMA table_info(campaigns)")}
        if "only_retries" not in cols:
            con.execute("ALTER TABLE campaigns ADD COLUMN only_retries INTEGER DEFAULT 0")
        if "without_youtube" not in cols:   # mesh climbs whose YouTube search hasn't been done (none to do it)
            con.execute("ALTER TABLE campaigns ADD COLUMN without_youtube INTEGER DEFAULT 0")
        if "priority" not in cols:          # areas are taken in this order, lowest first: as entered (next_priority())
            con.execute("ALTER TABLE campaigns ADD COLUMN priority REAL")
            con.execute("UPDATE campaigns SET priority=created")
        if "max_meshes" not in cols:        # stop once so many meshes are made, in its order (NULL: mesh all of it)
            con.execute("ALTER TABLE campaigns ADD COLUMN max_meshes INTEGER")
        if "count_failures" not in cols:    # with max_meshes: its climbs and boulders that failed count toward it too
            con.execute("ALTER TABLE campaigns ADD COLUMN count_failures INTEGER DEFAULT 0")
        if "same_photos_once" not in cols:  # a boulder isn't meshed again from the photos its current mesh was made from
            con.execute("ALTER TABLE campaigns ADD COLUMN same_photos_once INTEGER DEFAULT 1")
        if "keep_better" not in cols:       # a rebuild from the same photos that places fewer leaves the mesh before current
            con.execute("ALTER TABLE campaigns ADD COLUMN keep_better INTEGER DEFAULT 1")
        if "mapper_below" not in cols:      # COLMAP's mapper placing under this % of the photos tries more seeds (meshopts.py)
            con.execute(f"ALTER TABLE campaigns ADD COLUMN mapper_below INTEGER DEFAULT {round(meshopts.MAPPER['retry_below'] * 100)}")
        if "mapper_more" not in cols:       # how many more
            con.execute(f"ALTER TABLE campaigns ADD COLUMN mapper_more INTEGER DEFAULT {meshopts.MAPPER['more']}")
        if con.execute("PRAGMA user_version").fetchone()[0] < 1:   # once: areas on the old defaults take the new ones
            con.execute("UPDATE campaigns SET mapper_more=? WHERE mapper_below=75 AND mapper_more=1", (meshopts.MAPPER["more"],))
            con.execute("PRAGMA user_version=1")
        cols = {r[1] for r in con.execute("PRAGMA table_info(campaign_items)")}
        # redo: what it's queued to do again (REDO_NAMES); was: its status before; changed: its photos have
        # changed since it was last meshed or checked; redo_started: a rebuild that began, to carry on with
        # waiting_since: when it first waited for volunteers' YouTube searches and videos (status 'waiting')
        # own_tried: when it was meshed on its own after a pooled mesh hardly showed it (before keep_rest()), once;
        # pool: for redo "cluster", the pool it meshes (pools.key); paused: when someone paused it (pause_item()), so that
        # it isn't taken until they resume it; rerun_rules: the MESH_RULES under which it was queued again as its climb's
        # meshes were old (queue_old()), once for each; old_rerun: that's still to go or in progress; blocked: why it
        # can't go on (data lost from the backup) until it's approved on the admin page (lost_decide())
        for c, t in (("retry", "INTEGER DEFAULT 0"), ("redo", "TEXT"), ("was", "TEXT"), ("changed", "INTEGER DEFAULT 0"),
                     ("redo_started", "INTEGER DEFAULT 0"), ("waiting_since", "REAL"), ("own_tried", "REAL"), ("pool", "TEXT"),
                     ("paused", "REAL"), ("rerun_rules", "INTEGER"), ("old_rerun", "INTEGER DEFAULT 0"), ("blocked", "TEXT")):
            if c not in cols:
                con.execute(f"ALTER TABLE campaign_items ADD COLUMN {c} {t}")
        # climbs queued to be harvested again from scratch for what volunteers fetched, not started yet: added to instead,
        # since Nov 12, 2026 (run_harvest()'s add)
        con.execute("UPDATE campaign_items SET redo=CASE redo WHEN 'harvest' THEN 'more' ELSE 'more+rebuild' END "
                    "WHERE status='pending' AND redo IN ('harvest', 'harvest+rebuild') AND COALESCE(redo_started, 0)=0 "
                    "AND (note LIKE 'volunteers have fetched%' OR note LIKE 'queued to harvest again with the videos volunteers%' "
                    "OR note LIKE 'waited % h for volunteers%')")
        # and so other projects queued to harvest again for them (reharvest_with_videos(), before its add), as their logs say
        for r in con.execute("SELECT id, job FROM projects WHERE status='queued' AND job LIKE '%\"harvest\"%'").fetchall():
            try:
                job = json.loads(r["job"])
                said = (PROJECTS / r["id"] / "job.log").read_text(errors="replace")[-4000:]
            except (OSError, ValueError):
                continue
            if (job.get("type") == "harvest" and job.get("again") and not job.get("add")
                    and "volunteers fetched the videos this harvest couldn't have: harvesting it again" in said):
                con.execute("UPDATE projects SET job=? WHERE id=?", (json.dumps({**job, "add": True}), r["id"]))
        if "replaced_by" not in {r[1] for r in con.execute("PRAGMA table_info(meshes)")}:
            con.execute("ALTER TABLE meshes ADD COLUMN replaced_by TEXT")   # a rebuild of the same photos' project
        if "rules" not in {r[1] for r in con.execute("PRAGMA table_info(meshes)")}:
            con.execute("ALTER TABLE meshes ADD COLUMN rules INTEGER")      # the MESH_RULES it was made under
        if "old_data" not in {r[1] for r in con.execute("PRAGMA table_info(meshes)")}:    # a newer mesh under the same rules, from
            con.execute("ALTER TABLE meshes ADD COLUMN old_data TEXT")                    # more, covers its climbs (old_by_data())
        for n, since in sorted(MESH_RULES_SINCE.items()):     # those made before it was kept: by when they were made
            con.execute("UPDATE meshes SET rules=? WHERE rules IS NULL AND source LIKE 'project:%' AND created>=?", (n, since))
        # an area's climbs' boulders were their area paths joined by "/", which a name with one in it ("Drifter/Smoking
        # Boulder") broke when split again: JSON, as boulder_of() has them, since 2026-11-11
        for r in con.execute("SELECT campaign_id, source_id, data FROM campaign_items WHERE boulder IS NOT NULL AND boulder NOT LIKE '[%'").fetchall():
            try:
                key = boulder_key(json.loads(r["data"]).get("area_path"))
            except (TypeError, ValueError):
                key = None
            con.execute("UPDATE campaign_items SET boulder=? WHERE campaign_id=? AND source_id=?", (key, r["campaign_id"], r["source_id"]))
        # meshes were named as their projects, "(harvest)", "(boulder)" or "(pooled)" after: without, since 2026-11-11
        for suffix in (" (harvest)", " (boulder)", " (pooled)"):
            con.execute("UPDATE meshes SET name=substr(name, 1, length(name) - ?) WHERE name LIKE ?", (len(suffix), "%" + suffix))
        # areas named as holding several boulders were left without one; now their lowest area, for pooling by photos
        for r in con.execute("SELECT campaign_id, source_id, data FROM campaign_items WHERE boulder IS NULL AND data IS NOT NULL").fetchall():
            try:
                key = boulder_key(json.loads(r["data"]).get("area_path"))
            except (TypeError, ValueError, AttributeError):
                key = None
            if key:
                con.execute("UPDATE campaign_items SET boulder=? WHERE campaign_id=? AND source_id=?", (key, r["campaign_id"], r["source_id"]))
        # and so those harvested for a boulder whose mesh was made were marked failed with it: done with that mesh
        for r in con.execute("SELECT campaign_id, source_id, boulder, note FROM campaign_items WHERE status IN ('failed','skipped') "
                             "AND note LIKE 'its boulder meshed from %' AND boulder IS NOT NULL").fetchall():
            p = con.execute("SELECT status, mesh_id FROM projects WHERE kind='boulder' AND boulder=? ORDER BY created DESC LIMIT 1",
                            (r["boulder"],)).fetchone()
            if p and p["status"] == "done" and p["mesh_id"]:
                con.execute("UPDATE campaign_items SET status='done', mesh_id=?, note=? WHERE campaign_id=? AND source_id=?",
                            (p["mesh_id"], "meshed with its boulder: " + r["note"].removeprefix("its boulder meshed from "),
                             r["campaign_id"], r["source_id"]))
        # A restart only pauses an area: its climb carries on from the last step it finished, and the
        # area carries on by itself unless someone stopped it. Other jobs are lost, as their queue is: one that had
        # started is marked interrupted, not failed (Retry carries on with it); one still waiting stays queued, and is
        # queued again as the server starts (requeue_waiting()), as it was asked for. One without its job recorded
        # (from before jobs were) is interrupted, for Retry to run.
        con.execute("UPDATE projects SET status='stopped' WHERE id IN (SELECT project_id FROM campaign_items "
                    "WHERE status='running' OR (status='pending' AND retry=1 AND redo IS NULL))")
        # one meshed again as its meshes were old goes after the rest of its area's list, but once started, next
        con.execute("UPDATE campaign_items SET retry=1 WHERE status='running' AND old_rerun=1")
        con.execute("UPDATE campaign_items SET status='pending', note='interrupted by a restart: carrying on from where it got to' WHERE status='running'")
        con.execute("UPDATE campaigns SET status='stopped', note=NULL WHERE status='stopping'")
        cut = con.execute("SELECT id, status, job FROM projects WHERE status IN ('queued','running','harvesting') "
                          "AND NOT (status='queued' AND job IS NOT NULL)").fetchall()
        waiting = con.execute("SELECT id FROM projects WHERE status='queued' AND job IS NOT NULL").fetchall()
        for r in cut:
            job = json.loads(r["job"]) if r["job"] else None
            if job and r["status"] != "queued":
                job["resume"] = True    # it had started: it carries on from the last step it finished
            con.execute("UPDATE projects SET status='interrupted', progress=NULL, job=? WHERE id=?",
                        (json.dumps(job) if job else None, r["id"]))
    for r in cut:
        append_log(r["id"], "INTERRUPTED: the server restarted " + ("before this job started: Retry runs it" if r["status"] == "queued"
                                                                    else "during this job: Retry carries on from the last step it finished"))
    for r in waiting:
        append_log(r["id"], "-- the server restarted before this job started: it's queued again, as it was")


def q(sql: str, *args, one=False, write=False):
    with _db_lock, db() as con:
        cur = con.execute(sql, args)
        if write:
            return cur.lastrowid
        rows = [dict(r) for r in cur.fetchall()]
        return (rows[0] if rows else None) if one else rows


def new_id() -> str:
    return secrets.token_hex(6)


def safe_name(name: str) -> str:
    name = SAFE.sub("_", Path(name).name).strip("._") or "file"
    return name[:120]


# ----------------------------------------------------------------- files sent in parts
_uploads_lock = threading.Lock()


def upload_paths(uid: str) -> tuple[Path, Path]:
    """An upload's data so far, and what it is ({name, size, created})."""
    if not re.fullmatch(r"[0-9a-f]{12}", uid):
        raise ValueError(f"no upload {uid}")
    return UPLOADS / f"{uid}.part", UPLOADS / f"{uid}.json"


def start_upload(name: str, size: int) -> str:
    """Make room for a file that's coming in parts; its id. Uploads left unused for UPLOAD_HOURS are cleared first."""
    if size <= 0:
        raise ValueError("an upload needs its size")
    if size > MAX_UPLOAD:
        raise ValueError(f"upload exceeds {MAX_UPLOAD // 2**20} MB")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    for f in UPLOADS.iterdir():
        try:
            if time.time() - f.stat().st_mtime > UPLOAD_HOURS * 3600:
                f.unlink()
        except FileNotFoundError:
            pass
    uid = new_id()
    data, meta = upload_paths(uid)
    data.touch()
    meta.write_text(json.dumps({"name": safe_name(name), "size": size, "created": time.time()}))
    return uid


def upload_state(uid: str) -> dict | None:
    """{name, size, received}, or None if there's no such upload."""
    data, meta = upload_paths(uid)
    try:
        info = json.loads(meta.read_text())
        return {"name": info["name"], "size": info["size"], "received": data.stat().st_size}
    except FileNotFoundError:
        return None


def finished_upload(uid: str) -> tuple[str, Path]:
    """A file sent in full, as (its name, where it is), for the request using it to move; it's no longer an upload.

    Raises:
        ValueError: there's no such upload, or not all of it has come.
    """
    state = upload_state(uid)
    if not state:
        raise ValueError(f"no upload {uid}: send that file again")
    if state["received"] != state["size"]:
        raise ValueError(f"{state['name']} has come only in part ({state['received']} of {state['size']} bytes)")
    data, meta = upload_paths(uid)
    meta.unlink()
    return state["name"], data


def put_file(source: bytes | Path, dest: Path) -> None:
    """Save a file a request brought (bytes) or that came in parts (a path, moved)."""
    if isinstance(source, Path):
        shutil.move(source, dest)
    else:
        dest.write_bytes(source)


def drop_file(source: bytes | Path) -> None:
    if isinstance(source, Path):
        source.unlink(missing_ok=True)


# ----------------------------------------------------------------- climbs helpers
FAR_KM = 5.0    # a route's own GPS this far from its area's is a mistake on its page: Swing Dance's was 135 km off


def _km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math
    p = math.pi / 180
    h = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def upsert_climb(c: Climb) -> str:
    row = q("SELECT id, lat, lng, coord_source FROM climbs WHERE source=? AND source_id=?", c.source, c.source_id, one=True)
    if row and c.raw.get("stub"):   # an area-table summary never replaces a record we already have
        return row["id"]
    if c.coord_source == "route" and c.lat is not None and c.lng is not None:
        known = (row["lat"], row["lng"], row["coord_source"]) if row and row["coord_source"] in ("area", "parent-area") else None
        if known is None and c.source == "mountainproject":    # as an area's table gave it
            item = q("SELECT data FROM campaign_items WHERE source_id=? AND data IS NOT NULL LIMIT 1", c.source_id, one=True)
            d = json.loads(item["data"]) if item else {}
            known = (d["lat"], d["lng"], d.get("coord_source") or "area") if d.get("lat") is not None and d.get("lng") is not None else None
        if known and known[0] is not None and _km(c.lat, c.lng, known[0], known[1]) > FAR_KM:
            c.raw["route_gps_ignored"] = [c.lat, c.lng]
            c.lat, c.lng, c.coord_source = known
    cid = row["id"] if row else new_id()
    # coordinates edited by hand survive re-imports
    q("""INSERT INTO climbs(id,source,source_id,url,name,kind,grade,lat,lng,coord_source,boulder_name,area_path,data,created)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(source,source_id) DO UPDATE SET url=excluded.url,name=excluded.name,kind=excluded.kind,grade=excluded.grade,
           lat=CASE WHEN climbs.coord_source='manual' THEN climbs.lat ELSE COALESCE(excluded.lat,climbs.lat) END,
           lng=CASE WHEN climbs.coord_source='manual' THEN climbs.lng ELSE COALESCE(excluded.lng,climbs.lng) END,
           coord_source=CASE WHEN climbs.coord_source='manual' THEN 'manual'
                             ELSE COALESCE(excluded.coord_source,climbs.coord_source) END,
           boulder_name=excluded.boulder_name,
           area_path=excluded.area_path,data=excluded.data""",
      cid, c.source, c.source_id, c.url, c.name, c.kind, c.grade, c.lat, c.lng, c.coord_source,
      c.boulder_name, json.dumps(c.area_path), json.dumps(c.to_dict()), time.time(), write=True)
    _doubts["at"] = 0.0
    return cid


BOULDER_KM = 1.0   # a climb this far from the others on its boulder is probably misplaced


def settle_far_coordinates() -> int:
    """Climbs saved before importing compared a route page's GPS with its area's (upsert_climb): where the page's
    is over FAR_KM from the area's, the area's are used instead, and the page's are kept for coord_doubts() to
    show, to be put right there.

    Returns:
        How many were changed.
    """
    rows = q("""SELECT c.id, c.lat, c.lng, c.data, json_extract(ci.data, '$.lat') AS alat, json_extract(ci.data, '$.lng') AS alng
                FROM climbs c JOIN campaign_items ci ON ci.source_id=c.source_id
                WHERE c.source='mountainproject' AND c.coord_source='route' AND ci.data IS NOT NULL""")
    done = set()
    for r in rows:
        if r["id"] in done or r["alat"] is None or r["alng"] is None or r["lat"] is None or r["lng"] is None \
                or _km(r["lat"], r["lng"], r["alat"], r["alng"]) <= FAR_KM:
            continue
        data = json.loads(r["data"] or "{}")
        data.setdefault("raw", {})["route_gps_ignored"] = [r["lat"], r["lng"]]
        data.update(lat=r["alat"], lng=r["alng"], coord_source="area")
        q("UPDATE climbs SET lat=?, lng=?, coord_source='area', data=? WHERE id=?", r["alat"], r["alng"], json.dumps(data), r["id"], write=True)
        done.add(r["id"])
    _doubts["at"] = 0.0
    return len(done)


_doubts: dict = {"at": 0.0, "found": {}}


def coord_doubts(fresh: bool = False) -> dict[str, dict]:
    """Climbs whose coordinates look wrong, so that they can be put right where they came from; worked out
    again at most every 30 s unless fresh, as it reads every climb.

    Returns:
        {climb id: {km, why, expected: [lat, lng]}}: a route page's GPS far from its area's (FAR_KM), whether
        set aside on import or from before that was, or a climb far from the other climbs on its boulder.
    """
    import statistics
    if not fresh and time.time() - _doubts["at"] < 30:
        return _doubts["found"]
    rows = q("""SELECT id, source, source_id, lat, lng, coord_source, boulder_name, area_path,
                       json_extract(data, '$.raw.route_gps_ignored') AS ignored FROM climbs WHERE kind != 'area'""")
    area = {r["source_id"]: (r["lat"], r["lng"]) for r in q("""SELECT source_id, json_extract(data, '$.lat') AS lat,
                json_extract(data, '$.lng') AS lng FROM campaign_items WHERE data IS NOT NULL""") if r["lat"] is not None and r["lng"] is not None}
    out = {}
    for r in rows:
        if r["ignored"] and r["lat"] is not None:
            g = json.loads(r["ignored"])
            km = _km(g[0], g[1], r["lat"], r["lng"])
            out[r["id"]] = {"km": round(km, 1), "expected": [r["lat"], r["lng"]],
                            "why": f"its page's GPS ({g[0]:.5f}, {g[1]:.5f}) is {km:.0f} km from its area's, which are used here instead"}
        elif r["coord_source"] == "route" and r["lat"] is not None and r["source"] == "mountainproject" and r["source_id"] in area:
            a = area[r["source_id"]]
            km = _km(r["lat"], r["lng"], *a)
            if km > FAR_KM:
                out[r["id"]] = {"km": round(km, 1), "expected": list(a), "why": f"its page's GPS is {km:.0f} km from where its area puts it"}
    boulders = {}
    for r in rows:
        if r["lat"] is not None and r["boulder_name"] and r["coord_source"] != "manual":
            boulders.setdefault((r["boulder_name"], r["area_path"]), []).append(r)
    for (name, _), rs in boulders.items():
        if len(rs) < 3:
            continue
        mid = (statistics.median(x["lat"] for x in rs), statistics.median(x["lng"] for x in rs))   # where most of them are
        for r in rs:
            km = _km(r["lat"], r["lng"], *mid)
            if r["id"] not in out and km > BOULDER_KM:
                out[r["id"]] = {"km": round(km, 1), "expected": [round(mid[0], 5), round(mid[1], 5)],
                                "why": f"{km:.1f} km from where most of the climbs on {name} are"}
    _doubts.update(at=time.time(), found=out)
    return out


def climb_rows(ids: list[str], data: bool = False) -> list[dict]:
    """Climbs as their cards show them, in the order given (those not found left out): each with its counts of
    images and videos (with data, its data instead), of meshes linked to it as its own (n_links, n_links_old of them old:
    MESH_RULES), of its boulder's current meshes (n_boulder, n_boulder_old), and of those an area linked it to that were
    made for another climb on it (n_shared, shared_of: the climb the newest current one was made for). A few queries for them all, rather than six each: the
    Climbs tab lists up to 500, which took 2 s a search."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in q(f"SELECT * FROM climbs WHERE id IN ({marks})", *ids)}
    own, own_old = {}, {}
    for r in q(f"SELECT l.climb_id, {OLD_MESH} AS old, COUNT(*) n FROM mesh_links l LEFT JOIN meshes m ON m.id=l.mesh_id "
               f"WHERE l.climb_id IN ({marks}) AND COALESCE(l.note,'') NOT LIKE ? GROUP BY l.climb_id, old", *ids, SHARED + "%"):
        own[r["climb_id"]] = own.get(r["climb_id"], 0) + r["n"]
        if r["old"]:
            own_old[r["climb_id"]] = r["n"]
    nshared = {r["climb_id"]: r["n"] for r in q(f"SELECT climb_id, COUNT(*) n FROM mesh_links WHERE climb_id IN ({marks}) "
                                                "AND note LIKE ? GROUP BY climb_id", *ids, SHARED + "%")}
    shared: dict[str, dict] = {}
    for r in q(f"""SELECT l.climb_id, l.note, ({MADE_FOR}) AS made_for FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id
                   WHERE l.climb_id IN ({marks}) AND l.note LIKE ? ORDER BY m.replaced_by IS NOT NULL, m.created DESC""",
               SHARED + "%", *ids, SHARED + "%"):
        shared.setdefault(r["climb_id"], r)     # the first of each: current before older, newest first
    out = []
    for cid in ids:
        r = rows.get(cid)
        if r is None:
            continue
        r = dict(r)
        r["area_path"] = json.loads(r["area_path"] or "[]")
        d = json.loads(r.pop("data") or "{}")
        if data:
            r["data"] = d
        else:
            r["n_images"] = len(d.get("images") or [])
            r["n_videos"] = len(d.get("videos") or [])
        r["n_links"], r["n_links_old"] = own.get(cid, 0), own_old.get(cid, 0)
        r["boulder_key"] = boulder_of(r["area_path"])
        s = shared.get(cid)
        r["n_shared"] = nshared.get(cid, 0)
        r["shared_of"] = (s["made_for"] or s["note"][len(SHARED):]) if s else None
        out.append(r)
    keys = sorted({r["boulder_key"] for r in out if r["boulder_key"]})
    counts, olds = {}, {}
    for r in q(f"SELECT boulder, COUNT(*) n, SUM({OLD_MESH}) old FROM meshes m WHERE replaced_by IS NULL AND boulder IN "
               f"({','.join('?' * len(keys))}) GROUP BY boulder", *keys) if keys else []:
        counts[r["boulder"]], olds[r["boulder"]] = r["n"], r["old"] or 0
    for r in out:   # meshes of its whole boulder (run_boulder())
        key = r.pop("boulder_key")
        r["n_boulder"], r["n_boulder_old"] = (counts.get(key, 0), olds.get(key, 0)) if key else (0, 0)
    return out


def climb_row(cid: str, full=False) -> dict | None:
    rows = climb_rows([cid], data=full)
    if not rows:
        return None
    r = rows[0]
    if full:
        r["meshes"] = q("SELECT m.*, l.note FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id WHERE l.climb_id=? AND COALESCE(l.note,'') NOT LIKE ?",
                        cid, SHARED + "%")
        r["boulder"] = boulder_info(r)
        r["projects"] = q("SELECT id,name,status,kind,mesh_id,harvest FROM projects WHERE climb_id=? ORDER BY created DESC", cid)
        # the areas it's in, and how far meshing it there got: an area's climbs it hasn't reached have no climb yet
        r["areas"] = q("""SELECT c.id, c.name, c.status AS area_status, ci.status, ci.note, ci.rank, ci.mesh_id, ci.project_id
                          FROM campaign_items ci JOIN campaigns c ON c.id=ci.campaign_id
                          WHERE ci.climb_id=? OR (?='mountainproject' AND ci.source_id=?) ORDER BY c.created""",
                       cid, r["source"], r["source_id"])
        r["videos_found"] = q("""SELECT cv.url, cv.found_by, cv.frames, v.title, v.uploader, v.license, v.height_cm, v.wingspan_cm,
                                    v.ape_index_cm, v.quotes, c.height_cm AS channel_height_cm, c.wingspan_cm AS channel_wingspan_cm,
                                    c.ape_index_cm AS channel_ape_index_cm, c.quotes AS channel_quotes
                                 FROM climb_videos cv LEFT JOIN videos v ON v.url=cv.url LEFT JOIN channels c ON c.channel_url=v.channel_url
                                 WHERE cv.climb_id=? ORDER BY cv.found_by, cv.created""", cid)
    return r


# How many meshes a climb has, as SQL beside a climbs row (binds SHARED + "%"): those linked to it as its own (its own
# meshes, and pools of it with others), and its boulder's (run_boulder()), as its card counts them (n_links, n_boulder)
MESH_COUNT = ("((SELECT COUNT(*) FROM mesh_links l WHERE l.climb_id=climbs.id AND COALESCE(l.note,'') NOT LIKE ?)"
              " + (SELECT COUNT(*) FROM meshes b WHERE b.boulder=climbs.area_path AND b.replaced_by IS NULL))")


def climb_obj(cid: str) -> Climb:
    r = q("SELECT data,lat,lng,coord_source,name,grade,boulder_name FROM climbs WHERE id=?", cid, one=True)
    if not r:
        raise KeyError(cid)
    c = Climb.from_dict(json.loads(r["data"]))
    # manual edits win over the cached source data
    c.lat, c.lng, c.coord_source = r["lat"], r["lng"], r["coord_source"]
    c.name, c.grade, c.boulder_name = r["name"], r["grade"], r["boulder_name"]
    return c


def link_mesh(mid: str, cid: str, note: str | None) -> None:
    """Link a mesh to a climb; a mesh without coordinates inherits the climb's."""
    q("INSERT OR REPLACE INTO mesh_links VALUES(?,?,?,?)", mid, cid, note, time.time(), write=True)
    q("UPDATE meshes SET lat=COALESCE(lat,(SELECT lat FROM climbs WHERE id=?)), "
      "lng=COALESCE(lng,(SELECT lng FROM climbs WHERE id=?)) WHERE id=?", cid, cid, mid, write=True)


def boulder_key(area_path: list[str] | None) -> str | None:
    """A climb's lowest area, as areas' lists record it: its area path, as JSON (as boulder_of() has it). Whether
    that's one boulder or a group is leaf_kind()'s."""
    return json.dumps(list(area_path)) if area_path else None


def leaf_kind(key: str | None, kinds: dict | None = None) -> str | None:
    """What a lowest area is: "group" where its name says it holds several boulders (GROUP_AREA), otherwise as its page
    said (boulder_kind(): "boulder", "group" or "unknown"), or None if that hasn't been read."""
    if not key:
        return None
    try:
        if GROUP_AREA.search(str(json.loads(key)[-1])):
            return "group"
    except (TypeError, ValueError, IndexError):
        return None
    return (kinds if kinds is not None else boulder_kinds()).get(key)


def boulder_kinds() -> dict[str, str]:
    """What each lowest area read so far is (boulder_kind()): {key: "boulder" | "group" | "unknown"}."""
    return {r["key"]: r["kind"] for r in q("SELECT key, kind FROM boulders WHERE kind IS NOT NULL")}


def pools_whole(boulder: str | None, kinds: dict | None = None) -> bool:
    """Whether a lowest area's climbs are meshed together, as one boulder: unless its page says it holds several, or
    doesn't say it's one rock (then photos say what pools: climb_clusters()). Not read yet (its page couldn't be had),
    as before: by its name."""
    if not boulder:
        return False
    return leaf_kind(boulder, kinds) in (None, "boulder")


def boulder_kind(boulder: str | None, area_url: str | None, log=print) -> str | None:
    """What a lowest area of climbs is, from its Mountain Project page (mp_mod.area_kind(): its name and description),
    read once and kept (the boulders table). A group's, or one that doesn't say, climbs harvested for it as one boulder
    are queued to mesh on their own (settle_groups()). None if it can't be read now.

    Args:
        area_url: its page, as its climbs' pages name it (Climb.area_urls[-1]).
    """
    if not boulder:
        return None
    row = q("SELECT kind FROM boulders WHERE key=?", boulder, one=True)
    if row and row["kind"]:
        return row["kind"]
    if not area_url:
        return None
    try:
        area = climbs_mod.fetch_climb_url(area_url, include_children=False)[0]
    except Exception as e:
        log(f"couldn't read its area's page, {area_url} ({type(e).__name__}: {e}): pooled by its name, as before")
        return None
    kind, why = mp_mod.area_kind(area.name, area.description)
    for key in dict.fromkeys([boulder, json.dumps(list(area.area_path)) if area.area_path else boulder]):
        q("INSERT OR REPLACE INTO boulders(key, name, url, kind, why, routes, checked) VALUES(?,?,?,?,?,?,?)", key,
          (area.name or "").strip("* "), area_url, kind, why, len(area.raw.get("routes") or []), time.time(), write=True)
    log(f"{(area.name or '').strip('* ')}: " + {"boulder": "one boulder", "group": "several boulders",
                                                 "unknown": "not clearly one boulder"}[kind] + f", as {why}")
    if kind != "boulder":
        settle_groups(boulder)
    return kind


def climb_area_url(climb: Climb) -> str | None:
    """A climb's lowest area's page: from its own page, read now if it hasn't been (and kept, so a harvest reads it
    from there)."""
    if climb.area_urls:
        return climb.area_urls[-1]
    if not climb.url:
        return None
    try:
        got = climbs_mod.fetch_climb_url(climb.url, include_children=False)[0]
    except Exception:
        return None
    return got.area_urls[-1] if got.area_urls else None


def settle_groups(boulder: str) -> int:
    """Climbs of a lowest area that isn't one boulder, harvested by areas only for its mesh: each queued to mesh on its
    own, from its own photos. Returns how many."""
    rows = q("SELECT campaign_id, source_id FROM campaign_items WHERE boulder=? AND status='harvested' AND redo IS NULL", boulder)
    for r in rows:
        set_item(r["campaign_id"], r["source_id"], status="pending", retry=1, redo="rebuild", was="harvested", redo_started=0,
                 note="its Mountain Project area holds several boulders, or doesn't say it's one: meshed on its own")
    return len(rows)


# Meshes made before the rules that decide what an area meshes last changed (what's one boulder and what's a group, which
# photos the filter keeps) are "old": shown so to editors, left out of a boulder's meshes for visitors, and to be made
# again under the rules, as old_mesh_report() follows. MESH_RULES goes up with each such change; MESH_RULES_SINCE says
# when the server began making meshes under each, for meshes made before meshes.rules was kept. Only what areas and
# projects made (source "project:"): an uploaded mesh is never old.
MESH_RULES = 1
MESH_RULES_SINCE = {1: 1794510600.0}    # 2026-11-12 11:10 PST: boulderme.sh ran b70f624 from between 11:03 and 11:16, the
                                        # first with 5a7cb26 (an area whose page doesn't say it's one rock isn't meshed
                                        # whole) and 584beda (filter verdicts from before the CLIP check get it)
OLD_MESH = f"(m.source LIKE 'project:%' AND COALESCE(m.rules, 0) < {MESH_RULES})"     # an old mesh, as meshes m


def mesh_is_old(row: dict) -> bool:
    return (row.get("source") or "").startswith("project:") and (row.get("rules") or 0) < MESH_RULES


def mesh_old(mid: str) -> bool:
    """Whether a mesh is old, as made under older rules (mesh_is_old()) or covered by a newer one (meshes.old_data), or
    replaced: not worth finding beta or size in, as what replaces it gets them."""
    row = q("SELECT source, rules, old_data, replaced_by FROM meshes WHERE id=?", mid, one=True)
    return bool(row and (mesh_is_old(row) or row["old_data"] or row["replaced_by"]))


SHARED = "same boulder as "   # how areas noted linking a climb to the mesh made for another climb on its boulder, before
                              # they meshed every climb on its own: shown as that other climb's, not this one's
# The climb a shared link's mesh was made for, by the mesh's own link, as SQL beside a mesh_links l (binds SHARED + "%").
# Not the note's name: an area could link a climb through another that only shared the mesh itself, and names change.
MADE_FOR = ("SELECT c.name FROM mesh_links o JOIN climbs c ON c.id=o.climb_id "
            "WHERE o.mesh_id=l.mesh_id AND COALESCE(o.note,'') NOT LIKE ? ORDER BY o.created LIMIT 1")


def boulder_of(area_path: list[str] | str | None) -> str | None:
    """A boulder as its projects and meshes record it: the area path its climbs share, as climbs.area_path has
    it, or None if the last area holds several boulders (boulder_key())."""
    path = json.loads(area_path or "[]") if isinstance(area_path, str) else area_path
    return json.dumps(path) if boulder_key(path) else None


def boulder_mesh_with(climb_id: str, area_path) -> str | None:
    """The newest current mesh of a climb's boulder (run_boulder()) that pooled photos of the climb's own, if there is
    one: as another area made it, whose climbs on the boulder this one's overlap."""
    boulder = boulder_of(area_path)
    if not boulder:
        return None
    for m in q("SELECT id, source, rules FROM meshes WHERE boulder=? AND replaced_by IS NULL ORDER BY created DESC", boulder):
        if not (m["source"] or "").startswith("project:") or mesh_is_old(m):     # an old one is to be made again
            continue
        try:
            climbs = json.loads((PROJECTS / m["source"].split(":", 1)[1] / CLIMBS_FILE).read_text())["climbs"]
        except (OSError, ValueError, KeyError):
            continue
        if any(c.get("id") == climb_id and c.get("projects") for c in climbs):
            return m["id"]
    return None


def boulder_unchanged(boulder: str) -> str | None:
    """A boulder's current mesh (run_boulder()), if it was made from the photos its climbs' harvests hold now: the same
    harvests of the same climbs, none harvested again since its photos were pooled. None otherwise.

    Args:
        boulder: as boulder_of() has it.
    """
    proj = boulder_project(boulder)
    if not proj or proj["status"] != "done" or not proj["mesh_id"] or not q(
            f"SELECT 1 FROM meshes m WHERE id=? AND replaced_by IS NULL AND NOT {OLD_MESH}", proj["mesh_id"]):
        return None     # nor an old one (MESH_RULES), made again under the rules from the same photos
    try:
        record = json.loads((PROJECTS / proj["id"] / CLIMBS_FILE).read_text())
        then, pooled_at = {c["id"]: sorted(c["projects"]) for c in record["climbs"]}, float(record["created"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if {c["id"]: sorted(climb_harvests(c["id"])) for c in boulder_climbs(boulder)} != then:
        return None
    for h in {h for hs in then.values() for h in hs}:
        photos = PROJECTS / h / "photos"
        if photos_away(h):      # offloaded to the backup, as they were
            continue
        try:
            if max([photos.stat().st_mtime] + [f.stat().st_mtime for f in photos.iterdir()]) > pooled_at:
                return None     # harvested again since
        except OSError:
            return None
    return proj["mesh_id"]


def boulder_name(boulder: str) -> str:
    """A boulder's name; a pool's, its climbs' (pool_name())."""
    if is_pool(boulder):
        return pool_name(boulder)
    return json.loads(boulder)[-1].strip("* ")


def boulder_climbs(boulder: str) -> list[dict]:
    """The climbs known on a boulder, by name; a pool's, its members."""
    if is_pool(boulder):
        members = pool_members(boulder)
        return q(f"SELECT id, name, grade, lat, lng, data FROM climbs WHERE id IN ({','.join('?' * len(members)) or 'NULL'}) ORDER BY name",
                 *members)
    return q("SELECT id, name, grade, lat, lng, data FROM climbs WHERE area_path=? ORDER BY name", boulder)


# ------------------------------------------------------------------ pools: climbs of one rock, by their photos
POOL_MARK = "#"     # the last of a pool's key: after its lowest area's path, a climb of it


def is_pool(key: str | None) -> bool:
    try:
        return bool(key) and str(json.loads(key)[-1]).startswith(POOL_MARK)
    except (TypeError, ValueError, IndexError):
        return False


def pool_members(key: str) -> list[str]:
    r = q("SELECT members FROM pools WHERE key=?", key, one=True)
    return json.loads(r["members"]) if r else []


def climbs_named(ids: list[str]) -> str:
    """These climbs' names, the first three and how many more ("" for none)."""
    names = [c["name"] for c in q(f"SELECT name FROM climbs WHERE id IN ({','.join('?' * len(ids)) or 'NULL'}) ORDER BY name", *ids)]
    return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def pool_name(key: str) -> str:
    """A pool's name: its climbs' (climbs_named())."""
    return climbs_named(pool_members(key)) or json.loads(key)[-2].strip("* ")


def pool_for(leaf: str, members: list[str]) -> str:
    """The pool of these climbs of a lowest area: one there is whose climbs are among them (the one sharing most), now
    with all of them, or a new one."""
    best, share = None, 0
    for r in q("SELECT key, members FROM pools WHERE leaf=?", leaf):
        if is_rest(r["key"]):   # what a boulder's mesh left out (keep_rest()), not climbs whose photos match
            continue
        had = set(json.loads(r["members"]))
        if had <= set(members) and len(had) > share:
            best, share = r["key"], len(had)
    key = best or json.dumps(json.loads(leaf) + [POOL_MARK + sorted(members)[0]])
    q("INSERT INTO pools(key, leaf, members, created, updated) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
      "members=excluded.members, updated=excluded.updated", key, leaf, json.dumps(sorted(members)), time.time(), time.time(), write=True)
    return key


def climb_samples(climb_ids: list[str]) -> dict[str, list[Path]]:
    """Each climb's own photos (its page's and its videos' frames, from its harvests) to compare with another's."""
    out = {}
    for cid in climb_ids:
        own = [PROJECTS / h / "photos" / n for h in climb_harvests(cid) for n in sorted(viewpoints.own_photos(PROJECTS / h))]
        if own:
            out[cid] = relevance_mod.link_sample(own)
    return out


def link(a: str, b: str, pairs: int, how: str) -> None:
    a, b = sorted((a, b))
    q("INSERT INTO climb_links(a, b, pairs, how, checked) VALUES(?,?,?,?,?) ON CONFLICT(a, b) DO UPDATE SET "
      "pairs=MAX(pairs, excluded.pairs), how=CASE WHEN excluded.pairs > pairs THEN excluded.how ELSE how END, checked=excluded.checked",
      a, b, pairs, how, time.time(), write=True)


# names too common to say which climb a description means
COMMON_NAMES = re.compile(r"^(the\s+)?(unnamed|unknown|no name|project|open project|arete|arête|traverse|roof|crack|slab|dyno|mantle|"
                          r"prow|corner|face|left|right|center|centre|direct|sit|sit start|ss|lowball|highball|warm[- ]?up)$", re.I)


def links_from_text(climb_ids: list[str]) -> int:
    """Climbs whose page (description or location) names another of them ("left of X", "same boulder as X"): linked, as
    on one rock, most likely ("text"). Returns how many pairs."""
    rows = q(f"SELECT id, name, data FROM climbs WHERE id IN ({','.join('?' * len(climb_ids)) or 'NULL'})", *climb_ids)
    names = {r["id"]: (r["name"] or "").strip() for r in rows}
    n = 0
    for r in rows:
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        text = f"{d.get('description') or ''} {d.get('location') or ''}"
        for other, name in names.items():
            if other != r["id"] and len(name) >= 5 and not COMMON_NAMES.match(name) and \
                    re.search(r"(?<![\w'])" + re.escape(name) + r"(?![\w'])", text, re.I):
                link(r["id"], other, relevance_mod.LINK_MIN_PAIRS, "text")
                n += 1
    return n


def link_climbs(climb_ids: list[str], log) -> None:
    """Compare these climbs' photos, those pairs not compared before (relevance.link_climbs()); with many, each with
    those whose photos look most like its own (relevance.nearest()). Those whose pages name each other are linked by
    that (links_from_text())."""
    links_from_text(climb_ids)
    samples = climb_samples(climb_ids)
    if len(samples) < 2:
        return
    known = {(r["a"], r["b"]) for r in q(f"SELECT a, b FROM climb_links WHERE a IN ({','.join('?' * len(samples))})", *samples)}
    new = [c for c in samples if sum((min(c, o), max(c, o)) in known for o in samples if o != c) < min(relevance_mod.LINK_NEAREST, len(samples) - 1)]
    pairs = [p for p in relevance_mod.nearest(samples, new, relevance_mod.LINK_NEAREST, log) if p not in known]
    if pairs:
        for (a, b), n in relevance_mod.link_climbs(samples, pairs, log).items():
            link(a, b, n, "photos")


def links_from_models(mid: str, pid: str) -> int:
    """Climbs whose photos landed in one of COLMAP's models together (pipeline.write_models()), in a pooled mesh's
    project: linked, as overlapping. Returns how many pairs."""
    try:
        models = json.loads((MESHES / mid / pipeline.MODELS_FILE).read_text())["models"]
        files = {f["file"]: f.get("pooled_from") for f in json.loads((PROJECTS / pid / "sources.json").read_text()).get("files", [])}
        whose = {h: c["id"] for c in json.loads((PROJECTS / pid / CLIMBS_FILE).read_text())["climbs"] for h in c.get("projects") or []}
    except (OSError, ValueError, KeyError):
        return 0
    n = 0
    for m in models:
        count: dict[str, int] = {}
        for name in m["photos"]:
            cid = whose.get(files.get(name) or "")
            if cid:
                count[cid] = count.get(cid, 0) + 1
        ids = sorted(c for c, k in count.items() if k >= relevance_mod.LINK_MIN_PAIRS)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                link(a, b, min(count[a], count[b]), "colmap")
                n += 1
    return n


def clusters(climb_ids: list[str]) -> list[list[str]]:
    """These climbs, grouped by their links (link_climbs()): each group's photos are of one rock, or rocks seen together."""
    parent = {c: c for c in climb_ids}

    def root(c):
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c
    for r in q(f"SELECT a, b FROM climb_links WHERE pairs >= ? AND a IN ({','.join('?' * len(climb_ids)) or 'NULL'})",
               relevance_mod.LINK_MIN_PAIRS, *climb_ids):
        if r["b"] in parent:
            parent[root(r["a"])] = root(r["b"])
    groups: dict[str, list[str]] = {}
    for c in climb_ids:
        groups.setdefault(root(c), []).append(c)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g))


def boulder_url(boulder: str) -> str | None:
    """A boulder's own page (Mountain Project's area for it: the last of its climbs' areas), as its climbs' pages name
    it, or None if none says."""
    for c in boulder_climbs(boulder):
        try:
            urls = json.loads(c["data"] or "{}").get("area_urls") or []
        except ValueError:
            continue
        if urls and str(urls[-1]).startswith("https://"):
            return urls[-1]
    return None


def adopt_listed_climbs(boulder: str) -> int:
    """Save the climbs an area lists on a boulder that it hasn't reached yet, from what its list says of them,
    so that a boulder's mesh can include them too. Returns how many."""
    rows = q("SELECT source_id, data FROM campaign_items WHERE boulder=? AND climb_id IS NULL AND data IS NOT NULL", boulder)
    for r in rows:
        cid = upsert_climb(Climb.from_dict(json.loads(r["data"])))
        q("UPDATE campaign_items SET climb_id=? WHERE source_id=? AND climb_id IS NULL", cid, r["source_id"], write=True)
    return len(rows)


def adopt_page_climbs(boulder: str, log) -> int | None:
    """Save the problems a boulder's Mountain Project page lists (its route table) that aren't known yet, so that its
    mesh harvests and places every climb on it, as one boulder needs. Returns how many problems the page lists, or None
    if it couldn't be read (its climbs known by then stand)."""
    row = q("SELECT url FROM boulders WHERE key=? AND url IS NOT NULL", boulder, one=True)
    if not row or is_pool(boulder):
        return None
    try:
        stubs = [c for c in climbs_mod.fetch_climb_url(row["url"], include_children=True)[1:] if c.kind == "boulder"]
    except Exception as e:
        log(f"couldn't read its page's problems ({type(e).__name__}: {e}): meshing the climbs known on it")
        return None
    known = {r["source_id"] for r in q("SELECT source_id FROM climbs WHERE area_path=?", boulder)}
    new = [c for c in stubs if c.source_id not in known]
    for c in new:
        c.area_path = json.loads(boulder)
        upsert_climb(c)
    if new:
        log(f"{plural(len(new), 'problem')} its Mountain Project page lists weren't known: now they are, to harvest and place too")
    return len(stubs)


def boulder_project(boulder: str) -> dict | None:
    """A boulder's project, which pools its climbs' harvests (run_boulder())."""
    return q("SELECT * FROM projects WHERE kind='boulder' AND boulder=? ORDER BY created DESC LIMIT 1", boulder, one=True)


def pool_leaf(key: str) -> str:
    """The lowest area a pooled mesh's climbs are on: a boulder's own key, or the area a pool of climbs is in."""
    return json.dumps(json.loads(key)[:-1]) if is_pool(key) else key


def leaf_url(leaf: str, cache: dict) -> str | None:
    """A lowest area's Mountain Project page: as read for what it is (boulder_kind()), or as its climbs' pages name it.

    Args:
        cache: {lowest area: its page}, kept by the caller for the many meshes of one listing.
    """
    if leaf not in cache:
        row = q("SELECT url FROM boulders WHERE key=? AND url IS NOT NULL", leaf, one=True)
        cache[leaf] = row["url"] if row else boulder_url(leaf)
    return cache[leaf]


def pooled_summary(key: str, climbs: list[dict], kinds: dict, mid: str | None = None, homes: dict | None = None,
                   least: float | None = None) -> dict:
    """What a pooled mesh's card says of it: how many climbs' photos it's made from and how many of them it places (by
    their own photos, each counted only on the mesh of its boulder that places most of them: homes, {climb: mesh}), its
    lowest area and what that is, and whether it's one boulder. climbs: {id, name, view (views.json's, if placed), own
    (how many photos of its own were pooled, if none placed)}.

    It's one boulder when Mountain Project says the area is one rock, it places more than one climb, each climb with
    photos of its own has at least least % of them placed (the admin page's boulders.placed_percent), and at least one,
    every climb on it was tried in it, and nothing it left out makes a mesh apart (keep_rest()). Where its reconstruction
    recorded how what it left out is tied to it (scenes), that's each scene that counts (rest_models()): none is waiting
    to be meshed or made a mesh, though one whose meshing was tried and made nothing doesn't count against it. Before
    that, it's the climbs that left REST_LEAST out: none did, or meshing theirs made nothing. Otherwise a mesh placing
    more than one climb is a group. A mesh from before its rest was kept goes by whether every climb known on the area
    is placed in it. short: the climbs with own photos too few of which it places, {name, placed, of}; apart: how many
    scenes (or climbs) it left out to mesh apart, and rest how that went, as rest_summary() says.
    """
    leaf = pool_leaf(key)
    placed = {c["id"] for c in climbs if c.get("view") and (not homes or homes.get(c["id"], mid) == mid)}
    least = settings().get("boulders.placed_percent") if least is None else least
    short = []
    for c in climbs:
        of = (c.get("view") or {}).get("of") or c.get("own") or 0
        n = (c.get("view") or {}).get("photos", 0) if c["id"] in placed else 0
        if of and (n < 1 or 100 * n < least * of):
            short.append({"name": c.get("name"), "placed": n, "of": of})
    kind = leaf_kind(leaf, kinds)
    rest = load_rest(mid) if mid and not is_pool(key) else None
    apart, state, split, blocks = rest_summary(rest) if rest is not None else (0, None, False, False)
    untried = boulder_untried(mid, leaf) if mid and not is_pool(key) else None
    if len(placed) < 2 or short:
        whole = False
    elif rest is not None:
        whole = kind == "boulder" and not blocks and untried == 0
    else:
        on_leaf = {c["id"] for c in q("SELECT id FROM climbs WHERE area_path=?", leaf)} if kind == "boulder" and not is_pool(key) else set()
        whole = bool(on_leaf) and on_leaf <= {c["id"] for c in climbs if c.get("view")}
    return {"climbs": len(climbs), "placed": len(placed), "area": json.loads(leaf)[-1].strip("* "), "kind": kind, "whole": whole,
            "apart": apart, "rest": state if apart else None, "split": split, "untried": untried,
            "scenes": bool(rest and "models" in rest), "rest_of": rest_parent(key) if is_rest(key) else None, "short": short,
            "least": least}


def boulder_shares() -> list[dict]:
    """Each current mesh of an area Mountain Project calls one rock (not a pool's): each of its climbs with photos of
    their own and how many of them it places (views.json), fewest first, for the admin page to show which are either
    side of boulders.placed_percent; evidence, whether its videos' frames counted only where placed or tied to placed
    photos (viewpoints.own_by_evidence()). [{id, name, evidence, climbs: [{name, placed, of}]}]"""
    kinds, out = boulder_kinds(), []
    for m in q("SELECT id, name, boulder FROM meshes WHERE boulder IS NOT NULL AND replaced_by IS NULL ORDER BY created DESC"):
        if is_pool(m["boulder"]) or leaf_kind(m["boulder"], kinds) != "boulder":
            continue
        views = viewpoints.load(MESHES / m["id"]) or {}
        names = {c["id"]: c["name"] for c in q("SELECT id, name FROM climbs WHERE area_path=?", m["boulder"])}
        climbs = [{"name": names.get(cid, cid), "placed": v.get("photos") or 0, "of": v["of"]}
                  for cid, v in (views.get("climbs") or {}).items() if v and v.get("of")]
        climbs += [{"name": names.get(cid, cid), "placed": 0, "of": n} for cid, n in (views.get("unplaced") or {}).items() if n]
        if climbs:
            out.append({"id": m["id"], "name": m["name"], "evidence": bool(views.get("evidence")),
                        "climbs": sorted(climbs, key=lambda c: (c["placed"] / c["of"], c["name"]))})
    return out


def boulder_untried(mid: str, leaf: str) -> int | None:
    """How many of a boulder's climbs its mesh didn't try to place: known on it, or listed on its Mountain Project page
    when the mesh was made, but with no photo pooled into it from a harvest of theirs (never harvested, a harvest that
    found none, or known since). None if its project's record of them can't be read."""
    src = q("SELECT source FROM meshes WHERE id=?", mid, one=True)
    if not src or not src["source"].startswith("project:"):
        return None
    pdir = PROJECTS / src["source"].split(":", 1)[1]
    try:
        record = json.loads((pdir / CLIMBS_FILE).read_text())
    except (OSError, ValueError):
        return None
    try:
        files = json.loads((pdir / "sources.json").read_text()).get("files") or []
        gave = {h for f in files for h in [f.get("pooled_from"), *(f.get("also_in") or [])] if h}
    except (OSError, ValueError):   # no record of which harvest gave which photo: those harvested, as before
        gave = {h for c in record.get("climbs") or [] for h in c.get("projects") or []}
    pooled = {c["id"] for c in record.get("climbs") or [] if gave & set(c.get("projects") or [])}
    known = {r["id"] for r in q("SELECT id FROM climbs WHERE area_path=?", leaf)}
    return max(len(known - pooled), (record.get("page_problems") or 0) - len(pooled), 0)


def boulder_homes(rows: list[dict], views: dict[str, dict]) -> dict[str, str]:
    """Which mesh each climb counts toward among the current meshes of one Mountain Project boulder: its own mesh and
    the mesh of what that left out (rows, {id, boulder}; views, {mesh: views.json}): the one placing most of its own
    photos, so that a climb is in only one group. {climb: mesh}; climbs placed in only one aren't listed."""
    best: dict[tuple[str, str], tuple[int, str]] = {}
    seen: dict[tuple[str, str], int] = {}
    for r in rows:
        key = r["boulder"]
        if not key or (is_pool(key) and not is_rest(key)):
            continue
        leaf = pool_leaf(key)
        for cid, v in ((views.get(r["id"]) or {}).get("climbs") or {}).items():
            n = (v or {}).get("photos") or 0
            seen[(leaf, cid)] = seen.get((leaf, cid), 0) + 1
            if n > best.get((leaf, cid), (-1, ""))[0]:
                best[(leaf, cid)] = (n, r["id"])
    return {cid: m for (leaf, cid), (n, m) in best.items() if seen[(leaf, cid)] > 1}


def boulder_info(climb: dict) -> dict | None:
    """What a climb's card says of its boulder: the other climbs on it and their own meshes, the boulder's own
    meshes, with this climb's view of each, and meshes areas linked it to as made for another climb on it."""
    boulder = boulder_of(climb["area_path"])
    if not boulder:
        return None
    climbs = boulder_climbs(boulder)
    own = {}
    for r in q(f"""SELECT l.climb_id, m.id, m.name FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id
                   WHERE m.replaced_by IS NULL AND COALESCE(l.note,'') NOT LIKE ? AND l.climb_id IN ({",".join("?" * len(climbs))})""",
               SHARED + "%", *[c["id"] for c in climbs]):
        own.setdefault(r["climb_id"], []).append({"id": r["id"], "name": r["name"]})
    meshes = []
    rests = [m["id"] for m in q("SELECT id, boulder FROM meshes WHERE boulder LIKE ? AND replaced_by IS NULL", boulder[:-1] + ", %")
             if is_rest(m["boulder"]) and pool_leaf(m["boulder"]) == boulder]     # and those of what its mesh left out
    for m in q(f"SELECT id, name, created FROM meshes WHERE (boulder=? OR id IN ({','.join('?' * len(rests)) or 'NULL'})) "
               "AND replaced_by IS NULL ORDER BY created DESC", boulder, *rests):
        views = viewpoints.load(MESHES / m["id"]) or {}
        meshes.append({**m, "view": (views.get("climbs") or {}).get(climb["id"]), "placed": len(views.get("climbs") or {}),
                       "unplaced": (views.get("unplaced") or {}).get(climb["id"])})
    shared = q(f"""SELECT m.id, m.name, l.note, ({MADE_FOR}) AS made_for FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id
                   WHERE l.climb_id=? AND l.note LIKE ? AND m.replaced_by IS NULL""", SHARED + "%", climb["id"], SHARED + "%")
    proj = boulder_project(boulder)
    kind = q("SELECT kind, why, url FROM boulders WHERE key=?", boulder, one=True)
    named = leaf_kind(boulder, {}) == "group"
    return {"key": boulder, "name": boulder_name(boulder), "meshes": meshes, "kind": "group" if named else kind and kind["kind"],
            "why": "its name says so" if named else kind and kind["why"], "url": (kind and kind["url"]) or boulder_url(boulder),
            "shared": [{"id": s["id"], "name": s["name"], "made_for": s["made_for"] or s["note"][len(SHARED):]} for s in shared],
            "climbs": [{"id": c["id"], "name": c["name"], "grade": c["grade"], "meshes": own.get(c["id"], [])}
                       for c in climbs if c["id"] != climb["id"]],
            "project": {k: proj[k] for k in ("id", "status")} if proj else None}


def supersede(old: str, new: str) -> None:
    """A rebuilt mesh takes over from the one it replaces: its climbs, and its place in area lists.
    The old one stays, as an older version of the new one. The meshes of what the old one left out (run_rest()) go with
    it, as older versions of the new one, which has its own; going back to one brings back its own."""
    for link in q("SELECT climb_id, note FROM mesh_links WHERE mesh_id=?", old):
        link_mesh(new, link["climb_id"], link["note"])
    q("DELETE FROM mesh_links WHERE mesh_id=?", old, write=True)
    q("UPDATE campaign_items SET mesh_id=? WHERE mesh_id=?", new, old, write=True)
    q("UPDATE meshes SET replaced_by=? WHERE id=? OR replaced_by=?", new, old, old, write=True)
    q("UPDATE meshes SET replaced_by=NULL WHERE id=?", new, write=True)
    for r in q("SELECT id, boulder, replaced_by FROM meshes WHERE boulder LIKE ?", f"%{REST_MARK}%"):
        if not is_rest(r["boulder"]) or r["id"] in (old, new):
            continue
        if rest_parent(r["boulder"]) == old and r["replaced_by"] is None:
            q("UPDATE meshes SET replaced_by=? WHERE id=?", new, r["id"], write=True)
            q("DELETE FROM mesh_links WHERE mesh_id=? AND note=?", r["id"], REST_LINK, write=True)
        elif rest_parent(r["boulder"]) == new and r["replaced_by"] == new:
            q("UPDATE meshes SET replaced_by=NULL WHERE id=?", r["id"], write=True)
            members = set(pool_members(r["boulder"]))
            for cid in (viewpoints.load(MESHES / r["id"]) or {}).get("climbs") or {}:
                if cid in members:
                    link_mesh(r["id"], cid, REST_LINK)


def make_current(mid: str) -> None:
    """Go back to an older version of a mesh, which takes over from the one that replaced it."""
    m = q("SELECT replaced_by, source FROM meshes WHERE id=?", mid, one=True)
    if not m or not m["replaced_by"]:
        return
    supersede(m["replaced_by"], mid)
    if m["source"].startswith("project:"):
        q("UPDATE projects SET mesh_id=? WHERE id=?", mid, m["source"].split(":", 1)[1], write=True)
    meshes_changed()


def clean_mesh(mid: str, log) -> None:
    """Work out which of a new mesh's pieces to hide. Only costs that cleanup if it fails."""
    if not meshclean.available():
        return
    try:
        meshclean.clean(MESHES / mid, log)
    except Exception as e:
        log(f"cleanup failed ({e}); showing the whole mesh")


def clean_existing_meshes() -> None:
    """Give reconstructions from before cleanups existed one. Skips any that has one or had it deleted."""
    if not meshclean.available():
        return
    use_processing_cores()
    for m in q("SELECT id,name FROM meshes WHERE source LIKE 'project:%' AND filename='mesh.glb' ORDER BY created"):
        if meshclean.load(MESHES / m["id"]) is None:
            clean_mesh(m["id"], lambda line, name=m["name"]: print(f"{name}: {line}", flush=True))
            time.sleep(0.1)


_cleanup_lock = threading.Lock()    # held while a cleanup is changed from the API, so two changes at once don't mix


def cleanup_answer(mid: str, record: dict, **extra) -> dict:
    """A mesh's cleanup as the API gives it: with how many changes to it can be undone, and the mesh's version now,
    which its files' addresses carry, and which changes with its pieces."""
    row = q("SELECT filename, source FROM meshes WHERE id=?", mid, one=True)
    v = mesh_version(mid, texture_mode(mid, row["source"]), row["filename"]) if row else None
    return {**record, "undo": meshclean.undo_steps(served_dir(mid)), "v": v, **extra}


def chosen_faces(mid: str, body: dict, record: dict):
    """The faces chosen in the viewer for a change to a mesh's cleanup, as a bool for each of the mesh's own faces.

    The viewer sends a bit for each face of the copy it has ("count" of them, gzipped, in base64), and with "lod" the
    copy packed for viewing that it's from (glbpack.py), whose faces are in another order. Only the full one has
    every face: a preview's are fewer and simplified.
    """
    import zlib

    import numpy as np
    count = int(body.get("count") or 0)
    if count <= 0:
        raise ValueError("count: how many faces the viewer's copy has")
    try:
        unzip = zlib.decompressobj(wbits=31)
        bits = unzip.decompress(base64.b64decode(body.get("faces") or "", validate=True), (count + 7) // 8)
    except (ValueError, zlib.error) as e:
        raise ValueError("faces: a gzipped bit for each face, in base64") from e
    chosen = np.unpackbits(np.frombuffer(bits, np.uint8), bitorder="little")[:count].astype(bool)
    if len(chosen) != count:
        raise ValueError("faces: fewer than count")
    lod = body.get("lod")
    if not lod:
        return chosen
    if lod != "full":
        raise ValueError("this needs every face of the mesh: load its full detail first")
    row = q("SELECT filename, source FROM meshes WHERE id=?", mid, one=True)
    src = viewer_glb(mid, texture_mode(mid, row["source"]), row["filename"]) if row else None
    order = glbpack.path(src, "full", faces=True) if src else None
    if order is None:
        raise ValueError("this mesh's copy for viewing has changed since it was loaded: open it again")
    origin = np.frombuffer(order.read_bytes(), "<u4")
    if len(origin) != count or (origin.size and int(origin.max()) >= record["faces"]):
        raise ValueError("the faces chosen don't match this mesh: open it again")
    out = np.zeros(record["faces"], bool)
    out[origin[chosen]] = True
    return out


def orient_existing_meshes() -> None:
    """Report on reconstructions from before orientation.json existed. A model still on disk can be
    checked; most had theirs deleted to save space, so all a report can say is what their photos hold."""
    if not orientation.available():
        return
    for m in q("SELECT id,source FROM meshes WHERE source LIKE 'project:%' ORDER BY created"):
        mesh_dir = MESHES / m["id"]
        if orientation.load(mesh_dir) is not None:
            continue
        pid = m["source"].split(":", 1)[1]
        model, photos = PROJECTS / pid / "work" / "aligned", PROJECTS / pid / "photos"
        try:
            if (model / "images.bin").exists():
                report = orientation.check(model, photos)
                if report["iphone"]["status"] == "used":    # this mesh is already built, so it stays as it is
                    report["iphone"]["status"] = "not_applied"
                orientation.save(mesh_dir, report, model)
            else:
                orientation.save(mesh_dir, orientation.no_cameras(photos))
        except Exception as e:
            print(f"orientation report failed for {m['id']}: {e}", flush=True)
        time.sleep(0.05)


def record_sources(mid: str, pid: str, log) -> None:
    """Keep with a new mesh where its photos came from. Only costs that record if it fails."""
    try:
        provenance.save(MESHES / mid, PROJECTS / pid)
    except Exception as e:
        log(f"recording where the photos came from failed ({e})")


def source_existing_meshes() -> None:
    """Record where the photos of reconstructions from before photo_sources.json came from, from their
    projects as they are now. Those without cameras.json can only list every photo they were given."""
    for m in q("SELECT id,source FROM meshes WHERE source LIKE 'project:%' ORDER BY created"):
        pid = m["source"].split(":", 1)[1]
        if provenance.load(MESHES / m["id"]) is None and (PROJECTS / pid).exists() and (MESHES / m["id"]).exists():
            record_sources(m["id"], pid, lambda line, mid=m["id"]: print(f"{mid}: {line}", flush=True))


def shrink_texture(path: Path, sides=SHRINK_SIDES) -> tuple[int, dict[int, Path]]:
    """JPEG copies of a texture at most each of sides pixels wide and high, kept in smaller/ beside it, made from one
    reading of it. Where it's no larger than a side, that's a copy at its own size, named for the smallest of SHRINK_SIDES
    it fits (so made once), or for a JPEG the texture itself.

    Returns:
        Its larger side, and the copy for each of sides.
    """
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None    # these are our own files, and the large ones are why we're here

    def name(side: int) -> Path:
        return path.parent / "smaller" / f"{path.stem}-{side}.jpg"
    with Image.open(path) as im:     # only its header, until it's converted
        size = max(im.size)
        fit = next((s for s in SHRINK_SIDES if s >= size), None)
        jpeg = path.suffix.lower() in (".jpg", ".jpeg")
        copies = {s: path if size <= s and jpeg else name(s if size > s or fit is None else fit) for s in sides}
        todo = sorted({c for c in copies.values() if c != path and not c.exists()}, reverse=True)
        if not todo:
            return size, copies
        rgb = im.convert("RGB")
    todo[0].parent.mkdir(exist_ok=True)
    for out in todo:
        side = int(out.stem.rsplit("-", 1)[1])
        scale = min(1.0, side / size)
        small = rgb if scale == 1 else rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
                                                  Image.LANCZOS, reducing_gap=3.0)
        partial = out.with_suffix(".partial")
        small.save(partial, "JPEG", quality=90)
        partial.replace(out)
    return size, copies


def shrunk_texture(path: Path, side: int) -> Path:
    """A JPEG copy of a texture at most side pixels wide and high (shrink_texture)."""
    return shrink_texture(path, [side])[1][side]


# ------------------------------------------------------------------ packed copies for the viewer (glbpack.py)
_packs: "queue.PriorityQueue[tuple[int, float, str]]" = queue.PriorityQueue()
_pack_lock = threading.Lock()
_packing: dict[str, int] = {}          # meshes queued, and how soon: 0 asked for by a viewer or new, 1 catching up
_pack_failed: dict[str, list] = {}     # meshes whose packing failed, and the version of the GLB that failed
_pack_now: dict = {}                   # the mesh being packed now, and since when
_pack_took: list[float] = []           # how long the last few meshes packed took, in seconds


def viewer_glb(mid: str, mode: str, filename: str, make: bool = False) -> Path | None:
    """The GLB the viewer is sent for a mesh, as its texture is shown (texture_mode); None if it has none. make: if
    that's its untextured copy, made if it isn't yet."""
    if not filename.lower().endswith(".glb"):
        return None
    f = served_dir(mid, mode) / filename
    if not f.is_file():
        return None
    if mode == "hidden":
        untextured = f.parent / f"{f.stem}.untextured.glb"
        return meshclean.strip_textures(f, untextured) if make else untextured if untextured.is_file() else None
    return f


def queue_pack(mid: str, urgent: bool = False) -> None:
    """Pack a mesh for the viewer: first, if urgent (a new one, or one a viewer asked for)."""
    if not glbpack.available():
        return
    soon = 0 if urgent else 1
    with _pack_lock:
        if _packing.get(mid, 2) <= soon:
            return
        _packing[mid] = soon
    _packs.put((soon, time.time(), mid))


def pack_mesh(mid: str) -> bool:
    """A mesh's packed copies (glbpack.py) and its textures' JPEG copies (shrink_texture), as it's shown now, unless
    they're made already, or it isn't a GLB. Whether it made them."""
    m = q("SELECT source, filename FROM meshes WHERE id=?", mid, one=True)
    if not m:
        return False
    mode = texture_mode(mid, m["source"])
    src = viewer_glb(mid, mode, m["filename"], make=True)
    preview_faces = settings().get("viewer.preview_faces")
    rec = glbpack.record(src) if src else None
    if src is None or (rec and glbpack.made_for(rec, preview_faces)) or _pack_failed.get(mid) == glbpack.stamp(src):
        return False
    textures = {}
    if mode != "hidden" and _has("PIL"):
        for uri in glbpack.image_files(src):
            f = src.parent / uri
            if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg"):
                size, copies = shrink_texture(f)
                textures[uri] = {"side": size, "bytes": {str(s): c.stat().st_size for s, c in copies.items()}}
    glbpack.pack(src, lambda line: print(f"{mid}: {line}", flush=True), extra={"textures": textures}, preview_faces=preview_faces)
    return True


def pack_worker() -> None:
    """Packs meshes one at a time, on a thread of its own at the lowest CPU priority (glbpack.mjs runs niced too)."""
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), BETA_NICE)
    while True:
        _, _, mid = _packs.get()
        with _pack_lock:
            if _packing.pop(mid, None) is None:
                continue     # queued again sooner, and done then
        use_processing_cores()
        _pack_now.update(mid=mid, since=time.time())
        try:
            if pack_mesh(mid):
                _pack_took[:] = [*_pack_took[-19:], time.time() - _pack_now["since"]]
        except Exception as e:  # never let the thread die
            print(f"{mid}: packing for viewing failed: {type(e).__name__}: {e}", flush=True)
            try:
                m = q("SELECT source, filename FROM meshes WHERE id=?", mid, one=True)
                src = m and viewer_glb(mid, texture_mode(mid, m["source"]), m["filename"])
                if src:
                    _pack_failed[mid] = glbpack.stamp(src)
            except Exception:
                pass
        finally:
            _pack_now.clear()


def pack_progress() -> dict:
    """How far packing meshes for the viewer has got, for the admin page: of every GLB mesh, how many are packed as the
    admin page asks now, which is being packed, and roughly how long the rest will take. Those that aren't are queued,
    as the meshes list does."""
    want, total, done, todo = settings().get("viewer.preview_faces"), 0, 0, []
    for m in q("SELECT id, source, filename FROM meshes ORDER BY created DESC"):
        if not m["filename"].lower().endswith(".glb"):
            continue
        total += 1
        src = viewer_glb(m["id"], texture_mode(m["id"], m["source"]), m["filename"])
        rec = glbpack.record(src) if src else None
        if rec and glbpack.made_for(rec, want):
            done += 1
        elif m["id"] not in _pack_failed:
            todo.append(m["id"])
    for mid in todo:
        queue_pack(mid)
    now = dict(_pack_now)
    name = now and (q("SELECT name FROM meshes WHERE id=?", now["mid"], one=True) or {}).get("name")
    going = time.time() - now["since"] if now else 0
    each = sorted(_pack_took)[len(_pack_took) // 2] if _pack_took else None     # the median of the last 20
    return {"available": glbpack.available(), "total": total, "done": done, "left": len(todo), "failed": len(_pack_failed),
            "now": {"name": name, "seconds": round(going)} if now else None,
            "eta_seconds": round(max(each, each * len(todo) - going)) if each and todo else None, "preview_faces": want}


# ------------------------------------------------------------------ which way meshes face, and those nearby (heading.py)
NEARBY_MOST = 40                  # the most meshes Nearby lists, nearest first
TERRAIN = DATA / "cache" / "terrain"    # squares of elevation around meshes (terrain.py)
NEARBY_RADIUS = (5.0, 1000.0)     # the radius it may be asked for, in metres


def heading_inputs(mid: str) -> tuple[Path | None, list[dict]]:
    """What a mesh's heading is worked out from: its project's photos, and its climbs with their text, each with
    whether the mesh is that climb's own (made from its photos alone, as a climb's harvest's mesh is)."""
    m = q("SELECT source, boulder FROM meshes WHERE id=?", mid, one=True)
    if not m:
        return None, []
    photos = PROJECTS / m["source"].split(":", 1)[1] / "photos" if m["source"].startswith("project:") else None
    if m["boulder"]:
        rows, own = boulder_climbs(m["boulder"]), False
    else:
        rows = q("SELECT c.id, c.name, c.data FROM mesh_links l JOIN climbs c ON c.id=l.climb_id WHERE l.mesh_id=? "
                 "AND (l.note IS NULL OR l.note NOT LIKE ?)", mid, SHARED + "%")
        own = len(rows) == 1
    climbs = []
    for r in rows:
        try:
            data = json.loads(r.get("data") or "{}")
        except ValueError:
            data = {}
        # its own words first, then the area it's in, which Mountain Project often names by a face ("... - Southeast Face")
        area = (data.get("area_path") or [""])[-1] if isinstance(data.get("area_path"), list) else ""
        parts = [str(data[k]) for k in ("description", "location") if data.get(k) not in (None, "", "None")]
        climbs.append({"id": r["id"], "name": r["name"], "text": "\n".join(parts + ([f"In {area}."] if area else [])), "own": own})
    return photos, climbs


def keep_heading(mid: str, log=None) -> dict | None:
    """Work out which way a mesh faces (heading.py), keeping any edit by hand; the best of it. Only costs that if
    it fails."""
    try:
        photos, climbs = heading_inputs(mid)
        found = heading_mod.best(heading_mod.compute(MESHES / mid, photos, climbs))
        if log and found:
            log(f"facing: its -Z axis {found['deg']:.0f}° from north, from {found['detail']}")
        return found
    except Exception as e:
        (log or print)(f"{mid}: working out which way it faces failed ({type(e).__name__}: {e})")
        return None


def mesh_heading(mid: str) -> dict | None:
    """Which way a mesh faces, as best known ({deg, source, detail}), worked out first if it hasn't been since
    heading.py last changed; None if not known."""
    d = MESHES / mid
    if not d.is_dir():
        return None
    return heading_mod.best(heading_mod.load(d)) if heading_mod.current(d) else keep_heading(mid)


def heading_existing_meshes() -> None:
    """Work out which way meshes from before heading.py (or a change to it) face, newest first."""
    use_processing_cores()
    for m in q("SELECT id FROM meshes WHERE replaced_by IS NULL ORDER BY created DESC"):
        if (MESHES / m["id"]).is_dir() and not heading_mod.current(MESHES / m["id"]):
            keep_heading(m["id"])
            time.sleep(0.05)


# which way meshes face from their photos' skyline (skyline.py): in the background, one at a time, at the lowest CPU
# priority, once a mesh's size is known (its parallax check is in metres); most stop at its place's horizon, in a second
_skylines: "queue.Queue[str]" = queue.Queue()
_skyline_lock = threading.Lock()
_skyline_queued: set[str] = set()
_skyline_now: dict = {}


def queue_skyline(mid: str) -> None:
    """Look at a mesh's photos' skyline, if the admin page has that on (facing.skyline)."""
    if not settings().get("facing.skyline"):
        return
    with _skyline_lock:
        if mid in _skyline_queued:
            return
        _skyline_queued.add(mid)
    _skylines.put(mid)


def skyline_mesh(mid: str) -> None:
    """Which way a mesh faces from its photos' skyline, kept in its heading.json whether found or not."""
    m = q("SELECT source, lat, lng FROM meshes WHERE id=?", mid, one=True)
    if not m or not m["source"].startswith("project:") or m["lat"] is None or not (MESHES / mid).is_dir():
        return
    pid = m["source"].split(":", 1)[1]
    if photos_away(pid):
        heading_mod.set_skyline(MESHES / mid, None, {"version": skyline_mod.VERSION, "at": time.time(), "retry": True,
                                                     "why": "its photos are in the backup"})
        return
    found, note = skyline_mod.check(MESHES / mid, PROJECTS / pid / "photos", m["lat"], m["lng"], metres_per_unit(mid),
                                    DATA / "cache", log=lambda line: print(f"{mid}: {line}", flush=True))
    heading_mod.set_skyline(MESHES / mid, found, note)
    print(f"{mid}: skyline: " + (f"faces {found['deg']:.1f}°" if found else note["why"]) + f" ({note['seconds']} s)", flush=True)


def skyline_worker() -> None:
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), BETA_NICE)
    while True:
        mid = _skylines.get()
        with _skyline_lock:
            _skyline_queued.discard(mid)
        if not settings().get("facing.skyline"):
            continue
        use_processing_cores()
        _skyline_now.update(mid=mid, since=time.time())
        try:
            skyline_mesh(mid)
        except Exception as e:  # never let the thread die
            print(f"{mid}: looking at its skyline failed: {type(e).__name__}: {e}", flush=True)
        finally:
            _skyline_now.clear()


def skyline_wanted(mid: str) -> bool:
    """Whether a mesh's skyline is still to be looked at: never, not since skyline.py changed, or it's worth trying
    again (its size wasn't known then, or elevation data couldn't be had). Only once its size is known."""
    note = (heading_mod.load(MESHES / mid) or {}).get("skyline_note") or {}
    return bool(metres_per_unit(mid)) and (note.get("version") != skyline_mod.VERSION or bool(note.get("retry")))


def skyline_existing_meshes() -> None:
    """Queue the meshes whose skyline is still to be looked at, newest first."""
    if not settings().get("facing.skyline"):
        return
    for m in q("SELECT id FROM meshes WHERE replaced_by IS NULL AND source LIKE 'project:%' ORDER BY created DESC"):
        if (MESHES / m["id"]).is_dir() and skyline_wanted(m["id"]):
            queue_skyline(m["id"])


def skyline_progress() -> dict:
    """For the admin page: of the current meshes made here, how many have had their skyline looked at, how many it
    gave a facing for, and what's being looked at now."""
    total = checked = found = 0
    for m in q("SELECT id FROM meshes WHERE replaced_by IS NULL AND source LIKE 'project:%'"):
        record = heading_mod.load(MESHES / m["id"]) or {}
        note = record.get("skyline_note") or {}
        total += 1
        checked += note.get("version") == skyline_mod.VERSION and not note.get("retry")
        found += bool(record.get("skyline"))
    now = dict(_skyline_now)
    name = now and (q("SELECT name FROM meshes WHERE id=?", now["mid"], one=True) or {}).get("name")
    return {"on": bool(settings().get("facing.skyline")), "total": total, "checked": checked, "found": found,
            "queued": _skylines.qsize(), "now": {"name": name, "seconds": round(time.time() - now["since"])} if now else None}


def metres_per_unit(mid: str) -> float | None:
    """How many metres one of a mesh's units is, if its size was found (scale.py)."""
    rec = scale_mod.load(MESHES / mid) or {}
    v = rec.get("metres_per_unit") or (rec.get("previous") or {}).get("metres_per_unit")     # the last found, while found again
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def nearby(mid: str, radius: float) -> dict | None:
    """The meshes within radius metres of one, by their coordinates, nearest first, each with what the viewer needs
    to place it around this one: how far east and north, its size in metres, which way it faces, and its files.
    None if there's no such mesh."""
    me = q("SELECT * FROM meshes WHERE id=?", mid, one=True)
    if not me:
        return None
    out = {"radius": radius, "self": {"id": mid, "lat": me["lat"], "lng": me["lng"], "heading": mesh_heading(mid),
                                      "metres_per_unit": metres_per_unit(mid)}, "meshes": [], "more": 0}
    if me["lat"] is None or me["lng"] is None:
        return out
    kx, ky = 111320.0 * math.cos(math.radians(me["lat"])), 110574.0     # metres a degree, east and north, here
    found = []
    for r in q("SELECT * FROM meshes WHERE replaced_by IS NULL AND id != ? AND lat IS NOT NULL AND lng IS NOT NULL", mid):
        east, north = (r["lng"] - me["lng"]) * kx, (r["lat"] - me["lat"]) * ky
        if math.hypot(east, north) <= radius:
            found.append((math.hypot(east, north), east, north, r))
    found.sort(key=lambda f: f[0])
    for d, east, north, r in found[:NEARBY_MOST]:
        r["texture"] = texture_mode(r["id"], r["source"])
        out["meshes"].append({"id": r["id"], "name": r["name"], "filename": r["filename"], "texture": r["texture"],
                              "distance_m": round(d, 1), "east_m": round(east, 2), "north_m": round(north, 2),
                              "metres_per_unit": metres_per_unit(r["id"]), "heading": mesh_heading(r["id"]),
                              "pack": pack_info(r), "v": mesh_version(r["id"], r["texture"], r["filename"])})
    out["more"] = max(0, len(found) - NEARBY_MOST)
    return out


def mesh_version(mid: str, mode: str, filename: str) -> str:
    """A token for what the viewer is sent for a mesh now, which changes whenever any of it would: how its texture is
    shown, the file it's sent, its packed copies, its cleanup's pieces of each face, its splat."""
    src = viewer_glb(mid, mode, filename) or served_dir(mid, mode) / filename
    parts = [mode]
    for f in (src, src.parent / meshclean.FACES_FILE, MESHES / mid / splat_mod.FILE):
        try:
            st = f.stat()
            parts.append(f"{st.st_size}.{st.st_mtime_ns}")
        except OSError:
            parts.append("-")
    rec = glbpack.record(src) if src.suffix.lower() == ".glb" else None
    parts.append(str(rec["made"]) if rec else "-")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def pack_info(r: dict) -> dict | None:
    """What the viewer can fetch for a listed mesh instead of its own files: the sizes of its packed copies, and of
    its textures' JPEG copies; None if it isn't packed. One not packed, or not for the previews the admin page asks
    for now, is queued (shown as it was meanwhile)."""
    src = viewer_glb(r["id"], r["texture"], r["filename"])
    rec = glbpack.record(src) if src else None
    if (not rec or not glbpack.made_for(rec, settings().get("viewer.preview_faces"))) \
            and r["filename"].lower().endswith(".glb") and r["id"] not in _pack_failed:
        queue_pack(r["id"])
    if not rec:
        return None
    sides: dict[str, int] = {}
    for t in (rec.get("textures") or {}).values():
        for s, n in t["bytes"].items():
            sides[s] = sides.get(s, 0) + n
    textures = rec.get("textures") or {}
    return {"full": rec["full"]["bytes"], "preview": (rec.get("preview") or {}).get("bytes"),
            "texture": {"side": max(t["side"] for t in textures.values()), "bytes": sides} if textures else None}


# ------------------------------------------------------------------ worker
jobs: "queue.Queue[dict]" = queue.Queue()


def queue_job(job: dict) -> None:
    """Queue a job someone asked for, keeping a project's with the project, to run again as it was asked for
    if a restart interrupts it, or to queue again after a restart if it hadn't started (requeue_waiting())."""
    if job.get("pid"):
        q("UPDATE projects SET job=?, job_at=? WHERE id=?", json.dumps(job), time.time(), job["pid"], write=True)
    jobs.put(job)


def requeue_waiting() -> int:
    """As the server starts: the jobs people asked for that a restart found still queued (init_db() leaves their
    projects queued), queued again in the order they were. Returns how many."""
    with jobs.mutex:
        there = {j.get("pid") for j in jobs.queue}
    n = 0
    for r in q("SELECT id, job FROM projects WHERE status='queued' AND job IS NOT NULL ORDER BY COALESCE(job_at, created)"):
        if r["id"] in there:
            continue
        try:
            jobs.put(json.loads(r["job"]))
            n += 1
        except ValueError:
            q("UPDATE projects SET status='interrupted' WHERE id=?", r["id"], write=True)
    return n


def append_log(pid: str, line: str) -> None:
    # a file: appending to the database column rewrites the whole log per line, which throttles
    # a chatty COLMAP once the log reaches megabytes
    d = PROJECTS / pid
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "job.log", "a") as f:
        f.write(line + "\n")


def read_log(pid: str, older: str = "", tail: int = 64_000) -> str:
    """The end of a project's log, after what's in its log column (old jobs, server restarts)."""
    f = PROJECTS / pid / "job.log"
    if not f.exists():
        return older[-tail:]
    with open(f, "rb") as fh:
        fh.seek(max(0, f.stat().st_size - tail))
        return (older[-tail:] + fh.read().decode("utf-8", "replace"))


def tracker_for(pid: str, steps: list[tuple[str, str, float]], follow: Callable[[dict], None] | None = None) -> Tracker:
    """A project's job's tracker, saving its progress with the project, and giving it to follow if given (as a snapshot).
    Each step it finishes is kept in step_runs too (keep_step_runs())."""
    kept: set = set()

    def save(snapshot: str) -> None:
        q("UPDATE projects SET progress=? WHERE id=?", snapshot, pid, write=True)
        snap = json.loads(snapshot)
        keep_step_runs(pid, snap, kept)
        if follow:
            follow(snap)
    return Tracker(steps, save, where="server")


def project_units(pid: str) -> int | None:
    """How much a project's steps have to do, for their times (steptimes.py): its photos, as found by its harvest if
    it's been harvested, else in its folder now (None if there are none yet, as a boulder's before it pools them)."""
    p = q("SELECT harvest FROM projects WHERE id=?", pid, one=True)
    try:
        n = json.loads(p["harvest"]).get("images") if p and p["harvest"] else None
    except (TypeError, ValueError, AttributeError):
        n = None
    if n is None:
        photos = PROJECTS / pid / "photos"
        n = sum(1 for f in photos.iterdir() if f.suffix.lower() in PHOTO_EXT) if photos.is_dir() else 0
    return n or None


def step_machine(pid: str, where: str | None) -> str | None:
    """Where a project's step ran, for step_runs: "server", or the name of the machine on its job ("machine" if that's
    gone)."""
    if where != "machine":
        return "server" if where in (None, "server", "server+volunteers") else where
    return next((j["machine"] for j in MACHINES.running() if j["label"] == pid and j["machine"]), "machine")


def keep_step_runs(pid: str, snap: dict, kept: set) -> None:
    """Keep each step of a tracker's snapshot that's newly finished in step_runs (steptimes.py); kept holds those kept
    already. Only costs that step's time if it fails."""
    try:
        new = [st for st in snap.get("steps", []) if st.get("state") == "done" and st.get("seconds") is not None
               and (st["key"], st.get("started")) not in kept]
        if not new:
            return
        kind = (q("SELECT kind FROM projects WHERE id=?", pid, one=True) or {}).get("kind")
        units = project_units(pid)
        for st in new:
            kept.add((st["key"], st.get("started")))
            r = steptimes_mod.row(pid, st, kind, step_machine(pid, st.get("where")), units)
            if r:
                q("INSERT OR IGNORE INTO step_runs VALUES(?,?,?,?,?,?,?)", *r, write=True)
        _typicals["at"] = 0.0
    except Exception as e:
        print(f"step_runs: not kept ({type(e).__name__}: {e})", flush=True)


def boulder_place(boulder: str | None) -> tuple[float | None, float | None]:
    """Where a boulder is: the middle of its climbs' coordinates, or nowhere yet."""
    import statistics
    ll = [(c["lat"], c["lng"]) for c in boulder_climbs(boulder) if c["lat"] is not None and c["lng"] is not None] if boulder else []
    return (statistics.median(x[0] for x in ll), statistics.median(x[1] for x in ll)) if ll else (None, None)


CLIMBS_FILE = "climbs.json"   # in a boulder's project: its climbs, the harvests pooled from each, and the photos of their own


def placed_photos(mid: str) -> list[str] | None:
    """The photos a mesh's reconstruction placed, by name (its cameras.json), or None if it kept none."""
    try:
        return [c["name"] for c in json.loads((MESHES / mid / "cameras.json").read_text())]
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ------------------------------------------------------------------ the rest: what a boulder's mesh left out
# A boulder's mesh is made from every photo its climbs' harvests kept (run_boulder()), and its reconstruction places what
# it can, then tries the rest against the finished model (pipeline._place_rest()). What it still leaves out may be another
# side no photo joins, another rock under the same Mountain Project boulder, or somewhere else: a video's walk in, or
# another boulder it filmed. COLMAP's mapper makes a model of each set of those photos that fit together, and the
# reconstruction records each, with how the photos the mesh left out are tied to those it placed (pipeline.write_models()).
# A model is a scene of the boulder's to mesh apart (rest_models()) when it has REST_LEAST photos the mesh left out and is
# tied to the boulder: it holds the Mountain Project page photo of one of its climbs, or LINK_MIN_PAIRS of its photos
# share pipeline.TIE_LEAST verified matches with photos the mesh placed (the bars the off-topic filter and linking climbs
# use). Each such scene is meshed once, apart (run_rest()), with boulders.rest_auto or from the admin page. A boulder's
# mesh with a scene waiting or meshed isn't one boulder (pooled_summary()). Meshes made before the ties were recorded go
# by the climbs that left REST_LEAST or more out instead, whose left-out photos are meshed together, once.
REST_FILE = "rest.json"     # beside a boulder's mesh: {created, mesh, climbs: {id: {name, left, own}}, models: [{name, photos,
                            # placed, tied, page, counts, why, climbs, attempt}]}; before ties, apart and attempt for models
REST_LEAST = pipeline.MIN_REGISTERED    # photos left out, at least, to mesh apart: what a mesh needs placed
REST_MARK = POOL_MARK + "rest "     # the last of the key of a pool of what a boulder's mesh left out: that mesh's id, and the
                                    # name of its model (none, before models were tied)
REST_LINK = "meshed from the photos its boulder's mesh left out"   # a mesh_links note: a rest mesh, to each climb it places
REST_TRIED = ("failed", "poor", "deleted")  # attempts that were tried and made nothing (or, of an older version of the
                                            # boulder's mesh, what they made was deleted)
REST_BUSY = ("queued", "running")
_rest_lock = threading.RLock()      # rest.json is read, changed and written again by jobs and requests


def is_rest(key: str | None) -> bool:
    try:
        return bool(key) and str(json.loads(key)[-1]).startswith(REST_MARK)
    except (TypeError, ValueError, IndexError):
        return False


def rest_ref(key: str) -> tuple[str, str | None]:
    """The boulder's mesh whose left-out photos a rest pool's key is of, and the model of them it meshes (None before
    models were tied: its climbs' left-out photos)."""
    mid, _, model = str(json.loads(key)[-1])[len(REST_MARK):].partition(" ")
    return mid, model or None


def rest_parent(key: str) -> str:
    """The boulder's mesh whose left-out photos a rest pool's key is of."""
    return rest_ref(key)[0]


def load_rest(mid: str) -> dict | None:
    try:
        return json.loads((MESHES / mid / REST_FILE).read_text())
    except (OSError, ValueError):
        return None


def save_rest(mid: str, record: dict) -> None:
    tmp = MESHES / mid / (REST_FILE + ".partial")
    tmp.write_text(json.dumps(record))
    tmp.replace(MESHES / mid / REST_FILE)


def rest_attempts(rec: dict) -> list[dict]:
    """What of a rest record is to be meshed apart, each with its attempt: the scenes that count, or before models were
    tied, the record itself if any climb left enough out."""
    if "models" in rec:
        return [m for m in rec["models"] if m.get("counts")]
    return [rec] if rec.get("apart") else []


def rest_state(attempt: dict | None) -> str | None:
    """How meshing a scene of what a boulder's mesh left out stands: its attempt's status, or "interrupted" where that
    says queued or running and its project isn't now (a restart or a stop cut it short). None if never tried."""
    status = (attempt or {}).get("status")
    if status in REST_BUSY:
        p = q("SELECT status FROM projects WHERE id=?", attempt.get("project"), one=True)
        if not p or p["status"] not in ("queued", "running", "harvesting"):
            return "interrupted"
    return status


def rest_due(attempt: dict | None) -> bool:
    """Whether meshing it is to be queued: never tried, or cut short of an outcome (a restart, no machine, an error)."""
    return rest_state(attempt) not in REST_TRIED + REST_BUSY + ("done",)


def rest_summary(rec: dict) -> tuple[int, str | None, bool, bool]:
    """What a boulder's mesh's rest record says for its card (pooled_summary()): how many scenes (before ties, climbs)
    it left out to mesh apart; how that stands ("running" or "queued" if any is, None while any waits to be, else "done"
    if any made a mesh, or "failed"); whether any made a mesh; and whether that keeps it from being one boulder (any
    waiting, or made)."""
    states = [rest_state(x.get("attempt")) for x in rest_attempts(rec)]
    made = "done" in states
    blocks = any(s not in REST_TRIED for s in states)
    state = ("running" if "running" in states else "queued" if "queued" in states else
             None if any(s not in REST_TRIED + ("done",) for s in states) else "done" if made else "failed" if states else None)
    return len(states) if "models" in rec else len(rec.get("apart") or []), state, made, blocks


def rest_models(models: dict, placed: set[str], own: dict[str, set[str]], whose: dict[str, set[str]],
                names: dict[str, str]) -> list[dict]:
    """Each model COLMAP made of a boulder's photos other than the one its mesh is made from (pipeline.write_models()),
    largest first, with its photos the mesh didn't place, and whether it counts as a scene of the boulder to mesh apart:
    REST_LEAST of those or more, not in a larger one that counts, and tied to the boulder, by holding the page photo of
    one of its climbs or LINK_MIN_PAIRS of them sharing pipeline.TIE_LEAST verified matches with photos the mesh placed.

    Args:
        models: models.json, with the ties.
        placed: the photos the mesh placed.
        own: {climb: its own photos}, as the boulder's climbs.json has them.
        whose: {photo: the climbs whose harvests had it}.
        names: {climb: its name}.

    Returns:
        [{name, photos, placed (by the model), tied (how many of photos), page (photos), counts, why, climbs}]: climbs,
        those whose own photos are in it, or without any, those whose harvests had its photos.
    """
    from climbs.relevance import is_frame
    ties = {n: int((v or {}).get("inliers") or 0) for n, v in (models.get("left") or {}).items()}
    pages: dict[str, set[str]] = {}
    for cid, photos in own.items():
        for n in photos:
            if not is_frame(Path(n)):
                pages.setdefault(n, set()).add(cid)
    out: list[dict] = []
    claimed: set[str] = set()
    for m in models.get("models") or []:
        if m.get("name") == models.get("main"):
            continue
        photos = [n for n in m["photos"] if n not in placed and n not in claimed]
        page = [n for n in photos if n in pages]
        tied = sum(ties.get(n, 0) >= pipeline.TIE_LEAST for n in photos)
        climbs = sorted(c for c, mine in own.items() if mine & set(photos)) or sorted({c for n in photos for c in whose.get(n, ())})
        counts = False
        if len(photos) < REST_LEAST:
            why = (f"{plural(len(photos), 'photo')} the boulder's mesh didn't place" + (", the rest being in a larger one"
                   if len(photos) < len([n for n in m["photos"] if n not in placed]) else "") + f": a mesh needs {REST_LEAST}")
        elif page:
            counts, why = True, "it holds a page photo of " + climbs_text({c for n in page for c in pages[n]}, names)
        elif tied >= relevance_mod.LINK_MIN_PAIRS:
            counts, why = True, f"{tied} of its photos share {pipeline.TIE_LEAST}+ verified matches with photos the boulder's mesh placed"
        else:
            why = (f"not tied to the boulder: no climb's page photo, and {tied} of its photos share {pipeline.TIE_LEAST}+ verified "
                   f"matches with photos the boulder's mesh placed (it needs {relevance_mod.LINK_MIN_PAIRS})")
        if counts:
            claimed |= set(photos)
        out.append({"name": m["name"], "photos": photos, "placed": len(m["photos"]), "tied": tied, "page": page, "counts": counts,
                    "why": why, "climbs": climbs})
    return out


def climbs_text(ids, names: dict[str, str]) -> str:
    """Climbs' names from names, as a list in a sentence."""
    got = sorted(names.get(c, c) for c in ids)
    return ", ".join(got[:-1]) + (" and " if len(got) > 1 else "") + got[-1] if got else "none"


def rest_of(mid: str, pid: str) -> dict:
    """What a boulder's mesh left out of each of its climbs' photos: of the photos it was made from, those it didn't
    place, by the climbs whose harvests had them (sources.json's pooled_from and also_in), with how many of each's are
    its own (its page's, and its videos' frames tied to photos it placed: viewpoints.own_by_evidence()). Where its
    reconstruction recorded the ties (models.json), also each other model COLMAP made of its photos, and whether it's a
    scene to mesh apart (rest_models()); otherwise the climbs with REST_LEAST or more left out, to mesh apart.

    Raises:
        ValueError: what it placed, or was made from, can't be told: without the ties, it kept no cameras, or its photos
            are in the backup.
    """
    models = pipeline.load_models(MESHES / mid) or {}
    main = next((m for m in models.get("models") or [] if m.get("name") == models.get("main")), None)
    if main is not None and isinstance(models.get("left"), dict):
        placed = set(main["photos"])
        given = placed | set(models["left"])
        ties = {n: int((v or {}).get("inliers") or 0) for n, v in models["left"].items()}
    else:
        kept = placed_photos(mid)
        if kept is None:
            raise ValueError("it kept no record of the photos it placed (cameras.json)")
        if photos_away(pid):
            raise ValueError("its photos are in the backup, so what it was made from can't be listed")
        placed, ties = set(kept), None
        photos = PROJECTS / pid / "photos"
        given = {f.name for f in photos.iterdir()} if photos.is_dir() else set()
    climbs = json.loads((PROJECTS / pid / CLIMBS_FILE).read_text())["climbs"]
    by_harvest: dict[str, set] = {}
    for c in climbs:
        for h in c.get("projects") or []:
            by_harvest.setdefault(h, set()).add(c["id"])
    try:
        files = json.loads((PROJECTS / pid / "sources.json").read_text()).get("files") or []
    except (OSError, ValueError):
        files = []
    whose: dict[str, set] = {}
    for f in files:
        if f.get("file") in given:
            for h in [f.get("pooled_from")] + list(f.get("also_in") or []):
                whose.setdefault(f["file"], set()).update(by_harvest.get(h, ()))
    left: dict[str, set] = {}
    for name, cids in whose.items():
        if name not in placed:
            for cid in cids:
                left.setdefault(cid, set()).add(name)
    names = {c["id"]: c["name"] for c in climbs}
    own = lambda v: len(viewpoints.own_by_evidence({n for n in v if relevance_mod.is_trusted(Path(n))}, placed, ties))  # noqa: E731
    out = {cid: {"name": names[cid], "left": sorted(v), "own": own(v)} for cid, v in left.items()}
    record = {"created": time.time(), "mesh": mid, "climbs": out}
    if ties is not None:
        record["models"] = rest_models(models, placed, {c["id"]: set(c.get("own") or []) for c in climbs}, whose, names)
    else:
        record.update(apart=sorted((cid for cid, v in out.items() if len(v["left"]) >= REST_LEAST), key=lambda c: names[c]),
                      attempt=None)
    return record


def queue_rest(mid: str, leaf: str, model: str | None, members: list[str], resume: bool = False) -> dict:
    """Queue meshing a scene of what a boulder's mesh left out, its model (None, before models were tied: these climbs'
    left-out photos), in a pool and project of its own (run_rest()), unless it's queued or running. Returns its attempt.

    Args:
        resume: carry on from where an attempt before got to.
    """
    key = json.dumps(json.loads(leaf) + [REST_MARK + mid + (f" {model}" if model else "")])
    q("INSERT INTO pools(key, leaf, members, created, updated) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
      "members=excluded.members, updated=excluded.updated", key, leaf, json.dumps(sorted(members)), time.time(), time.time(), write=True)
    rpid = boulder_project_made(key)
    if q("SELECT status FROM projects WHERE id=?", rpid, one=True)["status"] not in ("queued", "running", "harvesting"):
        q("UPDATE projects SET status='queued', progress=NULL WHERE id=?", rpid, write=True)
        queue_job({"type": "boulder", "pid": rpid, **({"resume": True} if resume else {})})
    return {"project": rpid, "status": "queued", "at": time.time()}


def rest_said(record: dict, queued: int) -> str:
    """What keep_rest() logs of a rest record with models."""
    models = record["models"]
    if not models:
        return "rest: COLMAP made no other model of the photos it left out"
    counted = [m for m in models if m["counts"]]
    each = "; ".join(f"{m['name']} ({plural(len(m['photos']), 'photo')} left out): {'to mesh apart, as ' if m['counts'] else ''}{m['why']}"
                     for m in models)
    return (f"rest: COLMAP made {plural(len(models), 'other model')} of its photos, {len(counted)} of them a scene tied to it to mesh "
            f"apart" + (f", {queued} queued" if queued else "") + f": {each}")


def keep_rest(mid: str, pid: str, log, queue: bool = True, untied: bool = True) -> dict | None:
    """After a boulder's mesh is made (one Mountain Project lists as one area, not a pool): what it left out (rest_of()),
    kept beside it, with how meshing each scene of it went before; and with queue, each scene that counts never tried,
    or cut short, queued to be meshed apart (queue_rest()). Only costs that if it fails.

    Args:
        untied: with queue, also queue what a mesh from before ties were recorded left out (its climbs with REST_LEAST
            or more out), which nothing checks is of a rock its photos see: nothing does any more, as Drifter's made a
            mesh of another rock that way. Meshing the boulder again records its scenes instead.
    """
    try:
        with _rest_lock:
            old = load_rest(mid) or {}
            record = rest_of(mid, pid)
            leaf = q("SELECT boulder FROM meshes WHERE id=?", mid, one=True)["boulder"]
            queued = 0
            if "models" in record:
                had = {m.get("name"): m.get("attempt") for m in old.get("models") or []}
                for m in record["models"]:
                    m["attempt"] = had.get(m["name"])
                    if queue and m["counts"] and rest_due(m["attempt"]):
                        m["attempt"] = queue_rest(mid, leaf, m["name"], m["climbs"], resume=bool(m["attempt"]))
                        queued += 1
                log(rest_said(record, queued))
            else:
                record["attempt"] = old.get("attempt")
                apart = record["apart"]
                if apart and queue and untied and rest_due(record["attempt"]):
                    record["attempt"] = queue_rest(mid, leaf, None, apart, resume=bool(record["attempt"]))
                    names = ", ".join(record["climbs"][c]["name"] for c in apart)
                    log(f"rest: the photos it left out of {plural(len(apart), 'climb')} ({names}) queued to be meshed together")
                elif record["climbs"]:
                    log(f"rest: it left out photos of {plural(len(record['climbs']), 'climb')}" +
                        ("" if apart else ", too few of any to mesh apart"))
            save_rest(mid, record)
        return record
    except Exception as e:
        log(f"rest: couldn't work out what it left out ({type(e).__name__}: {e})")
        return None


def rest_attempt(parent: str, model: str | None, **how) -> None:
    """Record how meshing a scene of what a boulder's mesh left out is going (its model's; None, before models were
    tied, its climbs'), in that mesh's rest record."""
    with _rest_lock:
        rec = load_rest(parent)
        if rec is None:
            return
        target = next((m for m in rec.get("models") or [] if m.get("name") == model), None) if model else rec
        if target is None:
            return
        target["attempt"] = {**(target.get("attempt") or {}), **how, "at": time.time()}
        save_rest(parent, rec)


def log_shares(mid: str, log) -> None:
    """In a boulder's mesh's log: how many of each climb's own photos it places, and which are under the admin page's
    boulders.placed_percent, for choosing that from what meshes make."""
    try:
        views = viewpoints.load(MESHES / mid) or {}
        names = {c["id"]: c["name"] for c in q("SELECT c.id, c.name FROM climbs c JOIN meshes m ON c.area_path=m.boulder "
                                               "WHERE m.id=?", mid)}
        least = settings().get("boulders.placed_percent")
        got = [(names.get(cid, cid), v.get("photos") or 0, v["of"]) for cid, v in (views.get("climbs") or {}).items() if v and v.get("of")]
        got += [(names.get(cid, cid), 0, n) for cid, n in (views.get("unplaced") or {}).items() if n]
        if got:
            said = ", ".join(f"{name} {n}/{of}{' (under)' if n < 1 or 100 * n < least * of else ''}" for name, n, of in
                             sorted(got, key=lambda g: (g[1] / g[2], g[0])))
            log(f"own photos placed, each climb with any: {said}; a boulder needs at least {least:g}% of each, and one" +
                ("; video frames count as a climb's own only where placed, or tied to photos that are" if views.get("evidence") else ""))
    except Exception as e:
        log(f"couldn't say how much of each climb it places ({type(e).__name__}: {e})")


def rest_existing_meshes() -> None:
    """What boulders' meshes made before the rest was kept left out (keep_rest()): kept, not queued, as only their
    cards' boulder or group depends on it; the admin page queues those with any to mesh (rest_queue_all()). Then, with
    boulders.rest_auto, what a restart cut short carries on (rest_resume())."""
    for m in q("SELECT m.id, m.source, m.boulder FROM meshes m WHERE m.boulder IS NOT NULL AND m.replaced_by IS NULL"):
        if is_pool(m["boulder"]) or load_rest(m["id"]) or not (MESHES / m["id"]).is_dir():
            continue
        pid = m["source"].split(":", 1)[1] if m["source"].startswith("project:") else None
        if pid and (PROJECTS / pid / CLIMBS_FILE).exists():
            keep_rest(m["id"], pid, lambda s: None, queue=False)
    rest_resume()


REST_CUT = ("interrupted", "waiting", "stopped")    # cut short by a restart, no machine (or photos in the backup), a stop


def rest_resume() -> int:
    """With boulders.rest_auto, after a restart: meshing each scene of what boulders' meshes left out that was cut short
    (REST_CUT; not an error, which may only happen again) carries on from where it got to. Returns how many."""
    if not settings().get("boulders.rest_auto"):
        return 0
    n = 0
    for m in q("SELECT id, boulder FROM meshes WHERE boulder IS NOT NULL AND replaced_by IS NULL"):
        if is_pool(m["boulder"]):
            continue
        with _rest_lock:
            rec = load_rest(m["id"])
            again = [x for x in rest_attempts(rec or {}) if rest_state(x.get("attempt")) in REST_CUT]
            for x in again:
                model = x.get("name") if "models" in rec else None
                x["attempt"] = queue_rest(m["id"], m["boulder"], model, x["climbs"] if model else rec["apart"], resume=True)
            if again:
                save_rest(m["id"], rec)
                n += len(again)
    return n


def rest_waiting() -> list[str]:
    """Current boulders' meshes with a scene they left out, tied to them, to mesh apart, never tried or cut short. Not
    those from before ties were recorded, whose left-out photos nothing checks are of a rock their photos see (meshing
    the boulder again records them)."""
    out = []
    for m in q("SELECT id, boulder FROM meshes WHERE boulder IS NOT NULL AND replaced_by IS NULL"):
        rec = None if is_pool(m["boulder"]) else load_rest(m["id"])
        if rec and "models" in rec and any(rest_due(x.get("attempt")) for x in rest_attempts(rec)):
            out.append(m["id"])
    return out


def rest_queue_all() -> int:
    """The admin page's: queue meshing what every current boulder's mesh left out, where that was never tried, or was
    cut short. Returns for how many meshes."""
    n = 0
    for mid in rest_waiting():
        m = q("SELECT source FROM meshes WHERE id=?", mid, one=True)
        if m and m["source"].startswith("project:"):
            rec = keep_rest(mid, m["source"].split(":", 1)[1], print, untied=False)
            n += bool(rec and any(rest_state(x.get("attempt")) in REST_BUSY for x in rest_attempts(rec)))
    return n


def untied_meshes() -> list[dict]:
    """Current boulders' meshes (not pools') made before ties were recorded: no rest record, or one without models.
    Meshing them again records their scenes and ties, and counts their climbs' frames by evidence. Not one of a lowest
    area read as holding several boulders, or not saying it's one, whose climbs are meshed on their own and pooled by
    their photos instead (meshing it whole again would only make another of a group). [{id, boulder}]"""
    out, kinds = [], boulder_kinds()
    for m in q("SELECT id, boulder FROM meshes WHERE boulder IS NOT NULL AND replaced_by IS NULL ORDER BY created"):
        rec = None if is_pool(m["boulder"]) else load_rest(m["id"])
        if not is_pool(m["boulder"]) and (rec is None or "models" not in rec) and leaf_kind(m["boulder"], kinds) in (None, "boulder"):
            out.append(m)
    return out


OLD_REPORT_EVERY = 60      # seconds old_mesh_report() and old_climbs() are kept before they're worked out again
_old_report: dict = {"at": 0.0, "report": None}
_old_state: dict = {"at": 0.0, "state": None}
ALREADY = "already %"       # an area's climb done with a mesh it had (run_campaign_item(), settle_boulder()): not tried


def old_changed() -> None:
    """What's old has changed (a mesh made, replaced or deleted): old_climbs() and old_mesh_report() work it out again."""
    _old_state["at"] = _old_report["at"] = 0.0


def mesh_climbs(mid: str, boulder: str | None = None, shared: bool = False) -> set[str]:
    """The climbs a mesh is of: those linked to it as their own (with shared, those linked "via" it too), and for a
    boulder's, a group's or a rest's mesh, those it places (views.json)."""
    out = {r["climb_id"] for r in q("SELECT climb_id FROM mesh_links WHERE mesh_id=?" + ("" if shared else " AND COALESCE(note,'') NOT LIKE ?"),
                                    mid, *([] if shared else [SHARED + "%"]))}
    if boulder:
        out |= set((viewpoints.load(MESHES / mid) or {}).get("climbs") or {})
    return out


def old_climbs(fresh: bool = False) -> dict:
    """Where the climbs of old meshes (MESH_RULES) are, as old_mesh_report() counts them: the climbs of current old
    meshes (linked to one as their own; a boulder's climbs, or a group's, for a boulder's or a group's mesh), and those
    linked only to another climb's mesh, "via" it (SHARED). Each is remade, if a current mesh made under the rules is of
    it (linked to it, or a boulder's or a group's placing it); tried, if under the rules an area finished it (done,
    skipped or failed, but not done with a mesh it already had) or queued it again for its old meshes and finished
    that (queue_old()), or a project of it ran to an end; else still to be tried. Worked out at most every
    OLD_REPORT_EVERY seconds, or when old_changed().

    Returns:
        {old: [current old meshes], made: [current meshes areas and projects made], climbs, via, remade (sets of climb
        ids), tried: {climb id: why}}.
    """
    if not fresh and _old_state["state"] and time.time() - _old_state["at"] < OLD_REPORT_EVERY:
        return _old_state["state"]
    since = MESH_RULES_SINCE[MESH_RULES]
    made = q("SELECT id, source, rules, boulder, old_data FROM meshes m WHERE replaced_by IS NULL AND source LIKE 'project:%'")
    old = [m for m in made if mesh_is_old(m)]
    of_old = {r["climb_id"] for r in q(f"SELECT l.climb_id FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id WHERE m.replaced_by IS NULL "
                                       f"AND {OLD_MESH} AND COALESCE(l.note,'') NOT LIKE ?", SHARED + "%")}
    for m in old:
        if m["boulder"]:
            of_old |= {c["id"] for c in boulder_climbs(m["boulder"])}
    via = {r["climb_id"] for r in q("SELECT DISTINCT climb_id FROM mesh_links WHERE note LIKE ? AND climb_id NOT IN "
                                    "(SELECT climb_id FROM mesh_links WHERE COALESCE(note,'') NOT LIKE ?)", SHARED + "%", SHARED + "%")}
    remade = {r["climb_id"] for r in q("SELECT l.climb_id FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id WHERE m.replaced_by IS NULL "
                                       "AND m.source LIKE 'project:%' AND COALESCE(m.rules, 0)>=? AND COALESCE(l.note,'') NOT LIKE ?",
                                       MESH_RULES, SHARED + "%")}
    for m in made:      # a new boulder's or group's mesh: the climbs it places
        if m["boulder"] and not mesh_is_old(m):
            remade |= set(((viewpoints.load(MESHES / m["id"]) or {}).get("climbs") or {}))
    tried: dict[str, str] = {}
    for r in q("SELECT climb_id, status, note FROM campaign_items WHERE climb_id IS NOT NULL AND status IN ('done','skipped','failed') "
               "AND ((COALESCE(rerun_rules, 0)>=? AND COALESCE(old_rerun, 0)=0) OR (updated>=? AND COALESCE(note,'') NOT LIKE ?)) "
               "ORDER BY updated", MESH_RULES, since, ALREADY):
        tried[r["climb_id"]] = f"{r['status']} in its area since" + (f": {r['note']}" if r["note"] else "")
    for r in q("SELECT climb_id, status, progress FROM projects WHERE climb_id IS NOT NULL AND status IN ('done','failed') AND progress IS NOT NULL"):
        try:
            began = max((st.get("started") or 0 for st in json.loads(r["progress"]).get("steps") or []), default=0)
        except (ValueError, AttributeError):
            continue
        if began >= since and r["climb_id"] not in tried:
            tried[r["climb_id"]] = f"its project {'made a mesh' if r['status'] == 'done' else 'failed'} since, none of it current"
    state = {"old": old, "made": made, "climbs": of_old | via, "via": via, "remade": remade, "tried": tried}
    _old_state.update(at=time.time(), state=state)
    return state


def old_waiting(fresh: bool = False) -> set[str]:
    """Climbs of old meshes, or linked only via another climb's, that no new mesh is of and that haven't been tried under
    the rules (old_climbs()): what areas queue again (queue_old())."""
    st = old_climbs(fresh)
    return {c for c in st["climbs"] if c not in st["remade"] and c not in st["tried"]}


OLD_RERUN_NOTE = "its meshes are old (made before the rules for what an area meshes last changed)"


def queue_old(camp: dict) -> int:
    """Queue again an area's climbs whose only meshes are old, or that are linked only via another climb's mesh, not
    tried under the rules yet (old_waiting()), once for each MESH_RULES, whatever the area's status: at low priority,
    taken only when no area has anything else to take (_claim_old()), and never starting or resuming the area's own
    list. One with a harvest is queued to look in every source for new photos and mesh it again ("new+rebuild",
    whatever it finds; on a boulder meshed whole, harvested for its boulder's mesh, made again once its climbs are); one
    without is harvested and meshed as if new. Returns how many."""
    retry = 0
    waiting = old_waiting()
    if not waiting:
        return 0
    rows = q("SELECT ci.source_id, ci.status, ci.climb_id, ci.project_id, p.status AS pstatus FROM campaign_items ci "
             "LEFT JOIN projects p ON p.id=ci.project_id WHERE ci.campaign_id=? AND ci.status IN ('done','skipped','failed') "
             "AND ci.redo IS NULL AND ci.climb_id IS NOT NULL AND COALESCE(ci.rerun_rules, 0)<? AND ci.blocked IS NULL",
             camp["id"], MESH_RULES)
    n = 0
    for r in rows:
        if r["climb_id"] not in waiting or r["pstatus"] in ("queued", "running", "harvesting"):
            continue
        if r["pstatus"] and (PROJECTS / r["project_id"]).is_dir():
            set_item(camp["id"], r["source_id"], status="pending", retry=retry, redo="new+rebuild", was=r["status"], redo_started=0,
                     rerun_rules=MESH_RULES, old_rerun=1, note=f"queued to {REDO_NAMES['new+rebuild']}: {OLD_RERUN_NOTE}")
        else:
            set_item(camp["id"], r["source_id"], status="pending", retry=retry, redo=None, project_id=None, rerun_rules=MESH_RULES,
                     old_rerun=1, note=f"queued to harvest and mesh: {OLD_RERUN_NOTE}, or another climb's")
        n += 1
    return n


def retire_covered(mid: str, log=print) -> list[str]:
    """After a mesh made under the rules: each current old mesh (MESH_RULES lower) every one of whose climbs it's of
    too (mesh_climbs(), those linked via the old one included) becomes an older version of it, as a rebuild's mesh
    before does. Never one made under the same rules, which old_by_data() may call old instead. Their links to those
    climbs go, as the new mesh is of them (linked to it, or for a boulder's, placing it); to a climb's own mesh they move
    to it, as supersede() moves them. Returns the meshes it replaced."""
    new = q("SELECT id, source, rules, boulder, replaced_by FROM meshes WHERE id=?", mid, one=True)
    if not new or new["replaced_by"] or mesh_is_old(new) or not (new["source"] or "").startswith("project:"):
        return []
    covers = mesh_climbs(mid, new["boulder"])
    if not covers:
        return []
    out = []
    for m in q(f"SELECT id, boulder FROM meshes m WHERE replaced_by IS NULL AND id!=? AND {OLD_MESH}", mid):
        if is_rest(m["boulder"]) and rest_parent(m["boulder"]) == mid:
            continue
        theirs = mesh_climbs(m["id"], m["boulder"], shared=True) or (
            {c["id"] for c in boulder_climbs(m["boulder"])} if m["boulder"] and not is_rest(m["boulder"]) else set())
        if not theirs or not theirs <= covers:
            continue
        if new["boulder"]:
            q("DELETE FROM mesh_links WHERE mesh_id=?", m["id"], write=True)
            q("UPDATE campaign_items SET mesh_id=? WHERE mesh_id=?", mid, m["id"], write=True)
            q("UPDATE meshes SET replaced_by=? WHERE id=? OR replaced_by=?", mid, m["id"], m["id"], write=True)
            for r in q("SELECT id, boulder FROM meshes WHERE boulder LIKE ? AND replaced_by IS NULL", f"%{REST_MARK}%"):
                if is_rest(r["boulder"]) and rest_parent(r["boulder"]) == m["id"]:
                    q("UPDATE meshes SET replaced_by=? WHERE id=?", mid, r["id"], write=True)
                    q("DELETE FROM mesh_links WHERE mesh_id=? AND note=?", r["id"], REST_LINK, write=True)
        else:
            supersede(m["id"], mid)
        out.append(m["id"])
    if out:
        old_changed()
        log(f"old meshes {', '.join(out)}, made before the rules for what an area meshes last changed, of climbs this is of "
            "too: older versions of this now")
    return out


def old_mesh_report(fresh: bool = False) -> dict:
    """How far the meshes are from none old (MESH_RULES), for the admin page: the climbs of old meshes and those linked
    only via another climb's (old_climbs()), each remade; missed, if tried under the rules and yet no new mesh is of it,
    which is to be looked into; else waiting, not tried under them yet (queued again as their areas run: queue_old()).
    Worked out at most every OLD_REPORT_EVERY seconds.

    Old by data (old_by_data()) are counted apart, with the pairs to decide.

    Returns:
        {rules, since, old_meshes, meshes (current ones made by areas and projects), climbs, remade, waiting, via,
        missed: [{id, name, why}], old_data (how many), decide: [{id, name, newer, newer_name, why}]}.
    """
    if not fresh and _old_report["report"] and time.time() - _old_report["at"] < OLD_REPORT_EVERY:
        return _old_report["report"]
    st = old_climbs(fresh)
    climbs, remade, tried = st["climbs"], st["remade"], st["tried"]
    missed_ids = sorted(c for c in climbs if c in tried and c not in remade)
    waiting_ids = [c for c in climbs if c not in tried and c not in remade]
    names = {r["id"]: r["name"] for r in q(f"SELECT id, name FROM climbs WHERE id IN ({','.join('?' * len(missed_ids)) or 'NULL'})", *missed_ids)}
    report = {"rules": MESH_RULES, "since": MESH_RULES_SINCE[MESH_RULES], "old_meshes": len(st["old"]), "meshes": len(st["made"]),
              "climbs": len(climbs), "remade": len(climbs & remade), "waiting": len([c for c in climbs if c not in tried and c not in remade]),
              "via": len(st["via"] - remade), "missed": [{"id": c, "name": names.get(c, c), "why": tried[c]} for c in missed_ids],
              "old_data": len([m for m in st["made"] if m["old_data"] and not mesh_is_old(m)]), "decide": _old_data["decide"],
              "needs_backup": _needs_backup(waiting_ids)}
    _old_report.update(at=time.time(), report=report)
    return report


# ------------------------------------------------------------------ old by data: a newer mesh under the same rules, from more
OLD_DATA_FIELDS = ("meshopts", "filter", "leaf_kind", "pool")   # what else told apart two meshes made from the same photos
_old_data: dict = {"at": 0.0, "decide": [], "flagged": 0}


def _photo_set(mid: str) -> tuple[set[str] | None, str]:
    """A mesh's photos, to compare with another's: by content (SHA-1s, from inputs.json), or, where those weren't kept,
    by name (from inputs.json, else photo_sources.json); and which ("sha1", "name", or "" for neither)."""
    rec = meshinputs.load(MESHES / mid) or {}
    files = (rec.get("photos") or {}).get("files") if isinstance(rec.get("photos"), dict) else None
    if files:
        return {v[1] for v in files.values()}, "sha1"
    src = provenance.load(MESHES / mid)
    if src and src.get("photos"):
        return {p["file"] for p in src["photos"] if p.get("file")}, "name"
    return None, ""


def _compare_made(a: str, b: str) -> tuple[str, str]:
    """Mesh a, against b, a newer one under the same rules of every climb a is of: ("old", why) if b was made from
    photos a lacked (by inputs.json; without it, if a's photos are some of b's); else ("decide", why)."""
    pa, ka = _photo_set(a)
    pb, kb = _photo_set(b)
    if pa is None or pb is None:
        return "decide", "what it, or the newer one, was made from wasn't kept"
    if ka == kb == "sha1":
        if pb - pa:
            return "old", f"the newer one was made from {plural(len(pb - pa), 'photo')} it lacked"
        if pb == pa:
            ra, rb = meshinputs.load(MESHES / a) or {}, meshinputs.load(MESHES / b) or {}
            differ = [f for f in OLD_DATA_FIELDS if ra.get(f) != rb.get(f)]
            return "decide", ("the same photos, with other " + ", ".join(differ) if differ else
                              "the same photos and settings, and a different result")
        return "decide", f"the newer one was made from {plural(len(pa - pb), 'photo')} fewer, and none it lacked"
    if ka != kb:    # one by content and one by name: by name, from what each kept
        pa = set(((meshinputs.load(MESHES / a) or {}).get("photos") or {}).get("files") or {}) or pa if ka == "sha1" else pa
        pb = set(((meshinputs.load(MESHES / b) or {}).get("photos") or {}).get("files") or {}) or pb if kb == "sha1" else pb
    if pa < pb:
        return "old", f"its photos are {len(pa)} of the newer one's {len(pb)} (what they were made from wasn't kept)"
    return "decide", ("the same photos by name" if pa == pb else "its photos aren't some of the newer one's") + \
        " (what they were made from wasn't kept)"


def old_by_data(log=print) -> dict:
    """Which current meshes are old by data: a newer current mesh, made under the same rules (MESH_RULES), is of every climb
    one is of (mesh_climbs()), and was made from photos it lacked (_compare_made()). Kept in meshes.old_data (the newer
    one's id): hidden from visitors' lists, tagged for editors, never made an older version or deleted. A pair that fits
    neither kind of old (the same photos and a different result, say) is to be decided on the admin page
    (decide_old()), and what was decided stands. A mesh old by rules isn't looked at: it's old already.

    Returns:
        {flagged: how many are old by data, decide: [{id, name, newer, newer_name, why}]}.
    """
    rows = [m for m in q("SELECT id, name, source, rules, boulder, created, old_data FROM meshes m WHERE replaced_by IS NULL "
                         "AND source LIKE 'project:%' ORDER BY created") if not mesh_is_old(m)]
    climbs = {m["id"]: mesh_climbs(m["id"], m["boulder"]) for m in rows}
    of: dict[str, list[dict]] = {}
    for m in rows:
        for c in climbs[m["id"]]:
            of.setdefault(c, []).append(m)
    decided = {(r["mesh_id"], r["newer"]): r["verdict"] for r in q("SELECT * FROM old_decisions")}
    flags, decide = {}, []
    for a in rows:
        mine = climbs[a["id"]]
        if not mine:
            continue
        first = next(iter(mine))
        newer = [b for b in of.get(first, []) if b["created"] > a["created"] and (b["rules"] or 0) == (a["rules"] or 0)
                 and mine <= climbs[b["id"]] and not (is_rest(b["boulder"]) and rest_parent(b["boulder"]) == a["id"])]
        asks = []
        for b in sorted(newer, key=lambda b: -b["created"]):
            verdict = decided.get((a["id"], b["id"]))
            why = "decided on the admin page" if verdict else ""
            if not verdict:
                verdict, why = _compare_made(a["id"], b["id"])
            if verdict == "old":
                flags[a["id"]] = b["id"]
                break
            if verdict == "decide":
                asks.append({"id": a["id"], "name": a["name"], "newer": b["id"], "newer_name": b["name"], "why": why})
        else:
            decide += asks
    for m in rows:
        if (m["old_data"] or None) != flags.get(m["id"]):
            q("UPDATE meshes SET old_data=? WHERE id=?", flags.get(m["id"]), m["id"], write=True)
    q("UPDATE meshes SET old_data=NULL WHERE old_data IS NOT NULL AND (replaced_by IS NOT NULL OR source NOT LIKE 'project:%')", write=True)
    _old_data.update(at=time.time(), decide=decide, flagged=len(flags))
    if flags or decide:
        log(f"old by data: {len(flags)} mesh(es) a newer one under the same rules was made from more for; {len(decide)} to decide")
    return {"flagged": len(flags), "decide": decide}


_old_data_lock = threading.Lock()
_old_data_again = threading.Event()


def meshes_changed() -> None:
    """A mesh was made, replaced or deleted: what's old is worked out again (old_changed()), old by data in the
    background, once at a time (a change while it runs has it run once more)."""
    old_changed()
    _old_data_again.set()
    if not _old_data_lock.acquire(blocking=False):
        return

    def run() -> None:
        try:
            while _old_data_again.is_set():
                _old_data_again.clear()
                try:
                    old_by_data()
                except Exception as e:      # only costs that
                    print(f"old by data: failed ({type(e).__name__}: {e})", flush=True)
                old_changed()
        finally:
            _old_data_lock.release()
    threading.Thread(target=run, daemon=True, name="old-by-data").start()


def decide_old(mid: str, newer: str, verdict: str) -> dict:
    """The admin page's answer for a pair old_by_data() couldn't tell: "old" (mid is old by data, as newer covers it) or
    "keep" (both stay current). It stands until either is replaced or deleted."""
    if verdict not in ("old", "keep"):
        raise ValueError("verdict must be old or keep")
    if not q("SELECT 1 FROM meshes WHERE id=?", mid) or not q("SELECT 1 FROM meshes WHERE id=?", newer):
        raise KeyError(mid)
    q("INSERT OR REPLACE INTO old_decisions(mesh_id, newer, verdict, decided) VALUES(?,?,?,?)", mid, newer, verdict, time.time(), write=True)
    return old_by_data()


def _needs_backup(climb_ids) -> int:
    """How many of these climbs have a harvest whose photos are offloaded, to be brought back from the backup before
    they're meshed again (a boulder's climbs: any of them)."""
    ids = list(climb_ids)
    if not ids:
        return 0
    harvests = q(f"SELECT id, climb_id FROM projects WHERE kind='harvest' AND climb_id IN ({','.join('?' * len(ids))})", *ids)
    away = {r["path"] for r in q("SELECT path FROM offloads WHERE done IS NOT NULL AND approved IS NULL AND lost IS NULL "
                                 "AND path LIKE 'projects/%/photos'")}
    return len({h["climb_id"] for h in harvests if f"projects/{h['id']}/photos" in away})


def remesh_untied(log=print) -> dict:
    """The admin page's: mesh again each boulder whose current mesh was made before ties were recorded (untied_meshes()),
    from its climbs' photos as harvested (run_boulder(): only problems its page lists that were never harvested are,
    nothing is fetched again), first reading what its lowest area is where that never was, so that it can be told one
    boulder or not. A lowest area found to hold several boulders, or not to say it's one, isn't meshed as one: its climbs
    go on their own and are pooled by their photos (settle_groups(), settle_clusters()); Gunsmoke Wall's six climbs, read
    so on Nov 12, 2026, were pooled as a group just as its whole mesh was queued again, which would have made a second.
    Returns {queued, groups, busy, unread}."""
    out = {"queued": 0, "groups": 0, "busy": 0, "unread": 0}
    for m in untied_meshes():
        boulder = m["boulder"]
        if leaf_kind(boulder) is None:
            url = None
            for c in q("SELECT data FROM climbs WHERE area_path=? AND data IS NOT NULL", boulder):
                try:
                    url = climb_area_url(Climb.from_dict(json.loads(c["data"])))
                except Exception:
                    url = None
                if url:
                    break
            if boulder_kind(boulder, url, log) is None:
                out["unread"] += 1
        if leaf_kind(boulder) not in (None, "boulder"):
            out["groups"] += 1
            continue
        body, code = mesh_boulder(boulder)
        out["queued" if code == 202 else "busy" if "already" in body.get("error", "") else "groups"] += 1
    return out


# ------------------------------------------------------------------ what each mesh was made from (meshinputs.py)
INPUTS_CHECK = DATA / "inputs-check.json"   # the last dry run's report (inputs_check())
INPUTS_BUDGET = 120     # seconds into a dry run after which it starts no more dedupe or filtering from caches
_inputs_lock = threading.Lock()     # one dry run at a time


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def inputs_kind(proj: dict | None) -> str:
    """What a project's mesh is, for meshinputs: a "boulder"'s, a "pool"'s, a "rest" (what a boulder's mesh left out),
    a "climb"'s own, or "other" (photos uploaded, or its project gone)."""
    if proj and proj["kind"] == "boulder" and proj["boulder"]:
        return "rest" if is_rest(proj["boulder"]) else "pool" if is_pool(proj["boulder"]) else "boulder"
    return "climb" if proj and proj["climb_id"] else "other"


def _climbs_pooled(pdir: Path) -> dict | None:
    try:
        return {c["id"]: sorted(c.get("projects") or []) for c in _read_json(pdir / CLIMBS_FILE)["climbs"]}
    except (TypeError, KeyError):
        return None


def _pooled_names(pdir: Path) -> list[str] | None:
    record = _read_json(pdir / "sources.json")
    return None if record is None else sorted(f["file"] for f in record.get("files") or [] if f.get("pooled_from"))


def current_filter() -> dict:
    """The off-topic filter as this code has it, as meshinputs records one."""
    return {"version": relevance_mod.VERSION, "settings": relevance_mod.settings_key(), "methods": sorted(relevance_mod.METHODS)}


def inputs_record(pid: str, log=None) -> dict | None:
    """What a project's mesh is about to be made from (meshinputs.py), read before it's made; None, said in the log,
    if that fails, which only costs the record."""
    try:
        proj = q("SELECT kind, climb_id, boulder FROM projects WHERE id=?", pid, one=True)
        pdir, kind = PROJECTS / pid, inputs_kind(proj)
        rec = {"project": pid, "kind": kind, "photos": meshinputs.photo_files(meshinputs.photos_in(pdir / "photos")),
               "filter": meshinputs.filter_of(_read_json(pdir / "relevance.json")), "meshopts": meshopts.mapper(),
               "mesh_rules": MESH_RULES, "unknown": {}}
        if kind in ("boulder", "pool"):
            rec["leaf"] = pool_leaf(proj["boulder"])
            rec["leaf_kind"] = leaf_kind(rec["leaf"]) or "unread"
            rec["climbs"], rec["pooled"] = _climbs_pooled(pdir), _pooled_names(pdir)
            if kind == "pool":
                rec["pool"] = sorted(rec["climbs"]) if rec["climbs"] is not None else None
            for f in ("climbs", "pooled", "pool"):
                if f in rec and rec[f] is None:
                    rec["unknown"][f] = "its project kept no record of it"
        return rec
    except Exception as e:
        if log:
            log(f"reading what it's made from failed ({type(e).__name__}: {e})")
        return None


def backfill_inputs(mid: str) -> dict | None:
    """What a mesh made before inputs.json was kept was made from, as far as its project still tells: each field only
    where what holds it is unchanged since the mesh was made (photos, and the records of what was pooled and filtered,
    written before then; its area's page read before then), and unknown otherwise. COLMAP's mapper settings weren't
    kept, so are unknown. None for a mesh not made from a project."""
    m = q("SELECT id, source, created, rules FROM meshes WHERE id=?", mid, one=True)
    if not m or not (m["source"] or "").startswith("project:") or not (MESHES / mid).is_dir():
        return None
    pid, made = m["source"].split(":", 1)[1], m["created"] or 0
    proj = q("SELECT kind, climb_id, boulder FROM projects WHERE id=?", pid, one=True)
    pdir, kind = PROJECTS / pid, inputs_kind(proj)
    unknown: dict[str, str] = {"meshopts": "COLMAP's mapper settings weren't kept before inputs.json"}
    rec = {"mesh": mid, "project": pid, "kind": kind, "made": made, "backfilled": True, "mesh_rules": m["rules"] or 0,
           "unknown": unknown}
    if not proj or not pdir.is_dir():
        for f in ("photos", "filter"):
            unknown[f] = "its project is gone"
        return rec
    folder = pdir / "photos"
    if photos_away(pid):
        unknown["photos"] = "its photos are offloaded to the backup"
    else:
        paths = meshinputs.photos_in(folder)
        if not folder.is_dir() or folder.stat().st_mtime > made or any(p.stat().st_mtime > made for p in paths):
            unknown["photos"] = "its project's photos changed after it was made"
        else:
            rec["photos"] = meshinputs.photo_files(paths)
    rel = pdir / "relevance.json"
    if not rel.exists():
        rec["filter"] = "none"
    elif rel.stat().st_mtime <= made:
        rec["filter"] = meshinputs.filter_of(_read_json(rel))
    else:
        unknown["filter"] = "its filter's report was written again after it was made"
    if kind in ("boulder", "pool"):
        leaf = rec["leaf"] = pool_leaf(proj["boulder"])
        row = q("SELECT kind, checked FROM boulders WHERE key=?", leaf, one=True)
        if leaf_kind(leaf, {}) == "group":     # by its name
            if kind == "pool":
                rec["leaf_kind"] = "group"
            else:
                unknown["leaf_kind"] = "its name says it holds several boulders now; what was made of it then wasn't kept"
        elif not row or not row["kind"]:
            rec["leaf_kind"] = "unread"
        elif (row["checked"] or 0) <= made:
            rec["leaf_kind"] = row["kind"]
        else:
            unknown["leaf_kind"] = "its area's page was read after it was made"
        climbs = _read_json(pdir / CLIMBS_FILE)
        if climbs and (climbs.get("created") or 0) <= made and _climbs_pooled(pdir) is not None:
            rec["climbs"] = _climbs_pooled(pdir)
            if kind == "pool":
                rec["pool"] = sorted(rec["climbs"])
        else:
            unknown["climbs"] = "its climbs were pooled again after it was made" if climbs else "its project kept no record of it"
            if kind == "pool":
                unknown["pool"] = unknown["climbs"]
        sources = _read_json(pdir / "sources.json")
        if sources and (sources.get("created") or 0) <= made:
            rec["pooled"] = _pooled_names(pdir)
        else:
            unknown["pooled"] = "its photos were pooled again after it was made" if sources else "its project kept no record of it"
    return rec


def inputs_of(mid: str) -> dict | None:
    """A mesh's inputs.json, backfilled (backfill_inputs()) and kept if it had none."""
    rec = meshinputs.load(MESHES / mid)
    if rec is None:
        rec = backfill_inputs(mid)
        if rec is not None:
            rec = meshinputs.save(MESHES / mid, rec)
    return rec


def inputs_existing_meshes() -> None:
    """Backfill inputs.json for each mesh made from a project before it was kept (backfill_inputs())."""
    n = 0
    for m in q("SELECT id FROM meshes WHERE source LIKE 'project:%' ORDER BY created DESC"):
        if meshinputs.load(MESHES / m["id"]) is None and (MESHES / m["id"]).is_dir():
            try:
                n += inputs_of(m["id"]) is not None
            except Exception as e:
                print(f"{m['id']}: backfilling what it was made from failed ({type(e).__name__}: {e})", flush=True)
    if n:
        print(f"recorded what {n} mesh{'' if n == 1 else 'es'} made before inputs.json were made from, as far as their projects tell", flush=True)
    meshes_changed()    # which are old by data, now that what they were made from is known


def _linked_as(names: dict[str, Path], where: Path) -> list[Path]:
    """Symbolic links to these files, under the names given, in a new folder: photos as a step would see them."""
    where.mkdir(parents=True)
    out = []
    for name, src in sorted(names.items()):
        (where / name).symlink_to(src.resolve())
        out.append(where / name)
    return out


def _filtered_now(names: dict[str, Path], prior: dict | None, deadline: float, dedupe: bool,
                  more: list[Path] | None = None) -> dict[str, Path]:
    """Which of these photos ({name: file}) dedupe (if asked) and the off-topic filter would keep now, from the filter's
    cache and earlier verdicts (prior) alone (relevance.filter_photos()'s cache_only). Raises relevance.Uncached where
    that needs working out, or TimeoutError past the deadline."""
    if time.time() > deadline:
        raise TimeoutError
    stage = Path(tempfile.mkdtemp(prefix="bm-inputs-", dir=DATA / "compute" if (DATA / "compute").is_dir() else None))
    try:
        paths = _linked_as(names, stage / "photos")
        quiet = lambda s: None  # noqa: E731
        if dedupe:
            paths = diversity_mod.dedupe(stage / "photos", quiet)
        report = relevance_mod.filter_photos(paths, None, quiet, prior=prior, more_anchors=more, cache_only=True)
        return {n: names[n] for n in report["kept"]}
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _mapper_now(kind: str, rec: dict, proj: dict | None) -> dict | None:
    """COLMAP's mapper settings a mesh would be made with now: of the areas that list its climb (or its boulder's
    climbs), the defaults if none does, or None if they differ."""
    if kind in ("boulder", "pool"):
        rows = q("SELECT DISTINCT campaign_id FROM campaign_items WHERE boulder=?", rec.get("leaf"))
    elif proj and proj["climb_id"]:
        rows = q("SELECT DISTINCT campaign_id FROM campaign_items WHERE climb_id=?", proj["climb_id"])
    else:
        rows = []
    found = {json.dumps(mapper_of(c), sort_keys=True) for r in rows
             for c in [q("SELECT * FROM campaigns WHERE id=?", r["campaign_id"], one=True)] if c}
    if len(found) > 1:
        return None
    return {**meshopts.MAPPER, **json.loads(found.pop())} if found else dict(meshopts.MAPPER)


def _pool_now(leaf: str, members: list[str]) -> tuple[list[str] | None, str | None]:
    """The climbs that would pool with these now (settle_clusters()): of the lowest area's climbs with harvests, the
    group linked by their photos that holds most of them; or None, and what telling would need, where a climb with photos
    of its own was never compared with any other."""
    ids = [r["id"] for r in q("SELECT id FROM climbs WHERE area_path=?", leaf) if climb_harvests(r["id"])]
    linked = {x for r in q(f"SELECT a, b FROM climb_links WHERE a IN ({','.join('?' * len(ids)) or 'NULL'})", *ids)
              if r["b"] in ids for x in (r["a"], r["b"])}
    unlinked = [c for c in ids if c not in linked and any(viewpoints.own_photos(PROJECTS / h) for h in climb_harvests(c))]
    if unlinked and len(ids) > 1:
        return None, f"matching the photos of {plural(len(unlinked), 'climb')} of its area with the others' (link_climbs())"
    groups = clusters(ids) if len(ids) >= 2 else [[c] for c in ids]
    best = max(groups, key=lambda g: (len(set(g) & set(members)), -len(g)), default=[])
    return (sorted(best) if len(best) >= 2 and set(best) & set(members) else []), None


def inputs_would(rec: dict, deadline: float) -> tuple[dict, dict, list[str]]:
    """What a mesh would be made from now (its inputs.json's fields), from the database, its project's and harvests'
    folders and the off-topic filter's cache and earlier verdicts: nothing downloaded, no CLIP, matching or COLMAP.

    Returns:
        The fields as now (meshinputs.compare()'s now), {field: what telling it would need}, and notes.
    """
    pid, kind = rec["project"], rec["kind"]
    proj = q("SELECT kind, climb_id, boulder FROM projects WHERE id=?", pid, one=True)
    pdir = PROJECTS / pid
    now: dict = {"mesh_rules": MESH_RULES}
    needs: dict[str, str] = {}
    notes: list[str] = []
    if not proj or not pdir.is_dir():
        return now, {"photos": "its project, which is gone"}, notes
    if kind == "rest":
        return now, {"photos": "meshing its boulder again: its photos are what that mesh left out"}, notes
    mapper = _mapper_now(kind, rec, proj)
    if mapper is None:
        needs["meshopts"] = "knowing which of the areas that list it meshes it, as their mapper settings differ"
    else:
        now["meshopts"] = mapper
    pooling = kind in ("boulder", "pool")
    was_filter = rec.get("filter")
    now["filter"] = "none" if was_filter == "none" and not pooling else current_filter()
    filter_changed = isinstance(was_filter, dict) and any(
        was_filter.get(k) is not None and was_filter.get(k) != now["filter"][k] for k in ("version", "settings", "methods"))
    photos = rec.get("photos") if isinstance(rec.get("photos"), dict) else None
    if not pooling:
        if photos_away(pid):
            needs["photos"] = "bringing its photos back from the backup"
            return now, needs, notes
        have = {p.name: p for p in meshinputs.photos_in(pdir / "photos")}
        if filter_changed and photos:   # judged again from scratch: what the filter rejected too, but not dedupe
            rejected = pdir / "rejected"
            out = {f.name.split("_", 1)[1]: f for f in (sorted(rejected.iterdir()) if rejected.is_dir() else [])
                   if f.name.split("_", 1)[0] in relevance_mod.REASONS and "_" in f.name}
            more = harvest_mod.anchors_of(boulder_projects(proj["climb_id"], pid)) if proj["climb_id"] else []
            try:
                have = _filtered_now({**out, **have}, _read_json(pdir / "relevance.json"), deadline, False, more)
            except relevance_mod.Uncached as e:
                needs["photos"] = str(e)
            except TimeoutError:
                needs["photos"] = "the off-topic filter from its cache (this check's time ran out)"
        if photos and "photos" not in needs:
            diff = meshinputs.same_photos(photos, have)
            now["photos"] = {"changed": diff} if diff else {"same": True}
        return now, needs, notes

    leaf = rec.get("leaf") or pool_leaf(proj["boulder"])
    now["leaf"] = leaf
    kind_now = leaf_kind(leaf)
    if kind_now is None:
        needs["leaf_kind"] = "reading its area's Mountain Project page"
    else:
        now["leaf_kind"] = kind_now
    if kind == "pool":
        members, why = _pool_now(leaf, rec.get("pool") or sorted(_climbs_pooled(pdir) or []))
        if kind_now == "boulder":
            members, why = [], None
            notes.append("its area is one boulder now: meshed whole, not pooled")
        if why:
            needs["pool"] = needs["climbs"] = needs["pooled"] = needs["photos"] = why
            return now, needs, notes
        now["pool"] = members
        if not members:
            notes.append("its climbs wouldn't be pooled now")
        ids = members
    else:
        ids = [c["id"] for c in boulder_climbs(leaf)]
        if kind_now not in (None, *meshinputs.WHOLE):
            notes.append("its area isn't one boulder now: its climbs are pooled by their photos, not meshed whole")
    names = {r["id"]: r["name"] for r in q(f"SELECT id, name FROM climbs WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", *ids)}
    ordered = sorted(ids, key=lambda c: (names.get(c) or "", c))   # boulder_climbs()' order
    now["climbs"] = {c: climb_harvests(c) for c in ordered}
    unharvested = [c for c in ordered if not now["climbs"][c]]
    if unharvested:
        notes.append(f"{plural(len(unharvested), 'climb')} of it never harvested, which meshing it with fetching would harvest first")
    others = [PROJECTS / h for c in ordered for h in now["climbs"][c]]
    away = [o.name for o in others if photos_away(o.name)]
    if away:
        needs["pooled"] = needs["photos"] = f"bringing the photos of {plural(len(away), 'harvest')} back from the backup"
        return now, needs, notes
    picked = harvest_mod.pool_candidates(others)[0]
    now["pooled"] = sorted(name for _, name, _ in picked)
    if not photos:
        return now, needs, notes
    sources = {name: src for src, name, _ in picked}
    if rec.get("pooled") is not None and sorted(rec["pooled"]) == now["pooled"] and not filter_changed:
        kept = {n: sources[n] for n in photos["files"] if n in sources}     # the same photos, judged as before
    else:
        try:
            kept = _filtered_now(sources, _read_json(pdir / "relevance.json"), deadline, True)
        except relevance_mod.Uncached as e:
            needs["photos"] = str(e)
            return now, needs, notes
        except TimeoutError:
            needs["photos"] = "dedupe and the off-topic filter from its cache (this check's time ran out)"
            return now, needs, notes
    diff = meshinputs.same_photos(photos, kept)
    now["photos"] = {"changed": diff} if diff else {"same": True}
    return now, needs, notes


def inputs_check(budget: float = INPUTS_BUDGET, log=None) -> dict:
    """The dry run: for each current mesh made by areas and projects, whether it would be made from the same inputs now
    (inputs_would(), meshinputs.compare()), with no meshing, downloading, CLIP or matching. Records missing are
    backfilled first. Kept in INPUTS_CHECK.

    Args:
        budget: seconds into it after which it starts no more dedupe or filtering from caches, for meshes whose
            photos or filter changed: their photos are then reported as needing that.

    Returns:
        {at, took, meshes, rules, filter (as now), counts, changed: [{id, name, kind, changed, unknown, notes}],
        unknown: [same], same: [{id, name, kind}]}.
    """
    with _inputs_lock:
        began = time.time()
        deadline = began + budget
        rows = q("SELECT id, name FROM meshes WHERE replaced_by IS NULL AND source LIKE 'project:%' ORDER BY created")
        out: dict[str, list] = {"changed": [], "unknown": [], "same": []}
        for i, m in enumerate(rows):
            if log and i and i % 50 == 0:
                log(f"{i} of {len(rows)} meshes checked")
            rec = inputs_of(m["id"])
            if rec is None:
                out["unknown"].append({"id": m["id"], "name": m["name"], "kind": None, "changed": {}, "notes": [],
                                       "unknown": {"photos": "its folder is gone"}})
                continue
            try:
                now, needs, notes = inputs_would(rec, deadline)
            except Exception as e:
                now, needs, notes = {}, {"photos": f"working it out, which failed ({type(e).__name__}: {e})"}, []
            got = meshinputs.compare(rec, now, needs)
            entry = {"id": m["id"], "name": m["name"], "kind": rec.get("kind")}
            out[got["status"]].append(entry if got["status"] == "same" else {**entry, **{k: got[k] for k in ("changed", "unknown")}, "notes": notes})
        report = {"at": began, "took": round(time.time() - began, 1), "meshes": len(rows), "rules": MESH_RULES,
                  "filter": current_filter(), "counts": {k: len(v) for k, v in out.items()}, **out}
        try:
            INPUTS_CHECK.write_text(json.dumps(report, indent=1))
        except OSError:
            pass
        return report


def rest_deleted(mid: str, key: str) -> None:
    """A mesh of what a boulder's mesh left out was deleted: while that boulder's mesh is current, its scene is meshed
    again from the same photos (linked from the boulder's project, as kept: nothing is fetched again), and its card stays
    a group meanwhile; otherwise only noted."""
    parent, model = rest_ref(key)
    with _rest_lock:
        rec = load_rest(parent) or {}
        target = next((m for m in rec.get("models") or [] if m.get("name") == model), None) if model else rec
        if not target or (target.get("attempt") or {}).get("mesh") != mid:
            return
        row = q("SELECT boulder, replaced_by FROM meshes WHERE id=?", parent, one=True)
        if not row or row["replaced_by"]:
            rest_attempt(parent, model, status="deleted")
            return
        target["attempt"] = queue_rest(parent, row["boulder"], model, target["climbs"] if model else rec.get("apart") or [])
        save_rest(parent, rec)


def run_rest(pid: str, key: str, resume: bool, log, tracker) -> None:
    """Mesh a scene of what a boulder's mesh left out (keep_rest()): its model's photos (before models were tied, its
    climbs' left-out photos), from that mesh's own photos, as filtered for it, in this pool's project. Then each climb
    whose own photos place in it is linked to it, and it's named for them, and the boulder's mesh's rest says how it
    went: it made nothing ("failed", or "poor" with too little diversity to try) only when its photos weren't enough,
    as too_few_placed() tells; another failure is an "error", to try again."""
    parent, model = rest_ref(key)
    rec = load_rest(parent) or {"climbs": {}, "apart": []}
    pdir = PROJECTS / pid
    members = pool_members(key)

    def attempt(**how) -> None:
        rest_attempt(parent, model, project=pid, **how)
    proj = q("SELECT harvest FROM projects WHERE id=?", pid, one=True)
    if resume and proj["harvest"]:
        summary = json.loads(proj["harvest"])
    else:
        src = q("SELECT source FROM meshes WHERE id=?", parent, one=True)
        if not src or not src["source"].startswith("project:"):
            raise ValueError(f"the boulder's mesh {parent} is gone")
        ppid = src["source"].split(":", 1)[1]
        need_back([ppid], "meshing what its boulder's mesh left out")
        if model:
            entry = next((m for m in rec.get("models") or [] if m.get("name") == model), None)
            if entry is None:
                raise ValueError(f"the boulder's mesh {parent} has no record of a model {model} of what it left out")
            names = sorted(entry["photos"])
            log(f"-- meshing the {plural(len(names), 'photo')} of COLMAP's model {model} of what the boulder's mesh {parent} left "
                f"out: {entry['why']}")
            try:
                own = {c["id"]: set(c.get("own") or []) for c in json.loads((PROJECTS / ppid / CLIMBS_FILE).read_text())["climbs"]}
            except (OSError, ValueError, KeyError):
                own = {}
            own_of = lambda c: sorted(own.get(c, set()) & set(names))  # noqa: E731
        else:
            names = sorted({n for c in members for n in (rec["climbs"].get(c) or {}).get("left", [])})
            log(f"-- meshing the {plural(len(names), 'photo')} the boulder's mesh {parent} left out of {plural(len(members), 'climb')}")
            own_of = lambda c: [n for n in (rec["climbs"].get(c) or {}).get("left", []) if relevance_mod.is_trusted(Path(n))]  # noqa: E731
        for partial in ("photos", "rejected", "masks"):
            shutil.rmtree(pdir / partial, ignore_errors=True)
        (pdir / "photos").mkdir(parents=True)
        for n in names:
            f = PROJECTS / ppid / "photos" / n
            if f.is_file():
                try:
                    os.link(f, pdir / "photos" / n)
                except OSError:
                    shutil.copy2(f, pdir / "photos" / n)
        try:
            files = [f for f in json.loads((PROJECTS / ppid / "sources.json").read_text()).get("files") or [] if f.get("file") in names]
        except (OSError, ValueError):
            files = []
        (pdir / "sources.json").write_text(json.dumps({"files": files}))
        climbs = {c["id"]: c for c in boulder_climbs(key)}
        record = [{"id": c, "name": climbs[c]["name"] if c in climbs else c, "grade": climbs[c]["grade"] if c in climbs else None,
                   "projects": [], "own": own_of(c)} for c in members]
        (pdir / CLIMBS_FILE).write_text(json.dumps({"boulder": key, "climbs": record, "created": time.time()}, indent=1))
        summary = {"images": len(names), "rest_of": parent, **({"model": model} if model else {}),
                   "diversity": diversity_mod.assess(pdir / "photos", log)}
        q("UPDATE projects SET harvest=?, status='harvested' WHERE id=?", json.dumps(summary), pid, write=True)
    verdict = diversity_mod.judge(summary.get("diversity"))
    log(f"left out: {summary['images']} image(s), verdict {verdict}")
    if verdict == "poor":
        log("not enough viewpoint diversity for photogrammetry - skipping reconstruction")
        tracker.skip_rest()
        attempt(status="poor", mesh=None, climbs=[])
        return
    attempt(status="running")
    run_reconstruct(pid, internet=True, tracker=tracker, resume=resume)
    p = q("SELECT status, mesh_id FROM projects WHERE id=?", pid, one=True)
    if p["status"] == "done" and p["mesh_id"]:
        shutil.rmtree(pdir / "work", ignore_errors=True)
        seen = (viewpoints.load(MESHES / p["mesh_id"]) or {}).get("climbs") or {}
        placed = [c for c in members if c in seen]
        for c in placed:
            link_mesh(p["mesh_id"], c, REST_LINK)
        if placed:     # named for the climbs it shows, not all those whose photos it was made from
            q("UPDATE meshes SET name=? WHERE id=?", climbs_named(placed), p["mesh_id"], write=True)
        log(f"rest: places {plural(len(placed), 'climb')} by their own photos")
        attempt(status="done", mesh=p["mesh_id"], climbs=placed)
    else:
        why = too_few_placed(pid)
        attempt(status="failed" if why else "error", mesh=None, climbs=[], why=why or last_failure(pid))


def fewer_placed(before: str, mid: str, photos: Path) -> tuple[int, int] | None:
    """(how many photos mesh mid placed, how many mesh before did), if mid was made from photos that included every one
    before placed, and placed fewer of them; else None."""
    was, now = placed_photos(before), placed_photos(mid)
    if not was or now is None or len(now) >= len(was):
        return None
    given = {f.name for f in photos.iterdir()} if photos.is_dir() else set()
    return (len(now), len(was)) if set(was) <= given else None


def place_climbs(mid: str, pid: str, log) -> None:
    """Work out where on a boulder's mesh each of its climbs is seen from (viewpoints.py). Only costs that if it fails."""
    try:
        if not (MESHES / mid / "cameras.json").exists():
            log("views: no cameras were kept, so its climbs can't be placed on it")
            return
        climbs = json.loads((PROJECTS / pid / CLIMBS_FILE).read_text())["climbs"]
        viewpoints.compute(MESHES / mid, {c["id"]: set(c["own"]) for c in climbs}, log)
    except Exception as e:
        log(f"placing its climbs failed ({type(e).__name__}: {e})")


def run_reconstruct(pid: str, internet: bool = False, tracker: Tracker | None = None, resume: bool = False,
                    keep_better: bool = False) -> str | None:
    """Make a project's mesh.

    Args:
        resume: carry on from the last step an interrupted run finished, rather than from scratch.
        keep_better: if this places fewer photos than the project's mesh before, from photos that included every one
            that mesh placed (fewer_placed()), that one stays the current one, and this is kept as an older version of it.

    Returns:
        The id of the mesh it made, if it made one.
    """
    pdir = PROJECTS / pid
    q("UPDATE projects SET status='running' WHERE id=?", pid, write=True)
    started, log_from = time.time(), (pdir / "job.log").stat().st_size if (pdir / "job.log").exists() else 0
    log = lambda s: append_log(pid, s)  # noqa: E731
    try:
        need_back([pid], "meshing it")
    except (PhotosAway, LostData) as e:     # it waits for them, as for a machine; or for the admin page, if they're lost
        log(str(e))
        q("UPDATE projects SET status='stopped' WHERE id=?", pid, write=True)
        raise
    tracker = tracker or tracker_for(pid, pipeline.steps())
    try:
        work = pdir / "work"
        if not resume:
            shutil.rmtree(work, ignore_errors=True)
        mid = new_id()
        remote = bool(_executor) and (current_worker().remote or not _local_meshing)
        if not remote and not _local_meshing:
            raise compute.ComputeUnavailable("this server meshes nothing itself (Reconstructions at once on this server is 0), "
                                             "and no other machines are set up")
        build = _executor.reconstruct if remote else pipeline.reconstruct
        inputs = inputs_record(pid, log)
        for st in tracker.snapshot()["steps"]:      # the harvest's last, which was left running until a next one began
            if st["state"] == "running" and not st.get("parallel"):
                tracker.end(st["key"])
        tracker.at("machine" if remote else "server")
        mesh_path, demo = build(pdir / "photos", work, MESHES / mid, log, USE_GPU, internet=internet, tracker=tracker)
        proj = q("SELECT name, climb_id, mesh_id, kind, boulder FROM projects WHERE id=?", pid, one=True)
        q("INSERT INTO meshes(id,name,filename,source,created,size,demo,lat,lng,boulder,rules) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          mid, mesh_name(proj["name"]), mesh_path.name, f"project:{pid}", time.time(), mesh_path.stat().st_size, int(demo),
          *boulder_place(proj["boulder"]), proj["boulder"] if proj["kind"] == "boulder" else None, MESH_RULES, write=True)
        if inputs:
            try:
                meshinputs.save(MESHES / mid, {**inputs, "mesh": mid, "made": time.time()})
            except OSError as e:
                log(f"keeping what it was made from failed ({e})")
        if proj["climb_id"]:
            link_mesh(mid, proj["climb_id"], "auto-linked from reconstruction project")
            log(f"mesh linked to climb {proj['climb_id']}")
        before = proj["mesh_id"] if proj["mesh_id"] and proj["mesh_id"] != mid and q("SELECT id FROM meshes WHERE id=?", proj["mesh_id"]) else None
        worse = fewer_placed(before, mid, pdir / "photos") if before and keep_better and not mesh_is_old(
            q("SELECT source, rules FROM meshes WHERE id=?", before, one=True)) else None      # never an old one over it
        if worse:
            supersede(mid, before)
            log(f"this placed {worse[0]} of its photos, fewer than the {worse[1]} mesh {before} placed from them: that one stays "
                "the current one, and this is kept as an older version of it")
        elif before:
            supersede(before, mid)
            log(f"this replaces mesh {before}, which stays as an older version")
        current = before if worse else mid
        clean_mesh(mid, log)
        queue_pack(mid, urgent=True)
        record_sources(mid, pid, log)
        if proj["kind"] == "boulder" and not worse:
            place_climbs(mid, pid, log)
        if not worse:
            retire_covered(mid, log)
        meshes_changed()
        keep_heading(mid, log)
        tracker.finish()     # before it's marked done, so that anything waiting for that sees it all finished
        keep_timings(mid, pid, started, log_from, tracker, remote)
        log("done")
        q("UPDATE projects SET status='done', mesh_id=?, demo=? WHERE id=?", current, int(demo), pid, write=True)
        q("UPDATE campaign_items SET status='done', mesh_id=?, retry=0, note=NULL, updated=? WHERE project_id=? AND status='failed'",
          current, time.time(), pid, write=True)   # made by hand since its area gave up on it
        if not demo and not worse:
            queue_scale(mid)
            queue_splat(mid)
            if settings().get("beta.auto") and beta_mod.videos(MESHES / mid):
                queue_beta(mid)     # while its videos are still in the cache
        return mid
    except compute.ComputeUnavailable as e:    # not the climb's failure: it waits for a machine
        log(f"waiting for a machine to mesh it on: {e}")
        q("UPDATE projects SET status='stopped' WHERE id=?", pid, write=True)
        raise
    except Exception as e:
        diagnostics.log_failure(log, e, DATA)
        tracker.finish("failed")
        q("UPDATE projects SET status='failed' WHERE id=?", pid, write=True)


def keep_timings(mid: str, pid: str, started: float, log_from: int, tracker: Tracker, remote: bool) -> None:
    """Keep with a new mesh how long it took to make, and where (timings.py), from its job's log since log_from and its
    tracker. Only costs that if it fails."""
    try:
        with open(PROJECTS / pid / "job.log", errors="replace") as f:
            f.seek(log_from)
            text = f.read()
        rec = timings_mod.record(text, tracker.snapshot(), started, time.time(), current_worker().name if remote else "this server",
                                 VERSION, remote, {s[0] for s in pipeline.steps()})
        timings_mod.save(MESHES / mid, rec)
        _timings[mid] = rec
    except Exception as e:
        append_log(pid, f"timings: not kept ({type(e).__name__}: {e})")


_timings: dict[str, dict | None] = {}     # meshes' timings.json, as read


def mesh_timings(mid: str) -> dict | None:
    if mid not in _timings:
        _timings[mid] = timings_mod.load(MESHES / mid)
    return _timings[mid]


def timings_brief(rec: dict | None) -> dict | None:
    """What the mesh's details show of its timings."""
    if not rec:
        return None
    keys = ("where", "remote", "wall", "tools_seconds", "cpu_seconds", "harvest_seconds", "photos_in", "placed", "vggt_seconds",
            "waited", "from_log", "version", "ended")
    return {k: rec.get(k) for k in keys}


def mesh_areas():
    """A function giving a mesh row (with its climbs, and boulder_climbs for a boulder's own) the names of the areas it
    was made for (those whose list holds it as a climb's mesh, or else lists its climbs), and the Mountain Project
    path of its first climb's area, top first."""
    made_for: dict[str, set] = {}
    by_climb: dict[str, set] = {}
    for r in q("SELECT ci.mesh_id, ci.climb_id, c.name FROM campaign_items ci JOIN campaigns c ON c.id=ci.campaign_id"):
        if r["mesh_id"]:
            made_for.setdefault(r["mesh_id"], set()).add(r["name"])
        if r["climb_id"]:
            by_climb.setdefault(r["climb_id"], set()).add(r["name"])
    paths = {c["id"]: c["area_path"] for c in q("SELECT id, area_path FROM climbs WHERE area_path IS NOT NULL")}

    def of(m: dict) -> tuple[list[str], list[str] | None]:
        climbs = [c["id"] for c in m.get("climbs") or [] if not c.get("shared")] + [c["id"] for c in m.get("boulder_climbs") or []]
        names = made_for.get(m["id"]) or set().union(*(by_climb.get(c, set()) for c in climbs))
        path = next((paths[c] for c in climbs if c in paths), None)
        try:
            path = json.loads(path) if path else None
        except ValueError:
            path = None
        return sorted(names), path
    return of


def timings_existing_meshes() -> None:
    """Timings for meshes made before they were kept, from their projects' job logs (timings.backfill()): the newest of
    each project's, as its log's last run is the one that made it."""
    try:
        # each project's newest mesh only: its log's last run is that one's
        n = timings_mod.backfill(q("SELECT id, source, created FROM meshes m WHERE source LIKE 'project:%' AND created = "
                                   "(SELECT MAX(created) FROM meshes x WHERE x.source = m.source)"), PROJECTS, MESHES, None)
        if n:
            _timings.clear()
            print(f"timings: {n} older mesh(es) given theirs from their job logs", flush=True)
    except Exception as e:
        print(f"timings: backfill failed ({type(e).__name__}: {e})", flush=True)


def timings_summary() -> dict:
    """The admin page's medians of how long meshes took (timings.summarize()): over the last week, and all."""
    recs = [r for m in q("SELECT id FROM meshes WHERE source LIKE 'project:%'") if (r := mesh_timings(m["id"]))]
    return {"week": timings_mod.summarize(recs, since=time.time() - 7 * 86400), "all": timings_mod.summarize(recs), "meshes": len(recs)}


CHANNEL_TTL = 30 * 86400    # how long a channel's description is trusted before it's read again
# how long an area's climb waits for volunteers' YouTube searches and videos before it's meshed without the rest
VOLUNTEER_WAIT = float(os.environ.get("VOLUNTEER_WAIT_HOURS", "24")) * 3600


def record_videos(climb_id: str, videos: list[dict], log) -> None:
    """Keep what's known of a climb's videos, and what they and their uploaders' channels say about the
    climber's size. Only costs that if it fails.

    Args:
        videos: dicts with url and found_by ("page" or "search"), and whatever youtube.details() gave.
    """
    for v in videos:
        if not v.get("url"):
            continue
        try:
            s = save_video(v)
            q("""INSERT INTO climb_videos(climb_id,url,found_by,frames,created) VALUES(?,?,?,?,?)
                 ON CONFLICT(climb_id,url) DO UPDATE SET frames=COALESCE(excluded.frames,frames)""",
              climb_id, v["url"], v.get("found_by") or "page", v.get("frames"), time.time(), write=True)
            if v.get("channel_url"):
                record_channel(v["channel_url"])
            if any(s[k] is not None for k in ("height_cm", "wingspan_cm", "ape_index_cm")):
                log(f"climber's size in {v['url']}: {s['quotes']}")
        except Exception as e:
            log(f"couldn't record video {v.get('url')}: {type(e).__name__}: {e}")


def save_video(v: dict) -> dict:
    """Keep a video's details (youtube.details()) in the videos table; what they say about the climber's size."""
    s = youtube_mod.stats(f"{v.get('title') or ''}\n{v.get('description') or ''}")
    q("""INSERT INTO videos(url,title,uploader,channel_id,channel_url,duration,upload_date,license,description,fetched,
                            height_cm,wingspan_cm,ape_index_cm,first_person,quotes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(url) DO UPDATE SET title=COALESCE(excluded.title,title), uploader=COALESCE(excluded.uploader,uploader),
           channel_id=COALESCE(excluded.channel_id,channel_id), channel_url=COALESCE(excluded.channel_url,channel_url),
           duration=COALESCE(excluded.duration,duration), upload_date=COALESCE(excluded.upload_date,upload_date),
           license=COALESCE(excluded.license,license), description=COALESCE(excluded.description,description),
           fetched=CASE WHEN excluded.description IS NULL THEN fetched ELSE excluded.fetched END,
           height_cm=CASE WHEN excluded.description IS NULL THEN height_cm ELSE excluded.height_cm END,
           wingspan_cm=CASE WHEN excluded.description IS NULL THEN wingspan_cm ELSE excluded.wingspan_cm END,
           ape_index_cm=CASE WHEN excluded.description IS NULL THEN ape_index_cm ELSE excluded.ape_index_cm END,
           first_person=CASE WHEN excluded.description IS NULL THEN first_person ELSE excluded.first_person END,
           quotes=CASE WHEN excluded.description IS NULL THEN quotes ELSE excluded.quotes END""",
      v["url"], v.get("title"), v.get("uploader"), v.get("channel_id"), v.get("channel_url"), v.get("duration"),
      v.get("upload_date"), v.get("license") if v.get("description") is not None else None, v.get("description"),
      time.time(), s["height_cm"], s["wingspan_cm"], s["ape_index_cm"], int(s["first_person"]),
      json.dumps(s["quotes"]) if s["quotes"] else None, write=True)
    return s


def record_channel(url: str) -> None:
    """Read a channel's description for the climber's size, unless it was read in the last CHANNEL_TTL; while
    YouTube is left to volunteers, or refusing this server, ask them for it instead."""
    seen = q("SELECT fetched FROM channels WHERE channel_url=?", url, one=True)
    if seen and time.time() - (seen["fetched"] or 0) < CHANNEL_TTL:
        return
    try:
        ch = youtube_mod.channel(url)
    except youtube_mod.Refused:
        row = q("SELECT channel_id FROM videos WHERE channel_url=? AND channel_id IS NOT NULL LIMIT 1", url, one=True)
        if row and settings().get("youtube.via") != "here" and VOLUNTEERS.any():
            VOLUNTEERS.want("channel", row["channel_id"], "channel", row["channel_id"])
        return
    save_channel(url, ch)


def save_channel(url: str, ch: dict) -> None:
    """Keep a channel's name and description (youtube.channel()), and what it says about the climber's size."""
    s = youtube_mod.stats(ch["description"])
    q("""INSERT OR REPLACE INTO channels(channel_url,channel_id,name,description,fetched,height_cm,wingspan_cm,ape_index_cm,
                                         first_person,quotes) VALUES(?,?,?,?,?,?,?,?,?,?)""",
      url, ch["channel_id"], ch["name"], ch["description"], time.time(), s["height_cm"], s["wingspan_cm"], s["ape_index_cm"],
      int(s["first_person"]), json.dumps(s["quotes"]) if s["quotes"] else None, write=True)


def photo_names(pid: str) -> list[str]:
    photos = PROJECTS / pid / "photos"
    return sorted(p.name for p in photos.iterdir() if p.suffix.lower() in PHOTO_EXT) if photos.is_dir() else []


def new_videos(climb_id: str, pid: str, log) -> list[str]:
    """Search YouTube for a climb, and say which of the videos found its project has no frames from.
    Also reads the details of its videos from before, which weren't kept then.

    Returns:
        The new videos' URLs.
    """
    c = climb_obj(climb_id)
    used = {e["url"] for e in provenance.harvested(PROJECTS / pid).values() if e.get("source") == "youtube" and e.get("url")}
    have = {youtube_mod.video_id(u) or u for u in used | {v.url for v in c.videos}}
    found = youtube_mod.search(c, log, want=search_wanter("harvest", pid), days=1)
    record_videos(climb_id, [{"url": v.url, "found_by": "search", "title": v.title, "uploader": v.credit} for v in found], log)
    known = {r["url"] for r in q("SELECT url FROM videos WHERE description IS NOT NULL")}
    for url in sorted(used - known):
        try:
            record_videos(climb_id, [{**youtube_mod.metadata(url), "url": url, "found_by": "page"}], log)
        except Exception as e:
            log(f"couldn't read the details of {url}: {e}")
    return [v.url for v in found if (youtube_mod.video_id(v.url) or v.url) not in have]


def boulder_projects(climb_id: str, but: str | None = None) -> list[Path]:
    """The harvests of every climb on a climb's boulder (as boulder_key() has it), its own included, newest
    first: what a harvest of it matches web photos against, and what a boulder's project pools. Not those
    that pooled others' themselves: a boulder's, or a climb's from before boulders had their own."""
    c = q("SELECT area_path FROM climbs WHERE id=?", climb_id, one=True)
    if not c or not boulder_key(json.loads(c["area_path"] or "[]")):
        return []
    if leaf_kind(c["area_path"]) == "group":     # its other climbs are on other rocks
        rows = q("SELECT id FROM projects WHERE climb_id=? AND kind='harvest' AND harvest IS NOT NULL AND id != ? AND name NOT LIKE ? "
                 "ORDER BY created DESC", climb_id, but or "", "%" + POOLED)
        return [PROJECTS / r["id"] for r in rows if (PROJECTS / r["id"] / "photos").is_dir()]
    rows = q("""SELECT p.id FROM projects p JOIN climbs c ON c.id=p.climb_id WHERE c.area_path=? AND p.kind='harvest'
                AND p.harvest IS NOT NULL AND p.id != ? AND p.name NOT LIKE ? ORDER BY p.created DESC""",
             c["area_path"], but or "", "%" + POOLED)
    return [PROJECTS / r["id"] for r in rows if (PROJECTS / r["id"] / "photos").is_dir()]


def run_harvest(pid: str, sources: set[str], reconstruct: bool, relevance_filter: bool = True, resume: bool = False,
                again: bool = False, wait: bool = False, need_youtube: bool = False, add: bool = False,
                follow: Callable[[dict], None] | None = None, refresh: bool = False, rejudge: bool = False) -> None:
    """Gather a climb's photos into its project, and optionally make its mesh. Only photos found for it are kept,
    so that its mesh shows the climb; the other harvests of its boulder only help tell which web photos are of it.

    Args:
        resume: carry on after an interruption. A finished harvest isn't repeated. A partial one
            starts again, which is quick, as its downloads are cached.
        again: harvest a project that was harvested before, from scratch, adding to its log.
        wait: rather than mesh it now, leave it "waiting" while volunteers still have YouTube searches or videos to
            fetch for it (an area's climb, which is meshed once they've come, so that it's meshed once).
        need_youtube: rather than mesh it now, leave it "waiting" until YouTube has been searched for it (an area's
            climb, unless its area meshes without that).
        add: with again, add the videos volunteers fetched since to what the harvest before found (harvest_mod's add),
            rather than start again: its photos stay (brought back from the backup first, if they were offloaded,
            waiting for that as for a machine), and only YouTube is looked at. Without a harvest before, from scratch.
        refresh: with add, every source is looked at again, and only what wasn't found before downloaded.
        rejudge: with add, nothing is looked for: every photo is judged again by the filter, from scratch, but for
            those put back by hand.
        follow: given its progress as it goes (tracker_for()), as a boulder's harvest of its climbs follows each.
    """
    pdir = PROJECTS / pid
    before = q("SELECT harvest FROM projects WHERE id=?", pid, one=True)["harvest"] if again and add else None
    add = bool(before) and (pdir / "sources.json").is_file()
    if add:
        need_back([pid], "judging its photos again" if rejudge else "adding to its photos")
    finished = q("SELECT harvest FROM projects WHERE id=?", pid, one=True)["harvest"] if resume else None
    if resume:
        append_log(pid, "-- carrying on after an interruption")
    elif add and rejudge:
        append_log(pid, "-- judging every photo again, from scratch, keeping those put back by hand")
    elif add and refresh:
        append_log(pid, "-- looking in every source again, for what wasn't found before")
    elif add:
        append_log(pid, "-- adding the videos volunteers fetched to what was harvested before")
    elif again:
        append_log(pid, "-- harvesting again")
    else:
        q("UPDATE projects SET log='' WHERE id=?", pid, write=True)
        (pdir / "job.log").unlink(missing_ok=True)
    q("UPDATE projects SET status='harvesting' WHERE id=?", pid, write=True)
    log = lambda s: append_log(pid, s)  # noqa: E731
    tracker = None
    try:
        proj = q("SELECT climb_id FROM projects WHERE id=?", pid, one=True)
        c = climb_obj(proj["climb_id"])
        stub = bool(c.raw.get("stub"))
        if add and rejudge:
            sources = set()
        elif add and not refresh:
            sources = {"youtube"} & set(sources) or {"youtube"}
        tracker = tracker_for(pid, ([("page", "Fetch problem page", 1)] if stub else [])
                              + harvest_mod.steps(sources, relevance_filter, add and not refresh)
                              + (pipeline.steps() if reconstruct else []), follow)
        if finished:
            summary = json.loads(finished)
            log("the photos were already harvested")
        else:
            if (resume or again) and not add:
                for partial in ("photos", "rejected"):
                    shutil.rmtree(pdir / partial, ignore_errors=True)
                (pdir / "photos").mkdir()
                offload_mod.drop(q, [f"projects/{pid}/{part}" for part in ("photos", "masks", "rejected")])     # made again
            if stub:
                tracker.update("page", 0.1, "up to a minute on Mountain Project")
                log(f"fetching the problem page for its photos/videos: {c.url}")
                upsert_climb(climbs_mod.fetch_climb_url(c.url, include_children=False)[0])
                c = climb_obj(proj["climb_id"])
            elif again and c.source == "mountainproject":
                try:    # from the cache if it's under 30 days old, as most will be
                    upsert_climb(climbs_mod.fetch_climb_url(c.url, include_children=False)[0])
                    c = climb_obj(proj["climb_id"])
                except Exception as e:
                    log(f"couldn't read the problem page again ({e}); using what was read before")
            summary = harvest_mod.harvest(c, pdir, log, sources, relevance_filter=relevance_filter, tracker=tracker,
                                          anchors_from=boulder_projects(proj["climb_id"], pid), want=video_wanter("harvest", pid),
                                          want_search=search_wanter("harvest", pid), add=add, refresh=add and refresh,
                                          rejudge=add and rejudge)
            videos = summary.pop("videos", [])
            record_videos(proj["climb_id"], videos, log)
            summary["videos"] = [{k: v.get(k) for k in ("url", "found_by", "title", "uploader", "frames", "license")} for v in videos]
            if add:     # the harvest before's videos and frames, with what's new
                was = json.loads(before)
                had = {v.get("url") for v in summary["videos"]}
                summary["videos"] = [v for v in was.get("videos") or [] if v.get("url") not in had] + summary["videos"]
                summary["video_frames"] = (was.get("video_frames") or 0) + (summary.get("video_frames") or 0)
                summary["queries"] = was.get("queries") or summary.get("queries")
            q("UPDATE projects SET harvest=?, status='harvested' WHERE id=?", json.dumps(summary), pid, write=True)
        verdict = diversity_mod.judge(summary.get("diversity"))     # by the current rules, for a harvest finished earlier
        log(f"harvest complete: {summary['images']} image(s), verdict {verdict}")
        coming = VOLUNTEERS.pending("harvest", pid)
        if reconstruct and ((wait and coming) or (need_youtube and not youtube_checked(summary))):
            log(f"waiting for volunteers' YouTube searches and videos ({coming} to come): it's harvested again and meshed "
                f"once they have, or after {VOLUNTEER_WAIT / 3600:.0f} h without them" if coming else
                "not meshed until YouTube has been searched for it, by a volunteer if this server won't: it's harvested again "
                f"to try in {VOLUNTEER_WAIT / 3600:.0f} h, or once one has")
            q("UPDATE projects SET status='waiting' WHERE id=?", pid, write=True)
            tracker.skip_rest()
            return
        if reconstruct and verdict != "poor":
            run_reconstruct(pid, internet=True, tracker=tracker, resume=resume)
            return
        if reconstruct:
            log("not enough viewpoint diversity for photogrammetry - skipping reconstruction "
                "(add your own photos to this project and press Reconstruct to try anyway)")
        tracker.skip_rest()
    except compute.ComputeUnavailable:
        raise
    except Exception as e:
        diagnostics.log_failure(log, e, DATA)
        if tracker:
            tracker.finish("failed")
        q("UPDATE projects SET status='failed' WHERE id=?", pid, write=True)


# ------------------------------------------------------------------ a boulder's own mesh, from every climb on it
BOULDER_SUFFIX = " (boulder)"   # the name of a boulder's project
PROJECT_SUFFIXES = (" (harvest)", BOULDER_SUFFIX, " (pooled)")     # what a project's name says of it, which its mesh's doesn't


def mesh_name(project_name: str) -> str:
    """A mesh's name, from its project's: the climbs or boulder it's of, not how it was made (the card says that)."""
    for s in PROJECT_SUFFIXES:
        if project_name.endswith(s):
            return project_name[: -len(s)]
    return project_name


def climb_harvests(climb_id: str) -> list[str]:
    """A climb's own harvests that found photos, newest first (not a pooled one from before boulders had theirs)."""
    rows = q("SELECT id FROM projects WHERE climb_id=? AND kind='harvest' AND harvest IS NOT NULL AND name NOT LIKE ? ORDER BY created DESC",
             climb_id, "%" + POOLED)
    return [r["id"] for r in rows if (PROJECTS / r["id"] / "photos").is_dir()]


def harvest_climb(climb_id: str, name: str, log, follow: Callable[[dict], None] | None = None) -> str | None:
    """Harvest a climb that hasn't been, as an area would, without meshing it: in a harvest of it that didn't finish,
    if there is one, or else a new project. An area that lists the climb and has no project for it takes this one,
    to mesh from when it gets there. Not one an area or a job is harvesting now.

    Returns:
        Its project, or None if it's being harvested elsewhere.
    """
    p = q("SELECT id, status FROM projects WHERE climb_id=? AND kind='harvest' AND harvest IS NULL AND name NOT LIKE ? "
          "ORDER BY created DESC LIMIT 1", climb_id, "%" + POOLED, one=True)
    if p and p["status"] in ("queued", "running", "harvesting"):
        log(f"  {name} is being harvested elsewhere now: not waiting for it")
        return None
    if p:
        pid = p["id"]
    else:
        pid = new_id()
        (PROJECTS / pid / "photos").mkdir(parents=True)
        q("INSERT INTO projects(id,name,created,kind,climb_id,status) VALUES(?,?,?,?,?,'queued')",
          pid, f"{name} (harvest)"[:80], time.time(), "harvest", climb_id, write=True)
    run_harvest(pid, set(HARVEST_SOURCES), False, resume=bool(p), follow=follow)
    q("UPDATE campaign_items SET project_id=? WHERE climb_id=? AND project_id IS NULL", pid, climb_id, write=True)
    return pid


def boulder_project_made(boulder: str) -> str:
    """A boulder's project, made now if it has none."""
    proj = boulder_project(boulder)
    if proj:
        return proj["id"]
    pid = new_id()
    (PROJECTS / pid / "photos").mkdir(parents=True)
    q("INSERT INTO projects(id,name,created,kind,boulder,status) VALUES(?,?,?,?,?,'new')",
      pid, f"{boulder_name(boulder)}{BOULDER_SUFFIX}"[:80], time.time(), "boulder", boulder, write=True)
    return pid


def mesh_boulder(boulder: str) -> tuple[dict, int]:
    """Queue meshing a boulder from every photo of its climbs, in its project.

    Returns:
        The response, and its HTTP status.
    """
    if leaf_kind(boulder) == "group":
        return {"error": "its Mountain Project area holds several boulders, so it isn't meshed as one: an area's climbs there "
                         "whose photos match are meshed together"}, 409
    proj = boulder_project(boulder)
    if proj and proj["status"] in ("queued", "running", "harvesting"):
        return {"error": "already being meshed", "project_id": proj["id"]}, 409
    pid = boulder_project_made(boulder)
    q("UPDATE projects SET status='queued', progress=NULL WHERE id=?", pid, write=True)
    queue_job({"type": "boulder", "pid": pid})
    return {"queued": True, "project_id": pid, "position": jobs.qsize()}, 202


def run_boulder(pid: str, resume: bool = False, fetch: bool = True, keep_better: bool = False) -> None:
    """Mesh a whole boulder from every photo its climbs' harvests found, in a project and mesh of its own, which
    take nothing from any climb's own; then work out where on it each climb is seen from (viewpoints.py), by the
    photos from its own page and videos. A climb's own harvests stay as they are, to mesh it from on its own.

    Args:
        resume: carry on after an interruption: climbs harvested already aren't again, and photos pooled
            already go straight on to meshing, from the last step that finished.
        fetch: first harvest each of its climbs that has no harvest yet, in a project of its own, as an area would
            (new_harvest_project()). Otherwise only what has been harvested is used.
        keep_better: as run_reconstruct() has it.
    """
    proj = q("SELECT boulder, harvest FROM projects WHERE id=?", pid, one=True)
    boulder, pdir = proj["boulder"], PROJECTS / pid
    if is_rest(boulder):     # what a boulder's mesh left out: from that mesh's own photos
        q("UPDATE projects SET status='harvesting' WHERE id=?", pid, write=True)
        tracker = tracker_for(pid, pipeline.steps())
        parent, model = rest_ref(boulder)
        try:
            run_rest(pid, boulder, resume, lambda s: append_log(pid, s), tracker)
        except (compute.ComputeUnavailable, Cancelled) as e:    # not how it went: it waits to be queued again, and carries on
            q("UPDATE projects SET status='stopped' WHERE id=?", pid, write=True)
            rest_attempt(parent, model, project=pid, status="waiting" if isinstance(e, compute.ComputeUnavailable) else "stopped",
                         why=str(e) or None)
            raise
        except Exception as e:  # nor is this: an error, to try again
            diagnostics.log_failure(lambda s: append_log(pid, s), e, DATA)
            tracker.finish("failed")
            q("UPDATE projects SET status='failed' WHERE id=?", pid, write=True)
            rest_attempt(parent, model, project=pid, status="error", mesh=None, climbs=[], why=f"{type(e).__name__}: {e}")
        return
    finished = proj["harvest"] if resume else None
    if not finished:    # the harvests it pools: back from the backup first, if any were offloaded
        try:
            need_back([h for c in boulder_climbs(boulder) for h in climb_harvests(c["id"])], f"pooling {boulder_name(boulder)}")
        except (PhotosAway, LostData) as e:
            append_log(pid, f"-- {e}")
            raise
    if resume:
        append_log(pid, "-- carrying on after an interruption")
    elif proj["harvest"]:
        append_log(pid, "-- pooling its climbs' photos again")
    else:
        (pdir / "job.log").unlink(missing_ok=True)
    q("UPDATE projects SET status='harvesting' WHERE id=?", pid, write=True)
    log = lambda s: append_log(pid, s)  # noqa: E731
    tracker = None
    try:
        page = None
        if fetch:
            adopted = adopt_listed_climbs(boulder)
            if adopted:
                log(f"{adopted} climb(s) an area lists here, which it hadn't reached, are now known")
            page = adopt_page_climbs(boulder, log) if not finished else None
        climbs = boulder_climbs(boulder)
        missing = [c for c in climbs if not climb_harvests(c["id"])] if fetch and not finished else []
        tracker = tracker_for(pid, ([("climbs", "Harvest its climbs", 3 * len(missing))] if missing else [])
                              + harvest_mod.steps(set()) + pipeline.steps())
        if missing:
            tracker.units("climbs", len(missing))
        for i, c in enumerate(missing):
            tracker.update("climbs", i / len(missing), c["name"], count=(i, len(missing), "climbs"))
            log(f"harvesting {c['name']}, which hadn't been")

            def follow(snap: dict, i: int = i, name: str = c["name"]) -> None:   # its harvest's progress, as this step's
                now = next((st for st in snap.get("steps", []) if st["state"] == "running" and not st.get("parallel")), None)
                tracker.update("climbs", (i + snap.get("overall", 0) / 100) / len(missing),
                               f"{name}: {now['label'].lower()}{', ' + now['detail'] if now.get('detail') else ''}" if now else name)
            cpid = harvest_climb(c["id"], c["name"], log, follow)
            got = q("SELECT harvest FROM projects WHERE id=?", cpid, one=True) if cpid else None
            if got:
                log(f"  {c['name']}: " + (f"{json.loads(got['harvest'])['images']} photo(s)" if got["harvest"] else f"failed: {last_failure(cpid)}"))
        if finished:
            summary = json.loads(finished)
            log("its climbs' photos were already pooled")
        else:
            for partial in ("photos", "rejected"):
                shutil.rmtree(pdir / partial, ignore_errors=True)
            (pdir / "photos").mkdir()
            record = []
            for c in climbs:
                harvests = climb_harvests(c["id"])
                own = set().union(*(viewpoints.own_photos(PROJECTS / h) for h in harvests))
                record.append({"id": c["id"], "name": c["name"], "grade": c["grade"], "projects": harvests, "own": sorted(own)})
            (pdir / CLIMBS_FILE).write_text(json.dumps({"boulder": boulder, "climbs": record, "created": time.time(),
                                                        "page_problems": page}, indent=1))
            others = [PROJECTS / h for r in record for h in r["projects"]]
            log(f"-- pooling the photos of {len(others)} harvest(s) of {sum(bool(r['projects']) for r in record)} of its "
                f"{len(record)} climb(s)")
            if not others:
                raise ValueError("none of its climbs has been harvested")
            lat, lng = boulder_place(boulder)
            c0 = Climb(source="boulder", source_id="/".join(json.loads(boulder)), url="", name=boulder_name(boulder), kind="area",
                       area_path=json.loads(boulder), boulder_name=boulder_name(boulder), lat=lat, lng=lng,
                       videos=[v for c in climbs for v in Climb.from_dict(json.loads(c["data"])).videos])
            summary = harvest_mod.harvest(c0, pdir, log, set(), tracker=tracker, pool_from=others)
            summary.pop("videos", None)
            summary["climbs"] = {"pooled": sum(bool(r["projects"]) for r in record), "of": len(record)}
            q("UPDATE projects SET harvest=?, status='harvested' WHERE id=?", json.dumps(summary), pid, write=True)
        verdict = diversity_mod.judge(summary.get("diversity"))
        log(f"pooled: {summary['images']} image(s), verdict {verdict}")
        if verdict == "poor":
            log("not enough viewpoint diversity for photogrammetry, even with every climb's photos - skipping reconstruction")
            tracker.skip_rest()
            return
        run_reconstruct(pid, internet=True, tracker=tracker, resume=resume, keep_better=keep_better)
        done = q("SELECT status, mesh_id FROM projects WHERE id=?", pid, one=True)
        if done["status"] == "done":
            shutil.rmtree(pdir / "work", ignore_errors=True)   # most of its disk use, and only useful to itself
            if not is_pool(boulder) and done["mesh_id"]:
                keep_rest(done["mesh_id"], pid, log, queue=settings().get("boulders.rest_auto"), untied=False)
                log_shares(done["mesh_id"], log)
    except compute.ComputeUnavailable:
        raise
    except Exception as e:
        diagnostics.log_failure(log, e, DATA)
        if tracker:
            tracker.finish("failed")
        q("UPDATE projects SET status='failed' WHERE id=?", pid, write=True)


# ------------------------------------------------------------------ campaigns: mesh a whole area
_outages: list[tuple[str, str]] = []    # (campaign, climb) failing in a row before finishing a harvest
COMPUTE_WAIT = 300      # seconds a worker whose machine couldn't be had waits before trying again


def use_processing_cores() -> None:
    """Keep the calling thread, and whatever it starts, to the cores the admin page gives this server's own processing
    (compute.processing_cores): the last of them, so that fewer than all leaves the first for answering visitors."""
    try:
        allowed = sorted(os.sched_getaffinity(os.getpid()))
        n = max(1, min(int(settings().get("compute.processing_cores") or len(allowed)), len(allowed)))
        os.sched_setaffinity(threading.get_native_id(), allowed[-n:])
    except (AttributeError, OSError, ValueError):
        pass


class Worker:
    """Somewhere climbs are meshed: this server, or for a remote worker, machines from compute.py. Each
    has its own thread, and its own JobControl, so stopping one area's climb leaves others alone."""

    def __init__(self, name: str, remote: bool = False, control: JobControl | None = None):
        self.name, self.remote = name, remote
        self.control = control or JobControl()
        self.campaign: str | None = None   # the area whose climb it's on
        self.item: str | None = None       # and that climb's source_id
        self.boulder: str | None = None    # which no other worker takes a climb on meanwhile, as harvests of its climbs
                                           # use each other's photos, and its own mesh pools them
        self.doing: str | None = None
        self.job: dict | None = None       # a job someone asked for that it's running: {job, started, from (% done)}
        self.error: str | None = None
        self.wait_until = 0.0
        self.waiting: dict | None = None   # an area's climb it found no machine for: {campaign, climb, boulder, why, since}
        self.retired = False


_first = Worker("this server", control=shared_control)   # what threads that aren't workers count as
_workers: list[Worker] = [_first]
_workers_lock = threading.Lock()
_claim_lock = threading.Lock()
_here = threading.local()
_pool: compute.Pool | None = None
_old_pools: list[compute.Pool] = []
_executor: compute.RemoteExecutor | pullwork.Board | None = None
_pool_key = None
_local_meshing = True    # whether this server meshes climbs itself; with compute.local_workers 0, only other machines do


def current_worker() -> Worker:
    return getattr(_here, "worker", None) or _first


def busy_campaigns() -> set[str]:
    return {w.campaign for w in _workers if w.campaign}


def create_campaign(area: str, order: str = "popularity", csv_text: str | None = None, start: bool = True,
                    without_youtube: bool = False, max_meshes: int | None = None, count_failures: bool = False,
                    check_new: bool = False, same_photos_once: bool = True, keep_better: bool = True,
                    mapper_below: int | None = None, mapper_more: int | None = None) -> dict:
    """List a Mountain Project area's boulder problems, in one request, and queue them for meshing.

    Args:
        area: its URL or id.
        order: "popularity" (MP's own ranking, as tick counts are only on each climb's own page),
            "rating" (average stars), or "data" (most photos and videos first).
        csv_text: a Route Finder export to use instead of fetching one.
        start: start meshing as soon as nothing else is queued.
        without_youtube: mesh climbs whose YouTube search hasn't been done, as when no volunteer can do it; otherwise
            they wait for it.
        max_meshes: stop once this many meshes are made, of its climbs in its order, a boulder meshed from several of
            them counting once (in_scope()); None to mesh all of it.
        count_failures: with max_meshes, count its climbs and boulders that failed or were skipped toward it too.
        check_new: for an area listed already, ask Mountain Project for its list again, and add the climbs added since.
        same_photos_once: don't mesh a boulder again from the photos its current mesh was made from (boulder_unchanged()).
        keep_better: when a rebuild from the photos the mesh before was made from places fewer of them, that mesh stays
            the current one (run_reconstruct()).
        mapper_below, mapper_more: when COLMAP's mapper places under mapper_below % of a climb's or boulder's photos, it
            tries mapper_more more seeds, and the model placing most is kept (meshopts.py); None for the defaults, 0 for never.

    An area listed already is taken up again rather than listed twice, with these settings: what it has done stays
    done, so that a larger max_meshes only goes on to the next ones. Its list stays as it was, but for the climbs
    check_new adds, after the rest, so that its order doesn't change. It's returned with "again" true, and "added".
    """
    if order not in CAMPAIGN_ORDERS:
        raise ValueError(f"order must be one of {', '.join(CAMPAIGN_ORDERS)}")
    max_meshes = clean_max_meshes(max_meshes)
    mapper = clean_mapper({"mapper_below": mapper_below, "mapper_more": mapper_more})
    aid = mp_mod.area_id(area or "")
    if not aid and not csv_text:
        raise ValueError("enter a Mountain Project area URL, like https://www.mountainproject.com/area/105720495/joshua-tree-national-park")
    old = q("SELECT * FROM campaigns WHERE area_id=? ORDER BY created DESC LIMIT 1", aid, one=True) if aid and not csv_text else None
    if old:
        added = add_new_climbs(old["id"], mp_mod.export_area_csv(aid, sort="popularity", fresh=True)) if check_new else 0
        q("UPDATE campaigns SET ord=?, max_meshes=?, count_failures=?, without_youtube=?, same_photos_once=?, keep_better=?, "
          "mapper_below=?, mapper_more=?, priority=? WHERE id=?", order, max_meshes, int(bool(count_failures)), int(bool(without_youtube)),
          int(bool(same_photos_once)), int(bool(keep_better)), mapper["mapper_below"], mapper["mapper_more"], next_priority(),
          old["id"], write=True)
        if start and old["status"] in ("stopped", "done"):
            q("UPDATE campaigns SET status='running', note=NULL, only_retries=0 WHERE id=?", old["id"], write=True)
        return {**q("SELECT * FROM campaigns WHERE id=?", old["id"], one=True), "again": True, "added": added}
    recs = mp_mod.parse_area_csv(csv_text) if csv_text else mp_mod.export_area_csv(aid, sort="popularity")
    if not recs:
        raise ValueError("Mountain Project lists no boulder problems under that area")
    common = os.path.commonprefix([c.area_path for c in recs])
    cid = new_id()
    note = "Mountain Project's export stops at 1000 climbs: these are the most popular 1000" if len(recs) >= 1000 else None
    q("INSERT INTO campaigns(id,name,area_id,ord,created,note,status,without_youtube,max_meshes,count_failures,same_photos_once,"
      "keep_better,mapper_below,mapper_more,priority) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", cid,
      common[-1].strip("* ") if common else f"area {aid}", aid, order, time.time(), note, "running" if start else "stopped",
      int(bool(without_youtube)), max_meshes, int(bool(count_failures)), int(bool(same_photos_once)), int(bool(keep_better)),
      mapper["mapper_below"], mapper["mapper_more"], next_priority(), write=True)
    add_new_climbs(cid, recs)
    return q("SELECT * FROM campaigns WHERE id=?", cid, one=True)


def add_new_climbs(cid: str, recs: list[Climb]) -> int:
    """Add to an area's list the climbs of a Mountain Project list it doesn't have, ranked after those it has, in
    that list's order. Returns how many."""
    have = {r["source_id"] for r in q("SELECT source_id FROM campaign_items WHERE campaign_id=?", cid)}
    last = q("SELECT COALESCE(MAX(rank), 0) n FROM campaign_items WHERE campaign_id=?", cid, one=True)["n"]
    new = []
    for c in recs:
        if c.source_id not in have:
            have.add(c.source_id)
            new.append(c)
    rows = [(cid, c.source_id, last + i + 1, c.name, c.grade, c.rating, c.url, boulder_key(c.area_path), json.dumps(c.to_dict()))
            for i, c in enumerate(new)]
    with _db_lock, db() as con:
        con.executemany("INSERT OR IGNORE INTO campaign_items(campaign_id,source_id,rank,name,grade,stars,url,boulder,data) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def next_priority() -> float:
    """The priority after every area's: an area entered, or entered again, goes after those there are, so that a list of
    them entered at once is taken in its order, top first (_claim())."""
    return (q("SELECT MAX(priority) p FROM campaigns", one=True)["p"] or 0.0) + 1.0


def clean_max_meshes(n) -> int | None:
    """An area's limit as given: a whole number above 0, or None (or empty) for none."""
    if n is None or n == "":
        return None
    if isinstance(n, bool) or not isinstance(n, (int, float, str)) or not str(n).strip().isdigit() or int(n) < 1:
        raise ValueError("how many meshes: a whole number above 0, or empty for all")
    return int(n)


def clean_mapper(options: dict) -> dict:
    """An area's settings for COLMAP's mapper as given (mapper_below: a whole % from 0 to 100; mapper_more: seeds, 0 to
    5), the defaults (meshopts.py) for those None or missing."""
    out = {"mapper_below": round(meshopts.MAPPER["retry_below"] * 100), "mapper_more": meshopts.MAPPER["more"]}
    for key, top in (("mapper_below", 100), ("mapper_more", 5)):
        v = options.get(key)
        if v is None or v == "":
            continue
        if isinstance(v, bool) or not str(v).strip().isdigit() or not 0 <= int(v) <= top:
            raise ValueError(f"{key}: a whole number from 0 to {top}")
        out[key] = int(v)
    return out


def mapper_of(camp: dict) -> dict:
    """An area's settings for COLMAP's mapper, as meshopts.using() takes them."""
    below, more = camp.get("mapper_below"), camp.get("mapper_more")
    return {"retry_below": (meshopts.MAPPER["retry_below"] if below is None else below / 100),
            "more": meshopts.MAPPER["more"] if more is None else more}


def set_campaign_options(camp_id: str, options: dict) -> dict:
    """Change how an area meshes: {"without_youtube": whether a climb is meshed before YouTube has been searched for it}.
    Turned on, its climbs waiting for volunteers are meshed with what they have, as after waiting too long.
    {"max_meshes": how many meshes to stop at, or None for all; "count_failures": whether failures count toward it}:
    an area done that these give more to carries on. {"same_photos_once", "keep_better", "mapper_below", "mapper_more"}: as
    create_campaign() has them."""
    limit = {}
    if "max_meshes" in options:
        limit["max_meshes"] = clean_max_meshes(options["max_meshes"])
    if "count_failures" in options:
        limit["count_failures"] = int(bool(options["count_failures"]))
    if limit:
        q(f"UPDATE campaigns SET {', '.join(f'{k}=?' for k in limit)} WHERE id=?", *limit.values(), camp_id, write=True)
        camp = q("SELECT * FROM campaigns WHERE id=?", camp_id, one=True)
        if camp["status"] == "done" and ordered_pending(camp):
            q("UPDATE campaigns SET status='running', note=NULL WHERE id=?", camp_id, write=True)
    for key in ("same_photos_once", "keep_better"):
        if key in options:
            q(f"UPDATE campaigns SET {key}=? WHERE id=?", int(bool(options[key])), camp_id, write=True)
    mapper = {k: options[k] for k in ("mapper_below", "mapper_more") if k in options and options[k] not in (None, "")}
    if mapper:
        clean = clean_mapper(mapper)
        q(f"UPDATE campaigns SET {', '.join(f'{k}=?' for k in mapper)} WHERE id=?", *(clean[k] for k in mapper), camp_id, write=True)
    if "without_youtube" in options:
        q("UPDATE campaigns SET without_youtube=? WHERE id=?", int(bool(options["without_youtube"])), camp_id, write=True)
        if options["without_youtube"]:
            q("UPDATE campaign_items SET waiting_since=0 WHERE campaign_id=? AND status='waiting'", camp_id, write=True)
    return q("SELECT * FROM campaigns WHERE id=?", camp_id, one=True)


def _in_order(camp: dict, items: list[dict]) -> list[dict]:
    """An area's climbs in its order: Mountain Project's popularity (its rank), stars, or photos and videos."""
    if camp["ord"] == "rating":
        return sorted(items, key=lambda i: (-(i["stars"] or 0), i["rank"]))
    if camp["ord"] == "data":
        return sorted(items, key=lambda i: (i["media"] is None, -(i["media"] or 0), i["rank"]))
    return sorted(items, key=lambda i: i["rank"])


def _units(camp: dict, items: list[dict]) -> list[list[dict]]:
    """An area's climbs in its order, grouped as each mesh is made of them: a boulder's together where it's meshed
    from all their photos (pooled()), each other climb alone; each group where its first climb comes."""
    together, kinds = settings().get("areas.meshes") == "boulders", boulder_kinds()
    units: dict[str, list[dict]] = {}
    for i in _in_order(camp, items):
        units.setdefault(i["boulder"] if together and pools_whole(i["boulder"], kinds) else "\0" + i["source_id"], []).append(i)
    return list(units.values())


UNFINISHED = ("pending", "running", "waiting", "harvested")


def limit_scope(camp: dict) -> tuple[set[str] | None, int]:
    """What an area with a limit (max_meshes) works through: its climbs and boulders (_units()) in its order, until as
    many as its limit have made a mesh or may yet, or failed too if it counts failures (count_failures). One that
    fails otherwise brings in the next. What's done stays within it, so that raising the limit only goes on. One done
    whose only meshes are old (or another climb's), not tried under the rules yet (old_waiting()), may yet make one,
    rather than having made one: it's queued again (queue_old()).

    Returns:
        (the climbs within it, by source_id, or None without a limit; how many meshes they've made).
    """
    if not camp.get("max_meshes"):
        return None, 0
    items = q("SELECT source_id, rank, stars, media, boulder, status, climb_id, rerun_rules FROM campaign_items WHERE campaign_id=?",
              camp["id"])
    stale = old_waiting()
    for i in items:
        if i["status"] == "done" and i["climb_id"] in stale and (i["rerun_rules"] or 0) < MESH_RULES:
            i["status"] = "pending"
    scope, n, made = set(), 0, 0
    for unit in _units(camp, items):
        if n >= camp["max_meshes"]:
            break
        scope |= {i["source_id"] for i in unit}
        statuses = {i["status"] for i in unit}
        made += "done" in statuses
        n += bool("done" in statuses or statuses & set(UNFINISHED) or camp.get("count_failures"))
    return scope, made


def in_scope(camp: dict) -> set[str] | None:
    """The climbs an area works through within its limit (limit_scope()), or None without one."""
    return limit_scope(camp)[0]


def following(item: dict) -> bool:
    """Whether an area's climb is queued for what follows from work done (ordered_pending())."""
    return item.get("redo") in ("own", "cluster") or (item.get("redo") == "rebuild" and item.get("was") == "harvested")


def ordered_pending(camp: dict, old: bool = False) -> list[dict]:
    """A campaign's waiting climbs, next first: within its limit, if it has one. Not those paused (pause_item()), nor
    (unless old) those queued again as their meshes are old (queue_old()), which go at low priority (_claim_old())."""
    items = q("SELECT rank,source_id,name,grade,stars,url,media,project_id,retry,redo,was,boulder,waiting_since,old_rerun FROM campaign_items "
              "WHERE campaign_id=? AND status='pending' AND paused IS NULL AND blocked IS NULL "
              f"{'' if old else 'AND COALESCE(old_rerun, 0)=0 '}ORDER BY rank", camp["id"])
    scope = in_scope(camp)
    if scope is None:
        items = _in_order(camp, items)
    else:       # a boulder's climbs one after another, so that its mesh is made before going on; and whatever the limit,
                # what follows from work done: meshing on their own those harvested for a boulder's mesh that won't be
                # (settle_groups()) or that one hardly showed (before keep_rest()), and pools of climbs (settle_clusters())
        items = [i for unit in _units(camp, [i for i in items if i["source_id"] in scope or following(i)]) for i in unit]
    # retries were asked for, and a climb that was interrupted comes next, to finish what it started; those queued again as
    # their meshes are old (queue_old()) after the rest
    items.sort(key=lambda i: (bool(i["old_rerun"]), not i["retry"], not i["project_id"]))
    return items


def set_item(camp_id: str, source_id: str, **fields) -> None:
    fields["updated"] = time.time()
    q(f"UPDATE campaign_items SET {', '.join(k + '=?' for k in fields)} WHERE campaign_id=? AND source_id=?",
      *fields.values(), camp_id, source_id, write=True)


def campaign_status(camp_id: str) -> str | None:
    r = q("SELECT status FROM campaigns WHERE id=?", camp_id, one=True)
    return r["status"] if r else None


def read_ahead(camp: dict) -> None:
    """For "data" order: read waiting climbs' pages, most popular first, until DATA_WINDOW of them
    have a known amount of data. Each is a page its climb's turn would read anyway, and well below
    a crawl: one more per climb meshed, after the first few."""
    items = ordered_pending(camp)
    known = sum(i["media"] is not None for i in items)
    for it in items:
        if known >= DATA_WINDOW or campaign_status(camp["id"]) != "running":
            return
        if it["media"] is not None:
            continue
        try:
            rec = climbs_mod.fetch_climb_url(it["url"], include_children=False)[0]
            set_item(camp["id"], it["source_id"], climb_id=upsert_climb(rec),
                     media=len(rec.images) + VIDEO_WORTH * len(rec.videos))
            known += 1
        except Exception as e:
            set_item(camp["id"], it["source_id"], status="failed", note=f"couldn't read its page: {e}")


def last_failure(pid: str, prefix: str = "FAILED: ") -> str:
    for line in reversed(read_log(pid).splitlines()):
        if line.startswith(prefix):
            return line[len(prefix):]
    return "failed; see its project's log"


def run_campaign_item(camp: dict, item: dict) -> None:
    """Harvest and mesh one climb, on its own, whatever else on its boulder has a mesh: a mesh made for a problem on
    one side of a rock isn't one of a problem on the other side. Whatever this finishes is saved like any other
    mesh, so stopping later loses nothing but the climb in progress."""
    if item.get("redo"):
        return run_redo(camp, item)
    cid, sid = camp["id"], item["source_id"]
    pid = item["project_id"]
    resume = bool(pid and q("SELECT id FROM projects WHERE id=?", pid) and (PROJECTS / pid).is_dir())
    jobctl.begin(low_priority=True)
    try:
        set_item(cid, sid, status="running", project_id=pid if resume else None, note=None)
        full = q("SELECT data,climb_id,boulder FROM campaign_items WHERE campaign_id=? AND source_id=?", cid, sid, one=True)
        c = Climb.from_dict(json.loads(full["data"]))
        climb_id = full["climb_id"] or upsert_climb(c)
        have = q(f"SELECT l.mesh_id FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id WHERE l.climb_id=? AND COALESCE(l.note,'') "
                 f"NOT LIKE ? AND NOT {OLD_MESH} LIMIT 1", climb_id, SHARED + "%", one=True)    # not one old, or another's
        made = q("SELECT id FROM meshes WHERE source=? LIMIT 1", f"project:{pid}", one=True) if resume else None
        with_boulder = (boulder_mesh_with(climb_id, c.area_path) if not (made or have) and settings().get("areas.meshes") == "boulders"
                        else None)
        if made:    # interrupted in the moment between saving its mesh and recording that
            q("UPDATE projects SET status='done', mesh_id=? WHERE id=?", made["id"], pid, write=True)
            set_item(cid, sid, status="done", climb_id=climb_id, mesh_id=made["id"])
        elif have:
            set_item(cid, sid, status="done", climb_id=climb_id, mesh_id=have["mesh_id"], note="already had a mesh")
        elif with_boulder:
            set_item(cid, sid, status="done", climb_id=climb_id, mesh_id=with_boulder, note="already meshed with its boulder")
        else:
            if not resume:
                pid = new_id()
                (PROJECTS / pid / "photos").mkdir(parents=True)
                q("INSERT INTO projects(id,name,created,kind,climb_id,status) VALUES(?,?,?,?,?,'queued')",
                  pid, f"{c.name} (harvest)"[:80], time.time(), "harvest", climb_id, write=True)
            set_item(cid, sid, climb_id=climb_id, project_id=pid)
            if item.get("retry"):
                append_log(pid, "-- trying again")
            if full["boulder"] and settings().get("areas.meshes") == "boulders":
                boulder_kind(full["boulder"], climb_area_url(c), lambda s: append_log(pid, s))
            pool = pooled(cid, full["boulder"])
            try:
                run_harvest(pid, set(HARVEST_SOURCES), not pool, resume=resume, wait=may_wait(item),
                            need_youtube=not camp.get("without_youtube"))
            except Cancelled:
                q("UPDATE projects SET status='stopped' WHERE id=?", pid, write=True)
                set_item(cid, sid, status="pending", note="stopped partway: carries on from where it got to when resumed")
                return
            except compute.ComputeUnavailable as e:
                q("UPDATE projects SET status='stopped' WHERE id=?", pid, write=True)
                set_item(cid, sid, status="pending", note=f"waiting for a machine to mesh it on: {e}")
                raise
            p = q("SELECT status,mesh_id,harvest FROM projects WHERE id=?", pid, one=True)
            if p["status"] == "waiting" or (pool and p["status"] != "failed" and (
                    (may_wait(item) and VOLUNTEERS.pending("harvest", pid)) or unchecked(camp, pid))):
                wait_for_volunteers(cid, sid, pid, item.get("waiting_since") if may_wait(item) else None)
                return
            if pool and p["status"] != "failed":    # its boulder is meshed from it and its neighbours' (settle_boulder())
                append_log(pid, "harvested for its boulder's mesh, made from every photo of the climbs on it once they're all harvested")
                set_item(cid, sid, status="harvested", note=HARVESTED_NOTE)
                return
            if p["status"] != "failed":     # kept if it failed, so that trying again starts from its last finished step
                shutil.rmtree(PROJECTS / pid / "work", ignore_errors=True)   # most of a job's disk use, and only useful to itself
            if p["status"] == "done":
                set_item(cid, sid, status="done", mesh_id=p["mesh_id"])
            elif p["status"] == "harvested":
                set_item(cid, sid, status="skipped", note="photos don't show enough different viewpoints")
            elif too_few_placed(pid):
                set_item(cid, sid, status="skipped", note=too_few_placed(pid))
            else:
                set_item(cid, sid, status="failed", note=last_failure(pid))
                if not p["harvest"]:
                    _outages.append((cid, sid))
                    if len(_outages) >= OUTAGE_LIMIT:
                        for oc, os_ in _outages:
                            set_item(oc, os_, status="pending", note="will be tried again when resumed")
                        q("UPDATE campaigns SET status='stopped', note=? WHERE id=?", f"stopped after {OUTAGE_LIMIT} climbs in a row "
                          f"failed before finishing a harvest; last error: {last_failure(pid)}", cid, write=True)
                        _outages.clear()
                    return
            link_after(cid, sid)
        _outages.clear()
    finally:
        q("UPDATE campaign_items SET retry=0, old_rerun=CASE WHEN status IN ('harvested','waiting') THEN old_rerun ELSE 0 END "
          "WHERE campaign_id=? AND source_id=? AND status NOT IN ('pending','running')", cid, sid, write=True)    # those go on at low priority


HARVESTED_NOTE = "harvested for its boulder's mesh, made once the area's climbs on it are all harvested"


def pooled(camp_id: str, boulder: str | None) -> bool:
    """Whether an area's climb on this boulder is only harvested, for its boulder's mesh from every photo of the climbs on
    it (areas.meshes "boulders"), rather than meshed on its own: when the area lists another climb on the boulder."""
    if not boulder or settings().get("areas.meshes") != "boulders" or not pools_whole(boulder):
        return False
    return q("SELECT count(*) AS n FROM campaign_items WHERE campaign_id=? AND boulder=?", camp_id, boulder, one=True)["n"] >= 2


def settle_boulder(camp_id: str, boulder: str | None) -> bool:
    """Once none of an area's climbs on a boulder is still to be harvested (pending, running or waiting for volunteers),
    queue meshing the boulder from all their photos (run_boulder()), on the highest-ranked of them harvested for it.

    Returns:
        Whether it was queued.
    """
    if not boulder or not pools_whole(boulder):
        return False
    items = q("SELECT source_id, status, old_rerun FROM campaign_items WHERE campaign_id=? AND boulder=? ORDER BY rank", camp_id, boulder)
    camp = q("SELECT * FROM campaigns WHERE id=?", camp_id, one=True) or {}
    scope = in_scope(camp)
    if scope is not None:   # those past an area's limit (as after lowering it) aren't waited for
        items = [i for i in items if i["source_id"] in scope or i["status"] in ("running", "waiting", "harvested")]
    if camp.get("status") != "running":     # one not running remakes only its old meshes: its own list isn't waited for
        items = [i for i in items if i["old_rerun"] or i["status"] not in ("pending", "waiting")]
    if any(i["status"] in ("pending", "running", "waiting") for i in items):
        return False
    harvested = [i for i in items if i["status"] == "harvested"]
    if not harvested:
        return False
    mesh = boulder_unchanged(boulder) if camp.get("same_photos_once", 1) != 0 else None
    if mesh:    # as when a climb harvested for it resumes after its mesh was made: that mesh is theirs
        q("UPDATE campaign_items SET status='done', mesh_id=?, retry=0, note=?, updated=? WHERE campaign_id=? AND boulder=? "
          "AND status='harvested'", mesh, SAME_PHOTOS_NOTE, time.time(), camp_id, boulder, write=True)
        return False
    set_item(camp_id, harvested[0]["source_id"], status="pending", retry=1, redo="boulder", was="harvested", redo_started=0,
             note=f"queued to {REDO_NAMES['boulder']}")
    return True


SAME_PHOTOS_NOTE = "already meshed with its boulder, from these same photos"


def boulder_meshed(camp_id: str, source_id: str, end: dict, failed: bool) -> dict:
    """After meshing a boulder for an area's climbs harvested for it: each of them, this one too, is done with the
    boulder's mesh, or skipped or failed with it, so that none waits for it any more."""
    boulder = q("SELECT boulder FROM campaign_items WHERE campaign_id=? AND source_id=?", camp_id, source_id, one=True)["boulder"]
    proj = boulder_project(boulder) if boulder else None
    if not failed and proj and proj["status"] == "done" and proj["mesh_id"]:
        status, mesh = "done", proj["mesh_id"]
    else:
        status, mesh = ("skipped" if not failed and proj and proj["status"] == "harvested" else "failed"), None
    note = (f"meshed with its boulder: {end['note'].removeprefix('its boulder meshed from ')}"
            if status == "done" and end["note"].startswith("its boulder meshed from ") else end["note"])
    q("UPDATE campaign_items SET status=?, mesh_id=COALESCE(?, mesh_id), note=?, retry=0, old_rerun=0, updated=? "
      "WHERE campaign_id=? AND boulder=? AND status='harvested' AND source_id!=?", status, mesh, note, time.time(), camp_id, boulder,
      source_id, write=True)
    return {**end, "status": status, "note": note, **({"mesh_id": mesh} if mesh else {})}


def may_wait(item: dict) -> bool:
    """Whether an area's climb may wait for volunteers: until VOLUNTEER_WAIT after it first did."""
    return not item.get("waiting_since") or time.time() - item["waiting_since"] < VOLUNTEER_WAIT


def youtube_checked(summary: dict | None) -> bool:
    """Whether a harvest searched YouTube for its climb, here or through a volunteer (harvests from before that was
    recorded did)."""
    return bool((summary or {}).get("youtube_checked", True))


def unchecked(camp: dict, pid: str) -> bool:
    """Whether an area's climb is still to be searched for on YouTube, which its area needs before meshing it."""
    if camp.get("without_youtube"):
        return False
    p = q("SELECT harvest FROM projects WHERE id=?", pid, one=True)
    return bool(p and p["harvest"]) and not youtube_checked(json.loads(p["harvest"]))


def wait_for_volunteers(cid: str, sid: str, pid: str, since: float | None) -> None:
    """Leave an area's climb waiting for what volunteers are fetching for it, rather than mesh it twice: once
    without it, and again with it; or for its YouTube search, which its area needs before meshing it. volunteer_ready()
    queues it to harvest again and mesh once it has all come, and settle_waiting() after VOLUNTEER_WAIT. What came
    while it was still being harvested, too late for that harvest, is given again now (the broker's resettle())."""
    n = VOLUNTEERS.pending("harvest", pid)
    note = (f"waiting for volunteers: {n} YouTube search(es) and video(s) to come" if n else
            "waiting for its YouTube search, which no volunteer has been asked for (there are none): add one, or let its "
            "area mesh without")
    set_item(cid, sid, status="waiting", waiting_since=since or time.time(), redo=None, retry=0, redo_started=0, note=note)
    VOLUNTEERS.resettle("harvest", pid)


def settle_waiting(camp_id: str, only_old: bool = False) -> None:
    """An area's climbs that have waited for volunteers for VOLUNTEER_WAIT: harvested again and meshed with what's come.
    Only those queued again as their meshes are old (queue_old()) if only_old."""
    q("UPDATE campaign_items SET status='pending', retry=1, redo='more+rebuild', was='waiting', redo_started=0, note=? "
      f"WHERE campaign_id=? AND status='waiting' AND waiting_since<?{' AND old_rerun=1' if only_old else ''}",
      f"waited {VOLUNTEER_WAIT / 3600:.0f} h for volunteers: meshing it with what's come", camp_id, time.time() - VOLUNTEER_WAIT,
      write=True)


# ------------------------------------------------------------------ how much room the data takes (storage.cap_gb)
_usage: dict = {}       # {total, by: {name: bytes}, checked}


USAGE_PARTS = {"photos": "photos", "rejected": "rejected photos", "videos": "videos", "masks": "people masks",
               "work": "reconstructions' work"}    # a project's folders, and what the admin page calls each


def measure_usage() -> dict:
    """How much DATA_DIR holds, by what (the admin page's bars): projects' photos, the photos their filter rejected,
    videos (fetched, in the cache, and uploaded to projects), meshes, people masks, what reconstructions left, the
    web cache, models, the rest of the cache, and everything else (the databases, their snapshots, logs). With how many
    climbs, meshes, photos and videos that is, and the disk's room."""
    def size(path: Path, files: list | None = None) -> int:
        total = 0
        for root, _, names in os.walk(path):
            for f in names:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    continue
                if files is not None:
                    files.append(f)
        return total
    by = dict.fromkeys(USAGE_PARTS.values(), 0)
    counted = {"photos": [], "rejected": [], "videos": []}
    projects = [d for d in PROJECTS.iterdir() if d.is_dir()] if PROJECTS.exists() else []
    for d in projects:
        for part, name in USAGE_PARTS.items():
            by[name] += size(d / part, counted.get(part))
    fetched: list = []
    cache = {name: size(DATA / "cache" / sub, fetched if sub == "videos" else None)
             for sub, name in (("videos", "fetched videos"), ("http", "web cache"), ("models", "models"))}
    by["videos"] += cache.pop("fetched videos")
    by.update(meshes=size(MESHES), **cache)
    by["other cache"] = size(DATA / "cache") - sum(size(DATA / "cache" / sub) for sub in ("videos", "http", "models"))
    by["other"] = size(DATA) - sum(by.values())
    names = {d.name for d in projects}
    climbs = {r["climb_id"] for r in q("SELECT id, climb_id FROM projects WHERE climb_id IS NOT NULL") if r["id"] in names}
    meshes = sum(1 for d in MESHES.iterdir() if d.is_dir()) if MESHES.exists() else 0
    disk = shutil.disk_usage(DATA)
    _usage.update(total=sum(by.values()), by=by, checked=time.time(), free=disk.free, disk=disk.total,
                  counts={"climbs": len(climbs), "projects": len(projects), "meshes": meshes, "photos": len(counted["photos"]),
                          "rejected photos": len(counted["rejected"]), "videos": len(counted["videos"]) + len(fetched)},
                  # a climb's data: its projects' folders (photos, rejected, masks, videos, work), over the climbs that have any
                  per_climb=sum(by[n] for n in USAGE_PARTS.values()) / len(climbs) if climbs else None,
                  per_mesh=by["meshes"] / meshes if meshes else None)
    return _usage


def data_cap() -> float:
    """The most the data may take, in bytes (storage.cap_gb), or 0 for no limit."""
    return settings().get("storage.cap_gb") * 2**30


def full() -> str | None:
    """Why no more is fetched (the cap is reached), or None."""
    cap = data_cap()
    if cap and _usage.get("total", 0) > cap:
        return (f"the server is out of room ({_usage['total'] / 2**30:.1f} of {cap / 2**30:g} GB): "
                "waiting for some to be made")
    return None


def warn_room() -> float:
    """How little room left under the cap is told of, in bytes: storage.warn_gb if set, else storage.warn_percent of the
    cap (0 without a cap)."""
    return (settings().get("storage.warn_gb") * 2**30) or data_cap() * settings().get("storage.warn_percent") / 100


def room_left() -> dict:
    """What the admin page says of the room left under the cap: {left, warn_at, climbs}, climbs being how many climbs'
    worth (their data and a mesh each, on average) the room left, and the warning's, are."""
    cap, at = data_cap(), warn_room()
    if not cap or "total" not in _usage:
        return {}
    each = (_usage.get("per_climb") or 0) + (_usage.get("per_mesh") or 0)
    return {"left": cap - _usage["total"], "warn_at": at, "climbs": (cap - _usage["total"]) / each if each else None,
            "warn_climbs": at / each if each else None}


_room_warned = False    # told of: not again until there's room again (half as much more than the warning's)


def check_room() -> None:
    """After measuring: tell your phone (notify.py) once the room left under the cap is under warn_room(), and once
    the cap is reached, each once until there's room again."""
    global _room_warned
    room = room_left()
    if not room:
        return
    left, at, gb = room["left"], room["warn_at"], lambda b: f"{b / 2**30:.1f} GB"
    if left <= at and not _room_warned:
        _room_warned = True
        n = round(room["climbs"] or 0)
        worth = (f", about {n} climb's worth" if n == 1 else f", about {n} climbs' worth") + " at their average" \
            if room["climbs"] and left > 0 else ""
        NOTIFIER.send("BoulderMesh is out of room" if left <= 0 else "BoulderMesh is nearly out of room",
                      (f"{gb(_usage['total'])} of its {data_cap() / 2**30:g} GB cap: areas and volunteers wait until there's room."
                       if left <= 0 else f"{gb(left)} left under its {data_cap() / 2**30:g} GB cap{worth}. At the cap, areas and "
                       "volunteers wait until there's room.") + " The admin page's Room for data shows what takes it.",
                      priority=4, tags=["floppy_disk"])
    elif left > 1.5 * at:
        _room_warned = False


# ------------------------------------------------------------------ offloading to the backup (offload.py)
# Above storage.offload_percent of the cap, what the backup on another machine holds and nothing here uses any more is
# deleted, oldest first in each kind, in this order, down to that %: photos the filter rejected (after REJECTED_DAYS);
# fetched videos, once every climb they're of has been meshed (or skipped, or failed) and beta has been found in each of
# its meshes; the photos and people masks of climbs and boulders meshed MESHED_DAYS ago, or skipped SKIPPED_DAYS ago,
# none of whose boulder's climbs are still to be harvested or meshed; and the big files of older versions of meshes
# replaced MESHED_DAYS ago. The web cache's pages older than CACHE_DAYS go too, without the backup (it keeps none).
REJECTED_DAYS, MESHED_DAYS, SKIPPED_DAYS, CACHE_DAYS = 3, 14, 30, 30
MESH_FILE_MIN = 2**20     # an older mesh's files this big or bigger (its mesh and textures, not its small records)
OFFLOAD_KINDS = ("rejected photos", "videos", "photos", "older meshes")


class PhotosAway(compute.ComputeUnavailable):
    """What a job needs is in the backup: it's been asked for back, and the job waits for it, as for a machine."""


class LostData(Exception):
    """What a job needs was offloaded, and the backup has lost it (offload.check_manifest()): the job is blocked, not
    waiting, until the admin page says to go on without it (lost_decide()). paths: those lost."""

    def __init__(self, what: str, paths: list[str]):
        super().__init__(f"{what}: lost from the backup: {', '.join(paths[:3])}{f' and {len(paths) - 3} more' if len(paths) > 3 else ''}")
        self.paths = paths


def need_back(pids, what: str) -> None:
    """Ask for the photos and masks of these projects back, if they're offloaded, and raise PhotosAway if any were; or
    LostData if any the backup lost, that nothing was approved to go on without, without asking for the rest."""
    paths = [f"projects/{p}/{part}" for p in pids for part in ("photos", "masks")]
    gone = offload_mod.lost(q, paths)
    if gone:
        for r in gone:
            q("UPDATE offloads SET blocked=? WHERE path=?", what, r["path"], write=True)
        raise LostData(what, [r["path"] for r in gone])
    asked = offload_mod.want(DATA, q, paths)
    if asked:
        raise PhotosAway(f"{what}: its photos are in the backup, and have been asked for back, which the backup's next "
                         "run brings (half-hourly, with deploy/bouldermesh-restore.timer)")


def photos_away(pid: str) -> bool:
    return offload_mod.away(q, f"projects/{pid}/photos")


def _in_play() -> tuple[set[str], set[str]]:
    """Climbs still to be harvested or meshed, and the boulders (area paths) they're on, whose climbs' photos a
    boulder's mesh may still pool."""
    climbs = {r["climb_id"] for r in q("SELECT climb_id FROM campaign_items WHERE climb_id IS NOT NULL AND status IN "
                                       "('pending','running','waiting','harvested')")}
    climbs |= {r["climb_id"] for r in q("SELECT climb_id FROM projects WHERE climb_id IS NOT NULL AND status IN "
                                         "('new','queued','harvesting','running','waiting','stopped','interrupted','failed')")}
    boulders = {r["area_path"] for r in q(f"SELECT area_path FROM climbs WHERE id IN ({','.join('?' * len(climbs))})", *climbs)} if climbs else set()
    boulders |= {r["boulder"] for r in q("SELECT boulder FROM projects WHERE kind='boulder' AND status IN "
                                         "('queued','harvesting','running','stopped','interrupted')") if r["boulder"]}
    return climbs, boulders


def _beta_found(mid: str) -> bool:
    return (beta_mod.load(MESHES / mid) or {}).get("status") == "done"


def offload_candidates() -> list[tuple[str, str, str]]:
    """What may be offloaded now, in the order it goes: (path under the data folder, kind, what it's of)."""
    now, out = time.time(), []
    busy, boulders = _in_play()
    projects = {p["id"]: p for p in q("SELECT p.id, p.kind, p.status, p.created, p.mesh_id, p.climb_id, p.boulder, c.area_path, "
                                      "m.created AS meshed FROM projects p LEFT JOIN climbs c ON c.id=p.climb_id "
                                      "LEFT JOIN meshes m ON m.id=p.mesh_id")}
    # photos the filter rejected
    for d in sorted(PROJECTS.iterdir(), key=lambda d: d.name) if PROJECTS.exists() else []:
        rej = d / "rejected"
        files = [f for f in rej.iterdir() if f.is_file()] if rej.is_dir() else []
        if files and max(f.stat().st_mtime for f in files) < now - REJECTED_DAYS * 86400:
            out.append((f"projects/{d.name}/rejected", "rejected photos", d.name))
    # fetched videos, once every climb they're of was meshed (or not) and beta found in each of its meshes
    of: dict[str, set[str]] = {}
    for r in q("SELECT climb_id, url FROM climb_videos"):
        vid = youtube_mod.video_id(r["url"])
        if vid:
            of.setdefault(vid, set()).add(r["climb_id"])
    tried = {r["climb_id"] for r in q("SELECT DISTINCT climb_id FROM projects WHERE kind='harvest' AND harvest IS NOT NULL AND climb_id IS NOT NULL")}
    own: dict[str, list[str]] = {}
    for r in q("SELECT l.climb_id, l.mesh_id FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id WHERE m.replaced_by IS NULL "
               "AND COALESCE(l.note,'') NOT LIKE ?", SHARED + "%"):
        own.setdefault(r["climb_id"], []).append(r["mesh_id"])
    of_boulder = {r["boulder"]: r["id"] for r in q("SELECT id, boulder FROM meshes WHERE boulder IS NOT NULL AND replaced_by IS NULL")}
    area = {r["id"]: r["area_path"] for r in q("SELECT id, area_path FROM climbs")}
    vdir = youtube_mod.VIDEO_CACHE
    for f in sorted((x for x in vdir.iterdir() if x.suffix not in (".json", ".part")), key=lambda x: x.stat().st_mtime) if vdir.is_dir() else []:
        vid = f.name.split(".")[0]
        climbs = of.get(vid, set())
        if not climbs:
            done = f.stat().st_mtime < now - CACHE_DAYS * 86400     # of no climb known: once it's sat a while
        else:
            done = all(c not in busy and c in tried and all(_beta_found(m) for m in own.get(c, []) + (
                [of_boulder[area[c]]] if area.get(c) in of_boulder else [])) for c in climbs)
        if done:
            out.append((f"cache/videos/{f.name}", "videos", vid))
    # photos and masks of climbs and boulders meshed a while ago, or skipped a while ago, that nothing still pools
    for p in sorted(projects.values(), key=lambda p: p["created"] or 0):
        boulder = p["boulder"] if p["kind"] == "boulder" else p["area_path"]
        if p["kind"] not in ("harvest", "boulder") or p["climb_id"] in busy or boulder in boulders:
            continue
        meshed = p["status"] == "done" and p["meshed"] and p["meshed"] < now - MESHED_DAYS * 86400
        skipped = p["status"] == "harvested" and p["kind"] == "harvest" and (p["created"] or now) < now - SKIPPED_DAYS * 86400
        if meshed or skipped:
            for part in ("photos", "masks"):
                d = PROJECTS / p["id"] / part
                if d.is_dir() and any(d.iterdir()):
                    out.append((f"projects/{p['id']}/{part}", "photos", p["id"]))
    # older versions of meshes, replaced a while ago: their big files
    for m in q("SELECT m.id FROM meshes m JOIN meshes n ON n.id=m.replaced_by WHERE n.created < ? ORDER BY m.created",
               now - MESHED_DAYS * 86400):
        d = MESHES / m["id"]
        for f in sorted(d.iterdir()) if d.is_dir() else []:
            if f.is_file() and f.suffix != ".json" and f.stat().st_size >= MESH_FILE_MIN and offload_mod.PATH.fullmatch(f"meshes/{m['id']}/{f.name}"):
                out.append((f"meshes/{m['id']}/{f.name}", "older meshes", m["id"]))
    return out


def prune_web_cache(need: float) -> int:
    """Delete web cache files older than CACHE_DAYS, oldest first, until need bytes are freed: searches and pages,
    fetched again if wanted. Not the photos kept for good (cache/http-kept). Returns the bytes freed."""
    root, freed = DATA / "cache" / "http", 0
    old = sorted(((f.stat().st_mtime, f) for f in root.rglob("*") if f.is_file()), key=lambda x: x[0]) if root.is_dir() else []
    for t, f in old:
        if t >= time.time() - CACHE_DAYS * 86400 or freed >= need:
            break
        n = f.stat().st_size
        f.unlink(missing_ok=True)
        freed += n
    return freed


def offload_step() -> dict:
    """What making room does after each measuring: put back what the backup sent back; and above storage.offload_percent
    of the cap, delete what was marked before the backup's last good copy began and is still to go, the web cache's
    old pages, and mark more to go, down to that %. Returns what it did."""
    did = {"back": offload_mod.take_restores(DATA, q, log_server)}
    did["manifest"] = take_manifest()
    cap, pct = data_cap(), settings().get("storage.offload_percent")
    marked = q("SELECT path, bytes FROM offloads WHERE done IS NULL")
    if not cap or not pct or "total" not in _usage:
        if marked:      # nothing's to go: what was marked stays here
            offload_mod.drop(q, [r["path"] for r in marked])
            offload_mod.write_lists(DATA, q)
        return did
    for r in q("SELECT path FROM offloads WHERE done IS NOT NULL AND wanted IS NULL"):    # made again since (harvested anew)
        p = DATA / r["path"]
        if (p.is_file() and p.stat().st_size) or (p.is_dir() and any(p.iterdir())):
            offload_mod.drop(q, [r["path"]])
    need = _usage["total"] - cap * pct / 100
    if need <= 0:
        offload_mod.drop(q, [r["path"] for r in marked])
        offload_mod.write_lists(DATA, q)
        return did
    quiet = manifest_quiet()
    if quiet:       # nothing goes, nor is marked, until the backup says again what it holds
        did["paused"] = quiet
        offload_mod.write_lists(DATA, q)
        return did
    eligible = {c[0]: c for c in offload_candidates()}
    did["freed"] = offload_mod.finish(DATA, q, lambda row: row["path"] in eligible, log_server, need)
    did["cache"] = prune_web_cache(need - did["freed"])
    left = need - did["freed"] - did["cache"] - sum(r["bytes"] or 0 for r in q("SELECT bytes FROM offloads WHERE done IS NULL"))
    have = {r["path"] for r in q("SELECT path FROM offloads")}
    for path, kind, ref in eligible.values():
        if left <= 0:
            break
        if path not in have:
            left -= offload_mod.mark(DATA, q, path, kind, ref)
    offload_mod.write_lists(DATA, q)
    if did["freed"] or did["cache"]:
        measure_usage()
    return did


MANIFEST_HOURS = 26     # the backup sends a manifest each night: without a good one for longer, offloading pauses
MANIFEST_DONE = DATA / "backup-manifest.json"   # what the last manifest taken said (take_manifest())
MANIFEST_WAIT = DATA / "backup-manifest.since"  # when this server began waiting for one, before its first
_manifest_told: dict = {"quiet": False}


def manifest_state() -> dict:
    """What the backup's manifests have said: {received (when the last good one came), began (when its copy began), files,
    lost (paths newly lost by it), confirmed, hashes, bad (why the last one sent wasn't taken), waiting_since}."""
    st = _read_json(MANIFEST_DONE) or {}
    if not st.get("received"):
        try:
            st["waiting_since"] = float(MANIFEST_WAIT.read_text())
        except (OSError, ValueError):
            st["waiting_since"] = time.time()
            try:
                MANIFEST_WAIT.write_text(str(st["waiting_since"]))
            except OSError:
                pass
    return st


def manifest_quiet(now: float | None = None) -> str | None:
    """Why offloading is paused, if the backup hasn't sent a good manifest for more than MANIFEST_HOURS (or, before its
    first, since this server began waiting for one); else None."""
    st, now = manifest_state(), time.time() if now is None else now
    since = st.get("received") or st.get("waiting_since") or now
    if now - since <= MANIFEST_HOURS * 3600:
        return None
    return (f"no manifest from the backup for {(now - since) / 3600:.0f} h" if st.get("received") else
            f"no manifest from the backup yet, {(now - since) / 3600:.0f} h after waiting for one began") + \
        ": offloading is paused, as what it holds can't be checked"


def take_manifest() -> dict | None:
    """Take the manifest the backup's handshake left (offload.MANIFEST), if it's new: compare it with what was offloaded
    (offload.check_manifest()), and tell your phone at once of anything lost. Then, or if none came for MANIFEST_HOURS,
    tell your phone once that offloading is paused. Returns what checking it found, or None."""
    path = DATA / offload_mod.MANIFEST
    out = None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    st = manifest_state()
    if mtime and mtime > (st.get("taken") or 0):
        got = offload_mod.read_manifest(path.read_text(errors="replace"))
        if got is None:
            st.update(taken=mtime, bad=f"the manifest sent {time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))} isn't whole: not taken")
            log_server(f"offload: {st['bad']}")
        else:
            out = offload_mod.check_manifest(DATA, q, got)
            st.update(taken=mtime, received=mtime, began=got["began"], bad=None,
                      **{k: out[k] for k in ("files", "confirmed", "hashes")}, lost=out["lost"])
            log_server(f"offload: the backup's manifest lists {out['files']} file(s): {out['confirmed']} path(s) marked confirmed, "
                       f"{out['hashes']} SHA-1(s) checked, {len(out['lost'])} lost")
            if out["lost"]:
                threading.Thread(target=NOTIFIER.send, args=(
                    "BoulderMesh: the backup lost offloaded data",
                    f"{plural(len(out['lost']), 'path')} offloaded to the backup aren't held there as they were: "
                    f"{', '.join(out['lost'][:4])}{' and more' if len(out['lost']) > 4 else ''}. Jobs that need them are blocked until "
                    "the admin page's Backup section says what to do."), kwargs={"priority": 5, "tags": ["rotating_light"]},
                    daemon=True).start()
            offload_mod.write_lists(DATA, q)
        tmp = MANIFEST_DONE.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v for k, v in st.items() if k != "waiting_since"}))
        tmp.replace(MANIFEST_DONE)
    quiet = manifest_quiet() if data_cap() and settings().get("storage.offload_percent") else None    # only while offloading
    if quiet and not _manifest_told["quiet"]:
        _manifest_told["quiet"] = True
        threading.Thread(target=NOTIFIER.send, args=("BoulderMesh: no word from the backup", quiet + ". Is the nightly backup "
                                                     "running, with the new deploy/backup.sh?"),
                         kwargs={"priority": 4, "tags": ["warning"]}, daemon=True).start()
    elif not quiet:
        _manifest_told["quiet"] = False
    return out


def lost_blocks() -> list[dict]:
    """What the backup lost blocks, for the admin page: each area's climb blocked (campaign_items.blocked), and each
    other job a lost path blocked; each with the paths, what they are, and why they're lost."""
    lost = {r["path"]: r for r in q("SELECT path, kind, ref, lost_why, blocked, approved FROM offloads WHERE lost IS NOT NULL")}
    out, held = [], set()
    for i in q("SELECT ci.campaign_id, ci.source_id, ci.name, ci.blocked, c.name AS area FROM campaign_items ci "
               "JOIN campaigns c ON c.id=ci.campaign_id WHERE ci.blocked IS NOT NULL ORDER BY ci.updated"):
        b = _block_of(i["blocked"])
        held |= set(b["paths"])
        out.append({"campaign": i["campaign_id"], "source": i["source_id"], "what": f"{i['area']}: {i['name']}", "why": b["why"],
                    "paths": [{"path": p, "kind": (lost.get(p) or {}).get("kind"), "lost_why": (lost.get(p) or {}).get("lost_why")}
                              for p in b["paths"]]})
    for p, r in lost.items():
        if r["blocked"] and not r["approved"] and p not in held:
            out.append({"path": p, "what": r["blocked"], "why": f"{r['blocked']}: lost from the backup",
                        "paths": [{"path": p, "kind": r["kind"], "lost_why": r["lost_why"]}]})
    return out


def _block_of(text: str | None) -> dict:
    try:
        b = json.loads(text or "")
        return {"why": str(b.get("why") or ""), "paths": list(b.get("paths") or [])}
    except (ValueError, AttributeError):
        return {"why": text or "", "paths": []}


def block_item(camp_id: str, source_id: str, e: LostData) -> None:
    """An area's climb whose job needs what the backup lost: left to go, but not taken (ordered_pending()), until the admin
    page says to re-mesh without it or leave it as it is (lost_decide()); nothing of it is made or replaced meanwhile."""
    set_item(camp_id, source_id, status="pending", blocked=json.dumps({"why": str(e), "paths": e.paths}), note=str(e))


def lost_decide(verdict: str, campaign: str | None = None, source: str | None = None, path: str | None = None) -> str:
    """The admin page's answer for a job what the backup lost blocks: "remesh" goes on without it (the paths approved:
    a job that needs them takes them as they are here, none), "leave" leaves it as it is (an area's climb back as it was,
    not queued again for this; a path's block forgotten, so that it's asked again if another job needs it)."""
    if verdict not in ("remesh", "leave"):
        raise ValueError("verdict must be remesh or leave")
    if campaign and source:
        i = q("SELECT name, blocked, was, redo, status, mesh_id FROM campaign_items WHERE campaign_id=? AND source_id=?", campaign, source, one=True)
        if not i or not i["blocked"]:
            raise ValueError("that climb isn't blocked")
        paths = _block_of(i["blocked"])["paths"]
        if verdict == "remesh":
            for p in paths:
                q("UPDATE offloads SET approved=? WHERE path=?", time.time(), p, write=True)
            set_item(campaign, source, blocked=None, note=f"to go on without what the backup lost ({', '.join(paths)}), as approved")
            return f"{i['name']} goes on without what the backup lost"
        for p in paths:     # answered: another job needing them asks again
            q("UPDATE offloads SET blocked=NULL WHERE path=?", p, write=True)
        status = i["was"] if i["redo"] else ("done" if i["mesh_id"] else "skipped")
        set_item(campaign, source, blocked=None, status=status or "done", redo=None, retry=0, redo_started=0, old_rerun=0,
                 note=f"left as it was: what it needs was lost from the backup ({', '.join(paths)})")
        return f"{i['name']} left as it was"
    if not path or not q("SELECT 1 FROM offloads WHERE path=? AND lost IS NOT NULL", path):
        raise ValueError("no such path lost")
    if verdict == "remesh":
        q("UPDATE offloads SET approved=? WHERE path=?", time.time(), path, write=True)
        return f"jobs that need {path} go on without it: try them again"
    q("UPDATE offloads SET blocked=NULL WHERE path=?", path, write=True)
    return f"left as it is; another job needing {path} asks again"


def backup_needs(pids) -> int:
    """How many of these projects' photos are offloaded, and so to be brought back from the backup before they're used."""
    pids = [p for p in dict.fromkeys(pids) if p]
    if not pids:
        return 0
    away = {r["path"] for r in q(f"SELECT path FROM offloads WHERE done IS NOT NULL AND approved IS NULL AND lost IS NULL AND path IN "
                                 f"({','.join('?' * len(pids))})", *[f"projects/{p}/photos" for p in pids])}
    return sum(f"projects/{p}/photos" in away for p in pids)


def log_server(line: str) -> None:
    print(line, flush=True)


def watch_usage() -> None:
    """Measure DATA_DIR now and every USAGE_EVERY seconds, while there's a cap, say when it's nearly reached, and make room
    by offloading to the backup; and put back what the backup sends back, cap or not."""
    while True:
        try:
            if data_cap():
                measure_usage()
                check_room()
            offload_step()
        except Exception as e:  # never let the thread die
            print(f"usage error: {e!r}", flush=True)
        time.sleep(USAGE_EVERY)


def snapshot_databases() -> list[Path]:
    """Copy each database into SNAPSHOTS as <name>.backup, by SQLite's backup API, which is safe while they're in use,
    as copying the files isn't: the filter's cache too (relevance.sqlite), which takes hours of work to make again.
    Returns the copies."""
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    made = []
    for db_file in (DATA / "bouldermesh.sqlite", DATA / "volunteers.sqlite", DATA / "compute" / "machines.sqlite",
                    DATA / "cache" / "relevance.sqlite"):
        if not db_file.exists():
            continue
        out = SNAPSHOTS / f"{db_file.name}.backup"
        tmp = out.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        src, dst = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=60), sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
        made.append(out)
    return made


def snapshot_worker() -> None:
    """Snapshot the databases a minute after starting, and every SNAPSHOT_EVERY after."""
    time.sleep(60)
    while SNAPSHOT_EVERY:
        try:
            snapshot_databases()
        except Exception as e:  # never let the thread die
            print(f"snapshot error: {e!r}", flush=True)
        time.sleep(SNAPSHOT_EVERY)


def _claim(w: Worker):
    """Choose this worker's next climb, and mark it as running, under _claim_lock: an area's next waiting
    climb, from the area fewest other workers are on, but not one on a boulder another worker is on, whose
    harvests use each other's photos. Areas with nothing left are settled here.

    Returns:
        (area, climb), or None.
    """
    others = [x for x in _workers if x is not w]
    camps = q("SELECT * FROM campaigns WHERE status='running' ORDER BY priority, created")     # as entered, top first
    why = full()
    for camp in camps:      # paused, not stopped: they carry on once there's room
        note = f"paused: {why}" if why else None
        if (camp["note"] or "").startswith("paused: ") or note:
            q("UPDATE campaigns SET note=? WHERE id=? AND COALESCE(note,'')!=COALESCE(?,'')", note, camp["id"], note, write=True)
    if why:
        return None
    camps.sort(key=lambda c: sum(x.campaign == c["id"] for x in others))
    for camp in camps:
        if shutil.disk_usage(DATA).free < CAMPAIGN_MIN_FREE:
            q("UPDATE campaigns SET status='stopped', note=? WHERE id=?",
              f"stopped: under {CAMPAIGN_MIN_FREE / 2**30:.0f} GB of disk left", camp["id"], write=True)
            continue
        if camp["ord"] == "data" and not camp["only_retries"] and not any(x.campaign for x in others):
            read_ahead(camp)
        settle_waiting(camp["id"])
        for b in q("SELECT DISTINCT boulder FROM campaign_items WHERE campaign_id=? AND status='harvested'", camp["id"]):
            settle_boulder(camp["id"], b["boulder"])     # as after a restart between its last harvest and queueing its mesh
        for b in q("SELECT DISTINCT boulder FROM campaign_items WHERE campaign_id=? AND boulder IS NOT NULL "
                   "AND status IN ('done','skipped','failed')", camp["id"]):
            settle_clusters(camp["id"], b["boulder"])
        items = ordered_pending(camp)
        waiting = bool(items)
        if camp["only_retries"]:
            items = [i for i in items if i["retry"]]
        taken = {x.boulder for x in others if x.boulder}
        free = [i for i in items if not (i["boulder"] and i["boulder"] in taken)]
        if not free:
            if not items and not any(x.campaign == camp["id"] for x in others):
                own = "AND COALESCE(old_rerun, 0)=0"     # what's queued again at low priority doesn't hold it up
                held = q(f"SELECT count(*) AS n FROM campaign_items WHERE campaign_id=? AND status='waiting' {own}", camp["id"], one=True)["n"]
                paused = q(f"SELECT count(*) AS n FROM campaign_items WHERE campaign_id=? AND status='pending' AND paused IS NOT NULL {own}",
                           camp["id"], one=True)["n"]
                blocked = q("SELECT count(*) AS n FROM campaign_items WHERE campaign_id=? AND status='pending' AND blocked IS NOT NULL "
                            f"AND paused IS NULL {own}", camp["id"], one=True)["n"]
                if held or paused or blocked:    # it carries on once volunteers have fetched what those wait for, they're
                    # resumed, or the admin page says what to do without what the backup lost
                    note = "; ".join([f"{held} climb(s) waiting for volunteers' YouTube searches and videos"] * bool(held)
                                     + [f"{paused} climb(s) {PAUSED_NOTE}"] * bool(paused)
                                     + [f"{blocked} climb(s) {BLOCKED_NOTE}"] * bool(blocked))
                    q("UPDATE campaigns SET note=? WHERE id=? AND COALESCE(note,'')!=?", note, camp["id"], note, write=True)
                else:
                    q("UPDATE campaigns SET status=?, only_retries=0, note=NULL WHERE id=? AND status='running'",
                      "stopped" if waiting else "done", camp["id"], write=True)
            continue
        item = free[0]
        set_item(camp["id"], item["source_id"], status="running")
        q("UPDATE campaigns SET note=NULL WHERE id=? AND (note LIKE '%waiting for volunteers%' OR note LIKE ? OR note LIKE ?)", camp["id"],
          f"%{PAUSED_NOTE}%", f"%{BLOCKED_NOTE}%", write=True)
        w.campaign, w.boulder, w.doing = camp["id"], item["boulder"] if pools_whole(item["boulder"]) else None, item["name"]
        w.item = item["source_id"]
        return camp, item
    return _claim_old(w, others)


def _claim_old(w: Worker, others: list[Worker]):
    """With nothing else for any area to take: the next of every area's climbs queued again as their meshes are old
    (queue_old()), whatever the area's status, one interrupted first, then by the areas' order and each one's own. The
    area's status and its own list stay as they are: a stopped or done one runs only these, in the background.

    Returns:
        (area, climb), or None.
    """
    if shutil.disk_usage(DATA).free < CAMPAIGN_MIN_FREE:
        return None
    camps = q("SELECT * FROM campaigns ORDER BY priority, created")
    for camp in camps:
        queue_old(camp)
        settle_waiting(camp["id"], only_old=True)
        for b in q("SELECT DISTINCT boulder FROM campaign_items WHERE campaign_id=? AND status='harvested' AND old_rerun=1", camp["id"]):
            settle_boulder(camp["id"], b["boulder"])
    taken = {x.boulder for x in others if x.boulder}
    items = [(camp, i) for camp in camps for i in ordered_pending(camp, old=True) if i["old_rerun"]]
    items = [(c, i) for c, i in items if not (i["boulder"] and i["boulder"] in taken)]
    if not items:
        return None
    camp, item = min(items, key=lambda ci: not ci[1]["retry"])     # stable: else in the areas' order
    set_item(camp["id"], item["source_id"], status="running")
    w.campaign, w.boulder, w.doing = camp["id"], item["boulder"] if pools_whole(item["boulder"]) else None, item["name"]
    w.item = item["source_id"]
    return camp, item


def next_campaign_step() -> bool:
    """Mesh the next climb of a running area on this thread's worker, if there's one it can take.

    Returns:
        Whether there was one.
    """
    w = current_worker()
    with _claim_lock:
        picked = _claim(w)
    if not picked:
        return False
    camp, item = picked
    try:
        with meshopts.using(mapper_of(camp)):    # for its meshes, here or on a machine elsewhere
            run_campaign_item(camp, item)
        w.error = w.waiting = None
        settle_boulder(camp["id"], item.get("boulder"))
    except compute.ComputeUnavailable as e:     # the climb waits; so does this worker, for a while
        w.error, w.wait_until = f"{time.strftime('%H:%M')}: {e}", time.time() + COMPUTE_WAIT
        since = min([x.waiting["since"] for x in _workers if x.waiting and x.waiting["campaign"] == camp["id"]] + [time.time()])
        w.waiting = {"campaign": camp["id"], "climb": item["name"], "since": since, "why": str(e),
                     "boulder": (item.get("redo") or "").split("+")[0] in ("boulder", "pool")}
        set_item(camp["id"], item["source_id"], status="pending")
    except LostData as e:       # what it needs was lost from the backup: blocked until the admin page says
        block_item(camp["id"], item["source_id"], e)
    except Exception as e:
        set_item(camp["id"], item["source_id"], status="failed", note=f"{type(e).__name__}: {e}")
    finally:
        w.campaign = w.boulder = w.doing = w.item = None
        q("UPDATE campaign_items SET paused=NULL WHERE campaign_id=? AND source_id=? AND status NOT IN ('pending','running')",
          camp["id"], item["source_id"], write=True)    # paused only to be stopped, as it finished
        if not any(x.campaign == camp["id"] for x in _workers):
            q("UPDATE campaigns SET status='stopped', note=NULL WHERE id=? AND status='stopping'", camp["id"], write=True)
    return True


def queue_retry(camp_id: str, source_id: str, project_id: str | None) -> None:
    """Put one of a campaign's climbs at the front of its list, resuming it if it was paused. A campaign that isn't
    running runs only its retries and then stops, rather than carrying on down its list."""
    set_item(camp_id, source_id, status="pending", retry=1, paused=None, note="queued to try again")
    if project_id:
        q("UPDATE projects SET status='queued', progress=NULL WHERE id=?", project_id, write=True)
    q("UPDATE campaigns SET status='running', only_retries=1, note=NULL WHERE id=? AND status IN ('stopped','done')", camp_id, write=True)


def retry_campaign(camp_id: str) -> int:
    """Try all of a campaign's failed climbs again, each from the last step it finished.

    Returns:
        How many climbs were queued.
    """
    failed = q("SELECT source_id,project_id FROM campaign_items WHERE campaign_id=? AND status='failed'", camp_id)
    for item in failed:
        queue_retry(camp_id, item["source_id"], item["project_id"])
    return len(failed)


def retry_project(pid: str) -> tuple[dict, int]:
    """Try a project again from the last step it finished. One a restart interrupted runs its job as it was
    asked for, carrying on from there if it had started.

    Returns:
        The response, and its HTTP status.
    """
    proj = q("SELECT status,kind,harvest,job FROM projects WHERE id=?", pid, one=True)
    if not proj:
        return {"error": "not found"}, 404
    if proj["status"] in ("queued", "running", "harvesting"):
        return {"error": "already running"}, 409
    if proj["status"] not in ("failed", "stopped", "interrupted"):
        return {"error": "it hasn't failed: Reconstruct makes another mesh"}, 409
    if proj["kind"] not in ("harvest", "boulder") or proj["harvest"]:    # otherwise it gets its photos first
        n = len([f for f in (PROJECTS / pid / "photos").iterdir() if f.suffix.lower() in PHOTO_EXT])
        if n < (1 if not pipeline.real_pipeline_ready() else 3):
            return {"error": "upload at least 3 overlapping photos"}, 400
    item = q("SELECT campaign_id,source_id FROM campaign_items WHERE project_id=? LIMIT 1", pid, one=True)
    if item:
        queue_retry(item["campaign_id"], item["source_id"], pid)
    else:
        q("UPDATE projects SET status='queued', progress=NULL WHERE id=?", pid, write=True)
        queue_job(json.loads(proj["job"]) if proj["status"] in ("interrupted", "stopped") and proj["job"] else {"type": "retry", "pid": pid})
    return {"queued": True}, 202


def stop_project(pid: str) -> tuple[dict, int]:
    """Stop a project's job someone asked for (the Projects tab's Stop): one queued leaves the queue; one running stops
    at once, with the tool it's running and any job it handed a machine. It's left stopped, and Retry runs it again as
    it was asked for, carrying on from the last step it finished. An area's climb whose work it is (area_work()), in
    progress or still to go, is paused in its area instead (pause_item()), as the area would take it up again.

    Returns:
        The response, and its HTTP status.
    """
    proj = q("SELECT * FROM projects WHERE id=?", pid, one=True)
    if not proj:
        return {"error": "not found"}, 404
    held = [i for i in area_work(pid, proj) if i["status"] in ("pending", "running") and not i["paused"]]
    if held:    # an area's climb on it, in progress or to go: paused there, as the area would take it up again
        for i in held:
            pause_item(i["campaign_id"], i["source_id"])
        return {"paused": [{"area": i["area"], "climb": i["name"]} for i in held]}, 200
    with jobs.mutex:
        waiting = [j for j in jobs.queue if j.get("pid") == pid]
        for j in waiting:
            jobs.queue.remove(j)
    on = [w for w in list(_workers) if w.job and w.job["job"].get("pid") == pid]
    if not waiting and not on:
        return {"error": "nothing of it is queued or running"}, 409
    job = json.loads(proj["job"]) if proj["job"] else (on[0].job["job"] if on else waiting[0])
    q("UPDATE projects SET status='stopped', job=? WHERE id=?", json.dumps({**job, "resume": True} if on else job), pid, write=True)
    append_log(pid, "-- stopped by hand: Retry carries on with it" + (" from the last step it finished" if on else ""))
    for w in on:
        w.control.stop()
    return {"stopped": True}, 200


ITEM_STATES = {"pending": "still to go", "running": "in progress", "waiting": "waiting for volunteers' YouTube searches and videos",
               "harvested": "harvested, waiting for the rest of its boulder's", "done": "meshed", "skipped": "skipped", "failed": "failed"}
OWN_REDOS = ("harvest", "videos", "more", "new", "refilter", "rebuild", "rebuild-changed", "try-skipped", "own")   # done in a climb's own project
MESHES_FOR = ("boulder", "pool", "cluster")     # done in the project of the climb's boulder, or of a group of climbs


def redo_head(redo: str | None) -> str:
    """What an area's climb is queued to do again, before any "+rebuild"."""
    return (redo or "").split("+")[0]


def _area_items(where: str = "1", *args) -> list[dict]:
    """Areas' climbs, with their areas' names."""
    return q("SELECT ci.campaign_id, c.name AS area, ci.source_id, ci.name, ci.status, ci.paused, ci.redo, ci.was, ci.mesh_id, "
             "ci.project_id, ci.boulder, ci.pool, ci.climb_id, ci.old_rerun FROM campaign_items ci JOIN campaigns c ON c.id=ci.campaign_id "
             f"WHERE {where} ORDER BY c.created, ci.rank", *args)


def project_areas() -> dict[str, list[dict]]:
    """The areas each project was made for, for the Projects tab: {project id: [{id and name (the area's), kind ("climb"
    for a climb's own harvest; "boulder", "group" or "rest" for the project of a boulder's mesh, of a group of climbs'
    whose photos match, or of what a boulder's mesh left out), and for a climb, its name ("climb"), its status on the
    list and whether it's paused; else how many of the area's climbs it's of ("climbs")}]}."""
    items = _area_items()
    by: dict[str, dict[str, list[dict]]] = {"boulder": {}, "pool": {}, "climb_id": {}}
    out: dict[str, list[dict]] = {}
    for i in items:
        for k in by:
            if i[k]:
                by[k].setdefault(i[k], []).append(i)
        if i["project_id"]:
            out.setdefault(i["project_id"], []).append({"id": i["campaign_id"], "name": i["area"], "kind": "climb", "climb": i["name"],
                                                        "status": i["status"], "paused": bool(i["paused"]),
                                                        **({"old_rerun": True} if i["old_rerun"] else {})})
    for p in q("SELECT id, boulder FROM projects WHERE kind='boulder' AND boulder IS NOT NULL"):
        key = p["boulder"]
        if is_rest(key):
            parent = q("SELECT boulder FROM meshes WHERE id=?", rest_parent(key), one=True)
            kind, on = "rest", by["boulder"].get((parent or {}).get("boulder") or "", [])
        elif is_pool(key):
            kind, on = "group", list({id(i): i for i in by["pool"].get(key, []) + [i for c in pool_members(key) for i in by["climb_id"].get(c, [])]}.values())
        else:
            kind, on = "boulder", by["boulder"].get(key, [])
        for area in dict.fromkeys(i["campaign_id"] for i in on):
            mine = [i for i in on if i["campaign_id"] == area]
            old = any(i["old_rerun"] and i["status"] in UNFINISHED for i in mine)
            out.setdefault(p["id"], []).append({"id": area, "name": mine[0]["area"], "kind": kind, "climbs": len(mine),
                                                **({"old_rerun": True} if old else {})})
    return out


def area_work(pid: str, proj: dict) -> list[dict]:
    """The areas' climbs whose work is in this project: one whose own harvest it is, unless it's meshing its boulder or
    a group (in that one's project); for a boulder's or group's project, those meshing it, or queued to."""
    if proj["kind"] == "boulder" and proj["boulder"]:
        key = proj["boulder"]
        return [i for i in _area_items("ci.boulder=? OR ci.pool=?", key, key)
                if redo_head(i["redo"]) in MESHES_FOR and (i["pool"] if redo_head(i["redo"]) == "cluster" else i["boulder"]) == key]
    return [i for i in _area_items("ci.project_id=?", pid) if redo_head(i["redo"]) not in MESHES_FOR]


def project_effects(pid: str) -> dict | None:
    """What stopping or deleting a project would do, for the Projects tab to say first: {"stop": {says, blocked},
    "delete": {says, blocked}}, says being sentences of what happens to its job, its meshes and the areas' climbs it's
    for, and blocked why it can't be done now (else None). None if there's no such project."""
    proj = q("SELECT * FROM projects WHERE id=?", pid, one=True)
    if not proj:
        return None
    work = area_work(pid, proj)
    held = [i for i in work if i["status"] in ("pending", "running") and not i["paused"]]
    with jobs.mutex:
        queued = any(j.get("pid") == pid for j in jobs.queue)
    running = any(w.job and w.job["job"].get("pid") == pid for w in list(_workers))
    stop = [f"In {i['area']}, {i['name']} is paused: the area goes on with its other climbs, and this one waits until you "
            "resume it there, on the Areas tab. It then carries on from the last step it finished; one it's partway "
            "through, on a machine too, starts again." if i["status"] == "running" else
            f"In {i['area']}, {i['name']} is paused before the area gets to it: no worker takes it until you resume it there, "
            "on the Areas tab." for i in held]
    if not held and running:
        stop.append("Its job stops now, with the tool it's running and any job it handed a machine. Retry carries on from the "
                    "last step it finished; one it was partway through starts again.")
    elif not held and queued:
        stop.append("It leaves the queue, and Retry queues it again.")

    busy = next((i for i in work if i["status"] == "running"), None)
    blocked = (f"{busy['name']} is in progress in {busy['area']}, in this project: stop it first (which pauses it there)" if busy
               else "its job is queued or running: stop it first" if running or queued else None)
    says = [f"Its photos, videos, people masks, log{' and harvest record' if proj['kind'] in ('harvest', 'boulder') else ''} are "
            "deleted, for good."]
    made = q("SELECT COUNT(*) AS n FROM meshes WHERE source=?", f"project:{pid}", one=True)["n"]
    if made:
        says.append(("The mesh made from it stays, with what was found of its size and beta, but can't be rebuilt, retextured, "
                     "or have its" if made == 1 else f"The {made} meshes made from it stay, with what was found of their size and "
                     "beta, but can't be rebuilt, retextured, or have their") + " size or beta found again, which use its photos.")
    own = _area_items("ci.project_id=?", pid)
    for i in own:
        first, what = redo_head(i["redo"]), f"In {i['area']}, {i['name']}"
        if i["status"] in ("done", "skipped", "failed"):
            says.append(f"{what} stays {ITEM_STATES[i['status']]}{', with its mesh' if i['mesh_id'] else ''}. The area's Harvest "
                        "again and Rebuild leave it out from then on, with no harvest of it to go by.")
        elif first == "cluster":
            says.append(f"{what} is still queued to {REDO_NAMES['cluster']}, which goes without its photos.")
        elif first in MESHES_FOR:
            says.append(f"{what} is still queued to {REDO_NAMES[first]}, which harvests it again first.")
        elif i["mesh_id"] and first in OWN_REDOS:
            says.append(f"{what} isn't to {REDO_NAMES.get(i['redo'], first)} after all: it stays "
                        f"{ITEM_STATES.get(i['was']) or 'meshed'}, with the mesh it has.")
        else:
            says.append(f"{what} is harvested again from the start, in a new project, when the area gets to it"
                        + (", once you resume it" if i["paused"] else "")
                        + ("; its boulder's mesh waits for that" if i["status"] == "harvested" else "") + ".")
    if proj["kind"] == "harvest":   # pooled in a boulder's or group's mesh
        climb = ((q("SELECT name FROM climbs WHERE id=?", proj["climb_id"], one=True) or {}).get("name")
                 or next((i["name"] for i in own), "its climb"))
        for b in q("SELECT id, name, boulder FROM projects WHERE kind='boulder' AND boulder IS NOT NULL"):
            try:
                rec = json.loads((PROJECTS / b["id"] / CLIMBS_FILE).read_text())
                pooled = any(pid in (c.get("projects") or []) for c in rec.get("climbs") or [])
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if pooled and not is_rest(b["boulder"]):
                says.append(f"Its photos are pooled in the mesh of {mesh_name(b['name'])}, which stays. "
                            + ("Meshing that group again goes without them." if is_pool(b["boulder"])
                               else f"Meshing that boulder again harvests {climb} again first."))
    if proj["kind"] == "boulder" and proj["boulder"]:
        says.append("The climbs' own harvests it pooled stay as they are. Meshing it again pools them again, in a new project; "
                    "meanwhile an area that gets to one of its climbs meshes it again, as it can't tell its mesh was made "
                    "from the same photos.")
        if proj["mesh_id"] and load_rest(proj["mesh_id"]):
            says.append("What its mesh left out can't be meshed apart again, as that's made from its photos.")
        for i in work:
            if i["status"] == "pending":
                says.append(f"In {i['area']}, {i['name']}'s queued {REDO_NAMES.get(i['redo'], 'meshing')} starts over, in a new project.")
    return {"stop": {"says": stop, "blocked": None if stop else "nothing of it is queued or running"},
            "delete": {"says": says, "blocked": blocked}}


def delete_project(pid: str) -> tuple[dict, int]:
    """Delete a project and all in it (the Projects tab's ✕, which says first what that does: project_effects()), but
    not while its job, or an area's climb in it, is queued or running. An area's climb whose own harvest it was no
    longer has one: one finished keeps its status and mesh; one queued to mesh its boulder or group still is; one with
    a mesh already queued to do something again in this project (OWN_REDOS) doesn't, keeping that mesh; and any other
    is harvested again from the start when its area gets to it.

    Returns:
        The response, and its HTTP status.
    """
    fx = project_effects(pid)
    if fx is None:
        return {"error": "not found"}, 404
    if fx["delete"]["blocked"]:
        return {"error": f"Not deleted: {fx['delete']['blocked']}."}, 409
    for i in _area_items("ci.project_id=?", pid):
        first, at = redo_head(i["redo"]), (i["campaign_id"], i["source_id"])
        if i["status"] in ("done", "skipped", "failed") or first in MESHES_FOR:
            set_item(*at, project_id=None)
        elif i["mesh_id"] and first in OWN_REDOS:
            set_item(*at, status=i["was"] if i["was"] in ("done", "skipped", "failed") else "done", project_id=None, redo=None,
                     retry=0, redo_started=0, changed=0, waiting_since=None,
                     note=f"its project was deleted, so it wasn't to {REDO_NAMES.get(i['redo'], first)} after all")
        else:
            set_item(*at, status="pending", project_id=None, redo=None, was=None, retry=0, redo_started=0, changed=0, waiting_since=None,
                     note="its project was deleted: harvested again from the start")
    q("DELETE FROM projects WHERE id=?", pid, write=True)
    q("DELETE FROM offloads WHERE path LIKE ?", f"projects/{pid}/%", write=True)     # nothing there to bring back
    shutil.rmtree(PROJECTS / pid, ignore_errors=True)
    return {"deleted": pid}, 200


def run_retry(pid: str) -> None:
    """Try a project that isn't part of an area again."""
    proj = q("SELECT kind,harvest FROM projects WHERE id=?", pid, one=True)
    if not proj:
        return
    append_log(pid, "-- trying again")
    if proj["kind"] == "boulder":
        run_boulder(pid, resume=True)
    elif proj["kind"] == "harvest" and not proj["harvest"]:
        run_harvest(pid, set(HARVEST_SOURCES), True, resume=True)
    else:
        run_reconstruct(pid, internet=proj["kind"] == "harvest", resume=True)


REDO_NAMES = {"harvest": "harvest again", "videos": "look for new videos", "rebuild": "rebuild", "rebuild-changed": "rebuild",
              "more": "add what volunteers fetched", "more+rebuild": "add what volunteers fetched and rebuild",
              "new": "check for new photos", "new+rebuild": "check for new photos and rebuild",
              "refilter": "judge its photos again", "refilter+rebuild": "judge its photos again and rebuild",
              "harvest+rebuild": "harvest again and rebuild", "videos+rebuild": "look for new videos and rebuild",
              "boulder": "mesh its boulder from every climb's photos", "pool": "mesh its boulder from every climb's photos",
              "try-skipped": "try meshing, as the viewpoint check would let it now",
              "own": "mesh it on its own, as its boulder's mesh hardly shows it",
              "cluster": "mesh it with the climbs its photos match"}
POOLED = " (pooled)"     # the name of a project made, before boulders had their own, from every harvest of a boulder for one of its climbs


def boulders_to_mesh(camp_id: str) -> list[dict]:
    """An area's boulders with more than one climb on its list, at least one of them been through, whose own mesh
    hasn't been made or queued: each one's highest-ranked climb that's been through, to queue it on."""
    rows = q("""SELECT ci.source_id, ci.boulder, ci.rank, ci.climb_id FROM campaign_items ci
                WHERE ci.campaign_id=? AND ci.boulder IS NOT NULL AND ci.status IN ('done','skipped','failed') AND ci.redo IS NULL
                  AND (SELECT COUNT(*) FROM campaign_items x WHERE x.campaign_id=ci.campaign_id AND x.boulder=ci.boulder) >= 2
                  AND NOT EXISTS (SELECT 1 FROM campaign_items z WHERE z.boulder=ci.boulder AND z.redo IN ('boulder','pool'))
                ORDER BY ci.rank""", camp_id)
    have = {r["boulder"] for r in q("SELECT boulder FROM projects WHERE kind='boulder'")}
    out, seen = [], set()
    for r in rows:
        boulder = r["boulder"]
        if not pools_whole(boulder):    # a group's climbs whose photos match are pooled instead (settle_clusters())
            continue
        if r["boulder"] not in seen and boulder not in have:
            seen.add(r["boulder"])
            out.append(r)
    return out


def queue_boulders(camp_id: str) -> int:
    """Queue meshing each of an area's boulders_to_mesh() from every photo of its climbs (run_boulder()), as
    queue_redo() does, harvesting first those of its climbs that the area hasn't.

    Returns:
        How many were queued.
    """
    rows = boulders_to_mesh(camp_id)
    for r in rows:
        status = q("SELECT status FROM campaign_items WHERE campaign_id=? AND source_id=?", camp_id, r["source_id"], one=True)["status"]
        set_item(camp_id, r["source_id"], status="pending", retry=1, redo="boulder", was=status, redo_started=0,
                 note=f"queued to {REDO_NAMES['boulder']}")
    if rows:
        q("UPDATE campaigns SET status='running', only_retries=1, note=NULL WHERE id=? AND status IN ('stopped','done')", camp_id, write=True)
    return len(rows)


def triable(camp_id: str) -> list[dict]:
    """An area's climbs skipped for too few viewpoints whose harvest the current rules would mesh (diversity.judge()),
    but not those with new photos, which Rebuild changed meshes."""
    rows = q("""SELECT ci.source_id, ci.status, p.harvest FROM campaign_items ci JOIN projects p ON p.id=ci.project_id
                WHERE ci.campaign_id=? AND ci.status='skipped' AND ci.redo IS NULL AND ci.mesh_id IS NULL AND ci.changed=0
                  AND p.status='harvested' AND p.harvest IS NOT NULL""", camp_id)
    return [{"source_id": r["source_id"], "status": r["status"]} for r in rows
            if diversity_mod.judge(json.loads(r["harvest"]).get("diversity")) != "poor"]


def queue_redo(camp_id: str, what: str) -> int:
    """Queue an area's climbs that have been through it to be harvested or rebuilt again, ahead of the
    rest of its list. An area that isn't running runs only these, and then stops, as for retries.

    Args:
        what: "harvest" every climb with photos of its own, from scratch; "new": every one, looking in every source
            again and adding only what wasn't found before; "refilter": every one's photos judged again by the filter
            from scratch (but those put back by hand); "videos": only those that YouTube has new
            videos of; "rebuild" every one with enough viewpoints; "rebuild-changed": only those whose
            photos changed since they were meshed or checked. "harvest+rebuild" and "videos+rebuild"
            rebuild each climb whose photos changed straight after harvesting it.

    Returns:
        How many climbs were queued.
    """
    if what not in REDO_NAMES:
        raise ValueError(f"what must be one of {', '.join(REDO_NAMES)}")
    rows = triable(camp_id) if what == "try-skipped" else q(
        "SELECT ci.source_id, ci.status FROM campaign_items ci JOIN projects p ON p.id=ci.project_id "
        "WHERE ci.campaign_id=? AND ci.status IN ('done','skipped','failed') AND p.status NOT IN ('queued','running','harvesting')"
        + ("" if what.split("+")[0] in ("harvest", "videos", "more", "new") else " AND p.harvest IS NOT NULL")
        + (" AND ci.changed=1" if what == "rebuild-changed" else ""), camp_id)
    for r in rows:
        set_item(camp_id, r["source_id"], status="pending", retry=1, redo=what, was=r["status"], redo_started=0,
                 note=f"queued to {REDO_NAMES[what]}")
    if rows:
        q("UPDATE campaigns SET status='running', only_retries=1, note=NULL WHERE id=? AND status IN ('stopped','done')", camp_id, write=True)
    return len(rows)


def _verdict(pid: str) -> str | None:
    """A harvest's viewpoint verdict, by the current rules (diversity.judge())."""
    h = q("SELECT harvest FROM projects WHERE id=?", pid, one=True)["harvest"]
    return diversity_mod.judge(json.loads(h).get("diversity")) if h else None


def too_few_placed(pid: str) -> str | None:
    """Why a project's reconstruction failed, if it was that its photos weren't enough: COLMAP couldn't place enough
    of them, or those it placed made a mesh with next to no faces. An area counts that as skipped, not failed, as it
    says nothing's wrong, only that the photos aren't enough."""
    why = last_failure(pid) or ""
    if m := re.search(r"only (\d+) image\(s\) could be registered", why):
        return f"COLMAP could place only {m[1]} of its photos together"
    if m := re.search(r"the mesh has only (\d+) face", why):
        return f"its photos made a mesh of only {m[1]} faces"
    return None


def settle_too_few_placed() -> int:
    """Areas' climbs that failed, before too_few_placed() counted that as skipped, for the same reason: skipped
    now, with its note. Returns how many."""
    n = 0
    for r in q("SELECT campaign_id, source_id, project_id FROM campaign_items WHERE status='failed' AND redo IS NULL "
               "AND project_id IS NOT NULL"):
        note = too_few_placed(r["project_id"])
        if note:
            set_item(r["campaign_id"], r["source_id"], status="skipped", note=note)
            n += 1
    return n


def _harvest_again(row: dict, log) -> dict:
    pid = row["project_id"]
    before = photo_names(pid)
    first = row["redo"].split("+")[0]
    if first in ("more", "new", "refilter"):     # added to what it had: what volunteers fetched, or every source's new
        run_harvest(pid, set(HARVEST_SOURCES), False, again=True, add=True, refresh=first == "new", rejudge=first == "refilter")
        return _harvested_again(row, pid, before)
    if first == "videos":
        new = new_videos(row["climb_id"], pid, log)
        if not new:
            log("no videos of this climb on YouTube that its photos don't already have frames from")
            return {"status": row["was"], "note": "no new videos found"}
        log(f"{len(new)} new video(s) of this climb: harvesting it again")
    run_harvest(pid, set(HARVEST_SOURCES), False, again=True)
    return _harvested_again(row, pid, before)


def _harvested_again(row: dict, pid: str, before: set[str]) -> dict:
    """How an area's climb harvested again (_harvest_again()) went: its status and note, and whether its photos changed."""
    p = q("SELECT status,harvest FROM projects WHERE id=?", pid, one=True)
    if row["mesh_id"]:
        q("UPDATE projects SET status='done' WHERE id=? AND status='harvested'", pid, write=True)
    if p["status"] == "failed":
        return {"status": row["was"], "note": f"harvesting again failed: {last_failure(pid)}"}
    summary, verdict = json.loads(p["harvest"]), _verdict(pid)
    changed = photo_names(pid) != before
    status = "done" if row["mesh_id"] else ("skipped" if verdict == "poor" else row["was"])
    note = f"harvested again: {summary['images']} photos, {summary.get('video_frames') or 0} of them from videos; " + (
        "not enough different viewpoints" if verdict == "poor" else
        "Rebuild changed will use them" if changed else "the same photos as before")
    return {"status": status, "note": note, "changed": int((changed or bool(row["changed"])) and verdict != "poor")}


def _rebuild(cid: str, row: dict, log) -> dict:
    pid = row["project_id"]
    if _verdict(pid) == "poor":
        return {"status": "done" if row["mesh_id"] else "skipped", "note": "not enough different viewpoints to mesh", "changed": 0}
    resume = bool(row["redo_started"]) and (PROJECTS / pid / "work").is_dir()
    set_item(cid, row["source_id"], redo_started=1)
    append_log(pid, "-- carrying on rebuilding" if resume else "-- rebuilding")
    camp = q("SELECT keep_better FROM campaigns WHERE id=?", cid, one=True) or {}
    made = run_reconstruct(pid, internet=True, resume=resume, keep_better=camp.get("keep_better", 1) != 0)
    p = q("SELECT status,mesh_id FROM projects WHERE id=?", pid, one=True)
    if made and p["status"] == "done" and row["mesh_id"] and p["mesh_id"] == row["mesh_id"] != made:
        shutil.rmtree(PROJECTS / pid / "work", ignore_errors=True)
        return {"status": "done", "mesh_id": row["mesh_id"], "changed": 0, "note": "rebuilt, but that placed fewer of the same "
                "photos: the mesh before stays the current one, and the new one is kept as an older version of it"}
    if p["status"] == "done" and p["mesh_id"] and p["mesh_id"] != row["mesh_id"]:
        shutil.rmtree(PROJECTS / pid / "work", ignore_errors=True)
        return {"status": "done", "mesh_id": p["mesh_id"], "note": "rebuilt" + (": the one before is kept as an older version" if row["mesh_id"] else ""),
                "changed": 0}
    if row["mesh_id"]:     # it still has the mesh it had
        q("UPDATE projects SET status='done', mesh_id=? WHERE id=?", row["mesh_id"], pid, write=True)
    if not row["mesh_id"] and too_few_placed(pid):
        return {"status": "skipped", "note": too_few_placed(pid), "changed": 0}
    return {"status": "done" if row["mesh_id"] else "failed", "note": f"rebuilding failed: {last_failure(pid)}"}


def _boulder_again(cid: str, row: dict) -> dict:
    """Mesh the boulder of one of an area's climbs from every photo of its climbs, in its own project (run_boulder()),
    harvesting first those the area hasn't. The climb itself keeps its status: only its note says how it went."""
    climb = climb_row(row["climb_id"])
    boulder = boulder_of(climb["area_path"]) if climb else None
    if not boulder or leaf_kind(boulder) == "group":
        return {"status": row["was"], "note": "its area holds several boulders, so it has none of its own to mesh"}
    proj = boulder_project(boulder)
    if proj and proj["status"] in ("queued", "running", "harvesting") and not row["redo_started"]:
        if row["was"] == "harvested":   # its climbs wait for that mesh: this tries again in a while
            raise compute.ComputeUnavailable("its boulder is being meshed by something else now")
        return {"status": row["was"], "note": "its boulder is being meshed already"}
    camp = q("SELECT same_photos_once, keep_better FROM campaigns WHERE id=?", cid, one=True) or {}
    if not row["redo_started"] and camp.get("same_photos_once", 1) != 0 and boulder_unchanged(boulder):
        return {"status": row["was"], "note": SAME_PHOTOS_NOTE}
    resume = bool(row["redo_started"] and proj and proj["status"] in ("stopped", "interrupted", "failed", "harvesting", "running"))
    pid = boulder_project_made(boulder)
    set_item(cid, row["source_id"], redo_started=1)
    run_boulder(pid, resume=resume, keep_better=camp.get("keep_better", 1) != 0)
    p = q("SELECT status, mesh_id, harvest FROM projects WHERE id=?", pid, one=True)
    harvest = json.loads(p["harvest"]) if p["harvest"] else {}
    of = harvest.get("climbs") or {}
    what = f"{harvest.get('images', 0)} photos of {of.get('pooled', '?')} of its {of.get('of', '?')} climbs"
    if p["status"] == "done" and p["mesh_id"]:
        return {"status": row["was"], "note": f"its boulder meshed from {what}"}
    if p["status"] == "harvested":
        return {"status": row["was"], "note": f"its boulder: {what}, still not enough different viewpoints"}
    return {"status": row["was"], "note": "its boulder: " + (too_few_placed(pid) or f"meshing failed: {last_failure(pid)}")}


def run_redo(camp: dict, item: dict) -> None:
    """Harvest or rebuild again one of an area's climbs that has been through it before."""
    cid, sid = camp["id"], item["source_id"]
    row = q("SELECT source_id,climb_id,project_id,mesh_id,redo,was,changed,redo_started,waiting_since,boulder,pool,old_rerun "
            "FROM campaign_items WHERE campaign_id=? AND source_id=?", cid, sid, one=True)
    row["was"] = row["was"] or "done"
    log = lambda s: append_log(row["project_id"], s)  # noqa: E731
    first, *then = row["redo"].split("+")
    if row["boulder"] and leaf_kind(row["boulder"]) is None and row["climb_id"] and settings().get("areas.meshes") == "boulders":
        data = q("SELECT data FROM climbs WHERE id=?", row["climb_id"], one=True)     # what its lowest area is, before pooling it
        if data and data["data"]:                                                       # again (as run_campaign_item())
            boulder_kind(row["boulder"], climb_area_url(Climb.from_dict(json.loads(data["data"]))), log if row["project_id"] else print)
    if first in ("boulder", "pool"):    # pool: queued before boulders had their own meshes
        return _run_boulder_redo(camp, row)
    if first == "own":
        return _run_own_redo(camp, row)
    if first == "cluster":
        return _run_cluster_redo(camp, row)
    jobctl.begin(low_priority=True)
    try:
        set_item(cid, sid, status="running", note=None)
        if first in ("harvest", "videos", "more", "new", "refilter") and not row["redo_started"]:    # a rebuild that started was harvested already
            end = _harvest_again(row, log)
            if row["was"] == "waiting" and not row["mesh_id"] and may_wait(row) and VOLUNTEERS.pending("harvest", row["project_id"]):
                log("waiting for more from volunteers: the YouTube videos their search found")
                return wait_for_volunteers(cid, sid, row["project_id"], row["waiting_since"])
            if row["was"] == "waiting" and not row["mesh_id"] and unchecked(camp, row["project_id"]):   # tried again later
                log("still not searched for on YouTube: not meshed yet")
                return wait_for_volunteers(cid, sid, row["project_id"], None)
            remake = bool(row["old_rerun"])     # its meshes are old (queue_old()): made again, whatever was found
            if pooled(cid, row["boulder"]) and (row["was"] in ("waiting", "harvested") or (then and (end.get("changed") or remake))):
                end = {**end, "status": "harvested", "note": HARVESTED_NOTE}     # its boulder's mesh is made again with it
            elif then and (end.get("changed") or remake or (row["was"] == "waiting" and not row["mesh_id"])):
                set_item(cid, sid, changed=1)
                harvested_note = end["note"].split("; ")[0]     # not "Rebuild changed will use them": it's being rebuilt
                end = _rebuild(cid, {**row, "changed": 1}, log)
                end["note"] = f"{harvested_note}; {end['note']}"
        else:
            end = _rebuild(cid, row, log)
    except Cancelled:
        q("UPDATE projects SET status='stopped' WHERE id=?", row["project_id"], write=True)
        set_item(cid, sid, status="pending", note=f"stopped partway: carries on with its {REDO_NAMES[row['redo']]} when resumed")
        return
    except compute.ComputeUnavailable as e:
        q("UPDATE projects SET status='stopped' WHERE id=?", row["project_id"], write=True)
        set_item(cid, sid, status="pending", note=f"waiting for a machine to mesh it on: {e}")
        raise
    except LostData:    # blocked until the admin page says (next_campaign_step())
        q("UPDATE projects SET status='stopped' WHERE id=? AND status IN ('harvesting','running')", row["project_id"], write=True)
        raise
    except Exception as e:
        end = {"status": row["was"], "note": f"{REDO_NAMES[row['redo']]}: {type(e).__name__}: {e}"}
    set_item(cid, sid, **end, redo=None, retry=0, redo_started=0, **({} if end.get("status") in ("harvested", "waiting") else {"old_rerun": 0}))
    if end.get("status") != "harvested":
        link_after(cid, sid)


def plural(n: int, one: str) -> str:
    return f"{n} {one}{'' if n == 1 else 's'}"


_settled: dict[tuple[str, str], tuple] = {}     # (area, lowest area) -> its climbs' states when settle_clusters() last looked


def settle_clusters(camp_id: str, leaf: str) -> int:
    """Once an area's climbs of a lowest area that isn't one boulder are all through (none pending, running, waiting or
    queued again), those whose photos are of one rock (clusters(), by the links worked out as each was harvested)
    queued to mesh together as a pool, on the first of them in the area's order, unless they have been, these same
    climbs. Not one boulder's, whose mesh's left-out photos are meshed together instead (keep_rest()). Returns how many
    were queued."""
    kind = leaf_kind(leaf)
    if kind is None:
        return 0
    items = q("SELECT source_id, climb_id, status, redo, was, rank, updated FROM campaign_items WHERE campaign_id=? AND boulder=? "
              "ORDER BY rank", camp_id, leaf)
    seen = tuple((i["source_id"], i["status"], i["redo"], i["updated"]) for i in items)
    if _settled.get((camp_id, leaf)) == seen:
        return 0
    _settled[(camp_id, leaf)] = seen
    camp = q("SELECT * FROM campaigns WHERE id=?", camp_id, one=True) or {}
    scope = in_scope(camp)
    due = items if scope is None else [i for i in items if i["source_id"] in scope or i["status"] in ("running", "waiting", "harvested")
                                       or following(i)]
    if any(i["status"] in ("pending", "running", "waiting", "harvested") or i["redo"] for i in due):
        return 0
    ids = [i["climb_id"] for i in items if i["status"] in ("done", "skipped", "failed") and i["climb_id"] and climb_harvests(i["climb_id"])]
    if kind == "boulder":   # what its mesh left out is meshed together instead (keep_rest())
        return 0
    queued = 0
    for group in clusters(ids) if len(ids) >= 2 else []:
        if len(group) < 2:
            continue
        key = pool_for(leaf, group)
        proj = boulder_project(key)
        if proj and proj["status"] in ("queued", "running", "harvesting"):
            continue
        try:
            then = {c["id"] for c in json.loads((PROJECTS / proj["id"] / CLIMBS_FILE).read_text())["climbs"]} if proj else None
        except (OSError, ValueError, KeyError):
            then = None
        if then == set(group) and not old_pool(proj):  # pooled from these climbs before, however it went
            continue
        first = next(i for i in items if i["climb_id"] in group)
        set_item(camp_id, first["source_id"], status="pending", retry=1, redo="cluster", was=first["status"], redo_started=0, pool=key,
                 note=f"its photos match those of {plural(len(group) - 1, 'other climb')}: meshing them together")
        queued += 1
    if queued:
        q("UPDATE campaigns SET status='running', only_retries=1, note=NULL WHERE id=? AND status='done'", camp_id, write=True)
    return queued


def _run_cluster_redo(camp: dict, row: dict) -> None:
    """run_redo() for meshing together the climbs of a pool (settle_clusters()), in the pool's own project, from their
    harvests (run_boulder()); each of them is linked to its mesh. The climb that carries it keeps its status."""
    cid, sid, key = camp["id"], row["source_id"], row["pool"]
    jobctl.begin(low_priority=True)
    pid = None
    try:
        set_item(cid, sid, status="running", note=None)
        members = pool_members(key) if key else []
        if len(members) < 2:
            end = {"status": row["was"], "note": "the climbs its photos matched aren't to be pooled any more"}
        else:
            proj = boulder_project(key)
            resume = bool(row["redo_started"] and proj and proj["status"] in ("stopped", "interrupted", "failed", "harvesting", "running"))
            pid = boulder_project_made(key)
            set_item(cid, sid, redo_started=1)
            keep = (q("SELECT keep_better FROM campaigns WHERE id=?", cid, one=True) or {}).get("keep_better", 1) != 0
            run_boulder(pid, resume=resume, fetch=False, keep_better=keep)
            p = q("SELECT status, mesh_id, harvest FROM projects WHERE id=?", pid, one=True)
            if p["status"] == "done" and p["mesh_id"]:
                for m in members:
                    link_mesh(p["mesh_id"], m, POOLED_LINK)
                links_from_models(p["mesh_id"], pid)
                images = (json.loads(p["harvest"]) if p["harvest"] else {}).get("images", "?")
                end = {"status": row["was"], "note": f"meshed with the {plural(len(members) - 1, 'other climb')} its photos match, "
                                                     f"from {images} photos"}
            elif p["status"] == "harvested":
                end = {"status": row["was"], "note": "with the climbs its photos match: still not enough different viewpoints"}
            else:
                end = {"status": row["was"], "note": "with the climbs its photos match: " + (too_few_placed(pid) or f"meshing failed: {last_failure(pid)}")}
    except Cancelled:
        if pid:
            q("UPDATE projects SET status='stopped' WHERE id=?", pid, write=True)
        set_item(cid, sid, status="pending", note=f"stopped partway: carries on with its {REDO_NAMES['cluster']} when resumed")
        return
    except compute.ComputeUnavailable as e:
        set_item(cid, sid, status="pending", note=f"waiting for a machine to mesh it with the climbs its photos match on: {e}")
        raise
    except LostData:
        raise
    except Exception as e:
        end = {"status": row["was"], "note": f"{REDO_NAMES['cluster']}: {type(e).__name__}: {e}"}
    set_item(cid, sid, **end, redo=None, retry=0, redo_started=0, pool=None)


POOLED_LINK = "pooled with the climbs its photos match"     # a mesh_links note: a pool's mesh, linked to each of its climbs


def began_since(progress: str | None) -> bool:
    """Whether a project's last job began under the rules (MESH_RULES_SINCE), by its progress."""
    try:
        began = max((st.get("started") or 0 for st in json.loads(progress or "{}").get("steps") or []), default=0)
    except (ValueError, AttributeError):
        return False
    return began >= MESH_RULES_SINCE[MESH_RULES]


def old_pool(proj: dict | None) -> bool:
    """Whether a pool's project (settle_clusters()) is to mesh again from the same climbs: its mesh is old (MESH_RULES),
    and it hasn't tried under the rules."""
    if not proj or not proj["mesh_id"] or began_since(proj.get("progress")):
        return False
    m = q("SELECT source, rules, replaced_by FROM meshes WHERE id=?", proj["mesh_id"], one=True)
    return bool(m and not m["replaced_by"] and mesh_is_old(m))


def adopt_old_pools(log=print) -> int:
    """Meshes made before boulders had their own, from one climb's project that pooled the harvests of others on its
    boulder (named "... (pooled)"): each made a pool of the climbs whose photos it has (pools, as settle_clusters()
    makes them), with where each is seen from in it (viewpoints.py), so that its card says which it shows, and each of
    them linked to it. Once each, at start. Returns how many."""
    done = 0
    for m in q("SELECT m.id, m.source FROM meshes m JOIN projects p ON m.source = 'project:' || p.id "
               "WHERE m.boulder IS NULL AND p.kind='harvest' AND p.name LIKE ?", "%" + POOLED):
        pid = m["source"].split(":", 1)[1]
        try:
            proj = q("SELECT climb_id FROM projects WHERE id=?", pid, one=True)
            climb = climb_row(proj["climb_id"]) if proj and proj["climb_id"] else None
            if not climb or not (PROJECTS / pid / "photos").is_dir():
                continue
            files = {f["file"]: f.get("pooled_from") for f in json.loads((PROJECTS / pid / "sources.json").read_text()).get("files", [])}
            whose = {r["id"]: r["climb_id"] for r in q(
                f"SELECT id, climb_id FROM projects WHERE id IN ({','.join('?' * len(set(files.values()) - {None})) or 'NULL'})",
                *(set(files.values()) - {None}))}
            own: dict[str, set[str]] = {}
            for f in (PROJECTS / pid / "photos").iterdir():
                cid = whose.get(files.get(f.name) or "") if files.get(f.name) else climb["id"]
                if cid:
                    own.setdefault(cid, set())
                    if relevance_mod.is_trusted(f):
                        own[cid].add(f.name)
            if len(own) < 2:
                continue
            leaf = climb["area_path"] if isinstance(climb["area_path"], str) else json.dumps(list(climb["area_path"] or []))
            key = pool_for(leaf, sorted(own))
            if (MESHES / m["id"] / "cameras.json").exists():
                viewpoints.compute(MESHES / m["id"], own, log)
            q("UPDATE meshes SET boulder=? WHERE id=?", key, m["id"], write=True)
            for cid in own:     # those linked as on the same boulder, or not at all, are in it
                if not q("SELECT 1 FROM mesh_links WHERE mesh_id=? AND climb_id=? AND COALESCE(note,'') NOT LIKE ?", m["id"], cid, SHARED + "%"):
                    link_mesh(m["id"], cid, POOLED_LINK)
            done += 1
        except Exception as e:     # one that can't be read stays as it was
            log(f"pools: couldn't adopt mesh {m['id']} ({type(e).__name__}: {e})")
    if done:
        log(f"pools: {done} mesh(es) made by pooling a boulder's harvests, before boulders had their own, now say which climbs they show")
    return done


def link_after(camp_id: str, source_id: str) -> None:
    """After an area's climb is through, if its lowest area isn't one boulder: its photos compared with its neighbours'
    (link_climbs()), for settle_clusters() to pool those of one rock. Only costs that if it fails."""
    it = q("SELECT climb_id, boulder, project_id FROM campaign_items WHERE campaign_id=? AND source_id=?", camp_id, source_id, one=True)
    if not it or not it["boulder"] or not it["climb_id"] or leaf_kind(it["boulder"]) not in ("group", "unknown"):
        return
    log = (lambda s: append_log(it["project_id"], s)) if it["project_id"] else print
    try:
        others = [c["id"] for c in q("SELECT id FROM climbs WHERE area_path=?", it["boulder"])]
        link_climbs(list(dict.fromkeys([it["climb_id"]] + others)), log)
    except Exception as e:
        log(f"links: comparing its photos with its neighbours' failed ({type(e).__name__}: {e})")


def _run_own_redo(camp: dict, row: dict) -> None:
    """run_redo() for meshing a climb on its own after its boulder's mesh hardly placed its photos, as queued before
    keep_rest() replaced that: from its own harvest. If that can't be done, it keeps its boulder's mesh."""
    cid, sid, pid, pooled_mesh = camp["id"], row["source_id"], row["project_id"], row["mesh_id"]
    jobctl.begin(low_priority=True)
    try:
        set_item(cid, sid, status="running", note=None)
        resume = bool(row["redo_started"]) and (PROJECTS / pid / "work").is_dir()
        set_item(cid, sid, redo_started=1)
        append_log(pid, "-- carrying on meshing it on its own" if resume else
                   "-- meshing it on its own: its boulder's mesh hardly placed its own photos")
        run_reconstruct(pid, internet=True, resume=resume)
        p = q("SELECT status, mesh_id FROM projects WHERE id=?", pid, one=True)
        if p["status"] == "done" and p["mesh_id"] and p["mesh_id"] != pooled_mesh:
            shutil.rmtree(PROJECTS / pid / "work", ignore_errors=True)
            end = {"status": "done", "mesh_id": p["mesh_id"], "note": "meshed on its own too, as its boulder's mesh hardly placed its photos"}
        else:
            why = too_few_placed(pid) or f"meshing failed: {last_failure(pid)}"
            end = {"status": "done" if pooled_mesh else row["was"], "mesh_id": pooled_mesh, "note": f"on its own, {why}; its boulder's mesh stays"}
    except Cancelled:
        q("UPDATE projects SET status='stopped' WHERE id=?", pid, write=True)
        set_item(cid, sid, status="pending", note=f"stopped partway: carries on with its {REDO_NAMES['own']} when resumed")
        return
    except compute.ComputeUnavailable as e:
        q("UPDATE projects SET status='stopped' WHERE id=?", pid, write=True)
        set_item(cid, sid, status="pending", note=f"waiting for a machine to mesh it on: {e}")
        raise
    except LostData:
        raise
    except Exception as e:
        end = {"status": "done" if pooled_mesh else row["was"], "mesh_id": pooled_mesh, "note": f"{REDO_NAMES['own']}: {type(e).__name__}: {e}"}
    set_item(cid, sid, **end, redo=None, retry=0, redo_started=0, own_tried=time.time())
    link_after(cid, sid)


def _run_boulder_redo(camp: dict, row: dict) -> None:
    """run_redo() for meshing a climb's boulder, whose project is the boulder's, not the climb's."""
    cid, sid = camp["id"], row["source_id"]
    jobctl.begin(low_priority=True)
    failed = False
    try:
        set_item(cid, sid, status="running", note=None)
        end = _boulder_again(cid, row)
    except Cancelled:
        climb = climb_row(row["climb_id"])
        proj = boulder_project(boulder_of(climb["area_path"])) if climb and boulder_of(climb["area_path"]) else None
        if proj:
            q("UPDATE projects SET status='stopped' WHERE id=?", proj["id"], write=True)
        set_item(cid, sid, status="pending", note=f"stopped partway: carries on with its {REDO_NAMES['boulder']} when resumed")
        return
    except compute.ComputeUnavailable as e:
        set_item(cid, sid, status="pending", note=f"waiting for a machine to mesh its boulder on: {e}")
        raise
    except LostData:
        raise
    except Exception as e:
        end, failed = {"status": row["was"], "note": f"{REDO_NAMES['boulder']}: {type(e).__name__}: {e}"}, True
    if row["was"] == "harvested":
        end = boulder_meshed(cid, sid, end, failed)
    elif not failed:    # one done or skipped before carried it: its neighbours harvested for it are done with it too
        b = q("SELECT boulder FROM campaign_items WHERE campaign_id=? AND source_id=?", cid, sid, one=True)["boulder"]
        proj = boulder_project(b) if b else None
        if proj and proj["status"] == "done" and proj["mesh_id"]:
            boulder_meshed(cid, sid, end, failed)
    set_item(cid, sid, **end, redo=None, retry=0, redo_started=0)
    if not failed:  # which climbs' photos it placed together (what it left out is meshed apart: keep_rest())
        b = q("SELECT boulder FROM campaign_items WHERE campaign_id=? AND source_id=?", cid, sid, one=True)["boulder"]
        proj = boulder_project(b) if b else None
        if proj and proj["status"] == "done" and proj["mesh_id"]:
            try:
                links_from_models(proj["mesh_id"], proj["id"])
            except Exception as e:
                append_log(proj["id"], f"links: failed ({type(e).__name__}: {e})")


def run_beta(mid: str) -> None:
    """Find where climbers put their hands and feet in a mesh's videos (beta.py)."""
    m = q("SELECT source FROM meshes WHERE id=?", mid, one=True)
    if not m or not m["source"].startswith("project:"):
        return
    mesh_dir, pid = MESHES / mid, m["source"].split(":", 1)[1]

    def log(line: str) -> None:
        with open(mesh_dir / "beta.log", "a") as f:
            f.write(line.rstrip() + "\n")
    try:
        need_back([pid], "finding its beta")
        if offloading():
            remote_beta(mid, pid, log)
        else:
            beta_mod.find(mesh_dir, PROJECTS / pid / "photos", log)
        if (mesh_dir / beta_mod.POSES_FILE).exists():
            queue_scale(mid)    # the climbers' reach is checked against the size there
    except youtube_mod.Refused:     # it says it's waiting, and stays queued: for volunteers to fetch its videos, if any can
        want = video_wanter("beta", mid)
        rec = beta_mod.load(mesh_dir) or {}
        done = {v["url"] for v in rec.get("videos", [])}
        missing = [u for u in beta_mod.videos(mesh_dir) if u not in done and not youtube_mod.cached(u)]
        if missing:     # it waits for them, which volunteers, if any, are asked for
            for u in missing if want else []:
                want(u)
            beta_mod.save(mesh_dir, {**rec, "status": "queued", "progress": None, "waiting_videos": missing})
            log(f"beta: {len(missing)} video(s) " + ("asked of volunteers" if want else "to wait for: there are no volunteers to ask"))
    except pullwork.NotTaken as e:      # to the back of the queue, so the next isn't held up behind it
        log(f"beta: {e}: tried again later")
        beta_mod.save(mesh_dir, {**(beta_mod.load(mesh_dir) or {"videos": []}), "status": "queued", "progress": None, "resume": True,
                                 "queued": time.time(), "ahead": False, "machine_wait": time.time() + TAKE_BACKOFF,
                                 "note": f"waited for a machine: {e}"})
    except compute.ComputeUnavailable as e:     # tried again in a while
        log(f"beta: waiting for a machine to find it on: {e}")
        beta_mod.save(mesh_dir, {**(beta_mod.load(mesh_dir) or {"videos": []}), "status": "queued", "progress": None, "resume": True,
                                 "machine_wait": time.time() + COMPUTE_WAIT})
    except Cancelled:
        beta_mod.save(mesh_dir, {**(beta_mod.load(mesh_dir) or {"videos": []}), "status": "stopped", "progress": None})
    except Exception as e:
        log(f"FAILED: {type(e).__name__}: {e}")
        beta_mod.save(mesh_dir, {**(beta_mod.load(mesh_dir) or {"videos": []}), "status": f"failed: {type(e).__name__}: {e}", "progress": None})


# ------------------------------------------------------------------ finding beta, on a thread of its own
BETA_NICE = 19
_beta_control = JobControl()
_beta_wake = threading.Event()
_beta_now: dict | None = None    # the mesh it's finding beta in: {mid, started, thread}


def queue_beta(mid: str, ahead: bool = False) -> bool:
    """Queue finding a mesh's beta, unless it's queued or being found already; whether it was queued.

    Args:
        ahead: before those queued for every mesh at once, as someone asked for this one.
    """
    if (beta_mod.load(MESHES / mid) or {}).get("status") in ("queued", "running") or mesh_old(mid):
        return False
    beta_mod.save(MESHES / mid, {"status": "queued", "videos": [], "progress": None, "queued": time.time(), "ahead": ahead})
    _beta_wake.set()
    return True


def beta_ready(mid: str) -> bool:
    """Whether a queued mesh's beta can be looked for now: the videos it waited for are all in the cache, or this
    server may fetch videos from YouTube now (it isn't refusing, and they aren't left to volunteers), or, where they
    are, it hasn't asked volunteers for any yet. Not while it waits for a machine to find it on."""
    rec = beta_mod.load(MESHES / mid) or {}
    if rec.get("machine_wait", 0) > time.time():
        return False
    waiting = rec.get("waiting_videos") or []
    if waiting and all(youtube_mod.cached(u) for u in waiting):
        return True
    if youtube_mod.via == "volunteers":
        return not waiting
    return not youtube_mod.refused()


def beta_queue() -> list[str]:
    """Meshes queued to find beta in, in the order it'll take them: those asked for one at a time first. Old ones
    (mesh_old()) are dropped from it, as what replaces them gets its own."""
    found = []
    for d in MESHES.iterdir() if MESHES.exists() else []:
        rec = beta_mod.load(d)
        if rec and rec["status"] == "queued" and mesh_old(d.name):
            beta_mod.save(d, {**rec, "status": "stopped", "progress": None, "note": "not looked for: the mesh is old"})
        elif rec and rec["status"] == "queued":
            found.append(((not rec.get("ahead"), rec.get("queued", 0)), d.name))
    return [mid for _, mid in sorted(found)]


def beta_findable() -> list[str]:
    """Meshes with video frames placed in them, whose beta hasn't been found or isn't being: newest first."""
    out = []
    for r in q("SELECT id, source, rules, old_data FROM meshes WHERE source LIKE 'project:%' AND replaced_by IS NULL ORDER BY created DESC"):
        if mesh_is_old(r) or r["old_data"]:
            continue
        status = (beta_mod.load(MESHES / r["id"]) or {}).get("status")
        if status not in ("done", "queued", "running") and beta_mod.videos(MESHES / r["id"]):
            out.append(r["id"])
    return out


def stop_beta() -> int:
    """Stop finding beta: the mesh it's on, and those queued. Returns how many were queued."""
    queued = beta_queue()
    for mid in queued:
        rec = beta_mod.load(MESHES / mid) or {}
        beta_mod.save(MESHES / mid, {**rec, "status": "stopped", "progress": None, "waiting": None, "resume": None})
    _beta_control.stop()
    return len(queued)


def beta_worker() -> None:
    """Finds beta in queued meshes, one at a time, alongside the workers' meshing. It's on its own thread so that
    it never holds up an area, at the lowest CPU priority (the pose model's threads inherit it), as it can make do
    with what meshing leaves. While YouTube is refusing downloads, it takes only meshes whose videos are all in the
    cache, as volunteers fetched them (beta_ready())."""
    global _beta_now
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), BETA_NICE)
    use_control(_beta_control)
    while True:
        _beta_wake.wait(timeout=60)
        _beta_wake.clear()
        use_processing_cores()
        try:
            requeue_waiting_scale()
        except Exception as e:  # never let the thread die
            print(f"scale requeue error: {e!r}", flush=True)
        while not _stopping and not _draining:
            queued = [mid for mid in beta_queue() if beta_ready(mid)]
            if not queued:
                break
            if mesh_old(queued[0]):     # became old while queued
                beta_queue()
                continue
            _beta_now = {"mid": queued[0], "started": time.time(), "thread": threading.get_ident()}
            jobctl.begin(low_priority=True)
            try:
                run_beta(queued[0])
            except Exception as e:  # never let the thread die
                print(f"beta error on {queued[0]}: {e!r}", flush=True)
            finally:
                _beta_now = None


_frames_wake = threading.Event()
_frames_control = JobControl()


PRUNE_IDLE_HOURS = 6        # a cached video used more lately than this is left alone


def prune_videos() -> dict:
    """Delete the cached videos beta can't use, as none of their frames is placed in any mesh (beta_mod.videos()),
    once every project that took frames from them is settled: done, failed or skipped, with no area climb of it still
    to harvest, waiting, or harvested for a boulder not yet meshed, and no boulder's mesh pooling it now. Only a harvest
    again would use them, which can fetch them again. Their details (<id>.json) stay. Left alone: those no harvest has
    used yet (a volunteer's, for a climb to harvest again), those a beta or a volunteer's request waits for, those used
    in the last PRUNE_IDLE_HOURS, and any asked back from the backup. One offloaded to the backup is forgotten there
    (offload.drop()), so that nothing asks for it back.

    Returns:
        {"deleted": videos, "bytes": freed here, "dropped": offloaded ones forgotten}.
    """
    out = {"deleted": 0, "bytes": 0, "dropped": 0}
    cache = youtube_mod.VIDEO_CACHE
    local = {f.stem: f for f in cache.iterdir() if f.suffix != ".json" and not f.name.endswith(".part")} if cache.is_dir() else {}
    away = {Path(r["path"]).stem: r for r in q("SELECT path, done, wanted FROM offloads WHERE path LIKE 'cache/videos/%'")}
    if not local and not away:
        return out
    used: dict[str, set[str]] = {}         # video id -> projects that took frames from it
    for s in PROJECTS.glob("*/sources.json"):
        try:
            for f in json.loads(s.read_text()).get("files", []):
                vid = youtube_mod.video_id(f.get("url") or "") if f.get("source") == "youtube" else None
                if vid:
                    used.setdefault(vid, set()).add(s.parent.name)
        except (OSError, ValueError, AttributeError):
            continue
    placed = {youtube_mod.video_id(u) for d in MESHES.iterdir() if d.is_dir() for u in beta_mod.videos(d)} if MESHES.is_dir() else set()
    waiting = {youtube_mod.video_id(u) for mid in beta_queue() for u in (beta_mod.load(MESHES / mid) or {}).get("waiting_videos") or []}
    try:
        wanted = {r["video_id"] for r in VOLUNTEERS.q("SELECT t.video_id FROM tasks t JOIN wants w ON w.task=t.id "
                                                    "WHERE t.kind='video' AND w.applied=0")}
    except Exception:
        wanted = set()
    unsettled = {r["id"] for r in q("SELECT id FROM projects WHERE status NOT IN ('done','failed','harvested')")}
    for r in q("SELECT ci.project_id, ci.campaign_id, ci.boulder FROM campaign_items ci WHERE ci.project_id IS NOT NULL AND "
               "(ci.status IN ('pending','running','waiting','harvested') OR (ci.boulder IS NOT NULL AND EXISTS (SELECT 1 FROM "
               "campaign_items o WHERE o.campaign_id=ci.campaign_id AND o.boulder=ci.boulder AND o.status IN "
               "('pending','running','waiting','harvested'))))"):
        unsettled.add(r["project_id"])
    now, dropped = time.time(), []
    for vid in set(local) | set(away):
        projects = used.get(vid)
        if not projects or vid in placed or vid in waiting or vid in wanted or projects & unsettled:
            continue
        f = local.get(vid)
        if f is not None:
            try:
                if now - f.stat().st_mtime < PRUNE_IDLE_HOURS * 3600:
                    continue
                size = f.stat().st_size
                f.unlink()
                out["deleted"] += 1
                out["bytes"] += size
            except OSError:
                continue
        row = away.get(vid)
        if row and not (row["done"] and row["wanted"]):     # one asked back is left for its restore
            dropped.append(row["path"])
    if dropped:
        offload_mod.drop(q, dropped)
        offload_mod.write_lists(DATA, q)
        out["dropped"] = sum(1 for p in dropped if away[Path(p).stem]["done"])
    if out["deleted"] or out["dropped"]:
        print(f"videos: deleted {out['deleted']} cached video(s) none of whose frames is placed in a mesh "
              f"({out['bytes'] / 2**30:.2f} GB), and forgot {out['dropped']} offloaded to the backup", flush=True)
    return out


def prune_worker() -> None:
    """prune_videos() an hour after starting and hourly after, at the lowest CPU priority, if the admin page says to."""
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), BETA_NICE)
    while True:
        time.sleep(3600)
        try:
            if settings().get("videos.prune_unplaced"):
                prune_videos()
        except Exception as e:  # never let the thread die
            print(f"videos: pruning failed ({type(e).__name__}: {e})", flush=True)


def frames_worker() -> None:
    """Turns videos uploaded to projects into photos, one at a time, oldest first, on a thread of its own at the lowest
    CPU priority, like finding beta. A video is deleted once it's done, or has failed; one a restart cut short is
    done again."""
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), BETA_NICE)
    use_control(_frames_control)
    while True:
        _frames_wake.wait(timeout=300)
        _frames_wake.clear()
        use_processing_cores()
        while not _stopping and not _draining:
            waiting = sorted((f for f in PROJECTS.glob("*/videos/*") if f.is_file()), key=lambda f: f.stat().st_mtime)
            if not waiting:
                break
            video, pid = waiting[0], waiting[0].parent.parent.name
            jobctl.begin(low_priority=True)
            try:
                frames_from_video(pid, video)
            except Exception as e:  # never let the thread die
                append_log(pid, f"video {video.name}: no photos from it: {e}")
            finally:
                video.unlink(missing_ok=True)


def frames_from_video(pid: str, video: Path) -> None:
    """A video's VIDEO_FRAMES sharpest frames, spread over its length, as the project's photos vid_<tag>_NNN.jpg."""
    tag = hashlib.sha1(video.name.encode()).hexdigest()[:8]
    work = PROJECTS / pid / f"_frames_{tag}"
    append_log(pid, f"video {video.name}: choosing its sharpest frames")
    try:
        frames = harvest_mod.extract_frames(video, work, VIDEO_FRAMES, lambda line: append_log(pid, line))
        photos = PROJECTS / pid / "photos"
        photos.mkdir(exist_ok=True)
        for j, f in enumerate(frames):
            shutil.move(str(f), photos / f"vid_{tag}_{j:03d}.jpg")
        append_log(pid, f"video {video.name}: {len(frames)} frame(s) added to the photos")
    finally:
        shutil.rmtree(work, ignore_errors=True)


_splats: "queue.Queue[str]" = queue.Queue()


def queue_splat(mid: str) -> None:
    """Fit a splat of what's around a new mesh (splat.py), if SPLATS=1 and it can be done here."""
    if splat_mod.enabled() and not splat_mod.unavailable():
        _splats.put(mid)


def splat_worker() -> None:
    """Fits splats one at a time on a thread of its own, at the lowest CPU priority: the GPU does the work. Each mesh's
    goes in its own folder, with splat.log."""
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), BETA_NICE)
    while True:
        mid = _splats.get()
        use_processing_cores()
        m = q("SELECT source FROM meshes WHERE id=?", mid, one=True)
        if not m or not m["source"].startswith("project:") or not (MESHES / mid / orientation.CAMERAS_FILE).exists():
            continue
        pdir = PROJECTS / m["source"].split(":", 1)[1]

        def log(line: str, mid=mid) -> None:
            with open(MESHES / mid / "splat.log", "a") as f:
                f.write(line + "\n")
        try:
            splat_mod.fit(MESHES / mid, pdir / "photos", pdir / "masks" if (pdir / "masks").is_dir() else None, log)
        except Exception as e:  # never let the thread die
            log(f"FAILED: {type(e).__name__}: {e}")


def queue_scale(mid: str) -> bool:
    """Queue finding a reconstructed mesh's scale (scale.py), unless it's being found; whether it was queued."""
    if not (MESHES / mid / "cameras.json").exists() or (scale_mod.load(MESHES / mid) or {}).get("status") in ("queued", "running"):
        return False
    scale_mod.save(MESHES / mid, {"status": "queued", "progress": None})
    queue_job({"type": "scale", "mid": mid})
    return True


KNOWN_OBJECT_FILE = "known_object.json"   # in a project: what of known size its photos show (scale.known_object())


def known_object(pid: str) -> dict | None:
    try:
        return json.loads((PROJECTS / pid / KNOWN_OBJECT_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def mesh_videos(mid: str) -> dict:
    """What each of a mesh's videos, and its uploader's channel, says about the climber's size, by url."""
    record = mesh_record(MESHES / mid) or {}
    urls = {p["url"] for p in record.get("photos", []) if p.get("source") == "youtube" and p.get("url")}
    rows = q(f"""SELECT v.url, v.height_cm, v.wingspan_cm, v.ape_index_cm, c.height_cm AS channel_height_cm,
                        c.wingspan_cm AS channel_wingspan_cm, c.ape_index_cm AS channel_ape_index_cm
                 FROM videos v LEFT JOIN channels c ON c.channel_url=v.channel_url WHERE v.url IN ({",".join("?" * len(urls))})""",
             *urls) if urls else []
    return {r["url"]: r for r in rows}


def run_scale(mid: str) -> None:
    """Find how big a reconstructed mesh is (scale.py)."""
    m = q("SELECT source FROM meshes WHERE id=?", mid, one=True)
    if not m or not m["source"].startswith("project:"):
        return
    mesh_dir, pid = MESHES / mid, m["source"].split(":", 1)[1]
    if mesh_old(mid):   # became old while queued: what replaces it gets its own
        scale_mod.save(mesh_dir, {**(scale_mod.load(mesh_dir) or {}), "status": "stopped", "progress": None,
                                  "note": "not looked for: the mesh is old"})
        return

    def log(line: str) -> None:
        with open(mesh_dir / "scale.log", "a") as f:
            f.write(line.rstrip() + "\n")
    climbs = [json.loads(r["data"]) for r in q("SELECT c.data FROM mesh_links l JOIN climbs c ON c.id=l.climb_id WHERE l.mesh_id=?", mid)
              or q("SELECT c.data FROM meshes m JOIN climbs c ON c.area_path=m.boulder WHERE m.id=?", mid)]   # a boulder's: all on it
    try:
        need_back([pid], "finding its size")
        if offloading():
            remote_scale(mid, pid, climbs, log)
        else:
            scale_mod.estimate(mesh_dir, PROJECTS / pid / "photos", PROJECTS / pid / "masks", climbs, mesh_videos(mid), log,
                               known=known_object(pid))
    except compute.ComputeUnavailable as e:     # queued again once a machine is there (beta_worker); not at once if none took it
        log(f"scale: waiting for a machine to find it on: {e}")
        scale_mod.save(mesh_dir, {**(scale_mod.load(mesh_dir) or {}), "status": MACHINE_WAIT, "progress": None,
                                  "machine_wait": time.time() + TAKE_BACKOFF if isinstance(e, pullwork.NotTaken) else None})
    except Cancelled:
        scale_mod.save(mesh_dir, {**(scale_mod.load(mesh_dir) or {}), "status": "stopped", "progress": None})
    except Exception as e:
        log(f"FAILED: {type(e).__name__}: {e}")
        scale_mod.save(mesh_dir, {**(scale_mod.load(mesh_dir) or {}), "status": f"failed: {type(e).__name__}: {e}", "progress": None})
    if skyline_wanted(mid):     # its skyline's parallax check needed its size
        queue_skyline(mid)


# ------------------------------------------------------------------ beta and size, on machines that connect here
MESH_INPUT = {".glb", ".ply", ".json", ".bin"}   # what of a mesh's folder its beta and size need: not textures
MACHINE_WAIT = "waiting for a machine"
TAKE_WITHIN = 3600      # seconds a machine has to take a beta or size job, before it's put back for later (pullwork.NotTaken)
TAKE_BACKOFF = 1800     # seconds one put back so waits before it's tried again


def offloading() -> bool:
    """Whether finding beta and size goes to the machines that connect here too: when this server meshes nothing itself."""
    return not _local_meshing and _executor is MACHINES


def mesh_inputs(mid: str, pid: str, photos: list[str]) -> dict[str, Path]:
    """A mesh's own files, and those of its project's photos and people masks named, for a job (pullwork.Board.task)."""
    out = {f"mesh/{f.name}": f for f in (MESHES / mid).iterdir()
           if f.is_file() and f.suffix in MESH_INPUT and f.name != meshclean.CLEANED_FILE}
    for name in photos:
        for kind, f in (("photos", PROJECTS / pid / "photos" / name), ("masks", PROJECTS / pid / "masks" / f"{name}.png")):
            if f.is_file():
                out[f"{kind}/{f.name}"] = f
    return out


def take_made(back: Path, mid: str, kind: str) -> list[str]:
    """Move what a job made (remote_job.MADE) into the mesh's folder."""
    import remote_job
    moved = []
    for f in (back / "out").iterdir() if (back / "out").is_dir() else []:
        if f.is_file() and remote_job.MADE[kind].fullmatch(f.name):
            os.replace(f, MESHES / mid / f.name)
            moved.append(f.name)
    return moved


def remote_beta(mid: str, pid: str, log) -> None:
    """beta_mod.find(), on a machine that connects here: with its videos from the cache, cut to the seconds it looks at.

    Raises:
        youtube_mod.Refused: some of its videos aren't in the cache, for volunteers to fetch (as run_beta() does).
    """
    mesh_dir = MESHES / mid
    found = beta_mod.videos(mesh_dir)
    rec = beta_mod.load(mesh_dir) or {}
    done = {v["url"] for v in rec.get("videos", []) if rec.get("resume") and v.get("status") in ("done", "no holds found")}
    todo = [u for u in found if u not in done]
    if any(not youtube_mod.cached(u) for u in todo):
        raise youtube_mod.Refused({"since": time.time(), "until": time.time() + 3600, "level": 0, "volunteers": True,
                                   "error": "some of its videos aren't here yet"})
    stage = Path(tempfile.mkdtemp(prefix="bm-beta-", dir=DATA / "compute"))
    try:
        files = mesh_inputs(mid, pid, sorted({n for v in found.values() for n in v["frames"]}))
        for url in todo:
            src = youtube_mod.cached(url)
            dst = stage / f"{youtube_mod.video_id(url) or src.stem}{src.suffix}"
            try:
                jobctl.run(["ffmpeg", "-loglevel", "error", "-y", "-t", str(beta_mod.MAX_SECONDS), "-i", str(src), "-an", "-c", "copy",
                            str(dst)], timeout=600)
            except Exception:
                shutil.copy(src, dst)
            files[f"videos/{dst.name}"] = dst
        beta_mod.save(mesh_dir, {**rec, "status": "running", "progress": "on a machine that connects here", "started": time.time()})
        _, back = MACHINES.task("beta", files, {}, f"beta: {mid}", log, take_within=TAKE_WITHIN)
        try:
            log(f"beta: back from the machine: {', '.join(take_made(back, mid, 'beta')) or 'nothing'}")
        finally:
            shutil.rmtree(back, ignore_errors=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def remote_scale(mid: str, pid: str, climbs: list[dict], log) -> None:
    """scale_mod.estimate(), on a machine that connects here."""
    mesh_dir = MESHES / mid
    names = [c["name"] for c in json.loads((mesh_dir / "cameras.json").read_text())]
    scale_mod.save(mesh_dir, {**(scale_mod.load(mesh_dir) or {}), "status": "running", "progress": "on a machine that connects here"})
    _, back = MACHINES.task("scale", mesh_inputs(mid, pid, names), {"climbs": climbs, "videos": mesh_videos(mid), "known": known_object(pid)},
                            f"size: {mid}", log, take_within=TAKE_WITHIN)
    try:
        log(f"scale: back from the machine: {', '.join(take_made(back, mid, 'scale')) or 'nothing'}")
    finally:
        shutil.rmtree(back, ignore_errors=True)


FILTER_VERSION = 12     # the oldest machines' script that filters a harvest's photos (remote_job.py --task filter)
FILTER_TAKE = 180       # seconds a machine has to take a harvest's filtering, before it's done here instead


class _StepProgress:
    """A remote job's progress (compute.follow() gives it a tracker) to a harvest step's progress(fraction, detail),
    which once the machine reports says the step is on a machine (Tracker.callback()'s at())."""

    def __init__(self, progress):
        self.progress, self.heard = progress, False

    def update(self, key: str, frac: float = 0.0, detail: str | None = None) -> None:
        if self.progress:
            if not self.heard and hasattr(self.progress, "at"):
                self.progress.at("machine")
            self.heard = True
            self.progress(frac, detail)


def filter_anywhere(paths: list[Path], rejected: Path | None, log, **kw) -> dict:
    """relevance_mod.RUNNER: a harvest's filtering (relevance.filter_photos()) on a machine that connects here, when this
    server meshes on machines (offloading()), the admin page's relevance.machines is on, and one with a script that
    does it has been in touch; otherwise, or if no machine takes it within FILTER_TAKE or it fails, here (filter_here(),
    one at a time). Either way the report is the same, and the photos it rejects are moved here."""
    if not (paths and settings().get("relevance.machines") and offloading() and MACHINES.can(FILTER_VERSION)) or kw.get("anchors") is not None:
        return filter_here(paths, rejected, log, **kw)
    try:
        return filter_remote(paths, rejected, log, **kw)
    except Cancelled:
        raise
    except Exception as e:
        log(f"relevance: not on a machine ({e if isinstance(e, compute.ComputeUnavailable) else f'{type(e).__name__}: {e}'}): "
            "filtering here")
        if hasattr(kw.get("progress"), "at"):
            kw["progress"].at("server")
        return filter_here(paths, rejected, log, **kw)


_filtering_here = threading.Lock()      # the server's own filtering: one harvest's at a time (filter_here())


def filter_here(paths: list[Path], rejected: Path | None, log, **kw) -> dict:
    """relevance.filter_photos() on this server, one harvest's at a time, as two at once spun in the kernel for 5 hours on
    the 2-core public server (Nov 12, 2026). One waiting for another says so in its step, and can be stopped meanwhile."""
    if not _filtering_here.acquire(blocking=False):
        log("relevance: waiting for another harvest's filtering here to finish")
        if kw.get("progress"):
            kw["progress"](0.0, "waiting for another harvest's filtering on the server")
        while not _filtering_here.acquire(timeout=2):
            jobctl.check()
    try:
        return relevance_mod.filter_photos(paths, rejected, log, **kw)
    finally:
        _filtering_here.release()


def filter_remote(paths: list[Path], rejected: Path | None, log, methods: list[str] | None = None, progress=None,
                  more_anchors: list[Path] | None = None, prior: dict | None = None, anchors: None = None) -> dict:
    """relevance.filter_photos() on a machine that connects here: its photos, the other harvests' anchors, and what
    this server's cache knows of them go; its report and the pairs it matched come back (kept in the cache here),
    and the photos it rejects are moved to rejected here."""
    stage = Path(tempfile.mkdtemp(prefix="bm-filter-", dir=DATA / "compute"))
    try:
        files = {f"photos/{p.name}": p for p in paths}
        more = {f"more/{i}/{p.name}": p for i, p in enumerate(more_anchors or [])}
        files.update(more)
        relevance_mod.export_cache(list(paths) + list(more_anchors or []), stage / "cache.sqlite")
        files["cache.sqlite"] = stage / "cache.sqlite"
        pid = rejected.parent.name if rejected is not None else None
        name = (q("SELECT name FROM projects WHERE id=?", pid, one=True) or {}).get("name") if pid else None
        log(f"relevance: filtering {plural(len(paths), 'photo')} on a machine that connects here")
        if progress:    # the step starts, waiting for a machine to take it
            if hasattr(progress, "at"):
                progress.at("waiting")
            progress(0.0)
        _, back = MACHINES.task("filter", files, {"methods": methods, "more": sorted(more), "prior": prior,
                                                  "matcher": relevance_mod.MATCHER},
                                f"filter: {name or pid or 'photos'}", log, tracker=_StepProgress(progress), version=FILTER_VERSION,
                                take_within=FILTER_TAKE)
        try:
            report = json.loads((back / "out" / "relevance.json").read_text())
            relevance_mod.merge_cache(back / "out" / "cache.sqlite")
        finally:
            shutil.rmtree(back, ignore_errors=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if rejected is not None:
        for p in paths:
            if p.name in report["rejected"] and p.exists():
                rejected.mkdir(exist_ok=True)
                p.rename(rejected / f"{report['rejected'][p.name]}_{p.name}")
    return report


def requeue_waiting_scale() -> None:
    """Queue again the sizes that waited for a machine, once one is there."""
    if not (offloading() and MACHINES.connected()):
        return
    for d in MESHES.iterdir() if MESHES.exists() else []:
        rec = scale_mod.load(d) or {}
        if rec.get("status") == MACHINE_WAIT and (rec.get("machine_wait") or 0) <= time.time() and not mesh_old(d.name):
            queue_scale(d.name)


def run_retexture(mid: str) -> None:
    """Texture a mesh again from only its photos that are free to copy (retexture.py)."""
    m = q("SELECT source FROM meshes WHERE id=?", mid, one=True)
    if not m or not m["source"].startswith("project:"):
        return
    mesh_dir = MESHES / mid

    def log(line: str) -> None:
        with open(mesh_dir / "retexture.log", "a") as f:
            f.write(line.rstrip() + "\n")
    try:
        if photos_away(m["source"].split(":", 1)[1]):
            log("its photos are in the backup: not textured again (Make textures... once they're back)")
            return
        retexture.retexture(mesh_dir, PROJECTS / m["source"].split(":", 1)[1] / "photos", log, video_info)
    except Exception as e:
        log(f"FAILED: {type(e).__name__}: {e}")
    finally:
        _retexturing.discard(mid)


_retexturing: set[str] = set()


# ------------------------------------------------------------------ what the site shows (policy.py)
def texture_mode(mid: str, source: str | None = None) -> str:
    """How a mesh's texture is shown: "full", "permitted" (from permitted/) or "hidden"."""
    if source is None:
        row = q("SELECT source FROM meshes WHERE id=?", mid, one=True)
        source = row["source"] if row else ""
    return policy.texture_mode(settings().get("site.textures"), source == "upload", mesh_record(MESHES / mid),
                               retexture.has_variant(MESHES / mid))


def served_dir(mid: str, mode: str | None = None) -> Path:
    """The folder a mesh's files are served from: permitted/ when that's the version shown."""
    return MESHES / mid / retexture.DIR if (mode or texture_mode(mid)) == "permitted" else MESHES / mid


_sources_cache: dict[str, tuple[float, dict]] = {}


def photo_entry(pid: str, name: str) -> dict:
    """What's known of where one of a project's photos came from, for policy.source_access."""
    path = PROJECTS / pid / "sources.json"
    stamp = path.stat().st_mtime if path.exists() else 0.0
    if _sources_cache.get(pid, (None,))[0] != stamp:
        _sources_cache[pid] = (stamp, provenance.harvested(PROJECTS / pid))
    return provenance.describe(name, _sources_cache[pid][1])


def record_put_back(pid: str, name: str) -> None:
    """Keep in a project's filter report (relevance.json) that a photo it rejected was put back by hand, so that
    harvesting or filtering it again keeps it (relevance.filter_photos()'s put_back)."""
    path = PROJECTS / pid / "relevance.json"
    try:
        rep = json.loads(path.read_text())
    except (OSError, ValueError):
        rep = {}
    (rep.get("rejected") or {}).pop(name, None)
    if isinstance(rep.get("kept"), list) and name not in rep["kept"]:
        rep["kept"].append(name)
    rep["put_back"] = sorted(set(rep.get("put_back") or []) | {name})
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(rep, indent=1))
    tmp.replace(path)


PHOTO_THUMBS = DATA / "cache" / "photo-thumbs"     # small copies of projects' photos (photo_thumb())
THUMB_PX = 320
SMALL_PX = 160      # the most a photo not clearly free to copy is shown at (site.small_thumbs), as image search engines do


def photo_thumb(pid: str, folder: str, name: str, px: int = THUMB_PX) -> Path | None:
    """A small JPEG of one of a project's photos (in folder: "photos" or "rejected"), at most px on a side, made the
    first time it's asked for and again if the photo changes, or None if the photo isn't here."""
    src = PROJECTS / pid / folder / name
    if not src.is_file():
        return None
    dst = PHOTO_THUMBS / pid / f"{folder}_{name}{'' if px == THUMB_PX else f'.{px}'}.jpg"
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        from PIL import Image
        dst.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im.draft("RGB", (px * 2, px * 2))   # JPEG: decode at a fraction of full size
            im = im.convert("RGB")
            im.thumbnail((px, px))
            tmp = dst.with_suffix(".part")
            im.save(tmp, "JPEG", quality=80)
            tmp.replace(dst)
    return dst


def photo_groups(pid: str, climb: str | None = None) -> list[dict]:
    """A project's photos by where they came from, in the page's order of sources (as SOURCE_INFO), each with what
    the site may show of it ({file, show, page, small: shown only as a SMALL_PX copy, site.small_thumbs}). With climb, a boulder's pooled photos of only that climb's
    harvests ("own": the boulder's own search), as boulder_panel() counts them."""
    photos = PROJECTS / pid / "photos"
    names = sorted(f.name for f in photos.iterdir() if f.suffix.lower() in PHOTO_EXT) if photos.is_dir() else []
    if climb:
        try:
            files = {f["file"]: f.get("pooled_from") for f in json.loads((PROJECTS / pid / "sources.json").read_text()).get("files", [])}
            whose = {h: c["id"] for c in json.loads((PROJECTS / pid / CLIMBS_FILE).read_text())["climbs"] for h in c.get("projects") or []}
        except (OSError, ValueError, KeyError):
            files, whose = {}, {}
        names = [n for n in names if (whose.get(files[n]) if files.get(n) else None) == (None if climb == "own" else climb)]
    setting, small = settings().get("site.sources"), settings().get("site.small_thumbs")
    by: dict[str, list[dict]] = {}
    for n in names:
        e = photo_entry(pid, n)
        show = policy.source_access(setting, e)
        by.setdefault(e.get("source") or "other", []).append({"file": n, "show": show, "page": e.get("page") or e.get("url"),
                                                                 "small": show != "show" and small})
    order = ["ddg", "youtube", "commons", "mountainproject", "openbeta", "flickr", "google", "upload"]
    return [{"source": k, "photos": by[k]} for k in sorted(by, key=lambda k: order.index(k) if k in order else len(order))]


def video_info(url: str) -> dict | None:
    """What the videos table knows of a video, for provenance.load: its licence and uploader; None if nothing."""
    row = q("SELECT license, uploader FROM videos WHERE url=?", url, one=True)
    return {"license": row["license"], "uploader": row["uploader"]} if row else None


def video_entry(url: str) -> dict:
    """A video as a photo's record, for policy.permitted and source_access."""
    return {"source": "youtube", **(video_info(url) or {"license": None})}


def mesh_record(mesh_dir: Path) -> dict | None:
    """A mesh's provenance, with its frames' licences and uploaders filled in from the videos table where the
    harvest hadn't recorded them: older harvests didn't, and a video's licence is learnt from its metadata later."""
    return provenance.load(mesh_dir, video_info)


def stop_campaign(camp_id: str, now: bool = True) -> None:
    """Stop a campaign, now or once the climbs it's on are finished."""
    on = [w for w in _workers if w.campaign == camp_id]
    q("UPDATE campaigns SET status=?, note=NULL, only_retries=0 WHERE id=? AND status IN ('running','stopping')",
      "stopping" if on and not now else "stopped", camp_id, write=True)
    if now:
        for w in on:
            w.control.stop()


TYPICAL_EVERY = 600    # seconds between reading step_runs again (and at once after a step's kept)
_typicals: dict = {"at": 0.0, "by": {}}


def step_runs() -> dict[str, list[dict]]:
    """step_runs by step, newest first (read again every TYPICAL_EVERY seconds, or after a step's kept)."""
    if time.time() - _typicals["at"] >= TYPICAL_EVERY:
        by: dict[str, list[dict]] = {}
        for r in q("SELECT step, kind, machine, units, seconds FROM step_runs ORDER BY started DESC LIMIT 20000"):
            by.setdefault(r["step"], []).append(r)
        _typicals.update(at=time.time(), by=by)
    return _typicals["by"]


def backfill_step_runs() -> None:
    """At the first start with step_runs, fill it from each project's last job's progress (all that was kept of
    runs before), its photos as its harvest found, and where each step ran ("machine" for any of them)."""
    if q("SELECT 1 FROM step_runs LIMIT 1"):
        return
    n = 0
    for r in q("SELECT id, kind, progress, harvest FROM projects WHERE progress IS NOT NULL"):
        try:
            steps = json.loads(r["progress"])["steps"]
            units = json.loads(r["harvest"]).get("images") if r["harvest"] else None
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        for st in steps:
            row = steptimes_mod.row(r["id"], st, r["kind"], "server" if st.get("where") in (None, "server", "server+volunteers")
                                    else st["where"], units)
            if row:
                n += bool(q("INSERT OR IGNORE INTO step_runs VALUES(?,?,?,?,?,?,?)", *row, write=True))
    print(f"step_runs: {n} step(s) from projects' last jobs", flush=True)


def with_typicals(progress: dict, harvest: str | None, pid: str | None = None) -> dict:
    """A job's progress (a tracker's snapshot), each step still to finish given how long it'll probably take
    ("typical", seconds, steptimes.estimate()): from runs of the same kind of project, on the machine it's on (a step
    that's begun: where it is; one to come: the machine meshing it now, if one is), of about as many photos as it has
    (or for a boulder's "climbs" step, climbs), for its time left in the UI and deploy.sh."""
    runs = step_runs()
    units = kind = machine = None
    if pid:
        units, kind = project_units(pid), (q("SELECT kind FROM projects WHERE id=?", pid, one=True) or {}).get("kind")
        machine = step_machine(pid, "machine") if progress.get("where") == "machine" else None
        machine = None if machine == "machine" else machine
    else:
        try:
            units = json.loads(harvest).get("images") if harvest else None
        except (TypeError, ValueError, AttributeError):
            pass
    for st in progress.get("steps", []):
        if st.get("state") in ("pending", "running"):
            on = step_machine(pid, st["where"]) if pid and st.get("where") else machine
            t = steptimes_mod.estimate(runs.get(st["key"], []), st["key"], st.get("units", units), kind, None if on == "machine" else on)
            if t is not None:
                st["typical"] = round(t, 1)
    return progress


PAUSED_NOTE = "paused, carrying on once resumed"    # in an area's note: how many of its climbs, with nothing else to take
BLOCKED_NOTE = "blocked, as what they need was lost from the backup: the admin page says what to do"    # and so


def pause_item(camp_id: str, source_id: str, on: bool = True) -> None:
    """Pause one of an area's climbs, still to go or in progress, or resume it. Paused, no worker takes it until it's
    resumed, while the area goes on with its others; one in progress stops now (its worker alone, where stop_campaign()
    stops them all), and when resumed goes next, carrying on from the last step it finished, as after a stop. A restart
    leaves it paused.

    Raises:
        KeyError: the area has no such climb.
        ValueError: it's finished (or waits for something), so there's nothing to pause.
    """
    row = q("SELECT name, status FROM campaign_items WHERE campaign_id=? AND source_id=?", camp_id, source_id, one=True)
    if not row:
        raise KeyError(source_id)
    if not on:
        set_item(camp_id, source_id, paused=None)
        return
    if row["status"] not in ("pending", "running"):
        raise ValueError(f"{row['name']} isn't to go or in progress ({row['status']}): there's nothing to pause")
    set_item(camp_id, source_id, paused=time.time())
    for w in list(_workers):
        if w.campaign == camp_id and w.item == source_id:
            w.control.stop()


def campaign_view(camp: dict) -> dict:
    """A campaign for the UI, with where it's got to."""
    cid = camp["id"]
    counts = {r["status"]: r["n"] for r in q("SELECT status,COUNT(*) n FROM campaign_items WHERE campaign_id=? GROUP BY status", cid)}
    again = {key: q(f"SELECT COUNT(*) n FROM campaign_items WHERE campaign_id=? AND {where}", cid, one=True)["n"]
             for key, where in (("changed", "changed=1"), ("queued", "redo IS NOT NULL AND status IN ('pending','running') AND COALESCE(old_rerun, 0)=0"))}
    again["boulders"] = len(boulders_to_mesh(cid))
    kinds, leaves = boulder_kinds(), q("SELECT boulder, COUNT(*) n FROM campaign_items WHERE campaign_id=? GROUP BY boulder", cid)
    found = [leaf_kind(r["boulder"], kinds) for r in leaves if r["boulder"]]
    listed = {"climbs": sum(r["n"] for r in leaves), "boulders": found.count("boulder"), "groups": found.count("group"),
              "unread": len(found) - found.count("boulder") - found.count("group")}    # its lowest areas, as MP says
    again["triable"] = len(triable(cid))
    again["old"] = q(f"SELECT COUNT(*) n FROM campaign_items ci JOIN meshes m ON m.id=ci.mesh_id WHERE ci.campaign_id=? AND {OLD_MESH}",
                     cid, one=True)["n"]     # climbs whose mesh was made before the rules (MESH_RULES): not done yet
    worked = q("SELECT rank,name,grade,stars,url,climb_id,mesh_id,project_id,status,note,updated FROM campaign_items "
               "WHERE campaign_id=? AND status NOT IN ('pending','running') ORDER BY updated DESC LIMIT 300", cid)
    running = q("SELECT rank,name,grade,stars,project_id,source_id,boulder,redo,pool,updated,paused,old_rerun FROM campaign_items "
                "WHERE campaign_id=? AND status='running' ORDER BY updated", cid)
    for r in running:
        # one meshing its boulder (or a group its photos matched) is on that project, not its own harvest, which is done
        first, pool = (r["redo"] or "").split("+")[0], r.pop("pool", None)
        whole = boulder_project(r["boulder"]) if first in ("boulder", "pool") and r["boulder"] else \
            boulder_project(pool) if first == "cluster" and pool else None
        r["meshing"] = {"kind": "group" if first == "cluster" else "boulder", "name": whole["name"].removesuffix(" (boulder)"),
                        "project_id": whole["id"]} if whole else None
        p = q("SELECT status,progress,harvest FROM projects WHERE id=?", whole["id"] if whole else r["project_id"], one=True) \
            if whole or r["project_id"] else None
        r["progress"] = with_typicals(json.loads(p["progress"]), p["harvest"], whole["id"] if whole else r["project_id"]) \
            if p and p["progress"] else None
        try:
            r["boulder_panel"] = boulder_panel(camp, r)
        except Exception as e:      # only the panel is lost
            print(f"error: the boulder panel of {r['name']}: {type(e).__name__}: {e}", flush=True)
            r["boulder_panel"] = None
    paused = q("SELECT rank,name,grade,stars,source_id,project_id,note,paused FROM campaign_items "
               "WHERE campaign_id=? AND status='pending' AND paused IS NOT NULL ORDER BY paused", cid)
    if paused:
        counts["paused"] = len(paused)
    pending = ordered_pending(camp)
    # queued again as their meshes are old (queue_old()): at low priority, counted and listed apart from its own
    old_next = [i for i in ordered_pending(camp, old=True) if i["old_rerun"]]
    old_counts = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM campaign_items WHERE campaign_id=? AND old_rerun=1 "
                                                  "AND status IN ('pending','running','waiting','harvested') AND paused IS NULL "
                                                  "GROUP BY status", cid)}
    for k, n in old_counts.items():
        counts[k] = counts.get(k, 0) - n
    made = None
    if camp.get("max_meshes"):      # those past its limit aren't to go
        counts["beyond"] = counts.get("pending", 0) - len(paused) - len(pending)
        counts["pending"] = len(pending) + len(paused)
        made = limit_scope(camp)[1]
    last_mesh = q("SELECT MAX(updated) t FROM campaign_items WHERE campaign_id=? AND status='done' AND mesh_id IS NOT NULL",
                  cid, one=True)["t"]
    old_queued = sum(old_counts.values())
    # its climbs still to go whose photos are offloaded, to be brought back from the backup first; and those blocked as lost
    needs_backup = backup_needs([r["project_id"] for r in q("SELECT project_id FROM campaign_items WHERE campaign_id=? AND status IN "
                                                            "('pending','waiting','harvested') AND project_id IS NOT NULL", cid)])
    blocked = q("SELECT COUNT(*) n FROM campaign_items WHERE campaign_id=? AND blocked IS NOT NULL", cid, one=True)["n"]
    return {**camp, "counts": counts, "again": again, "listed": listed, "old_queued": old_queued, "needs_backup": needs_backup,
            "blocked": blocked, "worked": worked, "current": running[0] if running else None,
            "running": running, "paused": paused, "next": pending[:10], "old_next": old_next[:10], "machine_wait": machine_wait(cid), "made": made, "last_mesh": last_mesh}


def active_areas(camps: list[dict]) -> set[str]:
    """The running areas being worked on: those a worker is on now, or waits on a machine for, and while fewer than
    there are workers, the next a free worker would take (_claim(): the oldest with a climb to go). The rest of the
    running ones wait their turn."""
    workers = [w for w in _workers if not w.retired]
    on = {w.campaign for w in workers if w.campaign} | {w.waiting["campaign"] for w in workers if w.waiting}
    for c in sorted((c for c in camps if c["status"] == "running"), key=lambda c: (c.get("priority") or 0, c["created"])):
        if len(on) >= len(workers):
            break
        if c["id"] not in on and ordered_pending(c):
            on.add(c["id"])
    return on


def campaigns_listed() -> list[dict]:
    """Every area for the Areas pane, each with "state": "running" for one being worked on (active_areas()), "queued"
    for one running that waits its turn, "waiting" for one running with no climb to take now (they wait for volunteers,
    or for the rest of their boulder's), otherwise its status; and "place", its place in the order areas are taken.
    Those being worked on first, the one with the newest mesh first; then those queued and waiting, in the order they're
    taken; then the rest, newest first."""
    camps = q("SELECT * FROM campaigns ORDER BY created DESC")
    active = active_areas(camps)
    order = [c["id"] for c in sorted((c for c in camps if c["status"] == "running"), key=lambda c: (c.get("priority") or 0, c["created"]))]
    out = []
    for c in camps:
        v = campaign_view(c)
        v["state"] = (("running" if c["id"] in active else "queued" if v["next"] else "waiting") if c["status"] == "running"
                      else c["status"])
        own_going = c["status"] == "stopping" or (c["status"] == "running" and v["next"])
        if v["running"] and all(r["old_rerun"] for r in v["running"]) and not own_going:
            v["state"] = "low priority"     # only remaking its old meshes (queue_old()), whatever its status
        v["place"] = order.index(c["id"]) + 1 if c["id"] in order else None
        out.append(v)
    rank = {"running": 0, "stopping": 0, "low priority": 0, "queued": 1, "waiting": 1}
    out.sort(key=lambda v: (rank.get(v["state"], 2), -(v["last_mesh"] or 0) if rank.get(v["state"]) == 0 else
                            v["place"] if rank.get(v["state"]) == 1 else -v["created"]))
    return out


def _image_count(folder: Path) -> int:
    return sum(1 for f in folder.iterdir() if f.suffix.lower() in (".jpg", ".jpeg", ".png")) if folder.is_dir() else 0


def boulder_panel(camp: dict, item: dict) -> dict | None:
    """For an area's climb being worked on that's harvested for its boulder's mesh, or the boulder's mesh itself
    (pooled()): the boulder's name and climbs, each with what it gives the mesh. None for a climb meshed on its own.

    Returns:
        {name, url (its page), pooled (whether these are the boulder's own photos, after its filter, by the harvest they came from,
        rather than each climb's own harvest's), rows: [{name, grade, rank, listed (on the area's list), state
        ("pending": not harvested yet, "in": gives photos, "out": none), photos (page photos and video frames too),
        why (the climb that brought the boulder in: the first of them in the area's order), url (its Mountain Project
        page), photos_of (where its photos are, for photo_groups())}], own (photos of the boulder's own search, once
        pooled), harvested_at (when the last of its climbs' harvests finished, once none is still to come), waiting
        (harvested, and its mesh not started yet)}.
    """
    boulder = item.get("boulder")
    if not boulder or not pooled(camp["id"], boulder):
        return None
    key = boulder
    listed = q("SELECT source_id, name, grade, rank, stars, media, status, climb_id, project_id FROM campaign_items "
               "WHERE campaign_id=? AND boulder=? ORDER BY rank", camp["id"], boulder)
    first = _in_order(camp, listed)[0]["source_id"] if listed else None
    rows = []
    for r in listed:
        photos = _image_count(PROJECTS / r["project_id"] / "photos") if r["project_id"] else 0
        harvested = r["status"] not in ("pending", "running", "waiting")
        rows.append({"climb": r["climb_id"], "name": r["name"], "grade": r["grade"], "rank": r["rank"], "listed": True,
                     "state": "pending" if not harvested else "in" if photos and r["status"] != "failed" else "out",
                     "photos": photos if harvested or photos else None, "why": r["source_id"] == first,
                     "projects": [r["project_id"]] if r["project_id"] else []})
    known = {r["climb"] for r in rows}
    for c in boulder_climbs(key):       # others on it the boulder's mesh harvests too (run_boulder())
        if c["id"] in known:
            continue
        harvests = climb_harvests(c["id"])
        photos = _image_count(PROJECTS / harvests[0] / "photos") if harvests else None
        rows.append({"climb": c["id"], "name": c["name"], "grade": c["grade"], "rank": None, "listed": False,
                     "state": "in" if photos else "pending", "photos": photos, "why": False, "projects": harvests})
    urls = {c["id"]: c["url"] for c in q(f"SELECT id, url FROM climbs WHERE id IN ({','.join('?' * len(rows))})",
                                           *[r["climb"] for r in rows])} if rows else {}
    for r in rows:      # where the photos counted are, for the page to show them (photo_groups())
        r["photos_of"] = {"project": r["projects"][0]} if r["projects"] and r["photos"] else None
        r["url"] = urls.get(r["climb"])
    ends = [(PROJECTS / pid / "sources.json").stat().st_mtime for r in rows for pid in r["projects"][:1]
            if (PROJECTS / pid / "sources.json").exists()]
    harvested_at = max(ends) if ends and not any(r["state"] == "pending" for r in rows) else None
    whole = boulder_project(key)
    started = whole and (whole["status"] == "done" or any(
        st.get("started") for st in (json.loads(whole["progress"]) if whole["progress"] else {}).get("steps", [])
        if st["key"] in {k[0] for k in pipeline.steps()}))
    out = {"name": boulder_name(key), "url": boulder_url(key), "pooled": False, "rows": rows, "own": None,
           "harvested_at": harvested_at, "waiting": bool(harvested_at and not started),
           "needs_backup": backup_needs([pid for r in rows for pid in r["projects"]])}     # harvests offloaded, to bring back
    proj = boulder_project(key) if (item.get("redo") or "").split("+")[0] in ("boulder", "pool") else None
    sources = PROJECTS / proj["id"] / "sources.json" if proj else None
    if sources and sources.exists() and sources.stat().st_mtime >= (item.get("updated") or 0) - 5:     # pooled this time
        try:
            files = {f["file"]: f.get("pooled_from") for f in json.loads(sources.read_text()).get("files", [])}
            record = json.loads((PROJECTS / proj["id"] / CLIMBS_FILE).read_text())["climbs"]
        except (OSError, ValueError, KeyError):
            return out
        whose = {pid: c["id"] for c in record for pid in c.get("projects") or []}
        kept = [f.name for f in (PROJECTS / proj["id"] / "photos").iterdir() if f.suffix.lower() in (".jpg", ".jpeg", ".png")] \
            if (PROJECTS / proj["id"] / "photos").is_dir() else []
        by: dict[str | None, int] = {}
        for name in kept:
            src = files.get(name)
            by[whose.get(src) if src else None] = by.get(whose.get(src) if src else None, 0) + 1
        done = proj["status"] in ("harvested", "running", "done", "failed") or bool(proj.get("harvest"))
        for r in rows:
            n = by.get(r["climb"], 0) if r["climb"] else 0      # a climb never harvested has none: not the boulder's own
            r.update(photos=n, state=("in" if n else "out") if done else ("in" if n else "pending"),
                     photos_of={"project": proj["id"], "climb": r["climb"]} if r["climb"] else None)
        out.update(pooled=True, own=by.get(None, 0), own_of={"project": proj["id"], "climb": "own"})
    return out


_idle_why: dict = {"at": 0.0, "why": None}


def machines_idle_why() -> str | None:
    """Why this server has no job for a machine asking for one: what its areas are doing instead (harvesting, climbs
    harvested and waiting for the rest of their boulder's, or waiting for volunteers), and what finding beta waits for.
    Worked out at most once a minute, as idle machines ask every 15 s."""
    if time.time() - _idle_why["at"] < 60:
        return _idle_why["why"]
    parts = []
    for w in _workers:
        if w.campaign and w.doing and not w.retired:
            name = (q("SELECT name FROM campaigns WHERE id=?", w.campaign, one=True) or {}).get("name", "an area")
            parts.append(f"{name} is harvesting {w.doing}")
    running = q("SELECT id FROM campaigns WHERE status='running'")
    if running:
        ids = [c["id"] for c in running]
        marks = ",".join("?" * len(ids))
        n = {r["status"]: r["n"] for r in q(f"SELECT status, COUNT(*) n FROM campaign_items WHERE campaign_id IN ({marks}) "
                                              "AND status IN ('harvested','waiting','pending') GROUP BY status", *ids)}
        if n.get("harvested"):
            parts.append(f"{n['harvested']} climb(s) harvested, waiting for the rest of their boulder's before it's meshed")
        if n.get("waiting"):
            parts.append(f"{n['waiting']} waiting for volunteers")
        if n.get("pending"):
            parts.append(f"{n['pending']} still to harvest")
    else:
        parts.append("no area running")
    beta = [beta_mod.load(MESHES / mid) or {} for mid in beta_queue()]
    if beta:
        videos = {u for r in beta for u in r.get("waiting_videos") or [] if not youtube_mod.cached(u)}
        parts.append(f"finding beta: {len(beta)} mesh(es) queued" + (f", waiting for {len(videos)} video(s)" if videos else ""))
    _idle_why.update(at=time.time(), why="; ".join(parts) or None)
    return _idle_why["why"]


def machine_kind() -> str | None:
    """The kind of machine areas' climbs are meshed on besides this server, as the admin page sets it, for saying what
    a climb waits for; None if there's no other kind."""
    return {"pull": "a machine that connects here for work", "ssh": "one of the machines reached over SSH",
            "aws": "an AWS instance"}.get(compute_settings()["compute.provider"])


def machine_wait(cid: str) -> dict | None:
    """What an area's climbs wait for, when its workers found no machine to mesh on (next_campaign_step()): {climb,
    boulder (its boulder's mesh, not its own), kind (machine_kind()), why, since, next (when it's tried again), and for
    machines that connect here (pull), last_seen (any's last word, or None) and connected (one is there now)}; None if
    they don't."""
    waits = [w for w in _workers if w.waiting and w.waiting["campaign"] == cid and not w.retired]
    if not waits:
        return None
    first = min(waits, key=lambda w: w.waiting["since"])
    out = {k: first.waiting[k] for k in ("climb", "boulder", "why", "since")}
    out.update(kind=machine_kind(), next=min(w.wait_until for w in waits), last_seen=None, connected=False, pull=False)
    if compute_settings()["compute.provider"] == "pull":
        seen = [m["last_seen"] for m in MACHINES.state()["machines"] if m["status"] not in ("revoked", "denied") and m["last_seen"]]
        out.update(last_seen=max(seen) if seen else None, connected=MACHINES.connected(), pull=True)
    return out


def run_job(job: dict) -> None:
    """A job someone asked for, on this thread's worker."""
    w = current_worker()
    w.job = {"job": job, "started": time.time(), "from": job_progress(job) or 0.0, "thread": threading.get_ident()}
    jobctl.begin(low_priority=job["type"] in ("retexture", "scale", "boulder"))
    try:
        if job["type"] == "scale":
            run_scale(job["mid"])
        elif job["type"] == "retexture":
            run_retexture(job["mid"])
        elif job["type"] == "reconstruct":
            run_reconstruct(job["pid"], job.get("internet", False), resume=job.get("resume", False))
        elif job["type"] == "harvest":
            run_harvest(job["pid"], set(job["sources"]), job["reconstruct"], job.get("filter", True), resume=job.get("resume", False),
                        again=job.get("again", False), add=job.get("add", False))
        elif job["type"] == "retry":
            run_retry(job["pid"])
        elif job["type"] == "boulder":
            run_boulder(job["pid"], resume=job.get("resume", False))
    except Cancelled:   # stopped by hand (stop_project()), which said so: left stopped, whatever the job made of it
        if job.get("pid"):
            q("UPDATE projects SET status='stopped' WHERE id=?", job["pid"], write=True)
    except Exception as e:  # never let the worker die
        append_log(job.get("pid", "?"), f"worker error: {e!r}")
    finally:
        w.job = None


def worker(w: Worker | None = None) -> None:
    """A worker's thread: jobs people asked for first (on this server's workers only), then areas' climbs.
    Once the server is stopping, it takes no more of either."""
    w = w or _first
    _here.worker = w
    use_control(w.control)
    while not w.retired:
        if _stopping or _draining or time.time() < w.wait_until:
            time.sleep(2)
            continue
        use_processing_cores()      # each time round, as the admin page may have changed it
        if not w.remote:
            try:
                job = jobs.get(timeout=CAMPAIGN_POLL)
            except queue.Empty:
                job = None
            if job and _stopping:   # it came as the stop began: it stays queued, to be marked interrupted
                jobs.put(job)
                continue
            if job:
                run_job(job)
                continue
        if w.retired or not (w.remote or _local_meshing):   # retired while waiting for a job; areas' climbs are for
            continue                                        # other machines' workers
        try:
            if not next_campaign_step() and w.remote:
                time.sleep(CAMPAIGN_POLL)
        except (Exception, Cancelled) as e:  # never let the worker die
            print(f"campaign error on {w.name}: {e!r}", flush=True)


# ------------------------------------------------------------------ stopping, and what it would interrupt
STOP_WAIT = float(os.environ.get("STOP_WAIT_MINUTES", "10")) * 60   # docker-compose.yml's stop_grace_period is longer
_stopping: dict | None = None    # once a stop has begun: {since, until, waiting_for}
_draining: dict | None = None    # deploy.sh about to restart it: {since, now}: no new work taken; with now, no waiting on stop
AGAIN = {"reconstruct": "Retry on its project runs it", "harvest": "Retry on its project runs it", "retry": "Retry on its project runs it",
         "boulder": "Retry on its project runs it", "retexture": "Make textures... on the admin page runs it",
         "scale": "it's queued again by itself"}     # what runs a job after a restart interrupted it


def job_progress(job: dict) -> float | None:
    """How far a job someone asked for has got, in percent, if it says."""
    if job.get("pid"):
        try:
            return float(json.loads(q("SELECT progress FROM projects WHERE id=?", job["pid"], one=True)["progress"])["overall"])
        except (TypeError, ValueError, KeyError):
            return None
    if job["type"] == "scale":
        m = re.match(r"people in photo (\d+) of (\d+)", (scale_mod.load(MESHES / job["mid"]) or {}).get("progress") or "")
        return 90 * (int(m[1]) - 1) / int(m[2]) if m else None
    return None


def job_info(job: dict, run: dict | None = None) -> dict:
    """A job someone asked for, as /api/jobs gives it; if it's running, how far it's got, and roughly how many
    seconds it has left: from how fast it got on up to the last time its progress moved, less the time since (None
    until that's clear, or once a stall has outlasted it, as then there's no telling). Not from how long it's taken
    in all, which while a step says nothing of its progress (a boulder harvesting a climb) grew without end."""
    table, key = ("projects", "pid") if job.get("pid") else ("meshes", "mid")
    row = q(f"SELECT name FROM {table} WHERE id=?", job.get(key), one=True)
    out = {"type": job["type"], ("project_id" if key == "pid" else "mesh_id"): job.get(key), "name": row["name"] if row else job.get(key)}
    if run:
        now, at = job_progress(job), time.time()
        if now is not None and now != run.get("moved", (None,))[0]:
            run["moved"] = (now, at)      # run is the worker's record of the job: kept between calls
        moved = run.get("moved", (now, at))[1]
        took, gained = moved - run["started"], (now or 0.0) - run["from"]
        eta = took * (100 - now) / gained - (at - moved) if now is not None and took >= 60 and gained >= 2 else None
        out.update(started=run["started"], progress=now, eta=eta if eta is None or eta > 0 else None)
    return out


def jobs_state(board: list[dict] | None = None) -> dict:
    """What a restart would stop now: jobs people started outside areas, which it interrupts (running and
    queued), and areas' climbs, which it only pauses. A running job's "machine_job" is the job on a machine that
    connects here that it's waiting for, if it is: {id, state, machine}, from board (MACHINES.running()) if given."""
    with jobs.mutex:
        waiting = list(jobs.queue)
    runs = [w.job for w in list(_workers)]
    by_thread = {j["owner"]: j for j in (MACHINES.running() if board is None else board)}
    now = _beta_now and q("SELECT name FROM meshes WHERE id=?", _beta_now["mid"], one=True)

    def running(r: dict) -> dict:
        mj = by_thread.get(r.get("thread"))
        return {**job_info(r["job"], r), "machine_job": {k: mj[k] for k in ("id", "state", "machine")} if mj else None}
    return {"running": [running(r) for r in runs if r], "queued": [job_info(j) for j in waiting],
            "area_climbs": q("SELECT c.name AS area, i.name, i.project_id FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id "
                             "WHERE i.status='running' ORDER BY i.updated"),
            "beta": {"running": now["name"] if now else None, "queued": len(beta_queue()), "youtube": youtube_mod.refused()},
            "stopping": _stopping}


def minutes(s: float) -> str:
    return f"{max(1, round(s / 60))} min"


def job_label(j: dict) -> str:
    bits = [j["type"]] + ([f"{j['progress']:.0f}%"] if j.get("progress") is not None else []) + \
        ([f"about {minutes(j['eta'])} left"] if j.get("eta") is not None else [])
    return f"{j['name']} ({', '.join(bits)})"


def drain(on: bool, now: bool = False) -> dict:
    """For deploy.sh, before it restarts the server: with on, take no new work (areas' climbs, jobs people asked for,
    beta, a video's frames, and machines' jobs), while what's in progress goes on; with now too, a stop doesn't wait
    for jobs to finish either (stop_when_idle()). Off, carry on as before. A restart clears it anyway."""
    global _draining
    _draining = {"since": (_draining or {}).get("since") or time.time(), "now": now} if on else None
    MACHINES.paused = "the server is about to restart, for a new version" if on else None
    if not on:
        _beta_wake.set()
        _frames_wake.set()
    return deploy_state()


def _left(pid: str, progress: dict | None, keys: set[str] | None = None) -> float | None:
    """Seconds left of a project's job (of only the steps with these keys, if given), as its progress bar says
    (with_typicals(), steptimes.job_left()), or None when there's no telling."""
    if not progress:
        return None
    steps = [st for st in with_typicals(progress, None, pid).get("steps", []) if keys is None or st["key"] in keys]
    return steptimes_mod.job_left({"steps": steps}) if steps else None


MACHINE_TASKS = {"reconstruct": "meshing", "beta": "finding beta", "scale": "scale",     # as deploy_state() tells them
                 "filter": "filtering photos"}
KINDS = {"reconstruct": "meshing"}     # deploy_state()'s kinds of work, from task or job types: else as they are


def deploy_state() -> dict:
    """What a restart would cost now, for deploy.sh (cli.py deploy-check): each piece of work in progress, whether a
    restart loses much of it ("costly"), how long it's been going, and about how long it has left (eta, seconds, None
    when there's no telling yet). Costly: jobs on machines, which end with a restart and start over; jobs people asked
    for, which it interrupts; a climb meshed on this server, which starts its step again; beta found here. Not costly:
    an area's climb being harvested (its downloads are kept, and it carries on), and a volunteer's upload (it carries
    on from what came). A job here that's handed its work to a machine is told as the machine's job: costly once a
    machine has taken it, and not while it waits for one, as nothing's done yet (and while draining, none takes it).
    Also whether it's draining (drain()), and "eta", the longest of the costly ones'. Each item's "kind" is the sort of
    work (meshing, harvest, size, ...), for cli.py to group and colour by, and "project_id" the project it works on, if
    any, so that work for the same project (a climb's boulder meshing on a machine) can be told under the job doing it."""
    items, now = [], time.time()
    mesh_keys = {k[0] for k in pipeline.steps()}
    board = MACHINES.running()
    asked = jobs_state(board)["running"]
    for_job = {j["machine_job"]["id"]: j for j in asked if j["machine_job"]}    # a machine's job -> the job here waiting for it
    beta_name = beta_job = None
    if _beta_now:
        beta_name = (q("SELECT name FROM meshes WHERE id=?", _beta_now["mid"], one=True) or {}).get("name", _beta_now["mid"])
        beta_job = next((j["id"] for j in board if j["owner"] == _beta_now.get("thread")), None)
    for j in board:
        elapsed = now - (j["taken"] or j["created"])
        typical = MACHINES.typical(j["task"])
        task = MACHINE_TASKS.get(j["task"], j["task"])
        mine = for_job.get(j["id"])
        project = q("SELECT name FROM projects WHERE id=?", j["label"], one=True) if j["task"] == "reconstruct" else None
        lost = "ends, and starts again on a machine after" if j["state"] == "running" else "queued again after"
        eta = None
        if j["state"] == "running":     # from how far its steps have got (the machine tells its project), else as usual
            p = q("SELECT progress FROM projects WHERE id=?", j["label"], one=True) if project else None
            eta = _left(j["label"], json.loads(p["progress"]), mesh_keys) if p and p["progress"] else None
            if eta is None and typical and typical > elapsed:
                eta = typical - elapsed     # past the usual time, there's no telling
        if mine and mine["type"] != "scale":
            lost = "interrupted: runs again when asked again"
        items.append({"what": f"{task} on {j['machine']}" if j["state"] == "running" else f"{task}, waiting for a machine",
                      "name": mine["name"] if mine else beta_name if j["id"] == beta_job else project["name"] if project else j["label"],
                      "kind": KINDS.get(j["task"], j["task"]), "project_id": j["label"] if project else (mine or {}).get("project_id"),
                      "costly": j["state"] == "running", "elapsed": round(elapsed),
                      "eta": round(eta) if eta is not None else None, "lost": lost})
    for r in q("SELECT c.name area, i.name, i.project_id, i.updated, i.redo, i.boulder FROM campaign_items i "
               "JOIN campaigns c ON c.id=i.campaign_id WHERE i.status='running'"):
        # meshing its boulder (run_redo()) goes on in the boulder's project, not the climb's, which says nothing meanwhile
        bp = boulder_project(r["boulder"]) if r["boulder"] and (r["redo"] or "").split("+")[0] in ("boulder", "pool") else None
        ppid = bp["id"] if bp else r["project_id"]
        p = q("SELECT progress FROM projects WHERE id=?", ppid, one=True) if ppid else None
        prog = json.loads(p["progress"]) if p and p["progress"] else None
        step = next((s for s in (prog or {}).get("steps", []) if s.get("state") == "running"), None)
        meshing_here = bool(step and step["key"] in mesh_keys and _local_meshing and not _executor)
        items.append({"what": f"{r['area']}: " + ("its boulder: " if bp else "") + (step["label"].lower() if step else "starting"),
                      "name": r["name"], "kind": "meshing" if meshing_here else "area", "project_id": ppid, "costly": meshing_here, "elapsed": round(now - (r["updated"] or now)),
                      "eta": (lambda t: round(t) if t is not None else None)(_left(ppid, prog, mesh_keys if meshing_here else None) if ppid else None),
                      "lost": "starts its step again after" if meshing_here else "pauses, and carries on after"})
    for j in asked:
        if j["machine_job"]:
            continue    # told as its machine's job, above
        sizing = j["type"] == "scale"
        items.append({"what": "scale here" if sizing else f"{j['type']}, asked for", "name": j["name"], "costly": True,
                      "kind": KINDS.get(j["type"], j["type"]), "project_id": j.get("project_id"),
                      "elapsed": round(now - j.get("started", now)), "eta": j.get("eta"),
                      "lost": "starts again after" if sizing else "interrupted: runs again when asked again"})
    if _beta_now and not beta_job:
        items.append({"what": "finding beta here", "name": beta_name, "costly": True, "kind": "beta", "project_id": None,
                      "elapsed": round(now - _beta_now["started"]),
                      "eta": None, "lost": "starts again after"})
    for j in jobs_state(board)["queued"]:     # nothing done yet: a project's is queued again as the server starts
        items.append({"what": f"{j['type']}, queued", "name": j["name"], "costly": False, "elapsed": None, "eta": None,
                      "kind": KINDS.get(j["type"], j["type"]), "project_id": j.get("project_id"),
                      "lost": "queued again after" if j.get("project_id") or j["type"] == "scale" else
                      f"lost: {AGAIN[j['type']]} again" if j["type"] in AGAIN else "lost: ask for it again"})
    try:
        for t in VOLUNTEERS.q("SELECT t.id, t.video_id, t.size, v.name FROM tasks t LEFT JOIN volunteers v ON v.id=t.volunteer "
                              "WHERE t.state='assigned' AND t.kind='video'"):
            got = (VOLUNTEERS.files / f"{t['id']}.part").stat().st_size if (VOLUNTEERS.files / f"{t['id']}.part").exists() else 0
            if got:
                items.append({"what": f"a video coming from {t['name'] or 'a volunteer'}", "name": t["video_id"], "costly": False,
                              "kind": "video", "project_id": None,
                              "elapsed": None, "eta": None, "lost": "pauses: the volunteer carries on from what came",
                              "progress": round(100 * got / t["size"]) if t["size"] else None})
    except Exception:  # only the volunteers' part is lost
        pass
    costly = [i for i in items if i["costly"]]
    etas = [i["eta"] for i in costly if i["eta"] is not None]
    return {"draining": bool(_draining), "items": items, "costly": len(costly),
            "eta": round(max(etas)) if etas and len(etas) == len(costly) else None}


def stop_when_idle(server: ThreadingHTTPServer) -> None:
    """Stop the server (on SIGTERM) once the jobs people started outside areas have finished, as a restart
    interrupts them. Meanwhile it takes no more work, and serves the site as before. It waits at most
    STOP_WAIT, and only for jobs that look like finishing by then. Areas' climbs aren't waited for, as a
    restart only pauses them. A second SIGTERM stops it at once."""
    global _stopping
    say = lambda s: print(f"stopping: {s}", flush=True)  # noqa: E731
    if _stopping or (_draining or {}).get("now"):
        say("now, as asked again" if _stopping else "now, as deploy.sh asked")
        mark_stopped()
        return server.shutdown()
    _stopping = {"since": time.time(), "until": time.time() + STOP_WAIT, "waiting_for": []}
    passed, said = set(), 0.0     # jobs not waited for, as they'd take too long
    ident = lambda j: (j["type"], j.get("project_id") or j.get("mesh_id"))  # noqa: E731
    while True:
        state = jobs_state()
        for j in state["running"]:
            if j["eta"] is not None and time.time() + j["eta"] > _stopping["until"] and ident(j) not in passed:
                passed.add(ident(j))
                say(f"not waiting for {job_label(j)}, as it won't finish in time: after the restart, {AGAIN[j['type']]}")
            elif j["machine_job"] and j["machine_job"]["state"] == "queued" and ident(j) not in passed:
                passed.add(ident(j))    # nothing's done yet, and no machine takes it while deploy.sh drains
                say(f"not waiting for {job_label(j)}, as no machine has taken it yet: after the restart, {AGAIN[j['type']]}")
        waiting = [j for j in state["running"] if ident(j) not in passed]
        _stopping["waiting_for"] = [job_label(j) for j in waiting]
        left = _stopping["until"] - time.time()
        if not waiting or left <= 0:
            break
        if time.time() - said >= 60:
            say(f"{'still ' if said else ''}waiting up to {minutes(left)} for jobs started outside areas to finish, as a restart "
                f"would interrupt them: {'; '.join(_stopping['waiting_for'])}. Taking no new work meanwhile. SIGTERM again stops now.")
            said = time.time()
        time.sleep(2)
    for j in waiting:
        say(f"{job_label(j)} didn't finish in time: after the restart, {AGAIN[j['type']]}")
    if state["queued"]:
        say(f"{len(state['queued'])} queued job(s) won't start before the restart: " +
            "; ".join(f"{j['name']} ({j['type']}): {AGAIN[j['type']]}" for j in state["queued"]))
    if state["area_climbs"]:
        say("areas' climbs pause, and carry on after the restart: " + "; ".join(f"{c['name']} ({c['area']})" for c in state["area_climbs"]))
    if state["beta"]["running"] or state["beta"]["queued"]:
        say(f"finding beta pauses, and carries on after the restart: {state['beta']['running'] or 'nothing'} now, "
            f"{state['beta']['queued']} queued")
    say("now" if state["running"] or state["queued"] or state["area_climbs"] else
        "now, as what it waited for has finished" if said else "now: nothing was running")
    mark_stopped()
    server.shutdown()


def server_id() -> str:
    """This server's own name for the cloud machines it starts, so that it ends only its own."""
    path = DATA / "compute" / "server_id"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(8))
    return path.read_text().strip()


def compute_settings() -> dict:
    """The settings, with the image remote machines run filled in as this build's own for them (MACHINES_IMAGE) where
    the admin page leaves it empty: then they run the commit the server runs, whichever was shipped last."""
    s = settings().all()
    if not s["compute.image"] and MACHINES_IMAGE:
        s["compute.image"] = MACHINES_IMAGE
    return s


def configure_workers() -> None:
    """Start or retire workers, and make the pool of remote machines, to match the settings."""
    global _pool, _executor, _pool_key, _local_meshing
    s = compute_settings()
    with _workers_lock:
        key = (s["compute.provider"], s["compute.remote_workers"], s["compute.idle_minutes"], tuple(s["compute.ssh_hosts"]),
               s["compute.image"], s["compute.command"], *(v for k, v in s.items() if k.startswith("compute.aws.")),
               tuple(sorted((settings().secrets_status()["credentials"]).items())))
        if key != _pool_key:
            if _pool:
                _pool.close()
                _old_pools.append(_pool)
            _pool = _executor = None
            provider = None
            if s["compute.provider"] == "pull":     # machines that connect here: no machines to start, only jobs to hand out
                _executor = MACHINES if s["compute.remote_workers"] else None
            else:
                try:
                    provider = compute.provider_for(s, settings().secret, DATA / "compute", server_id())
                except Exception as e:
                    print(f"compute: can't use {s['compute.provider']}: {e}", flush=True)
            if provider and s["compute.remote_workers"]:
                _pool = compute.Pool(provider, s["compute.remote_workers"], s["compute.idle_minutes"])
                _executor = compute.RemoteExecutor(_pool, s["compute.image"], s["compute.command"], jobctl)
            _pool_key = key
        _local_meshing = s["compute.local_workers"] > 0
        want = {False: s["compute.local_workers"], True: s["compute.remote_workers"] if _executor else 0}
        for remote in (False, True):
            mine = [w for w in _workers if w.remote == remote and not w.retired]
            for w in mine[want[remote]:]:
                if w is not _first:
                    w.retired = True
            for i in range(len(mine), want[remote]):
                w = Worker(f"{'remote' if remote else 'this server'} {i + 1}", remote=remote)
                _workers.append(w)
                threading.Thread(target=worker, args=(w,), daemon=True).start()
        _workers[:] = [w for w in _workers if not w.retired or w.campaign or w.job]


def reap_machines() -> None:
    """End idle remote machines, every minute; at startup, any of this server's left from before."""
    if settings().get("compute.provider") == "aws":
        try:
            prov = compute.provider_for(compute_settings(), settings().secret, DATA / "compute", server_id())
            for iid in prov.leftovers():
                print(f"compute: ending {iid}, left running from before a restart", flush=True)
                prov.stop(compute.Node(host="", user="", id=iid))
        except Exception as e:
            print(f"compute: couldn't check for machines left running: {e}", flush=True)
    while True:
        time.sleep(60)
        try:
            MACHINES.sweep()    # machines that stopped asking: offline, in their record
            MACHINES.hourly()   # and once an hour, each one's time working, idle, held back or offline
        except Exception as e:
            print(f"machines: sweep failed ({type(e).__name__}: {e})", flush=True)
        for board in (MACHINES, VOLUNTEERS):    # who joined or left (presence.py), told on your phone
            try:
                board.presence()
            except Exception as e:
                print(f"presence: sweep failed ({type(e).__name__}: {e})", flush=True)
        for pool in [_pool, *_old_pools]:
            if pool:
                pool.reap()
        _old_pools[:] = [p for p in _old_pools if p.nodes]


def slot_doing(w: Worker) -> str | None:
    """What a worker is doing, for the admin page: harvesting a climb here, meshing it (here, or on a machine), a job
    someone asked for, or waiting for a machine; None if nothing."""
    if w.doing:
        row = q("SELECT p.status FROM campaign_items ci JOIN projects p ON p.id=ci.project_id WHERE ci.campaign_id=? AND "
                "ci.status='running' AND ci.name=? LIMIT 1", w.campaign, w.doing, one=True) if w.campaign else None
        status = (row or {}).get("status")
        if status == "harvesting":
            return f"harvesting {w.doing} here"
        if status in ("queued", "waiting"):
            return f"starting {w.doing}"
        return f"meshing {w.doing}" + (" on a machine" if w.remote else " here")
    if w.job:
        return f"{w.job['job'].get('type', 'a job')} asked for"
    return "waiting for a machine" if time.time() < w.wait_until else None


def compute_state() -> dict:
    return {"workers": [{"name": w.name, "remote": w.remote, "error": w.error, "busy": slot_doing(w)}
                        for w in _workers if not w.retired],
            "pool": _pool.status() if _pool else None, "local_meshing": _local_meshing,
            "machines": {**MACHINES.state(), "served": bool(WORKER_PORT), "url": WORKER_URL}}


# ----------------------------------------------------------------- machines that connect here for work
WORKER_URL = os.environ.get("WORKER_URL") or VOLUNTEER_URL     # the address they reach this at, for the admin page's command
def machine_rules() -> dict:
    """The admin page's settings for machines that register themselves (pullwork.RULES)."""
    s = settings().all()
    return {"provisional_work": s["machines.provisional_work"], "registrations_per_address": s["registration.per_address_day"],
            "max_waiting": s["registration.max_waiting"]}


# ------------------------------------------------------------------ telling your phone the server is back, and of downloads
STOPPED_AT = DATA / "stopped-at"    # when it last stopped as asked (SIGTERM), for saying how long it was down
SCRIPT_DOWNLOADS = DATA / "script-downloads.jsonl"  # each time someone (not a running copy) fetched a script, and where from
DOWNLOAD_GAP = 600      # seconds: at most one notification of downloads in this many; those meanwhile together after
DOWNLOAD_AGAIN = 86400  # the same address fetching the same script again within this many isn't told of again
BOTS = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|monitor|uptime|headless", re.I)
_fetched: dict[tuple[str, str], float] = {}     # (script, address) -> when it was last told of


# ------------------------------------------------------------------ when a job hangs: what each thread is doing
DIAGNOSTICS = DATA / "diagnostics"     # thread dumps (diagnostics.dump_threads()), when a job stalls or on SIGUSR1
STALL_MINUTES = 20      # a job whose progress hasn't moved this long while this server is busy has its threads written down
STALL_BUSY = 0.5        # busy: this server's own threads used at least this many cores, on average, meanwhile
STALL_GAP = 1800        # at most one such dump in this many seconds, however many jobs stall
_stalls: dict[str, tuple[str, float, float]] = {}     # project -> (its progress as last seen, since when, CPU seconds then)
_stall_dumped: set[str] = set()                       # projects written down for the stall they're in
_last_stall_dump = 0.0


def own_cpu() -> float:
    """CPU seconds this server's own threads have used (not the tools it runs)."""
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def dump_threads_now(why: str) -> Path | None:
    """Write down what each of this server's threads is doing (diagnostics.dump_threads()), saying where in its log."""
    try:
        path = diagnostics.dump_threads(DIAGNOSTICS, why)
        print(f"diagnostics: {why}: what each thread is doing is in data/diagnostics/{path.name}", flush=True)
        return path
    except Exception as e:  # only costs the dump
        print(f"diagnostics: couldn't write down the threads ({type(e).__name__}: {e})", flush=True)
        return None


def stall_check(now: float | None = None, cpu: float | None = None) -> list[str]:
    """Once a minute (stall_watch()): a job here whose progress hasn't moved for STALL_MINUTES while this server's own
    threads stayed busy (STALL_BUSY cores) looks hung, as Gunsmoke Wall's filter did for 5 hours on Nov 12, 2026 with two
    threads spinning in the kernel. Its threads are written down once for that stall (not one handed to a machine, which
    this server only waits for), its log says where, and your phone is told. Returns the projects written down for."""
    global _last_stall_dump
    now, cpu = time.time() if now is None else now, own_cpu() if cpu is None else cpu
    on_machines = {j["label"] for j in MACHINES.running()}
    dumped, seen = [], set()
    for r in q("SELECT id, name, progress FROM projects WHERE status IN ('running','harvesting')"):
        seen.add(r["id"])
        was = _stalls.get(r["id"])
        if not was or was[0] != (r["progress"] or "") or r["id"] in on_machines:
            _stalls[r["id"]] = (r["progress"] or "", now, cpu)
            _stall_dumped.discard(r["id"])
            continue
        _, since, cpu_then = was
        took = now - since
        if (took < STALL_MINUTES * 60 or r["id"] in _stall_dumped or (cpu - cpu_then) / took < STALL_BUSY
                or now - _last_stall_dump < STALL_GAP):
            continue
        _stall_dumped.add(r["id"])
        _last_stall_dump = now
        why = (f"{r['name']} ({r['id']}): no progress for {took / 60:.0f} min, while this server used "
               f"{(cpu - cpu_then) / took:.1f} cores")
        path = dump_threads_now(why)
        if path:
            append_log(r["id"], f"-- no progress for {took / 60:.0f} min while this server is busy: what each of its threads is "
                                f"doing is in data/diagnostics/{path.name}")
            threading.Thread(target=NOTIFIER.send, args=(f"BoulderMesh: {r['name']} looks stuck",
                             f"No progress for {took / 60:.0f} min, with the server busy. What its threads are doing is in "
                             f"data/diagnostics/{path.name}."), kwargs={"priority": 3, "tags": ["warning"]}, daemon=True).start()
            dumped.append(r["id"])
    for k in [k for k in _stalls if k not in seen]:
        _stalls.pop(k, None)
        _stall_dumped.discard(k)
    return dumped


def stall_watch() -> None:
    while True:
        time.sleep(60)
        try:
            stall_check()
        except Exception as e:  # never let the thread die
            print(f"stall watch error: {e!r}", flush=True)


def mark_stopped() -> None:
    try:
        STOPPED_AT.write_text(f"{time.time()}\n")
    except OSError:
        pass


def back_up_note(began: float) -> tuple[str, str]:
    """What tells your phone the server is back after a restart: its build, how long it took to start and, if it was
    stopped as asked, how long it was down; the areas carrying on."""
    def took(s: float) -> str:
        return f"{s:.0f} s" if s < 90 else f"{s / 60:.0f} min" if s < 5400 else f"{s / 3600:.1f} h"
    try:
        down = began - float(STOPPED_AT.read_text().strip())
        STOPPED_AT.unlink(missing_ok=True)
    except (OSError, ValueError):
        down = None
    areas = [r["name"] for r in q("SELECT name FROM campaigns WHERE status='running' ORDER BY priority")]
    text = (f"Running {VERSION or 'a build not recorded'}, up in {took(time.time() - began)}"
            + (f", after {took(down)} down" if down is not None and down >= 0 else ", after stopping without saying so (a crash, or a reboot)")
            + "." + (f" Carrying on with {', '.join(areas[:4])}{f' and {len(areas) - 4} more' if len(areas) > 4 else ''}." if areas else ""))
    return "BoulderMesh is back up", text


def tell_back_up(began: float, port: int) -> None:
    """Once the site answers after a restart (notify.restarts), tell your phone."""
    import urllib.request
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5).read()
            break
        except Exception:
            time.sleep(1)
    else:
        return
    if settings().get("notify.restarts"):
        NOTIFIER.send(*back_up_note(began), priority=2, tags=["arrows_counterclockwise"])


def script_downloaded(kind: str):
    """What a script being fetched does (volunteers.script_fetch()'s {ip, ua, headers}): kept in SCRIPT_DOWNLOADS, with
    roughly where from (notify.visitor_place(), from Cloudflare's headers), and your phone told (notify.downloads) at
    most once a DOWNLOAD_GAP, once a day for one address, with a map. Not a running copy updating itself (its
    User-Agent is the script's), nor a crawler."""
    def downloaded(info: dict) -> None:
        ua = str(info.get("ua") or "")
        if ua.startswith(("bouldermesh-volunteer", "bouldermesh-worker")) or BOTS.search(ua):
            return
        where = notify_mod.visitor_place(info.get("headers") or {})
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "script": kind, "ip": info.get("ip"), "ua": ua[:300],
                "place": where["place"], "map": where["map"]}
        with _client_lock:
            with open(SCRIPT_DOWNLOADS, "a") as f:
                f.write(json.dumps(line) + "\n")
            now, key = time.time(), (kind, str(info.get("ip")))
            if now - _fetched.get(key, 0) < DOWNLOAD_AGAIN:
                return
            _fetched[key] = now
        print(f"script downloaded: the {kind} script, from {where['place'] or 'somewhere'} ({notify_mod.browser_of(ua)})", flush=True)
        if settings().get("notify.downloads"):
            title = f"Someone downloaded the {kind} script"
            place = where["place"] or "somewhere Cloudflare didn't say"
            text = f"From {place}, with {notify_mod.browser_of(ua)}."
            DOWNLOADS.add(title, text, [{"action": "view", "label": "Map", "url": where["map"]}] if where["map"] else None)
    return downloaded


# ------------------------------------------------------------------ the leaderboard: who fetched videos, whose computers meshed
NAMES_SHOWN_SINCE = 1794441600.0    # 2026-11-12T00:00Z: from scripts version 9, registering says the name is shown on it
GPU_STEPS = re.compile(r"matching|densify|VGGT|points from those placements|people", re.I)   # tools that use a GPU, where there is one
# GPUs' single-precision (FP32) throughput, TFLOPS, from their makers' specifications: a GPU's hours count as this share of an
# RTX 3090's (35.6) on the leaderboard. The longest name in a GPU's own name is it; one not here counts as a 3090, marked so
GPU_TFLOPS = {"rtx 5090": 104.8, "rtx 5080": 56.3, "rtx 5070 ti": 43.9, "rtx 5070": 30.9,
              "rtx 4090": 82.6, "rtx 4080 super": 52.2, "rtx 4080": 48.7, "rtx 4070 ti super": 44.1, "rtx 4070 ti": 40.1,
              "rtx 4070 super": 35.5, "rtx 4070": 29.1, "rtx 4060 ti": 22.1, "rtx 4060": 15.1,
              "rtx 3090 ti": 40.0, "rtx 3090": 35.6, "rtx 3080 ti": 34.1, "rtx 3080": 29.8, "rtx 3070 ti": 21.7, "rtx 3070": 20.3,
              "rtx 3060 ti": 16.2, "rtx 3060": 12.7, "rtx 3050": 9.1, "rtx 2080 ti": 13.4, "rtx 2080 super": 11.2, "rtx 2080": 10.1,
              "rtx 2070 super": 9.1, "rtx 2070": 7.5, "rtx 2060 super": 7.2, "rtx 2060": 6.5, "titan rtx": 16.3,
              "gtx 1080 ti": 11.3, "gtx 1080": 8.9, "gtx 1070 ti": 8.2, "gtx 1070": 6.5, "gtx 1660 ti": 5.4, "gtx 1660 super": 5.0,
              "gtx 1660": 5.0, "gtx 1650": 3.0, "a100": 19.5, "h100": 51.2, "v100": 15.7, "t4": 8.1, "a10": 31.2, "l4": 30.3,
              "l40s": 91.6, "rtx a6000": 38.7, "rtx a5000": 27.8, "rtx a4000": 19.2, "rtx 6000 ada": 91.1}
GPU_BASE = 35.6     # an RTX 3090's


def gpu_factor(name: str | None) -> float | None:
    """How many RTX 3090 hours an hour of this GPU counts as (GPU_TFLOPS), or None for one not known. A laptop's
    version of a card counts as 60% of its desktop one, as they're held to lower power."""
    low = re.sub(r"\s+", " ", (name or "").lower())
    keys = [k for k in GPU_TFLOPS if re.search(r"(?<![\w])" + re.escape(k) + r"(?![\w])", low)]
    if not keys:
        return None
    return GPU_TFLOPS[max(keys, key=len)] / GPU_BASE * (0.6 if "laptop" in low or "mobile" in low or "max-q" in low else 1.0)


def shown_name(row: dict, kind: str, n: int, mode: str) -> str:
    """A contributor's name on the public leaderboard (leaderboard.names): theirs where it may be shown, else anonymous."""
    if row.get("name") and (mode == "all" or (mode == "new" and (row.get("created") or 0) >= NAMES_SHOWN_SINCE)):
        return row["name"]
    return f"anonymous {kind} {n}"


def machine_compute(measured_from: dict[str, float] | None = None) -> tuple[dict[str, dict], float | None]:
    """What each machine has made, by the meshes' timings (timings.json, which say where each was made): {its name: {meshes,
    cpu_hours (its tools' processor time, over all cores), gpu_hours (its GPU steps' time, where it had one: for meshes
    made before its script measured its GPU use, measured_from {name: when})}}, and when the first of them was made."""
    out: dict[str, dict] = {}
    first = None
    measured_from = measured_from or {}
    for d in MESHES.iterdir() if MESHES.exists() else []:
        try:
            t = json.loads((d / timings_mod.FILE).read_text())
        except (OSError, ValueError):
            continue
        if not t.get("remote") or not t.get("where"):
            continue
        c = out.setdefault(t["where"], {"meshes": 0, "cpu_hours": 0.0, "gpu_hours": 0.0})
        c["meshes"] += 1
        c["cpu_hours"] += (t.get("cpu_seconds") or 0) / 3600
        if t.get("gpu") and (t.get("started") or 0) < measured_from.get(t["where"], float("inf")):
            c["gpu_hours"] += sum(x.get("seconds") or 0 for x in t.get("tools") or [] if GPU_STEPS.search(x.get("step") or "")) / 3600
        if t.get("started"):
            first = t["started"] if first is None else min(first, t["started"])
    return out, first


def leaderboard() -> dict:
    """The public leaderboard (/volunteer/leaderboard): volunteers by what they fetched (Broker.contributions()), and
    machines by meshes made (from the meshes' timings), beta and sizes found (machine_jobs), with their processor and GPU
    time in meshing. Names as leaderboard.names says."""
    mode = settings().get("leaderboard.names")
    vols = [{**{k: v[k] for k in ("videos", "gb", "searches", "details")}, "revoked": bool(v["revoked"]),
             "name": shown_name(v, "volunteer", i + 1, mode)} for i, v in enumerate(VOLUNTEERS.contributions())]
    machines = MACHINES.q("SELECT id, name, created, revoked, gpu_name FROM machines ORDER BY created")
    measured: dict[str, dict] = {}      # each machine's jobs' measured GPU use: from when, and in 3090 hours
    for r in MACHINES.q("SELECT machine_id, gpu_name, MIN(taken) AS since, SUM(gpu_busy) AS busy FROM machine_jobs "
                        "WHERE gpu_busy IS NOT NULL GROUP BY machine_id, gpu_name"):
        k = gpu_factor(r["gpu_name"])
        g = measured.setdefault(r["machine_id"], {"since": r["since"], "hours": 0.0, "known": True})
        g["since"] = min(g["since"] or r["since"], r["since"] or g["since"])
        g["hours"] += (r["busy"] or 0) / 3600 * (k if k is not None else 1.0)
        g["known"] = g["known"] and k is not None
    made, since = machine_compute({m["name"]: measured[m["id"]]["since"] for m in machines if m["id"] in measured and measured[m["id"]]["since"]})
    jobs: dict[str, dict] = {}
    for r in MACHINES.q("SELECT machine_id, task, COUNT(*) AS n FROM machine_jobs WHERE outcome='done' GROUP BY machine_id, task"):
        jobs.setdefault(r["machine_id"], {})[r["task"]] = r["n"]
    jobs_since = (MACHINES.q("SELECT MIN(created) AS t FROM machine_jobs", one=True) or {}).get("t")
    workers = []
    for m in machines:
        c, j, g = made.get(m["name"], {}), jobs.get(m["id"], {}), measured.get(m["id"])
        meshes = max(c.get("meshes", 0), j.get("reconstruct", 0))
        if not (meshes or j.get("beta") or j.get("scale")):
            continue
        k = gpu_factor(m["gpu_name"])
        estimated = c.get("gpu_hours", 0.0) * (k if k is not None else 1.0)      # from the steps, before measuring
        workers.append({"m": m, "meshes": meshes, "beta": j.get("beta", 0), "sizes": j.get("scale", 0), "revoked": bool(m["revoked"]),
                        "cpu_hours": round(c.get("cpu_hours", 0.0), 2), "gpu_hours": round(estimated + (g["hours"] if g else 0.0), 2),
                        "gpu": m["gpu_name"], "gpu_rough": bool(estimated) or k is None or bool(g and not g["known"])})
    workers.sort(key=lambda w: (-w["meshes"], -w["gpu_hours"] - w["cpu_hours"], -w["beta"]))     # the GPU in 3090 hours
    for i, w in enumerate(workers):
        w["name"] = shown_name(w.pop("m"), "computer", i + 1, mode)
    return {"volunteers": vols, "workers": workers, "since": since, "jobs_since": jobs_since, "names": mode}


DOWNLOADS = notify_mod.Paced(lambda title, text, actions: threading.Thread(
    target=NOTIFIER.send, args=(title, text, actions, 2, ["inbox_tray"]), daemon=True).start(), DOWNLOAD_GAP)


def came_went(kind: str):
    """What tells your phone a machine (a worker) or a volunteer (a downloader) joined or left (presence.py), in the
    background."""
    which = {"machine": "Worker", "volunteer": "Downloader"}[kind]
    def tell(row: dict, state: str, why: str) -> None:
        name = row.get("name") or f"#{row['id']}"
        if state == "here":
            title = f"{which} {name} joined"
            text = f"It's {'lending its computer to mesh' if kind == 'machine' else 'fetching YouTube videos'}" \
                + (f" ({row['client']})" if row.get("client") else "") + "."
        else:
            title, text = f"{which} {name} left", f"{why[:1].upper()}{why[1:]}."
        threading.Thread(target=NOTIFIER.send, args=(title, text), kwargs={"priority": 2, "tags": ["wave"]}, daemon=True).start()
    return tell


MACHINES = pullwork.Board(DATA / "compute" / "machines.sqlite", DATA / "compute" / "jobs", lambda: compute_settings()["compute.image"],
                          jobctl, machine_rules, lambda m: NOTIFIER.registered("machine", m, f" ({m['client'] or 'a worker script'})"),
                          came_went=came_went("machine"), idle_why=lambda: machines_idle_why(), downloaded=script_downloaded("worker"),
                          cpu_image=lambda image: cpu_image_for(image))


def cpu_image_for(image: str) -> str:
    """The image a machine without a GPU runs a job made for image in: this build's slim one for them (WORKER_CPU_IMAGE,
    which only filters, for x86-64 and ARM64) for the machines' image the build named; else, in deploy/build.sh's names,
    the server's own (<commit> for <commit>-gpu, which only x86-64 runs); else the same."""
    if WORKER_CPU_IMAGE and image == MACHINES_IMAGE:
        return WORKER_CPU_IMAGE
    return image.removesuffix("-gpu")


def machine_command(m: dict, result: str) -> dict:
    """What the admin page shows for a machine: the command to run on it, with its token, and where to get the script."""
    url = WORKER_URL or "https://<this site>"
    image = compute_settings()["compute.image"]
    return {**m, "result": result, "command": f"python3 bouldermesh_worker.py --server {url} --token {m['token'] or 'bmw_...'}",
            "download": f"{url}/worker/bouldermesh_worker.py", "image_prefix": image.rsplit(":", 1)[0] + ":" if image else None}


def admin_machine_add(body: dict) -> dict:
    m = MACHINES.add(body.get("name") or "")
    return machine_command(m, f"added {m['name']}: run the command below on it, which has its token (keep it private)")


def admin_machine_show(body: dict) -> dict:
    m = MACHINES.token(str(body.get("id") or ""))
    if not m:
        raise ValueError("no such machine")
    return machine_command(m, f"{m['name']}: run the command below on it, which has its token (keep it private)" if m["token"] else
                           f"{m['name']}: its token wasn't kept (it was added before tokens were, or it's revoked): revoke it and "
                           "add it again for a command with one")


def admin_machine_revoke(body: dict) -> dict:
    MACHINES.revoke(str(body.get("id") or ""))
    return {"result": "revoked: its token no longer works, and any climb it was meshing waits for another machine"}


def admin_machine_approve(body: dict) -> dict:
    m = MACHINES.approve(str(body.get("id") or ""))
    if not m:
        raise ValueError("no such machine waiting")
    return {"result": f"approved {m['name']}: it gets climbs to mesh, and those it made while waiting take its meshes"}


def admin_machine_deny(body: dict) -> dict:
    MACHINES.revoke(str(body.get("id") or ""), "denied")
    return {"result": "turned down: its token no longer works, and what it made while waiting is dropped"}


# ----------------------------------------------------------------- errors in visitors' browsers
CLIENT_ERRORS = DATA / "client-errors.jsonl"   # one report a line; the one before it, client-errors.jsonl.1, once it's full
CLIENT_ERRORS_BYTES = 2 * 2**20                # how big the file gets before that
CLIENT_REPORT_MAX = 8192                       # the largest report taken
CLIENT_REPORTS = {"overlap": (30, 600), "error": (20, 600), "view": (120, 600), "rating": (120, 3600), "feedback": (5, 3600)}  # at most this many of each from one address in this many seconds
MESH_VIEWS = DATA / "mesh-views.jsonl"       # each mesh shown in a visitor's browser, and how it was asked for
_client_reports: dict[tuple[str, str], list[float]] = {}
_client_lock = threading.Lock()


def _client_allowed(kind: str, sender: str) -> bool:
    """Count a report of a kind from a visitor's address, unless it has sent too many lately (CLIENT_REPORTS). Call with
    _client_lock held."""
    now = time.time()
    most, within = CLIENT_REPORTS[kind]
    recent = [t for t in _client_reports.get((kind, sender), []) if now - t < within]
    if len(recent) >= most:
        _client_reports[(kind, sender)] = recent
        return False
    _client_reports[(kind, sender)] = recent + [now]
    for k in [k for k, ts in _client_reports.items() if not ts or now - ts[-1] > max(w for _, w in CLIENT_REPORTS.values())]:
        del _client_reports[k]
    return True


def _client_report(kind: str, sender: str, path: Path, line: dict) -> bool:
    """Append a report from a visitor's browser to path, unless that address has sent too many of its kind lately
    (CLIENT_REPORTS), starting a new file once it's over CLIENT_ERRORS_BYTES, the old one kept as <name>.1."""
    now = time.time()
    with _client_lock:
        if not _client_allowed(kind, sender):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > CLIENT_ERRORS_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with open(path, "a") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "version": VERSION,
                                **{k: v for k, v in line.items() if v is not None}}) + "\n")
    return True


def record_client_error(report: dict, sender: str, user_agent: str, editor: bool) -> bool:
    """Keep what a visitor's browser said went wrong (a mesh that wouldn't load, an error in the page), for looking at
    later: in CLIENT_ERRORS and the log. Who sent it isn't kept, only their browser. Returns False if that address has
    sent too many lately."""
    text = lambda k, n: str(report.get(k) or "")[:n] or None  # noqa: E731
    line = {"kind": text("kind", 40), "message": text("message", 1000), "mesh": text("mesh", 40), "file": text("file", 200),
            "page": text("page", 300), "source": text("source", 300), "browser": user_agent[:300] or None, "editor": editor}
    if not _client_report("error", sender, CLIENT_ERRORS, line):
        return False
    print(f"browser error ({line['kind']}){' in mesh ' + line['mesh'] if line['mesh'] else ''}: {(line['message'] or '')[:200]}", flush=True)
    return True


NEARBY_OVERLAPS = DATA / "nearby-overlaps.jsonl"   # meshes Nearby wouldn't show, as they'd overlap the one in view


def record_overlap(report: dict, sender: str, editor: bool) -> bool | None:
    """Keep a pair of meshes the viewer's Nearby left out as overlapping (at the same point, or their placed bounds
    mostly in one another), for looking at later: as often the same rock meshed twice. None if either isn't a mesh;
    False if that address has sent too many lately."""
    a, b = str(report.get("mesh") or "")[:40], str(report.get("other") or "")[:40]
    names = {r["id"]: r["name"] for r in q("SELECT id, name FROM meshes WHERE id IN (?, ?)", a, b)}
    if a not in names or b not in names:
        return None
    try:
        share = round(float(report.get("share")), 3) if report.get("share") is not None else None
        dist = round(float(report.get("distance_m")), 2) if report.get("distance_m") is not None else None
    except (TypeError, ValueError):
        share = dist = None
    line = {"mesh": a, "mesh_name": names[a], "other": b, "other_name": names[b], "why": str(report.get("why") or "")[:40] or None,
            "share": share, "distance_m": dist, "editor": editor}
    if not _client_report("overlap", sender, NEARBY_OVERLAPS, line):
        return False
    print(f"nearby: {names[b]} ({b}) overlaps {names[a]} ({a}): {line['why']}, not shown", flush=True)
    return True


# ----------------------------------------------------------------- comments and bug reports from visitors
FEEDBACK = DATA / "feedback.jsonl"      # one a line, as plain text (feedback.py)
FEEDBACK_ALL = (200, 86400)             # at most this many kept from everyone together in this many seconds (CLIENT_REPORTS
                                        # has one address's), so that many addresses can't fill the file or the phone
FEEDBACK_GAP = 60                       # seconds: at most one notification in this many; those meanwhile together after
_feedback_times: list[float] = []
FEEDBACK_NOTES = notify_mod.Paced(lambda title, text, actions: threading.Thread(
    target=NOTIFIER.send, args=(title, text, actions, 3, ["speech_balloon"]), daemon=True).start(), FEEDBACK_GAP)


def record_feedback(report: dict, sender: str, user_agent: str, editor: bool) -> bool | None:
    """Keep a comment or bug report from the page's "?" tab, cleaned (feedback.parse()), in FEEDBACK, and tell the
    admin's phone (notify.feedback), as plain text. Who sent it isn't kept, only their browser and what they gave to be
    reached by, and the mesh that was in view if the viewer's
    Report a problem button opened the form. Returns None for one dropped as a bot's (its hidden field filled in), and False if that address, or
    everyone together, has sent too many lately.

    Raises:
        ValueError: for one that's empty, too long, or not text.
    """
    line = feedback_mod.parse(report)
    if line is None:
        print("a comment with the form's hidden field filled in: dropped", flush=True)
        return None
    found = q("SELECT name FROM meshes WHERE id=?", line["mesh"]) if line["mesh"] else []
    line["mesh"] = line["mesh"] if found else None      # only one there is
    now = time.time()
    with _client_lock:
        _feedback_times[:] = [t for t in _feedback_times if now - t < FEEDBACK_ALL[1]]
        if len(_feedback_times) >= FEEDBACK_ALL[0] or not _client_allowed("feedback", sender):
            return False
        _feedback_times.append(now)
        feedback_mod.keep(FEEDBACK, {**line, "browser": feedback_mod.clean(user_agent[:300]) or None, "editor": editor, "version": VERSION})
    print(f"a visitor's {'bug report' if line['kind'] == 'bug' else 'comment'}: {line['message'][:200]!r}", flush=True)
    if settings().get("notify.feedback"):
        FEEDBACK_NOTES.add(*feedback_mod.note({**line, "mesh_name": f"{feedback_mod.clean(found[0]['name'] or '')[:100]} ({line['mesh']})" if found else None}))
    return True


def record_mesh_view(report: dict, sender: str, editor: bool) -> bool | None:
    """Keep that a visitor's browser showed a mesh, in MESH_VIEWS and the log: which, how it was asked for (via: random, link,
    climb, meshes, map, area, project, beta), how long it took to load, and its note if it had one. Requests for the
    files don't say, as browsers and Cloudflare keep copies. Not who, nor their browser.

    Returns:
        None if there's no such mesh; False if that address has sent too many lately.
    """
    mid = str(report.get("mesh") or "")
    m = q("SELECT name FROM meshes WHERE id=?", mid, one=True) if re.fullmatch(r"[0-9a-f]{6,32}", mid) else None
    if not m:
        return None
    try:
        seconds = round(float(report.get("seconds")), 1)
    except (TypeError, ValueError):
        seconds = None
    via = str(report.get("via") or "")[:20] or None
    line = {"mesh": mid, "name": m["name"], "via": via, "seconds": seconds, "note": str(report.get("note") or "")[:300] or None,
            "editor": editor}
    if not _client_report("view", sender, MESH_VIEWS, line):
        return False
    print(f"viewed mesh {mid} ({m['name']}){' from ' + via if via else ''}" + (f" in {seconds} s" if seconds is not None else ""), flush=True)
    return True


RATINGS = {1: "bad", 2: "ok", 3: "good"}
RATING_COMMENT_MAX = 2000       # characters of a comment on a mesh kept


def mesh_ratings() -> dict[str, dict]:
    """Each rated mesh's ratings: {mesh id: {mean, n}}, of those with a rating (not only a comment)."""
    return {r["mesh_id"]: {"mean": round(r["mean"], 3), "n": r["n"]} for r in
            q("SELECT mesh_id, AVG(rating) AS mean, COUNT(rating) AS n FROM mesh_ratings WHERE rating IS NOT NULL GROUP BY mesh_id")}


def rate_mesh(mid: str, report: dict, sender: str) -> dict | bool | None:
    """A visitor's rating of a mesh (1 bad, 2 ok, 3 good, or none) and comment, as their browser sends them each time
    either changes: one each per browser (an id the page makes and keeps), replaced from wherever it's sent; with
    neither, it goes. The address it came from is kept with it, and limits how often.
    Anyone viewing the site may, as for client reports.

    Returns:
        The mesh's ratings after ({mean, n}, or {"n": 0}); None if there's no such mesh; False if that address has
        sent too many lately.

    Raises:
        ValueError: not a rating.
    """
    m = q("SELECT name FROM meshes WHERE id=?", mid, one=True) if re.fullmatch(r"[0-9a-f]{6,32}", mid) else None
    if not m:
        return None
    browser, rating = str(report.get("browser") or ""), report.get("rating")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", browser):
        raise ValueError("browser: the id this browser keeps")
    if isinstance(rating, bool) or rating not in (None, *RATINGS):
        raise ValueError("rating: 1 (bad), 2 (ok), 3 (good) or null")
    comment = str(report.get("comment") or "").strip()[:RATING_COMMENT_MAX] or None
    with _client_lock:
        if not _client_allowed("rating", sender):
            return False
    now = time.time()
    if rating is None and comment is None:
        q("DELETE FROM mesh_ratings WHERE mesh_id=? AND browser=?", mid, browser, write=True)
    else:
        q("INSERT INTO mesh_ratings(mesh_id, browser, address, rating, comment, created, updated) VALUES(?,?,?,?,?,?,?) "
          "ON CONFLICT(mesh_id, browser) DO UPDATE SET address=excluded.address, rating=excluded.rating, "
          "comment=excluded.comment, updated=excluded.updated", mid, browser, sender, rating, comment, now, now, write=True)
    print(f"rated mesh {mid} ({m['name']}): {RATINGS.get(rating, 'no rating')}{', with a comment' if comment else ''}", flush=True)
    return mesh_ratings().get(mid) or {"n": 0}


# ----------------------------------------------------------------- handler
_csp: dict = {"mtime": None, "value": ""}


def admin_origins() -> list[str]:
    """Where the page may look for the admin page (admin_link()), to tell its Admin button whether this browser reaches it."""
    a = admin_link()
    return [re.match(r"[a-z]+://[^/]+", u)[0] for u in (a["url"], a["local"]) if re.match(r"[a-z]+://[^/]+", u)]


def page_csp() -> str | None:
    """The page's Content-Security-Policy: its own inline scripts by their hashes, libraries from their CDNs, images from
    anywhere (climbs' photos are shown from their sites), and never framed. The splat viewer sorts in a worker made from
    a blob: URL, with WebAssembly, which 'wasm-unsafe-eval' allows (it doesn't allow eval itself). It may fetch data: and blob: URLs, as the 3D
    viewer loads a mesh's buffers embedded in it that way (OpenMVS embeds a second one when a texture is split in two).
    CSP=0 turns it off; CSP=<policy> replaces it."""
    setting = os.environ.get("CSP")
    if setting == "0":
        return None
    if setting:
        return setting
    page = FRONTEND / "index.html"
    mtime = page.stat().st_mtime
    if _csp["mtime"] != mtime:
        hashes = " ".join(f"'sha256-{base64.b64encode(hashlib.sha256(m).digest()).decode()}'"
                          for m in re.findall(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page.read_bytes(), re.S))
        _csp.update(mtime=mtime, value=(
            f"default-src 'self'; script-src 'self' {hashes} 'wasm-unsafe-eval' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
            "worker-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; img-src 'self' data: blob: https:; "
            f"connect-src 'self' data: blob: {' '.join(dict.fromkeys(admin_origins()))}; "
            "font-src 'self' data:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"))
    return _csp["value"]


ASSET_CACHE = "public, max-age=31536000, immutable"     # files named by what's in them never change
SITE_TEXT = "Boulders in 3D, made from photos and videos of their climbs."
_page: dict = {"mtime": None}


def page_parts() -> dict:
    """index.html as it's served: its markup, with its styles and its module script moved to files named by what's in
    them (/static/app.<hash>.css and .js), which browsers and Cloudflare keep a year, so that the page for each link is a
    few KB and the rest comes once. Made again when the file changes. {html, assets: {name: (type, body, gzipped)}}"""
    page = FRONTEND / "index.html"
    mtime = page.stat().st_mtime
    if _page["mtime"] != mtime:
        text, assets = page.read_text(), {}

        def moved(m: re.Match, ext: str, ctype: str, tag: str) -> str:
            body = m[1].encode()
            name = f"app.{hashlib.sha256(body).hexdigest()[:16]}.{ext}"
            assets[name] = (ctype, body, gzip.compress(body, 9))
            return tag.format(f"/static/{name}")
        text = re.sub(r"<style>(.*?)</style>", lambda m: moved(m, "css", "text/css; charset=utf-8", '<link rel="stylesheet" href="{}">'),
                      text, count=1, flags=re.S)
        text = re.sub(r'<script type="module">(.*?)</script>',
                      lambda m: moved(m, "js", "text/javascript; charset=utf-8", '<script type="module" src="{}"></script>'),
                      text, count=1, flags=re.S)
        _page.update(mtime=mtime, html=text, assets=assets)
    return _page


def page_head(title: str, text: str, path: str | None, found: bool = True, mid: str | None = None) -> str:
    """The page's title, and what apps showing a link's preview (WhatsApp, iMessage, Slack) read: its title and a line
    about it, the link's own address, and the picture of the mesh it opens (mid's, thumbs.py), where SITE_URL says the
    site's."""
    e = lambda v: html.escape(v, quote=True)    # noqa: E731
    url = f"{SITE_URL}{path}" if SITE_URL and path is not None else None
    row = q("SELECT source FROM meshes WHERE id=?", mid, one=True) if mid and SITE_URL else None
    pic = row and thumb_now(mid, texture_mode(mid, row["source"]))
    src = f"{SITE_URL}/meshes/{mid}/thumb.jpg?v={pic['v']}" if pic else ""
    image = [f'<meta property="og:image" content="{e(src)}">',
             f'<meta property="og:image:width" content="{thumbs_mod.SIZE[0]}">', f'<meta property="og:image:height" content="{thumbs_mod.SIZE[1]}">',
             f'<meta property="og:image:alt" content="{e(title)} in 3D">'] if pic else []
    return "\n".join([f"<title>{e(title if title == 'BoulderMesh' else title + ' · BoulderMesh')}</title>",
                      f'<meta name="description" content="{e(text)}">', '<meta property="og:site_name" content="BoulderMesh">',
                      '<meta property="og:type" content="website">', f'<meta property="og:title" content="{e(title)}">',
                      f'<meta property="og:description" content="{e(text)}">', *image,
                      f'<meta name="twitter:card" content="{"summary_large_image" if image else "summary"}">',
                      *([f'<meta property="og:url" content="{e(url)}">', f'<link rel="canonical" href="{e(url)}">'] if url and found else []),
                      *([] if found else ['<meta name="robots" content="noindex">'])])


def link_page(kind: str, key: str) -> tuple[int, str, str | None]:
    """What the page at /c/, /b/ or /m/<key> says of itself (page_head()): (status, its head, the address to redirect
    to, for an alias or a name spelt otherwise)."""
    if kind == "m":
        m = q("SELECT name FROM meshes WHERE id=?", key, one=True) if re.fullmatch(r"[0-9a-f]+", key) else None
        g = None if m else gone_mesh(key) if re.fullmatch(r"[0-9a-f]+", key) else None
        if not m and not g:
            return 404, page_head("No such mesh", SITE_TEXT, None, False), None
        own = [links_mod.named(c["name"], c["grade"]) for c in q(
            "SELECT c.name, c.grade FROM mesh_links l JOIN climbs c ON c.id=l.climb_id WHERE l.mesh_id=? AND COALESCE(l.note,'') "
            "NOT LIKE ? ORDER BY c.name", key, SHARED + "%")] if m else []
        title = ", ".join(own[:3]) + (f" +{len(own) - 3}" if len(own) > 3 else "") if own else (m or g)["name"] or "A mesh"
        return 200, page_head(title, f"{title} in 3D, a mesh made from photos and videos of climbing it.", f"/m/{key}",
                              mid=key if m else g["successor"]), None
    slug = links_mod.slugify(key)
    found = find_link(kind, slug)
    if not found:
        return 404, page_head(f"No {'climb' if kind == 'c' else 'boulder'} called {key} here", SITE_TEXT, None, False), None
    if found["slug"] != key:
        return 301, "", f"/{kind}/{found['slug']}"
    if kind == "c":
        c = q("SELECT name, grade, area_path FROM climbs WHERE id=?", found["ref"], one=True) or {}
        name = links_mod.named(c.get("name") or key, c.get("grade"))
        where = [str(a).strip("* ") for a in json.loads(c.get("area_path") or "[]")][-3:][::-1]
        return 200, page_head(name, f"{name} in 3D" + (f" · {', '.join(where)}" if where else "") + ".", f"/c/{key}",
                              mid=climb_mesh(found["ref"])), None
    path = json.loads(found["leaf"] or "[]")
    first = boulder_choices(found["leaf"], found["slug"])["meshes"][:1] if path else []
    n = len(boulder_mesh_sets().get(found["leaf"], []))
    where = ", ".join(str(a).strip("* ") for a in path[-3:-1][::-1])
    return 200, page_head(boulder_name(found["leaf"]) if path else key,
                          f"{boulder_name(found['leaf']) if path else key} in 3D" + (f" · {where}" if where else "")
                          + (f" · {n} meshes" if n > 1 else "") + ".", f"/b/{key}", mid=first[0]["id"] if first else None), None


class _Discard(io.RawIOBase):
    """Where the body of an answer to a HEAD request goes."""

    def writable(self):
        return True

    def write(self, b):
        return len(b)


class Handler(BaseHTTPRequestHandler):
    server_version = "BoulderMesh/0.2"
    sys_version = ""        # not the Python version
    timeout = 120           # seconds a connection may sit idle, so that stalled clients don't hold threads for ever
    head_only = False       # answering a HEAD request: once the headers have gone, nothing more is sent

    # -- helpers ----------------------------------------------------------
    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")   # the camera: Edit…'s Scan button
        super().end_headers()
        if self.head_only:
            self.wfile = _Discard()     # do_HEAD puts the connection's own back

    def address_string(self) -> str:
        """The sender's address, for the log: the one in CLIENT_IP_HEADER if that's set and the request has it."""
        headers = getattr(self, "headers", None)
        return (CLIENT_IP_HEADER and headers and headers.get(CLIENT_IP_HEADER)) or self.client_address[0]

    def send_page(self, head: str | None = None, status: int = 200) -> None:
        """The page (page_parts()), with head (page_head()) for its title and preview: compressed here, as its
        no-transform keeps Caddy and Cloudflare from doing it, and checked again on each visit by its ETag."""
        body = page_parts()["html"].replace("<title>BoulderMesh</title>", head or page_head("BoulderMesh", SITE_TEXT, "/"), 1).encode()
        tag, csp = f'"{hashlib.sha256(body).hexdigest()[:24]}"', page_csp()
        headers = {"Cache-Control": "no-cache, no-transform", "ETag": tag, "Vary": "Accept-Encoding",
                   **({"Content-Security-Policy": csp} if csp else {})}
        if status == 200 and tag in (self.headers.get("If-None-Match") or ""):
            return self.send_body(b"", "text/html; charset=utf-8", 304, headers)
        self.send_body(body, "text/html; charset=utf-8", status, headers)

    def send_asset(self, name: str) -> None:
        """One of the page's own files (page_parts()), kept a year; one an older page named, before a restart, is gone."""
        found = page_parts()["assets"].get(name)
        if not found:
            return self.send_body(json.dumps({"error": "not found: the page has changed since, so load it again"}).encode(),
                                  "application/json", 404, {"Cache-Control": "no-store"})
        ctype, body, packed = found
        tag = f'"{name}"'
        headers = {"Cache-Control": ASSET_CACHE, "ETag": tag, "Vary": "Accept-Encoding"}
        if tag in (self.headers.get("If-None-Match") or ""):
            return self.send_body(b"", ctype, 304, headers)
        self.send_body(body, ctype, 200, headers, packed)

    def send_body(self, body: bytes, ctype: str, status: int, headers: dict, packed: bytes | None = None) -> None:
        """body, gzipped if the browser takes that (packed, if it's been already) and it's worth it."""
        if status != 304 and len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body, headers = packed or gzip.compress(body, 6), {**headers, "Content-Encoding": "gzip"}
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_json(self, obj, code=200, headers: dict | None = None):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data: bytes, ctype: str = "application/octet-stream", cache: str | None = None):
        """Send bytes made for this request, to be kept as long as files are, or as cache says (a Cache-Control)."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-File-Size", str(len(data)))
        self.send_header("Cache-Control", cache or f"public, max-age={FILE_CACHE}")
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, e: Exception, code: int | None = None) -> None:
        """A failure as JSON: its message without this server's paths, and the whole of it in the log. A ValueError
        is the client's fault (400) unless a code is given."""
        code = code or (400 if isinstance(e, ValueError) else 500)
        if code == 500:
            print(f"error: {self.command} {self.path}: {type(e).__name__}: {e}", flush=True)
        self.send_json({"error": str(e).replace(str(DATA), "data").replace(str(ROOT), ".")}, code)

    def send_file(self, path: Path, download=False, headers: dict | None = None, cache: str | None = None):
        """Send a file, as its type if it's one the site serves (SERVED_TYPES; anything under frontend/), else as bytes to
        save; with Last-Modified, so that a browser asking If-Modified-Since gets a 304 instead of the file again. cache:
        its Cache-Control, if not the usual."""
        if not path.is_file():
            return self.send_json({"error": "not found"}, 404)
        st = path.stat()
        in_frontend = FRONTEND.resolve() in path.resolve().parents
        if in_frontend:
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        else:
            ctype = SERVED_TYPES.get(path.suffix.lower(), "application/octet-stream")
            download = download or path.suffix.lower() not in SERVED_TYPES
        modified = email.utils.formatdate(st.st_mtime, usegmt=True)
        # no-transform: proxies in front leave the page as it is, so Cloudflare doesn't add its analytics beacon (which the
        # page's policy would block anyway) or rewrite the page in any other way, whatever its dashboard settings
        cache = cache or ("no-cache, no-transform" if in_frontend and path.suffix == ".html" else f"public, max-age={FILE_CACHE}")
        since = self.headers.get("If-Modified-Since")
        if since and not download:
            try:
                unchanged = email.utils.parsedate_to_datetime(since).timestamp() >= int(st.st_mtime)
            except (TypeError, ValueError):
                unchanged = False
            if unchanged:
                self.send_response(304)
                self.send_header("Last-Modified", modified)
                self.send_header("Cache-Control", cache)
                return self.end_headers()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("X-File-Size", str(st.st_size))    # for progress bars when a proxy compresses it (three.js reads it)
        self.send_header("Last-Modified", modified)
        self.send_header("Cache-Control", cache)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        if self.head_only:
            return
        try:
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True    # the client went away

    def read_body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length isn't a number") from None
        if n < 0:
            raise ValueError("Content-Length is negative")
        if n > MAX_UPLOAD:
            raise ValueError(f"upload exceeds {MAX_UPLOAD // 2**20} MB")
        return self.rfile.read(n)

    # -- changes -----------------------------------------------------------
    def is_editor(self) -> bool:
        """Whether this request carries the editing token (EDIT_TOKEN), as "Authorization: Bearer <token>"."""
        got = self.headers.get("Authorization", "")
        return bool(EDIT_TOKEN) and hmac.compare_digest(got.encode(), f"Bearer {EDIT_TOKEN}".encode())

    def refused(self) -> bool:
        """Refuse a change, before its body is read: every change on a read-only site (READ_ONLY=1); any without the
        editing token when one is set (EDIT_TOKEN); and any that another site's page sent, with an Origin that isn't this
        host, as a visitor to an open site may have such a page open. Says so in the log."""
        why, code, headers = None, 403, None
        if READ_ONLY:
            why = "this copy of the site is view-only"
        elif EDIT_TOKEN and not self.is_editor():
            why, code, headers = "editing this site needs its token", 401, {"WWW-Authenticate": 'Bearer realm="BoulderMesh"'}
        elif self.cross_site():
            why = "refused: sent from another site's page"
        if why:
            print(f"refused {self.command} {self.path} from {self.address_string()}: {why}", flush=True)
            self.send_json({"error": why}, code, headers=headers)
        return bool(why)

    def cross_site(self) -> bool:
        """Whether another site's page sent this: its Origin isn't this host, or the browser says so."""
        hosts = {(self.headers.get(h) or "").lower() for h in ("Host", "X-Forwarded-Host")}
        origin = self.headers.get("Origin")
        return (origin is not None and origin.split("//", 1)[-1].lower() not in hosts) or self.headers.get("Sec-Fetch-Site") == "cross-site"

    def pair(self) -> None:
        """POST /api/pair: {"code"} from the admin page's QR (admin_edit_pair()) gives this browser the editing token. From
        any visitor, as the code is what's checked, but only from this site's own pages; each try is logged."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        self.close_connection = True    # a body left unread would otherwise be taken for the next request
        if self.cross_site():
            return self.send_json({"error": "refused: sent from another site's page"}, 403)
        if not 0 < n <= 1024:
            return self.send_json({"error": "send the code as a small JSON object"}, 400)
        try:
            code = json.loads(self.rfile.read(n)).get("code")
        except (ValueError, AttributeError):
            return self.send_json({"error": "send the code as a small JSON object"}, 400)
        who = f"{self.address_string()} ({(self.headers.get('User-Agent') or 'no User-Agent')[:160]})"
        if READ_ONLY or not EDIT_TOKEN:
            return self.send_json({"error": "this site has no editing token to give"}, 400)
        if not take_pairing(code, who):
            print(f"refused a pairing code from {who}", flush=True)
            return self.send_json({"error": "That code was used already, or is out of date: show a new QR on the admin page."}, 403)
        print(f"gave the editing token to {who}, for the admin page's QR", flush=True)
        threading.Thread(target=NOTIFIER.send, args=("A browser can now edit BoulderMesh", f"From the admin page's QR: {who}"),
                         kwargs={"priority": 2, "tags": ["key"]}, daemon=True).start()
        self.send_json({"token": EDIT_TOKEN})

    def client_report(self, kind: str) -> None:
        """POST /api/client-errors: what went wrong in a visitor's browser (record_client_error()), or POST /api/mesh-views:
        a mesh it showed (record_mesh_view()). Anyone viewing the site may send them, even where nothing may be changed,
        but only from its own pages, small, and not too often."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if self.cross_site():
            return self.send_json({"error": "refused: sent from another site's page"}, 403)
        if not 0 < n <= CLIENT_REPORT_MAX:
            self.close_connection = True    # its body, unread, would otherwise be taken for the next request
            return self.send_json({"error": f"a report is 1 byte to {CLIENT_REPORT_MAX // 1024} KB of JSON"}, 413 if n > 0 else 400)
        try:
            report = json.loads(self.rfile.read(n))
            assert isinstance(report, dict)
        except (ValueError, AssertionError):
            return self.send_json({"error": "a report is a JSON object"}, 400)
        editor = not READ_ONLY and (not EDIT_TOKEN or self.is_editor())
        kept = (record_client_error(report, self.address_string(), self.headers.get("User-Agent") or "", editor) if kind == "error"
                else record_overlap(report, self.address_string(), editor) if kind == "overlap"
                else record_mesh_view(report, self.address_string(), editor))
        if kept is None:
            return self.send_json({"error": "no such mesh"}, 404)
        if not kept:
            return self.send_json({"error": "too many reports from here lately"}, 429)
        self.send_json({"kept": True}, 202)

    def feedback(self) -> None:
        """POST /api/feedback: a comment or bug report from the page's "?" tab (record_feedback()), from any visitor, as
        client_report() takes reports: only from the site's own pages, small, and not too often."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if self.cross_site() or not 0 < n <= feedback_mod.REQUEST_BYTES:
            self.close_connection = True    # its body, unread, would otherwise be taken for the next request
            if self.cross_site():
                return self.send_json({"error": "refused: sent from another site's page"}, 403)
            return self.send_json({"error": f"a comment is 1 byte to {feedback_mod.REQUEST_BYTES // 1024} KB of JSON"}, 413 if n > 0 else 400)
        try:
            report = json.loads(self.rfile.read(n))
        except ValueError:
            report = None
        if not isinstance(report, dict):
            return self.send_json({"error": "a comment is a JSON object"}, 400)
        try:
            kept = record_feedback(report, self.address_string(), self.headers.get("User-Agent") or "",
                                   not READ_ONLY and (not EDIT_TOKEN or self.is_editor()))
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        if kept is False:
            return self.send_json({"error": "too many comments from here lately"}, 429)
        self.send_json({"kept": True}, 202)     # also for one dropped as a bot's, which isn't told

    def rate(self, mid: str) -> None:
        """POST /api/meshes/<id>/rating: a visitor's rating and comment (rate_mesh()), from any visitor, as client_report()
        takes reports: only from the site's own pages, and small."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if self.cross_site():
            return self.send_json({"error": "refused: sent from another site's page"}, 403)
        if not 0 < n <= CLIENT_REPORT_MAX:
            self.close_connection = True
            return self.send_json({"error": f"a rating is 1 byte to {CLIENT_REPORT_MAX // 1024} KB of JSON"}, 413 if n > 0 else 400)
        try:
            report = json.loads(self.rfile.read(n))
            assert isinstance(report, dict)
            got = rate_mesh(mid, report, self.address_string())
        except (ValueError, AssertionError) as e:
            return self.send_json({"error": str(e) if isinstance(e, ValueError) and str(e) else "a rating is a JSON object"}, 400)
        if got is None:
            return self.send_json({"error": "no such mesh"}, 404)
        if got is False:
            return self.send_json({"error": "too many ratings from here lately"}, 429)
        self.send_json({"rating": got})

    def read_multipart(self) -> tuple[dict, list[tuple[str, bytes]]]:
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            raise ValueError("expected multipart/form-data")
        raw = b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + self.read_body()
        msg = BytesParser(policy=HTTP).parsebytes(raw)
        fields, files = {}, []
        for part in msg.iter_parts():
            fname = part.get_filename()
            data = part.get_payload(decode=True) or b""
            if fname:
                files.append((safe_name(fname), data))
            else:
                fields[part.get_param("name", header="content-disposition")] = data.decode(errors="replace")
        return fields, files

    def read_files(self) -> tuple[dict, list[tuple[str, bytes | Path]]]:
        """A multipart request's fields and files, with the files sent before in parts that its "uploads" field names
        (their ids, separated by commas) as paths."""
        fields, files = self.read_multipart()
        files = list(files) + [finished_upload(u) for u in re.split(r"[\s,]+", fields.pop("uploads", "")) if u]
        return fields, files

    def read_json(self) -> dict:
        body = self.read_body()
        return json.loads(body or b"{}")

    def log_message(self, fmt, *args):  # quieter
        # str(): send_error() logs its code first, and a TypeError there dropped the connection without an answer
        if not any(x in str(args[0] if args else "") for x in ("/api/projects", "/api/campaigns")):
            super().log_message(fmt, *args)

    # -- GET ---------------------------------------------------------------
    def do_HEAD(self):
        """A GET's answer without its body, as uptime checks and some caches ask for."""
        own, self.head_only = self.wfile, True
        try:
            self.do_GET()
        finally:
            self.wfile, self.head_only = own, False

    def do_GET(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                return self.send_page()
            m = re.fullmatch(r"/static/(app\.[0-9a-f]{16}\.(?:js|css))", p)
            if m:
                return self.send_asset(m[1])
            m = re.fullmatch(r"/([cbm])/([^/]+)/?", p)
            if m:   # a climb's, a boulder's or a mesh's link: the page, which opens it
                status, head, moved = link_page(m[1], unquote(m[2]))
                if moved:
                    self.send_response(301)
                    self.send_header("Location", moved + (f"?{u.query}" if u.query else ""))
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
                return self.send_page(head, status)
            m = re.fullmatch(r"/api/links/([cbm])/([^/]+)", p)
            if m:   # what the page opens for a link: a climb, a boulder's meshes, or what took a deleted mesh's place
                kind, key = m[1], unquote(m[2])
                if kind == "m":
                    g = gone_mesh(key)
                    return self.send_json({"id": key, "gone": g} if g else {"error": "no such mesh"}, 200 if g else 404)
                found = find_link(kind, links_mod.slugify(key))
                if not found:
                    return self.send_json({"error": f"no {'climb' if kind == 'c' else 'boulder'} called {key} here"}, 404)
                if kind == "b":
                    return self.send_json(boulder_choices(found["leaf"], found["slug"]))
                c = q("SELECT id, name, grade FROM climbs WHERE id=?", found["ref"], one=True)
                if not c:
                    return self.send_json({"error": "that climb is gone"}, 404)
                return self.send_json({"slug": found["slug"], **c, "same": same_named(c["id"], c["name"])})
            if p.startswith("/static/"):
                f = (FRONTEND / p[len("/static/"):]).resolve()
                if FRONTEND.resolve() not in f.parents:
                    return self.send_json({"error": "forbidden"}, 403)
                return self.send_file(f)
            if p == "/api/status":
                env = {k: bool(os.environ.get(k)) for k in ("GOOGLE_CSE_KEY", "GOOGLE_CSE_CX", "FLICKR_KEY")}
                env["HARVEST_DDG"] = harvest_mod.ddg_enabled()
                gpu, gpu_why = pipeline.gpu_choice(USE_GPU)
                return self.send_json({"real_pipeline": pipeline.real_pipeline_ready(), "tools": pipeline.tools_available(),
                                       "queue": jobs.qsize(), "relevance_methods": relevance_mod.available(), "people_masks": pipeline.occlusion.available(), "harvest_sources": HARVEST_SOURCES, "env": env,
                                       "gpu": gpu, "gpu_why": gpu_why,
                                       "ytdlp": bool(shutil.which("yt-dlp") and shutil.which("ffmpeg")),
                                       "youtube_refusing": youtube_mod.refused(), "youtube_searches_refusing": youtube_mod.refused(video=False),
                                       "known_objects": {k: v[2] for k, v in scale_mod.KNOWN.items()},
                                       "opencv": _has("cv2"), "pillow": _has("PIL"),
                                       "policy": {"textures": settings().get("site.textures"), "sources": settings().get("site.sources")},
                                       "map_km": settings().get("site.map_km"),
                                       "donate_url": settings().get("site.donate_url") or None,   # the "?" tab's Donate button
                                       # the volunteers' page, for the header's Volunteer button, where this server takes them
                                       "volunteer_url": VOLUNTEER_URL if VOLUNTEER_PORT else None,
                                       "editing": "none" if READ_ONLY else "token" if EDIT_TOKEN else "open",
                                       "editor": not READ_ONLY and (not EDIT_TOKEN or self.is_editor()), "stopping": _stopping,
                                       "version": VERSION, "thumbs": thumbs_mod.available(),
                                       # where the admin page is, and how to reach it over SSH: for editors only
                                       "admin": admin_link() if not READ_ONLY and (not EDIT_TOKEN or self.is_editor()) else None})
            if p == "/api/jobs":     # what a restart would interrupt now, and what it would only pause
                return self.send_json(jobs_state())
            if p == "/api/deploy":   # for deploy.sh, from this machine only
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    return self.send_json({"error": "only from the server itself"}, 403)
                return self.send_json(deploy_state())
            # ---- projects
            if p == "/api/projects":
                rows, areas = q("SELECT id,name,created,status,mesh_id,demo,kind,climb_id FROM projects ORDER BY created DESC"), project_areas()
                for r in rows:
                    if r["id"] in areas:
                        r["areas"] = areas[r["id"]]
                        # remaking an old mesh (queue_old()): after everything else
                        r["low_priority"] = any(a.get("old_rerun") for a in r["areas"])
                return self.send_json(rows)
            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/effects", p)
            if m:   # what stopping or deleting it would do, to say first
                fx = project_effects(m[1])
                return self.send_json(fx) if fx else self.send_json({"error": "not found"}, 404)
            m = re.fullmatch(r"/api/projects/([0-9a-f]+)", p)
            if m:
                proj = q("SELECT * FROM projects WHERE id=?", m[1], one=True)
                if not proj:
                    return self.send_json({"error": "not found"}, 404)
                photos = PROJECTS / m[1] / "photos"
                proj["photos"] = sorted(f.name for f in photos.iterdir() if f.suffix.lower() in PHOTO_EXT) if photos.exists() else []
                videos = PROJECTS / m[1] / "videos"
                proj["videos_waiting"] = sorted(f.name for f in videos.iterdir() if f.is_file()) if videos.exists() else []
                proj["log"] = read_log(m[1], proj.get("log") or "")
                proj["failure"] = (last_failure(m[1]) if proj["status"] == "failed" else
                                   last_failure(m[1], "INTERRUPTED: ") if proj["status"] == "interrupted" else None)
                proj["progress"] = with_typicals(json.loads(proj["progress"]), proj.get("harvest"), m[1]) if proj.get("progress") else None
                proj["harvest"] = json.loads(proj["harvest"]) if proj.get("harvest") else None
                proj["known_object"] = known_object(m[1])
                rejected = PROJECTS / m[1] / "rejected"
                try:
                    clipped = "boulder" in json.loads((PROJECTS / m[1] / "relevance.json").read_text()).get("methods", [])
                except (FileNotFoundError, ValueError):
                    clipped = False
                order = ("nomatch", "notboulder", "card")     # the plausible ones first
                found = [(f.name.split("_", 1)[0], f.name) for f in (rejected.iterdir() if rejected.exists() else [])]
                proj["filtered"] = [{"file": name, "why": relevance_mod.NOMATCH_CLIPPED if why == "nomatch" and clipped else relevance_mod.REASONS[why]}
                                    for why, name in sorted((x for x in found if x[0] in order), key=lambda x: (order.index(x[0]), x[1]))]
                setting = settings().get("site.sources")     # what of each photo the page may show
                proj["access"] = {}
                for name in proj["photos"] + [x["file"].split("_", 1)[1] for x in proj["filtered"]]:
                    e = photo_entry(m[1], name)
                    proj["access"][name] = {"show": policy.source_access(setting, e), "page": e.get("page") or e.get("url")}
                return self.send_json(proj)
            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/harvest", p)
            if m:
                d = PROJECTS / m[1]
                out = {}
                for name in ("sources", "diversity"):
                    f = d / f"{name}.json"
                    out[name] = json.loads(f.read_text()) if f.exists() else None
                return self.send_json(out)
            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/photo-groups", p)
            if m:
                if not (PROJECTS / m[1]).is_dir():
                    return self.send_json({"error": "not found"}, 404)
                return self.send_json({"groups": photo_groups(m[1], (qs.get("climb") or [None])[0])})
            m = re.fullmatch(r"/photos/([0-9a-f]+)/(rejected/)?([A-Za-z0-9._-]+)", p)
            if m:   # rejected photos are named <reason>_<name>; ?thumb: a small copy (photo_thumb())
                name = m[3].split("_", 1)[1] if m[2] and "_" in m[3] else m[3]
                shown = policy.source_access(settings().get("site.sources"), photo_entry(m[1], name)) == "show"
                if not shown and not ("thumb" in qs and settings().get("site.small_thumbs")):
                    return self.send_json({"error": "not shown: it isn't clearly free to copy"}, 403)
                folder = "rejected" if m[2] else "photos"
                if "thumb" in qs:   # one not clearly free to copy: only a SMALL_PX copy
                    try:
                        small = photo_thumb(m[1], folder, m[3], THUMB_PX if shown else SMALL_PX)
                    except (OSError, ValueError) as e:
                        return self.send_json({"error": f"no small copy: {e}"}, 500)
                    return self.send_file(small, cache="public, max-age=86400") if small else self.send_json({"error": "not found"}, 404)
                return self.send_file(PROJECTS / m[1] / folder / m[3])
            # ---- meshes
            if p == "/api/meshes":
                rows = q("SELECT * FROM meshes ORDER BY created DESC")
                links = q("SELECT l.mesh_id, l.note, c.id, c.name, c.grade, c.url, c.area_path FROM mesh_links l JOIN climbs c ON c.id=l.climb_id")
                by, leaves = {}, {}
                for l in links:     # shared: an area linked it as on the same boulder as the climb the mesh was made for
                    shared = (l["note"] or "").startswith(SHARED)
                    by.setdefault(l["mesh_id"], []).append({"id": l["id"], "name": l["name"], "grade": l["grade"], "url": l["url"],
                                                            "shared": shared})
                    if not shared:
                        leaves.setdefault(l["mesh_id"], set()).add(l["area_path"])
                urls = {}
                on = {}
                for c in q("SELECT c.id, c.name, c.grade, c.area_path FROM climbs c WHERE c.area_path IN "
                           "(SELECT boulder FROM meshes WHERE boulder IS NOT NULL) ORDER BY c.name"):
                    on.setdefault(c.pop("area_path"), []).append(c)
                kinds = boulder_kinds()
                for key in {r["boulder"] for r in rows if is_pool(r["boulder"])}:     # a pool's: its climbs
                    on[key] = [{k: c[k] for k in ("id", "name", "grade")} for c in boulder_climbs(key)]
                areas, rated = mesh_areas(), mesh_ratings()
                pooled_views = {r["id"]: viewpoints.load(MESHES / r["id"]) or {} for r in rows if r["boulder"]}
                homes = boulder_homes([r for r in rows if not r.get("replaced_by")], pooled_views)
                least = settings().get("boulders.placed_percent")
                for r in rows:
                    r["climbs"] = by.get(r["id"], [])
                    r["rating"] = rated.get(r["id"])
                    r["old"] = mesh_is_old(r)
                    if r["boulder"]:    # a pooled mesh (a boulder's, or a pool's): its climbs, and where each is seen from
                        views = pooled_views[r["id"]]
                        r["boulder_climbs"] = [{**c, "view": (views.get("climbs") or {}).get(c["id"]),
                                                "own": (views.get("unplaced") or {}).get(c["id"], 0)} for c in on.get(r["boulder"], [])]
                        r["pooled"] = pooled_summary(r["boulder"], r["boulder_climbs"], kinds, r["id"], homes, least)
                    # the Mountain Project page of the lowest area a mesh of several climbs is on: a pooled mesh's, or
                    # that of the climbs linked to one, when they're all on one
                    leaf = (pool_leaf(r["boulder"]) if r["boulder"] else
                            next(iter(leaves[r["id"]])) if sum(not c["shared"] for c in r["climbs"]) > 1 and len(leaves[r["id"]]) == 1 else None)
                    r["boulder_url"] = leaf_url(leaf, urls) if leaf else None
                    r["texture"] = texture_mode(r["id"], r["source"])
                    try:    # one mesh's unreadable file shouldn't empty the whole list
                        r["cleanup"] = meshclean.summary(meshclean.load(served_dir(r["id"], r["texture"])))
                        r["orientation"] = orientation.load(MESHES / r["id"])
                        r["sources"] = provenance.summary(mesh_record(MESHES / r["id"]))
                        r["beta"] = beta_mod.summary(beta_mod.load(MESHES / r["id"]))
                        r["marked"] = len(beta_mod.load_holds(MESHES / r["id"])["holds"])
                        r["scale"] = scale_mod.summary(scale_mod.load(MESHES / r["id"]))
                    except Exception as e:
                        print(f"error: listing mesh {r['id']}: {type(e).__name__}: {e}", flush=True)
                    r["cameras_kept"] = (MESHES / r["id"] / orientation.CAMERAS_FILE).exists()
                    r["timings"] = timings_brief(mesh_timings(r["id"]))
                    try:
                        r["metrics"] = meshmetrics.load(MESHES / r["id"])
                    except Exception as e:
                        r["metrics"] = None
                        print(f"error: mesh {r['id']}'s metrics: {type(e).__name__}: {e}", flush=True)
                    r["areas"], r["area_path"] = areas(r)
                    r["splat"] = r.get("texture") == "full" and (MESHES / r["id"] / splat_mod.FILE).exists()
                    try:
                        r["pack"] = pack_info(r)
                        r["v"] = mesh_version(r["id"], r["texture"], r["filename"])     # for its files' addresses
                    except Exception as e:
                        r["pack"] = r["v"] = None
                        print(f"error: mesh {r['id']}'s packed copies: {type(e).__name__}: {e}", flush=True)
                    try:
                        r["thumb"] = thumb_info(r)      # its picture, for its card (thumbs.py)
                    except Exception as e:
                        r["thumb"] = None
                        print(f"error: mesh {r['id']}'s picture: {type(e).__name__}: {e}", flush=True)
                # the names in its climbs' and its boulder's links, for Share, and how many current meshes its boulder has
                # (boulder_mesh_sets(): its own, what that left out, its climbs' own), for the viewer's Meshes button
                try:    # links failing shouldn't empty the list
                    cnames, bnames = link_names()
                    sets = boulder_mesh_sets(rows, kinds)
                    leaf_of = {mid: leaf for leaf, ids in sets.items() for mid in ids}
                    current = {r["id"] for r in rows if not r.get("replaced_by")}
                    old = {r["id"] for r in rows if r["old"] or r.get("old_data")}     # either kind: left out for visitors
                    if any(c["id"] not in cnames for r in rows for c in r["climbs"]) or \
                            any(leaf not in bnames for leaf, ids in sets.items() if current.intersection(ids)):
                        cnames, bnames = link_names(force=True)     # one added since the last naming
                    for r in rows:
                        for c in r["climbs"] + r.get("boulder_climbs", []):
                            c["slug"] = cnames.get(c["id"])
                        leaf = leaf_of.get(r["id"])
                        r["boulder_link"] = {"slug": bnames[leaf], "name": boulder_name(leaf),
                                             "meshes": sum(mid in current for mid in sets[leaf]),
                                             "old": sum(mid in current and mid in old for mid in sets[leaf])} if leaf in bnames else None
                except Exception as e:
                    print(f"error: meshes' links: {type(e).__name__}: {e}", flush=True)
                return self.send_json(rows)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/beta", p)
            if m:   # what each video's climber did, and the holds marked by hand
                if not (MESHES / m[1]).is_dir():
                    return self.send_json({"error": "not found"}, 404)
                found = beta_mod.load(MESHES / m[1])
                setting = settings().get("site.sources")
                for v in (found or {}).get("videos", []):     # a video's frames, drawn on, are copies of it
                    access = policy.source_access(setting, video_entry(v["url"]))
                    if access != "show":
                        v.pop("image", None)
                    if access == "hide":
                        v.update(url=None, title="a video that isn't clearly free to copy", uploader=None)
                return self.send_json({"found": found, "marked": beta_mod.load_holds(MESHES / m[1]), "available": beta_mod.available()})
            if p == "/api/videos":   # every video known, and what it or its channel says about the climber's size
                return self.send_json(q("""SELECT v.url,v.title,v.uploader,v.channel_url,v.duration,v.upload_date,v.license,
                      v.height_cm,v.wingspan_cm,v.ape_index_cm,v.first_person,v.quotes,
                      c.height_cm AS channel_height_cm,c.wingspan_cm AS channel_wingspan_cm,c.ape_index_cm AS channel_ape_index_cm,
                      c.quotes AS channel_quotes,
                      (SELECT GROUP_CONCAT(cv.climb_id || ':' || cv.found_by || ':' || COALESCE(cv.frames,'')) FROM climb_videos cv
                       WHERE cv.url=v.url) AS climbs
                    FROM videos v LEFT JOIN channels c ON c.channel_url=v.channel_url ORDER BY v.url"""))
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/inputs", p)
            if m:   # what it was made from (meshinputs.py): ?files=1 with each photo's size and SHA-1
                record = inputs_of(m[1]) if (MESHES / m[1]).is_dir() else None
                if not record:
                    return self.send_json({"error": "not made from a project, or no such mesh"}, 404)
                return self.send_json(record if qs.get("files") == ["1"] else meshinputs.brief(record))
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/sources", p)
            if m:   # every photo, with where it came from: what crediting or re-hosting a mesh needs
                record = mesh_record(MESHES / m[1])
                return self.send_json(record) if record else self.send_json({"error": "not recorded for this mesh"}, 404)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/cleanup", p)
            if m:
                record = meshclean.load(served_dir(m[1]))
                return self.send_json(cleanup_answer(m[1], record)) if record else self.send_json({"error": "no cleanup"}, 404)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/nearby", p)
            if m:   # the meshes around one, to show around it
                try:
                    radius = float((qs.get("radius") or ["50"])[0])
                except ValueError:
                    return self.send_json({"error": "radius: a number of metres"}, 400)
                if not NEARBY_RADIUS[0] <= radius <= NEARBY_RADIUS[1]:
                    return self.send_json({"error": f"radius: from {NEARBY_RADIUS[0]:g} to {NEARBY_RADIUS[1]:g} metres"}, 400)
                found = nearby(m[1], radius)
                return self.send_json(found) if found else self.send_json({"error": "not found"}, 404)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/terrain", p)
            if m:   # the ground around it, for Nearby: where its heights are, and how they lie around the mesh
                me = q("SELECT lat, lng FROM meshes WHERE id=?", m[1], one=True)
                if not me:
                    return self.send_json({"error": "not found"}, 404)
                try:
                    radius = float((qs.get("radius") or ["50"])[0])
                except ValueError:
                    return self.send_json({"error": "radius: a number of metres"}, 400)
                if me["lat"] is None or me["lng"] is None:
                    return self.send_json({"source": None, "why": "this mesh has no coordinates"})
                try:
                    found = terrain_mod.square(TERRAIN, me["lat"], me["lng"], 2 * (1.5 * min(max(radius, 5), 1000) + 40))
                except Exception as e:
                    print(f"terrain: {type(e).__name__}: {e}", flush=True)
                    return self.send_json({"error": f"the elevation service failed: {type(e).__name__}"}, 502)
                if not found:
                    return self.send_json({"source": None, "why": "no free elevation data here: only the US's and France's"})
                return self.send_json({**terrain_mod.placed(found, me["lat"], me["lng"]), "heights": f"/terrain/{found['key']}.bin"})
            m = re.fullmatch(r"/terrain/([0-9a-f]{16})\.bin", p)
            if m:   # a square's heights: what's at an address never changes
                return self.send_file(TERRAIN / f"{m[1]}.bin", cache=f"public, max-age={MESH_CACHE}, immutable")
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/heading", p)
            if m:   # which way it faces: the best known, and each way it was worked out
                if not (MESHES / m[1]).is_dir():
                    return self.send_json({"error": "not found"}, 404)
                return self.send_json({"heading": mesh_heading(m[1]), "record": heading_mod.load(MESHES / m[1])})
            m = re.fullmatch(r"/api/uploads/([0-9a-f]{12})", p)
            if m:   # how much of a file sent in parts has come, to carry on from there
                state = upload_state(m[1])
                return self.send_json(state) if state else self.send_json({"error": "no such upload"}, 404)
            m = re.fullmatch(r"/meshes/([0-9a-f]+)/thumb(\.small)?\.jpg", p)
            if m:   # its picture (thumbs.py), drawn as its texture is shown now, or none; kept as a texture is, as it shows one
                row = q("SELECT source FROM meshes WHERE id=?", m[1], one=True)
                now = row and thumb_now(m[1], texture_mode(m[1], row["source"]))
                if not now:
                    return self.send_json({"error": "it has no picture yet"}, 404)
                cache = f"public, max-age={TEXTURE_CACHE}" if qs.get("v", [""])[0] in ("", now["v"]) else "no-cache"
                return self.send_file(MESHES / m[1] / thumbs_mod.DIR / (thumbs_mod.SMALL_FILE if m[2] else thumbs_mod.LARGE), cache=cache)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/thumb", p)
            if m:   # its picture, and whether one's being drawn or failed, for Share's Use this view
                row = q("SELECT source, filename FROM meshes WHERE id=?", m[1], one=True)
                if not row:
                    return self.send_json({"error": "not found"}, 404)
                mode, rec = texture_mode(m[1], row["source"]), thumbs_mod.load(MESHES / m[1])
                want = thumbs_mod.key(mesh_version(m[1], mode, row["filename"]), MESHES / m[1], rec.get("pose"))
                return self.send_json({"thumb": thumb_now(m[1], mode), "queued": m[1] in _thumbing,
                                       "failed": rec.get("failed") if rec.get("failed_key") == want else None})
            m = re.fullmatch(r"/meshes/([0-9a-f]+)/([A-Za-z0-9._-]+)", p)
            if m:
                mid, name = m[1], m[2]
                if name.startswith("beta_"):     # a video's frame, drawn on: shown as the video's licence allows
                    v = next((v for v in (beta_mod.load(MESHES / mid) or {}).get("videos", []) if v.get("image") == name), None)
                    if not v or policy.source_access(settings().get("site.sources"), video_entry(v["url"])) != "show":
                        return self.send_json({"error": "not shown: it isn't clearly free to copy"}, 403)
                    return self.send_file(MESHES / mid / name)
                mode = texture_mode(mid)
                if name == splat_mod.FILE and mode != "full":     # what's around the rock, learnt from all its photos
                    return self.send_json({"error": "not shown: the scene is made from photos that aren't all free to copy"}, 403)
                base = served_dir(mid, mode)
                f = base / name
                is_image = f.suffix.lower() in (".png", ".jpg", ".jpeg")
                if mode == "hidden" and is_image:
                    return self.send_json({"error": "textures aren't shown"}, 403)
                row = q("SELECT filename FROM meshes WHERE id=?", mid, one=True)
                cache = f"public, max-age={TEXTURE_CACHE}" if is_image else None
                if qs.get("v"):     # the mesh's version, from the list: kept long if it's still this one's, else not at all
                    if row and qs["v"][0] == mesh_version(mid, mode, row["filename"]):
                        cache = f"public, max-age={TEXTURE_CACHE}" if is_image or name == splat_mod.FILE else f"public, max-age={MESH_CACHE}, immutable"
                    else:
                        cache = "no-cache"
                side = int(qs["max"][0]) if qs.get("max", [""])[0].isdigit() else 0
                if side in SHRINK_SIDES and is_image and f.is_file():
                    if not _has("PIL"):
                        return self.send_json({"error": "shrinking textures needs Pillow"}, 501)
                    f = shrunk_texture(f, side)
                lod = (qs.get("lod") or [""])[0]
                if lod and lod not in glbpack.LODS:
                    return self.send_json({"error": f"lod: one of {', '.join(glbpack.LODS)}"}, 400)
                if f.suffix.lower() == ".glb" and f.is_file():
                    if "cleaned" in qs and meshclean.available():
                        f = meshclean.write_cleaned(base) or f
                    if mode == "hidden":
                        f = meshclean.strip_textures(f, base / f"{f.stem}.untextured.glb")
                    if lod:     # its packed copy, which the viewer only asks for when the list of meshes says it has one
                        packed = glbpack.path(f, lod)
                        if packed is None:
                            queue_pack(mid, urgent=True)
                            return self.send_json({"error": "this mesh isn't packed yet"}, 404)
                        f = packed
                if name == meshclean.FACES_FILE and lod and f.is_file():     # its cleanup, in a packed copy's order
                    record = meshclean.load(base)
                    src = viewer_glb(mid, mode, row["filename"]) if row else None
                    data = glbpack.labels(src, lod, f, record["label_bytes"]) if src and record and not record.get("deleted") else None
                    if data is None:
                        return self.send_json({"error": "this mesh isn't packed yet"}, 404)
                    return self.send_bytes(data, cache=cache)
                return self.send_file(f, download="download" in qs, cache=None if "download" in qs else cache)
            if p == "/api/campaigns":
                return self.send_json(campaigns_listed())
            # ---- climbs
            if p == "/api/climbs":   # the newest, or with q those of every climb with each of its words: limit (most 2000) from offset
                sql, args, order = "SELECT id FROM climbs", [], []
                conds, doubts = [], coord_doubts()
                if qs.get("doubtful"):    # only those whose coordinates look wrong
                    conds.append(f"id IN ({','.join('?' * len(doubts)) or 'NULL'})")
                    args += list(doubts)
                text = (qs.get("q") or [""])[0].strip()
                for word in text.split()[:8]:
                    conds.append("(name LIKE ? OR boulder_name LIKE ? OR area_path LIKE ? OR grade LIKE ?)")
                    args += [f"%{word}%"] * 4
                if text:
                    order, oargs = ["lower(name)=lower(?) DESC", "name LIKE ? DESC"], [text, f"%{text}%"]
                if qs.get("source"):
                    conds.append("source=?")
                    args.append(qs["source"][0])
                if qs.get("meshed"):
                    conds.append("(id IN (SELECT climb_id FROM mesh_links) OR area_path IN (SELECT boulder FROM meshes WHERE boulder IS NOT NULL))")
                if conds:
                    sql += " WHERE " + " AND ".join(conds)
                if qs.get("multi"):     # those with more than one mesh first, to pick out
                    order = [f"{MESH_COUNT} > 1 DESC"] + order
                try:
                    limit = min(max(int((qs.get("limit") or ["500"])[0]), 1), 2000)
                    offset = max(int((qs.get("offset") or ["0"])[0]), 0)
                except ValueError:
                    return self.send_json({"error": "limit and offset are whole numbers"}, 400)
                sql += " ORDER BY " + ", ".join(order + ["created DESC"]) + f" LIMIT {limit} OFFSET {offset}"
                ids = [r["id"] for r in q(sql, *args, *([SHARED + "%"] if qs.get("multi") else []), *(oargs if text else []))]
                return self.send_json([{**r, "coords_doubt": doubts.get(r["id"])} for r in climb_rows(ids)])
            if p == "/api/climbs/count":
                return self.send_json({"climbs": q("SELECT count(*) AS n FROM climbs", one=True)["n"], "doubtful": len(coord_doubts()),
                                       "multi": q(f"SELECT count(*) AS n FROM climbs WHERE {MESH_COUNT} > 1", SHARED + "%", one=True)["n"]})
            m = re.fullmatch(r"/api/climbs/([0-9a-f]+)", p)
            if m:
                r = climb_row(m[1], full=True)
                if not r:
                    return self.send_json({"error": "not found"}, 404)
                r["coords_doubt"] = coord_doubts().get(m[1])
                setting = settings().get("site.sources")
                if setting != "all":    # its page's photos and videos, as far as their licences allow
                    data = r["data"]
                    images = []
                    for im in data.get("images") or []:
                        access = policy.source_access(setting, {"source": im.get("source"), "license": im.get("license")})
                        if access == "show":
                            images.append(im)
                        elif access == "link":
                            images.append({**im, "url": None})
                    data["images"] = images
                    if setting == "permitted":
                        data["videos"] = [v for v in data.get("videos") or [] if policy.permitted(video_entry(v["url"]))]
                        r["videos_found"] = [v for v in r["videos_found"] if policy.permitted(video_entry(v["url"]))]
                return self.send_json(r)
            if p == "/api/geojson":
                feats = []
                for r in q("SELECT id,name,grade,kind,lat,lng,coord_source,boulder_name,source FROM climbs WHERE lat IS NOT NULL"):
                    feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
                                  "properties": {"layer": "climb", **r}})
                for r in q("SELECT id,name,lat,lng,filename FROM meshes WHERE lat IS NOT NULL"):
                    feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
                                  "properties": {"layer": "mesh", **r}})
                return self.send_json({"type": "FeatureCollection", "features": feats})
            return self.send_json({"error": "not found"}, 404)
        except Exception as e:
            return self.send_error_json(e, 500)

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/deploy/drain":    # deploy.sh, from this machine only: {"on": bool, "now": bool}
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self.send_json({"error": "only from the server itself"}, 403)
            b = self.read_json()
            return self.send_json(drain(bool(b.get("on", True)), bool(b.get("now"))))
        if p in ("/api/client-errors", "/api/mesh-views", "/api/nearby-overlaps"):    # from any visitor, not only editors
            return self.client_report({"/api/client-errors": "error", "/api/mesh-views": "view"}.get(p, "overlap"))
        if p == "/api/feedback":    # also from any visitor
            return self.feedback()
        if p == "/api/pair":    # also from any visitor: the code is what's checked
            return self.pair()
        m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/rating", p)
        if m:   # also from any visitor
            return self.rate(m[1])
        if self.refused():
            return
        try:
            # ---- projects
            if p == "/api/projects":
                body = self.read_json()
                pid = new_id()
                name = (body.get("name") or f"project {pid}").strip()[:80]
                (PROJECTS / pid / "photos").mkdir(parents=True)
                q("INSERT INTO projects(id,name,created,kind,climb_id) VALUES(?,?,?,?,?)", pid, name, time.time(),
                  "user", body.get("climb_id"), write=True)
                return self.send_json({"id": pid, "name": name}, 201)

            if p == "/api/uploads":    # {name, size}: a file to come in parts of at most PART_MAX
                body = self.read_json()
                uid = start_upload(str(body.get("name") or "file"), int(body.get("size") or 0))
                return self.send_json({"id": uid, "part": PART_MAX}, 201)
            m = re.fullmatch(r"/api/uploads/([0-9a-f]{12})", p)
            if m:   # ?offset=<bytes so far>, with the next part as the body
                state = upload_state(m[1])
                n = int(self.headers.get("Content-Length") or 0)
                if not state:
                    return self.send_json({"error": "no such upload: start again"}, 404)
                if not 0 < n <= PART_MAX:
                    return self.send_json({"error": f"a part is 1 byte to {PART_MAX // 2**20} MB"}, 400)
                part = self.read_body()
                offset = int((parse_qs(urlparse(self.path).query).get("offset") or ["-1"])[0])
                data, _ = upload_paths(m[1])
                with _uploads_lock:
                    have = data.stat().st_size
                    if offset != have:      # a part lost or sent twice: the sender carries on from what's here
                        return self.send_json({"error": f"expected the part at byte {have}", "received": have}, 409)
                    if have + len(part) > state["size"]:
                        return self.send_json({"error": "more than the size it was started with"}, 400)
                    with open(data, "ab") as f:
                        f.write(part)
                return self.send_json({"received": have + len(part), "size": state["size"]})

            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/photos", p)
            if m:
                pid = m[1]
                if not q("SELECT 1 FROM projects WHERE id=?", pid, one=True):
                    return self.send_json({"error": "not found"}, 404)
                _, files = self.read_files()
                saved, videos = [], []
                for fname, data in files:
                    ext = Path(fname).suffix.lower()
                    if ext in PHOTO_EXT:
                        put_file(data, PROJECTS / pid / "photos" / fname)
                        saved.append(fname)
                    elif ext in VIDEO_EXT:      # turned into photos by frames_worker
                        (PROJECTS / pid / "videos").mkdir(exist_ok=True)
                        put_file(data, PROJECTS / pid / "videos" / fname)
                        videos.append(fname)
                    else:
                        drop_file(data)
                if videos:
                    append_log(pid, f"videos uploaded, to be turned into photos: {', '.join(videos)}")
                    _frames_wake.set()
                return self.send_json({"saved": saved, "videos": videos})

            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/known-object", p)
            if m:   # {kind} or {what, height_cm, width_cm}, or {} for nothing: its mesh is sized again
                proj = q("SELECT mesh_id FROM projects WHERE id=?", m[1], one=True)
                if not proj:
                    return self.send_json({"error": "not found"}, 404)
                body = self.read_json()
                if not isinstance(body, dict) or body and not scale_mod.known_object(body):
                    return self.send_json({"error": "give one of " + ", ".join(scale_mod.KNOWN) + ", or a bottle's height_cm (5-100)"}, 400)
                f = PROJECTS / m[1] / KNOWN_OBJECT_FILE
                if body:
                    f.write_text(json.dumps(body))
                else:
                    f.unlink(missing_ok=True)
                mid = proj["mesh_id"]
                sizing = bool(mid and q("SELECT 1 FROM meshes WHERE id=?", mid) and (MESHES / mid / "cameras.json").exists() and queue_scale(mid))
                return self.send_json({"known_object": body or None, "sizing": sizing})
            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/reconstruct", p)
            if m:
                pid = m[1]
                proj = q("SELECT status, kind FROM projects WHERE id=?", pid, one=True)
                if not proj:
                    return self.send_json({"error": "not found"}, 404)
                if proj["status"] in ("queued", "running", "harvesting"):
                    return self.send_json({"error": "already running"}, 409)
                n = len([f for f in (PROJECTS / pid / "photos").iterdir() if f.suffix.lower() in PHOTO_EXT])
                if n < (1 if not pipeline.real_pipeline_ready() else 3):
                    return self.send_json({"error": "upload at least 3 overlapping photos"}, 400)
                q("UPDATE projects SET status='queued', progress=NULL WHERE id=?", pid, write=True)
                queue_job({"type": "reconstruct", "pid": pid, "internet": proj["kind"] in ("harvest", "boulder")})
                return self.send_json({"queued": True, "position": jobs.qsize()}, 202)

            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/retry", p)
            if m:
                return self.send_json(*retry_project(m[1]))
            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/stop", p)
            if m:
                return self.send_json(*stop_project(m[1]))
            if p == "/api/projects/retry-interrupted":     # every project a restart interrupted, oldest first
                ids = [r["id"] for r in q("SELECT id FROM projects WHERE status='interrupted' ORDER BY created")]
                return self.send_json({"queued": sum(retry_project(pid)[1] == 202 for pid in ids), "of": len(ids)})

            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/restore", p)
            if m:
                # a photo the relevance filter rejected wrongly
                name = Path(self.read_json().get("file") or "").name
                src = PROJECTS / m[1] / "rejected" / name
                if "_" not in name or not src.is_file():
                    return self.send_json({"error": "not found"}, 404)
                src.rename(PROJECTS / m[1] / "photos" / name.split("_", 1)[1])
                record_put_back(m[1], name.split("_", 1)[1])
                return self.send_json({"restored": name.split("_", 1)[1]})

            # ---- meshes
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/thumb", p)
            if m:   # {pose}: draw its picture from that camera, or (None) as it would be by itself
                if not thumbs_mod.available():
                    return self.send_json({"error": "pictures of meshes can't be drawn on this server"}, 501)
                if not (MESHES / m[1]).is_dir():
                    return self.send_json({"error": "not found"}, 404)
                pose = thumbs_mod.check_pose(self.read_json().get("pose"))
                rec = thumbs_mod.load(MESHES / m[1])
                rec.update(pose=pose, failed=None, failed_key=None)
                thumbs_mod.save(MESHES / m[1], rec)
                queue_thumb(m[1], urgent=True)
                return self.send_json({"queued": True})
            if p == "/api/meshes":
                fields, files = self.read_files()
                if not files:
                    return self.send_json({"error": "no file"}, 400)
                mid = new_id()
                d = MESHES / mid
                d.mkdir(parents=True)
                main, skipped = None, []
                for fname, data in files:
                    ext = Path(fname).suffix.lower()
                    if ext not in MESH_EXT and ext not in MESH_EXTRA_EXT:    # a mesh, its textures and material; never a page
                        skipped.append(fname)
                        drop_file(data)
                        continue
                    put_file(data, d / fname)
                    if ext in MESH_EXT and main is None:
                        main = fname
                if main is None:
                    shutil.rmtree(d)
                    return self.send_json({"error": f"need one of {sorted(MESH_EXT)}"}, 400)
                name = (fields.get("name") or Path(main).stem)[:80]
                lat = float(fields["lat"]) if fields.get("lat") else None
                lng = float(fields["lng"]) if fields.get("lng") else None
                q("INSERT INTO meshes(id,name,filename,source,created,size,demo,lat,lng) VALUES(?,?,?,?,?,?,?,?,?)", mid, name, main, "upload", time.time(),
                  (d / main).stat().st_size, 0, lat, lng, write=True)
                if fields.get("climb_id"):
                    link_mesh(mid, fields["climb_id"], "linked at upload")
                return self.send_json({"id": mid, "name": name, "filename": main, "skipped": skipped}, 201)

            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/cleanup", p)
            if m:
                if not (MESHES / m[1]).is_dir():
                    return self.send_json({"error": "not found"}, 404)
                if not meshclean.available():
                    return self.send_json({"error": "cleaning up meshes needs numpy"}, 501)
                try:
                    with _cleanup_lock:
                        record = meshclean.clean(served_dir(m[1]), print, force=True)
                except ValueError as e:
                    return self.send_json({"error": f"can't clean up this mesh: {e}"}, 400)
                return self.send_json(cleanup_answer(m[1], record)) if record else self.send_json({"error": "only GLB meshes can be cleaned up"}, 400)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/cleanup/(edit|undo)", p)
            if m:   # a cut, or an area hidden or shown, with faces chosen in the viewer; or the last change undone
                if not (MESHES / m[1]).is_dir():
                    return self.send_json({"error": "not found"}, 404)
                if not meshclean.available():
                    return self.send_json({"error": "cleaning up meshes needs numpy"}, 501)
                d = served_dir(m[1])
                try:
                    with _cleanup_lock:
                        if m[2] == "undo":
                            return self.send_json(cleanup_answer(m[1], meshclean.undo(d)))
                        body, record = self.read_json(), meshclean.load(d)
                        if not record or record.get("deleted"):
                            raise ValueError("this mesh has no cleanup to edit")
                        record, made = meshclean.edit(d, body.get("op"), chosen_faces(m[1], body, record))
                    return self.send_json(cleanup_answer(m[1], record, made=made))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/links", p)
            if m:
                body = self.read_json()
                if not q("SELECT 1 FROM climbs WHERE id=?", body.get("climb_id"), one=True):
                    return self.send_json({"error": "unknown climb"}, 404)
                link_mesh(m[1], body["climb_id"], body.get("note"))
                return self.send_json({"linked": True}, 201)

            m = re.fullmatch(r"/api/climbs/([0-9a-f]+)/boulder", p)
            if m:   # mesh its boulder from every photo of its climbs, harvesting those that haven't been first (run_boulder())
                climb = climb_row(m[1])
                if not climb:
                    return self.send_json({"error": "not found"}, 404)
                boulder = boulder_of(climb["area_path"])
                if not boulder:
                    return self.send_json({"error": "its area holds several boulders, so which one it's on isn't known"}, 409)
                return self.send_json(*mesh_boulder(boulder))

            # ---- climbs
            if p == "/api/climbs/import":
                body = self.read_json()
                url = (body.get("url") or "").strip()
                recs = climbs_mod.fetch_climb_url(url, include_children=bool(body.get("include_children", True)))
                ids = [upsert_climb(c) for c in recs]
                return self.send_json({"imported": [climb_row(i) for i in ids]}, 201)

            if p == "/api/campaigns":
                b = self.read_json()
                try:
                    camp = create_campaign(b.get("area") or "", b.get("order") or "popularity", b.get("csv"), bool(b.get("start", True)),
                                           bool(b.get("without_youtube")), b.get("max_meshes"), bool(b.get("count_failures")),
                                           bool(b.get("check_new")), bool(b.get("same_photos_once", True)), bool(b.get("keep_better", True)),
                                           b.get("mapper_below"), b.get("mapper_more"))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                return self.send_json(campaign_view(camp), 201)
            m = re.fullmatch(r"/api/campaigns/([0-9a-f]+)/options", p)
            if m:
                if not campaign_status(m[1]):
                    return self.send_json({"error": "not found"}, 404)
                try:
                    return self.send_json(campaign_view(set_campaign_options(m[1], self.read_json())))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
            m = re.fullmatch(r"/api/campaigns/([0-9a-f]+)/(start|stop)", p)
            if m:
                if not campaign_status(m[1]):
                    return self.send_json({"error": "not found"}, 404)
                if m[2] == "start":
                    q("UPDATE campaigns SET status='running', note=NULL, only_retries=0 WHERE id=? AND status IN ('stopped','stopping')", m[1], write=True)
                else:
                    stop_campaign(m[1], bool(self.read_json().get("now", True)))
                return self.send_json(campaign_view(q("SELECT * FROM campaigns WHERE id=?", m[1], one=True)))

            m = re.fullmatch(r"/api/campaigns/([0-9a-f]+)/pause", p)
            if m:   # {"source_id": one of its climbs, "paused": false to resume it}
                if not campaign_status(m[1]):
                    return self.send_json({"error": "not found"}, 404)
                b = self.read_json()
                try:
                    pause_item(m[1], str(b.get("source_id")), b.get("paused", True) is not False)
                except KeyError:
                    return self.send_json({"error": "no such climb in this area"}, 404)
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 409)
                return self.send_json(campaign_view(q("SELECT * FROM campaigns WHERE id=?", m[1], one=True)))
            m = re.fullmatch(r"/api/campaigns/([0-9a-f]+)/retry", p)
            if m:
                if not campaign_status(m[1]):
                    return self.send_json({"error": "not found"}, 404)
                queued = retry_campaign(m[1])
                return self.send_json({**campaign_view(q("SELECT * FROM campaigns WHERE id=?", m[1], one=True)), "queued": queued})
            m = re.fullmatch(r"/api/campaigns/([0-9a-f]+)/boulders", p)
            if m:   # its boulders with more than one climb, each meshed from every photo of its climbs (run_boulder())
                camp = q("SELECT * FROM campaigns WHERE id=?", m[1], one=True)
                if not camp:
                    return self.send_json({"error": "not found"}, 404)
                queued = queue_boulders(m[1])
                return self.send_json({**campaign_view(q("SELECT * FROM campaigns WHERE id=?", m[1], one=True)), "queued": queued})
            m = re.fullmatch(r"/api/campaigns/([0-9a-f]+)/(harvest-again|rebuild)", p)
            if m:
                if not campaign_status(m[1]):
                    return self.send_json({"error": "not found"}, 404)
                b = self.read_json()
                what = (("refilter" if b.get("refilter") else "new" if b.get("new_only") else "videos" if b.get("videos_only")
                         else "harvest") + ("+rebuild" if b.get("then_rebuild") else "")
                        if m[2] == "harvest-again" else "try-skipped" if b.get("skipped_only")
                        else ("rebuild-changed" if b.get("changed_only") else "rebuild"))
                queued = queue_redo(m[1], what)
                return self.send_json({**campaign_view(q("SELECT * FROM campaigns WHERE id=?", m[1], one=True)), "queued": queued})
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/beta", p)
            if m:
                mesh = q("SELECT source FROM meshes WHERE id=?", m[1], one=True)
                if not mesh:
                    return self.send_json({"error": "not found"}, 404)
                if not mesh["source"].startswith("project:"):
                    return self.send_json({"error": "only reconstructed meshes have videos to find beta in"}, 409)
                if not beta_mod.available():
                    return self.send_json({"error": "finding beta needs rtmlib and onnxruntime (in the Docker image)"}, 501)
                if not queue_beta(m[1], ahead=True):
                    return self.send_json({"error": "already finding it"}, 409)
                return self.send_json({"queued": True}, 202)
            if p == "/api/meshes/beta-all":     # every mesh with videos whose beta hasn't been found
                if not beta_mod.available():
                    return self.send_json({"error": "finding beta needs rtmlib and onnxruntime (in the Docker image)"}, 501)
                return self.send_json({"queued": sum(queue_beta(mid) for mid in beta_findable())}, 202)
            if p == "/api/meshes/beta-stop":    # the mesh it's on, and every one queued
                return self.send_json({"unqueued": stop_beta()})
            if p == "/api/meshes/scale-all":   # every reconstructed mesh without a scale, or {"again": true} all of them
                again = bool(self.read_json().get("again"))
                queued = sum(queue_scale(r["id"]) for r in q("SELECT id FROM meshes WHERE source LIKE 'project:%' AND replaced_by IS NULL ORDER BY created")
                             if again or not scale_mod.load(MESHES / r["id"]) or scale_mod.load(MESHES / r["id"])["status"] not in ("done", "nothing to go by"))
                return self.send_json({"queued": queued}, 202)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/scale", p)
            if m:
                mesh = q("SELECT source FROM meshes WHERE id=?", m[1], one=True)
                if not mesh:
                    return self.send_json({"error": "not found"}, 404)
                if not mesh["source"].startswith("project:") or not (MESHES / m[1] / "cameras.json").exists():
                    return self.send_json({"error": "only meshes reconstructed here, with their cameras kept, can be sized"}, 409)
                if not queue_scale(m[1]):
                    return self.send_json({"error": "already finding it"}, 409)
                return self.send_json({"queued": True}, 202)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/holds", p)
            if m:   # holds marked by hand: {holds: [{p, role}]}, replacing those there
                if not (MESHES / m[1]).is_dir():
                    return self.send_json({"error": "not found"}, 404)
                try:
                    return self.send_json(beta_mod.save_holds(MESHES / m[1], self.read_json().get("holds") or []))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/make-current", p)
            if m:
                if not q("SELECT replaced_by FROM meshes WHERE id=? AND replaced_by IS NOT NULL", m[1]):
                    return self.send_json({"error": "not an older version"}, 409)
                make_current(m[1])
                return self.send_json({"current": m[1]})

            if p == "/api/climbs/search":   # OpenBeta live search (not stored until imported)
                body = self.read_json()
                recs = climbs_mod.search(query=body.get("q") or "", lat=body.get("lat"), lng=body.get("lng"),
                                         radius_m=int(body.get("radius_m") or 2000))
                return self.send_json({"results": [c.to_dict() for c in recs]})

            if p == "/api/climbs/manual":
                b = self.read_json()
                if not b.get("name"):
                    return self.send_json({"error": "name required"}, 400)
                c = Climb(source="manual", source_id=new_id(), url=b.get("url") or "", name=b["name"][:120],
                          kind=b.get("kind") or "boulder", grade=b.get("grade"),
                          lat=float(b["lat"]) if b.get("lat") not in (None, "") else None,
                          lng=float(b["lng"]) if b.get("lng") not in (None, "") else None,
                          coord_source="manual" if b.get("lat") not in (None, "") else None,
                          boulder_name=b.get("boulder_name"), area_path=[a for a in (b.get("area_path") or []) if a],
                          description=b.get("description"))
                if b.get("video"):
                    c.videos = [climbs_mod.Media(url=b["video"], kind="video", source="manual")]
                if c.url and "kayaclimb.com" in c.url:
                    c.source = "kaya"
                return self.send_json(climb_row(upsert_climb(c)), 201)

            if p == "/api/climbs/kaya-export":   # user-provided CSV/JSON export
                fields, files = self.read_multipart()
                if not files:
                    return self.send_json({"error": "no file"}, 400)
                fname, data = files[0]
                text = data.decode("utf-8", "replace")
                rows = json.loads(text) if fname.lower().endswith(".json") else list(csv.DictReader(io.StringIO(text)))
                if isinstance(rows, dict):
                    rows = rows.get("climbs") or rows.get("ascents") or rows.get("data") or []
                ids = [upsert_climb(c) for c in kaya_mod.from_export(rows)]
                return self.send_json({"imported": [climb_row(i) for i in ids]}, 201)

            m = re.fullmatch(r"/api/climbs/([0-9a-f]+)/harvest", p)
            if m:
                cid = m[1]
                r = climb_row(cid)
                if not r:
                    return self.send_json({"error": "not found"}, 404)
                body = self.read_json()
                sources = [s for s in (body.get("sources") or HARVEST_SOURCES) if s in HARVEST_SOURCES]
                pid = new_id()
                (PROJECTS / pid / "photos").mkdir(parents=True)
                name = f"{r['name']} (harvest)"[:80]
                q("INSERT INTO projects(id,name,created,kind,climb_id,status) VALUES(?,?,?,?,?,'queued')",
                  pid, name, time.time(), "harvest", cid, write=True)
                queue_job({"type": "harvest", "pid": pid, "sources": sources, "reconstruct": bool(body.get("reconstruct", True)),
                          "filter": bool(body.get("filter", True))})
                return self.send_json({"project_id": pid, "queued": True}, 202)

            return self.send_json({"error": "not found"}, 404)
        except Exception as e:
            return self.send_error_json(e)

    # -- PATCH -------------------------------------------------------------
    def do_PATCH(self):
        p = urlparse(self.path).path
        if self.refused():
            return
        try:
            body = self.read_json()
            m = re.fullmatch(r"/api/climbs/([0-9a-f]+)", p)
            if m:
                allowed = {"name", "grade", "lat", "lng", "boulder_name", "kind", "url"}
                sets = {k: body[k] for k in allowed if k in body}
                if "lat" in sets or "lng" in sets:
                    sets["coord_source"] = "manual"
                if not sets:
                    return self.send_json({"error": "nothing to update"}, 400)
                q(f"UPDATE climbs SET {', '.join(k + '=?' for k in sets)} WHERE id=?", *sets.values(), m[1], write=True)
                return self.send_json(climb_row(m[1], full=True))
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/cleanup", p)
            if m:
                try:
                    with _cleanup_lock:
                        record = meshclean.set_removed(served_dir(m[1]), body.get("removed", []))
                    return self.send_json(cleanup_answer(m[1], record))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/heading", p)
            if m:   # turned by hand ({"deg": its -Z axis's compass bearing}), or {"deg": null} for what's worked out
                if not (MESHES / m[1]).is_dir():
                    return self.send_json({"error": "not found"}, 404)
                try:
                    record = heading_mod.set_edited(MESHES / m[1], body.get("deg"))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                if record.get("version") != heading_mod.VERSION:
                    keep_heading(m[1])
                return self.send_json({"heading": mesh_heading(m[1]), "record": heading_mod.load(MESHES / m[1])})
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)", p)
            if m:
                sets = {k: body[k] for k in ("name", "lat", "lng") if k in body}
                if not sets:
                    return self.send_json({"error": "nothing to update"}, 400)
                q(f"UPDATE meshes SET {', '.join(k + '=?' for k in sets)} WHERE id=?", *sets.values(), m[1], write=True)
                return self.send_json(q("SELECT * FROM meshes WHERE id=?", m[1], one=True))
            return self.send_json({"error": "not found"}, 404)
        except Exception as e:
            return self.send_error_json(e)

    # -- DELETE ------------------------------------------------------------
    def do_DELETE(self):
        p = urlparse(self.path).path
        if self.refused():
            return
        m = re.fullmatch(r"/api/campaigns/([0-9a-f]+)", p)
        if m:   # the list goes; meshes and projects it made stay
            stop_campaign(m[1])
            q("DELETE FROM campaign_items WHERE campaign_id=?", m[1], write=True)
            q("DELETE FROM campaigns WHERE id=?", m[1], write=True)
            return self.send_json({"deleted": m[1]})
        m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/cleanup", p)
        if m:   # the whole mesh shows again; its own files were never changed
            with _cleanup_lock:
                meshclean.delete(served_dir(m[1]))
            return self.send_json(cleanup_answer(m[1], {"deleted": True}))
        m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/links/([0-9a-f]+)", p)
        if m:
            q("DELETE FROM mesh_links WHERE mesh_id=? AND climb_id=?", m[1], m[2], write=True)
            return self.send_json({"unlinked": True})
        m = re.fullmatch(r"/api/meshes/([0-9a-f]+)", p)
        if m:   # an area's climb done with it is done with another current mesh of that climb (linked, or its boulder's)
            row = q("SELECT boulder FROM meshes WHERE id=?", m[1], one=True)
            if row and is_rest(row["boulder"]):
                rest_deleted(m[1], row["boulder"])
            for item in q("SELECT campaign_id, source_id, climb_id FROM campaign_items WHERE mesh_id=?", m[1]):
                other = (q("SELECT m.id, m.name FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id WHERE l.climb_id=? AND m.id!=? "
                           "AND m.replaced_by IS NULL ORDER BY m.created DESC LIMIT 1", item["climb_id"], m[1], one=True)
                         or q("SELECT m.id, m.name FROM meshes m JOIN climbs c ON m.boulder=c.area_path WHERE c.id=? AND m.id!=? "
                              "AND m.replaced_by IS NULL ORDER BY m.created DESC LIMIT 1", item["climb_id"], m[1], one=True))
                q("UPDATE campaign_items SET mesh_id=?, note=? WHERE campaign_id=? AND source_id=?", other and other["id"],
                  f"its mesh was deleted: done with {other['name']}" if other else "its mesh was deleted",
                  item["campaign_id"], item["source_id"], write=True)
            keep_gone(m[1])     # for its /m/ links
            q("DELETE FROM meshes WHERE id=?", m[1], write=True)
            q("DELETE FROM mesh_links WHERE mesh_id=?", m[1], write=True)
            q("DELETE FROM mesh_ratings WHERE mesh_id=?", m[1], write=True)
            shutil.rmtree(MESHES / m[1], ignore_errors=True)
            meshes_changed()
            return self.send_json({"deleted": m[1]})
        m = re.fullmatch(r"/api/projects/([0-9a-f]+)", p)
        if m:
            return self.send_json(*delete_project(m[1]))
        m = re.fullmatch(r"/api/climbs/([0-9a-f]+)", p)
        if m:
            q("DELETE FROM climbs WHERE id=?", m[1], write=True)
            q("DELETE FROM mesh_links WHERE climb_id=?", m[1], write=True)
            q("UPDATE projects SET climb_id=NULL WHERE climb_id=?", m[1], write=True)
            return self.send_json({"deleted": m[1]})
        return self.send_json({"error": "not found"}, 404)


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "36184"))


def admin_link() -> dict:
    """Where an editor's browser may find the admin page, and how to reach it over SSH from elsewhere, for the main page's
    Admin button: addresses and a command, nothing secret.

    The address is where it's published: ADMIN_BIND and ADMIN_PUBLISHED_PORT, which docker-compose.yml passes on, or else
    ADMIN_HOST and ADMIN_PORT; ADMIN_URL replaces it. ADMIN_SSH (as "you@203.0.113.7", or a name in ~/.ssh/config) goes in
    the command, as a server behind a proxy such as Cloudflare can't be reached over SSH by its site's name.
    """
    in_docker = Path("/.dockerenv").exists()
    host = (os.environ.get("ADMIN_BIND") if in_docker else os.environ.get("ADMIN_HOST")) or "127.0.0.1"
    port = int(os.environ.get("ADMIN_PUBLISHED_PORT") or ADMIN_PORT)
    loopback = host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::")
    remote = "127.0.0.1" if loopback else host
    ssh = os.environ.get("ADMIN_SSH")
    return {"url": os.environ.get("ADMIN_URL") or f"http://{'localhost' if loopback else host}:{port}",
            "local": f"http://localhost:{port}", "lan": not loopback, "password": bool(os.environ.get("ADMIN_PASSWORD")),
            "ssh": f"ssh -N -L {port}:{remote}:{port} {ssh or '<you>@<this server>'}", "ssh_known": bool(ssh)}


# ----------------------------------------------------------------- the editing token, to a phone from a QR
# the main site's address, for the link in the admin page's QR that a phone's camera app opens
SITE_URL = (os.environ.get("SITE_URL") or (f"https://{os.environ['SITE_HOST']}" if os.environ.get("SITE_HOST") else "")).rstrip("/") or None
PAIR_SECONDS = 300
_pairing: dict = {"code": None, "until": 0.0, "given": None}
_pairing_lock = threading.Lock()


def admin_edit_pair(_: dict) -> dict:
    """A code that gives a browser the editing token once, in the next PAIR_SECONDS, for the admin page to show as a QR:
    a link to the main site, for a phone's camera app, which the Scan button by the main page's Edit… reads too. It's
    neither the token nor anything that lasts, as a QR is easily photographed, and a phone's history keeps the links it
    opened. A new code replaces the last, and none outlasts a restart."""
    if READ_ONLY or not EDIT_TOKEN:
        raise ValueError("the main site " + ("is view-only (READ_ONLY)" if READ_ONLY else "has no EDIT_TOKEN, so anyone who reaches it can change it")
                         + ": there's no editing token to give")
    code, now = secrets.token_urlsafe(16), time.time()
    with _pairing_lock:
        _pairing.update(code=code, until=now + PAIR_SECONDS)
    text = f"{SITE_URL}/#pair={code}" if SITE_URL else f"pair={code}"
    return {"qr": notify_mod.qr_svg(text), "text": text, "link": bool(SITE_URL), "seconds": PAIR_SECONDS, "made": now}


def take_pairing(code, who: str) -> bool:
    """Whether code is the admin page's latest pairing code (admin_edit_pair()), still in time; it then works no more."""
    with _pairing_lock:
        ok = (isinstance(code, str) and bool(_pairing["code"]) and time.time() < _pairing["until"]
              and hmac.compare_digest(code.encode(), _pairing["code"].encode()))
        if ok:
            _pairing.update(code=None, given={"at": time.time(), "to": who})
        return ok


def pairing_state() -> dict:
    """For the admin page: whether there's a token to give, whether its last code is waiting, and who used one last."""
    with _pairing_lock:
        return {"editing": "none" if READ_ONLY else "token" if EDIT_TOKEN else "open", "site_url": SITE_URL,
                "waiting": bool(_pairing["code"]) and time.time() < _pairing["until"], "given": _pairing["given"]}


# ----------------------------------------------------------------- links: /c/<climb>, /b/<boulder>, /m/<mesh id>
SYNC_EVERY = 30         # seconds between giving new climbs and boulders their links' names (links_mod.sync()), at most
_links: dict = {"at": 0.0}
_links_lock = threading.Lock()


def boulder_mesh_sets(rows: list[dict] | None = None, kinds: dict | None = None) -> dict[str, list[str]]:
    """The current meshes of each lowest area meshed as one boulder (pools_whole()): its own (run_boulder()), those of
    what they left out (run_rest()), and its climbs' own, newest first. {leaf: [mesh id]}

    Args:
        rows: the current meshes, {id, boulder, created}, if the caller has them.
    """
    rows = rows if rows is not None else q("SELECT id, boulder, created FROM meshes WHERE replaced_by IS NULL")
    kinds = kinds if kinds is not None else boulder_kinds()
    own: dict[str, set] = {}
    for r in q("SELECT l.mesh_id, c.area_path FROM mesh_links l JOIN climbs c ON c.id=l.climb_id "
               "WHERE COALESCE(l.note,'') NOT LIKE ?", SHARED + "%"):
        own.setdefault(r["mesh_id"], set()).add(boulder_of(r["area_path"]))     # None for a climb with no area
    sets: dict[str, list[tuple[float, str]]] = {}
    for r in rows:
        key = r.get("boulder")
        if key:
            leaf = pool_leaf(key) if is_rest(key) else None if is_pool(key) else key
        else:
            leaf = next(iter(own[r["id"]])) if len(own.get(r["id"], ())) == 1 else None
        if leaf and pools_whole(leaf, kinds):
            sets.setdefault(leaf, []).append((r.get("created") or 0, r["id"]))
    return {leaf: [mid for _, mid in sorted(ms, reverse=True)] for leaf, ms in sets.items()}


def sync_links(force: bool = False) -> None:
    """Give climbs, and boulders with a mesh, the names in their links (links_mod.sync()), at most every SYNC_EVERY
    seconds unless forced: the first added with a name gets it plain."""
    with _links_lock:
        if not force and time.time() - _links["at"] < SYNC_EVERY:
            return
        _links["at"] = time.time()
        climbs = [{"ref": c["id"], "name": c["name"], "more": [c["boulder_name"], (c["grade"] or "").split(" ")[0]]}
                  for c in q("SELECT id, name, boulder_name, grade FROM climbs ORDER BY created, id")]
        with _db_lock, db() as con:
            known = {r[0]: r[1] for r in con.execute("SELECT leaf, ref FROM slugs WHERE kind='b' AND current=1")}
        first = {r["area_path"]: r["t"] for r in q("SELECT area_path, MIN(created) t FROM climbs GROUP BY area_path")}
        urls: dict = {}
        boulders = []
        for leaf in sorted(boulder_mesh_sets(), key=lambda k: (first.get(k) or 0, k)):
            path = json.loads(leaf)
            boulders.append({"ref": known.get(leaf) or links_mod.boulder_ref(leaf, leaf_url(leaf, urls)),
                             "name": boulder_name(leaf), "more": [str(a).strip("* ") for a in path[-2:-4:-1]], "leaf": leaf})
        with _db_lock, db() as con:
            links_mod.sync(con, "c", climbs)
            links_mod.sync(con, "b", boulders)


def startup_links() -> None:
    """Name the climbs and boulders added since the last run (sync_links()), as the server starts."""
    try:
        sync_links(force=True)
    except Exception as e:
        print(f"error: naming links: {type(e).__name__}: {e}", flush=True)


def link_names(force: bool = False) -> tuple[dict[str, str], dict[str, str]]:
    """The current names in climbs' and boulders' links: ({climb id: name}, {boulder key: name})."""
    sync_links(force)
    with _db_lock, db() as con:
        return links_mod.current(con, "c"), links_mod.current(con, "b")


def find_link(kind: str, slug: str) -> dict | None:
    """What /c/<slug> or /b/<slug> is of ({slug, ref, leaf}, its current name in slug), giving new climbs and boulders
    their names first if it's no one's yet."""
    for force in (False, True):
        if force:
            sync_links(force=True)
        with _db_lock, db() as con:
                found = links_mod.resolve(con, kind, slug)
        if found:
            return found
    return None


def same_named(cid: str, name: str) -> list[dict]:
    """The other climbs whose names make the same link name as this one's, with their own links, for its page to say
    so: {slug, name, grade, boulder}."""
    want, names = links_mod.slugify(name), link_names()[0]
    return [{"slug": names[c["id"]], "name": c["name"], "grade": c["grade"], "boulder": c["boulder_name"]}
            for c in q("SELECT id, name, grade, boulder_name FROM climbs WHERE id != ? AND lower(name) LIKE ?", cid,
                       "%" + "%".join(want.split("-")) + "%")
            if links_mod.slugify(c["name"]) == want and c["id"] in names][:5]


def boulder_choices(leaf: str, slug: str) -> dict:
    """What /b/<slug> offers: the boulder's current meshes (boulder_mesh_sets()), most of its climbs first, each with the
    climbs it's the home of (home: placing more of their own photos than its other meshes do, or made for them) and
    others it shows; and its climbs no mesh places, with why where that's known."""
    ids = boulder_mesh_sets().get(leaf, [])
    rows = q(f"SELECT id, boulder, created FROM meshes WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", *ids)
    views = {r["id"]: viewpoints.load(MESHES / r["id"]) or {} for r in rows if r["boulder"]}
    homes = boulder_homes(rows, views)
    linked: dict[str, list[dict]] = {}
    for r in q(f"SELECT mesh_id, climb_id, note FROM mesh_links WHERE mesh_id IN ({','.join('?' * len(ids)) or 'NULL'})", *ids):
        linked.setdefault(r["mesh_id"], []).append(r)
    climbs = boulder_climbs(leaf)
    on, out, unplaced = {c["id"] for c in climbs}, [], {}
    for r in rows:
        if r["boulder"]:
            v = views[r["id"]]
            seen = {cid: (x or {}).get("photos") or 0 for cid, x in (v.get("climbs") or {}).items() if cid in on}
            home = [cid for cid, n in seen.items() if n and homes.get(cid, r["id"]) == r["id"]]
            others = [cid for cid, n in seen.items() if n and cid not in home]
            for cid, n in (v.get("unplaced") or {}).items():
                unplaced[cid] = max(unplaced.get(cid, 0), n or 0)
        else:
            home = [x["climb_id"] for x in linked.get(r["id"], []) if not (x["note"] or "").startswith(SHARED)]
            others = [x["climb_id"] for x in linked.get(r["id"], []) if (x["note"] or "").startswith(SHARED)]
        metrics = meshmetrics.load(MESHES / r["id"]) or {}
        out.append({"id": r["id"], "home": home, "others": others, "placed": metrics.get("placed") or 0,
                    "faces": metrics.get("faces"), "created": r["created"]})
    out.sort(key=lambda m: (-len(m["home"]), -m["placed"], -(m["created"] or 0)))
    shown = {cid for m in out for cid in m["home"] + m["others"]}
    missing = [{"id": c["id"], "why": f"{plural(unplaced[c['id']], 'photo')} of its own tried, none placed"
                if unplaced.get(c["id"]) else None} for c in climbs if c["id"] not in shown]
    path = json.loads(leaf)
    return {"slug": slug, "key": leaf, "name": boulder_name(leaf), "area": [str(a).strip("* ") for a in path[:-1]][-2:],
            "url": leaf_url(leaf, {}), "meshes": out, "missing": missing,
            "climbs": [{"id": c["id"], "name": c["name"], "grade": c["grade"]} for c in climbs]}


def gone_mesh(mid: str) -> dict | None:
    """A deleted mesh, for its /m/ links (links_mod.gone()), with the name of its boulder's link if it was one's."""
    with _db_lock, db() as con:
        g = links_mod.gone(con, mid)
    if g and g["leaf"]:
        g["boulder_slug"] = link_names()[1].get(g["leaf"])
    return g


def keep_gone(mid: str) -> None:
    """Before a mesh is deleted: what its /m/ links open instead (links_mod.mesh_gone())."""
    m = q("SELECT name, boulder, replaced_by FROM meshes WHERE id=?", mid, one=True)
    if not m:
        return
    climbs = [r["climb_id"] for r in q("SELECT climb_id, note FROM mesh_links WHERE mesh_id=?", mid)
              if not (r["note"] or "").startswith(SHARED)]
    leaf = next((leaf for leaf, ids in boulder_mesh_sets(q("SELECT id, boulder, created FROM meshes WHERE id=?", mid)).items()
                 if mid in ids), None)     # the boulder it's one of the meshes of
    with _db_lock, db() as con:
        links_mod.mesh_gone(con, mid, m["name"], m["replaced_by"], climbs, leaf)
        con.execute("UPDATE meshes_gone SET successor=? WHERE successor=?", (m["replaced_by"], mid))


def climb_mesh(cid: str) -> str | None:
    """The mesh a climb's link opens, as the page picks it (pickClimbMesh()): its own (not one of what its boulder's
    left out), else the current one of its boulder's or pool's placing most of its own photos, else one an area linked it
    to; None if none."""
    own = [r for r in q("SELECT m.id, m.boulder FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id WHERE l.climb_id=? AND "
                        "COALESCE(l.note,'') NOT LIKE ? ORDER BY m.replaced_by IS NOT NULL, m.created DESC", cid, SHARED + "%")
           if not is_rest(r["boulder"])]
    if own:
        return own[0]["id"]
    c = q("SELECT area_path FROM climbs WHERE id=?", cid, one=True)
    leaf = boulder_of(c["area_path"]) if c else None
    best = None
    for r in q("SELECT id, boulder, created FROM meshes WHERE replaced_by IS NULL AND (boulder=? OR boulder LIKE ?)",
               leaf, (leaf or "")[:-1] + ", %") if leaf else []:
        if r["boulder"] != leaf and not (is_pool(r["boulder"]) and cid in pool_members(r["boulder"])):
            continue
        n = ((viewpoints.load(MESHES / r["id"]) or {}).get("climbs") or {}).get(cid) or {}
        best = max(best or (-1, 0, ""), (n.get("photos") or 0, r["created"] or 0, r["id"]))
    if best:
        return best[2]
    shared = q("SELECT m.id FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id WHERE l.climb_id=? AND l.note LIKE ? "
               "ORDER BY m.replaced_by IS NOT NULL, m.created DESC LIMIT 1", cid, SHARED + "%", one=True)
    return shared["id"] if shared else None


# ----------------------------------------------------------------- pictures of meshes (thumbs.py)
_thumbs: "queue.PriorityQueue[tuple[int, float, str]]" = queue.PriorityQueue()
_thumb_lock = threading.Lock()
_thumbing: dict[str, int] = {}      # meshes queued, and how soon: 0 asked for by an editor, 1 new or out of date
_renderer: dict = {}


def thumb_site() -> str:
    """Where the browser drawing pictures opens the site: this server, as it listens (THUMB_SITE says otherwise)."""
    host = os.environ.get("HOST", "0.0.0.0")
    host = "127.0.0.1" if host in ("0.0.0.0", "::", "localhost", "") else host
    return os.environ.get("THUMB_SITE") or f"http://{host}:{int(os.environ.get('PORT', '8470'))}"


def thumb_now(mid: str, mode: str | None) -> dict | None:
    """A mesh's picture as its card and links may show it: {v, hand}, or None if it hasn't one drawn as its texture is
    shown now, which would show what the site no longer does."""
    d = MESHES / mid
    rec = thumbs_mod.load(d)
    if not rec.get("v") or rec.get("mode") != mode or not (d / thumbs_mod.DIR / thumbs_mod.LARGE).is_file():
        return None
    return {"v": rec["v"], "hand": bool(rec.get("pose"))}


def thumb_info(r: dict) -> dict | None:
    """A listed mesh's picture (thumb_now()); one missing or out of date is queued, once the mesh is packed for viewing,
    as it's drawn from its preview. Older versions of rebuilt meshes keep what they had."""
    d = MESHES / r["id"]
    if thumbs_mod.available() and not r.get("replaced_by") and (r.get("pack") or not r["filename"].lower().endswith(".glb")):
        rec = thumbs_mod.load(d)
        want = thumbs_mod.key(r.get("v"), d, rec.get("pose"))
        if rec.get("key") != want and (rec.get("failed_key") != want or time.time() - (rec.get("failed_at") or 0) > thumbs_mod.RETRY):
            queue_thumb(r["id"])     # one that failed, again after a while: most fail as the server's too busy
    return thumb_now(r["id"], r.get("texture"))


def queue_thumb(mid: str, urgent: bool = False) -> None:
    soon = 0 if urgent else 1
    with _thumb_lock:
        if _thumbing.get(mid, 2) <= soon:
            return
        _thumbing[mid] = soon
    _thumbs.put((soon, time.time(), mid))


def thumb_pose(mid: str) -> dict | None:
    """Where a mesh's picture is drawn from by itself: from its own climb's view, on a boulder's mesh, else the view of
    its climb with most photos placed (views.json), else the view from all its photos (viewpoints.photos_view()); None
    for one with none of those, which the viewer frames as it opens it."""
    d = MESHES / mid
    climbs = (viewpoints.load(d) or {}).get("climbs") or {}
    if climbs:
        own = [r["climb_id"] for r in q("SELECT climb_id FROM mesh_links WHERE mesh_id=? AND COALESCE(note,'') NOT LIKE ?",
                                        mid, SHARED + "%")]
        v = climbs[own[0]] if len(own) == 1 and own[0] in climbs else max(climbs.values(), key=lambda v: v.get("photos") or 0)
        return {"eye": v["eye"], "target": v["target"]}
    return viewpoints.photos_view(d)


def draw_thumb(mid: str) -> bool:
    """Draw a mesh's picture (thumbs_mod.Renderer), unless it's drawn already from what it would be now. Whether it
    drew one; a failure is kept, with what it was drawn from and when, so as not to try that again for RETRY seconds."""
    m = q("SELECT source, filename FROM meshes WHERE id=?", mid, one=True)
    if not m:
        return False
    d = MESHES / mid
    mode = texture_mode(mid, m["source"])
    rec = thumbs_mod.load(d)
    pose = rec.get("pose")
    want = thumbs_mod.key(mesh_version(mid, mode, m["filename"]), d, pose)
    if rec.get("key") == want:
        return False
    out = d / thumbs_mod.DIR
    out.mkdir(exist_ok=True)
    tmp = out / "drawing.jpg"
    if "r" not in _renderer:
        _renderer["r"] = thumbs_mod.Renderer(BETA_NICE)
    try:
        camera = pose or thumb_pose(mid)
        took = _renderer["r"].draw(f"{thumb_site()}/m/{mid}?thumb={quote(json.dumps(camera) if camera else 'auto')}"
                                   + ("" if pose else "&fit=1"), tmp)
        thumbs_mod.shrink(tmp, out / thumbs_mod.SMALL_FILE)
        tmp.replace(out / thumbs_mod.LARGE)
    except Exception as e:
        print(f"{mid}: its picture couldn't be drawn: {type(e).__name__}: {e}", flush=True)
        rec = thumbs_mod.load(d)
        rec.update(failed=str(e), failed_key=want, failed_at=time.time())
        thumbs_mod.save(d, rec)
        return False
    rec = thumbs_mod.load(d)    # a camera chosen meanwhile stays, and draws it again
    rec.update(key=want, mode=mode, v=want, made=time.time(), seconds=round(took, 1), failed=None, failed_key=None)
    thumbs_mod.save(d, rec)
    if rec.get("pose") != pose:
        queue_thumb(mid, urgent=True)
    return True


def thumb_worker() -> None:
    """Draws meshes' pictures one at a time, on a thread of its own at the lowest CPU priority, as the browser does."""
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), BETA_NICE)
    while True:
        _, _, mid = _thumbs.get()
        with _thumb_lock:
            if _thumbing.pop(mid, None) is None:
                continue
        use_processing_cores()
        try:
            draw_thumb(mid)
        except Exception as e:  # never let the thread die
            print(f"{mid}: drawing its picture failed: {type(e).__name__}: {e}", flush=True)


# ----------------------------------------------------------------- volunteers
def search_wanter(purpose: str, ref: str):
    """What to ask volunteers for a YouTube search with, for purpose and ref, when this server won't search itself;
    None if there's no volunteer, or the admin page says to ask YouTube only here."""
    if settings().get("youtube.via") == "here" or not VOLUNTEERS.any():
        return None
    return lambda words: VOLUNTEERS.want("search", words, purpose, ref)


def video_wanter(purpose: str, ref: str):
    """What to ask volunteers for a video with, for purpose ("harvest" or "beta") and ref (its project or mesh), when
    YouTube won't let this server have it; None if there's no volunteer, or the admin page says to fetch only here."""
    if settings().get("youtube.via") == "here" or not VOLUNTEERS.any():
        return None
    return lambda url: VOLUNTEERS.want("video", youtube_mod.video_id(url) or "", purpose, ref)


def volunteer_delivered(task: dict, path: Path | None, info: dict) -> None:
    """What a volunteer fetched: a video into youtube's cache, and its details where the harvest would have kept
    them (every URL of the videos table naming it); a search's results into youtube's searches; a channel's
    description for every URL the videos table has for it."""
    if task["kind"] == "search":
        youtube_mod.save_search(info["query"], info["results"], "volunteer")
        return
    if task["kind"] == "channel":
        urls = {r["channel_url"] for r in q("SELECT DISTINCT channel_url FROM videos WHERE channel_id=? AND channel_url IS NOT NULL",
                                              info["channel_id"])}
        for url in urls or {info["url"]}:
            save_channel(url, {"channel_id": info["channel_id"], "name": info["name"], "description": info["description"]})
        return
    info = youtube_mod.details({**info, "webpage_url": info["url"], "extractor": "youtube"})
    if path:
        youtube_mod.cache_put(task["video_id"], path, info)
    urls = [r["url"] for r in q("SELECT url FROM videos WHERE url LIKE ?", f"%{task['video_id']}%")
            if youtube_mod.video_id(r["url"]) == task["video_id"]]
    for url in urls or [info["url"]]:
        save_video({**info, "url": url})


def volunteer_ready(purpose: str, ref: str) -> bool | None:
    """Everything a harvest or a mesh's beta wanted of volunteers has come or failed: carry on with it. False if not now
    (the harvest still running), which the broker keeps to give again once the climb waits (wait_for_volunteers())."""
    if purpose == "harvest":
        return reharvest_with_videos(ref)
    if purpose == "beta":
        _beta_wake.set()
    return None


def reharvest_with_videos(pid: str) -> bool:
    """Harvest a project again with the videos volunteers fetched for it, now in the cache. An area's climb is queued
    to harvest again and rebuild (if its photos changed), and a finished area runs only that; a stopped one waits
    to be started. Any other project is harvested again, to be rebuilt by hand. False, doing nothing, while the
    project is queued or running: what came may have been too late for the harvest under way."""
    proj = q("SELECT status FROM projects WHERE id=?", pid, one=True)
    if not proj:
        return True
    if proj["status"] in ("queued", "running", "harvesting"):
        return False
    item = q("SELECT campaign_id, source_id, status FROM campaign_items WHERE project_id=? LIMIT 1", pid, one=True)
    if item:
        if item["status"] == "waiting":
            set_item(item["campaign_id"], item["source_id"], status="pending", retry=1, redo="more+rebuild", was="waiting",
                     redo_started=0, note="volunteers have fetched what it waited for: adding it to its photos, then meshing it")
        elif item["status"] == "harvested":     # harvested for its boulder's mesh, which isn't made yet: added to first
            set_item(item["campaign_id"], item["source_id"], status="pending", retry=1, redo="more", was="harvested",
                     redo_started=0, note="volunteers have fetched more for it: adding it to its photos for its boulder's mesh")
        elif item["status"] in ("done", "skipped", "failed"):
            set_item(item["campaign_id"], item["source_id"], status="pending", retry=1, redo="more+rebuild", was=item["status"],
                     redo_started=0, note="queued to add the videos volunteers fetched to its photos")
            q("UPDATE campaigns SET status='running', only_retries=1, note=NULL WHERE id=? AND status='done'", item["campaign_id"], write=True)
        return True
    append_log(pid, "volunteers fetched the videos this harvest couldn't have: adding them to its photos (Reconstruct uses them)")
    q("UPDATE projects SET status='queued', progress=NULL WHERE id=?", pid, write=True)
    queue_job({"type": "harvest", "pid": pid, "sources": sorted(HARVEST_SOURCES), "reconstruct": False, "again": True, "add": True})
    return True


def volunteer_rules() -> dict:
    """The admin page's limits for volunteers (volunteers.RULES)."""
    s = settings().all()
    return {"per_hour": s["volunteers.per_hour"], "per_day": s["volunteers.per_day"],
            "provisional_per_hour": s["volunteers.provisional_per_hour"], "provisional_per_day": s["volunteers.provisional_per_day"],
            "provisional_work": s["volunteers.provisional_work"], "registrations_per_address": s["registration.per_address_day"],
            "max_waiting": s["registration.max_waiting"]}


def volunteer_troubled(v: dict, was: str | None, now: str, why: str) -> None:
    """A volunteer's script said YouTube refuses, throttles or degrades it, and it didn't before: on your phone, if
    notifications are on (notify.py), in the background."""
    then = {"refused": "It rests 1, 2, 4, then 8 hours, while YouTube still refuses it.", "throttled": "It rests an hour.",
            "degraded": "It carries on. A newer yt-dlp usually fixes this; volunteers update theirs daily."}[now]
    threading.Thread(target=NOTIFIER.send, args=(f"Volunteer {v['name']}: YouTube {now} it", f"{why}. {then}"),
                     kwargs={"priority": 3, "tags": ["warning"]}, daemon=True).start()


def volunteers_widespread(n: int, of: int, example: str) -> None:
    """Many volunteers say YouTube degrades them at once (volunteers.WIDESPREAD): likely YouTube changed something."""
    threading.Thread(target=NOTIFIER.send, args=(
        "YouTube is degrading several volunteers", f"{n} of the {of} volunteers heard from in the last few hours say so, as: "
        f"{example}. That's likely YouTube changing something, which a newer yt-dlp usually fixes (volunteers update theirs "
        "daily), rather than one address's trouble."), kwargs={"priority": 4, "tags": ["warning"]}, daemon=True).start()


VOLUNTEERS = volunteers_mod.Broker(DATA / "volunteers.sqlite", DATA / "volunteers", volunteer_delivered, volunteer_ready,
                                   paused=lambda: full(), rules=volunteer_rules,
                                   registered=lambda v: NOTIFIER.registered("volunteer", v, f" ({v['client'] or 'a volunteer script'})"),
                                   troubled=volunteer_troubled, widespread=volunteers_widespread, came_went=came_went("volunteer"),
                                   downloaded=script_downloaded("volunteer"), leaderboard=lambda: leaderboard())


def notify_act(verb: str, kind: str, rid: str) -> str:
    """What a phone's reply to a notification asks (notify.py): approve or deny a volunteer or a machine, or the test's ping."""
    if verb == "ping":
        return "the button reached the server"
    found = VOLUNTEERS.get(rid) if kind == "volunteer" else MACHINES.get(rid) if kind == "machine" else None
    if not found:
        return f"no {kind} {rid} here any more"
    if verb == "approve":
        if found.get("revoked"):
            return f"{found['name']} was already turned down or revoked: approve it on the admin page instead, if anything"
        (VOLUNTEERS if kind == "volunteer" else MACHINES).approve(rid)
        return f"approved {found['name']}"
    if verb == "deny":
        if found.get("status") != "provisional" or found.get("revoked"):
            return f"{found['name']} was already {found.get('status') or 'approved'}: revoke it on the admin page, if you mean to"
        (VOLUNTEERS if kind == "volunteer" else MACHINES).revoke(rid, "denied")
        return f"turned down {found['name']}"
    return f"nothing to do for {verb}"


NOTIFIER = notify_mod.Notifier(lambda: settings().all(), DATA / "notify.key", DATA / "notify.state", notify_act,
                               lambda line: print(line, flush=True))


def want_skipped() -> dict:
    """Ask volunteers for every video a harvest went without (of the 6 it takes), and every video a mesh queued to
    find beta in is waiting for; how many of each are coming."""
    n = {"harvest": 0, "beta": 0}
    for r in q("SELECT id, harvest FROM projects WHERE harvest IS NOT NULL"):
        summary = json.loads(r["harvest"])
        for words in [*((summary.get("asked") or {}).get("searches") or []), *([summary["unsearched"]] if summary.get("unsearched") else [])]:
            if not youtube_mod.cached_search(words):    # searches asked for that never came, or never asked, as with no volunteers
                n["harvest"] += VOLUNTEERS.want("search", words, "harvest", r["id"])
        for v in (json.loads(r["harvest"]).get("videos") or [])[:6]:
            vid = youtube_mod.video_id(v.get("url") or "")
            if vid and not v.get("frames") and not youtube_mod.cached(vid):
                n["harvest"] += VOLUNTEERS.want("video", vid, "harvest", r["id"])
    for mid in beta_queue():
        rec = beta_mod.load(MESHES / mid) or {}
        done = {v["url"] for v in rec.get("videos", [])}
        for url in beta_mod.videos(MESHES / mid):
            vid = youtube_mod.video_id(url)
            if vid and url not in done and not youtube_mod.cached(vid):
                n["beta"] += VOLUNTEERS.want("video", vid, "beta", mid)
    return n


def admin_volunteer_add(body: dict) -> dict:
    v = VOLUNTEERS.add(body.get("name") or "", body.get("per_hour"), body.get("per_day"))
    url = VOLUNTEER_URL or "https://<this site>"
    return {**v, "result": f"added {v['name']}: give them the command below, which has their token (shown only now)",
            "command": f"python3 bouldermesh_volunteer.py --server {url} --token {v['token']}",
            "download": f"{url}/volunteer/bouldermesh_volunteer.py"}


def admin_volunteer_limits(body: dict) -> dict:
    """A volunteer's own limits ({id, per_hour, per_day}), or null for either: the site's."""
    v = VOLUNTEERS.set_limits(str(body.get("id") or ""), body.get("per_hour"), body.get("per_day"))
    if not v:
        raise ValueError("no such volunteer")
    own = [f"{n} {what}" for n, what in ((v["per_hour"], "an hour"), (v["per_day"], "a day")) if n]
    return {"result": f"{v['name']}: at most {' and '.join(own)}, within the site's" if own else f"{v['name']}: the site's limits"}


def admin_volunteer_revoke(body: dict) -> dict:
    VOLUNTEERS.revoke(str(body.get("id") or ""))
    return {"result": "revoked: its token no longer works, and what it was fetching goes to someone else"}


def admin_volunteer_approve(body: dict) -> dict:
    v = VOLUNTEERS.approve(str(body.get("id") or ""))
    if not v:
        raise ValueError("no such volunteer waiting")
    return {"result": f"approved {v['name']}: their limits are everyone's now"}


def admin_volunteer_deny(body: dict) -> dict:
    VOLUNTEERS.revoke(str(body.get("id") or ""), "denied")
    return {"result": "turned down: their token no longer works"}


def admin_notify_topics(_: dict) -> dict:
    """New topics for notifications (and a new signing key): the phone has to subscribe to the new one."""
    settings().update({**NOTIFIER.new_topics(), "notify.enabled": True})
    return {"result": "new topics made: subscribe to the one shown in the ntfy app (the old one gets nothing more)"}


def admin_notify_test(_: dict) -> dict:
    return {"result": NOTIFIER.test()}


def admin_volunteers_skipped(_: dict) -> dict:
    n = want_skipped()
    return {"result": f"asked volunteers for {n['harvest']} video(s) and search(es) harvests went without and {n['beta']} video(s) for finding beta; "
                      "each climb is harvested again once its videos have come"}


def admin_volunteers_details(_: dict) -> dict:
    """Videos whose licence and uploader were never read (before that was kept): their details, without the video."""
    n = 0
    for r in q("SELECT url FROM videos WHERE license IS NULL"):
        vid = youtube_mod.video_id(r["url"])
        n += bool(vid) and VOLUNTEERS.want("details", vid, "details", r["url"])
    return {"result": f"asked volunteers for the details of {n} video(s)"}


def admin_volunteers_cancel(_: dict) -> dict:
    return {"result": f"dropped {VOLUNTEERS.cancel()} queued; what's being fetched carries on"}


def apply_settings() -> None:
    """What changing the admin page's settings changes at once."""
    youtube_mod.via = settings().get("youtube.via")
    relevance_mod.MATCHER = settings().get("relevance.matcher")
    relevance_mod.RUNNER = filter_anywhere      # on a machine when one can (relevance.machines), else here
    configure_workers()
    NOTIFIER.reconfigured()
    threading.Thread(target=skyline_existing_meshes, daemon=True).start()


BACKUP_STALE_HOURS = 36     # the backup copies each night: a good copy older than this means it has stopped


def backup_state() -> dict:
    """What the backup has told this server through its handshake (deploy/backup-handshake.py), for the admin page:
    when its last good copy began (data/backed-up), its last report of a night's copy, good or not (data/backup-report.json:
    its size, why it failed, and what it keeps by night), and how old a good copy may be before it's out of date."""
    try:
        confirmed = float((DATA / offload_mod.MARK).read_text().strip())
    except (OSError, ValueError):
        confirmed = None
    try:
        report = json.loads((DATA / "backup-report.json").read_text())
    except (OSError, ValueError):
        report = None
    return {"confirmed": confirmed, "report": report, "stale_hours": BACKUP_STALE_HOURS}


def admin_state() -> dict:
    """What the admin page shows: settings, which credentials are set, the workers, and how many meshes
    each texture setting would affect."""
    counts = dict.fromkeys(("meshes", "all_permitted", "variant", "hidden_if_permitted", "can_retexture", "need_rebuild"), 0)
    for m in q("SELECT id, source FROM meshes WHERE replaced_by IS NULL"):
        d, upload = MESHES / m["id"], m["source"] == "upload"
        ok, variant = upload or bool(policy.mesh_permitted(mesh_record(d))), retexture.has_variant(d)
        counts["meshes"] += 1
        counts["all_permitted"] += ok
        counts["variant"] += variant and not ok
        counts["hidden_if_permitted"] += not ok and not variant
        if not ok and m["source"].startswith("project:"):
            found, why = retexture.usable(d, PROJECTS / m["source"].split(":", 1)[1] / "photos", video_info)
            counts["can_retexture"] += bool(found)
            counts["need_rebuild"] += bool(why and "rebuild" in why)
    return {"settings": settings().all(), "secrets": settings().secrets_status(), "compute": compute_state(),
            "policy": counts, "retexture_queued": len(_retexturing), "site_port": int(os.environ.get("PORT", "8470")),
            "proxy_ranges": RANGES.state() if RANGES else None, "version": VERSION,
            "worker_image": compute_settings()["compute.image"], "own_image": OWN_IMAGE, "machines_image": MACHINES_IMAGE,
            "volunteers": {**VOLUNTEERS.state(), "served": bool(VOLUNTEER_PORT), "url": VOLUNTEER_URL},
            "notify": NOTIFIER.state(), "pairing": pairing_state(), "feedback": feedback_mod.recent(FEEDBACK),
            "usage": {**_usage, "cap": data_cap() or None, "full": full(), **room_left()}, "timings": timings_summary(),
            "packing": pack_progress(), "skyline": skyline_progress(), "rest_waiting": len(rest_waiting()), "untied_meshes": len(untied_meshes()), "old_meshes": old_mesh_report(), "boulder_shares": boulder_shares(), "cpu_count": len(os.sched_getaffinity(os.getpid())) if hasattr(os, "sched_getaffinity") else os.cpu_count(),
            "offload": offload_mod.state(DATA, q), "backup": backup_state(),
            "manifest": {**manifest_state(), "quiet": manifest_quiet(), "hours": MANIFEST_HOURS}, "lost_blocks": lost_blocks()}


def admin_test(_: dict) -> dict:
    if settings().get("compute.provider") == "pull":
        st = MACHINES.state()
        return {"result": f"{st['connected']} of {len([m for m in st['machines'] if m['status'] != 'revoked'])} machine(s) "
                          f"asked for work in the last {pullwork.CONNECTED // 60} minutes"
                          + ("" if WORKER_PORT else "; WORKER_PORT isn't set, so none can reach this server")}
    prov = compute.provider_for(compute_settings(), settings().secret, DATA / "compute", server_id())
    return {"result": prov.test() if prov else "nothing to test: this server does everything itself"}


def admin_end_nodes(_: dict) -> dict:
    ended = 0
    for pool in [_pool, *_old_pools]:
        if pool:
            ended += len([n for n in pool.nodes if not n.busy])
            pool.close()
    prov = compute.provider_for(compute_settings(), settings().secret, DATA / "compute", server_id())
    busy = {n.id for pool in [_pool, *_old_pools] if pool for n in pool.nodes}
    for iid in (prov.leftovers() if prov else []):
        if iid not in busy:
            prov.stop(compute.Node(host="", user="", id=iid))
            ended += 1
    return {"result": f"ended {ended} machine(s); any in use end when their climb is done"}


def admin_clear_rejected(_: dict) -> dict:
    """Delete the photos the filter rejected (projects' rejected/), of every project not being harvested or meshed:
    most of what harvests leave on disk, and needed only to put one back by hand."""
    freed, n = 0, 0
    for r in q("SELECT id, status FROM projects"):
        d = PROJECTS / r["id"] / "rejected"
        if r["status"] in ("queued", "running", "harvesting") or not d.is_dir():
            continue
        freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    if data_cap():
        measure_usage()
    return {"result": f"deleted the rejected photos of {n} project(s): {freed / 2**30:.1f} GB"}


def admin_measure(_: dict) -> dict:
    u = measure_usage()
    return {"result": f"the data takes {u['total'] / 2**30:.1f} GB" + (f" of {data_cap() / 2**30:g}" if data_cap() else "")}


def admin_lost_decide(body: dict) -> dict:
    """The admin page's answer for a job what the backup lost blocks: {verdict: remesh | leave, and campaign and source
    (an area's climb), or path}."""
    return {"result": lost_decide(str(body.get("verdict") or ""), body.get("campaign"), body.get("source"), body.get("path"))}


def admin_old_decide(body: dict) -> dict:
    """The admin page's answer for a pair of meshes old_by_data() couldn't tell: {mesh, newer, verdict: old | keep}."""
    try:
        got = decide_old(str(body.get("mesh") or ""), str(body.get("newer") or ""), str(body.get("verdict") or ""))
    except KeyError:
        raise ValueError("no such mesh")
    old_changed()
    return {"result": ("called old by data" if body.get("verdict") == "old" else "kept as it is") + f"; {len(got['decide'])} left to decide"}


def admin_remesh_untied(_: dict) -> dict:
    """Mesh again the boulders whose meshes were made before ties were recorded (remesh_untied()), in the background, as
    reading their areas' pages waits its turn with Mountain Project."""
    n = len(untied_meshes())
    if not n:
        return {"result": "every boulder's mesh was made with its scenes and ties recorded"}

    def run() -> None:
        got = remesh_untied()
        print(f"admin: meshing boulders again: {got['queued']} queued, {got['groups']} found to hold several boulders, "
              f"{got['busy']} being meshed already, {got['unread']} whose area's page couldn't be read", flush=True)
    threading.Thread(target=run, daemon=True, name="remesh-untied").start()
    return {"result": f"queuing {plural(n, 'boulder')} to mesh again (reading first what their areas are, where never read): "
                      f"each shows in Projects once queued"}


def admin_inputs_check(_: dict) -> dict:
    """Start the dry run of what each mesh would be made from now (inputs_check()), in the background; or, while one
    runs, say so. Either way, what the last one found."""
    last = _read_json(INPUTS_CHECK)
    said = (f"; the last, {time.strftime('%Y-%m-%d %H:%M', time.localtime(last['at']))}, found "
            + ", ".join(f"{n} {k}" for k, n in last["counts"].items()) + f" (in {INPUTS_CHECK})") if last else ""
    if _inputs_lock.locked():
        return {"result": "a check of what meshes would be made from now is running" + said}

    def run() -> None:
        got = inputs_check()
        print(f"admin: what meshes would be made from now: " + ", ".join(f"{n} {k}" for k, n in got["counts"].items())
              + f", in {got['took']} s", flush=True)
    threading.Thread(target=run, daemon=True, name="inputs-check").start()
    return {"result": f"checking what each mesh would be made from now, in the background: the report goes to {INPUTS_CHECK}" + said}


def admin_rest(_: dict) -> dict:
    """Queue meshing each scene tied to a current boulder's mesh that it left out, where that was never tried, or was
    cut short."""
    n = rest_queue_all()
    return {"result": f"queued {n} mesh{'' if n == 1 else 'es'} of what boulders' meshes left out" if n else "nothing left out to mesh"}


def admin_retexture(_: dict) -> dict:
    """Queue every mesh that can be textured from only its photos that are free to copy."""
    n = away = 0
    for m in q("SELECT id, source FROM meshes WHERE replaced_by IS NULL AND source LIKE 'project:%'"):
        d = MESHES / m["id"]
        if m["id"] in _retexturing or policy.mesh_permitted(mesh_record(d)):
            continue
        if photos_away(m["source"].split(":", 1)[1]):
            away += 1
            continue
        if retexture.usable(d, PROJECTS / m["source"].split(":", 1)[1] / "photos", video_info)[0]:
            _retexturing.add(m["id"])
            queue_job({"type": "retexture", "mid": m["id"]})
            n += 1
    return {"result": f"queued {n} mesh(es) to be textured from only the photos free to copy"
                      + (f"; {away} whose photos are in the backup weren't" if away else "")}


def main() -> None:
    began = time.time()
    init_db()
    with db() as con:
        links_mod.init(con)
    threading.Thread(target=startup_links, daemon=True, name="links").start()
    moved = settle_far_coordinates()
    if moved:
        print(f"{moved} climb(s) whose page's GPS was far from their area's now use their area's", flush=True)
    settled = settle_too_few_placed()
    if settled:
        print(f"{settled} area climb(s) that failed as COLMAP placed too few photos now count as skipped, as they would now", flush=True)
    apply_settings()
    if not diagnostics.allow_tracing():     # so that py-spy can attach to it, from a stall's dump or inside its container
        print("diagnostics: couldn't let py-spy attach to this process; a thread dump will only have Python's side", flush=True)
    threading.Thread(target=stall_watch, daemon=True, name="stall-watch").start()
    again = requeue_waiting()
    if again:
        print(f"{plural(again, 'job')} people asked for, still waiting when the server stopped, queued again", flush=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=reap_machines, daemon=True).start()
    threading.Thread(target=clean_existing_meshes, daemon=True).start()
    threading.Thread(target=orient_existing_meshes, daemon=True).start()
    threading.Thread(target=source_existing_meshes, daemon=True).start()
    threading.Thread(target=heading_existing_meshes, daemon=True).start()
    threading.Thread(target=skyline_worker, daemon=True).start()
    threading.Thread(target=adopt_old_pools, daemon=True).start()
    threading.Thread(target=rest_existing_meshes, daemon=True).start()
    threading.Thread(target=timings_existing_meshes, daemon=True).start()
    threading.Thread(target=inputs_existing_meshes, daemon=True, name="inputs-backfill").start()
    threading.Thread(target=backfill_step_runs, daemon=True).start()
    for d in MESHES.iterdir() if MESHES.exists() else []:
        beta_mod.carry_on(d)        # finding beta and size carry on after a restart, like areas
        if scale_mod.carry_on(d):
            queue_job({"type": "scale", "mid": d.name})
    threading.Thread(target=beta_worker, daemon=True).start()
    threading.Thread(target=watch_usage, daemon=True).start()
    threading.Thread(target=snapshot_worker, daemon=True).start()
    threading.Thread(target=frames_worker, daemon=True).start()
    threading.Thread(target=prune_worker, daemon=True).start()
    threading.Thread(target=pack_worker, daemon=True).start()
    if thumbs_mod.available():
        threading.Thread(target=thumb_worker, daemon=True, name="thumbs").start()
    print(f"pictures of meshes: {'drawn here' if thumbs_mod.available() else 'not drawn: no node and playwright-core here (thumbs.py)'}", flush=True)
    if splat_mod.enabled():
        why = splat_mod.unavailable()
        print(f"splats: {'not fitted, as ' + why if why else 'fitted for each new mesh'}", flush=True)
        if not why:
            threading.Thread(target=splat_worker, daemon=True).start()
    if RANGES:
        threading.Thread(target=RANGES.run, daemon=True).start()
    threading.Thread(target=NOTIFIER.follow, daemon=True).start()     # replies from the phone, while notifications are on
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8470"))
    mode = "COLMAP + OpenMVS" if pipeline.real_pipeline_ready() else "DEMO (COLMAP/OpenMVS not found)"
    print(f"BoulderMesh {VERSION or '(no build recorded)'} on http://{host}:{port}  data={DATA}  pipeline={mode}", flush=True)
    print("editing: " + ("none: READ_ONLY=1" if READ_ONLY else "with the token in EDIT_TOKEN" if EDIT_TOKEN else
                         "open to anyone who can reach this port; set EDIT_TOKEN, or READ_ONLY=1, before exposing it"), flush=True)
    in_docker = Path("/.dockerenv").exists()
    admin_mod.start(admin_mod.Context(settings(), admin_state, apply_settings,
                                      {"test": admin_test, "end-nodes": admin_end_nodes, "retexture": admin_retexture, "rest": admin_rest,
                                       "remesh-untied": admin_remesh_untied, "inputs-check": admin_inputs_check,
                                       "old-decide": admin_old_decide, "lost-decide": admin_lost_decide,
                                       "volunteer-add": admin_volunteer_add, "volunteer-revoke": admin_volunteer_revoke,
                                       "volunteer-limits": admin_volunteer_limits,
                                       "volunteers-skipped": admin_volunteers_skipped, "volunteers-details": admin_volunteers_details,
                                       "volunteers-cancel": admin_volunteers_cancel, "machine-add": admin_machine_add, "machine-show": admin_machine_show,
                                       "machine-revoke": admin_machine_revoke, "clear-rejected": admin_clear_rejected,
                                       "volunteers-forget": lambda _: {"result": f"forgot {VOLUNTEERS.forget_gone()} volunteer(s)"},
                                       "machines-forget": lambda _: {"result": f"forgot {MACHINES.forget_gone()} machine(s)"},
                                       "volunteer-approve": admin_volunteer_approve, "volunteer-deny": admin_volunteer_deny,
                                       "machine-approve": admin_machine_approve, "machine-deny": admin_machine_deny,
                                       "notify-topics": admin_notify_topics, "notify-test": admin_notify_test, "edit-pair": admin_edit_pair,
                                       "measure": admin_measure}),
                    os.environ.get("ADMIN_HOST", "0.0.0.0" if in_docker else "127.0.0.1"), ADMIN_PORT)
    if VOLUNTEER_PORT:
        volunteers_mod.serve(VOLUNTEERS, os.environ.get("VOLUNTEER_HOST", "0.0.0.0" if in_docker else "127.0.0.1"), VOLUNTEER_PORT,
                             lambda line: print(line, flush=True))
    if WORKER_PORT:
        pullwork.serve(MACHINES, os.environ.get("WORKER_HOST", "0.0.0.0" if in_docker else "127.0.0.1"), WORKER_PORT,
                       lambda line: print(line, flush=True))
    server = ThreadingHTTPServer((host, port), Handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=stop_when_idle, args=(server,), daemon=True).start())
    # docker kill -s USR1 <container>: what each thread is doing, written down now (README, "When it hangs")
    signal.signal(signal.SIGUSR1, lambda *_: threading.Thread(target=dump_threads_now, args=("asked for (SIGUSR1)",), daemon=True).start())
    threading.Thread(target=tell_back_up, args=(began, port), daemon=True).start()
    server.serve_forever()
    sys.stdout.flush()
    os._exit(0)     # without waiting on workers' threads, whose jobs the next start marks interrupted


if __name__ == "__main__":
    main()
