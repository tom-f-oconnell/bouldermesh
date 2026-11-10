"""Volunteers: other people's computers fetching YouTube videos, and videos' details, for this server.

YouTube refuses a server's downloads with its bot check after a few hours of harvesting, and a cloud server's almost
at once (design.md, "When YouTube refuses"). A volunteer runs volunteer/bouldermesh_volunteer.py with a token from the
admin page. It asks for work, fetches one video at a time from its own address, at least as cautiously as this server
would, and sends each back in parts. Work names a video only by its YouTube id, which the script turns into a
youtube.com address itself, so it can't be made to fetch anything else.

The broker keeps its own database (volunteers.sqlite), apart from the site's, which deploy/ship.sh replaces, and the
files coming in under DATA_DIR/volunteers until they're whole. It's served on a port of its own (VOLUNTEER_PORT) that
answers only /volunteer/..., so that a proxy in front can pass on just that: Caddy on the public server, to its own
app (option B in design.md), or through an SSH tunnel to another server (option A).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

PER_HOUR = 15               # videos a volunteer, or an address, may be given in an hour (hal's refusals came after 20-30)
PER_DAY = 100
BLOCK_HOURS = (1, 2, 4, 8)  # a volunteer YouTube refused rests this long, longer each time it's still refused
STALE_MINUTES = 90          # work given out that nothing has come of for this long goes back to the queue
MAX_TRIES = 3               # a video that fails this often, for reasons that might pass, isn't asked for again
PART_MAX = 64 * 2**20       # the largest part of a file, under Cloudflare's 100 MB per request
MAX_VIDEO = 600 * 2**20     # the script asks yt-dlp for at most 500 MB
VIDEO_FORMAT = "bv*[height<=1080]/b"   # as the harvest downloads: video only, no audio
VIDEO_EXT = {"mp4", "webm", "mkv", "mov", "m4v", "3gp"}
FAILED_DAYS = 30            # a video that failed for good isn't asked for again for this long
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
DETAILS = {"title": 500, "uploader": 200, "channel": 200, "channel_id": 100, "channel_url": 300, "uploader_url": 300,
           "upload_date": 20, "description": 20000, "license": 200}     # yt-dlp's names, as youtube.details() reads them
SCRIPT = Path(__file__).resolve().parents[1] / "volunteer" / "bouldermesh_volunteer.py"
CLIENT_VERSION = 1          # the oldest script that speaks to this


def clean_details(info: dict, video_id: str) -> dict:
    """What a volunteer said of a video, cut to what youtube.details() keeps, with its address made here: nothing
    else of the volunteer's (yt-dlp's full details have its IP address in the download links)."""
    out = {"url": f"https://www.youtube.com/watch?v={video_id}"}
    for k, n in DETAILS.items():
        v = info.get(k)
        out[k] = str(v)[:n] if v is not None else None
    try:
        out["duration"] = float(info["duration"]) if info.get("duration") is not None else None
    except (TypeError, ValueError):
        out["duration"] = None
    return out


def probe_duration(path: Path) -> float | None:
    """A video's length by ffprobe, or None if it can't tell (or it isn't a video)."""
    try:
        out = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=120)
        info = json.loads(out.stdout or "{}")
        if not info.get("streams"):
            return None
        return float(info["format"]["duration"])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None


