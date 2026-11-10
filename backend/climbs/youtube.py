"""Finding a climb's videos on YouTube, and what their uploaders say about the climber's size.

Uses yt-dlp, which the harvest already downloads videos with: its search and metadata are the same
kind of access. A search result counts only if its title names the climb and something placing it
(its area, boulder or grade), as climb names like "Relic" aren't unique. design.md has how well that
picked out the right videos.

Climbers often give their height or ape index in a video's description or on their channel, which
could later give a mesh a scale. stats() reads them from free text; nothing is inferred.

Every yt-dlp run goes through call(): one at a time across the app, GAP seconds apart. When YouTube
answers with its bot check ("Sign in to confirm you're not a bot") or a rate limit, nothing that fetches
a video or its details is asked of it for an hour, then 2, 4 and 8 while it still answers that way.
Searches and channel pages have a pause of their own, the same, kept apart: they went through while
downloads were refused, and never got the check on hal. design.md has what led to this.

Videos fetched are kept in a cache by their id, with their details, so that finding beta and harvesting again don't
ask YouTube a second time: those this server downloads, and those volunteers fetch from their own addresses
(volunteers.py) when YouTube refuses this server, or always if `via` says so. Searches go the same way, and what each
found is kept too, by its words, for SEARCH_DAYS: a harvest that asked volunteers to search finds their results there
when it's harvested again.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable

from .common import CACHE_DIR, Climb, Media, jobctl

Log = Callable[[str], None]
MAX_RESULTS = 15           # search results to look through per climb
MAX_MATCHES = 4            # of which at most this many are kept
MAX_MINUTES = 15           # longer videos are mostly compilations and vlogs, with little of any one rock
REGION_SUFFIX = re.compile(r"\b(national|state|regional|county)\s+(park|forest|monument|recreation area)\b|\bbouldering\b|\barea\b", re.I)
GENERIC = {"boulder", "boulders", "the boulder", "area", "bouldering"}
GAP = (8.0, 15.0)          # seconds between one yt-dlp run and the next, at random in this range (yt-dlp's wiki: 5-10)
SLEEP_REQUESTS = ["--sleep-requests", "0.75"]   # between the requests within one run, as yt-dlp's `-t sleep` does
BLOCK_HOURS = (1, 2, 4, 8)  # how long to leave YouTube alone after its bot check, each time it's still there
REFUSAL = re.compile(r"not a bot|HTTP Error 429|Too Many Requests|content isn.t available, try again later", re.I)
STATE_FILE = CACHE_DIR.parent / "youtube.json"
PAGES_STATE_FILE = CACHE_DIR.parent / "youtube-pages.json"   # searches and channel pages, refused apart from videos
VIDEO_CACHE = CACHE_DIR.parent / "videos"     # <id>.<ext> and <id>.json, its details
SEARCHES = CACHE_DIR.parent / "youtube-searches"   # what each search found, here or by a volunteer, by its words
SEARCH_DAYS = 30            # how long a search's results are used before searching again
CACHE_BYTES = int(float(os.environ.get("VIDEO_CACHE_GB", "50")) * 2**30)   # the oldest used go beyond this
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
# where videos, searches and channels' pages come from, set by the server from its settings: "here";
# "volunteers_when_refused", here unless YouTube is refusing this server, when volunteers are asked; or "volunteers",
# never here (a cloud server, which YouTube refuses)
via = "volunteers_when_refused"
_gate = threading.Lock()
_last = 0.0


class Refused(RuntimeError):
    """YouTube is refusing this machine for now (its bot check, or a rate limit)."""

    def __init__(self, state: dict):
        self.state = state
        super().__init__("this server leaves YouTube to volunteers" if state.get("volunteers") else
                         f"YouTube is refusing this machine's {state.get('what', 'downloads')} (its bot check) until "
                         f"{time.strftime('%H:%M UTC', time.gmtime(state['until']))}")


def available() -> bool:
    return shutil.which("yt-dlp") is not None


def refused(video: bool = True) -> dict | None:
    """While YouTube's videos, or with video=False its searches and channel pages, are being left alone:
    {since, until, level, error, what}, else None."""
    try:
        state = json.loads((STATE_FILE if video else PAGES_STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return state if time.time() < state.get("until", 0) else None


def call(cmd: list[str], video: bool = True, **kw) -> str:
    """Run a yt-dlp command (jobctl.run's arguments) through the gate.

    Args:
        video: it fetches a video or a video's details, rather than a search or a channel page. Each kind is
            refused while YouTube's bot check is on for it, which its answer turns on.

    Raises:
        Refused: YouTube is refusing, or just did.
    """
    global _last
    if via == "volunteers":
        raise Refused({"since": time.time(), "until": time.time() + 3600, "level": 0, "volunteers": True,
                       "error": "this server doesn't ask YouTube for anything itself"})
    path, what = (STATE_FILE, "downloads") if video else (PAGES_STATE_FILE, "searches")
    while not _gate.acquire(timeout=1):
        jobctl.check()
    try:
        try:
            state = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            state = None
        if state and time.time() < state["until"]:
            raise Refused(state)
        until = _last + random.uniform(*GAP)
        while time.time() < until:
            jobctl.check()
            time.sleep(min(1.0, until - time.time()))
        try:
            out = jobctl.run(cmd[:1] + SLEEP_REQUESTS + cmd[1:], **kw)
        except Exception as e:
            if REFUSAL.search(str(e)):
                level = min(len(BLOCK_HOURS) - 1, state["level"] + 1) if state else 0
                state = {"since": state["since"] if state else time.time(), "level": level, "what": what,
                         "until": time.time() + BLOCK_HOURS[level] * 3600, "error": str(e)[:300]}
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(state))
                raise Refused(state) from None
            raise
        finally:
            _last = time.time()
        if state:     # it let one through: it's stopped refusing
            path.unlink(missing_ok=True)
        return out
    finally:
        _gate.release()


def cached(url: str) -> Path | None:
    """The video at url (or with that YouTube id), if it's in the cache; marked as just used."""
    vid = url if VIDEO_ID.fullmatch(url or "") else video_id(url or "")
    if not vid or not VIDEO_CACHE.is_dir():
        return None
    for f in VIDEO_CACHE.glob(f"{vid}.*"):
        if f.suffix != ".json" and not f.name.endswith(".part"):
            try:
                os.utime(f)
            except OSError:
                pass
            return f
    return None


def cached_details(url: str) -> dict | None:
    vid = url if VIDEO_ID.fullmatch(url or "") else video_id(url or "")
    try:
        return json.loads((VIDEO_CACHE / f"{vid}.json").read_text()) if vid else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def cache_put(url: str, video: Path, info: dict | None = None) -> Path:
    """Keep a fetched video (moved) and its details by its YouTube id, then drop the least recently used beyond
    CACHE_BYTES. A video that isn't YouTube's stays where it is. Returns where it is now."""
    vid = url if VIDEO_ID.fullmatch(url or "") else video_id(url or "")
    if not vid:
        return video
    VIDEO_CACHE.mkdir(parents=True, exist_ok=True)
    for old in VIDEO_CACHE.glob(f"{vid}.*"):
        old.unlink(missing_ok=True)
    dest = VIDEO_CACHE / f"{vid}{video.suffix.lower()}"
    shutil.move(str(video), dest)
    if info:
        (VIDEO_CACHE / f"{vid}.json").write_text(json.dumps(info))
    trim_cache(keep=dest)
    return dest


def trim_cache(keep: Path | None = None) -> None:
    files = [f for f in VIDEO_CACHE.glob("*") if f.suffix != ".json" and f != keep] if VIDEO_CACHE.is_dir() else []
    total = sum(f.stat().st_size for f in files) + (keep.stat().st_size if keep else 0)
    for f in sorted(files, key=lambda f: f.stat().st_mtime):
        if total <= CACHE_BYTES:
            break
        total -= f.stat().st_size
        f.unlink(missing_ok=True)
        (VIDEO_CACHE / f"{f.stem}.json").unlink(missing_ok=True)


def norm(text: str | None) -> str:
    """Lower case, no accents or punctuation, single spaces, padded so words can be found with ' w '."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", text).split()) + " "


def region(c: Climb) -> str:
    """The best-known place a climb is in, shortened as people write it: "Joshua Tree"."""
    place = next((p for p in c.area_path if re.search(r"\b(park|forest|valley|canyon|monument)\b", p, re.I)), None)
    place = place or (c.area_path[1] if len(c.area_path) > 1 else (c.area_path[0] if c.area_path else ""))
    return " ".join(REGION_SUFFIX.sub(" ", place).split())


def v_grade(c: Climb) -> str | None:
    m = re.search(r"\bV\d+\b", c.grade or "", re.I)
    return m[0].upper() if m else None


def query(c: Climb) -> str:
    return " ".join(x for x in (c.name, v_grade(c), region(c)) if x)


def context(c: Climb) -> list[str]:
    """Words that place a video at this climb, normalised: any one of them is enough."""
    out = [region(c), c.boulder_name or "", *(c.area_path[-3:] if c.area_path else [])]
    grade = v_grade(c)
    out = [norm(x) for x in out if x and x.strip().lower() not in GENERIC and len(x.strip()) >= 4 and x.strip().lower() != c.name.lower()]
    if grade:
        out.append(norm(grade))
    return [x for x in dict.fromkeys(out) if x.strip()]


def matches(c: Climb, title: str | None, description: str | None = None) -> bool:
    """Whether a video is of this climb: its title names it, and the title or description places it."""
    name = norm(c.name)
    if len(name.strip()) < 3 or name not in norm(title):
        return False
    text = norm(f"{title} {description or ''}")
    return any(x in text for x in context(c))


def _ytdlp(args: list[str], timeout: int = 180, video: bool = True) -> dict:
    out = call(["yt-dlp", "-J", "--no-warnings", *args], video=video, timeout=timeout,
               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return json.loads(out or "{}")


def _search_file(q: str) -> Path:
    return SEARCHES / (hashlib.sha1(q.encode()).hexdigest()[:16] + ".json")


def cached_search(q: str, days: float = SEARCH_DAYS) -> dict | None:
    """What a search for these words found, if it was searched in the last `days`: {query, results, fetched, by}."""
    try:
        rec = json.loads(_search_file(q).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return rec if rec.get("query") == q and time.time() - rec.get("fetched", 0) < days * 86400 else None


def result(e: dict) -> dict | None:
    """What's kept of a search result (yt-dlp's flat entry, or a volunteer's), or None if it isn't a video."""
    vid = str(e.get("id") or "")
    if not VIDEO_ID.fullmatch(vid):
        vid = video_id(str(e.get("url") or "")) or ""
    if not VIDEO_ID.fullmatch(vid):
        return None
    try:
        duration = float(e["duration"]) if e.get("duration") is not None else None
    except (TypeError, ValueError):
        duration = None
    text = lambda k, n: str(e[k])[:n] if e.get(k) is not None else None  # noqa: E731
    return {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": text("title", 500),
            "description": text("description", 2000), "duration": duration, "channel": text("channel", 200),
            "channel_id": text("channel_id", 40), "uploader": text("uploader", 200)}


def save_search(q: str, results: list[dict], by: str) -> dict:
    """Keep what a search found ("here", or by a "volunteer"), for SEARCH_DAYS."""
    rec = {"query": q, "results": [r for r in map(result, results) if r][:50], "fetched": time.time(), "by": by}
    SEARCHES.mkdir(parents=True, exist_ok=True)
    tmp = _search_file(q).with_suffix(".tmp")
    tmp.write_text(json.dumps(rec))
    tmp.replace(_search_file(q))
    return rec


def search(c: Climb, log: Log, known: set[str] = frozenset(), want: Callable[[str], bool] | None = None,
           asked: list | None = None, days: float = SEARCH_DAYS) -> list[Media]:
    """Videos of a climb found by searching YouTube, best first: by what a search for its words found in the last
    `days`, here or by a volunteer, or else by searching now, unless YouTube is left to volunteers or refusing
    this server, when volunteers are asked to search instead (the harvest carries on without).

    Args:
        known: URLs it already has, which aren't returned again.
        want: called with the search's words to ask volunteers for it; whether it's coming.
        asked: the words volunteers were asked to search for are added to it.
    """
    q = query(c)
    hit = cached_search(q, days)
    if hit:
        log(f"youtube search: {q!r}: as {'a volunteer' if hit.get('by') == 'volunteer' else 'this server'} searched "
            f"{(time.time() - hit['fetched']) / 3600:.0f} h ago")
    else:
        why = None
        if via == "volunteers":
            why = "this server leaves YouTube to volunteers"
        elif not available():
            log("youtube search: skipped (needs yt-dlp)")
            return []
        else:
            try:
                hit = save_search(q, _ytdlp(["--flat-playlist", f"ytsearch{MAX_RESULTS}:{q}"], video=False).get("entries") or [], "here")
            except Refused as e:
                why = str(e)
            except Exception as e:
                log(f"youtube search: {q!r} failed: {e}")
                return []
        if not hit:
            if want and via != "here" and want(q):
                if asked is not None:
                    asked.append(q)
                log(f"youtube search: {why}: asked volunteers to search for {q!r}; this climb is harvested again when they have")
            else:
                log(f"youtube search: {q!r} skipped: {why}")
            return []
    found = hit["results"]
    out = []
    for e in found:
        url = e.get("url") or (f"https://www.youtube.com/watch?v={e['id']}" if e.get("id") else None)
        minutes = (e.get("duration") or 0) / 60
        if not url or url in known or not e.get("duration") or minutes > MAX_MINUTES:
            continue
        if matches(c, e.get("title"), e.get("description")):
            out.append(Media(url=url, kind="video", page=url, title=e.get("title"), credit=e.get("channel") or e.get("uploader"),
                             source="youtube-search"))
    log(f"youtube search: {q!r} -> {len(found)} result(s), {len(out)} of this climb"
        + (f", keeping {MAX_MATCHES}" if len(out) > MAX_MATCHES else ""))
    return out[:MAX_MATCHES]


def video_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", url)
    return m[1] if m else None


def details(info: dict) -> dict:
    """What's kept of yt-dlp's metadata for a video."""
    lic = info.get("license")
    youtube = "youtube" in (info.get("extractor") or info.get("webpage_url") or "").lower()
    return {"url": info.get("webpage_url") or info.get("original_url"), "title": info.get("title"),
            "uploader": info.get("uploader") or info.get("channel"), "channel_id": info.get("channel_id"),
            "channel_url": info.get("channel_url") or info.get("uploader_url"), "duration": info.get("duration"),
            "upload_date": info.get("upload_date"), "description": info.get("description"),
            "license": lic or ("YouTube standard licence (all rights reserved)" if youtube else "no Creative Commons licence marked")}


def metadata(url: str) -> dict:
    """A video's details, without downloading it."""
    return details(_ytdlp(["--skip-download", "--no-playlist", url]))


def channel(url: str) -> dict:
    """A channel's name and description (its "about" text)."""
    info = _ytdlp(["--flat-playlist", "--playlist-items", "0", url], timeout=120, video=False)
    return {"channel_url": url, "channel_id": info.get("channel_id") or info.get("id"),
            "name": info.get("channel") or info.get("uploader") or info.get("title"), "description": info.get("description") or ""}


# ------------------------------------------------------------------ climber height and ape index
FEET = r"(?<![\d.])([4-7])\s*(?:'|’|′|ft\.?|feet|foot)\s*(?:and\s*)?(?:(\d{1,2}(?:\.\d)?)\s*(?:\"|”|″|''|in\b\.?|inch(?:es)?))?"
FEET_INCHES = r"(?<![\d.])([4-7])\s*(?:'|’|′|ft\.?|feet|foot)\s*(?:and\s*)?(\d{1,2}(?:\.\d)?)\s*(?:\"|”|″|''|in\b\.?|inch(?:es)?)?"
METRES = r"(?<![\d.])(1[.,][4-9]\d?|2[.,][01]\d?)\s*m\b"
CENTIMETRES = r"(?<![\d.])(1[4-9]\d|2[01]\d)\s*cm\b"
FIRST_PERSON = re.compile(r"\b(i'?m|i am|im|me|my height|myself|for reference)\b", re.I)
SIZE_WORDS = re.compile(r"\b(tall|height|climber|stand|short)\b", re.I)
SPAN_WORDS = re.compile(r"\b(wing\s*span|arm\s*span|reach)\b", re.I)
NOT_PEOPLE = re.compile(r"\b(boulder|highball|wall|drop|fall|landing|problem|roof|lip|topout|top out|rock)\b", re.I)


def _cm(feet, inches=None, metres=None, cm=None) -> float | None:
    if cm:
        return float(cm)
    if metres:
        return round(float(metres.replace(",", ".")) * 100, 1)
    inches = float(inches) if inches else 0.0
    if inches >= 12:
        return None
    return round((int(feet) * 12 + inches) * 2.54, 1)


def _around(text: str, m: re.Match, before: int = 40, after: int = 25) -> tuple[str, str]:
    return text[max(0, m.start() - before):m.start()], text[m.end():m.end() + after]


def stats(text: str | None) -> dict:
    """A climber's height, wingspan and ape index, as stated in free text, in cm.

    A height needs a word saying it's a person's (tall, height, I'm, climber...), unless it's given in
    feet and inches; heights near words about the rock (a "6 ft tall boulder") don't count.

    Returns:
        {height_cm, wingspan_cm, ape_index_cm, first_person, quotes}: values None where not stated,
        first_person if the text says it's the writer's own, and the quoted text each came from.
    """
    text = text or ""
    out = {"height_cm": None, "wingspan_cm": None, "ape_index_cm": None, "first_person": False, "quotes": []}

    def quote(m):
        out["quotes"].append(text[max(0, m.start() - 40):m.end() + 20].replace("\n", " ").strip())

    for pattern, kind in ((FEET_INCHES, "ftin"), (FEET, "ft"), (METRES, "m"), (CENTIMETRES, "cm")):
        for m in re.finditer(pattern, text, re.I):
            before, after = _around(text, m)
            if kind == "ft" and m[2] is None and not FIRST_PERSON.search(before + after):
                continue    # "6 ft" alone is as likely the rock
            if NOT_PEOPLE.search(after[:20]) or (kind != "ftin" and NOT_PEOPLE.search(before[-20:])):
                continue
            span = bool(SPAN_WORDS.search(re.split(r"[,;\n]|\.\s", before)[-1])   # the label in its own clause,
                        or re.match(r"\s*(wing\s*span|arm\s*span|reach)\b", after, re.I))   # or straight after it
            if kind in ("m", "cm", "ft") and not (span or SIZE_WORDS.search(before + after) or FIRST_PERSON.search(before + after)):
                continue
            value = _cm(m[1], m[2] if kind in ("ftin", "ft") else None, metres=m[1] if kind == "m" else None,
                        cm=m[1] if kind == "cm" else None)
            if value is None:
                continue
            if kind == "ftin" and re.match(r"\s*(and\s+a\s+half|½)", text[m.end():], re.I):
                value = round(value + 1.27, 1)
            key = "wingspan_cm" if span else "height_cm"
            if out[key] is None:
                out[key] = value
                out["first_person"] |= bool(FIRST_PERSON.search(before + after))
                quote(m)
    ape = re.search(r"ape(?:\s*index)?\s*(?:of|is|:|=)?\s*(\+|-|−|–|plus|minus|positive|negative)?\s*(\d+(?:\.\d+)?)\s*(cm|in\b|inch(?:es)?|\"|”)?", text, re.I) \
        or re.search(r"([+-]|plus|minus)\s*(\d+(?:\.\d+)?)\s*(cm|in\b|inch(?:es)?|\"|”)?\s*ape", text, re.I)
    if ape:
        sign = -1 if (ape[1] or "").lower() in ("-", "−", "–", "minus", "negative") else 1
        value = float(ape[2]) * (1 if (ape[3] or "").lower() == "cm" else 2.54)
        if value <= 30:
            out["ape_index_cm"] = round(sign * value, 1)
            quote(ape)
    if out["ape_index_cm"] is None and out["height_cm"] and out["wingspan_cm"]:
        out["ape_index_cm"] = round(out["wingspan_cm"] - out["height_cm"], 1)
    return out
