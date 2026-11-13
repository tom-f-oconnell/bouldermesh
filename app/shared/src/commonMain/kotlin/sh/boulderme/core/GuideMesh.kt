package sh.boulderme.core

import kotlin.math.abs
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.sqrt

/**
 * The light copy of a mesh the server gives for drawing over the camera (its guide.bin): triangles in the mesh's own
 * coordinates, each with the patch it belongs to, how finely it was photographed (units a pixel; 0 if never) and from
 * how many directions.
 */
class GuideMesh(
    val positions: FloatArray,      // x, y, z for each vertex
    val indices: IntArray,          // a, b, c for each face
    val patch: IntArray,            // for each face; NO_PATCH for none
    val gsd: FloatArray,            // for each face
    val views: IntArray,            // for each face
    val patches: Int,
) {
    val vertices: Int get() = positions.size / 3
    val faces: Int get() = indices.size / 3

    fun vertex(i: Int) = Vec3(positions[3 * i].toDouble(), positions[3 * i + 1].toDouble(), positions[3 * i + 2].toDouble())

    private val centres = DoubleArray(faces * 3)
    private val radii = DoubleArray(faces)
    private val normals = DoubleArray(faces * 3)

    init {
        for (f in 0 until faces) {
            val a = vertex(indices[3 * f]); val b = vertex(indices[3 * f + 1]); val c = vertex(indices[3 * f + 2])
            val m = (a + b + c) / 3.0
            centres[3 * f] = m.x; centres[3 * f + 1] = m.y; centres[3 * f + 2] = m.z
            radii[f] = max(a.distance(m), max(b.distance(m), c.distance(m)))
            val n = ((b - a) cross (c - a)).unit()
            normals[3 * f] = n.x; normals[3 * f + 1] = n.y; normals[3 * f + 2] = n.z
        }
    }

    fun centre(f: Int) = Vec3(centres[3 * f], centres[3 * f + 1], centres[3 * f + 2])
    fun normal(f: Int) = Vec3(normals[3 * f], normals[3 * f + 1], normals[3 * f + 2])

    /**
     * How far along a ray (from origin, along unit direction dir) it first meets the mesh, between near and far; null
     * if it doesn't. Faces are met from either side.
     */
    fun raycast(origin: Vec3, dir: Vec3, near: Double = 0.0, far: Double = Double.MAX_VALUE): Double? {
        var best = far
        var hit = false
        for (f in 0 until faces) {
            val cx = centres[3 * f] - origin.x; val cy = centres[3 * f + 1] - origin.y; val cz = centres[3 * f + 2] - origin.z
            val along = cx * dir.x + cy * dir.y + cz * dir.z
            val r = radii[f]
            if (along < near - r || along > best + r) continue
            val perp2 = cx * cx + cy * cy + cz * cz - along * along
            if (perp2 > r * r) continue
            val t = triangle(origin, dir, f) ?: continue
            if (t >= near && t < best) { best = t; hit = true }
        }
        return if (hit) best else null
    }

    /** Möller-Trumbore: how far along the ray face f is met, or null. */
    private fun triangle(o: Vec3, d: Vec3, f: Int): Double? {
        val a = vertex(indices[3 * f]); val e1 = vertex(indices[3 * f + 1]) - a; val e2 = vertex(indices[3 * f + 2]) - a
        val p = d cross e2
        val det = e1 dot p
        if (abs(det) < 1e-14) return null
        val inv = 1.0 / det
        val s = o - a
        val u = (s dot p) * inv
        if (u < 0.0 || u > 1.0) return null
        val q = s cross e1
        val v = (d dot q) * inv
        if (v < 0.0 || u + v > 1.0) return null
        val t = (e2 dot q) * inv
        return if (t > 1e-9) t else null
    }

    /** Whether nothing of the mesh lies between two points (but for what's within `slack` of the second). */
    fun clear(from: Vec3, to: Vec3, slack: Double): Boolean {
        val d = to - from
        val length = d.norm
        if (length < 1e-9) return true
        return raycast(from, d / length, 0.0, length - slack) == null
    }

    /** Its bounds: the lowest and highest corner. */
    fun bounds(): Pair<Vec3, Vec3> {
        var lx = Double.MAX_VALUE; var ly = lx; var lz = lx; var hx = -lx; var hy = -lx; var hz = -lx
        for (i in 0 until vertices) {
            val v = vertex(i)
            if (v.x < lx) lx = v.x; if (v.y < ly) ly = v.y; if (v.z < lz) lz = v.z
            if (v.x > hx) hx = v.x; if (v.y > hy) hy = v.y; if (v.z > hz) hz = v.z
        }
        return Vec3(lx, ly, lz) to Vec3(hx, hy, hz)
    }

    /** A grid over its faces' centres, for finding the nearest to a point quickly (Registration). */
    inner class Nearest(private val cell: Double) {
        private val lo = bounds().first
        private val buckets = HashMap<Long, MutableList<Int>>()

        init {
            for (f in 0 until faces) buckets.getOrPut(key(centre(f))) { ArrayList() }.add(f)
        }

        private fun key(p: Vec3) = pack(floor((p.x - lo.x) / cell).toLong(), floor((p.y - lo.y) / cell).toLong(), floor((p.z - lo.z) / cell).toLong())
        private fun pack(i: Long, j: Long, k: Long) = ((i + 4096) shl 42) or ((j + 4096) shl 21) or (k + 4096)

        /** The face whose centre is nearest p, if one is within `within`; and how far. */
        fun of(p: Vec3, within: Double): Pair<Int, Double>? {
            val reach = kotlin.math.ceil(within / cell).toLong().coerceAtLeast(1)
            val ci = floor((p.x - lo.x) / cell).toLong(); val cj = floor((p.y - lo.y) / cell).toLong(); val ck = floor((p.z - lo.z) / cell).toLong()
            var best = -1
            var bestD = within * within
            for (i in ci - reach..ci + reach) for (j in cj - reach..cj + reach) for (k in ck - reach..ck + reach) {
                val list = buckets[pack(i, j, k)] ?: continue
                for (f in list) {
                    val dx = centres[3 * f] - p.x; val dy = centres[3 * f + 1] - p.y; val dz = centres[3 * f + 2] - p.z
                    val d = dx * dx + dy * dy + dz * dz
                    if (d < bestD) { bestD = d; best = f }
                }
            }
            return if (best >= 0) best to sqrt(bestD) else null
        }
    }

    companion object {
        const val NO_PATCH = 65535

        /** Reads guide.bin. Throws IllegalArgumentException if it isn't one, or is cut short. */
        fun parse(bytes: ByteArray): GuideMesh {
            require(bytes.size >= 16 && bytes[0] == 'B'.code.toByte() && bytes[1] == 'M'.code.toByte() && bytes[2] == 'G'.code.toByte() && bytes[3] == '1'.code.toByte()) { "not a guide mesh" }
            val nv = u32(bytes, 4); val nf = u32(bytes, 8); val np = u32(bytes, 12)
            val need = 16L + nv * 12L + nf * 12L + nf * 2L + nf * 4L + nf
            require(nv in 0..5_000_000 && nf in 0..5_000_000 && bytes.size.toLong() >= need) { "guide mesh cut short" }
            var at = 16
            val pos = FloatArray(nv * 3) { Float.fromBits(u32(bytes, at + 4 * it)) }
            at += nv * 12
            val idx = IntArray(nf * 3) { u32(bytes, at + 4 * it) }
            at += nf * 12
            require(idx.all { it in 0 until nv }) { "guide mesh: a face names a vertex it doesn't have" }
            val patch = IntArray(nf) { (bytes[at + 2 * it].toInt() and 0xff) or ((bytes[at + 2 * it + 1].toInt() and 0xff) shl 8) }
            at += nf * 2
            val gsd = FloatArray(nf) { Float.fromBits(u32(bytes, at + 4 * it)) }
            at += nf * 4
            val views = IntArray(nf) { bytes[at + it].toInt() and 0xff }
            return GuideMesh(pos, idx, patch, gsd, views, np)
        }

        private fun u32(b: ByteArray, at: Int): Int =
            (b[at].toInt() and 0xff) or ((b[at + 1].toInt() and 0xff) shl 8) or ((b[at + 2].toInt() and 0xff) shl 16) or ((b[at + 3].toInt() and 0xff) shl 24)

        /** Writes one, as the server does: for tests, and for a mesh made on the phone. */
        fun bytes(positions: FloatArray, indices: IntArray, patch: IntArray, gsd: FloatArray, views: IntArray, patches: Int): ByteArray {
            val nv = positions.size / 3; val nf = indices.size / 3
            val out = ByteArray(16 + nv * 12 + nf * 12 + nf * 2 + nf * 4 + nf)
            "BMG1".forEachIndexed { i, c -> out[i] = c.code.toByte() }
            var at = 4
            fun put(v: Int) { out[at] = v.toByte(); out[at + 1] = (v shr 8).toByte(); out[at + 2] = (v shr 16).toByte(); out[at + 3] = (v shr 24).toByte(); at += 4 }
            put(nv); put(nf); put(patches)
            positions.forEach { put(it.toRawBits()) }
            indices.forEach { put(it) }
            patch.forEach { out[at] = it.toByte(); out[at + 1] = (it shr 8).toByte(); at += 2 }
            gsd.forEach { put(it.toRawBits()) }
            views.forEach { out[at++] = it.toByte() }
            return out
        }
    }
}
