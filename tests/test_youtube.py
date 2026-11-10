#!/usr/bin/env python3
"""Telling a climb's videos from others, and reading climbers' heights and ape indexes. Offline."""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from climbs import youtube as yt  # noqa: E402
from climbs.common import Climb  # noqa: E402

relic = Climb(source="mountainproject", source_id="106743866", url="https://www.mountainproject.com/route/106743866/relic",
              name="Relic", grade="V9 7C", boulder_name="Tidal Wave Boulder",
              area_path=["California", "Joshua Tree National Park", "Joshua Tree Bouldering", "Hidden Valley Area",
                         "Outback Bouldering", "Tidal Wave Boulder"])
assert yt.region(relic) == "Joshua Tree" and yt.v_grade(relic) == "V9", (yt.region(relic), yt.v_grade(relic))
assert yt.query(relic) == "Relic V9 Joshua Tree", yt.query(relic)

# the titles one real search returned (2026-11-03): the first six are Relic
for title in ["Relic V9 - Joshua Tree", "Relic v9 Joshua tree", "Relic (V9) - Joshua Tree", "relic [ joshua tree ]",
              "Relic - Joshua Tree", "Relic - Joshua tree HD"]:
    assert yt.matches(relic, title), title
for title in ["Burlday Bash V9 (FA) Joshua Tree Bouldering", "Bittersweet V9 - Joshua Tree Bouldering",
              "Joshua Tree - A Name I Don’t Know V7", "Broken Flowers (Sit) V9 Joshua Tree Bouldering",
              "RAW: BITTERSWEET V9 JOSHUA TREE, CALIFORNIA", "La Migra V3 | Joshua Tree bouldering"]:
    assert not yt.matches(relic, title), title
assert not yt.matches(relic, "Relic Hunters official trailer"), "a name needs something placing it"
assert not yt.matches(relic, "Relics of Joshua Tree"), "whole words only"
assert yt.matches(relic, "Relic", "Hidden Valley Area, on the Tidal Wave Boulder"), "the description can place it"
assert yt.matches(relic, "Relic V9"), "the grade can place it"
assert yt.video_id("https://www.youtube.com/watch?v=e-mg8jg4fT4") == "e-mg8jg4fT4"
assert yt.video_id("https://youtu.be/H2EeJEvxpJI") == "H2EeJEvxpJI"

# heights, wingspans and ape indexes in cm, or None
cases = [
    ("I'm 5'10\" with a +2 ape index", 177.8, None, 5.1, True),
    ("Me (5'7) on Relic, flashed it", 170.2, None, None, True),
    ("This 6 ft tall boulder is sharp", None, None, None, False),
    ("I'm 6ft so the start is easy", 182.9, None, None, True),
    ("The climber is 178 cm tall, wingspan 185cm", 178.0, 185.0, 7.0, False),
    ("Height: 1.83m, ape index -1cm", 183.0, None, -1.0, False),
    ("A 15 ft highball above a bad landing", None, None, None, False),
    ("V5 then 5.10 slab, 3 stars", None, None, None, False),
    ("5'11\" climber, short reach", 180.3, None, None, False),
    ("+3 ape helps on the crux", None, None, 7.6, False),
    ("positive ape index, long reach", None, None, None, False),
    ("Relic (V9) at Joshua Tree National Park", None, None, None, False),
    ("my ape index is +4\" and I'm 5 foot 8", 172.7, None, 10.2, True),
    ("Arm span 6'2\", height 5'9\"", 175.3, 188.0, 12.7, False),
    ("Video by Sam, 2 feet of snow on the topout", None, None, None, False),
    ("I'm 15 ft from the start when I jump", None, None, None, False),
    ("I'm 6'1 and found it reachy", 185.4, None, None, True),
    ("the least weird/ morpho beta i've seen (5'5 and a half plus 2 ape ish)", 166.4, None, 5.1, False),
    ("i like climb Height: 5’5” | Ape Index: +1", 165.1, None, 2.5, False),
    ("-I'm 5'10 and have a -2 ape (5'8 wingspan)", 177.8, 172.7, -5.1, True),
]
for text, height, span, ape, me in cases:
    s = yt.stats(text)
    got = (s["height_cm"], s["wingspan_cm"], s["ape_index_cm"])
    assert got == (height, span, ape), (text, got)
    assert s["first_person"] == me, (text, s)
    assert bool(s["quotes"]) == any(x is not None for x in got), (text, s["quotes"])

d = yt.details({"webpage_url": "https://www.youtube.com/watch?v=x", "extractor": "youtube", "title": "t", "channel": "c", "license": None})
assert d["license"] == "YouTube standard licence (all rights reserved)" and d["uploader"] == "c", d
assert yt.details({"webpage_url": "https://vimeo.com/1", "extractor": "vimeo", "license": "by-nc"})["license"] == "by-nc"

