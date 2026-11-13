package sh.boulderme.core

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * What a photo's pixels say of it: how sharp it is, and how much of it is blown out or black. `sharp` is the detail
 * from one pixel to the next in the middle of the picture, against the detail at a quarter of its size, across it and
 * down it, whichever is less (a phone's motion smears one way only). A blur of a few pixels takes most of the first
 * and little of the second, so it falls severalfold; what it is for a sharp photo depends on the lens and on what's in
 * the picture (more for fine texture than for plain edges), which is why a capture's own photos say what's usual.
 */
data class ImageStats(val sharp: Double, val detail: Double, val clipHigh: Double, val clipLow: Double, val mean: Double)

object Quality {
    const val BLUR_PX = 1.5           // more smear than this from the phone's own turning and moving: taken again
    const val SHARP_LEAST = 0.10      // less fine detail than this, of a photo with detail to see: blurred or out of focus
    const val SHARP_OF_USUAL = 0.5    // or, once a capture has shown what sharp is on this phone, less than this share of that
    const val SHARP_FLOOR = 0.05      // and never asked to be less than this
    const val COARSE = 4              // the size detail is compared against: this many pixels to one
    const val DETAIL_LEAST = 0.6      // less detail than this at a quarter of the size: blank (sky, a blank wall), sharpness unknown
    const val CLIP_HIGH = 0.05        // more of it blown out than this: over-exposed
    const val CLIP_LOW = 0.25         // more of it black than this: under-exposed
    const val CROP = 1024             // the middle of the photo looked at, at full size, pixels a side

    /**
     * A photo's stats from its brightness alone, one byte a pixel, `stride` bytes a row.
     * Sharpness is from its middle CROP pixels square, at full size; clipping from every 8th pixel of all of it.
     */
    fun imageStats(luma: ByteArray, width: Int, height: Int, stride: Int = width): ImageStats {
        val side = min(CROP, min(width, height)) / COARSE * COARSE
        val x0 = (width - side) / 2; val y0 = (height - side) / 2
        fun px(x: Int, y: Int) = luma[y * stride + x].toInt() and 0xff
        var fineX = 0.0; var fineY = 0.0; var nf = 0
        for (y in y0 + 1 until y0 + side - 1) for (x in x0 + 1 until x0 + side - 1) {
            val p = 2 * px(x, y)
            fineX += abs(p - px(x - 1, y) - px(x + 1, y)); fineY += abs(p - px(x, y - 1) - px(x, y + 1)); nf++
        }
        // the same, of the picture at a quarter of the size (each pixel the mean of sixteen)
        val n4 = side / COARSE
        val small = IntArray(n4 * n4)
        for (j in 0 until n4) for (i in 0 until n4) {
            var sum = 0
            for (dy in 0 until COARSE) for (dx in 0 until COARSE) sum += px(x0 + COARSE * i + dx, y0 + COARSE * j + dy)
            small[j * n4 + i] = sum          // sixteen times its brightness: kept whole, divided below
        }
        var coarseX = 0.0; var coarseY = 0.0; var nc = 0
        for (j in 1 until n4 - 1) for (i in 1 until n4 - 1) {
            val p = 2 * small[j * n4 + i]
            coarseX += abs(p - small[j * n4 + i - 1] - small[j * n4 + i + 1]); coarseY += abs(p - small[(j - 1) * n4 + i] - small[(j + 1) * n4 + i]); nc++
        }
        val per = (COARSE * COARSE).toDouble()
        val cx = if (nc > 0) coarseX / nc / per else 0.0; val cy = if (nc > 0) coarseY / nc / per else 0.0
        val across = if (cx > 1e-6) fineX / nf / cx else 0.0
        val down = if (cy > 1e-6) fineY / nf / cy else 0.0
        var hi = 0; var lo = 0; var n = 0; var sum = 0L
        var y = 0
        while (y < height) {
            var x = 0
            while (x < width) {
                val v = px(x, y)
                if (v >= 250) hi++ else if (v <= 5) lo++
                sum += v; n++; x += 8
            }
            y += 8
        }
        return ImageStats(min(across, down), (cx + cy) / 2, hi.toDouble() / max(1, n), lo.toDouble() / max(1, n), sum.toDouble() / max(1, n))
    }

    /**
     * How many pixels a photo is smeared by the phone's own motion while its shutter was open: its turning (radians a
     * second) and its moving (metres a second, across a subject `distanceM` away).
     */
    fun blurPx(gyroRadS: Double, speedMS: Double, exposureS: Double, fx: Double, distanceM: Double?): Double =
        gyroRadS * exposureS * fx + if (distanceM != null && distanceM > 0.05) speedMS * exposureS / distanceM * fx else 0.0

    /** The verdict on a photo: whether to keep it, and if not, why, in words for the person holding the phone. */
    data class Verdict(val ok: Boolean, val blurPx: Double, val reasons: List<String>)

    /**
     * @param usualSharp how sharp this capture's good photos have been, if there are enough to say: one well under that
     *   is soft, whatever the lens and the phone's own sharpening make of "sharp".
     */
    fun judge(stats: ImageStats?, blurPx: Double, tracking: Boolean = true, usualSharp: Double? = null): Verdict {
        val why = ArrayList<String>()
        if (blurPx > BLUR_PX) why.add("the phone was moving (about ${(blurPx * 10).toInt() / 10.0} pixels of blur)")
        if (stats != null) {
            val least = usualSharp?.let { max(SHARP_FLOOR, SHARP_OF_USUAL * it) } ?: SHARP_LEAST
            if (stats.detail >= DETAIL_LEAST && stats.sharp < least) why.add("it came out soft: out of focus, or blurred")
            if (stats.clipHigh > CLIP_HIGH) why.add("too bright: ${(stats.clipHigh * 100).toInt()}% of it is blown out")
            if (stats.clipLow > CLIP_LOW) why.add("too dark: ${(stats.clipLow * 100).toInt()}% of it is black")
        }
        if (!tracking) why.add("the phone had lost track of where it was")
        return Verdict(why.isEmpty(), blurPx, why)
    }
}

/** A GPS fix: where, how far off it says it may be (metres), and when (seconds, any clock that only goes forward). */
data class GpsFix(val lat: Double, val lng: Double, val alt: Double?, val accM: Double, val t: Double)

/**
 * The phone's sensors as a photo was taken, in the phone's own axes (x to its right, y to its top, z out of its
 * screen): accel what an accelerometer reads at rest (9.81 m/s2 upwards), gyro in radians a second, mag in microtesla.
 */
data class SensorSample(
    val accel: Vec3? = null, val gyro: Vec3? = null, val mag: Vec3? = null, val magAccuracy: Int? = null,
    val headingDeg: Double? = null, val headingAccDeg: Double? = null, val gps: GpsFix? = null, val t: Double = 0.0,
)

/** How far each reading taken with a photo can be trusted. */
data class SensorGrades(val gravityOk: Boolean, val headingOk: Boolean, val gpsOk: Boolean, val notes: List<String>)

object Sensors {
    const val G = 9.80665
    const val STILL_ACCEL = 0.35        // m/s2 from g: more, and the phone was being moved, so "down" is off
    const val STILL_GYRO = 0.35         // rad/s
    const val FIELD_MIN = 22.0; const val FIELD_MAX = 67.0     // microtesla: the Earth's field is within these everywhere
    const val FIELD_OFF = 0.15          // of this capture's usual field: further off, and something magnetic is near
    const val GPS_OK_M = 8.0
    const val GPS_FRESH_S = 3.0

    /**
     * @param usualFieldUt the middle field strength of this capture's readings so far, if there are enough.
     * @param now the same clock as the sample's and its fix's.
     */
    fun grade(s: SensorSample, usualFieldUt: Double? = null, now: Double = s.t): SensorGrades {
        val notes = ArrayList<String>()
        val still = (s.accel?.let { abs(it.norm - G) <= STILL_ACCEL } ?: false) && (s.gyro?.let { it.norm <= STILL_GYRO } ?: true)
        if (s.accel != null && !still) notes.add("gravity: the phone was moving")
        var heading = s.headingDeg != null
        s.mag?.norm?.let { f ->
            if (f < FIELD_MIN || f > FIELD_MAX) { heading = false; notes.add("compass: field of ${f.toInt()} µT isn't the Earth's alone") }
            else if (usualFieldUt != null && abs(f - usualFieldUt) > FIELD_OFF * usualFieldUt) { heading = false; notes.add("compass: something magnetic is near") }
        }
        if (s.magAccuracy != null && s.magAccuracy < 2) { heading = false; notes.add("compass: needs calibrating (wave the phone in a figure of 8)") }
        if (s.headingAccDeg != null && s.headingAccDeg > 25) { heading = false; notes.add("compass: unsure by ${s.headingAccDeg.toInt()}°") }
        val gps = s.gps?.let { it.accM <= GPS_OK_M && now - it.t <= GPS_FRESH_S } ?: false
        if (s.gps == null) notes.add("GPS: no fix") else if (!gps) notes.add(if (s.gps.accM > GPS_OK_M) "GPS: only good to ${s.gps.accM.toInt()} m" else "GPS: fix is ${(now - s.gps.t).toInt()} s old")
        return SensorGrades(still, heading, gps, notes)
    }
}
