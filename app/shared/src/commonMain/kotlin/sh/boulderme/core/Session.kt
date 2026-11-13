package sh.boulderme.core

import kotlin.math.max

/**
 * What the phone knows as each camera frame arrives: where it is in the world (with the lens of the photos it takes),
 * how fast it's turning, its shutter time, and how far clockwise its picture is turned to be shown upright (90 for a
 * phone held upright), which is what makes a hint's left and right the screen's.
 */
data class FrameIn(val pose: CameraPose, val gyroRadS: Double, val exposureS: Double, val tracking: Boolean, val t: Double, val uprightDeg: Int = 0)

/** What to show and do for a frame: the hint, whether to take a photo now, and how far along the capture is. */
data class FrameOut(val hint: Hint, val shoot: Boolean, val progress: Progress, val blurPx: Double)

/** Something to draw in the world: a hold, an edge or a marked spot, coloured by how done it is (Coverage.LEVEL_*). */
data class Marker(val id: String, val world: Vec3, val radiusM: Double, val level: Int, val kind: String, val current: Boolean)

/** A photo just taken: where from (with the lens of the photo itself), the sensors then, and what its pixels say. */
data class ShotIn(
    val pose: CameraPose, val sensors: SensorSample, val stats: ImageStats?, val exposureS: Double?, val iso: Double?, val focusM: Double?,
    val tracking: Boolean, val arSession: String, val t: Double, val unix: Double, val sensorOrientation: Int = 90, val gyroRadS: Double = 0.0,
)

/** The verdict on it: whether to keep its file, why not if not, what it helped, and the text to put in the file. */
data class ShotOut(val record: ShotRecord, val kept: Boolean, val why: List<String>, val helped: List<String>, val userComment: String)

/**
 * One capture, from lining the mesh up to the last photo: the phone's apps feed it what their sensors and camera say,
 * and show what it answers. Either guided by a mesh there already is (a plan and its guide), or of a rock with none
 * (`orbit` set by where the rock is).
 *
 * @param id this capture's own name, in its photos' names.
 * @param app the app and its version; device: the phone.
 */
class CaptureSession(val plan: Plan?, val guide: GuideMesh?, val id: String, val app: String, val device: String, private val started: Double) {
    val coverage: Coverage? = plan?.let { Coverage(it, guide) }
    var orbit: Orbit? = null
        private set
    var fit: Similarity? = null
        private set
    var refined: Registration.Refined? = null
        private set
    val taps = ArrayList<Pair<Vec3, Vec3>>()
    var auto = true                     // photos take themselves when one's worth taking
    var level: Level? = null            // which way is down, if a turn on the spot found it: else the tracking's own
    var calibration: RecordCalibration? = null
    var place: GpsAverager.Result? = null
    private val nearest: GuideMesh.Nearest? = guide?.let { g -> val (lo, hi) = g.bounds(); g.Nearest(max(1e-6, (hi - lo).norm / 40)) }
    private val shots = ArrayList<ShotRecord>()
    private val fields = ArrayList<Double>()
    private val sharps = ArrayList<Double>()
    private var current: String? = null
    private var lastPose: CameraPose? = null
    private var lastT = 0.0
    private var speed = 0.0
    private var lastShotT = -1e9
    private var kept = 0

    val mode: String get() = if (plan != null) "plan" else "orbit"
    val shotCount: Int get() = kept
    val currentTarget: String? get() = current

    // ---- lining the mesh up ------------------------------------------------------------------------------------------
    /** A spot tapped on the mesh (its coordinates) and the same spot tapped in the world. Returns the fit once two are in, if they can say. */
    fun tap(mesh: Vec3, world: Vec3): Similarity? {
        taps.add(mesh to world)
        return solveTaps()
    }

    fun clearTaps() { taps.clear(); fit = null; refined = null }

    private fun solveTaps(): Similarity? {
        val f = Registration.fromTaps(taps) ?: return null
        setFit(f)
        return f
    }

    private fun setFit(f: Similarity) { fit = f; coverage?.metresPerUnit = f.scale }

