#!/usr/bin/env python3
"""Videos volunteers are asked for that the backup keeps in its archive/: asked of the backup instead, not given to
volunteers meanwhile (unless it doesn't send them in time), and done once it has sent them, as if a volunteer had,
so that what wanted them carries on. Offline: the broker is real, the backup a file and an inbox.

    python3 tests/test_archive_restore.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = tempfile.mkdtemp(prefix="bouldermesh-archive-")
os.environ["DATA_DIR"] = tmp

import server  # noqa: E402

try:
    server.init_db()
    D = server.DATA
    B = server.VOLUNTEERS
    ready = []
    B.ready = lambda purpose, ref: ready.append((purpose, ref))
    v = B.volunteer(B.add("vol")["token"])
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        assert B.want("video", vid, "harvest", "p1")
    (D / "backup-archived.txt").write_text("9\t1700000000.5\tcache/videos/aaaaaaaaaaa.mp4\n2\t1700000000\tcache/videos/aaaaaaaaaaa.json\n"
                                           "bad line\n5\t1\t/etc/passwd\n")
    assert set(server.offload_mod.archived(D)) == {"aaaaaaaaaaa"}, "only good lines, and only videos"

    # asked of the backup, with its details; not given to volunteers meanwhile; the other is
    assert server.restore_from_archive() == 1
    assert set((D / "offload-wanted.list").read_text().split()) == {"cache/videos/aaaaaaaaaaa.mp4", "cache/videos/aaaaaaaaaaa.json"}
    assert server.restore_from_archive() == 0, "once"
    got = B.work(v, "10.0.0.1", kinds=["video"])["task"]
    assert got["video_id"] == "bbbbbbbbbbb", got
    assert B.pending("harvest", "p1") == 2, "both still coming: what waits for them waits on"
    print("asking the archive OK")

    # the backup sends it: in the cache, its task done, and what wanted it told once the other has come too
    inbox = D / "restore-inbox" / "cache" / "videos"
    inbox.mkdir(parents=True)
    (inbox / "aaaaaaaaaaa.mp4").write_bytes(b"old video")
    (inbox / "aaaaaaaaaaa.json").write_text("{}")
    did = server.offload_step()
    assert sorted(did["back"]) == ["cache/videos/aaaaaaaaaaa.json", "cache/videos/aaaaaaaaaaa.mp4"], did
    assert server.youtube_mod.cached("aaaaaaaaaaa") and (D / "cache/videos/aaaaaaaaaaa.json").exists()
    assert B.q("SELECT state FROM tasks WHERE video_id='aaaaaaaaaaa'", one=True)["state"] == "done"
    assert not server.q("SELECT 1 FROM archive_wants") and not (D / "offload-wanted.list").read_text().strip()
    assert B.pending("harvest", "p1") == 1 and not ready, "the other still coming"
    B.fulfil(got["id"])      # (as if it came too)
    assert ready == [("harvest", "p1")], ready
    print("sent back OK")

    # one the backup doesn't send in time goes to volunteers after all, and isn't asked of it again
    assert B.want("video", "ccccccccccc", "beta", "m1")
    with open(D / "backup-archived.txt", "a") as f:
        f.write("3\t1700000000\tcache/videos/ccccccccccc.webm\n")
    assert server.restore_from_archive() == 1
    assert "task" not in B.work(v, "10.0.0.1", kinds=["video"]), "held for the backup"
    old = time.time() - server.volunteers_mod.RESTORE_WAIT - 60
    B.q("UPDATE tasks SET restoring=? WHERE video_id='ccccccccccc'", old)
    server.q("UPDATE archive_wants SET asked=?", old, write=True)
    assert server.restore_from_archive() == 0 and not server.q("SELECT 1 FROM archive_wants"), "forgotten, not asked again"
    assert B.work(v, "10.0.0.1", kinds=["video"])["task"]["video_id"] == "ccccccccccc"
    print("not sent in time OK")
    print("ALL ARCHIVE RESTORE TESTS PASSED")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
