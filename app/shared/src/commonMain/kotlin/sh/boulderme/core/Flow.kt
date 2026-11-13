package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.round

/** Where a capture is: lining the mesh up (a tap on the small model, then on the rock), checking the fit, or taking photos. */
enum class Phase { STARTING, GHOST, ROCK, CONFIRM, CENTRE, CAPTURE, FAILED }

/**
 * What the capture screen shows over the camera. `sensors`: what was wrong with the phone's readings at the last photo
 * (its compass, its GPS, how still it was), which the photo was kept in spite of; empty if nothing was.
 */
data class CaptureUi(
    val phase: Phase = Phase.STARTING, val text: String = "Starting the camera…", val turnX: Double = 0.0, val turnY: Double = 0.0,
    val total: Int = 0, val done: Int = 0, val holds: Int = 0, val holdsDone: Int = 0, val kept: Int = 0, val rejected: Int = 0,
    val note: String = "", val tracking: Boolean = false, val auto: Boolean = true, val canSkip: Boolean = false, val wide: Boolean = false,
    val marking: Boolean = false, val pairs: Int = 0, val guided: Boolean = true, val sensors: String = "",
)

/**
 * What to draw for a camera frame, and whether to take a photo now.
 *
 * @param guide where to draw the guide mesh (its mesh-to-world fit), or null for not at all; solid: as a thing of its
 *   own (the small model) rather than a wash over the rock; alpha: how much of each face's see-throughness to keep.
 * @param levels how done each of the guide's patches is (Coverage.LEVEL_*), when that's changed since the last frame.
 * @param shoot take a photo now, and hand it to shot() (or say noShot() if it couldn't be taken).
 */
class FlowOut(val ui: CaptureUi, val guide: Similarity?, val solid: Boolean, val alpha: Float, val levels: IntArray?, val markers: List<Marker>, val shoot: Boolean)

/**
 * A capture as its screen goes through it, for both phones' apps: they tell it each camera frame, each tap and each
 * button, and draw and do what it answers. Not safe to call from two threads at once: an app that judges its photos
 * off its drawing thread keeps a lock round every call.
 *
 * With a mesh to add to: a small model of it is put in front of the phone; a spot tapped on it, then the same spot
 * tapped on the real rock, twice, line the mesh up (GHOST, ROCK); the fit is shown over the rock to check (CONFIRM), and
 * tightens against the points the phone tracks; then photos (CAPTURE). With none: the rock's middle is tapped (CENTRE).
 */
class CaptureFlow(val session: CaptureSession) {
    private val plan = session.plan
    private val guide = session.guide
    var phase = if (plan != null && guide != null) Phase.GHOST else Phase.CENTRE
        private set
    var marking = false
        private set
    var wide = false
        private set
    private var ghost: Similarity? = null
    private var ghostTurn = 0.0
    private var meshTap: Vec3? = null
    private val tapMarks = ArrayList<Marker>()
    private var wantShot = false
    private var inFlight = false
    private var rejected = 0
    private var note = ""
    private var sensorNote = ""
    private var levelsDirty = true
    private var markers: List<Marker> = emptyList()
    private val cloud = HashMap<Long, Vec3>()
    private var lastRefine = 0.0
    private var lastPose: CameraPose? = null
    private var lastTarget: String? = null
    private var ui = CaptureUi(phase = phase, guided = plan != null)

    // ---- from the screen ---------------------------------------------------------------------------------------------
    fun shutter() { wantShot = true }
    fun setAuto(on: Boolean) { session.auto = on }
    fun skip() { session.skipCurrent("skipped on the phone"); levelsDirty = true }
    fun toggleMarking() { marking = !marking }
    fun toggleWide() { wide = !wide; session.setScope(Scope(minPrio = if (wide) 1 else 2)); levelsDirty = true }
    fun turnGhost(degrees: Double) { ghostTurn += degrees * PI / 180; ghost = null }
    fun bringGhost() { ghost = null }
    fun confirmFit() { if (phase == Phase.CONFIRM) { phase = Phase.CAPTURE; levelsDirty = true; note = "" } }
    fun restartAlign() { if (plan == null) return; session.clearTaps(); tapMarks.clear(); meshTap = null; ghost = null; phase = Phase.GHOST; note = "" }
    fun addSpot() { if (phase == Phase.CONFIRM) { phase = Phase.GHOST; ghost = null; note = "" } }

    /** With no mesh: the side the hint is sending you round to can't be reached. */
    fun blockSide() { lastPose?.let { session.orbit?.blockNext(it) } }

