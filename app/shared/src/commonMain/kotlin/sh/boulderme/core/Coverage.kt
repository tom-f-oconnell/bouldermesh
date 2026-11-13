package sh.boulderme.core

import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.tan

/** Which targets count: those that matter at least `minPrio` (2: holds and the rock on climbs), of these climbs if any are named. */
data class Scope(val minPrio: Int = 2, val climbs: Set<String> = emptySet())

/** How a target was seen in one photo. */
data class Seen(val gsdM: Double, val distanceM: Double, val cosFacing: Double, val towards: Vec3, val u: Double, val v: Double)

/** How far through the targets in scope a capture is. */
data class Progress(val total: Int, val done: Int, val skipped: Int, val holds: Int, val holdsDone: Int) {
    val left: Int get() = total - done - skipped
    val finished: Boolean get() = total > 0 && left == 0
}

/** What to do next, in words, and which way: `turnX`, `turnY` point across and up the screen (-1..1) when the target is out of frame. */
data class Hint(
    val kind: Kind, val text: String, val targetId: String? = null,
    val turnX: Double = 0.0, val turnY: Double = 0.0, val moveM: Double = 0.0,
    val u: Double? = null, val v: Double? = null,
) {
    enum class Kind { NONE, TURN, CLOSER, BACK, FACE, ANGLE, EDGE, SHOOT, DONE }
}

/**
 * What of a mesh's plan has been photographed well enough: each target's finest view and the directions it's been seen
 * from, counting what its earlier photos gave it and adding each photo taken now. All in the mesh's own coordinates.
 */
class Coverage(val plan: Plan, val guide: GuideMesh?, var metresPerUnit: Double = plan.frame.metresPerUnit) {
    class State(val target: Target) {
        var bestGsdM: Double? = null        // of the photos taken now
        val dirs = ArrayList<Vec3>()        // good views taken now, each from a different direction
        var shots = 0
        var skipped: String? = null
    }

    val states: List<State> = plan.targets.map { State(it) }
    private val byId = states.associateBy { it.target.id }
    var scope = Scope()
    var refFx = 3000.0      // a phone's main camera at 12 megapixels, until a photo says

    private val cosFacing = cos(plan.facingDeg * kotlin.math.PI / 180)

    fun state(id: String): State? = byId[id]
    fun inScope(s: State): Boolean = s.target.prio >= scope.minPrio && (scope.climbs.isEmpty() || s.target.climbs.any { it in scope.climbs })

    /** How finely a target needs seeing, in metres a pixel: what its kind needs, or the best a phone held up could do if it's out of reach. */
    fun needGsdM(s: State): Double? {
        val mm = plan.needOf(s.target.kind).gsdMm ?: return null
        val up = (s.target.p[1] - plan.frame.groundY) * metresPerUnit
        val nearest = max(MIN_STANDOFF_M, up - REACH_M)
        return max(mm / 1000.0, 1.15 * nearest / refFx)
    }

    fun needViews(s: State): Int = plan.needOf(s.target.kind).views

    /** Directions it's been seen from well enough: those taken now, and its earlier photos' if they saw it finely enough. */
    fun goodViews(s: State): Int {
        val need = needGsdM(s)
        val had = s.target.haveGsd?.let { it * metresPerUnit }
        val before = if (need != null && had != null && had <= need * GOOD_ENOUGH) s.target.haveViews else 0
        return s.dirs.size + before
    }

    fun bestGsdM(s: State): Double? {
        val had = s.target.haveGsd?.let { it * metresPerUnit }
        val now = s.bestGsdM
        return if (had != null && now != null) min(had, now) else now ?: had
    }

    fun done(s: State): Boolean {
        val need = needGsdM(s) ?: return s.dirs.size >= needViews(s)
        val best = bestGsdM(s) ?: return false
        return best <= need && goodViews(s) >= needViews(s)
    }

    /** From 0 to 1, how near done a target is. */
    fun fraction(s: State): Double {
        val views = clamp01(goodViews(s).toDouble() / needViews(s))
        val need = needGsdM(s) ?: return clamp01(s.dirs.size.toDouble() / needViews(s))
        val fine = bestGsdM(s)?.let { clamp01(need / it) } ?: 0.0
        return if (done(s)) 1.0 else min(0.95, 0.5 * views + 0.5 * fine)
    }

