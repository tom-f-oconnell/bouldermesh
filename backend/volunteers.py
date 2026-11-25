"""Volunteers: other people's computers fetching YouTube videos, videos' details, searches and channels' pages for this server.

YouTube refuses a server's downloads with its bot check after a few hours of harvesting, and a cloud server's almost
at once (design.md, "When YouTube refuses"). A volunteer runs volunteer/bouldermesh_volunteer.py, which makes a token
and registers it here (registration.py): provisional until approved on the admin page or from a phone notification, and
meanwhile given work only as the admin page's settings allow. A token made on the admin page works at once. It asks for work, fetches one video at a time from its own address, at least as cautiously as this server
would, and sends each back in parts. Work names a video only by its YouTube id, a search only by its words and a
channel only by its id, which the script turns into a youtube.com address itself, so it can't be made to fetch
anything else; a storyboard (YouTube's small frames of a video, storyboards.py) by addresses the script checks are
i.ytimg.com's, of that video. A search sends back each result's id, title, description, length and channel; a channel its name and
description.

The broker keeps its own database (volunteers.sqlite), apart from the site's, which deploy/ship.sh replaces, and the
files coming in under DATA_DIR/volunteers until they're whole. It's served on a port of its own (VOLUNTEER_PORT) that
answers only /volunteer/..., so that a proxy in front can pass on just that: Caddy on the public server, to its own
app (option B in design.md), or through an SSH tunnel to another server (option A).
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import random
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

import dbconn
import presence
import registration
import storyboards
import zipfile

PER_HOUR = 15               # videos a volunteer, or an address, may be given in an hour (hal's refusals came after 20-30)
PER_DAY = 200
# Searches and channels' pages: an allowance of their own, apart from videos and videos' details, which go through
# YouTube's player, where its bot check is. hal's searches went on through every spell of its refusals.
PAGES_PER_HOUR, PAGES_PER_DAY = 30, 200
PAGE_KINDS = ("search", "channel", "storyboard")    # storyboards: a request to i.ytimg.com, counted with pages
SCRIPT_DEFAULTS = (15, 200)     # what the script fetches at most without being told less, from version 10 (100 a day before)
PAGE_SQL = "(" + ",".join(f"'{k}'" for k in PAGE_KINDS) + ")"
ONE = {"videos": "video", "searches": "search"}
# what the admin page's settings say by default (volunteers.* there): a provisional volunteer's limits, whether one gets
# work at all before it's approved, and how many may register
RULES = {"per_hour": PER_HOUR, "per_day": PER_DAY, "provisional_per_hour": 3, "provisional_per_day": 10,
         "provisional_work": True, "registrations_per_address": 5, "max_waiting": 30,
         "yt_dlp": "nightly",  # which yt-dlp volunteers' scripts install: its nightly builds (PyPI's pre-releases) or stable
         # the share of videos checked against their storyboards, fetched by a trusted volunteer (check()): of those from
         # volunteers not trusted, and from trusted ones; and whether a trusted one may ask YouTube's player for a
         # storyboard's addresses itself, when those the sender got don't work from another address
         "check_new": 1.0, "check_trusted": 0.2, "check_fresh": True}
BLOCK_HOURS = (1, 2, 4, 8)  # a volunteer YouTube refused rests this long, longer each time it's still refused
THROTTLE_REST = 3600        # a volunteer YouTube throttled rests this long (seconds), without the refusals' growing rests
TROUBLE = ("refused", "throttled", "degraded")   # how YouTube treats a volunteer, as its script sees it, worst first
LOG_DAYS = 180              # youtube_log keeps every task's outcome this long, to compare yt-dlp versions and days by
# what a script's error says when YouTube refuses it (the script's own REFUSAL): the line a notification quotes
REFUSAL = re.compile(r"not a bot|HTTP Error 429|Too Many Requests|content isn.t available, try again later", re.I)
WIDESPREAD = (6 * 3600, 2, 0.5)     # degraded for at least 2 volunteers, and half of those heard from, within 6 h: YouTube's
                                    # change, which a newer yt-dlp is likely to fix, not one address's trouble
STALE_MINUTES = 90          # work given out that nothing has come of for this long goes back to the queue
PAUSE_WAIT = 1800           # seconds a volunteer waits before asking again, while the server says it can't take more
MAX_TRIES = 3               # a video that fails this often, for reasons that might pass, isn't asked for again
PART_MAX = 64 * 2**20       # the largest part of a file, under Cloudflare's 100 MB per request
MAX_VIDEO = 600 * 2**20     # the script asks yt-dlp for at most 500 MB
IDLE_WAIT = 60              # seconds an idle volunteer waits before asking again, so that a harvest's asks start soon
# a volunteer not heard from for this long has left (presence.py), unless it said goodbye: scripts from version 6 keep in
# touch while they wait (at least every 10 min); older ones wait silently through a day's limit or a rest
GONE_AFTER, GONE_AFTER_OLD = 30 * 60, 26 * 3600
VIDEO_FORMAT = "bv*[height<=1080]/b"   # as the harvest downloads: video only, no audio
VIDEO_EXT = {"mp4", "webm", "mkv", "mov", "m4v", "3gp"}
FAILED_DAYS = 30            # a video that failed for good isn't asked for again for this long
# what YouTube says of a video that won't come however often it's asked: removed, private, members-only, or its account
# closed. Volunteer scripts say so themselves (their FOR_GOOD, with more), but before version 7 missed "This video is
# unavailable", which was then asked for 3 times: this catches that from any script. Not "try again later" (a refusal).
FOR_GOOD = re.compile(r"video unavailable|video is unavailable|video is no longer available|private video|video is private|"
                      r"has been removed|members[- ]only|account associated with this video has been terminated", re.I)
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
WORDS = re.compile(r"[^\x00-\x1f\x7f]{3,200}")
KEYS = {"video": VIDEO_ID, "details": VIDEO_ID, "search": WORDS, "channel": CHANNEL_ID, "storyboard": VIDEO_ID}   # what names each kind of task
OLD_KINDS = ("video", "details")    # what a script that doesn't say does
SEARCH_RESULTS = 15         # results a search asks for, as the server's own (youtube.MAX_RESULTS)
DETAILS = {"title": 500, "uploader": 200, "channel": 200, "channel_id": 100, "channel_url": 300, "uploader_url": 300,
           "upload_date": 20, "description": 20000, "license": 200}     # yt-dlp's names, as youtube.details() reads them
SCRIPT = Path(__file__).resolve().parents[1] / "volunteer" / "bouldermesh_volunteer.py"
CHECK_DAYS = 2             # a check no trusted volunteer has done in this long is given up
CHECK_KEEP_DAYS = 14        # what a check compared (the video's frames, the storyboard) is kept this long, to look at
STORYBOARD_MAX = 8 * 2**20  # a storyboard's images, zipped, at most
CLIENT_VERSION = 1          # the oldest script that speaks to this
# Trials: how a volunteer's script runs yt-dlp, chosen for it on the admin page, to compare one way with another in
# youtube_log (A/B tests). Each is a name and what it sets, from the few things scripts from version 14 let the server
# choose (their own TRIAL_KEYS): player_client, yt-dlp's YouTube clients for videos and their details. Older scripts
# carry on as they are. None now: the mweb trial (Nov 17-18, 2026) is over (design.md, "PO tokens and mweb didn't help").
TRIALS: dict[str, dict] = {}


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


RANK_UNWANTED = 1    # next_task()'s rank for a task nothing wants any more


class Broker:
    """Volunteers, the work for them, and what they send back.

    Args:
        db_path: its database.
        files: where files come in.
        delivered: called with (task, path to the video or None, its details) when a task is done; the video is
            the callee's to move.
        ready: called with (purpose, ref) once every video wanted for that is done or has failed, at least one
            having come: ("harvest", project id), ("beta", mesh id), ("details", url). False back means not now, kept
            for resettle().
        probe: a video file's length, to check it's the one asked for (probe_duration()).
        paused: why no volunteer is to be given work now (as when the server is out of room), or None.
        rules: the admin page's limits, as RULES.
        registered: called with a new volunteer's row when one registers itself.
        troubled: called with (the volunteer's row, its health before, its health now, why) when its script says YouTube
            refuses, throttles or degrades it, and it didn't before (or less so).
        widespread: called with (how many, of how many, an example) when many volunteers say YouTube is degrading them at
            once (WIDESPREAD), at most once in that span.
        came_went: called with (a volunteer's row, "here" or "gone", why) when it starts being heard from or stops, not as
            it rests or waits for work (presence.py).
        rank: called with (purpose, ref) for how urgent what a task is wanted for is: lower is given first (next_task()),
            the oldest first among equals. All the same by default.
    """

    def __init__(self, db_path: Path, files: Path, delivered: Callable[[dict, Path | None, dict], None],
                 ready: Callable[[str, str], bool | None], probe: Callable[[Path], float | None] = probe_duration,
                 paused: Callable[[], str | None] = lambda: None, rules: Callable[[], dict] = lambda: RULES,
                 registered: Callable[[dict], None] = lambda v: None,
                 troubled: Callable[[dict, str | None, str, str], None] = lambda v, was, now, why: None,
                 widespread: Callable[[int, int, str], None] = lambda n, of, example: None,
                 came_went: Callable[[dict, str, str], None] = lambda v, state, why: None,
                 downloaded: Callable[[dict], None] = lambda info: None,
                 leaderboard: Callable[[], dict] | None = None, rank: Callable[[str, str], float] = lambda purpose, ref: 0,
                 checked: Callable[[dict, dict], None] = lambda check, sender: None):
        self.db_path, self.files, self.delivered, self.ready, self.probe = db_path, files, delivered, ready, probe
        self.paused, self.rules, self.registered, self.troubled, self.widespread = paused, rules, registered, troubled, widespread
        self.came_went, self.downloaded, self.rank, self.checked = came_went, downloaded, rank, checked
        self.leaderboard = leaderboard or (lambda: {"volunteers": self.contributions(), "workers": []})
        self.widespread_said = 0.0      # when widespread() was last called
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
                CREATE TABLE IF NOT EXISTS youtube_log(at REAL, volunteer TEXT, kind TEXT, status TEXT, health TEXT,
                  detail TEXT, yt_dlp TEXT, kbps REAL, pot TEXT);
                CREATE INDEX IF NOT EXISTS youtube_log_at ON youtube_log(at);
                CREATE TABLE IF NOT EXISTS checks(id TEXT PRIMARY KEY, video_id TEXT, volunteer TEXT, ip TEXT, video_task TEXT,
                  spec TEXT, state TEXT, task TEXT, checker TEXT, mode TEXT, given_error TEXT, verdict TEXT, result TEXT,
                  error TEXT, created REAL, finished REAL);
                CREATE INDEX IF NOT EXISTS checks_task ON checks(task);
            """)
            if "kind" not in {r[1] for r in con.execute("PRAGMA table_info(given)")}:   # rows from before count as videos
                con.execute("ALTER TABLE given ADD COLUMN kind TEXT")
            if con.execute("PRAGMA user_version").fetchone()[0] < 1:
                # a volunteer's own limits are kept only when someone chose them; before, everyone was given the limits
                # of the day (15 and 100), which then held them to those whatever the site's became
                con.execute("UPDATE volunteers SET per_hour=NULL WHERE per_hour=15")
                con.execute("UPDATE volunteers SET per_day=NULL WHERE per_day=100")
                con.execute("PRAGMA user_version=1")
            registration.migrate(con, "volunteers")
            logged = {r[1] for r in con.execute("PRAGMA table_info(youtube_log)")}
            if "pot" not in logged:     # whether it used a PO token (script 13)
                con.execute("ALTER TABLE youtube_log ADD COLUMN pot TEXT")
            if "trial" not in logged:   # the trial its script ran under (script 14), as it said
                con.execute("ALTER TABLE youtube_log ADD COLUMN trial TEXT")
            have = {r[1] for r in con.execute("PRAGMA table_info(volunteers)")}
            # how YouTube treats it, as its script last said (health: ok, or one of TROUBLE), and why; the speed of its
            # last whole video, in KB/s; its yt-dlp; why it rests (rest_why: "refused" or "throttled"); the limits its
            # script last asked for (its own, or what its volunteer gave it); the trial it's in (TRIALS), if any
            for name, kind in (("health", "TEXT"), ("health_detail", "TEXT"), ("health_at", "REAL"), ("kbps", "REAL"),
                               ("yt_dlp", "TEXT"), ("rest_why", "TEXT"), ("asked_per_hour", "INTEGER"), ("asked_per_day", "INTEGER"),
                               ("trial", "TEXT")):
                if name not in have:
                    con.execute(f"ALTER TABLE volunteers ADD COLUMN {name} {kind}")
            if "trusted" not in have:  # whose storyboards check others' videos, and whose own are checked less (check())
                con.execute("ALTER TABLE volunteers ADD COLUMN trusted INTEGER")
                con.execute("UPDATE volunteers SET trusted=1 WHERE revoked IS NULL AND COALESCE(status,'approved')!='provisional' "
                            "AND done>=100")
            presence.migrate(con, "volunteers", GONE_AFTER_OLD)

    def db(self) -> sqlite3.Connection:
        con = dbconn.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def q(self, sql: str, *args, one: bool = False) -> list[dict] | dict | None:
        with self.lock, self.db() as con:
            rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        return (rows[0] if rows else None) if one else rows

    # -- volunteers ---------------------------------------------------------------------------------------------
    def add(self, name: str, per_hour: int | None = None, per_day: int | None = None) -> dict:
        """A new volunteer: {id, name, token}. The token is shown this once; only its hash is kept. Its own limits only
        if given (within PER_HOUR and PER_DAY); else the site's apply, as they are at the time (set_limits())."""
        name = (name or "").strip()[:80]
        if not name:
            raise ValueError("give the volunteer a name")
        name = registration.unique_name(name, (r["name"] for r in self.q("SELECT name FROM volunteers")))
        vid, token = secrets.token_hex(4), "bmv_" + secrets.token_urlsafe(32)
        self.q("INSERT INTO volunteers(id,name,token_hash,created,per_hour,per_day,status,approved) VALUES(?,?,?,?,?,?,?,?)", vid,
               name, registration.token_hash(token), time.time(), _own(per_hour, PER_HOUR), _own(per_day, PER_DAY),
               "approved", time.time())
        return {"id": vid, "name": name, "token": token}

    def set_limits(self, vid: str, per_hour=None, per_day=None) -> dict | None:
        """A volunteer's own limits, within PER_HOUR and PER_DAY; None (or "") for either: the site's, whatever they
        are. None for one unknown."""
        self.q("UPDATE volunteers SET per_hour=?, per_day=? WHERE id=?", _own(per_hour, PER_HOUR), _own(per_day, PER_DAY), vid)
        return self.get(vid)

    def set_trial(self, vid: str, trial: str | None) -> dict | None:
        """Put a volunteer in one of TRIALS, or none (None or ""), from its next ask for work. None for one unknown."""
        trial = trial or None
        if trial is not None and trial not in TRIALS:
            raise ValueError(f"no such trial: {trial}")
        self.q("UPDATE volunteers SET trial=? WHERE id=?", trial, vid)
        return self.get(vid)

    def set_trusted(self, vid: str, trusted: bool) -> dict | None:
        """Trust a volunteer, or stop: a trusted one fetches storyboards to check others' videos by, and its own videos
        are checked less often (RULES' check_trusted)."""
        self.q("UPDATE volunteers SET trusted=? WHERE id=? AND revoked IS NULL", 1 if trusted else None, vid)
        return self.get(vid)

    @staticmethod
    def trial_for(v: dict) -> dict | None:
        """What a volunteer's script is told of its trial: {"name", and what it sets}, or None."""
        t = TRIALS.get(v.get("trial") or "")
        return {"name": v["trial"], **{k: x for k, x in t.items() if k != "what"}} if t else None

    def register(self, token: str, name, ip: str, client: str = "") -> dict:
        """A volunteer's script registering the token it made: provisional until approved (registration.register()).
        The new volunteer goes to registered()."""
        rules = {**RULES, **self.rules()}
        before = self.volunteer(token)
        v = registration.register(self.q, "volunteers", token, name, ip, "volunteer", rules["registrations_per_address"],
                                  rules["max_waiting"], {"client": str(client)[:80]})
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

    def forget_gone(self, now: float | None = None) -> int:
        """Forget volunteers revoked or denied that never fetched anything and registered over a day ago (registrations
        from an address are counted for a day by their rows); those that fetched something stay, for the leaderboard.
        How many went."""
        now = now or time.time()
        gone = [r["id"] for r in self.q("SELECT id FROM volunteers WHERE revoked IS NOT NULL AND COALESCE(done, 0)=0 AND "
                                        "created<? AND id NOT IN (SELECT volunteer FROM tasks WHERE state='done' AND "
                                        "volunteer IS NOT NULL)", now - 86400)]
        for vid in gone:
            self.q("DELETE FROM given WHERE volunteer=?", vid)
            self.q("DELETE FROM volunteers WHERE id=?", vid)
        return len(gone)

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
    def next_task(self, kinds: list[str], v: dict | None = None, ip: str = "", fresh: bool = True) -> dict | None:
        """The queued task of these kinds to give next: storyboards first (quick, and their addresses may not last),
        then by rank() (the most urgent of what each is wanted for), then searches and videos before details and
        channels, then oldest first. A storyboard isn't given to the volunteer whose video it checks, nor to its
        address, nor one that needs YouTube's player (a "fresh" check) unless fresh."""
        rows = self.q(f"SELECT t.*, (SELECT group_concat(w.purpose || char(31) || w.ref, char(30)) FROM wants w WHERE w.task=t.id) "
                      f"AS wanted FROM tasks t WHERE t.state='queued' AND t.kind IN ({','.join('?' * len(kinds))}) "
                      f"AND (t.kind!='storyboard' OR NOT EXISTS (SELECT 1 FROM checks c WHERE c.task=t.id AND "
                      f"(c.volunteer=? OR c.ip=? OR (c.mode='fresh' AND ?=0))))", *kinds, (v or {}).get("id"), ip, int(fresh))
        if not rows:
            return None

        def rank(row: dict) -> float:
            wants = [w.split("\x1f", 1) for w in (row["wanted"] or "").split("\x1e") if "\x1f" in w]
            try:
                return min((self.rank(p, r) for p, r in wants), default=RANK_UNWANTED)
            except Exception as e:  # noqa: BLE001 - oldest first, as before, rather than none given
                print(f"volunteers: ranking a task failed: {e!r}", flush=True)
                return 0
        best = min(rows, key=lambda r: (r["kind"] != "storyboard", rank(r), r["kind"] in ("details", "channel"), r["created"]))
        best.pop("wanted", None)
        return best

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
        self._expire_checks()

    # -- the volunteer's side ---------------------------------------------------------------------------------------
    def pending_tasks(self, purpose: str, refs) -> list[dict]:
        """What's still coming for this purpose and any of these refs: each task once, with when it was last moved on
        ("moved": wanted, given out, or heard of from its volunteer, whichever came last)."""
        refs = list(dict.fromkeys(refs))
        if not refs:
            return []
        return self.q(f"SELECT DISTINCT t.id, t.kind, t.state, t.created, t.assigned, t.touched, t.tries, "
                      f"max(t.created, COALESCE(t.assigned, 0), COALESCE(t.touched, 0)) AS moved FROM wants w JOIN tasks t ON t.id=w.task "
                      f"WHERE w.purpose=? AND w.ref IN ({','.join('?' * len(refs))}) AND t.state IN ('queued','assigned')", purpose, *refs)

    def pending(self, purpose: str, ref: str) -> int:
        """How many things wanted for this purpose and ref are still coming."""
        return self.q("SELECT count(DISTINCT t.id) AS n FROM wants w JOIN tasks t ON t.id=w.task WHERE w.purpose=? AND w.ref=? "
                      "AND t.state IN ('queued','assigned')", purpose, ref, one=True)["n"]

    def work(self, v: dict, ip: str, client: str = "", per_hour: int | None = None, per_day: int | None = None,
             kinds: list | None = None) -> dict:
        """The next thing for a volunteer to fetch, {"task": ...}, or {"wait": seconds, "why": ...}: what it was
        given and hasn't finished, then the oldest wanted of the kinds its script does or asks for now (videos and
        searches before details and channels), within its limits and its address's: videos' (with videos' details) and,
        apart, searches' (with channels' pages)."""
        kinds = [k for k in (kinds or OLD_KINDS) if k in KEYS and (k != "storyboard" or v.get("trusted"))] or list(OLD_KINDS)
        now = time.time()
        with self.lock:
            self.requeue_stale()
            self.q("UPDATE volunteers SET last_seen=?, active_at=?, last_ip=?, client=?, asked_per_hour=?, asked_per_day=? WHERE id=?",
                   now, now, ip, client[:80], _own(per_hour, 10**6), _own(per_day, 10**6), v["id"])
            why = self.paused()
            if why:     # what it has stays its own, to send when it can
                return {"wait": PAUSE_WAIT, "why": why}
            rules = {**RULES, **self.rules()}
            waiting = registration.provisional(v)
            if waiting and not rules["provisional_work"]:
                return {"wait": PAUSE_WAIT, "why": "waiting for approval by whoever runs this site", "provisional": True}
            if (v.get("refused_until") or 0) > now:
                return {"wait": v["refused_until"] - now, "why": "YouTube was throttling your downloads lately: resting"
                        if v.get("rest_why") == "throttled" else "YouTube refused you lately: resting"}
            mine = self.q("SELECT * FROM tasks WHERE volunteer=? AND state='assigned'", v["id"], one=True)
            if mine:
                return {"task": self._task_view(mine)}
            most_hour, most_day = ((rules["provisional_per_hour"], rules["provisional_per_day"]) if waiting else
                                   (rules["per_hour"], rules["per_day"]))
            caps = {"videos": (min(_limit(per_hour, v["per_hour"] or PER_HOUR), _limit(most_hour, PER_HOUR)),
                               min(_limit(per_day, v["per_day"] or PER_DAY), _limit(most_day, PER_DAY))),
                    "searches": (_limit(most_hour, PER_HOUR), _limit(most_day, PER_DAY)) if waiting else (PAGES_PER_HOUR, PAGES_PER_DAY)}
            held = {}       # what's held back by its limits: {"videos" or "searches": the answer to wait for it}
            for what, (hour, day) in caps.items():
                pages = "IN" if what == "searches" else "NOT IN"
                for span, cap, per in ((3600, hour, "an hour"), (86400, day, "a day")):
                    for field, value, whose in (("volunteer", v["id"], "you"), ("ip", ip, "your address")):
                        rows = self.q(f"SELECT at FROM given WHERE {field}=? AND at>? AND COALESCE(kind,'video') {pages} "
                                      f"{PAGE_SQL} ORDER BY at", value, now - span)
                        if len(rows) >= cap and what not in held:
                            held[what] = {"wait": rows[len(rows) - cap]["at"] + span - now + 1,
                                          "why": f"{cap} {what if cap != 1 else ONE[what]} {per} is the most for {whose}"
                                                 + (" until you're approved" if waiting else "")}
            open_kinds = [k for k in kinds if ("searches" if k in PAGE_KINDS else "videos") not in held]
            task = self.next_task(open_kinds, v, ip, fresh="videos" not in held) if open_kinds else None
            if not task:
                waiting_for = [h for what, h in held.items() if self.q(
                    f"SELECT 1 FROM tasks WHERE state='queued' AND kind IN ({','.join('?' * len(kinds))}) AND kind {'IN' if what == 'searches' else 'NOT IN'} "
                    f"{PAGE_SQL} LIMIT 1", *kinds, one=True)]
                if not waiting_for:
                    return {"wait": IDLE_WAIT, "why": "nothing wanted now"}
                soonest = min(waiting_for, key=lambda h: h["wait"])
                return {**soonest, "wait": min(soonest["wait"], IDLE_WAIT)} if open_kinds else soonest     # others may come meanwhile
            self.q("UPDATE tasks SET state='assigned', volunteer=?, ip=?, assigned=?, touched=?, size=NULL, ext=NULL WHERE id=?",
                   v["id"], ip, now, now, task["id"])
            self.q("INSERT INTO given(volunteer,ip,at,kind) VALUES(?,?,?,?)", v["id"], ip, now, task["kind"])
            self.q("DELETE FROM given WHERE at<?", now - 2 * 86400)
            (self.files / f"{task['id']}.part").unlink(missing_ok=True)
        return {"task": self._task_view(task)}

    def alive(self, v: dict) -> None:
        """A volunteer's script, waiting (for its limits, or a rest), says it's still there."""
        now = time.time()
        self.q("UPDATE volunteers SET last_seen=?, active_at=? WHERE id=?", now, now, v["id"])

    def bye(self, v: dict) -> None:
        """A volunteer's script stopped: it's gone, at once (presence.py); what it had goes back to the queue."""
        self.q("UPDATE volunteers SET bye_at=? WHERE id=?", time.time(), v["id"])
        self.q("UPDATE tasks SET state='queued', volunteer=NULL WHERE volunteer=? AND state='assigned'", v["id"])

    def presence(self) -> list:
        """Tell of volunteers that came or went since (presence.sweep())."""
        def quiet(v: dict) -> float:
            found = re.search(r"script (\d+)", v.get("client") or "")
            return GONE_AFTER if found and int(found[1]) >= 6 else GONE_AFTER_OLD
        return presence.sweep(self.q, "volunteers", quiet, self.came_went)

    def _task_view(self, t: dict) -> dict:
        if t["kind"] == "search":
            return {"id": t["id"], "kind": "search", "query": t["video_id"], "max_results": SEARCH_RESULTS}
        if t["kind"] == "channel":
            return {"id": t["id"], "kind": "channel", "channel_id": t["video_id"]}
        if t["kind"] == "storyboard":
            c = self.q("SELECT spec, mode FROM checks WHERE task=?", t["id"], one=True) or {}
            if c.get("mode") == "fresh":
                return {"id": t["id"], "kind": "storyboard", "video_id": t["video_id"], "fresh": True}
            spec = json.loads(c.get("spec") or "{}")
            return {"id": t["id"], "kind": "storyboard", "video_id": t["video_id"], "urls": [f["url"] for f in spec.get("fragments") or []]}
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
            if t["kind"] == "storyboard":
                if ext != "zip" or not 0 < size <= STORYBOARD_MAX:
                    raise ValueError(f"a storyboard as a zip file of up to {STORYBOARD_MAX // 2**20} MB")
            elif t["kind"] != "video":
                raise ValueError("only videos and storyboards come in parts")
            elif ext not in VIDEO_EXT or not 0 < size <= MAX_VIDEO:
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
            health = self._health(v, status, report, t["kind"])
            if status == "refused":
                level = min(len(BLOCK_HOURS) - 1, (v.get("refusal_level") if v.get("refusal_level") is not None else -1) + 1)
                until = now + BLOCK_HOURS[level] * 3600
                self.q("UPDATE volunteers SET refusal_level=?, refused_until=?, rest_why='refused' WHERE id=?", level, until, v["id"])
                self.q("UPDATE tasks SET state='queued', volunteer=NULL WHERE id=?", task_id)
                f.unlink(missing_ok=True)
                return {"rest_until": until}
            if health == "throttled":   # it rests, without the refusals' growing rests; what it fetched still counts
                until = max(v.get("refused_until") or 0, now + THROTTLE_REST)
                self.q("UPDATE volunteers SET refused_until=?, rest_why='throttled' WHERE id=?", until, v["id"])
            if t["kind"] == "storyboard" and status == "failed" and report.get("denied") and self._given_refused(t, report):
                f.unlink(missing_ok=True)
                return {"ok": True}
            if status != "ok":
                error = str(report.get("error") or "failed")[:500]
                self._failed(t, error, bool(report.get("permanent"))
                             or bool(FOR_GOOD.search(error) and not re.search(r"try again later", error, re.I)))
                f.unlink(missing_ok=True)
                if t["kind"] == "storyboard":
                    self.q("UPDATE checks SET state='failed', error=?, finished=? WHERE task=? AND "
                           "(SELECT state FROM tasks WHERE id=?)='failed'", error, now, task_id, task_id)
                return {"ok": True}
            check = None
            if t["kind"] == "storyboard":
                check = self._compare(v, t, f, report)
                info = {"check": check["id"], "verdict": check["verdict"]}
            elif t["kind"] == "search":
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
                check = self._plan_check(v, t, path, report)
            self.q("UPDATE tasks SET state='done', finished=?, info=?, error=NULL WHERE id=?", now, json.dumps(info), task_id)
            self.q("UPDATE volunteers SET refusal_level=-1, done=done+1 WHERE id=?", v["id"])
            if health != "throttled":   # a throttled one's rest stands, though what it sent came
                self.q("UPDATE volunteers SET refused_until=NULL WHERE id=?", v["id"])
        if t["kind"] == "storyboard":
            try:
                self.checked(check, self.get(check["volunteer"]) or {})
            except Exception as e:  # noqa: BLE001 - told or not, the check is kept
                print(f"volunteers: telling of a check failed: {e!r}", flush=True)
            return {"ok": True}
        if check:
            threading.Thread(target=self._prepare_check, args=(check,), daemon=True).start()
        try:
            self.delivered({**t, "state": "done"}, path, info)
        finally:
            if path:
                path.unlink(missing_ok=True)
            self._settle(task_id)
        return {"ok": True}

    def _health(self, v: dict, status: str, report: dict, kind: str | None = None) -> str | None:
        """Record how YouTube treats a volunteer, from what its script reported with a task (its "health": {"signs":
        [{"kind": one of TROUBLE, or "note" for a warning when it worked, "what": the line yt-dlp said}], "kbps",
        "yt_dlp"}; scripts before version 5 send none): refused if the task was, else the worst sign, else ok if it
        worked; a failure without signs (a private video) says nothing. Calls troubled() when that's worse than before, and widespread() when many are degraded.
        Every report goes in youtube_log too (kind: the task's), kept LOG_DAYS, whatever it said of its health.

        Returns:
            Its health now, or None if this said nothing of it.
        """
        h = report.get("health") if isinstance(report.get("health"), dict) else {}
        given = [x for x in h.get("signs") or [] if isinstance(x, dict)][:10]
        signs = [x for x in given if x.get("kind") in TROUBLE]
        notes = [str(x.get("what") or "") for x in given if x.get("kind") == "note"]   # warnings, though it worked
        worst = min(signs, key=lambda x: TROUBLE.index(x["kind"])) if signs else None
        if status == "refused":     # the line that made it a refusal, else the last
            lines = [l.strip() for l in str(report.get("error") or "").strip().splitlines() if l.strip()]
            why = next((l for l in lines if REFUSAL.search(l)), lines[-1] if lines else "refused")
            health = "refused"
        elif worst:
            health, why = worst["kind"], str(worst.get("what") or worst["kind"])
        elif status == "ok":
            health, why = "ok", f"yt-dlp warned: {notes[0]}" if notes else ""
        else:
            health, why = None, ""
        kbps = h.get("kbps") if isinstance(h.get("kbps"), (int, float)) and not isinstance(h.get("kbps"), bool) else None
        ytdlp = str(h.get("yt_dlp") or "")[:40] or None
        now = time.time()
        self.q("UPDATE volunteers SET kbps=COALESCE(?, kbps), yt_dlp=COALESCE(?, yt_dlp) WHERE id=?", kbps, ytdlp, v["id"])
        failed = [l.strip() for l in str(report.get("error") or "").strip().splitlines() if l.strip()]
        pot = str(h.get("pot"))[:160] if h.get("pot") else None     # used, unused or failed: <why> (script 13 on)
        trial = str(h.get("trial"))[:40] if h.get("trial") else None    # the trial it ran under (script 14 on)
        self.q("INSERT INTO youtube_log(at, volunteer, kind, status, health, detail, yt_dlp, kbps, pot, trial) VALUES(?,?,?,?,?,?,?,?,?,?)",
               now, v["id"], kind, str(status or "")[:20], health, (why or (failed[-1] if failed and status != "ok" else ""))[:300] or None,
               ytdlp or v.get("yt_dlp"), kbps, pot, trial)
        self.q("DELETE FROM youtube_log WHERE at<?", now - LOG_DAYS * 86400)
        if health is None:
            return None
        self.q("UPDATE volunteers SET health=?, health_detail=?, health_at=? WHERE id=?", health, why[:300], now, v["id"])
        was = v.get("health")
        # told when it gets worse, and every time it's refused or throttled: the server tells only unusual runs of those
        if health in TROUBLE and (was not in TROUBLE or TROUBLE.index(health) < TROUBLE.index(was) or health != "degraded"):
            try:
                self.troubled(v, was, health, why[:300])
            except Exception:  # noqa: S110 - telling someone mustn't stop the work
                pass
        if health == "degraded":
            self._check_widespread(now, why)
        return health

    def _check_widespread(self, now: float, example: str) -> None:
        span, least, share = WIDESPREAD
        heard = self.q("SELECT id, health FROM volunteers WHERE health_at>? AND revoked IS NULL", now - span)
        n = sum(r["health"] == "degraded" for r in heard)
        if n >= least and n >= share * len(heard) and now - self.widespread_said > span:
            self.widespread_said = now
            try:
                self.widespread(n, len(heard), example[:300])
            except Exception:  # noqa: S110
                pass

    def trouble(self) -> dict | None:
        """Whether many volunteers have said lately that YouTube degrades them (WIDESPREAD): {"degraded": n, "of": m,
        "example": what one said}, or None."""
        span, least, share = WIDESPREAD
        heard = self.q("SELECT health, health_detail FROM volunteers WHERE health_at>? AND revoked IS NULL ORDER BY health_at DESC",
                       time.time() - span)
        bad = [r for r in heard if r["health"] == "degraded"]
        if len(bad) >= least and len(bad) >= share * len(heard):
            return {"degraded": len(bad), "of": len(heard), "example": bad[0]["health_detail"]}
        return None

    # -- checking volunteers' videos against their storyboards (storyboards.py) ------------------------------------
    def _plan_check(self, v: dict, t: dict, path: Path, report: dict) -> str | None:
        """Whether to check a video just sent, by its storyboard (RULES' check_new or check_trusted): if so, the new
        check's id, with the video linked where _prepare_check() reads it. One with no storyboard is noted as such."""
        rules = {**RULES, **self.rules()}
        if random.random() >= float(rules["check_trusted" if v.get("trusted") else "check_new"]):
            return None
        spec = storyboards.pick(report.get("storyboards"), t["video_id"])
        cid, now = secrets.token_hex(6), time.time()
        self.q("INSERT INTO checks(id,video_id,volunteer,ip,video_task,spec,state,mode,error,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
               cid, t["video_id"], v["id"], t["ip"], t["id"], json.dumps(spec) if spec else None,
               "preparing" if spec else "skipped", "given", None if spec else "no storyboard was sent with it", now)
        if not spec:
            return None
        checks = self.files / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        os.link(path, checks / f"{cid}.video")
        return cid

    def _prepare_check(self, cid: str) -> None:
        """The video's frames at its storyboard's times kept (storyboards.video_frames()), then the storyboard asked of a
        trusted volunteer."""
        video = self.files / "checks" / f"{cid}.video"
        try:
            c = self.q("SELECT * FROM checks WHERE id=?", cid, one=True)
            frames = storyboards.video_frames(video, json.loads(c["spec"]), self.probe(video))
            storyboards.save_frames(frames, self.files / "checks" / f"{cid}.npz")
            task = secrets.token_hex(6)
            with self.lock:
                self.q("INSERT INTO tasks(id,kind,video_id,state,created) VALUES(?,'storyboard',?,'queued',?)", task, c["video_id"], time.time())
                self.q("UPDATE checks SET state='queued', task=? WHERE id=?", task, cid)
        except Exception as e:  # noqa: BLE001 - the video is in already; only its check is lost
            print(f"volunteers: preparing check {cid} failed: {e!r}", flush=True)
            self.q("UPDATE checks SET state='failed', error=?, finished=? WHERE id=?", f"keeping its frames failed: {e}"[:300], time.time(), cid)
        finally:
            video.unlink(missing_ok=True)

    def _given_refused(self, t: dict, report: dict) -> bool:
        """The storyboard's addresses, as its sender got them, didn't work for the volunteer checking: noted, and (if
        RULES' check_fresh) asked again of a trusted volunteer, who asks YouTube's player for them itself. Whether so."""
        c = self.q("SELECT * FROM checks WHERE task=?", t["id"], one=True)
        if not c or c["mode"] != "given":
            return False
        error = str(report.get("error") or "refused")[:300]
        fresh = bool({**RULES, **self.rules()}["check_fresh"])
        self.q("UPDATE checks SET given_error=?, mode=? WHERE id=?", error, "fresh" if fresh else "given", c["id"])
        if fresh:
            self.q("UPDATE tasks SET state='queued', volunteer=NULL, error=? WHERE id=?", error, t["id"])
        return fresh

    def _compare(self, v: dict, t: dict, f: Path, report: dict) -> dict:
        """A storyboard come back, compared with its video's frames: the check, done, with its verdict.

        Raises:
            ValueError: it hasn't all come, or isn't a storyboard.
        """
        c = self.q("SELECT * FROM checks WHERE task=?", t["id"], one=True)
        if not c:
            raise LookupError("that check is gone")
        if not f.exists() or t["size"] is None or f.stat().st_size != t["size"]:
            raise ValueError("the storyboard hasn't all come: send it first")
        spec = json.loads(c["spec"])
        if c["mode"] == "fresh":
            spec = storyboards.pick([report.get("storyboard")], t["video_id"])
            if not spec:
                raise ValueError("a storyboard of YouTube's, for that video, is wanted with it")
        try:
            with zipfile.ZipFile(f) as z:
                names = sorted((n for n in z.namelist() if re.fullmatch(r"\d+\.img", n)), key=lambda n: int(n.split(".")[0]))
                if sum(z.getinfo(n).file_size for n in names) > STORYBOARD_MAX:
                    raise ValueError("too big")
                images = [z.read(n) for n in names]
            frames = storyboards.load_frames(self.files / "checks" / f"{c['id']}.npz")
            board = storyboards.tiles(images, spec, frames["stretched"].shape[:0:-1])
        except (zipfile.BadZipFile, ValueError) as e:
            raise ValueError(f"that isn't the storyboard: {e}") from None
        result = storyboards.compare(frames, board, spec["interval"])
        f.rename(self.files / "checks" / f"{c['id']}.zip")       # kept a while, to look at (CHECK_KEEP_DAYS)
        self.q("UPDATE checks SET state='done', checker=?, verdict=?, result=?, finished=? WHERE id=?",
               v["id"], result["verdict"], json.dumps(result), time.time(), c["id"])
        return self.q("SELECT * FROM checks WHERE id=?", c["id"], one=True)

    def recompare(self) -> int:
        """Compare again the checks whose video frames and storyboard are still kept (CHECK_KEEP_DAYS), with today's
        storyboards.compare() and tiles(): a verdict that changes is kept, as after the fix for a storyboard's last
        image YouTube cuts short (every short video's check had failed: 14 of ace-3's 29 were mismatches and 6 unclear, all matches after, Nov 24).
        Returns how many changed."""
        n = 0
        for c in self.q("SELECT * FROM checks WHERE state='done' AND mode='given'"):
            try:
                frames = storyboards.load_frames(self.files / "checks" / f"{c['id']}.npz")
                with zipfile.ZipFile(self.files / "checks" / f"{c['id']}.zip") as z:
                    names = sorted((m for m in z.namelist() if re.fullmatch(r"\d+\.img", m)), key=lambda m: int(m.split(".")[0]))
                    images = [z.read(m) for m in names]
                spec = json.loads(c["spec"])
                result = storyboards.compare(frames, storyboards.tiles(images, spec, frames["stretched"].shape[:0:-1]), spec["interval"])
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                continue
            if result["verdict"] != c["verdict"]:
                self.q("UPDATE checks SET verdict=?, result=? WHERE id=?", result["verdict"], json.dumps(result), c["id"])
                print(f"volunteers: check {c['id']} of {c['video_id']} compared again: {c['verdict']} -> {result['verdict']}", flush=True)
                n += 1
        return n

    def _expire_checks(self) -> None:
        """Checks no trusted volunteer did in CHECK_DAYS given up, and what checks compared deleted after CHECK_KEEP_DAYS:
        at most once an hour."""
        now = time.time()
        if now - getattr(self, "_expired_at", 0) < 3600:
            return
        self._expired_at = now
        for c in self.q("SELECT id, task FROM checks WHERE state IN ('queued','preparing') AND created<?", now - CHECK_DAYS * 86400):
            self.q("UPDATE checks SET state='expired', finished=? WHERE id=?", now, c["id"])
            self.q("UPDATE tasks SET state='cancelled', finished=? WHERE id=? AND state='queued'", now, c["task"])
        for f in (self.files / "checks").glob("*"):
            try:
                if f.suffix in (".npz", ".zip") and f.stat().st_mtime < now - CHECK_KEEP_DAYS * 86400:
                    f.unlink()
            except OSError:
                pass

    def check_summary(self, latest: int = 12) -> dict:
        """For the admin page: each volunteer's checks' verdicts ({id: {match, mismatch, unclear, ...}}), and the
        latest checks, with who sent and who checked, and whether the sender's storyboard addresses worked."""
        per: dict[str, dict] = {}
        for r in self.q("SELECT volunteer, COALESCE(verdict, state) AS v, count(*) AS n FROM checks GROUP BY volunteer, COALESCE(verdict, state)"):
            per.setdefault(r["volunteer"], {})[r["v"]] = r["n"]
        names = {v["id"]: v["name"] for v in self.q("SELECT id, name FROM volunteers")}
        rows = self.q("SELECT * FROM checks WHERE state!='skipped' ORDER BY created DESC LIMIT ?", latest)
        given = self.q("SELECT count(*) AS n, sum(given_error IS NULL AND mode='given') AS worked FROM checks "
                       "WHERE state='done' OR given_error IS NOT NULL", one=True)
        return {"per": per, "given": given,
                "latest": [{**{k: r[k] for k in ("id", "video_id", "state", "mode", "given_error", "verdict", "error", "created", "finished")},
                            "sender": names.get(r["volunteer"]), "checker": names.get(r["checker"]),
                            "result": json.loads(r["result"]) if r["result"] else None} for r in rows]}

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
                self._ready(w["purpose"], w["ref"])

    def _ready(self, purpose: str, ref: str) -> bool:
        """Call ready(); if it says not now (False: what it's for is busy, as a harvest still running), what came is kept
        as not applied, for resettle() to give again."""
        if self.ready(purpose, ref) is not False:
            return True
        self.q("UPDATE wants SET applied=0 WHERE purpose=? AND ref=?", purpose, ref)
        return False

    def resettle(self, purpose: str, ref: str) -> bool:
        """Give again what came for a purpose and ref when ready() said not now, once nothing more is coming for it.
        Returns whether ready() took it."""
        with self.lock:
            if self.pending(purpose, ref) or not self.q(
                    "SELECT 1 FROM wants w JOIN tasks t ON t.id=w.task WHERE w.purpose=? AND w.ref=? AND w.applied=0 "
                    "AND t.state='done' LIMIT 1", purpose, ref):
                return False
            self.q("UPDATE wants SET applied=1 WHERE purpose=? AND ref=?", purpose, ref)
        return self._ready(purpose, ref)

    def unsettled(self, purpose: str, since: float) -> list[str]:
        """The refs for a purpose that something came for since then (a task finished) that ready() hasn't taken, as it
        said not now: for resettle()."""
        return [r["ref"] for r in self.q("SELECT DISTINCT w.ref FROM wants w JOIN tasks t ON t.id=w.task WHERE w.purpose=? "
                                         "AND w.applied=0 AND t.state='done' AND t.finished>=?", purpose, since)]

    # -- for the admin page ---------------------------------------------------------------------------------------
    def contributions(self) -> list[dict]:
        """What each volunteer has fetched, for the leaderboard: {id, name, created, revoked, registered (by its own
        script), videos, gb (of video), searches, details (videos' details and channels' descriptions), last}, those that
        fetched anything, most videos first."""
        out: dict[str, dict] = {}
        for r in self.q("SELECT t.volunteer, t.kind, COUNT(*) AS n, COALESCE(SUM(t.size), 0) AS bytes, MAX(t.finished) AS last "
                        "FROM tasks t WHERE t.state='done' AND t.volunteer IS NOT NULL GROUP BY t.volunteer, t.kind"):
            c = out.setdefault(r["volunteer"], {"videos": 0, "gb": 0.0, "searches": 0, "details": 0, "last": 0})
            if r["kind"] == "video":
                c["videos"], c["gb"] = r["n"], round(r["bytes"] / 2**30, 2)
            elif r["kind"] == "search":
                c["searches"] = r["n"]
            elif r["kind"] in ("details", "channel"):
                c["details"] += r["n"]
            c["last"] = max(c["last"], r["last"] or 0)
        rows = {v["id"]: v for v in self.q("SELECT id, name, created, revoked, registered_ip FROM volunteers")}
        board = [{"id": vid, "name": rows[vid]["name"], "created": rows[vid]["created"], "revoked": rows[vid]["revoked"],
                  "registered": rows[vid]["registered_ip"] is not None, **c} for vid, c in out.items() if vid in rows]
        return sorted(board, key=lambda b: (-b["videos"], -b["searches"], -b["details"], b["created"] or 0))

    def youtube_log(self, days: float = 7, latest: int = 20) -> dict:
        """What volunteers' scripts said of YouTube over the last days, summed up: by yt-dlp version, volunteer,
        whether its script made PO tokens and the trial it ran under, and by day, the tasks that worked, were throttled, refused or degraded, or
        failed otherwise (a private video), how many used a PO token and how many failed to make one, with the median
        speed of whole videos; and the latest troubles themselves. A nightly yt-dlp's version has four parts
        (2026.11.14.232655), a release's three."""
        rows = self.q("SELECT l.*, v.name FROM youtube_log l LEFT JOIN volunteers v ON v.id=l.volunteer WHERE l.at>? ORDER BY l.at",
                      time.time() - days * 86400)

        def sums(group: list[dict]) -> dict:
            out = {k: 0 for k in ("tasks", "ok", *TROUBLE, "failed", "pot_used", "pot_failed")}
            for r in group:
                out["tasks"] += 1
                out[r["health"] if r["health"] in TROUBLE else "ok" if r["status"] == "ok" else "failed"] += 1
                out["pot_used"] += r["pot"] == "used"
                out["pot_failed"] += (r["pot"] or "").startswith("failed")
            speeds = sorted(r["kbps"] for r in group if r["kbps"])
            out["kbps_median"] = round(speeds[len(speeds) // 2]) if speeds else None
            return out
        by_version, by_day = {}, {}
        for r in rows:
            by_version.setdefault((r["yt_dlp"] or "?", r["name"] or r["volunteer"], r["pot"] is not None, r["trial"] or ""), []).append(r)
            by_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(r["at"])), []).append(r)
        return {"days": days,
                "by_version": [{"yt_dlp": k[0], "nightly": k[0].count(".") >= 3, "volunteer": k[1], "pot": k[2], "trial": k[3] or None,
                                **sums(g)}
                               for k, g in sorted(by_version.items(), key=lambda kv: (kv[0][0], kv[0][1]), reverse=True)],
                "by_day": [{"day": d, **sums(g)} for d, g in sorted(by_day.items(), reverse=True)],
                "latest": [{"at": r["at"], "volunteer": r["name"], "kind": r["kind"], "health": r["health"], "detail": r["detail"],
                            "yt_dlp": r["yt_dlp"], "pot": r["pot"], "trial": r["trial"]} for r in rows
                           if r["health"] in TROUBLE or (r["pot"] or "").startswith("failed")][-latest:][::-1]}

    def state(self) -> dict:
        now = time.time()
        vols = []
        rules = {**RULES, **self.rules()}
        for v in self.q("SELECT * FROM volunteers ORDER BY created"):
            given = self.q("SELECT at, kind FROM given WHERE volunteer=? AND at>?", v["id"], now - 86400)
            pages = [g for g in given if g["kind"] in PAGE_KINDS]
            given = [g for g in given if g["kind"] not in PAGE_KINDS]
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
                         "per_hour": min(v["per_hour"] or PER_HOUR, hour, v.get("asked_per_hour") or PER_HOUR),
                         "per_day": min(v["per_day"] or PER_DAY, day, v.get("asked_per_day") or PER_DAY), "done": v["done"],
                         "own_per_hour": v["per_hour"], "own_per_day": v["per_day"], **script_limits(v),
                         "fetching": fetching, "provisional": waiting, "registered": v.get("registered_ip") is not None,
                         "created": v["created"], "last_hour": sum(g["at"] > now - 3600 for g in given), "last_day": len(given),
                         "pages_last_hour": sum(g["at"] > now - 3600 for g in pages), "pages_last_day": len(pages),
                         "health": v.get("health"), "health_detail": v.get("health_detail"), "health_at": v.get("health_at"),
                         "kbps": v.get("kbps"), "yt_dlp": v.get("yt_dlp"), "rest_why": v.get("rest_why"),
                         "trial": v.get("trial"), "trusted": bool(v.get("trusted"))})
        counts = {f"{r['state']}_{r['kind']}": r["n"] for r in self.q("SELECT state, kind, count(*) AS n FROM tasks GROUP BY state, kind")}
        wanted = {r["purpose"]: r["n"] for r in self.q("SELECT w.purpose, count(DISTINCT w.task) AS n FROM wants w JOIN tasks t "
                                                         "ON t.id=w.task WHERE t.state IN ('queued','assigned') GROUP BY w.purpose")}
        recent = self.q("SELECT kind, video_id, state, error, finished FROM tasks WHERE state IN ('done','failed') "
                        "ORDER BY finished DESC LIMIT 8")
        return {"volunteers": vols, "counts": counts, "wanted": wanted, "recent": recent,
                "connected": sum(v["status"] in ("fetching", "waiting for work") for v in vols), "trouble": self.trouble(),
                "youtube": self.youtube_log(), "trials": {k: t["what"] for k, t in TRIALS.items()}, "checks": self.check_summary()}


