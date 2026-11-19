package sh.boulderme.core

import kotlin.coroutines.Continuation
import kotlin.coroutines.EmptyCoroutineContext
import kotlin.coroutines.startCoroutine
import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.sqrt

/**
 * Lining a mesh up with no taps: frames of the rock are placed against the mesh's own photos (where each camera was,
 * in the mesh's coordinates), and each frame's place by the phone's tracking says where that is in the world.
 *
 * Two ways place frames, behind one Locator: on the phone, against the mesh's locate pack (backend/locate.py, kept
 * with the mesh's offline copy), and on the server (POST /api/capture/locate/<mesh>), which needs signal. Whichever
 * answers with a fit that passes fromLocated()'s checks first is used, then tightened by Registration.refine() as
 * the taps' fit is.
 */

/** A frame to place: its picture (grey, w*h bytes, rows as stored) and where the phone's tracking had the camera. */
class LocateFrame(val id: Int, val grey: ByteArray, val w: Int, val h: Int, val world: CameraPose)

/**
 * A frame placed against the mesh: its camera in the mesh's coordinates (CameraPose's image axes), from `inliers`
 * matches to the mesh's points, `rmsPx` off once placed. `by`: "phone" or "server".
 */
data class Located(val id: Int, val mesh: CameraPose, val inliers: Int, val rmsPx: Double, val by: String)

/** Places frames against a mesh. Returns those it could place (none, if it couldn't place any). */
fun interface Locator {
    suspend fun locate(plan: Plan, frames: List<LocateFrame>): List<Located>
}

object Locate {
    /** Fewest matches agreeing on a frame's place for it to count. */
    const val MIN_INLIERS = 25
    /** How far off a frame's matches may be once placed, on average, in pixels, for it to count. */
    const val MAX_RMS_PX = 2.5
    /** One frame alone is trusted only with this many agreeing matches, this close: else two must agree. */
    const val ALONE_INLIERS = 100
    const val ALONE_RMS_PX = 2.0
    /** Frames' turns from the mesh to the world further apart than this, in degrees, aren't the same fit. */
    const val MAX_TURN_SPREAD_DEG = 4.0
    /** A frame whose camera lands further than this, in metres, from where its tracking had it, disagrees. */
    const val MAX_CENTRE_OFF_M = 0.25
    /** Camera centres need to be at least this far apart, in metres, to say the mesh's size from them. */
    const val MIN_BASELINE_M = 0.5

    /** How a set of placed frames was turned into a fit, or why it wasn't. */
    data class Result(val fit: Similarity?, val frames: Int, val turnSpreadDeg: Double, val centreOffM: Double,
                      val scaleFrom: String, val why: String = "")

    /**
     * The mesh-to-world fit from frames placed in both: each frame's camera in the mesh's coordinates and in the
     * world's. The turn is the frames' turns averaged; the size from how far apart their cameras are when they're far
     * enough apart, else `scale` (metres a mesh unit) if known; the shift from where the cameras are.
     *
     * @param located frames placed against the mesh, each with an id among `frames`.
     * @param scale metres a mesh unit, if known (not a guess): used when the cameras are too close together to say.
     */
    fun fromLocated(located: List<Located>, frames: List<LocateFrame>, scale: Double?): Result {
        val byId = frames.associateBy { it.id }
        // one answer for each frame (the phone and the server may both have placed it): the one with most matches
        val good = located.filter { it.inliers >= MIN_INLIERS && it.rmsPx <= MAX_RMS_PX && it.id in byId }
            .groupBy { it.id }.values.map { same -> same.maxBy { it.inliers } }
        if (good.isEmpty()) return Result(null, 0, 0.0, 0.0, "", "no frame placed with $MIN_INLIERS matches")
        // the turn taking each frame's mesh camera to its world camera (world = turn * mesh); the frames that agree
        // with each frame's, the most of them (a frame placed wrongly agrees with none, and is left out)
        val turns = good.map { l -> (byId.getValue(l.id).world.rot * l.mesh.rot.conj()).unit() }
        val groups = good.indices.map { i -> good.indices.filter { j -> quatAngleDeg(turns[i], turns[j]) <= MAX_TURN_SPREAD_DEG } }
        val ranked = groups.distinct().sortedWith(compareByDescending<List<Int>> { it.size }.thenByDescending { it.sumOf { i -> good[i].inliers } })
        if (ranked.size >= 2 && ranked[0].size == ranked[1].size && ranked[0].toSet() != ranked[1].toSet())
            return Result(null, good.size, 0.0, 0.0, "", "the frames placed disagree, as many one way as another")
        var best: Result? = null
        for (g in ranked) {
            if (g.size == 1 && (good[g[0]].inliers < ALONE_INLIERS || good[g[0]].rmsPx > ALONE_RMS_PX)) {
                if (best == null) best = Result(null, 1, 0.0, 0.0, "", if (good.size == 1) "one frame placed, too weakly to go by alone" else "the frames placed disagree")
                continue
            }
            val r = fitOf(g.map { good[it].mesh to byId.getValue(good[it].id).world }, scale)
            if (r.fit != null) return r
            if (best == null || best.fit == null && best.frames < r.frames) best = r
        }
        return best!!
    }

