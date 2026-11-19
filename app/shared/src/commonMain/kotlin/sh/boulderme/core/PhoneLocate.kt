package sh.boulderme.core

import kotlin.math.ln
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.sqrt
import kotlin.random.Random

/**
 * A mesh's locate pack (backend/locate.py's build_pack(): its points, and OpenCV SIFT descriptors of them from its
 * photos), as the phone reads it, and placing a frame against it on the phone: SIFT (Sift), matches kept by Lowe's
 * ratio against other points, and the camera by RANSAC over three matches at a time, then tightened on those that
 * agree. As locate.py's place() does, so the two can be held to the same results.
 */
class LocatePack(val xyz: FloatArray, val point: IntArray, val photo: IntArray, val desc: ByteArray, val photos: Int) {
    val points: Int get() = xyz.size / 3
    val entries: Int get() = point.size

    companion object {
        /** Reads a pack's .bin. Throws IllegalArgumentException if it isn't one this reads. */
        fun parse(b: ByteArray): LocatePack {
            require(b.size >= 20 && b[0] == 'B'.code.toByte() && b[1] == 'M'.code.toByte() && b[2] == 'L'.code.toByte() && b[3] == 'P'.code.toByte()) { "not a locate pack" }
            fun u32(at: Int) = (b[at].toInt() and 0xff) or ((b[at + 1].toInt() and 0xff) shl 8) or ((b[at + 2].toInt() and 0xff) shl 16) or ((b[at + 3].toInt() and 0xff) shl 24)
            val v = u32(4); val n = u32(8); val e = u32(12); val nph = u32(16)
            require(v == 1) { "locate pack version $v" }
            require(n >= 0 && e >= 0 && b.size.toLong() == 20L + 12L * n + 4L * e + 2L * e + 128L * e) { "locate pack cut short" }
            var o = 20
            val xyz = FloatArray(n * 3) { Float.fromBits(u32(o + 4 * it)) }; o += 12 * n
            val point = IntArray(e) { u32(o + 4 * it) }; o += 4 * e
            val photo = IntArray(e) { (b[o + 2 * it].toInt() and 0xff) or ((b[o + 2 * it + 1].toInt() and 0xff) shl 8) }; o += 2 * e
            val desc = b.copyOfRange(o, o + 128 * e)
            require(point.all { it in 0 until n }) { "locate pack's entries name points it hasn't" }
            return LocatePack(xyz, point, photo, desc, nph)
        }
    }
}

object PhoneLocate {
    const val RATIO = 0.8
    const val REPROJ_PX = 4.0
    const val ITERS = 1000
    const val FEATURES = 1500
    /** The frame's longer side is brought to about this many pixels first (halving), for speed. */
    const val SIDE = 1000

    /** Where a frame's camera was against a pack, as locate.py's place() returns it: world (mesh) to camera. */
    data class Placed(val rot: Quat, val shift: Vec3, val inliers: Int, val matched: Int, val rmsPx: Double) {
        /** As a CameraPose in the mesh's coordinates. */
        fun pose(lens: Lens): CameraPose { val r = rot.conj(); return CameraPose(r.rotate(-shift), r, lens) }
    }

