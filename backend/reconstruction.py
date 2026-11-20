"""What a mesh keeps of the reconstruction it was made from, so that photos can be added to it later without placing
its own photos again (experiments/mapper/incr): its sparse model as levelled, in the mesh's own frame (the one
cameras.json is in), and the pairs of photos COLMAP verified, without their features.

Features are left out. They are most of COLMAP's database (about 1.6 of its 2 MB a photo, and they hardly compress),
and the same COLMAP gives the same ones, to the bit, from the same photo with the same settings on the CPU, however
many threads it has. build.json says which COLMAP and which settings, so a later add can tell whether extracting
them again lines up with the model's points; when it doesn't, the model's cameras still do, as cameras.json's do.
What's kept comes to about 0.3 MB a photo. Since PIPELINE_VERSION 2 the features are kept too, in a file of their own
(FEATURES_FILE, about 1.2 MB a photo), as GPU SIFT doesn't give the same ones again: full_database() puts COLMAP's
database back together from the two, for adding photos to the model.

    reconstruction.tar.gz, beside the mesh
        model/       COLMAP's binary model: cameras.bin, images.bin, points3D.bin (and rigs.bin, frames.bin from COLMAP 4)
        pairs.db     COLMAP's database with its keypoints, descriptors and raw matches emptied: its photos, lenses
                     and verified pairs (two_view_geometries) remain
        build.json   {colmap, extract, mapper, made}: the COLMAP that made it (its `colmap -h` banner), its feature
                     extraction's arguments with paths left out and whether that ran on the GPU or the CPU, and which
                     mapper placed its cameras (pipeline.py's mapper.json: global or incremental, and why)
"""
from __future__ import annotations

import gzip
import io
import json
import shutil
import sqlite3
import struct
import tarfile
import time
from pathlib import Path
from typing import Callable

FILE = "reconstruction.tar.gz"
EMPTIED = ("keypoints", "descriptors", "matches")
PATHS = {"--database_path": "<database>", "--image_path": "<photos>", "--ImageReader.mask_path": "<masks>"}


def extract_args(cmd: list[str]) -> list[str]:
    """A feature_extractor command's options with its paths replaced by what they were, so that they can be kept and
    compared: the program and subcommand dropped."""
    out, args = [], cmd[2:] if len(cmd) > 1 and cmd[1] == "feature_extractor" else list(cmd)
    for i, a in enumerate(args):
        out.append(PATHS[args[i - 1]] if i and args[i - 1] in PATHS else a)
    return out