class OffsetError(Exception):
    def __init__(self, have: int):
        self.have = have
        super().__init__(f"expected the part at byte {have}")


def script_limits(v: dict) -> dict:
    """The limits a volunteer's script last asked for, and whether its volunteer chose them (--per-hour, --per-day) or
    they're its version's own: scripts before 10 kept to 100 a day, whatever the site allowed."""
    found = re.search(r"script (\d+)", v.get("client") or "")
    hour, day = SCRIPT_DEFAULTS[0], (SCRIPT_DEFAULTS[1] if found and int(found[1]) >= 10 else 100)
    asked = (v.get("asked_per_hour"), v.get("asked_per_day"))
    return {"script_per_hour": asked[0], "script_per_day": asked[1],
            "script_chose": bool(found) and any(a is not None and a < most for a, most in zip(asked, (hour, day)))}


def _own(given, most: int) -> int | None:
    """A limit someone chose, within most; None if none was."""
    return None if given is None or given == "" else _limit(given, most)


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


def script_fetch(handler: BaseHTTPRequestHandler) -> dict:
    """Who fetched a script, for telling of it: {ip, ua, headers}: the sender (_client_ip()), its User-Agent, and
    Cloudflare's headers about where it is (CF-IPCountry and the like)."""
    return {"ip": _client_ip(handler), "ua": handler.headers.get("User-Agent") or "",
            "headers": {k.lower(): v for k, v in handler.headers.items() if k.lower().startswith("cf-ip") or k.lower().startswith("cf-region")}}


