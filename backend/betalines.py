"""One line for each climb on a mesh, from where the climbers in its videos went: drawn on the rock with the climb's
name, as a guidebook's topo has them.

Finding beta (beta.py) keeps each video's climber's centre as it's cast onto the rock. One video's track wanders: a ray
past the rock's edge, the wrong person followed, a walk in to the start, a fall and another go. The videos of one climb
mostly agree where it goes, though, and a climb's line is where they do:

  1. each video's track is cut into runs: its points on pieces of the mesh that its cleanup hides (meshclean.py: stray
     fragments, far scenery, which a ray past the rock's edge lands on) dropped, its pieces joined where one ends near
     where and when the next starts, or further off but on the way it was going (_joins()), smoothed over time, and
     given points evenly along its length. A run that ends up where it started (someone standing, or going there and
     back) is left out
  2. of the runs of the climb's own videos (those of no other climb on the mesh), the one that other videos keep near
     for longest is what the line starts as. Past the first and last points another video comes near, it's kept only
     while it carries on the way it was going: the rest is one video's wander. A climb with one video, or whose videos
     never agree, gets its run that ends up furthest away. A video that agrees with nothing isn't used, which is most of
     what's off: whole videos (another climb, frames matched to the wrong side of the rock)
  3. each of the line's points is moved to the middle of where the videos near it went, so that no one video's wobble
     shows, and the line is smoothed along its length
  4. it's carried on to the last holds its videos' hands reach past its end (to_the_top()): the climber's middle stops
     an arm's length short of them
  5. it's laid on the mesh, on what of it shows: each point moved onto the surface nearest it, with the surface's
     normal there, which the viewer draws it flat against

A line also says which of its videos' holds are the climb's (holds_by_lines()). A hold further than a reach (REACH_M)
from the line isn't: it's a ray past the rock's edge, or the climber was somewhere else then (walking in, off the
top, on another climb). Those are marked off, which the viewer hides, and the roles of the rest are given again by the
line (roles_by_line()), rather than by wherever each hand was first and last seen still: each hand's first hold on the
line is a start hold, unless the video first shows its climber well up the line (it starts partway: the start isn't in
it), and each hand's hold furthest along the line is its finish, whenever it was held (a hand is held again lower
after a fall, on another go, or walking off). A line checks itself first: if a third of its own videos' holds are
beyond a reach of it (astray()), it and they disagree, and it says nothing of holds.

A line can also be drawn by hand, in the viewer, as points clicked on the rock from the climb's start to its top
(DRAWN_FILE, {climb id: {p, by, at}}). It then is the climb's line, whatever its videos say and whether it has any,
laid on the mesh between its points like the rest, and it speaks for all its climb's videos' holds.

lines.json  {key, lines: {climb id: {p: [[x, y, z]], n: [[x, y, z]], at, videos, agree, astray}}, holds: {video url:
            {off: [hold id], roles: {hold id: role}}}}: p the line's points on the mesh (in its coordinates, as
            beta.json's), n the surface's unit normal at each, at the index of the point its name goes by (the one
            furthest from the mesh's other lines), videos how many of the climb's videos had a run, agree how many of
            those the line keeps near (itself included), astray the share of those videos' holds beyond a reach of
            it, and drawn: true for one drawn by hand (videos and agree 0); holds has the videos with a hold off their climbs' lines or given another role by them (shown()
            applies it to a beta record); key is what it was made from (key())
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FILE = "lines.json"
DRAWN_FILE = "lines_drawn.json"
DRAWN_MOST = 200        # points a line drawn by hand may have
VERSION = 4             # raised when lines would come out differently, so that they're made again
STEP = 0.02             # between a line's points, as a share of the rock's size (as every length here)
SMOOTH_SECONDS = 1.0    # a track is smoothed over about this long either side
CHAIN_SECONDS = 4.0     # pieces of a track this close in time,
CHAIN_JUMP = 0.15       # and this close in place, are one run: the tracker lost the climber for a moment
JOIN_SECONDS = 10.0     # as are pieces up to this long apart,
JOIN_JUMP = 0.3         # and this far, where the next is on the way the track was going (TURN) and goes on that way
JOIN_BACK = 3.0         # the way a track was going where it breaks: over its last seconds
MIN_RUN = 0.12          # a run that ends up less than this from where it started isn't a climb
NEAR = 0.08             # videos within this of each other agree
AGREE = 0.25            # a line is cut back towards where videos agree only if they do over this share of it
AHEAD = 0.25            # the way a line is going at a point: from this far back along it
TURN = 0.5              # past where videos agree, a run is kept while it goes on within this cosine of the line's way
DOWN = 0.3              # a run that mostly goes down counts this much: climbs go up, or along
HOOK = 0.15             # an end that doubles back for less than this share of the line is cut off
SMOOTH = 0.04           # a line is smoothed along its length over about this
ROUNDS = 2              # times a line's points are moved to the middle of the videos near them
PATCH = 0.025           # the surface a point is laid on is fitted to the mesh's vertices this near it
LABEL = (0.3, 0.8)      # a line's name goes by a point this far along it
REACH_M = 1.75          # metres: a hold further than this from its climb's line isn't one of the climb's
REACH = 0.3             # the same where a mesh's size in metres isn't known, as a share of the rock's size
REACH_BOUNDS = (0.08, 0.5)      # and the least and most it is, as shares of the rock's size, whatever the scale found says
ASTRAY = 1 / 3          # a line this share of whose own videos' holds are beyond a reach of it says nothing of holds
PARTWAY = 1 / 3         # a video that first shows its climber further than this along the line has no start holds
LEVEL = 0.06            # holds within this share of a reach of a hand's furthest along the line are as far: the last held is its finish


# ------------------------------------------------------------------ runs
def _smoothed(x, P, width: float):
    """Points P, each at x (a time, or how far along a path), smoothed over about `width` of x either side: each is
    where a straight line fitted to those near it (a Gaussian's weights) puts it, rather than their average, which
    would pull a path's ends in, having points on one side only."""
    import numpy as np
    dx = x[None, :] - x[:, None]
    w = np.exp(-0.5 * (dx / width) ** 2)
    s0, s1, s2 = w.sum(axis=1), (w * dx).sum(axis=1), (w * dx * dx).sum(axis=1)
    t0, t1 = w @ P, (w * dx) @ P
    det = s0 * s2 - s1 * s1
    flat = det < 1e-9 * np.maximum(s0 * s2, 1e-300)      # all its weight at one x: the average
    det = np.where(flat, 1.0, det)
    return np.where(flat[:, None], t0 / s0[:, None], (s2[:, None] * t0 - s1[:, None] * t1) / det[:, None])


def _lengths(P):
    """How far along a path each of its points is."""
    import numpy as np
    return np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(P, axis=0), axis=1))]


