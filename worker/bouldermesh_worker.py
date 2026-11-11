#!/usr/bin/env python3
"""Lend this machine to a BoulderMesh server for reconstructions, when the server can't reach it.

BoulderMesh makes 3D models of boulders from photos. A small server can't reconstruct them itself, and one machine
that can may be behind a firewall the server can't get through. This script asks the server for a climb to mesh, fetches
its photos over HTTPS, runs the reconstruction in the server's own Docker image (with the GPU, if there is one), sends
its log as it goes, and then the mesh. Run it with the server's address:

    python3 bouldermesh_worker.py --server https://boulders.example.com --check

The first time, it makes this machine's token, asks your name (optional, for whoever runs the server), and registers
the token with the server, which gives it climbs once whoever runs it approves the machine. A token from the server's
admin page works too (--token), at once.

Python 3.9 or newer and Docker (with NVIDIA's container toolkit, for a GPU), which whoever runs it may use. The server
and token are remembered (in ~/.bouldermesh-worker), so after the first time it's only `python3 bouldermesh_worker.py`.
Jobs' photos and results, and the models they download, go there too, or under --data, such as on a bigger disk. It
checks there's room for a couple of climbs there, and for the image in Docker's storage, before starting either, and
stops saying what's short rather than filling a disk partway through a job. Each job's folder is deleted after it, and
an image it pulled once it pulls a newer one.
Stop it with Ctrl-C: the climb it was on goes back to the server, for this or another machine.

It keeps a record of its time in usage.jsonl in ~/.bouldermesh-worker (each job, and each change between working, waiting
for climbs and held back), and says once an hour how the hour went.

It takes no climbs while others keep the GPU busy (over 50% as a median over 3 minutes, until it's quiet again for 5;
--busy-gpu-util), or while less than 24 GB of memory is free (--need-ram-gb), and only those whose GPU memory fits in
what's free; it tells the server why, which keeps a record of how often each machine works, waits or is held back. A
climb it has started, it finishes. Its CPU use is held down by --nice and --cpus.

When the server has a newer version of this script, it says so when the script asks for a climb, and the script replaces
itself with that (checked against what the server said of it) and starts again, between one job and the next, never
during one; --no-auto-update stops that.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

VERSION = 7                 # 2: takes task.json for a reconstruction; 3: updates itself when the server says to;
                            # 4: holds back while others keep its GPU busy or its memory is short, and says so;
                            # 5: says goodbye when it stops, so that the server knows at once it's gone;
                            # 6: updates itself when downloaded into another git repository's folder, and says why if not;
                            # 7: keeps a record of its time (usage.jsonl), and says once an hour how the hour went
SCRIPT_PATH = "/worker/bouldermesh_worker.py"     # where the server serves it
AGENT = "bouldermesh-worker"
TASKS = ("reconstruct", "beta", "scale")     # remote_job.py's: meshing a climb, finding its beta, finding its size
INPUTS = ("photos", "masks", "mesh", "videos", "task.json")   # what a job's folder may hold
SEND_EVERY = 5              # seconds between reports of a job's log, which also tell the server it's alive
JOB_ID = re.compile(r"[0-9a-f]{12}")
GB = 2**30                  # as df -h counts them
CLIMB_GB = 3.0              # a climb's job folder at its biggest: its photos, COLMAP's and OpenMVS's work, its results
CLIMBS_AHEAD = 2            # room for this many climbs before taking one
MODELS_GB = 5.5             # the models jobs download the first time they need them (VGGT's weights are 4.7 GB of it)
IMAGE_TIMES = 4             # an image takes about this many times its download (compressed layers, kept, and unpacked)
IMAGE_GUESS_GB = 18         # what to allow for an image whose size the registry won't say (the GPU build's is about 16)
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}")
TURNED_AWAY = 3             # the exit status when the server won't take this script: starting again won't help,
                            # so systemd units leave it stopped (RestartPreventExitStatus)
BUSY_GPU_UTIL = 50          # others' GPU use (%), as a median, over which it takes no climbs
# memory free (MemAvailable) a job may need at its peak (19 GB seen on hal-2, 24 on hal itself); the variable, as the flag
NEED_RAM_GB = float(os.environ.get("BOULDERMESH_NEED_RAM_GB", "24"))
# seconds between GPU samples, over which others' use makes it hold back, and over which it must be quiet again to stop
GPU_EVERY, BUSY_WINDOW, QUIET_WINDOW = (float(x) for x in os.environ.get("BOULDERMESH_GPU_WINDOWS", "5,180,300").split(","))


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


class GpuWatch:
    """The GPU's use (nvidia-smi), sampled every GPU_EVERY seconds on a thread of its own, each sample marked by whether
    a job of this script's was running then (or had just ended), so that others' use can be told from its own."""

    def __init__(self):
        self.samples: deque = deque(maxlen=int(QUIET_WINDOW / GPU_EVERY) + 10)     # (time, util %, used MB, total MB, ours)
        self.running, self.ours_until = False, 0.0
        threading.Thread(target=self._loop, daemon=True).start()

    @staticmethod
    def sample() -> tuple[float, float, float] | None:
        """The first GPU's (utilization %, memory used MB, memory total MB), or None if nvidia-smi can't say."""
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=30)
            util, used, total = (float(x) for x in r.stdout.splitlines()[0].split(","))
            return util, used, total
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None

    def _loop(self) -> None:
        while True:
            s = self.sample()
            if s:
                self.samples.append((time.time(), *s, self.running or time.time() < self.ours_until))
            time.sleep(GPU_EVERY)

    def ours(self, running: bool) -> None:
        """Mark a job of this script's as running, or ended (its use lingers in the samples for a little)."""
        self.running = running
        if not running:
            self.ours_until = time.time() + 2 * GPU_EVERY

    def others(self, seconds: float) -> list[float]:
        """Others' utilization in the samples of the last seconds, while no job of this script's ran."""
        cut = time.time() - seconds
        return [s[1] for s in list(self.samples) if s[0] >= cut and not s[4]]

    def free_mb(self) -> float | None:
        """GPU memory free in the last sample without a job of this script's."""
        mine = [s for s in list(self.samples) if not s[4]]
        return mine[-1][3] - mine[-1][2] if mine else None


