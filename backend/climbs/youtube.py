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
SEARCH_METHOD = 2           # how climbs' searches are made: raised when query(), wide() or matches() change, so that
                            # those searched before can be searched again (the admin page's "search YouTube again")
REGION_SUFFIX = re.compile(r"\b(national|state|regional|county)\s+(park|forest|monument|recreation area)\b|\bbouldering\b|\barea\b", re.I)
GENERIC = {"boulder", "boulders", "the boulder", "area", "bouldering"}
# words many climbs' names share, which don't tell one from another (distinctive())
PLAIN_WORDS = set("""the a an of and on in to unnamed unknown problem arete crack traverse roof dyno mantle sit start stand low high
left right center centre direct variation var face slab corner prow lip boulder project classic easy hard
one two three four five six seven eight nine ten i ii iii iv v""".split())
GAP = (8.0, 15.0)          # seconds between one yt-dlp run and the next, at random in this range (yt-dlp's wiki: 5-10)
SLEEP_REQUESTS = ["--sleep-requests", "0.75"]   # between the requests within one run, as yt-dlp's `-t sleep` does
BLOCK_HOURS = (1, 2, 4, 8)  # how long to leave YouTube alone after its bot check, each time it's still there
REFUSAL = re.compile(r"not a bot|HTTP Error 429|Too Many Requests|content isn.t available, try again later", re.I)
STATE_FILE = CACHE_DIR.parent / "youtube.json"
PAGES_STATE_FILE = CACHE_DIR.parent / "youtube-pages.json"   # searches and channel pages, refused apart from videos
VIDEO_CACHE = CACHE_DIR.parent / "videos"     # <id>.<ext> and <id>.json, its details
SEARCHES = CACHE_DIR.parent / "youtube-searches"   # what each search found, here or by a volunteer, by its words
WIDE_LOG = CACHE_DIR.parent / "youtube-wide.jsonl"   # what wide() searches found that the search with the place didn't (log_wide())
SEARCH_DAYS = 30            # how long a search's results are used before searching again
CACHE_BYTES = int(float(os.environ.get("VIDEO_CACHE_GB", "50")) * 2**30)   # the oldest used go beyond this
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
# where videos, searches and channels' pages come from, set by the server from its settings: "here";
# "volunteers_when_refused", here unless YouTube is refusing this server, when volunteers are asked; or "volunteers",
# never here (a cloud server, which YouTube refuses)
via = "volunteers_when_refused"
wide_retry = True          # search again without its place when the search with it finds nothing of a climb (youtube.wide_retry)
# set by tests that run a whole server: nothing at all is asked of YouTube (searches come back empty, downloads are refused)
OFFLINE = os.environ.get("YOUTUBE_OFFLINE") == "1"
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
    if OFFLINE:
        raise Refused({"since": time.time(), "until": time.time() + 3600, "level": 0,
                       "error": "YOUTUBE_OFFLINE is set: nothing is asked of YouTube"})
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


# whether trim_cache() drops the least recently used videos beyond CACHE_BYTES: on a machine, whose cache is only what a
# job was sent. The server sets it False: it keeps every video, offloading those nothing needs to the backup instead
# (its offload_step()), as a video dropped is one a volunteer must fetch again
TRIM = True


def trim_cache(keep: Path | None = None) -> None:
    """Drop the least recently used videos beyond CACHE_BYTES, unless not TRIM."""
    if not TRIM:
        return
    files = [f for f in VIDEO_CACHE.glob("*") if f.suffix != ".json" and not f.name.endswith(".part") and f != keep] \
        if VIDEO_CACHE.is_dir() else []
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


def short(place: str) -> str:
    """A place's name as people write it: "Joshua Tree National Park" and "Bishop Area" are "Joshua Tree" and "Bishop"."""
    return " ".join(REGION_SUFFIX.sub(" ", place or "").split())


def region(c: Climb) -> str:
    """The best-known place a climb is in, shortened as people write it: "Joshua Tree". Past Mountain Project's
    "International" and continent, as "Europe" says nothing."""
    path = c.area_path[2:] if c.area_path[:1] == ["International"] else c.area_path
    place = next((p for p in path if re.search(r"\b(park|forest|valley|canyon|monument)\b", p, re.I)), None)
    place = place or (path[1] if len(path) > 1 else (path[0] if path else ""))
    return short(place)


def place(c: Climb) -> str:
    """The place its search names: the climbing area it was meshed as part of ("Horse Flats", not the forest
    "Angeles" it's in), as that's how people title their videos; else its region()."""
    return short(c.search_place or "") or region(c)


