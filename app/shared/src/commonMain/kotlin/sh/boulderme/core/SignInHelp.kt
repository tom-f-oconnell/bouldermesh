package sh.boulderme.core

/**
 * What both apps say when they're first opened and not signed in: how to sign in from wherever the account was joined,
 * a browser on this phone first (the site's "Sign the app in", which opens the app by its bouldermesh://pair link),
 * else a computer or another phone (a code from its "Sign in another device").
 */
object SignInHelp {
    /** The site's own address, as people would type it. */
    fun site(server: String) = server.trimEnd('/').removePrefix("https://").removePrefix("http://")

    /** The site's page that opens on the account's panel (frontend/index.html, "#app"), for the button to the browser. */
    fun link(server: String) = server.trimEnd('/') + "/#app"

    const val INTRO = "Sending what you photograph needs your BoulderMesh account. You joined in a web browser: sign this app in from there."

    fun samePhone(server: String) = "Go back to the browser you joined in, on ${site(server)}. If your invite link opened inside an email " +
        "or chat app, that app is the browser to go back to. Press your name at the top, then \"Sign the app in\": this app opens, signed in."

    const val OPEN_NOTE = "This opens the phone's usual browser. If it says you're not signed in there, open the site in the one you joined in instead."

    /** [scan]: the app can read the QR shown with the code (Android's). */
    fun otherDevice(server: String, scan: Boolean) = "There, on ${site(server)}, press your name at the top, then \"Sign in another device\", and " +
        (if (scan) "Scan the QR it shows, or type its code here." else "type the code it shows here.")

    const val NOT_JOINED = "Not joined yet? Open your invite link on this phone first, then come back to this app."

    const val LATER = "Settings has all of this too, for signing in later. Until then you can still get meshes and capture."
}
