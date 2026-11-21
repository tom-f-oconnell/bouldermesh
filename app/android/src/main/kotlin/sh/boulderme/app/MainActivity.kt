package sh.boulderme.app

import sh.boulderme.core.TERMS_VERSION
import android.Manifest
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.window.DialogWindowProvider
import android.view.Gravity
import sh.boulderme.core.HALF_SHARE
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.remember
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.core.content.ContextCompat
import android.widget.Toast
import android.os.Handler
import android.os.Looper
import com.google.ar.core.ArCoreApk

/** Where the app is. */
sealed interface Screen {
    data object Home : Screen
    /** getFirst: download the first mesh listed, with its textures, without being asked anything (for trying it in an emulator). */
    data class Browse(val getFirst: Boolean = false) : Screen
    data object Settings : Screen
    /** On first opening, not signed in: how to sign in from where the account was joined. */
    data object SignIn : Screen
    data object Calibrate : Screen
    /** mesh: the mesh being added to, or null for a rock with none. */
    data class Capture(val mesh: String?, val name: String, val set: String? = null,      // set: the set of beta it's for
                       val focus: sh.boulderme.core.Focus? = null,                         // for BetaDrafts.FOCUS: the sets and holds chosen
                       val reopen: String? = null,                                         // a capture finished earlier, to carry on (Reopen)
                       val gym: Boolean = false) : Screen                                  // a gym climb, its holds found by their colour
    data class Done(val id: String) : Screen
    /** Before uploading: the holds the app found by itself, to keep or leave out (HoldReview). */
    data class Holds(val id: String) : Screen
    /** A capture on this phone: what it holds, by what each part was for; and one of its photos (or scenery's frames). */
    data class Upload(val id: String) : Screen
    data class Photo(val id: String, val name: String, val scenery: Boolean, val group: String) : Screen
    /** A mesh in the site's own viewer; back: where Back goes; choose: its sets and holds chosen there for hold closeups (?choose). */
    data class Viewer(val mesh: String, val name: String, val back: Screen = Home, val choose: Boolean = false) : Screen
}

/** Where Back goes from a screen. */
fun backOf(s: Screen): Screen = when (s) {
    is Screen.Photo -> Screen.Upload(s.id)
    is Screen.Holds -> Screen.Done(s.id)
    is Screen.Viewer -> s.back
    else -> Screen.Home
}

class MainActivity : ComponentActivity() {
    lateinit var store: Store
    lateinit var api: Api
    lateinit var sensors: PhoneSensors
    private var screen by mutableStateOf<Screen>(Screen.Home)
    private var topHalf by mutableStateOf(false)    // the account's role draws the app in the screen's top half only (Store.topHalf)
    private var arAsked = false     // Google Play Services for AR looked at (and asked for, if missing) since the app started
    private var tryingOut = false   // started by app/ci/screens-android.sh: nothing that would cover its screens

