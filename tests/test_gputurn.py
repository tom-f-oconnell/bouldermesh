#!/usr/bin/env python3
"""Jobs' GPU steps taking turns (backend/gputurn.py), as two workers' jobs on one machine do: a second process's step
waits for the first's to finish, a step inside another takes no second lock, and without GPU_LOCK nothing waits. Also
the worker script's mount of the lock, and its not holding back for another worker's job. Offline, no GPU.

    python3 tests/test_gputurn.py
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import gputurn  # noqa: E402

HOLD = """
import sys, time
sys.path.insert(0, {backend!r})
import gputurn
with gputurn.turn("first", print):
    print("holding", flush=True)
    time.sleep({seconds})
"""

with tempfile.TemporaryDirectory() as tmp:
    lock = Path(tmp) / "gpu.lock"
    env = {**os.environ, "GPU_LOCK": str(lock)}
    first = subprocess.Popen([sys.executable, "-c", HOLD.format(backend=str(ROOT / "backend"), seconds=1.5)], env=env,
                             stdout=subprocess.PIPE, text=True)
    assert first.stdout.readline().strip() == "holding"
    said: list[str] = []
    os.environ["GPU_LOCK"] = str(lock)
    start = time.time()
    with gputurn.turn("densify", said.append):
        waited = time.time() - start
        with gputurn.turn("inner", said.append):     # no second lock of its own, which would wait for itself
            pass
    first.wait()
    assert waited > 0.8, f"the second step waited for the first's: {waited:.2f} s"
    assert said[0] == "densify: waiting for the GPU, which another job on this machine is using", said
    assert said[1].startswith("densify: the GPU's free, after waiting") and len(said) == 2, said

    start, said = time.time(), []
    with gputurn.turn("free", said.append):     # the lock let go: no waiting, and nothing said
        pass
    assert time.time() - start < 0.5 and not said, said

    del os.environ["GPU_LOCK"]
    first = subprocess.Popen([sys.executable, "-c", HOLD.format(backend=str(ROOT / "backend"), seconds=1.5)], env=env,
                             stdout=subprocess.PIPE, text=True)
    assert first.stdout.readline().strip() == "holding"
    start = time.time()
    with gputurn.turn("no lock", said.append):    # a job run by the server, or by an older worker
        pass
    assert time.time() - start < 0.5 and not said
    first.wait()

    try:
        with gputurn.turn("fails", print):
            raise RuntimeError("the step failed")
    except RuntimeError:
        pass
    assert gputurn._held == 0, "a failed step lets go of its turn"

spec = importlib.util.spec_from_file_location("worker", ROOT / "worker" / "bouldermesh_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
calls: list[list[str]] = []
worker.quiet = lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "3f2a1b\n", "")
assert worker.GpuWatch.siblings() and calls[-1][-1] == "name=^bmw-"
worker.quiet = lambda cmd: subprocess.CompletedProcess(cmd, 0, "", "")
assert not worker.GpuWatch.siblings()
worker.quiet = lambda cmd: None
assert not worker.GpuWatch.siblings(), "no Docker to ask: nothing of a sibling's to count"
source = (ROOT / "worker" / "bouldermesh_worker.py").read_text()
assert '"-v", f"{lock}:/gpu.lock", "-e", "GPU_LOCK=/gpu.lock"' in source

print("ok")
