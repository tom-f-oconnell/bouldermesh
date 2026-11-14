package sh.boulderme.core

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * A photo's corners and what each looks like, found on a small copy of its brightness: enough to tell whether it will
 * match the photos taken near it, as meshing needs them to. FAST corners (a ring of 16 pixels round each, 9 in a row
 * all brighter or all darker), at most a few to each cell of a grid so that they spread over the picture, each
 * described by 256 comparisons of pairs of pixels round it (BRIEF), on a lightly blurred copy.
 *
 * Only a warning comes of it: a photo is never kept or left out for what this says (Session's notes).
 *
 * @property xs, ys where each corner is, in the stored picture's pixels (as CameraPose.lens has them).
 * @property desc four longs (256 bits) for each corner.
 */
class Features(val xs: FloatArray, val ys: FloatArray, val desc: LongArray) {
    val size: Int get() = xs.size

    companion object {
        const val SIDE = 640            // the small copy's longer side, pixels
        const val FAST_T = 12           // brightness steps a ring pixel must differ by: low, so that faint texture counts
        const val GRID_X = 16; const val GRID_Y = 12
        const val PER_CELL = 4
        const val PATCH = 15            // half the side of the square a corner's comparisons are made in
        private val RING = intArrayOf(0, -3, 1, -3, 2, -2, 3, -1, 3, 0, 3, 1, 2, 2, 1, 3, 0, 3, -1, 3, -2, 2, -3, 1, -3, 0, -3, -1, -2, -2, -1, -3)
        private val PAIRS: IntArray by lazy {      // 256 pairs of offsets, the same every time: spread round the middle, nearer it more often
            var seed = 0x2545F4914F6CDD1DL
            fun next(): Int {
                seed = seed xor (seed shl 13); seed = seed xor (seed ushr 7); seed = seed xor (seed shl 17)
                val a = ((seed ushr 11) % 1000).toInt() / 1000.0; val b = ((seed ushr 33) % 1000).toInt() / 1000.0
                return ((a + b - 1.0) * PATCH).toInt().coerceIn(-PATCH, PATCH)      // a triangle, peaked at the middle
            }
            IntArray(256 * 4) { next() }
        }

        /**
         * The features of a picture given as its brightness (one byte a pixel, `stride` bytes a row), as the sensor read it
         * out; `turn` is how far clockwise it's turned to store it (CameraPose.turned()), so that corners are placed in
         * the stored picture as its pose has it.
         */
        fun of(luma: ByteArray, width: Int, height: Int, stride: Int = width, turn: Int = 0): Features {
            val k = max(1, (max(width, height) + SIDE - 1) / SIDE)
            val w = width / k; val h = height / k
            val small = IntArray(w * h)
            for (y in 0 until h) for (x in 0 until w) {     // each pixel the mean of k by k
                var sum = 0
                for (dy in 0 until k) { val row = (y * k + dy) * stride + x * k; for (dx in 0 until k) sum += luma[row + dx].toInt() and 0xff }
                small[y * w + x] = sum / (k * k)
            }
            // corners: FAST-9, scored by how far the ring differs, the best few in each cell
            val m = 3 + PATCH + 1
            val cellW = (w + GRID_X - 1) / GRID_X; val cellH = (h + GRID_Y - 1) / GRID_Y
            val best = Array(GRID_X * GRID_Y) { ArrayList<IntArray>() }
            val ring = IntArray(16)
            for (y in m until h - m) for (x in m until w - m) {
                val c = small[y * w + x]
                var brighter = 0; var darker = 0
                for (i in 0 until 16) {
                    val v = small[(y + RING[2 * i + 1]) * w + x + RING[2 * i]]; ring[i] = v
                    if (v > c + FAST_T) brighter = brighter or (1 shl i) else if (v < c - FAST_T) darker = darker or (1 shl i)
                }
                if (!arc9(brighter) && !arc9(darker)) continue
                var score = 0
                for (i in 0 until 16) score += max(0, abs(ring[i] - c) - FAST_T)
                val cell = best[min(GRID_Y - 1, y / cellH) * GRID_X + min(GRID_X - 1, x / cellW)]
                if (cell.size < PER_CELL) cell.add(intArrayOf(x, y, score))
                else { val worst = cell.minBy { it[2] }; if (score > worst[2]) { worst[0] = x; worst[1] = y; worst[2] = score } }
            }
            val corners = best.flatMap { it }
            // a lightly blurred copy (3 by 3) to compare pixels on, as single pixels are noisy
            val blur = IntArray(w * h)
            for (y in 1 until h - 1) for (x in 1 until w - 1) {
                var s = 0
                for (dy in -1..1) for (dx in -1..1) s += small[(y + dy) * w + x + dx]
                blur[y * w + x] = s
            }
            val xs = FloatArray(corners.size); val ys = FloatArray(corners.size); val desc = LongArray(corners.size * 4)
            val pairs = PAIRS
            for ((n, c) in corners.withIndex()) {
                val x = c[0]; val y = c[1]
                for (b in 0 until 256) {
                    val a = blur[(y + pairs[4 * b + 1]) * w + x + pairs[4 * b]]; val o = blur[(y + pairs[4 * b + 3]) * w + x + pairs[4 * b + 2]]
                    if (a < o) desc[n * 4 + b / 64] = desc[n * 4 + b / 64] or (1L shl (b % 64))
                }
                // to the full-size picture, then turned as it's stored
                val fx = (x + 0.5f) * k; val fy = (y + 0.5f) * k
                when (turn) {
                    90 -> { xs[n] = height - fy; ys[n] = fx }
                    180 -> { xs[n] = width - fx; ys[n] = height - fy }
                    270 -> { xs[n] = fy; ys[n] = width - fx }
                    else -> { xs[n] = fx; ys[n] = fy }
                }
            }
            return Features(xs, ys, desc)
        }

        /** Whether 9 or more of the ring's 16 bits in a row are set, round the ring. */
        private fun arc9(bits: Int): Boolean {
            if (bits == 0) return false
            val twice = bits or (bits shl 16)
            var run = 0
            for (i in 0 until 32) { if (twice and (1 shl i) != 0) { run++; if (run >= 9) return true } else run = 0 }
            return false
        }

        private fun hamming(a: LongArray, i: Int, b: LongArray, j: Int): Int =
            (a[4 * i] xor b[4 * j]).countOneBits() + (a[4 * i + 1] xor b[4 * j + 1]).countOneBits() +
                (a[4 * i + 2] xor b[4 * j + 2]).countOneBits() + (a[4 * i + 3] xor b[4 * j + 3]).countOneBits()

        /**
         * How many of two photos' corners match each other and agree with where the phone's tracking says each photo
         * was taken from: each match's ray from one camera has to pass within EPI_DEG of the plane through both
         * cameras and the other's ray (a match that disagrees is a coincidence). With the cameras nearly in one place,
         * where that says nothing, matches are counted as they are.
         */
        fun links(a: Features, pa: CameraPose, b: Features, pb: CameraPose): Int {
            if (a.size == 0 || b.size == 0) return 0
            val base = pb.centre - pa.centre
            val far = base.norm >= MIN_BASE_M
            var n = 0
            for (i in 0 until a.size) {
                var first = 257; var second = 257; var at = -1
                for (j in 0 until b.size) {
                    val d = hamming(a.desc, i, b.desc, j)
                    if (d < first) { second = first; first = d; at = j } else if (d < second) second = d
                }
                if (at < 0 || first > MATCH_MOST || first > RATIO * second) continue
                if (!far) { n++; continue }
                val ra = ray(pa, a.xs[i], a.ys[i]); val rb = ray(pb, b.xs[at], b.ys[at])
                val normal = (base cross ra).unit()
                if (abs(normal dot rb) <= SIN_EPI) n++
            }
            return n
        }

        private fun ray(p: CameraPose, u: Float, v: Float): Vec3 =
            p.rot.rotate(Vec3((u - p.lens.cx) / p.lens.fx, (v - p.lens.cy) / p.lens.fy, 1.0)).unit()

        const val MATCH_MOST = 64           // bits of 256 two corners may differ by and still be a match
        const val RATIO = 0.8               // and the next best a fifth further off at least
        const val MIN_BASE_M = 0.03
        const val EPI_DEG = 2.0             // generous: the tracking's own error, and a phone's lens as the tracking has it
        private val SIN_EPI = kotlin.math.sin(EPI_DEG * kotlin.math.PI / 180)
    }
}

/** What a photo's matching said: its corners, the photos near it it was tried against, and how many matches it shares with each. */
data class Matching(val features: Int, val links: List<Pair<String, Int>>) {
    val best: Int? get() = links.maxOfOrNull { it.second }
}
