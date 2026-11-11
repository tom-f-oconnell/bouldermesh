"""Making room on a server whose data a backup on another machine holds (deploy/backup.sh), and getting it back.

Nothing is deleted here that the backup can't be shown to hold. In two steps: a path chosen to make room is first marked
(the list file data/offload.list, which the backup reads before copying, and from then on keeps what it names however
the server's copy changes); it's deleted once the backup says a copy that began after the mark went well (the backup's
handshake, deploy/backup-handshake.py, writes the time that copy began to data/backed-up). A path wanted back is listed
in data/offload-wanted.list; the backup's next run sends it into data/restore-inbox/, and take_restores() puts it back.

Paths are relative to the data folder, and only of the kinds the backup's handshake takes back: a project's photos/,
masks/ or rejected/ (a folder, emptied rather than removed, so that what looks for it still finds it), a fetched video
in cache/videos/, or one file of a mesh in meshes/<id>/.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable

LIST, WANTED, MARK, INBOX = "offload.list", "offload-wanted.list", "backed-up", "restore-inbox"
PATH = re.compile(r"projects/[0-9a-f]{6,32}/(photos|masks|rejected)|cache/videos/[A-Za-z0-9_-]{11}\.[a-z0-9]{2,5}"
                  r"|meshes/[0-9a-f]{6,32}/[A-Za-z0-9_.-]{1,120}")
SCHEMA = """CREATE TABLE IF NOT EXISTS offloads(path TEXT PRIMARY KEY, kind TEXT, ref TEXT, bytes INTEGER, marked REAL,
              done REAL, wanted REAL)"""

Q = Callable[..., list]


def size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


def backed_up_at(data: Path) -> float | None:
    """When the backup's last good copy began, as its handshake said, or None if it never has."""
    try:
        return float((data / MARK).read_text().strip())
    except (OSError, ValueError):
        return None


def write_lists(data: Path, q: Q) -> None:
    """The lists the backup reads: every path marked or offloaded (which it keeps), and those wanted back."""
    for name, rows in ((LIST, q("SELECT path FROM offloads ORDER BY path")),
                       (WANTED, q("SELECT path FROM offloads WHERE done IS NOT NULL AND wanted IS NOT NULL ORDER BY path"))):
        tmp = data / (name + ".tmp")
        tmp.write_text("".join(r["path"] + "\n" for r in rows))
        tmp.replace(data / name)


def mark(data: Path, q: Q, path: str, kind: str, ref: str) -> int:
    """Mark a path to be offloaded once the backup holds it. Returns its size."""
    if not PATH.fullmatch(path):
        raise ValueError(f"not a path that can be offloaded: {path}")
    n = size(data / path)
    q("INSERT OR IGNORE INTO offloads(path, kind, ref, bytes, marked) VALUES(?,?,?,?,?)", path, kind, ref, n, time.time(), write=True)
    return n


def away(q: Q, path: str) -> bool:
    """Whether a path's files are only in the backup now."""
    return bool(q("SELECT 1 FROM offloads WHERE path=? AND done IS NOT NULL", path))


def drop(q: Q, paths) -> None:
    """Forget paths marked or offloaded, as when what they named is made again (a climb harvested again)."""
    for p in paths:
        q("DELETE FROM offloads WHERE path=?", p, write=True)


def finish(data: Path, q: Q, still: Callable[[dict], bool], log: Callable[[str], None], need: float | None = None) -> int:
    """Delete what was marked before the backup's last good copy began, if it's still to go (still(row)), in the order
    marked, until need bytes are freed (all, if None); unmark what's no longer to go. Returns the bytes freed."""
    since, freed = backed_up_at(data), 0
    for row in q("SELECT * FROM offloads WHERE done IS NULL ORDER BY marked"):
        if not still(row):
            q("DELETE FROM offloads WHERE path=?", row["path"], write=True)
            continue
        if since is None or row["marked"] >= since or (need is not None and freed >= need):
            continue        # the backup hasn't copied it since it was marked, or enough is freed
        target = data / row["path"]
        n = size(target)
        if target.is_dir():
            for f in list(target.iterdir()):
                shutil.rmtree(f) if f.is_dir() else f.unlink()
        else:
            target.unlink(missing_ok=True)
        q("UPDATE offloads SET done=?, bytes=? WHERE path=?", time.time(), n or row["bytes"], row["path"], write=True)
        freed += n
    if freed:
        log(f"offload: {freed / 2**20:.0f} MB the backup holds deleted here")
    return freed


def want(data: Path, q: Q, paths) -> list[str]:
    """Ask for paths offloaded to be brought back, on the backup's next run. Returns those that are away."""
    asked = []
    for p in paths:
        if away(q, p):
            q("UPDATE offloads SET wanted=COALESCE(wanted, ?) WHERE path=?", time.time(), p, write=True)
            asked.append(p)
    if asked:
        write_lists(data, q)
    return asked


def take_restores(data: Path, q: Q, log: Callable[[str], None]) -> list[str]:
    """Put back what the backup sent into the inbox (the handshake's restore), for paths wanted back. Returns them."""
    inbox, back = data / INBOX, []
    if not inbox.is_dir():
        return back
    for row in q("SELECT path FROM offloads WHERE done IS NOT NULL AND wanted IS NOT NULL"):
        src, dst = inbox / row["path"], data / row["path"]
        if not src.exists():
            continue
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                if f.is_file():
                    os.replace(f, dst / f.name)
            shutil.rmtree(src, ignore_errors=True)
            times = [f.stat().st_mtime for f in dst.iterdir() if f.is_file()]
            if times:   # as it was before (the files keep theirs): what compares a harvest's photos with a pool's sees no change
                os.utime(dst, (max(times), max(times)))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
        q("DELETE FROM offloads WHERE path=?", row["path"], write=True)
        back.append(row["path"])
    if back:
        write_lists(data, q)
        log(f"offload: {len(back)} put back from the backup")
    for leftover in sorted(inbox.rglob("*"), reverse=True):     # anything not wanted, or emptied folders
        if leftover.is_dir():
            try:
                leftover.rmdir()
            except OSError:
                pass
    return back


def state(data: Path, q: Q) -> dict:
    """For the admin page: what's offloaded, marked, and wanted back, by kind, and when the backup last said it held it all."""
    out = {"backed_up": backed_up_at(data), "away": {}, "marked": {}, "wanted": 0}
    for r in q("SELECT kind, done IS NOT NULL AS done, COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS bytes FROM offloads GROUP BY kind, done IS NOT NULL"):
        out["away" if r["done"] else "marked"][r["kind"]] = {"n": r["n"], "bytes": r["bytes"]}
    out["wanted"] = q("SELECT COUNT(*) AS n FROM offloads WHERE done IS NOT NULL AND wanted IS NOT NULL")[0]["n"]
    return out
