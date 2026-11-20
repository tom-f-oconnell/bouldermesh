package sh.boulderme.core

/**
 * How a capture goes, step by step, for each kind of capture (Kind): shown as each capture starts, until "Don't show
 * again" for its kind, and from Settings. Each step is its heading and what to do in it, naming the capture screen's
 * own buttons.
 */
object CaptureGuide {
    const val TITLE = "How a capture goes"

    /**
     * A kind of capture, each with its own steps and its own "Don't show again" (`key`). A gym climb's holds are always
     * asked for close up; a mesh got from the server is added to as an outdoor one, gym or not, as the app can't tell.
     */
    enum class Kind(val key: String, val title: String) {
        OUTDOOR_NEW("outdoor_new", "An outdoor capture"),
        OUTDOOR_ADD("outdoor_add", "Adding to a mesh"),
        CLOSEUPS("closeups", "Hold closeups"),
        GYM_NEW("gym_new", "A gym capture");

        val adding: Boolean get() = this == OUTDOOR_ADD || this == CLOSEUPS
    }

    /** The kind of a capture: of a gym climb (with no mesh), adding to a mesh, or for one of its sets of beta (`set`). */
    fun kind(mesh: String?, set: String?, gym: Boolean): Kind = when {
        mesh == null && gym -> Kind.GYM_NEW
        mesh == null -> Kind.OUTDOOR_NEW
        set != null -> Kind.CLOSEUPS
        else -> Kind.OUTDOOR_ADD
    }

    private val TURN = "1. Turn on the spot" to
        "First the app finds which way is down. Hold the phone as for a photo and turn slowly all the way round " +
        "where you stand, pausing as you face each way. It needn't be upright, only steady and at the same angle " +
        "to you all the way round: elbows in helps.\n" +
        "A direction read unsteadily is shown to be faced again. If it won't finish, Looser asks less of it. " +
        "Skip trusts the phone's own down instead, which is less exact."

    private val LINE_UP = "2. Line up the mesh" to
        "A small model of the mesh appears. Tap a spot on it that you can find on the real rock, then tap that " +
        "spot on the rock. Do it twice, with spots far apart and not one above the other. As in the site's viewer, " +
        "drag one finger to turn the model, pinch to zoom in on a spot, and drag two fingers to move it; Turn it and " +
        "Bring it here (back as it first was) help too.\n" +
        "Then check that the coloured mesh sits on the rock, and press It fits: start (or Add a spot)."

    private val MIDDLE = "2. Tap the middle" to "Tap the middle of the rock on the screen."

    private val HOLD = "2. Tap one of the climb's holds" to
        "Tap right on a hold of the climb you're capturing. Its colour picks out the climb's other holds in each photo " +
        "from then on."

    private val PHOTOS = "3. Take photos" to
        "The words and arrows at the top say where to stand and where to point. With Auto on, each photo is " +
        "taken by itself once the phone is steady and the view adds something; Take takes one yourself.\n" +
        "Skip this passes over a part you can't get to, and Can't reach that side over a whole side. Doing: " +
        "switches what counts: the whole rock, or only its climbs. Mark a hold asks for close photos of a hold you tap."

    private val CLOSE = "3. Photograph the holds close up" to
        "The words and arrows at the top lead you to each of the set's holds in turn, asking for it from close, all " +
        "round: on the way in, a photo at each halving of the distance ties the close ones to the rest. Card photo " +
        "asks for one with a bank card flat beside the hold, for its size. Skip this passes over a hold you can't get to."

    private val GYM_PHOTOS = "3. Walk along the wall" to
        "The words at the top say where to stand. Walk along the wall, not round it: each hold of the climb's colour " +
        "is found in the photos, then asked for close up. Other climbs' holds of the same colour nearby are found too: " +
        "Not part of it, then tap the hold, leaves one out. Add a hold, then tap it, adds one that was missed. " +
        "Colour: narrower or wider changes how alike a hold's colour must be."

    private val SCENERY = "Scenery (if you like)" to
        "Scenery has you walk round the rock filming its surroundings. Back to the rock returns to the photos."

    private val FINISH = "4. Finish and upload" to
        "Finish keeps the photos on the phone, to upload then or later from the home screen (Settings can " +
        "keep uploads to Wi-Fi). The mesh is made on the server: Check if meshed says when it's ready, and " +
        "Get meshes… gets it to view."

    /** The steps of a kind of capture. */
    fun steps(kind: Kind): List<Pair<String, String>> = when (kind) {
        Kind.OUTDOOR_NEW -> listOf(TURN, MIDDLE, PHOTOS, SCENERY, FINISH)
        Kind.OUTDOOR_ADD -> listOf(TURN, LINE_UP, PHOTOS, SCENERY, FINISH)
        Kind.CLOSEUPS -> listOf(TURN, LINE_UP, CLOSE, FINISH)
        Kind.GYM_NEW -> listOf(TURN, HOLD, GYM_PHOTOS, FINISH)
    }

    /** A dialog's title for a kind of capture. */
    fun title(kind: Kind): String = "${kind.title}: how it goes"

    /** All of a kind's steps as plain text, for a dialog that takes only a string. */
    fun text(kind: Kind): String = steps(kind).joinToString("\n\n") { (head, body) -> "$head\n$body" }

    /** What a phone's store keeps of the kinds not to show again: their keys, comma-separated. */
    fun hidden(kept: String): Set<String> = kept.split(",").filter { it.isNotBlank() }.toSet()

    fun hide(kept: String, kind: Kind): String = (hidden(kept) + kind.key).sorted().joinToString(",")

    /**
     * Asked before the phone's first outdoor capture, while its compass hasn't been calibrated: whether to do that first.
     * A capture's mesh is turned to face the way its photos' compass readings say, where they agree.
     */
    const val COMPASS_TITLE = "Calibrate the compass first?"
    const val COMPASS_TEXT =
        "Each photo records which way the phone faced, which sets which way the mesh faces on the map. An " +
        "uncalibrated phone compass is often 20-30° off, and further near metal. After half a minute of calibrating " +
        "(turning the phone every which way, away from cars and bolts) it's typically within about 5-10°.\n" +
        "You can also do it any time from Calibrate on the home screen."
}
