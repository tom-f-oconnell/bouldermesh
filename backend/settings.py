"""Settings changed on the admin page, and credentials kept for it.

Settings live in the database's settings table, as JSON, with DEFAULTS for any not set; SITE_TEXTURES and
SITE_SOURCES in the environment change those two defaults, for a copy of the site set up by its compose file
rather than its admin page. Credentials
(cloud keys, an SSH key) are kept apart, in DATA_DIR/secrets.json, readable only by the server's user,
and encrypted with a key that isn't kept in DATA_DIR: BOULDERMESH_SECRETS_KEY, a Fernet key, which
`python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` makes.
Without it, credentials can't be saved from the admin page, and the usual environment variables
(AWS_ACCESS_KEY_ID and so on) are the way to give them. They're never sent back to a browser: the
admin page only learns whether each is set.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from pathlib import Path

import dbconn

DEFAULTS = {
    # compute: where reconstructions run. The server itself runs local_workers of them (0: none, as on a small server
    # that only harvests); with a provider, up to remote_workers more run on other machines at once.
    "compute.local_workers": 1,
    # cores this server's own processing may use (harvests and their off-topic filter, packing meshes, finding beta
    # here): the last of them, so that fewer than all leaves the first for answering visitors; all by default
    "compute.processing_cores": os.cpu_count() or 1,
    "compute.provider": "none",            # none | ssh | aws | pull (machines that connect here for work: pullwork.py)
    "compute.remote_workers": 2,
    "compute.idle_minutes": 10,            # a cloud node with nothing to do is ended after this
    "compute.image": "",                   # the Docker image nodes run, e.g. ghcr.io/you/bouldermesh:latest
    "compute.command": "",                 # how a node runs a job; empty for docker run with the image
    "compute.ssh_hosts": [],               # for ssh: user@host[:port] of machines kept running
    "compute.aws.region": "us-east-1",
    "compute.aws.instance_type": "g4dn.2xlarge",
    "compute.aws.spot": True,
    "compute.aws.ami": "",                 # empty: AWS's latest Deep Learning base GPU AMI, by its SSM parameter
    "compute.aws.security_group": "",      # empty: one is made, open to SSH from this server's address only
    "compute.aws.subnet": "",
    "compute.aws.volume_gb": 100,
    "compute.aws.instance_profile": "",    # for pulling the image from ECR
    # what the main site shows
    "site.textures": "all",                # all | permitted (only from clearly licensed photos) | none
    "site.sources": "all",                 # all | link (non-permitted ones only as links) | permitted
    "site.via_links": True,                # a climb never meshed on its own opens the mesh made for another on its boulder ("via")
    "site.small_thumbs": True,             # with sources link/permitted: small copies of the rest anyway, linking to where they came from
    "site.map_km": 1.0,                    # how many km across (its narrower side) the map opens around a boulder
    "site.donate_url": "",                 # where the "?" tab's Donate button goes (https://…); none while empty (DONATE_URL starts it)
    # the viewer opens a mesh with at least twice as many faces as a simplified copy of about this many first, then the
    # rest (glbpack.py): 150,000 is about 1 MB, and on Caveman looks the same at a phone's size; 0: no previews
    "viewer.preview_faces": 150_000,
    "youtube.via": "volunteers_when_refused",   # here | volunteers_when_refused | volunteers (never here: a cloud server)
    # the most the data may take, in GB (0: no limit): over it, volunteers and areas wait for room (DATA_CAP_GB starts it)
    "storage.cap_gb": 0.0,
    # notify (notify.py) when less than this % of the cap is left, as about 5 climbs' or boulders' worth at 60 GB, or this
    # many GB if set (0: the %)
    "storage.warn_percent": 0.5,
    "storage.warn_gb": 0.0,
    # above this % of the cap, what the backup holds and isn't in use is offloaded to it (offload.py), down to it (0: never)
    "storage.offload_percent": 70.0,
    # an area's climbs that share a boulder with others it lists: harvested, and meshed together as their boulder
    # ("boulders"), or each meshed on its own ("climbs")
    "areas.meshes": "boulders",
    # how the off-topic photo filter matches a search photo against the climb's own (relevance.py): brute force (the
    # original) or FLANN's approximate nearest neighbours (faster)
    "relevance.matcher": "brute",
    # filter a harvest's photos on a machine that connects here when one with a new enough script is in touch (they take
    # it before meshing), rather than on this server; here if none takes it within 3 minutes, or it fails
    "relevance.machines": True,
    # find beta in each new mesh with video frames placed in it, while its videos are still in the cache; and delete
    # cached videos none of whose frames is placed in any mesh, once the climbs that used them are settled
    "beta.auto": True,
    # at most this many meshes' beta found at once, on as many machines that connect here and take beta (one, here)
    "beta.parallel": 3,
    # work out which way each mesh faces from the skyline in its photos (skyline.py), where distant mountains show;
    # elsewhere it stops at its place's horizon, in about a second
    "facing.skyline": True,
    # a boulder's mesh is one boulder (pooled_summary()) only if each of its climbs with photos of its own has at least
    # this % of them placed in it, and at least one (0: one is enough)
    "boulders.placed_percent": 0.0,
    # after each boulder's mesh, mesh apart each scene of what it left out that's tied to it (keep_rest()); off, a
    # boulder's mesh only records them (rest.json). Never what a mesh from before ties left out, which isn't checked
    "boulders.rest_auto": True,
    "videos.prune_unplaced": True,
    # clients that register themselves (registration.py): provisional until approved here or from a phone. Volunteers
    # fetching videos: their limits (the script's own are 15 an hour, 200 a day; searches have their own), and while provisional, whether they
    # get work and how much. Machines meshing: whether provisional ones get work (then what they make waits for approval).
    "volunteers.per_hour": 15,
    "volunteers.per_day": 200,
    "volunteers.provisional_work": True,
    "volunteers.provisional_per_hour": 3,
    "volunteers.provisional_per_day": 10,
    "machines.provisional_work": False,
    "registration.per_address_day": 5,     # registrations a day from one address, of each kind
    "registration.max_waiting": 30,        # provisional ones waiting at once, of each kind; more must wait
    # notifications on a phone through ntfy (notify.py): the server, and the topics, which are made on the admin page
    "notify.enabled": False,
    "notify.server": "https://ntfy.sh",
    "notify.topic": "",
    "notify.reply_topic": "",
    "notify.restarts": True,        # when the server is back up after a restart
    "notify.downloads": True,       # when someone downloads the worker or volunteer script, roughly from where
    "notify.feedback": True,        # when a visitor leaves a comment or bug report on the "?" tab (feedback.py)
    # whose names the public leaderboard shows (leaderboard()): those who registered since the scripts said it would
    # ("new"), everyone's ("all"), or no one's ("none"); the rest as anonymous
    "leaderboard.names": "new",
}
CHOICES = {"compute.provider": ("none", "ssh", "aws", "pull"), "site.textures": ("all", "permitted", "none"),
           "site.sources": ("all", "link", "permitted"), "youtube.via": ("here", "volunteers_when_refused", "volunteers"),
           "areas.meshes": ("boulders", "climbs"), "relevance.matcher": ("brute", "flann"), "leaderboard.names": ("new", "all", "none")}
LIMITS = {"site.map_km": (0.05, 2000), "boulders.placed_percent": (0, 100), "beta.parallel": (1, 16), "viewer.preview_faces": (0, 5_000_000), "storage.cap_gb": (0, 100000), "storage.warn_percent": (0, 50), "storage.warn_gb": (0, 100000), "storage.offload_percent": (0, 100), "compute.local_workers": (0, 4), "compute.processing_cores": (1, os.cpu_count() or 1), "compute.remote_workers": (0, 16), "compute.idle_minutes": (1, 240),
          "compute.aws.volume_gb": (30, 2000), "volunteers.per_hour": (1, 15), "volunteers.per_day": (1, 200),
          "volunteers.provisional_per_hour": (1, 15), "volunteers.provisional_per_day": (1, 200),
          "registration.per_address_day": (1, 100), "registration.max_waiting": (1, 1000)}
for _key in ("site.textures", "site.sources", "youtube.via"):    # a public copy's starting points, from its .env
    _given = os.environ.get(_key.replace(".", "_").upper(), "").strip()
    if _given in CHOICES[_key]:
        DEFAULTS[_key] = _given
    elif _given:
        print(f"{_key.replace('.', '_').upper()}={_given} ignored: one of {', '.join(CHOICES[_key])}", flush=True)
try:
    DEFAULTS["storage.cap_gb"] = max(0.0, float(os.environ.get("DATA_CAP_GB") or 0))
except ValueError:
    print(f"DATA_CAP_GB={os.environ['DATA_CAP_GB']} ignored: a number of GB", flush=True)
DONATE_URL = re.compile(r"https://[^\s\"'<>\\]{4,300}")      # a link a page can show: https only, nothing that ends an attribute
if DONATE_URL.fullmatch(os.environ.get("DONATE_URL", "").strip()):
    DEFAULTS["site.donate_url"] = os.environ["DONATE_URL"].strip()
elif os.environ.get("DONATE_URL", "").strip():
    print("DONATE_URL ignored: an https:// address", flush=True)
SECRETS = ("aws.access_key_id", "aws.secret_access_key", "ssh.private_key")
SECRETS_FILE = "secrets.json"
_lock = threading.Lock()


def env_name(secret: str) -> str:
    """The environment variable a credential can be given by instead: aws.access_key_id is AWS_ACCESS_KEY_ID."""
    return secret.replace(".", "_").upper()


class Settings:
    """Settings in a database, with credentials alongside it."""

    def __init__(self, db_path: Path, data_dir: Path):
        self.db_path, self.data_dir = Path(db_path), Path(data_dir)
        with self._db() as con:
            con.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")

    def _db(self):
        return dbconn.connect(self.db_path, timeout=30)

    def get(self, key: str):
        if key not in DEFAULTS:
            raise KeyError(key)
        with self._db() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else DEFAULTS[key]

    def all(self) -> dict:
        with self._db() as con:
            stored = {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM settings")}
        return {k: stored.get(k, v) for k, v in DEFAULTS.items()}

    def update(self, changes: dict) -> dict:
        """Check and save some settings.

        Raises:
            ValueError: for an unknown setting or a value it can't take; nothing is saved then.
        """
        clean = {}
        for k, v in changes.items():
            if k not in DEFAULTS:
                raise ValueError(f"no setting {k}")
            want = type(DEFAULTS[k])
            if want is bool:
                if not isinstance(v, bool):
                    raise ValueError(f"{k} is true or false")
            elif want is float:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError(f"{k} is a number")
                v = float(v)
                lo, hi = LIMITS.get(k, (0, 10 ** 9))
                if not lo <= v <= hi:
                    raise ValueError(f"{k} is from {lo} to {hi}")
            elif want is int:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
                    raise ValueError(f"{k} is a whole number")
                v = int(v)
                lo, hi = LIMITS.get(k, (0, 10 ** 9))
                if not lo <= v <= hi:
                    raise ValueError(f"{k} is from {lo} to {hi}")
            elif want is list:
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise ValueError(f"{k} is a list of text")
                v = [x.strip() for x in v if x.strip()]
            elif not isinstance(v, str):
                raise ValueError(f"{k} is text")
            else:
                v = v.strip()
            if k in CHOICES and v not in CHOICES[k]:
                raise ValueError(f"{k} is one of {', '.join(CHOICES[k])}")
            if k == "notify.server" and not (v.startswith("https://") and len(v) > 8 and " " not in v):
                raise ValueError("notify.server is an https:// address, such as https://ntfy.sh")
            if k in ("notify.topic", "notify.reply_topic") and v and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v):
                raise ValueError(f"{k} is up to 64 letters, digits, - and _")
            if k == "site.donate_url" and v and not DONATE_URL.fullmatch(v):
                raise ValueError("site.donate_url is an https:// address, or empty for no Donate button")
            clean[k] = v
        with self._db() as con:
            con.executemany("INSERT OR REPLACE INTO settings VALUES(?,?)", [(k, json.dumps(v)) for k, v in clean.items()])
        return self.all()

    # -------------------------------------------------------------- credentials
    def _fernet(self):
        key = os.environ.get("BOULDERMESH_SECRETS_KEY")
        if not key:
            return None
        from cryptography.fernet import Fernet
        return Fernet(key.encode())

    def can_store_secrets(self) -> bool:
        try:
            return self._fernet() is not None
        except Exception:
            return False

    def _read_secrets(self) -> dict:
        path = self.data_dir / SECRETS_FILE
        if not path.exists():
            return {}
        f = self._fernet()
        if f is None:
            raise RuntimeError("credentials are saved, but BOULDERMESH_SECRETS_KEY isn't set to decrypt them")
        try:
            return json.loads(f.decrypt(path.read_bytes()).decode())
        except Exception:
            raise RuntimeError("the saved credentials can't be decrypted: is BOULDERMESH_SECRETS_KEY the key they were saved with?") from None

    def secret(self, name: str) -> str | None:
        """A credential: the saved one, or else its environment variable (env_name)."""
        if name not in SECRETS:
            raise KeyError(name)
        try:
            saved = self._read_secrets().get(name)
        except RuntimeError:
            saved = None
        return saved or os.environ.get(env_name(name))

    def set_secrets(self, values: dict) -> dict:
        """Save credentials, encrypted; an empty value removes one.

        Raises:
            RuntimeError: without BOULDERMESH_SECRETS_KEY to encrypt them with.
            ValueError: for an unknown credential.
        """
        f = self._fernet()
        if f is None:
            raise RuntimeError("set BOULDERMESH_SECRETS_KEY (outside the data directory) to save credentials here, "
                               "or give them as environment variables")
        for k in values:
            if k not in SECRETS:
                raise ValueError(f"no credential {k}")
        with _lock:
            current = self._read_secrets()
            for k, v in values.items():
                if v:
                    current[k] = str(v)
                else:
                    current.pop(k, None)
            path = self.data_dir / SECRETS_FILE
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(f.encrypt(json.dumps(current).encode()))
            os.replace(tmp, path)
        return self.secrets_status()

    def secrets_status(self) -> dict:
        """Which credentials are set, and where from, never their values."""
        try:
            saved = self._read_secrets()
            error = None
        except Exception as e:
            saved, error = {}, str(e)
        out = {}
        for name in SECRETS:
            out[name] = "saved" if saved.get(name) else ("environment" if os.environ.get(env_name(name)) else None)
        return {"credentials": out, "can_save": self.can_store_secrets(), "error": error}
