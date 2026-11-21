#!/usr/bin/env python3
"""Volunteer this computer to fetch YouTube videos for a BoulderMesh server, slowly, one at a time.

BoulderMesh makes 3D models of boulders from photos and climbing videos. YouTube stops one server from fetching many
videos, so volunteers fetch some from their own addresses. This script asks the server for a video (only ever by its
YouTube id: it makes the youtube.com address itself), fetches it with yt-dlp, sends it to the server, deletes its copy,
and waits before the next. It also searches YouTube for a climb's videos (the server sends only the words), and reads a
channel's description (by its id), sending back what they say. Run it with the server's address:

    python3 bouldermesh_volunteer.py --server https://boulders.example.com

The first time, it makes a token of its own, asks your name (optional, for whoever runs the server), and registers the
token with the server, where it waits for approval: until then it fetches only a few videos a day, or none. A token
whoever runs the server gave you works too (--token), at once.

Python 3.8 or newer, nothing else: it installs yt-dlp, and Deno (the JavaScript runtime yt-dlp needs for YouTube), into
a folder of its own (~/.bouldermesh-volunteer), and updates them daily, and when it updates itself. yt-dlp needs Python
3.11 or newer: with an older one (as Ubuntu 22.04's and before), it installs uv there too, which fetches a newer Python
for them. Stop it with Ctrl-C at any time; delete that folder to remove everything.

When the server has a newer version of this script, it says so, and the script replaces itself with that (checked
against what the server said of it) and starts again, between one fetch and the next; --no-auto-update stops that.

It fetches as a visitor who isn't signed in. --cookies FILE (or --cookies-from-browser) makes yt-dlp sign in to YouTube
with an account's cookies instead, which can get that account banned: read what --help says of them first.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

# The script's version, which the server compares with the one it serves to tell a running copy to update itself. Keep
# it alone on its line, with nothing after the number: scripts before 8 find it in the newer one only as that.
# 3: updates itself when the server says to; 4: then updates yt-dlp and Deno at once;
# 5: tells the server of signs YouTube is degrading or throttling it, and rests when throttled;
# 6: says it's still there while it waits (every 10 min), and goodbye when it stops;
# 7: updates itself when downloaded into another git repository's folder, says why if it doesn't, and takes "This video
#    is unavailable" as for good;
# 8: its version alone on its line, as updating needs (with a comment after it, the server never offered an update)
# 9: says the name it asks for is shown on the site's public leaderboard
# 10: videos spread over each hour (about 4 min apart at 15 an hour), 200 a day; searches and channels' pages counted
#     apart from videos, 30 an hour and 200 a day, and fetched while a video's turn is still to come
# 11: installs the yt-dlp the server says (its nightly builds, or stable), switching at once when that changes; a
#     refusal's report starts with the line that made it one
# 12: --cookies FILE and --cookies-from-browser, after a warning, and saying so to the server
# 13: PO tokens from bgutil-ytdlp-pot-provider (its plugin and its script, run with Deno), and tells the server whether
#     each video used one; yt-dlp's and Deno's caches in its own folder
# 14: trials: the server may choose a few of yt-dlp's settings (TRIAL_KEYS), to compare one way with another, and it
#     says which trial each task ran under
# 15: no PO tokens: bgutil's plugin is uninstalled and its folder deleted, as tokens didn't stop YouTube's 403s, and
#     with the mweb client (which uses one for every video) it was throttled three times as often (Nov 18, 2026)
# 16: sends a video's storyboard (YouTube's small seek-bar frames) with it, and fetches others' storyboards from
#     i.ytimg.com when the server asks, to check that videos volunteers sent are the ones asked for
VERSION = 16
SCRIPT_PATH = "/volunteer/bouldermesh_volunteer.py"     # where the server serves it
AGENT = "bouldermesh-volunteer"
POT_PLUGIN = "bgutil-ytdlp-pot-provider"    # PO tokens' yt-dlp plugin, which scripts 13 and 14 installed: removed
# what a trial the server chooses may set (each checked here), and how: YouTube's clients for videos and their details
TRIAL_KEYS = {"player_client": re.compile(r"-?[a-z_]{2,20}(,-?[a-z_]{2,20}){0,5}")}
TRIAL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,40}")
TOOLS_PYTHON = (3, 11)      # the oldest Python today's yt-dlp runs on (and Deno's package wants 3.10)
FETCHED_PYTHON = "3.12"     # what uv fetches for them when this computer has none as new
KINDS = ["video", "details", "search", "channel", "storyboard"]   # what this version does
# Videos and their details go through YouTube's player, where its bot check is (it refused hal after hours at 20-30 an
# hour, fetched in bursts): spread over each hour, an hour's share apart (3600 / per hour seconds) times a random number
# in SPREAD. Searches and channels' pages never met it there, so they have their own allowance, PAGES_*.
CLASS = {"video": "video", "details": "video", "search": "page", "channel": "page", "storyboard": "page"}
GAP = (8.0, 15.0)           # seconds between one request to YouTube and the next, of any kind, at random in this range
SPREAD = (0.9, 1.25)
PER_HOUR, PER_DAY = 15, 200  # videos (and videos' details): the most, whatever the server allows
PAGES_PER_HOUR, PAGES_PER_DAY = 30, 200     # searches and channels' pages
BLOCK_HOURS = (1, 2, 4, 8)  # rest after YouTube refuses, longer each time it still does
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
TURNED_AWAY = 3             # the exit status when the server won't take this script: starting again won't help
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
WORDS = re.compile(r"[^\x00-\x1f\x7f]{3,200}")
RESULT_KEYS = ("id", "title", "description", "duration", "channel", "channel_id", "uploader")
REFUSAL = re.compile(r"not a bot|HTTP Error 429|Too Many Requests|content isn.t available, try again later", re.I)
# what yt-dlp says when YouTube holds back (a fix for which is usually a newer yt-dlp), or throttles this address
DEGRADED = re.compile(r"PO Token|po_token|SABR|Signature extraction failed|nsig extraction failed|n challenge|challenge solving|"
                      r"Unable to extract|formats (have been skipped|may be missing)|Only images are available|"
                      r"Requested format is not available|JavaScript runtime", re.I)
THROTTLED = re.compile(r"HTTP Error 403|unable to download video data|giving up after \d+ retries", re.I)
SLOW_KBPS = 128             # a whole video arriving slower than this (and than a third of this computer's usual) is throttled
THROTTLE_HOURS = 1          # rest after YouTube throttles
FOR_GOOD = re.compile(r"Video unavailable|video is unavailable|video is no longer available|Private video|video is private|"
                      r"has been removed|members[- ]only|not available in your country|"
                      r"account associated with this video has been terminated|copyright|confirm your age|age[- ]restricted|"
                      r"is not a valid URL|larger than max-filesize", re.I)
# Storyboards: YouTube's small frames of a video, from its image servers (not its video servers, and without the bot
# check). One is sent with each video, and others' are fetched when the server asks, from i.ytimg.com only.
STORYBOARD_HOSTS = ("i.ytimg.com", "i9.ytimg.com")
STORYBOARD_MOST, STORYBOARD_WIDTH = 12, 64      # images in a storyboard at most; the narrowest tiles worth having
STORYBOARD_BYTES = 8 * 2**20
BROWSER = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
KEEP = ("title", "uploader", "channel", "channel_id", "channel_url", "uploader_url", "duration", "upload_date",
        "description", "license")
# what --help and the prompt say of signing in to YouTube with an account's cookies (yt-dlp's wiki, "Extractors",
# YouTube, as of Nov 2026)
COOKIES_WARNING = """\
Signing in to YouTube with an account's cookies can get that account banned. yt-dlp's own documentation says of
using an account: "you run the risk of it being banned (temporarily or permanently)". Use an account made for this,
never your main Google account.