def v_grade(c: Climb) -> str | None:
    m = re.search(r"\bV\d+\b", c.grade or "", re.I)
    return m[0].upper() if m else None


def font_grades(c: Climb) -> list[str]:
    """The Fontainebleau grades a climbing-history.org climb's page gives it, lower case without their plus ("8c",
    "9a" for 8C+/9A), as norm() leaves them in a title; none for any other climb, whose searches go by V grades as they
    always have."""
    if c.source != "climbinghistory":
        return []
    return list(dict.fromkeys(m[0] for part in str(c.grades.get("font") or "").lower().split("/")
                              if (m := re.match(r"[3-9][abc]?", part.strip()))))


def said_grade(c: Climb) -> str | None:
    """The grade its search names: its V grade; or, for a climbing-history.org climb outside the US, its Fontainebleau
    grade ("9A"), as videos of it are titled."""
    font = str(c.grades.get("font") or "").split("/")[0].strip() if font_grades(c) else ""
    return font if font and c.raw.get("country") not in (None, "United States") else v_grade(c)


def query(c: Climb) -> str:
    return " ".join(x for x in (c.name, said_grade(c), place(c)) if x)


def wide(c: Climb) -> str:
    """Its search without the place, for when the one with it finds nothing of it (youtube.wide_retry)."""
    return " ".join(x for x in (c.name, said_grade(c)) if x)


def context(c: Climb) -> list[str]:
    """Words that place a video at this climb, normalised: any one of them is enough."""
    out = [place(c), region(c), c.boulder_name or "", *(c.area_path[-3:] if c.area_path else [])]
    if c.source == "climbinghistory":      # its country too: its path's last three stop at a region few titles name
        out.append(c.raw.get("country") or "")
    out = [norm(x) for x in out if x and x.strip().lower() not in GENERIC and len(x.strip()) >= 4 and x.strip().lower() != c.name.lower()]
    out += [" " + x.split(" ", 2)[2] for x in out if x.startswith(" the ") and len(x) > 10]    # "The Tramway" titled "Tramway"
    return [x for x in dict.fromkeys(out) if x.strip()]    # not its grade alone: "The Wave V1" is in many places


def distinctive(name: str) -> bool:
    """Whether a climb's name is unusual enough that it and its grade alone say a video is of it: two words that many
    names share (PLAIN_WORDS) don't count, or one of six letters or more ("Spleef", "Arbor Days"; not "The Fin" or
    "Scoop Problem")."""
    words = [w for w in norm(name).split() if w not in PLAIN_WORDS and not w.isdigit()]
    return len(words) >= 2 or any(len(w) >= 6 for w in words)


def name_forms(name: str) -> list[str]:
    """A climb's name as a title may give it, normalised: as it is, without a leading "the", and with its last word
    singular ("Arbor Days" is often "Arbor Day")."""
    out = [norm(name)]
    words = out[0].split()
    if len(words) > 1 and words[0] == "the":
        out.append(" " + " ".join(words[1:]) + " ")
    for w in list(out):
        ws = w.split()
        if ws and len(ws[-1]) > 3 and ws[-1].endswith("s") and not ws[-1].endswith("ss"):
            out.append(" " + " ".join(ws[:-1] + [ws[-1][:-1]]) + " ")
    return list(dict.fromkeys(out))


def names_only_its_boulder(c: Climb, title: str | None) -> bool:
    """Whether a title names this climb only as part of its boulder's or area's name, and names another climb on the
    same boulder (c.boulder_mates): "Zinger Boulder. Dish Mantel. V2" is of Dish Mantel, not of Zinger."""
    t = norm(title)
    forms = [f for f in name_forms(c.name) if len(f.strip()) >= 3]
    if any(f in t for m in c.boulder_mates for f in name_forms(m) if len(f.strip()) >= 3):
        for ctx in sorted(set(context(c) + [norm(c.boulder_name or "")]), key=len, reverse=True):
            if ctx.strip() and any(f in ctx for f in forms):
                t = t.replace(ctx, " ")
        return not any(f in t for f in forms)
    return False


