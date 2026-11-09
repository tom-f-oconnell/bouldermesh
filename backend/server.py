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

import csv
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
import orientation  # noqa: E402
import policy  # noqa: E402
import provenance  # noqa: E402
import retexture  # noqa: E402
import settings as settings_mod  # noqa: E402
import pipeline  # noqa: E402
import climbs as climbs_mod  # noqa: E402
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
MESH_EXT = {".ply", ".obj", ".glb", ".gltf", ".stl"}
SAFE = re.compile(r"[^A-Za-z0-9._-]+")
HARVEST_SOURCES = harvest_mod.SOURCES
CAMPAIGN_ORDERS = ("popularity", "rating", "data")
CAMPAIGN_POLL = 2.0     # seconds the worker waits for a queued job before moving a campaign on
DATA_WINDOW = 8         # "data" order: how many waiting climbs' pages to have read before picking one
VIDEO_WORTH = 20        # "data" order: roughly how many usable photos a beta video gives
OUTAGE_LIMIT = 5        # stop a campaign after this many climbs in a row fail before finishing a harvest
CAMPAIGN_MIN_FREE = float(os.environ.get("CAMPAIGN_MIN_FREE_GB", "50")) * 2**30   # stop rather than fill a shared disk
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
          kind TEXT DEFAULT 'user',    -- user | harvest
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
          status TEXT DEFAULT 'pending',   -- pending | running | done | skipped | failed
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
        # job: the last job queued for it outside an area, to run again, as it was asked for, if a restart stops it
        for c, t in (("kind", "TEXT DEFAULT 'user'"), ("climb_id", "TEXT"), ("harvest", "TEXT"), ("progress", "TEXT"), ("job", "TEXT")):
            if c not in cols:
                con.execute(f"ALTER TABLE projects ADD COLUMN {c} {t}")
        cols = {r[1] for r in con.execute("PRAGMA table_info(meshes)")}
        for c in ("lat", "lng"):
            if c not in cols:
                con.execute(f"ALTER TABLE meshes ADD COLUMN {c} REAL")
        cols = {r[1] for r in con.execute("PRAGMA table_info(campaigns)")}
        if "only_retries" not in cols:
            con.execute("ALTER TABLE campaigns ADD COLUMN only_retries INTEGER DEFAULT 0")
        cols = {r[1] for r in con.execute("PRAGMA table_info(campaign_items)")}
        # redo: what it's queued to do again (REDO_NAMES); was: its status before; changed: its photos have
        # changed since it was last meshed or checked; redo_started: a rebuild that began, to carry on with
        for c, t in (("retry", "INTEGER DEFAULT 0"), ("redo", "TEXT"), ("was", "TEXT"), ("changed", "INTEGER DEFAULT 0"),
                     ("redo_started", "INTEGER DEFAULT 0")):
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


# ----------------------------------------------------------------- climbs helpers
def upsert_climb(c: Climb) -> str:
    row = q("SELECT id FROM climbs WHERE source=? AND source_id=?", c.source, c.source_id, one=True)
    if row and c.raw.get("stub"):   # an area-table summary never replaces a record we already have
        return row["id"]
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
    return cid


def climb_row(cid: str, full=False) -> dict | None:
    r = q("SELECT * FROM climbs WHERE id=?", cid, one=True)
    if not r:
        return None
    r["area_path"] = json.loads(r["area_path"] or "[]")
    data = json.loads(r.pop("data") or "{}")
    if full:
        r["data"] = data
        r["meshes"] = q("SELECT m.*, l.note FROM mesh_links l JOIN meshes m ON m.id=l.mesh_id WHERE l.climb_id=?", cid)
        r["projects"] = q("SELECT id,name,status,kind,mesh_id,harvest FROM projects WHERE climb_id=? ORDER BY created DESC", cid)
        r["videos_found"] = q("""SELECT cv.url, cv.found_by, cv.frames, v.title, v.uploader, v.license, v.height_cm, v.wingspan_cm,
                                    v.ape_index_cm, v.quotes, c.height_cm AS channel_height_cm, c.wingspan_cm AS channel_wingspan_cm,
                                    c.ape_index_cm AS channel_ape_index_cm, c.quotes AS channel_quotes
                                 FROM climb_videos cv LEFT JOIN videos v ON v.url=cv.url LEFT JOIN channels c ON c.channel_url=v.channel_url
                                 WHERE cv.climb_id=? ORDER BY cv.found_by, cv.created""", cid)
    else:
        r["n_images"] = len(data.get("images") or [])
        r["n_videos"] = len(data.get("videos") or [])
    r["n_links"] = q("SELECT COUNT(*) n FROM mesh_links WHERE climb_id=?", cid, one=True)["n"]
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