    /** How far the taps' size is from the server's, as a ratio (1: the same); null if the server's was a guess. */
    fun scaleAgainstServer(): Double? = plan?.takeIf { !it.frame.scaleGuessed }?.let { p -> fit?.let { it.scale / p.frame.metresPerUnit } }

    /** Tightens the fit against points the phone's tracking has found in the world. Returns how it went, or null if it couldn't. */
    fun refine(worldPoints: List<Vec3>): Registration.Refined? {
        val f = fit ?: return null
        return refineWith(f, tighter(f, worldPoints) ?: return null)
    }

    /**
     * The slow half of refine(): a tighter fit than `start` against these points. It reads nothing of this session that
     * changes, so an app works it out off the thread its frames are drawn on, then hands it to refineWith().
     */
    fun tighter(start: Similarity, worldPoints: List<Vec3>): Registration.Refined? {
        val g = guide ?: return null; val n = nearest ?: return null
        return Registration.refine(start, worldPoints, g, n)
    }

    /** Takes a fit tighter() made from `start`, unless the fit has been set afresh since, or this one is more likely wrong than that was. */
    fun refineWith(start: Similarity, r: Registration.Refined): Registration.Refined? {
        if (fit != start) return null
        // a fit that moved the mesh a long way, or that few points agree with, is more likely wrong than the taps were
        if (r.rmsM > 0.15 || r.fit.apply(Vec3.ZERO).distance(start.apply(Vec3.ZERO)) > 1.5) return null
        refined = r
        setFit(r.fit)
        return r
    }

    /** Starts capturing a rock with no mesh: its middle, in the world. */
    fun startOrbit(centre: Vec3) { orbit = Orbit(centre) }

    // ---- as it goes --------------------------------------------------------------------------------------------------
    fun frame(f: FrameIn): FrameOut {
        lastPose?.let { was ->      // how fast the phone is moving, smoothed over about a sixth of a second
            if (f.t > lastT) { val k = 1 - kotlin.math.exp(-(f.t - lastT) / SPEED_SMOOTH_S); speed += k * (f.pose.centre.distance(was.centre) / (f.t - lastT) - speed) }
        }
        lastPose = f.pose; lastT = f.t
        val cov = coverage; val orb = orbit; val fitNow = fit
        val none = Progress(0, 0, 0, 0, 0)
        if (!f.tracking) return FrameOut(Hint(Hint.Kind.NONE, "Hold on: the phone is finding where it is. Move it slowly over the rock."), false, cov?.progress() ?: orb?.progress() ?: none, 0.0)
        if (cov != null) {
            if (fitNow == null) return FrameOut(Hint(Hint.Kind.NONE, "Line the mesh up with the rock first"), false, cov.progress(), 0.0)
            val inMesh = f.pose.into(fitNow)
            cov.refFx = f.pose.lens.fx
            val progress = cov.progress()
            val target = cov.pick(inMesh, current)
            current = target?.target?.id
            // how far off what's being photographed is; from a view to stand at, that's the rock, taken as a few metres off
            val far = target?.let { if (it.target.isView) VIEW_FAR_M else inMesh.centre.distance(it.target.point) * cov.metresPerUnit }
            val blur = Quality.blurPx(f.gyroRadS, speed, f.exposureS, f.pose.lens.fx, far)
            if (target == null) return FrameOut(Hint(Hint.Kind.DONE, if (progress.total == 0) "Nothing to do here: widen what counts" else "Done: every part that counts is covered. Finish, or widen to the rest."), false, progress, blur)
            var hint = cov.hint(inMesh, target, f.uprightDeg)
            val steady = blur <= STEADY_PX
            val worth = cov.gain(inMesh) > 0
            if (hint.kind == Hint.Kind.SHOOT && !steady) hint = hint.copy(text = "Hold still: the phone is moving")
            return FrameOut(hint, auto && steady && worth && f.t - lastShotT >= MIN_GAP_S, progress, blur)
        }
        if (orb != null) {
            val blur = Quality.blurPx(f.gyroRadS, speed, f.exposureS, f.pose.lens.fx, f.pose.centre.distance(orb.centre))
            val steady = blur <= STEADY_PX
            var hint = orb.hint(f.pose)
            if (hint.kind == Hint.Kind.SHOOT && !steady) hint = hint.copy(text = "Hold still: the phone is moving")
            return FrameOut(hint, auto && steady && orb.useful(f.pose) && f.t - lastShotT >= MIN_GAP_S, orb.progress(), blur)
        }
        return FrameOut(Hint(Hint.Kind.NONE, "Tap the middle of the rock to start"), false, none, 0.0)
    }

