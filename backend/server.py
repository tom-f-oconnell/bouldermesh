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
  cache/http/                           cached web responses (climb pages, API calls, images)
"""
from __future__ import annotations

import base64
import csv
import email.utils
import hashlib
import hmac
import io
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import time
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import admin as admin_mod  # noqa: E402
import beta as beta_mod  # noqa: E402
import scale as scale_mod  # noqa: E402
import compute  # noqa: E402
import diagnostics  # noqa: E402
import meshclean  # noqa: E402
import meshopts  # noqa: E402
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
SHRINK_SIDES = (2048, 4096)                      # sizes a texture can be asked for, if a browser can't manage the original
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
# the header in which a proxy in front names the sender, for the log (X-Real-IP behind deploy/Caddyfile); only for a port
# nothing but that proxy can reach, as anyone else could write it
CLIENT_IP_HEADER = os.environ.get("CLIENT_IP_HEADER") or None
# which build this is, and the name it's pushed under; and the image remote machines run unless the admin page names
# another, the same commit's GPU build (or this one, from before there was a separate one): given by deploy/build.sh
# (the Dockerfile's last lines), and empty for any other build
VERSION = os.environ.get("BOULDERMESH_COMMIT") or None
OWN_IMAGE = os.environ.get("BOULDERMESH_IMAGE") or None
MACHINES_IMAGE = os.environ.get("BOULDERMESH_WORKER_IMAGE") or OWN_IMAGE
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
# lowest areas named like these hold several rocks, so one mesh can't stand in for all their climbs
GROUP_AREA = re.compile(r"\b(boulders|area|circuit|bouldering|rocks|blocks|cluster|complex)\b", re.I)

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
    con = sqlite3.connect(DATA / "bouldermesh.sqlite", check_same_thread=False)
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
        """)
        # migrations for databases created by v0.1
        cols = {r[1] for r in con.execute("PRAGMA table_info(projects)")}
        # job: the last job queued for it outside an area, to run again, as it was asked for, if a restart stops it;
        # boulder: for a boulder's project, the area path of its climbs (as climbs.area_path has it)
        for c, t in (("kind", "TEXT DEFAULT 'user'"), ("climb_id", "TEXT"), ("harvest", "TEXT"), ("progress", "TEXT"), ("job", "TEXT"),
                     ("boulder", "TEXT")):
            if c not in cols:
                con.execute(f"ALTER TABLE projects ADD COLUMN {c} {t}")
        cols = {r[1] for r in con.execute("PRAGMA table_info(meshes)")}
        for c in ("lat", "lng"):
            if c not in cols:
                con.execute(f"ALTER TABLE meshes ADD COLUMN {c} REAL")
        if "boulder" not in cols:   # a mesh of a whole boulder (a boulder project's): the area path of its climbs
            con.execute("ALTER TABLE meshes ADD COLUMN boulder TEXT")
        cols = {r[1] for r in con.execute("PRAGMA table_info(campaigns)")}
        if "only_retries" not in cols:
            con.execute("ALTER TABLE campaigns ADD COLUMN only_retries INTEGER DEFAULT 0")
        if "without_youtube" not in cols:   # mesh climbs whose YouTube search hasn't been done (none to do it)
            con.execute("ALTER TABLE campaigns ADD COLUMN without_youtube INTEGER DEFAULT 0")
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
        cols = {r[1] for r in con.execute("PRAGMA table_info(campaign_items)")}
        # redo: what it's queued to do again (REDO_NAMES); was: its status before; changed: its photos have
        # changed since it was last meshed or checked; redo_started: a rebuild that began, to carry on with
        # waiting_since: when it first waited for volunteers' YouTube searches and videos (status 'waiting')
        for c, t in (("retry", "INTEGER DEFAULT 0"), ("redo", "TEXT"), ("was", "TEXT"), ("changed", "INTEGER DEFAULT 0"),
                     ("redo_started", "INTEGER DEFAULT 0"), ("waiting_since", "REAL")):
            if c not in cols:
                con.execute(f"ALTER TABLE campaign_items ADD COLUMN {c} {t}")
        if "replaced_by" not in {r[1] for r in con.execute("PRAGMA table_info(meshes)")}:
            con.execute("ALTER TABLE meshes ADD COLUMN replaced_by TEXT")   # a rebuild of the same photos' project
        # A restart only pauses an area: its climb carries on from the last step it finished, and the
        # area carries on by itself unless someone stopped it. Other jobs are lost, as their queue is, so
        # they're marked interrupted, not failed: Retry carries on with one, or runs one that hadn't started.
        con.execute("UPDATE projects SET status='stopped' WHERE id IN (SELECT project_id FROM campaign_items "
                    "WHERE status='running' OR (status='pending' AND retry=1 AND redo IS NULL))")
        con.execute("UPDATE campaign_items SET status='pending', note='interrupted by a restart: carrying on from where it got to' WHERE status='running'")
        con.execute("UPDATE campaigns SET status='stopped', note=NULL WHERE status='stopping'")
        cut = con.execute("SELECT id, status, job FROM projects WHERE status IN ('queued','running','harvesting')").fetchall()
        for r in cut:
            job = json.loads(r["job"]) if r["job"] else None
            if job and r["status"] != "queued":
                job["resume"] = True    # it had started: it carries on from the last step it finished
            con.execute("UPDATE projects SET status='interrupted', progress=NULL, job=? WHERE id=?",
                        (json.dumps(job) if job else None, r["id"]))
    for r in cut:
        append_log(r["id"], "INTERRUPTED: the server restarted " + ("before this job started: Retry runs it" if r["status"] == "queued"
                                                                    else "during this job: Retry carries on from the last step it finished"))


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


