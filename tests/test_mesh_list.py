#!/usr/bin/env python3
"""The kept /api/meshes (server._mesh_list_for()): fresh copies, an older one given while it's made again in the
background, a change by an editor waited for, and the counts of each. Offline, with the list's making stubbed.

    python3 tests/test_mesh_list.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="bouldermesh-meshlist-")

import server  # noqa: E402

made: list[int] = []
mark = ["1"]
release = threading.Event()


def make(slow: bool = False):
    if slow:
        release.wait(10)
    made.append(len(made) + 1)
    return {"n": made[-1]}


def main() -> None:
    server.meshes_mark = lambda: mark[0]
    server.MESH_LIST_TTL = 0.3
    get = lambda slow=False: server._mesh_list_for(None, server.MESH_LIST_TTL, lambda: make(slow))  # noqa: E731

    assert get() == b'{"n": 1}' and get() == b'{"n": 1}', "made once, then kept"
    s = server.mesh_list_stats()
    assert (s["waited"], s["fresh"], s["made"]) == (1, 1, 1), s

    # past its time: the older copy at once, made again in the background, at most one making at a time
    time.sleep(0.35)
    began = time.monotonic()
    assert get(slow=True) == b'{"n": 1}' and get(slow=True) == b'{"n": 1}' and time.monotonic() - began < 0.5
    assert len(server._mesh_lists_making) == 1
    release.set()
    for _ in range(100):
        if not server._mesh_lists_making:
            break
        time.sleep(0.02)
    assert get() == b'{"n": 2}', "the new copy once made"
    s = server.mesh_list_stats()
    assert (s["older"], s["made"]) == (2, 2), s

    # a mesh made: the older copy still, until the new one's made
    mark[0] = "2"
    release.clear()
    assert get(slow=True) == b'{"n": 2}'
    # an editor's change meanwhile: the next asking waits for a fresh one, and the making begun before it isn't kept
    server.forget_mesh_list()
    assert get() == b'{"n": 3}'
    release.set()
    for _ in range(100):
        if not server._mesh_lists_making:
            break
        time.sleep(0.02)
    assert get() == b'{"n": 3}', "the making begun before the change was dropped"

    day = server.mesh_list_note()
    assert day and day.startswith("The list of meshes: ") and "waited for it" in day, day
    assert server.mesh_list_note() is None, "the day's counts start again"
    print("mesh list OK")


if __name__ == "__main__":
    main()