# What a computer without an NVIDIA GPU does for the site (the worker script's CPU_TASKS, by remote_job.py's --task):
# the volunteer page's Compute helper says each, so keep it in step with them (tests/test_volunteers.py checks).
HELPER_JOBS = {"filter": "sorts each climb's photos from the web into those that show its boulder and those that don't",
               "beta": "finds the holds climbers use in each boulder's videos, from where their hands and feet go"}


def helper_jobs_text() -> str:
    """The Compute helper button's second line: what it does, from HELPER_JOBS."""
    jobs = list(HELPER_JOBS.values())
    text = jobs[0] if len(jobs) == 1 else "; ".join(jobs[:-1]) + "; and " + jobs[-1]
    return text[0].upper() + text[1:] + ", in Docker, on Linux, a Mac or Windows."


COPY_ICON = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
             'stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/>'
             '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>')
PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Volunteer for boulderme.sh</title>
<link rel="icon" href="/static/favicon-volunteer.svg" type="image/svg+xml">
<script>/* light or dark as chosen on the main page: ?theme= from its Volunteer button, or what it keeps, if it's this address */
try { const t = new URLSearchParams(location.search).get('theme') || localStorage.getItem('bm_theme');
      if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t; } catch (e) {}</script>
<style>
:root{color-scheme:light dark;--code:#f2f2f2;--line:#ccc;--ok:#2a8a3a;--acc:#1a73c8;--acc-fg:#fff;--mute:#666}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--code:#1e1e1e;--line:#444;--ok:#5fd68a;--acc:#5cc8ff;--acc-fg:#001018;--mute:#999}}
:root[data-theme=light]{color-scheme:light}
:root[data-theme=dark]{color-scheme:dark;--code:#1e1e1e;--line:#444;--ok:#5fd68a;--acc:#5cc8ff;--acc-fg:#001018;--mute:#999}
body{font:16px/1.5 system-ui,sans-serif;max-width:42em;margin:2em auto;padding:0 16px}
h2{font-size:1.25em;margin:1.2em 0 .3em} h3{font-size:1.05em;margin:1.3em 0 .3em}
code{background:var(--code);padding:1px 4px;border-radius:3px}
.choose{display:grid;gap:12px;margin:1.2em 0}
.choose a{display:block;padding:18px 20px;border-radius:10px;border:2px solid var(--acc);color:inherit;text-decoration:none;font-size:1.2em;
  font-weight:600;line-height:1.3}
.choose a small{display:block;font-weight:400;font-size:.8em;color:var(--mute);margin-top:4px}
.choose a.on{background:var(--acc);color:var(--acc-fg)} .choose a.on small{color:inherit;opacity:.85}
@media (max-width:560px){.choose .req{display:block}}   /* on a phone, a button's requirements on a line of their own */
h1 a.home{color:inherit;text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:5px;text-decoration-color:var(--acc)}
h1 a.home:hover{color:var(--acc)}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:.8em 0 .4em;border-bottom:1px solid var(--line);padding-bottom:6px}
.tabs button{font:inherit;padding:5px 12px;border-radius:6px;border:1px solid var(--line);background:transparent;color:inherit;cursor:pointer}
.tabs button.on{background:var(--acc);border-color:var(--acc);color:var(--acc-fg)}
.os{display:none} .os.on{display:block}
section{display:none} section.on{display:block}
.box{position:relative;margin:6px 0 10px}
.box pre{background:var(--code);border:1px solid var(--line);border-radius:6px;padding:10px 46px 10px 12px;margin:0;
  white-space:pre-wrap;overflow-wrap:anywhere}
.copy{position:absolute;top:6px;right:6px;background:transparent;color:inherit;border:1px solid var(--line);border-radius:5px;padding:5px;
  line-height:0;cursor:pointer}
.copy.done{border-color:var(--ok);color:var(--ok)}
ul{padding-left:1.2em} li{margin:.2em 0}
.lead{float:right;margin-top:.35em;font-size:.85em;padding:4px 10px;border:1px solid var(--acc);border-radius:6px;color:var(--acc);
  text-decoration:none}
</style>
<a class="lead" href="/volunteer/leaderboard" title="Who has fetched the most videos, and whose computers have made the most meshes">Leaderboard</a>
<h1>Volunteer for <a class="home" href="/" title="BoulderMesh's meshes">boulderme.sh</a></h1>
<p>BoulderMesh makes 3D models of boulders from photos and climbing videos. You can help from your own computer in three
ways now: each script makes a token for you, asks your name (shown on the leaderboard; you can leave it out), and registers itself with this
site, and whoever runs it then approves it. A fourth way, collecting data yourself, is coming soon.</p>
<div class="choose">
  <a href="#videos" data-show="videos">Collect videos<small>Fetch YouTube videos of climbs from your address, slowly. Python is all it needs.</small></a>
  <a href="#compute" data-show="compute">Compute meshes <span class="req">(requires Linux + GPU)</span><small>Turn climbs' photos into 3D models on your computer's NVIDIA GPU, in Docker.</small></a>
  <a href="#helper" data-show="helper">Compute helper <span class="req">(all platforms; CPU only)</span><small>{helper_jobs}</small></a>
  <a href="#data" data-show="data">Data collection<small>Test the phone apps: photograph boulders so their meshes come out whole, level and the right size.</small></a>
</div>

<section id="videos">
<h2>Help collect videos</h2>
<p>YouTube stops one server from fetching many videos, so volunteers fetch some from their own computers, slowly: at most
15 an hour, spread over it, and 200 a day, one at a time. You need Python 3.8 or newer (with one older than 3.11, it fetches a newer one
for yt-dlp by itself).</p>
<h3>1. Get the script</h3>
<div class="box"><pre>curl -O {base}/volunteer/bouldermesh_volunteer.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p><a href="/volunteer/bouldermesh_volunteer.py" download="bouldermesh_volunteer.py">Or download it here</a>. On Windows,
<code>curl.exe</code> in place of <code>curl</code>.</p>
<h3>2. Run it</h3>
<div class="box"><pre>python3 bouldermesh_volunteer.py --server {base}</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p>On Windows, <code>py</code> in place of <code>python3</code>. The first time, it makes your token and asks your name
(optional). It remembers the server and the token (in <code>.bouldermesh-volunteer</code> in your home folder, readable
only by you), so after that it's only <code>python3 bouldermesh_volunteer.py</code>. Until whoever runs this site
approves you, it fetches only a few videos a day, or none. When this site has a newer version of the script, it
replaces itself with that between one video and the next (<code>--no-auto-update</code> stops that).</p>
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
8 GB of its memory or more to mesh (each climb only once enough of it is free); with 4 to 8 GB, it finds beta and
sizes on the GPU, and with less, helps on the CPU, as a <a href="#helper">compute helper</a> does.</li>
<li><b>Linux on an x86-64 computer</b>, with Docker and NVIDIA's Container Toolkit, Docker usable without sudo (in the
<code>docker</code> group), and Python 3.9 or newer. On Windows, use WSL 2 with Docker's GPU support, and run everything
below inside it. A Mac won't do: it has no NVIDIA GPU, and Docker on a Mac can't use the one it has.</li>
<li><b>16 GB of memory</b> or more (32 is better), and as many CPU cores as you'll give it: some steps use only the CPU.</li>
<li><b>Disk:</b> about 18 GB where Docker keeps its images (it checks before downloading the image), and 12 GB where the
script keeps its jobs and the models they download, which <code>--data</code> can put on another disk.</li>
<li><b>Internet:</b> a climb's photos are 50 to 300 MB down, and its mesh 20 to 60 MB up.</li>
</ul>
<p><b>No NVIDIA GPU, or not Linux?</b> See <a href="#helper">Compute helper</a>: the same script helps without one.</p>
<h3>1. Get the script</h3>
<div class="box"><pre>curl -O {base}/worker/bouldermesh_worker.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p><a href="/worker/bouldermesh_worker.py" download="bouldermesh_worker.py">Or download it here</a>.</p>
<h3>2. Check it can work</h3>
<div class="box"><pre>python3 bouldermesh_worker.py --server {base} --check</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p>The first time, it makes this machine's token, asks your name (optional) and registers with this site, which then
waits for whoever runs it to approve the machine before giving it climbs. It says whether it's approved yet, and whether
there's room for jobs and the image; short of room, it says what's short. Add <code>--cpus N</code> to limit the cores jobs use, and <code>--data DIR</code> to keep jobs and models
on another disk. It remembers them all, with the server and the token (in <code>.bouldermesh-worker</code> in your home
folder, readable only by you). When this site has a newer version of the script, it replaces itself with that between
one climb and the next, never during one (<code>--no-auto-update</code> stops that). It takes no climb while your own work
keeps the GPU busier than half (<code>--busy-gpu-util</code>) or leaves less than 24 GB of memory free
(<code>--need-ram-gb</code>), and finishes any it has started.</p>
<p>Last, it asks whether to start by itself from now on, whenever the computer starts (as a systemd service of your own,
running a copy of the script in <code>.bouldermesh-worker</code>; it may ask for your password, to run while you're
logged out). Say yes, and it's running: you're done. Then <code>journalctl --user -u bouldermesh-worker -f</code> shows
what it's doing, and <code>systemctl --user disable --now bouldermesh-worker</code> stops it for good. To pause it for a while,
<code>systemctl --user stop bouldermesh-worker</code> stops it for now (any climb it's on goes to another computer), and
it starts again with <code>systemctl --user start bouldermesh-worker</code>, or by itself when the computer next starts.</p>
<h3>3. Or run it yourself</h3>
<p>If you said no:</p>
<div class="box"><pre>python3 bouldermesh_worker.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
</section>

<section id="helper">
<h2>Help as a compute helper</h2>
<p>The same script as for meshes, on a computer without an NVIDIA GPU, does the work for meshes that
needs only a processor: it {helper_list}. It runs in this site's small Docker image for it (about 1.9 GB, for Intel and AMD
processors and for Apple Silicon), one job at a time, each a few minutes. Only your computer connects out, over HTTPS;
nothing needs to reach it. Ctrl-C stops it, and the job goes to another computer. It takes no jobs until whoever runs
this site approves it, as what it keeps is what gets meshed.</p>
<p>Wherever it runs, it needs about <b>4 GB of memory</b> free for Docker, <b>5 GB of disk</b> where Docker keeps its
images and 2 GB for jobs, and a climb's photos (50 to 300 MB) down for each job.</p>
<div class="tabs" role="tablist">
  <button data-os="linux" role="tab">Linux</button>
  <button data-os="mac" role="tab">Mac</button>
  <button data-os="windows" role="tab">Windows</button>
