#!/usr/bin/env python3
"""Forget the oldest fetched videos offloaded to the backup, the first of two steps to deleting them.

The server never deletes a fetched video by itself: past VIDEO_CACHE_GB (or the data's cap) it offloads those nothing
needs any more to the backup, which keeps them in current/ as long as the server lists them (backend/offload.py). This
forgets the oldest of those, by when they were offloaded, up to the given number of GB: no longer listed in
data/offload.list, the backup's next copy moves them into its archive/, from which deploy/backup-delete-videos.py
deletes the oldest, by hand. Their details (<id>.json) stay; a harvest wanting one again asks volunteers for it. Those
asked back, or lost, are left alone. It only lists what it would forget, unless --yes.

Run where the server's data is, as in its container:
    docker compose -f docker-compose.public.yml exec bouldermesh python3 backend/forget_videos.py 20
    docker compose -f docker-compose.public.yml exec bouldermesh python3 backend/forget_videos.py 20 --yes
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import offload as offload_mod  # noqa: E402


def connect(data: Path):
    con = sqlite3.connect(data / "bouldermesh.sqlite", timeout=60)
    con.row_factory = sqlite3.Row

    def q(sql: str, *args, one: bool = False, write: bool = False):
        cur = con.execute(sql, args)
        if write:
            con.commit()
            return None
        rows = [dict(r) for r in cur.fetchall()]
        return (rows[0] if rows else None) if one else rows
    return con, q


def oldest(q, gb: float) -> list[dict]:
    """The videos offloaded longest ago, up to gb GB in all."""
    out, total = [], 0
    for r in q("SELECT path, bytes, done FROM offloads WHERE path LIKE 'cache/videos/%' AND done IS NOT NULL AND wanted IS NULL "
               "AND lost IS NULL ORDER BY done"):
        if total + (r["bytes"] or 0) > gb * 2**30:
            break
        out.append(r)
        total += r["bytes"] or 0
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("gb", type=float, help="how many GB of the oldest offloaded videos to forget")
    ap.add_argument("--yes", action="store_true", help="forget them (otherwise only say which)")
    a = ap.parse_args()
    data = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
    con, q = connect(data)
    try:
        rows = oldest(q, a.gb)
        offloaded = q("SELECT count(*) AS n, COALESCE(sum(bytes), 0) AS b FROM offloads WHERE path LIKE 'cache/videos/%' "
                      "AND done IS NOT NULL", one=True)
        print(f"{offloaded['n']} videos offloaded to the backup, {offloaded['b'] / 2**30:.1f} GB")
        if not rows:
            print("none to forget")
            return
        day = lambda t: time.strftime("%Y-%m-%d", time.localtime(t))  # noqa: E731
        print(f"the oldest {len(rows)}, {sum(r['bytes'] or 0 for r in rows) / 2**30:.1f} GB, offloaded "
              f"{day(rows[0]['done'])} to {day(rows[-1]['done'])}")
        if not a.yes:
            print("nothing forgotten: run again with --yes to")
            return
        offload_mod.drop(q, [r["path"] for r in rows])
        offload_mod.write_lists(data, q)
        print("forgotten: the backup's next copy moves them into its archive/ (deploy/backup-delete-videos.py deletes from there)")
    finally:
        con.close()


if __name__ == "__main__":
    main()
