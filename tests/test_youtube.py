#!/usr/bin/env python3
"""Telling a climb's videos from others, and reading climbers' heights and ape indexes. Offline."""
from __future__ import annotations

import pathlib
import sys

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
print("ALL YOUTUBE TESTS PASSED")
