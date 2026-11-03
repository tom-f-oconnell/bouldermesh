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
import beta as beta_mod  # noqa: E402
import diagnostics  # noqa: E402
import meshclean  # noqa: E402
import orientation  # noqa: E402
import provenance  # noqa: E402
import pipeline  # noqa: E402
import climbs as climbs_mod  # noqa: E402
from climbs import harvest as harvest_mod  # noqa: E402
from climbs import relevance as relevance_mod  # noqa: E402
from climbs import youtube as youtube_mod  # noqa: E402
from progress import Tracker  # noqa: E402
from climbs import kaya as kaya_mod  # noqa: E402
from climbs import mountainproject as mp_mod  # noqa: E402
from climbs.common import Cancelled, Climb, jobctl  # noqa: E402

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
        for c, t in (("kind", "TEXT DEFAULT 'user'"), ("climb_id", "TEXT"), ("harvest", "TEXT"), ("progress", "TEXT")):
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
        # area carries on by itself unless someone stopped it. Other jobs are lost, as their queue is.
        con.execute("UPDATE projects SET status='stopped' WHERE id IN (SELECT project_id FROM campaign_items "
                    "WHERE status='running' OR (status='pending' AND retry=1 AND redo IS NULL))")
        con.execute("UPDATE campaign_items SET status='pending', note='interrupted by a restart: carrying on from where it got to' WHERE status='running'")
        con.execute("UPDATE campaigns SET status='stopped', note=NULL WHERE status='stopping'")
        con.execute("UPDATE projects SET status='failed', progress=NULL, log=log||'\nserver restarted during job' "
                    "WHERE status IN ('queued','running','harvesting')")


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
        mesh_path, demo = pipeline.reconstruct(pdir / "photos", work, MESHES / mid, log, USE_GPU, internet=internet, tracker=tracker)
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
        q("UPDATE projects SET status='done', mesh_id=?, demo=? WHERE id=?", mid, int(demo), pid, write=True)
        q("UPDATE campaign_items SET status='done', mesh_id=?, retry=0, note=NULL, updated=? WHERE project_id=? AND status='failed'",
          mid, time.time(), pid, write=True)   # made by hand since its area gave up on it
        tracker.finish()
        log("done")
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
    except Exception as e:
        diagnostics.log_failure(log, e, DATA)
        if tracker:
            tracker.finish("failed")
        q("UPDATE projects SET status='failed' WHERE id=?", pid, write=True)


