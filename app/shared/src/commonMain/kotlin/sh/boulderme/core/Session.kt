package sh.boulderme.core

import kotlin.math.max

/**
 * `stillLens`: holds wanted in detail are taken as full-size stills with it (FrameOut.still).
 *
 * What the phone knows as each camera frame arrives: where it is in the world (with the lens of the photos it takes),
 * how fast it's turning, its shutter time, and how far clockwise its picture is turned to be shown upright (90 for a
 * phone held upright), which is what makes a hint's left and right the screen's.
 */
data class FrameIn(val pose: CameraPose, val gyroRadS: Double, val exposureS: Double, val tracking: Boolean, val t: Double, val uprightDeg: Int = 0,
                   val stillLens: Lens? = null)     // the lens of a full-size still, on a phone whose photos are otherwise its tracking's frames

/** What to show and do for a frame: the hint, whether to take a photo now, and how far along the capture is. */
data class FrameOut(val hint: Hint, val shoot: Boolean, val progress: Progress, val blurPx: Double,
                    val still: Boolean = false,     // a photo taken now should be a full-size still: it's of a hold wanted in detail
                    val status: String = "",        // how that hold stands, in a line
                    val steadyS: Double = 0.0,      // how long the phone has held still enough for a photo, to this frame
                    val redundant: Boolean = false) // something's still to do, but a photo from just here would add nothing to it

/** Something to draw in the world: a hold, an edge or a marked spot, coloured by how done it is (Coverage.LEVEL_*). */
data class Marker(val id: String, val world: Vec3, val radiusM: Double, val level: Int, val kind: String, val current: Boolean,
                  val role: String? = null,     // a hold's: what it's drawn in is the plan's colour for it, as the site draws it
                  val rgb: Int? = null)         // or this colour (0xRRGGBB): a gym climb's hold, its own

/**
 * A photo just taken: where from, as stored (its pose and lens are the stored picture's: CameraPose.turned() of the
 * tracking's, if the picture is turned upright to store it, by `turnedDeg` clockwise), the sensors then, and what its
 * pixels say.
 */
