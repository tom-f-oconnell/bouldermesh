"""Offline tests of the relevance filter and job progress, on synthetic photos.  Run: python3 tests/test_relevance.py

The same-rock and CLIP checks need OpenCV and open_clip; without them only their absence is tested.
"""
import json, pathlib, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
import numpy as np
from PIL import Image, ImageDraw

from climbs import relevance
from progress import Tracker

relevance.CACHE_FILE = pathlib.Path(tempfile.mkdtemp(prefix="bouldermesh-relevance-")) / "relevance.sqlite"   # not the real one



def make_rock(seed: int) -> np.ndarray:
    """A big "rock": detail at several scales, so that SIFT has plenty to find at any zoom."""
    rng = np.random.default_rng(seed)
    r = sum(np.asarray(Image.fromarray((rng.random((1600 // s, 2000 // s)) * 255).astype(np.uint8)).resize((2000, 1600), Image.BICUBIC), dtype=float)
            for s in (4, 16, 64))
    return (255 * (r - r.min()) / np.ptp(r)).astype(np.uint8)


rock = make_rock(3)


def view_of(tmp: pathlib.Path, name: str, x: int, y: int, base=rock, w: int = 1200, h: int = 900) -> pathlib.Path:
    p = tmp / name
    Image.fromarray(base[y:y + h, x:x + w]).save(p, quality=92)
    return p


def title_card(tmp: pathlib.Path, name: str) -> pathlib.Path:
    im = Image.new("RGB", (1280, 720))
    d = ImageDraw.Draw(im)
    for i, t in enumerate(("PAWEL ZIOLO", "on", "MIDNIGHT LIGHTNING")):
        d.text((520, 300 + 40 * i), t, fill=(255, 255, 255))
    im.save(tmp / name)
    return tmp / name


with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    photos = tmp / "photos"
    photos.mkdir()
    # black bars aren't flat content: a real photo squeezed into a wide frame must survive
    bars = np.zeros((720, 1280), dtype=np.uint8)
    bars[:, 500:780] = np.asarray(Image.fromarray(rock[:720, :280]))
    Image.fromarray(bars).save(photos / "yt_bars.jpg", quality=92)
    assert relevance.flat_share(photos / "yt_bars.jpg") < 0.1, relevance.flat_share(photos / "yt_bars.jpg")
    card = title_card(photos, "yt_card.jpg")
    assert relevance.flat_share(card) > relevance.FLAT_MAX, relevance.flat_share(card)
    a1, a2 = view_of(photos, "mountainproject_a.jpg", 100, 100), view_of(photos, "mountainproject_b.jpg", 400, 300)
    same = view_of(photos, "ddg_same.jpg", 700, 500)
    other = view_of(photos, "commons_other.jpg", 300, 200, make_rock(4))
    paths = sorted(p for p in photos.iterdir())

    assert relevance.is_trusted(a1) and not relevance.is_trusted(same) and not relevance.is_trusted(other)
    logs = []
    r = relevance.filter_photos(paths, None, logs.append, methods=["cards"])
    assert r["rejected"] == {"yt_card.jpg": "card"} and r["counts"]["card"] == 1, r
    assert all(p.exists() for p in paths), "report-only mode must not move anything"

    if "same-rock" in relevance.available():
        k, cov = relevance.match(relevance._features(same), relevance._features(a2))
        assert k >= relevance.MIN_INLIERS and cov > relevance.MIN_COVERAGE, (k, cov)
        k, _ = relevance.match(relevance._features(other), relevance._features(a2))
        assert k < relevance.MIN_INLIERS, k

        steps = []
        r = relevance.filter_photos(paths, tmp / "rejected", logs.append, methods=["cards", "same-rock"],
                                    progress=lambda f=0.0, d=None: steps.append(f))
        assert sorted(r["rejected"]) == ["commons_other.jpg", "yt_card.jpg"], r["rejected"]
        assert r["rejected"]["commons_other.jpg"] == "nomatch", r
        assert sorted(r["kept"]) == ["ddg_same.jpg", "mountainproject_a.jpg", "mountainproject_b.jpg", "yt_bars.jpg"], r
        assert (tmp / "rejected" / "nomatch_commons_other.jpg").exists() and not other.exists()
        assert steps and all(0 <= f <= 1 for f in steps), steps
        (tmp / "rejected" / "nomatch_commons_other.jpg").rename(other)
        (tmp / "rejected" / "card_yt_card.jpg").rename(card)

        # a photo that only overlaps another web photo, not the anchor, is found through it
        near = view_of(photos, "ddg_near.jpg", 300, 0, w=600, h=600)
        far = view_of(photos, "ddg_far.jpg", 700, 0, w=600, h=600)
        anchor = view_of(photos, "mountainproject_c.jpg", 0, 0, w=600, h=600)
        k, _ = relevance.match(relevance._features(far), relevance._features(anchor))
        assert k < relevance.MIN_INLIERS, k
        r = relevance.filter_photos([anchor, near, far], None, logs.append, methods=["same-rock"])
        assert not r["rejected"] and r["scores"]["ddg_far.jpg"]["match"] == "ddg_near.jpg", r
        rounds, relevance.CHAIN_ROUNDS = relevance.CHAIN_ROUNDS, 1
        r = relevance.filter_photos([anchor, near, far], None, logs.append, methods=["same-rock"])
        relevance.CHAIN_ROUNDS = rounds
        assert list(r["rejected"]) == ["ddg_far.jpg"], r["rejected"]
        for p in (near, far, anchor):
            p.unlink()

        # another problem's page photo of the same rock, from another harvest, anchors web photos without being kept
        elsewhere = tmp / "elsewhere"
        elsewhere.mkdir()
        theirs = view_of(elsewhere, "mountainproject_z.jpg", 650, 450)
        web_only = [view_of(photos, "ddg_w1.jpg", 700, 500), other]
        r = relevance.filter_photos(web_only, None, logs.append, methods=["same-rock"], more_anchors=[theirs])
        assert r["rejected"] == {"commons_other.jpg": "nomatch"} and r["kept"] == ["ddg_w1.jpg"], r
        assert r["scores"]["ddg_w1.jpg"]["match"] == "mountainproject_z.jpg", r["scores"]
        web_only[0].unlink()

        # no trusted photos at all: the group that matches each other stands in for the anchors
        web = [view_of(photos, f"web_{i}.jpg", x, y) for i, (x, y) in enumerate(((100, 100), (250, 200), (400, 300), (550, 400)))]
        r = relevance.filter_photos(web + [other], None, logs.append, methods=["same-rock"])
        assert r["rejected"] == {"commons_other.jpg": "nomatch"}, r["rejected"]
        # and nothing to go on: nothing is rejected
        lone = [other, view_of(photos, "web_lone.jpg", 300, 200, make_rock(5))]
        r = relevance.filter_photos(lone, None, logs.append, methods=["same-rock"])
        assert not r["rejected"], r["rejected"]
        print("same-rock OK")

        # again, as a harvest again does: every pair known from before, by the photos' contents, and the same verdicts
        logs.clear()
        first = relevance.filter_photos(paths, None, logs.append, methods=["cards", "same-rock"])
        logs.clear()
        again = relevance.filter_photos(paths, None, logs.append, methods=["cards", "same-rock"])
        assert again == first and any("matched 0 pair(s) of photos (brute), the rest known from before" in x for x in logs), logs
        copied = tmp / "copied"
        copied.mkdir()
        for q in paths:
            (copied / q.name).write_bytes(q.read_bytes())
        logs.clear()
        moved = relevance.filter_photos(sorted(copied.iterdir()), None, logs.append, methods=["cards", "same-rock"])
        assert moved["rejected"] == first["rejected"] and any("matched 0 pair(s)" in x for x in logs), "the same photos elsewhere"
        # FLANN's approximate matching: the same verdicts, its own record of pairs
        relevance.MATCHER = "flann"
        try:
            k, cov = relevance.match(relevance._features(same), relevance._features(a2))
            assert k >= relevance.MIN_INLIERS and cov > relevance.MIN_COVERAGE, (k, cov)
            assert relevance.match(relevance._features(other), relevance._features(a2))[0] < relevance.MIN_INLIERS
            logs.clear()
            fl = relevance.filter_photos(paths, None, logs.append, methods=["cards", "same-rock"])
            assert fl["rejected"] == first["rejected"] and fl["kept"] == first["kept"], fl
            assert any("(flann)" in x and "matched 0" not in x for x in logs), "worked out afresh, not brute force's"
        finally:
            relevance.MATCHER = "brute"
        # without a cache, as before
        saved_cache, relevance.CACHE_FILE = relevance.CACHE_FILE, None
        assert relevance.filter_photos(paths, None, logs.append, methods=["cards", "same-rock"]) == first
        relevance.CACHE_FILE = saved_cache
        print("matches kept, and FLANN OK")

        # an earlier run's verdicts stand (a boulder pooled again): what it kept and its title cards aren't checked again,
        # and a photo it found matching nothing is matched only against what's new since; not after it kept everything for
        # want of any match, nor with other settings
        assert relevance.reusable(first) and first["settings"] == relevance.settings_key() and first["fallback"] is False
        assert not relevance.reusable({**first, "fallback": True}) and not relevance.reusable({**first, "settings": "older"})
        assert relevance.reusable({k: v for k, v in first.items() if k != "settings"}), "from before settings were kept, with rejections"
        assert not relevance.reusable({"kept": ["a.jpg"], "rejected": {}}), "rejected none: maybe the fallback"
        calls = []
        real_match = relevance.match
        relevance.match = lambda fa, fb: (calls.append(1), real_match(fa, fb))[1]
        saved_cache, relevance.CACHE_FILE = relevance.CACHE_FILE, None   # every pair worked out, to count them
        try:
            logs.clear()
            same_again = relevance.filter_photos(paths, None, logs.append, methods=["cards", "same-rock"], prior=first)
            assert same_again["rejected"] == first["rejected"] and same_again["kept"] == first["kept"], same_again
            assert same_again["reused"] == len(first["kept"]) + 1 and calls == [], (same_again["reused"], len(calls))
            assert same_again["scores"] == first["scores"], "as they were"
            assert "as an earlier run found them" in logs[0], logs[0]
            # a new page photo of the other rock: the photo that matched nothing is kept, matched only against it
            newer = view_of(photos, "mountainproject_d.jpg", 500, 400, make_rock(4))
            calls.clear()
            r = relevance.filter_photos(paths + [newer], None, logs.append, methods=["cards", "same-rock"], prior=first)
            assert "commons_other.jpg" in r["kept"] and r["scores"]["commons_other.jpg"]["match"] == "mountainproject_d.jpg", r
            assert len(calls) == 1, ("only against the new anchor", len(calls))
            newer.unlink()
            # every photo would be rejected: kept, and that run isn't one to go by
            r = relevance.filter_photos([card], None, logs.append, methods=["cards"])
            assert r["fallback"] and not relevance.reusable(r), r
        finally:
            relevance.match, relevance.CACHE_FILE = real_match, saved_cache
        print("an earlier run's verdicts OK")

        # what the cache knows of a set of photos, for a machine to filter them with, and what it worked out, kept here
        export = tmp / "export.sqlite"
        n = relevance.export_cache(paths, export)
        assert n > 0, "pairs among them known"
        import sqlite3
        with sqlite3.connect(export) as con:
            assert con.execute("SELECT COUNT(*) FROM pairs").fetchone()[0] == n
            con.execute("INSERT INTO pairs VALUES('x1','x2',?,99,0.5)", (relevance._how(),))
        assert relevance.merge_cache(export) >= n + 1
        assert relevance.cache().pairs(relevance._how(), ["x1"]) == {("x1", "x2"): (99, 0.5)}
        print("cache export and merge OK")

        # a machine's side (remote_job.py --task filter): the same report as here, nothing moved, and its cache back
        import shutil
        import remote_job
        job = tmp / "job"
        (job / "photos").mkdir(parents=True)
        for q in paths:
            shutil.copy(q, job / "photos" / q.name)
        relevance.export_cache(paths, job / "cache.sqlite")
        (job / "task.json").write_text(json.dumps({"methods": ["cards", "same-rock"], "more": [], "prior": None, "matcher": "brute"}))
        saved_file, out = relevance.CACHE_FILE, []
        try:
            assert remote_job.filter_task(job, out.append) == 0
        finally:
            relevance.CACHE_FILE = saved_file
        there = json.loads((job / "out" / "relevance.json").read_text())
        assert there["rejected"] == first["rejected"] and there["kept"] == first["kept"], there
        assert all((job / "photos" / q.name).exists() for q in paths) and (job / "out" / "cache.sqlite").is_file()
        assert any("matched 0 pair(s)" in x for x in out), ("the pairs known here went with it", out)
        print("filtering on a machine OK")
    else:
        assert "same-rock" not in relevance.filter_photos(paths, None, logs.append)["methods"]
        print("same-rock skipped: no OpenCV")

    # never empties a whole project
    r = relevance.filter_photos([card], None, logs.append, methods=["cards"])
    assert r["kept"] == ["yt_card.jpg"] and not r["rejected"], r
    print("cards OK")

    if "boulder" in relevance.available():
        try:
            probs = relevance.boulder_probability([a1, card], logs.append)
            assert probs[card] < relevance.BOULDER_MIN_P, probs
            loaded, relevance._load_clip = relevance._load_clip, None    # kept: the model isn't needed again
            try:
                assert relevance.boulder_probability([card, a1], logs.append) == {card: probs[card], a1: probs[a1]}
            finally:
                relevance._load_clip = loaded
            print("boulder OK")
        except OSError as e:   # the weights couldn't be downloaded
            print("boulder skipped:", e)

saved = []
t = Tracker([("a", "A", 1), ("b", "B", 3), ("c", "C", 1)], saved.append)
t.update("a", 0.5, "half")
assert t.snapshot()["overall"] == 10 and t.snapshot()["steps"][0]["state"] == "running"
t.update("a", 0.2)
assert t.snapshot()["steps"][0]["frac"] == 0.5, "progress must never go backwards"
t.update("b", 0.5)   # ends the step before it
s = t.snapshot()
assert s["steps"][0]["state"] == "done" and s["steps"][0]["frac"] == 1 and s["overall"] == 50, s
t.skip_rest()
s = t.snapshot()
assert s["steps"][2]["state"] == "skipped" and s["overall"] == 100, s
assert json.loads(saved[-1]) == s
t = Tracker([("a", "A", 1), ("b", "B", 1)])
t.update("a", 0.5)
t.finish("failed")
assert [x["state"] for x in t.snapshot()["steps"]] == ["failed", "skipped"], t.snapshot()
t = Tracker([("a", "A", 1)])
t.update("unknown", 0.5)   # jobs report steps that a given install may not have
assert t.snapshot()["overall"] == 0
# a step alongside the others: it neither ends those before it nor is ended by those after, only by end()
t = Tracker([("a", "A", 1), ("b", "B", 1), ("v", "V", 1, True), ("c", "C", 1)])
t.update("v", 0.1, "searching")
t.update("a", 0.5)
t.update("b", 0.5)
state = lambda: {x["key"]: x["state"] for x in t.snapshot()["steps"]}  # noqa: E731
assert state() == {"a": "done", "b": "running", "v": "running", "c": "pending"}, state()
t.end("v", detail="2 frames")
t.update("c", 0.5)
assert state() == {"a": "done", "b": "done", "v": "done", "c": "running"}, state()
print("progress OK")

# --- pooling other harvests of the same boulder ---------------------------------------------------
from climbs import harvest  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)

    def project(name, kept, rejected, files):
        d = td / name
        (d / "photos").mkdir(parents=True)
        (d / "rejected").mkdir()
        for f in kept:
            (d / "photos" / f).write_bytes(f.encode())
        for f in rejected:
            (d / "rejected" / f).write_bytes(f.encode())
        (d / "sources.json").write_text(json.dumps({"files": [{"file": f, "url": f"http://x/{f}", "license": "CC BY"} for f in files]}))
        return d
    a = project("a", ["mountainproject_1.jpg", "ddg_2.jpg"], ["nomatch_ddg_3.jpg", "notboulder_ddg_4.jpg", "card_yt_x_5.jpg", "dup_ddg_6.jpg"],
                ["mountainproject_1.jpg", "ddg_2.jpg", "ddg_3.jpg"])
    b = project("b", ["ddg_2.jpg", "yt_ab_7.jpg"], ["nomatch_ddg_8.jpg"], ["yt_ab_7.jpg"])
    here = project("here", ["ddg_9.jpg"], [], [])
    logged = []
    added = harvest.pool(here, [a, b], logged.append)
    got = sorted(x.name for x in (here / "photos").iterdir())
    assert got == ["ddg_2.jpg", "ddg_3.jpg", "ddg_8.jpg", "ddg_9.jpg", "mountainproject_1.jpg", "yt_ab_7.jpg"], got
    assert (here / "photos" / "mountainproject_1.jpg").stat().st_ino == (a / "photos" / "mountainproject_1.jpg").stat().st_ino, "linked, not copied"
    by = {x["file"]: x for x in added}
    assert by["mountainproject_1.jpg"]["url"] == "http://x/mountainproject_1.jpg" and by["mountainproject_1.jpg"]["pooled_from"] == "a"
    assert by["yt_ab_7.jpg"]["pooled_from"] == "b" and by["ddg_8.jpg"] == {"file": "ddg_8.jpg", "pooled_from": "b"}, "kept, then unmatched; no junk"
    assert "pooled 5 photo(s) from 2 other harvest(s)" in logged[0], logged
    assert by["ddg_2.jpg"]["pooled_from"] == "a" and by["ddg_2.jpg"]["also_in"] == ["b"] and "also_in" not in by["ddg_3.jpg"], by
    few = harvest.pool(project("few", [], [], []), [a, b], logged.append, limit=2)
    assert [x["file"] for x in few] == ["mountainproject_1.jpg", "yt_ab_7.jpg"], "each one's own first, taking turns"
    assert [p.name for p in harvest.anchors_of([a, b])] == ["mountainproject_1.jpg", "yt_ab_7.jpg"]

# --- splitting what didn't match into what looks like bouldering and what doesn't ------------------
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    (d / "rejected").mkdir()
    for f in ("nomatch_ddg_a.jpg", "nomatch_ddg_b.jpg", "card_ddg_c.jpg"):
        (d / "rejected" / f).write_bytes(b"x")
    (d / "relevance.json").write_text(json.dumps({"methods": ["cards", "same-rock"], "kept": [], "scores": {},
                                                  "rejected": {"ddg_a.jpg": "nomatch", "ddg_b.jpg": "nomatch", "ddg_c.jpg": "card"}}))
    real = relevance.boulder_probability, relevance.available
    relevance.boulder_probability = lambda paths, log, progress=None: {p: (0.9 if p.name.endswith("a.jpg") else 0.1) for p in paths}
    relevance.available = lambda: ["cards", "boulder", "same-rock"]
    try:
        assert relevance.split_rejected(d, print) == {"nomatch": 1, "notboulder": 1}
        assert sorted(x.name for x in (d / "rejected").iterdir()) == ["card_ddg_c.jpg", "nomatch_ddg_a.jpg", "notboulder_ddg_b.jpg"]
        r = json.loads((d / "relevance.json").read_text())
        assert r["rejected"]["ddg_b.jpg"] == "notboulder" and r["scores"]["ddg_a.jpg"]["boulder"] == 0.9 and "boulder" in r["methods"], r
        assert r["counts"] == {"card": 1, "notboulder": 1, "nomatch": 1}, r["counts"]
        assert relevance.split_rejected(d, print) is None, "once"
    finally:
        relevance.boulder_probability, relevance.available = real
print("pooling and splitting OK")

# --- the viewpoint verdict, by the current rules, from a report made under older ones ---------------
from climbs import diversity  # noqa: E402

graph = lambda k, n, verdict="poor": {"estimated_registerable": k, "unique_images": n, "verdict": verdict, "view_graph": {"available": True}}  # noqa: E731
assert diversity.judge(graph(diversity.GOOD, 40)) == "good" and diversity.judge(graph(diversity.MARGINAL, 6)) == "marginal"
assert diversity.judge(graph(diversity.TRY_VIEWS, diversity.TRY_PHOTOS)) == "marginal", "few connected views, but enough photos for COLMAP to try"
assert diversity.judge(graph(diversity.TRY_VIEWS, diversity.TRY_PHOTOS - 1)) == "poor"
assert diversity.judge(graph(diversity.TRY_VIEWS - 1, 200)) == "poor", "many photos that hardly connect are mostly junk"
assert diversity.judge({"verdict": "unknown-good", "view_graph": {"available": False}}) == "unknown-good", "nothing to judge again"
assert diversity.judge(None) == "unknown"
print("viewpoint verdict OK")

# --- which climbs' photos are of the same rock: two climbs on one rock, one on another; each pair of climbs counted
if "same-rock" in relevance.available():
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        samples = {"left": [view_of(d, "mountainproject_l1.jpg", 100, 100), view_of(d, "yt_l2.jpg", 300, 200)],
                   "right": [view_of(d, "mountainproject_r1.jpg", 600, 500), view_of(d, "yt_r2.jpg", 700, 400)],
                   "else": [view_of(d, "mountainproject_e1.jpg", 200, 300, make_rock(6)), view_of(d, "yt_e2.jpg", 500, 400, make_rock(6))]}
        assert relevance.link_sample(sorted(d.iterdir()), 3)[:2] == [d / "mountainproject_e1.jpg", d / "mountainproject_l1.jpg"], "stills first"
        pairs = relevance.nearest(samples, ["left"], 8, print)
        assert pairs == [("else", "left"), ("left", "right")], pairs
        logs = []
        got = relevance.link_climbs(samples, pairs, logs.append)
        assert got[("left", "right")] >= relevance.LINK_MIN_PAIRS and got[("else", "left")] == 0, got
        assert "1 of the same rock" in logs[-1], logs
        print("climbs of one rock OK")
else:
    print("climbs of one rock skipped: no OpenCV")
print("ALL RELEVANCE TESTS PASSED")
