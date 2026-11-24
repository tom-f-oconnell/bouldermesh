#!/usr/bin/env python3
"""Downloading ahead (server.download_ahead()): in Mountain Project's turns nothing is waiting for, the pages and photos
of each running area's next climbs, kept as their harvests keep them, so that the harvests find them there; a photo's
missing _large_ size asked for once; a turn reserved by anyone else left alone; a failed request not asked again.
Offline: the network is a stub.

    python3 tests/test_ahead.py
"""
from __future__ import annotations

import io
import os
import pathlib
import sys
import tempfile
import time
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
tmp = tempfile.mkdtemp(prefix="bouldermesh-ahead-")
os.environ.update(DATA_DIR=tmp, HTTP_IGNORE_ROBOTS="1", HTTP_MIN_INTERVAL="0")

import server  # noqa: E402
from climbs import common, harvest as harvest_mod  # noqa: E402
from climbs.common import Climb, Media  # noqa: E402

HEAD = 'Route,Location,URL,"Avg Stars","Your Stars","Route Type",Rating,Pitches,Length,"Area Latitude","Area Longitude"\n'
CSV = HEAD + "".join(f'{n},"{n} Boulder > Sector > JT > California",https://www.mountainproject.com/route/{i}/{n.lower()},{s},-1,Boulder,V{i},,,34.0,-116.0\n'
                     for i, n, s in ((1, "Alpha", 3.0), (2, "Beta", 2.0)))
asked: list[str] = []
missing = {"https://www.mountainproject.com/assets/photos/climb/111_large_1.jpg"}


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=60):
    url = req.full_url
    asked.append(url)
    if url in missing or "broken" in url:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    if "/photo/" in url:    # a photo's own page, naming its file
        return Answer(b'<img src="/assets/photos/climb/333_medium_9.jpg">')
    return Answer(b"x" * 30000)


common.urllib.request.urlopen = fake_urlopen
pages = {"https://www.mountainproject.com/route/1/alpha": [
    Media(url="https://www.mountainproject.com/assets/photos/climb/111_medium_1.jpg", kind="image", page="p1", source="mountainproject"),
    Media(url="", kind="image", page="https://www.mountainproject.com/photo/222", source="mountainproject")]}


def fake_fetch(url, include_children=False):
    asked.append(url)
    return [Climb(source="mountainproject", source_id=url.split("/")[-2], url=url, name=url.split("/")[-1].title(), kind="boulder",
                  images=list(pages.get(url, [])), area_path=["California", "JT", "Sector", "Alpha Boulder"])]


server.climbs_mod.fetch_climb_url = fake_fetch
try:
    server.init_db()
    assert server.download_ahead() == "nothing", "no area running"
    c = server.create_campaign("", "popularity", CSV)
    order = []
    for _ in range(10):
        before = len(asked)
        state = server.download_ahead()
        order.append((state, asked[before:]))
        if state == "nothing":
            break
    urls = [u for _, got in order for u in got]
    assert urls[0] == "https://www.mountainproject.com/route/1/alpha", urls
    assert "https://www.mountainproject.com/assets/photos/climb/111_large_1.jpg" in urls and \
        "https://www.mountainproject.com/assets/photos/climb/111_medium_1.jpg" in urls, "_large_ missing: _medium_ instead"
    assert "https://www.mountainproject.com/photo/222" in urls, "the photo page of one its page didn't link directly"
    assert urls[-1] == "https://www.mountainproject.com/route/2/beta" and order[-1][0] == "nothing", order
    got = {i["name"]: i for i in server.q("SELECT * FROM campaign_items WHERE campaign_id=?", c["id"])}
    assert got["Alpha"]["climb_id"] and not server.climb_obj(got["Alpha"]["climb_id"]).raw.get("stub"), "read as a harvest reads it"
    assert server._ahead["pages"] == 3 and server._ahead["photos"] == 2, server._ahead
    print("downloading ahead, page then photos, climb by climb OK")

    # the harvest's own download then asks the site for nothing: each photo, _large_ or the _medium_ it falls back to
    asked.clear()
    media = harvest_mod.from_climb(server.climb_obj(got["Alpha"]["climb_id"]), lambda s: None)
    out = harvest_mod.download_all(media, pathlib.Path(tmp) / "photos", lambda s: None)
    assert len(out) == 2 and asked == [], asked
    assert all(harvest_mod.photo_cached(m) for m in media)
    print("the harvest finds them kept OK")

    # a turn another request has reserved is left alone; one that failed isn't asked again ahead
    host = "www.mountainproject.com"
    common.http._last[host] = time.time() + 30
    assert not common.http.slot_free("https://www.mountainproject.com/route/9/x")
    pages["https://www.mountainproject.com/route/3/gamma"] = [Media(url="https://www.mountainproject.com/assets/photos/climb/broken_medium_1.jpg",
                                                                    kind="image", page="p3", source="mountainproject")]
    server.create_campaign("", "popularity", HEAD + 'Gamma,"Gamma Boulder > Sector > JT > California",https://www.mountainproject.com/route/3/gamma,1,-1,Boulder,V3,,,34.0,-116.0\n')
    assert server.download_ahead() == "waiting", "its turn is taken"
    common.http._last[host] = 0
    asked.clear()
    assert server.download_ahead() == "fetched" and asked == ["https://www.mountainproject.com/route/3/gamma"]
    assert server.download_ahead() == "fetched" and len(asked) == 3, asked     # _large_ then _medium_, both missing
    assert server.failed_ahead("https://www.mountainproject.com/assets/photos/climb/broken_large_1.jpg")
    assert server.download_ahead() == "nothing", "a failed one not asked for again"
    server._draining = {"since": time.time()}
    assert server.download_ahead() == "nothing", "not while about to restart"
    server._draining = None
    print("spare turns only, failures left to the harvest OK")
    print("ALL AHEAD TESTS PASSED")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
