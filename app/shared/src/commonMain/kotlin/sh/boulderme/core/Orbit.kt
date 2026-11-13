package sh.boulderme.core

import kotlin.math.atan2
import kotlin.math.floor
import kotlin.math.tan

/**
 * Capturing a rock there's no mesh of yet: photos from all round as much of it as can be walked, in sectors about its
 * middle, any of which can be marked as out of reach; and spots tapped on it (holds) that need close views.
 * All in the world the phone tracks.
 */
class Orbit(val centre: Vec3, val sectors: Int = 12) {
    class Sector { var shots = 0; var blocked = false }
    class Spot(val id: String, val p: Vec3) {
        val dirs = ArrayList<Vec3>()
        var bestGsdM: Double? = null
        var skipped = false
        fun done() = skipped || (dirs.size >= SPOT_VIEWS && (bestGsdM ?: 1.0) <= SPOT_GSD_M)
    }

    val ring = Array(sectors) { Sector() }
    val spots = ArrayList<Spot>()
    private var last: CameraPose? = null

    fun sectorOf(p: Vec3): Int {
        val a = (atan2(p.x - centre.x, p.z - centre.z) + 2 * kotlin.math.PI) % (2 * kotlin.math.PI)
        return floor(a / (2 * kotlin.math.PI / sectors)).toInt().coerceIn(0, sectors - 1)
    }

    fun addSpot(p: Vec3): Spot = Spot("s${spots.size}", p).also { spots.add(it) }
    fun block(sector: Int, blocked: Boolean = true) { ring[sector].blocked = blocked }

    private fun looksAtRock(pose: CameraPose) = angleDeg(pose.forward, centre - pose.centre) < 40.0

    private fun seesSpot(pose: CameraPose, s: Spot): Double? {
        val at = pose.project(s.p) ?: return null
        if (at.u < 0 || at.u > pose.lens.w || at.v < 0 || at.v > pose.lens.h) return null
        return pose.centre.distance(s.p) / pose.lens.fx
    }

    private fun spotGain(pose: CameraPose, s: Spot): Boolean {
        if (s.done()) return false
        val gsd = seesSpot(pose, s) ?: return false
        if (gsd > SPOT_GSD_M * Coverage.GOOD_ENOUGH) return false
        val towards = (pose.centre - s.p).unit()
        return s.dirs.none { angleDeg(it, towards) < VIEW_DEG } || (s.bestGsdM ?: 1.0) > SPOT_GSD_M && gsd <= SPOT_GSD_M
    }

    /** Whether a photo from here adds anything: a spot's view, or the rock from somewhere new. */
    fun useful(pose: CameraPose): Boolean {
        if (spots.any { spotGain(pose, it) }) return true
        if (!looksAtRock(pose) || ring[sectorOf(pose.centre)].blocked) return false     // a side marked out of reach: nothing asked of it
        val was = last ?: return true
        val off = pose.centre.distance(centre)
        return pose.centre.distance(was.centre) >= STEP * off || angleDeg(pose.forward, was.forward) >= TURN_DEG
    }

    fun add(pose: CameraPose): List<String> {
        val helped = ArrayList<String>()
        if (looksAtRock(pose)) { ring[sectorOf(pose.centre)].shots++; helped.add("sector${sectorOf(pose.centre)}") }
        for (s in spots) {
            val gsd = seesSpot(pose, s) ?: continue
            if (s.bestGsdM == null || gsd < s.bestGsdM!!) s.bestGsdM = gsd
            val towards = (pose.centre - s.p).unit()
            if (gsd <= SPOT_GSD_M * Coverage.GOOD_ENOUGH && s.dirs.none { angleDeg(it, towards) < VIEW_DEG }) { s.dirs.add(towards); helped.add(s.id) }
        }
        last = pose
        return helped
    }

    fun progress(): Progress {
        val open = ring.count { !it.blocked }
        return Progress(open + spots.size, ring.count { !it.blocked && it.shots >= SECTOR_SHOTS } + spots.count { it.done() && !it.skipped },
            spots.count { it.skipped }, spots.size, spots.count { it.done() && !it.skipped })
    }

    /** The nearest side still to do from sector `here`: which way round it is (to the left the sector number falls) and how many sectors off; `sectors` off if none. */
    private fun next(here: Int): Pair<Int, Int> {
        var best = -1; var bestSteps = sectors
        for (k in 1 until sectors) for (sign in intArrayOf(-1, 1)) {
            val s = ((here + sign * k) % sectors + sectors) % sectors
            if (!ring[s].blocked && ring[s].shots < SECTOR_SHOTS && k < bestSteps) { best = sign; bestSteps = k }
        }
        return best to bestSteps
    }

    /** Marks the side hint() is sending you round to as out of reach. Returns whether there was one. */
    fun blockNext(pose: CameraPose): Boolean {
        val here = sectorOf(pose.centre)
        val (sign, steps) = next(here)
        if (steps == sectors) return false
        block(((here + sign * steps) % sectors + sectors) % sectors)
        return true
    }

    fun hint(pose: CameraPose): Hint {
        spots.firstOrNull { !it.done() && seesSpot(pose, it) != null }?.let { s ->
            val dm = pose.centre.distance(s.p)
            val gsd = dm / pose.lens.fx
            if (gsd > SPOT_GSD_M) return Hint(Hint.Kind.CLOSER, "Move ${Coverage.metres(dm - SPOT_GSD_M * pose.lens.fx)} closer to the spot you marked", s.id, moveM = dm - SPOT_GSD_M * pose.lens.fx)
            val towards = (pose.centre - s.p).unit()
            if (s.dirs.any { angleDeg(it, towards) < VIEW_DEG })
                return Hint(Hint.Kind.ANGLE, "Good. Step ${Coverage.metres(dm * tan(VIEW_DEG * 1.3 * kotlin.math.PI / 180))} to one side for another angle on it", s.id)
            return Hint(Hint.Kind.SHOOT, "Hold still", s.id)
        }
        if (!looksAtRock(pose)) return Hint(Hint.Kind.TURN, "Point the camera at the rock")
        val here = sectorOf(pose.centre)
        if (ring[here].shots < SECTOR_SHOTS && !ring[here].blocked) return Hint(Hint.Kind.SHOOT, "Hold still")
        val (best, bestSteps) = next(here)
        if (bestSteps == sectors) {
            val left = spots.count { !it.done() }
            return if (left > 0) Hint(Hint.Kind.TURN, "Now the ${if (left == 1) "spot" else "$left spots"} you marked: go up close")
            else Hint(Hint.Kind.DONE, "All round done. Finish, or mark holds for close-ups")
        }
        return Hint(Hint.Kind.FACE, "Step round to your ${if (best < 0) "left" else "right"}, or mark that side as out of reach", turnX = best.toDouble())
    }

    companion object {
        const val SECTOR_SHOTS = 2          // photos from a sector for it to count
        const val STEP = 0.10               // of how far off the rock is: moved this far since the last photo, another is worth it
        const val TURN_DEG = 8.0            // or turned this far
        const val SPOT_VIEWS = 4
        const val SPOT_GSD_M = 0.0005       // a marked spot is seen finely enough at half a millimetre a pixel
        const val VIEW_DEG = 15.0
    }
}
