"""What a textured mesh's files say about its texture, for bench.py's texture experiments (run in the app's image,
which has numpy and PIL): texstats.py <folder with a .glb> writes <folder>/texstats.json and prints it.

- glb_mb: the GLB's size, with any texture files beside it that it names instead of holding.
- textures, mpx: how many texture images, and their pixels in millions.
- used_mpx: the pixels that aren't TextureMesh's empty colour (0x808080, as the app sets it): the texture that is
  actually on the mesh.
- sharpness: over the used pixels whose four neighbours are used too, the variance of each pixel's difference from
  its neighbours' mean (a Laplacian), on the grey image, 0-255. Blurrier or coarser texture gives a lower number. It
  compares variants of one mesh; a smaller texture can score as sharp per pixel while holding less, so read it with
  used_mpx.
- faces: the mesh's triangles, from the GLB's own index counts (a mesh simplified before texturing has fewer).
"""
import io
import json
import struct
import sys
from pathlib import Path

EMPTY = 128         # each channel of the empty colour


def glb_parts(path: Path) -> tuple[dict, bytes]:
    """A GLB's JSON and its binary chunk."""
    data = path.read_bytes()
    magic, _, _ = struct.unpack_from("<4sII", data, 0)
    assert magic == b"glTF", "not a GLB"
    doc, binary, at = {}, b"", 12
    while at < len(data):
        size, kind = struct.unpack_from("<I4s", data, at)
        chunk = data[at + 8:at + 8 + size]
        if kind == b"JSON":
            doc = json.loads(chunk)
        elif kind == b"BIN\0":
            binary = chunk
        at += 8 + size
    return doc, binary


def images(path: Path) -> tuple[list[bytes], int]:
    """A GLB's texture images as their files' bytes (held in it, or named by it and beside it), and the bytes of
    those beside it."""
    doc, binary = glb_parts(path)
    out, beside = [], 0
    for image in doc.get("images", []):
        if "bufferView" in image:
            view = doc["bufferViews"][image["bufferView"]]
            out.append(binary[view.get("byteOffset", 0):view.get("byteOffset", 0) + view["byteLength"]])
        elif "uri" in image and not image["uri"].startswith("data:") and (path.parent / image["uri"]).exists():
            out.append((path.parent / image["uri"]).read_bytes())
            beside += len(out[-1])
    return out, beside


def faces(path: Path) -> int:
    doc, _ = glb_parts(path)
    total = 0
    for mesh in doc.get("meshes", []):
        for prim in mesh.get("primitives", []):
            if prim.get("mode", 4) == 4 and "indices" in prim:
                total += doc["accessors"][prim["indices"]]["count"] // 3
    return total


def measure(image_bytes: bytes) -> tuple[int, int, float, int]:
    """(pixels, used pixels, the sum of squared Laplacian over the pixels it counts, how many it counts)."""
    import numpy as np
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    rgb = np.asarray(Image.open(io.BytesIO(image_bytes)).convert("RGB"), dtype=np.int16)
    used = (np.abs(rgb - EMPTY) > 3).any(2)
    grey = (rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114).astype(np.float32)
    lap = grey[1:-1, 1:-1] - (grey[:-2, 1:-1] + grey[2:, 1:-1] + grey[1:-1, :-2] + grey[1:-1, 2:]) / 4
    inner = used[1:-1, 1:-1] & used[:-2, 1:-1] & used[2:, 1:-1] & used[1:-1, :-2] & used[1:-1, 2:]
    v = lap[inner]
    return used.size, int(used.sum()), float((v.astype(np.float64) ** 2).sum()), int(v.size)


def stats(folder: Path) -> dict:
    glbs = sorted(folder.glob("*.glb"))
    if not glbs:
        return {"error": "no .glb"}
    glb = glbs[0]
    files, beside = images(glb)
    pixels = used = n = 0
    squares = 0.0
    for f in files:
        p, u, s, k = measure(f)
        pixels, used, squares, n = pixels + p, used + u, squares + s, n + k
    return {"glb": glb.name, "glb_mb": round((glb.stat().st_size + beside) / 2 ** 20, 2), "textures": len(files),
            "mpx": round(pixels / 1e6, 2), "used_mpx": round(used / 1e6, 2),
            "sharpness": round(squares / n, 2) if n else None, "faces": faces(glb)}


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    result = stats(folder)
    try:
        (folder / "texstats.json").write_text(json.dumps(result, indent=1))
    except OSError as e:        # a folder it may only read: the numbers are still printed
        print(f"not saved: {e}", file=sys.stderr)
    print(json.dumps(result))
