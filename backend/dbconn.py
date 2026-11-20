"""SQLite connections that close as their with block ends.

A connection's own with block only commits or rolls back; the connection, and its open file, last until the garbage
collector frees it. A busy minute of mesh lists, each hundreds of queries, so ran the server out of open files on
Nov 12 (Errno 24). These close then instead.
"""
from __future__ import annotations

import sqlite3


class Closing(sqlite3.Connection):
    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def connect(path, **kw) -> sqlite3.Connection:
    """sqlite3.connect(), as a connection that closes as its with block ends."""
    return sqlite3.connect(path, factory=Closing, **kw)


def use_wal(path) -> str | None:
    """Put a database in write-ahead-log mode, which stays set in the file: readers then never block writers, nor writers
    readers. In the rollback journal's mode (SQLite's default) a read held open for minutes, as an analysis script looping
    over a table on Nov 19, 2026, made the server's writes fail with "database is locked", and four meshing jobs with
    them. Returns the mode it's in, or None if it couldn't be changed now (another connection was busy)."""
    try:
        with connect(path, timeout=30) as con:
            return con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    except sqlite3.OperationalError:
        return None
