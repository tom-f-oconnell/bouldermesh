package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cbrt
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.sqrt

/**
 * A small copy of a picture in CIELAB (L 0-100, a and b about -100..100), upright as the photo is stored, its colours
 * shifted so that the grey of the wall reads as grey whatever the camera's white balance did.
 *
 * @property step how many of the stored picture's pixels each of its pixels is across.
 */
class Picture(val w: Int, val h: Int, val step: Double, val l: FloatArray, val a: FloatArray, val b: FloatArray) {
    /** The pixel of it at the stored picture's (u, v), or -1 outside it. */
    fun at(u: Double, v: Double): Int {
        val x = (u / step).toInt(); val y = (v / step).toInt()
        return if (x in 0 until w && y in 0 until h) y * w + x else -1
    }

    companion object {
        const val SIDE = 320            // its longer side, pixels: a hand-sized hold 3 m off is still a few of them

        /**
         * From a camera's YUV 4:2:0 picture with its colour in pairs of bytes a row (NV21: V first; ARKit's CbCr: U
         * first), full range, turned clockwise by `turn` as the photo is stored.
         */
        fun ofYuv(y: ByteArray, yStride: Int, uv: ByteArray, uvOffset: Int, uvStride: Int, width: Int, height: Int, vFirst: Boolean,
                  turn: Int = 0, side: Int = SIDE): Picture {
            val k = max(1, (max(width, height) + side - 1) / side)
            val w = width / k; val h = height / k
            val l = FloatArray(w * h); val a = FloatArray(w * h); val b = FloatArray(w * h)
            val s = max(1, k / 4)        // only every few pixels of a big block are read: its mean barely changes
            val lab = DoubleArray(3)
            for (yy in 0 until h) for (xx in 0 until w) {
                var sy = 0; var n = 0
                var row = yy * k
                while (row < yy * k + k) { var col = xx * k; while (col < xx * k + k) { sy += y[row * yStride + col].toInt() and 0xff; n++; col += s }; row += s }
                var su = 0; var sv = 0; var m = 0
                val half = max(1, k / 2)
                var cr = yy * k / 2
                while (cr < yy * k / 2 + half) {
                    var cc = xx * k / 2
                    while (cc < xx * k / 2 + half) {
                        val at = uvOffset + cr * uvStride + cc * 2
                        val first = uv[at].toInt() and 0xff; val second = uv[at + 1].toInt() and 0xff
                        if (vFirst) { sv += first; su += second } else { su += first; sv += second }
                        m++; cc += max(1, s / 2)
                    }
                    cr += max(1, s / 2)
                }
                val yv = sy.toDouble() / n; val u = su.toDouble() / m - 128; val v = sv.toDouble() / m - 128
                rgbToLab(yv + 1.402 * v, yv - 0.344136 * u - 0.714136 * v, yv + 1.772 * u, lab)
                val i = yy * w + xx
                l[i] = lab[0].toFloat(); a[i] = lab[1].toFloat(); b[i] = lab[2].toFloat()
            }
            return finish(w, h, k.toDouble(), l, a, b, turn)
        }

        /** From 0xRRGGBB pixels (tests, and a picture already made small). */
        fun ofRgb(rgb: IntArray, width: Int, height: Int, step: Double = 1.0, turn: Int = 0): Picture {
            val l = FloatArray(rgb.size); val a = FloatArray(rgb.size); val b = FloatArray(rgb.size)
            val lab = DoubleArray(3)
            for (i in rgb.indices) {
                val c = rgb[i]
                rgbToLab(((c shr 16) and 0xff).toDouble(), ((c shr 8) and 0xff).toDouble(), (c and 0xff).toDouble(), lab)
                l[i] = lab[0].toFloat(); a[i] = lab[1].toFloat(); b[i] = lab[2].toFloat()
            }
            return finish(width, height, step, l, a, b, turn)
        }

        private fun finish(w: Int, h: Int, step: Double, l: FloatArray, a: FloatArray, b: FloatArray, turn: Int): Picture {
            neutral(l, a, b)
            return when (((turn % 360) + 360) % 360) {
                90 -> Picture(h, w, step, turned(l, w, h, 90), turned(a, w, h, 90), turned(b, w, h, 90))
                180 -> Picture(w, h, step, turned(l, w, h, 180), turned(a, w, h, 180), turned(b, w, h, 180))
                270 -> Picture(h, w, step, turned(l, w, h, 270), turned(a, w, h, 270), turned(b, w, h, 270))
                else -> Picture(w, h, step, l, a, b)
            }
        }

        /** The picture w by h turned clockwise by a quarter, a half or three quarters. */
        private fun turned(src: FloatArray, w: Int, h: Int, deg: Int): FloatArray {
            val out = FloatArray(src.size)
            when (deg) {
                90 -> for (y in 0 until w) for (x in 0 until h) out[y * h + x] = src[(h - 1 - x) * w + y]
                180 -> for (i in src.indices) out[src.size - 1 - i] = src[i]
                else -> for (y in 0 until w) for (x in 0 until h) out[y * h + x] = src[x * w + (w - 1 - y)]
            }
            return out
        }

        /**
         * Shifts a and b so that the near-greys (most of a gym's walls) average to grey: a camera's white balance
         * leaves them tinted, by a different amount from one photo to the next on a phone that doesn't hold it.
         */
        private fun neutral(l: FloatArray, a: FloatArray, b: FloatArray) {
            val xs = ArrayList<Float>(); val ys = ArrayList<Float>()
            for (i in l.indices) if (l[i] in 15f..97f && a[i] * a[i] + b[i] * b[i] < NEUTRAL_C * NEUTRAL_C) { xs.add(a[i]); ys.add(b[i]) }
            if (xs.size < l.size / 20) return
            xs.sort(); ys.sort()
            val da = xs[xs.size / 2].coerceIn(-NEUTRAL_MOST, NEUTRAL_MOST); val db = ys[ys.size / 2].coerceIn(-NEUTRAL_MOST, NEUTRAL_MOST)
            for (i in l.indices) { a[i] -= da; b[i] -= db }
        }

        private const val NEUTRAL_C = 15f       // a pixel this near grey may be the wall's
        private const val NEUTRAL_MOST = 10f    // and the wall's tint is never taken to be more than this

        private val LINEAR = DoubleArray(256) { val c = it / 255.0; if (c <= 0.04045) c / 12.92 else ((c + 0.055) / 1.055).pow(2.4) }

        /** sRGB (0-255 each, clamped) to CIELAB, D65. */
        fun rgbToLab(r: Double, g: Double, b: Double, out: DoubleArray) {
            val lr = LINEAR[r.toInt().coerceIn(0, 255)]; val lg = LINEAR[g.toInt().coerceIn(0, 255)]; val lb = LINEAR[b.toInt().coerceIn(0, 255)]
            val x = (0.4124 * lr + 0.3576 * lg + 0.1805 * lb) / 0.95047
            val y = 0.2126 * lr + 0.7152 * lg + 0.0722 * lb
            val z = (0.0193 * lr + 0.1192 * lg + 0.9505 * lb) / 1.08883
            fun f(t: Double) = if (t > 0.008856) cbrt(t) else 7.787 * t + 16.0 / 116
            val fx = f(x); val fy = f(y); val fz = f(z)
            out[0] = 116 * fy - 16; out[1] = 500 * (fx - fy); out[2] = 200 * (fy - fz)
        }

        /** CIELAB to 0xRRGGBB, clamped to what sRGB can show. */
        fun labToRgb(l: Double, a: Double, b: Double): Int {
            val fy = (l + 16) / 116; val fx = fy + a / 500; val fz = fy - b / 200
            fun inv(t: Double) = if (t * t * t > 0.008856) t * t * t else (t - 16.0 / 116) / 7.787
            val x = inv(fx) * 0.95047; val y = inv(fy); val z = inv(fz) * 1.08883
            val lin = doubleArrayOf(3.2406 * x - 1.5372 * y - 0.4986 * z, -0.9689 * x + 1.8758 * y + 0.0415 * z, 0.0557 * x - 0.2040 * y + 1.0570 * z)
            var out = 0
            for (c in lin) {
                val s = if (c <= 0.0031308) 12.92 * c else 1.055 * c.coerceAtLeast(0.0).pow(1 / 2.4) - 0.055
                out = (out shl 8) or (s * 255).toInt().coerceIn(0, 255)
            }
            return out
        }
    }
}

