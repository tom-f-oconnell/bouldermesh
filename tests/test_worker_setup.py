#!/usr/bin/env python3
"""The worker script's --setup (installing Docker and NVIDIA's container toolkit where they're missing), its check of
Docker when it starts, and --check's offer to start it by itself (as a systemd service of the user's own, or on a Mac a
launchd agent), and the room on Docker Desktop's disk, on machines
described here rather than looked at. Offline: nothing is installed or run, and the service is written to a scratch folder."""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import re
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("worker", ROOT / "worker" / "bouldermesh_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def machine(**changes) -> dict:
    """A machine as machine_state() describes one: by default hal, with everything in place."""
    st = {"linux": True, "debian": True, "system": "Ubuntu 26.04 LTS", "root": False, "user": "pat", "docker": True,
          "docker_state": "ok", "runtimes": ["io.containerd.runc.v2", "nvidia", "runc"], "running": 0, "in_group": True,
          "cdi": True, "cgroup": "systemd", "gpu": True, "gpu_hardware": True, "toolkit": True, "have": {"curl": True, "gpg": True}}
    return {**st, **changes}


def commands(steps) -> list[str]:
    return [cmd for step in steps for cmd in step["commands"]]


def check_safe(steps) -> None:
    """Every install asks apt's own question, and nothing installs or changes a GPU driver."""
    for cmd in commands(steps):
        assert not re.search(r"(^|\s)(-y|--yes|--assume-yes|-qq)(\s|$)", cmd), cmd
        assert not re.search(r"nvidia-driver|ubuntu-drivers|cuda-drivers|modprobe", cmd), cmd


# --- everything there: nothing to do
steps, notes = worker.setup_steps(machine())
assert steps == [] and notes == [], (steps, notes)
assert worker.docker_problem(machine(), "auto") is None
print("ready machine OK")

# --- a fresh Ubuntu with a GPU and its driver: Docker, the group, the toolkit, its runtime, a restart, a check
st = machine(docker=False, docker_state=None, runtimes=[], running=None, in_group=False, toolkit=False, have={"curl": False, "gpg": True})
steps, notes = worker.setup_steps(st)
check_safe(steps)
whys = [s["why"].split(":")[0].split(",")[0] for s in steps]
assert [w.split()[0] for w in whys] == ["Install", "Let", "Install", "Give", "Restart", "Check"], whys
assert commands(steps)[:2] == ["sudo apt-get update", "sudo apt-get install docker.io"], commands(steps)
assert "sudo usermod -aG docker pat" in commands(steps) and "root" in steps[1]["warn"]
toolkit = steps[2]["commands"]
assert toolkit[0] == "sudo apt-get install curl", "only what's missing of curl and gpg"
assert any("nvidia.github.io/libnvidia-container/gpgkey" in c and "sudo gpg --dearmor" in c for c in toolkit)
assert toolkit[-1] == "sudo apt-get install nvidia-container-toolkit"
assert steps[3]["commands"] == ["sudo nvidia-ctk runtime configure --runtime=docker --cdi.enabled"]
assert steps[4]["commands"] == ["sudo systemctl restart docker"] and steps[4]["warn"] is None, "a Docker installed just now runs nothing"
steps, _ = worker.setup_steps(machine(docker_state="denied", running=None, toolkit=False, runtimes=[]))
assert "any containers" in steps[-2]["warn"], "how many isn't known"
assert notes == []
assert worker.docker_problem(st, "auto") == "Docker isn't installed"
print("fresh machine OK")

# --- Docker there, with containers running, but no toolkit: the restart says what it stops
st = machine(toolkit=False, runtimes=["runc"], running=3)
steps, notes = worker.setup_steps(st)
check_safe(steps)
assert len(steps) == 4 and "3 container(s)" in steps[2]["warn"] and "Say no" in steps[2]["warn"], steps
assert "toolkit isn't installed" in worker.docker_problem(st, "auto") and worker.docker_problem(st, "none") is None
assert worker.docker_problem(st, "cdi") is None, "a CDI device is the user's own arrangement"
steps, _ = worker.setup_steps(machine(runtimes=["runc"], running=0))
assert [s["commands"][0] for s in steps][:2] == ["sudo nvidia-ctk runtime configure --runtime=docker --cdi.enabled", "sudo systemctl restart docker"]
assert steps[1]["warn"] is None, "nothing running: nothing to warn of"
# CDI alone missing: switched on, Docker restarted with its warning, and the GPU tried as a CDI device
steps, _ = worker.setup_steps(machine(cdi=False, running=2))
assert len(steps) == 3 and "--cdi.enabled" in steps[0]["commands"][0] and "systemd reloads" in steps[0]["why"], steps
assert "2 container(s)" in steps[1]["warn"] and "--device nvidia.com/gpu=all" in steps[2]["commands"][-1], steps
check_safe(steps)
print("toolkit missing OK")

# --- no GPU, or a GPU without its driver: no GPU steps, and a driver is left to the user
for st, says in ((machine(gpu=False, gpu_hardware=False), "no NVIDIA GPU"), (machine(gpu=False, gpu_hardware=True), "ubuntu-drivers")):
    steps, notes = worker.setup_steps(st)
    assert steps == [] and len(notes) == 1 and (says in notes[0] or says.lower() in notes[0].lower()), notes
    assert worker.docker_problem(st, "auto") is None
print("no GPU OK")

# --- Docker stopped, or this user not let in yet
steps, _ = worker.setup_steps(machine(docker_state="stopped"))
assert commands(steps) == ["sudo systemctl enable --now docker"], steps
assert worker.docker_problem(machine(docker_state="stopped"), "auto") == "Docker isn't running"
steps, notes = worker.setup_steps(machine(docker_state="denied", in_group=False))
assert commands(steps) == ["sudo usermod -aG docker pat"] and notes == []
assert "aren't in the docker group" in worker.docker_problem(machine(docker_state="denied", in_group=False), "auto")
steps, notes = worker.setup_steps(machine(docker_state="denied", in_group=True))
assert steps == [] and "log in again" in notes[0], "joined since this login began"
assert "log in again" in worker.docker_problem(machine(docker_state="denied", in_group=True), "auto")
print("permissions OK")

# --- as root: no sudo, and no group to join
steps, _ = worker.setup_steps(machine(root=True, user=None, docker=False, docker_state=None, in_group=False, toolkit=False, runtimes=[]))
assert not any(c.startswith("sudo ") or "| sudo " in c for c in commands(steps)) and not any("usermod" in c for c in commands(steps))
print("root OK")

# --- running the steps: each asked about, stopping at a no or a failure
said, ran = [], []
steps = [{"why": "one", "commands": ["a", "b"]}, {"why": "two", "warn": "careful", "commands": ["c"]}, {"why": "three", "commands": ["d"]}]
answers = iter(["y", "yes", "n"])
assert not worker.run_steps(steps, ask=lambda q: next(answers), run=lambda c: ran.append(c) or 0, say=said.append)
assert ran == ["a", "b", "c"] and any("Note: careful" in s for s in said) and said[-1].startswith("Stopped there"), (ran, said)
ran.clear()
assert not worker.run_steps(steps, ask=lambda q: "y", run=lambda c: ran.append(c) or (2 if c == "b" else 0), say=said.append)
assert ran == ["a", "b"] and "exit status 2" in said[-1], "nothing after a failure"
ran.clear()
assert worker.run_steps(steps, ask=lambda q: "Y", run=lambda c: ran.append(c) or 0, say=said.append) and ran == ["a", "b", "c", "d"]
assert not worker.run_steps(steps, ask=lambda q: "", run=lambda c: 1 / 0, say=said.append), "no answer is no"
print("running steps OK")

# --- a service started before its user joined the docker group: only starting again gives it the group
assert "restart the computer" in worker.docker_problem(machine(docker_state="denied", in_group=True, service=True), "auto")
print("service's groups OK")

# --- starting by itself: offered only where systemd runs the user's own services, and not if it starts by itself already
linux = {"systemd": True, "root": False, "wsl": False, "user": "pat", "user_unit": "not-found", "system_unit": "not-found",
         "linger": False}
assert worker.autostart_offer(linux) == (True, None)
assert worker.autostart_offer({**linux, "user_unit": "disabled"}) == (True, None), "turned off: offered again"
for change in ({"systemd": False, "user_unit": "", "system_unit": ""}, {"root": True}, {"wsl": True}):
    assert worker.autostart_offer({**linux, **change}) == (False, None), change
offer, note = worker.autostart_offer({**linux, "system_unit": "enabled"})
assert not offer and "the system's bouldermesh-worker" in note, "hal's, deploy/bouldermesh-worker.service"
offer, note = worker.autostart_offer({**linux, "user_unit": "enabled"})
assert not offer and "sudo loginctl enable-linger pat" in note and "restart" in note, note
assert "linger" not in worker.autostart_offer({**linux, "user_unit": "enabled", "linger": True})[1]
print("offering OK")

# --- the service: ExecStart's words escaped and quoted, as systemd reads them
assert worker.unit_word("/usr/bin/python3") == "/usr/bin/python3"
assert worker.unit_word("/home/pat/my jobs/50%$x") == '"/home/pat/my jobs/50%%$$x"'
assert worker.unit_word('a"b\\c') == '"a\\"b\\\\c"'
assert worker.unit_word("PATH=$HOME/bin:%x", dollars=False) == "PATH=$HOME/bin:%%x", "Environment doesn't expand $"
assert worker.unit_word("") == '""'


def options(**changes) -> argparse.Namespace:
    """--check's arguments, as main() leaves them: by default, nothing given that isn't remembered."""
    return argparse.Namespace(**{"home": "~/.bouldermesh-worker", "nice": 10, "need_gb": None, "docker_arg": [],
                                 "no_auto_update": False, "server": "https://boulders.example.com", **changes})


assert worker.service_args(options()) == [], "the rest is remembered, in config.json"
scratch = pathlib.Path(tempfile.mkdtemp())
assert worker.service_args(options(home=str(scratch), nice=5, need_gb=20.0, docker_arg=["--memory=64g"], no_auto_update=True)) == [
    "--home", str(scratch.resolve()), "--nice", "5", "--need-gb", "20", "--docker-arg=--memory=64g", "--no-auto-update"]
unit = worker.service_unit("https://boulders.example.com", ["/usr/bin/python3", "/home/pat/.bouldermesh-worker/bouldermesh_worker.py"],
                           {"PATH": "/usr/bin:/bin", "DOCKER_HOST": "unix:///run/user/1000/docker.sock"})
lines = unit.splitlines()
assert "ExecStart=/usr/bin/python3 /home/pat/.bouldermesh-worker/bouldermesh_worker.py" in lines, unit
assert "Environment=DOCKER_HOST=unix:///run/user/1000/docker.sock" in lines and "Environment=PATH=/usr/bin:/bin" in lines
assert "RestartPreventExitStatus=3" in lines and "WantedBy=default.target" in lines, "a user's service, from login or the computer's start"
assert all(worker.CARRIED.fullmatch(k) for k in ("PATH", "DOCKER_HOST", "BOULDERMESH_NEED_RAM_GB"))
assert not any(worker.CARRIED.fullmatch(k) for k in ("HOME", "MYPATH", "PATHS", "SSH_AUTH_SOCK"))
print("service text OK")

# --- whether it's still running after starting: a failed start waits to try again, as auto-restart
up = {"ActiveState": "active", "SubState": "running", "NRestarts": "0"}
assert worker.service_running(wait=0, look=lambda: up)
for down in ({"ActiveState": "activating", "SubState": "auto-restart", "NRestarts": "0"}, {"ActiveState": "failed"},
             {**up, "NRestarts": "1"}, {}):
    assert not worker.service_running(wait=0, look=lambda: down), down
looks = iter([up, up, {"ActiveState": "failed"}])
assert not worker.service_running(wait=5, look=lambda: next(looks)), "stopped partway through the wait"
print("running OK")

# --- installing it: asked first (Enter is yes), the script copied where it updates itself, and the service started
home, units = scratch / "settings", scratch / "units"
home.mkdir()


def install(answer: str, ran: list, fail: str | None = None, up: bool = True, linger: bool = False, **changes):
    (units / "bouldermesh-worker.service").unlink(missing_ok=True)     # each a first install: one already there isn't asked about
    said = []
    ok = worker.install_service(options(home=str(home), **changes), {**linux, "linger": linger}, ask=lambda q: answer,
                                run=lambda cmd: ran.append(" ".join(cmd)) or int(" ".join(cmd) == fail), say=said.append,
                                unit_dir=units, running=lambda: up)
    return ok, "\n".join(said)


ran = []
ok, said = install("n", ran)
assert not ok and ran == [] and not units.exists() and not (home / "bouldermesh_worker.py").exists(), "no: nothing done"
ok, said = install("", ran, nice=5)
assert ok, said
assert ran == ["systemctl --user daemon-reload", "systemctl --user enable --now bouldermesh-worker", "loginctl enable-linger"], ran
copy = home / "bouldermesh_worker.py"
assert copy.read_bytes() == (ROOT / "worker" / "bouldermesh_worker.py").read_bytes(), "a copy outside the git checkout"
text = (units / "bouldermesh-worker.service").read_text()
assert f"ExecStart={sys.executable} {copy.resolve()} --home {home.resolve()} --nice 5" in text.splitlines(), text
assert worker.load_conf(home).get("service") == "bouldermesh-worker", "a run by hand then says it runs already"
assert "disable --now bouldermesh-worker" in said
ran.clear()                 # installed already as Y would install it: not asked again, whatever the PATH it was written with
unit_text = (units / "bouldermesh-worker.service").read_text()
(units / "bouldermesh-worker.service").write_text(re.sub(r"(?m)^Environment=\"?PATH=.*\n", 'Environment="PATH=/elsewhere"\n', unit_text))
asked = []
ok = worker.install_service(options(home=str(home), nice=5), {**linux, "linger": False, "user_unit": "disabled"},
                            ask=lambda q: asked.append(q) or "y", run=lambda cmd: ran.append(cmd) or 0, say=asked.append,
                            unit_dir=units, running=lambda: True)
assert not ok and ran == [] and not any("[Y/n]" in a for a in asked) and "systemctl --user enable --now" in asked[-1], asked
ok = worker.install_service(options(home=str(home), nice=5), {**linux, "linger": False, "user_unit": "enabled"},
                            ask=lambda q: 1 / 0, run=lambda cmd: 1 / 0, say=lambda m: None, unit_dir=units, running=lambda: True)
assert ok, "enabled and running: nothing to ask or do"
(units / "bouldermesh-worker.service").write_text(unit_text)
ran.clear()
ok, said = install("y", ran, linger=True)
assert ok and ran[-1] == "systemctl --user enable --now bouldermesh-worker", "lingering already"
ran.clear()
ok, said = install("y", ran, fail="loginctl enable-linger")
assert ok and "sudo loginctl enable-linger pat" in said, "runs only while logged in, and says what would start it with the computer"
(home / "config.json").unlink()
ran.clear()
ok, said = install("y", ran, fail="systemctl --user enable --now bouldermesh-worker")
assert not ok and ran[-1].startswith("systemctl --user enable") and "exit status 1" in said
assert "service" not in worker.load_conf(home), "not running as a service"
quiet = worker.quiet
worker.quiet = lambda cmd: None     # the journal, of a service that isn't there
try:
    ok, said = install("y", [], up=False, linger=True)
finally:
    worker.quiet = quiet
assert not ok and "it stopped" in said and "systemctl --user status bouldermesh-worker" in said, said
shutil.rmtree(scratch)
print("installing OK")

# --- on a Mac: a launchd agent of the user's own, offered unless loaded already
mac = {**linux, "systemd": False, "user_unit": "", "system_unit": "", "linger": False, "launchd": True, "agent": False}
assert worker.autostart_offer(mac) == (True, None)
assert worker.autostart_offer({**mac, "root": True}) == (False, None)
offer, note = worker.autostart_offer({**mac, "agent": True})
assert not offer and "kickstart -k gui/" in note and worker.AGENT_LOG in note, note
printed = """gui/501/org.bouldermesh.worker = {
	active count = 1
	path = /Users/pat/Library/LaunchAgents/org.bouldermesh.worker.plist
	state = running
	program = /usr/bin/python3
	runs = 1
	pid = 4242
"""
assert worker.agent_props(printed) == {"ActiveState": "active", "SubState": "running", "NRestarts": "0"}
assert worker.service_running(wait=0, look=lambda: worker.agent_props(printed))
again = printed.replace("state = running", "state = not running").replace("runs = 1", "runs = 3")
assert worker.agent_props(again) == {"ActiveState": "inactive", "SubState": "not", "NRestarts": "2"}
assert not worker.service_running(wait=0, look=lambda: worker.agent_props(again)), "ended, and waiting to try again"
assert not worker.service_running(wait=0, look=lambda: worker.agent_props(printed.replace("runs = 1", "runs = 2")))
print("Mac offer OK")

import plistlib     # noqa: E402
scratch = pathlib.Path(tempfile.mkdtemp())
home, agents = scratch / "settings", scratch / "agents"
home.mkdir()
real_log = worker.AGENT_LOG
worker.AGENT_LOG = str(scratch / "logs" / "worker.log")


def install_mac(answer: str, ran: list, fail: str | None = None, up: bool = True, loaded: bool = False, **changes):
    said = []
    ok = worker.install_service(options(home=str(home), **changes), {**mac, "agent": loaded}, ask=lambda q: answer,
                                run=lambda cmd: ran.append(" ".join(cmd)) or int(" ".join(cmd).startswith(fail or "\0")),
                                say=said.append, unit_dir=agents, running=lambda: up)
    return ok, "\n".join(said)


try:
    ran = []
    ok, said = install_mac("n", ran)
    assert not ok and ran == [] and not agents.exists(), "no: nothing done"
    ok, said = install_mac("", ran, nice=5)
    assert ok, said
    assert [r.split()[:2] for r in ran] == [["launchctl", "enable"], ["launchctl", "bootstrap"]], ran
    plist = plistlib.loads((agents / "org.bouldermesh.worker.plist").read_bytes())
    copy = home / "bouldermesh_worker.py"
    assert plist["ProgramArguments"] == [sys.executable, str(copy.resolve()), "--home", str(home.resolve()), "--nice", "5"], plist
    assert plist["KeepAlive"] == {"SuccessfulExit": False} and plist["RunAtLoad"] and plist["StandardOutPath"] == worker.AGENT_LOG
    assert copy.read_bytes() == (ROOT / "worker" / "bouldermesh_worker.py").read_bytes()
    assert worker.load_conf(home).get("service") == "bouldermesh-worker"
    assert "launchctl bootout gui/" in said and (scratch / "logs").is_dir()
    ran.clear()     # loaded as this would write it: nothing asked or run
    ok = worker.install_service(options(home=str(home), nice=5), {**mac, "agent": True}, ask=lambda q: 1 / 0,
                                run=lambda cmd: 1 / 0, say=lambda m: None, unit_dir=agents, running=lambda: True)
    assert ok
    ok, said = install_mac("y", ran, loaded=True)       # changed (no --nice now): unloaded, then loaded again
    assert ok and ran[0].startswith("launchctl bootout gui/") and ran[-1].startswith("launchctl bootstrap"), ran
    ran.clear()
    ok, said = install_mac("y", ran, fail="launchctl bootstrap")
    assert not ok and "exit status 1" in said
    pathlib.Path(worker.AGENT_LOG).write_text("one\ncan't use Docker: Cannot connect to the Docker daemon\n")
    ok, said = install_mac("y", [], up=False)
    assert not ok and "Cannot connect to the Docker daemon" in said and "tries again every minute" in said, said
    # the log starts again past AGENT_LOG_MOST, the one before kept
    most = worker.AGENT_LOG_MOST
    worker.AGENT_LOG_MOST = 10
    worker.trim_agent_log()
    assert pathlib.Path(worker.AGENT_LOG).stat().st_size == 0 and "Docker daemon" in pathlib.Path(worker.AGENT_LOG + ".old").read_text()
    worker.AGENT_LOG_MOST = most
finally:
    worker.AGENT_LOG = real_log
print("Mac agent OK")

# --- Docker Desktop's disk: what its sparse file may still grow by, and no more than the computer's own disk has
raw = scratch / "Docker.raw"
with open(raw, "wb") as f:
    f.truncate(64 * 2**30)      # a 64 GB limit, nothing in it yet
    f.write(b"x" * 2**20)
free = worker.desktop_disk_free(raw)
assert free is not None and free <= worker.free_gb(scratch) + 0.01 and free <= 64, free
assert worker.desktop_disk_free(scratch / "none") is None
real_run = worker.subprocess.run
worker.subprocess.run = lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": "Docker Desktop\n"})()
real_raws = worker.DESKTOP_RAW
worker.DESKTOP_RAW = (str(scratch / "none"), str(raw))
try:
    (where, room), = worker.docker_free()
    assert where.startswith(worker.DESKTOP_DISK) and abs(room - free) < 0.01, (where, room)
    worker.DESKTOP_RAW = (str(scratch / "none"),)
    assert worker.docker_free() == [], "Docker Desktop, but its disk not found: not known"
finally:
    worker.subprocess.run, worker.DESKTOP_RAW = real_run, real_raws
shutil.rmtree(scratch)
print("Docker Desktop's disk OK")

# --- nvidia-smi's reading of the first GPU: past warnings it prints first, and its utilization "[N/A]" where it can't say
assert worker.GpuWatch.sample("35, 1200, 8192\n12, 100, 24576\n") == (35.0, 1200.0, 8192.0)
assert worker.GpuWatch.sample("[N/A], 1200, 8192\n") == (None, 1200.0, 8192.0), "its memory is what matters"
assert worker.GpuWatch.sample("WARNING: infoROM is corrupted at gpu 0000:01:00.0\n35, 1200, 8192\n") == (35.0, 1200.0, 8192.0)
assert worker.GpuWatch.sample("") is None and worker.GpuWatch.sample("[N/A], [N/A], [N/A]\n") is None
print("reading the GPU OK")

# --- what it takes, by its GPU's memory in all: the CPU below any job's least, and no job its GPU could never hold
GPU = ["--gpus", "all"]
fit = worker.Worker.fit_gpu
gpu, tasks, small, said = fit(GPU, "auto", "NVIDIA GeForce GTX 1060 3GB", 3072, ())
assert gpu == [] and tasks == worker.CPU_TASKS and small == "NVIDIA GeForce GTX 1060 3GB (3.0 GB)" and "on the CPU" in said[0], "Gerty"
gpu, tasks, small, said = fit(GPU, "auto", "NVIDIA GeForce RTX 2060", 6144, ())
assert gpu == GPU and tasks == ("beta", "scale", "filter") and small is None, tasks
assert said == ["NVIDIA GeForce RTX 2060 (6.0 GB in all) can't hold reconstruct (7.4 GB), splat (7.8 GB): not taking those"], said
gpu, tasks, _, said = fit(GPU, "auto", "NVIDIA GeForce RTX 2070", 8192, ())
assert gpu == GPU and tasks == worker.TASKS and said == [], "blackbox: the server fits each job to what's free"
gpu, tasks, _, said = fit(GPU, "auto", "NVIDIA GeForce RTX 2060", 6144, ("reconstruct",))
assert gpu == GPU and tasks == ("reconstruct",) and "the server won't give it those" in said[0], "--tasks as given, but said"
gpu, tasks, _, said = fit(GPU, "auto", None, None, ())
assert gpu == [] and tasks == worker.CPU_TASKS and "doesn't say how much memory" in said[0], "unknown isn't unlimited"
assert fit(GPU, "all", None, None, ()) == (GPU, worker.TASKS, None, []), "--gpu all: as asked"
assert fit([], "auto", None, None, ()) == ([], worker.CPU_TASKS, None, []), "no GPU"
assert fit([], "none", None, None, ("filter",)) == ([], ("filter",), None, [])
print("fitting the GPU OK")

# --- at boot, a driver that doesn't answer yet is waited for; long after, or with no NVIDIA GPU there, it isn't
real = (worker.quiet, worker.since_boot, worker.nvidia_hardware, worker.shutil.which, worker.log)
answers, waits, logged = iter([1, 1, 0]), [], []
worker.quiet = lambda cmd: type("R", (), {"returncode": next(answers), "stdout": ""})()
worker.since_boot, worker.nvidia_hardware, worker.log = lambda: 40.0, lambda: True, logged.append
worker.shutil.which = lambda tool: "/usr/bin/" + tool
try:
    assert worker.driver_answers(sleep=waits.append) and waits == [10, 10] and len(logged) == 1, (waits, logged)
    worker.quiet = lambda cmd: type("R", (), {"returncode": 9, "stdout": ""})()
    worker.since_boot = lambda: 4000.0
    waits.clear()
    assert not worker.driver_answers(sleep=waits.append) and waits == [], "long after boot: no waiting"
    worker.since_boot, worker.nvidia_hardware = lambda: 40.0, lambda: False
    assert not worker.driver_answers(sleep=waits.append) and waits == [], "no NVIDIA GPU: nothing to wait for"
    ages = iter([40.0, 200.0, 400.0])
    worker.since_boot, worker.nvidia_hardware, logged[:] = lambda: next(ages), lambda: True, []
    assert not worker.driver_answers(sleep=waits.append) and waits == [10, 10] and "working without the GPU" in logged[-1]
    worker.shutil.which = lambda tool: None
    assert not worker.driver_answers(sleep=waits.append), "no driver installed"
finally:
    worker.quiet, worker.since_boot, worker.nvidia_hardware, worker.shutil.which, worker.log = real
print("waiting for the driver OK")

# --- a job's input: what remote_job.py's tasks are sent unpacks, anything else is refused
import io
import tarfile
with tempfile.TemporaryDirectory() as tmp:
    def packed(*names: str) -> pathlib.Path:
        f = pathlib.Path(tmp) / "in.tar"
        with tarfile.open(f, "w") as tar:
            for n in names:
                info = tarfile.TarInfo(n)
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
        return f
    worker.extract(packed("task.json", "photos/a.jpg", "scenery/pano_01.jpg", "scenery/walk_02.jpg"), pathlib.Path(tmp) / "job")
    assert (pathlib.Path(tmp) / "job" / "scenery" / "pano_01.jpg").is_file(), "a capture's scenery"
    try:
        worker.extract(packed("photos/a.jpg", "elsewhere/x"), pathlib.Path(tmp) / "job2")
        raise AssertionError("took a folder no job has")
    except RuntimeError as e:
        assert "shouldn't" in str(e)
print("a job's input OK")

# --- elsewhere: --setup says what to install by hand
assert worker.os_release() is not None
print("ALL WORKER SETUP TESTS PASSED")
