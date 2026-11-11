"""Clients that register themselves: volunteers fetching videos (volunteers.py) and machines meshing (pullwork.py).

Each script makes its own token, asks whoever runs it for a name (optional), and registers the token here, of which
only the hash is kept. A token registered so is provisional until approved, on the admin page or from a phone
notification (notify.py); the admin page's settings say whether provisional ones get work meanwhile, and how much. Tokens
made on the admin page are approved from the start.

Anyone who can reach the site can register, so: each address may register only so many a day (generously, so that
someone who lost a token can make another), only so many may wait for approval at once, and those never approved are
dropped after WAITING_DAYS without being heard from.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

WAITING_DAYS = 14       # a provisional client not heard from for this long is dropped
NAME_MAX = 40
TOKEN = re.compile(r"[A-Za-z0-9_-]{24,200}")
COLUMNS = {"status": "TEXT", "registered_ip": "TEXT", "approved": "REAL"}   # what registering needs of a table of clients
_lock = threading.Lock()


class Refused(Exception):
    """Registering refused for now: code is the HTTP status (429 for an address over its limit, 503 when too many wait)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def migrate(con, table: str) -> None:
    """Add what registering keeps to a table of clients made before it; their rows count as approved."""
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, kind in COLUMNS.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def provisional(row: dict | None) -> bool:
    return bool(row) and row.get("status") == "provisional" and not row.get("revoked")


def clean_name(raw, default: str) -> str:
    """A name as its client gave it: stripped, each run of whitespace within it one '-', without control characters,
    and at most NAME_MAX long; default if nothing's left."""
    s = re.sub(r"\s+", "-", str(raw or "").strip())
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)[:NAME_MAX].strip("-")
    return s or default


def unique_name(name: str, taken) -> str:
    """name, or the first of name-2, name-3, ... not taken (whatever its case)."""
    lower = {str(t).lower() for t in taken}
    if name.lower() not in lower:
        return name
    n = 2
    while f"{name}-{n}".lower() in lower:
        n += 1
    return f"{name}-{n}"


def register(q: Callable, table: str, token: str, name, ip: str, default: str, per_address: int, max_waiting: int,
             extra: dict | None = None) -> dict:
    """Register a client's token, provisional; a token already known is returned as it is (registering again is
    harmless).

    Args:
        q: the table's module's query function, q(sql, *args, one=False).
        default: the name for a client that gave none.
        per_address: how many may be registered from one address in a day (0: any number).
        max_waiting: how many provisional ones may wait for approval at once (0: any number).
        extra: more columns for the new row.

    Returns:
        The client's row.

    Raises:
        ValueError: not a token; Refused: over a limit.
    """
    if not TOKEN.fullmatch(token or ""):
        raise ValueError("a token is 24 to 200 letters, digits, - and _")
    h, now = token_hash(token), time.time()
    with _lock:
        q(f"DELETE FROM {table} WHERE status='provisional' AND revoked IS NULL AND COALESCE(last_seen, created)<?",
          now - WAITING_DAYS * 86400)
        old = q(f"SELECT * FROM {table} WHERE token_hash=?", h, one=True)
        if old:
            return old
        if per_address and len(q(f"SELECT 1 FROM {table} WHERE registered_ip=? AND created>?", ip, now - 86400)) >= per_address:
            raise Refused(429, f"{per_address} a day is the most that may register from one address: try again tomorrow")
        if max_waiting and len(q(f"SELECT 1 FROM {table} WHERE status='provisional' AND revoked IS NULL")) >= max_waiting:
            raise Refused(503, "too many are waiting for approval just now: try again later")
        rid = secrets.token_hex(4)
        cols = {"id": rid, "name": unique_name(clean_name(name, default), (r["name"] for r in q(f"SELECT name FROM {table}"))),
                "token_hash": h, "created": now, "status": "provisional", "registered_ip": ip, "last_ip": ip, **(extra or {})}
        q(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", *cols.values())
        return q(f"SELECT * FROM {table} WHERE id=?", rid, one=True)


_served: dict[str, tuple] = {}


def served_script(path: Path) -> dict | None:
    """The client script this serves, as one updating itself checks it: {version (its VERSION line), sha256}; None if
    it can't be read."""
    try:
        st = path.stat()
        key = (st.st_mtime, st.st_size)
        if _served.get(str(path), (None,))[0] != key:
            data = path.read_bytes()
            m = re.search(rb"^VERSION = (\d+)\b", data, re.M)
            _served[str(path)] = (key, {"version": int(m[1]), "sha256": hashlib.sha256(data).hexdigest()} if m else None)
        return _served[str(path)][1]
    except OSError:
        return None


def script_news(path: Path, body: dict) -> dict:
    """What a client script asking for work is told of the one this serves, if that's newer: {"update": {version,
    sha256}} when it said it updates itself ("updates"), to do now, instead of any work; or else {"newer": version}, for
    it to mention. Scripts from before updates don't ask, and get work as they always did."""
    served = served_script(path)
    try:
        theirs = int(body.get("version") or 0)
    except (TypeError, ValueError):
        theirs = 0
    if not served or theirs >= served["version"]:
        return {}
    return {"update": served} if body.get("updates") is True else {"newer": served["version"]}


def too_old(path: Path, where: str) -> dict:
    """The 426 answer for a script older than the server speaks to: with how to update, for one that updates itself."""
    served = served_script(path)
    return {"error": f"this script is too old: download it again from {where}", **({"update": served} if served else {})}


def approve(q: Callable, table: str, rid: str) -> dict | None:
    """Approve a provisional client; its row afterwards, or None for one unknown or revoked."""
    q(f"UPDATE {table} SET status='approved', approved=? WHERE id=? AND revoked IS NULL AND status='provisional'", time.time(), rid)
    row = q(f"SELECT * FROM {table} WHERE id=?", rid, one=True)
    return row if row and not row.get("revoked") else None