def mem_available_gb() -> float | None:
    """Memory free for a job (MemAvailable), or None where /proc/meminfo isn't there."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024 / GB
    except (OSError, ValueError, IndexError):
        pass
    return None


class Usage:
    """This machine's time for the server, kept as it goes: each job (its task, how long, how it ended) and each change
    between working, waiting for a climb and held back, as JSON lines in usage.jsonl in its folder; and once an hour, a
    line of how the hour went, there and in its log."""

    NAMES = {"working": "working", "idle": "waiting for climbs", "held": "held back"}

    def __init__(self, folder: Path):
        self.path = folder / "usage.jsonl"
        self.state, self.since, self.hour_start = None, time.time(), time.time()
        self.spent: dict[str, float] = {}
        self.jobs: list[str] = []

    def write(self, rec: dict) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > 5 * 2**20:     # the newest 5-10 MB kept
                self.path.replace(self.path.with_name(self.path.name + ".1"))
            with open(self.path, "a") as f:
                f.write(json.dumps({"t": round(time.time(), 1), **rec}) + "\n")
        except OSError:
            pass

    def to(self, state: str, why: str | None = None) -> None:
        """Now in this state ("working", "idle" or "held")."""
        now = time.time()
        if self.state:
            self.spent[self.state] = self.spent.get(self.state, 0.0) + now - self.since
        if state != self.state:
            self.write({"event": "state", "state": state, **({"why": why} if why else {})})
        self.state, self.since = state, now
        self.tick()

    def job(self, jid: str, task: str, seconds: float, outcome: str) -> None:
        self.write({"event": "job", "id": jid, "task": task, "seconds": round(seconds, 1), "outcome": outcome})
        self.jobs.append(outcome)

    def tick(self) -> None:
        """Once an hour has passed since the last: say how it went, and start the next."""
        now = time.time()
        if now - self.hour_start < 3600:
            return
        if self.state:
            self.spent[self.state] = self.spent.get(self.state, 0.0) + now - self.since
            self.since = now
        minutes = {k: round(v / 60) for k, v in self.spent.items()}
        ended = {o: self.jobs.count(o) for o in dict.fromkeys(self.jobs)}
        self.write({"event": "hour", "minutes": minutes, "jobs": ended})
        log(f"the last hour: " + ", ".join(f"{self.NAMES.get(k, k)} {v} min" for k, v in minutes.items())
            + (f"; jobs: {', '.join(f'{n} {o}' for o, n in ended.items())}" if ended else "; no jobs"))
        self.spent, self.jobs, self.hour_start = {}, [], now


class Gone(Exception):
    """The server no longer wants this job: it was stopped there, or the server restarted."""


class NotHere(Exception):
    """This machine can't do a job now, through no fault of the job's: it goes back for another machine."""


# -- updating itself ------------------------------------------------------------------------------------------------
auto_update = True          # --no-auto-update turns it off


def tracked_by_git(me: Path) -> bool:
    """Whether this file is the one a git checkout of BoulderMesh has (a developer's, which git pull updates): one git
    tracks there. A copy downloaded into another repository's folder isn't; without git to ask, the repository's layout
    says (worker/ in a checkout)."""
    if not any((d / ".git").exists() for d in me.parents):
        return False
    try:
        return subprocess.run(["git", "-C", str(me.parent), "ls-files", "--error-unmatch", me.name],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return me.parent.name == "worker" and (me.parent.parent / ".git").exists()


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
    and then doesn't return. (A token it won't take is forgotten, and another registered: Worker.renew().)"""
    if keep and (getattr(e, "answer", None) or {}).get("update"):
        update_itself(server, e.answer["update"], keep)
    log(f"{server} says this script is too old. Download it again: curl -O {server}{SCRIPT_PATH}")
    return SystemExit(TURNED_AWAY)


class Server:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def call(self, path: str, body: dict | bytes | None = None, raw: bool = False, tries: int = 8):
        """JSON from the server (or bytes, if raw), trying again after a dropped connection or its being away. An HTTP
        error's JSON is raised as HTTPError with .answer set; Gone for a job the server no longer has."""
        data, kind = (body, "application/octet-stream") if isinstance(body, bytes) else \
            (json.dumps(body).encode() if body is not None else None, "application/json")
        for attempt in range(tries):
            req = urllib.request.Request(self.url + path, data=data, method="POST" if data is not None else "GET",
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": kind,
                                                  "User-Agent": f"bouldermesh-worker/{VERSION}"})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    out = r.read()
                    return out if raw else json.loads(out or b"{}")
            except urllib.error.HTTPError as e:
                try:
                    e.answer = json.loads(e.read() or b"{}")
                except ValueError:
                    e.answer = {}
                if e.code == 409 and e.answer.get("gone"):
                    raise Gone(e.answer.get("error")) from None
                if e.code in (502, 503, 504, 520, 521, 522, 523, 524) and attempt < tries - 1:   # the server, or the way there, is away
                    time.sleep(min(30 * (attempt + 1), 120))
                    continue
                raise
            except (urllib.error.URLError, OSError) as e:
                if attempt == tries - 1:
                    raise
                log(f"can't reach {self.url} ({e}); trying again")
                time.sleep(min(30 * (attempt + 1), 120))
        raise RuntimeError("unreachable")