</div>

<div class="os" data-os="linux">
<h3>What it needs</h3>
<ul>
<li>Docker, usable without sudo (in the <code>docker</code> group), and Python 3.9 or newer. On Debian or Ubuntu, step 2
offers to install what's missing.</li>
</ul>
<h3>1. Get the script</h3>
<div class="box"><pre>curl -O {base}/worker/bouldermesh_worker.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<h3>2. On Debian or Ubuntu, install what's missing</h3>
<div class="box"><pre>python3 bouldermesh_worker.py --setup</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p>It shows each step's commands and runs them only if you say so. Joining the <code>docker</code> group takes effect
when you log in again.</p>
<h3>3. Check it can work</h3>
<div class="box"><pre>python3 bouldermesh_worker.py --server {base} --check</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p>The first time, it makes this computer's token, asks your name (optional) and registers with this site. It says
whether it's approved yet and whether there's room. With an NVIDIA GPU it would mesh too: add <code>--tasks filter --gpu
none</code> to only help this way. <code>--cpus N</code> limits the cores jobs use. It remembers them all.</p>
<p>Last, it asks whether to start by itself from now on, whenever the computer starts. Say yes, and it's running: you're
done. Then <code>journalctl --user -u bouldermesh-worker -f</code> shows what it's doing, and
<code>systemctl --user disable --now bouldermesh-worker</code> stops it for good. To pause it for a while,
<code>systemctl --user stop bouldermesh-worker</code> stops it for now (any job it's on goes to another computer), and
it starts again with <code>systemctl --user start bouldermesh-worker</code>, or by itself when the computer next starts.</p>
<h3>4. Or run it yourself</h3>
<p>If you said no:</p>
<div class="box"><pre>python3 bouldermesh_worker.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
</div>

<div class="os" data-os="mac">
<h3>What it needs</h3>
<ul>
<li>A Mac with Apple Silicon (M1 or newer) or an Intel processor.</li>
<li><b>Docker:</b> <a href="https://docs.docker.com/desktop/setup/install/mac-install/">Docker Desktop</a> (free for
personal use), or OrbStack or Colima. In Docker Desktop's Settings, under Resources, give it at least 4 GB of memory, and
under General, start it when you sign in, if you'd like the script to run then too. A Mac's own GPU can't be used from
Docker, so a Mac helps this way only.</li>
<li><b>Python 3.9 or newer:</b> <code>python3 --version</code> in Terminal says; if it offers to install the command line
developer tools, say yes, or install Python from <a href="https://www.python.org/downloads/macos/">python.org</a>.</li>
</ul>
<h3>1. Get the script</h3>
<p>In Terminal (Applications, Utilities):</p>
<div class="box"><pre>curl -O {base}/worker/bouldermesh_worker.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<h3>2. Check it can work</h3>
<p>With Docker running:</p>
<div class="box"><pre>python3 bouldermesh_worker.py --server {base} --check</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p>The first time, it makes this computer's token, asks your name (optional) and registers with this site. It says
whether it's approved yet. <code>--cpus N</code> limits the cores jobs use. It remembers them all.</p>
<h3>3. Run it</h3>
<div class="box"><pre>python3 bouldermesh_worker.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<h3>Optional: start it by itself, whenever you sign in</h3>
<p>As a launch agent of your own, with the script left in your home folder:</p>
<div class="box"><pre>mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/sh.boulderme.worker.plist &lt;&lt;EOF
&lt;?xml version="1.0" encoding="UTF-8"?&gt;
&lt;!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"&gt;
&lt;plist version="1.0"&gt;&lt;dict&gt;
  &lt;key&gt;Label&lt;/key&gt;&lt;string&gt;sh.boulderme.worker&lt;/string&gt;
  &lt;key&gt;ProgramArguments&lt;/key&gt;&lt;array&gt;
    &lt;string&gt;$(command -v python3)&lt;/string&gt;&lt;string&gt;$HOME/bouldermesh_worker.py&lt;/string&gt;
  &lt;/array&gt;
  &lt;key&gt;EnvironmentVariables&lt;/key&gt;&lt;dict&gt;
    &lt;key&gt;PATH&lt;/key&gt;&lt;string&gt;/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$HOME/.orbstack/bin&lt;/string&gt;
  &lt;/dict&gt;
  &lt;key&gt;RunAtLoad&lt;/key&gt;&lt;true/&gt;
  &lt;key&gt;KeepAlive&lt;/key&gt;&lt;true/&gt;
  &lt;key&gt;ThrottleInterval&lt;/key&gt;&lt;integer&gt;60&lt;/integer&gt;
  &lt;key&gt;StandardOutPath&lt;/key&gt;&lt;string&gt;$HOME/Library/Logs/bouldermesh-worker.log&lt;/string&gt;
  &lt;key&gt;StandardErrorPath&lt;/key&gt;&lt;string&gt;$HOME/Library/Logs/bouldermesh-worker.log&lt;/string&gt;
&lt;/dict&gt;&lt;/plist&gt;
EOF
launchctl load -w ~/Library/LaunchAgents/sh.boulderme.worker.plist</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p><code>tail -f ~/Library/Logs/bouldermesh-worker.log</code> shows what it's doing, and
<code>launchctl unload -w ~/Library/LaunchAgents/sh.boulderme.worker.plist</code> stops it for good. It waits while
Docker isn't running, and carries on once it is.</p>
</div>

<div class="os" data-os="windows">
<h3>What it needs</h3>
<ul>
<li>Windows 10 (version 2004 or newer) or 11, with <b>WSL 2</b>: Linux in Windows, where the script runs. In PowerShell
as administrator, <code>wsl --install</code> installs it with Ubuntu, and asks you to restart; then open Ubuntu from the
Start menu once, to make its user.</li>
<li><b>Docker:</b> <a href="https://docs.docker.com/desktop/setup/install/windows-install/">Docker Desktop</a> (free for
personal use), using WSL 2, with Settings, Resources, WSL integration turned on for Ubuntu. Or, without Docker Desktop,
step 2 below installs Docker inside Ubuntu itself.</li>
</ul>
<p>Everything below is typed in the Ubuntu window, not in PowerShell.</p>
<h3>1. Get the script</h3>
<div class="box"><pre>cd ~ &amp;&amp; curl -O {base}/worker/bouldermesh_worker.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<h3>2. Without Docker Desktop: install Docker in Ubuntu</h3>
<div class="box"><pre>python3 bouldermesh_worker.py --setup</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p>It asks before each step. Afterwards, close the Ubuntu window and open it again, for the <code>docker</code> group to
take effect.</p>
<h3>3. Check it can work</h3>
<div class="box"><pre>python3 bouldermesh_worker.py --server {base} --check</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p>The first time, it makes this computer's token, asks your name (optional) and registers with this site. It says
whether it's approved yet. <code>--cpus N</code> limits the cores jobs use. It remembers them all.</p>
<h3>4. Run it</h3>
<div class="box"><pre>python3 bouldermesh_worker.py</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p>It runs while the Ubuntu window stays open.</p>
<h3>Optional: start it by itself, whenever you sign in</h3>
<p>In PowerShell (not as administrator), once:</p>
<div class="box"><pre>$a = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d Ubuntu -- bash -lc 'python3 ~/bouldermesh_worker.py >> ~/bouldermesh-worker.log 2>&amp;1'"
Register-ScheduledTask -TaskName "BoulderMesh compute helper" -Action $a -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)</pre><button class="copy" title="Copy" aria-label="Copy">{icon}</button></div>
<p><code>tail -f ~/bouldermesh-worker.log</code> in Ubuntu shows what it's doing, and
<code>Unregister-ScheduledTask -TaskName "BoulderMesh compute helper"</code> in PowerShell stops it starting.</p>
</div>
</section>