def slim_database(db: Path, dest: Path) -> None:
    """A copy of a COLMAP database with its features and raw matches emptied (their tables kept, so it still opens
    as a COLMAP database), compacted."""
    dest.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        tables = {r[0] for r in out.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in EMPTIED:
            if t in tables:
                out.execute(f"DELETE FROM {t}")
        out.commit()
        out.execute("VACUUM")
    finally:
        src.close()
        out.close()


def keep(model: Path, db: Path, out_dir: Path, log: Callable[[str], None], colmap: str | None = None,
         extract: dict | None = None, mapper: dict | None = None) -> Path | None:
    """Keep a mesh's reconstruction beside it (FILE in out_dir). Never fails the mesh: if something is missing or
    goes wrong, it says so and keeps nothing.

    Args:
        model: the sparse model the mesh was made from, as levelled.
        db: COLMAP's database.
        colmap: the COLMAP that made it, as its banner says.
        extract: {args, device} of its feature extraction (extract_args()).
        mapper: which mapper placed its cameras, as pipeline.py records it (mapper.json).

    Returns:
        The file kept, or None.
    """
    files = sorted(p for p in model.glob("*.bin")) if model.is_dir() else []
    if not any(p.name == "images.bin" for p in files) or not db.is_file():
        log(f"reconstruction: not kept (no binary model in {model.name} or no database)")
        return None
    dest = out_dir / FILE
    partial = out_dir / (FILE + ".partial")
    slim = db.with_name("pairs.db")
    try:
        slim_database(db, slim)
        build = {"colmap": colmap, "extract": extract, "mapper": mapper, "made": time.time(), "model_files": [p.name for p in files]}
        out_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(partial, "w:gz", compresslevel=6) as tar:
            for p in files:
                tar.add(p, arcname=f"model/{p.name}")
            tar.add(slim, arcname="pairs.db")
            data = json.dumps(build, indent=1).encode()
            info = tarfile.TarInfo("build.json")
            info.size, info.mtime = len(data), int(time.time())
            tar.addfile(info, io.BytesIO(data))
        partial.replace(dest)
        with open(model / "images.bin", "rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
        size = dest.stat().st_size
        log(f"reconstruction: kept ({size / 1e6:.1f} MB, {size / 1e6 / max(n, 1):.2f} MB a placed photo)")
        return dest
    except (OSError, sqlite3.Error, tarfile.TarError, struct.error) as e:
        log(f"reconstruction: not kept ({type(e).__name__}: {e})")
        partial.unlink(missing_ok=True)
        return None
    finally:
        slim.unlink(missing_ok=True)


def unpack(mesh_dir: Path, dest: Path) -> dict | None:
    """Take a mesh's kept reconstruction out into dest (model/, pairs.db, build.json).

    Returns:
        Its build.json, or None if the mesh kept none.
    """
    path = mesh_dir / FILE
    if not path.is_file():
        return None
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "r:gz") as tar:
        tar.extractall(dest, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    return json.loads((dest / "build.json").read_text())


# ------------------------------------------------------------------------- features, kept apart (since PIPELINE_VERSION 2)
FEATURES_FILE = "features.db.gz"    # beside the mesh: its photos' keypoints and descriptors, offloadable on their own
FEATURES_LEFT_OUT = ("matches", "two_view_geometries")    # in the kept reconstruction's pairs.db already, or not needed


def keep_features(db: Path, out_dir: Path, log: Callable[[str], None]) -> Path | None:
    """Keep a mesh's photos' features beside it (FEATURES_FILE: COLMAP's database without FEATURES_LEFT_OUT's tables
    filled, gzipped: its photos, lenses, COLMAP 4's rigs and frames, keypoints and descriptors), so that photos added later are matched against the same features its model's points came from:
    GPU SIFT gives slightly different ones from run to run (Nov 20: 1 to 12 of 76 photos). Never fails the mesh."""
    if not db.is_file():
        return None
    dest, tmp = out_dir / FEATURES_FILE, db.with_name("features.db")
    tmp.unlink(missing_ok=True)
    try:
        src, out = sqlite3.connect(f"file:{db}?mode=ro", uri=True), sqlite3.connect(tmp)
        try:
            src.backup(out)
            for (t,) in out.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                if t in FEATURES_LEFT_OUT:
                    out.execute(f"DELETE FROM {t}")
            out.commit()
            out.execute("VACUUM")
        finally:
            src.close()
            out.close()
        out_dir.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".partial")
        with open(tmp, "rb") as f, gzip.open(part, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g, 1 << 20)
        part.replace(dest)
        log(f"features: kept ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest
    except (OSError, sqlite3.Error) as e:
        log(f"features: not kept ({type(e).__name__}: {e})")
        return None
    finally:
        tmp.unlink(missing_ok=True)


def full_database(mesh_dir: Path, unpacked: Path, dest: Path) -> Path:
    """COLMAP's database as the mesh was placed from, put back together in dest: its kept features (FEATURES_FILE)
    with the verified pairs of its kept reconstruction (unpacked: unpack()'s folder, with pairs.db).

    Raises:
        FileNotFoundError: the mesh kept no features, or no reconstruction.
    """
    feats = mesh_dir / FEATURES_FILE
    if not feats.is_file() or not (unpacked / "pairs.db").is_file():
        raise FileNotFoundError(f"{mesh_dir.name} kept no features" if not feats.is_file() else "no kept reconstruction")
    dest.unlink(missing_ok=True)
    with gzip.open(feats, "rb") as g, open(dest, "wb") as f:
        shutil.copyfileobj(g, f, 1 << 20)
    con = sqlite3.connect(dest)
    try:
        con.execute("ATTACH DATABASE ? AS pairs", (str(unpacked / "pairs.db"),))
        for t in ("matches", "two_view_geometries"):
            have = {r[0] for r in con.execute("SELECT name FROM pairs.sqlite_master WHERE type='table'")}
            if t in have:
                con.execute(f"DELETE FROM main.{t}")
                con.execute(f"INSERT INTO main.{t} SELECT * FROM pairs.{t}")
        con.commit()
        con.execute("DETACH DATABASE pairs")
    finally:
        con.close()
    return dest
