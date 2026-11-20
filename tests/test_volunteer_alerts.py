#!/usr/bin/env python3
"""What of YouTube's throttles and refusals of volunteers reaches the admin's phone (only runs in a row longer than lately),
and the day's summary's line on volunteers fetching clearly fewer videos than usual. Offline.

    python3 tests/test_volunteer_alerts.py
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
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="bouldermesh-valerts-")

import server  # noqa: E402

server.init_db()
sent: list[tuple[str, str]] = []
server.NOTIFIER.send = lambda title, text, **_: sent.append((title, text))
server.threading = type("T", (), {"Thread": lambda target, args=(), kwargs=None, daemon=None: type(
    "R", (), {"start": lambda self: target(*args, **(kwargs or {}))})(), "Event": threading.Event, "Lock": threading.Lock})
V = server.VOLUNTEERS
now = time.time()
for vid, name in (("v1", "ace"), ("v2", "tay")):
    V.q("INSERT INTO volunteers(id, name, created) VALUES(?,?,?)", vid, name, now - 30 * 86400)


def log(vid: str, health: str, ago_h: float) -> None:
    V.q("INSERT INTO youtube_log(at, volunteer, kind, status, health) VALUES(?,?,?,?,?)", now - ago_h * 3600, vid, "video",
        "refused" if health == "refused" else "ok" if health == "ok" else "failed", health)


def trouble(vid: str, ago_h: float, health: str = "throttled") -> None:
    """A report, and the server told of it, as the broker does (after logging it)."""
    log(vid, health, ago_h)
    server.volunteer_troubled({"id": vid, "name": dict(v1="ace", v2="tay")[vid]}, "ok", health, "Sign in to confirm you're not a bot")


# lately: singles and a pair, as usual
for h, kind in ((100, "throttled"), (99, "ok"), (90, "throttled"), (89, "throttled"), (88, "ok")):
    log("v1", kind, h)
assert server.trouble_runs("v1", now - 7 * 86400) == [1, 2, 0]
trouble("v1", 10)
trouble("v1", 9)
assert sent == [], "one or two in a row: as usual, untold"
trouble("v1", 8, "refused")
assert len(sent) == 1 and "3 times in a row" in sent[0][0] and "before: 2" in sent[0][1], sent
trouble("v1", 7)
assert len(sent) == 1, "told once a run"
log("v1", "ok", 6)
for h in (5, 4, 3):
    trouble("v1", h)
assert len(sent) == 1, "no longer than the last run (4): untold"
trouble("v1", 2)
assert len(sent) == 1, "as long as it (4): untold"
trouble("v1", 1)
assert len(sent) == 2 and "5 times" in sent[1][0], sent
print("runs in a row OK")

# pace: ace usually ~100 a day, tay ~100; ace's day drops to 30 while tay's holds
for d in range(1, 8):
    for vid in ("v1", "v2"):
        for i in range(100):
            V.q("INSERT INTO tasks(id, kind, video_id, state, created, volunteer, finished) VALUES(?,?,?,?,?,?,?)",
                f"{vid}-{d}-{i}", "video", "x", "done", now - d * 86400 - 600, vid, now - d * 86400 - 300)
for vid, n in (("v1", 30), ("v2", 95)):
    for i in range(n):
        V.q("INSERT INTO tasks(id, kind, video_id, state, created, volunteer, finished) VALUES(?,?,?,?,?,?,?)",
            f"{vid}-0-{i}", "video", "x", "done", now - 3600, vid, now - 600)
note = server.volunteer_pace_note(now)
assert "ace fetched 30" in note and "tay" not in note, note
V.q("UPDATE tasks SET volunteer='v1' WHERE id LIKE 'v2-0-%' AND CAST(substr(id, 6) AS INT) >= 30")    # both fell alike
V.q("DELETE FROM tasks WHERE id LIKE 'v2-0-%' AND CAST(substr(id, 6) AS INT) >= 30")
V.q("DELETE FROM tasks WHERE id LIKE 'v1-0-%' AND CAST(substr(id, 6) AS INT) >= 30")
assert server.volunteer_pace_note(now) == "", "less to fetch for everyone: nothing said"
print("pace OK")
print("ALL VOLUNTEER ALERT TESTS PASSED")
