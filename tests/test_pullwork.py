#!/usr/bin/env python3
"""Machines that connect to the server for work: the board's jobs and machines, and the whole way through, from a climb
waiting for a machine to its mesh coming back, with worker/bouldermesh_worker.py run against the board's port and a
stand-in for docker that runs remote_job.py here. Also a failing job, a stopped climb, a lost machine, a machine giving
a job back, and results that try to write outside their folder. Offline; needs Pillow for the photos.

    python3 tests/test_pullwork.py
"""
from __future__ import annotations

import io
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="bouldermesh-pullwork-"))
os.environ["DATA_DIR"] = str(TMP / "data")
sys.path.insert(0, str(ROOT / "backend"))
import compute  # noqa: E402
import pullwork  # noqa: E402
from climbs.common import Cancelled, JobControl  # noqa: E402
from progress import Tracker  # noqa: E402

try:
    from PIL import Image
except ImportError:
    print("Pillow not installed: skipping the machines tests")
    sys.exit(0)

SCRIPT = ROOT / "worker" / "bouldermesh_worker.py"
SCRIPT_VERSION = int(re.search(r"^VERSION = (\d+)", SCRIPT.read_text(), re.M)[1])
FAKE_DOCKER = f'''#!{sys.executable}
import json, os, signal, sys
args = sys.argv[1:]
state = os.environ["FAKE_DOCKER_DIR"]
with open(os.path.join(state, "calls"), "a") as f:
    f.write(" ".join(args) + "\\n")
if args[:1] == ["version"]:
    print("99.0"); sys.exit(0)
if args[:1] == ["info"]:
    print(state); sys.exit(0)
if args[:2] == ["manifest", "inspect"]:
    size = int(os.environ.get("FAKE_IMAGE_BYTES", "1000"))
    print(json.dumps({{"Descriptor": {{"platform": {{"architecture": "amd64", "os": "linux"}}}}, "SchemaV2Manifest": {{"layers": [{{"size": size}}]}}}}))
    sys.exit(0)
if args[:2] == ["image", "rm"]:
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    sys.exit(0 if os.path.exists(os.path.join(state, "have-image")) else 1)
if args[:1] == ["pull"]:
    open(os.path.join(state, "have-image"), "w").close(); print("sha256:0123"); sys.exit(0)
if args[:2] == ["rm", "-f"]:
    pid = os.path.join(state, args[2] + ".pid")
    if os.path.exists(pid):
        try:
            os.kill(int(open(pid).read()), signal.SIGKILL)
        except ProcessLookupError:
            pass
    sys.exit(0)
if args[:1] == ["run"]:
    name = args[args.index("--name") + 1]
    mounts = dict(reversed(args[i + 1].rsplit(":", 1)) for i, a in enumerate(args) if a == "-v")
    open(os.path.join(state, name + ".pid"), "w").write(str(os.getpid()))
    os.environ["DATA_DIR"], os.environ["JOB"] = mounts["/data"], mounts["/job"]
    if os.environ.get("FAKE_JOB"):
        os.execvp("sh", ["sh", "-c", os.environ["FAKE_JOB"]])
    i = args.index("/app/backend/remote_job.py")
    os.execv(sys.executable, [sys.executable, "{ROOT}/backend/remote_job.py", mounts["/job"], *args[i + 2:]])
sys.exit("fake docker: " + " ".join(args))
'''
fake = TMP / "fake"
(fake / "bin").mkdir(parents=True)
(fake / "bin" / "docker").write_text(FAKE_DOCKER)
(fake / "bin" / "docker").chmod(0o755)
(fake / "bin" / "nvidia-smi").write_text(f"""#!{sys.executable}
import os, sys
if "-L" in sys.argv:
    print("GPU 0: a fake one"); sys.exit(0)
print(open(os.environ["FAKE_GPU"]).read() if os.environ.get("FAKE_GPU") else "0, 0, 24576")
""")
(fake / "bin" / "nvidia-smi").chmod(0o755)
ENV = {**os.environ, "PATH": f"{fake / 'bin'}:{os.environ['PATH']}", "FAKE_DOCKER_DIR": str(fake), "BOULDERMESH_NEED_RAM_GB": "0"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(path: str, body=None, token: str | None = None, raw: bytes | None = None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(URL + path, data=data, method="POST" if data is not None else "GET",
                                 headers={**({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req) as r:
            out = r.read()
            return r.status, (json.loads(out) if r.headers.get("Content-Type") == "application/json" else out)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def worker(token: str, *extra: str, env: dict | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--server", URL, "--token", token, "--home", str(TMP / "home"),
                           "--gpu", "none", *extra], env=env or ENV, capture_output=True, text=True, timeout=timeout)


def project(name: str) -> Path:
    p = TMP / name
    (p / "photos").mkdir(parents=True)
    (p / "masks").mkdir()
    (p / "masks" / "old.jpg.png").write_bytes(b"kept")
    for i in range(3):
        Image.new("RGB", (64, 48), (10 * i, 80, 120)).save(p / "photos" / f"p{i}.jpg")
    return p


class Seen(Tracker):
    def __init__(self):
        super().__init__([])
        self.keys = []

    def update(self, key, frac=0.0, detail=None):
        self.keys.append(key)


def in_thread(fn, *args, **kw) -> dict:
    """Run fn in a thread; what it returned or raised lands in the dict."""
    out: dict = {}

    def run():
        try:
            out["value"] = fn(*args, **kw)
        except BaseException as e:   # noqa: BLE001 - for the test to look at
            out["error"] = e

    out["thread"] = threading.Thread(target=run, daemon=True)
    out["thread"].start()
    return out


ctl = JobControl()
image = ["ghcr.io/me/bouldermesh:abc123"]
board = pullwork.Board(TMP / "data" / "compute" / "machines.sqlite", TMP / "data" / "compute" / "jobs", lambda: image[0], ctl)
port = free_port()
URL = f"http://127.0.0.1:{port}"
assert pullwork.serve(board, "127.0.0.1", port, lambda line: None)
pullwork.POLL = 0.3     # the wait for a machine to turn up, after a restart here

# --- machines and their tokens -----------------------------------------------------------------------------------------
hal = board.add("hal")
assert hal["token"].startswith("bmw_") and board.machine(hal["token"])["name"] == "hal" and board.machine("bmw_nope") is None
assert board.token(hal["id"]) == {"id": hal["id"], "name": "hal", "token": hal["token"]}, "kept, to show its command again"
assert oct((TMP / "data" / "compute" / "machines.sqlite").stat().st_mode)[-3:] == "600" and board.token("nope") is None
gone = board.add("gone")
board.revoke(gone["id"])
assert board.token(gone["id"])["token"] is None and not board.q("SELECT token FROM machines WHERE id=?", gone["id"], one=True)["token"]
try:
    board.add("  ")
    raise AssertionError("a machine needs a name")
except ValueError:
    pass
assert call("/worker/api/work", {"version": 1}, "bmw_nope")[0] == 401
assert call("/worker/api/work", {"version": 0}, hal["token"])[0] == 426, "too old a script"
status, page = call("/worker/")
assert status == 200 and b"bouldermesh_worker.py" in page
assert call("/worker/bouldermesh_worker.py")[1] == SCRIPT.read_bytes()
assert not board.connected(), "a script too old doesn't count as there"
try:
    board.reconstruct(project("none") / "photos", TMP / "none" / "work", TMP / "out-none", print)
    raise AssertionError("no machine has asked for work")
except compute.ComputeUnavailable as e:
    assert "no machine" in str(e), e
assert not board.jobs and not list((TMP / "data" / "compute" / "jobs").glob("*")), "nothing left from it"
r = worker(hal["token"], "--check", "--cpus", "3")
assert r.returncode == 0 and "knows this token as 'hal'" in r.stdout, r.stdout + r.stderr
assert board.connected() and json.loads((TMP / "home" / "config.json").read_text())["cpus"] == 3, "the token and options are kept"
print("machines OK")

# --- a whole job: the climb waits, the machine takes it, runs it, and the mesh comes back -------------------------------
p = project("whole")
logged: list[str] = []
seen = Seen()
got = in_thread(board.reconstruct, p / "photos", p / "work", TMP / "out1", logged.append, True, True, seen)
r = worker(hal["token"], "--once")      # its token and --cpus 3 remembered from --check
got["thread"].join(60)
assert "value" in got, (got, r.stdout, r.stderr, logged)
mesh, demo = got["value"]
assert mesh.exists() and mesh.parent == TMP / "out1" and demo is True, (mesh, demo)    # no COLMAP here: the demo mesh
assert (p / "masks" / "old.jpg.png").read_bytes() == b"kept", "masks from before stay"
assert any("hal took it" in line for line in logged) and any("remote: running on" in line for line in logged), logged
assert any("pulling ghcr.io/me/bouldermesh:abc123" in line for line in logged) and any("finished in" in line for line in logged), logged
assert seen.keys, "progress followed"
calls = (fake / "calls").read_text()
run = next(line for line in calls.splitlines() if line.startswith("run "))
for want in ("--user", "--cap-drop ALL", "--cpus 3.0", "-e DATA_DIR=/data", "nice -n 10", "remote_job.py /job --internet"):
    assert want in run, (want, run)
assert "--gpus" not in run and "--gpu" not in run.split("remote_job.py")[1], "--gpu none"
assert "pull -q ghcr.io/me/bouldermesh:abc123" in calls, "an image it didn't have is pulled"
assert not board.jobs and not list((TMP / "data" / "compute" / "jobs").glob("*")) and not list((TMP / "home" / "jobs").glob("*"))
assert not list(p.glob("bm-remote-*")), "the folder results came back through is gone"
assert board.state()["machines"][0]["done"] == 1
print("a whole job OK")

# --- a job that fails says why --------------------------------------------------------------------------------------------
p = project("fails")
got = in_thread(board.reconstruct, p / "photos", p / "work", TMP / "out2", print)
r = worker(hal["token"], "--once", env={**ENV, "FAKE_JOB": "echo 'FAILED: only 2 image(s) could be registered'; exit 1"})
got["thread"].join(30)
assert isinstance(got.get("error"), RuntimeError) and "only 2 image(s)" in str(got["error"]), (got, r.stdout)
assert board.state()["machines"][0]["failed"] == 1
print("a failing job OK")

# --- a climb stopped here stops the job there -----------------------------------------------------------------------------
p = project("stopped")
logged = []
got = in_thread(board.reconstruct, p / "photos", p / "work", TMP / "out3", logged.append)
proc = subprocess.Popen([sys.executable, str(SCRIPT), "--home", str(TMP / "home"), "--once"], env={**ENV, "FAKE_JOB": "exec sleep 60"},
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
t0 = time.time()
while not any("running on" in line for line in logged) and time.time() - t0 < 30:
    time.sleep(0.2)
ctl.stop()
got["thread"].join(10)
assert isinstance(got.get("error"), Cancelled), got
out, _ = proc.communicate(timeout=30)
assert "no longer wanted" in out and time.time() - t0 < 40, out
assert any(line.startswith("rm -f bmw-") for line in (fake / "calls").read_text().splitlines()), "its container is ended"
ctl.begin()
print("stopping OK")

# --- a machine that goes quiet; one that gives a job back; one restarting; one revoked ------------------------------------
pullwork.QUIET = 1.5
p = project("quiet")
ip = "203.0.113.9"
got = in_thread(board.reconstruct, p / "photos", p / "work", TMP / "out4", print)
time.sleep(0.5)
m = board.machine(hal["token"])
job = board.work(m, ip, {"client": "test"})["job"]
got["thread"].join(10)
assert isinstance(got.get("error"), compute.ComputeUnavailable) and "lost hal" in str(got["error"]), got
assert call(f"/worker/api/jobs/{job['id']}/log", {"lines": ["late"]}, hal["token"])[1].get("gone"), "and it hears it's gone"
pullwork.QUIET = 900

other = board.add("other")
o = board.machine(other["token"])
logged = []
got = in_thread(board.reconstruct, p / "photos", p / "work", TMP / "out5", logged.append)
time.sleep(0.5)
job = board.work(m, ip, {"client": "test", "gpu": True})["job"]
assert board.work(m, ip, {})["job"]["id"] == job["id"], "a machine that lost track of its job gets it again"
assert "wait" in board.work(o, ip, {}), "and no one else does"
status, part = call(f"/worker/api/jobs/{job['id']}/input?offset=0", token=hal["token"])
assert status == 200 and tarfile.open(fileobj=io.BytesIO(part)).getnames()[0] in ("photos", "masks")
assert call(f"/worker/api/jobs/{job['id']}/input?offset=0", token=other["token"])[0] == 409, "not another's"
assert call(f"/worker/api/jobs/{job['id']}/output?offset=5&size=10", token=hal["token"], raw=b"x")[1]["received"] == 0, "parts in order"
assert call(f"/worker/api/jobs/{job['id']}/done", {"status": "ok"}, hal["token"])[0] == 400, "not ok before the results have come"
assert call(f"/worker/api/jobs/{job['id']}/done", {"status": "released"}, hal["token"])[0] == 200
job2 = board.work(o, ip, {})["job"]
assert job2["id"] == job["id"], "a job given back goes to the next machine"
board.revoke(other["id"])
assert call("/worker/api/work", {"version": 1}, other["token"])[0] == 403
assert board.work(m, ip, {})["job"]["id"] == job["id"], "a revoked machine's job goes to another"
# results that would write outside out/ and masks/ are refused
evil = TMP / "evil.tar"
with tarfile.open(evil, "w") as tar:
    data = b"#!/bin/sh\n"
    info = tarfile.TarInfo("out/../../escaped.sh")
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
blob = evil.read_bytes()
assert call(f"/worker/api/jobs/{job['id']}/output?offset=0&size={len(blob)}", token=hal["token"], raw=blob)[1]["received"] == len(blob)
board.report(m, job["id"], ['@@result {"mesh": "mesh.glb", "demo": false}'])
assert call(f"/worker/api/jobs/{job['id']}/done", {"status": "ok"}, hal["token"])[0] == 200
got["thread"].join(10)
assert isinstance(got.get("error"), ValueError) and "shouldn't" in str(got["error"]), got
assert not (TMP / "escaped.sh").exists() and not list(p.glob("bm-remote-*"))
assert any("hal (GPU) took it" in line for line in logged) and any("stopped before finishing" in line for line in logged), logged
assert any("other was revoked" in line for line in logged), logged
print("quiet, given back, revoked and bad results OK")

# --- other jobs: finding beta, or size, from a mesh's files, and what they made coming back ---------------------------------
src = TMP / "task-in"
src.mkdir()
(src / "cameras.json").write_text('[{"name": "a.jpg"}]')
logged = []
got = in_thread(board.task, "beta", {"mesh/cameras.json": src / "cameras.json"}, {"why": "test"}, "beta: m1", logged.append)
time.sleep(0.5)
assert "wait" in board.work(board.machine(hal["token"]), ip, {"version": 1}), "a script too old for it isn't given it"
made = 'mkdir -p "$JOB/out" && cp "$JOB/mesh/cameras.json" "$JOB/out/beta.json" && grep -q test "$JOB/task.json" && echo \'@@result {"mesh": "", "files": ["beta.json"]}\''
r = worker(hal["token"], "--once", env={**ENV, "FAKE_JOB": made})
got["thread"].join(30)
assert "value" in got, (got, r.stdout, r.stderr, logged)
result, back = got["value"]
assert result["files"] == ["beta.json"] and (back / "out" / "beta.json").read_text() == '[{"name": "a.jpg"}]', result
shutil.rmtree(back)
run = [line for line in (fake / "calls").read_text().splitlines() if line.startswith("run ")][-1]
assert "remote_job.py /job --task beta" in run, run
assert not board.jobs and not list((TMP / "data" / "compute" / "jobs").glob("*"))
print("other jobs OK")

# --- --data: jobs and the models they download somewhere other than --home, remembered -----------------------------------
got = in_thread(board.task, "beta", {"mesh/cameras.json": src / "cameras.json"}, {"why": "test"}, "beta: m2", print)
time.sleep(0.5)
r = worker(hal["token"], "--once", "--data", str(TMP / "big-disk" / "bm"), env={**ENV, "FAKE_JOB": made})
got["thread"].join(30)
assert "value" in got, (got, r.stdout, r.stderr)
shutil.rmtree(got["value"][1])
run = [line for line in (fake / "calls").read_text().splitlines() if line.startswith("run ")][-1]
assert f"-v {TMP / 'big-disk' / 'bm' / 'data'}:/data" in run and f"{TMP / 'big-disk' / 'bm' / 'jobs'}/" in run, run
assert json.loads((TMP / "home" / "config.json").read_text())["data"] == str(TMP / "big-disk" / "bm"), "remembered"
assert not list((TMP / "big-disk" / "bm" / "jobs").iterdir()), "and the job's folder gone after"
print("--data OK")

# --- room: for jobs under --data (or --home), and for the image in Docker's storage, before either is used -------------------
fresh_home = ["--home", str(TMP / "home-room"), "--server", URL, "--token", hal["token"], "--gpu", "none"]
r = subprocess.run([sys.executable, str(SCRIPT), *fresh_home, "--need-gb", "1e9", "--check"], env=ENV, capture_output=True, text=True, timeout=60)
assert r.returncode and "isn't room for jobs" in r.stderr and "Give --data DIR" in r.stderr, r.stdout + r.stderr
r = subprocess.run([sys.executable, str(SCRIPT), *fresh_home, "--need-gb", "1e9", "--once", "--data", str(TMP / "small-disk")], env=ENV,
                   capture_output=True, text=True, timeout=60)
assert r.returncode and "not starting" in r.stderr and "Give --data" not in r.stderr, "--data given: nothing to suggest"
r = subprocess.run([sys.executable, str(SCRIPT), *fresh_home, "--need-gb", "0", "--check"], env=ENV, capture_output=True, text=True, timeout=60)
assert r.returncode == 0 and "room for jobs in" in r.stdout and "Docker's images:" in r.stdout, r.stdout + r.stderr
(fake / "have-image").unlink(missing_ok=True)
got = in_thread(board.task, "beta", {"mesh/cameras.json": src / "cameras.json"}, {"why": "test"}, "beta: m3", print)
time.sleep(0.5)
r = worker(hal["token"], "--once", env={**ENV, "FAKE_IMAGE_BYTES": str(10**15)})
assert r.returncode and "Docker keeps images in" in r.stderr and "docker image prune" in r.stderr, r.stdout + r.stderr
assert [j["state"] for j in board.state()["jobs"]] == ["queued"], "the job given back"
(TMP / "home" / "pulled.json").write_text(json.dumps(["ghcr.io/me/bouldermesh:older"]))
r = worker(hal["token"], "--once", env={**ENV, "FAKE_JOB": made})
got["thread"].join(30)
assert "value" in got, (got, r.stdout, r.stderr)
shutil.rmtree(got["value"][1])
assert "image rm ghcr.io/me/bouldermesh:older" in (fake / "calls").read_text(), "the image it pulled before is deleted"
assert json.loads((TMP / "home" / "pulled.json").read_text()) == ["ghcr.io/me/bouldermesh:abc123"]
print("room OK")

# --- a machine that won't run the image asked for gives the job back, and stops -------------------------------------------
p = project("prefix")
got = in_thread(board.reconstruct, p / "photos", p / "work", TMP / "out6", print)
time.sleep(0.5)
r = worker(hal["token"], "--once", "--image-prefix", "ghcr.io/someone-else/")
assert r.returncode and "only runs images starting ghcr.io/someone-else/" in r.stderr, r.stderr
st = board.state()
assert [j["state"] for j in st["jobs"]] == ["queued"], st["jobs"]
ctl.stop()
got["thread"].join(10)
ctl.begin()
print("image prefix OK")

# --- a machine registering itself: no climbs until it's approved, or with the admin page's leave, meshing whose results wait --
rules = dict(pullwork.RULES)
board.rules = lambda: rules
news: list = []
board.registered = news.append
reg_home = TMP / "home-reg"


def registered_worker(*extra: str, ok: bool = True) -> str:
    r = subprocess.run([sys.executable, str(SCRIPT), "--server", URL, "--home", str(reg_home), "--gpu", "none", *extra], env=ENV,
                       capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    return r.stdout + r.stderr


out = registered_worker("--check", "--name", "lab  box")
assert "as 'lab-box'" in out and "waiting for approval; no climbs until then" in out, out
lab_token = json.loads((reg_home / "config.json").read_text())["token"]
lab = board.machine(lab_token)
assert lab_token.startswith("bmw_") and lab["status"] == "provisional" and [n["id"] for n in news] == [lab["id"]]
assert board.work(lab, ip, {"version": 2}) == {"wait": 300, "why": "waiting for approval by whoever runs this site", "provisional": True}
assert next(m for m in board.state()["machines"] if m["name"] == "lab-box")["status"] == "awaiting approval"
rules["provisional_work"] = True
board.jobs["b" * 12] = pullwork.Job(id="b" * 12, label="beta: x", input=TMP / "none.tar", size=1, internet=False, image=image[0],
                                    task="beta", version=2)
assert "wait" in board.work(lab, ip, {"version": 2}), "only meshing: not finding beta, or size"
del board.jobs["b" * 12]
p = project("held")
got = in_thread(board.reconstruct, p / "photos", p / "work", TMP / "out-held", print)
r = registered_worker("--once")
got["thread"].join(60)
assert isinstance(got.get("error"), compute.ComputeUnavailable) and "waits for approval" in str(got["error"]), (got, r)
held = TMP / "data" / "compute" / "held"
assert (held / "held.tar").exists() and json.loads((held / "held.json").read_text())["machine"] == lab["id"]
assert not (TMP / "out-held").exists() and next(m for m in board.state()["machines"] if m["name"] == "lab-box")["held"] == 1
try:
    board.reconstruct(p / "photos", p / "work", TMP / "out-held", print)
    raise AssertionError("its machine still waits")
except compute.ComputeUnavailable as e:
    assert "waits for approval" in str(e) and not board.jobs, "at once, with no new job"
board.approve(lab["id"])
logged = []
mesh, demo = board.reconstruct(p / "photos", p / "work", TMP / "out-held", logged.append)
assert mesh.exists() and mesh.parent == TMP / "out-held" and not list(held.glob("*")), (mesh, list(held.glob("*")))
assert any("before it was approved" in line for line in logged), logged
(p / "photos" / "p0.jpg").write_bytes(b"other")      # held from other photos than the climb has now: not taken
(held / "held.json").write_text(json.dumps({"machine": lab["id"], "inputs": [], "result": {}}))
(held / "held.tar").write_bytes(b"x")
assert board._take_held(p, {"photos": p / "photos"}, print) is None and not list(held.glob("*"))
# turned down: what it made goes, and its token says so
nobody = board.register("bmw_" + "z" * 40, " \t ", "198.51.100.7")
assert nobody["name"] == "machine" and nobody["status"] == "provisional"
(held / "other.json").write_text(json.dumps({"machine": nobody["id"]}))
(held / "other.tar").write_bytes(b"x")
board.revoke(nobody["id"], "denied")
assert not list(held.glob("other.*"))
assert "turned down" in call("/worker/api/work", {"version": 2}, "bmw_" + "z" * 40)[1]["error"]
# the script forgets a token the server no longer takes, says so, and registers a new one as with none
for token, says in (("bmw_" + "z" * 40, "this machine was turned down by whoever runs this site"), ("bmw_" + "u" * 40, "unknown token")):
    r = worker(token, "--check")
    new = json.loads((TMP / "home" / "config.json").read_text())["token"]
    assert r.returncode == 0 and f"warning: {URL} no longer takes this machine's token ({says}" in r.stdout, r.stdout + r.stderr
    assert "Forgot it, and registering a new one" in r.stdout and "knows this token as" in r.stdout and "Traceback" not in r.stderr
    assert new != token and board.machine(new)["status"] == "provisional", (new, token)
r = worker("bmw_" + "u" * 40, "--once")
assert r.returncode == 0 and "no longer takes this machine's token" in r.stdout and "Traceback" not in r.stderr, r.stdout + r.stderr
r = subprocess.run([sys.executable, str(SCRIPT), "--home", str(TMP / "home"), "--forget-token"], env=ENV, capture_output=True, text=True,
                   timeout=60, stdin=subprocess.DEVNULL)
conf = json.loads((TMP / "home" / "config.json").read_text())
assert r.returncode == 0 and "forgot the token" in r.stdout and "token" not in conf and conf["server"] == URL, (r.stdout, conf)
out = subprocess.run([sys.executable, str(SCRIPT), "--home", str(TMP / "home"), "--register", "--name", "again", "--check"], env=ENV,
                     capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
again = json.loads((TMP / "home" / "config.json").read_text())["token"]
assert out.returncode == 0 and "as 'again'" in out.stdout and again != "bmw_" + "u" * 40, out.stdout + out.stderr
assert board.machine(again)["status"] == "provisional", "--register: a new token in place of the remembered one"
assert call("/worker/api/register", {"version": 2}, "short")[0] == 400
st, j = call("/worker/api/register", {"version": 2, "name": "x"}, lab_token)
assert st == 200 and j["name"] == "lab-box" and j["status"] == "approved", "registering again is harmless"
print("registering OK")

# --- remote_job.py's other jobs, here: what they make in the mesh's folder goes in out/, and nothing else -----------------
import contextlib  # noqa: E402

import beta  # noqa: E402
import remote_job  # noqa: E402
import scale  # noqa: E402
from climbs import youtube  # noqa: E402

job = TMP / "remote-job"
(job / "mesh").mkdir(parents=True)
(job / "photos").mkdir()
(job / "task.json").write_text(json.dumps({"climbs": [{"name": "Alpha"}], "videos": {}, "known": None}))
seen = {}
beta.find = lambda mesh, photos, log: [(mesh / "beta.json").write_text("{}"), (mesh / "beta_0123abcd.jpg").write_bytes(b"j"),
                                       (mesh / "notes.html").write_text("x"), seen.update(via=youtube.via, cache=youtube.VIDEO_CACHE)]
scale.estimate = lambda mesh, photos, masks, climbs, videos, log, known=None: [(mesh / "scale.json").write_text("{}"),
                                                                              seen.update(climbs=climbs, masks=masks)]
printed = io.StringIO()
with contextlib.redirect_stdout(printed):
    assert remote_job.task(job, "beta", lambda line: None) == 0
    assert remote_job.task(job, "scale", lambda line: None) == 0
assert sorted(f.name for f in (job / "out").iterdir()) == ["beta.json", "beta_0123abcd.jpg", "scale.json"]
assert '"files": ["beta.json", "beta_0123abcd.jpg"]' in printed.getvalue() and '"files": ["scale.json"]' in printed.getvalue()
assert seen == {"via": "volunteers", "cache": job / "videos", "climbs": [{"name": "Alpha"}], "masks": None}, "never YouTube from there"
youtube.via = "volunteers_when_refused"
print("remote_job's other jobs OK")

# --- an area's settings for COLMAP's mapper go with its reconstructions, as task.json, only to scripts that take one ------
import meshopts  # noqa: E402

photos_only = {"photos": job / "photos"}
plain = board._new_job("reconstruct", photos_only, {"internet": True}, "plain", image[0])
told = board._new_job("reconstruct", photos_only, {"internet": True, "mapper": {"retry_below": 0.6, "more": 2}}, "told", image[0])
with tarfile.open(plain.input) as t:
    assert "task.json" not in t.getnames() and plain.version == 1
with tarfile.open(told.input) as t:
    assert json.loads(t.extractfile("task.json").read())["mapper"] == {"retry_below": 0.6, "more": 2} and told.version == 2
for j in (plain, told):
    del board.jobs[j.id]
    j.input.unlink()
got = {}
real_reconstruct = remote_job.pipeline.reconstruct
remote_job.pipeline.reconstruct = lambda *a, **k: (got.update(mapper=meshopts.mapper()), (job / "out" / "mesh.glb", False))[1]
argv = sys.argv
try:
    for told_it, want in ((None, meshopts.MAPPER), ({"retry_below": 0.6, "more": 2}, {"retry_below": 0.6, "more": 2})):
        (job / "task.json").write_text(json.dumps({"mapper": told_it} if told_it else {}))
        sys.argv = ["remote_job.py", str(job), "--internet"]
        with contextlib.redirect_stdout(io.StringIO()):
            assert remote_job.main() == 0
        assert got["mapper"] == want, got
finally:
    remote_job.pipeline.reconstruct, sys.argv = real_reconstruct, argv
print("the mapper's settings OK")

# --- the script updating itself when the server serves a newer one: between jobs, never during one ---------------------
NEWER = """#!/usr/bin/env python3
VERSION = 99
import sys, pathlib
pathlib.Path(__file__).with_name("ran.json").write_text(__import__("json").dumps(sys.argv[1:]))
"""
newer = TMP / "served" / "bouldermesh_worker.py"
newer.parent.mkdir()
newer.write_text(NEWER)
served, pullwork.SCRIPT = pullwork.SCRIPT, newer
here = TMP / "wrun" / "bouldermesh_worker.py"       # a copy, outside a git checkout, as a machine has it
here.parent.mkdir()
shutil.copy2(SCRIPT, here)
args = ["--server", URL, "--token", hal["token"], "--home", str(TMP / "home"), "--gpu", "none"]
try:
    for extra in (["--check"], ["--once"], ["--once", "--no-auto-update"]):
        r = subprocess.run([sys.executable, str(here), *args, *extra], env=ENV, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and here.read_bytes() == SCRIPT.read_bytes(), r.stdout + r.stderr
        assert ("newer version of this script (99)" in r.stdout) == (extra != ["--check"]), r.stdout
    r = subprocess.run([sys.executable, str(here), *args, "--register"][:-1], env=ENV, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and f"updated itself from version {SCRIPT_VERSION} to 99" in r.stdout and here.read_text() == NEWER, r.stdout + r.stderr
    assert json.loads((here.parent / "ran.json").read_text()) == args, "started again with the same arguments"
    assert (TMP / "home" / "previous.py").read_bytes() == SCRIPT.read_bytes(), "the one before kept"
    assert not board.jobs or all(j.machine != hal["id"] for j in board.jobs.values()), "given no job meanwhile"
    r = subprocess.run([sys.executable, str(SCRIPT), *args, "--once"], env=ENV, capture_output=True, text=True, timeout=120)
    assert "it doesn't update itself (it's in a git checkout" in r.stdout and SCRIPT.read_bytes() != NEWER.encode(), r.stdout
    assert board.q("SELECT client FROM machines WHERE id=?", hal["id"], one=True)["client"].endswith(
        "; won't update itself: it's in a git checkout"), "and the server's told why"
    other = TMP / "their-repo" / "tools"     # downloaded into another repository's folder, untracked: it updates itself
    other.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(other.parent)], check=True)
    theirs = other / "bouldermesh_worker.py"
    shutil.copy2(SCRIPT, theirs)
    r = subprocess.run([sys.executable, str(theirs), *args], env=ENV, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and f"updated itself from version {SCRIPT_VERSION} to 99" in r.stdout and theirs.read_text() == NEWER, r.stdout + r.stderr
finally:
    pullwork.SCRIPT = served
print("updating itself OK")

# --- holding back while its owner's work keeps its GPU busy, or its memory is short; and what the server keeps of it -----
box = board.add("gpu-box")
mb = lambda: board.machine(box["token"])     # noqa: E731
asks = {"version": SCRIPT_VERSION, "gpu": True}
r = board.work(mb(), "10.0.0.9", {**asks, "held": "others keep its GPU busy (85% over the last 3 min, over 50%)"})
assert r["held"] and "others keep its GPU busy" in r["why"] and board.current(box["id"])["state"] == "held back", r
assert board.held_why() is None, "hal, there too, isn't held back"
assert board.work(mb(), "10.0.0.9", asks) == {"wait": pullwork.POLL} and board.current(box["id"])["state"] == "idle"
many = TMP / "many"
(many / "photos").mkdir(parents=True)
for i in range(30):
    (many / "photos" / f"p{i}.jpg").write_bytes(b"x")
big = board._new_job("reconstruct", {"photos": many / "photos"}, {"internet": True}, "big", image[0])
assert big.gpu_mb == 6300 + 1000 + 100 * 20 == pullwork.gpu_need_mb("reconstruct", 30) and pullwork.gpu_need_mb("reconstruct", 1) == 7600
r = board.work(mb(), "10.0.0.9", {**asks, "gpu_free_mb": 5000})
assert r["held"] and "its GPU has 4.9 GB free, and the next climb may need 9.1 GB" in r["why"] and big.state == "queued", r
assert board.current(box["id"])["why"].startswith("its GPU has 4.9 GB"), "the same state: only the reason replaced"
r = board.work(mb(), "10.0.0.9", {**asks, "gpu_free_mb": 12000})
assert r["job"]["id"] == big.id and board.current(box["id"]) == {**board.current(box["id"]), "state": "working", "why": "big"}
del board.jobs[big.id]
big.input.unlink()
# gone quiet: offline from CONNECTED after it was last heard from, by the sweep or when it asks again
board.q("UPDATE machines SET last_seen=? WHERE id=?", time.time() - 600, box["id"])
board.seen[box["id"]] = time.time() - 600
board.sweep()
assert board.current(box["id"])["state"] == "offline" and abs(board.current(box["id"])["start"] - (time.time() - 600 + pullwork.CONNECTED)) < 5
assert board.work(mb(), "10.0.0.9", asks) == {"wait": pullwork.POLL} and board.current(box["id"])["state"] == "idle"
states = [c["state"] for c in board.changes(200) if c["name"] == "gpu-box"]
assert states[0] == "idle" and "offline" in states and states.count("held back") == 2, states
# its shares of a day: as recorded, with an open record of one gone quiet counted as offline
quiet = board.add("quiet-box")
now = time.time()
for state, start, end in (("working", now - 4 * 3600, now - 3 * 3600), ("idle", now - 3 * 3600, now - 3600), ("held back", now - 3600, None)):
    board.q("INSERT INTO machine_states VALUES(?,?,?,?,?)", quiet["id"], state, None, start, end)
board.q("UPDATE machines SET last_seen=? WHERE id=?", now - 1800 - pullwork.CONNECTED, quiet["id"])
sh = board.shares(quiet["id"], now - 86400)
assert sh["hours"] == 4.0 and sh["working"] == 0.25 and sh["idle"] == 0.5 and sh["held back"] == 0.125 and sh["offline"] == 0.125, sh
assert board.shares(quiet["id"], now - 2 * 3600)["idle"] == 0.5, "only since"
st = {m["name"]: m for m in board.state()["machines"]}
assert st["quiet-box"]["day"] == sh and st["gpu-box"]["now"]["state"] == "idle" and "changes" in board.state()
# the script: holding back while others keep its GPU busy, and taking climbs again once it's quiet
gpu_file = TMP / "gpu.csv"
gpu_file.write_text("90, 3000, 24576")
genv = {**ENV, "FAKE_GPU": str(gpu_file), "BOULDERMESH_GPU_WINDOWS": "0.1,0.6,1.2"}
proc = subprocess.Popen([sys.executable, str(SCRIPT), "--server", URL, "--token", box["token"], "--home", str(TMP / "gbox"), "--gpu", "all"],
                        env=genv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
pullwork.POLL, poll = 0.3, pullwork.POLL
try:
    def wait_state(state, why_has="", seconds=30):
        for _ in range(int(seconds * 10)):
            c = board.current(box["id"])
            if c and c["state"] == state and why_has in (c["why"] or ""):
                return c
            time.sleep(0.1)
        raise AssertionError(f"not {state}: {board.current(box['id'])}")
    wait_state("held back", "others keep its GPU busy")
    gpu_file.write_text("3, 3000, 24576")
    wait_state("idle")
finally:
    proc.terminate()
    out = proc.communicate(timeout=30)[0]
    pullwork.POLL = poll
assert "no climbs for now: held back: others keep its GPU busy" in out and out.count("others keep its GPU busy") == 1, out
r = subprocess.run([sys.executable, str(SCRIPT), "--server", URL, "--token", box["token"], "--home", str(TMP / "gbox"), "--once",
                    "--need-ram-gb", "1e6"], env=genv, capture_output=True, text=True, timeout=60)
assert "GB of memory free, and a climb may need 1e+06 GB" in r.stdout and board.current(box["id"])["state"] == "held back", r.stdout
print("holding back OK")

# --- machines joining and leaving (presence.py): told when one starts being heard from or stops, not as it's held back,
# and each at most once in an hour, so that one dropping out and back in tells where it ended up
import presence  # noqa: E402

came: list = []
board.came_went = lambda m, state, why: came.append((m["name"], state, why))
board.q("UPDATE machines SET told=CASE WHEN active_at>? THEN 'here' ELSE 'gone' END, told_at=NULL", time.time() - pullwork.GONE_AFTER)
board.presence()
came.clear()
pat = board.add("pat")
r = worker(pat["token"], "--check", "--home", str(TMP / "pat"))
board.presence()
assert came == [], "a token's check isn't joining"
r = worker(pat["token"], "--once", "--home", str(TMP / "pat"), "--need-ram-gb", "1e6")       # it asks, held back, and stops
assert board.q("SELECT bye_at FROM machines WHERE id=?", pat["id"], one=True)["bye_at"], "it said goodbye" + r.stdout + r.stderr
board.q("UPDATE machines SET bye_at=NULL WHERE id=?", pat["id"])
board.presence()
assert came == [("pat", "here", "")], came
board.q("UPDATE machines SET bye_at=?, told_at=told_at-3600 WHERE id=?", time.time(), pat["id"])
board.presence()
assert len(came) == 1, "a goodbye just said may be a restart"
board.q("UPDATE machines SET told_at=told_at+3600, active_at=?, bye_at=? WHERE id=?", time.time() - 300, time.time() - 200, pat["id"])
board.presence()
assert len(came) == 1, "stopped, but not told again within the hour"
board.q("UPDATE machines SET told_at=told_at-3600 WHERE id=?", pat["id"])
board.presence()
assert came[-1] == ("pat", "gone", "it said goodbye"), came
board.q("UPDATE machines SET bye_at=NULL, active_at=?, told_at=told_at-3600 WHERE id=?", time.time() - pullwork.GONE_AFTER - 60, pat["id"])
board.presence()
assert len(came) == 2, "gone, and still gone: nothing new"
board.q("UPDATE machines SET active_at=?, told_at=told_at-3600 WHERE id=?", time.time(), pat["id"])
board.presence()
assert came[-1] == ("pat", "here", "") and len(came) == 3
board.q("UPDATE machines SET active_at=?, told_at=told_at-3600 WHERE id=?", time.time() - pullwork.GONE_AFTER - 60, pat["id"])
board.presence()
assert came[-1][1] == "gone" and came[-1][2].startswith("not heard from since"), came
print("joining and leaving OK")
print("ALL MACHINES TESTS PASSED")
