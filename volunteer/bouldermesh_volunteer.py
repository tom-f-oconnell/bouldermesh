#!/usr/bin/env python3
"""Volunteer this computer to fetch YouTube videos for a BoulderMesh server, slowly, one at a time.

BoulderMesh makes 3D models of boulders from photos and climbing videos. YouTube stops one server from fetching many
videos, so volunteers fetch some from their own addresses. This script asks the server for a video (only ever by its
YouTube id: it makes the youtube.com address itself), fetches it with yt-dlp, sends it to the server, deletes its copy,
and waits before the next. It also searches YouTube for a climb's videos (the server sends only the words), and reads a
channel's description (by its id), sending back what they say. Run it with the token you were given:

    python3 bouldermesh_volunteer.py --server https://boulders.example.com --token bmv_...

Python 3.9 or newer, nothing else: it installs yt-dlp, and Deno (the JavaScript runtime yt-dlp needs for YouTube), into
a folder of its own (~/.bouldermesh-volunteer), and updates them daily. Stop it with Ctrl-C at any time; delete that
folder to remove everything.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = 2
KINDS = ["video", "details", "search", "channel"]   # what this version does
GAP = (8.0, 15.0)           # seconds between one fetch from YouTube and the next, at random in this range
PER_HOUR, PER_DAY = 15, 100  # the most, whatever the server allows
BLOCK_HOURS = (1, 2, 4, 8)  # rest after YouTube refuses, longer each time it still does
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
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
  limits above between everyone at that address, and doesn't keep it with the videos.

Stop it with Ctrl-C at any time. Everything it installs is in {home}."""


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


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
        """yt-dlp and Deno in a venv here, made the first time and updated daily, unless --yt-dlp names one."""
        if self.args.yt_dlp:
            return
        venv = self.home / "venv"
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        self.ytdlp = str(bindir / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp"))
        if not Path(self.ytdlp).exists():
            log(f"installing yt-dlp and Deno in {venv} (a minute or two)")
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        elif time.time() - self.state.get("updated", 0) < 86400:
            return
        else:
            log("updating yt-dlp and Deno")
        pip = str(bindir / ("pip.exe" if os.name == "nt" else "pip"))
        subprocess.run([pip, "install", "-q", "--upgrade", "yt-dlp[default]", "deno"], check=True)
        self.state["updated"] = time.time()
        self.save()

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
    def run(self) -> None:
        self.tools()
        version = self.run_ytdlp(["--version"], self.home).stdout.strip()
        log(f"volunteering for {self.server.url} with yt-dlp {version}: at most {self.per_hour} an hour, {self.per_day} a day")
        while True:
            self.wait_turn()
            try:
                r = self.server.call("/volunteer/api/work", {"version": VERSION, "client": f"script {VERSION}, yt-dlp {version}",
                                                             "per_hour": self.per_hour, "per_day": self.per_day, "kinds": KINDS})
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 426):
                    raise SystemExit(f"the server said: {e.answer.get('error') or e}") from None
                log(f"the server said: {e.answer.get('error') or e}; asking again in 10 min")
                time.sleep(600)
                continue
            except (urllib.error.URLError, OSError) as e:
                log(f"can't reach {self.server.url} ({e}); asking again in 10 min")
                time.sleep(600)
                continue
            if "task" in r:
                try:
                    self.do(r["task"])
                except urllib.error.HTTPError as e:
                    log(f"the server said: {e.answer.get('error') or e}")
                if self.args.once:
                    return
                continue
            if self.args.once:
                log(f"nothing to do: {r.get('why')}")
                return
            log(f"nothing to fetch now ({r.get('why')}): asking again in {r['wait'] / 60:.0f} min")
            time.sleep(max(60, min(r["wait"], 3600)))
            if time.time() - self.state.get("updated", 0) > 86400:
                self.tools()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--server", help="the BoulderMesh site, such as https://boulders.example.com (remembered)")
    ap.add_argument("--token", help="the token you were given (remembered)")
    ap.add_argument("--per-hour", type=int, default=PER_HOUR, help=f"at most this many videos an hour ({PER_HOUR} at most)")
    ap.add_argument("--per-day", type=int, default=PER_DAY, help=f"at most this many a day ({PER_DAY} at most)")
    ap.add_argument("--agree", action="store_true", help="agree to what's explained on the first run without being asked")
    ap.add_argument("--home", default=os.environ.get("BOULDERMESH_VOLUNTEER_HOME", "~/.bouldermesh-volunteer"),
                    help="where it keeps its settings, yt-dlp and its work")
    ap.add_argument("--yt-dlp", help="use this yt-dlp instead of installing one (it must find Deno itself)")
    ap.add_argument("--once", action="store_true", help="do one thing and stop")
    args = ap.parse_args()
    home = Path(args.home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    conf_file = home / "config.json"
    try:
        conf = json.loads(conf_file.read_text())
    except (FileNotFoundError, ValueError):
        conf = {}
    args.server, args.token = args.server or conf.get("server"), args.token or conf.get("token")
    if not args.server or not args.token:
        ap.error("--server and --token are needed the first time")
    if conf.get("agreed") != args.server:
        print(AGREEMENT.format(server=args.server, gap_lo=GAP[0], gap_hi=GAP[1], per_hour=min(args.per_hour, PER_HOUR),
                               per_day=min(args.per_day, PER_DAY), home=home))
        if not args.agree and input("\nType yes to start: ").strip().lower() != "yes":
            raise SystemExit("not started")
    conf.update(server=args.server, token=args.token, agreed=args.server)
    conf_file.write_text(json.dumps(conf))
    os.chmod(conf_file, 0o600)
    signal.signal(signal.SIGTERM, _stop)    # as Ctrl-C: what it was fetching goes back to the server
    try:
        Volunteer(args).run()
    except KeyboardInterrupt:
        log("stopped")


def _stop(*_) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    main()