# --- the gate: YouTube's bot check leaves it alone for a while, longer each time; searches still go through ---------
with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    calls, fake = td / "calls", td / "yt-dlp"
    # a stand-in yt-dlp: records its arguments, and answers with the bot check while td/refuse exists
    fake.write_text(f"""#!/bin/sh
echo "$*" >> {calls}
case "$*" in *--flat-playlist*) search=1 ;; *) search= ;; esac
if [ -e {td}/refuse ] && [ -z "$search" ]; then
  echo "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. This helps protect our community." >&2; exit 1
fi
if [ -e {td}/refuse-search ] && [ -n "$search" ]; then
  echo "ERROR: [youtube:search] x: Sign in to confirm you’re not a bot. This helps protect our community." >&2; exit 1
fi
echo '{{"entries": [], "title": "t"}}'
""")
    fake.chmod(0o755)
    yt.STATE_FILE, yt.PAGES_STATE_FILE, yt.GAP = td / "youtube.json", td / "youtube-pages.json", (0.0, 0.0)
    run = lambda video=True, args=("--skip-download", "x"): yt.call([str(fake), "-J", *args], video=video, timeout=30,  # noqa: E731
                                                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert json.loads(run())["title"] == "t" and yt.refused() is None
    assert "--sleep-requests 0.75" in calls.read_text(), "requests within a run are spaced too"
    (td / "refuse").touch()
    try:
        run()
        raise AssertionError("refused")
    except yt.Refused as e:
        first = e.state
    assert first["level"] == 0 and 3500 < first["until"] - time.time() <= 3600 and "not a bot" in first["error"], first
    n = len(calls.read_text().splitlines())
    try:
        run()
        raise AssertionError("still refused")
    except yt.Refused:
        pass
    assert len(calls.read_text().splitlines()) == n, "nothing asked of YouTube meanwhile"
    run(video=False, args=("--flat-playlist", "ytsearch15:x"))
    assert len(calls.read_text().splitlines()) == n + 1, "a search still goes"
    yt.STATE_FILE.write_text(json.dumps({**first, "until": time.time() - 1}))    # an hour on: it tries again
    assert yt.refused() is None
    try:
        run()
        raise AssertionError("refused again")
    except yt.Refused as e:
        second = e.state
    assert second["level"] == 1 and 7100 < second["until"] - time.time() <= 7200 and second["since"] == first["since"], second
    yt.STATE_FILE.write_text(json.dumps({**second, "until": time.time() - 1}))
    (td / "refuse").unlink()
    run()
    assert yt.refused() is None and not yt.STATE_FILE.exists(), "one let through: it's over"

    # searches refused have a pause of their own, which leaves downloads alone
    search = lambda: run(video=False, args=("--flat-playlist", "ytsearch15:x"))     # noqa: E731
    (td / "refuse-search").touch()
    try:
        search()
        raise AssertionError("search refused")
    except yt.Refused as e:
        assert e.state["what"] == "searches" and "searches" in str(e) and yt.refused(video=False) and yt.refused() is None, e.state
    n = len(calls.read_text().splitlines())
    try:
        search()
        raise AssertionError("searches still refused")
    except yt.Refused:
        pass
    assert len(calls.read_text().splitlines()) == n, "no search asked of YouTube meanwhile"
    run()
    assert len(calls.read_text().splitlines()) == n + 1, "a download still goes"
    yt.PAGES_STATE_FILE.write_text(json.dumps({**yt.refused(video=False), "until": time.time() - 1}))
    (td / "refuse-search").unlink()
    search()
    assert yt.refused(video=False) is None and not yt.PAGES_STATE_FILE.exists()
    log, real_call = [], yt.call
    yt.call = lambda cmd, **kw: real_call([str(fake), *cmd[1:]], **kw)     # search() names yt-dlp itself
    (td / "refuse-search").touch()
    try:
        n = len(calls.read_text().splitlines())
        for _ in range(2):
            assert yt.search(relic, log.append) == [] and "refusing this machine's searches" in log[-1], log
        assert len(calls.read_text().splitlines()) == n + 1, "the second search waits, without asking"
    finally:
        yt.call = real_call
    (td / "refuse-search").unlink()
    yt.PAGES_STATE_FILE.unlink()

    # a harvest skips the rest of its videos rather than asking again for each
    from climbs import harvest
    from climbs.common import Media
    (td / "refuse").touch()
    saved = shutil.which
    shutil.which = lambda name: str(fake) if name == "yt-dlp" else saved(name)
    lines, n = [], len(calls.read_text().splitlines())
    try:
        got = harvest.video_frames([Media(url=f"https://www.youtube.com/watch?v=v{i}", kind="video") for i in range(4)],
                                   td / "out", lines.append)
    finally:
        shutil.which = saved
    assert got == [] and len(calls.read_text().splitlines()) == n + 1, lines
    assert any("skipping 4 video(s)" in x for x in lines), lines

# --- a harvest asks for YouTube's videos alongside its photos, not after them -------------------------------------------
import threading  # noqa: E402

from climbs import harvest  # noqa: E402
from progress import Tracker  # noqa: E402

search_asked, photos_saw, wanted = threading.Event(), [], []
real = (harvest.download_all, yt.search)


def slow_photos(media, out_dir, log, progress=None, **kw):     # slow, as Mountain Project's minute a photo makes them
    for i in range(50):
        progress(i / 50, f"{i} of 50 photos")
        if search_asked.wait(0.2):
            break
    photos_saw.append(search_asked.is_set())
    return []


def search_by_volunteer(c, log, want=None, asked=None, state=None, **kw):
    want(f"{c.name} bouldering")
    asked.append(f"{c.name} bouldering")
    search_asked.set()
    state["checked"] = False
    return []


harvest.download_all, yt.search = slow_photos, search_by_volunteer
snapshots = []
t = Tracker(harvest.steps({"climb", "youtube"}, relevance_filter=False), snapshots.append)
try:
    with tempfile.TemporaryDirectory() as d:
        out = harvest.harvest(relic, pathlib.Path(d), lambda line: None, {"climb", "youtube"}, relevance_filter=False,
                              tracker=t, want=lambda url: True, want_search=lambda words: wanted.append(words) or True)
finally:
    harvest.download_all, yt.search = real
assert wanted == ["Relic bouldering"] and photos_saw == [True], "volunteers were asked while the photos were still coming"
assert out["youtube_checked"] is False, out
steps = {x["key"]: x for x in t.snapshot()["steps"]}
assert steps["video"]["state"] == "done" and "volunteers asked for 1 search" in steps["video"]["detail"], steps["video"]
print("YouTube alongside photos OK")
print("ALL YOUTUBE TESTS PASSED")
