package sh.boulderme.core

import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.floor
import kotlin.math.tan

/**
 * Capturing a rock there's no mesh of yet: photos from all round as much of it as can be walked, in sectors about its
 * middle, any of which can be marked as out of reach; and spots tapped on it (holds) that need close views.
 * More rings can be asked for, each all round again from another height (crouched, or the phone up on a pole): a ring's
 * height is where its first photo was taken from, and only photos within RING_BAND_M of it count for it, once there's
 * more than one. `others`: heights photos have already been taken from round it (a capture guided by a mesh, whose
 * rings come after), which a new ring has to keep away from as from an earlier ring. All in the world the phone tracks.
 */
class Orbit(val centre: Vec3, val sectors: Int = 12, private val others: List<Double> = emptyList()) {
    class Sector { var shots = 0; var blocked = false }
    /** One time round. `y`: the height it's at, once its first photo is in. */
    class Ring(val n: Int, sectors: Int) {
        var y: Double? = null
        val shots = IntArray(sectors)
    }
    class Spot(val id: String, val p: Vec3) {
        val dirs = ArrayList<Vec3>()
        var bestGsdM: Double? = null
        var skipped = false
        fun done() = skipped || (dirs.size >= SPOT_VIEWS && (bestGsdM ?: 1.0) <= SPOT_GSD_M)
    }

    val ring = Array(sectors) { Sector() }      // the first ring's photos, and which sides are out of reach, for every ring
    val rings = ArrayList<Ring>()
    val spots = ArrayList<Spot>()
    private var last: CameraPose? = null

    fun sectorOf(p: Vec3): Int {
        val a = (atan2(p.x - centre.x, p.z - centre.z) + 2 * kotlin.math.PI) % (2 * kotlin.math.PI)
        return floor(a / (2 * kotlin.math.PI / sectors)).toInt().coerceIn(0, sectors - 1)
    }

    init { if (others.isEmpty()) rings.add(Ring(1, sectors)) }
    val active: Ring? get() = rings.lastOrNull()
    private fun shotsAt(r: Ring, k: Int) = if (r.n == 1) ring[k].shots else r.shots[k]
    private val heights: Boolean get() = rings.size + others.size > 1        // only then does a photo's height say which ring it's of

    /** Starts another ring round the rock, from another height. Returns it. */
    fun newRing(): Ring = Ring((active?.n ?: 0) + 1, sectors).also { rings.add(it) }
    fun ringDone(r: Ring): Boolean = (0 until sectors).all { ring[it].blocked || shotsAt(r, it) >= SECTOR_SHOTS }

    /** How far the phone is above (+) or below the height of ring `r` (0 with no height yet), and whether a photo from there counts for it. */
    private fun off(pose: CameraPose, r: Ring): Double = r.y?.let { pose.centre.y - it } ?: 0.0
    private fun startsAway(pose: CameraPose, r: Ring): Boolean =
        (others + rings.filter { it !== r }.mapNotNull { it.y }).all { abs(pose.centre.y - it) >= RING_APART_M }
    fun inRing(pose: CameraPose, r: Ring): Boolean = !heights || if (r.y == null) startsAway(pose, r) else abs(off(pose, r)) <= RING_BAND_M

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
        val r = active ?: return false
        if (!inRing(pose, r)) return false
        if (r.y == null) return true
        val was = last ?: return true
        val off = pose.centre.distance(centre)
        return pose.centre.distance(was.centre) >= STEP * off || angleDeg(pose.forward, was.forward) >= TURN_DEG
    }

    fun add(pose: CameraPose): List<String> {
        val helped = ArrayList<String>()
        val r = active
        if (r != null && looksAtRock(pose) && inRing(pose, r)) {
            val k = sectorOf(pose.centre)
            if (r.y == null) r.y = pose.centre.y
            if (r.n == 1) { ring[k].shots++; helped.add("sector$k") } else { r.shots[k]++; helped.add("ring${r.n}_sector$k") }
        }
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
        val open = ring.count { !it.blocked } * rings.size
        val sides = rings.sumOf { r -> (0 until sectors).count { !ring[it].blocked && shotsAt(r, it) >= SECTOR_SHOTS } }
        return Progress(open + spots.size, sides + spots.count { it.done() && !it.skipped },
            spots.count { it.skipped }, spots.size, spots.count { it.done() && !it.skipped })
    }

    /** The nearest side still to do from sector `here`: which way round it is (to the left the sector number falls) and how many sectors off; `sectors` off if none. */
    private fun next(here: Int): Pair<Int, Int> {
        var best = -1; var bestSteps = sectors
        val r = active ?: return best to bestSteps
        for (k in 1 until sectors) for (sign in intArrayOf(-1, 1)) {
            val s = ((here + sign * k) % sectors + sectors) % sectors
            if (!ring[s].blocked && shotsAt(r, s) < SECTOR_SHOTS && k < bestSteps) { best = sign; bestSteps = k }
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
        val r = active ?: return Hint(Hint.Kind.DONE, "All round done")
        fun say(t: String) = if (heights) "Ring ${r.n}: $t" else t.replaceFirstChar { it.uppercase() }
        if (heights && !inRing(pose, r)) {
            if (r.y == null) return Hint(Hint.Kind.GO, say("hold the phone at least ${Coverage.metres(RING_APART_M)} higher or lower than the other rings: up on a pole, or crouched"))
            val dy = off(pose, r)
            return Hint(Hint.Kind.GO, say("${if (dy > 0) "lower" else "raise"} the phone ${Coverage.metres(abs(dy))}, to this ring's height"), turnY = if (dy > 0) -1.0 else 1.0)
        }
        val here = sectorOf(pose.centre)
        if (shotsAt(r, here) < SECTOR_SHOTS && !ring[here].blocked) return Hint(Hint.Kind.SHOOT, say("hold still"))
        val (best, bestSteps) = next(here)
        if (bestSteps == sectors) {
            val left = spots.count { !it.done() }
            return if (left > 0) Hint(Hint.Kind.TURN, "Now the ${if (left == 1) "spot" else "$left spots"} you marked: go up close")
            else Hint(Hint.Kind.DONE, if (heights) "Ring ${r.n} done, all round. Finish, or another ring from another height" else "All round done. Finish, another ring from another height, or mark holds for close-ups")
        }
        return Hint(Hint.Kind.FACE, say("step round to your ${if (best < 0) "left" else "right"}, or mark that side as out of reach"), turnX = best.toDouble())
    }

    companion object {
        const val SECTOR_SHOTS = 2          // photos from a sector for it to count
        const val STEP = 0.10               // of how far off the rock is: moved this far since the last photo, another is worth it
        const val TURN_DEG = 8.0            // or turned this far
        const val SPOT_VIEWS = 4
        const val SPOT_GSD_M = 0.0005       // a marked spot is seen finely enough at half a millimetre a pixel
        const val VIEW_DEG = 15.0
        const val RING_BAND_M = 0.5         // a photo this near a ring's height is of that ring
        const val RING_APART_M = 0.9        // and a new ring starts at least this far above or below the others
    }
}