def register(server: str, name: str | None, patient: bool = False) -> tuple[str, str]:
    """Make a token, register it with the server under name (asked for, if not given and there's someone to ask), and
    return it and the name given. Exits saying why if the server won't have it; or, if patient, waits half an hour and
    tries again while the server is out of reach or has too many registrations (429, 503)."""
    if name is None and os.isatty(0):
        name = input("Your name or a nickname, shown to whoever runs the server (optional; Enter to leave it out): ")
    name = (name or "").strip()
    token = "bmw_" + secrets.token_urlsafe(32)
    while True:
        try:
            r = Server(server, token).call("/worker/api/register", {"name": name, "version": VERSION,
                                                                     "client": f"script {VERSION} on {socket.gethostname()}"}, tries=1)
            break
        except urllib.error.HTTPError as e:
            if not (patient and e.code in (429, 503)):
                raise SystemExit(f"the server wouldn't register this machine: {e.answer.get('error') or e}") from None
            log(f"the server won't register a new token now ({e.answer.get('error') or e}): trying again in 30 min")
        except (urllib.error.URLError, OSError) as e:
            if not patient:
                raise SystemExit(f"can't reach {server}: {e}") from None
            log(f"can't reach {server} to register a new token ({e}): trying again in 30 min")
        time.sleep(1800)
    log(f"registered with {server} as {r.get('name')!r}: it gives this machine climbs once whoever runs it approves it"
        + (" (meanwhile it may give it some, whose meshes wait for that approval)" if r.get("may_work") else ""))
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