    fun nextName(): String = "cap_${id}_${(shots.size + 1).toString().padStart(3, '0')}.jpg"

    /** Counts a photo just taken, named by nextName(). A photo not kept is to be deleted. */
    fun shot(s: ShotIn): ShotOut {
        val name = nextName()
        lastShotT = s.t
        val cov = coverage; val orb = orbit; val fitNow = fit
        val inMesh = if (cov != null && fitNow != null) s.pose.into(fitNow) else null
        val far = when {
            inMesh != null && cov != null -> current?.let { cov.state(it) }?.let { if (it.target.isView) VIEW_FAR_M else inMesh.centre.distance(it.target.point) * cov.metresPerUnit }
            orb != null -> s.pose.centre.distance(orb.centre)
            else -> null
        }
        val blur = Quality.blurPx(s.gyroRadS, speed, s.exposureS ?: 0.01, s.pose.lens.fx, far)
        val usual = if (sharps.size >= USUAL_FROM) sharps.sorted()[(sharps.size * 3) / 4] else null
        val verdict = Quality.judge(s.stats, blur, s.tracking, usual)
        if (verdict.ok) s.stats?.takeIf { it.detail >= Quality.DETAIL_LEAST }?.let { sharps.add(it.sharp) }
        s.sensors.mag?.norm?.let { fields.add(it) }
        val usualField = if (fields.size >= 8) fields.sorted()[fields.size / 2] else null
        val grades = Sensors.grade(s.sensors, usualField, s.t)
        val helped = if (!verdict.ok) emptyList() else when {
            inMesh != null && cov != null -> cov.addShot(inMesh)
            orb != null -> orb.add(s.pose)
            else -> emptyList()
        }
        // down three ways: the tracking's own; the tracking's with what a turn on the spot found (the one used, if there was
        // a turn); and this photo's accelerometer alone, less what the turn found the phone adds, kept to check the others by
        val found = level
        val force = s.sensors.accel?.takeIf { grades.gravityOk }?.let { Axes.forceInImage(it, s.sensorOrientation) }
        val downAcc = force?.let { -(if (found != null) it - found.offset else it).unit() }
        val downTrack = if (s.tracking) Axes.downInImage(s.pose) else null
        val downAr = if (s.tracking && found != null) Axes.downInImage(s.pose, found.down) else downTrack
        val down = downAr ?: downAcc
        // the two should agree: where they don't, with the phone still, the tracking's world has shifted or the phone was knocked
        val apart = if (downAr != null && downAcc != null) angleDeg(downAr, downAcc) else null
        val notes = if (apart != null && apart > (if (found != null) DOWN_DOUBT_DEG else 2 * DOWN_DOUBT_DEG))
            grades.notes + "down: the phone's tracking and its accelerometer are ${kotlin.math.round(apart * 10) / 10}° apart" else grades.notes
        val blob = ShotBlob(
            app = app, device = device, cap = id, i = shots.size + 1, t = s.unix,
            down = down?.toList(), downSrc = if (downAr != null) (if (found != null) "turn" else "ar") else if (downAcc != null) "accel" else null,
            downAcc = downAcc?.toList(), downTrack = downTrack?.takeIf { found != null }?.toList(),
            heading = s.sensors.headingDeg?.takeIf { grades.headingOk }, headingAcc = s.sensors.headingAccDeg,
            gps = s.sensors.gps?.let { BlobGps(it.lat, it.lng, it.alt, it.accM, s.t - it.t) },
            ar = BlobAr(s.arSession, s.pose.centre.toList(), s.pose.rot.toList(), if (s.tracking) "normal" else "limited"),
            k = s.pose.lens.let { BlobLens(it.fx, it.fy, it.cx, it.cy, it.w, it.h) },
            exp = BlobExposure(s.exposureS, s.iso, s.focusM),
            q = BlobQuality(s.stats?.sharp, blur, s.stats?.clipHigh, s.stats?.clipLow, verdict.ok),
        )
        val record = ShotRecord(name, blob, verdict.ok, verdict.reasons, notes, helped)
        shots.add(record)
        if (verdict.ok) kept++
        return ShotOut(record, verdict.ok, verdict.reasons, helped, blob.userComment())
    }

