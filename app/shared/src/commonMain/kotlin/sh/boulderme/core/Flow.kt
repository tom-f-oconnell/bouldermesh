package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.round

/** Where a capture is: finding which way is down (a turn on the spot), lining the mesh up (a tap on the small model, then on the rock), checking the fit, or taking photos. */
enum class Phase { STARTING, LEVEL, GHOST, ROCK, CONFIRM, CENTRE, CAPTURE, FAILED }

/**
 * What the capture screen shows over the camera. `sensors`: what was wrong with the phone's readings at the last photo
 * (its compass, its GPS, how still it was), which the photo was kept in spite of; empty if nothing was.
 */
data class CaptureUi(
    val phase: Phase = Phase.STARTING, val text: String = "Starting the camera…", val turnX: Double = 0.0, val turnY: Double = 0.0,
    val total: Int = 0, val done: Int = 0, val holds: Int = 0, val holdsDone: Int = 0, val kept: Int = 0, val rejected: Int = 0,
    val note: String = "", val tracking: Boolean = false, val auto: Boolean = true, val canSkip: Boolean = false, val wide: Boolean = false,
    val marking: Boolean = false, val pairs: Int = 0, val guided: Boolean = true, val sensors: String = "",
    val reach: String = "",         // what counts, in words: "whole rock", "its climbs", or "the holds of <a set of beta>"
    val target: String = "",        // how the hold being photographed in detail stands
    val canCard: Boolean = false,   // a photo with a bank card beside that hold can be asked for; card: the next photo is it
    val card: Boolean = false,
)

/**
 * What to draw for a camera frame, and whether to take a photo now.
 *
 * @param guide where to draw the guide mesh (its mesh-to-world fit), or null for not at all; solid: as a thing of its
 *   own (the small model) rather than a wash over the rock; alpha: how much of each face's see-throughness to keep.
 * @param levels how done each of the guide's patches is (Coverage.LEVEL_*), when that's changed since the last frame.
 * `set`: the set of beta the capture is for, if any (an id of the plan's sets): its holds are photographed in detail,
 * as a replica of them needs, and the other holds are left.
 *
 * @param shoot take a photo now, and hand it to shot() (or say noShot() if it couldn't be taken).
 */
class FlowOut(val ui: CaptureUi, val guide: Similarity?, val solid: Boolean, val alpha: Float, val levels: IntArray?, val markers: List<Marker>, val shoot: Boolean,
              val still: Boolean = false,      // the photo to take is a full-size still, where the phone can: of a hold wanted in detail
              val card: Boolean = false)       // and it's the one with a card in it: say so in shot() (ShotIn.card)

/**
 * A capture as its screen goes through it, for both phones' apps: they tell it each camera frame, each tap and each
 * button, and draw and do what it answers. Not safe to call from two threads at once: an app that judges its photos
 * off its drawing thread keeps a lock round every call.
 *
 * With a mesh to add to: a small model of it is put in front of the phone; a spot tapped on it, then the same spot
 * tapped on the real rock, twice, line the mesh up (GHOST, ROCK); the fit is shown over the rock to check (CONFIRM), and
 * tightens against the points the phone tracks; then photos (CAPTURE). With none: the rock's middle is tapped (CENTRE).
 */
class CaptureFlow(val session: CaptureSession, val set: String? = null) {
    private val plan = session.plan
    private val guide = session.guide
    private val afterLevel = if (plan != null && guide != null) Phase.GHOST else Phase.CENTRE
    var phase = Phase.LEVEL
        private set
    private var turn = LevelTurn()
    var marking = false
        private set
    /** What counts: 1 the whole rock (what a texture of the capture's own needs), 2 its climbs, 3 only the holds of the capture's set of beta. */
    var reach = 1
        private set
    val wide: Boolean get() = reach == 1
    private val setName = set?.let { id -> plan?.sets?.firstOrNull { it.id == id }?.let { it.name ?: "this beta" } }
    private var cardNext = false
    private var cardShot = false
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
    private val cloud = HashMap<Long, Vec3>()       // what the tracking has found of the world, since it last lost its place
    private val past = ArrayList<Pair<Vec3, Int>>() // and before that, each with which spell of tracking found it
    private var block = 1                           // spells of tracking so far: a new one each time it was lost and came back
    private var lostAt: Double? = null
    private var tracked = false
    private var lastRefine = 0.0
    private var refining = false
    private var lastPose: CameraPose? = null
    private var lastTarget: String? = null
    private var ui = CaptureUi(phase = phase, guided = plan != null)

