"""Volunteers: other people's computers fetching YouTube videos, videos' details, searches and channels' pages for this server.

YouTube refuses a server's downloads with its bot check after a few hours of harvesting, and a cloud server's almost
at once (design.md, "When YouTube refuses"). A volunteer runs volunteer/bouldermesh_volunteer.py, which makes a token
and registers it here (registration.py): provisional until approved on the admin page or from a phone notification, and
meanwhile given work only as the admin page's settings allow. A token made on the admin page works at once. It asks for work, fetches one video at a time from its own address, at least as cautiously as this server
would, and sends each back in parts. Work names a video only by its YouTube id, a search only by its words and a
channel only by its id, which the script turns into a youtube.com address itself, so it can't be made to fetch
anything else. A search sends back each result's id, title, description, length and channel; a channel its name and
description.

The broker keeps its own database (volunteers.sqlite), apart from the site's, which deploy/ship.sh replaces, and the
files coming in under DATA_DIR/volunteers until they're whole. It's served on a port of its own (VOLUNTEER_PORT) that
answers only /volunteer/..., so that a proxy in front can pass on just that: Caddy on the public server, to its own
app (option B in design.md), or through an SSH tunnel to another server (option A).
"""
from __future__ import annotations

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

import registration

PER_HOUR = 15               # videos a volunteer, or an address, may be given in an hour (hal's refusals came after 20-30)
PER_DAY = 100
# what the admin page's settings say by default (volunteers.* there): a provisional volunteer's limits, whether one gets
# work at all before it's approved, and how many may register
RULES = {"per_hour": PER_HOUR, "per_day": PER_DAY, "provisional_per_hour": 3, "provisional_per_day": 10,
         "provisional_work": True, "registrations_per_address": 5, "max_waiting": 30}
BLOCK_HOURS = (1, 2, 4, 8)  # a volunteer YouTube refused rests this long, longer each time it's still refused
STALE_MINUTES = 90          # work given out that nothing has come of for this long goes back to the queue
PAUSE_WAIT = 1800           # seconds a volunteer waits before asking again, while the server says it can't take more
MAX_TRIES = 3               # a video that fails this often, for reasons that might pass, isn't asked for again
PART_MAX = 64 * 2**20       # the largest part of a file, under Cloudflare's 100 MB per request
MAX_VIDEO = 600 * 2**20     # the script asks yt-dlp for at most 500 MB
VIDEO_FORMAT = "bv*[height<=1080]/b"   # as the harvest downloads: video only, no audio
VIDEO_EXT = {"mp4", "webm", "mkv", "mov", "m4v", "3gp"}
FAILED_DAYS = 30            # a video that failed for good isn't asked for again for this long
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
WORDS = re.compile(r"[^\x00-\x1f\x7f]{3,200}")
KEYS = {"video": VIDEO_ID, "details": VIDEO_ID, "search": WORDS, "channel": CHANNEL_ID}   # what names each kind of task
OLD_KINDS = ("video", "details")    # what a script that doesn't say does
SEARCH_RESULTS = 15         # results a search asks for, as the server's own (youtube.MAX_RESULTS)
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


def clean_results(results) -> list[dict]:
    """A volunteer's search results, cut to what youtube.result() keeps, with their addresses made here."""
    out = []
    for e in results if isinstance(results, list) else []:
        if isinstance(e, dict) and VIDEO_ID.fullmatch(str(e.get("id") or "")):
            out.append({k: e.get(k) for k in ("id", "title", "description", "duration", "channel", "channel_id", "uploader")})
    return out[:50]


