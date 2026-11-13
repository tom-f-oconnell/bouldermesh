package sh.boulderme.core

import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.max
import kotlin.math.sqrt

/** Readings turned into the axes of a stored photo: x along its rows, y down its columns, z the way the camera looks. */
object Axes {
    /** Which way is down in a photo taken from a tracked pose (the tracking's world has +Y up). */
    fun downInImage(pose: CameraPose): Vec3 = pose.rot.conj().rotate(Vec3(0.0, -1.0, 0.0)).unit()

    /**
     * Which way is down in a photo from the accelerometer as it was taken (the phone's axes; it reads upwards at rest).
     *
     * @param sensorOrientation how the back camera's sensor is mounted: 90 on iPhones and nearly every Android phone
     *   (its rows run down the phone), 270 on a few.
     */
    fun downFromAccel(accel: Vec3, sensorOrientation: Int = 90): Vec3 {
        val d = -accel.unit()
        return if (sensorOrientation == 270) Vec3(d.y, d.x, -d.z) else Vec3(-d.y, -d.x, -d.z)
    }

    /**
     * The EXIF Orientation that shows a photo upright: 1 as stored, 6 turned a quarter clockwise, 3 a half, 8 three
     * quarters; from which way is down in its stored pixels, or from how the screen showed it (uprightDeg) if the camera
     * was pointing too nearly straight up or down for that to say.
     */
    fun exifOrientation(down: Vec3?, uprightDeg: Int): Int {
        val d = down?.unit()
        val turn = if (d == null || abs(d.z) > 0.9) ((uprightDeg % 360) + 360) % 360
            else if (abs(d.y) >= abs(d.x)) (if (d.y > 0) 0 else 180) else (if (d.x > 0) 90 else 270)      // down along its rows: on its side
        return when (turn) { 90 -> 6; 180 -> 3; 270 -> 8; else -> 1 }
    }

    /**
     * The compass bearing the back camera points along, from how the phone is turned in a frame of east, north and up;
     * null when it points too nearly straight up or down to say.
     */
    fun headingOfView(phoneToEnu: Quat, declinationDeg: Double = 0.0): Double? {
        val f = phoneToEnu.rotate(Vec3(0.0, 0.0, -1.0))
        if (abs(f.z) > 0.94) return null
        return ((bearingDeg(f.x, f.y) + declinationDeg) % 360 + 360) % 360
    }

    /** The same, for a frame of north, west and up (as an iPhone's attitude with x to true north has). */
    fun headingOfViewNwu(phoneToNwu: Quat): Double? = headingOfNwu(phoneToNwu.rotate(Vec3(0.0, 0.0, -1.0)))

    /** The compass bearing of a direction given as north, west and up; null when it's too nearly straight up or down to say. */
    fun headingOfNwu(f: Vec3): Double? {
        val u = f.unit()
        if (abs(u.z) > 0.94) return null
        return bearingDeg(-u.y, u.x)
    }
}

/**
 * The accelerometer's offsets, from three readings with the phone at rest: flat on its back; the same after half a
 * turn on the spot; and flipped onto its screen. The half turn cancels the surface's slope, so it needn't be level.
 */
class AccelCalibration {
    var flat: Vec3? = null
    var turned: Vec3? = null
    var flipped: Vec3? = null

    /** The offsets (m/s2, the phone's axes) to take from every reading, once all three are in and they make sense. */
    fun bias(): Vec3? {
        val a = flat ?: return null; val b = turned ?: return null; val c = flipped ?: return null
        if (a.z < 0.8 * Sensors.G || b.z < 0.8 * Sensors.G || c.z > -0.8 * Sensors.G) return null       // not flat, or not flipped
        val out = Vec3((a.x + b.x) / 2, (a.y + b.y) / 2, (a.z + c.z) / 2)
        return if (out.norm < MAX_BIAS) out else null
    }

    /** How many degrees the offsets would have put "down" off by, with the phone upright: what calibrating gains. */
    fun gainDeg(): Double? = bias()?.let { kotlin.math.atan2(hypot2(it.x, it.z), Sensors.G) * 180 / kotlin.math.PI }

    private fun hypot2(a: Double, b: Double) = sqrt(a * a + b * b)

    companion object { const val MAX_BIAS = 1.5 }
}

/** The mean of readings taken while the phone was at rest, and whether it was. */
class StillMean {
    private var sum = Vec3.ZERO
    private var n = 0
    private var spread = 0.0
    private var last: Vec3? = null

    fun add(v: Vec3) {
        last?.let { spread = max(spread, v.distance(it)) }
        last = v; sum += v; n++
    }

    val count: Int get() = n
    fun mean(): Vec3? = if (n > 0) sum / n.toDouble() else null
    fun still(limit: Double = 0.25): Boolean = n >= 10 && spread <= limit
    fun reset() { sum = Vec3.ZERO; n = 0; spread = 0.0; last = null }
}

/**
 * The magnetometer's offset (what the phone itself adds to the Earth's field), from readings taken while it's turned
 * every which way: they lie on a sphere whose middle is the offset and whose radius is the field's strength.
 */