def climb_row(cid: str, full=False) -> dict | None:
    r = q("SELECT * FROM climbs WHERE id=?", cid, one=True)
    if not r:
        return None
    r["area_path"] = json.loads(r["area_path"] or "[]")
    data = json.loads(r.pop("data") or "{}")
    if full:
        r["data"] = data
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
    else:
        r["n_images"] = len(data.get("images") or [])
        r["n_videos"] = len(data.get("videos") or [])
    r["n_links"] = q("SELECT COUNT(*) n FROM mesh_links WHERE climb_id=? AND COALESCE(note,'') NOT LIKE ?", cid, SHARED + "%", one=True)["n"]
    # meshes of its whole boulder (run_boulder()), and those an area linked it to that were made for another climb on it
    boulder = boulder_of(r["area_path"])
    r["n_boulder"] = q("SELECT COUNT(*) n FROM meshes WHERE boulder=? AND replaced_by IS NULL", boulder, one=True)["n"] if boulder else 0
    # the climb the mesh its View button opens was made for (the page picks a current one, the newest first)
    shared = q(f"""SELECT l.note, ({MADE_FOR}) AS made_for FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id
                   WHERE l.climb_id=? AND l.note LIKE ? ORDER BY m.replaced_by IS NOT NULL, m.created DESC LIMIT 1""",
               SHARED + "%", cid, SHARED + "%", one=True)
    r["n_shared"] = q("SELECT COUNT(*) n FROM mesh_links WHERE climb_id=? AND note LIKE ?", cid, SHARED + "%", one=True)["n"]
    r["shared_of"] = (shared["made_for"] or shared["note"][len(SHARED):]) if shared else None
    return r


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
    """Which boulder a climb is on, as areas' lists record it: its area path, unless the last area holds
    several boulders."""
    return "/".join(area_path) if area_path and not GROUP_AREA.search(area_path[-1]) else None


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
    for m in q("SELECT id, source FROM meshes WHERE boulder=? AND replaced_by IS NULL ORDER BY created DESC", boulder):
        if not (m["source"] or "").startswith("project:"):
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
            "SELECT 1 FROM meshes WHERE id=? AND replaced_by IS NULL", proj["mesh_id"]):
        return None
    try:
        record = json.loads((PROJECTS / proj["id"] / CLIMBS_FILE).read_text())
        then, pooled_at = {c["id"]: sorted(c["projects"]) for c in record["climbs"]}, float(record["created"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if {c["id"]: sorted(climb_harvests(c["id"])) for c in boulder_climbs(boulder)} != then:
        return None
    for h in {h for hs in then.values() for h in hs}:
        photos = PROJECTS / h / "photos"
        try:
            if max([photos.stat().st_mtime] + [f.stat().st_mtime for f in photos.iterdir()]) > pooled_at:
                return None     # harvested again since
        except OSError:
            return None
    return proj["mesh_id"]


def boulder_name(boulder: str) -> str:
    return json.loads(boulder)[-1].strip("* ")


def boulder_climbs(boulder: str) -> list[dict]:
    """The climbs known on a boulder, by name."""
    return q("SELECT id, name, grade, lat, lng, data FROM climbs WHERE area_path=? ORDER BY name", boulder)


def adopt_listed_climbs(boulder: str) -> int:
    """Save the climbs an area lists on a boulder that it hasn't reached yet, from what its list says of them,
    so that a boulder's mesh can include them too. Returns how many."""
    rows = q("SELECT source_id, data FROM campaign_items WHERE boulder=? AND climb_id IS NULL AND data IS NOT NULL",
             boulder_key(json.loads(boulder)))
    for r in rows:
        cid = upsert_climb(Climb.from_dict(json.loads(r["data"])))
        q("UPDATE campaign_items SET climb_id=? WHERE source_id=? AND climb_id IS NULL", cid, r["source_id"], write=True)
    return len(rows)


def boulder_project(boulder: str) -> dict | None:
    """A boulder's project, which pools its climbs' harvests (run_boulder())."""
    return q("SELECT * FROM projects WHERE kind='boulder' AND boulder=? ORDER BY created DESC LIMIT 1", boulder, one=True)


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
    for m in q("SELECT id, name, created FROM meshes WHERE boulder=? AND replaced_by IS NULL ORDER BY created DESC", boulder):
        views = viewpoints.load(MESHES / m["id"]) or {}
        meshes.append({**m, "view": (views.get("climbs") or {}).get(climb["id"]), "placed": len(views.get("climbs") or {}),
                       "unplaced": (views.get("unplaced") or {}).get(climb["id"])})
    shared = q(f"""SELECT m.id, m.name, l.note, ({MADE_FOR}) AS made_for FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id
                   WHERE l.climb_id=? AND l.note LIKE ? AND m.replaced_by IS NULL""", SHARED + "%", climb["id"], SHARED + "%")
    proj = boulder_project(boulder)
    return {"key": boulder, "name": boulder_name(boulder), "meshes": meshes,
            "shared": [{"id": s["id"], "name": s["name"], "made_for": s["made_for"] or s["note"][len(SHARED):]} for s in shared],
            "climbs": [{"id": c["id"], "name": c["name"], "grade": c["grade"], "meshes": own.get(c["id"], [])}
                       for c in climbs if c["id"] != climb["id"]],
            "project": {k: proj[k] for k in ("id", "status")} if proj else None}


def supersede(old: str, new: str) -> None:
    """A rebuilt mesh takes over from the one it replaces: its climbs, and its place in area lists.
    The old one stays, as an older version of the new one."""
    for link in q("SELECT climb_id, note FROM mesh_links WHERE mesh_id=?", old):
        link_mesh(new, link["climb_id"], link["note"])
    q("DELETE FROM mesh_links WHERE mesh_id=?", old, write=True)
    q("UPDATE campaign_items SET mesh_id=? WHERE mesh_id=?", new, old, write=True)
    q("UPDATE meshes SET replaced_by=? WHERE id=? OR replaced_by=?", new, old, old, write=True)
    q("UPDATE meshes SET replaced_by=NULL WHERE id=?", new, write=True)


def make_current(mid: str) -> None:
    """Go back to an older version of a mesh, which takes over from the one that replaced it."""
    m = q("SELECT replaced_by, source FROM meshes WHERE id=?", mid, one=True)
    if not m or not m["replaced_by"]:
        return
    supersede(m["replaced_by"], mid)
    if m["source"].startswith("project:"):
        q("UPDATE projects SET mesh_id=? WHERE id=?", mid, m["source"].split(":", 1)[1], write=True)


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
    for m in q("SELECT id,name FROM meshes WHERE source LIKE 'project:%' AND filename='mesh.glb' ORDER BY created"):
        if meshclean.load(MESHES / m["id"]) is None:
            clean_mesh(m["id"], lambda line, name=m["name"]: print(f"{name}: {line}", flush=True))
            time.sleep(0.1)


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


def shrunk_texture(path: Path, side: int) -> Path:
    """A copy of a texture at most side pixels wide and high, kept beside it, or itself if that small."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None    # these are our own files, and the large ones are why we're here
    out = path.parent / "smaller" / f"{path.stem}-{side}.jpg"
    if out.exists():
        return out
    with Image.open(path) as im:
        if max(im.size) <= side:
            return path
        im = im.convert("RGB")
    im.thumbnail((side, side), Image.LANCZOS)
    out.parent.mkdir(exist_ok=True)
    partial = out.with_suffix(".partial")
    im.save(partial, "JPEG", quality=88)
    partial.replace(out)
    return out


# ------------------------------------------------------------------ worker
jobs: "queue.Queue[dict]" = queue.Queue()


def queue_job(job: dict) -> None:
    """Queue a job someone asked for, keeping a project's with the project, to run again as it was asked for
    if a restart interrupts it."""
    if job.get("pid"):
        q("UPDATE projects SET job=? WHERE id=?", json.dumps(job), job["pid"], write=True)
    jobs.put(job)


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


def tracker_for(pid: str, steps: list[tuple[str, str, float]]) -> Tracker:
    return Tracker(steps, lambda snapshot: q("UPDATE projects SET progress=? WHERE id=?", snapshot, pid, write=True))


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
        mesh_path, demo = build(pdir / "photos", work, MESHES / mid, log, USE_GPU, internet=internet, tracker=tracker)
        proj = q("SELECT name, climb_id, mesh_id, kind, boulder FROM projects WHERE id=?", pid, one=True)
        q("INSERT INTO meshes(id,name,filename,source,created,size,demo,lat,lng,boulder) VALUES(?,?,?,?,?,?,?,?,?,?)", mid, proj["name"],
          mesh_path.name, f"project:{pid}", time.time(), mesh_path.stat().st_size, int(demo), *boulder_place(proj["boulder"]),
          proj["boulder"] if proj["kind"] == "boulder" else None, write=True)
        if proj["climb_id"]:
            link_mesh(mid, proj["climb_id"], "auto-linked from reconstruction project")
            log(f"mesh linked to climb {proj['climb_id']}")
        before = proj["mesh_id"] if proj["mesh_id"] and proj["mesh_id"] != mid and q("SELECT id FROM meshes WHERE id=?", proj["mesh_id"]) else None
        worse = fewer_placed(before, mid, pdir / "photos") if before and keep_better else None
        if worse:
            supersede(mid, before)
            log(f"this placed {worse[0]} of its photos, fewer than the {worse[1]} mesh {before} placed from them: that one stays "
                "the current one, and this is kept as an older version of it")
        elif before:
            supersede(before, mid)
            log(f"this replaces mesh {before}, which stays as an older version")
        current = before if worse else mid
        clean_mesh(mid, log)
        record_sources(mid, pid, log)
        if proj["kind"] == "boulder" and not worse:
            place_climbs(mid, pid, log)
        tracker.finish()     # before it's marked done, so that anything waiting for that sees it all finished
        keep_timings(mid, pid, started, log_from, tracker, remote)
        log("done")
        q("UPDATE projects SET status='done', mesh_id=?, demo=? WHERE id=?", current, int(demo), pid, write=True)
        q("UPDATE campaign_items SET status='done', mesh_id=?, retry=0, note=NULL, updated=? WHERE project_id=? AND status='failed'",
          current, time.time(), pid, write=True)   # made by hand since its area gave up on it
        if not demo and not worse:
            queue_scale(mid)
            queue_splat(mid)
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
    rows = q("""SELECT p.id FROM projects p JOIN climbs c ON c.id=p.climb_id WHERE c.area_path=? AND p.kind='harvest'
                AND p.harvest IS NOT NULL AND p.id != ? AND p.name NOT LIKE ? ORDER BY p.created DESC""",
             c["area_path"], but or "", "%" + POOLED)
    return [PROJECTS / r["id"] for r in rows if (PROJECTS / r["id"] / "photos").is_dir()]


def run_harvest(pid: str, sources: set[str], reconstruct: bool, relevance_filter: bool = True, resume: bool = False,
                again: bool = False, wait: bool = False, need_youtube: bool = False) -> None:
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
    """
    pdir = PROJECTS / pid
    finished = q("SELECT harvest FROM projects WHERE id=?", pid, one=True)["harvest"] if resume else None
    if resume:
        append_log(pid, "-- carrying on after an interruption")
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
        tracker = tracker_for(pid, ([("page", "Fetch problem page", 1)] if stub else []) + harvest_mod.steps(sources, relevance_filter)
                              + (pipeline.steps() if reconstruct else []))
        if finished:
            summary = json.loads(finished)
            log("the photos were already harvested")
        else:
            if resume or again:
                for partial in ("photos", "rejected"):
                    shutil.rmtree(pdir / partial, ignore_errors=True)
                (pdir / "photos").mkdir()
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
                                          want_search=search_wanter("harvest", pid))
            videos = summary.pop("videos", [])
            record_videos(proj["climb_id"], videos, log)
            summary["videos"] = [{k: v.get(k) for k in ("url", "found_by", "title", "uploader", "frames", "license")} for v in videos]
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
BOULDER_SUFFIX = " (boulder)"   # the name of a boulder's project and mesh


def climb_harvests(climb_id: str) -> list[str]:
    """A climb's own harvests that found photos, newest first (not a pooled one from before boulders had theirs)."""
    rows = q("SELECT id FROM projects WHERE climb_id=? AND kind='harvest' AND harvest IS NOT NULL AND name NOT LIKE ? ORDER BY created DESC",
             climb_id, "%" + POOLED)
    return [r["id"] for r in rows if (PROJECTS / r["id"] / "photos").is_dir()]


def harvest_climb(climb_id: str, name: str, log) -> str | None:
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
    run_harvest(pid, set(HARVEST_SOURCES), False, resume=bool(p))
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
    finished = proj["harvest"] if resume else None
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
        if fetch:
            adopted = adopt_listed_climbs(boulder)
            if adopted:
                log(f"{adopted} climb(s) an area lists here, which it hadn't reached, are now known")
        climbs = boulder_climbs(boulder)
        missing = [c for c in climbs if not climb_harvests(c["id"])] if fetch and not finished else []
        tracker = tracker_for(pid, ([("climbs", "Harvest its climbs", 3 * len(missing))] if missing else [])
                              + harvest_mod.steps(set()) + pipeline.steps())
        for i, c in enumerate(missing):
            tracker.update("climbs", i / len(missing), c["name"])
            log(f"harvesting {c['name']}, which hadn't been")
            cpid = harvest_climb(c["id"], c["name"], log)
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
            (pdir / CLIMBS_FILE).write_text(json.dumps({"boulder": boulder, "climbs": record, "created": time.time()}, indent=1))
            others = [PROJECTS / h for r in record for h in r["projects"]]
            log(f"-- pooling the photos of {len(others)} harvest(s) of {sum(bool(r['projects']) for r in record)} of its "
                f"{len(record)} climb(s)")
            if not others:
                raise ValueError("none of its climbs has been harvested")
            lat, lng = boulder_place(boulder)
            c0 = Climb(source="boulder", source_id=boulder_key(json.loads(boulder)), url="", name=boulder_name(boulder), kind="area",
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
        if q("SELECT status FROM projects WHERE id=?", pid, one=True)["status"] == "done":
            shutil.rmtree(pdir / "work", ignore_errors=True)   # most of its disk use, and only useful to itself
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


class Worker:
    """Somewhere climbs are meshed: this server, or for a remote worker, machines from compute.py. Each
    has its own thread, and its own JobControl, so stopping one area's climb leaves others alone."""

    def __init__(self, name: str, remote: bool = False, control: JobControl | None = None):
        self.name, self.remote = name, remote
        self.control = control or JobControl()
        self.campaign: str | None = None   # the area whose climb it's on
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
          "mapper_below=?, mapper_more=? WHERE id=?", order, max_meshes, int(bool(count_failures)), int(bool(without_youtube)),
          int(bool(same_photos_once)), int(bool(keep_better)), mapper["mapper_below"], mapper["mapper_more"], old["id"], write=True)
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
      "keep_better,mapper_below,mapper_more) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", cid, common[-1].strip("* ") if common else f"area {aid}",
      aid, order, time.time(), note, "running" if start else "stopped", int(bool(without_youtube)), max_meshes, int(bool(count_failures)),
      int(bool(same_photos_once)), int(bool(keep_better)), mapper["mapper_below"], mapper["mapper_more"], write=True)
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
    together = settings().get("areas.meshes") == "boulders"
    units: dict[str, list[dict]] = {}
    for i in _in_order(camp, items):
        units.setdefault(i["boulder"] if together and i["boulder"] else "\0" + i["source_id"], []).append(i)
    return list(units.values())


