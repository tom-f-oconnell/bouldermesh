"""Offline tests of the relevance filter and job progress, on synthetic photos.  Run: python3 tests/test_relevance.py

The same-rock and CLIP checks need OpenCV and open_clip; without them only their absence is tested.
"""
import json, pathlib, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
import numpy as np
from PIL import Image, ImageDraw

from climbs import relevance
from progress import Tracker



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

        # no trusted photos at all: the group that matches each other stands in for the anchors
        web = [view_of(photos, f"web_{i}.jpg", x, y) for i, (x, y) in enumerate(((100, 100), (250, 200), (400, 300), (550, 400)))]
        r = relevance.filter_photos(web + [other], None, logs.append, methods=["same-rock"])
        assert r["rejected"] == {"commons_other.jpg": "nomatch"}, r["rejected"]
        # and nothing to go on: nothing is rejected
        lone = [other, view_of(photos, "web_lone.jpg", 300, 200, make_rock(5))]
        r = relevance.filter_photos(lone, None, logs.append, methods=["same-rock"])
        assert not r["rejected"], r["rejected"]
        print("same-rock OK")
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
print("progress OK")
print("ALL RELEVANCE TESTS PASSED")