/**
 * A patch of one colour in a photo, that may be a hold: where it is in the stored picture (pixels), how big, its colour
 * (CIELAB, the mean of its pixels) and the brightness round it. `src`: what found it, "colour" (Blobs.find) or "model"
 * (a hold-finding model's mask, Blobs.ofMask).
 */
data class Blob(val u: Double, val v: Double, val areaPx: Double, val x0: Double, val y0: Double, val x1: Double, val y1: Double,
                val l: Double, val a: Double, val b: Double, val ringL: Double, val edge: Boolean, val src: String = "colour") {
    val chroma: Double get() = hypot(a, b)
    val hueDeg: Double get() = ((atan2(b, a) * 180 / PI) + 360) % 360
    val chromatic: Boolean get() = chroma >= Blobs.C_MIN
    val radiusPx: Double get() = sqrt(areaPx / PI)
}

/** Finding patches of one colour in a picture: the colour finder (A) and what a model's mask of holds gives (B). */
object Blobs {
    const val C_MIN = 13.0          // less colourful than this: grey, black or white
    const val JOIN_HUE = 10.0       // neighbouring colourful pixels within this many degrees of hue are one patch
    const val JOIN_L = 18.0         // and this much brightness (a hold's lit top and shaded side are still one hold)
    const val JOIN_GREY_L = 7.0     // neighbouring grey pixels: within this much brightness
    const val MIN_PX = 5            // a patch smaller than this (of the small picture's pixels) is noise
    const val MAX_SHARE = 0.12      // and one bigger than this share of the picture a wall, a painted panel or a volume
    const val GREY_CONTRAST = 12.0  // a grey patch is a hold (a black or white one) only if this much darker or lighter than round it

