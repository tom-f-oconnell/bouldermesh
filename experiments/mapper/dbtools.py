"""Changes to a copy of a COLMAP 3.9.1 features database, made before a mapper sees it (and before COLMAP 4 migrates
it). Standard library and numpy only: nothing here needs COLMAP.

    dbtools.py thin <database> <out> <keypoints a photo>     the keypoints in most verified matches, spread out
    dbtools.py lens <database> <out>                         one camera for each video's frames of one size
    dbtools.py dedup <database> <out> <measure> <threshold>  without a video's frames that show what a kept one does
    dbtools.py check <database>                              the layout and indices COLMAP relies on

Each writes <out> (never the database it reads) and prints what it changed as JSON. The layout is COLMAP 3.9's:
cameras, images, keypoints (float32, 2, 4 or 6 a row: x and y first), descriptors (uint8), matches and
two_view_geometries (uint32 pairs of keypoint indices, the first column the image with the lower id), a pair's id
being image_id1 * 2147483647 + image_id2.
"""
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import numpy as np

MAX_IMAGE_ID = 2147483647
VIDEO = re.compile(r"^(yt_.+)_(\d+)\.[^.]+$")      # a video's frame, as the app names it: yt_<video>_<frame>.jpg
MIN_INLIERS = 12        # verified matches a pair needs to stay verified: bench.py's TwoViewGeometry.min_num_inliers


def pair(pair_id: int) -> tuple[int, int]:
    """(image_id1, image_id2) of a pair's id, image_id1 the lower."""
    b = pair_id % MAX_IMAGE_ID
    return (pair_id - b) // MAX_IMAGE_ID, b


def indices(blob: bytes | None) -> np.ndarray:
    """A matches blob as rows of (keypoint in the first image, keypoint in the second)."""
    return np.frombuffer(blob or b"", np.uint32).reshape(-1, 2)


def keypoints(con: sqlite3.Connection) -> dict[int, np.ndarray]:
    """Each image's keypoints, one row each (x, y, then shape if any)."""
    return {i: np.frombuffer(d or b"", np.float32).reshape(rows, cols) if rows else np.zeros((0, max(cols, 2)), np.float32)
            for i, rows, cols, d in con.execute("SELECT image_id, rows, cols, data FROM keypoints")}


def sizes(con: sqlite3.Connection) -> dict[int, tuple[int, int]]:
    """Each image's (width, height), from its camera."""
    return {i: (w, h) for i, w, h in con.execute(
        "SELECT image_id, width, height FROM images JOIN cameras ON images.camera_id = cameras.camera_id")}


def counts(con: sqlite3.Connection) -> dict:
    one = lambda sql: con.execute(sql).fetchone()[0] or 0      # noqa: E731
    return {"cameras": one("SELECT count(*) FROM cameras"), "images": one("SELECT count(*) FROM images"),
            "keypoints": one("SELECT sum(rows) FROM keypoints"), "matches": one("SELECT sum(rows) FROM matches"),
            "verified_pairs": one("SELECT count(*) FROM two_view_geometries WHERE rows > 0"),
            "verified_matches": one("SELECT sum(rows) FROM two_view_geometries")}


def spread(xy: np.ndarray, score: np.ndarray, n: int, width: int, height: int, grid: int = 12) -> np.ndarray:
    """Which n keypoints to keep: those in most verified matches, taken a cell of a grid at a time (each cell's best,
    then each cell's second best, ...) so that they cover the image rather than one textured patch of it.

    Returns:
        Their indices, ascending. Keypoints in no verified match are only kept (the first n) when none is in any.
    """
    idx = np.flatnonzero(score > 0)
    if len(idx) == 0:
        return np.arange(min(n, len(score)))
    if len(idx) <= n:
        return idx
    cx = np.clip((xy[idx, 0] / max(width, 1) * grid).astype(int), 0, grid - 1)
    cy = np.clip((xy[idx, 1] / max(height, 1) * grid).astype(int), 0, grid - 1)
    cell, s = cy * grid + cx, score[idx]
    order = np.lexsort((-s, cell))                       # by cell, the best of each first
    c = cell[order]
    starts = np.flatnonzero(np.r_[True, c[1:] != c[:-1]])
    rank = np.arange(len(order)) - np.repeat(starts, np.diff(np.r_[starts, len(order)]))     # place within its cell
    pick = np.lexsort((-s[order], rank))[:n]
    return np.sort(idx[order][pick])


def _open(src: Path, dst: Path) -> sqlite3.Connection:
    assert Path(src).resolve() != Path(dst).resolve(), "the database read is never the one written"
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    for extra in ("-wal", "-shm"):      # a copy of a database must not pick up another's journal
        Path(str(dst) + extra).unlink(missing_ok=True)
    return sqlite3.connect(dst)


