#!/usr/bin/env python3
"""Checking volunteers' videos by their storyboards: storyboards.py's comparison, on storyboards made here from the
same video (a match), another (a mismatch) and the same one shifted in time; and the broker's way through, from a
video sent with its storyboard to a trusted volunteer's fetching it, its addresses refused and asked for afresh, and
the verdict told. Offline; needs ffmpeg.

    python3 tests/test_storyboards.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import storyboards  # noqa: E402
import volunteers  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="bouldermesh-storyboards-"))
VID = "abcdefghijk"


def make_video(path: Path, source: str, seconds: int = 60) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{source}=size=640x360:rate=25", "-t", str(seconds),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def board_images(video: Path, interval: float, cols: int = 5, rows: int = 5, w: int = 160, h: int = 90) -> list[bytes]:
    """A storyboard as YouTube's look: tiles every interval, rows x cols to an image, as JPEG."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps=1/{interval},scale={w}:{h}", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)
    out = []
    for start in range(0, len(frames), rows * cols):
        sheet = np.zeros((rows * h, cols * w, 3), np.uint8)
        for i, fr in enumerate(frames[start:start + rows * cols]):
            sheet[i // cols * h:(i // cols + 1) * h, i % cols * w:(i % cols + 1) * w] = fr
        buf = io.BytesIO()
        Image.fromarray(sheet).save(buf, "JPEG", quality=70)
        out.append(buf.getvalue())
    return out


def spec_for(images: list[bytes], interval: float, seconds: float, vid: str = VID) -> dict:
    per = 25 * interval
    frags = [{"url": f"https://i.ytimg.com/sb/{vid}/storyboard3_L2/M{i}.jpg?sigh=x", "duration": min(per, seconds - i * per)}
             for i in range(len(images))]
    return {"format_id": "sb1", "width": 160, "height": 90, "rows": 5, "columns": 5, "fps": 1 / interval, "fragments": frags}


def comparing() -> None:
    a = make_video(TMP / "a.mp4", "testsrc2")
    b = make_video(TMP / "b.mp4", "mandelbrot")
    c = make_video(TMP / "c.mp4", "life")
    for bad in ("http://i.ytimg.com/sb/abcdefghijk/x.jpg", "https://evil.example/sb/abcdefghijk/x.jpg",
                "https://i.ytimg.com/sb/zzzzzzzzzzz/x.jpg", "https://i.ytimg.com/vi/abcdefghijk/x.jpg"):
        assert not storyboards.valid_url(bad, VID), bad
    imgs = board_images(a, 2.0)
    spec = storyboards.pick([spec_for(imgs, 2.0, 60), {"format_id": "sb0", "width": 48, "height": 27, "rows": 10, "columns": 10,
                                                       "fragments": [{"url": "https://i.ytimg.com/sb/abcdefghijk/s.jpg", "duration": 60}]},
                             {"format_id": "sb2", "width": 320, "height": 180, "rows": 3, "columns": 3, "fps": 0.5,
                              "fragments": [{"url": f"https://evil.example/sb/{VID}/{i}", "duration": 18} for i in range(4)]}], VID)
    assert spec and spec["width"] == 160 and abs(spec["interval"] - 2) < 1e-9, spec
    frames = storyboards.video_frames(a, spec)
    same = storyboards.compare(frames, storyboards.tiles(imgs, spec), spec["interval"])
    print("same video:", same)
    assert same["verdict"] == "match", same
    for other, name in ((b, "mandelbrot"), (c, "life")):
        theirs = storyboards.compare(storyboards.video_frames(other, spec), storyboards.tiles(imgs, spec), spec["interval"])
        print(f"another video ({name}):", theirs)
        assert theirs["verdict"] == "mismatch", theirs
    # a's storyboard read as if it were 20 s later: the frames near each tile's time are others
    shifted = [(t + 20, tile) for t, tile in storyboards.tiles(imgs, spec)]
    late = storyboards.compare(frames, shifted, spec["interval"])
    print("shifted 20 s:", late)
    assert late["share"] < same["share"]
    # letterboxed: a 4:3 video in 16:9 tiles
    d = TMP / "d.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(a), "-vf", "scale=480:360", "-c:v", "libx264", str(d)], check=True)
    boxed = []
    for data in board_images(d, 2.0, w=120, h=90):
        im = Image.open(io.BytesIO(data))
        sheet = Image.new("RGB", (800, 450))
        for i in range(25):
            sheet.paste(im.crop((i % 5 * 120, i // 5 * 90, i % 5 * 120 + 120, i // 5 * 90 + 90)), (i % 5 * 160 + 20, i // 5 * 90))
        buf = io.BytesIO()
        sheet.save(buf, "JPEG")
        boxed.append(buf.getvalue())
    fitted = storyboards.compare(storyboards.video_frames(d, spec), storyboards.tiles(boxed, spec), spec["interval"])
    print("letterboxed:", fitted)
    assert fitted["verdict"] == "match", fitted


def through_the_broker() -> None:
    told = []
    rules = {**volunteers.RULES, "check_new": 1.0, "check_trusted": 0.0}
    b = volunteers.Broker(TMP / "v.sqlite", TMP / "files", lambda t, p, i: None, lambda p, r: None,
                          rules=lambda: rules, checked=lambda c, sender: told.append((c, sender)))
    alice, bob, carol = (b.add(n) for n in ("alice", "bob", "carol"))
    b.set_trusted(bob["id"], True)
    alice, bob, carol = (b.get(x["id"]) for x in (alice, bob, carol))
    a = TMP / "a.mp4"
    imgs = board_images(a, 2.0)
    sent = spec_for(imgs, 2.0, 60)
    b.probe = lambda path: 60.0

    def send_video(who, vid=VID):
        assert b.want("video", vid, "harvest", "p1")
        t = b.work(who, "198.51.100.1", kinds=["video"])["task"]
        data = a.read_bytes()
        b.part(who, t["id"], 0, len(data), "mp4", data)
        assert b.finish(who, t["id"], {"status": "ok", "info": {"duration": 60}, "storyboards": [spec_for(imgs, 2.0, 60, vid)]})["ok"]

    def wait_queued(n):
        for _ in range(300):
            if len(b.q("SELECT 1 FROM checks WHERE state='queued'")) >= n:
                return
            time.sleep(0.1)
        raise AssertionError(b.q("SELECT state, error FROM checks"))

    def bundle(images) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i, data in enumerate(images):
                z.writestr(f"{i}.img", data)
        return buf.getvalue()

    send_video(alice)
    wait_queued(1)
    # alice (not trusted) and carol (not trusted) aren't given it; bob is, with the addresses alice sent
    assert "task" not in b.work(alice, "198.51.100.1", kinds=["storyboard"]) or \
        b.work(alice, "198.51.100.1", kinds=["storyboard"])["task"]["kind"] != "storyboard"
    assert "task" not in b.work(carol, "198.51.100.3", kinds=["storyboard"])
    t = b.work(bob, "198.51.100.2", kinds=["video", "storyboard"])["task"]
    assert t["kind"] == "storyboard" and t["urls"] == [f["url"] for f in sent["fragments"]], t
    data = bundle(imgs)
    b.part(bob, t["id"], 0, len(data), "zip", data)
    assert b.finish(bob, t["id"], {"status": "ok"})["ok"]
    (c, sender), = told
    assert c["verdict"] == "match" and c["checker"] == bob["id"] and sender["id"] == alice["id"], c
    # the next one's addresses don't work from bob's: asked again, afresh, and the storyboard he finds is used
    send_video(alice, "bcdefghijkl")
    wait_queued(1)
    t = b.work(bob, "198.51.100.2", kinds=["storyboard"])["task"]
    assert "urls" in t
    assert b.finish(bob, t["id"], {"status": "failed", "denied": True, "error": "i.ytimg.com said HTTP 403 for image 1 of 3"})["ok"]
    t2 = b.work(bob, "198.51.100.2", kinds=["storyboard"])["task"]
    assert t2["id"] == t["id"] and t2.get("fresh"), t2
    other = board_images(TMP / "b.mp4", 2.0)      # what bob fetched is another video's: a mismatch
    data = bundle(other)
    b.part(bob, t["id"], 0, len(data), "zip", data)
    b.finish(bob, t["id"], {"status": "ok", "storyboard": spec_for(other, 2.0, 60, "bcdefghijkl")})
    c = told[-1][0]
    assert c["verdict"] == "mismatch" and c["mode"] == "fresh" and "403" in c["given_error"], c
    s = b.check_summary()
    assert s["per"][alice["id"]] == {"match": 1, "mismatch": 1} and s["given"]["worked"] == 1, s
    # a trusted volunteer's own videos aren't checked at check_trusted 0; one without a storyboard is noted
    rules["check_trusted"] = 0.0
    assert b.want("video", "cdefghijklm", "harvest", "p1")
    t = b.work(carol, "198.51.100.3", kinds=["video"])["task"]
    data = a.read_bytes()
    b.part(carol, t["id"], 0, len(data), "mp4", data)
    b.finish(carol, t["id"], {"status": "ok", "info": {"duration": 60}})
    assert b.q("SELECT state, error FROM checks WHERE video_id='cdefghijklm'")[0]["state"] == "skipped"
    print("broker: ok")


if __name__ == "__main__":
    comparing()
    through_the_broker()
    print("all ok")
