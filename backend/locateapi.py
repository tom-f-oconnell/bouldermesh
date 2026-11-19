"""Lining a mesh up with no taps, on the server: POST /api/capture/locate/<mesh> places a phone's frames against the
mesh's own photos (locate.py's pack: features of its placed photos tied to its model's points) and answers where each
frame's camera was, in the mesh's coordinates. The phone pairs that with where its tracking had the same camera
(app core Locate.fromLocated()) for the mesh-to-world fit.

    request   {frames: [{id, jpeg_b64, lens: {fx, fy, cx, cy, w, h}}]}: 1 to MAX_FRAMES frames, grey or colour JPEGs of
              about 1280 pixels, each with its lens for that picture
    answer    {located: [{id, c: [x, y, z], q: [w, x, y, z], inliers, rms_px, how}], ms}: those placed, each camera's
              centre and its turn from camera to mesh in image axes (x right, y down, z forward), i.e. the inverse of
              COLMAP's q, t; how: "phone" (as a phone's place() would) or "more" (the server's heavier try)

A frame the pack can't place is tried again with more of its features and more RANSAC tries (MORE_OPTS), while
there's time left (BUDGET_S): on 99 photos of 12 meshes left out of their packs, that placed 71 where the phone's
settings placed 68, with no more wrongly. Looser matching (a ratio of 0.85, 8 px) placed 72 but half as many again
wrongly, and a pack with more features a photo hardly differs, as its points are the model's own.

Packs are made when first asked for, one at a time in the background, as that takes minutes for a big pool: until
one is ready the answer is 503 with how long to wait.
"""
from __future__ import annotations

import base64
import binascii
import io
import struct
import tarfile
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

MAX_FRAMES = 4
MAX_JPEG = 2 * 2**20        # bytes, a frame
MAX_BODY = MAX_FRAMES * MAX_JPEG * 4 // 3 + 64 * 1024     # base64 is 4/3 the bytes
MAX_SIDE = 4096             # pixels, a frame's longer side
PER_MINUTE = 12             # calls one sender (an account or an address) may make a minute
AT_ONCE = 2                 # calls being placed at once, from anyone
BUDGET_S = 6.0              # past this, frames left over aren't tried the heavier way (the apps wait 8 s)
PHONE = "phone"         # the pack: what the apps download, and the server places against
PACK_FILES = {PHONE: "locate"}      # beside the mesh, a .bin and a .json
MORE_OPTS = {"iters": 20000, "max_features": 8000}     # place()'s, the heavier try


class Refused(Exception):
    """A request that can't be answered as asked: its HTTP code, why in words, and anything else to answer."""

    def __init__(self, code: int, why: str, **extra):
        super().__init__(why)
        self.code, self.why, self.extra = code, why, extra

    def answer(self) -> dict:
        return {"error": self.why, **self.extra}


def frames_of(body) -> list[dict]:
    """A request's frames, checked and decoded: [{id, grey (uint8 h x w), lens: {fx, fy, cx, cy, w, h}}], the lens
    scaled to the picture as decoded if it was given for another size. Raises Refused (400 or 413) if they aren't."""
    import cv2
    frames = body.get("frames") if isinstance(body, dict) else None
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise Refused(400, f"send 1 to {MAX_FRAMES} frames")
    out, ids = [], set()
    for f in frames:
        if not isinstance(f, dict) or not isinstance(f.get("id"), int) or f["id"] in ids:
            raise Refused(400, "each frame has an id of its own, a whole number")
        ids.add(f["id"])
        lens = f.get("lens")
        try:
            lens = {k: float(lens[k]) for k in ("fx", "fy", "cx", "cy", "w", "h")}
            assert all(np.isfinite(v) for v in lens.values()) and min(lens["fx"], lens["fy"], lens["w"], lens["h"]) > 0
        except (TypeError, KeyError, ValueError, AssertionError):
            raise Refused(400, "each frame's lens is {fx, fy, cx, cy, w, h}, in pixels") from None
        b64 = f.get("jpeg_b64")
        if not isinstance(b64, str) or len(b64) > MAX_JPEG * 4 // 3 + 4:
            raise Refused(413, f"each frame is a JPEG of at most {MAX_JPEG // 2**20} MB")
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise Refused(400, "a frame's jpeg_b64 isn't base64") from None
        if raw[:2] != b"\xff\xd8":
            raise Refused(400, "a frame isn't a JPEG")
        grey = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        if grey is None or min(grey.shape) < 64 or max(grey.shape) > MAX_SIDE:
            raise Refused(400, f"a frame couldn't be read, or isn't 64 to {MAX_SIDE} pixels a side")
        h, w = grey.shape
        sx, sy = w / lens["w"], h / lens["h"]
        if abs(sx - sy) > 0.02 * max(sx, sy):
            raise Refused(400, f"a frame's lens is for {lens['w']:.0f}x{lens['h']:.0f}, which isn't the shape of its {w}x{h} picture")
        lens = {"fx": lens["fx"] * sx, "fy": lens["fy"] * sy, "cx": lens["cx"] * sx, "cy": lens["cy"] * sy, "w": w, "h": h}
        out.append({"id": f["id"], "grey": grey, "lens": lens})
    return out


