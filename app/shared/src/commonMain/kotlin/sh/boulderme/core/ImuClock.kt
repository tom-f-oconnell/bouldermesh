package sh.boulderme.core

import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.sqrt

/**
 * The motion sensors' readings lined up in time with the camera's frames. A frame's pose is where the camera was when
 * its picture was taken, tens of milliseconds before it reaches the app; the newest accelerometer reading is from about
 * then. A phone tilting at 0.2 radians a second moves 0.3° in 30 ms, as much as the turn on the spot may disagree by.
 * So readings are kept for a few seconds and the one at the frame's moment is taken, between the two either side.
 *
 * The two clocks are meant to be the same, but on some phones they aren't quite, or aren't at all: so how far apart they
 * are is found from the readings themselves. How fast the camera turned between frames (from the tracking) and how
 * fast the gyroscope says it was turning are the same thing, so the gyroscope's readings slid against the frames match
 * best where the clocks agree. That needs the speed to vary (a hand's wobble is enough); a phone held quite still, or
 * turning steadily, says nothing, and the last answer is kept.
 *
 * Not safe to use from two threads at once.
 */
class ImuClock(val keepS: Double = 4.0) {
    private class Series(val keepS: Double) {
        val t = ArrayDeque<Double>()
        val v = ArrayDeque<Vec3>()

        fun add(at: Double, x: Vec3) {
            if (t.isNotEmpty() && at <= t.last()) return        // the same reading seen again, or out of order
            t.addLast(at); v.addLast(x)
            while (t.size > 2 && t.first() < at - keepS) { t.removeFirst(); v.removeFirst() }
        }

        val newest: Double get() = if (t.isEmpty()) Double.NaN else t.last()

        /** The reading at `at`, between the two either side; the newest if `at` is after it; null if before them all. */
        fun at(at: Double): Vec3? {
            if (t.isEmpty() || at < t.first()) return null
            if (at >= t.last()) return v.last()
            var lo = 0; var hi = t.size - 1
            while (hi - lo > 1) { val mid = (lo + hi) / 2; if (t[mid] <= at) lo = mid else hi = mid }
            val k = (at - t[lo]) / (t[hi] - t[lo]).coerceAtLeast(1e-9)
            return v[lo] + (v[hi] - v[lo]) * k
        }
    }

    private val accel = Series(keepS)
    private val gyro = Series(keepS)
    private val frames = ArrayDeque<Pair<Double, Quat>>()
    private val found = ArrayList<Double>()
    private var lastTry = Double.NEGATIVE_INFINITY

    /** A sensor's time for an instant, less the camera's: what's added to a frame's time to find its readings. */
    var offsetS = 0.0
        private set

    /**
     * One try at how far apart the clocks are.
     *
     * @param offsetS the best match; fitRadS how far the speeds still disagree there (rms); motionRadS how much the
     *   speed varied, which is what the match goes by.
     * @param taken whether it was believed; usedS the offset now used, the median of those believed lately.
     * @param lagS how long after the frame's moment the newest accelerometer reading was, which is what was used before
     *   the two were lined up (negative: before it).
     */
    data class Estimate(val offsetS: Double, val fitRadS: Double, val motionRadS: Double, val taken: Boolean, val usedS: Double, val lagS: Double)

    fun accel(t: Double, v: Vec3) = accel.add(t, v)
    fun gyro(t: Double, v: Vec3) = gyro.add(t, v)

    /** The accelerometer's reading at a frame's moment, on the camera's clock; null with none kept from then. */
    fun accelAt(t: Double): Vec3? = accel.at(t + offsetS)

    /** The gyroscope's, likewise (radians a second, the phone's axes). */
    fun gyroAt(t: Double): Vec3? = gyro.at(t + offsetS)

    /**
     * A tracked frame: its time on the camera's clock and how the camera was turned. Every TRY_EVERY_S, tries again how
     * far apart the clocks are, and returns how that went (for the log); null otherwise or when there's too little to go by.
     */
    fun frame(t: Double, rot: Quat): Estimate? {
        if (frames.isNotEmpty() && t <= frames.last().first) return null
        frames.addLast(t to rot)
        while (frames.size > 2 && frames.first().first < t - keepS) frames.removeFirst()
        if (t - lastTry < TRY_EVERY_S) return null
        lastTry = t
        return estimate(t)
    }

    private fun estimate(now: Double): Estimate? {
        // the speed the camera turned at between each two frames, at the moment between them
        val mids = ArrayList<Double>(); val speeds = ArrayList<Double>()
        for (i in 0 until frames.size - 1) {
            val (t1, q1) = frames[i]; val (t2, q2) = frames[i + 1]
            val dt = t2 - t1
            if (dt <= 1e-4 || dt > 0.1) continue
            val w = abs((q1.conj() * q2).unit().w).coerceAtMost(1.0)
            mids.add((t1 + t2) / 2); speeds.add(2 * acos(w) / dt)
        }
        if (speeds.size < LEAST_PAIRS) return null
        val mean = speeds.average()
        val motion = sqrt(speeds.sumOf { (it - mean) * (it - mean) } / speeds.size)
        fun cost(o: Double): Double? {
            var sum = 0.0; var n = 0
            for (i in mids.indices) {
                val at = mids[i] + o
                if (at > gyro.newest) continue
                val g = gyro.at(at) ?: continue
                val e = g.norm - speeds[i]
                sum += e * e; n++
            }
            return if (n < speeds.size * 0.7) null else sum / n
        }
        val steps = (RANGE_S / STEP_S).toInt()
        val costs = (-steps..steps).map { cost(it * STEP_S) }
        val best = costs.indices.filter { costs[it] != null }.minByOrNull { costs[it]!! } ?: return null
        var o = (best - steps) * STEP_S
        // between the steps: the bottom of a parabola through the best and its neighbours
        val a = costs.getOrNull(best - 1); val b = costs[best]!!; val c = costs.getOrNull(best + 1)
        if (a != null && c != null && a + c - 2 * b > 1e-12) o += STEP_S * (a - c) / (2 * (a + c - 2 * b))
        val fit = sqrt(b)
        val taken = motion >= MIN_MOTION_RAD_S && fit <= motion * MAX_FIT_SHARE && best != 0 && best != costs.size - 1
        val lag = accel.newest.let { if (it.isNaN()) 0.0 else it - (now + offsetS) }
        if (taken) {
            found.add(o)
            if (found.size > KEEP_FOUND) found.removeAt(0)
            offsetS = found.sorted()[found.size / 2]
        }
        return Estimate(o, fit, motion, taken, offsetS, lag)
    }

    companion object {
        const val TRY_EVERY_S = 2.0
        const val RANGE_S = 0.25            // how far apart the clocks are looked for, either way
        const val STEP_S = 0.0025
        const val LEAST_PAIRS = 40
        const val MIN_MOTION_RAD_S = 0.05   // how much the speed must vary to go by
        const val MAX_FIT_SHARE = 0.6       // and how much of that may be left unexplained at the best match
        const val KEEP_FOUND = 7
    }
}