def _resampled(P, step: float):
    """A path with its points `step` apart along it (as near as divides it evenly), ends kept."""
    import numpy as np
    s = _lengths(P)
    n = max(2, int(round(s[-1] / step)) + 1)
    at = np.linspace(0.0, s[-1], n)
    return np.stack([np.interp(at, s, P[:, k]) for k in range(3)], axis=1)


def _thinned(P, step: float):
    """A path without the points less than step from the last one kept: where a climber stays put, their track's
    wobble would otherwise count as going somewhere. Its last point is kept."""
    import numpy as np
    keep = [0]
    for i in range(1, len(P)):
        if np.linalg.norm(P[i] - P[keep[-1]]) >= step:
            keep.append(i)
    if keep[-1] != len(P) - 1:
        keep.append(len(P) - 1)
    return P[keep]


def _reach(P) -> float:
    """How far a path gets from where it starts, or ends: the most of either."""
    import numpy as np
    return float(max(np.linalg.norm(P - P[0], axis=1).max(), np.linalg.norm(P - P[-1], axis=1).max()))


def _went(P) -> float:
    """How far a run ends up from where it starts: less (DOWN) if that's mostly down."""
    import numpy as np
    d = P[-1] - P[0]
    far = float(np.linalg.norm(d))
    return far * (DOWN if d[1] < -0.5 * far else 1.0)


def _joins(chain: list, piece: list, size: float) -> bool:
    """Whether a piece of a track carries on the stretch before it ([[t, x, y, z]], both): it starts close after in
    time and place (the tracker lost the climber for a moment), or further off (JOIN_SECONDS, JOIN_JUMP) but on the
    way the track was going, and itself goes on that way. A climber lost for some seconds on the way up is found
    again further up; after a fall, or setting off down, they're found the other way."""
    import numpy as np
    last, first = np.asarray(chain[-1], float), np.asarray(piece[0], float)
    wait, jump = first[0] - last[0], first[1:] - last[1:]
    far = float(np.linalg.norm(jump))
    if wait <= CHAIN_SECONDS and far <= CHAIN_JUMP * size:
        return True
    if wait > JOIN_SECONDS or far > JOIN_JUMP * size:
        return False
    back = next((r for r in reversed(chain) if last[0] - r[0] >= JOIN_BACK), chain[0])
    way = last[1:] - np.asarray(back[1:], float)
    on = np.asarray(piece[-1][1:], float) - first[1:]
    if np.linalg.norm(way) < STEP * size or np.linalg.norm(on) < STEP * size:      # no saying which way either goes
        return False
    cos = lambda a, b: float(a @ b) / max(float(np.linalg.norm(a) * np.linalg.norm(b)), 1e-12)  # noqa: E731
    return cos(on, way) >= TURN and (far < STEP * size or cos(jump, way) >= TURN)


def top_holds(P, videos: list[dict], far: float) -> list:
    """Where a line's videos' hands go on past its last point: for each video with any, the middle of its hand
    holds that are nearest the line at its end, ahead of it the way it's going, and within `far` (a reach) of it.

    Args:
        P: the line's points, in the order climbed.
        videos: those the line keeps near, each with its holds ([{p, contacts}]).
    """
    import numpy as np
    import beta
    S = _lengths(P)
    way = P[-1] - P[max(0, len(P) - 1 - max(1, int(round(AHEAD / STEP / 2))))]
    way = way / max(float(np.linalg.norm(way)), 1e-12)
    out = []
    for v in videos:
        past = []
        for h in v.get("holds") or []:
            p = np.asarray(h["p"], float)
            if any(beta.LIMBS[c["limb"]][0] == "hand" for c in h["contacts"]) and (p - P[-1]) @ way > 0 \
                    and np.linalg.norm(p - P[-1]) <= far and _along(p, P, S) >= S[-1] - 1e-9:
                past.append(p)
        if past:
            out.append(np.mean(past, axis=0))
    return out


