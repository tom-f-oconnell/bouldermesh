"""How a mesh is made, where an area chooses (its More options): for the thread making it, read by the pipeline here,
or sent with the job to a machine elsewhere, as task.json's "mapper" (remote_job.py).

COLMAP's mapper can place many fewer photos of the same set from one run to the next (19 of 19, then 13, for one
boulder): when the model it keeps places under retry_below of the photos, it tries this many more seeds, and the model
placing most is kept.
"""
from __future__ import annotations

import contextlib
import threading

# its defaults, wherever no area says otherwise: no more seeds since Nov 11, 2026, as over 5 runs they took 72 minutes
# for 3 more photos, and placing the rest (pipeline._place_rest()) recovers photos the mapper left out for far less
# texture_every: a mesh is textured from every still and every this-many-th frame of each video (2: every second, which
# halved the step and its CPU time on two boulders with the texture's sharpness unchanged on one and lower on the other:
# benchmarking.md; 1: all of them; since Nov 16 stills are all kept: pipeline.texture_views()). The whole site's, from the admin page (set_site()), not an area's. OpenMVS 2.4 on.
# method: which mapper places the cameras (pipeline.mapper_method()): "incremental" (3.9.1's) or "global" (COLMAP 4.2.1's,
# the default Nov 15-17, until it was found to misplace groups of cameras: experiments/mapper/global-mapper-issues.md);
# the whole site's, from the admin page, unless MAPPER is set in the environment
MAPPER = {"retry_below": 0.75, "more": 0, "texture_every": 2, "method": "incremental"}
_local = threading.local()
_site: dict = {}        # what the whole site sets otherwise than the defaults (set_site())


def set_site(options: dict) -> None:
    """What the admin page's settings say of how meshes are made, where it isn't the default: sent with every job."""
    global _site
    _site = {k: v for k, v in options.items() if k in MAPPER and (v != MAPPER[k] or k == "method")}  # the mapper always:
    # a machine on an image from before its default changed would otherwise use that image's default


def mapper() -> dict:
    """The mapper's settings for the mesh this thread is making."""
    return {**MAPPER, **(given() or {})}


def placing() -> dict:
    """mapper(), of what decides how photos are placed: what a mesh's record of what it was made from keeps, and what
    it's compared by later. How it's textured isn't among them, so changing that makes no mesh out of date."""
    return {k: v for k, v in mapper().items() if k not in NOT_COMPARED}


TEXTURE_ONLY = ("texture_every",)
# not kept in inputs.json either: which mapper placed a mesh is in its own mapper.json, and switching the site's
# (the admin page) is a choice for meshes made from then on, not a reason to call every mesh made before out of date
NOT_COMPARED = TEXTURE_ONLY + ("method",)


def given() -> dict | None:
    """What an area, or the whole site, set for the mesh this thread is making, if anything: what a job elsewhere is sent."""
    return {**_site, **(getattr(_local, "mapper", None) or {})} or None


@contextlib.contextmanager
def using(mapper: dict | None):
    """The mapper's settings for what this thread meshes inside it ({"retry_below": 0-1, "more": seeds}), or None for
    the defaults."""
    before = getattr(_local, "mapper", None)
    _local.mapper = {k: mapper[k] for k in MAPPER if k in mapper} if mapper else None
    try:
        yield
    finally:
        _local.mapper = before