class Worker:
    def __init__(self, args):
        self.args = args
        self.home = Path(args.data or args.home).expanduser().resolve()    # jobs/, and data/: the models jobs download
        self.settings = Path(args.home).expanduser().resolve()              # config.json, and pulled.json
        self.server = Server(args.server, args.token)
        self.gpu = self.gpu_args(args.gpu)
        self.container: str | None = None
        self.watch = GpuWatch() if self.gpu else None
        self.gpu_busy = False
        (self.home / "data").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def gpu_args(choice: str) -> list[str]:
        if choice == "none":
            return []
        if choice == "cdi":
            return ["--device", "nvidia.com/gpu=all"]
        if choice == "all" or shutil.which("nvidia-smi") and subprocess.run(["nvidia-smi", "-L"], capture_output=True).returncode == 0:
            return ["--gpus", "all"]
        return []

    # -- others using this machine -----------------------------------------------------------------------------------
    def held_back(self) -> str | None:
        """Why this machine shouldn't take a climb now, or None: too little memory free, or others keeping its GPU busy
        (a median over BUSY_WINDOW, and once over, quiet again over QUIET_WINDOW, so as not to flip back and forth). The
        GPU memory a climb needs, the server checks against what's free (gpu_free_mb)."""
        need = self.args.need_ram_gb if self.args.need_ram_gb is not None else NEED_RAM_GB
        free = mem_available_gb()
        if need and free is not None and free < need:
            return f"{free:.0f} GB of memory free, and a climb may need {need:g} GB"
        limit = self.args.busy_gpu_util if self.args.busy_gpu_util is not None else BUSY_GPU_UTIL
        if self.watch and limit:
            window = QUIET_WINDOW if self.gpu_busy else BUSY_WINDOW
            recent = self.watch.others(window)
            if len(recent) >= 3:
                self.gpu_busy = statistics.median(recent) > limit
            if self.gpu_busy:
                span = f"{window / 60:.0f} min" if window >= 60 else f"{window:.0f} s"
                return (f"others keep its GPU busy ({statistics.median(recent):.0f}% over the last {span}, over {limit:g}%)"
                        if recent else "others kept its GPU busy")
        return None

    # -- room on disk --------------------------------------------------------------------------------------------------
    def data_room(self) -> str | None:
        """Why there isn't room under --data (or --home) for CLIMBS_AHEAD climbs and the models still to download,
        or None if there is."""
        have = tree_size(self.home / "data" / "cache" / "models") / GB
        need = self.args.need_gb if self.args.need_gb is not None else CLIMBS_AHEAD * CLIMB_GB + max(0.0, MODELS_GB - have)
        free = free_gb(self.home)
        if free >= need:
            return None
        why = (f"{self.home} has {free:.1f} GB free, and jobs need about {need:.1f} GB there (room for {CLIMBS_AHEAD} climbs at "
               f"{CLIMB_GB:g} GB" + (f", and {MODELS_GB - have:.1f} GB of models to download the first time" if have < MODELS_GB else "") + ")")
        if not self.args.data:
            why += (". Give --data DIR to put jobs and models on a disk with more room (it's remembered; this script's settings "
                    "stay where they are)")
        return why

    def image_room(self, image: str) -> str | None:
        """Why Docker's storage hasn't room to pull image, or None if it has (or can't be checked)."""
        size = image_download(image)
        need = size * IMAGE_TIMES / GB if size else IMAGE_GUESS_GB
        for where in docker_storage():
            free = free_gb(where)
            if free < need:
                return (f"Docker keeps images in {where}, which has {free:.1f} GB free, and {image} needs about {need:.1f} GB "
                        f"({'its layers, kept and unpacked' if size else 'a guess, as the registry did not say its size'}). Make room "
                        "there (docker image prune deletes images nothing uses), or move Docker's storage (data-root in "
                        "/etc/docker/daemon.json)")
        return None

    def prune_pulled(self, keep: str) -> None:
        """Delete the images this script pulled before, now that it has pulled keep, unless a container still uses one."""
        state = self.settings / "pulled.json"
        try:
            pulled = json.loads(state.read_text())
        except (FileNotFoundError, ValueError):
            pulled = []
        left = [keep]
        for old in pulled:
            if old != keep and subprocess.run(["docker", "image", "rm", old], capture_output=True).returncode:
                left.append(old)    # in use, or gone already: tried again next time
        state.write_text(json.dumps(left))

    def client(self) -> dict:
        docker = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True)
        if docker.returncode:
            raise SystemExit(f"can't use Docker: {(docker.stderr or docker.stdout).strip()[-300:]}")
        blocked = update_blocked(self.server.url)
        return {"version": VERSION, "client": f"{socket.gethostname()}: docker {docker.stdout.strip()}, script {VERSION}"
                + (f"; won't update itself: {blocked.split(',')[0]}" if blocked else ""),
                "gpu": bool(self.gpu), "cpus": self.args.cpus}

    # -- one job --------------------------------------------------------------------------------------------------------
    def do(self, job: dict) -> None:
        jid, image, task = str(job.get("id") or ""), str(job.get("image") or ""), str(job.get("task") or "reconstruct")
        if not JOB_ID.fullmatch(jid) or not IMAGE.fullmatch(image) or task not in TASKS:
            raise RuntimeError(f"the server sent a job this script can't run: {job}")
        api = f"/worker/api/jobs/{jid}"
        if self.args.image_prefix and not image.startswith(self.args.image_prefix):
            self.server.call(f"{api}/done", {"status": "released"})
            raise SystemExit(f"the server asked for {image}, and this machine only runs images starting {self.args.image_prefix}")
        short = self.data_room() or (self.image_room(image) if not have_image(image) else None)
        if short:
            self.server.call(f"{api}/done", {"status": "released"})
            raise SystemExit(f"not enough room for the job, so it's given back: {short}")
        work = self.home / "jobs" / jid
        shutil.rmtree(work, ignore_errors=True)
        (work / "job").mkdir(parents=True)
        try:
            self.fetch(api, int(job["size"]), work / "input.tar")
            extract(work / "input.tar", work / "job")
            (work / "input.tar").unlink()
            if not have_image(image):
                code, last = self.stream(api, ["docker", "pull", "-q", image], f"remote: pulling {image}")
                if code:
                    raise NotHere(f"couldn't pull {image}: {last}")
                self.prune_pulled(image)
            self.container = f"bmw-{jid}"
            cmd = ["docker", "run", "--rm", "--init", "--name", self.container, "--user", f"{os.getuid()}:{os.getgid()}",
                   "-e", "HOME=/tmp", "-e", "DATA_DIR=/data", "-v", f"{self.home / 'data'}:/data", "-v", f"{work / 'job'}:/job",
                   "--security-opt", "no-new-privileges", "--cap-drop", "ALL", *self.gpu,
                   *(["--cpus", str(self.args.cpus)] if self.args.cpus else []), *self.args.docker_arg, image,
                   "nice", "-n", str(self.args.nice), "python3", "/app/backend/remote_job.py", "/job",
                   *(["--task", task] if task != "reconstruct" else []), *(["--internet"] if job.get("internet") else []),
                   *(["--gpu"] if self.gpu else [])]
            code, last = self.stream(api, cmd, f"remote: running on {socket.gethostname()}" + (" with its GPU" if self.gpu else ""))
            self.container = None
            if code:
                self.server.call(f"{api}/done", {"status": "failed", "error": last or f"the job exited with {code}"})
                log(f"job {jid} failed: {last}")
                self.outcome = "failed"
                return
            with tarfile.open(work / "output.tar", "w") as tar:
                for part in ("out", "masks") if task == "reconstruct" else ("out",):
                    if (work / "job" / part).is_dir():
                        tar.add(work / "job" / part, arcname=part)
            self.send(api, work / "output.tar")
            self.server.call(f"{api}/done", {"status": "ok"})
            log(f"job {jid} done")
            self.outcome = "done"
        except Gone as e:
            log(f"job {jid} is no longer wanted ({e}): leaving it")
            self.outcome = "gone"
        except (KeyboardInterrupt, NotHere, OSError):     # this machine's trouble, not the job's: someone else's to do
            self.outcome = "given back"
            try:
                self.server.call(f"{api}/done", {"status": "released"}, tries=1)
            except Exception:  # noqa: S110 - giving it up anyway
                pass
            raise
        except (RuntimeError, tarfile.TarError, ValueError) as e:
            self.server.call(f"{api}/done", {"status": "failed", "error": f"on {socket.gethostname()}: {e}"})
            log(f"job {jid} failed here: {e}")
            self.outcome = "failed"
        finally:
            self.stop_container()
            shutil.rmtree(work, ignore_errors=True)

    def stop_container(self) -> None:
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
            self.container = None

    def fetch(self, api: str, size: int, dest: Path) -> None:
        """The job's photos and masks, in parts, carrying on from what's here after a dropped connection."""
        log(f"fetching {size / 1e6:.0f} MB of photos")
        with open(dest, "ab") as f:
            while f.tell() < size:
                part = self.server.call(f"{api}/input?offset={f.tell()}", raw=True)
                if not part:
                    raise RuntimeError("the server sent less than it said")
                f.write(part)

    def send(self, api: str, path: Path) -> None:
        """The results in parts, carrying on from what the server has after a dropped connection."""
        size, part = path.stat().st_size, 64 * 2**20
        log(f"sending {size / 1e6:.0f} MB of results")
        sent = self.server.call(f"{api}/output").get("received", 0)
        with open(path, "rb") as f:
            while sent < size:
                f.seek(sent)
                try:
                    sent = self.server.call(f"{api}/output?offset={sent}&size={size}", f.read(part))["received"]
                except urllib.error.HTTPError as e:
                    if e.code != 409 or "received" not in e.answer:
                        raise
                    sent = e.answer["received"]

    def stream(self, api: str, cmd: list[str], first: str) -> tuple[int, str]:
        """Run a command, sending what it prints to the server every few seconds (and saying it's alive even when it
        prints nothing). Stops it if the server no longer wants the job.

        Returns:
            Its exit code, and the last line it printed that wasn't progress.
        """
        lines: queue.Queue = queue.Queue()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace")

        def read():
            for line in proc.stdout:
                lines.put(line.rstrip("\n"))
            lines.put(None)

        threading.Thread(target=read, daemon=True).start()
        pending, last, sent, ended = [first], "", 0.0, False
        try:
            while not ended:
                try:
                    line = lines.get(timeout=1)
                    if line is None:
                        ended = True
                    else:
                        pending.append(line)
                        last = line if line and not line.startswith("@@") else last
                except queue.Empty:
                    pass
                if ended or len(pending) >= 500 or time.time() - sent >= SEND_EVERY:
                    self.server.call(f"{api}/log", {"lines": pending})
                    pending, sent = [], time.time()
            return proc.wait(), last
        except BaseException:
            self.stop_container()
            proc.kill()
            raise

    # -- the loop -------------------------------------------------------------------------------------------------------
    def renew(self, e: urllib.error.HTTPError) -> None:
        """The server no longer takes this machine's token (401, 403): forget it, warn, and carry on as with none,
        registering a new one."""
        said = (getattr(e, "answer", None) or {}).get("error") or f"HTTP {e.code}"
        log(f"warning: {self.server.url} no longer takes this machine's token ({said}). Forgot it, and registering a new one, "
            "which waits for approval like any other.")
        forget_token(self.args.home)
        conf = load_conf(self.args.home)
        token, name = register(self.server.url, conf["name"] if "name" in conf else self.args.name, patient=True)
        save_conf(self.args.home, {**load_conf(self.args.home), "token": token, "name": name})
        self.server.token = token

    def run(self) -> None:
        client = self.client()
        short = self.data_room()
        if self.args.check:
            for attempt in range(2):
                try:
                    r = self.server.call("/worker/api/work", {**client, "check": True}, tries=2)
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403) and not attempt:
                        self.renew(e)
                        continue
                    if e.code == 426:
                        raise turned_away(self.server.url, e) from None
                    raise SystemExit(f"the server said: {(getattr(e, 'answer', None) or {}).get('error') or e}") from None
                except (urllib.error.URLError, OSError) as e:
                    raise SystemExit(f"can't reach {self.server.url}: {e}") from None
            log(f"{self.server.url} knows this token as {r.get('name')!r}"
                + (": approved" if r.get("status") != "provisional" else
                   ", waiting for approval" + ("; it may get climbs meanwhile" if r.get("may_work") else "; no climbs until then")))
            if short:
                raise SystemExit(f"but there isn't room for jobs: {short}")
            log(f"room for jobs in {self.home}: {free_gb(self.home):.0f} GB free; Docker's images: "
                + ", ".join(f"{w} {free_gb(w):.0f} GB free" for w in docker_storage()))
            return
        if short:
            raise SystemExit(f"not starting: {short}")
        blocked = update_blocked(self.server.url)
        log(f"working for {self.server.url}: {client['client']}" + (", with the GPU" if self.gpu else ", without a GPU")
            + (f", at most {self.args.cpus} cores" if self.args.cpus else "")
            + (f"; it doesn't update itself ({blocked})" if blocked else "; it updates itself when the server says to"))
        updates = not blocked and not self.args.once
        usage = Usage(Path(self.args.home).expanduser())
        while True:
            held = self.held_back()
            asking = {**client, "updates": updates, "held": held,
                      "gpu_free_mb": self.watch.free_mb() if self.watch else None}
            try:
                r = self.server.call("/worker/api/work", asking)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    self.renew(e)
                    continue
                if e.code == 426:
                    raise turned_away(self.server.url, e, Path(self.args.home).expanduser() if updates else None) from None
                log(f"the server said: {e.answer.get('error') or e}; asking again in 5 min")
                time.sleep(300)
                continue
            except (urllib.error.URLError, OSError) as e:
                log(f"can't reach {self.server.url} ({e}); asking again in 5 min")
                time.sleep(300)
                continue
            if "update" in r:       # between one job and the next: none is interrupted
                if not update_itself(self.server.url, r["update"], Path(self.args.home).expanduser()):
                    updates = False     # it carries on as it is, and the server gives it climbs again
                continue
            if r.get("newer") and r["newer"] != getattr(self, "newer", None):
                log(f"the server has a newer version of this script ({r['newer']}): curl -O {self.server.url}{SCRIPT_PATH}")
                self.newer = r["newer"]
            usage.to("working" if "job" in r else "held" if r.get("held") else "idle", r.get("why"))
            if "job" in r:
                log(f"job {r['job'].get('id')}: {r['job'].get('size', 0) / 1e6:.0f} MB of photos, in {r['job'].get('image')}")
                began, self.outcome = time.time(), "stopped"
                try:
                    if self.watch:
                        self.watch.ours(True)
                    self.do(r["job"])
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):       # its token stopped working partway through: the job went back
                        self.renew(e)
                        continue
                    if e.code == 426:
                        raise turned_away(self.server.url, e, Path(self.args.home).expanduser() if updates else None) from None
                    log(f"the server said: {(getattr(e, 'answer', None) or {}).get('error') or e}")
                except (NotHere, OSError) as e:
                    log(f"couldn't do it here ({e}): gave it back; asking again in 5 min")
                    if not self.args.once:
                        time.sleep(300)
                finally:
                    if self.watch:
                        self.watch.ours(False)
                    usage.job(str(r["job"].get("id")), str(r["job"].get("task") or "reconstruct"), time.time() - began, self.outcome)
            elif self.args.once:
                log(f"nothing to do{': ' + r['why'] if r.get('why') else ''}")
            else:
                kind = re.sub(r"[\d.]+", "#", r.get("why") or "")     # said once, not again for each new figure
                if r.get("why") and kind != getattr(self, "why", None):
                    log(f"no climbs for now: {r['why']}")
                self.why = kind
                time.sleep(max(5, min(float(r.get("wait") or 15), 300)))
            if self.args.once:
                return


