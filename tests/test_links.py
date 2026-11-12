#!/usr/bin/env python3
"""The names in climbs' and boulders' links (backend/links.py): who gets a name plain, what's added where it's taken,
renames kept as aliases, a name never given to another, and deleted meshes kept for their links. Offline, in memory.

    python3 tests/test_links.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import links  # noqa: E402


def names() -> None:
    assert links.slugify("The Mandala") == "the-mandala"
    assert links.slugify("Église du Sacré-Cœur", "V3") == "eglise-du-sacre-coeur-v3", "accents dropped"
    assert links.slugify("  Sit/Stand (V4)!! ") == "sit-stand-v4"
    assert links.slugify("ドラゴン") == "", "nothing left"
    assert len(links.slugify("x" * 200)) == links.LONGEST
    got = list(zip(range(5), links.candidates("Unnamed", ["The Pig Sty", "V2"], "c-x")))
    assert [s for _, s in got] == ["unnamed", "unnamed-the-pig-sty", "unnamed-the-pig-sty-v2", "unnamed-the-pig-sty-v2-2",
                                   "unnamed-the-pig-sty-v2-3"], got
    got = [s for _, s in zip(range(3), links.candidates("ドラゴン", ["Iwaya", None], "c-abc123"))]
    assert got == ["iwaya", "iwaya-2", "iwaya-3"], ("a name with nothing left starts from its boulder's", got)
    got = [s for _, s in zip(range(2), links.candidates("ドラゴン", [], "c-abc123"))]
    assert got == ["c-abc123-2", "c-abc123-3"], got
    for name, grade, want in [("Unnamed V0", "V0+ 4+", "Unnamed V0/+"), ("Unnamed V0", "V0 4", "Unnamed V0"),
                              ("Unnamed v4", "V4 6B", "Unnamed V4"), ("Unnamed V3", "V3- 6A", "Unnamed V3-/3"),
                              ("Unnamed V0", "V1 5", "Unnamed V0/1"), ("Unnamed V1", "V0 4", "Unnamed V0/1"),
                              ("Unnamed V0-", "V0+", "Unnamed V0-/+"), ("Unnamed V-easy", "V0 4", "Unnamed V-easy/0"),
                              ("Unnamed V3", "V2-3 5+", "Unnamed V3 V2-3"), ("V50", "V4 6B", "V50 V4"),
                              ("Big V50", "V4", "Big V50 V4"), ("V1 Left", "V1+ 5", "V1 Left V1+"),
                              ("The Mandala", "V12 8A+", "The Mandala V12"), ("Unnamed V2", None, "Unnamed V2")]:
        assert links.named(name, grade) == want, (name, grade, links.named(name, grade), want)
    assert links.boulder_ref('["CA","Bishop","Drifter"]', "https://www.mountainproject.com/area/105739324/drifter") == "mp:105739324"
    assert links.boulder_ref('["CA","Bishop","Drifter"]', None) == 'key:["CA","Bishop","Drifter"]'
    print("names OK")


def sync() -> None:
    con = sqlite3.connect(":memory:")
    links.init(con)
    climb = lambda ref, name, boulder="B", grade="V1": {"ref": ref, "name": name, "more": [boulder, grade]}  # noqa: E731
    items = [climb("a", "The Mandala", "Mandala Boulder", "V12"), climb("b", "Unnamed", "The Pig Sty", "V2"),
             climb("c", "Unnamed", "The Pig Sty", "V3"), climb("d", "Unnamed", "The Pig Sty", "V3"), climb("e", "Unnamed", "Iceberg")]
    assert links.sync(con, "c", items) == 5
    now = links.current(con, "c")
    assert now == {"a": "the-mandala", "b": "unnamed", "c": "unnamed-the-pig-sty", "d": "unnamed-the-pig-sty-v3",
                   "e": "unnamed-iceberg"}, ("first come gets it plain; then its boulder's, its grade, a number", now)
    assert links.sync(con, "c", items) == 0, "nothing new: nothing given"

    # a later climb of the same name never takes one already given, so old links keep their meaning
    items.append(climb("f", "The Mandala", "Other Rock", "V3"))
    links.sync(con, "c", items)
    assert links.current(con, "c")["f"] == "the-mandala-other-rock" and links.resolve(con, "c", "the-mandala")["ref"] == "a"

    # a rename: a new name, and the old one an alias of it, never given to anyone else
    items[0] = climb("a", "The Mandala Direct", "Mandala Boulder", "V12")
    links.sync(con, "c", items)
    assert links.current(con, "c")["a"] == "the-mandala-direct"
    assert links.resolve(con, "c", "the-mandala") == {"slug": "the-mandala-direct", "ref": "a", "leaf": None}, "an alias"
    items.append(climb("g", "The Mandala", "Mandala Boulder", "V12"))
    links.sync(con, "c", items)
    assert links.current(con, "c")["g"] == "the-mandala-mandala-boulder", "the old name stays the renamed one's"
    # renamed back: its own old name again
    items[0] = climb("a", "The Mandala", "Mandala Boulder", "V12")
    links.sync(con, "c", items)
    assert links.current(con, "c")["a"] == "the-mandala" and links.resolve(con, "c", "the-mandala-direct")["slug"] == "the-mandala"
    assert links.resolve(con, "c", "nobody") is None

    # boulders: their area's name where theirs is taken; a boulder's key follows it when its area is renamed
    bs = [{"ref": "mp:1", "name": "Dike Boulder", "more": ["Hidden Valley", "Joshua Tree"], "leaf": "k1"},
          {"ref": "mp:2", "name": "Dike Boulder", "more": ["Real Hidden Valley", "Joshua Tree"], "leaf": "k2"}]
    links.sync(con, "b", bs)
    assert links.current(con, "b") == {"k1": "dike-boulder", "k2": "dike-boulder-real-hidden-valley"}
    bs[0]["leaf"] = "k1-renamed"
    links.sync(con, "b", bs)
    assert links.resolve(con, "b", "dike-boulder")["leaf"] == "k1-renamed", "the same boulder, at its new key"
    assert links.sync(con, "c", []) == 0 and links.current(con, "c")["a"] == "the-mandala", "climbs and boulders apart"

    # a deleted mesh, for its links
    links.mesh_gone(con, "m1", "Drifter (boulder)", "m2", ["a", "b"], '["CA","Drifter"]')
    assert links.gone(con, "m1") == {"name": "Drifter (boulder)", "successor": "m2", "climbs": ["a", "b"],
                                     "leaf": '["CA","Drifter"]', "deleted": links.gone(con, "m1")["deleted"]}
    assert links.gone(con, "m9") is None
    print("sync OK")


def main() -> None:
    names()
    sync()
    print("ALL LINK TESTS PASSED")


if __name__ == "__main__":
    main()
