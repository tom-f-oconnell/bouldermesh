"""Tests of variants.py that need no COLMAP, Docker or benchmark folder: python3 test_variants.py (or pytest).
What the new methods would run is checked as text here; that the tools accept it is for the first runs to show.
"""
import importlib.util
import io
import json
import shlex
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench  # noqa: E402
import texstats  # noqa: E402
import variants  # noqa: E402

PROJECTS = {"e6de7747fab5", "31de9a59303a", "9f3b4b740847", "38f31439985f", "ce7571e0a4a3"}
# a 3.9.1 mapper's log: only some lines carry a time
OLD = """I20261113 08:30:00.000000     8 misc.cc:198]
Finding good initial image pair
I20261113 08:30:10.000000     8 incremental_mapper.cc:404] => Image sees 10 / 90 points
Initializing with image pair #3 and #4
Registering image #5 (3)
I20261113 08:30:40.000000     8 incremental_mapper.cc:404] => Image sees 50 / 90 points
Registering image #6 (4)
I20261113 08:31:00.000000     8 incremental_mapper.cc:404] => Image sees 60 / 90 points
Finding good initial image pair
Registering image #9 (3)
I20261113 08:40:00.000000     8 incremental_mapper.cc:404] => Image sees 60 / 90 points
Registering image #10 (120)
I20261113 09:10:00.000000     8 incremental_mapper.cc:404] => Image sees 70 / 90 points
Finding good initial image pair
I20261113 09:20:00.000000     8 incremental_mapper.cc:404] => No good initial image pair found.
I20261113 09:20:00.500000     8 timer.cc:91] Elapsed time: 50.008 [minutes]
"""
# a 4.2.1 one, across midnight: every line has a time
NEW = """I20261113 23:59:00.000000     7 incremental_pipeline.cc:465] Finding good initial image pair
I20261113 23:59:01.000000     7 incremental_pipeline.cc:492] Registering initial image pair #16 and #32
I20261113 23:59:30.000000     7 incremental_pipeline.cc:620] Registering image #17 (num_reg_frames=2)
I20261114 00:00:30.000000     7 incremental_pipeline.cc:620] Registering image #18 (num_reg_frames=40)
I20261114 00:01:00.000000     7 incremental_pipeline.cc:465] Finding good initial image pair
I20261114 00:01:30.000000     7 incremental_pipeline.cc:620] Registering image #3 (num_reg_frames=6)
I20261114 00:02:00.000000     7 timer.cc:90] Elapsed time: 3.000 [minutes]
"""


class Registry(unittest.TestCase):
    def test_every_new_method_is_one_bench_can_name_and_parse(self):
        for name, (family, command, extra, env) in variants.methods(bench.GLOBAL).items():
            self.assertIn(name, bench.METHODS)
            self.assertIn(family, ("app", "new"))
            self.assertIn(command, ("mapper", "global_mapper", "thin", "hier"))
            build = "plain" if family == "app" else "complete"
            for cond in ("dev", "prod", "c6t3"):
                run = name + "-s3" + ("+complete" if family == "new" else "") + (f"@{cond}" if cond != "dev" else "")
                self.assertEqual(bench.run_parts(run), (name, build, cond))
        self.assertTrue(variants.GPU_METHODS <= bench.GPU_METHODS)
        self.assertTrue(all(bench.METHODS[m][0] == "new" for m in variants.GPU_METHODS))

    def test_the_plain_mappers_get_their_options_and_the_app_s_settings(self):
        s = bench.mapper_script("sidecut", 4, "t6", [])
        for part in ("colmap mapper", "--random_seed 4", "--Mapper.min_model_size 6", "--Mapper.num_threads 6",
                     "--Mapper.max_num_models 3", "--Mapper.init_num_trials 50", "--Mapper.max_model_overlap 5"):
            self.assertIn(part, s)
        s = bench.mapper_script("models1_421", 4, "prod", [])
        self.assertIn("--Mapper.random_seed 4", s)
        self.assertIn("--Mapper.max_num_models 1", s)
        s = bench.mapper_script("global_one", 0, "prod", [])
        self.assertIn("colmap global_mapper", s)
        self.assertIn("--GlobalMapper.min_num_matches 20", s)
        self.assertIn("--GlobalMapper.multiple_models 0", s)
        self.assertIn("--GlobalMapper.track_required_tracks_per_view 2500", bench.mapper_script("global_tpv2500", 0, "prod", []))

    def test_derived_feature_sets(self):
        self.assertEqual(variants.derived("sift391_lens"), ("sift391", "lens"))
        self.assertEqual(variants.derived("siftgpu391_thin2500"), ("siftgpu391", "thin2500"))
        for not_one in ("sift391", "aliked421_lens", "sift391_thin99", "sift421"):
            self.assertIsNone(variants.derived(not_one))
        for feat in variants.feats(bench.FEATS):
            self.assertEqual(bench.FEATS[feat][0], "app")              # a 3.9.1 database, changed before any migration
            self.assertIsNotNone(variants.derived(feat))
        self.assertEqual(bench.feat_dir("p", "sift391_lens").name, "sift391_lens")

    def test_a_newer_colmap_s_feature_set_is_looked_for_in_its_build_s_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, bench.R = bench.R, Path(tmp)
            try:
                self.assertEqual(bench.feat_db("p", "sift391", "complete"), Path(tmp, "p", "sift391", "database.db"))
                self.assertEqual(bench.feat_db("p", "aliked421", "complete"), Path(tmp, "p", "aliked421+complete", "database.db"))
                Path(tmp, "p", "aliked421").mkdir(parents=True)
                Path(tmp, "p", "aliked421", "database.db").write_bytes(b"")
                self.assertEqual(bench.feat_db("p", "aliked421", "complete"), Path(tmp, "p", "aliked421", "database.db"))
            finally:
                bench.R = old