    /**
     * Places a grey picture against a pack.
     *
     * @param lens the picture's (its size w by h; OpenCV's pixels, the first pixel's centre at 0).
     * @return null if fewer than Locate.MIN_INLIERS matches agree on a place.
     */
    fun place(pack: LocatePack, grey: ByteArray, w: Int, h: Int, lens: Lens, random: Random = Random(1)): Placed? {
        var g = grey; var gw = w; var gh = h; var k = 1
        while (max(gw, gh) > SIDE * 1.4) {      // halve, each pixel the mean of four
            val nw = gw / 2; val nh = gh / 2
            val out = ByteArray(nw * nh)
            for (y in 0 until nh) for (x in 0 until nw) {
                val a = 2 * y * gw + 2 * x
                out[y * nw + x] = (((g[a].toInt() and 0xff) + (g[a + 1].toInt() and 0xff) + (g[a + gw].toInt() and 0xff) + (g[a + gw + 1].toInt() and 0xff) + 2) / 4).toByte()
            }
            g = out; gw = nw; gh = nh; k *= 2
        }
        val f = Sift.of(g, gw, gh, max = FEATURES)
        // back to the picture's pixels: a halved pixel's centre is between the four it came from
        val xs = DoubleArray(f.size) { f.xs[it] * k + (k - 1) / 2.0 }; val ys = DoubleArray(f.size) { f.ys[it] * k + (k - 1) / 2.0 }
        val m = match(pack, f)
        if (m.size < Locate.MIN_INLIERS) return null
        val img = m.map { (q, _) -> xs[q] to ys[q] }
        val obj = m.map { (_, p) -> Vec3(pack.xyz[3 * p].toDouble(), pack.xyz[3 * p + 1].toDouble(), pack.xyz[3 * p + 2].toDouble()) }
        return ransac(obj, img, lens, random)?.let { it.copy(matched = m.size) }
    }

    /** The frame's features matched to pack points: (feature, point), one match to each point, by Lowe's ratio against other points. */
    internal fun match(pack: LocatePack, f: Sift): List<Pair<Int, Int>> {
        val r2 = RATIO * RATIO
        val found = ArrayList<Triple<Int, Int, Float>>()
        val d = pack.desc; val q = f.desc
        val qi = IntArray(128)
        for (i in 0 until f.size) {
            for (t in 0 until 128) qi[t] = q[i * 128 + t].toInt() and 0xff
            var best = Long.MAX_VALUE; var bestP = -1; var second = Long.MAX_VALUE
            for (e in 0 until pack.entries) {
                var s = 0L; val base = e * 128
                var t = 0
                while (t < 128) { val x = qi[t] - (d[base + t].toInt() and 0xff); s += x * x; t++ }
                val p = pack.point[e]
                if (s < best) { if (p != bestP) second = best; best = s; bestP = p }
                else if (s < second && p != bestP) second = s
            }
            if (bestP >= 0 && (second == Long.MAX_VALUE || best <= r2 * second)) found.add(Triple(i, bestP, f.response[i]))
        }
        val seen = HashSet<Int>()
        return found.sortedByDescending { it.third }.filter { seen.add(it.second) }.map { it.first to it.second }
    }

    private fun ransac(obj: List<Vec3>, img: List<Pair<Double, Double>>, lens: Lens, random: Random): Placed? {
        val n = obj.size
        val rays = img.map { (u, v) -> Vec3((u - lens.cx) / lens.fx, (v - lens.cy) / lens.fy, 1.0).unit() }
        var best: List<Int> = emptyList(); var bestFit: Similarity? = null
        var iters = ITERS
        var i = 0
        while (i < iters) {
            i++
            val a = random.nextInt(n); var b = random.nextInt(n); var c = random.nextInt(n)
            if (a == b || a == c || b == c) continue
            for (s in p3p(listOf(obj[a], obj[b], obj[c]), listOf(rays[a], rays[b], rays[c]))) {
                val inl = inliers(s, obj, img, lens)
                if (inl.size > best.size) {
                    best = inl; bestFit = s
                    val miss = 1 - (inl.size.toDouble() / n).pow(3)      // a sample of three not all agreeing
                    val need = if (miss <= 0) 0 else (ln(0.001) / ln(miss)).toInt() + 1
                    iters = min(ITERS, max(50, need))
                }
            }
        }
        var fit = bestFit ?: return null
        if (best.size < Locate.MIN_INLIERS) return null
        repeat(3) {
            fit = refine(fit, best.map { obj[it] }, best.map { img[it] }, lens) ?: fit
            best = inliers(fit, obj, img, lens)
        }
        if (best.size < Locate.MIN_INLIERS) return null
        var e2 = 0.0
        for (j in best) { val (du, dv) = residual(fit, obj[j], img[j], lens)!!; e2 += du * du + dv * dv }
        return Placed(fit.rot, fit.shift, best.size, n, sqrt(e2 / best.size))
    }