def _close(con: sqlite3.Connection) -> dict:
    """Finishes a changed database as one file of its own (no journal beside it, no freed pages in it): its counts."""
    con.commit()
    con.execute("PRAGMA journal_mode = DELETE")
    con.execute("VACUUM")
    after = counts(con)
    con.close()
    return after


def thin(src: Path, dst: Path, n: int, min_inliers: int = MIN_INLIERS, grid: int = 12) -> dict:
    """dst: src with at most n keypoints a photo, the ones spread() picks, and every match rewritten to their new
    indices. A pair left with fewer than min_inliers verified matches stops being a verified pair (as one the matcher
    found too few for: no matches, configuration 0); its geometry otherwise stays as it was estimated from them all.

    Returns:
        Counts before and after, and how many verified pairs were lost.
    """
    con = _open(src, dst)
    before, kps, size = counts(con), keypoints(con), sizes(con)
    score = {i: np.zeros(len(k), np.int64) for i, k in kps.items()}
    for pid, blob in con.execute("SELECT pair_id, data FROM two_view_geometries WHERE rows > 0"):
        a, b = pair(pid)
        m = indices(blob)
        score[a] += np.bincount(m[:, 0], minlength=len(score[a]))
        score[b] += np.bincount(m[:, 1], minlength=len(score[b]))
    new = {}
    for i, k in kps.items():
        keep = spread(k[:, :2], score[i], n, *size.get(i, (1, 1)), grid=grid)
        new[i] = np.full(len(k), -1, np.int64)
        new[i][keep] = np.arange(len(keep))
        con.execute("UPDATE keypoints SET rows = ?, data = ? WHERE image_id = ?", (len(keep), k[keep].tobytes(), i))
        row = con.execute("SELECT rows, cols, data FROM descriptors WHERE image_id = ?", (i,)).fetchone()
        if row and row[0] == len(k):
            d = np.frombuffer(row[2] or b"", np.uint8).reshape(row[0], row[1])
            con.execute("UPDATE descriptors SET rows = ?, data = ? WHERE image_id = ?", (len(keep), d[keep].tobytes(), i))
    lost = 0
    for table in ("matches", "two_view_geometries"):
        for pid, blob in con.execute(f"SELECT pair_id, data FROM {table} WHERE rows > 0").fetchall():
            a, b = pair(pid)
            m = indices(blob)
            m = np.stack([new[a][m[:, 0]], new[b][m[:, 1]]], 1)
            m = m[(m >= 0).all(1)].astype(np.uint32)
            if table == "two_view_geometries" and len(m) < min_inliers:
                lost += 1
                con.execute("UPDATE two_view_geometries SET rows = 0, data = ?, config = 0 WHERE pair_id = ?", (b"", pid))
            else:
                con.execute(f"UPDATE {table} SET rows = ?, data = ? WHERE pair_id = ?", (len(m), m.tobytes(), pid))
    after = _close(con)
    return {"what": "thin", "keypoints_a_photo": n, "before": before, "after": after, "verified_pairs_lost": lost}


def videos(con: sqlite3.Connection) -> dict[str, list[tuple[int, str, int]]]:
    """Each video's frames in the database, in frame order: (image_id, name, camera_id)."""
    out = {}
    for i, name, cam in con.execute("SELECT image_id, name, camera_id FROM images"):
        if m := VIDEO.match(name):
            out.setdefault(m.group(1), []).append((int(m.group(2)), i, name, cam))
    return {v: [f[1:] for f in sorted(frames)] for v, frames in out.items()}


def lens(src: Path, dst: Path) -> dict:
    """dst: src with one camera for all of a video's frames of one size and camera model (the camera of its first
    frame), where src has one a frame; the others' cameras are deleted. Photos that aren't frames keep their own.

    Returns:
        Cameras before and after, and how many videos and frames now share.
    """
    con = _open(src, dst)
    before = counts(con)
    cams = {c: (model, w, h) for c, model, w, h in con.execute("SELECT camera_id, model, width, height FROM cameras")}
    shared = frames = 0
    for frames_of in videos(con).values():
        groups = {}
        for image_id, _, cam in frames_of:
            groups.setdefault(cams[cam], []).append((image_id, cam))
        for members in groups.values():
            keep = members[0][1]
            others = sorted({cam for _, cam in members} - {keep})
            if not others:
                continue
            shared, frames = shared + 1, frames + len(members)
            con.executemany("UPDATE images SET camera_id = ? WHERE image_id = ?", [(keep, i) for i, _ in members])
            for cam in others:      # a camera something else still uses (none does, with one a photo) is left
                if not con.execute("SELECT 1 FROM images WHERE camera_id = ? LIMIT 1", (cam,)).fetchone():
                    con.execute("DELETE FROM cameras WHERE camera_id = ?", (cam,))
    after = _close(con)
    return {"what": "lens", "before": before, "after": after, "groups_sharing": shared, "frames_sharing": frames}