    /**
     * How a camera (in the mesh's coordinates) sees a target, or null if it doesn't: out of frame, facing away, too
     * far to be worth it, or (with `hidden` checked) behind another part of the mesh.
     */
    fun see(pose: CameraPose, s: State, hidden: Boolean = true): Seen? {
        val t = s.target
        val p = t.point
        val at = pose.project(p) ?: return null
        val mx = pose.lens.w * FRAME_MARGIN; val my = pose.lens.h * FRAME_MARGIN
        if (at.u < mx || at.u > pose.lens.w - mx || at.v < my || at.v > pose.lens.h - my) return null
        val away = pose.centre - p
        val d = away.norm
        if (d < 1e-9) return null
        val towards = away / d
        val facing = t.normal dot towards
        if (t.isEdge) {
            val beyond = t.beyond?.let { it dot towards } ?: 0.0
            if (facing < 0.1 && beyond < 0.3) return null
        } else if (facing < cosFacing) return null
        val dm = d * metresPerUnit
        if (dm > FAR_M) return null
        if (hidden && guide != null && !guide.clear(pose.centre, p, max(t.r, 0.05 * d))) return null
        return Seen(dm / pose.lens.fx / max(facing, 0.2), dm, facing, towards, at.u, at.v)
    }

    private fun newDirection(s: State, towards: Vec3): Boolean = s.dirs.none { angleDeg(it, towards) < plan.viewDeg }

    private fun counts(s: State, seen: Seen): Boolean {
        val need = needGsdM(s) ?: return true
        return seen.gsdM <= need * GOOD_ENOUGH
    }

    /** Counts a photo taken from this camera. Returns the targets it did something for. */
    fun addShot(pose: CameraPose): List<String> {
        refFx = pose.lens.fx
        val helped = ArrayList<String>()
        for (s in states) {
            val seen = see(pose, s) ?: continue
            s.shots++
            var better = false
            if (s.bestGsdM == null || seen.gsdM < s.bestGsdM!!) { better = s.bestGsdM == null || seen.gsdM < s.bestGsdM!! * 0.85; s.bestGsdM = seen.gsdM }
            if (counts(s, seen) && newDirection(s, seen.towards)) { s.dirs.add(seen.towards); better = true }
            if (better) helped.add(s.target.id)
        }
        return helped
    }

    /** How many targets in scope and still to do a photo from here would help: for taking one by itself. Nothing hidden is checked. */
    fun gain(pose: CameraPose): Int {
        var n = 0
        for (s in states) {
            if (!inScope(s) || s.skipped != null || done(s)) continue
            val seen = see(pose, s, hidden = false) ?: continue
            val finer = bestGsdM(s)?.let { seen.gsdM < it * 0.85 } ?: true
            val need = needGsdM(s)
            val wanted = need == null || (bestGsdM(s) ?: Double.MAX_VALUE) > need
            if ((counts(s, seen) && newDirection(s, seen.towards)) || (finer && wanted && counts(s, seen))) n++
        }
        return n
    }

    fun skip(id: String, why: String) { byId[id]?.skipped = why }
    fun unskip(id: String) { byId[id]?.skipped = null }

    fun progress(): Progress {
        val mine = states.filter { inScope(it) }
        return Progress(mine.size, mine.count { it.skipped == null && done(it) }, mine.count { it.skipped != null },
            mine.count { it.target.isHold }, mine.count { it.target.isHold && it.skipped == null && done(it) })
    }

    /** The target to go for next from here: one in frame if any is still to do (holds first), else the least turn away. */
    fun pick(pose: CameraPose, current: String? = null): State? {
        var best: State? = null
        var bestCost = Double.MAX_VALUE
        for (s in states) {
            if (!inScope(s) || s.skipped != null || done(s)) continue
            val c = pose.rot.conj().rotate(s.target.point - pose.centre)
            val off = atan2(hypot(c.x, c.y), c.z) * 180 / kotlin.math.PI          // degrees off the middle of the view
            val far = c.norm * metresPerUnit
            var cost = off + 8.0 * far - 25.0 * s.target.prio - 30.0 * fraction(s)   // nearly done: finish it
            if (s.target.id == current) cost -= 20.0
            if (cost < bestCost) { bestCost = cost; best = s }
        }
        return best
    }

