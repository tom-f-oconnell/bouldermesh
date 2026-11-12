"""Readable links to climbs and boulders, /c/<name> and /b/<name>, beside /m/<mesh id> for one exact mesh (design.md,
"Links to climbs, boulders and meshes").

Each climb, and each boulder meshed as one, is given the name in its link once, and keeps it: the first with a name
gets it plain, a later one with its boulder's name added (a boulder, its area's), then its grade (a boulder, the area
above), then a number. A name is never given to anything else, so a link never comes to mean something new: a renamed
climb or boulder gets a new name, and its old one stays, as an alias of the new.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from itertools import count
from typing import Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS slugs(
  kind TEXT, slug TEXT, ref TEXT, base TEXT, current INTEGER, leaf TEXT, created REAL, PRIMARY KEY(kind, slug));
CREATE INDEX IF NOT EXISTS slugs_ref ON slugs(kind, ref, current);
CREATE TABLE IF NOT EXISTS meshes_gone(id TEXT PRIMARY KEY, name TEXT, successor TEXT, climbs TEXT, leaf TEXT, deleted REAL);
"""
# slugs: kind "c" (ref, a climb's id) or "b" (ref, "mp:<its Mountain Project area's number>", or "key:<its lowest area's
# key>" without one; leaf, that key as it is now); base, the name it was made from, as slugify() has it, to see a rename;
# current, 0 for an alias. meshes_gone: a mesh deleted, for /m/ links to it: what took its place, its own climbs, and
# the boulder it was a mesh of (its key).
LONGEST = 80
# letters that have no plain form of their own to fall back to, as "é" has "e"
LETTERS = str.maketrans({"æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe", "ß": "ss", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
                         "ł": "l", "Ł": "L", "þ": "th", "Þ": "Th", "ı": "i"})


def slugify(*parts: str | None) -> str:
    """Words as a link has them: accents dropped, lower case, anything else between words a hyphen."""
    text = unicodedata.normalize("NFKD", " ".join(p for p in parts if p).translate(LETTERS)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:LONGEST].strip("-")


def candidates(name: str | None, more: Iterable[str | None], fallback: str) -> Iterator[str]:
    """The names to try, in turn: name's own, then with each of more added, then numbered from 2. A name with nothing
    left of it (one in Japanese) starts from more, and fallback if that's empty too."""
    parts, seen, last = [name], set(), ""
    for extra in [None, *more]:
        if extra is not None:
            parts.append(extra)
        s = slugify(*parts)
        if s and s not in seen:
            seen.add(s)
            last = s
            yield s
    last = last or fallback
    for n in count(2):
        yield f"{last}-{n}"


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def sync(con: sqlite3.Connection, kind: str, items: list[dict]) -> int:
    """Give each item without a name one, and one whose name changed a new one, keeping its old as an alias.

    Args:
        items: {ref, name, more (what to add to its name, in turn, where it's taken), leaf (a boulder's key now)}, in the
            order they were added, which decides who gets a name first.

    Returns:
        How many names it gave.
    """
    rows = con.execute("SELECT slug, ref, base, current, leaf FROM slugs WHERE kind=?", (kind,)).fetchall()
    taken = {r[0]: r[1] for r in rows}
    cur = {r[1]: r for r in rows if r[3]}
    now, given = time.time(), 0
    for it in items:
        base, ref = slugify(it["name"]), it["ref"]
        had = cur.get(ref)
        if had and had[2] == base:
            if it.get("leaf") and had[4] != it["leaf"]:
                con.execute("UPDATE slugs SET leaf=? WHERE kind=? AND slug=?", (it["leaf"], kind, had[0]))
            continue
        slug = next(s for s in candidates(it["name"], it.get("more") or [], kind + "-" + ref[-6:])
                    if taken.get(s, ref) == ref)
        if had:
            con.execute("UPDATE slugs SET current=0 WHERE kind=? AND slug=?", (kind, had[0]))
        con.execute("INSERT OR REPLACE INTO slugs VALUES(?,?,?,?,1,?,?)", (kind, slug, ref, base, it.get("leaf"), now))
        taken[slug] = ref
        cur[ref] = (slug, ref, base, 1, it.get("leaf"))
        given += 1
    return given


def resolve(con: sqlite3.Connection, kind: str, slug: str) -> dict | None:
    """What a link's name is of: {slug (its current name: another for an alias), ref, leaf}; None if it's no one's."""
    row = con.execute("SELECT ref FROM slugs WHERE kind=? AND slug=?", (kind, slug)).fetchone()
    if not row:
        return None
    now = con.execute("SELECT slug, ref, leaf FROM slugs WHERE kind=? AND ref=? AND current=1", (kind, row[0])).fetchone()
    return {"slug": now[0], "ref": now[1], "leaf": now[2]} if now else None


def current(con: sqlite3.Connection, kind: str) -> dict[str, str]:
    """{ref: its current name} for one kind; for boulders, {leaf: name}."""
    rows = con.execute("SELECT ref, slug, leaf FROM slugs WHERE kind=? AND current=1", (kind,)).fetchall()
    return {(r[2] if kind == "b" else r[0]): r[1] for r in rows if (r[2] if kind == "b" else r[0])}


def boulder_ref(leaf: str, url: str | None) -> str:
    """Who a boulder is, across renames: its Mountain Project area's number where its page is known, else its key."""
    m = re.search(r"mountainproject\.com/area/(\d+)", url or "")
    return f"mp:{m[1]}" if m else f"key:{leaf}"


def mesh_gone(con: sqlite3.Connection, mid: str, name: str | None, successor: str | None, climbs: list[str],
              leaf: str | None) -> None:
    """Keep what a deleted mesh's /m/ links should open instead: what took its place, its boulder's, or its climbs'."""
    con.execute("INSERT OR REPLACE INTO meshes_gone VALUES(?,?,?,?,?,?)",
                (mid, name, successor, json.dumps(climbs), leaf, time.time()))


def gone(con: sqlite3.Connection, mid: str) -> dict | None:
    """A deleted mesh, as mesh_gone() kept it: {name, successor, climbs, leaf, deleted}; None if it never was one."""
    r = con.execute("SELECT name, successor, climbs, leaf, deleted FROM meshes_gone WHERE id=?", (mid,)).fetchone()
    return {"name": r[0], "successor": r[1], "climbs": json.loads(r[2] or "[]"), "leaf": r[3], "deleted": r[4]} if r else None
