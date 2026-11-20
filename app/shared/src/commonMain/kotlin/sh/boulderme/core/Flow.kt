package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.round

/**
 * Where a capture is: finding which way is down (a turn on the spot), lining the mesh up (a tap on the small model, then
 * on the rock), checking the fit, taking photos, or walking round for the scenery (SceneryWalk), which can be started
 * from taking photos and goes back to it.
 */
enum class Phase { STARTING, LEVEL, GHOST, ROCK, CONFIRM, CENTRE, CAPTURE, SCENERY, FAILED }

/**
 * What the capture screen shows over the camera. `sensors`: what was wrong with the phone's readings at the last photo
 * (its compass, its GPS, how still it was), which the photo was kept in spite of; empty if nothing was.
 */
data class CaptureUi(
    val phase: Phase = Phase.STARTING, val text: String = "Starting the camera…", val turnX: Double = 0.0, val turnY: Double = 0.0,
    val total: Int = 0, val done: Int = 0, val holds: Int = 0, val holdsDone: Int = 0, val kept: Int = 0, val rejected: Int = 0,
    val note: String = "", val tracking: Boolean = false, val auto: Boolean = true, val canSkip: Boolean = false, val wide: Boolean = false,
    val marking: Boolean = false, val pairs: Int = 0, val guided: Boolean = true, val sensors: String = "",
    val scenery: Int = 0,           // frames kept for the scenery round the rock
    val reach: String = "",         // what counts, in words: "whole rock", "its climbs", or "the holds of <a set of beta>"
    val target: String = "",        // how the hold being photographed in detail stands
    val askMore: Int = 0,           // everything asked for is done, and this many holds others marked privately are left: photograph them too?
    val canCard: Boolean = false,   // a photo with a bank card beside that hold can be asked for; card: the next photo is it
    val card: Boolean = false,
    val waiting: Boolean = false,   // Take was pressed: the photo is taken once the phone holds still
    val level: LevelUi? = null,     // how the turn on the spot is going, while it's going
    val ring: Int = 0,              // the ring round the rock photos go by now (0: none, the mesh's targets)
    val flash: Boolean = false,     // a photo is being taken now: flash the screen, so one taken by itself is seen to be
    val tieLeft: Int = 0,           // a capture carried on: photos of the rock still wanted before its scenery is placed by them
    val unplaced: Int = 0,          // frames of the scenery taken in a spell of tracking with too few photos to place them by
    val downNote: String = "",      // what became of the turn on the spot's down when the tracking lost its place: shown until dismissed
    val turnAgain: Boolean = false, // and the turn should be done again: offer it (CaptureFlow.turnAgain())
    val gym: GymUi? = null,         // a gym climb's colour, once picked
)

/**
 * How a gym climb's capture stands: its colour's name and what its holds are drawn in, how many have been found and
 * photographed close up, how widely the colour is taken (`tolerance` of `tolerances` steps, the middle the default),
 * what a tap on the picture does now (`tap`: "add" a hold of the colour, "drop" one found that isn't of the climb; null
 * neither), which finder's patches count (`finder`, of `finders`: "colour", and "model" where the phone has one), and
 * how many holds were found that haven't been checked (`unchecked`: not asked for, nor counted in `holds`, until kept).
 */
data class GymUi(val name: String, val rgb: Int, val holds: Int, val done: Int, val tolerance: Int, val tolerances: Int, val tap: String?,
                 val finder: String, val finders: List<String>, val unchecked: Int = 0)

/**
 * How the turn on the spot is going, for the screen to show beside its progress.
 *
 * @param faced each direction round the circle (LevelTurn.states()): 0 to face, 1 being read, 2 read, 3 to face again.
 * @param facing which of them the phone faces now.
 * @param wobbleNow how much the phone is moving now, as degrees of tilt it puts in the accelerometer; wobbleTurn the
 *   same over the turn so far; wobbleOk what it should stay under.
 * @param agreeDeg how well the directions read so far agree (null before there are enough to say); agreeOk the most
 *   it may come to for the turn to be believed.
 * @param tooFast the phone is turning too quickly for its readings to be used.
 */
data class LevelUi(val faced: List<Int>, val facing: Int, val wobbleNow: Double, val wobbleTurn: Double, val wobbleOk: Double,
                   val agreeDeg: Double?, val agreeOk: Double, val tooFast: Boolean,
                   val ownDeg: Double = CaptureFlow.OWN_GUESS_DEG,     // how far off the phone's own down likely is, and what says so
                   val ownFrom: String = "",
                   val canLooser: Boolean = false, val canStricter: Boolean = false,
                   val helps: Boolean = true) {    // the agreement asked for likely gives a better down than the phone's own: else warn
    /** How far off down likely comes out (9 times in 10) with the directions agreeing just within agreeOk, the phone turned about upright. */
    val expectedDeg: Double get() = LevelTurn.ERROR_PER_SPREAD * agreeOk

    /** What the agreement asked for comes to, against the phone's own down: said each time it's changed, and under the gauges. */
    fun limitText(): String = CaptureFlow.limitText(agreeOk, ownDeg, ownFrom, canStricter)

    /** How far to turn to face the nearest direction to be faced again: degrees, to the left if positive; null if none is. */
    fun redoTurnDeg(): Int? {
        val n = faced.size
        if (facing < 0) return null
        // the directions are numbered anticlockwise as seen from above: the way a turn to the left goes
        val k = (0 until n).filter { faced[it] == 3 }.map { val d = ((it - facing) % n + n) % n; if (d <= n / 2) d else d - n }
            .minByOrNull { kotlin.math.abs(it) } ?: return null
        return k * 360 / n
    }

    fun steadyText(): String = if (tooFast) "Turning too fast to read: slower" else
        "Steadiness ${tenths(wobbleNow)}° now, ${tenths(wobbleTurn)}° over the turn (keep under ${tenths(wobbleOk)}°)"

    fun agreeText(): String = agreeDeg?.let { "The directions so far agree to ${hundredths(it)}° (needs under ${hundredths(agreeOk)}°)" }
        ?: "How well the directions agree shows from ${LevelTurn.CHECK_LEAST} of them"

    fun redoText(): String? = redoTurnDeg()?.let { if (it == 0) "Face this way again, steadily"
        else "Turn ${kotlin.math.abs(it)}° to your ${if (it > 0) "left" else "right"}, to face a direction again" }

    private fun tenths(x: Double) = round(x * 10) / 10
    private fun hundredths(x: Double) = round(x * 100) / 100
}

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
              val card: Boolean = false,       // and it's the one with a card in it: say so in shot() (ShotIn.card)
              val grab: String? = null,        // keep this frame, as the tracking has it, for the scenery ("walk" or "pano"): hand it to scenery();
                                               // or "locate": its grey picture to place against the mesh, to locateFrame()
              val tick: Boolean = false)       // sound a tick now, if the app's set to: a photo from here would add nothing (TICK_S apart)

/**
 * A capture as its screen goes through it, for both phones' apps: they tell it each camera frame, each tap and each
 * button, and draw and do what it answers. Not safe to call from two threads at once: an app that judges its photos
 * off its drawing thread keeps a lock round every call.
 *
 * With a mesh to add to: a small model of it is put in front of the phone; a spot tapped on it, then the same spot
 * tapped on the real rock, twice, line the mesh up (GHOST, ROCK); the fit is shown over the rock to check (CONFIRM), and
 * tightens against the points the phone tracks; then photos (CAPTURE). With none: the rock's middle is tapped (CENTRE).
 */
/**
 * @param levelLimit degrees the turn on the spot's directions are asked to agree within (one of LEVEL_LIMITS: the app
 *   keeps the last chosen); ownTilts how far off the phone's own down was in its earlier turns (degrees, Level.tiltDeg),
 *   to say what skipping the turn costs. onSettings is told both whenever either changes, for the app to keep.
 */
