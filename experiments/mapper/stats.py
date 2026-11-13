"""Small readers for the benchmark's COLMAP models and meshes, standard library and numpy only.

    stats.py largest <sparse dir>             the name of the model placing most photos
    stats.py unrefined <database> <model>     names of placed photos whose camera is still the database's first guess
    stats.py table <bench dir>                every run's results, as tab-separated rows
"""
import json
import re
import sqlite3
import struct
import sys
from pathlib import Path

NUM_PARAMS = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8, 5: 8, 6: 12, 7: 5, 8: 4, 9: 5, 10: 12}


def cameras(model: Path) -> dict[int, list[float]]:
    """Each camera's parameters."""
    if (model / "cameras.txt").exists():
        out = {}
        for line in open(model / "cameras.txt"):
            if line.strip() and not line.startswith("#"):
                f = line.split()
                out[int(f[0])] = [float(x) for x in f[4:]]
        return out
    out = {}
    with open(model / "cameras.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            cid, mid, _w, _h = struct.unpack("<iiQQ", f.read(24))
            n = NUM_PARAMS[mid]
            out[cid] = list(struct.unpack(f"<{n}d", f.read(8 * n)))
    return out


def images(model: Path) -> dict[str, int]:
    """Each placed photo's camera."""
    out = {}
    if (model / "images.txt").exists():
        lines = [l for l in open(model / "images.txt") if not l.startswith("#")]
        for line in lines[0::2]:
            f = line.split()
            if len(f) >= 10:
                out[f[9]] = int(f[8])
        return out
    with open(model / "images.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            f.read(4 + 56)
            cid = struct.unpack("<i", f.read(4))[0]
            name = b""
            while (c := f.read(1)) != b"\0":
                name += c
            n = struct.unpack("<Q", f.read(8))[0]
            f.read(24 * n)
            out[name.decode()] = cid
    return out


def poses(model: Path) -> dict[str, tuple]:
    """Each placed photo's world-to-camera rotation (3x3) and translation, from a binary or text model."""
    import numpy as np

    def rotation(q):
        w, x, y, z = q
        return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                         [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                         [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])
    out = {}
    if (model / "images.bin").exists():
        with open(model / "images.bin", "rb") as f:
            for _ in range(struct.unpack("<Q", f.read(8))[0]):
                v = struct.unpack("<I7dI", f.read(64))
                name = b""
                while (c := f.read(1)) != b"\0":
                    name += c
                f.read(24 * struct.unpack("<Q", f.read(8))[0])
                out[name.decode()] = (rotation(v[1:5]), np.array(v[5:8]))
        return out
    lines = [l for l in open(model / "images.txt") if not l.startswith("#")]
    for line in lines[0::2]:
        f = line.split()
        if len(f) >= 10:
            out[f[9]] = (rotation([float(x) for x in f[1:5]]), np.array([float(x) for x in f[5:8]]))
    return out


def points(model: Path) -> tuple[int, float]:
    """(3D points, mean photos seeing each) of a binary model."""
    n = total = 0
    with open(model / "points3D.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            f.read(8 + 24 + 3 + 8)
            k = struct.unpack("<Q", f.read(8))[0]
            f.read(8 * k)
            n, total = n + 1, total + k
    return n, (total / n if n else 0.0)


def agreement(a: dict, b: dict, fit: set | None = None, report: set | None = None) -> dict | None:
    """How far model a's cameras are from model b's once a is laid over b. The rotation between the two models is
    the mean of the cameras' own (which way each faces), then scale and shift come from where they stand: boulder
    photos are often taken from along a line, which leaves a fit to positions alone free to spin about it. Fitted to
    the cameras named in fit, or all they share, and again without the worst fifth.

    Returns:
        Over the cameras in report (or all shared): how many, the median and 90th percentile of the distance between
        the two positions as a share of the scene's size (RMS distance of b's shared cameras from their middle), and
        of the angle between the two orientations in degrees. None with fewer than 3 cameras to fit.
    """
    import numpy as np
    shared = sorted(set(a) & set(b))
    use = [n for n in shared if fit is None or n in fit]
    if len(use) < 3:
        return None
    centre = lambda m, n: -m[n][0].T @ m[n][1]      # noqa: E731
    angle = lambda A: float(np.degrees(np.arccos(np.clip((np.trace(A) - 1) / 2, -1, 1))))      # noqa: E731

    def similarity(names):
        U, _, Vt = np.linalg.svd(sum(b[n][0].T @ a[n][0] for n in names))
        Rs = U @ np.diag([1, 1, np.sign(np.linalg.det(U @ Vt))]) @ Vt
        X, Y = np.array([Rs @ centre(a, n) for n in names]), np.array([centre(b, n) for n in names])
        mx, my = X.mean(0), Y.mean(0)
        s = ((Y - my) * (X - mx)).sum() / max(((X - mx) ** 2).sum(), 1e-12)
        return s, Rs, my - s * mx

    def errors(names, s, Rs, t, size):
        return ([float(np.linalg.norm(s * Rs @ centre(a, n) + t - centre(b, n))) / size for n in names],
                [angle(a[n][0] @ Rs.T @ b[n][0].T) for n in names])
    Y = np.array([centre(b, n) for n in shared])
    size = float(np.sqrt(((Y - Y.mean(0)) ** 2).sum(1).mean())) or 1.0
    s, Rs, t = similarity(use)
    pos, ang = errors(use, s, Rs, t, size)
    worst = dict(zip(use, (p + d / 30 for p, d in zip(pos, ang))))
    s, Rs, t = similarity(sorted(use, key=worst.get)[:max(3, int(len(use) * 0.8))])
    names = [n for n in shared if report is None or n in report]
    if not names:
        return {"cameras": 0}
    pos, ang = errors(names, s, Rs, t, size)
    q = lambda v, p: round(float(np.percentile(v, p)), 4)       # noqa: E731
    return {"cameras": len(names), "pos_median": q(pos, 50), "pos_p90": q(pos, 90), "deg_median": q(ang, 50),
            "deg_p90": q(ang, 90)}


def largest(sparse: Path) -> tuple[str, int, int]:
    """(name, photos) of the model placing most photos, and how many models there are."""
    best, n, models = "", 0, 0
    for m in sorted(sparse.iterdir()) if sparse.exists() else []:
        if (m / "images.bin").exists() or (m / "images.txt").exists():
            models += 1
            k = len(images(m))
            if k > n:
                best, n = m.name, k
    return best, n, models


def unrefined(db: Path, model: Path) -> list[str]:
    """Placed photos whose focal length is exactly the database's first guess: the mapper never refined it."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    first = {cid: struct.unpack(f"<{len(p) // 8}d", p) for cid, p in con.execute("select camera_id, params from cameras")}
    cams = cameras(model)
    return [name for name, cid in images(model).items() if cid in first and abs(cams[cid][0] - first[cid][0]) < 1e-9]


def mesh_stats(ply: Path) -> dict | None:
    """Faces, and the share of the surface in the largest connected piece, of a binary PLY from OpenMVS."""
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
    import meshclean
    if not ply.exists():
        return None
    data = ply.read_bytes()
    end = data.index(b"end_header\n") + len(b"end_header\n")
    header = data[:end].decode()
    nv = int(re.search(r"element vertex (\d+)", header).group(1))
    nf = int(re.search(r"element face (\d+)", header).group(1))
    vprops = re.search(r"element vertex \d+\n(.*?)element face", header, re.S).group(1).count("property")
    assert vprops == 3 and "list uchar uint" in header or "list uchar int" in header, header
    P = np.frombuffer(data, "<f4", nv * 3, end).reshape(nv, 3)
    F = np.frombuffer(data, np.dtype([("n", "u1"), ("i", "<i4", 3)]), nf, end + nv * 12)["i"].astype(np.int64)
    roots = meshclean._components(F, nv)[F[:, 0]]
    _, labels = np.unique(roots, return_inverse=True)
    Q = P.astype(np.float64)
    area = np.linalg.norm(np.cross(Q[F[:, 1]] - Q[F[:, 0]], Q[F[:, 2]] - Q[F[:, 0]]), axis=1) / 2
    areas = np.bincount(labels.ravel(), weights=area)
    return {"faces": nf, "one_piece": float(areas.max() / areas.sum()), "pieces": len(areas)}


def table(bench: Path) -> None:
    print("boulder\tphotos\tmethod\tseed\tmapper_s\trc\tmodels\tplaced\terror\tafter_rest\tdropped\tmesh_s\tfaces\tone_piece")
    for proj in sorted((bench / "runs").iterdir()):
        n = len(list((bench / "projects" / proj.name / "photos").iterdir()))
        for r in sorted(p for p in proj.iterdir() if p.is_dir()):
            if not (r / "time").exists():
                continue
            secs, rc = (r / "time").read_text().split()
            best, placed, models = largest(r / "sparse")
            log = (r / "map.log").read_text(errors="replace")
            m = r / "m"
            err = rest = dropped = mesh_s = faces = piece = ""
            a = (m / "analyzer.txt").read_text() if (m / "analyzer.txt").exists() else ""
            if x := re.search(r"Registered images: (\d+)", a):
                rest = x.group(1)
            am = (m / "analyzer_map.txt").read_text() if (m / "analyzer_map.txt").exists() else ""
            if x := re.search(r"Mean reprojection error: ([\d.]+)", am):
                err = x.group(1)
            dropped = sum(len((m / f).read_text().split("\n")) - 1 for f in ("drop.txt", "drop2.txt") if (m / f).exists())
            if (m / "time").exists():
                mesh_s = (m / "time").read_text().strip()
            st = mesh_stats(m / "scene_dense_mesh.ply")
            if st:
                faces, piece = st["faces"], f"{st['one_piece']:.2f}"
            method, seed = r.name.rsplit("-s", 1)
            print("\t".join(map(str, [proj.name, n, method, seed, secs, rc, models, placed, err, rest, dropped, mesh_s,
                                      faces, piece])))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "largest":
        print(largest(Path(sys.argv[2]))[0])
    elif cmd == "unrefined":
        sys.stdout.write("".join(f"{n}\n" for n in unrefined(Path(sys.argv[2]), Path(sys.argv[3]))))
    elif cmd == "table":
        table(Path(sys.argv[2]))
