"""How long each step of making a mesh takes (harvesting, filtering, each meshing step, a boulder's harvest of its
climbs), kept for every run, so that the time left the progress bars and deploy.sh show gets better as runs pile up.

Each run is kept with what it was of (a climb's own harvest or a boulder's, its "kind"), where it ran ("server" or a
machine's name), and how much it had to do ("units": photos in the project as it ended, or a boulder's climbs for its
"climbs" step). A step's time is estimated from the runs most like it: of the same kind on the same machine when there
are enough, else more loosely, and of those, the ones nearest it in units (none more than twice as far off as the
nearest), each scaled by how much more or less it had to do. So a 300-photo boulder isn't told the time of a 20-photo climb, nor hal-2 the server's.
"""
from __future__ import annotations

import math
import statistics
import time

SCHEMA = """CREATE TABLE IF NOT EXISTS step_runs(
  project_id TEXT, step TEXT, kind TEXT, machine TEXT, units INTEGER, started REAL, seconds REAL,
  PRIMARY KEY(project_id, step, started))"""
LEAST = 3           # runs a set of them needs to go by
NEAREST = 7         # runs nearest in units an estimate is the median of
RECENT = 200        # the most recent runs of a step looked at
SCALE = (0.25, 4.0)  # how far a run's time is scaled for having had less or more to do


def row(pid: str, step: dict, kind: str | None, machine: str | None, units: int | None) -> tuple | None:
    """A finished step of a tracker's snapshot as a step_runs row, or None if it isn't one to keep (not done, or with
    no time)."""
    if step.get("state") != "done" or step.get("seconds") is None or not step.get("started"):
        return None
    return (pid, step["key"], kind, machine, step.get("units", units), step["started"], step["seconds"])


def estimate(runs: list[dict], step: str, units: int | None, kind: str | None = None, machine: str | None = None) -> float | None:
    """Seconds this step will probably take, from runs (step_runs rows, newest first), or None with too few.

    Args:
        units: how much it has to do (as a run's units), or None if unknown, for the median of the recent runs.
        kind, machine: prefer runs of this kind of project and on this machine ("server" or a name), each dropped in
            turn (machine first) while there are fewer than LEAST runs.
    """
    of = [r for r in runs if r["step"] == step]
    pool: list[dict] = []
    for k, m in dict.fromkeys(((kind, machine), (kind, None), (None, machine), (None, None))):
        pool = [r for r in of if (k is None or r["kind"] == k) and (m is None or r["machine"] == m)][:RECENT]
        if len(pool) >= LEAST:
            break
    if len(pool) < LEAST:
        return None
    sized = [r for r in pool if r["units"] is not None] if units is not None else []
    if len(sized) < LEAST:
        return statistics.median(r["seconds"] for r in pool[:30])
    far = lambda r: abs(math.log1p(r["units"]) - math.log1p(units))  # noqa: E731
    near = sorted(sized, key=far)[:NEAREST]
    near = [r for r in near if far(r) < far(near[0]) + math.log(2)]     # within twice the nearest's difference
    return statistics.median(r["seconds"] * min(max((units + 1) / (r["units"] + 1), SCALE[0]), SCALE[1]) for r in near)


def step_left(s: dict, now: float | None = None) -> tuple[float | None, float]:
    """A step's seconds left, and how long it's been going: at its own pace once that's clear (5% done, a minute in),
    else its "typical" (estimate()) less the time gone, and None when there's no telling (as the page's stepLeft())."""
    if s.get("state") not in ("running", "pending"):
        return 0.0, 0.0
    going = (now or time.time()) - s["started"] if s.get("state") == "running" and s.get("started") else 0.0
    if going >= 60 and s.get("frac", 0) >= 0.05:
        return going * (1 - s["frac"]) / s["frac"], going
    t = s.get("typical")
    return (t - going if t is not None and t > going else None), going


def job_left(progress: dict | None, now: float | None = None) -> float | None:
    """A job's seconds left, from its steps' (step_left()), a step alongside the others counting only for as long as
    it outlasts them; None if any step's can't be told (as the page's jobTiming())."""
    left = 0.0
    for s in (progress or {}).get("steps", []):
        t, _ = step_left(s, now)
        if t is None:
            return None
        left = max(left, t) if s.get("parallel") else left + t
    return left
