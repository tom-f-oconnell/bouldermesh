"""Each mesh's picture, for link previews and the Meshes tab's cards (design.md, "Pictures of meshes"): the site's own
viewer, opened at /m/<id>?thumb=..., drawn in a browser with no screen by thumbs.mjs, and saved as a JPEG of 1200 x 630,
the size link previews want, with a small copy for cards.

Beside each mesh: thumb/large.jpg and thumb/small.jpg, and thumb.json: {pose (the camera chosen by hand: {eye, target}
in the mesh's own coordinates, or None, for the viewer's own choice), key (what it was drawn from, as key() has it), mode
(how its texture was shown then), v (the version in its addresses), made, seconds, failed (why the last try for key
failed, if it did)}.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import select
import shutil
import subprocess
import threading
import time
from pathlib import Path

SCRIPT = Path(__file__).with_name("thumbs.mjs")
VERSION = 1                 # how pictures are drawn: one more draws them all again
SIZE, SMALL = (1200, 630), (480, 252)
DIR, LARGE, SMALL_FILE, RECORD = "thumb", "large.jpg", "small.jpg", "thumb.json"
TIMEOUT = 180               # seconds one picture may take, page and mesh loading included
IDLE = 300                  # seconds the browser stays open with nothing to draw


def core() -> Path:
    return Path(os.environ.get("PLAYWRIGHT_CORE") or "/opt/playwright-core")


def available() -> bool:
    """Whether pictures can be drawn here: node, and playwright-core with its browser (the Docker image's, or as
    PLAYWRIGHT_CORE and PLAYWRIGHT_BROWSERS_PATH say); THUMBS=0 turns them off."""
    return os.environ.get("THUMBS", "1") != "0" and bool(shutil.which("node")) and (core() / "package.json").is_file()


def load(mesh_dir: Path) -> dict:
    try:
        return json.loads((mesh_dir / RECORD).read_text())
    except (OSError, ValueError):
        return {}


def save(mesh_dir: Path, record: dict) -> None:
    tmp = mesh_dir / (RECORD + ".tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(mesh_dir / RECORD)


def key(version: str | None, mesh_dir: Path, pose: dict | None) -> str:
    """What a mesh's picture is drawn from: what the viewer is sent for it (version: mesh_version()), which way is up,
    where its climbs and its photos were taken from (orientation.json, views.json, cameras.json: the camera it's drawn
    from by itself), and the camera chosen by hand."""
    parts = [str(VERSION), version or "-", json.dumps(pose, sort_keys=True)]
    for name in ("orientation.json", "views.json", "cameras.json"):
        try:
            st = (mesh_dir / name).stat()
            parts.append(f"{st.st_size}.{st.st_mtime_ns}")
        except OSError:
            parts.append("-")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def check_pose(pose) -> dict | None:
    """A camera as the page sends it, {eye: [x, y, z], target: [x, y, z]}, made safe to keep; ValueError if it isn't one.
    None stays None (the viewer's own choice)."""
    if pose is None:
        return None
    try:
        out = {k: [float(x) for x in pose[k]] for k in ("eye", "target")}
    except (TypeError, KeyError, ValueError):
        raise ValueError("a camera is {eye: [x, y, z], target: [x, y, z]}") from None
    if any(len(v) != 3 or not all(math.isfinite(x) and abs(x) < 1e6 for x in v) for v in out.values()) or out["eye"] == out["target"]:
        raise ValueError("a camera is {eye: [x, y, z], target: [x, y, z]}, two different points")
    return out


def shrink(large: Path, small: Path) -> None:
    from PIL import Image
    with Image.open(large) as im:
        im.convert("RGB").resize(SMALL, Image.LANCZOS).save(small, "JPEG", quality=82, optimize=True)


class Renderer:
    """thumbs.mjs, kept running between pictures, and closed when nothing's been drawn for IDLE seconds."""

    def __init__(self, nice: int = 19):
        self.proc: subprocess.Popen | None = None
        self.nice, self.used = nice, 0.0
        self.lock = threading.Lock()
        threading.Thread(target=self._idle, daemon=True, name="thumbs-idle").start()

    def _start(self) -> subprocess.Popen:
        if self.proc and self.proc.poll() is None:
            return self.proc
        nice = ["nice", "-n", str(self.nice)] if shutil.which("nice") else []
        self.proc = subprocess.Popen([*nice, "node", str(SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, env={**os.environ, "PLAYWRIGHT_CORE": str(core())})
        return self.proc

    def draw(self, url: str, out: Path, size: tuple[int, int] = SIZE) -> float:
        """Draw url's view into out (a JPEG); how many seconds it took. RuntimeError, saying why, if it couldn't."""
        with self.lock:
            p = self._start()
            p.stdin.write(json.dumps({"url": url, "out": str(out), "width": size[0], "height": size[1], "timeout": TIMEOUT}) + "\n")
            p.stdin.flush()
            ready, _, _ = select.select([p.stdout], [], [], TIMEOUT + 30)
            line = p.stdout.readline() if ready else ""
            self.used = time.time()
            if not line:
                self.close(locked=True)
                raise RuntimeError("the browser drawing it stopped answering" if ready else "it took too long")
            got = json.loads(line)
            if not got.get("ok"):
                raise RuntimeError(got.get("error") or "it couldn't be drawn")
            return float(got.get("seconds") or 0)

    def close(self, locked: bool = False) -> None:
        if not locked:
            self.lock.acquire()
        try:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.stdin.close()
                    self.proc.wait(timeout=20)
                except (OSError, subprocess.TimeoutExpired):
                    self.proc.kill()
            self.proc = None
        finally:
            if not locked:
                self.lock.release()

    def _idle(self) -> None:
        while True:
            time.sleep(30)
            if self.proc and time.time() - self.used > IDLE and self.lock.acquire(blocking=False):
                try:
                    if time.time() - self.used > IDLE:
                        self.close(locked=True)
                finally:
                    self.lock.release()
