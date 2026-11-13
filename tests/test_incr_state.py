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
