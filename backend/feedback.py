"""Comments and bug reports visitors leave on the site's "?" tab: cleaning what they typed, and the file it's kept in.

Anyone may send one, so nothing typed is trusted: it's kept as plain text (JSON lines in DATA_DIR/feedback.jsonl, never
HTML), without control and invisible formatting characters, and no longer than the form allows. Whatever shows it
(the admin page, a notification) escapes it or shows it as text. The form's hidden "website" field is left empty by
people and filled in by bots that fill in every field: a comment with it filled in is dropped, and answered as if kept.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path

KINDS = ("comment", "bug")
MESSAGE = 4000          # characters of a comment
CONTACT = 200           # of how to reach its sender, if they said
HONEYPOT = "website"    # the field people don't see
REQUEST_BYTES = 32768   # the largest request taken: a full comment in a script of 4 bytes a character, as JSON
FILE_BYTES = 2 * 2**20  # how big the file gets before a new one is started, the old one kept as <name>.1
NOTE_CHARS = 600        # of a comment in a notification


def clean(value: str, lines: bool = False) -> str:
    """Text as typed, made plain: no control characters, invisible formatting characters (which can reorder or hide
    text), private-use or unpaired ones; every kind of space a space; line breaks kept (at most one empty line running)
    if lines, else spaces too."""
    out = []
    for ch in unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")):
        cat = unicodedata.category(ch)
        if ch == "\n" or cat in ("Zl", "Zp"):
            out.append("\n" if lines else " ")
        elif ch == "\t" or cat == "Zs":
            out.append(" ")
        elif cat not in ("Cc", "Cf", "Cs", "Co"):
            out.append(ch)
    text = "".join(out)
    if not lines:
        return re.sub(r" {2,}", " ", text).strip()
    return re.sub(r"\n{3,}", "\n\n", re.sub(r" +\n", "\n", text)).strip()


def parse(report: dict) -> dict | None:
    """A comment as the form sent it, cleaned for keeping: {kind, message, contact, page}; None for one a bot sent (its
    hidden field filled in).

    Raises:
        ValueError: for one that isn't text, is empty once cleaned, or is longer than the form allows.
    """
    for k in ("kind", "message", "contact", "page", HONEYPOT):
        if not isinstance(report.get(k, ""), str):
            raise ValueError(f"{k} is text")
    if report.get(HONEYPOT):
        return None
    raw, contact = report.get("message", ""), report.get("contact", "")
    if len(raw) > MESSAGE:
        raise ValueError(f"a comment is up to {MESSAGE} characters")
    if len(contact) > CONTACT:
        raise ValueError(f"how to reach you is up to {CONTACT} characters")
    message = clean(raw, lines=True)
    if not message:
        raise ValueError("a comment needs some text")
    kind = report.get("kind") or "comment"
    if kind not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)}")
    page = clean(report.get("page", "")[:300])
    return {"kind": kind, "message": message, "contact": clean(contact) or None,
            "page": page if re.fullmatch(r"/[^\s\"'<>\\]*", page) else None}     # the site's own path and #, or nothing


def keep(path: Path, line: dict) -> None:
    """Append a comment to the file, starting a new one once it's over FILE_BYTES."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > FILE_BYTES:
        path.replace(path.with_name(path.name + ".1"))
    with open(path, "a") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **{k: v for k, v in line.items() if v is not None}}) + "\n")


def recent(path: Path, most: int = 30) -> dict:
    """The newest comments kept, newest first, and how many the file holds, for the admin page."""
    try:
        rows = path.read_text().splitlines()
    except OSError:
        rows = []
    out = []
    for row in reversed(rows[-most:]):
        try:
            out.append(json.loads(row))
        except ValueError:
            pass
    return {"count": len(rows), "recent": out, "file": path.name}


def note(line: dict) -> tuple[str, str]:
    """A notification's title and plain text for a comment: its start, how to reach its sender, and the page it was on."""
    text = line["message"] if len(line["message"]) <= NOTE_CHARS else line["message"][:NOTE_CHARS].rstrip() + "…"
    more = [f"{label}: {line[k]}" for k, label in (("contact", "Reach them"), ("page", "Page")) if line.get(k)]
    return ("BoulderMesh: a bug report" if line["kind"] == "bug" else "BoulderMesh: a comment"), "\n".join([text, *([""] if more else []), *more])
