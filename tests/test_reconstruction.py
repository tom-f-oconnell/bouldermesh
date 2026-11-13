#!/usr/bin/env python3
"""What a mesh keeps of its reconstruction (backend/reconstruction.py): the model as it was, the database without its
features, and which COLMAP and settings made them, on a made-up model and a database in COLMAP's layout.

    python3 tests/test_reconstruction.py
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import struct
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
import reconstruction  # noqa: E402


def database(path: pathlib.Path) -> None:
    """A COLMAP-like database of two photos with features, raw matches and one verified pair."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE cameras (camera_id INTEGER PRIMARY KEY, model INTEGER, width INTEGER, height INTEGER, params BLOB, prior_focal_length INTEGER);
        CREATE TABLE images (image_id INTEGER PRIMARY KEY, name TEXT UNIQUE, camera_id INTEGER);
        CREATE TABLE keypoints (image_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB);
        CREATE TABLE descriptors (image_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB);
        CREATE TABLE matches (pair_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB);
        CREATE TABLE two_view_geometries (pair_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB, config INTEGER);
    """)
    c.execute("INSERT INTO cameras VALUES (1, 2, 600, 400, ?, 0)", (struct.pack("<4d", 500, 300, 200, 0),))
    for i, name in ((1, "a.jpg"), (2, "b.jpg")):
        c.execute("INSERT INTO images VALUES (?, ?, 1)", (i, name))
        c.execute("INSERT INTO keypoints VALUES (?, 5000, 6, ?)", (i, bytes(5000 * 6 * 4)))
        c.execute("INSERT INTO descriptors VALUES (?, 5000, 128, ?)", (i, bytes(range(256)) * 2500))
    c.execute("INSERT INTO matches VALUES (2147483649, 900, 2, ?)", (bytes(900 * 8),))
    c.execute("INSERT INTO two_view_geometries VALUES (2147483649, 300, 2, ?, 2)", (struct.pack("<600I", *range(600)),))
    c.commit()
    c.close()


with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    model, out = td / "model", td / "out"
    model.mkdir()
    (model / "images.bin").write_bytes(struct.pack("<Q", 2) + b"images")
    (model / "cameras.bin").write_bytes(b"cameras")
    (model / "points3D.bin").write_bytes(b"points" * 1000)
    database(td / "database.db")
    said = []
    cmd = ["colmap", "feature_extractor", "--database_path", str(td / "database.db"), "--image_path", str(td / "photos"),
           "--ImageReader.single_camera", "0", "--ImageReader.mask_path", str(td / "masks"), "--SiftExtraction.max_image_size", "2400"]
    args = reconstruction.extract_args(cmd)
    assert args == ["--database_path", "<database>", "--image_path", "<photos>", "--ImageReader.single_camera", "0",
                    "--ImageReader.mask_path", "<masks>", "--SiftExtraction.max_image_size", "2400"], args

    kept = reconstruction.keep(model, td / "database.db", out, said.append, "COLMAP 3.9.1 (Commit x with CUDA)", {"args": args, "device": "cpu"})
    assert kept == out / reconstruction.FILE and kept.is_file(), said
    assert not (out / (reconstruction.FILE + ".partial")).exists() and not (td / "pairs.db").exists(), "nothing left behind"
    assert "kept" in said[-1] and "a placed photo" in said[-1], said
    assert kept.stat().st_size < (td / "database.db").stat().st_size / 4, "features are what's big, and they're left out"

    back = td / "back"
    build = reconstruction.unpack(out, back)
    assert build["colmap"].startswith("COLMAP 3.9.1") and build["extract"] == {"args": args, "device": "cpu"}, build
    assert build["model_files"] == ["cameras.bin", "images.bin", "points3D.bin"], build
    for f in ("images.bin", "cameras.bin", "points3D.bin"):
        assert (back / "model" / f).read_bytes() == (model / f).read_bytes(), f
    c = sqlite3.connect(back / "pairs.db")
    count = lambda t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
    assert [count(t) for t in ("keypoints", "descriptors", "matches")] == [0, 0, 0], "features and raw matches emptied"
    assert count("images") == 2 and count("cameras") == 1, "photos and lenses kept"
    rows, data = c.execute("SELECT rows, data FROM two_view_geometries").fetchone()
    assert rows == 300 and struct.unpack("<600I", data)[:3] == (0, 1, 2), "verified pairs kept as they were"
    c.close()
    assert json.loads((back / "build.json").read_text()) == build

    # nothing to keep: no database, or no binary model; the mesh goes on without it
    said.clear()
    assert reconstruction.keep(model, td / "none.db", td / "out2", said.append) is None and not (td / "out2").exists()
    assert "not kept" in said[-1], said
    (td / "text").mkdir()
    (td / "text" / "images.txt").write_text("")
    assert reconstruction.keep(td / "text", td / "database.db", td / "out3", said.append) is None
    assert reconstruction.unpack(td / "out3", td / "back3") is None, "a mesh that kept none"
    print("reconstruction kept OK")
print("ALL RECONSTRUCTION TESTS PASSED")