class MagCalibration {
    private val samples = ArrayList<Vec3>()

    fun add(v: Vec3) { if (samples.isEmpty() || samples.last().distance(v) > 1.0) samples.add(v) }
    val count: Int get() = samples.size

    data class Result(val offset: Vec3, val fieldUt: Double, val rmsUt: Double, val covered: Double)

    /** From 0 to 1: how much of every which way it's been turned so far (the share of 26 directions it has readings near). */
    fun covered(): Double {
        if (samples.size < 8) return 0.0
        val mid = Registration.mean(samples)
        val seen = HashSet<Int>()
        for (s in samples) {
            val d = (s - mid).unit()
            seen.add(bucket(d.x) * 9 + bucket(d.y) * 3 + bucket(d.z))
        }
        seen.remove(13)
        return seen.size / 26.0
    }

    private fun bucket(x: Double) = if (x < -0.4) 0 else if (x > 0.4) 2 else 1

    /** The fit, or null with too few readings or ones that don't lie on a sphere. */
    fun solve(): Result? {
        if (samples.size < 20) return null
        // |m - c|^2 = r^2  =>  2 c.m + (r^2 - |c|^2) = |m|^2, linear in c and k = r^2 - |c|^2
        val ata = Array(4) { DoubleArray(4) }; val atb = DoubleArray(4)
        for (m in samples) {
            val row = doubleArrayOf(2 * m.x, 2 * m.y, 2 * m.z, 1.0); val rhs = m dot m
            for (i in 0..3) { atb[i] += row[i] * rhs; for (j in 0..3) ata[i][j] += row[i] * row[j] }
        }
        val x = solveLinear(ata, atb) ?: return null
        val c = Vec3(x[0], x[1], x[2])
        val r2 = x[3] + (c dot c)
        if (r2 <= 0) return null
        val r = sqrt(r2)
        val rms = sqrt(samples.sumOf { val e = it.distance(c) - r; e * e } / samples.size)
        return Result(c, r, rms, covered())
    }

    companion object {
        /** Gaussian elimination with pivoting; null if the equations don't pin the answer down. */
        internal fun solveLinear(a: Array<DoubleArray>, b: DoubleArray): DoubleArray? {
            val n = b.size
            val m = Array(n) { i -> DoubleArray(n + 1) { j -> if (j < n) a[i][j] else b[i] } }
            for (col in 0 until n) {
                var piv = col
                for (r in col + 1 until n) if (abs(m[r][col]) > abs(m[piv][col])) piv = r
                if (abs(m[piv][col]) < 1e-9) return null
                val t = m[col]; m[col] = m[piv]; m[piv] = t
                for (r in 0 until n) if (r != col) {
                    val f = m[r][col] / m[col][col]
                    for (k in col..n) m[r][k] -= f * m[col][k]
                }
            }
            return DoubleArray(n) { m[it][n] / m[it][it] }
        }
    }
}

/**
 * One place from many GPS fixes taken standing still in the open: each weighed by how good it says it is, those far
 * from the rest left out.
 */
class GpsAverager {
    private val fixes = ArrayList<GpsFix>()

    fun add(f: GpsFix) { if (f.accM in 0.0..WORST_M) fixes.add(f) }
    val count: Int get() = fixes.size
    val seconds: Double get() = if (fixes.size < 2) 0.0 else fixes.last().t - fixes.first().t

    data class Result(val lat: Double, val lng: Double, val alt: Double?, val accM: Double, val n: Int, val seconds: Double)

    fun result(): Result? {
        if (fixes.isEmpty()) return null
        var keep: List<GpsFix> = fixes
        var lat = 0.0; var lng = 0.0
        repeat(3) {
            val w = keep.sumOf { 1 / (it.accM * it.accM) }
            lat = keep.sumOf { it.lat / (it.accM * it.accM) } / w
            lng = keep.sumOf { it.lng / (it.accM * it.accM) } / w
            val off = keep.map { metresApart(lat, lng, it.lat, it.lng) }
            val limit = max(3.0, 3 * 1.4826 * off.sorted()[off.size / 2])
            val next = keep.filterIndexed { i, _ -> off[i] <= limit }
            if (next.size == keep.size || next.size < 3) return@repeat
            keep = next
        }
        val alts = keep.mapNotNull { it.alt }
        // fixes a few seconds apart share most of what's wrong with them: every 10 s counts as one more
        val independent = max(1.0, 1 + (keep.last().t - keep.first().t) / 10.0)
        val typical = keep.map { it.accM }.sorted()[keep.size / 2]
        return Result(lat, lng, if (alts.isEmpty()) null else alts.average(), typical / sqrt(independent), keep.size, seconds)
    }

    companion object {
        const val WORST_M = 30.0
        fun metresApart(lat1: Double, lng1: Double, lat2: Double, lng2: Double): Double {
            val r = 6378137.0
            val dn = (lat2 - lat1) * kotlin.math.PI / 180 * r
            val de = (lng2 - lng1) * kotlin.math.PI / 180 * r * cos(lat1 * kotlin.math.PI / 180)
            return sqrt(dn * dn + de * de)
        }
    }
}
