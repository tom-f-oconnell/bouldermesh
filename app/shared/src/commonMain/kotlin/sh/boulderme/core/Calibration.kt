package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/** Readings turned into the axes of a stored photo: x along its rows, y down its columns, z the way the camera looks. */
object Axes {
    /**
     * Which way is down in a photo taken from a tracked pose.
     *
     * @param worldDown down in the tracking's world: its own -Y, unless a turn on the spot has found better (Level).
     */
    fun downInImage(pose: CameraPose, worldDown: Vec3 = Vec3(0.0, -1.0, 0.0)): Vec3 = pose.rot.conj().rotate(worldDown).unit()

    /**
     * What the accelerometer reads (the phone's axes: x to its right, y to its top, z out of its screen; at rest, 9.81
     * m/s2 upwards) in a photo's axes.
     *
     * @param sensorOrientation how the back camera's sensor is mounted: 90 on iPhones and nearly every Android phone
     *   (its rows run down the phone), 270 on a few.
     */
    fun forceInImage(accel: Vec3, sensorOrientation: Int = 90): Vec3 =
        if (sensorOrientation == 270) Vec3(accel.y, accel.x, -accel.z) else Vec3(-accel.y, -accel.x, -accel.z)

    /** Which way is down in a photo from the accelerometer as it was taken, by that reading alone. */
    fun downFromAccel(accel: Vec3, sensorOrientation: Int = 90): Vec3 = -forceInImage(accel, sensorOrientation).unit()

    /**
     * How far clockwise (0, 90, 180 or 270 degrees) to turn the sensor's picture to store it upright: by which way is
     * down in it, or by how the screen showed it (screenDeg) if the camera was pointing too nearly straight up or down
     * for that to say. Photos are stored upright so that everything that reads them, the networks trained on upright
     * pictures among them, sees what the reconstruction sees, whatever it makes of an EXIF Orientation.
     */
    fun uprightTurn(down: Vec3?, screenDeg: Int): Int {
        val d = down?.unit()
        return if (d == null || abs(d.z) > 0.9) ((screenDeg % 360) + 360) % 360
            else if (abs(d.y) >= abs(d.x)) (if (d.y > 0) 0 else 180) else (if (d.x > 0) 90 else 270)      // down along its rows: on its side
    }