def to_the_top(P, videos: list[dict], far: float, step: float):
    """A line carried on to where its videos' hands go past its end. A line is where the climber's middle went, which
    stops an arm's length short of the last holds, and sooner where the camera loses them at the top. Carried on to
    the middle of top_holds(), where two of its videos have some (or its only one does).

    Returns:
        The line, with points on to there (`step` apart) if it's carried on.
    """
    import numpy as np
    tops = top_holds(P, videos, far)
    if len(tops) < min(2, len(videos)) or not tops:
        return P
    end = np.mean(tops, axis=0)
    n = int(np.linalg.norm(end - P[-1]) / step)
    if n < 1:
        return P
    return np.vstack([P, [P[-1] + (end - P[-1]) * k / n for k in range(1, n + 1)]])


def runs(centre: list, size: float) -> list:
    """A video's climber's way over the rock, as runs.

    Args:
        centre: its centre track, in pieces (beta.centre_track()): [[[t, x, y, z]]].
        size: the rock's size (beta.rock_size()).

    Returns:
        [points, an array n x 3], in time order: each a stretch the climber was followed through, smoothed, its points
        STEP apart; those that end up nowhere (MIN_RUN) left out.
    """
    import numpy as np
    chains: list[list] = []
    for piece in sorted((p for p in centre or [] if len(p) >= 2), key=lambda p: p[0][0]):
        if chains and _joins(chains[-1], piece, size):
            chains[-1] = chains[-1] + list(piece)
        else:
            chains.append(list(piece))
    out = []
    for chain in chains:
        a = np.asarray(chain, float)
        if len(a) < 3:
            continue
        P = _resampled(_thinned(_smoothed(a[:, 0], a[:, 1:], SMOOTH_SECONDS), STEP * size / 2), STEP * size)
        if np.linalg.norm(P[-1] - P[0]) >= MIN_RUN * size:
            out.append(P)
    return out


# ------------------------------------------------------------------ a climb's line
def _near(P, Q, radius: float):
    """For each of P's points, whether any of Q's is within radius."""
    import numpy as np
    if not len(Q):
        return np.zeros(len(P), bool)
    d2 = ((P[:, None, :] - Q[None, :, :]) ** 2).sum(axis=2)
    return d2.min(axis=1) <= radius * radius


def _carries_on(P, way) -> int:
    """How many of a run's points past P[0], the last one other videos come near, to keep: those that carry on the
    way the line was going there (within TURN of it, as the crow flies from P[0]). A climber seen for longer in one
    video goes on up the climb; one walking in to its start comes from the side."""
    import numpy as np
    way = np.asarray(way, float)
    length = np.linalg.norm(way)
    if length < 1e-12:
        return 0
    way = way / length
    keep = 0
    for j in range(1, len(P)):
        chord = P[j] - P[0]
        if chord @ way >= TURN * np.linalg.norm(chord):
            keep = j
        elif j >= 3:
            break
    return keep


def _unhooked(P):
    """A line without a short end that doubles back: cut at the point furthest from its other end, where what's past
    it is under HOOK of the line. A track often turns back on itself where the climber stops."""
    import numpy as np
    for _ in range(2):      # its last end, then (turned round) its first
        far = int(np.argmax(np.linalg.norm(P - P[0], axis=1)))
        s = _lengths(P)
        if far < len(P) - 1 and far >= 2 and s[-1] - s[far] < HOOK * s[-1]:
            P = P[:far + 1]
        P = P[::-1]
    return P


def _smooth_along(P, width: float):
    """A path smoothed along its length, over about `width` either side."""
    return _smoothed(_lengths(P), P, width)