UNFINISHED = ("pending", "running", "waiting", "harvested")


def limit_scope(camp: dict) -> tuple[set[str] | None, int]:
    """What an area with a limit (max_meshes) works through: its climbs and boulders (_units()) in its order, until as
    many as its limit have made a mesh or may yet, or failed too if it counts failures (count_failures). One that
    fails otherwise brings in the next. What's done stays within it, so that raising the limit only goes on.

    Returns:
        (the climbs within it, by source_id, or None without a limit; how many meshes they've made).
    """
    if not camp.get("max_meshes"):
        return None, 0
    items = q("SELECT source_id, rank, stars, media, boulder, status FROM campaign_items WHERE campaign_id=?", camp["id"])
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


def ordered_pending(camp: dict) -> list[dict]:
    """A campaign's waiting climbs, next first: within its limit, if it has one."""
    items = q("SELECT rank,source_id,name,grade,stars,url,media,project_id,retry,redo,boulder,waiting_since FROM campaign_items "
              "WHERE campaign_id=? AND status='pending' ORDER BY rank", camp["id"])
    scope = in_scope(camp)
    if scope is None:
        items = _in_order(camp, items)
    else:       # a boulder's climbs one after another, so that its mesh is made before going on
        items = [i for unit in _units(camp, [i for i in items if i["source_id"] in scope]) for i in unit]
    # retries were asked for, and a climb that was interrupted comes next, to finish what it started
    items.sort(key=lambda i: (not i["retry"], not i["project_id"]))
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
        have = q("SELECT mesh_id FROM mesh_links WHERE climb_id=? AND COALESCE(note,'') NOT LIKE ? LIMIT 1", climb_id, SHARED + "%", one=True)
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
            pool = pooled(cid, item.get("boulder"))
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
        _outages.clear()
    finally:
        q("UPDATE campaign_items SET retry=0 WHERE campaign_id=? AND source_id=? AND status NOT IN ('pending','running')", cid, sid, write=True)


