"""Moving boulders' meshes, and the harvests behind them, from one BoulderMesh's data to another's: from a test copy
to the server it was copied from, or to any other, such as the public one.

  python3 cli.py export-boulders bundle.tar [--boulder NAME ...] [--since TIME]   (with DATA_DIR the copy's)
  python3 cli.py import-bundle bundle.tar                                         (with DATA_DIR the one to add to)

A bundle is a tar of manifest.json, with the database rows, and the folders of the projects and meshes in it. Importing
adds only what isn't there: projects and meshes by id, and climbs by where they came from (their source and its id),
as two copies give a climb first seen in either its own id. A climb whose page the bundle has read, where the copy
being added to has only an area list's summary of it, takes the page's details. An area list's climbs take the
bundle's harvests as theirs, where they have none. Nothing already there is replaced or deleted.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import time
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
FORMAT = 1


def _server():
    import server    # reads DATA_DIR when it's first imported: the caller sets it
    server.init_db()
    return server


def _in(column: str, values: list) -> tuple[str, list]:
    return (f"{column} IN ({','.join('?' * len(values))})", list(values)) if values else ("0", [])


def export(out: Path, boulders: list[str] | None = None, since: float | None = None, log: Log = print) -> dict:
    """Write a bundle of boulders' own meshes and projects, every version, with their climbs, those climbs' harvests and
    meshes, and what's known of their videos.

    Args:
        boulders: the boulders, by name (the last area of their path) or whole area path as JSON; default every one
            with a project.
        since: only harvests and climbs' meshes made since then (a time.time()), such as since a test copy was made;
            boulders' own projects and meshes go regardless.

    Returns:
        What went in it, by table.
    """
    S = _server()
    known = [r["boulder"] for r in S.q("SELECT DISTINCT boulder FROM projects WHERE kind='boulder' AND boulder IS NOT NULL")]
    if boulders:
        picked = [b for b in known if b in boulders or json.loads(b)[-1] in boulders]
        missing = [b for b in boulders if b not in picked and not any(json.loads(k)[-1] == b for k in picked)]
        if missing:
            raise ValueError(f"no boulder project for: {', '.join(missing)}")
        known = picked
    when, args = ("created >= ?", [since]) if since else ("1", [])
    where, wargs = _in("area_path", known)
    climbs = S.q(f"SELECT * FROM climbs WHERE {where}", *wargs)
    cids = [c["id"] for c in climbs]
    bw, bargs = _in("boulder", known)
    cw, cargs = _in("climb_id", cids)
    projects = S.q(f"SELECT * FROM projects WHERE (kind='boulder' AND {bw}) OR (kind='harvest' AND {cw} AND {when})",
                   *bargs, *cargs, *args)
    sources = [f"project:{p['id']}" for p in projects]
    sw, sargs = _in("source", sources)
    lw, largs = _in("id", [r["mesh_id"] for r in S.q(f"SELECT mesh_id FROM mesh_links WHERE {cw}", *cargs)])
    meshes = S.q(f"SELECT * FROM meshes WHERE {bw} OR {sw} OR ({lw} AND {when})", *bargs, *sargs, *largs, *args)
    mw, margs = _in("mesh_id", [m["id"] for m in meshes])
    links = S.q(f"SELECT * FROM mesh_links WHERE {mw} AND {cw}", *margs, *cargs)
    climb_videos = S.q(f"SELECT * FROM climb_videos WHERE {cw}", *cargs)
    uw, uargs = _in("url", sorted({v["url"] for v in climb_videos}))
    videos = S.q(f"SELECT * FROM videos WHERE {uw}", *uargs)
    chw, chargs = _in("channel_url", sorted({v["channel_url"] for v in videos if v["channel_url"]}))
    channels = S.q(f"SELECT * FROM channels WHERE {chw}", *chargs)
    items = S.q(f"SELECT DISTINCT source_id, climb_id, project_id FROM campaign_items WHERE {cw}", *cargs)
    for p in projects:
        p["job"] = None     # a job left queued there isn't one here
    manifest = {"format": FORMAT, "created": time.time(), "from": str(S.DATA), "since": since, "boulders": known,
                "rows": {"climbs": climbs, "projects": projects, "meshes": meshes, "mesh_links": links, "videos": videos,
                         "channels": channels, "climb_videos": climb_videos, "campaign_items": items}}
    tmp = out.with_name(out.name + ".partial")
    with tarfile.open(tmp, "w") as tar:
        data = json.dumps(manifest, indent=1).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime = len(data), int(time.time())
        tar.addfile(info, io.BytesIO(data))
        for kind, rows in (("projects", projects), ("meshes", meshes)):
            for r in rows:
                d = S.DATA / kind / r["id"]
                if d.is_dir():   # a project's work folder is only of use to the job that made it
                    tar.add(d, f"{kind}/{r['id']}", filter=lambda t: None if t.name.split("/")[2:3] == ["work"] else t)
    tmp.replace(out)
    counts = {k: len(v) for k, v in manifest["rows"].items()}
    log(f"exported {len(known)} boulder(s) to {out} ({out.stat().st_size / 2**20:.0f} MB): "
        + ", ".join(f"{n} {k}" for k, n in counts.items()))
    return counts


def _stub(data: str | None) -> bool:
    try:
        return bool((json.loads(data or "{}").get("raw") or {}).get("stub"))
    except ValueError:
        return True


def _insert(S, table: str, row: dict) -> None:
    """Add a row unless one with its key is there. Columns this copy doesn't have are left out."""
    cols = [c for c in row if c in {r["name"] for r in S.q(f"SELECT name FROM pragma_table_info('{table}')")}]
    S.q(f"INSERT OR IGNORE INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", *[row[c] for c in cols], write=True)