def climb_line(own: list[list], others: list[list], size: float) -> dict | None:
    """Where a climb goes, from its videos' runs.

    Args:
        own: each of the climb's own videos' runs (runs()): those of videos of no other climb on the mesh.
        others: the same for its videos that are also of other climbs there: they can agree with a line, not start one.
        size: the rock's size.

    Returns:
        {points: n x 3, videos, agree, near}, or None with no run to go by: videos is how many had a run, agree how
        many of them the line keeps near (itself included), and near which: indices into own followed by others, as
        given.
    """
    import numpy as np
    given = [k for k, v in enumerate(list(own) + list(others)) if v]     # where each video with a run was in those given
    own = [v for v in own if v]
    others = [v for v in others if v]
    if not own:
        return None
    clouds = [np.vstack(v) for v in own + others]       # each video's points, whichever run they're of
    R, step = NEAR * size, STEP * size
    best = None     # (score, video, run, which of its points other videos come near)
    for i, video in enumerate(own):
        rest = [c for j, c in enumerate(clouds) if j != i]
        for run in video:
            seen = np.zeros(len(run), int)
            for c in rest:
                seen += _near(run, c, R)
            score = (int((seen > 0).sum()), int(seen.sum()), _went(run))
            if best is None or score > best[0]:
                best = (score, i, run, seen > 0)
    _, i, line, agreed = best
    if not agreed.any() and len(clouds) > 1:    # no two of its videos agree: the one that ends up furthest
        _, i, line = max(((_went(run), j, run) for j, v in enumerate(own) for run in v), key=lambda x: x[0])
        agreed = np.zeros(len(line), bool)
    if agreed.mean() >= AGREE:
        whole, on = line, np.flatnonzero(agreed)
        a, b = int(on[0]), int(on[-1])
        back = max(1, int(round(AHEAD / STEP)))
        a -= _carries_on(whole[a::-1], whole[a] - whole[min(b, a + back)])
        b += _carries_on(whole[b:], whole[b] - whole[max(a, b - back)])
        line = whole[a:b + 1]
        if len(line) < 3 or _reach(line) < MIN_RUN * size:
            line = whole
    rest = [c for j, c in enumerate(clouds) if j != i]
    near = [j for j, c in enumerate(clouds) if j == i or _near(line, c, R).mean() >= AGREE]
    for _ in range(ROUNDS if rest else 0):
        moved = line.copy()
        for k, p in enumerate(line):
            middles = [p]
            for c in rest:
                d = np.linalg.norm(c - p, axis=1)
                close = d < R
                if close.any():
                    w = np.exp(-0.5 * (d[close] / (R / 2)) ** 2)
                    middles.append((w[:, None] * c[close]).sum(axis=0) / w.sum())
            moved[k] = np.mean(middles, axis=0)
        line = _resampled(_smooth_along(moved, SMOOTH * size), step)
    line = _resampled(_smooth_along(_unhooked(line), SMOOTH * size), step)
    if len(line) < 3 or _reach(line) < MIN_RUN * size:
        return None
    return {"points": line, "videos": len(clouds), "agree": len(near), "near": [given[j] for j in near]}


def lines(videos: list[dict], of: dict[str, list[str]], size: float) -> dict[str, dict]:
    """A line for each of a mesh's climbs that its videos give one.

    Args:
        videos: beta.json's, each with its url and centre track.
        of: each climb's videos' urls, by its id: every climb on the mesh.
        size: the rock's size.

    Returns:
        {climb id: {points, videos, agree, near}}, as climb_line() gives them, near being the urls of the videos the
        line keeps near; for the climbs with a line.
    """
    climbs_of: dict[str, set] = {}
    for cid, urls in of.items():
        for u in urls:
            climbs_of.setdefault(u, set()).add(cid)
    ran = {v["url"]: runs(v.get("centre") or [], size) for v in videos if v.get("url") in climbs_of}
    out = {}
    for cid, urls in of.items():
        mine = [u for u in dict.fromkeys(urls) if ran.get(u)]
        own, others = [u for u in mine if len(climbs_of[u]) == 1], [u for u in mine if len(climbs_of[u]) > 1]
        one = climb_line([ran[u] for u in own], [ran[u] for u in others], size)
        if one:
            out[cid] = {**one, "near": [(own + others)[k] for k in one["near"]]}
    return out


# ------------------------------------------------------------------ holds by the lines
def reach(size: float, metres_per_unit: float | None = None) -> float:
    """How far from its climb's line a hold can be and still be the climb's, in the mesh's units: REACH_M where its
    scale is known (scale.py), kept within REACH_BOUNDS of the rock's size, as a scale found is sometimes far out; else
    REACH of the rock's size."""
    if not metres_per_unit or metres_per_unit <= 0:
        return REACH * size
    return min(max(REACH_M / metres_per_unit, REACH_BOUNDS[0] * size), REACH_BOUNDS[1] * size)


def _from_line(p, P) -> float:
    """How far a point is from a line (its points P, joined)."""
    import numpy as np
    p, A, B = np.asarray(p, float), P[:-1], P[1:]
    AB = B - A
    t = np.clip(((p - A) * AB).sum(axis=1) / np.maximum((AB * AB).sum(axis=1), 1e-18), 0.0, 1.0)
    return float(np.linalg.norm(A + t[:, None] * AB - p, axis=1).min())


def _along(p, P, S) -> float:
    """How far along a line (its points P, joined, each S along it) the point of it nearest p is."""
    import numpy as np
    p, A, B = np.asarray(p, float), P[:-1], P[1:]
    AB = B - A
    t = np.clip(((p - A) * AB).sum(axis=1) / np.maximum((AB * AB).sum(axis=1), 1e-18), 0.0, 1.0)
    i = int(np.argmin(np.linalg.norm(A + t[:, None] * AB - p, axis=1)))
    return float(S[i] + t[i] * (S[i + 1] - S[i]))


def partway(centre: list, P, near: float) -> bool:
    """Whether a video first shows its climber well along a line (over PARTWAY of it): where its centre track
    (in pieces) is first within `near` of the line, over its first four points there."""
    import numpy as np
    S = _lengths(P)
    first = []
    for row in sorted((r for piece in centre or [] for r in piece), key=lambda r: r[0]):
        if _from_line(row[1:], P) <= near:
            first.append(_along(row[1:], P, S))
            if len(first) == 4:
                break
    return bool(first) and float(np.median(first)) > PARTWAY * S[-1]


