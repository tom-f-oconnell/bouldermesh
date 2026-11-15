package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.floor
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min

/**
 * What's around the rock, for the site to draw behind its mesh (a Gaussian splat, fitted on the server: backend/splat.py),
 * which needn't be exact but has to sit where the rock does: frames taken while walking a loop a few rock-widths out,
 * looking away from the rock and back at it, in the same tracking as the rock's own photos, which is what places them.
 *
 * The loop is split into SECTORS by bearing from the rock's middle; each sector wants a frame looking out and one
 * looking in. Frames are taken as the phone moves (every STEP_M or TURN_DEG), not only for a sector still to fill: a
 * splat wants many overlapping views. All in the tracking's world, +Y up.
 *
 * @param centre the rock's middle; rockM: about how far its sides are from that, in metres.
 */
class SceneryWalk(val centre: Vec3, rockM: Double) {
    val rockM: Double = rockM.coerceIn(0.5, 15.0)
    /** About how far out to walk. */
    val ringM: Double = (this.rockM * 2.5).coerceIn(4.0, 25.0)
    val nearestM: Double = max(this.rockM * 1.2, 2.0)
    val outDone = BooleanArray(SECTORS)
    val inDone = BooleanArray(SECTORS)
    private var last: CameraPose? = null
    private var lastT = -1e9
    var frames = 0
        private set

    val total: Int get() = 2 * SECTORS
    val done: Int get() = outDone.count { it } + inDone.count { it }

    private fun flat(v: Vec3) = Vec3(v.x, 0.0, v.z)
    fun sectorOf(p: Vec3): Int {
        val a = (atan2(p.x - centre.x, p.z - centre.z) + 2 * PI) % (2 * PI)
        return floor(a / (2 * PI / SECTORS)).toInt().coerceIn(0, SECTORS - 1)
    }
    private fun distance(p: Vec3) = flat(p - centre).norm

    /** Which way the camera looks, against the way out from the rock: 1 straight out, -1 straight at the rock. */
    fun outward(pose: CameraPose): Double {
        val out = flat(pose.centre - centre).unit(); val f = flat(pose.forward).unit()
        return if (out.norm < 0.5 || f.norm < 0.5) 0.0 else out dot f
    }

    private fun inRange(pose: CameraPose) = distance(pose.centre) >= nearestM && distance(pose.centre) <= ringM * 2.5

    /** Whether to take a frame from here now: far enough out, steady, and moved or turned enough since the last. */
    fun wanted(pose: CameraPose, t: Double, steady: Boolean): Boolean {
        if (!steady || !inRange(pose) || frames >= MOST || t - lastT < MIN_GAP_S) return false
        val was = last ?: return true
        return was.centre.distance(pose.centre) >= STEP_M || angleDeg(was.forward, pose.forward) >= TURN_DEG
    }

    /** Counts a frame taken from here. */
    fun add(pose: CameraPose, t: Double) {
        last = pose; lastT = t; frames++
        if (!inRange(pose)) return
        val s = sectorOf(pose.centre); val o = outward(pose)
        if (o >= COS_FACING) outDone[s] = true else if (o <= -COS_FACING) inDone[s] = true
    }

    /**
     * What to do next, in words, with the way to go across the screen: further out, round to the next sector still
     * wanted, or turned to face out (or in) as that sector wants.
     */
    fun hint(pose: CameraPose, uprightDeg: Int): Hint {
        val d = distance(pose.centre)
        if (d < nearestM) return Hint(Hint.Kind.BACK, "Walk further from the rock: about ${metres(ringM)} out", moveM = ringM - d)
        if (done >= total) return Hint(Hint.Kind.DONE, "All round, out and in: that's the scenery. Finish, or carry on for more.")
        val here = sectorOf(pose.centre); val o = outward(pose)
        if (!outDone[here] && o < COS_FACING && (inDone[here] || o > -COS_FACING)) return Hint(Hint.Kind.TURN, "Turn to face away from the rock, and walk slowly round")
        if (!inDone[here] && outDone[here] && o > -COS_FACING) return Hint(Hint.Kind.TURN, "Now face the rock from here, then walk on round")
        // the nearest sector still wanted, round the loop: which way across the screen
        var best = -1; var bestSteps = SECTORS
        for (k in 1 until SECTORS) for (sgn in intArrayOf(1, -1)) {
            val s = ((here + sgn * k) % SECTORS + SECTORS) % SECTORS
            if ((!outDone[s] || !inDone[s]) && k < bestSteps) { best = s; bestSteps = k }
        }
        if (best < 0) return Hint(Hint.Kind.NONE, "Walk slowly round the rock, ${metres(ringM)} or so out")
        val a = (best + 0.5) * 2 * PI / SECTORS
        val spot = centre + Vec3(kotlin.math.sin(a), 0.0, kotlin.math.cos(a)) * max(d, nearestM)
        val c = pose.rot.conj().rotate(spot - pose.centre)
        val (sx, sy) = Coverage.toScreen(c.x, c.y, uprightDeg)
        val n = hypot(sx, sy).let { if (it > 1e-9) it else 1.0 }
        val word = if (c.z < 0 && abs(sx) < abs(c.z)) "behind you" else if (sx >= 0) "to the right" else "to the left"
        return Hint(Hint.Kind.GO, "Walk slowly round, ${metres(ringM)} or so out: more to see $word (${done} of $total)", turnX = sx / n, turnY = -sy / n,
            moveM = spot.distance(pose.centre))
    }

    private fun metres(m: Double) = if (m >= 10) "${m.toInt()} m" else "${kotlin.math.round(m * 2) / 2} m"

    companion object {
        const val SECTORS = 16
        const val STEP_M = 0.3
        const val TURN_DEG = 8.0
        const val MIN_GAP_S = 0.4
        const val MOST = 600            // frames a walk takes at most: about 100 MB
        val COS_FACING = kotlin.math.cos(55 * PI / 180)
    }
}

/**
 * Frames for a panorama of what's far off, taken during the turn on the spot that starts a capture: one for each
 * BUCKETS of the turn, when the phone is steady and held near level. What's far off needs only which way each looked.
 */
class PanoTurn {
    private val taken = BooleanArray(BUCKETS)
    val count: Int get() = taken.count { it }

    fun bucketOf(pose: CameraPose): Int? {
        val f = pose.forward
        if (abs(f.y) > kotlin.math.sin(MAX_PITCH_DEG * PI / 180)) return null
        val a = (atan2(f.x, f.z) + 2 * PI) % (2 * PI)
        return floor(a / (2 * PI / BUCKETS)).toInt().coerceIn(0, BUCKETS - 1)
    }

    fun wanted(pose: CameraPose, steady: Boolean): Boolean = steady && bucketOf(pose)?.let { !taken[it] } == true
    fun add(pose: CameraPose) { bucketOf(pose)?.let { taken[it] = true } }

    companion object {
        const val BUCKETS = 24
        const val MAX_PITCH_DEG = 25.0
    }
}

/** How a frame for the scenery is taken: smear from the phone's own turning, for something a few metres or more off. */
internal fun sceneryBlurPx(gyroRadS: Double, exposureS: Double, fx: Double): Double = gyroRadS * exposureS * fx
