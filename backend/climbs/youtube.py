"""Finding a climb's videos on YouTube, and what their uploaders say about the climber's size.

Uses yt-dlp, which the harvest already downloads videos with: its search and metadata are the same
kind of access. A search result counts only if its title names the climb and something placing it
(its area, boulder or grade), as climb names like "Relic" aren't unique. design.md has how well that
picked out the right videos.

Climbers often give their height or ape index in a video's description or on their channel, which
could later give a mesh a scale. stats() reads them from free text; nothing is inferred.

Every yt-dlp run goes through call(): one at a time across the app, GAP seconds apart. When YouTube
answers with its bot check ("Sign in to confirm you're not a bot") or a rate limit, nothing that fetches
a video or its details is asked of it for an hour, then 2, 4 and 8 while it still answers that way;
searches still go through, as they never got the check. design.md has what led to this.
"""
from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import threading
import time
import unicodedata
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
_gate = threading.Lock()
_last = 0.0


class Refused(RuntimeError):
    """YouTube is refusing this machine for now (its bot check, or a rate limit)."""

    def __init__(self, state: dict):
        self.state = state
        super().__init__(f"YouTube is refusing this machine's downloads (its bot check) until "
                         f"{time.strftime('%H:%M UTC', time.gmtime(state['until']))}")


def available() -> bool:
    return shutil.which("yt-dlp") is not None


def refused() -> dict | None:
    """While YouTube is being left alone: {since, until, level, error}, else None."""
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return state if time.time() < state.get("until", 0) else None


def call(cmd: list[str], video: bool = True, **kw) -> str:
    """Run a yt-dlp command (jobctl.run's arguments) through the gate.

    Args:
        video: it fetches a video or a video's details, which YouTube's bot check applies to: refused
            while that's on, and turning it on when YouTube answers with it.

    Raises:
        Refused: YouTube is refusing, or just did.
    """
    global _last
    while not _gate.acquire(timeout=1):
        jobctl.check()
    try:
        try:
            state = json.loads(STATE_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            state = None
        if video and state and time.time() < state["until"]:
            raise Refused(state)
        until = _last + random.uniform(*GAP)
        while time.time() < until:
            jobctl.check()
            time.sleep(min(1.0, until - time.time()))
        try:
            out = jobctl.run(cmd[:1] + SLEEP_REQUESTS + cmd[1:], **kw)
        except Exception as e:
            if video and REFUSAL.search(str(e)):
                level = min(len(BLOCK_HOURS) - 1, state["level"] + 1) if state else 0
                state = {"since": state["since"] if state else time.time(), "level": level,
                         "until": time.time() + BLOCK_HOURS[level] * 3600, "error": str(e)[:300]}
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                STATE_FILE.write_text(json.dumps(state))
                raise Refused(state) from None
            raise
        finally:
            _last = time.time()
        if video and state:     # it let one through: it's stopped refusing
            STATE_FILE.unlink(missing_ok=True)
        return out
    finally:
        _gate.release()


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


def search(c: Climb, log: Log, known: set[str] = frozenset()) -> list[Media]:
    """Videos of a climb found by searching YouTube, best first.

    Args:
        known: URLs it already has, which aren't returned again.
    """
    if not available():
        log("youtube search: skipped (needs yt-dlp)")
        return []
    q = query(c)
    try:
        found = _ytdlp(["--flat-playlist", f"ytsearch{MAX_RESULTS}:{q}"], video=False).get("entries") or []
    except Exception as e:
        log(f"youtube search: {q!r} failed: {e}")
        return []
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