<section id="data">
<h2>Data collection: testing the phone apps</h2>
<p>The Android and iPhone apps photograph a boulder so that its mesh comes out whole, level, the right size and in the
right place: they show what's still to photograph over the camera's picture, take each photo when it's worth taking,
and record which way was down, where the phone was and which way it faced. They're new and have hardly been used on a
phone yet, so what goes wrong is as useful to hear as what works.</p>
<p>Testing needs an account on this site (from an invite), to upload what you capture: it's credited to your name.</p>

<h3>1. Install the app</h3>
<p><b>Android</b> (8.0 or newer, on a phone that supports Google Play Services for AR, as most recent ones do):</p>
<ol>
<li>On the phone, download <a href="{android_apk}"><b>the latest Android app</b></a> (an <code>.apk</code> file, about
11 MB).</li>
<li>Open it from the download. The first time, Android asks to allow installing apps from your browser: allow it, go
back, and press <i>Install</i>. (Play Protect may warn that it's from an unknown developer: <i>Install anyway</i>.)</li>
<li>Open the app, with signal, and accept its terms. If Google Play Services for AR, which capturing needs, isn't on
the phone, the Play Store then offers it at once: install it (it needs signal; it's free, from Google). If the app says
this phone can't run it, the phone can't capture, though the rest of the app works.</li>
</ol>
<p>To update, download and install the latest the same way, over the old one: your captures stay. Only if Android says
the app conflicts with one installed (a copy built elsewhere) do you need to uninstall first, which deletes captures not
yet uploaded: upload them first.</p>
<p><b>iPhone</b> (iOS 16 or newer): install Apple's <a href="https://apps.apple.com/app/testflight/id899247664">TestFlight</a>
app, then open the app's TestFlight link from this site (press your name at the top of the main page, once you've joined
from your invite), <i>Accept</i>, and <i>Install</i>. New versions come through TestFlight.</p>

<h3>2. Sign it in</h3>
<ul>
<li><b>Signed in to this site in the phone's browser:</b> press your name at the top of the main page, then
<i>Sign the app in</i>: the app opens, signed in as you.</li>
<li><b>Otherwise:</b> on the site, press your name, then <i>Sign in another device</i>, for a code; type it in the app
(or, on Android, scan its QR).</li>
</ul>
<p>The app says the same the first time it opens, until it's signed in or you press <i>Not now</i>; <i>Settings</i>
has it too.</p>

<h3>3. Before you go</h3>
<ul>
<li><b>While you have signal,</b> check Google Play Services for AR is installed (Android: the app asks for it when it
opens if not), and open <i>Get meshes…</i> and get the boulders you'll visit (nearest first; <i>Get the N
within 2 km</i> takes them all). They're kept on the phone, with their holds, for where there's no signal.</li>
<li><b>Before each capture,</b> <i>Calibrate</i> the compass: half a minute of turning the phone every which way, away
from metal (car, crash pad zips, rings).</li>
</ul>

