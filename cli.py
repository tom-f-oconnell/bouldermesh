#!/usr/bin/env python3
"""Command-line access to the climb adapters and photo harvester.

  python cli.py fetch  <url> [--no-children]          metadata for an MP / OpenBeta / Kaya URL -> JSON
  python cli.py search "<area name>" [--near LAT LNG --radius M]   OpenBeta search -> JSON
  python cli.py mp-csv <mp_area_id> [--type boulder]  Mountain Project route-finder CSV export -> JSON
  python cli.py harvest <url> <out_dir> [--sources climb,google,ddg,commons,flickr,youtube] [--no-filter]
                                                       download public photos + relevance + diversity reports
  python cli.py diversity <photos_dir>                 score an existing photo folder
  python cli.py relevance <photos_dir> [--anchors DIR] [--move]
                                                       which photos are substantially of the boulder? (report-only
                                                       unless --move; anchors default to climb page / video photos)
  python cli.py jobs [--server URL]                    what a restart of the running server would interrupt; exits 1
                                                       if that's anything (default http://localhost:$PORT, or 8470)
  python cli.py offtopic-split [--limit N]             harvests filtered before the boulder check (CLIP): split what
                                                       didn't match into what looks like bouldering and what doesn't
  python cli.py export-boulders <bundle.tar> [--boulder NAME ...] [--since TIME] [--data DIR]
                                                       boulders' own meshes, with their climbs' harvests, from a copy
                                                       of the data (default every boulder with a project; TIME is
                                                       seconds since 1970, or YYYY-MM-DD[THH:MM] local time)
  python cli.py import-bundle <bundle.tar> [--data DIR]  add such a bundle to another copy, replacing nothing there
                                                       (run it where the data is writable: in Docker, docker exec)
  python cli.py client-errors [-n N] [--data DIR]      the last N errors visitors' browsers reported (meshes that
                                                       wouldn't load, errors in the page), newest last
  python cli.py mesh-views [-n N] [--hours H] [--data DIR]  the last N meshes visitors' browsers showed, and how often
                                                       each was shown in the last H hours (default 24), and how
  python cli.py script-downloads [-n N] [--data DIR]   the last N downloads of the worker and volunteer scripts: when, which,
                                                       the address, roughly where (and a map), and with what
  python cli.py mesh-ratings [-n N] [--data DIR]       visitors' ratings of meshes (bad, OK, good), each mesh's, and
                                                       the last N comments on them, newest last
  python cli.py splat <mesh_id> [--steps N] [--out FILE] [--data DIR]
                                                       fit the splat of what's around a reconstructed mesh, for the
                                                       viewer's Scene background (needs CUDA torch and gsplat: splat.py)

Environment: GOOGLE_CSE_KEY/GOOGLE_CSE_CX, FLICKR_KEY, HARVEST_DDG=0 (no DuckDuckGo), DATA_DIR (cache location),
             BOULDERMESH_UA (please set a contact address), HTTP_MIN_INTERVAL (min. seconds between requests
             to one host; a longer robots.txt Crawl-delay wins).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))
import climbs  # noqa: E402
from climbs import diversity, harvest, mountainproject, relevance  # noqa: E402

IMAGE_EXT = {".jpg", ".jpeg", ".png"}


def splat(data: Path, mesh_id: str, steps: int, out: str | None) -> int:
    """Fit a reconstructed mesh's splat from its project's photos and masks (splat.py)."""
    import sqlite3
    import splat as splat_mod
    row = sqlite3.connect(f"file:{data / 'bouldermesh.sqlite'}?mode=ro", uri=True).execute(
        "SELECT source FROM meshes WHERE id=?", (mesh_id,)).fetchone()
    if not row or not row[0].startswith("project:"):
        print(f"{mesh_id}: no such reconstructed mesh in {data}", file=sys.stderr)
        return 1
    project = data / "projects" / row[0].split(":", 1)[1]
    masks = project / "masks"
    try:
        splat_mod.fit(data / "meshes" / mesh_id, project / "photos", masks if masks.is_dir() else None, steps=steps,
                      out=Path(out) if out else None)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("url"); f.add_argument("--no-children", action="store_true")
    s = sub.add_parser("search"); s.add_argument("query", nargs="?", default="")
    s.add_argument("--near", nargs=2, type=float, metavar=("LAT", "LNG")); s.add_argument("--radius", type=int, default=2000)
    c = sub.add_parser("mp-csv"); c.add_argument("area_id"); c.add_argument("--type", default="boulder")
    h = sub.add_parser("harvest"); h.add_argument("url"); h.add_argument("out_dir")
    h.add_argument("--sources", default=",".join(harvest.SOURCES)); h.add_argument("--no-filter", action="store_true")
    d = sub.add_parser("diversity"); d.add_argument("photos_dir")
    r = sub.add_parser("relevance"); r.add_argument("photos_dir"); r.add_argument("--anchors"); r.add_argument("--move", action="store_true")
    j = sub.add_parser("jobs"); j.add_argument("--server", default=f"http://localhost:{os.environ.get('PORT', '8470')}")
    here = f"http://localhost:{os.environ.get('PORT', '8470')}"
    dc = sub.add_parser("deploy-check", help="what a restart would cost now (exit 3 if it would lose work in progress)")
    dc.add_argument("--server", default=here)
    dc.add_argument("--brief", action="store_true", help="one line: only what a restart would lose, as deploy.sh waits for it")
    dc.add_argument("--color", action="store_true", help="colour it, by kind of work")
    dc.add_argument("--from", dest="source", help="the server's /api/deploy JSON from this file ('-': stdin), rather than asking it")
    dd = sub.add_parser("deploy-drain", help="take no new work (--off: carry on as before; --now: a stop won't wait either)")
    dd.add_argument("--server", default=here); dd.add_argument("--off", action="store_true"); dd.add_argument("--now", action="store_true")
    o = sub.add_parser("offtopic-split"); o.add_argument("--limit", type=int, default=0)
    sub.add_parser("span-check")
    e = sub.add_parser("export-boulders"); e.add_argument("bundle"); e.add_argument("--boulder", action="append")
    e.add_argument("--since"); e.add_argument("--data")
    i = sub.add_parser("import-bundle"); i.add_argument("bundle"); i.add_argument("--data")
    ce = sub.add_parser("client-errors"); ce.add_argument("-n", type=int, default=30); ce.add_argument("--data")
    mv = sub.add_parser("mesh-views"); mv.add_argument("-n", type=int, default=20); mv.add_argument("--hours", type=float, default=24)
    mv.add_argument("--data")
    mr = sub.add_parser("mesh-ratings"); mr.add_argument("-n", type=int, default=30); mr.add_argument("--data")
    sd = sub.add_parser("script-downloads"); sd.add_argument("-n", type=int, default=30); sd.add_argument("--data")
    sp = sub.add_parser("splat"); sp.add_argument("mesh_id"); sp.add_argument("--steps", type=int, default=7000)
    sp.add_argument("--out"); sp.add_argument("--data")
    a = ap.parse_args()

    if a.cmd == "splat":
        sys.exit(splat(Path(a.data or os.environ.get("DATA_DIR") or Path(__file__).resolve().parent / "data"), a.mesh_id, a.steps, a.out))

    if a.cmd == "client-errors":
        sys.exit(client_errors(Path(a.data or os.environ.get("DATA_DIR") or Path(__file__).resolve().parent / "data"), a.n))
    if a.cmd == "mesh-views":
        sys.exit(mesh_views(Path(a.data or os.environ.get("DATA_DIR") or Path(__file__).resolve().parent / "data"), a.n, a.hours))
    if a.cmd == "script-downloads":
        sys.exit(script_downloads(Path(a.data or os.environ.get("DATA_DIR") or Path(__file__).resolve().parent / "data"), a.n))
    if a.cmd == "mesh-ratings":
        sys.exit(mesh_ratings(Path(a.data or os.environ.get("DATA_DIR") or Path(__file__).resolve().parent / "data"), a.n))

    if a.cmd in ("export-boulders", "import-bundle"):
        if a.data:
            os.environ["DATA_DIR"] = str(Path(a.data).resolve())
        import bundle
        if a.cmd == "import-bundle":
            bundle.import_(Path(a.bundle).resolve())
        else:
            since = None
            if a.since:
                import datetime
                since = float(a.since) if a.since.replace(".", "").isdigit() else datetime.datetime.fromisoformat(a.since).timestamp()
            bundle.export(Path(a.bundle).resolve(), a.boulder, since)
        return

    if a.cmd == "jobs":
        sys.exit(jobs(a.server))
    if a.cmd == "deploy-check":
        sys.exit(deploy_check(a.server, a.brief, a.color, a.source))
    if a.cmd == "deploy-drain":
        _deploy(a.server, {"on": not a.off, "now": a.now})
        print("carrying on as before" if a.off else "taking no new work" + ("; a stop won't wait for jobs either" if a.now else ""))
        sys.exit(0)
    if a.cmd == "offtopic-split":
        sys.exit(offtopic_split(a.limit))
    if a.cmd == "span-check":
        sys.exit(span_check())

    if a.cmd == "fetch":
        recs = climbs.fetch_climb_url(a.url, include_children=not a.no_children)
        print(json.dumps([r.to_dict() for r in recs], indent=1))
    elif a.cmd == "search":
        lat, lng = (a.near or (None, None))
        print(json.dumps([r.to_dict() for r in climbs.search(a.query, lat, lng, a.radius)], indent=1))
    elif a.cmd == "mp-csv":
        print(json.dumps([r.to_dict() for r in mountainproject.export_area_csv(a.area_id, a.type)], indent=1))
    elif a.cmd == "harvest":
        recs = climbs.fetch_climb_url(a.url, include_children=False)
        out = Path(a.out_dir)
        summary = harvest.harvest(recs[0], out, print, set(a.sources.split(",")), relevance_filter=not a.no_filter)
        print(json.dumps(summary, indent=1))
        print(f"\nphotos in {out / 'photos'}; provenance in {out / 'sources.json'}")
    elif a.cmd == "diversity":
        print(json.dumps(diversity.assess(Path(a.photos_dir), print), indent=1))
    elif a.cmd == "relevance":
        d = Path(a.photos_dir)
        anchors = sorted(p for p in Path(a.anchors).iterdir() if p.suffix.lower() in IMAGE_EXT) if a.anchors else None
        photos = sorted(p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXT)
        print(json.dumps(relevance.filter_photos(photos + (anchors or []), d.parent / "rejected" if a.move else None, print,
                                                 anchors=anchors), indent=1))


def _reports(data: Path, name: str) -> list[dict]:
    """A file of reports from visitors' browsers, and the one before it, oldest first."""
    lines = []
    for f in (data / f"{name}.1", data / name):
        if f.exists():
            lines += f.read_text().splitlines()
    return [json.loads(x) for x in lines if x.strip()]


