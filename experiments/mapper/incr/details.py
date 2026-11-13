"""Detail meshes of a beta set's holds, for printed replicas: each hold's region meshed from its close-ups at the
photos' full resolution, its accuracy measured, tied to the rock's mesh and checked, and a solid to print.

A hold is {id, p, r_m} (betasets.json's, or a capture record's detail.holds): p in the rock mesh's frame (the state's
model's), r_m the region's radius in metres (default 0.075, 0.03 to 0.15). For each:

  photos     the state's registered photos that see p in frame, from near enough and square enough to it
  dense      OpenMVS on those photos alone at resolution level 0 (full size), the cloud kept to 1.5 r about p
  mesh       ReconstructMesh, no decimation, cut to the sphere of radius r about p
  accuracy   split_half_mm: the median distance between two meshes made from disjoint halves of the photos;
             reprojection: the sparse model's error for those photos
  tie        point-to-plane ICP of the detail surface against the rock mesh: the rigid correction it would want and
             the RMS left (tie_rms_mm); a region whose correction or RMS is too large is "untied" and not shipped
  scale      metres a unit from the capture's fit (the phone's tracking, about 2%), and from a bank card where a
             close-up has one (card.py), whichever is surer
  solid      the cut mesh closed into a watertight solid with a flat base, in millimetres, for a printer: STL and 3MF

    details.py solid <mesh.ply> <p x,y,z> <r units> <mm a unit> <out stem>    the printable solid alone
    (core.py's detail step does the rest, in the tools: it needs pycolmap and OpenMVS)

Writes details/<hold id>.ply (metres, in the mesh's frame), .stl and .3mf (millimetres), and details.json
{v, holds: [{id, p, r_m, faces, gsd_mm, views, tie_rms_mm, split_half_mm, scale, scale_err, scale_from, status, why}]}.
"""
from __future__ import annotations

import json
import math
import sys
import zipfile
from pathlib import Path

import numpy as np

VERSION = 1
try:        # the server's own limits on a hold's reach (backend/beta.py), where it's there to read
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))
    import beta as _beta
    R_DEFAULT, R_MIN, R_MAX = _beta.HOLD_R_M, _beta.HOLD_R_LEAST, _beta.HOLD_R_MOST
except Exception:       # in the tools' container, which has no backend: the same numbers
    R_DEFAULT, R_MIN, R_MAX = 0.075, 0.03, 0.15
MAX_TIE_SHIFT_MM, MAX_TIE_TURN_DEG, MAX_TIE_RMS_MM = 5.0, 2.0, 6.0
TRACKING_SCALE_ERR = 0.02           # the phone's tracking's scale floor (the coordinator's 2%)


def radius(hold: dict) -> float:
    return float(min(max(hold.get("r_m") or R_DEFAULT, R_MIN), R_MAX))


# ---------------------------------------------------------------- near neighbours, without scipy