    /** Something went wrong the person should know of (a photo that couldn't be saved). */
    fun say(what: String) { note = what }

    /**
     * A tap on the screen.
     *
     * @param from, along the ray the tapped point looks along, in the world (along a unit vector).
     * @param hit where the phone's tracking says that ray meets the real world, or null if it can't say; asked only
     *   when the tap is one on the rock.
     */
    fun tap(from: Vec3, along: Vec3, hit: () -> Vec3?) {
        when (phase) {
            Phase.GHOST -> {
                val g = ghost ?: return; val mesh = guide ?: return
                val inv = g.inverse()
                val o = inv.apply(from); val d = inv.dir(along).unit()
                val t = mesh.raycast(o, d)
                if (t == null) { note = "That tap missed the model."; return }
                val spot = o + d * t
                meshTap = spot
                tapMarks.add(Marker("m${tapMarks.size}", g.apply(spot), 0.03, Coverage.LEVEL_PART, "hold", true))
                phase = Phase.ROCK; note = ""
            }
            Phase.ROCK -> {
                val spot = meshTap ?: return
                val world = hit()
                if (world == null) { note = "The phone hasn't made out the rock there yet: move a little and tap again."; return }
                tapMarks.add(Marker("w${tapMarks.size}", world, 0.05, Coverage.LEVEL_DONE, "hold", false))
                val fit = session.tap(spot, world)
                note = ""
                phase = if (fit != null) { tapMarks.clear(); levelsDirty = true; Phase.CONFIRM } else {
                    if (session.taps.size >= 2) note = "Those are too nearly one above the other to tell which way it's turned."
                    Phase.GHOST
                }
            }
            Phase.CENTRE -> {
                val world = hit()
                if (world == null) note = "The phone hasn't made out the rock there yet: move a little and tap again."
                else { session.startOrbit(world); phase = Phase.CAPTURE; note = ""; levelsDirty = true }
            }
            Phase.CAPTURE -> if (marking) {
                val world = hit()
                if (world == null) note = "The phone hasn't made out the rock there yet: move a little and tap again."
                else { session.orbit?.addSpot(world); marking = false; levelsDirty = true; note = "" }
            }
            else -> {}
        }
    }

    // ---- tightening the fit ------------------------------------------------------------------------------------------
    /** A point the phone's tracking has found in the world: kept, one to each 5 cm, for tightening the fit against. */
    fun addPoint(x: Double, y: Double, z: Double) {
        if (plan == null || cloud.size >= CLOUD_MOST) return
        val key = ((round(x / CELL_M).toLong() and 0x1fffff) shl 42) or ((round(y / CELL_M).toLong() and 0x1fffff) shl 21) or (round(z / CELL_M).toLong() and 0x1fffff)
        if (!cloud.containsKey(key)) cloud[key] = Vec3(x, y, z)
    }

    val pointCount: Int get() = cloud.size

    /** The points to tighten the fit against, if it's time to (every few seconds once there's a fit): hand them to refine(). */
    fun refineDue(now: Double): List<Vec3>? {
        if (plan == null || session.fit == null || (phase != Phase.CONFIRM && phase != Phase.CAPTURE)) return null
        if (now - lastRefine < (if (phase == Phase.CAPTURE) REFINE_EVERY_S * 2 else REFINE_EVERY_S) || cloud.size < REFINE_LEAST) return null
        lastRefine = now
        val every = cloud.size / REFINE_POINTS + 1      // enough of them to fit to, few enough not to hold a frame up
        return cloud.values.filterIndexed { i, _ -> i % every == 0 }
    }

    fun refine(points: List<Vec3>) {
        val r = session.refine(points) ?: return
        if (phase == Phase.CONFIRM) note = "Fitted to ${r.matched} points the phone tracks, ${(r.rmsM * 1000).toInt() / 10.0} cm off."
        levelsDirty = true
    }

