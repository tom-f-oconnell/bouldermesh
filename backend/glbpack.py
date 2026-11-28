"""Copies of a reconstruction's GLB made for the viewer to download, while the GLB itself stays as it is, for downloads and
everything else on the server. glbpack.mjs makes them with meshoptimizer, run by node or deno (the Docker image's):

  packed/<stem>.full.glb          the same faces, compressed (Caveman: 35 MB to 6.9 MB)
  packed/<stem>.preview.glb       for a mesh of at least twice the faces a preview is to have (the admin page's
                                  viewer.preview_faces, PREVIEW_FACES by default), simplified to about that many
                                  (Caveman, to 150,000: 1.1 MB), to show while the rest comes
  packed/<stem>.middle.glb        for a mesh of at least twice MIDDLE_FACES, simplified to about that many: what phones
                                  get in place of every face (only big meshes need it: 19 of 836 on Nov 27)
  packed/<stem>.<lod>.faces.bin   which of the GLB's own faces each of a packed one's comes from, so its cleanup applies
  packed/<stem>.json              what was made, from which version of the GLB: written last, so it says the rest is there

They're kept beside the GLB they're made from: mesh.glb, permitted/mesh.glb, or the untextured copy (meshclean.py).
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
VERSION = 1
LODS = ("full", "middle", "preview")
DIR = "packed"
PREVIEW_FACES = 150_000     # about 1 MB; 0: no previews
MIDDLE_FACES = 1_000_000    # 40 MB of a phone's graphics memory, against 124 MB for Spots of Time's 3.1M
SCRIPT = Path(__file__).with_name("glbpack.mjs")


def runtime() -> list[str] | None:
    """The command that runs glbpack.mjs here, or None."""
    if shutil.which("node"):
        return ["node", str(SCRIPT)]
    if shutil.which("deno"):
        return ["deno", "run", "--allow-read", "--allow-write", "--no-prompt", str(SCRIPT)]
    return None


def available() -> bool:
    return runtime() is not None


def stamp(src: Path) -> list[int]:
    st = src.stat()
    return [st.st_size, st.st_mtime_ns]


def _record_file(src: Path) -> Path:
    return src.parent / DIR / f"{src.stem}.json"


def record(src: Path) -> dict | None:
    """What was packed from src, if from this version of it and of the packer; else None."""
    try:
        r = json.loads(_record_file(src).read_text())
        now = stamp(src)
    except (OSError, json.JSONDecodeError):
        return None
    return r if r.get("version") == VERSION and r.get("source") == now else None


def made_for(rec: dict, preview_faces: int, middle_faces: int = MIDDLE_FACES) -> bool:
    """Whether a record's copies were made for previews of preview_faces faces (or for none, if 0), and for middle
    copies of middle_faces, if the mesh is big enough to have one (those packed before they were made have none)."""
    small = not middle_faces or rec.get("source_faces", 0) < 2 * middle_faces
    middle_ok = rec.get("middle_target") == middle_faces or (small and "middle" not in rec)
    return rec.get("preview_target", PREVIEW_FACES) == preview_faces and middle_ok


def path(src: Path, lod: str, faces: bool = False) -> Path | None:
    """A packed copy of src (lod "full", "middle" or "preview"), or with faces its faces' origins; None if there's none
    for this version of src."""
    r = record(src)
    if not r or not r.get(lod):
        return None
    p = src.parent / DIR / f"{src.stem}.{lod}.{'faces.bin' if faces else 'glb'}"
    return p if p.is_file() else None


def image_files(src: Path) -> list[str]:
    """The files a GLB's images are in, beside it, from its JSON alone."""
    with open(src, "rb") as f:
        magic, _, _, n, kind = struct.unpack("<IIIII", f.read(20))
        if magic != 0x46546C67 or kind != 0x4E4F534A:
            raise ValueError("not a GLB file")
        j = json.loads(f.read(n))
    return [i["uri"] for i in j.get("images", []) if i.get("uri") and not i["uri"].startswith("data:") and "/" not in i["uri"]]


