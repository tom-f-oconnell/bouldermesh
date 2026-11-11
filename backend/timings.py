"""How long each mesh took to make, kept with it (timings.json in its folder), to compare machines, pipelines and
settings: on the mesh's details, and summed up on the admin page.

From what any run has, wherever it ran: the progress tracker's steps (progress.py), which say how long each step took,
harvest steps too when the same job harvested; and the job log's line for each tool the pipeline ran ("-- ran N s,
using up to X GB of memory, Y s of CPU", diagnostics.Usage.summary()), relayed from the machine that ran it, which adds
CPU time and memory. Meshes made before this was kept get it from their project's log (backfill()), marked "from_log".
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

FILE = "timings.json"
RAN = re.compile(r"^-- ran (\d+) s, using (?:up to ([\d.]+) GB of memory, )?(\d+) s of CPU")
WAITED = re.compile(r"^remote: (.+?) (?:\(GPU\) )?took it after (\d+) s")
RUNNING_ON = re.compile(r"^remote: running on (.+?)( with its GPU)?$")
PLACED = re.compile(r"registered (\d+)/(\d+) images")
VGGT = re.compile(r"^VGGT: placed them in (\d+) s")
START = ("== Finding people to ignore", "== COLMAP: feature extraction")
BANDS = ((0, 20), (20, 40), (40, 80), (80, None))       # photos in, for comparing like with like


def parse_log(text: str) -> dict:
    """What a reconstruction's part of a job log says of its run: each tool run ({step, seconds, cpu, peak_gb}), how
    many photos went in and were placed, how long VGGT took to place them (if it did), and for a job another machine
    took, which one, whether with its GPU, and how long the job waited for it."""
    out: dict = {"tools": [], "photos_in": None, "placed": None, "vggt_seconds": None, "machine": None, "gpu": None,
                 "waited": None}
    step = None
    for line in text.splitlines():
        if line.startswith("== "):
            step = line[3:].strip()
        elif m := RAN.match(line):
            out["tools"].append({"step": step, "seconds": int(m[1]), "cpu": int(m[3]),
                                 **({"peak_gb": float(m[2])} if m[2] else {})})
        elif m := PLACED.search(line):
            out["placed"], out["photos_in"] = int(m[1]), int(m[2])
        elif m := VGGT.match(line):
            out["vggt_seconds"] = int(m[1]) + sum(t["seconds"] for t in out["tools"] if (t["step"] or "").startswith("VGGT"))
        elif m := WAITED.match(line):
            out["machine"], out["waited"] = m[1], int(m[2])
        elif m := RUNNING_ON.match(line):
            out["gpu"] = bool(m[2])
    if out["vggt_seconds"] is not None:     # with its rounds of COLMAP points from what it placed
        rounds = [t["seconds"] for t in out["tools"] if "points from those placements" in (t["step"] or "")]
        out["vggt_seconds"] += sum(rounds)
    return out


def record(log_text: str, progress: dict | None, started: float, ended: float, where: str, version: str | None,
           remote: bool, mesh_keys: set[str] | None = None) -> dict:
    """A mesh's timings, as run_reconstruct() keeps them when it saves the mesh.

    Args:
        log_text: the job log from when this reconstruction began.
        progress: the job's tracker snapshot, whose steps say how long each took (harvest steps too, if this job
            harvested first).
        started, ended: when the reconstruction began, and when its mesh was saved.
        where: what made it: "this server", or the machine's name.
        mesh_keys: the reconstruction's own steps (pipeline.steps()), to tell them from a harvest's before them.
    """
    parsed = parse_log(log_text)
    steps = [{"key": s["key"], "label": s["label"], "seconds": s["seconds"],
              **({"part": "mesh" if s["key"] in mesh_keys else "harvest"} if mesh_keys is not None else {})}
             for s in (progress or {}).get("steps", []) if s.get("seconds") is not None]
    harvest = [s["seconds"] for s in steps if s.get("part") == "harvest"]
    tools = parsed.pop("tools")
    return {"version": version, "where": parsed.pop("machine") or where, "remote": remote, "started": round(started, 1),
            "ended": round(ended, 1), "wall": round(ended - started, 1), "tools_seconds": sum(t["seconds"] for t in tools),
            "cpu_seconds": sum(t["cpu"] for t in tools), "peak_gb": max((t.get("peak_gb", 0) for t in tools), default=0) or None,
            "harvest_seconds": round(sum(harvest), 1) if harvest else None, **parsed, "steps": steps, "tools": tools}


def save(mesh_dir: Path, rec: dict) -> None:
    (Path(mesh_dir) / FILE).write_text(json.dumps(rec, indent=1))


def load(mesh_dir: Path) -> dict | None:
    try:
        return json.loads((Path(mesh_dir) / FILE).read_text())
    except (OSError, ValueError):
        return None


def last_run(log_text: str) -> str | None:
    """The part of a job log of its last reconstruction that saved a mesh (with what a machine that took it said
    first, and the tool's line after), or None."""
    lines = log_text.splitlines()
    end = max((i for i, line in enumerate(lines) if "Mesh saved" in line), default=None)
    if end is None:
        return None
    start = max((i for i in range(end + 1) if lines[i].startswith(START)), default=None)
    if start is None:
        return None
    if lines[start].startswith(START[1]):
        before = max((i for i in range(start) if lines[i].startswith("== ")), default=None)
        if before is not None and lines[before].startswith(START[0]):
            start = before
    head = max((i for i in range(max(0, start - 10), start) if lines[i].startswith("remote: ")), default=None)
    while head is not None and head > 0 and lines[head - 1].startswith("remote: "):
        head -= 1
    tail = end + 1
    while tail < min(len(lines), end + 40) and not lines[tail].startswith("== "):
        tail += 1
    return "\n".join(lines[start if head is None else head:tail])


def backfill(meshes: list[dict], projects: Path, meshes_dir: Path, version: str | None = None) -> int:
    """Timings for meshes made before they were kept, from their project's job log: its last run that saved a mesh.
    Approximate: only the tools' times, with no wait or transfer, and when it ended taken as the mesh's creation.

    Args:
        meshes: rows with id, source ("project:<id>") and created.

    Returns:
        How many got timings.
    """
    n = 0
    for m in meshes:
        mdir = meshes_dir / m["id"]
        if not mdir.is_dir() or (mdir / FILE).exists() or not (m.get("source") or "").startswith("project:"):
            continue
        try:
            text = (projects / m["source"].split(":", 1)[1] / "job.log").read_text(errors="replace")
        except OSError:
            continue
        run = last_run(text)
        if not run:
            continue
        rec = record(run, None, m["created"], m["created"], "this server", version, "remote: running on" in run)
        rec.update(started=None, wall=None, from_log=True)
        save(mdir, rec)
        n += 1
    return n


def band(photos: int | None) -> str | None:
    for lo, hi in BANDS:
        if photos is not None and photos >= lo and (hi is None or photos < hi):
            return f"{lo}-{hi}" if hi else f"{lo}+"
    return None


def summarize(recs: list[dict], since: float | None = None) -> list[dict]:
    """Medians for the admin page, by what made them and how many photos went in: [{where, photos, n, minutes (the
    tools' time), cpu_minutes, wall_minutes (from starting to the mesh saved, with any wait and transfer), waited
    (seconds, for a machine), vggt (how many VGGT placed), vggt_seconds}], those since `since` only if given."""
    groups: dict[tuple, list[dict]] = {}
    for r in recs:
        if since and (r.get("ended") or 0) < since:
            continue
        groups.setdefault((r.get("where") or "?", band(r.get("photos_in")) or "?"), []).append(r)
    med = lambda xs: round(statistics.median(xs), 1) if xs else None  # noqa: E731
    out = []
    order = {band(lo): i for i, (lo, _) in enumerate(BANDS)}
    for (where, photos), rs in sorted(groups.items(), key=lambda kv: (kv[0][0], order.get(kv[0][1], len(order)))):
        out.append({"where": where, "photos": photos, "n": len(rs),
                    "minutes": med([r["tools_seconds"] / 60 for r in rs]),
                    "cpu_minutes": med([r["cpu_seconds"] / 60 for r in rs]),
                    "wall_minutes": med([r["wall"] / 60 for r in rs if r.get("wall") is not None]),
                    "waited": med([r["waited"] for r in rs if r.get("waited") is not None]),
                    "vggt": sum(r.get("vggt_seconds") is not None for r in rs),
                    "vggt_seconds": med([r["vggt_seconds"] for r in rs if r.get("vggt_seconds") is not None]),
                    "from_log": sum(bool(r.get("from_log")) for r in rs)})
    return out