    /** The colour finder: patches of one colour, but those too small or too big to be a hold, and grey ones as grey as round them. */
    fun find(p: Picture): List<Blob> {
        val n = p.w * p.h
        val parent = IntArray(n) { it }
        fun root(i: Int): Int { var r = i; while (parent[r] != r) { parent[r] = parent[parent[r]]; r = parent[r] }; return r }
        fun join(i: Int, j: Int) { val a = root(i); val b = root(j); if (a != b) parent[max(a, b)] = min(a, b) }
        val c = FloatArray(n) { hypot(p.a[it], p.b[it]) }
        val hue = FloatArray(n) { (atan2(p.b[it], p.a[it]) * 180 / PI).toFloat() }
        fun alike(i: Int, j: Int): Boolean {
            val ci = c[i] >= C_MIN; val cj = c[j] >= C_MIN
            if (ci != cj) return false
            val dl = abs(p.l[i] - p.l[j])
            if (!ci) return dl < JOIN_GREY_L
            var dh = abs(hue[i] - hue[j]); if (dh > 180) dh = 360 - dh
            val ratio = c[i] / c[j]
            return dh < JOIN_HUE && dl < JOIN_L && ratio in 0.6f..1.67f
        }
        for (y in 0 until p.h) for (x in 0 until p.w) {
            val i = y * p.w + x
            if (x + 1 < p.w && alike(i, i + 1)) join(i, i + 1)
            if (y + 1 < p.h && alike(i, i + p.w)) join(i, i + p.w)
        }
        val label = IntArray(n) { root(it) }
        return summarise(p, label, "colour") { r, grey -> !grey || abs(r.l - r.ringL) >= GREY_CONTRAST }
    }

    /**
     * What a hold-finding model's mask gives: `label` a number for each pixel of the picture (0 none, else which hold),
     * each hold's colour the mean of its pixels'. A model's hold is a hold whatever its colour, so grey ones are kept.
     */
    fun ofMask(p: Picture, label: IntArray): List<Blob> = summarise(p, IntArray(label.size) { if (label[it] > 0) label[it] else -1 }, "model", minPx = 3) { _, _ -> true }

    private class Sum { var n = 0; var x = 0.0; var y = 0.0; var l = 0.0; var a = 0.0; var b = 0.0; var x0 = Int.MAX_VALUE; var y0 = Int.MAX_VALUE; var x1 = -1; var y1 = -1
        var ringL = 0.0; var ringN = 0; var edge = false }