def roles_by_line(holds: list[dict], P, far: float, started_partway: bool = False) -> list[str]:
    """Each hold's role by its climb's line, for a video's holds within a reach of it.

    As beta.roles(), but for what a line can tell that when a hold was held can't. A hand's finish hold is the one
    furthest along the line, not the last it held: of those within LEVEL of a reach of the furthest, the last held.
    And a video that starts partway up has no start holds: each hand's first is then a hand hold like any other.

    Args:
        holds: [{p, contacts: [{limb, t0, t1}]}].
        P: the line's points (n x 3).
        far: a reach (reach()).
        started_partway: partway() of the video's centre track.

    Returns:
        A role for each hold, in order.
    """
    import beta
    S = _lengths(P)
    hands = [(c["t0"], c["t1"], c["limb"], i) for i, h in enumerate(holds) for c in h["contacts"] if beta.LIMBS[c["limb"]][0] == "hand"]
    along = {i: _along(holds[i]["p"], P, S) for i in {x[3] for x in hands}}
    starts, finishes = set(), set()
    for limb in ("lh", "rh"):
        mine = [x for x in hands if x[2] == limb]
        if not mine:
            continue
        if not started_partway:
            starts.add(min(mine)[3])
        top = max(along[x[3]] for x in mine)
        finishes.add(max((x for x in mine if along[x[3]] >= top - LEVEL * far), key=lambda x: x[1])[3])
    handed = {x[3] for x in hands}
    return ["start" if i in starts else "finish" if i in finishes else "hand" if i in handed else "foot" for i in range(len(holds))]


def astray(videos: list[dict], laid: dict[str, dict], far: float) -> dict[str, float]:
    """For each line, the share of the holds of the videos it keeps near that are further than `far` from it. Mostly
    a few in a hundred. Where it's a third or more, the line and its own videos' holds disagree: it stops short of the
    climb's top, or is somewhere the climb isn't, or the holds are mostly stray; there's no telling which from here.

    Args:
        videos, laid, far: as holds_by_lines().

    Returns:
        {climb id: share}, 0 for a line whose videos have no holds.
    """
    import numpy as np
    holds = {v.get("url"): v.get("holds") or [] for v in videos}
    out = {}
    for cid, one in laid.items():
        P = np.asarray(one["p"], float)
        off = [_from_line(h["p"], P) > far for u in one["near"] for h in holds.get(u, ())]
        out[cid] = float(np.mean(off)) if off else 0.0
    return out


def holds_by_lines(videos: list[dict], of: dict[str, list[str]], laid: dict[str, dict], far: float, near: float = 0.0) -> dict[str, dict]:
    """Which of each video's holds its climbs' lines leave out, and the roles of those they keep.

    A line speaks for a video's holds if it's agreed by two videos or more, or it came from that video; a line from
    one video alone says nothing of the climb's other videos, which may be the ones that are right. Nor does a line
    speak for any if too many (ASTRAY) of its own videos' holds are beyond a reach of it (astray()): then the line
    itself is in doubt. A hold is off if it's further than `far` from every line that speaks for its video. The rest
    are given roles again, as if the holds that are off had never been found: by the line (roles_by_line()) where one
    line speaks for the video, and by when each was held alone (beta.roles()) where two do, as a video of two climbs
    has two starts and two finishes, and there's no telling which hold is of which.

    Args:
        videos: beta.json's, each with its url, holds ([{id, role, p, contacts}]) and centre track.
        of: each climb's videos' urls, by its id.
        laid: each climb's line, {p: its points (n x 3), near: the urls of the videos it keeps near, agree}.
        far: reach().
        near: how near a line a climber's centre is on it (NEAR of the rock's size), for partway().

    Returns:
        {url: {off: [hold id], roles: {hold id (as text, as JSON keeps it): role}}}: the videos with a hold off, or one
        whose role is no longer the one found; roles has only those that changed.
    """
    import numpy as np
    import beta
    doubt = astray(videos, laid, far)
    sure = {cid: one for cid, one in laid.items() if doubt[cid] < ASTRAY}
    out = {}
    for v in videos:
        u, holds = v.get("url"), v.get("holds") or []
        by = [np.asarray(one["p"], float) for cid, one in sure.items() if u in of.get(cid, ()) and (one["agree"] >= 2 or u in one["near"])]
        if not holds or not by:
            continue
        off = [h["id"] for h in holds if min(_from_line(h["p"], P) for P in by) > far]
        kept = [h for h in holds if h["id"] not in off]
        if len(by) == 1:
            given = roles_by_line(kept, by[0], far, partway(v.get("centre") or [], by[0], near))
        else:
            given = beta.roles([h["contacts"] for h in kept])
        changed = {str(h["id"]): role for h, role in zip(kept, given) if role != h.get("role")}
        if off or changed:
            out[u] = {"off": off, "roles": changed}
    return out


