"""Shared types and a polite HTTP client (stdlib only).

The client:
  * caches every response on disk (DATA_DIR/cache/http, default TTL 30 days)
  * rate-limits per host (default one request per 3 s, or robots.txt Crawl-delay if longer)
  * honours robots.txt Disallow rules for the configured user agent, except on documented
    APIs (obey_robots=False), where those rules only address crawlers
  * never retries aggressively (one retry on 5xx / connection reset)
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

USER_AGENT = os.environ.get(
    "BOULDERMESH_UA",
    "BoulderMesh/0.2 (personal photogrammetry prototype; contact: set BOULDERMESH_UA env var)")
CACHE_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parents[2] / "data")) / "cache" / "http"
CACHE_TTL = int(os.environ.get("HTTP_CACHE_TTL", str(30 * 86400)))
MIN_INTERVAL = float(os.environ.get("HTTP_MIN_INTERVAL", "3.0"))  # seconds between requests to one host
RESPECT_ROBOTS = os.environ.get("HTTP_IGNORE_ROBOTS", "0") != "1"


# ------------------------------------------------------------------ types
@dataclass
class Media:
    url: str                      # direct file URL (image or video page)
    kind: str = "image"           # image | video
    page: str | None = None       # human page the media came from
    title: str | None = None
    credit: str | None = None     # photographer / uploader
    license: str | None = None    # e.g. "CC BY-SA 4.0", "all rights reserved (Mountain Project user)"
    width: int | None = None
    height: int | None = None
    source: str | None = None     # adapter name

    def to_dict(self):
        return dataclasses.asdict(self)


@dataclass
class Climb:
    source: str                   # mountainproject | openbeta | kaya | manual
    source_id: str                # id/uuid on that source
    url: str
    name: str
    kind: str = "boulder"         # boulder | route | area
    grade: str | None = None      # display grade, e.g. "V8 7B" or "5.10a"
    grades: dict = field(default_factory=dict)   # {vscale:, font:, yds:, ...}
    fa: str | None = None
    rating: float | None = None   # avg stars
    votes: int | None = None
    lat: float | None = None
    lng: float | None = None
    coord_source: str | None = None   # route | area | parent-area | manual | none
    elevation_m: float | None = None
    area_path: list[str] = field(default_factory=list)   # ["California", ..., "Big Columbia Boulder"]
    area_urls: list[str] = field(default_factory=list)
    boulder_name: str | None = None   # best guess at the rock this problem is on
    description: str | None = None
    location: str | None = None
    protection: str | None = None
    images: list[Media] = field(default_factory=list)
    videos: list[Media] = field(default_factory=list)
    cross_refs: dict = field(default_factory=dict)   # {"mp_id": "...", "openbeta_uuid": "..."}
    fetched_at: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)          # adapter-specific leftovers

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict) -> "Climb":
        d = dict(d)
        d["images"] = [Media(**m) for m in d.get("images", [])]
        d["videos"] = [Media(**m) for m in d.get("videos", [])]
        known = {f.name for f in dataclasses.fields(Climb)}
        return Climb(**{k: v for k, v in d.items() if k in known})

    def guess_boulder_name(self) -> str | None:
        """Boulder problems live in an 'area' that is usually the boulder itself."""
        if self.boulder_name:
            return self.boulder_name
        if self.kind != "boulder":
            return None
        for name in reversed(self.area_path):
            if re.search(r"\b(boulder|block|bloc|rock|stone|egg|slab|cube)\b", name, re.I):
                return name
        return self.area_path[-1] if self.area_path else None


# ------------------------------------------------------------------ HTTP
class HttpError(RuntimeError):
    def __init__(self, url, status, msg=""):
        super().__init__(f"HTTP {status} for {url} {msg}".strip())
        self.status = status
        self.url = url


class ApiError(RuntimeError):
    """An API reported a failure in the body of a 200 response."""


class _Http:
    def __init__(self):
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # -- robots -----------------------------------------------------------
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        if not RESPECT_ROBOTS:
            return None
        u = urllib.parse.urlparse(url)
        host = u.netloc
        rp = self._robots.get(host, "unset")
        if rp == "unset":
            rp = urllib.robotparser.RobotFileParser()
            try:
                req = urllib.request.Request(f"{u.scheme}://{host}/robots.txt", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=15) as r:
                    rp.parse(r.read().decode("utf-8", "replace").splitlines())
            except Exception:
                rp = None  # unreachable robots.txt -> assume allowed
            self._robots[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    # -- throttle ---------------------------------------------------------
    def interval(self, url: str) -> float:
        """Seconds between requests to url's host: HTTP_MIN_INTERVAL, or robots.txt
        Crawl-delay when that is longer (Mountain Project asks for 60)."""
        rp = self._robots_for(url)
        delay = rp.crawl_delay(USER_AGENT) if rp is not None else None
        return max(MIN_INTERVAL, float(delay or 0))

    def _throttle(self, url: str):
        host = urllib.parse.urlparse(url).netloc
        gap = self.interval(url)
        # reserve the next slot for this host, but sleep outside the lock so other hosts aren't held up
        with self._lock:
            slot = max(time.time(), self._last.get(host, 0) + gap)
            self._last[host] = slot
        time.sleep(max(0.0, slot - time.time()))

    # -- cache ------------------------------------------------------------
    @staticmethod
    def _cache_path(url: str, data: bytes | None = None) -> Path:
        key = url + ("|" + hashlib.sha1(data).hexdigest() if data else "")
        h = hashlib.sha1(key.encode()).hexdigest()
        return CACHE_DIR / h[:2] / h

    def get(self, url: str, headers: dict | None = None, data: bytes | None = None, ttl: int = CACHE_TTL,
            use_cache: bool = True, binary: bool = False, obey_robots: bool = True) -> bytes:
        """GET (or POST when data is given). Returns body bytes. Cached by (url, data)."""
        cp = self._cache_path(url, data)
        if use_cache and cp.exists() and time.time() - cp.stat().st_mtime < ttl:
            return cp.read_bytes()
        if obey_robots and not self.allowed(url):
            raise HttpError(url, 403, "(disallowed by robots.txt)")
        hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.8"}
        hdrs.update(headers or {})
        for attempt in (1, 2):
            self._throttle(url)
            req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data else "GET")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    body = r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code >= 500 and attempt == 1:
                    time.sleep(5)
                    continue
                raise HttpError(url, e.code, e.reason) from None
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                if attempt == 1:
                    time.sleep(5)
                    continue
                raise HttpError(url, 0, str(e)) from None
        if use_cache:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_bytes(body)
        return body

    def get_text(self, url: str, **kw) -> str:
        return self.get(url, **kw).decode("utf-8", "replace")

    def get_json(self, url: str, payload: Any = None, headers: dict | None = None, error=None, **kw) -> Any:
        """error(response) -> message or None, for APIs that report failures in a 200 response:
        raises ApiError and keeps the (often transient) failure out of the cache."""
        hdrs = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        res = json.loads(self.get(url, headers=hdrs, data=data, **kw))
        msg = error(res) if error else None
        if msg:
            self._cache_path(url, data).unlink(missing_ok=True)
            raise ApiError(msg)
        return res


http = _Http()


# ------------------------------------------------------------------ text utils
_WS = re.compile(r"\s+")


def clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = _WS.sub(" ", s).strip()
    return s or None


def parse_latlng(text: str | None) -> tuple[float, float] | None:
    if not text:
        return None
    m = re.search(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", text)
    if not m:
        return None
    lat, lng = float(m[1]), float(m[2])
    if abs(lat) > 90 or abs(lng) > 180:
        return None
    return lat, lng


YT_RE = re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:youtube(?:-nocookie)?\.com/(?:watch\?[^\"'\s<]*v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")
VIMEO_RE = re.compile(r"https?://(?:www\.)?vimeo\.com/(\d+)")
INSTA_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:reel|p)/([A-Za-z0-9_-]+)")


def find_videos(text: str, source: str, page: str | None = None) -> list[Media]:
    seen, out = set(), []
    for m in YT_RE.finditer(text):
        vid = m[1]
        if vid not in seen:
            seen.add(vid)
            out.append(Media(url=f"https://www.youtube.com/watch?v={vid}", kind="video", page=page, source=source))
    for m in VIMEO_RE.finditer(text):
        if m[0] not in seen:
            seen.add(m[0])
            out.append(Media(url=m[0], kind="video", page=page, source=source))
    for m in INSTA_RE.finditer(text):
        if m[0] not in seen:
            seen.add(m[0])
            out.append(Media(url=m[0], kind="video", page=page, source=source))
    return out
