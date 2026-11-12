#!/usr/bin/env python3
"""What gets recorded when a tool fails, with real processes killed in real ways. Offline."""
from __future__ import annotations

import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
import diagnostics  # noqa: E402
import pipeline  # noqa: E402
from climbs.common import Cancelled, jobctl  # noqa: E402
from diagnostics import ToolFailed, describe_exit  # noqa: E402

# ---- saying how something ended
assert describe_exit(1) == "exited with code 1"
assert describe_exit(143) == "exited with code 143, which usually means something inside it got SIGTERM", describe_exit(143)
assert describe_exit(127) == "exited with code 127"
assert describe_exit(-11) == "crashed with SIGSEGV (signal 11)"
assert describe_exit(-6) == "crashed with SIGABRT (signal 6)"
assert describe_exit(-15) == "was killed with SIGTERM (signal 15), not by this app"
assert describe_exit(-15, "sent from outside this container, by uid 61876") == \
    "was killed with SIGTERM (signal 15), not by this app: sent from outside this container, by uid 61876"
assert "out-of-memory killer fired 2 time(s)" in describe_exit(-9, oom_kills=2)
assert str(ToolFailed("/usr/local/bin/OpenMVS/DensifyPointCloud", -15)).startswith("DensifyPointCloud was killed with SIGTERM")
assert str(ToolFailed("ffmpeg", 1, detail="no such file")) == "ffmpeg exited with code 1: no such file"
assert isinstance(ToolFailed("x", 1), RuntimeError), "falling back to the CPU and retrying the mapper catch RuntimeError"

# ---- who sent it, from what signal_note prints
assert diagnostics.read_sender("Estimated depth-maps 21") is None
assert diagnostics.read_sender("signal_note: signal 15 code 0 from pid 0 uid 61876") == "sent from outside this container, by uid 61876"
assert diagnostics.read_sender("signal_note: signal 15 code 128 from pid 0 uid 0") == "sent by the kernel"
assert diagnostics.read_sender(f"signal_note: signal 15 code 0 from pid {os.getpid()} uid 0").startswith(f"sent by process {os.getpid()} (")
assert "already gone" in diagnostics.read_sender("signal_note: signal 15 code 0 from pid 99999999 uid 3")
print("descriptions OK")

# ---- real tools through the real _run
tmp = pathlib.Path(tempfile.mkdtemp())


def run(*cmd: str) -> tuple[list[str], ToolFailed | None]:
    logged = []
    try:
        pipeline._run(list(cmd), tmp, logged.append)
    except ToolFailed as e:
        return logged, e
    return logged, None


logged, failed = run("sh", "-c", "echo hello; exit 0")
assert failed is None and "hello" in logged
assert logged[-1].startswith("-- ran "), logged
assert not any(line.startswith("diagnostics:") for line in logged), "only a failure needs these"

logged, failed = run("sh", "-c", "exit 3")
assert failed.code == 3 and str(failed) == "sh exited with code 3", failed
assert str(failed) in logged, logged
report = [line for line in logged if line.startswith("diagnostics:")]
assert any("lowest memory available" in line and "out-of-memory kills while it ran: 0" in line for line in report), report
assert any("machine now:" in line and "memory available" in line and "GB disk free" in line for line in report), report

logged, failed = run("sh", "-c", "kill -TERM $$; sleep 5")
assert failed.code == -15 and "SIGTERM (signal 15), not by this app" in str(failed), failed

logged, failed = run("sh", "-c", "kill -SEGV $$; sleep 5")
assert failed.code == -11 and "crashed with SIGSEGV" in str(failed) and "not by this app" not in str(failed), failed

logged, failed = run("sh", "-c", "kill -KILL $$; sleep 5")
assert failed.code == -9 and "SIGKILL (signal 9), not by this app" in str(failed), failed

# stopping a job isn't a failure, and it's the one kill that is ours
jobctl.begin()
threading.Timer(0.5, jobctl.stop).start()
try:
    run("sleep", "30")
    raise AssertionError("should have been stopped")
except Cancelled:
    pass
jobctl.begin()

# a tool that uses memory has it noted
if sys.executable:
    logged, failed = run(sys.executable, "-c", "import time; x = bytearray(150 * 2**20); x[::4096] = b'1' * len(x[::4096]); time.sleep(5)")
    assert failed is None, failed
    assert "using up to 0." in logged[-1] or "using up to 1." in logged[-1], logged[-1]