    /** Leaves the target being asked for (out of reach, or not wanted), and goes on to the next. */
    fun skipCurrent(why: String) { current?.let { coverage?.skip(it, why) }; current = null }

    fun setScope(scope: Scope) { coverage?.scope = scope; current = null }

    /** The holds, edges and marked spots to draw, in the world. */
    fun markers(): List<Marker> {
        val out = ArrayList<Marker>()
        val cov = coverage; val f = fit
        if (cov != null && f != null) for (s in cov.states) {
            if (s.target.kind == "patch" && s.target.id != current) continue
            if (!cov.inScope(s) && s.target.kind != "hold") continue
            val radius = if (s.target.isView) VIEW_RING_M else max(0.04, s.target.r * f.scale)
            out.add(Marker(s.target.id, f.apply(s.target.point), radius, cov.level(s), s.target.kind, s.target.id == current))
        }
        orbit?.spots?.forEach { s ->
            out.add(Marker(s.id, s.p, 0.08, if (s.skipped) Coverage.LEVEL_SKIPPED else if (s.done()) Coverage.LEVEL_DONE else if (s.dirs.isNotEmpty()) Coverage.LEVEL_PART else Coverage.LEVEL_TODO, "hold", false))
        }
        return out
    }

    /** The whole capture, to send with its photos. */
    fun record(ended: Double): CaptureRecord {
        val cov = coverage
        val p = cov?.progress() ?: orbit?.progress()
        return CaptureRecord(
            id = id, app = app, device = device, mesh = plan?.mesh?.takeIf { it.isNotEmpty() }, climbs = cov?.scope?.climbs?.sorted() ?: emptyList(),
            plan = plan?.let { RecordPlan(it.key, it.made, it.guide.sha1) },
            started = started, ended = ended, mode = mode,
            fit = fit?.let { RecordFit(it.scale, it.rot.toList(), it.shift.toList(), it.tiltDeg, taps.size, refined?.matched, refined?.rmsM) },
            place = place?.let { RecordPlace(it.lat, it.lng, it.alt, it.accM, it.n, it.seconds) },
            calibration = calibration,
            level = level?.let { RecordLevel(it.down.toList(), it.tiltDeg, it.offset.toList(), it.offsetDeg, it.spreadDeg, it.sectors, it.samples) },
            downAgreeDeg = shots.filter { it.kept }.mapNotNull { r -> r.blob.down?.let { d -> r.blob.downAcc?.let { a -> angleDeg(Vec3.of(d), Vec3.of(a)) } } }
                .sorted().let { if (it.isEmpty()) null else it[it.size / 2] },
            skipped = cov?.states?.filter { it.skipped != null }?.associate { it.target.id to it.skipped!! } ?: emptyMap(),
            progress = p?.let { RecordProgress(it.total, it.done, it.skipped, it.holds, it.holdsDone) },
            shots = shots.toList(),
        )
    }

    companion object {
        const val STEADY_PX = 1.0       // a photo takes itself only when the phone's own motion would smear it less than this
        const val MIN_GAP_S = 0.6       // and no sooner than this after the last
        const val SPEED_SMOOTH_S = 0.15
        const val USUAL_FROM = 5        // photos kept before their sharpness says what's usual
        const val VIEW_FAR_M = 3.0      // how far off the rock is taken to be from where an earlier photo was taken, for judging blur
        const val VIEW_RING_M = 0.25    // the ring drawn where an earlier photo was taken
        const val DOWN_DOUBT_DEG = 1.5  // a photo's down by the turn on the spot and by its own accelerometer further apart than this is said
    }
}