    private fun summarise(p: Picture, label: IntArray, src: String, minPx: Int = MIN_PX, keep: (Blob, Boolean) -> Boolean): List<Blob> {
        val sums = HashMap<Int, Sum>()
        for (y in 0 until p.h) for (x in 0 until p.w) {
            val i = y * p.w + x; val k = label[i]
            if (k < 0) continue
            val s = sums.getOrPut(k) { Sum() }
            s.n++; s.x += x; s.y += y; s.l += p.l[i]; s.a += p.a[i]; s.b += p.b[i]
            s.x0 = min(s.x0, x); s.y0 = min(s.y0, y); s.x1 = max(s.x1, x); s.y1 = max(s.y1, y)
            if (x == 0 || y == 0 || x == p.w - 1 || y == p.h - 1) s.edge = true
        }
        val most = MAX_SHARE * p.w * p.h
        val wanted = sums.filterValues { it.n in minPx..most.toInt() }
        // the brightness just round each: what a grey hold has to stand out from
        for (y in 0 until p.h) for (x in 0 until p.w) {
            val i = y * p.w + x
            val s = wanted[label[i]] ?: continue
            for (j in intArrayOf(if (x > 0) i - 1 else -1, if (x + 1 < p.w) i + 1 else -1, if (y > 0) i - p.w else -1, if (y + 1 < p.h) i + p.w else -1))
                if (j >= 0 && label[j] != label[i]) { s.ringL += p.l[j]; s.ringN++ }
        }
        val out = ArrayList<Blob>()
        val k = p.step
        for (s in wanted.values) {
            val bl = Blob((s.x / s.n + 0.5) * k, (s.y / s.n + 0.5) * k, s.n * k * k, s.x0 * k, s.y0 * k, (s.x1 + 1) * k, (s.y1 + 1) * k,
                s.l / s.n, s.a / s.n, s.b / s.n, if (s.ringN > 0) s.ringL / s.ringN else s.l / s.n, s.edge, src)
            if (keep(bl, !bl.chromatic)) out.add(bl)
        }
        return out
    }
}

/**
 * The colour of a gym climb's holds, from those tapped as being of it: a patch is of it if it's near enough in colour
 * to any of them. `tolerance` widens (above 1) or narrows what counts as near enough.
 */
class HoldColour(first: Blob) {
    val yes = arrayListOf(first)
    var tolerance = 1.0

    fun add(b: Blob) { yes.add(b) }

    fun matches(b: Blob): Boolean = yes.any { near(it, b) }

    private fun near(e: Blob, b: Blob): Boolean {
        val t = tolerance
        if (e.chromatic) {
            if (!b.chromatic) return false
            var dh = abs(e.hueDeg - b.hueDeg); if (dh > 180) dh = 360 - dh
            return dh <= HUE_TOL * t && b.chroma in e.chroma * (0.5 / t)..e.chroma * 2.0 * t && abs(b.l - e.l) <= L_TOL * t
        }
        return b.chroma < Blobs.C_MIN * 1.5 && abs(b.l - e.l) <= GREY_L_TOL * t && abs(b.l - b.ringL) >= Blobs.GREY_CONTRAST
    }

    private val mean: Triple<Double, Double, Double> get() = Triple(yes.sumOf { it.l } / yes.size, yes.sumOf { it.a } / yes.size, yes.sumOf { it.b } / yes.size)

    /** What it's called, roughly: "yellow", "blue", "black"… */
    val name: String get() = mean.let { (l, a, b) -> nameOf(l, a, b) }

    /** What to draw its holds in: its colour, made bright enough to see over a picture. */
    val rgb: Int get() = mean.let { (l, a, b) -> Picture.labToRgb(l.coerceIn(35.0, 85.0), a, b) }

    companion object {
        const val HUE_TOL = 14.0        // degrees of hue from a tapped hold's
        const val L_TOL = 35.0          // and brightness: chalk and shade change it a lot
        const val GREY_L_TOL = 12.0     // a grey hold's brightness
        val TOLERANCES = listOf(0.6, 0.8, 1.0, 1.3, 1.7)

        fun nameOf(l: Double, a: Double, b: Double): String {
            val c = hypot(a, b)
            if (c < Blobs.C_MIN) return if (l < 30) "black" else if (l > 78) "white" else "grey"
            val h = ((atan2(b, a) * 180 / PI) + 360) % 360
            return when {
                h < 20 || h >= 345 -> if (l > 62) "pink" else "red"
                h < 55 -> if (l > 70 && c < 50) "pink" else if (l < 35 && c < 30) "brown" else "red"
                h < 85 -> if (l < 45 && c < 40) "brown" else "orange"
                h < 125 -> "yellow"
                h < 175 -> "green"
                h < 225 -> "teal"
                h < 295 -> "blue"
                else -> if (l > 60) "pink" else "purple"
            }
        }
    }
}

/**
 * A gym climb's holds, found by their colour (HoldColour) in the photos of a capture: each photo's patches (Blob) of
 * every colour are kept as it's taken, put in the world where the points the phone tracks say they are; the ones of
 * the climb's colour, wherever two or more photos (or a tap) agree, are its holds, each to photograph as a marked hold
 * is. Patches of every colour are kept so that the colour can be changed, widened or narrowed at any time.
 *
 * Climbs share holds now and then (most often a start hold, the climb splitting two ways from it), and some gyms mark
 * climbs with tape rather than colour: neither is handled. A hold tapped with "Not part of it" is left out from then on.
 */