    /** What to do about a target from here, in words. */
    fun hint(pose: CameraPose, s: State): Hint {
        val t = s.target
        val c = pose.rot.conj().rotate(t.point - pose.centre)
        val d = c.norm
        val at = pose.project(t.point)
        val mx = pose.lens.w * FRAME_MARGIN; val my = pose.lens.h * FRAME_MARGIN
        val what = if (t.isHold) "the ${t.role ?: ""} hold".replace("  ", " ") else if (t.isEdge) "the edge" else "this part"
        if (at == null || at.u < mx || at.u > pose.lens.w - mx || at.v < my || at.v > pose.lens.h - my) {
            if (c.z < 0 && hypot(c.x, c.y) < -c.z) return Hint(Hint.Kind.TURN, "Turn round: $what is behind you", t.id, turnX = if (c.x >= 0) 1.0 else -1.0)
            val n = hypot(c.x, c.y).let { if (it > 1e-9) it else 1.0 }
            val across = c.x / n; val down = c.y / n       // image y runs down
            val word = if (kotlin.math.abs(across) >= kotlin.math.abs(down)) (if (across > 0) "right" else "left") else (if (down > 0) "down" else "up")
            return Hint(Hint.Kind.TURN, "Point the camera $word, at $what", t.id, turnX = across, turnY = -down)
        }
        val towards = (pose.centre - t.point).unit()
        val facing = t.normal dot towards
        val dm = d * metresPerUnit
        if (t.isEdge) {
            return Hint(Hint.Kind.EDGE, "The mesh ends here: show what's past this edge, from a few steps apart", t.id, u = at.u, v = at.v)
        }
        if (facing < cosFacing) {
            // where facing it would be: out along its normal; which side of the view that is
            val want = pose.rot.conj().rotate(t.point + t.normal * d - pose.centre)
            return Hint(Hint.Kind.FACE, "Move ${if (want.x > 0) "right" else "left"} to face $what more squarely", t.id, turnX = if (want.x > 0) 1.0 else -1.0, u = at.u, v = at.v)
        }
        if (dm < TOO_CLOSE_M) return Hint(Hint.Kind.BACK, "Too close to focus: move back a little", t.id, moveM = TOO_CLOSE_M - dm, u = at.u, v = at.v)
        val need = needGsdM(s)
        val gsd = dm / pose.lens.fx / max(facing, 0.2)
        // too coarse to count at all, or fine enough to count but with no view yet as fine as it needs
        if (need != null && (gsd > need * GOOD_ENOUGH || (gsd > need && (bestGsdM(s) ?: Double.MAX_VALUE) > need))) {
            val enough = need * pose.lens.fx * max(facing, 0.2)       // metres off at which it's seen finely enough
            val closer = dm - enough
            return Hint(Hint.Kind.CLOSER, "Move ${metres(closer)} closer to $what", t.id, moveM = closer, u = at.u, v = at.v)
        }
        val seen = Seen(gsd, dm, facing, towards, at.u, at.v)
        if (!newDirection(s, towards)) {
            val step = dm * tan(plan.viewDeg * 1.3 * kotlin.math.PI / 180)
            return Hint(Hint.Kind.ANGLE, "Good. Step ${metres(step)} to one side for another angle on $what", t.id, moveM = step, u = at.u, v = at.v)
        }
        return if (counts(s, seen)) Hint(Hint.Kind.SHOOT, "Hold still", t.id, u = at.u, v = at.v) else Hint(Hint.Kind.NONE, "", t.id, u = at.u, v = at.v)
    }

    /** How done each of the guide's patches is, for colouring it: LEVEL_*. */
    fun patchLevels(): IntArray {
        val out = IntArray(guide?.patches ?: 0) { LEVEL_OUT }
        for (s in states) {
            if (s.target.kind != "patch") continue
            val k = s.target.id.drop(1).toIntOrNull() ?: continue
            if (k in out.indices) out[k] = level(s)
        }
        return out
    }

    fun level(s: State): Int = when {
        s.skipped != null -> LEVEL_SKIPPED
        !inScope(s) -> if (done(s)) LEVEL_DONE else LEVEL_OUT
        done(s) -> LEVEL_DONE
        fraction(s) >= 0.5 -> LEVEL_PART
        else -> LEVEL_TODO
    }

    companion object {
        const val FRAME_MARGIN = 0.06       // of the picture: a target nearer its edge than this isn't well in it
        const val FAR_M = 12.0              // from further than this a photo isn't of the target
        const val TOO_CLOSE_M = 0.25
        const val MIN_STANDOFF_M = 0.35     // as near as a phone is held to rock
        const val REACH_M = 2.2             // as high as a phone is held, from the ground
        const val GOOD_ENOUGH = 1.5         // a view this many times coarser than needed still counts as a direction
        const val LEVEL_OUT = 0; const val LEVEL_TODO = 1; const val LEVEL_PART = 2; const val LEVEL_DONE = 3; const val LEVEL_SKIPPED = 4

        internal fun metres(m: Double): String = if (m < 0.95) "${max(5, ((m * 100).roundToInt() / 5) * 5)} cm" else "${(m * 10).roundToInt() / 10.0} m"
    }
}
