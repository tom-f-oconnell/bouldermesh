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
    o = sub.add_parser("offtopic-split"); o.add_argument("--limit", type=int, default=0)
    sub.add_parser("span-check")
    e = sub.add_parser("export-boulders"); e.add_argument("bundle"); e.add_argument("--boulder", action="append")
    e.add_argument("--since"); e.add_argument("--data")
    i = sub.add_parser("import-bundle"); i.add_argument("bundle"); i.add_argument("--data")
    ce = sub.add_parser("client-errors"); ce.add_argument("-n", type=int, default=30); ce.add_argument("--data")
    mv = sub.add_parser("mesh-views"); mv.add_argument("-n", type=int, default=20); mv.add_argument("--hours", type=float, default=24)
    mv.add_argument("--data")
    mr = sub.add_parser("mesh-ratings"); mr.add_argument("-n", type=int, default=30); mr.add_argument("--data")
    sp = sub.add_parser("splat"); sp.add_argument("mesh_id"); sp.add_argument("--steps", type=int, default=7000)
    sp.add_argument("--out"); sp.add_argument("--data")
    a = ap.parse_args()

    if a.cmd == "splat":
        sys.exit(splat(Path(a.data or os.environ.get("DATA_DIR") or Path(__file__).resolve().parent / "data"), a.mesh_id, a.steps, a.out))

    if a.cmd == "client-errors":
        sys.exit(client_errors(Path(a.data or os.environ.get("DATA_DIR") or Path(__file__).resolve().parent / "data"), a.n))
    if a.cmd == "mesh-views":
        sys.exit(mesh_views(Path(a.data or os.environ.get("DATA_DIR") or Path(__file__).resolve().parent / "data"), a.n, a.hours))
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
