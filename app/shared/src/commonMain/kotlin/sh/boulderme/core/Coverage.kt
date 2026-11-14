package sh.boulderme.core

import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.hypot
import kotlin.math.ln
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.roundToInt
import kotlin.math.tan

/**
 * Which targets count: those that matter at least `minPrio` (2: holds and the rock on climbs), of these climbs if any
 * are named. set: the set of beta the capture is for: its holds are asked for in detail (as finely as the phone can,
 * from all round: Need.closeM), as a replica of a hold needs; the other holds count as holds do, unless minPrio is 3,
 * which leaves only the set's own. anon: holds of sets their owners keep to themselves count too (they're left for last).
 */
data class Scope(val minPrio: Int = 2, val climbs: Set<String> = emptySet(), val set: String? = null, val anon: Boolean = false)

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
    enum class Kind { NONE, TURN, CLOSER, BACK, FACE, ANGLE, EDGE, GO, SHOOT, DONE }
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
        val rungs = HashSet<Int>()          // of a hold's ladder (Need.ladder), those with a photo taken now: 1 the finest
        val sides = HashSet<Int>()          // of a hold wanted in detail: the sides it has a close view from (SIDE_*)
        var card = false                    // and whether it has a photo with a card beside it, for its size
        var textured = false                // a patch: a photo taken now is square on and fine enough to texture it with
        internal var wanted: Set<Int>? = null
        internal var wantedAt = 0.0
    }

    val states: List<State> = plan.targets.map { State(it) }
    private val byId = states.associateBy { it.target.id }
    var scope = Scope()
    var refFx = 3000.0      // a phone's main camera at 12 megapixels, until a photo says

    private val cosFacing = cos(plan.facingDeg * kotlin.math.PI / 180)

    fun state(id: String): State? = byId[id]
    var closeScale = 1.0    // how much further off than asked this phone turned out to focus: grows when close photos come out soft
    var refTan = 0.5        // the tangent of half the angle the shorter side of a photo takes in
    /** The lens holds wanted in detail are photographed with, where it isn't every photo's: a phone whose other photos are its tracking's frames takes full-size stills of them. */
    var detailLens: Lens? = null
    private val texture: Need? = plan.need["texture"]
    private val cosTexture = texture?.facingDeg?.let { cos(it * kotlin.math.PI / 180) } ?: cosFacing

    /** The lens photos are taken with. */
    fun lens(l: Lens) { refFx = l.fx; refTan = min(l.w, l.h) / 2.0 / l.fx }
    private fun detailFx(): Double = detailLens?.fx ?: refFx
    private fun detailTan(): Double = detailLens?.let { min(it.w, it.h) / 2.0 / it.fx } ?: refTan

    fun inScope(s: State): Boolean {
        val set = scope.set
        if (s.target.anon && !scope.anon) return false
        // only the set's own holds, when that's all that's asked for; otherwise every hold known counts, the set's in detail
        if (set != null && s.target.isHold && scope.minPrio >= 3) return set in s.target.sets
        if (s.target.isView) return scope.minPrio <= 3      // the views that tie a capture to its mesh's photos: whatever else counts
        return s.target.prio >= scope.minPrio && (scope.climbs.isEmpty() || s.target.climbs.any { it in scope.climbs })
    }

    /** Whether a target is a hold of the set of beta the capture is for: wanted in detail. */
    fun detail(s: State): Boolean = scope.set != null && s.target.isHold && scope.set in s.target.sets

    /** Whether a target is rock wanted only for the mesh's texture: on no climb, when the plan says what a texture needs. */
    private fun forTexture(s: State): Boolean = texture != null && s.target.kind == "patch" && s.target.prio < 2

    /** What a target needs: its kind's; for a hold of the capture's own set of beta, the detail need; for rock on no climb, the texture's. */
    fun needFor(s: State): Need = if (detail(s)) plan.needOf("detail") else if (forTexture(s)) texture!! else plan.needOf(s.target.kind)

    private fun height(s: State): Double = (s.target.p[1] - plan.frame.groundY) * metresPerUnit

    /**
     * Whether a patch still wants a photo taken now to texture it with: nearly square on and fine enough. What its
     * earlier photos saw doesn't count for that: the texture is to be from the capture's own.
     */
    fun wantsTexture(s: State): Boolean = texture != null && s.target.kind == "patch" && !s.textured

    /** How square on a photo that textures a patch has to be, as a cosine: as the plan says, or for rock above reach, as square as it can be seen at all. */
    private fun cosTex(s: State): Double = if (height(s) > REACH_M) cosFacing else cosTexture
    private fun texGsdM(s: State): Double = max((texture?.gsdMm ?: 2.0) / 1000.0, needGsdM(s) ?: 0.0)
    private fun textures(s: State, seen: Seen): Boolean = wantsTexture(s) && seen.cosFacing >= cosTex(s) && seen.gsdM <= texGsdM(s)

    /**
     * How far off a hold wanted in detail is photographed from, in metres: as near as the lens focuses, or as far as
     * it takes to have the whole of it (Target.detailM round it) in the picture. Null for anything else.
     */
    fun closeM(s: State): Double? {
        val close = needFor(s).closeM ?: return null
        return max(close, (s.target.detailM ?: 0.0) * REGION_FILL / detailTan()) * closeScale
    }

    /** As close as a target is photographed from, in metres: nearer is out of focus. */
    fun closestM(s: State): Double = closeM(s)?.let { it * CLOSEST } ?: TOO_CLOSE_M

    /**
     * How finely a target needs seeing, in metres a pixel: what it needs (a number, or what the lens gives from as close
     * as a hold wanted in detail is photographed), or the best a phone held up could do if it's out of reach.
     */
    fun needGsdM(s: State): Double? {
        val close = closeM(s)
        val fx = if (close != null) detailFx() else refFx
        val base = close?.let { it / fx } ?: needFor(s).gsdMm?.let { it / 1000.0 } ?: return null
        val beyond = height(s) - REACH_M       // how far above where a phone can be held
        if (close != null) return if (beyond > 0) max(base, 1.15 * max(beyond, close) / fx) else base
        return max(base, 1.15 * max(MIN_STANDOFF_M, beyond) / fx)
    }

    fun needViews(s: State): Int = needFor(s).views
    private fun viewDeg(s: State): Double = needFor(s).viewDeg ?: plan.viewDeg

    /** A close photo came out soft: this phone doesn't focus as near as was asked, so what's asked backs off a fifth. */
    fun backOff() { closeScale = min(CLOSE_SCALE_MOST, closeScale * 1.2); states.forEach { it.wanted = null } }

    // ---- a hold wanted in detail: its sides, and the photos that tie its close ones in ---------------------------------
    /** Across a target's face (to the right of someone looking at it) and up along it. A roof or a slab's top has no left and right by gravity: any will do. */
    private fun faceAxes(n: Vec3): Pair<Vec3, Vec3> {
        var a = Vec3(0.0, 1.0, 0.0) cross n
        if (a.norm < 0.3) a = Vec3(1.0, 0.0, 0.0) cross n
        a = a.unit()
        return a to (n cross a)
    }

    /** Which side of a hold a view from `towards` is of (SIDE_*): straight on within half Need.sidesDeg, else the side it's most round to. Null if its need has no sides. */
    fun sideOf(s: State, towards: Vec3): Int? {
        val round = needFor(s).sidesDeg ?: return null
        val n = s.target.normal
        if (angleDeg(towards, n) < round / 2) return SIDE_FRONT
        val (a, b) = faceAxes(n)
        val x = towards dot a; val y = towards dot b
        return if (kotlin.math.abs(x) >= kotlin.math.abs(y)) (if (x > 0) SIDE_RIGHT else SIDE_LEFT) else (if (y > 0) SIDE_ABOVE else SIDE_BELOW)
    }

    /** Where a phone would be for a close view of a hold from one of its sides, in the mesh's coordinates. */
    fun sideSpot(s: State, side: Int): Vec3 {
        val n = s.target.normal
        val (a, b) = faceAxes(n)
        val round = (needFor(s).sidesDeg ?: 0.0) * kotlin.math.PI / 180
        val dir = when (side) { SIDE_RIGHT -> a; SIDE_LEFT -> -a; SIDE_ABOVE -> b; SIDE_BELOW -> -b; else -> Vec3.ZERO }
        val out = if (side == SIDE_FRONT) n else n * cos(round) + dir * kotlin.math.sin(round)
        return s.target.point + out * ((closeM(s) ?: MIN_STANDOFF_M) / metresPerUnit)
    }

    /**
     * The sides a hold wanted in detail is to be seen from: straight on, and each of its left, right, above and below
     * that a phone can get to (not in the ground, not above reach, not with rock in the way).
     */
    fun sidesWanted(s: State): Set<Int> {
        if (needFor(s).sidesDeg == null) return emptySet()
        s.wanted?.takeIf { s.wantedAt == metresPerUnit }?.let { return it }
        val out = HashSet<Int>(); out.add(SIDE_FRONT)
        for (side in SIDE_LEFT..SIDE_BELOW) {
            val from = sideSpot(s, side)
            val up = (from.y - plan.frame.groundY) * metresPerUnit
            if (up < 0.15 || up > REACH_M + 0.2) continue
            if (guide != null && !guide.clear(from, s.target.point, 0.3 * from.distance(s.target.point))) continue
            out.add(side)
        }
        s.wanted = out; s.wantedAt = metresPerUnit
        return out
    }

    /** The steps of a hold's ladder that are needed, not only asked for on the way in: its nearest Need.ties, which tie its close photos to the rest. */
    fun tiesWanted(s: State): IntRange = 1..min(needFor(s).ties, rungCount(s))
    private fun tiesMissing(s: State): List<Int> = tiesWanted(s).filter { it !in s.rungs }

    private fun fineEnough(s: State): Boolean {
        val want = needGsdM(s) ?: return true
        val best = bestGsdM(s) ?: return false
        return best <= want * (if (needFor(s).closeM != null) DETAIL_SLACK else 1.0)
    }

    /** What each hold in scope has got so far, for the capture's record and its screen. */
    fun reached(): List<RecordHold> = states.filter { it.target.isHold && (if (scope.set != null) detail(it) else inScope(it)) }.map { s ->
        val sides = sidesWanted(s); val ties = tiesWanted(s)
        RecordHold(s.target.id, s.target.holds, s.target.p, if (detail(s)) s.target.detailM else null, bestGsdM(s)?.let { kotlin.math.round(it * 1e5) / 100 },
            goodViews(s), needViews(s), s.sides.sorted().map { SIDE_NAMES[it] }, sides.sorted().map { SIDE_NAMES[it] }, ties.count { it in s.rungs }, ties.count(),
            s.card, s.skipped == null && done(s))
    }

    /** How a hold wanted in detail stands, in a line. */
    fun status(s: State): String {
        val fine = bestGsdM(s)?.let { "${kotlin.math.round(it * 1e5) / 100} mm a pixel" } ?: "no close photo yet"
        val sides = sidesWanted(s)
        return "$fine · ${min(goodViews(s), needViews(s))} of ${needViews(s)} angles" + (if (sides.isNotEmpty()) " · ${s.sides.count { it in sides }} of ${sides.size} sides" else "") +
            tiesWanted(s).let { if (it.isEmpty()) "" else " · ${it.count { k -> k in s.rungs }} of ${it.count()} from further off" }
    }

    /**
     * How many steps a target's ladder has: photos wanted on the way in, each `ladder` times coarser than the last,
     * from what it needs up to how finely its earlier photos saw it (or LADDER_TOP_M, if they saw it finer or not at all).
     */
    fun rungCount(s: State): Int {
        val by = needFor(s).ladder?.takeIf { it > 1.01 } ?: return 0
        val need = needGsdM(s) ?: return 0
        val top = min(LADDER_TOP_M, s.target.haveGsd?.let { it * metresPerUnit } ?: LADDER_TOP_M)
        return floor(ln(top / need) / ln(by) + 0.25).toInt().coerceIn(0, LADDER_MOST)
    }

    /** Which step of its ladder a view this fine is (1 the finest), if any: within half a step either side of it. */
    fun rungAt(s: State, gsdM: Double): Int? {
        val n = rungCount(s)
        if (n == 0 || gsdM <= 0) return null
        val by = needFor(s).ladder!!; val need = needGsdM(s)!!
        val k = (ln(gsdM / need) / ln(by)).roundToInt()
        return k.takeIf { it in 1..n }
    }

    /** A step of the ladder as a view to take: how fine. */
    private fun rungGsdM(s: State, k: Int): Double = needGsdM(s)!! * needFor(s).ladder!!.pow(k)

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
        if (forTexture(s)) return s.textured
        if (needGsdM(s) == null) return s.dirs.size >= needViews(s)
        if (!fineEnough(s) || goodViews(s) < needViews(s) || wantsTexture(s)) return false
        return s.sides.containsAll(sidesWanted(s)) && tiesMissing(s).isEmpty()
    }

    /** From 0 to 1, how near done a target is. */
    fun fraction(s: State): Double {
        if (forTexture(s)) return if (s.textured) 1.0 else 0.0
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
        if (t.isView) {     // not something in the picture: somewhere to take one from, looking the way an earlier photo did
            val dm = pose.centre.distance(p) * metresPerUnit
            if (dm > plan.bridge.withinM || angleDeg(pose.forward, t.normal) > plan.bridge.deg) return null
            return Seen(0.0, dm, 1.0, -pose.forward, pose.lens.cx, pose.lens.cy)
        }
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

    private fun newDirection(s: State, towards: Vec3): Boolean = s.dirs.none { angleDeg(it, towards) < viewDeg(s) }

    private fun counts(s: State, seen: Seen): Boolean {
        val need = needGsdM(s) ?: return true
        return seen.gsdM <= need * GOOD_ENOUGH
    }

    /** Counts a photo taken from this camera. Returns the targets it did something for. */
    fun addShot(pose: CameraPose): List<String> {
        if (pose.lens != detailLens) lens(pose.lens)
        val helped = ArrayList<String>()
        for (s in states) {
            val seen = see(pose, s) ?: continue
            s.shots++
            var better = false
            if (s.bestGsdM == null || seen.gsdM < s.bestGsdM!!) { better = s.bestGsdM == null || seen.gsdM < s.bestGsdM!! * 0.85; s.bestGsdM = seen.gsdM }
            if (counts(s, seen) && newDirection(s, seen.towards)) { s.dirs.add(seen.towards); better = true }
            if (!s.target.isView) rungAt(s, seen.gsdM)?.let { if (s.rungs.add(it)) better = true }
            if (counts(s, seen)) sideOf(s, seen.towards)?.let { if (s.sides.add(it)) better = true }
            if (textures(s, seen)) { s.textured = true; better = true }
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
            if (!s.target.isView && rungAt(s, seen.gsdM)?.let { it !in s.rungs } == true) { n++; continue }      // a step on the way in, not yet taken
            if (counts(s, seen) && sideOf(s, seen.towards)?.let { it !in s.sides && it in sidesWanted(s) } == true) { n++; continue }
            if (textures(s, seen)) { n++; continue }
            if ((counts(s, seen) && newDirection(s, seen.towards)) || (finer && wanted && counts(s, seen))) n++
        }
        return n
    }

    /** Holds of sets kept private that are still to photograph: asked about once the rest is done. */
    fun anonLeft(): Int = states.count { it.target.anon && it.skipped == null && !done(it) }

    fun skip(id: String, why: String) { byId[id]?.skipped = why }
    fun unskip(id: String) { byId[id]?.skipped = null }

    fun progress(): Progress {
        val mine = states.filter { inScope(it) }
        return Progress(mine.size, mine.count { it.skipped == null && done(it) }, mine.count { it.skipped != null },
            mine.count { it.target.isHold }, mine.count { it.target.isHold && it.skipped == null && done(it) })
    }

    /** The target to go for next from here: one in frame if any is still to do (holds first), else the least turn away. */
    fun pick(pose: CameraPose, current: String? = null): State? {
        // first, the views its earlier photos were taken from, nearest first: the new photos are tied to the old through them
        states.filter { it.target.isView && inScope(it) && it.skipped == null && !done(it) }
            .minByOrNull { pose.centre.distance(it.target.point) - (if (it.target.id == current) 0.5 / metresPerUnit else 0.0) }?.let { return it }
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

    /**
     * What to do about a target from here, in words.
     *
     * @param uprightDeg how far clockwise the picture is turned to be shown upright on the screen (90 for a phone held
     *   upright, whose camera reads its picture out on its side): left, right, up and down are the screen's.
     */
    fun hint(pose: CameraPose, s: State, uprightDeg: Int = 0): Hint {
        val t = s.target
        val c = pose.rot.conj().rotate(t.point - pose.centre)
        val d = c.norm
        if (t.isView) {
            val dm = d * metresPerUnit
            if (dm > plan.bridge.withinM) {      // which way to walk: where the spot is, across the screen
                val (sx, sy) = toScreen(c.x, c.y, uprightDeg)
                val n = hypot(sx, sy).let { if (it > 1e-9) it else 1.0 }
                val behind = c.z < 0
                return Hint(Hint.Kind.GO, "Walk ${metres(dm)} to the ring${if (behind) " behind you" else ""}: an earlier photo was taken from there", t.id,
                    turnX = if (behind) (if (sx >= 0) 1.0 else -1.0) else sx / n, turnY = if (behind) 0.0 else -sy / n, moveM = dm)
            }
            if (angleDeg(pose.forward, t.normal) > plan.bridge.deg) {
                val want = pose.rot.conj().rotate(t.normal)     // the way that photo looked, as the camera has it
                val (sx, sy) = toScreen(want.x, want.y, uprightDeg)
                val n = hypot(sx, sy).let { if (it > 1e-9) it else 1.0 }
                return Hint(Hint.Kind.TURN, "Now look at the rock as that photo did", t.id, turnX = sx / n, turnY = -sy / n)
            }
            return Hint(Hint.Kind.SHOOT, "Hold still", t.id)
        }
        val at = pose.project(t.point)
        val mx = pose.lens.w * FRAME_MARGIN; val my = pose.lens.h * FRAME_MARGIN
        val what = if (t.isHold) "the ${t.role ?: ""} hold".replace("  ", " ") else if (t.isEdge) "the edge" else "this part"
        if (at == null || at.u < mx || at.u > pose.lens.w - mx || at.v < my || at.v > pose.lens.h - my) {
            val (sx, sy) = toScreen(c.x, c.y, uprightDeg)
            if (c.z < 0 && hypot(c.x, c.y) < -c.z) return Hint(Hint.Kind.TURN, "Turn round: $what is behind you", t.id, turnX = if (sx >= 0) 1.0 else -1.0)
            val n = hypot(sx, sy).let { if (it > 1e-9) it else 1.0 }
            val across = sx / n; val down = sy / n       // y runs down the screen
            val word = if (kotlin.math.abs(across) >= kotlin.math.abs(down)) (if (across > 0) "right" else "left") else (if (down > 0) "down" else "up")
            return Hint(Hint.Kind.TURN, "Point the camera $word, at $what", t.id, turnX = across, turnY = -down)
        }
        val towards = (pose.centre - t.point).unit()
        val facing = t.normal dot towards
        val dm = d * metresPerUnit
        if (t.isEdge) {
            return Hint(Hint.Kind.EDGE, "The mesh ends here: show what's past this edge, from a few steps apart", t.id, u = at.u, v = at.v)
        }
        if (facing < (if (wantsTexture(s)) cosTex(s) else cosFacing)) {
            // where facing it would be: out along its normal; which side of the view that is
            val want = pose.rot.conj().rotate(t.point + t.normal * d - pose.centre)
            val (wx, wy) = toScreen(want.x, want.y, uprightDeg)
            val word = if (kotlin.math.abs(wx) >= kotlin.math.abs(wy)) (if (wx > 0) "right" else "left") else (if (wy > 0) "lower" else "higher")
            val n = hypot(wx, wy).let { if (it > 1e-9) it else 1.0 }
            return Hint(Hint.Kind.FACE, "Move $word to face $what more squarely", t.id, turnX = wx / n, turnY = -wy / n, u = at.u, v = at.v)
        }
        if (dm < closestM(s)) return Hint(Hint.Kind.BACK, "Too close to focus: move back a little", t.id, moveM = closestM(s) - dm, u = at.u, v = at.v)
        if (closeM(s) != null) return detailHint(pose, s, at.u, at.v, dm, facing, towards, uprightDeg, what)
        val need = needGsdM(s)
        val gsd = dm / pose.lens.fx / max(facing, 0.2)
        if (wantsTexture(s)) {      // one for its texture first: square on (which it is, by here) and fine enough
            val fine = texGsdM(s)
            if (gsd > fine) return Hint(Hint.Kind.CLOSER, "Move ${metres(dm - fine * pose.lens.fx * max(facing, 0.2))} closer to $what", t.id, moveM = dm - fine * pose.lens.fx * max(facing, 0.2), u = at.u, v = at.v)
            return Hint(Hint.Kind.SHOOT, "Hold still", t.id, u = at.u, v = at.v)
        }
        // on the way in: a photo from each step of its ladder that's still to take, before going closer
        if (need != null && gsd > need && !done(s) && rungCount(s) > 0) {
            val here = rungAt(s, gsd)
            if (here != null && here !in s.rungs) return Hint(Hint.Kind.SHOOT, "Hold still: one from here, on the way in to $what", t.id, u = at.u, v = at.v)
            val next = (1..rungCount(s)).filter { it !in s.rungs && rungGsdM(s, it) < gsd }.maxOrNull()     // the nearest step further in that's still to take
            if (next != null) {
                val closer = dm - rungGsdM(s, next) * pose.lens.fx * max(facing, 0.2)
                if (closer > 0.05) return Hint(Hint.Kind.CLOSER, "Move ${metres(closer)} closer to $what", t.id, moveM = closer, u = at.u, v = at.v)
            }
        }
        // too coarse to count at all, or fine enough to count but with no view yet as fine as it needs
        if (need != null && (gsd > need * GOOD_ENOUGH || (gsd > need && (bestGsdM(s) ?: Double.MAX_VALUE) > need))) {
            val enough = need * pose.lens.fx * max(facing, 0.2)       // metres off at which it's seen finely enough
            val closer = dm - enough
            return Hint(Hint.Kind.CLOSER, "Move ${metres(closer)} closer to $what", t.id, moveM = closer, u = at.u, v = at.v)
        }
        val seen = Seen(gsd, dm, facing, towards, at.u, at.v)
        if (!newDirection(s, towards)) {
            val step = dm * tan(viewDeg(s) * 1.3 * kotlin.math.PI / 180)
            return Hint(Hint.Kind.ANGLE, "Good. Step ${metres(step)} to one side for another angle on $what", t.id, moveM = step, u = at.u, v = at.v)
        }
        return if (counts(s, seen)) Hint(Hint.Kind.SHOOT, "Hold still", t.id, u = at.u, v = at.v) else Hint(Hint.Kind.NONE, "", t.id, u = at.u, v = at.v)
    }

    /**
     * What to do about a hold wanted in detail that's in the picture and faced: its close view straight on first (the
     * one that says how finely it's seen), then its sides, then other angles, then the photos from further off that tie
     * the close ones to the rest. Steps of its ladder are taken as they're passed on the way in.
     */
    private fun detailHint(pose: CameraPose, s: State, u: Double, v: Double, dm: Double, facing: Double, towards: Vec3, uprightDeg: Int, what: String): Hint {
        val t = s.target
        val fx = pose.lens.fx
        val close = closeM(s) ?: return Hint(Hint.Kind.NONE, "", t.id, u = u, v = v)
        val want = needGsdM(s) ?: (close / fx)
        val slant = max(facing, 0.2)
        val gsd = dm / fx / slant
        val seen = Seen(gsd, dm, facing, towards, u, v)
        fun still(text: String = "Hold still") = Hint(Hint.Kind.SHOOT, text, t.id, u = u, v = v)
        fun go(spot: Vec3, text: String, kind: Hint.Kind): Hint {      // towards somewhere to hold the phone: an arrow across the screen
            val c = pose.rot.conj().rotate(spot - pose.centre)
            val (sx, sy) = toScreen(c.x, c.y, uprightDeg)
            val n = hypot(sx, sy).let { if (it > 1e-9) it else 1.0 }
            return Hint(kind, text, t.id, turnX = sx / n, turnY = -sy / n, moveM = pose.centre.distance(spot) * metresPerUnit, u = u, v = v)
        }
        /** Nearer, to where this view counts; or if that's nearer than the lens focuses (it's too far round), round to face it more. */
        fun nearer(): Hint {
            val enough = want * GOOD_ENOUGH * 0.9 * fx * slant
            return if (enough >= closestM(s) && dm > enough) Hint(Hint.Kind.CLOSER, "Move ${metres(dm - enough)} closer to $what", t.id, moveM = dm - enough, u = u, v = v)
                else go(sideSpot(s, SIDE_FRONT), "Come round to face $what more squarely", Hint.Kind.FACE)
        }
        val here = rungAt(s, gsd)
        if (gsd > want * DETAIL_SLACK && here != null && here !in s.rungs) return still("Hold still: one from here, on the way in to $what")
        if (!fineEnough(s)) {
            if (sideOf(s, towards) != SIDE_FRONT) return go(sideSpot(s, SIDE_FRONT), "Move to face $what straight on", Hint.Kind.FACE)
            if (dm > close) return Hint(Hint.Kind.CLOSER, "Move ${metres(dm - close * 0.9)} closer: $what should fill the picture", t.id, moveM = dm - close * 0.9, u = u, v = v)
            return still()
        }
        val missing = sidesWanted(s) - s.sides
        if (missing.isNotEmpty()) {
            val mine = sideOf(s, towards)
            if (mine != null && mine in missing) return if (counts(s, seen)) still() else nearer()
            val side = missing.minByOrNull { pose.centre.distance(sideSpot(s, it)) }!!
            return go(sideSpot(s, side), "Now $what from ${SIDE_WORDS[side]}, as close", Hint.Kind.ANGLE)
        }
        if (goodViews(s) < needViews(s)) {
            if (!counts(s, seen)) return nearer()
            if (newDirection(s, towards)) return still()
            val step = dm * tan(viewDeg(s) * 1.3 * kotlin.math.PI / 180)
            return Hint(Hint.Kind.ANGLE, "Good. Another angle on $what: ${metres(step)} to one side, between the ones so far", t.id, moveM = step, u = u, v = v)
        }
        val ties = tiesMissing(s)
        if (ties.isNotEmpty()) {
            if (here != null && here in ties) return still("Hold still: one with the rock round it")
            val k = ties.minByOrNull { kotlin.math.abs(rungGsdM(s, it) - gsd) }!!
            val off = rungGsdM(s, k) * fx * slant
            return if (off > dm) Hint(Hint.Kind.BACK, "Step back to ${metres(off)} from $what: one with the rock round it, which ties the close ones in", t.id, moveM = off - dm, u = u, v = v)
                else Hint(Hint.Kind.CLOSER, "Move ${metres(dm - off)} closer to $what: one with the rock round it", t.id, moveM = dm - off, u = u, v = v)
        }
        return Hint(Hint.Kind.NONE, "", t.id, u = u, v = v)
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
        const val LADDER_TOP_M = 0.008      // the coarsest step of a ladder: 8 mm of rock a pixel, about how web photos see a boulder
        const val LADDER_MOST = 8
        const val REGION_FILL = 1.1         // a hold's region with this much room round it fits the picture's shorter side
        const val CLOSEST = 0.8             // of how far off a hold wanted in detail is asked for: nearer is taken as out of focus
        const val DETAIL_SLACK = 1.2        // and its closest view this much coarser than asked is as fine as asked (not quite square on, or a little far)
        const val SIDE_FRONT = 0; const val SIDE_LEFT = 1; const val SIDE_RIGHT = 2; const val SIDE_ABOVE = 3; const val SIDE_BELOW = 4
        val SIDE_NAMES = listOf("front", "left", "right", "above", "below")
        val SIDE_WORDS = listOf("straight on", "its left", "its right", "above", "below")
        const val CLOSE_SCALE_MOST = 2.0    // at most twice as far off as asked: a lens that can't focus at 30 cm is another problem
        const val LEVEL_OUT = 0; const val LEVEL_TODO = 1; const val LEVEL_PART = 2; const val LEVEL_DONE = 3; const val LEVEL_SKIPPED = 4

        /** Across and down the screen, from across and down the picture, for a picture shown turned uprightDeg clockwise. */
        fun toScreen(x: Double, y: Double, uprightDeg: Int): Pair<Double, Double> = when (((uprightDeg % 360) + 360) % 360) {
            90 -> -y to x
            180 -> -x to -y
            270 -> y to -x
            else -> x to y
        }

        internal fun metres(m: Double): String = if (m < 0.95) "${max(5, ((m * 100).roundToInt() / 5) * 5)} cm" else "${(m * 10).roundToInt() / 10.0} m"
    }
}
