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