def mesh_views(data: Path, n: int, hours: float) -> int:
    """Print the last n meshes visitors' browsers showed (server.record_mesh_view()), and how often each was shown in the
    last `hours`, and how it was asked for."""
    import collections
    import datetime
    views = _reports(data, "mesh-views.jsonl")
    if not views:
        print(f"no views reported (nothing in {data / 'mesh-views.jsonl'})")
        return 0
    for v in views[-n:]:
        took = f" in {v['seconds']} s" if v.get("seconds") is not None else ""
        print(f"{v['time']} {v.get('version') or '-'} {v['mesh']} {v.get('name', '')}"
              f"{' from ' + v['via'] if v.get('via') else ''}{took}{' (editor)' if v.get('editor') else ''}"
              + (f"\n    {v['note']}" if v.get("note") else ""))
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = [v for v in views if v["time"] >= since]
    print(f"\nin the last {hours:g} h: {len(recent)} view(s) of {len({v['mesh'] for v in recent})} mesh(es); by how: "
          + ", ".join(f"{k or 'other'} {c}" for k, c in collections.Counter(v.get("via") for v in recent).most_common()))
    for (mid, name), c in collections.Counter((v["mesh"], v.get("name", "")) for v in recent).most_common(15):
        print(f"  {c:4d}  {mid} {name}")
    return 0