def clean_channel(info: dict, channel_id: str) -> dict:
    """A channel's name and description, as a volunteer read them."""
    text = lambda k, n: str(info[k])[:n] if info.get(k) is not None else None  # noqa: E731
    return {"channel_id": channel_id, "url": f"https://www.youtube.com/channel/{channel_id}", "name": text("name", 200),
            "description": text("description", 20000) or ""}


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
        paused: why no volunteer is to be given work now (as when the server is out of room), or None.
        rules: the admin page's limits, as RULES.
        registered: called with a new volunteer's row when one registers itself.
    """

    def __init__(self, db_path: Path, files: Path, delivered: Callable[[dict, Path | None, dict], None],
                 ready: Callable[[str, str], None], probe: Callable[[Path], float | None] = probe_duration,
                 paused: Callable[[], str | None] = lambda: None, rules: Callable[[], dict] = lambda: RULES,
                 registered: Callable[[dict], None] = lambda v: None):
        self.db_path, self.files, self.delivered, self.ready, self.probe = db_path, files, delivered, ready, probe
        self.paused, self.rules, self.registered = paused, rules, registered
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
            registration.migrate(con, "volunteers")

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
        name = registration.unique_name(name, (r["name"] for r in self.q("SELECT name FROM volunteers")))
        vid, token = secrets.token_hex(4), "bmv_" + secrets.token_urlsafe(32)
        self.q("INSERT INTO volunteers(id,name,token_hash,created,per_hour,per_day,status,approved) VALUES(?,?,?,?,?,?,?,?)", vid,
               name, registration.token_hash(token), time.time(), _limit(per_hour, PER_HOUR), _limit(per_day, PER_DAY),
               "approved", time.time())
        return {"id": vid, "name": name, "token": token}

    def register(self, token: str, name, ip: str, client: str = "") -> dict:
        """A volunteer's script registering the token it made: provisional until approved (registration.register()).
        The new volunteer goes to registered()."""
        rules = {**RULES, **self.rules()}
        before = self.volunteer(token)
        v = registration.register(self.q, "volunteers", token, name, ip, "volunteer", rules["registrations_per_address"],
                                  rules["max_waiting"], {"per_hour": PER_HOUR, "per_day": PER_DAY, "client": str(client)[:80]})
        if not before:
            self.registered(v)
        return v

    def approve(self, vid: str) -> dict | None:
        """A provisional volunteer approved: its limits become everyone's. None for one unknown or revoked."""
        return registration.approve(self.q, "volunteers", vid)

    def revoke(self, vid: str, status: str | None = None) -> None:
        """Its token stops working, and what it was fetching goes back to the queue. status "denied" for one never
        approved."""
        self.q("UPDATE volunteers SET revoked=?, status=COALESCE(?, status) WHERE id=?", time.time(), status, vid)
        self.q("UPDATE tasks SET state='queued', volunteer=NULL WHERE volunteer=? AND state='assigned'", vid)

    def volunteer(self, token: str) -> dict | None:
        return self.q("SELECT * FROM volunteers WHERE token_hash=?", registration.token_hash(token), one=True)

    def get(self, vid: str) -> dict | None:
        return self.q("SELECT * FROM volunteers WHERE id=?", vid, one=True)

    def any(self) -> bool:
        """Whether there's a volunteer who could fetch what's wanted: one approved, or provisional ones while they may."""
        if {**RULES, **self.rules()}["provisional_work"]:
            return bool(self.q("SELECT 1 FROM volunteers WHERE revoked IS NULL LIMIT 1"))
        return bool(self.q("SELECT 1 FROM volunteers WHERE revoked IS NULL AND COALESCE(status,'approved')!='provisional' LIMIT 1"))

    # -- what's wanted --------------------------------------------------------------------------------------------
    def want(self, kind: str, video_id: str, purpose: str, ref: str) -> bool:
        """Ask volunteers for a video ("video") or its details ("details") by its id, a search ("search") by its
        words, or a channel's name and description ("channel") by its id, for purpose and ref (as ready() is called
        with). Whether it's coming: false for one that failed for good lately. (The tasks table keeps the id or words
        as video_id.)"""
        if kind not in KEYS or not KEYS[kind].fullmatch(video_id or ""):
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
    def pending(self, purpose: str, ref: str) -> int:
        """How many things wanted for this purpose and ref are still coming."""
        return self.q("SELECT count(DISTINCT t.id) AS n FROM wants w JOIN tasks t ON t.id=w.task WHERE w.purpose=? AND w.ref=? "
                      "AND t.state IN ('queued','assigned')", purpose, ref, one=True)["n"]

    def work(self, v: dict, ip: str, client: str = "", per_hour: int | None = None, per_day: int | None = None,
             kinds: list | None = None) -> dict:
        """The next thing for a volunteer to fetch, {"task": ...}, or {"wait": seconds, "why": ...}: what it was
        given and hasn't finished, then the oldest wanted of the kinds its script does (videos and searches before
        details and channels), within its limits and its address's."""
        kinds = [k for k in (kinds or OLD_KINDS) if k in KEYS] or list(OLD_KINDS)
        now = time.time()
        with self.lock:
            self.requeue_stale()
            self.q("UPDATE volunteers SET last_seen=?, last_ip=?, client=? WHERE id=?", now, ip, client[:80], v["id"])
            why = self.paused()
            if why:     # what it has stays its own, to send when it can
                return {"wait": PAUSE_WAIT, "why": why}
            rules = {**RULES, **self.rules()}
            waiting = registration.provisional(v)
            if waiting and not rules["provisional_work"]:
                return {"wait": PAUSE_WAIT, "why": "waiting for approval by whoever runs this site", "provisional": True}
            if (v.get("refused_until") or 0) > now:
                return {"wait": v["refused_until"] - now, "why": "YouTube refused you lately: resting"}
            mine = self.q("SELECT * FROM tasks WHERE volunteer=? AND state='assigned'", v["id"], one=True)
            if mine:
                return {"task": self._task_view(mine)}
            most_hour, most_day = ((rules["provisional_per_hour"], rules["provisional_per_day"]) if waiting else
                                   (rules["per_hour"], rules["per_day"]))
            hour = min(_limit(per_hour, v["per_hour"] or PER_HOUR), _limit(most_hour, PER_HOUR))
            day = min(_limit(per_day, v["per_day"] or PER_DAY), _limit(most_day, PER_DAY))
            for span, cap, what in ((3600, hour, "an hour"), (86400, day, "a day")):
                for field, value, whose in (("volunteer", v["id"], "you"), ("ip", ip, "your address")):
                    rows = self.q(f"SELECT at FROM given WHERE {field}=? AND at>? ORDER BY at", value, now - span)
                    if len(rows) >= cap:
                        return {"wait": rows[len(rows) - cap]["at"] + span - now + 1,
                                "why": f"{cap} {what} is the most for {whose}" + (" until you're approved" if waiting else "")}
            task = self.q(f"SELECT * FROM tasks WHERE state='queued' AND kind IN ({','.join('?' * len(kinds))}) "
                          "ORDER BY kind IN ('details','channel'), created LIMIT 1", *kinds, one=True)
            if not task:
                return {"wait": 600, "why": "nothing wanted now"}
            self.q("UPDATE tasks SET state='assigned', volunteer=?, ip=?, assigned=?, touched=?, size=NULL, ext=NULL WHERE id=?",
                   v["id"], ip, now, now, task["id"])
            self.q("INSERT INTO given(volunteer,ip,at) VALUES(?,?,?)", v["id"], ip, now)
            self.q("DELETE FROM given WHERE at<?", now - 2 * 86400)
            (self.files / f"{task['id']}.part").unlink(missing_ok=True)
        return {"task": self._task_view(task)}

    def _task_view(self, t: dict) -> dict:
        if t["kind"] == "search":
            return {"id": t["id"], "kind": "search", "query": t["video_id"], "max_results": SEARCH_RESULTS}
        if t["kind"] == "channel":
            return {"id": t["id"], "kind": "channel", "channel_id": t["video_id"]}
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
        """What came of a task: {"status": "ok", "info": details} (a video sent in full first; for a search,
        "results": [...] instead, and for a channel "info" is its name and description), "refused" (YouTube's bot
        check, or a rate limit: the volunteer rests), "released" (the volunteer stopped), or "failed" with "error", and
        "permanent" for a video that won't ever come (private, removed)."""
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
            if t["kind"] == "search":
                info = {"query": t["video_id"], "results": clean_results(report.get("results"))}
            elif t["kind"] == "channel":
                info = clean_channel(report.get("info") or {}, t["video_id"])
            else:
                info = clean_details(report.get("info") or {}, t["video_id"])
            path = None
            if t["kind"] == "video":
                if not f.exists() or t["size"] is None or f.stat().st_size != t["size"]:
                    raise ValueError("the video hasn't all come: send the rest first")
                path = f.with_name(f"{task_id}.{t['ext']}")
                f.rename(path)
                length = self.probe(path)
                listed = self.listed_duration(t["video_id"], v["id"])
                off = lambda d: d and abs(length - d) > max(5, 0.1 * d)  # noqa: E731
                if length is None or off(info["duration"]) or off(listed):
                    path.unlink(missing_ok=True)
                    self._failed(t, "the file didn't look like that video" if length is None else
                                 f"the file is {length:.0f} s long, the video {(listed if off(listed) else info['duration']):.0f} s"
                                 + (" by another's search" if off(listed) else ""), False)
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

    def listed_duration(self, video_id: str, besides: str) -> float | None:
        """The video's length as someone other than this volunteer saw it: in a search's results, or its details,
        that another volunteer sent lately. The volunteer sending a video also says how long it is, which only catches
        a file that went wrong, not one sent in its place on purpose."""
        for t in self.q("SELECT kind, info FROM tasks WHERE state='done' AND kind IN ('search','details') AND volunteer!=? "
                        "AND (kind='search' OR video_id=?) ORDER BY finished DESC LIMIT 500", besides, video_id):
            try:
                info = json.loads(t["info"] or "{}")
            except ValueError:
                continue
            found = [info] if t["kind"] == "details" else [r for r in info.get("results") or [] if r.get("id") == video_id]
            for r in found:
                try:
                    if r.get("duration"):
                        return float(r["duration"])
                except (TypeError, ValueError):
                    pass
        return None

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
        rules = {**RULES, **self.rules()}
        for v in self.q("SELECT * FROM volunteers ORDER BY created"):
            given = self.q("SELECT at FROM given WHERE volunteer=? AND at>?", v["id"], now - 86400)
            fetching = self.q("SELECT kind, video_id, size, touched FROM tasks WHERE volunteer=? AND state='assigned'", v["id"], one=True)
            waiting = registration.provisional(v)
            if v["revoked"]:
                status = "denied" if v.get("status") == "denied" else "revoked"
            elif waiting and not rules["provisional_work"]:
                status = "awaiting approval"
            elif (v["refused_until"] or 0) > now:
                status = "resting"
            elif fetching:
                status = "fetching"
            elif v["last_seen"] and now - v["last_seen"] < 1800:
                status = "waiting for work"
            else:
                status = "not connected"
            hour, day = ((rules["provisional_per_hour"], rules["provisional_per_day"]) if waiting else (rules["per_hour"], rules["per_day"]))
            vols.append({"id": v["id"], "name": v["name"], "status": status, "last_seen": v["last_seen"], "address": v["last_ip"],
                         "client": v["client"], "refused_until": v["refused_until"] if (v["refused_until"] or 0) > now else None,
                         "per_hour": min(v["per_hour"] or PER_HOUR, hour), "per_day": min(v["per_day"] or PER_DAY, day), "done": v["done"],
                         "fetching": fetching, "provisional": waiting, "registered": v.get("registered_ip") is not None,
                         "created": v["created"], "last_hour": sum(g["at"] > now - 3600 for g in given), "last_day": len(given)})
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