def _key(table: str) -> list[str]:
    return {"mesh_links": ["mesh_id", "climb_id"], "climb_videos": ["climb_id", "url"], "videos": ["url"],
            "channels": ["channel_url"]}.get(table, ["id"])


def import_(bundle: Path, log: Log = print) -> dict:
    """Add a bundle's boulders, climbs, harvests and meshes to this data (DATA_DIR), and their folders, without
    replacing anything already there.

    Returns:
        How many of each were added.
    """
    S = _server()
    added = {k: 0 for k in ("climbs", "climbs_updated", "projects", "meshes", "mesh_links", "videos", "channels",
                            "climb_videos", "campaign_items")}
    staging = S.DATA / f"import-{os.getpid()}-{int(time.time())}"
    with tarfile.open(bundle) as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
        if manifest.get("format") != FORMAT:
            raise ValueError(f"a bundle of format {manifest.get('format')}, not {FORMAT}")
        rows = manifest["rows"]
        ids = {}     # its climbs' ids -> this copy's
        for c in rows["climbs"]:
            here = S.q("SELECT id, data FROM climbs WHERE source=? AND source_id=?", c["source"], c["source_id"], one=True)
            if here:
                ids[c["id"]] = here["id"]
                if _stub(here["data"]) and not _stub(c["data"]):   # as upsert_climb() does: hand-set coordinates stay
                    S.upsert_climb(S.Climb.from_dict(json.loads(c["data"])))
                    added["climbs_updated"] += 1
            else:
                cid = c["id"] if not S.q("SELECT 1 FROM climbs WHERE id=?", c["id"]) else S.new_id()
                ids[c["id"]] = cid
                S.q("INSERT INTO climbs(id,source,source_id,url,name,kind,grade,lat,lng,coord_source,boulder_name,area_path,data,created) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", cid, *[c[k] for k in ("source", "source_id", "url", "name", "kind", "grade", "lat",
                    "lng", "coord_source", "boulder_name", "area_path", "data", "created")], write=True)
                added["climbs"] += 1
        new = {"projects": [], "meshes": []}
        for table in ("projects", "meshes"):
            for r in rows[table]:
                if S.q(f"SELECT 1 FROM {table} WHERE id=?", r["id"]):
                    continue
                if table == "projects" and r.get("climb_id"):
                    r["climb_id"] = ids.get(r["climb_id"], r["climb_id"])
                _insert(S, table, r)
                new[table].append(r["id"])
                added[table] += 1
        for table in ("mesh_links", "climb_videos"):
            for r in rows[table]:
                r["climb_id"] = ids.get(r["climb_id"], r["climb_id"])
                if not S.q(f"SELECT 1 FROM {table} WHERE " + " AND ".join(f"{k}=?" for k in _key(table)), *[r[k] for k in _key(table)]):
                    _insert(S, table, r)
                    added[table] += 1
        for table in ("videos", "channels"):
            for r in rows[table]:
                if not S.q(f"SELECT 1 FROM {table} WHERE {_key(table)[0]}=?", r[_key(table)[0]]):
                    _insert(S, table, r)
                    added[table] += 1
        for r in rows["campaign_items"]:    # an area's climb that the copy first saved is the same climb here
            added["campaign_items"] += S.q("SELECT COUNT(*) n FROM campaign_items WHERE source_id=? AND climb_id IS NULL",
                                           r["source_id"], one=True)["n"]
            S.q("UPDATE campaign_items SET climb_id=? WHERE source_id=? AND climb_id IS NULL",
                ids.get(r["climb_id"], r["climb_id"]), r["source_id"], write=True)
        for r in sorted(rows["projects"], key=lambda r: -(r.get("created") or 0)):   # and takes its newest harvest, where it has none
            if r["id"] in new["projects"] and r.get("kind") == "harvest" and r.get("climb_id"):
                added["campaign_items"] += S.q("SELECT COUNT(*) n FROM campaign_items WHERE climb_id=? AND project_id IS NULL",
                                               r["climb_id"], one=True)["n"]
                S.q("UPDATE campaign_items SET project_id=? WHERE climb_id=? AND project_id IS NULL", r["id"], r["climb_id"], write=True)
        wanted = [f"{k}/{i}" for k in ("projects", "meshes") for i in new[k]]
        members = [m for m in tar.getmembers() if any(m.name == w or m.name.startswith(w + "/") for w in wanted)]
        staging.mkdir(parents=True)
        try:
            tar.extractall(staging, members=members, filter="data")
            for w in wanted:
                if (staging / w).is_dir() and not (S.DATA / w).exists():
                    (S.DATA / w).parent.mkdir(parents=True, exist_ok=True)
                    (staging / w).rename(S.DATA / w)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    log(f"imported {bundle}: " + (", ".join(f"{n} {k.replace('_', ' ')}" for k, n in added.items() if n) or "nothing new"))
    return added
