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
import pipeline  # noqa: E402
import climbs as climbs_mod  # noqa: E402
from climbs import harvest as harvest_mod  # noqa: E402
from climbs import relevance as relevance_mod  # noqa: E402
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
          status TEXT);                -- running | stopping | stopped | done
        CREATE TABLE IF NOT EXISTS campaign_items(
          campaign_id TEXT, source_id TEXT, rank INTEGER, name TEXT, grade TEXT, stars REAL, url TEXT,
          boulder TEXT, data TEXT, media INTEGER, climb_id TEXT, project_id TEXT, mesh_id TEXT,
          note TEXT, updated REAL,
          status TEXT DEFAULT 'pending',   -- pending | running | done | skipped | failed
          PRIMARY KEY(campaign_id, source_id));
        CREATE INDEX IF NOT EXISTS campaign_items_boulder ON campaign_items(boulder);
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
        con.execute("UPDATE projects SET status='failed', progress=NULL, log=log||'\nserver restarted during job' "
                    "WHERE status IN ('queued','running','harvesting')")
        # a restart is an interruption: what finished stays, and nothing heavy restarts unattended
        con.execute("UPDATE campaign_items SET status='pending', note='interrupted: starts again when resumed' WHERE status='running'")
        con.execute("UPDATE campaigns SET status='stopped', note='stopped by a server restart' WHERE status IN ('running','stopping')")


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


def run_reconstruct(pid: str, internet: bool = False, tracker: Tracker | None = None) -> None:
    pdir = PROJECTS / pid
    q("UPDATE projects SET status='running' WHERE id=?", pid, write=True)
    log = lambda s: append_log(pid, s)  # noqa: E731
    tracker = tracker or tracker_for(pid, pipeline.steps())
    try:
        work = pdir / "work"
        shutil.rmtree(work, ignore_errors=True)
        mid = new_id()
        mesh_path, demo = pipeline.reconstruct(pdir / "photos", work, MESHES / mid, log, USE_GPU, internet=internet, tracker=tracker)
        proj = q("SELECT name, climb_id FROM projects WHERE id=?", pid, one=True)
        q("INSERT INTO meshes VALUES(?,?,?,?,?,?,?,?,?)", mid, proj["name"], mesh_path.name, f"project:{pid}",
          time.time(), mesh_path.stat().st_size, int(demo), None, None, write=True)
        if proj["climb_id"]:
            link_mesh(mid, proj["climb_id"], "auto-linked from reconstruction project")
            log(f"mesh linked to climb {proj['climb_id']}")
        q("UPDATE projects SET status='done', mesh_id=?, demo=? WHERE id=?", mid, int(demo), pid, write=True)
        tracker.finish()
        log("done")
    except Exception as e:
        log(f"FAILED: {e}")
        tracker.finish("failed")
        q("UPDATE projects SET status='failed' WHERE id=?", pid, write=True)


