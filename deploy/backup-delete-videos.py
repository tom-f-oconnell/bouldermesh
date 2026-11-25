#!/usr/bin/env python3
"""Delete the oldest fetched videos the backup keeps for good (archive/data/cache/videos/), to make room on it: the one
way a fetched video is ever deleted. Run by hand, on whatever holds the backup (the server, for the Storage Box mounted
there). Only lists what it would delete, oldest (by the time each file carries: when the server last used it) first,
up to the given number of GB, unless --yes. Their details (<id>.json) stay. A harvest wanting one again asks volunteers.

Videos the server has offloaded are in current/ instead, kept as long as it lists them: forget the oldest of those first
on the server (backend/forget_videos.py), and the backup's next copies move them into archive/.

    deploy/backup-delete-videos.py ~/storagebox/bouldermesh-backups 20
    deploy/backup-delete-videos.py ~/storagebox/bouldermesh-backups 20 --yes
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path


def oldest(videos: Path, gb: float) -> list[tuple[Path, int, float]]:
    """The videos there used longest ago, up to gb GB in all: (file, bytes, its time)."""
    files = []
    for f in videos.iterdir() if videos.is_dir() else []:
        if f.is_file() and f.suffix != ".json" and ".~" not in f.name:
            st = f.stat()
            files.append((f, st.st_size, st.st_mtime))
    out, total = [], 0
    for f, n, t in sorted(files, key=lambda x: x[2]):
        if total + n > gb * 2**30:
            break
        out.append((f, n, t))
        total += n
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("backup", type=Path, help="the backup's folder (BACKUP_DIR)")
    ap.add_argument("gb", type=float, help="how many GB of the oldest kept videos to delete")
    ap.add_argument("--yes", action="store_true", help="delete them (otherwise only say which)")
    a = ap.parse_args()
    videos = a.backup / "archive" / "data" / "cache" / "videos"
    if not (a.backup / "current").is_dir():
        raise SystemExit(f"{a.backup} isn't a backup's folder (no current/ in it)")
    rows = oldest(videos, a.gb)
    have = sum(1 for f in videos.iterdir() if f.suffix != ".json" and ".~" not in f.name) if videos.is_dir() else 0
    print(f"{have} videos kept for good in {videos}")
    if not rows:
        print("none to delete")
        return
    day = lambda t: time.strftime("%Y-%m-%d", time.localtime(t))  # noqa: E731
    print(f"the oldest {len(rows)}, {sum(n for _, n, _ in rows) / 2**30:.1f} GB, last used {day(rows[0][2])} to {day(rows[-1][2])}")
    if not a.yes:
        print("nothing deleted: run again with --yes to")
        return
    for f, _, _ in rows:
        f.unlink()
        for old in videos.glob(f.name + ".~*~"):    # its earlier copies, kept beside it
            old.unlink()
    print(f"deleted {len(rows)}")


if __name__ == "__main__":
    main()