class Grid:
    """Points hashed into cubes of side `cell`, for the nearest of them to a query (within a cube's reach)."""

    def __init__(self, P: np.ndarray, cell: float):
        self.P, self.cell = P, cell
        keys = np.floor(P / cell).astype(np.int64)
        self.cells: dict = {}
        for i, k in enumerate(map(tuple, keys)):
            self.cells.setdefault(k, []).append(i)

    def nearest(self, Q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Each query's nearest point (index, -1 if none within the next cube) and distance."""
        idx, dist = np.full(len(Q), -1), np.full(len(Q), np.inf)
        for j, q in enumerate(Q):
            k = np.floor(q / self.cell).astype(np.int64)
            cand = [i for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
                    for i in self.cells.get((k[0] + dx, k[1] + dy, k[2] + dz), ())]
            if cand:
                d = np.linalg.norm(self.P[cand] - q, axis=1)
                m = int(np.argmin(d))
                idx[j], dist[j] = cand[m], d[m]
        return idx, dist


def vertex_normals(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    n = np.zeros_like(V)
    fn = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    for k in range(3):
        np.add.at(n, F[:, k], fn)
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)


# ---------------------------------------------------------------- cutting, comparing, tying

def cut(V: np.ndarray, F: np.ndarray, p, r: float) -> tuple[np.ndarray, np.ndarray]:
    """The faces whose three corners lie within r of p, with their vertices renumbered."""
    inside = np.linalg.norm(V - np.asarray(p, float), axis=1) <= r
    keep = inside[F].all(axis=1)
    used, back = np.unique(F[keep], return_inverse=True)
    return V[used], back.reshape(-1, 3)


def surface_distance(A: np.ndarray, V: np.ndarray, F: np.ndarray, reach: float) -> np.ndarray:
    """Each point of A's distance from mesh (V, F), along the nearest vertex's normal (point to plane), NaN where no
    vertex is within reach."""
    g = Grid(V, reach)
    i, _ = g.nearest(A)
    N = vertex_normals(V, F)
    out = np.full(len(A), np.nan)
    ok = i >= 0
    out[ok] = np.einsum("ij,ij->i", A[ok] - V[i[ok]], N[i[ok]])
    return out


def split_half(Va, Fa, Vb, Fb, reach: float) -> float:
    """The median distance between two meshes of a region (each from half its photos), both ways."""
    d = np.concatenate([surface_distance(Va, Vb, Fb, reach), surface_distance(Vb, Va, Fa, reach)])
    d = np.abs(d[np.isfinite(d)])
    return float(np.median(d)) if len(d) else float("nan")


def icp_point_to_plane(A: np.ndarray, V: np.ndarray, F: np.ndarray, reach: float, rounds: int = 10):
    """The small rigid motion laying points A onto mesh (V, F), point to plane, linearised: (R, t, rms before, rms
    after, points used). Used to check a detail region against the rock mesh, not to move it."""
    N = vertex_normals(V, F)
    g = Grid(V, reach)
    R, t = np.eye(3), np.zeros(3)
    rms0 = None
    used = 0
    for _ in range(rounds):
        X = A @ R.T + t
        i, _ = g.nearest(X)
        ok = i >= 0
        if ok.sum() < 6:
            break
        x, q, n = X[ok], V[i[ok]], N[i[ok]]
        r = np.einsum("ij,ij->i", x - q, n)
        keep = np.abs(r) <= max(3 * 1.4826 * np.median(np.abs(r)), 1e-12)
        x, n, r = x[keep], n[keep], r[keep]
        used = len(r)
        if rms0 is None:
            rms0 = float(np.sqrt(np.mean(r ** 2)))
        J = np.c_[np.cross(x, n), n]
        delta = np.linalg.lstsq(J, -r, rcond=None)[0]
        w, dt = delta[:3], delta[3:]
        a = float(np.linalg.norm(w))
        if a > 1e-12:
            k = w / a
            Kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
            dR = np.eye(3) + math.sin(a) * Kx + (1 - math.cos(a)) * Kx @ Kx
        else:
            dR = np.eye(3)
        R, t = dR @ R, dR @ t + dt
        if a < 1e-7 and np.linalg.norm(dt) < 1e-9:
            break
    X = A @ R.T + t
    i, _ = g.nearest(X)
    ok = i >= 0
    r = np.einsum("ij,ij->i", X[ok] - V[i[ok]], N[i[ok]]) if ok.any() else np.array([np.nan])
    r = r[np.abs(r) <= max(3 * 1.4826 * np.median(np.abs(r)), 1e-12)]
    return R, t, rms0, float(np.sqrt(np.mean(r ** 2))), used


def tie_check(R, t, rms_after: float, centre, mm: float) -> tuple[str, str]:
    """Whether a region is tied to the rock mesh well enough to ship: the correction ICP wanted (its shift at the
    region's middle and its turn) and what it left, against MAX_TIE_*."""
    shift_mm = float(np.linalg.norm(R @ np.asarray(centre) + t - np.asarray(centre))) * mm
    turn = math.degrees(math.acos(np.clip((np.trace(R) - 1) / 2, -1, 1)))
    if shift_mm > MAX_TIE_SHIFT_MM or turn > MAX_TIE_TURN_DEG:
        return "untied", f"against the rock mesh it would want moving {shift_mm:.1f} mm and turning {turn:.1f} deg"
    if rms_after * mm > MAX_TIE_RMS_MM:
        return "untied", f"it lies {rms_after * mm:.1f} mm (RMS) off the rock mesh"
    return "tied", f"{shift_mm:.1f} mm, {turn:.2f} deg off the rock mesh; {rms_after * mm:.1f} mm RMS"


# ---------------------------------------------------------------- the printable solid

def boundary_loops(F: np.ndarray) -> list[list[int]]:
    """The mesh's open edges chained into loops of vertex indices."""
    count = {}
    for tri in F:
        for i in range(3):
            a, b = tri[i], tri[(i + 1) % 3]
            count[(a, b)] = count.get((a, b), 0) + 1
    nxt = {a: b for (a, b) in count if (b, a) not in count}
    loops, seen = [], set()
    for start in list(nxt):
        if start in seen:
            continue
        loop, v = [], start
        while v in nxt and v not in seen:
            seen.add(v)
            loop.append(v)
            v = nxt[v]
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def solid(V: np.ndarray, F: np.ndarray, base_mm: float = 3.0, mm: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """A cut surface closed into a solid with a flat base: the surface, walls from its outer boundary down to a plane
    across its mean normal base_mm beyond its lowest point, and that plane filled. In millimetres (mm a unit).
    Holes inside the surface other than its outer boundary are closed with fans, flat."""
    V = np.asarray(V, float) * mm
    F = np.asarray(F, np.int64)
    n = vertex_normals(V, F).mean(axis=0)
    n /= np.linalg.norm(n)
    loops = sorted(boundary_loops(F), key=len, reverse=True)
    if not loops:
        return V, F
    outer, holes = loops[0], loops[1:]
    depth = float((V @ n).min()) - base_mm
    W = list(V)
    out_f = [list(f) for f in F]
    base_idx = []
    for v in outer:     # each boundary vertex dropped onto the base plane
        x = V[v]
        W.append(x - n * (x @ n - depth))
        base_idx.append(len(W) - 1)
    m = len(outer)
    for i in range(m):      # walls; the surface's boundary runs one way round, the walls go the other
        a, b = outer[i], outer[(i + 1) % m]
        a2, b2 = base_idx[i], base_idx[(i + 1) % m]
        out_f += [[b, a, a2], [b, a2, b2]]
    centre = np.mean([W[i] for i in base_idx], axis=0)
    W.append(centre)
    c = len(W) - 1
    for i in range(m):      # the base: a fan from its middle, facing out (down)
        out_f.append([base_idx[i], c, base_idx[(i + 1) % m]])
    for loop in holes:      # holes inside the surface: a fan each
        W.append(np.mean(V[loop], axis=0))
        h = len(W) - 1
        for i in range(len(loop)):
            out_f.append([loop[(i + 1) % len(loop)], loop[i], h])
    return np.array(W), np.array(out_f, np.int64)


def is_watertight(F: np.ndarray) -> bool:
    """Every edge in exactly two faces, once each way."""
    count = {}
    for tri in F:
        for i in range(3):
            e = (int(tri[i]), int(tri[(i + 1) % 3]))
            count[e] = count.get(e, 0) + 1
    return all(c == 1 and count.get((b, a)) == 1 for (a, b), c in count.items())


def write_stl(path: Path, V: np.ndarray, F: np.ndarray) -> None:
    import struct
    with open(path, "wb") as f:
        f.write(b"experiments/mapper/incr details.py, millimetres".ljust(80, b" "))
        f.write(struct.pack("<I", len(F)))
        for tri in F:
            a, b, c = V[tri]
            nrm = np.cross(b - a, c - a)
            nrm = nrm / (np.linalg.norm(nrm) or 1)
            f.write(struct.pack("<12fH", *nrm, *a, *b, *c, 0))


def write_3mf(path: Path, V: np.ndarray, F: np.ndarray, name: str) -> None:
    """A 3MF (a zip of the core spec's XML): units declared as millimetres, one object, single colour."""
    verts = "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in V)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in F)
    model = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<metadata name="Title">{name}</metadata><resources><object id="1" type="model"><mesh>'
             f'<vertices>{verts}</vertices><triangles>{tris}</triangles></mesh></object></resources>'
             '<build><item objectid="1"/></build></model>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", model)


def write_ply(path: Path, V: np.ndarray, F: np.ndarray) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import texture
    texture.write_ply(path, V, F)


def printable(V, F, p, r: float, mm: float, stem: Path, base_mm: float = 3.0) -> dict:
    """A region's surface cut to radius r about p, closed into a solid in millimetres, as stem.stl and stem.3mf; and
    the surface itself in metres as stem.ply (mm / 1000 a unit)."""
    Vc, Fc = cut(V, F, p, r)
    write_ply(stem.with_suffix(".ply"), (Vc - np.asarray(p, float)) * mm / 1000 + np.asarray(p, float) * mm / 1000, Fc)
    S, T = solid(Vc - np.asarray(p, float), Fc, base_mm=base_mm, mm=mm)
    write_stl(stem.with_suffix(".stl"), S, T)
    write_3mf(stem.with_suffix(".3mf"), S, T, stem.name)
    return {"faces": int(len(Fc)), "solid_faces": int(len(T)), "watertight": is_watertight(T)}


if __name__ == "__main__":
    if sys.argv[1] == "solid":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import core
        V, F = core.read_ply(Path(sys.argv[2]))
        p = [float(x) for x in sys.argv[3].split(",")]
        print(json.dumps(printable(V, F, p, float(sys.argv[4]), float(sys.argv[5]), Path(sys.argv[6]))))
