#!/usr/bin/env python3
"""The backup's handshake with this server: what deploy/backup.sh, on the machine holding the backup, may say and ask
here, through a key of its own whose forced command this is (README, "Backups"), in ~/.ssh/authorized_keys:

    command="python3 /home/you/bouldermesh/deploy/backup-handshake.py",restrict ssh-ed25519 AAAA... backup handshake

    backed-up <unix time>   its last copy, begun then, went well: what was marked to offload before then may go
                            (data/backed-up, read by the server's offloading, backend/offload.py)
    report                  how a night's copy went, as JSON on stdin ({status: ok|failed, began, ended, bytes, why,
                            kept: [{day, db, changed}]}), for the admin page (data/backup-report.json)
    wanted                  the paths under data/ wanted back from it (data/offload-wanted.list)
    restore                 a tar of some of those on stdin, unpacked into data/restore-inbox/, which the server puts back
    sample                  the files offloaded whose SHA-1s it's to read back tonight (data/offload-sample.list)
    manifest                what it holds of the paths offloaded, on stdin (backend/offload.py's manifest_of() has its
                            form), kept whole or not at all in data/backup-manifest.txt, which the server checks

The command comes from SSH_ORIGINAL_COMMAND (over SSH) or the arguments (run here). The data folder is the checkout's
data/, or BOULDERMESH_DATA.
"""
from __future__ import annotations

import os
import re
import sys
import tarfile
import time
from pathlib import Path

DATA = Path(os.environ.get("BOULDERMESH_DATA") or Path(__file__).resolve().parents[1] / "data")
PATH = re.compile(r"projects/[0-9a-f]{6,32}/(photos|masks|rejected)|cache/videos/[A-Za-z0-9_-]{11}\.[a-z0-9]{2,5}"
                  r"|meshes/[0-9a-f]{6,32}/[A-Za-z0-9_.-]{1,120}")      # as backend/offload.py's
MOST = 20 * 2**30       # the most a restore unpacks
REPORT_MOST = 64 * 1024     # the most a report may be
MANIFEST_MOST = 256 * 2**20     # the most a manifest may be
FILE = re.compile(r"(projects/[0-9a-f]{6,32}/(photos|masks|rejected)/[^\t\n]+|cache/videos/[A-Za-z0-9_-]{11}\.[a-z0-9]{2,5}"
                  r"|meshes/[0-9a-f]{6,32}/[A-Za-z0-9_.-]{1,120})")     # a file of one of those paths


def wanted() -> list[str]:
    try:
        lines = (DATA / "offload-wanted.list").read_text().split()
    except OSError:
        return []
    return [p for p in lines if PATH.fullmatch(p)]


def restore() -> int:
    """Unpack the tar on stdin into data/restore-inbox/: only files within the paths wanted back."""
    asked = wanted()
    inbox = DATA / "restore-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    n = total = 0
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tar:
        for m in tar:
            name = m.name.lstrip("./")
            if not m.isfile() or ".." in Path(name).parts or not any(name == p or name.startswith(p + "/") for p in asked):
                continue
            total += m.size
            if total > MOST:
                sys.exit(f"more than {MOST / 2**30:.0f} GB: stopped")
            out = inbox / name
            out.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(out.with_suffix(out.suffix + ".part"), "wb") as dst:
                while chunk := src.read(2**20):
                    dst.write(chunk)
            out.with_suffix(out.suffix + ".part").replace(out)
            os.utime(out, (m.mtime, m.mtime))     # as it was, so that it looks unchanged since it was harvested
            n += 1
    print(f"restored {n} file(s) into {inbox}")
    return 0


def report() -> int:
    """Keep what the backup says of its last copy (JSON on stdin), with when it said so, for the admin page."""
    import json
    raw = sys.stdin.buffer.read(REPORT_MOST + 1)
    if len(raw) > REPORT_MOST:
        sys.exit("too long")
    try:
        got = json.loads(raw)
    except ValueError:
        sys.exit("not JSON")
    if not isinstance(got, dict) or got.get("status") not in ("ok", "failed"):
        sys.exit("not a report: {status: ok|failed, ...}")
    keep = {k: got[k] for k in ("status", "began", "ended", "bytes", "why", "kept", "host") if k in got}
    tmp = DATA / "backup-report.json.tmp"
    tmp.write_text(json.dumps({**keep, "received": time.time()}))
    tmp.replace(DATA / "backup-report.json")
    return 0


def sample() -> list[str]:
    try:
        lines = (DATA / "offload-sample.list").read_text().splitlines()
    except OSError:
        return []
    return [p for p in lines if FILE.fullmatch(p) and ".." not in p.split("/")]


def manifest() -> int:
    """Keep the manifest on stdin, if it's whole (a "began" line first, an "end <n>" line last with n its file lines, and
    each line one of those): the server takes it from there."""
    raw = sys.stdin.buffer.read(MANIFEST_MOST + 1)
    if len(raw) > MANIFEST_MOST:
        sys.exit("too long")
    lines = raw.decode("utf-8", "replace").splitlines()
    if len(lines) < 2 or not re.fullmatch(r"began \d{9,11}(\.\d+)?", lines[0]) or not re.fullmatch(r"end \d+", lines[-1]):
        sys.exit("not a manifest: began <time> ... end <n>")
    n = 0
    for line in lines[1:-1]:
        f = line.split("\t")
        if len(f) == 4 and f[0] == "f" and f[1].isdigit() and re.fullmatch(r"\d+(\.\d+)?", f[2]) and FILE.fullmatch(f[3]):
            n += 1
        elif not (len(f) == 3 and f[0] == "h" and re.fullmatch(r"[0-9a-f]{40}", f[1]) and FILE.fullmatch(f[2])):
            sys.exit(f"not a manifest's line: {line[:200]}")
    if n != int(lines[-1][4:]):
        sys.exit(f"{n} file line(s), but its end says {lines[-1][4:]}: not whole")
    tmp = DATA / "backup-manifest.txt.tmp"
    tmp.write_bytes(raw)
    tmp.replace(DATA / "backup-manifest.txt")
    print(f"manifest of {n} file(s) kept")
    return 0


def main() -> int:
    words = (os.environ.get("SSH_ORIGINAL_COMMAND") or " ".join(sys.argv[1:])).split()
    if len(words) == 2 and words[0] == "backed-up" and re.fullmatch(r"\d{9,11}(\.\d+)?", words[1]):
        if float(words[1]) > time.time() + 60:
            sys.exit("that's in the future")
        tmp = DATA / "backed-up.tmp"
        tmp.write_text(words[1] + "\n")
        tmp.replace(DATA / "backed-up")
        return 0
    if words == ["wanted"]:
        print("\n".join(wanted()))
        return 0
    if words == ["restore"]:
        return restore()
    if words == ["report"]:
        return report()
    if words == ["sample"]:
        print("\n".join(sample()))
        return 0
    if words == ["manifest"]:
        return manifest()
    sys.exit("usage: backed-up <unix time> | wanted | restore | report | sample | manifest")


if __name__ == "__main__":
    sys.exit(main())
