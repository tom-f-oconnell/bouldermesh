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
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = 2
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
TURNED_AWAY = 3             # the exit status when the server won't take this token or script: starting again won't help,
                            # so systemd units leave it stopped (RestartPreventExitStatus)


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


class Gone(Exception):
    """The server no longer wants this job: it was stopped there, or the server restarted."""


class NotHere(Exception):
    """This machine can't do a job now, through no fault of the job's: it goes back for another machine."""


def turned_away(server: str, e: urllib.error.HTTPError) -> SystemExit:
    """Says why the server won't take this machine's token (401, 403) or this script (426), and what to do, rather than
    a traceback; returns the exit to raise."""
    said = (getattr(e, "answer", None) or {}).get("error") or f"HTTP {e.code}"
    if e.code == 401:
        log(f"{server} doesn't know this machine's token. Run this script with --register to make a new one and register "
            "it (it waits for approval), or give it a token from whoever runs the server with --token.")
    elif e.code == 403:
        log(f"{server} won't take this machine's token: {said}. If that's a mistake, ask whoever runs it; --register makes "
            "a new token, which waits for their approval.")
    else:
        log(f"{server} says this script is too old. Download it again: curl -O {server}/worker/bouldermesh_worker.py")
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


def register(server: str, name: str | None) -> str:
    """Make a token, register it with the server under name (asked for, if not given and there's someone to ask), and
    return it; exits saying why if the server won't have it."""
    if name is None and os.isatty(0):
        name = input("Your name or a nickname, shown to whoever runs the server (optional; Enter to leave it out): ")
    token = "bmw_" + secrets.token_urlsafe(32)
    try:
        r = Server(server, token).call("/worker/api/register", {"name": (name or "").strip(), "version": VERSION,
                                                                 "client": f"script {VERSION} on {socket.gethostname()}"}, tries=1)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"the server wouldn't register this machine: {e.answer.get('error') or e}") from None
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f"can't reach {server}: {e}") from None
    log(f"registered with {server} as {r.get('name')!r}: it gives this machine climbs once whoever runs it approves it"
        + (" (meanwhile it may give it some, whose meshes wait for that approval)" if r.get("may_work") else ""))
    return token


class Worker:
    def __init__(self, args):
        self.args = args
        self.home = Path(args.data or args.home).expanduser().resolve()    # jobs/, and data/: the models jobs download
        self.settings = Path(args.home).expanduser().resolve()              # config.json, and pulled.json
        self.server = Server(args.server, args.token)
        self.gpu = self.gpu_args(args.gpu)
        self.container: str | None = None
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
        return {"version": VERSION, "client": f"{socket.gethostname()}: docker {docker.stdout.strip()}, script {VERSION}",
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
                return
            with tarfile.open(work / "output.tar", "w") as tar:
                for part in ("out", "masks") if task == "reconstruct" else ("out",):
                    if (work / "job" / part).is_dir():
                        tar.add(work / "job" / part, arcname=part)
            self.send(api, work / "output.tar")
            self.server.call(f"{api}/done", {"status": "ok"})
            log(f"job {jid} done")
        except Gone as e:
            log(f"job {jid} is no longer wanted ({e}): leaving it")
        except (KeyboardInterrupt, NotHere, OSError):     # this machine's trouble, not the job's: someone else's to do
            try:
                self.server.call(f"{api}/done", {"status": "released"}, tries=1)
            except Exception:  # noqa: S110 - giving it up anyway
                pass
            raise
        except (RuntimeError, tarfile.TarError, ValueError) as e:
            self.server.call(f"{api}/done", {"status": "failed", "error": f"on {socket.gethostname()}: {e}"})
            log(f"job {jid} failed here: {e}")
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
    def run(self) -> None:
        client = self.client()
        short = self.data_room()
        if self.args.check:
            try:
                r = self.server.call("/worker/api/work", {**client, "check": True}, tries=2)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 426):
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
        log(f"working for {self.server.url}: {client['client']}" + (", with the GPU" if self.gpu else ", without a GPU")
            + (f", at most {self.args.cpus} cores" if self.args.cpus else ""))
        while True:
            try:
                r = self.server.call("/worker/api/work", client)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 426):
                    raise turned_away(self.server.url, e) from None
                log(f"the server said: {e.answer.get('error') or e}; asking again in 5 min")
                time.sleep(300)
                continue
            except (urllib.error.URLError, OSError) as e:
                log(f"can't reach {self.server.url} ({e}); asking again in 5 min")
                time.sleep(300)
                continue
            if "job" in r:
                log(f"job {r['job'].get('id')}: {r['job'].get('size', 0) / 1e6:.0f} MB of photos, in {r['job'].get('image')}")
                try:
                    self.do(r["job"])
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403, 426):       # its token stopped working partway through: the job went back
                        raise turned_away(self.server.url, e) from None
                    log(f"the server said: {(getattr(e, 'answer', None) or {}).get('error') or e}")
                except (NotHere, OSError) as e:
                    log(f"couldn't do it here ({e}): gave it back; asking again in 5 min")
                    if not self.args.once:
                        time.sleep(300)
            elif self.args.once:
                log(f"nothing to do{': ' + r['why'] if r.get('why') else ''}")
            else:
                if r.get("why") and r["why"] != getattr(self, "why", None):
                    log(f"no climbs for now: {r['why']}")
                self.why = r.get("why")
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
    ap.add_argument("--register", action="store_true", help="make a new token and register it, in place of the one remembered "
                    "(for one the server no longer takes)")
    ap.add_argument("--cpus", type=float, help="at most this many cores for a job (docker run --cpus; remembered)")
    ap.add_argument("--gpu", choices=("auto", "all", "cdi", "none"), help="give jobs the GPU: auto (if nvidia-smi finds one, "
                    "the default), all (--gpus all), cdi (the CDI device nvidia.com/gpu=all) or none (remembered)")
    ap.add_argument("--nice", type=int, default=10, help="run jobs at this niceness (10)")
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
    args = ap.parse_args()
    home = Path(args.home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    conf_file = home / "config.json"
    try:
        conf = json.loads(conf_file.read_text())
    except (FileNotFoundError, ValueError):
        conf = {}
    if args.register and args.token:
        ap.error("--register makes a new token, so not with --token")
    if args.server and args.server != conf.get("server") and not args.token or args.register:
        conf.pop("token", None)     # a token is the server's it was registered with
    for k in ("server", "token", "cpus", "gpu", "image_prefix", "data"):
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
        args.token = conf["token"] = register(args.server, args.name)
    tmp = conf_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(conf))
    os.chmod(tmp, 0o600)
    tmp.replace(conf_file)
    signal.signal(signal.SIGTERM, _stop)    # as Ctrl-C: the job goes back to the server
    try:
        Worker(args).run()
    except KeyboardInterrupt:
        log("stopped")


def _stop(*_) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    main()