def run_reconstruct(pid: str, internet: bool = False, tracker: Tracker | None = None, resume: bool = False) -> None:
    """Make a project's mesh.

    Args:
        resume: carry on from the last step an interrupted run finished, rather than from scratch.
    """
    pdir = PROJECTS / pid
    q("UPDATE projects SET status='running' WHERE id=?", pid, write=True)
    log = lambda s: append_log(pid, s)  # noqa: E731
    tracker = tracker or tracker_for(pid, pipeline.steps())
    try:
        work = pdir / "work"
        if not resume:
            shutil.rmtree(work, ignore_errors=True)
        mid = new_id()
        build = _executor.reconstruct if current_worker().remote and _executor else pipeline.reconstruct
        mesh_path, demo = build(pdir / "photos", work, MESHES / mid, log, USE_GPU, internet=internet, tracker=tracker)
        proj = q("SELECT name, climb_id, mesh_id FROM projects WHERE id=?", pid, one=True)
        q("INSERT INTO meshes(id,name,filename,source,created,size,demo,lat,lng) VALUES(?,?,?,?,?,?,?,?,?)", mid, proj["name"],
          mesh_path.name, f"project:{pid}", time.time(), mesh_path.stat().st_size, int(demo), None, None, write=True)
        if proj["climb_id"]:
            link_mesh(mid, proj["climb_id"], "auto-linked from reconstruction project")
            log(f"mesh linked to climb {proj['climb_id']}")
        if proj["mesh_id"] and proj["mesh_id"] != mid and q("SELECT id FROM meshes WHERE id=?", proj["mesh_id"]):
            supersede(proj["mesh_id"], mid)
            log(f"this replaces mesh {proj['mesh_id']}, which stays as an older version")
        clean_mesh(mid, log)
        record_sources(mid, pid, log)
        tracker.finish()     # before it's marked done, so that anything waiting for that sees it all finished
        log("done")
        q("UPDATE projects SET status='done', mesh_id=?, demo=? WHERE id=?", mid, int(demo), pid, write=True)
        q("UPDATE campaign_items SET status='done', mesh_id=?, retry=0, note=NULL, updated=? WHERE project_id=? AND status='failed'",
          mid, time.time(), pid, write=True)   # made by hand since its area gave up on it
        if not demo:
            queue_scale(mid)
    except compute.ComputeUnavailable as e:    # not the climb's failure: it waits for a machine
        log(f"waiting for a machine to mesh it on: {e}")
        q("UPDATE projects SET status='stopped' WHERE id=?", pid, write=True)
        raise
    except Exception as e:
        diagnostics.log_failure(log, e, DATA)
        tracker.finish("failed")
        q("UPDATE projects SET status='failed' WHERE id=?", pid, write=True)


CHANNEL_TTL = 30 * 86400    # how long a channel's description is trusted before it's read again


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
            q("""INSERT INTO climb_videos(climb_id,url,found_by,frames,created) VALUES(?,?,?,?,?)
                 ON CONFLICT(climb_id,url) DO UPDATE SET frames=COALESCE(excluded.frames,frames)""",
              climb_id, v["url"], v.get("found_by") or "page", v.get("frames"), time.time(), write=True)
            if v.get("channel_url"):
                record_channel(v["channel_url"])
            if any(s[k] is not None for k in ("height_cm", "wingspan_cm", "ape_index_cm")):
                log(f"climber's size in {v['url']}: {s['quotes']}")
        except Exception as e:
            log(f"couldn't record video {v.get('url')}: {type(e).__name__}: {e}")


def record_channel(url: str) -> None:
    """Read a channel's description for the climber's size, unless it was read in the last CHANNEL_TTL."""
    seen = q("SELECT fetched FROM channels WHERE channel_url=?", url, one=True)
    if seen and time.time() - (seen["fetched"] or 0) < CHANNEL_TTL:
        return
    ch = youtube_mod.channel(url)
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
    found = youtube_mod.search(c, log)
    record_videos(climb_id, [{"url": v.url, "found_by": "search", "title": v.title, "uploader": v.credit} for v in found], log)
    known = {r["url"] for r in q("SELECT url FROM videos WHERE description IS NOT NULL")}
    for url in sorted(used - known):
        try:
            record_videos(climb_id, [{**youtube_mod.metadata(url), "url": url, "found_by": "page"}], log)
        except Exception as e:
            log(f"couldn't read the details of {url}: {e}")
    return [v.url for v in found if (youtube_mod.video_id(v.url) or v.url) not in have]