def shown(record: dict | None, kept: dict | None) -> dict | None:
    """A mesh's beta as it's shown: its record (changed in place, and returned) with each hold its lines leave out
    marked off, and each they give another role given it (its own kept as found_role).

    Args:
        record: beta.json's.
        kept: lines.json's, made from that record (the caller's to check: key()); None changes nothing.
    """
    changes = (kept or {}).get("holds") or {}
    for v in (record or {}).get("videos") or []:
        one = changes.get(v.get("url"))
        if not one:
            continue
        off, given = set(one.get("off") or []), one.get("roles") or {}
        for h in v.get("holds") or []:
            if h.get("id") in off:
                h["off"] = True
            elif str(h.get("id")) in given:
                h["found_role"], h["role"] = h.get("role"), given[str(h["id"])]
    return record


# ------------------------------------------------------------------ on the mesh
class Surface:
    """The vertices of a mesh's faces, filed in a grid so that those near a point are quick to find, and their normals
    where they're asked for."""

    def __init__(self, V, F, cell: float):
        """F: the faces to go by (those that show); cell: the grid's, about as far as is usually looked from a point."""
        import numpy as np
        V = np.asarray(V, np.float32)
        used = np.zeros(len(V), bool)
        used[F.ravel()] = True
        self.V = V = V[used]
        self._F, self._index = F, np.cumsum(used) - 1      # its faces, by the vertices as numbered here
        self.N, self._box = None, None
        self.cell, self.low = float(cell), V.min(axis=0).astype(float)
        ijk = np.floor((V - self.low) / self.cell).astype(np.int64)
        self.dims = ijk.max(axis=0) + 1
        keys = (ijk[:, 0] * self.dims[1] + ijk[:, 1]) * self.dims[2] + ijk[:, 2]     # a column's cells (z) are in a row
        self.order = np.argsort(keys, kind="stable")
        self.keys = keys[self.order]

    def normals_in(self, lo, hi) -> None:
        """Work out the normals (N) of the vertices in the box from lo to hi: each its triangles' normals together, the
        bigger counting more, and not unit. The rest have none: most of a mesh is nowhere near its lines."""
        import numpy as np
        V = self.V
        self._box = (np.asarray(lo, float), np.asarray(hi, float))
        inside = ((V >= np.asarray(lo, np.float32)) & (V <= np.asarray(hi, np.float32))).all(axis=1)
        F = self._index[self._F]
        F = F[inside[F].any(axis=1)]
        a = V[F[:, 0]]
        fn = np.cross(V[F[:, 1]] - a, V[F[:, 2]] - a)
        self.N = np.zeros((len(V), 3), np.float32)
        for corner in range(3):
            for k in range(3):
                self.N[:, k] += np.bincount(F[:, corner], fn[:, k], len(V)).astype(np.float32)

    def within(self, p, h: float):
        """The vertices in the cube reaching h either way from p: their indices. None if the cube is too many of the
        grid's cells to be worth going through one by one."""
        import numpy as np
        p = np.asarray(p, float)
        lo = np.maximum(np.floor((p - h - self.low) / self.cell).astype(np.int64), 0)
        hi = np.minimum(np.floor((p + h - self.low) / self.cell).astype(np.int64), self.dims - 1)
        if (hi < lo).any():
            return np.zeros(0, np.int64)
        if (hi[0] - lo[0] + 1) * (hi[1] - lo[1] + 1) > 400:
            return None
        ix, iy = np.meshgrid(np.arange(lo[0], hi[0] + 1), np.arange(lo[1], hi[1] + 1), indexing="ij")
        column = (ix.ravel() * self.dims[1] + iy.ravel()) * self.dims[2]
        starts, ends = np.searchsorted(self.keys, column + lo[2], "left"), np.searchsorted(self.keys, column + hi[2], "right")
        some = np.concatenate([self.order[a:b] for a, b in zip(starts, ends)])
        return some[(np.abs(self.V[some] - p) <= h).all(axis=1)]

    def nearest(self, p, h: float) -> int:
        """The vertex nearest p: looked for within h of it, then as much further as it takes."""
        import numpy as np
        while True:
            some = self.within(p, h)
            if some is None:
                return int(np.argmin(((self.V - np.asarray(p, np.float32)) ** 2).sum(axis=1)))
            if some.size:
                d = np.linalg.norm(self.V[some] - p, axis=1)
                if d.min() <= h:    # none outside the cube can be nearer
                    return int(some[int(np.argmin(d))])
                h = float(d.min()) * 1.0001
            else:
                h *= 2

    def near(self, P):
        """For each of the points P, whether any vertex is in its cell of the grid or one beside it: so within a cell
        or two of it."""
        import numpy as np
        P = np.asarray(P, float).reshape(-1, 3)
        ijk = np.floor((P - self.low) / self.cell).astype(np.int64)
        out = np.zeros(len(P), bool)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    c = ijk + (dx, dy, dz)
                    inside = ((c >= 0) & (c < self.dims)).all(axis=1)
                    keys = (c[:, 0] * self.dims[1] + c[:, 1]) * self.dims[2] + c[:, 2]
                    at = np.minimum(np.searchsorted(self.keys, keys), len(self.keys) - 1)
                    out |= inside & (self.keys[at] == keys)
        return out

    def drape(self, P, size: float):
        """A line's points laid on the mesh.

        Args:
            P: its points, near the surface.
            size: the rock's size.

        Returns:
            (points, normals): each point moved onto the flat patch fitted to the vertices within PATCH of the vertex
            nearest it, with that patch's unit normal; a point whose patch has none takes its neighbour's.
        """
        import numpy as np
        P = np.asarray(P, float)
        r = PATCH * size
        lo, hi = P.min(axis=0) - 4 * r, P.max(axis=0) + 4 * r
        if self._box is None:
            self.normals_in(lo, hi)
        elif (lo < self._box[0]).any() or (hi > self._box[1]).any():     # further than the normals worked out so far
            self.normals_in(np.minimum(lo, self._box[0]), np.maximum(hi, self._box[1]))
        out, normals = P.copy(), np.zeros_like(P)
        for i, p in enumerate(P):
            nearest = self.V[self.nearest(p, r / 4)].astype(float)
            some = self.within(nearest, r)
            patch = some[((self.V[some] - nearest) ** 2).sum(axis=1) <= r * r]
            n = self.N[patch].astype(float).sum(axis=0)
            length = np.linalg.norm(n)
            if length < 1e-12:
                out[i] = nearest
                continue
            n /= length
            out[i] = p - ((p - self.V[patch].astype(float).mean(axis=0)) @ n) * n
            normals[i] = n
        known = np.flatnonzero(np.linalg.norm(normals, axis=1) > 0)
        for i in np.flatnonzero(np.linalg.norm(normals, axis=1) == 0):
            normals[i] = normals[known[np.argmin(np.abs(known - i))]] if known.size else (0.0, 1.0, 0.0)
        return out, normals