    private fun residual(s: Similarity, p: Vec3, uv: Pair<Double, Double>, lens: Lens): Pair<Double, Double>? {
        val c = s.apply(p)
        if (c.z <= 1e-6) return null
        return (lens.fx * c.x / c.z + lens.cx - uv.first) to (lens.fy * c.y / c.z + lens.cy - uv.second)
    }

    private fun inliers(s: Similarity, obj: List<Vec3>, img: List<Pair<Double, Double>>, lens: Lens): List<Int> {
        val t2 = REPROJ_PX * REPROJ_PX
        return obj.indices.filter { j -> residual(s, obj[j], img[j], lens)?.let { (du, dv) -> du * du + dv * dv <= t2 } ?: false }
    }

    /** Gauss-Newton on the reprojection error: a small turn about the camera and a shift, a few rounds. */
    private fun refine(start: Similarity, obj: List<Vec3>, img: List<Pair<Double, Double>>, lens: Lens): Similarity? {
        var s = start
        repeat(8) {
            val ata = Array(6) { DoubleArray(6) }; val atb = DoubleArray(6)
            for (j in obj.indices) {
                val c = s.apply(obj[j])
                if (c.z <= 1e-6) continue
                val rp = c - s.shift          // R p: the turn's lever
                val iz = 1 / c.z
                val du = lens.fx * c.x * iz + lens.cx - img[j].first; val dv = lens.fy * c.y * iz + lens.cy - img[j].second
                // d(projection)/d(camera point)
                val gu = Vec3(lens.fx * iz, 0.0, -lens.fx * c.x * iz * iz); val gv = Vec3(0.0, lens.fy * iz, -lens.fy * c.y * iz * iz)
                // camera point after a turn w and shift t: c + w x rp + t; d/dw of (g . (w x rp)) = rp x g
                for ((g, r) in listOf(gu to du, gv to dv)) {
                    val row = rp cross g
                    val jr = doubleArrayOf(row.x, row.y, row.z, g.x, g.y, g.z)
                    for (a in 0 until 6) { atb[a] -= jr[a] * r; for (b in 0 until 6) ata[a][b] += jr[a] * jr[b] }
                }
            }
            for (a in 0 until 6) ata[a][a] += 1e-9
            val x = MagCalibration.solveLinear(ata, atb) ?: return null
            val w = Vec3(x[0], x[1], x[2])
            val turn = if (w.norm > 1e-15) Quat.axisAngle(w, w.norm) else Quat.IDENTITY
            s = Similarity(1.0, (turn * s.rot).unit(), turn.rotate(s.shift) + Vec3(x[3], x[4], x[5]))
            if (w.norm < 1e-9 && sqrt(x[3] * x[3] + x[4] * x[4] + x[5] * x[5]) < 1e-9) return s
        }
        return s
    }