class Broker:
    """Volunteers, the work for them, and what they send back.

    Args:
        db_path: its database.
        files: where files come in.
        delivered: called with (task, path to the video or None, its details) when a task is done; the video is
            the callee's to move.
        ready: called with (purpose, ref) once every video wanted for that is done or has failed, at least one
            having come: ("harvest", project id), ("beta", mesh id), ("details", url).
        probe: a video file's length, to check it's the one asked for (probe_duration()).
    """

    def __init__(self, db_path: Path, files: Path, delivered: Callable[[dict, Path | None, dict], None],
                 ready: Callable[[str, str], None], probe: Callable[[Path], float | None] = probe_duration):
        self.db_path, self.files, self.delivered, self.ready, self.probe = db_path, files, delivered, ready, probe
        self.lock = threading.RLock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS volunteers(id TEXT PRIMARY KEY, name TEXT, token_hash TEXT UNIQUE, created REAL,
                  revoked REAL, last_seen REAL, last_ip TEXT, client TEXT, per_hour INTEGER, per_day INTEGER,
                  refused_until REAL, refusal_level INTEGER DEFAULT -1, done INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, kind TEXT, video_id TEXT, state TEXT, created REAL,
                  volunteer TEXT, ip TEXT, assigned REAL, touched REAL, size INTEGER, ext TEXT, tries INTEGER DEFAULT 0,
                  error TEXT, finished REAL, info TEXT);
                CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state, created);
                CREATE INDEX IF NOT EXISTS tasks_video ON tasks(kind, video_id);
                CREATE TABLE IF NOT EXISTS wants(task TEXT, purpose TEXT, ref TEXT, created REAL, applied INTEGER DEFAULT 0,
                  PRIMARY KEY(task, purpose, ref));
                CREATE TABLE IF NOT EXISTS given(volunteer TEXT, ip TEXT, at REAL);
            """)

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def q(self, sql: str, *args, one: bool = False) -> list[dict] | dict | None:
        with self.lock, self.db() as con:
            rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        return (rows[0] if rows else None) if one else rows

    # -- volunteers ---------------------------------------------------------------------------------------------
    def add(self, name: str, per_hour: int | None = None, per_day: int | None = None) -> dict:
        """A new volunteer: {id, name, token}. The token is shown this once; only its hash is kept."""
        name = (name or "").strip()[:80]
        if not name:
            raise ValueError("give the volunteer a name")
        vid, token = secrets.token_hex(4), "bmv_" + secrets.token_urlsafe(32)
        self.q("INSERT INTO volunteers(id,name,token_hash,created,per_hour,per_day) VALUES(?,?,?,?,?,?)", vid, name,
               hashlib.sha256(token.encode()).hexdigest(), time.time(), _limit(per_hour, PER_HOUR), _limit(per_day, PER_DAY))
        return {"id": vid, "name": name, "token": token}

    def revoke(self, vid: str) -> None:
        """Its token stops working, and what it was fetching goes back to the queue."""
        self.q("UPDATE volunteers SET revoked=? WHERE id=?", time.time(), vid)
        self.q("UPDATE tasks SET state='queued', volunteer=NULL WHERE volunteer=? AND state='assigned'", vid)

    def volunteer(self, token: str) -> dict | None:
        return self.q("SELECT * FROM volunteers WHERE token_hash=?", hashlib.sha256((token or "").encode()).hexdigest(), one=True)

    def any(self) -> bool:
        """Whether there's a volunteer who could fetch what's wanted."""
        return bool(self.q("SELECT 1 FROM volunteers WHERE revoked IS NULL LIMIT 1"))

    # -- what's wanted --------------------------------------------------------------------------------------------
    def want(self, kind: str, video_id: str, purpose: str, ref: str) -> bool:
        """Ask volunteers for a video ("video") or its details ("details"), for purpose and ref (as ready() is
        called with). Whether it's coming: false for a video that failed for good lately."""
        if kind not in ("video", "details") or not VIDEO_ID.fullmatch(video_id or ""):
            return False
        with self.lock:
            task = self.q("SELECT id FROM tasks WHERE kind=? AND video_id=? AND state IN ('queued','assigned')", kind, video_id, one=True)
            if not task:
                if self.q("SELECT 1 FROM tasks WHERE kind=? AND video_id=? AND state='failed' AND finished>?",
                          kind, video_id, time.time() - FAILED_DAYS * 86400, one=True):
                    return False
                task = {"id": secrets.token_hex(6)}
                self.q("INSERT INTO tasks(id,kind,video_id,state,created) VALUES(?,?,?,'queued',?)", task["id"], kind, video_id, time.time())
            self.q("INSERT OR IGNORE INTO wants(task,purpose,ref,created) VALUES(?,?,?,?)", task["id"], purpose, ref, time.time())
        return True

    def cancel(self) -> int:
        """Drop everything still queued; what's being fetched carries on."""
        with self.lock:
            n = len(self.q("SELECT id FROM tasks WHERE state='queued'"))
            self.q("UPDATE tasks SET state='cancelled', finished=? WHERE state='queued'", time.time())
        return n

    def requeue_stale(self) -> None:
        cut = time.time() - STALE_MINUTES * 60
        self.q("UPDATE tasks SET state='queued', volunteer=NULL, tries=tries+1 WHERE state='assigned' AND COALESCE(touched, assigned)<?", cut)
        self.q("UPDATE tasks SET state='failed', finished=?, error='given out too often without coming back' "
               "WHERE state='queued' AND tries>=?", time.time(), MAX_TRIES)

    # -- the volunteer's side ---------------------------------------------------------------------------------------
    def work(self, v: dict, ip: str, client: str = "", per_hour: int | None = None, per_day: int | None = None) -> dict:
        """The next thing for a volunteer to fetch, {"task": ...}, or {"wait": seconds, "why": ...}: what it was
        given and hasn't finished, then the oldest wanted, within its limits and its address's."""
        now = time.time()
        with self.lock:
            self.requeue_stale()
            self.q("UPDATE volunteers SET last_seen=?, last_ip=?, client=? WHERE id=?", now, ip, client[:80], v["id"])
            if (v.get("refused_until") or 0) > now:
                return {"wait": v["refused_until"] - now, "why": "YouTube refused you lately: resting"}
            mine = self.q("SELECT * FROM tasks WHERE volunteer=? AND state='assigned'", v["id"], one=True)
            if mine:
                return {"task": self._task_view(mine)}
            hour, day = _limit(per_hour, v["per_hour"]), _limit(per_day, v["per_day"])
            for span, cap, what in ((3600, min(hour, v["per_hour"]), "an hour"), (86400, min(day, v["per_day"]), "a day")):
                for field, value, whose in (("volunteer", v["id"], "you"), ("ip", ip, "your address")):
                    rows = self.q(f"SELECT at FROM given WHERE {field}=? AND at>? ORDER BY at", value, now - span)
                    if len(rows) >= cap:
                        return {"wait": rows[len(rows) - cap]["at"] + span - now + 1,
                                "why": f"{cap} {what} is the most for {whose}"}
            task = self.q("SELECT * FROM tasks WHERE state='queued' ORDER BY created LIMIT 1", one=True)
            if not task:
                return {"wait": 600, "why": "nothing wanted now"}
            self.q("UPDATE tasks SET state='assigned', volunteer=?, ip=?, assigned=?, touched=?, size=NULL, ext=NULL WHERE id=?",
                   v["id"], ip, now, now, task["id"])
            self.q("INSERT INTO given(volunteer,ip,at) VALUES(?,?,?)", v["id"], ip, now)
            self.q("DELETE FROM given WHERE at<?", now - 2 * 86400)
            (self.files / f"{task['id']}.part").unlink(missing_ok=True)
        return {"task": self._task_view(task)}

    def _task_view(self, t: dict) -> dict:
        return {"id": t["id"], "kind": t["kind"], "video_id": t["video_id"], "format": VIDEO_FORMAT,
                "max_filesize": "500m", "part": PART_MAX}

    def _mine(self, v: dict, task_id: str) -> dict:
        t = self.q("SELECT * FROM tasks WHERE id=?", task_id, one=True)
        if not t or t["volunteer"] != v["id"] or t["state"] != "assigned":
            raise LookupError("that isn't yours to fetch now: ask for work again")
        return t

    def received(self, v: dict, task_id: str) -> int:
        self._mine(v, task_id)
        f = self.files / f"{task_id}.part"
        return f.stat().st_size if f.exists() else 0

    def part(self, v: dict, task_id: str, offset: int, size: int, ext: str, body: bytes) -> int:
        """Add a part of a fetched video at offset, of a file of size bytes; how much has come.

        Raises:
            LookupError: not this volunteer's to send now.
            ValueError: too big, a kind of file it can't be, or more than size.
            OffsetError: not where the file has got to.
        """
        with self.lock:
            t = self._mine(v, task_id)
            ext = (ext or "").lower().lstrip(".")
            if t["kind"] != "video":
                raise ValueError("only videos come in parts")
            if ext not in VIDEO_EXT or not 0 < size <= MAX_VIDEO:
                raise ValueError(f"a video of up to {MAX_VIDEO // 2**20} MB, as {', '.join(sorted(VIDEO_EXT))}")
            if t["size"] is not None and (t["size"], t["ext"]) != (size, ext):
                raise ValueError("that's another file than the one being sent")
            self.files.mkdir(parents=True, exist_ok=True)
            f = self.files / f"{task_id}.part"
            have = f.stat().st_size if f.exists() else 0
            if offset != have:
                raise OffsetError(have)
            if have + len(body) > size:
                raise ValueError("more than the size it was started with")
            with open(f, "ab") as out:
                out.write(body)
            self.q("UPDATE tasks SET size=?, ext=?, touched=? WHERE id=?", size, ext, time.time(), task_id)
            return have + len(body)

    def finish(self, v: dict, task_id: str, report: dict) -> dict:
        """What came of a task: {"status": "ok", "info": details} (a video sent in full first), "refused" (YouTube's
        bot check, or a rate limit: the volunteer rests), "released" (the volunteer stopped), or "failed" with "error",
        and "permanent" for a video that won't ever come (private, removed)."""
        status, now = report.get("status"), time.time()
        with self.lock:
            t = self._mine(v, task_id)
            f = self.files / f"{task_id}.part"
            if status == "released":    # the volunteer stopped before finishing: someone else can have it
                self.q("UPDATE tasks SET state='queued', volunteer=NULL WHERE id=?", task_id)
                f.unlink(missing_ok=True)
                return {"ok": True}
            if status == "refused":
                level = min(len(BLOCK_HOURS) - 1, (v.get("refusal_level") if v.get("refusal_level") is not None else -1) + 1)
                until = now + BLOCK_HOURS[level] * 3600
                self.q("UPDATE volunteers SET refusal_level=?, refused_until=? WHERE id=?", level, until, v["id"])
                self.q("UPDATE tasks SET state='queued', volunteer=NULL WHERE id=?", task_id)
                f.unlink(missing_ok=True)
                return {"rest_until": until}
            if status != "ok":
                self._failed(t, str(report.get("error") or "failed")[:500], bool(report.get("permanent")))
                f.unlink(missing_ok=True)
                return {"ok": True}
            info = clean_details(report.get("info") or {}, t["video_id"])
            path = None
            if t["kind"] == "video":
                if not f.exists() or t["size"] is None or f.stat().st_size != t["size"]:
                    raise ValueError("the video hasn't all come: send the rest first")
                path = f.with_name(f"{task_id}.{t['ext']}")
                f.rename(path)
                length = self.probe(path)
                if length is None or (info["duration"] and abs(length - info["duration"]) > max(5, 0.1 * info["duration"])):
                    path.unlink(missing_ok=True)
                    self._failed(t, "the file didn't look like that video" if length is None else
                                 f"the file is {length:.0f} s long, the video {info['duration']:.0f} s", False)
                    return {"ok": False}
            self.q("UPDATE tasks SET state='done', finished=?, info=?, error=NULL WHERE id=?", now, json.dumps(info), task_id)
            self.q("UPDATE volunteers SET refusal_level=-1, refused_until=NULL, done=done+1 WHERE id=?", v["id"])
        try:
            self.delivered({**t, "state": "done"}, path, info)
        finally:
            if path:
                path.unlink(missing_ok=True)
            self._settle(task_id)
        return {"ok": True}

    def _failed(self, t: dict, error: str, permanent: bool) -> None:
        if permanent or t["tries"] + 1 >= MAX_TRIES:
            self.q("UPDATE tasks SET state='failed', finished=?, error=?, tries=tries+1 WHERE id=?", time.time(), error, t["id"])
            self._settle(t["id"])
        else:
            self.q("UPDATE tasks SET state='queued', volunteer=NULL, error=?, tries=tries+1 WHERE id=?", error, t["id"])

    def _settle(self, task_id: str) -> None:
        """For each purpose this task was wanted for: if nothing more it wants is coming, and something came, it's
        ready."""
        for w in self.q("SELECT purpose, ref FROM wants WHERE task=? AND applied=0", task_id):
            with self.lock:
                pending = self.q("SELECT 1 FROM wants w JOIN tasks t ON t.id=w.task WHERE w.purpose=? AND w.ref=? "
                                 "AND t.state IN ('queued','assigned') LIMIT 1", w["purpose"], w["ref"])
                came = self.q("SELECT 1 FROM wants w JOIN tasks t ON t.id=w.task WHERE w.purpose=? AND w.ref=? "
                              "AND w.applied=0 AND t.state='done' LIMIT 1", w["purpose"], w["ref"])
                if pending:
                    continue
                self.q("UPDATE wants SET applied=1 WHERE purpose=? AND ref=?", w["purpose"], w["ref"])
            if came:
                self.ready(w["purpose"], w["ref"])

    # -- for the admin page ---------------------------------------------------------------------------------------
    def state(self) -> dict:
        now = time.time()
        vols = []
        for v in self.q("SELECT * FROM volunteers ORDER BY created"):
            given = self.q("SELECT at FROM given WHERE volunteer=? AND at>?", v["id"], now - 86400)
            fetching = self.q("SELECT kind, video_id, size, touched FROM tasks WHERE volunteer=? AND state='assigned'", v["id"], one=True)
            if v["revoked"]:
                status = "revoked"
            elif (v["refused_until"] or 0) > now:
                status = "resting"
            elif fetching:
                status = "fetching"
            elif v["last_seen"] and now - v["last_seen"] < 1800:
                status = "waiting for work"
            else:
                status = "not connected"
            vols.append({"id": v["id"], "name": v["name"], "status": status, "last_seen": v["last_seen"], "address": v["last_ip"],
                         "client": v["client"], "refused_until": v["refused_until"] if (v["refused_until"] or 0) > now else None,
                         "per_hour": v["per_hour"], "per_day": v["per_day"], "done": v["done"], "fetching": fetching,
                         "last_hour": sum(g["at"] > now - 3600 for g in given), "last_day": len(given)})
        counts = {f"{r['state']}_{r['kind']}": r["n"] for r in self.q("SELECT state, kind, count(*) AS n FROM tasks GROUP BY state, kind")}
        wanted = {r["purpose"]: r["n"] for r in self.q("SELECT w.purpose, count(DISTINCT w.task) AS n FROM wants w JOIN tasks t "
                                                         "ON t.id=w.task WHERE t.state IN ('queued','assigned') GROUP BY w.purpose")}
        recent = self.q("SELECT kind, video_id, state, error, finished FROM tasks WHERE state IN ('done','failed') "
                        "ORDER BY finished DESC LIMIT 8")
        return {"volunteers": vols, "counts": counts, "wanted": wanted, "recent": recent,
                "connected": sum(v["status"] in ("fetching", "waiting for work") for v in vols)}