def label_at(P, others: list) -> int:
    """The index of the point a line's name goes by: of those LABEL along it, the one furthest from the other lines,
    so that the names of lines close together sit apart; the middle one with no other lines."""
    import numpy as np
    n = len(P)
    a = int(LABEL[0] * (n - 1))
    b = max(int(LABEL[1] * (n - 1)), a)
    if not others:
        return (a + b) // 2
    Q = np.vstack(others)
    d2 = ((P[a:b + 1, None, :] - Q[None, :, :]) ** 2).sum(axis=2).min(axis=1)
    return a + int(np.argmax(d2))


# ------------------------------------------------------------------ a mesh's
def shown_faces(mesh_dir: Path, faces: int):
    """Which of a mesh's faces show: not those of the pieces its cleanup hides (meshclean.py). None if they all do, or
    its cleanup isn't of the mesh as it's read here (`faces` of them, in its GLB's order)."""
    import numpy as np
    import meshclean
    record = meshclean.load(mesh_dir)
    if not record or record.get("deleted") or not record.get("removed") or record.get("faces") != faces:
        return None
    try:
        labels = meshclean._read_labels(mesh_dir, record)
    except (OSError, KeyError, ValueError):
        return None
    if len(labels) != faces:
        return None
    keep = np.ones(len(record["pieces"]), bool)
    keep[record["removed"]] = False
    return keep[labels]


def shown_stamp(mesh_dir: Path) -> str:
    """What says a mesh's cleanup is as it was: when it was last written, and how long it is."""
    import meshclean
    try:
        st = (mesh_dir / meshclean.JSON_FILE).stat()
    except OSError:
        return ""
    return f"{st.st_mtime_ns}:{st.st_size}"


def on_rock(centre: list, surface: Surface) -> list:
    """A centre track without its points away from the surface (of the mesh's faces that show: Surface.near()): on a
    piece that's hidden. Its pieces are cut where points go."""
    import numpy as np
    out = []
    for piece in centre or []:
        if not piece:
            continue
        run: list = []
        for row, near in zip(piece, surface.near(np.asarray(piece, float)[:, 1:])):
            if near:
                run.append(row)
            else:
                out.append(run)
                run = []
        out.append(run)
    return [r for r in out if len(r) >= 2]


def load_drawn(mesh_dir: Path) -> dict[str, dict]:
    """The lines drawn by hand on a mesh: {climb id: {p: [[x, y, z]], by, at}}."""
    try:
        got = json.loads((mesh_dir / DRAWN_FILE).read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def draw(mesh_dir: Path, climb: str, points, by: str | None = None, now: float | None = None) -> dict[str, dict]:
    """Keep a climb's line as drawn by hand, in place of any before; with no points, forget it, and its videos'
    line is the climb's again.

    Args:
        points: [[x, y, z]] on the mesh, in its coordinates, from the climb's start to its top: 2 to DRAWN_MOST.
        by: who drew it.

    Returns:
        load_drawn(), after.

    Raises:
        ValueError: they aren't such points.
    """
    import math
    import time
    drawn = load_drawn(mesh_dir)
    if points:
        ok = isinstance(points, list) and 2 <= len(points) <= DRAWN_MOST and all(
            isinstance(p, list) and len(p) == 3 and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in p)
            for p in points)
        if not ok:
            raise ValueError(f"a line is 2 to {DRAWN_MOST} points on the mesh, each [x, y, z]")
        drawn[climb] = {"p": [[round(float(x), 5) for x in p] for p in points], "by": by, "at": time.time() if now is None else now}
    else:
        drawn.pop(climb, None)
    tmp = mesh_dir / (DRAWN_FILE + ".tmp")
    tmp.write_text(json.dumps(drawn))
    tmp.replace(mesh_dir / DRAWN_FILE)
    return drawn