class Gym {
    /** A photo's patches: those of the colour finder (A) and, where the phone has a model, the model's (B); each with where it is, if known. */
    class Photo(val name: String, val pose: CameraPose, val spell: Int, val colour: List<Blob>, val model: List<Blob>?,
                val colourAt: List<Vec3?>, val modelAt: List<Vec3?>?)
    /** A hold of the climb: where, how far round, in how many photos it was found, and its id as a spot of the capture. */
    class Hold(val id: String, var p: Vec3, var rM: Double, var photos: Int, var picked: Boolean = false)
    private class Pick(val p: Vec3, val blob: Blob, val spell: Int)

    val photos = ArrayList<Photo>()
    var colour: HoldColour? = null
        private set
    /** Which finder's patches count: "colour" (A) or "model" (B). */
    var finder = FINDER_COLOUR
    private val picks = ArrayList<Pick>()
    private val dropped = ArrayList<Pair<Vec3, Int>>()      // holds said not to be part of it, each with its spell
    val holds = ArrayList<Hold>()
    private var nextId = 1
    var spell = 1
        private set
    /** How high the floor is (the world's y), from where the phone is held: until a photo says, from the tap. */
    var floorY: Double? = null
        private set
    private val heights = ArrayList<Double>()

    val hasModel: Boolean get() = photos.any { it.model != null }

    /**
     * Picks the climb's colour from a tap on a hold: `u`, `v` where in the picture (as stored, seen by `pose`), `world`
     * where the tap met the world. Returns the patch tapped, or null if there's none there (a tap on the wall).
     * add: one more hold of the same colour, which widens what counts as it; else the colour starts afresh.
     */
    fun pick(picture: Picture, pose: CameraPose, u: Double, v: Double, world: Vec3?, spell: Int, add: Boolean): Blob? {
        val blob = blobAt(Blobs.find(picture), picture, u, v) ?: return null
        val c = colour
        if (add && c != null) c.add(blob) else { colour = HoldColour(blob); picks.clear() }
        if (world != null) picks.add(Pick(world, blob, spell))
        if (floorY == null) floorY = pose.centre.y - EYE_M
        this.spell = spell
        refresh()
        return blob
    }

    /** The patch under (u, v), or the nearest whose box it's within a few pixels of. */
    private fun blobAt(blobs: List<Blob>, p: Picture, u: Double, v: Double): Blob? {
        val slack = 3 * p.step
        return blobs.filter { u in it.x0 - slack..it.x1 + slack && v in it.y0 - slack..it.y1 + slack }
            .minByOrNull { hypot(it.u - u, it.v - v) / max(it.radiusPx, p.step) }
    }

    /** Leaves out the hold nearest a ray from the phone (a tap), within DROP_M of it. Returns its id, or null if none is near. */
    fun drop(from: Vec3, along: Vec3): String? {
        val h = holds.minByOrNull { rayDistance(from, along, it.p) } ?: return null
        if (rayDistance(from, along, h.p) > max(DROP_M, h.rM * 1.5)) return null
        dropped.add(h.p to spell)
        refresh()
        return h.id
    }

    fun widen(): Boolean = step(1)
    fun narrow(): Boolean = step(-1)
    private fun step(by: Int): Boolean {
        val c = colour ?: return false
        val i = HoldColour.TOLERANCES.indexOfFirst { it >= c.tolerance - 1e-9 }.coerceAtLeast(0)
        val j = (i + by).coerceIn(0, HoldColour.TOLERANCES.size - 1)
        if (i == j) return false
        c.tolerance = HoldColour.TOLERANCES[j]
        refresh()
        return true
    }

    fun useFinder(f: String) { if (f != finder) { finder = f; refresh() } }

    /**
     * A photo kept: its patches, put in the world by the points the phone tracks (`cloud`, of its spell), and the
     * holds found again. `pose` is the stored picture's.
     */
    fun addPhoto(name: String, pose: CameraPose, spell: Int, colour: List<Blob>, model: List<Blob>?, cloud: Collection<Vec3>) {
        val grid = DepthGrid(pose, cloud)
        photos.add(Photo(name, pose, spell, colour, model, colour.map { grid.lift(it) }, model?.map { b -> grid.lift(b) }))
        this.spell = spell
        heights.add(pose.centre.y)
        floorY = heights.sorted()[heights.size / 2] - EYE_M
        refresh()
    }

