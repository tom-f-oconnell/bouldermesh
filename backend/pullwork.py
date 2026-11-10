"""Machines that connect to this server for work: reconstructions on machines the server can't reach.

compute.py reaches its machines over SSH, so they must be reachable from the server. A machine behind a firewall or
NAT, such as a lab's workstation reached only through the lab's VPN, isn't. It runs worker/bouldermesh_worker.py
instead, with a token from the admin page, and asks this server for work over HTTPS: it fetches a climb's photos and
people masks, runs the same job on them in the same Docker image as an SSH machine would (remote_job.py), sends its log
as it goes, and then the mesh, its cameras and the masks. The climb's worker thread here waits for that as it would for
an SSH machine, and a climb no machine is there to take, or whose machine goes quiet, waits (ComputeUnavailable).

Files go in parts, under Cloudflare's 100 MB a request, and carry on after a dropped connection. Like volunteers.py,
it's served on a port of its own (WORKER_PORT) that answers only /worker/..., for the proxy in front to pass on.
Machines and their tokens (hashed) are kept in a database of their own. Jobs are kept only in memory: a restart here
ends them anyway, the climbs carry on after it as new jobs, and a machine still running an old one is told it's gone.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import compute
from volunteers import OffsetError, _client_ip

PART_MAX = 64 * 2**20       # the largest part of a file either way, under Cloudflare's 100 MB a request
MAX_OUTPUT = 4 * 2**30      # what a job may send back: a mesh, its textures, cameras and masks
CONNECTED = 120             # seconds since a machine last asked or reported for it to count as there
QUIET = 15 * 60             # a job whose machine hasn't been heard from for this long is lost
POLL = 15                   # seconds a machine with nothing to do waits before asking again
SEND_EVERY = 5              # seconds between a running job's reports, which are also how it's known to be alive
MAX_LINES = 5000            # lines in one report
CLIENT_VERSION = 1          # the oldest script that speaks to this
SCRIPT = Path(__file__).resolve().parents[1] / "worker" / "bouldermesh_worker.py"
TOP = ("out", "masks")      # what a job's results may hold


@dataclass
class Job:
    id: str
    label: str
    input: Path
    size: int
    internet: bool
    image: str
    created: float = field(default_factory=time.time)
    state: str = "queued"           # queued | running | done | failed
    machine: str | None = None
    machine_name: str | None = None
    taken: float | None = None
    touched: float = 0.0            # when its machine last fetched, reported or sent
    lines: list[str] = field(default_factory=list)   # what its machine sent that the waiting thread hasn't followed yet
    out_size: int | None = None
    error: str | None = None
    task: str = "reconstruct"       # or one of remote_job.py's other tasks: "beta", "scale"
    version: int = 1                # the oldest script that does it

    @property
    def output(self) -> Path:
        return self.input.with_name(f"{self.id}.out.tar")


class Board:
    """The machines that connect here, and the reconstructions waiting for them.

    Args:
        db_path: its database of machines.
        files: where jobs' photos wait to be fetched and their results come in.
        image: the Docker image a job runs, asked for each job (the admin page's, or this server's own build).
        jobctl: the waiting thread's job control, to stop waiting when its climb is stopped.
    """

    def __init__(self, db_path: Path, files: Path, image: Callable[[], str], jobctl):
        self.db_path, self.files, self.image, self.jobctl = db_path, files, image, jobctl
        self.jobs: dict[str, Job] = {}
        self.cond = threading.Condition(threading.RLock())
        self.seen: dict[str, float] = {}
        shutil.rmtree(files, ignore_errors=True)    # jobs from before a restart, which ended with it
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS machines(id TEXT PRIMARY KEY, name TEXT, token_hash TEXT UNIQUE, created REAL,
                  revoked REAL, last_seen REAL, last_ip TEXT, client TEXT, gpu INTEGER, cpus REAL, done INTEGER DEFAULT 0,
                  failed INTEGER DEFAULT 0);
            """)

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def q(self, sql: str, *args, one: bool = False) -> list[dict] | dict | None:
        with self.db() as con:
            rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        return (rows[0] if rows else None) if one else rows

    # -- machines -------------------------------------------------------------------------------------------------
    def add(self, name: str) -> dict:
        """A new machine: {id, name, token}. The token is shown this once; only its hash is kept."""
        name = (name or "").strip()[:80]
        if not name:
            raise ValueError("give the machine a name")
        mid, token = secrets.token_hex(4), "bmw_" + secrets.token_urlsafe(32)
        self.q("INSERT INTO machines(id,name,token_hash,created) VALUES(?,?,?,?)", mid, name,
               hashlib.sha256(token.encode()).hexdigest(), time.time())
        return {"id": mid, "name": name, "token": token}

    def revoke(self, mid: str) -> None:
        """Its token stops working at once, and what it was doing goes to another machine."""
        self.q("UPDATE machines SET revoked=? WHERE id=?", time.time(), mid)
        with self.cond:
            self.seen.pop(mid, None)
            for j in self.jobs.values():
                if j.machine == mid and j.state == "running":
                    self._requeue(j, f"remote: {j.machine_name} was revoked: waiting for another machine")

    def machine(self, token: str) -> dict | None:
        return self.q("SELECT * FROM machines WHERE token_hash=?", hashlib.sha256((token or "").encode()).hexdigest(), one=True)

    def connected(self) -> bool:
        """Whether a machine has asked for work or reported lately."""
        cut = time.time() - CONNECTED
        with self.cond:
            return any(t > cut for t in self.seen.values())

    def _touch(self, m: dict, ip: str | None = None, client: dict | None = None) -> None:
        now = time.time()
        with self.cond:
            self.seen[m["id"]] = now
        if client is not None:
            self.q("UPDATE machines SET last_seen=?, last_ip=?, client=?, gpu=?, cpus=? WHERE id=?", now, ip,
                   str(client.get("client") or "")[:120], int(bool(client.get("gpu"))), _number(client.get("cpus")), m["id"])
        else:
            self.q("UPDATE machines SET last_seen=? WHERE id=?", now, m["id"])

    # -- the machine's side ---------------------------------------------------------------------------------------
    def work(self, m: dict, ip: str, client: dict) -> dict:
        """What a machine asking for work is to do: {"job": ...}, the job it had if it lost track of it (it starts
        again), or the oldest waiting; or {"wait": seconds}. With "check" in client, only says whose token it is."""
        self._touch(m, ip, client)
        if client.get("check"):
            return {"name": m["name"], "wait": POLL}
        now = time.time()
        with self.cond:
            mine = next((j for j in self.jobs.values() if j.machine == m["id"] and j.state == "running"), None)
            if mine:
                mine.lines.append(f"remote: {m['name']} started the job again")
                mine.out_size = None
                mine.output.unlink(missing_ok=True)
            else:
                version = int(_number(client.get("version")) or 1)
                mine = min((j for j in self.jobs.values() if j.state == "queued" and j.version <= version), key=lambda j: j.created,
                           default=None)
                if not mine:
                    return {"wait": POLL}
                gpu = " (GPU)" if client.get("gpu") else ""
                mine.state, mine.machine, mine.machine_name, mine.taken = "running", m["id"], m["name"], now
                mine.lines.append(f"remote: {m['name']}{gpu} took it after {now - mine.created:.0f} s")
            mine.touched = now
            self.cond.notify_all()
            return {"job": {"id": mine.id, "image": mine.image, "internet": mine.internet, "size": mine.size, "part": PART_MAX,
                            "task": mine.task}}

    def _mine(self, m: dict, job_id: str) -> Job:
        j = self.jobs.get(job_id)
        if not j or j.machine != m["id"] or j.state != "running":
            raise LookupError("that job isn't yours now: it was stopped, or given to another machine")
        return j

    def input(self, m: dict, job_id: str, offset: int) -> bytes:
        """A part of a job's photos and masks (a tar), from offset."""
        with self.cond:
            j = self._mine(m, job_id)
            j.touched = time.time()
        self._touch(m)
        if not 0 <= offset <= j.size:
            raise ValueError(f"offset {offset} is outside the {j.size} bytes")
        try:
            with open(j.input, "rb") as f:
                f.seek(offset)
                return f.read(PART_MAX)
        except FileNotFoundError:   # the climb was stopped meanwhile
            raise LookupError("that job isn't yours now: it was stopped, or given to another machine") from None

    def report(self, m: dict, job_id: str, lines: list) -> dict:
        """Lines a job printed, which the climb's log follows; also how a machine says it's still at it."""
        if not isinstance(lines, list) or len(lines) > MAX_LINES:
            raise ValueError(f"a list of at most {MAX_LINES} lines")
        with self.cond:
            j = self._mine(m, job_id)
            j.lines.extend(str(line)[:2000] for line in lines)
            j.touched = time.time()
            self.cond.notify_all()
        self._touch(m)
        return {"ok": True}

    def received(self, m: dict, job_id: str) -> int:
        with self.cond:
            j = self._mine(m, job_id)
            return j.output.stat().st_size if j.out_size is not None and j.output.exists() else 0

    def part(self, m: dict, job_id: str, offset: int, size: int, body: bytes) -> int:
        """Add a part of a job's results (a tar of out/ and masks/) at offset, of size bytes in all; how much has come.

        Raises:
            LookupError: not this machine's job now.
            ValueError: too big, or more than size.
            OffsetError: not where the file has got to.
        """
        with self.cond:
            j = self._mine(m, job_id)
            if not 0 < size <= MAX_OUTPUT:
                raise ValueError(f"results of up to {MAX_OUTPUT // 2**30} GB")
            if j.out_size is not None and j.out_size != size:
                raise ValueError("that's another file than the one being sent")
            have = j.output.stat().st_size if j.out_size is not None and j.output.exists() else 0
            if offset != have:
                raise OffsetError(have)
            if have + len(body) > size:
                raise ValueError("more than the size it was started with")
            if j.out_size is None:
                j.output.unlink(missing_ok=True)
            j.out_size = size
            with open(j.output, "ab") as out:
                out.write(body)
            j.touched = time.time()
        self._touch(m)
        return have + len(body)

    def finish(self, m: dict, job_id: str, report: dict) -> dict:
        """How a job ended: {"status": "ok"} once its results have all come, "failed" with "error" (its last line),
        or "released" when the machine stopped before finishing, for another machine to take it."""
        status = report.get("status")
        with self.cond:
            j = self._mine(m, job_id)
            if status == "released":
                self._requeue(j, f"remote: {j.machine_name} stopped before finishing: waiting for another machine")
                return {"ok": True}
            if status == "ok":
                if j.out_size is None or not j.output.exists() or j.output.stat().st_size != j.out_size:
                    raise ValueError("the results haven't all come: send the rest first")
                j.state = "done"
            else:
                j.state, j.error = "failed", str(report.get("error") or "failed on the machine")[:500]
            self.cond.notify_all()
        col = "done" if status == "ok" else "failed"
        self.q(f"UPDATE machines SET {col}={col}+1 WHERE id=?", m["id"])
        return {"ok": True}

    def _requeue(self, j: Job, note: str) -> None:
        j.state, j.machine, j.machine_name, j.taken, j.out_size = "queued", None, None, None, None
        j.output.unlink(missing_ok=True)
        j.lines.append(note)
        self.cond.notify_all()

    # -- the climb's side: what compute.RemoteExecutor does, for these machines ---------------------------------------
    def reconstruct(self, images: Path, work: Path, out_dir: Path, log, use_gpu: bool = True, internet: bool = False,
                    tracker=None) -> tuple[Path, bool]:
        """Reconstruct a project's photos on a machine that connects here: what pipeline.reconstruct does, there.

        Args:
            images: the project's photos/, next to its masks/, which go too and come back with new ones.

        Returns:
            The mesh file and whether it's a demo, as pipeline.reconstruct does.

        Raises:
            compute.ComputeUnavailable: no machine has been in touch lately, or the one doing it went quiet.
        """
        project = images.parent
        files = {part: project / part for part in ("photos", "masks") if (project / part).is_dir()}
        result, back, job = self._run("reconstruct", files, {"internet": internet}, project.name, project, log, tracker)
        try:
            safe_extract(job.output, back, TOP)
            mesh = compute.take_results(back, out_dir, project, result)
        finally:
            shutil.rmtree(back, ignore_errors=True)
            job.output.unlink(missing_ok=True)
        return mesh, bool(result.get("demo"))

    def task(self, kind: str, files: dict[str, Path], args: dict, label: str, log, tracker=None) -> tuple[dict, Path]:
        """Run another kind of job (remote_job.py's --task: "beta" or "scale") on a machine that connects here.

        Args:
            files: what to send, by the name it has in the job's folder there (a file, or a folder and all in it).
            args: given to the job as task.json.
            label: what the admin page calls it.

        Returns:
            What the job said it made, and a new folder holding its out/, which is the caller's to delete.

        Raises:
            compute.ComputeUnavailable: no machine has been in touch lately, or the one doing it went quiet.
        """
        self.files.mkdir(parents=True, exist_ok=True)
        result, back, job = self._run(kind, files, args, label, self.files, log, tracker)
        try:
            safe_extract(job.output, back, ("out",))
        except BaseException:
            shutil.rmtree(back, ignore_errors=True)
            raise
        finally:
            job.output.unlink(missing_ok=True)
        return result, back

    def _run(self, kind: str, files: dict[str, Path], args: dict, label: str, near: Path, log, tracker) -> tuple[dict, Path, "Job"]:
        """Hand a job to a machine and follow it until it's done: its result, a new folder in near for its results,
        and the job, whose output (a tar) is the caller's to unpack and delete."""
        from progress import Tracker
        tracker = tracker or Tracker([])
        until = time.time() + 3 * POLL     # as just after a restart here, before machines have asked again
        while not self.connected():
            if time.time() > until:
                raise compute.ComputeUnavailable(f"no machine has asked this server for work in the last {CONNECTED // 60} minutes")
            self.jobctl.check()
            time.sleep(1)
        image = self.image()
        if not image:
            raise compute.ComputeUnavailable("set the Docker image the machines run on the admin page")
        job = self._new_job(kind, files, args, label, image)
        log(f"remote: {job.size / 1e6:.0f} MB ready for the machines that connect here, job {job.id}")
        result, last = None, ""
        try:
            while True:
                with self.cond:
                    if not job.lines and job.state in ("queued", "running"):
                        self.cond.wait(1.0)
                    lines, job.lines = job.lines, []
                    state, touched = job.state, job.touched
                for line in lines:
                    result = compute.follow(line, tracker, log) or result
                    if not line.startswith(("@@", "remote: ")):
                        last = line or last
                self.jobctl.check()
                if state == "queued" and not self.connected():
                    raise compute.ComputeUnavailable(f"no machine has asked this server for work in the last {CONNECTED // 60} minutes")
                if state == "running" and time.time() - touched > QUIET:
                    raise compute.ComputeUnavailable(f"lost {job.machine_name}: not heard from in {QUIET // 60} minutes")
                if state == "failed":
                    raise compute.job_failed(job.error or last)
                if state == "done":
                    break
            if not result:
                raise compute.job_failed(last)
            log(f"remote: {job.machine_name} finished in {(time.time() - job.taken) / 60:.0f} min")
            return result, Path(tempfile.mkdtemp(prefix="bm-remote-", dir=near)), job
        except BaseException:
            job.output.unlink(missing_ok=True)
            raise
        finally:
            with self.cond:     # a machine still on it hears it's gone the next time it reports
                self.jobs.pop(job.id, None)
            job.input.unlink(missing_ok=True)

    def _new_job(self, kind: str, files: dict[str, Path], args: dict, label: str, image: str) -> Job:
        jid = secrets.token_hex(6)
        self.files.mkdir(parents=True, exist_ok=True)
        path = self.files / f"{jid}.in.tar"
        with tarfile.open(path, "w") as tar:
            for name, src in files.items():
                tar.add(src, arcname=name)
            if kind != "reconstruct":
                data = json.dumps(args).encode()
                info = tarfile.TarInfo("task.json")
                info.size, info.mtime = len(data), time.time()
                tar.addfile(info, io.BytesIO(data))
        job = Job(id=jid, label=label, input=path, size=path.stat().st_size, internet=bool(args.get("internet")), image=image,
                  task=kind, version=1 if kind == "reconstruct" else 2)
        with self.cond:
            self.jobs[jid] = job
        return job

    # -- for the admin page ---------------------------------------------------------------------------------------
    def state(self) -> dict:
        now = time.time()
        with self.cond:
            jobs = [{"id": j.id, "project": j.label, "task": j.task, "state": j.state, "machine": j.machine_name, "machine_id": j.machine,
                     "minutes": round((now - (j.taken or j.created)) / 60, 1)} for j in self.jobs.values()]
            seen = dict(self.seen)
        machines = []
        for m in self.q("SELECT * FROM machines ORDER BY created"):
            doing = next((j for j in jobs if j["state"] == "running" and j["machine_id"] == m["id"]), None)
            if m["revoked"]:
                status = "revoked"
            elif seen.get(m["id"], 0) > now - CONNECTED:
                status = "working" if doing else "waiting for work"
            else:
                status = "not connected"
            machines.append({"id": m["id"], "name": m["name"], "status": status, "last_seen": m["last_seen"], "address": m["last_ip"],
                             "client": m["client"], "gpu": bool(m["gpu"]), "cpus": m["cpus"], "done": m["done"], "failed": m["failed"],
                             "job": doing})
        return {"machines": machines, "jobs": jobs, "connected": sum(m["status"] in ("working", "waiting for work") for m in machines)}