def run_harvest(pid: str, sources: set[str], reconstruct: bool, relevance_filter: bool = True) -> None:
    pdir = PROJECTS / pid
    q("UPDATE projects SET status='harvesting', log='' WHERE id=?", pid, write=True)
    (pdir / "job.log").unlink(missing_ok=True)
    log = lambda s: append_log(pid, s)  # noqa: E731
    tracker = None
    try:
        proj = q("SELECT climb_id FROM projects WHERE id=?", pid, one=True)
        c = climb_obj(proj["climb_id"])
        stub = bool(c.raw.get("stub"))
        tracker = tracker_for(pid, ([("page", "Fetch problem page", 1)] if stub else []) + harvest_mod.steps(sources, relevance_filter)
                              + (pipeline.steps() if reconstruct else []))
        if stub:
            tracker.update("page", 0.1, "up to a minute on Mountain Project")
            log(f"fetching the problem page for its photos/videos: {c.url}")
            upsert_climb(climbs_mod.fetch_climb_url(c.url, include_children=False)[0])
            c = climb_obj(proj["climb_id"])
        summary = harvest_mod.harvest(c, pdir, log, sources, relevance_filter=relevance_filter, tracker=tracker)
        q("UPDATE projects SET harvest=?, status='harvested' WHERE id=?", json.dumps(summary), pid, write=True)
        verdict = (summary.get("diversity") or {}).get("verdict", "unknown")
        log(f"harvest complete: {summary['images']} image(s), verdict {verdict}")
        if reconstruct and verdict != "poor":
            run_reconstruct(pid, internet=True, tracker=tracker)
            return
        if reconstruct:
            log("not enough viewpoint diversity for photogrammetry - skipping reconstruction "
                "(add your own photos to this project and press Reconstruct to try anyway)")
        tracker.skip_rest()
    except Exception as e:
        log(f"FAILED: {e}")
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
    items = q("SELECT rank,source_id,name,grade,stars,url,media,project_id FROM campaign_items "
              "WHERE campaign_id=? AND status='pending' ORDER BY rank", camp["id"])
    if camp["ord"] == "rating":
        items.sort(key=lambda i: (-(i["stars"] or 0), i["rank"]))
    elif camp["ord"] == "data":
        items.sort(key=lambda i: (i["media"] is None, -(i["media"] or 0), i["rank"]))
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


def discard_project(pid: str) -> None:
    q("DELETE FROM projects WHERE id=?", pid, write=True)
    shutil.rmtree(PROJECTS / pid, ignore_errors=True)


def last_failure(pid: str) -> str:
    for line in reversed(read_log(pid).splitlines()):
        if line.startswith("FAILED: "):
            return line[len("FAILED: "):]
    return "failed; see its project's log"


def run_campaign_item(camp: dict, item: dict) -> None:
    """Mesh one climb, unless a mesh of its boulder already exists. Whatever this finishes is
    saved like any other mesh, so stopping later loses nothing but the climb in progress."""
    global _campaign_running
    cid, sid = camp["id"], item["source_id"]
    if item["project_id"]:   # the unfinished work of an interrupted attempt
        discard_project(item["project_id"])
    jobctl.begin(low_priority=True)
    _campaign_running = cid
    try:
        set_item(cid, sid, status="running", project_id=None, note=None)
        full = q("SELECT data,climb_id,boulder FROM campaign_items WHERE campaign_id=? AND source_id=?", cid, sid, one=True)
        c = Climb.from_dict(json.loads(full["data"]))
        climb_id = full["climb_id"] or upsert_climb(c)
        have = q("SELECT mesh_id FROM mesh_links WHERE climb_id=? LIMIT 1", climb_id, one=True)
        same = full["boulder"] and q("SELECT name,mesh_id FROM campaign_items WHERE boulder=? AND mesh_id IS NOT NULL LIMIT 1",
                                     full["boulder"], one=True)
        if have:
            set_item(cid, sid, status="done", climb_id=climb_id, mesh_id=have["mesh_id"], note="already had a mesh")
        elif same:
            link_mesh(same["mesh_id"], climb_id, f"same boulder as {same['name']}")
            set_item(cid, sid, status="done", climb_id=climb_id, mesh_id=same["mesh_id"], note=f"same boulder as {same['name']}")
        else:
            pid = new_id()
            (PROJECTS / pid / "photos").mkdir(parents=True)
            q("INSERT INTO projects(id,name,created,kind,climb_id,status) VALUES(?,?,?,?,?,'queued')",
              pid, f"{c.name} (harvest)"[:80], time.time(), "harvest", climb_id, write=True)
            set_item(cid, sid, climb_id=climb_id, project_id=pid)
            try:
                run_harvest(pid, set(HARVEST_SOURCES), True)
            except Cancelled:
                discard_project(pid)
                set_item(cid, sid, status="pending", project_id=None, note="stopped partway: starts again when resumed")
                return
            shutil.rmtree(PROJECTS / pid / "work", ignore_errors=True)   # most of a job's disk use, and only useful to itself
            p = q("SELECT status,mesh_id,harvest FROM projects WHERE id=?", pid, one=True)
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
    if camp["ord"] == "data":
        read_ahead(camp)
    items = ordered_pending(camp)
    if not items:
        q("UPDATE campaigns SET status='done', note=NULL WHERE id=? AND status='running'", camp["id"], write=True)
    elif campaign_status(camp["id"]) == "running":
        try:
            run_campaign_item(camp, items[0])
        except Exception as e:
            set_item(camp["id"], items[0]["source_id"], status="failed", note=str(e))


