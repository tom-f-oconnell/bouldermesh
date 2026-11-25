"""Storyboards: checking that a video a volunteer sent is the one asked for, against YouTube's own small frames of it.

YouTube's player shows small frames of a video on its seek bar: storyboards, grids of tiles taken every few seconds
through the whole video, served from i.ytimg.com (YouTube's image servers, not its video servers, and without its bot
check). yt-dlp lists them among a video's formats (sb0-sb3), which a volunteer's script sends with the video. The
server keeps small frames of the video at the storyboard's times (video_frames()), and another volunteer, one
trusted, fetches the storyboard's images (volunteers.Broker's "storyboard" tasks): compare() then says whether they
match. An address only counts if it's i.ytimg.com's and names that video (valid_url()), so a volunteer can't send
images of its own choosing as the storyboard.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

SIZE = 80                   # the width frames and tiles are compared at (in pixels; the height as the tile's shape)
MOST_FRAMES = 1200          # frames kept of a video, at most
MOST_FRAGMENTS = 12         # a storyboard of more images than this isn't asked for
LEAST_WIDTH = 64            # the smallest tiles worth comparing
FLAT = 4.0                  # a tile varying less than this (grey levels' standard deviation) says nothing: black, or a title card
MATCH = 0.7                 # a tile whose best match among the frames near its time correlates at least this well matches
# What the share of tiles that match says, of at least LEAST_TILES that aren't flat: the same video at MATCHED or above,
# another video at MISMATCHED or below, unclear between. To be tuned on real videos (2026-11-20: guesses).
MATCHED, MISMATCHED, LEAST_TILES = 0.6, 0.25, 5


def valid_url(url: str, video_id: str) -> bool:
    """Whether a storyboard's image address is YouTube's, for this video."""
    try:
        u = urlparse(str(url))
    except ValueError:
        return False
    return (u.scheme == "https" and u.hostname in ("i.ytimg.com", "i9.ytimg.com") and
            u.path.startswith(f"/sb/{video_id}/") and len(str(url)) < 2000)


def pick(formats, video_id: str) -> dict | None:
    """The storyboard to check a video by, from yt-dlp's formats (or a volunteer's report of one): {width, height,
    rows, columns, interval (seconds from one tile to the next), fragments: [{url, duration}]}, of the fewest images
    with tiles at least LEAST_WIDTH wide, the largest tiles among those. None if there's none, or it isn't YouTube's."""
    found = []
    for f in formats or []:
        try:
            if not isinstance(f, dict) or not str(f.get("format_id") or "sb").startswith("sb"):
                continue
            frags = [{"url": str(x["url"]), "duration": float(x["duration"])} for x in f.get("fragments") or []]
            w, h, rows, cols = (int(f[k]) for k in ("width", "height", "rows", "columns"))
            if not frags or not 1 <= len(frags) <= MOST_FRAGMENTS or not all(valid_url(x["url"], video_id) for x in frags):
                continue
            if not (8 <= w <= 1000 and 8 <= h <= 1000 and 1 <= rows <= 20 and 1 <= cols <= 20):
                continue
            fps = float(f.get("fps") or 0)
            interval = 1 / fps if fps > 0 else frags[0]["duration"] / (rows * cols)
            if not 0.05 <= interval <= 600:
                continue
            found.append({"width": w, "height": h, "rows": rows, "columns": cols, "interval": interval, "fragments": frags})
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    if not found:
        return None
    wide = [s for s in found if s["width"] >= LEAST_WIDTH] or found
    return min(wide, key=lambda s: (len(s["fragments"]), -s["width"]))


def shape(spec: dict) -> tuple[int, int]:
    """The (width, height) frames and tiles are compared at: SIZE wide, the tiles' shape."""
    return SIZE, max(8, round(SIZE * spec["height"] / spec["width"]))


def length(spec: dict) -> float:
    return sum(f["duration"] for f in spec["fragments"])