It isn't needed for YouTube's rate limits: those are about 300 videos an hour for a visitor who isn't signed in
(about 2,000 signed in), and this script fetches at most 15. What stops volunteers is YouTube's bot check and its
refusing downloads (HTTP 403), which signing in may get past, at that account's risk.

YouTube changes an account's cookies whenever a YouTube tab is open, so cookies read from a browser you use
(--cookies-from-browser) soon stop working. yt-dlp's documentation suggests exporting them once from a private
window instead: sign in there, open only youtube.com/robots.txt, export its cookies to a file (with a browser
extension that writes the cookies.txt format), close the window without signing out, and give this script that
file with --cookies FILE.

The cookies stay on this computer: yt-dlp uses them, and nothing of them goes to the BoulderMesh server, which is
told only that this computer signs in."""

AGREEMENT = """\
Before you start: what this does, and what it may cost you.

- It asks {server} for YouTube videos of climbs, fetches them from this computer, and sends them there:
  one at a time, spread over each hour (about {spread:.0f} minutes apart), at most {per_hour} an hour and {per_day} a day.
  Each is deleted here once it's sent. Only videos' YouTube ids come from the server, and, to check videos others
  sent, addresses of YouTube's small seek-bar images of them, fetched only from i.ytimg.com. It also searches YouTube
  for climbs' videos, and reads channels' descriptions, from the server's words and ids: at most {pages_hour} an
  hour and {pages_day} a day, and nothing from YouTube comes less than {gap_lo:.0f}-{gap_hi:.0f} seconds after the last.
- YouTube's terms of service don't allow downloading videos except through YouTube's own features. Using this
  breaks them, and the videos are their uploaders' copyright.
- If YouTube decides your address is fetching too much, it may ask everyone on your network to sign in or prove
  they're not a bot for a while. When that happens, this stops for 1, then 2, 4 or 8 hours.
- YouTube and {server} see your IP address. The server keeps the address you last connected from, to share the
  limits above between everyone at that address, and doesn't keep it with the videos. It also keeps the name you
  give it next, if any, to show whoever runs it.

- When {server} has a newer version of this script, this replaces itself with it, as you'd download it from there
  yourself; --no-auto-update stops that.

Stop it with Ctrl-C at any time. Everything it installs is in {home}."""


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def refusal_report(err: str) -> str:
    """What a refusal's report says: the line that made it one (REFUSAL), then the end of what yt-dlp said."""
    line = next((l.strip() for l in err.splitlines() if REFUSAL.search(l)), "")
    return (line[:300] + "\n" + err[-300:]).strip() if line and line not in err[-300:] else err[-300:]


def why(e: Exception) -> str:
    return f"{Path(e.cmd[0]).name} exited with {e.returncode}" if isinstance(e, subprocess.CalledProcessError) else str(e)


def last_error(err: str) -> str:
    """The line that says why yt-dlp failed: its last ERROR, else its last line (now that its warnings are kept)."""
    lines = [x for x in (err or "").strip().splitlines() if x.strip()]
    return next((x for x in reversed(lines) if x.startswith("ERROR")), lines[-1] if lines else "")


def signs(err: str, failed: bool) -> list[dict]:
    """What yt-dlp printed that says YouTube is degrading (DEGRADED) or throttling (THROTTLED) this computer: {"kind",
    "what": the line}, each line once, a few at most. Only when it failed: yt-dlp warns of such things often while still
    getting what it was asked for, and then they're only a "note" (a slow download is caught by its speed)."""
    out, seen = [], set()
    for line in (err or "").splitlines():
        kind = "throttled" if THROTTLED.search(line) else "degraded" if DEGRADED.search(line) else None
        kind = kind if failed or not kind else "note"
        what = re.sub(r"^(WARNING|ERROR):\s*", "", line.strip())[:200]
        if kind and (kind, what) not in seen and len(out) < 6:
            seen.add((kind, what))
            out.append({"kind": kind, "what": what})
    return out


def storyboard_url(url, vid: str) -> bool:
    """Whether an address is one of YouTube's storyboard images of this video."""
    u = urllib.parse.urlparse(str(url))
    return u.scheme == "https" and u.hostname in STORYBOARD_HOSTS and u.path.startswith(f"/sb/{vid}/") and len(str(url)) < 2000


def storyboards(info: dict, vid: str) -> list[dict]:
    """yt-dlp's storyboard formats of a video, cut to what the server reads: no other address of yt-dlp's."""
    out = []
    for f in info.get("formats") or []:
        if not isinstance(f, dict) or not str(f.get("format_id") or "").startswith("sb"):
            continue
        frags = [{"url": x.get("url"), "duration": x.get("duration")} for x in f.get("fragments") or [] if isinstance(x, dict)]
        if frags and all(storyboard_url(x["url"], vid) for x in frags):
            out.append({k: f.get(k) for k in ("format_id", "width", "height", "rows", "columns", "fps")} | {"fragments": frags})
    return out


def best_storyboard(found: list[dict]) -> dict | None:
    """The one to fetch, as the server picks (storyboards.pick()): the fewest images, with tiles wide enough, the largest
    tiles among those."""
    ok = [f for f in found if 1 <= len(f["fragments"]) <= STORYBOARD_MOST]
    wide = [f for f in ok if (f.get("width") or 0) >= STORYBOARD_WIDTH] or ok
    return min(wide, key=lambda f: (len(f["fragments"]), -(f.get("width") or 0))) if wide else None


