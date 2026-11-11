#!/usr/bin/env python3
"""The worker script's --setup (installing Docker and NVIDIA's container toolkit where they're missing) and its check of
Docker when it starts, on machines described here rather than looked at. Offline: nothing is installed or run."""
from __future__ import annotations

import importlib.util
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("worker", ROOT / "worker" / "bouldermesh_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def machine(**changes) -> dict:
    """A machine as machine_state() describes one: by default hal, with everything in place."""
    st = {"linux": True, "debian": True, "system": "Ubuntu 26.04 LTS", "root": False, "user": "pat", "docker": True,
          "docker_state": "ok", "runtimes": ["io.containerd.runc.v2", "nvidia", "runc"], "running": 0, "in_group": True,
          "gpu": True, "gpu_hardware": True, "toolkit": True, "have": {"curl": True, "gpg": True}}
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
assert steps[3]["commands"] == ["sudo nvidia-ctk runtime configure --runtime=docker"]
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
assert [s["commands"][0] for s in steps][:2] == ["sudo nvidia-ctk runtime configure --runtime=docker", "sudo systemctl restart docker"]
assert steps[1]["warn"] is None, "nothing running: nothing to warn of"
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

# --- elsewhere: --setup says what to install by hand
assert worker.os_release() is not None
print("ALL WORKER SETUP TESTS PASSED")
