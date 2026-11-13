package sh.boulderme.core

import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * Lining a mesh up with the world the phone is tracking: from spots tapped on both, then tightened against the points
 * the phone's tracking finds on the rock. Both frames have +Y up, so two tapped spots are enough: they give the turn
 * about Y, the size and the shift.
 */
object Registration {
    /** Tapped spots further apart sideways than this share of how far apart they are say which way the mesh is turned. */
    const val MIN_SIDEWAYS = 0.25

    /**
     * The mesh-to-world fit from pairs of the same spot in each (a spot on the mesh, where it is in the world).
     *
     * @param scale metres a mesh unit if it's known and to be kept; null to take it from the pairs.
     * @return null with fewer than two pairs, or pairs too close together or too nearly above one another to tell.
     */
    fun fromTaps(pairs: List<Pair<Vec3, Vec3>>, scale: Double? = null): Similarity? {
        if (pairs.size < 2) return null
        val ma = mean(pairs.map { it.first }); val mb = mean(pairs.map { it.second })
        var saa = 0.0; var sbb = 0.0; var c = 0.0; var s = 0.0; var side = 0.0
        for ((a0, b0) in pairs) {
            val a = a0 - ma; val b = b0 - mb
            saa += a dot a; sbb += b dot b
            c += a.x * b.x + a.z * b.z
            s += a.z * b.x - a.x * b.z
            side += a.x * a.x + a.z * a.z
        }
        if (saa < 1e-12 || sbb < 1e-8 || side / saa < MIN_SIDEWAYS * MIN_SIDEWAYS) return null
        val k = scale ?: sqrt(sbb / saa)
        val rot = Quat.yaw(atan2(s, c))
        return Similarity(k, rot, mb - rot.rotate(ma * k))
    }

    /**
     * The least-squares scale, turn and shift taking points `from` onto points `to` (Horn's closed form).
     *
     * @param scale kept at this if given.
     * @param yawOnly the turn is about Y alone.
     */
    fun fit(from: List<Vec3>, to: List<Vec3>, scale: Double? = null, yawOnly: Boolean = false): Similarity? {
        if (from.size < 3 || from.size != to.size) return null
        val ma = mean(from); val mb = mean(to)
        val m = DoubleArray(9)
        var saa = 0.0; var sbb = 0.0
        for (i in from.indices) {
            val a = from[i] - ma; val b = to[i] - mb
            m[0] += a.x * b.x; m[1] += a.x * b.y; m[2] += a.x * b.z
            m[3] += a.y * b.x; m[4] += a.y * b.y; m[5] += a.y * b.z
            m[6] += a.z * b.x; m[7] += a.z * b.y; m[8] += a.z * b.z
            saa += a dot a; sbb += b dot b
        }
        if (saa < 1e-12 || sbb < 1e-12) return null
        val rot = if (yawOnly) Quat.yaw(atan2(m[6] - m[2], m[0] + m[8])) else horn(m)
        var along = 0.0
        for (i in from.indices) along += (to[i] - mb) dot rot.rotate(from[i] - ma)
        val k = scale ?: (along / saa)
        if (k <= 0) return null
        return Similarity(k, rot, mb - rot.rotate(ma * k))
    }

    /** The rotation best taking a onto b, from their summed outer products (row-major sums of a's x, y, z times b's). */
    private fun horn(m: DoubleArray): Quat {
        val sxx = m[0]; val sxy = m[1]; val sxz = m[2]; val syx = m[3]; val syy = m[4]; val syz = m[5]; val szx = m[6]; val szy = m[7]; val szz = m[8]
        val n = arrayOf(
            doubleArrayOf(sxx + syy + szz, syz - szy, szx - sxz, sxy - syx),
            doubleArrayOf(syz - szy, sxx - syy - szz, sxy + syx, szx + sxz),
            doubleArrayOf(szx - sxz, sxy + syx, -sxx + syy - szz, syz + szy),
            doubleArrayOf(sxy - syx, szx + sxz, syz + szy, -sxx - syy + szz),
        )
        val v = topEigenvector(n)
        return Quat(v[0], v[1], v[2], v[3]).unit()
    }

    /** The eigenvector of a symmetric 4x4 matrix with the largest eigenvalue, by Jacobi's rotations. */
    internal fun topEigenvector(a: Array<DoubleArray>): DoubleArray {
        val n = 4
        val v = Array(n) { i -> DoubleArray(n) { j -> if (i == j) 1.0 else 0.0 } }
        repeat(60) {
            var off = 0.0
            for (i in 0 until n) for (j in i + 1 until n) off += a[i][j] * a[i][j]
            if (off < 1e-22) return@repeat
            for (p in 0 until n) for (q in p + 1 until n) {
                if (abs(a[p][q]) < 1e-300) continue
                val theta = (a[q][q] - a[p][p]) / (2 * a[p][q])
                val t = (if (theta >= 0) 1.0 else -1.0) / (abs(theta) + sqrt(theta * theta + 1))
                val c = 1 / sqrt(t * t + 1); val s = t * c
                for (k in 0 until n) { val akp = a[k][p]; val akq = a[k][q]; a[k][p] = c * akp - s * akq; a[k][q] = s * akp + c * akq }
                for (k in 0 until n) { val apk = a[p][k]; val aqk = a[q][k]; a[p][k] = c * apk - s * aqk; a[q][k] = s * apk + c * aqk }
                for (k in 0 until n) { val vkp = v[k][p]; val vkq = v[k][q]; v[k][p] = c * vkp - s * vkq; v[k][q] = s * vkp + c * vkq }
            }
        }
        var best = 0
        for (i in 1 until n) if (a[i][i] > a[best][best]) best = i
        return DoubleArray(n) { v[it][best] }
    }

