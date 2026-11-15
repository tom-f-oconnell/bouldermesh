"""Tests of dbtools.py's database changes, on a small made-up database in COLMAP 3.9's layout (and, where hal's
benchmark folder is there, on a copy of a real one). No COLMAP needed: python3 test_dbtools.py (or pytest).
"""
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dbtools  # noqa: E402

REAL = Path("/data/tom/mapperbench/runs/e6de7747fab5/database.db")      # the 33-photo boulder's, 60 MB
SCHEMA = """
CREATE TABLE cameras (camera_id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, model INTEGER NOT NULL, width INTEGER NOT NULL,
    height INTEGER NOT NULL, params BLOB, prior_focal_length INTEGER NOT NULL);
CREATE TABLE images (image_id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, name TEXT NOT NULL UNIQUE, camera_id INTEGER NOT NULL,
    prior_qw REAL, prior_qx REAL, prior_qy REAL, prior_qz REAL, prior_tx REAL, prior_ty REAL, prior_tz REAL);
CREATE UNIQUE INDEX index_name ON images(name);
CREATE TABLE keypoints (image_id INTEGER PRIMARY KEY NOT NULL, rows INTEGER NOT NULL, cols INTEGER NOT NULL, data BLOB);
CREATE TABLE descriptors (image_id INTEGER PRIMARY KEY NOT NULL, rows INTEGER NOT NULL, cols INTEGER NOT NULL, data BLOB);
CREATE TABLE matches (pair_id INTEGER PRIMARY KEY NOT NULL, rows INTEGER NOT NULL, cols INTEGER NOT NULL, data BLOB);
CREATE TABLE two_view_geometries (pair_id INTEGER PRIMARY KEY NOT NULL, rows INTEGER NOT NULL, cols INTEGER NOT NULL, data BLOB,
    config INTEGER NOT NULL, F BLOB, E BLOB, H BLOB, qvec BLOB, tvec BLOB);
"""
# name, (width, height), how the scene's points land in it: a shift of them all, and a shift that grows with depth
PHOTOS = [("yt_vidA_000.jpg", (1280, 720), (0, 0), 0), ("yt_vidA_001.jpg", (1280, 720), (4, 2), 0),      # 001 repeats 000
          ("yt_vidA_002.jpg", (1280, 720), (30, 0), 160), ("yt_vidA_003.jpg", (1280, 720), (-60, 10), 320),
          ("yt_vidB_000.jpg", (1920, 1080), (100, 50), 90), ("mountainproject_abc.jpg", (1280, 720), (10, 10), 240)]


