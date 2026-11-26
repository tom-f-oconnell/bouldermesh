#!/usr/bin/env python3
"""Staging folders that jobs cut off by a stop left in DATA/compute, deleted as the server starts
(server.clear_stale_stages()), and nothing else there.

    python3 tests/test_stale_stages.py
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
tmp = tempfile.mkdtemp(prefix="bouldermesh-stages-")
os.environ["DATA_DIR"] = tmp

import server  # noqa: E402

try:
    compute = server.DATA / "compute"
    for name in ("bm-beta-abc", "bm-filter-def", "bm-scenery-ghi", "bm-inputs-jkl", "jobs"):
        (compute / name).mkdir(parents=True, exist_ok=True)
        (compute / name / "f").write_bytes(b"x")
    (compute / "machines.sqlite").write_bytes(b"")
    (compute / "bm-beta-file").write_bytes(b"")     # a file of that name: not a stage
    assert server.clear_stale_stages() == 4
    for _ in range(100):
        if not any(d.name.startswith(server.STAGE_PREFIXES) and d.is_dir() for d in compute.iterdir()):
            break
        time.sleep(0.05)
    assert sorted(p.name for p in compute.iterdir()) == ["bm-beta-file", "ec2.sqlite", "jobs", "machines.sqlite"], list(compute.iterdir())
    assert (compute / "jobs" / "f").exists()
    assert server.clear_stale_stages() == 0
    print("ok")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
