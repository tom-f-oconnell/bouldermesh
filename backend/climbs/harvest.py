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


def extract_frames(video: Path, work: Path, keep: int, log: Log, fps: float | None = None) -> list[Path]:
    """A video's sharpest frames, as JPEGs in work/ (f0001.jpg...), the rest deleted.

    Args:
        keep: how many to keep at most.
        fps: frames a second to choose from; by default about twice keep of them over the video's length (ffprobe's),
            from 0.5 to 4 a second, or 1 if its length can't be read.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("extracting frames needs ffmpeg on PATH")
    if keep <= 0:
        return []
    if fps is None:
        fps = 1.0
        try:
            secs = float(jobctl.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries", "format=duration",
                                     "-of", "csv=p=0", str(video)], timeout=60, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True).strip())
            fps = min(4.0, max(0.5, 2 * keep / secs))
        except (ValueError, ZeroDivisionError, OSError, RuntimeError, subprocess.SubprocessError):
            pass
    work.mkdir(parents=True, exist_ok=True)
    jobctl.run([ffmpeg, "-loglevel", "error", "-threads", "2", "-i", str(video), "-vf", f"fps={fps:g}", "-q:v", "2",
                str(work / "f%04d.jpg")], timeout=900)
    frames = sorted(work.glob("f*.jpg"))
    chosen = _sharpest(frames, keep, log)
    for f in set(frames) - set(chosen):
        f.unlink()
    return chosen


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
                youtube.call([ytdlp, "-q", "--no-playlist", "-f", "bv*[height<=1080]/b", "--max-filesize", "500m",
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
            frames = extract_frames(vid, tmp, (max_frames - len(saved)) // (n_videos - i), log, fps)   # a video that failed leaves its share
            for j, f in enumerate(frames):
                dst = out_dir / f"yt_{tag}_{j:03d}.jpg"
                shutil.move(str(f), dst)
                saved.append(dst)
            if infos is not None and v.url in infos:
                infos[v.url]["frames"] = len(frames)
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


def _sharpest(frames: list[Path], keep: int, log: Log) -> list[Path]:
    """Drop blurry frames (low Laplacian variance) - needs PIL+numpy; otherwise uniform subsample."""
    if len(frames) <= keep:
        return frames
    try:
        import numpy as np
        from PIL import Image, ImageFilter
        scored = []
        for f in frames:
            im = Image.open(f).convert("L")
            im.thumbnail((640, 640))
            lap = np.asarray(im.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1)), dtype=float)
            scored.append((lap.var(), f))
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
def download_all(media: list[Media], out_dir: Path, log: Log, min_bytes: int = 20_000, max_total: int = 200,
                 progress: Callable[..., None] | None = None) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    seen_urls = set()
    for i, m in enumerate(media):
        if progress:
            progress(i / max(1, len(media)), f"{i} of {len(media)} photos")
        if not m.url or m.url in seen_urls or len(manifest) >= max_total:
            continue
        seen_urls.add(m.url)
        try:
            try:
                data = http.get(m.url, binary=True, ttl=90 * 86400, headers={"Referer": m.page or ""})
            except HttpError as e:
                # mountainproject.resolve_image() picks _large_ without probing that it exists
                if not (m.source == "mountainproject" and "_large_" in m.url and e.status == 404):
                    raise
                m.url = m.url.replace("_large_", "_medium_")
                data = http.get(m.url, binary=True, ttl=90 * 86400, headers={"Referer": m.page or ""})
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
    from .relevance import is_trusted
    photos = project_dir / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    have = {p.name for p in photos.iterdir()}
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
    added = []
    for src, name, o in turns(own) + turns(kept) + turns(unmatched):
        if len(added) >= limit:
            break
        if name in have or not src.is_file():
            continue
        try:
            os.link(src, photos / name)
        except OSError:
            shutil.copy2(src, photos / name)
        have.add(name)
        entry = {**(entries.get(name) or {"file": name}), "pooled_from": o.name}
        also = [p for p in dict.fromkeys(found_in.get(name, [])) if p != o.name]
        if also:
            entry["also_in"] = also
        added.append(entry)
    if others:
        log(f"pooled {len(added)} photo(s) from {len(others)} other harvest(s) of this boulder")
    return added


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
                progress: Callable[[str], Callable[..., None] | None]) -> list[dict]:
    """harvest()'s photos: found in each of its sources, then downloaded into photos_dir; their manifest."""
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
    return download_all(media, photos_dir, log, progress=progress("download"))