<h3>4. At the rock</h3>
<ol>
<li>Press <i>Capture</i> on its mesh. First, <b>turn slowly all the way round on the spot</b>, the phone held upright as
for a photo: that's how it finds which way is down.</li>
<li>A small model of the mesh appears. Tap a spot on it you can find on the rock, then the same spot on the real rock;
then once more, well to one side of the first. If the mesh drawn over the rock sits on it, press <i>It fits: start</i>;
if not, <i>Add a spot</i> or <i>Start again</i>.</li>
<li><b>Follow the words at the top.</b> It first walks you to a few rings in the air, where earlier photos were taken,
for one photo from each. Then red parts of the mesh still need photos, amber ones are part done, green ones done; rings
mark holds, with a stop for a photo at each halving of the distance on the way in.</li>
<li>Hold still where it's useful and it takes the photo itself, and says if one came out blurred or badly lit.
<i>Take</i> takes one anyway; <i>Skip this</i> leaves what you can't reach (too high, the back).</li>
<li><b>Finish</b> when it's all green, or as much as you can get.</li>
</ol>
<p>A rock with no mesh yet: <i>Capture a new rock…</i>, tap its middle, and walk round as much of it as you can, with
<i>Can't reach that side</i> for the rest and <i>Mark a hold</i> for what needs close-ups.</p>