def drawn_stamp(mesh_dir: Path) -> str:
    """What says a mesh's hand-drawn lines are the same ones: their points, by climb."""
    drawn = load_drawn(mesh_dir)
    return hashlib.sha1(json.dumps(sorted((c, one.get("p")) for c, one in drawn.items())).encode()).hexdigest()[:12] if drawn else ""


def tracks(record: dict | None) -> str:
    """What says a mesh's beta's centre tracks and holds are the same ones: not when beta.json was written, which is
    every few seconds while beta is found."""
    said = hashlib.sha1()
    for v in sorted((record or {}).get("videos") or [], key=lambda v: str(v.get("url"))):
        said.update(json.dumps([v.get("url"), v.get("centre"), v.get("holds")]).encode())
    return said.hexdigest()


def key(tracked: str, of: dict[str, list[str]], shown: str = "", metres_per_unit: float | None = None, drawn: str = "") -> str:
    """What a mesh's lines are made from, to tell when they need making again: its videos' centre tracks and holds
    (tracks()), which videos are of which climb, which of its pieces show (shown_stamp()), its scale (a hold's reach
    is in metres), the lines drawn on it by hand (drawn_stamp()), and VERSION."""
    scale = round(float(metres_per_unit), 4) if metres_per_unit else None
    said = json.dumps([VERSION, tracked, sorted((c, sorted(set(u))) for c, u in of.items()), shown, scale, drawn])
    return hashlib.sha1(said.encode()).hexdigest()[:16]


def load(mesh_dir: Path) -> dict | None:
    try:
        return json.loads((mesh_dir / FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def make(mesh_dir: Path, of: dict[str, list[str]], record: dict | None = None, metres_per_unit: float | None = None) -> dict:
    """Make a mesh's lines, and what they say of its videos' holds, and keep them (FILE). The mesh is only read if its
    beta has tracks; if it can't be, it has no lines.

    Args:
        of: each climb's videos' urls, by its id: every climb on the mesh.
        record: its beta.json, if it's been read already.
        metres_per_unit: its scale, if it's known (reach()).

    Returns:
        The record kept: {key, lines, holds}.
    """
    import numpy as np
    import beta
    record = record if record is not None else beta.load(mesh_dir) or {}
    made_from = key(tracks(record), of, shown_stamp(mesh_dir), metres_per_unit, drawn_stamp(mesh_dir))     # before the mesh is read: a change meanwhile makes them again
    drawn = {cid: one for cid, one in load_drawn(mesh_dir).items() if cid in of and len(one.get("p") or []) >= 2}
    out, changes = {}, {}
    wanted = {u for urls in of.values() for u in urls}
    videos = [v for v in record.get("videos") or [] if v.get("url") in wanted and v.get("centre")]
    try:
        V, F = beta.geometry(mesh_dir) if videos or drawn else (None, None)
    except ValueError:      # no mesh here that can be read: no lines, rather than trying again each time they're asked for
        V = None
    if V is not None:
        size = beta.rock_size(V)
        shown = shown_faces(mesh_dir, len(F))
        surface = Surface(V, F if shown is None else F[shown], PATCH * size)
        if shown is not None:
            videos = [{**v, "centre": on_rock(v["centre"], surface)} for v in videos]
        far = reach(size, metres_per_unit)
        found = {cid: one for cid, one in lines(videos, of, size).items() if cid not in drawn}
        by_url = {v.get("url"): v for v in record.get("videos") or []}
        for one in found.values():      # each carried on to its videos' last holds
            one["points"] = to_the_top(one["points"], [by_url[u] for u in one["near"] if u in by_url], far, STEP * size)
        for cid, one in drawn.items():      # as drawn, which speaks for every video of its climb
            found[cid] = {"points": _resampled(np.asarray(one["p"], float), STEP * size), "videos": 0, "agree": 0, "near": [], "drawn": True}
        if found:
            every = np.vstack([one["points"] for one in found.values()])
            surface.normals_in(every.min(axis=0) - 4 * PATCH * size, every.max(axis=0) + 4 * PATCH * size)
        laid = {cid: surface.drape(one["points"], size) for cid, one in found.items()}
        for cid, one in found.items():
            P, normals = laid[cid]
            out[cid] = {"p": [[round(float(x), 5) for x in p] for p in P], "n": [[round(float(x), 3) for x in n] for n in normals],
                        "at": label_at(P, [laid[c][0] for c in laid if c != cid]), "videos": one["videos"], "agree": one["agree"],
                        **({"drawn": True} if one.get("drawn") else {})}
        by = {cid: {**found[cid], "p": laid[cid][0], **({"agree": 2} if found[cid].get("drawn") else {})} for cid in found}
        changes = holds_by_lines(record.get("videos") or [], of, by, far, NEAR * size)
        for cid, share in astray(record.get("videos") or [], by, far).items():
            out[cid]["astray"] = round(share, 3)
    kept = {"key": made_from, "lines": out, "holds": changes}
    tmp = mesh_dir / (FILE + ".tmp")
    tmp.write_text(json.dumps(kept))
    tmp.replace(mesh_dir / FILE)
    return kept