    /**
     * The cameras that see three points along three rays (unit, camera axes): world to camera, as similarities of
     * scale 1. Grunert's quartic (as Haralick et al. 1994 give it), its real roots found by Durand-Kerner and Newton.
     */
    internal fun p3p(p: List<Vec3>, f: List<Vec3>): List<Similarity> {
        val ca = f[1] dot f[2]; val cb = f[0] dot f[2]; val cg = f[0] dot f[1]
        val a2 = (p[1] - p[2]).let { it dot it }; val b2 = (p[0] - p[2]).let { it dot it }; val c2 = (p[0] - p[1]).let { it dot it }
        if (a2 < 1e-12 || b2 < 1e-12 || c2 < 1e-12) return emptyList()
        val m = (a2 - c2) / b2; val pl = (a2 + c2) / b2
        val a4 = (m - 1) * (m - 1) - 4 * c2 / b2 * ca * ca
        val a3 = 4 * (m * (1 - m) * cb - (1 - pl) * ca * cg + 2 * c2 / b2 * ca * ca * cb)
        val a2c = 2 * (m * m - 1 + 2 * m * m * cb * cb + 2 * (b2 - c2) / b2 * ca * ca - 4 * pl * ca * cb * cg + 2 * (b2 - a2) / b2 * cg * cg)
        val a1 = 4 * (-m * (1 + m) * cb + 2 * a2 / b2 * cg * cg * cb - (1 - pl) * ca * cg)
        val a0 = (1 + m) * (1 + m) - 4 * a2 / b2 * cg * cg
        val out = ArrayList<Similarity>()
        for (v in realRoots(doubleArrayOf(a0, a1, a2c, a3, a4))) {
            if (v <= 0) continue
            val den = 2 * (cg - v * ca)
            if (kotlin.math.abs(den) < 1e-12) continue
            val u = ((-1 + m) * v * v - 2 * m * cb * v + 1 + m) / den
            val q = 1 + v * v - 2 * v * cb
            if (u <= 0 || q <= 1e-12) continue
            val d1 = sqrt(b2 / q)
            val cam = listOf(f[0] * d1, f[1] * (u * d1), f[2] * (v * d1))
            val s = Registration.fit(p, cam, scale = 1.0) ?: continue
            if (p.indices.all { s.apply(p[it]).distance(cam[it]) < 1e-3 * sqrt(b2) }) out.add(s)
        }
        return out
    }

    /** The real roots of c[0] + c[1] x + ... + c[n] x^n (c[n] not 0), polished by Newton. */
    internal fun realRoots(c: DoubleArray): List<Double> {
        var n = c.size - 1
        while (n > 0 && kotlin.math.abs(c[n]) < 1e-14 * c.maxOf { kotlin.math.abs(it) }) n--
        if (n < 1) return emptyList()
        val k = DoubleArray(n + 1) { c[it] / c[n] }
        val re = DoubleArray(n) { 0.4 * kotlin.math.cos(2.0 * it + 0.9) }; val im = DoubleArray(n) { 0.9 * kotlin.math.sin(2.0 * it + 0.9) }
        for (r in 0 until n) { val s = 1.0 + k.dropLast(1).maxOf { kotlin.math.abs(it) }; re[r] *= s; im[r] *= s }
        repeat(500) {
            var moved = 0.0
            for (i in 0 until n) {
                var pr = 1.0; var pi = 0.0            // the monic polynomial at root i, by Horner
                for (j in n - 1 downTo 0) { val nr = pr * re[i] - pi * im[i] + k[j]; pi = pr * im[i] + pi * re[i]; pr = nr }
                var qr = 1.0; var qi = 0.0            // the product of (root i - root j)
                for (j in 0 until n) if (j != i) { val dr = re[i] - re[j]; val di = im[i] - im[j]; val nr = qr * dr - qi * di; qi = qr * di + qi * dr; qr = nr }
                val d = qr * qr + qi * qi
                if (d < 1e-300) continue
                val sr = (pr * qr + pi * qi) / d; val si = (pi * qr - pr * qi) / d
                re[i] -= sr; im[i] -= si
                moved = max(moved, kotlin.math.abs(sr) + kotlin.math.abs(si))
            }
            if (moved < 1e-14) return@repeat
        }
        val out = ArrayList<Double>()
        for (i in 0 until n) {
            if (kotlin.math.abs(im[i]) > 1e-6 * (1 + kotlin.math.abs(re[i]))) continue
            var x = re[i]
            repeat(5) {
                var pv = 0.0; var dv = 0.0
                for (j in n downTo 0) { dv = dv * x + pv; pv = pv * x + k[j] }
                if (kotlin.math.abs(dv) > 1e-300) x -= pv / dv
            }
            out.add(x)
        }
        return out
    }
}
