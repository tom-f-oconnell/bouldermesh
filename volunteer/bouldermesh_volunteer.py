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
a folder of its own (~/.bouldermesh-volunteer), and updates them daily. yt-dlp needs Python 3.11 or newer: with an older
one (as Ubuntu 22.04's and before), it installs uv there too, which fetches a newer Python for them. Stop it with Ctrl-C
at any time; delete that folder to remove everything.

When the server has a newer version of this script, it says so, and the script replaces itself with that (checked
against what the server said of it) and starts again, between one fetch and the next; --no-auto-update stops that.
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
import urllib.request
from pathlib import Path

VERSION = 3                 # 3: updates itself when the server says to
SCRIPT_PATH = "/volunteer/bouldermesh_volunteer.py"     # where the server serves it
AGENT = "bouldermesh-volunteer"
TOOLS_PYTHON = (3, 11)      # the oldest Python today's yt-dlp runs on (and Deno's package wants 3.10)
FETCHED_PYTHON = "3.12"     # what uv fetches for them when this computer has none as new
KINDS = ["video", "details", "search", "channel"]   # what this version does
GAP = (8.0, 15.0)           # seconds between one fetch from YouTube and the next, at random in this range
PER_HOUR, PER_DAY = 15, 100  # the most, whatever the server allows
BLOCK_HOURS = (1, 2, 4, 8)  # rest after YouTube refuses, longer each time it still does
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
TURNED_AWAY = 3             # the exit status when the server won't take this script: starting again won't help
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
WORDS = re.compile(r"[^\x00-\x1f\x7f]{3,200}")
RESULT_KEYS = ("id", "title", "description", "duration", "channel", "channel_id", "uploader")
REFUSAL = re.compile(r"not a bot|HTTP Error 429|Too Many Requests|content isn.t available, try again later", re.I)
FOR_GOOD = re.compile(r"Video unavailable|Private video|has been removed|members[- ]only|not available in your country|"
                      r"account associated with this video has been terminated|copyright|confirm your age|age[- ]restricted|"
                      r"is not a valid URL|larger than max-filesize", re.I)
KEEP = ("title", "uploader", "channel", "channel_id", "channel_url", "uploader_url", "duration", "upload_date",
        "description", "license")
AGREEMENT = """\
Before you start: what this does, and what it may cost you.

- It asks {server} for YouTube videos of climbs, fetches them from this computer, and sends them there:
  one at a time, {gap_lo:.0f}-{gap_hi:.0f} seconds apart, at most {per_hour} an hour and {per_day} a day.
  Each is deleted here once it's sent. Only videos' YouTube ids come from the server. It also searches YouTube
  for climbs' videos, and reads channels' descriptions, within the same limits, from the server's words and ids.
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


def why(e: Exception) -> str:
    return f"{Path(e.cmd[0]).name} exited with {e.returncode}" if isinstance(e, subprocess.CalledProcessError) else str(e)


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
        name = input("Your name or a nickname, shown to whoever runs the server (optional; Enter to leave it out): ")
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
    if any((d / ".git").exists() for d in me.parents):
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
        found = re.search(rb"^VERSION = (\d+)$", data, re.M)
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
        self.state.setdefault("fetched", [])
        self.server = Server(args.server, args.token)
        self.per_hour, self.per_day = min(args.per_hour, PER_HOUR), min(args.per_day, PER_DAY)
        self.ytdlp = args.yt_dlp

    def save(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.state_file)

    # -- yt-dlp ---------------------------------------------------------------------------------------------------
    def tools(self) -> None:
        """yt-dlp and Deno in a venv here, made the first time (again, if its Python is too old for them) and updated
        daily, unless --yt-dlp names one. Exits saying why if they can't be installed; a failed update carries on with
        those there are."""
        if self.args.yt_dlp:
            return
        venv = self.home / "venv"
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        self.ytdlp = str(bindir / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp"))
        fresh = not Path(self.ytdlp).exists() or python_version(python) < TOOLS_PYTHON
        if not fresh and time.time() - self.state.get("updated", 0) < 86400:
            return
        try:
            if fresh:
                if venv.exists():
                    log(f"making {venv} again, with a Python new enough for today's yt-dlp")
                    shutil.rmtree(venv)
                log(f"installing yt-dlp and Deno in {venv} (a minute or two)")
                self.make_venv(venv)
            else:
                log("updating yt-dlp and Deno")
            pip = [str(python), "-m", "pip", "install", "-q", "--upgrade"]
            subprocess.run([*pip, "pip"], check=True)
            subprocess.run([*pip, "yt-dlp[default]", "deno"], check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            if fresh:
                shutil.rmtree(venv, ignore_errors=True)     # tried again from the start next time
                raise SystemExit(f"couldn't install yt-dlp and Deno ({why(e)}): what's printed above says why. Or give "
                                 "--yt-dlp with one you have, version 2025.11 or newer, with Deno on the PATH.") from None
            log(f"couldn't update yt-dlp and Deno ({why(e)}): carrying on with those there are, and trying again tomorrow")
        self.state["updated"] = time.time()
        self.save()

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

    def run_ytdlp(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = str(Path(self.ytdlp).parent) + os.pathsep + env.get("PATH", "")    # Deno, beside it
        return subprocess.run([self.ytdlp, "--no-warnings", "--sleep-requests", "0.75", *args], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=3600)

    # -- pacing -------------------------------------------------------------------------------------------------------
    def wait_turn(self) -> None:
        """Until YouTube may be asked again here: after any rest it imposed, the gap since the last fetch, and
        within this computer's own hourly and daily limits."""
        while True:
            now = time.time()
            self.state["fetched"] = [t for t in self.state["fetched"] if t > now - 86400]
            hour = [t for t in self.state["fetched"] if t > now - 3600]
            waits = [(self.state.get("rest_until", 0) - now, "resting after YouTube refused")]
            if self.state["fetched"]:
                waits.append((max(self.state["fetched"]) + self.state.get("gap", GAP[0]) - now, None))
            if len(hour) >= self.per_hour:
                waits.append((min(hour) + 3600 - now, f"{self.per_hour} this hour"))
            if len(self.state["fetched"]) >= self.per_day:
                waits.append((min(self.state["fetched"]) + 86400 - now, f"{self.per_day} today"))
            wait, why = max(waits, key=lambda w: w[0])
            if wait <= 0:
                return
            if why:
                log(f"waiting {wait / 60:.0f} min: {why}")
            time.sleep(min(wait, 600))

    def fetched_now(self) -> None:
        self.state["fetched"].append(time.time())
        self.state["gap"] = random.uniform(*GAP)
        self.save()

    def refused(self) -> float:
        level = min(len(BLOCK_HOURS) - 1, self.state.get("refusal_level", -1) + 1)
        self.state.update(refusal_level=level, rest_until=time.time() + BLOCK_HOURS[level] * 3600)
        self.save()
        return BLOCK_HOURS[level]

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
        if not VIDEO_ID.fullmatch(vid) or kind not in ("video", "details"):
            raise RuntimeError(f"the server asked for something this script doesn't do: {task}")
        url = f"https://www.youtube.com/watch?v={vid}"
        work = self.home / "work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        done = f"/volunteer/api/tasks/{task['id']}/done"
        try:
            self.wait_turn()
            log(f"fetching {'the video' if kind == 'video' else 'the details of'} {url}")
            self.fetched_now()
            if kind == "details":
                r = self.run_ytdlp(["-J", "--skip-download", "--no-playlist", url], work)
            else:
                r = self.run_ytdlp(["-q", "--no-playlist", "-f", task["format"], "--max-filesize", task["max_filesize"],
                                    "--write-info-json", "-o", "video.%(ext)s", url], work)
            err = (r.stderr or "").strip()
            if r.returncode or (kind == "video" and not self.video_file(work)):
                if REFUSAL.search(err):
                    hours = self.refused()
                    self.server.call(done, {"status": "refused", "error": err[-300:]})
                    log(f"YouTube refused: resting {hours} h")
                    return
                why = err.splitlines()[-1] if err else ("larger than max-filesize" if r.returncode == 0 else f"yt-dlp exited with {r.returncode}")
                self.server.call(done, {"status": "failed", "error": why[-300:], "permanent": bool(FOR_GOOD.search(why) or FOR_GOOD.search(err))})
                log(f"couldn't fetch it: {why}")
                return
            self.state["refusal_level"] = -1
            self.save()
            if kind == "details":
                info = json.loads(r.stdout)
            else:
                info = json.loads((work / "video.info.json").read_text())
                self.send(task, self.video_file(work))
            out = self.server.call(done, {"status": "ok", "info": {k: info.get(k) for k in KEEP}})
            log("sent" if out.get("ok") else f"the server didn't take it: {out}")
        except KeyboardInterrupt:
            try:
                self.server.call(done, {"status": "released"}, tries=1)
            except Exception:  # noqa: S110 - stopping anyway
                pass
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def page(self, task: dict, args: list[str], what: str) -> None:
        """A search's results, or a channel's name and description: one request to YouTube, paced like a video."""
        done = f"/volunteer/api/tasks/{task['id']}/done"
        try:
            self.wait_turn()
            log(what)
            self.fetched_now()
            r = self.run_ytdlp(["-J", *args], self.home)
            err = (r.stderr or "").strip()
            if r.returncode:
                if REFUSAL.search(err):
                    hours = self.refused()
                    self.server.call(done, {"status": "refused", "error": err[-300:]})
                    log(f"YouTube refused: resting {hours} h")
                    return
                why = err.splitlines()[-1] if err else f"yt-dlp exited with {r.returncode}"
                self.server.call(done, {"status": "failed", "error": why[-300:], "permanent": bool(FOR_GOOD.search(err))})
                log(f"couldn't do it: {why}")
                return
            self.state["refusal_level"] = -1
            self.save()
            info = json.loads(r.stdout or "{}")
            if task["kind"] == "search":
                results = [{k: e.get(k) for k in RESULT_KEYS} for e in info.get("entries") or [] if isinstance(e, dict)]
                out = self.server.call(done, {"status": "ok", "results": results})
                log(f"sent {len(results)} result(s)" if out.get("ok") else f"the server didn't take it: {out}")
            else:
                out = self.server.call(done, {"status": "ok", "info": {"name": info.get("channel") or info.get("uploader") or info.get("title"),
                                                                        "description": info.get("description") or ""}})
                log("sent" if out.get("ok") else f"the server didn't take it: {out}")
        except KeyboardInterrupt:
            try:
                self.server.call(done, {"status": "released"}, tries=1)
            except Exception:  # noqa: S110 - stopping anyway
                pass
            raise

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
        version = self.run_ytdlp(["--version"], self.home).stdout.strip()
        blocked = update_blocked(self.server.url)
        log(f"volunteering for {self.server.url} with yt-dlp {version}: at most {self.per_hour} an hour, {self.per_day} a day; "
            + (f"it doesn't update itself ({blocked})" if blocked else "it updates itself when the server says to"))
        updates = not blocked and not self.args.once
        while True:
            self.wait_turn()
            try:
                r = self.server.call("/volunteer/api/work", {"version": VERSION, "client": f"script {VERSION}, yt-dlp {version}",
                                                             "per_hour": self.per_hour, "per_day": self.per_day, "kinds": KINDS,
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
            wait = max(60, min(r["wait"], 3600))
            if r.get("why") != getattr(self, "idle_why", None):      # said once, not every minute
                log(f"nothing to fetch now ({r.get('why')}): asking again every {wait / 60:.0f} min")
            self.idle_why = r.get("why")
            time.sleep(wait)
            if time.time() - self.state.get("updated", 0) > 86400:
                self.tools()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--server", help="the BoulderMesh site, such as https://boulders.example.com (remembered)")
    ap.add_argument("--token", help="a token whoever runs the server gave you (remembered); without one, it makes its own "
                    "the first time, and registers it")
    ap.add_argument("--name", help="your name or a nickname for whoever runs the server, when registering (asked for if not given)")
    ap.add_argument("--register", action="store_true", help="make a new token and register it, in place of the one remembered")
    ap.add_argument("--forget-token", action="store_true", help="delete the remembered token, and stop: the next run makes and "
                    "registers a new one")
    ap.add_argument("--per-hour", type=int, default=PER_HOUR, help=f"at most this many videos an hour ({PER_HOUR} at most)")
    ap.add_argument("--per-day", type=int, default=PER_DAY, help=f"at most this many a day ({PER_DAY} at most)")
    ap.add_argument("--agree", action="store_true", help="agree to what's explained on the first run without being asked")
    ap.add_argument("--home", default=os.environ.get("BOULDERMESH_VOLUNTEER_HOME", "~/.bouldermesh-volunteer"),
                    help="where it keeps its settings, yt-dlp and its work")
    ap.add_argument("--yt-dlp", help="use this yt-dlp instead of installing one (it must find Deno itself)")
    ap.add_argument("--once", action="store_true", help="do one thing and stop")
    ap.add_argument("--no-auto-update", action="store_true", help="don't replace this script with a newer one when the server "
                    "has one (it says so instead)")
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
    if conf.get("agreed") != args.server:
        print(AGREEMENT.format(server=args.server, gap_lo=GAP[0], gap_hi=GAP[1], per_hour=min(args.per_hour, PER_HOUR),
                               per_day=min(args.per_day, PER_DAY), home=home))
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


def _stop(*_) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    main()
