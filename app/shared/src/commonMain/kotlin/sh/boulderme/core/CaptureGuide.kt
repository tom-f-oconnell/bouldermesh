package sh.boulderme.core

/**
 * How a capture goes, step by step: shown before the first capture on a phone, and again from Settings. Each step is
 * its heading and what to do in it, naming the capture screen's own buttons.
 */
object CaptureGuide {
    const val TITLE = "How a capture goes"

    val STEPS: List<Pair<String, String>> = listOf(
        "1. Turn on the spot" to
            "First the app finds which way is down. Hold the phone as for a photo and turn slowly all the way round " +
            "where you stand, pausing as you face each way. It needn't be upright, only steady and at the same angle " +
            "to you all the way round: elbows in helps.\n" +
            "A direction read unsteadily is shown to be faced again. If it won't finish, Looser asks less of it. " +
            "Skip trusts the phone's own down instead, which is less exact.",
        "2. Line up the mesh (adding to one)" to
            "A small model of the mesh appears. Tap a spot on it that you can find on the real rock, then tap that " +
            "spot on the rock. Do it twice, with spots far apart and not one above the other. Turn it and Bring it " +
            "here move the model if it's in the way.\n" +
            "Then check that the coloured mesh sits on the rock, and press It fits: start (or Add a spot).",
        "2. Or tap the middle (a new rock)" to
            "For a rock with no mesh yet, tap the middle of the rock on the screen instead.",
        "3. Take photos" to
            "The words and arrows at the top say where to stand and where to point. With Auto on, each photo is " +
            "taken by itself once the phone is steady and the view adds something; Take takes one yourself.\n" +
            "Skip this passes over a part you can't get to, and Can't reach that side over a whole side. Doing: " +
            "switches what counts: the whole rock, or only its climbs. Mark a hold asks for close photos of a hold you tap.",
        "Hold closeups" to
            "Capturing for a set of beta asks for each of its holds from close, all round. Card photo asks for one " +
            "with a bank card flat beside the hold, for its size.",
        "Scenery (if you like)" to
            "Scenery has you walk round the rock filming its surroundings. Back to the rock returns to the photos.",
        "4. Finish and upload" to
            "Finish keeps the photos on the phone, to upload then or later from the home screen (Settings can " +
            "keep uploads to Wi-Fi). The mesh is made on the server: Check if meshed says when it's ready, and " +
            "Capture more adds to it later.",
    )

    /** All of it as plain text, for a dialog that takes only a string. */
    fun text(): String = STEPS.joinToString("\n\n") { (head, body) -> "$head\n$body" }
}
