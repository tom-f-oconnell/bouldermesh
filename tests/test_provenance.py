#!/usr/bin/env python3
"""Recording where a mesh's photos came from, on project folders built here. Offline."""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import provenance  # noqa: E402

VIDEO = "https://www.youtube.com/watch?v=abc"
TAG = hashlib.sha1(VIDEO.encode()).hexdigest()[:8]


def project(root: pathlib.Path) -> pathlib.Path:
    """A harvest with photos from three sources, one converted to JPEG after download, and one uploaded."""
    p = root / "proj"
    (p / "photos").mkdir(parents=True)
    names = ["ddg_aa.jpg", "ddg_bb.jpg", "ddg_cc.jpg", "commons_dd.jpg", f"yt_{TAG}_000.jpg", f"yt_{TAG}_001.jpg", "mine.jpg"]
    for n in names:
        (p / "photos" / n).write_bytes(b"x")
    files = [{"file": "ddg_aa.jpg", "source": "ddg", "url": "https://a/1.jpg", "page": "https://a/", "license": "unknown (web image; check page)"},
             {"file": "ddg_bb.jpg", "source": "ddg", "url": "https://b/2.jpg", "license": "unknown (web image; check page)"},
             {"file": "ddg_cc.webp", "source": "ddg", "url": "https://c/3.webp", "license": "unknown (web image; check page)"},
             {"file": "commons_dd.jpg", "source": "commons", "url": "https://commons/4.jpg", "credit": "Ann", "license": "CC BY-SA 3.0"},
             {"file": "ddg_rejected.jpg", "source": "ddg", "url": "https://d/5.jpg", "license": "unknown (web image; check page)"}]
    files += [{"file": f"yt_{TAG}_00{i}.jpg", "url": None, "kind": "video-frame", "source": "youtube",
               "license": "frame from linked beta video; reconstruction use only"} for i in range(2)]
    (p / "sources.json").write_text(json.dumps({"climb": {"videos": [{"url": VIDEO, "kind": "video"}]}, "files": files}))
    return p


with tempfile.TemporaryDirectory() as td:
    root = pathlib.Path(td)
    proj, mesh = project(root), root / "mesh"
    mesh.mkdir()

    # without cameras.json: every photo it was given, each matched to what the harvest recorded
    record = provenance.save(mesh, proj)
    assert record["basis"] == "given" and len(record["photos"]) == 7, record
    by = {p["file"]: p for p in record["photos"]}
    assert by["ddg_cc.jpg"]["url"] == "https://c/3.webp", "a photo converted to JPEG keeps its entry"
    assert by[f"yt_{TAG}_001.jpg"]["url"] == VIDEO, "a frame's video is found from the climb's videos"
    assert by["mine.jpg"]["source"] == "upload", "a photo the harvest didn't record is the user's own"
    assert "ddg_rejected.jpg" not in by, "only photos it was given"
    assert provenance.load(mesh) == json.loads((mesh / provenance.FILE).read_text())

    s = provenance.summary(provenance.load(mesh))
    assert s["basis"] == "given" and s["total"] == 7, s
    assert [(x["source"], x["photos"]) for x in s["sources"]] == [("ddg", 3), ("youtube", 2), ("commons", 1), ("upload", 1)], s
    yt = next(x for x in s["sources"] if x["source"] == "youtube")
    assert yt["videos"] == 1 and next(x for x in s["sources"] if x["source"] == "ddg")["videos"] is None, s
    assert next(x for x in s["sources"] if x["source"] == "commons")["licenses"] == {"CC BY-SA 3.0": 1}, s

    # with cameras.json: only the photos placed in the mesh
    (mesh / "cameras.json").write_text(json.dumps([{"name": "commons_dd.jpg", "q": [1, 0, 0, 0], "t": [0, 0, 0]},
                                                   {"name": f"yt_{TAG}_000.jpg", "q": [1, 0, 0, 0], "t": [0, 0, 0]}]))
    s = provenance.summary(provenance.save(mesh, proj))
    assert s["basis"] == "placed" and s["total"] == 2 and {x["source"] for x in s["sources"]} == {"commons", "youtube"}, s

    # a project's record gone: names still say where most came from
    (proj / "sources.json").unlink()
    (mesh / "cameras.json").unlink()
    by = {p["file"]: p for p in provenance.save(mesh, proj)["photos"]}
    assert by["ddg_aa.jpg"]["source"] == "ddg" and by["ddg_aa.jpg"]["license"] is None, by["ddg_aa.jpg"]
    assert by[f"yt_{TAG}_000.jpg"]["source"] == "youtube" and by["mine.jpg"]["source"] == "upload"
    assert provenance.summary(None) is None and provenance.load(root / "nowhere") is None

    # a pooled photo its own harvest hadn't recorded: an entry without a source, which its name fills in
    (proj / "sources.json").write_text(json.dumps({"files": [{"file": "ddg_aa.jpg", "pooled_from": "other"},
                                                             {"file": "mine.jpg", "pooled_from": "other"}]}))
    by = {p["file"]: p for p in provenance.save(mesh, proj)["photos"]}
    assert by["ddg_aa.jpg"]["source"] == "ddg" and by["ddg_aa.jpg"]["pooled_from"] == "other", by["ddg_aa.jpg"]
    assert by["mine.jpg"]["source"] == "upload", by["mine.jpg"]
    # and one saved like that before: loading it fills it in, so the summary doesn't fail
    (mesh / provenance.FILE).write_text(json.dumps({"basis": "given", "photos": [{"file": "ddg_aa.jpg", "pooled_from": "other"},
                                                                                 {"file": f"yt_{TAG}_000.jpg", "source": "youtube"}]}))
    s = provenance.summary(provenance.load(mesh))
    assert sorted(x["source"] for x in s["sources"]) == ["ddg", "youtube"], s
print("ALL PROVENANCE TESTS PASSED")