class Reports(unittest.TestCase):
    def test_attempts_in_a_391_log(self):
        first, last, models = variants.attempts(OLD)
        self.assertEqual([m["size"] for m in models], [4, 120, 0])
        self.assertAlmostEqual(last - first, 3000.5)
        big = max(models, key=lambda m: m["size"])
        self.assertAlmostEqual(big["start"] - first, 60)            # the small first model
        self.assertAlmostEqual(big["end"] - big["start"], 2340)
        self.assertAlmostEqual(last - big["end"], 600.5)            # looking for another model to start

    def test_attempts_in_a_421_log_across_midnight(self):
        first, last, models = variants.attempts(NEW)
        self.assertEqual([m["size"] for m in models], [40, 6])
        self.assertAlmostEqual(last - first, 180)
        self.assertAlmostEqual(models[0]["end"] - models[0]["start"], 120)

    def test_sidemodels_prints_a_row_a_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp, "boulder", "sift391", "base-s0@prod")
            run.mkdir(parents=True)
            (run / "map.log").write_text(OLD)
            (run / "map.json").write_text(json.dumps({"wall": 3000.6}))
            Path(tmp, "boulder", "sift391", "global-s0").mkdir()
            Path(tmp, "boulder", "sift391", "global-s0", "map.log").write_text("I20261113 08:30:00.000000 7 x.cc:1] nothing\n")
            out, sys.stdout = sys.stdout, io.StringIO()
            try:
                variants.sidemodels(Path(tmp))
                text = sys.stdout.getvalue()
            finally:
                sys.stdout = out
        rows = [l.split("\t") for l in text.strip().split("\n")]
        self.assertEqual(len(rows), 2)                              # the global mapper's log has no models: left out
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual((row["boulder"], row["run"], row["models_started"], row["largest"], row["second"]),
                         ("boulder", "base-s0@prod", "3", "120", "4"))
        self.assertEqual((row["before_s"], row["largest_s"], row["after_s"], row["side_share"]), ("60.0", "2340.0", "600.5", "0.22"))

    def test_consensus_shows_the_model_far_from_all_others(self):
        ag = lambda deg, pos: {"deg_median": deg, "pos_median": pos}      # noqa: E731
        pairs = [("a", "b", ag(0.2, 0.01)), ("a", "c", ag(0.3, 0.01)), ("b", "c", ag(0.1, 0.02)),
                 ("a", "odd", ag(18.0, 0.4)), ("b", "odd", ag(20.0, 0.5)), ("c", "odd", ag(33.0, 0.6)), ("a", "tiny", {})]
        con = variants.consensus(pairs)
        self.assertEqual(con["odd"], {"others": 3, "deg": 20.0, "pos": 0.5, "deg_best": 18.0})
        self.assertEqual(con["a"]["deg"], 0.3)
        self.assertEqual(con["b"]["deg"], 0.2)
        self.assertNotIn("tiny", con)

    def test_agreement_s_distribution(self):
        import numpy as np
        rng = np.random.default_rng(0)

        def turn(axis, deg):
            a = np.radians(deg)
            k = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]], float)
            return np.eye(3) + np.sin(a) * k + (1 - np.cos(a)) * k @ k
        a, b = {}, {}
        for i in range(40):
            R, C = turn([0, 0, 1], 9 * i), rng.uniform(-5, 5, 3)
            a[f"im{i}"] = (R, -R @ C)
            off = 0.0 if i < 30 else 3.0 if i < 36 else 8.0            # 30 exact, 6 turned 3 degrees, 4 turned 8
            R2 = turn([1, 0, 0], off) @ R
            b[f"im{i}"] = (R2, -R2 @ C)
        ag = bench.stats.agreement(a, b)
        self.assertEqual(ag["cameras"], 40)
        self.assertAlmostEqual(ag["deg_le1"], 0.75, delta=0.03)
        self.assertAlmostEqual(ag["deg_le5"], 0.9, delta=0.03)
        self.assertGreaterEqual(ag["deg_le2"], ag["deg_le1"])
        self.assertGreater(ag["pos_le10pct"], 0.7)
        self.assertLessEqual(ag["pos_le2pct"], ag["pos_le10pct"])