    private fun fitOf(pairs: List<Pair<CameraPose, CameraPose>>, scale: Double?): Result {
        val turns = pairs.map { (m, w) -> (w.rot * m.rot.conj()).unit() }
        val first = turns[0]
        var sw = 0.0; var sx = 0.0; var sy = 0.0; var sz = 0.0
        for (q in turns) {
            val s = if (q.w * first.w + q.x * first.x + q.y * first.y + q.z * first.z < 0) -1.0 else 1.0
            sw += s * q.w; sx += s * q.x; sy += s * q.y; sz += s * q.z
        }
        val rot = Quat(sw, sx, sy, sz).unit()
        val spread = turns.maxOf { quatAngleDeg(it, rot) }
        val mm = Registration.mean(pairs.map { it.first.centre }); val mw = Registration.mean(pairs.map { it.second.centre })
        val apart = pairs.maxOf { it.second.centre.distance(mw) } * 2
        var sm = 0.0; var sws = 0.0
        for ((m, w) in pairs) { sm += (m.centre - mm).let { it dot it }; sws += (w.centre - mw).let { it dot it } }
        val (k, from) = when {
            pairs.size >= 2 && apart >= MIN_BASELINE_M && sm > 1e-12 -> sqrt(sws / sm) to "cameras"
            scale != null -> scale to "plan"
            else -> return Result(null, pairs.size, spread, 0.0, "", "the mesh's size isn't known and the frames were too close together to say it")
        }
        val fit = Similarity(k, rot, mw - rot.rotate(mm * k))
        val off = pairs.maxOf { (m, w) -> fit.apply(m.centre).distance(w.centre) }
        val why = when {
            spread > MAX_TURN_SPREAD_DEG -> "the frames disagree on the turn by ${spread.toInt()}°"
            off > MAX_CENTRE_OFF_M -> "a frame lands ${(off * 100).toInt()} cm from where the phone was"
            else -> ""
        }
        return Result(if (why.isEmpty()) fit else null, pairs.size, spread, off, from, why)
    }

    /**
     * Runs a Locator from code that isn't a coroutine (neither app needs kotlinx.coroutines for it): `done` is told
     * what it placed (nothing, if it failed), on whichever thread it finished on. A Locator that never suspends (the
     * phone's) runs on the calling thread, so call this off the drawing thread.
     */
    fun start(locator: Locator, plan: Plan, frames: List<LocateFrame>, done: (List<Located>) -> Unit) {
        val work: suspend () -> List<Located> = { locator.locate(plan, frames) }
        work.startCoroutine(Continuation(EmptyCoroutineContext) { done(it.getOrDefault(emptyList())) })
    }

    private fun quatAngleDeg(a: Quat, b: Quat): Double {
        val d = abs(a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z).coerceAtMost(1.0)
        return 2 * acos(d) * 180 / kotlin.math.PI
    }
}