    /** How a refinement went: the fit, how many of the points given lay on the mesh, and how far off they were (metres, rms). */
    data class Refined(val fit: Similarity, val matched: Int, val rmsM: Double)

    /**
     * Tightens a fit against points the phone's tracking found in the world: each is matched to the nearest face of
     * the mesh, those too far off are left out (the ground, other rocks), and the fit is nudged (a small turn, shift
     * and change of size) to bring the faces' planes onto their points, a few times over, each with a closer limit.
     * Planes, so that a point needn't know where along a face it lies: only that it's on it.
     *
     * @param start the fit so far, close enough that most points on the rock land within `reachM` of the mesh.
     * @param points in the world.
     * @param freeScale whether the mesh's size may change (it's a guess, or the taps set it).
     * @param maxTiltDeg past this far off level, the turn is kept about Y alone.
     * @return null if too few points land on the mesh.
     */
    fun refine(start: Similarity, points: List<Vec3>, guide: GuideMesh, nearest: GuideMesh.Nearest, reachM: Double = 0.4,
               freeScale: Boolean = true, maxTiltDeg: Double = 20.0, rounds: Int = 12): Refined? {
        var fit = start
        var reach = reachM
        var last: Refined? = null
        repeat(rounds) {
            val inv = fit.inverse()
            val on = ArrayList<Vec3>(); val normal = ArrayList<Vec3>(); val off = ArrayList<Double>()
            for (w in points) {
                val m = inv.apply(w)
                val (f, _) = nearest.of(m, reach / fit.scale) ?: continue
                val n = fit.dir(guide.normal(f))
                val d = (fit.apply(guide.centre(f)) - w) dot n      // how far the face's plane is from the point, along its normal
                if (abs(d) > reach) continue
                on.add(w + n * d); normal.add(n); off.add(d)
            }
            if (on.size < MIN_MATCHED) return last
            // the nearest three quarters: what's left of the ground and of other rocks goes
            val limit = off.map { abs(it) }.sorted()[(off.size * 3) / 4]
            val keep = off.indices.filter { abs(off[it]) <= limit }
            val middle = mean(keep.map { on[it] })
            fun solve(yawOnly: Boolean): Similarity? {
                // unknowns: the turn (x, y, z, or y alone), the shift, and the change of size about the middle
                val cols = (if (yawOnly) 1 else 3) + 3 + (if (freeScale) 1 else 0)
                val ata = Array(cols) { DoubleArray(cols) }; val atb = DoubleArray(cols)
                for (i in keep) {
                    val q = on[i]; val n = normal[i]
                    val lever = q cross n
                    val row = DoubleArray(cols)
                    var c = 0
                    if (yawOnly) row[c++] = lever.y else { row[c++] = lever.x; row[c++] = lever.y; row[c++] = lever.z }
                    row[c++] = n.x; row[c++] = n.y; row[c++] = n.z
                    if (freeScale) row[c] = n dot (q - middle)
                    for (a in 0 until cols) { atb[a] -= row[a] * off[i]; for (b in 0 until cols) ata[a][b] += row[a] * row[b] }
                }
                for (a in 0 until cols) ata[a][a] += 1e-9 * keep.size       // so that what the points don't pin down stays put
                val x = MagCalibration.solveLinear(ata, atb) ?: return null
                var c = 0
                val turn = if (yawOnly) Vec3(0.0, x[c++], 0.0) else Vec3(x[c++], x[c++], x[c++])
                val shift = Vec3(x[c++], x[c++], x[c++])
                val grow = if (freeScale) (1 + x[c]).coerceIn(0.9, 1.1) else 1.0      // a tenth at a time, so that a bad round can't run away with it
                val r = if (turn.norm > 1e-12) Quat.axisAngle(turn, min(turn.norm, 0.2)) else Quat.IDENTITY
                return Similarity(fit.scale * grow, (r * fit.rot).unit(), r.rotate((fit.shift - middle) * grow) + middle + shift)
            }
            var next = solve(false) ?: return last
            if (next.tiltDeg > maxTiltDeg) next = solve(true) ?: return last
            fit = next
            last = Refined(fit, on.size, sqrt(keep.sumOf { off[it] * off[it] } / keep.size))     // as far off as they were before this nudge
            reach = max(0.08, reach * 0.8)
        }
        // how far off the points are once done, measured afresh
        val f = fit; val inv = f.inverse()
        val left = ArrayList<Double>()
        for (w in points) {
            val (face, _) = nearest.of(inv.apply(w), reach / f.scale) ?: continue
            left.add(abs((f.apply(guide.centre(face)) - w) dot f.dir(guide.normal(face))))
        }
        if (left.size < MIN_MATCHED) return last
        val kept = left.sorted().take((left.size * 3) / 4)
        return Refined(f, left.size, sqrt(kept.sumOf { it * it } / kept.size))
    }

    const val MIN_MATCHED = 30

    internal fun mean(p: List<Vec3>): Vec3 {
        var x = 0.0; var y = 0.0; var z = 0.0
        for (v in p) { x += v.x; y += v.y; z += v.z }
        return Vec3(x / p.size, y / p.size, z / p.size)
    }
}