    init { session.set = set.takeIf { setName != null }; session.setScope(Scope(minPrio = reach)) }

    // ---- from the screen ---------------------------------------------------------------------------------------------
    /** Goes on without the turn on the spot: down is then as the phone's tracking has it. */
    fun skipLevel() { if (phase == Phase.LEVEL) { phase = afterLevel; note = "" } }
    fun shutter() { wantShot = true }
    fun setAuto(on: Boolean) { session.auto = on }
    fun skip() { session.skipCurrent("skipped on the phone"); levelsDirty = true }
    fun toggleMarking() { marking = !marking }
    /** What counts, round to the next: the whole rock, its climbs, and (for a set of beta) only its holds. */
    fun toggleWide() { reach = if (reach >= (if (setName != null) 3 else 2)) 1 else reach + 1; session.setScope(Scope(minPrio = reach)); levelsDirty = true }
    /** The next photo taken is one with a bank card flat on the rock beside the hold: its size, to a fraction of a millimetre. */
    fun toggleCard() { cardNext = !cardNext }
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
        if (cloud.size >= CLOUD_MOST) return
        val key = ((round(x / CELL_M).toLong() and 0x1fffff) shl 42) or ((round(y / CELL_M).toLong() and 0x1fffff) shl 21) or (round(z / CELL_M).toLong() and 0x1fffff)
        if (!cloud.containsKey(key)) cloud[key] = Vec3(x, y, z)
    }

    val pointCount: Int get() = cloud.size + past.size

    /**
     * Those points as a file to send with the capture's record (app/FORMAT.md): "BMP1", how many, then for each its
     * place in the tracking's world (metres, +Y up: three float32) and which spell of tracking found it (uint16: 1 the
     * first, as the photos' `ar.s` counts them), all little-endian.
     */
    fun points(): ByteArray {
        val all = past + cloud.values.map { it to block }
        val out = ByteArray(8 + all.size * 14)
        "BMP1".forEachIndexed { i, c -> out[i] = c.code.toByte() }
        var at = 4
        fun put(v: Int, bytes: Int = 4) { for (b in 0 until bytes) out[at++] = (v shr (8 * b)).toByte() }
        put(all.size)
        for ((p, spell) in all) { put(p.x.toFloat().toRawBits()); put(p.y.toFloat().toRawBits()); put(p.z.toFloat().toRawBits()); put(spell, 2) }
        return out
    }

    /** The name of the spell of tracking a photo was taken in: the app's own for the capture, with which spell after the first. */
    private fun spell(base: String) = if (block > 1) "$base.$block" else base

    /** Tightening the fit, as a piece of work: the fit as it was and the points to tighten it against. */
    class Tighten(val start: Similarity, val points: List<Vec3>)

    /**
     * The fit to tighten and the points to tighten it against, if it's time to (every few seconds once there's a fit).
     * tighter() does the work, which takes a while and may be done on another thread with no lock held; tightened()
     * takes its answer, back on this one.
     */
    fun refineDue(now: Double): Tighten? {
        val fit = session.fit
        if (plan == null || fit == null || refining || (phase != Phase.CONFIRM && phase != Phase.CAPTURE)) return null
        if (now - lastRefine < (if (phase == Phase.CAPTURE) REFINE_EVERY_S * 2 else REFINE_EVERY_S) || cloud.size < REFINE_LEAST) return null
        lastRefine = now
        refining = true
        val every = cloud.size / REFINE_POINTS + 1      // enough of them to fit to, few enough to be quick
        return Tighten(fit, cloud.values.filterIndexed { i, _ -> i % every == 0 })
    }

    fun tighter(work: Tighten): Registration.Refined? = session.tighter(work.start, work.points)

    fun tightened(work: Tighten, found: Registration.Refined?) {
        refining = false
        val r = found?.let { session.refineWith(work.start, it) } ?: return
        if (phase == Phase.CONFIRM) note = "Fitted to ${r.matched} points the phone tracks, ${(r.rmsM * 1000).toInt() / 10.0} cm off."
        levelsDirty = true
    }

    // ---- each frame --------------------------------------------------------------------------------------------------
    /**
     * @param pose the camera, in the world, with the lens of the photos it takes.
     * @param uprightDeg how far clockwise its picture is turned to be shown upright (90 for a phone held upright).
     * @param accel what the accelerometer reads now (the phone's axes, m/s2, upwards at rest), for the turn on the spot.
     * @param sensorOrientation how the camera's sensor is mounted (Axes.forceInImage()).
     */
    fun frame(pose: CameraPose, tracking: Boolean, gyroRadS: Double, exposureS: Double, t: Double, uprightDeg: Int,
              accel: Vec3? = null, sensorOrientation: Int = 90, stillLens: Lens? = null): FlowOut {
        lastPose = pose
        // tracking that was lost for more than a moment may have come back with its world somewhere else: photos and points
        // from then on are another spell's, tied to the earlier ones by what they show rather than by where the phone says it was
        if (!tracking) { if (tracked && lostAt == null) lostAt = t } else {
            if (lostAt?.let { t - it >= LOST_S } == true) { past += cloud.values.map { it to block }; cloud.clear(); block++ }
            lostAt = null; tracked = true
        }
        val base = ui.copy(phase = phase, kept = session.shotCount, rejected = rejected, note = note, sensors = sensorNote, auto = session.auto, wide = wide, marking = marking,
            reach = when (reach) { 1 -> "whole rock"; 2 -> "its climbs"; else -> "the holds of $setName" }, card = cardNext, target = "", canCard = false,
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
            Phase.LEVEL -> {
                if (accel != null) turn.add(pose, accel, sensorOrientation, gyroRadS)
                if (turn.filled == turn.sectors) {
                    val found = turn.solve()
                    if (found != null && found.spreadDeg <= LEVEL_SPREAD_MOST) {
                        session.level = found
                        note = "Down found: the ${found.sectors} directions agree to ${hundredths(found.spreadDeg)}°. The phone's tracking had it ${hundredths(found.tiltDeg)}° off, " +
                            "and one reading of its accelerometer would have been ${hundredths(found.offsetDeg)}° off."
                        phase = afterLevel
                    } else {
                        turn = LevelTurn()
                        note = "That turn was too unsteady to tell which way is down: once more, more slowly."
                    }
                }
                ui = base.copy(phase = phase, note = note, turnX = 0.0, turnY = 0.0, total = turn.sectors, done = if (phase == Phase.LEVEL) turn.filled else turn.sectors,
                    text = "To find which way is down: hold the phone upright, as for a photo, and turn slowly all the way round on the spot")
                return FlowOut(ui, null, false, 0f, null, emptyList(), false)
            }
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
                val out = session.frame(FrameIn(pose, gyroRadS, exposureS, true, t, uprightDeg, stillLens))
                if (out.hint.targetId != lastTarget) { lastTarget = out.hint.targetId; levelsDirty = true }      // the ring round what's asked for moves
                fresh()
                val p = out.progress
                ui = base.copy(text = out.hint.text, turnX = out.hint.turnX, turnY = out.hint.turnY, total = p.total, done = p.done + p.skipped, holds = p.holds,
                    holdsDone = p.holdsDone, canSkip = plan != null && out.hint.targetId != null, target = out.status, canCard = out.status.isNotEmpty())
                if (cardNext) ui = ui.copy(text = "Hold a bank card flat on the rock beside the hold, all of it in the picture and out of any glare, and press the shutter")
                // a card photo is taken by hand: it waits for the card to be there
                val shoot = (if (cardNext) wantShot else out.shoot || wantShot) && !inFlight
                if (shoot) { inFlight = true; wantShot = false; cardShot = cardNext }
                return FlowOut(ui, session.fit, false, 0.75f, levels, markers, shoot, out.still, shoot && cardNext)
            }
            else -> { ui = base; return FlowOut(ui, null, false, 0f, null, emptyList(), false) }
        }
    }

    /** The photo frame() asked for, taken. A photo not kept is to be deleted. */
    fun shot(s: ShotIn): ShotOut {
        inFlight = false
        val out = session.shot(s.copy(arSession = spell(s.arSession), card = s.card || cardShot))
        if (cardShot && out.kept) { cardNext = false; levelsDirty = true }
        cardShot = false
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
        const val LOST_S = 1.0              // tracking lost for longer than this starts a new spell: shorter is a blink, with its world kept
        const val LEVEL_SPREAD_MOST = 0.4   // degrees the directions of a turn on the spot may disagree by, for it to be believed

        private fun hundredths(x: Double) = kotlin.math.round(x * 100) / 100
    }
}
