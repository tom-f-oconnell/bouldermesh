"""What each run's steps after the mapper made, from OpenMVS's own logs, and its textured meshes to look at: meshes.py

    meshes.py [project ...]         a Markdown row per post folder: photos given depth maps, points after fusing them,
                                    the mesh's faces, texture patches, the GLB's size, and each step's seconds
    meshes.py export <folder>       every post folder's GLB copied there as <boulder>-<run>-<post folder>.glb, and the
                                    incremental pipeline's as <boulder>-pipeline-<state>.glb (those there are skipped)

Speed alone doesn't compare two OpenMVS versions or settings: 2.4 fuses depth maps to far fewer points than 2.3 by
default, so the same model gives a coarser mesh in less time. The counts are read from the steps' logs, as the point
clouds and meshes themselves are the containers' root's.
"""
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench  # noqa: E402

GLB = "scene_dense_mesh_texture.glb"


def last(text: str, pattern: str) -> str:
    """The last match's first group in a log, with thousands separated; empty if there is none."""
    found = re.findall(pattern, text)
    return f"{int(found[-1]):,}" if found else ""


def row(o: Path) -> list[str]:
    log = lambda step: (o / f"{step}.log").read_text(errors="replace") if (o / f"{step}.log").exists() else ""      # noqa: E731
    rec = lambda step: json.loads((o / f"{step}.json").read_text()) if (o / f"{step}.json").exists() else {}       # noqa: E731
    secs = lambda step: f"{rec(step)['wall']:.0f}" + ("" if rec(step)["rc"] == 0 else f" (rc {rec(step)['rc']})") if rec(step) else ""  # noqa: E731
    dense, mesh, texture = log("dense"), log("mesh"), log("texture")
    return [o.parts[-4][:4], o.parts[-2], o.name, last(dense, r"fused and filtered: (\d+) depth-maps"),
            last(dense, r"Densifying point-cloud completed: (\d+) points"), last(mesh, r"saved: \d+ vertices, (\d+) faces"),
            last(texture, r"image completed: (\d+) patches"),
            f"{(o / GLB).stat().st_size / 1e6:.0f}" if (o / GLB).exists() else "", secs("dense"), secs("mesh"), secs("texture")]


def table(only: list[str]) -> None:
    print("| Boulder | Run | Post | Depth maps | Points | Faces | Texture patches | GLB MB | Dense s | Mesh s | Texture s |\n"
          "|---|---|---|---|---|---|---|---|---|---|---|")
    for o in sorted(bench.R.glob("*/*/*/post-*")):
        if ".stale" not in str(o) and (not only or o.parts[-4] in only):
            print("| " + " | ".join(row(o)) + " |")


def export(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    found = [(o / GLB, f"{o.parts[-4]}-{o.parts[-2]}-{o.name.split('@')[0]}.glb") for o in sorted(bench.R.glob("*/*/*/post-*"))
             if ".stale" not in str(o)]
    found += [(d / "dense" / GLB, f"{d.parent.name}-pipeline-{d.name}.glb") for d in sorted((bench.BENCH / "incr" / "r").glob("*/*"))]
    for src, name in found:
        if src.exists() and not (dest / name).exists():
            shutil.copyfile(src, dest / name)
            print(name)


if __name__ == "__main__":
    if sys.argv[1:2] == ["export"]:
        export(Path(sys.argv[2]))
    else:
        table(sys.argv[1:])