def run_harvest(pid: str, sources: set[str], reconstruct: bool, relevance_filter: bool = True, resume: bool = False,
                again: bool = False) -> None:
    """Gather a project's photos, and optionally make its mesh.

    Args:
        resume: carry on after an interruption. A finished harvest isn't repeated. A partial one
            starts again, which is quick, as its downloads are cached.
        again: harvest a project that was harvested before, from scratch, adding to its log.
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
            summary = harvest_mod.harvest(c, pdir, log, sources, relevance_filter=relevance_filter, tracker=tracker)
            videos = summary.pop("videos", [])
            record_videos(proj["climb_id"], videos, log)
            summary["videos"] = [{k: v.get(k) for k in ("url", "found_by", "title", "uploader", "frames", "license")} for v in videos]
            q("UPDATE projects SET harvest=?, status='harvested' WHERE id=?", json.dumps(summary), pid, write=True)
        verdict = (summary.get("diversity") or {}).get("verdict", "unknown")
        log(f"harvest complete: {summary['images']} image(s), verdict {verdict}")
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
        self.boulder: str | None = None    # which no other worker takes a climb on meanwhile
        self.doing: str | None = None
        self.job: dict | None = None       # a job someone asked for that it's running: {job, started, from (% done)}
        self.error: str | None = None
        self.wait_until = 0.0
        self.retired = False


_first = Worker("this server", control=shared_control)   # what threads that aren't workers count as
_workers: list[Worker] = [_first]
_workers_lock = threading.Lock()
_claim_lock = threading.Lock()
_here = threading.local()
_pool: compute.Pool | None = None
_old_pools: list[compute.Pool] = []
_executor: compute.RemoteExecutor | None = None
_pool_key = None


def current_worker() -> Worker:
    return getattr(_here, "worker", None) or _first


def busy_campaigns() -> set[str]:
    return {w.campaign for w in _workers if w.campaign}


def create_campaign(area: str, order: str = "popularity", csv_text: str | None = None, start: bool = True) -> dict:
    """List a Mountain Project area's boulder problems, in one request, and queue them for meshing.

    Args:
        area: its URL or id.
        order: "popularity" (MP's own ranking, as tick counts are only on each climb's own page),
            "rating" (average stars), or "data" (most photos and videos first).
        csv_text: a Route Finder export to use instead of fetching one.
        start: start meshing as soon as nothing else is queued.
    """
    if order not in CAMPAIGN_ORDERS:
        raise ValueError(f"order must be one of {', '.join(CAMPAIGN_ORDERS)}")
    aid = mp_mod.area_id(area or "")
    if not aid and not csv_text:
        raise ValueError("enter a Mountain Project area URL, like https://www.mountainproject.com/area/105720495/joshua-tree-national-park")
    recs = mp_mod.parse_area_csv(csv_text) if csv_text else mp_mod.export_area_csv(aid, sort="popularity")
    if not recs:
        raise ValueError("Mountain Project lists no boulder problems under that area")
    common = os.path.commonprefix([c.area_path for c in recs])
    cid = new_id()
    note = "Mountain Project's export stops at 1000 climbs: these are the most popular 1000" if len(recs) >= 1000 else None
    q("INSERT INTO campaigns(id,name,area_id,ord,created,note,status) VALUES(?,?,?,?,?,?,?)", cid,
      common[-1].strip("* ") if common else f"area {aid}", aid, order, time.time(), note, "running" if start else "stopped", write=True)
    rows = [(cid, c.source_id, i + 1, c.name, c.grade, c.rating, c.url,
             "/".join(c.area_path) if c.area_path and not GROUP_AREA.search(c.area_path[-1]) else None,
             json.dumps(c.to_dict())) for i, c in enumerate(recs)]
    with _db_lock, db() as con:
        con.executemany("INSERT OR IGNORE INTO campaign_items(campaign_id,source_id,rank,name,grade,stars,url,boulder,data) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", rows)
    return q("SELECT * FROM campaigns WHERE id=?", cid, one=True)


def ordered_pending(camp: dict) -> list[dict]:
    """A campaign's waiting climbs, next first."""
    items = q("SELECT rank,source_id,name,grade,stars,url,media,project_id,retry,redo,boulder FROM campaign_items "
              "WHERE campaign_id=? AND status='pending' ORDER BY rank", camp["id"])
    if camp["ord"] == "rating":
        items.sort(key=lambda i: (-(i["stars"] or 0), i["rank"]))
    elif camp["ord"] == "data":
        items.sort(key=lambda i: (i["media"] is None, -(i["media"] or 0), i["rank"]))
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
    """Mesh one climb, unless a mesh of its boulder already exists. Whatever this finishes is
    saved like any other mesh, so stopping later loses nothing but the climb in progress."""
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
        have = q("SELECT mesh_id FROM mesh_links WHERE climb_id=? LIMIT 1", climb_id, one=True)
        same = full["boulder"] and q("SELECT name,mesh_id FROM campaign_items WHERE boulder=? AND mesh_id IS NOT NULL LIMIT 1",
                                     full["boulder"], one=True)
        made = q("SELECT id FROM meshes WHERE source=? LIMIT 1", f"project:{pid}", one=True) if resume else None
        if made:    # interrupted in the moment between saving its mesh and recording that
            q("UPDATE projects SET status='done', mesh_id=? WHERE id=?", made["id"], pid, write=True)
            set_item(cid, sid, status="done", climb_id=climb_id, mesh_id=made["id"])
        elif have:
            set_item(cid, sid, status="done", climb_id=climb_id, mesh_id=have["mesh_id"], note="already had a mesh")
        elif same:
            link_mesh(same["mesh_id"], climb_id, f"same boulder as {same['name']}")
            set_item(cid, sid, status="done", climb_id=climb_id, mesh_id=same["mesh_id"], note=f"same boulder as {same['name']}")
        else:
            if not resume:
                pid = new_id()
                (PROJECTS / pid / "photos").mkdir(parents=True)
                q("INSERT INTO projects(id,name,created,kind,climb_id,status) VALUES(?,?,?,?,?,'queued')",
                  pid, f"{c.name} (harvest)"[:80], time.time(), "harvest", climb_id, write=True)
            set_item(cid, sid, climb_id=climb_id, project_id=pid)
            if item.get("retry"):
                append_log(pid, "-- trying again")
            try:
                run_harvest(pid, set(HARVEST_SOURCES), True, resume=resume)
            except Cancelled:
                q("UPDATE projects SET status='stopped' WHERE id=?", pid, write=True)
                set_item(cid, sid, status="pending", note="stopped partway: carries on from where it got to when resumed")
                return
            except compute.ComputeUnavailable as e:
                q("UPDATE projects SET status='stopped' WHERE id=?", pid, write=True)
                set_item(cid, sid, status="pending", note=f"waiting for a machine to mesh it on: {e}")
                raise
            p = q("SELECT status,mesh_id,harvest FROM projects WHERE id=?", pid, one=True)
            if p["status"] != "failed":     # kept if it failed, so that trying again starts from its last finished step
                shutil.rmtree(PROJECTS / pid / "work", ignore_errors=True)   # most of a job's disk use, and only useful to itself
            if p["status"] == "done":
                set_item(cid, sid, status="done", mesh_id=p["mesh_id"])
            elif p["status"] == "harvested":
                set_item(cid, sid, status="skipped", note="photos don't show enough different viewpoints")
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


