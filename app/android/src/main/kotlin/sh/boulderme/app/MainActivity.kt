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
    /** getFirst: download the first mesh listed without being asked (for trying it in an emulator). */
    data class Browse(val getFirst: Boolean = false) : Screen
    data object Settings : Screen
    data object Calibrate : Screen
    /** mesh: the mesh being added to, or null for a rock with none. */
    data class Capture(val mesh: String?, val name: String, val set: String? = null) : Screen      // set: the set of beta it's for
    data class Done(val id: String) : Screen
}

class MainActivity : ComponentActivity() {
    lateinit var store: Store
    lateinit var api: Api
    lateinit var sensors: PhoneSensors
    private var screen by mutableStateOf<Screen>(Screen.Home)
    private var arAsked = false     // Google Play Services for AR looked at (and asked for, if missing) since the app started
    private var tryingOut = false   // started by app/ci/screens-android.sh: nothing that would cover its screens

    fun has(permission: String) = ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = Store(this); api = Api(store); sensors = PhoneSensors(this)
        Crashes.watch(store.crashes)
        if (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) tryOut()
        signInFrom(intent)
        if (store.token.isNotEmpty() && store.credit.isBlank()) Thread {     // paired before the app filled this in itself
            api.accountName()?.let { if (store.credit.isBlank()) store.credit = it }
        }.start()
        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
                Surface(Modifier.fillMaxSize()) {
                    val go: (Screen) -> Unit = { screen = it }
                    BackHandler(enabled = screen != Screen.Home && screen !is Screen.Capture) { screen = Screen.Home }
                    var accepted by remember { mutableStateOf(store.terms >= TERMS_VERSION) }
                    if (!accepted) TermsScreen { store.terms = TERMS_VERSION; accepted = true; ensureArCore() }
                    else when (val s = screen) {
                        is Screen.Capture -> CaptureScreen(this, s, go)       // the camera, edge to edge
                        else -> Box(Modifier.fillMaxSize().safeDrawingPadding()) {
                            when (s) {
                                Screen.Home -> HomeScreen(this@MainActivity, go)
                                is Screen.Browse -> BrowseScreen(this@MainActivity, s.getFirst, go)
                                Screen.Settings -> SettingsScreen(this@MainActivity, go)
                                Screen.Calibrate -> CalibrateScreen(this@MainActivity, go)
                                is Screen.Done -> DoneScreen(this@MainActivity, s.id, go)
                                is Screen.Capture -> {}
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
     * screen browse|settings|calibrate|capture`, with `--ez get true` to download the first mesh listed, and `--es
     * mesh <id>` for the mesh to capture: the first downloaded if not given, `none` for a rock with no mesh).
     */
    private fun tryOut() {
        tryingOut = intent.hasExtra("server") || intent.hasExtra("screen")
        intent.getStringExtra("server")?.let { store.server = it }
        store.terms = if (intent.getStringExtra("terms") == "show") 0 else TERMS_VERSION      // the terms' own screen is looked at too
        screen = when (intent.getStringExtra("screen")) {
            "browse" -> Screen.Browse(intent.getBooleanExtra("get", false))
            "settings" -> Screen.Settings
            "calibrate" -> Screen.Calibrate
            "capture" -> {
                val asked = intent.getStringExtra("mesh")
                val mesh = asked?.takeIf { it != "none" } ?: store.downloaded().firstOrNull()?.mesh?.takeIf { asked == null }
                Screen.Capture(mesh, mesh?.let { store.plan(it)?.name } ?: "A rock", intent.getStringExtra("set"))
            }
            else -> Screen.Home
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
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
            runOnUiThread { Toast.makeText(this, said, Toast.LENGTH_LONG).show() }
        }.start()
    }

    override fun onResume() {
        super.onResume()
        sensors.start(has(Manifest.permission.ACCESS_FINE_LOCATION))
        ensureArCore()
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
