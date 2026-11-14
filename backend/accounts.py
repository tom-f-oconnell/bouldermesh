"""People with accounts on the site: who they are, what they may do, and how a browser or phone shows it's theirs.

An account is made from an invite (a link the admin page gives, good once, for a while): whoever opens it picks a name
and their browser is given a token, kept there and sent as "Authorization: Bearer <token>", as the site's own editing
token is. No passwords and no email. Another device (the phone app) gets a token of its own from a pairing code a
signed-in one asks for. Only hashes of tokens, invites and codes are kept.

Roles: "owner" (everything; whoever holds the site's EDIT_TOKEN is the owner too, with no account), "moderator" (may
approve what's added to the areas they're given, `areas`: campaign ids, or "*" for all), "user". What a user's token may
do is a short list of routes in server.py (ALLOWED there): anything not on it still needs the owner.

The terms are accepted once, before anything is sent (TERMS, TERMS_VERSION): an account that hasn't accepted the
current version is refused uploads, and the apps don't go past their first screen without it.

Kept in its own database (data/accounts.sqlite), as machines' and volunteers' are.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

ROLES = ("owner", "moderator", "user")
RANK = {"owner": 0, "moderator": 1, "user": 2}      # whose work goes first: the owner's, moderators', then users'
NAME_MAX = 40
INVITE_DAYS = 14
PAIR_SECONDS = 600
TOKENS_MOST = 8             # devices an account may have signed in at once: the oldest goes when another joins
LICENCE = "CC-BY-4.0"
TERMS_VERSION = 1
TERMS = (
    "BoulderMesh is a community project: using it to capture or add to a rock means sharing what you add.\n\n"
    "Everything you capture with the app or upload to the site (photos, videos, the readings stored with them, and beta "
    "you choose to share), and whatever is made from it alone (meshes, textures, models of holds), is published under "
    "the Creative Commons Attribution 4.0 International licence (CC BY 4.0): the site and anyone else may copy, change "
    "and reuse it, for any purpose, as long as they credit you by the name you give.\n\n"
    "Only add what is yours to license this way. What you add is reviewed before it is shown, and may be declined or "
    "removed. Meshes that also draw on other people's photos stay under those photos' own terms."
)


class Refused(Exception):
    """An invite, a code or a change that isn't taken: the message is for whoever asked."""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def clean_name(raw) -> str:
    name = " ".join(str(raw or "").split())[:NAME_MAX]
    if len(name) < 2:
        raise Refused("a name of at least two characters is needed: it's what your photos are credited to")
    return name