def script_downloads(data: Path, n: int) -> int:
    """Print the last n downloads of the worker and volunteer scripts (server.script_downloaded()), newest last."""
    lines = _reports(data, "script-downloads.jsonl")
    if not lines:
        print(f"no downloads yet (nothing in {data / 'script-downloads.jsonl'})")
        return 0
    for d in lines[-n:]:
        print(f"{d['time']} {d.get('script', '?'):9} {d.get('ip') or '?':39} {d.get('place') or '(no place)'}"
              + (f"  {d['map']}" if d.get("map") else "") + f"\n    {(d.get('ua') or '')[:140]}")
    return 0


def mesh_ratings(data: Path, n: int) -> int:
    """Print visitors' ratings of meshes (server.rate_mesh()): each rated mesh's average and how many of each, best
    first, and the last n comments, newest last. Not who: neither address nor browser."""
    import sqlite3
    import time
    con = sqlite3.connect(f"file:{data / 'bouldermesh.sqlite'}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT r.mesh_id, m.name, AVG(r.rating), SUM(r.rating=1), SUM(r.rating=2), SUM(r.rating=3) "
                           "FROM mesh_ratings r LEFT JOIN meshes m ON m.id=r.mesh_id WHERE r.rating IS NOT NULL "
                           "GROUP BY r.mesh_id ORDER BY AVG(r.rating) DESC, COUNT(*) DESC").fetchall()
        said = con.execute("SELECT r.updated, r.mesh_id, m.name, r.rating, r.comment FROM mesh_ratings r LEFT JOIN meshes m "
                           "ON m.id=r.mesh_id WHERE r.comment IS NOT NULL ORDER BY r.updated DESC LIMIT ?", (n,)).fetchall()
    except sqlite3.OperationalError:
        rows, said = [], []
    if not rows and not said:
        print("no ratings yet")
        return 0
    for mid, name, mean, bad, ok, good in rows:
        print(f"{mean:.2f}  {mid} {name or '(deleted)'}: {good} good, {ok} OK, {bad} bad")
    if said:
        print(f"\nthe last {len(said)} comment(s):")
    for updated, mid, name, rating, comment in reversed(said):
        what = {1: "bad", 2: "OK", 3: "good"}.get(rating, "no rating")
        print(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(updated))} {mid} {name or '(deleted)'} ({what}):\n    "
              + comment.replace("\n", "\n    "))
    return 0