data class ShotIn(
    val pose: CameraPose, val sensors: SensorSample, val stats: ImageStats?, val exposureS: Double?, val iso: Double?, val focusM: Double?,
    val tracking: Boolean, val arSession: String, val t: Double, val unix: Double, val sensorOrientation: Int = 90, val gyroRadS: Double = 0.0,
    val turnedDeg: Int = 0,
    val still: Boolean = false,         // a full-size still (FrameOut.still), its pose the tracking's either side of it (span)
    val span: BlobSpan? = null, val paused: Boolean = false, val lensDerived: Boolean = false,
    val card: Boolean = false,          // a bank card lies beside the hold being photographed: kept for its size, and counted for nothing else
    val features: Features? = null,     // its corners (Features.of()), to match against the photos near it: for a warning only
    val blobs: List<Blob>? = null,      // in a gym capture: its patches of one colour (Blobs.find()), and those a hold-finding model found, if the phone has one
    val modelBlobs: List<Blob>? = null,
    val arPoints: Int? = null,          // the points the tracking was following as it was taken, and those it had found in what it shows
    val arCells: Int? = null,
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
class CaptureSession(val plan: Plan?, val guide: GuideMesh?, val id: String, val app: String, val device: String, private var started: Double) {
    val coverage: Coverage? = plan?.let { Coverage(it, guide) }
    var orbit: Orbit? = null
        private set
    /** A gym climb's holds, found by their colour, in a capture for one (CaptureFlow's gym): each a spot of the orbit. */
    var gym: Gym? = null
    var fit: Similarity? = null
        private set
    var refined: Registration.Refined? = null
        private set
    val taps = ArrayList<Pair<Vec3, Vec3>>()
    var auto = true                     // photos take themselves when one's worth taking
    /** Which way is down, if a turn on the spot found it (else the tracking's own): for photos of its spell only (levelSpell). */
    var level: Level? = null
        private set
    /** The spell of the phone's tracking (1 the first) the latest turn on the spot was in: its down holds in that world alone. */
    var levelSpell = 0
        private set
    private val levels = ArrayList<Pair<Int, RecordLevel>>()        // this visit's turns, each with its spell
    private var earlierLevels: List<RecordLevel> = emptyList()      // and those of the capture carried on, as it recorded them
    var calibration: RecordCalibration? = null
    var place: GpsAverager.Result? = null
    var set: String? = null             // the set of beta the capture is for, if one was chosen: its holds are wanted in detail
    var licence: String? = null         // what its photos are given under, and who to credit: as the app's settings say
    var credit: String? = null
    private val nearest: GuideMesh.Nearest? = guide?.let { g -> val (lo, hi) = g.bounds(); g.Nearest(max(1e-6, (hi - lo).norm / 40)) }
    private val shots = ArrayList<ShotRecord>()
    private val scenery = ArrayList<SceneryShot>()
    private class Marked(val id: String, val world: Vec3, val spell: Int, val mesh: Vec3?)
    private val marked = ArrayList<Marked>()
    val markedCount: Int get() = marked.size
    val sceneryCount: Int get() = scenery.size
    private val fields = ArrayList<Double>()
    private val sharps = ArrayList<Double>()
    private val matched = ArrayDeque<Triple<String, CameraPose, Features>>()      // kept photos' corners, the latest MATCHED_MOST
    private var current: String? = null
    private var lastPose: CameraPose? = null
    private var lastT = 0.0
    private var speed = 0.0
    private var lastShotT = -1e9
    private var steadySince: Double? = null
    private var kept = 0
    private var earlier: CaptureRecord? = null      // the capture as it was finished, if this carries it on (restore())

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

    /** Lines the mesh up by a fit found with no taps (Locate.fromLocated()): taps so far are dropped. */
    fun located(f: Similarity) { taps.clear(); refined = null; setFit(f) }

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

    /**
     * Makes the orbit's spots the gym climb's holds as now found: new ones added, those no longer found taken away,
     * and each where it's now found, asking as finely as a phone can photograph it from the ground.
     */
    fun syncGym() {
        val g = gym ?: return; val orb = orbit ?: return
        val fx = lastPose?.lens?.fx ?: shots.lastOrNull { it.kept }?.blob?.k?.fx ?: DEFAULT_FX
        val ids = g.holds.map { it.id }.toSet()
        for (s in orb.spots.filter { it.gym && it.id !in ids }) orb.removeSpot(s.id)
        for (h in g.holds) {
            val s = orb.spot(h.id) ?: orb.addSpot(h.p, h.id, gym = true)
            s.p = h.p; s.rM = h.rM; s.needGsdM = g.bestGsdM(h.p, fx)
        }
    }

    /**
     * Starts another ring all round the rock, from another height. With a mesh, the first: round its middle, kept away
     * from the height most photos so far were taken from. Returns its number, or null with a mesh not yet lined up.
     */
    fun newRing(): Int? {
        if (orbit == null) {
            val g = guide ?: return null; val f = fit ?: return null
            val (lo, hi) = g.bounds()
            val ys = shots.filter { it.kept }.mapNotNull { it.blob.ar?.p?.getOrNull(1) }.sorted()
            val usual = if (ys.isNotEmpty()) ys[ys.size / 2] else lastPose?.centre?.y ?: return null
            orbit = Orbit(f.apply((lo + hi) * 0.5), others = listOf(usual))
        }
        return orbit!!.newRing().n
    }

    /**
     * A hold tapped on the rock (`world`, in spell `spell` of the phone's tracking): to photograph close up from all round,
     * and to add to the mesh made of the capture as a set of beta of the account's. With a mesh lined up, a target of
     * its own, in the capture's set of beta if it's for one; with none, a spot of the orbit. Returns its id, or null with
     * neither yet.
     */
    fun markHold(world: Vec3, spell: Int): String? {
        val cov = coverage; val f = fit; val orb = orbit
        val id: String; var inMesh: Vec3? = null
        if (cov != null && f != null) {
            val m = f.inverse().apply(world)
            id = "p${marked.size + 1}"
            holdTarget(cov, id, m, lastPose?.let { (f.inverse().apply(it.centre) - m).unit() })
            inMesh = m
        } else if (orb != null) id = orb.addSpot(world).id.let { s -> if (marked.any { it.id == s }) "$s.$spell" else s }     // a spot of a capture carried on
        else return null
        marked.add(Marked(id, world, spell, inMesh))
        return id
    }

    /** A hold marked on the phone, as a target at `m` in the mesh: facing as the mesh does there, else `away` (towards the phone). */
    private fun holdTarget(cov: Coverage, id: String, m: Vec3, away: Vec3?) {
        val g = guide; val n = nearest
        val face = if (g != null && n != null) n.of(m, MARK_NEAR_M / cov.metresPerUnit)?.first else null
        val normal = if (face != null) g!!.normal(face) else away ?: Vec3(0.0, 0.0, 1.0)
        cov.add(Target(id, "hold", m.toList(), normal.toList(), MARK_REACH_M / cov.metresPerUnit, prio = 3, from = "phone",
            climbs = cov.scope.climbs.sorted(), sets = listOfNotNull(cov.scope.set), detailM = MARK_DETAIL_M))
    }

    /**
     * Takes up a capture finished earlier, to carry it on (CaptureFlow.reopen()): its photos, scenery and marked holds
     * are kept, and named on from. With a mesh, what its photos covered counts again, by the fit they were taken with,
     * and what was skipped stays skipped. Its tracking's world isn't this one: the rock is lined up afresh.
     */
    fun restore(was: CaptureRecord) {
        earlier = was
        started = was.started
        shots.addAll(was.shots); kept = shots.count { it.kept }
        scenery.addAll(was.scenery)
        val cov = coverage
        val fitWas = was.fit?.let { Similarity(it.scale, Quat.of(it.rot), Vec3.of(it.shift)) }
        if (cov != null && fitWas != null) cov.metresPerUnit = fitWas.scale     // until this visit's lining up says
        for (h in was.marked) {
            marked.add(Marked(h.id, Vec3.of(h.p), spellNumber(h.s), h.m?.let { Vec3.of(it) }))
            if (cov != null && h.m != null) holdTarget(cov, h.id, Vec3.of(h.m), null)
        }
        if (cov != null && fitWas != null) {
            for (s in shots) {
                val ar = s.blob.ar; val k = s.blob.k
                if (!s.kept || s.blob.ref != null || ar == null || k == null) continue
                cov.addShot(CameraPose(Vec3.of(ar.p), Quat.of(ar.q), Lens(k.fx, k.fy, k.cx, k.cy, k.w, k.h)).into(fitWas))
            }
        }
        for ((id, why) in was.skipped) cov?.skip(id, why)
        // each earlier turn on the spot holds in its own spell's world: kept as recorded, for the server to level by. A
        // record from before v 6 has one, in the first spell
        earlierLevels = was.levels.ifEmpty { listOfNotNull(was.level?.let { it.copy(s = it.s ?: firstSpellName(was)) }) }
    }

    private fun firstSpellName(was: CaptureRecord): String? = (was.shots.mapNotNull { it.blob.ar?.s } + was.scenery.map { it.ar.s }).firstOrNull()?.substringBefore('.')

    /**
     * Down as a turn on the spot found it, in spell `spell` of the phone's tracking, the directions having been asked
     * to agree within `limitDeg`: photos and frames of that spell take it from now on, those of others the tracking's own.
     */
    fun setLevel(found: Level, spell: Int, limitDeg: Double? = null) {
        level = found; levelSpell = spell
        levels.add(spell to RecordLevel(found.down.toList(), found.tiltDeg, found.offset.toList(), found.offsetDeg, found.spreadDeg, found.sectors, found.samples,
            null, found.expectedDeg, found.axisTiltDeg, found.gravity, found.forceMean, limitDeg))
    }

    /** The turn on the spot's down, if there was one and spell `spell` is the one it was found in; else null. */
    private fun levelFor(spell: Int): Level? = level?.takeIf { spell == levelSpell }

    /** The photos kept since the latest turn on the spot with its down: those the mesh is levelled by (0 with no turn). */
    fun keptWithTurn(): Int = if (level == null) 0 else
        shots.count { it.kept && it.blob.ref == null && it.blob.downSrc == "turn" && spellNumber(it.blob.ar?.s) == levelSpell }

    /** The photos kept in spell `n` of the phone's tracking (1 the first), but for those of a card: what ties that spell's scenery to the mesh. */
    fun keptInSpell(n: Int): Int = shots.count { it.kept && it.blob.ref == null && it.blob.ar != null && spellNumber(it.blob.ar.s) == n }

    /** The frames of the scenery, from spell `from` of the tracking on, in a spell with fewer than TIE_SHOTS photos kept: likely not placed. */
    fun unplacedScenery(from: Int = 1): Int {
        if (scenery.isEmpty()) return 0
        val photos = shots.filter { it.kept && it.blob.ref == null && it.blob.ar != null }.groupingBy { spellNumber(it.blob.ar!!.s) }.eachCount()
        return scenery.count { val n = spellNumber(it.ar.s); n >= from && (photos[n] ?: 0) < TIE_SHOTS }
    }

    /** Whether photos now go by a ring round the rock, rather than the mesh's targets: with no mesh, or while a ring asked for is unfinished. */
    private fun ringOn(): Boolean { val o = orbit ?: return false; return coverage == null || o.active?.let { !o.ringDone(it) } == true }
    /** The number of the ring photos go by now; 0 if none. */
    val ringNow: Int get() = if (ringOn()) orbit?.active?.n ?: 0 else 0

    // ---- as it goes --------------------------------------------------------------------------------------------------
    fun frame(f: FrameIn): FrameOut {
        lastPose?.let { was ->      // how fast the phone is moving, smoothed over about a sixth of a second
            if (f.t > lastT) { val k = 1 - kotlin.math.exp(-(f.t - lastT) / SPEED_SMOOTH_S); speed += k * (f.pose.centre.distance(was.centre) / (f.t - lastT) - speed) }
        }
        lastPose = f.pose; lastT = f.t
        val cov = coverage; val orb = orbit; val fitNow = fit
        val none = Progress(0, 0, 0, 0, 0)
        if (!f.tracking) steadySince = null
        if (!f.tracking) return FrameOut(Hint(Hint.Kind.NONE, "Hold on: the phone is finding where it is. Move it slowly over the rock."), false, cov?.progress() ?: orb?.progress() ?: none, 0.0)
        if (cov != null && !ringOn()) {
            if (fitNow == null) return FrameOut(Hint(Hint.Kind.NONE, "Line the mesh up with the rock first"), false, cov.progress(), 0.0)
            val inMesh = f.pose.into(fitNow)
            cov.lens(f.pose.lens); cov.detailLens = f.stillLens
            val progress = cov.progress()
            val target = cov.pick(inMesh, current)
            current = target?.target?.id
            val close = target != null && cov.detail(target)
            val still = close && f.stillLens != null
            val eye = if (still) inMesh.copy(lens = f.stillLens!!) else inMesh      // as the photo taken now would see it
            // how far off what's being photographed is; from a view to stand at, that's the rock, taken as a few metres off
            val far = target?.let { if (it.target.isView) VIEW_FAR_M else inMesh.centre.distance(it.target.point) * cov.metresPerUnit }
            val blur = Quality.blurPx(f.gyroRadS, speed, f.exposureS, eye.lens.fx, far)
            val steadyS = steadyFor(blur, f.t)      // counted when all's done too: Take still takes one
            if (target == null) return FrameOut(Hint(Hint.Kind.DONE, if (progress.total == 0) "Nothing to do here: widen what counts" else "Done: every part that counts is covered. Finish, or widen to the rest."), false, progress, blur, steadyS = steadyS)
            val worth = cov.gain(eye) > 0
            val hint = shootHint(cov.hint(eye, target, f.uprightDeg), worth, steadyS)
            return FrameOut(hint, auto && steadyS >= STEADY_FOR_S && worth && f.t - lastShotT >= MIN_GAP_S, progress, blur, still, if (close) cov.status(target!!) else "", steadyS, !worth)
        }
        if (orb != null) {
            val blur = Quality.blurPx(f.gyroRadS, speed, f.exposureS, f.pose.lens.fx, f.pose.centre.distance(orb.centre))
            val steadyS = steadyFor(blur, f.t)
            val useful = orb.useful(f.pose)
            val g = gym
            val raw = orb.hint(f.pose, f.uprightDeg, { s -> if (s.gym && g?.colour != null) "the ${g.colour!!.name} hold" else "the spot you marked" }, { s -> s.gym && g?.high(s.p) == true })
            current = raw.targetId?.takeIf { orb.spot(it) != null }
            val hint = shootHint(raw, useful, steadyS)
            return FrameOut(hint, auto && steadyS >= STEADY_FOR_S && useful && f.t - lastShotT >= MIN_GAP_S, orb.progress(), blur, steadyS = steadyS,
                redundant = !useful && hint.kind != Hint.Kind.DONE)
        }
        return FrameOut(Hint(Hint.Kind.NONE, "Tap the middle of the rock to start"), false, none, 0.0)
    }

    /**
     * A hint to take a photo, said as what will happen: with Auto on, that it takes itself once the phone holds still;
     * with it off, to press Take. A photo from here that would add nothing (`worth` false) isn't asked for: a step along is.
     */
    private fun shootHint(h: Hint, worth: Boolean, steadyS: Double): Hint {
        if (h.kind != Hint.Kind.SHOOT) return h
        // what the hint said besides "hold still": a ring's number before it, why this photo after it
        val at = h.text.indexOf("old still").takeIf { it >= 1 } ?: return h
        val lead = h.text.substring(0, at - 1); val why = h.text.substring(at + "old still".length).removePrefix(":").trim()
        val say = when {
            !worth -> return h.copy(kind = Hint.Kind.ANGLE, text = lead + (if (lead.isEmpty()) "G" else "g") + "ot this view: step along a little, or point at another part")
            !auto -> if (steadyS < STEADY_FOR_S) "hold still and press Take" else "press Take"
            steadyS < STEADY_FOR_S -> "hold still: the photo takes itself once the phone is steady"
            else -> "taking the photo…"
        }
        return h.copy(text = lead + (if (lead.isEmpty()) say.replaceFirstChar { it.uppercase() } else say) + if (why.isEmpty()) "" else " ($why)")
    }

    /** How long the phone has been still enough for a photo, up to this frame at `t` whose blur is `blur`: 0 if it isn't now. */
    private fun steadyFor(blur: Double, t: Double): Double {
        if (blur > STEADY_PX) { steadySince = null; return 0.0 }
        return t - (steadySince ?: t.also { steadySince = it })
    }

    /**
     * Counts a frame of the scenery (kind "walk" or "pano"), as stored: its pose is the stored picture's (turned by
     * `turnedDeg`). Returns its record, whose name is the file to write it to in the capture's scenery/.
     */
    fun sceneryShot(kind: String, pose: CameraPose, t: Double, arSession: String, turnedDeg: Int, stats: ImageStats?, blurPx: Double?): SceneryShot {
        val n = scenery.count { it.kind == kind } + 1
        val name = if (kind == "pano") "pano_${id}_${n.toString().padStart(2, '0')}.jpg" else "scn_${id}_${n.toString().padStart(3, '0')}.jpg"
        val found = levelFor(spellNumber(arSession))
        val down = Axes.downInImage(pose, found?.down ?: Vec3(0.0, -1.0, 0.0))
        val shot = SceneryShot(name, kind, t, BlobAr(arSession, pose.centre.toList(), pose.rot.toList()), pose.lens.let { BlobLens(it.fx, it.fy, it.cx, it.cy, it.w, it.h) },
            turnedDeg, stats?.sharp, blurPx, down.toList(), if (found != null) "turn" else "ar", found?.expectedDeg)
        scenery.add(shot)
        return shot
    }

    fun nextName(): String = "cap_${id}_${(shots.size + 1).toString().padStart(3, '0')}.jpg"

    /** Counts a photo just taken, named by nextName(). A photo not kept is to be deleted. */
    fun shot(s: ShotIn): ShotOut {
        val name = nextName()
        lastShotT = s.t
        val cov = coverage; val orb = orbit; val fitNow = fit
        val inMesh = if (cov != null && fitNow != null) s.pose.into(fitNow) else null
        val ring = ringOn()
        val far = when {
            ring && orb != null -> s.pose.centre.distance(orb.centre)
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
        // the hold a card photo is of: the one being asked for, if it's wanted in detail
        val cardOf = if (s.card) current?.let { cov?.state(it) }?.takeIf { cov?.detail(it) == true } else null
        if (verdict.ok) cardOf?.card = true
        // a close photo of a hold that came out soft, with the phone still: nearer than this phone focuses
        if (!verdict.ok && cov != null && inMesh != null && blur <= Quality.BLUR_PX && verdict.reasons.any { "soft" in it })
            current?.let { cov.state(it) }?.takeIf { cov.detail(it) }?.let { t ->
                val close = cov.closeM(t)
                if (close != null && inMesh.centre.distance(t.target.point) * cov.metresPerUnit < close * 1.15) cov.backOff()
            }
        // whether meshing is likely to match it with the photos near it: a warning, never a reason not to keep it
        val matching = s.features?.let { f -> Matching(f.size, neighbours(s.pose).map { (n, p, g) -> n to Features.links(f, s.pose, g, p) }) }
        val warn = ArrayList<String>()
        if (matching != null && matching.features < FEW_CORNERS) warn.add("little texture here for meshing to match (${matching.features} corners)")
        else if (matching?.best != null && matching.best!! < FEW_LINKS) warn.add("few matches with the photos near it (best ${matching.best}): meshing may not join it up")
        if (s.arPoints != null && s.arPoints < FEW_AR_POINTS) warn.add("the phone's tracking sees few points here (${s.arPoints})")
        if (verdict.ok && !s.card && s.features != null) { matched.addLast(Triple(name, s.pose, s.features)); if (matched.size > MATCHED_MOST) matched.removeFirst() }
        val helped = if (!verdict.ok || s.card) emptyList() else
            (if (inMesh != null && cov != null) cov.addShot(inMesh) else emptyList()) + (if (orb != null && (ring || cov == null)) orb.add(s.pose) else emptyList())
        // down three ways: the tracking's own; the tracking's with what a turn on the spot found (the one used, if there was
        // a turn in this photo's spell of tracking: another spell's world has a down of its own); and this photo's
        // accelerometer alone, less what the last turn found the phone adds (that's the phone's, whatever the world),
        // kept to check the others by
        val found = levelFor(spellNumber(s.arSession))
        val offset = level?.offset
        val force = s.sensors.accel?.takeIf { grades.gravityOk }?.let { Axes.forceInImage(it, s.sensorOrientation) }
        val downAcc = force?.let { -Axes.turn(if (offset != null) it - offset else it, s.turnedDeg).unit() }     // the offset is in the sensor's axes
        val downTrack = if (s.tracking) Axes.downInImage(s.pose) else null
        val downAr = if (s.tracking && found != null) Axes.downInImage(s.pose, found.down) else downTrack
        val down = downAr ?: downAcc
        // the two should agree: where they don't, with the phone still, the tracking's world has shifted or the phone was knocked
        val apart = if (downAr != null && downAcc != null) angleDeg(downAr, downAcc) else null
        val notes = if (apart != null && apart > (if (found != null) DOWN_DOUBT_DEG else 2 * DOWN_DOUBT_DEG))
            grades.notes + "down: the phone's tracking and its accelerometer are ${kotlin.math.round(apart * 10) / 10}° apart" else grades.notes
        // sunlit and shaded rock in one picture: fine for placing it, poor for a texture
        val light = s.stats?.takeIf { it.clipHigh > HARSH_CLIP && it.clipLow > HARSH_CLIP }?.let { "harsh light: sunlit and shaded rock in one picture. Cloud or full shade textures better" }
        val blob = ShotBlob(
            app = app, device = device, cap = id, i = shots.size + 1, t = s.unix,
            down = down?.toList(), downSrc = if (downAr != null) (if (found != null) "turn" else "ar") else if (downAcc != null) "accel" else null,
            downAcc = downAcc?.toList(), downTrack = downTrack?.takeIf { found != null }?.toList(), downExp = found?.expectedDeg?.takeIf { downAr != null }, turn = s.turnedDeg,
            heading = s.sensors.headingDeg?.takeIf { grades.headingOk }, headingAcc = s.sensors.headingAccDeg,
            gps = s.sensors.gps?.let { BlobGps(it.lat, it.lng, it.alt, it.accM, s.t - it.t) },
            ar = BlobAr(s.arSession, s.pose.centre.toList(), s.pose.rot.toList(), if (s.tracking) "normal" else "limited", s.span, if (s.paused) 1 else null),
            k = s.pose.lens.let { BlobLens(it.fx, it.fy, it.cx, it.cy, it.w, it.h) },
            exp = BlobExposure(s.exposureS, s.iso, s.focusM),
            q = BlobQuality(s.stats?.sharp, blur, s.stats?.clipHigh, s.stats?.clipLow, verdict.ok),
            ref = if (s.card) CARD else null, refHold = if (s.card) cardOf?.target?.holds?.firstOrNull() ?: cardOf?.target?.id else null,
            feat = if (matching == null && s.arPoints == null) null else BlobFeat(matching?.features ?: 0, matching?.links?.size ?: 0, matching?.best,
                matching?.links?.maxByOrNull { it.second }?.first, s.arPoints, s.arCells),
            still = if (s.still) 1 else null, kSrc = if (s.lensDerived) "derived" else null, lic = licence, by = credit,
        )
        val record = ShotRecord(name, blob, verdict.ok, verdict.reasons, notes + listOfNotNull(light) + warn, helped)
        shots.add(record)
        if (verdict.ok) kept++
        return ShotOut(record, verdict.ok, verdict.reasons, helped, blob.userComment())
    }

    /**
     * The kept photos to match one taken from `pose` against: looking within NEAR_DEG of the same way, nearest first
     * (a degree counted as a centimetre), the last one kept always among them if it looked that way too.
     */
    private fun neighbours(pose: CameraPose): List<Triple<String, CameraPose, Features>> {
        val near = matched.filter { angleDeg(it.second.forward, pose.forward) < NEAR_DEG }
        val ranked = near.sortedBy { it.second.centre.distance(pose.centre) * 100 + angleDeg(it.second.forward, pose.forward) }.take(NEIGHBOURS)
        val last = matched.lastOrNull()?.takeIf { it in near && it !in ranked }
        return if (last != null) ranked + last else ranked
    }

    /** Leaves the target being asked for (out of reach, or not wanted), and goes on to the next. */
    fun skipCurrent(why: String) { current?.let { if (orbit?.skipSpot(it) != true) coverage?.skip(it, why) }; current = null }

    var anon = false                    // holds of sets kept private count too: from the start if the app's setting says, or once asked at the end
    fun setScope(scope: Scope) { coverage?.scope = scope.copy(set = set, anon = anon); current = null }

    /** The holds, edges and marked spots to draw, in the world. */
    fun markers(): List<Marker> {
        val out = ArrayList<Marker>()
        val cov = coverage; val f = fit
        if (cov != null && f != null) for (s in cov.states) {
            if (s.target.kind == "patch" && s.target.id != current) continue
            if (!cov.inScope(s) && s.target.kind != "hold") continue
            val radius = if (s.target.isView) VIEW_RING_M else max(0.04, s.target.r * f.scale)
            val mine = if (cov.detail(s)) (s.target.detailM ?: radius) else radius      // a hold wanted in detail: the part of the rock that's wanted
            out.add(Marker(s.target.id, f.apply(s.target.point), mine, cov.level(s), s.target.kind, s.target.id == current, s.target.role.takeIf { s.target.isHold }))
        }
        val rgb = gym?.colour?.rgb
        orbit?.spots?.forEach { s ->
            out.add(Marker(s.id, s.p, if (s.gym) s.rM else 0.08, if (s.skipped) Coverage.LEVEL_SKIPPED else if (s.done()) Coverage.LEVEL_DONE else if (s.dirs.isNotEmpty()) Coverage.LEVEL_PART else Coverage.LEVEL_TODO,
                "hold", s.id == current, rgb = if (s.gym) rgb else null))
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
            // a capture carried on keeps its first visit's fit, in that visit's world: its photos' own downs are this one's
            fit = earlier?.fit ?: fit?.let { RecordFit(it.scale, it.rot.toList(), it.shift.toList(), it.tiltDeg, taps.size, refined?.matched, refined?.rmsM) },
            place = listOfNotNull(place?.let { RecordPlace(it.lat, it.lng, it.alt, it.accM, it.n, it.seconds) }, earlier?.place).minByOrNull { it.accM },
            calibration = calibration ?: earlier?.calibration,
            levels = earlierLevels + levels.map { (n, l) -> l.copy(s = spellName(n)) },
            downAgreeDeg = shots.filter { it.kept }.mapNotNull { r -> r.blob.down?.let { d -> r.blob.downAcc?.let { a -> angleDeg(Vec3.of(d), Vec3.of(a)) } } }
                .sorted().let { if (it.isEmpty()) null else it[it.size / 2] },
            skipped = cov?.states?.filter { it.skipped != null }?.associate { it.target.id to it.skipped!! } ?: emptyMap(),
            progress = p?.let { RecordProgress(it.total, it.done, it.skipped, it.holds, it.holdsDone) },
            detail = set?.let { id -> cov?.let { RecordDetail(id, it.reached()) } }, licence = licence, credit = credit,
            shots = shots.toList(), scenery = scenery.toList(),
            marked = marked.map { h -> RecordMarked(h.id, spellName(h.spell), h.world.toList(), h.mesh?.toList()) } +
                (gym?.let { g -> g.holds.map { RecordMarked(it.id, spellName(g.spell), it.p.toList()) } } ?: emptyList()),
            gym = gym?.record(),
        )
    }

    /** Spell `n`'s name, as its photos' ar.s have it; null before anything was taken to name it by. */
    private fun spellName(n: Int): String? = arBase()?.let { if (n > 1) "$it.$n" else it }

    /** The phone tracking's own name for the capture's session, without its spell: as its photos have it. */
    private fun arBase(): String? = (shots.firstOrNull { it.blob.ar != null }?.blob?.ar?.s ?: scenery.firstOrNull()?.ar?.s)?.substringBefore('.')

    companion object {
        const val DEFAULT_FX = 1500.0   // a phone's tracking frames' focal length, pixels, until one says
        const val MARK_NEAR_M = 0.3     // a hold marked on the phone faces the way the mesh does within this of it
        const val MARK_REACH_M = 0.15   // as a target: this far round it (the server's captureplan.HOLD_RADIUS_M)
        const val MARK_DETAIL_M = 0.075 // and the hold itself, wanted in detail (beta.HOLD_R_M)
        const val CARD = "id1-card"     // a photo's ref: a bank card (ID-1, 85.60 by 53.98 mm) flat on the rock in it
        // warnings that a photo may not join up with the others when meshed: low, as they're only said, and to be tuned
        // against which photos meshing did place (the capture's log and record have every photo's numbers)
        const val FEW_CORNERS = 40      // of up to Features' 768
        const val FEW_LINKS = 8         // matches agreeing with the tracking, with the best of the photos near it
        const val FEW_AR_POINTS = 10
        const val NEAR_DEG = 75.0
        const val NEIGHBOURS = 5
        const val MATCHED_MOST = 300
        const val HARSH_CLIP = 0.01     // this share of a picture blown out and as much black: lit and shaded rock together
        const val STEADY_PX = 1.0       // a photo takes itself only when the phone's own motion would smear it less than this
        const val STEADY_FOR_S = 0.3    // and has been for this long: a photo lands about half a second after it's asked for, and
                                        // one steady frame alone often turned out to be a pause in a swing, blurred by then
        const val MIN_GAP_S = 0.6       // and no sooner than this after the last
        const val SPEED_SMOOTH_S = 0.15
        const val USUAL_FROM = 5        // photos kept before their sharpness says what's usual
        const val VIEW_FAR_M = 3.0      // how far off the rock is taken to be from where an earlier photo was taken, for judging blur
        const val VIEW_RING_M = 0.25    // the ring drawn where an earlier photo was taken
        const val TIE_SHOTS = 8         // photos kept in a spell of tracking for its scenery to be placed: the server ties a spell by 4 of
                                        // them its mesh places (backend/frames.py LEAST), and not every photo is placed
        const val DOWN_DOUBT_DEG = 1.5  // a photo's down by the turn on the spot and by its own accelerometer further apart than this is said
    }
}