class OffsetError(Exception):
    def __init__(self, have: int):
        self.have = have
        super().__init__(f"expected the part at byte {have}")


def _limit(given, most: int) -> int:
    try:
        n = int(given)
    except (TypeError, ValueError):
        return most
    return max(1, min(n, most))


# ------------------------------------------------------------------------------------------- serving it
def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    """The sender: the proxy's X-Real-IP when the connection is from a loopback or private address, as a proxy's is
    (Caddy, or an SSH tunnel), else the connection's own."""
    peer = handler.client_address[0]
    try:
        local = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
    except ValueError:
        local = False
    return (handler.headers.get("X-Real-IP") or peer) if local else peer


PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Volunteer for BoulderMesh</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:42em;margin:2em auto;padding:0 16px}code,pre{background:#eee;padding:2px 4px}</style>
<h1>Volunteering for BoulderMesh</h1>
<p>BoulderMesh makes 3D models of boulders from photos and climbing videos. YouTube stops one server from fetching many
videos, so volunteers fetch some from their own computers, slowly: at most 15 an hour and 100 a day, one at a time.</p>
<p>You need a token from whoever runs this site, and Python 3.9 or newer. Then:</p>
<pre>curl -O {base}/volunteer/bouldermesh_volunteer.py
python3 bouldermesh_volunteer.py --server {base} --token YOUR_TOKEN</pre>
<p>Before it starts, it explains what it does and asks you to agree. Among other things: YouTube's terms don't allow
downloading videos except through YouTube itself, and if YouTube decides your address is fetching too much, everyone on
your network may be asked to sign in or prove they're not a bot for a while.</p>
"""


def handler_for(broker: Broker, log: Callable[[str], None]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BoulderMeshVolunteers"
        sys_version = ""
        timeout = 120

        def log_message(self, fmt, *args):
            pass

        def send(self, code: int, body: bytes, kind: str = "application/json", headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj, code: int = 200) -> None:
            self.send(code, json.dumps(obj).encode())

        def who(self) -> dict | None:
            auth = self.headers.get("Authorization", "")
            v = broker.volunteer(auth[7:].strip()) if auth.startswith("Bearer ") else None
            if not v:
                self.json({"error": "unknown token: ask whoever runs this site for one"}, 401)
                return None
            if v["revoked"]:
                self.json({"error": "this token no longer works"}, 403)
                return None
            return v

        def body(self, most: int) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= most:
                raise ValueError(f"a request of at most {most // 2**20} MB")
            return self.rfile.read(n)

        def do_GET(self):
            u = urlparse(self.path)
            base = f"https://{self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or 'this-site'}"
            if u.path in ("/volunteer", "/volunteer/"):
                return self.send(200, PAGE.replace("{base}", base).encode(), "text/html; charset=utf-8")
            if u.path == "/volunteer/bouldermesh_volunteer.py":
                return self.send(200, SCRIPT.read_bytes(), "text/x-python; charset=utf-8",
                                 {"Content-Disposition": 'attachment; filename="bouldermesh_volunteer.py"'})
            m = re.fullmatch(r"/volunteer/api/tasks/([0-9a-f]{12})/part", u.path)
            if m and (v := self.who()):
                try:
                    return self.json({"received": broker.received(v, m[1])})
                except LookupError as e:
                    return self.json({"error": str(e)}, 409)
            if not m:
                self.json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            v = self.who()
            if not v:
                return
            try:
                if u.path == "/volunteer/api/work":
                    body = json.loads(self.body(2**20) or b"{}")
                    if int(body.get("version") or 0) < CLIENT_VERSION:
                        return self.json({"error": "this script is too old: download it again from /volunteer/"}, 426)
                    return self.json(broker.work(v, _client_ip(self), str(body.get("client") or ""),
                                                 body.get("per_hour"), body.get("per_day")))
                m = re.fullmatch(r"/volunteer/api/tasks/([0-9a-f]{12})/(part|done)", u.path)
                if m and m[2] == "part":
                    one = lambda k: (qs.get(k) or [""])[0]     # noqa: E731
                    data = self.body(PART_MAX)
                    try:
                        got = broker.part(v, m[1], int(one("offset") or -1), int(one("size") or 0), one("ext"), data)
                    except OffsetError as e:
                        return self.json({"error": str(e), "received": e.have}, 409)
                    return self.json({"received": got})
                if m:
                    report = json.loads(self.body(2**20) or b"{}")
                    out = broker.finish(v, m[1], report)
                    log(f"volunteers: {v['name']}: {report.get('status')} {m[1]}" + (f": {report.get('error')}" if report.get("error") else ""))
                    return self.json(out)
                self.json({"error": "not found"}, 404)
            except LookupError as e:
                self.json({"error": str(e)}, 409)
            except ValueError as e:
                self.json({"error": str(e)}, 400)
            except Exception as e:
                log(f"volunteers: error on {u.path}: {type(e).__name__}: {e}")
                self.json({"error": "the server failed at that: try again later"}, 500)

    return Handler


def serve(broker: Broker, host: str, port: int, log: Callable[[str], None] = print) -> ThreadingHTTPServer | None:
    """Serve volunteers on host:port in the background (None if it can't), only /volunteer/..."""
    try:
        httpd = ThreadingHTTPServer((host, port), handler_for(broker, log))
    except OSError as e:
        log(f"volunteers: NOT served on {host}:{port}: {e}")
        return None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"volunteers: served on http://{host}:{port}/volunteer/")
    return httpd
