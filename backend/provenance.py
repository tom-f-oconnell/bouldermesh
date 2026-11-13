"""Where each mesh's photos came from, and under what licence, kept with the mesh.

  photo_sources.json    one entry per photo: file, source, url, page, credit and licence, as the
                        harvest recorded them. For the photos placed in the mesh if cameras.json
                        says which they were, otherwise for every photo it was given.

Kept with the mesh rather than looked up in its project, as a project's photos can change or be
deleted after the mesh is built. Photos in a project that the harvest didn't record are taken to be
the user's own uploads. A video frame's licence and uploader, when the harvest hadn't recorded them,
come from what the server has since learnt of the video (load's `videos`).
"""
from __future__ import annotations

import collections
import hashlib
import json
import time
from pathlib import Path
from typing import Callable

FILE = "photo_sources.json"
PHOTO_EXT = {".jpg", ".jpeg", ".png"}
SOURCES = ("ddg", "youtube", "commons", "mountainproject", "openbeta", "flickr", "google")


def _video_urls(climb: dict) -> dict[str, str]:
    """Video frames are named yt_<tag>_NNN.jpg, the tag being the start of the SHA-1 of the video's
    URL (harvest.video_frames), which older harvests didn't record. The climb lists its videos."""
    out = {}
    for v in climb.get("videos") or []:
        url = v.get("url") if isinstance(v, dict) else v
        if url:
            out[hashlib.sha1(url.encode()).hexdigest()[:8]] = url
    return out


def harvested(project_dir: Path) -> dict[str, dict]:
    """What the harvest recorded about each of a project's photos, by file name and by stem (photos
    converted to JPEG after downloading keep their stem)."""
    path = project_dir / "sources.json"
    if not path.exists():
        return {}
    record = json.loads(path.read_text())
    videos = _video_urls(record.get("climb") or {})
    out = {}
    for entry in record.get("files") or []:
        entry = dict(entry)
        if entry.get("source") == "youtube" and not entry.get("url"):
            parts = entry["file"].split("_")
            entry["url"] = videos.get(parts[1]) if len(parts) > 2 else None
        out[entry["file"]] = out[Path(entry["file"]).stem] = entry
    return out


def describe(name: str, known: dict[str, dict]) -> dict:
    """One photo's entry, from what the harvest recorded, or else from its name. A pooled photo whose harvest
    hadn't recorded it has an entry without a source (harvest.pool()), which its name fills in."""
    entry = known.get(name) or known.get(Path(name).stem)
    if entry and entry.get("source"):
        return {**entry, "file": name}
    return {**_by_name(name), **(entry or {}), "file": name}


def _by_name(name: str) -> dict:
    prefix = name.split("_")[0]
    if prefix == "yt":
        return {"file": name, "source": "youtube", "license": None}
    if prefix in SOURCES:
        return {"file": name, "source": prefix, "license": None}
    return {"file": name, "source": "upload", "license": "the user's own photo"}


def captured(project_dir: Path) -> dict[str, dict]:
    """The photos an app of ours took for a project (its capture's record names them), each with the licence the
    person who took them gives them under and who to credit: {} if its record gives no licence, or there's none."""
    try:
        record = json.loads((project_dir / "capture.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict) or not record.get("licence"):
        return {}
    entry = {"source": "upload", "license": str(record["licence"])[:40], "credit": str(record["credit"])[:80] if record.get("credit") else None}
    return {s["name"]: entry for s in record.get("shots") or [] if isinstance(s, dict) and s.get("name")}


def save(mesh_dir: Path, project_dir: Path) -> dict:
    """Record where a mesh's photos came from.

    Returns:
        The record written.
    """
    cameras = mesh_dir / "cameras.json"
    placed = [c["name"] for c in json.loads(cameras.read_text())] if cameras.exists() else None
    photos = project_dir / "photos"
    names = placed if placed is not None else sorted(
        p.name for p in (photos.iterdir() if photos.exists() else []) if p.suffix.lower() in PHOTO_EXT)
    known, own = harvested(project_dir), captured(project_dir)
    record = {"basis": "placed" if placed is not None else "given", "created": time.time(),
              "photos": [{**describe(n, known), **own.get(n, {})} for n in names]}
    (mesh_dir / FILE).write_text(json.dumps(record, indent=1))
    return record


def unrecorded(licence: str | None) -> bool:
    """Whether a licence string says nothing: missing, or the harvest's note that it wasn't recorded."""
    return not licence or licence.startswith(("frame from linked beta video", "not recorded"))


def load(mesh_dir: Path, videos: Callable[[str], dict | None] | None = None) -> dict | None:
    """The record kept with a mesh, or None.

    Args:
        videos: what's known of a video by its URL, {"license", "uploader"} or None, for frames whose licence
            or credit the harvest hadn't recorded: the server learns these after some harvests.
    """
    path = mesh_dir / FILE
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    # saved before describe() filled in pooled photos' sources
    record["photos"] = [p if p.get("source") else describe(p["file"], {p["file"]: p}) for p in record["photos"]]
    if videos:
        for p in record["photos"]:
            if p.get("source") == "youtube" and p.get("url") and (unrecorded(p.get("license")) or not p.get("credit")):
                info = videos(p["url"]) or {}
                if unrecorded(p.get("license")) and info.get("license"):
                    p["license"] = info["license"]
                if not p.get("credit") and info.get("uploader"):
                    p["credit"] = info["uploader"]
    return record


def summary(record: dict | None) -> dict | None:
    """Photos per source, with the licences recorded for them and, for video frames, how many videos.

    Returns:
        {basis, total, sources: [{source, photos, licenses: {licence: photos}, videos, credits: who to credit for
        photos taken with an app of ours}]}, sources
        most photos first, or None without a record.
    """
    if not record:
        return None
    by: dict[str, dict] = {}
    for p in record["photos"]:
        s = by.setdefault(p["source"], {"source": p["source"], "photos": 0, "licenses": collections.Counter(), "videos": set(), "credits": []})
        s["photos"] += 1
        if p["source"] == "upload" and p.get("credit") and p["credit"] not in s["credits"]:
            s["credits"].append(p["credit"])
        s["licenses"][p.get("license") or "not recorded"] += 1
        if p["source"] == "youtube" and p.get("url"):
            s["videos"].add(p["url"])
    out = sorted(by.values(), key=lambda s: -s["photos"])
    for s in out:
        s["licenses"] = dict(s["licenses"].most_common())
        s["videos"] = len(s["videos"]) if s["source"] == "youtube" else None
    return {"basis": record["basis"], "total": len(record["photos"]), "sources": out}