    fun has(permission: String) = ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = Store(this); api = Api(store); sensors = PhoneSensors(this)
        Crashes.watch(store.crashes)
        if (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) tryOut()
        if (store.token.isEmpty() && !store.signInAsked && screen == Screen.Home) screen = Screen.SignIn
        signInFrom(intent)
        if (intent?.action == Updates.ACTION) Updates.installerSaid(this, intent)
        topHalf = store.topHalf
        store.onTopHalf = { v -> runOnUiThread { topHalf = v } }
        // the account's name (if paired before the app filled it in itself) and whether its role draws in the top half only
        if (store.token.isNotEmpty() && !tryingOut && store.online()) Thread {
            api.accountName()?.let { if (store.credit.isBlank()) store.credit = it }
        }.start()
        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
              CompositionLocalProvider(LocalTopHalf provides topHalf) {
               Box(Modifier.fillMaxSize().background(Color.Black)) {
                Surface(Modifier.fillMaxWidth().fillMaxHeight(if (topHalf) HALF_SHARE.toFloat() else 1f)) {
                    val go: (Screen) -> Unit = { screen = it }
                    BackHandler(enabled = screen != Screen.Home && screen !is Screen.Capture) {
                        if (screen == Screen.SignIn) store.signInAsked = true
                        screen = backOf(screen)
                    }
                    var accepted by remember { mutableStateOf(store.terms >= TERMS_VERSION) }
                    if (!accepted) TermsScreen { store.terms = TERMS_VERSION; accepted = true; ensureArCore() }
                    else when (val s = screen) {
                        is Screen.Capture -> CaptureScreen(this@MainActivity, s, go)       // the camera, edge to edge
                        else -> Box(Modifier.fillMaxSize().safeDrawingPadding()) {
                            when (s) {
                                Screen.Home -> HomeScreen(this@MainActivity, go)
                                is Screen.Browse -> BrowseScreen(this@MainActivity, s.getFirst, go)
                                Screen.Settings -> SettingsScreen(this@MainActivity, go)
                                Screen.SignIn -> SignInScreen(this@MainActivity, go)
                                Screen.Calibrate -> CalibrateScreen(this@MainActivity, go)
                                is Screen.Done -> DoneScreen(this@MainActivity, s.id, go)
                                is Screen.Holds -> HoldsScreen(this@MainActivity, s.id, go)
                                is Screen.Upload -> UploadScreen(this@MainActivity, s.id, go)
                                is Screen.Photo -> PhotoScreen(this@MainActivity, s, go)
                                is Screen.Viewer -> ViewerScreen(this@MainActivity, s, go)
                                is Screen.Capture -> {}
                            }
                        }
                    }
                }
               }
              }
            }
        }
    }

    /**
     * In a debug build, for trying it in an emulator with nobody to press anything (app/ci/screens-android.sh): extras
     * on the intent that starts it set the server (`--es server <address>`) and open a screen straight away (`--es
     * screen browse|settings|signin|calibrate|capture|viewer|upload|photo`, with `--ez get true` to download the first mesh listed,
     * `--es mesh <id>` for the mesh to capture: the first downloaded if not given, `none` for a rock with no mesh; the
     * viewer shows the first downloaded; upload and photo show a capture on the phone, `--es capture <id>` or the newest;
     * `--es sample <url of a zip>` puts a capture on the phone first).
     */
    private fun tryOut() {
        tryingOut = intent.hasExtra("server") || intent.hasExtra("screen")
        intent.getStringExtra("server")?.let { store.server = it }
        // CI's runs skip the terms (but for the terms' own screen); anyone else's start of a debug build, as testers install
        // from android-latest, is asked to accept them as a release build's would be
        if (tryingOut || intent.hasExtra("terms")) store.terms = if (intent.getStringExtra("terms") == "show") 0 else TERMS_VERSION
        if (tryingOut) store.signInAsked = true     // the prompt to sign in only on its own screen (`--es screen signin`)
        // a capture to put on the phone first, as a zip of its folder (app/ci/sample_capture.py): the shell can't write
        // into the app's files on Android 11, so it's fetched from the machine running the emulator
        intent.getStringExtra("sample")?.let { url ->
            Thread {
                runCatching {
                    java.util.zip.ZipInputStream(java.net.URL(url).openStream()).use { zip ->
                        while (true) {
                            val e = zip.nextEntry ?: break
                            val to = java.io.File(store.captures, e.name).canonicalFile
                            if (!to.path.startsWith(store.captures.canonicalPath + "/") || e.isDirectory) continue
                            to.parentFile?.mkdirs(); to.outputStream().use { zip.copyTo(it) }
                        }
                    }
                }.onFailure { android.util.Log.e("BoulderMesh", "no sample capture from $url", it) }
            }.apply { start(); join() }
        }
        screen = when (intent.getStringExtra("screen")) {
            "browse" -> Screen.Browse(intent.getBooleanExtra("get", false))
            "settings" -> Screen.Settings
            "signin" -> Screen.SignIn
            "calibrate" -> Screen.Calibrate
            "capture" -> {
                val asked = intent.getStringExtra("mesh")
                val mesh = asked?.takeIf { it != "none" } ?: store.downloaded().firstOrNull()?.mesh?.takeIf { asked == null }
                Screen.Capture(mesh, mesh?.let { store.plan(it)?.name } ?: "A rock", intent.getStringExtra("set"), gym = intent.getBooleanExtra("gym", false))
            }
            "viewer" -> store.downloaded().firstOrNull()?.let { Screen.Viewer(it.mesh, it.name) } ?: Screen.Home
            "upload", "photo" -> (intent.getStringExtra("capture") ?: store.captureIds().firstOrNull())?.let { id ->
                val first = store.contents(id).first.firstOrNull { it.photos.isNotEmpty() }
                if (intent.getStringExtra("screen") == "photo" && first != null) Screen.Photo(id, first.photos.first(), first.scenery, first.title) else Screen.Upload(id)
            } ?: Screen.Home
            else -> Screen.Home
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.action == Updates.ACTION) return Updates.installerSaid(this, intent)
        signInFrom(intent)
    }

    /**
     * The site's "Sign the app in" on this phone (frontend/index.html, appsHtml()): bouldermesh://pair?code=<a pairing
     * code of the account's>&server=<the site>. Signs in with the code, crediting photos to the account's name if
     * they're credited to nobody yet, and says how it went. A link for a server other than the app's is refused, so
     * a link from elsewhere can't send this phone's captures somewhere else.
     */
    private fun signInFrom(intent: Intent?) {
        val link = intent?.data?.takeIf { it.scheme == "bouldermesh" && it.host == "pair" } ?: return
        intent.data = null      // once: not again as the activity is made again
        val code = link.getQueryParameter("code")?.takeIf { it.isNotBlank() } ?: return
        val server = link.getQueryParameter("server")?.trimEnd('/')
        if (server != null && server != store.server.trimEnd('/')) {
            Toast.makeText(this, "That link is for $server, and this app sends to ${store.server}: change Server in Settings first.", Toast.LENGTH_LONG).show()
            return
        }
        Thread {
            val said = try {
                val name = api.signIn(code)
                if (name != null) "Signed in as $name" + (if (store.credit == name) ": photos are credited to $name." else ".") else "Paired: this phone can upload now."
            } catch (e: Exception) { "Signing in failed: ${e.message}" }
            runOnUiThread {
                Toast.makeText(this, said, Toast.LENGTH_LONG).show()
                if (store.token.isNotEmpty() && screen == Screen.SignIn) screen = Screen.Home
            }
        }.start()
    }

    override fun onResume() {
        super.onResume()
        sensors.start(has(Manifest.permission.ACCESS_FINE_LOCATION))
        ensureArCore()
        if (!tryingOut && store.online()) Updates.look(unlessWithinMs = 10 * 60_000L)     // a newer build, offered on the Home screen
    }

    /**
     * Google Play Services for AR, which capturing needs, asked for as soon as the terms are accepted (the Play Store's
     * own prompt, which needs signal), rather than first at the rock, where there may be none. Once each time the app
     * starts; a phone that can't run it is told so. Capturing asks again if it's still missing (ArController.resume()).
     */
    private fun ensureArCore() {
        if (arAsked || tryingOut || store.terms < TERMS_VERSION) return
        val ar = ArCoreApk.getInstance()
        val got = ar.checkAvailability(this)
        if (got.isTransient) {      // still finding out: again shortly
            Handler(Looper.getMainLooper()).postDelayed({ ensureArCore() }, 300)
            return
        }
        arAsked = true
        when (got) {
            ArCoreApk.Availability.SUPPORTED_NOT_INSTALLED, ArCoreApk.Availability.SUPPORTED_APK_TOO_OLD -> try {
                ar.requestInstall(this, true)
            } catch (e: Exception) {
                Toast.makeText(this, "Capturing needs Google Play Services for AR, which couldn't be installed now (${e.message}): " +
                    "it's asked for again when you capture, with signal.", Toast.LENGTH_LONG).show()
            }
            ArCoreApk.Availability.UNSUPPORTED_DEVICE_NOT_CAPABLE -> Toast.makeText(this,
                "This phone can't run Google Play Services for AR, which capturing needs: the rest of the app works.", Toast.LENGTH_LONG).show()
            else -> {}
        }
    }

    override fun onPause() {
        sensors.stop()
        super.onPause()
    }
}

/** Whether the app is drawn in the screen's top half only (Store.topHalf), for what's drawn outside its own content. */
val LocalTopHalf = compositionLocalOf { false }

/** Called inside a dialog's content: puts the dialog in the screen's top half too, when the app is drawn there only. */
@Composable
fun KeepDialogInTopHalf() {
    if (LocalTopHalf.current) (LocalView.current.parent as? DialogWindowProvider)?.window?.setGravity(Gravity.TOP)
}