def similarity_share(p: np.ndarray, q: np.ndarray, tolerance: float, trials: int = 64) -> float:
    """The share of matched points (p in one image, q in the other) that one shift, turn and zoom of the image
    explains to within tolerance pixels: backend/climbs/diversity.py's test of a pair without parallax (there,
    OpenCV's estimateAffinePartial2D on ORB matches), from the best of some two-point guesses, refitted once."""
    if len(p) < 2:
        return 0.0
    P, Q = p[:, 0] + 1j * p[:, 1], q[:, 0] + 1j * q[:, 1]
    rng = np.random.default_rng(0)
    i, j = rng.integers(0, len(P), trials), rng.integers(0, len(P), trials)
    ok = np.abs(P[i] - P[j]) > 1e-6
    i, j = i[ok], j[ok]
    if not len(i):
        return 0.0
    a = (Q[i] - Q[j]) / (P[i] - P[j])
    b = Q[i] - a * P[i]
    inl = np.abs(a[:, None] * P[None] + b[:, None] - Q[None]) < tolerance
    best = inl[inl.sum(1).argmax()]
    if best.sum() >= 2:         # least squares over the guess's own inliers
        pm, qm = P[best].mean(), Q[best].mean()
        den = (np.abs(P[best] - pm) ** 2).sum()
        if den > 1e-9:
            a1 = (np.conj(P[best] - pm) * (Q[best] - qm)).sum() / den
            best = np.maximum(best.sum(), (np.abs(a1 * P + (qm - a1 * pm) - Q) < tolerance).sum())
            return float(best / len(P))
    return float(best.sum() / len(P))


def near_duplicates(con: sqlite3.Connection, measure: str, threshold: float, min_inliers: int = 150) -> dict[str, dict]:
    """A video's frames that show what an earlier, kept frame of it already shows, going through each video in frame
    order. A frame is compared with each kept frame it shares at least min_inliers verified matches with (diversity.py's
    IDENTICAL_INLIERS), by:

    - "sim": the share of those matches a shift, turn and zoom explains, to 3 pixels at 1024 across, as diversity.py
      measures a pair with no parallax (it calls 0.9 or more the same shot, and only discounts the pair; here a lower
      threshold drops more);
    - "move": how far the matches moved, as a median share of the image's diagonal: under the threshold is a duplicate.

    Returns:
        Each dropped frame's name -> the kept frame it repeats, the measure's value and the matches it was taken over.
    """
    assert measure in ("sim", "move"), measure
    kps, size = keypoints(con), sizes(con)
    verified = {pid: rows for pid, rows in con.execute("SELECT pair_id, rows FROM two_view_geometries WHERE rows >= ?", (min_inliers,))}
    dropped = {}
    for frames in videos(con).values():
        kept = []
        for image_id, name, _ in frames:
            why = None
            for other, other_name in kept:
                a, b = min(other, image_id), max(other, image_id)
                pid = a * MAX_IMAGE_ID + b
                if pid not in verified:
                    continue
                m = indices(con.execute("SELECT data FROM two_view_geometries WHERE pair_id = ?", (pid,)).fetchone()[0])
                p, q = kps[a][m[:, 0], :2].astype(np.float64), kps[b][m[:, 1], :2].astype(np.float64)
                w, h = size[image_id]
                if measure == "sim":
                    value = similarity_share(p, q, 3.0 * max(1.0, max(w, h) / 1024))
                    same = value >= threshold
                else:
                    value = float(np.median(np.linalg.norm(p - q, axis=1)) / np.hypot(w, h))
                    same = value < threshold
                if same:
                    why = {"repeats": other_name, "value": round(value, 4), "matches": len(m)}
                    break
            if why:
                dropped[name] = why
            else:
                kept.append((image_id, name))
    return dropped