def pack(src: Path, log: Log = print, extra: dict | None = None, timeout: float = 900, preview_faces: int = PREVIEW_FACES,
         middle_faces: int = MIDDLE_FACES) -> dict:
    """Make src's packed copies, replacing any from before, at a low priority.

    Args:
        extra: kept in the record too (the server's sizes of its textures).
        preview_faces: about how many faces a preview has, made only for a mesh of at least twice as many; 0: none.
        middle_faces: the same for the middle copy, which phones get in place of every face.

    Returns:
        The record: {"full": {"bytes", "faces"}, "preview" and "middle": the same, with "error", or absent,
        "source_faces", "seconds", ...}.
    """
    cmd = runtime()
    if not cmd:
        raise RuntimeError("packing meshes needs node or deno")
    out = src.parent / DIR
    out.mkdir(exist_ok=True)
    tmp = out / f".{src.stem}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    made_from = stamp(src)
    try:
        nice = ["nice", "-n", "15"] if shutil.which("nice") else []
        r = subprocess.run([*nice, *cmd, str(src), str(tmp), str(preview_faces), str(2 * preview_faces), str(middle_faces),
                            str(2 * middle_faces)], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "DENO_NO_UPDATE_CHECK": "1"})
        if r.returncode:
            raise RuntimeError((r.stderr.strip().splitlines() or [f"exit status {r.returncode}"])[-1])
        made = json.loads(r.stdout.strip().splitlines()[-1])
        _record_file(src).unlink(missing_ok=True)     # what follows isn't what it described
        for lod in LODS:
            for part in ("glb", "faces.bin"):
                f, dst = tmp / f"{lod}.{part}", out / f"{src.stem}.{lod}.{part}"
                if f.exists():
                    f.replace(dst)
                else:
                    dst.unlink(missing_ok=True)
        rec = {"version": VERSION, "source": made_from, "made": time.time(), "preview_target": preview_faces,
               "middle_target": middle_faces, **made, **(extra or {})}
        partial = out / f"{src.stem}.json.partial"
        partial.write_text(json.dumps(rec))
        partial.replace(_record_file(src))
        log(f"packed for viewing: {made['full']['bytes'] / 1e6:.1f} MB"
            + (f", and a preview of {made['preview']['faces']:,} faces, {made['preview']['bytes'] / 1e6:.1f} MB" if made.get("preview") else "")
            + (f", and for phones {made['middle']['faces']:,} faces, {made['middle']['bytes'] / 1e6:.1f} MB" if made.get("middle") else "")
            + f" (from {src.stat().st_size / 1e6:.1f} MB, in {made['seconds']:.0f} s)")
        return rec
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def simplified(src: Path, dst: Path, faces: int, timeout: float = 900) -> dict:
    """A copy of src simplified to about this many faces (or all of them, packed, if it has fewer than twice as many),
    written to dst (a GLB) at a low priority, with no textures needed, for comparing two meshes in the viewer.

    Returns:
        {"faces", "source_faces", "error": the most it moved the surface, as a share of the mesh's size (0 if not
        simplified), "bytes", "seconds"}.
    """
    cmd = runtime()
    if not cmd:
        raise RuntimeError("simplifying meshes needs node or deno")
    tmp = dst.parent / f".{dst.name}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        nice = ["nice", "-n", "15"] if shutil.which("nice") else []
        r = subprocess.run([*nice, *cmd, str(src), str(tmp), str(faces), str(2 * faces)], capture_output=True, text=True,
                           timeout=timeout, env={**os.environ, "DENO_NO_UPDATE_CHECK": "1"})
        if r.returncode:
            raise RuntimeError((r.stderr.strip().splitlines() or [f"exit status {r.returncode}"])[-1])
        made = json.loads(r.stdout.strip().splitlines()[-1])
        lod = "preview" if made.get("preview") else "full"
        (tmp / f"{lod}.glb").replace(dst)
        return {"faces": made[lod]["faces"], "source_faces": made["source_faces"], "error": made[lod].get("error", 0),
                "bytes": made[lod]["bytes"], "seconds": made["seconds"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def labels(src: Path, lod: str, faces_file: Path, label_bytes: int) -> bytes | None:
    """A cleanup's piece for each face (meshclean.FACES_FILE, label_bytes each) put in the order of src's packed copy's
    faces; None if it has none."""
    import numpy as np
    order = path(src, lod, faces=True)
    if order is None:
        return None
    pieces = np.frombuffer(faces_file.read_bytes(), {2: "<u2", 4: "<u4"}[label_bytes])
    origin = np.frombuffer(order.read_bytes(), "<u4")
    if origin.size and int(origin.max()) >= pieces.size:
        raise ValueError("the cleanup doesn't fit this mesh")
    return pieces[origin].tobytes()