class Pieces(unittest.TestCase):
    def test_poses_keeps_the_cameras_and_drops_the_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp, "txt"), Path(tmp, "poses")
            src.mkdir()
            (src / "cameras.txt").write_text("# cameras\n1 SIMPLE_RADIAL 100 80 90 50 40 0.01\n")
            (src / "rigs.txt").write_text("# a 4.2.1 model has these\n")
            (src / "images.txt").write_text("# images\n# two lines each\n1 1 0 0 0 0.5 0 2 1 a.jpg\n10.5 20.5 7 30 40 -1\n"
                                            "2 1 0 0 0 1.5 0 2 1 b.jpg\n\n3 1 0 0 0 2.5 0 2 1 c.jpg\n1 2 3\n")
            (src / "points3D.txt").write_text("# points\n7 0 0 5 128 128 128 0.5 1 0\n")
            variants.poses(src, dst)
            self.assertEqual(sorted(p.name for p in dst.iterdir()), ["cameras.txt", "images.txt", "points3D.txt", "rigs.txt"])
            self.assertEqual((dst / "cameras.txt").read_text(), (src / "cameras.txt").read_text())
            body = [l for l in (dst / "images.txt").read_text().split("\n") if not l.startswith("#")]
            self.assertEqual(body, ["1 1 0 0 0 0.5 0 2 1 a.jpg", "", "2 1 0 0 0 1.5 0 2 1 b.jpg", "", "3 1 0 0 0 2.5 0 2 1 c.jpg", "", ""])
            self.assertEqual([l for l in (dst / "points3D.txt").read_text().split("\n") if l and not l.startswith("#")], [])
            self.assertEqual(sorted(bench.stats.poses(dst)), ["a.jpg", "b.jpg", "c.jpg"])

    def test_a_job_s_condition_is_replaced_for_a_batch(self):
        words, P, feat, build = variants.job_parts("all ce7571e0a4a3 global 1 --cond prod --build complete --post complete", "c4")
        self.assertEqual(words, ["all", "ce7571e0a4a3", "global", "1", "--build", "complete", "--post", "complete", "--cond", "c4"])
        self.assertEqual((P, feat, build), ("ce7571e0a4a3", "sift391", "complete"))
        self.assertEqual(variants.job_parts("all x base 0 --feat sift391_lens", "c2")[2:], ("sift391_lens", "plain"))
        self.assertEqual(bench.condition("c4"), (4, 4, True))

    def test_a_job_s_result_is_read_from_its_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "p" / "sift391" / "global-s1+complete@c4"
            for sub, step, wall, rc, wait in (("", "map", 100, 0, 30), ("rest", "rest", 10, 0, 0), ("post-complete@c4", "dense", 50, 0, 5),
                                              ("post-complete@c4", "texture", 70, 0, 0)):
                (run / sub).mkdir(parents=True, exist_ok=True)
                (run / sub / f"{step}.json").write_text(json.dumps({"step": step, "wall": wall, "rc": rc, "lock_wait": wait}))
            (run / "models.json").write_text("{}")
            other = root / "p" / "sift391" / "global-s0+complete@c4"
            other.mkdir()
            (other / "map.json").write_text(json.dumps({"step": "map", "wall": 999, "rc": 1}))
            r = variants.job_result(root, shlex.split("all p global 1 --build complete --post complete --cond c4"))
            self.assertEqual(r, {"steps": {"map": 100, "rest": 10, "dense": 50, "texture": 70}, "steps_s": 230, "lock_wait_s": 35,
                                 "failed": [], "mesh": True})
            (run / "post-complete@c4" / "texture.json").write_text(json.dumps({"step": "texture", "wall": 3, "rc": 1}))
            r = variants.job_result(root, shlex.split("all p global 1 --build complete --post complete --cond c4"))
            self.assertEqual((r["mesh"], r["failed"]), (False, ["texture"]))
            r = variants.job_result(root, shlex.split("all p global 0 --build complete --cond c4"))
            self.assertEqual((r["mesh"], r["failed"]), (False, ["map"]))

    def test_together_runs_a_batch_in_its_own_folder_and_counts_meshes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            saved = (bench.BENCH, bench.R, bench.CAP_FILE, variants.subprocess.run)
            bench.BENCH, bench.R, bench.CAP_FILE = tmp, tmp / "r2", tmp / "cores-cap.txt"
            bench.CAP_FILE.write_text("8\n")
            for P in ("a", "b"):
                (bench.R / P / "sift391").mkdir(parents=True)
                (bench.R / P / "sift391" / "database.db").write_bytes(b"db")
                (bench.R / P / "sift391" / "match.json").write_text("{}")
            jobs = tmp / "jobs-x.txt"
            jobs.write_text("# two jobs\nall a base 0 --post app\nall b base 0 --post app --cond prod\nall a base 1\n")
            ran = []

            def fake(cmd, stdout=None, stderr=None, env=None):
                words = cmd[2:]
                ran.append((words, env["BENCH_R"], env["MEM"]))
                run = Path(env["BENCH_R"], words[1], "sift391", f"base-s{words[3]}@c4")
                self.assertTrue((run.parent / "database.db").is_symlink())
                for sub, step, rc in (("", "map", 0), ("post-app@c4", "texture", 1 if words[1] == "b" else 0)):
                    (run / sub).mkdir(parents=True, exist_ok=True)
                    if sub == "" or "--post" in words:
                        (run / sub / f"{step}.json").write_text(json.dumps({"step": step, "wall": 5.0, "rc": rc}))
                return type("done", (), {"returncode": 0})()
            variants.subprocess.run = fake
            out, sys.stdout = sys.stdout, io.StringIO()
            try:
                variants.together(2, jobs)
                root = tmp / "together" / "jobs-x-n2"
                saved_batch = json.loads((root / "together.json").read_text())
                variants.together(2, jobs)                      # made already: printed again, nothing run
                text = sys.stdout.getvalue()
            finally:
                sys.stdout = out
                bench.BENCH, bench.R, bench.CAP_FILE, variants.subprocess.run = saved
            self.assertEqual(len(ran), 3)
            self.assertTrue(all(w[-2:] == ["--cond", "c4"] and w.count("--cond") == 1 for w, _, _ in ran))      # 8 cores, 2 at a time
            self.assertTrue(all(r == str(root) for _, r, _ in ran))
            self.assertEqual((saved_batch["at_a_time"], saved_batch["cores_each"], saved_batch["meshes"]), (2, 4, 2))
            self.assertEqual([j["mesh"] for j in saved_batch["jobs"]], [True, False, True])      # b's texture failed
            self.assertEqual(saved_batch["jobs"][1]["failed"], ["texture"])
            self.assertIn("meshes_per_hour", text)
            self.assertEqual(text.count("jobs-x-n2\t2\t4\t3\t2"), 2)

    def test_texture_variants_change_only_what_they_name(self):
        for name, (args, only_new) in variants.TEXTURE.items():
            self.assertEqual(len(args) % 2, 0, name)
            self.assertEqual(only_new, "--views-file" in args, name)
            for flag in args[0::2]:      # each in 2.3's --help and 2.4's source, but the views file (2.4's)
                self.assertIn(flag, ("--resolution-level", "--decimate", "--global-seam-leveling", "--local-seam-leveling",
                                     "--max-texture-size", "--views-file"), name)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "no PIL here (the app's image has it)")
    def test_texstats_reads_a_glb_s_textures(self):
        import numpy as np
        from PIL import Image
        rng = np.random.default_rng(0)

        def glb(folder: Path, sharp: bool) -> None:
            img = np.full((64, 64, 3), 128, np.uint8)                       # half empty
            patch = rng.integers(0, 255, (64, 32, 3), dtype=np.uint8) if sharp else np.tile(np.linspace(20, 90, 32, dtype=np.uint8)[None, :, None], (64, 1, 3))
            img[:, :32] = patch
            png = io.BytesIO()
            Image.fromarray(img).save(png, "PNG")
            binary = png.getvalue() + b"\0" * (-len(png.getvalue()) % 4)
            doc = json.dumps({"images": [{"bufferView": 0, "mimeType": "image/png"}],
                              "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(png.getvalue())}],
                              "accessors": [{"count": 300}], "meshes": [{"primitives": [{"indices": 0}]}]}).encode()
            doc += b" " * (-len(doc) % 4)
            folder.mkdir()
            (folder / "m.glb").write_bytes(b"glTF" + struct.pack("<II", 2, 28 + len(doc) + len(binary))
                                           + struct.pack("<I4s", len(doc), b"JSON") + doc + struct.pack("<I4s", len(binary), b"BIN\0") + binary)
        with tempfile.TemporaryDirectory() as tmp:
            glb(Path(tmp, "sharp"), True)
            glb(Path(tmp, "smooth"), False)
            a, b = texstats.stats(Path(tmp, "sharp")), texstats.stats(Path(tmp, "smooth"))
        self.assertEqual((a["textures"], a["faces"], a["mpx"]), (1, 100, 0.0))
        self.assertAlmostEqual(a["used_mpx"] * 1e6, 64 * 32, delta=6000)       # rounded to 0.01 megapixels
        self.assertGreater(a["sharpness"], 100 * max(b["sharpness"], 0.01))