class Accounts:
    """The accounts database. Safe to use from several threads."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT UNIQUE, role TEXT, areas TEXT, created REAL,
                    invited_by TEXT, quota_mb REAL, terms INTEGER, terms_at REAL, disabled INTEGER DEFAULT 0, last_seen REAL);
                CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, user_id TEXT, label TEXT, created REAL, last_seen REAL);
                CREATE TABLE IF NOT EXISTS invites(hash TEXT PRIMARY KEY, role TEXT, areas TEXT, note TEXT, created REAL,
                    expires REAL, used_by TEXT, used_at REAL);
                CREATE TABLE IF NOT EXISTS pairings(hash TEXT PRIMARY KEY, user_id TEXT, expires REAL);
            """)

    def _con(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def _q(self, sql: str, *args, one=False):
        with self.lock, self._con() as con:
            rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        return (rows[0] if rows else None) if one else rows

    @staticmethod
    def _user(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {**row, "areas": json.loads(row["areas"]) if row.get("areas") else [], "disabled": bool(row["disabled"]),
                "accepted": row.get("terms") == TERMS_VERSION}

    # ---- invites -------------------------------------------------------------------------------------------------------
    def invite(self, role: str = "user", areas: list | None = None, note: str | None = None, days: float = INVITE_DAYS,
               now: float | None = None) -> dict:
        """A new invite: {code, expires}. The code is shown once and not kept."""
        if role not in ("moderator", "user"):
            raise Refused("an invite is for a user or a moderator")
        now = time.time() if now is None else now
        code = secrets.token_urlsafe(16)
        self._q("INSERT INTO invites(hash, role, areas, note, created, expires) VALUES(?,?,?,?,?,?)", _hash(code), role,
                json.dumps(sorted({str(a) for a in areas or []})), (note or "")[:80] or None, now, now + days * 86400)
        return {"code": code, "expires": now + days * 86400, "role": role}

    def invites(self, now: float | None = None) -> list[dict]:
        """Invites not yet used or run out, newest first, without their codes."""
        now = time.time() if now is None else now
        return self._q("SELECT substr(hash, 1, 8) AS id, role, areas, note, created, expires FROM invites "
                       "WHERE used_by IS NULL AND expires > ? ORDER BY created DESC", now)

    def join(self, code, name, label: str | None = None, now: float | None = None) -> dict:
        """Make an account from an invite. Returns {token, user}.

        Raises:
            Refused: the invite isn't one, was used or has run out; or the name is too short or taken.
        """
        now = time.time() if now is None else now
        name = clean_name(name)
        token, uid = secrets.token_urlsafe(32), secrets.token_hex(6)
        with self.lock, self._con() as con:
            inv = con.execute("SELECT * FROM invites WHERE hash=?", (_hash(str(code or "")),)).fetchone()
            if inv is None or inv["used_by"] or inv["expires"] <= now:
                raise Refused("that invite isn't good: it was used already, has run out, or was mistyped")
            if con.execute("SELECT 1 FROM users WHERE lower(name)=lower(?)", (name,)).fetchone():
                raise Refused("that name is taken: pick another")
            con.execute("INSERT INTO users(id, name, role, areas, created, invited_by, last_seen) VALUES(?,?,?,?,?,?,?)",
                        (uid, name, inv["role"], inv["areas"], now, inv["hash"][:8], now))
            con.execute("UPDATE invites SET used_by=?, used_at=? WHERE hash=?", (uid, now, inv["hash"]))
            con.execute("INSERT INTO tokens(hash, user_id, label, created, last_seen) VALUES(?,?,?,?,?)", (_hash(token), uid, (label or "")[:60] or None, now, now))
        return {"token": token, "user": self.get(uid)}

    # ---- who a request is from -----------------------------------------------------------------------------------------
    def get(self, uid: str) -> dict | None:
        return self._user(self._q("SELECT * FROM users WHERE id=?", uid, one=True))

    def user_for(self, token: str | None, now: float | None = None) -> dict | None:
        """The account a token is for, if it's one and the account isn't disabled."""
        if not token:
            return None
        now = time.time() if now is None else now
        h = _hash(token)
        row = self._q("SELECT u.*, t.last_seen AS token_seen FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.hash=?", h, one=True)
        if row is None or row["disabled"]:
            return None
        if now - (row.pop("token_seen") or 0) > 60:
            self._q("UPDATE tokens SET last_seen=? WHERE hash=?", now, h)
            self._q("UPDATE users SET last_seen=? WHERE id=?", now, row["id"])
        return self._user(row)

    def users(self) -> list[dict]:
        rows = self._q("SELECT u.*, (SELECT COUNT(*) FROM tokens t WHERE t.user_id = u.id) AS devices FROM users u ORDER BY u.created")
        return [self._user(r) for r in rows]

    # ---- changing one --------------------------------------------------------------------------------------------------
    def change(self, uid: str, changes: dict) -> dict:
        """Change an account's role, areas, quota_mb (None: the site's default), disabled or name: those given."""
        user = self.get(uid)
        if user is None:
            raise Refused("there's no such account")
        if "role" in changes:
            if changes["role"] not in ROLES:
                raise Refused("a role is one of " + ", ".join(ROLES))
            self._q("UPDATE users SET role=? WHERE id=?", changes["role"], uid)
        if "areas" in changes:
            self._q("UPDATE users SET areas=? WHERE id=?", json.dumps(sorted({str(a) for a in changes["areas"] or []})), uid)
        if "quota_mb" in changes:
            quota = changes["quota_mb"]
            if quota is not None and (isinstance(quota, bool) or not isinstance(quota, (int, float)) or quota < 0):
                raise Refused("a quota is megabytes, or nothing for the site's own")
            self._q("UPDATE users SET quota_mb=? WHERE id=?", quota, uid)
        if "disabled" in changes:
            self._q("UPDATE users SET disabled=? WHERE id=?", int(bool(changes["disabled"])), uid)
            if changes["disabled"]:
                self.sign_out(uid)
        if "name" in changes:
            name = clean_name(changes["name"])
            if self._q("SELECT 1 FROM users WHERE lower(name)=lower(?) AND id<>?", name, uid):
                raise Refused("that name is taken: pick another")
            self._q("UPDATE users SET name=? WHERE id=?", name, uid)
        return self.get(uid)

    def accept(self, uid: str, version, now: float | None = None) -> dict:
        """The account accepts the terms as they stand. Raises Refused for any other version: they've changed since it read them."""
        if version != TERMS_VERSION:
            raise Refused("the terms have changed: read them again")
        self._q("UPDATE users SET terms=?, terms_at=? WHERE id=?", TERMS_VERSION, time.time() if now is None else now, uid)
        return self.get(uid)

    def sign_out(self, uid: str, token: str | None = None) -> int:
        """Forget an account's tokens: the one given, or all of them. Returns how many."""
        before = len(self._q("SELECT 1 FROM tokens WHERE user_id=?", uid))
        if token:
            self._q("DELETE FROM tokens WHERE user_id=? AND hash=?", uid, _hash(token))
        else:
            self._q("DELETE FROM tokens WHERE user_id=?", uid)
        return before - len(self._q("SELECT 1 FROM tokens WHERE user_id=?", uid))

    # ---- another device ------------------------------------------------------------------------------------------------
    def pair_code(self, uid: str, now: float | None = None) -> dict:
        """A code a signed-in device asks for, for another to sign in with: good once, for PAIR_SECONDS."""
        now = time.time() if now is None else now
        code = secrets.token_urlsafe(9)
        self._q("DELETE FROM pairings WHERE user_id=? OR expires<=?", uid, now)
        self._q("INSERT INTO pairings(hash, user_id, expires) VALUES(?,?,?)", _hash(code), uid, now + PAIR_SECONDS)
        return {"code": code, "expires": now + PAIR_SECONDS}

    def take_pair(self, code, label: str | None = None, now: float | None = None) -> dict:
        """Sign a device in with a pairing code. Returns {token, user}. Raises Refused if the code isn't good."""
        now = time.time() if now is None else now
        h = _hash(str(code or ""))
        with self.lock, self._con() as con:
            row = con.execute("SELECT * FROM pairings WHERE hash=?", (h,)).fetchone()
            con.execute("DELETE FROM pairings WHERE hash=? OR expires<=?", (h, now))
            if row is None or row["expires"] <= now:
                raise Refused("that code isn't good: it was used already or has run out")
            user = con.execute("SELECT disabled FROM users WHERE id=?", (row["user_id"],)).fetchone()
            if user is None or user["disabled"]:
                raise Refused("that account is closed")
            token = secrets.token_urlsafe(32)
            con.execute("INSERT INTO tokens(hash, user_id, label, created, last_seen) VALUES(?,?,?,?,?)", (_hash(token), row["user_id"], (label or "")[:60] or None, now, now))
            old = con.execute("SELECT hash FROM tokens WHERE user_id=? ORDER BY created DESC LIMIT -1 OFFSET ?", (row["user_id"], TOKENS_MOST)).fetchall()
            con.executemany("DELETE FROM tokens WHERE hash=?", [(o["hash"],) for o in old])
        return {"token": token, "user": self.get(row["user_id"])}


def may_moderate(user: dict | None, area: str | None) -> bool:
    """Whether an account may approve what's added to an area: the owner any, a moderator those it's given ("*": all)."""
    if not user or user.get("disabled"):
        return False
    if user["role"] == "owner":
        return True
    return user["role"] == "moderator" and ("*" in user["areas"] or (area is not None and str(area) in user["areas"]))


def public(user: dict) -> dict:
    """An account as it's told about itself."""
    return {k: user.get(k) for k in ("id", "name", "role", "areas", "accepted", "quota_mb", "created")}