HARVESTED_NOTE = "harvested for its boulder's mesh, made once the area's climbs on it are all harvested"


def pooled(camp_id: str, boulder: str | None) -> bool:
    """Whether an area's climb on this boulder is only harvested, for its boulder's mesh from every photo of the climbs on
    it (areas.meshes "boulders"), rather than meshed on its own: when the area lists another climb on the boulder."""
    if not boulder or settings().get("areas.meshes") != "boulders":
        return False
    return q("SELECT count(*) AS n FROM campaign_items WHERE campaign_id=? AND boulder=?", camp_id, boulder, one=True)["n"] >= 2


def settle_boulder(camp_id: str, boulder: str | None) -> bool:
    """Once none of an area's climbs on a boulder is still to be harvested (pending, running or waiting for volunteers),
    queue meshing the boulder from all their photos (run_boulder()), on the highest-ranked of them harvested for it.

    Returns:
        Whether it was queued.
    """
    if not boulder:
        return False
    items = q("SELECT source_id, status FROM campaign_items WHERE campaign_id=? AND boulder=? ORDER BY rank", camp_id, boulder)
    camp = q("SELECT * FROM campaigns WHERE id=?", camp_id, one=True) or {}
    scope = in_scope(camp)
    if scope is not None:   # those past an area's limit (as after lowering it) aren't waited for
        items = [i for i in items if i["source_id"] in scope or i["status"] in ("running", "waiting", "harvested")]
    if any(i["status"] in ("pending", "running", "waiting") for i in items):
        return False
    harvested = [i for i in items if i["status"] == "harvested"]
    if not harvested:
        return False
    mesh = boulder_unchanged(json.dumps(boulder.split("/"))) if camp.get("same_photos_once", 1) != 0 else None
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
    proj = boulder_project(json.dumps(boulder.split("/"))) if boulder else None     # as boulder_of() has it, from boulder_key()'s
    if not failed and proj and proj["status"] == "done" and proj["mesh_id"]:
        status, mesh = "done", proj["mesh_id"]
    else:
        status, mesh = ("skipped" if not failed and proj and proj["status"] == "harvested" else "failed"), None
    note = (f"meshed with its boulder: {end['note'].removeprefix('its boulder meshed from ')}"
            if status == "done" and end["note"].startswith("its boulder meshed from ") else end["note"])
    q("UPDATE campaign_items SET status=?, mesh_id=COALESCE(?, mesh_id), note=?, retry=0, updated=? "
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
    queues it to harvest again and mesh once it has all come, and settle_waiting() after VOLUNTEER_WAIT."""
    n = VOLUNTEERS.pending("harvest", pid)
    note = (f"waiting for volunteers: {n} YouTube search(es) and video(s) to come" if n else
            "waiting for its YouTube search, which no volunteer has been asked for (there are none): add one, or let its "
            "area mesh without")
    set_item(cid, sid, status="waiting", waiting_since=since or time.time(), redo=None, retry=0, redo_started=0, note=note)


def settle_waiting(camp_id: str) -> None:
    """An area's climbs that have waited for volunteers for VOLUNTEER_WAIT: harvested again and meshed with what's come."""
    q("UPDATE campaign_items SET status='pending', retry=1, redo='harvest+rebuild', was='waiting', redo_started=0, note=? "
      "WHERE campaign_id=? AND status='waiting' AND waiting_since<?",
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


def watch_usage() -> None:
    """Measure DATA_DIR now and every USAGE_EVERY seconds, while there's a cap."""
    while True:
        try:
            if data_cap():
                measure_usage()
        except Exception as e:  # never let the thread die
            print(f"usage error: {e!r}", flush=True)
        time.sleep(USAGE_EVERY)


def snapshot_databases() -> list[Path]:
    """Copy each database into SNAPSHOTS as <name>.backup, by SQLite's backup API, which is safe while they're in use,
    as copying the files isn't. Returns the copies."""
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    made = []
    for db_file in (DATA / "bouldermesh.sqlite", DATA / "volunteers.sqlite", DATA / "compute" / "machines.sqlite"):
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
    camps = q("SELECT * FROM campaigns WHERE status='running' ORDER BY created")
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
        items = ordered_pending(camp)
        waiting = bool(items)
        if camp["only_retries"]:
            items = [i for i in items if i["retry"]]
        taken = {x.boulder for x in others if x.boulder}
        free = [i for i in items if not (i["boulder"] and i["boulder"] in taken)]
        if not free:
            if not items and not any(x.campaign == camp["id"] for x in others):
                held = q("SELECT count(*) AS n FROM campaign_items WHERE campaign_id=? AND status='waiting'", camp["id"], one=True)["n"]
                if held:    # it carries on once volunteers have fetched what those wait for
                    note = f"{held} climb(s) waiting for volunteers' YouTube searches and videos"
                    q("UPDATE campaigns SET note=? WHERE id=? AND COALESCE(note,'')!=?", note, camp["id"], note, write=True)
                else:
                    q("UPDATE campaigns SET status=?, only_retries=0, note=NULL WHERE id=? AND status='running'",
                      "stopped" if waiting else "done", camp["id"], write=True)
            continue
        item = free[0]
        set_item(camp["id"], item["source_id"], status="running")
        q("UPDATE campaigns SET note=NULL WHERE id=? AND note LIKE '%waiting for volunteers%'", camp["id"], write=True)
        w.campaign, w.boulder, w.doing = camp["id"], item["boulder"], item["name"]
        return camp, item
    return None


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
    except Exception as e:
        set_item(camp["id"], item["source_id"], status="failed", note=f"{type(e).__name__}: {e}")
    finally:
        w.campaign = w.boulder = w.doing = None
        if not any(x.campaign == camp["id"] for x in _workers):
            q("UPDATE campaigns SET status='stopped', note=NULL WHERE id=? AND status='stopping'", camp["id"], write=True)
    return True


def queue_retry(camp_id: str, source_id: str, project_id: str | None) -> None:
    """Put one of a campaign's climbs at the front of its list. A campaign that isn't running runs
    only its retries and then stops, rather than carrying on down its list."""
    set_item(camp_id, source_id, status="pending", retry=1, note="queued to try again")
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
        queue_job(json.loads(proj["job"]) if proj["status"] == "interrupted" and proj["job"] else {"type": "retry", "pid": pid})
    return {"queued": True}, 202


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
              "harvest+rebuild": "harvest again and rebuild", "videos+rebuild": "look for new videos and rebuild",
              "boulder": "mesh its boulder from every climb's photos", "pool": "mesh its boulder from every climb's photos",
              "try-skipped": "try meshing, as the viewpoint check would let it now"}
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
        boulder = json.dumps(r["boulder"].split("/"))
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
        what: "harvest" every climb with photos of its own; "videos": only those that YouTube has new
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
        + ("" if what.split("+")[0] in ("harvest", "videos") else " AND p.harvest IS NOT NULL")
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
    if row["redo"].split("+")[0] == "videos":
        new = new_videos(row["climb_id"], pid, log)
        if not new:
            log("no videos of this climb on YouTube that its photos don't already have frames from")
            return {"status": row["was"], "note": "no new videos found"}
        log(f"{len(new)} new video(s) of this climb: harvesting it again")
    run_harvest(pid, set(HARVEST_SOURCES), False, again=True)
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
    if not boulder:
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
    row = q("SELECT source_id,climb_id,project_id,mesh_id,redo,was,changed,redo_started,waiting_since,boulder FROM campaign_items "
            "WHERE campaign_id=? AND source_id=?", cid, sid, one=True)
    row["was"] = row["was"] or "done"
    log = lambda s: append_log(row["project_id"], s)  # noqa: E731
    first, *then = row["redo"].split("+")
    if first in ("boulder", "pool"):    # pool: queued before boulders had their own meshes
        return _run_boulder_redo(camp, row)
    jobctl.begin(low_priority=True)
    try:
        set_item(cid, sid, status="running", note=None)
        if first in ("harvest", "videos") and not row["redo_started"]:    # a rebuild that started was harvested already
            end = _harvest_again(row, log)
            if row["was"] == "waiting" and not row["mesh_id"] and may_wait(row) and VOLUNTEERS.pending("harvest", row["project_id"]):
                log("waiting for more from volunteers: the YouTube videos their search found")
                return wait_for_volunteers(cid, sid, row["project_id"], row["waiting_since"])
            if row["was"] == "waiting" and not row["mesh_id"] and unchecked(camp, row["project_id"]):   # tried again later
                log("still not searched for on YouTube: not meshed yet")
                return wait_for_volunteers(cid, sid, row["project_id"], None)
            if pooled(cid, row["boulder"]) and (row["was"] in ("waiting", "harvested") or (then and end.get("changed"))):
                end = {**end, "status": "harvested", "note": HARVESTED_NOTE}     # its boulder's mesh is made again with it
            elif then and (end.get("changed") or (row["was"] == "waiting" and not row["mesh_id"])):
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
    except Exception as e:
        end = {"status": row["was"], "note": f"{REDO_NAMES[row['redo']]}: {type(e).__name__}: {e}"}
    set_item(cid, sid, **end, redo=None, retry=0, redo_started=0)


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
    except Exception as e:
        end, failed = {"status": row["was"], "note": f"{REDO_NAMES['boulder']}: {type(e).__name__}: {e}"}, True
    if row["was"] == "harvested":
        end = boulder_meshed(cid, sid, end, failed)
    elif not failed:    # one done or skipped before carried it: its neighbours harvested for it are done with it too
        b = q("SELECT boulder FROM campaign_items WHERE campaign_id=? AND source_id=?", cid, sid, one=True)["boulder"]
        proj = boulder_project(json.dumps(b.split("/"))) if b else None
        if proj and proj["status"] == "done" and proj["mesh_id"]:
            boulder_meshed(cid, sid, end, failed)
    set_item(cid, sid, **end, redo=None, retry=0, redo_started=0)


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
_beta_now: dict | None = None    # the mesh it's finding beta in: {mid, started}