<h3>5. Upload</h3>
<p>Once you have signal, <i>Upload</i>. The server meshes your photos together with the mesh's own, and the new mesh
appears beside the old one, linked to the same climbs.</p>

<h3>Telling us how it went</h3>
<p>Anything odd, confusing or broken: the <b>?</b> tab on the main page (say which phone), or on an iPhone, a screenshot
sent from TestFlight. What the app did comes to the server with each capture, so saying when and where helps us find it.</p>
</section>

<script>
for (const b of document.querySelectorAll('.copy')) b.onclick = async () => {
  const pre = b.previousElementSibling;
  try { await navigator.clipboard.writeText(pre.textContent); }
  catch { const r = document.createRange(); r.selectNodeContents(pre); getSelection().removeAllRanges(); getSelection().addRange(r); document.execCommand('copy'); }
  b.classList.add('done'); b.title = 'Copied';
  setTimeout(() => { b.classList.remove('done'); b.title = 'Copy'; }, 1500);
};
// the ways to help: one shows at a time, from the buttons or the address's #videos, #compute or #helper
function show(which){
  for (const s of document.querySelectorAll('section')) s.classList.toggle('on', s.id === which);
  for (const a of document.querySelectorAll('.choose a')) a.classList.toggle('on', a.dataset.show === which);
}
addEventListener('hashchange', () => show(location.hash.slice(1)));
show(location.hash.slice(1));
// the compute helper's instructions for one system at a time: this computer's, to start with
function showOs(os){
  for (const b of document.querySelectorAll('.tabs button')) b.classList.toggle('on', b.dataset.os === os);
  for (const d of document.querySelectorAll('.os')) d.classList.toggle('on', d.dataset.os === os);
}
for (const b of document.querySelectorAll('.tabs button')) b.onclick = () => showOs(b.dataset.os);
showOs(/Mac/.test(navigator.platform || navigator.userAgent) ? 'mac' : /Win/.test(navigator.platform || navigator.userAgent) ? 'windows' : 'linux');
</script>
"""


LEADERBOARD_PAGE = PAGE.split("<h1>")[0].replace("<title>Volunteer for boulderme.sh</title>", "<title>boulderme.sh leaderboard</title>").replace(
    '<a class="lead" href="/volunteer/leaderboard" title="Who has fetched the most videos, and whose computers have made the most meshes">Leaderboard</a>',
    '<a class="lead" href="/volunteer/">Volunteer</a>') + """<style>