COPY_ICON = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
             'stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/>'
             '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>')
PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Volunteer for BoulderMesh</title>
<style>
:root{color-scheme:light dark;--code:#f2f2f2;--line:#ccc;--ok:#2a8a3a;--acc:#1a73c8;--acc-fg:#fff;--mute:#666}
@media (prefers-color-scheme:dark){:root{--code:#1e1e1e;--line:#444;--ok:#5fd68a;--acc:#5cc8ff;--acc-fg:#001018;--mute:#999}}
body{font:16px/1.5 system-ui,sans-serif;max-width:42em;margin:2em auto;padding:0 16px}
h2{font-size:1.25em;margin:1.2em 0 .3em} h3{font-size:1.05em;margin:1.3em 0 .3em}
code{background:var(--code);padding:1px 4px;border-radius:3px}
.choose{display:grid;gap:12px;margin:1.2em 0}
.choose a{display:block;padding:18px 20px;border-radius:10px;border:2px solid var(--acc);color:inherit;text-decoration:none;font-size:1.2em;
  font-weight:600;line-height:1.3}
.choose a small{display:block;font-weight:400;font-size:.8em;color:var(--mute);margin-top:4px}
.choose a.on{background:var(--acc);color:var(--acc-fg)} .choose a.on small{color:inherit;opacity:.85}
section{display:none} section.on{display:block}
.box{position:relative;margin:6px 0 10px}
.box pre{background:var(--code);border:1px solid var(--line);border-radius:6px;padding:10px 46px 10px 12px;margin:0;
  white-space:pre-wrap;overflow-wrap:anywhere}
.copy{position:absolute;top:6px;right:6px;background:transparent;color:inherit;border:1px solid var(--line);border-radius:5px;padding:5px;
  line-height:0;cursor:pointer}
.copy.done{border-color:var(--ok);color:var(--ok)}
ul{padding-left:1.2em} li{margin:.2em 0}
</style>
<h1>Volunteer for BoulderMesh</h1>
<p>BoulderMesh makes 3D models of boulders from photos and climbing videos. You can help in two ways, from your own
computer. Either script makes a token for you, asks your name (you can leave it out), and registers itself with this
site; whoever runs it then approves it.</p>
<div class="choose">
  <a href="#videos" data-show="videos">Help collect videos<small>Fetch YouTube videos of climbs from your address, slowly. Python is all it needs.</small></a>
  <a href="#compute" data-show="compute">Help compute meshes<small>(GPU required) Turn climbs' photos into 3D models on your computer, in Docker.</small></a>
</div>

<section id="videos">
<h2>Help collect videos</h2>
<p>YouTube stops one server from fetching many videos, so volunteers fetch some from their own computers, slowly: at most
15 an hour and 100 a day, one at a time. You need Python 3.9 or newer.</p>
<h3>1. Get the script</h3>
<div class="box"><pre>curl -O {base}/volunteer/bouldermesh_volunteer.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p><a href="/volunteer/bouldermesh_volunteer.py" download="bouldermesh_volunteer.py">Or download it here</a>. On Windows,
<code>curl.exe</code> in place of <code>curl</code>.</p>
<h3>2. Run it</h3>
<div class="box"><pre>python3 bouldermesh_volunteer.py --server {base}</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p>On Windows, <code>py</code> in place of <code>python3</code>. The first time, it makes your token and asks your name
(optional). It remembers the server and the token (in <code>.bouldermesh-volunteer</code> in your home folder, readable
only by you), so after that it's only <code>python3 bouldermesh_volunteer.py</code>. Until whoever runs this site
approves you, it fetches only a few videos a day, or none.</p>
<p>The first time, it explains what it does and asks you to agree. Among other things: YouTube's terms don't allow
downloading videos except through YouTube itself, and if YouTube decides your address is fetching too much, everyone on
your network may be asked to sign in or prove they're not a bot for a while.</p>
</section>

<section id="compute">
<h2>Help compute meshes</h2>
<p>A small script asks this site for a climb to mesh, fetches its photos, runs the reconstruction in this site's own
Docker image on your computer's GPU, and sends back the mesh: one climb at a time, each most of an hour. Only your
computer connects out, over HTTPS; nothing needs to reach it. Ctrl-C stops it, and the climb goes to another computer.</p>
<h3>What it needs</h3>
<ul>
<li><b>An NVIDIA GPU</b>, with a current driver (535 or newer): GTX 900 series or newer (CUDA compute capability 5.0), and
8 GB of its memory or more for the most help (less will still match photos; placing the hardest ones uses as much as is
free).</li>
<li><b>Linux on an x86-64 computer</b>, with Docker and NVIDIA's Container Toolkit, Docker usable without sudo (in the
<code>docker</code> group), and Python 3.9 or newer. On Windows, use WSL 2 with Docker's GPU support, and run everything
below inside it. A Mac won't do: it has no NVIDIA GPU, and Docker on a Mac can't use the one it has.</li>
<li><b>16 GB of memory</b> or more (32 is better), and as many CPU cores as you'll give it: some steps use only the CPU.</li>
<li><b>Disk:</b> about 18 GB where Docker keeps its images (it checks before downloading the image), and 12 GB where the
script keeps its jobs and the models they download, which <code>--data</code> can put on another disk.</li>
<li><b>Internet:</b> a climb's photos are 50 to 300 MB down, and its mesh 20 to 60 MB up.</li>
</ul>
<h3>1. Get the script</h3>
<div class="box"><pre>curl -O {base}/worker/bouldermesh_worker.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p><a href="/worker/bouldermesh_worker.py" download="bouldermesh_worker.py">Or download it here</a>.</p>
<h3>2. Check it can work</h3>
<div class="box"><pre>python3 bouldermesh_worker.py --server {base} --check</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p>The first time, it makes this machine's token, asks your name (optional) and registers with this site, which then
waits for whoever runs it to approve the machine before giving it climbs. It says whether it's approved yet, and whether
there's room for jobs and the image; short of room, it says what's short. Add <code>--cpus N</code> to limit the cores jobs use, and <code>--data DIR</code> to keep jobs and models
on another disk. It remembers them all, with the server and the token (in <code>.bouldermesh-worker</code> in your home
folder, readable only by you).</p>
<h3>3. Run it</h3>
<div class="box"><pre>python3 bouldermesh_worker.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<h3>Optional: start it by itself, whenever the computer starts</h3>
<p>As a systemd service of your own, not root's. Put the script where it'll stay (here, your home folder), then:</p>
<div class="box"><pre>mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/bouldermesh-worker.service &lt;&lt;'EOF'
[Unit]
Description=BoulderMesh: meshes for {base}

[Service]
ExecStart=/usr/bin/python3 %h/bouldermesh_worker.py
Restart=always
RestartSec=60

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now bouldermesh-worker
loginctl enable-linger "$USER"</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p>The last line lets it run while you're logged out, and start with the computer (it may ask for your password).
<code>journalctl --user -u bouldermesh-worker -f</code> shows what it's doing, and
<code>systemctl --user disable --now bouldermesh-worker</code> stops it for good.</p>
</section>

<script>
for (const b of document.querySelectorAll('.copy')) b.onclick = async () => {
  const pre = b.previousElementSibling;
  try { await navigator.clipboard.writeText(pre.textContent); }
  catch { const r = document.createRange(); r.selectNodeContents(pre); getSelection().removeAllRanges(); getSelection().addRange(r); document.execCommand('copy'); }
  b.classList.add('done'); b.title = 'Copied';
  setTimeout(() => { b.classList.remove('done'); b.title = 'Copy'; }, 1500);
};
// the two ways to help: one shows at a time, from the buttons or the address's #videos or #compute
function show(which){
  for (const s of document.querySelectorAll('section')) s.classList.toggle('on', s.id === which);
  for (const a of document.querySelectorAll('.choose a')) a.classList.toggle('on', a.dataset.show === which);
}
addEventListener('hashchange', () => show(location.hash.slice(1)));
show(location.hash.slice(1));
</script>
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
                self.json({"error": "unknown token: run the script without --token, and it makes one and registers it"}, 401)
                return None
            if v["revoked"]:
                self.json({"error": "this token was turned down by whoever runs this site" if v.get("status") == "denied" else
                           "this token no longer works"}, 403)
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
                return self.send(200, PAGE.replace("{base}", base).replace("{icon}", COPY_ICON).encode(), "text/html; charset=utf-8")
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

        def register(self) -> None:
            """A script registering the token it made (the Authorization header's), with its name if it gave one."""
            auth = self.headers.get("Authorization", "")
            try:
                body = json.loads(self.body(2**16) or b"{}")
                if int(body.get("version") or 0) < CLIENT_VERSION:
                    return self.json({"error": "this script is too old: download it again from /volunteer/"}, 426)
                v = broker.register(auth[7:].strip() if auth.startswith("Bearer ") else "", body.get("name"), _client_ip(self),
                                    str(body.get("client") or ""))
            except registration.Refused as e:
                return self.json({"error": str(e)}, e.code)
            except ValueError as e:
                return self.json({"error": str(e)}, 400)
            if v["revoked"]:
                return self.json({"error": "this token no longer works"}, 403)
            rules = {**RULES, **broker.rules()}
            waiting = registration.provisional(v)
            log(f"volunteers: {v['name']} registered from {_client_ip(self)}" if waiting else f"volunteers: {v['name']} registered again")
            self.json({"id": v["id"], "name": v["name"], "status": "provisional" if waiting else "approved",
                       "may_work": not waiting or rules["provisional_work"],
                       "per_hour": rules["provisional_per_hour" if waiting else "per_hour"],
                       "per_day": rules["provisional_per_day" if waiting else "per_day"]})

        def do_POST(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/volunteer/api/register":
                return self.register()
            v = self.who()
            if not v:
                return
            try:
                if u.path == "/volunteer/api/work":
                    body = json.loads(self.body(2**20) or b"{}")
                    if int(body.get("version") or 0) < CLIENT_VERSION:
                        return self.json({"error": "this script is too old: download it again from /volunteer/"}, 426)
                    return self.json(broker.work(v, _client_ip(self), str(body.get("client") or ""),
                                                 body.get("per_hour"), body.get("per_day"), body.get("kinds")))
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