def queue_beta(mid: str, ahead: bool = False) -> bool:
    """Queue finding a mesh's beta, unless it's queued or being found already; whether it was queued.

    Args:
        ahead: before those queued for every mesh at once, as someone asked for this one.
    """
    if (beta_mod.load(MESHES / mid) or {}).get("status") in ("queued", "running"):
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
    """Meshes queued to find beta in, in the order it'll take them: those asked for one at a time first."""
    found = []
    for d in MESHES.iterdir() if MESHES.exists() else []:
        rec = beta_mod.load(d)
        if rec and rec["status"] == "queued":
            found.append(((not rec.get("ahead"), rec.get("queued", 0)), d.name))
    return [mid for _, mid in sorted(found)]


def beta_findable() -> list[str]:
    """Meshes with video frames placed in them, whose beta hasn't been found or isn't being: newest first."""
    out = []
    for r in q("SELECT id FROM meshes WHERE source LIKE 'project:%' AND replaced_by IS NULL ORDER BY created DESC"):
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
        try:
            requeue_waiting_scale()
        except Exception as e:  # never let the thread die
            print(f"scale requeue error: {e!r}", flush=True)
        while not _stopping:
            queued = [mid for mid in beta_queue() if beta_ready(mid)]
            if not queued:
                break
            _beta_now = {"mid": queued[0], "started": time.time()}
            jobctl.begin(low_priority=True)
            try:
                run_beta(queued[0])
            except Exception as e:  # never let the thread die
                print(f"beta error on {queued[0]}: {e!r}", flush=True)
            finally:
                _beta_now = None


_frames_wake = threading.Event()
_frames_control = JobControl()


def frames_worker() -> None:
    """Turns videos uploaded to projects into photos, one at a time, oldest first, on a thread of its own at the lowest
    CPU priority, like finding beta. A video is deleted once it's done, or has failed; one a restart cut short is
    done again."""
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), BETA_NICE)
    use_control(_frames_control)
    while True:
        _frames_wake.wait(timeout=300)
        _frames_wake.clear()
        while not _stopping:
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

    def log(line: str) -> None:
        with open(mesh_dir / "scale.log", "a") as f:
            f.write(line.rstrip() + "\n")
    climbs = [json.loads(r["data"]) for r in q("SELECT c.data FROM mesh_links l JOIN climbs c ON c.id=l.climb_id WHERE l.mesh_id=?", mid)
              or q("SELECT c.data FROM meshes m JOIN climbs c ON c.area_path=m.boulder WHERE m.id=?", mid)]   # a boulder's: all on it
    try:
        if offloading():
            remote_scale(mid, pid, climbs, log)
        else:
            scale_mod.estimate(mesh_dir, PROJECTS / pid / "photos", PROJECTS / pid / "masks", climbs, mesh_videos(mid), log,
                               known=known_object(pid))
    except compute.ComputeUnavailable as e:     # queued again once a machine is there (beta_worker)
        log(f"scale: waiting for a machine to find it on: {e}")
        scale_mod.save(mesh_dir, {**(scale_mod.load(mesh_dir) or {}), "status": MACHINE_WAIT, "progress": None})
    except Cancelled:
        scale_mod.save(mesh_dir, {**(scale_mod.load(mesh_dir) or {}), "status": "stopped", "progress": None})
    except Exception as e:
        log(f"FAILED: {type(e).__name__}: {e}")
        scale_mod.save(mesh_dir, {**(scale_mod.load(mesh_dir) or {}), "status": f"failed: {type(e).__name__}: {e}", "progress": None})


# ------------------------------------------------------------------ beta and size, on machines that connect here
MESH_INPUT = {".glb", ".ply", ".json", ".bin"}   # what of a mesh's folder its beta and size need: not textures
MACHINE_WAIT = "waiting for a machine"


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
        _, back = MACHINES.task("beta", files, {}, f"beta: {mid}", log)
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
                            f"size: {mid}", log)
    try:
        log(f"scale: back from the machine: {', '.join(take_made(back, mid, 'scale')) or 'nothing'}")
    finally:
        shutil.rmtree(back, ignore_errors=True)


def requeue_waiting_scale() -> None:
    """Queue again the sizes that waited for a machine, once one is there."""
    if not (offloading() and MACHINES.connected()):
        return
    for d in MESHES.iterdir() if MESHES.exists() else []:
        if (scale_mod.load(d) or {}).get("status") == MACHINE_WAIT:
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


