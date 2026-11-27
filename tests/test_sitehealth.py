#!/usr/bin/env python3
"""The daily summary of how the site went for visitors (sitehealth.py), from made-up report files; offline.

    python3 tests/test_sitehealth.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import sitehealth  # noqa: E402

DAY = "2026-11-19T"
BEFORE = "2026-11-17T10:00:00Z"


def t(hour: int) -> str:
    return f"{DAY}{hour:02d}:00:00Z"


def main() -> None:
    views = [{"time": BEFORE, "mesh": "aa", "seconds": 99}]     # before the day: left out
    views += [{"time": t(h), "mesh": "aa" if h % 2 else "bb", "name": "Caveman", "seconds": float(h), "via": "link",
               **({"page_seconds": 5.0, "list_seconds": 2.0} if h == 1 else {})} for h in range(1, 13)]
    views += [{"time": t(13), "mesh": "cc", "seconds": 40, "editor": True}]       # an editor's: left out
    views += [{"time": t(14), "mesh": "aa", "event": "left", "left": "page", "seconds": 20},
              {"time": t(14), "mesh": "bb", "event": "left", "left": "other", "seconds": 4}]
    views += [{"time": t(15), "mesh": "aa", "event": "frames", "frames": {"n": 200, "median_ms": 80, "p95_ms": 200, "slow": 40}, "gpu": "Mali-G52"},
              {"time": t(15), "mesh": "bb", "event": "frames", "frames": {"n": 900, "median_ms": 16, "p95_ms": 20, "slow": 0}, "gpu": "RTX"}]
    errors = [{"time": BEFORE, "kind": "page", "message": "Failed to fetch"},
              {"time": t(2), "kind": "page", "message": "Failed to fetch"},
              {"time": t(3), "kind": "page", "message": "Script error."},
              {"time": t(3), "kind": "page", "message": "undefined is not an object (evaluating 'window.ethereum.selectedAddress = undefined')"},
              {"time": t(4), "kind": "page", "message": "p.filtered is undefined", "editor": True},
              {"time": t(5), "kind": "webgl-lost", "message": "the graphics card dropped the view", "gpu": "Mali-G52"},
              {"time": t(6), "kind": "page", "message": "invalid assignment to const 'active'", "build": "0c915be0aaaaaaaa", "old": True}]
    slow = [{"time": t(6), "method": "GET", "path": "/api/meshes", "seconds": 4.2},
            {"time": t(7), "method": "GET", "path": "/api/meshes", "seconds": 9.0},
            {"time": t(8), "method": "GET", "path": "/m/<id>", "seconds": 3.1}]

    s = sitehealth.summary(views, errors, slow, f"{DAY}00:00:00Z")
    assert s["views"] == 12 and s["meshes"] == 2, s
    assert s["load"]["median"] == 7 and s["load"]["slow"] == 2 and s["load"]["worst"] == (12.0, "Caveman"), s["load"]
    assert s["first"] == {"median": 5.0, "p90": 5.0} and s["list"]["median"] == 2.0, s
    assert s["left"] == {"n": 2, "page": 1, "median": 20}, s["left"]
    assert s["frames"]["n"] == 2 and s["frames"]["jerky"] == 1 and s["frames"]["gpus"] == [("Mali-G52", 1)], s["frames"]
    errs = s["errors"]
    assert errs["n"] == 3 and errs["editor"] == 1 and errs["lost_view"] == 1, errs
    top = {e["message"]: e for e in errs["top"]}
    assert not top["Failed to fetch"]["new"] and top["p.filtered is undefined"]["new"], "seen before the day, or not"
    assert "Script error." not in top and not any("ethereum" in m for m in top), "extensions' and others' scripts left out"
    assert errs["old_pages"] == 1 and "invalid assignment to const 'active'" not in top, "an old page's: apart"
    assert s["slow"] == {"n": 3, "top": [("GET /api/meshes", 2, 9.0), ("GET /m/<id>", 1, 3.1)]}, s["slow"]

    title, text, look = sitehealth.note(s)
    assert look and "12 views" in title and "3 slow requests" in title, title
    for bit in ("12 view(s) of 2 mesh(es)", "2 over 10 s, slowest Caveman at 12 s", "Page open to first mesh: median 5 s",
                "Given up on while loading: 2 (1 by leaving", "Moved badly", "Mali-G52 1", "lost to the graphics card",
                "NEW page: p.filtered is undefined", "GET /api/meshes 2x (up to 9 s)",
                "pages older than the one served (tabs left open across a deploy): 1, as 1x invalid assignment"):
        assert bit in text, (bit, text)
    assert len(text) < 2000

    quiet = sitehealth.summary(views, errors, slow, "2026-11-20T00:00:00Z")
    assert sitehealth.note(quiet) is None, "nothing at all: nothing to send"
    calm = sitehealth.note(sitehealth.summary([{"time": t(1), "mesh": "aa", "seconds": 2}], [], [], f"{DAY}00:00:00Z"))
    assert calm and not calm[2], calm

    # where the page was opened from: editors' and bots' apart, places by their point, countries, linking sites, days
    opened = [{"time": BEFORE, "country": "US", "lat": 37.4, "lng": -118.6},
              {"time": t(1), "country": "US", "region": "California", "city": "Bishop", "lat": 37.4, "lng": -118.4, "new": True, "ref": "www.reddit.com"},
              {"time": t(2), "country": "US", "city": "Bishop", "lat": 37.4, "lng": -118.4},
              {"time": t(3), "country": "FR", "city": "Fontainebleau", "lat": 48.4, "lng": 2.7, "app": True},
              {"time": t(4)},
              {"time": t(5), "country": "US", "lat": 1.0, "lng": 1.0, "editor": True},
              {"time": t(6), "country": "US", "lat": 2.0, "lng": 2.0, "bot": True}]
    pv = sitehealth.page_views(opened, f"{DAY}00:00:00Z")
    assert (pv["n"], pv["new"], pv["editor"], pv["app"], pv["unplaced"]) == (4, 1, 1, 1, 1), pv
    assert pv["places"][0] == {"lat": 37.4, "lng": -118.4, "n": 2, "place": "Bishop, California, US"} and len(pv["places"]) == 2, pv
    assert pv["countries"] == [("US", 2), ("FR", 1), ("?", 1)] and pv["referrers"] == [("www.reddit.com", 1)], pv
    assert pv["days"] == [("2026-11-19", 4)], pv
    title, text, _ = sitehealth.note(quiet, opened=pv)
    assert text.startswith("The page opened 4 time(s) (1 in new browsers), from US 2, FR 1, ? 1."), text
    # embeds, in other sites' pages: counted apart, with the sites showing them
    emb = opened + [{"time": t(7), "path": "/embed/c/the-mandala", "ref": "blog.example", "country": "US", "lat": 3.0, "lng": 3.0},
                    {"time": t(8), "path": "/embed/m/<id>"}, {"time": t(9), "path": "/embed/m/<id>", "editor": True}]
    pe = sitehealth.page_views(emb, f"{DAY}00:00:00Z")
    assert pe["n"] == pv["n"] and pe["places"] == pv["places"] and pe["embedded"] == {"n": 2, "sites": [("blog.example", 1), ("?", 1)]}, pe
    assert pv["embedded"] == {"n": 0, "sites": []}, pv
    text = sitehealth.note(quiet, opened=pe)[1]
    assert "\nEmbeds shown in other sites' pages: 2 (blog.example 1, ? 1).\n" in text, text
    assert sitehealth.note(quiet, opened=sitehealth.page_views(emb[-3:], f"{DAY}00:00:00Z")), "embeds alone: still sent"

    with tempfile.TemporaryDirectory() as td:   # from a data folder: the file and the one before it, bad lines skipped
        d = Path(td)
        (d / "mesh-views.jsonl.1").write_text(json.dumps(views[1]) + "\n")
        (d / "mesh-views.jsonl").write_text("not json\n" + json.dumps(views[2]) + "\n")
        got = sitehealth.for_data(d, 24, now=1795135980)     # 2026-11-20T00:53Z
        assert got and got[0].startswith("BoulderMesh's day: 2 views"), got
        assert sitehealth.for_data(d, 24, now=1795135980 + 86400) is None, "a day later: none of it"
        (d / "page-views.jsonl").write_text(json.dumps(opened[1]) + "\n")
        spans = sitehealth.page_views_for_data(d, now=1795135980)
        assert set(spans) == {"day", "week", "month"} and spans["week"]["n"] == 1, spans
    print("site health OK")


if __name__ == "__main__":
    main()