def pose_answer(pose: dict) -> dict:
    """locate.place()'s world-to-camera pose (COLMAP's: x = R X + t) as the apps take it: the camera's centre, and
    its turn from camera to mesh as a quaternion [w, x, y, z]."""
    R = np.asarray(pose["R"], float).reshape(3, 3)
    t = np.asarray(pose["t"], float).reshape(3)
    c = -R.T @ t
    return {"c": [round(float(v), 6) for v in c], "q": [round(float(v), 8) for v in quat_of(R.T)],
            "inliers": int(pose["inliers"]), "rms_px": round(float(pose["rms_px"]), 3)}


def quat_of(R: np.ndarray) -> np.ndarray:
    """A rotation matrix's quaternion [w, x, y, z], with w >= 0."""
    m = R
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        s = 2 * np.sqrt(tr + 1)
        q = [s / 4, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s]
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = 2 * np.sqrt(1 + m[0, 0] - m[1, 1] - m[2, 2])
        q = [(m[2, 1] - m[1, 2]) / s, s / 4, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s]
    elif m[1, 1] > m[2, 2]:
        s = 2 * np.sqrt(1 + m[1, 1] - m[0, 0] - m[2, 2])
        q = [(m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, s / 4, (m[1, 2] + m[2, 1]) / s]
    else:
        s = 2 * np.sqrt(1 + m[2, 2] - m[0, 0] - m[1, 1])
        q = [(m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, s / 4]
    q = np.array(q) / np.linalg.norm(q)
    return -q if q[0] < 0 else q


class Limits:
    """Who may be placing frames now: one call at a time a sender, PER_MINUTE a minute each, AT_ONCE from everyone."""

    def __init__(self, per_minute: int = PER_MINUTE, at_once: int = AT_ONCE, clock: Callable[[], float] = time.monotonic):
        self.per_minute, self.clock = per_minute, clock
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(at_once)
        self.busy: set[str] = set()
        self.calls: dict[str, list[float]] = {}

    def begin(self, who: str) -> None:
        """Take a turn for `who`, or raise Refused (429, or 503 when the server's busy with others). end() it after."""
        now = self.clock()
        with self.lock:
            if who in self.busy:
                raise Refused(429, "one at a time: the last frames are still being placed", retry_s=1)
            calls = self.calls[who] = [t for t in self.calls.get(who, []) if now - t < 60]
            if len(self.calls) > 5000:
                self.calls = {who: calls}
            if len(calls) >= self.per_minute:
                raise Refused(429, f"at most {self.per_minute} a minute", retry_s=round(60 - (now - calls[0]) + 0.5))
            if not self.slots.acquire(blocking=False):
                raise Refused(503, "the server is placing others' frames: try again in a moment", retry_s=2)
            calls.append(now)
            self.busy.add(who)

    def end(self, who: str) -> None:
        with self.lock:
            self.busy.discard(who)
            self.slots.release()


class Packs:
    """Each mesh's pack to place frames against (locate.py), made when first asked for, one at a time in the background,
    and again once its reconstruction is newer; the last few read kept in memory. The one queue: the apps' pack
    download asks here too.

    Args:
        resolve: resolve(mesh id, photos): a mesh's folder, and (with photos) the folders its photos are in, in the
            order to look (its project's, then those it pooled from), having asked for any in the backup back. Raises
            Refused if it can't have a pack (yet).
        build: locate.build_pack(mesh_dir, photo_dirs, out, **opts), writing <out>.bin and <out>.json beside the mesh.
        load: locate.load_pack(path of the .bin).
        keep: packs kept in memory.
    """

    def __init__(self, resolve: Callable[[str, bool], tuple[Path, list[Path]]], build: Callable, load: Callable[[Path], object],
                 keep: int = 6, log: Callable[[str], None] = lambda s: print(s, flush=True)):
        self.resolve, self.build, self.load, self.keep, self.log = resolve, build, load, keep, log
        self.lock = threading.Lock()
        self.cache: dict[tuple[str, str], tuple[int, object]] = {}     # (mesh, kind) -> (its file's mtime, pack)
        self.making: dict[tuple[str, str], float] = {}                 # queued or being made, since
        self.failed: dict[tuple[str, str], tuple[int, str]] = {}       # couldn't be: for which reconstruction, why
        self.queue: list[tuple[str, str]] = []
        self.took: list[float] = []                                    # seconds the last few took to make
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None

    @staticmethod
    def stamp(mesh_dir: Path) -> int:
        from reconstruction import FILE
        try:
            return (mesh_dir / FILE).stat().st_mtime_ns
        except FileNotFoundError:
            raise Refused(409, "this mesh kept no reconstruction to place frames against (it was made before they were kept)") from None

    def retry_s(self, mid: str, kind: str) -> int:
        """Roughly how long until a pack being made is ready: those ahead of it and itself, at the recent pace."""
        with self.lock:
            ahead = next((i for i, k in enumerate(self.queue) if k == (mid, kind)), len(self.queue)) + 1
            each = sorted(self.took)[len(self.took) // 2] if self.took else 60.0
        return max(5, int(min(ahead * each, 3600)))

    def path(self, mid: str, kind: str = PHONE) -> Path | None:
        """A mesh's pack file of this kind (its .bin), or None while it's being made (queued, if it wasn't yet).
        Raises Refused (409, or 503 while its photos come back) if it can't have one."""
        mesh_dir, _ = self.resolve(mid, False)
        stamp, key = self.stamp(mesh_dir), (mid, kind)
        f = mesh_dir / (PACK_FILES[kind] + ".bin")
        with self.lock:
            if f.is_file() and f.stat().st_mtime_ns >= stamp and key not in self.making:
                return f
            bad = self.failed.get(key)
            if bad and bad[0] == stamp:
                raise Refused(409, f"no pack could be made for this mesh: {bad[1]}")
            if key in self.making:
                return None
        self.resolve(mid, True)     # its photos: asked back from the backup if they're there (Refused till they're here)
        with self.lock:
            if key not in self.making:
                self.making[key] = time.time()
                self.queue.append(key)
                self._start()
        return None

    def get(self, mid: str, kind: str = PHONE):
        """A mesh's pack, read, or None while it's being made (as path())."""
        f = self.path(mid, kind)
        if f is None:
            return None
        key, mtime = (mid, kind), f.stat().st_mtime_ns
        with self.lock:
            got = self.cache.get(key)
            if got and got[0] == mtime:
                self.cache[key] = self.cache.pop(key)     # most recently used last
                return got[1]
        try:
            pack = self.load(f)
        except Exception as e:      # an older pack's format, or a broken one: made again
            self.log(f"locate: {mid}'s {kind} pack can't be read ({type(e).__name__}: {e}): making it again")
            f.unlink(missing_ok=True)
            return self.path(mid, kind)     # None: queued
        with self.lock:
            self.cache[key] = (mtime, pack)
            while len(self.cache) > self.keep:
                self.cache.pop(next(iter(self.cache)))
        return pack

    def _start(self) -> None:
        self.wake.set()
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="locate-packs", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        while True:
            with self.lock:
                key = self.queue.pop(0) if self.queue else None
                if key is None:
                    self.wake.clear()
            if key is None:
                if not self.wake.wait(600):
                    with self.lock:
                        if not self.queue:
                            self.thread = None
                            return
                continue
            mid, kind = key
            t0, stamp = time.time(), None
            try:
                mesh_dir, dirs = self.resolve(mid, True)
                stamp = self.stamp(mesh_dir)
                self.build(mesh_dir, dirs, out=PACK_FILES[kind])
                if not (mesh_dir / (PACK_FILES[kind] + ".bin")).is_file():
                    raise RuntimeError("it wrote no pack")
                with self.lock:
                    self.took = (self.took + [time.time() - t0])[-9:]
                self.log(f"locate: {mid}'s {kind} pack made in {time.time() - t0:.0f} s")
            except Exception as e:
                why = e.why if isinstance(e, Refused) else f"{type(e).__name__}: {e}"
                if not (isinstance(e, Refused) and e.code == 503) and stamp is not None:     # 503: photos coming back
                    with self.lock:
                        self.failed[key] = (stamp, why)
                self.log(f"locate: {mid}'s {kind} pack not made: {why}")
            finally:
                with self.lock:
                    self.making.pop(key, None)


def locate(frames: list[dict], pack, place: Callable, budget_s: float | None = None) -> dict:
    """Place each frame against a pack, and those it can't again the heavier way (MORE_OPTS) while there's time. The
    answer (see above), but for its ms."""
    t0, budget_s = time.monotonic(), BUDGET_S if budget_s is None else budget_s
    located, missed = [], []
    for f in frames:
        pose = place(pack, f["grey"], f["lens"])
        if pose is None:
            missed.append(f)
        else:
            located.append({"id": f["id"], **pose_answer(pose), "how": PHONE})
    for f in missed:
        if time.monotonic() - t0 >= budget_s:
            break
        pose = place(pack, f["grey"], f["lens"], **MORE_OPTS)
        if pose is not None:
            located.append({"id": f["id"], **pose_answer(pose), "how": "more"})
    return {"located": located}


def answer(mid: str, body, who: str, packs: Packs, limits: Limits, place: Callable) -> tuple[dict, int]:
    """POST /api/capture/locate/<mid>: the answer, and its HTTP code. `who`: the sender, for the limits (an account's
    id, or its address)."""
    t0 = time.monotonic()
    try:
        frames = frames_of(body)
        pack = packs.get(mid, PHONE)
        if pack is None:
            raise Refused(503, "this mesh's pack is being made: try again soon", retry_s=packs.retry_s(mid, PHONE))
        limits.begin(who)
        try:
            got = locate(frames, pack, place)
        finally:
            limits.end(who)
    except Refused as e:
        return e.answer(), e.code
    return {**got, "ms": round((time.monotonic() - t0) * 1000)}, 200


def model_photos(mesh_dir: Path) -> list[str] | None:
    """The names of the photos placed in a mesh's kept reconstruction, or None if it kept none."""
    from reconstruction import FILE
    path = mesh_dir / FILE
    if not path.is_file():
        return None
    with tarfile.open(path, "r:gz") as tar:
        f = tar.extractfile("model/images.bin")
        data = io.BytesIO(f.read()) if f else None
    if data is None:
        return None
    names = []
    for _ in range(struct.unpack("<Q", data.read(8))[0]):
        data.read(64)
        name = b""
        while (c := data.read(1)) not in (b"\0", b""):
            name += c
        n = struct.unpack("<Q", data.read(8))[0]
        data.seek(24 * n, 1)
        names.append(name.decode())
    return names


def readiness(mesh_dir: Path, photos: Path | None, away: set[str] = frozenset()) -> dict:
    """What a mesh has of what placing frames against it needs: its kept reconstruction, and the photos placed in it
    in its project's photos (`away`: those only in the backup for now, which come back when asked for).

    Returns:
        {ok, why, placed, here, away, missing (a few names)}, ok when it has a reconstruction and every photo placed
        in it is here or can come back.
    """
    names = model_photos(mesh_dir)
    if names is None:
        return {"ok": False, "why": "no reconstruction kept", "placed": 0, "here": 0, "away": 0, "missing": []}
    if photos is None:
        return {"ok": False, "why": "not made from a project's photos", "placed": len(names), "here": 0, "away": 0, "missing": []}
    here = [n for n in names if (photos / n).is_file()]
    back = [n for n in names if n not in set(here) and n in away]
    missing = [n for n in names if n not in set(here) and n not in away]
    why = "" if not missing else f"{len(missing)} of its {len(names)} placed photos are gone"
    return {"ok": not missing, "why": why, "placed": len(names), "here": len(here), "away": len(back), "missing": missing[:5]}


PACKS: Packs | None = None      # the server's (server.py sets it)


def pack(mid: str, which: str = PHONE) -> Path | None:
    """The server's pack file (.bin) of a mesh, for the apps' download: None while it's being made (queued if it
    wasn't; PACKS.retry_s() says how long). Raises Refused if the mesh can't have one."""
    return PACKS.path(mid, which)
