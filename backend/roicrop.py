"""Which meshes OpenMVS 2.4 cropped to a region of interest it guessed, and how much of their dense points that took,
from their projects' job logs.

From the 2.4 deploy (Nov 15, 2026) until --estimate-roi 0 (Nov 24, da3a32a), DensifyPointCloud guessed a region from
the cameras and sparse points, chose each depth map's neighbouring photos by the points inside it, and dropped every
dense point outside it; ReconstructMesh then cropped to it too. A guess can be a sliver (Swing Dance Boulder: 93% of
the points cut). Every mesh densified with a guessed region is possibly affected, as its depth maps were made by it
even where few points were cut; the share cut orders their remaking and flags the worst to editors.

A job log holds each run of its project, one after another: each "== OpenMVS: densify" starts one, whose region (if
any), fused and cut points are read, up to the faces TextureMesh then saved (as many as the mesh's GLB has, by which a
run is matched to its mesh; a run resumed after an interruption carries on in the same log).
"""
from __future__ import annotations

import re
from pathlib import Path

SINCE = 1794719100.0    # 2026-11-15 05:05 UTC, 2de8198 (both images with OpenMVS 2.4): none was cropped before
FLAG = 0.10             # a mesh with more of its dense points cut than this is tagged so to editors
URGENT = 0.20           # and one with more than this is made again first

_DENSIFY = "== OpenMVS: densify"
_ROI = re.compile(r"ROI estimated with position")
_FUSED = re.compile(r"Depth-maps fused and filtered: \d+ depth-maps, \d+ depths, (\d+) points")
_CUT = re.compile(r"Point-cloud trimmed to ROI: (\d+) points removed")
_SAVED = re.compile(r"Mesh 'scene_dense_mesh_texture\.glb' saved: \d+ vertices, (\d+) faces")


def runs(lines) -> list[dict]:
    """Each densify run in a job log's lines: {estimated (a region was guessed), fused (points), cut (points dropped
    outside the region), faces (those TextureMesh saved after it, or None)}."""
    out: list[dict] = []
    run = None
    for line in lines:
        if line.startswith(_DENSIFY):
            run = {"estimated": False, "fused": None, "cut": 0, "faces": None}
            out.append(run)
        elif run is None:
            continue
        elif _ROI.search(line):
            run["estimated"] = True
        elif m := _FUSED.search(line):
            run["fused"] = int(m[1])
        elif m := _CUT.search(line):
            run["cut"] = int(m[1])
        elif run["faces"] is None and (m := _SAVED.search(line)):
            run["faces"] = int(m[1])
    return out


def of_log(log: Path) -> list[dict]:
    """runs() of a job log, read line by line; [] if there's none."""
    try:
        with open(log, errors="replace") as f:
            return runs(line for line in f if "OpenMVS" in line or "ROI" in line or "Depth-maps fused" in line
                        or "scene_dense_mesh_texture.glb" in line)
    except OSError:
        return []


def record(faces: int | None, made: float, found: list[dict]) -> dict:
    """What's known of a mesh with this many faces, made then, from its project's densify runs (runs()):
    {affected, share (of its dense points cut, or None where not known), cut, fused, from: "log" | "date"}.
    The last run whose faces match is its own; with none, one made since SINCE counts as possibly affected."""
    mine = [r for r in found if faces and r["faces"] == faces]
    if mine:
        r = mine[-1]
        share = round(r["cut"] / r["fused"], 4) if r["estimated"] and r["fused"] else (0.0 if not r["estimated"] else None)
        return {"affected": r["estimated"], "share": share, "cut": r["cut"], "fused": r["fused"], "from": "log"}
    return {"affected": made >= SINCE, "share": None, "cut": None, "fused": None, "from": "date"}
