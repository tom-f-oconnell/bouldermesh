"""Making room on a server whose data a backup on another machine holds (deploy/backup.sh), and getting it back.

Nothing is deleted here that the backup can't be shown to hold. In two steps: a path chosen to make room is first marked
(the list file data/offload.list, which the backup reads before copying, and from then on keeps what it names however
the server's copy changes), and each of its files recorded, with its size, time and SHA-1 (offload_files); it's deleted
once a manifest from the backup confirms it holds each of those files as recorded. After each night's copy, the backup's
handshake (deploy/backup-handshake.py) leaves its manifest in data/backup-manifest.txt: every file it holds under the
paths listed, with its size and time, and the SHA-1s of a few (data/offload-sample.list, a rotating sample, SAMPLE_BYTES
a night). check_manifest() compares it with what was recorded: a file offloaded that it lacks, or holds otherwise, is
lost. A path wanted back is listed in data/offload-wanted.list; the backup's next run sends it into data/restore-inbox/,
and take_restores() puts it back.

Paths are relative to the data folder, and only of the kinds the backup's handshake takes back: a project's photos/,
masks/ or rejected/ (a folder, emptied rather than removed, so that what looks for it still finds it), a fetched video
in cache/videos/, or one file of a mesh in meshes/<id>/.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable

LIST, WANTED, MARK, INBOX = "offload.list", "offload-wanted.list", "backed-up", "restore-inbox"
SAMPLE, MANIFEST = "offload-sample.list", "backup-manifest.txt"
SAMPLE_BYTES = 512 * 2**20      # how much of what's offloaded the backup reads back each night, to check its SHA-1s
PATH = re.compile(r"projects/[0-9a-f]{6,32}/(photos|masks|rejected)|cache/videos/[A-Za-z0-9_-]{11}\.[a-z0-9]{2,5}"
                  r"|meshes/[0-9a-f]{6,32}/[A-Za-z0-9_.-]{1,120}")
SCHEMA = """CREATE TABLE IF NOT EXISTS offloads(path TEXT PRIMARY KEY, kind TEXT, ref TEXT, bytes INTEGER, marked REAL,
              done REAL, wanted REAL)"""
# confirmed: when a manifest last showed the backup holds every file of it as recorded; lost: when one showed it doesn't
# (lost_why says how), after it was deleted here; approved: when the admin page said to go on without it; blocked: the job
# it last stopped, as lost
COLUMNS = (("confirmed", "REAL"), ("lost", "REAL"), ("lost_why", "TEXT"), ("approved", "REAL"), ("blocked", "TEXT"))
# each file of a path marked or offloaded, as it was when marked (or as the backup first said it held it, for one offloaded
# before files were recorded: sha1 then comes from the first sample that reads it); checked: when its SHA-1 was last
# checked against the backup's copy
FILES = """CREATE TABLE IF NOT EXISTS offload_files(path TEXT, file TEXT, bytes INTEGER, mtime REAL, sha1 TEXT, checked REAL,
              PRIMARY KEY(path, file))"""


def migrate(con) -> None:
    """The tables, and the columns added since (con: a sqlite3 connection)."""
    con.execute(SCHEMA)
    con.execute(FILES)
    have = {r[1] for r in con.execute("PRAGMA table_info(offloads)")}
    for c, t in COLUMNS:
        if c not in have:
            con.execute(f"ALTER TABLE offloads ADD COLUMN {c} {t}")

Q = Callable[..., list]


def size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


def files_of(data: Path, path: str) -> list[Path]:
    """The files of a path: itself, or those under a folder."""
    target = data / path
    if target.is_file():
        return [target]
    return sorted(f for f in target.rglob("*") if f.is_file()) if target.is_dir() else []


def sha1_of(f: Path) -> str:
    h = hashlib.sha1()
    with open(f, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def record(data: Path, q: Q, path: str) -> int:
    """Record each file of a path as it is now (size, time, SHA-1), in place of what was, unconfirmed. Returns its
    bytes."""
    q("DELETE FROM offload_files WHERE path=?", path, write=True)
    total = 0
    for f in files_of(data, path):
        st = f.stat()
        q("INSERT INTO offload_files(path, file, bytes, mtime, sha1) VALUES(?,?,?,?,?)", path, str(f.relative_to(data)), st.st_size,
          st.st_mtime, sha1_of(f), write=True)
        total += st.st_size
    q("UPDATE offloads SET confirmed=NULL WHERE path=?", path, write=True)
    return total


def as_recorded(data: Path, q: Q, path: str) -> bool:
    """Whether a path's files here are those recorded (by size and time), and there were some."""
    rec = {r["file"]: r for r in q("SELECT file, bytes, mtime FROM offload_files WHERE path=?", path)}
    now = {str(f.relative_to(data)): f.stat() for f in files_of(data, path)}
    return bool(rec) and set(rec) == set(now) and all(
        now[f].st_size == r["bytes"] and abs(now[f].st_mtime - r["mtime"]) < 1 for f, r in rec.items())