    // ---- each frame --------------------------------------------------------------------------------------------------
    /**
     * @param pose the camera, in the world, with the lens of the photos it takes.
     * @param uprightDeg how far clockwise its picture is turned to be shown upright (90 for a phone held upright).
     */
    fun frame(pose: CameraPose, tracking: Boolean, gyroRadS: Double, exposureS: Double, t: Double, uprightDeg: Int): FlowOut {
        lastPose = pose
        val base = ui.copy(phase = phase, kept = session.shotCount, rejected = rejected, note = note, sensors = sensorNote, auto = session.auto, wide = wide, marking = marking,
            pairs = session.taps.size, tracking = tracking)
        if (!tracking) {
            ui = base.copy(text = "Hold on: the phone is finding where it is. Move it slowly, looking at the rock.", turnX = 0.0, turnY = 0.0)
            return FlowOut(ui, null, false, 0f, null, emptyList(), false)
        }
        var levels: IntArray? = null
        fun fresh() {
            if (!levelsDirty) return
            levelsDirty = false
            levels = session.coverage?.patchLevels()
            markers = session.markers()
        }
        when (phase) {
            Phase.GHOST, Phase.ROCK -> {
                val g = ghost ?: placeGhost(pose).also { ghost = it }
                fresh()
                val n = session.taps.size
                ui = base.copy(turnX = 0.0, turnY = 0.0, text = if (phase == Phase.ROCK) "Now tap the same spot on the real rock"
                    else if (n >= 2) "Tap another spot on the small model that you can find on the rock, well to one side of the others"
                    else "Tap a spot on the small model that you can find on the rock (${n + 1} of 2). Pick two far apart, and not one above the other.")
                return FlowOut(ui, g, true, 2.5f, levels, tapMarks.toList(), false)
            }
            Phase.CONFIRM -> {
                fresh()
                ui = base.copy(turnX = 0.0, turnY = 0.0, text = "Is the coloured mesh sitting on the rock? It tightens by itself as you look it over.")
                return FlowOut(ui, session.fit, false, 0.8f, levels, markers, false)
            }
            Phase.CENTRE -> {
                ui = base.copy(text = "Tap the middle of the rock")
                return FlowOut(ui, null, false, 0f, null, emptyList(), false)
            }
            Phase.CAPTURE -> {
                val out = session.frame(FrameIn(pose, gyroRadS, exposureS, true, t, uprightDeg))
                if (out.hint.targetId != lastTarget) { lastTarget = out.hint.targetId; levelsDirty = true }      // the ring round what's asked for moves
                fresh()
                val p = out.progress
                ui = base.copy(text = out.hint.text, turnX = out.hint.turnX, turnY = out.hint.turnY, total = p.total, done = p.done + p.skipped, holds = p.holds,
                    holdsDone = p.holdsDone, canSkip = plan != null && out.hint.targetId != null)
                val shoot = (out.shoot || wantShot) && !inFlight
                if (shoot) { inFlight = true; wantShot = false }
                return FlowOut(ui, session.fit, false, 0.75f, levels, markers, shoot)
            }
            else -> { ui = base; return FlowOut(ui, null, false, 0f, null, emptyList(), false) }
        }
    }

    /** The photo frame() asked for, taken. A photo not kept is to be deleted. */
    fun shot(s: ShotIn): ShotOut {
        inFlight = false
        val out = session.shot(s)
        sensorNote = out.record.notes.joinToString(" · ")
        if (out.kept) { note = ""; levelsDirty = true } else { rejected++; note = "Not kept: ${out.why.joinToString("; ")}." }
        return out
    }

    /** The photo frame() asked for couldn't be taken. */
    fun noShot(why: String? = null) { inFlight = false; if (why != null) note = why }

    /** The small model, a metre in front and a little below the eye, the side its photos were taken from towards the phone. */
    private fun placeGhost(pose: CameraPose): Similarity {
        val g = guide!!; val pl = plan!!
        val (lo, hi) = g.bounds()
        val middle = (lo + hi) / 2.0
        val k = GHOST_ACROSS_M / pl.frame.size.coerceAtLeast(1e-9)
        val flat = Vec3(pose.forward.x, 0.0, pose.forward.z)
        val ahead = if (flat.norm > 0.2) flat.unit() else Vec3(0.0, 0.0, -1.0)       // looking straight down or up: any way will do
        val at = pose.centre + ahead * GHOST_AHEAD_M + Vec3(0.0, -0.25, 0.0)
        var from = Vec3.ZERO
        for (c in pl.cameras) from += Vec3.of(c.c) - middle
        val turn = if (Vec3(from.x, 0.0, from.z).norm > 1e-6) atan2(-ahead.x, -ahead.z) - atan2(from.x, from.z) else 0.0
        val rot = Quat.yaw(turn + ghostTurn)
        return Similarity(k, rot, at - rot.rotate(middle * k))
    }

    companion object {
        const val GHOST_ACROSS_M = 0.6      // how big the small model is drawn
        const val GHOST_AHEAD_M = 1.1
        const val CELL_M = 0.05             // tracked points are kept one to each of these
        const val CLOUD_MOST = 20000
        const val REFINE_POINTS = 2000
        const val REFINE_LEAST = 150
        const val REFINE_EVERY_S = 3.0
    }
}
