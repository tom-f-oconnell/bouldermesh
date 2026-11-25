#!/usr/bin/env python3
"""Deleting kept videos, which is only by hand: forget_videos.py forgets the oldest offloaded ones (the backup then
moves them into archive/), and deploy/backup-delete-videos.py deletes the oldest from archive/. Each only lists what it
would do without --yes.

    python3 tests/test_forget_videos.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import offload  # noqa: E402

tmp = pathlib.Path(tempfile.mkdtemp(prefix="bouldermesh-forget-"))
try:
    data = tmp / "data"
    data.mkdir()
    con = sqlite3.connect(data / "bouldermesh.sqlite")
    offload.migrate(con)
    now = time.time()
    for i, (done, wanted) in enumerate(((now - 30 * 86400, None), (now - 20 * 86400, None), (now - 10 * 86400, None),
                                        (now - 40 * 86400, now), (None, None))):
        con.execute("INSERT INTO offloads(path,kind,ref,bytes,marked,done,wanted) VALUES(?,?,?,?,?,?,?)",
                    (f"cache/videos/{str(i) * 11}.mp4", "videos", str(i) * 11, 2**30, now - 50 * 86400, done, wanted))
    con.commit()
    con.close()
    env = {**os.environ, "DATA_DIR": str(data)}
    run = lambda *a: subprocess.run([sys.executable, *a], env=env, capture_output=True, text=True, check=True).stdout  # noqa: E731
    out = run(str(ROOT / "backend/forget_videos.py"), "2.5")
    assert "the oldest 2, 2.0 GB" in out and "nothing forgotten" in out, out
    assert len(sqlite3.connect(data / "bouldermesh.sqlite").execute("SELECT * FROM offloads").fetchall()) == 5
    out = run(str(ROOT / "backend/forget_videos.py"), "2.5", "--yes")
    left = {r[0] for r in sqlite3.connect(data / "bouldermesh.sqlite").execute("SELECT path FROM offloads")}
    assert left == {"cache/videos/22222222222.mp4", "cache/videos/33333333333.mp4", "cache/videos/44444444444.mp4"}, \
        "the two offloaded longest ago; not one asked back, nor one only marked"
    assert "00000000000" not in (data / "offload.list").read_text() and "22222222222" in (data / "offload.list").read_text()
    print("forgetting OK")

    backup = tmp / "backup"
    (backup / "current").mkdir(parents=True)
    videos = backup / "archive/data/cache/videos"
    videos.mkdir(parents=True)
    for i, age in enumerate((30, 10, 20)):
        for name in (f"{str(i) * 11}.mp4", f"{str(i) * 11}.json"):
            (videos / name).write_bytes(b"\0" * 1000)
            os.utime(videos / name, (now - age * 86400, now - age * 86400))
    (videos / "00000000000.mp4.~2026-11-01~").write_bytes(b"\0")
    out = run(str(ROOT / "deploy/backup-delete-videos.py"), str(backup), str(2500 / 2**30))
    assert "3 videos kept" in out and "the oldest 2" in out and "nothing deleted" in out and (videos / "00000000000.mp4").exists(), out
    run(str(ROOT / "deploy/backup-delete-videos.py"), str(backup), str(2500 / 2**30), "--yes")
    assert sorted(f.name for f in videos.iterdir()) == ["00000000000.json", "11111111111.json", "11111111111.mp4", "22222222222.json"], \
        "the two used longest ago, and an earlier copy of one; their details stay"
    print("deleting from the backup OK")
    print("ALL FORGET VIDEO TESTS PASSED")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
