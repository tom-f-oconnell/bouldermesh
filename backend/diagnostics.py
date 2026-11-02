"""What to record when a tool or a job fails, so that the cause can be worked out afterwards.

A killed tool can't say why, and its parent only learns which signal it was: signal_note.c records who
sent it. By the time a tool has been killed the memory it used is free again, so ToolWatch samples
memory while it runs.

The app only ever kills its own tools with SIGKILL, when someone stops a job, and that ends as
Cancelled before any of this. So any other tool killed by a signal was killed by something else.
"""
from __future__ import annotations

import os
import re
import resource
import shutil
import signal
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

SIGNAL_NOTE_LIB = Path("/usr/local/lib/signal_note.so")
SENDER = re.compile(r"^signal_note: signal (\d+) code (-?\d+) from pid (\d+) uid (\d+)")
SI_KERNEL = 128
CRASH_SIGNALS = {signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS, signal.SIGFPE, signal.SIGILL}
SAMPLE_SECONDS = 2.0
SHORT_OF_MEMORY = 0.10  # of the total, roughly where a memory watchdog starts killing things
GB = 2**30


def signal_name(number: int) -> str | None:
    try:
        return signal.Signals(number).name
    except ValueError:
        return None


def describe_exit(code: int, sender: str | None = None, oom_kills: int = 0) -> str:
    """Say how a process ended.

    Args:
        code: its return code, which is negative for the signal that killed it.
        sender: who sent that signal, if known.
        oom_kills: how many times the kernel's out-of-memory killer fired while it ran.
    """
    if code >= 0:
        name = signal_name(code - 128) if code > 128 else None
        return f"exited with code {code}" + (f", which usually means something inside it got {name}" if name else "")
    name = signal_name(-code) or "an unknown signal"
    if -code in CRASH_SIGNALS:
        return f"crashed with {name} (signal {-code})"
    text = f"was killed with {name} (signal {-code}), not by this app"
    if sender:
        return f"{text}: {sender}"
    if oom_kills:
        return f"{text}: the kernel's out-of-memory killer fired {oom_kills} time(s) while it ran"
    return text


class ToolFailed(RuntimeError):
    """A tool ended with something other than success."""

    def __init__(self, tool: str, code: int, sender: str | None = None, oom_kills: int = 0, detail: str = ""):
        self.tool = Path(tool).name
        self.code = code
        super().__init__(f"{self.tool} {describe_exit(code, sender, oom_kills)}" + (f": {detail}" if detail else ""))


def last_line(output: str | bytes | None) -> str:
    """The last thing a tool said, which is usually why it stopped."""
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else output or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def tool_env() -> dict | None:
    """The environment to run a tool in: None for the app's own, if signal_note isn't built here."""
    return {**os.environ, "LD_PRELOAD": str(SIGNAL_NOTE_LIB)} if SIGNAL_NOTE_LIB.exists() else None


def read_sender(line: str) -> str | None:
    """Who sent a signal, from the line signal_note printed, or None if this isn't that line."""
    m = SENDER.match(line)
    if not m:
        return None
    code, pid, uid = int(m[2]), int(m[3]), int(m[4])
    if code == SI_KERNEL:
        return "sent by the kernel"
    if pid == 0:
        return f"sent from outside this container, by uid {uid}"
    try:
        name = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        name = "already gone"
    return f"sent by process {pid} ({name}), uid {uid}"