def make(path: Path, points: int = 900, seen: float = 0.8, weak_pair: tuple[int, int] = (5, 6)) -> None:
    """A database of PHOTOS: one camera a photo, the same scene points seen in each (most of them), a third of the
    points bunched in one corner, every pair matched and verified; weak_pair with only 14 verified matches."""
    rng = np.random.default_rng(1)
    world = np.r_[rng.uniform([100, 100], [1100, 600], (points * 2 // 3, 2)), rng.uniform([100, 100], [200, 180], (points // 3, 2))]
    depth = rng.uniform(0, 1, len(world))
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    views = {}
    for i, (name, (w, h), shift, parallax) in enumerate(PHOTOS, 1):
        cam = len(PHOTOS) + 1 - i          # not in the images' order, as in COLMAP's own databases
        con.execute("INSERT INTO cameras VALUES (?, 2, ?, ?, ?, 0)", (cam, w, h, np.array([1.2 * w, w / 2, h / 2, 0.0]).tobytes()))
        con.execute("INSERT INTO images (image_id, name, camera_id) VALUES (?, ?, ?)", (i, name, cam))
        ids = np.flatnonzero(rng.uniform(size=len(world)) < seen)
        xy = world[ids] + shift + np.c_[parallax * depth[ids], np.zeros(len(ids))] + rng.normal(0, 0.3, (len(ids), 2))
        junk = rng.uniform([0, 0], [w, h], (150, 2))                 # keypoints that match nothing
        order = rng.permutation(len(ids) + len(junk))
        kp = np.c_[np.r_[xy, junk], np.tile([1, 0, 0, 1], (len(order), 1))][order].astype(np.float32)
        views[i] = {int(p): int(np.flatnonzero(order == k)[0]) for k, p in enumerate(ids)}       # scene point -> keypoint
        con.execute("INSERT INTO keypoints VALUES (?, ?, 6, ?)", (i, len(kp), kp.tobytes()))
        con.execute("INSERT INTO descriptors VALUES (?, ?, 128, ?)",
                    (i, len(kp), rng.integers(0, 255, (len(kp), 128), dtype=np.uint8).tobytes()))
    for a in views:
        for b in views:
            if a < b:
                common = sorted(set(views[a]) & set(views[b]))
                m = np.array([[views[a][p], views[b][p]] for p in common], np.uint32)
                inl = m[:14] if (a, b) == weak_pair else m[::2] if (a + b) % 2 else m
                pid = a * dbtools.MAX_IMAGE_ID + b
                con.execute("INSERT INTO matches VALUES (?, ?, 2, ?)", (pid, len(m), m.tobytes()))
                con.execute("INSERT INTO two_view_geometries VALUES (?, ?, 2, ?, 3, ?, ?, ?, ?, ?)",
                            (pid, len(inl), inl.tobytes(), np.eye(3).tobytes(), np.eye(3).tobytes(), np.eye(3).tobytes(),
                             np.array([1.0, 0, 0, 0]).tobytes(), np.zeros(3).tobytes()))
    con.commit()
    con.close()


def matched_points(db: Path) -> set:
    """Every verified match as where its two keypoints are, which a change of indices must leave as it was."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    kps, out = dbtools.keypoints(con), set()
    for pid, blob in con.execute("SELECT pair_id, data FROM two_view_geometries WHERE rows > 0"):
        a, b = dbtools.pair(pid)
        for i, j in dbtools.indices(blob):
            out.add((a, b, *np.round(kps[a][i, :2], 2), *np.round(kps[b][j, :2], 2)))
    con.close()
    return out


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MadeUp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "database.db"
        make(self.src)
        self.before = digest(self.src)

    def tearDown(self):
        self.assertEqual(digest(self.src), self.before, "the database read was changed")
        self.tmp.cleanup()

    def test_the_made_up_database_is_sound(self):
        self.assertEqual(dbtools.check(self.src), [])
        self.assertEqual(dbtools.pair(3 * dbtools.MAX_IMAGE_ID + 5), (3, 5))

    def test_check_finds_a_bad_index_and_a_missing_camera(self):
        bad = self.dir / "bad.db"
        con = dbtools._open(self.src, bad)
        con.execute("UPDATE two_view_geometries SET data = ? WHERE pair_id = ?",
                    (np.array([[99999, 0]], np.uint32).tobytes() * 1, 1 * dbtools.MAX_IMAGE_ID + 2))
        con.execute("UPDATE two_view_geometries SET rows = 1 WHERE pair_id = ?", (1 * dbtools.MAX_IMAGE_ID + 2,))
        con.execute("DELETE FROM cameras WHERE camera_id = 1")
        con.commit()
        con.close()
        problems = " ".join(dbtools.check(bad))
        self.assertIn("index past", problems)
        self.assertIn("camera 1 isn't there", problems)

    def test_thin_keeps_the_most_matched_keypoints_and_every_index_valid(self):
        out = self.dir / "thin.db"
        r = dbtools.thin(self.src, out, 120)
        self.assertEqual(dbtools.check(out), [])
        con = sqlite3.connect(out)
        rows = [n for n, in con.execute("SELECT rows FROM keypoints")]
        self.assertTrue(all(n <= 120 for n in rows) and max(rows) == 120, rows)
        self.assertEqual(rows, [n for n, in con.execute("SELECT rows FROM descriptors")])
        self.assertEqual(r["before"]["images"], r["after"]["images"])
        self.assertEqual(r["before"]["cameras"], r["after"]["cameras"])
        self.assertLess(r["after"]["verified_matches"], r["before"]["verified_matches"])
        # what is left of each pair's matches joins the same two places as before
        self.assertTrue(matched_points(out) <= matched_points(self.src))
        self.assertGreater(len(matched_points(out)), 0)
        # no keypoint is kept that no verified match uses, while matched ones were to be had
        used = {}
        for pid, blob in con.execute("SELECT pair_id, data FROM two_view_geometries WHERE rows > 0"):
            a, b = dbtools.pair(pid)
            m = dbtools.indices(blob)
            used.setdefault(a, set()).update(m[:, 0].tolist())
            used.setdefault(b, set()).update(m[:, 1].tolist())
        self.assertTrue(all(len(u) > 60 for u in used.values()), {k: len(v) for k, v in used.items()})
        con.close()

    def test_thin_drops_a_pair_left_with_too_few_verified_matches(self):
        out = self.dir / "thin.db"
        r = dbtools.thin(self.src, out, 60)          # the weak pair's 14 matches can't all be among each photo's 60
        con = sqlite3.connect(out)
        weak = con.execute("SELECT rows, config, length(data) FROM two_view_geometries WHERE pair_id = ?",
                           (5 * dbtools.MAX_IMAGE_ID + 6,)).fetchone()
        self.assertEqual(weak, (0, 0, 0))
        self.assertGreaterEqual(r["verified_pairs_lost"], 1)
        self.assertEqual(r["after"]["verified_pairs"], r["before"]["verified_pairs"] - r["verified_pairs_lost"])
        self.assertTrue(all(n >= dbtools.MIN_INLIERS for n, in con.execute("SELECT rows FROM two_view_geometries WHERE rows > 0")))
        con.close()
        self.assertEqual(dbtools.check(out), [])

    def test_thin_with_room_for_everything_changes_no_match(self):
        out = self.dir / "thin.db"
        r = dbtools.thin(self.src, out, 100000)
        self.assertEqual(r["after"]["verified_matches"], r["before"]["verified_matches"])
        self.assertEqual(matched_points(out), matched_points(self.src))
        self.assertLess(r["after"]["keypoints"], r["before"]["keypoints"])        # the unmatched ones went
        self.assertEqual(dbtools.check(out), [])

    def test_spread_covers_the_image_before_it_fills_one_corner(self):
        rng = np.random.default_rng(0)
        corner = rng.uniform([0, 0], [100, 60], (500, 2))             # many strong keypoints in one cell
        rest = rng.uniform([0, 0], [1200, 720], (300, 2))             # weaker ones all over
        xy, score = np.r_[corner, rest], np.r_[np.full(500, 9), np.full(300, 2)]
        keep = dbtools.spread(xy, score, 200, 1200, 720)
        self.assertEqual(len(keep), 200)
        self.assertTrue((np.diff(keep) > 0).all())
        self.assertGreater((keep >= 500).sum(), 100)                  # most of the 200 are from all over
        top = dbtools.spread(xy, score, 200, 1200, 720, grid=1)       # one cell: just the strongest
        self.assertTrue((top < 500).all())
        self.assertEqual(list(dbtools.spread(xy, np.zeros(800, int), 5, 1200, 720)), [0, 1, 2, 3, 4])
        self.assertEqual(len(dbtools.spread(xy, score, 5000, 1200, 720)), 800)

    def test_lens_gives_a_video_one_camera_and_leaves_the_rest(self):
        out = self.dir / "lens.db"
        r = dbtools.lens(self.src, out)
        self.assertEqual(dbtools.check(out), [])
        con = sqlite3.connect(out)
        cams = dict(con.execute("SELECT name, camera_id FROM images"))
        vid_a = {cams[n] for n in cams if n.startswith("yt_vidA_")}
        self.assertEqual(len(vid_a), 1)
        self.assertEqual(vid_a, {6})                                   # its first frame's
        self.assertEqual(len({cams["yt_vidB_000.jpg"], cams["mountainproject_abc.jpg"]} | vid_a), 3)
        self.assertEqual(con.execute("SELECT count(*) FROM cameras").fetchone()[0], 3)
        self.assertEqual((r["before"]["cameras"], r["after"]["cameras"], r["groups_sharing"], r["frames_sharing"]), (6, 3, 1, 4))
        self.assertEqual(r["before"]["verified_matches"], r["after"]["verified_matches"])
        con.close()

    def test_lens_keeps_apart_a_video_s_frames_of_another_size(self):
        con = sqlite3.connect(self.src)
        con.execute("UPDATE cameras SET width = 640, height = 360 WHERE camera_id = (SELECT camera_id FROM images WHERE name = 'yt_vidA_003.jpg')")
        con.commit()
        con.close()
        self.before = digest(self.src)
        out = self.dir / "lens.db"
        dbtools.lens(self.src, out)
        con = sqlite3.connect(out)
        cams = dict(con.execute("SELECT name, camera_id FROM images"))
        self.assertEqual(len({cams[f"yt_vidA_00{i}.jpg"] for i in range(3)}), 1)
        self.assertNotEqual(cams["yt_vidA_003.jpg"], cams["yt_vidA_000.jpg"])
        self.assertEqual(con.execute("SELECT count(*) FROM cameras").fetchone()[0], 4)
        con.close()

    def test_similarity_share(self):
        rng = np.random.default_rng(0)
        p = rng.uniform(0, 1000, (300, 2))
        turn = np.array([[np.cos(0.2), -np.sin(0.2)], [np.sin(0.2), np.cos(0.2)]])
        q = 1.1 * p @ turn.T + [30, -12]
        self.assertGreater(dbtools.similarity_share(p, q + rng.normal(0, 0.3, p.shape), 3.0), 0.99)
        half = q.copy()
        half[:150] += rng.uniform(40, 200, (150, 2))                    # half the points move with depth
        self.assertAlmostEqual(dbtools.similarity_share(p, half, 3.0), 0.5, delta=0.05)
        self.assertEqual(dbtools.similarity_share(p[:1], q[:1], 3.0), 0.0)

    def test_dedup_drops_the_frame_that_repeats_a_kept_one(self):
        for measure, threshold in (("sim", 0.9), ("move", 0.02)):
            out = self.dir / f"dedup-{measure}.db"
            r = dbtools.dedup(self.src, out, measure, threshold)
            self.assertEqual(list(r["dropped_frames"]), ["yt_vidA_001.jpg"], measure)
            self.assertEqual(r["dropped_frames"]["yt_vidA_001.jpg"]["repeats"], "yt_vidA_000.jpg")
            self.assertEqual((r["frames_in_videos"], r["dropped"]), (5, 1))
            self.assertEqual(dbtools.check(out), [])
            con = sqlite3.connect(out)
            self.assertEqual(con.execute("SELECT count(*) FROM images").fetchone()[0], 5)
            self.assertEqual(con.execute("SELECT count(*) FROM cameras").fetchone()[0], 5)
            self.assertEqual(con.execute("SELECT count(*) FROM keypoints").fetchone()[0], 5)
            for table in ("matches", "two_view_geometries"):
                pairs = [dbtools.pair(p) for p, in con.execute(f"SELECT pair_id FROM {table}")]
                self.assertEqual(len(pairs), 10)
                self.assertFalse(any(2 in p for p in pairs))
            con.close()
            self.assertTrue(matched_points(out) <= matched_points(self.src))

    def test_dedup_at_a_threshold_nothing_meets_drops_nothing(self):
        r = dbtools.dedup(self.src, self.dir / "dedup.db", "move", 0.0)
        self.assertEqual(r["dropped"], 0)
        self.assertEqual(r["before"], r["after"])

    def test_a_changed_database_is_one_file_with_no_journal(self):
        out = self.dir / "thin.db"
        dbtools.thin(self.src, out, 100)
        con = sqlite3.connect(out)
        self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        con.close()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith("thin")), ["thin.db"])


@unittest.skipUnless(REAL.exists(), "hal's benchmark folder isn't here")
class Real(unittest.TestCase):
    """The same changes on a copy of the 33-photo boulder's own database (never the database itself)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/data/tom/mapperbench")
        cls.dir = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_thin(self):
        for n in (1500, 2500):
            out = self.dir / f"thin{n}.db"
            r = dbtools.thin(REAL, out, n)
            self.assertEqual(dbtools.check(out), [])
            con = sqlite3.connect(out)
            self.assertLessEqual(max(k for k, in con.execute("SELECT rows FROM keypoints")), n)
            con.close()
            self.assertEqual(r["after"]["images"], 33)
            self.assertGreater(r["after"]["verified_pairs"], 0.9 * r["before"]["verified_pairs"])

    def test_lens(self):
        out = self.dir / "lens.db"
        r = dbtools.lens(REAL, out)
        self.assertEqual(dbtools.check(out), [])
        self.assertLess(r["after"]["cameras"], r["before"]["cameras"])
        self.assertEqual(r["after"]["verified_matches"], r["before"]["verified_matches"])

    def test_dedup(self):
        out = self.dir / "dedup.db"
        r = dbtools.dedup(REAL, out, "sim", 0.9)
        self.assertEqual(dbtools.check(out), [])
        self.assertEqual(r["after"]["images"], r["before"]["images"] - r["dropped"])
        self.assertLess(r["dropped"], r["frames_in_videos"])


if __name__ == "__main__":
    unittest.main()