table{border-collapse:collapse;width:100%;font-size:.92em;margin:.4em 0 1.4em} th,td{text-align:right;padding:4px 6px;border-bottom:1px solid var(--line)}
th:nth-child(2),td:nth-child(2){text-align:left} th{font-weight:600;color:var(--mute);font-size:.85em} .note{color:var(--mute);font-size:.85em}
td.gone{color:var(--mute)} .wrap{overflow-x:auto}
</style>
<h1><a class="home" href="/">boulderme.sh</a> leaderboard</h1>
<p class="note">Thank you, all. Names are as each chose them when their script registered (or anonymous); see <a href="/volunteer/">Volunteer</a> to join.</p>
<h2>Videos</h2>
<p class="note">Videos fetched from YouTube for the server, which can't fetch them itself, and the searches that found them.</p>
<div class="wrap"><table id="vols"><thead><tr><th>#</th><th>Volunteer</th><th>Videos</th><th>GB</th><th>Searches</th><th title="Videos' details and channels' descriptions">Details</th></tr></thead><tbody></tbody></table></div>
<h2>Compute</h2>
<p class="note">Computers lent to make meshes, find climbers' beta and boulders' sizes.</p>
<div class="wrap"><table id="workers"><thead><tr><th>#</th><th>Computer</th><th>Meshes</th><th title="Where climbers' hands and feet go, found in videos">Beta</th>
<th title="How big a boulder is">Sizes</th><th title="Processor time, summed over its cores, in meshing">CPU core-hours</th><th title="The GPU's use by its jobs, measured as they ran (from worker script 10), in hours of an RTX 3090's: each GPU's hours weighed by its single-precision speed. ≈: partly estimated, from the time of the steps that use a GPU, or a GPU not known">GPU hours <small>(3090)</small></th></tr></thead><tbody></tbody></table></div>
<p class="note" id="updated"></p>
<script>
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const num = (n, d = 0) => n ? Number(n).toFixed(d) : '–';
fetch('/volunteer/api/leaderboard').then(r => r.json()).then(b => {
  const row = (cells, gone) => `<tr>${cells.map((c, i) => `<td${i === 1 && gone ? ' class="gone" title="No longer helping"' : ''}>${c}</td>`).join('')}</tr>`;
  document.querySelector('#vols tbody').innerHTML = b.volunteers.map((v, i) => row([i + 1, esc(v.name), v.videos, num(v.gb, 1), v.searches, v.details], v.revoked)).join('')
    || '<tr><td colspan="6" class="note">No one yet.</td></tr>';
  document.querySelector('#workers tbody').innerHTML = b.workers.map((w, i) => row([i + 1, esc(w.name) + (w.gpu ? ` <small class="note">${esc(w.gpu.replace(/^NVIDIA (GeForce )?/, ''))}</small>` : ''),
    w.meshes, w.beta, w.sizes, num(w.cpu_hours, 1), (w.gpu_rough && w.gpu_hours ? '≈' : '') + num(w.gpu_hours, 1)], w.revoked)).join('')
    || '<tr><td colspan="7" class="note">No one yet.</td></tr>';
  document.querySelector('#updated').textContent = b.since ? `Meshes counted since ${new Date(b.since * 1000).toLocaleDateString()}; beta and sizes since ${new Date(b.jobs_since * 1000).toLocaleDateString()}.` : '';
}).catch(e => { document.querySelector('#updated').textContent = "Couldn't load it: " + e.message; });
</script>
"""
# the Android app's latest build, under a name that stays (.github/workflows/apps.yml): a direct download, no sign-in
ANDROID_APK = "https://github.com/tom-f-oconnell/bouldermesh/releases/download/android-latest/bouldermesh.apk"
LEADERBOARD_CACHE = 60      # seconds the leaderboard is kept before it's worked out again


def handler_for(broker: Broker, log: Callable[[str], None]):
    cached: dict = {"at": 0.0, "board": None}

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
                page = PAGE.replace("{helper_jobs}", html.escape(helper_jobs_text()))
                page = page.replace("{helper_list}", html.escape("; ".join(HELPER_JOBS.values())))
                page = page.replace("{android_apk}", ANDROID_APK)
                return self.send(200, page.replace("{base}", base).replace("{icon}", COPY_ICON).encode(), "text/html; charset=utf-8")
            if u.path in ("/volunteer/leaderboard", "/volunteer/leaderboard/"):
                return self.send(200, LEADERBOARD_PAGE.encode(), "text/html; charset=utf-8")
            if u.path == "/volunteer/api/leaderboard":     # anyone's to see, worked out at most once a LEADERBOARD_CACHE
                now = time.time()
                if now - cached["at"] > LEADERBOARD_CACHE:
                    try:
                        cached.update(at=now, board=broker.leaderboard())
                    except Exception as e:  # noqa: BLE001 - said, rather than the connection dropped
                        print(f"leaderboard: {type(e).__name__}: {e}", flush=True)
                        return self.json({"error": "the leaderboard couldn't be worked out just now"}, 500)
                return self.json(cached["board"])
            if u.path == "/volunteer/bouldermesh_volunteer.py":
                try:    # someone fetching the script (or a copy updating itself, which the server tells apart)
                    broker.downloaded(script_fetch(self))
                except Exception as e:  # noqa: BLE001 - telling of it mustn't stop the download
                    log(f"volunteers: telling of a download failed: {type(e).__name__}: {e}")
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
                    return self.json(registration.too_old(SCRIPT, "/volunteer/"), 426)
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
            if u.path in ("/volunteer/api/alive", "/volunteer/api/bye"):
                self.body(2**16)
            if u.path == "/volunteer/api/alive":
                broker.alive(v)
                return self.json({"ok": True})
            if u.path == "/volunteer/api/bye":
                broker.bye(v)
                log(f"volunteers: {v['name']} said goodbye")
                return self.json({"ok": True})
            try:
                if u.path == "/volunteer/api/work":
                    body = json.loads(self.body(2**20) or b"{}")
                    if int(body.get("version") or 0) < CLIENT_VERSION:
                        return self.json(registration.too_old(SCRIPT, "/volunteer/"), 426)
                    news = registration.script_news(SCRIPT, body)
                    if "update" in news:    # asked between fetches: it updates itself and asks again
                        log(f"volunteers: {v['name']} told to update its script to version {news['update']['version']}")
                        return self.json(news)
                    return self.json({**broker.work(v, _client_ip(self), str(body.get("client") or ""),
                                                    body.get("per_hour"), body.get("per_day"), body.get("kinds")), **news,
                                      "yt_dlp": {**RULES, **broker.rules()}.get("yt_dlp") or "stable",   # which yt-dlp scripts run
                                      "trial": broker.trial_for(v)})
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
