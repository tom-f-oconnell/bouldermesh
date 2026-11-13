"""What each mesh was made from, kept beside it (inputs.json in its folder), so that a change to how meshes are made can
mark old only the meshes whose inputs it changes, and a dry run can say which those would be (server.inputs_check()).

  photos       the photos it was given: {n, digest, at, files: {name: [bytes, SHA-1]}}, digest over every name and
               SHA-1, at when they were read
  leaf, leaf_kind
               for a boulder's or a pool's mesh: its lowest area, and what that was taken to be (server.leaf_kind():
               "boulder", "group", "unknown", or "unread" where its page hadn't been read, which is meshed whole too)
  pool         for a pool's mesh: the climbs pooled, whose photos matched
  climbs       for a boulder's or a pool's mesh: each climb's harvests pooled ({climb id: [project ids]}, CLIMBS_FILE)
  pooled       for a boulder's or a pool's mesh: the photos its harvests gave (harvest.pool()), before dedupe and the
               off-topic filter
  filter       the off-topic filter that judged the photos, from its report (relevance.json): {version, settings,
               methods}, or "none" where no filter ran
  meshopts     COLMAP's mapper settings (meshopts.mapper())
  mesh_rules   server.MESH_RULES it was made under
  unknown      {field: why} for each field that couldn't be told, which is then null
  fingerprint  SHA-1 over all of the above, or null if any is unknown

A field that doesn't apply to a mesh (a climb's own mesh has no pool) is left out. Meshes made before this was kept
have what their projects still tell of them (server.backfill_inputs()), and "backfilled": true.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

FILE = "inputs.json"
FORMAT = 1
PHOTO_EXT = {".jpg", ".jpeg", ".png"}
FIELDS = ("photos", "leaf", "leaf_kind", "pool", "climbs", "pooled", "filter", "meshopts", "mesh_rules")
WHOLE = ("boulder", "unread")   # leaf kinds meshed as one boulder (server.pools_whole())


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def digest_of(files: dict[str, list]) -> str:
    """One SHA-1 over photos' names and contents ({name: [bytes, SHA-1]})."""
    return hashlib.sha1("".join(f"{n}\0{files[n][1]}\n" for n in sorted(files)).encode()).hexdigest()


def photo_files(paths: list[Path], at: float | None = None) -> dict:
    """The photos field for these files: each one's size and SHA-1 (as of at, by default now)."""
    import time
    files = {p.name: [p.stat().st_size, sha1_file(p)] for p in sorted(paths)}
    return {"n": len(files), "digest": digest_of(files), "at": time.time() if at is None else at, "files": files}


def photos_in(folder: Path) -> list[Path]:
    """The photos in a folder that a mesh is made from."""
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_EXT) if folder.is_dir() else []


def same_photos(recorded: dict, now: dict[str, Path]) -> dict | None:
    """How these photos ({name: file}) differ from the photos recorded: None if not at all, else {added, removed,
    contents}. A file of the same size not written since it was recorded is taken to hold what it did; any other is read
    again."""
    files = recorded.get("files") or {}
    added, removed = sorted(set(now) - set(files)), sorted(set(files) - set(now))
    contents = []
    for name in sorted(set(now) & set(files)):
        st = os.stat(now[name])
        if st.st_size != files[name][0] or st.st_mtime > (recorded.get("at") or 0):
            if st.st_size != files[name][0] or sha1_file(now[name]) != files[name][1]:
                contents.append(name)
    return {"added": added, "removed": removed, "contents": contents} if added or removed or contents else None


def filter_of(report: dict | None) -> dict | str:
    """The filter field, from a relevance.json: "none" for no report. A report from before the filter's version was
    kept has none (null)."""
    if report is None:
        return "none"
    return {"version": report.get("version"), "settings": report.get("settings"), "methods": sorted(report.get("methods") or [])}