# a harvest's tools say what went wrong too
try:
    jobctl.run(["sh", "-c", "echo 'ERROR: video unavailable' >&2; exit 1"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    raise AssertionError("should have failed")
except ToolFailed as e:
    assert str(e) == "sh exited with code 1: ERROR: video unavailable", e
print("real tools OK")

# ---- running out of memory, as a watchdog sees it
watch = diagnostics.ToolWatch(os.getpid())
watch.stop()
watch.lowest = (2 * diagnostics.GB, 0, watch.started)
watch.total = 100 * diagnostics.GB
assert any("memory watchdog" in line for line in watch.report(-15, tmp)), "short of memory, killed from outside"
assert not any("memory watchdog" in line for line in watch.report(-11, tmp)), "a crash isn't that"
watch.lowest = (50 * diagnostics.GB, 0, watch.started)
assert not any("memory watchdog" in line for line in watch.report(-15, tmp)), "plenty left, so it wasn't that"

# ---- failures that aren't a tool's
logged = []
try:
    {}["missing"]
except KeyError as e:
    diagnostics.log_failure(logged.append, e, tmp)
assert "Traceback" in logged[0] and "machine now:" in logged[1] and logged[2] == "FAILED: KeyError: 'missing'", logged
logged = []
try:
    raise RuntimeError("only 3 image(s) could be registered")
except RuntimeError as e:
    diagnostics.log_failure(logged.append, e, tmp)
assert logged[-1] == "FAILED: only 3 image(s) could be registered", logged
logged = []
diagnostics.log_failure(logged.append, ToolFailed("colmap", -15), tmp)
assert logged == ["FAILED: colmap was killed with SIGTERM (signal 15), not by this app"], "it logged the rest when it failed"
print("failures OK")

# ---- signal_note itself: only if this machine can build it, which the Docker image always does
gcc = shutil.which("gcc")
if gcc:
    lib = tmp / "signal_note.so"
    source = pathlib.Path(__file__).resolve().parent.parent / "backend" / "signal_note.c"
    subprocess.run([gcc, "-shared", "-fPIC", "-O2", "-Wall", "-Werror", "-o", str(lib), str(source)], check=True)
    env = {**os.environ, "LD_PRELOAD": str(lib)}
    proc = subprocess.Popen(["sleep", "30"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    out = proc.communicate(timeout=10)[0]
    assert proc.returncode == -15, f"it must still die of the same signal: {proc.returncode}"
    line = next(x for x in out.splitlines() if x.startswith("signal_note:"))
    assert diagnostics.read_sender(line) == f"sent by process {os.getpid()} ({pathlib.Path('/proc/self/comm').read_text().strip()}), uid {os.getuid()}", line
    # one it was told to ignore stays ignored, as nohup relies on
    proc = subprocess.Popen(["sh", "-c", "trap '' TERM; exec sleep 3"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    out = proc.communicate(timeout=10)[0]
    assert proc.returncode == 0 and "signal_note" not in out, (proc.returncode, out)
    # a many-threaded tool dies the same way
    code = ("import threading, time\n"
            "for _ in range(8): threading.Thread(target=time.sleep, args=(30,), daemon=True).start()\n"
            "time.sleep(30)")
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1)
    proc.send_signal(signal.SIGTERM)
    out = proc.communicate(timeout=10)[0]
    assert proc.returncode == -15 and "signal_note: signal 15" in out, (proc.returncode, out)
    print("signal_note OK")
else:
    print("signal_note skipped: no gcc")

# what a hung process's threads are doing: the busiest first, each Python thread's stack, and py-spy's native dump once
# this process lets it attach (Yama only lets a parent, otherwise)
stop = threading.Event()


def spinner():
    while not stop.is_set():
        pass


threading.Thread(target=spinner, daemon=True, name="spinner").start()
time.sleep(1.0)
try:
    rows = diagnostics.thread_table()
    assert rows[0]["user"] + rows[0]["system"] > 0.3 and {"tid", "name", "state", "voluntary", "involuntary"} <= set(rows[0]), rows[0]
    assert diagnostics.allow_tracing(), "Linux, with Yama or without it"
    path = diagnostics.dump_threads(tmp / "diagnostics", "a test")
    text = path.read_text()
    assert path.parent == tmp / "diagnostics" and "a test" in text and "[python: spinner]" in text, text[:800]
    assert "--- python thread spinner" in text and "in spinner" in text, text
    if "isn't installed" not in text:
        assert '"spinner"' in text.split("--- py-spy dump --native:")[1], text.split("--- py-spy dump --native:")[1][:600]
        print("thread dump, with py-spy's native frames OK")
    else:
        print("thread dump OK (no py-spy here)")
finally:
    stop.set()

shutil.rmtree(tmp, ignore_errors=True)
print("ALL DIAGNOSTICS TESTS PASSED")
