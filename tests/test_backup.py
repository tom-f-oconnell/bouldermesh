#!/usr/bin/env python3
"""Backups: the server's safe copies of its databases, and deploy/backup.sh copying a server's checkout here (from a
local folder standing in for the server): what it takes and leaves, the databases kept by night, and what a later copy
replaced or deleted kept aside. Offline; needs rsync.

    python3 tests/test_backup.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="bouldermesh-backup-"))
checkout = TMP / "server-checkout"
os.environ["DATA_DIR"] = str(checkout / "data")
sys.path.insert(0, str(ROOT / "backend"))
import server  # noqa: E402

if not shutil.which("rsync"):
    print("rsync not installed: skipping")
    sys.exit(0)
try:
    server.init_db()
    server.q("INSERT INTO projects(id,name,created,kind,status) VALUES('aaaaaaaaaaaa','x',0,'harvest','done')", write=True)
    made = server.snapshot_databases()
    assert {p.name for p in made} >= {"bouldermesh.sqlite.backup", "volunteers.sqlite.backup", "machines.sqlite.backup"}, made
    snap = sqlite3.connect(server.SNAPSHOTS / "bouldermesh.sqlite.backup")
    assert snap.execute("SELECT name FROM projects").fetchall() == [("x",)] and oct((server.SNAPSHOTS / "bouldermesh.sqlite.backup").stat().st_mode)[-3:] == "600"
    snap.close()
    print("snapshots OK")

    data = checkout / "data"
    for rel, body in (("meshes/m1/mesh.glb", b"glb"), ("meshes/m2/mesh.glb", b"glb2"), ("projects/aaaaaaaaaaaa/photos/p.jpg", b"jpg"),
                      ("projects/aaaaaaaaaaaa/rejected/r.jpg", b"rej"), ("projects/aaaaaaaaaaaa/work/big.bin", b"work"),
                      ("projects/aaaaaaaaaaaa/sources.json", b"{}"), ("cache/videos/vvvvvvvvvvv.mp4", b"video"),
                      ("cache/http/page", b"html"), ("cache/models/w.pt", b"weights"), ("uploads/u.part", b"up"),
                      ("compute/jobs/j.in.tar", b"tar"), ("bouldermesh.sqlite-wal", b"wal"), ("mesh-views.jsonl", b"{}\n")):
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        (data / rel).write_bytes(body)
    (checkout / ".env").write_text("EDIT_TOKEN=secret\n")
    (checkout / "backend").mkdir()
    (checkout / "backend" / "server.py").write_text("code")
    dest = TMP / "backups"
    env = {**os.environ, "BACKUP_DIR": str(dest)}
    run = lambda: subprocess.run(["sh", str(ROOT / "deploy" / "backup.sh"), str(checkout)], env=env, capture_output=True, text=True)  # noqa: E731
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    got = {str(p.relative_to(dest / "current")) for p in (dest / "current").rglob("*") if p.is_file()}
    assert got == {".env", "data/meshes/m1/mesh.glb", "data/meshes/m2/mesh.glb", "data/projects/aaaaaaaaaaaa/photos/p.jpg",
                   "data/projects/aaaaaaaaaaaa/rejected/r.jpg", "data/projects/aaaaaaaaaaaa/sources.json", "data/cache/videos/vvvvvvvvvvv.mp4",
                   "data/mesh-views.jsonl", "data/snapshots/bouldermesh.sqlite.backup", "data/snapshots/volunteers.sqlite.backup",
                   "data/snapshots/machines.sqlite.backup"}, sorted(got)
    nights = list((dest / "db").iterdir())
    assert len(nights) == 1 and {p.name for p in nights[0].iterdir()} >= {"bouldermesh.sqlite.backup"}
    assert oct(dest.stat().st_mode)[-3:] == "700" and (dest / "last-ok").exists()

    # the next night: a mesh deleted and a photo replaced on the server; the copy follows, keeping what it had aside
    shutil.rmtree(data / "meshes" / "m2")
    (data / "projects" / "aaaaaaaaaaaa" / "photos" / "p.jpg").write_bytes(b"jpg, again")
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (dest / "current" / "data" / "meshes" / "m2").exists()
    aside = {str(p.relative_to(next((dest / "changed").iterdir()))) for p in (dest / "changed").rglob("*") if p.is_file()}
    assert {"data/meshes/m2/mesh.glb", "data/projects/aaaaaaaaaaaa/photos/p.jpg"} <= aside, aside
    print("backup.sh OK")

    # through SSH as the public server takes the backup key: rsync's command forced to rrsync -ro, rooted at the checkout
    if shutil.which("rrsync"):
        fake_ssh = TMP / "fake-ssh"
        fake_ssh.write_text(f"""#!/bin/sh
# ssh [options] host command...: the host's forced command, rrsync -ro {checkout}, given the command asked for
while [ "$1" != rsync ]; do shift; done
SSH_ORIGINAL_COMMAND="$*" exec rrsync -ro {checkout}
""")
        fake_ssh.chmod(0o755)
        shutil.rmtree(dest)
        r = subprocess.run(["sh", str(ROOT / "deploy" / "backup.sh"), "tom@server.example:"], env={**env, "BACKUP_SSH": str(fake_ssh)},
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (dest / "current" / ".env").exists() and (dest / "current" / "data" / "meshes" / "m1" / "mesh.glb").exists()
        assert not (dest / "current" / "backend").exists() and not (dest / "current" / "data" / "cache" / "http").exists()
        print("through rrsync OK")
    else:
        print("rrsync not installed: skipping the forced command")
    # a source with no data/, as the home folder is when a key without the restriction logs in: a failure, not a backup
    (TMP / "home").mkdir()
    r = subprocess.run(["sh", str(ROOT / "deploy" / "backup.sh"), str(TMP / "home")], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "no data/ came" in r.stderr, r.stdout + r.stderr
    print("nothing to copy OK")
    print("ALL BACKUP TESTS PASSED")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