def video_frames(path: Path, spec: dict, seconds: float | None = None) -> dict:
    """Small grey frames of a video for compare(): {"rate": frames a second, "stretched": array (n, h, w), "fitted":
    the same letterboxed into the tiles' shape}, at twice the storyboard's rate (fewer for a long video). One ffmpeg,
    on one thread, at low priority."""
    w, h = shape(spec)
    seconds = seconds or length(spec)
    rate = min(2 / spec["interval"], MOST_FRAMES / max(seconds, 1))
    graph = (f"[0:v]fps={rate:.6f},format=gray,split[a][b];[a]scale={w}:{h}:flags=area[s];"
             f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=area,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2[f];[s][f]hstack")
    r = subprocess.run(["nice", "-n", "15", "ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-i", str(path),
                        "-filter_complex", graph, "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True, timeout=1800)
    if r.returncode:
        raise RuntimeError(f"ffmpeg: {r.stderr.decode(errors='replace')[-300:]}")
    both = np.frombuffer(r.stdout, np.uint8)
    n = len(both) // (2 * w * h)
    both = both[:n * 2 * w * h].reshape(n, h, 2 * w)
    return {"rate": rate, "stretched": both[:, :, :w].copy(), "fitted": both[:, :, w:].copy()}


def save_frames(frames: dict, path: Path) -> None:
    with open(path, "wb") as f:
        np.savez_compressed(f, rate=np.array(frames["rate"]), stretched=frames["stretched"], fitted=frames["fitted"])


def load_frames(path: Path) -> dict:
    with np.load(path) as z:
        return {"rate": float(z["rate"]), "stretched": z["stretched"], "fitted": z["fitted"]}


def tiles(images: list[bytes], spec: dict, size: tuple[int, int] | None = None) -> list[tuple[float, np.ndarray]]:
    """A storyboard's tiles, each (its time in the video, grey at size: (width, height), shape()'s by default), from its
    images in order. A tile past the video's end (the last image's blank ones) is left out.

    Raises:
        ValueError: an image that isn't one, or too few of them.
    """
    from PIL import Image
    if len(images) != len(spec["fragments"]):
        raise ValueError(f"{len(images)} image(s) of the storyboard's {len(spec['fragments'])}")
    w, h = size or shape(spec)
    out, end, per = [], length(spec), spec["rows"] * spec["columns"]
    for i, data in enumerate(images):
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception as e:  # noqa: BLE001 - PIL raises many kinds
            raise ValueError(f"not an image: {e}") from None
        im = im.convert("L")
        # the last image YouTube cuts short after the last row it fills (800x270 for 6 rows of 80x45): its tiles are
        # the shape of the others, not its height shared among every row (Nov 24: every short video's check failed so)
        tw, full = im.width // spec["columns"], im.height // spec["rows"]
        th = round(tw * spec["height"] / spec["width"]) if spec.get("width") and spec.get("height") else full
        th = full if abs(full - th) <= 1 else th
        if tw < 8 or th < 8:
            raise ValueError(f"an image of {im.width}x{im.height} can't hold {spec['columns']}x{spec['rows']} tiles")
        for r in range(min(spec["rows"], im.height // th)):
            for c in range(spec["columns"]):
                t = (i * per + r * spec["columns"] + c) * spec["interval"]
                if t < end:
                    tile = im.crop((c * tw, r * th, (c + 1) * tw, (r + 1) * th)).resize((w, h), Image.Resampling.BOX)
                    out.append((t, np.asarray(tile, np.uint8)))
    return out


def _norm(a: np.ndarray) -> np.ndarray:
    """Zero mean and unit length, after halving its size (a blur, which makes re-encoding and scaling count less)."""
    a = a.astype(np.float32)
    h, w = a.shape[-2] // 2 * 2, a.shape[-1] // 2 * 2
    a = a[..., :h, :w].reshape(*a.shape[:-2], h // 2, 2, w // 2, 2).mean(axis=(-3, -1))
    a = a - a.mean(axis=(-2, -1), keepdims=True)
    return a / (np.linalg.norm(a, axis=(-2, -1), keepdims=True) + 1e-6)


def compare(frames: dict, board: list[tuple[float, np.ndarray]], interval: float) -> dict:
    """How well a storyboard's tiles match a video's frames: each tile against the frames within its interval and a
    half (and at least 2 s) of its time, stretched or letterboxed, the best correlation. {"verdict": "match",
    "mismatch" or "unclear", "tiles": compared (not flat), "matched", "share", "median": the median best correlation}."""
    rate = frames["rate"]
    near = max(1.5 * interval, 2.0)
    both = [_norm(frames["stretched"]), _norm(frames["fitted"])]
    n = len(both[0])
    best = []
    for t, tile in board:
        if tile.std() < FLAT or not n:
            continue
        lo, hi = max(0, int((t - near) * rate)), min(n, int((t + near) * rate) + 2)
        if lo >= hi:
            best.append(-1.0)       # past the video's end: it's shorter than the storyboard says
            continue
        tn = _norm(tile)
        best.append(float(max((f[lo:hi] * tn).sum(axis=(-2, -1)).max() for f in both)))
    matched = sum(b >= MATCH for b in best)
    share = matched / len(best) if best else 0.0
    verdict = ("unclear" if len(best) < LEAST_TILES else "match" if share >= MATCHED else
               "mismatch" if share <= MISMATCHED else "unclear")
    return {"verdict": verdict, "tiles": len(best), "matched": matched, "share": round(share, 3),
            "median": round(float(np.median(best)), 3) if best else None}