def backed_up_at(data: Path) -> float | None:
    """When the backup's last good copy began, as its handshake said, or None if it never has."""
    try:
        return float((data / MARK).read_text().strip())
    except (OSError, ValueError):
        return None


def sample(q: Q, budget: int = SAMPLE_BYTES) -> list[str]:
    """The files offloaded whose SHA-1s the backup is to read back next: those checked longest ago (never, first), until
    budget bytes. Those with no SHA-1 recorded yet (offloaded before they were) too, to record it."""
    out, total = [], 0
    for r in q("SELECT f.file, f.bytes FROM offload_files f JOIN offloads o ON o.path=f.path WHERE o.done IS NOT NULL "
               "AND o.lost IS NULL ORDER BY f.checked IS NOT NULL, f.checked, f.file"):
        if total + (r["bytes"] or 0) > budget and out:
            break
        out.append(r["file"])
        total += r["bytes"] or 0
    return out


def write_lists(data: Path, q: Q) -> None:
    """The lists the backup reads: every path marked or offloaded (which it keeps), those wanted back, and the files
    whose SHA-1s to read back (sample())."""
    for name, rows in ((LIST, [r["path"] for r in q("SELECT path FROM offloads ORDER BY path")]),
                       (WANTED, [r["path"] for r in q("SELECT path FROM offloads WHERE done IS NOT NULL AND wanted IS NOT NULL "
                                                      "AND lost IS NULL ORDER BY path")]),
                       (SAMPLE, sample(q))):
        tmp = data / (name + ".tmp")
        tmp.write_text("".join(p + "\n" for p in rows))
        tmp.replace(data / name)


def mark(data: Path, q: Q, path: str, kind: str, ref: str) -> int:
    """Mark a path to be offloaded once the backup holds it. Returns its size."""
    if not PATH.fullmatch(path):
        raise ValueError(f"not a path that can be offloaded: {path}")
    if q("SELECT 1 FROM offloads WHERE path=?", path):
        return 0
    n = size(data / path)
    q("INSERT INTO offloads(path, kind, ref, bytes, marked) VALUES(?,?,?,?,?)", path, kind, ref, n, time.time(), write=True)
    record(data, q, path)
    return n


def away(q: Q, path: str) -> bool:
    """Whether a path's files are only in the backup now (not one lost there that the admin page said to go on without)."""
    return bool(q("SELECT 1 FROM offloads WHERE path=? AND done IS NOT NULL AND approved IS NULL", path))


def lost(q: Q, paths) -> list[dict]:
    """Of these paths, those lost from the backup that nothing has been approved to go on without: {path, kind, ref,
    lost, lost_why}."""
    out = []
    for p in paths:
        out += q("SELECT path, kind, ref, lost, lost_why FROM offloads WHERE path=? AND lost IS NOT NULL AND approved IS NULL", p)
    return out


def drop(q: Q, paths) -> None:
    """Forget paths marked or offloaded, as when what they named is made again (a climb harvested again)."""
    for p in paths:
        q("DELETE FROM offloads WHERE path=?", p, write=True)
        q("DELETE FROM offload_files WHERE path=?", p, write=True)