def _number(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def safe_extract(tar_path: Path, dest: Path, top: tuple[str, ...]) -> None:
    """Unpack a tar from elsewhere: only files and folders, all under the given top-level folders."""
    with tarfile.open(tar_path) as tar:
        members = tar.getmembers()
        for t in members:
            parts = Path(t.name).parts
            if not (t.isfile() or t.isdir()) or not parts or parts[0] not in top or ".." in parts or Path(t.name).is_absolute():
                raise ValueError(f"the results hold something they shouldn't: {t.name!r}")
        tar.extractall(dest, members=members, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))


# ------------------------------------------------------------------------------------------- serving it
PAGE = """BoulderMesh: machines lent for reconstructions connect here, with a token from the admin page.
The script: {base}/worker/bouldermesh_worker.py
"""


def handler_for(board: Board, log: Callable[[str], None]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BoulderMeshWorkers"
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
            m = board.machine(auth[7:].strip()) if auth.startswith("Bearer ") else None
            if not m:
                self.json({"error": "unknown token: make one on the server's admin page"}, 401)
                return None
            if m["revoked"]:
                self.json({"error": "this token no longer works"}, 403)
                return None
            return m

        def body(self, most: int) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= most:
                raise ValueError(f"a request of at most {most // 2**20} MB")
            return self.rfile.read(n)

        def handle_errors(self, fn) -> None:
            try:
                fn()
            except OffsetError as e:
                self.json({"error": str(e), "received": e.have}, 409)
            except LookupError as e:
                self.json({"error": str(e), "gone": True}, 409)
            except ValueError as e:
                self.json({"error": str(e)}, 400)
            except Exception as e:
                log(f"machines: error on {urlparse(self.path).path}: {type(e).__name__}: {e}")
                self.json({"error": "the server failed at that: try again later"}, 500)

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            base = f"https://{self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or 'this-site'}"
            if u.path in ("/worker", "/worker/"):
                return self.send(200, PAGE.replace("{base}", base).encode(), "text/plain; charset=utf-8")
            if u.path == "/worker/bouldermesh_worker.py" and SCRIPT.exists():
                return self.send(200, SCRIPT.read_bytes(), "text/x-python; charset=utf-8",
                                 {"Content-Disposition": 'attachment; filename="bouldermesh_worker.py"'})
            m = re.fullmatch(r"/worker/api/jobs/([0-9a-f]{12})/(input|output)", u.path)
            if not m:
                return self.json({"error": "not found"}, 404)
            who = self.who()
            if not who:
                return

            def get():
                if m[2] == "input":
                    return self.send(200, board.input(who, m[1], int((qs.get("offset") or ["0"])[0] or 0)), "application/octet-stream")
                self.json({"received": board.received(who, m[1])})

            self.handle_errors(get)

        def do_POST(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            who = self.who()
            if not who:
                return

            def post():
                if u.path == "/worker/api/work":
                    body = json.loads(self.body(2**20) or b"{}")
                    if int(body.get("version") or 0) < CLIENT_VERSION:
                        return self.json({"error": "this script is too old: download it again from /worker/"}, 426)
                    out = board.work(who, _client_ip(self), body)
                    if "job" in out:
                        log(f"machines: {who['name']} took job {out['job']['id']}")
                    return self.json(out)
                m = re.fullmatch(r"/worker/api/jobs/([0-9a-f]{12})/(log|output|done)", u.path)
                if not m:
                    return self.json({"error": "not found"}, 404)
                if m[2] == "log":
                    return self.json(board.report(who, m[1], (json.loads(self.body(8 * 2**20) or b"{}")).get("lines") or []))
                if m[2] == "output":
                    one = lambda k: (qs.get(k) or [""])[0]     # noqa: E731
                    got = board.part(who, m[1], int(one("offset") or -1), int(one("size") or 0), self.body(PART_MAX))
                    return self.json({"received": got})
                report = json.loads(self.body(2**20) or b"{}")
                out = board.finish(who, m[1], report)
                log(f"machines: {who['name']}: job {m[1]} {report.get('status')}" + (f": {report.get('error')}" if report.get("error") else ""))
                return self.json(out)

            self.handle_errors(post)

    return Handler


def serve(board: Board, host: str, port: int, log: Callable[[str], None] = print) -> ThreadingHTTPServer | None:
    """Serve machines on host:port in the background (None if it can't), only /worker/..."""
    try:
        httpd = ThreadingHTTPServer((host, port), handler_for(board, log))
    except OSError as e:
        log(f"machines: NOT served on {host}:{port}: {e}")
        return None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"machines: served on http://{host}:{port}/worker/")
    return httpd