def campaign_view(camp: dict) -> dict:
    """A campaign for the UI, with where it's got to."""
    cid = camp["id"]
    counts = {r["status"]: r["n"] for r in q("SELECT status,COUNT(*) n FROM campaign_items WHERE campaign_id=? GROUP BY status", cid)}
    again = {key: q(f"SELECT COUNT(*) n FROM campaign_items WHERE campaign_id=? AND {where}", cid, one=True)["n"]
             for key, where in (("changed", "changed=1"), ("queued", "redo IS NOT NULL AND status IN ('pending','running')"))}
    again["boulders"] = len(boulders_to_mesh(cid))
    again["triable"] = len(triable(cid))
    worked = q("SELECT rank,name,grade,stars,url,climb_id,mesh_id,project_id,status,note,updated FROM campaign_items "
               "WHERE campaign_id=? AND status NOT IN ('pending','running') ORDER BY updated DESC LIMIT 300", cid)
    running = q("SELECT rank,name,grade,stars,project_id FROM campaign_items WHERE campaign_id=? AND status='running' ORDER BY updated", cid)
    for r in running:
        p = q("SELECT status,progress FROM projects WHERE id=?", r["project_id"], one=True) if r["project_id"] else None
        r["progress"] = json.loads(p["progress"]) if p and p["progress"] else None
    pending = ordered_pending(camp)
    made = None
    if camp.get("max_meshes"):      # those past its limit aren't to go
        counts["beyond"] = counts.get("pending", 0) - len(pending)
        counts["pending"] = len(pending)
        made = limit_scope(camp)[1]
    return {**camp, "counts": counts, "again": again, "worked": worked, "current": running[0] if running else None,
            "running": running, "next": pending[:10], "machine_wait": machine_wait(cid), "made": made}


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
    w.job = {"job": job, "started": time.time(), "from": job_progress(job) or 0.0}
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
                        again=job.get("again", False))
        elif job["type"] == "retry":
            run_retry(job["pid"])
        elif job["type"] == "boulder":
            run_boulder(job["pid"], resume=job.get("resume", False))
    except (Exception, Cancelled) as e:  # never let the worker die
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
        if _stopping or time.time() < w.wait_until:
            time.sleep(2)
            continue
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
        if not (w.remote or _local_meshing):    # areas' climbs are for other machines' workers
            continue
        try:
            if not next_campaign_step() and w.remote:
                time.sleep(CAMPAIGN_POLL)
        except (Exception, Cancelled) as e:  # never let the worker die
            print(f"campaign error on {w.name}: {e!r}", flush=True)