    /** A patch of the colour in a photo: the ray from the camera through its middle, the photo, and its size in pixels. */
    private class Ray(val o: Vec3, val d: Vec3, val photo: Int, val fx: Double, val rPx: Double)
    private class Group(val p: Vec3, val photos: Int, val rM: Double, val picked: Boolean)

    /**
     * The photos' patches of the climb's colour, as places in the world, grouped into holds: those of the spell now.
     * Each patch's ray is a line it lies on, to a centimetre or two; how far along it is from the tracked points inside
     * it is far rougher. So a guess at where a hold is (from those points, or where two rays meet) is only a start:
     * the rays of the photos that pass near it (one each, the nearest) then put it where they all meet most nearly, a
     * few times over, closing in. A hold needs three photos' rays, or two that agree with the tracked points.
     */
    fun refresh() {
        val c = colour ?: run { holds.clear(); return }
        val rays = ArrayList<Ray>()
        val seeds = ArrayList<Pair<Vec3, Boolean>>()        // guesses, and whether from tracked points (else rays meeting)
        val bare = ArrayList<Ray>()
        for ((k, ph) in photos.withIndex()) {
            if (ph.spell != spell) continue
            val (blobs, at) = if (finder == FINDER_MODEL && ph.model != null) ph.model to ph.modelAt!! else ph.colour to ph.colourAt
            for ((i, b) in blobs.withIndex()) {
                if (!c.matches(b)) continue
                val r = Ray(ph.pose.centre, rayOf(ph.pose, b.u, b.v), k, ph.pose.lens.fx, b.radiusPx)
                rays.add(r)
                val p = at[i]
                if (p != null) seeds.add(p to true) else if (bare.size < RAYS_MOST) bare.add(r)
            }
        }
        for (i in bare.indices) for (j in i + 1 until bare.size) {
            if (bare[i].photo == bare[j].photo || angleDeg(bare[i].d, bare[j].d) < MEET_DEG) continue
            meet(bare[i].o, bare[i].d, bare[j].o, bare[j].d)?.let { seeds.add(it to false) }
        }
        val picked = picks.filter { it.spell == spell }.map { it.p }
        val groups = ArrayList<Group>()
        fun taken(p: Vec3) = groups.any { it.p.distance(p) < max(JOIN_M, it.rM) }
        for (p in picked) refine(p, rays)?.let { g -> groups.add(Group(if (g.p.distance(p) < JOIN_MOST_M) g.p else p, g.photos, g.rM, true)) }
            ?: groups.add(Group(p, 0, 0.05, true))
        for ((seed, tracked) in seeds) {
            if (taken(seed)) continue
            val g = refine(seed, rays) ?: continue
            if (g.photos < MIN_PHOTOS || (g.photos < 3 && !(tracked && g.p.distance(seed) < AGREE_M))) continue
            if (g.photos < SEEN_SHARE * inView(g.p)) continue       // rays of different holds of the colour meeting by chance
            if (!taken(g.p)) groups.add(g)
        }
        val found = groups.filter { g -> dropped.none { (p, s) -> s == spell && p.distance(g.p) <= max(DROP_M, g.rM) } }
        // the same ids as before for the same holds, so that what their photos so far gave them stays
        val was = ArrayList(holds)
        holds.clear()
        for (g in found.sortedByDescending { it.photos }) {
            val old = was.minByOrNull { it.p.distance(g.p) }?.takeIf { it.p.distance(g.p) <= KEEP_ID_M }
            if (old != null) was.remove(old)
            val h = old ?: Hold("g${nextId++}", g.p, g.rM, 0)
            h.p = g.p; h.rM = g.rM.coerceIn(MIN_R_M, MAX_R_M); h.photos = g.photos; h.picked = g.picked
            holds.add(h)
        }
    }

    /** How many of the photos of the spell now have `p` in view: a hold's, if it's there, should be found in most of them. */
    private fun inView(p: Vec3): Int = photos.count { ph ->
        if (ph.spell != spell) return@count false
        val at = ph.pose.project(p) ?: return@count false
        val mx = ph.pose.lens.w * VIEW_MARGIN; val my = ph.pose.lens.h * VIEW_MARGIN
        at.depth in 0.2..15.0 && at.u in mx..ph.pose.lens.w - mx && at.v in my..ph.pose.lens.h - my
    }