def _claim(w: Worker):
    """Choose this worker's next climb, and mark it as running, under _claim_lock: an area's next waiting
    climb, from the area fewest other workers are on, but not one on a boulder another worker is
    meshing, which would make two meshes of it. Areas with nothing left are settled here.

    Returns:
        (area, climb), or None.
    """
    others = [x for x in _workers if x is not w]
    camps = q("SELECT * FROM campaigns WHERE status='running' ORDER BY created")
    camps.sort(key=lambda c: sum(x.campaign == c["id"] for x in others))
    for camp in camps:
        if shutil.disk_usage(DATA).free < CAMPAIGN_MIN_FREE:
            q("UPDATE campaigns SET status='stopped', note=? WHERE id=?",
              f"stopped: under {CAMPAIGN_MIN_FREE / 2**30:.0f} GB of disk left", camp["id"], write=True)
            continue
        if camp["ord"] == "data" and not camp["only_retries"] and not any(x.campaign for x in others):
            read_ahead(camp)
        items = ordered_pending(camp)
        waiting = bool(items)
        if camp["only_retries"]:
            items = [i for i in items if i["retry"]]
        taken = {x.boulder for x in others if x.boulder}
        free = [i for i in items if not (i["boulder"] and i["boulder"] in taken)]
        if not free:
            if not items and not any(x.campaign == camp["id"] for x in others):
                q("UPDATE campaigns SET status=?, only_retries=0, note=NULL WHERE id=? AND status='running'",
                  "stopped" if waiting else "done", camp["id"], write=True)
            continue
        item = free[0]
        set_item(camp["id"], item["source_id"], status="running")
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
        run_campaign_item(camp, item)
        w.error = None
    except compute.ComputeUnavailable as e:     # the climb waits; so does this worker, for a while
        w.error, w.wait_until = f"{time.strftime('%H:%M')}: {e}", time.time() + COMPUTE_WAIT
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
    if proj["kind"] != "harvest" or proj["harvest"]:    # otherwise it gets its photos first
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
    if proj["kind"] == "harvest" and not proj["harvest"]:
        run_harvest(pid, set(HARVEST_SOURCES), True, resume=True)
    else:
        run_reconstruct(pid, internet=proj["kind"] == "harvest", resume=True)