class Queues(unittest.TestCase):
    """Every line of the new queue and jobs files is a job bench.py can read, of a method, feature set, boulder,
    condition and variant that exist."""

    FILES = sorted(p for p in HERE.glob("*.txt") if p.name.startswith(("queue-new", "queue-side", "queue-thin", "queue-lens", "queue-dedup",
                                                                        "queue-threads", "queue-hier", "queue-together", "queue-texture",
                                                                        "queue-gpufeat", "jobs-")))

    def test_the_files_are_there(self):
        self.assertGreaterEqual(len(self.FILES), 16, [p.name for p in self.FILES])

    def test_every_line(self):
        for f in self.FILES:
            for line in f.read_text().split("\n"):
                if not line.strip() or line.startswith("#"):
                    continue
                w, where = shlex.split(line), f"{f.name}: {line}"
                get = lambda flag, default: w[w.index(flag) + 1] if flag in w else default      # noqa: E731
                feat, cond, build, tools = get("--feat", "sift391"), get("--cond", "dev"), get("--build", "plain"), get("--tools", "app")
                self.assertIn(feat, bench.FEATS, where)
                bench.condition(cond)
                self.assertIn(build, ("plain", "complete", "v3", "native"), where)
                if w[0] == "together":
                    self.assertTrue(w[1].isdigit() and (HERE / w[2]).exists(), where)
                    continue
                self.assertIn(w[1], PROJECTS, where)
                if w[0] == "all":
                    family = bench.METHODS[w[2]][0]
                    self.assertTrue(w[3].isdigit(), where)
                    self.assertEqual(family == "new", "--build" in w, where)           # 4.2.1 methods name their build
                    self.assertTrue(family == "new" or bench.FEATS[feat][0] == "app", where)
                    if f.name.startswith("jobs-"):
                        self.assertNotIn("--cond", w, where)                           # together sets it
                    for t in filter(None, get("--post", "").split(",")):
                        self.assertIn(t, ("app", "complete"), where)
                elif w[0] == "texture":
                    self.assertIn(w[3], variants.TEXTURE, where)
                    self.assertFalse(variants.TEXTURE[w[3]][1] and tools == "app", where)
                    self.assertIn(bench.run_parts(w[2])[0], bench.METHODS, where)
                elif w[0] in ("post", "people"):
                    runs = [x for x in w[2:3] if x not in ("cpu", "gpu") and not x.startswith("--")]
                    for run in runs:
                        self.assertIn(bench.run_parts(run)[0], bench.METHODS, where)
                else:
                    self.assertEqual(w[0], "feat", where)
                self.assertIn(w[0], ["feat", "map", "rest", "post", "all", "held", *variants.COMMANDS], where)


if __name__ == "__main__":
    unittest.main()
