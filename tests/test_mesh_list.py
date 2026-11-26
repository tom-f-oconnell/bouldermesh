#!/usr/bin/env python3
"""The kept /api/meshes (server._mesh_list_for()): fresh copies, an older one given while it's made again in the
background, a change waited for only by who made it, rows for links, copies saved for the next start, and the counts
of each. Offline, with the list's making stubbed.

    python3 tests/test_mesh_list.py
"""
from __future__ import annotations

import os
import pathlib
import json
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
    # an editor's change meanwhile: others get the older copy, made again in the background; the editor waits for a
    # making begun since, not the one begun before the change
    server.forget_mesh_list(("editor", "e1"))
    assert get() == b'{"n": 2}', "others aren't kept waiting by another's change"
    mine = server._mesh_list_for(None, server.MESH_LIST_TTL, make, ("editor", "e1"))
    assert mine == b'{"n": 3}', mine
    release.set()
    for _ in range(100):
        if not server._mesh_lists_making:
            break
        time.sleep(0.02)
    assert get() == b'{"n": 3}', "the making begun before the change isn't kept over the one begun since"
    assert server._mesh_lists_kept[None].began >= server._mesh_list_changes[("editor", "e1")]

    # rows for links, and a copy saved and read back by the next start
    server.MESH_LIST_SAVE_EVERY = 0
    rows = [{"id": "a1", "climbs": [{"id": "c1"}]}, {"id": "b2", "adds_to": "a1", "boulder_climbs": [{"id": "c2"}]}]
    key = (True, "e1")
    server._mesh_list_for(key, 5, lambda: rows)
    assert server._mesh_list_for(key, 5, lambda: []) == json.dumps(rows).encode(), "the same JSON as made whole"
    assert [json.loads(r)["id"] for r in server.list_rows(key, climbs=["c2"])] == ["b2", "a1"], "with the mesh it adds to"
    assert [json.loads(r)["id"] for r in server.list_rows(key, ["a1"], ["c1"])] == ["a1"]
    assert server.list_rows(key, ["zz"]) == [] and server.list_rows((True, "nobody"), ["a1"]) is None
    saved = sorted(f.name for f in server.MESH_LIST_SAVED.glob("*.json"))
    assert saved == ["editor-e1.json", "visitors.json"], saved
    server._mesh_lists_kept.clear()
    assert server.load_mesh_lists() == 2
    hold, began = threading.Event(), time.monotonic()
    assert server._mesh_list_for(key, 5, lambda: (hold.wait(10), [])[1]) == json.dumps(rows).encode(), "the saved copy at once"
    assert time.monotonic() - began < 0.5 and key in server._mesh_lists_making, "while it's made again"
    hold.set()

    day = server.mesh_list_note()
    assert day and day.startswith("The list of meshes: ") and "waited for it" in day, day
    assert server.mesh_list_note() is None, "the day's counts start again"
    print("mesh list OK")


if __name__ == "__main__":
    main()
