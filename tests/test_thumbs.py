#!/usr/bin/env python3
"""Pictures of meshes (backend/thumbs.py): what a picture is drawn from, the cameras an editor may send, the small copy,
and, where node and playwright-core are here (as in the Docker image, or as PLAYWRIGHT_CORE and PLAYWRIGHT_BROWSERS_PATH
say), a real one drawn by a running server: by itself, then from a camera sent as Share's Use this view does.

    python3 tests/test_thumbs.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
import thumbs  # noqa: E402


def units(tmp: Path) -> None:
    d = tmp / "m"
    d.mkdir(parents=True)
    k = thumbs.key("v1", d, None)
    assert k == thumbs.key("v1", d, None) and k != thumbs.key("v2", d, None), "what the viewer is sent"
    pose = {"eye": [1.0, 2.0, 3.0], "target": [0.0, 0.0, 0.0]}
    assert thumbs.key("v1", d, pose) != k, "a camera chosen by hand"
    (d / "orientation.json").write_text("{}")
    k2 = thumbs.key("v1", d, None)
    assert k2 != k, "which way is up"
    (d / "views.json").write_text("{}")
    assert thumbs.key("v1", d, None) != k2, "where its climbs were photographed from"

    assert thumbs.check_pose(None) is None
    assert thumbs.check_pose({"eye": [1, 2, 3], "target": ["0", 0, 0.5]}) == {"eye": [1.0, 2.0, 3.0], "target": [0.0, 0.0, 0.5]}
    for bad in ({"eye": [1, 2]}, {"eye": [1, 2, 3], "target": [1, 2, 3]}, {"eye": [1, 2, float("nan")], "target": [0, 0, 0]},
                {"eye": [1e9, 0, 0], "target": [0, 0, 0]}, [1, 2, 3], {"eye": "abc", "target": [0, 0, 0]}):
        try:
            thumbs.check_pose(bad)
            raise AssertionError(f"took {bad}")
        except ValueError:
            pass

    thumbs.save(d, {"pose": pose, "v": "abc"})
    assert thumbs.load(d) == {"pose": pose, "v": "abc"} and thumbs.load(tmp) == {}
    try:
        from PIL import Image
    except ImportError:
        print("units OK (no Pillow: the small copy untried)")
        return
    Image.new("RGB", thumbs.SIZE, (90, 120, 160)).save(d / "big.jpg")
    thumbs.shrink(d / "big.jpg", d / "small.jpg")
    with Image.open(d / "small.jpg") as im:
        assert im.size == thumbs.SMALL, im.size
    print("units OK")


def drawn(tmp: Path) -> None:
    """A real picture, from a running server, where this machine can draw them."""
    if not thumbs.available():
        print("drawing skipped: no node and playwright-core here (set PLAYWRIGHT_CORE and PLAYWRIGHT_BROWSERS_PATH)")
        return
    import os
    from PIL import Image
    from test_public import PLY, Server, req
    env = {k: os.environ[k] for k in ("PLAYWRIGHT_CORE", "PLAYWRIGHT_BROWSERS_PATH") if os.environ.get(k)}
    srv = Server(tmp, **env)
    B = srv.url
    try:
        assert req(B, "GET", "/api/status")[2]["thumbs"] is True
        mid = req(B, "POST", "/api/meshes", files=[("files", "m.ply", PLY)])[2]["id"]
        req(B, "GET", "/api/meshes")   # queues it
        first = wait(B, mid, None)
        big = tmp / "data" / "meshes" / mid / thumbs.DIR / thumbs.LARGE
        with Image.open(big) as im:
            assert im.size == thumbs.SIZE and im.format == "JPEG", (im.size, im.format)
        st, h, small = req(B, "GET", f"/meshes/{mid}/thumb.small.jpg?v={first['v']}")
        assert st == 200 and h["Content-Type"] == "image/jpeg" and h["Cache-Control"] == "public, max-age=86400", h
        assert first["hand"] is False and json.loads((big.parent.parent / thumbs.RECORD).read_text())["seconds"] > 0

        # a camera chosen by hand draws it again, and Back to automatic again
        pose = {"eye": [0.5, 0.2, 0.4], "target": [0.3, 0.3, 0.0]}
        assert req(B, "POST", f"/api/meshes/{mid}/thumb", {"pose": pose})[2] == {"queued": True}
        second = wait(B, mid, first["v"])
        assert second["hand"] is True and second["v"] != first["v"]
        assert req(B, "POST", f"/api/meshes/{mid}/thumb", {"pose": None})[0] == 200
        assert wait(B, mid, second["v"])["v"] == first["v"], "by itself again: as it was"
    finally:
        srv.stop()
    print("drawing OK")


def wait(B: str, mid: str, was: str | None) -> dict:
    from test_public import req
    for _ in range(120):
        got = req(B, "GET", f"/api/meshes/{mid}/thumb")[2]
        assert not got["failed"], got
        if got["thumb"] and got["thumb"]["v"] != was:
            return got["thumb"]
        time.sleep(1)
    raise AssertionError(f"no picture of {mid} after 2 minutes")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bouldermesh-thumbs-") as td:
        units(Path(td) / "u")
        drawn(Path(td) / "s")
    print("ALL THUMBNAIL TESTS PASSED")


if __name__ == "__main__":
    main()
