"""How a mesh is made, where an area chooses (its More options): for the thread making it, read by the pipeline here,
or sent with the job to a machine elsewhere, as task.json's "mapper" (remote_job.py).

COLMAP's mapper can place many fewer photos of the same set from one run to the next (19 of 19, then 13, for one
boulder): when the model it keeps places under retry_below of the photos, it tries this many more seeds, and the model
placing most is kept.
"""
from __future__ import annotations

import contextlib
import threading

MAPPER = {"retry_below": 0.75, "more": 1}   # its defaults, wherever no area says otherwise
_local = threading.local()


def mapper() -> dict:
    """The mapper's settings for the mesh this thread is making."""
    return {**MAPPER, **(given() or {})}


def given() -> dict | None:
    """What an area set for the mesh this thread is making, if anything: what a job elsewhere is sent."""
    return getattr(_local, "mapper", None)


@contextlib.contextmanager
def using(mapper: dict | None):
    """The mapper's settings for what this thread meshes inside it ({"retry_below": 0-1, "more": seeds}), or None for
    the defaults."""
    before = given()
    _local.mapper = {k: mapper[k] for k in MAPPER if k in mapper} if mapper else None
    try:
        yield
    finally:
        _local.mapper = before