def client_errors(data: Path, n: int) -> int:
    """Print the last n reports from visitors' browsers (server.record_client_error()), from the file and the one before."""
    reports = _reports(data, "client-errors.jsonl")
    if not reports:
        print(f"no errors reported (nothing in {data / 'client-errors.jsonl'})")
        return 0
    for r in reports[-n:]:
        where = " ".join(x for x in (f"mesh {r['mesh']}" if r.get("mesh") else "", r.get("source") or "", r.get("page") or "") if x)
        print(f"{r['time']} {r.get('version') or '-'} {r['kind']}: {r.get('message', '')[:300]}\n    {where} | {r.get('browser', '')[:120]}")
    return 0


def jobs(server: str) -> int:
    """Print what a restart of the server would interrupt (jobs started outside areas) and what it would
    only pause (areas' climbs).

    Returns:
        1 if a restart would interrupt anything, else 0.
    """
    get = lambda path: json.load(urllib.request.urlopen(server.rstrip("/") + path, timeout=30))  # noqa: E731
    try:
        st = get("/api/jobs")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        st = old_jobs(get)

    def line(j):
        bits = [j["type"]] + ([f"{j['progress']:.0f}%"] if j.get("progress") is not None else []) + \
            ([f"about {max(1, round(j['eta'] / 60))} min left"] if j.get("eta") is not None else [])
        return f"  {j['name']} ({', '.join(bits)})"
    if st["stopping"]:
        print("the server is stopping, and waiting for: " + ("; ".join(st["stopping"]["waiting_for"]) or "nothing"))
    for title, rows in (("running outside areas, which a restart interrupts:", st["running"]),
                        ("queued outside areas, which a restart interrupts:", st["queued"])):
        if rows:
            print(title, *map(line, rows), sep="\n")
    if st["area_climbs"]:
        print("areas' climbs, which a restart pauses, to carry on afterwards:",
              *(f"  {c['name']} ({c['area']})" for c in st["area_climbs"]), sep="\n")
    beta = st.get("beta") or {}
    if beta.get("running") or beta.get("queued"):
        print(f"finding beta, which a restart pauses, to carry on afterwards: {beta['running'] or 'nothing'} now, "
              f"{beta['queued']} queued" + (" (waiting while YouTube refuses downloads)" if beta.get("youtube") else ""))
    busy = bool(st["running"] or st["queued"])
    if not busy:
        print("nothing outside areas is running or queued: a restart interrupts nothing")
    return int(busy)