    /** A direction in a picture's axes, in the axes of the same picture turned clockwise by `deg`. */
    fun turn(v: Vec3, deg: Int): Vec3 = when (((deg % 360) + 360) % 360) {
        90 -> Vec3(-v.y, v.x, v.z)
        180 -> Vec3(-v.x, -v.y, v.z)
        270 -> Vec3(v.y, -v.x, v.z)
        else -> v
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
 * Which way is truly down in the world the phone's tracking keeps, as a turn on the spot found it (LevelTurn). It holds
 * only in that world: the spell of tracking the turn was in (a world the tracking starts afresh, after losing its place
 * or on another visit, has a down of its own).
 *
 * @param down a unit vector in the tracking's world.
 * @param tiltDeg how far the tracking's own down (-Y) was from it.
 * @param offset what the phone adds of its own to its accelerometer's reading, in the axes of the camera's picture as its
 *   sensor reads it out (before any turn to store it upright; m/s2): the sensor's zero error and how it sits against
 *   the camera, as far as the turn could tell them from gravity.
 * @param spreadDeg how far the directions faced still disagree with that taken out: how good the answer is.
 * @param gravity the strength of gravity the fit found (m/s2), and forceMean what the accelerometer read on average:
 *   the two differ only by the phone's own errors.
 * @param axisTiltDeg how far the axis the phone was turned about leaned from down. The part of the phone's offset along
 *   that axis can't be told from gravity, so a leaning turn leaves some of it in down (LevelTurn.expectedDeg()).
 * @param expectedDeg how far off down likely is (9 times in 10), from the spread and the lean.
 */
data class Level(val down: Vec3, val tiltDeg: Double, val offset: Vec3, val spreadDeg: Double, val sectors: Int, val samples: Int,
                 val gravity: Double = Sensors.G, val forceMean: Double = Sensors.G, val axisTiltDeg: Double = 0.0, val expectedDeg: Double = 0.0) {
    /** How far off one accelerometer reading alone would have put down, in degrees. */
    val offsetDeg: Double get() = atan2(offset.norm, Sensors.G) * 180 / PI
}

/**
 * Everything the fit of a turn found, whether or not it was taken: for the log, and for saying how well a turn would do.
 * `why`: null if it gave an answer; else "few" (fewer directions read than asked for), "singular" (the readings don't
 * pin it down) or "not_gravity" (the strength it found is too far from what the accelerometer read).
 */
data class LevelFit(val why: String?, val sectors: Int, val samples: Int, val gravity: Double?, val forceMean: Double?, val spreadDeg: Double?,
                    val tiltDeg: Double?, val offset: Vec3?, val offsetDeg: Double?, val axisTiltDeg: Double?, val expectedDeg: Double?, val down: Vec3?) {
    /** As fields of a log line. */
    fun fields(): Array<Pair<String, Any?>> = arrayOf("why" to why, "sectors" to sectors, "samples" to samples, "gravity" to gravity, "force_mean" to forceMean,
        "spread_deg" to spreadDeg, "tilt_deg" to tiltDeg, "offset" to offset?.toList(), "offset_deg" to offsetDeg, "axis_tilt_deg" to axisTiltDeg,
        "expected_deg" to expectedDeg, "down" to down?.toList())
}

/**
 * Finding which way is down by reversal, as a spirit level is checked by turning it end for end. One accelerometer
 * reading is off by the sensor's own zero error and by how it's mounted against the camera, each a degree or so and
 * fixed to the phone; gravity is fixed to the world. So with the phone held as for a photo and turned all the way round
 * on the spot, the phone's errors point every way in turn and gravity one way: fitted together over the turn (the
 * tracking says how the phone was turned at each reading), they come apart. Nothing level or plumb is needed.
 *
 * Readings are kept by the direction faced, in `sectors` equal parts of the circle, each counting the same however
 * long was spent facing it. As the turn goes, check() says how well the directions so far agree, and which one most
 * spoils it; clear() lets that one alone be faced again, rather than the whole turn.
 */
class LevelTurn(val sectors: Int = 12) {
    private class Sum { var n = 0; val r = DoubleArray(9); var m = Vec3.ZERO; var f = Vec3.ZERO; var wobble = 0.0; var norm = 0.0 }
    private val sums = Array(sectors) { Sum() }
    var samples = 0
        private set

    /** How many of the directions have readings enough. */
    val filled: Int get() = sums.count { it.n >= PER_SECTOR_LEAST }

    /** Directions cleared by clear(), not yet faced again. */
    private val cleared = BooleanArray(sectors)

    /** The direction being faced now (an index of the sectors), or -1 before the first reading. */
    var facing = -1
        private set

    /** Degrees the force read now is from gravity as it's known so far, smoothed over a fraction of a second: how still the phone is. */
    var wobbleNow = 0.0
        private set
    private var wobbleSq = 0.0
    private var wobbleSum = 0.0
    private var wobbleN = 0
    private var recent: Vec3? = null        // the force in the world, smoothed over a second: gravity, until there's a fit
    private var fitted: Fit? = null         // the last check()'s fit
    private var lastT = Double.NaN

    /** The same, as the root mean square over the readings used so far. */
    val wobbleTurn: Double get() = if (wobbleN == 0) 0.0 else sqrt(wobbleSum / wobbleN)

    /** Each direction's state: 0 not faced yet, 1 being read, 2 read enough, 3 cleared for disagreeing with the rest. */
    fun states(): IntArray = IntArray(sectors) { i -> val n = sums[i].n
        if (n >= PER_SECTOR_LEAST) 2 else if (cleared[i]) 3 else if (n > 0) 1 else 0 }

    /** Each read direction's own wobble (as wobbleTurn), for the log; -1 for those not read enough. */
    fun sectorWobbles(): List<Double> = sums.map { if (it.n >= PER_SECTOR_LEAST) sqrt(it.wobble / it.n) else -1.0 }

    /**
     * A reading as the phone turns: where the tracking has the camera, and what the accelerometer reads (the phone's
     * axes, m/s2), at time `t` (seconds; NaN to take readings as 1/30 s apart). Returns whether it was used: not while
     * the camera points steeply up or down, turns fast or is jolted.
     */
    fun add(pose: CameraPose, accel: Vec3, sensorOrientation: Int = 90, gyroRadS: Double = 0.0, t: Double = Double.NaN): Boolean {
        val dt = if (t.isNaN() || lastT.isNaN()) 1.0 / 30 else (t - lastT).coerceIn(0.0, 0.2)
        lastT = t
        val ahead = pose.forward
        val force = Axes.forceInImage(accel, sensorOrientation)
        val world = pose.rot.rotate(force)
        val sector = floor(((atan2(ahead.x, ahead.z) + 2 * PI) % (2 * PI)) / (2 * PI / sectors)).toInt().coerceIn(0, sectors - 1)
        facing = sector
        // how far this reading is from gravity: as the fit so far has it, or as the last second's readings average out
        val r = recent
        recent = if (r == null) world else r + (world - r) * min(1.0, dt / RECENT_S)
        val f = fitted
        val expect = if (f != null) f.u + pose.rot.rotate(f.c) else r ?: world
        val d = angleDeg(world, expect)
        wobbleSq += (d * d - wobbleSq) * min(1.0, dt / NOW_S)
        wobbleNow = sqrt(wobbleSq)
        if (abs(ahead.y) > MAX_PITCH || gyroRadS > MAX_TURNING || abs(accel.norm - Sensors.G) > MAX_JOLT) return false
        val s = sums[sector]
        if (s.n >= PER_SECTOR_MOST) return false
        val cx = pose.rot.rotate(Vec3(1.0, 0.0, 0.0)); val cy = pose.rot.rotate(Vec3(0.0, 1.0, 0.0)); val cz = pose.rot.rotate(Vec3(0.0, 0.0, 1.0))
        val cols = arrayOf(cx, cy, cz)
        for (j in 0 until 3) { s.r[j] += cols[j].x; s.r[3 + j] += cols[j].y; s.r[6 + j] += cols[j].z }      // row-major: image axes to world
        s.m += world; s.f += force; s.wobble += d * d; s.norm += accel.norm
        s.n++; samples++
        if (s.n >= PER_SECTOR_LEAST) cleared[sector] = false
        wobbleSum += d * d; wobbleN++
        return true
    }

    /** Forgets what was read facing one direction, to be faced again. */
    fun clear(sector: Int) {
        val s = sums[sector]
        samples -= s.n
        s.n = 0; s.r.fill(0.0); s.m = Vec3.ZERO; s.f = Vec3.ZERO; s.wobble = 0.0; s.norm = 0.0
        cleared[sector] = true
    }

    private class Fit(val u: Vec3, val c: Vec3, val off: Double, val k: Int, val forceMean: Double) {
        val gravityOk: Boolean get() = abs(u.norm - forceMean) <= NOT_GRAVITY
    }

    /**
     * Each reading, turned into the world, is gravity's force u plus the phone's own offset c turned as the phone
     * was: u + R c. Least squares over the directions' means gives both. Turned about one axis, the part of c along the
     * phone's direction that stays on that axis can't be told from u along it: it's pinned at nothing (PIN), which
     * changes only gravity's strength when the axis is plumb. Left to a slight ridge, it took up whatever the hand's
     * wobble said and threw gravity's strength off, so that real turns held steadily upright came out as "not gravity".
     */
    private fun fitRaw(used: List<Sum>): Fit? {
        if (used.size < 3) return null
        val k = used.size.toDouble()
        val a = Array(6) { DoubleArray(6) }; val b = DoubleArray(6)
        for (i in 0 until 3) { a[i][i] = k; a[3 + i][3 + i] = k * (1 + RIDGE) }
        var axis = Vec3.ZERO        // the phone's own direction that the tracking's up runs along, on the whole
        var norm = 0.0
        for (s in used) {
            val n = s.n.toDouble()
            for (i in 0 until 3) for (j in 0 until 3) { a[i][3 + j] += s.r[3 * i + j] / n; a[3 + j][i] += s.r[3 * i + j] / n }
            val m = s.m / n; val f = s.f / n
            b[0] += m.x; b[1] += m.y; b[2] += m.z; b[3] += f.x; b[4] += f.y; b[5] += f.z
            axis += Vec3(s.r[3], s.r[4], s.r[5]) / n
            norm += s.norm / n
        }
        if (axis.norm > 1e-9) {
            val e = axis.unit(); val ev = doubleArrayOf(e.x, e.y, e.z)
            for (i in 0 until 3) for (j in 0 until 3) a[3 + i][3 + j] += PIN * k * ev[i] * ev[j]
        }
        val x = MagCalibration.solveLinear(a, b) ?: return null
        val u = Vec3(x[0], x[1], x[2]); val c = Vec3(x[3], x[4], x[5])
        var off = 0.0
        for (s in used) {
            val n = s.n.toDouble()
            val rc = Vec3((s.r[0] * c.x + s.r[1] * c.y + s.r[2] * c.z) / n, (s.r[3] * c.x + s.r[4] * c.y + s.r[5] * c.z) / n, (s.r[6] * c.x + s.r[7] * c.y + s.r[8] * c.z) / n)
            val e = s.m / n - u - rc
            off += e dot e
        }
        return Fit(u, c, off, used.size, norm / k)
    }

    private fun fit(used: List<Sum>): Fit? = fitRaw(used)?.takeIf { it.gravityOk }

    /**
     * How far the axis the phone was turned about leaned from `down`, in degrees: from the turn between each two
     * neighbouring directions read (numbered round the circle, so each such turn is the same way round).
     */
    private fun axisTilt(down: Vec3): Double? {
        var sum = Vec3.ZERO
        for (i in 0 until sectors) {
            val p = sums[i]; val q = sums[(i + 1) % sectors]
            if (p.n < PER_SECTOR_LEAST || q.n < PER_SECTOR_LEAST) continue
            // M = Rq Rp^T, whose skew part points along the axis turned about
            val m = Array(3) { r -> DoubleArray(3) { c -> (0 until 3).sumOf { j -> q.r[3 * r + j] / q.n * (p.r[3 * c + j] / p.n) } } }
            sum += Vec3(m[2][1] - m[1][2], m[0][2] - m[2][0], m[1][0] - m[0][1])
        }
        if (sum.norm < 1e-9) return null
        val d = angleDeg(sum, down)
        return min(d, 180 - d)
    }

    /** What the fit of the directions read so far comes to, taken or not (LevelFit), from `least` of them. */
    fun diagnose(least: Int = sectors): LevelFit {
        val used = sums.filter { it.n >= PER_SECTOR_LEAST }
        fun none(why: String) = LevelFit(why, used.size, samples, null, null, null, null, null, null, null, null, null)
        if (used.size < max(3, least)) return none("few")
        val f = fitRaw(used) ?: return none("singular")
        val down = -f.u.unit()
        val spread = atan2(sqrt(f.off / used.size), f.u.norm) * 180 / PI
        val offsetDeg = atan2(f.c.norm, Sensors.G) * 180 / PI
        val axis = axisTilt(down)
        // the offset's part along the axis was pinned at nothing, so gravity's strength took it up: how far that is from
        // standard gravity says how big it may have been (with the accelerometer's scale error, and local gravity's 0.3%)
        val hidden = atan2(kotlin.math.hypot(f.c.norm, f.u.norm - Sensors.G), Sensors.G) * 180 / PI
        return LevelFit(if (f.gravityOk) null else "not_gravity", used.size, samples, f.u.norm, f.forceMean, spread, angleDeg(down, Vec3(0.0, -1.0, 0.0)),
            f.c, offsetDeg, axis, expectedDeg(spread, hidden, axis ?: 0.0), down)
    }

    /** How far the directions of a fit disagree, as it would be expected to come out with all of them read alike. */
    private fun agreeDeg(f: Fit): Double {
        val free = 3 * f.k - FREE
        if (free <= 0) return 0.0
        return atan2(sqrt(f.off / free * (3 * sectors - FREE) / sectors), f.u.norm) * 180 / PI
    }

    /**
     * How the turn so far stands.
     *
     * @param agreeDeg how far the directions read so far disagree, scaled to what a whole turn like them would come to:
     *   comparable with Level.spreadDeg from the start.
     * @param worst the direction that disagrees most: the one whose leaving out helps most; withoutDeg, the rest's agreement then.
     */
    data class Check(val agreeDeg: Double, val sectors: Int, val worst: Int, val withoutDeg: Double)

    /** How the turn so far stands, from CHECK_LEAST directions read on; null before. */
    fun check(): Check? {
        val used = (0 until sectors).filter { sums[it].n >= PER_SECTOR_LEAST }
        if (used.size < CHECK_LEAST) return null
        val all = fit(used.map { sums[it] }) ?: return null
        fitted = all
        var worst = -1; var without = Double.MAX_VALUE
        for (i in used) {
            val f = fit(used.filter { it != i }.map { sums[it] }) ?: continue
            val a = agreeDeg(f)
            if (a < without) { without = a; worst = i }
        }
        if (worst < 0) return null
        return Check(agreeDeg(all), used.size, worst, without)
    }

    /** The answer, from the directions with readings enough; null with fewer than `least` of them, or readings that aren't gravity's. */
    fun solve(least: Int = sectors): Level? {
        val d = diagnose(least)
        if (d.why != null) return null
        return Level(d.down!!, d.tiltDeg!!, d.offset!!, d.spreadDeg!!, d.sectors, d.samples, d.gravity!!, d.forceMean!!, d.axisTiltDeg ?: 0.0, d.expectedDeg!!)
    }

    companion object {
        const val PER_SECTOR_LEAST = 10     // readings facing a direction for it to count: a third of a second's frames
        const val PER_SECTOR_MOST = 400     // and past which more add nothing
        const val MAX_PITCH = 0.8           // of the view's direction, up or down: steeper than about 50 degrees isn't "as for a photo"
        const val MAX_TURNING = 0.9         // radians a second: a turn quicker than about 7 seconds isn't read
        const val MAX_JOLT = 1.0            // m/s2 from g
        const val RIDGE = 1e-4
        const val PIN = 100.0               // how hard the offset's part along the turn's axis is held at nothing (fitRaw())
        const val NOT_GRAVITY = 0.6         // m/s2 the strength found may be from what the accelerometer reads on average
        /**
         * Down's likely error (9 times in 10) for each degree the directions of a whole turn disagree by (spreadDeg),
         * from simulated turns of 12 directions whose readings each carried the hand's own wobble (0.1 to 1.5 degrees a
         * direction): 0.37 to 0.4 above a spread of 0.4 degrees.
         */
        const val ERROR_PER_SPREAD = 0.4

        /**
         * How far off down likely is (9 times in 10), for a turn whose directions disagree by `spreadDeg`, whose phone
         * may add up to `offsetDeg` of its own along the axis it was turned about, which leaned `axisTiltDeg` from down.
         */
        fun expectedDeg(spreadDeg: Double, offsetDeg: Double, axisTiltDeg: Double): Double =
            kotlin.math.hypot(ERROR_PER_SPREAD * spreadDeg, offsetDeg * kotlin.math.sin(axisTiltDeg * PI / 180))
        const val CHECK_LEAST = 5           // directions read before check() says anything: with fewer, the fit bends to them
        const val FREE = 5                  // what the fit is free to choose: u, and c but for its part along u
        const val RECENT_S = 1.0
        const val NOW_S = 0.4
        /**
         * Degrees of wobble (wobbleNow) a turn can be held to and still come out agreeing well enough. A guide for the
         * screen, from what a third to half a second of readings a direction averages away: to tune from the logs.
         */
        const val WOBBLE_OK = 0.8
    }
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
