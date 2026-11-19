package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.exp
import kotlin.math.floor
import kotlin.math.ln
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * SIFT features as OpenCV finds and describes them (its sift.simd.hpp, followed step for step), so that a frame's
 * match a locate pack's, whose are OpenCV's (backend/locate.py). Unlike OpenCV's default, the picture isn't doubled
 * first: a phone's frame is big enough, and that's a quarter of the work.
 *
 * @property xs, ys where each is, in the picture's pixels (OpenCV's: the first pixel's centre at 0).
 * @property desc 128 bytes each, as RootSIFT (locate.py's root_sift()): unsigned, in a ByteArray.
 */
class Sift(val xs: FloatArray, val ys: FloatArray, val response: FloatArray, val desc: ByteArray) {
    val size: Int get() = xs.size

    private class Image(val w: Int, val h: Int, val px: FloatArray = FloatArray(w * h)) {
        operator fun get(x: Int, y: Int) = px[y * w + x]
    }

    private class Raw(val x: Float, val y: Float, val size: Float, val response: Float, val octave: Int, val layer: Int, val angle: Float)

    companion object {
        const val LAYERS = 3
        const val CONTRAST = 0.04
        const val EDGE = 10.0
        const val SIGMA = 1.6
        private const val BORDER = 5
        private const val MAX_INTERP = 5
        private const val ORI_BINS = 36
        private const val ORI_SIG = 1.5
        private const val ORI_RADIUS = 3 * ORI_SIG
        private const val ORI_PEAK = 0.8
        private const val D = 4
        private const val N = 8
        private const val DESCR_SCL = 3.0
        private const val DESCR_MAG_THR = 0.2
        private const val INT_DESCR = 512.0

        /**
         * The strongest `max` features of a grey picture.
         *
         * @param grey one byte a pixel, rows `stride` apart.
         */
        fun of(grey: ByteArray, w: Int, h: Int, stride: Int = w, max: Int = 1500): Sift {
            val base = Image(w, h)
            for (y in 0 until h) for (x in 0 until w) base.px[y * w + x] = (grey[y * stride + x].toInt() and 0xff).toFloat()
            val first = blur(base, sqrt(max(SIGMA * SIGMA - 0.25, 0.01)))
            val octaves = max(1, (ln(min(w, h).toDouble()) / ln(2.0)).roundToInt() - 2)
            val gauss = pyramid(first, octaves)
            val dog = gauss.map { g -> (0 until g.size - 1).map { i -> Image(g[i].w, g[i].h, FloatArray(g[i].px.size) { g[i + 1].px[it] - g[i].px[it] }) } }
            val found = ArrayList<Raw>()
            for (o in 0 until octaves) for (l in 1..LAYERS) extrema(gauss[o], dog[o], o, l, found)
            val kept = found.sortedByDescending { it.response }.take(max)
            val n = kept.size
            val xs = FloatArray(n); val ys = FloatArray(n); val resp = FloatArray(n); val desc = ByteArray(n * 128)
            val one = FloatArray(128)
            for ((i, k) in kept.withIndex()) {
                xs[i] = k.x; ys[i] = k.y; resp[i] = k.response
                describe(gauss[k.octave][k.layer], k, one)
                rootSift(one, desc, i * 128)
            }
            return Sift(xs, ys, resp, desc)
        }

        private fun pyramid(first: Image, octaves: Int): List<List<Image>> {
            val n = LAYERS + 3
            val sig = DoubleArray(n)
            sig[0] = SIGMA
            val k = 2.0.pow(1.0 / LAYERS)
            for (i in 1 until n) { val prev = k.pow(i - 1) * SIGMA; val total = prev * k; sig[i] = sqrt(total * total - prev * prev) }
            val out = ArrayList<List<Image>>()
            for (o in 0 until octaves) {
                val layers = ArrayList<Image>()
                for (i in 0 until n) {
                    layers.add(when {
                        o == 0 && i == 0 -> first
                        i == 0 -> half(out[o - 1][LAYERS])
                        else -> blur(layers[i - 1], sig[i])
                    })
                }
                out.add(layers)
            }
            return out
        }

        private fun half(a: Image): Image {
            val w = a.w / 2; val h = a.h / 2
            val b = Image(w, h)
            for (y in 0 until h) for (x in 0 until w) b.px[y * w + x] = a[2 * x, 2 * y]
            return b
        }

        /** A Gaussian blur as OpenCV's GaussianBlur with a size from sigma (its float rule) and reflected borders. */
        private fun blur(a: Image, sigma: Double): Image {
            var size = ((sigma * 4 * 2 + 1).roundToInt()) or 1
            if (size < 3) size = 3
            val r = size / 2
            val kern = DoubleArray(size) { val x = it - r; exp(-(x * x) / (2 * sigma * sigma)) }
            val s = kern.sum()
            val kf = FloatArray(size) { (kern[it] / s).toFloat() }
            val w = a.w; val h = a.h
            val tmp = FloatArray(w * h); val out = Image(w, h)
            fun refl(i: Int, n: Int): Int {     // BORDER_REFLECT_101
                if (n == 1) return 0
                var j = i
                while (j < 0 || j >= n) j = if (j < 0) -j else 2 * n - 2 - j
                return j
            }
            for (y in 0 until h) {
                val row = y * w
                for (x in 0 until w) {
                    var acc = 0f
                    if (x >= r && x < w - r) { for (t in 0 until size) acc += kf[t] * a.px[row + x - r + t] }
                    else for (t in 0 until size) acc += kf[t] * a.px[row + refl(x - r + t, w)]
                    tmp[row + x] = acc
                }
            }
            for (y in 0 until h) for (x in 0 until w) {
                var acc = 0f
                if (y >= r && y < h - r) { for (t in 0 until size) acc += kf[t] * tmp[(y - r + t) * w + x] }
                else for (t in 0 until size) acc += kf[t] * tmp[refl(y - r + t, h) * w + x]
                out.px[y * w + x] = acc
            }
            return out
        }

        private fun extrema(gauss: List<Image>, dog: List<Image>, o: Int, l: Int, out: MutableList<Raw>) {
            val threshold = floor(0.5 * CONTRAST / LAYERS * 255).toFloat()
            val img = dog[l]; val prev = dog[l - 1]; val next = dog[l + 1]
            val w = img.w; val h = img.h
            for (y in BORDER until h - BORDER) for (x in BORDER until w - BORDER) {
                val v = img.px[y * w + x]
                if (abs(v) <= threshold) continue
                var isMax = v > 0; var isMin = v < 0
                loop@ for (im in arrayOf(prev, img, next)) for (dy in -1..1) for (dx in -1..1) {
                    val u = im.px[(y + dy) * w + x + dx]
                    if (isMax && u > v) isMax = false
                    if (isMin && u < v) isMin = false
                    if (!isMax && !isMin) break@loop
                }
                if (!isMax && !isMin) continue
                val k = adjust(dog, o, l, x, y) ?: continue
                orientations(gauss[k.layer], k, out)
            }
        }

        /** OpenCV's adjustLocalExtrema: the extremum to sub-pixel and sub-layer, or null if it's weak or on an edge. */
        private fun adjust(dog: List<Image>, o: Int, layer0: Int, x0: Int, y0: Int): Raw? {
            val scale = 1.0 / 255; val d1 = scale * 0.5; val d2 = scale; val cross = scale * 0.25
            var x = x0; var y = y0; var l = layer0
            var xi = 0.0; var xr = 0.0; var xc = 0.0
            var i = 0
            while (true) {
                if (i >= MAX_INTERP) return null
                val img = dog[l]; val prev = dog[l - 1]; val next = dog[l + 1]
                val dx = (img[x + 1, y] - img[x - 1, y]) * d1; val dy = (img[x, y + 1] - img[x, y - 1]) * d1; val ds = (next[x, y] - prev[x, y]) * d1
                val v2 = img[x, y] * 2.0
                val dxx = (img[x + 1, y] + img[x - 1, y] - v2) * d2; val dyy = (img[x, y + 1] + img[x, y - 1] - v2) * d2; val dss = (next[x, y] + prev[x, y] - v2) * d2
                val dxy = (img[x + 1, y + 1] - img[x - 1, y + 1] - img[x + 1, y - 1] + img[x - 1, y - 1]) * cross
                val dxs = (next[x + 1, y] - next[x - 1, y] - prev[x + 1, y] + prev[x - 1, y]) * cross
                val dys = (next[x, y + 1] - next[x, y - 1] - prev[x, y + 1] + prev[x, y - 1]) * cross
                val hm = arrayOf(doubleArrayOf(dxx, dxy, dxs), doubleArrayOf(dxy, dyy, dys), doubleArrayOf(dxs, dys, dss))
                val sol = solve3(hm, doubleArrayOf(dx, dy, ds)) ?: return null
                xc = -sol[0]; xr = -sol[1]; xi = -sol[2]
                if (abs(xi) < 0.5 && abs(xr) < 0.5 && abs(xc) < 0.5) break
                if (abs(xi) > Int.MAX_VALUE / 3.0 || abs(xr) > Int.MAX_VALUE / 3.0 || abs(xc) > Int.MAX_VALUE / 3.0) return null
                x += xc.roundToInt(); y += xr.roundToInt(); l += xi.roundToInt()
                if (l < 1 || l > LAYERS || x < BORDER || x >= img.w - BORDER || y < BORDER || y >= img.h - BORDER) return null
                i++
            }
            val img = dog[l]; val prev = dog[l - 1]; val next = dog[l + 1]
            val dx = (img[x + 1, y] - img[x - 1, y]) * d1; val dy = (img[x, y + 1] - img[x, y - 1]) * d1; val ds = (next[x, y] - prev[x, y]) * d1
            val contr = img[x, y] * scale + (dx * xc + dy * xr + ds * xi) * 0.5
            if (abs(contr) * LAYERS < CONTRAST) return null
            val v2 = img[x, y] * 2.0
            val dxx = (img[x + 1, y] + img[x - 1, y] - v2) * d2; val dyy = (img[x, y + 1] + img[x, y - 1] - v2) * d2
            val dxy = (img[x + 1, y + 1] - img[x - 1, y + 1] - img[x + 1, y - 1] + img[x - 1, y - 1]) * cross
            val tr = dxx + dyy; val det = dxx * dyy - dxy * dxy
            if (det <= 0 || tr * tr * EDGE >= (EDGE + 1) * (EDGE + 1) * det) return null
            val f = 2.0.pow(o)
            return Raw(((x + xc) * f).toFloat(), ((y + xr) * f).toFloat(), (SIGMA * 2.0.pow((l + xi) / LAYERS) * f * 2).toFloat(),
                abs(contr).toFloat(), o, l, 0f)
        }

        private fun solve3(a: Array<DoubleArray>, b: DoubleArray): DoubleArray? {
            val det = a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0]) + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
            if (abs(det) < 1e-300) return null
            fun col(c: Int) = Array(3) { r -> DoubleArray(3) { k -> if (k == c) b[r] else a[r][k] } }
            fun d3(m: Array<DoubleArray>) = m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
            return doubleArrayOf(d3(col(0)) / det, d3(col(1)) / det, d3(col(2)) / det)
        }

        /** OpenCV's calcOrientationHist and the peaks it keeps: one keypoint for each direction strong enough. */
        private fun orientations(img: Image, k: Raw, out: MutableList<Raw>) {
            val f = 2.0.pow(k.octave)
            val scl = k.size * 0.5 / f
            val px = (k.x / f).roundToInt(); val py = (k.y / f).roundToInt()
            val radius = (ORI_RADIUS * scl).roundToInt()
            val sigma = ORI_SIG * scl
            val expScale = -1.0 / (2 * sigma * sigma)
            val raw = DoubleArray(ORI_BINS)
            for (i in -radius..radius) {
                val y = py + i
                if (y <= 0 || y >= img.h - 1) continue
                for (j in -radius..radius) {
                    val x = px + j
                    if (x <= 0 || x >= img.w - 1) continue
                    val dx = (img[x + 1, y] - img[x - 1, y]).toDouble(); val dy = (img[x, y - 1] - img[x, y + 1]).toDouble()
                    val wgt = exp((i * i + j * j) * expScale)
                    val ori = degrees(dy, dx)
                    var bin = (ORI_BINS / 360.0 * ori).roundToInt()
                    if (bin >= ORI_BINS) bin -= ORI_BINS
                    if (bin < 0) bin += ORI_BINS
                    raw[bin] += wgt * sqrt(dx * dx + dy * dy)
                }
            }
            val hist = DoubleArray(ORI_BINS) { b ->
                fun r(o: Int) = raw[((b + o) % ORI_BINS + ORI_BINS) % ORI_BINS]
                (r(-2) + r(2)) * (1.0 / 16) + (r(-1) + r(1)) * (4.0 / 16) + r(0) * (6.0 / 16)
            }
            val top = hist.max()
            val thr = top * ORI_PEAK
            for (j in 0 until ORI_BINS) {
                val l = if (j > 0) j - 1 else ORI_BINS - 1; val r = if (j < ORI_BINS - 1) j + 1 else 0
                if (hist[j] > hist[l] && hist[j] > hist[r] && hist[j] >= thr) {
                    var bin = j + 0.5 * (hist[l] - hist[r]) / (hist[l] - 2 * hist[j] + hist[r])
                    bin = if (bin < 0) ORI_BINS + bin else if (bin >= ORI_BINS) bin - ORI_BINS else bin
                    var angle = 360.0 - (360.0 / ORI_BINS) * bin
                    if (abs(angle - 360.0) < 1.2e-7) angle = 0.0
                    out.add(Raw(k.x, k.y, k.size, k.response, k.octave, k.layer, angle.toFloat()))
                }
            }
        }

        private fun degrees(y: Double, x: Double): Double { val a = atan2(y, x) * 180 / PI; return if (a < 0) a + 360 else a }

        /** OpenCV's calcSIFTDescriptor, into out (128 floats, as OpenCV's before it rounds them to bytes). */
        private fun describe(img: Image, k: Raw, out: FloatArray) {
            val f = 2.0.pow(k.octave)
            val ptx = (k.x / f).roundToInt(); val pty = (k.y / f).roundToInt()
            var angle = 360.0 - k.angle
            if (abs(angle - 360.0) < 1.2e-7) angle = 0.0
            val scl = k.size * 0.5 / f
            val cosT0 = kotlin.math.cos(angle * PI / 180); val sinT0 = kotlin.math.sin(angle * PI / 180)
            val binsPerDeg = N / 360.0
            val expScale = -1.0 / (D * D * 0.5)
            val histWidth = DESCR_SCL * scl
            var radius = (histWidth * 1.4142135623730951 * (D + 1) * 0.5).roundToInt()
            radius = min(radius, sqrt((img.w * img.w + img.h * img.h).toDouble()).toInt())
            val cosT = cosT0 / histWidth; val sinT = sinT0 / histWidth
            val hist = DoubleArray((D + 2) * (D + 2) * (N + 2))
            for (i in -radius..radius) for (j in -radius..radius) {
                val cRot = j * cosT - i * sinT; val rRot = j * sinT + i * cosT
                val rbin = rRot + D / 2 - 0.5; val cbin = cRot + D / 2 - 0.5
                val r = pty + i; val c = ptx + j
                if (rbin > -1 && rbin < D && cbin > -1 && cbin < D && r > 0 && r < img.h - 1 && c > 0 && c < img.w - 1) {
                    val dx = (img[c + 1, r] - img[c - 1, r]).toDouble(); val dy = (img[c, r - 1] - img[c, r + 1]).toDouble()
                    val wgt = exp((cRot * cRot + rRot * rRot) * expScale)
                    val mag = sqrt(dx * dx + dy * dy) * wgt
                    var obin = (degrees(dy, dx) - angle) * binsPerDeg
                    val r0 = floor(rbin).toInt(); val c0 = floor(cbin).toInt(); var o0 = floor(obin).toInt()
                    val rb = rbin - r0; val cb = cbin - c0; obin -= o0
                    if (o0 < 0) o0 += N
                    if (o0 >= N) o0 -= N
                    val vr1 = mag * rb; val vr0 = mag - vr1
                    val vrc11 = vr1 * cb; val vrc10 = vr1 - vrc11
                    val vrc01 = vr0 * cb; val vrc00 = vr0 - vrc01
                    val vrco111 = vrc11 * obin; val vrco110 = vrc11 - vrco111
                    val vrco101 = vrc10 * obin; val vrco100 = vrc10 - vrco101
                    val vrco011 = vrc01 * obin; val vrco010 = vrc01 - vrco011
                    val vrco001 = vrc00 * obin; val vrco000 = vrc00 - vrco001
                    val idx = ((r0 + 1) * (D + 2) + c0 + 1) * (N + 2) + o0
                    hist[idx] += vrco000; hist[idx + 1] += vrco001
                    hist[idx + (N + 2)] += vrco010; hist[idx + (N + 3)] += vrco011
                    hist[idx + (D + 2) * (N + 2)] += vrco100; hist[idx + (D + 2) * (N + 2) + 1] += vrco101
                    hist[idx + (D + 3) * (N + 2)] += vrco110; hist[idx + (D + 3) * (N + 2) + 1] += vrco111
                }
            }
            for (i in 0 until D) for (j in 0 until D) {
                val idx = ((i + 1) * (D + 2) + (j + 1)) * (N + 2)
                hist[idx] += hist[idx + N]; hist[idx + 1] += hist[idx + N + 1]
                for (kk in 0 until N) out[(i * D + j) * N + kk] = hist[idx + kk].toFloat()
            }
            var nrm2 = 0.0
            for (v in out) nrm2 += v.toDouble() * v
            val thr = sqrt(nrm2) * DESCR_MAG_THR
            nrm2 = 0.0
            for (i in out.indices) { val v = min(out[i].toDouble(), thr); out[i] = v.toFloat(); nrm2 += v * v }
            val s = INT_DESCR / max(sqrt(nrm2), 1.2e-7)
            for (i in out.indices) out[i] = (out[i] * s).toFloat()      // OpenCV's float descriptors aren't rounded
        }

        /** As locate.py's root_sift(): L1-normalised, square-rooted, times 512, to a byte. */
        private fun rootSift(d: FloatArray, out: ByteArray, at: Int) {
            var sum = 0.0
            for (v in d) sum += v
            sum = max(sum, 1e-12)
            for (i in 0 until 128) out[at + i] = min(255, (sqrt(d[i] / sum) * 512).roundToInt()).toByte()
        }
    }
}
