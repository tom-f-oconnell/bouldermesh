"""Telling of the clients that come and go: machines lending themselves (pullwork.py) and volunteers fetching videos
(volunteers.py). Only when one starts being heard from, or stops (saying goodbye, or going quiet for longer than it
would while working), not as it's busy, idle or held back; and for each one at most once in WINDOW, so that one that
drops out and back in a few times tells only where it ended up, at the end of the window.

Each table of clients gets: active_at (when it last asked for work or sent any, not a token check), bye_at (when it last
said goodbye), and told / told_at (what was last told of it, "here" or "gone", and when).
"""
from __future__ import annotations

import time
from typing import Callable

WINDOW = 3600       # seconds within which one client is told of once
GRACE = 120         # a goodbye counts once it's this old, so that a script restarted (an update, systemctl) is still here

COLUMNS = {"active_at": "REAL", "bye_at": "REAL", "told": "TEXT", "told_at": "REAL"}


def migrate(con, table: str, recent: float) -> None:
    """Add the columns. Clients known before start as told of as they are (here if last seen within recent seconds), so
    that no one is told they all joined."""
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, kind in COLUMNS.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    if "told" not in have:
        now = time.time()
        con.execute(f"UPDATE {table} SET active_at=last_seen, told=CASE WHEN last_seen>? THEN 'here' ELSE 'gone' END", (now - recent,))


def here(row: dict, now: float, quiet: float) -> bool:
    """Whether a client counts as here: heard from within quiet seconds, and not since saying goodbye (GRACE ago)."""
    active, bye = row.get("active_at") or 0, row.get("bye_at") or 0
    return not row.get("revoked") and active > now - quiet and (bye < active or bye > now - GRACE)


def sweep(q: Callable, table: str, quiet: Callable[[dict], float], tell: Callable[[dict, str, str], None],
          now: float | None = None) -> list[tuple[str, str]]:
    """Tell of each client whose being here or gone differs from what was last told of it, if that was WINDOW ago (or
    never); one that registered itself within WINDOW was told of by its registration. Returns [(name, "here"|"gone")].

    Args:
        quiet: how long a client may go unheard from while still here, for its row (its script's way of keeping in touch).
        tell: called with (row, "here" or "gone", why).
    """
    now = now or time.time()
    told = []
    for row in q(f"SELECT * FROM {table} WHERE revoked IS NULL"):
        state = "here" if here(row, now, quiet(row)) else "gone"
        if row.get("told") is None and row.get("registered_ip") is not None and (row.get("created") or 0) > now - WINDOW:
            q(f"UPDATE {table} SET told=?, told_at=? WHERE id=?", state, now, row["id"])   # its registration said so
            continue
        if state == (row.get("told") or "gone") or (row.get("told_at") and now - row["told_at"] < WINDOW):
            continue
        if state == "gone":
            why = ("it said goodbye" if (row.get("bye_at") or 0) >= (row.get("active_at") or 0) else
                   f"not heard from since {time.strftime('%H:%M', time.localtime(row.get('active_at') or now))}")
        else:
            why = ""
        q(f"UPDATE {table} SET told=?, told_at=? WHERE id=?", state, now, row["id"])
        try:
            tell(row, state, why)
        except Exception:  # noqa: S110 - telling someone mustn't stop the rest
            pass
        told.append((row["name"], state))
    return told
