#!/usr/bin/env python3
"""The incremental mapper's state logic (experiments/mapper/incr/state.py), without COLMAP or a GPU."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "mapper" / "incr"))
import state as st  # noqa: E402


def photos(d: Path, n: int, start: int = 0) -> list[Path]:
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(start, start + n):
        p = d / f"p{i:03d}.jpg"
        p.write_bytes(f"photo {i}".encode())
        out.append(p)
    return out


def fake_model(root: Path, name: str = "model.new") -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "images.bin").write_bytes(b"x")


def test_first_add_and_duplicates(tmp_path):
    s = st.State(tmp_path / "s")
    new, same = s.plan(photos(tmp_path / "in", 5))
    assert len(new) == 5 and same == []
    s.begin(new)
    fake_model(s.root)
    rec = s.commit({"kind": "first", "placed": {"p000.jpg": "vggt", "p001.jpg": "pnp"}, "tried": [n for _, n, _ in new]})
    assert rec["placed_total"] == 2 and s.has_model() and not s.journal()
    assert s.unplaced() == ["p002.jpg", "p003.jpg", "p004.jpg"]
    # the same photos again, under other names: nothing new
    again = tmp_path / "again"
    again.mkdir()
    for p in (tmp_path / "in").iterdir():
        (again / ("x" + p.name)).write_bytes(p.read_bytes())
    new, same = st.State(s.root).plan(sorted(again.iterdir()))
    assert new == [] and len(same) == 5


def test_name_clash_gets_hash(tmp_path):
    s = st.State(tmp_path / "s")
    new, _ = s.plan(photos(tmp_path / "a", 1))
    s.begin(new)
    s.commit({"kind": "first", "placed": {}})
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "p000.jpg").write_bytes(b"another photo")
    new, _ = st.State(s.root).plan([tmp_path / "b" / "p000.jpg"])
    assert new[0][1].startswith("p000-") and new[0][1].endswith(".jpg")


def test_interrupted_add_keeps_old_state_and_resumes(tmp_path):
    s = st.State(tmp_path / "s")
    new, _ = s.plan(photos(tmp_path / "in", 3))
    s.begin(new)
    fake_model(s.root)
    s.commit({"kind": "first", "placed": {"p000.jpg": "vggt"}})
    before = json.loads(s.manifest_path.read_text())
    # an add that dies after making half a model
    s2 = st.State(s.root)
    new2, _ = s2.plan(photos(tmp_path / "more", 2, 10))
    s2.begin(new2)
    fake_model(s2.root)
    assert json.loads(s.manifest_path.read_text()) == before
    # the next add: the half-made model is thrown away, the pending photos are taken up with no copy offered again
    s3 = st.State(s.root)
    new3, same3 = s3.plan(sorted((tmp_path / "more").iterdir()))
    assert new3 == [] and len(same3) == 2
    j = s3.begin(new3)
    assert set(j["photos"]) == {"p010.jpg", "p011.jpg"} and not (s3.root / "model.new").exists()
    fake_model(s3.root)
    rec = s3.commit({"kind": "add", "placed": {"p010.jpg": "pnp"}})
    assert rec["new"] == ["p010.jpg", "p011.jpg"] and rec["placed_total"] == 2 and (s3.root / "model.prev").exists()


def test_failed_add_keeps_photos_unplaced(tmp_path):
    s = st.State(tmp_path / "s")
    new, _ = s.plan(photos(tmp_path / "in", 2))
    s.begin(new)
    fake_model(s.root)
    s.commit({"kind": "first", "placed": {"p000.jpg": "vggt", "p001.jpg": "vggt"}})
    new, _ = s.plan(photos(tmp_path / "rock2", 2, 50))
    s.begin(new)
    rec = s.commit({"kind": "failed", "placed": {}, "tried": ["p050.jpg", "p051.jpg"]})
    assert rec["placed_total"] == 2 and s.unplaced() == ["p050.jpg", "p051.jpg"]
    assert s.photos["p050.jpg"]["tries"] == 1


def test_resolve_gate():
    assert st.resolve_due(100, 60, 0, 0)[0]                 # grown 67% since the last full solve
    assert not st.resolve_due(70, 60, 0, 0)[0]
    assert st.resolve_due(70, 60, 10, 3)[0]                 # most linked photos failed to register
    assert not st.resolve_due(70, 60, 4, 0)[0]              # too few tried to judge
    assert st.resolve_due(24, 23, 1, 1, left_joined=8, placed_now=24)[0]       # an add joined photos it couldn't place
    assert not st.resolve_due(24, 23, 1, 1, left_joined=2, placed_now=24)[0]


def test_remesh_gate():
    assert st.remesh_due(None, 10, 0, 100)[0]
    mesh = {"placed": 40}
    assert st.remesh_due(mesh, 42, 10000, 500)[0]           # 5% more points: more surface
    assert not st.remesh_due(mesh, 42, 10000, 100)[0]       # cameras only
    assert st.remesh_due(mesh, 51, 10000, 0)[0]             # many more views
    assert not st.remesh_due(mesh, 0, 0, 0)[0]
    # measured on the mesh, the coverage decides, not the point count
    g = {"covered_before": 0.40, "covered_after": 0.41, "seen_before": 0.9, "seen_after": 0.9, "new_points": 900, "new_points_off_mesh": 10}
    assert not st.remesh_due(mesh, 42, 10000, 900, g)[0]
    assert st.remesh_due(mesh, 42, 10000, 900, {**g, "covered_after": 0.48})[0]
    assert st.remesh_due(mesh, 42, 10000, 900, {**g, "new_points_off_mesh": 400})[0]


GOOD = {"placed": 72, "points": 40000, "track": 7.4, "err_mean": 0.77, "err_p90": 1.72, "pairs": 750, "pair_med_deg": 2.2,
        "pair_p90_deg": 18.0, "odd_lenses": 0, "untied": 0}


def test_model_check():
    """The model's check of itself, on the numbers the benchmark's models gave (selfcheck.py table)."""
    assert st.model_check(GOOD)["ok"]
    assert st.model_check({**GOOD, "err_mean": 0.95, "err_p90": 1.75, "pair_med_deg": 3.03, "odd_lenses": 8, "placed": 143})["ok"]     # Drifter's baseline
    spoilt = st.model_check({**GOOD, "err_mean": 5.70, "err_p90": 15.08, "pair_med_deg": 11.2})         # 9f3b after the assist
    assert not spoilt["ok"] and spoilt["failed"] == ["error", "pairs"] and "5.70 px" in spoilt["why"]
    assert st.model_check({**GOOD, "err_mean": 1.09, "err_p90": 1.93, "pair_med_deg": 16.2, "odd_lenses": 24, "placed": 114})["failed"] == ["pairs", "lenses"]
    assert st.model_check({**GOOD, "err_mean": 2.45, "err_p90": 3.13})["failed"] == ["error"]
    assert st.model_check({**GOOD, "pairs": 5, "pair_med_deg": 40.0})["ok"], "too few pairs to judge by"
    assert not st.model_check({**GOOD, "err_mean": None, "err_p90": None, "points": 0})["ok"]
    assert st.model_check({**GOOD, "placed": 6, "odd_lenses": 3})["ok"], "too few cameras for the lens share to mean much"


def test_check_rank_keeps_the_better():
    """Of several starts' models: one that passes; else the one failing least, then nearest its pairs' geometry."""
    ok = {**GOOD, **st.model_check(GOOD)}
    worse = {**GOOD, "pair_med_deg": 11.2, "err_mean": 5.7, "err_p90": 15.0}
    worse = {**worse, **st.model_check(worse)}
    pairs_only = {**GOOD, "pair_med_deg": 9.0, "placed": 60}
    pairs_only = {**pairs_only, **st.model_check(pairs_only)}
    pairs_more = {**GOOD, "pair_med_deg": 16.2, "placed": 140}
    pairs_more = {**pairs_more, **st.model_check(pairs_more)}
    assert max([worse, ok, pairs_only], key=st.check_rank) is ok
    assert max([worse, pairs_only, pairs_more], key=st.check_rank) is pairs_only
    assert max([pairs_more, worse], key=st.check_rank) is pairs_more


def test_vggt_start_worth():
    assert st.vggt_start_worth(80, 72)[0] and st.vggt_start_worth(42, 44)[0]
    no, why = st.vggt_start_worth(42, 150)
    assert not no and "42 of the 150" in why
    assert not st.vggt_start_worth(42, 72)[0], "58% is under two thirds"
    assert st.vggt_start_worth(80, 110)[0] and not st.vggt_start_worth(0, 0)[0]


def test_pair_angles():
    """Cameras against the two-view geometry of their pairs: none off where the model is the one the pairs came
    from, and the turn found where a camera is turned away."""
    import math
    import sqlite3
    import numpy as np
    import selfcheck
    rng = np.random.default_rng(0)

    def rot(axis, deg):
        a = np.asarray(axis, float) / np.linalg.norm(axis)
        K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
        t = math.radians(deg)
        return np.eye(3) + math.sin(t) * K + (1 - math.cos(t)) * K @ K
    cams = {}
    for i in range(6):
        R = rot(rng.normal(size=3), rng.uniform(0, 40))
        cams[f"p{i}.jpg"] = {"R": R, "t": rng.normal(size=3), "f": 1000.0 + 50 * i, "cx": 640.0, "cy": 360.0, "w": 1280, "h": 720, "seen": 100}
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "db.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE images (image_id INTEGER, name TEXT)")
        con.execute("CREATE TABLE two_view_geometries (pair_id INTEGER, rows INTEGER, config INTEGER, F BLOB)")
        names = sorted(cams)
        for i, n in enumerate(names):
            con.execute("INSERT INTO images VALUES (?, ?)", (i + 1, n))
        for i, a in enumerate(names):
            for j in range(i + 1, len(names)):
                b = names[j]
                R = cams[b]["R"] @ cams[a]["R"].T
                t = cams[b]["t"] - R @ cams[a]["t"]
                E = np.array([[0, -t[2], t[1]], [t[2], 0, -t[0]], [-t[1], t[0], 0]]) @ R
                Ka, Kb = [np.array([[cams[n]["f"], 0, 640.0], [0, cams[n]["f"], 360.0], [0, 0, 1.0]]) for n in (a, b)]
                F = np.linalg.inv(Kb).T @ E @ np.linalg.inv(Ka)
                con.execute("INSERT INTO two_view_geometries VALUES (?, ?, ?, ?)",
                            ((i + 1) * selfcheck.MAX_IMAGE_ID + j + 1, 100, 3, F.astype(np.float64).tobytes()))
        con.execute("INSERT INTO two_view_geometries VALUES (?, ?, ?, ?)", (1 * selfcheck.MAX_IMAGE_ID + 2, 5, 3, b""))       # too few inliers
        con.commit()
        con.close()
        ang = selfcheck.pair_angles(cams, db)
        assert len(ang) == 15 and max(ang) < 1e-4
        n = selfcheck.check(cams, rng.uniform(0.3, 1.5, 500), 6.0, db)
        assert n["ok"] and n["pairs"] == 15
        turned = {k: dict(v) for k, v in cams.items()}
        for k in names[:4]:      # most of the cameras turned away from where their pairs have them
            turned[k]["R"] = rot(rng.normal(size=3), 20) @ turned[k]["R"]
        bad = selfcheck.numbers(turned, rng.uniform(0.3, 1.5, 500), 6.0, db)
        assert bad["pair_med_deg"] > 10 and st.model_check({**bad, "pairs": 30})["failed"] == ["pairs"]


if __name__ == "__main__":
    import tempfile
    for name, f in sorted(globals().items()):
        if name.startswith("test_"):
            if f.__code__.co_argcount:
                with tempfile.TemporaryDirectory() as d:
                    f(Path(d))
            else:
                f()
            print("ok", name)