def matches(c: Climb, title: str | None, description: str | None = None) -> bool:
    """Whether a video is of this climb: its title names it (name_forms()), and the title or description places it, or
    the title gives its grade too and its name is distinctive(): its V grade, or for a climbing-history.org climb its
    Fontainebleau grade (font_grades(): "Burden of Dreams 9A"). Not if it names it only within its boulder's name,
    and names another of its boulder's climbs (names_only_its_boulder())."""
    forms = [f for f in name_forms(c.name) if len(f.strip()) >= 3]
    if not forms or not any(f in norm(title) for f in forms) or names_only_its_boulder(c, title):
        return False
    text = norm(f"{title} {description or ''}")
    if any(x in text for x in context(c)):
        return True
    grades = [g.lower() for g in [v_grade(c)] if g] + font_grades(c)
    return any(re.search(rf"\b{g}\b", norm(title)) for g in grades) and distinctive(c.name)


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
           asked: list | None = None, days: float = SEARCH_DAYS, state: dict | None = None) -> list[Media]:
    """Videos of a climb found by searching YouTube, best first: by what a search for its words found in the last
    `days`, here or by a volunteer, or else by searching now, unless YouTube is left to volunteers or refusing
    this server, when volunteers are asked to search instead (the harvest carries on without). With wide_retry, a
    search that finds nothing of it is followed by one without its place (wide()).

    Args:
        known: URLs it already has, which aren't returned again.
        want: called with the search's words to ask volunteers for it; whether it's coming.
        asked: the words volunteers were asked to search for are added to it.
        state: given {"checked": whether YouTube was searched for it, here or by a volunteer, "words": its search}.
    """
    q = query(c)
    if state is not None:
        state.update(checked=False, words=q)
    out = _search_one(c, q, log, known, want, asked, days)
    if out is None:
        return []
    if not out and wide_retry and wide(c) != q:
        asked = [] if asked is None else asked
        n = len(asked)
        more = _search_one(c, wide(c), log, known, want, asked, days)
        if more is None and len(asked) > n:     # its wider search waits for a volunteer
            if state is not None:
                state.update(words=wide(c))
            return []
        out = more or []    # or failed: the narrower one stands
        if more is not None:
            log_wide(c, q, wide(c), out)
    if state is not None:
        state["checked"] = True
    return out[:MAX_MATCHES]


def log_wide(c: Climb, narrow: str, words: str, kept: list[Media]) -> None:
    """Keep, once for each wide search, what it found that the search with the place didn't: every result new to it,
    whether matches() took it and why, for judging the filtering later (WIDE_LOG, a line of JSON each)."""
    try:
        hit, before = cached_search(words, days=1e9), cached_search(narrow, days=1e9)
        if not hit or (WIDE_LOG.exists() and f'"wide": {json.dumps(words)}, "fetched": {hit["fetched"]}' in WIDE_LOG.read_text()):
            return      # that search kept already (a harvest uses it again from the cache)
        seen = {e.get("id") or e.get("url") for e in (before or {}).get("results") or []}
        took = {m.url for m in kept}
        ctx = context(c)
        rows = []
        for e in hit["results"]:
            if (e.get("id") or e.get("url")) in seen:
                continue
            url = e.get("url") or (f"https://www.youtube.com/watch?v={e['id']}" if e.get("id") else None)
            text = norm(f"{e.get('title')} {e.get('description') or ''}")
            rows.append({"url": url, "title": e.get("title"), "description": (e.get("description") or "")[:300],
                         "kept": url in took, "placed": any(x in text for x in ctx)})
        line = {"at": time.time(), "climb": c.name, "grade": c.grade, "area_path": c.area_path, "place": place(c),
                "narrow": narrow, "wide": words, "fetched": hit["fetched"], "distinctive": distinctive(c.name), "results": rows}
        with open(WIDE_LOG, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:   # only the record is lost
        pass


def _search_one(c: Climb, q: str, log: Log, known: set[str], want, asked: list | None, days: float) -> list[Media] | None:
    """search() for one set of words: the videos of the climb it found, or None if it wasn't searched."""
    hit = cached_search(q, days)
    if hit:
        log(f"youtube search: {q!r}: as {'a volunteer' if hit.get('by') == 'volunteer' else 'this server'} searched "
            f"{(time.time() - hit['fetched']) / 3600:.0f} h ago")
    else:
        why = None
        if via == "volunteers":
            why = "this server leaves YouTube to volunteers"
        elif OFFLINE:
            log("youtube search: skipped (YOUTUBE_OFFLINE is set)")
            return None
        elif not available():
            log("youtube search: skipped (needs yt-dlp)")
            return None
        else:
            try:
                hit = save_search(q, _ytdlp(["--flat-playlist", f"ytsearch{MAX_RESULTS}:{q}"], video=False).get("entries") or [], "here")
            except Refused as e:
                why = str(e)
            except Exception as e:
                log(f"youtube search: {q!r} failed: {e}")
                return None
        if not hit:
            if want and via != "here" and want(q):
                if asked is not None:
                    asked.append(q)
                log(f"youtube search: {why}: asked volunteers to search for {q!r}; this climb is harvested again when they have")
            else:
                log(f"youtube search: {q!r} skipped: {why}")
            return None
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
    return out


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