/**
 * @param gym a gym climb, with no mesh: the first tap is on one of its holds, whose colour picks out the rest of them
 *   in the photos (Gym), each then asked for close up as a marked hold is; the wall is walked along, not round.
 * @param panorama frames are kept during the turn on the spot, for a panorama of what's far off; scenery the walk
 *   round for the scenery can be started (startScenery(), sceneryOn). The apps turn both off for a gym climb, unless Settings has them.
 */
class CaptureFlow(val session: CaptureSession, val set: String? = null, anonFromStart: Boolean = false,
                  levelLimit: Double = LEVEL_SPREAD_MOST, ownTilts: List<Double> = emptyList(), gym: Boolean = false,
                  private val panorama: Boolean = true, val sceneryOn: Boolean = true) {
    /** Degrees the turn on the spot's directions must agree within, for it to be believed (looser(), stricter()). */
    var levelLimit = LEVEL_LIMITS.minByOrNull { kotlin.math.abs(it - levelLimit) }!!
        private set
    /** How far off the phone's own down was in its turns so far, the latest last (at most OWN_KEPT). */
    val ownTilts: List<Double> get() = tilts.toList()
    private val tilts = ArrayList(ownTilts.takeLast(OWN_KEPT))
    var onSettings: ((Double, List<Double>) -> Unit)? = null
    private var returnTo: Phase? = null     // a turn done again (turnAgain()): where it goes back to after
    private var downNote = ""
    private var turnAgainAsked = false
    private val plan = session.plan
    private val guide = session.guide
    private val afterLevel = if (plan != null && guide != null) Phase.GHOST else Phase.CENTRE
    var phase = Phase.LEVEL
        private set
    private var turn = LevelTurn()
    private var turnSpell = 1               // the spell of tracking the turn being read is in: a new one starts it afresh
    // a limit kept from before that's no better than the phone's own (as its earlier turns, or the guess, have it) is
    // tightened to the loosest that is, if any is
    init { if (!helps(this.levelLimit)) this.levelLimit = LEVEL_LIMITS.lastOrNull { helps(it) } ?: LEVEL_LIMITS.first() }
    private var levelChecked = -1           // how many directions were read when the turn was last checked, and when
    private var levelCheckT = -1.0
    private var agree: Double? = null
    private var levelNote = false
    var marking = false
        private set
    /** What a tap on the picture does to a gym climb's holds now: "add" one of its colour, "drop" one found; null neither. */
    var gymTap: String? = null
        private set
    private val gym: Gym? = if (gym && session.plan == null) Gym().also { session.gym = it } else null
    /** What counts: 1 the whole rock (what a texture of the capture's own needs), 2 its climbs, 3 only the holds of the capture's set of beta. */
    var reach = 1
        private set
    val wide: Boolean get() = reach == 1
    private val setName = set?.let { id -> plan?.sets?.firstOrNull { it.id == id }?.let { it.name ?: "this beta" } }
    private var askedMore = anonFromStart
    private var cardNext = false
    private val pano = PanoTurn()           // frames of what's far off, from the turn on the spot
    private var walk: SceneryWalk? = null   // the walk round for the scenery, once started
    private var grabbing = false
    private var cardShot = false
    private var ghost: Similarity? = null
    private var ghostTurn = 0.0
    private var ghostScale = 1.0           // the small model's scale as first placed: zooming is kept within ZOOM_LEAST..ZOOM_MOST of it
    private var meshTap: Vec3? = null
    /** Whether the app can place frames against the mesh (Locator: on the phone with its pack, or on the server). */
    var canLocate = false
    private val locFrames = ArrayList<LocateFrame>()    // frames grabbed to place against the mesh, and those placed
    private val locFound = ArrayList<Located>()
    private var lastLocGrab = -1e9
    private var locTried = 0                            // answers had from the locators, placed or not
    private var locNext = 0                             // the next frame's id: never reused, so answers about frames from before Start again match none
    private val tapMarks = ArrayList<Marker>()      // spots tapped on the rock, in the world
    private val modelMarks = ArrayList<Vec3>()      // and on the small model, in the mesh's coordinates: they move with it
    private var wantShot = false
    private var wantAt: Double? = null    // the first frame after Take was pressed
    private var inFlight = false
    private var autoShot = false            // the photo in flight was taken by itself, not by Take
    private var shotAt = -1e9
    private var lastTick = -1e9
    private var lastShotT = -1e9
    private var rejected = 0
    private var note = ""
    private var sensorNote = ""
    private var levelsDirty = true
    private var markers: List<Marker> = emptyList()
    private val cloud = HashMap<Long, Vec3>()       // what the tracking has found of the world, since it last lost its place
    private val past = ArrayList<Pair<Vec3, Int>>() // and before that, each with which spell of tracking found it
    private var block = 1                           // spells of tracking so far: a new one each time it was lost and came back
    private var firstSpell = 1                      // this visit's first: after a capture finished earlier's, if this carries it on
    private var earlierLog = ""
    /** The phone tracking's name for a capture carried on (reopen()), which the app goes on giving its photos; null for a new one. */
    var arBase: String? = null
        private set
    private var lostAt: Double? = null
    private var tracked = false
    private var lastRefine = 0.0
    private var refining = false
    private var lastPose: CameraPose? = null
    private var lastTarget: String? = null
    private var ui = CaptureUi(phase = phase, guided = plan != null)
    /** How the capture went, as it went (EventLog): sent with it. */
    val log = EventLog()
    private var now = 0.0                   // the last frame's time: what the screen's own actions are logged at
    private var said: Phase? = null         // what the log last had of the phase, the tracking, the hint and the words on the screen
    private var saidTracking: Boolean? = null
    private var saidHint: Pair<Hint.Kind, String?>? = null
    private var saidText = ""
    private var lastHint: Hint? = null
    private var spanFrom = -1.0             // frames since then: how many, the longest gap between two, and how many untracked
    private var spanFrames = 0
    private var spanGap = 0.0
    private var spanLost = 0
    private var lastFrameT = -1.0

    init { session.set = set.takeIf { setName != null }; session.anon = anonFromStart; session.setScope(Scope(minPrio = reach)) }

    /** Something the app itself wants in the log (how its camera was set up, an error it caught): what, and what else of it. */
    fun event(e: String, vararg fields: Pair<String, Any?>) = log.add(now, e, *fields)

    /** The log as it stands, a JSON line each. */
    fun logText(): String = earlierLog + log.text()

    /**
     * Carries on a capture finished earlier (Reopen), from what was saved of it: its record, tracked points and log.
     * Down is found and the rock lined up afresh, as this visit's tracking is a world of its own: its photos and
     * frames are the next spell's, under the earlier name (arBase), and its scenery is placed by the photos of the rock
     * taken in it, so CaptureSession.TIE_SHOTS are asked for before the walk round.
     */
    fun reopen(earlier: CaptureRecord, points: ByteArray?, log: String) {
        session.restore(earlier)
        val pts = points?.let { readPoints(it) } ?: emptyList()
        past += pts
        val spells = earlier.shots.mapNotNull { it.blob.ar?.s } + earlier.scenery.map { it.ar.s } + earlier.marked.mapNotNull { it.s }
        block = ((spells.map { spellNumber(it) } + pts.map { it.second }).maxOrNull() ?: 0) + 1
        firstSpell = block
        turnSpell = block
        arBase = spells.firstOrNull()?.substringBefore('.')
        earlierLog = log.let { if (it.isEmpty() || it.endsWith("\n")) it else it + "\n" }
        event("reopen", "photos" to session.shotCount, "scenery" to session.sceneryCount, "spell" to block)
    }

    /** Photos of the rock still wanted in this visit before its scenery can be placed (0 but for a capture carried on). */
    val tieLeft: Int get() = if (firstSpell == 1) 0 else kotlin.math.max(0, CaptureSession.TIE_SHOTS - session.keptInSpell(block))

    /** The answer to "photograph the holds others marked privately too?" (CaptureUi.askMore): asked once. */
    fun more(yes: Boolean) { event("more", "yes" to yes); askedMore = true; if (yes) { session.anon = true; session.setScope(Scope(minPrio = reach)); levelsDirty = true } }

    // ---- from the screen ---------------------------------------------------------------------------------------------
    /**
     * Starts the walk round for the scenery, from taking photos: round the rock's middle (the mesh lined up, or the one
     * tapped). Returns why it can't, or null.
     */
    fun startScenery(): String? {
        if (!sceneryOn) return "Scenery is off for gym captures: Settings can turn it on."
        if (phase != Phase.CAPTURE) return "Start taking photos first."
        tieLeft.takeIf { it > 0 }?.let { return "First $it more photo${if (it == 1) "" else "s"} of the rock, from round it: this visit's scenery is placed on the mesh by them." }
        val w = walk ?: run {
            val fit = session.fit; val g = guide; val orb = session.orbit
            when {
                fit != null && g != null -> {
                    val (lo, hi) = g.bounds()
                    SceneryWalk(fit.apply((lo + hi) / 2.0), kotlin.math.hypot(hi.x - lo.x, hi.z - lo.z) / 2 * fit.scale)
                }
                orb != null -> SceneryWalk(orb.centre, ORBIT_ROCK_M)
                else -> return "Line the mesh up with the rock first."
            }
        }
        walk = w; phase = Phase.SCENERY; note = ""
        event("scenery_start", "ring_m" to w.ringM, "rock_m" to w.rockM, "frames" to w.frames)
        return null
    }

    /** Back to taking photos of the rock. */
    fun endScenery() { if (phase == Phase.SCENERY) { event("scenery_end", "frames" to walk?.frames, "done" to walk?.done); phase = Phase.CAPTURE; levelsDirty = true } }

    /**
     * The frame FlowOut.grab asked for, as stored (`pose` turned by `turnedDeg`, as a photo's is). Returns its record,
     * whose name is the file to write it to in the capture's scenery/; noGrab() if it couldn't be had.
     */
    fun scenery(kind: String, pose: CameraPose, t: Double, arSession: String, turnedDeg: Int, stats: ImageStats?, gyroRadS: Double, exposureS: Double): SceneryShot {
        grabbing = false
        if (kind == "pano") pano.add(pose) else walk?.add(pose, t)
        val shot = session.sceneryShot(kind, pose, t, spell(arSession), turnedDeg, stats, sceneryBlurPx(gyroRadS, exposureS, pose.lens.fx))
        event("scenery", "kind" to kind, "name" to shot.name, "sharp" to stats?.sharp, "blur_px" to shot.blurPx, "out" to walk?.outward(pose))
        return shot
    }

    fun noGrab(why: String? = null) { grabbing = false; if (why != null) event("no_grab", "why" to why) }

    /** Goes on without the turn on the spot: down is then as the phone's tracking has it. Ask first (skipWarning()). */
    fun skipLevel() {
        if (phase != Phase.LEVEL) return
        val (own, from) = ownDown()
        event("level_skipped", "filled" to turn.filled, "own_deg" to own, "own_from" to from, "limit_deg" to levelLimit, *turn.diagnose(LevelTurn.CHECK_LEAST).fields())
        phase = returnTo ?: afterLevel; returnTo = null; note = ""
    }

    /** What going on without the turn costs, for a prompt to confirm it before skipLevel(). */
    fun skipWarning(): String {
        val (own, from) = ownDown()
        return "Without the turn, down is the phone's own, likely about ${hundredths(own)}° off ($from). " +
            "With the turn, at the agreement asked for now, it's likely within ${hundredths(LevelTurn.ERROR_PER_SPREAD * levelLimit)}°. " +
            "A wall's steepness in the mesh is off by as much. Go on without it?"
    }

    /**
     * How far off the phone's own down likely is, and what says so: this turn's directions read so far, if enough
     * agree; else this phone's earlier turns; else a guess.
     */
    fun ownDown(): Pair<Double, String> {
        val d = turn.diagnose(LevelTurn.CHECK_LEAST)
        if (d.why == null && d.tiltDeg != null && (d.spreadDeg ?: 99.0) <= levelLimit) return d.tiltDeg to "by this turn's ${d.sectors} directions so far"
        if (tilts.isNotEmpty()) return tilts.sorted()[tilts.size / 2] to "this phone's last ${if (tilts.size == 1) "turn" else "${tilts.size} turns"}"
        return OWN_GUESS_DEG to "a guess: three Pixel 6 captures' photos put it at 0.3 to 0.4°, other phones untried"
    }

    /** Whether asking the directions to agree within `limitDeg` likely gives a better down than the phone's own. */
    fun helps(limitDeg: Double): Boolean = LevelTurn.ERROR_PER_SPREAD * limitDeg < ownDown().first

    /**
     * Asks the turn's directions to agree less closely: easier to finish, down less sure. Not so loosely that the turn
     * would likely be no better than the phone's own down: that's refused, saying why. Returns the limit then.
     */
    fun looser(): Double {
        val next = LEVEL_LIMITS.firstOrNull { it > levelLimit } ?: return levelLimit
        if (!helps(next)) {
            val (own, from) = ownDown()
            event("level_limit_refused", "deg" to next, "expected_deg" to LevelTurn.ERROR_PER_SPREAD * next, "own_deg" to own)
            note = "Not looser: at ${hundredths(next)}° down would likely be within ${hundredths(LevelTurn.ERROR_PER_SPREAD * next)}°, " +
                "no better than the phone's own (about ${hundredths(own)}° off, $from). Skip the turn instead, if it can't be held steadier."
            return levelLimit
        }
        return setLimit(next)
    }

    /** Asks them to agree more closely. Returns the new limit. */
    fun stricter(): Double = setLimit(LEVEL_LIMITS.lastOrNull { it < levelLimit } ?: levelLimit)

    private fun setLimit(deg: Double): Double {
        if (deg != levelLimit) {
            levelLimit = deg; levelChecked = -1
            event("level_limit", "deg" to deg, "expected_deg" to LevelTurn.ERROR_PER_SPREAD * deg)
            val (own, from) = ownDown()
            note = limitText(deg, own, from)
            onSettings?.invoke(levelLimit, ownTilts)
        }
        return levelLimit
    }

    /**
     * Does the turn on the spot again, in the tracking's world as it is now (after it lost its place, the earlier
     * turn's down doesn't hold here), then goes back to what was being done.
     */
    fun turnAgain() {
        event("turn_again", "phase" to phase.name, "spell" to block)
        if (phase == Phase.LEVEL) return
        returnTo = phase; phase = Phase.LEVEL
        turn = LevelTurn(); turnSpell = block; levelChecked = -1; agree = null; levelNote = false
        downNote = ""; turnAgainAsked = false; note = ""
    }

    /** Leaves the note of what became of down (CaptureUi.downNote) and the offer of a turn again. */
    fun dismissDownNote() { event("down_note_dismissed", "turn_again" to turnAgainAsked); downNote = ""; turnAgainAsked = false }
    /** Takes a photo once the phone holds still enough for it: at once if it already is, else within the next WAIT_S. */
    fun shutter() { event("shutter", "waiting" to wantShot); if (!wantShot) { wantShot = true; wantAt = null } }
    fun setAuto(on: Boolean) { event("auto", "on" to on); session.auto = on }
    fun skip() { event("skip", "target" to session.currentTarget); session.skipCurrent("skipped on the phone"); levelsDirty = true }
    fun toggleMarking() { marking = !marking; gymTap = null; event("marking", "on" to marking) }

    /** The next tap adds a hold of the gym climb's colour ("add"), or leaves out a hold found that isn't of it ("drop"); pressed again, neither. */
    fun gymTap(mode: String) { gymTap = if (gymTap == mode) null else mode; marking = false; event("gym_tap", "mode" to gymTap) }
    /** Takes the climb's colour more widely (more holds count as it) or more narrowly. */
    fun gymWider() = gymChanged("gym_wider", gym?.widen())
    fun gymNarrower() = gymChanged("gym_narrower", gym?.narrow())
    /** Which finder's patches count: "colour" or "model" (Gym.FINDER_*). */
    fun gymFinder(f: String) { val g = gym ?: return; g.useFinder(f); gymChanged("gym_finder", true, "finder" to f) }

    /** The gym climb's holds found and not yet checked, each with where it came from and where it's seen best. */
    fun gymToCheck(): List<ReviewHold> = gym?.let { g -> g.unchecked.sortedByDescending { it.photos }.map { ReviewHold(it.id, true, g.how(it), g.view(it)) } } ?: emptyList()

    /** Says the holds found `ids` (null: all those unchecked) are the climb's, to be asked for close up; `reject` those not (kept: null, all the others). */
    fun gymCheck(keep: Collection<String>?, reject: Collection<String>?) {
        val g = gym ?: return
        val was = g.unchecked.size
        if (keep == null || keep.isNotEmpty()) g.keep(keep)
        val out = if (reject == null || reject.isNotEmpty()) g.reject(reject) else 0
        session.syncGym(); levelsDirty = true
        event("gym_check", "unchecked" to was, "kept" to (keep?.size ?: (was - out)), "rejected" to out, "holds" to g.holds.count { it.ok })
        val k = g.holds.count { it.ok }
        note = "${k} ${g.colour?.name} hold${if (k == 1) "" else "s"} kept" + if (out > 0) ", $out left out" else ""
    }

    private fun gymChanged(e: String, changed: Boolean?, vararg more: Pair<String, Any?>) {
        val g = gym ?: return
        if (changed != true) { note = "It can't go any ${if (e == "gym_wider") "wider" else "narrower"}"; return }
        session.syncGym(); levelsDirty = true
        event(e, "tolerance" to g.colour?.tolerance, "holds" to g.holds.size, *more)
        note = "${g.holds.size} ${g.colour?.name} hold${if (g.holds.size == 1) "" else "s"} found so far"
    }

    /** Where a ray from the phone lands in a picture seen by `pose`: (u, v), or null if behind it. */
    private fun pixel(pose: CameraPose, from: Vec3, along: Vec3): Pair<Double, Double>? = pose.project(from + along * 2.0)?.let { it.u to it.v }
    /** What counts, round to the next: the whole rock, its climbs, and (for a set of beta) only its holds. */
    fun toggleWide() { event("reach", "from" to reach); reach = if (reach >= (if (setName != null) 3 else 2)) 1 else reach + 1; session.setScope(Scope(minPrio = reach)); levelsDirty = true }
    /** The next photo taken is one with a bank card flat on the rock beside the hold: its size, to a fraction of a millimetre. */
    fun toggleCard() { cardNext = !cardNext; event("card", "on" to cardNext) }
    fun turnGhost(degrees: Double) { event("ghost_turn", "deg" to degrees); ghostTurn += degrees * PI / 180; orbitGhost(degrees, 0.0) }
    fun bringGhost() { event("ghost_bring"); ghost = null }

    /** Whether the small model is on the screen, for drags and pinches to move it (orbitGhost(), zoomGhost(), panGhost()). */
    val modelShown: Boolean get() = ghost != null && (phase == Phase.GHOST || phase == Phase.ROCK)
    private val shownGhost: Similarity? get() = if (modelShown) ghost else null

    /** The small model's middle, in the world. */
    private fun ghostMiddle(g: Similarity): Vec3 = guide!!.bounds().let { (lo, hi) -> g.apply((lo + hi) / 2.0) }

    /** Where a ray meets the plane through the small model's middle that faces the camera (null: it doesn't, ahead). */
    private fun onGhostPlane(g: Similarity, from: Vec3, along: Vec3): Vec3? {
        val n = lastPose?.forward ?: along
        val d = along dot n
        if (d < 1e-6) return null
        val t = ((ghostMiddle(g) - from) dot n) / d
        return if (t > 0) from + along * t else null
    }

    /**
     * Turns the small model about its middle as a finger dragged across it would, as the site's viewer turns a mesh:
     * yawDeg about up (a drag sideways), pitchDeg about the camera's sideways axis (a drag up or down).
     */
    fun orbitGhost(yawDeg: Double, pitchDeg: Double) {
        val g = shownGhost ?: return
        val c = ghostMiddle(g)
        var q = Quat.yaw(yawDeg * PI / 180)
        val side = lastPose?.rot?.rotate(Vec3(1.0, 0.0, 0.0))
        if (side != null && pitchDeg != 0.0) q = Quat.axisAngle(side, pitchDeg * PI / 180) * q
        ghost = Similarity(g.scale, (q * g.rot).unit(), q.rotate(g.shift - c) + c)
    }

    /**
     * Makes the small model bigger (factor > 1) or smaller about the spot of it under a pinch (the ray from, along: the
     * middle of the fingers), or where that ray meets its middle's depth if it misses, as the site's viewer zooms to the
     * pointer.
     */
    fun zoomGhost(factor: Double, from: Vec3, along: Vec3) {
        val g = shownGhost ?: return; val mesh = guide ?: return
        val k = (g.scale * factor).coerceIn(ghostScale * ZOOM_LEAST, ghostScale * ZOOM_MOST) / g.scale
        if (k == 1.0) return
        val inv = g.inverse()
        val hit = mesh.raycast(inv.apply(from), inv.dir(along).unit())?.let { g.apply(inv.apply(from) + inv.dir(along).unit() * it) }
        val pivot = hit ?: onGhostPlane(g, from, along) ?: ghostMiddle(g)
        ghost = Similarity(g.scale * k, g.rot, pivot + (g.shift - pivot) * k)
    }

    /** Moves the small model as two fingers drag it: what was under the ray from, was, is under from, now, at its middle's depth. */
    fun panGhost(from: Vec3, was: Vec3, now: Vec3) {
        val g = shownGhost ?: return
        val a = onGhostPlane(g, from, was) ?: return; val b = onGhostPlane(g, from, now) ?: return
        ghost = g.copy(shift = g.shift + (b - a))
    }
    fun confirmFit() { if (phase == Phase.CONFIRM) { event("fit_confirmed", "taps" to session.taps.size, "refined_rms_m" to session.refined?.rmsM); phase = Phase.CAPTURE; levelsDirty = true; note = "" } }
    fun restartAlign() {
        if (plan == null) return
        event("align_restart", "phase" to phase.name); session.clearTaps(); tapMarks.clear(); modelMarks.clear(); meshTap = null; ghost = null; phase = Phase.GHOST; note = ""
        locFrames.clear(); locFound.clear(); locTried = 0; lastLocGrab = -1e9
    }

    /**
     * The frame FlowOut.grab "locate" asked for: its grey picture (as the tracking has it, not turned upright) and its
     * camera then. Returns it, for the app to hand to its Locators (off this thread), and their answers to located().
     */
    fun locateFrame(grey: ByteArray, w: Int, h: Int, pose: CameraPose): LocateFrame {
        grabbing = false
        val f = LocateFrame(locNext++, grey, w, h, CameraPose(pose.centre, pose.rot, pose.lens.scaledTo(w, h)))
        locFrames.add(f)
        event("locate_frame", "id" to f.id, "size" to "${w}x$h")
        return f
    }

    /**
     * What a Locator made of frames from locateFrame(): those it placed (none, if it placed none). Lines the mesh up
     * once Locate.fromLocated() trusts what's been placed so far, unless it was lined up by hand meanwhile.
     */
    fun located(found: List<Located>, by: String, frames: Int = 1) {
        locTried += frames
        locFound.addAll(found)
        val known = plan?.frame?.takeIf { !it.scaleGuessed }
        val r = Locate.fromLocated(locFound, locFrames, known?.metresPerUnit, known?.scaleSd)
        event("located", "by" to by, "placed" to found.size, "inliers" to found.map { it.inliers }, "rms_px" to found.map { kotlin.math.round(it.rmsPx * 10) / 10 },
            "fit" to (r.fit != null), "frames" to r.frames, "turn_spread_deg" to r.turnSpreadDeg, "centre_off_m" to r.centreOffM, "scale_from" to r.scaleFrom, "why" to r.why)
        val fit = r.fit ?: return
        if (phase != Phase.GHOST && phase != Phase.ROCK) return
        session.located(fit)
        tapMarks.clear(); modelMarks.clear(); meshTap = null
        phase = Phase.CONFIRM; levelsDirty = true
        note = "Lined up by itself, from ${r.frames} view${if (r.frames == 1) "" else "s"} matched to the mesh's photos" + if (by == "server") " (on the server)." else "."
    }

    /** Whether to grab a frame to place against the mesh now: every LOCATE_EVERY_S while steady, from a new view, up to LOCATE_MOST. */
    private fun wantsLocate(pose: CameraPose, tracking: Boolean, t: Double, steady: Boolean): Boolean {
        if (!canLocate || grabbing || !tracking || !steady || t - lastLocGrab < LOCATE_EVERY_S || locFrames.size >= LOCATE_MOST) return false
        val f = locFrames.lastOrNull()?.world
        if (f != null && f.centre.distance(pose.centre) < LOCATE_MOVED_M && angleDeg(f.forward, pose.forward) < LOCATE_TURNED_DEG) return false
        lastLocGrab = t
        return true
    }
    fun addSpot() { if (phase == Phase.CONFIRM) { event("align_add_spot"); phase = Phase.GHOST; ghost = null; note = "" } }

    /** With no mesh: the side the hint is sending you round to can't be reached. */
    /** Starts another ring round the rock, from another height. */
    fun newRing() {
        val n = session.newRing()
        event("ring", "n" to n)
        note = if (n == null) "Line the mesh up with the rock first" else "Ring $n: all round again, from at least ${Coverage.metres(Orbit.RING_APART_M)} higher or lower than before"
        levelsDirty = true
    }
    fun blockSide() {
        val way = lastPose?.let { session.orbit?.blockNext(it) }
        event("block_side", "then" to way)
        note = when (way) {
            null -> "Nothing to mark: no side is being asked for now"
            0 -> "Marked out of reach. Every side you can walk to is done"
            else -> "Marked out of reach: go back round the other way, to your ${if (way < 0) "left" else "right"}"
        }
        levelsDirty = true
    }

    /** Something went wrong the person should know of (a photo that couldn't be saved). */
    fun say(what: String) { event("error", "what" to what); note = what }

    /**
     * A tap on the screen.
     *
     * @param from, along the ray the tapped point looks along, in the world (along a unit vector).
     * @param hit where the phone's tracking says that ray meets the real world, or null if it can't say; asked only
     *   when the tap is one on the rock.
     * @param picture the camera's picture as the tap saw it (Picture, upright as a photo is stored, with the camera as
     *   that picture has it), or null if it can't be had; asked only for a tap on a gym climb's hold.
     */
    fun tap(from: Vec3, along: Vec3, picture: (() -> Pair<Picture, CameraPose>?)? = null, hit: () -> Vec3?) {
        when (phase) {
            Phase.GHOST -> {
                val g = ghost ?: return; val mesh = guide ?: return
                val inv = g.inverse()
                val o = inv.apply(from); val d = inv.dir(along).unit()
                val t = mesh.raycast(o, d)
                if (t == null) { event("tap_model", "hit" to false); note = "That tap missed the model."; return }
                event("tap_model", "hit" to true)
                val spot = o + d * t
                meshTap = spot
                modelMarks.add(spot)
                phase = Phase.ROCK; note = ""
            }
            Phase.ROCK -> {
                val spot = meshTap ?: return
                val world = hit()
                if (world == null) { event("tap_rock", "hit" to false); note = "The phone hasn't made out the rock there yet: move a little and tap again."; return }
                tapMarks.add(Marker("w${tapMarks.size}", world, 0.05, Coverage.LEVEL_DONE, ALIGN, false))
                val fit = session.tap(spot, world)
                event("tap_rock", "hit" to true, "pairs" to session.taps.size, "fit" to (fit != null), "scale" to fit?.scale, "tilt_deg" to fit?.tiltDeg,
                    "scale_vs_server" to session.scaleAgainstServer())
                note = ""
                phase = if (fit != null) { tapMarks.clear(); modelMarks.clear(); levelsDirty = true; Phase.CONFIRM } else {
                    if (session.taps.size >= 2) note = "Those are too nearly one above the other to tell which way it's turned."
                    Phase.GHOST
                }
            }
            Phase.CENTRE -> {
                val world = hit()
                if (world == null) note = "The phone hasn't made out the rock there yet: move a little and tap again."
                else if (gym != null) {
                    val pic = picture?.invoke()
                    val got = pic?.let { (p, pose) -> pixel(pose, from, along)?.let { (u, v) -> gym.pick(p, pose, u, v, world, block, add = false) } }
                    event("gym_pick", "found" to (got != null), "picture" to (pic != null), "name" to gym.colour?.name, "lab" to got?.let { listOf(it.l, it.a, it.b) }, "holds" to gym.holds.size)
                    if (got == null) { note = if (pic == null) "The camera's picture couldn't be read: tap again." else "No colour to go by there: tap right on a hold of the climb."; return }
                    session.startOrbit(world)
                    session.orbit!!.blockBehind(from)
                    session.syncGym()
                    phase = Phase.CAPTURE; levelsDirty = true
                    note = "The climb's holds are ${gym.colour!!.name}: those of its colour are found in each photo, then asked for close up. " +
                        "Other climbs' holds of this colour nearby are found too: Not part of it, then tap one, leaves it out."
                }
                else { session.startOrbit(world); phase = Phase.CAPTURE; note = ""; levelsDirty = true }
            }
            Phase.CAPTURE -> if (gym != null && gymTap == "add") {
                val pic = picture?.invoke()
                val world = hit()
                val got = pic?.let { (p, pose) -> pixel(pose, from, along)?.let { (u, v) -> gym.pick(p, pose, u, v, world, block, add = true) } }
                event("gym_add", "found" to (got != null), "hit" to (world != null), "lab" to got?.let { listOf(it.l, it.a, it.b) }, "holds" to gym.holds.size)
                if (got == null) { note = if (pic == null) "The camera's picture couldn't be read: tap again." else "No colour to go by there: tap right on a hold."; return }
                gymTap = null; session.syncGym(); levelsDirty = true
                note = "Added. ${gym.holds.size} ${gym.colour?.name} hold${if (gym.holds.size == 1) "" else "s"} found so far"
            } else if (gym != null && gymTap == "drop") {
                val id = gym.drop(from, along)
                event("gym_drop", "id" to id, "holds" to gym.holds.size)
                if (id == null) { note = "No hold found near there: tap on one of the rings."; return }
                gymTap = null; session.syncGym(); levelsDirty = true
                note = "Left out: it isn't asked for, nor sent as one of the climb's"
            } else if (marking) {
                val world = hit()
                if (world == null) note = "The phone hasn't made out the rock there yet: move a little and tap again."
                else {
                    val id = session.markHold(world, block)
                    event("mark", "id" to id)
                    marking = false; levelsDirty = true
                    note = if (id == null) "" else "Marked: it's asked for close up, and once the capture is meshed it's a hold of yours there, in a set of beta"
                }
            }
            else -> {}
        }
    }

    // ---- tightening the fit ------------------------------------------------------------------------------------------
    /** A point the phone's tracking has found in the world: kept, one to each 5 cm, for tightening the fit against. */
    /** How many points the tracking is following in this frame: said with each batch of them the app gives addPoint(). */
    fun tracked(n: Int) { trackedNow = n }
    private var trackedNow: Int? = null

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

    /**
     * The tracking came back after losing its place, perhaps in a world of its own: a turn being read starts afresh, and
     * a turn's down holds only for the photos of its spell. The mesh's down comes from those, so too few of them is
     * warned of, with a turn again offered.
     */
    private fun newSpell(lostS: Double) {
        val lvl = session.level
        val inTurn = session.keptWithTurn()
        event("spell", "n" to block, "lost_s" to lostS, "level_spell" to session.levelSpell.takeIf { lvl != null }, "kept_in_level_spell" to inTurn)
        if (phase == Phase.LEVEL) {
            turn = LevelTurn(); turnSpell = block; levelChecked = -1; agree = null
            note = "The phone lost its place, so the turn starts again: its world may have moved."
            return
        }
        if (lvl == null) return
        val tie = CaptureSession.TIE_SHOTS
        if (inTurn >= tie) {
            downNote = "The phone lost its place. The turn's down holds for the $inTurn photos taken since it, not those from now on: " +
                "the mesh is levelled by those photos, and the rest follow through the mesh."
            turnAgainAsked = false
        } else {
            downNote = "The phone lost its place, with only $inTurn photo${if (inTurn == 1) "" else "s"} taken since the turn on the spot " +
                "(at least $tie are needed to level the mesh by it reliably). Its down doesn't hold for photos from now on. " +
                "Turn on the spot again?" + (if (tieLeft > 0) " The $tieLeft photos of the rock asked for after it count for both." else "")
            turnAgainAsked = true
        }
        event("down_note", "turn_again" to turnAgainAsked, "kept_in_level_spell" to inTurn)
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
        val r = found?.let { session.refineWith(work.start, it) }
        event("refine", "points" to work.points.size, "found" to (found != null), "taken" to (r != null), "matched" to found?.matched, "rms_m" to found?.rmsM,
            "moved_m" to found?.fit?.apply(Vec3.ZERO)?.distance(work.start.apply(Vec3.ZERO)))
        r ?: return
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
        now = t
        if (said == null) event("start", "guided" to (plan != null), "mesh" to plan?.mesh, "plan" to plan?.key, "set" to set, "reach" to reach,
            "lens" to "${pose.lens.w}x${pose.lens.h} fx ${pose.lens.fx.toInt()}", "still" to stillLens?.let { "${it.w}x${it.h} fx ${it.fx.toInt()}" })
        // how frames are coming: every SPAN_S, how many, the longest wait between two, and how many untracked
        if (lastFrameT >= 0) spanGap = kotlin.math.max(spanGap, t - lastFrameT)
        lastFrameT = t; spanFrames++; if (!tracking) spanLost++
        if (spanFrom < 0) spanFrom = t
        if (t - spanFrom >= SPAN_S) {
            event("frames", "fps" to spanFrames / (t - spanFrom), "gap_max_s" to spanGap, "untracked" to spanLost, "exposure_s" to exposureS, "gyro" to gyroRadS,
                "ar_pts" to trackedNow, "cloud" to cloud.size)
            spanFrom = t; spanFrames = 0; spanGap = 0.0; spanLost = 0
        }
        val out = frameAt(pose, tracking, gyroRadS, exposureS, t, uprightDeg, accel, sensorOrientation, stillLens)
        if (tracking != saidTracking) { event("tracking", "on" to tracking); saidTracking = tracking }
        if (phase != said) { event("phase", "to" to phase.name); said = phase }
        val h = lastHint
        if ((phase == Phase.CAPTURE || phase == Phase.SCENERY) && h != null) {
            val key = h.kind to h.targetId
            if (key != saidHint) { event("hint", "kind" to h.kind.name, "target" to h.targetId, "text" to h.text, "move_m" to h.moveM.takeIf { it != 0.0 },
                "done" to out.ui.done, "total" to out.ui.total); saidHint = key }
            lastHint = null
        } else if (phase != Phase.CAPTURE && phase != Phase.SCENERY && out.ui.text != saidText && tracking) { event("says", "text" to out.ui.text); saidText = out.ui.text }
        if (out.ui.askMore > 0 && "askMore" != saidText) { event("ask_more", "left" to out.ui.askMore); saidText = "askMore" }
        return out
    }

    private fun frameAt(pose: CameraPose, tracking: Boolean, gyroRadS: Double, exposureS: Double, t: Double, uprightDeg: Int,
                        accel: Vec3?, sensorOrientation: Int, stillLens: Lens?): FlowOut {
        lastPose = pose
        // tracking that was lost for more than a moment may have come back with its world somewhere else: photos and points
        // from then on are another spell's, tied to the earlier ones by what they show rather than by where the phone says it was
        if (!tracking) { if (tracked && lostAt == null) lostAt = t } else {
            if (lostAt?.let { t - it >= LOST_S } == true) {
                past += cloud.values.map { it to block }; cloud.clear(); block++
                newSpell(t - lostAt!!)
            }
            lostAt = null; tracked = true
        }
        val base = ui.copy(phase = phase, kept = session.shotCount, scenery = session.sceneryCount, rejected = rejected, note = note, sensors = sensorNote, auto = session.auto, wide = wide, marking = marking,
            reach = when (reach) { 1 -> "whole rock"; 2 -> "its climbs"; else -> "only the holds of $setName" }, card = cardNext, target = "", canCard = false, askMore = 0,
            pairs = session.taps.size, tracking = tracking, level = null, tieLeft = tieLeft, unplaced = session.unplacedScenery(firstSpell),
            downNote = downNote, turnAgain = turnAgainAsked, gym = gymUi())
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
                if (accel != null) turn.add(pose, accel, sensorOrientation, gyroRadS, t)
                // checked as each direction is read, and now and then as more comes in: a direction that spoils the
                // rest is cleared to be faced again as soon as it shows, and the rest kept
                if (turn.filled != levelChecked || t - levelCheckT >= LEVEL_CHECK_S) {
                    levelChecked = turn.filled; levelCheckT = t
                    val c = turn.check()
                    agree = c?.agreeDeg
                    if (levelNote && 3 !in turn.states()) { note = ""; levelNote = false }      // what was cleared has been faced again
                    if (c != null && c.agreeDeg > levelLimit && (c.withoutDeg <= levelLimit || c.sectors >= LEVEL_CLEAR_FROM)) {
                        event("level_redo", "sector" to c.worst, "of" to c.sectors, "agree_deg" to c.agreeDeg, "without_deg" to c.withoutDeg,
                            "wobble_deg" to turn.sectorWobbles()[c.worst], "wobble_turn_deg" to turn.wobbleTurn, "limit_deg" to levelLimit,
                            *turn.diagnose(LevelTurn.CHECK_LEAST).fields())
                        turn.clear(c.worst)
                        levelNote = true
                        levelChecked = turn.filled
                        note = if (c.withoutDeg <= levelLimit) "One direction was read unsteadily: it is to be faced again, and the rest are kept."
                            else "Too unsteady so far: the directions agree to ${hundredths(c.agreeDeg)}°, and need ${hundredths(levelLimit)}°. " +
                                "The worst is cleared to face again: turn more slowly, elbows in, and pause facing each way. Or ask less of it (Looser)."
                    } else if (turn.filled == turn.sectors) {
                        val d = turn.diagnose()
                        val found = turn.solve()
                        val taken = found != null && found.spreadDeg <= levelLimit
                        event("level", "found" to (found != null), "taken" to taken, "limit_deg" to levelLimit, "wobble_turn_deg" to turn.wobbleTurn,
                            "sector_wobbles" to turn.sectorWobbles(), "spell" to turnSpell, *d.fields())
                        if (found != null && taken) {
                            session.setLevel(found, turnSpell, levelLimit)
                            tilts.add(found.tiltDeg); while (tilts.size > OWN_KEPT) tilts.removeAt(0)
                            onSettings?.invoke(levelLimit, ownTilts)
                            note = "Down found: likely within ${hundredths(found.expectedDeg)}° (the ${found.sectors} directions agree to ${hundredths(found.spreadDeg)}°). " +
                                "The phone's own had it ${hundredths(found.tiltDeg)}° off, and one reading of its accelerometer would have been ${hundredths(found.offsetDeg)}° off. " +
                                "It holds while the phone keeps its place: should it lose it, you're told."
                            phase = returnTo ?: afterLevel; returnTo = null
                        } else if (found == null) {        // readings that aren't gravity's at all: nothing in them to keep
                            turn = LevelTurn(); levelChecked = 0
                            note = "The readings of that turn didn't add up (${d.why}): once more, more slowly."
                        }
                    }
                }
                val lvl = if (phase == Phase.LEVEL) ownDown().let { (own, from) ->
                    LevelUi(turn.states().toList(), turn.facing, turn.wobbleNow, turn.wobbleTurn, LevelTurn.WOBBLE_OK, agree, levelLimit, gyroRadS > LevelTurn.MAX_TURNING,
                        own, from, LEVEL_LIMITS.firstOrNull { it > levelLimit }?.let { helps(it) } ?: false, levelLimit > LEVEL_LIMITS.first(), helps(levelLimit)) } else null
                ui = base.copy(phase = phase, note = note, turnX = 0.0, turnY = 0.0, total = turn.sectors, done = if (phase == Phase.LEVEL) turn.filled else turn.sectors,
                    level = lvl, text = "To find which way is down: turn slowly all the way round on the spot, the phone held as for a photo. " +
                        "It needn't be upright, only steady and at the same angle to you all the way round" +
                        if (panorama) "" else ". The camera only follows where the phone points: no photos are kept during the turn")
                // and a frame for each step of the turn, for a panorama of what's far off
                val grab = if (panorama && !grabbing && pano.wanted(pose, sceneryBlurPx(gyroRadS, exposureS, pose.lens.fx) <= STEADY_SCENERY_PX)) "pano" else null
                if (grab != null) grabbing = true
                return FlowOut(ui, null, false, 0f, null, emptyList(), false, grab = grab)
            }
            Phase.GHOST, Phase.ROCK -> {
                val g = ghost ?: placeGhost(pose).also { ghost = it; ghostScale = it.scale }
                fresh()
                val n = session.taps.size
                val auto = if (!canLocate) "" else if (locFrames.size >= LOCATE_MOST && locTried >= locFrames.size) "\n\nIt couldn't line itself up from what it saw: tap to line it up."
                    else "\n\nOr hold the phone still pointing at the rock, from a step or two away: it's trying to line itself up (${locFrames.size} view${if (locFrames.size == 1) "" else "s"} so far)."
                ui = base.copy(turnX = 0.0, turnY = 0.0, text = (if (phase == Phase.ROCK) "Now tap the same spot on the real rock"
                    else if (n >= 2) "Tap another spot on the small model that you can find on the rock, well to one side of the others"
                    else "Tap a spot on the small model that you can find on the rock (${n + 1} of 2). Pick two far apart, and not one above the other. " +
                        "Drag to turn it, pinch to zoom, two fingers to move it.") + auto)
                val onModel = modelMarks.mapIndexed { i, p -> Marker("m$i", g.apply(p), 0.03 * g.scale / ghostScale, Coverage.LEVEL_PART, ALIGN, false) }
                val grab = if (wantsLocate(pose, tracking, t, sceneryBlurPx(gyroRadS, exposureS, pose.lens.fx) <= STEADY_SCENERY_PX)) { grabbing = true; "locate" } else null
                return FlowOut(ui, g, true, 1.25f, levels, modelHolds(g, pose) + onModel + tapMarks, false, grab = grab)
            }
            Phase.CONFIRM -> {
                fresh()
                ui = base.copy(turnX = 0.0, turnY = 0.0, text = "Is the coloured mesh sitting on the rock? It tightens by itself as you look it over.")
                return FlowOut(ui, session.fit, false, 0.8f, levels, markers, false)
            }
            Phase.SCENERY -> {
                val w = walk!!
                val h = w.hint(pose, uprightDeg)
                lastHint = h
                val grab = if (!grabbing && w.wanted(pose, t, sceneryBlurPx(gyroRadS, exposureS, pose.lens.fx) <= STEADY_SCENERY_PX)) "walk" else null
                if (grab != null) grabbing = true
                ui = base.copy(text = h.text, turnX = h.turnX, turnY = h.turnY, total = w.total, done = w.done, canSkip = false, target = "", canCard = false)
                return FlowOut(ui, session.fit, false, 0.3f, null, emptyList(), false, grab = grab)
            }
            Phase.CENTRE -> {
                ui = base.copy(text = if (gym != null) "Tap a hold of the climb you're capturing: its colour picks out the rest of its holds" else "Tap the middle of the rock")
                return FlowOut(ui, null, false, 0f, null, emptyList(), false)
            }
            Phase.CAPTURE -> {
                val out = session.frame(FrameIn(pose, gyroRadS, exposureS, true, t, uprightDeg, stillLens))
                lastHint = out.hint
                if (out.hint.targetId != lastTarget) { lastTarget = out.hint.targetId; levelsDirty = true }      // the ring round what's asked for moves
                fresh()
                val p = out.progress
                ui = base.copy(text = out.hint.text, turnX = out.hint.turnX, turnY = out.hint.turnY, total = p.total, done = p.done + p.skipped, holds = p.holds,
                    holdsDone = p.holdsDone, canSkip = out.hint.targetId != null && (plan != null || session.orbit?.spot(out.hint.targetId) != null),
                    target = out.status, canCard = out.status.isNotEmpty())
                val left = if (out.hint.kind == Hint.Kind.DONE && !askedMore) session.coverage?.anonLeft() ?: 0 else 0
                if (left > 0) ui = ui.copy(askMore = left, text = "Done. $left more hold${if (left == 1) " has" else "s have"} been marked here by others, privately. Photograph ${if (left == 1) "it" else "them"} too?")
                val tie = tieLeft
                if (tie > 0 && (out.hint.kind == Hint.Kind.DONE || out.redundant)) ui = ui.copy(text = "Take $tie more photo${if (tie == 1) "" else "s"} of the rock " +
                    "from round it (press Take): they place this visit's scenery and panorama on the mesh")
                if (cardNext) ui = ui.copy(text = "Hold a bank card flat on the rock beside the hold, all of it in the picture and out of any glare, and press the shutter")
                // the button's photo waits for the phone to hold still, as one taken by itself does; a card photo is taken only by the button
                val steady = out.steadyS >= CaptureSession.STEADY_FOR_S
                if (wantShot && wantAt == null) wantAt = t
                if (wantShot && !steady && t - wantAt!! > WAIT_S) { wantShot = false; event("shutter_lapsed", "blur_px" to out.blurPx); note = "Not taken: the phone didn't hold still. Brace it, or move more slowly, and press Take again." }
                val pressed = wantShot && steady
                val shoot = (if (cardNext) pressed else out.shoot || pressed) && !inFlight
                if (shoot) { inFlight = true; autoShot = !pressed; shotAt = t; wantShot = false; cardShot = cardNext; event("shoot", "auto" to out.shoot, "still" to out.still, "card" to cardNext, "blur_px" to out.blurPx, "steady_s" to out.steadyS) }
                else if (wantShot && !steady) ui = ui.copy(text = "Hold still: the photo is taken as soon as the phone is steady")
                ui = ui.copy(waiting = wantShot, note = note, ring = session.ringNow, flash = t - shotAt < FLASH_S)
                // ticks while a photo from here would add nothing, quiet for a moment round each photo, so its ding is heard alone
                val tick = out.redundant && !shoot && !inFlight && t - lastTick >= TICK_S && t - lastShotT >= TICK_S
                if (tick) lastTick = t
                return FlowOut(ui, session.fit, false, 0.75f, levels, markers, shoot, out.still, shoot && cardNext, tick = tick)
            }
            else -> { ui = base; return FlowOut(ui, null, false, 0f, null, emptyList(), false) }
        }
    }

    /** The photo frame() asked for, taken. A photo not kept is to be deleted. */
    fun shot(s: ShotIn): ShotOut {
        inFlight = false
        lastShotT = s.t
        // the points found of what it shows, all told, one to each 5 cm: how much of it the tracking could make out
        val cells = cloud.values.count { p -> s.pose.project(p)?.let { it.depth in 0.1..12.0 && it.u in 0.0..s.pose.lens.w.toDouble() && it.v in 0.0..s.pose.lens.h.toDouble() } == true }
        val out = session.shot(s.copy(arSession = spell(s.arSession), card = s.card || cardShot, arPoints = s.arPoints ?: trackedNow, arCells = cells))
        event("shot", "name" to out.record.name, "kept" to out.kept, "why" to out.why.joinToString("; ").ifEmpty { null }, "helped" to out.helped.size,
            "notes" to out.record.notes.joinToString("; ").ifEmpty { null }, "sharp" to s.stats?.sharp, "exposure_s" to s.exposureS, "iso" to s.iso, "focus_m" to s.focusM,
            "corners" to out.record.blob.feat?.n, "tried" to out.record.blob.feat?.tried, "best_links" to out.record.blob.feat?.best, "ar_pts" to out.record.blob.feat?.arPts,
            "ar_cells" to out.record.blob.feat?.arCells)
        if (cardShot && out.kept) { cardNext = false; levelsDirty = true }
        var newHolds = 0
        if (gym != null && out.kept && !s.card && !cardShot && s.blobs != null) {
            val was = gym.holds.size
            val uncheckedWas = gym.unchecked.size
            gym.addPhoto(out.record.name, s.pose, block, s.blobs, s.modelBlobs, cloud.values)
            session.syncGym(); levelsDirty = true
            event("gym_photo", "name" to out.record.name, "colour_blobs" to s.blobs.size, "model_blobs" to s.modelBlobs?.size,
                "matched" to gym.colour?.let { c -> s.blobs.count { c.matches(it) } }, "model_matched" to gym.colour?.let { c -> s.modelBlobs?.count { c.matches(it) } },
                "holds" to gym.holds.size, "new" to gym.holds.size - was, "unchecked" to gym.unchecked.size)
            newHolds = gym.unchecked.size - uncheckedWas
        }
        cardShot = false
        // a gym capture doesn't ask for the compass to be calibrated: its facing hardly matters indoors (Calibrate still can)
        sensorNote = out.record.notes.filter { gym == null || it != Sensors.CALIBRATE_NOTE }.joinToString(" · ")
        val how = if (autoShot) "taken by itself (Auto)" else "taken"
        if (out.kept) {
            note = "Photo ${session.shotCount} $how and kept"; levelsDirty = true
            if (newHolds > 0 && gym != null) note += ". ${gym.unchecked.size} ${gym.colour?.name} hold${if (gym.unchecked.size == 1) "" else "s"} found to check (grey rings): " +
                "only those you keep are asked for close up"
        }
        else { rejected++; note = "Not kept, the photo $how: ${out.why.joinToString("; ")}." }
        return out
    }

    private fun gymUi(): GymUi? {
        val g = gym ?: return null; val c = g.colour ?: return null
        val spots = session.orbit?.spots?.filter { it.gym } ?: emptyList()
        return GymUi(c.name, c.rgb, g.holds.count { it.ok }, spots.count { it.done() }, HoldColour.TOLERANCES.indexOfFirst { it >= c.tolerance - 1e-9 }, HoldColour.TOLERANCES.size,
            gymTap, g.finder, if (g.hasModel || gymModel) listOf(Gym.FINDER_COLOUR, Gym.FINDER_MODEL) else listOf(Gym.FINDER_COLOUR), g.unchecked.size)
    }

    /** The app has a hold-finding model and gives its patches (ShotIn.modelBlobs): it can be chosen as the finder from the start. */
    var gymModel = false

    /** Whether this is a gym climb's capture: its photos' patches are wanted (ShotIn.blobs). */
    val isGym: Boolean get() = gym != null

    /** The photo frame() asked for couldn't be taken. */
    fun noShot(why: String? = null) { event("no_shot", "why" to why); inFlight = false; if (why != null) note = why }

    /**
     * The beta's holds on the small model, in their roles' colours as the site draws them (MODEL_HOLD), those on its
     * side towards the phone: what to line it up by. A hold with no role (one kept private) isn't shown.
     */
    private fun modelHolds(g: Similarity, pose: CameraPose): List<Marker> = plan?.targets.orEmpty().mapNotNull { h ->
        if (!h.isHold || h.role == null) return@mapNotNull null
        val at = g.apply(h.point)
        if (h.normal.norm > 0.5 && (g.dir(h.normal) dot (pose.centre - at)) < 0) return@mapNotNull null
        Marker("h:${h.id}", at, h.r * g.scale, Coverage.LEVEL_OUT, MODEL_HOLD, false, h.role)
    }

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
        const val ALIGN = "align"           // a Marker's kind: a spot tapped to line the mesh up (a white cross, unlike any hold)
        const val MODEL_HOLD = "model_hold" // and a hold on the small model: its role's colour alone
        const val GHOST_AHEAD_M = 1.1
        const val ZOOM_LEAST = 0.3          // how much smaller and bigger than it's first placed the small model can be made
        const val ZOOM_MOST = 25.0
        const val CELL_M = 0.05             // tracked points are kept one to each of these
        const val CLOUD_MOST = 20000
        const val REFINE_POINTS = 2000
        const val REFINE_LEAST = 150
        const val REFINE_EVERY_S = 3.0
        const val LOST_S = 1.0              // tracking lost for longer than this starts a new spell: shorter is a blink, with its world kept
        const val FLASH_S = 0.25            // how long the screen flashes as a photo is taken
        const val TICK_S = 0.8              // how often the tick sounds, while a photo from where the phone is would add nothing
        const val WAIT_S = 8.0              // how long a press of Take waits for the phone to hold still
        const val STEADY_SCENERY_PX = 1.5   // a scenery frame is kept only with the phone turning less than smears it this much
        const val LOCATE_EVERY_S = 1.5      // frames to place against the mesh are grabbed at most this often,
        const val LOCATE_MOST = 8           // this many at most before it's left to taps,
        const val LOCATE_MOVED_M = 0.15     // each from at least this far from the last, or turned this far from it
        const val LOCATE_TURNED_DEG = 8.0
        const val ORBIT_ROCK_M = 2.0        // how far a rock with no mesh is taken to reach from its middle, for the walk round it
        const val SPAN_S = 5.0              // how often how frames are coming is logged
        const val LEVEL_SPREAD_MOST = 0.4   // degrees the directions of a turn on the spot may disagree by, for it to be believed, at first
        /** What that can be set to (looser(), stricter()): down then likely within LevelTurn.ERROR_PER_SPREAD times as much. */
        val LEVEL_LIMITS = listOf(0.4, 0.7, 1.0, 1.5, 2.5)
        const val OWN_KEPT = 10             // the phone's own down's error in its last this many turns is kept, to say what skipping costs
        /**
         * How far off a phone's own down is taken to be, before any turn of its own says: three Pixel 6 captures' photos,
         * each fitted for gravity and the phone's accelerometer offset as a turn is, put the tracking's down 0.28, 0.28 and
         * 0.43 degrees off, each give or take about 0.3.
         */
        const val OWN_GUESS_DEG = 0.5
        const val LEVEL_CLEAR_FROM = 8      // directions read from which the worst is cleared even when clearing it alone won't do
        const val LEVEL_CHECK_S = 0.5

        private fun hundredths(x: Double) = kotlin.math.round(x * 100) / 100

        /** What asking a turn's directions to agree within `limitDeg` comes to, against the phone's own down (`ownDeg` off, as `ownFrom` says). */
        fun limitText(limitDeg: Double, ownDeg: Double, ownFrom: String, canStricter: Boolean = true): String {
            val expected = LevelTurn.ERROR_PER_SPREAD * limitDeg
            return "Asks the directions to agree within ${hundredths(limitDeg)}°: down then likely within ${hundredths(expected)}°, " +
                "against about ${hundredths(ownDeg)}° off for the phone's own ($ownFrom)." +
                (if (expected < ownDeg) "" else if (canStricter) " Warning: at this, the turn is likely no better than the phone's own. Press Stricter."
                 else " Warning: the phone's own down looks about as good as a turn can do here: skipping it costs little.")
        }
    }
}
