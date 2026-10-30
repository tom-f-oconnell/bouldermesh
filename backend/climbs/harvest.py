"""Gather publicly available photos (and video frames) of a boulder for photogrammetry.

Sources, each optional and independently switchable:

  climb      photos already attached to the climb record (Mountain Project / OpenBeta)
  google     Google Custom Search JSON API, image mode.  Needs GOOGLE_CSE_KEY + GOOGLE_CSE_CX
             (free tier: 100 queries/day).  This is the *supported* way to query Google
             Images; scraping google.com/images is against Google's ToS and is not done here.
  ddg        DuckDuckGo image search via its unofficial i.js endpoint (no key; brittle,
             may break or rate-limit; off by default).
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
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urlencode

from .common import Climb, Media, http

Log = Callable[[str], None]
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
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
def from_climb(c: Climb, log: Log) -> list[Media]:
    out = []
    for m in c.images:
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
                res = http.get_json("https://www.googleapis.com/customsearch/v1?" + urlencode(params), ttl=7 * 86400)
            except Exception as e:
                log(f"google: {q!r} failed: {e}")
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


def ddg_images(queries: list[str], log: Log, per_query: int = 40) -> list[Media]:
    """Unofficial DuckDuckGo image endpoint. Off unless HARVEST_DDG=1."""
    if os.environ.get("HARVEST_DDG", "0") != "1":
        log("ddg: skipped (set HARVEST_DDG=1 to enable the unofficial DuckDuckGo image search)")
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
    params = {"action": "query", "format": "json", "generator": "geosearch", "ggscoord": f"{lat}|{lng}",
              "ggsradius": min(radius_m, 10000), "ggsnamespace": 6, "ggslimit": limit,
              "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 2048}
    try:
        res = http.get_json("https://commons.wikimedia.org/w/api.php?" + urlencode(params), ttl=7 * 86400)
    except Exception as e:
        log(f"commons: failed: {e}")
        return []
    out = []
    for p in (res.get("query") or {}).get("pages", {}).values():
        for ii in p.get("imageinfo") or []:
            if not re.search(r"\.(jpe?g|png)$", ii.get("url", ""), re.I):
                continue
            em = ii.get("extmetadata") or {}
            out.append(Media(url=ii.get("thumburl") or ii["url"], kind="image", page=ii.get("descriptionurl"),
                             title=p.get("title"), credit=re.sub("<[^>]+>", "", (em.get("Artist") or {}).get("value", "")),
                             license=(em.get("LicenseShortName") or {}).get("value"),
                             width=ii.get("thumbwidth") or ii.get("width"), height=ii.get("thumbheight") or ii.get("height"),
                             source="commons"))
    log(f"commons: {len(out)} geotagged image(s) within {radius_m} m")
    return out


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
            res = http.get_json("https://api.flickr.com/services/rest/?" + urlencode(params), ttl=7 * 86400)
        except Exception as e:
            log(f"flickr: failed: {e}")
            break
        for p in (res.get("photos") or {}).get("photo", []):
            u = p.get("url_o") or p.get("url_l")
            if u:
                out.append(Media(url=u, kind="image", page=f"https://www.flickr.com/photos/{p['owner']}/{p['id']}",
                                 title=p.get("title"), credit=p.get("ownername"), license=f"flickr license id {p.get('license')}",
                                 source="flickr"))
        if not text:
            log(f"flickr: {len(out)} geotagged photo(s) within {radius_km} km")
    return out


def video_frames(videos: list[Media], out_dir: Path, log: Log, fps: float = 1.0, max_frames: int = 150,
                 max_videos: int = 4) -> list[Path]:
    ytdlp, ffmpeg = shutil.which("yt-dlp"), shutil.which("ffmpeg")
    if not (ytdlp and ffmpeg):
        log("youtube: skipped (needs yt-dlp and ffmpeg on PATH)")
        return []
    saved = []
    for i, v in enumerate(videos[:max_videos]):
        tag = hashlib.sha1(v.url.encode()).hexdigest()[:8]
        tmp = out_dir / f"_video_{tag}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            log(f"youtube: downloading {v.url}")
            subprocess.run([ytdlp, "-q", "--no-playlist", "-f", "bv*[height<=1080]/b", "--max-filesize", "500m",
                            "-o", str(tmp / "video.%(ext)s"), v.url], check=True, timeout=900,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            vid = next(tmp.glob("video.*"))
            subprocess.run([ffmpeg, "-loglevel", "error", "-i", str(vid), "-vf", f"fps={fps}", "-q:v", "2",
                            str(tmp / "f%04d.jpg")], check=True, timeout=900)
            frames = sorted(tmp.glob("f*.jpg"))
            frames = _sharpest(frames, max_frames // max(1, min(len(videos), max_videos)), log)
            for j, f in enumerate(frames):
                dst = out_dir / f"yt_{tag}_{j:03d}.jpg"
                shutil.move(str(f), dst)
                saved.append(dst)
            log(f"youtube: kept {len(frames)} frame(s) from {v.url}")
        except Exception as e:
            log(f"youtube: {v.url} failed: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
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
def download_all(media: list[Media], out_dir: Path, log: Log, min_bytes: int = 20_000, max_total: int = 200) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    seen_urls = set()
    for m in media:
        if not m.url or m.url in seen_urls or len(manifest) >= max_total:
            continue
        seen_urls.add(m.url)
        try:
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


# ----------------------------------------------------------------------------- orchestrate
def harvest(c: Climb, project_dir: Path, log: Log, sources: set[str] | None = None) -> dict:
    """Collect photos for climb `c` into project_dir/photos (provenance + diversity report
    are written next to it, never inside it, so the reconstruction only sees images)."""
    sources = sources or {"climb", "google", "commons", "flickr", "youtube"}
    photos_dir = project_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    queries = search_terms(c)
    log(f"harvest for {c.name!r} (boulder: {c.boulder_name or '?'}, coords: {c.lat}, {c.lng})")
    log("queries: " + " | ".join(queries))
    media: list[Media] = []
    if "climb" in sources:
        media += from_climb(c, log)
    if "google" in sources:
        media += google_images(queries, log)
    if "ddg" in sources:
        media += ddg_images(queries, log)
    if c.lat is not None and c.lng is not None:
        if "commons" in sources:
            media += commons_geosearch(c.lat, c.lng, log)
        if "flickr" in sources:
            media += flickr_search(c.lat, c.lng, queries, log)
    else:
        log("no coordinates -> skipping geo sources (commons, flickr)")
    manifest = download_all(media, photos_dir, log)
    frames = []
    if "youtube" in sources and c.videos:
        frames = video_frames(c.videos, photos_dir, log)
        manifest += [{"file": f.name, "url": None, "kind": "video-frame", "source": "youtube",
                      "license": "frame from linked beta video; reconstruction use only"} for f in frames]
    # remove webp (COLMAP wants jpg/png) by converting if PIL available
    _normalise_images(photos_dir, log)
    (project_dir / "sources.json").write_text(json.dumps({"climb": c.to_dict(), "queries": queries,
                                                           "files": manifest, "created": time.time()}, indent=1))
    try:
        from . import diversity
        report = diversity.assess(photos_dir, log)
    except Exception as e:
        log(f"diversity check failed: {e}")
        report = {"error": str(e)}
    (project_dir / "diversity.json").write_text(json.dumps(report, indent=1))
    return {"images": len([p for p in photos_dir.iterdir() if p.suffix.lower() in (".jpg", ".png")]),
            "video_frames": len(frames), "queries": queries, "diversity": report}


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