def meminfo() -> dict[str, int]:
    """/proc/meminfo in bytes. This is the whole machine's, even in a container."""
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            out[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def oom_kills() -> tuple[int, int]:
    """How many processes the kernel's out-of-memory killer has killed, on the machine and in this container."""
    def count(path: str, key: str) -> int:
        try:
            for line in Path(path).read_text().splitlines():
                name, value = line.split()[:2]
                if name == key:
                    return int(value)
        except (OSError, ValueError):
            pass
        return 0
    return count("/proc/vmstat", "oom_kill"), count("/sys/fs/cgroup/memory.events", "oom_kill")


def gpu_free_mb() -> int | None:
    """Free memory on the emptiest GPU visible here, or None if there isn't one."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    free = [int(x) for x in r.stdout.split() if x.isdigit()]
    return max(free) if r.returncode == 0 and free else None


def machine_state(data_dir: Path | str = "/data") -> str:
    """How busy the machine is right now, in a line for a log."""
    parts = []
    mem = meminfo()
    if mem:
        parts.append(f"{mem['MemAvailable'] / GB:.1f} of {mem['MemTotal'] / GB:.1f} GB memory available, "
                     f"{mem['SwapFree'] / GB:.1f} of {mem['SwapTotal'] / GB:.1f} GB swap free")
    try:
        load = Path("/proc/loadavg").read_text().split()[:3]
        parts.append(f"load {' '.join(load)} on {os.cpu_count()} cores")
    except OSError:
        pass
    try:
        full = next(line for line in Path("/proc/pressure/memory").read_text().splitlines() if line.startswith("full"))
        parts.append("memory pressure " + full.split()[1])
    except (OSError, StopIteration, IndexError):
        pass
    try:
        parts.append(f"{shutil.disk_usage(data_dir).free / GB:.0f} GB disk free")
    except OSError:
        pass
    gpu = gpu_free_mb()
    if gpu is not None:
        parts.append(f"{gpu} MB GPU memory free")
    return "; ".join(parts)


class ToolWatch:
    """Keeps an eye on a running tool and on the machine's memory, from its own thread."""

    def __init__(self, pid: int):
        self.pid = pid
        self.started = time.monotonic()
        self.seconds = 0.0
        self.peak = 0                   # bytes of memory the tool has used at most
        self.lowest: tuple[int, int, float] | None = None   # bytes available, bytes of swap free, when
        self.total = 0
        self.oom_kills = 0
        self._ooms = oom_kills()
        self._cpu = self._children_cpu()
        self.cpu = 0.0
        self._done = threading.Event()
        self._sample()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @staticmethod
    def _children_cpu() -> float:
        r = resource.getrusage(resource.RUSAGE_CHILDREN)
        return r.ru_utime + r.ru_stime

    def _sample(self) -> None:
        mem = meminfo()
        if mem:
            self.total = mem["MemTotal"]
            if self.lowest is None or mem["MemAvailable"] < self.lowest[0]:
                self.lowest = (mem["MemAvailable"], mem["SwapFree"], time.monotonic())
        try:
            for line in Path(f"/proc/{self.pid}/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    self.peak = max(self.peak, int(line.split()[1]) * 1024)
        except (OSError, ValueError, IndexError):
            pass

    def _loop(self) -> None:
        while not self._done.wait(SAMPLE_SECONDS):
            self._sample()

    def stop(self) -> None:
        """Call once the tool has ended."""
        self._done.set()
        self._thread.join(timeout=2)
        self.seconds = time.monotonic() - self.started
        machine, container = oom_kills()
        self.oom_kills = max(machine - self._ooms[0], container - self._ooms[1])
        self.cpu = self._children_cpu() - self._cpu

    def summary(self) -> str:
        """A line for the log about any run of a tool, as how big they get is useful in itself."""
        sampled = self.peak and self.seconds >= SAMPLE_SECONDS    # anything shorter ended before it was seen
        peak = f"up to {self.peak / GB:.1f} GB of memory, " if sampled else ""
        return f"-- ran {self.seconds:.0f} s, using {peak}{self.cpu:.0f} s of CPU"

    def report(self, code: int, data_dir: Path | str = "/data") -> list[str]:
        """Lines for the log about a run that failed."""
        lines = []
        short = False
        if self.lowest:
            available, swap, when = self.lowest
            short = available < SHORT_OF_MEMORY * self.total
            lines.append(f"diagnostics: lowest memory available while it ran: {available / GB:.1f} of {self.total / GB:.1f} GB, "
                         f"{self.started + self.seconds - when:.0f} s before it ended, with {swap / GB:.1f} GB swap free; "
                         f"out-of-memory kills while it ran: {self.oom_kills}")
        lines.append(f"diagnostics: machine now: {machine_state(data_dir)}")
        if code in (-signal.SIGTERM, -signal.SIGKILL) and short:
            lines.append("diagnostics: memory was nearly out, so a memory watchdog (like earlyoom) or the kernel is the likeliest sender")
        return lines


def log_failure(log: Callable[[str], None], e: BaseException, data_dir: Path | str = "/data") -> None:
    """Log what ended a job, as a line starting "FAILED: " that the UI shows, after anything else useful."""
    if not isinstance(e, ToolFailed):   # a tool that failed has already logged all this
        log(traceback.format_exc().rstrip())
        log(f"diagnostics: machine now: {machine_state(data_dir)}")
    log(f"FAILED: {e if isinstance(e, RuntimeError) else f'{type(e).__name__}: {e}'}")