    /**
     * Where the rays near `seed` meet most nearly: each photo's nearest ray within reach, the point that's nearest them
     * all, again with the reach narrowed. Null if fewer than two photos' rays come near, or they're too nearly one way.
     */
    private fun refine(seed: Vec3, rays: List<Ray>): Group? {
        var p = seed
        var tol = REFINE_FIRST_M
        var support: List<Ray> = emptyList()
        repeat(REFINE_ROUNDS) {
            support = rays.groupBy { it.photo }.values.mapNotNull { rs -> rs.minByOrNull { rayDistance(it.o, it.d, p) }?.takeIf { rayDistance(it.o, it.d, p) <= tol + MEET_PER_M * it.o.distance(p) } }
            if (support.size < 2) return null
            p = nearest(support) ?: return null
            tol = max(REFINE_LAST_M, tol * 0.5)
        }
        val sizes = support.map { it.rPx / it.fx * it.o.distance(p) }.sorted()
        return Group(p, support.size, sizes[sizes.size / 2], false)
    }

    /** The point nearest all these rays (least squares), or null if they're too nearly parallel to say. */
    private fun nearest(rays: List<Ray>): Vec3? {
        val a = DoubleArray(9); val b = DoubleArray(3)
        for (r in rays) {
            val d = doubleArrayOf(r.d.x, r.d.y, r.d.z); val o = doubleArrayOf(r.o.x, r.o.y, r.o.z)
            for (i in 0..2) for (j in 0..2) {
                val m = (if (i == j) 1.0 else 0.0) - d[i] * d[j]
                a[3 * i + j] += m; b[i] += m * o[j]
            }
        }
        val det = a[0] * (a[4] * a[8] - a[5] * a[7]) - a[1] * (a[3] * a[8] - a[5] * a[6]) + a[2] * (a[3] * a[7] - a[4] * a[6])
        if (abs(det) < 1e-6 * rays.size * rays.size * rays.size) return null
        fun col(k: Int) = DoubleArray(9) { idx -> if (idx % 3 == k) b[idx / 3] else a[idx] }
        fun det3(m: DoubleArray) = m[0] * (m[4] * m[8] - m[5] * m[7]) - m[1] * (m[3] * m[8] - m[5] * m[6]) + m[2] * (m[3] * m[7] - m[4] * m[6])
        return Vec3(det3(col(0)) / det, det3(col(1)) / det, det3(col(2)) / det)
    }

    /** Where two rays come nearest each other, if that's ahead of both and they pass within MEET_M of each other there. */
    private fun meet(o1: Vec3, d1: Vec3, o2: Vec3, d2: Vec3): Vec3? {
        val w = o1 - o2
        val b = d1 dot d2; val d = d1 dot w; val e = d2 dot w
        val den = 1 - b * b
        if (den < 1e-9) return null
        val s = (b * e - d) / den; val t = (e - b * d) / den
        if (s < 0.2 || t < 0.2) return null
        val p1 = o1 + d1 * s; val p2 = o2 + d2 * t
        return if (p1.distance(p2) <= MEET_M + MEET_PER_M * s) (p1 + p2) / 2.0 else null
    }

    /**
     * What a photo of hold at `p` can at best see it at, from the ground: from no nearer than STANDOFF_M, held no
     * higher than REACH_M above the floor, by a phone with focal length `fx` (pixels). A hold within reach is asked
     * for as finely as a marked one is; one higher up for the best a phone held up can do.
     */
    fun bestGsdM(p: Vec3, fx: Double): Double {
        val reach = (floorY ?: (p.y - REACH_M)) + REACH_M
        val above = max(0.0, p.y - reach)
        return max(Orbit.SPOT_GSD_M, hypot(above, STANDOFF_M) / fx * BEST_SLACK)
    }

    /** Whether a hold is out of reach from the ground: higher than a phone held up. */
    fun high(p: Vec3): Boolean = floorY?.let { p.y > it + REACH_M + 0.2 } ?: false

    /** What it's sent as, with the capture's record. */
    fun record(): RecordGym? {
        val c = colour ?: return null
        return RecordGym(c.name, hex(c.rgb), c.tolerance, finder,
            c.yes.map { listOf(it.l, it.a, it.b).map { x -> kotlin.math.round(x * 10) / 10 } },
            holds.map { RecordGymHold(it.id, it.p.toList(), it.rM, it.photos, if (it.picked) 1 else null) },
            photos.sumOf { it.colour.size }, photos.sumOf { it.model?.size ?: 0 })
    }