def dedup(src: Path, dst: Path, measure: str, threshold: float) -> dict:
    """dst: src without the frames near_duplicates() finds (their images, keypoints, descriptors, cameras no other
    image uses, and every pair they are in), so that no later step sees them.

    Returns:
        Counts before and after, the frames in videos, and each dropped frame with what it repeats.
    """
    con = _open(src, dst)
    before = counts(con)
    in_videos = sum(len(f) for f in videos(con).values())
    dropped = near_duplicates(con, measure, threshold)
    ids = {name: i for i, name in con.execute("SELECT image_id, name FROM images")}
    gone = {ids[name] for name in dropped}
    for i in gone:
        cam = con.execute("SELECT camera_id FROM images WHERE image_id = ?", (i,)).fetchone()[0]
        for table in ("keypoints", "descriptors", "images"):
            con.execute(f"DELETE FROM {table} WHERE image_id = ?", (i,))
        if not con.execute("SELECT 1 FROM images WHERE camera_id = ? LIMIT 1", (cam,)).fetchone():
            con.execute("DELETE FROM cameras WHERE camera_id = ?", (cam,))
    for table in ("matches", "two_view_geometries"):
        con.executemany(f"DELETE FROM {table} WHERE pair_id = ?",
                        [(pid,) for pid, in con.execute(f"SELECT pair_id FROM {table}").fetchall() if set(pair(pid)) & gone])
    after = _close(con)
    return {"what": "dedup", "measure": measure, "threshold": threshold, "before": before, "after": after,
            "frames_in_videos": in_videos, "dropped": len(dropped), "dropped_frames": dropped}


def check(db: Path) -> list[str]:
    """What is wrong with a database's layout or indices, for COLMAP to read it: nothing, if all is well. Every
    table and column of 3.9's layout there; each image's camera there; keypoints and descriptors a row each and
    their blobs the size their rows and columns say; each pair's two images there, the lower id first, and each
    match's two indices within those images' keypoints."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return _check(con)
    finally:
        con.close()


def _check(con: sqlite3.Connection) -> list[str]:
    bad = []
    want = {"cameras": {"camera_id", "model", "width", "height", "params", "prior_focal_length"},
            "images": {"image_id", "name", "camera_id"}, "keypoints": {"image_id", "rows", "cols", "data"},
            "descriptors": {"image_id", "rows", "cols", "data"}, "matches": {"pair_id", "rows", "cols", "data"},
            "two_view_geometries": {"pair_id", "rows", "cols", "data", "config", "F", "E", "H"}}
    for table, cols in want.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if cols - have:
            bad.append(f"{table}: no column(s) {sorted(cols - have)}")
    if bad:
        return bad
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        bad.append("sqlite's integrity check fails")
    cams = {c for c, in con.execute("SELECT camera_id FROM cameras")}
    n = {}
    for i, cam in con.execute("SELECT image_id, camera_id FROM images"):
        n[i] = 0
        if cam not in cams:
            bad.append(f"image {i}: its camera {cam} isn't there")
    for i, rows, cols, size in con.execute("SELECT image_id, rows, cols, length(data) FROM keypoints"):
        if i not in n:
            bad.append(f"keypoints of an image that isn't there: {i}")
            continue
        n[i] = rows
        if (size or 0) != rows * cols * 4 or (rows and cols not in (2, 4, 6)):
            bad.append(f"image {i}: keypoints blob of {size} bytes for {rows} x {cols}")
    for i, rows, cols, size in con.execute("SELECT image_id, rows, cols, length(data) FROM descriptors"):
        if i not in n or rows != n[i] or (size or 0) != rows * cols:
            bad.append(f"image {i}: {rows} descriptors ({size} bytes) for {n.get(i)} keypoints")
    for table in ("matches", "two_view_geometries"):
        for pid, rows, cols, blob in con.execute(f"SELECT pair_id, rows, cols, data FROM {table}"):
            a, b = pair(pid)
            if a not in n or b not in n or not a < b:
                bad.append(f"{table}: pair {pid} is of images {a} and {b}")
                continue
            m = indices(blob)
            if len(m) != rows or (rows and cols != 2):
                bad.append(f"{table}: pair {a}-{b} says {rows} x {cols}, holds {len(m)}")
            elif rows and (m[:, 0].max() >= n[a] or m[:, 1].max() >= n[b]):
                bad.append(f"{table}: pair {a}-{b} has an index past its image's keypoints")
            if len(bad) > 20:
                return bad
    return bad


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "thin":
        print(json.dumps(thin(Path(args[0]), Path(args[1]), int(args[2])), indent=1))
    elif cmd == "lens":
        print(json.dumps(lens(Path(args[0]), Path(args[1])), indent=1))
    elif cmd == "dedup":
        print(json.dumps(dedup(Path(args[0]), Path(args[1]), args[2], float(args[3])), indent=1))
    elif cmd == "check":
        problems = check(Path(args[0]))
        print("\n".join(problems) or "ok")
        sys.exit(1 if problems else 0)