# ------------------------------------------------------------------ stopping, and what it would interrupt
STOP_WAIT = float(os.environ.get("STOP_WAIT_MINUTES", "10")) * 60   # docker-compose.yml's stop_grace_period is longer
_stopping: dict | None = None    # once a stop has begun: {since, until, waiting_for}
AGAIN = {"reconstruct": "Retry on its project", "harvest": "Retry on its project", "retry": "Retry on its project",
         "boulder": "Retry on its project",
         "retexture": "Make textures... on the admin page", "scale": "Find size in the viewer"}


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
    seconds it has left, from how fast it's got on so far (None until that's clear)."""
    table, key = ("projects", "pid") if job.get("pid") else ("meshes", "mid")
    row = q(f"SELECT name FROM {table} WHERE id=?", job.get(key), one=True)
    out = {"type": job["type"], ("project_id" if key == "pid" else "mesh_id"): job.get(key), "name": row["name"] if row else job.get(key)}
    if run:
        now, took = job_progress(job), time.time() - run["started"]
        gained = (now or 0.0) - run["from"]
        out.update(started=run["started"], progress=now,
                   eta=took * (100 - now) / gained if now is not None and took >= 60 and gained >= 2 else None)
    return out


def jobs_state() -> dict:
    """What a restart would stop now: jobs people started outside areas, which it interrupts (running and
    queued), and areas' climbs, which it only pauses."""
    with jobs.mutex:
        waiting = list(jobs.queue)
    runs = [w.job for w in list(_workers)]
    now = _beta_now and q("SELECT name FROM meshes WHERE id=?", _beta_now["mid"], one=True)
    return {"running": [job_info(r["job"], r) for r in runs if r], "queued": [job_info(j) for j in waiting],
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


def stop_when_idle(server: ThreadingHTTPServer) -> None:
    """Stop the server (on SIGTERM) once the jobs people started outside areas have finished, as a restart
    interrupts them. Meanwhile it takes no more work, and serves the site as before. It waits at most
    STOP_WAIT, and only for jobs that look like finishing by then. Areas' climbs aren't waited for, as a
    restart only pauses them. A second SIGTERM stops it at once."""
    global _stopping
    say = lambda s: print(f"stopping: {s}", flush=True)  # noqa: E731
    if _stopping:
        say("now, as asked again")
        return server.shutdown()
    _stopping = {"since": time.time(), "until": time.time() + STOP_WAIT, "waiting_for": []}
    passed, said = set(), 0.0     # jobs not waited for, as they'd take too long
    ident = lambda j: (j["type"], j.get("project_id") or j.get("mesh_id"))  # noqa: E731
    while True:
        state = jobs_state()
        for j in state["running"]:
            if j["eta"] is not None and time.time() + j["eta"] > _stopping["until"] and ident(j) not in passed:
                passed.add(ident(j))
                say(f"not waiting for {job_label(j)}, as it won't finish in time: after the restart, {AGAIN[j['type']]} runs it again")
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
        say(f"{job_label(j)} didn't finish in time: after the restart, {AGAIN[j['type']]} runs it again")
    if state["queued"]:
        say(f"{len(state['queued'])} queued job(s) won't start before the restart: " +
            "; ".join(f"{j['name']} ({j['type']}): {AGAIN[j['type']]} runs it" for j in state["queued"]))
    if state["area_climbs"]:
        say("areas' climbs pause, and carry on after the restart: " + "; ".join(f"{c['name']} ({c['area']})" for c in state["area_climbs"]))
    if state["beta"]["running"] or state["beta"]["queued"]:
        say(f"finding beta pauses, and carries on after the restart: {state['beta']['running'] or 'nothing'} now, "
            f"{state['beta']['queued']} queued")
    say("now" if state["running"] or state["queued"] or state["area_climbs"] else
        "now, as what it waited for has finished" if said else "now: nothing was running")
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
        except Exception as e:
            print(f"machines: sweep failed ({type(e).__name__}: {e})", flush=True)
        for pool in [_pool, *_old_pools]:
            if pool:
                pool.reap()
        _old_pools[:] = [p for p in _old_pools if p.nodes]


def compute_state() -> dict:
    return {"workers": [{"name": w.name, "remote": w.remote, "error": w.error,
                         "busy": f"meshing {w.doing}" if w.doing else ("waiting for a machine" if time.time() < w.wait_until else None)}
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


MACHINES = pullwork.Board(DATA / "compute" / "machines.sqlite", DATA / "compute" / "jobs", lambda: compute_settings()["compute.image"],
                          jobctl, machine_rules, lambda m: NOTIFIER.registered("machine", m, f" ({m['client'] or 'a worker script'})"))


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
CLIENT_REPORTS = {"error": (20, 600), "view": (120, 600)}   # at most this many of each from one address in this many seconds
MESH_VIEWS = DATA / "mesh-views.jsonl"       # each mesh shown in a visitor's browser, and how it was asked for
_client_reports: dict[tuple[str, str], list[float]] = {}
_client_lock = threading.Lock()


def _client_report(kind: str, sender: str, path: Path, line: dict) -> bool:
    """Append a report from a visitor's browser to path, unless that address has sent too many of its kind lately
    (CLIENT_REPORTS), starting a new file once it's over CLIENT_ERRORS_BYTES, the old one kept as <name>.1."""
    now = time.time()
    most, within = CLIENT_REPORTS[kind]
    with _client_lock:
        recent = [t for t in _client_reports.get((kind, sender), []) if now - t < within]
        if len(recent) >= most:
            _client_reports[(kind, sender)] = recent
            return False
        _client_reports[(kind, sender)] = recent + [now]
        for k in [k for k, ts in _client_reports.items() if not ts or now - ts[-1] > max(w for _, w in CLIENT_REPORTS.values())]:
            del _client_reports[k]
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
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        super().end_headers()
        if self.head_only:
            self.wfile = _Discard()     # do_HEAD puts the connection's own back

    def address_string(self) -> str:
        """The sender's address, for the log: the one in CLIENT_IP_HEADER if that's set and the request has it."""
        headers = getattr(self, "headers", None)
        return (CLIENT_IP_HEADER and headers and headers.get(CLIENT_IP_HEADER)) or self.client_address[0]

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

    def send_error_json(self, e: Exception, code: int | None = None) -> None:
        """A failure as JSON: its message without this server's paths, and the whole of it in the log. A ValueError
        is the client's fault (400) unless a code is given."""
        code = code or (400 if isinstance(e, ValueError) else 500)
        if code == 500:
            print(f"error: {self.command} {self.path}: {type(e).__name__}: {e}", flush=True)
        self.send_json({"error": str(e).replace(str(DATA), "data").replace(str(ROOT), ".")}, code)

    def send_file(self, path: Path, download=False, headers: dict | None = None):
        """Send a file, as its type if it's one the site serves (SERVED_TYPES; anything under frontend/), else as bytes to
        save; with Last-Modified, so that a browser asking If-Modified-Since gets a 304 instead of the file again."""
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
        cache = "no-cache, no-transform" if in_frontend and path.suffix == ".html" else f"public, max-age={FILE_CACHE}"
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
                else record_mesh_view(report, self.address_string(), editor))
        if kept is None:
            return self.send_json({"error": "no such mesh"}, 404)
        if not kept:
            return self.send_json({"error": "too many reports from here lately"}, 429)
        self.send_json({"kept": True}, 202)

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
                csp = page_csp()
                return self.send_file(FRONTEND / "index.html", headers={"Content-Security-Policy": csp} if csp else None)
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
                                       # the volunteers' page, for the header's Volunteer button, where this server takes them
                                       "volunteer_url": VOLUNTEER_URL if VOLUNTEER_PORT else None,
                                       "editing": "none" if READ_ONLY else "token" if EDIT_TOKEN else "open",
                                       "editor": not READ_ONLY and (not EDIT_TOKEN or self.is_editor()), "stopping": _stopping,
                                       "version": VERSION,
                                       # where the admin page is, and how to reach it over SSH: for editors only
                                       "admin": admin_link() if not READ_ONLY and (not EDIT_TOKEN or self.is_editor()) else None})
            if p == "/api/jobs":     # what a restart would interrupt now, and what it would only pause
                return self.send_json(jobs_state())
            # ---- projects
            if p == "/api/projects":
                return self.send_json(q("SELECT id,name,created,status,mesh_id,demo,kind,climb_id FROM projects ORDER BY created DESC"))
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
                proj["harvest"] = json.loads(proj["harvest"]) if proj.get("harvest") else None
                proj["progress"] = json.loads(proj["progress"]) if proj.get("progress") else None
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
            m = re.fullmatch(r"/photos/([0-9a-f]+)/(rejected/)?([A-Za-z0-9._-]+)", p)
            if m:   # rejected photos are named <reason>_<name>
                name = m[3].split("_", 1)[1] if m[2] and "_" in m[3] else m[3]
                if policy.source_access(settings().get("site.sources"), photo_entry(m[1], name)) != "show":
                    return self.send_json({"error": "not shown: it isn't clearly free to copy"}, 403)
                return self.send_file(PROJECTS / m[1] / ("rejected" if m[2] else "photos") / m[3])
            # ---- meshes
            if p == "/api/meshes":
                rows = q("SELECT * FROM meshes ORDER BY created DESC")
                links = q("SELECT l.mesh_id, l.note, c.id, c.name, c.grade FROM mesh_links l JOIN climbs c ON c.id=l.climb_id")
                by = {}
                for l in links:     # shared: an area linked it as on the same boulder as the climb the mesh was made for
                    by.setdefault(l["mesh_id"], []).append({"id": l["id"], "name": l["name"], "grade": l["grade"],
                                                            "shared": (l["note"] or "").startswith(SHARED)})
                on = {}
                for c in q("SELECT c.id, c.name, c.grade, c.area_path FROM climbs c WHERE c.area_path IN "
                           "(SELECT boulder FROM meshes WHERE boulder IS NOT NULL) ORDER BY c.name"):
                    on.setdefault(c.pop("area_path"), []).append(c)
                for r in rows:
                    r["climbs"] = by.get(r["id"], [])
                    if r["boulder"]:    # a boulder's own mesh: the climbs on it, and where each is seen from
                        views = viewpoints.load(MESHES / r["id"]) or {}
                        r["boulder_climbs"] = [{**c, "view": (views.get("climbs") or {}).get(c["id"])} for c in on.get(r["boulder"], [])]
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
                    r["splat"] = r.get("texture") == "full" and (MESHES / r["id"] / splat_mod.FILE).exists()
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
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/sources", p)
            if m:   # every photo, with where it came from: what crediting or re-hosting a mesh needs
                record = mesh_record(MESHES / m[1])
                return self.send_json(record) if record else self.send_json({"error": "not recorded for this mesh"}, 404)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/cleanup", p)
            if m:
                record = meshclean.load(served_dir(m[1]))
                return self.send_json(record) if record else self.send_json({"error": "no cleanup"}, 404)
            m = re.fullmatch(r"/api/uploads/([0-9a-f]{12})", p)
            if m:   # how much of a file sent in parts has come, to carry on from there
                state = upload_state(m[1])
                return self.send_json(state) if state else self.send_json({"error": "no such upload"}, 404)
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
                side = int(qs["max"][0]) if qs.get("max", [""])[0].isdigit() else 0
                if side in SHRINK_SIDES and is_image and f.is_file():
                    if not _has("PIL"):
                        return self.send_json({"error": "shrinking textures needs Pillow"}, 501)
                    f = shrunk_texture(f, side)
                if f.suffix.lower() == ".glb" and f.is_file():
                    if "cleaned" in qs and meshclean.available():
                        f = meshclean.write_cleaned(base) or f
                    if mode == "hidden":
                        f = meshclean.strip_textures(f, base / f"{f.stem}.untextured.glb")
                return self.send_file(f, download="download" in qs)
            if p == "/api/campaigns":
                return self.send_json([campaign_view(c) for c in q("SELECT * FROM campaigns ORDER BY created DESC")])
            # ---- climbs
            if p == "/api/climbs":   # the newest 500, or with q those of every climb with each of its words
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
                sql += " ORDER BY " + ", ".join(order + ["created DESC"]) + " LIMIT 500"
                return self.send_json([{**climb_row(r["id"]), "coords_doubt": doubts.get(r["id"])} for r in q(sql, *args, *(oargs if text else []))])
            if p == "/api/climbs/count":
                return self.send_json({"climbs": q("SELECT count(*) AS n FROM climbs", one=True)["n"], "doubtful": len(coord_doubts())})
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
        if p in ("/api/client-errors", "/api/mesh-views"):    # from any visitor, not only editors
            return self.client_report("error" if p == "/api/client-errors" else "view")
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
                return self.send_json({"restored": name.split("_", 1)[1]})

            # ---- meshes
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
                    record = meshclean.clean(served_dir(m[1]), print, force=True)
                except ValueError as e:
                    return self.send_json({"error": f"can't clean up this mesh: {e}"}, 400)
                return self.send_json(record) if record else self.send_json({"error": "only GLB meshes can be cleaned up"}, 400)
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
                what = (("videos" if b.get("videos_only") else "harvest") + ("+rebuild" if b.get("then_rebuild") else "")
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
                    return self.send_json(meshclean.set_removed(served_dir(m[1]), body.get("removed", [])))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
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
            meshclean.delete(served_dir(m[1]))
            return self.send_json({"deleted": True})
        m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/links/([0-9a-f]+)", p)
        if m:
            q("DELETE FROM mesh_links WHERE mesh_id=? AND climb_id=?", m[1], m[2], write=True)
            return self.send_json({"unlinked": True})
        m = re.fullmatch(r"/api/meshes/([0-9a-f]+)", p)
        if m:
            q("DELETE FROM meshes WHERE id=?", m[1], write=True)
            q("DELETE FROM mesh_links WHERE mesh_id=?", m[1], write=True)
            shutil.rmtree(MESHES / m[1], ignore_errors=True)
            return self.send_json({"deleted": m[1]})
        m = re.fullmatch(r"/api/projects/([0-9a-f]+)", p)
        if m:
            q("DELETE FROM projects WHERE id=?", m[1], write=True)
            shutil.rmtree(PROJECTS / m[1], ignore_errors=True)
            return self.send_json({"deleted": m[1]})
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


def volunteer_ready(purpose: str, ref: str) -> None:
    """Everything a harvest or a mesh's beta wanted of volunteers has come or failed: carry on with it."""
    if purpose == "harvest":
        reharvest_with_videos(ref)
    elif purpose == "beta":
        _beta_wake.set()


def reharvest_with_videos(pid: str) -> None:
    """Harvest a project again with the videos volunteers fetched for it, now in the cache. An area's climb is queued
    to harvest again and rebuild (if its photos changed), and a finished area runs only that; a stopped one waits
    to be started. Any other project is harvested again, to be rebuilt by hand."""
    proj = q("SELECT status FROM projects WHERE id=?", pid, one=True)
    if not proj or proj["status"] in ("queued", "running", "harvesting"):
        return
    item = q("SELECT campaign_id, source_id, status FROM campaign_items WHERE project_id=? LIMIT 1", pid, one=True)
    if item:
        if item["status"] == "waiting":
            set_item(item["campaign_id"], item["source_id"], status="pending", retry=1, redo="harvest+rebuild", was="waiting",
                     redo_started=0, note="volunteers have fetched what it waited for: harvesting it again, then meshing it")
        elif item["status"] == "harvested":     # harvested for its boulder's mesh, which isn't made yet: again first
            set_item(item["campaign_id"], item["source_id"], status="pending", retry=1, redo="harvest", was="harvested",
                     redo_started=0, note="volunteers have fetched more for it: harvesting it again for its boulder's mesh")
        elif item["status"] in ("done", "skipped", "failed"):
            set_item(item["campaign_id"], item["source_id"], status="pending", retry=1, redo="harvest+rebuild", was=item["status"],
                     redo_started=0, note="queued to harvest again with the videos volunteers fetched")
            q("UPDATE campaigns SET status='running', only_retries=1, note=NULL WHERE id=? AND status='done'", item["campaign_id"], write=True)
        return
    append_log(pid, "volunteers fetched the videos this harvest couldn't have: harvesting it again (Reconstruct uses them)")
    q("UPDATE projects SET status='queued', progress=NULL WHERE id=?", pid, write=True)
    queue_job({"type": "harvest", "pid": pid, "sources": sorted(HARVEST_SOURCES), "reconstruct": False, "again": True})


def volunteer_rules() -> dict:
    """The admin page's limits for volunteers (volunteers.RULES)."""
    s = settings().all()
    return {"per_hour": s["volunteers.per_hour"], "per_day": s["volunteers.per_day"],
            "provisional_per_hour": s["volunteers.provisional_per_hour"], "provisional_per_day": s["volunteers.provisional_per_day"],
            "provisional_work": s["volunteers.provisional_work"], "registrations_per_address": s["registration.per_address_day"],
            "max_waiting": s["registration.max_waiting"]}


VOLUNTEERS = volunteers_mod.Broker(DATA / "volunteers.sqlite", DATA / "volunteers", volunteer_delivered, volunteer_ready,
                                   paused=lambda: full(), rules=volunteer_rules,
                                   registered=lambda v: NOTIFIER.registered("volunteer", v, f" ({v['client'] or 'a volunteer script'})"))


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
    configure_workers()
    NOTIFIER.reconfigured()


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
            "notify": NOTIFIER.state(),
            "usage": {**_usage, "cap": data_cap() or None, "full": full()}, "timings": timings_summary()}


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