def stop_campaign(camp_id: str, now: bool = True) -> None:
    """Stop a campaign, now or once the climb it's on is finished."""
    running = _campaign_running == camp_id
    q("UPDATE campaigns SET status=?, note=NULL WHERE id=? AND status IN ('running','stopping')",
      "stopping" if running and not now else "stopped", camp_id, write=True)
    if running and now:
        jobctl.stop()


def campaign_view(camp: dict) -> dict:
    """A campaign for the UI, with where it's got to."""
    cid = camp["id"]
    counts = {r["status"]: r["n"] for r in q("SELECT status,COUNT(*) n FROM campaign_items WHERE campaign_id=? GROUP BY status", cid)}
    worked = q("SELECT rank,name,grade,stars,url,climb_id,mesh_id,project_id,status,note,updated FROM campaign_items "
               "WHERE campaign_id=? AND status NOT IN ('pending','running') ORDER BY updated DESC LIMIT 300", cid)
    current = q("SELECT rank,name,grade,stars,project_id FROM campaign_items WHERE campaign_id=? AND status='running' LIMIT 1", cid, one=True)
    if current and current["project_id"]:
        p = q("SELECT status,progress FROM projects WHERE id=?", current["project_id"], one=True)
        current["progress"] = json.loads(p["progress"]) if p and p["progress"] else None
    return {**camp, "counts": counts, "worked": worked, "current": current, "next": ordered_pending(camp)[:10]}


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
        jobctl.begin()
        try:
            if job["type"] == "reconstruct":
                run_reconstruct(job["pid"], job.get("internet", False))
            elif job["type"] == "harvest":
                run_harvest(job["pid"], set(job["sources"]), job["reconstruct"], job.get("filter", True))
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
                return self.send_json(rows)
            m = re.fullmatch(r"/meshes/([0-9a-f]+)/([A-Za-z0-9._-]+)", p)
            if m:
                f = MESHES / m[1] / m[2]
                side = int(qs["max"][0]) if qs.get("max", [""])[0].isdigit() else 0
                if side in SHRINK_SIDES and f.suffix.lower() in (".png", ".jpg", ".jpeg") and f.is_file():
                    if not _has("PIL"):
                        return self.send_json({"error": "shrinking textures needs Pillow"}, 501)
                    f = shrunk_texture(f, side)
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
                q("INSERT INTO meshes VALUES(?,?,?,?,?,?,?,?,?)", mid, name, main, "upload", time.time(),
                  (d / main).stat().st_size, 0, lat, lng, write=True)
                if fields.get("climb_id"):
                    link_mesh(mid, fields["climb_id"], "linked at upload")
                return self.send_json({"id": mid, "name": name, "filename": main}, 201)

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
                    q("UPDATE campaigns SET status='running', note=NULL WHERE id=? AND status IN ('stopped','stopping')", m[1], write=True)
                else:
                    stop_campaign(m[1], bool(self.read_json().get("now", True)))
                return self.send_json(campaign_view(q("SELECT * FROM campaigns WHERE id=?", m[1], one=True)))

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
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8470"))
    mode = "COLMAP + OpenMVS" if pipeline.real_pipeline_ready() else "DEMO (COLMAP/OpenMVS not found)"
    print(f"BoulderMesh on http://{host}:{port}  data={DATA}  pipeline={mode}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