def harvest(c: Climb, project_dir: Path, log: Log, sources: set[str] | None = None, relevance_filter: bool = True,
            tracker=None, pool_from: list[Path] | None = None, anchors_from: list[Path] | None = None,
            want: Callable[[str], bool] | None = None, want_search: Callable[[str], bool] | None = None,
            add: bool = False) -> dict:
    """Collect photos for climb `c` into project_dir/photos (provenance + reports are written
    next to it, never inside it, so the reconstruction only sees images).

    Args:
        sources: where to look; None for all of SOURCES, and none for only what pool_from has.
        relevance_filter: drop photos not substantially of the boulder (see relevance.py).
        tracker: a progress.Tracker with this harvest's steps().
        pool_from: other harvests of the same boulder, whose photos are added (pool()).
        anchors_from: other harvests of the same boulder, whose own photos (anchors_of()) the relevance
            filter matches web photos against, without adding them.
        want: given the URL of each video YouTube wouldn't let this server have, to ask volunteers (video_frames()).
        want_search: given the words of a YouTube search this server won't do, to ask volunteers (youtube.search()).
        add: add to what an earlier harvest here found, rather than start again: its photos and sources.json stay,
            videos it took frames from (or found none of use in) aren't framed again, and only the new photos are
            judged by the filter (its earlier report's verdicts stand). As when volunteers bring the videos a climb
            waited for. "video_frames" then counts only the new frames.

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
        manifest = list(earlier) if add else photos_part(c, sources, queries, photos_dir, log, progress)
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
                             "found_by": "search" if v.source == "youtube-search" else "page",
                             "title": info.get("title") or v.title, "credit": info.get("uploader") or v.credit,
                             "license": info.get("license") or "frame from linked beta video; licence not recorded"})
    if pool_from:
        manifest += pool(project_dir, pool_from, log)
    # remove webp (COLMAP wants jpg/png) by converting if PIL available
    _normalise_images(photos_dir, log)
    (project_dir / "sources.json").write_text(json.dumps({"climb": c.to_dict(), "queries": queries,
                                                           "files": manifest, "created": time.time()}, indent=1))
    filtered = None
    try:
        paths = diversity.dedupe(photos_dir, log, progress=progress("dedupe"))
        if relevance_filter:
            more = anchors_of(anchors_from) if anchors_from else []
            if more:
                log(f"relevance: also matching against {len(more)} photo(s) from the pages and videos of "
                    f"{len(anchors_from)} other harvest(s) of this boulder")
            try:    # what an earlier run here found of photos it had too (a boulder pooled again, a climb harvested again)
                prior = json.loads((project_dir / "relevance.json").read_text())
            except (OSError, ValueError):
                prior = None
            filtered = relevance.run_filter(paths, project_dir / "rejected", log, progress=progress("filter"),
                                            more_anchors=more, prior=prior)
            (project_dir / "relevance.json").write_text(json.dumps(filtered, indent=1))
            paths = [p for p in paths if p.name in set(filtered["kept"])]
        report = diversity.assess_unique(paths, log, progress=progress("diversity"))
    except Exception as e:
        log(f"diversity check failed: {e}")
        report = {"error": str(e)}
    (project_dir / "diversity.json").write_text(json.dumps(report, indent=1))
    return {"images": len([p for p in photos_dir.iterdir() if p.suffix.lower() in (".jpg", ".png")]),
            "video_frames": sum(f.exists() for f in frames), "queries": queries, "diversity": report,
            "videos": [{"url": v.url, "found_by": "search" if v.source == "youtube-search" else "page", "title": v.title,
                        "uploader": v.credit, "frames": 0, **infos.get(v.url, {})} for v in videos],
            "relevance": {k: filtered[k] for k in ("methods", "counts")} if filtered else None, "asked": asked,
            "youtube_checked": yt["checked"], **({} if yt["checked"] else {"unsearched": yt.get("words")})}


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