def finish(data: Path, q: Q, still: Callable[[dict], bool], log: Callable[[str], None], need: float | None = None) -> int:
    """Delete what a manifest from the backup confirmed it holds, each file as recorded here, if it's still to go
    (still(row)) and its files are still as recorded, in the order marked, until need bytes are freed (all, if None);
    unmark what's no longer to go. One whose files changed since is recorded again, to be confirmed again. Returns the
    bytes freed."""
    freed = 0
    for row in q("SELECT * FROM offloads WHERE done IS NULL ORDER BY marked"):
        if not still(row):
            drop(q, [row["path"]])
            continue
        if need is not None and freed >= need:
            continue
        if not as_recorded(data, q, row["path"]):
            record(data, q, row["path"])     # changed since it was marked (or marked before files were recorded)
            continue
        if not row["confirmed"]:
            continue        # no manifest has shown the backup holds it, as it is
        target = data / row["path"]
        n = size(target)
        if target.is_dir():
            for f in list(target.iterdir()):
                shutil.rmtree(f) if f.is_dir() else f.unlink()
        else:
            target.unlink(missing_ok=True)
        q("UPDATE offloads SET done=?, bytes=? WHERE path=?", time.time(), n or row["bytes"], row["path"], write=True)
        freed += n
    if freed:
        log(f"offload: {freed / 2**20:.0f} MB the backup holds deleted here")
    return freed


def want(data: Path, q: Q, paths) -> list[str]:
    """Ask for paths offloaded to be brought back, on the backup's next run. Returns those that are away."""
    asked = []
    for p in paths:
        if away(q, p):
            q("UPDATE offloads SET wanted=COALESCE(wanted, ?) WHERE path=?", time.time(), p, write=True)
            asked.append(p)
    if asked:
        write_lists(data, q)
    return asked


def take_restores(data: Path, q: Q, log: Callable[[str], None]) -> list[str]:
    """Put back what the backup sent into the inbox (the handshake's restore), for paths wanted back. Returns them."""
    inbox, back = data / INBOX, []
    if not inbox.is_dir():
        return back
    for row in q("SELECT path FROM offloads WHERE done IS NOT NULL AND wanted IS NOT NULL"):
        src, dst = inbox / row["path"], data / row["path"]
        if not src.exists():
            continue
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                if f.is_file():
                    os.replace(f, dst / f.name)
            shutil.rmtree(src, ignore_errors=True)
            times = [f.stat().st_mtime for f in dst.iterdir() if f.is_file()]
            if times:   # as it was before (the files keep theirs): what compares a harvest's photos with a pool's sees no change
                os.utime(dst, (max(times), max(times)))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
        drop(q, [row["path"]])
        back.append(row["path"])
    if back:
        write_lists(data, q)
        log(f"offload: {len(back)} put back from the backup")
    for leftover in sorted(inbox.rglob("*"), reverse=True):     # anything not wanted, or emptied folders
        if leftover.is_dir():
            try:
                leftover.rmdir()
            except OSError:
                pass
    return back


def manifest_of(root: Path, paths, sample_files=(), began: float | None = None) -> str:
    """A manifest, as deploy/backup.sh writes one of its copy (root, the server's data/ there): "began <time>"; a line
    "f<TAB>bytes<TAB>mtime<TAB>file" for each file under each path it holds; "h<TAB>SHA-1<TAB>file" for each of the sample
    it holds; "end <how many f lines>"."""
    lines = [f"began {began if began is not None else time.time()}"]
    n = 0
    for p in paths:
        for f in files_of(root, p):
            st = f.stat()
            lines.append(f"f\t{st.st_size}\t{st.st_mtime}\t{f.relative_to(root)}")
            n += 1
    for name in sample_files:
        if (root / name).is_file():
            lines.append(f"h\t{sha1_of(root / name)}\t{name}")
    return "\n".join(lines + [f"end {n}"]) + "\n"


def read_manifest(text: str) -> dict | None:
    """A manifest's {began, files: {file: (bytes, mtime)}, sha1: {file: SHA-1}}, or None if it isn't whole."""
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].startswith("began ") or not lines[-1].startswith("end "):
        return None
    try:
        began, count = float(lines[0][6:]), int(lines[-1][4:])
    except ValueError:
        return None
    files, sha1 = {}, {}
    for line in lines[1:-1]:
        parts = line.split("\t")
        try:
            if parts[0] == "f" and len(parts) == 4:
                files[parts[3]] = (int(parts[1]), float(parts[2]))
            elif parts[0] == "h" and len(parts) == 3 and re.fullmatch(r"[0-9a-f]{40}", parts[1]):
                sha1[parts[2]] = parts[1]
            else:
                return None
        except ValueError:
            return None
    return {"began": began, "files": files, "sha1": sha1} if len(files) == count else None