REDO_NAMES = {"harvest": "harvest again", "videos": "look for new videos", "rebuild": "rebuild", "rebuild-changed": "rebuild",
              "harvest+rebuild": "harvest again and rebuild", "videos+rebuild": "look for new videos and rebuild"}


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
    rows = q("SELECT ci.source_id, ci.status FROM campaign_items ci JOIN projects p ON p.id=ci.project_id "
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
    h = q("SELECT harvest FROM projects WHERE id=?", pid, one=True)["harvest"]
    return (json.loads(h).get("diversity") or {}).get("verdict") if h else None


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
    run_reconstruct(pid, internet=True, resume=resume)
    p = q("SELECT status,mesh_id FROM projects WHERE id=?", pid, one=True)
    if p["status"] == "done" and p["mesh_id"] and p["mesh_id"] != row["mesh_id"]:
        shutil.rmtree(PROJECTS / pid / "work", ignore_errors=True)
        return {"status": "done", "mesh_id": p["mesh_id"], "note": "rebuilt" + (": the one before is kept as an older version" if row["mesh_id"] else ""),
                "changed": 0}
    if row["mesh_id"]:     # it still has the mesh it had
        q("UPDATE projects SET status='done', mesh_id=? WHERE id=?", row["mesh_id"], pid, write=True)
    return {"status": "done" if row["mesh_id"] else "failed", "note": f"rebuilding failed: {last_failure(pid)}"}


def run_redo(camp: dict, item: dict) -> None:
    """Harvest or rebuild again one of an area's climbs that has been through it before."""
    cid, sid = camp["id"], item["source_id"]
    row = q("SELECT source_id,climb_id,project_id,mesh_id,redo,was,changed,redo_started FROM campaign_items "
            "WHERE campaign_id=? AND source_id=?", cid, sid, one=True)
    row["was"] = row["was"] or "done"
    log = lambda s: append_log(row["project_id"], s)  # noqa: E731
    jobctl.begin(low_priority=True)
    try:
        set_item(cid, sid, status="running", note=None)
        first, *then = row["redo"].split("+")
        if first in ("harvest", "videos") and not row["redo_started"]:    # a rebuild that started was harvested already
            end = _harvest_again(row, log)
            if then and end.get("changed"):
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
        beta_mod.find(mesh_dir, PROJECTS / pid / "photos", log)
    except Cancelled:
        beta_mod.save(mesh_dir, {**(beta_mod.load(mesh_dir) or {"videos": []}), "status": "stopped", "progress": None})
    except Exception as e:
        log(f"FAILED: {type(e).__name__}: {e}")
        beta_mod.save(mesh_dir, {**(beta_mod.load(mesh_dir) or {"videos": []}), "status": f"failed: {type(e).__name__}: {e}", "progress": None})


def queue_scale(mid: str) -> bool:
    """Queue finding a reconstructed mesh's scale (scale.py), unless it's being found; whether it was queued."""
    if not (MESHES / mid / "cameras.json").exists() or (scale_mod.load(MESHES / mid) or {}).get("status") in ("queued", "running"):
        return False
    scale_mod.save(MESHES / mid, {"status": "queued", "progress": None})
    queue_job({"type": "scale", "mid": mid})
    return True


def mesh_videos(mid: str) -> dict:
    """What each of a mesh's videos, and its uploader's channel, says about the climber's size, by url."""
    record = provenance.load(MESHES / mid) or {}
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
    climbs = [json.loads(r["data"]) for r in q("SELECT c.data FROM mesh_links l JOIN climbs c ON c.id=l.climb_id WHERE l.mesh_id=?", mid)]
    try:
        scale_mod.estimate(mesh_dir, PROJECTS / pid / "photos", PROJECTS / pid / "masks", climbs, mesh_videos(mid), log)
    except Cancelled:
        scale_mod.save(mesh_dir, {**(scale_mod.load(mesh_dir) or {}), "status": "stopped", "progress": None})
    except Exception as e:
        log(f"FAILED: {type(e).__name__}: {e}")
        scale_mod.save(mesh_dir, {**(scale_mod.load(mesh_dir) or {}), "status": f"failed: {type(e).__name__}: {e}", "progress": None})


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
        retexture.retexture(mesh_dir, PROJECTS / m["source"].split(":", 1)[1] / "photos", log)
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
    return policy.texture_mode(settings().get("site.textures"), source == "upload", provenance.load(MESHES / mid),
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


def video_entry(url: str) -> dict:
    lic = q("SELECT license FROM videos WHERE url=?", url, one=True)
    return {"source": "youtube", "license": lic["license"] if lic else None}


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
    worked = q("SELECT rank,name,grade,stars,url,climb_id,mesh_id,project_id,status,note,updated FROM campaign_items "
               "WHERE campaign_id=? AND status NOT IN ('pending','running') ORDER BY updated DESC LIMIT 300", cid)
    running = q("SELECT rank,name,grade,stars,project_id FROM campaign_items WHERE campaign_id=? AND status='running' ORDER BY updated", cid)
    for r in running:
        p = q("SELECT status,progress FROM projects WHERE id=?", r["project_id"], one=True) if r["project_id"] else None
        r["progress"] = json.loads(p["progress"]) if p and p["progress"] else None
    return {**camp, "counts": counts, "again": again, "worked": worked, "current": running[0] if running else None,
            "running": running, "next": ordered_pending(camp)[:10]}


def run_job(job: dict) -> None:
    """A job someone asked for, on this thread's worker."""
    w = current_worker()
    w.job = {"job": job, "started": time.time(), "from": job_progress(job) or 0.0}
    jobctl.begin(low_priority=job["type"] in ("beta", "retexture", "scale"))
    try:
        if job["type"] == "beta":
            run_beta(job["mid"])
        elif job["type"] == "scale":
            run_scale(job["mid"])
        elif job["type"] == "retexture":
            run_retexture(job["mid"])
        elif job["type"] == "reconstruct":
            run_reconstruct(job["pid"], job.get("internet", False), resume=job.get("resume", False))
        elif job["type"] == "harvest":
            run_harvest(job["pid"], set(job["sources"]), job["reconstruct"], job.get("filter", True), resume=job.get("resume", False))
        elif job["type"] == "retry":
            run_retry(job["pid"])
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
        try:
            if not next_campaign_step() and w.remote:
                time.sleep(CAMPAIGN_POLL)
        except (Exception, Cancelled) as e:  # never let the worker die
            print(f"campaign error on {w.name}: {e!r}", flush=True)


# ------------------------------------------------------------------ stopping, and what it would interrupt
STOP_WAIT = float(os.environ.get("STOP_WAIT_MINUTES", "10")) * 60   # docker-compose.yml's stop_grace_period is longer
_stopping: dict | None = None    # once a stop has begun: {since, until, waiting_for}
AGAIN = {"reconstruct": "Retry on its project", "harvest": "Retry on its project", "retry": "Retry on its project",
         "beta": "Find beta on the Beta tab", "retexture": "Make textures... on the admin page", "scale": "Find size in the viewer"}


def job_progress(job: dict) -> float | None:
    """How far a job someone asked for has got, in percent, if it says."""
    if job.get("pid"):
        try:
            return float(json.loads(q("SELECT progress FROM projects WHERE id=?", job["pid"], one=True)["progress"])["overall"])
        except (TypeError, ValueError, KeyError):
            return None
    if job["type"] == "beta":
        m = re.match(r"video (\d+) of (\d+)", (beta_mod.load(MESHES / job["mid"]) or {}).get("progress") or "")
        return 100 * (int(m[1]) - 1) / int(m[2]) if m else None
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
    return {"running": [job_info(r["job"], r) for r in runs if r], "queued": [job_info(j) for j in waiting],
            "area_climbs": q("SELECT c.name AS area, i.name, i.project_id FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id "
                             "WHERE i.status='running' ORDER BY i.updated"),
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


def configure_workers() -> None:
    """Start or retire workers, and make the pool of remote machines, to match the settings."""
    global _pool, _executor, _pool_key
    s = settings().all()
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
            try:
                provider = compute.provider_for(s, settings().secret, DATA / "compute", server_id())
            except Exception as e:
                print(f"compute: can't use {s['compute.provider']}: {e}", flush=True)
            if provider and s["compute.remote_workers"]:
                _pool = compute.Pool(provider, s["compute.remote_workers"], s["compute.idle_minutes"])
                _executor = compute.RemoteExecutor(_pool, s["compute.image"], s["compute.command"], jobctl)
            _pool_key = key
        want = {False: s["compute.local_workers"], True: s["compute.remote_workers"] if _pool else 0}
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
            prov = compute.provider_for(settings().all(), settings().secret, DATA / "compute", server_id())
            for iid in prov.leftovers():
                print(f"compute: ending {iid}, left running from before a restart", flush=True)
                prov.stop(compute.Node(host="", user="", id=iid))
        except Exception as e:
            print(f"compute: couldn't check for machines left running: {e}", flush=True)
    while True:
        time.sleep(60)
        for pool in [_pool, *_old_pools]:
            if pool:
                pool.reap()
        _old_pools[:] = [p for p in _old_pools if p.nodes]


def compute_state() -> dict:
    return {"workers": [{"name": w.name, "remote": w.remote, "error": w.error,
                         "busy": f"meshing {w.doing}" if w.doing else ("waiting for a machine" if time.time() < w.wait_until else None)}
                        for w in _workers if not w.retired],
            "pool": _pool.status() if _pool else None}


# ----------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "BoulderMesh/0.2"

    # -- helpers ----------------------------------------------------------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, download=False):
        if not path.is_file():
            return self.send_json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix in (".ply", ".obj", ".stl"):
            ctype = "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_UPLOAD:
            raise ValueError(f"upload exceeds {MAX_UPLOAD // 2**20} MB")
        return self.rfile.read(n)

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

    def read_json(self) -> dict:
        body = self.read_body()
        return json.loads(body or b"{}")

    def log_message(self, fmt, *args):  # quieter
        if not any(x in (args[0] if args else "") for x in ("/api/projects", "/api/campaigns")):
            super().log_message(fmt, *args)

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                return self.send_file(FRONTEND / "index.html")
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
                                       "opencv": _has("cv2"), "pillow": _has("PIL"),
                                       "policy": {"textures": settings().get("site.textures"), "sources": settings().get("site.sources")},
                                       "stopping": _stopping})
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
                proj["log"] = read_log(m[1], proj.get("log") or "")
                proj["failure"] = (last_failure(m[1]) if proj["status"] == "failed" else
                                   last_failure(m[1], "INTERRUPTED: ") if proj["status"] == "interrupted" else None)
                proj["harvest"] = json.loads(proj["harvest"]) if proj.get("harvest") else None
                proj["progress"] = json.loads(proj["progress"]) if proj.get("progress") else None
                rejected = PROJECTS / m[1] / "rejected"
                proj["filtered"] = [{"file": f.name, "why": relevance_mod.REASONS[f.name.split("_", 1)[0]]}
                                    for f in (sorted(rejected.iterdir()) if rejected.exists() else [])
                                    if f.name.split("_", 1)[0] in relevance_mod.REASONS]
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
                links = q("SELECT l.mesh_id, c.id, c.name, c.grade FROM mesh_links l JOIN climbs c ON c.id=l.climb_id")
                by = {}
                for l in links:
                    by.setdefault(l["mesh_id"], []).append({"id": l["id"], "name": l["name"], "grade": l["grade"]})
                for r in rows:
                    r["climbs"] = by.get(r["id"], [])
                    r["texture"] = texture_mode(r["id"], r["source"])
                    r["cleanup"] = meshclean.summary(meshclean.load(served_dir(r["id"], r["texture"])))
                    r["orientation"] = orientation.load(MESHES / r["id"])
                    r["sources"] = provenance.summary(provenance.load(MESHES / r["id"]))
                    r["beta"] = beta_mod.summary(beta_mod.load(MESHES / r["id"]))
                    r["marked"] = len(beta_mod.load_holds(MESHES / r["id"])["holds"])
                    r["scale"] = scale_mod.summary(scale_mod.load(MESHES / r["id"]))
                    r["cameras_kept"] = (MESHES / r["id"] / orientation.CAMERAS_FILE).exists()
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
                record = provenance.load(MESHES / m[1])
                return self.send_json(record) if record else self.send_json({"error": "not recorded for this mesh"}, 404)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/cleanup", p)
            if m:
                record = meshclean.load(served_dir(m[1]))
                return self.send_json(record) if record else self.send_json({"error": "no cleanup"}, 404)
            m = re.fullmatch(r"/meshes/([0-9a-f]+)/([A-Za-z0-9._-]+)", p)
            if m:
                mid, name = m[1], m[2]
                if name.startswith("beta_"):     # a video's frame, drawn on: shown as the video's licence allows
                    v = next((v for v in (beta_mod.load(MESHES / mid) or {}).get("videos", []) if v.get("image") == name), None)
                    if not v or policy.source_access(settings().get("site.sources"), video_entry(v["url"])) != "show":
                        return self.send_json({"error": "not shown: it isn't clearly free to copy"}, 403)
                    return self.send_file(MESHES / mid / name)
                mode = texture_mode(mid)
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
            if p == "/api/climbs":
                sql, args = "SELECT id FROM climbs", []
                conds = []
                if qs.get("q"):
                    conds.append("(name LIKE ? OR boulder_name LIKE ? OR area_path LIKE ?)")
                    args += [f"%{qs['q'][0]}%"] * 3
                if qs.get("source"):
                    conds.append("source=?")
                    args.append(qs["source"][0])
                if qs.get("meshed"):
                    conds.append("id IN (SELECT climb_id FROM mesh_links)")
                if conds:
                    sql += " WHERE " + " AND ".join(conds)
                sql += " ORDER BY created DESC LIMIT 500"
                return self.send_json([climb_row(r["id"]) for r in q(sql, *args)])
            m = re.fullmatch(r"/api/climbs/([0-9a-f]+)", p)
            if m:
                r = climb_row(m[1], full=True)
                if not r:
                    return self.send_json({"error": "not found"}, 404)
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
            return self.send_json({"error": str(e)}, 500)

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        p = urlparse(self.path).path
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

            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/photos", p)
            if m:
                pid = m[1]
                if not q("SELECT 1 FROM projects WHERE id=?", pid, one=True):
                    return self.send_json({"error": "not found"}, 404)
                _, files = self.read_multipart()
                saved = []
                for fname, data in files:
                    if Path(fname).suffix.lower() not in PHOTO_EXT:
                        continue
                    (PROJECTS / pid / "photos" / fname).write_bytes(data)
                    saved.append(fname)
                return self.send_json({"saved": saved})

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
                queue_job({"type": "reconstruct", "pid": pid, "internet": proj["kind"] == "harvest"})
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
                fields, files = self.read_multipart()
                if not files:
                    return self.send_json({"error": "no file"}, 400)
                mid = new_id()
                d = MESHES / mid
                d.mkdir(parents=True)
                main = None
                for fname, data in files:
                    (d / fname).write_bytes(data)
                    if Path(fname).suffix.lower() in MESH_EXT and main is None:
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
                return self.send_json({"id": mid, "name": name, "filename": main}, 201)

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
                    camp = create_campaign(b.get("area") or "", b.get("order") or "popularity", b.get("csv"), bool(b.get("start", True)))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                return self.send_json(campaign_view(camp), 201)
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
            m = re.fullmatch(r"/api/campaigns/([0-9a-f]+)/(harvest-again|rebuild)", p)
            if m:
                if not campaign_status(m[1]):
                    return self.send_json({"error": "not found"}, 404)
                b = self.read_json()
                what = (("videos" if b.get("videos_only") else "harvest") + ("+rebuild" if b.get("then_rebuild") else "")
                        if m[2] == "harvest-again" else ("rebuild-changed" if b.get("changed_only") else "rebuild"))
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
                if (beta_mod.load(MESHES / m[1]) or {}).get("status") in ("queued", "running"):
                    return self.send_json({"error": "already finding it"}, 409)
                beta_mod.save(MESHES / m[1], {"status": "queued", "videos": [], "progress": None})
                queue_job({"type": "beta", "mid": m[1]})
                return self.send_json({"queued": True}, 202)
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
            return self.send_json({"error": str(e)}, 500)

    # -- PATCH -------------------------------------------------------------
    def do_PATCH(self):
        p = urlparse(self.path).path
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
            return self.send_json({"error": str(e)}, 500)

    # -- DELETE ------------------------------------------------------------
    def do_DELETE(self):
        p = urlparse(self.path).path
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


def admin_state() -> dict:
    """What the admin page shows: settings, which credentials are set, the workers, and how many meshes
    each texture setting would affect."""
    counts = dict.fromkeys(("meshes", "all_permitted", "variant", "hidden_if_permitted", "can_retexture", "need_rebuild"), 0)
    for m in q("SELECT id, source FROM meshes WHERE replaced_by IS NULL"):
        d, upload = MESHES / m["id"], m["source"] == "upload"
        ok, variant = upload or bool(policy.mesh_permitted(provenance.load(d))), retexture.has_variant(d)
        counts["meshes"] += 1
        counts["all_permitted"] += ok
        counts["variant"] += variant and not ok
        counts["hidden_if_permitted"] += not ok and not variant
        if not ok and m["source"].startswith("project:"):
            found, why = retexture.usable(d, PROJECTS / m["source"].split(":", 1)[1] / "photos")
            counts["can_retexture"] += bool(found)
            counts["need_rebuild"] += bool(why and "rebuild" in why)
    return {"settings": settings().all(), "secrets": settings().secrets_status(), "compute": compute_state(),
            "policy": counts, "retexture_queued": len(_retexturing), "site_port": int(os.environ.get("PORT", "8470"))}


def admin_test(_: dict) -> dict:
    prov = compute.provider_for(settings().all(), settings().secret, DATA / "compute", server_id())
    return {"result": prov.test() if prov else "nothing to test: this server does everything itself"}


def admin_end_nodes(_: dict) -> dict:
    ended = 0
    for pool in [_pool, *_old_pools]:
        if pool:
            ended += len([n for n in pool.nodes if not n.busy])
            pool.close()
    prov = compute.provider_for(settings().all(), settings().secret, DATA / "compute", server_id())
    busy = {n.id for pool in [_pool, *_old_pools] if pool for n in pool.nodes}
    for iid in (prov.leftovers() if prov else []):
        if iid not in busy:
            prov.stop(compute.Node(host="", user="", id=iid))
            ended += 1
    return {"result": f"ended {ended} machine(s); any in use end when their climb is done"}


def admin_retexture(_: dict) -> dict:
    """Queue every mesh that can be textured from only its photos that are free to copy."""
    n = 0
    for m in q("SELECT id, source FROM meshes WHERE replaced_by IS NULL AND source LIKE 'project:%'"):
        d = MESHES / m["id"]
        if m["id"] in _retexturing or policy.mesh_permitted(provenance.load(d)):
            continue
        if retexture.usable(d, PROJECTS / m["source"].split(":", 1)[1] / "photos")[0]:
            _retexturing.add(m["id"])
            queue_job({"type": "retexture", "mid": m["id"]})
            n += 1
    return {"result": f"queued {n} mesh(es) to be textured from only the photos free to copy"}


def main() -> None:
    init_db()
    configure_workers()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=reap_machines, daemon=True).start()
    threading.Thread(target=clean_existing_meshes, daemon=True).start()
    threading.Thread(target=orient_existing_meshes, daemon=True).start()
    threading.Thread(target=source_existing_meshes, daemon=True).start()
    for d in MESHES.iterdir() if MESHES.exists() else []:
        beta_mod.interrupted(d)     # a search for beta that a restart stopped, as jobs don't carry on
        scale_mod.interrupted(d)
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8470"))
    mode = "COLMAP + OpenMVS" if pipeline.real_pipeline_ready() else "DEMO (COLMAP/OpenMVS not found)"
    print(f"BoulderMesh on http://{host}:{port}  data={DATA}  pipeline={mode}", flush=True)
    in_docker = Path("/.dockerenv").exists()
    admin_mod.start(admin_mod.Context(settings(), admin_state, configure_workers,
                                      {"test": admin_test, "end-nodes": admin_end_nodes, "retexture": admin_retexture}),
                    os.environ.get("ADMIN_HOST", "0.0.0.0" if in_docker else "127.0.0.1"), ADMIN_PORT)
    server = ThreadingHTTPServer((host, port), Handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=stop_when_idle, args=(server,), daemon=True).start())
    server.serve_forever()
    sys.stdout.flush()
    os._exit(0)     # without waiting on workers' threads, whose jobs the next start marks interrupted


if __name__ == "__main__":
    main()