def admin_retexture(_: dict) -> dict:
    """Queue every mesh that can be textured from only its photos that are free to copy."""
    n = 0
    for m in q("SELECT id, source FROM meshes WHERE replaced_by IS NULL AND source LIKE 'project:%'"):
        d = MESHES / m["id"]
        if m["id"] in _retexturing or policy.mesh_permitted(mesh_record(d)):
            continue
        if retexture.usable(d, PROJECTS / m["source"].split(":", 1)[1] / "photos", video_info)[0]:
            _retexturing.add(m["id"])
            queue_job({"type": "retexture", "mid": m["id"]})
            n += 1
    return {"result": f"queued {n} mesh(es) to be textured from only the photos free to copy"}


def main() -> None:
    init_db()
    moved = settle_far_coordinates()
    if moved:
        print(f"{moved} climb(s) whose page's GPS was far from their area's now use their area's", flush=True)
    settled = settle_too_few_placed()
    if settled:
        print(f"{settled} area climb(s) that failed as COLMAP placed too few photos now count as skipped, as they would now", flush=True)
    apply_settings()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=reap_machines, daemon=True).start()
    threading.Thread(target=clean_existing_meshes, daemon=True).start()
    threading.Thread(target=orient_existing_meshes, daemon=True).start()
    threading.Thread(target=source_existing_meshes, daemon=True).start()
    threading.Thread(target=timings_existing_meshes, daemon=True).start()
    for d in MESHES.iterdir() if MESHES.exists() else []:
        beta_mod.carry_on(d)        # finding beta carries on after a restart, like areas
        scale_mod.interrupted(d)
    threading.Thread(target=beta_worker, daemon=True).start()
    threading.Thread(target=watch_usage, daemon=True).start()
    threading.Thread(target=snapshot_worker, daemon=True).start()
    threading.Thread(target=frames_worker, daemon=True).start()
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
                                      {"test": admin_test, "end-nodes": admin_end_nodes, "retexture": admin_retexture,
                                       "volunteer-add": admin_volunteer_add, "volunteer-revoke": admin_volunteer_revoke,
                                       "volunteers-skipped": admin_volunteers_skipped, "volunteers-details": admin_volunteers_details,
                                       "volunteers-cancel": admin_volunteers_cancel, "machine-add": admin_machine_add, "machine-show": admin_machine_show,
                                       "machine-revoke": admin_machine_revoke, "clear-rejected": admin_clear_rejected,
                                       "volunteer-approve": admin_volunteer_approve, "volunteer-deny": admin_volunteer_deny,
                                       "machine-approve": admin_machine_approve, "machine-deny": admin_machine_deny,
                                       "notify-topics": admin_notify_topics, "notify-test": admin_notify_test,
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
    server.serve_forever()
    sys.stdout.flush()
    os._exit(0)     # without waiting on workers' threads, whose jobs the next start marks interrupted


if __name__ == "__main__":
    main()
