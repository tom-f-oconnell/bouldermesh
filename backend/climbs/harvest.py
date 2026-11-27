"""Gather publicly available photos (and video frames) of a boulder for photogrammetry.

Sources, each optional and independently switchable:

  climb      photos already attached to the climb record (Mountain Project / OpenBeta)
  google     Google Custom Search JSON API, image mode.  Needs GOOGLE_CSE_KEY + GOOGLE_CSE_CX
             (free tier: 100 queries/day).  This is the *supported* way to query Google
             Images; scraping google.com/images is against Google's ToS and is not done here.
  ddg        DuckDuckGo image search via its unofficial i.js endpoint (no key; brittle,
             may break or rate-limit; HARVEST_DDG=0 turns it off).
  commons    Wikimedia Commons geosearch around the climb's coordinates (no key, CC-licensed).
  flickr     Flickr API photo search around the coordinates (needs FLICKR_KEY; CC filter on
             by default).
  youtube    frames extracted from YouTube/Vimeo beta videos linked on the climb page
             (needs yt-dlp and ffmpeg on PATH).  Beta videos often pan around a boulder and
             are the most photogrammetry-friendly source of all.

Every saved file gets a line in <project>/photos/sources.json with url, page, source, licence
and credit so the provenance is auditable.  Images are stored locally for reconstruction
only - most are copyrighted by their photographers; don't redistribute them.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urlencode

from . import youtube
from .common import ApiError, Climb, HttpError, Media, current_control, http, jobctl, use_control

Log = Callable[[str], None]
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
SOURCES = ("climb", "google", "ddg", "commons", "flickr", "youtube")
POOL_SKIP = ("small", "unreadable", "dup", "card", "notboulder")   # what other harvests judged unusable, not pooled
POOL_MAX = 400            # photos pooled into one harvest at most
PEOPLE_WORDS = re.compile(r"\b(climber|climbing|sends?|sending|flash|topping out|dyno|mantle)\b", re.I)


# ----------------------------------------------------------------------------- queries
def search_terms(c: Climb) -> list[str]:
    """Query strings that tend to return the rock rather than the climber."""
    boulder = c.boulder_name or c.guess_boulder_name()
    area = c.area_path[-2] if len(c.area_path) >= 2 else (c.area_path[-1] if c.area_path else "")
    region = next((p for p in c.area_path if re.search(r"\b(park|forest|valley|canyon|mountain|national)\b", p, re.I)), "")
    terms = []
    if boulder and boulder.lower() != c.name.lower():
        terms += [f'"{boulder}" {area} boulder', f'"{boulder}" bouldering {region}'.strip()]
    terms += [f'"{c.name}" {c.grade or ""} boulder {area}'.strip(), f'"{c.name}" bouldering {region}'.strip()]
    if c.kind == "area":
        terms = [f'"{c.name}" boulder {area}'.strip(), f'"{c.name}" bouldering']
    seen, out = set(), []
    for t in terms:
        t = re.sub(r"\s+", " ", t).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


# ----------------------------------------------------------------------------- sources
def from_climb(c: Climb, log: Log, progress: Callable[..., None] | None = None) -> list[Media]:
    out = []
    for i, m in enumerate(c.images):
        if progress:
            progress(i / max(1, len(c.images)), f"photo {i + 1} of {len(c.images)} on the climb's page")
        if m.source == "mountainproject":
            try:
                from . import mountainproject
                mountainproject.resolve_image(m, prefer_large=True)
            except Exception as e:
                log(f"  mp image resolve failed: {e}")
        if m.url:
            out.append(m)
    log(f"climb record: {len(out)} photo(s)")
    return out


def google_images(queries: list[str], log: Log, per_query: int = 20) -> list[Media]:
    key, cx = os.environ.get("GOOGLE_CSE_KEY"), os.environ.get("GOOGLE_CSE_CX")
    if not (key and cx):
        log("google: skipped (set GOOGLE_CSE_KEY and GOOGLE_CSE_CX; create a Programmable Search Engine with "
            "'search the entire web' + image search enabled)")
        return []
    out = []
    for q in queries:
        for start in range(1, per_query + 1, 10):
            params = {"key": key, "cx": cx, "q": q, "searchType": "image", "num": 10, "start": start,
                      "imgSize": "large", "safe": "active"}
            try:
                res = http.get_json("https://www.googleapis.com/customsearch/v1?" + urlencode(params), ttl=7 * 86400,
                                    obey_robots=False)
            except Exception as e:
                log(f"google: {q!r} failed: {str(e).replace(key, '<key>')}")
                break
            items = res.get("items") or []
            for it in items:
                img = it.get("image") or {}
                out.append(Media(url=it["link"], kind="image", page=img.get("contextLink"), title=it.get("title"),
                                 width=img.get("width"), height=img.get("height"), source="google",
                                 license="unknown (web image; check page)"))
            if len(items) < 10:
                break
        log(f"google: {q!r} -> {len(out)} so far")
    return out


def ddg_enabled() -> bool:
    """Whether DuckDuckGo image search may be used: yes, unless HARVEST_DDG=0."""
    return os.environ.get("HARVEST_DDG", "1") != "0"


def ddg_images(queries: list[str], log: Log, per_query: int = 40) -> list[Media]:
    """Unofficial DuckDuckGo image endpoint. Off if HARVEST_DDG=0."""
    if not ddg_enabled():
        log("ddg: skipped (HARVEST_DDG=0)")
        return []
    out = []
    for q in queries:
        try:
            html = http.get_text("https://duckduckgo.com/?" + urlencode({"q": q, "iax": "images", "ia": "images"}),
                                 ttl=86400)
            m = re.search(r"vqd=['\"]?([\d-]+)", html)
            if not m:
                log(f"ddg: no vqd token for {q!r} (endpoint changed?)")
                continue
            res = http.get_json("https://duckduckgo.com/i.js?" + urlencode(
                {"l": "us-en", "o": "json", "q": q, "vqd": m[1], "f": ",,,,,", "p": "1"}),
                headers={"Referer": "https://duckduckgo.com/"}, ttl=86400)
            for r in (res.get("results") or [])[:per_query]:
                out.append(Media(url=r.get("image"), kind="image", page=r.get("url"), title=r.get("title"),
                                 width=r.get("width"), height=r.get("height"), source="ddg",
                                 license="unknown (web image; check page)"))
            log(f"ddg: {q!r} -> {len(out)} so far")
        except Exception as e:
            log(f"ddg: {q!r} failed: {e}")
    return out


def commons_geosearch(lat: float, lng: float, log: Log, radius_m: int = 400, limit: int = 50) -> list[Media]:
    # iiurlwidth is one of Wikimedia's standard thumbnail sizes: others return the (heavily rate-limited) original
    params = {"action": "query", "format": "json", "generator": "geosearch", "ggscoord": f"{lat}|{lng}",
              "ggsradius": min(radius_m, 10000), "ggsnamespace": 6, "ggslimit": limit,
              "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1920}
    for attempt in range(3):
        try:
            res = http.get_json("https://commons.wikimedia.org/w/api.php?" + urlencode(params), ttl=7 * 86400,
                                obey_robots=False, error=lambda r: (r.get("error") or {}).get("info"))
            break
        except Exception as e:
            if isinstance(e, ApiError) and attempt < 2:  # geosearch is often "too busy" for a moment
                log(f"commons: {e} Retrying.")
                time.sleep(15)
                continue
            log(f"commons: failed: {e}")
            return []
    out = []
    for p in (res.get("query") or {}).get("pages", {}).values():
        for ii in p.get("imageinfo") or []:
            if not re.search(r"\.(jpe?g|png)(\?|$)", ii.get("url", ""), re.I):  # URLs carry utm_ parameters
                continue
            em = ii.get("extmetadata") or {}
            out.append(Media(url=ii.get("thumburl") or ii["url"], kind="image", page=ii.get("descriptionurl"),
                             title=p.get("title"), credit=re.sub("<[^>]+>", "", (em.get("Artist") or {}).get("value", "")),
                             license=(em.get("LicenseShortName") or {}).get("value"),
                             width=ii.get("thumbwidth") or ii.get("width"), height=ii.get("thumbheight") or ii.get("height"),
                             source="commons"))
    log(f"commons: {len(out)} geotagged image(s) within {radius_m} m")
    return out


# flickr.photos.licenses.getInfo's ids; any others are recorded by number
FLICKR_LICENSES = {"0": "All rights reserved", "1": "CC BY-NC-SA 2.0", "2": "CC BY-NC 2.0", "3": "CC BY-NC-ND 2.0",
                   "4": "CC BY 2.0", "5": "CC BY-SA 2.0", "6": "CC BY-ND 2.0", "7": "No known copyright restrictions",
                   "8": "United States Government Work", "9": "CC0 1.0", "10": "Public Domain Mark 1.0"}


def flickr_search(lat: float, lng: float, queries: list[str], log: Log, radius_km: float = 0.5,
                  cc_only: bool = True, limit: int = 100) -> list[Media]:
    key = os.environ.get("FLICKR_KEY")
    if not key:
        log("flickr: skipped (set FLICKR_KEY)")
        return []
    base = {"method": "flickr.photos.search", "api_key": key, "format": "json", "nojsoncallback": 1,
            "lat": lat, "lon": lng, "radius": radius_km, "radius_units": "km", "per_page": min(limit, 250),
            "extras": "url_l,url_o,geo,license,owner_name", "content_type": 1, "media": "photos"}
    if cc_only:
        base["license"] = "1,2,3,4,5,6,7,9,10"
    out = []
    for text in [""] + queries[:2]:
        params = dict(base, **({"text": text} if text else {}))
        try:
            res = http.get_json("https://api.flickr.com/services/rest/?" + urlencode(params), ttl=7 * 86400,
                                obey_robots=False, error=lambda r: r.get("message") if r.get("stat") == "fail" else None)
        except Exception as e:
            log(f"flickr: failed: {str(e).replace(key, '<key>')}")
            break
        for p in (res.get("photos") or {}).get("photo", []):
            u = p.get("url_o") or p.get("url_l")
            if u:
                out.append(Media(url=u, kind="image", page=f"https://www.flickr.com/photos/{p['owner']}/{p['id']}",
                                 title=p.get("title"), credit=p.get("ownername"),
                                 license=FLICKR_LICENSES.get(str(p.get("license")), f"flickr license id {p.get('license')}"),
                                 source="flickr"))
        if not text:
            log(f"flickr: {len(out)} geotagged photo(s) within {radius_km} km")
    return out


# How a video's frames are chosen, kept with each harvest's videos ("way"), so that meshes made from frames chosen an
# older way can be told (the admin page's "frames" tier). 1: the sharpest in each of its share's equal stretches of the
# whole video, then repeats left out. 2 (Nov 26, 2026): that still, for a video of one shot, under LONG_SECONDS, with no
# moment linked (the same frames as before); otherwise by its shots, from its linked moment, or over a long video's
# thinner candidates (extract_frames()).
FRAMES_VERSION = 2
# what's asked of YouTube for a video: the best at up to 1080p that fits in 500 MB (volunteers.VIDEO_FORMAT, which says why)
VIDEO_FORMAT = "bv*[height<=1080][filesize<?500M][filesize_approx<?500M]/b"
MOMENT_BEFORE = 10.0        # seconds before a linked moment that frames are taken from (links are often a little late)
MOMENT_SECONDS = 300.0      # and after it: as long as beta looks at a video (beta.MAX_SECONDS)
LONG_SECONDS = 900.0        # a video longer than this (the longest a search keeps: youtube.MAX_MINUTES) has its candidate
MAX_CANDIDATES = 900        # frames spread over it, this many, rather than one a second however long it is
SHOT_ALIKE = 0.85           # two candidates in a row whose small copies correlate this well are one shot, without matching
VIEW_ALIKE = 0.9            # and a candidate this like a view's kept frame shows it again (a camera that didn't move)
SHOT_MATCHES = 15           # else they're one shot if this many of their features match with one geometry (diversity.pair())
SHOT_LOOKBACK = 8           # a shot showing what one of this many shots before it ended on carries that shot on
VIEW_LOOKBACK = 2           # within a shot, a frame is held against this many of the views before it
EDIT_SPARE = 2              # a video of several shots is given this many times its places, as the filter takes out what isn't the rock


def video_seconds(video: Path) -> float | None:
    """A video's length (ffprobe's), or None if it can't be read."""
    try:
        return float(jobctl.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", str(video)], timeout=60, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True).strip())
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        return None


def extract_frames(video: Path, work: Path, keep: int, log: Log, fps: float | None = None,
                   stats: dict | None = None, start: float | None = None, edits: bool = False) -> list[Path]:
    """A video's frames for meshing, as JPEGs in work/ (f0001.jpg...), the rest deleted: at most keep of them.

    As it always was, unless edits: its sharpest frames, one in each of keep equal stretches of it, less those that
    repeat one kept (drop_repeats()). With edits (a video found for a climb, which may be cut together from several
    shots, or an hour long), that's still how a video of one shot is done, if it's no longer than LONG_SECONDS and no
    moment is linked: the same frames as before.

    Otherwise its share goes to what it shows rather than to its minutes (by_shots()): each shot's distinct views
    are counted, and the frames spread evenly over them, so that an edit's ten seconds round the rock aren't one frame
    beside three minutes from a tripod. With a moment linked (start), only MOMENT_BEFORE before it to MOMENT_SECONDS
    after is looked at; a video longer than LONG_SECONDS is looked at whole, but through MAX_CANDIDATES frames.

    Args:
        keep: how many to keep at most.
        stats: given, filled with how many places it took ("chosen": those of repeats and views passed over among
            them, which aren't for another video's frames), how many of those it left empty ("repeats"), each kept
            frame's time in the video in seconds ("times", by file name), its "shots" and "views" where counted,
            "way" ("stretches" or "shots") and "span" ([from, to] in seconds, what was looked at).
        fps: frames a second to choose from; by default about twice keep of them over the video's length (ffprobe's),
            from 0.5 to 4 a second, or 1 if its length can't be read.
        start: the moment its link points to, in seconds, if it does (only used with edits).
        edits: look for shots, and use start, as above.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("extracting frames needs ffmpeg on PATH")
    if keep <= 0:
        return []
    secs = video_seconds(video)
    if fps is None:
        fps = min(4.0, max(0.5, 2 * keep / secs)) if secs else 1.0
    moment = bool(edits and start is not None and secs and 0 <= start < secs)
    t0 = max(0.0, start - MOMENT_BEFORE) if moment else 0.0
    length = min(MOMENT_BEFORE + MOMENT_SECONDS, secs - t0) if moment else secs
    long = bool(edits and length and length > LONG_SECONDS)
    if long and length * fps > MAX_CANDIDATES:
        fps = MAX_CANDIDATES / length
    work.mkdir(parents=True, exist_ok=True)
    jobctl.run([ffmpeg, "-loglevel", "error", "-threads", "2", *(["-ss", f"{t0:.2f}", "-t", f"{length:.2f}"] if moment else []),
                "-i", str(video), "-vf", f"fps={fps:g}", "-q:v", "2", str(work / "f%04d.jpg")], timeout=900)
    frames = sorted(work.glob("f*.jpg"))
    times = {f.name: round(t0 + i / fps, 2) for i, f in enumerate(frames)}
    sharpness: dict[Path, float] = {}
    shots = find_shots(frames, log) if edits else None
    if shots is None or (len(shots) <= 1 and not moment and not long):       # as before FRAMES_VERSION 2, frame for frame
        sharpest = _sharpest(frames, keep, log, sharpness)
        chosen = drop_repeats(sharpest, sharpness, log)
        took, way, views = len(sharpest), "stretches", None
    else:
        # an edit of several shots shows much besides the rock, which the off-topic filter takes out (its frames have to
        # match, as relevance.filter_photos()'s unsure): so it's given EDIT_SPARE times its places to choose from
        several = len(shots) > 1
        picked, views = by_shots(shots, keep * EDIT_SPARE if several else keep, log, sharpness)
        chosen = drop_repeats(picked, sharpness, log)       # the same view come back to after other shots
        took, way = min(keep, len(frames)), "shots"
        log(f"frames: {len(shots)} shot(s) with {views} distinct view(s)"
            + (f", from {t0:.0f} s to {t0 + length:.0f} s (its link points to {start:.0f} s)" if moment else "")
            + (f", through {len(frames)} frames of its {length / 60:.0f} min" if long else "") + f": {len(chosen)} kept")
    if stats is not None:
        of = {f: i for i, shot in enumerate(shots or []) for f in shot}
        stats.update(chosen=took, repeats=max(0, took - len(chosen)), times={f.name: times[f.name] for f in chosen}, way=way,
                     shot_of={f.name: of[f] for f in chosen} if way == "shots" and len(shots) > 1 else None,
                     shots=len(shots) if shots is not None else None, views=views,
                     span=[round(t0, 2), round(t0 + length, 2)] if length else None)
    for f in set(frames) - set(chosen):
        f.unlink()
    return chosen


def _small(f: Path):
    """A frame as 64 x 36 grey levels, with mean 0 and deviation 1 (None if it can't be read, or is flat: black, a
    plain card): what two frames are compared by before anything dearer."""
    import numpy as np
    from PIL import Image
    try:
        with Image.open(f) as im:
            im.draft("L", (128, 72))
            a = np.asarray(im.convert("L").resize((64, 36), Image.BILINEAR), dtype=np.float32)
    except Exception:  # noqa: BLE001
        return None
    sd = float(a.std())
    return (a - a.mean()) / sd if sd > 1.0 else None


def _alike(a, b) -> float:
    """How well two frames' small copies (_small()) correlate, -1 to 1; 0 where either is missing."""
    return float((a * b).mean()) if a is not None and b is not None else 0.0


class _Features:
    """Frames' ORB features (diversity.orb_features()), worked out when first asked for; available is False without OpenCV."""

    def __init__(self):
        try:
            import cv2
            self.bf, self.available = cv2.BFMatcher(cv2.NORM_HAMMING), True
        except ImportError:
            self.bf, self.available = None, False
        self._of: dict[Path, object] = {}

    def of(self, f: Path):
        from . import diversity
        if f not in self._of:
            self._of[f] = (diversity.orb_features([f]) or [None])[0]
        return self._of[f]

    def pair(self, a: Path, b: Path) -> tuple[int, bool] | None:
        """diversity.pair() of two frames, or None if either has too few features to say."""
        from . import diversity
        fa, fb = self.of(a), self.of(b)
        return diversity.pair(fa, fb, self.bf) if fa is not None and fb is not None else None


def find_shots(frames: list[Path], log: Log) -> list[list[Path]]:
    """A video's candidate frames (in time order) split into its shots. A frame starts a new shot where it's cut to:
    its small copy doesn't correlate with the frame before's (SHOT_ALIKE), nor do their features match with one
    geometry (SHOT_MATCHES, which a camera moving fast within one shot still gives). A frame that can't be judged
    (black, blurred to nothing) stays with the shot before it, and the next is held against the last that could be,
    so that a fade through black is a cut. A shot that comes back to a view an earlier one ended on (SHOT_LOOKBACK
    shots back: an edit going between two tripods) carries that one on, so that its view is counted once: by their
    small copies (VIEW_ALIKE), or by their features matching with no parallax (diversity.pair()'s same shot), which
    holds when the climber has moved on and the camera hasn't. Without OpenCV, the small copies decide alone."""
    if not frames:
        return []
    small = {f: _small(f) for f in frames}
    feats = _Features()
    shots, last = [[frames[0]]], frames[0] if small[frames[0]] is not None else None
    for f in frames[1:]:
        if small[f] is None:
            shots[-1].append(f)
            continue
        cut = False
        if last is not None and _alike(small[last], small[f]) < SHOT_ALIKE:
            got = feats.pair(last, f) if feats.available else (0, False)
            cut = got is not None and got[0] < SHOT_MATCHES
        if cut:
            shots.append([])
        shots[-1].append(f)
        last = f
    merged: list[list[Path]] = []
    came_from = None        # the shot the one before this went into, which this was cut away from
    for shot in shots:
        first = next((f for f in shot if small[f] is not None), None)
        back = None
        if first is not None:
            for earlier in reversed(merged[-SHOT_LOOKBACK:]):
                end = next((f for f in reversed(earlier) if small[f] is not None), None)
                if earlier is came_from or end is None:
                    continue
                if _alike(small[end], small[first]) >= VIEW_ALIKE:
                    back = earlier
                    break
                got = feats.pair(end, first) if feats.available else None    # the climber has moved, but the camera hasn't
                if got is not None and got[1]:
                    back = earlier
                    break
        if back is not None:
            back.extend(shot)
        else:
            merged.append(list(shot))
        came_from = back if back is not None else merged[-1]
    return merged


def shot_views(shot: list[Path], sharpness: dict[Path, float], feats: _Features | None = None) -> list[Path]:
    """A shot's distinct views, each as its sharpest frame, in time order: a frame shows a view again if its small copy
    is like that view's kept frame (VIEW_ALIKE), or it's the same shot by diversity.pair() (no parallax: a camera that
    only turned or zoomed), looking back over the shot's last VIEW_LOOKBACK views."""
    feats = feats or _Features()

    def sharp(f: Path) -> float:
        if f not in sharpness:
            try:
                sharpness[f] = _sharpness(f)
            except Exception:  # noqa: BLE001
                sharpness[f] = 0.0
        return sharpness[f]
    views: list[list] = []      # [its kept frame, that frame's small copy]
    for f in shot:
        small, same = _small(f), None
        for v in reversed(views[-VIEW_LOOKBACK:]):
            if _alike(small, v[1]) >= VIEW_ALIKE:
                same = v
                break
        if same is None and feats.available:
            for v in reversed(views[-VIEW_LOOKBACK:]):
                got = feats.pair(f, v[0])
                if got is not None and got[1]:
                    same = v
                    break
        if same is None:
            views.append([f, small])
        elif sharp(f) > sharp(same[0]):
            same[0], same[1] = f, small
    return [v[0] for v in views]


def by_shots(shots: list[list[Path]], keep: int, log: Log, sharpness: dict[Path, float]) -> tuple[list[Path], int]:
    """keep frames of a video spread over what it shows: each shot's distinct views (shot_views()) get places in
    proportion to how many they are, every shot at least one while there are places (the shots with most views first
    when there aren't), and each of a shot's places takes the sharpest of an equal stretch of its views. With no more
    views than places, every view is kept, and the rest of the places stay empty.

    Returns:
        (the frames kept, in time order; how many distinct views the video has).
    """
    feats = _Features()
    views = [shot_views(s, sharpness, feats) for s in shots if s]
    total = sum(len(v) for v in views)
    if total <= keep:
        return sorted(f for v in views for f in v), total
    order = sorted(range(len(views)), key=lambda i: (-len(views[i]), i))
    quota = [0] * len(views)
    for i in order[:keep]:
        quota[i] = 1
    left = keep - sum(quota)
    if left > 0:
        spare = [len(v) - n for v, n in zip(views, quota)]
        want = [left * sp / max(1, sum(spare)) for sp in spare]
        for i in range(len(views)):
            quota[i] += int(want[i])
        for i in sorted(range(len(views)), key=lambda i: (-(want[i] - int(want[i])), i))[:keep - sum(quota)]:
            quota[i] += 1
    out = []
    for v, n in zip(views, quota):
        n = min(n, len(v))
        for k in range(n):
            stretch = v[int(k * len(v) / n):int((k + 1) * len(v) / n)] or [v[-1]]
            out.append(max(stretch, key=lambda f: sharpness.get(f, 0.0)))
    return sorted(out), total


# A video's frame that shows what a frame kept before it does is left out (Tom, Nov 15: the job gets smaller, and the
# freed places aren't filled): the same shot by diversity.py's measure (enough matches, nearly all of them explained by
# one shift, turn and zoom: a camera that didn't move, or only turned or zoomed), which the mapper benchmark measured
# on COLMAP's own matches as "dedup90" (Drifter's frames 176 to 125, the steps after it 782 to 487 s, as many photos
# placed). Each frame is held against the REPEAT_LOOKBACK kept just before it; of two, the sharper is kept.
REPEAT_LOOKBACK = 8


def drop_repeats(frames: list[Path], sharpness: dict[Path, float], log: Log) -> list[Path]:
    """frames (one video's, in time order) without those that repeat a kept one (diversity.pair()'s same shot), the
    sharper of each two kept, in time order. Without OpenCV, all of them."""
    if len(frames) < 2:
        return frames
    from . import diversity
    feats = diversity.orb_features(frames)
    if feats is None:
        log("frames: OpenCV isn't installed, so repeated frames are kept")
        return frames
    import cv2
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    def sharp(f: Path) -> float:
        if f not in sharpness:
            try:
                sharpness[f] = _sharpness(f)
            except Exception:  # noqa: BLE001: the earlier is kept
                sharpness[f] = 0.0
        return sharpness[f]
    kept: list[list] = []      # [frame, features]
    dropped = 0
    for f, feat in zip(frames, feats):
        same = None
        if feat is not None:
            same = next((k for k in reversed(kept[-REPEAT_LOOKBACK:]) if k[1] is not None and diversity.pair(feat, k[1], bf)[1]), None)
        if same is None:
            kept.append([f, feat])
            continue
        dropped += 1
        if sharp(f) > sharp(same[0]):
            same[0], same[1] = f, feat
    if dropped:
        log(f"frames: {dropped} of {len(frames)} left out as repeating a frame kept (the camera didn't move, or only turned "
            "or zoomed)")
    return sorted(k[0] for k in kept)


def video_frames(videos: list[Media], out_dir: Path, log: Log, fps: float = 1.0, max_frames: int = 150,
                 max_videos: int = 6, progress: Callable[..., None] | None = None, infos: dict | None = None,
                 want: Callable[[str], bool] | None = None, asked: list | None = None) -> list[Path]:
    """Download videos and keep their sharpest frames, named yt_<start of the SHA-1 of the URL>_NNN.jpg. A YouTube
    video is kept in youtube's cache, so that finding beta, or harvesting again, needn't ask YouTube for it again,
    and one in the cache isn't asked for.

    Args:
        infos: filled with each video's details (youtube.details) and how many frames it gave, by URL.
        want: called with the URL of each video not had because YouTube is refusing this server, to have it
            fetched by volunteers (whether it's coming); without it, they're skipped.
        asked: the URLs of the videos volunteers were asked for are added to it.
    """
    ytdlp, ffmpeg = shutil.which("yt-dlp"), shutil.which("ffmpeg")
    if not ffmpeg:
        log("youtube: skipped (needs ffmpeg on PATH)")
        return []
    saved, refused, missed = [], None, []
    used = 0        # places taken, those of frames left out as repeats among them (extract_frames())
    n_videos = min(len(videos), max_videos)
    for i, v in enumerate(videos[:max_videos]):
        have = youtube.cached(v.url)
        if not have and (refused or not ytdlp):
            missed.append(v.url)
            continue
        if progress:
            progress(i / n_videos, f"downloading video {i + 1} of {n_videos}")
        tag = hashlib.sha1(v.url.encode()).hexdigest()[:8]
        tmp = out_dir / f"_video_{tag}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            if have:
                log(f"youtube: {v.url}: fetched before")
                vid, details = have, youtube.cached_details(v.url)
                if infos is not None and details:
                    infos[v.url] = {**details, "frames": 0}
            else:
                log(f"youtube: downloading {v.url}")
                youtube.call([ytdlp, "-q", "--no-playlist", "-f", VIDEO_FORMAT, "--max-filesize", "500m",
                              "--write-info-json", "-o", str(tmp / "video.%(ext)s"), v.url], timeout=900,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                info_file = tmp / "video.info.json"
                details = youtube.details(json.loads(info_file.read_text())) if info_file.exists() else None
                if infos is not None and details:
                    infos[v.url] = {**details, "frames": 0}
                vid = next(p for p in tmp.glob("video.*") if not p.name.endswith((".json", ".part")))
                vid = youtube.cache_put(v.url, vid, details)
            if progress:
                progress((i + 0.7) / n_videos, f"extracting frames from video {i + 1} of {n_videos}")
            stats: dict = {}
            frames = extract_frames(vid, tmp, (max_frames - used) // (n_videos - i), log, fps, stats,   # a video that failed leaves its share
                                    start=getattr(v, "start", None), edits=True)
            used += stats.get("chosen", len(frames))
            times, unsure = {}, {}
            for j, f in enumerate(frames):
                dst = out_dir / f"yt_{tag}_{j:03d}.jpg"
                shutil.move(str(f), dst)
                saved.append(dst)
                times[dst.name] = (stats.get("times") or {}).get(f.name)
                if stats.get("shot_of"):
                    unsure[dst.name] = f"{tag}:{stats['shot_of'][f.name]}"
            if infos is not None:
                infos.setdefault(v.url, {}).update(frames=len(frames), times=times, unsure=unsure, way=stats.get("way"), shots=stats.get("shots"),
                                                   views=stats.get("views"), span=stats.get("span"), start=getattr(v, "start", None),
                                                   frames_version=FRAMES_VERSION)
            log(f"youtube: kept {len(frames)} frame(s) from {v.url}")
        except youtube.Refused as e:
            refused = e
            missed.append(v.url)
        except Exception as e:
            log(f"youtube: {v.url} failed: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if missed:
        why = refused or "no yt-dlp on PATH"
        if want:
            coming = [url for url in missed if want(url)]
            if asked is not None:
                asked += coming
            log(f"youtube: {why}: {len(coming)} of {len(missed)} video(s) asked of volunteers; this climb is harvested "
                "again when they come")
        else:
            log(f"youtube: {why}: skipping {len(missed)} video(s). An area's \"look for new videos\" gets them later.")
    return saved


def _sharpness(f: Path) -> float:
    """How sharp a picture is: its Laplacian's variance on a copy at most 640 across (needs PIL and numpy)."""
    import numpy as np
    from PIL import Image, ImageFilter
    im = Image.open(f).convert("L")
    im.thumbnail((640, 640))
    return float(np.asarray(im.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1)), dtype=float).var())


def _sharpest(frames: list[Path], keep: int, log: Log, sharpness: dict[Path, float] | None = None) -> list[Path]:
    """Drop blurry frames (low Laplacian variance) - needs PIL+numpy; otherwise uniform subsample. Each frame's
    sharpness goes into `sharpness`, where given."""
    if len(frames) <= keep:
        return frames
    try:
        scored = []
        for f in frames:
            scored.append((_sharpness(f), f))
            if sharpness is not None:
                sharpness[f] = scored[-1][0]
        # keep the sharpest frame in each of `keep` equal time windows -> coverage + sharpness
        win = len(scored) / keep
        out = []
        for k in range(keep):
            chunk = scored[int(k * win):int((k + 1) * win)] or [scored[-1]]
            out.append(max(chunk)[1])
        return out
    except Exception:
        step = len(frames) / keep
        return [frames[int(i * step)] for i in range(keep)]


# ----------------------------------------------------------------------------- download
def _missing_mark(url: str) -> Path:
    """Where it's kept that a Mountain Project photo's _large_ size isn't there (a 404), so it isn't asked for again."""
    return http._cache_path(url + "|404", kept=True)


def fetch_photo(m: Media) -> bytes:
    """A photo's bytes, kept in the cache for good: a Mountain Project one's _large_ size, or where that isn't there
    (asked once, and remembered: _missing_mark()) its _medium_ (m.url then says which)."""
    large = m.source == "mountainproject" and "_large_" in m.url
    if not (large and _missing_mark(m.url).exists()):
        try:
            return http.get(m.url, binary=True, keep=True, headers={"Referer": m.page or ""})
        except HttpError as e:
            # mountainproject.resolve_image() picks _large_ without probing that it exists
            if not (large and e.status == 404):
                raise
            mark = _missing_mark(m.url)
            mark.parent.mkdir(parents=True, exist_ok=True)
            mark.touch()
    m.url = m.url.replace("_large_", "_medium_")
    return http.get(m.url, binary=True, keep=True, headers={"Referer": m.page or ""})


def photo_cached(m: Media) -> bool:
    """Whether fetch_photo(m) would be answered from the cache, asking Mountain Project for nothing."""
    if http.cached(m.url, keep=True):
        return True
    return "_large_" in m.url and _missing_mark(m.url).exists() and http.cached(m.url.replace("_large_", "_medium_"), keep=True)


def download_all(media: list[Media], out_dir: Path, log: Log, min_bytes: int = 20_000, max_total: int = 200,
                 progress: Callable[..., None] | None = None, skip: set[str] = frozenset()) -> list[dict]:
    """Download each photo (each URL once, none in skip: those a harvest here had already), up to max_total."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    seen_urls = set(skip)
    for i, m in enumerate(media):
        if progress:
            progress(i / max(1, len(media)), f"{i} of {len(media)} photos")
        if not m.url or m.url in seen_urls or len(manifest) >= max_total:
            continue
        seen_urls.add(m.url)
        try:
            data = fetch_photo(m)
        except Exception as e:
            log(f"  skip {m.url[:90]}: {e}")
            continue
        if len(data) < min_bytes:
            continue
        ext = _ext_for(data) or Path(m.url.split("?")[0]).suffix.lower()
        if ext not in IMG_EXT:
            continue
        h = hashlib.sha1(data).hexdigest()[:12]
        name = f"{m.source or 'web'}_{h}{'.jpg' if ext == '.jpeg' else ext}"
        (out_dir / name).write_bytes(data)
        manifest.append({"file": name, **m.to_dict()})
    log(f"downloaded {len(manifest)} image(s)")
    return manifest


def _ext_for(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


# ----------------------------------------------------------------------------- other harvests of the boulder
def pool(project_dir: Path, others: list[Path], log: Log, limit: int = POOL_MAX) -> list[dict]:
    """Photos that other harvests of the same boulder found, linked into this one's photos/: first each one's
    own (its problem's page photos and video frames), then the rest each kept, then what its filter found
    didn't match known photos, as a search for one problem often finds another's side of the rock. Within
    each, the harvests take turns, so that up to the limit, each has as many as any other. What they judged
    junk or duplicates isn't pooled. Own photos are trusted anchors here too (relevance.py), so this harvest
    has its boulder's anchors.

    Args:
        others: those harvests' project folders, most useful first.

    Returns:
        The sources.json entries of the photos added, each with pooled_from: the first project it came
        from, and also_in: any others that had it too.
    """
    photos = project_dir / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    picked, entries, found_in = pool_candidates(others, limit, {p.name for p in photos.iterdir()})
    added = []
    for src, name, o in picked:
        try:
            os.link(src, photos / name)
        except OSError:
            shutil.copy2(src, photos / name)
        entry = {**(entries.get(name) or {"file": name}), "pooled_from": o.name}
        also = [p for p in dict.fromkeys(found_in.get(name, [])) if p != o.name]
        if also:
            entry["also_in"] = also
        added.append(entry)
    if others:
        log(f"pooled {len(added)} photo(s) from {len(others)} other harvest(s) of this boulder")
    return added


def pool_candidates(others: list[Path], limit: int = POOL_MAX, have: set[str] | None = None
                    ) -> tuple[list[tuple[Path, str, Path]], dict[str, dict], dict[str, list[str]]]:
    """What pool() takes from other harvests, in its order, without linking anything: as what a boulder's mesh would be
    made from now (meshinputs.py) needs too.

    Args:
        have: names already in the pooling harvest's photos, not taken again.

    Returns:
        Each photo taken, as (its file, the name it takes, the harvest it's from); the harvests' sources.json entries,
        by name; and the harvests each name is in.
    """
    from .relevance import is_trusted
    have = set(have or ())
    entries: dict[str, dict] = {}
    found_in: dict[str, list[str]] = {}
    own, kept, unmatched = ({o.name: [] for o in others} for _ in range(3))
    for o in others:
        try:
            for f in json.loads((o / "sources.json").read_text()).get("files", []):
                entries.setdefault(f["file"], {**f, "pooled_from": o.name})
        except (OSError, ValueError, KeyError):
            pass
        if (o / "photos").is_dir():
            for f in sorted((o / "photos").iterdir()):
                if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                    (own if is_trusted(f) else kept)[o.name].append((f, f.name, o))
                    found_in.setdefault(f.name, []).append(o.name)
        if (o / "rejected").is_dir():
            for f in sorted((o / "rejected").iterdir()):
                if "_" in f.name and f.name.split("_", 1)[0] not in POOL_SKIP:
                    unmatched[o.name].append((f, f.name.split("_", 1)[1], o))
                    found_in.setdefault(f.name.split("_", 1)[1], []).append(o.name)
    def turns(by: dict[str, list]) -> list:
        return [x for row in itertools.zip_longest(*by.values()) for x in row if x]
    picked = []
    for src, name, o in turns(own) + turns(kept) + turns(unmatched):
        if len(picked) >= limit:
            break
        if name in have or not src.is_file():
            continue
        have.add(name)
        picked.append((src, name, o))
    return picked, entries, found_in


def anchors_of(others: list[Path]) -> list[Path]:
    """The photos other harvests of the same boulder kept from their problems' pages and videos: known photos
    of the boulder, for the relevance filter to match this harvest's web photos against."""
    from .relevance import is_trusted
    return [f for o in others if (o / "photos").is_dir() for f in sorted((o / "photos").iterdir())
            if f.suffix.lower() in (".jpg", ".jpeg", ".png") and is_trusted(f)]


# ----------------------------------------------------------------------------- orchestrate
def steps(sources: set[str], relevance_filter: bool = True, add: bool = False) -> list[tuple[str, str, float]]:
    """The progress steps of harvest(), weighted roughly by duration (with add, no photos are found or downloaded)."""
    out = [] if add else [("find", "Find photos", 1), ("download", "Download photos", 5)]
    if "youtube" in sources:     # alongside the photos, so that volunteers are asked at once
        out.append(("video", "YouTube videos and their frames", 5, True))
    out.append(("dedupe", "Remove duplicates", 1))
    if relevance_filter:
        out.append(("filter", "Filter off-topic photos", 3))
    return out + [("diversity", "Check viewpoint diversity", 3)]


def photos_part(c: Climb, sources: set[str], queries: list[str], photos_dir: Path, log: Log,
                progress: Callable[[str], Callable[..., None] | None], skip: set[str] = frozenset()) -> list[dict]:
    """harvest()'s photos: found in each of its sources, then downloaded into photos_dir (but those at URLs in skip,
    which it had already); their manifest."""
    media: list[Media] = []
    finding = [s for s in ("climb", "google", "ddg", "commons", "flickr") if s in sources]
    report_find = progress("find") or (lambda frac=0.0, detail=None: None)
    for i, s in enumerate(finding):
        report_find(i / len(finding), s)
        if s == "climb":
            media += from_climb(c, log, progress=lambda f=0.0, d=None, i=i: report_find((i + f) / len(finding), d))
        elif s == "google":
            media += google_images(queries, log)
        elif s == "ddg":
            media += ddg_images(queries, log)
        elif c.lat is None or c.lng is None:
            log(f"no coordinates -> skipping {s}")
        elif s == "commons":
            media += commons_geosearch(c.lat, c.lng, log)
        else:
            media += flickr_search(c.lat, c.lng, queries, log)
    if skip:
        log(f"{sum(1 for m in media if m.url in skip)} of the {len(media)} photo(s) found were found before: only the rest are downloaded")
    return download_all(media, photos_dir, log, progress=progress("download"), skip=skip)


def harvest(c: Climb, project_dir: Path, log: Log, sources: set[str] | None = None, relevance_filter: bool = True,
            tracker=None, pool_from: list[Path] | None = None, anchors_from: list[Path] | Callable[[], list[Path]] | None = None,
            want: Callable[[str], bool] | None = None, want_search: Callable[[str], bool] | None = None,
            add: bool = False, refresh: bool = False, rejudge: bool = False) -> dict:
    """Collect photos for climb `c` into project_dir/photos (provenance + reports are written
    next to it, never inside it, so the reconstruction only sees images).

    Args:
        sources: where to look; None for all of SOURCES, and none for only what pool_from has.
        relevance_filter: drop photos not substantially of the boulder (see relevance.py).
        tracker: a progress.Tracker with this harvest's steps().
        pool_from: other harvests of the same boulder, whose photos are added (pool()).
        anchors_from: other harvests of the same boulder, whose own photos (anchors_of()) the relevance
            filter matches web photos against, without adding them; or what gives them, called just before the filter
            (which may wait there for those to finish: server.FilterTurns).
        want: given the URL of each video YouTube wouldn't let this server have, to ask volunteers (video_frames()).
        want_search: given the words of a YouTube search this server won't do, to ask volunteers (youtube.search()).
        add: add to what an earlier harvest here found, rather than start again: its photos and sources.json stay,
            videos it took frames from (or found none of use in) aren't framed again, and only the new photos are
            judged by the filter (its earlier report's verdicts stand). As when volunteers bring the videos a climb
            waited for. "video_frames" then counts only the new frames.
        refresh: with add, look in every source again (sources), downloading only the photos it hadn't found before.
        rejudge: with add, judge every photo again from scratch (what the filter rejected is put back first), but
            for those put back by hand, which stay; nothing is looked for (sources is then empty).

    Returns:
        A summary, whose "asked" says what volunteers were asked for: {"searches": [words], "videos": [URLs]};
        "youtube_checked" whether YouTube was searched for the climb (here or by a volunteer), and if it wasn't,
        "unsearched" the words of the search it still needs.
    """
    from . import diversity, relevance
    sources = set(SOURCES) if sources is None else set(sources)
    progress = tracker.callback if tracker else lambda key: None
    photos_dir = project_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    queries = search_terms(c)
    log(f"harvest for {c.name!r} (boulder: {c.boulder_name or '?'}, coords: {c.lat}, {c.lng})")
    log("queries: " + " | ".join(queries))
    # YouTube alongside the photos, on a thread of its own: searching it (or asking volunteers to) and fetching the videos
    # needn't wait for the photos, which Mountain Project's minute between requests makes slow
    frames, videos, infos, asked, yt, failed = [], [], {}, {"searches": [], "videos": []}, {"checked": True}, []

    earlier: list[dict] = []
    if add:
        try:
            earlier = json.loads((project_dir / "sources.json").read_text()).get("files") or []
        except (OSError, ValueError):
            earlier = []
    framed = {youtube.video_id(f["url"]) or f["url"] for f in earlier if f.get("kind") == "video-frame" and f.get("url")}

    def youtube_part(control):
        use_control(control)    # stopping the job stops this part too, and the tools it runs
        report_video = progress("video") or (lambda frac=0.0, detail=None: None)
        if want_search is not None and hasattr(report_video, "at"):
            report_video.at("server+volunteers")    # volunteers search YouTube and fetch its videos; frames are taken here
        try:
            report_video(0, "searching YouTube")
            seen = {youtube.video_id(v.url) or v.url for v in c.videos}    # the page's links, in any of YouTube's URL forms
            found = youtube.search(c, log, want=want_search, asked=asked["searches"], state=yt)
            videos.extend(list(c.videos) + [v for v in found if (youtube.video_id(v.url) or v.url) not in seen])
            new = [v for v in videos if (youtube.video_id(v.url) or v.url) not in framed]
            if add and len(new) < len(videos):
                log(f"youtube: {len(videos) - len(new)} video(s) framed before, kept as they are")
            if new:
                frames.extend(video_frames(new, photos_dir, log, progress=progress("video"), infos=infos, want=want,
                                           asked=asked["videos"]))
            if tracker:
                tracker.end("video", detail=f"{len(frames)} frame(s) from {len(videos)} video(s)"
                            + (f"; volunteers asked for {len(asked['searches'])} search(es) and {len(asked['videos'])} video(s)"
                               if asked["searches"] or asked["videos"] else ""))
        except BaseException as e:      # raised again on the harvest's own thread
            failed.append(e)

    alongside = None
    if "youtube" in sources:
        alongside = threading.Thread(target=youtube_part, args=(current_control(),), name="harvest-youtube", daemon=True)
        alongside.start()
    try:
        known = {f["url"] for f in earlier if f.get("url") and f.get("kind") != "video-frame"}
        manifest = list(earlier) + photos_part(c, sources, queries, photos_dir, log, progress, skip=known) if add and refresh \
            else list(earlier) if add else photos_part(c, sources, queries, photos_dir, log, progress)
    finally:
        if alongside:
            alongside.join()
    if failed:
        raise failed[0]
    if videos:
        by_tag = {hashlib.sha1(v.url.encode()).hexdigest()[:8]: v for v in videos}   # as video_frames names them
        listed = {m["file"] for m in manifest}
        for f in frames:
            if f.name in listed:
                continue
            v = by_tag[f.name.split("_")[1]]
            info = infos.get(v.url) or {}
            manifest.append({"file": f.name, "url": v.url, "page": v.url, "kind": "video-frame", "source": "youtube",
                             "t": (info.get("times") or {}).get(f.name),     # seconds into its video (None before FRAMES_VERSION 2)
                             # for a frame of a video of several shots: which video and shot, as the filter needs it to match
                             **({"shot": info["unsure"][f.name]} if (info.get("unsure") or {}).get(f.name) else {}),
                             "found_by": "search" if v.source == "youtube-search" else "page",
                             "title": info.get("title") or v.title, "credit": info.get("uploader") or v.credit,
                             "license": info.get("license") or "frame from linked beta video; licence not recorded"})
    if pool_from:
        manifest += pool(project_dir, pool_from, log)
    # remove webp (COLMAP wants jpg/png) by converting if PIL available
    _normalise_images(photos_dir, log)
    (project_dir / "sources.json").write_text(json.dumps({"climb": c.to_dict(), "queries": queries,
                                                           "files": manifest, "created": time.time()}, indent=1))
    filtered = report = None
    try:
        paths = diversity.dedupe(photos_dir, log, progress=progress("dedupe"))
        if relevance_filter:
            anchors_from = anchors_from() if callable(anchors_from) else anchors_from
            more = anchors_of(anchors_from) if anchors_from else []
            if more:
                log(f"relevance: also matching against {len(more)} photo(s) from the pages and videos of "
                    f"{len(anchors_from)} other harvest(s) of this boulder")
            try:    # what an earlier run here found of photos it had too (a boulder pooled again, a climb harvested again)
                prior = json.loads((project_dir / "relevance.json").read_text())
            except (OSError, ValueError):
                prior = None
            if rejudge:     # from scratch: what it rejected back among the photos, and only the hand's put-backs standing
                back = 0
                for f in sorted((project_dir / "rejected").iterdir()) if (project_dir / "rejected").is_dir() else []:
                    why, _, name = f.name.partition("_")
                    if why in relevance.REASONS and name and not (photos_dir / name).exists():
                        f.rename(photos_dir / name)
                        back += 1
                log(f"relevance: judging every photo again from scratch ({back} rejected before put back to judge)")
                paths = diversity.dedupe(photos_dir, log)
                prior = {"put_back": (prior or {}).get("put_back") or []}
            here = {p.name for p in paths}
            unsure = {m["file"]: m["shot"] for m in manifest if m.get("shot") and m.get("file") in here}
            filtered = relevance.run_filter(paths, project_dir / "rejected", log, progress=progress("filter"),
                                            more_anchors=more, prior=prior, assess=progress("diversity") or (lambda f=0.0, d=None: None),
                                            **({"unsure": unsure} if unsure else {}))
            report = filtered.pop("diversity", None)    # checked on the machine that filtered them, if it was one
            (project_dir / "relevance.json").write_text(json.dumps(filtered, indent=1))
            paths = [p for p in paths if p.name in set(filtered["kept"])]
            if report is not None:
                log(f"diversity: checked on the machine that filtered them: {report.get('verdict')}")
        if report is None:
            report = diversity.assess_unique(paths, log, progress=progress("diversity"))
    except Exception as e:
        log(f"diversity check failed: {e}")
        report = {"error": str(e)}
    (project_dir / "diversity.json").write_text(json.dumps(report, indent=1))
    return {"images": len([p for p in photos_dir.iterdir() if p.suffix.lower() in (".jpg", ".png")]),
            "video_frames": sum(f.exists() for f in frames), "queries": queries, "diversity": report,
            "videos": [{"url": v.url, "found_by": "search" if v.source == "youtube-search" else "page", "title": v.title,
                        "uploader": v.credit, "frames": 0,
                        **{k: x for k, x in infos.get(v.url, {}).items() if k not in ("times", "unsure")}} for v in videos],
            "relevance": {k: filtered[k] for k in ("methods", "counts")} if filtered else None, "asked": asked,
            "youtube_checked": yt["checked"], **({} if yt["checked"] else {"unsearched": yt.get("words")}),
            **({"youtube_method": youtube.SEARCH_METHOD} if yt["checked"] and yt.get("words") else {})}


def _normalise_images(photos_dir: Path, log: Log) -> None:
    try:
        from PIL import Image
    except ImportError:
        for p in photos_dir.glob("*.webp"):
            p.unlink()
        return
    for p in list(photos_dir.iterdir()):
        if p.suffix.lower() == ".webp" or p.suffix.lower() == ".jpeg":
            try:
                Image.open(p).convert("RGB").save(p.with_suffix(".jpg"), quality=95)
                p.unlink()
            except Exception as e:
                log(f"  could not convert {p.name}: {e}")
                p.unlink(missing_ok=True)