def _deploy(server: str, drain: dict | None = None) -> dict:
    """The server's /api/deploy (deploy_state()), or with drain, POSTed to /api/deploy/drain first."""
    url = server.rstrip("/") + ("/api/deploy/drain" if drain is not None else "/api/deploy")
    req = urllib.request.Request(url, data=json.dumps(drain).encode() if drain is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if drain is not None else "GET")
    return json.load(urllib.request.urlopen(req, timeout=30))


def _mins(s) -> str:
    return "?" if s is None else f"{max(1, round(s / 60))} min"


def _item(i: dict) -> str:
    bits = ([f"{i['progress']}%"] if i.get("progress") is not None else []) + \
        ([f"going {_mins(i['elapsed'])}"] if i.get("elapsed") else []) + ([f"about {_mins(i['eta'])} left"] if i.get("eta") is not None else [])
    return f"{i['what']}: {i['name']}" + (f" ({', '.join(bits)})" if bits else "")


WIDTH = 101     # deploy-check's lines wrap at this
# how heavy each kind of work is, and how much a restart loses of it, lightest/least first: listed in this order
WEIGHT = ["video", "scale", "beta", "filter", "harvest", "area", "boulder", "meshing"]
LOSS = ["queued again", "pauses", "starts", "interrupted", "lost", "ends"]
# red, green, orange and yellow say how it's going (bad, good, warning, waiting), so kinds of work are other colours
COLOR = {"meshing": "1;34", "harvest": "36", "area": "38;5;141", "scale": "35", "beta": "38;5;105", "filter": "38;5;73",
         "boulder": "38;5;183", "video": "38;5;153"}
GREY = "90"     # what deploy.sh says as it waits, but for what's coloured otherwise
STATUS = {"running": "32", "waiting for a machine": "33", "queued": "90", "!": "1;31"}
VERB = {"ends": "31", "interrupted": "31", "lost": "31", "starts": "32", "pauses": "33", "queued": "90"}


def _paint(s: str, code: str | None, color: bool) -> str:
    return f"\033[{code}m{s}\033[0m" if color and code else s


def _kind(what: str) -> str:
    """An item's kind from what it says it is, for an older server's."""
    for start, kind in (("meshing", "meshing"), ("finding size", "scale"), ("scale", "scale"), ("finding beta", "beta"),
                        ("beta", "beta"), ("filtering", "filter"), ("harvest", "harvest"), ("boulder", "boulder"),
                        ("reconstruct", "meshing"), ("a video", "video")):
        if what.startswith(start):
            return kind
    return "area" if ": " in what else "other"


def _rank(i: dict) -> tuple:
    kind = WEIGHT.index(i["kind"]) if i.get("kind") in WEIGHT else WEIGHT.index("area")
    loss = next((n for n, w in enumerate(LOSS) if i["lost"].startswith(w)), 0)
    group = (2 if i.get("kind") == "meshing" else 1) if i["costly"] else 0
    return group, kind, loss, i.get("elapsed") or 0


def _state(i: dict) -> str:
    return "queued" if i["what"].endswith(", queued") else "waiting for a machine" if "waiting for a machine" in i["what"] else \
        "asked for" if "asked for" in i["what"] else "running"


def _parts(what: str, area: bool = False) -> tuple[str, str]:
    """What an item says it is, split into the work ("meshing", an area's name) and how it's going ("on hal-2",
    "queued", the step an area's climb is on)."""
    for sep in ((": ",) if area else (", ", " on ", " here", " coming from ")):
        if sep in what:
            job, rest = what.split(sep, 1)
            return job, (sep.strip(", :") + " " + rest).strip() if sep.strip(", :") else rest
    return what, ""


def _colored(line: str, i: dict, color: bool) -> str:
    """A line of an item's, with its work coloured by its kind, how it's going by that (green running, yellow waiting,
    grey queued), and a ! in red."""
    if not color:
        return line
    job, how = _parts(i["what"], i.get("kind") in ("area", "other"))
    if line.lstrip().startswith("!"):
        line = line.replace("!", _paint("!", STATUS["!"], True), 1)
    if job and job in line:
        line = line.replace(job, _paint(job, COLOR.get(i.get("kind")), True), 1)
    if how and how in line:
        last = how.rsplit(": ", 1)[-1]      # an area's climb's step, not "its boulder: " before it
        line = line.replace(last, _paint(last, STATUS.get(_state(i), STATUS["running"]), True), 1)
    return line