def fingerprint(record: dict) -> str | None:
    """One SHA-1 over every field the record has (the photos by their digest), or None if any is unknown."""
    if record.get("unknown"):
        return None
    fields = {f: record[f] for f in FIELDS if f in record}
    if isinstance(fields.get("photos"), dict):
        fields["photos"] = fields["photos"]["digest"]
    return hashlib.sha1(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def finish(record: dict) -> dict:
    """A record with its unknown fields null (and a filter's parts it didn't keep listed as unknown), and its
    fingerprint."""
    if isinstance(record.get("filter"), dict):
        for k in ("version", "settings"):
            if record["filter"].get(k) is None:
                record.setdefault("unknown", {})[f"filter.{k}"] = "not kept by the filter that judged its photos"
    for f in record.get("unknown") or {}:
        if "." not in f:
            record[f] = None
    record["format"] = FORMAT
    record["fingerprint"] = fingerprint(record)
    return record


def save(mesh_dir: Path, record: dict) -> dict:
    record = finish(record)
    tmp = mesh_dir / (FILE + ".tmp")
    tmp.write_text(json.dumps(record, indent=1))
    tmp.replace(mesh_dir / FILE)
    return record


def load(mesh_dir: Path) -> dict | None:
    try:
        return json.loads((mesh_dir / FILE).read_text())
    except (OSError, ValueError):
        return None


def brief(record: dict | None) -> dict | None:
    """A record without each photo's size and SHA-1, for a mesh's details."""
    if not record:
        return None
    out = dict(record)
    if isinstance(out.get("photos"), dict):
        out["photos"] = {k: v for k, v in out["photos"].items() if k != "files"} | {"names": sorted(out["photos"]["files"])}
    return out


def _listed(a: list, b: list) -> dict:
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b))}


def compare(recorded: dict, now: dict, needs: dict[str, str]) -> dict:
    """How what a mesh would be made from now differs from what it was.

    Args:
        recorded: its inputs.json.
        now: the same fields, as the code now would have them; a field can't-be-told is missing, and in needs. The
            photos as {"same": True} or {"changed": same_photos()}, rather than a record.
        needs: {field: what telling it would need running}.

    Returns:
        {status: "changed" | "unknown" | "same", changed: {field: how}, unknown: {field: why}}: changed if any field
        recorded differs from now; else unknown if any is unknown then or can't be told now; else same.
    """
    changed, unknown = {}, {}
    gone = recorded.get("unknown") or {}
    for f in FIELDS:
        if f not in recorded and f not in now and f not in needs:
            continue
        if f in gone:
            unknown[f] = f"not known for this mesh: {gone[f]}"
            continue
        if f in needs:
            unknown[f] = f"can't tell without {needs[f]}"
            continue
        was, got = recorded.get(f), now.get(f)
        if f == "photos":
            if got and got.get("changed"):
                changed[f] = got["changed"]
            continue
        if f == "leaf_kind":
            if was != got and not (was in WHOLE and got in WHOLE):
                changed[f] = {"was": was, "now": got}
            continue
        if f in ("pool", "pooled"):
            if sorted(was or []) != sorted(got or []):
                changed[f] = _listed(was or [], got or [])
            continue
        if f == "climbs":
            was_, got_ = {c: sorted(h) for c, h in (was or {}).items() if h}, {c: sorted(h) for c, h in (got or {}).items() if h}
            if was_ != got_:
                changed[f] = {**_listed(list(was_), list(got_)),
                              "harvests": sorted(c for c in set(was_) & set(got_) if was_[c] != got_[c])}
            continue
        if f == "filter" and isinstance(was, dict) and isinstance(got, dict):
            for k in ("version", "settings", "methods"):
                if was.get(k) is None:
                    unknown[f"filter.{k}"] = "not kept by the filter that judged its photos"
            diff = {k: {"was": was.get(k), "now": got.get(k)} for k in ("version", "settings", "methods")
                    if was.get(k) is not None and was.get(k) != got.get(k)}
            if diff:
                changed[f] = diff
            continue
        if was != got:
            changed[f] = {"was": was, "now": got}
    status = "changed" if changed else "unknown" if unknown else "same"
    return {"status": status, "changed": changed, "unknown": unknown}