def python_version(exe) -> tuple[int, int]:
    """A Python's major and minor version, or (0, 0) if it won't run."""
    try:
        r = subprocess.run([str(exe), "-c", "import sys; print(*sys.version_info[:2])"], capture_output=True, text=True, timeout=60)
        return tuple(map(int, r.stdout.split())) if r.returncode == 0 else (0, 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return (0, 0)


def register(server: str, name: str | None, patient: bool = False) -> tuple[str, str]:
    """Make a token, register it with the server under name (asked for, if not given and there's someone to ask), and
    return it and the name given. Exits saying why if the server won't have it; or, if patient, waits half an hour and
    tries again while the server is out of reach or has too many registrations (429, 503)."""
    if name is None and sys.stdin.isatty():
        name = input("Your name or a nickname, shown to whoever runs the server and on its public leaderboard "
                     "(optional; Enter to leave it out): ")
    name = (name or "").strip()
    token = "bmv_" + secrets.token_urlsafe(32)
    while True:
        try:
            r = Server(server, token).call("/volunteer/api/register", {"name": name, "version": VERSION,
                                                                        "client": f"script {VERSION}"}, tries=1)
            break
        except urllib.error.HTTPError as e:
            if not (patient and e.code in (429, 503)):
                raise SystemExit(f"the server wouldn't register this: {e.answer.get('error') or e}") from None
            log(f"the server won't register a new token now ({e.answer.get('error') or e}): trying again in 30 min")
        except (urllib.error.URLError, OSError) as e:
            if not patient:
                raise SystemExit(f"can't reach {server}: {e}") from None
            log(f"can't reach {server} to register a new token ({e}): trying again in 30 min")
        time.sleep(1800)
    log(f"registered with {server} as {r.get('name')!r}: waiting for approval by whoever runs it. "
        + (f"Until then, it fetches at most {r.get('per_hour')} an hour and {r.get('per_day')} a day."
           if r.get("may_work") else "Until then, it waits."))
    return token, name


def load_conf(home) -> dict:
    try:
        return json.loads((Path(home).expanduser() / "config.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_conf(home, conf: dict) -> None:
    path = Path(home).expanduser() / "config.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(conf))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def forget_token(home) -> bool:
    """Delete the remembered token; whether there was one."""
    conf = load_conf(home)
    had = conf.pop("token", None) is not None
    save_conf(home, conf)
    return had


# -- updating itself ------------------------------------------------------------------------------------------------
auto_update = True          # --no-auto-update turns it off


def tracked_by_git(me: Path) -> bool:
    """Whether this file is the one a git checkout of BoulderMesh has (a developer's, which git pull updates): one git
    tracks there. A copy downloaded into another repository's folder isn't; without git to ask, the repository's layout
    says (volunteer/ in a checkout)."""
    if not any((d / ".git").exists() for d in me.parents):
        return False
    try:
        return subprocess.run(["git", "-C", str(me.parent), "ls-files", "--error-unmatch", me.name],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return me.parent.name == "volunteer" and (me.parent.parent / ".git").exists()


def update_blocked(server: str) -> str | None:
    """Why this script can't update itself from the server, or None if it can: only when allowed, over HTTPS (or to
    this machine), where it may replace its own file, and not in a git checkout (as a developer's), which git updates."""
    from urllib.parse import urlparse
    u = urlparse(server)
    me = Path(__file__).resolve()
    if not auto_update:
        return "--no-auto-update"
    if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
        return "only over HTTPS"
    if tracked_by_git(me):
        return "it's in a git checkout, which git pull updates"
    if not (os.access(me, os.W_OK) and os.access(me.parent, os.W_OK)):
        return f"it can't replace {me}"
    return None


def update_itself(server: str, info: dict | None, keep: Path) -> bool:
    """Replace this script with the newer one the server says it serves (info: {version, sha256}), checked against
    those, and run that in its place with the same arguments: at a moment when nothing's in progress, as only asking
    for work brings it. Returns False, having said why, if it didn't (so the caller carries on as it is); otherwise it
    doesn't return. The one before is kept in keep/previous.py."""
    import hashlib
    try:
        version, sha = int((info or {}).get("version") or 0), str((info or {}).get("sha256") or "")
        blocked = update_blocked(server)
        if blocked or version <= VERSION:
            log(f"the server has a newer script (version {version}), but this one can't update itself ({blocked or 'not newer'}): "
                f"download it again with curl -O {server.rstrip('/')}{SCRIPT_PATH}")
            return False
        req = urllib.request.Request(server.rstrip("/") + SCRIPT_PATH, headers={"User-Agent": f"{AGENT}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read(2**21 + 1)
        found = re.search(rb"^VERSION = (\d+)\b", data, re.M)
        if len(data) > 2**21 or hashlib.sha256(data).hexdigest() != sha or not found or int(found[1]) != version:
            log(f"the newer script from {server} wasn't what the server said it would be: carrying on with this one")
            return False
        compile(data, SCRIPT_PATH, "exec")
        me = Path(__file__).resolve()
        keep.mkdir(parents=True, exist_ok=True)
        shutil.copy2(me, keep / "previous.py")
        tmp = me.with_name(me.name + ".new")
        tmp.write_bytes(data)
        os.chmod(tmp, os.stat(me).st_mode & 0o7777)
        os.replace(tmp, me)
    except Exception as e:      # whatever went wrong, the script as it is still runs
        log(f"couldn't update itself ({type(e).__name__}: {e}): carrying on with this one")
        return False
    log(f"updated itself from version {VERSION} to {version}, as {server} said to (the one before is in {keep / 'previous.py'}); "
        "starting it")
    argv = [sys.executable, str(me)] + [a for a in sys.argv[1:] if a != "--register"]     # registered: its token is remembered
    sys.stdout.flush()
    if os.name == "nt":     # Windows has no exec: run it, and end with it
        raise SystemExit(subprocess.call(argv))
    os.execv(sys.executable, argv)
    return True


def turned_away(server: str, e: urllib.error.HTTPError, keep: Path | None = None) -> SystemExit:
    """Says the server won't take this script, as too old (426), and what to do, rather than a traceback; returns the
    exit to raise. With keep (its folder), it first updates itself, if the server said how and it can (update_itself()),
    and then doesn't return. (A token it won't take is forgotten, and another registered: Volunteer.renew().)"""
    if keep and (getattr(e, "answer", None) or {}).get("update"):
        update_itself(server, e.answer["update"], keep)
    log(f"{server} says this script is too old. Download it again: curl -O {server}{SCRIPT_PATH}")
    return SystemExit(TURNED_AWAY)


class Server:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def call(self, path: str, body: dict | bytes | None = None, method: str | None = None, tries: int = 6) -> dict:
        """JSON from the server, trying again after a dropped connection or its being away; an HTTP error's JSON is
        raised as HTTPError with .answer set."""
        data, kind = (body, "application/octet-stream") if isinstance(body, bytes) else \
            (json.dumps(body).encode() if body is not None else None, "application/json")
        for attempt in range(tries):
            req = urllib.request.Request(self.url + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": kind,
                                                  "User-Agent": f"bouldermesh-volunteer/{VERSION}"})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                try:
                    e.answer = json.loads(e.read() or b"{}")
                except ValueError:
                    e.answer = {}
                if e.code in (502, 503, 504) and attempt < tries - 1:     # the server, or the way to it, is away
                    time.sleep(30 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, OSError) as e:
                if attempt == tries - 1:
                    raise
                log(f"can't reach {self.url} ({e}); trying again")
                time.sleep(30 * (attempt + 1))
        raise RuntimeError("unreachable")


class Volunteer:
    def __init__(self, args):
        self.args, self.home = args, Path(args.home).expanduser()
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        self.state_file = self.home / "state.json"
        try:
            self.state = json.loads(self.state_file.read_text())
        except (FileNotFoundError, ValueError):
            self.state = {}
        self.state.setdefault("fetched", [])     # when videos (and videos' details) were fetched, over the last day
        self.state.setdefault("paged", [])       # when searches and channels' pages were
        self.server = Server(args.server, args.token)
        self.per_hour, self.per_day = max(1, min(args.per_hour, PER_HOUR)), max(1, min(args.per_day, PER_DAY))
        self.ytdlp = args.yt_dlp

    def save(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.state_file)

    # -- yt-dlp ---------------------------------------------------------------------------------------------------
    def tools(self) -> None:
        """yt-dlp and Deno in a venv here, made the first time (again, if its Python is too old for them) and updated
        daily, and at once when this script has been replaced by a newer one (as it updates itself), unless --yt-dlp names
        one. Exits saying why if they can't be installed; a failed update carries on with those there are."""
        if self.args.yt_dlp:
            return
        venv = self.home / "venv"
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        self.ytdlp = str(bindir / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp"))
        fresh = not Path(self.ytdlp).exists() or python_version(python) < TOOLS_PYTHON
        newer = self.state.get("updated_by") != VERSION     # updated before by another version of this script (or one before 4)
        channel = self.state.get("channel") or "stable"     # which yt-dlp the server last said: "nightly" or "stable"
        switched = self.state.get("installed_channel", "stable") != channel
        if not fresh and not newer and not switched and time.time() - self.state.get("updated", 0) < 86400:
            return
        try:
            if fresh:
                if venv.exists():
                    log(f"making {venv} again, with a Python new enough for today's yt-dlp")
                    shutil.rmtree(venv)
                log(f"installing yt-dlp and Deno in {venv} (a minute or two)")
                self.make_venv(venv)
            else:
                log("updating yt-dlp and Deno" + (f", as this script is now version {VERSION}" if newer else "")
                    + (f", to yt-dlp's {channel} builds, as the server says" if switched else ""))
            pip = [str(python), "-m", "pip", "install", "-q", "--upgrade"]
            subprocess.run([*pip, "pip"], check=True)
            # nightly: yt-dlp's pre-releases on PyPI; back to stable from them, a reinstall, as a nightly is newer
            ytdlp = [*(["--pre"] if channel == "nightly" else []), *(["--force-reinstall"] if switched and channel == "stable" else [])]
            subprocess.run([*pip, *ytdlp, "yt-dlp[default]"], check=True)
            subprocess.run([*pip, "deno"], check=True)
            self.state["installed_channel"] = channel
            self.remove_pot(python)
        except (subprocess.CalledProcessError, OSError) as e:
            if fresh:
                shutil.rmtree(venv, ignore_errors=True)     # tried again from the start next time
                raise SystemExit(f"couldn't install yt-dlp and Deno ({why(e)}): what's printed above says why. Or give "
                                 "--yt-dlp with one you have, version 2025.11 or newer, with Deno on the PATH.") from None
            log(f"couldn't update yt-dlp and Deno ({why(e)}): carrying on with those there are, and trying again tomorrow")
        self.state["updated"], self.state["updated_by"] = time.time(), VERSION
        self.save()

    def remove_pot(self, python: Path) -> None:
        """What scripts 13 and 14 installed for PO tokens, removed: their yt-dlp plugin (which yt-dlp would otherwise
        still load) and its token script's folder (about 250 MB)."""
        pot = self.home / "pot"
        if pot.exists() or "pot" in self.state:
            log("removing the PO token plugin and its script, which this script no longer uses")
            subprocess.run([str(python), "-m", "pip", "uninstall", "-q", "-y", POT_PLUGIN], capture_output=True)
            shutil.rmtree(pot, ignore_errors=True)
            shutil.rmtree(self.home / "cache" / POT_PLUGIN, ignore_errors=True)
            self.state.pop("pot", None)
            self.state.pop("pot_why", None)

    def tool_env(self, bindir: Path | None = None) -> dict:
        """yt-dlp's and Deno's environment: Deno beside yt-dlp on the PATH, and their caches (yt-dlp's and Deno's) in
        this folder, so that deleting it removes everything."""
        env = dict(os.environ)
        env["PATH"] = str(bindir or Path(self.ytdlp).parent) + os.pathsep + env.get("PATH", "")
        env.update(XDG_CACHE_HOME=str(self.home / "cache"), DENO_DIR=str(self.home / "cache" / "deno"), DENO_NO_UPDATE_CHECK="1",
                   DENO_NO_PROMPT="1")
        return env

    def make_venv(self, venv: Path) -> None:
        """A venv for yt-dlp and Deno, from this Python, or another on the PATH new enough for them, or else one uv
        fetches: uv itself installed from PyPI in a venv of this Python's, and its Python kept here too."""
        for exe in [sys.executable, *filter(None, (shutil.which(f"python3.{m}") for m in range(20, TOOLS_PYTHON[1] - 1, -1)))]:
            if python_version(exe) >= TOOLS_PYTHON:
                subprocess.run([exe, "-m", "venv", str(venv)], check=True)
                return
        here = ".".join(map(str, sys.version_info[:2]))
        uv = shutil.which("uv")
        if not uv:
            log(f"this computer's Python ({here}) is older than yt-dlp needs ({'.'.join(map(str, TOOLS_PYTHON))}): "
                f"installing uv in {self.home / 'uv'}, to fetch Python {FETCHED_PYTHON} for it")
            boot = self.home / "uv"
            shutil.rmtree(boot, ignore_errors=True)
            try:
                subprocess.run([sys.executable, "-m", "venv", str(boot)], check=True)
            except subprocess.CalledProcessError:
                raise SystemExit("couldn't make a venv with this Python: on Debian or Ubuntu, `sudo apt install python3-venv` "
                                 "first") from None
            bpy = boot / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run([str(bpy), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
            subprocess.run([str(bpy), "-m", "pip", "install", "-q", "uv"], check=True)
            uv = str(boot / ("Scripts/uv.exe" if os.name == "nt" else "bin/uv"))
        else:
            log(f"this computer's Python ({here}) is older than yt-dlp needs: {uv} fetches Python {FETCHED_PYTHON} for it")
        env = {**os.environ, "UV_PYTHON_INSTALL_DIR": str(self.home / "python"), "UV_CACHE_DIR": str(self.home / "uv-cache")}
        subprocess.run([uv, "venv", "-q", "--seed", "--python", FETCHED_PYTHON, str(venv)], check=True, env=env)

    def take_trial(self, trial) -> None:
        """The trial the server put this volunteer in (with its ask for work), kept if it's one this script can run:
        a name, and only TRIAL_KEYS, each as it allows. Anything else: none, and said so once."""
        ok = (isinstance(trial, dict) and TRIAL_NAME.fullmatch(str(trial.get("name") or "")) and
              all(k in TRIAL_KEYS and isinstance(x, str) and TRIAL_KEYS[k].fullmatch(x) for k, x in trial.items() if k != "name"))
        if trial and not ok and trial != getattr(self, "refused_trial", None):
            log(f"the server's trial isn't one this script can run, so it runs as usual: {str(trial)[:200]}")
            self.refused_trial = trial
        trial = trial if ok else None
        if trial != getattr(self, "trial", None):
            log(f"in the server's {trial['name']} trial: {', '.join(f'{k} {x}' for k, x in trial.items() if k != 'name') or 'as usual'}"
                if trial else "in no trial: as usual")
        self.trial = trial

    def trial_args(self) -> list[str]:
        """yt-dlp's options for the trial it's in, for videos and their details."""
        t = getattr(self, "trial", None) or {}
        return ["--extractor-args", f"youtube:player_client={t['player_client']}"] if t.get("player_client") else []

    def run_ytdlp(self, args: list[str], cwd: Path, trial: bool = False) -> subprocess.CompletedProcess:
        env = dict(os.environ, PATH=str(Path(self.ytdlp).parent) + os.pathsep + os.environ.get("PATH", "")) \
            if self.args.yt_dlp else self.tool_env()      # Deno, beside it
        return subprocess.run([self.ytdlp, "--sleep-requests", "0.75", *cookie_args(self.args),
                               *(self.trial_args() if trial else []), *args], cwd=cwd,
                              env=env, capture_output=True, text=True, timeout=3600)     # warnings kept: signs()

    # -- pacing -------------------------------------------------------------------------------------------------------
    def ready_in(self, cls: str) -> tuple[float, str | None]:
        """How long until YouTube may be asked for something of a class ("video" or "page") here, and why, if it's a
        limit or a rest rather than the gap since the last request: after any rest YouTube imposed, the gap since any
        request, for a video an hour's share since the last video, and within this computer's own limits."""
        now = time.time()
        for k in ("fetched", "paged"):
            self.state[k] = [t for t in self.state[k] if t > now - 86400]
        times, hourly, daily, what = ((self.state["fetched"], self.per_hour, self.per_day, "videos") if cls == "video" else
                                      (self.state["paged"], PAGES_PER_HOUR, PAGES_PER_DAY, "searches"))
        hour = [t for t in times if t > now - 3600]
        waits = [(self.state.get("rest_until", 0) - now, f"resting after YouTube {self.state.get('rest_why') or 'refused'}")]
        last = max(self.state["fetched"] + self.state["paged"], default=None)
        if last:
            waits.append((last + self.state.get("gap", GAP[0]) - now, None))
        if cls == "video" and times:
            waits.append((max(times) + self.state.get("video_gap", 3600 / self.per_hour) - now, None))
        if len(hour) >= hourly:
            waits.append((min(hour) + 3600 - now, f"{hourly} {what} this hour"))
        if len(times) >= daily:
            waits.append((min(times) + 86400 - now, f"{daily} {what} today"))
        return max(waits, key=lambda w: w[0])

    def wait_turn(self, cls: str) -> None:
        """Until YouTube may be asked for something of a class here (ready_in())."""
        while True:
            wait, why = self.ready_in(cls)
            if wait <= 0:
                return
            if why:
                log(f"waiting {wait / 60:.0f} min: {why}")
            self.nap(min(wait, 600))

    def nap(self, seconds: float) -> None:
        """Wait, telling the server at least every 10 minutes that this is still here (what it tells whoever runs it of
        volunteers joining and leaving counts on that)."""
        end = time.time() + seconds
        while (left := end - time.time()) > 0:
            time.sleep(min(left, 600))
            if end - time.time() > 0 or seconds >= 600:
                try:
                    self.server.call("/volunteer/api/alive", {"version": VERSION}, tries=1)
                except Exception:  # noqa: S110 - the next ask for work says so too
                    pass

    def fetched_now(self, cls: str) -> None:
        self.state["fetched" if cls == "video" else "paged"].append(time.time())
        self.state["gap"] = random.uniform(*GAP)
        if cls == "video":
            self.state["video_gap"] = random.uniform(*SPREAD) * 3600 / self.per_hour
        self.save()

    def refused(self) -> float:
        level = min(len(BLOCK_HOURS) - 1, self.state.get("refusal_level", -1) + 1)
        self.state.update(refusal_level=level, rest_until=time.time() + BLOCK_HOURS[level] * 3600, rest_why="refused")
        self.save()
        return BLOCK_HOURS[level]

    def react(self, found: list[dict]) -> None:
        """Say what YouTube's signs were, and rest when it throttles."""
        for x in found:
            log(f"YouTube {x['kind'] if x['kind'] != 'note' else 'warning'}: {x['what']}")
        throttled = [x for x in found if x["kind"] == "throttled"]
        if throttled:
            self.throttled(throttled[0]["what"])

    def throttled(self, why: str) -> None:
        """YouTube throttles this computer: it rests THROTTLE_HOURS, without the refusals' growing rests."""
        self.state.update(rest_until=max(self.state.get("rest_until", 0), time.time() + THROTTLE_HOURS * 3600), rest_why="throttled")
        self.save()
        log(f"YouTube seems to be throttling this computer ({why}): resting {THROTTLE_HOURS} h")

    def speed(self, size: int, seconds: float) -> tuple[float | None, bool]:
        """A whole video's speed, in KB/s (None if too small to tell), and whether that's throttled: under SLOW_KBPS, and
        under a third of this computer's usual (the median of its last speeds that weren't), or before it has a usual,
        under half SLOW_KBPS (so that a slow connection becomes its own usual, rather than always looking throttled)."""
        if size < 3 * 2**20 or seconds <= 0:
            return None, False
        kbps = size / 1024 / seconds
        usual = sorted(self.state.get("speeds") or [])
        slow = kbps < SLOW_KBPS and (kbps < usual[len(usual) // 2] / 3 if len(usual) >= 3 else kbps < SLOW_KBPS / 2)
        if not slow:
            self.state["speeds"] = (self.state.get("speeds") or [])[-9:] + [round(kbps)]
        return round(kbps), slow

    def health(self, found: list[dict], kbps: float | None = None) -> dict:
        """What's sent with each task's result: how YouTube treated this computer (signs()), its speed, its yt-dlp,
        and the trial it ran under."""
        return {"signs": found, "kbps": kbps, "yt_dlp": getattr(self, "ytdlp_version", None),
                "trial": (getattr(self, "trial", None) or {}).get("name")}

    # -- one task -----------------------------------------------------------------------------------------------------
    def do(self, task: dict) -> None:
        vid, kind = task.get("video_id") or "", task.get("kind")
        if kind == "search":
            words, n = str(task.get("query") or ""), int(task.get("max_results") or 15)
            if not WORDS.fullmatch(words) or not 1 <= n <= 50:
                raise RuntimeError(f"the server asked for something this script doesn't do: {task}")
            return self.page(task, ["--flat-playlist", f"ytsearch{n}:{words}"], f"searching YouTube for {words!r}")
        if kind == "channel":
            cid = str(task.get("channel_id") or "")
            if not CHANNEL_ID.fullmatch(cid):
                raise RuntimeError(f"the server asked for something this script doesn't do: {task}")
            return self.page(task, ["--flat-playlist", "--playlist-items", "0", f"https://www.youtube.com/channel/{cid}"],
                             f"reading the description of https://www.youtube.com/channel/{cid}")
        if kind == "storyboard":
            return self.storyboard(task)
        if not VIDEO_ID.fullmatch(vid) or kind not in ("video", "details"):
            raise RuntimeError(f"the server asked for something this script doesn't do: {task}")
        url = f"https://www.youtube.com/watch?v={vid}"
        work = self.home / "work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        done = f"/volunteer/api/tasks/{task['id']}/done"
        try:
            self.wait_turn("video")
            log(f"fetching {'the video' if kind == 'video' else 'the details of'} {url}")
            self.fetched_now("video")
            began = time.time()
            if kind == "details":
                r = self.run_ytdlp(["-J", "--skip-download", "--no-playlist", url], work, trial=True)
            else:
                r = self.run_ytdlp(["--no-progress", "--no-playlist", "-f", task["format"], "--max-filesize", task["max_filesize"],
                                    "--write-info-json", "-o", "video.%(ext)s", url], work, trial=True)
            failed = bool(r.returncode or (kind == "video" and not self.video_file(work)))
            err, found = (r.stderr or "").strip(), signs(r.stderr, failed)
            if failed:
                if REFUSAL.search(err):
                    hours = self.refused()
                    self.server.call(done, {"status": "refused", "error": refusal_report(err), "health": self.health(found)})
                    log(f"YouTube refused: resting {hours} h")
                    return
                why = last_error(err) or ("larger than max-filesize" if r.returncode == 0 else f"yt-dlp exited with {r.returncode}")
                self.server.call(done, {"status": "failed", "error": why[-300:], "permanent": bool(FOR_GOOD.search(why) or FOR_GOOD.search(err)),
                                        "health": self.health(found)})
                log(f"couldn't fetch it: {why}")
                self.react(found)
                return
            self.state["refusal_level"] = -1
            self.save()
            kbps = None
            if kind == "details":
                info = json.loads(r.stdout)
            else:
                info = json.loads((work / "video.info.json").read_text())
                video = self.video_file(work)
                kbps, slow = self.speed(video.stat().st_size, time.time() - began)
                if slow:
                    found.append({"kind": "throttled", "what": f"the video came at {kbps:.0f} KB/s"})
                self.send(task, video)
            out = self.server.call(done, {"status": "ok", "info": {k: info.get(k) for k in KEEP}, "health": self.health(found, kbps),
                                          "storyboards": storyboards(info, vid)})
            log("sent" if out.get("ok") else f"the server didn't take it: {out}")
            self.react(found)
        except KeyboardInterrupt:
            try:
                self.server.call(done, {"status": "released"}, tries=1)
            except Exception:  # noqa: S110 - stopping anyway
                pass
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def page(self, task: dict, args: list[str], what: str) -> None:
        """A search's results, or a channel's name and description: one request to YouTube, within the pages' own limits."""
        done = f"/volunteer/api/tasks/{task['id']}/done"
        try:
            self.wait_turn("page")
            log(what)
            self.fetched_now("page")
            r = self.run_ytdlp(["-J", *args], self.home)
            err, found = (r.stderr or "").strip(), signs(r.stderr, bool(r.returncode))
            if r.returncode:
                if REFUSAL.search(err):
                    hours = self.refused()
                    self.server.call(done, {"status": "refused", "error": refusal_report(err), "health": self.health(found)})
                    log(f"YouTube refused: resting {hours} h")
                    return
                why = last_error(err) or f"yt-dlp exited with {r.returncode}"
                self.server.call(done, {"status": "failed", "error": why[-300:], "permanent": bool(FOR_GOOD.search(err)),
                                        "health": self.health(found)})
                log(f"couldn't do it: {why}")
                self.react(found)
                return
            self.state["refusal_level"] = -1
            self.save()
            info = json.loads(r.stdout or "{}")
            if task["kind"] == "search":
                results = [{k: e.get(k) for k in RESULT_KEYS} for e in info.get("entries") or [] if isinstance(e, dict)]
                out = self.server.call(done, {"status": "ok", "results": results, "health": self.health(found)})
                log(f"sent {len(results)} result(s)" if out.get("ok") else f"the server didn't take it: {out}")
            else:
                out = self.server.call(done, {"status": "ok", "info": {"name": info.get("channel") or info.get("uploader") or info.get("title"),
                                                                        "description": info.get("description") or ""}, "health": self.health(found)})
                log("sent" if out.get("ok") else f"the server didn't take it: {out}")
            self.react(found)
        except KeyboardInterrupt:
            try:
                self.server.call(done, {"status": "released"}, tries=1)
            except Exception:  # noqa: S110 - stopping anyway
                pass
            raise

    def storyboard(self, task: dict) -> None:
        """Another volunteer's video's storyboard, for the server to check that video by: the images at the addresses
        given (YouTube's, for that video), or with "fresh", at those yt-dlp finds now (a request to YouTube's player,
        taking a video's turn). Sent as one zip file."""
        vid = str(task.get("video_id") or "")
        done = f"/volunteer/api/tasks/{task['id']}/done"
        urls = [str(u) for u in task.get("urls") or []]
        if not VIDEO_ID.fullmatch(vid) or not all(storyboard_url(u, vid) for u in urls) or not (urls or task.get("fresh")):
            raise RuntimeError(f"the server asked for something this script doesn't do: {task}")
        work = self.home / "work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        report = {}
        try:
            if task.get("fresh"):
                self.wait_turn("video")
                log(f"fetching the details of https://www.youtube.com/watch?v={vid}, for its storyboard")
                self.fetched_now("video")
                r = self.run_ytdlp(["-J", "--skip-download", "--no-playlist", f"https://www.youtube.com/watch?v={vid}"], work, trial=True)
                err, found = (r.stderr or "").strip(), signs(r.stderr, bool(r.returncode))
                if r.returncode:
                    if REFUSAL.search(err):
                        hours = self.refused()
                        self.server.call(done, {"status": "refused", "error": refusal_report(err), "health": self.health(found)})
                        log(f"YouTube refused: resting {hours} h")
                        return
                    why = last_error(err) or f"yt-dlp exited with {r.returncode}"
                    self.server.call(done, {"status": "failed", "error": why[-300:], "health": self.health(found)})
                    return
                best = best_storyboard(storyboards(json.loads(r.stdout), vid))
                if not best:
                    self.server.call(done, {"status": "failed", "error": "YouTube has no storyboard of it", "health": self.health(found)})
                    return
                urls, report["storyboard"] = [x["url"] for x in best["fragments"]], best
            self.wait_turn("page")
            log(f"fetching {len(urls)} storyboard image(s) of https://www.youtube.com/watch?v={vid}")
            self.fetched_now("page")
            bundle = work / "storyboard.zip"
            with zipfile.ZipFile(bundle, "w") as z:
                for i, u in enumerate(urls):
                    if i:
                        time.sleep(random.uniform(1, 3))
                    try:
                        req = urllib.request.Request(u, headers={"User-Agent": BROWSER})
                        with urllib.request.urlopen(req, timeout=60) as resp:
                            z.writestr(f"{i}.img", resp.read(STORYBOARD_BYTES))
                    except urllib.error.HTTPError as e:
                        self.server.call(done, {"status": "failed", "denied": e.code in (401, 403, 404, 410),
                                                "error": f"i.ytimg.com said HTTP {e.code} for image {i + 1} of {len(urls)}",
                                                "health": self.health([])})
                        log(f"i.ytimg.com said HTTP {e.code}")
                        return
            path = f"/volunteer/api/tasks/{task['id']}/part"
            size = bundle.stat().st_size
            self.server.call(f"{path}?offset=0&size={size}&ext=zip", bundle.read_bytes())
            out = self.server.call(done, {"status": "ok", "health": self.health([]), **report})
            log("sent" if out.get("ok") else f"the server didn't take it: {out}")
        except KeyboardInterrupt:
            try:
                self.server.call(done, {"status": "released"}, tries=1)
            except Exception:  # noqa: S110 - stopping anyway
                pass
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def video_file(work: Path) -> Path | None:
        return next((p for p in work.glob("video.*") if not p.name.endswith((".json", ".part", ".ytdl"))), None)

    def send(self, task: dict, video: Path) -> None:
        """The video in parts, carrying on from what the server has after a dropped connection."""
        size, ext, part = video.stat().st_size, video.suffix.lstrip("."), int(task.get("part") or 64 * 2**20)
        path = f"/volunteer/api/tasks/{task['id']}/part"
        sent = self.server.call(path).get("received", 0)
        log(f"sending {size / 1e6:.0f} MB")
        with open(video, "rb") as f:
            while sent < size:
                f.seek(sent)
                try:
                    sent = self.server.call(f"{path}?offset={sent}&size={size}&ext={ext}", f.read(part))["received"]
                except urllib.error.HTTPError as e:
                    if e.code != 409 or "received" not in e.answer:
                        raise
                    sent = e.answer["received"]

    # -- the loop -----------------------------------------------------------------------------------------------------
    def renew(self, e: urllib.error.HTTPError) -> None:
        """The server no longer takes this token (401, 403): forget it, warn, and carry on as with none, registering a
        new one."""
        said = (getattr(e, "answer", None) or {}).get("error") or f"HTTP {e.code}"
        log(f"warning: {self.server.url} no longer takes this token ({said}). Forgot it, and registering a new one, which "
            "waits for approval like any other.")
        forget_token(self.args.home)
        conf = load_conf(self.args.home)
        token, name = register(self.server.url, conf["name"] if "name" in conf else self.args.name, patient=True)
        save_conf(self.args.home, {**load_conf(self.args.home), "token": token, "name": name})
        self.server.token = token

    def run(self) -> None:
        self.tools()
        version = self.ytdlp_version = self.run_ytdlp(["--version"], self.home).stdout.strip()
        blocked = update_blocked(self.server.url)
        log(f"volunteering for {self.server.url} with yt-dlp {version}: at most {self.per_hour} videos an hour, {self.per_day} a day, "
            f"and {PAGES_PER_HOUR} searches an hour, {PAGES_PER_DAY} a day; "
            + (f"it doesn't update itself ({blocked})" if blocked else "it updates itself when the server says to"))
        updates = not blocked and not self.args.once
        while True:
            ready = {cls: self.ready_in(cls) for cls in set(CLASS.values())}
            kinds = [k for k in KINDS if ready[CLASS[k]][0] <= 0]   # asking only for what may be fetched now
            if not kinds:
                wait, why = min(ready.values(), key=lambda w: w[0])
                if why and why != getattr(self, "idle_why", None):
                    log(f"waiting {wait / 60:.0f} min: {why}")
                self.idle_why = why
                self.nap(min(wait, 600))
                continue
            try:
                r = self.server.call("/volunteer/api/work", {"version": VERSION, "client": f"script {VERSION}, yt-dlp {version}"
                                                             + ("; signed in (cookies)" if cookie_args(self.args) else "")
                                                             + (f"; won't update itself: {blocked.split(',')[0]}" if blocked else ""),
                                                             "per_hour": self.per_hour, "per_day": self.per_day, "kinds": kinds,
                                                             "updates": updates})
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    self.renew(e)
                    continue
                if e.code == 426:
                    raise turned_away(self.server.url, e, self.home if updates else None) from None
                log(f"the server said: {e.answer.get('error') or e}; asking again in 10 min")
                time.sleep(600)
                continue
            except (urllib.error.URLError, OSError) as e:
                log(f"can't reach {self.server.url} ({e}); asking again in 10 min")
                time.sleep(600)
                continue
            if "update" in r:       # between one fetch and the next: nothing's lost
                if not update_itself(self.server.url, r["update"], self.home):
                    updates = False     # it carries on as it is, and the server gives it work again
                continue
            if r.get("yt_dlp") in ("nightly", "stable") and r["yt_dlp"] != (self.state.get("channel") or "stable"):
                self.state["channel"] = r["yt_dlp"]     # between one fetch and the next
                self.save()
                self.tools()
                version = self.ytdlp_version = self.run_ytdlp(["--version"], self.home).stdout.strip()
            self.take_trial(r.get("trial"))
            if r.get("newer") and r["newer"] != getattr(self, "newer", None):
                log(f"the server has a newer version of this script ({r['newer']}): curl -O {self.server.url}{SCRIPT_PATH}")
                self.newer = r["newer"]
            if "task" in r:
                self.idle_why = None
                try:
                    self.do(r["task"])
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):       # its token stopped working partway through
                        self.renew(e)
                        continue
                    if e.code == 426:
                        raise turned_away(self.server.url, e, self.home if updates else None) from None
                    log(f"the server said: {(getattr(e, 'answer', None) or {}).get('error') or e}")
                except (urllib.error.URLError, OSError) as e:      # as the server going, after trying again
                    log(f"stopped partway ({e}); asking again in 10 min")
                    time.sleep(600)
                if self.args.once:
                    return
                continue
            if self.args.once:
                log(f"nothing to do: {r.get('why')}")
                return
            later = [w for w, _ in ready.values() if w > 0]     # a kind not asked for, whose turn comes sooner
            wait = max(60, min([r["wait"], 3600, *later]))
            if r.get("why") != getattr(self, "idle_why", None):      # said once, not every minute
                log(f"nothing to fetch now ({r.get('why')}): asking again in {wait / 60:.0f} min")
            self.idle_why = r.get("why")
            self.nap(wait)
            if time.time() - self.state.get("updated", 0) > 86400:
                self.tools()
                version = self.ytdlp_version = self.run_ytdlp(["--version"], self.home).stdout.strip()


def cookie_args(args) -> list[str]:
    """yt-dlp's options for the cookies given (--cookies, --cookies-from-browser), or none."""
    if getattr(args, "cookies", None):
        return ["--cookies", str(Path(args.cookies).expanduser())]
    if getattr(args, "cookies_from_browser", None):
        return ["--cookies-from-browser", args.cookies_from_browser]
    return []


def cookies_agreed(args, conf: dict) -> bool:
    """With cookies given, COOKIES_WARNING printed and agreed to, unless agreed to before for the same cookies (each
    start then says only which); --agree takes it as agreed, after printing it. False if it isn't agreed to."""
    given = " ".join(cookie_args(args))
    if not given:
        return True
    if conf.get("cookies_agreed") == given:
        log(f"signing in to YouTube with {given} (an account at risk of a ban: --help says more)")
        return True
    print("\n" + COOKIES_WARNING + "\n")
    if args.agree:
        print("(--agree: taken as agreed)")
    else:
        try:
            if input("Type yes to have yt-dlp sign in with these cookies: ").strip().lower() != "yes":
                return False
        except EOFError:        # no one to ask (a service): run it once at a terminal first, or with --agree
            return False
    conf["cookies_agreed"] = given
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Cookies (--cookies, --cookies-from-browser):\n\n" + COOKIES_WARNING)
    ap.add_argument("--server", help="the BoulderMesh site, such as https://boulders.example.com (remembered)")
    ap.add_argument("--token", help="a token whoever runs the server gave you (remembered); without one, it makes its own "
                    "the first time, and registers it")
    ap.add_argument("--name", help="your name or a nickname for whoever runs the server, when registering (asked for if not given)")
    ap.add_argument("--register", action="store_true", help="make a new token and register it, in place of the one remembered")
    ap.add_argument("--forget-token", action="store_true", help="delete the remembered token, and stop: the next run makes and "
                    "registers a new one")
    ap.add_argument("--per-hour", type=int, default=PER_HOUR, help=f"at most this many videos an hour ({PER_HOUR} at most), spread "
                    "evenly over it")
    ap.add_argument("--per-day", type=int, default=PER_DAY, help=f"at most this many videos a day ({PER_DAY} at most); searches "
                    f"have their own limits, {PAGES_PER_HOUR} an hour and {PAGES_PER_DAY} a day")
    ap.add_argument("--agree", action="store_true", help="agree to what's explained on the first run without being asked")
    ap.add_argument("--home", default=os.environ.get("BOULDERMESH_VOLUNTEER_HOME", "~/.bouldermesh-volunteer"),
                    help="where it keeps its settings, yt-dlp and its work")
    ap.add_argument("--yt-dlp", help="use this yt-dlp instead of installing one (it must find Deno itself)")
    ap.add_argument("--once", action="store_true", help="do one thing and stop")
    ap.add_argument("--no-auto-update", action="store_true", help="don't replace this script with a newer one when the server "
                    "has one (it says so instead)")
    ap.add_argument("--cookies", metavar="FILE", help="sign in to YouTube with the cookies in FILE (cookies.txt format; "
                    "remembered). This can get that account banned: see the warning about cookies at the end of --help")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER[:PROFILE]", help="sign in to YouTube with the cookies a browser "
                    "of yours has (as yt-dlp's option of that name; remembered). This can get that account banned, and "
                    "YouTube soon changes the cookies of a browser in use: see the warning about cookies at the end of --help")
    ap.add_argument("--no-cookies", action="store_true", help="forget the cookies remembered, and fetch without signing in")
    args = ap.parse_args()
    global auto_update
    auto_update = not args.no_auto_update
    home = Path(args.home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    conf = load_conf(home)
    if args.forget_token:
        log(f"forgot the token remembered for {conf.get('server')}" if forget_token(home) else "no token was remembered")
        return
    server_changed = args.server and args.server != conf.get("server")
    args.server = args.server or conf.get("server")
    if args.register and args.token:
        ap.error("--register makes a new token, so not with --token")
    args.token = args.token or (None if server_changed or args.register else conf.get("token"))
    if not args.server:
        ap.error("--server is needed the first time")
    if args.cookies and args.cookies_from_browser:
        ap.error("--cookies or --cookies-from-browser, not both")
    if args.no_cookies:
        conf.pop("cookies", None)
        conf.pop("cookies_from_browser", None)
        conf.pop("cookies_agreed", None)
    elif args.cookies or args.cookies_from_browser:
        if args.cookies and not Path(args.cookies).expanduser().is_file():
            ap.error(f"--cookies: no such file: {args.cookies}")
        conf["cookies"], conf["cookies_from_browser"] = args.cookies and str(Path(args.cookies).expanduser().resolve()), args.cookies_from_browser
    else:
        args.cookies, args.cookies_from_browser = conf.get("cookies"), conf.get("cookies_from_browser")
    if not cookies_agreed(args, conf):
        raise SystemExit("not started: run it again without --cookies or --cookies-from-browser (or with --no-cookies), "
                         "or at a terminal to agree")
    if conf.get("agreed") != args.server:
        per_hour = max(1, min(args.per_hour, PER_HOUR))
        print(AGREEMENT.format(server=args.server, gap_lo=GAP[0], gap_hi=GAP[1], per_hour=per_hour, spread=60 / per_hour,
                               per_day=min(args.per_day, PER_DAY), pages_hour=PAGES_PER_HOUR, pages_day=PAGES_PER_DAY, home=home))
        if not args.agree and input("\nType yes to start: ").strip().lower() != "yes":
            raise SystemExit("not started")
    signal.signal(signal.SIGTERM, _stop)    # as Ctrl-C: what it was fetching goes back to the server
    try:
        volunteer = Volunteer(args)
        volunteer.tools()       # first, so that a computer that can't install them leaves no registration waiting
        if not args.token:
            args.token, conf["name"] = register(args.server, args.name if args.name is not None else conf.get("name"))
            volunteer.server.token = args.token
        conf.update(server=args.server, token=args.token, agreed=args.server)
        save_conf(home, conf)
        volunteer.run()
    except KeyboardInterrupt:
        log("stopped")
    finally:    # gone, as whoever runs the server is told (not when it updates itself: that carries on, in its place)
        if args.token:
            try:
                Server(args.server, args.token).call("/volunteer/api/bye", {}, tries=1)
            except Exception:  # noqa: S110 - stopping anyway
                pass


def _stop(*_) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    main()
