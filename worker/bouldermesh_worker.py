#!/usr/bin/env python3
"""Lend this machine to a BoulderMesh server for reconstructions, when the server can't reach it.

BoulderMesh makes 3D models of boulders from photos. A small server can't reconstruct them itself, and one machine
that can may be behind a firewall the server can't get through. This script asks the server for a climb to mesh, fetches
its photos over HTTPS, runs the reconstruction in the server's own Docker image (with the GPU, if there is one), sends
its log as it goes, and then the mesh. Run it with a token from the server's admin page:

    python3 bouldermesh_worker.py --server https://boulders.example.com --token bmw_...

Python 3.9 or newer and Docker (with NVIDIA's container toolkit, for a GPU), which whoever runs it may use. The server
and token are remembered (in ~/.bouldermesh-worker), so after the first time it's only `python3 bouldermesh_worker.py`.
Stop it with Ctrl-C: the climb it was on goes back to the server, for this or another machine.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
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
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}")


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


class Gone(Exception):
    """The server no longer wants this job: it was stopped there, or the server restarted."""


class NotHere(Exception):
    """This machine can't do a job now, through no fault of the job's: it goes back for another machine."""


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


class Worker:
    def __init__(self, args):
        self.args, self.home = args, Path(args.home).expanduser().resolve()
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
        work = self.home / "jobs" / jid
        shutil.rmtree(work, ignore_errors=True)
        (work / "job").mkdir(parents=True)
        try:
            self.fetch(api, int(job["size"]), work / "input.tar")
            extract(work / "input.tar", work / "job")
            (work / "input.tar").unlink()
            if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
                code, last = self.stream(api, ["docker", "pull", "-q", image], f"remote: pulling {image}")
                if code:
                    raise NotHere(f"couldn't pull {image}: {last}")
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
        if self.args.check:
            r = self.server.call("/worker/api/work", {**client, "check": True})
            log(f"{self.server.url} knows this token as {r.get('name')!r}")
            return
        log(f"working for {self.server.url}: {client['client']}" + (", with the GPU" if self.gpu else ", without a GPU")
            + (f", at most {self.args.cpus} cores" if self.args.cpus else ""))
        while True:
            try:
                r = self.server.call("/worker/api/work", client)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 426):
                    raise SystemExit(f"the server said: {e.answer.get('error') or e}") from None
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
                    log(f"the server said: {e.answer.get('error') or e}")
                except (NotHere, OSError) as e:
                    log(f"couldn't do it here ({e}): gave it back; asking again in 5 min")
                    if not self.args.once:
                        time.sleep(300)
            elif self.args.once:
                log("nothing to do")
            else:
                time.sleep(max(5, min(float(r.get("wait") or 15), 300)))
            if self.args.once:
                return


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
    ap.add_argument("--token", help="this machine's token, from the server's admin page (remembered)")
    ap.add_argument("--cpus", type=float, help="at most this many cores for a job (docker run --cpus; remembered)")
    ap.add_argument("--gpu", choices=("auto", "all", "cdi", "none"), help="give jobs the GPU: auto (if nvidia-smi finds one, "
                    "the default), all (--gpus all), cdi (the CDI device nvidia.com/gpu=all) or none (remembered)")
    ap.add_argument("--nice", type=int, default=10, help="run jobs at this niceness (10)")
    ap.add_argument("--docker-arg", action="append", default=[], help="another option for docker run, such as --memory=64g "
                    "(repeat it for more)")
    ap.add_argument("--image-prefix", help="run only images whose names start with this, such as ghcr.io/you/bouldermesh: "
                    "(remembered)")
    ap.add_argument("--home", default=os.environ.get("BOULDERMESH_WORKER_HOME", "~/.bouldermesh-worker"),
                    help="where it keeps its settings, its jobs, and the models jobs download")
    ap.add_argument("--check", action="store_true", help="only check the server knows the token, and remember it")
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
    for k in ("server", "token", "cpus", "gpu", "image_prefix"):
        if getattr(args, k) is None:
            setattr(args, k, conf.get(k))
        else:
            conf[k] = getattr(args, k)
    args.gpu = args.gpu or "auto"
    if not args.server or not args.token:
        ap.error("--server and --token are needed the first time")
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