    companion object {
        const val FINDER_COLOUR = "colour"
        const val FINDER_MODEL = "model"
        const val MIN_PHOTOS = 2        // a hold found in fewer photos (and not tapped) isn't one: one photo alone may be a passing patch
        const val JOIN_M = 0.06         // holds placed this near each other (or within the first's size) are one: a big hold's middle moves with the view
        const val JOIN_MOST_M = 0.2     // a tapped hold the photos put further than this from the tap stays where tapped
        const val KEEP_ID_M = 0.12      // a hold found again within this of where it was is the same one
        const val DROP_M = 0.15         // a tap within this of a hold leaves it out
        const val MIN_R_M = 0.04; const val MAX_R_M = 0.3
        const val EYE_M = 1.45          // how far above the floor a phone is held to photograph
        const val REACH_M = 2.1         // and how high it can be held up
        const val STANDOFF_M = 0.35     // the nearest it can be held to a hold out of reach, below it and out from the wall
        const val BEST_SLACK = 1.15
        const val MEET_DEG = 3.0        // rays closer in direction than this say nothing of how far off a patch is
        const val MEET_M = 0.03; const val MEET_PER_M = 0.015
        const val RAYS_MOST = 400
        const val AGREE_M = 0.15        // a hold seen in two photos only is one if they put it this near where the tracked points do
        const val REFINE_FIRST_M = 0.12 // rays this near a guess (and a little more for each metre off) are first taken as of its hold
        const val REFINE_LAST_M = 0.03  // and this near, last
        const val REFINE_ROUNDS = 4
        const val SEEN_SHARE = 0.4      // a hold is found in at least this share of the photos it's in view of (on a real gym's photos,
                                        // holds were in 0.5-1 of them and chance meetings of other holds' rays in 0.05-0.3)
        const val VIEW_MARGIN = 0.03

        /** The ray from a camera through a pixel of its picture, in the world. */
        fun rayOf(pose: CameraPose, u: Double, v: Double): Vec3 =
            pose.rot.rotate(Vec3((u - pose.lens.cx) / pose.lens.fx, (v - pose.lens.cy) / pose.lens.fy, 1.0)).unit()

        fun rayDistance(from: Vec3, along: Vec3, p: Vec3): Double {
            val t = max(0.0, (p - from) dot along)
            return (from + along * t).distance(p)
        }
    }

    /** The points the phone tracks, as a photo sees them: how far off each part of it is. */
    private class DepthGrid(val pose: CameraPose, cloud: Collection<Vec3>) {
        private val cell = max(8.0, pose.lens.w / 64.0)
        private val nx = (pose.lens.w / cell).toInt() + 1; private val ny = (pose.lens.h / cell).toInt() + 1
        private val depths = HashMap<Int, ArrayList<Double>>()
        init {
            for (p in cloud) {
                val at = pose.project(p) ?: continue
                if (at.depth !in 0.1..15.0 || at.u < 0 || at.v < 0 || at.u >= pose.lens.w || at.v >= pose.lens.h) continue
                depths.getOrPut((at.v / cell).toInt() * nx + (at.u / cell).toInt()) { ArrayList() }.add(at.depth)
            }
        }

        /**
         * Where a patch is in the world: along the ray through its middle, as far off as the nearest of the points
         * inside it (a hold stands out from the wall behind it); null with none there.
         */
        fun lift(b: Blob): Vec3? {
            val sx = (b.x1 - b.x0) * 0.2; val sy = (b.y1 - b.y0) * 0.2
            val cx0 = ((b.x0 + sx) / cell).toInt().coerceIn(0, nx - 1); val cx1 = ((b.x1 - sx) / cell).toInt().coerceIn(cx0, nx - 1)
            val cy0 = ((b.y0 + sy) / cell).toInt().coerceIn(0, ny - 1); val cy1 = ((b.y1 - sy) / cell).toInt().coerceIn(cy0, ny - 1)
            val got = ArrayList<Double>()
            for (y in cy0..cy1) for (x in cx0..cx1) depths[y * nx + x]?.let { got.addAll(it) }
            if (got.isEmpty()) return null
            got.sort()
            val near = got.filter { it <= got[0] + NEAR_M }
            val d = near[near.size / 2]
            val l = pose.lens
            return pose.centre + pose.rot.rotate(Vec3((b.u - l.cx) / l.fx * d, (b.v - l.cy) / l.fy * d, d))
        }

        companion object { const val NEAR_M = 0.2 }
    }
}

/** 0xRRGGBB as "#rrggbb". */
fun hex(rgb: Int): String = "#" + (0..2).joinToString("") { ((rgb shr (16 - 8 * it)) and 0xff).toString(16).padStart(2, '0') }