# ------------------------------------------------------------------ campaigns: mesh a whole area
_campaign_running: str | None = None    # the campaign whose climb the worker is on
_outages: list[tuple[str, str]] = []    # (campaign, climb) failing in a row before finishing a harvest


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
    items = q("SELECT rank,source_id,name,grade,stars,url,media,project_id,retry,redo FROM campaign_items "
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


def last_failure(pid: str) -> str:
    for line in reversed(read_log(pid).splitlines()):
        if line.startswith("FAILED: "):
            return line[len("FAILED: "):]
    return "failed; see its project's log"


def run_campaign_item(camp: dict, item: dict) -> None:
    """Mesh one climb, unless a mesh of its boulder already exists. Whatever this finishes is
    saved like any other mesh, so stopping later loses nothing but the climb in progress."""
    global _campaign_running
    if item.get("redo"):
        return run_redo(camp, item)
    cid, sid = camp["id"], item["source_id"]
    pid = item["project_id"]
    resume = bool(pid and q("SELECT id FROM projects WHERE id=?", pid) and (PROJECTS / pid).is_dir())
    jobctl.begin(low_priority=True)
    _campaign_running = cid
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
        _campaign_running = None
        q("UPDATE campaign_items SET retry=0 WHERE campaign_id=? AND source_id=? AND status NOT IN ('pending','running')", cid, sid, write=True)
        q("UPDATE campaigns SET status='stopped', note=NULL WHERE id=? AND status='stopping'", cid, write=True)


def next_campaign_step() -> None:
    """Move the oldest running campaign on by one climb."""
    camp = q("SELECT * FROM campaigns WHERE status='running' ORDER BY created LIMIT 1", one=True)
    if not camp:
        return
    if shutil.disk_usage(DATA).free < CAMPAIGN_MIN_FREE:
        q("UPDATE campaigns SET status='stopped', note=? WHERE id=?",
          f"stopped: under {CAMPAIGN_MIN_FREE / 2**30:.0f} GB of disk left", camp["id"], write=True)
        return
    if camp["ord"] == "data" and not camp["only_retries"]:
        read_ahead(camp)
    items = ordered_pending(camp)
    waiting = bool(items)
    if camp["only_retries"]:
        items = [i for i in items if i["retry"]]
    if not items:
        q("UPDATE campaigns SET status=?, only_retries=0, note=NULL WHERE id=? AND status='running'",
          "stopped" if waiting else "done", camp["id"], write=True)
    elif campaign_status(camp["id"]) == "running":
        try:
            run_campaign_item(camp, items[0])
        except Exception as e:
            set_item(camp["id"], items[0]["source_id"], status="failed", note=f"{type(e).__name__}: {e}")


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
    """Try a project again from the last step it finished.

    Returns:
        The response, and its HTTP status.
    """
    proj = q("SELECT status,kind,harvest FROM projects WHERE id=?", pid, one=True)
    if not proj:
        return {"error": "not found"}, 404
    if proj["status"] in ("queued", "running", "harvesting"):
        return {"error": "already running"}, 409
    if proj["status"] not in ("failed", "stopped"):
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
        jobs.put({"type": "retry", "pid": pid})
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
    global _campaign_running
    cid, sid = camp["id"], item["source_id"]
    row = q("SELECT source_id,climb_id,project_id,mesh_id,redo,was,changed,redo_started FROM campaign_items "
            "WHERE campaign_id=? AND source_id=?", cid, sid, one=True)
    row["was"] = row["was"] or "done"
    log = lambda s: append_log(row["project_id"], s)  # noqa: E731
    jobctl.begin(low_priority=True)
    _campaign_running = cid
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
    except Exception as e:
        end = {"status": row["was"], "note": f"{REDO_NAMES[row['redo']]}: {type(e).__name__}: {e}"}
    finally:
        _campaign_running = None
        q("UPDATE campaigns SET status='stopped', note=NULL WHERE id=? AND status='stopping'", cid, write=True)
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


def stop_campaign(camp_id: str, now: bool = True) -> None:
    """Stop a campaign, now or once the climb it's on is finished."""
    running = _campaign_running == camp_id
    q("UPDATE campaigns SET status=?, note=NULL, only_retries=0 WHERE id=? AND status IN ('running','stopping')",
      "stopping" if running and not now else "stopped", camp_id, write=True)
    if running and now:
        jobctl.stop()


def campaign_view(camp: dict) -> dict:
    """A campaign for the UI, with where it's got to."""
    cid = camp["id"]
    counts = {r["status"]: r["n"] for r in q("SELECT status,COUNT(*) n FROM campaign_items WHERE campaign_id=? GROUP BY status", cid)}
    again = {key: q(f"SELECT COUNT(*) n FROM campaign_items WHERE campaign_id=? AND {where}", cid, one=True)["n"]
             for key, where in (("changed", "changed=1"), ("queued", "redo IS NOT NULL AND status IN ('pending','running')"))}
    worked = q("SELECT rank,name,grade,stars,url,climb_id,mesh_id,project_id,status,note,updated FROM campaign_items "
               "WHERE campaign_id=? AND status NOT IN ('pending','running') ORDER BY updated DESC LIMIT 300", cid)
    current = q("SELECT rank,name,grade,stars,project_id FROM campaign_items WHERE campaign_id=? AND status='running' LIMIT 1", cid, one=True)
    if current and current["project_id"]:
        p = q("SELECT status,progress FROM projects WHERE id=?", current["project_id"], one=True)
        current["progress"] = json.loads(p["progress"]) if p and p["progress"] else None
    return {**camp, "counts": counts, "again": again, "worked": worked, "current": current, "next": ordered_pending(camp)[:10]}


def worker() -> None:
    while True:
        try:
            job = jobs.get(timeout=CAMPAIGN_POLL)   # jobs people asked for go ahead of a campaign's next climb
        except queue.Empty:
            try:
                next_campaign_step()
            except (Exception, Cancelled) as e:  # never let the worker die
                print(f"campaign error: {e!r}", flush=True)
            continue
        jobctl.begin(low_priority=job["type"] == "beta")
        try:
            if job["type"] == "beta":
                run_beta(job["mid"])
            elif job["type"] == "reconstruct":
                run_reconstruct(job["pid"], job.get("internet", False))
            elif job["type"] == "harvest":
                run_harvest(job["pid"], set(job["sources"]), job["reconstruct"], job.get("filter", True))
            elif job["type"] == "retry":
                run_retry(job["pid"])
        except (Exception, Cancelled) as e:  # never let the worker die
            append_log(job.get("pid", "?"), f"worker error: {e!r}")


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
                                       "opencv": _has("cv2"), "pillow": _has("PIL")})
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
                proj["failure"] = last_failure(m[1]) if proj["status"] == "failed" else None
                proj["harvest"] = json.loads(proj["harvest"]) if proj.get("harvest") else None
                proj["progress"] = json.loads(proj["progress"]) if proj.get("progress") else None
                rejected = PROJECTS / m[1] / "rejected"
                proj["filtered"] = [{"file": f.name, "why": relevance_mod.REASONS[f.name.split("_", 1)[0]]}
                                    for f in (sorted(rejected.iterdir()) if rejected.exists() else [])
                                    if f.name.split("_", 1)[0] in relevance_mod.REASONS]
                return self.send_json(proj)
            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/harvest", p)
            if m:
                d = PROJECTS / m[1]
                out = {}
                for name in ("sources", "diversity"):
                    f = d / f"{name}.json"
                    out[name] = json.loads(f.read_text()) if f.exists() else None
                return self.send_json(out)
            m = re.fullmatch(r"/photos/([0-9a-f]+)/([A-Za-z0-9._-]+)", p)
            if m:
                return self.send_file(PROJECTS / m[1] / "photos" / m[2])
            m = re.fullmatch(r"/photos/([0-9a-f]+)/rejected/([A-Za-z0-9._-]+)", p)
            if m:
                return self.send_file(PROJECTS / m[1] / "rejected" / m[2])
            # ---- meshes
            if p == "/api/meshes":
                rows = q("SELECT * FROM meshes ORDER BY created DESC")
                links = q("SELECT l.mesh_id, c.id, c.name, c.grade FROM mesh_links l JOIN climbs c ON c.id=l.climb_id")
                by = {}
                for l in links:
                    by.setdefault(l["mesh_id"], []).append({"id": l["id"], "name": l["name"], "grade": l["grade"]})
                for r in rows:
                    r["climbs"] = by.get(r["id"], [])
                    r["cleanup"] = meshclean.summary(meshclean.load(MESHES / r["id"]))
                    r["orientation"] = orientation.load(MESHES / r["id"])
                    r["sources"] = provenance.summary(provenance.load(MESHES / r["id"]))
                    r["beta"] = beta_mod.summary(beta_mod.load(MESHES / r["id"]))
                    r["cameras_kept"] = (MESHES / r["id"] / orientation.CAMERAS_FILE).exists()
                return self.send_json(rows)
            m = re.fullmatch(r"/api/meshes/([0-9a-f]+)/beta", p)
            if m:   # what each video's climber did, and the holds marked by hand
                if not (MESHES / m[1]).is_dir():
                    return self.send_json({"error": "not found"}, 404)
                return self.send_json({"found": beta_mod.load(MESHES / m[1]), "marked": beta_mod.load_holds(MESHES / m[1]),
                                       "available": beta_mod.available()})
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
                record = meshclean.load(MESHES / m[1])
                return self.send_json(record) if record else self.send_json({"error": "no cleanup"}, 404)
            m = re.fullmatch(r"/meshes/([0-9a-f]+)/([A-Za-z0-9._-]+)", p)
            if m:
                f = MESHES / m[1] / m[2]
                side = int(qs["max"][0]) if qs.get("max", [""])[0].isdigit() else 0
                if side in SHRINK_SIDES and f.suffix.lower() in (".png", ".jpg", ".jpeg") and f.is_file():
                    if not _has("PIL"):
                        return self.send_json({"error": "shrinking textures needs Pillow"}, 501)
                    f = shrunk_texture(f, side)
                if "cleaned" in qs and f.suffix.lower() == ".glb" and meshclean.available():
                    f = meshclean.write_cleaned(MESHES / m[1]) or f
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
                return self.send_json(r) if r else self.send_json({"error": "not found"}, 404)
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
                jobs.put({"type": "reconstruct", "pid": pid, "internet": proj["kind"] == "harvest"})
                return self.send_json({"queued": True, "position": jobs.qsize()}, 202)

            m = re.fullmatch(r"/api/projects/([0-9a-f]+)/retry", p)
            if m:
                return self.send_json(*retry_project(m[1]))

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
                    record = meshclean.clean(MESHES / m[1], print, force=True)
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
                jobs.put({"type": "beta", "mid": m[1]})
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
                jobs.put({"type": "harvest", "pid": pid, "sources": sources, "reconstruct": bool(body.get("reconstruct", True)),
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
                    return self.send_json(meshclean.set_removed(MESHES / m[1], body.get("removed", [])))
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
            meshclean.delete(MESHES / m[1])
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


def main() -> None:
    init_db()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=clean_existing_meshes, daemon=True).start()
    threading.Thread(target=orient_existing_meshes, daemon=True).start()
    threading.Thread(target=source_existing_meshes, daemon=True).start()
    for d in MESHES.iterdir() if MESHES.exists() else []:
        beta_mod.interrupted(d)     # a search for beta that a restart stopped, as jobs don't carry on
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8470"))
    mode = "COLMAP + OpenMVS" if pipeline.real_pipeline_ready() else "DEMO (COLMAP/OpenMVS not found)"
    print(f"BoulderMesh on http://{host}:{port}  data={DATA}  pipeline={mode}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