def free_gb(path: Path) -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / GB


def tree_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def have_image(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


def image_download(image: str) -> int | None:
    """How many bytes pulling an image downloads, its layers' compressed sizes for this machine, as its registry says
    (docker manifest inspect); None if it won't say."""
    try:
        r = subprocess.run(["docker", "manifest", "inspect", "-v", image], capture_output=True, text=True, timeout=120)
        found = json.loads(r.stdout) if r.returncode == 0 else None
    except (subprocess.SubprocessError, ValueError):
        return None
    for m in found if isinstance(found, list) else [found] if found else []:
        platform = (m.get("Descriptor") or {}).get("platform") or {}
        layers = (m.get("SchemaV2Manifest") or m.get("OCIManifest") or {}).get("layers") or []
        if layers and platform.get("os", "linux") == "linux" and platform.get("architecture", "amd64") in ("amd64", "x86_64"):
            return sum(int(layer.get("size") or 0) for layer in layers)
    return None


def docker_storage() -> list[Path]:
    """Where Docker keeps images here: its data root, and containerd's, which Docker's containerd image store uses."""
    r = subprocess.run(["docker", "info", "--format", "{{.DockerRootDir}}"], capture_output=True, text=True)
    out = [Path(r.stdout.strip())] if r.returncode == 0 and r.stdout.strip() else []
    out += [Path("/var/lib/containerd")]
    seen, keep = set(), []
    for p in out:
        try:
            dev = os.stat(p).st_dev
        except OSError:
            continue
        if dev not in seen:
            seen.add(dev)
            keep.append(p)
    return keep


def extract(tar_path: Path, dest: Path) -> None:
    """Unpack what the job needs (photos, masks, a mesh, videos, its task.json): only files and folders, under INPUTS."""
    with tarfile.open(tar_path) as tar:
        members = tar.getmembers()
        for t in members:
            parts = Path(t.name).parts
            if not (t.isfile() or t.isdir()) or not parts or parts[0] not in INPUTS or ".." in parts or Path(t.name).is_absolute():
                raise RuntimeError(f"the job holds something it shouldn't: {t.name!r}")
        tar.extractall(dest, members=members, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--server", help="the BoulderMesh site, such as https://boulders.example.com (remembered)")
    ap.add_argument("--token", help="a token from the server's admin page (remembered); without one, it makes its own the "
                    "first time, and registers it")
    ap.add_argument("--name", help="your name or a nickname for whoever runs the server, when registering (asked for if not given)")
    ap.add_argument("--forget-token", action="store_true", help="delete the remembered token, and stop: the next run makes and "
                    "registers a new one")
    ap.add_argument("--register", action="store_true", help="make a new token and register it, in place of the one remembered")
    ap.add_argument("--cpus", type=float, help="at most this many cores for a job (docker run --cpus; remembered)")
    ap.add_argument("--gpu", choices=("auto", "all", "cdi", "none"), help="give jobs the GPU: auto (if nvidia-smi finds one, "
                    "the default), all (--gpus all), cdi (the CDI device nvidia.com/gpu=all) or none (remembered)")
    ap.add_argument("--nice", type=int, default=10, help="run jobs at this niceness (10)")
    ap.add_argument("--busy-gpu-util", type=float, help=f"take no climbs while others keep the GPU busier than this (%%, as a "
                    f"median over {BUSY_WINDOW / 60:g} min; {BUSY_GPU_UTIL} by default, 0 never; remembered)")
    ap.add_argument("--need-ram-gb", type=float, help=f"take no climbs while less memory than this is free (GB; {NEED_RAM_GB} by "
                    "default, 0 never; remembered)")
    ap.add_argument("--docker-arg", action="append", default=[], help="another option for docker run, such as --memory=64g "
                    "(repeat it for more)")
    ap.add_argument("--image-prefix", help="run only images whose names start with this, such as ghcr.io/you/bouldermesh: "
                    "(remembered)")
    ap.add_argument("--home", default=os.environ.get("BOULDERMESH_WORKER_HOME", "~/.bouldermesh-worker"),
                    help="where it keeps its settings, and unless --data says otherwise, its jobs and the models they download")
    ap.add_argument("--data", help="where jobs' photos and results, and the models they download, go: a few GB, such as on a "
                    "bigger disk (remembered; --home if not given)")
    ap.add_argument("--need-gb", type=float, help=f"the room jobs need under --data before it takes one, in GB (by default "
                    f"{CLIMBS_AHEAD * CLIMB_GB:g}, and {MODELS_GB:g} more until the models are downloaded)")
    ap.add_argument("--check", action="store_true", help="only check the server knows the token, and remember it, and that "
                    "there's room for jobs")
    ap.add_argument("--once", action="store_true", help="ask for one job, do it if there's one, and stop")
    ap.add_argument("--no-auto-update", action="store_true", help="don't replace this script with a newer one when the server "
                    "has one (it says so instead)")
    args = ap.parse_args()
    global auto_update
    auto_update = not args.no_auto_update
    home = Path(args.home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    conf = load_conf(home)
    if args.forget_token:
        log(f"forgot the token remembered for {conf.get('server')}" if forget_token(home) else "no token was remembered")
        return
    if args.register and args.token:
        ap.error("--register makes a new token, so not with --token")
    if args.server and args.server != conf.get("server") and not args.token or args.register:
        conf.pop("token", None)     # a token is the server's it was registered with
    for k in ("server", "token", "cpus", "gpu", "image_prefix", "data", "busy_gpu_util", "need_ram_gb"):
        if getattr(args, k) is None:
            setattr(args, k, conf.get(k))
        else:
            conf[k] = getattr(args, k)
    args.gpu = args.gpu or "auto"
    if args.data:
        args.data = conf["data"] = str(Path(args.data).expanduser().resolve())
        Path(args.data).mkdir(parents=True, exist_ok=True)
        os.chmod(args.data, 0o700)
    if not args.server:
        ap.error("--server is needed the first time")
    if not args.token:
        args.token, conf["name"] = register(args.server, args.name if args.name is not None else conf.get("name"))
        conf["token"] = args.token
    save_conf(home, conf)
    signal.signal(signal.SIGTERM, _stop)    # as Ctrl-C: the job goes back to the server
    try:
        Worker(args).run()
    except KeyboardInterrupt:
        log("stopped")
    finally:
        if not args.check:      # gone, as whoever runs the server is told (not when it updates itself: that carries on)
            try:
                Server(args.server, args.token).call("/worker/api/bye", {}, tries=1)
            except Exception:  # noqa: S110 - stopping anyway
                pass


def _stop(*_) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    main()