def _brief(i: dict, color: bool) -> str:
    """An item for deploy.sh's line as it waits, in grey (GREY) but for its work and how it's going, coloured as in
    _colored(), and the machine or volunteer it's on, left white."""
    text = _item(i)
    if not color:
        return text
    back = lambda code, t: f"\033[{code}m{t}\033[{GREY}m"   # noqa: E731
    job, how = _parts(i["what"], i.get("kind") in ("area", "other"))
    if job and text.startswith(job):
        text = back(COLOR.get(i.get("kind"), GREY), job) + text[len(job):]
    for lead in ("on ", "coming from "):
        if how.startswith(lead) and how in text:   # "on hal-2": "on" as how it's going, the machine white
            return text.replace(how, back(STATUS["running"], lead.strip()) + " " + back("0", how[len(lead):]), 1)
    if how:
        last = how.rsplit(": ", 1)[-1]
        text = text.replace(last, back(STATUS.get(_state(i), STATUS["running"]), last), 1)
    return text


def _show(st: dict, color: bool = False) -> None:
    """Each item, ! ones and heavier, costlier ones lower down (meshing last), wrapped at WIDTH; work for the project of
    one before it (by "project_id") told under that one, as part of it. Then, after a line "@@legend" for deploy.sh,
    what a restart does to each kind of work listed, once for each."""
    import textwrap
    items = sorted(st["items"], key=_rank)
    parents: dict[str, dict] = {}
    for i in items:
        if i["costly"] and i.get("project_id"):
            parents.setdefault(i["project_id"], i)
    under: dict[int, list[dict]] = {}
    for i in items:
        top = parents.get(i.get("project_id"))
        if top is not None and top is not i:
            under.setdefault(id(top), []).append(i)
    for top in items:
        if parents.get(top.get("project_id"), top) is not top:
            continue    # told under the one it's part of
        for n, line in enumerate(textwrap.wrap(f"{'!' if top['costly'] else ' '} {_item(top)}", WIDTH - 2, subsequent_indent="    ")):
            print("  " + (_colored(line, top, color) if n == 0 else line))
        for sub in under.get(id(top), []):
            for n, line in enumerate(textwrap.wrap(f"  └ {_item(sub)}", WIDTH - 2, subsequent_indent="      ")):
                print("  " + (_colored(line, sub, color) if n == 0 else line))
    legend: dict[tuple, dict] = {}
    for i in items:
        legend.setdefault((i.get("kind") or "other", _state(i), i["lost"]), i)
    print("@@legend")
    print("a restart:")
    for (kind, state, lost), i in legend.items():     # coloured as its lines are, and what happens by its verbs
        said = " ".join(_paint(w, VERB.get(w.strip(",:")), color) for w in lost.split(" "))
        print(f"  {_paint(kind, COLOR.get(kind), color)} ({_paint(state, STATUS.get(state, STATUS['running']), color)}): {said}")


def deploy_check(server: str, brief: bool = False, color: bool = False, source: str | None = None) -> int:
    """Print what a restart would cost now, each piece of work in progress marked ! if it would lose much of it, and
    what a restart does to each kind of work, after a line "@@legend" (_show()).

    Args:
        brief: print only a line of what a restart would lose, for deploy.sh to print as it waits.
        color: in colour: each piece of work by its kind, how it's going, and ! in red (_colored()), as for a terminal.
        source: a file of the server's /api/deploy JSON ('-': stdin), fetched by deploy.sh, which shows it with this
            copy of cli.py rather than the server's (which may be older).

    Returns:
        3 if a restart would lose work in progress, else 0 (1 if the server can't be asked; 2 is argparse's, as from an
        older cli.py without this command, or without --brief).
    """
    try:
        st = json.load(sys.stdin if source == "-" else open(source)) if source else _deploy(server)
        for i in st["items"]:
            i.setdefault("kind", _kind(i["what"]))   # from a server from before deploy_state() said (Nov 12, 2026)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        print(f"can't ask the server what's running: {e}")
        return 1
    if brief:
        costly = [i for i in st["items"] if i["costly"]]
        line = "waiting for: " + "; ".join(_brief(i, color) for i in costly) if costly else "nothing a restart would lose is in progress"
        print(f"\033[{GREY}m{line}\033[0m" if color else line)
        return 3 if costly else 0
    if not st["items"]:
        print("nothing in progress: a restart loses nothing")
        return 0
    if not st["costly"]:
        print("in progress, which a restart only pauses, so there's nothing to wait for:")
        _show(st, color)
        return 0
    print("in progress (marked !: a restart now loses much of it" +
          (f"; waiting for them would take about {_mins(st['eta'])}):" if st["eta"] is not None else "):"))
    _show(st, color)
    return 3


