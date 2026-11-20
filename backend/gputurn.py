"""One GPU step at a time among the jobs on one machine, so that two jobs never run the GPU out of memory together.

A machine's worker script runs each job in a container of its own, and more than one worker may run on a machine. The
GPU steps of a job (SIFT, matching, VGGT, OpenMVS's depth maps, finding people, a splat's fitting) each take the GPU in
turn() first: a lock on a file all the machine's workers give their jobs (GPU_LOCK, bind-mounted by the worker script).
A job whose turn hasn't come waits, rather than failing or falling back to the CPU; its CPU steps (the mapper above all)
go on meanwhile alongside the other job's. Without GPU_LOCK (a job run by the server itself, or by an older worker),
turn() does nothing. Others' use of the GPU (not BoulderMesh's) isn't held back by this: the steps' own checks of free
memory, and their fallbacks to the CPU, are still what deal with that.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import sys
import time
from typing import Callable, Iterator

_held = 0     # how deep in turn() this process is: the lock is taken by the outermost only


@contextlib.contextmanager
def turn(what: str, log: Callable[[str], None]) -> Iterator[None]:
    """Hold the machine's GPU for a step, waiting for any other job's step to finish first.

    Args:
        what: the step, for the log's lines when it waits.
        log: where to say it waits, and for how long it did.
    """
    global _held
    path = os.environ.get("GPU_LOCK")
    if not path or _held:
        _held += 1
        try:
            yield
        finally:
            _held -= 1
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"{what}: waiting for the GPU, which another job on this machine is using")
            start = time.time()
            fcntl.flock(fd, fcntl.LOCK_EX)
            log(f"{what}: the GPU's free, after waiting {time.time() - start:.0f} s")
        _held = 1
        try:
            yield
        finally:
            _held = 0
            _empty_cache()
    finally:
        os.close(fd)    # lets go of the lock


def _empty_cache() -> None:
    """Give back the GPU memory torch keeps cached in this process, for the next job's step to have."""
    torch = sys.modules.get("torch")
    try:
        if torch is not None and torch.cuda.is_initialized():
            torch.cuda.empty_cache()
    except Exception:  # noqa: S110 - only tidying
        pass
