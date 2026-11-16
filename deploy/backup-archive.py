#!/usr/bin/env python3
"""Move what a night's backup set aside (changed/<day>/, deploy/backup.sh) that can't easily be had again into archive/,
kept for good: fetched videos, projects' photos, people masks and rejected photos, and meshes (a mesh deleted on the
server whole, records and all; one still there only its big files, as a retexture or rebuild replaced them, not the
records it rewrites as it goes). The rest stays in changed/<day>/, and goes with it after KEEP_DAYS.

archive/ has the server's layout (archive/data/...), each file once: one set aside again at the same path, changed,
is kept beside the first as <name>.~<day>~. A photo set aside only because the filter moved it into rejected/, where
the copy still has it, isn't kept twice.

    deploy/backup-archive.py <backup dir> <day>...      (several days: newest first, so a rejected photo is found as that)
"""
from __future__ import annotations

import filecmp
import os
import re
import shutil
import sys
from pathlib import Path

KEPT = [re.compile(p) for p in (r"cache/videos/[^/]+", r"projects/[^/]+/(photos|masks|rejected)/.+")]
MESH = re.compile(r"meshes/([^/]+)/(.+)")
BIG = 1 << 20     # a mesh still on the server: only its files this big or bigger (meshes, textures), not its records


def kept(rel: str, roots: list[Path]) -> bool:
    """Whether a file set aside at rel (under data/), not a mesh's, is one kept for good. roots: where a rejected copy
    of a photo may be kept instead (the copy, the archive, the night's set-aside files)."""
    if any(p.fullmatch(rel) for p in KEPT):
        m = re.fullmatch(r"projects/([^/]+)/photos/([^/]+)", rel)
        if m:   # moved into rejected/ by the filter, kept as that: not twice
            for root in roots:
                rej = root / "projects" / m[1] / "rejected"
                if rej.is_dir() and any(f.name.endswith("_" + m[2]) for f in rej.iterdir()):
                    return False
        return True
    return False


def archive(dest: Path, day: str) -> tuple[int, int]:
    """Move the night's kept files from changed/<day>/data into archive/data; (files, bytes) moved."""
    src, out, current = dest / "changed" / day / "data", dest / "archive" / "data", dest / "current" / "data"
    n = size = 0
    if not src.is_dir():
        return n, size
    for root, _, files in os.walk(src):
        for name in files:
            f = Path(root) / name
            rel = f.relative_to(src).as_posix()
            m = MESH.fullmatch(rel)
            if m:
                keep = not (current / "meshes" / m[1]).is_dir() or f.stat().st_size >= BIG
            else:
                keep = kept(rel, [current, out, src])
            if not keep:
                continue
            to = out / rel
            to.parent.mkdir(parents=True, exist_ok=True)
            b = f.stat().st_size
            if to.exists():
                if filecmp.cmp(f, to, shallow=False):     # the same as kept already
                    f.unlink()
                    continue
                to = to.with_name(f"{to.name}.~{day}~")
            shutil.move(f, to)
            n, size = n + 1, size + b
    return n, size


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    for day in sys.argv[2:]:
        files, size = archive(Path(sys.argv[1]), day)
        print(f"{day}: archived {files} file(s), {size / 2**30:.2f} GB")