def offtopic_split(limit: int = 0) -> int:
    """relevance.split_rejected() for every harvest under DATA_DIR (default ./data), newest first.

    Returns:
        1 without CLIP (torch and open_clip), else 0.
    """
    if "boulder" not in relevance.available():
        print("needs torch and open_clip")
        return 1
    root = Path(os.environ.get("DATA_DIR", "data")) / "projects"
    done = sorted((p for p in root.iterdir() if (p / "relevance.json").exists()), key=lambda p: (p / "relevance.json").stat().st_mtime, reverse=True)
    total = {"nomatch": 0, "notboulder": 0}
    for i, p in enumerate(done[:limit or None]):
        r = relevance.split_rejected(p, lambda s: None)
        if r:
            total = {k: total[k] + r[k] for k in total}
            print(f"{i + 1}/{len(done)} {p.name}: {r['nomatch']} look like bouldering, {r['notboulder']} don't", flush=True)
    print(f"in all: {total['nomatch']} look like bouldering, {total['notboulder']} don't")
    return 0


def span_check() -> int:
    """How each video's climber's reach (scale.py's span, recorded as a check) compares with its mesh's size from
    climbers' limbs in its videos, for every mesh under DATA_DIR (default ./data) sized since its beta was found.

    Returns:
        1 if no mesh has a span check yet, else 0.
    """
    import statistics
    rows = []
    for f in sorted((Path(os.environ.get("DATA_DIR", "data")) / "meshes").glob("*/scale.json")):
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        limbs = {e["group"]: e["metres_per_unit"] for e in rec.get("estimates") or [] if e["kind"] == "video"}
        for c in rec.get("checks") or []:
            if c["kind"] == "span" and rec.get("metres_per_unit"):
                rows.append((f.parent.name, c["group"], c["n"], c["metres_per_unit"] / rec["metres_per_unit"],
                             c["metres_per_unit"] / limbs[c["group"]] if c["group"] in limbs else None))
    if not rows:
        print("no mesh has a span check yet: find beta in meshes with videos, then find their size again")
        return 1
    print("mesh          frames  span/final  span/same video's limbs  video")
    for mid, url, n, final, same in rows:
        print(f"{mid}  {n:6d}  {final:10.2f}  {f'{same:.2f}' if same else '-':>23}  {url}")
    ratios = sorted(r[3] for r in rows)
    print(f"{len(rows)} videos in {len({r[0] for r in rows})} meshes: span/final median {statistics.median(ratios):.2f}, "
          f"within 25% for {sum(0.8 <= x <= 1.25 for x in ratios)} of them")
    return 0


def old_jobs(get) -> dict:
    """/api/jobs, pieced together from a server from before it, which ignores SIGTERM, and fails what a
    restart interrupts: projects not in an area that are busy, searches for beta, and how many are queued."""
    print("(a server from before /api/jobs: a restart fails the jobs below, and it ignores SIGTERM, so give "
          "`docker compose up` -t 10)")
    areas = {r["project_id"]: (r["name"], c["name"]) for c in get("/api/campaigns") for r in c.get("running") or [] if r.get("project_id")}
    busy = [p for p in get("/api/projects") if p["status"] in ("queued", "running", "harvesting")]
    beta = [m for m in get("/api/meshes") if (m.get("beta") or {}).get("status") in ("queued", "running")]
    queued = get("/api/status").get("queue") or 0
    out = [{"type": p["status"], "name": p["name"]} for p in busy if p["id"] not in areas]
    out += [{"type": f"beta, {m['beta']['status']}", "name": m["name"]} for m in beta]
    if queued and not any(p["status"] == "queued" for p in busy):
        out.append({"type": "queued", "name": f"{queued} job(s) not tied to a project"})
    return {"running": out, "queued": [], "stopping": None,
            "area_climbs": [{"name": name, "area": area} for pid, (name, area) in areas.items()]}


if __name__ == "__main__":
    main()