def check_manifest(data: Path, q: Q, got: dict) -> dict:
    """Compare a manifest (read_manifest()) with what was recorded of each path marked or offloaded. A path offloaded
    with a file it lacks, or holds with another size or time, or whose SHA-1 differs from the one recorded, is lost (unless
    it's here again, as when made again since). One marked, all of whose files it holds as recorded, is confirmed: it may
    go (finish()); any other isn't, until a manifest shows the backup holds it. A path offloaded before its files were
    recorded takes what the backup holds of it as its record, with each SHA-1 from its first sample; holding none of it,
    it's lost.

    Returns:
        {lost: [paths newly lost], confirmed: how many marked are, hashes: how many SHA-1s checked, files: in the manifest}.
    """
    now, out = time.time(), {"lost": [], "confirmed": 0, "hashes": 0, "files": len(got["files"])}
    under: dict[str, list[str]] = {}
    for f in got["files"]:
        top = f if PATH.fullmatch(f) else "/".join(f.split("/")[:3])     # a file itself, or one under a project's folder
        under.setdefault(top, []).append(f)
    for row in q("SELECT * FROM offloads"):
        path = row["path"]
        rec = {r["file"]: r for r in q("SELECT * FROM offload_files WHERE path=?", path)}
        if not rec and row["done"]:
            held = under.get(path, [])
            for f in held:
                q("INSERT INTO offload_files(path, file, bytes, mtime) VALUES(?,?,?,?)", path, f, *got["files"][f], write=True)
            rec = {r["file"]: r for r in q("SELECT * FROM offload_files WHERE path=?", path)}
            why = None if held else "the backup holds none of it"
        else:
            why = None
        missing = [f for f in rec if f not in got["files"]]
        differ = [f for f, r in rec.items() if f in got["files"] and (got["files"][f][0] != r["bytes"] or abs(got["files"][f][1] - r["mtime"]) >= 1)]
        bad = []
        for f, r in rec.items():
            h = got["sha1"].get(f)
            if not h:
                continue
            out["hashes"] += 1
            if r["sha1"] and r["sha1"] != h:
                bad.append(f)
            q("UPDATE offload_files SET sha1=COALESCE(sha1, ?), checked=? WHERE path=? AND file=?", h, now, path, f, write=True)
        if missing or differ or bad:
            why = "; ".join(x for x in (
                f"{len(missing)} of its {len(rec)} file(s) missing ({missing[0]}{', …' if len(missing) > 1 else ''})" if missing else "",
                f"{len(differ)} held with another size or time ({differ[0]}{', …' if len(differ) > 1 else ''})" if differ else "",
                f"{len(bad)} with other contents ({bad[0]}{', …' if len(bad) > 1 else ''})" if bad else "") if x)
        if not row["done"]:
            ok = not why and bool(rec)
            q("UPDATE offloads SET confirmed=? WHERE path=?", now if ok else None, path, write=True)
            out["confirmed"] += ok
        elif why and not row["lost"]:
            q("UPDATE offloads SET lost=?, lost_why=? WHERE path=?", now, why, path, write=True)
            out["lost"].append(path)
        elif not why and row["lost"] and not row["approved"]:
            q("UPDATE offloads SET lost=NULL, lost_why=NULL, blocked=NULL WHERE path=?", path, write=True)  # it has it after all
    return out


def state(data: Path, q: Q) -> dict:
    """For the admin page: what's offloaded, marked, and wanted back, by kind, when the backup last said it held it all,
    and what it lost."""
    out = {"backed_up": backed_up_at(data), "away": {}, "marked": {}, "wanted": 0}
    for r in q("SELECT kind, done IS NOT NULL AS done, COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS bytes FROM offloads "
               "WHERE lost IS NULL GROUP BY kind, done IS NOT NULL"):
        out["away" if r["done"] else "marked"][r["kind"]] = {"n": r["n"], "bytes": r["bytes"]}
    out["wanted"] = q("SELECT COUNT(*) AS n FROM offloads WHERE done IS NOT NULL AND wanted IS NOT NULL AND lost IS NULL")[0]["n"]
    out["confirmed"] = q("SELECT COUNT(*) AS n FROM offloads WHERE done IS NULL AND confirmed IS NOT NULL")[0]["n"]
    out["lost"] = q("SELECT path, kind, ref, bytes, lost, lost_why, approved, blocked FROM offloads WHERE lost IS NOT NULL ORDER BY lost")
    return out
